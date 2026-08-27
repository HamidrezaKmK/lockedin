# CLAUDE.md — developer guide for `lockedin`

The "ultimate research assistant for grad students": upload papers → group into **idea
bubbles** (topic tags) → maintain Notion-like multi-page Markdown reports per bubble (you write
them). A switchable LLM backend (local Qwen / OpenAI / Claude / Gemini) exists **only** to
summarize uploaded PDFs — there is no AI chat anywhere in the product.

It is a prototype, local-first FastAPI app. Patterns were lifted from the sibling `../ocd`
project (contextvar per-user roots, PBKDF2 auth, OpenAI-compatible model layer, single
no-build SPA).

A plain `claude` session in this repo is for **developing lockedin**. The supported report
workflow uses `lockedin-scientist sync <bubble>` from a user's project; it maintains one
project-local, bubble-bound `.lockedin/` directory and its `SKILL.md` guides normal agent runs.

## Run / test

```bash
uv sync                          # manage deps + .venv (NOT pip)
ollama serve && ollama pull qwen2.5:7b-instruct   # optional local Qwen provider
uv run lockedin serve [--port 8080] [--host 0.0.0.0]
uv run lockedin doctor           # check the model configuration in the current data root
```

Always run `uv run ...` **from the project root** — `cd`-ing elsewhere breaks uv's project
resolution and the `lockedin` entry point fails to spawn.

Sharing: a Cloudflare quick tunnel (`cloudflared tunnel --url http://localhost:<port>`) gives a
temporary public HTTPS URL with no domain/account — see README. Use `lockedin-scientist` for
agent-driven report editing through authorized, project-local bubble synchronization.

### Tests

Stdlib `unittest` (no pytest dependency). Two layers under `tests/`:

```bash
# Deterministic regression suite — no network, no LLM, runs anywhere in <1s.
LOCKEDIN_HOME=/tmp/li_test uv run python -m unittest tests.test_editing_logic -v

# Everything:
LOCKEDIN_HOME=/tmp/li_test uv run python -m unittest discover -s tests -t . -v
```

- `tests/test_editing_logic.py` — the canonical regression guard: `normalize_wikilinks` on
  save, display-math normalization + mtime stability, save-conflict detection, TODOs,
  citations, figures, review comments, asset summarization (`models.complete` canned), and
  `ChatSurfaceStaysRemoved` — pins that the deleted research chat (routes, backend machinery,
  frontend pane) does not creep back. Every bug we've hit has a test here — extend it.
- `tests/test_presence.py` — the presence registry and its HTTP surface, including that a
  rejected (426) worker is still listed with its diagnosis.
- `tests/source-markers.mjs` (`npm run test:source-markers`) — pins where the preview's
  `<!--li-src:N-->` offset markers may be injected. A marker is an HTML comment, and an HTML
  block does not lazily continue a blockquote, so a marker between two `>` lines closes the
  quote and the next `>` opens a new one — which rendered one quote as one box per line. Only a
  quote's first line is marked; headings and list items keep theirs.
- `tests/presence-e2e.mjs` (`npm run test:presence-e2e`) — real Chrome against a disposable data
  root: registers four worker directories (healthy / failing / out of date / cleanly stopped)
  through the ordinary v2 endpoints and drives the chip, dropdown, detail, and leave path,
  including that the clean stop is hidden. Set `LOCKEDIN_E2E_SHOTS=<dir>`
  to keep screenshots.
- `tests/toolmenu-e2e.mjs` (`npm run test:toolmenu-e2e`) — real Chrome: the page toolbar carries
  only `+`, the tab list, `⋮` and `⛶`; the dropdown groups view modes / this bubble / Overleaf /
  sharing; a view mode picked from it applies and is marked active; Papers opens as a popup and
  closes on its backdrop; toggling the public link rewrites the open menu in place and lights the
  trigger's dot; an outside click closes it; and the whole thing still works at 390×800 with no
  floating mobile buttons left. Honors `LOCKEDIN_E2E_SHOTS` too.
- `tests/scientist-setup-e2e.mjs` (`npm run test:scientist-setup-e2e`) — real Chrome: the 🤖 sits
  beside the presence chip, the dialog mints a link and says it is single-use, the OS tabs swap
  between the `curl` and PowerShell forms, Copy reaches the clipboard, the link actually serves a
  runnable script, and the dialog fits a phone. Honors `LOCKEDIN_E2E_SHOTS`.
- `tests/test_setup_link.py` — the ticket's negative properties: no session no mint, one redeem
  only, expiry, and that serving a script never leaks the token.
