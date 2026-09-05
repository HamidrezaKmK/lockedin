"""End-to-end tests for the Scientist routes that move an oversized asset on request.

The ordinary sync deliberately will not carry these (see ``test_large_asset_sync``), so the only
way one moves is through these routes. They matter most at sizes nobody wants in a unit test —
a gigabyte checkpoint — so what is pinned here is the contract that makes such a transfer safe:
it is sliced (no single request body can exceed what a proxy will carry), an interrupted transfer
resumes rather than restarting, a push replaces its namesake instead of accumulating copies, and
nothing half-written is ever visible as an asset.

Run: ``uv run --with pytest python -m pytest tests/test_large_asset_transfer.py``
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from lockedin import auth, paths, scientist_sync, server, workspaces

CAP = 32 * 1024          # a stand-in threshold, so "oversized" costs kilobytes not gigabytes
SLICE = 8 * 1024         # a stand-in for the client's 32 MB slice


@contextmanager
def scientist_workspace():
    """A real server, one approved bubble, and a Scientist token for it.

    The bubble is created through the API rather than on disk: a bubble lives under the user's
    *workspace* home, which is what the Scientist routes resolve, and hand-placing it elsewhere
    makes every route answer 404.
    """
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        for name in ("ASSETS", "REPORTS", "config", "data"):
            (home / name).mkdir(parents=True, exist_ok=True)
        # Without this the session cookie is Secure-only and TestClient drops it over http, so
        # the bubble could never be created. The flag is read at import time, so patch the value.
        with patch.object(server, "SECURE_COOKIES", False), \
                patch.object(paths, "base_root", lambda: home), \
                patch.object(scientist_sync, "LARGE_ASSET_BYTES", CAP):
                with TestClient(server.build_app()) as client:
                    client.post("/api/signup",
                                json={"username": "alice", "password": "temporary-password"})
                    slug = client.post("/api/bubbles", json={"name": "Current Work"}).json()["slug"]
                    client.post(f"/api/bubbles/{slug}/approve", json={"instructions": ""})
                    token = auth.new_scientist_token("alice", "test")
                    bubble_home = workspaces.workspace_home(
                        workspaces.migrate_legacy("alice", auth.load_accounts()["alice"])["id"])
                    client.headers.update({
                        "Authorization": f"Bearer {token}",
                        "X-LockedIn-Scientist-Version": server.SCIENTIST_CLIENT_VERSION,
                    })
                    yield client, bubble_home, slug


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LargeAssetTransferTests(unittest.TestCase):
    def _push(self, client, slug, name, payload, *, stop_after=None):
        """Slice a payload up the way the client does. ``stop_after`` abandons it partway."""
        base = f"/api/scientist/v2/bubbles/{slug}/large-asset/push"
        begun = client.post(base + "/begin",
                            json={"filename": name, "total_size": len(payload)}).json()
        upload_id, offset = begun["upload_id"], int(begun["received"])
        sent = 0
        while offset < len(payload):
            if stop_after is not None and sent >= stop_after:
                return upload_id, offset
            block = payload[offset:offset + SLICE]
            response = client.post(f"{base}/{upload_id}?offset={offset}", content=block)
            self.assertEqual(response.status_code, 200, response.text)
            offset = int(response.json()["received"])
            sent += len(block)
        client.post(f"{base}/{upload_id}/finish")
        return upload_id, offset

    def test_a_file_far_over_the_cap_arrives_intact_in_slices(self):
        payload = os.urandom(CAP * 10)
        with scientist_workspace() as (client, home, slug):
            self._push(client, slug, "checkpoint.bin", payload)
            with paths.use_root(home):
                landed = paths.bubble_assets_dir(slug) / "checkpoint.bin"
            self.assertEqual(sha(landed.read_bytes()), sha(payload))

    def test_no_single_slice_approaches_a_proxy_body_limit(self):
        # The whole reason this route exists: one request must stay small.
        payload = os.urandom(CAP * 10)
        sizes = []
        with scientist_workspace() as (client, home, slug):
            real = client.post

            def spy(url, *a, **kw):
                if "large-asset/push" in url and kw.get("content") is not None:
                    sizes.append(len(kw["content"]))
                return real(url, *a, **kw)

            with patch.object(client, "post", spy):
                self._push(client, slug, "checkpoint.bin", payload)
        self.assertGreater(len(sizes), 1, "a large file must be split across requests")
        self.assertLessEqual(max(sizes), SLICE)

    def test_an_interrupted_push_resumes_instead_of_restarting(self):
        payload = os.urandom(CAP * 10)
        with scientist_workspace() as (client, home, slug):
            first, stopped = self._push(client, slug, "checkpoint.bin", payload,
                                        stop_after=CAP * 3)
            self.assertGreater(stopped, 0)
            self.assertLess(stopped, len(payload))
            with paths.use_root(home):
                # Nothing half-written may be visible as an asset.
                self.assertFalse((paths.bubble_assets_dir(slug) / "checkpoint.bin").exists())
            base = f"/api/scientist/v2/bubbles/{slug}/large-asset/push"
            again = client.post(base + "/begin",
                                json={"filename": "checkpoint.bin",
                                      "total_size": len(payload)}).json()
            self.assertEqual(again["upload_id"], first)
            self.assertEqual(again["received"], stopped)
            self.assertTrue(again["resumed"])
            self._push(client, slug, "checkpoint.bin", payload)
            with paths.use_root(home):
                landed = paths.bubble_assets_dir(slug) / "checkpoint.bin"
            self.assertEqual(sha(landed.read_bytes()), sha(payload))

    def test_pushing_the_same_name_twice_replaces_rather_than_accumulates(self):
        first, second = os.urandom(CAP * 5), os.urandom(CAP * 5)
        with scientist_workspace() as (client, home, slug):
            self._push(client, slug, "checkpoint.bin", first)
            self._push(client, slug, "checkpoint.bin", second)
            with paths.use_root(home):
                adir = paths.bubble_assets_dir(slug)
            self.assertEqual(sha((adir / "checkpoint.bin").read_bytes()), sha(second))
            self.assertFalse((adir / "checkpoint-2.bin").exists())

    def test_the_asset_streams_back_without_base64(self):
        payload = os.urandom(CAP * 6)
        with scientist_workspace() as (client, home, slug):
            self._push(client, slug, "checkpoint.bin", payload)
            got = client.post(f"/api/scientist/v2/bubbles/{slug}/large-asset",
                              json={"paths": ["reports/assets/checkpoint.bin"]})
            self.assertEqual(got.status_code, 200)
            self.assertEqual(sha(got.content), sha(payload))
            # It must not come back through the JSON route that encodes whole files.
            listed = client.post(f"/api/scientist/v2/bubbles/{slug}/files",
                                 json={"paths": ["reports/assets/checkpoint.bin"]}).json()
            self.assertEqual(listed["files"], [])
            self.assertEqual([s["path"] for s in listed["skipped"]],
                             ["reports/assets/checkpoint.bin"])

    def test_the_routes_refuse_an_unauthenticated_caller(self):
        payload = os.urandom(CAP * 3)
        with scientist_workspace() as (client, home, slug):
            self._push(client, slug, "checkpoint.bin", payload)
            client.headers.pop("Authorization")
            base = f"/api/scientist/v2/bubbles/{slug}/large-asset"
            for response in (client.get(base + "s"),
                             client.post(base, json={"paths": ["reports/assets/checkpoint.bin"]}),
                             client.post(base + "/push/begin",
                                         json={"filename": "x.bin", "total_size": 1})):
                self.assertEqual(response.status_code, 401)

    def test_a_bad_offset_is_refused_so_a_retry_cannot_corrupt_the_file(self):
        with scientist_workspace() as (client, home, slug):
            base = f"/api/scientist/v2/bubbles/{slug}/large-asset/push"
            begun = client.post(base + "/begin",
                                json={"filename": "c.bin", "total_size": CAP * 4}).json()
            wrong = client.post(f"{base}/{begun['upload_id']}?offset={CAP * 2}",
                                content=b"x" * SLICE)
            self.assertEqual(wrong.status_code, 400)
            self.assertIn("out of order", wrong.json()["detail"])

    def test_a_large_asset_can_be_deleted_from_the_server(self):
        # Deleting the local copy does not propagate — the rule that stops the file syncing down
        # stops the deletion syncing up — so removing one needs a route of its own.
        payload = os.urandom(CAP * 4)
        with scientist_workspace() as (client, home, slug):
            self._push(client, slug, "checkpoint.bin", payload)
            with paths.use_root(home):
                landed = paths.bubble_assets_dir(slug) / "checkpoint.bin"
            self.assertTrue(landed.exists())
            gone = client.post(f"/api/scientist/v2/bubbles/{slug}/large-asset/delete",
                               json={"paths": ["reports/assets/checkpoint.bin"]})
            self.assertEqual(gone.status_code, 200)
            self.assertEqual(gone.json()["deleted"], ["reports/assets/checkpoint.bin"])
            self.assertFalse(landed.exists())
            listed = client.get(f"/api/scientist/v2/bubbles/{slug}/large-assets").json()
            self.assertEqual([a["path"] for a in listed["assets"]], [])

    def test_deleting_something_already_gone_is_reported_not_fatal(self):
        with scientist_workspace() as (client, home, slug):
            gone = client.post(f"/api/scientist/v2/bubbles/{slug}/large-asset/delete",
                               json={"paths": ["reports/assets/never-existed.bin"]})
            self.assertEqual(gone.status_code, 200)
            self.assertEqual(gone.json(), {"deleted": [], "missing":
                                           ["reports/assets/never-existed.bin"]})

    def test_delete_refuses_anything_that_is_not_an_oversized_asset(self):
        # A normal asset is removed by deleting it locally; a traversal is not removed at all.
        with scientist_workspace() as (client, home, slug):
            with paths.use_root(home):
                adir = paths.bubble_assets_dir(slug)
                adir.mkdir(parents=True, exist_ok=True)
                (adir / "small.png").write_bytes(b"x" * 16)
            for bad in ("reports/assets/small.png", "../../../etc/passwd",
                        "reports/pages/overview.md"):
                got = client.post(f"/api/scientist/v2/bubbles/{slug}/large-asset/delete",
                                  json={"paths": [bad]})
                self.assertEqual(got.json()["deleted"], [], bad)
            with paths.use_root(home):
                self.assertTrue((paths.bubble_assets_dir(slug) / "small.png").exists())

    def test_the_listing_flags_whether_a_page_references_the_file(self):
        # What makes deleting safe to offer from a command line.
        payload = os.urandom(CAP * 4)
        with scientist_workspace() as (client, home, slug):
            self._push(client, slug, "figure.bin", payload)
            listed = client.get(f"/api/scientist/v2/bubbles/{slug}/large-assets").json()
            self.assertTrue(listed["assets"][0]["unused"])
            with paths.use_root(home):
                page = paths.bubble_pages_dir(slug)
                target = next(page.glob("*.md"))
                target.write_text(target.read_text() + "\n![f](assets/figure.bin)\n")
            listed = client.get(f"/api/scientist/v2/bubbles/{slug}/large-assets").json()
            self.assertFalse(listed["assets"][0]["unused"])


if __name__ == "__main__":
    unittest.main()
