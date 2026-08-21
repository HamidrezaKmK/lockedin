"""Background ingest pipeline: extract text, metadata, and a paper summary.

Runs after every upload (via FastAPI BackgroundTasks). The summary is cached so the asset
report generation stay low-latency (read-once, reuse).

The whole pipeline is fail-safe: any error is logged and leaves the asset usable (it just
is marked as requiring attention for manual tagging). Reports are never auto-generated here.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from . import assets, models, paths

logger = logging.getLogger(__name__)

MAX_SUMMARY_INPUT_CHARS = 24_000   # cap text fed to the summarizer (keeps local models happy)
MAX_METADATA_INPUT_CHARS = 12_000

SUMMARY_SYSTEM = (
    "You are a research assistant. Summarize the following document for a grad student's "
    "personal knowledge base. Write concise Markdown with these sections: "
    "**TL;DR** (2-3 sentences), **Key contributions** (bullets), **Methods** (bullets), "
    "**Results / takeaways** (bullets), and **Why it matters**. Preserve important math using "
    "LaTeX with ONLY $...$ or $$...$$ delimiters — NEVER \\( \\) or \\[ \\]. "
    "Do not invent content beyond the document."
)

METADATA_SYSTEM = (
    "Extract bibliographic metadata from the beginning of this research paper. Return STRICT JSON "
    "only, exactly in this shape: {\"title\": \"paper title\", \"authors\": [\"First Last\"]}. "
    "Use an empty string or empty list when a field cannot be determined. Do not invent names or "
    "include affiliations, abstracts, venues, or commentary."
)


def extract_text_from_pdf(pdf_path: Path) -> str:
    """Extract text from all pages with pypdf. Returns '' on failure."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 — skip a bad page, keep the rest
                continue
        return "\n".join(parts).strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("PDF text extraction failed for %s: %s", pdf_path, e)
        return ""


def summarize_pdf(home: Path, pdf_id: str) -> str:
    """Summarize the cached full text via the active model; cache to summary.md."""
    text = assets.get_text(pdf_id)
    if not text:
        return ""
    snippet = text[:MAX_SUMMARY_INPUT_CHARS]
    meta = assets.load_meta(pdf_id)
    user = f"Title (if known): {meta.get('title', '')}\n\nDocument text:\n{snippet}"
    summary = models.complete(home, [{"role": "user", "content": user}],
                              system=SUMMARY_SYSTEM, temperature=0.2)
    assets.save_summary(pdf_id, summary)
    return summary


def _parse_paper_metadata(raw: str) -> tuple[str, list[str]]:
    """Tolerantly parse the model's strict-JSON title/author response."""
    raw = (raw or "").strip()
    candidates = []
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    candidates.append(raw)
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        title = str(data.get("title") or "").strip()
        authors = data.get("authors") or []
        if isinstance(authors, str):
            authors = [authors]
        if not isinstance(authors, list):
            authors = []
        clean_authors = []
        for author in authors:
            name = str(author).strip()
            if name and name not in clean_authors:
                clean_authors.append(name)
        return title, clean_authors
    return "", []


def extract_paper_metadata(home: Path, pdf_id: str, *, force: bool = False) -> dict:
    """Use the active model to capture a paper's canonical title and author list."""
    with paths.use_root(home):
        meta = assets.load_meta(pdf_id)
        if meta.get("metadata_extracted") and not force:
            return meta
        text = assets.get_text(pdf_id)
        if not text:
            return meta
        raw = models.complete(home, [{"role": "user", "content": text[:MAX_METADATA_INPUT_CHARS]}],
                              system=METADATA_SYSTEM, temperature=0)
        title, authors = _parse_paper_metadata(raw)
        return assets.update_asset(pdf_id, extracted_title=title, authors=authors,
                                   metadata_extracted=True)


def run_ingest(home: Path, pdf_id: str) -> None:
    """Post-upload pipeline. Bubbles are always assigned manually by the user."""
    with paths.use_root(home):
        try:
            meta = assets.load_meta(pdf_id)
        except FileNotFoundError:
            return
        # 1) extract full text
        try:
            text = extract_text_from_pdf(assets.pdf_path(pdf_id))
            if text:
                assets.save_text(pdf_id, text)
        except Exception as e:  # noqa: BLE001
            logger.warning("ingest extract failed for %s: %s", pdf_id, e)
            text = ""

        # 2) extract canonical title/authors (best-effort; separate from any user-facing title)
        try:
            extract_paper_metadata(home, pdf_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("ingest metadata extraction failed for %s: %s", pdf_id, e)

        # 3) summarize (best-effort)
        try:
            summarize_pdf(home, pdf_id)
            assets.update_asset(pdf_id, summarized=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("ingest summarize failed for %s: %s", pdf_id, e)
