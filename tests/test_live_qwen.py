"""Live integration test: a realistic editing interaction driven by the real qwen model.

Unlike test_editing_logic (canned model), this exercises the *actual* model end-to-end to catch
prompt-level regressions — e.g. the model copying ``## [Overview]`` into a section edit and
duplicating the page. qwen is non-deterministic, so we assert structural INVARIANTS that must
hold for any sane output, retrying a couple of times to ride out connection blips / off turns.

Skipped automatically when Ollama/qwen is unreachable or the diffusion PDFs aren't present
locally (run ``uv run python -m tests.setup_unittest_user`` to provision them).

Run: ``uv run python -m unittest tests.test_live_qwen -v``
"""
from __future__ import annotations

import re
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
class LiveQwenEditing(unittest.TestCase):
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

    def test_dedupe_does_not_duplicate_the_page(self):
        # Try a few phrasings until the model proposes an edit; then check the invariant.
        for prompt in ("Edit the overview so each page is linked only once.",
                       "The Key Papers links are duplicated — remove the duplicates.",
                       "Dedupe the links in the overview."):
            done = self._chat(prompt)
            ep = done.get("edit_proposal")
            self._assert_no_raw_tags(done.get("chat_text", ""))
            if not ep:
                continue
            # Apply the proposal exactly as Accept would, then check the saved page.
            write_overview(self.home, ep["content"])
            saved = read_overview(self.home)
            self._assert_no_raw_tags(saved)
            self.assertEqual(saved.count("# Diffusion Models"), 1,
                             f"page title duplicated — the append bug is back:\n{saved}")
            # every wikilink resolves to a real page slug after normalization-on-save
            with paths.use_root(self.home):
                slugs = {p["page_slug"] for p in bubbles.list_pages(DIFFUSION_BUBBLE)}
            for tgt in re.findall(r"\[\[([^\]]+)\]\]", saved):
                self.assertIn(tgt, slugs, f"dead link [[{tgt}]] in saved overview")
            return
        self.skipTest("qwen never proposed an edit across phrasings (model off turn)")

    def test_new_pages_are_not_created_during_chat(self):
        # Regardless of what the model proposes, chat must not create pages itself.
        with paths.use_root(self.home):
            before = {p["page_slug"] for p in bubbles.list_pages(DIFFUSION_BUBBLE)}
        done = self._chat("Create a dedicated page for each of the two papers.")
        self._assert_no_raw_tags(done.get("chat_text", ""))
        with paths.use_root(self.home):
            after = {p["page_slug"] for p in bubbles.list_pages(DIFFUSION_BUBBLE)}
        self.assertEqual(before, after, "chat created pages without per-page confirmation")
        for p in (done.get("new_page_proposals") or []):
            self._assert_no_raw_tags(p["content"])


if __name__ == "__main__":
    unittest.main()
