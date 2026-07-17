"""Workspace registry, membership checks, and migration of legacy user research roots."""
from __future__ import annotations

import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import paths


class WorkspaceError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def registry_path() -> Path:
    return paths.base_root() / "data" / "workspaces" / "workspaces.yaml"


def workspace_home(workspace_id: str) -> Path:
    return paths.base_root() / "data" / "workspaces" / workspace_id


def _load() -> dict:
    p = registry_path()
    if not p.exists():
        return {"version": 1, "workspaces": {}}
    return yaml.safe_load(p.read_text()) or {"version": 1, "workspaces": {}}


def _save(data: dict) -> None:
    p = registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False))
    tmp.replace(p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _new_id(data: dict) -> str:
    while True:
        value = secrets.token_hex(16)
        if value not in data["workspaces"]:
            return value


def _ensure_dirs(workspace_id: str) -> Path:
    root = workspace_home(workspace_id)
    for rel in ("ASSETS", "REPORTS", "config"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    return root


def get(workspace_id: str) -> dict | None:
    return _load().get("workspaces", {}).get(workspace_id)


def create(user: str, name: str, *, kind: str = "shared") -> dict:
    name = (name or "").strip()
    if not name or len(name) > 120:
        raise WorkspaceError("Workspace name must be 1-120 characters.")
    data = _load()
    wid = _new_id(data)
    rec = {"id": wid, "name": name, "kind": kind, "owner_user": user,
           "created_at": _now(), "updated_at": _now(),
           "members": {user: {"role": "admin", "joined_at": _now()}}}
    data.setdefault("workspaces", {})[wid] = rec
    _save(data)
    _ensure_dirs(wid)
    return rec


def ensure_personal(user: str, account: dict | None = None) -> dict:
    """Return the user's durable Personal workspace, creating/migrating it if needed."""
    data = _load()
    rec = account or {}
    wid = rec.get("personal_workspace_id")
    existing = data.get("workspaces", {}).get(wid) if wid else None
    if existing:
        _ensure_dirs(wid)
        return existing
    # Recover old records without a pointer before creating a duplicate.
    for candidate in data.get("workspaces", {}).values():
        if candidate.get("kind") == "personal" and candidate.get("owner_user") == user:
            _ensure_dirs(candidate["id"])
            return candidate
    return create(user, "Personal", kind="personal")


def list_for_user(user: str) -> list[dict]:
    rows = []
    for rec in _load().get("workspaces", {}).values():
        member = rec.get("members", {}).get(user)
        if member:
            rows.append({"id": rec["id"], "name": rec["name"], "kind": rec.get("kind", "shared"),
                         "role": member.get("role", "editor"), "owner_user": rec.get("owner_user", ""),
                         "updated_at": rec.get("updated_at", "")})
    return sorted(rows, key=lambda r: (r["kind"] != "personal", r["name"].lower(), r["id"]))


def resolve(user: str, workspace_id: str, *, admin: bool = False) -> tuple[dict, Path]:
    rec = get(workspace_id)
    if not rec:
        raise WorkspaceError("No such workspace.")
    member = rec.get("members", {}).get(user)
    if not member:
        raise PermissionError("You do not have access to this workspace.")
    if admin and member.get("role") != "admin":
        raise PermissionError("Workspace admin access is required.")
    return rec, _ensure_dirs(workspace_id)


def invite(actor: str, workspace_id: str, username: str) -> dict:
    rec, _ = resolve(actor, workspace_id, admin=True)
    username = username.strip().lower()
    if not username or username == actor:
        raise WorkspaceError("Choose another approved user.")
    if rec.get("kind") == "personal":
        raise WorkspaceError("Personal workspaces cannot have additional members.")
    data = _load(); item = data["workspaces"][workspace_id]
    if username in item["members"]:
        raise WorkspaceError("That user is already a member.")
    item["members"][username] = {"role": "editor", "joined_at": _now()}
    item["updated_at"] = _now(); _save(data)
    return item


def set_role(actor: str, workspace_id: str, username: str, role: str) -> dict:
    if role not in {"admin", "editor"}: raise WorkspaceError("Invalid workspace role.")
    rec, _ = resolve(actor, workspace_id, admin=True)
    if rec.get("kind") == "personal": raise WorkspaceError("Personal workspace roles cannot change.")
    data = _load(); item = data["workspaces"][workspace_id]
    member = item["members"].get(username)
    if not member: raise WorkspaceError("That user is not a member.")
    if member.get("role") == "admin" and role == "editor":
        if sum(m.get("role") == "admin" for m in item["members"].values()) <= 1:
            raise WorkspaceError("A workspace must have at least one admin.")
    member["role"] = role; item["updated_at"] = _now(); _save(data)
    return item


def remove_member(actor: str, workspace_id: str, username: str) -> dict:
    rec, _ = resolve(actor, workspace_id, admin=True)
    if rec.get("kind") == "personal": raise WorkspaceError("Personal workspace members cannot be removed.")
    data = _load(); item = data["workspaces"][workspace_id]; member = item["members"].get(username)
    if not member: raise WorkspaceError("That user is not a member.")
    if member.get("role") == "admin" and sum(m.get("role") == "admin" for m in item["members"].values()) <= 1:
        raise WorkspaceError("A workspace must have at least one admin.")
    del item["members"][username]; item["updated_at"] = _now(); _save(data)
    return item


def members(user: str, workspace_id: str) -> list[dict]:
    rec, _ = resolve(user, workspace_id)
    return [{"username": u, "role": m.get("role", "editor"), "owner": u == rec.get("owner_user")}
            for u, m in sorted(rec.get("members", {}).items())]


def migrate_legacy(user: str, account: dict) -> dict:
    """Move existing research content into the Personal workspace, once and idempotently."""
    rec = ensure_personal(user, account)
    root = _ensure_dirs(rec["id"]); legacy = paths.user_home(user)
    for rel in ("ASSETS", "REPORTS", "bubbles.yaml", "todos.yaml", "config/math.yaml"):
        source, target = legacy / rel, root / rel
        if not source.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            # The destination root is created with standard empty directories; merge contents so
            # an existing user's ASSETS/ or REPORTS/ is never skipped merely because it exists.
            for child in source.iterdir():
                destination = target / child.name
                if destination.exists():
                    continue
                shutil.move(str(child), str(destination))
            try:
                source.rmdir()
            except OSError:
                pass
        elif not target.exists():
            shutil.move(str(source), str(target))
    return rec
