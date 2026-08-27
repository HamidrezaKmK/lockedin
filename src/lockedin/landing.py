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
        "kicker": "AI that communicates clearly for research",
        "title_accent": "locked",
        "title_rest": "in",
        "lede": "Stop reading your agent’s work in a terminal",
        "copy": "An agent that writes into a chat window is an agent whose work you cannot review. lockedin gives it somewhere to put a real document — slides, figures, derivations — and gives you a way to mark that work up the way you would a student’s.",
        "points": [
            {"title": "Read, don’t scroll", "text": "Findings arrive as dated slide decks and living report pages, not as scrollback you lose on the next session."},
            {"title": "Mark it up", "text": "✗ wrong · ? I don’t follow · → go deeper · ✓ keep · ✂ cut. Point at the exact sentence; the agent answers there."},
            {"title": "Any model", "text": "Claude, Codex, GPT, Gemini, or a local Qwen. lockedin is the interface, not the model."},
        ],
    },
    "auth": {
        "title": "Enter your workspace",
        "note": "Log in, or create an account and give your agent somewhere to write.",
    },
    "why": {
        "title": "Why this exists",
        "text": "lockedin came out of equal parts fascination and frustration with working alongside AI through a terminal. The models had become good collaborators; the interface had not. Findings scrolled past in a session and were gone, a correction meant retyping the paragraph you were objecting to, and there was nowhere for an argument to live between the chat and the paper.\n\nSo: a hub with a proper surface. The agent presents; you review it the way a supervisor reviews a student — on the page, at the sentence, in writing that lasts.",
        "bullets": [
            "Chalk talks: dated slide decks an agent writes when something needs your judgement.",
            "Marks, not replies: five of them, on slides and report pages alike, anchored to the exact text.",
            "A record that survives the session: every revision keeps the mark that caused it.",
        ],
    },
    "workflow": {
        "title": "From an experiment to something you can argue with",
        "intro": "Point an agent at the repo where the work actually happens; read what it found as a document, not a transcript.",
        "steps": [
            {"number": "01", "title": "Bubble", "text": "Open a topic. It gets a statement of the idea, a multi-page report, and a place for talks."},
            {"number": "02", "title": "Connect", "text": "Sync a code repository, a training run, or an agent session to it. The agent reads the bubble and writes back into it."},
            {"number": "03", "title": "Present", "text": "Experiment results arrive as figures, report pages, and slide decks — written to be read, with the uncertain parts marked as uncertain."},
            {"number": "04", "title": "Review", "text": "Mark the exact sentence, argue in the thread, and watch the slide come back revised with your objection on the record."},
        ],
    },
    "components": {
        "title": "What is in it",
        "intro": "One place for the papers, the writing, and the tools you already use.",
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
        "title": "Research like a PI, not a grad student",
        "text": "The pitch is not that the agent does your work. It is that you get to do the part of the job that is actually yours: reading a claim closely, finding the step that does not hold, and saying so. Someone else assembles the derivation, runs the sweep and redraws the figure — and comes back with a revision that names your objection.",
        "bullets": [
            "You read and judge; the agent derives, runs, plots and rewrites.",
            "A mark is a pointed finger plus one word — it costs a tap, not a paragraph.",
            "Marks disappear when they are answered. Nothing accumulates a list nobody reads.",
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
