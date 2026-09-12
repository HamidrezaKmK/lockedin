# Agent lifecycle contract and stress test

This is the release gate for changes that can affect LockedIn Scientist agents. It applies equally
to Codex, Claude Code, and agy. A provider-specific implementation may differ, but the behavior a
person sees must satisfy the same contract.

## Product contract

1. **Setup is folder-scoped.** From a bubble, choose macOS, Linux, or Windows explicitly and run
   the one-use setup line in the project directory on the machine that will run agents. The line
   installs or upgrades Scientist, authorizes that machine, binds or resumes that directory, and
   installs every detected provider skill. It does not launch a model or create a conversation.
2. **Registration happens inside a conversation.** Start `codex`, `claude`, or `agy` normally,
   invoke the LockedIn skill, and optionally register that current conversation under a name.
   Exiting the interactive CLI makes its queued work eligible to run.
3. **Recovery preserves identities.** A stopped worker, revoked machine credential, temporary
   network failure, or stale provider writer lock must not erase an agent record or blank its
   conversation id. Running a fresh setup line in the same directory restores synchronization and
   retained agents. If a failed setup left a partial `.lockedin` without a valid binding, a fresh
   setup line moves that tree to a timestamped sibling recovery folder, rebuilds from the server,
   and preserves its stable worker identity; it never silently deletes unsynchronized local work.
   A recovery may adopt a provider-supported full-history fork when the original
   thread is stuck behind a stale writer, but it must never create an empty replacement silently.
4. **An attached conversation owns itself.** While its interactive chat is open, report and chalk
   talk files remain ordinary synchronized files and the person can work in them directly. Direct
   messages and mark assignments may queue, but no background provider process starts until the
   chat exits. Queue polling and presence checks consume no model turn.
5. **A name is one growing memory.** Every successful background turn resumes the conversation id
   stored for that named agent. A missing or provider-rejected id fails visibly and stays stored;
   it is never cleared automatically. Only the explicit `agent reset` escape hatch may change the active provider conversation.
   Resolving a mark must never erase the active agent’s readable work history.
6. **Retirement stops execution, not memory.** Website retirement removes the active profile and
   recovery mapping and gracefully cancels unfinished jobs. Its direct and mark-based history is
   retained only in a private server-side archive; normal web APIs and the frontend expose neither
   the retired profile nor its jobs. The retired name is immediately reusable. A later setup link
   does not recreate the retired agent.
7. **Providers have parity.** Registration, attachment, queuing, direct replies, mark edits,
   recovery, continuity, failure reporting, and retirement have the same acceptance criteria for
   Codex, Claude, and agy.
8. **Spend is deliberate.** Setup, sync, presence, retirement, recovery checks, and all simulated
   tests use zero model calls. A live gate uses the smallest suitable model, one short response per
   checkpoint, and no exploratory searches unless a failure requires diagnosis.

## Automated gate: run for every agent-related change

From the repository root:

```bash
uv run python -m unittest \
  tests.test_agent_vendors \
  tests.test_agents_client \
  tests.test_agents \
  tests.test_setup_link \
  tests.test_skill_freshness -v
node tests/agents-e2e.mjs
node tests/scientist-setup-e2e.mjs
```

Before and after the gate, verify that the installed client was not touched. The installer test
must set both a temporary `HOME` and `XDG_DATA_HOME`.

```bash
sha256sum ~/.local/share/lockedin-scientist/client/scientist_cli.py \
  ~/.local/share/lockedin-scientist/client/agent_vendors.py
```

The automated gate must prove, for all three provider adapters:

- an existing id is resumed and the configured model is forwarded;
- an attached chat reports `attached`, leaves the job queued, and starts no child process;
- the first job after exit starts exactly once;
- two sequential jobs use the same stored id and the second prompt is not a fresh-persona prompt;
- a busy-chat signature requeues without spending an additional turn immediately;
- a missing id and a provider “not found” response fail without updating `conversation` or `fresh`;
- a Codex stale-writer recovery forks the full history and atomically adopts the returned id;
- a successful mark edit posts exactly one reply and a direct message returns exactly one reply;
- the agent popup orders direct jobs and marked threads together and includes mark context plus all replies;
- resolving a mark hides it from the report or chalk talk, cancels unfinished assigned work, and
  leaves its quote, image, comments, and replies unchanged in agent history;
- deleting a chalk-talk slide automatically resolves and archives its marks, preserves their threads
  and screenshots, and shifts marks on every retained later slide;
