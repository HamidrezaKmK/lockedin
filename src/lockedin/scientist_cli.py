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


def _colour(text: object, code: str) -> str:
    """Add restrained ANSI colour for people, never for pipes or NO_COLOR users."""
    enabled = not os.environ.get("NO_COLOR") and (bool(os.environ.get("FORCE_COLOR")) or sys.stdout.isatty())
    return f"\033[{code}m{text}\033[0m" if enabled else str(text)


def bold(text: object) -> str: return _colour(text, "1")
def dim(text: object) -> str: return _colour(text, "2")
def cyan(text: object) -> str: return _colour(text, "36")
def violet(text: object) -> str: return _colour(text, "38;5;141")
def green(text: object) -> str: return _colour(text, "32")
def red(text: object) -> str: return _colour(text, "31")


def heading(title: str, subtitle: str = "") -> None:
    print(violet("◆") + " " + bold(title))
    if subtitle:
        print("  " + dim(subtitle))


def welcome() -> None:
    """A useful first impression that does not require an account or network access."""
    print()
    print(violet("╭────────────────────────────────────╮"))
    print(violet("│") + "         " + bold("LockedIn Scientist") + "         " + violet("│"))
    print(violet("│") + "   " + dim("your research workspace, local") + "   " + violet("│"))
    print(violet("╰────────────────────────────────────╯"))
    print()
    print(bold("Get started"))
    print(f"  {cyan('1.')} {dim('Authorize this computer')}\n     {cyan('lockedin-scientist login --server https://lockedin.codes')}")
    print(f"  {cyan('2.')} {dim('See your active bubbles')}\n     {cyan('lockedin-scientist bubbles')}")
    print(f"  {cyan('3.')} {dim('Sync once without starting an assistant')}\n     {cyan('lockedin-scientist sync')}")
    print(f"  {cyan('4.')} {dim('Launch the coding CLI you use')}\n     {cyan('lockedin-scientist codex <bubble-slug>')}\n     {cyan('lockedin-scientist claude <bubble-slug>')}\n     {cyan('lockedin-scientist agy <bubble-slug>')}")
    print()
    print(dim("Use --help to see every command. Your workspace stays synchronized while you work."))


def data_root() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / APP
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / APP


def config_path() -> Path: return data_root() / "accounts.json"


def client_install_path() -> Path: return data_root() / "client" / "scientist_cli.py"


def command_bin_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "LockedInScientist" / "bin"
    return Path.home() / ".local" / "bin"


def load_config() -> dict:
    p = config_path()
    return json.loads(p.read_text()) if p.exists() else {"accounts": []}


def save_config(cfg: dict) -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps(cfg, indent=2) + "\n")
    try: os.chmod(config_path(), 0o600)
    except OSError: pass


