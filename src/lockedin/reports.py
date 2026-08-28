"""The in-app usage guide.

There is no AI chat in the product any more — the model layer exists solely to summarize
uploaded PDFs (see ``tagger``). This module is the single source of truth for the user-facing
usage guide: ``APP_USAGE_GUIDE_SECTIONS`` renders in the web app's help modal, and
``guide_section`` feeds the Scientist skill and the ``lockedin editguide`` CLI.
"""
from __future__ import annotations

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

## Background model for paper summaries

Separate from the agent you actually work with, a background model can summarize papers for
you: when you upload a PDF it extracts metadata, suggests tags, and writes the one-time summary
shown on the asset page. That is its whole job — there is no AI chat in the app.

Set it up in **⚙️ Settings**: pick a provider card (Qwen on the server for premium accounts, or
OpenAI / Claude / Gemini with your own API key) and click **Configure** to add the key or
endpoint. The coloured dot shows the provider's last health-check.
""",
    },
    {
        "title": "Workspaces",
        "content": """\
## Your active workspace

Every account has a private **Personal** workspace. It opens automatically when you sign in and
is the safe home for your individual research. You can also create or join shared workspaces.
The active workspace controls everything research-related: Library papers, Bubbles, report pages
and figures, TODOs, citations, and math macros. The top-right workspace switcher
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
lifecycle. Switching workspaces immediately changes the Library, TODOs, and Bubbles you
see; unsaved report edits must be saved or discarded first.

## Shared math macros

**Math Macros** lives inside the active workspace settings. A macro belongs to the workspace,
not an individual account, so every member, preview, public share, and Scientist client uses the
same definitions. Add or edit a macro there before using it in report math.
""",
    },
    {
        "title": "Bubbles",
        "content": """\
## What is a bubble?

A **bubble** is a topic group. Each bubble has a name, a multi-page Markdown wiki,
and attached papers. Bubbles created in the app are
available immediately.

## Creating

Click **+ New bubble** in the Bubbles view, or tag any paper with a new topic name.
New bubbles are created explicitly from the Bubbles view.

## Renaming & deleting

Rename a bubble from its detail view via **🏷️ Edit titles** in the **⋮** menu (the same mode
that renames its pages).

**Archive** on a bubble card hides it from the Bubbles list without touching its papers or pages;
**Show archive** lists the archived ones, where **Restore** brings one back. The 🗑 icon deletes a
bubble **and all its wiki pages** — archive is the reversible option.

## The bubble home

Opening a bubble shows its **home**: the idea (one paragraph and a goal, kept short on purpose),
the document as a row of page cards, and the **chalk talks**. Click a page card to open it; the
**‹** beside the bubble name brings you back from anywhere.

The idea is written by the agent and stamped with when it last revised it — it is the agent's
own statement of what the work is for. If it is subtly wrong, that is the cheapest and
highest-value thing you can correct, so **✎ correct this** is right beside it. Both fields take
Markdown and LaTeX.

## Page tabs

Each bubble has a mini-wiki. The tabs at the top of the editor list all pages.

- **+** at the left of the tab row — create a new page
- **◧** and **◨** at the right — open the Markdown editor and your marks. With both closed you
  get the rendered page and nothing else
- Click a tab to switch pages (the current page auto-saves first)
- Drag a tab to reorder pages; page order controls wiki navigation and bubble-wide numbering
- **✕** on a tab deletes that page (the overview page cannot be deleted)
- Pick **🏷️ Edit titles** in the **⋮** menu (beside the presence pill) to rename pages

The eye icon beside **Insert link** in the editor toolbar hides or shows the current page in
read-only previews and public shares. Hidden pages are omitted from the tab bar by default.
Use the **☷ Show hidden pages** control beside the sync icon to reveal them; they appear in a
separate right-aligned group until you hide them again.

## Quick-add a paper

Open **📚 Papers** in the **⋮** menu to see the bubble's attached papers and a **Quick add
paper** form. Paste a PDF URL, provide a title, and optionally paste BibTeX. Quick add fetches
the PDF as a new asset, attaches it directly to the current bubble at relevance 5, and saves the
BibTeX when valid. You can edit or add BibTeX later from the asset detail page.