- retirement cancels open work, removes the active profile, recovery mapping, and jobs from normal
  web responses, preserves a server-only archive, and permits immediate name reuse;
- popup and mark-thread replies render inline/display LaTeX through KaTeX, the icon-only Send
  control stays vertically centered, and the working robot has a reduced-motion fallback;
- every RHS mark card collapses and expands from its header control without losing draft, job,
  or conversation state, and its collapsed state survives reload plus logout/login for that
  user, workspace, and bubble;
- an open chalk talk polls only its lightweight revision/status route for at least two minutes;
  it must not repeatedly download or parse the full deck, notes, or job history, and resolving a
  mark must hide its card immediately while safely restoring it if the request fails; an expired
  or revoked login must stop that tab's polling interval instead of retrying forever;
- freehand drawing accepts Ctrl+Z and Cmd+Z as undo, exposes an adjustable outer screenshot box,
  stores the chosen region, and uploads a PNG whose dimensions match that smaller crop;
- a standalone chalk-talk image visibly renders its Markdown caption, including LaTeX and links;
- setup tickets expose all three OS choices, are single-use, and resume an existing binding;
- Windows upgrade warnings show only the PowerShell installer, workers are detached from the
  launching PowerShell process, and setup reports success only after the child is verifiably alive.
- a setup link run against a partial `.lockedin` with no binding preserves the whole partial tree
  in a timestamped sibling recovery folder, restores the worker identity, and completes a clean sync.
- a disposable Git main checkout and linked worktree are created under `tests/.tmp`; setup run from
  a nested worktree directory binds `.lockedin` to that worktree, and Codex, Claude, and agy all
  register there without borrowing the main checkout or a sibling worktree's conversation.
- a mark-tagged scratch figure script synchronizes privately, appears under the bubble's
  Assets → Agent scratch tab,
  downloads byte-for-byte, stays invisible to another workspace member, and is edited in place by
  a second agent assigned to the same mark rather than duplicated; an untagged flat file also
  synchronizes into that bubble as legacy scratch without a fabricated mark association.

## Live disposable gate

Run this after automated tests for lifecycle, queue, setup, provider-adapter, or installer changes.
The harness must create its own test account, workspace, bubble, server data, Git repository, linked
worktree, provider homes, and client state beneath a unique `tests/.tmp/` directory, then remove that
directory even after failure. Never use a production account, production bubble (including
`drifting-models-for-rl`), repository outside the harness, or the operator's provider/session stores.

Suggested low-cost models as of 2026-09-11:

| Provider | Model | Extra limit |
|---|---|---|
| Codex | `gpt-5.6-luna` | short prompts, low effort |
| Claude Code | `haiku` | `--max-budget-usd 0.05` for direct CLI probes |
| agy | `gemini-3.8-flash-low` | low effort |

Provider catalogs change. Confirm locally with the CLI help or model-list command before the run;
choose the cheapest model that still supports file edits and tools.

### A. Setup and registration

For each OS tab, confirm that selecting it changes only the displayed command. Execute one Linux or
macOS line on the test host. Confirm the second redemption fails, the existing directory binding is
resumed rather than rebuilt, and no provider process was launched.

Create a main Git checkout plus a linked worktree in the disposable directory. Connect from a nested
directory inside the linked worktree and require `.lockedin/config/binding.json` to be created at the
linked worktree top level. Put a sentinel binding in main and prove setup neither borrows nor modifies
it. Run `doctor`, registration, listing, recovery, and retirement from that nested directory. Seed
newer fake Codex and agy conversations in main and a sibling tree; discovery must select only the
active worktree. Claude's environment session id must register against the same worktree-local binding.

For each provider, start one cheap interactive conversation in the disposable project, invoke the
skill, register a unique disposable name, and exit. Record name, provider, model, agent id, and
conversation id. Confirm all three appear idle under the same folder.

### B. Continuity and direct messages

Send each agent two tiny direct messages:

1. `Remember marker <provider>-A. Reply only READY.`
2. `What marker did I give you? Reply with it only.`

Both jobs must finish once. The second answer must contain the first marker, and the agent's stored
conversation id must remain unchanged. No fresh-persona preamble may appear in the second job log.

### C. Manual attachment pause

Open each recorded conversation interactively. While it is open, queue `Reply only AFTER-EXIT.`
Confirm for at least two worker polls that the job remains queued, the UI says the chat is open,
and no provider child for that job exists. Make a harmless direct edit to the disposable chalk
slide and confirm it synchronizes. Exit the interactive chat; the queued job must then run once in
the same conversation.

