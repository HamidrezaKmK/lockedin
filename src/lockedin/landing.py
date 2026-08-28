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
        "kicker": "Research with AI, without the chat window",
        "title_accent": "locked",
        "title_rest": "in",
        "lede": "Your agent is not a chatbot. Stop treating it like one.",
        "copy": "We work with capable research collaborators through a chat thread — the one interface that cannot hold an argument, a figure, or a record. lockedin is an implementation of how it should work instead: the agent gives you a chalk talk, you write the document together, and you drop to the exact sentence when something is wrong.",
        "points": [
            {"title": "It presents; you review", "text": "Findings arrive as dated slide decks and living pages — the shape a colleague uses to convince you, not a transcript you scroll."},
            {"title": "Intervene at the sentence", "text": "✗ wrong · ? I don’t follow · → go deeper · ✓ keep · ✂ cut. One tap on the exact line, and the answer comes back there."},
            {"title": "One place for the context", "text": "Papers, figures, derivations, TODOs and the manuscript in the same bubble the agent is working in."},
        ],
    },
    "auth": {
        "title": "Enter your workspace",
        "note": "Log in, or create an account and give your agent somewhere to write.",
    },
    "why": {
        "title": "Chat is the wrong shape for this work",
        "text": "The models became good collaborators. The interface did not move: we still talk to them in a thread of messages, which is how you ask a question, not how you supervise research.\n\nThink about how it actually works with a good student. They do not send you a wall of text. They book half an hour and talk you through it at the board. You interrupt at the step that does not hold. What survives goes into the write-up, and the write-up is the thing you both keep. Nobody scrolls back through a conversation to find out what was decided.\n\nlockedin is that arrangement, built for an agent.",
        "bullets": [
            "It gives a chalk talk — a dated deck, one idea per slide, when something needs your judgement.",
            "You write the document together, and it is the document that lasts, not the thread.",
            "You drop to the low level when it matters: mark the exact sentence, and the argument happens there.",
        ],
    },
    "workflow": {
        "title": "How a week actually goes",
        "intro": "Not a demo flow — the loop you repeat while an idea is being worked out.",
        "steps": [
            {"number": "01", "title": "You set the question", "text": "Open a bubble, drop in the papers, say in a paragraph what you are trying to establish. That statement is what every agent reads first."},
            {"number": "02", "title": "It goes away and works", "text": "Point it at the repo. It runs the experiment, draws the figure, writes the derivation — in the bubble, where you can find it later."},
            {"number": "03", "title": "It gives a chalk talk", "text": "When something needs your judgement it writes a short deck: one idea per slide, the soft spots named, and what it needs from you at the end."},
            {"number": "04", "title": "You supervise", "text": "Mark the step that does not hold. It re-derives, argues back if you are wrong, and the revision carries your objection in its history."},
        ],
    },
    "components": {
        "title": "Everything the work needs, in one place",
        "intro": "An agent is only as good as what it can see. The papers, the figures, the open questions and the manuscript live in the same bubble it is working in — so context is something you keep, not something you re-paste every session.",
        "features": [
            {"icon": "📚", "title": "Library", "text": "Keep track of your papers, books, and other research materials with appropriate notes and links to idea bubbles and bibtex entries."},
            {"icon": "🫧", "title": "Idea Bubbles", "text": "Create idea bubbles to organize your research ideas and connect them to your papers and other research materials."},
            {"icon": "🔗", "title": "Overleaf", "text": "Keep a bubble bound to its Overleaf project so the manuscript and the working notes do not drift apart."},
            {"icon": "💬", "title": "Chalk talks", "text": "Dated slide decks an agent writes to explain an idea, which you mark up with five marks instead of replying in prose."},
            {"icon": "🤖", "title": "Your model, your key", "text": "Frontier or open — Claude, Codex, GPT, Gemini, or a local Qwen. lockedin is a better way to talk to them, not another one of them."},
            {"icon": "💬", "title": "Slack", "text": "Reach your papers, TODOs and notes from the channel your group already lives in."},
            {"icon": "📋", "title": "TODOs", "text": "Track open research tasks and link them from your report pages with Github issue style referencing."},
        ],
    },
    "pi": {
        "title": "No compromise on the model",
        "text": "lockedin is an interface, not a model, and not a wrapper that resells you one. Bring the subscription you already pay for — Claude, Codex, Gemini, GPT — or point it at a model running on your own machine. Your key stays yours, your work stays on your disk, and switching model does not cost you the bubble, the marks or the history.",
        "bullets": [
            "Works with the agent you already use, in the terminal you already use.",
            "Self-hostable and open source: run the whole thing locally if you want to.",
            "Nothing is locked to a vendor — least of all the record of what you decided.",
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
        "title": "Connecting a project takes one line",
        "intro": "In the app, the 🤖 button on any bubble gives you a single command to paste — it installs the client, signs this machine in, and connects the folder you are standing in. The steps below are only for setting it up by hand.",
        "platforms": [
            {"title": "macOS or Linux", "text": "Python 3.11+ required. Installs only the Scientist client in your user PATH.",
             "command": "curl -fsSL https://raw.githubusercontent.com/HamidrezaKmK/lockedin/main/install.sh | bash"},
            {"title": "Windows PowerShell", "text": "Python 3.11+ required. Installs the Scientist client in local app data.",
             "command": "irm https://raw.githubusercontent.com/HamidrezaKmK/lockedin/main/install.ps1 | iex"},
        ],
        "steps": [
            {"title": "Authorize", "text": "Sign in to your lockedin account in the browser.",
             "command": "lockedin-scientist login --server https://lockedin.codes"},
            {"title": "Choose a workspace", "text": "List your workspaces and select the one used across projects.",
             "command": "lockedin-scientist workspaces switch <workspace-id-or-name>"},
            {"title": "Pick a bubble", "text": "List the approved bubbles in the selected workspace.",
             "command": "lockedin-scientist bubbles"},
            {"title": "Stay in sync", "text": "Create a project-local bubble workspace that keeps reports synchronized in the background.",
             "command": "lockedin-scientist sync <bubble-slug>"},
            {"title": "Install a native skill", "text": "Set up the lockedin-scientist skill once for the agent you use.",
             "command": "lockedin-scientist <codex|claude|agy> setup"},
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
