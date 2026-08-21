"""Shared test fixtures + helpers.

The deterministic tests build throwaway workspaces under ``paths.use_root`` and never touch
real user data or the network. The live qwen test seeds a temp workspace with the diffusion
PDFs copied from local data (the ``unittest`` user created by ``setup_unittest_user.py``, or
``sth`` as a fallback) — it copies only ``meta.yaml``/``summary.md``/``text.txt`` (not the
50 MB ``paper.pdf``), since the tests only need the cached summaries.
"""
from __future__ import annotations

import shutil
import urllib.error
import urllib.request
from pathlib import Path

import lockedin
from lockedin import bubbles, models, paths

# Real on-disk data dir of this checkout (data/users/<user>/…), independent of LOCKEDIN_HOME.
REPO_ROOT = Path(lockedin.__file__).resolve().parents[2]
REPO_DATA_USERS = REPO_ROOT / "data" / "users"

DIFFUSION_BUBBLE = "diffusion-models"
# pdf_id -> title, for the two papers the diffusion bubble is built around.
DIFFUSION_PDFS = {
    "5c9310e62e90": "Generative Models Via Drifting",
    "e48b9e55962e": "A geometric view of data complexity",
}


def qwen_reachable(base_url: str = "http://localhost:11434/v1") -> bool:
    """True if a local Ollama (OpenAI-compatible) endpoint answers /models."""
    try:
        urllib.request.urlopen(base_url.rstrip("/") + "/models", timeout=2)
        return True
    except (urllib.error.URLError, OSError):
        return False


def source_user_with_pdfs() -> str | None:
    """Pick a local user whose ASSETS contain the diffusion PDFs."""
    for user in ("unittest"):
        adir = REPO_DATA_USERS / user / "ASSETS"
        if all((adir / pid / "meta.yaml").exists() for pid in DIFFUSION_PDFS):
            return user
    return None


def set_qwen(home: Path, model: str = "qwen2.5:7b-instruct") -> None:
    models.save_config(home, {"active": "qwen",
                              "qwen": {"base_url": "http://localhost:11434/v1", "model": model}})


def seed_diffusion_workspace(home: Path, source_user: str) -> list[str]:
    """Copy the diffusion PDFs' metadata/summaries into ``home``, create+approve the bubble.

    Copies only meta.yaml/summary.md/text.txt (no raw PDF needed). Returns
    the list of pdf_ids seeded. Caller must have created ``home`` (e.g. via auth/ensure_dirs).
    """
    src_assets = REPO_DATA_USERS / source_user / "ASSETS"
    dst_assets = home / "ASSETS"
    dst_assets.mkdir(parents=True, exist_ok=True)
    for pid in DIFFUSION_PDFS:
        (dst_assets / pid).mkdir(parents=True, exist_ok=True)
        for fname in ("meta.yaml", "summary.md", "text.txt"):
            src = src_assets / pid / fname
            if src.exists():
                shutil.copy2(src, dst_assets / pid / fname)
    with paths.use_root(home):
        bubbles.create_bubble("Diffusion Models")
        bubbles.approve_bubble(DIFFUSION_BUBBLE, "")
        bubbles.ensure_pages(DIFFUSION_BUBBLE)
        # A realistic bubble has a page per paper; their slugs match the overview's links.
        for title in DIFFUSION_PDFS.values():
            bubbles.create_page(DIFFUSION_BUBBLE, title)
    return list(DIFFUSION_PDFS)


def write_overview(home: Path, content: str, bubble: str = DIFFUSION_BUBBLE) -> None:
    with paths.use_root(home):
        bubbles.save_page(bubble, "overview", content)


def read_overview(home: Path, bubble: str = DIFFUSION_BUBBLE) -> str:
    with paths.use_root(home):
        return bubbles.get_page(bubble, "overview")


def make_bubble(home: Path, name: str = "Diffusion Models") -> str:
    """Create+approve an empty bubble (no PDFs) for pure-logic tests."""
    with paths.use_root(home):
        slug = bubbles.create_bubble(name)
        bubbles.approve_bubble(slug, "")
        bubbles.ensure_pages(slug)
    return slug
