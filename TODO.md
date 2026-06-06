

Goal: make this repo cloneable and deployable on a fresh server without relying on `/home/hamid`, `lockedin.codes`, or any machine-specific local state.

This is a portability/deployment cleanup. Do not refactor unrelated app logic.

## Scope

Fix the setup path so a new user can:

1. clone the repo anywhere,
2. run the local server,
3. optionally expose it through Cloudflare Tunnel,
4. optionally install systemd user services for persistent operation,
5. do all of that without editing hardcoded `/home/hamid/...` paths.

Do not vendor frontend CDN assets in this pass. Just document that the browser needs internet access for CDN-hosted frontend libraries.

## Important Current Problems

- `ops/healthcheck.sh` hardcodes:
- `/home/hamid/.local/bin`
- `/home/hamid/.nvm/versions/node/v22.22.3/bin`
- `/home/hamid/projects/lockedin`
- `https://lockedin.codes/`
- `ops/README.md` tells users to run `/home/hamid/projects/lockedin/ops/healthcheck.sh`.
- `ops/README.md` describes systemd units, but the repo does not ship actual `.service` or `.timer` files.
- README currently suggests `--host 0.0.0.0` / LAN access. We do not want to support or advertise LAN access. Supported modes should be:
- local-only on `127.0.0.1`
- public/web access through HTTPS, e.g. Cloudflare Tunnel
- Domain docs use `lockedin.codes` as a concrete example. Make docs generic.

## Tasks

### 1. Make `ops/healthcheck.sh` portable

Update `ops/healthcheck.sh` so it derives the repo root from the script location.

Expected approach:

