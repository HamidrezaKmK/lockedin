# CLAUDE.md — developer guide for `lockedin`

The "ultimate research assistant for grad students": upload papers → group into **idea
bubbles** (topic tags) → maintain Notion-like multi-page Markdown reports per bubble (you write
them), alongside a **read-only** research chat that knows your reports + papers, with a
switchable LLM backend (local Qwen / OpenAI / Claude / Gemini).

It is a prototype, local-first FastAPI app. Patterns were lifted from the sibling `../ocd`
project (contextvar per-user roots, PBKDF2 auth, OpenAI-compatible model layer, single
no-build SPA).

A plain `claude` session in this repo is for **developing lockedin**. Separately, the
`./scientist.sh` launcher can run Claude, Codex, or agy as a *report assistant* for one user
(it authenticates from `.env` and injects bubble-scoped context, so the base commands are
unaffected) — see
[`docs/DEV_MODE.md`](docs/DEV_MODE.md).

## Run / test

```bash
uv sync                          # manage deps + .venv (NOT pip)
ollama serve && ollama pull qwen2.5:7b-instruct   # default model
uv run lockedin serve [--port 8080] [--host 0.0.0.0]
uv run lockedin doctor           # check the active model is reachable
uv run lockedin devmode          # verify LOCKEDIN_USER/PASS, print that user's workspace (docs/DEV_MODE.md)
```

Always run `uv run ...` **from the project root** — `cd`-ing elsewhere breaks uv's project
resolution and the `lockedin` entry point fails to spawn.

Sharing: a Cloudflare quick tunnel (`cloudflared tunnel --url http://localhost:<port>`) gives a
temporary public HTTPS URL with no domain/account — see README. For agent-driven editing of a
user's reports without running the server, see [`docs/DEV_MODE.md`](docs/DEV_MODE.md) +
`uv run lockedin devmode`.

News crawler (premium, opt-in): grant a user, then run the server with the switch on. Crawling
happens entirely in-app via the **Crawl now** button (no daemon). Uses the host `claude` CLI's
own login — log it into your Claude subscription first.

```bash
uv run lockedin news-grant <user>                  # accounts.yaml: news_enabled=true
LOCKEDIN_NEWS_ENABLED=1 uv run lockedin serve       # News chat works for granted users
uv run lockedin news-revoke <user>                  # remove entitlement
```

Without `LOCKEDIN_NEWS_ENABLED=1` (the default), `POST /api/news/crawl` → 503. The switch is
read from the **server's** environment. `uv run lockedin serve` loads the project-root `.env`
first, and systemd deployments additionally read `ops/lockedin.env`.

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
  with a canned response so the read-only chat + save pipeline is pinned exactly: the chat
  returns prose only (never an edit/new-page proposal), stray `<EDIT>`/`<NEWPAGE>` tags are
  scrubbed from display, the chat never writes a page, `normalize_wikilinks` on save, math
  normalization, chat-title cleanup. Every bug we've hit has a test here — extend it.
- `tests/test_live_qwen.py` — runs a realistic chat through qwen and asserts the read-only
  *invariants*: a content question gets a non-empty reply with no raw `<EDIT>`/`<NEWPAGE>` tags,
  and the chat never creates/removes/mutates a page. qwen is non-deterministic, so it retries.
- `tests/test_news.py` — deterministic guard for the news crawl chat: monkeypatches
  `news._agent_events` with canned Claude Code NDJSON and pins the stream→`@@ITEM`-parse→
  live-save→dedup→session→accept/discard flow, the date range/pointer, and premium/kill-switch
  gating. No network/LLM.
- `tests/_fixtures.py` — builds throwaway qwen workspaces seeded with the two diffusion papers
  (copies `meta.yaml`/`summary.md`/`text.txt` from a local user, not the 50 MB `paper.pdf`).
- `tests/setup_unittest_user.py` — (re)creates the persistent `unittest`/`unittest` fixture user
  in `data/users/`, qwen-backed, with the diffusion bubble + a seeded overview. Idempotent.
  Log in as it to repro by hand.

You can still smoke-test the HTTP layer with FastAPI's `TestClient` against a throwaway home.
The session cookie is now `Secure` by default, so any auth flow must use an **HTTPS base URL**
(or set `LOCKEDIN_INSECURE_COOKIE=1`) — otherwise the cookie isn't sent back and you get 401:

