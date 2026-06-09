# lockedin

> A local-first research assistant for grad students.

`lockedin` is a FastAPI app for keeping up with research: upload papers and posts, group them
into idea bubbles, maintain math-aware Markdown reports, and chat with a switchable LLM backend.

The default deployment is private and local-only on `127.0.0.1`. If you want web access, keep the
app bound to localhost and expose it through HTTPS with a tunnel such as Cloudflare Tunnel.
Public exposure still needs application-layer hardening; see [Security](#security).

## What It Does

- Per-user workspaces with username/password auth. The first account is the admin; later
  sign-ups stay pending until an admin approves them in-app.
- Paper upload, extraction, one-time summaries, and cached report context.
- Auto-tagging into idea bubbles.
- Multi-page Markdown reports with internal links, figures, KaTeX, and live preview.
- Read-only research chat grounded in report pages and paper summaries.
- Model switcher for local Qwen through Ollama, OpenAI, Claude, and Gemini.
- Optional Slack bot in socket mode.
- Optional Claude-powered News crawler for granted users.
- Optional unlisted read-only sharing links for bubbles.

The browser needs internet access for CDN-hosted frontend libraries. They are not vendored.

## Setup Guide

These steps work from any clone path on a local machine or fresh server.

### 1. Clone And Install

```bash
git clone <repo-url> lockedin
cd lockedin
uv sync
```

Required:

- Python compatible with [pyproject.toml](pyproject.toml).
- [uv](https://docs.astral.sh/uv/).

Optional:

- [Ollama](https://ollama.com/) for the default local Qwen model.
- `cloudflared` for temporary or permanent HTTPS access.
- systemd user services for persistent operation.
- Slack app tokens for the bot.
- Claude CLI for the News crawler.

### 2. Set Up The Default Local Model

The default model is Qwen through Ollama. Install Ollama, then pull the model:

```bash
ollama pull qwen2.5:7b-instruct
uv run lockedin doctor
```

If you do not want local Qwen, you can still start the app and configure OpenAI, Claude, or Gemini
from the model settings in the web UI.

### 3. Choose How To Run The Server

For interactive local use:

```bash
uv run lockedin serve
```

Open `http://127.0.0.1:8000/`, sign up the first user, upload a paper, approve suggested tags,
and start editing reports. The first account you create becomes the admin and is approved
automatically; any later sign-ups stay pending until you approve them from **Settings → User
access**.

For a server that should keep running, use systemd instead of manually starting `uv run lockedin
serve`. The systemd setup below starts the same server on `127.0.0.1:8080`.

### 4. Optional: Persistent Systemd Services

Systemd is a deployment path, not an extra step after running the app manually. Use it when you
want lockedin to restart on failure and survive logout.

```bash
cp ops/lockedin.env.example ops/lockedin.env
./ops/install-systemd-user.sh
systemctl --user enable --now lockedin-serve.service
systemctl --user enable --now lockedin-monitor.timer
```

Then open `http://127.0.0.1:8080/`.

Useful checks:

```bash
systemctl --user status lockedin-serve.service
journalctl --user -u lockedin-serve.service -f
./ops/healthcheck.sh
```

For boot persistence after logout:

```bash
loginctl enable-linger "$USER"
```

More details: [docs/OPS.md](docs/OPS.md).

### 5. Optional: Temporary HTTPS URL

For ad-hoc remote access without buying or configuring a domain:

```bash
uv run lockedin serve --host 127.0.0.1 --port 8080
cloudflared tunnel --url http://localhost:8080
```

Use the printed `https://<random>.trycloudflare.com` URL while `cloudflared` is running.

If you are using systemd for the server, do not run the first command. Just start the temporary
tunnel against the already-running `http://localhost:8080`.

### 6. Optional: Permanent Domain

Create a Cloudflare Tunnel public hostname that routes your domain to the local service:

- Service type: `HTTP`
- URL: `localhost:8080`

With systemd, store the tunnel token privately:

```bash
printf '%s\n' 'CLOUDFLARE_TUNNEL_TOKEN=<YOUR_TOKEN>' > ops/tunnel.env
```

Set the public URL in `ops/lockedin.env` if you want the monitor to check it:

```bash
LOCKEDIN_PUBLIC_URL=https://yourdomain.example/
```

Then enable the tunnel service:

```bash
systemctl --user enable --now lockedin-tunnel.service
```

Full walkthrough: [docs/DOMAIN_SETUP.md](docs/DOMAIN_SETUP.md).

### 7. Optional: Slack Bot

The Slack bot uses socket mode, so it does not need a public URL. Create a Slack app, add bot
scopes and event subscriptions, then put the tokens in `.env` for manual use or
`ops/lockedin.env` for systemd.

Manual run:

```bash
uv run lockedin slackbot
```

Systemd run:

```bash
systemctl --user enable --now lockedin-slackbot.service
```

Because lockedin marks auth cookies `Secure` by default, prefer the HTTPS tunnel/domain URL:

```bash
LOCKEDIN_URL=https://yourdomain.example/
```

Plain `http://127.0.0.1:8080/` works only if the server also runs with
`LOCKEDIN_INSECURE_COOKIE=1`.

Full setup: [docs/SLACKBOT_SETUP.md](docs/SLACKBOT_SETUP.md).

### 8. Optional: News Crawler

The News crawler is opt-in and off by default. It uses the host `claude` CLI and spends your
Claude subscription/API capacity.

```bash
uv run lockedin news-grant <username>
LOCKEDIN_NEWS_ENABLED=1 uv run lockedin serve
```

For systemd, set `LOCKEDIN_NEWS_ENABLED=1` in `ops/lockedin.env` and restart:

```bash
systemctl --user restart lockedin-serve.service
```

Granted users see the News tab in the app. Revoke access with:

```bash
uv run lockedin news-revoke <username>
```

### 9. Optional: Direct Report Editing With Agents

For hands-on report editing without running the web server, use DEV mode. It authenticates
against your `.env` account credentials and scopes CLI agents to that user's report files.

See [docs/DEV_MODE.md](docs/DEV_MODE.md).

## Security

`lockedin` was designed for trusted local use. HTTPS tunnels protect transport, but public
exposure also needs application-layer hardening:

- Raise the minimum password length (currently 4 characters).
- Add rate limiting or lockout for `/api/login` and `/api/signup`.
- Sign-up is already gated by admin approval (new accounts are pending until approved), but
  consider also disabling self-signup entirely for a fully private deployment.
- Audit public share links, upload/file-serving paths, and model-key handling.

## Data Layout

All per-user content lives under `data/users/<username>/` and is git-ignored:

```text
data/users/<username>/
  config/active_model.yaml
  ASSETS/<pdf_id>/
    paper.pdf
    text.txt
    summary.md
    meta.yaml
  REPORTS/<bubble_slug>/
    pages.yaml
    pages/<page_slug>.md
  bubbles.yaml
```

## Architecture

`server.py` is HTTP/SSE glue over `service.py`. Per-user isolation uses a contextvar root in
`paths.py`. `models.py` exposes one active-model interface across Ollama, OpenAI, Anthropic, and
Gemini. `tagger.py` runs ingest tagging. `reports.py` stores and renders per-bubble Markdown
pages. The frontend is a single `src/lockedin/web/index.html` app using CDN-hosted libraries.
