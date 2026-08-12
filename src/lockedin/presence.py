"""Live presence for a bubble: who has it open, and which Scientist workers keep it synchronized.

Two kinds of participant, at the two granularities that actually matter:

* a **viewer** is a *person* — keyed by username, so one user with three browser tabs is one
  viewer, and the tab/session identity is deliberately discarded;
* a **worker** is a *project directory* — keyed by the stable id its ``.lockedin`` binding
  carries, because ``lockedin-scientist`` permits exactly one worker per directory. Several
  agents editing through one directory are one worker; the same bubble synchronized into two
  directories is two workers, which is the case worth seeing.

The registry is process-local and in-memory, like :mod:`auth` sessions, and is never persisted:
presence is a claim about *now*, so a restarted server should report an empty bubble rather than
resurrect rows for workers that may have died while it was down.

Viewers report themselves through an explicit heartbeat. Workers do not: their sync loop already
polls the bubble manifest every few seconds, so the server records them from the identity and
health headers riding on requests they were making anyway.
"""
from __future__ import annotations

import threading
import time

# A viewer heartbeats roughly every 20s; a worker's sync loop polls every 5s.
VIEWER_TTL = 70.0
WORKER_TTL = 25.0
# A worker that dies stays listed, greyed out, for this long. Silently dropping it would make
# "this sync just died" indistinguishable from "there was never a worker here".
WORKER_GRAVE = 600.0
# Presence is bounded by real people and real directories, but the maps are keyed by values that
# arrive on requests, so cap them rather than trusting that.
MAX_PER_BUBBLE = 200

_LOCK = threading.Lock()
# (workspace_id, slug) -> username -> last_seen
_VIEWERS: dict[tuple[str, str], dict[str, float]] = {}
# (workspace_id, slug) -> worker_id -> record
_WORKERS: dict[tuple[str, str], dict[str, dict]] = {}


def _clean(value: str, limit: int = 200) -> str:
    """Header values are attacker-adjacent and land in the UI: flatten and bound them."""
    return " ".join(str(value or "").split())[:limit]


def _key(workspace_id: str, slug: str) -> tuple[str, str]:
    return (str(workspace_id or ""), str(slug or ""))


def touch_viewer(workspace_id: str, slug: str, user: str, *, now: float | None = None) -> None:
    """Record that ``user`` has this bubble open."""
    if not user:
        return
    now = time.time() if now is None else now
    with _LOCK:
        seen = _VIEWERS.setdefault(_key(workspace_id, slug), {})
        if user not in seen and len(seen) >= MAX_PER_BUBBLE:
            return
        seen[user] = now


def touch_worker(workspace_id: str, slug: str, *, worker_id: str, user: str, label: str = "",
                 status: str = "", error: str = "", version: str = "", rejected: str = "",
                 now: float | None = None) -> None:
    """Record a Scientist worker's identity and self-reported health.

    ``status``/``error`` are what the client's sync loop last observed; ``rejected`` is what the
    *server* concluded about this request (an out-of-date client, say), which matters because a
    rejected worker cannot report anything about itself.
    """
    worker_id = _clean(worker_id, 64)
    if not worker_id or not user:
        return
    now = time.time() if now is None else now
    with _LOCK:
        workers = _WORKERS.setdefault(_key(workspace_id, slug), {})
        rec = workers.get(worker_id)
        if rec is None:
            if len(workers) >= MAX_PER_BUBBLE:
                return
            rec = workers[worker_id] = {"worker_id": worker_id, "first_seen": now}
        rec.update(user=user, label=_clean(label, 80), status=_clean(status, 40),
                   error=_clean(error, 300), version=_clean(version, 40),
                   rejected=_clean(rejected, 40), last_seen=now)


def _viewer_rows(key: tuple[str, str], now: float) -> list[dict]:
    seen = _VIEWERS.get(key, {})
    for user, ts in list(seen.items()):
        if now - ts > VIEWER_TTL:
            del seen[user]
    if not seen:
        _VIEWERS.pop(key, None)
    return sorted(({"user": user, "seen_ago": round(now - ts, 1)} for user, ts in seen.items()),
                  key=lambda row: row["user"])


def _health(rec: dict, now: float) -> tuple[str, str]:
    """Collapse client-reported status and server observation into one state plus a reason."""
    silent = now - rec["last_seen"]
    if rec.get("rejected") == "outdated":
        return "dead", "out of date — reinstall lockedin-scientist"
    if rec.get("rejected") == "deauthorized":
        return "dead", "authorization was revoked"
    status = rec.get("status", "")
    if status == "stopped":
        return "dead", "the worker was stopped"
    if status == "failed":
        return "dead", rec.get("error") or "the worker failed"
    if silent > WORKER_TTL:
        return "unresponsive", f"no contact for {int(silent)}s"
    if status == "degraded" or rec.get("error"):
        return "degraded", rec.get("error") or "the last synchronization failed"
    return "live", ""


def _worker_rows(key: tuple[str, str], now: float) -> list[dict]:
    workers = _WORKERS.get(key, {})
    rows = []
    for worker_id, rec in list(workers.items()):
        state, reason = _health(rec, now)
        if state == "dead" and now - rec["last_seen"] > WORKER_GRAVE:
            del workers[worker_id]
            continue
        rows.append({"worker_id": worker_id, "user": rec["user"], "label": rec.get("label", ""),
                     "state": state, "reason": reason, "version": rec.get("version", ""),
                     "seen_ago": round(now - rec["last_seen"], 1),
                     "uptime": round(now - rec["first_seen"], 1)})
    if not workers:
        _WORKERS.pop(key, None)
    # Two directories can share a display name; keep the label honest by disambiguating only the
    # ones that actually collide, so the common case stays readable.
    names: dict[str, int] = {}
    for row in rows:
        row["display"] = f"{row['user']}:{row['label']}" if row["label"] else row["user"]
        names[row["display"]] = names.get(row["display"], 0) + 1
    for row in rows:
        if names[row["display"]] > 1:
            row["display"] = f"{row['display']}#{row['worker_id'][:4]}"
    rows.sort(key=lambda row: (row["state"] == "dead", row["display"]))
    return rows


def snapshot(workspace_id: str, slug: str, *, now: float | None = None) -> dict:
    """The current viewers and workers for one bubble, pruning anything expired."""
    now = time.time() if now is None else now
    key = _key(workspace_id, slug)
    with _LOCK:
        viewers, workers = _viewer_rows(key, now), _worker_rows(key, now)
    live = [row for row in workers if row["state"] != "dead"]
    return {"viewers": viewers, "workers": workers,
            # Two live directories synchronizing one bubble is legitimate but conflict-prone, and
            # it is the situation the operator most wants surfaced rather than inferred.
            "duplicate_workers": [row["display"] for row in live] if len(live) > 1 else []}


def drop_viewer(workspace_id: str, slug: str, user: str) -> None:
    """Forget a viewer immediately, on an explicit leave."""
    with _LOCK:
        _VIEWERS.get(_key(workspace_id, slug), {}).pop(user, None)


def reset() -> None:
    """Clear all presence. For tests."""
    with _LOCK:
        _VIEWERS.clear()
        _WORKERS.clear()
