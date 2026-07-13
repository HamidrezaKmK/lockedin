"""Background ingest pipeline: extract text -> summarize -> suggest idea-bubble tags.

Runs after every upload (via FastAPI BackgroundTasks). The summary is cached so chat and
report generation stay low-latency (read-once, reuse). Tag suggestion reuses the user's
existing bubbles when a paper fits one, inventing a new short tag only when none apply.

The whole pipeline is fail-safe: any error is logged and leaves the asset usable (it just
ends up in the attention queue for manual tagging). Reports are never auto-generated here.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from . import assets, bubbles, models, paths

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

TAG_SYSTEM = (
    "You classify research papers into short topic tags ('bubbles'). "
    "Reuse one of the user's EXISTING bubbles whenever the paper fits it; only invent a new "
    "tag when none apply. Tags are 1-4 words, lowercase, concept-level (e.g. 'diffusion models', "
    "'optimal transport', 'mechanistic interpretability'). Return STRICT JSON only: "
    '{"tags": ["...", "..."]} with 1-4 tags total.'
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
        meta["extracted_title"] = title
        meta["authors"] = authors
        meta["metadata_extracted"] = True
        assets.save_meta(pdf_id, meta)
        return meta


def _parse_tags(raw: str) -> list[str]:
    """Pull a tags list out of a model response (tolerant of stray prose / code fences)."""
    raw = raw.strip()
    candidates = []
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    candidates.append(raw)
    for c in candidates:
        try:
            obj = json.loads(c)
            tags = obj.get("tags") if isinstance(obj, dict) else obj
            if isinstance(tags, list):
                return [str(t).strip().lower() for t in tags if str(t).strip()][:4]
        except Exception:  # noqa: BLE001
            continue
    return []


def suggest_tags(home: Path, text: str, existing_bubbles: list[str]) -> list[str]:
    if not text:
        return []
    existing = ", ".join(existing_bubbles) if existing_bubbles else "(none yet)"
    user = (f"Existing bubbles to reuse when possible: {existing}\n\n"
            f"Paper excerpt:\n{text[:8000]}\n\n"
            "Return the JSON object of tags now.")
    raw = models.complete(home, [{"role": "user", "content": user}],
                          system=TAG_SYSTEM, temperature=0)
    return _parse_tags(raw)


def run_ingest(home: Path, pdf_id: str, had_user_tags: bool) -> None:
    """Post-upload pipeline. Always summarizes; suggests tags only when the user gave none."""
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
            meta = assets.load_meta(pdf_id)
            meta["summarized"] = True
            assets.save_meta(pdf_id, meta)
        except Exception as e:  # noqa: BLE001
            logger.warning("ingest summarize failed for %s: %s", pdf_id, e)

        # 4) suggest tags only when the user supplied none
        if not had_user_tags:
            try:
                existing = [b["name"] for b in bubbles.all_bubbles()]
                tags = suggest_tags(home, text, existing)
            except Exception as e:  # noqa: BLE001
                logger.warning("ingest tag-suggest failed for %s: %s", pdf_id, e)
                tags = []
            meta = assets.load_meta(pdf_id)
            meta["suggested_tags"] = tags
            meta["attention_flag"] = True   # always flag uncategorized uploads for review
            assets.save_meta(pdf_id, meta)
            for t in tags:
                bubbles.propose_bubble(t)
