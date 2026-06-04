# 🔒 lockedin

> The ultimate research assistant for grad students.

A local-first FastAPI app to keep up with research in your field: upload papers/blogposts,
group them into **idea bubbles** (topic tags), and maintain AI-assisted, math-aware Markdown
reports per topic — with a chat sidebar and a switchable LLM backend.

## Features

- **Per-user workspaces** — username/password auth; each user gets `ASSETS/` and `REPORTS/`.
- **Global model switcher** — one active model at a time, switched from the top bar:
  local **Qwen** (Ollama, free), **OpenAI**, **Claude**, or **Gemini**. Every task (tagging,
  summarizing, chat) uses the active model.
- **Summarize-once** — each PDF is read in full once (cached `summary.md`); the chat reuses the
  summary to stay fast. Deep-read a PDF on demand for full-text detail.
- **Auto-tagging** — upload a PDF with no tags and Qwen suggests idea-bubble tags (reusing
  existing bubbles when they fit), flagging it in the **Attention Queue** for your review.
- **Notion-like reports** — each bubble is a mini-wiki you write yourself: **multiple markdown
  pages** (tabs), **internal links** between them (`[[page-slug]]`), and **drag/paste figures**
  (PNGs). Built on the [Toast UI Editor](https://ui.toast.com/tui-editor) (markdown + live
  preview) with **KaTeX** math. Markdown stays the source of truth.
- **Read-only research chat** — the chat sidebar knows the full text of your report pages, a
  summary of every tagged paper, and the full text of any **deep-read** papers you attach. Ask
  questions, explain math, compare papers, brainstorm. It does **not** edit your reports — you
  do that in the editor (or with a strong model in DEV_MODE).
- **News feed (premium)** — authorized users write plain-English monitoring instructions
  ("monitor arXiv cs.LG", "watch <person>'s blog") and **chat with a Claude Code crawl agent**
  that browses the web and streams in relevant papers as it finds them, grouped per bubble in a
  dismissible feed. Say "continue" for more, steer it ("focus on diffusion"), pick a model + date
  range, and hit **✓ I'm happy** to save and advance your date pointer. Times out gracefully
  (keeps what it found). Runs entirely inside the server — no separate process.
  See [enabling it](#news-feed-premium-1).
- **Share a bubble** — flip on an unlisted, read-only link for any bubble and send it to a
  friend or manager (no login needed). They get a rendered preview of all its pages; headings
  carry 🔗 anchors so you can deep-link to a specific section. Toggle it off anytime.
- **Account settings** — change your username or password from the top bar (username changes
  carry your whole workspace over).
- **Standalone bubbles** — create a bubble/report for a nascent idea with no papers yet.

## Quick start

This project is managed with [uv](https://docs.astral.sh/uv/).

```bash
uv sync                       # create .venv and install everything from pyproject.toml/uv.lock

# default model is local Qwen via Ollama:
ollama serve
ollama pull qwen2.5:7b-instruct

uv run lockedin serve                      # -> http://127.0.0.1:8000
uv run lockedin serve --port 8080          # custom port
uv run lockedin serve --host 0.0.0.0      # bind to all interfaces (LAN access)
uv run lockedin doctor                     # check the active model is reachable
```

(Or activate the env once with `source .venv/bin/activate`, then call `lockedin …` directly.)

Sign up, then:
1. **Assets** → upload a PDF (leave tags blank to auto-tag).
2. **Attention** → approve the suggested tags.
3. **Idea Bubbles** → open a bubble, **Generate** a report, edit it, and chat in the sidebar.
4. Top bar → switch to **OpenAI**/**Claude** (click ⚙ to enter an API key).

## News feed (premium)

The News crawler is opt-in and off by default (it spends *your* Claude tokens via the host
`claude` CLI). To enable it:

```bash
# 1. Log the host `claude` CLI into your Claude subscription (one-time): `claude` then sign in.
# 2. Authorize a user (writes news_enabled into accounts.yaml):
uv run lockedin news-grant <username>
# 3. Start the server with the news switch on:
LOCKEDIN_NEWS_ENABLED=1 uv run lockedin serve
```

Granted users now see a **📰 News** tab, split into a paper feed (top) and a crawl chat
(bottom). They add monitoring instructions (⚙), pick a model + date range, and chat with the
agent ("crawl cs.LG for my bubbles"); papers stream into the feed as they're found. Say
**continue** for more, steer it in plain English, then **✓ I'm happy** to save and move the date
pointer. Matching is driven by a per-bubble **scope summary** (auto-built from each bubble's
report + papers, refreshed when it changes), not just titles. Past crawl conversations are saved
and viewable under **🕘 History**. Revoke with `uv run lockedin news-revoke <username>`.

Notes: the switch is read from the **server's** environment, so set `LOCKEDIN_NEWS_ENABLED=1`
on the `serve` command (it isn't loaded from `.env`). On a Claude Max subscription, crawling is
flat-rate — the cost estimate is an approximate API-metered figure for comparison.

## Share a temporary public URL

To let someone reach your local instance over HTTPS, use a Cloudflare quick tunnel
([`cloudflared`](https://github.com/cloudflare/cloudflared)) — no domain or account needed:

```bash
uv run lockedin serve --port 8080                  # terminal 1: the app
cloudflared tunnel --url http://localhost:8080 2>&1 | grep --line-buffered trycloudflare.com
terminal 2: prints a public https URL
```

It prints a `https://<random>.trycloudflare.com` URL (near the **top** of the output). It's live
as long as `cloudflared` runs and changes each restart — fine for ad-hoc sharing. The app stays
bound to localhost; only the tunnel reaches it.

For hands-on editing without running the server at all, see [DEV_MODE.md](DEV_MODE.md).

## Layout

All per-user content lives under `data/users/<username>/` and is **git-ignored**:

```
data/users/<username>/
  config/active_model.yaml   # active provider + API keys
  ASSETS/<pdf_id>/           # paper.pdf, text.txt, summary.md, meta.yaml
  REPORTS/<bubble_slug>/report.md
  bubbles.yaml               # bubble registry (approval state, instructions)
```

## Architecture

`server.py` is thin HTTP/SSE glue over `service.py`; per-user isolation uses a contextvar
root (`paths.use_root`). `models.py` exposes one active-model interface (`stream_chat`,
`complete`, `attach_pdf`) across Ollama/OpenAI/Anthropic. `tagger.py` runs the
extract→summarize→suggest-tags ingest as a background task. `reports.py` streams report
generation and section/selection edits. The frontend is a single dependency-free
`web/index.html` (marked.js + KaTeX from CDN).

> Lightweight auth for local/LAN use — put it behind HTTPS before exposing it.
