"""Contract tests for the vendor boundary.

These tests intentionally import the adapters directly.  A vendor CLI release should be
handled here without changing the worker, queue, confinement, or server code.
"""
from __future__ import annotations

import inspect
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lockedin import agent_vendors, agents, scientist_cli


class VendorRegistryContractTests(unittest.TestCase):
    def test_every_supported_vendor_implements_the_same_worker_contract(self):
        self.assertEqual(agent_vendors.names(), scientist_cli.VENDORS)
        self.assertEqual(agent_vendors.names(), agents.VENDORS)
        for name in agent_vendors.names():
            adapter = agent_vendors.get(name)
            self.assertEqual(adapter.name, name)
            self.assertTrue(callable(adapter.turn_command))
            self.assertTrue(callable(adapter.chat_command))
            self.assertTrue(callable(adapter.conversation_exists))
            self.assertTrue(callable(adapter.discover_conversation))
            self.assertTrue(callable(adapter.purge_conversation))
            self.assertTrue(adapter.state_roots)

    def test_worker_module_contains_no_vendor_cli_flags_or_store_layouts(self):
        source = inspect.getsource(scientist_cli)
        for vendor_detail in (
            "--dangerously-skip-permissions",
            "--permission-mode",
            "sandbox_workspace_write.network_access",
            "conversation_summaries.db",
            "session_index.jsonl",
        ):
            self.assertNotIn(vendor_detail, source)

    def test_unknown_vendor_fails_at_the_boundary(self):
        with self.assertRaisesRegex(RuntimeError, "unknown vendor"):
            agent_vendors.get("carrier-pigeon")

    def test_only_claude_preassigns_a_conversation_id(self):
        preassigned = {name for name in agent_vendors.names()
                       if agent_vendors.get(name).preassigns_conversation_id}
        self.assertEqual(preassigned, {"claude"})

    def test_installed_two_file_client_runs_without_the_locked_in_package(self):
        source = Path(scientist_cli.__file__).resolve()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            shutil.copy2(source, target / "scientist_cli.py")
            shutil.copy2(Path(agent_vendors.__file__).resolve(), target / "agent_vendors.py")
            result = subprocess.run([sys.executable, str(target / "scientist_cli.py"), "--version"],
                                    capture_output=True, text=True, cwd=target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(scientist_cli.SCIENTIST_CLIENT_VERSION, result.stdout)


class SeamlessUpgradeTests(unittest.TestCase):
    def test_unix_installer_finishes_with_worker_repair(self):
        installer = Path(__file__).resolve().parents[1] / "install.sh"
        source = installer.read_text()
        self.assertIn('"$bin/lockedin-scientist" upgrade-workers', source)
        self.assertIn('cmp -s "$client_tmp" "$root/scientist_cli.py"', source)
        self.assertIn("running workers and vendor integrations were left untouched", source)

    def test_running_the_real_unix_installer_twice_makes_the_second_run_a_noop(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            curl = fake_bin / "curl"
            curl.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                f"repo = pathlib.Path({str(repository)!r})\n"
                "args = sys.argv[1:]\n"
                "url = next(arg for arg in args if arg.startswith('http'))\n"
                "if '/commits/' in url:\n"
                "    data = b'{\\n  \"sha\": \"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"\\n}'\n"
                "elif url.endswith('/scientist_cli.py'):\n"
                "    data = (repo / 'src/lockedin/scientist_cli.py').read_bytes()\n"
                "elif url.endswith('/agent_vendors.py'):\n"
                "    data = (repo / 'src/lockedin/agent_vendors.py').read_bytes()\n"
                "else:\n"
                "    raise SystemExit('unexpected URL: ' + url)\n"
                "if '-o' in args:\n"
                "    pathlib.Path(args[args.index('-o') + 1]).write_bytes(data)\n"
                "else:\n"
                "    sys.stdout.buffer.write(data)\n"
            )
            curl.chmod(0o755)
            env = {**os.environ, "HOME": str(root / "home"), "PATH": f"{fake_bin}:/usr/bin:/bin",
                   "PYTHON": sys.executable}
            first = subprocess.run(["bash", str(repository / "install.sh")], env=env,
                                   capture_output=True, text=True)
            second = subprocess.run(["bash", str(repository / "install.sh")], env=env,
                                    capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("running workers and vendor integrations were left untouched", second.stdout)

    def test_upgrade_restarts_active_workers_but_never_deliberately_stopped_ones(self):
        workers = {"workers": {
            "active": {"status": "running", "pid": 0, "project": "/active", "jobs": []},
            "secure-stop": {"status": "stopped", "pid": 0, "project": "/stopped", "jobs": []},
        }}
        with patch.object(scientist_cli, "load_workers", return_value=workers), \
                patch.object(scientist_cli, "_install_detected_skills", return_value=[]), \
                patch.object(scientist_cli, "_restart_upgraded_worker", return_value="restarted") as restart:
            scientist_cli.upgrade_workers_command()
        restart.assert_called_once_with("active")

    def test_reinstall_is_a_noop_for_workers_already_on_this_exact_version(self):
        workers = {"workers": {"current": {
            "status": "running", "client_version": scientist_cli.SCIENTIST_CLIENT_VERSION,
            "pid": 42, "project": "/current", "jobs": ["j-still-running"],
        }}}
        with patch.object(scientist_cli, "load_workers", return_value=workers), \
                patch.object(scientist_cli, "_install_detected_skills", return_value=[]), \
                patch.object(scientist_cli, "_restart_upgraded_worker") as restart, \
                patch.object(scientist_cli.subprocess, "Popen") as popen:
            scientist_cli.upgrade_workers_command()
        restart.assert_not_called()
        popen.assert_not_called()

    def test_large_mixed_worker_set_only_touches_outdated_active_records(self):
        records = {}
        for number in range(100):
            records[f"current-{number}"] = {
                "status": "running", "client_version": scientist_cli.SCIENTIST_CLIENT_VERSION,
                "pid": 0, "project": f"/current/{number}", "jobs": [],
            }
            records[f"legacy-{number}"] = {
                "status": "degraded", "pid": 0, "project": f"/legacy/{number}", "jobs": [],
            }
            records[f"stopped-{number}"] = {
                "status": "stopped", "pid": 0, "project": f"/stopped/{number}", "jobs": [],
            }
        with patch.object(scientist_cli, "load_workers", return_value={"workers": records}), \
                patch.object(scientist_cli, "_install_detected_skills", return_value=[]), \
                patch.object(scientist_cli, "_restart_upgraded_worker", return_value="restarted") as restart:
            scientist_cli.upgrade_workers_command()
        self.assertEqual(restart.call_count, 100)
        self.assertEqual({call.args[0] for call in restart.call_args_list},
                         {f"legacy-{number}" for number in range(100)})

    def test_upgrade_defers_a_busy_worker_instead_of_killing_its_turn(self):
        workers = {"workers": {"busy": {
            "status": "running", "pid": 42, "project": "/busy", "jobs": ["j-1"],
        }}}
        with patch.object(scientist_cli, "load_workers", return_value=workers), \
                patch.object(scientist_cli, "_install_detected_skills", return_value=[]), \
                patch.object(scientist_cli, "_alive", return_value=True), \
                patch.object(scientist_cli.subprocess, "Popen") as popen, \
                patch.object(scientist_cli, "_restart_upgraded_worker") as restart:
            scientist_cli.upgrade_workers_command()
        restart.assert_not_called()
        self.assertIn("_upgrade-worker", popen.call_args.args[0])

    def test_deferred_restart_aborts_if_stop_agents_stopped_the_worker(self):
        with patch.object(scientist_cli, "_worker_record", return_value={"status": "stopped"}), \
                patch.object(scientist_cli, "start_sync") as start:
            result = scientist_cli._restart_upgraded_worker("secure-stop", wait_for_jobs=True)
        self.assertEqual(result, "stopped")
        start.assert_not_called()

    def test_concurrent_installers_cannot_both_enter_worker_migration(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"LOCKEDIN_SCIENTIST_HOME": directory}):
            with scientist_cli._upgrade_lock() as first:
                with scientist_cli._upgrade_lock() as second:
                    self.assertTrue(first)
                    self.assertFalse(second)


if __name__ == "__main__":
    unittest.main()
