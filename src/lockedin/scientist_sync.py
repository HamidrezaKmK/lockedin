"""Bubble-scoped filesystem primitives for project-local Scientist clients.

The v2 protocol exports one approved bubble at a time. Paper assets, indexes, and
configuration are read-only; report pages, figures, and chalk-talk decks accept writes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from slugify import slugify

from . import assets, bubbles, feedback, paths, talks


def revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Assets above this are listed but never content-synced. A photo archive or dataset is not
# something an agent reads or edits, while hashing one on every manifest poll costs more than the
# rest of the bubble put together — a 3.9 GB zip is ~2.2 s of hashing per poll, and clients poll
# every few seconds. ``lockedin-scientist assets`` fetches them on request instead.
LARGE_ASSET_BYTES = int(os.environ.get("LOCKEDIN_SYNC_MAX_ASSET_BYTES") or 25 * 1024 * 1024)


def _is_large(source: Path | bytes) -> bool:
    return isinstance(source, Path) and source.stat().st_size > LARGE_ASSET_BYTES


def _stamp(path: Path) -> str:
    """A revision for a file we deliberately do not read: size and mtime, not content."""
    st = path.stat()
    return revision(f"{st.st_size}:{st.st_mtime_ns}".encode("utf-8"))


def _approved(slug: str) -> bool:
    return bool(bubbles.load_registry().get(slug, {}).get("approved"))


def _safe_rel(rel: str) -> bool:
    parts = Path(rel).parts
    return bool(parts) and not any(part in ("", ".", "..") for part in parts)


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _page_feedback_items(slug: str) -> list[dict]:
    """Full open page feedback for keyed retrieval and the recovery file."""
    items = []
    for page in bubbles.list_pages(slug):
        page_slug = page["page_slug"]
        for thread in bubbles.list_comments(slug, page_slug).get("threads", []):
            if thread.get("status") != "open":
                continue
            item = dict(thread)
            item.update({"surface": "page", "page": page_slug,
                         "page_title": page.get("title", page_slug),
                         "source_path": f"reports/pages/{page_slug}.md"})
            anchor = item.get("anchor") or {}
            quote = str(anchor.get("quote") or "")
            item["selected_text"] = quote
            if item.get("anchor_state") == "attached" and isinstance(anchor.get("start"), int):
                item["offsets"] = {"start": anchor["start"],
                                   "end": anchor["start"] + len(quote)}
            items.append(item)
    return items


def _indexed_context(slug: str) -> dict[str, bytes]:
    """Generated JSON routing layer for token-bounded Scientist discovery."""
    records = talks.ensure_sync_ids(slug)
    sync_by_talk = {str(rec["id"]): str(rec["sync_id"]) for rec in records}
    talk_details = {str(rec["id"]): talks.talk_detail(slug, str(rec["id"])) for rec in records}
    talk_feedback = []
    for item in talks.open_notes_for_agent(slug):
        item = dict(item)
        sync_id = sync_by_talk.get(str(item.get("talk") or ""), "")
        item.update({"surface": "chalk_talk", "talk_id": sync_id,
                     "source_path": f"reports/talks/{sync_id}/slides.md",
                     "marks_path": f"reports/talks/{sync_id}/marks.json"})
        if item.get("screenshot"):
            item["screenshot"] = {**item["screenshot"],
                                  "file": "feedback/shots/" + Path(item["screenshot"]["file"]).name}
        talk_feedback.append(item)
    page_feedback = _page_feedback_items(slug)
    all_feedback = page_feedback + talk_feedback

    marks_by_key: dict[str, dict] = {}
    by_local_id: dict[str, list[str]] = {}
    for item in all_feedback:
        local_id = str(item.get("note_id") or item.get("id") or "")
        if item["surface"] == "chalk_talk":
            key = f"{item['talk_id']}:{local_id}"
            pointer = {"surface": "chalk_talk", "id": local_id,
                       "talk_id": item["talk_id"], "talk_title": item.get("talk_title", ""),
                       "slide": item.get("slide", 0), "slide_title": item.get("slide_title", ""),
                       "mark": item.get("mark", ""), "source_path": item["source_path"],
                       "detail_path": item["marks_path"]}
        else:
            key = f"page:{item['page']}:{local_id}"
            pointer = {"surface": "page", "id": local_id, "page": item["page"],
                       "page_title": item.get("page_title", ""), "kind": item.get("kind", ""),
                       "source_path": item["source_path"],
                       "detail_path": f"feedback/pages/{item['page']}.json"}
        marks_by_key[key] = pointer
        by_local_id.setdefault(local_id, []).append(key)

    talk_index = {}
    out: dict[str, bytes] = {}
    for rec in records:
        server_id, sync_id = str(rec["id"]), str(rec["sync_id"])
        detail = talk_details[server_id]
        mark_items = [item for item in talk_feedback if item.get("talk_id") == sync_id]
        # The deck is already next door. Do not duplicate every slide into marks.json.
        compact_marks = [{k: v for k, v in item.items() if k != "slide_source"}
                         for item in mark_items]
        marks_path = f"reports/talks/{sync_id}/marks.json"
        slides_path = f"reports/talks/{sync_id}/slides.md"
        out[marks_path] = _json_bytes({"version": 1, "talk_id": sync_id,
                                       "by_id": {item["note_id"]: item for item in compact_marks}})
        talk_index[sync_id] = {
            "id": sync_id, "title": rec.get("title", ""), "summary": rec.get("intent", ""),
            "date": rec.get("date", ""), "slides": len(detail.get("slides", [])),
            "slide_titles": [slide.get("title", "") for slide in detail.get("slides", [])],
            "open_mark_ids": [item["note_id"] for item in mark_items],
            "slides_path": slides_path, "marks_path": marks_path,
        }

    page_index = {}
    for page in bubbles.list_pages(slug):
        slug_id = page["page_slug"]
        page_marks = [item for item in page_feedback if item.get("page") == slug_id]
        out[f"feedback/pages/{slug_id}.json"] = _json_bytes({
            "version": 1, "page": slug_id,
            "by_id": {str(item.get("id") or ""): item for item in page_marks},
        })
        page_index[slug_id] = {"id": slug_id, "title": page.get("title", slug_id),
                               "path": f"reports/pages/{slug_id}.md",
                               "marks_path": f"feedback/pages/{slug_id}.json",
                               "open_mark_ids": [str(item.get("id") or "") for item in page_marks]}
    out["indexes/chalk-talks.json"] = _json_bytes({"version": 1, "by_id": talk_index,
                                                     "order": list(talk_index)})
    out["indexes/marks.json"] = _json_bytes({"version": 1, "by_key": marks_by_key,
                                              "by_local_id": by_local_id})
    out["indexes/pages.json"] = _json_bytes({"version": 1, "by_id": page_index})
    paper_index = {}
    for meta in bubbles.pdfs_for_bubble(slug):
        pdf_id = str(meta.get("pdf_id") or "")
        if not pdf_id:
            continue
        paper_index[pdf_id] = {
            "id": pdf_id, "title": meta.get("title") or meta.get("name") or pdf_id,
            "authors": meta.get("authors") or "", "tags": meta.get("tags") or [],
            "relevance": meta.get("relevance") or meta.get("why") or "",
            "summary_path": f"assets/{pdf_id}/summary.md",
            "text_path": f"assets/{pdf_id}/text.txt",
            "metadata_path": f"assets/{pdf_id}/meta.yaml",
            "pdf_path": f"assets/{pdf_id}/paper.pdf",
        }
    report_assets = {}
    asset_root = paths.bubble_assets_dir(slug)
    if asset_root.exists():
        for path in sorted(asset_root.iterdir()):
            if path.is_file() and not path.name.endswith(".tmp"):
                report_assets[path.name] = {"name": path.name,
                                            "path": f"reports/assets/{path.name}"}
    out["indexes/papers.json"] = _json_bytes({"version": 1, "by_id": paper_index})
    out["indexes/report-assets.json"] = _json_bytes({"version": 1, "by_name": report_assets})
    out["feedback/all.json"] = _json_bytes({"version": 1, "marks": all_feedback})
    entry = bubbles.load_registry().get(slug, {})
    out["index.json"] = _json_bytes({
        "version": 1, "bubble": {"slug": slug, "name": entry.get("name", slug)},
        "counts": {"chalk_talks": len(talk_index), "pages": len(page_index),
                   "papers": len(paper_index), "report_assets": len(report_assets),
                   "open_marks": len(all_feedback)},
        "indexes": {"chalk_talks": "indexes/chalk-talks.json",
                    "marks": "indexes/marks.json", "pages": "indexes/pages.json",
                    "papers": "indexes/papers.json", "report_assets": "indexes/report-assets.json"},
        "feedback_fallback": "feedback/all.json",
    })
    return out


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
        out: dict[str, Path | bytes] = {}
        report = paths.bubble_dir(slug)
        if report.exists():
            for path in report.rglob("*"):
                if not path.is_file() or path.name.endswith(".tmp"):
                    continue
                rel = path.relative_to(report)
                # Private review comments (and legacy chats/ dirs) do not belong in an agent project.
                if rel.parts and rel.parts[0] in {"chats", "comments", ".uploads"}:
                    continue
                # Chalk talks are exported below into stable, title-independent folders. The
                # server's flat storage names are implementation details, not agent addresses.
                if rel.parts[:1] == ("talks",):
                    continue
                if rel.as_posix() in {"pages.yaml", "_lockedin_papers.md"}:
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
        out.update(feedback.images(slug))
        # JSON indexes are always present, even in a clean bubble. Their tiny counts tell an
        # agent whether deeper retrieval is needed without loading any deck or feedback body.
        out.update(_indexed_context(slug))
        for rec in talks.ensure_sync_ids(slug):
            out[f"reports/talks/{rec['sync_id']}/slides.md"] = paths.bubble_talk_path(slug, rec["id"])
        return out


def _content(source: Path | bytes) -> bytes:
    return source if isinstance(source, bytes) else source.read_bytes()


def manifest(home: Path, slug: str) -> dict:
    """Every exported path, with a revision.

    Oversized assets stay listed — dropping them would read as "deleted on the server" and make a
    client bin its local copy — but carry a size/mtime revision and an ``oversize`` flag, so no
    poll ever reads their contents and no client downloads them by accident.
    """
    files = []
    for rel, source in sorted(_files(home, slug).items()):
        if _is_large(source):
            files.append({"path": rel, "revision": _stamp(source),
                          "oversize": True, "size": source.stat().st_size})
        else:
            files.append({"path": rel, "revision": revision(_content(source))})
    return {"files": files, "large_asset_bytes": LARGE_ASSET_BYTES}


def bubble_is_open(home: Path, slug: str) -> bool:
    """Whether this bubble is exported to Scientist clients at all."""
    with paths.use_root(home):
        return _approved(slug)


def large_asset_path(home: Path, slug: str, rel: str) -> Path:
    """Resolve one oversized asset for streaming; raises rather than guessing."""
    available = _files(home, slug)
    source = available.get(rel) if _safe_rel(rel) else None
    if not isinstance(source, Path) or not _is_large(source):
        raise FileNotFoundError(rel)
    return source


def large_assets(home: Path, slug: str) -> list[dict]:
    """The assets a sync deliberately skips, for the on-demand fetch command."""
    return sorted(({"path": rel, "size": source.stat().st_size,
                    "revision": _stamp(source)}
                   for rel, source in _files(home, slug).items() if _is_large(source)),
                  key=lambda item: item["path"])


def read_files(home: Path, slug: str, wanted: list[str]) -> dict:
    """Read files by path.

    Oversized assets are never returned here: this encodes whole files as base64 in one JSON
    body, so a multi-gigabyte archive would be held in memory twice over. They are fetched
    instead by ``large_asset_path``, which streams straight off disk.
    """
    available = _files(home, slug)
    files, skipped = [], []
    for rel in dict.fromkeys(wanted):
        if not _safe_rel(rel) or rel not in available:
            continue
        source = available[rel]
        if _is_large(source):
            skipped.append({"path": rel, "size": source.stat().st_size, "oversize": True})
            continue
        raw = _content(source)
        files.append({"path": rel, "revision": revision(raw),
                      "content_b64": base64.b64encode(raw).decode("ascii")})
    return {"files": files, "skipped": skipped}


def writable_path(slug: str, rel: str) -> bool:
    """Whether a local v2 report file can be pushed to this exact bubble."""
    if not _safe_rel(rel):
        return False
    parts = Path(rel).parts
    if len(parts) not in (3, 4) or parts[0] != "reports":
        return False
    if parts[1] == "assets":
        # ``.tmp`` is reserved. ``apply_writes`` stages through a temp file and ``_files`` hides
        # that suffix from the manifest, so an asset actually named ``*.tmp`` would push
        # successfully, stay invisible to every surface, and then be deleted locally on the next
        # sync as "no longer on the server". Refuse it here so the client is told instead.
        return (len(parts) == 3 and Path(parts[2]).name == parts[2]
                and not parts[2].endswith(".tmp"))
    if parts[1] == "talks":
        # The sidecars are generated (marks, snapshots, version history) and must not be pushed;
        # only the deck itself is the agent's to write.
        return bool(len(parts) == 4 and parts[3] == "slides.md"
                    and talks.valid_sync_id(parts[2]))
    if len(parts) != 3 or parts[1] != "pages" or not rel.endswith(".md"):
        return False
    page_slug = Path(parts[2]).stem
    return bool(page_slug and slugify(page_slug) == page_slug)


def _server_path(slug: str, rel: str) -> Path:
    parts = Path(rel).parts
    if parts[1] == "assets":
        return paths.bubble_assets_dir(slug) / parts[2]
    if parts[1] == "talks":
        talk_id = talks.talk_id_from_sync_id(slug, parts[2]) or parts[2]
        return paths.bubble_talk_path(slug, talk_id)
    return paths.bubble_page_path(slug, Path(parts[2]).stem)


def apply_writes(home: Path, slug: str, writes: list[dict], *, actor: str = "") -> dict:
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
                parts = Path(rel).parts
                sync_id = parts[2]
                talk_id = talks.talk_id_from_sync_id(slug, sync_id)
                talk_id = talk_id or sync_id
                try:
                    talks.absorb_push(slug, talk_id, raw.decode("utf-8"),
                                       actor=actor or "the connected user")
                except UnicodeDecodeError:
                    conflicts.append({"path": rel, "reason": "a deck must be UTF-8 text"})
                    continue
                except (KeyError, ValueError) as exc:
                    conflicts.append({"path": rel, "reason": str(exc),
                                      "revision": revision(current),
                                      "content_b64": base64.b64encode(current).decode("ascii")})
                    continue
                # The deck file is the source of truth; the registry entry is derived from it, so
                # writing a new file *is* how an agent creates a talk. Nothing else to ask for.
                talks.register_deck(slug, talk_id, sync_id=sync_id)
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
            elif Path(rel).parts[1] == "talks":
                parts = Path(rel).parts
                sync_id = parts[2]
                talk_id = talks.talk_id_from_sync_id(slug, sync_id)
                removed = talks.delete_talk(slug, talk_id or sync_id)
            else:
                removed = bubbles.delete_bubble_asset(slug, Path(rel).name)
                if removed: bubbles.touch_bubble(slug)
            if removed: applied.append({"path": rel})
            else: conflicts.append({"path": rel, "reason": "no such Scientist file"})
    return {"applied": applied, "conflicts": conflicts}
