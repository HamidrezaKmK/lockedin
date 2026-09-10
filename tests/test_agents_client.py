"""Regression tests for the stdlib-only Scientist client's "agents" feature.

Deterministic: no network, no model, no real vendor CLI. Fake vendor "binaries" are short Python
`-c` scripts run through `sys.executable`; the real ``ProjectSync._request`` is replaced with a
recording fake so nothing ever leaves the process.

Run: ``LOCKEDIN_HOME=/tmp/li_agents_t2 uv run python -m unittest tests.test_agents_client -v``
"""
from __future__ import annotations

import base64
import fcntl
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import types

from lockedin import agent_vendors, scientist_cli


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@contextmanager
def _held_agy_lock(path: Path):
    """agy marks a conversation "live" by an *unreleased* advisory flock on its presence file —
    the file's mere existence means nothing (see `_agy_live_conversations`'s docstring in
    scientist_cli.py). Simulate a live conversation by actually holding that lock for the
    duration of the `with` block, the same way a real agy process running in a terminal would.
    """
    path.write_text("")
    fd = os.open(path, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# 1. data_root()
# ---------------------------------------------------------------------------


class DataRootTests(unittest.TestCase):
    def test_data_root_honours_the_scientist_home_override(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"LOCKEDIN_SCIENTIST_HOME": directory}):
                self.assertEqual(scientist_cli.data_root(), Path(directory))


# ---------------------------------------------------------------------------
# 2. detect_conversation
# ---------------------------------------------------------------------------


def _bare_env(**extra) -> dict:
    """A minimal environment: HOME/PATH kept so stdlib calls (Path.home(), subprocess) still work."""
    base = {"HOME": os.environ.get("HOME", "/root"), "PATH": os.environ.get("PATH", "/usr/bin")}
    base.update(extra)
    return base


