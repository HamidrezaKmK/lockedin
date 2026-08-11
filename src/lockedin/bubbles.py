"""Bubbles — topic groupings that own a Markdown report.

A bubble has a slug (filesystem key) and a display name. Bubbles are tracked in a registry
(``bubbles.yaml``) so they can carry approval state + report instructions and exist
*standalone* (no PDFs yet). A bubble is also surfaced if any PDF carries its slug, so tags
applied directly to a PDF still produce a bubble.

Report generation is gated: a bubble must be ``approved`` before a report can be generated.
New bubbles created in LockedIn are approved immediately. The ``approved`` flag remains as a
write-safety boundary for older or externally-created registry entries.

All paths resolve against the active per-user context root.
"""
from __future__ import annotations

import hashlib
import json as _json
import logging
import os
import re
import secrets
import shutil
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml
from slugify import slugify

from . import assets, paths


logger = logging.getLogger(__name__)
_CANONICAL_REVIEW_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

try:  # POSIX only; the in-process lock below remains the portable fallback.
    import fcntl
except ImportError:  # pragma: no cover - exercised on non-POSIX hosts
    fcntl = None


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


_OVERLEAF_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,128}$")


def normalize_overleaf_project(value: str) -> str:
    """Accept an Overleaf Cloud project ID, project URL, or Git URL without credentials."""
    raw = (value or "").strip()
    if _OVERLEAF_ID_RE.fullmatch(raw):
        return raw
    parsed = urlparse(raw)
    if parsed.scheme != "https" or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Enter an Overleaf Cloud project link, Git link, or project ID.")
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    project_id = ""
    if host in {"www.overleaf.com", "overleaf.com"} and parsed.username is None and len(parts) == 2 and parts[0] == "project":
        project_id = parts[1]
    elif host == "git.overleaf.com" and parsed.username in {None, "git"} and len(parts) == 1:
        project_id = parts[0]
    if not _OVERLEAF_ID_RE.fullmatch(project_id):
        raise ValueError("Enter an Overleaf Cloud project link, Git link, or project ID.")
    return project_id


def overleaf_urls(project_id: str | None) -> dict:
    if not project_id:
        return {"overleaf_project_id": None, "overleaf_url": "", "overleaf_git_url": ""}
    return {"overleaf_project_id": project_id,
            "overleaf_url": f"https://www.overleaf.com/project/{project_id}",
            "overleaf_git_url": f"https://git@git.overleaf.com/{project_id}"}


def migrate_overleaf_fields() -> int:
    """Add the optional field without changing existing bubble metadata or timestamps."""
    reg = load_registry(); changed = 0
    for entry in reg.values():
        if "overleaf_project_id" not in entry:
            entry["overleaf_project_id"] = None; changed += 1
    if changed:
        save_registry(reg)
    return changed


def set_overleaf_project(slug: str, value: str | None) -> dict:
    reg = load_registry(); entry = reg.get(slug)
    if entry is None:
        raise KeyError(f"Bubble {slug!r} not found.")
    entry["overleaf_project_id"] = normalize_overleaf_project(value) if value else None
    reg[slug] = entry; save_registry(reg)
    _write_overleaf_config(slug, entry["overleaf_project_id"])
    return overleaf_urls(entry["overleaf_project_id"])


def _write_overleaf_config(slug: str, project_id: str | None) -> None:
    """Materialize the server-owned Scientist export without exposing it as a report file."""
    target = paths.bubble_dir(slug) / "_lockedin_overleaf.yaml"
    if not project_id:
        target.unlink(missing_ok=True)
        return
    # JSON is valid YAML and keeps the dependency-free Scientist client able to read this config.
    _atomic_write(target, _json.dumps(overleaf_urls(project_id), indent=2, sort_keys=True) + "\n")


def touch_bubble(slug: str, *, when: "str | None" = None) -> None:
    """Update a bubble's last-edited timestamp, materializing PDF-derived bubbles if needed."""
    reg = load_registry()
    entry = reg.get(slug)
    if entry is None:
        entry = {"name": slug_to_name(slug), "approved": False, "archived": False, "instructions": "",
                 "created_at": _now_iso(), "overleaf_project_id": None}
    entry["last_edited_at"] = when or _now_iso()
    reg[slug] = entry
    save_registry(reg)


def write_citation_file(slug: str, asset_metas: "list[dict] | None" = None) -> None:
    """Write this bubble's generated paper inventory for scientist sessions.

    The implementation name is retained for compatibility, but the generated artifact is an
    asset inventory. BibTeX is optional metadata, not the criterion for whether a paper belongs
    in a scientist session.
    """
    title = slug_to_name(slug)
    lines = [
        f"# Attached Papers & Citation BibTeX for {title}",
        "",
        "This file is generated by lockedin for scientist sessions.",
        "Every asset attached to this bubble is listed below, ordered by relevance.",
        "Use a BibTeX key with \\cite{key} only when that asset provides one.",
        "Do not edit this file; edit asset BibTeX from the lockedin web UI.",
        "",
    ]
    attached = []
    for meta in asset_metas if asset_metas is not None else assets.list_assets():
        if slug not in (meta.get("idea_bubbles") or []):
            continue
        score = assets.bubble_scores(meta).get(slug, 5)
        attached.append((int(score), meta))
    attached.sort(key=lambda item: (-item[0], (item[1].get("title") or item[1].get("filename") or "").lower()))
    for score, meta in attached:
        bibtex = str(meta.get("bibliography") or "").strip()
        pdf_id = meta.get("pdf_id") or "unknown-asset"
        asset_title = meta.get("title") or meta.get("filename") or pdf_id
        lines.extend([f"## [Relevance {score}] {asset_title}", "", f"- Asset id: `{pdf_id}`"])
        if meta.get("url_source"):
            lines.append(f"- Source URL: {meta['url_source']}")
        if bibtex:
            lines.extend(["", "```bibtex", *bibtex.splitlines(), "```"])
        else:
            lines.append("- No BibTeX is saved for this asset yet; do not invent a citation key.")
        lines.append("")
    if not attached:
        lines.append("_No assets are attached to this bubble._")
        lines.append("")
    path = paths.bubble_dir(slug) / "_lockedin_papers.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    legacy = paths.bubble_dir(slug) / "_lockedin_citations.md"
    if legacy.exists():
        legacy.unlink()


def refresh_citation_files(slugs: "set[str] | list[str] | None" = None) -> None:
    """Refresh generated citation inventory files for selected or all approved bubbles."""
    asset_metas = assets.list_assets()
    if slugs is None:
        targets = [b["slug"] for b in all_bubbles() if b.get("approved")]
    else:
        targets = sorted({s for s in slugs if s})
    for slug in targets:
        write_citation_file(slug, asset_metas)


def propose_bubble(name: str) -> str:
    """Register an auto-suggested bubble (approved=False). Idempotent; never downgrades approval."""
    slug = slugify(name)
    if not slug:
        return slug
    reg = load_registry()
    if slug not in reg:
        now = _now_iso()
        reg[slug] = {"name": name.strip(), "approved": False, "archived": False, "instructions": "",
                     "created_at": now, "last_edited_at": now}
        save_registry(reg)
    return slug


def purge_legacy_auto_suggestions() -> list[str]:
    """Remove obsolete, unapproved auto-suggestion records from this workspace.

    LockedIn no longer proposes bubbles automatically. Those old records have no user-facing
    purpose, but a direct link to one could still expose the obsolete approval screen. They
    never represent a user-created bubble (normal creation approves immediately), so remove
    their registry entries and any report folders they may have materialized.
    """
    reg = load_registry()
    stale = sorted(slug for slug, entry in reg.items() if not entry.get("approved"))
    if not stale:
        return []
    for slug in stale:
        reg.pop(slug, None)
        bubble_root = paths.bubble_dir(slug)
        if bubble_root.exists():
            shutil.rmtree(bubble_root)
    save_registry(reg)
    return stale


