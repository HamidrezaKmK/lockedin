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
_SYNC_WARNING_AFTER = 3


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
    print(f"  {cyan('2.')} {dim('List and select a workspace')}\n     {cyan('lockedin-scientist workspaces')}\n     {cyan('lockedin-scientist switch <workspace-id-or-name>')}")
    print(f"  {cyan('3.')} {dim('See approved bubbles in that workspace')}\n     {cyan('lockedin-scientist bubbles')}")
    print(f"  {cyan('4.')} {dim('Sync once without starting an assistant')}\n     {cyan('lockedin-scientist sync')}")
    print(f"  {cyan('5.')} {dim('Launch the coding CLI you use')}\n     {cyan('lockedin-scientist codex <bubble-slug>')}\n     {cyan('lockedin-scientist claude <bubble-slug>')}\n     {cyan('lockedin-scientist agy <bubble-slug>')}")
    print(f"  {cyan('↻')} {dim('Resume the latest session with syncing')}\n     {cyan('lockedin-scientist resume <codex|claude|agy> <bubble-slug>')}")
    print(f"  {cyan('↗')} {dim('Grant an external directory for one session')}\n     {cyan('lockedin-scientist <codex|claude|agy> <bubble-slug> --add-dir <directory>')}")
    print()
    print(bold("Troubleshooting & cleanup"))
    print(f"  {cyan('↻')} {dim('Replace the mirror with the current website state')}\n     {cyan('lockedin-scientist sync --from-server')}")
    print(f"  {cyan('−')} {dim('Remove the client; keep mirror and authorization')}\n     {cyan('lockedin-scientist uninstall')}")
    print(f"  {cyan('×')} {dim('Remove the client, mirror, authorization, and sync state')}\n     {cyan('lockedin-scientist uninstall --purge-data --yes')}")
    print()
    print(dim("Use --help to see every command. Scientist keeps one selected workspace and synchronizes it while you work."))


def data_root() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / APP
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / APP


