"""Idea bubbles — topic groupings that own a Markdown report.

A bubble has a slug (filesystem key) and a display name. Bubbles are tracked in a registry
(``bubbles.yaml``) so they can carry approval state + report instructions and exist
*standalone* (no PDFs yet). A bubble is also surfaced if any PDF carries its slug, so tags
applied directly to a PDF still produce a bubble.

Report generation is gated: a bubble must be ``approved`` before a report can be generated.
Auto-suggested bubbles (from the tagger) start ``approved=False`` and wait in the UI.

All paths resolve against the active per-user context root.
"""
from __future__ import annotations

import json as _json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

import yaml
from slugify import slugify

from . import assets, paths


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def load_registry() -> dict:
    path = paths.BUBBLES_YAML
    if not path.exists():
        return {}
    return (yaml.safe_load(path.read_text()) or {}).get("bubbles", {})


def save_registry(reg: dict) -> None:
    _atomic_write(paths.BUBBLES_YAML, yaml.safe_dump({"bubbles": reg}, sort_keys=True,
                                                     allow_unicode=True))


def propose_bubble(name: str) -> str:
    """Register an auto-suggested bubble (approved=False). Idempotent; never downgrades approval."""
    slug = slugify(name)
    if not slug:
        return slug
    reg = load_registry()
    if slug not in reg:
        reg[slug] = {"name": name.strip(), "approved": False, "instructions": "",
                     "created_at": _now_iso()}
        save_registry(reg)
    return slug


def create_bubble(name: str) -> str:
    """User-created standalone bubble (approved=True)."""
    slug = slugify(name)
    if not slug:
        raise ValueError("Bubble name must contain letters or numbers.")
    reg = load_registry()
    entry = reg.get(slug, {})
    reg[slug] = {"name": name.strip(), "approved": True,
                 "instructions": entry.get("instructions", ""),
                 "created_at": entry.get("created_at", _now_iso())}
    save_registry(reg)
    return slug


def approve_bubble(slug: str, instructions: str = "") -> dict:
    reg = load_registry()
    entry = reg.get(slug)
    if entry is None:
        # bubble exists only via PDF tags — materialize it in the registry
        entry = {"name": slug_to_name(slug), "approved": True, "instructions": instructions,
                 "created_at": _now_iso()}
    else:
        entry["approved"] = True
        if instructions:
            entry["instructions"] = instructions
    reg[slug] = entry
    save_registry(reg)
    return entry


def rename_bubble(slug: str, new_name: str) -> dict:
    """Update the display name of a bubble (slug stays the same)."""
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("Bubble name cannot be empty.")
    reg = load_registry()
    if slug not in reg:
        raise KeyError(f"Bubble {slug!r} not found.")
    reg[slug]["name"] = new_name
    save_registry(reg)
    return reg[slug]


def delete_bubble(slug: str) -> None:
    """Remove a bubble from the registry, delete its reports, and strip its tag from all PDFs."""
    import shutil

    # Remove from registry
    reg = load_registry()
    reg.pop(slug, None)
    save_registry(reg)

    # Delete the reports directory (pages + assets)
    bdir = paths.bubble_dir(slug)
    if bdir.exists():
        shutil.rmtree(bdir)

    # Strip the slug from every PDF's idea_bubbles + tags
    for m in assets.list_assets():
        if slug in m.get("idea_bubbles", []):
            idx = m["idea_bubbles"].index(slug)
            m["idea_bubbles"].remove(slug)
            # also remove the corresponding display tag (same index if lists are in sync)
            if idx < len(m.get("tags", [])):
                m["tags"].pop(idx)
            else:
                # fall back: remove any tag whose slug matches
                m["tags"] = [t for t in m.get("tags", []) if slugify(t) != slug]
            assets.save_meta(m["pdf_id"], m)


def is_approved(slug: str) -> bool:
    return bool(load_registry().get(slug, {}).get("approved"))


def get_instructions(slug: str) -> str:
    return load_registry().get(slug, {}).get("instructions", "")


# --------------------------------------------------------------------------- #
# Derivation / listing
# --------------------------------------------------------------------------- #
def _pdf_slug_names() -> dict[str, str]:
    """Map slug -> a display name, derived from PDF tags."""
    out: dict[str, str] = {}
    for m in assets.list_assets():
        tags = m.get("tags", [])
        bubbles = m.get("idea_bubbles", [])
        for i, slug in enumerate(bubbles):
            if slug and slug not in out:
                out[slug] = tags[i] if i < len(tags) else slug
    return out


