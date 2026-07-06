"""Model layer — one *global active model* chosen by the user.

Unlike a per-role setup, lockedin uses a single active provider at a time (switched from the
top bar). Every task — tagging, summarizing, chat, report generation, edits — goes through
the same active model. Four providers are supported:

  qwen    local Ollama via its OpenAI-compatible API (premium; runs on the server)
  openai  OpenAI API (needs api_key)
  claude  Anthropic API (needs api_key)
  gemini  Google Gemini via its OpenAI-compatible endpoint (needs api_key from AI Studio)
          https://generativelanguage.googleapis.com/v1beta/openai/

Config lives per-user in ``config/active_model.yaml``::

    active: openai
    qwen:   {base_url: http://localhost:11434/v1, model: qwen2.5:7b-instruct}
    openai: {model: gpt-4o, api_key: sk-...}
    claude: {model: claude-sonnet-4-6, api_key: sk-ant-...}
    gemini: {model: gemini-2.5-flash-preview-05-20, api_key: AIza...}

Messages use the OpenAI shape (``[{"role", "content"}]``); a ``system`` string is passed
separately and adapted per provider (OpenAI: a system message; Anthropic: the ``system`` arg).
"""
from __future__ import annotations

import base64
import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

import yaml

from . import paths

PROVIDERS = ("qwen", "openai", "claude", "gemini")

# Gemini's OpenAI-compatible base URL — no extra SDK needed.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

DEFAULT_CONFIG: dict[str, Any] = {
    "active": "openai",
    "qwen": {"base_url": "http://localhost:11434/v1", "model": "qwen2.5:7b-instruct"},
    "openai": {"model": "gpt-4o", "api_key": ""},
    "claude": {"model": "claude-sonnet-4-6", "api_key": ""},
    # Get a free API key at https://aistudio.google.com/apikey
    "gemini": {"model": "gemini-2.5-flash-preview-05-20", "api_key": ""},
}

DEFAULT_MAX_TOKENS = 4096


@dataclass
class ActiveModelConfig:
    active: str = "openai"
    base_url: str = "http://localhost:11434/v1"
    model: str = "qwen2.5:7b-instruct"
    api_key: str = ""
    raw: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Config I/O
# --------------------------------------------------------------------------- #
def load_config(home: Path) -> dict:
    """Read active_model.yaml for ``home`` (a user workspace), merged over defaults."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    path = home / "config" / "active_model.yaml"
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    if cfg.get("active") not in PROVIDERS:
        cfg["active"] = "openai"
    if cfg.get("active") == "qwen" and not qwen_allowed(home):
        cfg["active"] = "openai"
    return cfg


def save_config(home: Path, cfg: dict) -> dict:
    merged = load_config(home)
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    if merged.get("active") not in PROVIDERS:
        merged["active"] = "openai"
    if merged.get("active") == "qwen" and not qwen_allowed(home):
        raise PermissionError("Qwen is a premium feature. Use your own API key or ask an admin to enable premium.")
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "config" / "active_model.yaml").write_text(yaml.safe_dump(merged, sort_keys=False))
    return merged


def qwen_allowed(home: Path) -> bool:
    """True when this user's account may use server-side Qwen compute."""
    try:
        from . import auth

        users = auth.load_accounts()
        if home.name not in users:
            return True
        return auth.is_premium(home.name)
    except Exception:  # noqa: BLE001 - temp workspaces in tests may not have accounts.
        return True


def set_active_provider(home: Path, provider: str) -> dict:
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider {provider!r}. Choose one of {PROVIDERS}.")
    if provider == "qwen" and not qwen_allowed(home):
        raise PermissionError("Qwen is a premium feature. Use your own API key or ask an admin to enable premium.")
    return save_config(home, {"active": provider})


def get_active_config(home: Path) -> ActiveModelConfig:
    cfg = load_config(home)
    active = cfg["active"]
    section = cfg.get(active, {})
    return ActiveModelConfig(
        active=active,
        base_url=section.get("base_url", "http://localhost:11434/v1"),
        model=section.get("model", ""),
        api_key=section.get("api_key", "") or "",
        raw=cfg,
    )


def supports_pdf(home: Path) -> bool:
    """True if the active provider can ingest the actual PDF file as context."""
    return get_active_config(home).active == "claude"


