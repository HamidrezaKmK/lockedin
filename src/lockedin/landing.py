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
        "kicker": "Research command center for grad students",
        "title_accent": "locked",
        "title_rest": "in",
        "lede": "Turn a pile of papers and ideas into a rigorous research",
        "copy": "Collect PDFs, shape ideas into bubbles, write technical reports, and keep your notes, references, TODOs, and AI-assisted research close to the work.",
        "points": [
            {"title": "Organize Research", "text": "Keep PDFs, notes, tags, summaries, and BibTeX in a fully indexed library."},
            {"title": "Math Help with AI", "text": "Build bubbles and math-aware reports that evolve with your project."},
            {"title": "Keep Connected", "text": "Use Slack and your preferred models without losing the context of your work."},
        ],
    },
    "auth": {
        "title": "Enter your workspace",
        "note": "Log in, or create an account and start building your research graph.",
    },
    "workflow": {
        "title": "Paper pile to working theory",
        "intro": "Collect sources, shape topic clusters, write technical notes, then chat/share/track.",
        "steps": [
            {"number": "01", "title": "Bubble", "text": "Create a theme or idea you would like to research on"},
            {"number": "02", "title": "Library", "text": "Enrich the context of that research bubble with papers and use AI-assisted summaries"},
            {"number": "03", "title": "Editing + AI-Assist", "text": "Start editing the bubble, add pages, equations, figures, like an Overleaf environment. Open up the CLI and start editing with AI, ask for relevant literature, validate theorems, and keep up-to-date."},
            {"number": "04", "title": "Share", "text": "Share ideas with team-members or compose blogposts to share with the public."},
        ],
    },
    "components": {
        "title": "Library",
        "intro": "Every view is built around repeated research work.",
        "features": [
            {"icon": "📚", "title": "Library", "text": "Keep track of your papers, books, and other research materials with appropriate notes and links to idea bubbles and bibtex entries."},
            {"icon": "🫧", "title": "Idea Bubbles", "text": "Create idea bubbles to organize your research ideas and connect them to your papers and other research materials."},
            {"icon": "🔗", "title": "Share Latex", "text": "Share your latex code with your collaborators and get feedback on your writing."},
            {"icon": "🤖", "title": "AI-powered help", "text": "Use your favourite LLM models to help with your research from your CLI and power it with a better context."},
            {"icon": "💬", "title": "Slack Sync", "text": "Sync papers and research notes across devices using Slack."},
            {"icon": "📋", "title": "TODOs", "text": "Track open research tasks and link them from your report pages with Github issue style referencing."},
        ],
    },
    "privacy": {
        "title": "Open source, yours to shape",
        "text": "Run lockedin on your own machine, adapt it to your research group, and keep ownership of the workbench where your ideas take shape.",
        "bullets": [
            "User data stays behind login; public share pages are unlisted and read-only.",
            "Standard accounts can bring their own OpenAI, Claude, or Gemini API key. Server-side Qwen can be limited to premium users.",
            "For remote access, run the app behind your own HTTPS tunnel or domain setup instead of exposing the local server directly.",
        ],
    },
    "scientist": {
        "title": "Bring your own AI subscription",
        "intro": "Install the lightweight Scientist companion, authorize, and work synchronized on the website with Codex, Claude, or Antigravity.",
        "platforms": [
            {"title": "macOS or Linux", "text": "Python 3.11+ required. Installs only the Scientist client in your user PATH.",
             "command": "curl -fsSL https://raw.githubusercontent.com/HamidrezaKmK/lockedin/scientist/install.sh | bash"},
            {"title": "Windows PowerShell", "text": "Python 3.11+ required. Installs the Scientist client in local app data.",
             "command": "irm https://raw.githubusercontent.com/HamidrezaKmK/lockedin/scientist/install.ps1 | iex"},
        ],
        "steps": [
            {"title": "Authorize", "text": "Sign in to your lockedin account in the browser.",
             "command": "lockedin-scientist login --server https://lockedin.codes"},
            {"title": "Pick a bubble", "text": "List the active workspace names and slugs available to you.",
             "command": "lockedin-scientist bubbles"},
            {"title": "Stay in sync", "text": "Pull website changes and push local report work without starting an agent.",
             "command": "lockedin-scientist sync"},
            {"title": "Start working", "text": "Launch the coding CLI you already use against one bubble.",
             "command": "lockedin-scientist <codex|claude|agy> <bubble-name>"},
        ],
    },
    "footer": "Made with 💜 by a PhD student",
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
