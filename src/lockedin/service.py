"""Shared service layer — the single place the server calls into.

Non-streaming operations are wrapped in ``paths.use_root(home)`` here so the server stays
pure HTTP glue. Streaming generators (chat / generate / edit) manage their own root context
internally (they must keep it open across yields), so those are thin pass-throughs.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import yaml

from . import assets, auth, bubbles, models, paths, reports, sharing, tagger, todos, workspaces


def ensure_workspace(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    for sub in ("ASSETS", "REPORTS", "config"):
        (home / sub).mkdir(parents=True, exist_ok=True)


def migrate_overleaf_fields() -> int:
    """Backfill the optional Overleaf field in all workspace and remaining legacy registries."""
    roots = list(workspaces.all_homes())
    if paths.USERS_DIR.exists():
        roots.extend(path for path in paths.USERS_DIR.iterdir() if path.is_dir())
    seen, changed = set(), 0
    for root in roots:
        root = root.resolve()
        if root in seen or not (root / "bubbles.yaml").exists():
            continue
        seen.add(root)
        with paths.use_root(root):
            changed += bubbles.migrate_overleaf_fields()
    return changed


# ---- assets ----
def save_asset(home: Path, pdf_bytes: bytes, filename: str, title: str = "",
               tags: list[str] | None = None, url_source: str = "",
               bibliography: str = "") -> str:
    with paths.use_root(home):
        bibliography = bibliography.strip()
        # Validate before creating files, so an invalid or duplicate key does not leave behind
        # a partially added asset.
        if bibliography:
            assets.validate_bibtex_unique("", bibliography)
        pdf_id = assets.save_asset(pdf_bytes, filename, title=title, tags=tags,
                                   url_source=url_source, bibliography=bibliography)
        meta = assets.load_meta(pdf_id)
        bubbles.refresh_citation_files(meta.get("idea_bubbles", []))
        return pdf_id


def fetch_and_save_asset(home: Path, url: str, title: str = "",
                         tags: list[str] | None = None, bibliography: str = "") -> str:
    """Download a PDF from ``url`` and store it as a new asset. Returns the new pdf_id.

    Shares ``assets.fetch_pdf_from_url`` with the Slack bot. Raises ``ValueError`` if the link
    is reachable but isn't a PDF (the download itself may raise on network errors / oversize).
    """
    fetched = assets.fetch_pdf_from_url(url)
    if fetched is None:
        raise ValueError("That link doesn't point to a PDF.")
    pdf_bytes, filename = fetched
    return save_asset(home, pdf_bytes, filename, title=title, tags=tags,
                      url_source=url, bibliography=bibliography)


def list_assets(home: Path) -> list[dict]:
    with paths.use_root(home):
        out = []
        for meta in assets.list_assets():
            meta = dict(meta)
            meta.pop("suggested_tags", None)  # legacy auto-suggestions are no longer surfaced
            meta["bubble_scores"] = assets.bubble_scores(meta)
            out.append(meta)
        return out


def get_asset(home: Path, pdf_id: str) -> dict:
    with paths.use_root(home):
        meta = assets.load_meta(pdf_id)
        meta.pop("suggested_tags", None)  # legacy auto-suggestions are no longer surfaced
        meta["bubble_scores"] = assets.bubble_scores(meta)
        meta["bubble_memberships"] = bubbles.memberships_for_asset(pdf_id)
        return meta


def update_asset(home: Path, pdf_id: str, **fields) -> dict:
    with paths.use_root(home):
        before = set(assets.load_meta(pdf_id).get("idea_bubbles", []))
        meta = assets.update_asset(pdf_id, **fields)
        after = set(meta.get("idea_bubbles", []))
        bubbles.refresh_citation_files(before | after)
        return meta


def update_asset_bibliography(home: Path, pdf_id: str, bibliography: str) -> dict:
    with paths.use_root(home):
        assets.validate_bibtex_unique(pdf_id, bibliography)
        meta = assets.update_asset(pdf_id, bibliography=bibliography or "")
        bubbles.refresh_citation_files(meta.get("idea_bubbles", []))
        return meta


def preview_bibtex(home: Path, bibliography: str) -> dict:
    with paths.use_root(home):
        return assets.preview_bibtex(bibliography)


def delete_asset(home: Path, pdf_id: str) -> bool:
    with paths.use_root(home):
        try:
            affected = set(assets.load_meta(pdf_id).get("idea_bubbles", []))
        except FileNotFoundError:
            affected = set()
        ok = assets.delete_asset(pdf_id)
        if ok:
            bubbles.refresh_citation_files(affected)
        return ok


def asset_pdf_path(home: Path, pdf_id: str) -> Path:
    with paths.use_root(home):
        return assets.pdf_path(pdf_id)


def asset_summary(home: Path, pdf_id: str) -> str:
    with paths.use_root(home):
        return assets.get_summary(pdf_id)


class ModelUnavailableError(RuntimeError):
    """Raised when a requested LLM action has no usable active provider."""


def resummarize_asset(home: Path, pdf_id: str) -> str:
    """Refresh an asset's cached summary with the workspace owner's active model."""
    with paths.use_root(home):
        if not assets.exists(pdf_id):
            raise FileNotFoundError(pdf_id)
        health = models.health_check(home, live=True)
        if not health.get("ok"):
            raise ModelUnavailableError("No working active LLM is available. " +
                                        health.get("message", "Configure a model in Settings."))
        summary = tagger.summarize_pdf(home, pdf_id)
        if not summary.strip():
            raise ValueError("This PDF has no extractable text to summarize.")
        assets.update_asset(pdf_id, summarized=True)
        return summary


# ---- bubbles ----
def list_bubbles(home: Path, *, archived: bool = False) -> list[dict]:
    with paths.use_root(home):
        return [b for b in bubbles.all_bubbles()
                if b.get("approved") and bool(b.get("archived")) == archived]


def bubble_detail(home: Path, slug: str) -> dict:
    with paths.use_root(home):
        return bubbles.bubble_detail(slug)


def create_bubble(home: Path, name: str) -> str:
    with paths.use_root(home):
        return bubbles.create_bubble(name)


def register_user_tags(home: Path, tags: list[str]) -> None:
    """Tags the user assigned explicitly become approved bubbles immediately."""
    with paths.use_root(home):
        for t in tags:
            if t.strip():
                bubbles.create_bubble(t)


def rename_bubble(home: Path, slug: str, new_name: str) -> dict:
    with paths.use_root(home):
        return bubbles.rename_bubble(slug, new_name)


def set_bubble_overleaf(home: Path, slug: str, value: str | None) -> dict:
    with paths.use_root(home):
        bubbles.set_overleaf_project(slug, value)
        return bubbles.bubble_detail(slug)


def set_bubble_archived(home: Path, slug: str, archived: bool) -> dict:
    with paths.use_root(home):
        return bubbles.set_bubble_archived(slug, archived)


def approve_bubble(home: Path, slug: str, instructions: str = "") -> dict:
    with paths.use_root(home):
        return bubbles.approve_bubble(slug, instructions)


def delete_bubble(home: Path, slug: str) -> None:
    with paths.use_root(home):
        bubbles.delete_bubble(slug)
    sharing.drop_bubble(home.name, slug)  # revoke any public share link for the gone bubble


def add_pdf_to_bubble(home: Path, slug: str, pdf_id: str) -> dict:
    """Tag an existing PDF so it joins this bubble; returns the refreshed bubble detail."""
    with paths.use_root(home):
        bubbles.add_pdf_to_bubble(slug, pdf_id)
        return bubbles.bubble_detail(slug)


def remove_pdf_from_bubble(home: Path, slug: str, pdf_id: str) -> dict:
    """Untag a PDF so it leaves this bubble; returns the refreshed bubble detail."""
    with paths.use_root(home):
        bubbles.remove_pdf_from_bubble(slug, pdf_id)
        return bubbles.bubble_detail(slug)


def set_pdf_bubble_score(home: Path, slug: str, pdf_id: str, score: int) -> dict:
    with paths.use_root(home):
        bubbles.set_pdf_bubble_score(slug, pdf_id, score)
        return bubbles.bubble_detail(slug)


def migrate_papers(home: Path, source: str, dest: str, items: list[dict]) -> dict:
    """Copy chosen papers from one bubble into another at explicit relevance scores.

    Papers are shared assets and membership is a tag, so this copies without duplicating any file
    and leaves the source bubble entirely untouched. Each item is ``{"pdf_id": str, "score": 1-5}``.

    Only current members of ``source`` are eligible, and a paper already in ``dest`` is skipped
    rather than re-scored: the picker hides those, so accepting them here would silently overwrite
    a relevance the user never saw. Both rules are enforced server-side so a stale browser tab
    cannot turn this into "tag any asset into any bubble".
    """
    if source == dest:
        raise ValueError("Pick two different bubbles.")
    # Validate every score before touching anything. Tagging happens before scoring, so a bad
    # score found halfway through would otherwise leave that paper in the destination unscored.
    planned = []
    for item in items:
        try:
            score = int(item.get("score", 5))
        except (TypeError, ValueError):
            raise ValueError("Relevance score must be an integer from 1 to 5.")
        if not 1 <= score <= 5:
            raise ValueError("Relevance score must be an integer from 1 to 5.")
        planned.append((str(item.get("pdf_id") or ""), score))
    with paths.use_root(home):
        registry = bubbles.load_registry()
        for slug in (source, dest):
            if slug not in registry:
                raise KeyError(f"No such bubble: {slug!r}")
        eligible = {m["pdf_id"] for m in bubbles.pdfs_for_bubble(source)}
        already = {m["pdf_id"] for m in bubbles.pdfs_for_bubble(dest)}
        migrated, skipped = [], []
        for pdf_id, score in planned:
            if pdf_id not in eligible or pdf_id in already:
                skipped.append(pdf_id)
                continue
            # add_pdf_to_bubble tags via bubbles.tag_for_slug, which is what keeps a renamed
            # destination from splitting into a phantom slug. Never hand-roll that tag.
            bubbles.add_pdf_to_bubble(dest, pdf_id)
            bubbles.set_pdf_bubble_score(dest, pdf_id, score)
            migrated.append(pdf_id)
        # Setting a score does not refresh the generated inventory, and its "[Relevance N]"
        # headings are what a Scientist session reads as the authoritative paper list. Rewrite it
        # once, after the batch has settled.
        bubbles.write_citation_file(dest)
        return {"migrated": migrated, "skipped": skipped, "bubble": bubbles.bubble_detail(dest)}


# ---- public sharing ----
def set_bubble_share(home: Path, slug: str, active: bool) -> dict:
    """Toggle a bubble's unlisted public share and register its token in the global index."""
    with paths.use_root(home):
        res = bubbles.set_share_active(slug, active)
    if res.get("share_token"):
        workspace = workspaces.get(home.name)
        if workspace and workspaces.workspace_home(home.name).resolve() == home.resolve():
            sharing.register(res["share_token"], "", slug, workspace_id=home.name)
        else:  # legacy user-home shares remain supported for existing installations and tests
            sharing.register(res["share_token"], home.name, slug)
    return res


