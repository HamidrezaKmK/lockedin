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

APP_USAGE_GUIDE_SECTIONS = [
    {
        "title": "Profile & Users",
        "content": """\
## Signing in

New accounts must be **approved by an admin** before they can log in. The very first account
created is automatically admin. Until approved, login shows *"Account is waiting for admin
approval."*

On phones, tap **☰** (top-left) to open the sidebar.

## Your account

Go to **⚙️ Settings → Account** to change your username or password. You must supply your
current password to confirm. A username change carries all your data over and keeps you
logged in. The **Log out** button is also in that section.

## Admin: managing users

Admins see a **User access** card in Settings. From there you can:
- **Approve** pending sign-ups so they can log in
- **Revoke** approval to lock an account
- **Delete** a user permanently

You cannot approve, revoke, or delete your own account.
""",
    },
    {
        "title": "Models & Chat",
        "content": """\
## Switching the active model

Open **⚙️ Settings**. The model cards show Qwen (local), OpenAI, Claude, and Gemini.
Double-click a card (or click **Use this model**) to activate it. Click **Configure** to
set the API key or Ollama endpoint for that provider.

| Provider | Notes |
|----------|-------|
| **Qwen (local)** | Runs via Ollama — private, free, no API key needed |
| **OpenAI** | GPT-4o and others; API key from platform.openai.com |
| **Claude** | Anthropic models; API key from console.anthropic.com |
| **Gemini** | Google models; API key from aistudio.google.com/apikey |

The coloured dot next to the active model updates when a health-check runs.

## Math macros

In **⚙️ Settings → Math Macros** you can define shorthand LaTeX commands. For example,
define `\\mu` → `\\mathbf{\\mu}` to use your own shorthand everywhere on the page.
Macros apply automatically to all math in your reports.

## Research chat

The chat sidebar (right pane, inside any bubble) is a **read-only** assistant: it
discusses your reports and papers but cannot edit pages. It knows:
- The full text of every page in the current bubble
- A summary of every tagged paper
- The full text of any PDF you attach via deep-read

**Deep-read** — use the 📎 dropdown at the bottom of the chat to attach specific PDFs.
With Claude the actual PDF is sent; with other models the extracted text is used.

**Sessions** — chat history is saved automatically. The session dropdown at the top of the
chat pane lets you switch between or delete saved conversations.

## News crawler (premium)

If enabled for your account, a **📰 News** view appears in the sidebar. Click **Crawl now**
to search the web for recent papers relevant to your approved bubbles. Found papers stream
into the feed; steer with follow-up messages, then **accept** (advances the date pointer)
or **discard** the batch.
""",
    },
    {
        "title": "Bubbles",
        "content": """\
## What is a bubble?

A **bubble** is a topic group. Each bubble has a name, a multi-page Markdown wiki,
attached papers, and a read-only research chat sidebar. Bubbles start in an unapproved
state when auto-suggested; you must approve them before the wiki opens.

## Creating & approving

Click **+ New bubble** in the Bubbles view, or tag any paper with a new topic name.
Auto-suggested bubbles land in the **🔔 Attention** queue — approve them there.

## Renaming & deleting

Rename a bubble from its detail view: an **approved** bubble is renamed via **🏷️ Edit titles**
in the view dropdown (the same mode that renames its pages); an **unapproved** one has a ✏️
pencil next to its name. The 🗑 icon on a bubble card deletes the bubble **and all its wiki pages**.

## Page tabs

Each bubble has a mini-wiki. The tabs at the top of the editor list all pages.

- **+ Page** — create a new page
- Click a tab to switch pages (the current page auto-saves first)
- **✕** on a tab deletes that page (the overview page cannot be deleted)
- Pick **🏷️ Edit titles** in the view dropdown to rename pages (and the bubble itself)

Renaming a page updates its display everywhere — existing links that used the old title
are rewritten automatically.

## Sharing

Click **🔗 Share** in the bubble header to publish an unlisted, read-only link. While
sharing is active the button shows **🟢 Sharing** and a **📋 Copy link** button appears.
Anyone with the link can browse all pages (no login needed). Toggle off to revoke
immediately; toggle back on to restore the same URL.

Hovering a heading on the shared page reveals a 🔗 anchor for deep-linking to a section.
""",
    },
    {
        "title": "TODOs",
        "content": """\
## Overview

TODOs are your personal task list (like GitHub issues). Each item has a numeric **id**,
a **title**, an optional Markdown **note**, and a **done** flag.

## Managing TODOs

Open the **✅ TODOs** view in the sidebar.

- **+ New TODO** — creates a new item; give it a title and optional note
- Click a TODO to expand and edit its note
- The **Open / Done** toggle at the top filters the list
- Check the checkbox to mark done (it moves to the Done list)
- The 🗑 button deletes a TODO — only allowed when no report page references it
- Deleting a TODO compacts the remaining ids; any shifted `@id` references in report pages
  are updated automatically

## Referencing TODOs in reports

Write `@5` in any report page to create a live link to TODO #5. It renders as a clickable
link showing the TODO title, with strikethrough if done.

`@50` never accidentally matches `@5` — the number must match exactly.
""",
    },
    {
        "title": "Assets",
        "content": """\
## Uploading papers

Use **📚 Assets** to add a PDF or a paper URL. You can optionally set:
- **Title** — defaults to the filename
- **Tags** — comma-separated topic names; each tag links the paper to a bubble
- **Source URL** — the paper's arXiv/DOI link

Leave tags blank to let the AI auto-suggest them in the background.

## Editing an asset

Click any asset card to open its detail panel:
- Edit title, tags, notes, source URL
- Open the paper PDF
- **Move to attention queue** if you want to review or summarize it later
- **Remove from attention queue** once it no longer needs attention

## Auto-tagging & the Attention queue

After upload, papers without tags are summarised and given suggested tags. They appear in
**🔔 Attention** until you review the suggestions. Auto-suggested bubbles also appear there
until approved.

The Assets view is always your full paper inventory. Attention is just a queue toggle on
those same assets: every paper stays visible in Assets whether or not it is in Attention.

## Tagging a paper into a bubble

When editing tags, use the tag name shown inside each bubble's detail view (visible as
a small label). Using a display name that differs from what's shown can create a duplicate
bubble — always copy the tag exactly from the bubble's detail view.
""",
    },
    {
        "title": "Slackbot",
        "content": """\
## Slackbot access

If your workspace has the lockedin Slack app installed, you can use it from Slack DMs or
by @-mentioning the bot in a channel.

{{SLACKBOT_INVITE}}

On first use, the bot asks for your lockedin username and password. After that, your Slack
user is linked to that lockedin account and the bot can refresh its session after restarts.
You only need to log in again if you change your lockedin username or password.

## What it can do

- **`select`** — choose the active bubble for questions
- **`list`** — show your bubbles
- **Ask a question** — the bot answers using your active bubble's reports and paper summaries
- **Attach a PDF** — uploads it to your Assets queue
- **Send a PDF link** — fetches and uploads the paper when the link resolves to a PDF
- **`todos`** — list, add, edit, complete, or remove open TODOs
- **`news`** — list retrieved news items and why they match your bubbles, if News is enabled
- **`crawl`** — run the premium News crawler from Slack, if enabled

The Slackbot follows the same account approval and per-user workspace rules as the website.
""",
    },
    {
        "title": "Editing Guide",
        "content": """\
## The editor

Open any bubble and use the view dropdown in the toolbar:
- **◧ Split** — editor left, live preview right
- **✏️ Edit** — plain Markdown editor
- **👁 Read** — rendered reading view (default)
- **🏷️ Edit titles** — rename the bubble and its pages inline; pick another view (or press
  Enter in a title) to save

**Save**: click the **⟳ synced** badge, or press **Ctrl/⌘+S**.

The editor toolbar shows: **↶ undo**, **↷ redo**, insert table, insert image, insert link.

### On mobile

The bubble page is streamlined to the essentials: the page tabs, the **⟳ synced** badge, and
the view dropdown. Switch between **👁 Read** and **✏️ Edit** right from that dropdown. A small
**↗** link in the top-right corner opens the read-only preview of the current page. Papers and
the research chat live behind the floating **📚** and **💬** buttons. (Sharing is done from a
larger screen.)

---

## Markdown

Standard CommonMark: headings `#`, bold `**`, italic `*`, lists, blockquotes `>`,
fenced code blocks, tables, images. Nothing unusual here.

---

## Math

Inline math goes between single dollar signs:

| What you type | What you get |
|---------------|--------------|
| `$E = mc^2$` | $E = mc^2$ |
| `$\\nabla \\cdot F = 0$` | $\\nabla \\cdot F = 0$ |

For a centred display equation, use double dollar signs. Typing

```
$$\\int_0^\\infty e^{-x}\\,dx = 1$$
```

renders as:

$$\\int_0^\\infty e^{-x}\\,dx = 1$$

For multi-line aligned equations use an `align` environment:

```
\\begin{align}
f(x) &= x^2 + 2x + 1 \\\\
     &= (x+1)^2
\\end{align}
```

**Supported environments:** `align`, `equation`, `gather`, `multline`, `alignat`
(and their starred variants). **Do not use** `\\( \\)` or `\\[ \\]`.

### Numbered equations

Add `\\label{eq:name}` on any line inside a display block to give it a number.
Lines without a label show no number. Numbers are sequential **across the whole
bubble** — every page's equations share one counter, in page order.

| What you type | What you get |
|---------------|--------------|
| `\\eqref{eq:name}` | a clickable **(n)** |
| `\\ref{eq:name}` | the bare number **n** |

References are **global**: you can `\\eqref` a label before it appears, and even
one defined on another page of the bubble. They also work **inside** a math block.

---

## Theorem environments

Write `\\begin{theorem}[optional title] ... \\end{theorem}` to get a styled box.
Here is what it looks like:

\\begin{theorem}[Example Theorem]
For any $n \\geq 1$, we have $\\sum_{k=1}^n k = \\frac{n(n+1)}{2}$.
\\end{theorem}

\\begin{proof}
By induction: the base case $n=1$ gives $1 = 1$. The inductive step follows from
$\\sum_{k=1}^{n+1} k = \\frac{n(n+1)}{2} + (n+1) = \\frac{(n+1)(n+2)}{2}$.
\\end{proof}

**All supported environments:**

| Environment | Numbered? |
|-------------|-----------|
| `theorem` | ✓ (own counter) |
| `lemma` | ✓ (own counter) |
| `corollary` | ✓ (own counter) |
| `definition` | ✓ (own counter) |
| `proposition` | ✓ (own counter) |
| `remark` | ✓ (own counter) |
| `proof` | ✗ — ends with ∎ |

The optional `[title]` appears after the number. For example,
`\\begin{theorem}[Spectral Theorem]` renders as **Theorem 1 (Spectral Theorem)**.

### Cross-referencing environments

Put `\\label{thm:key}` inside any environment (it is hidden in the display).
Then write `\\thmref{thm:key}` anywhere — on this or any other page of the
bubble, and even inside a math block — to get an inline reference. Theorem
counters are bubble-wide too. For example, `\\thmref{thm:key}` → **Theorem 2**.

---

## Wikilinks

Link to another page in the same bubble by writing its title in double brackets:

| What you type | What you get |
|---------------|--------------|
| `[[Overview]]` | a link to the page titled "Overview" |
| `[[My Page\\|see here]]` | a link with custom label "see here" |

Links always display the page's current title. Renaming a page automatically updates
any links that used the old title.

---

## Math macros

Define your own shorthand commands in **⚙️ Settings → Math Macros**. For example,
you might define `\\bmu` → `\\boldsymbol{\\mu}` so you can write `$\\bmu$` everywhere.
Macros are applied automatically to all math on every page.

---

## TODOs in reports

Type an `@` followed by a TODO's number anywhere in a page to link to it — for example
`@5` links to TODO number 5. It renders as a clickable link showing the TODO's title
(struck through if the TODO is done). The number must match exactly, so `@50` never
accidentally links to TODO 5.
""",
    },
]