### D. Mark edit

Create one disposable chalk talk with three separate, unmistakable typos. Put one text mark on each
and assign one to each provider. Ask each agent to change only its typo. Confirm each file diff is
one word, each mark receives exactly one provider reply, all jobs finish, and all stored conversation
ids still match the continuity records. Open each agent popup and confirm that its direct exchanges and
marked quote/comment/reply thread appear together in chronological job order. Include one inline
and one display LaTeX reply and confirm both render, exercise the working indicator, and collapse and
re-expand a long RHS mark without changing its job or thread. Reload, then log out and back in, and
require that mark to remain collapsed both times. Delay one resolve response by a full second and
require the card to disappear before the response arrives. Leave the talk open for at least two
minutes and require repeated lightweight status calls but no repeated full-talk downloads. Draw two
strokes, undo one with Ctrl/Cmd+Z, shrink and move the screenshot box, and require one stored stroke
plus a cropped PNG. Include a standalone figure whose caption has LaTeX and a wikilink; require a
visible caption, rendered math, and a working link.

### E. Cross-agent scratch reuse

In the disposable linked worktree, assign one figure mark to a cheap agent and ask it to create and
run a small Python figure generator. Require every used scratch artifact to have the exact stable
`mark-...--<description>.<ext>` prefix supplied by the turn. Assign a follow-up on the same mark to a
different provider and request one unmistakable visual change. Its job log must show that it searched
the matching prefix, opened the existing script, edited that same pathname, and reran it. Fail if a
second generator is created. Confirm the final script appears once in the bubble's Assets → Agent
scratch tab, downloads byte-for-byte, and is absent from another workspace member's listing and
download route. Add an untagged flat file and confirm it appears only in this bubble with the
**Legacy · not linked** label; nested, hidden, and temporary files must remain local.

### F. Recovery

Stop the disposable worker without retiring agents. Mint a fresh setup link from the real bubble
HTTP route and run it in the same directory. Confirm a healthy replacement worker appears, all
three identities and conversation ids remain, and a third marker-recall message succeeds. Redeeming
the same ticket again must fail.

On a Windows test host, run the PowerShell setup line from an ordinary PowerShell terminal and
again through `iex`. After each run, wait for at least three sync intervals, then require `ps` to
show exactly one live worker for the project and `doctor` to pass. Close the launching shell and
repeat both checks from a new shell. Force an outdated-client response and confirm the warning
contains `install.ps1 | iex` and contains neither `install.sh` nor `bash`. A child that exits during
startup must make setup fail with its worker log path; it must never print a successful “running”
line.

For Codex only, also simulate or reproduce a stale writer lock. The first attempt may requeue; any
adopted fork must contain the prior marker and become the sole stored conversation for later turns.
A genuinely open interactive chat must never trigger that recovery path.

### G. Resolve and preserve history

For each provider, record the full popup history, resolve its completed disposable mark, and reopen
the popup. The mark must disappear from the working report or chalk talk while the recorded quote,
picture reference, user turns, agent replies, job status, and order remain unchanged. Repeat once
with a queued mark and once with a running mark: both jobs must cancel cleanly, no provider child may
start or continue, and no conversation id may change. Retire the agent and confirm it and its jobs
disappear from the frontend and normal web API. On the server, confirm
`lockedin agent-archives <bubble> --workspace <id> --owner <user> --json` retains the same history.
Register a new agent with the
retired name and confirm it receives a distinct id and an empty new history.

### H. Missing memory and retirement

In an isolated provider home, hide one disposable conversation record and submit one tiny job. It
must fail visibly, make zero new conversation, and preserve the old id. Restore the record and
confirm a newly submitted job resumes it.

Retire all disposable agents from the website. Confirm open jobs are cancelled, active profiles and
histories disappear from the frontend, server-side archives remain available, retired names can be
reused, and running the setup link does not restore them.
Delete the disposable bubble/files and verify the
queue is empty and the worker is healthy or deliberately stopped.

## Evidence to report

A release report should state:

- commit and Scientist client version;
- automated test counts and browser result;
- provider CLI versions and cheap models used;
- per-provider conversation id before/after (abbreviated), job ids, attempts, and replies;
- attachment wait duration, setup ticket single-use result, and worker health;
- exact disposable file diff and confirmation that cleanup completed;
- any skipped provider and the concrete reason. A simulated adapter test is not a substitute for a
  skipped live subscription test; label the two separately.