def migrate_share_index_to_workspaces() -> int:
    """Move all legacy share-index entries onto the users' Personal workspace ids."""
    accounts = auth.load_accounts()
    personal = {username: rec.get("personal_workspace_id", "")
                for username, rec in accounts.items()
                if rec.get("personal_workspace_id") and workspaces.get(rec["personal_workspace_id"])}
    # An early workspace implementation accidentally put the workspace id in the legacy
    # ``user`` field. Normalize those records too.
    personal.update({workspace_id: workspace_id for workspace_id in personal.values()})
    return sharing.migrate_user_entries(personal)


def share_target(token: str) -> tuple[Path, str] | None:
    """Resolve a share token to ``(home, slug)`` IF the bubble is currently shared, else None."""
    ent = sharing.resolve(token)
    if not ent:
        return None
    workspace_id = ent.get("workspace_id") or ent.get("user", "")
    # Entries written before the workspace migration stored the workspace id in ``user``.
    # Recognize those links so turning sharing back on is not required to repair them.
    home = (workspaces.workspace_home(workspace_id)
            if workspaces.get(workspace_id) else paths.user_home(workspace_id))
    with paths.use_root(home):
        entry = bubbles.load_registry().get(ent["slug"])
        if not entry or not entry.get("share_active"):
            return None
    return home, ent["slug"]