def create_bubble(name: str) -> str:
    """User-created standalone bubble (approved=True).

    If the slug already exists, keep its existing entry untouched except to ensure it's approved —
    do NOT overwrite the display ``name`` (a rename) or share state. The ``name`` here is only the
    seed for a *new* bubble; for an existing one, identity is the slug and the name is display-only.
    """
    slug = slugify(name)
    if not slug:
        raise ValueError("Bubble name must contain letters or numbers.")
    reg = load_registry()
    entry = reg.get(slug)
    if entry is None:
        now = _now_iso()
        reg[slug] = {"name": name.strip(), "approved": True, "archived": False, "instructions": "", "overleaf_project_id": None,
                     "created_at": now, "last_edited_at": now}
    else:
        entry["approved"] = True
        entry["archived"] = False
        entry["last_edited_at"] = _now_iso()
    save_registry(reg)
    return slug


def approve_bubble(slug: str, instructions: str = "") -> dict:
    reg = load_registry()
    entry = reg.get(slug)
    if entry is None:
        # bubble exists only via PDF tags — materialize it in the registry
        now = _now_iso()
        entry = {"name": slug_to_name(slug), "approved": True, "archived": False, "instructions": instructions, "overleaf_project_id": None,
                 "created_at": now, "last_edited_at": now}
    else:
        entry["approved"] = True
        entry["archived"] = False
        if instructions:
            entry["instructions"] = instructions
        entry["last_edited_at"] = _now_iso()
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
    reg[slug]["last_edited_at"] = _now_iso()
    save_registry(reg)
    return reg[slug]


def set_bubble_archived(slug: str, archived: bool) -> dict:
    """Reversibly hide a bubble without changing its reports, assets, or memberships."""
    reg = load_registry()
    if slug not in reg:
        raise KeyError(f"Bubble {slug!r} not found.")
    reg[slug]["archived"] = bool(archived)
    save_registry(reg)
    return reg[slug]


def delete_bubble(slug: str) -> None:
    """Remove a bubble from the registry, delete its reports, and strip its tag from all PDFs."""
    import shutil

    # Snapshot page locks before removing the registry entry. Removing the entry first prevents
    # new review mutations from validating; acquiring every existing page lock then drains any
    # mutation that had already started before the reports directory is removed.
    reg = load_registry()
    if not _CANONICAL_REVIEW_SLUG.fullmatch(str(slug or "")) or slug not in reg:
        return
    page_slugs = []
    if paths.bubble_manifest_path(slug).exists():
        page_slugs = sorted({str(item.get("page_slug") or "")
                             for item in manifest(slug).get("pages", [])
                             if _CANONICAL_REVIEW_SLUG.fullmatch(str(item.get("page_slug") or ""))})
    reg.pop(slug, None)
    save_registry(reg)

    with ExitStack() as locks:
        for page_slug in page_slugs:
            locks.enter_context(_page_lock(slug, page_slug))
        # Delete the reports directory (pages + assets) only after in-flight review writes drain.
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
            scores = dict(m.get("bubble_scores") or {})
            scores.pop(slug, None)
            m["bubble_scores"] = {s: scores[s] for s in m.get("idea_bubbles", []) if s in scores}
            assets.save_meta(m["pdf_id"], m)


def is_approved(slug: str) -> bool:
    return bool(load_registry().get(slug, {}).get("approved"))


def get_instructions(slug: str) -> str:
    return load_registry().get(slug, {}).get("instructions", "")


# --------------------------------------------------------------------------- #
# Derivation / listing
# --------------------------------------------------------------------------- #
def _pdf_slug_names(asset_metas: list[dict] | None = None) -> dict[str, str]:
    """Map slug -> a display name, derived from PDF tags.

    Callers that are already building a bubble view pass their one asset-metadata scan here.
    This avoids repeatedly parsing every ``meta.yaml`` for each bubble in the same response.
    """
    out: dict[str, str] = {}
    for m in asset_metas if asset_metas is not None else assets.list_assets():
        tags = m.get("tags", [])
        bubbles = m.get("idea_bubbles", [])
        for i, slug in enumerate(bubbles):
            if slug and slug not in out:
                out[slug] = tags[i] if i < len(tags) else slug
    return out


def slug_to_name(slug: str, *, reg: dict | None = None,
                 pdf_names: dict[str, str] | None = None) -> str:
    reg = load_registry() if reg is None else reg
    if slug in reg and reg[slug].get("name"):
        return reg[slug]["name"]
    return (pdf_names if pdf_names is not None else _pdf_slug_names()).get(slug, slug)


def tag_for_slug(slug: str, *, reg: dict | None = None,
                 pdf_names: dict[str, str] | None = None) -> str:
    """A display tag string guaranteed to slugify back to ``slug`` (the bubble's membership key).

    A bubble's display ``name`` is cosmetic and may be renamed freely, but membership is keyed by
    the immutable slug (``idea_bubbles`` = slugified tags). So when we tag a PDF into a bubble we
    must use a tag that *slugifies to the slug* — never the (possibly-renamed) display name, which
    would slugify to a different, phantom slug and split the bubble's papers. Prefer an existing
    member's tag (keeps a bubble's tag display consistent), then the registry name if it still maps
    to this slug, else a readable de-slugified form of the slug.
    """
    existing = (pdf_names if pdf_names is not None else _pdf_slug_names()).get(slug)
    if existing and assets.slug_of(existing) == slug:
        return existing
    name = ((load_registry() if reg is None else reg).get(slug) or {}).get("name", "")
    if name and assets.slug_of(name) == slug:
        return name
    readable = slug.replace("-", " ")
    return readable if assets.slug_of(readable) == slug else slug


def _with_bubble_score(meta: dict, slug: str) -> dict:
    out = dict(meta)
    scores = assets.bubble_scores(out)
    out["bubble_scores"] = scores
    out["bubble_score"] = scores.get(slug, 5)
    return out


def _paper_sort_key(meta: dict) -> tuple[int, str, str]:
    title = (meta.get("title") or meta.get("filename") or "").lower()
    return (-int(meta.get("bubble_score", 5)), title, meta.get("pdf_id", ""))


def pdfs_for_bubble(slug: str, *, asset_metas: list[dict] | None = None) -> list[dict]:
    pdfs = [_with_bubble_score(m, slug) for m in (asset_metas if asset_metas is not None else assets.list_assets())
            if slug in m.get("idea_bubbles", [])]
    pdfs.sort(key=_paper_sort_key)
    return pdfs


def add_pdf_to_bubble(slug: str, pdf_id: str) -> dict:
    """Make an existing PDF a member of this bubble by tagging it with the bubble's name.

    Membership is derived from a PDF's ``idea_bubbles`` (slugs of its tags), so we append a tag
    that slugifies back to this bubble's slug (``tag_for_slug``) and let ``assets.update_asset``
    re-sync the slug list. We deliberately do NOT use the display name: if the bubble was renamed,
    its name slugifies to a different slug and the PDF would join a phantom bubble. Idempotent: a
    no-op if the PDF is already a member.
    """
    meta = assets.load_meta(pdf_id)
    if slug in meta.get("idea_bubbles", []):
        scores = assets.bubble_scores(meta)
        if slug not in (meta.get("bubble_scores") or {}):
            meta["bubble_scores"] = scores
            assets.save_meta(pdf_id, meta)
        return _with_bubble_score(meta, slug)
    tags = list(meta.get("tags", []))
    tags.append(tag_for_slug(slug))
    updated = assets.update_asset(pdf_id, tags=tags)
    scores = assets.bubble_scores(updated)
    scores[slug] = 5
    updated["bubble_scores"] = scores
    assets.save_meta(pdf_id, updated)
    touch_bubble(slug)
    write_citation_file(slug)
    return _with_bubble_score(updated, slug)


