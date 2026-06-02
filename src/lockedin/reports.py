"""Report generation + AI editing, all streamed (v2: page-targeted).

Each public function is a generator that yields SSE-ready dicts::

    {"type": "delta",   "text": "..."}     # incremental model output
    {"type": "done",    "content": "..."}  # final full page markdown
    {"type": "error",   "detail": "..."}   # something went wrong

The server forwards these straight to the client. Report generation is *gated*: it refuses
to run on an unapproved bubble, and writes the target page. Edits return a *proposal* the user
reviews as a diff (NOT saved server-side). Everything is grounded in cached per-PDF summaries
(cheap); chat can deep-read a PDF on demand for detail.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, Optional

from . import assets, bubbles, models, paths

GENERATE_SYSTEM = (
    "You are helping a grad student maintain a living research report (an 'idea bubble' page). "
    "Produce a well-structured Markdown page they can grow over time. Use '#' for the title and "
    "'##' for sections. Suggested sections: Overview, Key Papers, Mathematical Foundations, "
    "Open Questions, My Ideas. Render math with LaTeX using ONLY $...$ for inline and $$...$$ for "
    "display math. NEVER use \\( \\) or \\[ \\] delimiters. To link to "
    "another page in this bubble, write [[page-slug]]. Ground claims in the provided paper "
    "summaries; where unsure, leave a clearly marked TODO rather than inventing detail."
)

PAGE_EDIT_SYSTEM = (
    "You rewrite an entire Markdown research page per the user's instruction. Return ONLY the "
    "full replacement Markdown for the page (keep a leading '# ' title). No code fences, and no "
    "meta-commentary of ANY kind — no changelog lines, no 'Removed…/Updated…/Here is…/I have…' "
    "notes. The output is the page exactly as a reader sees it. Preserve [[...]] links and LaTeX "
    "math, using ONLY $...$ (inline) and $$...$$ (display) — NEVER \\( \\) or \\[ \\]."
)

SECTION_EDIT_SYSTEM = (
    "You edit one section of a Markdown research page. Return ONLY the replacement Markdown for "
    "that section, and it MUST begin with the exact '## ' heading line being replaced. No code "
    "fences, and no meta-commentary of ANY kind — no changelog lines, no 'Removed…/Updated…/Here "
    "is…/I have…' notes. The output is the section exactly as a reader sees it. Preserve [[...]] "
    "links and LaTeX math, using ONLY $...$ (inline) and $$...$$ (display) — NEVER \\( \\) or \\[ \\]."
)

CHAT_SYSTEM = """\
You are the research assistant embedded in a grad student's idea bubble. You answer questions,
brainstorm, and — when it makes sense — propose edits to the report pages.

RESPONSE STYLE
Be concise: 1-4 sentences or a short bullet list by default. Skip preamble. Don't restate the
question. Only write at length when explicitly asked. For math use LaTeX with ONLY $...$ (inline)
and $$...$$ (display) delimiters — NEVER \\( \\) or \\[ \\]. Cite papers when relevant.

PROPOSING EDITS
If the user asks you to rewrite, add, remove, or improve part of a report page, respond with:
1. One brief sentence explaining what you're changing (shown in chat).
2. Immediately after, an <EDIT> block in exactly this format:

