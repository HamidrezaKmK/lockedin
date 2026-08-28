"""Chalk talks — dated slide decks the agent writes to explain an idea, and the marks you
leave on them.

Two rules shape everything here:

* **The deck is markdown the agent authored, and it is the shared address space.** A mark is
  anchored by the *quoted text* plus a little context on either side, resolved against that
  markdown — never against rendered HTML and never by character offset, which would break the
  moment either side edits. The quote is an address that provably exists in the file the agent
  wrote, so "you marked this string ✗ wrong" needs no translation layer.
* **Your marks live in a sidecar, never inside the deck.** A talk is a dated artifact; your pen
  must not edit the historical record. The sidecar also lets a mark carry state (open / replied /
  addressed) and survive the slide being rewritten underneath it.

A mark that can no longer be resolved is reported as `orphan`, with the text it used to point
at, and is never silently dropped — losing a reviewer's objection is the one unacceptable bug.
"""

from __future__ import annotations

import os
import re
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import yaml
from slugify import slugify

from . import paths

# The five marks. Deliberately few: most supervision is one of these, and a tap carries a much
# stronger signal than prose the agent has to infer intent from.
KINDS = {
    "bad":  {"glyph": "✗", "label": "wrong",   "means": "this is wrong"},
    "q":    {"glyph": "?", "label": "unclear", "means": "I don't follow"},
    "more": {"glyph": "→", "label": "deeper",  "means": "go deeper"},
    "good": {"glyph": "✓", "label": "good",    "means": "good, keep this"},
    "cut":  {"glyph": "✂", "label": "cut",     "means": "cut this"},
    # The sixth mark is not in the picker: it is created by the draw tool, and the drawing is
    # the message. The strokes live on the note and are burned into the snapshot the agent gets.
    "ink":  {"glyph": "✍", "label": "drawn",   "means": "look at what I drew on the picture and apply it"},
}
KIND_ORDER = ["bad", "q", "more", "good", "cut", "ink"]

SLIDE_KINDS = ["setup", "derivation", "evidence", "comparison", "implementation", "ask"]
_SLIDE_KIND_RE = re.compile(r"[A-Za-z][A-Za-z0-9 _&/+-]{0,47}$")

# Slides are separated by a horizontal rule on its own line. Chosen because it is what a human
# writing markdown slides reaches for anyway, and because every markdown renderer already
# treats it as a break.
SEPARATOR = re.compile(r"^-{3,}\s*$", re.M)

