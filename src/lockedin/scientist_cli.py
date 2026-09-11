"""Project-local, bubble-scoped synchronization client for LockedIn.

The client keeps authorization and the active workspace in the OS-local profile.  Research
material lives only in ``.lockedin`` below the project from which ``sync`` is started.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.util
import difflib
import functools
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    from . import agent_vendors
except ImportError:  # Standalone client installed beside agent_vendors.py.
    import agent_vendors  # type: ignore[no-redef]

APP = "lockedin-scientist"
SCIENTIST_CLIENT_VERSION = "2026.09.11.7"
POLL_SECONDS = 5
# A worker that has not completed a cycle in three polls is wedged rather than merely busy.
# `doctor` reports that verdict and `resync` repairs exactly what `doctor` complains about, so
# both read the threshold from here.
WORKER_STALE_SECONDS = POLL_SECONDS * 3
BINDING_KEYS = ("server", "user", "workspace_id", "bubble")
WORKER_HISTORY_LIMIT = 10
TERMINAL_WORKER_STATUSES = {"stopped"}
ATTENTION_WORKER_STATUSES = {"degraded", "failed"}
VENDORS = agent_vendors.names()
MANAGED_VENDOR_SKILL_MARKER = "<!-- Managed by lockedin-scientist -->"
# One headless agent turn per assigned mark. The worker ends a turn that runs longer than this and
# reports it failed; the vendor's own print-mode timeout is set to match.
# Substrings (matched case-insensitively) seen in real vendor CLI output when a turn failed only
# because the agent's own interactive chat held the session open, not because the work was bad.
# Mirrors agents.BUSY_ERROR server-side; this client has no import of that module.
AGENT_BUSY_ERROR = "the agent's chat was open, so the turn was postponed"
AGENT_BUSY_SIGNATURES = agent_vendors.COMMON_BUSY_SIGNATURES
# Substrings (matched case-insensitively) seen in real vendor CLI output when the conversation id
# on file was deleted or otherwise not honoured, distinct from a turn that failed for its own
# reasons. Kept specific — no bare "does not exist" — so an ordinary error is not swallowed here.
AGENT_LOST_CONVERSATION_ERROR = (
    "the agent's saved conversation is unavailable; its identity and conversation id were preserved, "
    "and no replacement conversation was started"
)
AGENT_LOST_CONVERSATION_SIGNATURES = agent_vendors.COMMON_LOST_SIGNATURES
AGENT_COOLDOWN_SECONDS = 60
# Keep this below the server's 45-minute job lease. Twenty minutes proved too short for legitimate
# research/code turns; permission prompts cannot consume this allowance because headless launches
# close stdin and use each vendor's non-interactive approval flags below.
AGENT_TURN_SECONDS = int(os.environ.get("LOCKEDIN_AGENT_TURN_SECONDS") or 40 * 60)
# A vendor CLI that explicitly says it is reconnecting several times is not doing useful work.
# Give transient outages a minute and a half, then fail visibly instead of burning the whole turn.
AGENT_NETWORK_GRACE_SECONDS = int(os.environ.get("LOCKEDIN_AGENT_NETWORK_GRACE_SECONDS") or 90)
AGENT_NETWORK_RECONNECT_SIGNATURE = agent_vendors.COMMON_NETWORK_SIGNATURES[0]
# Different agents may work at once. One agent never runs two turns — the server refuses that too.
AGENT_MAX_PARALLEL = int(os.environ.get("LOCKEDIN_AGENT_MAX_PARALLEL") or 2)
AGENT_OUTPUT_TAIL = 4000
# A worker-local budget the server cannot override by offering more jobs: zero or negative means
# unlimited. Persisted turn start times (see AgentRunner._record_turn_start) survive a worker
# restart, so this is a real cap, not merely a per-process counter.
AGENT_MAX_TURNS_PER_HOUR = int(os.environ.get("LOCKEDIN_AGENT_MAX_TURNS_PER_HOUR") or 100)
AGENT_MAX_TURNS_PER_DAY = int(os.environ.get("LOCKEDIN_AGENT_MAX_TURNS_PER_DAY") or 500)
AGENT_BUDGET_HOUR_SECONDS = 3600
AGENT_BUDGET_DAY_SECONDS = 24 * 3600
SCRATCH_SYNC_NAME = re.compile(r"^(?:mark|thread)-[a-z0-9][a-z0-9._-]*--[a-z0-9][a-z0-9._-]*$")


class SecureModeStop(RuntimeError):
    """The server ordered this sync worker to stop until the user starts it locally again."""


def agent_turns_disabled() -> bool:
    """Whether ``LOCKEDIN_AGENT_TURNS`` currently asks ``AgentRunner`` to pause all dispatch.

    Read at call time (not into a module-level constant at import time) so a test — or an
    operator — can flip the environment variable and have the very next ``tick()`` honour it.
    """
    return os.environ.get("LOCKEDIN_AGENT_TURNS", "").strip().lower() in {"off", "0", "false"}

# This is deliberately a short bootstrap, not a copy of the report-editing guide.  The guide is
# bubble- and workspace-specific, so it belongs in the generated project-local skill that the
# bootstrap reads on every invocation.
VENDOR_SKILL_BOOTSTRAP = f"""---
name: lockedin-scientist
description: Work safely with a project-local LockedIn Scientist bubble. Read its generated .lockedin/SKILL.md before editing reports or its optional local Overleaf checkout.
---

{MANAGED_VENDOR_SKILL_MARKER}

# LockedIn Scientist

Read `<project-root>/.lockedin/SKILL.md` in full before making any change, where
`<project-root>` is the nearest directory, starting at the agent session's working directory and
walking up through its parents, that contains `.lockedin/config/binding.json`.

Run that search from the **active workspace directory shown by the current agent session**. Some
CLI agents start command tools in their own scratch folder; that scratch folder is not the
project. Do not search from there, reuse a previous project, or guess a project from the user's
home directory. Git is not required for this: the project does not have to be a repository.

If the session is inside a Git repository or linked **worktree**, first resolve its own boundary:

```
git rev-parse --path-format=absolute --show-toplevel
```

Search for the binding only from the active session directory up to and including that boundary.
Never cross into the main checkout through `--git-common-dir`, and never borrow `.lockedin` or a
conversation from a sibling worktree. If this worktree is not connected yet, use its bubble setup
link here; the link creates `.lockedin` in this worktree. Never use `find`, a glob, `grep`, or a
home-directory search to locate other `.lockedin` directories or guides.
It contains the current bubble's editing guide, paper context, math conventions, permitted write
paths, conflict recovery rules, and—when present—rules for the local Overleaf checkout. Follow it
as the source of truth.

If there is no `.lockedin/SKILL.md` at that root, do not create a replacement and do not look
elsewhere. Tell the user to run `lockedin-scientist sync <bubble-slug>` from the project root.
"""



def _colour(text: object, code: str) -> str:
    enabled = not os.environ.get("NO_COLOR") and (bool(os.environ.get("FORCE_COLOR")) or sys.stdout.isatty())
    return f"\033[{code}m{text}\033[0m" if enabled else str(text)


def bold(text: object) -> str: return _colour(text, "1")
def dim(text: object) -> str: return _colour(text, "2")
def cyan(text: object) -> str: return _colour(text, "36")
def violet(text: object) -> str: return _colour(text, "38;5;141")
def orange(text: object) -> str: return _colour(text, "38;5;214")
def green(text: object) -> str: return _colour(text, "32")
def red(text: object) -> str: return _colour(text, "31")


def heading(title: str, subtitle: str = "") -> None:
    print()
    print(violet("◆") + " " + bold(title))
    if subtitle:
        print("  " + dim(subtitle))


def welcome() -> None:
    """A human-first overview that matches the v2 project-local workflow."""
    def frame_line(text: str, style) -> None:
        # Center plain text first: ANSI escape codes added by `style` must not affect frame width.
        print(violet("│") + style(text.center(36)) + violet("│"))

    print()
    print(violet("╭────────────────────────────────────╮"))
    frame_line("LockedIn Scientist", bold)
    frame_line("research assistent", dim)
    print(violet("╰────────────────────────────────────╯"))
    print()
    print(bold("Fastest start"))
    print(f"  {cyan('•')} {dim('Open a bubble on the website, click the 🤖 icon, and paste the line it gives you.')}")
    print(f"    {dim('It installs, authorizes, binds a folder, and sets up your agent in one step.')}")
    print()
    print(bold("Or set it up by hand"))
    print(f"  {cyan('1.')} {dim('Authorize this computer')}\n     {cyan('lockedin-scientist login --server https://lockedin.codes')}")
    print(f"  {cyan('2.')} {dim('Choose a workspace')}\n     {cyan('lockedin-scientist workspaces')}\n     {cyan('lockedin-scientist workspaces switch <workspace-id-or-name>')}")
    print(f"  {cyan('3.')} {dim('See approved bubbles')}\n     {cyan('lockedin-scientist bubbles')}")
    print(f"  {cyan('4.')} {dim('Synchronize one bubble into this project')}\n     {cyan('lockedin-scientist sync <bubble-slug>')}")
    print()
    print(bold("Manage synchronization"))
    print(f"  {cyan('•')} {dim('List workers')}\n     {cyan('lockedin-scientist ps')}")
    print(f"  {cyan('•')} {dim('Stop a worker without removing local files')}\n     {cyan('lockedin-scientist stop <worker-id>')}")
    print(f"  {cyan('•')} {dim('Resume this project’s bubble after a worker stopped')}\n     {cyan('lockedin-scientist resync')}")
    print(f"  {cyan('•')} {dim('Replace this project’s .lockedin from the server')}\n     {cyan('lockedin-scientist hard-reset <bubble-slug>')}")
    print(f"  {cyan('•')} {dim('Verify this project’s worker and server connection')}\n     {cyan('lockedin-scientist doctor')}")
    print()
    print(bold("Large files"))
    print(f"  {cyan('\u2022')} {dim('Big binaries are listed but never synced automatically \u2014 move them on request')}\n     {cyan('lockedin-scientist assets')}")
    print(f"  {cyan('\u2022')} {dim('Bring one down, or send one up (both take --all)')}\n     {cyan('lockedin-scientist assets pull <filename>')}\n     {cyan('lockedin-scientist assets push <filename>')}")
    print(f"  {cyan('\u2022')} {dim('Delete one from the bubble \u2014 deleting it locally does not')}\n     {cyan('lockedin-scientist assets rm <filename>')}")
    print()
    print(bold("Agents: answer marks without opening the chat"))
    print(f"  {cyan('•')} {dim('From inside a codex/claude/agy chat in this project, give it a name and a role')}\n     {cyan('lockedin-scientist agent register --name <name> --role <role> --goal <goal>')}")
    print(f"  {cyan('•')} {dim('See the agents on this bubble, and what is queued for them')}\n     {cyan('lockedin-scientist agent list')}\n     {cyan('lockedin-scientist agent jobs')}")
    print(f"  {cyan('•')} {dim('Revive or reopen an agent; reset it; or retire it (--purge deletes the conversation)')}\n     {cyan('lockedin-scientist agent revive <name>')}\n     {cyan('lockedin-scientist agent chat <name>')}\n     {cyan('lockedin-scientist agent reset <name>')}\n     {cyan('lockedin-scientist agent retire <name>')}")
    print(f"  {cyan('•')} {dim('What a headless turn runs when it is done with a job')}\n     {cyan('lockedin-scientist agent reply <job-id> --text <answer>')}\n     {cyan('lockedin-scientist agent fail <job-id> --reason <why>')}")
    print()
    print(bold("Native agent skills"))
    print(f"  {cyan('•')} {dim('Install the LockedIn Scientist skill once for your agent')}\n     {cyan('lockedin-scientist <codex|claude|agy> setup')}")
    print()
    print(bold("Manual Overleaf publishing"))
    print(f"  {cyan('•')} {dim('Link an Overleaf project from the bubble page, then connect its local checkout')}\n     {cyan('lockedin-scientist overleaf connect')}")
    print(f"  {cyan('•')} {dim('See status or explicitly publish local LaTex changes')}\n     {cyan('lockedin-scientist overleaf status')}\n     {cyan('lockedin-scientist overleaf sync')}")
    print(f"  {cyan('•')} {dim('Read setup, credential-helper, and recovery guidance')}\n     {cyan('lockedin-scientist overleaf help')}")
    print()
    print(dim("Then launch your coding agent normally and invoke the lockedin-scientist skill."))


def data_root() -> Path:
    # A second client profile beside the default one (a dev server, a test): its own accounts,
    # workers, and logs, so nothing it does can touch the profile that talks to production.
    override = os.environ.get("LOCKEDIN_SCIENTIST_HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / APP
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / APP


def config_path() -> Path: return data_root() / "accounts.json"
def workers_path() -> Path: return data_root() / "runtime" / "workers.json"


def _atomic_json(path: Path, value: dict, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    if private:
        try: os.chmod(path, 0o600)
        except OSError: pass


def _figure_name(filename: str) -> str:
    """The name the website would store this figure under.

    A stdlib echo of ``bubbles.save_bubble_image``: lowercase the extension, slugify the stem. The
    server uses python-slugify, which this dependency-free client cannot import, so unicode
    transliteration may differ — this is only ever used to *warn*, never to rename a file.
    """
    stem = re.sub(r"[^a-z0-9]+", "-", Path(filename).stem.lower()).strip("-")
    return (stem or "image") + (Path(filename).suffix or ".png").lower()


def _remove_tree(path: Path) -> None:
    """Remove a managed tree even after pull-only files made it read-only."""
    if not path.exists():
        return
    for item in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try: os.chmod(item, 0o755 if item.is_dir() else 0o644)
        except OSError: pass
    try: os.chmod(path, 0o755)
    except OSError: pass
    shutil.rmtree(path)


def load_config() -> dict:
    path = config_path()
    if not path.exists(): return {"accounts": []}
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return {"accounts": []}


def save_config(cfg: dict) -> None: _atomic_json(config_path(), cfg, private=True)


def load_workers() -> dict:
    path = workers_path()
    if not path.exists(): return {"workers": {}}
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return {"workers": {}}


def _prune_worker_history(data: dict) -> None:
    """Retain every live worker and a bounded, useful terminal history."""
    workers = data.setdefault("workers", {})
    terminal = [(worker_id, rec) for worker_id, rec in workers.items()
                if rec.get("status") in TERMINAL_WORKER_STATUSES]
    terminal.sort(key=lambda item: item[1].get("stopped_at", item[1].get("started_at", 0)), reverse=True)
    keep = {worker_id for worker_id, _ in terminal[:WORKER_HISTORY_LIMIT]}
    for worker_id, _ in terminal[WORKER_HISTORY_LIMIT:]:
        workers.pop(worker_id, None)


def save_workers(data: dict) -> None:
    _prune_worker_history(data)
    _atomic_json(workers_path(), data, private=True)


def header_value(value: object) -> str:
    """A header must be one bounded line, and presence headers carry raw error text."""
    return " ".join(str(value or "").split())[:300]


def request(server: str, method: str, path: str, body: dict | None = None, token: str = "", workspace: str = "", *, timeout: float = 90, extra: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": f"{APP}/{SCIENTIST_CLIENT_VERSION}",
               "X-LockedIn-Scientist-Version": SCIENTIST_CLIENT_VERSION}
    if token: headers["Authorization"] = "Bearer " + token
    if workspace: headers["X-LockedIn-Workspace"] = workspace
    for name, value in (extra or {}).items():
        cleaned = header_value(value)
        if cleaned: headers[name] = cleaned
    req = urllib.request.Request(server.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        if exc.code == 426:
            raise RuntimeError("LockedIn Scientist is out of date. Reinstall it, then retry.") from exc
        raise RuntimeError(f"server returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach LockedIn server: {exc.reason}") from exc


def account_request(account: dict, method: str, path: str, body: dict | None = None, *, workspace: str = "", timeout: float = 90, extra: dict | None = None) -> dict:
    args = (account["server"], method, path, body, account["token"], workspace or account.get("workspace_id", ""))
    kwargs = {"extra": extra} if extra else {}
    if timeout != 90: kwargs["timeout"] = timeout
    return request(*args, **kwargs)


def download_request(account: dict, path: str, rel: str, dest: Path, *, timeout: float = 900,
                     on_progress=None, scratch: Path | None = None) -> int:
    """Stream a response body straight to disk. Never buffers the whole file in memory."""
    headers = {"Accept": "application/octet-stream",
               "User-Agent": f"{APP}/{SCIENTIST_CLIENT_VERSION}",
               "X-LockedIn-Scientist-Version": SCIENTIST_CLIENT_VERSION,
               "Content-Type": "application/json",
               "Authorization": "Bearer " + account["token"]}
    workspace = account.get("workspace_id", "")
    if workspace: headers["X-LockedIn-Workspace"] = workspace
    body = json.dumps({"paths": [rel]}).encode()
    req = urllib.request.Request(account["server"].rstrip("/") + path, data=body,
                                 headers=headers, method="POST")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Never beside the destination: reports/assets is what the sync pushes from, so an abandoned
    # part-file there becomes a real asset on the next push. One did.
    scratch = scratch or dest.parent
    scratch.mkdir(parents=True, exist_ok=True)
    tmp = scratch / (dest.name + ".part")
    written = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or 0)
            with tmp.open("wb") as fh:
                while True:
                    block = response.read(1024 * 1024)
                    if not block: break
                    fh.write(block); written += len(block)
                    if on_progress: on_progress(written, total)
        tmp.replace(dest)          # only a complete download replaces what was there
        return written
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"server returned {exc.code}: {exc.read().decode(errors='replace')}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach LockedIn server: {exc.reason}") from exc
    finally:
        # A completed download has already renamed tmp away, so this only ever fires on a failure
        # — including the interrupt and the kill, which the except clauses never see.
        tmp.unlink(missing_ok=True)


def upload_request(account: dict, path: str, payload: bytes, *, timeout: float = 900) -> dict:
    """POST raw bytes (one slice of a large asset). JSON in, JSON out is not enough here."""
    headers = {"Accept": "application/json", "Content-Type": "application/octet-stream",
               "User-Agent": f"{APP}/{SCIENTIST_CLIENT_VERSION}",
               "X-LockedIn-Scientist-Version": SCIENTIST_CLIENT_VERSION,
               "Authorization": "Bearer " + account["token"]}
    workspace = account.get("workspace_id", "")
    if workspace: headers["X-LockedIn-Workspace"] = workspace
    req = urllib.request.Request(account["server"].rstrip("/") + path, data=payload,
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"server returned {exc.code}: {exc.read().decode(errors='replace')}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach LockedIn server: {exc.reason}") from exc


def choose_account() -> dict:
    accounts = load_config().get("accounts", [])
    if not accounts:
        raise RuntimeError("No account authorized. Run `lockedin-scientist login --server <URL>` first.")
    return accounts[-1]


def installer_command() -> str:
    """The native reinstall command for this client's operating system."""
    if os.name == "nt":
        return "irm https://raw.githubusercontent.com/HamidrezaKmK/lockedin/main/install.ps1 | iex"
    return "curl -fsSL https://raw.githubusercontent.com/HamidrezaKmK/lockedin/main/install.sh | bash"


def warn_if_outdated(account: dict | None = None) -> None:
    """Show an upgrade warning for local-only commands before a worker discovers it later."""
    if account is None:
        accounts = load_config().get("accounts", [])
        if not accounts:
            return
        account = accounts[-1]
    try:
        account_request(account, "GET", "/api/scientist/v2/bubbles", timeout=3)
    except RuntimeError as exc:
        if "out of date" not in str(exc).lower():
            return
        print(orange("! LockedIn Scientist is out of date."), file=sys.stderr)
        print(f"  Reinstall: {installer_command()}", file=sys.stderr)


def login(server: str) -> None:
    server = server.rstrip("/")
    start = request(server, "POST", "/api/scientist/v2/device", {"client_name": APP})
    url = server + start["verification_uri"]
    heading("Authorize this computer", "Open the link below, sign in, then return here.")
    print("\n  " + cyan(url) + "\n")
    webbrowser.open(url)
    until = time.time() + int(start["expires_in"])
    while time.time() < until:
        time.sleep(int(start["interval"]))
        result = request(server, "GET", f"/api/scientist/v2/device/{start['device_code']}/token")
        if result.get("status") != "authorized": continue
        workspaces = request(server, "GET", "/api/scientist/v2/workspaces", token=result["token"])
        cfg = load_config(); accounts = cfg.setdefault("accounts", [])
        accounts[:] = [a for a in accounts if not (a.get("server") == server and a.get("user") == result["user"])]
        accounts.append({"server": server, "user": result["user"], "token": result["token"],
                         "workspace_id": workspaces.get("personal_workspace_id", "")})
        save_config(cfg)
        print(green("✓") + f" Authorized {bold(result['user'])} on {dim(server)}")
        print(dim("  Your active workspace is saved for every project on this device."))
        return
    raise RuntimeError("Device authorization timed out.")


def _print_workspaces(rows: list[dict], active_workspace_id: str) -> None:
    heading("Your workspaces", "The selected workspace is used across all projects.")
    if not rows:
        print(dim("  No workspaces are available."))
        return
    for index, row in enumerate(rows, 1):
        active = "  " + green("✓ active") if row.get("id") == active_workspace_id else ""
        print(f"  {cyan(str(index) + '.'):<11}{bold(row['name'])}{active}")
        print(f"             {dim(row['id'] + ' · ' + row.get('role', 'editor'))}")
    print()
    print(dim("  Switch with: lockedin-scientist workspaces switch <workspace-id-or-name>"))


def workspaces_command(account: dict) -> list[dict]:
    rows = account_request(account, "GET", "/api/scientist/v2/workspaces").get("workspaces", [])
    _print_workspaces(rows, account.get("workspace_id", ""))
    return rows


def switch_workspace(account: dict, query: str) -> None:
    rows = account_request(account, "GET", "/api/scientist/v2/workspaces").get("workspaces", [])
    hits = [r for r in rows if r.get("id") == query or r.get("name", "").lower() == query.lower()]
    if len(hits) != 1: raise RuntimeError("Use a workspace id or an unambiguous exact workspace name.")
    cfg = load_config()
    for item in cfg.get("accounts", []):
        if item.get("server") == account["server"] and item.get("user") == account["user"]:
            item["workspace_id"] = hits[0]["id"]
    save_config(cfg)
    account["workspace_id"] = hits[0]["id"]
    _print_workspaces(rows, account["workspace_id"])
    print()
    print(green("✓") + f" Active workspace: {bold(hits[0]['name'])}")
    print(dim("  New project synchronizations will use this workspace."))


def bubbles_command(account: dict) -> list[dict]:
    rows = account_request(account, "GET", "/api/scientist/v2/bubbles").get("bubbles", [])
    print()
    print(violet("◆") + " " + bold("LockedIn Scientist"))
    print("  " + dim("approved bubbles in the active workspace"))
    print()
    if not rows:
        print(dim("  No approved bubbles yet."))
        return rows
    for index, row in enumerate(rows, 1):
        print(f"  {cyan(str(index) + '.'):<11}{bold(row['name'])}")
        print(f"             {dim(row['slug'])}")
    print()
    print(dim("  Sync one with: lockedin-scientist sync <bubble-slug>"))
    return rows


# Bump when the guide text changes: a project only regenerates SKILL.md when this marker in its
# copy stops matching, so an edit to the guide reaches no existing agent until this moves.
SKILL_VERSION = 52