- `tests/_fixtures.py` — builds throwaway qwen workspaces seeded with the two diffusion papers
  (copies `meta.yaml`/`summary.md`/`text.txt` from a local user, not the 50 MB `paper.pdf`).
- `tests/setup_unittest_user.py` — (re)creates the persistent `unittest`/`unittest` fixture user
  in `data/users/`, qwen-backed, with the diffusion bubble + a seeded overview. Idempotent.
  Log in as it to repro by hand. (The live qwen chat test it once fed was removed with the chat.)

You can still smoke-test the HTTP layer with FastAPI's `TestClient` against a throwaway home.
The session cookie is now `Secure` by default, so any auth flow must use an **HTTPS base URL**
(or set `LOCKEDIN_INSECURE_COOKIE=1`) — otherwise the cookie isn't sent back and you get 401:

```bash
LOCKEDIN_HOME=/tmp/li uv run python -c "from fastapi.testclient import TestClient; \
from lockedin import server; c=TestClient(server.build_app(), base_url='https://testserver'); ..."
```

**When to run / extend the tests (policy for agents):** after any change that touches the
save pipeline (`bubbles.save_page`/wikilinks) or asset ingestion (`tagger`, `models.complete`),
or any change the user flags as **major** — or whenever the user explicitly asks. Add a
deterministic test in `test_editing_logic.py` for the specific behavior first (it's the
reproducible guard). A change is "major" if it alters a documented design decision or how
pages are saved/linked.

## Architecture

Layered; the server is thin HTTP glue over `service.py`.

| Module | Responsibility |
|--------|----------------|
| `paths.py` | All filesystem paths. A `contextvars` root pushed with `paths.use_root(home)` selects the active workspace for research content; account-registry paths resolve against the base root. |
| `auth.py` | PBKDF2-HMAC-SHA256, `accounts.yaml`, in-memory sessions (lost on restart). New accounts are approved immediately; the first is also admin and premium. `set_password`/`rename_user` back the account-settings endpoint; `is_admin`/`set_approved`/`delete_user` enforce administration rules; `MIN_PASSWORD_LEN=4`. |
| `sharing.py` | Global (base-root) `share_index.yaml` mapping an unlisted token → `{workspace_id, slug}` for the public `/share/<token>` routes (which run with no session). |
| `models.py` | **One active model per account** (`qwen`/`openai`/`claude`/`gemini`). Credentials remain account-private even in shared workspaces. Its only consumer is asset ingestion: `complete` (built on `stream_chat`) and `health_check`, via OpenAI-compatible providers (qwen via Ollama, OpenAI, Gemini) or Anthropic's SDK (Claude). |
| `assets.py` | PDF storage: `ASSETS/<pdf_id>/{paper.pdf,text.txt,summary.md,meta.yaml}`. Atomic writes. |
| `tagger.py` | Background ingest after upload: extract text, model metadata, and a cached summary. Fail-safe. |
| `bubbles.py` | Bubble registry (`bubbles.yaml`) + the per-bubble **mini-wiki**: pages manifest, page CRUD, image storage, legacy-`report.md` migration. |
| `todos.py` | **Workspace-wide TODOs** (GitHub-issue style), stored in `todos.yaml` (`next_id` + `todos` map). Pure storage: compact integer `id`, `title`, markdown `note`, `done`. CRUD only — it does **not** import `bubbles`; reference counting, delete guard, and report-reference rewrites after id compaction live in `service.py`. Referenced from report pages as `@<id>`. |
| `setup_tickets.py` | **One-shot bubble setup links** (the 🤖 button), in-memory only. Mints/serves/redeems the ticket behind `curl .../setup/<t>.sh \| bash`, and renders the bash and PowerShell it serves. |
| `presence.py` | **Live bubble presence**, in-memory only (like auth sessions, lost on restart). Viewers keyed by **username** (tabs collapse); Scientist workers keyed by the **project directory**'s stable `worker_uid` (agents in one directory collapse; two directories on one bubble stay two rows). Computes each worker's health from what the client reported plus what the server observed. |
| `reports.py` | The in-app usage guide (`APP_USAGE_GUIDE_SECTIONS` + `guide_section`), served by `/api/help`, the Scientist guide endpoint, and `lockedin editguide`. Nothing else lives here. |
| `service.py` | Orchestration; wraps ops in `use_root`. |
| `server.py` | FastAPI app + routes. |
| `web/index.html` | The entire SPA — vanilla JS IIFE, no build step, CDN deps. |

