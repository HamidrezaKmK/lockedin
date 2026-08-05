# todo

Deferred work, with enough context to pick up cold.

## Give `agy` persistent Scientist instructions

**Status:** not started. Found 2026-08-05 while verifying vendor parity after the deletion-sync
fix (`6d2ca42`).

**Problem.** `run_agent` in `src/lockedin/scientist_cli.py` delivers the `role()` instructions
differently per vendor, and agy comes off worst:

| vendor | fresh session | resumed session |
|--------|---------------|-----------------|
| codex  | `-c developer_instructions=…` (system-level) | same |
| claude | `--append-system-prompt …` (system-level) | same |
| agy    | `-i …` — the **first user message** | **nothing at all** |

Two consequences, both agy-only:

1. `lockedin-scientist resume agy <bubble>` launches `["agy", *add_dir_args, "--continue"]` with
   no instructions whatsoever. The resumed session has no delete guidance, no paper inventory, no
   "never edit pages.yaml", and no external-directory scope rules — it is a bare Antigravity agent
   sitting in the mirror. `tests/test_scientist.py::test_all_vendor_launchers_resume_latest_without_new_session_prompt`
   currently *asserts* this (`if model == "agy": self.assertNotIn("-i", cmd)`), so that expectation
   has to change with the fix.
2. Even on a fresh session the instructions are a user turn, not a system prompt, so they scroll
   out of context or get compacted away on a long session. codex and claude re-assert the role
   every turn. The rules most likely to be lost are the ones that matter most: re-read
   `_lockedin_papers.md` before answering inventory questions, never edit `pages.yaml`, delete a
   page by removing its file.

Confirmed against the installed binary (`agy --help`): there is **no** `--append-system-prompt` or
any other system-prompt flag. `-i` is `--prompt-interactive`. So there is no direct equivalent.

**Clearing the conversation makes this worse.** Verified interactively (claude 2.1.222, tmux): after
`/clear` the model loses all conversation history but still reports a marker planted via
`--append-system-prompt`, because the system prompt is process-level and is rebuilt into the new
session. codex should behave the same way (`-c developer_instructions=` is process config; not
verified empirically). agy's instructions live *in the conversation*, so any clear / new-conversation
wipes the LockedIn role completely — scope boundaries, delete rules, and paper inventory included —
while the mirror keeps syncing underneath. `.agents/AGENTS.md` fixes this case too, since agy
re-reads project-local instructions per session rather than per conversation.

**Proposed fix.** agy auto-loads project-local instructions from `.agents/` at the project root
(`GEMINI.md` / `AGENTS.md`). agy already runs with `cwd=mirror.root`, so writing `role(mirror,
bubble, add_dirs)` into `<mirror>/.agents/AGENTS.md` immediately before launch would cover **both**
fresh and resumed sessions — the closest structural equivalent to codex's `developer_instructions`.

It stays client-local: `.agents/` is not in `scientist_sync._safe_files` (root allowlist plus
`REPORTS/`+`ASSETS/`), so it never syncs to the website. Rewrite it on every launch so a bubble
switch or a changed paper inventory cannot leave stale instructions behind.

**Why it was deferred.** This is a new instruction-delivery mechanism rather than a bug fix, and
agy's file loading cannot be validated headlessly — `-p` print mode is slow/flaky and auto-approves
`ask` decisions, so the check has to be an interactive `-i`/TUI run driven under tmux
(`tmux new-session -d …; send-keys; capture-pane -p`); raw PTY scraping cannot reconstruct agy's
alt-screen redraws. It also needs a `SCIENTIST_CLIENT_VERSION` bump in **both**
`scientist_cli.py` and `server.py` plus a re-release (push to the `scientist` branch, restart
`lockedin-serve.service`), which forces every installed client to reinstall.

**Acceptance.**
- A resumed agy session can state the bubble's attached papers and the page-deletion rule.
- Instructions survive a long session (verify after compaction, not just at turn one).
- `.agents/` never appears in `scientist_sync.manifest()` output for the workspace.
- Update the resume test above; add a launcher test that agy gets instructions on both paths.
