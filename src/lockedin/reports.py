"""Streamed research chat (read-only) + chat-title helper.

The web app no longer writes to report pages with the model — that proved too unreliable with
small local models. Editing is done by the user directly in the Markdown editor (or by a strong
model in DEV_MODE). What remains here is:

* ``chat_stream`` — a knowledgeable, READ-ONLY research assistant grounded in the bubble's report
  pages, every tagged paper's summary, and the full text of any "deep-read" papers. It never
  edits anything; it just discusses. Context is bounded and the conversation is compacted
  internally so it fits a local model's window.
* ``generate_chat_title`` — a short, cute session name for the sidebar.

Each public generator yields SSE-ready dicts::

    {"type": "delta", "text": "..."}                         # incremental model output
    {"type": "done",  "full_response": "...", "chat_text": "..."}
    {"type": "error", "detail": "..."}
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, Optional

from . import assets, bubbles, models, paths

APP_USAGE_GUIDE = """\
# lockedin — User Guide

## Overview
lockedin is a local-first research assistant for grad students. Upload papers (PDFs), group them
into **Idea Bubbles** (topic tags), and maintain multi-page Markdown reports per bubble, with a
research chat sidebar and a switchable LLM backend.

## Navigation
The left sidebar has three views:
- **Assets** — your uploaded PDFs
- **Idea Bubbles** — topic groups with report wikis
- **Attention** — papers that need manual tagging review

## Assets (PDFs)
- **Upload**: drag/select a PDF, optionally give a title, comma-separated bubble tags, and a
  source URL. If you leave tags blank, the AI will auto-suggest them in the background.
- **Edit**: click any asset card to edit its title, tags, notes, and source URL; clear the
  attention flag; or open the PDF inline.
- **Auto-tagging**: uploaded papers without tags are queued for background summarization and
  tag suggestion. They appear in the **Attention** queue until reviewed.

## Idea Bubbles
- **Create**: click "+ New bubble" in the Bubbles view, or tag a PDF with a new name.
- **Suggestions**: auto-tagging suggestions stay in the Attention queue. They do not become
  visible bubbles until you apply them by editing the asset's tags.
- **Rename**: click the ✏️ pencil next to the bubble title inside its detail view.
- **Delete**: the 🗑 icon on the bubble card removes the bubble and all its pages.

## Report Editor (multi-page wiki)
Each approved bubble has a mini-wiki of Markdown pages. Open a bubble to enter the editor. You
write the reports yourself — the chat assistant is read-only and does not edit pages for you.

### Page tabs
- **+ Page** — create a new page (give it a title)
- Click a tab to switch pages (current page auto-saves first)
- **✕** on a tab deletes that page (the home/overview page cannot be deleted)

### Writing Markdown
The left half is the editor (plain Markdown); the right half is the live preview.

**Math** — use standard LaTeX delimiters:
- Inline: `$E = mc^2$`
- Display: `$$\\int_0^\\infty f(x)\\,dx$$`

**Numbered equations with cross-references**: put `\\label{name}` inside a `$$` block to
auto-number it, then reference it anywhere with `\\eqref{name}`, which renders as the matching
number. Forward references work.

**Wikilinks** — link between pages in the same bubble by title: `[[Overview]]`, `[[Key Papers]]`.
The system resolves titles to real page slugs on save.

### Toolbar buttons (top of the editor pane)
| Button | Action |
|--------|--------|
| `+ Page` | Create a new page |
| `⟳ synced` / `⟳ unsynced` | **Click to save.** Shows sync state. If an external edit was detected, confirms whether to load the remote version or overwrite with your edits. |
| `⊞ preview` / `⊟ preview` | Toggle the live preview pane |
| `chat ❮` / `chat ❯` | Collapse or expand the chat sidebar |
| `🔍` | Open a full-page standalone preview in a new tab (with a ← Back button) |

### Auto-sync
If you edit a page's `.md` file directly on disk (e.g. from another tool or a DEV_MODE session),
the browser detects the change within ~5 seconds and silently reloads the content — as long as
you have no unsaved local edits. If you do have unsaved edits, a toast notifies you and the ⟳
badge will prompt you to choose when you click to save.

## Chat Sidebar
The right pane is a **read-only** research assistant. It knows the full text of this bubble's
report pages, a summary of every tagged paper, and — when you attach them — the full text of
specific papers. Use it to ask questions, explain math, summarize, compare papers, and
brainstorm. It does **not** edit your report; if you want wording for a page, ask it to draft the
text and paste it into the editor yourself.

