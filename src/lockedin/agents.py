"""Agents: named CLI conversations a bubble can hand a mark to.

An *agent* is not a running model. It is a persistent conversation in a coding CLI (codex,
claude, agy) plus a persona the user gave it — name, role, goal, personality — registered from
inside that conversation once. The record lives on the server so the bubble page can show who
is attached to each synchronized directory and what they are doing.

A *job* is one mark assigned to one agent. The Scientist sync worker that owns the agent's
directory learns about queued jobs on its ordinary poll, runs a single headless turn into the
agent's conversation, and reports the outcome. Idle costs nothing: between jobs no model process
exists anywhere.

Storage is two YAML files under ``REPORTS/<slug>/agents/``, never exported to clients raw; the
sync layer publishes generated ``indexes/agents.json`` and ``indexes/jobs.json`` instead.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml
from slugify import slugify

from . import agent_vendors, auth, bubbles, paths, talks

try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

logger = logging.getLogger(__name__)

VENDORS = agent_vendors.names()
STATUSES = ("queued", "running", "done", "failed", "cancelled")
OPEN_STATUSES = {"queued", "running"}
# A turn that failed only because the agent's own chat was open, not because the work was bad.
BUSY_ERROR = "the agent's chat was open, so the turn was postponed"
# Cap on requeues from a busy chat: past this many attempts the job fails outright rather than
# looping forever on a chat that never closes.
MAX_JOB_ATTEMPTS = 5
# A turn that has been "running" this long on a worker the server can no longer see is dead.
JOB_MAX_SECONDS = 45 * 60
# An agent reported attached (its chat is open in a terminal) stays so until the next heartbeat;
# after this much silence the claim is stale and the agent is simply whatever its worker is.
ATTACHED_TTL = 30.0
RETAIN_DAYS = 7
RETAIN_COUNT = 200
# Bounds so a runaway agent or a stolen cookie cannot pile up unbounded work.
MAX_OPEN_JOBS_PER_AGENT = 10
MAX_OPEN_JOBS_PER_OWNER = 40
MAX_JOBS_PER_USER_PER_HOUR = 60
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,39}$")
_CONVERSATION_RE = re.compile(r"^[A-Za-z0-9._\-]{1,120}$")
_JOB_ID_RE = re.compile(r"^j-\d{6}$")


class AgentError(ValueError):
    """A request that is well-formed but cannot be honoured (400)."""


class Conflict(AgentError):
    """The state machine refuses the transition (409)."""


class Forbidden(AgentError):
    """The actor is not entitled to touch this agent or job (403)."""


class TooMany(AgentError):
    """A concurrency or rate cap was hit (429)."""


class NotFound(KeyError):
    """No such agent, job, or mark (404)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(value: str) -> float:
    try:
        text = str(value or "").replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


@contextmanager
def _bubble_lock(slug: str):
    """One lock per bubble, across threads and (where flock exists) server processes."""
    key = str(paths.bubble_agents_dir(slug).resolve())
    with _LOCKS_GUARD:
        thread_lock = _LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        fd = None
        try:
            if fcntl is not None:
                lock_path = (paths.CONFIG_DIR / ".agent-locks"
                             / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".lock"))
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except OSError as exc:
                    logger.warning("Agent interprocess lock unavailable for %s: %s", slug, exc)
                    os.close(fd)
                    fd = None
            yield
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


