"""Premium interactive "news" crawl chat.

A per-user list of free-form *monitoring instructions* (e.g. "monitor arXiv cs.LG", "watch
<person>'s blog") is crawled **conversationally** by the operator's Claude Code agent: we drive
the host ``claude`` CLI headlessly in streaming mode (``-p --output-format stream-json``) with
read-only web tools. The user chats with the agent in the web UI — it streams its activity and
emits each relevant paper the instant it finds one (so a timeout keeps what it already got).
"continue" resumes the same agent session (``--resume``); the date cursor only advances when the
user explicitly accepts.

The agent talks to the server through a one-line protocol: for each confirmed paper it prints a
line ``@@ITEM {json}`` (json = ``{bubble,title,url,source,published,reason}``); everything else is
normal prose. The server persists each item immediately (dedup) and strips those lines from the
displayed chat. All persistence / dedup / bubble-mapping happens here; the agent never writes
files or runs shell.

Gated three ways: a global kill switch (env ``LOCKEDIN_NEWS_ENABLED``, default OFF), a per-account
``news_enabled`` flag (see :mod:`auth`), and per-turn bounds (model / ``--max-turns`` / timeout).

Two per-user files (atomic YAML, like :mod:`bubbles`):

* ``config/news.yaml``   — user-editable instructions (each with its own ``last_checked``);
* ``news_items.yaml``    — crawler-owned items + a ``seen`` dedup set + the active ``session``.

Both resolve against the active per-user context root, so callers wrap in ``paths.use_root``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

import yaml
from slugify import slugify

from . import assets, bubbles, models, paths

logger = logging.getLogger(__name__)

COLD_START_DAYS = 7  # first crawl of a new instruction backfills this many days
ITEM_PREFIX = "@@ITEM "  # one-line protocol the agent uses to emit a found paper

# A typed message that means "I'm satisfied — save and advance the date cursor".
ACCEPT_RE = re.compile(
    r"\b(i'?m happy|looks good|that'?s enough|that is enough|good enough|i'?m done|"
    r"we'?re done|save( it| this| them)?|accept( it| this| them)?|perfect|all done)\b", re.I)


@dataclass
class CrawlConfig:
    """Per-turn safety bounds. Subscription/Max plan → flat cost, so we bound work, not dollars."""
    model: str = "claude-haiku-4-5"
    max_turns: int = 25       # agent tool-loop cycles per turn
    timeout: int = 300        # seconds; the subprocess is killed past this (partial items survive)
    max_items: int = 30       # asked of the agent per turn


DEFAULTS = CrawlConfig()

# Model choices offered in the UI before a crawl. ``est_usd`` is a ROUGH API-metered estimate for
# one turn — shown so users can compare. On a Claude Max subscription, crawling is flat-rate.
MODELS = [
    {"id": "claude-haiku-4-5", "label": "Haiku — fast & cheap", "est_usd": 0.30},
    {"id": "claude-sonnet-4-6", "label": "Sonnet — balanced", "est_usd": 0.90},
    {"id": "claude-opus-4-8", "label": "Opus — most thorough", "est_usd": 4.50},
]


def model_options() -> list[dict]:
    return [dict(m) for m in MODELS]


_TRUTHY = {"1", "true", "yes", "on"}

SYSTEM_PROMPT = (
    "You are an interactive research-news crawler for an academic user, talking with them in a "
    "chat. Use ONLY the WebSearch and WebFetch tools to find genuinely NEW publications/posts "
    "that match the user's monitoring instructions AND are relevant to one of their bubbles. "
    "Prefer precision over recall.\n"
    "HARD DATE RULE: a crawl date range [SINCE .. UNTIL] is given. Include an item ONLY if its "
    "publication/last-updated date falls within that range (strictly after SINCE, up to and "
    "including UNTIL). If you cannot positively confirm an item's date is in range, DO NOT include "
    "it. Never include older papers or standing/old submissions — they bloat the feed. When in "
    "doubt, leave it out.\n"
    "PROTOCOL: the MOMENT you confirm an in-range, relevant item, output it on its OWN line as:\n"
    '@@ITEM {"bubble":"<idea-bubble name>","title":"...","url":"https://...",'
    '"source":"<which instruction>","published":"YYYY-MM-DD","reason":"one sentence"}\n'
    "Emit one @@ITEM line per paper, as you go. Everything else you say is normal chat prose. Do "
    "NOT repeat an item you already emitted earlier in this conversation.\n"
    "CRITICAL: a paper reaches the user's feed ONLY via its @@ITEM line. NEVER mention, list, or "
    "count a paper in prose unless you have ALSO emitted its @@ITEM line — if you tell the user you "
    "found N papers, there MUST be N @@ITEM lines. To be safe, ALWAYS end your turn with a final "
    'fenced block ```json\\n{"items":[ ...every in-range item you found this turn... ]}\\n``` '
    "(same fields as @@ITEM). It's fine that this repeats the @@ITEM lines — the app de-duplicates.\n"
    "BE ECONOMICAL: for a listing page (e.g. an arXiv /list/ URL) fetch it ONCE and judge from the "
    "titles/abstracts there; only open an individual page for a borderline case. When you stop, "
    "tell the user how many in-range items you found and whether there may be more. If the user "
    "says they're satisfied, just confirm — the app handles saving."
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _today() -> str:
    return _now().date().isoformat()


def _cold_start() -> str:
    return (_now() - timedelta(days=COLD_START_DAYS)).date().isoformat()


def _default_since() -> str:
    """Default crawl-range start: the global pointer (advanced each time you accept a crawl), or a
    cold-start window if you've never accepted one. The instructions themselves carry no date."""
    return (get_pointer() or "")[:10] or _cold_start()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def news_globally_enabled() -> bool:
    """Master kill switch. Defaults OFF — nothing crawls until the operator opts in."""
    return os.environ.get("LOCKEDIN_NEWS_ENABLED", "").strip().lower() in _TRUTHY


