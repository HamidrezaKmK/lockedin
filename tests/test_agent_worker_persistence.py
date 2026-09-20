"""Persistent-worker regressions exercised by ``stress-test-agents.sh``.

These model the production failure where a completed Claude edit spent long enough synchronizing
that ``await-sync`` called its live worker stale, suggested ``resync``, and the managed turn then
terminated its own parent process. Provider adapters all share this worker path, so the invariant
covers Codex, Claude, agy, and OpenCode without paid calls.
"""
from __future__ import annotations

import io
import json
import os
import signal
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lockedin import scientist_cli


ACCOUNT = {
    "server": "https://example.test",
    "user": "stress-user",
    "token": "li_sc_test",
    "workspace_id": "stress-workspace",
}


class PersistentAgentWorkerTests(unittest.TestCase):
    def _bound_project(self, directory: str) -> tuple[Path, Path]:
        project = Path(directory) / "project"
        config = project / ".lockedin" / "config"
        page = project / ".lockedin" / "reports" / "pages" / "result.md"
        config.mkdir(parents=True)
        page.parent.mkdir(parents=True)
        page.write_text("# Edited result\n")
        (config / "binding.json").write_text(json.dumps({
            "server": ACCOUNT["server"], "user": ACCOUNT["user"],
            "workspace_id": ACCOUNT["workspace_id"], "bubble": "stress-bubble",
        }))
        scientist_cli.save_config({"accounts": [dict(ACCOUNT)]})
        return project, page

    @staticmethod
    def _worker(project: Path, **changes) -> dict:
        record = {
            "id": "worker", "project": str(project.resolve()), "bubble": "stress-bubble",
            "server": ACCOUNT["server"], "user": ACCOUNT["user"],
            "workspace_id": ACCOUNT["workspace_id"], "pid": 4242,
            "status": "running", "started_at": time.time(),
            "last_sync": time.time() - scientist_cli.WORKER_STALE_SECONDS - 5,
            "last_error": "",
        }
        record.update(changes)
        return record

    def test_slow_sync_does_not_reclassify_a_live_worker_as_stopped(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"XDG_DATA_HOME": directory, "LOCKEDIN_JOB_ID": "j-stress"}), patch.object(
                scientist_cli, "_alive", return_value=True), patch.object(
                scientist_cli, "account_request", return_value={"files": []}):
            project, page = self._bound_project(directory)
            scientist_cli.save_workers({"workers": {"worker": self._worker(project)}})
            state = project / ".lockedin" / "config" / "sync-state.json"
            revision = scientist_cli.ProjectSync._rev(page.read_bytes())

            def complete_sync(_delay: float) -> None:
                state.write_text(json.dumps({"files": {
                    "reports/pages/result.md": {"revision": revision},
                }}))

            output = io.StringIO()
            with patch.object(scientist_cli.time, "sleep", side_effect=complete_sync), redirect_stdout(output):
                scientist_cli.await_sync_command(project, str(page), timeout=1)
            self.assertIn("is synchronized", output.getvalue())
            self.assertEqual(scientist_cli.load_workers()["workers"]["worker"]["status"], "running")

    def test_managed_turn_cannot_run_any_worker_lifecycle_command(self):
        calls = (
            ("stop", lambda root: scientist_cli.stop_command("worker")),
            ("sync", lambda root: scientist_cli.start_sync({}, "bubble", root)),
            ("connect", lambda root: scientist_cli.connect_command("https://x", "w", "b")),
            ("resync", lambda root: scientist_cli.resync_command(root)),
            ("upgrade-workers", lambda root: scientist_cli.upgrade_workers_command()),
            ("hard-reset", lambda root: scientist_cli.hard_reset({}, "bubble", root)),
            ("agent revive", lambda root: scientist_cli.agent_revive_command(root, "Ada")),
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"XDG_DATA_HOME": directory, "LOCKEDIN_JOB_ID": "j-stress"}), patch.object(
                scientist_cli.os, "kill") as kill, patch.object(
                scientist_cli, "_stop_and_wait") as stop:
            root = Path(directory) / "project"
            for command, invoke in calls:
                with self.subTest(command=command), self.assertRaisesRegex(RuntimeError, "managed agent job"):
                    invoke(root)
            kill.assert_not_called()
            stop.assert_not_called()

    def test_managed_await_failure_never_instructs_the_agent_to_resync(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"XDG_DATA_HOME": directory, "LOCKEDIN_JOB_ID": "j-stress"}), patch.object(
                scientist_cli, "_alive", return_value=False):
            project, page = self._bound_project(directory)
            scientist_cli.save_workers({"workers": {"worker": self._worker(
                project, status="stopped", pid=0)}})
            with self.assertRaises(RuntimeError) as caught:
                scientist_cli.await_sync_command(project, str(page), timeout=0)
            message = str(caught.exception)
            self.assertIn("Report this job as failed", message)
            self.assertNotIn("resync", message)

    def test_explicit_shell_stop_still_signals_and_marks_the_worker(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"XDG_DATA_HOME": directory, "LOCKEDIN_JOB_ID": ""}), patch.object(
                scientist_cli, "_alive", return_value=True), patch.object(
                scientist_cli.os, "kill") as kill:
            project = Path(directory) / "project"
            scientist_cli.save_workers({"workers": {"worker": self._worker(project)}})
            with redirect_stdout(io.StringIO()):
                scientist_cli.stop_command("worker")
            kill.assert_called_once_with(4242, signal.SIGTERM)
            self.assertEqual(scientist_cli.load_workers()["workers"]["worker"]["status"], "stopping")

    def test_generated_skill_forbids_self_revival_but_preserves_human_recovery(self):
        guide = scientist_cli.GUIDES["reports.md"]
        self.assertIn("LOCKEDIN_JOB_ID", guide)
        self.assertIn("never run `sync`, `resync`, `stop`", guide)
        self.assertIn("Only a human's explicit command", guide)
        self.assertIn("Outside a managed job", guide)


if __name__ == "__main__":
    unittest.main()
