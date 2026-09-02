"""Regression tests for the size of a single sync request.

A proxy in front of the server caps one request body (Cloudflare stops at 100 MB). The sync used
to collect every changed file into one push, so a bubble that gained a few hundred ordinary
figures — each far below any per-file limit, together several hundred megabytes — produced one
request no proxy would accept, and the worker wedged with a 413 it could never get past. Batches
are therefore bounded by bytes, not by file count.
"""
from __future__ import annotations

import base64
import unittest

from lockedin.scientist_cli import REQUEST_PAYLOAD_BYTES, _batched_by_size


class BatchBySizeTests(unittest.TestCase):
    def test_it_splits_a_run_of_small_items_into_bounded_batches(self):
        items = [("f%d" % i, 4 * 1024 * 1024) for i in range(40)]   # 160 MB in 4 MB pieces
        batches = list(_batched_by_size(items, lambda item: item[1]))
        self.assertGreater(len(batches), 1)
        for batch in batches:
            self.assertLessEqual(sum(size for _, size in batch), REQUEST_PAYLOAD_BYTES)
        self.assertEqual([i for b in batches for i in b], items, "no item may be dropped")

    def test_one_oversized_item_still_goes_out_rather_than_being_dropped(self):
        items = [("huge", REQUEST_PAYLOAD_BYTES * 3)]
        self.assertEqual(list(_batched_by_size(items, lambda item: item[1])), [items])

    def test_an_empty_input_produces_no_requests(self):
        self.assertEqual(list(_batched_by_size([], lambda item: 0)), [])

    def test_the_real_failure_case_stays_under_the_proxy_limit(self):
        # 349 figures totalling ~395 MB is what wedged a real worker: base64 in one body is
        # ~527 MB against a 100 MB ceiling.
        writes = [{"path": f"reports/assets/photo-{i}.jpg",
                   "content_b64": base64.b64encode(b"x" * 1_132_000).decode()}
                  for i in range(349)]
        raw = lambda w: len(w["content_b64"]) * 3 // 4
        self.assertGreater(sum(raw(w) for w in writes) * 4 / 3, 100 * 1024 * 1024,
                           "the whole set must exceed the limit, or this proves nothing")
        for batch in _batched_by_size(writes, raw):
            encoded = sum(len(w["content_b64"]) for w in batch)
            self.assertLess(encoded, 100 * 1024 * 1024,
                            "one request must stay under what the proxy will carry")


if __name__ == "__main__":
    unittest.main()