# --------------------------------------------------------------------------- #
# System/message helpers
# --------------------------------------------------------------------------- #
def _split_system(messages: list[dict], system: Optional[str]) -> tuple[str, list[dict]]:
    """Collect any system messages + the explicit ``system`` arg into one string; return the rest."""
    sys_parts = [system] if system else []
    rest: list[dict] = []
    for m in messages:
        if m.get("role") == "system":
            sys_parts.append(str(m.get("content", "")))
        else:
            rest.append(m)
    return "\n\n".join(p for p in sys_parts if p), rest


# --------------------------------------------------------------------------- #
# Deep-read: attach a PDF (or its text) to the last user turn
# --------------------------------------------------------------------------- #
def attach_pdf(home: Path, messages: list[dict], pdf_path: Path,
               fallback_text: str = "", label: str = "") -> list[dict]:
    """Return a copy of ``messages`` with a PDF attached to the final user message.

    For ``claude`` the actual PDF is attached as a base64 document block. For ``qwen`` and
    ``openai`` we append the extracted text (``fallback_text``) — robust across models that
    don't accept PDFs over the chat API.
    """
    msgs = copy.deepcopy(messages)
    # find last user message; if none, append one
    idx = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("role") == "user"), None)
    if idx is None:
        msgs.append({"role": "user", "content": ""})
        idx = len(msgs) - 1

    if get_active_config(home).active == "claude" and pdf_path.exists():
        data = base64.standard_b64encode(pdf_path.read_bytes()).decode("ascii")
        existing = msgs[idx].get("content", "")
        parts: list[dict] = []
        if isinstance(existing, list):
            parts = existing
        elif existing:
            parts = [{"type": "text", "text": existing}]
        parts.insert(0, {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": data},
            "title": label or pdf_path.parent.name,
        })
        msgs[idx]["content"] = parts
    else:
        text = fallback_text.strip()
        if text:
            header = f"\n\n--- Full text of attached PDF ({label or 'document'}) ---\n"
            existing = msgs[idx].get("content", "")
            if isinstance(existing, str):
                msgs[idx]["content"] = existing + header + text
            else:
                existing.append({"type": "text", "text": header + text})
    return msgs


# --------------------------------------------------------------------------- #
# Inference
# --------------------------------------------------------------------------- #
def _openai_client(rc: ActiveModelConfig):
    from openai import OpenAI

    if rc.active == "qwen":
        return OpenAI(base_url=rc.base_url, api_key=rc.api_key or "ollama")
    if rc.active == "gemini":
        return OpenAI(base_url=GEMINI_BASE_URL, api_key=rc.api_key)
    return OpenAI(api_key=rc.api_key)  # openai


def _anthropic_client(rc: ActiveModelConfig):
    from anthropic import Anthropic

    return Anthropic(api_key=rc.api_key)


def _claude_credential(rc: ActiveModelConfig) -> str:
    """The active Claude API key, or ''."""
    return rc.api_key


def stream_chat(home: Path, messages: list[dict], system: Optional[str] = None,
                temperature: float = 0.3, *, claude_token: str = "") -> Iterator[str]:
    """Yield text deltas from the active model."""
    rc = get_active_config(home)
    if rc.active == "qwen" and not qwen_allowed(home):
        raise PermissionError("Qwen is a premium feature. Use your own API key or ask an admin to enable premium.")
    sys_text, rest = _split_system(messages, system)

    if rc.active == "claude":
        client = _anthropic_client(rc)
        kwargs: dict[str, Any] = {
            "model": rc.model,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "messages": rest,
            "temperature": temperature,
        }
        if sys_text:
            kwargs["system"] = sys_text
        with client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                yield text
        return

    # qwen / openai (OpenAI-compatible)
    client = _openai_client(rc)
    oai_messages = ([{"role": "system", "content": sys_text}] if sys_text else []) + rest
    stream = client.chat.completions.create(
        model=rc.model, messages=oai_messages, temperature=temperature, stream=True)
    for chunk in stream:
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            yield delta


def complete(home: Path, messages: list[dict], system: Optional[str] = None,
             temperature: float = 0.2, *, claude_token: str = "") -> str:
    """Non-streaming completion (tagging, summarizing, etc.)."""
    return "".join(stream_chat(home, messages, system=system, temperature=temperature,
                               claude_token=claude_token))


_COMPACT_KEEP = 8  # number of most-recent messages to preserve verbatim


