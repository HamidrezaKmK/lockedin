"""Minimal Slack bot for lockedin — per-user auth, PDF uploads, and model Q&A.

Launch: lockedin slackbot
Env vars: SLACK_BOT_TOKEN, SLACK_APP_TOKEN
Optional: LOCKEDIN_URL
"""
from __future__ import annotations

import json
import logging
import os  # used inside run() for env reads
import re

import httpx
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import assets

logger = logging.getLogger(__name__)

# Read lazily inside run() so .env is always loaded first.
URL    = ""
SLACK_SECRET = ""

_HELP = (
    "Commands:\n"
    "• `workspaces` / `switch workspace` — choose your active workspace\n"
    "• `select` — choose your active bubble\n"
    "• `list` — show all bubbles\n"
    "• `todos` — list your open TODOs and add / edit / complete / remove them\n"
    "• Attach a PDF — uploads it to your Library queue\n"
    "• Send a PDF link — downloads it into your Library queue\n"
    "• Anything else — asks your configured model about your active bubble"
)

# Per-Slack-user runtime state. The Slack→lockedin account link itself is persisted by the
# server after first login, so sessions can be refreshed after bot/server restarts.
_sessions:       dict[str, httpx.Client] = {}   # uid → logged-in HTTP client
_auth:           dict[str, str | None]   = {}   # uid → None (need username) | str (need password)
_usernames:      dict[str, str]          = {}   # uid → lockedin username for reauth prompts
_active_bubble:  dict[str, dict]         = {}   # uid → active bubble dict
_active_workspace: dict[str, dict]       = {}   # uid → active workspace dict
_selecting:      dict[str, list[dict]]   = {}   # uid → bubble list (awaiting number reply)
_workspace_selecting: dict[str, list[dict]] = {} # uid → workspace list (awaiting number reply)
_todo_flow:      dict[str, dict]         = {}   # uid → {stage, id, title, ...} (todos wizard)

_CONFIRM_RE = re.compile(r"^(ok|okay|yes|y|confirm|keep|default|same)$", re.IGNORECASE)
_CANCEL_RE = re.compile(r"^(cancel|exit|quit|no|stop)$", re.IGNORECASE)


def _slack_headers() -> dict[str, str]:
    return {"X-Lockedin-Slack-Secret": SLACK_SECRET} if SLACK_SECRET else {}


def _login(username: str, password: str, uid: str = "") -> httpx.Client:
    http = httpx.Client(timeout=60, follow_redirects=True)
    r = http.post(f"{URL}/api/login", json={"username": username, "password": password})
    r.raise_for_status()
    username = r.json().get("user") or username
    if uid:
        try:
            ws = http.get(f"{URL}/api/workspaces").json()
            personal = ws.get("personal_workspace_id", "")
            chosen = next((x for x in ws.get("workspaces", []) if x.get("id") == personal), None)
            if chosen:
                _active_workspace[uid] = chosen
                http.headers["X-LockedIn-Workspace"] = chosen["id"]
        except Exception:
            pass
    if uid and SLACK_SECRET:
        try:
            http.post(
                f"{URL}/api/slack/link",
                json={"slack_user_id": uid},
                headers=_slack_headers(),
            ).raise_for_status()
        except Exception as e:
            logger.warning("Could not persist Slack link for %s/%s: %s", uid, username, e)
    return http


def _linked_login(uid: str) -> httpx.Client | None:
    if not SLACK_SECRET:
        return None
    http = httpx.Client(timeout=60, follow_redirects=True)
    try:
        r = http.post(
            f"{URL}/api/slack/session",
            json={"slack_user_id": uid},
            headers=_slack_headers(),
        )
        if r.status_code == 404:
            http.close()
            return None
        r.raise_for_status()
        _usernames[uid] = r.json().get("user", "")
        ws = http.get(f"{URL}/api/workspaces").json()
        personal = ws.get("personal_workspace_id", "")
        chosen = next((x for x in ws.get("workspaces", []) if x.get("id") == personal), None)
        if chosen:
            _active_workspace[uid] = chosen
            http.headers["X-LockedIn-Workspace"] = chosen["id"]
        return http
    except Exception as e:
        logger.warning("Could not refresh Slack-linked session for %s: %s", uid, e)
        http.close()
        return None


