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
}
KIND_ORDER = ["bad", "q", "more", "good", "cut"]

SLIDE_KINDS = ["setup", "derivation", "evidence", "comparison", "implementation", "ask"]

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


def _parse_attrs(chunk: str) -> dict:
    m = _ATTR.search(chunk)
    if not m:
        return {}
    out = {}
    # Split only on commas that begin the next `key=`, so a list-valued attribute
    # (`resolves=n1,n2` — which is what an agent writes) survives intact.
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
            **{k: v for k, v in attrs.items() if k in ("resolves", "why")},
            "index": len(slides),
            "kind": attrs.get("kind", "setup"),
            "date": attrs.get("date", ""),
            "version": int(attrs.get("v", 1) or 1),
            "title": title,
            "sub": sub,
            "body": body.strip("\n"),
            "source": chunk.strip("\n"),
        })
    return slides


def render_deck(slides: list[dict]) -> str:
    """Inverse of `parse_deck`, used when a revision rewrites one slide in place."""
    out = []
    for s in slides:
        head = f"<!-- slide: kind={s.get('kind','setup')}, date={s.get('date','')}, v={s.get('version',1)} -->"
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
              paths.bubble_talk_history_path(slug, talk_id)):
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


def _anchor_context(source: str, quote: str) -> tuple[str, str]:
    i = source.find(quote)
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


def add_note(slug: str, talk_id: str, *, slide: int, kind: str, author: str,
             quote: str = "", text: str = "", rect: dict | None = None,
             version: int = 1) -> dict:
    if kind not in KINDS:
        raise ValueError(f"unknown mark: {kind}")
    if not quote and not rect:
        raise ValueError("a note must anchor to a quote or a region")

    slides = parse_deck(read_deck(slug, talk_id))
    source = slides[slide]["source"] if 0 <= slide < len(slides) else ""
    pre, suf = _anchor_context(source, quote) if quote else ("", "")

    data = load_notes(slug, talk_id)
    nid = f"n{data.get('next_id', 1)}"
    data["next_id"] = data.get("next_id", 1) + 1
    now = _now_iso()
    data.setdefault("notes", {})[nid] = {
        "id": nid, "slide": slide, "version": version, "kind": kind,
        "quote": quote, "prefix": pre, "suffix": suf, "rect": rect or None,
        "author": author, "created_at": now, "image": "",
        # A mark is the opening of a conversation, not a one-shot. The kind and the anchor are
        # fixed; what gets said about them is a thread.
        "messages": ([{"id": "m1", "author": author, "body": text,
                       "created_at": now, "edited_at": ""}] if text else []),
    }
    save_notes(slug, talk_id, data)
    return data["notes"][nid]


def reply_note(slug: str, talk_id: str, note_id: str, author: str, body: str) -> dict:
    """Add a turn to a mark's thread — yours or the agent's."""
    body = str(body or "").strip()
    if not body:
        raise ValueError("empty reply")
    data = load_notes(slug, talk_id)
    note = data.get("notes", {}).get(note_id)
    if not note:
        raise KeyError(note_id)
    msgs = note.setdefault("messages", [])
    msgs.append({"id": f"m{len(msgs) + 1}", "author": author, "body": body,
                 "created_at": _now_iso(), "edited_at": ""})
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
# Revisions
# --------------------------------------------------------------------------- #
def read_deck(slug: str, talk_id: str) -> str:
    p = paths.bubble_talk_path(slug, talk_id)
    return p.read_text() if p.exists() else ""


def load_history(slug: str, talk_id: str) -> dict:
    return _read_yaml(paths.bubble_talk_history_path(slug, talk_id), {"versions": []})