# ---- account ----
def update_account(username: str, *, current_password: str,
                   new_username: str = "", new_password: str = "") -> str:
    """Change a user's password and/or username. Returns the final (normalized) username.

    Verifies the current password first. A username change moves the whole workspace directory
    and repoints the account record, live sessions, and any public-share index entries.
    """
    if not auth.verify_password(username, current_password):
        raise ValueError("Current password is incorrect.")
    new_username = (new_username or "").strip().lower()

    if new_password:
        auth.set_password(username, new_password)

    if not new_username or new_username == username:
        return username

    if not auth.valid_username(new_username):
        raise ValueError("Username must be 1-32 chars: a-z, 0-9, '_' or '-'.")
    if auth.user_exists(new_username):
        raise ValueError("That username is already taken.")
    src, dst = paths.user_home(username), paths.user_home(new_username)
    if dst.exists():
        raise ValueError("That username is already taken.")
    shutil.move(str(src), str(dst))           # carry the whole workspace over
    try:
        final = auth.rename_user(username, new_username)
    except Exception:
        shutil.move(str(dst), str(src))       # roll back the move if the record rename fails
        raise
    sharing.rename_user(username, new_username)
    return final


def delete_account(username: str) -> None:
    """Permanently remove a user account, workspace files, and public-share references."""
    username = username.strip().lower()
    home = paths.user_home(username)
    auth.delete_user(username)
    sharing.drop_user(username)
    if home.exists():
        shutil.rmtree(home)