To edit one section:
<EDIT section="## Exact Heading">
replacement content for that section (include the ## heading line)
</EDIT>

To rewrite the entire page:
<EDIT page="true">
full replacement markdown
</EDIT>

CREATING NEW PAGES
If the user asks you to create a new page (e.g. "add a page about X" or "write a dedicated
page for Y"), respond with a <NEWPAGE> block per page. To also link the new page from the
current page, add a separate <EDIT> block updating the relevant section.

<NEWPAGE title="Page Title">
# Page Title

## Section
Content here...
</NEWPAGE>

LINKING RULES (important — read carefully)
- To link to a page, write [[Exact Page Title]] using the page's TITLE. The system resolves the
  title to the correct page automatically — do NOT invent slugs, lowercase-dashed names, or
  path-like targets such as [[Section/Title]]. Just the plain title in double brackets.
- You may link a page you are creating in this same response by its title.

EDIT RULES
- Only propose an EDIT when the user clearly wants a change to the report. For questions,
  brainstorming, or clarifications, just reply conversationally — no EDIT block.
- ALWAYS close every block with its matching </NEWPAGE> or </EDIT> tag.
- The text inside an <EDIT> block IS the report page itself. It must be PURE report content —
  exactly what a reader should see. NEVER put meta-commentary inside it: no changelog notes, no
  "Removed duplicate links", "Updated the section", "Here is the revised page", "I have…", "Note:",
  etc. If you want to describe what you changed, write that as your ONE sentence BEFORE the
  <EDIT> block (it shows in chat) — never inside the block.
- Section edits: the block MUST begin with the exact '## Heading' line you are replacing, then
  only that section's content (up to the next ##). Do NOT bleed into adjacent sections.
- Preserve [[...]] links and LaTeX math. Math delimiters are ALWAYS $...$ or $$...$$, never \\( \\) or \\[ \\].
- If a previous edit was rejected and the user gives feedback, propose a revised EDIT that
  addresses the feedback.\
"""

# Matches <EDIT section="..."> or <EDIT page="true"> blocks. Tolerant of a MISSING closing
# tag: a block runs until </EDIT>, the next <EDIT>/<NEWPAGE>, or end-of-text. Small local
# models routinely drop closing tags — without this, the raw XML leaks into saved pages.
# Groups: (1) section heading or None, (2) "true" if page="true" else None, (3) content.
# A bare <EDIT> (neither attribute) is malformed — see chat_stream, where it's ignored rather
# than treated as a whole-page replacement (that would let echoed garbage nuke the page).
_EDIT_RE = re.compile(
    r'<EDIT(?:\s+section="([^"]*)")?(?:\s+page="(true)")?\s*>'
    r'(.*?)(?:</EDIT>|(?=<EDIT\b)|(?=<NEWPAGE\b)|$)',
    re.DOTALL | re.IGNORECASE,
)

# Matches <NEWPAGE title="..."> blocks, equally tolerant of a missing </NEWPAGE>.
_NEWPAGE_RE = re.compile(
    r'<NEWPAGE\s+title="([^"]*)"\s*>'
    r'(.*?)(?:</NEWPAGE>|(?=<NEWPAGE\b)|(?=<EDIT\b)|$)',
    re.DOTALL | re.IGNORECASE,
)

# Leftover stray closing tags to scrub from displayed/saved text.
_STRAY_TAGS_RE = re.compile(r'</?(?:EDIT|NEWPAGE)\b[^>]*>', re.IGNORECASE)

# Math delimiter normalization. The frontend renders $...$ / $$...$$ reliably; models (qwen
# especially) often emit \( \) / \[ \] despite the prompt. Convert them so math always renders.
_DISPLAY_MATH_RE = re.compile(r'\\\[(.+?)\\\]', re.DOTALL)
_INLINE_MATH_RE = re.compile(r'\\\((.+?)\\\)', re.DOTALL)


def _normalize_math(text: str) -> str:
    """Rewrite ``\\(..\\)`` → ``$..$`` and ``\\[..\\]`` → ``$$..$$`` (the only forms we render)."""
    text = _DISPLAY_MATH_RE.sub(lambda m: f"$${m.group(1).strip()}$$", text)
    text = _INLINE_MATH_RE.sub(lambda m: f"${m.group(1).strip()}$", text)
    return text


def _bubble_context(slug: str) -> str:
    """Titles + cached summaries of the PDFs in a bubble, for grounding generation."""
    pdfs = bubbles.pdfs_for_bubble(slug)
    if not pdfs:
        return "(No PDFs are tagged to this bubble yet.)"
    blocks = []
    for m in pdfs:
        summary = assets.get_summary(m["pdf_id"]) or "(not yet summarized)"
        blocks.append(f"### {m.get('title', m['pdf_id'])}\n{summary}")
    return "\n\n".join(blocks)


def _other_pages(slug: str, page_slug: str) -> str:
    others = [p for p in bubbles.list_pages(slug) if p["page_slug"] != page_slug]
    if not others:
        return "(none)"
    return ", ".join(f"[[{p['page_slug']}]] ({p['title']})" for p in others)


# --------------------------------------------------------------------------- #
# Generate (writes the target page)
# --------------------------------------------------------------------------- #
def generate_template(home: Path, slug: str, page_slug: str,
                      instructions: str = "", *, claude_token: str = "") -> Iterator[dict]:
    with paths.use_root(home):
        if not bubbles.is_approved(slug):
            yield {"type": "error", "detail": "Approve this idea bubble before generating a report."}
            return
        bubbles.ensure_pages(slug)
        name = bubbles.slug_to_name(slug)
        context = _bubble_context(slug)
        user = (f"Idea bubble: **{name}**\n\n"
                f"User instructions: {instructions or '(none — use your best judgment)'}\n\n"
                f"Other pages you can link to: {_other_pages(slug, page_slug)}\n\n"
                f"Source paper summaries:\n{context}\n\n"
                "Write the page now.")
        acc = []
        try:
            for chunk in models.stream_chat(home, [{"role": "user", "content": user}],
                                            system=GENERATE_SYSTEM, temperature=0.4,
                                            claude_token=claude_token):
                acc.append(chunk)
                yield {"type": "delta", "text": chunk}
        except Exception as e:  # noqa: BLE001
            yield {"type": "error", "detail": str(e)}
            return
        content = _normalize_math("".join(acc).strip())
        bubbles.save_page(slug, page_slug, content)
        yield {"type": "done", "content": content}


# --------------------------------------------------------------------------- #
# Section splicing
# --------------------------------------------------------------------------- #
def list_sections(page: str) -> list[str]:
    """Return the '## ' heading lines present in the page (edit units)."""
    return [ln.strip() for ln in page.splitlines() if ln.strip().startswith("## ")]


# A heading the model copied from the prompt framing, e.g. "## [Overview]" → "## Overview".
_BRACKET_HEADING_RE = re.compile(r'^(#+)\s*\[\s*(.*?)\s*\]\s*$')


def _clean_heading(h: str) -> str:
    """Strip the prompt's ``[...]`` framing so '## [Overview]' becomes '## Overview'."""
    h = h.strip()
    m = _BRACKET_HEADING_RE.match(h)
    return f"{m.group(1)} {m.group(2)}".strip() if m else h


def _heading_key(h: str) -> str:
    return _clean_heading(h).lower()


def _find_heading(lines: list[str], heading: str) -> Optional[int]:
    """Index of the line matching ``heading`` (bracket- and case-insensitive), else None."""
    key = _heading_key(heading)
    return next((i for i, ln in enumerate(lines)
                 if ln.strip().startswith("## ") and _heading_key(ln) == key), None)


def _splice_section(page: str, heading: str, new_block: str) -> str:
    """Replace the block from ``heading`` up to the next '## ' (or EOF) with ``new_block``."""
    block = new_block.strip()
    first = block.split("\n", 1)[0].strip()

    # The model frequently returns a WHOLE page (leading '# ' H1 title) under a section edit —
    # especially when it mistakes the page title for the section. Treat that as a full-page
    # replacement instead of appending, which is what caused edits to duplicate the page.
    if first.startswith("# ") and not first.startswith("## "):
        return block + ("" if block.endswith("\n") else "\n")

    clean = _clean_heading(heading)
    # Re-attach the heading if the model omitted it (avoids silently deleting the heading).
    if _heading_key(first) != clean.lower():
        block = f"{clean}\n\n{block}"

    lines = page.splitlines(keepends=True)
    start = _find_heading(lines, heading)
    if start is None:
        sep = "" if page.endswith("\n") or not page else "\n\n"
        return page + sep + block.strip() + "\n"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].strip().startswith("## "):
            end = j
            break
    replacement = block.strip() + "\n"
    if end < len(lines):
        replacement += "\n"
    return "".join(lines[:start]) + replacement + "".join(lines[end:])


def _extract_section(page: str, heading: str) -> str:
    lines = page.splitlines(keepends=True)
    start = _find_heading(lines, heading)
    if start is None:
        return heading
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].strip().startswith("## "):
            end = j
            break
    return "".join(lines[start:end]).strip()


