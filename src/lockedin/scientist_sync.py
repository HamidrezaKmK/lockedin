"""Bubble-scoped filesystem primitives for project-local Scientist clients.

The v2 protocol exports one approved bubble at a time.  Paper assets and editing
configuration are read-only; only report pages and figures accept client writes.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from slugify import slugify

from . import assets, bubbles, paths


def revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _approved(slug: str) -> bool:
    return bool(bubbles.load_registry().get(slug, {}).get("approved"))


def _safe_rel(rel: str) -> bool:
    parts = Path(rel).parts
    return bool(parts) and not any(part in ("", ".", "..") for part in parts)


def _files(home: Path, slug: str) -> dict[str, Path]:
    """Map v2 project-local paths to source files for one bubble."""
    with paths.use_root(home):
        if not _approved(slug):
            raise KeyError(slug)
        out: dict[str, Path] = {}
        report = paths.bubble_dir(slug)
        if report.exists():
            for path in report.rglob("*"):
                if not path.is_file() or path.name.endswith(".tmp"):
                    continue
                rel = path.relative_to(report)
                # Chats and private review comments do not belong in an agent project.
                if rel.parts and rel.parts[0] in {"chats", "comments"}:
                    continue
                if rel.as_posix() == "_lockedin_overleaf.yaml":
                    out["config/overleaf.yaml"] = path
                    continue
                out[(Path("reports") / rel).as_posix()] = path
        for meta in bubbles.pdfs_for_bubble(slug):
            pdf_id = str(meta.get("pdf_id") or "")
            if not pdf_id:
                continue
            root = paths.asset_dir(pdf_id)
            for name in ("paper.pdf", "meta.yaml", "text.txt", "summary.md"):
                path = root / name
                if path.is_file():
                    out[(Path("assets") / pdf_id / name).as_posix()] = path
        for name, path in (("math.yaml", paths.MATH_CONFIG_YAML),
                           ("aesthetics.yaml", paths.AESTHETICS_CONFIG_YAML)):
            if path.is_file():
                out[(Path("config") / name).as_posix()] = path
        return out


def manifest(home: Path, slug: str) -> dict:
    return {"files": [{"path": rel, "revision": revision(path.read_bytes())}
                      for rel, path in sorted(_files(home, slug).items())]}


def read_files(home: Path, slug: str, wanted: list[str]) -> dict:
    available = _files(home, slug)
    files = []
    for rel in dict.fromkeys(wanted):
        if not _safe_rel(rel) or rel not in available:
            continue
        raw = available[rel].read_bytes()
        files.append({"path": rel, "revision": revision(raw),
                      "content_b64": base64.b64encode(raw).decode("ascii")})
    return {"files": files}


def writable_path(slug: str, rel: str, *, existing_pages: bool = True) -> bool:
    """Whether a local v2 report file can be pushed to this exact bubble."""
    if not _safe_rel(rel):
        return False
    parts = Path(rel).parts
    if len(parts) != 3 or parts[0] != "reports":
        return False
    if parts[1] == "assets":
        return Path(parts[2]).name == parts[2]
    if parts[1] != "pages" or not rel.endswith(".md"):
        return False
    page_slug = Path(parts[2]).stem
    return bool(page_slug and slugify(page_slug) == page_slug)


def _server_path(slug: str, rel: str) -> Path:
    parts = Path(rel).parts
    if parts[1] == "assets":
        return paths.bubble_assets_dir(slug) / parts[2]
    return paths.bubble_page_path(slug, Path(parts[2]).stem)


def apply_writes(home: Path, slug: str, writes: list[dict]) -> dict:
    conflicts, applied = [], []
    with paths.use_root(home):
        if not _approved(slug):
            return {"applied": [], "conflicts": [{"path": "", "reason": "unknown or unapproved bubble"}]}
        pages = {p["page_slug"] for p in bubbles.list_pages(slug)}
        for item in writes:
            rel = str(item.get("path", ""))
            if not writable_path(slug, rel) or (Path(rel).parts[1] == "pages" and Path(rel).stem not in pages):
                conflicts.append({"path": rel, "reason": "read-only or invalid Scientist path"})
                continue
            try:
                raw = base64.b64decode(str(item.get("content_b64", "")), validate=True)
            except Exception:
                conflicts.append({"path": rel, "reason": "invalid content"}); continue
            target = _server_path(slug, rel)
            current = target.read_bytes() if target.exists() else b""
            if str(item.get("base_revision", "")) != revision(current):
                conflicts.append({"path": rel, "reason": "stale revision", "revision": revision(current),
                                  "content_b64": base64.b64encode(current).decode("ascii")}); continue
            if not raw and current:
                conflicts.append({"path": rel, "reason": "refusing to replace non-empty content with an empty sync write",
                                  "revision": revision(current), "content_b64": base64.b64encode(current).decode("ascii")}); continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp"); tmp.write_bytes(raw); tmp.replace(target)
            if Path(rel).parts[1] == "assets": bubbles.touch_bubble(slug)
            applied.append({"path": rel, "revision": revision(raw)})
    return {"applied": applied, "conflicts": conflicts}


def register_page(home: Path, slug: str, page_slug: str, content_b64: str, base_revision: str) -> dict:
    rel = f"reports/pages/{page_slug}.md"
    if not writable_path(slug, rel):
        return {"applied": [], "conflicts": [{"path": rel, "reason": "read-only or invalid Scientist page"}]}
    try:
        raw = base64.b64decode(content_b64, validate=True); content = raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return {"applied": [], "conflicts": [{"path": rel, "reason": "invalid content"}]}
    with paths.use_root(home):
        if not _approved(slug):
            return {"applied": [], "conflicts": [{"path": rel, "reason": "unknown or unapproved bubble"}]}
        target = paths.bubble_page_path(slug, page_slug)
        current = target.read_bytes() if target.exists() else b""
        if base_revision != revision(current):
            conflict = {"path": rel, "reason": "stale revision", "revision": revision(current),
                        "content_b64": base64.b64encode(current).decode("ascii")}
            return {"applied": [], "conflicts": [conflict]}
        try: bubbles.register_page(slug, page_slug, content)
        except ValueError as exc:
            conflict = {"path": rel, "reason": str(exc), "revision": revision(current),
                        "content_b64": base64.b64encode(current).decode("ascii")}
            return {"applied": [], "conflicts": [conflict]}
        stored = target.read_bytes()
    return {"applied": [{"path": rel, "revision": revision(stored), "content_b64": base64.b64encode(stored).decode("ascii")}], "conflicts": []}


def apply_deletes(home: Path, slug: str, deletes: list[dict]) -> dict:
    conflicts, applied = [], []
    with paths.use_root(home):
        if not _approved(slug):
            return {"applied": [], "conflicts": [{"path": "", "reason": "unknown or unapproved bubble"}]}
        for item in deletes:
            rel = str(item.get("path", ""))
            if not writable_path(slug, rel):
                conflicts.append({"path": rel, "reason": "read-only or invalid Scientist path"}); continue
            target = _server_path(slug, rel); current = target.read_bytes() if target.exists() else b""
            if str(item.get("base_revision", "")) != revision(current):
                conflicts.append({"path": rel, "reason": "stale revision", "revision": revision(current),
                                  "content_b64": base64.b64encode(current).decode("ascii")}); continue
            if Path(rel).parts[1] == "pages":
                try: removed = bubbles.delete_page(slug, Path(rel).stem)
                except ValueError as exc:
                    conflicts.append({"path": rel, "reason": str(exc), "revision": revision(current),
                                      "content_b64": base64.b64encode(current).decode("ascii")}); continue
            else:
                removed = bubbles.delete_bubble_asset(slug, Path(rel).name)
                if removed: bubbles.touch_bubble(slug)
            if removed: applied.append({"path": rel})
            else: conflicts.append({"path": rel, "reason": "no such Scientist file"})
    return {"applied": applied, "conflicts": conflicts}
