"""One-shot setup tickets: the credential behind a bubble's 🤖 "connect a terminal" link.

Connecting a project to a bubble used to be five manual commands with two ids transcribed by hand.
A ticket collapses that into one copied line: the browser — already signed in — mints a Scientist
token and parks it here under an unguessable id, the page hands the user
``curl .../setup/<ticket>.sh | bash``, and ``lockedin-scientist connect`` redeems the ticket once
to authorize itself, bind the bubble, and install the agent skills.

The registry is process-local and in-memory, like :mod:`presence` and the device codes: a ticket is
a claim about *right now*. Nothing reaches disk, and a restarted server invalidates every
outstanding link rather than leaving live credentials lying around in a file.

**A ticket is a bearer credential.** For its short life, whoever runs the link is authorized as the
user who minted it — so tickets are single-use (``redeem`` pops), short-lived (:data:`TICKET_TTL`),
and unguessable. The UI says so next to the snippet; do not lengthen the TTL without revisiting
that. The token itself is minted by the caller, so revoking it is the ordinary Scientist-token
path, unaffected by the ticket having expired.
"""
from __future__ import annotations

import secrets
import threading
import time

# Long enough to switch to a terminal and paste; short enough that a snippet left on a screen or in
# a chat window is worthless. Matches the device-code window.
TICKET_TTL = 600.0
# Tickets exist only between a click and a paste, so a healthy process holds a handful. The cap is
# there because minting is reachable from a request, not because the number is expected to grow.
MAX_TICKETS = 500

# The install one-liners live here because this module already embeds them in the scripts it
# serves; the setup dialog shows the same strings for its by-hand fallback rather than keeping a
# fourth copy of them in the frontend.
INSTALL_UNIX = "curl -fsSL https://raw.githubusercontent.com/HamidrezaKmK/lockedin/main/install.sh | bash"
INSTALL_POWERSHELL = "irm https://raw.githubusercontent.com/HamidrezaKmK/lockedin/main/install.ps1 | iex"

_LOCK = threading.Lock()
# ticket id -> {user, token, workspace_id, slug, created_at}
_TICKETS: dict[str, dict] = {}


def _sweep(now: float) -> None:
    """Drop expired tickets. Callers hold the lock."""
    for ticket in [key for key, rec in _TICKETS.items() if now - rec["created_at"] > TICKET_TTL]:
        _TICKETS.pop(ticket, None)


def mint(user: str, token: str, workspace_id: str, slug: str) -> str:
    """Park an authorization for one bubble and return the id that redeems it."""
    now = time.time()
    ticket = secrets.token_urlsafe(24)
    with _LOCK:
        _sweep(now)
        if len(_TICKETS) >= MAX_TICKETS:
            # Oldest first: a flood of unredeemed tickets must not evict one just handed out.
            for stale, _ in sorted(_TICKETS.items(), key=lambda item: item[1]["created_at"])[:50]:
                _TICKETS.pop(stale, None)
        _TICKETS[ticket] = {"user": user, "token": token, "workspace_id": workspace_id,
                            "slug": slug, "created_at": now}
    return ticket


def peek(ticket: str) -> dict | None:
    """The ticket's bubble, without spending it — for rendering its script.

    Deliberately returns no token: serving the script must not be able to leak the credential to
    anyone who merely guesses at URLs. Only :func:`redeem` hands that over, and only once.
    """
    now = time.time()
    with _LOCK:
        _sweep(now)
        rec = _TICKETS.get(ticket)
        if not rec:
            return None
        return {"user": rec["user"], "workspace_id": rec["workspace_id"], "slug": rec["slug"],
                "expires_in": round(TICKET_TTL - (now - rec["created_at"]), 1)}


def redeem(ticket: str) -> dict | None:
    """Spend the ticket, returning its authorization exactly once."""
    now = time.time()
    with _LOCK:
        _sweep(now)
        rec = _TICKETS.pop(ticket, None)
    if not rec:
        return None
    return {"user": rec["user"], "token": rec["token"], "workspace_id": rec["workspace_id"],
            "slug": rec["slug"]}


def clear() -> None:
    """Forget every ticket. For tests."""
    with _LOCK:
        _TICKETS.clear()