# --------------------------------------------------------------------------- #
# Edit a page (whole page or one section) — returns a proposal, does NOT save
# --------------------------------------------------------------------------- #
def edit_page(home: Path, slug: str, page_slug: str, mode: str, instruction: str,
              page_context: str, section: Optional[str] = None,
              rejected_proposal: Optional[str] = None,
              *, claude_token: str = "") -> Iterator[dict]:
    with paths.use_root(home):
        links = _other_pages(slug, page_slug)
        refine = (f"\n\nIMPORTANT: A previous attempt was rejected by the user. "
                  f"That rejected proposal was:\n{rejected_proposal}\n\n"
                  f"The user's new instruction (what to change): {instruction}"
                  if rejected_proposal else "")
        if mode == "section":
            if not section:
                yield {"type": "error", "detail": "No section selected."}
                return
            block = _extract_section(page_context, section)
            system = SECTION_EDIT_SYSTEM
            user = (f"Instruction: {instruction}{refine}\n\nLinkable pages: {links}\n\n"
                    f"Current section to rewrite:\n{block}\n\nReturn the replacement section now.")
        elif mode == "page":
            system = PAGE_EDIT_SYSTEM
            user = (f"Instruction: {instruction}{refine}\n\nLinkable pages: {links}\n\n"
                    f"Current page:\n{page_context}\n\nReturn the full replacement page now.")
        else:
            yield {"type": "error", "detail": f"Unknown edit mode {mode!r}."}
            return

        acc = []
        try:
            for chunk in models.stream_chat(home, [{"role": "user", "content": user}],
                                            system=system, temperature=0.3,
                                            claude_token=claude_token):
                acc.append(chunk)
                yield {"type": "delta", "text": chunk}
        except Exception as e:  # noqa: BLE001
            yield {"type": "error", "detail": str(e)}
            return

        new_text = _normalize_math("".join(acc).strip())
        content = new_text if mode == "page" else _splice_section(page_context, section, new_text)
        # NOT saved here — the client reviews the proposal as a diff, then PUTs on accept.
        yield {"type": "done", "content": content}