def cache_root() -> Path:
    """A writable client-only fallback for machines with a protected data directory."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "LockedInScientist"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "LockedInScientist" / "cache"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / APP


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


def request(server: str, method: str, path: str, body: dict | None = None, token: str = "",
            workspace: str = "") -> dict:
    data = json.dumps(body).encode() if body is not None else None
    # Some reverse proxies reject Python's default ``Python-urllib/x.y`` bot user
    # agent. Identify this deterministic client without assuming a particular server.
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": f"{APP}/0.1"}
    if token: headers["Authorization"] = "Bearer " + token
    if workspace: headers["X-LockedIn-Workspace"] = workspace
    req = urllib.request.Request(server.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"server returned {e.code}: {detail}") from e


def account_request(account: dict, method: str, path: str, body: dict | None = None) -> dict:
    """Request for an account; omit the new header for legacy client state."""
    if account.get("workspace_id"):
        return request(account["server"], method, path, body, account["token"], account["workspace_id"])
    return request(account["server"], method, path, body, account["token"])


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
            ws = request(server, "GET", "/api/scientist/v1/workspaces", token=out["token"])
            accounts.append({"server": server, "user": out["user"], "token": out["token"],
                             "workspace_id": ws.get("personal_workspace_id", "")})
            cfg["accounts"] = accounts; save_config(cfg)
            print(green("✓") + f" Authorized {bold(out['user'])} on {dim(server)}.")
            return
    raise RuntimeError("Device authorization timed out.")


class Mirror:
    def __init__(self, account: dict):
        self.account = account
        # A person's local research mirror should have a predictable, durable location.
        # The server/token live in account metadata, not in this user-facing directory name.
        workspace_id = account.get("workspace_id")
        self.root = (data_root() / "data" / "workspaces" / workspace_id if workspace_id
                     else data_root() / "data" / "users" / account["user"])
        # One-time migration from the early server-URL-encoded layout.
        legacy_key = urllib.parse.quote(account["server"], safe="")
        legacy = data_root() / "servers" / legacy_key / "data" / "users" / account["user"]
        if not self.root.exists() and legacy.exists():
            self.root.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), str(self.root))
        # Synchronization bookkeeping must not live in, or be required by, the mirror. An old
        # client may have created its hidden mirror directory with restrictive ownership. Keep
        # all new state in a client sidecar, falling back to the platform cache when necessary.
        sidecar = data_root() / "runtime" / "users" / account["user"]
        cache_sidecar = cache_root() / "runtime" / "users" / account["user"]
        self.state_path = sidecar / "state.json"
        self.fallback_state_path = cache_sidecar / "state.json"
        self.base_dir = sidecar / "bases"
        self.fallback_base_dir = cache_sidecar / "bases"
        self.retry_dir = sidecar / "retries"
        self.fallback_retry_dir = cache_sidecar / "retries"
        self.legacy_state_path = self.root / ".lockedin-scientist" / "state.json"
        self.legacy_base_dir = self.root / ".lockedin-scientist" / "bases"
        self._volatile_state: dict | None = None
        self.root.mkdir(parents=True, exist_ok=True)

    def state(self) -> dict:
        if self._volatile_state is not None:
            return self._volatile_state
        for path in (self.state_path, self.fallback_state_path, self.legacy_state_path):
            try:
                if path.exists():
                    state = json.loads(path.read_text())
                    self.state_path = path
                    return state if isinstance(state, dict) else {"files": {}}
            except (OSError, json.JSONDecodeError):
                continue
        return {"files": {}}

    def save_state(self, state: dict) -> None:
        # State is an optimization for incremental sync, never a reason to fail a user's edit.
        # Prefer the normal sidecar, then the cache sidecar; retain an in-memory copy only if
        # both are unavailable for this process.
        for path in (self.state_path, self.fallback_state_path):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(state, indent=2) + "\n")
                tmp.replace(path)
                self.state_path = path
                self._volatile_state = None
                return
            except OSError:
                continue
        self._volatile_state = state

    def local_raw(self, rel: str) -> bytes:
        p = self.root / rel
        return p.read_bytes() if p.exists() else b""

    def local_writable_paths(self) -> list[str]:
        """Return new report files that an agent may legitimately create locally.

        The server manifest cannot mention a file that has never been uploaded.  Enumerating the
        two direct report-file locations closes that gap without exposing sidecar state, paper
        library files, nested paths, or symlinks outside the mirror.
        """
        reports = self.root / "REPORTS"
        if not reports.exists():
            return []
        candidates = list(reports.glob("*/pages/*.md")) + list(reports.glob("*/assets/*"))
        return sorted(p.relative_to(self.root).as_posix() for p in candidates
                      if p.is_file() and not p.is_symlink()
                      and self.writable(p.relative_to(self.root).as_posix()))

    def _base_name(self, rel: str) -> str:
        import hashlib
        return hashlib.sha256(rel.encode("utf-8")).hexdigest()

    def save_base(self, rel: str, raw: bytes) -> str:
        """Keep conflict bases out of the small revision state manifest."""
        name = self._base_name(rel)
        for directory in (self.base_dir, self.fallback_base_dir):
            try:
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / name
                if not path.exists() or path.read_bytes() != raw:
                    tmp = path.with_suffix(".tmp")
                    tmp.write_bytes(raw)
                    tmp.replace(path)
                self.base_dir = directory
                return name
            except OSError:
                continue
        # A base only improves the conflict diff. Sync can safely proceed without one.
        return ""

    def base_raw(self, old: dict, rel: str) -> bytes:
        name = old.get("base_file")
        if name:
            for directory in (self.base_dir, self.fallback_base_dir, self.legacy_base_dir):
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
        return len(p) == 4 and p[0] == "REPORTS" and ((p[2] == "pages" and rel.endswith(".md")) or p[2] == "assets")

    def retry(self, rel: str, base: bytes, local: bytes, remote: bytes) -> None:
        for directory in (self.retry_dir, self.fallback_retry_dir):
            try:
                d = directory / str(int(time.time() * 1000))
                d.mkdir(parents=True, exist_ok=True)
                (d / (Path(rel).name + ".base")).write_bytes(base)
                (d / (Path(rel).name + ".local")).write_bytes(local)
                (d / (Path(rel).name + ".remote")).write_bytes(remote)
                diff = "".join(difflib.unified_diff(base.decode(errors="replace").splitlines(True),
                                                     local.decode(errors="replace").splitlines(True),
                                                     fromfile="base", tofile="rejected-local"))
                (d / (Path(rel).name + ".patch")).write_text(diff)
                self.retry_dir = directory
                return
            except OSError:
                continue

    def recover_from_server(self) -> Path:
        """Archive local safe files, discard sync metadata, then pull the server authority."""
        backup = cache_root() / "recovery" / "users" / self.account["user"] / str(int(time.time()))
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root)
            if ".lockedin-scientist" in rel.parts or path.name == "paper.pdf" or "chats" in rel.parts:
                continue
            try:
                target = backup / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            except OSError:
                continue
        self._volatile_state = {"files": {}}
        for path in (self.state_path, self.fallback_state_path, self.legacy_state_path):
            try: path.unlink()
            except OSError: pass
        self.save_state({"files": {}})
        self.sync()
        return backup

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
                if name := self.save_base(rel, base):
                    tracked[rel]["base_file"] = name
        state["files"] = tracked

        def get_manifest() -> dict[str, dict]:
            data = account_request(self.account, "GET", "/api/scientist/v1/manifest")
            return {item["path"]: item for item in data["files"]}

        def get_files(paths: list[str]) -> list[dict]:
            files = []
            for start in range(0, len(paths), 200):
                data = account_request(self.account, "POST", "/api/scientist/v1/files",
                                       {"paths": paths[start:start + 200]})
                files.extend(data["files"])
            return files

        by_path = get_manifest()
        writes = []
        new_pages = []
        for rel, old in list(tracked.items()):
            if rel not in by_path or not self.writable(rel): continue
            raw = self.local_raw(rel)
            if self.rev(raw) != old["revision"]:
                writes.append({"path": rel, "base_revision": old["revision"],
                               "content_b64": base64.b64encode(raw).decode()})
        # New local report pages and figures have no prior server revision, so they are absent
        # from ``tracked``. Push them against the empty-file revision. Files that already exist
        # remotely stay server-authoritative until they are first pulled into the mirror. The
        # revision manifest remains the metadata gate: no content is downloaded or uploaded for
        # paths whose revision/name has not changed.
        for rel in self.local_writable_paths():
            if rel not in tracked and rel not in by_path:
                parts = Path(rel).parts
                if parts[2] == "pages":
                    new_pages.append(rel)
                else:
                    writes.append({"path": rel, "base_revision": self.rev(b""),
                                   "content_b64": base64.b64encode(self.local_raw(rel)).decode()})
        for rel in new_pages:
            parts = Path(rel).parts
            result = account_request(self.account, "POST", "/api/scientist/v1/pages", {
                "bubble": parts[1], "page_slug": Path(parts[3]).stem,
                "content_b64": base64.b64encode(self.local_raw(rel)).decode(),
                "base_revision": self.rev(b""),
            })
            for applied in result["applied"]:
                raw = base64.b64decode(applied.get("content_b64", "")) or self.local_raw(rel)
                target = self.root / rel; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(raw)
                tracked[rel] = {"revision": applied["revision"]}
                if name := self.save_base(rel, raw):
                    tracked[rel]["base_file"] = name
            for conflict in result["conflicts"]:
                current = base64.b64decode(conflict.get("content_b64", ""))
                self.retry(rel, b"", self.local_raw(rel), current)
                if conflict.get("content_b64"):
                    target = self.root / rel; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(current)
                print(f"[{APP} sync conflict] Could not create {rel}: {conflict['reason']}. "
                      "Saved the local version as a retry packet.", file=sys.stderr)
        if writes:
            result = account_request(self.account, "POST", "/api/scientist/v1/push", {"writes": writes})
            # A successful write already gives us the authoritative revision. Record it locally
            # so the next manifest comparison does not download the same (possibly large) figure
            # we just uploaded.
            for applied in result["applied"]:
                rel = applied["path"]
                if not self.writable(rel):
                    continue
                raw = self.local_raw(rel)
                tracked[rel] = {"revision": applied["revision"]}
                if name := self.save_base(rel, raw):
                    tracked[rel]["base_file"] = name
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
                if name := self.save_base(rel, raw):
                    tracked[rel]["base_file"] = name

        # Mirror server-side deletes when no local writable edit would be lost.
        for rel, old in list(tracked.items()):
            if rel in by_path:
                continue
            local = self.local_raw(rel)
            if not self.writable(rel) or self.rev(local) == old["revision"]:
                try: (self.root / rel).unlink()
                except FileNotFoundError: pass
                name = old.get("base_file", "")
                if name:
                    for directory in (self.base_dir, self.fallback_base_dir, self.legacy_base_dir):
                        try: (directory / name).unlink()
                        except OSError: pass
                tracked.pop(rel, None)
        self.save_state(state)


def choose_account() -> dict:
    accounts = load_config().get("accounts", [])
    if not accounts: raise RuntimeError("No account authorized. Run lockedin-scientist login --server https://lockedin.codes")
    return accounts[-1]


def print_bubbles(account: dict) -> None:
    """List approved bubbles without starting an agent or synchronizing the workspace."""
    rows = account_request(account, "GET", "/api/scientist/v1/bubbles").get("bubbles", [])
    if not rows:
        heading("Active bubbles")
        print(dim("No approved bubbles are available for this account yet."))
        return
    # Match the website: newest edited bubble first, with its stable ascending-slug tie order.
    rows.sort(key=lambda row: str(row.get("slug", "")))
    rows.sort(key=lambda row: str(row.get("last_edited_at", "")), reverse=True)
    heading("Active bubbles", f"{len(rows)} approved workspace{'s' if len(rows) != 1 else ''} for @{account['user']}")
    for index, row in enumerate(rows, start=1):
        print(f"\n  {cyan(f'{index:02d}')}  {bold(row['name'])}")
        print(f"      {dim('slug')}  {cyan(row['slug'])}")
    print("\n" + dim("Start a session with Codex, Claude, or Antigravity:") +
          f"\n  {cyan('lockedin-scientist <codex|claude|agy> <bubble-slug>')}")


def print_workspaces(account: dict) -> list[dict]:
    data = request(account["server"], "GET", "/api/scientist/v1/workspaces",
                   token=account["token"], workspace=account.get("workspace_id", ""))
    rows = data.get("workspaces", [])
    heading("Workspaces", f"for @{account['user']}")
    for row in rows:
        mark = " ✓" if row.get("id") == account.get("workspace_id") else ""
        print(f"  {cyan(row['id'])}  {bold(row['name'])}  {dim(row.get('role', 'editor'))}{mark}")
    return rows


def switch_workspace(account: dict, query: str) -> None:
    rows = print_workspaces(account)
    matches = [r for r in rows if r.get("id") == query or r.get("name", "").lower() == query.lower()]
    if len(matches) != 1:
        raise RuntimeError("Use a workspace id or an unambiguous exact workspace name.")
    account["workspace_id"] = matches[0]["id"]
    cfg = load_config()
    for item in cfg.get("accounts", []):
        if item.get("server") == account["server"] and item.get("user") == account["user"]:
            item["workspace_id"] = account["workspace_id"]
    save_config(cfg)
    print(green("✓") + " Active workspace: " + bold(matches[0]["name"]))


def external_dirs(values: list[str]) -> list[Path]:
    """Resolve explicit, per-session external workspace grants."""
    resolved: list[Path] = []
    seen: set[Path] = set()
    for value in values:
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise RuntimeError(f"External workspace directory does not exist or is not a directory: {value}")
        if path not in seen:
            resolved.append(path)
            seen.add(path)
    return resolved


def role(mirror: Mirror, bubble: str, add_dirs: list[Path] | None = None) -> str:
    inventory_path = mirror.root / "REPORTS" / bubble / "_lockedin_papers.md"
    inventory = inventory_path.read_text() if inventory_path.exists() else "(inventory unavailable; run lockedin-scientist sync)"
    external_scope = "\n".join(f"- {path}" for path in add_dirs or []) or "(none)"
    return f"""You are LockedIn Scientist. In the LockedIn mirror, work only in REPORTS/{bubble}/pages and REPORTS/{bubble}/assets.