### Deep-read
Use the "📎 Add paper to deep-read…" dropdown at the bottom of the chat to attach specific PDFs.
The assistant then reads the full paper text (or the PDF itself, with Claude) for the
conversation, enabling detailed paper-specific questions.

### Sessions
Chat history is saved automatically. Use the session dropdown at the top of the chat pane to
switch between saved conversations or start a new one. The 📋 icon opens a session management
panel to delete old sessions.

## Sharing a bubble
Open a bubble and click **🔗 Share** in its header to publish an unlisted, read-only link.
While sharing is on the button shows **🟢 Sharing** and a **📋 Copy link** button appears —
clicking it copies a link to the page you currently have open. Send that link to anyone (no
login needed) and they see a rendered, read-only preview of the bubble, able to browse all its
pages. On the shared page, hovering a heading reveals a 🔗 that copies a link straight to that
section, so you can point someone at a specific part. Click **🟢 Sharing** again to turn it off;
the link stops working immediately. Turning it back on restores the same link.

## Account
Click your **@username** in the top bar to change your username and/or password. You must enter
your current password to confirm. Changing your username carries all your data over and keeps
you logged in.

## Model Settings
Click any model tab in the topbar (🖥 Qwen, OpenAI, Claude, Gemini) to switch the active model.
Click the ⚙ gear icon on a tab to configure its API key or endpoint. Models:
- **Qwen (local)** — runs via Ollama on your machine; private and free.
- **OpenAI** — GPT-4o and others; requires an API key.
- **Claude** — Anthropic models; requires an API key.
- **Gemini** — Google models via AI Studio; requires an API key.
Only the active model's health indicator (coloured dot) is checked on load.
"""

# A SHORT, model-safe summary for the chat system prompt — facts only, no fenced code blocks.
APP_USAGE_BRIEF = (
    "ABOUT THIS APP (use these facts to answer usage questions):\n"
    "- Reports are multi-page Markdown wikis, one per idea bubble; the editor is on the left, a "
    "live preview on the right. The user writes the reports themselves.\n"
    "- Math: $...$ inline, $$...$$ display. Number a display equation by putting \\label{name} "
    "inside its $$ block, then reference it anywhere with \\eqref{name} (renders as its number).\n"
    "- Link to another page in the same bubble by its title in double brackets, e.g. [[Overview]].\n"
    "- The synced/unsynced badge saves the current page; the preview toggle shows/hides the "
    "preview; the chat pane can be collapsed; the magnifier opens a full-page preview.\n"
    "- This chat is read-only: it discusses but cannot edit pages. Deep-read attaches a paper's "
    "full text to the conversation.\n"
    "- Click 🔗 Share in a bubble's header to publish an unlisted read-only link (a 📋 Copy-link "
    "button then appears); headings on the shared page have 🔗 anchors for section links.\n"
    "- Click your @username in the top bar to change your username or password.\n"
    "For the complete step-by-step guide, tell the user to click the ? button in the top bar."
)

CHAT_SYSTEM = """\
You are a knowledgeable research assistant embedded in a grad student's idea bubble. You discuss
the papers and the user's report notes: answer questions, explain the math, summarize, compare
papers, surface open questions, and help the user think.

This is a READ-ONLY conversation. You CANNOT edit the report and have no tools to do so. Never
claim you changed a page and never emit XML-ish edit tags. If the user wants text for a page,
draft it in your reply and remind them to paste it into the editor themselves.

You are given the full text of this bubble's report pages, a summary of every tagged paper, and —
when the user attaches them — the full text of specific "deep-read" papers. Ground every claim in
that material; if the answer isn't there, say so rather than inventing. Cite papers by title.

RESPONSE STYLE
Be concise: 1-4 sentences or a short bullet list by default. Skip preamble. Don't restate the
question. Only write at length when explicitly asked. For math use LaTeX with ONLY $...$ (inline)
and $$...$$ (display) delimiters — NEVER \\( \\) or \\[ \\].