class DetectConversationTests(unittest.TestCase):
    def test_claude_code_session_id_env_wins_immediately(self):
        with tempfile.TemporaryDirectory() as project, patch.dict(
                os.environ, _bare_env(CLAUDE_CODE_SESSION_ID="sess-1"), clear=True):
            self.assertEqual(scientist_cli.detect_conversation(Path(project)), ("claude", "sess-1"))

    def test_a_single_live_agy_conversation_for_this_project_is_found(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as agy_home:
            project_path = Path(project).resolve()
            (Path(agy_home) / "presence").mkdir(parents=True)
            history = {"workspace": str(project_path), "conversationId": "conv-1", "timestamp": 1700000000000}
            (Path(agy_home) / "history.jsonl").write_text(json.dumps(history) + "\n")
            with patch.dict(os.environ, _bare_env(ANTIGRAVITY_CLI_HOME=agy_home), clear=True), \
                    _held_agy_lock(Path(agy_home) / "presence" / "conv-1.lock"):
                self.assertEqual(scientist_cli.detect_conversation(project_path), ("agy", "conv-1"))

    def test_two_live_agy_conversations_raise_asking_for_explicit_flags(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as agy_home:
            project_path = Path(project).resolve()
            (Path(agy_home) / "presence").mkdir(parents=True)
            lines = []
            for cid in ("conv-a", "conv-b"):
                lines.append(json.dumps({"workspace": str(project_path), "conversationId": cid,
                                         "timestamp": 1700000000000}))
            (Path(agy_home) / "history.jsonl").write_text("\n".join(lines) + "\n")
            with patch.dict(os.environ, _bare_env(ANTIGRAVITY_CLI_HOME=agy_home), clear=True), \
                    _held_agy_lock(Path(agy_home) / "presence" / "conv-a.lock"), \
                    _held_agy_lock(Path(agy_home) / "presence" / "conv-b.lock"):
                with self.assertRaisesRegex(RuntimeError, "--conversation"):
                    scientist_cli.detect_conversation(project_path)

    def test_explicit_vendor_and_conversation_pass_through(self):
        with tempfile.TemporaryDirectory() as project, patch.dict(os.environ, _bare_env(), clear=True):
            self.assertEqual(
                scientist_cli.detect_conversation(Path(project), vendor="agy", conversation="X"),
                ("agy", "X"))

    def test_conversation_without_vendor_raises(self):
        with tempfile.TemporaryDirectory() as project, patch.dict(os.environ, _bare_env(), clear=True):
            with self.assertRaises(RuntimeError):
                scientist_cli.detect_conversation(Path(project), conversation="X")

    def test_a_codex_session_recorded_for_this_project_cwd_is_found(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as codex_home:
            project_path = Path(project).resolve()
            sessions = Path(codex_home) / "sessions" / "2026" / "09" / "06"
            sessions.mkdir(parents=True)
            (sessions / "rollout-x.jsonl").write_text(
                json.dumps({"payload": {"id": "sid-1", "cwd": str(project_path)}}) + "\n")
            with patch.dict(os.environ, _bare_env(CODEX_HOME=codex_home), clear=True):
                self.assertEqual(scientist_cli.detect_conversation(project_path), ("codex", "sid-1"))

    def test_nothing_found_raises(self):
        with tempfile.TemporaryDirectory() as project, patch.dict(os.environ, _bare_env(), clear=True):
            with self.assertRaises(RuntimeError):
                scientist_cli.detect_conversation(Path(project))


# ---------------------------------------------------------------------------
# 2b. _alive — must never TerminateProcess the thing it's only supposed to probe on Windows
# ---------------------------------------------------------------------------


class AliveTests(unittest.TestCase):
    def test_alive_true_for_the_current_process(self):
        self.assertTrue(scientist_cli._alive(os.getpid()))

    def test_alive_false_for_a_pid_that_cannot_exist(self):
        self.assertFalse(scientist_cli._alive(2 ** 22 + 7))

    def test_the_posix_path_is_gated_behind_an_os_name_check_not_unconditional(self):
        # Pin the contract described in scientist_cli._alive's comment: os.kill(pid, 0) on
        # Windows maps to TerminateProcess, not a probe, so the function must never reach that
        # call path unconditionally. Source-inspect for the guard rather than the exact wording.
        import inspect
        source = inspect.getsource(scientist_cli._alive)
        self.assertIn('os.name == "nt"', source)
        self.assertIn("TerminateProcess", source)
        self.assertIn("os.kill", source)  # the POSIX fallback still exists

    def test_the_windows_branch_never_calls_os_kill(self):
        import ctypes

        def guard(pid, sig):
            raise AssertionError("os.kill must never be called on the Windows path")

        fake_kernel32 = types.SimpleNamespace(OpenProcess=lambda *a: 0)  # null handle: "gone"
        fake_windll = types.SimpleNamespace(kernel32=fake_kernel32)
        with patch.object(os, "name", "nt"), patch.object(os, "kill", guard), \
                patch.object(ctypes, "windll", fake_windll, create=True):
            self.assertFalse(scientist_cli._alive(4242))

    def test_the_windows_branch_reports_alive_when_still_active(self):
        import ctypes
        from ctypes import wintypes

        STILL_ACTIVE = 259
        WAIT_TIMEOUT = 0x102

        def guard(pid, sig):
            raise AssertionError("os.kill must never be called on the Windows path")

        def get_exit_code_process(handle, ptr):
            ptr._obj.value = STILL_ACTIVE
            return 1

        fake_kernel32 = types.SimpleNamespace(
            OpenProcess=lambda *a: 999,
            GetExitCodeProcess=get_exit_code_process,
            WaitForSingleObject=lambda handle, timeout: WAIT_TIMEOUT,
            CloseHandle=lambda handle: 1,
        )
        fake_windll = types.SimpleNamespace(kernel32=fake_kernel32)
        with patch.object(os, "name", "nt"), patch.object(os, "kill", guard), \
                patch.object(ctypes, "windll", fake_windll, create=True):
            self.assertTrue(scientist_cli._alive(4242))

    def test_the_windows_branch_is_conservative_on_any_ctypes_failure(self):
        import ctypes

        def guard(pid, sig):
            raise AssertionError("os.kill must never be called on the Windows path")

        class ExplodingWindll:
            @property
            def kernel32(self):
                raise OSError("no such attribute on this platform")

        with patch.object(os, "name", "nt"), patch.object(os, "kill", guard), \
                patch.object(ctypes, "windll", ExplodingWindll(), create=True):
            self.assertTrue(scientist_cli._alive(4242))


# ---------------------------------------------------------------------------
# 3. agent_attached
# ---------------------------------------------------------------------------


class AgentAttachedTests(unittest.TestCase):
    def test_agy_agent_attached_iff_presence_lock_exists(self):
        with tempfile.TemporaryDirectory() as agy_home, tempfile.TemporaryDirectory() as root:
            agent = {"id": "a1", "vendor": "agy", "conversation": "c1"}
            with patch.dict(os.environ, {"ANTIGRAVITY_CLI_HOME": agy_home}):
                self.assertFalse(scientist_cli.agent_attached(agent, Path(root)))
                (Path(agy_home) / "presence").mkdir(parents=True)
                lock_path = Path(agy_home) / "presence" / "c1.lock"
                with _held_agy_lock(lock_path):
                    self.assertTrue(scientist_cli.agent_attached(agent, Path(root)))
                # The lock file's mere existence, once released, means nothing.
                self.assertFalse(scientist_cli.agent_attached(agent, Path(root)))

    def test_claude_agent_attached_when_chat_pid_is_this_process(self):
        with tempfile.TemporaryDirectory() as root:
            agent = {"id": "a1", "vendor": "claude", "conversation": "conversation-long-enough"}
            _write_json(Path(root) / "config" / "agent-chats.json", {"a1": os.getpid()})
            self.assertTrue(scientist_cli.agent_attached(agent, Path(root)))

    def test_claude_agent_not_attached_for_a_dead_pid(self):
        with tempfile.TemporaryDirectory() as root:
            dead_pid = 2 ** 22 + 7
            agent = {"id": "a1", "vendor": "claude", "conversation": "conversation-long-enough"}
            _write_json(Path(root) / "config" / "agent-chats.json", {"a1": dead_pid})
            self.assertFalse(scientist_cli.agent_attached(agent, Path(root)))

    def test_short_conversation_skips_the_proc_scan(self):
        with tempfile.TemporaryDirectory() as root:
            dead_pid = 2 ** 22 + 7
            agent = {"id": "a1", "vendor": "claude", "conversation": "short"}
            _write_json(Path(root) / "config" / "agent-chats.json", {"a1": dead_pid})
            # Short conversation id: even though nothing matches, this must not scan /proc and
            # must simply come back false quickly.
            self.assertFalse(scientist_cli.agent_attached(agent, Path(root)))

    def test_agent_attached_recorded_pid_is_suppressed_by_ignore(self):
        with tempfile.TemporaryDirectory() as root:
            agent = {"id": "a1", "vendor": "claude", "conversation": "conversation-long-enough"}
            _write_json(Path(root) / "config" / "agent-chats.json", {"a1": os.getpid()})
            self.assertTrue(scientist_cli.agent_attached(agent, Path(root)))
            self.assertFalse(scientist_cli.agent_attached(agent, Path(root), ignore={os.getpid()}))

    def test_chat_pid_for_returns_zero_when_no_ancestor_matches(self):
        self.assertEqual(scientist_cli._chat_pid_for("definitely-not-a-real-vendor"), 0)

    def test_chat_pid_for_finds_a_matching_ancestor(self):
        # Spawn a short-lived helper process and pretend it is our parent, so the walk is
        # deterministic regardless of what the real test runner's ancestry looks like.
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            with patch.object(os, "getppid", return_value=proc.pid):
                found = scientist_cli._chat_pid_for(Path(sys.executable).name)
            self.assertEqual(found, proc.pid)
            self.assertTrue(scientist_cli._alive(found))
        finally:
            proc.terminate()
            proc.wait()

    def test_codex_app_server_is_not_a_conversation_attachment(self):
        """The desktop host survives `/exit`; neither registration nor polling may trust it."""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as root:
            codex = Path(tmp) / "codex"
            codex.symlink_to(sys.executable)
            proc = subprocess.Popen([str(codex), "-c", "import time; time.sleep(5)", "app-server"])
            try:
                with patch.object(os, "getppid", return_value=proc.pid):
                    self.assertEqual(scientist_cli._chat_pid_for("codex"), 0)
                agent = {"id": "a1", "vendor": "codex", "conversation": "conversation-long-enough"}
                _write_json(Path(root) / "config" / "agent-chats.json", {"a1": proc.pid})
                self.assertFalse(scientist_cli.agent_attached(agent, Path(root)))
            finally:
                proc.terminate()
                proc.wait()


# ---------------------------------------------------------------------------
# 4. agent_turn_prompt
# ---------------------------------------------------------------------------


PAGE_JOB = {
    "id": "j-000011",
    "instruction": "show the bound",
    "agent": {"name": "Ada", "role": "reviewer", "goal": "be right", "personality": "terse"},
    "mark": {
        "surface": "page", "id": "Qx7", "page": "overview", "page_title": "Overview",
        "glyph": "?", "means": "I don't follow", "quote": "the variance vanishes",
        "messages": [{"by": "hamid", "said": "why?"}],
        "source_path": "reports/pages/overview.md",
        "detail_path": "feedback/pages/overview.json",
    },
}

TALK_JOB = {
    "id": "j-000022",
    "instruction": "",
    "agent": {"name": "Ada", "role": "reviewer", "goal": "be right", "personality": "terse"},
    "mark": {
        "surface": "chalk_talk", "id": "M9", "talk_id": "talk-abc123def456", "talk_title": "T",
        "slide": 1, "slide_title": "S", "anchor_type": "drawing", "touches": ["a", "b"],
        "shot_path": "feedback/shots/x.png",
        "detail_path": "reports/talks/talk-abc123def456/marks.json",
        "source_path": "reports/talks/talk-abc123def456/slides.md",
        "messages": [],
    },
}

DIRECT_JOB = {
    "id": "j-000012", "kind": "direct", "created_by": "hamid",
    "instruction": "Summarize where we landed.\nUse two sentences.",
    "agent": PAGE_JOB["agent"], "mark": {"surface": "direct"},
}


class AgentTurnPromptTests(unittest.TestCase):
    def test_direct_message_prompt_is_a_turn_without_a_mark_edit(self):
        prompt = scientist_cli.agent_turn_prompt(DIRECT_JOB, cli="lockedin-scientist-dev", fresh=False)
        self.assertIn("Direct message from hamid", prompt)
        self.assertIn("Summarize where we landed.\nUse two sentences.", prompt)
        self.assertIn("agent reply j-000012", prompt)
        self.assertNotIn("Record:", prompt)
        self.assertNotIn("Edit:", prompt)

    def test_page_job_prompt_for_a_resumed_conversation(self):
        prompt = scientist_cli.agent_turn_prompt(PAGE_JOB, cli="lockedin-scientist-dev", fresh=False)
        self.assertIn("LockedIn job j-000011", prompt)
        self.assertIn("the variance vanishes", prompt)
        self.assertIn('hamid: "why?"', prompt)
        self.assertIn(".by_id[$id]", prompt)
        self.assertIn("feedback/pages/overview.json", prompt)
        self.assertIn("<comment-begin=Qx7>", prompt)
        self.assertIn("lockedin-scientist-dev agent reply j-000011", prompt)
        self.assertNotIn("You are Ada", prompt)

    def test_page_job_prompt_for_a_fresh_conversation(self):
        prompt = scientist_cli.agent_turn_prompt(PAGE_JOB, cli="lockedin-scientist-dev", fresh=True)
        self.assertTrue(prompt.startswith("You are Ada, reviewer."))
        self.assertIn("guides/agents.md", prompt)

    def test_talk_job_prompt_names_the_one_indexed_slide_and_the_picture(self):
        prompt = scientist_cli.agent_turn_prompt(TALK_JOB, cli="lockedin-scientist-dev", fresh=False)
        self.assertIn("slide 2", prompt)
        self.assertIn("Picture: .lockedin/feedback/shots/x.png", prompt)
        self.assertIn("never marks.json", prompt)


# ---------------------------------------------------------------------------
# 5. agent_turn_command / agent_chat_command
# ---------------------------------------------------------------------------


class AgentTurnCommandTests(unittest.TestCase):
    def test_agy_turn_command(self):
        # mode="none" pins this to the conservative flags this test is actually about; see
        # AgentTurnCommandConfinementModeTests for how the flags change with confinement.
        with patch.object(scientist_cli.shutil, "which", return_value="/bin/agy"):
            agent = {"vendor": "agy", "conversation": "c1", "model": "m1"}
            cmd = scientist_cli.agent_turn_command(agent, "PROMPT", mode="none")
        self.assertEqual(cmd[0], "/bin/agy")
        self.assertIn("--conversation", cmd)
        self.assertEqual(cmd[cmd.index("--conversation") + 1], "c1")
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "m1")
        self.assertIn("--mode", cmd)
        self.assertEqual(cmd[cmd.index("--mode") + 1], "accept-edits")
        self.assertEqual(cmd[-2:], ["-p", "PROMPT"])

    def test_claude_turn_command_with_conversation_resumes(self):
        with patch.object(scientist_cli.shutil, "which", return_value="/bin/claude"):
            agent = {"vendor": "claude", "conversation": "c1", "model": ""}
            cmd = scientist_cli.agent_turn_command(agent, "PROMPT")
        self.assertIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "c1")

    def test_claude_turn_command_without_conversation_uses_new_session_id(self):
        with patch.object(scientist_cli.shutil, "which", return_value="/bin/claude"):
            agent = {"vendor": "claude", "conversation": "", "model": ""}
            cmd = scientist_cli.agent_turn_command(agent, "PROMPT", new_id="new-id-1")
        self.assertIn("--session-id", cmd)
        self.assertEqual(cmd[cmd.index("--session-id") + 1], "new-id-1")

    def test_codex_turn_command_with_conversation_resumes(self):
        with patch.object(scientist_cli.shutil, "which", return_value="/bin/codex"):
            agent = {"vendor": "codex", "conversation": "c1", "model": ""}
            cmd = scientist_cli.agent_turn_command(agent, "PROMPT")
        self.assertEqual(cmd[-3:], ["resume", "c1", "PROMPT"])

    def test_unknown_vendor_raises(self):
        agent = {"vendor": "unknown"}
        with self.assertRaises(RuntimeError):
            scientist_cli.agent_turn_command(agent, "PROMPT")

    def test_agy_chat_command_builder(self):
        with patch.object(scientist_cli.shutil, "which", return_value="/bin/agy"):
            agent = {"vendor": "agy", "conversation": "c1", "model": ""}
            cmd = scientist_cli.agent_chat_command_for(agent, "new-id")
        self.assertEqual(cmd, ["/bin/agy", "--conversation", "c1"])

    def test_agy_chat_command_builder_includes_model(self):
        with patch.object(scientist_cli.shutil, "which", return_value="/bin/agy"):
            agent = {"vendor": "agy", "conversation": "c1", "model": "m2"}
            cmd = scientist_cli.agent_chat_command_for(agent, "new-id")
        self.assertEqual(cmd, ["/bin/agy", "--conversation", "c1", "--model", "m2"])

    def test_agent_chat_command_builder_is_shadowed_by_the_cli_action_BUG(self):
        with patch.object(scientist_cli.shutil, "which", return_value="/bin/agy"):
            agent = {"vendor": "agy", "conversation": "c1", "model": ""}
            cmd = scientist_cli.agent_chat_argv(agent, new_id="new-id")
        self.assertTrue(cmd[0].endswith("/agy"))


# ---------------------------------------------------------------------------
# 6. _discover_conversation
# ---------------------------------------------------------------------------


class DiscoverConversationTests(unittest.TestCase):
    def test_agy_output_with_conversation_id_field(self):
        with tempfile.TemporaryDirectory() as project:
            got = scientist_cli._discover_conversation(
                "agy", '{"conversation_id":"abc-1"}', started=0, project=Path(project))
        self.assertEqual(got, "abc-1")

    def test_claude_output_with_session_id_field(self):
        with tempfile.TemporaryDirectory() as project:
            got = scientist_cli._discover_conversation(
                "claude", '{"type":"result","session_id":"s9"}', started=0, project=Path(project))
        self.assertEqual(got, "s9")


# ---------------------------------------------------------------------------
# 6b. conversation_exists
# ---------------------------------------------------------------------------


class ConversationExistsTests(unittest.TestCase):
    def test_agy_present_file_is_true(self):
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "conversations").mkdir(parents=True)
            (Path(home) / "conversations" / "c1.db").write_text("")
            with patch.dict(os.environ, {"ANTIGRAVITY_CLI_HOME": home}):
                self.assertTrue(scientist_cli.conversation_exists({"vendor": "agy", "conversation": "c1"}))

    def test_agy_absent_file_is_false(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"ANTIGRAVITY_CLI_HOME": home}):
                self.assertFalse(scientist_cli.conversation_exists({"vendor": "agy", "conversation": "c1"}))

    def test_claude_present_file_is_true(self):
        with tempfile.TemporaryDirectory() as home:
            proj = Path(home) / "projects" / "myproj"
            proj.mkdir(parents=True)
            (proj / "s9.jsonl").write_text("")
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}):
                self.assertTrue(scientist_cli.conversation_exists({"vendor": "claude", "conversation": "s9"}))

    def test_claude_absent_file_is_false(self):
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "projects").mkdir(parents=True)
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}):
                self.assertFalse(scientist_cli.conversation_exists({"vendor": "claude", "conversation": "s9"}))

    def test_codex_present_session_file_is_true(self):
        with tempfile.TemporaryDirectory() as home:
            sessions = Path(home) / "sessions" / "2026" / "09"
            sessions.mkdir(parents=True)
            (sessions / "abc123.jsonl").write_text("{}")
            with patch.dict(os.environ, {"CODEX_HOME": home}):
                self.assertTrue(scientist_cli.conversation_exists({"vendor": "codex", "conversation": "abc123"}))

    def test_codex_present_in_session_index_is_true(self):
        with tempfile.TemporaryDirectory() as home:
            Path(home, "sessions").mkdir(parents=True)
            Path(home, "session_index.jsonl").write_text('{"id": "abc123"}\n')
            with patch.dict(os.environ, {"CODEX_HOME": home}):
                self.assertTrue(scientist_cli.conversation_exists({"vendor": "codex", "conversation": "abc123"}))

    def test_codex_absent_everywhere_is_false(self):
        with tempfile.TemporaryDirectory() as home:
            Path(home, "sessions").mkdir(parents=True)
            with patch.dict(os.environ, {"CODEX_HOME": home}):
                self.assertFalse(scientist_cli.conversation_exists({"vendor": "codex", "conversation": "abc123"}))

    def test_empty_conversation_is_new_not_missing(self):
        self.assertTrue(scientist_cli.conversation_exists({"vendor": "codex", "conversation": ""}))
        self.assertTrue(scientist_cli.conversation_exists({"vendor": "agy", "conversation": ""}))

    def test_unknown_vendor_cannot_tell_so_true(self):
        self.assertTrue(scientist_cli.conversation_exists({"vendor": "carrier-pigeon", "conversation": "x"}))


