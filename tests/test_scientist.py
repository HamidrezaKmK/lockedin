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

from lockedin import bubbles, paths, reports, scientist_cli, scientist_sync


@contextmanager
def temp_data_home():
    old = os.environ.get("XDG_DATA_HOME")
    with tempfile.TemporaryDirectory() as directory:
        os.environ["XDG_DATA_HOME"] = directory
        try:
            yield Path(directory)
        finally:
            if old is None:
                os.environ.pop("XDG_DATA_HOME", None)
            else:
                os.environ["XDG_DATA_HOME"] = old


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
        self.assertIn("curl -fsSL", section)
        self.assertIn("normal\ninteractive approval", section)

    def test_editguide_remains_the_canonical_editing_section(self):
        self.assertEqual(reports.guide_section("Missing"), "")
        self.assertIn("## The editor", reports.guide_section("Editing Guide"))


class SafeSyncBoundaryTest(unittest.TestCase):
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

    def test_only_approved_bubble_pages_and_assets_are_writable(self):
        with workspace() as (home, slug):
            for rel in (f"REPORTS/{slug}/pages/new.md", f"REPORTS/{slug}/assets/plot.png"):
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

    def test_unapproved_bubble_and_path_traversal_are_rejected(self):
        with workspace() as (home, slug):
            with paths.use_root(home):
                bubbles.propose_bubble("Draft")
            writes = [
                {"path": "REPORTS/draft/pages/x.md", "base_revision": scientist_sync.revision(b""), "content_b64": ""},
                {"path": f"REPORTS/{slug}/pages/../../config/keys.md", "base_revision": scientist_sync.revision(b""), "content_b64": ""},
            ]
            result = scientist_sync.apply_writes(home, writes)
        self.assertEqual(len(result["conflicts"]), 2)
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
    def test_same_user_gets_a_stable_mirror_across_reauthorization(self):
        with temp_data_home():
            first = scientist_cli.Mirror({"server": "https://one.example", "user": "alice", "token": "one"})
            second = scientist_cli.Mirror({"server": "https://other.example", "user": "alice", "token": "two"})
        self.assertEqual(first.root, second.root)
        self.assertEqual(first.root.parts[-3:], ("data", "users", "alice"))

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
                 patch.object(scientist_cli, "request", return_value={"bubbles": [{"slug": "work", "name": "Current Work"}]}):
                scientist_cli.sys.argv = ["lockedin-scientist", "bubbles"]
                output = io.StringIO()
                with redirect_stdout(output):
                    scientist_cli.main()
        finally:
            scientist_cli.sys.argv = original_argv
        self.assertIn("work  —  Current Work", output.getvalue())
        self.assertIn("<bubble-slug>", output.getvalue())

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


if __name__ == "__main__":
    unittest.main()
