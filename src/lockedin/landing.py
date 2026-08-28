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
        "copy": "Lockedin was built out of my fascination — and frustration — working with AI for research. More often than not, my sessions turned into walls of text and jargon that neither I nor the agent could follow. Rather than automating research and replacing humans, Lockedin focuses on keeping them in the loop, and make agents good colleagues and collaborators, while enriching how they express their thoughts.",
        "points": [
            {"title": "Review Together", "text": "Meet with your agent, then mark the work directly."},
            {"title": "Research Hub", "text": "Keep papers, code, Overleaf, and resources in one shared context."},
            {"title": "Bring Your AI", "text": "Use your OpenAI, Anthropic, or Google AI subscription."},
        ],
    },
    "auth": {
        "title": "Enter your workspace",
        "note": "Log in, or request access to give your agent somewhere to write.",
    },
    "why": {
        "title": "Communicate Like a Fellow Researcher",
        "text": "Creative thinking needs more than chat. Lockedin gives agents better ways to collaborate.",
        "bullets": [],
    },
    "workflow": {
        "title": "From Paper Pile to Working Theory",
        "intro": "Follows the day-to-day work of a researcher",
        "steps": [
            {"number": "01", "title": "Library", "text": "Move in PDFs, papers and resources"},
            {"number": "02", "title": "Setup a Goal", "text": "Creat a Bubble, which is an idea and an abstract with a goal"},
            {"number": "03", "title": "Link Documents", "text": "Link papers and resources alongside Overleaf"},
            {"number": "04", "title": "Setup Chalk Talks", "text": "Meetup with your agent every once in a while for a round of creative-thinking"},
            {"number": "05", "title": "Feedback Loop", "text": "Give feedback, improve, communicate clearly"},
        ],
    },
    "components": {
        "title": "Features",
        "intro": "AI is only as good as its context, our features are built around enriching it.",
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
        "text": "lockedin does not ship a new model — it integrates with the subscription you already have.",
        "bullets": [
            "Claude, Codex, Gemini — or Qwen on your own GPU.",
            "Your key stays yours; your work stays on your disk.",
            "Switching model costs nothing, Lockedin keeps a persistant history across models.",
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
            {"title": "Paste it in your repo", "text": "Installs the clients and binds the folder.",
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
