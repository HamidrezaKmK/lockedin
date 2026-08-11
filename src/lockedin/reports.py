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
and **⚪ Pearl** appear in that cycle. Your selection is remembered for your workspace. Owner
previews and public shared pages deliberately offer only **Dark** and **Light**. The landing page
is always Dark.

## Your account

Click the active workspace name in the top-right menu to see whether your account is **standard**
or **premium**, request premium access, log out, or open **Manage workspaces**.

Go to **⚙️ Settings → Account** to change your username or password. You must supply your
current password to confirm. A username change carries all your data over and keeps you
logged in.

## Admin: managing users

Admins see a **User access** card in Settings. From there you can:
- **Upgrade to premium** for users who should be allowed to use server-side Qwen
- **Remove premium** to move an account back to bring-your-own-key model usage
- **Delete** a user permanently

Users who request premium from the top-right account menu appear with a **requested** badge.
You cannot delete your own account.
""",
    },
    {
        "title": "Workspaces",
        "content": """\
## Your active workspace

Every account has a private **Personal** workspace. It opens automatically when you sign in and
is the safe home for your individual research. You can also create or join shared workspaces.
The active workspace controls everything research-related: Library papers, Bubbles, report pages
and figures, TODOs, chat history, citations, and math macros. The top-right workspace switcher
shows the active workspace name; use it to switch before opening any of those views.

Personal workspaces stay private and cannot be shared, transferred, or deleted independently.
Shared workspaces are separate research containers: adding or removing a member changes access,
not ownership of the work already contributed.

## Workspace tab

Open **🗂️ Workspace** in the sidebar to manage the active workspace.

- **Create a workspace** creates a new shared workspace with you as its first admin.
- **Active workspace** lists every member and their `admin` or `editor` role.
- Every member can see the roster. Workspace admins can search approved LockedIn accounts, add
  members, promote an editor to admin, demote an admin, or remove a member.
- A workspace must always retain at least one admin. Personal workspaces have one fixed member.

Editors can change research content. Admins additionally manage membership and workspace
lifecycle. Switching workspaces immediately changes the Library, TODOs, Bubbles, and chat you
see; unsaved report edits must be saved or discarded first.

## Shared math macros

**Math Macros** lives inside the active workspace settings. A macro belongs to the workspace,
not an individual account, so every member, preview, public share, and Scientist client uses the
same definitions. Add or edit a macro there before using it in report math.
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

""",
    },
    {
        "title": "Bubbles",
        "content": """\
## What is a bubble?

A **bubble** is a topic group. Each bubble has a name, a multi-page Markdown wiki,
attached papers, and a read-only research chat sidebar. Bubbles created in the app are
available immediately.

## Creating

Click **+ New bubble** in the Bubbles view, or tag any paper with a new topic name.
New bubbles are created explicitly from the Bubbles view.

## Renaming & deleting

Rename a bubble from its detail view via **🏷️ Edit titles** in the view dropdown (the same mode
that renames its pages). The 🗑 icon on a bubble card deletes the bubble **and all its wiki pages**.

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
Anyone with the link can browse visible pages (no login needed). Toggle off to revoke
immediately; toggle back on to restore the same URL.

Hovering a heading on the shared page reveals a 🔗 anchor for deep-linking to a section.
Shared pages have a theme-cycle button, restricted to the themes you enabled in **Aesthetics**.
""",
    },
    {
        "title": "TODOs",
        "content": """\
## Overview