# The marker is derived, never typed. It is what the staleness check compares against, so a
# hand-written copy that drifted from SKILL_VERSION would either pin every project to a stale
# guide or rewrite the skill on every five-second sync.
SKILL_ROUTER = f"<!-- lockedin-scientist-skill: {SKILL_VERSION} -->\n" + """\
# LockedIn Scientist

This project is synchronized with one LockedIn bubble. These rules always apply. The detail
lives in `.lockedin/guides/`.

**Read a guide when you are about to do the thing it covers — not in case you might.** One you
turn out not to need costs the user real money and crowds out what you are reasoning about.

| guide | read it | size |
|---|---|---|
| `guides/feedback.md` | before acting on a feedback index hit, or writing a chalk talk | small |
| `guides/paths.md` | before looking for anything under `.lockedin/` | small |
| `guides/reports.md` | before creating, deleting or submitting a report page | small |
| `guides/macros.md` | before using a `\\\\`-macro in maths | tiny |
| `guides/overleaf.md` | before touching a local Overleaf checkout | small |
| `guides/agents.md` | when asked to register as an agent, or when a prompt names a LockedIn job id | small |
| `guides/editing.md` | **reference, not prerequisite** | large |

`guides/editing.md` is a syntax reference for figures, citations, theorem environments and the
rest. Do not read it end to end: search it for the one construct you are unsure of, and skip it
entirely when editing prose or maths you already know how to write. It opens with its own
contents list.

## Where `.lockedin/` is

Every path in this guide is relative to the **project root**: the nearest directory, starting at
your working directory and walking up through its parents, that contains
`.lockedin/config/binding.json`. Resolve it once at the start of a session and use it as the
prefix. Git is not required for this: the project does not have to be a repository.

If you are working in a Git repository or linked **worktree**, its own
`git rev-parse --path-format=absolute --show-toplevel` is the project boundary. Search only from
the active session directory up to that boundary. Never cross through `--git-common-dir` into the
main checkout and never borrow `.lockedin` or a conversation from a sibling worktree. If this
worktree has no binding, run its bubble setup link from this worktree; that creates the local
`.lockedin` used by this agent and worker.

## Project work and LockedIn boundaries

Outside `.lockedin/`, work on this repository normally. The user's request and your usual agent
permissions determine whether you may create or edit project files such as source code, scripts,
tests, build artifacts, and `outputs/`. LockedIn does not restrict those paths.

The rules below apply **only inside `.lockedin/`**:

- Edit or create Markdown pages only under `.lockedin/reports/pages/`.
- Add or edit figures only under `.lockedin/reports/assets/`.
- `indexes/pages.json` is the generated page catalog: never edit it. To add a page, create a new
  lowercase, hyphenated `pages/<slug>.md` file; the sync worker registers it automatically.
- To delete a page, delete its `pages/<slug>.md` file; the sync worker removes it from the
  server manifest automatically. The overview/home page cannot be deleted.

## Read-only content inside `.lockedin`

`.lockedin/assets/` and `.lockedin/config/` are synchronized from LockedIn and must never be
edited, moved, deleted, or permission-changed. This does not restrict similarly named directories
elsewhere in the repository. Read paper information only from
`.lockedin/indexes/papers.json`, then open only the selected asset directory. Prefer higher
relevance papers first.

## Sync and conflicts

The sync worker publishes report changes periodically. If it restores a server copy, inspect
`.lockedin/config/conflicts/` and reapply the intended change to the current report instead of
restoring stale content. Use Markdown with `$...$` and `$$...$$` math delimiters only.

## What this bubble is for

`.lockedin/IDEA.md` states the premise: one paragraph on the idea and one line on the goal. Read
it at the start of a session — it is short, and everything else assumes it.

It is generated and read-only. If it is wrong, stale, or narrower than the work actually being
done, say so and propose better wording rather than working around it; the user applies it in
the app.

## Agents

The user can give this chat a name and a role, then assign marks or send it a direct message from
the bubble page without opening the chat: the sync worker runs one turn of *this conversation*
per assignment or message while the chat is closed. If the user asks you to register, become, or act as an agent — or a
prompt names a LockedIn job id like `j-000012` — read `guides/agents.md` and follow it.

## Indexed retrieval — do not scan first

`.lockedin/index.json` is the small router for synchronized context. Read individual JSON keys
with `jq`; do not `cat` an index or scan `.lockedin/` to discover content. Examples:

    jq '.counts' .lockedin/index.json
    jq --arg id 'n7' '.by_local_id[$id] // []' .lockedin/indexes/marks.json
    jq --arg id 'talk-ab12cd34ef56' '.by_id[$id]' .lockedin/indexes/chalk-talks.json

When a mark lookup returns one key, read that key from `.by_key`, then read the selected detail
with `jq --arg id '<mark-id>' '.by_id[$id]' <detail_path>` and open only the named source. Every
scoped feedback file uses this same `.by_id` shape. When a lookup returns several keys, use the talk/page named
by the user to choose; ask only if that context is genuinely ambiguous. When a talk is named,
query `indexes/chalk-talks.json` by id or title, then open only that talk folder.

If an index is missing, invalid, points to a missing id/path, or cannot disambiguate a referenced
mark, use `.lockedin/feedback/all.json` as the fallback. It is intentionally complete and
expensive; do not read it during a healthy indexed lookup.
"""


GUIDES = {
    'paths.md': """\
# Where things are

## Direct LockedIn paths — do not search for them

Use these exact paths when their information is needed, resolved against the project root above.
In a linked worktree they are inside that worktree's own `.lockedin`, never the main checkout's.
Do not spend time searching the project for an alternative copy:

- `.lockedin/config/math.yaml` — workspace math macros; the generated macro table below is the
  preferred ready-to-use form.
- `.lockedin/config/aesthetics.yaml` — report appearance configuration.
- `.lockedin/config/overleaf.yaml` — website-linked Overleaf metadata, only when present.
- `.lockedin/config/conflicts/` — rejected local report edits to recover manually, only when
  present.
- `.lockedin/indexes/papers.json` — keyed bubble paper index; query it before opening one paper.
- `.lockedin/assets/<pdf-id>/` — the selected paper's PDF, metadata, extracted text, and summary.

For any report-related search, search only inside `.lockedin/`: use
`.lockedin/reports/pages/` for report source, `.lockedin/reports/assets/` for report figures,
`.lockedin/indexes/papers.json` for attached-paper discovery, and `.lockedin/assets/` for
the one selected paper's material. Do not search the surrounding repository unless the user
explicitly asks to combine it with project code or files.

## Scratch

`.lockedin/scratch/` is shared by the agents attached to this project and is the first place to
look for reusable code or resources. Before creating a script, inspect the flat files whose
`mark-...--` prefix matches the current mark's **Scratch tag** from the job prompt. Read the
relevant candidates and prefer editing and rerunning one in place over creating a replacement.

Any scratch code, data, or resource used to answer a LockedIn job must be a flat file named
`<scratch-tag>--<descriptive-name>.<ext>`, using the exact tag supplied in the prompt. Examples:
`mark-talk-a1b2-n7--reward-landscape.py` and `mark-page-overview-c4--samples.csv`. Matching tagged
files are synchronized privately and can be downloaded from **Library → Agent scratch**. Untagged
files, subdirectories, caches, and virtual environments stay local and are never synchronized.""",

    'reports.md': """\
# Writing reports

## Reports: the live research record

`.lockedin/reports/` is the working research record: use it for fast-moving explanations,
experiments, figures, intermediate conclusions, and material that should be shared through the
LockedIn bubble. Its changes are synchronized continuously. It is normal for a report page to be
exploratory or to evolve quickly, but keep claims and citations accurate.

For a report figure stored in `.lockedin/reports/assets/`, use a portable relative Markdown image
link: `![descriptive caption](assets/filename.png)`. Do not paste a browser URL, a local absolute
path, or an `/api/...` URL. Relative `assets/` links render in LockedIn, its standalone previews,
and public shares, while staying valid when the bubble or workspace changes.

Save every figure as a file directly inside `.lockedin/reports/assets/`. A figure placed in a
subdirectory of that folder is **not** synchronized and never reaches the website, because LockedIn
serves figures from a single-segment URL. Name figures in lowercase with hyphens and no spaces
(`drift-field-two-moons.png`, not `Drift Field_TwoMoons.PNG`); that is exactly the name the website
assigns to an uploaded image, so a matching name avoids creating a duplicate figure.

## Before relying on a report submission

Before telling the user that a report edit is synchronized—or before making a sequence of edits
that relies on background synchronization—run `lockedin-scientist doctor` from the project root.
It verifies that this `.lockedin` directory has a matching, healthy worker and can reach its bound
LockedIn bubble. If it fails, do not claim the work was submitted; show the user the failure and
ask whether they want to repair it, in this order. Run these from the connected checkout or
worktree's own project root:

1. `lockedin-scientist ps` — every worker on this machine and the folder each one syncs.
2. `lockedin-scientist resync` — the usual repair. It resumes whatever bubble this project is
   already bound to, needs no arguments, and leaves `.lockedin` intact.
3. If the command itself is missing (a fresh cloud sandbox, for instance), ask the user for a
   setup link: the bubble page's robot button produces one line that installs the client, signs this
   machine in, and connects the folder you are working in. Pasting it here works — with no
   terminal to answer from it uses the current directory instead of prompting.
4. `lockedin-scientist hard-reset <bubble>` only when the directory itself is broken; it replaces
   `.lockedin` wholesale and discards local work that never synchronized. Ask first.""",

    'feedback.md': """\
# The user's marks, and chalk talks

## The five marks

`.lockedin/indexes/marks.json` routes every open mark by exact key. Read only the selected entry
from its `detail_path`; it names the kind, exact text or region, and conversation. The kind is
the instruction:

| mark | means | do |
|---|---|---|
| ✗ | this is wrong | re-derive; do not reword |
| ? | I don't follow | re-explain; do not re-derive |
| → | go deeper | expand, usually into a report page |
| ✓ | good, keep this | lean on it |
| ✂ | cut this | remove it |
| ✍ | look at what I drew | open the picture; the strokes are the feedback |

A mark with no sentence is complete: the kind said it.

A `✍` mark is a freehand drawing over the slide, and its `picture:` line is the whole message —
open it before anything else. If your tooling cannot open images, the mark's **the ink touches**
line names the words the strokes actually sit on — work from that plus the comment, and say that
you answered from the fallback rather than the picture. Read the strokes the way you would a reviewer's pen: crossed-out
text wants rewriting, an arrow wants something moved or reordered, a circle wants attention or
expansion, handwriting wants reading and doing. Address it like any other mark.

Work through them **with the user, not for them** — propose, agree, then change.
Make the smallest change that answers the mark: do not reorganise unrelated material,
broaden claims, or rewrite beyond what was asked.

If you think a mark is mistaken, say so and argue it; do not comply silently. An entry with a `picture:` line
points at a PNG of the slide with the mark drawn on: open it, since it carries layout a quote
cannot, and for a region mark it is the whole message.

Never edit `indexes/`, `feedback/`, or a deck's `marks.json` — they are generated and overwritten.

## Marks on report pages

A page mark is anchored by a `<comment-begin=id>…<comment-end=id>` tag pair in the page
source; the selected page-feedback JSON gives that id. Match it to its tags, then make the smallest useful edit
between them and leave both tags in place — removing them unanchors the mark.

The tags are managed by LockedIn: never create, copy, fabricate, rename, or move one, and never
add a pair for an unanchored mark. Never guess where an unanchored review belongs — ask.
The selected page-feedback JSON is read-only; never reply to, edit, delete, or resolve a page
mark there. The user resolves those in the app.

Colored passages use `\\textcolor{<color>}{text}`; keep each wrapper balanced and never nest them.
Markup that fails to parse inside a code span or fence is literal text, not an error.

## Chalk talks

A chalk talk is a stable folder at `reports/talks/<talk-id>/`; its editable deck is `slides.md`
and its generated open feedback is `marks.json`. Folder ids are opaque and never change when a
title changes. The deck explains one idea whose correctness
needs the user's judgement. Slides are separated by `---`; each has a
`<!-- slide: kind=…, date=… -->` header, a `# Title`, an optional one-line *italic
subtitle*, then Markdown with `$…$` maths and `\\cite{key}` citations. `kind` is one of
`setup, derivation, evidence, comparison, implementation, ask` — nothing else renders.

The report editing vocabulary also renders in a deck: tables, figures, workspace macros, display
math environments, coloured text, citations, and `theorem`, `lemma`, `corollary`, `proposition`,
`definition`, `assumption`, `remark`, and `proof` boxes. A theorem-style box may carry an optional
title and a `\\label{thm:key}`; `\\thmref{thm:key}` resolves anywhere in the same talk, including
another slide or a math block. Those counters and labels are **talk-local**: never rely on a
report-page theorem label from a deck, or on a deck label from a report. Citations remain
bubble-wide.

**Reading them without drowning.** Start with `IDEA.md`, then query the JSON indexes. Open a
deck only when an index hit points into it, the user names it, or you are about to write on the
same idea. When a mark names a slide, read that slide and its neighbours, not every talk.

**Writing one.** Create `reports/talks/talk-<stable-id>/slides.md`, where the id is lowercase,
opaque, and unrelated to the title (a timestamp or random hex is fine). The bubble indexes it on
arrival, taking the title from the first `#` and summary from its subtitle. Write one when asked,
or offer one when you reach something needing judgement. Never for status — status is the
document.

- A figure beats a paragraph. Save it flat in `reports/assets/` and reference it the same way a
  report page does: `![caption](assets/name.png)`. State the axis ranges in the caption — the
  user can mark a region of a figure, and a rectangle over unlabelled axes says little.
- One idea per slide, fitting a screen. If it does not fit, it is two slides.
- Condensed, not prose. Default to **five or six words per sentence or bullet**. Treat a longer
  line as a failure unless technical precision would be lost; then use the shortest wording
  that is still correct. Titles may run longer when the claim genuinely needs it.
- At most five bullets a slide. A bullet that wraps past two lines is a paragraph pretending —
  split it or cut it.
- Minimise equations; carry the idea in words. When the derivation *is* the point, number the
  steps so the user can mark the one that is wrong.
- Say what you are unsure of — the subtitle is the place.
- Open with why it matters, close with what you need. Titles carry the claim, not the
  topic — and never `Slide N:`, the deck numbers itself.

**Answering a mark.** Edit the slide in place. To reply in its thread, append this block anywhere
outside a code fence in the same deck, using the mark's id from its `marks.json`:

    <!-- lockedin-reply: n7 -->
    I replaced the approximation with the exact covariance term on slide 2.
    <!-- /lockedin-reply -->

On sync, LockedIn adds the text to mark `n7`'s thread and removes the block from the deck. The
same exact reply is safe to retry.

**Only when you are not working a job.** If your prompt named a LockedIn job id, answer with
`agent reply <job-id>` instead and do not add a reply block: each posts a turn, so doing both
posts your answer twice. See `guides/agents.md`. That is the whole of your power over a mark: **you cannot
resolve, remove, or delete one — anywhere** — and a `resolves=` attribute in a slide header is
ignored. The user resolves a mark in the app once your answer satisfies them; its thread remains archived. If your edit removes
the text a mark points at, the mark goes orphan and stays visible; that is normal, not a problem
to fix.
""",

    'agents.md': """\
# Agents: this chat, with a name, answering turns on its own

## What an agent is

An *agent* is this very conversation, registered on the bubble under a name, a role, a goal and
optionally a personality. Once registered, the user can **assign a mark or send you a direct
message from the bubble page** instead of coming here to ask. While this chat is closed, the sync
worker runs one headless turn of this same conversation per assignment: you keep your memory,
your name, and everything already discussed. Nothing runs while no turn is queued.

## Where a turn may write

A headless turn may read anything on the machine, but it may write only under `.lockedin/`:
throwaway code, environments, and outputs go in `.lockedin/scratch/`; anything meant to reach the
bubble goes in `.lockedin/reports/`. This is enforced by the worker itself — not by trusting the
vendor's own flags — so treat it as a real boundary, not a convention. The intended pattern for
stress-testing or trying out project code without risking it: write a script under
`.lockedin/scratch/` that imports from the project (e.g. adds the project root to `sys.path` and
imports its modules) and calls into it, writing any result under `.lockedin/scratch/`. A write
aimed at the project itself failing is the guarantee working as intended, not an error to route
around.

## Reuse scratch work before creating it again

Every mark or direct-message prompt supplies a stable **Scratch tag**. Before making code or a
resource, list `.lockedin/scratch/<scratch-tag>--*`, inspect relevant matches, and prioritize
editing and rerunning them in place. This is how a second agent continues a figure generator or
experiment created by the first agent on the same mark. Do not create a parallel script merely
because another agent authored the existing one. If no relevant match exists, create a flat file
named `<scratch-tag>--<descriptive-name>.<ext>`. Every scratch file actually used in the response
must carry that exact prefix; untagged scratch remains private to this machine and is not shown in
the frontend.

## Registering (once per conversation)

Only when the user asks you to register, become, or act as an agent. Then:

1. Ask the user, in one short message, for a **name**, a **role** (a few words), a **goal** (one
   sentence), and optionally a **personality**. Suggest defaults if they want you to. If the
   registration request already supplies those fields — including a profile chosen from the web
   app's presets — use them as written and do not ask for them again.
2. Run, from the project root:

       lockedin-scientist agent register --name "Ada" --role "skeptical reviewer" \\
           --goal "keep every derivation honest and readable" --personality "terse, likes counterexamples"

   The command works out which CLI conversation it is running inside; you never copy an id. Add
   `--model <id>` if the user names the model you run as; the worker passes it to headless turns.
   If it cannot tell which conversation this is, it says which flags to pass — ask the user.
3. Tell the user you now appear on the bubble page under this directory's sync, and that marks
   and direct messages they queue there will be answered while this chat is closed. Do not poll
   or wait for jobs.

`lockedin-scientist agent list` shows the agents on this bubble; `lockedin-scientist agent jobs`
shows open jobs from the local index (`--all` includes finished ones) — use it only when the user
asks what is queued. An operator can pause all dispatch without stopping synchronization by
setting `LOCKEDIN_AGENT_TURNS=off` on the worker.

## When a prompt is a job

A headless turn starts with `LockedIn job j-000012.` It either names one mark or says **Direct
message from …** and includes the user's free-form request. A direct message is not attached to a
mark: do the requested work within the same write boundary, then post the answer with `agent
reply`. For a mark, the prompt names its kind, where it sits, the quote or drawing, the user's
words, and the exact `jq` command and file to edit. Everything in
`guides/feedback.md` applies — the kind is the instruction, make the smallest change that answers
it, keep `<comment-begin>`/`<comment-end>` tags in place, never edit `indexes/`, `feedback/`, or a
deck's `marks.json`. Do the work directly; there is nobody to ask.

Then end the turn with **exactly one** of:

    lockedin-scientist agent reply j-000012 --text "What you changed and why, in a few sentences."
    lockedin-scientist agent fail  j-000012 --reason "Why this cannot or should not be done."

`reply` posts your text into the mark's thread (page marks and slide marks alike) or the direct
message screen, then closes the job; `fail` records the reason and closes it as failed. That
command *is* your reply, so do not also add a `<!-- lockedin-reply -->` block to the deck: both
post a turn, and the user would read your answer twice. A long reply can come from a file:
`--file notes.md`. A turn that ends without one of these is recorded as failed even if you edited
the right thing — the user sees nothing otherwise. Never resolve or delete a mark; only the user
does that in the app.

## The conversation itself

- `lockedin-scientist agent revive <name>` resumes a dead worker from that agent's project folder
  without resetting the registered personality or conversation. If **Stop agents** revoked the
  machine, first turn it off in the web app and use the one-use recovery command shown at the top
  of the offline agent's direct-message screen; that command upgrades and reauthorizes Scientist.
- `lockedin-scientist agent chat <name>` reopens an agent's conversation interactively. While
  it is open the worker will not drive it — assigned jobs wait and the page shows the agent as
  *attached* — and they run once the chat is closed.
- `lockedin-scientist agent reset <name>` forgets the conversation but keeps the persona: the
  next job (or `agent chat`) starts a new one and re-introduces you.
- `lockedin-scientist agent retire <name>` removes the agent from the bubble and cancels its
  open jobs; `--purge` also deletes the conversation from the vendor's store.""",

    'overleaf.md': """\
# Overleaf

## Optional Overleaf checkout: the publication manuscript

If `.lockedin/overleaf/` exists, it is a local LaTeX checkout for this bubble's website-linked
Overleaf project. It is the curated publication source, not a continuously published mirror of
the reports. You may work there normally: create or edit `.tex`, `.bib`, `.sty`, `.cls`, figure,
and other ordinary project files, and use the repository's usual LaTeX tooling.

- Before manuscript-level edits, inspect the document entry point, included files, bibliography,
  and existing project conventions. Preserve the manuscript's structure and compile it when the
  project's tooling is available.
- Transfer ideas from reports deliberately: adapt, verify, and integrate them into the manuscript
  rather than blindly copying an exploratory page. Keep references, labels, cross-references,
  notation, and claims publication-ready.
- The active workspace math macro table below applies to both reports and manuscript work unless
  the LaTeX project already defines an intentional equivalent.
- Do not edit `.lockedin/overleaf/.git/`, change its configured remote, or run
  `lockedin-scientist overleaf sync` unless the user explicitly asks to publish to Overleaf.
  The Scientist worker does not synchronize this checkout automatically: it never pulls, pushes,
  changes, or deletes it; manuscript changes stay local until that explicit sync.""",

}
def skill_document(editing_guide: str = "", math_macros: dict | None = None) -> str:
    """The file an agent loads every session: rules that always apply, and where the rest is.

    The detail used to live here too, which meant ~6.5k tokens of Overleaf procedure and LaTeX
    syntax loaded before a session that only wanted to answer one comment. It is now in
    ``.lockedin/guides/``, indexed above and read on demand.
    """
    return SKILL_ROUTER.rstrip() + "\n"


def macros_guide(math_macros: dict | None = None) -> str:
    macros = math_macros if isinstance(math_macros, dict) else {}
    lines = ["# Workspace math macros", "",
             "Read this before using a `\\`-macro in maths: only the commands below exist in this",
             "workspace, and an undefined one breaks the whole equation at render time.", ""]
    if not macros:
        lines.append("No custom workspace math macros are currently configured.")
    else:
        lines.extend(["| Command | Expansion |", "|---|---|"])
        for command, expansion in sorted(macros.items(), key=lambda item: str(item[0])):
            safe_command = str(command).replace("|", "\\|")
            safe_expansion = str(expansion).replace("|", "\\|").replace("\n", "<br>")
            lines.append(f"| `{safe_command}` | `{safe_expansion}` |")
    return "\n".join(lines) + "\n"


def write_skill_bundle(root: Path, editing_guide: str, math_macros: dict | None = None) -> None:
    """Write SKILL.md plus the guides it points at, and drop guides that no longer exist."""
    (root / "SKILL.md").write_text(skill_document(), encoding="utf-8")
    guides = root / "guides"
    guides.mkdir(parents=True, exist_ok=True)
    written = dict(GUIDES)
    # The agents guide tells the model which command to run; on a machine that invokes this
    # client through a differently named shim (a dev profile), that name must be the real one.
    cli = cli_name()
    if cli != APP:
        written["agents.md"] = written["agents.md"].replace(APP + " agent", cli + " agent")
    written["macros.md"] = macros_guide(math_macros)
    headings = [line.lstrip("# ").strip() for line in editing_guide.splitlines()
                if line.startswith("## ")]
    contents = "\n".join(f"- {h}" for h in headings)
    written["editing.md"] = ("# Editing reference\n\n"
                             "**Search this file for the construct you need; do not read it end to\n"
                             "end.** It is the largest guide here and most sessions need one\n"
                             "section of it, or none.\n\n"
                             "## What is in here\n\n" + contents + "\n\n"
                             + editing_guide.rstrip() + "\n")
    for name, body in written.items():
        (guides / name).write_text(body, encoding="utf-8")
    for stale in guides.glob("*.md"):
        if stale.name not in written:
            stale.unlink()


