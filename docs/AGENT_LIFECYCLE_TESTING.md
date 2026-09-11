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
   retained agents. A recovery may adopt a provider-supported full-history fork when the original
   thread is stuck behind a stale writer, but it must never create an empty replacement silently.
4. **An attached conversation owns itself.** While its interactive chat is open, report and chalk
   talk files remain ordinary synchronized files and the person can work in them directly. Direct
   messages and mark assignments may queue, but no background provider process starts until the
   chat exits. Queue polling and presence checks consume no model turn.
5. **A name is one growing memory.** Every successful background turn resumes the conversation id
   stored for that named agent. A missing or provider-rejected id fails visibly and stays stored;
   it is never cleared automatically. Only the explicit `agent reset` escape hatch can discard
   history, and retirement removes the identity from LockedIn entirely.
6. **Retirement is final inside LockedIn.** Website retirement removes the profile and recovery
   mapping and cancels unfinished jobs. The provider may still retain its own local transcript,
   but a later setup link does not recreate the retired LockedIn agent.
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
- retirement cancels open work, removes the profile, and leaves no recovery mapping;
- setup tickets expose all three OS choices, are single-use, and resume an existing binding.

## Live disposable gate

Run this after automated tests for lifecycle, queue, setup, provider-adapter, or installer changes.
Do not use a real research agent. Create a disposable bubble or clearly named disposable agents,
and record every original conversation id before starting.

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
marked quote/comment/reply thread appear together in chronological job order.

### E. Recovery

Stop the disposable worker without retiring agents. Mint a fresh setup link from the real bubble
HTTP route and run it in the same directory. Confirm a healthy replacement worker appears, all
three identities and conversation ids remain, and a third marker-recall message succeeds. Redeeming
the same ticket again must fail.

For Codex only, also simulate or reproduce a stale writer lock. The first attempt may requeue; any
adopted fork must contain the prior marker and become the sole stored conversation for later turns.
A genuinely open interactive chat must never trigger that recovery path.

### F. Missing memory and retirement

In an isolated provider home, hide one disposable conversation record and submit one tiny job. It
must fail visibly, make zero new conversation, and preserve the old id. Restore the record and
confirm a newly submitted job resumes it.

Retire all disposable agents from the website. Confirm open jobs are cancelled, profiles disappear,
and running the setup link does not restore them. Delete the disposable bubble/files and verify the
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
