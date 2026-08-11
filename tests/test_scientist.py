"""Regression tests for Scientist v2 project-local bubble synchronization."""
from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import time
import unittest
import yaml
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from lockedin import assets, bubbles, paths, scientist_cli, scientist_sync, service
from lockedin import server


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
        slug = service.create_bubble(home, "Current Work")
        service.approve_bubble(home, slug)
        yield home, slug


class FakeBubbleServer:
    def __init__(self, files: dict[str, bytes]):
        self.files = dict(files)

    def request(self, _server, method, endpoint, body=None, token="", workspace=""):
        if endpoint.endswith("/guide"):
            return {"guide": "## Markdown\n\nUse the canonical guide.\n\n## Math\n\nUse dollar delimiters.",
                    "math_macros": {"\\bmu": "\\boldsymbol{\\mu}"}}
        assert "/api/scientist/v2/bubbles/work/" in endpoint
        if endpoint.endswith("/manifest"):
            return {"files": [{"path": path, "revision": scientist_sync.revision(raw)}
                              for path, raw in sorted(self.files.items())]}
        if endpoint.endswith("/files"):
            return {"files": [{"path": path, "revision": scientist_sync.revision(self.files[path]),
                               "content_b64": base64.b64encode(self.files[path]).decode()}
                              for path in body["paths"] if path in self.files]}
        if endpoint.endswith("/push"):
            applied, conflicts = [], []
            for write in body["writes"]:
                path, current = write["path"], self.files.get(write["path"], b"")
                if write["base_revision"] != scientist_sync.revision(current):
                    conflicts.append({"path": path, "revision": scientist_sync.revision(current),
                                      "content_b64": base64.b64encode(current).decode()})
                else:
                    raw = base64.b64decode(write["content_b64"])
                    self.files[path] = raw
                    applied.append({"path": path, "revision": scientist_sync.revision(raw)})
            return {"applied": applied, "conflicts": conflicts}
        if endpoint.endswith("/deletes"):
            applied, conflicts = [], []
            for delete in body["deletes"]:
                path, current = delete["path"], self.files.get(delete["path"], b"")
                if delete["base_revision"] != scientist_sync.revision(current):
                    conflicts.append({"path": path, "revision": scientist_sync.revision(current),
                                      "content_b64": base64.b64encode(current).decode()})
                else:
                    self.files.pop(path, None); applied.append({"path": path})
            return {"applied": applied, "conflicts": conflicts}
        if endpoint.endswith("/pages"):
            path = "reports/pages/" + body["page_slug"] + ".md"
            if path in self.files:
                return {"applied": [], "conflicts": [{"path": path, "revision": scientist_sync.revision(self.files[path]),
                                                          "content_b64": base64.b64encode(self.files[path]).decode()}]}
            raw = base64.b64decode(body["content_b64"]); self.files[path] = raw
            return {"applied": [{"path": path, "revision": scientist_sync.revision(raw)}], "conflicts": []}
        raise AssertionError((method, endpoint))


ACCOUNT = {"server": "https://example.test", "user": "alice", "token": "token", "workspace_id": "personal"}