def slug_to_name(slug: str) -> str:
    reg = load_registry()
    if slug in reg and reg[slug].get("name"):
        return reg[slug]["name"]
    return _pdf_slug_names().get(slug, slug)


def pdfs_for_bubble(slug: str) -> list[dict]:
    return [m for m in assets.list_assets() if slug in m.get("idea_bubbles", [])]


def all_bubbles() -> list[dict]:
    """Union of registry bubbles and PDF-derived bubbles."""
    reg = load_registry()
    pdf_names = _pdf_slug_names()
    counts: dict[str, int] = {}
    for m in assets.list_assets():
        for slug in m.get("idea_bubbles", []):
            counts[slug] = counts.get(slug, 0) + 1

    slugs = set(reg) | set(pdf_names)
    out = []
    for slug in sorted(slugs):
        entry = reg.get(slug, {})
        name = entry.get("name") or pdf_names.get(slug, slug)
        out.append({
            "slug": slug,
            "name": name,
            "approved": bool(entry.get("approved", False)),
            "in_registry": slug in reg,
            "pdf_count": counts.get(slug, 0),
            "page_count": _page_count(slug),
            "instructions": entry.get("instructions", ""),
        })
    return out


def bubble_detail(slug: str) -> dict:
    reg = load_registry()
    entry = reg.get(slug, {})
    pdfs = pdfs_for_bubble(slug)
    approved = bool(entry.get("approved", False))
    pages, home, content = [], "", ""
    if approved:
        ensure_pages(slug)
        pages = list_pages(slug)
        home = manifest(slug).get("home", pages[0]["page_slug"] if pages else "")
        content = get_page(slug, home) if home else ""
    return {
        "slug": slug,
        "name": entry.get("name") or slug_to_name(slug),
        "approved": approved,
        "in_registry": slug in reg,
        "instructions": entry.get("instructions", ""),
        "pdf_count": len(pdfs),
        "page_count": _page_count(slug),
        "assets": pdfs,
        "images": list_bubble_images(slug),
        "pages": pages,
        "home": home,
        "page": home,
        "content": content,
    }


# --------------------------------------------------------------------------- #
# Pages — a per-bubble mini-wiki of markdown files (v2)
# --------------------------------------------------------------------------- #
def manifest(slug: str) -> dict:
    p = paths.bubble_manifest_path(slug)
    if not p.exists():
        return {"home": "", "pages": []}
    data = yaml.safe_load(p.read_text()) or {}
    data.setdefault("home", "")
    data.setdefault("pages", [])
    return data


