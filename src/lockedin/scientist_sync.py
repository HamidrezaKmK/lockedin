"""Safe filesystem snapshot primitives for installed Scientist clients.

The browser remains the source of truth.  This module deliberately exports only
research content, never account data, sessions, or provider credentials.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from slugify import slugify

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
    register_orphan_pages(home)
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
    """Only existing approved report pages and bubble images may be pushed."""
    parts = Path(rel).parts
    # Check lexical components before checking the REPORTS layout.  ``Path.resolve`` protects
    # against escaping ``home`` later, but a path such as REPORTS/slug/pages/../../config/x.md
    # would otherwise look like a permitted page to the shape test below.
    if any(part in ("", ".", "..") for part in parts):
        return False
    if len(parts) != 4 or parts[0] != "REPORTS":
        return False
    slug = parts[1]
    with paths.use_root(home):
        approved = any(b["slug"] == slug and b.get("approved") for b in bubbles.all_bubbles())
    if not approved:
        return False
    if parts[2] == "assets":
        return True
    if parts[2] != "pages" or not rel.endswith(".md"):
        return False
    page_slug = Path(parts[3]).stem
    with paths.use_root(home):
        return any(p["page_slug"] == page_slug for p in bubbles.list_pages(slug))


def _approved_page(home: Path, slug: str, page_slug: str) -> bool:
    if not slug or not page_slug or Path(page_slug).name != page_slug:
        return False
    with paths.use_root(home):
        approved = any(b["slug"] == slug and b.get("approved") for b in bubbles.all_bubbles())
    return approved


def register_page(home: Path, slug: str, page_slug: str, content_b64: str,
                  base_revision: str) -> dict:
    """Atomically create a Scientist page and its manifest entry, or return a conflict."""
    rel = f"REPORTS/{slug}/pages/{page_slug}.md"
    if not _approved_page(home, slug, page_slug):
        return {"applied": [], "conflicts": [{"path": rel, "reason": "read-only or invalid scientist page"}]}
    try:
        raw = base64.b64decode(content_b64, validate=True)
        content = raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return {"applied": [], "conflicts": [{"path": rel, "reason": "invalid content"}]}
    with paths.use_root(home):
        target = paths.bubble_page_path(slug, page_slug)
        current = target.read_bytes() if target.exists() else b""
        if base_revision != revision(current):
            conflict = {"path": rel, "reason": "stale revision", "revision": revision(current),
                        "content_b64": base64.b64encode(current).decode("ascii")}
            return {"applied": [], "conflicts": [conflict]}
        try:
            bubbles.register_page(slug, page_slug, content)
        except ValueError as exc:
            conflict = {"path": rel, "reason": str(exc), "revision": revision(current),
                        "content_b64": base64.b64encode(current).decode("ascii")}
            return {"applied": [], "conflicts": [conflict]}
        stored = target.read_bytes()
    return {"applied": [{"path": rel, "revision": revision(stored),
                         "content_b64": base64.b64encode(stored).decode("ascii")}], "conflicts": []}


def register_orphan_pages(home: Path) -> None:
    """Make old Scientist-created Markdown files visible without trusting manifest edits."""
    with paths.use_root(home):
        for bubble in bubbles.all_bubbles():
            slug = bubble["slug"]
            if not bubble.get("approved"):
                continue
            pages = {p["page_slug"] for p in bubbles.list_pages(slug)}
            for path in paths.bubble_pages_dir(slug).glob("*.md"):
                page_slug = path.stem
                if page_slug in pages or slugify(page_slug) != page_slug:
                    continue
                try:
                    bubbles.register_page(slug, page_slug, path.read_text())
                    pages.add(page_slug)
                except (OSError, UnicodeDecodeError, ValueError):
                    continue


def apply_deletes(home: Path, deletes: list[dict]) -> dict:
    """Apply revision-guarded deletions of report pages and figures.

    Removing the Markdown file is not enough: a page also has a manifest entry, so this routes
    through ``bubbles.delete_page`` and keeps ``pages.yaml`` authoritative.  Conflicts return the
    current content so a client can restore its mirror rather than lose the file locally.
    """
    conflicts, applied = [], []
    for item in deletes:
        rel = str(item.get("path", ""))
        if not writable_path(home, rel):
            conflicts.append({"path": rel, "reason": "read-only or invalid scientist path"})
            continue
        target = _target(home, rel)
        current = target.read_bytes() if target.exists() else b""
        if str(item.get("base_revision", "")) != revision(current):
            conflicts.append({"path": rel, "reason": "stale revision", "revision": revision(current),
                              "content_b64": base64.b64encode(current).decode("ascii")})
            continue
        parts = Path(rel).parts
        slug = parts[1]
        with paths.use_root(home):
            if parts[2] == "pages":
                try:
                    removed = bubbles.delete_page(slug, Path(parts[3]).stem)
                except ValueError as exc:  # the home page is deliberately undeletable
                    conflicts.append({"path": rel, "reason": str(exc), "revision": revision(current),
                                      "content_b64": base64.b64encode(current).decode("ascii")})
                    continue
            else:
                removed = bubbles.delete_bubble_asset(slug, parts[3])
                if removed:
                    bubbles.touch_bubble(slug)
        if not removed:
            conflicts.append({"path": rel, "reason": "no such scientist file"})
            continue
        applied.append({"path": rel})
    return {"applied": applied, "conflicts": conflicts}


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
            if Path(rel).suffix.lower() == ".gif":
                raw = bubbles.ensure_looping_gif(raw)
        except Exception:
            conflicts.append({"path": rel, "reason": "invalid content"})
            continue
        current = target.read_bytes() if target.exists() else b""
        base = str(item.get("base_revision", ""))
        if base != revision(current):
            conflicts.append({"path": rel, "reason": "stale revision", "revision": revision(current),
                              "content_b64": base64.b64encode(current).decode("ascii")})
            continue
        # A blank local mirror must never silently erase a populated report page or figure.
        # Clearing content remains possible in the web editor, where it is an explicit action;
        # Scientist clients receive a conflict and can recover their mirror instead.
        if not raw and current:
            conflicts.append({"path": rel, "reason": "refusing to replace non-empty content with an empty sync write",
                              "revision": revision(current),
                              "content_b64": base64.b64encode(current).decode("ascii")})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(raw)
        tmp.replace(target)
        applied.append({"path": rel, "revision": revision(raw)})
    return {"applied": applied, "conflicts": conflicts}
