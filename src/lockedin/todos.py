"""TODOs — lightweight, GitHub-issue-style task items, global per user.

Each TODO has an auto-incrementing integer ``id`` (referenced from report pages as ``@<id>``),
a ``title``, a markdown ``note`` (same math/markdown style as reports), a ``done`` flag, and a
``created_at`` stamp. Stored in a single per-user ``todos.yaml``::

    next_id: 4
    todos:
      "1": {id: 1, title: "...", note: "...", done: false, created_at: "..."}

This module is **pure storage**: it never imports :mod:`bubbles`. Reference counting (scanning
report pages for ``@<id>``) and the delete-when-unreferenced guard live in :mod:`service`, which
already orchestrates both. All paths resolve against the active per-user context root.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import yaml

from . import paths


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _load() -> dict:
    path = paths.TODOS_YAML
    if not path.exists():
        return {"next_id": 1, "todos": {}}
    data = yaml.safe_load(path.read_text()) or {}
    data.setdefault("next_id", 1)
    data.setdefault("todos", {})
    return data


def _save(data: dict) -> None:
    _atomic_write(paths.TODOS_YAML, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def list_todos() -> list[dict]:
    """All TODOs, sorted by id ascending."""
    todos = _load()["todos"]
    return [todos[k] for k in sorted(todos, key=lambda k: int(k))]


def get_todo(tid: int) -> dict | None:
    return _load()["todos"].get(str(int(tid)))


def add_todo(title: str, note: str = "") -> dict:
    data = _load()
    tid = int(data["next_id"])
    todo = {"id": tid, "title": (title or "").strip() or f"TODO {tid}",
            "note": note or "", "done": False, "created_at": _now_iso()}
    data["todos"][str(tid)] = todo
    data["next_id"] = tid + 1
    _save(data)
    return todo


def update_todo(tid: int, *, title: str | None = None, note: str | None = None,
                done: bool | None = None) -> dict:
    """Partial update. Raises ``KeyError`` if the TODO doesn't exist."""
    data = _load()
    key = str(int(tid))
    if key not in data["todos"]:
        raise KeyError(tid)
    todo = data["todos"][key]
    if title is not None:
        todo["title"] = title.strip() or todo["title"]
    if note is not None:
        todo["note"] = note
    if done is not None:
        todo["done"] = bool(done)
    _save(data)
    return todo


def delete_todo(tid: int) -> bool:
    """Remove a TODO. Returns True if it existed. (Reference guard is enforced in service.)"""
    data = _load()
    key = str(int(tid))
    if key not in data["todos"]:
        return False
    del data["todos"][key]
    _save(data)
    return True