def revise_slide(slug: str, talk_id: str, slide: int, *, body: str, why: str,
                 title: str | None = None, sub: str | None = None,
                 resolves: "list[str] | None" = None) -> dict:
    """Replace one slide's body, snapshotting the old one with *why* it changed.

    `why` is the point of the whole record: a revision that does not say which mark caused it
    is just a diff, and a diff does not tell you later why the argument has the shape it has.

    `resolves` names the marks this revision answers, and they are **deleted** — sidecar entry
    and snapshot both. That is deliberate. An addressed mark that lingers is clutter: it sits in
    every future agent's context, is re-read on every sync, and says nothing the revision does
    not already say better. The durable record is the version history, which keeps *what the
    mark asked for* in `marks`; the mark itself is working state and working state should end.
    """
    slides = parse_deck(read_deck(slug, talk_id))
    if not 0 <= slide < len(slides):
        raise IndexError(slide)
    old = slides[slide]

    notes = load_notes(slug, talk_id)
    consumed = []
    for note_id in (resolves or []):
        note = notes.get("notes", {}).get(note_id)
        if note is None:
            continue
        said = [m.get("body", "") for m in note.get("messages", []) if m.get("body")]
        consumed.append({"mark": note["kind"], "means": KINDS[note["kind"]]["means"],
                         "quote": note.get("quote", ""), "comment": said[0] if said else "",
                         "conversation": said, "region": note.get("rect") or None,
                         "by": note.get("author", "")})

    hist = load_history(slug, talk_id)
    hist.setdefault("versions", []).append({
        "slide": slide, "version": old["version"], "date": old.get("date", ""),
        "title": old["title"], "sub": old["sub"], "body": old["body"],
        "why": why, "marks": consumed, "superseded_at": _now_iso(),
    })
    _write_yaml(paths.bubble_talk_history_path(slug, talk_id), hist)

    for note_id in (resolves or []):
        if note_id in notes.get("notes", {}):
            del notes["notes"][note_id]
            note_image_path(slug, talk_id, note_id).unlink(missing_ok=True)
    if resolves:
        save_notes(slug, talk_id, notes)

    slides[slide] = {**old, "version": old["version"] + 1, "date": _today(),
                     "title": title if title is not None else old["title"],
                     "sub": sub if sub is not None else old["sub"],
                     "body": body}
    _atomic_write(paths.bubble_talk_path(slug, talk_id), render_deck(slides))
    return slides[slide]


# --------------------------------------------------------------------------- #
# Manual editing (the human's pen)
# --------------------------------------------------------------------------- #
# A slide is agent-authored, but the deck is a conversation — and sometimes the fastest reply
# is to edit the slide yourself. Manual editing shows the slide's markdown with every open mark
# materialised as a `\comment{id}{...}` wrapper, exactly the syntax report pages use, so the
# text a mark points at is visible and *moves with your edit* instead of silently orphaning.
# The wrappers exist only in the editing surface: storage stays quote-anchored, which is what
# the agent-facing feedback file is built from.
_WRAP_TOKEN = "\\comment{"
_WRAP_ID = re.compile(r"[A-Za-z0-9_-]+\Z")


