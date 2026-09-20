from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tests import live_review_gate


class LiveReviewGateHarnessTests(unittest.TestCase):
    def test_child_processes_cannot_read_or_reconfigure_the_calling_terminal(self):
        completed = subprocess.CompletedProcess(["tool"], 0, "", "")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                live_review_gate.subprocess, "run", return_value=completed) as mocked:
            live_review_gate.run(["tool"], cwd=Path(tmp))
        self.assertEqual(mocked.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_stress_harness_always_disables_terminal_mouse_tracking_on_exit(self):
        script = (Path(live_review_gate.__file__).parent / "stress-test-agents.sh").read_text(encoding="utf-8")
        self.assertIn("trap restore_terminal_mouse EXIT INT TERM", script)
        for mode in ("1000", "1002", "1003", "1006"):
            self.assertIn(f"[?{mode}l", script)

    def test_stress_harness_keeps_static_test_projects_outside_the_checkout(self):
        script = (Path(live_review_gate.__file__).parent / "stress-test-agents.sh").read_text(encoding="utf-8")
        self.assertNotIn('export TMPDIR="$runtime_tmp"', script)
        self.assertIn("env -u TMPDIR uv run python -m unittest", script)
        self.assertIn('TMPDIR="$runtime_tmp" uv run python tests/live_review_gate.py', script)



    def test_paid_calls_require_an_explicit_flag(self):
        completed = subprocess.run(
            [sys.executable, str(Path(live_review_gate.__file__))],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("without --paid", completed.stderr)

    def test_fixture_keeps_the_linked_worktree_binding_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            main, worktree, nested, sentinel = live_review_gate.fixture(Path(tmp))
            self.assertTrue(str(nested).startswith(str(worktree)))
            self.assertIn("WRONG-MAIN-CHECKOUT", sentinel.read_text(encoding="utf-8"))
            binding = worktree / ".lockedin/config/binding.json"
            self.assertIn("DISPOSABLE-WORKTREE", binding.read_text(encoding="utf-8"))
            self.assertTrue((worktree / ".lockedin/SKILL.md").is_file())
            self.assertNotEqual(live_review_gate.digest(sentinel), live_review_gate.digest(binding))

    def test_parsers_capture_final_replies_and_usage(self):
        codex = "\n".join((
            '{"type":"item.completed","item":{"type":"agent_message","text":"Done."}}',
            '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":7,"output_tokens":2}}',
        ))
        self.assertEqual(live_review_gate.parse_codex(codex), ("Done.", 10, 2, 7))
        agy = '{"result":"Which one?","usage":{"inputTokens":12,"outputTokens":3,"cacheReadTokens":8}}'
        self.assertEqual(live_review_gate.parse_agy(agy), ("Which one?", 12, 3, 8))
        claude = '{"result":"Changed it.","usage":{"input_tokens":14,"output_tokens":4,"cache_read_input_tokens":9}}'
        self.assertEqual(live_review_gate.parse_claude(claude), ("Changed it.", 14, 4, 9))
        opencode = "\n".join((
            '{"type":"text","part":{"text":"Synced."}}',
            '{"type":"step_finish","part":{"tokens":{"input":16,"output":5,"cache":{"read":11}}}}',
        ))
        self.assertEqual(live_review_gate.parse_opencode(opencode), ("Synced.", 16, 5, 11))


    def test_positional_and_flag_provider_subsets_match(self):
        for provider in ("codex", "claude", "agy", "opencode"):
            positional = live_review_gate.parse_args(["--paid", provider])
            flagged = live_review_gate.parse_args(["--paid", "--provider", provider])
            self.assertEqual(positional.provider, provider)
            self.assertEqual(flagged.provider, provider)


    def test_claude_probe_is_cheap_bounded_and_nonpersistent(self):
        args = live_review_gate.parse_args(["--paid", "claude"])
        response = '{"result":"Done.","usage":{"input_tokens":10,"output_tokens":2}}'
        completed = subprocess.CompletedProcess(["claude"], 0, response, "")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                live_review_gate, "run", return_value=completed) as mocked:
            live_review_gate.invoke("claude", "exact-edit", "prompt", Path(tmp), args)
        command = mocked.call_args.args[0]
        for expected in ("--model", "haiku", "--effort", "low", "--max-budget-usd",
                         "0.1", "--no-session-persistence", "--add-dir"):
            self.assertIn(expected, command)


    def test_opencode_probe_uses_the_free_model_json_and_disposable_auto_approval(self):
        args = live_review_gate.parse_args(["--paid", "opencode"])
        response = "\n".join((
            '{"type":"text","part":{"text":"Done."}}',
            '{"type":"step_finish","part":{"tokens":{"input":10,"output":2,"cache":{"read":1}}}}',
        ))
        completed = subprocess.CompletedProcess(["opencode"], 0, response, "")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                live_review_gate, "provider_binary", return_value="/usr/bin/opencode"), mock.patch.object(
                live_review_gate, "run", return_value=completed) as mocked:
            nested = Path(tmp) / "worktree" / "nested" / "session"
            nested.mkdir(parents=True)
            live_review_gate.invoke("opencode", "exact-edit", "prompt", nested, args)
        command = mocked.call_args.args[0]
        for expected in ("run", "--format", "json", "--model", args.opencode_model, "--auto"):
            self.assertIn(expected, command)

if __name__ == "__main__":
    unittest.main()
