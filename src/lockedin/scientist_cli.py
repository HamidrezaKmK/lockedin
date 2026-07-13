"""Installed deterministic supervisor for vendor coding CLIs.

No model calls live here.  It mirrors a LockedIn workspace and delegates the conversation to
the user's existing Codex, Claude Code, or Antigravity executable.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

APP = "lockedin-scientist"


def data_root() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / APP
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / APP


def config_path() -> Path: return data_root() / "accounts.json"


def load_config() -> dict:
    p = config_path()
    return json.loads(p.read_text()) if p.exists() else {"accounts": []}


def save_config(cfg: dict) -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps(cfg, indent=2) + "\n")
    try: os.chmod(config_path(), 0o600)
    except OSError: pass


def request(server: str, method: str, path: str, body: dict | None = None, token: str = "") -> dict:
    data = json.dumps(body).encode() if body is not None else None
    # Some reverse proxies reject Python's default ``Python-urllib/x.y`` bot user
    # agent. Identify this deterministic client without assuming a particular server.
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": f"{APP}/0.1"}
    if token: headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(server.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"server returned {e.code}: {detail}") from e


def login(server: str) -> None:
    server = server.rstrip("/")
    start = request(server, "POST", "/api/scientist/v1/device", {"client_name": APP})
    url = server + start["verification_uri"]
    print("Open this URL to authorize the client:\n" + url)
    webbrowser.open(url)
    until = time.time() + start["expires_in"]
    while time.time() < until:
        time.sleep(start["interval"])
        out = request(server, "GET", f"/api/scientist/v1/device/{start['device_code']}/token")
        if out.get("status") == "authorized":
            cfg = load_config(); accounts = [a for a in cfg["accounts"] if not (a["server"] == server and a["user"] == out["user"])]
            accounts.append({"server": server, "user": out["user"], "token": out["token"]})
            cfg["accounts"] = accounts; save_config(cfg)
            print(f"Authorized {out['user']} on {server}.")
            return
    raise RuntimeError("Device authorization timed out.")


class Mirror:
    def __init__(self, account: dict):
        self.account = account
        # A person's local research mirror should have a predictable, durable location.
        # The server/token live in account metadata, not in this user-facing directory name.
        self.root = data_root() / "data" / "users" / account["user"]
        # One-time migration from the early server-URL-encoded layout.
        legacy_key = urllib.parse.quote(account["server"], safe="")
        legacy = data_root() / "servers" / legacy_key / "data" / "users" / account["user"]
        if not self.root.exists() and legacy.exists():
            self.root.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), str(self.root))
        self.state_path = self.root / ".lockedin-scientist" / "state.json"
        self.root.mkdir(parents=True, exist_ok=True)

    def state(self) -> dict:
        return json.loads(self.state_path.read_text()) if self.state_path.exists() else {"files": {}}

    def save_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2) + "\n")

    def local_raw(self, rel: str) -> bytes:
        p = self.root / rel
        return p.read_bytes() if p.exists() else b""

    @staticmethod
    def rev(raw: bytes) -> str:
        import hashlib
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def writable(rel: str) -> bool:
        p = Path(rel).parts
        return len(p) >= 4 and p[0] == "REPORTS" and ((p[2] == "pages" and rel.endswith(".md")) or p[2] == "assets")

    def retry(self, rel: str, base: bytes, local: bytes, remote: bytes) -> None:
        d = self.root / ".lockedin-scientist" / "retries" / str(int(time.time() * 1000))
        d.mkdir(parents=True, exist_ok=True)
        (d / (Path(rel).name + ".base")).write_bytes(base)
        (d / (Path(rel).name + ".local")).write_bytes(local)
        (d / (Path(rel).name + ".remote")).write_bytes(remote)
        diff = "".join(difflib.unified_diff(base.decode(errors="replace").splitlines(True),
                                             local.decode(errors="replace").splitlines(True),
                                             fromfile="base", tofile="rejected-local"))
        (d / (Path(rel).name + ".patch")).write_text(diff)

    def sync(self) -> None:
        state = self.state(); tracked = state.setdefault("files", {})
        remote = request(self.account["server"], "GET", "/api/scientist/v1/snapshot", token=self.account["token"])
        by_path = {f["path"]: f for f in remote["files"]}
        writes = []
        for rel, old in tracked.items():
            if rel not in by_path or not self.writable(rel): continue
            raw = self.local_raw(rel)
            if self.rev(raw) != old["revision"]:
                writes.append({"path": rel, "base_revision": old["revision"],
                               "content_b64": base64.b64encode(raw).decode()})
        if writes:
            result = request(self.account["server"], "POST", "/api/scientist/v1/push", {"writes": writes}, self.account["token"])
            for conflict in result["conflicts"]:
                rel = conflict["path"]
                if conflict.get("content_b64"):
                    current = base64.b64decode(conflict["content_b64"])
                    local = self.local_raw(rel)
                    base = base64.b64decode(tracked.get(rel, {}).get("content_b64", ""))
                    self.retry(rel, base, local, current)
                    target = self.root / rel; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(current)
            remote = request(self.account["server"], "GET", "/api/scientist/v1/snapshot", token=self.account["token"])
            by_path = {f["path"]: f for f in remote["files"]}
        for rel, item in by_path.items():
            raw = base64.b64decode(item["content_b64"])
            local = self.local_raw(rel)
            old = tracked.get(rel)
            if not old or self.rev(local) == old["revision"] or not self.writable(rel):
                target = self.root / rel; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(raw)
            tracked[rel] = {"revision": item["revision"], "content_b64": item["content_b64"]}
        self.save_state(state)


def choose_account() -> dict:
    accounts = load_config().get("accounts", [])
    if not accounts: raise RuntimeError("No account authorized. Run lockedin-scientist login --server https://…")
    return accounts[-1]


def role(mirror: Mirror, bubble: str) -> str:
    inventory_path = mirror.root / "REPORTS" / bubble / "_lockedin_papers.md"
    inventory = inventory_path.read_text() if inventory_path.exists() else "(inventory unavailable; run lockedin-scientist sync)"
    return f"""You are LockedIn Scientist. Work only in REPORTS/{bubble}/pages and REPORTS/{bubble}/assets.
