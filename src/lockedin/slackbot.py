"""Minimal Slack bot for lockedin — per-user auth, PDF uploads, Qwen Q&A.

Launch: lockedin slackbot
Env vars: SLACK_BOT_TOKEN, SLACK_APP_TOKEN
Optional: LOCKEDIN_URL, QWEN_MODEL, OLLAMA_BASE_URL
"""
from __future__ import annotations

import logging
import os  # used inside run() for env reads
import re

import httpx
from openai import OpenAI
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

logger = logging.getLogger(__name__)

# Read lazily inside run() so .env is always loaded first.
URL    = ""
QWEN   = ""
OLLAMA = ""

_HELP = (
    "Commands:\n"
    "• `select` — choose your active idea bubble\n"
    "• `list` — show all bubbles\n"
    "• Attach a PDF — uploads it to your assets queue\n"
    "• Anything else — asks Qwen about your active bubble"
)

# Per-Slack-user state (in-memory; resets on bot restart)
_sessions:       dict[str, httpx.Client] = {}   # uid → logged-in HTTP client
_auth:           dict[str, str | None]   = {}   # uid → None (need username) | str (need password)
_active_bubble:  dict[str, dict]         = {}   # uid → active bubble dict
_selecting:      dict[str, list[dict]]   = {}   # uid → bubble list (awaiting number reply)


def _login(username: str, password: str) -> httpx.Client:
    http = httpx.Client(timeout=60, follow_redirects=True)
    http.post(f"{URL}/api/login", json={"username": username, "password": password}).raise_for_status()
    return http


