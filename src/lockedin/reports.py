"""Streamed research chat (read-only) + chat-title helper.

The web app no longer writes to report pages with the model — that proved too unreliable with
small local models. Editing is done by the user directly in the Markdown editor or through the
synchronized Scientist client. What remains here is:

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

New accounts can log in immediately after sign-up. The very first account becomes the
admin and starts with premium access. Later sign-ups are standard accounts by default: they
can use the site right away, but premium-only server compute is not enabled until an admin
upgrades them.

On phones, tap **☰** (top-left) to open the sidebar.

## Theme modes

Use the theme button in the top bar to cycle the colour scheme. Open
**⚙️ Settings → Aesthetics** to choose which of **🌙 Dark, ☀️ Light, 🦄 Pink, 🤖 Techno,**
and **⚪ Pearl** appear in that cycle. Your selection is remembered for your workspace and
also limits the theme switcher on every public page you share. The landing page is always Dark.

## Your account

Click your username in the top-right account menu to see whether you are **standard** or
**premium**, request premium access, or log out.

Go to **⚙️ Settings → Account** to change your username or password. You must supply your
current password to confirm. A username change carries all your data over and keeps you
logged in.

## Admin: managing users

Admins see a **User access** card in Settings. From there you can:
- **Upgrade to premium** for users who should be allowed to use server-side Qwen and News
- **Remove premium** to move an account back to bring-your-own-key model usage
- **Delete** a user permanently

Users who request premium from the top-right account menu appear with a **requested** badge.
You cannot delete your own account.
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
| **Qwen (premium)** | Runs on the server via Ollama; available only for premium accounts |
| **OpenAI** | GPT-4o and others; API key from platform.openai.com |
| **Claude** | Anthropic models; API key from console.anthropic.com |
| **Gemini** | Google models; API key from aistudio.google.com/apikey |

Standard accounts use OpenAI, Claude, or Gemini with their own API key. This keeps public
sign-ups from consuming the server's local compute by default.

The coloured dot next to the active model updates when a health-check runs.

## Math macros

In **⚙️ Settings → Math Macros**, click **Show macros** to expand the macro editor.
You can define shorthand LaTeX commands; for example, define `\\mu` →
`\\mathbf{\\mu}` to use your own shorthand everywhere on the page. Macros apply
automatically to all math in your reports.

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

If your account is premium, a **📰 News** view appears in the sidebar. Click **Crawl now**
to search the web for recent papers relevant to your approved bubbles. Found papers stream
into the feed; steer with follow-up messages, then **accept** (advances the date pointer)
or **discard** the batch. Standard users can request premium from the top-right account menu.
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
- Drag a tab to reorder pages; page order controls wiki navigation and bubble-wide numbering
- **✕** on a tab deletes that page (the overview page cannot be deleted)
- Pick **🏷️ Edit titles** in the view dropdown to rename pages (and the bubble itself)

The eye icon beside **Insert link** in the editor toolbar hides or shows the current page in
read-only previews and public shares. Hidden pages are omitted from the tab bar by default.
Use the **☷ Show hidden pages** control beside the sync icon to reveal them; they appear in a
separate right-aligned group until you hide them again.

## Quick-add a paper

Open **📚 Papers** in the page controls to see the bubble's attached papers and a **Quick add
paper** form. Paste a PDF URL, provide a title, and optionally paste BibTeX. Quick add fetches
the PDF as a new asset, attaches it directly to the current bubble at relevance 5, and saves the
BibTeX when valid. You can edit or add BibTeX later from the asset detail page.

Renaming a page updates its display everywhere — existing links that used the old title
are rewritten automatically.

## Sharing

Click **🔗 Share** in the bubble header to publish an unlisted, read-only link. While
sharing is active the button shows **🟢 Sharing** and a **📋 Copy link** button appears.
Anyone with the link can browse all pages (no login needed). Toggle off to revoke
immediately; toggle back on to restore the same URL.

Hovering a heading on the shared page reveals a 🔗 anchor for deep-linking to a section.
Shared pages have a theme-cycle button, restricted to the themes you enabled in **Aesthetics**.
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

- **+ New TODO** — opens a title form and creates a new item
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

While editing a report page, type `@` to open a TODO autocomplete menu in the top-right
of the editor. Use ↑/↓ to move, Enter/Tab to insert, or click an item.
""",
    },
    {
        "title": "Assets",
        "content": """\
## Uploading papers

Use **📚 Assets** to add a PDF or a paper URL. You can optionally set:
- **Title** — defaults to the filename
- **Extracted metadata** — the active model records a canonical paper title and author list
  from the PDF during background processing; your chosen asset title is preserved
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

## Finding assets

The Assets page has filters above the card grid:
- **Search** — matches your title, extracted paper title, authors, filename, PDF id, source URL,
  notes, tags, suggested tags, and BibTeX keys
- **Bubble** — limits results to assets attached to that bubble

You can combine both filters, for example selecting a bubble and typing part of a paper
title. Asset cards with saved BibTeX show a small **✓ BibTeX** badge.

## BibTeX and citations

An asset can optionally store BibTeX. Click an asset card, then use **+ BibTeX** or
**Edit BibTeX**. Paste entries such as:

```
@article{bases4spaces,
  title={...},
  author={...},
  year={...}
}
```

Click **Save BibTeX** to validate and save. BibTeX keys must be unique across your
assets. While editing, the panel shows a live preview of how the reference will render
inside a bubble page.

In a report page, cite an attached asset with `\\cite{key}`. A page may only cite keys
from assets attached to that same bubble; unknown or unattached keys are rejected on save.
References render as numbered citations, and each rendered page gets a **References**
section when the bubble has citations.

## Tagging a paper into a bubble

When editing or uploading an asset, use **➕ Pick an existing bubble…** to attach it
to an existing bubble. The picker uses the bubble's stable tag, so it avoids accidentally
creating a duplicate bubble after a rename. You can still type new comma-separated tags
when you intentionally want to create or suggest another topic.
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

The Slackbot follows the same account, premium, and per-user workspace rules as the website.
Questions use your configured model. Qwen from Slack also requires premium; otherwise configure
OpenAI, Claude, or Gemini with your own API key in the web settings.
""",
    },
    {
        "title": "Scientist CLI",
        "content": """\
## Work on a bubble from your terminal

`lockedin-scientist` is a small companion for an already installed **Codex**, **Claude Code**,
or **Antigravity** CLI. It keeps an authorized local mirror of your LockedIn workspace in sync
while the coding assistant works. It does not install the LockedIn server or send your workspace
to a model by itself.

### Install

On macOS or Linux (Python 3.11+):

```
curl -fsSL https://raw.githubusercontent.com/HamidrezaKmK/lockedin/scientist/install.sh | bash
```

On Windows PowerShell (Python 3.11+):

```
irm https://raw.githubusercontent.com/HamidrezaKmK/lockedin/scientist/install.ps1 | iex
```

The installer adds `lockedin-scientist` to your user PATH. On macOS/Linux, ensure
`~/.local/bin` is on PATH if your shell cannot find the command.

### Sign in and choose work

Authorize a device once; the command opens a browser page on the server you specify:

```
lockedin-scientist login --server https://your-lockedin.example
```

List the bubbles currently approved for this account. This is deterministic and does not start
an AI assistant:

```
lockedin-scientist bubbles
```

Then launch the coding CLI you already use with a bubble slug from that list:

```
lockedin-scientist codex <bubble-slug>
lockedin-scientist claude <bubble-slug>
lockedin-scientist agy <bubble-slug>
```

The CLI verifies the slug before launching an assistant. It uses the vendor CLI's normal
interactive approval behavior; it does not enable auto-approval or bypass permissions.

### Sync behavior and scope

The initial launch pulls your safe workspace content into a durable local mirror. During a
session it checks for website and local changes every five seconds and pushes local report edits
with revision checks. A concurrent website edit is preserved and recorded as a retry packet
instead of being overwritten.

Only pages and report assets inside an **approved** bubble can be written back. Credentials,
sessions, chat history, TODOs, bubble settings, and paper PDFs are never writable by Scientist.
Use `lockedin-scientist sync` to pull/push once without launching a coding CLI.
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

**Save**: click the leftmost sync icon in the editor toolbar, or press **Ctrl/⌘+S**. It changes to
**✎** while there are unsaved edits and **⚠** when a disk conflict needs attention.

The editor toolbar shows: **↶ undo**, **↷ redo**, text colour, insert table, insert image,
insert link, the page visibility eye, **☷ Show hidden pages**, and the leftmost sync icon. The eye hides
or shows the current page in read-only previews and public shares; hidden page tabs stay out of
the way until you use **☷**, then appear right-aligned in the tab bar.

Use **⛶** in the page controls to enter a focused workspace that hides the navigation and chat;
click it again to exit.

### On mobile

The bubble page is streamlined to the essentials: page tabs, the editor toolbar, and the view
dropdown. Switch between **👁 Read** and **✏️ Edit** right from that dropdown. A small
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

While editing, type `\\eqref{` to open an equation-label autocomplete menu.

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
| `assumption` | ✓ (own counter) |
| `remark` | ✓ (own counter) |
| `proof` | ✗ — ends with ∎ |

The optional `[title]` appears after the number. For example,
`\\begin{theorem}[Spectral Theorem]` renders as **Theorem 1 (Spectral Theorem)**.

### Cross-referencing environments

Put `\\label{thm:key}` inside any environment (it is hidden in the display).
Then write `\\thmref{thm:key}` anywhere — on this or any other page of the
bubble, and even inside a math block — to get an inline reference. Theorem
counters are bubble-wide too. For example, `\\thmref{thm:key}` → **Theorem 2**.

While editing, type `\\thmref{` to open an autocomplete menu for theorem, definition, lemma,
proposition, assumption, corollary, and remark labels.

---

## Citations

Assets with saved BibTeX can be cited from pages in bubbles where those assets are attached.
Write `\\cite{bibtex-key}` to render a numbered citation like **[1]**. The rendered page
also includes a **References** section for the bubble's citation set.

Type `\\cite{` in the editor to open a citation-key autocomplete menu. Only keys from
assets attached to the current bubble are suggested. Use ↑/↓, Enter/Tab, Escape, or click,
the same as the other editor autocomplete menus.

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

Type `@` in the editor to open the TODO autocomplete menu.
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
    "- In Settings → Aesthetics, users choose which themes appear in their top-bar cycle. Those "
    "same enabled themes are the only choices on owner previews and public share links; the "
    "landing page is always Dark.\n"
    "- Math: $...$ inline, $$...$$ display. Number a display equation by putting \\label{name} "
    "inside its $$ block, then reference it with \\eqref{name} (renders as its number). Equation "
    "and theorem (\\thmref) numbers/refs are bubble-wide — they work across pages and inside math.\n"
    "- The editor has autocomplete menus: type \\cite{ for attached-asset BibTeX keys, \\eqref{ "
    "for equation labels, \\thmref{ for theorem/definition labels, and @ for TODO ids.\n"
    "- Assets can store optional BibTeX. BibTeX keys must be unique; report pages can only cite "
    "keys from assets attached to that bubble. Asset cards with BibTeX show a ✓ BibTeX badge, "
    "and the Assets page can filter by search text plus bubble.\n"
    "- Link to another page in the same bubble by its title in double brackets, e.g. [[Overview]].\n"
    "- The sync icon beside Insert link saves the current page; the eye beside it hides/shows "
    "the current page in previews and shares; the chat pane can be collapsed; the magnifier "
    "opens a full-page preview.\n"
    "- This chat is read-only: it discusses but cannot edit pages. Deep-read attaches a paper's "
    "full text to the conversation.\n"
    "- Click 🔗 Share in a bubble's header to publish an unlisted read-only link (a 📋 Copy-link "
    "button then appears); headings on the shared page have 🔗 anchors for section links.\n"
    "- Go to ⚙️ Settings → Account to change your username or password, or to log out.\n"
    "For the complete step-by-step guide, tell the user to click the Help button in the top bar."
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


def bubble_paper_context(slug: str, *, include_paths: bool = False,
                         summary_budget: int | None = None) -> str:
    """Paper context for one bubble only, sorted by relevance.

    This is the shared source for web chat and scientist sessions. It deliberately starts from
    ``bubbles.pdfs_for_bubble`` so unrelated assets in the user's global ASSETS directory never
    enter the generated context.
    """
    pdfs = bubbles.pdfs_for_bubble(slug)
    if not pdfs:
        return "(no PDFs tagged yet)"
    lines: list[str] = []
    current_score: int | None = None
    for meta in pdfs:
        score = int(meta.get("bubble_score", 5))
        if score != current_score:
            if lines:
                lines.append("")
            lines.append(f"## Relevance {score}")
            current_score = score
        pdf_id = meta.get("pdf_id", "")
        title = meta.get("title") or meta.get("filename") or pdf_id
        lines.append("")
        lines.append(f"### [Relevance {score}] {title}")
        lines.append(f"- Asset id: `{pdf_id}`")
        if include_paths and pdf_id:
            adir = paths.asset_dir(pdf_id)
            lines.append(f"- Allowed summary path: `{adir / 'summary.md'}`")
            lines.append(f"- Allowed text path: `{adir / 'text.txt'}`")
            lines.append(f"- Allowed PDF path: `{adir / 'paper.pdf'}`")
        bib_keys = assets.bibtex_keys(meta.get("bibliography", ""))
        if bib_keys:
            lines.append(f"- BibTeX keys: {', '.join(bib_keys)}")
        summary = assets.get_summary(pdf_id) if pdf_id else ""
        if summary_budget is not None:
            summary = _clip(summary, summary_budget, "paper summary")
        lines.append(summary or "(no summary)")
    return "\n".join(lines)


def scientist_context(slug: str) -> str:
    """Generated context artifact for a scientist session scoped to one bubble."""
    name = bubbles.slug_to_name(slug)
    pages = bubbles.list_pages(slug)
    papers = paths.bubble_dir(slug) / "_lockedin_papers.md"
    lines = [
        f"# lockedin Scientist Context: {name}",
        "",
        f"- Bubble slug: `{slug}`",
        f"- Bubble report dir: `{paths.bubble_dir(slug)}`",
        f"- Attached-paper inventory: `{papers}`",
        "",
        "Use only this bubble's report pages and the attached papers listed below. Higher",
        "relevance scores should be prioritized for reading, retrieval, comparison, and citation.",
        "",
        "## Report Pages",
    ]
    if pages:
        for p in pages:
            page_path = paths.bubble_page_path(slug, p["page_slug"])
            hidden = " hidden" if p.get("hidden") else ""
            lines.append(f"- {p['title']} (`{p['page_slug']}`{hidden}): `{page_path}`")
    else:
        lines.append("- (no report pages yet)")
    lines.extend(["", "## Attached Papers", "",
                  bubble_paper_context(slug, include_paths=True, summary_budget=500)])
    return "\n".join(lines)


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

        summaries = bubble_paper_context(slug)

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