def remove_pdf_from_bubble(slug: str, pdf_id: str) -> dict:
    """Drop this bubble's tag from a PDF so it leaves the bubble (stays in Assets / other
    bubbles). ``update_asset`` re-syncs ``idea_bubbles`` from the trimmed tag list. Idempotent."""
    meta = assets.load_meta(pdf_id)
    if slug not in meta.get("idea_bubbles", []):
        return meta
    tags = [t for t in meta.get("tags", []) if assets.slug_of(t) != slug]
    updated = assets.update_asset(pdf_id, tags=tags)
    scores = assets.bubble_scores(updated)
    scores.pop(slug, None)
    updated["bubble_scores"] = scores
    assets.save_meta(pdf_id, updated)
    touch_bubble(slug)
    write_citation_file(slug)
    return updated


def set_pdf_bubble_score(slug: str, pdf_id: str, score: int) -> dict:
    """Set a PDF's relevance score inside a bubble, preserving membership."""
    if score < 1 or score > 5:
        raise ValueError("Relevance score must be an integer from 1 to 5.")
    meta = assets.load_meta(pdf_id)
    if slug not in meta.get("idea_bubbles", []):
        raise KeyError(f"Asset {pdf_id!r} is not attached to bubble {slug!r}.")
    scores = assets.bubble_scores(meta)
    scores[slug] = int(score)
    meta["bubble_scores"] = scores
    assets.save_meta(pdf_id, meta)
    touch_bubble(slug)
    return _with_bubble_score(meta, slug)


def memberships_for_asset(pdf_id: str) -> list[dict]:
    """Return approved bubble memberships for one asset, including relevance."""
    meta = assets.load_meta(pdf_id)
    scores = assets.bubble_scores(meta)
    reg = load_registry()
    pdf_names = _pdf_slug_names()
    out = []
    for slug in meta.get("idea_bubbles", []) or []:
        out.append({
            "slug": slug,
            "name": slug_to_name(slug, reg=reg, pdf_names=pdf_names),
            "tag": tag_for_slug(slug, reg=reg, pdf_names=pdf_names),
            "score": scores.get(slug, 5),
            "approved": bool(reg.get(slug, {}).get("approved")),
        })
    out.sort(key=lambda x: (-int(x.get("score", 5)), x.get("name", "").lower(), x.get("slug", "")))
    return out


def all_bubbles() -> list[dict]:
    """Union of registry bubbles and PDF-derived bubbles."""
    reg = load_registry()
    asset_metas = assets.list_assets()
    pdf_names = _pdf_slug_names(asset_metas)
    counts: dict[str, int] = {}
    for m in asset_metas:
        for slug in m.get("idea_bubbles", []):
            counts[slug] = counts.get(slug, 0) + 1

    slugs = set(reg) | set(pdf_names)
    out = []
    for slug in sorted(slugs):
        entry = reg.get(slug, {})
        name = entry.get("name") or pdf_names.get(slug, slug)
        last_edited_at = entry.get("last_edited_at") or entry.get("created_at") or ""
        out.append({
            "slug": slug,
            "name": name,
            "tag": tag_for_slug(slug, reg=reg, pdf_names=pdf_names),
            "approved": bool(entry.get("approved", False)),
            "archived": bool(entry.get("archived", False)),
            "in_registry": slug in reg,
            "pdf_count": counts.get(slug, 0),
            "page_count": _page_count(slug),
            "instructions": entry.get("instructions", ""),
            **overleaf_urls(entry.get("overleaf_project_id")),
            "last_edited_at": last_edited_at,
        })
    out.sort(key=lambda b: b.get("last_edited_at") or "", reverse=True)
    return out


def bubble_detail(slug: str) -> dict:
    reg = load_registry()
    entry = reg.get(slug, {})
    asset_metas = assets.list_assets()
    pdf_names = _pdf_slug_names(asset_metas)
    pdfs = pdfs_for_bubble(slug, asset_metas=asset_metas)
    approved = bool(entry.get("approved", False))
    pages, home, content = [], "", ""
    if approved:
        ensure_pages(slug)
        pages = list_pages(slug)
        home = manifest(slug).get("home", pages[0]["page_slug"] if pages else "")
        content = get_page(slug, home) if home else ""
    return {
        "slug": slug,
        "name": entry.get("name") or slug_to_name(slug, reg=reg, pdf_names=pdf_names),
        "tag": tag_for_slug(slug, reg=reg, pdf_names=pdf_names),
        "approved": approved,
        "archived": bool(entry.get("archived", False)),
        "in_registry": slug in reg,
        "instructions": entry.get("instructions", ""),
        "last_edited_at": entry.get("last_edited_at") or entry.get("created_at") or "",
        "pdf_count": len(pdfs),
        "page_count": _page_count(slug),
        "assets": pdfs,
        "images": list_bubble_images(slug),
        "pages": pages,
        "home": home,
        "page": home,
        "content": content,
        "share_active": bool(entry.get("share_active", False)),
        "share_token": entry.get("share_token", ""),
        **overleaf_urls(entry.get("overleaf_project_id")),
    }


# --------------------------------------------------------------------------- #
# Public sharing — an unlisted, read-only link per bubble
# --------------------------------------------------------------------------- #
def set_share_active(slug: str, active: bool) -> dict:
    """Toggle a bubble's public share. Mints a permanent token on first activation.

    Returns ``{"share_active", "share_token"}``. The token is stable: turning sharing off then
    back on restores the same link. Auto-suggested bubbles are materialized into the registry so
    the share state has somewhere to live.
    """
    reg = load_registry()
    entry = reg.get(slug)
    if entry is None:
        entry = {"name": slug_to_name(slug), "approved": True, "archived": False, "instructions": "",
                 "created_at": _now_iso(), "overleaf_project_id": None}
    token = entry.get("share_token") or secrets.token_urlsafe(16)
    entry["share_token"] = token
    entry["share_active"] = bool(active)
    entry["last_edited_at"] = entry.get("last_edited_at") or entry.get("created_at") or _now_iso()
    reg[slug] = entry
    save_registry(reg)
    return {"share_active": bool(active), "share_token": token}


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


# --------------------------------------------------------------------------- #
# Private review comments — deliberately separate from Markdown/source previews
# --------------------------------------------------------------------------- #
class ReviewSidecarError(ValueError):
    """A review sidecar exists but cannot safely be read as review state."""

    def __init__(self, path: Path, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"Review sidecar {path} is unreadable: {reason}")

    def as_detail(self) -> dict:
        return {"code": "invalid_review_sidecar", "message": str(self),
                "path": str(self.path)}


class ReviewTargetError(ValueError):
    """A review request did not name a real approved manifest page."""


def validate_review_target(slug: str, page_slug: str) -> None:
    """Reject path-like, unapproved, and orphan review targets before path construction."""
    if (not isinstance(slug, str) or not _CANONICAL_REVIEW_SLUG.fullmatch(slug)
            or not isinstance(page_slug, str) or not _CANONICAL_REVIEW_SLUG.fullmatch(page_slug)):
        raise ReviewTargetError("Review target must use canonical bubble and page slugs.")
    entry = load_registry().get(slug)
    if not entry or not entry.get("approved") or not paths.bubble_dir(slug).is_dir():
        raise ReviewTargetError("Review target bubble does not exist or is not approved.")
    pages = manifest(slug).get("pages", [])
    if not any(item.get("page_slug") == page_slug for item in pages):
        raise ReviewTargetError("Review target page is not in the bubble manifest.")
    if not paths.bubble_page_path(slug, page_slug).is_file():
        raise ReviewTargetError("Review target page does not exist.")