```bash
LOCKEDIN_HOME=/tmp/li uv run python -c "from fastapi.testclient import TestClient; \
from lockedin import server; c=TestClient(server.build_app(), base_url='https://testserver'); ..."
```

**When to run / extend the tests (policy for agents):** after any change that touches the
chat/save pipeline (`reports.chat_stream`, `bubbles.save_page`/wikilinks, `models.stream_chat`)
or any change the user flags as **major** — or whenever the user explicitly asks. Add a
deterministic test in `test_editing_logic.py` for the specific behavior first (it's the
reproducible guard), then run the live qwen test to confirm the real model still behaves. A
change is "major" if it alters a documented design decision, the SSE event shape, or how pages
are saved/linked.

## Architecture

Layered; the server is thin HTTP/SSE glue over `service.py`.

| Module | Responsibility |
|--------|----------------|
| `paths.py` | All filesystem paths. **Per-user isolation via a `contextvars` root** pushed with `paths.use_root(home)`. Per-user paths (`ASSETS_DIR`, `REPORTS_DIR`, bubble/page/asset paths) resolve against the active context root; account-registry paths resolve against the base root. |
| `auth.py` | PBKDF2-HMAC-SHA256, `accounts.yaml`, in-memory sessions (lost on restart). `set_password`/`rename_user` back the account-settings endpoint. **Account approval:** the first account created is `approved=true`+`admin=true`; later sign-ups are `approved=false` (with `pending_at`) and **cannot log in** until an admin flips them via the `/api/admin/users*` endpoints (surfaced in the Settings → *User access* panel). `is_approved`/`is_admin`/`set_approved`/`delete_user` enforce this; `MIN_PASSWORD_LEN=4`. |
| `sharing.py` | Global (base-root) `share_index.yaml` mapping an unlisted token → `{user, slug}` for the public `/share/<token>` routes (which run with no session). |
| `models.py` | **One global active model** (`qwen`/`openai`/`claude`/`gemini`) per user. `stream_chat`/`complete`/`attach_pdf`/`health_check`. Branches: OpenAI-compatible (qwen via Ollama, openai, gemini via its OpenAI-compat endpoint) vs Anthropic SDK (claude). Health check does NOT ping API for cloud providers — only checks credentials are present. |
| `assets.py` | PDF storage: `ASSETS/<pdf_id>/{paper.pdf,text.txt,summary.md,meta.yaml}`. Atomic writes. |
| `tagger.py` | Background ingest after upload: extract text → summarize (cached) → suggest reuse-first tags. Fail-safe. |
| `bubbles.py` | Bubble registry (`bubbles.yaml`) + the per-bubble **mini-wiki**: pages manifest, page CRUD, image storage, legacy-`report.md` migration, chat-session CRUD (`chats/` dir). |
| `todos.py` | **Global per-user TODOs** (GitHub-issue style), stored in `todos.yaml` (`next_id` + `todos` map). Pure storage: compact integer `id`, `title`, markdown `note`, `done`. CRUD only — it does **not** import `bubbles`; reference counting, delete guard, and report-reference rewrites after id compaction live in `service.py`. Referenced from report pages as `@<id>`. |
| `reports.py` | `chat_stream` — a **read-only** streamed research chat grounded in the bubble's report pages + paper summaries + deep-read PDFs (bounded context, internal compaction). Also `generate_chat_title`. It does NOT edit pages. |
| `news.py` | **Premium interactive crawl chat** (no daemon — all in `serve`). The user converses with the *operator's* Claude Code agent (headless **streaming** `claude -p --output-format stream-json --verbose --allowedTools WebSearch WebFetch`, web tools only, host login). `chat_stream` spawns the agent via `_agent_events` (the NDJSON Popen seam tests monkeypatch), parses a one-line `@@ITEM {json}` protocol to **save each paper the instant it's found** (`add_item`, dedup vs `seen`), and yields SSE `delta`/`activity`/`item`/`done` (with `stopped:"timeout"\|"max_turns"`, real `cost_usd`). "continue" resumes the same agent session (`--resume <uuid>`). Instructions are **plain text** (no per-instruction date); a single **global pointer** (`last_checked` in `config/news.yaml`) seeds the default crawl range and advances **only on `accept_session`** (typed "I'm happy" or the button) to the range's `until`. Matching is against a **persistent per-bubble scope summary** (not titles): on the first turn of a crawl, `refresh_bubble_summaries` regenerates each approved bubble's 2–4-sentence summary via the user's active model **only if its content changed** (sha256 fingerprint over name + instructions + report pages + paper summaries), caches it in `bubble_summaries.yaml`, and feeds those summaries to the agent. A **hard date-range rule** in the prompt ([since..until], plus a guaranteed closing ```json``` block) keeps old papers out. The whole interaction (tool-use **activities** + item markers + prose) is recorded to the session transcript **live** so history replays it exactly. A crawl turn sets a `running` flag on the session; the SSE worker runs to completion server-side even if the browser disconnects, so leaving the News page mid-crawl is safe — on return the SPA sees `running` and **reconnects by polling** `status`/feed until the turn ends (no live SSE re-attach). `discard_session` drops the batch. An end-of-turn **reconciliation pass** (`_extract_items_from_text`) re-scans the agent's full output + the final `result` payload for any `@@ITEM` line or fenced `{"items":[…]}` block, so every structured result reaches the feed even if streaming missed it (dedup prevents doubles). Ended conversations are **archived** to `news_chats/<uuid>.json` (`list/get/delete_chat_session`, like bubble chats). Stores in `news_items.yaml` (`items`/`seen`/active `session`). Gated by the kill switch (env `LOCKEDIN_NEWS_ENABLED`, default OFF — set on `serve`) + per-account `news_enabled`. Also surfaced in the Slack bot (`slackbot.py`) for entitled users via the `news` (list items + reasons) and `crawl` commands. `crawl` is an interactive wizard — it prompts for the from/to date range (showing defaults to confirm or replace), runs the turn, then enters a **steering** mode where free-text follow-ups (`continue`, refinements, `accept`, `stop`) drive the open session; both call `/api/news` and stream `/api/news/chat`. |
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
  bubbles.yaml                      # {slug: {name, approved, instructions, created_at, share_active, share_token}}
  todos.yaml                        # {next_id, todos:{<id>:{id,title,note,done,created_at}}} — global per-user TODOs