def _parse_wrappers(text: str) -> tuple[str, list[dict]]:
    r"""Strip `\comment{id}{body}` wrappers; return (clean_text, [{id, body, start}]).

    `start` is the body's offset in the *clean* text, so the caller can recompute the
    prefix/suffix context for exactly the occurrence the wrapper marked — `str.find` would
    quietly pick the first repeat instead. Malformed wrappers are left in the text untouched:
    an editor surface must never eat characters it merely failed to understand.
    """
    out: list[str] = []
    found: list[dict] = []
    i, clean_len = 0, 0
    while True:
        j = text.find(_WRAP_TOKEN, i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        clean_len += j - i
        id_start = j + len(_WRAP_TOKEN)
        id_end = text.find("}", id_start)
        nid = text[id_start:id_end] if id_end > 0 else ""
        if id_end < 0 or not _WRAP_ID.match(nid) or text[id_end + 1:id_end + 2] != "{":
            out.append(text[j:id_start])
            clean_len += id_start - j
            i = id_start
            continue
        depth, k = 1, id_end + 2
        while k < len(text) and depth:
            if text[k] == "{":
                depth += 1
            elif text[k] == "}":
                depth -= 1
            if depth:
                k += 1
        if depth:                       # unclosed — leave the raw text alone
            out.append(text[j:id_start])
            clean_len += id_start - j
            i = id_start
            continue
        body = text[id_end + 2:k]
        found.append({"id": nid, "body": body, "start": clean_len})
        out.append(body)
        clean_len += len(body)
        i = k + 1
    return "".join(out), found


def _wrap_notes(source: str, notes: list[dict]) -> str:
    r"""Inject `\comment{id}{quote}` at each note's anchor. Overlaps keep the first mark only."""
    spans = []
    for n in notes:
        quote = n.get("quote") or ""
        if not quote:
            continue
        pos = resolve_anchor(source, n)
        if pos >= 0:
            spans.append((pos, pos + len(quote), n["id"]))
    spans.sort()
    kept, last_end = [], -1
    for span in spans:
        if span[0] >= last_end:
            kept.append(span)
            last_end = span[1]
    out = source
    for start, end, nid in reversed(kept):
        out = f"{out[:start]}\\comment{{{nid}}}{{{out[start:end]}}}{out[end:]}"
    return out


def slide_edit_source(slug: str, talk_id: str, slides: list[dict], notes: dict, index: int) -> str:
    """The text the editor opens: this slide's markdown, marks materialised as wrappers."""
    mine = [n for n in notes.values() if n.get("slide") == index and n.get("quote")]
    wrapped = _wrap_notes(slides[index]["source"], mine)
    return _ATTR.sub("", wrapped, count=1).lstrip("\n")


def apply_slide_source(slug: str, talk_id: str, index: int, text: str, *, why: str = "",
                       kind: str | None = None) -> dict:
    """Save a hand-edited slide, updating every mark whose wrapper came back.

    A changed slide is versioned and snapshotted exactly like an agent revision — the history
    must not care whose hand held the pen. Marks whose wrappers survive get their quote and
    context re-anchored to the new text; a wrapper the editor deleted leaves its mark alone, to
    orphan loudly if its text is truly gone.
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
    if kind and kind not in SLIDE_KINDS:
        raise ValueError(f"kind must be one of {', '.join(SLIDE_KINDS)}")
    kind = kind or old["kind"]
    head = f"<!-- slide: kind={kind}, date={old.get('date', '')}, v={old['version']} -->"
    parsed = parse_deck(head + "\n" + clean.strip("\n") + "\n")
    if not parsed:
        raise ValueError("could not parse the slide")
    new = parsed[0]

    changed = ((new["title"], new["sub"], new["body"]) != (old["title"], old["sub"], old["body"])
               or kind != old["kind"])
    if changed:
        hist = load_history(slug, talk_id)
        hist.setdefault("versions", []).append({
            "slide": index, "version": old["version"], "date": old.get("date", ""),
            "title": old["title"], "sub": old["sub"], "body": old["body"],
            "why": why.strip() or "edited by hand", "marks": [],
            "superseded_at": _now_iso(),
        })
        _write_yaml(paths.bubble_talk_history_path(slug, talk_id), hist)
        new["version"] = old["version"] + 1
        new["date"] = _today()

    slides[index] = {**old, "kind": kind,
                     **{k: new[k] for k in ("title", "sub", "body", "version", "date")}}
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
    """Re-point notes and history after a slide is inserted (+1) or removed (-1) at `at`."""
    notes = load_notes(slug, talk_id)
    moved = False
    for n in notes.get("notes", {}).values():
        if n.get("slide", 0) >= at:
            n["slide"] = n["slide"] + delta
            moved = True
    if moved:
        save_notes(slug, talk_id, notes)
    hist = load_history(slug, talk_id)
    moved = False
    for v in hist.get("versions", []):
        if v.get("slide", 0) >= at:
            v["slide"] = v["slide"] + delta
            moved = True
    if moved:
        _write_yaml(paths.bubble_talk_history_path(slug, talk_id), hist)


def insert_slide(slug: str, talk_id: str, after: int) -> int:
    """Insert a blank slide after `after` (−1 for the front). Returns the new index."""
    slides = parse_deck(read_deck(slug, talk_id))
    pos = max(0, min(len(slides), after + 1))
    slides.insert(pos, {"index": pos, "kind": "setup", "date": _today(), "version": 1,
                        "title": "New slide", "sub": "", "body": "", "source": ""})
    _shift_slide_refs(slug, talk_id, pos, +1)
    _atomic_write(paths.bubble_talk_path(slug, talk_id), render_deck(slides))
    return pos


def delete_slide(slug: str, talk_id: str, index: int) -> bool:
    """Remove one slide — and with it every mark, snapshot and history entry it carried."""
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

    hist = load_history(slug, talk_id)
    hist["versions"] = [v for v in hist.get("versions", []) if v.get("slide") != index]
    _write_yaml(paths.bubble_talk_history_path(slug, talk_id), hist)
    _shift_slide_refs(slug, talk_id, index, -1)
    return True


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
def talk_detail(slug: str, talk_id: str) -> dict:
    rec = _record(slug, talk_id)
    if rec is None:
        raise KeyError(talk_id)
    slides = parse_deck(read_deck(slug, talk_id))
    notes = load_notes(slug, talk_id).get("notes", {})
    hist = load_history(slug, talk_id).get("versions", [])

    out_notes = []
    for note in sorted(notes.values(), key=lambda n: n.get("created_at", "")):
        src = slides[note["slide"]]["source"] if 0 <= note["slide"] < len(slides) else ""
        n = dict(note)
        n["orphan"] = bool(note.get("quote")) and resolve_anchor(src, note) < 0
        out_notes.append(n)

    for s in slides:
        s["history"] = [h for h in hist if h.get("slide") == s["index"]]
        # What the ✎ edit toggle opens: the slide's own markdown, with every open mark
        # materialised as the same `\comment{id}{...}` wrapper report pages use.
        s["edit_source"] = slide_edit_source(slug, talk_id, slides, notes, s["index"])
    return {"talk": rec, "slides": slides, "notes": out_notes,
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
                "on_version": note.get("version", 1),
                "current_version": s.get("version", 1),
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
            else:
                item["anchor"] = {"type": "region", **(note.get("rect") or {})}
            out.append(item)
    return out


def absorb_push(slug: str, talk_id: str, text: str) -> None:
    """Take a deck an agent pushed, and honour what its slide headers ask for.

    A pushed file is the agent's whole interface — it has one editor and no HTTP client, and a
    workflow that needs a second out-of-band call is a workflow that silently half-completes.
    So a revised slide may carry its own resolution in the header it already writes:

        <!-- slide: kind=derivation, date=2026-08-27, v=2, resolves=n1,n2, why=re-derived -->

    Slides whose body changed are versioned and snapshotted here rather than trusting the `v=`
    the agent typed, and any marks named in `resolves=` are deleted with their snapshots — the
    same ending the app's own revise path gives them.
    """
    old = {s["index"]: s for s in parse_deck(read_deck(slug, talk_id))}
    new = parse_deck(text)
    notes = load_notes(slug, talk_id)
    hist = load_history(slug, talk_id)
    dropped, changed = [], False

    for slide in new:
        was = old.get(slide["index"])
        asked = [n.strip() for n in str(slide.pop("resolves", "")).split(",") if n.strip()]
        why = str(slide.pop("why", "")).strip()
        edited = was is not None and (was["body"] != slide["body"] or was["title"] != slide["title"])
        if was is not None:
            slide["version"] = was["version"]
        if not edited and not asked:
            continue
        if not edited:
            # A mark can be answered by an *earlier* revision and only cleared now. Honour the
            # directive without inventing a version: there is no new text to record.
            for note_id in asked:
                if note_id in notes.get("notes", {}):
                    dropped.append(note_id)
            continue
        changed = True
        why = why or "revised by the agent"
        marks = []
        for note_id in asked:
            note = notes.get("notes", {}).get(note_id)
            if note is None:
                continue
            said = [m.get("body", "") for m in note.get("messages", []) if m.get("body")]
            marks.append({"mark": note["kind"], "means": KINDS[note["kind"]]["means"],
                          "quote": note.get("quote", ""), "comment": said[0] if said else "",
                          "conversation": said, "region": note.get("rect") or None,
                          "by": note.get("author", "")})
            dropped.append(note_id)
        hist.setdefault("versions", []).append({
            "slide": slide["index"], "version": was["version"], "date": was.get("date", ""),
            "title": was["title"], "sub": was["sub"], "body": was["body"],
            "why": why, "marks": marks, "superseded_at": _now_iso()})
        slide["version"] = was["version"] + 1
        slide["date"] = _today()

    if changed:
        _write_yaml(paths.bubble_talk_history_path(slug, talk_id), hist)
    for note_id in dropped:
        notes.get("notes", {}).pop(note_id, None)
        note_image_path(slug, talk_id, note_id).unlink(missing_ok=True)
    if dropped:
        save_notes(slug, talk_id, notes)
    _atomic_write(paths.bubble_talk_path(slug, talk_id), render_deck(new))


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
            where = (f"the text `{note['quote']}`" if note.get("quote")
                     else _region_phrase(note.get("rect") or {},
                                         has_picture=bool(note.get("image"))))
            lines = [f"### {k.get('glyph','')} {k.get('means','')} — slide {i + 1}: "
                     f"{sl.get('title','')}",
                     "",
                     f"- **id**: `{note['id']}` (name this in `resolves` when you revise)",
                     f"- **on**: {where}",
                     f"- **slide version when marked**: v{note.get('version', 1)} "
                     f"(now v{sl.get('version', 1)})"]
            if note.get("image"):
                lines.append(f"- **picture**: `feedback/shots/{note['id']}.png` — the slide as the "
                             f"reviewer saw it, with this mark drawn on. Open it: it shows layout "
                             f"and placement the text cannot.")
            for m in note.get("messages", []):
                if m.get("body"):
                    lines += ["", f"**{m.get('author','')}** — {m['body']}"]
            blocks.append("\n".join(lines) + "\n")
    return blocks


def open_note_images(slug: str) -> dict:
    """`feedback/shots/<note_id>.png` -> the stored snapshot, for open marks only."""
    out = {}
    for rec in load_index(slug).get("talks", []):
        tid = rec["id"]
        for note in load_notes(slug, tid).get("notes", {}).values():
            if not note.get("image"):
                continue
            path = note_image_path(slug, tid, note["id"])
            if path.is_file():
                out[f"feedback/shots/{note['id']}.png"] = path
    return out