# --------------------------------------------------------------------------- #
# Instruction store (config/news.yaml) — user-editable
# --------------------------------------------------------------------------- #
# Schema: { last_checked: <date|null>, instructions: [{id, text, enabled, created_at}] }
# Instructions are PLAIN TEXT — they carry no date. The single global ``last_checked`` pointer
# (advanced on accept) seeds the default crawl range; the chat's range is the source of truth.
def _load_config() -> dict:
    path = paths.NEWS_CONFIG_YAML
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _save_config(cfg: dict) -> None:
    _atomic_write(paths.NEWS_CONFIG_YAML,
                  yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))


def load_instructions() -> list[dict]:
    return list(_load_config().get("instructions", []))


def save_instructions(items: list[dict]) -> None:
    cfg = _load_config()
    cfg["instructions"] = items
    _save_config(cfg)


def get_pointer() -> Optional[str]:
    return _load_config().get("last_checked")


def set_pointer(date: Optional[str]) -> None:
    cfg = _load_config()
    cfg["last_checked"] = (date or None)
    _save_config(cfg)


def set_instructions(new_entries: list[dict]) -> list[dict]:
    """Replace the instruction list (plain text only). Preserves ``created_at`` by id."""
    existing = {i["id"]: i for i in load_instructions() if i.get("id")}
    out: list[dict] = []
    for e in new_entries:
        text = (e.get("text") or "").strip()
        if not text:
            continue
        prev = existing.get(e.get("id")) or {}
        out.append({
            "id": e.get("id") or secrets.token_hex(4),
            "text": text,
            "enabled": bool(e.get("enabled", True)),
            "created_at": prev.get("created_at") or _now_iso(),
        })
    save_instructions(out)
    return out