class ScientistServerBoundaryTest(unittest.TestCase):
    def test_retired_v1_paths_return_an_actionable_upgrade(self):
        with TestClient(server.build_app()) as client:
            response = client.get("/api/scientist/v1/manifest")
        self.assertEqual(response.status_code, 426)
        self.assertIn("Reinstall the newest version", response.json()["detail"])
        self.assertIn("/lockedin/main/install.sh", response.json()["detail"])

    def test_v2_login_and_sync_both_reject_an_outdated_client(self):
        with TestClient(server.build_app()) as client:
            login = client.post("/api/scientist/v2/device", json={"client_name": "old"})
            sync = client.get("/api/scientist/v2/bubbles")
        for response in (login, sync):
            self.assertEqual(response.status_code, 426)
            self.assertIn("/lockedin/main/install.sh", response.json()["detail"])

    def test_overleaf_field_normalizes_and_is_exported_only_when_assigned(self):
        with workspace() as (home, slug):
            with paths.use_root(home):
                self.assertIsNone(bubbles.bubble_detail(slug)["overleaf_project_id"])
                self.assertNotIn("config/overleaf.yaml", {f["path"] for f in scientist_sync.manifest(home, slug)["files"]})
                bubbles.set_overleaf_project(slug, "https://git@git.overleaf.com/abcDEF123")
                detail = bubbles.bubble_detail(slug)
                self.assertEqual(detail["overleaf_project_id"], "abcDEF123")
                self.assertEqual(detail["overleaf_url"], "https://www.overleaf.com/project/abcDEF123")
                self.assertIn("config/overleaf.yaml", {f["path"] for f in scientist_sync.manifest(home, slug)["files"]})
                with self.assertRaises(ValueError):
                    bubbles.set_overleaf_project(slug, "https://example.test/project/abcDEF123")

    def test_overleaf_field_migration_is_idempotent(self):
        with workspace() as (home, slug):
            with paths.use_root(home):
                reg = bubbles.load_registry(); reg[slug].pop("overleaf_project_id", None); bubbles.save_registry(reg)
                self.assertEqual(bubbles.migrate_overleaf_fields(), 1)
                self.assertEqual(bubbles.migrate_overleaf_fields(), 0)
                self.assertIsNone(bubbles.load_registry()[slug]["overleaf_project_id"])

    def test_manifest_is_one_bubble_with_only_attached_papers(self):
        with workspace() as (home, slug):
            other = service.create_bubble(home, "Other")
            service.approve_bubble(home, other)
            attached = service.save_asset(home, b"%PDF attached", "attached.pdf")
            detached = service.save_asset(home, b"%PDF detached", "detached.pdf")
            with paths.use_root(home):
                assets.save_summary(attached, "attached summary")
                assets.save_summary(detached, "detached summary")
                bubbles.add_pdf_to_bubble(slug, attached)
                report = paths.bubble_dir(slug)
                (report / "chats").mkdir(); (report / "chats" / "private.md").write_text("private")
                names = {item["path"] for item in scientist_sync.manifest(home, slug)["files"]}
        self.assertIn(f"assets/{attached}/paper.pdf", names)
        self.assertIn(f"assets/{attached}/summary.md", names)
        self.assertNotIn(f"assets/{detached}/paper.pdf", names)
        self.assertNotIn("reports/chats/private.md", names)

    def test_open_review_threads_are_exported_as_read_only_feedback_context(self):
        with workspace() as (home, slug):
            with paths.use_root(home):
                thread = bubbles.create_comment(
                    slug, "overview", "reviewer", "Clarify this claim.",
                    {"quote": "Current claim", "start": 12, "prefix": "Before ", "suffix": " after"})
                bubbles.reply_comment(slug, "overview", thread["id"], "author", "Will revise it.")
                names = {item["path"] for item in scientist_sync.manifest(home, slug)["files"]}
                self.assertIn("config/reviews.yaml", names)
                self.assertNotIn("reports/comments/overview.json", names)
                payload = scientist_sync.read_files(home, slug, ["config/reviews.yaml"])["files"][0]
                review = yaml.safe_load(base64.b64decode(payload["content_b64"]))
                self.assertEqual(review["threads"][0]["page_slug"], "overview")
                self.assertEqual(review["threads"][0]["anchor"]["quote"], "Current claim")
                self.assertEqual(review["threads"][0]["marker"],
                                 bubbles.comment_marker(thread["id"]))
                self.assertEqual([m["body"] for m in review["threads"][0]["messages"]],
                                 ["Clarify this claim.", "Will revise it."])
                bubbles.set_comment_status(slug, "overview", thread["id"], "resolved", "author")
                names = {item["path"] for item in scientist_sync.manifest(home, slug)["files"]}
        self.assertNotIn("config/reviews.yaml", names)

    def test_only_report_pages_and_flat_report_assets_are_writable(self):
        with workspace() as (home, slug):
            with paths.use_root(home):
                result = scientist_sync.apply_writes(home, slug, [
                    {"path": "assets/paper/summary.md", "base_revision": scientist_sync.revision(b""), "content_b64": ""},
                    {"path": "config/math.yaml", "base_revision": scientist_sync.revision(b""), "content_b64": ""},
                    {"path": "reports/assets/nested/figure.png", "base_revision": scientist_sync.revision(b""), "content_b64": ""},
                ])
        self.assertEqual(len(result["conflicts"]), 3)

    def test_scientist_page_write_preserves_inline_review_marker(self):
        with workspace() as (home, slug):
            original = b"Before this is a highlight after."
            with paths.use_root(home):
                bubbles.ensure_pages(slug)
            service.save_page(home, slug, "overview", original.decode())
            start = original.decode().index("this is a highlight")
            with paths.use_root(home):
                thread = bubbles.create_comment(
                    slug, "overview", "reviewer", "Keep this attached.",
                    {"quote": "this is a highlight", "start": start,
                     "prefix": "Before ", "suffix": " after"})
            base = service.get_page(home, slug, "overview").encode()
            marker = bubbles.comment_marker(thread["id"])
            marked = b"Before " + marker.encode() + b"this is a highlight" + marker.encode() + b" after."
            service.save_page(home, slug, "overview", marked.decode())
            base = service.get_page(home, slug, "overview").encode()
            updated = b"Before " + marker.encode() + b"this is a NEW highlight" + marker.encode() + b" after."
            result = scientist_sync.apply_writes(home, slug, [{
                "path": "reports/pages/overview.md",
                "base_revision": scientist_sync.revision(base),
                "content_b64": base64.b64encode(updated).decode(),
            }])
            self.assertEqual(result["conflicts"], [])
            self.assertIn(marker.encode(), service.get_page(home, slug, "overview").encode())

    def test_page_creation_is_bubble_scoped(self):
        with workspace() as (home, slug):
            other = service.create_bubble(home, "Other")
            service.approve_bubble(home, other)
            raw = base64.b64encode(b"# New\n").decode()
            created = scientist_sync.register_page(home, slug, "new-page", raw, scientist_sync.revision(b""))
            self.assertTrue(created["applied"])
            self.assertFalse((paths.bubble_dir(other) / "pages" / "new-page.md").exists())


