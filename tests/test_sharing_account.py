"""Deterministic tests for public bubble sharing + account (username/password) editing.

No network / no LLM. These exercise the base-root stores (accounts, share index) and the
workspace-move on username change, so each test points ``LOCKEDIN_HOME`` at a throwaway dir.

Run: ``uv run python -m unittest tests.test_sharing_account -v``
"""
from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def temp_base():
    """Run with a fresh base root (so accounts.yaml / share_index.yaml are isolated)."""
    prev = os.environ.get("LOCKEDIN_HOME")
    with tempfile.TemporaryDirectory() as d:
        os.environ["LOCKEDIN_HOME"] = d
        try:
            yield Path(d)
        finally:
            if prev is None:
                os.environ.pop("LOCKEDIN_HOME", None)
            else:
                os.environ["LOCKEDIN_HOME"] = prev


class Sharing(unittest.TestCase):
    def test_toggle_mints_stable_token_and_gates_on_active(self):
        with temp_base():
            from lockedin import auth, bubbles, paths, service, sharing
            auth.create_user("alice", "pw12")
            home = paths.user_home("alice")
            service.create_bubble(home, "Diffusion")
            service.approve_bubble(home, "diffusion")

            # off by default
            self.assertIsNone(service.share_target("nope"))

            r1 = service.set_bubble_share(home, "diffusion", True)
            tok = r1["share_token"]
            self.assertTrue(tok and r1["share_active"])
            self.assertEqual(service.share_target(tok), (home, "diffusion"))

            # deactivate → token no longer resolves
            service.set_bubble_share(home, "diffusion", False)
            self.assertIsNone(service.share_target(tok))

            # reactivate → SAME token works again (stable link)
            r2 = service.set_bubble_share(home, "diffusion", True)
            self.assertEqual(r2["share_token"], tok)
            self.assertEqual(service.share_target(tok), (home, "diffusion"))

    def test_delete_bubble_revokes_share(self):
        with temp_base():
            from lockedin import auth, paths, service, sharing
            auth.create_user("bob", "pw12")
            home = paths.user_home("bob")
            service.create_bubble(home, "Topic")
            service.approve_bubble(home, "topic")
            tok = service.set_bubble_share(home, "topic", True)["share_token"]
            service.delete_bubble(home, "topic")
            self.assertIsNone(sharing.resolve(tok))
            self.assertIsNone(service.share_target(tok))

    def test_workspace_share_resolves_to_the_workspace_home(self):
        with temp_base():
            from lockedin import auth, paths, service, workspaces
            auth.create_user("alice", "pw12")
            workspace = workspaces.ensure_personal("alice", auth.load_accounts()["alice"])
            home = workspaces.workspace_home(workspace["id"])
            service.create_bubble(home, "Topic")
            service.approve_bubble(home, "topic")
            token = service.set_bubble_share(home, "topic", True)["share_token"]
            self.assertEqual(service.share_target(token), (home, "topic"))

    def test_legacy_share_index_entries_migrate_to_personal_workspaces(self):
        with temp_base():
            from lockedin import auth, service, sharing, workspaces
            auth.create_user("alice", "pw12")
            personal_id = auth.load_accounts()["alice"]["personal_workspace_id"]
            personal_home = workspaces.workspace_home(personal_id)
            service.create_bubble(personal_home, "Topic")
            service.approve_bubble(personal_home, "topic")
            token = service.set_bubble_share(personal_home, "topic", True)["share_token"]
            # Mimic a pre-workspace index record that still carries the username.
            sharing.register(token, "alice", "topic")
            self.assertEqual(service.migrate_share_index_to_workspaces(), 1)
            self.assertEqual(sharing.resolve(token)["workspace_id"], personal_id)
            self.assertEqual(service.share_target(token), (personal_home, "topic"))

    def test_workspace_id_in_legacy_user_field_is_normalized(self):
        with temp_base():
            from lockedin import auth, service, sharing
            auth.create_user("alice", "pw12")
            workspace_id = auth.load_accounts()["alice"]["personal_workspace_id"]
            sharing.register("legacy-workspace-token", workspace_id, "topic")
            self.assertEqual(service.migrate_share_index_to_workspaces(), 1)
            self.assertEqual(sharing.resolve("legacy-workspace-token")["workspace_id"], workspace_id)


