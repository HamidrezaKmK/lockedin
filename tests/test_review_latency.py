"""Performance regressions for the server-owned review wrapper lifecycle.

These checks deliberately exercise the real page-save transaction (parser, sidecar
projection, and atomic file replacement) with a report much larger than a normal page.
They are not microbenchmarks: the generous one-second ceiling is a release guard against
accidentally bringing back the former whole-document diff/rebase path.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from lockedin import bubbles, paths


class ReviewSaveLatencyTests(unittest.TestCase):
    COMMENT_COUNT = 400

    def test_large_page_with_hundreds_of_comments_saves_under_one_second(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for child in ("ASSETS", "REPORTS", "config"):
                (home / child).mkdir(parents=True, exist_ok=True)

            with paths.use_root(home):
                slug = bubbles.create_bubble("Review latency")
                bubbles.ensure_pages(slug)

                chunks = ["# Large review page\n\n"]
                threads = []
                for index in range(self.COMMENT_COUNT):
                    thread_id = f"review-{index:04d}-variable-length"
                    body = (
                        f"Selected multiline passage {index} with $x_{{{index}}}$ and "
                        "escaped \\{braces\\}.\nThe second selected line stays attached."
                    )
                    chunks.append(
                        ("Background material " + ("x" * 900) + "\n\n")
                        + f"\\comment{{{thread_id}}}{{{body}}}\n\n"
                    )
                    threads.append(
                        {
                            "id": thread_id,
                            "page_slug": "overview",
                            "status": "open",
                            "anchor_state": "attached",
                            "created_at": "2026-01-01T00:00:00+00:00",
                            "updated_at": "2026-01-01T00:00:00+00:00",
                            "resolved_at": "",
                            "resolved_by": "",
                            "anchor": {"quote": body, "start": 0, "end": len(body)},
                            "messages": [],
                        }
                    )
                content = "".join(chunks)

                page_path = paths.bubble_page_path(slug, "overview")
                comments_path = paths.bubble_page_comments_path(slug, "overview")
                page_path.write_text(content)
                comments_path.parent.mkdir(parents=True, exist_ok=True)
                comments_path.write_text(
                    json.dumps({"version": 2, "threads": threads}, indent=2) + "\n"
                )
                base_mtime = page_path.stat().st_mtime

                started = time.perf_counter()
                state = bubbles.save_page_state(slug, "overview", content, base_mtime)
                elapsed = time.perf_counter() - started

                self.assertLess(
                    elapsed,
                    1.0,
                    f"large review page save took {elapsed:.3f}s; expected sub-second",
                )
                self.assertEqual(state["content"], content)
                self.assertEqual(len(state["threads"]), self.COMMENT_COUNT)
                self.assertTrue(
                    all(thread["anchor_state"] == "attached" for thread in state["threads"])
                )
                self.assertEqual(
                    len(bubbles.parse_comment_wrappers(state["content"])), self.COMMENT_COUNT
                )


if __name__ == "__main__":
    unittest.main()