def _save_manifest(slug: str, data: dict) -> None:
    _atomic_write(paths.bubble_manifest_path(slug),
                  yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def _page_count(slug: str) -> int:
    return len(manifest(slug).get("pages", []))


def _unique_page_slug(slug: str, title: str) -> str:
    base = slugify(title) or "page"
    existing = {p["page_slug"] for p in manifest(slug).get("pages", [])}
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def ensure_pages(slug: str) -> None:
    """Idempotent: migrate a legacy report.md into pages/, else seed a default Overview page."""
    if paths.bubble_manifest_path(slug).exists():
        return
    paths.bubble_pages_dir(slug).mkdir(parents=True, exist_ok=True)
    legacy = paths.bubble_report_path(slug)
    if legacy.exists():
        _atomic_write(paths.bubble_page_path(slug, "overview"), legacy.read_text())
        try:
            legacy.unlink()
        except OSError:
            pass
    else:
        name = slug_to_name(slug)
        _atomic_write(paths.bubble_page_path(slug, "overview"), f"# {name}\n\n")
    _save_manifest(slug, {"home": "overview", "pages": [{"page_slug": "overview",
                                                         "title": "Overview"}]})


def list_pages(slug: str) -> list[dict]:
    ensure_pages(slug)
    return manifest(slug).get("pages", [])


def get_page(slug: str, page_slug: str) -> str:
    p = paths.bubble_page_path(slug, page_slug)
    return p.read_text() if p.exists() else ""


_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def normalize_wikilinks(slug: str, content: str) -> str:
    """Rewrite ``[[X]]`` link targets to real page slugs.

    Models (especially small local ones) guess link targets — they invent path-like
    forms (``[[Key Papers/Some Title]]``) or write the human title (``[[Some Title]]``)
    instead of the slug the server assigned. This resolves each target against the
    bubble's page list: a leading ``prefix/`` is stripped, then the target is matched
    by slug first, then by title (case-insensitive). Unresolved targets are left as-is
    (prefix-stripped) so the user's literal text survives. Idempotent.
    """
    try:
        pages = manifest(slug).get("pages", [])
    except Exception:  # noqa: BLE001 — never let link cleanup break a save
        return content
    slugs = {p["page_slug"] for p in pages}
    by_title = {p["title"].strip().lower(): p["page_slug"] for p in pages}

    def repl(m: "re.Match") -> str:
        target = m.group(1).strip()
        if "/" in target:                      # drop invented "Section/Title" prefixes
            target = target.split("/")[-1].strip()
        if target in slugs:
            return f"[[{target}]]"
        hit = by_title.get(target.lower())
        return f"[[{hit}]]" if hit else f"[[{target}]]"

    return _WIKILINK_RE.sub(repl, content)


def save_page(slug: str, page_slug: str, content: str) -> None:
    _atomic_write(paths.bubble_page_path(slug, page_slug), normalize_wikilinks(slug, content))


def create_page(slug: str, title: str) -> str:
    ensure_pages(slug)
    page_slug = _unique_page_slug(slug, title)
    _atomic_write(paths.bubble_page_path(slug, page_slug), f"# {title.strip()}\n\n")
    data = manifest(slug)
    data["pages"].append({"page_slug": page_slug, "title": title.strip() or page_slug})
    _save_manifest(slug, data)
    return page_slug


def rename_page(slug: str, page_slug: str, title: str) -> None:
    data = manifest(slug)
    for p in data["pages"]:
        if p["page_slug"] == page_slug:
            p["title"] = title.strip() or page_slug
    _save_manifest(slug, data)


def delete_page(slug: str, page_slug: str) -> bool:
    data = manifest(slug)
    if page_slug == data.get("home"):
        raise ValueError("Cannot delete the home page.")
    before = len(data["pages"])
    data["pages"] = [p for p in data["pages"] if p["page_slug"] != page_slug]
    if len(data["pages"]) == before:
        return False
    _save_manifest(slug, data)
    try:
        paths.bubble_page_path(slug, page_slug).unlink()
    except OSError:
        pass
    return True


# --------------------------------------------------------------------------- #
# Figures / images
# --------------------------------------------------------------------------- #
def save_bubble_image(slug: str, filename: str, data: bytes) -> str:
    """Save an uploaded image under assets/ with a safe unique name; return its URL."""
    adir = paths.bubble_assets_dir(slug)
    adir.mkdir(parents=True, exist_ok=True)
    stem = slugify(Path(filename).stem) or "image"
    ext = (Path(filename).suffix or ".png").lower()
    name = f"{stem}{ext}"
    i = 2
    while (adir / name).exists():
        name = f"{stem}-{i}{ext}"
        i += 1
    (adir / name).write_bytes(data)
    return f"/api/bubbles/{slug}/assets/{name}"


def list_bubble_images(slug: str) -> list[str]:
    adir = paths.bubble_assets_dir(slug)
    if not adir.exists():
        return []
    return sorted(f"/api/bubbles/{slug}/assets/{p.name}" for p in adir.iterdir() if p.is_file())


# --------------------------------------------------------------------------- #
# Chat session persistence
# --------------------------------------------------------------------------- #
def _chats_dir(slug: str) -> Path:
    return paths.bubble_chats_dir(slug)


def _session_path(slug: str, session_id: str) -> Path:
    safe = slugify(session_id, separator="-") or session_id.replace("/", "-")
    return _chats_dir(slug) / f"{safe}.json"


def list_chat_sessions(slug: str) -> list[dict]:
    d = _chats_dir(slug)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json"), reverse=True):
        try:
            data = _json.loads(p.read_text())
            msgs = data.get("messages", [])
            preview = next((m["content"][:80] for m in msgs if m.get("role") == "user"), "")
            out.append({"id": data.get("id", p.stem), "title": data.get("title", "Chat"),
                        "created_at": data.get("created_at", ""), "preview": preview})
        except Exception:  # noqa: BLE001
            continue
    return out


def get_chat_session(slug: str, session_id: str) -> dict | None:
    p = _session_path(slug, session_id)
    if not p.exists():
        return None
    try:
        return _json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None


def save_chat_session(slug: str, session_id: str, title: str, messages: list[dict]) -> None:
    d = _chats_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    path = _session_path(slug, session_id)
    data = {"id": session_id, "title": title,
            "created_at": _now_iso(), "messages": messages}
    _atomic_write(path, _json.dumps(data, ensure_ascii=False, indent=2))


def delete_chat_session(slug: str, session_id: str) -> bool:
    p = _session_path(slug, session_id)
    if not p.exists():
        return False
    p.unlink()
    return True
