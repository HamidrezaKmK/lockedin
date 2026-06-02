# 🔒 lockedin

> The ultimate research assistant for grad students.

A local-first FastAPI app to keep up with research in your field: upload papers/blogposts,
group them into **idea bubbles** (topic tags), and maintain AI-assisted, math-aware Markdown
reports per topic — with a chat sidebar and a switchable LLM backend.

## Features

- **Per-user workspaces** — username/password auth; each user gets `ASSETS/` and `REPORTS/`.
- **Global model switcher** — one active model at a time, switched from the top bar:
  local **Qwen** (Ollama, free), **OpenAI**, or **Claude**. Every task (tagging, summarizing,
  chat, editing) uses the active model.
- **Summarize-once** — each PDF is read in full once (cached `summary.md`); chat & report
  generation reuse the summary to stay fast. Deep-read a PDF on demand for detail.
- **Auto-tagging** — upload a PDF with no tags and Qwen suggests idea-bubble tags (reusing
  existing bubbles when they fit), flagging it in the **Attention Queue** for your review.
- **Gated reports** — approve a bubble, add instructions, then generate a template. The AI
  never writes reports unprompted.
- **Notion-like reports** — each bubble is a mini-wiki: **multiple markdown pages** (tabs),
  **internal links** between them (`[[page-slug]]`), and **drag/paste figures** (PNGs). Built on
  the [Toast UI Editor](https://ui.toast.com/tui-editor) (markdown + live preview) with **KaTeX**
  math. Markdown stays the source of truth.
- **Chat-driven editing** — a scope toggle under the chat (`💬 Chat` · `✏️ Edit entire page` ·
  `✏️ Edit a section`) routes your message to a normal reply or an AI edit. Edits are shown as an
  **accept/reject diff** before anything is saved.
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