### Runtime data layout (all git-ignored under `data/`)

```
data/users/accounts.yaml            # account records, password hashes, Scientist tokens (chmod 600)
data/users/share_index.yaml         # {token: {workspace_id, slug}} — global public-share lookup
data/users/<username>/config/active_model.yaml  # account-private provider keys/models
data/workspaces/workspaces.yaml     # workspace records, membership, roles
data/workspaces/<workspace-id>/
  ASSETS/<pdf_id>/{paper.pdf,text.txt,summary.md,meta.yaml}
  REPORTS/<bubble_slug>/            # a mini-wiki per bubble
    pages.yaml                      # { home: <page_slug>, pages: [{page_slug,title}] }
    pages/<page-slug>.md
    assets/<figure>.png
  bubbles.yaml                      # {slug: {name, approved, instructions, created_at, share_active, share_token}}
  todos.yaml                        # {next_id, todos:{<id>:{id,title,note,done,created_at}}} — workspace-wide TODOs
```

## Key design decisions (don't regress these)

- **Markdown is the source of truth.** The frontend uses Toast UI Editor (CDN) but persists
  `.md`. The user writes/edits the reports themselves (in the editor or via the synchronized
  Scientist client). Don't switch to a JSON/block model.
- **There is no AI chat — the model only summarizes assets.** The product went through two
  retreats: first the AI page-editing contract (`<EDIT>`/`<NEWPAGE>`) was removed as unreliable,
  then the read-only research chat itself (pane, SSE endpoint, sessions, deep-read, Slack Q&A)
  was deleted as unused clutter. The single remaining model call path is asset ingestion:
  `tagger` → `models.complete` → provider (plus `models.health_check` for doctor/settings).
  If you ever reintroduce chat or AI editing, do it as a new, explicitly-gated feature — and
  note legacy workspaces may still carry orphaned `REPORTS/<slug>/chats/` dirs, which the
  Scientist sync must keep excluding. (`git log` has all the old machinery.)
- **Wikilink targets are normalized on save.** `bubbles.save_page` runs `normalize_wikilinks`:
  each `[[X]]` has any invented `prefix/` stripped, then resolves by slug, else by page **title**
  (case-insensitive), to the real slug. This is why the model is told to link by plain title —
  it can't guess server-assigned slugs, so it writes `[[Exact Title]]` and the save fixes it.
- **A byte-identical save must not move the page mtime, and normalization must round-trip to the
  Scientist client.** Every open browser polls `page_mtime` every 5s and re-renders on change, and
  a worker whose local copy differs from the stored bytes re-pushes every cycle. Two rules keep
  that stable: `save_page_state` skips the write when the normalized content equals what's on disk,
  and `scientist_sync.apply_writes` returns the stored bytes (`content_b64` in the applied item)
  whenever normalization changed a pushed page — the client adopts them so its copy converges.
  Breaking either re-creates the 24/7 re-push/re-render loop that made reading views jitter.
  Figures are served `private, no-cache` (revalidate through auth, 304 when unchanged) — never
  `no-store`, which forced a full re-download of every figure on each re-render.
- **Bubbles are created explicitly.** New bubbles are approved immediately and materialize their
  report pages on first use. The approval flag remains a compatibility and write-safety boundary
  for legacy or externally created records.
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
  username change updates the account record and active sessions. Research content is
  workspace-owned, while provider configuration remains under the account's private directory.
- **Summarize-once.** Every uploaded PDF is summarized once into `summary.md` during ingest;
  nothing re-reads the model afterwards except an explicit per-asset "resummarize".