# ---------------------------------------------------------------------------
# 7. AgentRunner end to end
# ---------------------------------------------------------------------------


ACCOUNT = {"server": "http://x", "user": "u", "token": "t", "workspace_id": "ws"}


class FakeAgentServer:
    """Records every call; answers exactly what AgentRunner needs, nothing more."""

    def __init__(self, heartbeat_jobs=None, *, secure_mode=False):
        self.calls: list[tuple[str, str, dict | None]] = []
        self._heartbeat_calls = 0
        self.heartbeat_jobs = heartbeat_jobs or []
        self.secure_mode = secure_mode

    def request(self, method: str, suffix: str, body: dict | None = None) -> dict:
        self.calls.append((method, suffix, body))
        if suffix == "agents/heartbeat":
            self._heartbeat_calls += 1
            if self._heartbeat_calls == 1:
                return {"jobs": self.heartbeat_jobs, "cancelled": [], "secure_mode": self.secure_mode}
            return {"jobs": [], "cancelled": [], "secure_mode": self.secure_mode}
        if suffix.endswith("/start"):
            return {"job": {}}
        if suffix.endswith("/result"):
            return {"job": {}}
        if suffix.startswith("agents/"):
            return {"agent": {}}
        return {}

    def calls_for(self, suffix: str) -> list[tuple[str, str, dict | None]]:
        return [c for c in self.calls if c[1] == suffix]


class AlwaysOfferFakeAgentServer(FakeAgentServer):
    """Like FakeAgentServer, but keeps offering the same job on every heartbeat — used to prove
    the runner's own cooldown (not merely the server withholding the job) blocks a redispatch."""

    def request(self, method: str, suffix: str, body: dict | None = None) -> dict:
        self.calls.append((method, suffix, body))
        if suffix == "agents/heartbeat":
            return {"jobs": self.heartbeat_jobs, "cancelled": []}
        if suffix.endswith("/start"):
            return {"job": {}}
        if suffix.endswith("/result"):
            return {"job": {}}
        if suffix.startswith("agents/"):
            return {"agent": {}}
        return {}


class CancelAfterStartFakeAgentServer(FakeAgentServer):
    """Offer once, then tell the worker that the user cancelled the running turn."""

    def request(self, method: str, suffix: str, body: dict | None = None) -> dict:
        self.calls.append((method, suffix, body))
        if suffix == "agents/heartbeat":
            self._heartbeat_calls += 1
            if not getattr(self, "started", False):
                return {"jobs": self.heartbeat_jobs, "cancelled": []}
            if not getattr(self, "cancel_sent", False):
                self.cancel_sent = True
                return {"jobs": [], "cancelled": [self.heartbeat_jobs[0]["id"]]}
            return {"jobs": [], "cancelled": []}
        if suffix.endswith("/start"):
            self.started = True
            return {"job": {}}
        if suffix.endswith("/result"):
            return {"job": {}}
        return {}


class RecoveringFakeAgentServer(FakeAgentServer):
    """Simulates what the real service does when the runner clears a conversation: a
    ``POST agents/<id>`` update changes the agent record, and the job keeps being offered (it was
    requeued, not finished) until a turn actually completes. Used to prove that a turn dispatched
    right after the clear is a genuinely fresh one, carrying the persona preamble."""

    def __init__(self, agent: dict, job_template: dict):
        super().__init__()
        self.agent = dict(agent)
        self.job_template = job_template
        self.offer = True

    def request(self, method: str, suffix: str, body: dict | None = None) -> dict:
        self.calls.append((method, suffix, body))
        if suffix == "agents/heartbeat":
            if not self.offer:
                return {"jobs": [], "cancelled": []}
            job = dict(self.job_template); job["agent"] = dict(self.agent)
            return {"jobs": [job], "cancelled": []}
        if suffix.endswith("/start"):
            return {"job": {}}
        if suffix.endswith("/result"):
            if (body or {}).get("status") != "requeue":
                self.offer = False
            return {"job": {}}
        if suffix.startswith("agents/"):
            if body:
                self.agent.update(body)
            return {"agent": dict(self.agent)}
        return {}


def _fake_vendor_cmd(script: str) -> list[str]:
    return [sys.executable, "-c", script]


AGENT_AG1 = {"id": "ag-1", "name": "Ada", "vendor": "agy", "conversation": "c1", "model": "", "fresh": False}


def _build_project(agents_by_id: dict, by_worker: dict, *, worker_uid: str = "w1") -> Path:
    tmp = tempfile.mkdtemp()
    project = Path(tmp)
    _write_json(project / ".lockedin" / "config" / "identity.json", {"worker_uid": worker_uid})
    _write_json(project / ".lockedin" / "indexes" / "agents.json",
                {"by_id": agents_by_id, "by_worker": by_worker})
    return project


PAGE_MARK = {
    "surface": "page", "id": "Qx1", "page": "overview", "page_title": "Overview",
    "glyph": "?", "means": "clarify", "quote": "", "messages": [],
    "source_path": "reports/pages/overview.md", "detail_path": "feedback/pages/overview.json",
}