# --------------------------------------------------------------------------- #
# Item + session store (news_items.yaml) — crawler-owned
# --------------------------------------------------------------------------- #
def load_items() -> dict:
    path = paths.NEWS_ITEMS_YAML
    if not path.exists():
        return {"items": {}, "seen": [], "session": None}
    data = yaml.safe_load(path.read_text()) or {}
    data.setdefault("items", {})
    data.setdefault("seen", [])
    data.setdefault("session", None)
    return data


def save_items(data: dict) -> None:
    _atomic_write(paths.NEWS_ITEMS_YAML,
                  yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def list_items(include_dismissed: bool = False) -> list[dict]:
    data = load_items()
    out = [{"id": k, **v} for k, v in data["items"].items()]
    if not include_dismissed:
        out = [i for i in out if i.get("state") != "dismissed"]
    out.sort(key=lambda i: (i.get("found_at", ""), i.get("published", "")), reverse=True)
    return out


def dismiss_item(item_id: str) -> bool:
    data = load_items()
    it = data["items"].get(item_id)
    if not it:
        return False
    it["state"] = "dismissed"  # stays in `seen`, so a re-crawl never resurfaces it
    save_items(data)
    return True


def _dedup_key(url: str, title: str) -> str:
    u = (url or "").strip().lower()
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([0-9]+\.[0-9]+)", u)
    if m:
        return "arxiv:" + m.group(1)
    u = re.sub(r"[#?].*$", "", u).rstrip("/")
    return u or ("title:" + (title or "").strip().lower())


def _bubble_resolver():
    """Return a fn mapping an agent-supplied bubble string to a real slug ("" if no match)."""
    try:
        bubs = bubbles.all_bubbles()
    except Exception:  # noqa: BLE001
        bubs = []
    by_slug = {b["slug"] for b in bubs}
    by_name = {b["name"].strip().lower(): b["slug"] for b in bubs}

    def resolve(s: str) -> str:
        s = (s or "").strip()
        if not s:
            return ""
        if s in by_slug:
            return s
        if s.lower() in by_name:
            return by_name[s.lower()]
        sl = slugify(s)
        return sl if sl in by_slug else ""

    return resolve


def add_item(raw: dict, session_uuid: str) -> Optional[dict]:
    """Persist ONE agent item live: map bubble, dedup vs `seen`, tag with session+key.

    Returns the stored record (with id) or ``None`` if it was a dup / invalid."""
    if not isinstance(raw, dict):
        return None
    title = (raw.get("title") or "").strip()
    if not title:
        return None
    url = (raw.get("url") or "").strip()
    key = _dedup_key(url, title)
    data = load_items()
    if key in set(data.get("seen", [])):
        return None
    item_id = secrets.token_hex(6)
    rec = {
        "bubble_slug": _bubble_resolver()(raw.get("bubble") or raw.get("bubble_slug") or ""),
        "title": title,
        "url": url,
        "source": (raw.get("source") or "").strip(),
        "published": (raw.get("published") or "").strip(),
        "reason": (raw.get("reason") or "").strip(),
        "found_at": _now_iso(),
        "state": "new",
        "session": session_uuid,
        "key": key,
    }
    data["items"][item_id] = rec
    data["seen"] = sorted(set(data.get("seen", [])) | {key})
    save_items(data)
    return {"id": item_id, **rec}


# --------------------------------------------------------------------------- #
# Session bookkeeping
# --------------------------------------------------------------------------- #
def get_session() -> Optional[dict]:
    return load_items().get("session")


def _set_session(sess: Optional[dict]) -> None:
    data = load_items()
    data["session"] = sess
    save_items(data)


def _append_msg(role: str, text: str) -> None:
    if not text:
        return
    data = load_items()
    s = data.get("session")
    if s is not None:
        s.setdefault("messages", []).append({"role": role, "text": text})
        save_items(data)


def _set_running(flag: bool) -> None:
    """Mark whether a crawl turn is in progress (so a returning page can reconnect by polling)."""
    data = load_items()
    s = data.get("session")
    if s is not None:
        s["running"] = bool(flag)
        save_items(data)


def _bump_session(cost: float, tokens: int) -> None:
    if not cost and not tokens:
        return
    data = load_items()
    s = data.get("session")
    if s is not None:
        s["cost_usd"] = round(float(s.get("cost_usd", 0.0)) + float(cost or 0.0), 4)
        s["tokens"] = int(s.get("tokens", 0)) + int(tokens or 0)
        save_items(data)


def _session_item_count(session_uuid: str) -> int:
    data = load_items()
    return sum(1 for v in data["items"].values()
               if v.get("session") == session_uuid and v.get("state") != "dismissed")


def accept_session() -> dict:
    """User is satisfied: advance the global date pointer to the session's range end (the `until`
    date, default today) and end the session."""
    data = load_items()
    s = data.get("session") or {}
    _archive_session(s)
    until = (s.get("until") or "")[:10] or _today()
    set_pointer(until)
    data["session"] = None
    save_items(data)
    return {"ok": True, "pointer": until}


def discard_session() -> dict:
    """Throw away the current session's items (clean slate) and end it. Cursor is untouched.
    The conversation transcript is still archived to history."""
    data = load_items()
    s = data.get("session")
    if not s:
        return {"ok": True, "removed": 0}
    _archive_session(s)  # archive the transcript before we drop the items
    uuid = s.get("uuid")
    seen = set(data.get("seen", []))
    removed = 0
    for iid in [k for k, v in data["items"].items() if v.get("session") == uuid]:
        seen.discard(data["items"][iid].get("key"))
        del data["items"][iid]
        removed += 1
    data["seen"] = sorted(k for k in seen if k)
    data["session"] = None
    save_items(data)
    return {"ok": True, "removed": removed}


# --------------------------------------------------------------------------- #
# Crawl-conversation history (archived on accept/discard) — like bubble chats
# --------------------------------------------------------------------------- #
def _safe_id(sid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", sid or "")[:64]


def _archive_session(sess: Optional[dict]) -> None:
    """Persist an ending crawl conversation to ``news_chats/<uuid>.json`` (skip if it had no
    messages). Title = the first user message."""
    if not sess:
        return
    msgs = sess.get("messages") or []
    if not msgs:
        return
    uuid = _safe_id(sess.get("uuid") or "") or secrets.token_hex(6)
    n_items = sum(1 for v in load_items()["items"].values() if v.get("session") == sess.get("uuid"))
    title = next((m.get("text") for m in msgs if m.get("role") == "user" and m.get("text")), "")
    rec = {"id": uuid, "title": (title or "Crawl").strip()[:80],
           "created_at": sess.get("started_at") or _now_iso(), "ended_at": _now_iso(),
           "model": sess.get("model"), "since": sess.get("since"), "until": sess.get("until"),
           "n_items": n_items, "cost_usd": sess.get("cost_usd", 0.0),
           "tokens": sess.get("tokens", 0), "messages": msgs}
    d = paths.news_chats_dir()
    d.mkdir(parents=True, exist_ok=True)
    _atomic_write(d / f"{uuid}.json", json.dumps(rec, ensure_ascii=False, indent=2))


def list_chat_sessions() -> list[dict]:
    d = paths.news_chats_dir()
    if not d.exists():
        return []
    out = []
    for f in d.glob("*.json"):
        try:
            r = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        out.append({k: r.get(k) for k in
                    ("id", "title", "created_at", "ended_at", "model", "since", "until",
                     "n_items", "cost_usd", "tokens")})
    out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return out


def get_chat_session(sid: str) -> Optional[dict]:
    f = paths.news_chats_dir() / f"{_safe_id(sid)}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:  # noqa: BLE001
        return None


def delete_chat_session(sid: str) -> bool:
    f = paths.news_chats_dir() / f"{_safe_id(sid)}.json"
    if f.exists():
        f.unlink()
        return True
    return False


def status() -> dict:
    """Lightweight status for the UI."""
    data = load_items()
    new_count = sum(1 for v in data["items"].values() if v.get("state") != "dismissed")
    return {
        "kill_switch_on": news_globally_enabled(),
        "instructions": load_instructions(),
        "new_count": new_count,
        "total_items": len(data["items"]),
        "session": data.get("session"),
        "default_since": _default_since(),
        "today": _today(),
    }


# --------------------------------------------------------------------------- #
# The Claude Code agent (headless, streaming subprocess)
# --------------------------------------------------------------------------- #
def _claude_cmd(prompt: str, *, session_uuid: str, resume: bool, cfg: CrawlConfig) -> list[str]:
    cmd = ["claude", "-p", prompt,
           "--output-format", "stream-json", "--verbose",
           "--model", cfg.model, "--max-turns", str(cfg.max_turns),
           "--allowedTools", "WebSearch", "WebFetch"]
    if resume:
        cmd += ["--resume", session_uuid]          # system prompt persists from the first turn
    else:
        cmd += ["--session-id", session_uuid, "--append-system-prompt", SYSTEM_PROMPT]
    return cmd


def _agent_events(cmd: list[str], timeout: int) -> Iterator[dict]:
    """Spawn ``claude`` and yield parsed NDJSON events as they stream. Killed past ``timeout``.

    The single seam tests monkeypatch. Yields nothing if the CLI can't be spawned.
    """
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, bufsize=1)
    except FileNotFoundError:
        logger.error("news: `claude` CLI not found on PATH — cannot crawl")
        return
    timer = threading.Timer(timeout, lambda: _safe_kill(proc))
    timer.start()
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:  # noqa: BLE001
                continue
    finally:
        timer.cancel()
        _safe_kill(proc)


def _safe_kill(proc) -> None:
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:  # noqa: BLE001
        pass


def _short_url(u: str) -> str:
    u = (u or "").replace("https://", "").replace("http://", "")
    return u[:60]


def _activity_label(name: str, inp) -> str:
    inp = inp or {}
    if name == "WebFetch":
        return "🌐 fetching " + _short_url(str(inp.get("url", "")))
    if name == "WebSearch":
        return "🔎 searching: " + (str(inp.get("query", "")) or "")[:80]
    return "… " + (name or "working")


def _loads_obj(s: str):
    """Parse a JSON object tolerantly: whole string, else the outermost {...}."""
    s = (s or "").strip()
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        a, b = s.find("{"), s.rfind("}")
        if a != -1 and b > a:
            try:
                return json.loads(s[a:b + 1])
            except Exception:  # noqa: BLE001
                return None
    return None


def _consume(buf: str, session_uuid: str, *, final: bool = False):
    """Split a text buffer into complete lines: @@ITEM lines are persisted live; the rest is
    display prose. Tolerates a leading bullet/number before @@ITEM. Returns
    (remaining_buffer, [stored_items], display_text)."""
    parts = buf.split("\n")
    rem = "" if final else parts.pop()
    items, out = [], []
    for line in parts:
        i = line.find(ITEM_PREFIX)
        if i != -1:
            rec = add_item(_loads_obj(line[i + len(ITEM_PREFIX):]) or {}, session_uuid)
            if rec:
                items.append(rec)
        elif line.strip():
            out.append(line)
    return rem, items, ("\n".join(out) + "\n" if out else "")


def _json_candidates(text: str) -> list:
    """Parsed JSON objects/arrays found in ```fenced``` blocks — a fallback if the agent emits a
    {"items": [...]} block instead of @@ITEM lines."""
    out = []
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text or "", re.S):
        try:
            out.append(json.loads(m.group(1).strip()))
        except Exception:  # noqa: BLE001
            pass
    return out


