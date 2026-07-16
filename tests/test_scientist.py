"""Regression tests for the installed, deterministic Scientist client and sync boundary."""
from __future__ import annotations

import base64
import io
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lockedin import bubbles, paths, reports, scientist_cli, scientist_sync, server


@contextmanager
def temp_data_home():
    old_data = os.environ.get("XDG_DATA_HOME")
    old_cache = os.environ.get("XDG_CACHE_HOME")
    with tempfile.TemporaryDirectory() as directory:
        os.environ["XDG_DATA_HOME"] = directory
        os.environ["XDG_CACHE_HOME"] = str(Path(directory) / "cache")
        try:
            yield Path(directory)
        finally:
            if old_data is None:
                os.environ.pop("XDG_DATA_HOME", None)
            else:
                os.environ["XDG_DATA_HOME"] = old_data
            if old_cache is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_cache


@contextmanager
def workspace():
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        for name in ("ASSETS", "REPORTS", "config"):
            (home / name).mkdir(parents=True)
        with paths.use_root(home):
            slug = bubbles.create_bubble("Current Work")
            bubbles.ensure_pages(slug)
        yield home, slug


class ScientistGuideTest(unittest.TestCase):
    def test_help_has_a_self_contained_scientist_tab(self):
        section = reports.guide_section("Scientist CLI")
        self.assertIn("lockedin-scientist login --server", section)
        self.assertIn("lockedin-scientist bubbles", section)
        self.assertIn("lockedin-scientist sync", section)
        self.assertIn("lockedin-scientist sync --from-server", section)
        self.assertIn("lockedin-scientist uninstall --purge-data --yes", section)
        self.assertIn("lockedin-scientist agy", section)
        self.assertIn("curl -fsSL", section)
        self.assertIn("normal\ninteractive approval", section)
        self.assertIn("REPORTS/<bubble-slug>/assets/", section)
        self.assertIn("my-figure.gif", section)

    def test_editguide_remains_the_canonical_editing_section(self):
        self.assertEqual(reports.guide_section("Missing"), "")
        guide = reports.guide_section("Editing Guide")
        self.assertIn("## The editor", guide)
        self.assertIn("centered-text", guide)

    def test_windows_installer_persists_and_refreshes_path(self):
        installer = (Path(__file__).resolve().parents[1] / "install.ps1").read_text()
        self.assertIn("SetEnvironmentVariable('Path'", installer)
        self.assertIn('$env:Path = "$bin;$env:Path"', installer)
        self.assertIn("Get-Command py", installer)
        self.assertIn("$env:PYTHON", installer)
        self.assertIn("[guid]::NewGuid()", installer)
        self.assertIn("Move-Item -Force -Path $clientTemp -Destination $client", installer)
        self.assertNotIn("setx ", installer.lower())