# Kept as a small inspectable baseline for code/tests; projects receive the complete guide below.
# The whole bundle as one string, for tests and inspection. An agent never loads this — it
# reads SKILL.md and only the guides it needs.
SKILL_RULES = SKILL_ROUTER + "\n\n" + "\n\n".join(GUIDES[k] for k in sorted(GUIDES))
SKILL = SKILL_ROUTER

OVERLEAF_HELP = """Overleaf uses the project linked to this bubble in LockedIn's website. Open the
bubble, click Overleaf, and add its Cloud project URL, Git URL, or project ID first.

When Git asks, enter username `git` and your Overleaf authentication token. If no OS credential
helper is configured, Scientist enables one private credential store for this user outside every
repository; after that first prompt, all of the user's Overleaf projects reuse the token. The
file is owner-only but stores the token as Git credential data, so prefer an OS keychain helper
when one is available. Its Git configuration is global but applies only to `git.overleaf.com`,
not other Git hosts. Changes remain local until you run `lockedin-scientist overleaf sync`.

If synchronization fails, work manually in `.lockedin/overleaf/`: inspect `git status`, fetch
from `lockedin-overleaf`, merge/rebase its default branch and resolve conflicts, then push to
that branch. `lockedin-scientist overleaf abort` aborts a rebase started by Scientist.
"""

LEGACY_OVERLEAF_README_PREFIX = """# LockedIn Overleaf integration

Overleaf synchronization is reserved for a later release."""


