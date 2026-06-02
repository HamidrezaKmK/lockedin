# CLAUDE.md — developer guide for `lockedin`

The "ultimate research assistant for grad students": upload papers → group into **idea
bubbles** (topic tags) → maintain AI-assisted, Notion-like multi-page Markdown reports per
bubble, with a chat sidebar and a switchable LLM backend (local Qwen / OpenAI / Claude).

It is a prototype, local-first FastAPI app. Patterns were lifted from the sibling `../ocd`
project (contextvar per-user roots, PBKDF2 auth, OpenAI-compatible model layer, single
no-build SPA).

A plain `claude` session in this repo is for **developing lockedin**. Separately, the
`./claude_scientist.sh` launcher runs Claude as a *report assistant* for one user (it injects
that role via `--append-system-prompt`, so the base command is unaffected) — see
[`DEV_MODE.md`](DEV_MODE.md).

## Run / test

```bash
uv sync                          # manage deps + .venv (NOT pip)
ollama serve && ollama pull qwen2.5:7b-instruct   # default model
uv run lockedin serve [--port 8080] [--host 0.0.0.0]
uv run lockedin doctor           # check the active model is reachable
uv run lockedin devmode          # verify LOCKEDIN_USER/PASS, print that user's workspace (DEV_MODE.md)
```

Always run `uv run ...` **from the project root** — `cd`-ing elsewhere breaks uv's project
resolution and the `lockedin` entry point fails to spawn.

Sharing: a Cloudflare quick tunnel (`cloudflared tunnel --url http://localhost:<port>`) gives a
temporary public HTTPS URL with no domain/account — see README. For agent-driven editing of a
user's reports without running the server, see [`DEV_MODE.md`](DEV_MODE.md) +
`uv run lockedin devmode`.

### Tests

Stdlib `unittest` (no pytest dependency). Two layers under `tests/`:

```bash
# Deterministic regression suite — no network, no LLM, runs anywhere in <1s.
LOCKEDIN_HOME=/tmp/li_test uv run python -m unittest tests.test_editing_logic -v

# Live integration — drives the REAL qwen model. Auto-skips if Ollama is down or the
# diffusion PDFs aren't seeded locally. Provision the fixture user first:
uv run python -m tests.setup_unittest_user        # creates user unittest/unittest (qwen)
uv run python -m unittest tests.test_live_qwen -v

# Everything:
LOCKEDIN_HOME=/tmp/li_test uv run python -m unittest discover -s tests -t . -v
```

- `tests/test_editing_logic.py` — the canonical regression guard. Replaces `models.stream_chat`
  with a canned response so the report-editing pipeline (`_splice_section`, `_EDIT_RE`/
  `_NEWPAGE_RE` tolerance, bare-`<EDIT>` rejection, `<NEWPAGE>` deferral, `normalize_wikilinks`)
  is pinned exactly. Every bug we've hit has a test here — extend it, don't replace it.
- `tests/test_live_qwen.py` — runs a realistic interaction through qwen and asserts structural
  *invariants* (page title never duplicated, no raw `<EDIT>`/`<NEWPAGE>` tags in saved content,
  all `[[links]]` resolve). qwen is non-deterministic, so it retries phrasings and asserts only
  what must hold for any sane output.
- `tests/_fixtures.py` — builds throwaway qwen workspaces seeded with the two diffusion papers
  (copies `meta.yaml`/`summary.md`/`text.txt` from a local user, not the 50 MB `paper.pdf`).
- `tests/setup_unittest_user.py` — (re)creates the persistent `unittest`/`unittest` fixture user
  in `data/users/`, qwen-backed, with the diffusion bubble + an overview that has duplicated
  links (the state the editing flow must clean up). Idempotent. Log in as it to repro by hand.

You can still smoke-test the HTTP layer with FastAPI's `TestClient` against a throwaway home.
The session cookie is now `Secure` by default, so any auth flow must use an **HTTPS base URL**
(or set `LOCKEDIN_INSECURE_COOKIE=1`) — otherwise the cookie isn't sent back and you get 401:

```bash
LOCKEDIN_HOME=/tmp/li uv run python -c "from fastapi.testclient import TestClient; \
from lockedin import server; c=TestClient(server.build_app(), base_url='https://testserver'); ..."
```

