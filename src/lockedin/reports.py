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

On phones the sidebar opens from its own edge: pull the handle on the left of the screen, or tap it. Tap the dimmed page beside it, or fling it left, to close it again.

## Theme modes

Use the theme button in the top bar to cycle the colour scheme. Open
**Settings → Aesthetics** to choose which of **Dark, Light, Pink, Techno** and **Pearl** appear
in that cycle; each row shows the palette it switches to. Your selection is remembered for your workspace. Owner
previews and public shared pages deliberately offer only **Dark** and **Light**. The landing page
is always Dark.

## Your account

Click the active workspace name in the top-right menu to see whether your account is **standard**
or **premium**, request premium access, log out, or open **Manage workspaces**.

Go to **Settings → Account** to change your username or password. You must supply your
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

Set it up in **Settings**: pick a provider card (Qwen on the server for premium accounts, or
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

Open **Workspace** in the sidebar to manage the active workspace.

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

Rename a bubble from its detail view via **Edit titles** in the **⋮** menu (the same mode that
renames its pages).

Each bubble card carries two icon buttons: **archive** hides it from the Bubbles list without
touching its papers or pages, and **delete** removes the bubble **and all its wiki pages**.
Archive is the reversible one. The archive button in the view header lists the archived bubbles,
where the same button on a card restores it.

## The bubble home

Opening a bubble shows its **home**: the idea (one paragraph and a goal, kept short on purpose),
the document as a row of page cards, and the **chalk talks**. Click a page card to open it; the
**‹** beside the bubble name brings you back from anywhere.

The idea is written by the agent and stamped with when it last revised it — it is the agent's
own statement of what the work is for. If it is subtly wrong, that is the cheapest and
highest-value thing you can correct, so **edit goals** is right beside it. Both fields take
Markdown and LaTeX.

## Page tabs

Each bubble has a mini-wiki. The tabs at the top of the editor list all pages.

- **+** at the left of the tab row — create a new page
- **the editor switch** and **the marks switch** at the right — open the Markdown editor and
  your marks. With both closed you get the rendered page and nothing else. The button between
  them enters a focused workspace
- Click a tab to switch pages (the current page auto-saves first)
- Drag a tab to reorder pages; page order controls wiki navigation and bubble-wide numbering
- the **close** button on a tab deletes that page (the overview page cannot be deleted); it
  shows on the tab you are on and on hover, and stays visible on touch
- Pick **Edit titles** in the **⋮** menu (the last segment of the presence cluster in the
  bubble's title row) to rename pages

The eye icon beside **Insert link** in the editor toolbar hides or shows the current page in
read-only previews and public shares. Hidden pages are omitted from the tab bar by default.
Use the **show hidden pages** control in the editor toolbar to reveal them; they appear in a
separate right-aligned group until you hide them again.

## Files on a bubble

**Assets** in the **⋮** menu is the bubble's own file drawer — figures a page links to, data, a
slide export, anything the work needs beside the prose. Drop files in, download them back, or
delete them. A page refers to one by a path under `assets/` — the usual Markdown image line,
pointing at the file name — and it resolves wherever the page is read, including on a public
share.

### Large files

Nothing here asks you to think about size until it matters, but it is worth knowing what happens.

A file over **16 MB** is not sent as one request. The browser slices it into **32 MB** pieces and
sends them one at a time, because a proxy in front of the server caps a single request body
(Cloudflare stops at 100 MB) while the server itself does not. You see one progress ring either
way; the slicing is not something you drive.

**An interrupted upload resumes.** The pieces already accepted stay on the server, so if the
connection drops at 80% you pick the same file again and it carries on from 80% rather than
starting over — the upload dialog says so when it fails. A dropped slice is retried on its own a
few times first, with a widening pause, since one bad slice over a tunnel is the ordinary
failure and re-sending it is cheap. Cancelling is the one thing that throws the staged bytes
away; anything simply abandoned is swept a day later.

Text assets preview in the browser up to **1 MB**. Past that the viewer says so and offers the
download instead, rather than pulling a large file into the page to render it.

If a bubble is synced to a folder by LockedIn Scientist, files over **25 MB** are listed but never
carried by the sync in either direction — see **Scientist CLI** for moving those deliberately.

## Quick-add a paper

Open **Papers** in the **⋮** menu to see the bubble's attached papers and a **Quick add
paper** form. Paste a PDF URL, provide a title, and optionally paste BibTeX. Quick add fetches
the PDF as a new asset, attaches it directly to the current bubble at relevance 5, and saves the
BibTeX when valid. You can edit or add BibTeX later from the asset detail page.

Renaming a page updates its display everywhere — existing links that used the old title
are rewritten automatically.

## Sharing

Open the **⋮** menu in the bubble's title row and click **Public link** to publish an unlisted,
read-only link. While sharing is active the row reads **On** and an **↗ Open shared page** entry
appears beneath it. Anyone with the link can browse visible pages (no login needed). Toggle off to revoke
immediately; toggle back on to restore the same URL.

Hovering a heading on the shared page reveals a link anchor for deep-linking to a section.
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

Each slide carries its kind (setup, derivation, evidence, comparison, implementation, ask) and
its date. A slide is a living surface: the agent edits it in place, and the current text is the
whole record — there is no version history to dig through, on purpose.

## The five marks

Select any text on a slide and pick one:

| mark | means | what the agent does |
|---|---|---|
| **wrong** | this is wrong | re-derives, rather than rewording |
| **unclear** | I don't follow | re-explains, rather than re-deriving |
| **deeper** | go deeper | expands, usually into a report page |
| **keep** | good, keep this | leans on it rather than cutting it |
| **cut** | cut this | removes it |

Each is drawn in the picker with its own mark and colour, and named underneath.

**A mark alone is a complete comment.** Tapping **wrong** on a sentence says everything it needs to;
the text box is optional. That matters on a phone, and it gives the agent a far stronger signal
than prose it has to infer intent from.

## Marking a region

Some marks are about *where things are*, not what they say. Hit **mark region**, drag a box
over any part of the slide, and pick a mark. A region mark stores a picture of the slide as you
saw it, with your box drawn on — which is the only thing that carries layout and placement to an
agent that cannot see your screen.

Region marks exist only on slides. On a report page, which reflows, a rectangle would say
nothing a quoted sentence does not.

## Drawing on a slide

Some feedback is faster to draw than to say. Hit **draw** and the slide becomes a canvas:
cross a line out, arrow a paragraph to where it belongs, circle the weak step, write in the
margin. **undo** removes the last stroke; **done** pins it, with an optional sentence.

It lands as a single **drawn** mark. The agent gets a picture of the slide with your strokes
on it and the instruction that the drawing *is* the feedback — crossed-out text wants rewriting,
arrows want things moved, circles want attention. Like region marks, drawing exists only on
chalk talks.

## Writing and editing by hand

A talk is not agent-only. **+ add chalk talk** offers two doors: **Ask an agent** hands you a
prompt to paste into the agent you already run, and **Write it yourself** opens a blank slide in
the editor.

On any open talk, **edit** switches the current slide to the same Markdown editor the document
uses. The first line is the `# title`, an optional `*subtitle*` line follows, then the body.
Every open mark appears as a `<comment-begin=…> … <comment-end=…>` pair — the same syntax
report pages use — and the mark follows the text between the tags when you rewrite it. Deleting
the tags leaves the mark to orphan loudly rather than silently vanishing.

The toolbar is the document editor's — same image upload, tables, text colour, centering and
undo — and the same coloured chip on its left shows the save state: a tick when saved, a pencil
while unsaved; click it to save in place, or **Save slide** to save and return to the deck.

A hand edit lands in place — the slide simply becomes what you saved. **+** inserts a blank
slide after the current one; the **delete** button removes the slide with its marks; the delete
button on a talk card on the bubble page removes the whole talk.

## Marks are conversations

Every mark is a thread. **reply** adds a turn; the agent answers in the same thread. You can
**edit** your own last turn — but only that, because rewriting something the other side has
already answered would leave that answer replying to words that no longer exist.

**remove** withdraws a mark entirely.

## What happens to a mark

A mark is working state, and working state should end — but ending it is yours alone. The
agent answers by editing the slide in place and replying in the thread; it cannot remove a mark,
anywhere. When the answer satisfies you, hit **remove** and the mark and its picture are deleted
— completely. Nothing accumulates a list of "resolved" items nobody reads.

If the agent thinks a mark is mistaken, it is told to say so and argue rather than comply.

## Asking for one

Press **+ add chalk talk** beside **Chalk talks** and pick **Ask an agent**. Say what you want
explained, add any steer ("five slides at most", "assume I know the ELBO"), and copy the message
into the session working on this bubble. The agent creates the talk by writing one file; it
appears here on its next sync.

You do not need to describe the format or the rules — the agent has already read them.

## Marks on report pages

Report pages take the same five marks. Select text in the **rendered** page (not the Markdown)
and pick one; the highlight appears where you read. Open the marks column with the marks
switch in the tab row.

The one difference is the anchor. A page is hand-edited constantly, so a page mark wraps the
text it points at — you will see `<comment-begin=…> … <comment-end=…>` in the Markdown — and
moves with it when you edit around it. A slide mark remembers the quoted text instead, and says
so plainly if that text later disappears.
""",
    },
    {
        "title": "TODOs",
        "content": """\
## Overview

TODOs are the active workspace's task list (like GitHub issues). Each item has a numeric **id**,
a **title**, an optional Markdown **note**, and a **done** flag.

## Managing TODOs

Open the **TODOs** view in the sidebar. It is a list: one row per item, carrying its `@id`, its
title, and how many report pages reference it.

- **+ New TODO** — opens a title form and creates a new item
- Click a row to open it and edit its note
- **Show done** / **Show open** at the top switches which list you are looking at
- **Mark done** on an open item closes it; **Reopen** brings it back
- **Delete TODO** removes it — only allowed when no report page references it
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

Use **Library** to add a PDF or a paper URL. You can optionally set:
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
title. Asset cards with saved BibTeX show a small **BibTeX** badge.

## Large PDFs

A paper goes up in a single request, and a PDF over **200 MB** is refused with a message saying
so. That is a different path from a bubble's own files, which slice and resume — a paper is
metadata plus text extraction, and one that large is almost always the wrong file.

Text extraction and the summary run in the background after the upload returns, so a long paper
lands in the list straight away and fills in its extracted title and summary shortly after.

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

When editing or uploading an asset, use the **Add to bubble…** picker to attach it
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

Open the bubble and click the **agent** button in the presence cluster (or **Connect an agent**
in the **⋮** menu). Pick your operating system, copy the single line it shows, and paste it into a terminal on
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

`workspaces switch` is a **device-global** setting shared by every project on the computer. The agent
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
An attached review maps to the Markdown between its `<comment-begin=…>` and `<comment-end=…>`
tags. The tags are server-owned: an agent may make the smallest requested edit between them, but
must not create, copy, rename, move, or reinsert a tag. It should read the entire thread, question
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
Place a descriptively named file in `.lockedin/reports/assets/`, then embed it on a page with an
ordinary Markdown image: the alt text becomes the figure's caption and carries its numbering, and
the path is relative to the page — `assets/my-figure.gif`. Captions may contain math.

Keep report artwork out of `.lockedin/assets/`, which is reserved for the paper library. Preview and shared
bubble pages restart GIFs from their first frame whenever they render.

### Large files

Assets over 25 MB are listed but never carried by the sync, in either direction. The manifest is
re-read on every poll, so hashing a multi-gigabyte archive would cost more than the whole rest of
the bubble — and a zip, a dataset, or a checkpoint is not something an agent reads anyway. They
move only when you ask:

```
lockedin-scientist assets
lockedin-scientist assets pull <filename>
lockedin-scientist assets push <filename>
lockedin-scientist assets rm <filename>
```

`assets` lists every large file on both sides and says which way each one needs to move: `pull`
brings one down into `.lockedin/reports/assets/`, `push` sends a local one up, replacing the
server's copy of that name. Both accept `--all` instead of a filename. Both slice the transfer so
any size fits through the proxy in front of the server, and both resume where they stopped if the
transfer is interrupted, so a failed 4 GB upload does not start over.

`rm` deletes one from the bubble. Deleting a large file locally does not remove it from the
server — the rule that keeps it from syncing down keeps the deletion from syncing up — so this is
the only way to reclaim that space from a terminal, the Assets panel in the app being the other.
It asks before it acts, refuses off a terminal without `--yes`, and will not remove a file a page
still references unless you add `--force`.

Because these files are not on disk in a synchronized project, nothing there would otherwise show
they exist. A generated `reports/assets/NOT-SYNCED.md` lists them with their sizes, so an agent
listing that folder sees what is in the bubble rather than concluding the files were lost.

Everything under the threshold keeps syncing automatically, exactly as before. Operators can move
the line with `LOCKEDIN_SYNC_MAX_ASSET_BYTES` in the server environment.

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

There are no named view modes. The right of the page-tab row carries two switches and a focus
button, and every layout is a combination of the two:

- **the editor switch** — opens the Markdown editor beside the rendered page
- **the marks switch** — opens your marks column
- **the focus button** between them — a focused workspace with the navigation hidden; press it
  again to leave

With both switches closed you get the rendered page and nothing else, which is the default.

**Save**: click the leftmost sync chip in the editor toolbar, or press **Ctrl/⌘+S**. The chip
shows the state — a tick when saved, a pencil while there are unsaved edits, a clock while
saving, and a cross when a disk conflict needs attention.

From left to right, the editor toolbar shows: the **sync** chip, insert image, insert table,
text colour, **center selected text**, insert link, the **page visibility** eye, **show hidden
pages**, **undo** and **redo**. The eye hides or shows the current page in read-only previews
and public shares; hidden page tabs stay out of the way until you use show-hidden-pages, then
appear right-aligned in the tab bar.

Everything else about the bubble — Edit titles, Papers, Assets, the Overleaf link, and
preview/sharing — lives in the **⋮** menu, which sits at the end of the presence cluster in the
bubble's title row rather than in the tab row.

### On mobile

The same controls, sized for a phone: the page tabs, the editor toolbar, **+**, and the **⋮**
menu in the title row. The two pane switches work the same way — open the editor when you want
to type, close it when you want to read.

---

## Marks on a page

Marks are private to signed-in members of the active workspace. They are never included in
read-only previews or unlisted shared links.

Report pages take the same five marks a chalk talk does — see **Chalk talks** for what each one
asks the agent to do. Select text in the **rendered** page, not in the Markdown, and the picker
opens where you read. A mark alone is a complete comment; the sentence is optional. Open the
marks column with the marks switch in the tab row.

A page mark wraps the text it points at, so you will see a `<comment-begin=…> … <comment-end=…>`
pair in the Markdown. LockedIn writes and removes those tags itself as part of one save; do not
type, copy or rename them by hand. They are stripped from rendered previews, public shares and
KaTeX, so they are source-only bookkeeping. A tag left unclosed is reported as a source error
with its line and column, and blocks the save until it is repaired. Marked ranges may sit beside
one another and may nest, but a selection whose edge would land inside a tag is refused.

Because the wrapper surrounds the text, a mark moves with its sentence as you edit around it. A
selection that runs through typeset math cannot be anchored back to the source, so the picker
stays shut rather than offering a mark it could not place.

Every mark is a thread: **reply** adds a turn, and you can **edit** your own last turn. **remove**
withdraws the mark entirely — and only you can, the agent cannot. Nothing accumulates a list of
resolved items. Deleting a report page removes that page's marks with it.

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
| `theorem` | Yes — own counter |
| `lemma` | Yes — own counter |
| `corollary` | Yes — own counter |
| `definition` | Yes — own counter |
| `proposition` | Yes — own counter |
| `assumption` | Yes — own counter |
| `remark` | Yes — own counter |
| `proof` | No — ends with ∎ |

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

## Large files

Files over 25 MB in `.lockedin/reports/assets/` are **not** carried by the ordinary sync, in
either direction. The manifest is re-read on every poll, so hashing a multi-gigabyte archive
would cost more than the whole rest of the bubble. Move one deliberately instead:

```
lockedin-scientist assets
lockedin-scientist assets pull <filename>
lockedin-scientist assets push <filename>
lockedin-scientist assets rm <filename>
```

`assets` lists every large file on both sides and says which way each one needs to move. If you
generate a big artifact — a dataset, an archive, a model checkpoint — writing it into
`.lockedin/reports/assets/` is not enough on its own: run `lockedin-scientist assets push` with
its filename (or `--all`) to send it, and say so in your report to the user. Likewise, a large
file the listing shows as `on server` is not on disk until you `pull` it. Both directions slice
the transfer and resume after an interruption, so a large file never has to start over.

Deleting a large file locally does **not** remove it from the bubble — the same rule that stops
it syncing down stops the deletion syncing up. Use `lockedin-scientist assets rm` with its
filename (or `--all`) for that; it is irreversible, so it asks first, and off a terminal it
refuses without `--yes`. It will not delete a file a page still references unless you add
`--force`, because that breaks the page rather than reclaiming space.

`reports/assets/NOT-SYNCED.md` is generated and lists every large file in the bubble with its
size, so listing that directory shows what is there but not on disk. Do not edit or delete it —
it is rewritten on every sync and disappears by itself once no large files remain.

Figures you actually embed in a page are far below this limit and sync normally; this applies
only to genuinely large binaries.

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

Define your own shorthand commands in **Settings → Math Macros**. For example,
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