def unix_script(origin: str, ticket: str, workspace_id: str, slug: str) -> str:
    """The bash the macOS/Linux snippet pipes into a shell.

    Two environments have to work from the same line. A person pastes it into their own terminal,
    where `connect` should ask which folder to use — and because the script is itself arriving on
    stdin from curl, that prompt only reaches them through `/dev/tty`. An **agent** pastes it into
    a shell with no controlling terminal at all, where opening `/dev/tty` is a hard error; that is
    also the case where the client is least likely to be installed already (a fresh cloud box),
    so failing there would break the one path that could have set it up. With nobody to ask, the
    directory the script was run from *is* the project, which is what a person would have answered.
    """
    connect = (f"lockedin-scientist connect \\\n"
               f"    --server {origin!r} \\\n"
               f"    --workspace {workspace_id!r} \\\n"
               f"    --bubble {slug!r} \\\n"
               f"    --ticket {ticket!r}")
    return f"""#!/usr/bin/env bash
set -euo pipefail

echo "Installing lockedin-scientist…"
{INSTALL_UNIX}

# A development server may be ahead of the released main branch. Overlay its matching,
# dependency-free client so the setup link and the server always speak the same protocol.
client_root="${{XDG_DATA_HOME:-$HOME/.local/share}}/lockedin-scientist/client"
client_tmp="$(mktemp)"
trap 'rm -f "$client_tmp"' EXIT
curl -fsSL {(origin + '/setup/scientist_cli.py')!r} -o "$client_tmp"
install -m 0644 "$client_tmp" "$client_root/scientist_cli.py"

# install.sh does not touch PATH; it only prints where it put the command.
export PATH="$HOME/.local/bin:$PATH"

# stderr is silenced *before* the open is attempted: bash applies redirections left to right,
# so the other order lets "/dev/tty: No such device" escape to an agent's log.
if : 2>/dev/null < /dev/tty; then
  exec {connect} < /dev/tty
else
  echo "No terminal to ask on — connecting the current directory: $PWD"
  exec {connect} \\
    --project "$PWD"
fi
"""


def powershell_script(origin: str, ticket: str, workspace_id: str, slug: str) -> str:
    """The PowerShell the Windows snippet pipes into ``iex``.

    Same two environments as :func:`unix_script`. Read-Host reads the console directly, so there
    is no ``/dev/tty`` dance — but with input redirected (an agent, CI) it cannot prompt either,
    and the current directory stands in for the answer.
    """
    def quote(value: str) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    connect = (f"lockedin-scientist connect `\n"
               f"    --server {quote(origin)} `\n"
               f"    --workspace {quote(workspace_id)} `\n"
               f"    --bubble {quote(slug)} `\n"
               f"    --ticket {quote(ticket)}")
    return f"""$ErrorActionPreference = 'Stop'
Write-Host "Installing lockedin-scientist…"
{INSTALL_POWERSHELL}

# Keep a development server and its installed client protocol-matched before connecting.
$clientRoot = Join-Path $env:LOCALAPPDATA 'LockedInScientist/client'
$client = Join-Path $clientRoot 'scientist_cli.py'
$clientTemp = Join-Path $clientRoot ("scientist_cli." + [guid]::NewGuid().ToString('N') + '.tmp')
Invoke-WebRequest {quote(origin + '/setup/scientist_cli.py')} -OutFile $clientTemp
Move-Item -Force -Path $clientTemp -Destination $client

if ([Console]::IsInputRedirected) {{
  Write-Host "No terminal to ask on - connecting the current directory: $PWD"
  {connect} `
    --project "$PWD"
}} else {{
  {connect}
}}
"""


def expired_script(shell: str) -> str:
    """What an unknown or spent ticket serves.

    A 404 body piped into a shell produces a confusing parse error, so an expired link explains
    itself and exits non-zero instead.
    """
    message = "This LockedIn setup link has expired or was already used. Open the bubble and click the robot icon for a fresh one."
    if shell == "powershell":
        return f"Write-Error {message!r}\nexit 1\n"
    return f'#!/usr/bin/env bash\necho {message!r} >&2\nexit 1\n'