**When to run / extend the tests (policy for agents):** after any change that touches the
report-editing pipeline (`reports.py`, `bubbles.save_page`/wikilinks, the `<EDIT>`/`<NEWPAGE>`
tag contract, `models.stream_chat`) or any change the user flags as **major** — or whenever the
user explicitly asks. Add a deterministic test in `test_editing_logic.py` for the specific
behavior first (it's the reproducible guard), then run the live qwen test to confirm the real
model still behaves. A change is "major" if it alters a documented design decision, the SSE
event shape, the tag/parse contract, or how pages are saved/linked.

## Architecture

Layered; the server is thin HTTP/SSE glue over `service.py`.

| Module | Responsibility |
|--------|----------------|
| `paths.py` | All filesystem paths. **Per-user isolation via a `contextvars` root** pushed with `paths.use_root(home)`. Per-user paths (`ASSETS_DIR`, `REPORTS_DIR`, bubble/page/asset paths) resolve against the active context root; account-registry paths resolve against the base root. |
| `auth.py` | PBKDF2-HMAC-SHA256, `accounts.yaml`, in-memory sessions (lost on restart). |
| `models.py` | **One global active model** (`qwen`/`openai`/`claude`/`gemini`) per user. `stream_chat`/`complete`/`attach_pdf`/`health_check`. Branches: OpenAI-compatible (qwen via Ollama, openai, gemini via its OpenAI-compat endpoint) vs Anthropic SDK (claude). Health check does NOT ping API for cloud providers — only checks credentials are present. |
| `assets.py` | PDF storage: `ASSETS/<pdf_id>/{paper.pdf,text.txt,summary.md,meta.yaml}`. Atomic writes. |
| `tagger.py` | Background ingest after upload: extract text → summarize (cached) → suggest reuse-first tags. Fail-safe. |
| `bubbles.py` | Bubble registry (`bubbles.yaml`) + the per-bubble **mini-wiki**: pages manifest, page CRUD, image storage, legacy-`report.md` migration, chat-session CRUD (`chats/` dir). |
| `reports.py` | Streamed generators: `generate_template` (gated, saves page), `edit_page` (returns a proposal, does NOT save), `chat_stream` (unified chat+edit — parses `<EDIT>` and `<NEWPAGE>` tags). |
| `service.py` | Orchestration; wraps non-streaming ops in `use_root`. Streaming generators manage their own root. |
| `server.py` | FastAPI app + routes + SSE. |
| `web/index.html` | The entire SPA — vanilla JS IIFE, no build step, CDN deps. |

### Per-user data layout (all git-ignored under `data/`)

```
data/users/<username>/
  config/active_model.yaml          # active provider + per-provider keys/models
  ASSETS/<pdf_id>/{paper.pdf,text.txt,summary.md,meta.yaml}
  REPORTS/<bubble_slug>/            # a mini-wiki per bubble
    pages.yaml                      # { home: <page_slug>, pages: [{page_slug,title}] }
    pages/<page-slug>.md
    assets/<figure>.png
    chats/<session_id>.json         # persisted chat sessions
  bubbles.yaml                      # {slug: {name, approved, instructions, created_at}}
data/users/accounts.yaml            # password hashes (chmod 600)
```

## Key design decisions (don't regress these)

- **Markdown is the source of truth.** The frontend uses Toast UI Editor (CDN) but persists
  `.md`. This is *why* AI edits + accept/reject diffs work and why we chose it over Editor.js.
  Don't switch to a JSON/block model.
- **Unified chat+edit model.** There is no separate "edit mode" — a single chat endpoint handles
  both. The model signals its intent via XML-ish tags in its response:
  - `<EDIT section="## Heading">...</EDIT>` — proposes a section replacement (diff-gated, not saved until Accept).
  - `<EDIT page="true">...</EDIT>` — proposes a full-page replacement (same).
  - `<NEWPAGE title="Title">...</NEWPAGE>` — proposes a new page (per-page Accept/Reject card, not saved until Accept).
  `_EDIT_RE` and `_NEWPAGE_RE` in `reports.py` parse these. **Both regexes tolerate a missing
  closing tag** (terminate at the next tag or EOF) — small local models routinely drop
  `</NEWPAGE>`/`</EDIT>`, and without this the raw XML leaked into saved pages. Section constraint
  is enforced by `_splice_section` — even if the model bleeds into adjacent sections, the splice
  only replaces the declared section. `done` event carries `full_response` (for history context),
  `chat_text` (cleaned prose), `edit_proposal`, and `new_page_proposals`.
- **Edits AND new pages are both gated.** `edit_page` / the `<EDIT>` tag returns the proposed
  full page but **never saves**; the client shows a diff overlay and only Accept does
  `PUT /pages/{page}`. `<NEWPAGE>` likewise returns *proposals* (`new_page_proposals`) — the
  client shows a per-page Accept card and only on Accept does `POST /pages` (assign slug) +
  `PUT /pages/{slug}` (save content). `generate_template` still saves immediately (explicit action).
- **Wikilink targets are normalized on save.** `bubbles.save_page` runs `normalize_wikilinks`:
  each `[[X]]` has any invented `prefix/` stripped, then resolves by slug, else by page **title**
  (case-insensitive), to the real slug. This is why the model is told to link by plain title —
  it can't guess server-assigned slugs, so it writes `[[Exact Title]]` and the save fixes it.
- **Report generation is gated on `approved`.** Auto-suggested bubbles start `approved=false`;
  no report can be generated until the user approves.
- **Summarize-once.** Every uploaded PDF is summarized once into `summary.md`; chat/generation
  reuse summaries to stay cheap. "Deep-read" attaches the real PDF (Claude) or `text.txt`
  (qwen/openai) for a single turn.
- **Within-bubble links only.** `[[page-slug]]` links navigate between a bubble's pages; no
  cross-bubble/global wiki.

## Gotchas

- **`server.py` must NOT use `from __future__ import annotations`.** FastAPI resolves the
  Pydantic body models that are defined *inside* `build_app()`; stringized annotations make it
  treat request bodies as query params (→ 422). Other modules use the future import freely.
- **SSE runs in a worker thread, not Starlette's threadpool.** `_stream()` in `server.py` runs
  each report generator in a dedicated `threading.Thread` feeding a `queue.Queue`. This keeps
  the per-user `contextvars` root consistent for the generator's whole life (including the final
  save) — Starlette could otherwise resume a sync generator on a different pool thread and lose
  the context. Streaming generators in `reports.py` therefore wrap their whole body in
  `with paths.use_root(home):` so the context survives across `yield`s.