class SafeSyncBoundaryTest(unittest.TestCase):
    def test_scientist_registers_new_page_and_filename_title(self):
        with workspace() as (home, slug):
            result = scientist_sync.register_page(home, slug, "methods-and-results",
                base64.b64encode(b"# Different heading\n").decode(), scientist_sync.revision(b""))
            with paths.use_root(home):
                pages = bubbles.list_pages(slug)
                content = bubbles.get_page(slug, "methods-and-results")
        self.assertEqual([item["path"] for item in result["applied"]],
                         [f"REPORTS/{slug}/pages/methods-and-results.md"])
        self.assertIn({"page_slug": "methods-and-results", "title": "methods and results"}, pages)
        self.assertEqual(content, "# Different heading\n")
        self.assertEqual(base64.b64decode(result["applied"][0]["content_b64"]), b"# Different heading\n")

    def test_scientist_manifest_registers_existing_orphan_page(self):
        with workspace() as (home, slug):
            orphan = home / "REPORTS" / slug / "pages" / "agent-notes.md"
            orphan.write_text("# Notes\n")
            scientist_sync.manifest(home)
            with paths.use_root(home):
                pages = bubbles.list_pages(slug)
        self.assertIn({"page_slug": "agent-notes", "title": "agent notes"}, pages)

    def test_generic_scientist_push_cannot_create_orphan_page(self):
        with workspace() as (home, slug):
            rel = f"REPORTS/{slug}/pages/orphan.md"
            result = scientist_sync.apply_writes(home, [{
                "path": rel, "base_revision": scientist_sync.revision(b""),
                "content_b64": base64.b64encode(b"# Orphan\n").decode(),
            }])
        self.assertEqual(result["applied"], [])
        self.assertIn("read-only", result["conflicts"][0]["reason"])

    def test_scientist_page_registration_rejects_existing_page(self):
        with workspace() as (home, slug):
            result = scientist_sync.register_page(home, slug, "overview",
                base64.b64encode(b"# Replacement\n").decode(), scientist_sync.revision(b""))
        self.assertEqual(result["applied"], [])
        self.assertEqual(result["conflicts"][0]["reason"], "stale revision")

    def test_manifest_excludes_sensitive_and_large_workspace_content(self):
        with workspace() as (home, slug):
            (home / "REPORTS" / slug / "pages" / "note.md").write_text("note")
            report_assets = home / "REPORTS" / slug / "assets"
            report_assets.mkdir(parents=True)
            (report_assets / "plot.png").write_bytes(b"image")
            asset = home / "ASSETS" / "paper"
            asset.mkdir(parents=True)
            (asset / "meta.yaml").write_text("title: paper")
            (asset / "paper.pdf").write_bytes(b"private-pdf")
            chats = home / "REPORTS" / slug / "chats"
            chats.mkdir(parents=True)
            (chats / "thread.json").write_text("private chat")
            (home / "credentials.json").write_text("secret")
            names = {item["path"] for item in scientist_sync.manifest(home)["files"]}
        self.assertIn(f"REPORTS/{slug}/pages/note.md", names)
        self.assertIn("ASSETS/paper/meta.yaml", names)
        self.assertNotIn("ASSETS/paper/paper.pdf", names)
        self.assertNotIn(f"REPORTS/{slug}/chats/thread.json", names)
        self.assertNotIn("credentials.json", names)

    def test_requested_files_are_deduplicated_and_unsafe_paths_are_ignored(self):
        with workspace() as (home, slug):
            rel = f"REPORTS/{slug}/pages/note.md"
            (home / rel).write_text("note")
            result = scientist_sync.read_files(home, [rel, rel, "../../credentials.json"])
        self.assertEqual([item["path"] for item in result["files"]], [rel])
        self.assertEqual(base64.b64decode(result["files"][0]["content_b64"]), b"note")

    def test_only_existing_approved_bubble_pages_and_assets_are_writable(self):
        with workspace() as (home, slug):
            rel = f"REPORTS/{slug}/pages/overview.md"
            outcome = scientist_sync.apply_writes(home, [{
                "path": rel, "base_revision": scientist_sync.revision((home / rel).read_bytes()),
                "content_b64": base64.b64encode(b"updated").decode(),
            }])
            self.assertEqual([item["path"] for item in outcome["applied"]], [rel])
            self.assertEqual((home / rel).read_bytes(), b"updated")
            rel = f"REPORTS/{slug}/assets/plot.png"
            outcome = scientist_sync.apply_writes(home, [{
                "path": rel, "base_revision": scientist_sync.revision(b""),
                "content_b64": base64.b64encode(b"new").decode(),
            }])
            self.assertEqual([item["path"] for item in outcome["applied"]], [rel])
            self.assertEqual((home / rel).read_bytes(), b"new")
            rejected = scientist_sync.apply_writes(home, [{
                "path": "todos.yaml", "base_revision": scientist_sync.revision(b""),
                "content_b64": base64.b64encode(b"bad").decode(),
            }])
        self.assertEqual(rejected["applied"], [])
        self.assertEqual(rejected["conflicts"][0]["reason"], "read-only or invalid scientist path")

    def test_animated_gif_is_synchronized_as_a_report_asset(self):
        with workspace() as (home, slug):
            rel = f"REPORTS/{slug}/assets/convergence.gif"
            gif = b"GIF89a\\x01\\x00\\x01\\x00"
            result = scientist_sync.apply_writes(home, [{
                "path": rel, "base_revision": scientist_sync.revision(b""),
                "content_b64": base64.b64encode(gif).decode(),
            }])
            self.assertIn(b"NETSCAPE2.0", (home / rel).read_bytes())
        self.assertEqual([item["path"] for item in result["applied"]], [rel])

    def test_scientist_adds_loop_metadata_to_a_single_play_gif(self):
        # Header + logical screen descriptor; sufficient to test lossless GIF metadata handling.
        single_play = b"GIF89a\x01\x00\x01\x00\x00\x00\x00" + b";"
        looping = bubbles.ensure_looping_gif(single_play)
        self.assertIn(b"NETSCAPE2.0", looping)
        self.assertTrue(looping.endswith(b";"))
        self.assertEqual(bubbles.ensure_looping_gif(looping), looping)

    def test_unapproved_bubble_and_path_traversal_are_rejected(self):
        with workspace() as (home, slug):
            with paths.use_root(home):
                bubbles.propose_bubble("Draft")
            writes = [
                {"path": "REPORTS/draft/pages/x.md", "base_revision": scientist_sync.revision(b""), "content_b64": ""},
                {"path": f"REPORTS/{slug}/pages/../../config/keys.md", "base_revision": scientist_sync.revision(b""), "content_b64": ""},
                {"path": f"REPORTS/{slug}/assets/nested/figure.gif", "base_revision": scientist_sync.revision(b""), "content_b64": ""},
            ]
            result = scientist_sync.apply_writes(home, writes)
        self.assertEqual(len(result["conflicts"]), 3)
        self.assertTrue(all("read-only" in item["reason"] for item in result["conflicts"]))

    def test_stale_write_returns_current_content_without_overwriting_web_edit(self):
        with workspace() as (home, slug):
            rel = f"REPORTS/{slug}/pages/overview.md"
            (home / rel).write_bytes(b"website version")
            result = scientist_sync.apply_writes(home, [{
                "path": rel, "base_revision": scientist_sync.revision(b"old"),
                "content_b64": base64.b64encode(b"local version").decode(),
            }])
            current = (home / rel).read_bytes()
        self.assertEqual(current, b"website version")
        self.assertEqual(base64.b64decode(result["conflicts"][0]["content_b64"]), b"website version")


