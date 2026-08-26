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
    """The bash the macOS/Linux snippet pipes into a shell."""
    # `< /dev/tty` is load-bearing: this script is itself arriving on stdin from curl, so without
    # it `connect`'s "which folder?" prompt would read the remainder of the script and never reach
    # a human. install.sh does not put its bin directory on PATH either, hence the explicit export.
    return f"""#!/usr/bin/env bash
set -euo pipefail

echo "Installing lockedin-scientist…"
curl -fsSL https://raw.githubusercontent.com/HamidrezaKmK/lockedin/main/install.sh | bash

export PATH="$HOME/.local/bin:$PATH"

exec lockedin-scientist connect \\
  --server {origin!r} \\
  --workspace {workspace_id!r} \\
  --bubble {slug!r} \\
  --ticket {ticket!r} < /dev/tty
"""


def powershell_script(origin: str, ticket: str, workspace_id: str, slug: str) -> str:
    """The PowerShell the Windows snippet pipes into ``iex``."""
    # install.ps1 already prepends its bin directory to the current session's PATH, so the command
    # below resolves without an explicit path. Read-Host reads the console, so no /dev/tty dance.
    def quote(value: str) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    return f"""$ErrorActionPreference = 'Stop'
Write-Host "Installing lockedin-scientist…"
irm https://raw.githubusercontent.com/HamidrezaKmK/lockedin/main/install.ps1 | iex

lockedin-scientist connect `
  --server {quote(origin)} `
  --workspace {quote(workspace_id)} `
  --bubble {quote(slug)} `
  --ticket {quote(ticket)}
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