def uninstall(*, purge_data: bool, assume_yes: bool) -> None:
    """Remove the installed standalone client without silently deleting research files."""
    installed = client_install_path()
    running = Path(__file__).resolve()
    if running != installed.resolve():
        raise RuntimeError("This is a development copy, not the installed standalone client. "
                           "Run the command installed in your user PATH to uninstall it.")
    if not assume_yes:
        if not sys.stdin.isatty():
            raise RuntimeError("Refusing a non-interactive uninstall. Re-run with --yes after reviewing the options.")
        scope = "the client, commands, authorization, local mirror, and sync state" if purge_data else "the client and command wrappers (your local mirror and authorization stay)"
        answer = input(f"Remove {scope}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print(dim("Uninstall cancelled."))
            return

    errors: list[str] = []
    for name in ("lockedin-scientist", "lockedin_scientist", "lockedin-scientist.cmd", "lockedin_scientist.cmd"):
        try: (command_bin_dir() / name).unlink(missing_ok=True)
        except OSError as exc: errors.append(f"{name}: {exc}")
    try: shutil.rmtree(installed.parent)
    except FileNotFoundError: pass
    except OSError as exc: errors.append(f"client files: {exc}")
    if purge_data:
        try: shutil.rmtree(data_root())
        except FileNotFoundError: pass
        except OSError as exc: errors.append(f"client data: {exc}")
    if errors:
        raise RuntimeError("Some files could not be removed: " + "; ".join(errors))
    print(green("✓") + " LockedIn Scientist was removed.")
    if purge_data:
        print(dim("Authorization, local mirror, and sync state were also removed."))
    else:
        print(dim("Your local mirror and authorization were kept. Reinstall later to resume."))


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
    heading("Authorize this computer", "A browser window will open. Sign in there, then return here.")
    print("\n" + cyan(url) + "\n")
    webbrowser.open(url)
    until = time.time() + start["expires_in"]
    while time.time() < until:
        time.sleep(start["interval"])
        out = request(server, "GET", f"/api/scientist/v1/device/{start['device_code']}/token")
        if out.get("status") == "authorized":
            cfg = load_config(); accounts = [a for a in cfg["accounts"] if not (a["server"] == server and a["user"] == out["user"])]
            accounts.append({"server": server, "user": out["user"], "token": out["token"]})
            cfg["accounts"] = accounts; save_config(cfg)
            print(green("✓") + f" Authorized {bold(out['user'])} on {dim(server)}.")
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
        # Bases are client bookkeeping, not mirrored research content.  Keep new bases outside
        # the workspace: an older client may have created ``.lockedin-scientist/bases`` with
        # restrictive ownership, but that must never prevent a user from syncing their work.
        self.base_dir = data_root() / "runtime" / "users" / account["user"] / "bases"
        self.legacy_base_dir = self.root / ".lockedin-scientist" / "bases"
        self.root.mkdir(parents=True, exist_ok=True)

    def state(self) -> dict:
        return json.loads(self.state_path.read_text()) if self.state_path.exists() else {"files": {}}

    def save_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2) + "\n")

    def local_raw(self, rel: str) -> bytes:
        p = self.root / rel
        return p.read_bytes() if p.exists() else b""

    def _base_name(self, rel: str) -> str:
        import hashlib
        return hashlib.sha256(rel.encode("utf-8")).hexdigest()

    def save_base(self, rel: str, raw: bytes) -> str:
        """Keep conflict bases out of the small revision state manifest."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        name = self._base_name(rel)
        path = self.base_dir / name
        if not path.exists() or path.read_bytes() != raw:
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(raw)
            tmp.replace(path)
        return name

    def base_raw(self, old: dict, rel: str) -> bytes:
        name = old.get("base_file")
        if name:
            for directory in (self.base_dir, self.legacy_base_dir):
                try: return (directory / name).read_bytes()
                except OSError: pass
        if old.get("content_b64"):  # one-time migration from v1 state files
            return base64.b64decode(old["content_b64"])
        return b""

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
        state = self.state()
        # v1 stored every file's base64 content in state. Move writable bases into separate files,
        # turning old large state files into small revision maps.
        tracked = {}
        for rel, old in state.get("files", {}).items():
            if not isinstance(old, dict) or not old.get("revision"):
                continue
            tracked[rel] = {"revision": old["revision"]}
            if self.writable(rel):
                base = self.base_raw(old, rel)
                tracked[rel]["base_file"] = self.save_base(rel, base)
        state["files"] = tracked

        def get_manifest() -> dict[str, dict]:
            data = request(self.account["server"], "GET", "/api/scientist/v1/manifest",
                           token=self.account["token"])
            return {item["path"]: item for item in data["files"]}

        def get_files(paths: list[str]) -> list[dict]:
            files = []
            for start in range(0, len(paths), 200):
                data = request(self.account["server"], "POST", "/api/scientist/v1/files",
                               {"paths": paths[start:start + 200]}, self.account["token"])
                files.extend(data["files"])
            return files

        by_path = get_manifest()
        writes = []
        for rel, old in list(tracked.items()):
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
                    base = self.base_raw(tracked.get(rel, {}), rel)
                    self.retry(rel, base, local, current)
                    target = self.root / rel; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(current)
            by_path = get_manifest()

        changed = [rel for rel, item in by_path.items()
                   if rel not in tracked or tracked[rel]["revision"] != item["revision"]
                   or not (self.root / rel).exists()]
        for item in get_files(changed):
            rel = item["path"]
            raw = base64.b64decode(item["content_b64"])
            local = self.local_raw(rel)
            old = tracked.get(rel)
            if not old or self.rev(local) == old["revision"] or not self.writable(rel):
                target = self.root / rel; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(raw)
            tracked[rel] = {"revision": item["revision"]}
            if self.writable(rel):
                tracked[rel]["base_file"] = self.save_base(rel, raw)

        # Mirror server-side deletes when no local writable edit would be lost.
        for rel, old in list(tracked.items()):
            if rel in by_path:
                continue
            local = self.local_raw(rel)
            if not self.writable(rel) or self.rev(local) == old["revision"]:
                try: (self.root / rel).unlink()
                except FileNotFoundError: pass
                try: (self.base_dir / old.get("base_file", "")).unlink()
                except (FileNotFoundError, IsADirectoryError): pass
                tracked.pop(rel, None)
        self.save_state(state)


def choose_account() -> dict:
    accounts = load_config().get("accounts", [])
    if not accounts: raise RuntimeError("No account authorized. Run lockedin-scientist login --server https://lockedin.codes")
    return accounts[-1]


def print_bubbles(account: dict) -> None:
    """List approved bubbles without starting an agent or synchronizing the workspace."""
    rows = request(account["server"], "GET", "/api/scientist/v1/bubbles",
                   token=account["token"]).get("bubbles", [])
    if not rows:
        heading("Active bubbles")
        print(dim("No approved bubbles are available for this account yet."))
        return
    rows.sort(key=lambda row: (str(row.get("name", "")).casefold(), str(row.get("slug", ""))))
    heading("Active bubbles", f"{len(rows)} approved workspace{'s' if len(rows) != 1 else ''} for @{account['user']}")
    for index, row in enumerate(rows, start=1):
        print(f"\n  {cyan(f'{index:02d}')}  {bold(row['name'])}")
        print(f"      {dim('slug')}  {cyan(row['slug'])}")
    print("\n" + dim("Start a session with Codex, Claude, or Antigravity:") +
          f"\n  {cyan('lockedin-scientist <codex|claude|agy> <bubble-slug>')}")


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


def _main() -> None:
    # Friendly form: `lockedin-scientist codex my-bubble`.
    if len(sys.argv) >= 2 and sys.argv[1] in ("codex", "claude", "agy"):
        sys.argv.insert(1, "run")
    parser = argparse.ArgumentParser(
        prog=APP,
        description="Keep an authorized LockedIn research workspace synchronized while you use an installed coding CLI.",
        epilog="""Examples:
  lockedin-scientist login --server https://lockedin.codes
  lockedin-scientist bubbles
  lockedin-scientist sync
  lockedin-scientist codex <bubble-slug>
  lockedin-scientist claude <bubble-slug>
  lockedin-scientist agy <bubble-slug>
  lockedin-scientist uninstall