This workspace is synchronized with the LockedIn website every five seconds. Re-read a page immediately before editing it.
If .lockedin-scientist/retries exists, inspect the newest retry packet and reapply your intended change to the current page instead of restoring stale text.
Never edit credentials, TODOs, bubbles.yaml, or unrelated bubbles. Describe intended report edits before writing.

TERMINAL OUTPUT RULE:
- In normal terminal conversation, do not emit LaTeX commands, delimiters, or raw equation source.
  Explain mathematics in plain language and Unicode where useful (for example, G(n), ω, or t − s).
- Markdown/LaTeX notation is allowed only inside report files being read or edited, or when the
  user explicitly asks to see the exact report text.

PAPER INVENTORY RULES:
- HARD RULE: whenever the user asks which papers/assets are attached, or asks whether that list
  changed, first re-read REPORTS/{bubble}/_lockedin_papers.md from disk in that same turn. Use
  its current contents as the answer; never reuse an earlier answer, infer from citations, or
  scan the whole ASSETS directory and guess membership.
- Attached papers live under ASSETS/<asset-id>/ (metadata, summary, extracted text, and sometimes
  paper.pdf). REPORTS/{bubble}/assets is only for report images, not the paper library.
- Only read papers whose asset ids appear in this inventory. Prefer higher relevance first.

AUTHORITATIVE ATTACHED-PAPER INVENTORY:
{inventory}"""


def require_bubble(mirror: Mirror, bubble: str) -> None:
    """Fail before spawning an agent when the requested bubble isn't an approved workspace."""
    rows = request(mirror.account["server"], "GET", "/api/scientist/v1/bubbles",
                   token=mirror.account["token"]).get("bubbles", [])
    if any(row.get("slug") == bubble for row in rows):
        return
    available = ", ".join(f"{row['slug']} ({row['name']})" for row in rows) or "(none)"
    raise RuntimeError(f"No approved bubble with slug {bubble!r}. Available: {available}")


def run_agent(model: str, mirror: Mirror, bubble: str) -> int:
    require_bubble(mirror, bubble)
    if not shutil.which(model): raise RuntimeError(f"{model} is not installed on PATH.")
    prompt = "Introduce yourself as the LockedIn research-report assistant and ask what to work on."
    instructions = role(mirror, bubble)
    # Use each vendor CLI's ordinary interactive permission behavior.  Do not opt into any
    # bypass/auto-accept mode; Codex can still ask for escalation when it judges it necessary.
    if model == "codex": cmd = ["codex", "--cd", str(mirror.root), "--sandbox", "workspace-write", "--ask-for-approval", "on-request", "--search", "-c", f"developer_instructions={instructions}", prompt]
    elif model == "claude": cmd = ["claude", "--append-system-prompt", instructions, prompt]
    else: cmd = ["agy", "-i", instructions + "\n\n" + prompt]
    stop = threading.Event()
    def supervise():
        while not stop.wait(5):
            try: mirror.sync()
            except Exception as e: print(f"[{APP} sync warning] {e}", file=sys.stderr)
    worker = threading.Thread(target=supervise, daemon=True); worker.start()
    try: return subprocess.run(cmd, cwd=mirror.root).returncode
    finally: stop.set(); mirror.sync()


def main() -> None:
    # Friendly form: `lockedin-scientist codex my-bubble`.
    if len(sys.argv) >= 2 and sys.argv[1] in ("codex", "claude", "agy"):
        sys.argv.insert(1, "run")
    parser = argparse.ArgumentParser(prog=APP)
    sub = parser.add_subparsers(dest="command")
    p_login = sub.add_parser("login"); p_login.add_argument("--server", required=True)
    sub.add_parser("sync")
    p_run = sub.add_parser("run"); p_run.add_argument("model", choices=("codex", "claude", "agy")); p_run.add_argument("bubble")
    args = parser.parse_args()
    if args.command == "login": login(args.server); return
    account = choose_account(); mirror = Mirror(account)
    mirror.sync()
    if args.command == "sync": print(mirror.root); return
    if args.command == "run": raise SystemExit(run_agent(args.model, mirror, args.bubble))
    parser.print_help()


if __name__ == "__main__": main()
