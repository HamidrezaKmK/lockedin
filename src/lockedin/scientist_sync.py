"""Bubble-scoped filesystem primitives for project-local Scientist clients.

The v2 protocol exports one approved bubble at a time.  Paper assets and editing
configuration are read-only; only report pages and figures accept client writes.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import yaml
from slugify import slugify

from . import assets, bubbles, feedback, paths, talks


def revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _approved(slug: str) -> bool:
    return bool(bubbles.load_registry().get(slug, {}).get("approved"))


def _safe_rel(rel: str) -> bool:
    parts = Path(rel).parts
    return bool(parts) and not any(part in ("", ".", "..") for part in parts)


def _review_feedback(slug: str) -> bytes | None:
    """Serialize open private review threads as Scientist's read-only feedback context."""
    threads = []
    for page in bubbles.list_pages(slug):
        page_slug = page["page_slug"]
        for thread in bubbles.list_comments(slug, page_slug).get("threads", []):
            if thread.get("status") != "open":
                continue
            anchor = dict(thread.get("anchor") or {})
            state = "attached" if thread.get("anchor_state") == "attached" else "unanchored"
            selected = str(anchor.get("quote") or "")
            exported = {
                "id": str(thread.get("id") or ""),
                "page_slug": page_slug,
                "status": "open",
                "anchor_state": state,
                "selected_text": selected,
                "created_at": thread.get("created_at", ""),
                "updated_at": thread.get("updated_at", ""),
                "context": {
                    "prefix": str(anchor.get("prefix") or ""),
                    "suffix": str(anchor.get("suffix") or ""),
                },
                "messages": [dict(message) for message in thread.get("messages", [])],
            }
            if state == "attached":
                start = int(anchor.get("start") or 0)
                exported["offsets"] = {"start": start, "end": start + len(selected)}
            threads.append(exported)
    if not threads:
        return None
    payload = {"version": 2, "threads": threads}
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).encode("utf-8")


def _idea_markdown(slug: str) -> bytes | None:
    """The bubble's premise, as a file the project actually contains.

    `abstract` and `goal` live in the workspace registry, which is not under ``REPORTS/`` and so
    was published nowhere — an agent could work a whole session without ever learning what the
    bubble was *for*. It is generated (never pushed back), so the user's edit in the app is the
    single source and reaches every worker on its next poll like any other changed file.
    """
    entry = bubbles.load_registry().get(slug, {})
    abstract = str(entry.get("abstract") or "").strip()
    goal = str(entry.get("goal") or "").strip()
    if not abstract and not goal:
        return None
    out = [f"# {entry.get('name') or slug}", ""]
    if abstract:
        out += [abstract, ""]
    if goal:
        out += ["## Goal", "", goal, ""]
    out += ["---", "",
            "*Generated from the bubble's premise — the shared statement of what this work is*",
            "*for. Read it before anything else. It is Markdown with LaTeX, and it is read-only*",
            "*here: if it is wrong, stale, or narrower than what you are actually doing, say so*",
            "*and propose better wording; the user applies it in the app.*"]
    if entry.get("premise_revised_at"):
        out += ["", f"*Last revised {str(entry['premise_revised_at'])[:10]}.*"]
    return ("\n".join(out) + "\n").encode()


def _files(home: Path, slug: str) -> dict[str, Path | bytes]:
    """Map v2 project-local paths to source files for one bubble."""
    with paths.use_root(home):
        if not _approved(slug):
            raise KeyError(slug)
        out: dict[str, Path] = {}
        report = paths.bubble_dir(slug)
        if report.exists():
            for path in report.rglob("*"):
                if not path.is_file() or path.name.endswith(".tmp"):
                    continue
                rel = path.relative_to(report)
                # Private review comments (and legacy chats/ dirs) do not belong in an agent project.
                if rel.parts and rel.parts[0] in {"chats", "comments"}:
                    continue
                # Chalk talks: the decks themselves are published (the agent revises them), but
                # the marks' sidecar, the snapshots, and the version history are not. Raw YAML
                # of every note ever left would sit in the agent's context forever; the
                # generated feedback/OPEN.md below carries only what is still open, and
                # disappears entirely once nothing is.
                if rel.parts[:1] == ("talks",) and (
                        rel.name.endswith((".notes.yaml", ".history.yaml"))
                        or rel.name == "talks.yaml"
                        or rel.parts[1:2] == ("shots",)):
                    continue
                # Report figures are flat by contract: they are stored flat, listed flat, and
                # served through /api/bubbles/<slug>/assets/{filename}, a single path segment that
                # cannot match a nested path. Publishing a nested figure would hand the client a
                # file it can never render, push back, or delete — so never export one.
                if rel.parts[:1] == ("assets",) and len(rel.parts) != 2:
                    continue
                if rel.as_posix() == "_lockedin_overleaf.yaml":
                    out["config/overleaf.yaml"] = path
                    continue
                out[(Path("reports") / rel).as_posix()] = path
        for meta in bubbles.pdfs_for_bubble(slug):
            pdf_id = str(meta.get("pdf_id") or "")
            if not pdf_id:
                continue
            root = paths.asset_dir(pdf_id)
            for name in ("paper.pdf", "meta.yaml", "text.txt", "summary.md"):
                path = root / name
                if path.is_file():
                    out[(Path("assets") / pdf_id / name).as_posix()] = path
        for name, path in (("math.yaml", paths.MATH_CONFIG_YAML),
                           ("aesthetics.yaml", paths.AESTHETICS_CONFIG_YAML)):
            if path.is_file():
                out[(Path("config") / name).as_posix()] = path
        idea = _idea_markdown(slug)
        if idea is not None:
            out["IDEA.md"] = idea
        reviews = _review_feedback(slug)
        if reviews is not None:
            out["config/reviews.yaml"] = reviews
        marks = feedback.open_markdown(slug)
        if marks is not None:
            out["feedback/OPEN.md"] = marks
            out.update(feedback.images(slug))
        return out


