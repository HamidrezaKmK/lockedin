"""Deterministic tests for bubble presence: people viewing and Scientist workers syncing.

No network / no LLM. Covers the two identity granularities the monitor exists to express — one
row per *user* however many tabs, one row per *project directory* however many agents — plus the
health states, including the out-of-date client that gets rejected before it reaches a route.

Run: ``LOCKEDIN_HOME=/tmp/li_test uv run python -m unittest tests.test_presence -v``
"""
from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from lockedin import presence


@contextmanager
def temp_base():
    prev = os.environ.get("LOCKEDIN_HOME")
    with tempfile.TemporaryDirectory() as d:
        os.environ["LOCKEDIN_HOME"] = d
        presence.reset()
        try:
            yield Path(d)
        finally:
            presence.reset()
            if prev is None:
                os.environ.pop("LOCKEDIN_HOME", None)
            else:
                os.environ["LOCKEDIN_HOME"] = prev


class Registry(unittest.TestCase):
    def setUp(self):
        presence.reset()

    def tearDown(self):
        presence.reset()

    def test_one_user_with_several_tabs_is_one_viewer(self):
        for _ in range(3):
            presence.touch_viewer("ws", "diffusion", "alice")
        presence.touch_viewer("ws", "diffusion", "bob")
        rows = presence.snapshot("ws", "diffusion")["viewers"]
        self.assertEqual([r["user"] for r in rows], ["alice", "bob"])

    def test_a_viewer_expires_but_a_fresh_beat_keeps_them(self):
        presence.touch_viewer("ws", "d", "alice", now=1000.0)
        presence.touch_viewer("ws", "d", "bob", now=1000.0)
        presence.touch_viewer("ws", "d", "bob", now=1000.0 + presence.VIEWER_TTL)
        rows = presence.snapshot("ws", "d", now=1000.0 + presence.VIEWER_TTL + 1)["viewers"]
        self.assertEqual([r["user"] for r in rows], ["bob"])

    def test_presence_is_scoped_to_one_bubble_and_workspace(self):
        presence.touch_viewer("ws1", "a", "alice")
        presence.touch_viewer("ws2", "a", "bob")
        presence.touch_viewer("ws1", "b", "carol")
        self.assertEqual([r["user"] for r in presence.snapshot("ws1", "a")["viewers"]], ["alice"])
        self.assertEqual([r["user"] for r in presence.snapshot("ws2", "a")["viewers"]], ["bob"])

    def test_one_directory_stays_one_worker_across_many_requests(self):
        for _ in range(5):
            presence.touch_worker("ws", "d", worker_id="uid-1", user="alice", label="thesis",
                                  status="running")
        rows = presence.snapshot("ws", "d")["workers"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["display"], "alice:thesis")
        self.assertEqual(rows[0]["state"], "live")

    def test_two_directories_are_two_workers_and_raise_the_duplicate_warning(self):
        presence.touch_worker("ws", "d", worker_id="uid-1", user="alice", label="thesis")
        presence.touch_worker("ws", "d", worker_id="uid-2", user="alice", label="notes")
        snap = presence.snapshot("ws", "d")
        self.assertEqual([r["display"] for r in snap["workers"]], ["alice:notes", "alice:thesis"])
        self.assertEqual(sorted(snap["duplicate_workers"]), ["alice:notes", "alice:thesis"])

    def test_colliding_directory_names_are_disambiguated_but_unique_ones_are_not(self):
        presence.touch_worker("ws", "d", worker_id="aaaa1111", user="alice", label="notes")
        presence.touch_worker("ws", "d", worker_id="bbbb2222", user="alice", label="notes")
        presence.touch_worker("ws", "d", worker_id="cccc3333", user="alice", label="thesis")
        by_id = {r["worker_id"]: r["display"] for r in presence.snapshot("ws", "d")["workers"]}
        self.assertEqual(by_id["aaaa1111"], "alice:notes#aaaa")
        self.assertEqual(by_id["bbbb2222"], "alice:notes#bbbb")
        self.assertEqual(by_id["cccc3333"], "alice:thesis")

    def test_a_silent_worker_becomes_unresponsive_then_is_kept_as_a_grave(self):
        presence.touch_worker("ws", "d", worker_id="u", user="alice", label="t",
                              status="running", now=1000.0)
        live = presence.snapshot("ws", "d", now=1000.0)["workers"][0]
        self.assertEqual(live["state"], "live")
        late = presence.snapshot("ws", "d", now=1000.0 + presence.WORKER_TTL + 1)["workers"][0]
        self.assertEqual(late["state"], "unresponsive")
        self.assertIn("no contact", late["reason"])
        # A stopped worker stays visible for a while: vanishing silently would be indistinguishable
        # from never having been there.
        presence.touch_worker("ws", "d", worker_id="u", user="alice", label="t",
                              status="stopped", now=2000.0)
        self.assertEqual(presence.snapshot("ws", "d", now=2000.0)["workers"][0]["state"], "dead")
        self.assertEqual(presence.snapshot("ws", "d", now=2000.0 + presence.WORKER_GRAVE + 1)["workers"], [])

    def test_a_worker_that_dies_without_saying_so_is_still_forgotten(self):
        # The parting "stopped" notice is best-effort: a killed worker, a dropped network, or a
        # failing final sync never sends one. Such a worker must still age out instead of sitting
        # in the list forever with its last error frozen on it.
        presence.touch_worker("ws", "d", worker_id="u", user="alice", label="t",
                              status="degraded", error="server returned 409: conflict", now=1000.0)
        stale = presence.snapshot("ws", "d", now=1000.0 + presence.WORKER_TTL + 1)["workers"][0]
        self.assertEqual(stale["state"], "unresponsive")
        self.assertEqual(presence.snapshot("ws", "d", now=1000.0 + presence.WORKER_GRAVE + 1)["workers"], [])

    def test_a_reported_sync_error_shows_as_degraded_with_its_reason(self):
        presence.touch_worker("ws", "d", worker_id="u", user="alice", label="t",
                              status="degraded", error="server returned 409: conflict")
        row = presence.snapshot("ws", "d")["workers"][0]
        self.assertEqual(row["state"], "degraded")
        self.assertEqual(row["reason"], "server returned 409: conflict")

    def test_a_rejected_client_is_diagnosed_rather_than_trusted(self):
        # The client cannot report on itself when the server refuses it, so the server's own
        # verdict wins over whatever status the request carried.
        presence.touch_worker("ws", "d", worker_id="u", user="alice", label="t",
                              status="running", rejected="outdated")
        row = presence.snapshot("ws", "d")["workers"][0]
        self.assertEqual(row["state"], "dead")
        self.assertIn("out of date", row["reason"])

    def test_header_values_are_flattened_and_bounded(self):
        presence.touch_worker("ws", "d", worker_id="u", user="alice", label="t",
                              status="degraded", error="line one\nline two\r\n  spaced " + "x" * 500)
        reason = presence.snapshot("ws", "d")["workers"][0]["reason"]
        self.assertNotIn("\n", reason)
        self.assertTrue(reason.startswith("line one line two spaced"))
        self.assertLessEqual(len(reason), 300)

    def test_an_unidentified_worker_is_not_recorded(self):
        presence.touch_worker("ws", "d", worker_id="", user="alice")
        presence.touch_worker("ws", "d", worker_id="u", user="")
        self.assertEqual(presence.snapshot("ws", "d")["workers"], [])