def _read(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return dict(default)
    return data if isinstance(data, dict) else dict(default)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    os.replace(tmp, path)


def _agents(slug: str) -> dict:
    data = _read(paths.bubble_agents_path(slug), {"version": 1, "agents": {}})
    data.setdefault("agents", {})
    return data


def _jobs(slug: str) -> dict:
    data = _read(paths.bubble_jobs_path(slug), {"version": 1, "next_seq": 1, "jobs": {}})
    data.setdefault("jobs", {})
    data.setdefault("next_seq", 1)
    return data


def _save_agents(slug: str, data: dict) -> None:
    _write(paths.bubble_agents_path(slug), data)
    bubbles.touch_bubble(slug)


def _save_jobs(slug: str, data: dict) -> None:
    _prune(data)
    _write(paths.bubble_jobs_path(slug), data)
    bubbles.touch_bubble(slug)


def jobs_mtime(slug: str) -> float:
    """What the page poller compares: moves on any job or agent change."""
    stamps = []
    for path in (paths.bubble_jobs_path(slug), paths.bubble_agents_path(slug)):
        if path.exists():
            stamps.append(path.stat().st_mtime)
    return max(stamps) if stamps else 0.0


def _prune(data: dict) -> None:
    jobs = data.get("jobs", {})
    terminal = [(jid, job) for jid, job in jobs.items() if job.get("status") not in OPEN_STATUSES]
    cutoff = datetime.now(timezone.utc).timestamp() - RETAIN_DAYS * 86400
    terminal.sort(key=lambda item: item[1].get("finished_at") or item[1].get("created_at") or "",
                  reverse=True)
    for index, (jid, job) in enumerate(terminal):
        stamp = _parse_ts(job.get("finished_at") or job.get("created_at"))
        if index >= RETAIN_COUNT or (stamp and stamp < cutoff):
            jobs.pop(jid, None)


# --------------------------------------------------------------------------- #
# Marks: one key space for both surfaces
# --------------------------------------------------------------------------- #
def parse_mark_key(key: str) -> tuple[str, str, str]:
    """``page:<page>:<thread>`` → ("page", page, thread); ``talk-…:<note>`` → ("talk", sync, note)."""
    key = str(key or "")
    if key.startswith("page:"):
        _, _, rest = key.partition(":")
        page, sep, thread = rest.rpartition(":")
        if not sep or not page or not thread:
            raise AgentError(f"malformed mark key {key!r}")
        return "page", page, thread
    sync_id, sep, note = key.partition(":")
    if not sep or not talks.valid_sync_id(sync_id) or not note:
        raise AgentError(f"malformed mark key {key!r}")
    return "talk", sync_id, note


def _page_thread(slug: str, page: str, thread_id: str) -> dict | None:
    try:
        threads = bubbles.list_comments(slug, page).get("threads", [])
    except Exception:
        return None
    return next((t for t in threads if t.get("id") == thread_id), None)


def _talk_note(slug: str, sync_id: str, note_id: str) -> tuple[str | None, dict | None, dict | None]:
    talk_id = talks.talk_id_from_sync_id(slug, sync_id)
    if not talk_id:
        return None, None, None
    note = talks.load_notes(slug, talk_id).get("notes", {}).get(note_id)
    rec = next((r for r in talks.load_index(slug).get("talks", []) if r.get("id") == talk_id), None)
    return talk_id, note, rec


def mark_pointer(slug: str, key: str) -> dict | None:
    """Everything a worker needs to brief an agent on one mark, or ``None`` if it is gone.

    Mirrors the shape of ``indexes/marks.json`` pointers and adds the human's words, so the
    headless prompt can be rendered without a second lookup on the client.
    """
    surface, owner, local_id = parse_mark_key(key)
    if surface == "page":
        thread = _page_thread(slug, owner, local_id)
        if not thread:
            return None
        anchor = thread.get("anchor") or {}
        kind = str(thread.get("kind") or "")
        return {"surface": "page", "id": local_id, "page": owner,
                "page_title": next((p.get("title", owner) for p in bubbles.list_pages(slug)
                                    if p.get("page_slug") == owner), owner),
                "kind": kind, "means": talks.KINDS.get(kind, {}).get("means", ""),
                "glyph": talks.KINDS.get(kind, {}).get("glyph", ""),
                "quote": str(anchor.get("quote") or ""),
                "anchor_state": thread.get("anchor_state", ""),
                "messages": [{"by": m.get("author", ""), "said": m.get("body", ""),
                              "agent": bool(m.get("agent"))} for m in thread.get("messages", [])],
                "source_path": f"reports/pages/{owner}.md",
                "detail_path": f"feedback/pages/{owner}.json"}
    talk_id, note, rec = _talk_note(slug, owner, local_id)
    if not note:
        return None
    kind = str(note.get("kind") or "")
    slide = int(note.get("slide", 0) or 0)
    slide_title = ""
    try:
        slides = talks.parse_deck(talks.read_deck(slug, talk_id))
        if 0 <= slide < len(slides):
            slide_title = slides[slide].get("title", "")
    except Exception:
        pass
    pointer = {"surface": "chalk_talk", "id": local_id, "talk_id": owner,
               "talk_title": (rec or {}).get("title", ""), "slide": slide,
               "slide_title": slide_title, "kind": kind,
               "means": talks.KINDS.get(kind, {}).get("means", ""),
               "glyph": talks.KINDS.get(kind, {}).get("glyph", ""),
               "quote": str(note.get("quote") or ""),
               "anchor_type": ("text" if note.get("quote") else
                               "drawing" if note.get("paths") else "region"),
               "touches": list(note.get("covers") or []),
               "messages": [{"by": m.get("author", ""), "said": m.get("body", ""),
                             "agent": bool(m.get("agent"))} for m in note.get("messages", [])],
               "source_path": f"reports/talks/{owner}/slides.md",
               "detail_path": f"reports/talks/{owner}/marks.json"}
    if note.get("image"):
        pointer["shot_path"] = f"feedback/shots/{note['image']}"
    return pointer


def mark_exists(slug: str, key: str) -> bool:
    return mark_pointer(slug, key) is not None


def _agent_replied_after(slug: str, key: str, since: str) -> bool:
    pointer = mark_pointer(slug, key)
    if not pointer:
        return False
    floor = _parse_ts(since)
    surface, owner, local_id = parse_mark_key(key)
    if surface == "page":
        thread = _page_thread(slug, owner, local_id) or {}
        messages = thread.get("messages", [])
    else:
        _, note, _ = _talk_note(slug, owner, local_id)
        messages = (note or {}).get("messages", [])
    return any(m.get("agent") and _parse_ts(m.get("created_at")) >= floor for m in messages)


def reply_to_mark(slug: str, key: str, *, author: str, body: str, source_key: str = "") -> dict:
    """Post one agent turn into a mark's thread, whichever surface it is on."""
    body = str(body or "").strip()
    if not body:
        raise AgentError("Reply text required.")
    surface, owner, local_id = parse_mark_key(key)
    if surface == "page":
        try:
            result = bubbles.reply_comment_state(slug, owner, local_id, author, body, agent=True)
        except KeyError as exc:
            raise NotFound(key) from exc
        message = result.get("message") or {}
        return {"surface": "page", "message_id": str(message.get("id") or "")}
    talk_id, note, _ = _talk_note(slug, owner, local_id)
    if not talk_id or not note:
        raise NotFound(key)
    note = talks.reply_note(slug, talk_id, local_id, author, body, source_key=source_key, agent=True)
    last = (note.get("messages") or [{}])[-1]
    return {"surface": "chalk_talk", "message_id": str(last.get("id") or "")}


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #
def _clean(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _message(value: object, limit: int = 4000) -> str:
    """Trim a free-form message without flattening the author's paragraphs."""
    return str(value or "").strip()[:limit]


def _owner_of(agent: dict) -> str:
    return str(agent.get("owner") or agent.get("registered_by") or "")


def _find_agent(data: dict, ref: str, owner: str | None = None) -> dict | None:
    agents = data.get("agents", {})
    agent = agents.get(ref)
    if agent is not None:
        if owner is not None and _owner_of(agent) != owner:
            return None
        return agent
    wanted = str(ref or "").strip().lower()
    for a in agents.values():
        if owner is not None and _owner_of(a) != owner:
            continue
        if str(a.get("name", "")).lower() == wanted:
            return a
    return None


def get_agent(slug: str, ref: str, *, owner: str | None = None) -> dict:
    with _bubble_lock(slug):
        agent = _find_agent(_agents(slug), ref, owner=owner)
    if not agent:
        raise NotFound(ref)
    return dict(agent)


def list_agents(slug: str, *, owner: str | None = None) -> list[dict]:
    with _bubble_lock(slug):
        agents = list(_agents(slug).get("agents", {}).values())
    if owner is not None:
        agents = [a for a in agents if _owner_of(a) == owner]
    return sorted((dict(a) for a in agents), key=lambda a: a.get("created_at", ""))


def register_agent(slug: str, *, name: str, role: str, goal: str, personality: str = "",
                   vendor: str, conversation: str, model: str = "", worker_id: str,
                   project_label: str = "", registered_by: str = "") -> dict:
    """Create an agent, or refresh the one already bound to this exact conversation.

    Agents belong to one person: ``owner`` (the account that registered them, ``registered_by``)
    scopes both the upsert match and the name-uniqueness check, so two different owners on the
    same bubble may each have an agent called "Ada". ``key`` is a stable, human-readable,
    server-side identifier — ``f"{owner}-{slug-of-name}"`` — unique per bubble, used in logs.
    """
    name = _clean(name, 40)
    if not _NAME_RE.match(name):
        raise AgentError("An agent name is 1–40 letters, digits, spaces, dots, dashes or underscores.")
    vendor = str(vendor or "").strip().lower()
    if vendor not in VENDORS:
        raise AgentError(f"vendor must be one of {', '.join(VENDORS)}")
    conversation = str(conversation or "").strip()
    if not _CONVERSATION_RE.match(conversation):
        raise AgentError("A conversation id is required (letters, digits, dots, dashes, underscores).")
    worker_id = _clean(worker_id, 64)
    if not worker_id:
        raise AgentError("worker_id is required.")
    owner = _clean(registered_by, 80)
    now = _now_iso()
    with _bubble_lock(slug):
        data = _agents(slug)
        agents = data["agents"]
        existing = next((a for a in agents.values()
                         if a.get("vendor") == vendor and a.get("conversation") == conversation
                         and _owner_of(a) == owner), None)
        clash = next((a for a in agents.values()
                      if str(a.get("name", "")).lower() == name.lower()
                      and _owner_of(a) == owner
                      and a is not existing), None)
        if clash:
            raise Conflict(f"Another agent of yours on this bubble is already called {clash['name']!r}.")
        if existing is None:
            agent_id = "ag-" + secrets.token_hex(4)
            while agent_id in agents:
                agent_id = "ag-" + secrets.token_hex(4)
            existing = {"id": agent_id, "created_at": now, "registered_by": owner, "owner": owner}
            agents[agent_id] = existing
        existing["owner"] = owner
        existing["registered_by"] = existing.get("registered_by") or owner
        existing["key"] = f"{owner or 'anon'}-{slugify(name) or 'agent'}"
        existing.update({
            "name": name, "role": _clean(role, 80), "goal": _clean(goal, 600),
            "personality": _clean(personality, 600), "vendor": vendor,
            "conversation": conversation, "model": _clean(model, 80),
            "worker_id": worker_id, "project_label": _clean(project_label, 120),
            "updated_at": now, "fresh": False,
        })
        existing.setdefault("heartbeat", {"at": "", "attached": False})
        _save_agents(slug, data)
        return dict(existing)


def update_agent(slug: str, agent_id: str, *, owner: str | None = None, **fields) -> dict:
    """Worker- or user-side field updates: conversation, model, fresh, persona text.

    ``owner``, when given, scopes the lookup: an id belonging to a different owner is reported as
    :class:`NotFound` (404) rather than :class:`Forbidden`, so a probe by id cannot learn that
    someone else's agent exists.
    """
    allowed = {"conversation": 120, "model": 80, "role": 80, "goal": 600, "personality": 600,
               "name": 40}
    with _bubble_lock(slug):
        data = _agents(slug)
        agent = data["agents"].get(agent_id)
        if not agent or (owner is not None and _owner_of(agent) != owner):
            raise NotFound(agent_id)
        for key, value in fields.items():
            if key == "fresh":
                agent["fresh"] = bool(value)
            elif key in allowed:
                cleaned = _clean(value, allowed[key])
                if key == "name":
                    if not _NAME_RE.match(cleaned):
                        raise AgentError("Bad agent name.")
                    agent_owner = _owner_of(agent)
                    if any(a is not agent and str(a.get("name", "")).lower() == cleaned.lower()
                           and _owner_of(a) == agent_owner
                           for a in data["agents"].values()):
                        raise Conflict(f"Another agent is already called {cleaned!r}.")
                if key == "conversation" and cleaned and not _CONVERSATION_RE.match(cleaned):
                    raise AgentError("Bad conversation id.")
                agent[key] = cleaned
        agent["updated_at"] = _now_iso()
        _save_agents(slug, data)
        return dict(agent)


def reset_agent(slug: str, agent_id: str, conversation: str = "", *, owner: str | None = None) -> dict:
    """Forget the conversation; the next job starts a new one and re-introduces the persona."""
    return update_agent(slug, agent_id, owner=owner, conversation=conversation, fresh=True)


def remove_agent(slug: str, agent_id: str, *, owner: str | None = None) -> dict:
    """Retire an agent and cancel whatever it had not finished."""
    with _bubble_lock(slug):
        data = _agents(slug)
        agent = data["agents"].get(agent_id)
        if not agent or (owner is not None and _owner_of(agent) != owner):
            raise NotFound(agent_id)
        data["agents"].pop(agent_id, None)
        _save_agents(slug, data)
        jobs = _jobs(slug)
        changed = False
        for job in jobs["jobs"].values():
            if job.get("agent_id") == agent_id and job.get("status") in OPEN_STATUSES:
                job["status"] = "cancelled"
                job["finished_at"] = _now_iso()
                job["error"] = "the agent was retired"
                changed = True
        if changed:
            _save_jobs(slug, jobs)
        return dict(agent)


def remove_owner(slug: str, owner: str) -> dict:
    """Retire every agent owned by one account on a bubble and cancel its open jobs."""
    owner = str(owner or "").strip().lower()
    with _bubble_lock(slug):
        registry = _agents(slug)
        removed = [dict(agent) for agent in registry["agents"].values()
                   if _owner_of(agent) == owner]
        removed_ids = {agent["id"] for agent in removed}
        if removed_ids:
            registry["agents"] = {aid: agent for aid, agent in registry["agents"].items()
                                  if aid not in removed_ids}
            _save_agents(slug, registry)
        data = _jobs(slug)
        cancelled = 0
        for job in data["jobs"].values():
            if job.get("owner", "") == owner and job.get("status") in OPEN_STATUSES:
                job["status"] = "cancelled"
                job["finished_at"] = _now_iso()
                job["error"] = "the owner stopped and removed all agents"
                cancelled += 1
        if cancelled:
            _save_jobs(slug, data)
    return {"agents": len(removed), "cancelled_jobs": cancelled}


def stop_owner(slug: str, owner: str) -> dict:
    """Stop one account's agents without erasing their identity or conversation.

    Secure mode revokes every machine credential, so these records cannot execute again until an
    authorized worker checks in.  Keeping them is what makes the stop reversible: the browser can
    still show each named agent and offer a recovery command that reconnects its original folder.
    """
    owner = str(owner or "").strip().lower()
    now = _now_iso()
    with _bubble_lock(slug):
        registry = _agents(slug)
        stopped = 0
        for agent in registry["agents"].values():
            if _owner_of(agent) != owner:
                continue
            agent["revive_required"] = True
            agent["stopped_at"] = now
            agent["heartbeat"] = {"at": "", "attached": False}
            agent["updated_at"] = now
            stopped += 1
        if stopped:
            _save_agents(slug, registry)
        data = _jobs(slug)
        cancelled = 0
        for job in data["jobs"].values():
            if job.get("owner", "") == owner and job.get("status") in OPEN_STATUSES:
                job["status"] = "cancelled"
                job["finished_at"] = now
                job["error"] = "the owner turned on Stop agents"
                cancelled += 1
        if cancelled:
            _save_jobs(slug, data)
    return {"agents": stopped, "cancelled_jobs": cancelled}


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
def _job_summary(job: dict, agents: dict) -> dict:
    agent = agents.get(job.get("agent_id", ""), {})
    result = job.get("result") or {}
    # Prefer live registry name; fall back to stored name so retiring an agent does not blank
    # the history of what it accomplished. Older jobs (pre-denormalization) have no stored name.
    agent_name = agent.get("name") or job.get("agent_name", "")
    return {"id": job["id"], "agent_id": job.get("agent_id", ""),
            "agent_name": agent_name, "owner": job.get("owner", ""),
            "kind": job.get("kind", "mark"),
            "mark_key": job.get("mark_key", ""),
            "instruction": job.get("instruction", ""), "status": job.get("status", ""),
            "created_by": job.get("created_by", ""), "created_at": job.get("created_at", ""),
            "started_at": job.get("started_at", ""), "finished_at": job.get("finished_at", ""),
            "attempts": int(job.get("attempts", 1) or 1), "error": job.get("error", ""),
            "late": bool(job.get("late")), "late_from": job.get("late_from", ""),
            "late_error": job.get("late_error", ""),
            "result": {"exit_code": result.get("exit_code"),
                       "output_tail": str(result.get("output_tail") or "")[-1200:],
                       "reply_text": str(result.get("reply_text") or "")[:4000],
                       "confirmed": bool(result.get("confirmed"))}}


def _open_count(jobs: dict, *, agent_id: str | None = None, owner: str | None = None) -> int:
    def matches(j: dict) -> bool:
        if j.get("status") not in OPEN_STATUSES:
            return False
        if agent_id is not None and j.get("agent_id") != agent_id:
            return False
        if owner is not None and j.get("owner", "") != owner:
            return False
        return True
    return sum(1 for j in jobs.values() if matches(j))


def _created_last_hour(jobs: dict, owner: str) -> int:
    cutoff = datetime.now(timezone.utc).timestamp() - 3600
    return sum(1 for j in jobs.values()
               if j.get("owner", "") == owner and _parse_ts(j.get("created_at")) >= cutoff)


def create_job(slug: str, *, agent_id: str, mark_key: str = "", instruction: str = "",
               created_by: str = "", kind: str = "mark") -> dict:
    """Queue either a mark assignment or one direct web-message turn."""
    if kind not in {"mark", "direct"}:
        raise AgentError("job kind must be mark or direct")
    if kind == "mark":
        parse_mark_key(mark_key)
        if not mark_exists(slug, mark_key):
            raise NotFound(mark_key)
    else:
        mark_key = ""
        instruction = _message(instruction)
        if not instruction:
            raise AgentError("Message text required.")
    with _bubble_lock(slug):
        agents = _agents(slug)["agents"]
        agent = _find_agent({"agents": agents}, agent_id)
        if not agent:
            raise NotFound(agent_id)
        owner = _owner_of(agent)
        created_by = _clean(created_by, 80)
        if owner and created_by and owner != created_by:
            raise Forbidden(f"{agent['name']} belongs to another owner.")
        if owner and auth.secure_mode(owner):
            raise Conflict("Secure mode is on for this account: turn it off before assigning new work.")
        data = _jobs(slug)
        for job in data["jobs"].values():
            if (kind == "mark" and job.get("agent_id") == agent["id"]
                    and job.get("mark_key") == mark_key
                    and job.get("status") in OPEN_STATUSES):
                raise Conflict(f"{agent['name']} already has this mark in progress ({job['id']}).")
        if _open_count(data["jobs"], agent_id=agent["id"]) >= MAX_OPEN_JOBS_PER_AGENT:
            raise TooMany(f"{agent['name']} already has {MAX_OPEN_JOBS_PER_AGENT} open jobs.")
        if owner:
            if _open_count(data["jobs"], owner=owner) >= MAX_OPEN_JOBS_PER_OWNER:
                raise TooMany(f"You already have {MAX_OPEN_JOBS_PER_OWNER} open jobs on this bubble.")
            if _created_last_hour(data["jobs"], owner) >= MAX_JOBS_PER_USER_PER_HOUR:
                raise TooMany(f"You have created {MAX_JOBS_PER_USER_PER_HOUR} jobs in the last hour.")
        seq = int(data.get("next_seq", 1) or 1)
        job_id = f"j-{seq:06d}"
        data["next_seq"] = seq + 1
        job = {"id": job_id, "agent_id": agent["id"], "agent_name": agent["name"], "owner": owner,
               "kind": kind, "mark_key": mark_key,
               "instruction": instruction if kind == "direct" else _clean(instruction, 2000),
               "status": "queued",
               "created_by": created_by, "created_at": _now_iso(),
               "started_at": "", "finished_at": "", "worker_id": "", "attempts": 1,
               "result": {"exit_code": None, "output_tail": "", "reply_message_id": "",
                          "confirmed": False}, "error": ""}
        data["jobs"][job_id] = job
        _save_jobs(slug, data)
        return _job_summary(job, agents)


def create_message(slug: str, *, agent_id: str, text: str, created_by: str = "") -> dict:
    """Queue a free-form message as a real agent turn, independent of any mark."""
    return create_job(slug, agent_id=agent_id, instruction=text, created_by=created_by,
                      kind="direct")


def _get_job(data: dict, job_id: str) -> dict:
    job = data.get("jobs", {}).get(job_id)
    if not job:
        raise NotFound(job_id)
    return job


def _require_owner(job_or_agent_owner: str, actor: str, message: str) -> None:
    """Raise :class:`Forbidden` when both sides are known and disagree.

    Silent (no check) whenever either side is unset — an empty owner means the record predates
    ownership (or was created directly in a test without one), and an empty actor means the
    caller did not ask for enforcement, matching every pre-existing call site.
    """
    if job_or_agent_owner and actor and job_or_agent_owner != actor:
        raise Forbidden(message)


def get_job(slug: str, job_id: str, *, owner: str | None = None) -> dict:
    with _bubble_lock(slug):
        agents = _agents(slug)["agents"]
        job = _get_job(_jobs(slug), job_id)
        if owner is not None and job.get("owner", "") != owner:
            raise NotFound(job_id)
        return _job_summary(job, agents)


def start_job(slug: str, job_id: str, *, worker_id: str, actor: str = "") -> dict:
    with _bubble_lock(slug):
        agents = _agents(slug)["agents"]
        data = _jobs(slug)
        job = _get_job(data, job_id)
        _require_owner(job.get("owner", ""), actor, f"{job_id} belongs to another owner.")
        if job.get("status") != "queued":
            raise Conflict(f"{job_id} is {job.get('status')}, not queued.")
        agent = agents.get(job.get("agent_id", ""))
        if not agent:
            job["status"] = "cancelled"; job["error"] = "the agent no longer exists"
            job["finished_at"] = _now_iso(); _save_jobs(slug, data)
            raise Conflict("The agent no longer exists.")
        agent_owner = _owner_of(agent)
        if agent_owner and auth.secure_mode(agent_owner):
            raise Conflict("Secure mode is on for this account: no new turns may start.")
        if agent.get("worker_id") != worker_id:
            raise Conflict("This job belongs to another directory's worker.")
        if any(j.get("agent_id") == agent["id"] and j.get("status") == "running"
               for j in data["jobs"].values()):
            raise Conflict(f"{agent['name']} is already running a job.")
        job["status"] = "running"
        job["started_at"] = _now_iso()
        job["worker_id"] = worker_id
        job["error"] = ""
        _save_jobs(slug, data)
        return _job_summary(job, agents)


def requeue_job(slug: str, job_id: str, *, reason: str = "") -> dict:
    """Send a ``running`` job back to ``queued`` because the agent's chat was open, not because
    the work failed. Only a running job may be requeued; any other source status is a Conflict.

    Capped: once ``attempts`` would exceed :data:`MAX_JOB_ATTEMPTS` the job is failed instead, so
    a chat that never closes cannot keep the job alive forever.
    """
    with _bubble_lock(slug):
        agents = _agents(slug)["agents"]
        data = _jobs(slug)
        job = _get_job(data, job_id)
        if job.get("status") != "running":
            raise Conflict(f"{job_id} is {job.get('status')}, not running.")
        attempts = int(job.get("attempts", 1) or 1) + 1
        job["attempts"] = attempts
        if attempts > MAX_JOB_ATTEMPTS:
            job["status"] = "failed"
            job["finished_at"] = _now_iso()
            job["worker_id"] = ""
            job["error"] = _clean(
                (reason or BUSY_ERROR) + " (the chat stayed open too many times)", 600)
        else:
            job["status"] = "queued"
            job["started_at"] = ""
            job["worker_id"] = ""
            job["error"] = _clean(reason or BUSY_ERROR, 600)
        _save_jobs(slug, data)
        return _job_summary(job, agents)


def finish_job(slug: str, job_id: str, *, status: str, exit_code: int | None = None,
               output_tail: str = "", error: str = "", actor: str = "") -> dict:
    """The worker's verdict on a finished turn. A reply that already landed wins over exit code."""
    if status == "requeue":
        return requeue_job(slug, job_id, reason=error)
    if status not in {"done", "failed"}:
        raise AgentError("status must be done, failed, or requeue")
    with _bubble_lock(slug):
        agents = _agents(slug)["agents"]
        data = _jobs(slug)
        job = _get_job(data, job_id)
        _require_owner(job.get("owner", ""), actor, f"{job_id} belongs to another owner.")
        result = job.setdefault("result", {})
        result["exit_code"] = exit_code
        result["output_tail"] = str(output_tail or "")[-4000:]
        if job.get("status") == "running":
            if status == "done" and not result.get("confirmed"):
                # The turn exited cleanly without calling `agent reply`: check whether it used the
                # legacy in-deck reply block instead before deciding it silently did nothing.
                if (job.get("kind", "mark") == "mark"
                        and _agent_replied_after(slug, job.get("mark_key", ""), job.get("started_at", ""))):
                    result["confirmed"] = True
                else:
                    target = "message" if job.get("kind") == "direct" else "mark"
                    status, error = "failed", error or f"the turn ended without replying to the {target}"
            job["status"] = status
            job["finished_at"] = _now_iso()
            job["error"] = _clean(error, 600) if status == "failed" else ""
        elif job.get("status") in OPEN_STATUSES:
            job["status"] = status
            job["finished_at"] = _now_iso()
            job["error"] = _clean(error, 600) if status == "failed" else ""
        _save_jobs(slug, data)
        return _job_summary(job, agents)


def credit(agent: dict, job: dict, actor: str = "") -> str:
    """How an agent's turn is signed in a thread.

    Deliberately the same phrasing the in-deck reply block produces through
    ``talks.absorb_push``, so one thread reads consistently however the agent answered. The
    account is the one the agent acts under: the caller's authenticated user, else whoever
    assigned the job, else whoever registered the agent.
    """
    name = str((agent or {}).get("name") or "").strip() or "agent"
    for candidate in (actor, (job or {}).get("created_by"), (agent or {}).get("registered_by")):
        who = str(candidate or "").strip()
        if who:
            return f"{name} on behalf of {who}"
    return name


def reply_job(slug: str, job_id: str, *, text: str, actor: str = "") -> dict:
    """The agent's own answer: post it into the mark's thread and close the job.

    The rule is not "the job must still be open" but "this job must not have answered already".
    A mark can be cancelled while an agent is mid-turn; the agent may still finish the work and
    call this after the cancel lands. The work happened and the user wants to read it, so a
    cancelled (or even failed) job legitimately becomes ``done`` here rather than being refused.
    What we actually guard against is a *second* reply from the same job, since a repeat would
    duplicate the turn in the thread. On the page surface that guard is the only protection there
    is: ``reply_to_mark``'s chalk-talk path passes ``source_key`` and ``talks.reply_note`` already
    ignores a repeat post with the same key, but the page path calls
    ``bubbles.reply_comment_state``, which has no such dedupe of its own. Do not remove this
    already-answered check without adding an equivalent guard on the page path.
    """
    with _bubble_lock(slug):
        agents = _agents(slug)["agents"]
        data = _jobs(slug)
        job = _get_job(data, job_id)
        _require_owner(job.get("owner", ""), actor, f"{job_id} belongs to another owner.")
        result = job.get("result") or {}
        if result.get("confirmed") or result.get("reply_message_id"):
            raise Conflict(f"{job_id} was already answered.")
        was_open = job.get("status") in OPEN_STATUSES
        prev_status = job.get("status")
        prev_error = job.get("error", "")
        agent = agents.get(job.get("agent_id", ""), {})
        reply_text = (_message(text) if job.get("kind") == "direct" else str(text or "").strip())
        if not reply_text:
            raise AgentError("Reply text required.")
        posted = ({"message_id": ""} if job.get("kind") == "direct" else
                  reply_to_mark(slug, job["mark_key"], author=credit(agent, job, actor),
                                body=reply_text, source_key=f"agent:{job_id}"))
        job["status"] = "done"
        job["finished_at"] = _now_iso()
        job["error"] = ""
        if not was_open:
            job["late"] = True
            job["late_from"] = prev_status
            job["late_error"] = prev_error
        result = job.setdefault("result", {})
        result["confirmed"] = True
        result["reply_message_id"] = posted.get("message_id", "")
        if job.get("kind") == "direct":
            result["reply_text"] = reply_text
        _save_jobs(slug, data)
        return _job_summary(job, agents)


def fail_job(slug: str, job_id: str, *, reason: str, actor: str = "") -> dict:
    """The agent declines: the reason lands in the thread so the user sees why.

    Same rule as :func:`reply_job`: refuse only if this job already answered, otherwise post the
    reason whatever the job's status is and record ``late``/``late_from`` when it was not open.
    """
    reason = _clean(reason, 600) or "the agent could not do this"
    with _bubble_lock(slug):
        agents = _agents(slug)["agents"]
        data = _jobs(slug)
        job = _get_job(data, job_id)
        _require_owner(job.get("owner", ""), actor, f"{job_id} belongs to another owner.")
        result = job.get("result") or {}
        if result.get("confirmed") or result.get("reply_message_id"):
            raise Conflict(f"{job_id} was already answered.")
        was_open = job.get("status") in OPEN_STATUSES
        prev_status = job.get("status")
        agent = agents.get(job.get("agent_id", ""), {})
        if job.get("kind") != "direct":
            try:
                reply_to_mark(slug, job["mark_key"], author=credit(agent, job, actor),
                              body=f"I could not do this: {reason}", source_key=f"agent-fail:{job_id}")
            except NotFound:
                pass
        job["status"] = "failed"
        job["finished_at"] = _now_iso()
        job["error"] = reason
        if not was_open:
            job["late"] = True
            job["late_from"] = prev_status
        job.setdefault("result", {})["confirmed"] = True
        _save_jobs(slug, data)
        return _job_summary(job, agents)


def cancel_job(slug: str, job_id: str, *, actor: str = "") -> dict:
    with _bubble_lock(slug):
        agents = _agents(slug)["agents"]
        data = _jobs(slug)
        job = _get_job(data, job_id)
        _require_owner(job.get("owner", ""), actor, f"{job_id} belongs to another owner.")
        if job.get("status") not in OPEN_STATUSES:
            raise Conflict(f"{job_id} is already {job.get('status')}.")
        job["status"] = "cancelled"
        job["finished_at"] = _now_iso()
        job["error"] = "cancelled by the user"
        _save_jobs(slug, data)
        return _job_summary(job, agents)


def reassign_job(slug: str, job_id: str, *, agent_id: str, actor: str = "") -> dict:
    with _bubble_lock(slug):
        agents = _agents(slug)["agents"]
        data = _jobs(slug)
        job = _get_job(data, job_id)
        _require_owner(job.get("owner", ""), actor, f"{job_id} belongs to another owner.")
        agent = _find_agent({"agents": agents}, agent_id)
        if not agent:
            raise NotFound(agent_id)
        target_owner = _owner_of(agent)
        _require_owner(target_owner, actor, f"{agent['name']} belongs to another owner.")
        if job.get("status") == "running":
            raise Conflict(f"{job_id} is running; cancel it first.")
        if (job.get("kind", "mark") == "mark"
                and any(j is not job and j.get("agent_id") == agent["id"]
                        and j.get("kind", "mark") == "mark"
                        and j.get("mark_key") == job.get("mark_key")
                        and j.get("status") in OPEN_STATUSES for j in data["jobs"].values())):
            raise Conflict(f"{agent['name']} already has this mark in progress.")
        if _open_count(data["jobs"], agent_id=agent["id"]) >= MAX_OPEN_JOBS_PER_AGENT:
            raise TooMany(f"{agent['name']} already has {MAX_OPEN_JOBS_PER_AGENT} open jobs.")
        if target_owner and _open_count(data["jobs"], owner=target_owner) >= MAX_OPEN_JOBS_PER_OWNER:
            raise TooMany(f"You already have {MAX_OPEN_JOBS_PER_OWNER} open jobs on this bubble.")
        job.update({"agent_id": agent["id"], "agent_name": agent["name"], "owner": target_owner,
                    "status": "queued", "started_at": "",
                    "finished_at": "", "worker_id": "", "error": "",
                    "attempts": int(job.get("attempts", 1) or 1) + 1,
                    "result": {"exit_code": None, "output_tail": "", "reply_message_id": "",
                               "confirmed": False}})
        _save_jobs(slug, data)
        return _job_summary(job, agents)


def reconcile(slug: str, *, worker_id: str = "", running_job_ids: list[str] | None = None,
              live_worker_ids: set[str] | None = None) -> None:
    """Close what finished elsewhere and fail what nobody is running any more.

    * A running job whose mark received an agent message after it started is done — this is how
      the legacy ``<!-- lockedin-reply -->`` deck block closes a job with no hook in the push path.
    * A running job on ``worker_id`` that the worker does not list as running died with a
      previous worker process.
    * A running job older than :data:`JOB_MAX_SECONDS` on a worker the server cannot see is dead.
    """
    now = datetime.now(timezone.utc).timestamp()
    with _bubble_lock(slug):
        data = _jobs(slug)
        changed = False
        for job in data["jobs"].values():
            if job.get("status") != "running":
                continue
            if (job.get("kind", "mark") == "mark"
                    and _agent_replied_after(slug, job.get("mark_key", ""), job.get("started_at", ""))):
                job["status"] = "done"; job["finished_at"] = _now_iso(); job["error"] = ""
                job.setdefault("result", {})["confirmed"] = True
                changed = True
                continue
            if (worker_id and job.get("worker_id") == worker_id and running_job_ids is not None
                    and job["id"] not in running_job_ids):
                job["status"] = "failed"; job["finished_at"] = _now_iso()
                job["error"] = "the sync worker restarted while this turn was running"
                changed = True
                continue
            started = _parse_ts(job.get("started_at"))
            if (live_worker_ids is not None and job.get("worker_id") not in live_worker_ids
                    and started and now - started > JOB_MAX_SECONDS):
                job["status"] = "failed"; job["finished_at"] = _now_iso()
                job["error"] = "the sync worker disappeared while this turn was running"
                changed = True
        if changed:
            _save_jobs(slug, data)


def heartbeat(slug: str, *, worker_id: str, agents: list[dict], running_job_ids: list[str],
             secure: bool = False, owner: str = "") -> dict:
    """The worker's per-poll check-in. Returns what it should run and what it should stop.

    ``secure`` is the caller's secure-mode switch (see ``auth.secure_mode``): while on, every
    running turn for this owner is told to stop and nothing new is handed out, so a person who
    stepped away is never surprised by more agent activity happening on their behalf. ``owner``,
    when given, additionally scopes which agents this worker is allowed to touch to that owner —
    belt and braces alongside the ``worker_id`` match, since a worker id is not itself a secret.
    """
    if secure:
        return {"jobs": [], "cancelled": list(running_job_ids or []), "secure_mode": True}
    reconcile(slug, worker_id=worker_id, running_job_ids=list(running_job_ids or []))
    now = _now_iso()
    reported = {str(item.get("id", "")): item for item in agents or []}
    with _bubble_lock(slug):
        registry = _agents(slug)
        mine = {aid: a for aid, a in registry["agents"].items()
               if a.get("worker_id") == worker_id and (not owner or _owner_of(a) == owner)}
        for aid, agent in mine.items():
            item = reported.get(aid, {})
            heartbeat = {"at": now, "attached": bool(item.get("attached"))}
            activity = item.get("activity")
            if isinstance(activity, dict) and activity.get("job_id"):
                heartbeat["activity"] = {
                    key: activity.get(key) for key in
                    ("job_id", "started_at", "last_output_at", "output_bytes", "deadline_at")
                }
            agent["heartbeat"] = heartbeat
            # Reaching this point proves a newly authorized worker owns the retained record.
            # Clear the secure-stop marker without touching its persona or conversation.
            agent.pop("revive_required", None)
            agent.pop("stopped_at", None)
            if "budget" in item:
                agent["budget"] = item.get("budget")
            if "confinement" in item:
                agent["confinement"] = str(item.get("confinement") or "")
            if "turn_timeout_seconds" in item:
                try: agent["turn_timeout_seconds"] = max(0, int(item.get("turn_timeout_seconds") or 0))
                except (TypeError, ValueError): pass
        if mine:
            _save_agents(slug, registry)
        data = _jobs(slug)
        queued, cancelled = [], []
        for job in sorted(data["jobs"].values(), key=lambda j: j.get("created_at", "")):
            if job.get("agent_id") not in mine:
                continue
            if job.get("status") == "queued":
                summary = _job_summary(job, registry["agents"])
                summary["agent"] = dict(mine[job["agent_id"]])
                if job.get("kind") == "direct":
                    summary["mark"] = {"surface": "direct"}
                else:
                    pointer = mark_pointer(slug, job.get("mark_key", ""))
                    if pointer is None:
                        job["status"] = "cancelled"; job["finished_at"] = now
                        job["error"] = "the mark was removed before the agent got to it"
                        _save_jobs(slug, data)
                        continue
                    summary["mark"] = pointer
                queued.append(summary)
            elif job.get("status") == "cancelled" and job["id"] in (running_job_ids or []):
                cancelled.append(job["id"])
    return {"jobs": queued, "cancelled": cancelled, "secure_mode": False}


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
def _status_for(agent: dict, live_worker_ids: set[str] | None, running: set[str]) -> str:
    if live_worker_ids is not None and agent.get("worker_id") not in live_worker_ids:
        return "offline"
    if agent["id"] in running:
        return "working"
    beat = agent.get("heartbeat") or {}
    if beat.get("attached") and _parse_ts(beat.get("at")) >= (
            datetime.now(timezone.utc).timestamp() - ATTACHED_TTL):
        return "attached"
    return "idle"


def overview(slug: str, *, workers: list[dict] | None = None, viewer: str = "") -> dict:
    """What the bubble page shows: agents with derived status, and jobs grouped for the marks.

    Agents belong to one person and are invisible to everyone else: unless ``viewer`` is
    explicitly ``None`` (an internal, cross-owner view used only by the legacy deck-reply
    resolver), only agents whose ``owner`` equals ``viewer`` — and only jobs belonging to them —
    are returned. While that owner has secure mode on, every one of their agents is reported
    ``"stopped"`` and the top level carries ``secure_mode: true``.
    """
    live: set[str] | None = None
    if workers is not None:
        live = {str(w.get("worker_id")) for w in workers if w.get("state") in ("live", "degraded")}
        reconcile(slug, live_worker_ids=live)
    filtered = viewer is not None
    secure = bool(filtered and viewer and auth.secure_mode(viewer))
    with _bubble_lock(slug):
        agents = _agents(slug)["agents"]
        jobs = list(_jobs(slug)["jobs"].values())
    if filtered:
        agents = {aid: a for aid, a in agents.items() if _owner_of(a) == viewer}
        # Filtered on the job's own denormalized ``owner`` — not on whether its agent is still in
        # the filtered map — so a retired agent's job history stays visible to the owner it
        # belonged to (see ``_job_summary``'s ``agent_name`` fallback, the same idea applied to
        # ``owner``).
        jobs = [j for j in jobs if j.get("owner", "") == viewer]
    running = {j["agent_id"] for j in jobs if j.get("status") == "running"}
    summaries = sorted((_job_summary(j, agents) for j in jobs), key=lambda j: j["created_at"])
    activity_by_job = {}
    for agent in agents.values():
        activity = (agent.get("heartbeat") or {}).get("activity") or {}
        if activity.get("job_id"):
            activity_by_job[str(activity["job_id"])] = dict(activity)
    for job in summaries:
        if job["id"] in activity_by_job:
            job["activity"] = activity_by_job[job["id"]]
    by_mark: dict[str, list[dict]] = {}
    mark_context: dict[str, dict | None] = {}
    for job in summaries:
        if job["kind"] == "mark":
            by_mark.setdefault(job["mark_key"], []).append(job)
            if job["mark_key"] not in mark_context:
                mark_context[job["mark_key"]] = mark_pointer(slug, job["mark_key"])
    last_by_agent: dict[str, dict] = {}
    for job in summaries:
        last_by_agent[job["agent_id"]] = job
    now_ts = datetime.now(timezone.utc).timestamp()
    rows = []
    for agent in sorted(agents.values(), key=lambda a: a.get("created_at", "")):
        row = dict(agent)
        row["status"] = "stopped" if secure else _status_for(agent, live, running)
        row["last_job"] = last_by_agent.get(agent["id"])
        row["open_jobs"] = sum(1 for j in summaries
                               if j["agent_id"] == agent["id"] and j["status"] in OPEN_STATUSES)
        row["owner"] = _owner_of(agent)
        row["key"] = agent.get("key", "")
        row["budget"] = agent.get("budget")
        row["confinement"] = agent.get("confinement", "")
        row["turns_last_hour"] = sum(
            1 for j in summaries if j["agent_id"] == agent["id"] and j.get("started_at")
            and now_ts - _parse_ts(j["started_at"]) <= 3600)
        row["turns_today"] = sum(
            1 for j in summaries if j["agent_id"] == agent["id"] and j.get("started_at")
            and now_ts - _parse_ts(j["started_at"]) <= 86400)
        agent_jobs = [j for j in summaries if j["agent_id"] == agent["id"]]
        # ``messages`` remains the backwards-compatible direct-message view. ``history`` is the
        # popup's complete chronological work record: direct turns plus the real marked thread,
        # including every human and agent reply currently attached to that mark.
        row["messages"] = [j for j in agent_jobs if j["kind"] == "direct"]
        row["history"] = []
        for job in agent_jobs:
            item = dict(job)
            if job["kind"] == "mark":
                pointer = mark_context.get(job["mark_key"])
                item["mark"] = dict(pointer) if pointer else None
            row["history"].append(item)
        rows.append(row)
    return {"agents": rows,
            "jobs": {"by_mark": by_mark,
                     "open": [j for j in summaries if j["status"] in OPEN_STATUSES],
                     "recent": [j for j in reversed(summaries) if j["status"] not in OPEN_STATUSES][:30]},
            "jobs_mtime": jobs_mtime(slug),
            "secure_mode": secure}


def indexes(slug: str, *, owner: str = "") -> tuple[dict, dict]:
    """The two generated files the sync layer publishes into ``.lockedin/indexes/``.

    ``owner``, when given, restricts both files (and the counts derived from them) to that
    owner's agents and jobs; the default ``""`` means everything on the bubble, which is what
    every pre-existing caller relies on.
    """
    with _bubble_lock(slug):
        agents = _agents(slug)["agents"]
        jobs = list(_jobs(slug)["jobs"].values())
    if owner:
        agents = {aid: a for aid, a in agents.items() if _owner_of(a) == owner}
        jobs = [j for j in jobs if j.get("owner", "") == owner]
    public = {}
    by_worker: dict[str, list[str]] = {}
    for aid, agent in agents.items():
        public[aid] = {k: agent.get(k, "") for k in
                       ("id", "name", "role", "goal", "personality", "vendor", "model",
                        "conversation", "worker_id", "project_label", "fresh", "owner", "key")}
        by_worker.setdefault(str(agent.get("worker_id", "")), []).append(aid)
    job_index = {}
    for job in jobs:
        entry = _job_summary(job, agents)
        entry.pop("result", None)
        if job.get("status") in OPEN_STATUSES:
            entry["pointer"] = ({"surface": "direct"} if job.get("kind") == "direct" else
                                mark_pointer(slug, job.get("mark_key", "")))
        job_index[job["id"]] = entry
    return ({"version": 1, "by_id": public, "by_worker": by_worker},
            {"version": 1, "by_id": job_index,
             "open": sorted(j["id"] for j in jobs if j.get("status") in OPEN_STATUSES)})
