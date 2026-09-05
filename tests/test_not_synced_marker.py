"""The marker that makes an unsynced large asset visible from the project folder.

A large asset is not on disk in the project, so nothing in `reports/assets/` shows it exists —
an agent that lists that directory sees the small figures and concludes the big ones are gone or
were never uploaded. One generated file states what is in the bubble and how to move it.

One marker, not a stub per asset, is deliberate: everything in `reports/assets/` is something the
sync will push, so each extra file there is another chance to leak a fake asset into a real
bubble. There is exactly one thing to keep out of the push path.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lockedin import paths, scientist_cli, scientist_sync

from tests._fixtures import make_bubble

CAP = 64 * 1024


class NotSyncedMarkerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        for sub in ("ASSETS", "REPORTS", "config"):
            (self.home / sub).mkdir(parents=True, exist_ok=True)
        self.slug = make_bubble(self.home, "Heavy")
        with paths.use_root(self.home):
            self.adir = paths.bubble_assets_dir(self.slug)
            self.adir.mkdir(parents=True, exist_ok=True)
            (self.adir / "figure.png").write_bytes(os.urandom(2048))
        self._patch = patch.object(scientist_sync, "LARGE_ASSET_BYTES", CAP)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _files(self):
        return scientist_sync._files(self.home, self.slug)

    def _big(self, name="photos.zip", mult=4):
        (self.adir / name).write_bytes(os.urandom(CAP * mult))

    def test_no_marker_when_everything_syncs_normally(self):
        # It must not appear in a bubble it has nothing to say about.
        self.assertNotIn(scientist_sync.NOT_SYNCED_PATH, self._files())

    def test_the_marker_names_every_large_asset_and_its_size(self):
        self._big("photos.zip")
        self._big("checkpoint.bin", mult=8)
        body = self._files()[scientist_sync.NOT_SYNCED_PATH].decode()
        self.assertIn("photos.zip", body)
        self.assertIn("checkpoint.bin", body)
        self.assertNotIn("figure.png", body)          # that one is on disk already
        self.assertIn("2 file(s)", body)

    def test_it_says_how_to_move_one_in_each_direction(self):
        self._big()
        body = self._files()[scientist_sync.NOT_SYNCED_PATH].decode()
        for command in ("assets pull", "assets push", "assets rm"):
            self.assertIn(command, body)

    def test_it_disappears_once_the_last_large_asset_is_gone(self):
        self._big()
        self.assertIn(scientist_sync.NOT_SYNCED_PATH, self._files())
        (self.adir / "photos.zip").unlink()
        self.assertNotIn(scientist_sync.NOT_SYNCED_PATH, self._files())

    def test_a_client_cannot_push_the_marker_back_as_a_real_asset(self):
        # It is generated. A pushed copy would become an actual file in the bubble's assets.
        self.assertFalse(scientist_sync.writable_path(self.slug, scientist_sync.NOT_SYNCED_PATH))
        self.assertTrue(scientist_sync.writable_path(self.slug, "reports/assets/figure.png"))

    def test_the_client_and_server_agree_on_where_it_lives(self):
        # The installed client is dependency-free and cannot import the server module, so the
        # path is repeated. If they drift, the client pushes the marker back as an asset.
        self.assertEqual(scientist_cli.NOT_SYNCED_PATH, scientist_sync.NOT_SYNCED_PATH)

    def test_it_is_not_mistaken_for_a_large_asset_itself(self):
        self._big()
        large = [a["path"] for a in scientist_sync.large_assets(self.home, self.slug)]
        self.assertNotIn(scientist_sync.NOT_SYNCED_PATH, large)


if __name__ == "__main__":
    unittest.main()