class AgentRunnerEndToEndTests(unittest.TestCase):
    def setUp(self):
        # tests/__init__.py sets LOCKEDIN_AGENT_TURNS=off as the suite-wide default so a
        # mis-written test never spends model tokens; this class exists specifically to dispatch
        # real (fake-vendor) turns, so lift that default here and restore it on teardown. The two
        # tests that exercise the guard itself set the variable back explicitly.
        patcher = patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("LOCKEDIN_AGENT_TURNS", None)
        # Most of this class dispatches AGENT_AG1/agent2 (conversations "c1"/"c2") without caring
        # about conversation-existence recovery, so give agy a home where both already "exist";
        # the tests that exercise the missing-conversation guard override ANTIGRAVITY_CLI_HOME
        # again within their own `with` block, which patch.dict restores back to this on exit.
        agy_home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, agy_home, ignore_errors=True)
        conv_dir = Path(agy_home) / "conversations"
        conv_dir.mkdir(parents=True)
        (conv_dir / "c1.db").write_text("")
        (conv_dir / "c2.db").write_text("")
        os.environ["ANTIGRAVITY_CLI_HOME"] = agy_home

    def _runner(self, project: Path, fake: FakeAgentServer, *, worker_uid="w1", cli="lockedin-scientist-dev"):
        sync = scientist_cli.ProjectSync(dict(ACCOUNT), project, "demo")
        sync._request = fake.request
        return scientist_cli.AgentRunner(sync, worker_uid, cli)

    def test_a_successful_turn_dispatches_reports_done_and_cleans_up(self):
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": AGENT_AG1, "mark": PAGE_MARK, "instruction": ""}
        fake = FakeAgentServer(heartbeat_jobs=[job])
        script = "import os; print('ENV_JOB_ID=' + os.environ.get('LOCKEDIN_JOB_ID', ''))"
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}), patch.object(
                scientist_cli, "agent_turn_command", lambda agent, prompt, **kw: _fake_vendor_cmd(script)):
            runner = self._runner(project, fake)
            runner.tick()

            heartbeats = fake.calls_for("agents/heartbeat")
            self.assertEqual(len(heartbeats), 1)
            self.assertEqual(heartbeats[0][2]["worker_id"], "w1")
            reported = heartbeats[0][2]["agents"][0]
            self.assertEqual((reported["id"], reported["attached"]), ("ag-1", False))
            self.assertIn("budget", reported)
            self.assertIn("confinement", reported)

            starts = fake.calls_for("jobs/j-000001/start")
            self.assertEqual(len(starts), 1)

            self.assertIn("j-000001", runner.procs)
            self.assertEqual(runner.running_job_ids(), ["j-000001"])

            log_path = Path(data_home) / "runtime" / "workers" / "w1" / "jobs" / "j-000001.log"
            self.assertTrue(log_path.exists())
            self.assertIn("LockedIn job j-000001", log_path.read_text())

            proc = runner.procs["j-000001"]["proc"]
            proc.wait(timeout=10)
            log_text = log_path.read_text()
            self.assertIn("ENV_JOB_ID=j-000001", log_text)

            runner.tick()
            results = fake.calls_for("jobs/j-000001/result")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][2]["status"], "done")
            self.assertEqual(results[0][2]["exit_code"], 0)
            self.assertIn("ENV_JOB_ID=j-000001", results[0][2]["output_tail"])
            self.assertEqual(runner.procs, {})
            self.assertFalse((Path(data_home) / "runtime" / "workers" / "w1" / "jobs" / "j-000001.pid").exists())

    def test_a_server_completed_job_reaps_a_vendor_root_still_waiting_on_background_work(self):
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": AGENT_AG1, "mark": PAGE_MARK, "instruction": ""}

        class RepliedWhileRootWaits(FakeAgentServer):
            terminal = False

            def request(self, method, suffix, body=None):
                if method == "GET" and suffix == "jobs/j-000001":
                    self.calls.append((method, suffix, body))
                    return {"job": {"status": "done" if self.terminal else "running"}}
                return super().request(method, suffix, body)

        fake = RepliedWhileRootWaits(heartbeat_jobs=[job])
        script = "import time; print('waiting for background work', flush=True); time.sleep(30)"
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}), patch.object(
                scientist_cli, "agent_turn_command", lambda agent, prompt, **kw: _fake_vendor_cmd(script)):
            runner = self._runner(project, fake)
            runner.tick()
            self.assertIn("j-000001", runner.procs)
            fake.terminal = True
            deadline = time.time() + 10
            while runner.procs and time.time() < deadline:
                runner.tick()
                time.sleep(0.05)

            self.assertEqual(runner.procs, {})
            self.assertTrue(fake.calls_for("jobs/j-000001"))
            self.assertEqual(fake.calls_for("jobs/j-000001/result"), [])
            self.assertFalse((Path(data_home) / "runtime" / "workers" / "w1" / "jobs" / "j-000001.pid").exists())

    def test_every_turn_has_closed_stdin_and_noninteractive_child_tool_environment(self):
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": AGENT_AG1, "mark": PAGE_MARK, "instruction": ""}
        fake = FakeAgentServer(heartbeat_jobs=[job])
        script = (
            "import os,sys; "
            "print('STDIN=' + repr(sys.stdin.read(1))); "
            "print('PROMPTS=' + ','.join(os.environ[k] for k in "
            "('GIT_TERMINAL_PROMPT','PIP_NO_INPUT','SSH_ASKPASS_REQUIRE')))"
        )
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}), patch.object(
                scientist_cli, "agent_turn_command", lambda agent, prompt, **kw: _fake_vendor_cmd(script)):
            runner = self._runner(project, fake)
            runner.tick()
            runner.procs["j-000001"]["proc"].wait(timeout=10)
            runner.tick()
        output = fake.calls_for("jobs/j-000001/result")[0][2]["output_tail"]
        self.assertIn("STDIN=''", output)
        self.assertIn("PROMPTS=0,1,never", output)

    def test_a_failing_turn_reports_the_exit_status(self):
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": AGENT_AG1, "mark": PAGE_MARK, "instruction": ""}
        fake = FakeAgentServer(heartbeat_jobs=[job])
        script = "import sys; sys.exit(3)"
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}), patch.object(
                scientist_cli, "agent_turn_command", lambda agent, prompt, **kw: _fake_vendor_cmd(script)):
            runner = self._runner(project, fake)
            runner.tick()
            proc = runner.procs["j-000001"]["proc"]
            proc.wait(timeout=10)
            runner.tick()
            results = fake.calls_for("jobs/j-000001/result")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][2]["status"], "failed")
            self.assertIn("exited with status 3", results[0][2]["error"])

    def test_a_structured_provider_failure_reports_its_real_reason(self):
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": AGENT_AG1, "mark": PAGE_MARK, "instruction": ""}
        fake = FakeAgentServer(heartbeat_jobs=[job])
        payload = json.dumps({"status": "ERROR", "error": "provider account access is disabled"})
        script = f"import sys; print({payload!r}); sys.exit(1)"
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}), patch.object(
                scientist_cli, "agent_turn_command", lambda agent, prompt, **kw: _fake_vendor_cmd(script)):
            runner = self._runner(project, fake)
            runner.tick()
            runner.procs["j-000001"]["proc"].wait(timeout=10)
            runner.tick()
        result = fake.calls_for("jobs/j-000001/result")[0][2]
        self.assertEqual(result["error"], "provider account access is disabled")

    def test_an_attached_agent_is_never_dispatched(self):
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": AGENT_AG1, "mark": PAGE_MARK, "instruction": ""}
        fake = FakeAgentServer(heartbeat_jobs=[job])
        with tempfile.TemporaryDirectory() as data_home, tempfile.TemporaryDirectory() as agy_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home, "ANTIGRAVITY_CLI_HOME": agy_home}):
            (Path(agy_home) / "presence").mkdir(parents=True)
            with _held_agy_lock(Path(agy_home) / "presence" / "c1.lock"):
                runner = self._runner(project, fake)
                runner.tick()
                heartbeats = fake.calls_for("agents/heartbeat")
                reported = heartbeats[0][2]["agents"][0]
                self.assertEqual((reported["id"], reported["attached"]), ("ag-1", True))
                self.assertEqual(fake.calls_for("jobs/j-000001/start"), [])
                self.assertEqual(runner.procs, {})

    def test_open_chat_then_cancel_a_hung_turn_never_duplicates_a_turn(self):
        """Regression for the Ada demo sequence: registration happens inside an open Codex chat,
        so repeated polls must leave the job queued. Once the chat closes exactly one turn may
        start, and cancelling that hung turn must kill it without another dispatch."""
        codex_agent = {**AGENT_AG1, "vendor": "codex", "conversation": "thread-1"}
        project = _build_project({"ag-1": codex_agent}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": codex_agent, "mark": PAGE_MARK, "instruction": ""}
        fake = CancelAfterStartFakeAgentServer(heartbeat_jobs=[job])
        # Each offered queued job is checked once for the heartbeat and again immediately before
        # dispatch, so fifty open-chat polls consume one hundred positive observations.
        attached = [True] * 100 + [False] * 40
        script = "import time; time.sleep(30)"
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}), patch.object(
                scientist_cli, "agent_attached", side_effect=lambda *a, **kw: attached.pop(0)), patch.object(
                scientist_cli, "conversation_exists", return_value=True), patch.object(
                scientist_cli, "agent_turn_command", lambda agent, prompt, **kw: _fake_vendor_cmd(script)):
            runner = self._runner(project, fake)
            for _ in range(50):
                runner.tick()
            self.assertEqual(fake.calls_for("jobs/j-000001/start"), [])
            self.assertEqual(runner.procs, {})

            runner.tick()  # the original chat closed: one and only one headless turn starts
            self.assertEqual(len(fake.calls_for("jobs/j-000001/start")), 1)
            self.assertIn("j-000001", runner.procs)
            runner.tick()  # cancellation reaches the next heartbeat
            deadline = time.time() + 10
            while runner.procs and time.time() < deadline:
                time.sleep(0.05)
                runner.tick()
            self.assertEqual(runner.procs, {})
            self.assertEqual(len(fake.calls_for("jobs/j-000001/start")), 1)
            results = fake.calls_for("jobs/j-000001/result")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][2]["status"], "failed")
            self.assertEqual(results[0][2]["error"], "cancelled by the user")

    def test_running_heartbeat_exposes_only_progress_counters_and_deadline(self):
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": AGENT_AG1, "mark": PAGE_MARK, "instruction": ""}
        fake = FakeAgentServer(heartbeat_jobs=[job])
        script = "import time; print('started', flush=True); time.sleep(30)"
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}), patch.object(
                scientist_cli, "agent_turn_command", lambda agent, prompt, **kw: _fake_vendor_cmd(script)):
            runner = self._runner(project, fake)
            runner.tick()
            time.sleep(0.1)
            runner.tick()
            item = fake.calls_for("agents/heartbeat")[-1][2]["agents"][0]
            self.assertEqual(item["activity"]["job_id"], "j-000001")
            self.assertGreater(item["activity"]["output_bytes"], 0)
            self.assertTrue(item["activity"]["last_output_at"].endswith("Z"))
            self.assertTrue(item["activity"]["deadline_at"].endswith("Z"))
            self.assertNotIn("output", item["activity"])
            runner.shutdown()

    def test_a_fresh_agent_learns_its_new_conversation_id(self):
        fresh_agent = {"id": "ag-1", "name": "Ada", "vendor": "agy", "conversation": "", "model": "", "fresh": True}
        project = _build_project({"ag-1": fresh_agent}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": fresh_agent, "mark": PAGE_MARK, "instruction": ""}
        fake = FakeAgentServer(heartbeat_jobs=[job])
        script = "print('{\"conversation_id\": \"new-1\"}')"
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}), patch.object(
                scientist_cli, "agent_turn_command", lambda agent, prompt, **kw: _fake_vendor_cmd(script)):
            runner = self._runner(project, fake)
            runner.tick()
            proc = runner.procs["j-000001"]["proc"]
            proc.wait(timeout=10)
            runner.tick()
            agent_updates = fake.calls_for("agents/ag-1")
            self.assertEqual(len(agent_updates), 1)
            self.assertEqual(agent_updates[0][2], {"conversation": "new-1", "fresh": False})
            log_path = Path(data_home) / "runtime" / "workers" / "w1" / "jobs" / "j-000001.log"
            prompt_start = log_path.read_text().splitlines()
            # The prompt is embedded after the "# ... <prompt>" header line.
            body = log_path.read_text().split("\n\n", 1)[1]
            self.assertTrue(body.startswith("You are Ada"))

    def test_agent_max_parallel_caps_simultaneous_dispatch(self):
        agent2 = {"id": "ag-2", "name": "Bo", "vendor": "agy", "conversation": "c2", "model": "", "fresh": False}
        project = _build_project({"ag-1": AGENT_AG1, "ag-2": agent2}, {"w1": ["ag-1", "ag-2"]})
        job1 = {"id": "j-000001", "agent": AGENT_AG1, "mark": PAGE_MARK, "instruction": ""}
        job2 = {"id": "j-000002", "agent": agent2, "mark": PAGE_MARK, "instruction": ""}
        fake = FakeAgentServer(heartbeat_jobs=[job1, job2])
        script = "import time; time.sleep(2)"
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}), patch.object(
                scientist_cli, "AGENT_MAX_PARALLEL", 1), patch.object(
                scientist_cli, "agent_turn_command", lambda agent, prompt, **kw: _fake_vendor_cmd(script)):
            runner = self._runner(project, fake)
            runner.tick()
            starts = [c for c in fake.calls if c[1].endswith("/start")]
            self.assertEqual(len(starts), 1)
            runner.shutdown()

    def test_a_turn_that_exceeds_the_time_budget_is_terminated_and_reported(self):
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": AGENT_AG1, "mark": PAGE_MARK, "instruction": ""}
        fake = FakeAgentServer(heartbeat_jobs=[job])
        script = "import time; time.sleep(30)"
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}), patch.object(
                scientist_cli, "AGENT_TURN_SECONDS", 0), patch.object(
                scientist_cli, "agent_turn_command", lambda agent, prompt, **kw: _fake_vendor_cmd(script)):
            runner = self._runner(project, fake)
            runner.tick()
            self.assertIn("j-000001", runner.procs)
            deadline = time.time() + 15
            while runner.procs and time.time() < deadline:
                time.sleep(0.2)
                runner.tick()
            self.assertEqual(runner.procs, {})
            results = fake.calls_for("jobs/j-000001/result")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][2]["status"], "failed")
            self.assertIn("timed out", results[0][2]["error"])

    def test_repeated_vendor_network_reconnects_fail_fast(self):
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": AGENT_AG1, "mark": PAGE_MARK, "instruction": ""}
        fake = FakeAgentServer(heartbeat_jobs=[job])
        script = ("import time; print('Reconnecting... waiting for network', flush=True); "
                  "print('Reconnecting... waiting for network', flush=True); "
                  "print('Reconnecting... waiting for network', flush=True); time.sleep(30)")
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}), patch.object(
                scientist_cli, "AGENT_NETWORK_GRACE_SECONDS", 0), patch.object(
                scientist_cli, "agent_turn_command", lambda agent, prompt, **kw: _fake_vendor_cmd(script)):
            runner = self._runner(project, fake)
            runner.tick()
            time.sleep(0.1)
            deadline = time.time() + 10
            while runner.procs and time.time() < deadline:
                runner.tick()
                time.sleep(0.05)
            self.assertEqual(runner.procs, {})
            result = fake.calls_for("jobs/j-000001/result")[0][2]
            self.assertEqual(result["status"], "failed")
            self.assertIn("repeated reconnects", result["error"])

    def test_a_busy_chat_error_requeues_instead_of_failing_and_blocks_redispatch(self):
        """Real captured failure: `codex exec resume <id>` exited 1 with a thread-store conflict
        because an interactive session held the writer. That must requeue the job, not fail it,
        and the runner must not immediately respawn into the same busy chat."""
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": AGENT_AG1, "mark": PAGE_MARK, "instruction": ""}
        fake = AlwaysOfferFakeAgentServer(heartbeat_jobs=[job])
        script = ("import sys; sys.stderr.write("
                  "'thread-store conflict: thread X already has an active writer\\n'); sys.exit(1)")
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}), patch.object(
                scientist_cli, "agent_turn_command", lambda agent, prompt, **kw: _fake_vendor_cmd(script)):
            runner = self._runner(project, fake)
            runner.tick()
            proc = runner.procs["j-000001"]["proc"]
            proc.wait(timeout=10)
            runner.tick()  # reaps: should post "requeue", not "failed"

            results = fake.calls_for("jobs/j-000001/result")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][2]["status"], "requeue")
            self.assertIn("ag-1", runner.cooldowns)

            starts_before = len(fake.calls_for("jobs/j-000001/start"))
            runner.tick()  # heartbeat re-offers the same job; the cooldown must still block it
            starts_after = len(fake.calls_for("jobs/j-000001/start"))
            self.assertEqual(starts_before, starts_after)

    def test_an_ordinary_error_still_fails_the_job(self):
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": AGENT_AG1, "mark": PAGE_MARK, "instruction": ""}
        fake = FakeAgentServer(heartbeat_jobs=[job])
        script = "import sys; sys.stderr.write('permission denied\\n'); sys.exit(1)"
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}), patch.object(
                scientist_cli, "agent_turn_command", lambda agent, prompt, **kw: _fake_vendor_cmd(script)):
            runner = self._runner(project, fake)
            runner.tick()
            proc = runner.procs["j-000001"]["proc"]
            proc.wait(timeout=10)
            runner.tick()
            results = fake.calls_for("jobs/j-000001/result")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][2]["status"], "failed")
            self.assertNotIn("ag-1", runner.cooldowns)

    def test_a_missing_conversation_is_caught_before_spawn_and_requeues(self):
        """AGENT_AG1's conversation "c1" has no file in the fake agy home: the runner must not
        spawn agy at all, must tell the server to forget the conversation exactly as `agent
        reset` does, and must requeue the job with a reason naming what happened."""
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": AGENT_AG1, "mark": PAGE_MARK, "instruction": ""}
        fake = FakeAgentServer(heartbeat_jobs=[job])

        def _never_spawn(agent, prompt, **kw):
            raise AssertionError("must not spawn into a conversation that no longer exists")

        with tempfile.TemporaryDirectory() as data_home, tempfile.TemporaryDirectory() as agy_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home, "ANTIGRAVITY_CLI_HOME": agy_home}), patch.object(
                scientist_cli, "agent_turn_command", _never_spawn):
            runner = self._runner(project, fake)
            runner.tick()

        self.assertEqual(runner.procs, {})
        clears = fake.calls_for("agents/ag-1")
        self.assertEqual(len(clears), 1)
        self.assertEqual(clears[0][2], {"conversation": "", "fresh": True})
        results = fake.calls_for("jobs/j-000001/result")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][2]["status"], "requeue")
        self.assertIn("no longer exists", results[0][2]["error"])
        self.assertNotIn("ag-1", runner.cooldowns)

    def test_a_lost_conversation_error_from_the_vendor_requeues_not_fails(self):
        """A conversation can vanish between the pre-dispatch check and the spawn, or a vendor may
        keep the file but refuse the id anyway; recognise its own words instead of failing."""
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": AGENT_AG1, "mark": PAGE_MARK, "instruction": ""}
        fake = FakeAgentServer(heartbeat_jobs=[job])
        script = "import sys; sys.stderr.write('Error: conversation not found\\n'); sys.exit(1)"
        with tempfile.TemporaryDirectory() as data_home, tempfile.TemporaryDirectory() as agy_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home, "ANTIGRAVITY_CLI_HOME": agy_home}), patch.object(
                scientist_cli, "agent_turn_command", lambda agent, prompt, **kw: _fake_vendor_cmd(script)):
            (Path(agy_home) / "conversations").mkdir(parents=True)
            (Path(agy_home) / "conversations" / "c1.db").write_text("")  # the cheap check passes
            runner = self._runner(project, fake)
            runner.tick()
            proc = runner.procs["j-000001"]["proc"]
            proc.wait(timeout=10)
            runner.tick()  # reaps

        clears = fake.calls_for("agents/ag-1")
        self.assertEqual(len(clears), 1)
        self.assertEqual(clears[0][2], {"conversation": "", "fresh": True})
        results = fake.calls_for("jobs/j-000001/result")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][2]["status"], "requeue")
        self.assertNotIn("ag-1", runner.cooldowns)

    def test_an_ordinary_conversation_mention_still_fails_not_a_lost_conversation(self):
        """The signatures must stay specific: an error that merely mentions "conversation" in
        passing must not be swallowed as a lost-conversation recovery."""
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": AGENT_AG1, "mark": PAGE_MARK, "instruction": ""}
        fake = FakeAgentServer(heartbeat_jobs=[job])
        script = ("import sys; sys.stderr.write("
                  "'the conversation history is too long to continue\\n'); sys.exit(1)")
        with tempfile.TemporaryDirectory() as data_home, tempfile.TemporaryDirectory() as agy_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home, "ANTIGRAVITY_CLI_HOME": agy_home}), patch.object(
                scientist_cli, "agent_turn_command", lambda agent, prompt, **kw: _fake_vendor_cmd(script)):
            (Path(agy_home) / "conversations").mkdir(parents=True)
            (Path(agy_home) / "conversations" / "c1.db").write_text("")
            runner = self._runner(project, fake)
            runner.tick()
            proc = runner.procs["j-000001"]["proc"]
            proc.wait(timeout=10)
            runner.tick()

        results = fake.calls_for("jobs/j-000001/result")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][2]["status"], "failed")
        self.assertEqual(fake.calls_for("agents/ag-1"), [])

    def test_after_clearing_the_next_tick_dispatches_a_fresh_turn_with_the_persona_preamble(self):
        job_template = {"id": "j-000001", "mark": PAGE_MARK, "instruction": ""}
        fake = RecoveringFakeAgentServer(AGENT_AG1, job_template)
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        script = "print('{\"conversation_id\": \"new-1\"}')"
        with tempfile.TemporaryDirectory() as data_home, tempfile.TemporaryDirectory() as agy_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home, "ANTIGRAVITY_CLI_HOME": agy_home}), patch.object(
                scientist_cli, "agent_turn_command", lambda agent, prompt, **kw: _fake_vendor_cmd(script)):
            runner = self._runner(project, fake)
            runner.tick()  # conversation missing: clears + requeues, no spawn
            self.assertEqual(runner.procs, {})
            self.assertEqual(fake.agent.get("conversation"), "")
            self.assertTrue(fake.agent.get("fresh"))

            runner.tick()  # server now offers the job again with the cleared agent: dispatch fresh
            self.assertIn("j-000001", runner.procs)
            proc = runner.procs["j-000001"]["proc"]
            proc.wait(timeout=10)

            log_path = Path(data_home) / "runtime" / "workers" / "w1" / "jobs" / "j-000001.log"
            body = log_path.read_text().split("\n\n", 1)[1]
            self.assertTrue(body.startswith("You are Ada"))

    def test_agent_turns_disabled_makes_no_request_and_spawns_nothing(self):
        """LOCKEDIN_AGENT_TURNS=off must stop tick() before any heartbeat, job start, or spawn —
        even when the fake server is offering a queued job right away."""
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": AGENT_AG1, "mark": PAGE_MARK, "instruction": ""}
        fake = FakeAgentServer(heartbeat_jobs=[job])
        script = "import sys; sys.exit(0)"
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home, "LOCKEDIN_AGENT_TURNS": "off"}), patch.object(
                scientist_cli, "agent_turn_command", lambda agent, prompt, **kw: _fake_vendor_cmd(script)):
            runner = self._runner(project, fake)
            runner.tick()
            self.assertEqual(fake.calls, [])
            self.assertEqual(runner.procs, {})
            self.assertIn("LOCKEDIN_AGENT_TURNS=off", runner.error)

    def test_agent_turns_enabled_by_default_still_dispatches(self):
        """Same setup as above, but with LOCKEDIN_AGENT_TURNS unset: proves the guard above is
        what makes the difference, not a broken fixture."""
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": AGENT_AG1, "mark": PAGE_MARK, "instruction": ""}
        fake = FakeAgentServer(heartbeat_jobs=[job])
        script = "import sys; sys.exit(0)"
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}, clear=False), patch.object(
                scientist_cli, "agent_turn_command", lambda agent, prompt, **kw: _fake_vendor_cmd(script)):
            os.environ.pop("LOCKEDIN_AGENT_TURNS", None)
            runner = self._runner(project, fake)
            runner.tick()
            self.assertTrue(fake.calls_for("agents/heartbeat"))
            self.assertTrue(fake.calls_for("jobs/j-000001/start"))
            self.assertIn("j-000001", runner.procs)
            proc = runner.procs["j-000001"]["proc"]
            proc.wait(timeout=10)
            runner.shutdown()

    def test_my_agents_is_empty_without_an_index_and_tick_makes_no_requests(self):
        project = Path(tempfile.mkdtemp())
        (project / ".lockedin" / "config").mkdir(parents=True)
        _write_json(project / ".lockedin" / "config" / "identity.json", {"worker_uid": "w1"})
        fake = FakeAgentServer()
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}):
            runner = self._runner(project, fake)
            self.assertEqual(runner.my_agents(), [])
            runner.tick()
            self.assertEqual(fake.calls, [])

    def test_heartbeat_carries_budget_and_confinement(self):
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        fake = FakeAgentServer()
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}), patch.object(
                scientist_cli, "confinement_mode", return_value="trust"):
            runner = self._runner(project, fake)
            runner.tick()
            heartbeats = fake.calls_for("agents/heartbeat")
            self.assertEqual(len(heartbeats), 1)
            body = heartbeats[0][2]
            self.assertEqual(body["confinement"], "trust")
            for key in ("hour_used", "hour_cap", "day_used", "day_cap", "exhausted", "resumes_at"):
                self.assertIn(key, body["budget"])
            self.assertFalse(body["budget"]["exhausted"])

    def test_secure_mode_terminates_a_running_turn_and_dispatches_nothing(self):
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": AGENT_AG1, "mark": PAGE_MARK, "instruction": ""}
        fake = FakeAgentServer(heartbeat_jobs=[job])
        script = "import time; time.sleep(30)"
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}), patch.object(
                scientist_cli, "agent_turn_command", lambda agent, prompt, **kw: _fake_vendor_cmd(script)):
            runner = self._runner(project, fake)
            runner.tick()
            self.assertIn("j-000001", runner.procs)
            fake.secure_mode = True
            runner.tick()
            self.assertEqual(runner.error, "secure mode is on: agents stopped")
            deadline = time.time() + 15
            while runner.procs and time.time() < deadline:
                time.sleep(0.2)
                runner.tick()
            self.assertEqual(runner.procs, {})
            starts = fake.calls_for("jobs/j-000001/start")
            self.assertEqual(len(starts), 1)  # no redispatch while secure mode holds