def _content(source: Path | bytes) -> bytes:
    return source if isinstance(source, bytes) else source.read_bytes()


def manifest(home: Path, slug: str) -> dict:
    return {"files": [{"path": rel, "revision": revision(_content(source))}
                      for rel, source in sorted(_files(home, slug).items())]}


def read_files(home: Path, slug: str, wanted: list[str]) -> dict:
    available = _files(home, slug)
    files = []
    for rel in dict.fromkeys(wanted):
        if not _safe_rel(rel) or rel not in available:
            continue
        raw = _content(available[rel])
        files.append({"path": rel, "revision": revision(raw),
                      "content_b64": base64.b64encode(raw).decode("ascii")})
    return {"files": files}


def writable_path(slug: str, rel: str, *, existing_pages: bool = True) -> bool:
    """Whether a local v2 report file can be pushed to this exact bubble."""
    if not _safe_rel(rel):
        return False
    parts = Path(rel).parts
    if len(parts) != 3 or parts[0] != "reports":
        return False
    if parts[1] == "assets":
        # ``.tmp`` is reserved. ``apply_writes`` stages through a temp file and ``_files`` hides
        # that suffix from the manifest, so an asset actually named ``*.tmp`` would push
        # successfully, stay invisible to every surface, and then be deleted locally on the next
        # sync as "no longer on the server". Refuse it here so the client is told instead.
        return Path(parts[2]).name == parts[2] and not parts[2].endswith(".tmp")
    if parts[1] == "talks":
        # The sidecars are generated (marks, snapshots, version history) and must not be pushed;
        # only the deck itself is the agent's to write.
        name = Path(parts[2]).name
        return (rel.endswith(".md") and name == parts[2]
                and not name.endswith((".notes.yaml", ".history.yaml"))
                and name != "talks.yaml")
    if parts[1] != "pages" or not rel.endswith(".md"):
        return False
    page_slug = Path(parts[2]).stem
    return bool(page_slug and slugify(page_slug) == page_slug)


def _server_path(slug: str, rel: str) -> Path:
    parts = Path(rel).parts
    if parts[1] == "assets":
        return paths.bubble_assets_dir(slug) / parts[2]
    if parts[1] == "talks":
        return paths.bubble_talk_path(slug, Path(parts[2]).stem)
    return paths.bubble_page_path(slug, Path(parts[2]).stem)