# ---- pages (per-bubble mini-wiki) ----
def list_pages(home: Path, slug: str) -> list[dict]:
    with paths.use_root(home):
        return bubbles.list_pages(slug)


def get_page(home: Path, slug: str, page_slug: str) -> str:
    with paths.use_root(home):
        return bubbles.get_page(slug, page_slug)


def save_page(home: Path, slug: str, page_slug: str, content: str,
              base_mtime: "float | None" = None) -> float:
    with paths.use_root(home):
        return bubbles.save_page(slug, page_slug, content, base_mtime)


def create_page(home: Path, slug: str, title: str) -> str:
    with paths.use_root(home):
        return bubbles.create_page(slug, title)


def rename_page(home: Path, slug: str, page_slug: str, title: str) -> None:
    with paths.use_root(home):
        bubbles.rename_page(slug, page_slug, title)


def set_page_hidden(home: Path, slug: str, page_slug: str, hidden: bool) -> None:
    with paths.use_root(home):
        bubbles.set_page_hidden(slug, page_slug, hidden)


def reorder_pages(home: Path, slug: str, page_slugs: list[str]) -> None:
    with paths.use_root(home):
        bubbles.reorder_pages(slug, page_slugs)


def delete_page(home: Path, slug: str, page_slug: str) -> bool:
    with paths.use_root(home):
        return bubbles.delete_page(slug, page_slug)


def page_poll(home: Path, slug: str, page_slug: str) -> dict:
    """Return file mtimes for the current page + manifest, plus the pages list.

    Used by the frontend's auto-sync poller to detect external edits (e.g. from
    dev-mode direct file edits) without a full page reload.
    """
    with paths.use_root(home):
        page_path = paths.bubble_page_path(slug, page_slug)
        manifest_path = paths.bubble_manifest_path(slug)
        page_mtime = page_path.stat().st_mtime if page_path.exists() else 0
        comments_mtime = bubbles.comments_mtime(slug, page_slug)
        manifest_mtime = manifest_path.stat().st_mtime if manifest_path.exists() else 0
        pages = bubbles.list_pages(slug) if manifest_path.exists() else []
    return {"page_mtime": page_mtime, "comments_mtime": comments_mtime,
            "manifest_mtime": manifest_mtime, "pages": pages}


def list_comments(home: Path, slug: str, page_slug: str) -> dict:
    with paths.use_root(home): return bubbles.list_comments(slug, page_slug)


def create_comment(home: Path, slug: str, page_slug: str, author: str, body: str, anchor: dict) -> dict:
    with paths.use_root(home): return bubbles.create_comment(slug, page_slug, author, body, anchor)


def reply_comment(home: Path, slug: str, page_slug: str, thread_id: str, author: str, body: str) -> dict:
    with paths.use_root(home): return bubbles.reply_comment(slug, page_slug, thread_id, author, body)


