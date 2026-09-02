"""Regression tests for slicing a large asset across several requests.

Production sits behind a Cloudflare tunnel that rejects any request body over 100 MB, while the
origin has no such limit, so a big figure is uploaded in slices and reassembled here. These pin
the reassembly rules that keep a sliced upload from silently producing a corrupt file: slices
land in order, a replayed slice is absorbed rather than doubled, an incomplete or over-long
upload is refused, and staging is never mistaken for a real asset.

Run: ``uv run --with pytest python -m pytest tests/test_chunked_upload.py``
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from lockedin import bubbles, paths

from tests._fixtures import make_bubble


class ChunkedUploadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        for sub in ("ASSETS", "REPORTS", "config"):
            (self.home / sub).mkdir(parents=True, exist_ok=True)
        self.slug = make_bubble(self.home, "Chunked")
        self.ctx = paths.use_root(self.home)
        self.ctx.__enter__()

    def tearDown(self):
        self.ctx.__exit__(None, None, None)
        self._tmp.cleanup()

    def _begin(self, filename: str, total: int) -> str:
        return bubbles.begin_chunked_upload(self.slug, filename, total)["upload_id"]

    def _send(self, payload: bytes, chunk: int, filename="holiday photos.zip") -> str:
        upload_id = self._begin(filename, len(payload))
        for off in range(0, len(payload), chunk):
            bubbles.append_chunk(self.slug, upload_id, off, payload[off:off + chunk])
        return bubbles.finish_chunked_upload(self.slug, upload_id)

    def test_slices_reassemble_byte_for_byte(self):
        payload = os.urandom(300_000)
        url = self._send(payload, 32_768)
        self.assertTrue(url.endswith("/holiday-photos.zip"))
        landed = paths.bubble_assets_dir(self.slug) / "holiday-photos.zip"
        self.assertEqual(landed.read_bytes(), payload)

    def test_a_slice_that_arrives_twice_is_absorbed_not_doubled(self):
        # The response to a slice can be lost after the server stored it; the client resends.
        payload = os.urandom(1000)
        upload_id = self._begin("f.bin", len(payload))
        bubbles.append_chunk(self.slug, upload_id, 0, payload[:400])
        self.assertEqual(bubbles.append_chunk(self.slug, upload_id, 0, payload[:400]), 400)
        bubbles.append_chunk(self.slug, upload_id, 400, payload[400:])
        bubbles.finish_chunked_upload(self.slug, upload_id)
        self.assertEqual((paths.bubble_assets_dir(self.slug) / "f.bin").read_bytes(), payload)

    def test_a_misplaced_slice_is_refused(self):
        upload_id = self._begin("f.bin", 900)
        with self.assertRaises(ValueError):
            bubbles.append_chunk(self.slug, upload_id, 300, b"x" * 300)

    def test_an_upload_bigger_than_it_declared_is_refused(self):
        upload_id = self._begin("f.bin", 100)
        with self.assertRaises(ValueError):
            bubbles.append_chunk(self.slug, upload_id, 0, b"x" * 500)

    def test_an_incomplete_upload_cannot_be_finished(self):
        upload_id = self._begin("f.bin", 900)
        bubbles.append_chunk(self.slug, upload_id, 0, b"x" * 100)
        with self.assertRaises(ValueError):
            bubbles.finish_chunked_upload(self.slug, upload_id)
        self.assertFalse((paths.bubble_assets_dir(self.slug) / "f.bin").exists())

    def test_staging_is_not_listed_as_an_asset(self):
        # The listing walks assets/ recursively, so staging must live outside it.
        upload_id = self._begin("f.bin", 900)
        bubbles.append_chunk(self.slug, upload_id, 0, b"x" * 100)
        self.assertEqual(bubbles.list_bubble_assets(self.slug), [])

    def test_cancelling_discards_the_staged_bytes(self):
        upload_id = self._begin("f.bin", 900)
        bubbles.append_chunk(self.slug, upload_id, 0, b"x" * 100)
        self.assertTrue(bubbles.abort_chunked_upload(self.slug, upload_id))
        self.assertFalse(bubbles.abort_chunked_upload(self.slug, upload_id))
        with self.assertRaises(FileNotFoundError):
            bubbles.finish_chunked_upload(self.slug, upload_id)

    def test_an_upload_id_cannot_escape_the_staging_directory(self):
        for bad in ("../../etc", "..", "a/b", "", "NOTHEX" * 5, "0" * 31):
            with self.assertRaises(ValueError, msg=bad):
                bubbles.append_chunk(self.slug, bad, 0, b"x")
            self.assertFalse(bubbles.abort_chunked_upload(self.slug, bad))

    def test_a_second_upload_of_one_name_does_not_overwrite_the_first(self):
        self._send(b"first-file-contents", 8, filename="fig.png")
        self._send(b"second-file-contents", 8, filename="fig.png")
        adir = paths.bubble_assets_dir(self.slug)
        self.assertEqual((adir / "fig.png").read_bytes(), b"first-file-contents")
        self.assertEqual((adir / "fig-2.png").read_bytes(), b"second-file-contents")

    def test_re_offering_the_same_file_resumes_instead_of_restarting(self):
        # The failure that matters in production: a multi-gigabyte upload dies partway. The next
        # attempt must carry on from the bytes already accepted, not from zero.
        payload = os.urandom(1000)
        first = bubbles.begin_chunked_upload(self.slug, "big.zip", len(payload))
        self.assertEqual((first["received"], first["resumed"]), (0, False))
        bubbles.append_chunk(self.slug, first["upload_id"], 0, payload[:600])

        again = bubbles.begin_chunked_upload(self.slug, "big.zip", len(payload))
        self.assertEqual(again["upload_id"], first["upload_id"])
        self.assertEqual((again["received"], again["resumed"]), (600, True))

        bubbles.append_chunk(self.slug, again["upload_id"], 600, payload[600:])
        bubbles.finish_chunked_upload(self.slug, again["upload_id"])
        self.assertEqual((paths.bubble_assets_dir(self.slug) / "big.zip").read_bytes(), payload)

    def test_a_different_file_does_not_resume_someone_elses_session(self):
        first = bubbles.begin_chunked_upload(self.slug, "big.zip", 1000)
        bubbles.append_chunk(self.slug, first["upload_id"], 0, b"x" * 600)
        # Same name, different size, and same size, different name: neither may be mistaken for it.
        other_size = bubbles.begin_chunked_upload(self.slug, "big.zip", 2000)
        other_name = bubbles.begin_chunked_upload(self.slug, "other.zip", 1000)
        for got in (other_size, other_name):
            self.assertNotEqual(got["upload_id"], first["upload_id"])
            self.assertEqual((got["received"], got["resumed"]), (0, False))

    def test_a_cancelled_session_is_not_resumed(self):
        first = bubbles.begin_chunked_upload(self.slug, "big.zip", 1000)
        bubbles.append_chunk(self.slug, first["upload_id"], 0, b"x" * 600)
        bubbles.abort_chunked_upload(self.slug, first["upload_id"])
        again = bubbles.begin_chunked_upload(self.slug, "big.zip", 1000)
        self.assertEqual((again["received"], again["resumed"]), (0, False))


if __name__ == "__main__":
    unittest.main()