def _extract_items_from_text(text: str, session_uuid: str) -> list:
    """End-of-turn safety net: persist EVERY structured item in the agent's full output — any
    @@ITEM line (forgiving) plus any fenced {"items":[...]} / list / single-item block. Dedup
    means anything already saved live is skipped, so this only adds what streaming missed."""
    out = []
    for line in (text or "").split("\n"):
        i = line.find(ITEM_PREFIX)
        if i != -1:
            rec = add_item(_loads_obj(line[i + len(ITEM_PREFIX):]) or {}, session_uuid)
            if rec:
                out.append(rec)
    for obj in _json_candidates(text):
        rows = obj.get("items") if isinstance(obj, dict) else (obj if isinstance(obj, list) else None)
        if rows is None and isinstance(obj, dict) and obj.get("title"):
            rows = [obj]
        for r in (rows or []):
            rec = add_item(r, session_uuid)
            if rec:
                out.append(rec)
    return out


# --------------------------------------------------------------------------- #
# Per-bubble scope summaries (the crawler's matching signal — titles alone are weak)
# --------------------------------------------------------------------------- #
_SUMMARY_SYSTEM = (
    "You summarize the RESEARCH SCOPE of one bubble for an automated paper-matching system. "
    "In 2–4 sentences, capture the specific topics, methods, problems, and the kind of papers that "
    "belong in it — use concrete technical keywords a matcher can rely on. Output ONLY the "
    "summary, no preamble."
)
_SUMMARY_INPUT_CHARS = 12000