APP REFERENCE
If the user asks how to use the website, editor, buttons, or a feature, answer from the facts
below. Do NOT show a fenced code block of these instructions — explain in prose.
""" + APP_USAGE_BRIEF

# Defensive scrub: a small local model may still echo a stray <EDIT>/<NEWPAGE> tag despite the
# read-only prompt. Strip any such tag from displayed text so raw XML never shows in chat.
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


# --------------------------------------------------------------------------- #
# Context budgeting — keep the assembled prompt within a local model's window
# --------------------------------------------------------------------------- #
_REPORT_CTX_BUDGET = 16000   # chars for report pages injected into the system prompt
_DEEPREAD_BUDGET = 14000     # chars of a single deep-read paper's full text (qwen/openai path)
_COMPACT_AT = 16             # compact the conversation when it exceeds this many messages


def _clip(text: str, budget: int, label: str = "content") -> str:
    text = text or ""
    if len(text) <= budget:
        return text
    return text[:budget] + f"\n\n…[{label} truncated — {len(text) - budget} more chars omitted]…"


def _report_context(slug: str, page_slug: str, cur_content: str, cur_title: str) -> str:
    """All report pages, current one in full first, others within the remaining char budget."""
    cur_block = f"===== PAGE: {cur_title} (currently open) =====\n{cur_content.strip()}"
    blocks = [cur_block]
    remaining = _REPORT_CTX_BUDGET - len(cur_block)
    for p in bubbles.list_pages(slug):
        if p["page_slug"] == page_slug:
            continue
        c = bubbles.get_page(slug, p["page_slug"]).strip()
        if not c:
            continue
        block = f"===== PAGE: {p['title']} =====\n{c}"
        if len(block) > remaining:
            block = _clip(block, max(remaining, 500), "page")
        blocks.append(block)
        remaining -= len(block)
        if remaining <= 0:
            break
    return "\n\n".join(blocks) or "(no report pages yet)"


# --------------------------------------------------------------------------- #
# Read-only research chat
# --------------------------------------------------------------------------- #
def chat_stream(home: Path, slug: str, page_slug: str, messages: list[dict],
                page_context: str = "", deep_read_ids: Optional[list[str]] = None) -> Iterator[dict]:
    """Stream a read-only research-assistant reply grounded in the bubble's reports + papers.

    The model only discusses — it never edits a page. The ``done`` event carries
    ``full_response`` (raw output, pushed to chat history) and ``chat_text`` (cleaned prose).

    Context assembly (bounded so it fits a local model's window):
    * every report page — current page in full, others within ``_REPORT_CTX_BUDGET``;
    * a summary of every tagged paper;
    * the full text of each deep-read PDF (Claude gets the PDF; others get clipped text);
    * the conversation itself, compacted via ``models.compact_chat`` once it grows past
      ``_COMPACT_AT`` turns.
    """
    with paths.use_root(home):
        name = bubbles.slug_to_name(slug)
        all_pages = bubbles.list_pages(slug)
        cur_content = page_context or bubbles.get_page(slug, page_slug)
        cur_title = next((p["title"] for p in all_pages if p["page_slug"] == page_slug), page_slug)

        report_ctx = _report_context(slug, page_slug, cur_content, cur_title)

        pdfs = bubbles.pdfs_for_bubble(slug)
        summaries = "\n\n".join(
            f"### {m.get('title', m['pdf_id'])}\n{assets.get_summary(m['pdf_id']) or '(no summary)'}"
            for m in pdfs) or "(no PDFs tagged yet)"

        system = (
            f"{CHAT_SYSTEM}\n\nIdea bubble: **{name}** — the user is currently viewing the page "
            f"**{cur_title}**.\n\n"
            f"REPORT PAGES (the user's living notes; quote/cite them, never reproduce the "
            f"`===== PAGE =====` markers):\n{report_ctx}\n\n"
            f"PAPER SUMMARIES:\n{summaries}")

        # Conversation, compacted internally when long so the window isn't blown.
        convo = list(messages)
        if len(convo) > _COMPACT_AT:
            try:
                convo = models.compact_chat(home, convo)
            except Exception:  # noqa: BLE001 — compaction is best-effort
                pass

        # Deep-read: attach the (budgeted) full text / PDF of each selected paper.
        for pid in (deep_read_ids or []):
            try:
                convo = models.attach_pdf(
                    home, convo, assets.pdf_path(pid),
                    fallback_text=_clip(assets.get_text(pid), _DEEPREAD_BUDGET, "paper text"),
                    label=assets.load_meta(pid).get("title", pid))
            except Exception:  # noqa: BLE001
                continue

        acc: list[str] = []
        try:
            for chunk in models.stream_chat(home, convo, system=system, temperature=0.4):
                acc.append(chunk)
                yield {"type": "delta", "text": chunk}
        except Exception as e:  # noqa: BLE001
            yield {"type": "error", "detail": str(e)}
            return

        full = _normalize_math("".join(acc))
        yield {"type": "done", "full_response": full,
               "chat_text": _STRAY_TAGS_RE.sub("", full).strip()}


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


def generate_chat_title(home: Path, messages: list[dict]) -> str:
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
                              system=_TITLE_SYSTEM, temperature=0.7)
    except Exception:  # noqa: BLE001 — title is cosmetic; never fail the chat over it
        return fallback
    return _clean_title(raw, fallback)