- **Presence costs the worker no extra traffic, and is never persisted.** A Scientist worker
  identifies itself with `X-LockedIn-Worker{,-Label,-Status,-Error}` headers riding on the manifest
  poll it already makes every `POLL_SECONDS`; the server records it in `workspace_request_context`
  **after** the response, so a client rejected with 426 is still listed — diagnosed as out of date
  instead of vanishing. The identity is `worker_uid` in `.lockedin/config/identity.json`, minted
  once per project directory and deliberately kept **out of `binding.json`** (that file is compared
  for exact equality against `{server,user,workspace_id,bubble}`, so an extra key reads as a
  mismatch) and stable across worker restarts. This is why `resync` exists next to `hard-reset`:
  `resync` reads `binding.json`, pins the account to the workspace it names, and restarts the
  worker in place, so the directory keeps its `worker_uid` and its row on the bubble page, while
  `hard-reset` deletes `.lockedin/` — `identity.json` included — and the project comes back as a
  new row. `resync` must never touch the profile's device-global active workspace, and is
  dispatched before `choose_account()` so it cannot depend on which account was authorized last. Browsers heartbeat `POST /api/bubbles/<slug>/presence`
  every 20s, which both reports the viewer and returns the snapshot. The control is one
  `.presence-group-card` pill of three segments — 👥 people | ⚙ workers | 🤖 connect — and each
  half opens only its own half of the menu, so a click answers the question it was asked. It
  renders before the first heartbeat with zeroes rather than appearing a moment late, counts stay
  visible at 0, and an unhealthy worker colours **the workers segment**, never the whole pill
  (`--warn` is a muddy brown in the light themes and reads as damage when it rings everything). Nothing is written to disk:
  presence is a claim about *now*, so a restarted server correctly shows an empty bubble. Each
  worker row carries an `attention` flag (dead by failure or rejection, vs a clean "stopped"): the
  UI hides attention-free graves from the count and the menu, and the multi-directory sync
  situation renders as a small muted note (`presence-dupnote`), not a warning box — don't
  re-escalate either; routine endings and legitimate dual syncs are not alarms.
- **Report figures are flat, everywhere.** A bubble's figures live directly in
  `REPORTS/<slug>/assets/` with no subdirectories. This is forced by the serving route
  `/api/bubbles/{slug}/assets/{filename}` — a **single path segment**, so a nested figure can never
  be rendered. `save_bubble_image` stores flat, `scientist_sync.writable_path` accepts only a bare
  basename, and `scientist_sync._files` refuses to *publish* a nested figure (publishing one would
  hand a Scientist client a file it can never display, push back, or delete). The client's
  `ProjectSync.figure_warnings()` reports nested or non-slug-named figures instead of skipping them
  silently — a dropped figure is invisible data loss, since `push`/`deletes` answer HTTP 200 with a
  `conflicts` list rather than an error. `.tmp` is a reserved asset suffix: `apply_writes` stages
  through a dot-prefixed temp file and `_files` hides that suffix, so an asset actually named
  `*.tmp` is rejected on push rather than syncing up and then vanishing locally.
- **Unreferenced figures are surfaced, never collected.** `bubbles.list_bubble_assets` marks each
  file `unused` (no page links to it) and `servable`, and the Assets browser badges them. Nothing
  deletes them automatically — a figure is routinely uploaded before the page that uses it, and
  `delete_page` deliberately leaves figures alone.
