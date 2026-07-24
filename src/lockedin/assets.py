"""PDF asset storage: one directory per PDF under ``ASSETS/<pdf_id>/``.

Each asset dir holds:
  paper.pdf    the uploaded file
  text.txt     cached full text (pypdf) — written by the ingest task
  summary.md   cached LLM summary — written by the ingest task
  meta.yaml    metadata (title, tags, idea_bubbles, bubble_scores, flags, notes)

All paths resolve against the active per-user context root (see :mod:`lockedin.paths`),
so callers wrap operations in ``paths.use_root(home)``.
"""
from __future__ import annotations

import os
import re
import secrets
import threading
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

import httpx
import yaml
from slugify import slugify

from . import paths

MAX_PDF_BYTES = 200 * 1024 * 1024  # 200 MB
_META_LOCKS: dict[str, threading.RLock] = {}
_META_LOCKS_GUARD = threading.Lock()
_DOI_RE = re.compile(r'10\.\d{4,9}/[-._;()/:A-Z0-9]+', re.IGNORECASE)


def _meta_lock(pdf_id: str) -> threading.RLock:
    """Serialize metadata read-modify-write operations for one asset."""
    with _META_LOCKS_GUARD:
        return _META_LOCKS.setdefault(pdf_id, threading.RLock())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _open_access_pdf_fallback(url: str) -> str | None:
    """Find a repository PDF for a DOI when the publisher blocks automated downloads."""
    match = _DOI_RE.search(unquote(url))
    if not match:
        return None
    doi = match.group(0).rstrip(".,;)")
    try:
        response = httpx.get(f"https://api.openalex.org/works/https://doi.org/{doi}",
                             timeout=10, headers={"User-Agent": "lockedin"})
        response.raise_for_status()
        work = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    locations = [work.get("best_oa_location"), *(work.get("locations") or [])]
    seen: set[str] = set()
    for location in locations:
        if not isinstance(location, dict):
            continue
        candidate = str(location.get("pdf_url") or "").strip()
        landing = str(location.get("landing_page_url") or "").strip()
        parsed = urlparse(landing)
        host = (parsed.hostname or "").lower()
        # OpenAlex records HAL's repository landing URL rather than its direct PDF URL.  HAL
        # uses several valid prefixes (for example ``hal-`` and ``inria-``), not just ``hal-``.
        if not candidate and (host == "hal.science" or host.endswith(".hal.science")):
            record = parsed.path.rstrip("/")
            if re.fullmatch(r"/[a-z0-9]+-\d+(?:v\d+)?", record, re.IGNORECASE):
                candidate = f"{parsed.scheme or 'https'}://{parsed.netloc}{record}/document"
        if candidate and candidate not in seen:
            seen.add(candidate)
            return candidate
    return None


def fetch_pdf_from_url(url: str) -> tuple[bytes, str] | None:
    """Download ``url`` and return ``(pdf_bytes, filename)`` if it points to a PDF, else ``None``.

    Uses a fresh client (NOT any caller's per-user session) so no session cookie is ever sent to
    an external host. Raises on an unreachable/oversized URL; returns ``None`` when the link is
    reachable but isn't a PDF, so callers can fall back (Slack → Q&A; web → error message).

    Shared by the web asset upload (``service.fetch_and_save_asset``) and the Slack bot's
    bare-link handler so the download/validation logic lives in exactly one place.
    """
    resp = httpx.get(url, follow_redirects=True, timeout=30,
                     headers={"User-Agent": "lockedin"})
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError:
        fallback = _open_access_pdf_fallback(url)
        if not fallback:
            raise
        resp = httpx.get(fallback, follow_redirects=True, timeout=30,
                         headers={"User-Agent": "lockedin"})
        resp.raise_for_status()
    data = resp.content
    if len(data) > MAX_PDF_BYTES:
        raise ValueError("file is larger than the 200 MB limit")
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    # Trust the magic bytes over the header — servers often mislabel PDFs.
    if not (ctype == "application/pdf" or data[:5] == b"%PDF-"):
        return None
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "download.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return data, name


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
               tags: list[str] | None = None, url_source: str = "",
               bibliography: str = "") -> str:
    """Write a new PDF + its meta.yaml. Returns the new pdf_id."""
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise ValueError("file is larger than the 200 MB limit")
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
        "bubble_scores": {slug_of(t): 5 for t in tags},
        "attention_flag": True,
        "summarized": False,
        "extracted_title": "",
        "authors": [],
        "metadata_extracted": False,
        "notes": "",
        "bibliography": bibliography.strip(),
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
    with _meta_lock(pdf_id):
        meta = load_meta(pdf_id)
        if "tags" in fields and fields["tags"] is not None:
            tags = [t.strip() for t in fields["tags"] if t.strip()]
            old_scores = bubble_scores(meta)
            slugs = [slug_of(t) for t in tags]
            meta["tags"] = tags
            meta["idea_bubbles"] = slugs
            meta["bubble_scores"] = {slug: int(old_scores.get(slug, 5)) for slug in slugs}
            fields.pop("tags")
        for k in ("title", "notes", "url_source", "attention_flag", "bibliography",
                  "extracted_title", "authors", "metadata_extracted", "summarized"):
            if k in fields and fields[k] is not None:
                meta[k] = fields[k]
        save_meta(pdf_id, meta)
        return meta