def _is_unauthorized(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401


def _forget_session(uid: str) -> None:
    old = _sessions.pop(uid, None)
    if old:
        old.close()
    _active_bubble.pop(uid, None)
    _active_workspace.pop(uid, None)
    _selecting.pop(uid, None)
    _workspace_selecting.pop(uid, None)
    _todo_flow.pop(uid, None)


def _ask_reauth(uid: str, say) -> None:
    _forget_session(uid)
    linked = _linked_login(uid)
    if linked is not None:
        _sessions[uid] = linked
        _auth.pop(uid, None)
        say("Your lockedin session was refreshed. Please retry that last message.")
        return
    if uid in _usernames:
        _auth[uid] = _usernames[uid]
        say("Your lockedin session expired. Send your password to log in again, then retry.")
    else:
        _auth[uid] = None
        say("Your lockedin session expired. Send your username to log in again, then retry.")


def _sole_url(text: str) -> str | None:
    """If the whole message is a single link, return it — else None.

    Slack renders bare links as ``<https://x>`` or ``<https://x|label>``, so unwrap that
    form too. Mirrors the web chat's bare-link → PDF-attach behavior.
    """
    t = text.strip()
    m = re.fullmatch(r"<(https?://[^>|]+)(?:\|[^>]*)?>", t)
    if m:
        return m.group(1)
    return t if re.fullmatch(r"https?://\S+", t) else None


def _ask_model(http: httpx.Client, slug: str, question: str) -> str:
    r = http.post(f"{URL}/api/slack/ask", json={"slug": slug, "question": question})
    r.raise_for_status()
    return (r.json().get("answer") or "").strip()


def _news_list(http: httpx.Client, say) -> None:
    """List every retrieved news item, grouped by bubble, with the reason it's relevant."""
    try:
        r = http.get(f"{URL}/api/news")
        if r.status_code == 403:
            say("📰 News isn't enabled for your account — it's a premium feature.")
            return
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        say(f"Couldn't load news: {e}")
        return
    items = data.get("items") or []
    if not items:
        say("📰 No news yet. Send `crawl` to search for new papers.")
        return
    names = {b["slug"]: b["name"] for b in (data.get("bubbles") or [])}
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(it.get("bubble_slug") or "", []).append(it)
    blocks = []
    for slug in sorted(groups, key=lambda s: (s == "", (names.get(s, s) or "").lower())):
        label = (names.get(slug, slug) if slug else "Other / unmatched")
        lines = [f"*🫧 {label}*"]
        for it in groups[slug]:
            title = it.get("title") or it.get("url") or "(untitled)"
            url = it.get("url") or ""
            line = f"• <{url}|{title}>" if url else f"• {title}"
            if it.get("reason"):
                line += f" — {it['reason']}"
            meta = " · ".join(x for x in (it.get("source"), it.get("published")) if x)
            if meta:
                line += f"  _({meta})_"
            lines.append(line)
        blocks.append("\n".join(lines))
    say(f"📰 *{len(items)} news item(s):*\n\n" + "\n\n".join(blocks))


def _news_crawl(http: httpx.Client, say, *, since: str | None = None,
                until: str | None = None, message: str = "") -> None:
    """Run one crawl turn (server-side Claude Code agent) and report the papers it found.

    First turn: ``message=""`` + a ``since``/``until`` range. Follow-ups: ``message=<steer text>``
    (the range is fixed by the open session, so it's omitted on resume)."""
    say("🔎 Working… this can take a minute or two." if not message else f"💬 _{message}_ …")
    found: list[dict] = []
    done: dict | None = None
    body: dict = {"message": message}
    if since:
        body["since"] = since
    if until:
        body["until"] = until
    try:
        with http.stream("POST", f"{URL}/api/news/chat", json=body,
                         timeout=httpx.Timeout(600.0)) as r:
            if r.status_code == 403:
                say("News isn't enabled for your account — it's a premium feature.")
                return
            if r.status_code == 503:
                say("The news crawler is currently disabled by the operator.")
                return
            r.raise_for_status()
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except Exception:
                    continue
                t = ev.get("type")
                if t == "item":
                    found.append(ev["item"])
                elif t == "done":
                    done = ev
                elif t == "error":
                    say(f"Crawl error: {ev.get('detail')}")
                    return
    except Exception as e:
        say(f"Crawl failed: {e}")
        return

    d = done or {}
    if d.get("accepted"):
        say(d.get("text") or "✅ Saved.")
        return
    if not found:
        stopped = d.get("stopped")
        say("Done — found no new papers in range" +
            (f" (stopped: {stopped}; send `continue` to keep going)." if stopped else "."))
        return
    lines = [f"*🔎 Found {len(found)} new paper(s):*"]
    for it in found:
        title = it.get("title") or it.get("url") or "(untitled)"
        url = it.get("url") or ""
        head = f"• <{url}|{title}>" if url else f"• {title}"
        if it.get("reason"):
            head += f" — {it['reason']}"
        lines.append(head)
    tail = "\n\nSend `continue` for more, a refinement to steer, `accept` to save, or `stop` to leave."
    if d.get("stopped") in ("timeout", "max_turns"):
        tail += " _(stopped early — there may be more.)_"
    if d.get("cost_usd"):
        tail += f" _(~${d['cost_usd']:.4f} this turn)_"
    say("\n".join(lines) + tail)


# ── todos: a minimal add/edit/done/remove wizard (done items are managed in the web app) ──────
_TODO_MENU = ("Reply `add`, `done <id>`, `edit <id>`, `remove <id>`, or `exit`. "
              "_(Reopen or browse completed TODOs in the web app.)_")


def _todo_list_and_menu(http, say, uid: str) -> None:
    """List the user's OPEN todos and arm the menu. Done todos are intentionally hidden here."""
    try:
        r = http.get(f"{URL}/api/todos"); r.raise_for_status()
        todos = r.json()["todos"]
    except Exception as e:
        if _is_unauthorized(e):
            _ask_reauth(uid, say)
            return
        say(f"Couldn't load TODOs: {e}")
        return
    open_todos = [t for t in todos if not t.get("done")]
    if open_todos:
        lines = [f"#{t['id']}  {t['title']}" + (f"  🔗{t['ref_count']}" if t.get("ref_count") else "")
                 for t in open_todos]
        say("*Your open TODOs:*\n" + "\n".join(lines) + "\n\n" + _TODO_MENU)
    else:
        say("You have no open TODOs. Reply `add` to create one, or `exit`. "
            "_(Completed TODOs live in the web app.)_")
    _todo_flow[uid] = {"stage": "menu"}


def _todo_menu_action(http, say, uid: str, text: str) -> None:
    """Handle a reply while the todo menu is armed."""
    low = text.strip().lower()
    if low == "add":
        _todo_flow[uid] = {"stage": "add_title"}
        say("Title for the new TODO?")
        return
    m = re.match(r"^(done|edit|remove|rm|delete|del)\s+#?(\d+)$", low)
    if not m:
        say("Didn't catch that. " + _TODO_MENU)
        return
    action, tid = m.group(1), int(m.group(2))
    try:
        if action == "done":
            http.patch(f"{URL}/api/todos/{tid}", json={"done": True}).raise_for_status()
            say(f"✅ @{tid} marked done.")
            _todo_flow.pop(uid, None)
        elif action == "edit":
            r = http.get(f"{URL}/api/todos/{tid}"); r.raise_for_status()
            todo = r.json()["todo"]
            _todo_flow[uid] = {"stage": "edit_title", "id": tid}
            say(f"Editing @{tid} — \"{todo['title']}\".\nNew title? (send text, or `ok` to keep it)")
        else:  # remove / rm / delete / del
            resp = http.delete(f"{URL}/api/todos/{tid}")
            if resp.status_code == 409:
                say(f"Can't remove @{tid}: {resp.json().get('detail', 'it is still referenced.')}\n"
                    "Pick another, or `exit`.")
                return  # keep the menu armed
            resp.raise_for_status()
            say(f"🗑 Removed @{tid}.")
            _todo_flow.pop(uid, None)
    except Exception as e:
        if _is_unauthorized(e):
            _ask_reauth(uid, say)
            _todo_flow.pop(uid, None)
            return
        say(f"That didn't work: {e}")


def _todo_flow_step(http, say, uid: str, text: str) -> None:
    """Drive the multi-turn add/edit sub-stages."""
    flow = _todo_flow[uid]
    if _CANCEL_RE.match(text):
        _todo_flow.pop(uid, None)
        say("Left the TODO manager.")
        return
    stage = flow["stage"]
    if stage == "menu":
        _todo_menu_action(http, say, uid, text)
        return
    if stage == "add_title":
        flow["title"] = text.strip()
        flow["stage"] = "add_note"
        say("A note? (send text — supports the same math/markdown as reports — or `ok` to skip)")
        return
    if stage == "add_note":
        note = "" if _CONFIRM_RE.match(text) else text
        try:
            r = http.post(f"{URL}/api/todos", json={"title": flow["title"], "note": note})
            r.raise_for_status()
            say(f"Added @{r.json()['todo']['id']} — \"{flow['title']}\". Send `todos` for the list.")
        except Exception as e:
            if _is_unauthorized(e):
                _ask_reauth(uid, say)
            else:
                say(f"Couldn't add: {e}")
        _todo_flow.pop(uid, None)
        return
    if stage == "edit_title":
        if not _CONFIRM_RE.match(text):
            flow["new_title"] = text.strip()
        flow["stage"] = "edit_note"
        say("New note? (send text, or `ok` to keep the current one)")
        return
    if stage == "edit_note":
        payload: dict = {}
        if "new_title" in flow:
            payload["title"] = flow["new_title"]
        if not _CONFIRM_RE.match(text):
            payload["note"] = text
        try:
            if payload:
                http.patch(f"{URL}/api/todos/{flow['id']}", json=payload).raise_for_status()
            say(f"Updated @{flow['id']}.")
        except Exception as e:
            if _is_unauthorized(e):
                _ask_reauth(uid, say)
            else:
                say(f"Couldn't update: {e}")
        _todo_flow.pop(uid, None)
        return
    _todo_flow.pop(uid, None)  # unknown stage — reset


def handle(event: dict, say) -> None:
    uid   = event.get("user") or ""
    text  = re.sub(r"<@[^>]+>", "", event.get("text") or "").strip()
    files = event.get("files") or []

    # ── auth flow ─────────────────────────────────────────────────────────────
    if uid not in _sessions:
        linked = _linked_login(uid)
        if linked is not None:
            _sessions[uid] = linked
            _auth.pop(uid, None)
        else:
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
                _sessions[uid] = _login(username, text, uid)
                _usernames[uid] = username
                del _auth[uid]
                say(f"Logged in as *{username}*.\n" + _HELP)
            except Exception:
                _auth[uid] = None
                say("Login failed. Send your username to try again.")
            return

    # ── normal operation ──────────────────────────────────────────────────────
    http = _sessions[uid]

    if uid in _workspace_selecting:
        rows = _workspace_selecting[uid]
        try:
            chosen = rows[int(text) - 1]
        except (ValueError, IndexError):
            say("Reply with a workspace number, or `cancel`.")
            return
        _active_workspace[uid] = chosen
        http.headers["X-LockedIn-Workspace"] = chosen["id"]
        _active_bubble.pop(uid, None); _todo_flow.pop(uid, None); _selecting.pop(uid, None)
        _workspace_selecting.pop(uid, None)
        say(f"Active workspace: *{chosen['name']}*.")
        return

    if re.match(r"^(workspaces|switch workspace)$", text, re.IGNORECASE):
        try:
            rows = http.get(f"{URL}/api/workspaces").raise_for_status().json().get("workspaces", [])
        except Exception as e:
            say(f"Couldn't load workspaces: {e}"); return
        active = _active_workspace.get(uid, {}).get("id")
        say("Your workspaces:\n" + "\n".join(f"{i+1}. *{w['name']}* ({w['role']})" + (" ← active" if w['id']==active else "") for i,w in enumerate(rows)) + "\n\nReply with a number to switch.")
        _workspace_selecting[uid] = rows
        return

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
            if _is_unauthorized(e):
                _ask_reauth(uid, say)
                return
            say(f"Upload failed: {e}")
    if files and not text:
        return

    # ── bare PDF link → download + add to assets queue ────────────────────────
    link = _sole_url(text)
    if link:
        try:
            pdf = assets.fetch_pdf_from_url(link)
        except Exception as e:
            say(f"Couldn't fetch that link: {e}")
            return
        if pdf is not None:
            data, name = pdf
            try:
                http.post(
                    f"{URL}/api/assets/upload",
                    files={"file": (name, data, "application/pdf")},
                    data={"url_source": link},
                ).raise_for_status()
                say(f"📎 Added *{name}* to your assets — it's in the attention queue for tagging.")
            except Exception as e:
                if _is_unauthorized(e):
                    _ask_reauth(uid, say)
                    return
                say(f"Upload failed: {e}")
            return
        # reachable but not a PDF → fall through to normal Q&A handling

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

    # ── crawl wizard: collect the from/to date range, then run ────────────────
    if False:  # retired feature; retained temporarily only to avoid disrupting old running workers
        flow = _news_flow[uid]
        if _CANCEL_RE.match(text):
            del _news_flow[uid]
            say("Crawl cancelled.")
            return
        is_ok, is_date = _CONFIRM_RE.match(text), _DATE_RE.match(text)
        if flow["stage"] == "from":
            if is_date:
                flow["since"] = text.strip()
            elif not is_ok:
                say(f"Reply with a date like `2026-06-01`, or `ok` to keep `{flow['since']}`. "
                    "`cancel` to abort.")
                return
            flow["stage"] = "to"
            say(f"From = `{flow['since']}` ✅\nNow the *to* date? Default `{flow['until']}` (today). "
                "Reply with a date, or `ok` to keep it.")
            return
        # stage == "to"
        if is_date:
            flow["until"] = text.strip()
        elif not is_ok:
            say(f"Reply with a date like `2026-06-03`, or `ok` to keep `{flow['until']}`.")
            return
        since, until = flow["since"], flow["until"]
        del _news_flow[uid]
        say(f"Crawling `{since}` → `{until}` …")
        _news_crawl(http, say, since=since, until=until)
        _news_steer.add(uid)
        return

    # ── crawl steering: free-text follow-ups drive the open crawl session ─────
    if False:
        if _STEER_STOP_RE.match(text):
            _news_steer.discard(uid)
            say("Left the crawl. Your session stays open — send `crawl` to resume, or use the web app.")
            return
        if _STEER_ACCEPT_RE.match(text):
            try:
                res = http.post(f"{URL}/api/news/accept")
                res.raise_for_status()
                say(f"✅ Saved — moved your date pointer to `{res.json().get('pointer', 'today')}`.")
            except Exception as e:
                say(f"Couldn't save: {e}")
            _news_steer.discard(uid)
            return
        _news_crawl(http, say, message=text)
        return

    # ── todos wizard: drive add/edit/done/remove sub-stages ───────────────────
    if uid in _todo_flow:
        _todo_flow_step(http, say, uid, text)
        return

    # ── todos: list open TODOs + arm the add/edit/done/remove menu ────────────
    if re.match(r"^todos?$", text, re.IGNORECASE):
        _todo_list_and_menu(http, say, uid)
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
        say("Your bubbles:\n" + "\n".join(lines) + "\n\nReply with a number to select.")
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

    # ── news: list retrieved items + why they're relevant (premium) ───────────
    if False:
        _news_list(http, say)
        return

    # ── crawl: start the date-range wizard (or resume an open session) ────────
    if False:
        try:
            r = http.get(f"{URL}/api/news/status")
            if r.status_code == 403:
                say("News isn't enabled for your account — it's a premium feature.")
                return
            r.raise_for_status()
            st = r.json()
        except Exception as e:
            say(f"Couldn't start crawl: {e}")
            return
        sess = st.get("session")
        if sess and sess.get("running"):
            _news_steer.add(uid)
            say("A crawl is already running for your account — give it a moment, then send a "
                "follow-up to steer it (or `stop` to leave).")
            return
        if sess:
            _news_steer.add(uid)
            say("You have an open crawl session. Send `continue` for more, a refinement to steer "
                "(e.g. `focus on diffusion`), `accept` to save & advance your date pointer, or `stop`.")
            return
        _news_flow[uid] = {"stage": "from", "since": st.get("default_since", ""),
                           "until": st.get("today", "")}
        say(f"🔎 *Crawl setup.* From which date should I look?\n"
            f"Default: `{_news_flow[uid]['since']}`.\n"
            "Reply with a date (`YYYY-MM-DD`), or `ok` to keep it. `cancel` to abort.")
        return

    # ── help ──────────────────────────────────────────────────────────────────
    if re.match(r"^help$", text, re.IGNORECASE):
        say(_HELP)
        return

    # ── question to active bubble ─────────────────────────────────────────────
    if text:
        bubble = _active_bubble.get(uid)
        if not bubble:
            say("I don't have an active bubble for questions yet. Here are your options:\n\n" + _HELP)
            return
        say(f"_(asking your configured model about *{bubble['name']}*…)_")
        try:
            answer = _ask_model(http, bubble["slug"], text)
            say(answer or "No answer returned.")
        except Exception as e:
            # A stale login (e.g. the server restarted, wiping its in-memory sessions) surfaces
            # as a 401 here — prompt a re-login instead of a confusing model error.
            if _is_unauthorized(e):
                _ask_reauth(uid, say)
                return
            say(f"Model error: {e}")


def run(*, slack_bot_token: str, slack_app_token: str) -> None:
    global URL, SLACK_SECRET
    URL    = os.environ.get("LOCKEDIN_URL",    "http://localhost:8000").rstrip("/")
    SLACK_SECRET = os.environ.get("LOCKEDIN_SLACK_SHARED_SECRET") or slack_bot_token

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
