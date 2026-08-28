"""Structured public landing-page content loaded from data/landing.yaml."""
from __future__ import annotations

import copy
import logging
from typing import Any

import yaml

from . import paths

log = logging.getLogger(__name__)


DEFAULT_LANDING: dict[str, Any] = {
    # Landing copy rule: every string here is read by someone deciding in seconds whether to
    # leave. One idea per line, no paragraph over two sentences — the argument is made by the
    # demo visual, not by prose.
    "hero": {
        "kicker_icon": "🔒",
        "kicker": "Collaborative Research with AI",
        "title_accent": "locked",
        "title_rest": "in",
        "lede": "AI should not be limited to chat threads.",
        "copy": "lockedin was built out of my fascination — and frustration — working with AI for research. More often than not, my sessions turned into walls of text and jargon that neither I nor the agent could follow. Rather than automating research, this focuses on making agents better collaborators and enriching how they express their thinking.",
        "points": [
            {"title": "Chalk talks", "text": "Dated decks, one idea per slide, whenever something needs your judgement."},
            {"title": "Feedback loop", "text": "Put markers and annotations on the work, the way a PI does for their grad student."},
            {"title": "One bubble", "text": "Papers, figures, TODOs and the manuscript, right where the agent works."},
        ],
    },
    "auth": {
        "title": "Enter your workspace",
        "note": "Log in, or create an account and give your agent somewhere to write.",
    },
    "why": {
        "title": "Communicate with agents like a PI",
        "text": "Chat threads are not the most creative expression of research — so we fixed that. The agent gives chalk talks at a board, and you answer with a reviewer's pen: select the text, pick a mark, or just draw on the slide.",
        "bullets": [],
    },
    "workflow": {
        "title": "Focus on better communication",
        "intro": "Remove the walls of text.",
        "steps": [
            {"number": "01", "title": "Set up a bubble", "text": "One place for the idea: papers, pages, figures, talks."},
            {"number": "02", "title": "Set a goal", "text": "One line every agent reads first, every session."},
            {"number": "03", "title": "Write the document", "text": "Ongoing, together — the record that lasts is not the thread."},
            {"number": "04", "title": "Get chalk talks", "text": "The parts that need your judgement arrive as slides."},
            {"number": "05", "title": "Give feedback", "text": "Five marks, drawings, hand edits — on the exact line."},
        ],
    },
    "components": {
        "title": "Features",
        "intro": "An agent is only as good as what it can see. Context is something you keep, not something you re-paste every session.",
        "features": [
            {"icon": "📚", "title": "Library", "text": "Papers with notes, tags and BibTeX — indexed for you and for the agent."},
            {"icon": "🫧", "title": "Idea bubbles", "text": "One idea: its pages, figures, talks and open questions together."},
            {"icon": "💬", "title": "Chalk talks", "text": "Dated decks you mark up instead of replying in prose."},
            {"icon": "🔗", "title": "Overleaf", "text": "The manuscript stays bound to the notes it grew from."},
            {"icon": "🗨️", "title": "Slack", "text": "Papers and TODOs from the channel your group already lives in."},
            {"icon": "📋", "title": "TODOs", "text": "Open tasks, referenced from pages GitHub-issue style."},
        ],
    },
    "pi": {
        "title": "Bring your own AI sub",
        "text": "lockedin does not ship a new model — it integrates with the subscription you already have, or a local model.",
        "bullets": [
            "Claude, Codex, Gemini — or Qwen on your own GPU.",
            "Your key stays yours; your work stays on your disk.",
            "Switching model never costs you the bubble, the marks or the history.",
        ],
    },
    "privacy": {
        "title": "Open source, yours to shape",
        "text": "Run it on your own machine, adapt it to your group, and own the record of what you decided.",
        "bullets": [],
    },
    "scientist": {
        "title": "Connecting a project takes one line",
        "intro": "Open your bubble, hit 🤖, paste one line into a bash in your repo — and your source code is connected to everything: the papers, the document, the talks, your marks.",
        "platforms": [],   # the manual installer cards were clutter; the 🤖 line does it all
        "steps": [
            {"title": "Hit 🤖 on the bubble", "text": "It hands you one command, pre-signed for this machine.",
             "command": ""},
            {"title": "Paste it in your repo", "text": "Installs the client, signs you in, binds the folder, starts the sync.",
             "command": "curl -fsSL https://lockedin.codes/setup/‹ticket›.sh | bash"},
            {"title": "Run your agent as usual", "text": "claude, codex, agy — the skill is in place, and reports, talks and feedback sync every few seconds.",
             "command": ""},
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


def _migrate_retired_scientist_landing(value: Any) -> Any:
    """Render the known v1 Scientist landing section as its safe v2 replacement.

    `data/landing.yaml` is intentionally user-editable and may outlive a server update.  Preserve
    genuine custom copy, but do not keep serving the exact retired installer/agent workflow.
    """
    if not isinstance(value, dict):
        return value
    platforms = _list(value.get("platforms"))
    steps = _list(value.get("steps"))
    commands = {str(item.get("command", "")) for item in [*platforms, *steps] if isinstance(item, dict)}
    retired = {
        "curl -fsSL https://raw.githubusercontent.com/HamidrezaKmK/lockedin/scientist/install.sh | bash",
        "irm https://raw.githubusercontent.com/HamidrezaKmK/lockedin/scientist/install.ps1 | iex",
        "lockedin-scientist sync",
        "lockedin-scientist <codex|claude|agy> <bubble-name>",
    }
    if not commands.intersection(retired):
        return value
    # Retired commands describe one coherent workflow; retain only its harmless prose overrides.
    migrated = copy.deepcopy(DEFAULT_LANDING["scientist"])
    for key in ("title", "intro"):
        if key in value:
            migrated[key] = value[key]
    return migrated


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
        "why": _section(data.get("why"), d["why"]),
        "pi": _section(data.get("pi"), d["pi"]),
        "workflow": _section(data.get("workflow"), d["workflow"], {
            "steps": {"number": "", "title": "", "text": ""},
        }),
        "components": _section(data.get("components"), d["components"], {
            "features": {"icon": "", "title": "", "text": ""},
        }),
        "privacy": _section(data.get("privacy"), d["privacy"]),
        "scientist": _section(_migrate_retired_scientist_landing(data.get("scientist")), d["scientist"], {
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
