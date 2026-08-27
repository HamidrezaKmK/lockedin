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

# Shown in the app's Help → Slackbot tab so people can join and DM the bot.
# SLACKBOT_INVITE_LINK=https://join.slack.com/t/your-workspace/shared_invite/...
```

`SLACKBOT_INVITE_LINK` is read by the **web server**, not the bot, so it belongs in
`ops/lockedin.env` too if you run the server under systemd — and the server needs a restart to
pick up a change. **Slack shared invites expire** (30 days, or after a set number of uses, unless
you create one that never expires). When the Help tab's link starts answering *"this link is no
longer active"*, generate a fresh one in Slack and replace the value; nothing in lockedin can
detect the expiry for you.

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
and password — after that your Slack user is linked to that lockedin account. The bot can refresh
its session from that link after bot or web-server restarts, so you only need to log in again if
you change your lockedin username or password.

For those persistent links, the web server and Slack bot must share
`LOCKEDIN_SLACK_SHARED_SECRET`. If you do not set it, both processes can use the same
`SLACK_BOT_TOKEN` instead. The bot never stores your lockedin password.

| Message | Action |
|---|---|
| *(first message)* | Bot asks for username, then password |
| Attach a PDF / send a PDF link | Uploads it to your Library queue |
| `workspaces` (or `switch workspace`) | Lists your workspaces, reply with a number to set the active one |
| `select` (or `switch`) | Lists your bubbles, reply with a number to set the active one |
| `list` | Lists your bubbles |
| `todos` | Lists your open TODOs, then add / edit / complete / remove them |
| `help` | Shows the command list |
| anything else | Shows the command list — the bot has no chat, it only manages assets |