def apply_writes(home: Path, slug: str, writes: list[dict]) -> dict:
    conflicts, applied = [], []
    with paths.use_root(home):
        if not _approved(slug):
            return {"applied": [], "conflicts": [{"path": "", "reason": "unknown or unapproved bubble"}]}
        pages = {p["page_slug"] for p in bubbles.list_pages(slug)}
        for item in writes:
            rel = str(item.get("path", ""))
            if not writable_path(slug, rel) or (Path(rel).parts[1] == "pages" and Path(rel).stem not in pages):  # noqa: E501
                conflicts.append({"path": rel, "reason": "read-only or invalid Scientist path"})
                continue
            try:
                raw = base64.b64decode(str(item.get("content_b64", "")), validate=True)
            except Exception:
                conflicts.append({"path": rel, "reason": "invalid content"}); continue
            target = _server_path(slug, rel)
            current = target.read_bytes() if target.exists() else b""
            if str(item.get("base_revision", "")) != revision(current):
                conflicts.append({"path": rel, "reason": "stale revision", "revision": revision(current),
                                  "content_b64": base64.b64encode(current).decode("ascii")}); continue
            if not raw and current:
                conflicts.append({"path": rel, "reason": "refusing to replace non-empty content with an empty sync write",
                                  "revision": revision(current), "content_b64": base64.b64encode(current).decode("ascii")}); continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if Path(rel).parts[1] == "talks":
                try:
                    talks.absorb_push(slug, Path(rel).stem, raw.decode("utf-8"))
                except UnicodeDecodeError:
                    conflicts.append({"path": rel, "reason": "a deck must be UTF-8 text"})
                    continue
                # The deck file is the source of truth; the registry entry is derived from it, so
                # writing a new file *is* how an agent creates a talk. Nothing else to ask for.
                talks.register_deck(slug, Path(rel).stem)
                # absorb_push stores a canonical render — headers rewritten, resolves= consumed —
                # so hand the stored bytes and THEIR revision back, exactly as save_page does.
                # Reporting revision(raw) left every client tracking a revision the manifest
                # would never show, which read as "remote changed" on the next cycle and turned
                # an agent's follow-up edit into a conflict that restored the server copy over it.
                stored = target.read_bytes()
                entry = {"path": rel, "revision": revision(stored)}
                if stored != raw:
                    entry["content_b64"] = base64.b64encode(stored).decode("ascii")
                applied.append(entry)
                continue
            if Path(rel).parts[1] == "pages":
                try:
                    bubbles.save_page(slug, Path(rel).stem, raw.decode("utf-8"),
                                      target.stat().st_mtime if target.exists() else None)
                except (UnicodeDecodeError, bubbles.PageConflict, bubbles.ReviewMarkupError) as exc:
                    conflicts.append({"path": rel, "reason": str(exc),
                                      "revision": revision(current),
                                      "content_b64": base64.b64encode(current).decode("ascii")})
                    continue
            else:
                # Dot-prefixed so the staging file can never collide with a real asset name.
                tmp = target.with_name("." + target.name + ".tmp"); tmp.write_bytes(raw); tmp.replace(target)
                bubbles.touch_bubble(slug)
            stored = target.read_bytes()
            entry = {"path": rel, "revision": revision(stored)}
            if stored != raw:
                # save_page normalized the content (wikilinks, display math). Hand the stored
                # bytes back so the client can adopt them: a client left holding its pre-normalized
                # copy sees "local changed" forever and re-pushes on every cycle.
                entry["content_b64"] = base64.b64encode(stored).decode("ascii")
            applied.append(entry)
    return {"applied": applied, "conflicts": conflicts}


def register_page(home: Path, slug: str, page_slug: str, content_b64: str, base_revision: str) -> dict:
    rel = f"reports/pages/{page_slug}.md"
    if not writable_path(slug, rel):
        return {"applied": [], "conflicts": [{"path": rel, "reason": "read-only or invalid Scientist page"}]}
    try:
        raw = base64.b64decode(content_b64, validate=True); content = raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return {"applied": [], "conflicts": [{"path": rel, "reason": "invalid content"}]}
    with paths.use_root(home):
        if not _approved(slug):
            return {"applied": [], "conflicts": [{"path": rel, "reason": "unknown or unapproved bubble"}]}
        target = paths.bubble_page_path(slug, page_slug)
        current = target.read_bytes() if target.exists() else b""
        if base_revision != revision(current):
            conflict = {"path": rel, "reason": "stale revision", "revision": revision(current),
                        "content_b64": base64.b64encode(current).decode("ascii")}
            return {"applied": [], "conflicts": [conflict]}
        try: bubbles.register_page(slug, page_slug, content)
        except ValueError as exc:
            conflict = {"path": rel, "reason": str(exc), "revision": revision(current),
                        "content_b64": base64.b64encode(current).decode("ascii")}
            return {"applied": [], "conflicts": [conflict]}
        stored = target.read_bytes()
    return {"applied": [{"path": rel, "revision": revision(stored), "content_b64": base64.b64encode(stored).decode("ascii")}], "conflicts": []}


def apply_deletes(home: Path, slug: str, deletes: list[dict]) -> dict:
    conflicts, applied = [], []
    with paths.use_root(home):
        if not _approved(slug):
            return {"applied": [], "conflicts": [{"path": "", "reason": "unknown or unapproved bubble"}]}
        for item in deletes:
            rel = str(item.get("path", ""))
            if not writable_path(slug, rel):
                conflicts.append({"path": rel, "reason": "read-only or invalid Scientist path"}); continue
            target = _server_path(slug, rel); current = target.read_bytes() if target.exists() else b""
            if str(item.get("base_revision", "")) != revision(current):
                conflicts.append({"path": rel, "reason": "stale revision", "revision": revision(current),
                                  "content_b64": base64.b64encode(current).decode("ascii")}); continue
            if Path(rel).parts[1] == "pages":
                try: removed = bubbles.delete_page(slug, Path(rel).stem)
                except ValueError as exc:
                    conflicts.append({"path": rel, "reason": str(exc), "revision": revision(current),
                                      "content_b64": base64.b64encode(current).decode("ascii")}); continue
            else:
                removed = bubbles.delete_bubble_asset(slug, Path(rel).name)
                if removed: bubbles.touch_bubble(slug)
            if removed: applied.append({"path": rel})
            else: conflicts.append({"path": rel, "reason": "no such Scientist file"})
    return {"applied": applied, "conflicts": conflicts}