```bash
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

Then use:

LOG="${LOCKEDIN_MONITOR_LOG:-$ROOT/ops/monitor.log}"
STATE="${LOCKEDIN_MONITOR_STATE:-$ROOT/ops/.state}"

Make URLs configurable:

LOCKEDIN_LOCAL_URL="${LOCKEDIN_LOCAL_URL:-http://127.0.0.1:8080/}"
LOCKEDIN_PUBLIC_URL="${LOCKEDIN_PUBLIC_URL:-}"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434/}"

Behavior:

- Always check local server via LOCKEDIN_LOCAL_URL.
- Only check public webpage/tunnel if LOCKEDIN_PUBLIC_URL is non-empty.
- If LOCKEDIN_PUBLIC_URL is empty, skip public URL check and log/summary as webpage=disabled or similar.
- Do not hardcode lockedin.codes.
- Do not hardcode /home/hamid.
- Use a generic PATH such as:

export PATH="${PATH:-/usr/local/bin:/usr/bin:/bin}:$HOME/.local/bin"

or preserve existing PATH and append common locations. Avoid NVM version-specific paths.

Make Claude diagnosis optional:

- If claude is not found, log that diagnosis is skipped.
- Do not let missing Claude CLI break the monitor.

### 2. Add ops env example

Add ops/lockedin.env.example.

It should include commented/default deployment knobs:

# Copy to ops/lockedin.env or another private env file used by systemd.
LOCKEDIN_PORT=8080
LOCKEDIN_LOCAL_URL=http://127.0.0.1:8080/
LOCKEDIN_PUBLIC_URL=https://yourdomain.example/
LOCKEDIN_NEWS_ENABLED=0

# Optional:
# OLLAMA_URL=http://127.0.0.1:11434/
# LOCKEDIN_MONITOR_LOG=/path/to/repo/ops/monitor.log
# LOCKEDIN_MONITOR_STATE=/path/to/repo/ops/.state

Keep real env files ignored. Check .gitignore and add entries if needed.

### 3. Add systemd user unit templates

Create tracked templates under something like:

ops/systemd/
lockedin-serve.service.template
lockedin-slackbot.service.template
lockedin-tunnel.service.template
lockedin-monitor.service.template
lockedin-monitor.timer

Templates should be portable and use placeholders such as:

__LOCKEDIN_ROOT__
__LOCKEDIN_ENV_FILE__
__LOCKEDIN_PORT__

Expected service behavior:

- lockedin-serve.service
- WorkingDirectory=__LOCKEDIN_ROOT__
- EnvironmentFile=__LOCKEDIN_ENV_FILE__
- ExecStart=uv run lockedin serve --host 127.0.0.1 --port ${LOCKEDIN_PORT}
- restart on failure

- lockedin-slackbot.service
- optional service
- WorkingDirectory=__LOCKEDIN_ROOT__
- EnvironmentFile=__LOCKEDIN_ENV_FILE__
- ExecStart=uv run lockedin slackbot

- lockedin-tunnel.service
- optional service
- use an env file for Cloudflare token or tunnel args
- do not commit real token

- lockedin-monitor.service
- runs __LOCKEDIN_ROOT__/ops/healthcheck.sh

- lockedin-monitor.timer
- runs every 2 minutes

If systemd variable expansion is awkward for port/env values, render them into the unit during install instead.

### 4. Add a systemd install script

Add ops/install-systemd-user.sh.

The script should:

1. determine repo root from its own location,
2. create ~/.config/systemd/user,
3. copy/render unit templates into that directory,
4. point units at the actual clone path,
5. create/copy an env file if missing, likely ops/lockedin.env,
6. run:

systemctl --user daemon-reload

7. print next commands rather than aggressively enabling everything without explanation.

Suggested printed commands:

systemctl --user enable --now lockedin-serve.service
systemctl --user enable --now lockedin-monitor.timer
systemctl --user enable --now lockedin-tunnel.service
systemctl --user enable --now lockedin-slackbot.service
loginctl enable-linger "$USER"

Only enable optional tunnel/slack services if their env requirements are configured, or clearly document that the user should enable them manually.

### 5. Update root README

Update README quick start:

- Remove uv run lockedin serve --host 0.0.0.0.
- Remove LAN-access language.
- Keep local default:

uv run lockedin serve

- For Cloudflare:

uv run lockedin serve --host 127.0.0.1 --port 8080
cloudflared tunnel --url http://localhost:8080

Add a “Fresh server install” section covering:

1. clone repo,
2. install uv,
3. uv sync,
4. optional Ollama setup for default Qwen,
5. uv run lockedin serve,
6. sign up first user,
7. optional Cloudflare setup,
8. optional systemd setup,
9. optional Slack bot,
10. optional News crawler / Claude CLI.

Clarify dependency categories:

- Required: Python compatible with pyproject.toml, uv.
- Required for default Qwen model: Ollama and qwen2.5:7b-instruct.
- Optional: Cloudflare Tunnel, systemd, Slack app tokens, Claude CLI for News.

Mention frontend CDN dependency briefly:

- The browser needs internet access to load CDN-hosted frontend libraries.
- Do not vendor assets in this pass.

### 6. Update ops/README.md

Rewrite it so it no longer describes only Hamid’s current machine.

It should explain:

- portable env file,
- install script,
- generated systemd user units,
- how to check status/logs,
- how to run healthcheck manually with ./ops/healthcheck.sh,
- how to configure LOCKEDIN_PUBLIC_URL,
- how to skip tunnel/public checks by leaving LOCKEDIN_PUBLIC_URL empty.

Remove /home/hamid/....

Avoid saying unit files are safe to commit unless they actually exist in the repo.

### 7. Update DOMAIN_SETUP.md

Make all examples generic:

- use yourdomain.example or yourdomain.codes,
- do not use lockedin.codes as the concrete configured domain,
- make it clear the Cloudflare route should point to localhost:8080 or whatever LOCKEDIN_PORT is.

Mention that if using ops monitor, set:

LOCKEDIN_PUBLIC_URL=https://yourdomain.example/

### 8. Expand .env.example

The current .env.example only covers dev mode. Expand it with commented optional sections:

# Dev mode
DEV_USERNAME=your-username
DEV_PASSWORD=your-password

# Server behavior
# LOCKEDIN_HOME=/absolute/path/for/data-root
# LOCKEDIN_NEWS_ENABLED=0
# LOCKEDIN_CORS_ORIGINS=

# Slack bot
# SLACK_BOT_TOKEN=
# SLACK_APP_TOKEN=
# LOCKEDIN_URL=http://localhost:8000
# OLLAMA_BASE_URL=http://localhost:11434/v1
# QWEN_MODEL=qwen2.5:7b-instruct

Do not include secrets.

## Acceptance Criteria

Run these checks before finishing:

git grep -n '/home/hamid' -- .
git grep -n 'lockedin.codes' -- .

Expected:

- no /home/hamid references in tracked files,
- no operational hardcoded lockedin.codes; generic examples are okay only if clearly placeholder-style, but prefer removing it entirely.

Also verify:

bash -n ops/healthcheck.sh
bash -n ops/install-systemd-user.sh

If possible, run:

uv run lockedin --help

Do not run destructive commands. Do not touch ignored local runtime files like:

.env
data/
ops/tunnel.env
ops/monitor.log
ops/.state/
.venv/
.claude/

## Non-goals

- Do not vendor frontend CDN assets.
- Do not harden public auth/signup in this pass.
- Do not redesign the app.
- Do not change model/provider behavior except docs/env examples.
- Do not commit real Cloudflare, Slack, Claude, OpenAI, Gemini, or user credentials.