class ScientistProjectSyncTest(unittest.TestCase):
    def test_initialization_creates_required_layout_and_binding(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
                scientist_cli, "request", return_value={"guide": "## Markdown\n\nCanonical guidance."}):
            project = Path(directory); (project / ".git" / "info").mkdir(parents=True)
            sync = scientist_cli.ProjectSync(ACCOUNT, project, "work")
            sync.validate_or_initialize()
            root = project / ".lockedin"
            self.assertEqual(json.loads((root / "config" / "binding.json").read_text())["bubble"], "work")
            self.assertTrue((root / "reports" / "pages").is_dir())
            self.assertTrue((root / "reports" / "assets").is_dir())
            self.assertIn("Complete LockedIn editing guide", (root / "SKILL.md").read_text())
            self.assertFalse((root / "overleaf").exists())
            self.assertIn(".lockedin/", (project / ".git" / "info" / "exclude").read_text())
            with self.assertRaises(RuntimeError):
                scientist_cli.ProjectSync(ACCOUNT, project, "other").validate_or_initialize()

    def test_missing_binding_is_an_actionable_preflight_error(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / ".lockedin" / "config").mkdir(parents=True)
            with self.assertRaisesRegex(RuntimeError, "hard-reset <bubble>"):
                scientist_cli.ProjectSync(ACCOUNT, project, "work").validate_or_initialize()

    def test_pull_only_assets_are_restored_and_detached_assets_removed(self):
        files = {"assets/paper/paper.pdf": b"server pdf", "assets/paper/summary.md": b"server summary",
                 "config/math.yaml": b"math", "config/reviews.yaml": b"threads: []\n", "config/overleaf.yaml": b'{"overleaf_project_id":"abcDEF123"}', "reports/pages.yaml": b"pages: []\n",
                 "reports/_lockedin_papers.md": b"# Papers\n", "reports/pages/overview.md": b"# Overview\n"}
        fake = FakeBubbleServer(files)
        with tempfile.TemporaryDirectory() as directory, patch.object(scientist_cli, "request", side_effect=fake.request):
            sync = scientist_cli.ProjectSync(ACCOUNT, Path(directory), "work")
            sync.sync_once()
            pdf = sync.root / "assets" / "paper" / "paper.pdf"
            pdf.chmod(0o644); pdf.write_bytes(b"local mutation")
            (sync.root / "assets").chmod(0o755)
            stale = sync.root / "assets" / "removed" / "paper.pdf"; stale.parent.mkdir(); stale.write_bytes(b"old")
            sync.sync_once()
            self.assertEqual(pdf.read_bytes(), b"server pdf")
            self.assertFalse(stale.exists())
            self.assertFalse(os.stat(pdf).st_mode & 0o222)
            self.assertTrue((sync.root / "reports" / "pages.yaml").exists())
            self.assertTrue((sync.root / "reports" / "_lockedin_papers.md").exists())
            self.assertTrue((sync.root / "config" / "overleaf.yaml").exists())
            reviews = sync.root / "config" / "reviews.yaml"
            self.assertEqual(reviews.read_bytes(), b"threads: []\n")
            self.assertFalse(os.stat(reviews).st_mode & 0o222)
            self.assertFalse((sync.root / "overleaf").exists())
            self.assertIn("`\\bmu`", (sync.root / "SKILL.md").read_text())

    def test_report_conflict_restores_server_copy_and_keeps_patch(self):
        files = {"reports/pages/overview.md": b"# Server\n"}
        fake = FakeBubbleServer(files)
        with tempfile.TemporaryDirectory() as directory, patch.object(scientist_cli, "request", side_effect=fake.request):
            sync = scientist_cli.ProjectSync(ACCOUNT, Path(directory), "work")
            sync.sync_once()
            page = sync.root / "reports" / "pages" / "overview.md"; page.write_bytes(b"# Local\n")
            fake.files["reports/pages/overview.md"] = b"# New server\n"
            sync.sync_once()
            self.assertEqual(page.read_bytes(), b"# New server\n")
            self.assertTrue(list((sync.root / "config" / "conflicts").rglob("*.patch")))

    def test_resolved_review_feedback_is_removed_locally_without_uploading(self):
        files = {"reports/pages/overview.md": b"# Overview\n",
                 "config/reviews.yaml": b"version: 1\nthreads: []\n"}
        fake = FakeBubbleServer(files)
        with tempfile.TemporaryDirectory() as directory, patch.object(scientist_cli, "request", side_effect=fake.request):
            sync = scientist_cli.ProjectSync(ACCOUNT, Path(directory), "work")
            sync.sync_once()
            review = sync.root / "config" / "reviews.yaml"
            self.assertTrue(review.exists())
            del fake.files["config/reviews.yaml"]
            sync.sync_once()
            self.assertFalse(review.exists())
            self.assertNotIn("config/reviews.yaml", fake.files)

    def test_new_page_and_delete_are_sent_to_server(self):
        files = {"reports/pages/overview.md": b"# Overview\n"}
        fake = FakeBubbleServer(files)
        with tempfile.TemporaryDirectory() as directory, patch.object(scientist_cli, "request", side_effect=fake.request):
            sync = scientist_cli.ProjectSync(ACCOUNT, Path(directory), "work")
            sync.sync_once()
            page = sync.root / "reports" / "pages" / "new-page.md"; page.write_text("# New\n")
            sync.sync_once()
            self.assertEqual(fake.files["reports/pages/new-page.md"], b"# New\n")
            page.unlink(); sync.sync_once()
            self.assertNotIn("reports/pages/new-page.md", fake.files)

    def test_page_deleted_on_server_is_removed_locally_without_becoming_a_new_page(self):
        files = {"reports/pages/overview.md": b"# Overview\n",
                 "reports/assets/figure.png": b"figure"}
        fake = FakeBubbleServer(files)
        with tempfile.TemporaryDirectory() as directory, patch.object(scientist_cli, "request", side_effect=fake.request):
            sync = scientist_cli.ProjectSync(ACCOUNT, Path(directory), "work")
            sync.sync_once()
            del fake.files["reports/pages/overview.md"]
            del fake.files["reports/assets/figure.png"]
            sync.sync_once()
            self.assertFalse((sync.root / "reports" / "pages" / "overview.md").exists())
            self.assertFalse((sync.root / "reports" / "assets" / "figure.png").exists())