This workspace is synchronized with the LockedIn website every five seconds. Re-read a page immediately before editing it.
If a sync conflict is reported, re-read the current page and reapply your intended change instead of restoring stale text.
Never edit credentials, TODOs, bubbles.yaml, or unrelated bubbles. Describe intended report edits before writing.

EXTERNAL WORKSPACE GRANTS:
- You may freely read and edit files under only these explicitly authorized external directories:
{external_scope}
- Those directories are local-only: never place their contents in the LockedIn mirror or assume they synchronize to the website.
- Do not access other external directories unless the user starts a new session with an appropriate --add-dir grant.

NEW REPORT PAGES:
- To add a website page, create one flat Markdown file at REPORTS/{bubble}/pages/<page-slug>.md.
- Use a lowercase hyphenated filename. Scientist registers it automatically; its website tab title is
  the filename with hyphens replaced by spaces. Never create or edit pages.yaml yourself.

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

FIGURES AND GIFS:
- REPORTS/{bubble}/assets is the synchronized place for report figures and animated GIFs. When
  adding one, create or copy a flat, descriptively named file there (for example,
  covariance-field.gif or reconstruction-error.png), then embed it in a report page with:
  ![Concise accessible caption](/api/bubbles/{bubble}/assets/filename.gif)
- This is the same asset URL and Markdown form produced by the website editor. Never put report
  figures in ASSETS/ (that directory is the paper library), and never use a local filesystem path
  in report Markdown. GIFs render from their first frame and loop in LockedIn previews.
