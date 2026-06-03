"""Shared service layer — the single place the server calls into.

Non-streaming operations are wrapped in ``paths.use_root(home)`` here so the server stays
pure HTTP glue. Streaming generators (chat / generate / edit) manage their own root context
internally (they must keep it open across yields), so those are thin pass-throughs.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import assets, auth, bubbles, models, paths, reports, sharing


def ensure_workspace(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    for sub in ("ASSETS", "REPORTS", "config"):
        (home / sub).mkdir(parents=True, exist_ok=True)


# ---- assets ----
def save_asset(home: Path, pdf_bytes: bytes, filename: str, title: str = "",
               tags: list[str] | None = None, url_source: str = "") -> str:
    with paths.use_root(home):
        return assets.save_asset(pdf_bytes, filename, title=title, tags=tags, url_source=url_source)


def list_assets(home: Path) -> list[dict]:
    with paths.use_root(home):
        return assets.list_assets()


def get_asset(home: Path, pdf_id: str) -> dict:
    with paths.use_root(home):
        return assets.load_meta(pdf_id)


def update_asset(home: Path, pdf_id: str, **fields) -> dict:
    with paths.use_root(home):
        return assets.update_asset(pdf_id, **fields)


def delete_asset(home: Path, pdf_id: str) -> bool:
    with paths.use_root(home):
        return assets.delete_asset(pdf_id)


def asset_pdf_path(home: Path, pdf_id: str) -> Path:
    with paths.use_root(home):
        return assets.pdf_path(pdf_id)


def asset_summary(home: Path, pdf_id: str) -> str:
    with paths.use_root(home):
        return assets.get_summary(pdf_id)


def attention_queue(home: Path) -> list[dict]:
    with paths.use_root(home):
        return assets.attention_queue()


# ---- bubbles ----
def list_bubbles(home: Path) -> list[dict]:
    with paths.use_root(home):
        return bubbles.all_bubbles()


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


# ---- public sharing ----
def set_bubble_share(home: Path, slug: str, active: bool) -> dict:
    """Toggle a bubble's unlisted public share and register its token in the global index."""
    with paths.use_root(home):
        res = bubbles.set_share_active(slug, active)
    if res.get("share_token"):
        sharing.register(res["share_token"], home.name, slug)
    return res


def share_target(token: str) -> tuple[Path, str] | None:
    """Resolve a share token to ``(home, slug)`` IF the bubble is currently shared, else None."""
    ent = sharing.resolve(token)
    if not ent:
        return None
    home = paths.user_home(ent["user"])
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


# ---- pages (per-bubble mini-wiki) ----
def list_pages(home: Path, slug: str) -> list[dict]:
    with paths.use_root(home):
        return bubbles.list_pages(slug)


def get_page(home: Path, slug: str, page_slug: str) -> str:
    with paths.use_root(home):
        return bubbles.get_page(slug, page_slug)


def save_page(home: Path, slug: str, page_slug: str, content: str) -> None:
    with paths.use_root(home):
        bubbles.save_page(slug, page_slug, content)


def create_page(home: Path, slug: str, title: str) -> str:
    with paths.use_root(home):
        return bubbles.create_page(slug, title)


def rename_page(home: Path, slug: str, page_slug: str, title: str) -> None:
    with paths.use_root(home):
        bubbles.rename_page(slug, page_slug, title)


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
        manifest_mtime = manifest_path.stat().st_mtime if manifest_path.exists() else 0
        pages = bubbles.list_pages(slug) if manifest_path.exists() else []
    return {"page_mtime": page_mtime, "manifest_mtime": manifest_mtime, "pages": pages}


# ---- figures ----
def save_bubble_image(home: Path, slug: str, filename: str, data: bytes) -> str:
    with paths.use_root(home):
        return bubbles.save_bubble_image(slug, filename, data)


def bubble_asset_path(home: Path, slug: str, filename: str) -> Path:
    with paths.use_root(home):
        return paths.bubble_assets_dir(slug) / filename


# ---- streaming (generators manage their own root) ----
def chat(home: Path, slug: str, page_slug: str, messages: list[dict], page_context: str = "",
         deep_read_ids: list[str] | None = None, *, claude_token: str = ""):
    return reports.chat_stream(home, slug, page_slug, messages, page_context, deep_read_ids,
                               claude_token=claude_token)


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


def generate_chat_title(home: Path, messages: list[dict], *, claude_token: str = "") -> str:
    return reports.generate_chat_title(home, messages, claude_token=claude_token)


def model_health(home: Path, *, claude_token: str = "") -> dict:
    return models.health_check(home, claude_token=claude_token)