def edit_comment_message(home: Path, slug: str, page_slug: str, thread_id: str, message_id: str, author: str, body: str) -> dict:
    with paths.use_root(home): return bubbles.edit_comment_message(slug, page_slug, thread_id, message_id, author, body)


def set_comment_status(home: Path, slug: str, page_slug: str, thread_id: str, status: str, actor: str) -> dict:
    with paths.use_root(home): return bubbles.set_comment_status(slug, page_slug, thread_id, status, actor)


def delete_comment(home: Path, slug: str, page_slug: str, thread_id: str) -> bool:
    with paths.use_root(home): return bubbles.delete_comment(slug, page_slug, thread_id)


# ---- todos (global per-user, referenced from report pages as @<id>) ----
_TODO_REF_RE = re.compile(r"@(\d+)")


def _scan_references() -> tuple[dict, dict]:
    """Scan every bubble report page for ``@<id>`` references (assumes an active use_root).

    Returns ``(counts, locations)`` where ``counts`` maps ``todo_id -> int`` and ``locations``
    maps ``todo_id -> [{bubble_slug, bubble_name, page_slug, page_title}, ...]``. References are
    tallied by integer value, so ``@50`` never counts toward ``@5``. References inside code
    fences are still counted — over-counting only ever blocks a delete, which is intended.
    """
    counts: dict[int, int] = {}
    locations: dict[int, list[dict]] = {}
    reg = bubbles.load_registry()
    for slug, entry in reg.items():
        name = entry.get("name", slug)
        try:
            # Read the manifest directly — NOT list_pages(), which calls ensure_pages() and
            # would seed/clobber a default page. This scan must stay strictly read-only.
            pages = bubbles.manifest(slug).get("pages", [])
        except Exception:  # noqa: BLE001 — a broken bubble must not break the whole scan
            continue
        for p in pages:
            md = bubbles.get_page(slug, p["page_slug"])
            seen_on_page: set[int] = set()
            for m in _TODO_REF_RE.finditer(md or ""):
                tid = int(m.group(1))
                counts[tid] = counts.get(tid, 0) + 1
                if tid not in seen_on_page:
                    seen_on_page.add(tid)
                    locations.setdefault(tid, []).append(
                        {"bubble_slug": slug, "bubble_name": name,
                         "page_slug": p["page_slug"], "page_title": p["title"]})
    return counts, locations


def _rewrite_todo_references(id_map: dict[int, int]) -> None:
    """Rewrite report-page ``@<id>`` references after TODO ids are compacted."""
    if not id_map:
        return
    reg = bubbles.load_registry()
    for slug in reg:
        try:
            pages = bubbles.manifest(slug).get("pages", [])
        except Exception:  # noqa: BLE001
            continue
        for p in pages:
            md = bubbles.get_page(slug, p["page_slug"])

            def repl(m: re.Match) -> str:
                old = int(m.group(1))
                return f"@{id_map.get(old, old)}"

            new_md = _TODO_REF_RE.sub(repl, md or "")
            if new_md != md:
                bubbles.save_page(slug, p["page_slug"], new_md)


def list_todos(home: Path) -> list[dict]:
    with paths.use_root(home):
        items = todos.list_todos()
        counts, _ = _scan_references()
    for t in items:
        t["ref_count"] = counts.get(t["id"], 0)
    return items


def get_todo(home: Path, tid: int) -> dict | None:
    with paths.use_root(home):
        todo = todos.get_todo(tid)
        if todo is None:
            return None
        _, locations = _scan_references()
    todo = dict(todo)
    refs = locations.get(int(tid), [])
    todo["ref_count"] = len(refs)
    todo["refs"] = refs
    return todo


def add_todo(home: Path, title: str, note: str = "") -> dict:
    with paths.use_root(home):
        return todos.add_todo(title, note)


def update_todo(home: Path, tid: int, **fields) -> dict:
    with paths.use_root(home):
        return todos.update_todo(tid, **fields)


def delete_todo(home: Path, tid: int) -> bool:
    """Delete a TODO only if it has zero ``@<id>`` references. Raises ValueError otherwise."""
    with paths.use_root(home):
        _, locations = _scan_references()
        refs = locations.get(int(tid), [])
        if refs:
            where = ", ".join(f"{r['bubble_name']} → {r['page_title']}" for r in refs)
            raise ValueError(
                f"Referenced in {len(refs)} place(s): {where}. "
                f"Remove the @{int(tid)} references first.")
        deleted, id_map = todos.delete_todo(tid)
        if deleted:
            _rewrite_todo_references(id_map)
        return deleted