- Before embedding, confirm that the asset file exists in REPORTS/{bubble}/assets. Let the normal
  five-second sync push it; do not call unrelated upload APIs or edit sync metadata.

AUTHORITATIVE ATTACHED-PAPER INVENTORY:
{inventory}"""


def require_bubble(mirror: Mirror, bubble: str) -> None:
    """Fail before spawning an agent when the requested bubble isn't an approved workspace."""
    rows = account_request(mirror.account, "GET", "/api/scientist/v1/bubbles").get("bubbles", [])
    if any(row.get("slug") == bubble for row in rows):
        return
    available = ", ".join(f"{row['slug']} ({row['name']})" for row in rows) or "(none)"
    raise RuntimeError(f"No approved bubble with slug {bubble!r}. Available: {available}")


def _sync_failure_message(failures: int, error: Exception) -> str | None:
    """Avoid interrupting an agent session for brief, self-healing sync outages."""
    if failures != _SYNC_WARNING_AFTER:
        return None
    return (f"[{APP} sync paused] {failures} consecutive sync attempts failed: {error}. "
            "Will keep retrying.")


def run_agent(model: str, mirror: Mirror, bubble: str, add_dirs: list[Path] | None = None,
              *, resume: bool = False) -> int:
    """Launch or resume a vendor session while supervising mirror synchronization."""
    require_bubble(mirror, bubble)
    if not shutil.which(model): raise RuntimeError(f"{model} is not installed on PATH.")
    add_dirs = add_dirs or []
    prompt = "Introduce yourself as the LockedIn research-report assistant and ask what to work on."
    instructions = role(mirror, bubble, add_dirs)
    add_dir_args = [arg for directory in add_dirs for arg in ("--add-dir", str(directory))]
    # Use each vendor CLI's ordinary interactive permission behavior.  Do not opt into any
    # bypass/auto-accept mode; Codex can still ask for escalation when it judges it necessary.
    if model == "codex":
        cmd = ["codex", *( ["resume", "--last"] if resume else []), "--cd", str(mirror.root),
               *add_dir_args, "--sandbox", "workspace-write", "--ask-for-approval", "on-request",
               "--search", "-c", f"developer_instructions={instructions}"]
        if not resume: cmd.append(prompt)
    elif model == "claude":
        cmd = ["claude", *add_dir_args]
        if resume:
            cmd.extend(["--continue", "--append-system-prompt", instructions])
        else:
            cmd.extend(["--append-system-prompt", instructions, prompt])
    else:
        cmd = ["agy", *add_dir_args, "--continue"] if resume else ["agy", *add_dir_args,
              "-i", instructions + "\n\n" + prompt]
    stop = threading.Event()
    def supervise():
        failures = 0
        warning_reported = False
        while not stop.wait(5):
            try:
                mirror.sync()
            except Exception as e:
                failures += 1
                if message := _sync_failure_message(failures, e):
                    print(message, file=sys.stderr)
                    warning_reported = True
            else:
                if warning_reported:
                    print(f"[{APP} sync recovered] Synchronization resumed after {failures} failed attempts.",
                          file=sys.stderr)
                failures = 0
                warning_reported = False
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
  lockedin-scientist workspaces
  lockedin-scientist switch <workspace-id-or-name>
  lockedin-scientist bubbles
  lockedin-scientist sync
  lockedin-scientist sync --from-server
  lockedin-scientist codex <bubble-slug>
  lockedin-scientist <codex|claude|agy> <bubble-slug> --add-dir <directory>
  lockedin-scientist claude <bubble-slug>
  lockedin-scientist agy <bubble-slug>
  lockedin-scientist resume codex <bubble-slug>
  lockedin-scientist uninstall
  lockedin-scientist uninstall --purge-data --yes