Renaming a page updates its display everywhere — existing links that used the old title
are rewritten automatically.

## Sharing

Open the **⋮** menu in the page toolbar and click **Public link** to publish an unlisted,
read-only link. While sharing is active the row reads **On** and an **↗ Open shared page** entry
appears beneath it. Anyone with the link can browse visible pages (no login needed). Toggle off to revoke
immediately; toggle back on to restore the same URL.

Hovering a heading on the shared page reveals a 🔗 anchor for deep-linking to a section.
Shared pages have a theme-cycle button, restricted to the themes you enabled in **Aesthetics**.
""",
    },
    {
        "title": "Chalk talks",
        "content": """\
## What a chalk talk is

The document is where an idea **lands**. A **chalk talk** is where it gets argued first.

It is a dated deck of slides an agent writes when it has reached something whose correctness
needs your judgement — a derivation it cannot justify, a design choice with no obvious winner,
a result it does not trust. You read it and **mark it up**, rather than replying to it.

Open a bubble to see its talks listed under **Chalk talks**, newest first, each showing its
slide count and how many of your marks are still open.

## Reading one

Click a talk to open it. **←** and **→** or the dot strip move between slides; a yellow dot is a
slide you have marked. **all slides** shows the whole deck as a contact sheet, so you can see at
a glance where your ink is; click any card to jump there.

Each slide carries its kind (setup, derivation, evidence, comparison, implementation, ask), its
date, and its version. A slide revised after your marks shows **◷** — click it for the history,
which records each version alongside the mark that caused it.

## The five marks

Select any text on a slide and pick one:

| mark | means | what the agent does |
|---|---|---|
| ✗ | this is wrong | re-derives, rather than rewording |
| ? | I don't follow | re-explains, rather than re-deriving |
| → | go deeper | expands, usually into a report page |
| ✓ | good, keep this | leans on it rather than cutting it |
| ✂ | cut this | removes it |

**A mark alone is a complete comment.** Tapping ✗ on a sentence says everything it needs to;
the text box is optional. That matters on a phone, and it gives the agent a far stronger signal
than prose it has to infer intent from.

## Marking a region

Some marks are about *where things are*, not what they say. Hit **⬚ mark region**, drag a box
over any part of the slide, and pick a mark. A region mark stores a picture of the slide as you
saw it, with your box drawn on — which is the only thing that carries layout and placement to an
agent that cannot see your screen.

Region marks exist only on slides. On a report page, which reflows, a rectangle would say
nothing a quoted sentence does not.

## Writing and editing by hand

A talk is not agent-only. **+ add chalk talk** offers two doors: **Ask an agent** hands you a
prompt to paste into the agent you already run, and **Write it yourself** opens a blank slide in
the editor.

On any open talk, **✎ edit** switches the current slide to the same Markdown editor the document
uses. The first line is the `# title`, an optional `*subtitle*` line follows, then the body.
Every open mark appears as a `\comment{id}{…}` wrapper — the same syntax report pages use — and
the mark follows the text inside the braces when you rewrite it. Deleting a wrapper leaves the
mark to orphan loudly rather than silently vanishing.

A hand edit is a revision like any other: it bumps the slide's version, and the one-line **why**
you optionally give is kept in the history. **+ add slide** inserts a blank slide after the
current one; **✂ delete slide** removes the slide with its marks and history; the ✕ on a talk
card on the bubble page deletes the whole talk.

## Marks are conversations

Every mark is a thread. **reply** adds a turn; the agent answers in the same thread. You can
**edit** your own last turn — but only that, because rewriting something the other side has
already answered would leave that answer replying to words that no longer exist.

**remove** withdraws a mark entirely.

## What happens to a mark

A mark is working state, and working state should end. When the agent has genuinely addressed
one, it revises the slide and names the mark; the mark and its picture are deleted, and what it
asked for is kept in the slide's version history instead. Nothing accumulates a list of
"resolved" items nobody reads.

If the agent thinks a mark is mistaken, it is told to say so and argue rather than comply.