class ScientistProfileAndWorkersTest(unittest.TestCase):
    def test_doctor_requires_a_matching_live_worker_and_server_probe(self):
        with temp_data_home(), tempfile.TemporaryDirectory() as directory, patch.object(
                scientist_cli, "_alive", return_value=True), patch.object(
                scientist_cli, "account_request", return_value={"files": []}) as request:
            project = Path(directory)
            config = project / ".lockedin" / "config"; config.mkdir(parents=True)
            binding = {"server": ACCOUNT["server"], "user": ACCOUNT["user"],
                       "workspace_id": ACCOUNT["workspace_id"], "bubble": "work"}
            (config / "binding.json").write_text(json.dumps(binding))
            scientist_cli.save_config({"accounts": [dict(ACCOUNT)]})
            scientist_cli.save_workers({"workers": {"worker": {
                "id": "worker", "project": str(project.resolve()), "bubble": "work",
                "server": ACCOUNT["server"], "user": ACCOUNT["user"],
                "workspace_id": ACCOUNT["workspace_id"], "pid": 42, "status": "running",
                "started_at": time.time(), "last_sync": time.time(), "last_error": "",
            }}})
            output = io.StringIO()
            with redirect_stdout(output): scientist_cli.doctor_command(project)
            self.assertIn("is healthy", output.getvalue())
            request.assert_called_once_with(dict(ACCOUNT), "GET", "/api/scientist/v2/bubbles/work/manifest")

    def test_doctor_explains_when_no_worker_is_assigned(self):
        with temp_data_home(), tempfile.TemporaryDirectory() as directory:
            project = Path(directory); config = project / ".lockedin" / "config"; config.mkdir(parents=True)
            (config / "binding.json").write_text(json.dumps({
                "server": ACCOUNT["server"], "user": ACCOUNT["user"],
                "workspace_id": ACCOUNT["workspace_id"], "bubble": "work"}))
            with self.assertRaisesRegex(RuntimeError, "No worker is assigned"):
                scientist_cli.doctor_command(project)

    def test_outdated_warning_is_visible_without_failing_a_local_command(self):
        output = io.StringIO()
        with patch.object(scientist_cli, "account_request", side_effect=RuntimeError("LockedIn Scientist is out of date. Reinstall it, then retry.")):
            with redirect_stderr(output): scientist_cli.warn_if_outdated(dict(ACCOUNT))
        self.assertIn("Reinstall: curl -fsSL", output.getvalue())

    def test_overleaf_help_and_unlinked_connect_are_actionable(self):
        output = io.StringIO()
        with redirect_stdout(output): scientist_cli.overleaf_help_command()
        self.assertIn("website", output.getvalue())
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "Link one from its LockedIn website page"):
                scientist_cli.overleaf_connect(Path(directory))

    def test_overleaf_connect_replaces_only_the_retired_placeholder(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(scientist_cli, "_git") as git, patch.object(
                scientist_cli, "_configure_overleaf_credential_store", return_value=None):
            project = Path(directory); config = project / ".lockedin" / "config"; config.mkdir(parents=True)
            (config / "overleaf.yaml").write_text('{"overleaf_git_url":"https://git@git.overleaf.com/abcDEF123"}')
            legacy = project / ".lockedin" / "overleaf"; legacy.mkdir()
            (legacy / "README.md").write_text(scientist_cli.LEGACY_OVERLEAF_README_PREFIX)
            git.return_value = scientist_cli.subprocess.CompletedProcess([], 0, stdout="helper")
            scientist_cli.overleaf_connect(project)
            self.assertFalse(legacy.exists())

    def test_overleaf_fallback_credentials_are_private_and_user_scoped(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(scientist_cli, "_git") as git, patch.object(
                scientist_cli, "data_root", return_value=Path(directory) / "profile"), patch.object(
                scientist_cli.shutil, "which", return_value="/usr/bin/git"), patch.object(
                scientist_cli.subprocess, "run", return_value=scientist_cli.subprocess.CompletedProcess([], 1, stdout="")):
            project = Path(directory) / "project"; checkout = project / ".lockedin" / "overleaf"
            checkout.mkdir(parents=True)
            stored = scientist_cli._configure_overleaf_credential_store(project, checkout)
            self.assertTrue(stored and stored.is_file())
            self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
            self.assertEqual(stored, scientist_cli._overleaf_credential_path())
            self.assertIn("--global", git.call_args_list[0].args[0])
            self.assertIn(f"store --file={stored}", git.call_args_list[0].args[0])

    def test_overleaf_uses_the_remote_default_branch_not_master(self):
        checkout = Path("/tmp/lockedin-overleaf-branch-test")
        with patch.object(scientist_cli, "_git", return_value=scientist_cli.subprocess.CompletedProcess(
                [], 0, stdout="ref: refs/heads/main\tHEAD\n")) as git:
            self.assertEqual(scientist_cli._overleaf_remote_branch(checkout), "main")
            self.assertEqual(git.call_args.args[0], ["ls-remote", "--symref", "lockedin-overleaf", "HEAD"])

    def test_git_error_without_captured_output_remains_actionable(self):
        with patch.object(scientist_cli.shutil, "which", return_value="/usr/bin/git"), patch.object(
                scientist_cli.subprocess, "run", return_value=scientist_cli.subprocess.CompletedProcess([], 1)):
            with self.assertRaisesRegex(RuntimeError, "Overleaf sync did not complete"):
                scientist_cli._git(["fetch"], Path.cwd())

    def test_empty_command_shows_the_v2_guided_overview(self):
        original = list(scientist_cli.sys.argv)
        try:
            scientist_cli.sys.argv = ["lockedin-scientist"]
            output = io.StringIO()
            with redirect_stdout(output): scientist_cli._main()
        finally:
            scientist_cli.sys.argv = original
        self.assertIn("research assistent", output.getvalue())
        self.assertIn("│         research assistent         │", output.getvalue())
        self.assertIn("hard-reset <bubble-slug>", output.getvalue())
        self.assertIn("Manual Overleaf publishing", output.getvalue())
        self.assertIn("overleaf help", output.getvalue())

    def test_bubbles_are_presented_as_a_numbered_terminal_list(self):
        with patch.object(scientist_cli, "request", return_value={"bubbles": [
                {"slug": "current-work", "name": "Current Work"},
                {"slug": "literature", "name": "Literature"},
        ]}):
            output = io.StringIO()
            with redirect_stdout(output): scientist_cli.bubbles_command(dict(ACCOUNT))
        rendered = output.getvalue()
        self.assertIn("1.", rendered)
        self.assertIn("Current Work", rendered)
        self.assertIn("sync <bubble-slug>", rendered)

    def test_degraded_worker_marker_is_orange(self):
        with patch.dict(os.environ, {"FORCE_COLOR": "1", "NO_COLOR": ""}, clear=False):
            self.assertIn("38;5;214", scientist_cli.orange("●"))

    def test_workspace_switch_persists_in_global_profile(self):
        with temp_data_home(), patch.object(scientist_cli, "request", return_value={"workspaces": [
                {"id": "personal", "name": "Personal"}, {"id": "lab", "name": "Lab"}]}) as request:
            scientist_cli.save_config({"accounts": [dict(ACCOUNT)]})
            account = dict(ACCOUNT)
            output = io.StringIO()
            with redirect_stdout(output): scientist_cli.switch_workspace(account, "Lab")
            self.assertEqual(scientist_cli.load_config()["accounts"][0]["workspace_id"], "lab")
            self.assertEqual(account["workspace_id"], "lab")
            self.assertIn("Lab  ✓ active", output.getvalue())
            self.assertEqual(request.call_count, 1)

    def test_ps_marks_a_dead_worker_stopped_without_touching_project(self):
        with temp_data_home(), tempfile.TemporaryDirectory() as project:
            lockedin = Path(project) / ".lockedin"; lockedin.mkdir(); marker = lockedin / "keep"; marker.write_text("yes")
            scientist_cli.save_workers({"workers": {"dead": {"id": "dead", "pid": 0, "project": project,
                                                                  "bubble": "work", "status": "running"}}})
            output = io.StringIO()
            with redirect_stdout(output): scientist_cli.ps_command()
            self.assertIn("dead", output.getvalue())
            self.assertEqual(scientist_cli.load_workers()["workers"]["dead"]["status"], "stopped")
            self.assertTrue(marker.exists())

    def test_ps_shows_recovery_for_a_missing_binding_failure(self):
        with temp_data_home():
            scientist_cli.save_workers({"workers": {"failed": {
                "id": "failed", "pid": 0, "project": "/project", "bubble": "work",
                "status": "failed", "error": "missing .lockedin binding",
            }}})
            output = io.StringIO()
            with redirect_stdout(output): scientist_cli.ps_command()
            self.assertIn("hard-reset work", output.getvalue())

    def test_worker_history_keeps_live_workers_and_only_recent_terminal_records(self):
        data = {"workers": {
            "live": {"id": "live", "status": "running", "started_at": 1},
            "failed": {"id": "failed", "status": "failed", "started_at": 2},
            **{f"old-{index}": {"id": f"old-{index}", "status": "stopped", "stopped_at": index}
               for index in range(scientist_cli.WORKER_HISTORY_LIMIT + 3)},
        }}
        scientist_cli._prune_worker_history(data)
        self.assertIn("live", data["workers"])
        self.assertIn("failed", data["workers"])
        self.assertEqual(len(data["workers"]), scientist_cli.WORKER_HISTORY_LIMIT + 2)
        self.assertNotIn("old-0", data["workers"])

    def test_command_surface_has_no_vendor_wrappers(self):
        source = Path(scientist_cli.__file__).read_text()
        self.assertNotIn('add_parser("resume")', source)
        self.assertNotIn("def " + "run" + "_agent", source)
        self.assertNotIn("--from-server", source)
        self.assertIn('add_parser("hard-reset")', source)
        self.assertIn('add_parser("overleaf")', source)
        self.assertIn("the sync worker registers it automatically", scientist_cli.SKILL_RULES)
        self.assertIn("the sync worker removes it", scientist_cli.SKILL_RULES)
        self.assertIn("config/math.yaml", scientist_cli.SKILL_RULES)
        self.assertIn("lockedin-scientist-skill: 11", scientist_cli.SKILL_RULES)
        self.assertIn("Outside `.lockedin/`, work on this repository normally", scientist_cli.SKILL_RULES)
        self.assertIn("manuscript changes stay local until that explicit sync", scientist_cli.SKILL_RULES)
        self.assertIn("create or edit `.tex`, `.bib`, `.sty`, `.cls`", scientist_cli.SKILL_RULES)
        self.assertIn("Reports: the live research record", scientist_cli.SKILL_RULES)
        self.assertIn("the curated publication source", scientist_cli.SKILL_RULES)
        self.assertIn("portable relative Markdown image", scientist_cli.SKILL_RULES)
        self.assertIn("Treat feedback critically", scientist_cli.SKILL_RULES)
        self.assertIn("Make the smallest change", scientist_cli.SKILL_RULES)
        self.assertIn("never reply to, edit, delete, or resolve", scientist_cli.SKILL_RULES)
        self.assertIn("Direct LockedIn paths — do not search for them", scientist_cli.SKILL_RULES)
        self.assertIn("For any report-related search, search only inside `.lockedin/`", scientist_cli.SKILL_RULES)
        self.assertIn("lockedin-scientist doctor", scientist_cli.SKILL_RULES)

    def test_skill_embeds_the_active_math_macro_table(self):
        skill = scientist_cli.skill_document("## Markdown\n", {"\\E": "\\mathbb{E}"})
        self.assertIn("| `\\E` | `\\mathbb{E}` |", skill)

    def test_vendor_setup_installs_named_native_bootstraps_without_a_profile(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(scientist_cli.shutil, "which", return_value="/usr/bin/agy"), patch.object(
                scientist_cli.subprocess, "run", return_value=scientist_cli.subprocess.CompletedProcess([], 0)) as run:
            home = Path(directory)
            codex = scientist_cli.setup_vendor_skill("codex", home=home)
            claude = scientist_cli.setup_vendor_skill("claude", home=home)
            agy = scientist_cli.setup_vendor_skill("agy", home=home)
            for targets in (codex, claude, agy):
                skill = next(path for path in targets if path.name == "SKILL.md")
                content = skill.read_text()
                self.assertIn("name: lockedin-scientist", content)
                self.assertIn(".lockedin/SKILL.md", content)
            plugin = json.loads(agy[0].read_text())
            self.assertEqual(plugin["name"], "lockedin-scientist")
            self.assertEqual(plugin["managed_by"], "lockedin-scientist")
            run.assert_called_once_with(
                ["/usr/bin/agy", "plugin", "install", str(agy[0].parent)],
                stdin=scientist_cli.subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
            )

    def test_vendor_setup_refuses_to_overwrite_a_user_owned_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".claude" / "skills" / "lockedin-scientist" / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text("# My skill\n")
            with self.assertRaisesRegex(RuntimeError, "Refusing to overwrite"):
                scientist_cli.setup_vendor_skill("claude", home=Path(directory))


if __name__ == "__main__":
    unittest.main()
