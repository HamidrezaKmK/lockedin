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
        "MATH_CONFIG_YAML": root / "config" / "math.yaml",
        "AESTHETICS_CONFIG_YAML": root / "config" / "aesthetics.yaml",
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


def bubble_comments_dir(slug: str) -> Path:
    """Private review-thread metadata, kept separate from report Markdown."""
    return bubble_dir(slug) / "comments"


def bubble_page_comments_path(slug: str, page_slug: str) -> Path:
    return bubble_comments_dir(slug) / f"{page_slug}.json"


def ensure_user_dirs(user: str) -> None:
    """Create a user's ASSETS/, REPORTS/, config/ (idempotent)."""
    home = user_home(user)
    for sub in ("ASSETS", "REPORTS", "config"):
        (home / sub).mkdir(parents=True, exist_ok=True)


def bubble_talks_dir(slug: str) -> Path:
    """Chalk talks: dated slide decks the agent writes to explain an idea."""
    return bubble_dir(slug) / "talks"


def bubble_talk_path(slug: str, talk_id: str) -> Path:
    return bubble_talks_dir(slug) / f"{talk_id}.md"


def bubble_talk_notes_path(slug: str, talk_id: str) -> Path:
    """Your marks on a deck. A sidecar, so your pen never edits the agent's record."""
    return bubble_talks_dir(slug) / f"{talk_id}.notes.yaml"


def bubble_talk_history_path(slug: str, talk_id: str) -> Path:
    return bubble_talks_dir(slug) / f"{talk_id}.history.yaml"


def bubble_talk_shots_dir(slug: str) -> Path:
    """Rendered snapshots of a marked slide, one per note.

    Kept out of ``REPORTS/<slug>/assets/`` deliberately: those are *report figures*, must be flat
    because of the single-segment serving route, and are listed as the bubble's files. A note
    snapshot is neither authored nor addressable by the user — its name is derived from ids — so
    it lives in its own directory and is served by its own route.
    """
    return bubble_talks_dir(slug) / "shots"