# --------------------------------------------------------------------------- #
# Unified chat (answers questions AND proposes structured edits when appropriate)
# --------------------------------------------------------------------------- #
def chat_stream(home: Path, slug: str, page_slug: str, messages: list[dict],
                page_context: str = "", deep_read_ids: Optional[list[str]] = None,
                *, claude_token: str = "") -> Iterator[dict]:
    """Stream a unified chat+edit response.

    The model decides whether to reply conversationally or propose an edit by including an
    ``<EDIT>`` block.  Deltas are streamed live; on completion the ``done`` event carries:

    * ``full_response`` — the full raw model output (client should push to chat history so
      rejected edits are visible in future context).
    * ``chat_text`` — the prose before any ``<EDIT>`` block (cleaned for display).
    * ``edit_proposal`` — present only when the model proposed an edit:
        ``{"section": "## Heading" | None, "content": "<proposed full page markdown>"}``
      The content is already spliced (section edits are applied via ``_splice_section``,
      constraining the change to exactly that section — the rule-based boundary check).
    * ``new_page_proposals`` — present only when the model proposed new pages:
        ``[{"title": str, "content": str}]``. These are PROPOSALS, not created here — the
      client gates each behind its own Accept/Reject card, then commits via POST+PUT (which
      assigns the real slug and normalizes ``[[links]]`` on save). Deferring creation keeps
      page-adds consistent with edits (both gated) and lets link targets resolve to the real
      slug. Tag parsing tolerates a missing ``</NEWPAGE>``/``</EDIT>`` (small models drop them),
      so raw XML can never leak into a saved page.
    """
    with paths.use_root(home):
        name = bubbles.slug_to_name(slug)
        all_pages = bubbles.list_pages(slug)
        cur_content = page_context or bubbles.get_page(slug, page_slug)
        cur_title = next((p["title"] for p in all_pages if p["page_slug"] == page_slug), page_slug)

        # Present each page fenced by markers that are CLEARLY NOT markdown headings, so the
        # model can't mistake the page framing for an editable '## ' section (which is exactly
        # what made it copy "## [Overview]" into section= and append duplicate pages).
        page_blocks = []
        for p in all_pages:
            is_current = p["page_slug"] == page_slug
            content = cur_content if is_current else bubbles.get_page(slug, p["page_slug"])
            if not is_current and not content.strip():
                continue
            tag = " (CURRENT PAGE — the one you edit)" if is_current else ""
            page_blocks.append(
                f"===== PAGE: {p['title']}{tag} =====\n{content.strip()}\n===== END PAGE =====")
        all_pages_text = "\n\n".join(page_blocks) or "(no pages yet)"

        cur_sections = list_sections(cur_content)
        sections_hint = (", ".join(f'"{s}"' for s in cur_sections)
                         if cur_sections else "(none yet — the page has only a # title)")

        pdfs = bubbles.pdfs_for_bubble(slug)
        summaries = "\n\n".join(
            f"### {m.get('title', m['pdf_id'])}\n{assets.get_summary(m['pdf_id']) or '(no summary)'}"
            for m in pdfs) or "(no PDFs tagged yet)"
        system = (
            f"{CHAT_SYSTEM}\n\nIdea bubble: **{name}**\n\n"
            f"The bubble's pages are shown below, each fenced by `===== PAGE: … =====` markers. "
            f"Those markers are NOT part of any page — never reproduce them in an edit.\n\n"
            f"{all_pages_text}\n\n"
            f"You are editing **{cur_title}**. Its editable sections (the exact '## ' headings "
            f"in its markdown) are: {sections_hint}.\n"
            f"- To change ONE section, use <EDIT section=\"## Exact Heading\"> with a heading "
            f"copied EXACTLY from that list — no brackets, no page title — and start the block "
            f"with that same '## ' line.\n"
            f"- To restructure the WHOLE page, use <EDIT page=\"true\"> with the full markdown "
            f"(starting with the '# ' title). Do this when the change spans multiple sections.\n"
            f"- Refer to other pages by their title in [[double brackets]].\n\n"
            f"Paper summaries:\n{summaries}")

        convo = list(messages)
        for pid in (deep_read_ids or []):
            try:
                convo = models.attach_pdf(home, convo, assets.pdf_path(pid),
                                          fallback_text=assets.get_text(pid),
                                          label=assets.load_meta(pid).get("title", pid))
            except Exception:  # noqa: BLE001
                continue

        acc: list[str] = []
        try:
            for chunk in models.stream_chat(home, convo, system=system, temperature=0.5,
                                            claude_token=claude_token):
                acc.append(chunk)
                yield {"type": "delta", "text": chunk}
        except Exception as e:  # noqa: BLE001
            yield {"type": "error", "detail": str(e)}
            return

        full = _normalize_math("".join(acc))

        # --- parse <NEWPAGE> blocks into PROPOSALS (NOT created — the client gates each
        #     one with its own Accept/Reject card, then commits via POST+PUT) ---
        new_page_proposals: list[dict] = []
        for np in _NEWPAGE_RE.finditer(full):
            title = np.group(1).strip()
            content = _STRAY_TAGS_RE.sub("", np.group(2)).strip()
            if title and content:
                new_page_proposals.append({"title": title, "content": content})

        # Strip all <NEWPAGE> blocks from the displayed text
        stripped = _NEWPAGE_RE.sub("", full).strip()

        # --- process optional <EDIT> block ---
        # Use the first VALID edit: it must declare section="…" or page="true". A bare <EDIT>
        # is malformed (models sometimes echo the prompt scaffolding) — ignore it rather than
        # treat it as a whole-page replacement, which would let garbage nuke the page on Accept.
        em = next((m for m in _EDIT_RE.finditer(stripped)
                   if m.group(1) or m.group(2) == "true"), None)
        if em:
            section = em.group(1)  # None → full-page edit (page="true")
            new_content = _STRAY_TAGS_RE.sub("", em.group(3)).strip()
            chat_text = _STRAY_TAGS_RE.sub("", _EDIT_RE.sub("", stripped[:em.start()])).strip()
            current_page = page_context or bubbles.get_page(slug, page_slug)
            proposed = _splice_section(current_page, section, new_content) if section else new_content
            yield {"type": "done", "full_response": full, "chat_text": chat_text,
                   "edit_proposal": {"section": section, "content": proposed},
                   "new_page_proposals": new_page_proposals or None}
        else:
            # No valid edit. Scrub any bare/malformed <EDIT> scaffolding from the shown prose.
            chat_text = _STRAY_TAGS_RE.sub("", _EDIT_RE.sub("", stripped)).strip()
            yield {"type": "done", "full_response": full, "chat_text": chat_text,
                   "new_page_proposals": new_page_proposals or None}


