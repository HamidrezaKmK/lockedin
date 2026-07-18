"""Global public-share index: maps an unguessable token to a workspace and bubble.

Bubble sharing is *unlisted*: a bubble carries a permanent ``share_token`` in its workspace
registry plus a ``share_active`` flag. This module keeps the cross-workspace lookup that the public
``/share/<token>`` routes need — they run with no logged-in session, so they can't know which
user's workspace to open without it.

The index lives at the **base** root (``data/users/share_index.yaml``), above any single user's
workspace, and stores only ``{token: {"workspace_id": ..., "slug": ...}}`` — no content. Access is still
gated per-request on the bubble's live ``share_active`` flag, so removing a token here is not
required to revoke access (turning sharing off is enough); we keep the mapping so a stable link
keeps working when sharing is toggled back on.
"""
from __future__ import annotations

import os

import yaml

from . import paths


def _load() -> dict[str, dict]:
    p = paths.SHARE_INDEX
    if not p.exists():
        return {}
    return dict((yaml.safe_load(p.read_text()) or {}).get("shares", {}))


def _save(index: dict[str, dict]) -> None:
    p = paths.SHARE_INDEX
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump({"shares": index}, sort_keys=True))
    os.replace(tmp, p)


def register(token: str, user: str, slug: str, *, workspace_id: str = "") -> None:
    """Idempotently record that ``token`` resolves to a workspace (or legacy user) and bubble."""
    index = _load()
    entry = {"workspace_id": workspace_id, "slug": slug} if workspace_id else {"user": user, "slug": slug}
    if index.get(token) != entry:
        index[token] = entry
        _save(index)


def resolve(token: str) -> dict | None:
    """Return a share-index entry for ``token``, or ``None`` if unknown."""
    return _load().get(token)


def migrate_user_entries(personal_workspaces: dict[str, str]) -> int:
    """Convert legacy user-home share entries to their users' Personal workspaces.

    The operation is idempotent and deliberately leaves entries alone when the user has no
    known Personal workspace, preserving compatibility with an un-migrated installation.
    """
    index = _load()
    changed = 0
    for entry in index.values():
        user = entry.get("user", "")
        workspace_id = personal_workspaces.get(user)
        if not workspace_id:
            continue
        entry.pop("user", None)
        entry["workspace_id"] = workspace_id
        changed += 1
    if changed:
        _save(index)
    return changed


def rename_user(old: str, new: str) -> None:
    """Repoint every share entry from ``old`` to ``new`` (used when a username changes)."""
    index = _load()
    changed = False
    for entry in index.values():
        if entry.get("user") == old:
            entry["user"] = new
            changed = True
    if changed:
        _save(index)


def drop_bubble(user: str, slug: str) -> None:
    """Remove any tokens pointing at a (now-deleted) bubble."""
    index = _load()
    dead = [t for t, e in index.items()
            if (e.get("user") == user or e.get("workspace_id") == user) and e.get("slug") == slug]
    if dead:
        for t in dead:
            index.pop(t, None)
        _save(index)


def drop_user(user: str) -> None:
    """Remove any public-share tokens owned by a deleted user."""
    index = _load()
    dead = [t for t, e in index.items() if e.get("user") == user]
    if dead:
        for t in dead:
            index.pop(t, None)
        _save(index)