The short model form above is equivalent to `lockedin-scientist run <model> <bubble-slug>`.
`sync` performs one safe pull/push cycle without launching a model.  During a model session,
the workspace is synchronized every five seconds.  Use NO_COLOR=1 for plain terminal output.""",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", title="commands", metavar="COMMAND")
    p_login = sub.add_parser("login", help="Authorize this computer in a browser.",
                             description="Open a browser device-authorization flow for a LockedIn server.")
    p_login.add_argument("--server", required=True, metavar="URL", help="LockedIn server URL, for example https://lockedin.codes.")
    sub.add_parser("sync", help="Pull/push once without launching a coding CLI.",
                   description="Synchronize the local mirror once, then print its location.")
    sub.add_parser("bubbles", help="List active bubble names and slugs; no model or sync.",
                   description="List approved bubbles available to the authorized account.")
    p_uninstall = sub.add_parser("uninstall", help="Remove the standalone client from this computer.",
                                 description="Remove command wrappers and installed client source. Local research data is kept unless explicitly purged.")
    p_uninstall.add_argument("--purge-data", action="store_true", help="Also remove authorization, local mirror, and all Scientist sync state.")
    p_uninstall.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt.")
    p_run = sub.add_parser("run", help="Start Codex, Claude, or Antigravity for one bubble.",
                           description="Sync first, verify the slug, then launch the chosen installed CLI.")
    p_run.add_argument("model", choices=("codex", "claude", "agy"), help="Installed coding CLI to run.")
    p_run.add_argument("bubble", metavar="BUBBLE-SLUG", help="Slug shown by `lockedin-scientist bubbles`.")
    args = parser.parse_args()
    if args.command is None:
        welcome()
        return
    if args.command == "login": login(args.server); return
    if args.command == "uninstall":
        uninstall(purge_data=args.purge_data, assume_yes=args.yes)
        return
    account = choose_account(); mirror = Mirror(account)
    if args.command == "bubbles": print_bubbles(account); return
    if args.command == "sync":
        mirror.sync()
        print(green("✓") + " Synced workspace\n  " + dim(mirror.root))
        return
    if args.command == "run":
        mirror.sync()
        print(green("✓") + f" Synced. Starting {bold(args.model)} in {cyan(args.bubble)}…")
        raise SystemExit(run_agent(args.model, mirror, args.bubble))
    parser.print_help()


def main() -> None:
    try:
        _main()
    except RuntimeError as exc:
        print(red("Error:") + " " + str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__": main()