def _comments(slug: str, page_slug: str) -> dict:
    path = paths.bubble_page_comments_path(slug, page_slug)
    if not path.exists():
        return {"version": 2, "threads": []}
    try:
        data = _json.loads(path.read_text())
    except (OSError, UnicodeError, _json.JSONDecodeError) as exc:
        raise ReviewSidecarError(path, str(exc)) from exc
    if not isinstance(data, dict) or not isinstance(data.get("threads"), list):
        raise ReviewSidecarError(path, "expected a JSON object with a threads list")
    data.setdefault("version", 1)
    return data


def _save_comments(slug: str, page_slug: str, data: dict) -> float:
    path = paths.bubble_page_comments_path(slug, page_slug)
    _atomic_write(path, _json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    touch_bubble(slug)
    return path.stat().st_mtime


_COMMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_COMMENT_COMMAND = "\\comment"
_TEXTCOLOR_COMMAND = "\\textcolor"
_TEXTCOLOR_VALUE_RE = re.compile(r"^(?:#[0-9A-Fa-f]{3}(?:[0-9A-Fa-f]{3})?|[A-Za-z][A-Za-z0-9-]{0,63})$")


@dataclass(frozen=True)
class CommentSpan:
    """Exact source offsets for one ``\\comment{id}{body}`` wrapper."""

    thread_id: str
    open_start: int
    body_start: int
    body_end: int
    close_end: int


class ReviewMarkupError(ValueError):
    """A deterministic, source-addressable review-wrapper validation failure."""

    def __init__(self, code: str, message: str, content: str, offset: int,
                 *, thread_id: str = ""):
        self.code = code
        self.offset = max(0, min(int(offset), len(content)))
        self.line = content.count("\n", 0, self.offset) + 1
        line_start = content.rfind("\n", 0, self.offset) + 1
        self.column = self.offset - line_start + 1
        self.thread_id = thread_id
        self.message = message
        super().__init__(f"{message} (line {self.line}, column {self.column})")

    def as_detail(self) -> dict:
        detail = {"code": self.code, "message": self.message, "offset": self.offset,
                  "line": self.line, "column": self.column}
        if self.thread_id:
            detail["comment_id"] = self.thread_id
        return detail


class TextColorMarkupError(ValueError):
    """A deterministic source error for ``\\textcolor{color}{body}`` markup."""

    def __init__(self, code: str, message: str, content: str, offset: int,
                 *, color: str = ""):
        self.code = code
        self.offset = max(0, min(int(offset), len(content)))
        self.line = content.count("\n", 0, self.offset) + 1
        line_start = content.rfind("\n", 0, self.offset) + 1
        self.column = self.offset - line_start + 1
        self.color = color
        self.message = message
        super().__init__(f"{message} (line {self.line}, column {self.column})")

    def as_detail(self) -> dict:
        detail = {"code": self.code, "message": self.message, "offset": self.offset,
                  "line": self.line, "column": self.column}
        if self.color:
            detail["color"] = self.color
        return detail


@dataclass(frozen=True)
class TextColorSpan:
    """Exact source offsets for one ``\\textcolor{color}{body}`` wrapper."""

    color: str
    open_start: int
    body_start: int
    body_end: int
    close_end: int


def _is_escaped(content: str, offset: int) -> bool:
    backslashes = 0
    i = offset - 1
    while i >= 0 and content[i] == "\\":
        backslashes += 1
        i -= 1
    return bool(backslashes % 2)


def parse_comment_wrappers(content: str) -> list[CommentSpan]:
    """Parse review wrappers in one linear pass.

    Ordinary balanced braces inside a body (including LaTeX arguments) are supported. A
    ``\\comment`` inside another comment is rejected because comment ranges may never overlap
    or contain one another. Adjacent wrappers remain valid.
    """
    spans: list[CommentSpan] = []
    seen: set[str] = set()
    pos = 0
    size = len(content)
    while pos < size:
        start = content.find(_COMMENT_COMMAND, pos)
        if start < 0:
            break
        if _is_escaped(content, start):
            pos = start + len(_COMMENT_COMMAND)
            continue
        arg = start + len(_COMMENT_COMMAND)
        # Do not treat commands such as \commentary as review markup.
        if arg >= size or content[arg] != "{":
            pos = arg
            continue
        id_start = arg + 1
        i = id_start
        while i < size and content[i] != "}":
            if content[i] == "{" and not _is_escaped(content, i):
                raise ReviewMarkupError("invalid_comment_id", "Comment IDs cannot contain braces.",
                                        content, i)
            i += 1
        if i >= size:
            raise ReviewMarkupError("unclosed_comment_id", "The comment ID is missing its closing brace.",
                                    content, start)
        thread_id = content[id_start:i]
        if not _COMMENT_ID_RE.fullmatch(thread_id):
            raise ReviewMarkupError(
                "invalid_comment_id",
                "Comment IDs may contain only letters, numbers, hyphens, and underscores.",
                content, id_start, thread_id=thread_id)
        if thread_id in seen:
            raise ReviewMarkupError("duplicate_comment", "A comment ID may appear only once on a page.",
                                    content, start, thread_id=thread_id)
        body_open = i + 1
        if body_open >= size or content[body_open] != "{":
            raise ReviewMarkupError("missing_comment_body", "The comment wrapper is missing its body.",
                                    content, body_open, thread_id=thread_id)
        body_start = body_open + 1
        depth = 1
        i = body_start
        while i < size:
            if (content.startswith(_COMMENT_COMMAND + "{", i)
                    and not _is_escaped(content, i)):
                raise ReviewMarkupError(
                    "intersecting_comments",
                    "Comments cannot overlap, contain, or intersect another comment.",
                    content, i, thread_id=thread_id)
            char = content[i]
            if char == "{" and not _is_escaped(content, i):
                depth += 1
            elif char == "}" and not _is_escaped(content, i):
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth:
            raise ReviewMarkupError("unclosed_comment", "The comment wrapper is missing its closing brace.",
                                    content, start, thread_id=thread_id)
        seen.add(thread_id)
        spans.append(CommentSpan(thread_id=thread_id, open_start=start, body_start=body_start,
                                 body_end=i, close_end=i + 1))
        pos = i + 1
    return spans


def parse_textcolor_wrappers(content: str) -> list[TextColorSpan]:
    """Parse text-color wrappers in one linear pass.

    A color wrapper can contain multiline Markdown and ordinary balanced LaTeX braces. Color
    wrappers themselves may not overlap or nest: a second ``\\textcolor`` inside a wrapper is
    rejected, while adjacent wrappers are valid.
    """
    spans: list[TextColorSpan] = []
    pos = 0
    size = len(content)
    while pos < size:
        start = content.find(_TEXTCOLOR_COMMAND, pos)
        if start < 0:
            break
        if _is_escaped(content, start):
            pos = start + len(_TEXTCOLOR_COMMAND)
            continue
        arg = start + len(_TEXTCOLOR_COMMAND)
        # Do not treat commands such as \textcolorful as color markup.
        if arg >= size or content[arg] != "{":
            pos = arg
            continue
        color_start = arg + 1
        i = color_start
        while i < size and content[i] != "}":
            if content[i] == "{" and not _is_escaped(content, i):
                raise TextColorMarkupError("invalid_textcolor", "Text colors cannot contain braces.",
                                           content, i)
            i += 1
        if i >= size:
            raise TextColorMarkupError("unclosed_textcolor", "The text color is missing its closing brace.",
                                       content, start)
        color = content[color_start:i]
        if not _TEXTCOLOR_VALUE_RE.fullmatch(color):
            raise TextColorMarkupError(
                "invalid_textcolor", "Use a hex value or a CSS color name in \\textcolor.",
                content, color_start, color=color)
        body_open = i + 1
        if body_open >= size or content[body_open] != "{":
            raise TextColorMarkupError("missing_textcolor_body", "The text-color wrapper is missing its body.",
                                       content, body_open, color=color)
        body_start = body_open + 1
        depth = 1
        i = body_start
        while i < size:
            if (content.startswith(_TEXTCOLOR_COMMAND + "{", i)
                    and not _is_escaped(content, i)):
                raise TextColorMarkupError(
                    "intersecting_textcolors",
                    "Text colors cannot overlap, contain, or intersect another text color.",
                    content, i, color=color)
            char = content[i]
            if char == "{" and not _is_escaped(content, i):
                depth += 1
            elif char == "}" and not _is_escaped(content, i):
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth:
            raise TextColorMarkupError("unclosed_textcolor", "The text-color wrapper is missing its closing brace.",
                                       content, start, color=color)
        spans.append(TextColorSpan(color=color, open_start=start, body_start=body_start,
                                   body_end=i, close_end=i + 1))
        pos = i + 1
    return spans


def comment_marker(thread_id: str) -> str:
    return f"\\comment{{{thread_id}}}{{"


def strip_comment_markers(content: str) -> str:
    """Remove validated review wrappers while preserving their Markdown bodies."""
    spans = parse_comment_wrappers(content)
    for span in reversed(spans):
        content = (content[:span.open_start] + content[span.body_start:span.body_end]
                   + content[span.close_end:])
    return content


def remove_comment_marker(content: str, thread_id: str) -> str:
    """Remove one validated wrapper while preserving its body."""
    for span in parse_comment_wrappers(content):
        if span.thread_id == thread_id:
            return (content[:span.open_start] + content[span.body_start:span.body_end]
                    + content[span.close_end:])
    return content


_PAGE_LOCKS: dict[str, threading.RLock] = {}
_PAGE_LOCKS_GUARD = threading.Lock()


@contextmanager
def _page_lock(slug: str, page_slug: str):
    """Serialize one page across threads and, where available, server processes.

    The lock file lives in the workspace config area, keyed by the page's absolute path, so
    deleting a report cannot let a waiting process recreate its directory merely to acquire a
    lock. Independent pages never block one another. ``flock`` is advisory and unavailable on
    some platforms; the per-process re-entrant lock remains the portable fallback.
    """
    key = str(paths.bubble_page_path(slug, page_slug).resolve())
    with _PAGE_LOCKS_GUARD:
        thread_lock = _PAGE_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        fd = None
        try:
            if fcntl is not None:
                lock_name = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".lock"
                lock_path = paths.CONFIG_DIR / ".review-locks" / lock_name
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except OSError as exc:  # e.g. a filesystem without advisory-lock support
                    logger.warning("Review interprocess lock unavailable for %s/%s: %s",
                                   slug, page_slug, exc)
                    os.close(fd)
                    fd = None
            yield
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


@contextmanager
def _review_page_lock(slug: str, page_slug: str):
    """Validate a review target before any lock path can be materialized."""
    validate_review_target(slug, page_slug)
    with _page_lock(slug, page_slug):
        # The page or bubble may have been deleted while this caller waited for the lock.
        validate_review_target(slug, page_slug)
        yield


def _copy_comments(data: dict) -> dict:
    return _json.loads(_json.dumps(data))


def _validate_known_wrappers(content: str, data: dict, *,
                             allow_unanchored: bool = False) -> list[CommentSpan]:
    spans = parse_comment_wrappers(content)
    known = {str(t.get("id") or ""): t for t in data.get("threads", [])}
    for span in spans:
        thread = known.get(span.thread_id)
        if thread is None:
            raise ReviewMarkupError("unknown_comment", "This comment ID does not exist.",
                                    content, span.open_start, thread_id=span.thread_id)
        if thread.get("status", "open") != "open":
            raise ReviewMarkupError("resolved_comment", "Resolved comments cannot remain in Markdown.",
                                    content, span.open_start, thread_id=span.thread_id)
        if not allow_unanchored and thread.get("anchor_state") == "unanchored":
            raise ReviewMarkupError(
                "reattached_comment",
                "Unanchored comments cannot be reattached by writing a comment wrapper.",
                content, span.open_start, thread_id=span.thread_id)
    return spans


def _reconcile_comments(content: str, data: dict, *,
                        allow_reattach: bool = False) -> tuple[str, dict, bool]:
    """Validate wrappers and derive every thread's anchor state from this exact source."""
    # Text-color wrappers are ordinary report source, but they share the same fast, strict
    # source-validation contract as review wrappers. Keeping this here makes every page-writing
    # path (browser saves, Scientist writes, and authoritative comment transitions) agree.
    parse_textcolor_wrappers(content)
    before = _json.dumps(data, ensure_ascii=False, sort_keys=True)
    spans = _validate_known_wrappers(content, data, allow_unanchored=allow_reattach)
    empty = [span for span in spans if span.body_start == span.body_end]
    if empty:
        for span in reversed(empty):
            content = content[:span.open_start] + content[span.close_end:]
        spans = _validate_known_wrappers(content, data, allow_unanchored=allow_reattach)
    by_id = {span.thread_id: span for span in spans}
    data["version"] = 2
    for thread in data.get("threads", []):
        tid = str(thread.get("id") or "")
        anchor = thread.setdefault("anchor", {})
        span = by_id.get(tid) if thread.get("status", "open") == "open" else None
        if span is None:
            thread["anchor_state"] = "unanchored"
            continue
        body = content[span.body_start:span.body_end]
        thread["anchor_state"] = "attached"
        anchor["quote"] = body
        anchor["start"] = span.body_start
        anchor["end"] = span.body_end
        anchor["prefix"] = content[max(0, span.open_start - 96):span.open_start]
        anchor["suffix"] = content[span.close_end:span.close_end + 96]
    changed = before != _json.dumps(data, ensure_ascii=False, sort_keys=True)
    return content, data, changed


def _project_comments(slug: str, page_slug: str, data: dict) -> dict:
    """Return current anchor states without changing either page or sidecar."""
    projected = _copy_comments(data)
    path = paths.bubble_page_path(slug, page_slug)
    if not path.exists():
        for thread in projected.get("threads", []):
            thread["anchor_state"] = "unanchored"
        return projected
    try:
        _, projected, _ = _reconcile_comments(path.read_text(), projected)
    except ReviewMarkupError as exc:
        projected["markup_error"] = exc.as_detail()
    return projected


def list_comments(slug: str, page_slug: str) -> dict:
    """Return private threads for one page; callers must already enforce membership."""
    with _review_page_lock(slug, page_slug):
        return _project_comments(slug, page_slug, _comments(slug, page_slug))


def comments_mtime(slug: str, page_slug: str) -> float:
    validate_review_target(slug, page_slug)
    path = paths.bubble_page_comments_path(slug, page_slug)
    return path.stat().st_mtime if path.exists() else 0


def _check_page_conflict(path: Path, base_mtime: "float | None") -> None:
    if base_mtime is not None:
        if not path.exists():
            raise PageConflict(0.0)
        disk_mtime = path.stat().st_mtime
        if abs(disk_mtime - base_mtime) > 1e-6:
            raise PageConflict(disk_mtime)


def _write_page_and_comments(slug: str, page_slug: str, content: str, data: dict) -> None:
    """Replace page and sidecar together, rolling the page back if the second replace fails."""
    page_path = paths.bubble_page_path(slug, page_slug)
    comments_path = paths.bubble_page_comments_path(slug, page_slug)
    page_path.parent.mkdir(parents=True, exist_ok=True)
    comments_path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(6)
    page_tmp = page_path.with_name(page_path.name + f".txn-{token}")
    comments_tmp = comments_path.with_name(comments_path.name + f".txn-{token}")
    old_page = page_path.read_bytes() if page_path.exists() else None
    old_comments = comments_path.read_bytes() if comments_path.exists() else None
    page_tmp.write_text(content)
    comments_tmp.write_text(_json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    page_replaced = comments_replaced = False
    try:
        os.replace(page_tmp, page_path); page_replaced = True
        os.replace(comments_tmp, comments_path); comments_replaced = True
    except Exception:
        try:
            if page_replaced:
                if old_page is None:
                    page_path.unlink(missing_ok=True)
                else:
                    rollback = page_path.with_name(page_path.name + f".rollback-{token}")
                    rollback.write_bytes(old_page); os.replace(rollback, page_path)
            if comments_replaced:
                if old_comments is None:
                    comments_path.unlink(missing_ok=True)
                else:
                    rollback = comments_path.with_name(comments_path.name + f".rollback-{token}")
                    rollback.write_bytes(old_comments); os.replace(rollback, comments_path)
        except Exception:  # noqa: BLE001 - preserve the original transaction error
            logger.exception("Could not roll back review transaction for %s/%s", slug, page_slug)
        raise
    finally:
        page_tmp.unlink(missing_ok=True)
        comments_tmp.unlink(missing_ok=True)
    touch_bubble(slug)


def _review_result(slug: str, page_slug: str, content: str, data: dict,
                   *, thread: "dict | None" = None) -> dict:
    page_path = paths.bubble_page_path(slug, page_slug)
    comments_path = paths.bubble_page_comments_path(slug, page_slug)
    result = {"content": content,
              "page_mtime": page_path.stat().st_mtime if page_path.exists() else 0,
              "comments_mtime": comments_path.stat().st_mtime if comments_path.exists() else 0,
              "threads": _copy_comments(data).get("threads", [])}
    if thread is not None:
        result["thread"] = _copy_comments(thread)
    return result


def create_comment_state(slug: str, page_slug: str, author: str, body: str, *,
                         content: str, base_mtime: "float | None",
                         selection_start: int, selection_end: int) -> dict:
    """Atomically create a thread and wrap the exact selected source range."""
    body = str(body or "").strip()
    if not body:
        raise ValueError("Comment text required.")
    with _review_page_lock(slug, page_slug):
        path = paths.bubble_page_path(slug, page_slug)
        _check_page_conflict(path, base_mtime)
        data = _copy_comments(_comments(slug, page_slug))
        spans = _validate_known_wrappers(content, data)
        start, end = int(selection_start), int(selection_end)
        if start < 0 or end > len(content) or start >= end:
            raise ReviewMarkupError("invalid_selection", "Select non-empty report text to comment on.",
                                    content, max(0, start))
        for span in spans:
            if start < span.close_end and end > span.open_start:
                raise ReviewMarkupError(
                    "intersecting_comments",
                    "Comments cannot overlap, contain, or intersect another comment.",
                    content, start, thread_id=span.thread_id)
        quote = content[start:end]
        known_ids = {str(t.get("id") or "") for t in data.get("threads", [])}
        while True:
            thread_id = secrets.token_urlsafe(6)
            if thread_id not in known_ids:
                break
        now = _now_iso()
        item = {"id": thread_id, "page_slug": page_slug, "status": "open",
                "anchor_state": "attached", "created_at": now, "updated_at": now,
                "resolved_at": "", "resolved_by": "",
                "anchor": {"quote": quote, "start": start,
                           "prefix": content[max(0, start - 96):start],
                           "suffix": content[end:end + 96]},
                "messages": [{"id": secrets.token_urlsafe(12), "author": author, "body": body,
                              "created_at": now, "edited_at": ""}]}
        marked = content[:start] + comment_marker(thread_id) + quote + "}" + content[end:]
        data.setdefault("threads", []).append(item)
        normalized = normalize_display_math(normalize_wikilinks(slug, marked))
        normalized, data, _ = _reconcile_comments(normalized, data)
        _write_page_and_comments(slug, page_slug, normalized, data)
        item = _thread(data, thread_id)
        return _review_result(slug, page_slug, normalized, data, thread=item)


def _thread(data: dict, thread_id: str) -> dict:
    for item in data.get("threads", []):
        if item.get("id") == thread_id:
            return item
    raise KeyError(thread_id)


def reply_comment_state(slug: str, page_slug: str, thread_id: str, author: str, body: str) -> dict:
    body = str(body or "").strip()
    if not body: raise ValueError("Reply text required.")
    with _review_page_lock(slug, page_slug):
        data = _comments(slug, page_slug); item = _thread(data, thread_id); now = _now_iso()
        msg = {"id": secrets.token_urlsafe(12), "author": author, "body": body,
               "created_at": now, "edited_at": ""}
        item.setdefault("messages", []).append(msg); item["updated_at"] = now
        _save_comments(slug, page_slug, data)
        page = paths.bubble_page_path(slug, page_slug)
        result = _review_result(slug, page_slug, page.read_text() if page.exists() else "", data,
                                thread=item)
        result["message"] = _copy_comments(msg)
        return result


def edit_comment_message_state(slug: str, page_slug: str, thread_id: str, message_id: str,
                               author: str, body: str) -> dict:
    body = str(body or "").strip()
    if not body: raise ValueError("Comment text required.")
    with _review_page_lock(slug, page_slug):
        data = _comments(slug, page_slug); item = _thread(data, thread_id)
        for msg in item.get("messages", []):
            if msg.get("id") == message_id:
                if msg.get("author") != author: raise PermissionError("You can only edit your own comments.")
                now = _now_iso(); msg["body"] = body; msg["edited_at"] = now; item["updated_at"] = now
                _save_comments(slug, page_slug, data)
                page = paths.bubble_page_path(slug, page_slug)
                result = _review_result(
                    slug, page_slug, page.read_text() if page.exists() else "", data, thread=item)
                result["message"] = _copy_comments(msg)
                return result
        raise KeyError(message_id)


def set_comment_status_state(slug: str, page_slug: str, thread_id: str, status: str, actor: str,
                             *, content: "str | None" = None,
                             base_mtime: "float | None" = None) -> dict:
    if status not in {"open", "resolved"}: raise ValueError("Invalid comment status.")
    with _review_page_lock(slug, page_slug):
        path = paths.bubble_page_path(slug, page_slug)
        _check_page_conflict(path, base_mtime)
        source = content if content is not None else (path.read_text() if path.exists() else "")
        data = _copy_comments(_comments(slug, page_slug)); item = _thread(data, thread_id)
        _validate_known_wrappers(source, data)
        now = _now_iso()
        if status == "resolved":
            source = remove_comment_marker(source, thread_id)
        item["status"] = status; item["updated_at"] = now
        item["resolved_at"] = now if status == "resolved" else ""
        item["resolved_by"] = actor if status == "resolved" else ""
        # Reopening does not guess or recreate a source anchor.
        item["anchor_state"] = "unanchored"
        normalized = normalize_display_math(normalize_wikilinks(slug, source))
        normalized, data, _ = _reconcile_comments(normalized, data)
        _write_page_and_comments(slug, page_slug, normalized, data)
        item = _thread(data, thread_id)
        return _review_result(slug, page_slug, normalized, data, thread=item)


def delete_comment_state(slug: str, page_slug: str, thread_id: str, *,
                         content: "str | None" = None,
                         base_mtime: "float | None" = None) -> "dict | None":
    with _review_page_lock(slug, page_slug):
        path = paths.bubble_page_path(slug, page_slug)
        _check_page_conflict(path, base_mtime)
        source = content if content is not None else (path.read_text() if path.exists() else "")
        data = _copy_comments(_comments(slug, page_slug))
        before = len(data.get("threads", []))
        if not any(t.get("id") == thread_id for t in data.get("threads", [])):
            return None
        _validate_known_wrappers(source, data)
        source = remove_comment_marker(source, thread_id)
        data["threads"] = [t for t in data["threads"] if t.get("id") != thread_id]
        assert len(data["threads"]) < before
        normalized = normalize_display_math(normalize_wikilinks(slug, source))
        normalized, data, _ = _reconcile_comments(normalized, data)
        _write_page_and_comments(slug, page_slug, normalized, data)
        return _review_result(slug, page_slug, normalized, data)


def migrate_review_comments() -> dict:
    """Migrate every page in the active root to explicit, non-recreated anchor states.

    Unknown and resolved wrappers are unwrapped while preserving their bodies. Missing wrappers
    become unanchored. Malformed pages are left byte-for-byte intact and reported through logs so
    an owner can repair the source deliberately.
    """
    stats = {"pages": 0, "sidecars": 0, "errors": 0, "unknown_wrappers": 0}
    reports_dir = paths.REPORTS_DIR
    if not reports_dir.exists():
        return stats
    for bubble_dir in reports_dir.iterdir():
        if not bubble_dir.is_dir():
            continue
        slug = bubble_dir.name
        pages_dir = bubble_dir / "pages"
        comments_dir = bubble_dir / "comments"
        page_slugs = set()
        if pages_dir.exists():
            page_slugs.update(path.stem for path in pages_dir.glob("*.md"))
        if comments_dir.exists():
            page_slugs.update(path.stem for path in comments_dir.glob("*.json"))
        for page_slug in sorted(page_slugs):
            page_path = paths.bubble_page_path(slug, page_slug)
            comments_path = paths.bubble_page_comments_path(slug, page_slug)
            if not page_path.exists():
                continue
            with _page_lock(slug, page_slug):
                try:
                    source = page_path.read_text()
                    data = _copy_comments(_comments(slug, page_slug))
                    before_source = source
                    before_data = _json.dumps(data, ensure_ascii=False, sort_keys=True)
                    spans = parse_comment_wrappers(source)
                    legacy_sidecar = data.get("version") != 2
                    known = {str(t.get("id") or ""): t for t in data.get("threads", [])}
                    removable = []
                    for span in spans:
                        thread = known.get(span.thread_id)
                        if thread is None:
                            stats["unknown_wrappers"] += 1
                            removable.append(span)
                            logger.warning("Removing unknown review wrapper %s from %s/%s",
                                           span.thread_id, slug, page_slug)
                        elif thread.get("status", "open") != "open":
                            removable.append(span)
                        elif not legacy_sidecar and thread.get("anchor_state") == "unanchored":
                            # Version-2 unanchored state is deliberate. A raw out-of-band wrapper
                            # must not turn startup into an implicit reattachment mechanism.
                            removable.append(span)
                    for span in reversed(removable):
                        source = (source[:span.open_start] + source[span.body_start:span.body_end]
                                  + source[span.close_end:])
                    source, data, _ = _reconcile_comments(
                        source, data, allow_reattach=legacy_sidecar)
                except (OSError, UnicodeError, ReviewMarkupError, ReviewSidecarError) as exc:
                    stats["errors"] += 1
                    logger.error("Review migration skipped %s/%s; preserving page and sidecar bytes: %s",
                                 slug, page_slug, exc)
                    continue
                page_changed = source != before_source
                data_changed = before_data != _json.dumps(data, ensure_ascii=False, sort_keys=True)
                if page_changed and (comments_path.exists() or data.get("threads")):
                    _write_page_and_comments(slug, page_slug, source, data)
                else:
                    if page_changed:
                        _atomic_write(page_path, source); touch_bubble(slug)
                    if data_changed:
                        _save_comments(slug, page_slug, data)
                stats["pages"] += int(page_changed)
                stats["sidecars"] += int(data_changed)
    return stats


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
        raw = m.group(1).strip()
        if "|" in raw:
            target, label = raw.split("|", 1)
            target = target.strip()
            label = label.strip()
        else:
            target = raw
            label = None
        if "/" in target:                      # drop invented "Section/Title" prefixes
            target = target.split("/")[-1].strip()
        if target in slugs:
            normalized = target
        else:
            hit = by_title.get(target.lower())
            normalized = hit if hit else target
        return f"[[{normalized}|{label}]]" if label is not None else f"[[{normalized}]]"

    return _WIKILINK_RE.sub(repl, content)


_FENCED_RE = re.compile(r"(^[ \t]*(?:```|~~~).*?(?:^[ \t]*(?:```|~~~)[ \t]*$|\Z))", re.S | re.M)
_DISPLAY_MATH_BLOCK_RE = re.compile(r"\$\$(?!\$)(.+?)\$\$", re.S)


def normalize_display_math(content: str) -> str:
    """Move a multi-line display-math opener onto its own line.

    Toast UI's markdown parser claims ``$$`` for its own custom-block widgets, which has nothing
    to do with math. A line matching ``$$`` followed by a letter *opens* a widget block, and only a
    line that is exactly ``$$`` closes it. So ``$$K_\\theta = ...`` spread over several lines opens
    a block whose closing ``,$$`` (at the end of the last line, not alone) never matches — and the
    editor paints every remaining line of the page with the custom-block background.

    A bare ``$$`` opener matches nothing, so moving the math down one line keeps the editor's
    highlighting intact. Only blocks that actually trip the parser are touched: single-line math is
    already safe (the widget rule ignores a ``$$…$$`` pair closed on its own line), and so is math
    whose first character is not a letter, such as ``$$\\operatorname{Cay}(K)``. KaTeX ignores the
    extra newline, so nothing about the rendered output changes. Idempotent.
    """

    def fix(text: str) -> str:
        def repl(m: "re.Match") -> str:
            inner = m.group(1)
            if "\n" not in inner or inner.startswith("\n"):
                return m.group(0)          # single-line, or already normalized
            if not re.match(r"[ \t]*[a-zA-Z]", inner):
                return m.group(0)          # never opens a Toast UI widget block
            return "$$\n" + inner + "$$"

        return _DISPLAY_MATH_BLOCK_RE.sub(repl, text)

    # Fenced code shows math source verbatim; rewriting inside it would corrupt an example.
    return "".join(part if i % 2 else fix(part)
                   for i, part in enumerate(_FENCED_RE.split(content)))


class PageConflict(Exception):
    """Page changed on disk since the editor loaded it.

    Raised by :func:`save_page` when an optimistic ``base_mtime`` no longer matches the
    file's current mtime — i.e. something edited the page out-of-band (e.g. a dev-mode
    direct file edit) since the content the caller is about to write was loaded. The
    caller (the autosave path) surfaces this as an "unsynced" conflict the user resolves
    by hand, rather than silently clobbering the external edit.
    """

    def __init__(self, disk_mtime: float):
        super().__init__("page changed on disk")
        self.disk_mtime = disk_mtime


def save_page(slug: str, page_slug: str, content: str,
              base_mtime: "float | None" = None) -> float:
    """Write a page atomically (after wikilink + display-math normalization); return its new mtime.

    If ``base_mtime`` is given, it's an optimistic-concurrency guard: when the file's
    current mtime differs (an external edit happened since it was loaded), raise
    :class:`PageConflict` instead of overwriting. ``None`` (the default) skips the check
    and always writes — preserving every existing caller.
    """
    return save_page_state(slug, page_slug, content, base_mtime)["page_mtime"]


def save_page_state(slug: str, page_slug: str, content: str,
                    base_mtime: "float | None" = None) -> dict:
    """Validate/reconcile review wrappers and return the authoritative page state."""
    with _review_page_lock(slug, page_slug):
        path = paths.bubble_page_path(slug, page_slug)
        _check_page_conflict(path, base_mtime)
        data = _copy_comments(_comments(slug, page_slug))
        normalized = normalize_display_math(normalize_wikilinks(slug, content))
        normalized, data, comments_changed = _reconcile_comments(normalized, data)
        if comments_changed:
            _write_page_and_comments(slug, page_slug, normalized, data)
        else:
            _atomic_write(path, normalized)
            touch_bubble(slug)
        return _review_result(slug, page_slug, normalized, data)


def create_page(slug: str, title: str) -> str:
    ensure_pages(slug)
    page_slug = _unique_page_slug(slug, title)
    _atomic_write(paths.bubble_page_path(slug, page_slug), f"# {title.strip()}\n\n")
    data = manifest(slug)
    data["pages"].append({"page_slug": page_slug, "title": title.strip() or page_slug})
    _save_manifest(slug, data)
    touch_bubble(slug)
    return page_slug


def register_page(slug: str, page_slug: str, content: str) -> None:
    """Register a Scientist-created page whose filename is already its stable slug.

    Browser-created pages choose their slug from a human title.  Scientist instead starts with
    ``pages/<page_slug>.md``, so this keeps that filename stable while using the same manifest
    and link-normalization invariants as every other page creation path.
    """
    if not page_slug or slugify(page_slug) != page_slug:
        raise ValueError("Invalid page slug.")
    # A newly registered page has no review sidecar, so every wrapper would necessarily be
    # fabricated. Scientist must never introduce comment markup itself.
    _validate_known_wrappers(content, {"version": 2, "threads": []})
    parse_textcolor_wrappers(content)
    ensure_pages(slug)
    data = manifest(slug)
    if any(p["page_slug"] == page_slug for p in data.get("pages", [])):
        raise ValueError("Page already exists.")
    _atomic_write(paths.bubble_page_path(slug, page_slug),
                  normalize_display_math(normalize_wikilinks(slug, content)))
    data.setdefault("pages", []).append({"page_slug": page_slug,
                                          "title": page_slug.replace("-", " ")})
    _save_manifest(slug, data)
    touch_bubble(slug)


def rename_page(slug: str, page_slug: str, title: str) -> None:
    data = manifest(slug)
    old_title = ""
    for p in data["pages"]:
        if p["page_slug"] == page_slug:
            old_title = p["title"]
            p["title"] = title.strip() or page_slug
    _save_manifest(slug, data)
    touch_bubble(slug)
    new_title = title.strip() or page_slug
    if old_title and old_title.lower() != new_title.lower():
        _rewrite_title_links(slug, old_title, page_slug)


def set_page_hidden(slug: str, page_slug: str, hidden: bool) -> None:
    data = manifest(slug)
    for p in data["pages"]:
        if p["page_slug"] == page_slug:
            p["hidden"] = bool(hidden)
            _save_manifest(slug, data)
            touch_bubble(slug)
            return
    raise KeyError(page_slug)


def reorder_pages(slug: str, page_slugs: list[str]) -> None:
    data = manifest(slug)
    pages = data.get("pages", [])
    current = [p["page_slug"] for p in pages]
    if set(page_slugs) != set(current) or len(page_slugs) != len(current):
        raise ValueError("Page order must contain each existing page exactly once.")
    by_slug = {p["page_slug"]: p for p in pages}
    data["pages"] = [by_slug[s] for s in page_slugs]
    _save_manifest(slug, data)
    touch_bubble(slug)


def _rewrite_title_links(slug: str, old_title: str, page_slug: str) -> None:
    """Replace [[Old Title]] with [[page_slug]] across all pages after a title rename."""
    old_lower = old_title.lower()

    def repl(m: "re.Match") -> str:
        raw = m.group(1).strip()
        if "|" in raw:
            target, label = raw.split("|", 1)
            if target.strip().lower() == old_lower:
                return f"[[{page_slug}|{label.strip()}]]"
            return m.group(0)
        if raw.lower() == old_lower:
            return f"[[{page_slug}]]"
        return m.group(0)

    for p in manifest(slug).get("pages", []):
        path = paths.bubble_page_path(slug, p["page_slug"])
        try:
            content = path.read_text()
            new_content = _WIKILINK_RE.sub(repl, content)
            if new_content != content:
                _atomic_write(path, new_content)
        except Exception:  # noqa: BLE001
            pass


def delete_page(slug: str, page_slug: str) -> bool:
    try:
        with _review_page_lock(slug, page_slug):
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
            try:
                paths.bubble_page_comments_path(slug, page_slug).unlink()
            except OSError:
                pass
            touch_bubble(slug)
            return True
    except ReviewTargetError:
        return False


# --------------------------------------------------------------------------- #
# Figures / images
# --------------------------------------------------------------------------- #
_GIF_LOOP_EXTENSION = b"!\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"


def ensure_looping_gif(data: bytes) -> bytes:
    """Add GIF's standard infinite-loop extension when an animation lacks one.

    Browsers honour an animated GIF's loop metadata; replacing an ``img`` source can restart
    it, but cannot turn a single-play GIF into a repeating animation.  This lossless byte-level
    insertion happens before the GIF's first block, after its optional global colour table.
    """
    if not (data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 13):
        return data
    if b"NETSCAPE2.0" in data or b"ANIMEXTS1.0" in data:
        return data
    packed = data[10]
    global_table_size = 3 * (1 << ((packed & 0x07) + 1)) if packed & 0x80 else 0
    offset = 13 + global_table_size
    if offset > len(data):
        return data
    return data[:offset] + _GIF_LOOP_EXTENSION + data[offset:]


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
    if ext == ".gif":
        data = ensure_looping_gif(data)
    (adir / name).write_bytes(data)
    return f"/api/bubbles/{slug}/assets/{name}"


def list_bubble_images(slug: str) -> list[str]:
    adir = paths.bubble_assets_dir(slug)
    if not adir.exists():
        return []
    return sorted(f"/api/bubbles/{slug}/assets/{p.name}" for p in adir.iterdir() if p.is_file())


def list_bubble_assets(slug: str) -> list[dict]:
    """Return file metadata for the bubble's private assets directory."""
    adir = paths.bubble_assets_dir(slug)
    if not adir.exists():
        return []
    out = []
    for path in adir.iterdir():
        if not path.is_file():
            continue
        out.append({"name": path.name, "size": path.stat().st_size,
                    "url": f"/api/bubbles/{slug}/assets/{path.name}"})
    return sorted(out, key=lambda item: item["name"].lower())


def delete_bubble_asset(slug: str, filename: str) -> bool:
    """Remove one file from a bubble's assets directory, never a directory/path traversal."""
    safe = Path(filename).name
    if safe != filename or not safe:
        return False
    path = paths.bubble_assets_dir(slug) / safe
    if not path.is_file():
        return False
    path.unlink()
    return True


def read_bubble_text_asset(slug: str, filename: str, *, max_bytes: int = 1_000_000) -> str:
    """Read a small UTF-8 text asset for the in-app file viewer."""
    safe = Path(filename).name
    if safe != filename or not safe:
        raise ValueError("Bad filename.")
    path = paths.bubble_assets_dir(slug) / safe
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size > max_bytes:
        raise ValueError("This file is too large to view here. Download it instead.")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise ValueError("This is not a UTF-8 text file. Open or download it instead.") from e


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
