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


class Account(unittest.TestCase):
    def test_new_accounts_are_auto_approved_but_not_premium_by_default(self):
        with temp_base():
            from lockedin import auth, models, paths
            auth.create_user("owner", "pw12")
            auth.create_user("alice", "pw12")

            self.assertTrue(auth.is_approved("alice"))
            self.assertFalse(auth.is_premium("alice"))
            self.assertEqual(models.load_config(paths.user_home("alice"))["active"], "openai")

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
