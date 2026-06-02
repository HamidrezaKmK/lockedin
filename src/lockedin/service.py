"""Shared service layer — the single place the server calls into.

Non-streaming operations are wrapped in ``paths.use_root(home)`` here so the server stays
pure HTTP glue. Streaming generators (chat / generate / edit) manage their own root context
internally (they must keep it open across yields), so those are thin pass-throughs.
"""
from __future__ import annotations

from pathlib import Path

from . import assets, bubbles, models, paths, reports


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


def approve_bubble(home: Path, slug: str, instructions: str = "") -> dict:
    with paths.use_root(home):
        return bubbles.approve_bubble(slug, instructions)


def delete_bubble(home: Path, slug: str) -> None:
    with paths.use_root(home):
        bubbles.delete_bubble(slug)


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


# ---- figures ----
def save_bubble_image(home: Path, slug: str, filename: str, data: bytes) -> str:
    with paths.use_root(home):
        return bubbles.save_bubble_image(slug, filename, data)


def bubble_asset_path(home: Path, slug: str, filename: str) -> Path:
    with paths.use_root(home):
        return paths.bubble_assets_dir(slug) / filename


# ---- streaming (generators manage their own root) ----
def generate_report(home: Path, slug: str, page_slug: str, instructions: str = "",
                    *, claude_token: str = ""):
    return reports.generate_template(home, slug, page_slug, instructions, claude_token=claude_token)


def edit_page(home: Path, slug: str, page_slug: str, mode: str, instruction: str,
             page_context: str, section=None, rejected_proposal=None, *, claude_token: str = ""):
    return reports.edit_page(home, slug, page_slug=page_slug, mode=mode,
                             instruction=instruction, page_context=page_context,
                             section=section, rejected_proposal=rejected_proposal,
                             claude_token=claude_token)


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


def compact_chat(home: Path, messages: list[dict], *, claude_token: str = "") -> list[dict]:
    return models.compact_chat(home, messages, claude_token=claude_token)


def generate_chat_title(home: Path, messages: list[dict], *, claude_token: str = "") -> str:
    return reports.generate_chat_title(home, messages, claude_token=claude_token)


def model_health(home: Path, *, claude_token: str = "") -> dict:
    return models.health_check(home, claude_token=claude_token)