## Asking for one

Press **+ add chalk talk** beside **Chalk talks** and pick **Ask an agent**. Say what you want
explained, add any steer ("five slides at most", "assume I know the ELBO"), and copy the message
into the session working on this bubble. The agent creates the talk by writing one file; it
appears here on its next sync.

You do not need to describe the format or the rules — the agent has already read them.

## Marks on report pages

Report pages take the same five marks. Select text in the **rendered** page (not the Markdown)
and pick one; the highlight appears where you read. Open the marks column with **◨** in the tab
row.

The one difference is the anchor. A page is hand-edited constantly, so a page mark wraps the
text it points at — you will see `\\comment{id}{…}` in the Markdown — and moves with it when you
edit around it. A slide mark remembers the quoted text instead, and says so plainly if that text
later disappears.
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

- **`help`** — show this list in Slack; anything the bot does not recognize shows it too
- **`workspaces`** or **`switch workspace`** — list and select the active workspace
- **`select`** or **`switch`** — choose the active bubble
- **`list`** — show your bubbles
- **Attach a PDF** — uploads it to your Library queue
- **Send a PDF link** — fetches and uploads the paper when the link resolves to a PDF
- **`todos`** — list, add, edit, complete, or remove open TODOs

The Slackbot has an active workspace before it has an active bubble. Switching workspaces clears
the active bubble and TODO flow, so uploads, TODOs, and bubble selection always stay inside the
workspace you chose. It follows the same account, premium, and workspace access rules as the
website. The bot has no chat — it manages assets and TODOs only.
""",
    },
    {
        "title": "Scientist CLI",
        "content": """\
## Work on a bubble from your terminal

`lockedin-scientist` synchronizes one approved bubble into `.lockedin/` in the project where you
run it. It does not launch Codex, Claude Code, or Antigravity: start your preferred agent normally
after installing its native `lockedin-scientist` bootstrap skill.

### The one-line way

Open the bubble and click **🤖** beside the presence chip (or **Connect an agent** in the **⋮**
menu). Pick your operating system, copy the single line it shows, and paste it into a terminal on
the machine where you write. It installs the client, signs that terminal in with no browser step,
asks which folder to use, binds the bubble to it, and installs the skill for whichever of Codex,
Claude Code, and Antigravity are on that machine — then tells you what to run.

That link is **single-use and expires in ten minutes**, because for its short life anyone who runs
it is signed in as you. Click the robot again for a fresh one; nothing is stored server-side, so a
server restart invalidates any outstanding link.

**You can give the line to an agent instead of a terminal.** If Claude Code, Codex, or
Antigravity is already open in the folder, paste it into that conversation and let it run. With
no terminal to answer from it connects the directory the agent is working in rather than asking,
so nothing needs typing — which is also what makes it work on a cloud sandbox where the client
was never installed.

That makes it the quickest repair when a folder's sync has stopped: paste a fresh line and it
puts the folder back, installing the client first if it is missing. Where the client is already
installed, `lockedin-scientist resync` does the same in one word.

Running it again is always safe: a folder already connected to this bubble is resumed, never
rebuilt. The rest of this section is the same setup done by hand.

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

`workspaces switch` is a **device-global** setting shared by every project on the computer. The 🤖
link avoids it entirely — it pins the bubble's workspace into that one project's binding, so
connecting one project never retargets the others.

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
Credentials, sessions, TODOs, bubble settings, PDFs, and paper summaries are never
writable by Scientist.

### Review feedback and health

Open private report reviews are available read-only at `.lockedin/config/reviews.yaml`. Each entry
includes its page, current `anchor_state`, selected or last-known text, and complete conversation.
An attached review maps to the exact Markdown body in `\\comment{<comment-id>}{...}`. The wrapper is
server-owned: an agent may make the smallest requested edit inside its existing body, but must not
create, copy, rename, nest, move, or reinsert a wrapper. It should read the entire thread, question
vague or unsupported feedback, and avoid unrelated rewrites. Unanchored reviews must never be
guessed back into the report; replying, resolving, and deleting remain website actions.