class AgentRunnerBudgetTests(unittest.TestCase):
    def setUp(self):
        # Same reasoning as AgentRunnerEndToEndTests.setUp: lift the suite-wide
        # LOCKEDIN_AGENT_TURNS=off default (some of these tests actually dispatch), and give agy a
        # home where AGENT_AG1's conversation "c1" already "exists" so dispatch is not blocked on
        # conversation-existence recovery, which is not what this class is testing.
        patcher = patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("LOCKEDIN_AGENT_TURNS", None)
        agy_home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, agy_home, ignore_errors=True)
        (Path(agy_home) / "conversations").mkdir(parents=True)
        (Path(agy_home) / "conversations" / "c1.db").write_text("")
        os.environ["ANTIGRAVITY_CLI_HOME"] = agy_home

    def _runner(self, project: Path, fake: FakeAgentServer, *, worker_uid="w1", cli="lockedin-scientist-dev"):
        sync = scientist_cli.ProjectSync(dict(ACCOUNT), project, "demo")
        sync._request = fake.request
        return scientist_cli.AgentRunner(sync, worker_uid, cli)

    def test_exhausted_hourly_budget_blocks_dispatch_and_names_when_it_resumes(self):
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": AGENT_AG1, "mark": PAGE_MARK, "instruction": ""}
        fake = FakeAgentServer(heartbeat_jobs=[job])
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}), patch.object(
                scientist_cli, "AGENT_MAX_TURNS_PER_HOUR", 2):
            runner = self._runner(project, fake)
            now = time.time()
            scientist_cli._atomic_json(runner.turns_path, {"turns": [now - 60, now - 30]})
            runner.tick()
            self.assertEqual(runner.procs, {})
            self.assertEqual(fake.calls_for("jobs/j-000001/start"), [])
            self.assertIn("budget: 2 turns in the last hour", runner.error)
            self.assertIn("next turn at", runner.error)
            body = fake.calls_for("agents/heartbeat")[0][2]
            self.assertTrue(body["budget"]["exhausted"])
            self.assertEqual(body["budget"]["hour_cap"], 2)
            self.assertEqual(body["budget"]["hour_used"], 2)
            self.assertTrue(body["budget"]["resumes_at"])

    def test_an_old_entry_beyond_the_window_is_ignored(self):
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        job = {"id": "j-000001", "agent": AGENT_AG1, "mark": PAGE_MARK, "instruction": ""}
        fake = FakeAgentServer(heartbeat_jobs=[job])
        script = "import sys; sys.exit(0)"
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}), patch.object(
                scientist_cli, "AGENT_MAX_TURNS_PER_HOUR", 2), patch.object(
                scientist_cli, "agent_turn_command", lambda agent, prompt, **kw: _fake_vendor_cmd(script)):
            runner = self._runner(project, fake)
            now = time.time()
            scientist_cli._atomic_json(runner.turns_path, {"turns": [now - 7200, now - 7100]})
            runner.tick()
            self.assertIn("j-000001", runner.procs)
            runner.procs["j-000001"]["proc"].wait(timeout=10)
            runner.shutdown()

    def test_the_turns_file_survives_a_new_runner_so_a_restart_cannot_reset_the_count(self):
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        fake = FakeAgentServer()
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}):
            runner1 = self._runner(project, fake)
            now = time.time()
            runner1._record_turn_start(now)
            runner2 = self._runner(project, fake)  # a fresh instance, same worker id
            budget, _ = runner2._budget(now)
            self.assertEqual(budget["hour_used"], 1)

    def test_cap_zero_or_negative_means_unlimited(self):
        project = _build_project({"ag-1": AGENT_AG1}, {"w1": ["ag-1"]})
        fake = FakeAgentServer()
        with tempfile.TemporaryDirectory() as data_home, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": data_home}), patch.object(
                scientist_cli, "AGENT_MAX_TURNS_PER_HOUR", 0), patch.object(
                scientist_cli, "AGENT_MAX_TURNS_PER_DAY", -5):
            runner = self._runner(project, fake)
            now = time.time()
            for _ in range(50):
                runner._record_turn_start(now)
            budget, error = runner._budget(now)
            self.assertFalse(budget["exhausted"])
            self.assertEqual(budget["hour_cap"], 0)
            self.assertEqual(budget["day_cap"], 0)
            self.assertEqual(error, "")


