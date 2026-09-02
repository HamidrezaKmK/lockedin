"""Regression tests for keeping oversized assets out of the polling sync.

``manifest()`` reads and hashes every exported file, and Scientist clients poll it every few
seconds. A multi-gigabyte photo archive in a bubble therefore costs more per poll than the entire
rest of the bubble, and no agent can read a zip anyway. Oversized assets stay *listed* — dropping
them would read as "deleted on the server" and make a client bin its local copy — but carry a
size/mtime revision, are never streamed as base64, and are fetched only on request.

Run: ``uv run --with pytest python -m pytest tests/test_large_asset_sync.py``
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lockedin import bubbles, paths, reports, scientist_sync

from tests._fixtures import make_bubble

SMALL = 4 * 1024
CAP = 64 * 1024


class LargeAssetSyncTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        for sub in ("ASSETS", "REPORTS", "config"):
            (self.home / sub).mkdir(parents=True, exist_ok=True)
        self.slug = make_bubble(self.home, "Heavy")
        with paths.use_root(self.home):
            adir = paths.bubble_assets_dir(self.slug)
            adir.mkdir(parents=True, exist_ok=True)
            (adir / "figure.png").write_bytes(os.urandom(SMALL))
            (adir / "photos.zip").write_bytes(os.urandom(CAP * 4))
        self._patch = patch.object(scientist_sync, "LARGE_ASSET_BYTES", CAP)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _entry(self, name: str) -> dict:
        files = scientist_sync.manifest(self.home, self.slug)["files"]
        return next(f for f in files if f["path"].endswith(name))

    def test_a_large_asset_stays_listed_so_it_is_not_read_as_deleted(self):
        # Hiding it would make the client delete its local copy on the next sync.
        paths_listed = [f["path"] for f in scientist_sync.manifest(self.home, self.slug)["files"]]
        self.assertIn("reports/assets/photos.zip", paths_listed)

    def test_a_large_asset_is_flagged_and_carries_its_size(self):
        entry = self._entry("photos.zip")
        self.assertTrue(entry["oversize"])
        self.assertEqual(entry["size"], CAP * 4)
        self.assertFalse(self._entry("figure.png").get("oversize"))

    def test_the_manifest_never_reads_a_large_asset(self):
        real_read = Path.read_bytes
        seen = []

        def spy(self_path, *a, **kw):
            seen.append(self_path.name)
            return real_read(self_path, *a, **kw)

        with patch.object(Path, "read_bytes", spy):
            scientist_sync.manifest(self.home, self.slug)
        self.assertNotIn("photos.zip", seen)
        self.assertIn("figure.png", seen)

    def test_its_revision_tracks_replacement_without_hashing_content(self):
        before = self._entry("photos.zip")["revision"]
        with paths.use_root(self.home):
            target = paths.bubble_assets_dir(self.slug) / "photos.zip"
            os.utime(target, (0, 0))
        self.assertNotEqual(self._entry("photos.zip")["revision"], before)

    def test_read_files_refuses_to_stream_a_large_asset(self):
        got = scientist_sync.read_files(
            self.home, self.slug, ["reports/assets/photos.zip", "reports/assets/figure.png"])
        self.assertEqual([f["path"] for f in got["files"]], ["reports/assets/figure.png"])
        self.assertEqual([s["path"] for s in got["skipped"]], ["reports/assets/photos.zip"])
        self.assertEqual(got["skipped"][0]["size"], CAP * 4)

    def test_the_on_demand_path_resolves_only_large_assets(self):
        got = scientist_sync.large_asset_path(self.home, self.slug, "reports/assets/photos.zip")
        self.assertEqual(got.read_bytes()[:0], b"")
        self.assertEqual(got.stat().st_size, CAP * 4)
        # A small file is served by the ordinary sync, and traversal is refused outright.
        for bad in ("reports/assets/figure.png", "../../../etc/passwd", "reports/pages/../../x"):
            with self.assertRaises(FileNotFoundError, msg=bad):
                scientist_sync.large_asset_path(self.home, self.slug, bad)

    def test_the_listing_reports_what_a_sync_skips(self):
        listed = scientist_sync.large_assets(self.home, self.slug)
        self.assertEqual([i["path"] for i in listed], ["reports/assets/photos.zip"])
        self.assertEqual(listed[0]["size"], CAP * 4)

    def test_the_manifest_publishes_the_threshold_for_clients(self):
        self.assertEqual(
            scientist_sync.manifest(self.home, self.slug)["large_asset_bytes"], CAP)

    def test_staging_for_an_in_progress_upload_is_never_exported(self):
        with paths.use_root(self.home):
            bubbles.begin_chunked_upload(self.slug, "huge.zip", 10_000)
        listed = [f["path"] for f in scientist_sync.manifest(self.home, self.slug)["files"]]
        self.assertFalse([p for p in listed if ".uploads" in p or p.endswith("part.tmp")])

    def test_a_pushed_asset_replaces_its_namesake_instead_of_doubling(self):
        # A client re-sending a file it pulled and edited means that file, not a second copy.
        with paths.use_root(self.home):
            adir = paths.bubble_assets_dir(self.slug)
            begun = bubbles.begin_chunked_upload(self.slug, "photos.zip", 8)
            bubbles.append_chunk(self.slug, begun["upload_id"], 0, b"replaced")
            bubbles.finish_chunked_upload(self.slug, begun["upload_id"], replace=True)
            self.assertEqual((adir / "photos.zip").read_bytes(), b"replaced")
            self.assertFalse((adir / "photos-2.zip").exists())

    def test_a_browser_upload_still_never_overwrites(self):
        # Two people uploading "figure.png" must get two files; only a push replaces.
        with paths.use_root(self.home):
            adir = paths.bubble_assets_dir(self.slug)
            before = (adir / "figure.png").read_bytes()
            begun = bubbles.begin_chunked_upload(self.slug, "figure.png", 5)
            bubbles.append_chunk(self.slug, begun["upload_id"], 0, b"other")
            bubbles.finish_chunked_upload(self.slug, begun["upload_id"])
            self.assertEqual((adir / "figure.png").read_bytes(), before)
            self.assertEqual((adir / "figure-2.png").read_bytes(), b"other")


class GuideMentionsLargeAssetsTests(unittest.TestCase):
    """The commands are useless if nobody is told they exist.

    Two audiences, two surfaces: the web help modal renders the Scientist CLI section, while a
    project's generated SKILL.md carries only the Editing Guide — and the agent is precisely who
    needs to know that writing a big file into the assets folder does not send it anywhere.
    """

    def test_the_web_help_documents_both_directions(self):
        guide = reports.guide_section("Scientist CLI")
        for expected in ("### Large files", "lockedin-scientist assets",
                         "assets pull", "assets push", "LOCKEDIN_SYNC_MAX_ASSET_BYTES"):
            self.assertIn(expected, guide)

    def test_the_agent_skill_documents_both_directions(self):
        guide = reports.guide_section("Editing Guide")
        for expected in ("## Large files", "lockedin-scientist assets",
                         "assets pull", "assets push"):
            self.assertIn(expected, guide)


if __name__ == "__main__":
    unittest.main()