def _ask_qwen(question: str, context: str) -> str:
    resp = OpenAI(base_url=OLLAMA, api_key="ollama").chat.completions.create(
        model=QWEN,
        messages=[
            {"role": "system", "content":
                "Answer questions using the provided research context. "
                "Be concise (2-4 sentences). Plain text only — no markdown."},
            {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"},
        ],
        max_tokens=600,
    )
    return resp.choices[0].message.content.strip()


def _context(http: httpx.Client, slug: str) -> str:
    bubble = http.get(f"{URL}/api/bubbles/{slug}").raise_for_status().json()["bubble"]
    parts: list[str] = []
    for p in (bubble.get("pages") or [])[:6]:
        try:
            c = http.get(
                f"{URL}/api/bubbles/{slug}/pages/{p['page_slug']}"
            ).raise_for_status().json()["content"]
            if c.strip():
                parts.append(f"[{p['title']}]\n{c[:2500]}")
        except Exception:
            pass
    for a in (bubble.get("assets") or [])[:5]:
        try:
            s = http.get(
                f"{URL}/api/assets/{a['pdf_id']}/summary"
            ).raise_for_status().json()["summary"]
            if s.strip():
                parts.append(f"[Paper: {a.get('title') or a['pdf_id']}]\n{s[:1200]}")
        except Exception:
            pass
    return "\n\n".join(parts) or "No content yet."


def handle(event: dict, say) -> None:
    uid   = event.get("user") or ""
    text  = re.sub(r"<@[^>]+>", "", event.get("text") or "").strip()
    files = event.get("files") or []

    # ── auth flow ─────────────────────────────────────────────────────────────
    if uid not in _sessions:
        if uid not in _auth:
            _auth[uid] = None
            say("Welcome to lockedin! What's your username?")
            return
        if _auth[uid] is None:
            _auth[uid] = text
            say("Got it. What's your password?")
            return
        username = _auth[uid]
        try:
            _sessions[uid] = _login(username, text)
            del _auth[uid]
            say(f"Logged in as *{username}*.\n" + _HELP)
        except Exception:
            _auth[uid] = None
            say("Login failed. Send your username to try again.")
        return

    # ── normal operation ──────────────────────────────────────────────────────
    http = _sessions[uid]

    # upload attached PDFs (checked before anything else)
    for f in files:
        if f.get("mimetype") != "application/pdf":
            continue
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        try:
            data = http.get(
                url, headers={"Authorization": f"Bearer {event.get('_bot_token', '')}"}
            ).content
            http.post(
                f"{URL}/api/assets/upload",
                files={"file": (f.get("name", "upload.pdf"), data, "application/pdf")},
            ).raise_for_status()
            say(f"Uploaded *{f.get('name', 'file')}* — auto-tagging in background.")
        except Exception as e:
            say(f"Upload failed: {e}")
    if files and not text:
        return

    # ── waiting for bubble number selection ───────────────────────────────────
    if uid in _selecting:
        bubbles = _selecting[uid]
        if re.match(r"^(cancel|exit|quit|no)$", text, re.IGNORECASE):
            del _selecting[uid]
            say("Selection cancelled.")
            return
        try:
            idx = int(text.strip()) - 1
            if 0 <= idx < len(bubbles):
                _active_bubble[uid] = bubbles[idx]
                del _selecting[uid]
                say(f"Active bubble: *{bubbles[idx]['name']}*. Ask me anything!")
            else:
                say(f"Please enter a number between 1 and {len(bubbles)}, or `cancel`.")
        except ValueError:
            say(f"Please enter a number (1–{len(bubbles)}), or `cancel`.")
        return

    # ── select / switch ───────────────────────────────────────────────────────
    if re.match(r"^(select|switch)(\s+bubble)?$", text, re.IGNORECASE):
        bs = http.get(f"{URL}/api/bubbles").raise_for_status().json()["bubbles"]
        if not bs:
            say("No bubbles yet. Upload and tag some PDFs first.")
            return
        current = _active_bubble.get(uid)
        lines = [
            f"{i+1}. *{b['name']}* ({b['pdf_count']} paper(s))"
            + (" ← active" if current and b["slug"] == current["slug"] else "")
            for i, b in enumerate(bs)
        ]
        say("Your idea bubbles:\n" + "\n".join(lines) + "\n\nReply with a number to select.")
        _selecting[uid] = bs
        return

    # ── list ──────────────────────────────────────────────────────────────────
    if re.match(r"^list$", text, re.IGNORECASE):
        bs = http.get(f"{URL}/api/bubbles").raise_for_status().json()["bubbles"]
        if not bs:
            say("No bubbles yet.")
            return
        current = _active_bubble.get(uid)
        say("\n".join(
            f"• *{b['name']}*{' ← active' if current and b['slug'] == current['slug'] else ''}"
            f" ({b['pdf_count']} paper(s))"
            for b in bs
        ))
        return

    # ── help ──────────────────────────────────────────────────────────────────
    if re.match(r"^help$", text, re.IGNORECASE):
        say(_HELP)
        return

    # ── question to active bubble ─────────────────────────────────────────────
    if text:
        bubble = _active_bubble.get(uid)
        if not bubble:
            say("No active bubble. Type `select` to pick one.")
            return
        say(f"_(asking Qwen about *{bubble['name']}*…)_")
        try:
            say(_ask_qwen(text, _context(http, bubble["slug"])))
        except Exception as e:
            say(f"Qwen error: {e}")


def run(*, slack_bot_token: str, slack_app_token: str) -> None:
    global URL, QWEN, OLLAMA
    URL    = os.environ.get("LOCKEDIN_URL",    "http://localhost:8000").rstrip("/")
    QWEN   = os.environ.get("QWEN_MODEL",      "qwen2.5:7b-instruct")
    OLLAMA = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")

    app = App(token=slack_bot_token)

    def _wrap(event, say):
        if event.get("bot_id"):
            return
        subtype = event.get("subtype")
        if subtype and subtype != "file_share":
            return
        event["_bot_token"] = slack_bot_token
        handle(event, say)

    @app.event("message")
    def on_dm(event, say):
        if event.get("channel_type") == "im":
            _wrap(event, say)

    @app.event("app_mention")
    def on_mention(event, say):
        _wrap(event, say)

    logger.info("lockedin bot running …")
    SocketModeHandler(app, slack_app_token).start()