def bubble_scores(meta: dict) -> dict[str, int]:
    """Return normalized per-bubble relevance scores for an asset.

    ``idea_bubbles`` remains the membership source of truth for compatibility. Missing legacy
    scores default to 5 so existing tagged papers keep maximum relevance until the user edits
    them.
    """
    raw = meta.get("bubble_scores") or {}
    out: dict[str, int] = {}
    for slug in meta.get("idea_bubbles", []) or []:
        try:
            score = int(raw.get(slug, 5))
        except (TypeError, ValueError):
            score = 5
        out[slug] = max(1, min(5, score))
    return out


def score_for_bubble(meta: dict, slug: str) -> int:
    return bubble_scores(meta).get(slug, 5)


# --------------------------------------------------------------------------- #
# BibTeX parsing / formatting
# --------------------------------------------------------------------------- #
_BIB_ENTRY_RE = re.compile(r"@([A-Za-z]+)\s*\{\s*([^,\s{}]+)\s*,", re.MULTILINE)
_BIB_FIELD_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9_-]*)\s*=\s*(\{(?:[^{}]|\{[^{}]*\})*\}|\"(?:[^\"\\]|\\.)*\"|[^,\n]+)",
    re.MULTILINE,
)


class BibtexError(ValueError):
    """Raised when BibTeX cannot be used as citation metadata."""


class DuplicateBibKeyError(BibtexError):
    """Raised when a BibTeX key is already used by this user."""

    def __init__(self, key: str, asset: dict | None = None):
        self.key = key
        self.asset = asset or {}
        title = self.asset.get("title") or self.asset.get("filename") or self.asset.get("pdf_id", "")
        suffix = f" on asset {title!r}" if title else ""
        super().__init__(f"Duplicate BibTeX key {key!r}{suffix}. Change the key and save again.")


def parse_bibtex_entries(text: str) -> list[dict]:
    """Parse enough BibTeX for citation keys and readable references.

    This is deliberately small: it extracts entries, keys, and common scalar fields without
    attempting full BibTeX macro or concatenation semantics.
    """
    text = text or ""
    entries: list[dict] = []
    matches = list(_BIB_ENTRY_RE.finditer(text))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].rsplit("}", 1)[0]
        fields = {}
        for fm in _BIB_FIELD_RE.finditer(body):
            key = fm.group(1).lower()
            value = fm.group(2).strip().rstrip(",").strip()
            if (value.startswith("{") and value.endswith("}")) or (
                value.startswith('"') and value.endswith('"')
            ):
                value = value[1:-1]
            fields[key] = re.sub(r"\s+", " ", value).strip()
        entries.append({"type": m.group(1).lower(), "key": m.group(2).strip(), "fields": fields})
    return entries


def bibtex_keys(text: str) -> list[str]:
    return [e["key"] for e in parse_bibtex_entries(text)]


def validate_bibtex_text(text: str) -> list[dict]:
    stripped = (text or "").strip()
    if not stripped:
        return []
    entries = parse_bibtex_entries(stripped)
    if not entries:
        raise BibtexError("BibTeX must contain at least one entry like @article{key, ...}.")
    seen: set[str] = set()
    for entry in entries:
        key = entry["key"]
        if key in seen:
            raise DuplicateBibKeyError(key)
        seen.add(key)
    return entries


def validate_bibtex_unique(pdf_id: str, text: str) -> list[dict]:
    """Validate BibTeX syntax and user-global key uniqueness for an asset save."""
    entries = validate_bibtex_text(text)
    keys = {e["key"] for e in entries}
    if not keys:
        return entries
    for meta in list_assets():
        other_id = meta.get("pdf_id")
        if other_id == pdf_id:
            continue
        for key in bibtex_keys(meta.get("bibliography", "")):
            if key in keys:
                raise DuplicateBibKeyError(key, meta)
    return entries


def format_bibtex_entry(entry: dict) -> str:
    f = entry.get("fields", {}) or {}
    parts: list[str] = []
    if f.get("author"):
        parts.append(f["author"])
    title = f.get("title")
    if title:
        parts.append(f'"{title}"')
    venue = f.get("journal") or f.get("booktitle") or f.get("publisher")
    if venue:
        parts.append(venue)
    if f.get("year"):
        parts.append(str(f["year"]))
    if f.get("doi"):
        parts.append(f"doi:{f['doi']}")
    elif f.get("url"):
        parts.append(f["url"])
    return ". ".join(p.strip().rstrip(".") for p in parts if p and p.strip()) + "."


def preview_bibtex(text: str) -> dict:
    entries = validate_bibtex_text(text)
    return {"entries": [{"key": e["key"], "text": format_bibtex_entry(e)} for e in entries]}


def delete_asset(pdf_id: str) -> bool:
    import shutil

    adir = paths.asset_dir(pdf_id)
    if not adir.exists():
        return False
    shutil.rmtree(adir)
    return True


def requires_attention() -> list[dict]:
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