- **A bubble's 🤖 link is a one-shot, in-memory bearer credential.** `POST
  /api/bubbles/<slug>/setup-link` mints a real Scientist token and parks it in `setup_tickets.py`
  under an unguessable id; `GET /setup/<t>.sh|.ps1` serves an *unauthenticated* script (the ticket
  is the credential) that installs the client and runs `lockedin-scientist connect`, which redeems
  the ticket once via `/api/scientist/v2/setup/<t>`. Nothing is persisted, so a restart invalidates
  every outstanding link — the same rule as presence and device codes. Three details are
  load-bearing: the served script runs `connect` with **`< /dev/tty`** when there is a terminal,
  because the script itself arrives on stdin from `curl` and the folder prompt would otherwise eat
  the rest of it — and falls back to `--project "$PWD"` when there is not, because an **agent** on
  a fresh cloud box has no controlling terminal and that is precisely where this link is the only
  thing that can install the client (the `/dev/tty` probe silences stderr *before* attempting the
  open, or bash logs a bogus "No such device" at it); `peek()`
  deliberately returns no token, so serving a script cannot leak one; and `connect` **pins the
  workspace into the project's binding rather than calling `workspaces switch`**, because that
  setting is device-global (see the `resync` note above). `connect` also reuses an existing
  account rather than re-running `login`, which would reset its `workspace_id` to personal.
- **Markup that cannot parse inside code is documentation, not an error.** The review
  (`\comment{id}{…}`) and text-colour (`\textcolor{c}{…}`) parsers exist twice —
  `parseCommentWrappers`/`parseTextColorWrappers` in `web/index.html` and
  `parse_comment_wrappers`/`parse_textcolor_wrappers` in `bubbles.py` — and both apply the same
  rule: if a token *fails to parse* and it begins inside a Markdown code span or fenced block,
  it is literal text and scanning continues. A **well-formed** wrapper is still a wrapper
  wherever it sits, so commenting on a code block keeps working and no stored page changes
  meaning; and broken markup in prose still raises with its line and column. Without this the
  in-app guide, which documents `\comment{<comment-id>}{…}` and renders through the very same
  pipeline as a report page, showed an error banner instead of two of its tabs — and no report
  page could explain the syntax and still be saved. The code-region scan
  (`markdownCodeRegions` / `_markdown_code_regions`) must stay identical in both languages, and
  runs lazily so the happy path never pays for it. `tests/review-parser.mjs` and
  `tests/textcolor-parser.mjs` pin the browser half; `DocumentedMarkupInCodeIsNotMarkup` in
  `tests/test_editing_logic.py` pins the server half and that every guide section parses.
- **One control surface for a bubble's tools, on every screen size.** Papers, the bubble's assets,
  the Overleaf link, preview/sharing, the Split/Edit/Read modes and Edit titles all live in the
  page toolbar's `⋮` menu (`buildBubbleToolsMenu`); the row itself keeps only `+` (page creation,
  far left, deliberately outside the scrolling tab list) and `⛶`. There is no phone-specific
  variant — the retired `.mobile-workspace-control` / `.desktop-only-tool` / floating
  `.mobile-bubble-actions` split left phones unable to create a page or switch view mode at all.
  The menu host is built **once per bubble** in `openBubble` (into `S.toolsMenu`) and re-homed by
  `refreshTabs`, which rebuilds that row on every page switch and every 5s poll — building it
  there instead would drop an open panel, and is how the old papers dropdown ended up a stale
  detached node. Its panel hangs inside `.pane`, which is `overflow:clip`, so it must keep a
  `max-height`, and its group must never be `overflow:hidden` — the panel is a descendant and
  would be clipped away. `⋮` and `⛶` are two halves of one `.tabrow-group` card, which carries the
  accent fill while both halves stay transparent; the `⋮` half is a button inside a positioning
  wrapper, so neutralising the base `button` background takes both selectors. **The card and `+`
  share one fill rule** (`.ptab-new,.tabrow-group`), defined once so the row cannot drift apart,
  and the panel is an
  accent-tinted lift of `--panel`. Colour alone cannot mark anything in that menu: the row
  re-declares `--accent` as a pale tint in four of the five themes, near enough to `--ink` that
  the active view mode was invisible — it wears a filled chip plus its ✓ instead. The trigger
  carries no state badge; whether the public link is live is read from the menu's own row.
- **One figure viewer, served to every rendered surface.** `web/lightbox.js` (route `/lightbox.js`,
  unauthenticated so public shares can load it) implements the full-screen click-to-zoom/pan viewer.
  The SPA loads it and calls `LockedInLightbox.watch("#previewWrap")` (covers Split and Read); the
  server-rendered preview/share page calls `watch("#content")`. It is one file precisely because the
  SPA and `_render_preview_html` are separate codebases whose duplicated browser logic has drifted
  before — don't reimplement it in either. `watch()` is delegated from `document`, so re-rendering a
  page never needs re-binding, and a figure wrapped in a link keeps its link instead of zooming.
- **Every SPA route carries its workspace.** The hash is `#w/<workspace_id>/<route>` —
  `#w/<ws>/bubble/<slug>/<page>`, `#w/<ws>/asset/<id>`, `#w/<ws>/bubbles`, and bare `#w/<ws>` for the
  library. `routePrefix`/`routeHref` build it and `pushRoute` applies it to every navigation;
  `applyHash` strips it before matching anything else. The selection is still mirrored into
  `localStorage` (`li_workspace`), but only as the default for a tab that opens *without* one:
  localStorage is shared by every tab, so a tab refreshed after another switched workspaces used to
  reload into the wrong workspace and 404 on its own bubble. The URL is the only per-tab memory a
  refresh survives, so it wins over storage — including in `boot()`, which must resolve it *before*
  the first `/api/me` so that request is asked in the right workspace. A prefix-less route still
  works (it means "whatever this tab has") and is upgraded in place; a workspace the user cannot
  resolve falls back to Personal and is dropped from the URL rather than bricking the tab.
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
- **No streaming endpoints remain.** The SSE worker-thread machinery (`_stream`/`_sse`) left
  with the chat. If a streamed endpoint ever returns, remember why it existed: a sync generator
  must not be resumed on a different threadpool thread or it loses the workspace `contextvars`
  root — run it in a dedicated thread feeding a queue.
- **Claude auth:** the Claude model uses an Anthropic API key stored in the account's model
  settings. Browser-pasted Claude OAuth/subscription tokens are not supported.
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