# ---- figures ----
def save_bubble_image(home: Path, slug: str, filename: str, data: bytes) -> str:
    with paths.use_root(home):
        return bubbles.save_bubble_image(slug, filename, data)


def bubble_asset_path(home: Path, slug: str, filename: str) -> Path:
    with paths.use_root(home):
        return paths.bubble_assets_dir(slug) / filename


def list_bubble_assets(home: Path, slug: str) -> list[dict]:
    with paths.use_root(home):
        return bubbles.list_bubble_assets(slug)


def delete_bubble_asset(home: Path, slug: str, filename: str) -> bool:
    with paths.use_root(home):
        return bubbles.delete_bubble_asset(slug, filename)


def bubble_text_asset(home: Path, slug: str, filename: str) -> str:
    with paths.use_root(home):
        return bubbles.read_bubble_text_asset(slug, filename)


# ---- streaming (generators manage their own root) ----
def chat(home: Path, slug: str, page_slug: str, messages: list[dict], page_context: str = "",
         deep_read_ids: list[str] | None = None):
    return reports.chat_stream(home, slug, page_slug, messages, page_context, deep_read_ids)


# ---- chat sessions ----
def list_chat_sessions(home: Path, slug: str) -> list[dict]:
    with paths.use_root(home):
        return bubbles.list_chat_sessions(slug)


def get_chat_session(home: Path, slug: str, session_id: str) -> dict | None:
    with paths.use_root(home):
        return bubbles.get_chat_session(slug, session_id)


def save_chat_session(home: Path, slug: str, session_id: str, title: str,
                      messages: list[dict]) -> None:
    with paths.use_root(home):
        bubbles.save_chat_session(slug, session_id, title, messages)


def delete_chat_session(home: Path, slug: str, session_id: str) -> bool:
    with paths.use_root(home):
        return bubbles.delete_chat_session(slug, session_id)


# ---- model settings ----
def get_model_config(home: Path) -> dict:
    return models.load_config(home)


def save_model_config(home: Path, cfg: dict) -> dict:
    return models.save_config(home, cfg)


def set_active_provider(home: Path, provider: str) -> dict:
    return models.set_active_provider(home, provider)


def generate_chat_title(home: Path, messages: list[dict]) -> str:
    return reports.generate_chat_title(home, messages)


def model_health(home: Path, *, live: bool = False) -> dict:
    return models.health_check(home, live=live)


# ---- math config ----
def load_math_config(home: Path) -> dict:
    with paths.use_root(home):
        p = paths.MATH_CONFIG_YAML
        if not p.exists():
            return {"macros": {}}
        return yaml.safe_load(p.read_text()) or {"macros": {}}


def save_math_config(home: Path, cfg: dict) -> dict:
    with paths.use_root(home):
        p = paths.MATH_CONFIG_YAML
        p.parent.mkdir(parents=True, exist_ok=True)
        bubbles._atomic_write(p, yaml.dump(cfg, allow_unicode=True))
    return cfg

# ---- aesthetics config ----
THEMES = ("dark", "light", "pink", "techno", "pearl")


def load_aesthetics_config(home: Path) -> dict:
    """Return the themes this user makes available in the app and shared pages."""
    with paths.use_root(home):
        p = paths.AESTHETICS_CONFIG_YAML
        data = yaml.safe_load(p.read_text()) if p.exists() else {}
    enabled = data.get("themes") if isinstance(data, dict) else None
    if not isinstance(enabled, list):
        enabled = list(THEMES)
    enabled = [theme for theme in THEMES if theme in enabled]
    return {"themes": enabled or list(THEMES)}


def save_aesthetics_config(home: Path, themes: list[str]) -> dict:
    enabled = [theme for theme in THEMES if theme in themes]
    if not enabled:
        raise ValueError("Choose at least one theme.")
    cfg = {"themes": enabled}
    with paths.use_root(home):
        p = paths.AESTHETICS_CONFIG_YAML
        p.parent.mkdir(parents=True, exist_ok=True)
        bubbles._atomic_write(p, yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    return cfg
