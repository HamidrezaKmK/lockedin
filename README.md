# 🔒 lockedin

> A research assistant for grad students.

**lockedin** is a FastAPI application designed to help you keep up with research. You can upload papers, extract summaries, group them into "bubbles", maintain TODOs, math-aware Markdown reports, and chat with a switchable LLM backend, plus a Slackbot plugin that helps you keep track with your ideas and papers on your phone even when you cannot open up your laptop.

By default, the deployment is private and local-only (`127.0.0.1`). For web access, keep it bound to localhost and expose it through an HTTPS tunnel (e.g., Cloudflare Tunnel). Public exposure requires application-layer hardening (see [Security](#-security)).

---

## ✨ Features

- **👤 Per-User Workspaces:** Authenticated workspaces with username/password. The first account becomes the admin; subsequent sign-ups are pending until admin approval.
- **📄 Paper Management:** Upload papers, extract text, generate one-time summaries, and cache report context.
- **🏷️ Auto-Tagging:** Automatically group uploaded documents into contextual "bubbles".
- **📝 Markdown Reports:** Create multi-page Markdown reports featuring internal links, figures, KaTeX math support, and live previews.
- **✅ TODO Manager:** GitHub-issue-style task items. Reference a TODO in any report page using `@id` (creates a clickable link). Notes support math/markdown. TODOs can only be deleted once all `@id` references are removed. Manage via web or Slack bot.
- **💬 Research Chat:** Read-only chat grounded in your report pages and paper summaries.
- **🤖 Switchable LLMs:** Easily switch between local models (Qwen via Ollama) and cloud models (OpenAI, Claude, Gemini).
- **🔌 Optional Integrations:**
  - Slack bot (via Socket Mode)
  - Claude-powered News crawler for granted users
  - Unlisted read-only sharing links for specific bubbles

*(Note: The browser requires internet access for CDN-hosted frontend libraries, which are not vendored.)*

---

## 🚀 Setup Guide

These steps work from any clone path on a local machine or fresh server.

### 1. Prerequisites
- Python compatible with [`pyproject.toml`](pyproject.toml)
- [uv](https://docs.astral.sh/uv/)

*(Optional but recommended: [Ollama](https://ollama.com/) for local models, `cloudflared` for HTTPS tunnels, Slack App tokens for the bot.)*

### 2. Clone and Install

```bash
git clone <repo-url> lockedin
cd lockedin
uv sync
```

### 3. Set Up the Local Model
The default model is Qwen via Ollama. Install Ollama and pull the model:

```bash
ollama pull qwen2.5:7b-instruct
uv run lockedin doctor
```
*(If you prefer OpenAI, Claude, or Gemini, you can skip this and configure them in the web UI settings instead.)*

### 4. Run the Server
For interactive local use:

```bash
uv run lockedin serve
```
Open `http://127.0.0.1:8000/`. Sign up as the first user (becomes admin automatically), upload a paper, and start editing reports! Any subsequent sign-ups will stay pending until approved from **Settings → User access**.

---

## 🛠️ Advanced Setup

<details>
<summary><b>Persistent Systemd Services</b></summary>

Systemd ensures `lockedin` restarts on failure and survives logout.

```bash
cp ops/lockedin.env.example ops/lockedin.env
./ops/install-systemd-user.sh
systemctl --user enable --now lockedin-serve.service
systemctl --user enable --now lockedin-monitor.timer
```
Access at `http://127.0.0.1:8080/`.

**Useful Checks:**
```bash
systemctl --user status lockedin-serve.service
journalctl --user -u lockedin-serve.service -f
./ops/healthcheck.sh
```

**Boot Persistence:**
```bash
loginctl enable-linger "$USER"
```
More details: [docs/OPS.md](docs/OPS.md).
</details>

<details>
<summary><b>Temporary HTTPS URL (Cloudflare Tunnel)</b></summary>

For remote access without a domain:
```bash
uv run lockedin serve --host 127.0.0.1 --port 8080
cloudflared tunnel --url http://localhost:8080
```
Use the provided `https://<random>.trycloudflare.com` URL. (If using systemd, just run the `cloudflared` command).
</details>

<details>
<summary><b>Permanent Domain</b></summary>

Create a Cloudflare Tunnel public hostname routing to `localhost:8080`.

With systemd, save your tunnel token:
```bash
printf '%s\n' 'CLOUDFLARE_TUNNEL_TOKEN=<YOUR_TOKEN>' > ops/tunnel.env
```
Set the public URL in `ops/lockedin.env`:
```bash
LOCKEDIN_PUBLIC_URL=https://yourdomain.example/
```
Enable the tunnel service:
```bash
systemctl --user enable --now lockedin-tunnel.service
```
Full walkthrough: [docs/DOMAIN_SETUP.md](docs/DOMAIN_SETUP.md).
</details>

<details>
<summary><b>Slack Bot</b></summary>

Uses socket mode (no public URL needed). Add your Slack app tokens to `.env` or `ops/lockedin.env`.

**Manual:**
```bash
uv run lockedin slackbot
```

**Systemd:**
```bash
systemctl --user enable --now lockedin-slackbot.service
```
For persistent logins, use an HTTPS URL for `LOCKEDIN_URL` and share `LOCKEDIN_SLACK_SHARED_SECRET` between server and bot. 
Full setup: [docs/SLACKBOT_SETUP.md](docs/SLACKBOT_SETUP.md).
</details>

<details>
<summary><b>News Crawler</b></summary>

Opt-in feature using the host `claude` CLI.
```bash
uv run lockedin news-grant <username>
LOCKEDIN_NEWS_ENABLED=1 uv run lockedin serve
```
Revoke access with: `uv run lockedin news-revoke <username>`
</details>

<details>
<summary><b>Direct Report Editing With Agents (DEV Mode)</b></summary>

For hands-on editing via CLI agents without the web server.

```bash
./claude_scientist.sh
./agy_scientist.sh
./codex_scientist.sh
```
To resume a session:
```bash
./claude_scientist.sh resume
```
See [docs/DEV_MODE.md](docs/DEV_MODE.md).
</details>

---

## 🛡️ Security

`lockedin` is designed for trusted local use. If exposed publicly (even via HTTPS), consider these application-layer hardenings:
- Raise the minimum password length (currently 4 characters).
- Add rate limiting or lockout for `/api/login` and `/api/signup`.
- Disable self-signup entirely for a strictly private deployment.
- Audit public share links, upload/file-serving paths, and model-key handling.

---

## 📂 Data Layout

All per-user content lives under `data/users/<username>/` and is git-ignored:

```text
data/landing.yaml              # optional global public landing-page copy
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

## Public Landing Page Copy

The unauthenticated `/` landing page can be edited without rebuilding the app by creating
`data/landing.yaml`. The server reads this file at startup, so restart `lockedin-serve.service`
after edits. Missing fields fall back to the built-in defaults, so you can override only the
parts you want:

```yaml
hero:
  kicker_icon: "🔒"
  kicker: "private research workspace"
  title_accent: "locked"
  title_rest: "in"
  lede: "A calm command center for papers, math notes, topic wikis, research chat, and TODOs."
  copy: "Upload PDFs, organize them into bubbles, write reports, and keep model-powered help close."
  points:
    - title: "Paper-first"
      text: "Keep PDFs, tags, notes, summaries, and BibTeX together."

auth:
  title: "Enter your workspace"
  note: "Log in, or create an account."

workflow:
  title: "From paper pile to working theory"
  intro: "Collect sources, shape topic clusters, write technical notes, then chat/share/track."
  steps:
    - number: "01"
      title: "Upload papers"
      text: "Add PDFs or PDF links."

components:
  title: "The pieces that stay connected"
  intro: "Every view is built around repeated research work."
  features:
    - icon: "📚"
      title: "Assets"
      text: "Your paper inventory with uploads, filters, notes, tags, summaries, and BibTeX."

privacy:
  title: "Local and private-first by default"
  text: "Research notes stay close to the machine and model configuration you control."
  bullets:
    - "User data stays behind login."

footer: "Made for focused research sessions."
```

---

## 🏗️ Architecture

- `server.py`: HTTP/SSE glue over `service.py`.
- `paths.py`: Contextvar root for per-user isolation.
- `models.py`: Active-model interface across Ollama, OpenAI, Anthropic, and Gemini.
- `tagger.py`: Runs ingest tagging.
- `reports.py`: Stores and renders per-bubble Markdown pages.
- **Frontend:** Single `src/lockedin/web/index.html` app using CDN-hosted libraries.