class HttpFlow(unittest.TestCase):
    def test_heartbeat_reports_the_caller_and_returns_the_snapshot(self):
        with temp_base():
            from lockedin import auth, server, service, paths
            auth.create_user("alice", "pw12")
            with TestClient(server.build_app(), base_url="https://testserver") as client:
                client.post("/api/login", json={"username": "alice", "password": "pw12"})
                me = client.get("/api/me").json()
                workspace_id = me["workspace_id"]
                from lockedin import workspaces
                home = workspaces.workspace_home(workspace_id)
                service.create_bubble(home, "Diffusion")
                service.approve_bubble(home, "diffusion")

                snap = client.post("/api/bubbles/diffusion/presence").json()
                self.assertEqual([r["user"] for r in snap["viewers"]], ["alice"])
                self.assertEqual(snap["workers"], [])

                # A worker on the same bubble shows up alongside the person.
                presence.touch_worker(workspace_id, "diffusion", worker_id="uid-1",
                                      user="alice", label="thesis", status="running")
                snap = client.post("/api/bubbles/diffusion/presence").json()
                self.assertEqual([r["display"] for r in snap["workers"]], ["alice:thesis"])

                client.delete("/api/bubbles/diffusion/presence")
                self.assertEqual(presence.snapshot(workspace_id, "diffusion")["viewers"], [])

    def test_presence_requires_a_session(self):
        with temp_base():
            from lockedin import server
            with TestClient(server.build_app(), base_url="https://testserver") as client:
                self.assertEqual(client.post("/api/bubbles/x/presence").status_code, 401)

    def test_an_outdated_worker_is_still_listed_with_its_rejection(self):
        with temp_base():
            from lockedin import auth, server, service, workspaces
            auth.create_user("alice", "pw12")
            token = auth.new_scientist_token("alice", "lockedin-scientist")
            personal = workspaces.ensure_personal("alice", auth.load_accounts()["alice"])
            home = workspaces.workspace_home(personal["id"])
            service.ensure_workspace(home)
            service.create_bubble(home, "Diffusion")
            service.approve_bubble(home, "diffusion")
            with TestClient(server.build_app(), base_url="https://testserver") as client:
                response = client.get(
                    "/api/scientist/v2/bubbles/diffusion/manifest",
                    headers={"Authorization": "Bearer " + token,
                             "X-LockedIn-Workspace": personal["id"],
                             "X-LockedIn-Scientist-Version": "2020.01.01.1",
                             "X-LockedIn-Worker": "uid-old",
                             "X-LockedIn-Worker-Label": "stale-clone"})
            self.assertEqual(response.status_code, 426)
            row = presence.snapshot(personal["id"], "diffusion")["workers"][0]
            self.assertEqual(row["display"], "alice:stale-clone")
            self.assertEqual(row["state"], "dead")
            self.assertIn("out of date", row["reason"])

    def test_a_current_worker_registers_from_its_ordinary_sync_poll(self):
        with temp_base():
            from lockedin import auth, server, service, workspaces
            auth.create_user("alice", "pw12")
            token = auth.new_scientist_token("alice", "lockedin-scientist")
            personal = workspaces.ensure_personal("alice", auth.load_accounts()["alice"])
            home = workspaces.workspace_home(personal["id"])
            service.ensure_workspace(home)
            service.create_bubble(home, "Diffusion")
            service.approve_bubble(home, "diffusion")
            with TestClient(server.build_app(), base_url="https://testserver") as client:
                response = client.get(
                    "/api/scientist/v2/bubbles/diffusion/manifest",
                    headers={"Authorization": "Bearer " + token,
                             "X-LockedIn-Workspace": personal["id"],
                             "X-LockedIn-Scientist-Version": server.SCIENTIST_CLIENT_VERSION,
                             "X-LockedIn-Worker": "uid-1",
                             "X-LockedIn-Worker-Label": "thesis",
                             "X-LockedIn-Worker-Status": "running"})
            self.assertEqual(response.status_code, 200)
            row = presence.snapshot(personal["id"], "diffusion")["workers"][0]
            self.assertEqual((row["display"], row["state"]), ("alice:thesis", "live"))
            self.assertEqual(row["version"], server.SCIENTIST_CLIENT_VERSION)


if __name__ == "__main__":
    unittest.main()