CONTEXT = 40  # chars of prefix/suffix stored with a quote, enough to disambiguate a repeat


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _slide_kind(value: str) -> str:
    """Normalize a human section label without allowing it to break the slide attribute."""
    kind = " ".join(str(value or "").split())
    if not _SLIDE_KIND_RE.fullmatch(kind):
        raise ValueError("slide section must be 1–48 letters, numbers, spaces, or - / + &")
    return kind


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _read_yaml(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return dict(default)
    return data if isinstance(data, dict) else dict(default)


def _write_yaml(path: Path, data: dict) -> None:
    _atomic_write(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


# --------------------------------------------------------------------------- #
# Deck parsing
# --------------------------------------------------------------------------- #
_ATTR = re.compile(r"<!--\s*slide:\s*(.*?)\s*-->", re.S)
_H1 = re.compile(r"^#\s+(.+?)\s*$", re.M)
_SUB = re.compile(r"^\*([^*].*?)\*\s*$", re.M)
# A Scientist has files, not an authenticated browser session.  This is its one deliberately
# narrow write path back into a review thread.  The server consumes the block on push, records
# the reply, and removes it from the deck before returning the canonical source to the worker.
_REPLY = re.compile(
    r"<!--\s*lockedin-reply:\s*([A-Za-z0-9_-]+)\s*-->\s*\n?(.*?)\n?<!--\s*/lockedin-reply\s*-->",
    re.S,
)


def _parse_attrs(chunk: str) -> dict:
    m = _ATTR.search(chunk)
    if not m:
        return {}
    out = {}
    # Split only on commas that begin the next `key=`, so a legacy list-valued attribute
    # survives intact enough to be recognised and dropped.
    for part in re.split(r",(?=\s*[A-Za-z_][A-Za-z0-9_]*\s*=)", m.group(1)):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out


def parse_deck(text: str) -> list[dict]:
    """Split a deck into slides.

    A slide is: an optional `<!-- slide: kind=... -->` attribute comment, a `# Title`, an
    optional single-line italic subtitle, and then the body. Everything is ordinary markdown so
    the agent writes a deck without learning a format, and any markdown viewer can read it.
    """
    slides = []
    for i, chunk in enumerate(SEPARATOR.split(text)):
        if not chunk.strip():
            continue
        attrs = _parse_attrs(chunk)
        body = _ATTR.sub("", chunk).strip("\n")

        title = ""
        m = _H1.search(body)
        if m:
            title = m.group(1).strip()
            body = body[:m.start()] + body[m.end():]

        sub = ""
        m = _SUB.search(body.lstrip("\n")[:400])
        if m:
            lead = body.lstrip("\n")
            sub = m.group(1).strip()
            body = lead[:m.start()] + lead[m.end():]

        slides.append({
            # `v=`, `why=` and `resolves=` are all legacy attributes now, deliberately dropped
            # so an old deck self-cleans the first time it is rewritten. `resolves=` in
            # particular is dead on purpose: resolution is the user's act alone, in the app.
            "index": len(slides),
            "kind": attrs.get("kind", "setup"),
            "date": attrs.get("date", ""),
            "title": title,
            "sub": sub,
            "body": body.strip("\n"),
            "source": chunk.strip("\n"),
        })
    return slides


def render_deck(slides: list[dict]) -> str:
    """Inverse of `parse_deck`, used when an edit rewrites one slide in place."""
    out = []
    for s in slides:
        head = f"<!-- slide: kind={s.get('kind','setup')}, date={s.get('date','')} -->"
        parts = [head, f"# {s.get('title','')}".rstrip()]
        if s.get("sub"):
            parts.append(f"*{s['sub']}*")
        parts.append("")
        parts.append(s.get("body", ""))
        out.append("\n".join(parts).rstrip() + "\n")
    return "\n---\n\n".join(out)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def _index_path(slug: str) -> Path:
    return paths.bubble_talks_dir(slug) / "talks.yaml"


def load_index(slug: str) -> dict:
    return _read_yaml(_index_path(slug), {"talks": []})


def save_index(slug: str, data: dict) -> None:
    _write_yaml(_index_path(slug), data)


def _record(slug: str, talk_id: str) -> dict | None:
    for rec in load_index(slug).get("talks", []):
        if rec.get("id") == talk_id:
            return rec
    return None


def create_talk(slug: str, title: str, *, intent: str = "", kicker: str = "",
                date: str | None = None, body: str = "") -> str:
    """Register a new deck. `body` may be a full multi-slide markdown document."""
    date = date or _today()
    stem = slugify(title)[:60] or "talk"
    talk_id = f"{date}-{stem}"
    idx = load_index(slug)
    existing = {t.get("id") for t in idx.get("talks", [])}
    n = 2
    while talk_id in existing:
        talk_id = f"{date}-{stem}-{n}"
        n += 1
    idx.setdefault("talks", []).insert(0, {
        "id": talk_id, "title": title, "date": date,
        "intent": intent, "kicker": kicker, "landed": "", "created_at": _now_iso(),
    })
    save_index(slug, idx)
    _atomic_write(paths.bubble_talk_path(slug, talk_id), body or f"# {title}\n")
    return talk_id


def register_deck(slug: str, talk_id: str) -> dict:
    """Index a deck file that appeared on disk, or refresh what the index says about it.

    Everything the registry holds is readable from the deck: the date is the id's prefix, the
    title is the first slide's heading, the intent is its subtitle. Deriving them means an agent
    creates a talk by writing one file, with no second API to remember and nothing to leave
    inconsistent.
    """
    slides = parse_deck(read_deck(slug, talk_id))
    first = slides[0] if slides else {}
    m = re.match(r"^(\d{4}-\d{2}-\d{2})-(.*)$", talk_id)
    date = m.group(1) if m else _today()
    title = first.get("title") or (m.group(2).replace("-", " ").capitalize() if m else talk_id)

    idx = load_index(slug)
    for rec in idx.setdefault("talks", []):
        if rec.get("id") == talk_id:
            # Refresh only what is still blank. Re-deriving the title on every push renamed a
            # talk to its first slide's heading each time an agent touched the deck.
            rec["title"] = rec.get("title") or title
            rec["intent"] = rec.get("intent") or first.get("sub", "")
            rec["kicker"] = rec.get("kicker") or first.get("kind", "")
            save_index(slug, idx)
            return rec
    rec = {"id": talk_id, "title": title, "date": date, "intent": first.get("sub", ""),
           "kicker": first.get("kind", ""), "landed": "", "created_at": _now_iso()}
    idx["talks"].insert(0, rec)
    save_index(slug, idx)
    return rec


def delete_talk(slug: str, talk_id: str) -> bool:
    idx = load_index(slug)
    talks = [t for t in idx.get("talks", []) if t.get("id") != talk_id]
    if len(talks) == len(idx.get("talks", [])):
        return False
    idx["talks"] = talks
    save_index(slug, idx)
    for note_id in load_notes(slug, talk_id).get("notes", {}):
        note_image_path(slug, talk_id, note_id).unlink(missing_ok=True)
    for p in (paths.bubble_talk_path(slug, talk_id),
              paths.bubble_talk_notes_path(slug, talk_id),
              # versioning is gone; this only sweeps a leftover from decks that predate that
              paths.bubble_talk_path(slug, talk_id).with_name(f"{talk_id}.history.yaml")):
        p.unlink(missing_ok=True)
    return True


# --------------------------------------------------------------------------- #
# Note snapshots
# --------------------------------------------------------------------------- #
def _shot_name(talk_id: str, note_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{talk_id}__{note_id}")
    return f"{safe}.png"


def note_image_path(slug: str, talk_id: str, note_id: str) -> Path:
    return paths.bubble_talk_shots_dir(slug) / _shot_name(talk_id, note_id)


def save_note_image(slug: str, talk_id: str, note_id: str, data: bytes) -> str:
    """Store the rendered slide as the reader saw it, with the mark drawn on.

    A quote tells the agent *which words*; the picture tells it what the slide looked like —
    where a figure sat, what was next to what, whether the layout itself was the problem. Text
    anchors cannot carry any of that, and for a region mark on a plot the picture is the only
    thing that carries meaning at all.
    """
    path = note_image_path(slug, talk_id, note_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".png.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)

    notes = load_notes(slug, talk_id)
    note = notes.get("notes", {}).get(note_id)
    if note is not None:
        note["image"] = _shot_name(talk_id, note_id)
        save_notes(slug, talk_id, notes)
    return _shot_name(talk_id, note_id)


# --------------------------------------------------------------------------- #
# Notes (your marks)
# --------------------------------------------------------------------------- #
def load_notes(slug: str, talk_id: str) -> dict:
    return _read_yaml(paths.bubble_talk_notes_path(slug, talk_id), {"next_id": 1, "notes": {}})


def save_notes(slug: str, talk_id: str, data: dict) -> None:
    _write_yaml(paths.bubble_talk_notes_path(slug, talk_id), data)


def _find_occurrence(source: str, quote: str, occurrence: int) -> int:
    """Index of the k-th occurrence of `quote`, or the last one that exists, or -1."""
    i, found = -1, 0
    while found < max(1, occurrence):
        j = source.find(quote, i + 1)
        if j < 0:
            break
        i, found = j, found + 1
    return i


def _anchor_context(source: str, quote: str, occurrence: int = 1) -> tuple[str, str]:
    i = _find_occurrence(source, quote, occurrence)
    if i < 0:
        return "", ""
    return source[max(0, i - CONTEXT):i], source[i + len(quote):i + len(quote) + CONTEXT]


def resolve_anchor(source: str, note: dict) -> int:
    """Find a mark's quote in the current slide source. -1 when it no longer exists.

    Tried in order: the quote inside its original prefix/suffix context (survives the same
    sentence appearing twice), then the bare quote (survives edits *around* it), then nothing —
    at which point the note orphans, loudly.
    """
    quote = note.get("quote") or ""
    if not quote:
        return -1
    pre, suf = note.get("prefix") or "", note.get("suffix") or ""
    if pre or suf:
        i = source.find(f"{pre}{quote}{suf}")
        if i >= 0:
            return i + len(pre)
    return source.find(quote)


def _clean_covers(covers) -> list | None:
    """What the ink or box actually touched, in the reviewer's own layout.

    Sampled client-side at pin time — the server never sees the rendering, so this is the one
    chance to catch it. It is the text fallback that keeps a drawing legible to an agent that
    cannot open images."""
    if not covers:
        return None
    out = [str(c)[:200] for c in list(covers)[:200] if str(c).strip()]
    return out or None


def _clean_paths(paths) -> list | None:
    """Normalised freehand strokes: a list of polylines of {x, y} percents of the slide box.

    Bounded and rounded here so a scribble cannot balloon the sidecar, and so the stored
    numbers are stable however the browser sampled the pointer.
    """
    if not paths:
        return None
    out = []
    for stroke in list(paths)[:200]:
        pts = []
        for pt in list(stroke)[:600]:
            try:
                x, y = float(pt["x"]), float(pt["y"])
            except (TypeError, KeyError, ValueError):
                continue
            pts.append({"x": round(max(0.0, min(100.0, x)), 2),
                        "y": round(max(0.0, min(100.0, y)), 2)})
        if len(pts) >= 2:
            out.append(pts)
    return out or None


def add_note(slug: str, talk_id: str, *, slide: int, kind: str, author: str,
             quote: str = "", text: str = "", rect: dict | None = None,
             paths: list | None = None, covers: list | None = None,
             occurrence: int = 1) -> dict:
    if kind not in KINDS:
        raise ValueError(f"unknown mark: {kind}")
    paths = _clean_paths(paths)
    covers = _clean_covers(covers)
    if not quote and not rect and not paths:
        raise ValueError("a note must anchor to a quote, a region, or a drawing")

    slides = parse_deck(read_deck(slug, talk_id))
    source = slides[slide]["source"] if 0 <= slide < len(slides) else ""
    pre, suf = _anchor_context(source, quote, occurrence) if quote else ("", "")

    data = load_notes(slug, talk_id)
    nid = f"n{data.get('next_id', 1)}"
    data["next_id"] = data.get("next_id", 1) + 1
    now = _now_iso()
    data.setdefault("notes", {})[nid] = {
        "id": nid, "slide": slide, "kind": kind,
        "quote": quote, "prefix": pre, "suffix": suf, "rect": rect or None,
        "paths": paths, "covers": covers,
        "author": author, "created_at": now, "image": "",
        # A mark is the opening of a conversation, not a one-shot. The kind and the anchor are
        # fixed; what gets said about them is a thread.
        "messages": ([{"id": "m1", "author": author, "body": text,
                       "created_at": now, "edited_at": ""}] if text else []),
    }
    save_notes(slug, talk_id, data)
    return data["notes"][nid]


def reply_note(slug: str, talk_id: str, note_id: str, author: str, body: str,
               *, source_key: str = "", agent: bool = False) -> dict:
    """Add a turn to a mark's thread — yours or the agent's."""
    body = str(body or "").strip()
    if not body:
        raise ValueError("empty reply")
    data = load_notes(slug, talk_id)
    note = data.get("notes", {}).get(note_id)
    if not note:
        raise KeyError(note_id)
    msgs = note.setdefault("messages", [])
    # A worker can retry after a dropped response.  Do not turn that transport retry into a
    # second identical answer in the user's thread.
    if source_key and any(m.get("source_key") == source_key for m in msgs):
        return note
    message = {"id": f"m{len(msgs) + 1}", "author": author, "body": body,
               "created_at": _now_iso(), "edited_at": ""}
    if source_key:
        message["source_key"] = source_key
    if agent:
        message["agent"] = True
    msgs.append(message)
    save_notes(slug, talk_id, data)
    return note


def edit_note(slug: str, talk_id: str, note_id: str, text: str, *, author: str = "") -> dict:
    """Reword the author's **last** turn, and only that.

    Editing anything further back would rewrite a conversation the other side has already
    answered — the reply below would then be responding to words that no longer exist. The kind
    and the anchor are never editable: they are what the mark *is*.
    """
    data = load_notes(slug, talk_id)
    note = data.get("notes", {}).get(note_id)
    if not note:
        raise KeyError(note_id)
    msgs = note.setdefault("messages", [])
    mine = [m for m in msgs if not author or m.get("author") == author]
    if not mine:
        msgs.append({"id": f"m{len(msgs) + 1}", "author": author,
                     "body": str(text or "").strip(), "created_at": _now_iso(), "edited_at": ""})
    else:
        last = mine[-1]
        if msgs[-1] is not last:
            raise ValueError("only the last message in a thread can be edited")
        last["body"] = str(text or "").strip()
        last["edited_at"] = _now_iso()
    save_notes(slug, talk_id, data)
    return note


def delete_note(slug: str, talk_id: str, note_id: str) -> bool:
    data = load_notes(slug, talk_id)
    if note_id not in data.get("notes", {}):
        return False
    del data["notes"][note_id]
    save_notes(slug, talk_id, data)
    note_image_path(slug, talk_id, note_id).unlink(missing_ok=True)
    return True


# --------------------------------------------------------------------------- #
# Reading and resolving
# --------------------------------------------------------------------------- #
def read_deck(slug: str, talk_id: str) -> str:
    p = paths.bubble_talk_path(slug, talk_id)
    return p.read_text() if p.exists() else ""


def resolve_marks(slug: str, talk_id: str, note_ids: list[str]) -> list[str]:
    """Delete the named marks — the whole of what resolution is, and the user's act alone.

    No agent-reachable path calls this: a pushed deck cannot delete a mark, only answer it.
    The one thing an agent's edit can do to a mark is strand it — remove the text it pointed
    at and it goes orphan, loudly, still visible. Deleting it happens in the app, by the
    person who made it, once the answer satisfies them.
    """
    notes = load_notes(slug, talk_id)
    dropped = []
    for note_id in (note_ids or []):
        if note_id in notes.get("notes", {}):
            del notes["notes"][note_id]
            note_image_path(slug, talk_id, note_id).unlink(missing_ok=True)
            dropped.append(note_id)
    if dropped:
        save_notes(slug, talk_id, notes)
    return dropped


# --------------------------------------------------------------------------- #
# Manual editing (the human's pen)
# --------------------------------------------------------------------------- #
# A slide is agent-authored, but the deck is a conversation — and sometimes the fastest reply
# is to edit the slide yourself. Manual editing shows the slide's markdown with every open mark
# materialised as a `<comment-begin=id>…<comment-end=id>` pair, exactly the syntax report pages
# use, so the text a mark points at is visible and *moves with your edit* instead of silently
# orphaning. The tags exist only in the editing surface: storage stays quote-anchored, which is
# what the agent-facing feedback file is built from.
_TAG_TOKEN = re.compile(r"<comment-(begin|end)=([A-Za-z0-9_-]+)>")


def _parse_wrappers(text: str) -> tuple[str, list[dict]]:
    """Strip `<comment-begin=id>body<comment-end=id>` pairs; return (clean_text, entries).

    Each entry is {id, body, start}, `start` being the body's offset in the *clean* text so
    the caller can recompute prefix/suffix for exactly the occurrence the tags marked. The
    tags pair by id, so a body may contain any braces at all — the flaw that retired the old
    brace-counted syntax. A begin without its end is left in the text untouched: an editor
    surface must never eat characters it merely failed to understand.
    """
    # Marks can overlap without being nested: A may start before B but end before B does.
    # Treat tags as independent range endpoints instead of recursively consuming one wrapper at
    # a time; otherwise B's end leaks into the editor and only the first mark survives a save.
    tokens = list(_TAG_TOKEN.finditer(text))
    by_id: dict[str, dict[str, list]] = {}
    for token in tokens:
        kind, nid = token.group(1), token.group(2)
        by_id.setdefault(nid, {"begin": [], "end": []})[kind].append(token)
    valid = {
        nid for nid, ends in by_id.items()
        if len(ends["begin"]) == len(ends["end"]) == 1
        and ends["begin"][0].start() < ends["end"][0].start()
    }

    out: list[str] = []
    starts: dict[str, int] = {}
    finished: dict[str, int] = {}
    order: list[str] = []
    cursor, clean_len = 0, 0
    for token in tokens:
        raw = text[cursor:token.start()]
        out.append(raw); clean_len += len(raw)
        kind, nid = token.group(1), token.group(2)
        if nid not in valid:
            # A malformed wrapper must remain visible rather than silently eating source.
            out.append(token.group(0)); clean_len += len(token.group(0))
        elif kind == "begin":
            starts[nid] = clean_len; order.append(nid)
        else:
            finished[nid] = clean_len
        cursor = token.end()
    out.append(text[cursor:])
    clean = "".join(out)
    found = [{"id": nid, "body": clean[starts[nid]:finished[nid]], "start": starts[nid]}
             for nid in order if nid in finished]
    return clean, found


def _wrap_notes(source: str, notes: list[dict]) -> str:
    """Inject `<comment-begin=id>quote<comment-end=id>` at each anchor.

    A slide can have crossing ranges. The serialized tags deliberately preserve those two
    independent ranges; :func:`_parse_wrappers` reads them back as such on save.
    """
    spans = []
    for n in notes:
        quote = n.get("quote") or ""
        if not quote:
            continue
        pos = resolve_anchor(source, n)
        if pos >= 0:
            spans.append((pos, pos + len(quote), n["id"]))
    if not spans:
        return source
    starts: dict[int, list[tuple[int, str]]] = {}
    ends: dict[int, list[tuple[int, str]]] = {}
    for start, end, nid in spans:
        starts.setdefault(start, []).append((end, nid))
        ends.setdefault(end, []).append((start, nid))
    out: list[str] = []
    cursor = 0
    for pos in sorted(set(starts) | set(ends)):
        out.append(source[cursor:pos])
        # Adjacent marks must stay adjacent; at a shared boundary close before opening. For
        # true nesting, close the innermost first and open the outermost first.
        for _, nid in sorted(ends.get(pos, []), reverse=True):
            out.append(f"<comment-end={nid}>")
        for _, nid in sorted(starts.get(pos, []), reverse=True):
            out.append(f"<comment-begin={nid}>")
        cursor = pos
    out.append(source[cursor:])
    return "".join(out)


def slide_edit_source(slug: str, talk_id: str, slides: list[dict], notes: dict, index: int) -> str:
    """The text the editor opens: this slide's markdown, marks materialised as wrappers."""
    mine = [n for n in notes.values() if n.get("slide") == index and n.get("quote")]
    wrapped = _wrap_notes(slides[index]["source"], mine)
    return _ATTR.sub("", wrapped, count=1).lstrip("\n")


def apply_slide_source(slug: str, talk_id: str, index: int, text: str, *,
                       kind: str | None = None) -> dict:
    """Save a hand-edited slide in place, updating every mark whose wrapper came back.

    Marks whose wrappers survive get their quote and context re-anchored to the new text; a
    wrapper the editor deleted leaves its mark alone, to orphan loudly if its text is truly
    gone. No version is kept — the slide is a living surface and the current text is the record.
    """
    clean, wraps = _parse_wrappers(str(text or "").replace("\r\n", "\n"))
    if SEPARATOR.search(clean):
        raise ValueError("a slide cannot contain a --- separator line; use add slide instead")
    if not clean.strip():
        raise ValueError("the slide is empty — delete it instead")

    slides = parse_deck(read_deck(slug, talk_id))
    if not 0 <= index < len(slides):
        raise IndexError(index)
    old = slides[index]
    kind = _slide_kind(kind) if kind else old["kind"]
    head = f"<!-- slide: kind={kind}, date={old.get('date', '')} -->"
    parsed = parse_deck(head + "\n" + clean.strip("\n") + "\n")
    if not parsed:
        raise ValueError("could not parse the slide")
    new = parsed[0]

    changed = ((new["title"], new["sub"], new["body"]) != (old["title"], old["sub"], old["body"])
               or kind != old["kind"])
    if changed:
        new["date"] = _today()

    slides[index] = {**old, "kind": kind,
                     **{k: new[k] for k in ("title", "sub", "body", "date")}}
    _atomic_write(paths.bubble_talk_path(slug, talk_id), render_deck(slides))

    if wraps:
        data = load_notes(slug, talk_id)
        for w in wraps:
            note = data.get("notes", {}).get(w["id"])
            if note is None or note.get("slide") != index:
                continue
            start, body = w["start"], w["body"]
            note["quote"] = body
            note["prefix"] = clean[max(0, start - CONTEXT):start]
            note["suffix"] = clean[start + len(body):start + len(body) + CONTEXT]
        save_notes(slug, talk_id, data)

    # Renaming the first slide by hand renames the talk — the registry title exists so agent
    # pushes cannot rename it out from under you, not so your own rename is ignored.
    if index == 0 and changed and new["title"] != old["title"]:
        idx = load_index(slug)
        for rec in idx.get("talks", []):
            if rec.get("id") == talk_id and rec.get("title") in ("", old["title"]):
                rec["title"] = new["title"]
                if rec.get("intent") in ("", old["sub"]):
                    rec["intent"] = new["sub"]
                save_index(slug, idx)
                break
    return slides[index]


def _shift_slide_refs(slug: str, talk_id: str, at: int, delta: int) -> None:
    """Re-point notes after a slide is inserted (+1) or removed (-1) at `at`."""
    notes = load_notes(slug, talk_id)
    moved = False
    for n in notes.get("notes", {}).values():
        if n.get("slide", 0) >= at:
            n["slide"] = n["slide"] + delta
            moved = True
    if moved:
        save_notes(slug, talk_id, notes)


def insert_slide(slug: str, talk_id: str, after: int) -> int:
    """Insert a blank slide after `after` (−1 for the front). Returns the new index."""
    slides = parse_deck(read_deck(slug, talk_id))
    pos = max(0, min(len(slides), after + 1))
    slides.insert(pos, {"index": pos, "kind": "setup", "date": _today(),
                        "title": "New slide", "sub": "", "body": "", "source": ""})
    _shift_slide_refs(slug, talk_id, pos, +1)
    _atomic_write(paths.bubble_talk_path(slug, talk_id), render_deck(slides))
    return pos


def delete_slide(slug: str, talk_id: str, index: int) -> bool:
    """Remove one slide — and with it every mark and snapshot it carried."""
    slides = parse_deck(read_deck(slug, talk_id))
    if not 0 <= index < len(slides):
        return False
    del slides[index]
    _atomic_write(paths.bubble_talk_path(slug, talk_id), render_deck(slides))

    notes = load_notes(slug, talk_id)
    for nid, n in list(notes.get("notes", {}).items()):
        if n.get("slide") == index:
            del notes["notes"][nid]
            note_image_path(slug, talk_id, nid).unlink(missing_ok=True)
    save_notes(slug, talk_id, notes)
    _shift_slide_refs(slug, talk_id, index, -1)
    return True


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
def talk_revision(slug: str, talk_id: str) -> str:
    """A lightweight identity for the deck and its review sidecar.

    The browser uses this to notice that a Scientist (or another collaborator) has
    changed a chalk talk while it is open.  Hash the stored bytes, rather than the
    parsed representation, so a newly-written remote file is always observable.
    """
    digest = hashlib.sha256()
    for path in (paths.bubble_talk_path(slug, talk_id),
                 paths.bubble_talk_notes_path(slug, talk_id)):
        try:
            digest.update(path.read_bytes())
        except FileNotFoundError:
            digest.update(b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def talk_detail(slug: str, talk_id: str) -> dict:
    rec = _record(slug, talk_id)
    if rec is None:
        raise KeyError(talk_id)
    slides = parse_deck(read_deck(slug, talk_id))
    notes = load_notes(slug, talk_id).get("notes", {})

    out_notes = []
    for note in sorted(notes.values(), key=lambda n: n.get("created_at", "")):
        # ``source`` includes the title and optional subtitle as well as the body. A title is
        # part of the argument a chalk talk makes, so marks on it must resolve and paint just
        # like marks on a paragraph below it.
        src = slides[note["slide"]]["source"] if 0 <= note["slide"] < len(slides) else ""
        n = dict(note)
        pos = resolve_anchor(src, note) if note.get("quote") else -1
        n["orphan"] = bool(note.get("quote")) and pos < 0
        if pos >= 0:
            # Which occurrence of the quote the anchor sits on — the painter must not light
            # up the first repeat of a sentence that was marked in its third.
            n["occurrence"] = src[:pos].count(note["quote"]) + 1
        out_notes.append(n)

    for s in slides:
        # What the ✎ edit toggle opens: the slide's own markdown, with every open mark
        # materialised as the same `<comment-begin=id>…<comment-end=id>` pair report pages use.
        s["edit_source"] = slide_edit_source(slug, talk_id, slides, notes, s["index"])
    return {"talk": rec, "slides": slides, "notes": out_notes,
            "revision": talk_revision(slug, talk_id),
            "open": len(out_notes)}


def list_talks(slug: str) -> list[dict]:
    """Newest first. A talk's date is the story's order, not the order it was filed."""
    out = []
    for rec in sorted(load_index(slug).get("talks", []),
                      key=lambda r: (r.get("date", ""), r.get("created_at", "")), reverse=True):
        tid = rec["id"]
        notes = load_notes(slug, tid).get("notes", {}).values()
        out.append({**rec,
                    "slides": len(parse_deck(read_deck(slug, tid))),
                    "open": len(list(notes)),
                    "notes": len(list(notes))})
    return out


def open_notes_for_agent(slug: str) -> list[dict]:
    """Exactly what a Scientist worker receives, riding the manifest poll it already makes.

    Deliberately free of anything browser-shaped: no HTML, no pixels, no DOM. A text mark is a
    quote the agent can `grep` in its own file; a region mark names the figure and a normalised
    rectangle, and leans on the human's words plus the slide source for meaning.
    """
    out = []
    for rec in load_index(slug).get("talks", []):
        tid = rec["id"]
        slides = parse_deck(read_deck(slug, tid))
        for note in load_notes(slug, tid).get("notes", {}).values():
            i = note.get("slide", 0)
            s = slides[i] if 0 <= i < len(slides) else {}
            item = {
                "talk": tid, "talk_title": rec.get("title", ""),
                "note_id": note["id"], "slide": i,
                "slide_title": s.get("title", ""), "slide_source": s.get("source", ""),
                "mark": note["kind"], "means": KINDS[note["kind"]]["means"],
                "conversation": [{"by": m.get("author", ""), "said": m.get("body", "")}
                                 for m in note.get("messages", [])],
            }
            if note.get("image"):
                item["screenshot"] = {
                    "file": f"talks/shots/{note['image']}",
                    "url": f"/api/bubbles/{slug}/talks/{tid}/notes/{note['id']}/shot.png",
                    "shows": "the slide as the reviewer saw it, with this mark drawn on",
                }
            if note.get("quote"):
                item["anchor"] = {"type": "text", "quote": note["quote"],
                                  "prefix": note.get("prefix", ""), "suffix": note.get("suffix", ""),
                                  "resolves": resolve_anchor(s.get("source", ""), note) >= 0}
            elif note.get("paths"):
                # The strokes themselves are browser-geometry noise to an agent; the snapshot
                # with them drawn on is the message, and `touches` is its text fallback.
                item["anchor"] = {"type": "drawing", "strokes": len(note["paths"])}
            else:
                item["anchor"] = {"type": "region", **(note.get("rect") or {})}
            if note.get("covers"):
                item["anchor"]["touches"] = note["covers"]
            out.append(item)
    return out


def absorb_push(slug: str, talk_id: str, text: str, *, actor: str) -> None:
    """Take a deck an agent pushed. The slide becomes what was pushed — nothing more.

    Deliberately powerless over marks: an agent can edit the text a mark points at (which may
    strand it as a loud orphan) and reply in its thread, but it can never delete one. A
    `resolves=` attribute in a pushed header — the old mechanism, or an agent trying its luck —
    is parsed and discarded without effect.
    """
    replies = []
    for match in _REPLY.finditer(text):
        note_id, body = match.group(1), match.group(2).strip()
        if not body:
            raise ValueError(f"reply for {note_id} is empty")
        replies.append((note_id, body))
    # Validate before changing either the deck or a thread: a typo must be a clean rejected
    # push, not a partially-applied edit.
    notes = load_notes(slug, talk_id).get("notes", {})
    unknown = [note_id for note_id, _ in replies if note_id not in notes]
    if unknown:
        raise ValueError("no such chalk-talk mark: " + ", ".join(sorted(set(unknown))))
    text = _REPLY.sub("", text)

    old = {s["index"]: s for s in parse_deck(read_deck(slug, talk_id))}
    new = parse_deck(text)
    for slide in new:
        was = old.get(slide["index"])
        edited = was is not None and (was["body"] != slide["body"] or was["title"] != slide["title"])
        if edited:
            slide["date"] = _today()
    _atomic_write(paths.bubble_talk_path(slug, talk_id), render_deck(new))
    for note_id, body in replies:
        fingerprint = hashlib.sha256(
            f"{talk_id}\0{note_id}\0{body}".encode("utf-8")
        ).hexdigest()
        reply_note(slug, talk_id, note_id, f"agent on behalf of {actor}", body,
                   source_key="scientist:" + fingerprint, agent=True)


# --------------------------------------------------------------------------- #
# What a syncing Scientist worker puts in the project
# --------------------------------------------------------------------------- #
def _region_phrase(rect: dict, *, has_picture: bool) -> str:
    """Describe a region mark, and promise a picture only when one exists.

    The snapshot is captured in the browser, so a mark made any other way — or one whose capture
    failed offline — has none. Saying "see the picture" regardless sent a live agent hunting for
    a file that was never there and reporting it as a blocker.
    """
    where = (f"a region of the slide at x {rect.get('x', 0):.0f}–{rect.get('x', 0) + rect.get('w', 0):.0f}%, "
             f"y {rect.get('y', 0):.0f}–{rect.get('y', 0) + rect.get('h', 0):.0f}%")
    return where + (" (see the picture — it is drawn on)" if has_picture
                    else " — no picture was captured for this one, so go by the coordinates and "
                         "the comment")


def _ink_phrase(note: dict) -> str:
    n = len(note.get("paths") or [])
    base = f"a freehand drawing over the whole slide ({n} stroke{'s' if n != 1 else ''})"
    return base + (" — open the picture: the strokes ARE the feedback. Crossed-out text wants "
                   "rewriting, arrows want things moved, circles want attention or expansion, "
                   "handwriting wants reading."
                   if note.get("image")
                   else " — no picture was captured for it, so go by the comment")


def feedback_blocks(slug: str) -> list[str]:
    """The chalk-talk half of `feedback/OPEN.md`. See `feedback.py` for the whole document."""
    blocks = []
    for rec in sorted(load_index(slug).get("talks", []),
                      key=lambda r: r.get("date", ""), reverse=True):
        tid = rec["id"]
        slides = parse_deck(read_deck(slug, tid))
        notes = list(load_notes(slug, tid).get("notes", {}).values())
        if not notes:
            continue
        blocks.append(f"## {rec.get('title', tid)}  *(chalk talk, {rec.get('date', '')})*\n\n"
                      f"`reports/talks/{tid}.md`\n")
        for note in sorted(notes, key=lambda n: (n.get("slide", 0), n.get("created_at", ""))):
            i = note.get("slide", 0)
            sl = slides[i] if 0 <= i < len(slides) else {}
            k = KINDS.get(note["kind"], {})
            if note.get("quote"):
                where = f"the text `{note['quote']}`"
            elif note.get("paths"):
                where = _ink_phrase(note)
            else:
                where = _region_phrase(note.get("rect") or {},
                                       has_picture=bool(note.get("image")))
            touches = note.get("covers") or []
            lines = [f"### {k.get('glyph','')} {k.get('means','')} — slide {i + 1}: "
                     f"{sl.get('title','')}",
                     "",
                     f"- **id**: `{note['id']}` — answer it; only the user can remove it, "
                     f"in the app",
                     f"- **on**: {where}",
                     ]
            if touches:
                joined = "; ".join(f"“{t}”" for t in touches[:12])
                lines.append(f"- **the ink touches**: {joined} — sampled from the reviewer's "
                             f"screen; if you cannot open images, this is what the drawing "
                             f"sits on")
            if note.get("image"):
                lines.append(f"- **picture**: `feedback/shots/{_shot_name(tid, note['id'])}` — "
                             f"the slide as the reviewer saw it, with this mark drawn on. Open "
                             f"it: it shows layout and placement the text cannot.")
            for m in note.get("messages", []):
                if m.get("body"):
                    lines += ["", f"**{m.get('author','')}** — {m['body']}"]
            blocks.append("\n".join(lines) + "\n")
    return blocks


def open_note_images(slug: str) -> dict:
    """`feedback/shots/<talk>__<note>.png` -> the stored snapshot, for open marks only.

    The name is talk-qualified because note ids restart at n1 in every deck — flat names
    collided and shipped the wrong picture."""
    out = {}
    for rec in load_index(slug).get("talks", []):
        tid = rec["id"]
        for note in load_notes(slug, tid).get("notes", {}).values():
            if not note.get("image"):
                continue
            path = note_image_path(slug, tid, note["id"])
            if path.is_file():
                out[f"feedback/shots/{_shot_name(tid, note['id'])}"] = path
    return out