# ---------------------------------------------------------------------------
# 9. Confinement
# ---------------------------------------------------------------------------


class ConfinementModeTests(unittest.TestCase):
    def test_env_override_none_forces_unconfined_even_with_a_reported_landlock_abi(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["LOCKEDIN_AGENT_CONFINEMENT"] = "none"
            with patch.object(scientist_cli, "landlock_abi", return_value=6):
                self.assertEqual(scientist_cli.confinement_mode(), "none")

    def test_env_override_trust_wins_over_everything(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["LOCKEDIN_AGENT_CONFINEMENT"] = "trust"
            with patch.object(sys, "platform", "win32"):
                self.assertEqual(scientist_cli.confinement_mode(), "trust")

    def test_linux_with_a_landlock_abi_is_landlock(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOCKEDIN_AGENT_CONFINEMENT", None)
            with patch.object(sys, "platform", "linux"), patch.object(
                    scientist_cli, "landlock_abi", return_value=6):
                self.assertEqual(scientist_cli.confinement_mode(), "landlock")

    def test_linux_without_a_landlock_abi_is_none(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOCKEDIN_AGENT_CONFINEMENT", None)
            with patch.object(sys, "platform", "linux"), patch.object(
                    scientist_cli, "landlock_abi", return_value=-1):
                self.assertEqual(scientist_cli.confinement_mode(), "none")

    def test_macos_with_sandbox_exec_present_is_seatbelt(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOCKEDIN_AGENT_CONFINEMENT", None)
            with patch.object(sys, "platform", "darwin"), patch.object(
                    os.path, "exists", return_value=True):
                self.assertEqual(scientist_cli.confinement_mode(), "seatbelt")

    def test_macos_without_sandbox_exec_is_none(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOCKEDIN_AGENT_CONFINEMENT", None)
            with patch.object(sys, "platform", "darwin"), patch.object(
                    os.path, "exists", return_value=False):
                self.assertEqual(scientist_cli.confinement_mode(), "none")

    def test_an_unrecognised_platform_is_none(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOCKEDIN_AGENT_CONFINEMENT", None)
            with patch.object(sys, "platform", "aix"):
                self.assertEqual(scientist_cli.confinement_mode(), "none")


def _landlock_available() -> bool:
    return scientist_cli.landlock_abi() >= 1


class SeatbeltProfileTests(unittest.TestCase):
    def test_macos_temp_directory_and_network_are_explicitly_allowed(self):
        with tempfile.TemporaryDirectory() as project_dir, tempfile.TemporaryDirectory() as mac_tmp, patch.dict(
                os.environ, {"TMPDIR": mac_tmp}):
            project = Path(project_dir)
            (project / ".lockedin").mkdir()
            roots = [root for root, required in scientist_cli._agent_writable_roots(project)
                     if required or root.exists()]
            profile = scientist_cli._seatbelt_profile(roots)
        self.assertIn("(allow network*)", profile)
        self.assertIn(f'(allow file-write* (subpath "{Path(mac_tmp).resolve()}"))', profile)
        self.assertIn(f'(allow file-write* (subpath "{(project / ".lockedin").resolve()}"))', profile)


class RealLandlockConfinementTests(unittest.TestCase):
    """The test that matters most: a real Landlock-confined child, not a mock."""

    def _run(self, project: Path, code: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", code], cwd=str(project),
            preexec_fn=functools.partial(scientist_cli._landlock_child_confine, project),
            capture_output=True, text=True, timeout=10)

    @unittest.skipUnless(_landlock_available(), "Landlock is not available on this kernel")
    def test_confined_child_can_write_inside_lockedin_read_everywhere_keep_network(self):
        with tempfile.TemporaryDirectory() as project_dir:
            project = Path(project_dir)
            (project / ".lockedin").mkdir()

            ok_inside = self._run(project, f"open({str(project / '.lockedin' / 'ok.txt')!r}, 'w').write('hi')")
            self.assertEqual(ok_inside.returncode, 0, ok_inside.stderr)
            self.assertTrue((project / ".lockedin" / "ok.txt").exists())

            can_read = self._run(project, "print(open('/etc/hostname').read())")
            self.assertEqual(can_read.returncode, 0, can_read.stderr)

            net = self._run(project, "import socket; socket.create_connection(('1.1.1.1', 80), "
                                      "timeout=3); print('net ok')")
            self.assertEqual(net.returncode, 0, net.stderr)
            self.assertIn("net ok", net.stdout)

    @unittest.skipUnless(_landlock_available(), "Landlock is not available on this kernel")
    def test_confined_child_cannot_write_to_the_home_directory_or_a_sibling_outside_tmp(self):
        with tempfile.TemporaryDirectory() as project_dir:
            project = Path(project_dir)
            (project / ".lockedin").mkdir()
            home_marker = Path.home() / "__lockedin_confinement_test_marker__"
            self.addCleanup(lambda: home_marker.unlink(missing_ok=True))
            denied_home = self._run(
                project, f"open({str(home_marker)!r}, 'w').write('hi')")
            self.assertNotEqual(denied_home.returncode, 0)
            self.assertFalse(home_marker.exists())

            outside_tmp = tempfile.mkdtemp(dir=str(Path.home()))
            self.addCleanup(shutil.rmtree, outside_tmp, ignore_errors=True)
            denied_sibling = self._run(
                project, f"open({str(Path(outside_tmp) / 'bad.txt')!r}, 'w').write('hi')")
            self.assertNotEqual(denied_sibling.returncode, 0)
            self.assertFalse((Path(outside_tmp) / "bad.txt").exists())

    @unittest.skipUnless(_landlock_available(), "Landlock is not available on this kernel")
    def test_confinement_fails_closed_with_exit_97_when_a_writable_root_does_not_exist(self):
        with tempfile.TemporaryDirectory() as project_dir, patch.dict(
                os.environ, {"LOCKEDIN_AGENT_WRITABLE": "/no/such/lockedin/writable/path"}):
            project = Path(project_dir)
            (project / ".lockedin").mkdir()
            r = self._run(project, "print('should never run')")
            self.assertEqual(r.returncode, 97)
            self.assertIn("could not confine the turn", r.stderr)
            self.assertNotIn("should never run", r.stdout)


class ScratchStressTestConfinementTests(unittest.TestCase):
    """The pattern documented in guides/agents.md: a scratch script imports and calls the
    project's own code — without write access to the project — to try it out or stress-test it."""

    @unittest.skipUnless(_landlock_available(), "Landlock is not available on this kernel")
    def test_scratch_script_calls_project_code_and_cannot_write_to_the_project(self):
        base = tempfile.mkdtemp(dir=str(Path.home()))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        project = Path(base)
        pycache = project / ".lockedin" / "scratch" / ".pycache"
        pycache.mkdir(parents=True)
        (project / "trunk").mkdir()
        (project / "trunk" / "__init__.py").write_text("")
        original = "def func():\n    return 42\n"
        (project / "trunk" / "mylib.py").write_text(original)
        out_path = project / ".lockedin" / "scratch" / "out.txt"
        mylib_path = project / "trunk" / "mylib.py"
        script = project / ".lockedin" / "scratch" / "try.py"
        script.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(project)!r})\n"
            "from trunk.mylib import func\n"
            f"open({str(out_path)!r}, 'w').write(str(func()))\n"
            "try:\n"
            f"    open({str(mylib_path)!r}, 'a').write('oops')\n"
            "except OSError:\n"
            "    open({!r}, 'w').write('denied')\n".format(str(project / ".lockedin" / "scratch" / "write_status.txt"))
        )
        env = dict(os.environ)
        env["PYTHONPYCACHEPREFIX"] = str(pycache)
        r = subprocess.run(
            [sys.executable, str(script)], cwd=str(project), env=env,
            preexec_fn=functools.partial(scientist_cli._landlock_child_confine, project),
            capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out_path.read_text(), "42")
        self.assertEqual(mylib_path.read_text(), original)
        self.assertEqual((project / ".lockedin" / "scratch" / "write_status.txt").read_text(), "denied")
        self.assertFalse((project / "trunk" / "__pycache__").exists())
        self.assertTrue(any(pycache.rglob("mylib*.pyc")))


class AgentTurnCommandConfinementModeTests(unittest.TestCase):
    """agent_turn_command's vendor flags follow the ``mode`` argument explicitly — see its
    docstring — so this is tested without touching the environment at all."""

    def setUp(self):
        patcher = patch.object(scientist_cli.shutil, "which", side_effect=lambda name: f"/bin/{name}")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _agent(self, vendor: str) -> dict:
        return {"vendor": vendor, "conversation": "c1", "model": ""}

    def test_claude_confined_and_trust_use_bypass_permissions(self):
        for mode in ("landlock", "seatbelt", "trust"):
            cmd = scientist_cli.agent_turn_command(self._agent("claude"), "P", mode=mode)
            self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "bypassPermissions", mode)

    def test_claude_none_keeps_accept_edits(self):
        cmd = scientist_cli.agent_turn_command(self._agent("claude"), "P", mode="none")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")

    def test_codex_confined_and_trust_bypass_approvals_and_sandbox(self):
        for mode in ("landlock", "seatbelt", "trust"):
            cmd = scientist_cli.agent_turn_command(self._agent("codex"), "P", mode=mode)
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)
            self.assertNotIn("-s", cmd)

    def test_codex_none_keeps_workspace_write_with_network_allowed(self):
        cmd = scientist_cli.agent_turn_command(self._agent("codex"), "P", mode="none")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertEqual(cmd[cmd.index("-s") + 1], "workspace-write")
        self.assertIn("sandbox_workspace_write.network_access=true", cmd)

    def test_agy_confined_and_trust_skip_permissions(self):
        for mode in ("landlock", "seatbelt", "trust"):
            cmd = scientist_cli.agent_turn_command(self._agent("agy"), "P", mode=mode)
            self.assertIn("--dangerously-skip-permissions", cmd)
            self.assertNotIn("--mode", cmd)

    def test_agy_none_keeps_accept_edits_mode(self):
        cmd = scientist_cli.agent_turn_command(self._agent("agy"), "P", mode="none")
        self.assertEqual(cmd[cmd.index("--mode") + 1], "accept-edits")
        self.assertNotIn("--dangerously-skip-permissions", cmd)

    def test_mode_defaults_to_the_machines_actual_confinement_when_omitted(self):
        with patch.object(scientist_cli, "confinement_mode", return_value="none"):
            cmd = scientist_cli.agent_turn_command(self._agent("claude"), "P")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")

    def test_supported_vendor_versions_get_every_available_noninteractive_flag(self):
        def help_text(_executable, subcommand=""):
            return "--permission-prompts --dangerously-bypass-hook-trust"
        with patch.object(agent_vendors, "_help_text", side_effect=help_text):
            claude = scientist_cli.agent_turn_command(self._agent("claude"), "P", mode="seatbelt")
            codex = scientist_cli.agent_turn_command(self._agent("codex"), "P", mode="seatbelt")
        self.assertEqual(claude[claude.index("--permission-prompts") + 1], "none")
        self.assertIn("--dangerously-bypass-hook-trust", codex)

    def test_older_vendor_versions_keep_working_when_optional_flags_are_absent(self):
        with patch.object(agent_vendors, "_help_text", return_value=""):
            claude = scientist_cli.agent_turn_command(self._agent("claude"), "P", mode="seatbelt")
            codex = scientist_cli.agent_turn_command(self._agent("codex"), "P", mode="seatbelt")
        self.assertNotIn("--permission-prompts", claude)
        self.assertNotIn("--dangerously-bypass-hook-trust", codex)

    def test_all_three_confined_commands_are_explicitly_headless(self):
        with patch.object(agent_vendors, "_help_text",
                          return_value="--permission-prompts --dangerously-bypass-hook-trust"):
            claude = scientist_cli.agent_turn_command(self._agent("claude"), "P", mode="seatbelt")
            codex = scientist_cli.agent_turn_command(self._agent("codex"), "P", mode="seatbelt")
            agy = scientist_cli.agent_turn_command(self._agent("agy"), "P", mode="seatbelt")
        self.assertIn("-p", claude)
        self.assertIn("bypassPermissions", claude)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", codex)
        self.assertIn("--dangerously-bypass-hook-trust", codex)
        self.assertIn("-p", agy)
        self.assertIn("--dangerously-skip-permissions", agy)
        self.assertIn("--disable-slash-commands", agy)
        self.assertIn("--print-timeout", agy)


class AgentTurnPromptConfinementTests(unittest.TestCase):
    def test_fresh_prompt_states_the_write_boundary(self):
        prompt = scientist_cli.agent_turn_prompt(PAGE_JOB, cli="lockedin-scientist-dev", fresh=True, mode="landlock")
        self.assertIn(".lockedin/scratch/", prompt)
        self.assertIn(".lockedin/reports/", prompt)
        self.assertNotIn("Nothing on this machine enforces", prompt)

    def test_fresh_prompt_in_none_mode_names_the_gap(self):
        prompt = scientist_cli.agent_turn_prompt(PAGE_JOB, cli="lockedin-scientist-dev", fresh=True, mode="none")
        self.assertIn("Nothing on this machine enforces that boundary", prompt)

    def test_resumed_prompt_omits_the_fresh_only_reminder(self):
        prompt = scientist_cli.agent_turn_prompt(PAGE_JOB, cli="lockedin-scientist-dev", fresh=False, mode="none")
        self.assertNotIn("Nothing on this machine enforces", prompt)


class ScratchSurvivesSyncTests(unittest.TestCase):
    """``.lockedin/scratch/`` is never scanned by ``_report_paths`` and is not one of the fixed
    top-level names ``sync_once`` prunes, so a sync cycle must leave it untouched."""

    class _FakeBubbleServer:
        def __init__(self):
            self.files: dict[str, bytes] = {}

        def request(self, _server, method, endpoint, body=None, token="", workspace="", *,
                    extra=None, timeout=90):
            if endpoint.endswith("/guide"):
                return {"guide": "## Markdown\n\nUse the canonical guide.\n"}
            if endpoint.endswith("/manifest"):
                return {"files": [{"path": p, "revision": scientist_cli.ProjectSync._rev(raw)}
                                  for p, raw in sorted(self.files.items())]}
            if endpoint.endswith("/files"):
                return {"files": [{"path": p, "revision": scientist_cli.ProjectSync._rev(self.files[p]),
                                   "content_b64": base64.b64encode(self.files[p]).decode()}
                                  for p in (body or {}).get("paths", []) if p in self.files]}
            if endpoint.endswith("/push") or endpoint.endswith("/deletes") or endpoint.endswith("/pages"):
                return {"applied": [], "conflicts": []}
            raise AssertionError(f"unexpected endpoint in scratch-survival test: {endpoint}")

    def test_validate_or_initialize_creates_scratch(self):
        fake = self._FakeBubbleServer()
        with tempfile.TemporaryDirectory() as directory, patch.object(
                scientist_cli, "request", side_effect=fake.request):
            project = Path(directory)
            sync = scientist_cli.ProjectSync(dict(ACCOUNT), project, "work")
            sync.validate_or_initialize()
            self.assertTrue((sync.root / "scratch").is_dir())
            self.assertTrue((sync.root / "scratch" / ".pycache").is_dir())

    def test_a_file_placed_in_scratch_survives_repeated_sync_cycles(self):
        fake = self._FakeBubbleServer()
        with tempfile.TemporaryDirectory() as directory, patch.object(
                scientist_cli, "request", side_effect=fake.request):
            project = Path(directory)
            sync = scientist_cli.ProjectSync(dict(ACCOUNT), project, "work")
            sync.validate_or_initialize()
            keep = sync.root / "scratch" / "notes.txt"
            keep.write_text("throwaway work")
            sync.sync_once()
            sync.sync_once()
            self.assertEqual(keep.read_text(), "throwaway work")
            self.assertNotIn("scratch/notes.txt", fake.files)


# ---------------------------------------------------------------------------
# 10. Argparse smoke tests
# ---------------------------------------------------------------------------


class MainStreamReconfigureTests(unittest.TestCase):
    """main() is the process entry point an agent's captured pipe hits; see scientist_cli.main's
    comment about locale encoding winning on a pipe (the Windows cp1252 '◆' crash)."""

    class _FakeStream:
        def __init__(self, name):
            self.name, self.reconfigure_calls = name, []

        def reconfigure(self, **kw):
            self.reconfigure_calls.append(kw)

        def write(self, *a, **kw): pass

        def flush(self): pass

    def _run_main_with(self, out, err):
        argv = sys.argv
        sys.argv = ["lockedin-scientist", "--version"]
        try:
            with patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
                with self.assertRaises(SystemExit):
                    scientist_cli.main()
        finally:
            sys.argv = argv

    def test_main_asks_both_streams_to_reconfigure_to_utf8_with_replace(self):
        out, err = self._FakeStream("stdout"), self._FakeStream("stderr")
        self._run_main_with(out, err)
        self.assertEqual(out.reconfigure_calls, [{"encoding": "utf-8", "errors": "replace"}])
        self.assertEqual(err.reconfigure_calls, [{"encoding": "utf-8", "errors": "replace"}])

    def test_main_tolerates_a_stream_without_reconfigure(self):
        class NoReconfigure:
            def write(self, *a, **kw): pass
            def flush(self): pass
        # Must not raise AttributeError just because a stream lacks reconfigure().
        self._run_main_with(NoReconfigure(), NoReconfigure())


class ArgparseSmokeTests(unittest.TestCase):
    def test_default_turn_budget_is_shared_100_per_hour_and_500_per_day(self):
        self.assertEqual(scientist_cli.AGENT_MAX_TURNS_PER_HOUR, 100)
        self.assertEqual(scientist_cli.AGENT_MAX_TURNS_PER_DAY, 500)

    def test_agent_help_lists_every_subcommand(self):
        import io
        argv = sys.argv
        sys.argv = ["lockedin-scientist", "agent", "--help"]
        out = io.StringIO()
        try:
            with self.assertRaises(SystemExit) as ctx, redirect_stdout(out):
                scientist_cli._main()
        finally:
            sys.argv = argv
        self.assertEqual(ctx.exception.code, 0)
        text = out.getvalue()
        for name in ("register", "list", "jobs", "chat", "revive", "reply", "fail", "reset", "retire"):
            self.assertIn(name, text)

    def test_welcome_mentions_agent_register(self):
        import io
        out = io.StringIO()
        with redirect_stdout(out):
            scientist_cli.welcome()
        self.assertIn("agent register", out.getvalue())

    def test_agent_revive_resumes_its_original_worker_without_resetting_persona(self):
        import io
        sync = types.SimpleNamespace(worker_uid=lambda: "w1")
        agent = {"id": "ag-1", "name": "Ada", "worker_id": "w1",
                 "role": "reviewer", "personality": "terse", "conversation": "conv-1"}
        out = io.StringIO()
        with patch.object(scientist_cli, "_agent_context",
                          return_value=(Path("/tmp/project"), {"bubble": "demo"}, sync)), \
                patch.object(scientist_cli, "_find_agent", return_value=agent), \
                patch.object(scientist_cli, "resync_command") as resync, redirect_stdout(out):
            scientist_cli.agent_revive_command(Path("/tmp/project"), "Ada")
        resync.assert_called_once_with(Path("/tmp/project"))
        self.assertIn("personality and conversation were preserved", out.getvalue())


if __name__ == "__main__":
    unittest.main()