class ScientistClientTest(unittest.TestCase):
    def test_terminal_colours_can_be_disabled_or_forced(self):
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertEqual(scientist_cli._colour("plain", "31"), "plain")
        with patch.dict(os.environ, {"NO_COLOR": "", "FORCE_COLOR": "1"}, clear=False):
            self.assertIn("\033[31m", scientist_cli._colour("red", "31"))

    def test_uninstall_removes_client_but_preserves_data_by_default(self):
        with temp_data_home() as root, tempfile.TemporaryDirectory() as bin_dir:
            client = scientist_cli.client_install_path()
            client.parent.mkdir(parents=True)
            client.write_text("standalone client")
            (root / "data" / "users" / "alice").mkdir(parents=True)
            (root / "accounts.json").write_text("{}")
            for name in ("lockedin-scientist", "lockedin_scientist"):
                (Path(bin_dir) / name).write_text("wrapper")
            with patch.object(scientist_cli, "__file__", str(client)), \
                 patch.object(scientist_cli, "command_bin_dir", return_value=Path(bin_dir)):
                scientist_cli.uninstall(purge_data=False, assume_yes=True)
            self.assertFalse(client.parent.exists())
            self.assertFalse((Path(bin_dir) / "lockedin-scientist").exists())
            self.assertTrue((root / "data" / "users" / "alice").exists())
            self.assertTrue((root / "accounts.json").exists())

    def test_uninstall_purge_removes_all_client_data(self):
        with temp_data_home() as root, tempfile.TemporaryDirectory() as bin_dir:
            client = scientist_cli.client_install_path()
            client.parent.mkdir(parents=True)
            client.write_text("standalone client")
            (root / "data").mkdir()
            with patch.object(scientist_cli, "__file__", str(client)), \
                 patch.object(scientist_cli, "command_bin_dir", return_value=Path(bin_dir)):
                scientist_cli.uninstall(purge_data=True, assume_yes=True)
            self.assertFalse((root / "lockedin-scientist").exists())

    def test_same_user_gets_a_stable_mirror_across_reauthorization(self):
        with temp_data_home():
            first = scientist_cli.Mirror({"server": "https://one.example", "user": "alice", "token": "one"})
            second = scientist_cli.Mirror({"server": "https://other.example", "user": "alice", "token": "two"})
        self.assertEqual(first.root, second.root)
        self.assertEqual(first.root.parts[-3:], ("data", "users", "alice"))

    def test_conflict_bases_live_outside_the_mirrored_workspace(self):
        with temp_data_home():
            mirror = scientist_cli.Mirror({"server": "https://example.test", "user": "alice", "token": "t"})
            # Simulate a protected/malformed preferred runtime directory. The client must
            # transparently move conflict bookkeeping to its cache fallback.
            mirror.base_dir.parent.mkdir(parents=True)
            mirror.base_dir.write_text("not a directory")
            name = mirror.save_base("REPORTS/work/pages/overview.md", b"base")
            self.assertEqual((mirror.base_dir / name).read_bytes(), b"base")
            self.assertEqual(mirror.base_dir, mirror.fallback_base_dir)
            self.assertEqual(mirror.base_raw({"base_file": name}, "ignored"), b"base")

    def test_unwritable_sidecar_is_nonfatal_and_keeps_state_in_memory(self):
        with temp_data_home():
            mirror = scientist_cli.Mirror({"server": "https://example.test", "user": "alice", "token": "t"})
            for path in (mirror.state_path, mirror.fallback_state_path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.parent.chmod(0o500)
            try:
                state = {"files": {"REPORTS/work/pages/overview.md": {"revision": "abc"}}}
                mirror.save_state(state)
                self.assertEqual(mirror.state(), state)
            finally:
                for path in (mirror.state_path, mirror.fallback_state_path):
                    path.parent.chmod(0o700)

    def test_server_recovery_archives_local_files_before_resetting_sync_state(self):
        with temp_data_home():
            mirror = scientist_cli.Mirror({"server": "https://example.test", "user": "alice", "token": "t"})
            local = mirror.root / "REPORTS" / "work" / "pages" / "overview.md"
            local.parent.mkdir(parents=True)
            local.write_text("local draft")
            with patch.object(mirror, "sync") as sync:
                backup = mirror.recover_from_server()
            self.assertEqual((backup / "REPORTS" / "work" / "pages" / "overview.md").read_text(), "local draft")
            sync.assert_called_once()

    def test_legacy_url_hashed_mirror_is_migrated_once(self):
        with temp_data_home():
            account = {"server": "https://example.test", "user": "alice", "token": "t"}
            key = scientist_cli.urllib.parse.quote(account["server"], safe="")
            old = scientist_cli.data_root() / "servers" / key / "data" / "users" / "alice"
            old.mkdir(parents=True)
            (old / "marker").write_text("keep")
            mirror = scientist_cli.Mirror(account)
            self.assertEqual((mirror.root / "marker").read_text(), "keep")
            self.assertFalse(old.exists())

    def test_bubbles_command_is_deterministic_and_does_not_sync(self):
        account = {"server": "https://example.test", "user": "alice", "token": "t"}
        original_argv = list(scientist_cli.sys.argv)
        try:
            with temp_data_home(), patch.object(scientist_cli, "choose_account", return_value=account), \
                 patch.object(scientist_cli.Mirror, "sync", side_effect=AssertionError("must not sync")), \
                 patch.object(scientist_cli, "request", return_value={"bubbles": [
                     {"slug": "older", "name": "Older", "last_edited_at": "2026-01-01T00:00:00+00:00"},
                     {"slug": "newer", "name": "Newer", "last_edited_at": "2026-02-01T00:00:00+00:00"},
                     {"slug": "a-tie", "name": "A tie", "last_edited_at": "2026-01-01T00:00:00+00:00"},
                 ]}):
                scientist_cli.sys.argv = ["lockedin-scientist", "bubbles"]
                output = io.StringIO()
                with redirect_stdout(output):
                    scientist_cli.main()
        finally:
            scientist_cli.sys.argv = original_argv
        text = output.getvalue()
        self.assertLess(text.index("Newer"), text.index("Older"))
        self.assertLess(text.index("A tie"), text.index("Older"))
        self.assertIn("slug  newer", text)
        self.assertIn("<bubble-slug>", text)

    def test_scientist_bubble_endpoint_keeps_website_recency_metadata(self):
        source = Path(server.__file__).read_text()
        self.assertIn('"last_edited_at": b.get("last_edited_at") or ""', source)

    def test_no_argument_invocation_shows_a_welcome_without_an_account(self):
        original_argv = list(scientist_cli.sys.argv)
        try:
            with temp_data_home():
                scientist_cli.sys.argv = ["lockedin-scientist"]
                output = io.StringIO()
                with redirect_stdout(output):
                    scientist_cli.main()
        finally:
            scientist_cli.sys.argv = original_argv
        self.assertIn("LockedIn Scientist", output.getvalue())
        self.assertIn("Get started", output.getvalue())
        self.assertIn("lockedin-scientist bubbles", output.getvalue())
        self.assertIn("lockedin-scientist sync", output.getvalue())
        self.assertIn("lockedin-scientist claude", output.getvalue())
        self.assertIn("lockedin-scientist agy", output.getvalue())
        self.assertIn("lockedin-scientist sync --from-server", output.getvalue())
        self.assertIn("lockedin-scientist uninstall", output.getvalue())
        self.assertIn("lockedin-scientist uninstall --purge-data --yes", output.getvalue())

    def test_help_describes_all_models_and_sync_without_an_account(self):
        original_argv = list(scientist_cli.sys.argv)
        try:
            scientist_cli.sys.argv = ["lockedin-scientist", "--help"]
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaises(SystemExit) as exited:
                scientist_cli.main()
        finally:
            scientist_cli.sys.argv = original_argv
        self.assertEqual(exited.exception.code, 0)
        text = output.getvalue()
        self.assertIn("Pull/push once", text)
        self.assertIn("lockedin-scientist codex", text)
        self.assertIn("lockedin-scientist claude", text)
        self.assertIn("lockedin-scientist agy", text)
        self.assertIn("sync --from-server", text)
        self.assertIn("uninstall --purge-data --yes", text)

    def test_unknown_slug_fails_before_a_vendor_cli_is_started(self):
        with temp_data_home():
            mirror = scientist_cli.Mirror({"server": "https://example.test", "user": "alice", "token": "t"})
            with patch.object(scientist_cli, "request", return_value={"bubbles": [{"slug": "real", "name": "Real"}]}), \
                 patch.object(scientist_cli.subprocess, "run", side_effect=AssertionError("must not run")):
                with self.assertRaisesRegex(RuntimeError, "No approved bubble.*missing.*real"):
                    scientist_cli.run_agent("codex", mirror, "missing")

    def test_role_uses_the_bubble_specific_paper_inventory(self):
        with temp_data_home():
            mirror = scientist_cli.Mirror({"server": "https://example.test", "user": "alice", "token": "t"})
            inventory = mirror.root / "REPORTS" / "work" / "_lockedin_papers.md"
            inventory.parent.mkdir(parents=True)
            inventory.write_text("# Attached papers\n- Exact current paper")
            prompt = scientist_cli.role(mirror, "work")
        self.assertIn("Exact current paper", prompt)
        self.assertIn("never reuse an earlier answer", prompt)
        self.assertIn("normal terminal conversation", prompt)
        self.assertIn("REPORTS/work/assets", prompt)
        self.assertIn("/api/bubbles/work/assets/filename.gif", prompt)

    def test_sync_pushes_a_new_local_gif_not_yet_in_the_server_manifest(self):
        with temp_data_home():
            mirror = scientist_cli.Mirror({"server": "https://example.test", "user": "alice", "token": "t"})
            rel = "REPORTS/work/assets/new-animation.gif"
            local = mirror.root / rel
            local.parent.mkdir(parents=True)
            local.write_bytes(b"GIF89a-new")
            remote = {}
            pushed = []
            requested_files = []

            def fake_request(_server, _method, endpoint, payload=None, token=None):
                if endpoint.endswith("/manifest"):
                    return {"files": [{"path": path, "revision": scientist_cli.Mirror.rev(raw)}
                                      for path, raw in remote.items()]}
                if endpoint.endswith("/push"):
                    applied = []
                    for item in payload["writes"]:
                        raw = base64.b64decode(item["content_b64"])
                        remote[item["path"]] = raw
                        pushed.append(item)
                        applied.append({"path": item["path"], "revision": scientist_cli.Mirror.rev(raw)})
                    return {"applied": applied, "conflicts": []}
                if endpoint.endswith("/files"):
                    requested_files.extend(payload["paths"])
                    return {"files": [{"path": path, "revision": scientist_cli.Mirror.rev(remote[path]),
                                       "content_b64": base64.b64encode(remote[path]).decode()}
                                      for path in payload["paths"]]}
                raise AssertionError(endpoint)

            with patch.object(scientist_cli, "request", side_effect=fake_request):
                mirror.sync()
            self.assertEqual([item["path"] for item in pushed], [rel])
            self.assertEqual(remote[rel], b"GIF89a-new")
            self.assertEqual(requested_files, [])


if __name__ == "__main__":
    unittest.main()