def load_bubble_summaries() -> dict:
    p = paths.BUBBLE_SUMMARIES_YAML
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}


def save_bubble_summaries(d: dict) -> None:
    _atomic_write(paths.BUBBLE_SUMMARIES_YAML, yaml.safe_dump(d, sort_keys=True, allow_unicode=True))


def _bubble_source_text(slug: str) -> str:
    """Everything that defines a bubble's scope: its report pages + each tagged paper's
    title and cached summary."""
    parts: list[str] = []
    try:
        for p in bubbles.list_pages(slug):
            parts.append(bubbles.get_page(slug, p["page_slug"]) or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        for m in bubbles.pdfs_for_bubble(slug):
            parts.append(m.get("title", ""))
            parts.append(assets.get_summary(m["pdf_id"]) or "")
    except Exception:  # noqa: BLE001
        pass
    return "\n\n".join(p for p in parts if p)


def _bubble_fingerprint(name: str, instructions: str, source_text: str) -> str:
    return hashlib.sha256("\x00".join([name, instructions, source_text]).encode("utf-8")).hexdigest()


def refresh_bubble_summaries(home: Path, *, claude_token: str = "", force: bool = False) -> dict:
    """(Re)generate the one-paragraph scope summary for each approved bubble whose content changed
    since last time (fingerprint over name + instructions + pages + paper summaries). Uses the
    user's active model. Fail-safe: a failed regeneration keeps the previous summary. Returns
    ``{slug: {summary, fingerprint, name, updated_at}}``."""
    store = load_bubble_summaries()
    try:
        bubs = [b for b in bubbles.all_bubbles() if b.get("approved")]
    except Exception:  # noqa: BLE001
        bubs = []
    out: dict = {}
    changed = False
    for b in bubs:
        slug, name, instr = b["slug"], b["name"], (b.get("instructions") or "")
        src = _bubble_source_text(slug)
        fp = _bubble_fingerprint(name, instr, src)
        prev = store.get(slug) or {}
        if not force and prev.get("fingerprint") == fp and prev.get("summary"):
            out[slug] = prev
            continue
        user = (f"Bubble name: {name}\nInstructions: {instr or '(none)'}\n\n"
                f"Content (report pages + paper summaries):\n{src[:_SUMMARY_INPUT_CHARS] or '(empty)'}")
        try:
            summary = models.complete(home, [{"role": "user", "content": user}],
                                      system=_SUMMARY_SYSTEM, temperature=0.2,
                                      claude_token=claude_token).strip()
        except Exception as e:  # noqa: BLE001
            logger.warning("news: bubble summary failed for %s: %s", name, e)
            summary = ""
        if summary:
            out[slug] = {"summary": summary, "fingerprint": fp, "name": name,
                         "updated_at": _now_iso()}
            changed = True
        else:
            out[slug] = prev or {"summary": "", "fingerprint": fp, "name": name}
    if changed or set(out) != set(store):
        save_bubble_summaries(out)
    return out


def _first_prompt(cfg: CrawlConfig, message: str, since: str, until: str,
                  summaries: Optional[dict] = None) -> str:
    instructions = [i for i in load_instructions()
                    if i.get("enabled", True) and (i.get("text") or "").strip()]
    try:
        bubs = bubbles.all_bubbles()
    except Exception:  # noqa: BLE001
        bubs = []
    summaries = summaries or {}
    lines = ["# Bubbles — match each item to the ONE whose SCOPE it best fits (a paper's "
             "title alone is not enough; judge against the scope description). Drop items that "
             "fit none:"]
    for b in bubs:
        slug = b["slug"]
        desc = (summaries.get(slug, {}).get("summary") or (b.get("instructions") or "")).strip()
        lines.append(f"- {b['name']}: {desc or '(no description yet)'}")
    lines.append("")
    lines.append(f"# Monitoring instructions — report ONLY items published in the date range "
                 f"[{since} .. {until}] (after {since}, up to and including {until}):")
    if not instructions:
        lines.append("- (none configured — tell the user to add monitoring instructions first)")
    for ins in instructions:
        lines.append(f"- {ins['text']}")
    if message and message.strip():
        lines += ["", f"# The user also said: {message.strip()}"]
    lines += ["", f"Start crawling now. Emit each relevant item as an @@ITEM line as you find it "
                  f"(up to ~{cfg.max_items} this turn), then tell me how many you found and "
                  f"whether there may be more."]
    return "\n".join(lines)


def chat_stream(home: Path, message: str, model: Optional[str] = None,
                since: Optional[str] = None, until: Optional[str] = None,
                claude_token: str = "") -> Iterator[dict]:
    """One conversational crawl turn. Yields SSE-shaped dicts: delta / activity / item / done /
    error. Saves items live; advances the cursor only on an explicit accept. ``since``/``until``
    (YYYY-MM-DD) set the crawl date range on the FIRST turn (defaults: pointer → today)."""
    with paths.use_root(home):
        if not news_globally_enabled():
            yield {"type": "error", "detail": "News is off. Start the server with "
                                              "LOCKEDIN_NEWS_ENABLED=1."}
            return

        msg = (message or "").strip()
        sess = load_items().get("session")

        # "I'm happy" → save & advance (only meaningful with an active session)
        if msg and ACCEPT_RE.search(msg):
            if not sess:
                yield {"type": "done", "stopped": False, "added": 0, "total": 0,
                       "cost_usd": 0.0, "text": "Nothing to save yet — start a crawl first."}
                return
            res = accept_session()
            yield {"type": "done", "accepted": True, "stopped": False, "added": 0, "total": 0,
                   "cost_usd": 0.0,
                   "text": f"Saved — moved your date pointer to {res.get('pointer')} ✓"}
            return

        first = not sess
        if first:
            session_uuid = str(_uuid.uuid4())
            cfg = CrawlConfig(model=model) if model else DEFAULTS
            rng_since = (since or _default_since())[:10]
            rng_until = (until or _today())[:10]
            _set_session({"uuid": session_uuid, "model": cfg.model,
                          "since": rng_since, "until": rng_until, "started_at": _now_iso(),
                          "cost_usd": 0.0, "tokens": 0, "messages": []})
            yield {"type": "activity", "text": "🧠 updating idea-bubble summaries from your reports…"}
            summaries = refresh_bubble_summaries(home, claude_token=claude_token)
            prompt = _first_prompt(cfg, msg, rng_since, rng_until, summaries)
        else:
            session_uuid = sess["uuid"]
            cfg = CrawlConfig(model=sess.get("model") or DEFAULTS.model)  # model is fixed per session
            rng = f"[only items dated after {sess.get('since')} and up to {sess.get('until')}]"
            prompt = ((msg or "continue — find more relevant items I don't already have, then stop")
                      + " " + rng)
        if msg:
            _append_msg("user", msg)
        _set_running(True)   # so a page returning mid-crawl knows to reconnect by polling

        cmd = _claude_cmd(prompt, session_uuid=session_uuid, resume=not first, cfg=cfg)

        buf = ""
        full_text = ""       # raw assistant text, for the end-of-turn reconciliation pass
        result_text = ""     # the final result payload (may hold items not seen mid-stream)
        added = 0
        cost = 0.0
        tokens = 0
        saw_any = saw_result = False
        stopped: object = False
        shown: list[str] = []
        def _emit_item(it):
            # record the item marker live so a returning page sees progress so far
            _append_msg("activity", "➕ added: " + (it.get("title") or it.get("url") or ""))
            return {"type": "item", "item": it}
        try:
            for ev in _agent_events(cmd, cfg.timeout):
                saw_any = True
                t = ev.get("type")
                if t == "assistant":
                    for blk in ((ev.get("message") or {}).get("content") or []):
                        bt = blk.get("type")
                        if bt == "text":
                            txt = blk.get("text", "")
                            buf += txt
                            full_text += txt
                            buf, items, text = _consume(buf, session_uuid)
                            for it in items:
                                added += 1
                                yield _emit_item(it)
                            if text:
                                shown.append(text)
                                yield {"type": "delta", "text": text}
                        elif bt == "tool_use":
                            label = _activity_label(blk.get("name", ""), blk.get("input"))
                            _append_msg("activity", label)   # persist each step live
                            yield {"type": "activity", "text": label}
                elif t == "result":
                    saw_result = True
                    result_text = ev.get("result") or ""
                    cost = float(ev.get("total_cost_usd") or 0.0)
                    u = ev.get("usage") or {}
                    tokens = sum(int(u.get(k, 0) or 0) for k in
                                 ("input_tokens", "output_tokens",
                                  "cache_creation_input_tokens", "cache_read_input_tokens"))
                    if ev.get("subtype") == "error_max_turns" or ev.get("stop_reason") == "max_turns":
                        stopped = "max_turns"
                    elif ev.get("is_error"):
                        stopped = "error"
                # system/init and user/tool_result events are ignored
        except Exception as e:  # noqa: BLE001
            logger.warning("news: stream failed for %s: %s", home.name, e)

        # flush any trailing partial line
        _, items, text = _consume(buf, session_uuid, final=True)
        for it in items:
            added += 1
            yield _emit_item(it)
        if text:
            shown.append(text)
            yield {"type": "delta", "text": text}

        if not saw_any:
            if first:
                discard_session()  # nothing started — don't strand an unusable session
            else:
                _set_running(False)
            yield {"type": "error", "detail": "The crawl agent didn't start. Is the `claude` CLI "
                                              "installed and logged into your Claude subscription "
                                              "on the server?"}
            return

        # safety net: ensure EVERY structured result in the full output reached the feed (dedup
        # skips anything already streamed live) — fixes "said it found N but added 0".
        for it in _extract_items_from_text(full_text + "\n" + result_text, session_uuid):
            added += 1
            yield _emit_item(it)

        if not saw_result and not stopped:
            stopped = "timeout"  # killed by the watchdog before the agent finished
        if shown:
            _append_msg("assistant", "".join(shown).strip())
        _bump_session(cost, tokens)
        _set_running(False)   # turn finished — a polling page will pick up the final state
        yield {"type": "done", "added": added, "total": _session_item_count(session_uuid),
               "stopped": stopped, "cost_usd": round(cost, 4), "tokens": tokens}
