"""PDF asset storage: one directory per PDF under ``ASSETS/<pdf_id>/``.

Each asset dir holds:
  paper.pdf    the uploaded file
  text.txt     cached full text (pypdf) — written by the ingest task
  summary.md   cached LLM summary — written by the ingest task
  meta.yaml    metadata (title, tags, idea_bubbles, flags, notes)

All paths resolve against the active per-user context root (see :mod:`lockedin.paths`),
so callers wrap operations in ``paths.use_root(home)``.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone

import yaml
from slugify import slugify

from . import paths


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug_of(tag: str) -> str:
    return slugify(tag)


def _atomic_write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #
def save_asset(pdf_bytes: bytes, filename: str, title: str = "",
               tags: list[str] | None = None, url_source: str = "") -> str:
    """Write a new PDF + its meta.yaml. Returns the new pdf_id."""
    pdf_id = secrets.token_hex(6)  # 12 hex chars
    adir = paths.asset_dir(pdf_id)
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "paper.pdf").write_bytes(pdf_bytes)
    tags = [t.strip() for t in (tags or []) if t.strip()]
    meta = {
        "pdf_id": pdf_id,
        "title": title.strip() or filename,
        "filename": filename,
        "url_source": url_source.strip(),
        "tags": tags,
        "idea_bubbles": [slug_of(t) for t in tags],
        "attention_flag": False,
        "suggested_tags": [],
        "summarized": False,
        "notes": "",
        "date_added": _now_iso(),
    }
    save_meta(pdf_id, meta)
    return pdf_id


# --------------------------------------------------------------------------- #
# Meta I/O
# --------------------------------------------------------------------------- #
def meta_path(pdf_id: str):
    return paths.asset_dir(pdf_id) / "meta.yaml"


def exists(pdf_id: str) -> bool:
    return meta_path(pdf_id).exists()


def load_meta(pdf_id: str) -> dict:
    path = meta_path(pdf_id)
    if not path.exists():
        raise FileNotFoundError(f"No asset {pdf_id!r}.")
    return yaml.safe_load(path.read_text()) or {}


def save_meta(pdf_id: str, meta: dict) -> None:
    _atomic_write(meta_path(pdf_id), yaml.safe_dump(meta, sort_keys=False, allow_unicode=True))


def list_assets() -> list[dict]:
    base = paths.ASSETS_DIR
    if not base.exists():
        return []
    out = []
    for d in base.iterdir():
        if d.is_dir() and (d / "meta.yaml").exists():
            try:
                out.append(yaml.safe_load((d / "meta.yaml").read_text()) or {})
            except Exception:  # noqa: BLE001 — skip a corrupt meta, don't crash the listing
                continue
    out.sort(key=lambda m: m.get("date_added", ""), reverse=True)
    return out


def update_asset(pdf_id: str, **fields) -> dict:
    """Patch allowed fields; keep tags<->idea_bubbles in sync."""
    meta = load_meta(pdf_id)
    if "tags" in fields and fields["tags"] is not None:
        tags = [t.strip() for t in fields["tags"] if t.strip()]
        meta["tags"] = tags
        meta["idea_bubbles"] = [slug_of(t) for t in tags]
        fields.pop("tags")
    for k in ("title", "notes", "url_source", "attention_flag", "suggested_tags"):
        if k in fields and fields[k] is not None:
            meta[k] = fields[k]
    save_meta(pdf_id, meta)
    return meta


def delete_asset(pdf_id: str) -> bool:
    import shutil

    adir = paths.asset_dir(pdf_id)
    if not adir.exists():
        return False
    shutil.rmtree(adir)
    return True


def attention_queue() -> list[dict]:
    return [m for m in list_assets() if m.get("attention_flag")]


# --------------------------------------------------------------------------- #
# Cached text / summary
# --------------------------------------------------------------------------- #
def pdf_path(pdf_id: str):
    return paths.asset_dir(pdf_id) / "paper.pdf"


def get_text(pdf_id: str) -> str:
    p = paths.asset_dir(pdf_id) / "text.txt"
    return p.read_text() if p.exists() else ""


def save_text(pdf_id: str, text: str) -> None:
    _atomic_write(paths.asset_dir(pdf_id) / "text.txt", text)


def get_summary(pdf_id: str) -> str:
    p = paths.asset_dir(pdf_id) / "summary.md"
    return p.read_text() if p.exists() else ""


def save_summary(pdf_id: str, summary: str) -> None:
    _atomic_write(paths.asset_dir(pdf_id) / "summary.md", summary)
