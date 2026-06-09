# Slackbot Setup

Works with your **existing Slack workspace** — you just add a new App to it.
No public URL needed (uses socket mode).

---

## 1. Create a Slack App in your workspace

1. Go to **https://api.slack.com/apps** → **Create New App** → **From scratch**
2. Name it `lockedin`, pick **your existing workspace**, click **Create App**

---

## 2. Get your two tokens

**App-Level Token (for socket mode)**
- Left sidebar → **Socket Mode** → Enable it
- Click **Generate** → name it anything, scope: `connections:write` → **Generate**
- Copy the `xapp-...` token → `SLACK_APP_TOKEN`

**Bot Token**
- Left sidebar → **OAuth & Permissions** → **Bot Token Scopes** → Add:
  `app_mentions:read`, `chat:write`, `files:read`, `im:history`, `im:read`, `im:write`
- Scroll up → **Install to Workspace** → Allow
- Copy the `xoxb-...` token → `SLACK_BOT_TOKEN`

---

## 3. Subscribe to events

- Left sidebar → **Event Subscriptions** → Enable → **Subscribe to bot events**:
  - `message.im`
  - `app_mention`
- Save Changes

---

## 4. Set env vars and run manually

Add to your project-root `.env` file (create it if it doesn't exist):

```bash
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...

# Optional overrides. Prefer an HTTPS URL unless the server has
# LOCKEDIN_INSECURE_COOKIE=1.
# LOCKEDIN_URL=https://yourdomain.example/
# OLLAMA_BASE_URL=http://localhost:11434/v1
# QWEN_MODEL=qwen2.5:7b-instruct
```

Then run:

```bash
uv run lockedin slackbot
```

If the web server is running through systemd on port `8080`, put the same values in
`ops/lockedin.env`. The server marks cookies `Secure` by default, so use your HTTPS tunnel URL
for `LOCKEDIN_URL`. Only use a plain local HTTP URL if you also set
`LOCKEDIN_INSECURE_COOKIE=1` for the server.

```bash
LOCKEDIN_URL=https://yourdomain.example/
LOCKEDIN_SLACKBOT_ENABLED=1
```

Then start the persistent service:

```bash
systemctl --user enable --now lockedin-slackbot.service
```

---

## What the bot can do

DM the bot (or @-mention it in a channel). On first contact it asks for your lockedin username
and password — after that it remembers your session. The lockedin account you log in with must be
**approved** (see the approval flow in the README), or login fails. Sessions live in memory, so
they reset when the bot **or** the web server restarts; the bot will simply ask you to log in
again on your next message.

| Message | Action |
|---|---|
| *(first message)* | Bot asks for username, then password |
| Attach a PDF / send a PDF link | Uploads it to your Assets queue |
| `select` (or `switch`) | Lists your bubbles, reply with a number to set the active one |
| `list` | Lists your idea bubbles |
| `news` | Lists crawled news items + why each is relevant (premium) |
| `crawl` | Searches the web for new papers for your bubbles (premium) |
| `help` | Shows the command list |
| anything else | Qwen answers using your **active** bubble's content (`select` one first) |

Qwen must be running locally unless you change the bot model settings:
`ollama serve && ollama pull qwen2.5:7b-instruct`
