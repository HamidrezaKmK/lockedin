"""Centralized filesystem layout.

Two roots, mirroring the ocd pattern:

* the **base root** — the project (or ``LOCKEDIN_HOME``) directory that holds ``data/`` and,
  for the multi-user server, ``data/users/``;
* a **context root** — a per-user workspace pushed temporarily with :func:`use_root`.

Per-user paths (``ASSETS_DIR``, ``REPORTS_DIR``, ``CONFIG_DIR`` …) resolve against the
*context* root when one is active, else the base root. The server pushes each request's
user workspace so the same code serves every user::

    with paths.use_root(paths.user_home("sth")):
        ...

Account-registry paths (``USERS_DIR``, ``ACCOUNTS_YAML``, :func:`user_home`) always resolve
against the *base* root — they live above any single user's workspace.
"""
from __future__ import annotations

import contextvars
import os
from contextlib import contextmanager
from pathlib import Path

_context_root: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "lockedin_context_root", default=None)


def base_root() -> Path:
    """Project root: ``LOCKEDIN_HOME`` if set, else three parents up from this file."""
    env = os.environ.get("LOCKEDIN_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def project_root() -> Path:
    """The currently active root (context workspace if one is pushed, else base)."""
    return _context_root.get() or base_root()


@contextmanager
def use_root(root: Path):
    """Temporarily resolve per-user paths against ``root``."""
    token = _context_root.set(Path(root).resolve())
    try:
        yield
    finally:
        _context_root.reset(token)


def user_home(user: str) -> Path:
    """Per-user workspace root."""
    return base_root() / "data" / "users" / user


# Names resolved against the *context* root (per-user when active).
def _context_paths() -> dict[str, Path]:
    root = project_root()
    return {
        "ROOT": root,
        "CONFIG_DIR": root / "config",
        "ASSETS_DIR": root / "ASSETS",
        "REPORTS_DIR": root / "REPORTS",
        "ACTIVE_MODEL_YAML": root / "config" / "active_model.yaml",
        "BUBBLES_YAML": root / "bubbles.yaml",
        "TODOS_YAML": root / "todos.yaml",
        "NEWS_CONFIG_YAML": root / "config" / "news.yaml",
        "MATH_CONFIG_YAML": root / "config" / "math.yaml",
        "AESTHETICS_CONFIG_YAML": root / "config" / "aesthetics.yaml",
        "NEWS_ITEMS_YAML": root / "news_items.yaml",
        "BUBBLE_SUMMARIES_YAML": root / "bubble_summaries.yaml",
    }


# Names resolved against the *base* root (the user registry, above any workspace).
def _base_paths() -> dict[str, Path]:
    users = base_root() / "data" / "users"
    return {"USERS_DIR": users, "ACCOUNTS_YAML": users / "accounts.yaml",
            "SHARE_INDEX": users / "share_index.yaml"}


def __getattr__(name: str) -> Path:  # PEP 562 — dynamic module attributes
    ctx = _context_paths()
    if name in ctx:
        return ctx[name]
    base = _base_paths()
    if name in base:
        return base[name]
    raise AttributeError(f"module 'lockedin.paths' has no attribute {name!r}")


# --------------------------------------------------------------------------- #
# Per-asset / per-bubble helpers (resolve against the active context root)
# --------------------------------------------------------------------------- #
def asset_dir(pdf_id: str) -> Path:
    return _context_paths()["ASSETS_DIR"] / pdf_id


def bubble_dir(slug: str) -> Path:
    return _context_paths()["REPORTS_DIR"] / slug


def bubble_report_path(slug: str) -> Path:
    """Legacy single-report path (v1). Kept for migration into the multi-page layout."""
    return bubble_dir(slug) / "report.md"


def bubble_manifest_path(slug: str) -> Path:
    return bubble_dir(slug) / "pages.yaml"


def bubble_pages_dir(slug: str) -> Path:
    return bubble_dir(slug) / "pages"


def bubble_page_path(slug: str, page_slug: str) -> Path:
    return bubble_pages_dir(slug) / f"{page_slug}.md"


def bubble_assets_dir(slug: str) -> Path:
    return bubble_dir(slug) / "assets"


def bubble_chats_dir(slug: str) -> Path:
    return bubble_dir(slug) / "chats"


def news_chats_dir() -> Path:
    """Per-user archive of ended crawl conversations (one JSON per session)."""
    return _context_paths()["ROOT"] / "news_chats"


def ensure_user_dirs(user: str) -> None:
    """Create a user's ASSETS/, REPORTS/, config/ (idempotent)."""
    home = user_home(user)
    for sub in ("ASSETS", "REPORTS", "config"):
        (home / sub).mkdir(parents=True, exist_ok=True)
