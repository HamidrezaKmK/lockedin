"""Safe filesystem snapshot primitives for installed Scientist clients.

The browser remains the source of truth.  This module deliberately exports only
research content, never account data, sessions, or provider credentials.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from . import bubbles, paths

_ROOT_FILES = ("bubbles.yaml", "todos.yaml", "config/math.yaml", "config/aesthetics.yaml")
_REVISION_CACHE: dict[str, tuple[int, int, str]] = {}


def revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_revision(path: Path) -> str:
    """Hash only files whose metadata changed since the last manifest request."""
    stat = path.stat()
    key = str(path)
    cached = _REVISION_CACHE.get(key)
    fingerprint = (stat.st_mtime_ns, stat.st_size)
    if cached and cached[:2] == fingerprint:
        return cached[2]
    value = revision(path.read_bytes())
    _REVISION_CACHE[key] = (*fingerprint, value)
    return value


def _safe_files(home: Path) -> list[Path]:
    out: list[Path] = []
    for rel in _ROOT_FILES:
        p = home / rel
        if p.is_file():
            out.append(p)
    for base in (home / "REPORTS", home / "ASSETS"):
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.name.endswith(".tmp") or p.name == "paper.pdf":
                continue
            # Chats are not useful agent workspace state and can be very sensitive/noisy.
            if "chats" in p.relative_to(home).parts:
                continue
            out.append(p)
    return sorted(out)


def snapshot(home: Path) -> dict:
    """Return a complete safe workspace snapshot, with opaque content revisions."""
    files = []
    for p in _safe_files(home):
        raw = p.read_bytes()
        files.append({"path": p.relative_to(home).as_posix(), "revision": revision(raw),
                      "content_b64": base64.b64encode(raw).decode("ascii")})
    return {"files": files}


def manifest(home: Path) -> dict:
    """Return a lightweight path/revision list for incremental clients."""
    return {"files": [{"path": p.relative_to(home).as_posix(), "revision": _file_revision(p)}
                      for p in _safe_files(home)]}


def read_files(home: Path, wanted: list[str]) -> dict:
    """Return content only for requested files that are currently safe to synchronize."""
    by_path = {p.relative_to(home).as_posix(): p for p in _safe_files(home)}
    files = []
    for rel in dict.fromkeys(wanted):
        p = by_path.get(rel)
        if not p:
            continue
        raw = p.read_bytes()
        files.append({"path": rel, "revision": _file_revision(p),
                      "content_b64": base64.b64encode(raw).decode("ascii")})
    return {"files": files}


def _target(home: Path, rel: str) -> Path:
    p = (home / rel).resolve()
    if p == home.resolve() or home.resolve() not in p.parents:
        raise ValueError("Invalid workspace path.")
    return p


def writable_path(home: Path, rel: str) -> bool:
    """Only selected approved bubble report pages and bubble images may be pushed."""
    parts = Path(rel).parts
    if len(parts) < 4 or parts[0] != "REPORTS":
        return False
    slug = parts[1]
    with paths.use_root(home):
        approved = any(b["slug"] == slug and b.get("approved") for b in bubbles.all_bubbles())
    if not approved:
        return False
    return (parts[2] == "pages" and rel.endswith(".md")) or parts[2] == "assets"


def apply_writes(home: Path, writes: list[dict]) -> dict:
    """Apply revision-guarded writes; conflicts return current content without mutation."""
    conflicts, applied = [], []
    for item in writes:
        rel = str(item.get("path", ""))
        if not writable_path(home, rel):
            conflicts.append({"path": rel, "reason": "read-only or invalid scientist path"})
            continue
        try:
            target = _target(home, rel)
            raw = base64.b64decode(str(item.get("content_b64", "")), validate=True)
        except Exception:
            conflicts.append({"path": rel, "reason": "invalid content"})
            continue
        current = target.read_bytes() if target.exists() else b""
        base = str(item.get("base_revision", ""))
        if base != revision(current):
            conflicts.append({"path": rel, "reason": "stale revision", "revision": revision(current),
                              "content_b64": base64.b64encode(current).decode("ascii")})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(raw)
        tmp.replace(target)
        applied.append({"path": rel, "revision": revision(raw)})
    return {"applied": applied, "conflicts": conflicts}
