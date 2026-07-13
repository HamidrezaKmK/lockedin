"""Structured public landing-page content loaded from data/landing.yaml."""
from __future__ import annotations

import copy
import logging
from typing import Any

import yaml

from . import paths

log = logging.getLogger(__name__)


DEFAULT_LANDING: dict[str, Any] = {
    "hero": {
        "kicker_icon": "🔒",
        "kicker": "private research workspace",
        "title_accent": "locked",
        "title_rest": "in",
        "lede": "A calm command center for papers, math notes, topic wikis, research chat, and the small TODOs that keep a project moving.",
        "copy": "Upload PDFs, organize them into bubbles, write math-aware reports, cite your own library, and keep model-powered research help close to the work without moving your notes into a public platform.",
        "points": [
            {"title": "Paper-first", "text": "Your library keeps source PDFs, tags, notes, summaries, and BibTeX together."},
            {"title": "Math-aware", "text": "Reports render equations, theorem boxes, citations, wikilinks, and TODO references."},
            {"title": "Private by default", "text": "Run locally or expose it only through your own HTTPS tunnel when needed."},
        ],
    },
    "auth": {
        "title": "Enter your workspace",
        "note": "Log in, or create an account and start building your research graph.",
    },
    "workflow": {
        "title": "From paper pile to working theory",
        "intro": "lockedin follows the everyday research loop: collect sources, shape topic clusters, write technical notes, then use chat, sharing, and tasks to keep context alive.",
        "steps": [
            {"number": "01", "title": "Upload papers", "text": "Add PDFs or PDF links, then capture titles, tags, source URLs, summaries, notes, and BibTeX."},
            {"number": "02", "title": "Organize into bubbles", "text": "Group papers into approved topic spaces with their own multi-page wiki and attached papers."},
            {"number": "03", "title": "Write reports", "text": "Use Markdown with KaTeX equations, labels, theorem environments, citations, TODO refs, and wikilinks."},
            {"number": "04", "title": "Chat, share, track", "text": "Discuss a bubble with grounded research chat, publish read-only links, and keep TODOs connected to notes."},
        ],
    },
    "components": {
        "title": "The pieces that stay connected",
        "intro": "Every view is built around repeated research work rather than a separate marketing funnel.",
        "features": [
            {"icon": "📚", "title": "Library", "text": "Your complete paper collection with upload, URL fetch, filters, notes, tags, summaries, and BibTeX validation."},
            {"icon": "🫧", "title": "Bubbles", "text": "Topic workspaces that bind papers, pages, chat sessions, citations, and share settings."},
            {"icon": "∑", "title": "Reports", "text": "Markdown pages with rendered math, numbered equations, theorem boxes, references, images, and tables."},
            {"icon": "✅", "title": "TODOs", "text": "Issue-style tasks with report references, open/done filters, notes, and automatic reference cleanup."},
            {"icon": "💬", "title": "Research Chat", "text": "A read-only assistant grounded in the current bubble, paper summaries, and selected deep-read PDFs."},
            {"icon": "#", "title": "Slackbot", "text": "Use Slack to select bubbles, ask questions, add papers, and manage TODOs."},
            {"icon": "⚙️", "title": "Model Settings", "text": "Switch between Qwen, OpenAI, Claude, and Gemini, configure keys, and manage math macros."},
            {"icon": "🔗", "title": "Sharing", "text": "Publish unlisted read-only bubble links, copy them, and revoke access without changing the private workspace."},
        ],
    },
    "privacy": {
        "title": "Local and private-first by default",
        "text": "lockedin is designed for research notes that should stay close to the machine, account, and model configuration you control.",
        "bullets": [
            "User data stays behind login; public share pages are unlisted and read-only.",
            "Standard accounts can bring their own OpenAI, Claude, or Gemini API key. Server-side Qwen can be limited to premium users.",
            "For remote access, run the app behind your own HTTPS tunnel or domain setup instead of exposing the local server directly.",
        ],
    },
    "scientist": {
        "title": "Use Scientist from your computer",
        "intro": "A small, dependency-free companion for your installed Codex, Claude, or Antigravity CLI. It mirrors only your authorized workspace and keeps it synchronized while you work.",
        "platforms": [
            {"title": "macOS or Linux", "text": "Requires Python 3.11+ and adds one command to ~/.local/bin.",
             "command": "curl -fsSL https://raw.githubusercontent.com/HamidrezaKmK/lockedin/scientist/install.sh | bash"},
            {"title": "Windows PowerShell", "text": "Requires Python 3.11+ and installs the client under your local app data folder.",
             "command": "irm https://raw.githubusercontent.com/HamidrezaKmK/lockedin/scientist/install.ps1 | iex"},
        ],
        "steps": [
            {"title": "Sign in once", "text": "Authorize this device in your browser.",
             "command": "lockedin-scientist login --server https://lockedin.codes"},
            {"title": "Choose a bubble", "text": "See the active bubble names and slugs you may use.",
             "command": "lockedin-scientist bubbles"},
            {"title": "Sync any time", "text": "Pull/push once without starting an assistant.",
             "command": "lockedin-scientist sync"},
            {"title": "Start your assistant", "text": "Use its slug with Codex, Claude, or Antigravity.",
             "command": "lockedin-scientist <codex|claude|agy> <bubble-slug>"},
        ],
        "note": "Your server URL is chosen at login; the installer does not install the LockedIn server.",
    },
    "footer": "Made for focused research sessions by HamidrezaKmK.",
}


def landing_yaml_path():
    return paths.base_root() / "data" / "landing.yaml"


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return default


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _section(src: Any, default: dict[str, Any], list_shapes: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    if not isinstance(src, dict):
        src = {}
    out: dict[str, Any] = {}
    list_shapes = list_shapes or {}
    for key, fallback in default.items():
        if key in list_shapes:
            items = _list(src.get(key, fallback))
            shape = list_shapes[key]
            out[key] = [
                {field: _text(item.get(field), item_default) for field, item_default in shape.items()}
                for item in items
                if isinstance(item, dict)
            ]
            continue
        if isinstance(fallback, list):
            out[key] = [_text(v) for v in _list(src.get(key, fallback))]
        else:
            out[key] = _text(src.get(key), str(fallback))
    return out


def normalize_landing(data: Any) -> dict[str, Any]:
    """Return a public-safe landing dict, using defaults for missing or invalid fields."""
    if not isinstance(data, dict):
        data = {}
    d = DEFAULT_LANDING
    return {
        "hero": _section(data.get("hero"), d["hero"], {
            "points": {"title": "", "text": ""},
        }),
        "auth": _section(data.get("auth"), d["auth"]),
        "workflow": _section(data.get("workflow"), d["workflow"], {
            "steps": {"number": "", "title": "", "text": ""},
        }),
        "components": _section(data.get("components"), d["components"], {
            "features": {"icon": "", "title": "", "text": ""},
        }),
        "privacy": _section(data.get("privacy"), d["privacy"]),
        "scientist": _section(data.get("scientist"), d["scientist"], {
            "platforms": {"title": "", "text": "", "command": ""},
            "steps": {"title": "", "text": "", "command": ""},
        }),
        "footer": _text(data.get("footer"), d["footer"]),
    }


def load_landing() -> dict[str, Any]:
    """Load global public landing content from data/landing.yaml, falling back to defaults."""
    path = landing_yaml_path()
    if not path.exists():
        return copy.deepcopy(DEFAULT_LANDING)
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not load %s; using default landing content: %s", path, exc)
        return copy.deepcopy(DEFAULT_LANDING)
    return normalize_landing(data)