data/users/accounts.yaml            # password hashes (chmod 600)
data/users/share_index.yaml         # {token: {user, slug}} — global lookup for public /share links
```

## Key design decisions (don't regress these)

- **Markdown is the source of truth.** The frontend uses Toast UI Editor (CDN) but persists
  `.md`. The user writes/edits the reports themselves (in the editor, or via a strong model in
  DEV_MODE). Don't switch to a JSON/block model.
- **The chat is READ-ONLY — no AI writes to pages.** The earlier `<EDIT>`/`<NEWPAGE>` tag
  contract, section splicing, diff-accept overlay, and `generate_template` were all **removed**:
  they were too unreliable with small local models. `chat_stream` now only discusses. It assembles
  a bounded context — every report page (current page in full, others within a char budget), a
  summary of every tagged paper, and the full text of any deep-read PDFs — and compacts the
  conversation internally (`models.compact_chat`) once it grows long. The `done` event carries
  only `full_response` and `chat_text`. If you reintroduce AI editing, do it as a separate,
  explicitly-gated path; don't smuggle it back into the chat. (`git log` has the old machinery.)
- **Wikilink targets are normalized on save.** `bubbles.save_page` runs `normalize_wikilinks`:
  each `[[X]]` has any invented `prefix/` stripped, then resolves by slug, else by page **title**
  (case-insensitive), to the real slug. This is why the model is told to link by plain title —
  it can't guess server-assigned slugs, so it writes `[[Exact Title]]` and the save fixes it.
- **Bubble approval gates the report workspace.** Auto-suggested bubbles start `approved=false`
  and show an approval pane; approving materializes the pages (`ensure_pages`) so the editor opens.
- **A bubble's identity is its immutable `slug`; its `name` is display-only.** Membership is
  `idea_bubbles` = `[slug_of(tag) for tag in tags]`, so a PDF belongs to a bubble iff one of its
  tags slugifies to that slug. Renaming only changes the registry `name`; it must NOT change any
  paper's tag/slug. When tagging a PDF into a bubble, always use `bubbles.tag_for_slug(slug)`
  (a tag guaranteed to slugify back to the slug), never the display name — using the (possibly
  renamed) name slugifies to a *different* slug and splits the bubble into a phantom. The API
  exposes both `name` (display) and `tag` (stable membership tag) per bubble; the frontend's
  "pick a bubble" dropdowns insert `tag`.
- **Public sharing is unlisted + token-gated, no login.** A bubble carries a permanent
  `share_token` and a `share_active` flag; `sharing.py` holds the base-root `token → {user, slug}`
  index. The `/share/<token>[/<page>]` routes have **no auth** — access is gated only by the token
  plus the live `share_active` flag (toggling off revokes immediately; the stable token means
  toggling back on restores the same link). `_render_preview_html` is shared by the owner preview
  and the public share page (different `link_base`/`asset_base`; share mode rewrites
  `/api/bubbles/<slug>/assets/` → `/share/<token>/assets/` so figures load without a session).
  Headings get slug ids + a click-to-copy 🔗 anchor for section deep-links.
- **Account changes go through `service.update_account`.** Requires the current password; a
  username change moves the whole workspace dir and repoints the account record, in-memory
  sessions (so the cookie stays valid), and `share_index` entries.
- **Summarize-once.** Every uploaded PDF is summarized once into `summary.md`; the chat reuses
  summaries to stay cheap. "Deep-read" attaches the real PDF (Claude) or `text.txt`
  (qwen/openai), now clipped to a char budget, for the conversation.
- **Within-bubble links only.** `[[page-slug]]` links navigate between a bubble's pages; no
  cross-bubble/global wiki.
- **TODO `@<id>` references resolve by exact digits.** Typing `@5` in any report page links
  to TODO #5. The SPA's `linkifyWikilinks` and the server's `_render_preview_html` both resolve
  `@(\d+)` (digits only, so `@50` never matches `@5`) against the global TODO list at display time;
  unknown ids stay literal. Reference counting (`service._scan_references`) scans page manifests
  **read-only** — it uses `bubbles.manifest`, NOT `list_pages` (which would `ensure_pages` and
  clobber an unmaterialized page). A TODO can be **deleted only when it has zero references** — the
  guard is in `service.delete_todo` (raises → HTTP 409), so both the web UI and the Slack `todos`
  command inherit it. After deletion, remaining TODO ids are compacted and report `@id` references
  for shifted TODOs are rewritten. Done TODOs are hidden by default (web: Open⇄Done toggle; Slack
  lists open only).

## Gotchas

- **`server.py` must NOT use `from __future__ import annotations`.** FastAPI resolves the
  Pydantic body models that are defined *inside* `build_app()`; stringized annotations make it
  treat request bodies as query params (→ 422). Other modules use the future import freely.
- **SSE runs in a worker thread, not Starlette's threadpool.** `_stream()` in `server.py` runs
  the chat generator in a dedicated `threading.Thread` feeding a `queue.Queue`. This keeps the
  per-user `contextvars` root consistent for the generator's whole life — Starlette could
  otherwise resume a sync generator on a different pool thread and lose the context. `chat_stream`
  therefore wraps its whole body in `with paths.use_root(home):` so the context survives `yield`s.
- **SSE event shape:** `{"type":"delta","text":...}`, `{"type":"done", ...}`, `{"type":"error","detail":...}`.
  The `done` event from `/chat` additionally carries `full_response` (raw output, pushed to chat
  history) and `chat_text` (cleaned prose for display). The frontend `streamPost()` parses
  `data:` lines.
- **Chat session persistence.** Sessions are stored as JSON in `REPORTS/<slug>/chats/`.
  `bubbles.{list,get,save,delete}_chat_session` manage them. Auto-saved client-side after each
  assistant reply. The session dropdown above the chat lets you switch between or delete sessions.
  Session ID format: `YYYYMMDDHHMMSS-<4hex>` (sorts by creation time).
- **Claude auth:** the global Claude model uses an Anthropic API key stored in the user's model
  settings. Browser-pasted Claude OAuth/subscription tokens are no longer supported. The News
  crawler is separate: it runs through the host Claude CLI when the backend feature is enabled.
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
