"""Project-local, bubble-scoped synchronization client for LockedIn.

The client keeps authorization and the active workspace in the OS-local profile.  Research
material lives only in ``.lockedin`` below the project from which ``sync`` is started.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

APP = "lockedin-scientist"
SCIENTIST_CLIENT_VERSION = "2026.09.02.2"
POLL_SECONDS = 5
# A worker that has not completed a cycle in three polls is wedged rather than merely busy.
# `doctor` reports that verdict and `resync` repairs exactly what `doctor` complains about, so
# both read the threshold from here.
WORKER_STALE_SECONDS = POLL_SECONDS * 3
BINDING_KEYS = ("server", "user", "workspace_id", "bubble")
WORKER_HISTORY_LIMIT = 10
TERMINAL_WORKER_STATUSES = {"stopped"}
ATTENTION_WORKER_STATUSES = {"degraded", "failed"}
VENDORS = ("codex", "claude", "agy")
MANAGED_VENDOR_SKILL_MARKER = "<!-- Managed by lockedin-scientist -->"

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
`<project-root>` is the directory containing the repository's shared `.git`:

```
git rev-parse --path-format=absolute --git-common-dir   # -> <project-root>/.git
```

Run that command with the tool's working directory set to the **active workspace directory shown
by the current agent session**. Some CLI agents start command tools in their own scratch folder;
that scratch folder is not the project. Do not run the locator there, reuse a previous project,
or guess a project from the user's home directory.

Use that command rather than assuming the current directory. In an ordinary checkout it names that
checkout; in a **git worktree** it names the main checkout, which is where `.lockedin/` lives —
the directory is untracked, so it never appears in a worktree. This is a normal setup, not a
problem to report. Read exactly the one path resolved by the command. Never use `find`, a glob,
`grep`, or a home-directory search to locate other `.lockedin` directories or guides.
It contains the current bubble's editing guide, paper context, math conventions, permitted write
paths, conflict recovery rules, and—when present—rules for the local Overleaf checkout. Follow it
as the source of truth.

If there is no `.lockedin/SKILL.md` at that root, do not create a replacement and do not look
elsewhere. Tell the user to run `lockedin-scientist sync <bubble-slug>` from the project root.
"""

AGY_PLUGIN_MANAGED_BY = "lockedin-scientist"


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
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / APP
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / APP


def config_path() -> Path: return data_root() / "accounts.json"
def workers_path() -> Path: return data_root() / "runtime" / "workers.json"


def _atomic_json(path: Path, value: dict, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
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
    try: return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError): return {"accounts": []}


def save_config(cfg: dict) -> None: _atomic_json(config_path(), cfg, private=True)


def load_workers() -> dict:
    path = workers_path()
    if not path.exists(): return {"workers": {}}
    try: return json.loads(path.read_text())
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
                     on_progress=None) -> int:
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
    tmp = dest.with_suffix(dest.suffix + ".part")
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
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"server returned {exc.code}: {exc.read().decode(errors='replace')}") from exc
    except urllib.error.URLError as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"cannot reach LockedIn server: {exc.reason}") from exc


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
        print("  Reinstall: curl -fsSL https://raw.githubusercontent.com/HamidrezaKmK/lockedin/main/install.sh | bash", file=sys.stderr)


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


SKILL_VERSION = 37

SKILL_ROUTER = """\
<!-- lockedin-scientist-skill: 37 -->
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
| `guides/editing.md` | **reference, not prerequisite** | large |

`guides/editing.md` is a syntax reference for figures, citations, theorem environments and the
rest. Do not read it end to end: search it for the one construct you are unsure of, and skip it
entirely when editing prose or maths you already know how to write. It opens with its own
contents list.

## Where `.lockedin/` is

Every path in this guide is relative to the **project root**: the directory holding the shared
`.git`, which `git rev-parse --path-format=absolute --git-common-dir` names (its parent). Resolve
it once at the start of a session and use it as the prefix.

If you are working in a **git worktree**, that root is the main checkout, not the worktree.
`.lockedin/` is untracked, so it does not come along — and the sync worker only watches the main
checkout's copy. Read and edit that copy. This is the ordinary arrangement: do not describe it to
the user, do not treat it as a problem, and do not run `lockedin-scientist sync` inside the
worktree to obtain a local one — the worker registry keys on directory, so that would start a
second worker on the same bubble and the bubble page would flag the pair as conflicting.

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

Use these exact paths when their information is needed, resolved against the project root above —
in a worktree they are in the main checkout, not the current directory. Do not spend time searching
the project for an alternative copy:

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
explicitly asks to combine it with project code or files.""",

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
ask whether they want to repair it, in this order. Run these from the project root — from a
worktree they fail with "No valid `.lockedin/config/binding.json` in this project":