# Backwards-compat alias used by the /api/help endpoint
APP_USAGE_GUIDE = "\n\n".join(s["content"] for s in APP_USAGE_GUIDE_SECTIONS)


def guide_section(title: str) -> str:
    """Return the markdown body of the usage-guide section with the given title (matched by
    title, not index), or "" if there is none. Single source of truth for the `lockedin
    editguide` CLI command and the scientist launchers."""
    return next((s["content"] for s in APP_USAGE_GUIDE_SECTIONS if s["title"] == title), "")

# A SHORT, model-safe summary for the chat system prompt — facts only, no fenced code blocks.
APP_USAGE_BRIEF = (
    "ABOUT THIS APP (use these facts to answer usage questions):\n"
    "- Reports are multi-page Markdown wikis, one per bubble; the editor is on the left, a "
    "live preview on the right. The user writes the reports themselves.\n"
    "- Math: $...$ inline, $$...$$ display. Number a display equation by putting \\label{name} "
    "inside its $$ block, then reference it with \\eqref{name} (renders as its number). Equation "
    "and theorem (\\thmref) numbers/refs are bubble-wide — they work across pages and inside math.\n"
    "- Link to another page in the same bubble by its title in double brackets, e.g. [[Overview]].\n"
    "- The synced/unsynced badge saves the current page; the preview toggle shows/hides the "
    "preview; the chat pane can be collapsed; the magnifier opens a full-page preview.\n"
    "- This chat is read-only: it discusses but cannot edit pages. Deep-read attaches a paper's "
    "full text to the conversation.\n"
    "- Click 🔗 Share in a bubble's header to publish an unlisted read-only link (a 📋 Copy-link "
    "button then appears); headings on the shared page have 🔗 anchors for section links.\n"
    "- Go to ⚙️ Settings → Account to change your username or password, or to log out.\n"
    "For the complete step-by-step guide, tell the user to click the ? button in the top bar."
)

CHAT_SYSTEM = """\
You are a knowledgeable research assistant embedded in a grad student's bubble. You discuss
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
