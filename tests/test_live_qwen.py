"""Live integration test: a realistic read-only chat driven by the real qwen model.

Unlike test_editing_logic (canned model), this exercises the *actual* model end-to-end. The chat
is READ-ONLY (no editing), so we assert the invariants that must hold for any sane output:
the chat never mutates a page, and no raw ``<EDIT>``/``<NEWPAGE>`` tags leak into the displayed
reply. qwen is non-deterministic, so we retry a couple of times to ride out blips / off turns.

Skipped automatically when Ollama/qwen is unreachable or the diffusion PDFs aren't present
locally (run ``uv run python -m tests.setup_unittest_user`` to provision them).

Run: ``uv run python -m unittest tests.test_live_qwen -v``
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from lockedin import bubbles, paths, reports

from tests._fixtures import (DIFFUSION_BUBBLE, qwen_reachable, read_overview,
                             seed_diffusion_workspace, set_qwen, source_user_with_pdfs,
                             write_overview)

_SOURCE = source_user_with_pdfs()
_SKIP = not qwen_reachable() or _SOURCE is None
_SKIP_REASON = ("qwen (Ollama) not reachable" if not qwen_reachable()
                else "diffusion PDFs not found locally — run tests.setup_unittest_user")

OVERVIEW_WITH_DUPES = (
    "# Diffusion Models\n\n## Overview\n\nDiffusion models are a powerful class of "
    "generative models.\n\n## Key Papers\n\n"
    "- [[generative-models-via-drifting]]\n- [[a-geometric-view-of-data-complexity]]\n"
    "- [[generative-models-via-drifting]]\n- [[a-geometric-view-of-data-complexity]]\n")

RAW_TAGS = ("<EDIT", "</EDIT", "<NEWPAGE", "</NEWPAGE")


@unittest.skipIf(_SKIP, _SKIP_REASON)
class LiveQwenChat(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        for sub in ("ASSETS", "REPORTS", "config"):
            (self.home / sub).mkdir(parents=True, exist_ok=True)
        set_qwen(self.home)
        seed_diffusion_workspace(self.home, source_user=_SOURCE)
        write_overview(self.home, OVERVIEW_WITH_DUPES)

    def tearDown(self):
        self._tmp.cleanup()

    def _chat(self, prompt, attempts=3):
        """Return the done event for a prompt, retrying past connection blips."""
        page_ctx = read_overview(self.home)
        last_err = None
        for _ in range(attempts):
            done = None
            for ev in reports.chat_stream(self.home, DIFFUSION_BUBBLE, "overview",
                                          [{"role": "user", "content": prompt}],
                                          page_context=page_ctx):
                if ev["type"] == "done":
                    done = ev
                elif ev["type"] == "error":
                    last_err = ev["detail"]
            if done is not None:
                return done
            time.sleep(2)
        self.skipTest(f"qwen produced no response after {attempts} tries ({last_err})")

    def _assert_no_raw_tags(self, text):
        for tag in RAW_TAGS:
            self.assertNotIn(tag, text, f"raw tag {tag} leaked into saved content:\n{text}")

    def test_chat_answers_without_raw_tags(self):
        # A normal content question should get a non-empty reply with no leaked edit tags.
        done = self._chat("Briefly, what problem do these papers address?")
        text = done.get("chat_text", "")
        self.assertTrue(text.strip(), "qwen returned an empty reply")
        self._assert_no_raw_tags(text)

    def test_chat_never_mutates_pages(self):
        # Even when asked to edit, the read-only chat must not change or create any page.
        with paths.use_root(self.home):
            before_pages = {p["page_slug"] for p in bubbles.list_pages(DIFFUSION_BUBBLE)}
        before_overview = read_overview(self.home)
        done = self._chat("Rewrite the overview and add a new page for each paper.")
        self._assert_no_raw_tags(done.get("chat_text", ""))
        with paths.use_root(self.home):
            after_pages = {p["page_slug"] for p in bubbles.list_pages(DIFFUSION_BUBBLE)}
        self.assertEqual(before_pages, after_pages, "chat created/removed pages — it must be read-only")
        self.assertEqual(before_overview, read_overview(self.home),
                         "chat mutated the overview — it must be read-only")


if __name__ == "__main__":
    unittest.main()
