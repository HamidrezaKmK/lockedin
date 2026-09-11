"""Vendor adapters for agent CLIs.

This is the only module that should know Codex, Claude, or Agy command-line flags and
on-disk conversation layouts.  The Scientist worker owns lifecycle, confinement, budgets,
and queue semantics; adapters translate that stable contract to a vendor installation.

Keep this module stdlib-only.  Vendor SDKs are deliberately not dependencies: CLI upgrades
should require changing one adapter and its contract tests, not the worker.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable


BinaryResolver = Callable[[str], str]
ManagedWriter = Callable[[Path, str], None]


@lru_cache(maxsize=32)
def _cached_help_text(executable: str, subcommand: str, fingerprint: tuple[int, int]) -> str:
    """Probe one installed binary generation, not merely one path forever."""
    try:
        argv = [executable] + ([subcommand] if subcommand else []) + ["--help"]
        result = subprocess.run(argv, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=10)
        return (result.stdout or "") + "\n" + (result.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return ""


def _help_text(executable: str, subcommand: str = "") -> str:
    """Best-effort feature detection keeps optional hardening flags version-tolerant.

    Vendor installers commonly replace a binary in place. Including its mtime and size in the
    cache key means a long-running Scientist notices that upgrade on its next turn.
    """
    try:
        stat = Path(executable).stat()
        fingerprint = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        fingerprint = (0, 0)
    return _cached_help_text(executable, subcommand, fingerprint)


def _supports(executable: str, option: str, *, subcommand: str = "") -> bool:
    return option in _help_text(executable, subcommand)


def _worktree_paths(project: Path) -> set[str]:
    found = {str(project.resolve())}
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=project, capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            main = Path(out.stdout.strip()).parent
            listed = subprocess.run(
                ["git", "worktree", "list", "--porcelain"], cwd=main,
                capture_output=True, text=True, timeout=10,
            )
            for line in listed.stdout.splitlines():
                if line.startswith("worktree "):
                    found.add(str(Path(line[9:].strip()).resolve()))
    except (OSError, subprocess.SubprocessError):
        pass
    return found


@dataclass(frozen=True)
class VendorAdapter:
    """Stable boundary used by the vendor-neutral worker."""

    name: str
    invocation: str
    state_roots: tuple[str, ...]
    busy_signatures: tuple[str, ...] = ()
    lost_signatures: tuple[str, ...] = ()
    preassigns_conversation_id: bool = False
    network_signatures: tuple[str, ...] = ()

    def home(self) -> Path:
        raise NotImplementedError

    def skill_paths(self, home: Path, app: str) -> tuple[Path, ...]:
        raise NotImplementedError

    def install_skill(self, home: Path, app: str, content: str, *, writer: ManagedWriter,
                      binary: BinaryResolver, run: Callable[..., subprocess.CompletedProcess]) -> tuple[Path, ...]:
        targets = self.skill_paths(home, app)
        writer(targets[0], content)
        return targets

    def setup_hint(self, app: str) -> str:
        raise NotImplementedError

    def turn_command(self, agent: dict, prompt: str, *, new_id: str, permissive: bool,
                     turn_minutes: int, fork_conversation: bool,
                     binary: BinaryResolver) -> list[str]:
        raise NotImplementedError

    def turn_environment(self) -> dict[str, str]:
        """Vendor-only environment additions for a headless turn."""
        return {}

    def failure_detail(self, output: str) -> str:
        """Extract a provider error from structured print-mode output, when unambiguous."""
        for line in reversed((output or "").splitlines()):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            is_error = (item.get("is_error") is True
                        or str(item.get("status") or "").upper() in {"ERROR", "FAILED"})
            if not is_error:
                continue
            for key in ("result", "error", "message"):
                detail = item.get(key)
                if isinstance(detail, str) and detail.strip():
                    return detail.strip()[:600]
        return ""

    def chat_command(self, agent: dict, *, new_id: str, binary: BinaryResolver) -> list[str]:
        raise NotImplementedError

    def conversations_for(self, project: Path) -> list[tuple[str, float]]:
        return []

    def live_conversations(self) -> set[str]:
        return set()

    def conversation_exists(self, conversation: str) -> bool:
        return True

    def discover_conversation(self, output: str, *, started: float, project: Path) -> str:
        match = re.search(
            r'"(?:conversation_id|conversationId|session_id|thread_id)"\s*:\s*"([^"]+)"',
            output,
        )
        return match.group(1) if match else ""

    def purge_conversation(self, conversation: str, *, binary: BinaryResolver) -> list[str]:
        return []


class AgyAdapter(VendorAdapter):
    def home(self) -> Path:
        return Path(os.environ.get("ANTIGRAVITY_CLI_HOME") or Path.home() / ".gemini" / "antigravity-cli")

    def skill_paths(self, home: Path, app: str) -> tuple[Path, ...]:
        plugin = home / ".gemini" / "antigravity-cli" / "plugins" / app
        return plugin / "plugin.json", plugin / "skills" / app / "SKILL.md"

    def install_skill(self, home: Path, app: str, content: str, *, writer: ManagedWriter,
                      binary: BinaryResolver, run: Callable[..., subprocess.CompletedProcess]) -> tuple[Path, ...]:
        plugin_json, skill_path = self.skill_paths(home, app)
        if plugin_json.exists():
            try: plugin = json.loads(plugin_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Could not inspect existing agy plugin at {plugin_json}: {exc}") from exc
            if plugin.get("managed_by") != app:
                raise RuntimeError(f"Refusing to overwrite the existing agy plugin at {plugin_json.parent}. "
                                   "Move or remove that user-owned plugin, then run setup again.")
        plugin_json.parent.mkdir(parents=True, exist_ok=True)
        plugin_json.write_text(json.dumps({
            "name": app, "version": "1.0.0",
            "description": "Project-local LockedIn Scientist bootstrap skill.", "managed_by": app,
        }, indent=2) + "\n", encoding="utf-8")
        writer(skill_path, content)
        executable = binary(self.name)
        try:
            installed = run([executable, "plugin", "install", str(plugin_json.parent)],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        except OSError as exc:
            raise RuntimeError(f"Could not ask agy to import its native skill: {exc}") from exc
        if installed.returncode:
            detail = (installed.stderr or installed.stdout).strip()
            raise RuntimeError(f"agy could not import the native {app} skill" + (f": {detail}" if detail else "."))
        return plugin_json, skill_path

    def setup_hint(self, app: str) -> str:
        return f"Restart agy if it is open, then use /skills to select {app} in a synchronized project."

    def turn_command(self, agent: dict, prompt: str, *, new_id: str, permissive: bool,
                     turn_minutes: int, fork_conversation: bool,
                     binary: BinaryResolver) -> list[str]:
        cmd = [binary(self.name), "--output-format", "json", "--disable-slash-commands"]
        cmd += ["--dangerously-skip-permissions"] if permissive else ["--mode", "accept-edits"]
        cmd += ["--print-timeout", f"{turn_minutes}m0s"]
        if agent.get("conversation"): cmd += ["--conversation", str(agent["conversation"])]
        if agent.get("model"): cmd += ["--model", str(agent["model"])]
        return cmd + ["-p", prompt]

    def chat_command(self, agent: dict, *, new_id: str, binary: BinaryResolver) -> list[str]:
        cmd = [binary(self.name)]
        if agent.get("conversation"): cmd += ["--conversation", str(agent["conversation"])]
        if agent.get("model"): cmd += ["--model", str(agent["model"])]
        return cmd

    def live_conversations(self) -> set[str]:
        presence = self.home() / "presence"
        if not presence.is_dir(): return set()
        try:
            import fcntl
        except ImportError:  # pragma: no cover - not Linux/macOS
            return set()
        live: set[str] = set()
        for path in presence.glob("*.lock"):
            try: fd = os.open(path, os.O_RDONLY)
            except OSError: continue
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    live.add(path.stem)
            finally: os.close(fd)
        return live

    @staticmethod
    def _time(value: object) -> float:
        text = str(value or "").strip()
        if not text: return 0.0
        try:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00").split(" m=")[0][:32].strip())
            if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=timezone.utc)
            return stamp.timestamp()
        except ValueError: return 0.0

    def conversations_for(self, project: Path) -> list[tuple[str, float]]:
        wanted, scores = _worktree_paths(project), {}
        db = self.home() / "conversation_summaries.db"
        if db.exists():
            try:
                import sqlite3
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
                try:
                    rows = con.execute("select conversation_id, workspace_uris, last_modified_time "
                                       "from conversation_summaries").fetchall()
                finally: con.close()
                for cid, uris, modified in rows:
                    if any(path in str(uris or "") for path in wanted):
                        scores[str(cid)] = max(scores.get(str(cid), 0.0), self._time(modified))
            except Exception: pass
        history = self.home() / "history.jsonl"
        if history.exists():
            try:
                for line in history.read_text(encoding="utf-8", errors="replace").splitlines()[-3000:]:
                    try: row = json.loads(line)
                    except json.JSONDecodeError: continue
                    cid = str(row.get("conversationId") or "")
                    if cid and str(row.get("workspace", "")) in wanted:
                        stamp = float(row.get("timestamp", 0) or 0) / 1000.0
                        scores[cid] = max(scores.get(cid, 0.0), stamp)
            except OSError: pass
        return sorted(scores.items(), key=lambda item: item[1], reverse=True)

    def conversation_exists(self, conversation: str) -> bool:
        return (self.home() / "conversations" / f"{conversation}.db").exists()

    def discover_conversation(self, output: str, *, started: float, project: Path) -> str:
        found = super().discover_conversation(output, started=started, project=project)
        if found: return found
        here = [cid for cid, stamp in self.conversations_for(project) if stamp >= started - 2]
        if here: return here[0]
        folder = self.home() / "conversations"
        if folder.is_dir():
            try: fresh = [p for p in folder.glob("*.db") if p.stat().st_mtime >= started - 2]
            except OSError: return ""
            if len(fresh) == 1: return fresh[0].stem
        return ""

    def purge_conversation(self, conversation: str, *, binary: BinaryResolver) -> list[str]:
        removed = []
        for path in (self.home() / "conversations").glob(f"{conversation}.db*"):
            try: path.unlink(); removed.append(str(path))
            except OSError: pass
        return removed


class ClaudeAdapter(VendorAdapter):
    def home(self) -> Path:
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")

    def skill_paths(self, home: Path, app: str) -> tuple[Path, ...]:
        return (home / ".claude" / "skills" / app / "SKILL.md",)

    def setup_hint(self, app: str) -> str:
        return f"Restart Claude Code if it is open, then invoke /{app} in a synchronized project."

    def turn_command(self, agent: dict, prompt: str, *, new_id: str, permissive: bool,
                     turn_minutes: int, fork_conversation: bool,
                     binary: BinaryResolver) -> list[str]:
        executable = binary(self.name)
        cmd = [executable, "-p", "--output-format", "json", "--permission-mode",
               "bypassPermissions" if permissive else "acceptEdits"]
        if _supports(executable, "--permission-prompts"):
            cmd += ["--permission-prompts", "none"]
        cmd += ["--resume", str(agent["conversation"])] if agent.get("conversation") else ["--session-id", new_id]
        if agent.get("model"): cmd += ["--model", str(agent["model"])]
        return cmd + [prompt]

    def chat_command(self, agent: dict, *, new_id: str, binary: BinaryResolver) -> list[str]:
        cmd = [binary(self.name)]
        cmd += ["--resume", str(agent["conversation"])] if agent.get("conversation") else ["--session-id", new_id]
        if agent.get("model"): cmd += ["--model", str(agent["model"])]
        return cmd

    def conversation_exists(self, conversation: str) -> bool:
        return any((self.home() / "projects").glob(f"*/{conversation}.jsonl"))

    def purge_conversation(self, conversation: str, *, binary: BinaryResolver) -> list[str]:
        removed = []
        for path in (self.home() / "projects").glob(f"*/{conversation}.jsonl"):
            try: path.unlink(); removed.append(str(path))
            except OSError: pass
        folder = next(iter((self.home() / "projects").glob(f"*/{conversation}")), None)
        if folder and folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True); removed.append(str(folder))
        return removed


class CodexAdapter(VendorAdapter):
    def home(self) -> Path:
        return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")

    def skill_paths(self, home: Path, app: str) -> tuple[Path, ...]:
        return (home / ".codex" / "skills" / app / "SKILL.md",)

    def setup_hint(self, app: str) -> str:
        return f"Start Codex in a synchronized project, then invoke ${app}."

    def turn_environment(self) -> dict[str, str]:
        """Make macOS keychain roots available to Codex's Rust TLS clients.

        Interactive macOS programs can consult the system trust service, while a Seatbelt child
        may only see the smaller file-based root set and report ``UnknownIssuer``. Current Codex
        supports a PEM bundle through ``CODEX_CA_CERTIFICATE``. Respect an operator's explicit
        CA choice; otherwise export the current user's keychain search list to one managed file.
        This changes only the background Codex child's environment, never shell profiles or the
        machine trust store.
        """
        if sys.platform != "darwin" or os.environ.get("CODEX_CA_CERTIFICATE") or os.environ.get("SSL_CERT_FILE"):
            return {}
        security = Path("/usr/bin/security")
        if not security.exists():
            return {}
        target = self.home() / "lockedin-macos-ca.pem"
        try:
            keychains = [Path("/Library/Keychains/System.keychain"),
                         Path("/System/Library/Keychains/SystemRootCertificates.keychain")]
            login = Path.home() / "Library" / "Keychains" / "login.keychain-db"
            if login.exists():
                keychains.append(login)
            result = subprocess.run([str(security), "find-certificate", "-a", "-p",
                                     *(str(path) for path in keychains)],
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
            pem = result.stdout or b""
            if result.returncode or b"-----BEGIN CERTIFICATE-----" not in pem:
                return {}
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_bytes(pem)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        except (OSError, subprocess.SubprocessError):
            return {}
        return {"CODEX_CA_CERTIFICATE": str(target)}

    def turn_command(self, agent: dict, prompt: str, *, new_id: str, permissive: bool,
                     turn_minutes: int, fork_conversation: bool,
                     binary: BinaryResolver) -> list[str]:
        executable = binary(self.name)
        cmd = [executable, "exec"]
        cmd += ["--dangerously-bypass-approvals-and-sandbox"] if permissive else [
            "-s", "workspace-write", "-c", "sandbox_workspace_write.network_access=true"]
        if permissive and _supports(executable, "--dangerously-bypass-hook-trust", subcommand="exec"):
            cmd += ["--dangerously-bypass-hook-trust"]
        cmd += ["--skip-git-repo-check", "--json"]
        if agent.get("model"): cmd += ["-m", str(agent["model"])]
        if agent.get("conversation"):
            cmd += ["fork" if fork_conversation else "resume", str(agent["conversation"])]
        return cmd + [prompt]

    def chat_command(self, agent: dict, *, new_id: str, binary: BinaryResolver) -> list[str]:
        cmd = [binary(self.name)]
        if agent.get("model"): cmd += ["-m", str(agent["model"])]
        if agent.get("conversation"): cmd += ["resume", str(agent["conversation"])]
        return cmd

    def conversations_for(self, project: Path) -> list[tuple[str, float]]:
        wanted, sessions = _worktree_paths(project), self.home() / "sessions"
        if not sessions.is_dir(): return []
        try: files = sorted(sessions.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:400]
        except OSError: return []
        found = []
        for path in files:
            try:
                with path.open(encoding="utf-8", errors="replace") as fh: meta = json.loads(fh.readline())
                payload = meta.get("payload") or {}
                cwd, sid = str(payload.get("cwd") or ""), str(payload.get("id") or payload.get("session_id") or "")
                if sid and cwd and str(Path(cwd).resolve()) in wanted:
                    found.append((sid, path.stat().st_mtime))
            except (OSError, json.JSONDecodeError): continue
        return found

    def conversation_exists(self, conversation: str) -> bool:
        # Modern Codex records resumable threads in a versioned SQLite store. Prefer an
        # exact ID lookup there: rollout filenames include a timestamp before the ID and
        # are not themselves the resume index.
        try:
            databases = sorted(
                self.home().glob("state*.sqlite"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            databases = []
        for database in databases:
            connection = None
            try:
                connection = sqlite3.connect(
                    f"file:{database}?mode=ro", uri=True, timeout=2,
                )
                if connection.execute(
                    "SELECT 1 FROM threads WHERE id = ? LIMIT 1", (conversation,),
                ).fetchone():
                    return True
            except (OSError, sqlite3.Error):
                # A locked, partially upgraded, or older store should not make the worker
                # discard a conversation that the filesystem can still prove exists.
                continue
            finally:
                if connection is not None:
                    connection.close()

        sessions = self.home() / "sessions"
        if sessions.is_dir():
            try:
                if any(sessions.rglob(f"*-{conversation}.jsonl")): return True
                # Keep compatibility with old Codex releases that used the bare ID first.
                if any(sessions.rglob(f"{conversation}*.jsonl")): return True
            except OSError: return True
        index = self.home() / "session_index.jsonl"
        if index.is_file():
            try: return conversation in index.read_text(encoding="utf-8", errors="replace")
            except OSError: return True
        return False

    def discover_conversation(self, output: str, *, started: float, project: Path) -> str:
        found = super().discover_conversation(output, started=started, project=project)
        if found: return found
        here = [sid for sid, stamp in self.conversations_for(project) if stamp >= started - 2]
        return here[0] if here else ""

    def purge_conversation(self, conversation: str, *, binary: BinaryResolver) -> list[str]:
        try:
            out = subprocess.run([binary(self.name), "delete", conversation], capture_output=True, text=True, timeout=60)
            return [f"codex session {conversation}"] if out.returncode == 0 else []
        except (RuntimeError, OSError, subprocess.SubprocessError): return []


COMMON_BUSY_SIGNATURES = (
    "already has an active writer", "thread-store conflict", "session is already in use",
    "another instance is running", "resource temporarily unavailable",
)
COMMON_LOST_SIGNATURES = (
    "conversation not found", "no such conversation", "unknown conversation", "session not found",
    "no such session", "thread not found", "could not find thread", "no conversation with id",
)
COMMON_NETWORK_SIGNATURES = ("reconnecting... waiting for network",)

_ADAPTERS: dict[str, VendorAdapter] = {
    "codex": CodexAdapter(name="codex", invocation="start codex, then invoke $lockedin-scientist",
                          state_roots=(".codex",), busy_signatures=COMMON_BUSY_SIGNATURES,
                          lost_signatures=COMMON_LOST_SIGNATURES, network_signatures=COMMON_NETWORK_SIGNATURES),
    "claude": ClaudeAdapter(name="claude", invocation="start claude, then invoke /lockedin-scientist",
                             state_roots=(".claude", ".claude.json"), busy_signatures=COMMON_BUSY_SIGNATURES,
                             lost_signatures=COMMON_LOST_SIGNATURES, preassigns_conversation_id=True,
                             network_signatures=COMMON_NETWORK_SIGNATURES),
    "agy": AgyAdapter(name="agy", invocation="start agy, then use /skills to select lockedin-scientist",
                       state_roots=(".gemini",), busy_signatures=COMMON_BUSY_SIGNATURES,
                       lost_signatures=COMMON_LOST_SIGNATURES, network_signatures=COMMON_NETWORK_SIGNATURES),
}


def names() -> tuple[str, ...]: return tuple(_ADAPTERS)


def get(name: str) -> VendorAdapter:
    try: return _ADAPTERS[name]
    except KeyError: raise RuntimeError(f"unknown vendor {name!r}") from None


def writable_state_paths(home: Path) -> tuple[Path, ...]:
    return tuple(home / rel for adapter in _ADAPTERS.values() for rel in adapter.state_roots)


def detect_conversation(project: Path, *, vendor: str = "", conversation: str = "",
                        env: dict[str, str] | None = None) -> tuple[str, str]:
    vendor = vendor.strip().lower()
    if vendor and vendor not in _ADAPTERS:
        raise RuntimeError(f"--vendor must be one of {', '.join(names())}")
    if conversation:
        if not vendor: raise RuntimeError("--conversation needs --vendor <codex|claude|agy> as well.")
        return vendor, conversation.strip()
    env = os.environ if env is None else env
    candidates: list[tuple[str, str]] = []
    if env.get("CLAUDE_CODE_SESSION_ID"): candidates.append(("claude", env["CLAUDE_CODE_SESSION_ID"]))
    for key, value in env.items():
        upper = key.upper()
        if not value or not any(word in upper for word in ("CONVERSATION", "THREAD", "SESSION")): continue
        if upper.startswith(("ANTIGRAVITY", "AGY")): candidates.append(("agy", value))
        elif upper.startswith("CODEX") and "ID" in upper: candidates.append(("codex", value))
    if vendor: candidates = [item for item in candidates if item[0] == vendor]
    if candidates: return candidates[0]
    if vendor in ("", "agy"):
        adapter = get("agy")
        here = [cid for cid, _ in adapter.conversations_for(project) if cid in adapter.live_conversations()]
        if len(here) == 1: return "agy", here[0]
        if len(here) > 1:
            raise RuntimeError("Several agy conversations are open in this project (" + ", ".join(here[:4])
                               + "). Pass --vendor agy --conversation <id>.")
    if vendor in ("", "codex"):
        here = get("codex").conversations_for(project)
        if here: return "codex", here[0][0]
    raise RuntimeError("Could not tell which conversation this is. From inside your agent's chat, run it "
                       "again; or pass --vendor <codex|claude|agy> --conversation <id> explicitly.")