1. `lockedin-scientist ps` — every worker on this machine and the folder each one syncs.
2. `lockedin-scientist resync` — the usual repair. It resumes whatever bubble this project is
   already bound to, needs no arguments, and leaves `.lockedin` intact.
3. If the command itself is missing (a fresh cloud sandbox, for instance), ask the user for a
   setup link: the bubble page's 🤖 button produces one line that installs the client, signs this
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
same exact reply is safe to retry. That is the whole of your power over a mark: **you cannot
resolve, remove, or delete one — anywhere** — and a `resolves=` attribute in a slide header is
ignored. The user removes a mark in the app once your answer satisfies them. If your edit removes
the text a mark points at, the mark goes orphan and stays visible; that is normal, not a problem
to fix.
""",

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
    (root / "SKILL.md").write_text(skill_document())
    guides = root / "guides"
    guides.mkdir(parents=True, exist_ok=True)
    written = dict(GUIDES)
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
        (guides / name).write_text(body)
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
            existing = path.read_text()
        except OSError as exc:
            raise RuntimeError(f"Could not inspect existing skill at {path}: {exc}") from exc
        if MANAGED_VENDOR_SKILL_MARKER not in existing:
            raise RuntimeError(
                f"Refusing to overwrite the existing skill at {path}. "
                "Move or remove that user-owned skill, then run setup again."
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _vendor_skill_paths(vendor: str, home: Path) -> tuple[Path, ...]:
    """Return the globally discovered native skill location for a supported agent."""
    if vendor == "codex":
        return (home / ".codex" / "skills" / APP / "SKILL.md",)
    if vendor == "claude":
        return (home / ".claude" / "skills" / APP / "SKILL.md",)
    if vendor == "agy":
        plugin = home / ".gemini" / "antigravity-cli" / "plugins" / APP
        return (plugin / "plugin.json", plugin / "skills" / APP / "SKILL.md")
    raise RuntimeError(f"Unknown agent {vendor!r}. Choose one of: {', '.join(VENDORS)}.")


def setup_vendor_skill(vendor: str, *, home: Path | None = None) -> tuple[Path, ...]:
    """Install the named bootstrap in the vendor's native global skill discovery path."""
    vendor = vendor.lower()
    home = Path.home() if home is None else Path(home)
    targets = _vendor_skill_paths(vendor, home)
    if vendor == "agy":
        plugin_json, skill_path = targets
        if plugin_json.exists():
            try:
                plugin = json.loads(plugin_json.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Could not inspect existing agy plugin at {plugin_json}: {exc}") from exc
            if plugin.get("managed_by") != AGY_PLUGIN_MANAGED_BY:
                raise RuntimeError(
                    f"Refusing to overwrite the existing agy plugin at {plugin_json.parent}. "
                    "Move or remove that user-owned plugin, then run setup again."
                )
        plugin_json.parent.mkdir(parents=True, exist_ok=True)
        plugin_json.write_text(json.dumps({
            "name": APP,
            "version": "1.0.0",
            "description": "Project-local LockedIn Scientist bootstrap skill.",
            "managed_by": AGY_PLUGIN_MANAGED_BY,
        }, indent=2) + "\n")
        _write_managed_vendor_file(skill_path, VENDOR_SKILL_BOOTSTRAP)
        agy = shutil.which("agy")
        if not agy:
            raise RuntimeError("agy is not installed or is not on PATH, so its native skill could not be imported.")
        try:
            installed = subprocess.run(
                [agy, "plugin", "install", str(plugin_json.parent)],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
            )
        except OSError as exc:
            raise RuntimeError(f"Could not ask agy to import its native skill: {exc}") from exc
        if installed.returncode:
            detail = (installed.stderr or installed.stdout).strip()
            raise RuntimeError(f"agy could not import the native {APP} skill" + (f": {detail}" if detail else "."))
    else:
        _write_managed_vendor_file(targets[0], VENDOR_SKILL_BOOTSTRAP)
    return targets


def setup_vendor_command(vendor: str) -> None:
    targets = setup_vendor_skill(vendor)
    heading(f"{vendor.title()} skill installed", "The bootstrap is global; its report guide remains project-local.")
    for target in targets:
        print(green("✓") + " " + dim(str(target)))
    if vendor == "codex":
        print(dim("  Start Codex in a synchronized project, then invoke $lockedin-scientist."))
    elif vendor == "claude":
        print(dim("  Restart Claude Code if it is open, then invoke /lockedin-scientist in a synchronized project."))
    else:
        print(dim("  Restart agy if it is open, then use /skills to select lockedin-scientist in a synchronized project."))


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
        value = json.loads(path.read_text())
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
        if len(entries) == 1 and entries[0] == legacy and legacy.read_text(errors="replace").startswith(LEGACY_OVERLEAF_README_PREFIX):
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
        try: return json.loads(self.binding_path.read_text())
        except (OSError, json.JSONDecodeError): return None

    def _state(self) -> dict:
        try: return json.loads(self.state_path.read_text())
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
            _atomic_json(self.binding_path, wanted)
            self._write_state({"files": {}})
            self._exclude_from_git()
        skill = self.root / "SKILL.md"
        if created or f"lockedin-scientist-skill: {SKILL_VERSION}" not in (skill.read_text() if skill.exists() else ""):
            self._refresh_skill()

    def _exclude_from_git(self) -> None:
        git = self.project / ".git"
        if not git.is_dir(): return
        exclude = git / "info" / "exclude"; exclude.parent.mkdir(parents=True, exist_ok=True)
        text = exclude.read_text() if exclude.exists() else ""
        if ".lockedin/" not in text.splitlines():
            exclude.write_text(text.rstrip("\n") + "\n.lockedin/\n")

    def worker_uid(self) -> str:
        """A stable id for *this project directory*, minted once and kept across worker restarts.

        The server monitors one row per synchronized directory. The per-run worker id would make a
        restarted worker look like a second directory, which is precisely the distinction the
        monitor exists to make.
        """
        try: return str(json.loads(self.identity_path.read_text())["worker_uid"])
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
        (folder / (stem + ".patch")).write_text(patch)

    def _report_paths(self) -> list[str]:
        """Local report content this sync may carry: pages, flat figures, and chalk-talk decks.

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
            if rel in oversize:
                return False
            parts = Path(rel).parts
            return bool(
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
                        if r not in oversize and not local_oversize(r)}
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
        # Everything except report pages and report assets is server-authoritative. This includes
        # the report manifest and paper inventory that agents read but never edit.
        for rel in sorted(r for r in remote if r not in report_remote and r not in oversize):
            path = self._local(rel)
            if (not path.exists() or self._rev(path.read_bytes()) != remote[rel]
                    or tracked.get(rel, {}).get("revision") != remote[rel]):
                item = fetch(rel); self._write_remote(item)
            tracked[rel] = {"revision": remote[rel]}
        math_revision = remote.get("config/math.yaml", "")
        # The version marker is checked here as well as in validate_or_initialize, which only runs
        # when a worker starts: a project whose worker has been up since before a SKILL_VERSION
        # bump would otherwise keep serving its agents the old guide indefinitely — exactly the
        # projects that are working fine and never get restarted.
        skill = self.root / "SKILL.md"
        stale = f"lockedin-scientist-skill: {SKILL_VERSION}" not in (
            skill.read_text() if skill.exists() else "")
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


def _alive(pid: int) -> bool:
    if pid <= 0: return False
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
    stop = False
    def end(*_):
        nonlocal stop; stop = True
    signal.signal(signal.SIGTERM, end); signal.signal(signal.SIGINT, end)
    _update_worker(worker_id, status="running", pid=os.getpid(), last_error="")
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
        except Exception as exc:
            _update_worker(worker_id, status="degraded", last_error=str(exc))
            sync.report = {"status": "degraded", "error": str(exc)}
        for _ in range(POLL_SECONDS * 10):
            if stop: break
            time.sleep(.1)
    # The parting synchronization doubles as a shutdown notice, so the server's monitor shows the
    # worker as stopped straight away instead of waiting for it to time out.
    sync.report = {"status": "stopped", "error": ""}
    try: sync.sync_once()
    except Exception: pass
    _update_worker(worker_id, status="stopped", stopped_at=time.time())


def start_sync(account: dict, bubble: str, project: Path, *, announce: bool = True) -> None:
    # `announce=False` is for callers that already printed their own heading (resync).
    if announce: heading("Synchronizing a bubble", f"{bubble} → {project / '.lockedin'}")
    sync = ProjectSync(account, project, bubble); sync.validate_or_initialize(); sync.sync_once()
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
           "bubble": bubble, "started_at": time.time(), "last_sync": time.time(), "last_error": "", "status": "starting"}
    data.setdefault("workers", {})[wid] = rec; save_workers(data)
    with log.open("ab") as stream:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_worker", wid, str(project.resolve())],
                                stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                                start_new_session=os.name != "nt")
    _update_worker(wid, pid=proc.pid)
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
            rec["status"] = "stopped"; rec["stopped_at"] = time.time()
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
        binding = json.loads(path.read_text())
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


def _project_worker(project: Path) -> dict | None:
    """The most recently started worker record for this project directory, if any."""
    project = project.resolve()
    records = [rec for rec in load_workers().get("workers", {}).values()
               if Path(rec.get("project", "")).resolve() == project]
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
    matches = [rec for rec in load_workers().get("workers", {}).values()
               if Path(rec.get("project", "")).resolve() == project and rec.get("bubble") == binding["bubble"]]
    heading("Scientist doctor", str(root))
    if not matches:
        raise RuntimeError(f"No worker is assigned to this project. Run `lockedin-scientist resync` from {project}.")
    rec = max(matches, key=lambda item: item.get("started_at", 0))
    if any(rec.get(key) != binding[key] for key in BINDING_KEYS):
        raise RuntimeError("The assigned worker does not match this .lockedin binding. Stop it, then run `lockedin-scientist resync`.")
    status, pid = rec.get("status", "?"), int(rec.get("pid", 0))
    if status != "running" or not _alive(pid):
        detail = rec.get("last_error") or rec.get("error") or status
        raise RuntimeError(f"Worker {rec.get('id', '?')} is not healthy ({detail}). Run `lockedin-scientist ps` for details, then `lockedin-scientist resync` to resume this project.")
    if not _worker_is_healthy(rec):
        raise RuntimeError(f"Worker {rec.get('id', '?')} has not completed a sync recently. Run `lockedin-scientist ps` and repair it before relying on report submission.")
    account = account_for_binding(binding)
    account_request(account, "GET", f"/api/scientist/v2/bubbles/{binding['bubble']}/manifest")
    print(green("✓") + f" Worker {bold(rec['id'])} is healthy and can reach bubble {bold(binding['bubble'])}.")


VENDOR_INVOCATION = {
    "codex": "start codex, then invoke $lockedin-scientist",
    "claude": "start claude, then invoke /lockedin-scientist",
    "agy": "start agy, then use /skills to select lockedin-scientist",
}


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
    return project.resolve()


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
        start_sync(account, bubble, project)

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
                   push: list[str] | None = None, push_all: bool = False) -> None:
    """List, fetch, or send the assets a sync deliberately leaves alone.

    Big binaries (photo archives, datasets, model checkpoints) are listed by the manifest but
    never content-synced in either direction: hashing one on every poll costs more than the whole
    rest of the bubble, and no agent reads a zip anyway. This is how you move one on purpose.
    """
    push = push or []
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
            if path.is_file() and cap and path.stat().st_size > cap:
                local[path.name] = path

    def matches(name: str, chosen: list[str]) -> bool:
        return name in chosen or f"reports/assets/{name}" in chosen

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
                item["path"], dest, on_progress=progress if live else None)
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
    rec = _project_worker(project)
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
    worker_p = sub.add_parser("_worker"); worker_p.add_argument("worker_id"); worker_p.add_argument("project")
    if len(sys.argv) == 1:
        warn_if_outdated()
        welcome()
        return
    args = parser.parse_args()
    if args.command == "_worker": _run_worker(args.worker_id, args.project); return
    if args.command == "login": login(args.server); return
    warn_if_outdated()
    if args.command == "ps": ps_command(); return
    if args.command == "stop": stop_command(args.worker_id); return
    if args.command == "doctor": doctor_command(Path.cwd()); return
    # Deliberately dispatched before choose_account(): resync resolves its account from the
    # project's own binding, so it must not depend on which account was authorized last.
    if args.command == "resync": resync_command(Path.cwd()); return
    if args.command == "assets":
        if args.assets_command == "pull":
            assets_command(Path.cwd(), args.name, pull_all=args.pull_all)
        elif args.assets_command == "push":
            assets_command(Path.cwd(), [], push=args.name, push_all=args.push_all)
        else:
            assets_command(Path.cwd(), [])
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
        project = Path.cwd()
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
    project = Path.cwd()
    if args.command == "sync": start_sync(account, args.bubble, project); return
    if args.command == "hard-reset": hard_reset(account, args.bubble, project, discard_overleaf=args.discard_overleaf); return


def main() -> None:
    try: _main()
    except RuntimeError as exc:
        print(red("✗") + " " + bold("Scientist could not complete that command"), file=sys.stderr)
        print("  " + str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__": main()
