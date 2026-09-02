# 🔒 lockedin

> A research assistant for grad students.

**lockedin** is a FastAPI application designed to help you keep up with research. You can upload papers, extract summaries, group them into "bubbles", maintain TODOs and math-aware Markdown reports (with a switchable LLM backend that summarizes your uploads), plus a Slackbot plugin that helps you keep track with your ideas and papers on your phone even when you cannot open up your laptop.

By default, the deployment is private and local-only (`127.0.0.1`). For web access, keep it bound to localhost and expose it through an HTTPS tunnel (e.g., Cloudflare Tunnel). Public exposure requires application-layer hardening (see [Security](#-security)).

---

## ✨ Features

- **📄 Paper Management:** Upload papers, extract text, and generate one-time summaries.
- **🏷️ Bubble Organization:** Group uploaded documents into contextual topic bubbles, with per-paper relevance scores.
- **📝 Markdown Reports:** Create multi-page Markdown reports featuring internal links, figures, KaTeX math support, and live previews.
- **✅ TODO Manager:** GitHub-issue-style task items. Reference a TODO in any report page using `@id` (creates a clickable link). Notes support math/markdown. TODOs can only be deleted once all `@id` references are removed. Manage via web or Slack bot.
- **🤖 Switchable LLMs:** Easily switch between local models (Qwen via Ollama) and cloud models (OpenAI, Claude, Gemini).
- **🔌 Optional Integrations:**
  - Slack bot (via Socket Mode)
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

### 3. Choose a Model
The app defaults to OpenAI until an account configures a provider. To use local Qwen, install
Ollama and pull the model:

```bash
ollama serve
ollama pull qwen2.5:7b-instruct
```
*(Configure OpenAI, Claude, or Gemini in the web UI Settings. Qwen is available to premium accounts.)*

### 4. Run the Server
For interactive local use:

```bash
uv run lockedin serve
```
Open `http://127.0.0.1:8000/`. The first account becomes an admin and has premium access; later accounts can sign in immediately and may request premium access from an admin. Upload a paper, organize it into a bubble, and start editing reports.

---

## 🛠️ Advanced Setup

<details>
<summary><b>Installed Scientist CLI</b></summary>

The optional, dependency-free `lockedin-scientist` client synchronizes one approved bubble into a
project-local `.lockedin/` directory. It does **not** install or launch Codex, Claude, agy, or the
LockedIn server. Install its small native bootstrap skill for the agent you use, then start that
agent normally in the project.
Python 3.11+ is required.

macOS/Linux:
```bash
curl -fsSL https://raw.githubusercontent.com/HamidrezaKmK/lockedin/main/install.sh | bash
```

Windows PowerShell:
```powershell
irm https://raw.githubusercontent.com/HamidrezaKmK/lockedin/main/install.ps1 | iex
```

The fastest path is the **🤖** button on a bubble page: pick your OS, copy the one line it shows,
and paste it into a terminal. It installs the client, authorizes it without a browser step, asks
which folder to use, binds the bubble, and installs the skill for whichever agents you have. The
link is single-use and expires in ten minutes.

To do the same by hand, authorize and synchronize a bubble from the project where you want the files:
```bash
lockedin-scientist login --server https://lockedin.codes
lockedin-scientist workspaces
lockedin-scientist workspaces switch <workspace-id-or-name>
lockedin-scientist bubbles
lockedin-scientist sync <bubble-slug>
```

Install the native agent integration once on this computer. It is named `lockedin-scientist` and
always reads the full, bubble-specific guide generated at `.lockedin/SKILL.md`:
```bash
lockedin-scientist codex setup
lockedin-scientist claude setup
lockedin-scientist agy setup
```
In Codex, invoke `$lockedin-scientist`; in Claude Code, invoke `/lockedin-scientist`; in agy,
use `/skills` to select `lockedin-scientist`. Run the respective setup again to update only the
managed bootstrap skill; it will refuse to overwrite a user-owned skill with the same name.

The global profile retains your login and active workspace across projects. `sync` creates a
single bubble-bound `.lockedin/` directory and starts one background worker for that project. It
pulls paper assets/configuration read-only and publishes only report pages and report assets from
inside `.lockedin/`; it does not limit an agent's normal access to the rest of the project.
`.lockedin/` is added to the repository's local Git exclude file, not its tracked `.gitignore`.

Assets over 25 MB are listed but never carried by the sync in either direction — the manifest is
re-read on every poll, so hashing a multi-gigabyte archive would cost more than the whole rest of
the bubble. Move them on request:
```bash
lockedin-scientist assets                    # what is large, and which way it needs to move
lockedin-scientist assets pull <filename>    # bring one down   (--all for every one)
lockedin-scientist assets push <filename>    # send one up      (--all for every one)
```
Both directions slice the transfer, so any size fits through a proxy that caps request bodies, and
both resume after an interruption. Operators can move the line with `LOCKEDIN_SYNC_MAX_ASSET_BYTES`.

Manage workers or rebuild a project from the server:
```bash
lockedin-scientist ps
lockedin-scientist doctor
lockedin-scientist resync
lockedin-scientist stop <worker-id>
lockedin-scientist hard-reset <bubble-slug>
```
`resync` resumes the bubble a project is already bound to — no bubble slug and no workspace switch,
because `.lockedin/` records both. It is the normal way to restart a stopped worker.
`stop` preserves `.lockedin/`. `hard-reset` stops that project worker, replaces `.lockedin/` with
the current selected bubble, then starts a new worker. Concurrent report edits are revision-guarded:
the server version is restored locally and the rejected local copy plus a patch are retained in
`.lockedin/config/conflicts/`. A bubble can optionally link one Overleaf Cloud project from its
website header. Once that link has synchronized, use `lockedin-scientist overleaf connect` to
clone it into `.lockedin/overleaf/`. Reports are the continuously synchronized research record;
the Overleaf checkout is the curated publication manuscript and is never changed by the worker.
Agents may edit its LaTeX, bibliography, figures, and other ordinary project files, but
`lockedin-scientist overleaf sync` is always an explicit, foreground publish step.

```bash
lockedin-scientist overleaf help
lockedin-scientist overleaf connect
lockedin-scientist overleaf status
lockedin-scientist overleaf sync
lockedin-scientist overleaf abort
lockedin-scientist overleaf disconnect
```

On the first Git prompt, enter username `git` and your Overleaf Git token. When no OS keychain
helper is installed, Scientist configures one owner-only credential store for your user, scoped
in Git to `git.overleaf.com`; subsequent LockedIn projects reuse it. It is not stored in the
repository. If a sync needs manual recovery, use `git status`, fetch/rebase the configured
`lockedin-overleaf` remote branch, resolve conflicts, and push manually. `hard-reset` preserves a
connected Overleaf checkout unless you explicitly add `--discard-overleaf`.

Open private report reviews are projected read-only into `.lockedin/config/reviews.yaml`. An
attached review maps to the exact source wrapped by `\comment{<comment-id>}{...}`. The generated
skill tells agents to preserve that server-owned wrapper, read the full conversation, make the
smallest in-scope edit inside it, and ask about vague feedback. Agents must never fabricate,
reinsert, move, reply to, resolve, or delete review markers. Run `lockedin-scientist doctor` before
claiming a report edit synchronized; it verifies that this project has a reachable healthy worker.

If `lockedin-scientist ps` reports a **failed** worker because `binding.json` is missing, Scientist
will show the recovery command. First copy any unsynchronized report work out of `.lockedin/`, then
run `lockedin-scientist hard-reset <bubble-slug>` from that project. This deliberately rebuilds the
directory from the server instead of guessing which bubble a damaged local directory belongs to.

Scientist checks its compatible client version whenever it contacts the synchronized workspace.
If it asks you to reinstall, rerun the installer for your platform above; it replaces only the
standalone client command and keeps your authorization and projects intact.
</details>

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

## 🛡️ Security

`lockedin` is designed for trusted local use. If exposed publicly (even via HTTPS), consider these application-layer hardenings:
- Raise the minimum password length (currently 4 characters).
- Add rate limiting or lockout for `/api/login` and `/api/signup`.
- Disable self-signup entirely for a strictly private deployment.
- Audit public share links, upload/file-serving paths, and model-key handling.

---

## 📂 Data Layout

All runtime data is git-ignored under `data/`. Research content belongs to a workspace; account credentials and model settings remain private to the account:

```text
data/landing.yaml              # optional global public landing-page copy
data/users/
  accounts.yaml                # account records, password hashes, Scientist tokens
  share_index.yaml             # public-share token index
  <username>/
    config/active_model.yaml   # account-private model configuration and API keys
data/workspaces/
  workspaces.yaml              # workspace membership and roles
  <workspace-id>/
    config/math.yaml
    config/aesthetics.yaml
    ASSETS/<pdf_id>/
      paper.pdf
      text.txt
      summary.md
      meta.yaml
    REPORTS/<bubble_slug>/
      pages.yaml
      pages/<page_slug>.md
    bubbles.yaml
    todos.yaml
```

## Public Landing Page Copy

The unauthenticated `/` landing page can be edited without rebuilding the app by creating
`data/landing.yaml`. The server reads this file on each request, so a browser refresh shows
changes without restarting the service. Missing fields fall back to the built-in defaults, so you
can override only the parts you want:

```yaml
hero:
  kicker_icon: "🔒"
  kicker: "private research workspace"
  title_accent: "locked"
  title_rest: "in"
  lede: "A calm command center for papers, math notes, topic wikis, and TODOs."
  copy: "Upload PDFs, organize them into bubbles, write reports, and share them."
  points:
    - title: "Paper-first"
      text: "Keep PDFs, tags, notes, summaries, and BibTeX together."

scientist:
  title: "Bring Scientist to your computer"
  intro: "Optional local companion for your coding CLI."
  platforms:
    - title: "macOS or Linux"
      text: "Python 3.11+ required."
      command: "curl -fsSL https://example/install.sh | bash"
  steps:
    - title: "Authorize"
      text: "Sign in once in your browser."
      command: "lockedin-scientist login --server https://lockedin.codes"

auth:
  title: "Enter your workspace"
  note: "Log in, or create an account."

workflow:
  title: "From paper pile to working theory"
  intro: "Collect sources, shape topic clusters, write technical notes, then share and track."
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
- `paths.py`: Contextvar root for the active workspace.
- `models.py`: Active-model interface across Ollama, OpenAI, Anthropic, and Gemini.
- `tagger.py`: Extracts text and model metadata, then caches an ingest summary.
- `reports.py`: Stores and renders per-bubble Markdown pages.
- **Frontend:** Single `src/lockedin/web/index.html` app using CDN-hosted libraries.
