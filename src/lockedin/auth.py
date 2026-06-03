"""Minimal multi-user auth: accounts, password hashing, and in-memory sessions.

Stdlib only (PBKDF2-HMAC-SHA256) — no extra dependency. Accounts live in
``data/users/accounts.yaml`` (git-ignored; it holds password *hashes*, never plaintext).
Each account owns a workspace at ``data/users/<user>/`` with ASSETS/, REPORTS/, config/.

This is lightweight auth meant for a trusted, local/LAN setup — not a hardened public
service. Sessions are kept in memory (cleared on restart) and the dev server runs over
plain HTTP; put it behind HTTPS before exposing it.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timezone

import yaml

from . import paths

USERNAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
RESERVED_USERNAMES = {"accounts", "admin", "root"}
MIN_PASSWORD_LEN = 4
_ITERATIONS = 200_000

# token -> username (in-memory; lost on restart)
_SESSIONS: dict[str, str] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def valid_username(username: str) -> bool:
    return bool(USERNAME_RE.match(username)) and username not in RESERVED_USERNAMES


def _hash_password(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), _ITERATIONS)
    return dk.hex()


# --------------------------------------------------------------------------- #
# Account store
# --------------------------------------------------------------------------- #
def load_accounts() -> dict[str, dict]:
    if not paths.ACCOUNTS_YAML.exists():
        return {}
    data = yaml.safe_load(paths.ACCOUNTS_YAML.read_text()) or {}
    return dict(data.get("users", {}))


def save_accounts(users: dict[str, dict]) -> None:
    paths.USERS_DIR.mkdir(parents=True, exist_ok=True)
    paths.ACCOUNTS_YAML.write_text(yaml.safe_dump({"users": users}, sort_keys=True))
    try:
        os.chmod(paths.ACCOUNTS_YAML, 0o600)  # hashes — keep readable only by owner
    except OSError:
        pass


def user_exists(username: str) -> bool:
    return username.strip().lower() in load_accounts()


def create_user(username: str, password: str) -> str:
    """Create an account + its workspace. Returns the normalized username. Raises ValueError."""
    username = username.strip().lower()
    if not valid_username(username):
        raise ValueError("Username must be 1-32 chars: a-z, 0-9, '_' or '-'.")
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    users = load_accounts()
    if username in users:
        raise ValueError("That username is already taken.")
    salt = secrets.token_hex(16)
    users[username] = {"salt": salt, "hash": _hash_password(password, salt),
                       "created_at": _now_iso()}
    save_accounts(users)
    paths.ensure_user_dirs(username)
    return username


def verify_password(username: str, password: str) -> bool:
    rec = load_accounts().get(username.strip().lower())
    if not rec:
        return False
    return hmac.compare_digest(_hash_password(password, rec["salt"]), rec["hash"])


def set_password(username: str, new_password: str) -> None:
    """Replace a user's password (fresh salt). Raises ValueError on a too-short password."""
    username = username.strip().lower()
    if len(new_password) < MIN_PASSWORD_LEN:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    users = load_accounts()
    rec = users.get(username)
    if rec is None:
        raise ValueError("No such user.")
    salt = secrets.token_hex(16)
    rec["salt"] = salt
    rec["hash"] = _hash_password(new_password, salt)
    save_accounts(users)


def rename_user(old: str, new: str) -> str:
    """Rename the account record and repoint live sessions. Returns the normalized new name.

    Does NOT move the user's workspace directory — the caller (service layer) does that, since
    it spans modules. Raises ValueError if the new name is invalid or already taken.
    """
    old = old.strip().lower()
    new = new.strip().lower()
    if not valid_username(new):
        raise ValueError("Username must be 1-32 chars: a-z, 0-9, '_' or '-'.")
    users = load_accounts()
    if old not in users:
        raise ValueError("No such user.")
    if new == old:
        return old
    if new in users:
        raise ValueError("That username is already taken.")
    users[new] = users.pop(old)
    save_accounts(users)
    # repoint any in-memory sessions to the new name so the user stays logged in
    for token, name in list(_SESSIONS.items()):
        if name == old:
            _SESSIONS[token] = new
    return new


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def new_session(username: str) -> str:
    token = secrets.token_urlsafe(24)
    _SESSIONS[token] = username
    return token


def session_user(token: str | None) -> str | None:
    return _SESSIONS.get(token) if token else None


def end_session(token: str | None) -> None:
    if token:
        _SESSIONS.pop(token, None)