# --------------------------------------------------------------------------- #
# Chat session titles — short, cute, model-generated names for the sidebar
# --------------------------------------------------------------------------- #
_TITLE_SYSTEM = (
    "You name a chat conversation for a sidebar list. Reply with ONLY the title: a short, cute, "
    "descriptive name of 2-5 words in Title Case. You may begin with a single fitting emoji. "
    "No surrounding quotes, no trailing punctuation, no explanation — just the title."
)


def _clean_title(raw: str, fallback: str) -> str:
    """First non-empty line of the model output, stripped of wrapping quotes / markdown /
    trailing punctuation (iteratively, since these can nest), and capped."""
    line = next((ln.strip() for ln in (raw or "").splitlines() if ln.strip()), "")
    for _ in range(4):
        before = line
        line = line.strip(" \t\"'`*_").strip()     # wrapping quotes / emphasis / backticks
        line = re.sub(r"^[#>\-\s]+", "", line)      # leading heading/bullet marks
        line = re.sub(r"[.!:;,]+$", "", line).strip()
        if line == before:
            break
    return line[:48] or fallback


def generate_chat_title(home: Path, messages: list[dict], *, claude_token: str = "") -> str:
    """A short, cute title for a chat session, grounded in its first couple of turns.

    Cheap, single non-streaming call on the active model. Falls back to a trimmed first user
    message if the model is unavailable or returns nothing usable.
    """
    parts: list[str] = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, str):
            content = "[attachment]"
        if m.get("role") in ("user", "assistant"):
            parts.append(f"{m['role']}: {content}")
        if len(parts) >= 3:
            break
    convo = "\n".join(parts)[:1500]
    first_user = next((m.get("content") for m in messages
                       if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
    fallback = (first_user or "New Chat").strip()[:40] or "New Chat"
    if not convo.strip():
        return fallback
    try:
        raw = models.complete(home, [{"role": "user", "content": f"Conversation:\n{convo}\n\nTitle:"}],
                              system=_TITLE_SYSTEM, temperature=0.7, claude_token=claude_token)
    except Exception:  # noqa: BLE001 — title is cosmetic; never fail the chat over it
        return fallback
    return _clean_title(raw, fallback)