def compact_chat(home: Path, messages: list[dict], *, claude_token: str = "") -> list[dict]:
    """Summarize older messages when a conversation grows long.

    Keeps the last ``_COMPACT_KEEP`` messages verbatim and replaces everything
    before them with a compact summary so the model retains context without
    consuming the entire context window.
    """
    if len(messages) <= _COMPACT_KEEP + 2:
        return messages
    old = messages[:-_COMPACT_KEEP]
    recent = messages[-_COMPACT_KEEP:]
    text = "\n".join(
        f"{m['role'].upper()}: {m.get('content', '') if isinstance(m.get('content'), str) else '[attachment]'}"
        for m in old
    )
    summary = complete(
        home,
        [{"role": "user", "content":
          f"Summarize this conversation compactly. Preserve all key facts, decisions, proposed edits, page names, and any context needed to continue the discussion:\n\n{text}"}],
        system="You summarize conversations. Be concise but complete. Keep technical details, file names, and decisions.",
        claude_token=claude_token,
    )
    return [
        {"role": "user", "content": f"[Summary of earlier conversation]\n{summary}"},
        {"role": "assistant", "content": "Understood — I have the full context from our earlier conversation."},
        *recent,
    ]


def health_check(home: Path, *, claude_token: str = "", live: bool = False) -> dict:
    """Check whether the active provider is ready to use.

    Without ``live`` API-key providers only verify that credentials are configured.
    With ``live`` every provider receives a tiny prompt and must return text.

    For qwen (Ollama) we hit the local /models endpoint, which is free.
    """
    rc = get_active_config(home)
    try:
        if rc.active == "qwen" and not qwen_allowed(home):
            return {"ok": False, "active": rc.active, "model": rc.model,
                    "message": "Qwen is a premium feature. Use OpenAI, Claude, or Gemini with your own API key."}
        if live:
            prompt = "Reply with exactly: OK"
            if rc.active == "claude":
                if not _claude_credential(rc):
                    return {"ok": False, "active": rc.active, "model": rc.model,
                            "message": "No Claude API key configured. Click ⚙ to add one."}
                client = _anthropic_client(rc)
                msg = client.messages.create(
                    model=rc.model,
                    max_tokens=8,
                    temperature=0,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = "".join(
                    block.text for block in msg.content
                    if getattr(block, "type", "") == "text" and getattr(block, "text", "")
                )
            else:
                if rc.active in {"openai", "gemini"} and not rc.api_key:
                    return {"ok": False, "active": rc.active, "model": rc.model,
                            "message": f"No {rc.active.title()} API key configured. Click ⚙ to add one."}
                client = _openai_client(rc)
                msg = client.chat.completions.create(
                    model=rc.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                    max_tokens=32 if rc.active == "gemini" else 8,
                )
                text = msg.choices[0].message.content if msg.choices else ""
            if (text or "").strip():
                return {"ok": True, "active": rc.active, "model": rc.model,
                        "message": f"{rc.active} '{rc.model}' responded"}
            return {"ok": False, "active": rc.active, "model": rc.model,
                    "message": f"{rc.active} '{rc.model}' returned an empty response"}

        if rc.active == "claude":
            if not _claude_credential(rc):
                return {"ok": False, "active": rc.active, "model": rc.model,
                        "message": "No Claude API key configured. Click ⚙ to add one."}
            return {"ok": True, "active": rc.active, "model": rc.model,
                    "message": f"claude '{rc.model}' configured"}

        if rc.active == "openai":
            if not rc.api_key:
                return {"ok": False, "active": rc.active, "model": rc.model,
                        "message": "No OpenAI API key configured. Click ⚙ to add one."}
            return {"ok": True, "active": rc.active, "model": rc.model,
                    "message": f"openai '{rc.model}' configured"}

        if rc.active == "gemini":
            if not rc.api_key:
                return {"ok": False, "active": rc.active, "model": rc.model,
                        "message": "No Gemini API key configured. Click ⚙ to add one."}
            return {"ok": True, "active": rc.active, "model": rc.model,
                    "message": f"gemini '{rc.model}' configured"}

        # qwen — hit the local Ollama /models list (no quota cost)
        client = _openai_client(rc)
        available = {m.id for m in client.models.list().data}
        ok = (not available) or any(
            rc.model == m or rc.model.split(":")[0] == m.split(":")[0] for m in available)
        if ok:
            return {"ok": True, "active": rc.active, "model": rc.model,
                    "message": f"qwen '{rc.model}' reachable at {rc.base_url}"}
        return {"ok": False, "active": rc.active, "model": rc.model,
                "message": f"Ollama up but model '{rc.model}' not found. Run: ollama pull {rc.model}"}
    except Exception as e:  # noqa: BLE001
        hint = " Is Ollama running?" if rc.active == "qwen" else ""
        return {"ok": False, "active": rc.active, "model": rc.model,
                "message": f"cannot reach {rc.active} ({e}).{hint}"}