class Account(unittest.TestCase):
    def test_only_the_first_account_is_approved_by_default(self):
        with temp_base():
            from lockedin import auth, models, paths
            auth.create_user("owner", "pw12")
            auth.create_user("alice", "pw12")

            self.assertTrue(auth.is_approved("owner"))
            self.assertFalse(auth.is_approved("alice"))
            self.assertFalse(auth.is_premium("alice"))
            self.assertEqual(models.load_config(paths.user_home("alice"))["active"], "openai")

    def test_unapproved_signup_and_login_explain_how_to_request_demo_access(self):
        with temp_base():
            from fastapi.testclient import TestClient
            from lockedin import auth, server

            auth.create_user("owner", "pw12")
            with TestClient(server.build_app(), base_url="https://testserver") as client:
                signup = client.post("/api/signup", json={"username": "alice", "password": "pw12"})
                self.assertEqual(signup.status_code, 200)
                self.assertEqual(signup.json(), {
                    "pending": True, "message": server.DEMO_ACCESS_MESSAGE})

                login = client.post("/api/login", json={"username": "alice", "password": "pw12"})
                self.assertEqual(login.status_code, 403)
                self.assertEqual(login.json()["detail"], server.DEMO_ACCESS_MESSAGE)

    def test_qwen_requires_premium_account(self):
        with temp_base():
            from lockedin import auth, models, paths
            auth.create_user("owner", "pw12")
            auth.create_user("alice", "pw12")
            home = paths.user_home("alice")

            with self.assertRaises(PermissionError):
                models.set_active_provider(home, "qwen")

            auth.set_premium("alice", True)
            self.assertTrue(auth.is_premium("alice"))
            self.assertEqual(models.set_active_provider(home, "qwen")["active"], "qwen")

    def test_model_settings_are_account_scoped_across_workspaces(self):
        with temp_base():
            from lockedin import auth, models, paths, workspaces
            auth.create_user("alice", "pw12")
            account_home = paths.user_home("alice")
            workspace = workspaces.create("alice", "Shared Research")
            workspace_home = workspaces.workspace_home(workspace["id"])
            with models.use_account_home(account_home):
                models.save_config(workspace_home, {"openai": {"api_key": "account-key"}})
                self.assertEqual(models.load_config(workspace_home)["openai"]["api_key"], "account-key")
            self.assertFalse((workspace_home / "config" / "active_model.yaml").exists())
            self.assertTrue((account_home / "config" / "active_model.yaml").exists())

    def test_premium_request_is_recorded_and_cleared_on_upgrade(self):
        with temp_base():
            from lockedin import auth
            auth.create_user("owner", "pw12")
            auth.create_user("alice", "pw12")

            requested_at = auth.request_premium("alice")
            self.assertTrue(requested_at)
            self.assertEqual(auth.list_users()[0]["premium_requested_at"], requested_at)

            auth.set_premium("alice", True)
            alice = next(u for u in auth.list_users() if u["username"] == "alice")
            self.assertTrue(alice["premium"])
            self.assertFalse(alice["premium_requested_at"])

    def test_change_password_requires_current_and_rehashes(self):
        with temp_base():
            from lockedin import auth, service
            auth.create_user("alice", "oldpw")
            with self.assertRaises(ValueError):
                service.update_account("alice", current_password="wrong", new_password="newpw")
            service.update_account("alice", current_password="oldpw", new_password="newpw")
            self.assertFalse(auth.verify_password("alice", "oldpw"))
            self.assertTrue(auth.verify_password("alice", "newpw"))

    def test_slack_link_is_cleared_on_password_or_username_change(self):
        with temp_base():
            from lockedin import auth, service
            auth.create_user("alice", "pw12")
            auth.link_slack_user("alice", "U123")
            self.assertEqual(auth.user_for_slack("U123"), "alice")

            service.update_account("alice", current_password="pw12", new_password="newpw")
            self.assertIsNone(auth.user_for_slack("U123"))

            auth.link_slack_user("alice", "U123")
            service.update_account("alice", current_password="newpw", new_username="alice2")
            self.assertIsNone(auth.user_for_slack("U123"))

    def test_rename_moves_workspace_and_repoints_share(self):
        with temp_base():
            from lockedin import auth, paths, service, sharing
            auth.create_user("alice", "pw12")
            home = paths.user_home("alice")
            service.create_bubble(home, "Diffusion")
            service.approve_bubble(home, "diffusion")
            # seed a real page so we can prove the workspace moved with its content
            service.bubble_detail(home, "diffusion")          # materialize pages
            service.save_page(home, "diffusion", "overview", "# Diffusion\n\nmine\n")
            tok = service.set_bubble_share(home, "diffusion", True)["share_token"]

            final = service.update_account("alice", current_password="pw12", new_username="alice2")
            self.assertEqual(final, "alice2")
            self.assertFalse(paths.user_home("alice").exists())
            self.assertTrue(paths.user_home("alice2").exists())
            # content carried over
            self.assertIn("mine", service.get_page(paths.user_home("alice2"), "diffusion", "overview"))
            # share index repointed → still resolves to the new home
            self.assertEqual(service.share_target(tok), (paths.user_home("alice2"), "diffusion"))
            # accounts store updated
            self.assertTrue(auth.user_exists("alice2"))
            self.assertFalse(auth.user_exists("alice"))

    def test_rename_to_taken_name_is_rejected(self):
        with temp_base():
            from lockedin import auth, service
            auth.create_user("alice", "pw12")
            auth.create_user("bob", "pw12")
            with self.assertRaises(ValueError):
                service.update_account("alice", current_password="pw12", new_username="bob")


if __name__ == "__main__":
    unittest.main()