- **SSE event shape:** `{"type":"delta","text":...}`, `{"type":"done", ...}`, `{"type":"error","detail":...}`.
  The `done` event from `/chat` additionally carries: `full_response`, `chat_text`, `edit_proposal`
  (`{section, content}` or absent), `new_page_proposals` (`[{title, content}]` or absent — the
  client commits each accepted one via `POST /pages` + `PUT /pages/{slug}`).
  Generate/edit `done` carries `content`. The frontend `streamPost()` parses `data:` lines.
- **Chat session persistence.** Sessions are stored as JSON in `REPORTS/<slug>/chats/`.
  `bubbles.{list,get,save,delete}_chat_session` manage them. Auto-saved client-side after each
  assistant reply. The session dropdown above the chat lets you switch between or delete sessions.
  Session ID format: `YYYYMMDDHHMMSS-<4hex>` (sorts by creation time).
- **Claude auth:** `auth_method` is `api_key` or `subscription`. Subscription uses the Anthropic
  SDK `auth_token` (Bearer) + `anthropic-beta: oauth-2025-04-20`; the user pastes a token (no
  full OAuth login flow). The server does NOT read `~/.claude/.credentials.json` — each user must
  enter their own token. The subscription tier shares rate limits with the Claude Code CLI; only
  Haiku reliably works under load. API-key path is the well-tested one.
- **Gemini** uses Google's OpenAI-compatible endpoint (`generativelanguage.googleapis.com/v1beta/openai/`).
  No new SDK needed — same code path as qwen/openai. API key from AI Studio (aistudio.google.com/apikey).
- **Math + WYSIWYG:** math renders via KaTeX auto-render on the markdown **preview** pane. Toast
  UI's full WYSIWYG mode handles inline LaTeX less reliably; default stays markdown+preview.
- **Frontend has no build.** Validate JS with `node --check` on the extracted `<script>` body;
  a `[`/`]` "imbalance" is just `[[wikilink]]` string literals, not a real error.
- **Legacy migration:** opening a bubble runs `bubbles.ensure_pages()`, which moves any old
  `report.md` into `pages/overview.md`. Don't reintroduce single-`report.md` assumptions.

## Conventions

- Atomic file writes (`.tmp` + `os.replace`) for anything user data touches.
- Background/LLM code is fail-safe: catch broadly, log, leave the asset usable (e.g. ingest
  failure → empty tags + `attention_flag=true`, surfaced in the Attention Queue).
- Slugs via `python-slugify`; tags carry both display `tags` and slugified `idea_bubbles`.
- Commit messages end with the standard Co-Authored-By trailer. `data/` is git-ignored — never
  commit user content or credentials.