def _write_managed_vendor_file(path: Path, content: str) -> None:
    """Update only a file that this command created previously."""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"Could not inspect existing skill at {path}: {exc}") from exc
        if MANAGED_VENDOR_SKILL_MARKER not in existing:
            raise RuntimeError(
                f"Refusing to overwrite the existing skill at {path}. "
                "Move or remove that user-owned skill, then run setup again."
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _vendor_skill_paths(vendor: str, home: Path) -> tuple[Path, ...]:
    """Return the globally discovered native skill location for a supported agent."""
    try:
        return agent_vendors.get(vendor).skill_paths(home, APP)
    except RuntimeError:
        raise RuntimeError(f"Unknown agent {vendor!r}. Choose one of: {', '.join(VENDORS)}.") from None


def setup_vendor_skill(vendor: str, *, home: Path | None = None) -> tuple[Path, ...]:
    """Install the named bootstrap in the vendor's native global skill discovery path."""
    vendor = vendor.lower()
    home = Path.home() if home is None else Path(home)
    try: adapter = agent_vendors.get(vendor)
    except RuntimeError:
        raise RuntimeError(f"Unknown agent {vendor!r}. Choose one of: {', '.join(VENDORS)}.") from None
    return adapter.install_skill(home, APP, VENDOR_SKILL_BOOTSTRAP,
                                 writer=_write_managed_vendor_file,
                                 binary=_vendor_binary, run=subprocess.run)


def setup_vendor_command(vendor: str) -> None:
    targets = setup_vendor_skill(vendor)
    heading(f"{vendor.title()} skill installed", "The bootstrap is global; its report guide remains project-local.")
    for target in targets:
        print(green("✓") + " " + dim(str(target)))
    print(dim("  " + agent_vendors.get(vendor).setup_hint(APP)))


def _git(args: list[str], cwd: Path, *, capture: bool = False) -> subprocess.CompletedProcess:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Git is required for Overleaf synchronization. Install Git, then retry.")
    result = subprocess.run([git, *args], cwd=cwd, stdin=None,
                            capture_output=capture, text=True)
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError((detail + "\n\n" if detail else "") + "Overleaf sync did not complete.\n\n" + OVERLEAF_HELP)
    return result


def _overleaf_remote_branch(checkout: Path) -> str:
    """Discover the remote's real default branch instead of assuming `master`."""
    symbolic = _git(["ls-remote", "--symref", "lockedin-overleaf", "HEAD"], checkout, capture=True)
    for line in (symbolic.stdout or "").splitlines():
        if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
            return line.removeprefix("ref: refs/heads/").removesuffix("\tHEAD")
    heads = _git(["ls-remote", "--heads", "lockedin-overleaf"], checkout, capture=True)
    branches = [line.rsplit("refs/heads/", 1)[1] for line in (heads.stdout or "").splitlines()
                if "refs/heads/" in line]
    if "main" in branches: return "main"
    if "master" in branches: return "master"
    if len(branches) == 1: return branches[0]
    if branches:
        raise RuntimeError("Could not determine Overleaf's default branch. Use `git branch -r` in `.lockedin/overleaf` and sync manually.")
    raise RuntimeError("The linked Overleaf Git project has no branch yet. Create or commit its first file in Overleaf, then retry.")


def _overleaf_credential_path() -> Path:
    """Keep the user's single Git credential store private and outside every repository."""
    return data_root() / "overleaf-credentials" / "credentials"


def _legacy_overleaf_credential_path(project: Path) -> Path:
    """Locate the brief per-project v2 credential layout for a one-time safe migration."""
    import hashlib
    key = hashlib.sha256(str(project.resolve()).encode()).hexdigest()[:24]
    return data_root() / "overleaf-credentials" / f"{key}.credentials"


def _is_managed_overleaf_helper(value: str) -> bool:
    return value.startswith("store --file=") and str(data_root() / "overleaf-credentials") in value


def _configure_overleaf_credential_store(project: Path, checkout: Path) -> Path | None:
    """Configure one user-level, Overleaf-only Git store when no external helper exists."""
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Git is required for Overleaf synchronization. Install Git, then retry.")
    configured = subprocess.run([git, "config", "--get-all", "credential.helper"], cwd=checkout,
                               capture_output=True, text=True)
    if configured.returncode not in {0, 1}:
        raise RuntimeError("Could not inspect Git credential-helper configuration.\n\n" + OVERLEAF_HELP)
    helpers = [line.strip() for line in configured.stdout.splitlines() if line.strip()]
    host_configured = subprocess.run(
        [git, "config", "--get-all", "credential.https://git.overleaf.com.helper"], cwd=checkout,
        capture_output=True, text=True,
    )
    if host_configured.returncode not in {0, 1}:
        raise RuntimeError("Could not inspect Git credential-helper configuration.\n\n" + OVERLEAF_HELP)
    host_helpers = [line.strip() for line in host_configured.stdout.splitlines() if line.strip()]
    if ((helpers and not all(_is_managed_overleaf_helper(value) for value in helpers)) or
            (host_helpers and not all(_is_managed_overleaf_helper(value) for value in host_helpers))):
        return None
    path = _overleaf_credential_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    legacy = _legacy_overleaf_credential_path(project)
    if not path.exists() and legacy.exists():
        shutil.copyfile(legacy, path)
        legacy.unlink()
    if not path.exists(): path.touch()
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    # Previous v2 builds configured this helper in each checkout. Remove that managed local
    # override so the new host-specific global configuration covers every project.
    local_configured = subprocess.run([git, "config", "--local", "--get-all", "credential.helper"], cwd=checkout,
                                     capture_output=True, text=True)
    if local_configured.returncode not in {0, 1}:
        raise RuntimeError("Could not inspect this project's Git configuration.\n\n" + OVERLEAF_HELP)
    local_helpers = [line.strip() for line in local_configured.stdout.splitlines() if line.strip()]
    if local_helpers and all(_is_managed_overleaf_helper(value) for value in local_helpers):
        _git(["config", "--local", "--unset-all", "credential.helper"], checkout)
    _git(["config", "--global", "--replace-all", "credential.https://git.overleaf.com.helper", f"store --file={path}"], checkout)
    _git(["config", "--global", "credential.https://git.overleaf.com.useHttpPath", "true"], checkout)
    return path


def _overleaf_config(project: Path) -> dict:
    path = project / ".lockedin" / "config" / "overleaf.yaml"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def overleaf_help_command() -> None:
    heading("Overleaf Git Bridge", "Manual publishing for a bubble-linked Overleaf project.")
    print("\n" + OVERLEAF_HELP)


def overleaf_connect(project: Path) -> None:
    config = _overleaf_config(project)
    url = str(config.get("overleaf_git_url") or "")
    if not url:
        raise RuntimeError("This bubble has no Overleaf project yet. Link one from its LockedIn website page, wait for sync, then retry.")
    root = project / ".lockedin" / "overleaf"
    if root.exists() and any(root.iterdir()):
        entries = list(root.iterdir())
        legacy = root / "README.md"
        if len(entries) == 1 and entries[0] == legacy and legacy.read_text(encoding="utf-8", errors="replace").startswith(LEGACY_OVERLEAF_README_PREFIX):
            legacy.unlink(); root.rmdir()
        else:
            raise RuntimeError(".lockedin/overleaf already exists. Use `lockedin-scientist overleaf status` or disconnect it first.")
    root.parent.mkdir(parents=True, exist_ok=True)
    _git(["clone", url, str(root)], project)
    _git(["remote", "rename", "origin", "lockedin-overleaf"], root)
    _git(["config", "core.fileMode", "false"], root)
    credential_path = _configure_overleaf_credential_store(project, root)
    heading("Overleaf connected", f"{config.get('overleaf_url', '')} → {root}")
    if credential_path:
        print(dim("  Git will securely reuse the token you enter next for this project."))
    print(green("✓") + " Local changes stay local until you run `lockedin-scientist overleaf sync`.")


def overleaf_status(project: Path) -> None:
    root = project / ".lockedin" / "overleaf"
    if not (root / ".git").is_dir():
        config = _overleaf_config(project)
        if config: print(dim("Overleaf is linked on the website but not cloned locally. Run `lockedin-scientist overleaf connect`."))
        else: print(dim("This bubble has no linked Overleaf project."))
        return
    heading("Overleaf status", str(root))
    result = _git(["status", "--short", "--branch"], root, capture=True)
    print(result.stdout.strip() or green("✓ Clean and ready to sync."))
    config = _overleaf_config(project)
    remote = _git(["remote", "get-url", "lockedin-overleaf"], root, capture=True).stdout.strip()
    if config and remote != config.get("overleaf_git_url"):
        print(orange("! The website association changed; disconnect and connect again before syncing."))
    elif not config:
        print(orange("! The website association was removed; this local clone was retained safely."))


def overleaf_sync(project: Path, message: str | None = None) -> None:
    root = project / ".lockedin" / "overleaf"
    if not (root / ".git").is_dir():
        raise RuntimeError("No local Overleaf checkout. Run `lockedin-scientist overleaf connect` first.")
    config = _overleaf_config(project)
    remote = _git(["remote", "get-url", "lockedin-overleaf"], root, capture=True).stdout.strip()
    if not config or remote != config.get("overleaf_git_url"):
        raise RuntimeError("The local checkout does not match the website's Overleaf association. Disconnect and connect again before syncing.")
    _configure_overleaf_credential_store(project, root)
    branch = _overleaf_remote_branch(root)
    dirty = _git(["status", "--porcelain"], root, capture=True).stdout.strip()
    if dirty:
        _git(["add", "-A"], root)
        _git(["commit", "-m", message or f"LockedIn Scientist sync {time.strftime('%Y-%m-%d %H:%M')}"], root)
    _git(["fetch", "lockedin-overleaf", branch], root)
    _git(["rebase", f"lockedin-overleaf/{branch}"], root)
    _git(["push", "lockedin-overleaf", f"HEAD:{branch}"], root)
    heading("Overleaf synchronized")
    print(green("✓") + " Pulled remote work and published the local checkout.")


def overleaf_abort(project: Path) -> None:
    _git(["rebase", "--abort"], project / ".lockedin" / "overleaf")
    print(green("✓") + " Aborted the Overleaf rebase.")


def overleaf_disconnect(project: Path, discard_local: bool) -> None:
    root = project / ".lockedin" / "overleaf"
    if not root.exists(): return
    if not discard_local and (root / ".git").is_dir():
        dirty = _git(["status", "--porcelain"], root, capture=True).stdout.strip()
        branch = _overleaf_remote_branch(root)
        ahead = _git(["log", "--oneline", f"lockedin-overleaf/{branch}..HEAD"], root, capture=True).stdout.strip()
        if dirty or ahead:
            raise RuntimeError("Overleaf has unsynced local work. Sync or copy it first, then use `overleaf disconnect --discard-local`.")
    _remove_tree(root)
    print(green("✓") + " Removed the local Overleaf checkout. The website association was unchanged.")


class ProjectSync:
    def __init__(self, account: dict, project: Path, bubble: str):
        self.account, self.project, self.bubble = account, project.resolve(), bubble
        self.root = self.project / ".lockedin"
        self.config = self.root / "config"
        self.binding_path = self.config / "binding.json"
        self.state_path = self.config / "sync-state.json"
        # Kept out of binding.json deliberately: that file is compared for exact equality against
        # the expected server/workspace/bubble, so an extra key there would read as a mismatch.
        self.identity_path = self.config / "identity.json"
        # What the last synchronization attempt observed, reported to the server on the next one.
        self.report = {"status": "", "error": ""}

    @staticmethod
    def _rev(data: bytes) -> str:
        import hashlib
        return hashlib.sha256(data).hexdigest()

    def _binding(self) -> dict | None:
        try: return json.loads(self.binding_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): return None

    def _state(self) -> dict:
        try: return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): return {"files": {}}

    def _write_state(self, state: dict) -> None: _atomic_json(self.state_path, state, private=True)

    def _refresh_skill(self) -> None:
        response = account_request(self.account, "GET", "/api/scientist/v2/guide")
        guide = response.get("guide", "")
        if not guide.strip():
            raise RuntimeError("The server did not provide the LockedIn Editing Guide. Reinstall or update the server.")
        write_skill_bundle(self.root, guide, response.get("math_macros"))

    def validate_or_initialize(self, *, reset: bool = False) -> None:
        current = self._binding()
        wanted = {"server": self.account["server"], "user": self.account["user"],
                  "workspace_id": self.account.get("workspace_id", ""), "bubble": self.bubble}
        if current and not reset and current != wanted:
            raise RuntimeError(".lockedin belongs to another server, workspace, or bubble. Run `lockedin-scientist hard-reset <bubble>`.")
        if self.root.exists() and current is None and not reset:
            raise RuntimeError(
                ".lockedin/config/binding.json is missing, so Scientist cannot safely identify its bubble. "
                "No worker was started. Copy any unsynchronized report work elsewhere, then run "
                "`lockedin-scientist hard-reset <bubble>` to rebuild .lockedin from the server."
            )
        if reset and self.root.exists(): _remove_tree(self.root)
        created = not self.root.exists()
        if created:
            (self.root / "assets").mkdir(parents=True)
            (self.root / "reports" / "pages").mkdir(parents=True)
            (self.root / "reports" / "assets").mkdir(parents=True)
            self.config.mkdir(parents=True)
        # The agent's throwaway space: code, venvs, outputs. Never synced — see sync_once, which
        # only ever reads/writes/deletes paths under reports/. Created unconditionally (not only
        # when `created`) so a project bound before this feature existed still gets one.
        # `.pycache` is where a scratch script's imports of project code land their bytecode cache
        # (see AgentRunner._dispatch's PYTHONPYCACHEPREFIX), inside the boundary instead of failing
        # silently beside a read-only module.
        (self.root / "scratch" / ".pycache").mkdir(parents=True, exist_ok=True)
        if created:
            _atomic_json(self.binding_path, wanted)
            self._write_state({"files": {}})
            self._exclude_from_git()
        skill = self.root / "SKILL.md"
        if created or f"lockedin-scientist-skill: {SKILL_VERSION}" not in (skill.read_text(encoding="utf-8") if skill.exists() else ""):
            self._refresh_skill()

    def _exclude_from_git(self) -> None:
        git = self.project / ".git"
        if not git.is_dir(): return
        exclude = git / "info" / "exclude"; exclude.parent.mkdir(parents=True, exist_ok=True)
        text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if ".lockedin/" not in text.splitlines():
            exclude.write_text(text.rstrip("\n") + "\n.lockedin/\n", encoding="utf-8")

    def worker_uid(self) -> str:
        """A stable id for *this project directory*, minted once and kept across worker restarts.

        The server monitors one row per synchronized directory. The per-run worker id would make a
        restarted worker look like a second directory, which is precisely the distinction the
        monitor exists to make.
        """
        try: return str(json.loads(self.identity_path.read_text(encoding="utf-8"))["worker_uid"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError): pass
        uid = secrets.token_hex(8)
        try:
            self.config.mkdir(parents=True, exist_ok=True)
            _atomic_json(self.identity_path, {"worker_uid": uid}, private=True)
        except OSError:
            pass
        return uid

    def _presence_headers(self) -> dict:
        return {"X-LockedIn-Worker": self.worker_uid(),
                "X-LockedIn-Worker-Label": header_value(self.project.name),
                "X-LockedIn-Worker-Status": header_value(self.report.get("status", "")),
                "X-LockedIn-Worker-Error": header_value(self.report.get("error", ""))}

    def _request(self, method: str, suffix: str, body: dict | None = None) -> dict:
        return account_request(self.account, method, f"/api/scientist/v2/bubbles/{self.bubble}/{suffix}", body,
                               extra=self._presence_headers())

    def _read_remote(self, paths: list[str], sizes: dict[str, int] | None = None) -> dict[str, dict]:
        result: dict[str, dict] = {}
        sizes = sizes or {}
        # Bounded by bytes as well as count: 200 figures is a modest number of files and a
        # response no proxy or memory budget should be asked to carry in one piece.
        for batch in _batched_by_size(paths, lambda rel: sizes.get(rel, 0)):
            for start in range(0, len(batch), 200):
                for item in self._request("POST", "files",
                                          {"paths": batch[start:start + 200]}).get("files", []):
                    result[item["path"]] = item
        return result

    def _local(self, rel: str) -> Path: return self.root / rel

    def _write_remote(self, item: dict) -> None:
        target = self._local(item["path"])
        # A prior pull protects asset trees; make just enough of that local protection writable
        # before replacing the server-authoritative file, then restore protection below.
        for parent in (target.parent.parent, target.parent):
            try: os.chmod(parent, 0o755)
            except OSError: pass
        try: os.chmod(target, 0o644)
        except OSError: pass
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(item["content_b64"]))
        self._protect(item["path"], target)

    def _protect(self, rel: str, path: Path) -> None:
        if (rel.startswith("assets/") or rel.startswith("indexes/") or rel.startswith("feedback/")
                or rel.endswith("/marks.json") or rel == "index.json"
                or rel in {"config/math.yaml", "config/aesthetics.yaml",
                           "config/overleaf.yaml"}):
            try: os.chmod(path, 0o444)
            except OSError: pass
        if rel.startswith("assets/"):
            for parent in (path.parent, path.parent.parent):
                try: os.chmod(parent, 0o555)
                except OSError: pass

    def _conflict(self, rel: str, base: bytes, local: bytes, remote: bytes) -> None:
        folder = self.config / "conflicts" / str(int(time.time() * 1000)); folder.mkdir(parents=True, exist_ok=True)
        stem = Path(rel).name
        (folder / (stem + ".base")).write_bytes(base)
        (folder / (stem + ".local")).write_bytes(local)
        (folder / (stem + ".remote")).write_bytes(remote)
        patch = "".join(difflib.unified_diff(base.decode(errors="replace").splitlines(True), local.decode(errors="replace").splitlines(True), fromfile="base", tofile="local"))
        (folder / (stem + ".patch")).write_text(patch, encoding="utf-8")

    def _report_paths(self) -> list[str]:
        """Local writable content: reports plus flat, provenance-tagged scratch artifacts.

        A deck an agent wrote was scanned nowhere, so it stayed local forever while the server
        would happily have taken it — writing one file is the whole documented way to create a
        talk. The generated sidecars beside a deck are excluded: they are the server's.
        """
        out = []
        for base in (self.root / "reports" / "pages", self.root / "reports" / "assets"):
            if not base.exists():
                continue
            for p in base.iterdir():
                if not p.is_file() or p.is_symlink():
                    continue
                out.append(p.relative_to(self.root).as_posix())
        talks_root = self.root / "reports" / "talks"
        if talks_root.exists():
            for p in talks_root.glob("talk-*/slides.md"):
                if p.is_file() and not p.is_symlink():
                    out.append(p.relative_to(self.root).as_posix())
        scratch_root = self.root / "scratch"
        if scratch_root.exists():
            for p in scratch_root.iterdir():
                if p.is_file() and not p.is_symlink() and scratch_sync_name(p.name):
                    out.append(f"scratch/{p.name}")
        return sorted(out)

    def unsynced_figures(self) -> list[str]:
        """Figures that exist locally but this sync cannot carry.

        Report figures are flat by contract — LockedIn serves them from a single-segment URL, so a
        figure in a subdirectory can never be rendered, pushed, or deleted. Skipping one silently is
        how an agent's work disappears, so the worker reports these as a degraded state.
        """
        base = self.root / "reports" / "assets"
        if not base.exists():
            return []
        return sorted(p.relative_to(base).as_posix() for p in base.rglob("*")
                      if p.is_file() and not p.is_symlink() and p.parent != base)

    def figure_warnings(self) -> list[str]:
        """Human-readable problems with this project's figures, worst first."""
        base = self.root / "reports" / "assets"
        if not base.exists():
            return []
        warnings = []
        nested = self.unsynced_figures()
        if nested:
            warnings.append(
                f"{len(nested)} figure(s) in subdirectories of .lockedin/reports/assets/ are NOT "
                f"synchronized; move them directly into that folder: "
                + ", ".join(nested[:5]) + (" …" if len(nested) > 5 else ""))
        # Only *unsynchronized* figures are worth mentioning. Once a figure has synced, pages link
        # to it by name, so renaming it would break those links — which is exactly why the server
        # does not normalize names on push either. Nagging about a working figure the owner must
        # not touch is noise, so this stays limited to files that are still free to rename.
        tracked = set(self._state().get("files", {}))
        odd = sorted(p.name for p in base.iterdir()
                     if p.is_file() and not p.is_symlink() and p.name != _figure_name(p.name)
                     and f"reports/assets/{p.name}" not in tracked)
        if odd:
            warnings.append(
                f"{len(odd)} new figure name(s) differ from what the website assigns; renaming them "
                f"before they are referenced avoids a duplicate if the same file is ever uploaded "
                f"there: " + ", ".join(f"{n} -> {_figure_name(n)}" for n in odd[:5])
                + (" …" if len(odd) > 5 else ""))
        return warnings

    def sync_once(self) -> None:
        self.validate_or_initialize()
        response = self._request("GET", "manifest")
        if response.get("secure_mode"):
            raise SecureModeStop(
                "Secure mode is on: this sync worker has stopped. Turn it off, then run "
                "lockedin-scientist resync locally to resume.")
        entries = response.get("files", [])
        remote = {f["path"]: f["revision"] for f in entries}
        cap = int(response.get("large_asset_bytes") or 0)
        remote_sizes = {f["path"]: int(f.get("size") or 0) for f in entries}
        # Assets the server declines to stream on a poll (photo archives, datasets). They stay in
        # the manifest so they are not mistaken for deletions, but they are not content-synced:
        # never fetched, never pushed, never removed locally. ``assets pull`` fetches one.
        oversize = {f["path"] for f in entries if f.get("oversize")}
        state = self._state(); tracked: dict = state.setdefault("files", {})
        prior_math_revision = state.get("skill_math_revision")
        remote_data: dict[str, dict] = {}

        def fetch(rel: str) -> dict | None:
            if rel not in remote: return None
            if rel not in remote_data: remote_data.update(self._read_remote([rel], remote_sizes))
            return remote_data.get(rel)

        # Layout v1 used title-derived flat deck filenames. Existing server talks receive a
        # deterministic opaque id, so migrate tracked files without fetching title/index content
        # and preserve their last-known revision plus any unsynced local edits.
        for legacy in list(tracked):
            parts = Path(legacy).parts
            if len(parts) != 3 or parts[:2] != ("reports", "talks") or not legacy.endswith(".md"):
                continue
            old_id = Path(parts[2]).stem
            sync_id = "talk-" + self._rev(old_id.encode("utf-8"))[:12]
            current = f"reports/talks/{sync_id}/slides.md"
            if current not in remote:
                continue
            old_path, new_path = self._local(legacy), self._local(current)
            if old_path.exists() and not new_path.exists():
                new_path.parent.mkdir(parents=True, exist_ok=True)
                old_path.replace(new_path)
            tracked[current] = tracked.pop(legacy)

        def pushable_path(rel: str) -> bool:
            # Server-generated, so it arrives through the read-only path below and is never sent
            # back; pushing a copy would make the marker a real asset in the bubble.
            if rel in oversize or rel == NOT_SYNCED_PATH:
                return False
            parts = Path(rel).parts
            return bool(
                len(parts) == 2 and parts[0] == "scratch" and scratch_sync_name(parts[1])
                or
                len(parts) == 3 and parts[:2] in (("reports", "pages"), ("reports", "assets"))
                or len(parts) == 4 and parts[:2] == ("reports", "talks")
                and parts[3] == "slides.md"
            )

        report_remote = {r for r in remote if pushable_path(r)}
        # A file the agent dropped in locally that is over the cap is skipped in the same way:
        # pushing it would base64 the whole thing into one request body.
        def local_oversize(rel: str) -> bool:
            path = self._local(rel)
            try: return bool(cap) and path.is_file() and path.stat().st_size > cap
            except OSError: return False

        report_local = {r for r in self._report_paths()
                        if r not in oversize and r != NOT_SYNCED_PATH and not local_oversize(r)}
        deletes, writes, creates = [], [], []
        for rel, old in list(tracked.items()):
            if not pushable_path(rel): continue
            path = self._local(rel); local = path.read_bytes() if path.exists() else b""
            if rel not in remote:
                if path.exists() and self._rev(local) != old.get("revision", ""):
                    self._conflict(rel, b"", local, b"")
                if path.exists(): path.unlink()
                # ``report_local`` was captured before this reconciliation pass. Remove the
                # server-deleted path from that snapshot too, so it cannot be mistaken for a new
                # local page/asset below and read after unlinking it.
                report_local.discard(rel)
                tracked.pop(rel, None); continue
            if not path.exists():
                deletes.append({"path": rel, "base_revision": old["revision"]}); continue
            local_changed = self._rev(local) != old["revision"]
            remote_changed = remote[rel] != old["revision"]
            if local_changed and remote_changed:
                item = fetch(rel); raw = base64.b64decode(item["content_b64"])
                self._conflict(rel, b"", local, raw); self._write_remote(item); tracked[rel] = {"revision": item["revision"]}
            elif local_changed:
                writes.append({"path": rel, "base_revision": old["revision"], "content_b64": base64.b64encode(local).decode("ascii")})
            elif remote_changed:
                item = fetch(rel); self._write_remote(item); tracked[rel] = {"revision": item["revision"]}
        for rel in sorted(report_local - set(tracked) - report_remote):
            raw = self._local(rel).read_bytes()
            if rel.startswith("reports/pages/"):
                creates.append((rel, raw))
            else:
                writes.append({"path": rel, "base_revision": self._rev(b""), "content_b64": base64.b64encode(raw).decode("ascii")})
        for rel in sorted(report_remote - set(tracked)):
            item = fetch(rel); self._write_remote(item); tracked[rel] = {"revision": item["revision"]}
        if deletes:
            result = self._request("POST", "deletes", {"deletes": deletes})
            for item in result.get("applied", []): tracked.pop(item["path"], None)
            for item in result.get("conflicts", []):
                if item.get("content_b64"):
                    self._write_remote(item); tracked[item["path"]] = {"revision": item["revision"]}
        for rel, raw in creates:
            result = self._request("POST", "pages", {"bubble": self.bubble, "page_slug": Path(rel).stem,
                                                         "content_b64": base64.b64encode(raw).decode("ascii"), "base_revision": self._rev(b"")})
            for item in result.get("applied", []): tracked[item["path"]] = {"revision": item["revision"]}
            for item in result.get("conflicts", []):
                if item.get("content_b64"):
                    self._conflict(rel, b"", raw, base64.b64decode(item["content_b64"])); self._write_remote(item)
        for writes_batch in (_batched_by_size(
                writes, lambda w: len(w.get("content_b64", "")) * 3 // 4) if writes else []):
            result = self._request("POST", "push", {"writes": writes_batch})
            for item in result.get("applied", []):
                # The server may store a normalized form of a pushed page (wikilinks, display
                # math) and hands the stored bytes back when it does. Adopt them: keeping the
                # pre-normalized local copy would read as "locally changed" on every later cycle
                # and re-push an already-synchronized page forever.
                if item.get("content_b64"): self._write_remote(item)
                tracked[item["path"]] = {"revision": item["revision"]}
            for item in result.get("conflicts", []):
                rel = item["path"]; local = self._local(rel).read_bytes() if self._local(rel).exists() else b""
                if item.get("content_b64"):
                    raw = base64.b64decode(item["content_b64"]); self._conflict(rel, b"", local, raw); self._write_remote(item); tracked[rel] = {"revision": item["revision"]}
        # Everything except writable report content and tagged scratch is server-authoritative.
        # This includes the report manifest and paper inventory that agents read but never edit.
        for rel in sorted(r for r in remote if r not in report_remote and r not in oversize):
            path = self._local(rel)
            if (not path.exists() or self._rev(path.read_bytes()) != remote[rel]
                    or tracked.get(rel, {}).get("revision") != remote[rel]):
                item = fetch(rel); self._write_remote(item)
            tracked[rel] = {"revision": remote[rel]}
        # The marker disappears from the manifest as soon as the last large asset is gone.
        # Nothing prunes reports/, so a stale copy would sit there claiming files that no longer
        # exist — the exact confusion it was added to prevent.
        if NOT_SYNCED_PATH not in remote:
            marker = self._local(NOT_SYNCED_PATH)
            if marker.exists():
                try: os.chmod(marker, 0o644); marker.unlink()
                except OSError: pass
            tracked.pop(NOT_SYNCED_PATH, None)
        math_revision = remote.get("config/math.yaml", "")
        # The version marker is checked here as well as in validate_or_initialize, which only runs
        # when a worker starts: a project whose worker has been up since before a SKILL_VERSION
        # bump would otherwise keep serving its agents the old guide indefinitely — exactly the
        # projects that are working fine and never get restarted.
        skill = self.root / "SKILL.md"
        stale = f"lockedin-scientist-skill: {SKILL_VERSION}" not in (
            skill.read_text(encoding="utf-8") if skill.exists() else "")
        if prior_math_revision != math_revision or stale:
            self._refresh_skill()
            state["skill_math_revision"] = math_revision
        for root_name in ("assets",):
            root = self.root / root_name
            if root.exists():
                for path in sorted(root.rglob("*"), reverse=True):
                    if path.is_file() and path.relative_to(self.root).as_posix() not in remote:
                        try: os.chmod(path, 0o644); path.unlink()
                        except OSError: pass
                    elif path.is_dir() and not any(path.iterdir()):
                        try: os.chmod(path, 0o755); path.rmdir()
                        except OSError: pass
        for rel in list(tracked):
            if rel not in remote and not pushable_path(rel):
                path = self._local(rel)
                try: os.chmod(path, 0o644); path.unlink(missing_ok=True)
                except OSError: pass
                tracked.pop(rel, None)
        self._write_state(state)


# --------------------------------------------------------------------------- #
# Agents: a persistent CLI conversation the bubble can hand a mark to
# --------------------------------------------------------------------------- #
def cli_name() -> str:
    """How this client is invoked here, so prompts and hints name the right command (a dev shim, say).

    A shim that execs this file leaves ``sys.argv[0]`` as the script path, so it announces its
    own name through ``LOCKEDIN_SCIENTIST_CLI_NAME`` instead.
    """
    announced = os.environ.get("LOCKEDIN_SCIENTIST_CLI_NAME", "").strip()
    if announced: return announced
    name = Path(sys.argv[0] or "").name
    return name if name.startswith(APP) else APP


def scratch_sync_name(name: str) -> bool:
    """Whether a flat scratch artifact carries the required mark/thread provenance tag."""
    return bool(Path(name).name == name and SCRATCH_SYNC_NAME.fullmatch(name))


def _scratch_part(value: object, fallback: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return (cleaned or fallback)[:80].rstrip("-")


def agent_scratch_tag(job: dict) -> str:
    """Stable filename prefix shared by every agent turn attached to the same mark."""
    mark = job.get("mark") or {}
    if job.get("kind") == "direct" or mark.get("surface") == "direct":
        return f"thread-{_scratch_part(job.get('thread_id') or job.get('id'), 'unknown')}"
    mark_id = _scratch_part(mark.get("id") or mark.get("note_id"), "unknown")
    if mark and mark.get("surface") == "page":
        return f"mark-page-{_scratch_part(mark.get('page'), 'page')}-{mark_id}"
    if mark:
        talk = _scratch_part(mark.get("talk_id") or mark.get("talk"), "talk")
        return f"mark-talk-{talk}-{mark_id}"
    return f"thread-{_scratch_part(job.get('thread_id') or job.get('id'), 'unknown')}"


def _git_toplevel(start: Path) -> Path | None:
    """Return this checkout/worktree's own top level, never Git's shared common directory."""
    try:
        out = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--show-toplevel"],
                             cwd=start, capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _project_root(start: Path) -> Path:
    """Find this checkout's binding without escaping into a parent or sibling worktree."""
    start = start.resolve()
    boundary = _git_toplevel(start)
    candidates = (start, *start.parents)
    for candidate in candidates:
        if boundary is not None and candidate != boundary and boundary not in candidate.parents:
            break
        if (candidate / ".lockedin" / "config" / "binding.json").exists():
            return candidate
        if boundary is not None and candidate == boundary:
            break
    return boundary or start


def conversation_exists(agent: dict) -> bool:
    """Whether the vendor still has this agent's conversation where it stores them.

    Checked before spawning into an old conversation, so a deleted chat id is caught cheaply
    rather than by launching the vendor and parsing its refusal. Returns True whenever the check
    cannot be made confidently — an unrecognised vendor, or an agent with no conversation yet,
    which is new rather than missing and belongs to the fresh-turn path — so an unknown layout
    never blocks real work.
    """
    vendor = str(agent.get("vendor") or "").strip()
    conversation = str(agent.get("conversation") or "").strip()
    if not conversation:
        return True
    try: return agent_vendors.get(vendor).conversation_exists(conversation)
    except RuntimeError: return True


def detect_conversation(project: Path, *, vendor: str = "", conversation: str = "") -> tuple[str, str]:
    """Which CLI conversation this command runs inside, so an agent never copies an id by hand."""
    return agent_vendors.detect_conversation(project, vendor=vendor, conversation=conversation)


def _chat_pids_path(root: Path) -> Path: return root / "config" / "agent-chats.json"


def _read_chat_pids(root: Path) -> dict:
    try: return json.loads(_chat_pids_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return {}


def _write_chat_pids(root: Path, data: dict) -> None:
    try: _atomic_json(_chat_pids_path(root), data, private=True)
    except OSError: pass


def _is_shared_vendor_host(vendor: str, cmdline: "list[bytes] | list[str]") -> bool:
    """Whether one vendor process hosts many conversations rather than this one chat."""
    if vendor.lower() != "codex":
        return False
    parts = []
    for part in cmdline:
        if isinstance(part, bytes):
            parts.append(part.decode(errors="replace").lower())
        else:
            parts.append(str(part).lower())
    return "app-server" in parts


def _chat_pid_for(vendor: str) -> int:
    """The vendor process this command is running inside, if it is.

    ``agent register`` is executed by the model from within its own chat, so that chat is one of
    this process's ancestors. Recording it is what lets the worker see a first session as
    attached; the conversation id it would otherwise look for is in that process's environment,
    not its arguments.
    """
    if not Path("/proc").is_dir():
        if os.name == "nt":
            return _chat_pid_for_windows(vendor)
        return 0
    pid = os.getppid()
    for _ in range(12):
        if pid <= 1:
            break
        try:
            cmdline = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
            stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        except OSError:
            break
        argv0 = Path((cmdline[0] or b"").decode(errors="replace")).name
        # `claude` is a node script; its argv0 may be the interpreter, so check the whole line.
        joined = b" ".join(cmdline).decode(errors="replace")
        matched = argv0 == vendor or f"/{vendor}" in joined or joined.startswith(vendor + " ")
        # Codex desktop runs every conversation through one long-lived app-server. Recording that
        # shared PID makes an agent look attached forever after `/exit`. A standalone `codex`
        # process is still conversation-specific and remains the right lifetime signal.
        if matched and not _is_shared_vendor_host(vendor, cmdline):
            return pid
        try:
            pid = int(stat.rsplit(")", 1)[1].split()[1])
        except (IndexError, ValueError):
            break
    return 0


def _chat_pid_for_windows(vendor: str) -> int:
    """Windows equivalent of the /proc ancestry walk above.

    There is no /proc, so resolve the whole process table with a single PowerShell call (one
    ``Get-CimInstance`` round trip) and walk parent links in memory. This runs once per
    ``agent register`` — never inside the poll loop — so the subprocess cost is acceptable.
    """
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return 0
        rows = json.loads(result.stdout)
        if isinstance(rows, dict):
            rows = [rows]
        by_pid = {}
        for row in rows:
            try:
                by_pid[int(row["ProcessId"])] = (int(row["ParentProcessId"]), str(row.get("Name") or ""),
                                                  str(row.get("CommandLine") or ""))
            except (KeyError, TypeError, ValueError):
                continue
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return 0

    # node.exe/python.exe are generic hosts: the stem/prefix check below only accepts them when
    # the name itself matches the vendor (e.g. vendor "node"), never as a catch-all — otherwise
    # every Python or Node ancestor in the tree would false-match.
    vendor_lower = vendor.lower()
    pid = os.getppid()
    for _ in range(12):
        entry = by_pid.get(pid)
        if not entry:
            break
        parent_pid, name, command_line = entry
        stem = Path(name).stem.lower()
        name_lower = name.lower()
        if (stem == vendor_lower or name_lower.startswith(vendor_lower)) and not (
                vendor_lower == "codex" and "app-server" in command_line.lower().split()):
            return pid
        if parent_pid == pid or parent_pid <= 0:
            break
        pid = parent_pid
    return 0


def agent_attached(agent: dict, root: Path, *, ignore: set[int] | None = None) -> bool:
    """Whether the agent's conversation is open in a terminal, so the worker must not drive it."""
    conversation = str(agent.get("conversation") or "")
    if not conversation: return False
    try:
        if conversation in agent_vendors.get(str(agent.get("vendor") or "")).live_conversations():
            return True
    except RuntimeError:
        pass
    pid = int(_read_chat_pids(root).get(str(agent.get("id", "")), 0) or 0)
    if pid and pid not in (ignore or set()) and _alive(pid):
        shared = False
        if Path("/proc").is_dir():
            try: shared = _is_shared_vendor_host(str(agent.get("vendor") or ""),
                                                 (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0"))
            except OSError: pass
        if not shared:
            return True
    proc = Path("/proc")
    if proc.is_dir() and len(conversation) >= 8:
        needle = conversation.encode()
        for entry in proc.iterdir():
            if not entry.name.isdigit() or int(entry.name) in (ignore or set()): continue
            try: cmdline = (entry / "cmdline").read_bytes()
            except OSError: continue
            if needle in cmdline and b"scientist_cli" not in cmdline:
                return True
    return False


def agent_turn_prompt(job: dict, *, cli: str, fresh: bool, mode: str | None = None) -> str:
    """One job, briefly. A resumed conversation already knows who it is and how this project works."""
    agent, mark = job.get("agent") or {}, job.get("mark") or {}
    mode = confinement_mode() if mode is None else mode
    lines: list[str] = []
    if fresh:
        persona = f"You are {agent.get('name') or 'an agent'}"
        if agent.get("role"): persona += f", {agent['role']}"
        persona += "."
        if agent.get("goal"): persona += f" Goal: {agent['goal']}."
        if agent.get("personality"): persona += f" Personality: {agent['personality']}."
        lines += [persona,
                  "This is a new conversation. Read `.lockedin/SKILL.md` and `.lockedin/guides/agents.md` "
                  "first; they describe this project and how you answer an assigned turn.",
                  "You may read anything on this machine, but write only under `.lockedin/`: "
                  "throwaway code, environments, and outputs go in `.lockedin/scratch/`; anything "
                  "meant to reach the bubble goes in `.lockedin/reports/`."]
        if mode == "none":
            lines.append("Nothing on this machine enforces that boundary right now — keep to it yourself.")
        lines.append("")
    scratch_tag = agent_scratch_tag(job)
    lines += [f"LockedIn job {job['id']}. Do it now, without asking questions.",
              f"Scratch tag: {scratch_tag}",
              f"Before creating code or resources, inspect `.lockedin/scratch/{scratch_tag}--*`. "
              "Reuse and edit a relevant existing file in place, even if another agent made it. "
              f"Every scratch file used for this answer must be flat and named `{scratch_tag}--<description>.<ext>`.",
              ""]
    if job.get("kind") == "direct" or mark.get("surface") == "direct":
        sender = str(job.get("created_by") or "the user")
        lines += [f"Direct message from {sender}:", str(job.get("instruction") or ""), "",
                  "Reply to the message when you are done. It is not attached to a report mark.", "",
                  "When done, run exactly one of:",
                  f"  {cli} agent reply {job['id']} --text \"<your response>\"",
                  f"  {cli} agent fail  {job['id']} --reason \"<why not>\"",
                  "Do not end the turn without running one of them."]
        return "\n".join(lines)
    kind = f"{mark.get('glyph')} ({mark.get('means')})" if mark.get("glyph") else str(mark.get("means") or "mark")
    if mark.get("surface") == "page":
        where = f"report page \"{mark.get('page_title') or mark.get('page')}\" ({mark.get('page')}), mark {mark.get('id')}"
    else:
        where = (f"chalk talk \"{mark.get('talk_title')}\" ({mark.get('talk_id')}), slide {int(mark.get('slide', 0) or 0) + 1}"
                 + (f" \"{mark['slide_title']}\"" if mark.get("slide_title") else "") + f", mark {mark.get('id')}")
    lines.append(f"Mark:   {kind} on {where}")
    touches = f"; the ink touches: {', '.join(mark['touches'][:8])}" if mark.get("touches") else ""
    if mark.get("quote"): lines.append(f"Quote:  \"{mark['quote']}\"")
    elif mark.get("anchor_type") == "drawing": lines.append("Drawn:  freehand ink on the slide" + touches)
    elif mark.get("anchor_type") == "region": lines.append("Region: a box drawn on the slide" + touches)
    for message in mark.get("messages", []):
        if message.get("agent") or not message.get("said"): continue
        lines.append(f"{message.get('by') or 'user'}: \"{message['said']}\"")
    if job.get("instruction"): lines.append(f"Note:   \"{job['instruction']}\"")
    if mark.get("shot_path"): lines.append(f"Picture: .lockedin/{mark['shot_path']}  (open it — the strokes are the feedback)")
    lines.append(f"Record: jq --arg id '{mark.get('id')}' '.by_id[$id]' .lockedin/{mark.get('detail_path')}")
    if mark.get("surface") == "page":
        lines.append(f"Edit:   .lockedin/{mark.get('source_path')}, between <comment-begin={mark.get('id')}> … "
                     f"<comment-end={mark.get('id')}> (keep both tags)")
    else:
        lines.append(f"Edit:   .lockedin/{mark.get('source_path')} (slide {int(mark.get('slide', 0) or 0) + 1}); never marks.json")
    lines += ["", "When done, run exactly one of:",
              f"  {cli} agent reply {job['id']} --text \"<what you changed and why>\"",
              f"  {cli} agent fail  {job['id']} --reason \"<why not>\"",
              "Do not end the turn without running one of them."]
    return "\n".join(lines)


def _vendor_binary(vendor: str) -> str:
    found = shutil.which(vendor)
    if not found:
        raise RuntimeError(f"`{vendor}` is not installed on this machine (not on PATH).")
    return found


# ---------------------------------------------------------------------------
# Confinement: where a headless turn may write, enforced by the worker itself
# ---------------------------------------------------------------------------
#
# All three vendors keep full capability inside a turn — bash, python, prototypes — but WHERE they
# may write is confined to this project's `.lockedin/` plus the handful of paths a vendor needs to
# persist its own conversation store. That boundary is enforced by the OS underneath the vendor,
# not by trusting codex/claude/agy's own sandbox flags, so all three end up with the same access.
#
# Landlock (Linux, unprivileged — ABI 6 as tested on this machine) is the primary mechanism: no
# root, no dedicated user, no namespace, no container. bubblewrap was tried here and found unusable
# without a sysctl change; a ctypes probe confirmed Landlock needs none of that — a confined child
# could write inside an allowed directory, was denied writes to the home directory and elsewhere,
# could read and execute everywhere, and kept network access (only `/dev/null` failed until `/dev`
# was added to the writable list). macOS gets a best-effort Seatbelt profile instead (untestable
# here, so kept small). Everywhere else — or when Landlock's syscalls are not available — the mode
# is "none": the turn runs with conservative vendor-side flags and no OS enforcement at all.
#
# LOCKEDIN_AGENT_CONFINEMENT overrides the auto-detected mode:
#   "none"  — force unconfined, conservative vendor flags (today's historical behaviour).
#   "trust" — the operator accepts full, unconfined capability deliberately, for example because
#             the worker itself already runs as a dedicated user or inside a VM of their own
#             making, so an escape from the OS sandbox here would not escape that outer boundary
#             either. Vendors get the same permissive flags as a confined turn, but nothing on
#             this machine enforces the write boundary.

LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_SYS_CREATE_RULESET = 444
_LANDLOCK_SYS_ADD_RULE = 445
_LANDLOCK_SYS_RESTRICT_SELF = 446
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12
LANDLOCK_ACCESS_FS_REFER = 1 << 13      # needs ABI >= 2
LANDLOCK_ACCESS_FS_TRUNCATE = 1 << 14   # needs ABI >= 3
LANDLOCK_ACCESS_FS_IOCTL_DEV = 1 << 15  # needs ABI >= 5
LANDLOCK_ACCESS_FS_READ_EXECUTE = (
    LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR)
# Rights the kernel only accepts on a rule whose path is a directory: everything about what a
# directory may *contain* (creating, removing, listing, or re-linking an entry beneath it). A rule
# on a regular file is legal (state a vendor writes, like ~/.claude.json), but the kernel rejects
# any of these bits for one with EINVAL, so they are masked out when the rule's target is not a
# directory — see `_landlock_child_confine`'s `add_rule`.
LANDLOCK_ACCESS_FS_DIR_ONLY = (
    LANDLOCK_ACCESS_FS_READ_DIR | LANDLOCK_ACCESS_FS_REMOVE_DIR | LANDLOCK_ACCESS_FS_REMOVE_FILE |
    LANDLOCK_ACCESS_FS_MAKE_CHAR | LANDLOCK_ACCESS_FS_MAKE_DIR | LANDLOCK_ACCESS_FS_MAKE_REG |
    LANDLOCK_ACCESS_FS_MAKE_SOCK | LANDLOCK_ACCESS_FS_MAKE_FIFO | LANDLOCK_ACCESS_FS_MAKE_BLOCK |
    LANDLOCK_ACCESS_FS_MAKE_SYM | LANDLOCK_ACCESS_FS_REFER)

# (minimum ABI, bit) for every access right this client knows about; a bit is only ever handed to
# the ruleset when the running kernel's ABI actually supports it, as the syscall requires.
_LANDLOCK_ABI_BITS = (
    (1, LANDLOCK_ACCESS_FS_EXECUTE), (1, LANDLOCK_ACCESS_FS_WRITE_FILE), (1, LANDLOCK_ACCESS_FS_READ_FILE),
    (1, LANDLOCK_ACCESS_FS_READ_DIR), (1, LANDLOCK_ACCESS_FS_REMOVE_DIR), (1, LANDLOCK_ACCESS_FS_REMOVE_FILE),
    (1, LANDLOCK_ACCESS_FS_MAKE_CHAR), (1, LANDLOCK_ACCESS_FS_MAKE_DIR), (1, LANDLOCK_ACCESS_FS_MAKE_REG),
    (1, LANDLOCK_ACCESS_FS_MAKE_SOCK), (1, LANDLOCK_ACCESS_FS_MAKE_FIFO), (1, LANDLOCK_ACCESS_FS_MAKE_BLOCK),
    (1, LANDLOCK_ACCESS_FS_MAKE_SYM),
    (2, LANDLOCK_ACCESS_FS_REFER),
    (3, LANDLOCK_ACCESS_FS_TRUNCATE),
    (5, LANDLOCK_ACCESS_FS_IOCTL_DEV),
)


def _landlock_handled_access(abi: int) -> int:
    """Every access bit the running kernel's Landlock ABI supports, OR'd together."""
    mask = 0
    for min_abi, bit in _LANDLOCK_ABI_BITS:
        if abi >= min_abi:
            mask |= bit
    return mask


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


def _raw_syscall(libc: ctypes.CDLL, number: int, *args: int) -> int:
    """The Landlock syscalls have no glibc wrapper on many still-current systems, so they are
    issued directly. Arguments are passed as ``long`` — wide enough for both small integers and
    the pointer values (buffer addresses, file descriptors) this module passes through it."""
    libc.syscall.restype = ctypes.c_long
    libc.syscall.argtypes = [ctypes.c_long] + [ctypes.c_long] * len(args)
    return int(libc.syscall(number, *args))


def landlock_abi() -> int:
    """The kernel's Landlock ABI version, or -1 when Landlock is unavailable.

    ``landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)`` is the kernel's documented
    probe: it reports the ABI version without creating anything, on any kernel new enough to know
    the syscall at all, and fails (ENOSYS or similar) otherwise.
    """
    try:
        ret = _raw_syscall(_libc(), _LANDLOCK_SYS_CREATE_RULESET, 0, 0, LANDLOCK_CREATE_RULESET_VERSION)
        return ret if ret > 0 else -1
    except OSError:
        return -1


def confinement_mode() -> str:
    """How a headless turn's writes are confined on this machine: "landlock", "seatbelt", "trust",
    or "none". Read at call time, not cached, so an operator's environment change — or a test's
    patch — takes effect on the very next turn.
    """
    override = os.environ.get("LOCKEDIN_AGENT_CONFINEMENT", "").strip().lower()
    if override == "none": return "none"
    if override == "trust": return "trust"
    if sys.platform.startswith("linux"):
        return "landlock" if landlock_abi() >= 1 else "none"
    if sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec"):
        return "seatbelt"
    return "none"


def _agent_writable_roots(project: Path) -> list[tuple[Path, bool]]:
    """Every path a headless turn may write beneath, each paired with whether it must exist.

    The project's own `.lockedin`, `/tmp`, and `/dev` are required: `/dev` is what makes
    `/dev/null`, `/dev/shm`, and GPU device nodes usable, and a missing required root means
    confinement itself cannot be set up correctly, so the turn must fail rather than quietly run
    with less confinement than asked. Vendor state directories are added only when they already
    exist, so a machine missing one vendor is never penalized for it. `LOCKEDIN_AGENT_WRITABLE` is
    colon-separated and deliberate — an operator opening a conda env should have a typo there fail
    loudly, not silently grant less access than requested.
    """
    home = Path.home()
    roots: list[tuple[Path, bool]] = [
        (project / ".lockedin", True),
        (Path("/tmp"), True),
        (Path("/dev"), True),
    ]
    # macOS normally sets TMPDIR to /var/folders/... rather than /tmp. Network/auth libraries and
    # vendor CLIs use it even for read-only requests; denying it can surface misleadingly as
    # "error sending request". Preserve the process's real temporary directory explicitly.
    runtime_tmp = os.environ.get("TMPDIR", "").strip()
    if runtime_tmp and Path(runtime_tmp).exists():
        roots.append((Path(runtime_tmp), False))
    for candidate in (*agent_vendors.writable_state_paths(home), home / ".cache", home / ".npm"):
        if candidate.exists():
            roots.append((candidate, False))
    for part in os.environ.get("LOCKEDIN_AGENT_WRITABLE", "").split(":"):
        part = part.strip()
        if part:
            roots.append((Path(part), True))
    return roots


def _landlock_child_confine(project: Path) -> None:
    """Run as ``preexec_fn`` in the forked child, before exec: restrict the child (and everything
    it execs) to read+execute beneath `/`, full access only beneath the project's `.lockedin` and
    the other writable roots. Any failure here writes one line to stderr and exits 97 rather than
    letting the turn run unconfined — ``AgentRunner._reap`` recognises exit 97 as "could not
    confine the turn", so a confinement failure is a failed job, never a silent escape.
    """
    try:
        libc = _libc()
        abi = landlock_abi()
        if abi < 1:
            raise OSError("Landlock ABI not reported by this kernel")
        libc.prctl.restype = ctypes.c_int
        libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")
        handled = _landlock_handled_access(abi)
        attr = struct.pack("=QQ", handled, 0) if abi >= 4 else struct.pack("=Q", handled)
        attr_buf = ctypes.create_string_buffer(attr, len(attr))
        ruleset_fd = _raw_syscall(libc, _LANDLOCK_SYS_CREATE_RULESET,
                                  ctypes.addressof(attr_buf), len(attr), 0)
        if ruleset_fd < 0:
            raise OSError(ctypes.get_errno(), "landlock_create_ruleset failed")

        def add_rule(path: Path, allowed_access: int) -> None:
            fd = os.open(str(path), os.O_PATH | os.O_CLOEXEC)
            try:
                if not stat.S_ISDIR(os.fstat(fd).st_mode):
                    allowed_access &= ~LANDLOCK_ACCESS_FS_DIR_ONLY
                rule = struct.pack("=Qi", allowed_access, fd)
                rule_buf = ctypes.create_string_buffer(rule, len(rule))
                ret = _raw_syscall(libc, _LANDLOCK_SYS_ADD_RULE, ruleset_fd, _LANDLOCK_RULE_PATH_BENEATH,
                                   ctypes.addressof(rule_buf), 0)
                if ret != 0:
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule failed for {path}")
            finally:
                os.close(fd)

        add_rule(Path("/"), LANDLOCK_ACCESS_FS_READ_EXECUTE)
        for root, required in _agent_writable_roots(project):
            if required or root.exists():
                add_rule(root, handled)
        if _raw_syscall(libc, _LANDLOCK_SYS_RESTRICT_SELF, ruleset_fd, 0) != 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")
        os.close(ruleset_fd)
    except Exception as exc:
        try:
            os.write(2, f"lockedin: could not confine the turn ({exc})\n".encode(errors="replace"))
        except OSError:
            pass
        os._exit(97)


def _seatbelt_profile(writable_roots: list[Path]) -> str:
    """A permissive Seatbelt profile: everything allowed by default, file writes denied except
    beneath the given roots. Best-effort only — there is no macOS machine to test this against
    here — so it is kept small and easy to audit by hand rather than clever."""
    lines = ["(version 1)", "(allow default)", "(allow network*)", "(deny file-write*)"]
    for root in writable_roots:
        escaped = str(root.resolve()).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'(allow file-write* (subpath "{escaped}"))')
    return "\n".join(lines) + "\n"


def seatbelt_command(cmd: list[str], project: Path) -> list[str]:
    """Best-effort macOS confinement: wrap the vendor command under ``sandbox-exec``."""
    roots = [root for root, required in _agent_writable_roots(project) if required or root.exists()]
    return ["/usr/bin/sandbox-exec", "-p", _seatbelt_profile(roots), *cmd]


def apply_confinement(cmd: list[str], mode: str, project: Path) -> tuple[list[str], dict]:
    """Wrap or annotate a vendor command so ``mode`` is actually enforced, returning the (possibly
    wrapped) argv and any extra ``subprocess.Popen`` keyword arguments. The turn's cwd stays the
    project root either way: confinement here is by path, not by cwd, so every path in the guides
    and prompts keeps meaning what it means today."""
    if mode == "landlock":
        return cmd, {"preexec_fn": functools.partial(_landlock_child_confine, project)}
    if mode == "seatbelt":
        return seatbelt_command(cmd, project), {}
    return cmd, {}


def agent_turn_command(agent: dict, prompt: str, *, new_id: str = "", mode: str | None = None,
                       fork_conversation: bool = False) -> list[str]:
    """One headless turn of the agent's conversation. Flags first: agy reads `-p` as the prompt's flag.

    ``mode`` selects the vendor flags (see ``confinement_mode``): a confined turn or an operator's
    explicit "trust" gets each vendor's most permissive flags, because the OS boundary — or the
    operator's own judgement — replaces the vendor's own sandbox; "none" keeps today's conservative
    flags, under which bash is unavailable to claude and agy. Defaults to the machine's actual mode
    so callers that do not care can omit it, but takes it as a plain argument so it is unit-testable
    without touching the environment.
    """
    minutes = max(1, AGENT_TURN_SECONDS // 60)
    mode = confinement_mode() if mode is None else mode
    permissive = mode in ("landlock", "seatbelt", "trust")
    adapter = agent_vendors.get(str(agent.get("vendor") or ""))
    return adapter.turn_command(agent, prompt, new_id=new_id, permissive=permissive,
                                turn_minutes=minutes, fork_conversation=fork_conversation,
                                binary=_vendor_binary)


def agent_chat_argv(agent: dict, *, new_id: str = "") -> list[str]:
    adapter = agent_vendors.get(str(agent.get("vendor") or ""))
    return adapter.chat_command(agent, new_id=new_id, binary=_vendor_binary)


def _discover_conversation(vendor: str, output: str, *, started: float, project: Path) -> str:
    """The id of a conversation a fresh turn just created, from its output or the vendor's store."""
    try: return agent_vendors.get(vendor).discover_conversation(output, started=started, project=project)
    except RuntimeError: return ""


def _looks_vendor_busy(output: str, vendor: str = "") -> bool:
    """Whether captured turn output matches a known vendor-busy signature (case-insensitive)."""
    lowered = (output or "").lower()
    signatures = agent_vendors.get(vendor).busy_signatures if vendor else AGENT_BUSY_SIGNATURES
    return any(signature in lowered for signature in signatures)


def _looks_conversation_lost(output: str, vendor: str = "") -> bool:
    """Whether captured turn output matches a known lost-conversation signature (case-insensitive).

    A conversation can vanish between the pre-dispatch ``conversation_exists`` check and the
    vendor actually running (or some vendors keep the file but refuse the id anyway), so this is
    the fallback: read the vendor's own refusal instead of trusting the check alone.
    """
    lowered = (output or "").lower()
    signatures = agent_vendors.get(vendor).lost_signatures if vendor else AGENT_LOST_CONVERSATION_SIGNATURES
    return any(signature in lowered for signature in signatures)


def _vendor_network_reconnects(output: str, vendor: str) -> int:
    """Number of known reconnect signals emitted by this vendor in captured output."""
    lowered = (output or "").lower()
    try: signatures = agent_vendors.get(vendor).network_signatures
    except RuntimeError: signatures = (AGENT_NETWORK_RECONNECT_SIGNATURE,)
    return sum(lowered.count(signature) for signature in signatures)


def _tail(path: Path, limit: int) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END); size = fh.tell()
            fh.seek(max(0, size - limit)); return fh.read().decode(errors="replace")
    except OSError:
        return ""


class AgentRunner:
    """Runs one headless turn per assigned job, inside the sync worker's five-second cycle.

    The worker learns about jobs on a heartbeat it only sends when this directory owns an agent
    (``indexes/agents.json`` says so, and it is already on disk). Between jobs no model process
    exists: an agent that is never assigned anything costs nothing at all.

    Setting ``LOCKEDIN_AGENT_TURNS=off`` (also accepts ``0``/``false``) makes every ``tick()``
    return immediately, before any heartbeat, job start, or process spawn — this pauses dispatch
    without stopping the rest of synchronization, which is useful on its own for debugging a
    worker or for running it under CI where nothing should ever launch a real coding agent.
    """

    def __init__(self, sync: ProjectSync, worker_id: str, cli: str):
        self.sync, self.worker_id, self.cli = sync, worker_id, cli
        self.jobs_dir = data_root() / "runtime" / "workers" / worker_id / "jobs"
        # Turn start times, pruned to the last day: a restart cannot reset the budget below, since
        # this is what tick() reads and appends to, not an in-memory counter.
        self.turns_path = data_root() / "runtime" / "workers" / worker_id / "turns.json"
        self.procs: dict[str, dict] = {}
        self.error = ""
        # Per-agent backstop: agent id -> a monotonic deadline after a busy-chat requeue, so we
        # do not immediately respawn a turn into a chat that just said it was in use. Cleared on
        # that agent's next successful turn.
        self.cooldowns: dict[str, float] = {}
        # Agent ids requeued during the _reap() call this tick: treated as attached for the rest
        # of the same tick so the dispatch loop below does not race to redispatch immediately.
        self._requeued_this_tick: set[str] = set()
        self._kill_strays()

    def _kill_strays(self) -> None:
        """A previous worker process may have left a turn running; it reports to nobody now."""
        for pid_file in self.jobs_dir.glob("*.pid") if self.jobs_dir.is_dir() else []:
            try: pid = int(pid_file.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError): pid = 0
            if pid and _alive(pid):
                try: os.killpg(pid, signal.SIGTERM) if os.name != "nt" else os.kill(pid, signal.SIGTERM)
                except OSError: pass
            pid_file.unlink(missing_ok=True)

    def my_agents(self) -> list[dict]:
        try: index = json.loads((self.sync.root / "indexes" / "agents.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): return []
        by_id = index.get("by_id", {})
        return [by_id[aid] for aid in index.get("by_worker", {}).get(self.sync.worker_uid(), []) if aid in by_id]

    def running_job_ids(self) -> list[str]: return sorted(self.procs)

    def _pids(self) -> set[int]: return {entry["proc"].pid for entry in self.procs.values()}

    def _heartbeat_agent(self, agent: dict, budget: dict) -> dict:
        """Describe one agent without pretending that a live process implies useful progress.

        The job id and log timestamps let the site distinguish a turn that only just started from
        one that has been quiet for a long time.  They deliberately contain no log text: prompts
        and model output can be private, while byte/time counters are enough for diagnosis.
        """
        item = {
            "id": agent["id"],
            "attached": agent_attached(agent, self.sync.root, ignore=self._pids()),
            "budget": budget,
            "confinement": confinement_mode(),
            "turn_timeout_seconds": AGENT_TURN_SECONDS,
        }
        entry = next((value for value in self.procs.values()
                      if value["agent"].get("id") == agent.get("id")), None)
        if entry:
            try:
                stat = entry["log"].stat()
                log_bytes, log_updated_at = stat.st_size, stat.st_mtime
            except OSError:
                log_bytes, log_updated_at = 0, entry["started"]
            item["activity"] = {
                "job_id": entry["job"]["id"],
                "started_at": datetime.fromtimestamp(entry["started"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "last_output_at": datetime.fromtimestamp(log_updated_at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "output_bytes": log_bytes,
                "deadline_at": datetime.fromtimestamp(entry["started"] + AGENT_TURN_SECONDS,
                                                       tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        return item

    @property
    def turns_disabled(self) -> bool:
        """Whether ``LOCKEDIN_AGENT_TURNS`` currently asks dispatch to pause; see the class docstring."""
        return agent_turns_disabled()

    def _load_turns(self) -> list[float]:
        try:
            data = json.loads(self.turns_path.read_text(encoding="utf-8"))
            return [float(t) for t in data.get("turns", []) if isinstance(t, (int, float))]
        except (OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
            return []

    def _record_turn_start(self, now: float) -> None:
        """Called once a turn actually spawns a vendor process — never for a dispatch that bails
        out before that (a missing conversation, a build-command failure, and so on)."""
        times = [t for t in self._load_turns() if now - t < AGENT_BUDGET_DAY_SECONDS]
        times.append(now)
        try:
            _atomic_json(self.turns_path, {"turns": times})
        except OSError:
            pass

    @staticmethod
    def _budget_window(times: list[float], now: float, seconds: int, cap: int) -> tuple[int, float]:
        """(turns actually started in the trailing ``seconds`` window, when the oldest of those
        would age out and free a slot — 0.0 when ``cap`` is unlimited or the window is not at its
        cap). ``used`` is reported even when ``cap`` is unlimited, for observability."""
        recent = sorted(t for t in times if now - t < seconds)
        used = len(recent)
        resumes_at = recent[used - cap] + seconds if cap > 0 and used >= cap else 0.0
        return used, resumes_at

    def _budget(self, now: float) -> tuple[dict, str]:
        """The current turn budget, and — only when it is exhausted — the line to put in
        ``self.error``. A cap of zero or less (``AGENT_MAX_TURNS_PER_HOUR``/``_DAY``) means
        unlimited, reported here as a cap of 0 and never exhausted."""
        times = self._load_turns()
        hour_used, hour_resume = self._budget_window(times, now, AGENT_BUDGET_HOUR_SECONDS, AGENT_MAX_TURNS_PER_HOUR)
        day_used, day_resume = self._budget_window(times, now, AGENT_BUDGET_DAY_SECONDS, AGENT_MAX_TURNS_PER_DAY)
        hour_cap = max(AGENT_MAX_TURNS_PER_HOUR, 0)
        day_cap = max(AGENT_MAX_TURNS_PER_DAY, 0)
        hour_hit = bool(hour_cap) and hour_used >= hour_cap
        day_hit = bool(day_cap) and day_used >= day_cap
        exhausted = hour_hit or day_hit
        error = ""
        resumes_epoch = 0.0
        if exhausted:
            cap, used, window, resumes_epoch = (
                (hour_cap, hour_used, "hour", hour_resume) if hour_hit else (day_cap, day_used, "day", day_resume))
            when = time.strftime("%H:%M", time.localtime(resumes_epoch)) if resumes_epoch else "?"
            error = f"budget: {cap} turns in the last {window}; next turn at {when}"
        resumes_iso = (datetime.fromtimestamp(resumes_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                       if resumes_epoch else "")
        budget = {"hour_used": hour_used, "hour_cap": hour_cap, "day_used": day_used, "day_cap": day_cap,
                  "exhausted": exhausted, "resumes_at": resumes_iso}
        return budget, error

    def tick(self) -> None:
        if self.turns_disabled:
            self.error = "agent turns are disabled by LOCKEDIN_AGENT_TURNS=off"
            return
        self._requeued_this_tick = set()
        self._reap()
        agents = self.my_agents()
        if not agents and not self.procs: return
        budget, budget_error = self._budget(time.time())
        beat = self.sync._request("POST", "agents/heartbeat", {
            "worker_id": self.sync.worker_uid(),
            "agents": [self._heartbeat_agent(a, budget) for a in agents],
            "running_job_ids": self.running_job_ids(),
            # Kept at top level for compatibility with clients/tests that used the original
            # telemetry shape. The authoritative per-agent copy above is what the server stores.
            "budget": budget, "confinement": confinement_mode()})
        if beat.get("secure_mode"):
            for job_id in list(self.procs):
                self._terminate(job_id, "secure mode is on")
            self.error = "secure mode is on: agents stopped"
            return
        for job_id in beat.get("cancelled", []):
            self._terminate(job_id, "cancelled by the user")
        if budget["exhausted"]:
            self.error = budget_error
            return
        busy = {entry["agent"].get("id") for entry in self.procs.values()}
        now = time.monotonic()
        for job in beat.get("jobs", []):
            if len(self.procs) >= AGENT_MAX_PARALLEL: break
            agent = job.get("agent") or {}
            aid = agent.get("id")
            if aid in busy: continue
            if aid in self._requeued_this_tick: continue  # just postponed; treat as attached
            if agent_attached(agent, self.sync.root, ignore=self._pids()): continue
            if self.cooldowns.get(aid, 0.0) > now: continue  # backstop cooldown still running
            self._dispatch(job); busy.add(aid)

    def _preserve_missing_conversation(self, agent: dict, job_id: str, *, reason: str,
                                       code: int | None = None, output: str = "") -> None:
        """Fail one turn without replacing a named agent's missing conversation.

        A name denotes one growing vendor conversation. Automatically clearing that id and
        retrying as ``fresh`` makes the UI appear to recover while silently erasing the person's
        working memory. Keep the record untouched so reconnecting the same folder/store can make
        it usable again; only the explicit ``agent reset`` escape hatch may discard that history.
        """
        self.cooldowns.pop(agent.get("id"), None)
        self._result(job_id, "failed", code, output, reason)

    def _dispatch(self, job: dict) -> None:
        agent = job["agent"]
        try:
            self.sync._request("POST", f"jobs/{job['id']}/start", {"worker_id": self.sync.worker_uid()})
        except RuntimeError as exc:
            if "server returned 409" in str(exc): return   # another cycle, or another worker, got it
            raise
        if agent.get("conversation") and not conversation_exists(agent):
            # Cheaper than spawning a vendor doomed to refuse it. Never make apparent progress by
            # replacing a named person's memory: fail visibly and leave the id available for the
            # setup-link / folder-store recovery path.
            self._preserve_missing_conversation(
                agent, job["id"],
                reason=f"{agent.get('vendor')} conversation {agent.get('conversation')!r} no longer exists; "
                       f"{AGENT_LOST_CONVERSATION_ERROR}")
            return
        fresh = bool(agent.get("fresh")) or not agent.get("conversation")
        # Codex can leave a conversation's writer owned after the registering terminal has
        # closed. The first resume reports that precise conflict and is requeued; on the retry,
        # fork the same history into a worker-owned thread instead of retrying forever or wiping
        # the agent's memory.
        fork_conversation = (
            str(agent.get("vendor") or "") == "codex"
            and bool(agent.get("conversation"))
            and int(job.get("attempts", 1) or 1) > 1
            and str(job.get("error") or "").startswith(AGENT_BUSY_ERROR)
        )
        new_id = str(uuid.uuid4())
        mode = confinement_mode()
        prompt = agent_turn_prompt(job, cli=self.cli, fresh=fresh, mode=mode)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        log = self.jobs_dir / f"{job['id']}.log"
        try:
            cmd = agent_turn_command(agent, prompt, new_id=new_id, mode=mode,
                                     fork_conversation=fork_conversation)
        except RuntimeError as exc:
            log.write_text(str(exc) + "\n", encoding="utf-8")
            self._result(job["id"], "failed", None, "", str(exc)); return
        cmd, popen_kwargs = apply_confinement(cmd, mode, self.sync.project)
        env = {**os.environ, "LOCKEDIN_JOB_ID": job["id"], "LOCKEDIN_BUBBLE": self.sync.bubble,
               "LOCKEDIN_PROJECT": str(self.sync.project), "LOCKEDIN_AGENT": str(agent.get("name") or ""),
               "LOCKEDIN_SCIENTIST_CLI": str(Path(__file__).resolve()), "NO_COLOR": "1",
               # A vendor may run ordinary developer tools. These close the common secondary
               # prompt paths too; stdin is DEVNULL below, so a missed prompt fails instead of
               # leaving a seemingly-running job waiting for a terminal that does not exist.
               "GIT_TERMINAL_PROMPT": "0", "PIP_NO_INPUT": "1", "SSH_ASKPASS_REQUIRE": "never"}
        # Keep vendor-specific transport/auth compatibility behind the adapter boundary. These
        # additions affect this child only and never rewrite the user's shell environment.
        env.update(agent_vendors.get(str(agent.get("vendor") or "")).turn_environment())
        # A scratch script that imports project code (the intended pattern — see guides/agents.md)
        # compiles it on the fly; without this, Python tries to write the .pyc beside the read-only
        # module, fails silently, and just recompiles every run. Point the cache inside the one tree
        # the turn can write, unless the operator already pointed it somewhere themselves.
        if not env.get("PYTHONPYCACHEPREFIX"):
            env["PYTHONPYCACHEPREFIX"] = str(self.sync.root / "scratch" / ".pycache")
        with log.open("w", encoding="utf-8") as fh:
            fh.write(f"# {time.strftime('%Y-%m-%d %H:%M:%S')} job {job['id']} → {agent.get('name')} "
                     f"({agent.get('vendor')}{' ' + agent['model'] if agent.get('model') else ''}, "
                     f"confinement={mode})\n")
            fh.write("# " + " ".join(shlex.quote(part) for part in cmd[:-1]) + " <prompt>\n\n" + prompt + "\n\n---- output ----\n")
        stream = log.open("ab")
        started = time.time()
        try:
            proc = subprocess.Popen(cmd, cwd=str(self.sync.project), env=env, stdin=subprocess.DEVNULL,
                                    stdout=stream, stderr=subprocess.STDOUT, start_new_session=os.name != "nt",
                                    **popen_kwargs)
        except OSError as exc:
            stream.close()
            self._result(job["id"], "failed", None, "", f"could not start {agent.get('vendor')}: {exc}"); return
        self._record_turn_start(started)
        (self.jobs_dir / f"{job['id']}.pid").write_text(str(proc.pid), encoding="utf-8")
        self.procs[job["id"]] = {"proc": proc, "agent": agent, "job": job, "started": started,
                                 "log": log, "stream": stream, "fresh": fresh,
                                 "adopt_conversation": fresh or fork_conversation,
                                 "new_id": new_id if agent_vendors.get(str(agent.get("vendor") or "")).preassigns_conversation_id
                                 and fresh else ""}

    def _terminate(self, job_id: str, reason: str) -> None:
        entry = self.procs.get(job_id)
        if not entry: return
        proc = entry["proc"]
        if proc.poll() is None:
            try: os.killpg(proc.pid, signal.SIGTERM) if os.name != "nt" else proc.terminate()
            except OSError: pass
        entry.setdefault("reason", reason)
        entry.setdefault("deadline", time.time() + 10)

    def _reap(self) -> None:
        now = time.time()
        for job_id, entry in list(self.procs.items()):
            proc = entry["proc"]
            if proc.poll() is None:
                # An agent can finish the LockedIn job from a child/background task while the
                # vendor's root CLI keeps waiting for other background work. The server reply is
                # authoritative: do not hold this agent's queue (Agy can otherwise wait for its
                # full print timeout after a successful reply). Reap only terminal jobs, and mark
                # them so the SIGTERM below is cleanup rather than a second, failed result.
                try:
                    remote = self.sync._request("GET", f"jobs/{job_id}").get("job", {})
                except RuntimeError:
                    remote = {}
                if remote.get("status") in {"done", "failed", "cancelled"}:
                    entry["server_finished"] = True
                    self._terminate(job_id, "")
                    continue
                age = now - entry["started"]
                if ("reason" not in entry and age > AGENT_NETWORK_GRACE_SECONDS
                        and _vendor_network_reconnects(
                            _tail(entry["log"], AGENT_OUTPUT_TAIL),
                            str(entry["agent"].get("vendor") or "")) >= 3):
                    self._terminate(job_id, "the agent could not reach its model service after repeated reconnects")
                elif "reason" not in entry and age > AGENT_TURN_SECONDS:
                    self._terminate(job_id, f"timed out after {AGENT_TURN_SECONDS // 60} minutes")
                elif "deadline" in entry and now > entry["deadline"]:
                    try: os.killpg(proc.pid, signal.SIGKILL) if os.name != "nt" else proc.kill()
                    except OSError: pass
                continue
            entry["stream"].close()
            (self.jobs_dir / f"{job_id}.pid").unlink(missing_ok=True)
            output = _tail(entry["log"], AGENT_OUTPUT_TAIL)
            agent = entry["agent"]
            if entry.get("adopt_conversation", entry["fresh"]):
                conversation = entry["new_id"] or _discover_conversation(
                    str(agent.get("vendor") or ""), output, started=entry["started"], project=self.sync.project)
                if conversation:
                    try: self.sync._request("POST", f"agents/{agent['id']}", {"conversation": conversation, "fresh": False})
                    except RuntimeError as exc: self.error = str(exc)
                else:
                    self.error = (f"{agent.get('name')}: could not learn the new conversation id; "
                                  f"run `{self.cli} agent chat {agent.get('name')}` once to create it")
            if entry.get("server_finished"):
                # The reply/failure already landed through `agent reply`/`agent fail`; emitting a
                # worker result after terminating the leftover root process would overwrite that
                # successful terminal state with SIGTERM's non-zero exit code.
                del self.procs[job_id]
                continue
            reason, code = entry.get("reason", ""), proc.returncode
            aid = agent.get("id")
            # `_landlock_child_confine` exits exactly 97 when it could not set up confinement, by
            # design — a confinement failure must be a failed job, never a silent escape into an
            # unconfined turn, so this is checked before any output-sniffing recovery path below.
            if not reason and code == 97:
                self._result(job_id, "failed", code, output, "could not confine the turn")
            # Backstop: a turn that failed only because the agent's own chat was open (a real
            # captured case: `codex exec resume` exited 1 with a thread-store conflict because an
            # interactive session held the writer) must not burn the job. Attach detection should
            # normally have caught this before dispatch, but it cannot be perfect on every vendor
            # or every OS, so fall back to sniffing the output for a known busy signature.
            elif not reason and code != 0 and _looks_vendor_busy(output, str(agent.get("vendor") or "")):
                if str(agent.get("vendor") or "") == "codex":
                    self.cooldowns.pop(aid, None)
                else:
                    self.cooldowns[aid] = time.monotonic() + AGENT_COOLDOWN_SECONDS
                self._requeued_this_tick.add(aid)
                self._result(job_id, "requeue", code, output, AGENT_BUSY_ERROR)
            elif not reason and code != 0 and _looks_conversation_lost(output, str(agent.get("vendor") or "")):
                # The conversation existed at dispatch time (or the layout could not be checked)
                # but the vendor refused the id anyway. Preserve the named agent's memory just as
                # the pre-dispatch check does; never turn this into an implicit fresh conversation.
                self._preserve_missing_conversation(
                    agent, job_id, reason=AGENT_LOST_CONVERSATION_ERROR,
                    code=code, output=output)
            else:
                status = "failed" if reason or code != 0 else "done"
                vendor = str(agent.get("vendor") or "")
                detail = agent_vendors.get(vendor).failure_detail(output) if code else ""
                error = reason or detail or (f"{vendor} exited with status {code}" if code else "")
                if status == "done":
                    self.cooldowns.pop(aid, None)
                self._result(job_id, status, code, output, error)
            del self.procs[job_id]

    def _result(self, job_id: str, status: str, code: int | None, output: str, error: str) -> None:
        try:
            self.sync._request("POST", f"jobs/{job_id}/result",
                               {"status": status, "exit_code": code, "output_tail": output, "error": error})
        except RuntimeError as exc:
            self.error = str(exc)

    def shutdown(self) -> None:
        for job_id in list(self.procs): self._terminate(job_id, "the sync worker stopped")
        deadline = time.time() + 5
        while time.time() < deadline and any(e["proc"].poll() is None for e in self.procs.values()):
            time.sleep(0.1)
        self._reap()


def _agent_context(start: Path) -> tuple[Path, dict, ProjectSync]:
    project = _project_root(start)
    binding = read_binding(project)
    account = account_for_binding(binding)
    return project, binding, ProjectSync(account, project, binding["bubble"])


def _find_agent(sync: ProjectSync, ref: str) -> dict:
    rows = sync._request("GET", "agents").get("agents", [])
    wanted = ref.strip().lower()
    for agent in rows:
        if agent.get("id") == ref or str(agent.get("name", "")).lower() == wanted:
            return agent
    raise RuntimeError(f"No agent called {ref!r} on this bubble. `{cli_name()} agent list` shows them.")


def agent_register_command(start: Path, *, name: str, role: str, goal: str, personality: str,
                           model: str, vendor: str, conversation: str) -> None:
    project, binding, sync = _agent_context(start)
    # Conversation discovery is scoped to the directory where this chat is actually open. A
    # linked worktree must not accidentally adopt a newer session from main or a sibling tree.
    vendor, conversation = detect_conversation(start.resolve(), vendor=vendor,
                                               conversation=conversation)
    agent = sync._request("POST", "agents", {
        "name": name, "role": role, "goal": goal, "personality": personality, "vendor": vendor,
        "conversation": conversation, "model": model, "worker_id": sync.worker_uid(),
        "project_label": project.name})["agent"]
    pid = _chat_pid_for(vendor)
    if pid:
        pids = {aid: p for aid, p in _read_chat_pids(project / ".lockedin").items() if _alive(int(p or 0))}
        pids[agent["id"]] = pid
        _write_chat_pids(project / ".lockedin", pids)
    heading("Registered an agent", f"{agent['name']} · {vendor}{' · ' + model if model else ''}")
    print(green("✓") + f" {bold(agent['name'])} is registered on bubble {bold(binding['bubble'])} "
          f"as {vendor} conversation {dim(conversation)}.")
    print(dim("  It appears under this directory's sync on the bubble page within a few seconds."))
    print(dim("  Marks assigned to it there run as headless turns of this conversation while the chat is closed."))
    print(dim(f"  Reopen it any time: {cli_name()} agent chat {shlex.quote(agent['name'])}"))


def agent_list_command(start: Path) -> None:
    project, binding, sync = _agent_context(start)
    rows = sync._request("GET", "agents").get("agents", [])
    heading("Agents on this bubble", f"{binding['bubble']} · {project}")
    if not rows:
        print(dim("  None yet. From inside a codex/claude/agy chat here, run:"))
        print(cyan(f"     {cli_name()} agent register --name <name> --role <role> --goal <goal>"))
        return
    mine = sync.worker_uid()
    for agent in rows:
        marker = {"working": green("●"), "idle": green("○"), "attached": orange("●"), "offline": dim("●")}.get(agent.get("status"), dim("●"))
        here = "" if agent.get("worker_id") == mine else dim(f"  (via {agent.get('project_label') or 'another directory'})")
        model = f" {dim(agent['model'])}" if agent.get("model") else ""
        print(f"  {marker} {bold(agent['name'])}  {agent.get('status', '?')}  {dim(agent.get('vendor', ''))}{model}{here}")
        if agent.get("fresh") and not agent.get("conversation"):
            print(f"    {dim('note: new conversation starts on the next job')}")
        if agent.get("role") or agent.get("goal"):
            print(f"    {dim(agent.get('role', ''))}{dim(' — ') if agent.get('role') and agent.get('goal') else ''}{dim(agent.get('goal', ''))}")
        last = agent.get("last_job")
        if last:
            print(f"    {dim('last job:')} {last['id']} {last['status']} {dim(last.get('mark_key', ''))}"
                  + (f" {red(last['error'])}" if last.get("error") else ""))


def agent_jobs_command(start: Path, *, show_all: bool) -> None:
    project = _project_root(start)
    path = project / ".lockedin" / "indexes" / "jobs.json"
    try: index = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RuntimeError("No job index in this project yet; is the sync worker running? Try `doctor`.")
    jobs = sorted(index.get("by_id", {}).values(), key=lambda j: j.get("created_at", ""))
    if not show_all:
        jobs = [j for j in jobs if j.get("status") in {"queued", "running"}]
    heading("Agent jobs", "open" if not show_all else "all recorded")
    if not jobs:
        print(dim("  Nothing queued. Assign a mark to an agent on the bubble page."))
        return
    for job in jobs:
        colour = {"queued": dim, "running": orange, "done": green, "failed": red, "cancelled": dim}.get(job.get("status"), dim)
        print(f"  {colour('●')} {bold(job['id'])}  {job.get('status')}  {dim('→')} {job.get('agent_name', '')}  {dim(job.get('mark_key', ''))}")
        if job.get("instruction"): print(f"    {dim('note:')} {job['instruction']}")
        if job.get("error"): print(f"    {red(job['error'])}")


def agent_reply_command(start: Path, job_id: str, *, text: str, file: str) -> None:
    if file:
        try: text = Path(file).read_text(encoding="utf-8")
        except OSError as exc: raise RuntimeError(f"Could not read {file}: {exc}") from exc
    if not text.strip():
        raise RuntimeError("Say what you did: --text \"...\" or --file <path>.")
    _, _, sync = _agent_context(start)
    job = sync._request("POST", f"jobs/{job_id}/reply", {"text": text})["job"]
    print(green("✓") + f" Replied to {bold(job.get('mark_key', ''))} as {bold(job.get('agent_name', ''))}; job {job_id} is done.")
    if job.get("late"):
        print(orange("•") + f" Job {job_id} had already been {job.get('late_from') or 'closed'} "
              "when this landed; the reply was posted to the mark anyway.")
        if job.get("late_error"):
            print(dim(f"  Earlier worker result: {job['late_error']}"))


def agent_fail_command(start: Path, job_id: str, *, reason: str) -> None:
    _, _, sync = _agent_context(start)
    job = sync._request("POST", f"jobs/{job_id}/fail", {"reason": reason})["job"]
    print(orange("•") + f" Job {job_id} marked failed; the reason was posted to {bold(job.get('mark_key', ''))}.")
    if job.get("late"):
        print(orange("•") + f" Job {job_id} had already been {job.get('late_from') or 'closed'} "
              "when this landed; the reason was posted to the mark anyway.")


def agent_chat_command(start: Path, ref: str) -> None:
    project, _, sync = _agent_context(start)
    agent = _find_agent(sync, ref)
    new_id = str(uuid.uuid4())
    cmd = agent_chat_command_for(agent, new_id)
    fresh = not agent.get("conversation")
    heading("Opening " + agent["name"], " ".join(shlex.quote(part) for part in cmd))
    print(dim("  While this chat is open, jobs assigned to this agent wait; they run once you leave."))
    pids = _read_chat_pids(project / ".lockedin"); started = time.time()
    proc = subprocess.Popen(cmd, cwd=str(project))
    pids[agent["id"]] = proc.pid; _write_chat_pids(project / ".lockedin", pids)
    try: proc.wait()
    finally:
        pids = _read_chat_pids(project / ".lockedin"); pids.pop(agent["id"], None)
        _write_chat_pids(project / ".lockedin", pids)
    if fresh:
        adapter = agent_vendors.get(str(agent.get("vendor") or ""))
        conversation = new_id if adapter.preassigns_conversation_id else adapter.discover_conversation(
            "", started=started, project=project)
        if conversation:
            sync._request("POST", f"agents/{agent['id']}", {"conversation": conversation, "fresh": False})
            print(green("✓") + f" {bold(agent['name'])} now lives in conversation {dim(conversation)}.")
        else:
            print(orange("•") + " Could not tell which conversation that was; the agent still has none. "
                  "Register it from inside the chat instead: `agent register`.")


def agent_chat_command_for(agent: dict, new_id: str) -> list[str]:
    return agent_chat_argv(agent, new_id=new_id)


def agent_reset_command(start: Path, ref: str) -> None:
    _, _, sync = _agent_context(start)
    agent = _find_agent(sync, ref)
    sync._request("POST", f"agents/{agent['id']}/reset", {"conversation": ""})
    print(green("✓") + f" {bold(agent['name'])} forgot its conversation. The next job — or `{cli_name()} agent chat "
          f"{shlex.quote(agent['name'])}` — starts a new one and re-introduces its role and goal.")
    if agent.get("conversation"):
        print(dim(f"  The old conversation {agent['conversation']} is still in {agent.get('vendor')}'s store; "
                  f"`agent retire --purge` would have deleted it."))


def agent_revive_command(start: Path, ref: str) -> None:
    """Resume this folder's worker and confirm the selected retained agent is still registered."""
    project, binding, sync = _agent_context(start)
    agent = _find_agent(sync, ref)
    if agent.get("worker_id") != sync.worker_uid():
        raise RuntimeError(
            f"{agent['name']} belongs to {agent.get('project_label') or 'another project folder'}. "
            "Run this command from that folder instead.")
    resync_command(project)
    print(green("✓") + f" {bold(agent['name'])} is ready on bubble {bold(binding['bubble'])}; "
          "its personality and conversation were preserved.")


def _purge_conversation(vendor: str, conversation: str) -> list[str]:
    if not conversation: return []
    try: return agent_vendors.get(vendor).purge_conversation(conversation, binary=_vendor_binary)
    except RuntimeError: return []


def agent_retire_command(start: Path, ref: str, *, purge: bool) -> None:
    _, _, sync = _agent_context(start)
    agent = _find_agent(sync, ref)
    sync._request("DELETE", f"agents/{agent['id']}")
    print(green("✓") + f" Retired {bold(agent['name'])}; its open jobs were cancelled.")
    if purge:
        removed = _purge_conversation(str(agent.get("vendor") or ""), str(agent.get("conversation") or ""))
        if removed:
            print(green("✓") + " Deleted the conversation from " + agent.get("vendor", "") + "'s store:")
            for item in removed: print(dim("    " + item))
        else:
            print(dim("  No stored conversation was found to delete."))
    elif agent.get("conversation"):
        print(dim(f"  The conversation itself is kept; `agent retire --purge` deletes it too."))


def agent_command(args) -> None:
    start = Path.cwd()
    if args.agent_command == "register":
        agent_register_command(start, name=args.name, role=args.role, goal=args.goal,
                               personality=args.personality, model=args.model, vendor=args.vendor,
                               conversation=args.conversation)
    elif args.agent_command == "list": agent_list_command(start)
    elif args.agent_command == "jobs": agent_jobs_command(start, show_all=args.show_all)
    elif args.agent_command == "reply": agent_reply_command(start, args.job, text=args.text, file=args.file)
    elif args.agent_command == "fail": agent_fail_command(start, args.job, reason=args.reason)
    elif args.agent_command == "chat": agent_chat_command(start, args.agent)
    elif args.agent_command == "revive": agent_revive_command(start, args.agent)
    elif args.agent_command == "reset": agent_reset_command(start, args.agent)
    elif args.agent_command == "retire": agent_retire_command(start, args.agent, purge=args.purge)


def _alive(pid: int) -> bool:
    if pid <= 0: return False
    if os.name == "nt":
        # Use a correctly typed Win32 HANDLE. ctypes defaults function results to a 32-bit int;
        # on 64-bit Windows that can truncate OpenProcess handles and falsely report a live
        # worker dead. SYNCHRONIZE is the minimum access needed for a zero-timeout wait.
        try:
            import ctypes
            from ctypes import wintypes

            SYNCHRONIZE = 0x00100000
            WAIT_OBJECT_0 = 0
            WAIT_TIMEOUT = 0x102
            ERROR_ACCESS_DENIED = 5

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not handle:
                # Access denied still proves a process owns the PID. Any other failure means the
                # PID is absent (or cannot be determined), and the caller may use a Popen handle.
                return ctypes.get_last_error() == ERROR_ACCESS_DENIED
            try:
                result = kernel32.WaitForSingleObject(handle, 0)
                if result == WAIT_TIMEOUT:
                    return True
                if result == WAIT_OBJECT_0:
                    return False
                return True  # Unknown wait result: do not kill or replace a possibly live worker.
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True  # A probe failure is not evidence that the process died.
    try: os.kill(pid, 0); return True
    except OSError: return False


def _worker_record(worker_id: str) -> dict | None: return load_workers().get("workers", {}).get(worker_id)


def _update_worker(worker_id: str, **changes) -> None:
    data = load_workers(); rec = data.setdefault("workers", {}).get(worker_id)
    if rec is None: return
    rec.update(changes); save_workers(data)


def _run_worker(worker_id: str, project: str) -> None:
    rec = _worker_record(worker_id)
    if not rec: return
    binding = ProjectSync({"server": rec.get("server", ""), "user": rec.get("user", "")}, Path(project), rec["bubble"])._binding()
    if not binding: _update_worker(worker_id, status="failed", error="missing .lockedin binding"); return
    account = next((item for item in load_config().get("accounts", [])
                    if item.get("server") == binding.get("server") and item.get("user") == binding.get("user")), None)
    if not account:
        _update_worker(worker_id, status="failed", error="the account for this project is no longer authorized")
        return
    account = dict(account); account["workspace_id"] = binding["workspace_id"]
    sync = ProjectSync(account, Path(project), binding["bubble"])
    runner = AgentRunner(sync, worker_id, rec.get("cli") or APP)
    stop = False
    def end(*_):
        nonlocal stop; stop = True
    signal.signal(signal.SIGTERM, end); signal.signal(signal.SIGINT, end)
    _update_worker(worker_id, status="running", pid=os.getpid(), last_error="",
                   client_version=SCIENTIST_CLIENT_VERSION)
    while not stop:
        try:
            sync.sync_once()
            # Figures the sync cannot carry are a real (silent) loss of an agent's work, so they
            # degrade the worker rather than being logged and forgotten. Name-style warnings are
            # advisory and ride along without changing the status.
            warnings = sync.figure_warnings()
            blocking = warnings[0] if sync.unsynced_figures() else ""
            _update_worker(worker_id, status="degraded" if blocking else "running",
                           last_sync=time.time(), last_error=blocking, warnings=warnings)
            sync.report = {"status": "degraded" if blocking else "running", "error": blocking}
        except SecureModeStop as exc:
            # This process itself is the remote execution path. End it permanently; turning the
            # web switch back off cannot restart anything on this machine. Resuming requires the
            # local `resync` command, so a stolen browser session cannot undo the stop remotely.
            runner.shutdown()
            _update_worker(worker_id, status="stopped", stopped_at=time.time(),
                           last_error=str(exc), agent_error=str(exc), jobs=[])
            return
        except Exception as exc:
            # Enabling secure mode atomically revokes every Scientist bearer token. The next poll
            # therefore reaches this path instead of receiving a manifest stop flag. Treat an
            # explicit deauthorization as terminal; a dead worker cannot be revived from the web.
            if "server returned 401" in str(exc):
                runner.shutdown()
                message = "Scientist authorization was revoked; authorize and resync locally to resume."
                _update_worker(worker_id, status="stopped", stopped_at=time.time(),
                               last_error=message, agent_error=message, jobs=[])
                return
            _update_worker(worker_id, status="degraded", last_error=str(exc))
            sync.report = {"status": "degraded", "error": str(exc)}
        # Agents ride on the same cycle: one heartbeat when this directory owns any, nothing when
        # it owns none. A failure here is reported beside the sync status, never in place of it.
        try:
            runner.tick()
            agent_error = runner.error
        except Exception as exc:
            agent_error = str(exc)
        _update_worker(worker_id, jobs=runner.running_job_ids(), agent_error=agent_error)
        for _ in range(POLL_SECONDS * 10):
            if stop: break
            time.sleep(.1)
    runner.shutdown()
    # The parting synchronization doubles as a shutdown notice, so the server's monitor shows the
    # worker as stopped straight away instead of waiting for it to time out.
    sync.report = {"status": "stopped", "error": ""}
    try: sync.sync_once()
    except Exception: pass
    _update_worker(worker_id, status="stopped", stopped_at=time.time())


def _worker_launch_kwargs(stream) -> dict:
    """Detach a long-lived worker from the shell that happened to install or resume it."""
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": stream, "stderr": stream}
    if os.name == "nt":
        # A PowerShell setup link runs through nested scripts. Without a detached process, its
        # console lifetime can take the worker with it as soon as setup returns.
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS |
                                   subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _await_worker_start(worker_id: str, proc, log: Path, *, timeout: float = 5.0) -> None:
    """Do not claim success until the child has entered its worker loop and stayed alive."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rec = _worker_record(worker_id) or {}
        status = str(rec.get("status") or "starting")
        # Popen.poll uses the original Windows process handle, so it is authoritative during
        # startup. The separate PID probe is for later commands running in another process.
        exit_code = proc.poll()
        alive = exit_code is None
        if status in {"running", "degraded"} and alive:
            time.sleep(0.15)
            if proc.poll() is None:
                return
        if status in {"failed", "stopped"} or not alive:
            detail = str(rec.get("last_error") or rec.get("error") or
                         f"worker process exited during startup (code {exit_code})")
            if alive:
                try: proc.terminate()
                except OSError: pass
            _update_worker(worker_id, status="failed", error=detail, last_error=detail,
                           stopped_at=time.time())
            raise RuntimeError(f"Scientist worker did not stay running: {detail}. Log: {log}")
        time.sleep(0.05)
    detail = f"worker did not report ready within {timeout:g} seconds"
    try: proc.terminate()
    except OSError: pass
    _update_worker(worker_id, status="failed", error=detail, last_error=detail,
                   stopped_at=time.time())
    raise RuntimeError(f"Scientist worker did not start: {detail}. Log: {log}")


def start_sync(account: dict, bubble: str, project: Path, *, announce: bool = True,
               recovered_identity: dict | None = None) -> None:
    # `announce=False` is for callers that already printed their own heading (resync).
    if announce: heading("Synchronizing a bubble", f"{bubble} → {project / '.lockedin'}")
    sync = ProjectSync(account, project, bubble)
    sync.validate_or_initialize()
    if recovered_identity and not sync.identity_path.exists():
        _atomic_json(sync.identity_path, recovered_identity, private=True)
    sync.sync_once()
    data = load_workers()
    for wid, rec in data.get("workers", {}).items():
        if Path(rec.get("project", "")).resolve() == project.resolve() and _alive(int(rec.get("pid", 0))):
            if rec.get("bubble") == bubble:
                print(green("✓") + f" Already synchronized by worker {bold(wid)}")
                print(dim("  Use lockedin-scientist ps to inspect it."))
                return
            raise RuntimeError("Another bubble worker already manages this project. Use hard-reset first.")
    wid = secrets.token_hex(6); log = data_root() / "runtime" / "workers" / f"{wid}.log"; log.parent.mkdir(parents=True, exist_ok=True)
    rec = {"id": wid, "pid": 0, "project": str(project.resolve()), "server": account["server"], "user": account["user"], "workspace_id": account.get("workspace_id", ""),
           "bubble": bubble, "started_at": time.time(), "last_sync": time.time(), "last_error": "", "status": "starting",
           "client_version": SCIENTIST_CLIENT_VERSION,
           # How this client is invoked here, so a headless agent turn is told the right command.
           "cli": cli_name()}
    data.setdefault("workers", {})[wid] = rec; save_workers(data)
    try:
        with log.open("ab") as stream:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "_worker", wid, str(project.resolve())],
                **_worker_launch_kwargs(stream))
    except OSError as exc:
        detail = f"could not launch worker: {exc}"
        _update_worker(wid, status="failed", error=detail, last_error=detail,
                       stopped_at=time.time())
        raise RuntimeError(f"{detail}. Log: {log}") from exc
    _update_worker(wid, pid=proc.pid, log=str(log))
    _await_worker_start(wid, proc, log)
    print(green("✓") + f" Synced {bold(bubble)}; worker {bold(wid)} is running.")
    print(dim("  Reports sync every five seconds. Run your agent normally from this project."))


def ps_command() -> None:
    data = load_workers()
    heading("Scientist sync workers", "Workers keep their project-local .lockedin directory synchronized.")
    records = list(data.get("workers", {}).values())
    if not records:
        print(dim("  No managed workers on this device."))
    for rec in records:
        if rec.get("status") in {"running", "starting", "degraded"} and not _alive(int(rec.get("pid", 0))):
            previous = rec.get("status", "unknown")
            rec["status"] = "stopped"; rec["stopped_at"] = time.time()
            if not rec.get("last_error"):
                suffix = f" See {rec['log']}." if rec.get("log") else ""
                rec["last_error"] = f"worker process exited unexpectedly while {previous}.{suffix}"
    records.sort(key=lambda rec: (0 if rec.get("status") in ATTENTION_WORKER_STATUSES else
                                  1 if rec.get("status") not in TERMINAL_WORKER_STATUSES else 2,
                                  -rec.get("started_at", 0)))
    for rec in records:
        status = rec.get("status", "?")
        marker = (green("●") if status == "running" else orange("●") if status == "degraded"
                  else red("●") if status == "failed" else dim("●"))
        print(f"  {marker} {bold(rec['id'])}  {status}  {dim('bubble:')} {rec.get('bubble', '')}")
        print(f"    {dim(rec.get('project', ''))}")
        if rec.get("last_error"): print("    " + red("error: ") + rec["last_error"])
        if rec.get("jobs"): print("    " + cyan("agent turns running: ") + ", ".join(rec["jobs"]))
        if rec.get("agent_error"): print("    " + orange("agents: ") + rec["agent_error"])
        for warning in rec.get("warnings", []) or []:
            if warning != rec.get("last_error"):
                print("    " + orange("warning: ") + warning)
        if status == "failed" and rec.get("error") == "missing .lockedin binding":
            print("    " + orange("recovery: ") +
                  f"copy any unsynced report work, then run `lockedin-scientist hard-reset {rec.get('bubble', '<bubble>')}` from this project")
    # Persist stale-record cleanup and bounded-history pruning even when no live status changed.
    save_workers(data)


def stop_command(worker_id: str) -> None:
    data = load_workers(); rec = data.get("workers", {}).get(worker_id)
    if not rec: raise RuntimeError("No such Scientist worker.")
    pid = int(rec.get("pid", 0))
    alive = _alive(pid)
    if alive:
        try: os.kill(pid, signal.SIGTERM)
        except OSError as exc: raise RuntimeError(f"Could not stop worker: {exc}") from exc
    rec["status"] = "stopping" if alive else "stopped"
    rec["stopped_at"] = time.time(); save_workers(data)
    heading("Stopping sync worker")
    print(green("✓") + f" Stop requested for worker {bold(worker_id)}.")
    print(dim("  .lockedin was left unchanged."))


def read_binding(project: Path) -> dict:
    """This project's bubble binding, validated.

    `.lockedin` records its own server, user, workspace and bubble, so a project never has to
    depend on — or agree with — the device-global workspace that `workspaces switch` selects.
    """
    path = project.resolve() / ".lockedin" / "config" / "binding.json"
    try:
        binding = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("No valid .lockedin/config/binding.json in this project. Run `lockedin-scientist sync <bubble>` first.") from exc
    if any(not binding.get(key) for key in BINDING_KEYS):
        raise RuntimeError("The .lockedin binding is incomplete. Run `lockedin-scientist hard-reset <bubble>` to rebuild it.")
    return binding


def account_for_binding(binding: dict) -> dict:
    """The authorized account for a binding, pinned to the workspace the binding names.

    Pinning rather than reading the profile's active workspace is what lets a project be repaired
    from any directory in any order; it is the same override the running worker applies.
    """
    account = next((item for item in load_config().get("accounts", [])
                    if item.get("server") == binding["server"] and item.get("user") == binding["user"]), None)
    if not account:
        raise RuntimeError("The account for this project is no longer authorized. "
                           f"Run `lockedin-scientist login --server {binding['server']}` again.")
    account = dict(account); account["workspace_id"] = binding["workspace_id"]
    return account


def _project_worker(project: Path, *, binding: dict | None = None) -> dict | None:
    """The newest worker for this project, optionally restricted to its exact binding."""
    project = project.resolve()
    records = [rec for rec in load_workers().get("workers", {}).values()
               if Path(rec.get("project", "")).resolve() == project]
    if binding is not None:
        records = [rec for rec in records if all(rec.get(key) == binding.get(key) for key in BINDING_KEYS)]
    return max(records, key=lambda rec: rec.get("started_at", 0)) if records else None


def _worker_is_healthy(rec: dict) -> bool:
    """Running, and having completed a cycle recently — the verdict `doctor` reports."""
    return (rec.get("status") == "running"
            and time.time() - float(rec.get("last_sync", 0) or 0) <= WORKER_STALE_SECONDS)


def doctor_command(project: Path) -> None:
    """Check that this project is bound to a live, current, reachable Scientist worker."""
    root = project.resolve() / ".lockedin"
    binding = read_binding(project)
    project = project.resolve()
    project_matches = [rec for rec in load_workers().get("workers", {}).values()
                       if Path(rec.get("project", "")).resolve() == project]
    matches = [rec for rec in project_matches
               if all(rec.get(key) == binding.get(key) for key in BINDING_KEYS)]
    heading("Scientist doctor", str(root))
    if not matches and project_matches:
        raise RuntimeError("The assigned worker does not match this .lockedin binding. Run "
                           "`lockedin-scientist resync` to replace it safely.")
    if not matches:
        raise RuntimeError(f"No worker is assigned to this project. Run `lockedin-scientist resync` from {project}.")
    rec = max(matches, key=lambda item: item.get("started_at", 0))
    status, pid = rec.get("status", "?"), int(rec.get("pid", 0))
    if status != "running" or not _alive(pid):
        detail = rec.get("last_error") or rec.get("error") or status
        raise RuntimeError(f"Worker {rec.get('id', '?')} is not healthy ({detail}). Run `lockedin-scientist ps` for details, then `lockedin-scientist resync` to resume this project.")
    if not _worker_is_healthy(rec):
        raise RuntimeError(f"Worker {rec.get('id', '?')} has not completed a sync recently. Run `lockedin-scientist ps` and repair it before relying on report submission.")
    account = account_for_binding(binding)
    account_request(account, "GET", f"/api/scientist/v2/bubbles/{binding['bubble']}/manifest")
    print(green("✓") + f" Worker {bold(rec['id'])} is healthy and can reach bubble {bold(binding['bubble'])}.")
    mode = confinement_mode()
    print(dim(f"  Agent turns run under confinement: {mode}."))
    if mode == "none":
        print(orange("•") + " Agents run unconfined on this machine: nothing stops a headless turn "
              "from writing outside .lockedin, and as a result bash is unavailable to claude and agy "
              "here. Set LOCKEDIN_AGENT_CONFINEMENT=trust to lift that at your own risk, for example "
              "if this worker already runs as a dedicated user or inside its own VM.")


VENDOR_INVOCATION = {vendor: agent_vendors.get(vendor).invocation for vendor in VENDORS}


def _connect_account(server: str, workspace_id: str, ticket: str) -> dict:
    """Authorize this computer for ``server``, preferring the ticket the web page already signed.

    A setup ticket is freshly minted by the page and is therefore a better credential than a
    cached account, which may have been revoked or may belong to an earlier local server.  Spend
    it even when the profile already has this server, but retain that account's selected workspace
    so connecting one project never retargets the user's other projects.  When no ticket was
    supplied an existing account remains the inexpensive normal path.
    """
    existing = next((item for item in load_config().get("accounts", [])
                     if item.get("server") == server), None)
    if ticket:
        try:
            granted = request(server, "GET", f"/api/scientist/v2/setup/{ticket}")
        except RuntimeError as exc:
            # Expired, already spent, or the server restarted. A usable cached account avoids
            # an unnecessary browser detour; otherwise the browser flow still works.
            print(orange("•") + f" That setup link could not be used ({exc}).")
            if existing:
                print(green("✓") + f" Already authorized as {bold(existing['user'])} on {dim(server)}")
            else:
                login(server)
        else:
            cfg = load_config(); accounts = cfg.setdefault("accounts", [])
            prior = next((item for item in accounts
                          if item.get("server") == server and item.get("user") == granted["user"]), None)
            accounts[:] = [a for a in accounts
                           if not (a.get("server") == server and a.get("user") == granted["user"])]
            accounts.append({"server": server, "user": granted["user"], "token": granted["token"],
                             "workspace_id": (prior or {}).get("workspace_id", granted.get("workspace_id", ""))})
            save_config(cfg)
            print(green("✓") + f" Refreshed authorization for {bold(granted['user'])} on {dim(server)}"
                  + dim(" (no browser needed)"))
    elif existing:
        print(green("✓") + f" Already authorized as {bold(existing['user'])} on {dim(server)}")
    else:
        login(server)
    account = next((item for item in load_config().get("accounts", [])
                    if item.get("server") == server), None)
    if not account:
        raise RuntimeError(f"Authorization did not complete. Run `lockedin-scientist login --server {server}`.")
    # Pinned locally, never through `workspaces switch`: the profile's active workspace is shared by
    # every project on this device, so connecting one project must not retarget the others.
    account = dict(account)
    account["workspace_id"] = workspace_id or account.get("workspace_id", "")
    return account


def _ask_project(supplied: str) -> Path:
    """Where the project lives. Asks, unless a path was given or there is nobody to ask."""
    if supplied:
        project = Path(supplied).expanduser()
    else:
        if not sys.stdin.isatty():
            raise RuntimeError("No terminal to ask which folder to use. Re-run with `--project <path>`.")
        default = Path.cwd()
        print()
        print("  " + bold("Which project folder should this bubble sync into?"))
        print("  " + dim(f"Press Enter for {default}"))
        answer = input("  " + cyan("path: ")).strip()
        project = Path(answer).expanduser() if answer else default
    project = project.expanduser()
    if not project.exists():
        project.mkdir(parents=True, exist_ok=True)
        print(green("✓") + f" Created {dim(str(project.resolve()))}")
    if not project.is_dir():
        raise RuntimeError(f"{project} is not a directory. Re-run with `--project <path>`.")
    project = project.resolve()
    top = _git_toplevel(project)
    if top is not None and top != project:
        print(dim(f"  Using this checkout's project root: {top}"))
        project = top
    return project


def _install_detected_skills() -> list[str]:
    """Install the native skill for every agent actually present on this machine.

    A missing agent is skipped rather than installed for: the closing message should name only
    commands the user can really run. A vendor that refuses (a user-owned skill file) or fails
    (agy's plugin import) is a warning, never fatal — one uncooperative agent must not undo an
    otherwise complete setup.
    """
    installed: list[str] = []
    for vendor in VENDORS:
        if not shutil.which(vendor):
            print(dim(f"  • {vendor} is not installed here — skipped."))
            continue
        try:
            setup_vendor_skill(vendor)
        except RuntimeError as exc:
            print(orange("•") + f" {vendor}: {exc}")
            continue
        installed.append(vendor)
        print(green("✓") + f" {vendor} skill installed")
    return installed


def _preserve_unbound_root(project: Path) -> tuple[Path, dict | None]:
    """Move a partial, unidentifiable .lockedin aside without discarding any local work."""
    root = project / ".lockedin"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = project.parent / f"{project.name}.lockedin-recovery-{stamp}"
    if backup.exists():
        backup = project.parent / f"{project.name}.lockedin-recovery-{stamp}-{secrets.token_hex(2)}"
    identity = None
    try:
        candidate = json.loads((root / "config" / "identity.json").read_text(encoding="utf-8"))
        if re.fullmatch(r"[0-9a-f]{16}", str(candidate.get("worker_uid") or "")):
            identity = {"worker_uid": candidate["worker_uid"]}
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        pass
    for wid, rec in load_workers().get("workers", {}).items():
        if Path(rec.get("project", "")).resolve() == project.resolve() and _alive(int(rec.get("pid", 0))):
            _stop_and_wait(wid)
    try:
        root.rename(backup)
    except OSError as exc:
        raise RuntimeError(f"Could not preserve the incomplete {root}: {exc}") from exc
    print(orange("•") + " The existing .lockedin was incomplete; preserved it without uploading at")
    print("    " + dim(str(backup)))
    return backup, identity


def connect_command(server: str, workspace_id: str, bubble: str, *,
                    ticket: str = "", project_path: str = "") -> None:
    """Do everything a fresh machine needs to work on one bubble with an agent.

    Authorize, bind a folder to the bubble, start its sync worker, install the skills for whatever
    agents are here, and finish by naming the command to run. Every step is idempotent, so running
    the same link twice is a no-op that reports what already exists.
    """
    server = server.rstrip("/")
    heading("Connecting a project to LockedIn", f"bubble {bubble} on {server}")
    account = _connect_account(server, workspace_id, ticket)
    # One cheap probe so a wrong workspace or a revoked token fails here, with a clear message,
    # rather than somewhere inside the first sync.
    try:
        account_request(account, "GET", f"/api/scientist/v2/bubbles/{bubble}/manifest")
    except RuntimeError as exc:
        raise RuntimeError(f"Could not reach bubble {bubble} on {server}: {exc}") from exc
    print(green("✓") + f" Reached bubble {bold(bubble)}")

    project = _ask_project(project_path)
    binding_path = project / ".lockedin" / "config" / "binding.json"
    if binding_path.exists():
        binding = read_binding(project)
        wanted = {"server": server, "user": account["user"],
                  "workspace_id": account.get("workspace_id", ""), "bubble": bubble}
        if binding != wanted:
            raise RuntimeError(
                f"{project / '.lockedin'} is already bound to bubble {binding['bubble']}. "
                f"Pick another folder, or run `lockedin-scientist hard-reset {bubble}` there to replace it.")
        resync_command(project)
    else:
        recovered_identity = None
        if (project / ".lockedin").exists():
            _, recovered_identity = _preserve_unbound_root(project)
        start_sync(account, bubble, project, recovered_identity=recovered_identity)

    heading("Agent skills", "Installed for the agents found on this computer.")
    installed = _install_detected_skills()

    heading("Ready", str(project))
    if installed:
        print("  " + dim("From this folder:"))
        for vendor in installed:
            print(f"  {cyan('•')} {VENDOR_INVOCATION[vendor]}")
    else:
        print("  " + dim("No agent CLI was found here. Install codex, claude, or agy, then run"))
        print("  " + cyan("lockedin-scientist codex setup") + dim("  (or claude / agy)"))
    print()
    print("  " + dim("Verify at any time with ") + cyan("lockedin-scientist doctor"))


def _stop_and_wait(worker_id: str) -> None:
    """Stop a worker and wait for it to actually exit, before replacing it."""
    stop_command(worker_id)
    for _ in range(30):
        rec = _worker_record(worker_id)
        if not rec or not _alive(int(rec.get("pid", 0))):
            return
        time.sleep(0.1)
    raise RuntimeError("Scientist worker did not stop in time; stop it manually, then retry.")


def _size_label(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


# Same reasoning as the browser dialog: comfortably under the 100 MB body cap a proxy in front
# of the server imposes, and big enough that a multi-gigabyte file is not thousands of requests.
# Kept equal to scientist_sync.NOT_SYNCED_PATH; the installed client cannot import it.
NOT_SYNCED_PATH = "reports/assets/NOT-SYNCED.md"

PUSH_CHUNK_BYTES = 32 * 1024 * 1024

# One request must stay well under what a proxy in front of the server will carry (Cloudflare
# stops at 100 MB). Files are base64 in these bodies, so the ceiling is on *raw* bytes with room
# for the ~4/3 expansion and the JSON around it. Three hundred ordinary figures are individually
# tiny and collectively far past any such limit, so batches are bounded by bytes, not by count.
REQUEST_PAYLOAD_BYTES = 24 * 1024 * 1024


def _batched_by_size(items, size_of, limit: int = REQUEST_PAYLOAD_BYTES):
    """Group items so each batch stays under ``limit`` raw bytes. Never yields an empty batch,
    so a single item larger than the limit still goes out alone rather than being dropped."""
    batch, used = [], 0
    for item in items:
        size = max(0, int(size_of(item)))
        if batch and used + size > limit:
            yield batch
            batch, used = [], 0
        batch.append(item)
        used += size
    if batch:
        yield batch


def _push_large_asset(account: dict, bubble: str, rel: str, path: Path, *, live: bool) -> str:
    """Send one oversized asset in slices, resuming whatever the server already holds."""
    base = f"/api/scientist/v2/bubbles/{bubble}/large-asset/push"
    total = path.stat().st_size
    begun = account_request(account, "POST", base + "/begin",
                            {"filename": path.name, "total_size": total})
    upload_id = begun["upload_id"]
    offset = min(int(begun.get("received") or 0), total)
    if offset and live:
        print(dim(f"  resuming at {_size_label(offset)} of {_size_label(total)}"))
    shown = [-1]
    with path.open("rb") as fh:
        while offset < total:
            fh.seek(offset)
            block = fh.read(PUSH_CHUNK_BYTES)
            if not block:
                break
            # A dropped connection mid-file is the normal failure over a tunnel. The server
            # checks the offset, so a resend can never staple a slice into the wrong place.
            for attempt in range(1, 6):
                try:
                    offset = int(upload_request(
                        account, f"{base}/{upload_id}?offset={offset}", block)["received"])
                    break
                except RuntimeError as exc:
                    # A 4xx is the server refusing this request, not the network dropping it;
                    # resending it would only fail the same way.
                    if attempt == 5 or "server returned 4" in str(exc):
                        raise
                    time.sleep(min(8.0, 0.5 * 2 ** (attempt - 1)))
            pct = int(offset * 100 / total) if total else 100
            if live and pct != shown[0] and pct % 5 == 0:
                shown[0] = pct
                print(f"\r  {path.name}  {pct:3d}%  "
                      f"{_size_label(offset)} of {_size_label(total)}", end="", flush=True)
    done = account_request(account, "POST", f"{base}/{upload_id}/finish")
    return done.get("path") or rel


def assets_command(project: Path, pull: list[str], pull_all: bool = False,
                   push: list[str] | None = None, push_all: bool = False,
                   remove: list[str] | None = None, remove_all: bool = False,
                   assume_yes: bool = False, force: bool = False) -> None:
    """List, fetch, or send the assets a sync deliberately leaves alone.

    Big binaries (photo archives, datasets, model checkpoints) are listed by the manifest but
    never content-synced in either direction: hashing one on every poll costs more than the whole
    rest of the bubble, and no agent reads a zip anyway. This is how you move one on purpose.
    """
    push = push or []
    remove = remove or []
    project = project.resolve()
    binding = read_binding(project)
    account = account_for_binding(binding)
    bubble = binding["bubble"]
    syncer = ProjectSync(account, project, bubble)
    live = sys.stdout.isatty()
    response = account_request(account, "GET",
                               f"/api/scientist/v2/bubbles/{bubble}/large-assets")
    remote = response.get("assets", [])
    cap = int(response.get("threshold") or 0)
    remote_by_name = {Path(item["path"]).name: item for item in remote}

    # Local files over the cap: the other half of the picture. An agent that generated a dataset
    # has one of these and no way to know the sync will not carry it.
    local_dir = syncer.root / "reports" / "assets"
    local: dict[str, Path] = {}
    if local_dir.is_dir():
        for path in sorted(local_dir.iterdir()):
            # ``.part`` is download scratch, not an asset. It is written elsewhere now, but a
            # leftover from before must not be swept up by ``push --all`` the way one already was.
            if path.is_file() and cap and path.stat().st_size > cap \
                    and path.suffix != ".part":
                local[path.name] = path

    def matches(name: str, chosen: list[str]) -> bool:
        return name in chosen or f"reports/assets/{name}" in chosen

    if remove or remove_all:
        chosen = remote if remove_all else [remote_by_name[Path(n).name] for n in remove
                                            if Path(n).name in remote_by_name]
        unknown = [n for n in remove if Path(n).name not in remote_by_name]
        if unknown:
            raise RuntimeError("Not a large asset on the server: " + ", ".join(unknown))
        if not chosen:
            heading("Deleting large assets", bubble)
            print(dim("  Nothing to delete."))
            return
        # A referenced file is a figure some page draws; removing it breaks that page rather
        # than reclaiming space nobody wanted.
        used = [i for i in chosen if not i.get("unused", True)]
        if used and not force:
            raise RuntimeError(
                "These are referenced by a page, so deleting them would break it:\n  "
                + "\n  ".join(Path(i["path"]).name for i in used)
                + "\n  Re-run with --force if you mean it.")
        heading("Deleting large assets", f"{bubble} \u2014 this cannot be undone")
        for item in chosen:
            print(f"  {bold(Path(item['path']).name):<46} {_size_label(item['size']):>10}"
                  + ("" if item.get("unused", True) else orange("  referenced by a page")))
        freed = sum(i["size"] for i in chosen)
        print(dim(f"  {len(chosen)} file(s), {_size_label(freed)} \u2014 removed from the bubble; "
                  "any local copy is left alone."))
        if not assume_yes:
            if not sys.stdin.isatty():
                raise RuntimeError("Refusing to delete without confirmation. "
                                   "Re-run with --yes if you mean it.")
            if input("  Type the bubble name to confirm: ").strip() != bubble:
                print(dim("  Nothing deleted."))
                return
        done = account_request(
            account, "POST", f"/api/scientist/v2/bubbles/{bubble}/large-asset/delete",
            {"paths": [i["path"] for i in chosen]})
        for rel in done.get("deleted", []):
            print(green("\u2713") + f" deleted {bold(Path(rel).name)}")
        for rel in done.get("missing", []):
            print(orange("\u2022") + f" already gone: {Path(rel).name}")
        return

    if push or push_all:
        names = sorted(local) if push_all else [n for n in local if matches(n, push)]
        unknown = [n for n in push if n not in local and Path(n).name not in local]
        if unknown:
            raise RuntimeError(
                "No local file over " + _size_label(cap) + " named: " + ", ".join(unknown)
                + f"\n  Large files are read from {local_dir}")
        if not names:
            heading("Pushing large assets", bubble)
            print(dim(f"  Nothing local is over {_size_label(cap)}; ordinary sync carries the rest."))
            return
        heading("Pushing large assets", bubble)
        for name in names:
            path = local[name]
            known = remote_by_name.get(name)
            if known and known["size"] == path.stat().st_size:
                print(green("✓") + f" {bold(name)} " + dim("already on the server, unchanged"))
                continue
            rel = _push_large_asset(account, bubble, f"reports/assets/{name}", path, live=live)
            print(f"\r{green('✓')} {bold(name)}  {_size_label(path.stat().st_size)} → "
                  + dim(rel) + (" " * 20 if live else ""))
        return

    if pull or pull_all:
        unknown = [n for n in pull if Path(n).name not in remote_by_name]
        if unknown:
            raise RuntimeError("Not a large asset in this bubble: " + ", ".join(unknown))
        wanted = remote if pull_all else [remote_by_name[Path(n).name] for n in pull]
        heading("Fetching large assets", bubble)
        for item in wanted:
            dest = syncer._local(item["path"])
            if dest.exists() and dest.stat().st_size == item["size"]:
                print(green("\u2713") + f" {bold(Path(item['path']).name)} "
                      + dim(f"already here ({_size_label(item['size'])})"))
                continue
            shown = [-1]

            def progress(done: int, total: int, _shown=shown, _size=item["size"],
                         _name=Path(item["path"]).name) -> None:
                target = total or _size
                pct = int(done * 100 / target) if target else 0
                if pct != _shown[0] and pct % 5 == 0:
                    _shown[0] = pct
                    print(f"\r  {_name}  {pct:3d}%  "
                          f"{_size_label(done)} of {_size_label(target)}", end="", flush=True)

            written = download_request(
                account, f"/api/scientist/v2/bubbles/{bubble}/large-asset",
                item["path"], dest, on_progress=progress if live else None,
                scratch=syncer.root / ".partial")
            print(f"\r{green('\u2713')} {bold(Path(item['path']).name)}  "
                  f"{_size_label(written)} \u2192 {dim(str(dest.relative_to(project)))}"
                  + (" " * 20 if live else ""))
        return

    # --- the listing ---
    heading("Large assets", f"{bubble} \u2014 moved on request, never by the sync")
    if not remote and not local:
        print(dim(f"  None. Anything over {_size_label(cap)} would appear here; "
                  "everything else syncs normally."))
        return
    names = sorted(set(remote_by_name) | set(local))
    for name in names:
        item, path = remote_by_name.get(name), local.get(name)
        size = item["size"] if item else path.stat().st_size
        if item and path and path.stat().st_size == item["size"]:
            where = green("\u2713 in sync")
        elif item and path:
            where = orange("differs \u2014 pull or push")
        elif item:
            where = dim("on server \u2014 pull")
        else:
            where = orange("local only \u2014 push")
        print(f"  {bold(name):<44} {_size_label(size):>10}  {where}")
    print()
    print(dim(f"  Anything over {_size_label(cap)} is listed but never transferred by the sync:"))
    print(dim("  re-hashing it on every poll would cost more than the rest of the bubble combined."))
    print("  Get one:   " + bold("lockedin-scientist assets pull <name>") + dim("   (or --all)"))
    print("  Send one:  " + bold("lockedin-scientist assets push <name>") + dim("   (or --all)"))
    print("  Delete:    " + bold("lockedin-scientist assets rm <name>") + dim("     (or --all)"))


def resync_command(project: Path) -> None:
    """Resume the bubble this project is already bound to.

    A worker dies for ordinary reasons — the machine slept, the server blipped, someone ran
    `stop` — and resuming it should not require remembering the bubble slug or first switching
    the device-global workspace back. `.lockedin` already knows both, so this reads the binding
    and never consults or changes the profile's active workspace. Unlike `hard-reset` it keeps
    the directory intact, including the `worker_uid` that identifies it on the bubble page.
    """
    project = project.resolve()
    binding = read_binding(project)
    account = account_for_binding(binding)
    bubble = binding["bubble"]
    heading("Resuming this project’s bubble", f"{bubble} → {project / '.lockedin'}")
    records = [rec for rec in load_workers().get("workers", {}).values()
               if Path(rec.get("project", "")).resolve() == project]
    for stale in records:
        if (_alive(int(stale.get("pid", 0)))
                and any(stale.get(key) != binding.get(key) for key in BINDING_KEYS)):
            print(orange("•") + f" Worker {bold(stale['id'])} belongs to an older project binding; replacing it.")
            _stop_and_wait(stale["id"])
    rec = _project_worker(project, binding=binding)
    if rec and _alive(int(rec.get("pid", 0))):
        if rec.get("bubble") != bubble:
            raise RuntimeError("Another bubble worker already manages this project. Use hard-reset first.")
        if _worker_is_healthy(rec):
            print(green("✓") + f" Worker {bold(rec['id'])} is already syncing {bold(bubble)}.")
            print(dim("  Run lockedin-scientist doctor to verify it."))
            return
        # Alive but wedged: replacing it is the repair this command exists for.
        print(orange("•") + f" Worker {bold(rec['id'])} is {rec.get('status', 'unresponsive')}; replacing it.")
        _stop_and_wait(rec["id"])
    start_sync(account, bubble, project, announce=False)


def _restart_upgraded_worker(worker_id: str, *, wait_for_jobs: bool = False) -> str:
    """Replace one pre-upgrade worker while preserving deliberate stops and in-flight turns."""
    if wait_for_jobs:
        while True:
            rec = _worker_record(worker_id)
            if not rec or rec.get("status") == "stopped": return "stopped"
            if not rec.get("jobs") or not _alive(int(rec.get("pid", 0))): break
            time.sleep(POLL_SECONDS)
    rec = _worker_record(worker_id)
    if not rec or rec.get("status") not in {"starting", "running", "degraded"}: return "stopped"
    project = Path(str(rec.get("project") or "")).resolve()
    if not project.is_dir(): return "missing"
    binding = read_binding(project)
    account = account_for_binding(binding)
    if _alive(int(rec.get("pid", 0))): _stop_and_wait(worker_id)
    start_sync(account, binding["bubble"], project, announce=False)
    return "restarted"


@contextmanager
def _upgrade_lock():
    """Serialize simultaneous curl installers without leaving a stale lock after a crash."""
    path = data_root() / "runtime" / "upgrade.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    acquired = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                if stream.tell() == 0: stream.write(b"0"); stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (OSError, ImportError):
            acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except (OSError, ImportError): pass
        stream.close()


def upgrade_workers_command() -> None:
    """Post-install migration: refresh skills and replace workers that were active beforehand.

    Workers already marked stopped are intentionally excluded. That is essential to Stop Agents:
    reinstalling software must not undo a security stop or bypass its local reauthorization step.
    """
    with _upgrade_lock() as acquired:
        if not acquired:
            print(dim("  Another Scientist installer is already finishing this upgrade."))
            return
        _upgrade_workers_locked()


def _upgrade_workers_locked() -> None:
    heading("Finishing Scientist upgrade", "Refreshing integrations and active project workers.")
    _install_detected_skills()
    records = load_workers().get("workers", {})
    active = [(worker_id, dict(rec)) for worker_id, rec in records.items()
              if rec.get("status") in {"starting", "running", "degraded"}
              and rec.get("client_version") != SCIENTIST_CLIENT_VERSION]
    current = [rec for rec in records.values()
               if rec.get("status") in {"starting", "running", "degraded"}
               and rec.get("client_version") == SCIENTIST_CLIENT_VERSION]
    secure_stops = [dict(rec) for rec in records.values() if rec.get("status") == "stopped"
                    and any(word in str(rec.get("last_error") or "").lower()
                            for word in ("authorization was revoked", "secure mode", "stop agents"))]
    restarted = deferred = 0
    failures: list[str] = []
    for worker_id, rec in active:
        if rec.get("jobs") and _alive(int(rec.get("pid", 0))):
            try:
                subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "_upgrade-worker", worker_id],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=os.name != "nt",
                    creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
                    if os.name == "nt" else 0,
                )
                deferred += 1
            except OSError as exc:
                failures.append(f"{rec.get('project') or worker_id}: could not defer restart ({exc})")
            continue
        try:
            if _restart_upgraded_worker(worker_id) == "restarted": restarted += 1
        except RuntimeError as exc:
            detail = str(exc)
            if "server returned 401" in detail:
                detail = ("authorization was revoked by Stop Agents; run login for "
                          f"{rec.get('server')}, then resync {rec.get('project')}")
            failures.append(f"{rec.get('project') or worker_id}: {detail}")
    if restarted: print(green("✓") + f" Restarted {restarted} active project worker(s).")
    if deferred: print(green("✓") + f" {deferred} busy worker restart(s) will happen after their current turns finish.")
    if not active: print(dim("  No outdated project workers needed restarting."))
    if current: print(dim(f"  Left {len(current)} already-current project worker(s) untouched."))
    for failure in failures: print(orange("•") + " " + failure)
    if failures:
        print(dim("  The client is updated; only the projects named above need local attention."))
    if secure_stops:
        print(orange("•") + " Workers stopped by Stop Agents remain stopped, as a security measure.")
        for rec in secure_stops:
            print(dim(f"  Reauthorize with `{cli_name()} login --server {rec.get('server')}`, then run "
                      f"`{cli_name()} resync` in {rec.get('project')}."))


def hard_reset(account: dict, bubble: str, project: Path, *, discard_overleaf: bool = False) -> None:
    heading("Hard reset", f"Replacing {project / '.lockedin'} from bubble {bubble}.")
    overleaf = project / ".lockedin" / "overleaf"
    if (overleaf / ".git").is_dir() and not discard_overleaf:
        raise RuntimeError("Hard reset would remove the local Overleaf checkout. Sync or copy its work first, then retry with `--discard-overleaf`.")
    for wid, rec in load_workers().get("workers", {}).items():
        if Path(rec.get("project", "")).resolve() == project.resolve() and _alive(int(rec.get("pid", 0))): _stop_and_wait(wid)
    sync = ProjectSync(account, project, bubble); sync.validate_or_initialize(reset=True)
    start_sync(account, bubble, project)


def _main() -> None:
    parser = argparse.ArgumentParser(
        prog=APP,
        description="Synchronize one LockedIn bubble into .lockedin in the current project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run without a command for a guided overview.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {SCIENTIST_CLIENT_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)
    login_p = sub.add_parser("login"); login_p.add_argument("--server", required=True)
    workspaces_p = sub.add_parser("workspaces")
    ws_sub = workspaces_p.add_subparsers(dest="workspace_command")
    ws_switch = ws_sub.add_parser("switch"); ws_switch.add_argument("workspace")
    sub.add_parser("bubbles")
    sync_p = sub.add_parser("sync"); sync_p.add_argument("bubble")
    sub.add_parser("ps")
    sub.add_parser("doctor", help="Verify this project's bound worker and server connection.")
    sub.add_parser("resync", help="Resume the bubble this project is already bound to.")
    assets_p = sub.add_parser("assets", help="Large assets this bubble does not sync automatically.")
    assets_sub = assets_p.add_subparsers(dest="assets_command")
    assets_pull = assets_sub.add_parser("pull", help="Download a large asset into this project.")
    assets_pull.add_argument("name", nargs="*", help="Asset filename, or none with --all.")
    assets_pull.add_argument("--all", action="store_true", dest="pull_all")
    assets_push = assets_sub.add_parser("push", help="Upload a large asset from this project.")
    assets_push.add_argument("name", nargs="*", help="Asset filename, or none with --all.")
    assets_push.add_argument("--all", action="store_true", dest="push_all")
    assets_rm = assets_sub.add_parser("rm", help="Delete a large asset from the bubble.")
    assets_rm.add_argument("name", nargs="*", help="Asset filename, or none with --all.")
    assets_rm.add_argument("--all", action="store_true", dest="remove_all")
    assets_rm.add_argument("--yes", action="store_true", dest="assume_yes",
                           help="Skip the confirmation prompt. Required when not on a terminal.")
    assets_rm.add_argument("--force", action="store_true",
                           help="Delete even if a page references the file.")
    connect_p = sub.add_parser("connect", help="Set this computer up for one bubble, end to end.")
    connect_p.add_argument("--server", required=True)
    connect_p.add_argument("--workspace", required=True)
    connect_p.add_argument("--bubble", required=True)
    connect_p.add_argument("--ticket", default="")
    connect_p.add_argument("--project", default="")
    stop_p = sub.add_parser("stop"); stop_p.add_argument("worker_id")
    reset_p = sub.add_parser("hard-reset"); reset_p.add_argument("bubble"); reset_p.add_argument("--discard-overleaf", action="store_true")
    for vendor in VENDORS:
        vendor_parser = sub.add_parser(vendor, help=f"Install the {APP} native skill for {vendor}.")
        vendor_sub = vendor_parser.add_subparsers(dest="vendor_command", required=True)
        vendor_sub.add_parser("setup", help="Install or update the managed native skill.")
    overleaf = sub.add_parser("overleaf").add_subparsers(dest="overleaf_command", required=True)
    overleaf.add_parser("help")
    overleaf.add_parser("connect")
    overleaf.add_parser("status")
    ol_sync = overleaf.add_parser("sync"); ol_sync.add_argument("--message")
    overleaf.add_parser("abort")
    ol_disconnect = overleaf.add_parser("disconnect"); ol_disconnect.add_argument("--discard-local", action="store_true")
    agent_p = sub.add_parser("agent", help="Named agents: register this chat, see its jobs, answer them.")
    agent_sub = agent_p.add_subparsers(dest="agent_command", required=True)
    reg = agent_sub.add_parser("register", help="Register the chat you are in as a named agent on this project's bubble.")
    reg.add_argument("--name", required=True, help="Short unique name, e.g. Ada.")
    reg.add_argument("--role", default="", help="A few words: what this agent is for.")
    reg.add_argument("--goal", default="", help="One sentence the agent keeps in mind.")
    reg.add_argument("--personality", default="", help="Optional tone or habits.")
    reg.add_argument("--model", default="", help="Model id the worker passes to headless turns (vendor default if omitted).")
    reg.add_argument("--vendor", default="", choices=list(VENDORS) + [""], help="Detected from the running chat if omitted.")
    reg.add_argument("--conversation", default="", help="Conversation/session id; detected if omitted.")
    agent_sub.add_parser("list", help="Agents on this project's bubble and what each is doing.")
    jobs_p = agent_sub.add_parser("jobs", help="Open jobs from the local index.")
    jobs_p.add_argument("--all", action="store_true", dest="show_all", help="Include finished jobs.")
    chat_p = agent_sub.add_parser("chat", help="Reopen an agent's conversation interactively.")
    chat_p.add_argument("agent", help="Agent name or id.")
    revive_p = agent_sub.add_parser("revive", help="Resume this folder's worker and retain the agent's persona.")
    revive_p.add_argument("agent", help="Agent name or id.")
    reply_p = agent_sub.add_parser("reply", help="Answer a job: post text into its mark's thread and close it.")
    reply_p.add_argument("job", help="Job id, e.g. j-000012.")
    reply_p.add_argument("--text", default="", help="What changed and why.")
    reply_p.add_argument("--file", default="", help="Read the reply from a file instead.")
    fail_p = agent_sub.add_parser("fail", help="Decline a job with a reason the user will see.")
    fail_p.add_argument("job"); fail_p.add_argument("--reason", required=True)
    reset_p = agent_sub.add_parser("reset", help="Forget the conversation; keep the persona.")
    reset_p.add_argument("agent")
    retire_p = agent_sub.add_parser("retire", help="Remove an agent from the bubble and cancel its jobs.")
    retire_p.add_argument("agent")
    retire_p.add_argument("--purge", action="store_true", help="Also delete the conversation from the vendor's store.")
    worker_p = sub.add_parser("_worker"); worker_p.add_argument("worker_id"); worker_p.add_argument("project")
    upgrade_worker_p = sub.add_parser("_upgrade-worker"); upgrade_worker_p.add_argument("worker_id")
    sub.add_parser("upgrade-workers", help=argparse.SUPPRESS)
    if len(sys.argv) == 1:
        warn_if_outdated()
        welcome()
        return
    args = parser.parse_args()
    if args.command == "_worker": _run_worker(args.worker_id, args.project); return
    if args.command == "_upgrade-worker": _restart_upgraded_worker(args.worker_id, wait_for_jobs=True); return
    if args.command == "upgrade-workers": upgrade_workers_command(); return
    if args.command == "login": login(args.server); return
    warn_if_outdated()
    if args.command == "ps": ps_command(); return
    if args.command == "stop": stop_command(args.worker_id); return
    if args.command == "doctor": doctor_command(_project_root(Path.cwd())); return
    # Also from the project's own binding: an agent registers from wherever its chat was opened.
    if args.command == "agent": agent_command(args); return
    # Deliberately dispatched before choose_account(): resync resolves its account from the
    # project's own binding, so it must not depend on which account was authorized last.
    if args.command == "resync": resync_command(_project_root(Path.cwd())); return
    if args.command == "assets":
        project = _project_root(Path.cwd())
        if args.assets_command == "pull":
            assets_command(project, args.name, pull_all=args.pull_all)
        elif args.assets_command == "push":
            assets_command(project, [], push=args.name, push_all=args.push_all)
        elif args.assets_command == "rm":
            assets_command(project, [], remove=args.name, remove_all=args.remove_all,
                           assume_yes=args.assume_yes, force=args.force)
        else:
            assets_command(project, [])
        return
    # Also above choose_account(): connect runs on a machine with no account yet — it is the
    # command that creates one.
    if args.command == "connect":
        connect_command(args.server, args.workspace, args.bubble,
                        ticket=args.ticket, project_path=args.project); return
    if args.command in VENDORS:
        if args.vendor_command == "setup": setup_vendor_command(args.command)
        return
    if args.command == "overleaf":
        project = _project_root(Path.cwd())
        if args.overleaf_command == "help": overleaf_help_command()
        elif args.overleaf_command == "connect": overleaf_connect(project)
        elif args.overleaf_command == "status": overleaf_status(project)
        elif args.overleaf_command == "sync": overleaf_sync(project, args.message)
        elif args.overleaf_command == "abort": overleaf_abort(project)
        elif args.overleaf_command == "disconnect": overleaf_disconnect(project, args.discard_local)
        return
    account = choose_account()
    if args.command == "workspaces":
        if args.workspace_command == "switch": switch_workspace(account, args.workspace)
        else: workspaces_command(account)
        return
    if args.command == "bubbles": bubbles_command(account); return
    project = _project_root(Path.cwd())
    if args.command == "sync": start_sync(account, args.bubble, project); return
    if args.command == "hard-reset": hard_reset(account, args.bubble, project, discard_overleaf=args.discard_overleaf); return


def main() -> None:
    # An agent often captures this CLI's output through a pipe rather than a real console; when
    # it does, Python falls back to the process locale encoding (e.g. cp1252 on Windows) instead
    # of the console's UTF-16 path, and printing a glyph like heading()'s "◆" raises
    # UnicodeEncodeError after the command has already done its work server-side. Force utf-8 on
    # both streams defensively, tolerating any object that doesn't support reconfigure().
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError): pass
    try: _main()
    except RuntimeError as exc:
        print(red("✗") + " " + bold("Scientist could not complete that command"), file=sys.stderr)
        print("  " + str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__": main()