Before relying on a report submission, run:

```
lockedin-scientist doctor
```

It verifies that this project has a matching healthy worker and that its bound server/bubble can
be reached. If it fails, do not claim that local report work synchronized; follow the recovery
guidance printed by the command.

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
![Description of the figure](assets/my-figure.gif)
```

Keep report artwork out of `.lockedin/assets/`, which is reserved for the paper library. Preview and shared
bubble pages restart GIFs from their first frame whenever they render.

### Manage workers and recover

```
lockedin-scientist ps
lockedin-scientist resync
lockedin-scientist stop <worker-id>
lockedin-scientist hard-reset <bubble-slug>
lockedin-scientist overleaf help
```

`resync` is the ordinary repair when a project's worker has stopped: run it from the project and it
resumes whatever bubble that directory is already bound to. It takes no arguments and needs no
workspace switch — `.lockedin/` records its own server, workspace, and bubble — and it leaves the
directory, its unsynchronized work, and its identity on the bubble page untouched. A healthy worker
is reported rather than restarted.

`stop` leaves `.lockedin/` intact. `hard-reset` is the heavier repair, for a directory that is
itself broken: it stops the project worker, replaces the directory with the current server bubble,
and starts a new worker. It preserves a connected Overleaf checkout unless you explicitly add
`--discard-overleaf`.

### Overleaf: an explicit publication workflow

Each bubble can optionally link one Overleaf Cloud project from the **⋮** menu in its page toolbar.
The link is workspace
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

Open any bubble and pick a view from the **⋮** menu in the page toolbar:
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

The page toolbar itself stays short: **+** at the far left, the page tabs, then **⋮** and
**⛶**. Everything else for the bubble — the view modes, Edit titles, 📚 Papers, 📁 Assets, the
Overleaf link, and preview/sharing — lives in **⋮**. Use **⛶** to enter a focused workspace that
hides the navigation; click it again to exit.

### On mobile

The bubble page is the same one control surface, sized for a phone: page tabs, the editor
toolbar, **+**, and **⋮**. Everything above is reachable from that menu, including switching
between **👁 Read** and **✏️ Edit**, and **👁 Preview page** for the read-only view.

---

## Review comments

Review comments are private to signed-in members of the active workspace. They are never
included in read-only previews or unlisted shared links.

On a desktop, switch to **✏️ Edit** or **◧ Split**, then use **💬 Toggle review** in the editor
toolbar. In Edit mode, Review appears beside the source editor; in Split mode it sits between
the editor and preview. Read, focused, and mobile views stay free of review UI.

To start a review, select source text and click the coloured **+** in the Review header. The
selected text receives a subtle yellow highlight in the editor. The source is wrapped in
`\\comment{<comment-id>}{...}` markers. LockedIn creates and removes those markers together with
the review thread in one save; do not type, copy, nest, or rename them yourself. Click the
highlighted body text to open its thread. Threads are collapsed by default; opening one collapses
the others. Markers are removed from rendered previews and KaTeX, so they are source-only
bookkeeping. A missing closing brace is shown as a source error and must be repaired before the
page can be saved. Commented ranges may sit next to one another but cannot overlap or contain one
another.

The Review header separates **Open** and **Resolved** threads. Workspace members can reply,
resolve/reopen, or delete a thread. You can double-click only your own comment or reply to edit
it in place; save with **Ctrl/⌘+Enter** or **Save**. Resolving or deleting a thread removes its
wrapper while preserving the report text. Reopening a resolved thread leaves it unanchored rather
than guessing where it belongs. If later edits remove the wrapper or selected text, the open thread
remains in its original review-list position with an **Unanchored** badge, but no text is
highlighted and LockedIn never recreates the wrapper. Deleting a report page also removes all of
that page's reviews.

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



def guide_section(title: str) -> str:
    """Return the markdown body of the usage-guide section with the given title (matched by
    title, not index), or "" if there is none. Single source of truth for the website and the
    project-local Scientist skill."""
    return next((s["content"] for s in APP_USAGE_GUIDE_SECTIONS if s["title"] == title), "")