The short model form above is equivalent to `lockedin-scientist run <model> <bubble-slug>`.
`resume <model> <bubble-slug>` reopens that vendor's latest session in the selected workspace
mirror while Scientist keeps syncing. Vendor histories are workspace-wide, not bubble-specific.
`--add-dir DIR` is repeatable and grants the launched assistant local read/write access to an
existing directory for that session only; it is never synchronized or saved by LockedIn.
`sync` performs one safe pull/push cycle without launching a model. `sync --from-server` archives
local safe files, resets sync bookkeeping, and makes the website state authoritative (it asks for
confirmation; use --yes only for a deliberate non-interactive recovery). During a model session,
the workspace is synchronized every five seconds. `uninstall` removes only the command/client by
default; `uninstall --purge-data --yes` also removes the mirror and authorization. Use NO_COLOR=1
for plain terminal output.""",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", title="commands", metavar="COMMAND")
    p_login = sub.add_parser("login", help="Authorize this computer in a browser.",
                             description="Open a browser device-authorization flow for a LockedIn server.")
    p_login.add_argument("--server", required=True, metavar="URL", help="LockedIn server URL, for example https://lockedin.codes.")
    p_sync = sub.add_parser("sync", help="Pull/push once without launching a coding CLI.",
                            description="Synchronize the local mirror once, then print its location.")
    p_sync.add_argument("--from-server", action="store_true",
                        help="Archive local files and replace the mirror with the current website state.")
    p_sync.add_argument("--yes", action="store_true", help="Confirm --from-server in a non-interactive shell.")
    sub.add_parser("workspaces", help="List workspaces available to this account.")
    p_switch = sub.add_parser("switch", help="Switch the active workspace for this Scientist client.")
    p_switch.add_argument("workspace", help="Workspace id or exact workspace name.")
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
    p_run.add_argument("--add-dir", action="append", default=[], metavar="DIR",
                       help="Grant this existing directory local read/write access for this session only; repeatable.")
    p_resume = sub.add_parser("resume", help="Resume the latest vendor session with Scientist synchronization.",
                              description="Resume the latest session in the selected workspace mirror; vendor histories are not bubble-specific.")
    p_resume.add_argument("model", choices=("codex", "claude", "agy"), help="Installed coding CLI to resume.")
    p_resume.add_argument("bubble", metavar="BUBBLE-SLUG", help="Approved bubble used for Scientist context.")
    p_resume.add_argument("--add-dir", action="append", default=[], metavar="DIR",
                          help="Grant this existing directory local read/write access for this session only; repeatable.")
    args = parser.parse_args()
    if args.command is None:
        welcome()
        return
    if args.command == "login": login(args.server); return
    if args.command == "uninstall":
        uninstall(purge_data=args.purge_data, assume_yes=args.yes)
        return
    account = choose_account(); mirror = Mirror(account)
    if args.command == "workspaces": print_workspaces(account); return
    if args.command == "switch": switch_workspace(account, args.workspace); return
    if args.command == "bubbles": print_bubbles(account); return
    if args.command == "sync":
        if args.from_server:
            if not args.yes:
                if not sys.stdin.isatty():
                    raise RuntimeError("`sync --from-server` replaces the local mirror. Re-run with --yes to confirm.")
                answer = input("Replace the local mirror with the website state? A local cache backup will be made. [y/N] ").strip().lower()
                if answer not in ("y", "yes"):
                    print(dim("Recovery cancelled."))
                    return
            backup = mirror.recover_from_server()
            print(green("✓") + " Recovered workspace from the website\n  " + dim(mirror.root))
            print(dim("Local backup: ") + dim(backup))
        else:
            mirror.sync()
            print(green("✓") + " Synced workspace\n  " + dim(mirror.root))
        return
    if args.command in ("run", "resume"):
        add_dirs = external_dirs(args.add_dir)
        mirror.sync()
        action = "Resuming latest" if args.command == "resume" else "Starting"
        print(green("✓") + f" Synced. {action} {bold(args.model)} in {cyan(args.bubble)}…")
        if add_dirs:
            print(dim("External workspace grants:"))
            for directory in add_dirs:
                print("  " + cyan(directory))
        raise SystemExit(run_agent(args.model, mirror, args.bubble, add_dirs,
                                   resume=args.command == "resume"))
    parser.print_help()


def main() -> None:
    try:
        _main()
    except RuntimeError as exc:
        print(red("Error:") + " " + str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__": main()
