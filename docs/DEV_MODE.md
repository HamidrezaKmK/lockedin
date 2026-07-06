# DEV MODE — Research-report assistant (no server)

Run `./claude_scientist.sh` (for Claude Code), `./agy_scientist.sh` (for Antigravity CLI / agy), or
`./codex_scientist.sh` (for Codex CLI) to launch your report assistant: it manages your reports directly on disk — summarize new
papers, write and edit report pages, reorganize bubbles, answer questions about your
library — the same job as the in-app chat sidebar, but through the CLI.

The plain commands are **not** repurposed — these wrappers inject the report-assistant role
for that one session and authenticate against your `.env` first.

> **The Claude and Antigravity wrappers run shell-free.** `agy_scientist.sh` and `claude_scientist.sh` launch the
> agent as a pure Markdown editor: the shell tool/terminal commands are disabled, file edits
> still **prompt** for your approval, and the launcher authenticates + captures the bubble/page
> listing itself and injects it into the session — so the agent ignores the "run
> `uv run lockedin devmode`" steps below (it can't run shell anyway). Claude does this via a
> `--tools Read Edit Write Glob Grep` allowlist. `agy` (Google's Antigravity CLI) has no such
> flags, so the wrapper uses the **project-local `.agents/` folder** that agy auto-loads:
> `.agents/hooks.json` registers a PreToolUse permission hook (`.agents/agy_permission_hook.sh`)
> that **denies** the shell tool (`run_command`); every write tool (`write_to_file` /
> `replace_file_content` / `multi_replace_file_content`) requires your **approval** before it
> runs. Your **global** agy config (`~/.gemini/antigravity-cli/settings.json`) is never read or
> modified — the gate is entirely project-local. (This also fixes the default "agy edits without
> asking" behavior: a global ask-rule can only name `write_file`, which agy doesn't have, so
> writes slip through; the hook gates the real tool names instead.) All of this is committed to
> the repo, so a fresh clone behaves identically — nothing is stored machine-locally (only your
> `.env` credentials are per-machine). Pick the model with `AGY_MODEL` (see `agy models`); a
> Flash model makes approvals feel much snappier than the default `Pro (High)`.
>
> **On approval prompts and diffs:** because your reports live under the git-ignored `data/`
> dir, agy treats every report file as *outside its workspace* and gates each write with a plain
> **"File access — allow / deny"** prompt — it does **not** render an inline diff for these edits
> (no agy setting tried, incl. `allowNonWorkspaceAccess`/`trustedWorkspaces`, changes this). To
> see what changed, type **`/diff`** in agy after an edit, press **ctrl+o** to expand the `Edit`
> step, or open the page in the lockedin web app. Edits never apply without your approval.
>
> `codex_scientist.sh` follows the same launcher pattern and starts Codex with a read-only
> sandbox, approval prompts, live web search for explicit source-search requests,
> multi-agent disabled, and the same report-assistant role injected into the session. Codex
> usually needs its shell tool for local file reads; set
> `CODEX_SCIENTIST_SHELL=disabled ./codex_scientist.sh` only if your installed Codex build
> exposes separate file-reading tools/resources. The role still limits shell use to read-only
> inspection of authorized Markdown/PDF files, and the read-only sandbox keeps writes behind
> approval prompts.

## Setup (once)

```bash
cp .env.example .env          # then edit: DEV_USERNAME / DEV_PASSWORD = your lockedin account
```

`.env` is git-ignored. Then:

```bash
./claude_scientist.sh                 # Claude Code version
./agy_scientist.sh                 # interactive — authenticates, then greets with your bubbles
./agy_scientist.sh "summarize the new paper into my diffusion report"   # one-shot kickoff
./codex_scientist.sh                  # Codex CLI version
```

The script verifies your `.env` credentials (via `uv run lockedin devmode`) before launching and
exits if they don't match.

## Resume agent sessions

Use each scientist wrapper's `resume` mode instead of calling the raw CLI resume command. The
wrapper re-authenticates, rebuilds the current workspace/editing-guide prompt, and resumes with
the same tool restrictions, policies, sandbox settings, and report-assistant role as a fresh
scientist session.

```bash
./codex_scientist.sh resume            # Codex resume picker
./codex_scientist.sh resume --last     # latest Codex session
./codex_scientist.sh resume <session>  # Codex session id/name, plus any Codex resume flags

./claude_scientist.sh resume           # Claude resume picker
./claude_scientist.sh resume --last    # latest Claude session
./claude_scientist.sh resume latest    # same as --last
./claude_scientist.sh resume <session> # Claude session id or picker search term

./agy_scientist.sh resume           # resume latest Antigravity session
./agy_scientist.sh resume latest    # same as above
./agy_scientist.sh resume <session> # resume session by conversation ID
```

The raw commands, such as `codex resume`, `claude --resume`, or `agy --continue`, can reopen
history, but they do not necessarily carry the scientist launcher's sandbox, allowlist, policy,
or injected report instructions. Prefer the wrapper commands above for report work.

---

## Your role (read this if you are the agent)

When the user asks you to work on **their reports** (not the lockedin codebase), you are their
research-report assistant for the account in `.env`. Do this:

1. **Authenticate & locate.** Run `uv run lockedin devmode`. It loads `.env`, checks the
   password, and prints the workspace path + the user's **approved** bubbles and page counts
   (suggested-but-unapproved bubbles have no report pages, so they're omitted). **If it fails,
   stop** and tell the user — do not read or edit anything under `data/users/`.

2. **Work only inside** `data/users/<DEV_USERNAME>/`. Never touch other users' folders or
   `data/users/accounts.yaml`.

3. **The Markdown is the report.** Edit the `.md` files directly — that's the deliverable.

### What you can do for the user

- **Summarize a new paper into a report** — read `ASSETS/<pdf_id>/summary.md` (and `text.txt`
  for detail; skip the big `paper.pdf`) and weave it into the relevant page.
- **Edit / rewrite a page** — `REPORTS/<bubble-slug>/pages/<page-slug>.md`.
- **Add a page** — create `pages/<new-slug>.md` (start with `# Title`) **and** append
  `{page_slug: <new-slug>, title: <Title>}` to that bubble's `pages.yaml`.
- **Add a bubble** — make `REPORTS/<slug>/pages.yaml` (`home: overview`, one `overview` page) +
  `pages/overview.md`, then add the slug to `bubbles.yaml` with `approved: true`.
- **Answer questions** grounded in the user's PDFs/pages; say so when you're unsure rather than
  inventing citations.
- **Re-sync** — if the user also runs lockedin elsewhere, `rsync` their
  `data/users/<DEV_USERNAME>/` down before editing and back up after; there's no built-in sync.

### Formatting rules (match the app)

- Math uses `$…$` (inline) and `$$…$$` (display) only — never `\( \)` or `\[ \]`.
- Internal links: `[[page-slug]]` or `[[Exact Page Title]]` (titles resolve to slugs on save).
- Output is clean report prose a reader sees — no XML tags, no "I changed…" changelog lines.
- `data/` is git-ignored: never commit the user's content or `.env`.

After structural edits, run `uv run lockedin devmode` again to re-print the bubble/page list as
a sanity check.