TODOs are the active workspace's task list (like GitHub issues). Each item has a numeric **id**,
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
        "title": "Library",
        "content": """\
## Uploading papers

Use **📚 Library** to add a PDF or a paper URL. You can optionally set:
- **Title** — defaults to the filename
- **Extracted metadata** — the active model records a canonical paper title and author list
  from the PDF during background processing; your chosen asset title is preserved
- **Tags** — comma-separated topic names; each tag links the paper to a bubble
- **Source URL** — the paper's arXiv/DOI link

Leave tags blank to keep the paper unassigned until you organize it into a bubble.

## Editing an asset

Click any asset card to open its detail panel:
- Edit title, tags, notes, source URL
- Open the paper PDF
- **Requires attention** if you want to review or summarize it later
- **Clear attention** once it no longer needs attention

## Background processing & Requires attention

After upload, LockedIn extracts text, records model-derived title and author metadata, and
caches a summary. New papers appear in the Library's **Requires attention** filter until you
clear that flag.

The Library is always the active workspace's full paper inventory. Use its filter menu to switch
between **Requires attention**, **Unassigned**, all papers, or a specific bubble.

## Finding papers

The Library page has filters above the card grid:
- **Search** — matches your title, extracted paper title, authors, filename, PDF id, source URL,
  notes, tags, suggested tags, and BibTeX keys
- **Bubble** — limits results to papers attached to that bubble

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

- **`workspaces`** or **`switch workspace`** — list and select the active workspace
- **`select`** — choose the active bubble for questions
- **`list`** — show your bubbles
- **Ask a question** — the bot answers using your active bubble's reports and paper summaries
- **Attach a PDF** — uploads it to your Library queue
- **Send a PDF link** — fetches and uploads the paper when the link resolves to a PDF
- **`todos`** — list, add, edit, complete, or remove open TODOs

The Slackbot has an active workspace before it has an active bubble. Switching workspaces clears
the active bubble and TODO flow, so uploads, TODOs, questions, and bubble selection always stay
inside the workspace you chose. It follows the same account, premium, and workspace access rules
as the website.
Questions use your configured model. Qwen from Slack also requires premium; otherwise configure
OpenAI, Claude, or Gemini with your own API key in the web settings.
""",
    },
    {
        "title": "Scientist CLI",
        "content": """\
## Work on a bubble from your terminal

`lockedin-scientist` synchronizes one approved bubble into `.lockedin/` in the project where you
run it. It does not launch Codex, Claude Code, or Antigravity: start your preferred agent normally
after installing its native `lockedin-scientist` bootstrap skill.

### Install

On macOS or Linux (Python 3.11+):

```
curl -fsSL https://raw.githubusercontent.com/HamidrezaKmK/lockedin/main/install.sh | bash
```

On Windows PowerShell (Python 3.11+):

```
irm https://raw.githubusercontent.com/HamidrezaKmK/lockedin/main/install.ps1 | iex
```

The installer adds `lockedin-scientist` to your user PATH. On macOS/Linux, ensure
`~/.local/bin` is on PATH if your shell cannot find the command.

Scientist checks its compatible client version whenever it contacts the server. If it asks you to
reinstall, rerun the installer for your platform. Authorization and active workspace persist.

### Sign in and choose work

Authorize a device once; the command opens a browser page on the server you specify:

```
lockedin-scientist login --server https://lockedin.codes
```

List the bubbles currently approved for this account. First list your available workspaces and
select one; the selected workspace is saved across all local projects:

```
lockedin-scientist workspaces
lockedin-scientist workspaces switch <workspace-id-or-name>
lockedin-scientist bubbles
lockedin-scientist sync <bubble-slug>
```

### Native agent skills

Install the `lockedin-scientist` bootstrap once for each agent you use:

```
lockedin-scientist codex setup
lockedin-scientist claude setup
lockedin-scientist agy setup
```

The bootstrap is intentionally short. In a synchronized project it reads the complete current
guide at `.lockedin/SKILL.md`, including that workspace's math macros and the bubble's editing
rules. Invoke `$lockedin-scientist` in Codex, `/lockedin-scientist` in Claude Code, or select it
through agy's `/skills` interface. Setup updates only the managed bootstrap and never overwrites
a user-owned skill with the same name.

`sync` creates `.lockedin/` and a background worker that pulls/pushes every five seconds. Git
ignores this directory through the project-local exclude file, leaving tracked project files alone.

### Sync behavior and scope

The initial sync populates one project-local bubble. Within `.lockedin/`, `assets/<pdf-id>/` and
`config/` are server-authoritative and read-only: they are restored from the server and detached
papers are removed locally. The synchronized report writeback paths are `reports/pages/` and
`reports/assets/`; an optional `.lockedin/overleaf/` checkout is separately editable as the local
publication manuscript. The rest of the repository remains available to your coding agent under
its normal permissions. A concurrent website edit restores the server copy and leaves the rejected
local copy and a patch in `config/conflicts/`.

Only pages and report assets inside the selected **approved** bubble can be written back.
Credentials, sessions, chat history, TODOs, bubble settings, PDFs, and paper summaries are never
writable by Scientist.

### New report pages

To add a page from Scientist, create a flat Markdown file at
`.lockedin/reports/pages/<page-slug>.md`. Scientist registers it in the website automatically;
the page tab title is derived from the filename with hyphens replaced by spaces. Do not edit
`.lockedin/reports/pages.yaml` yourself.

### Deleting pages and figures

Ask the assistant to delete the page's file at `.lockedin/reports/pages/<page-slug>.md`.
Scientist removes the page and its website navigation entry on the next sync; deleting a file in
`.lockedin/reports/assets/` removes that figure the same way. Blanking a page instead of
deleting its file does nothing: an empty sync write is refused so a damaged local copy can never wipe
a report. A bubble's home page cannot be deleted from Scientist; that removal is reported as a
conflict and the file is restored locally. Delete the home page in the browser instead.

### Figures and GIFs

Scientist can add report figures and animated GIFs using the same format as the browser editor.
Place a descriptively named file in `.lockedin/reports/assets/`, then embed it in a page:

```
![Description of the figure](/api/bubbles/<bubble-slug>/assets/my-figure.gif)
```

Keep report artwork out of `.lockedin/assets/`, which is reserved for the paper library. Preview and shared
bubble pages restart GIFs from their first frame whenever they render.

### Manage workers and recover

```
lockedin-scientist ps
lockedin-scientist stop <worker-id>
lockedin-scientist hard-reset <bubble-slug>
lockedin-scientist overleaf help
```

`stop` leaves `.lockedin/` intact. `hard-reset` stops the project worker, replaces the directory
with the current server bubble, and starts a new worker. It preserves a connected Overleaf checkout
unless you explicitly add `--discard-overleaf`.

### Overleaf: an explicit publication workflow

Each bubble can optionally link one Overleaf Cloud project from its header. The link is workspace
metadata managed by LockedIn; Scientist only reads it. After the worker has downloaded the link,
connect the local checkout:

```
lockedin-scientist overleaf help
lockedin-scientist overleaf connect
lockedin-scientist overleaf status
```

This creates `.lockedin/overleaf/`. It is deliberately separate from the report workspace:

- `.lockedin/reports/` is the live, continuously synchronized research record for evolving notes,
  experiments, and report figures.
- `.lockedin/overleaf/` is the curated publication manuscript. You and your normal coding agent
  may edit `.tex`, `.bib`, styles, figures, and other project files there. Review report material
  before transferring it, preserve the manuscript's conventions, and compile when available.
- The background Scientist worker never pulls, pushes, alters, or deletes the Overleaf checkout.
  Do not change `.git/` or its configured remote.

Publish Overleaf changes only when you explicitly ask for it:

```
lockedin-scientist overleaf sync
lockedin-scientist overleaf abort
lockedin-scientist overleaf disconnect
```

`sync` commits local checkout changes, fetches the linked remote's actual default branch,
rebases, and pushes only after a clean result. It never force-pushes, stashes, or resolves a
conflict. On the first Git prompt, enter username `git` and your Overleaf Git token. If no OS
keychain helper is configured, Scientist installs one owner-only credential store for your user,
globally scoped in Git only to `git.overleaf.com`; future LockedIn projects reuse it and the token
never lives in the project directory. If synchronization fails, inspect `git status`, fetch and
rebase the `lockedin-overleaf` remote's branch, resolve and commit the conflict manually, then
push. `overleaf abort` cancels a rebase started by Scientist.

If `ps` shows a **failed** worker because `.lockedin/config/binding.json` is missing, it prints the
exact recovery command. Copy any unsynchronized report work elsewhere first, then run
`lockedin-scientist hard-reset <bubble-slug>` from that project. Scientist does not guess which
bubble a damaged local directory belongs to.
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

From left to right, the editor toolbar shows: the **sync** icon, **💬 Toggle review**, insert image,
insert table, text colour, **≡ center selected text**, insert link, the page visibility eye,
**☷ Show hidden pages**, **↶ undo**, and **↷ redo**. The eye hides or shows the current page
in read-only previews and public shares; hidden page tabs stay out of the way until you use
**☷**, then appear right-aligned in the tab bar.

Use **⛶** in the page controls to enter a focused workspace that hides the navigation and chat;
click it again to exit.

### On mobile

The bubble page is streamlined to the essentials: page tabs, the editor toolbar, and the view
dropdown. Switch between **👁 Read** and **✏️ Edit** right from that dropdown. A small
**↗** link in the top-right corner opens the read-only preview of the current page. Papers and
the research chat live behind the floating **📚** and **💬** buttons. (Sharing is done from a
larger screen.)

---

## Review comments

Review comments are private to signed-in members of the active workspace. They are never
included in read-only previews or unlisted shared links.

On a desktop, switch to **✏️ Edit** or **◧ Split**, then use **💬 Toggle review** in the editor
toolbar. In Edit mode, Review appears beside the source editor; in Split mode it sits between
the editor and preview. Read, focused, and mobile views stay free of review UI.

To start a review, select source text and click the coloured **+** in the Review header. The
selected text receives a subtle yellow highlight in the editor. The source is wrapped in
`\\comment{<comment-id>}{...}` markers; preserve the wrapper and edit only the text inside it
when responding to that review. Click that highlighted text to open
its thread. Threads are collapsed by default; opening one collapses the others. Markers are
removed from rendered previews and KaTeX, so they are source-only bookkeeping.

The Review header separates **Open** and **Resolved** threads. Workspace members can reply,
resolve/reopen, or delete a thread. You can double-click only your own comment or reply to edit
it in place; save with **Ctrl/⌘+Enter** or **Save**. If later edits remove the selected text, the
thread remains in its original review-list position with an **Unanchored** badge, but no text is
highlighted. Deleting a report page also removes all of that page's reviews.

---

## Markdown

Standard CommonMark: headings `#`, bold `**`, italic `*`, lists, blockquotes `>`,
fenced code blocks, tables, images. Nothing unusual here.

### Centred text

Select a paragraph or block and use **≡ center selected text** in the editor toolbar. It writes
portable Markdown-compatible HTML:

```
<div class="centered-text">
Your centred text
</div>
```

The same markup works for Scientist edits, live previews, and shared bubble pages.

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

## Figures

Every Markdown image in a bubble is numbered in page order and receives a caption beneath it.
To add a captioned figure, insert an image with descriptive alt text; that text becomes the
caption in the rendered page. Add a `\\label{fig:your-key}` inside the same alt text when you
want to refer to the figure elsewhere with `\\figref{fig:your-key}`.

Figure numbers and references are bubble-wide, so they remain correct across pages. Type
`\\figref{` in the editor to choose an existing figure label.

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
    title, not index), or "" if there is none. Single source of truth for the website and the
    project-local Scientist skill."""
    return next((s["content"] for s in APP_USAGE_GUIDE_SECTIONS if s["title"] == title), "")

# A SHORT, model-safe summary for the chat system prompt — facts only, no fenced code blocks.
APP_USAGE_BRIEF = (
    "ABOUT THIS APP (use these facts to answer usage questions):\n"
    "- Reports are multi-page Markdown wikis, one per bubble; the editor is on the left, a "
    "live preview on the right. The user writes the reports themselves.\n"
    "- In Settings → Aesthetics, users choose which themes appear in their top-bar cycle. Those "
    "owner previews and public share links offer only Dark and Light; the landing page is "
    "always Dark.\n"
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
