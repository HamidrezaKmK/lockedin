"""Deterministic regression tests for the report-editing pipeline.

No network / no LLM: the model is replaced with a canned response so every case is exact and
reproducible. Each test pins one of the bugs we hit so it can never silently come back:

* section splicing (full-page-under-section, bracketed headings, missing headings, new sections)
* tag parsing tolerance (missing close tags) + bare-<EDIT> rejection
* <NEWPAGE> deferral (proposed, not created) and no raw-tag leakage
* wikilink normalization on save (title/prefix -> real slug)

Run: ``uv run python -m unittest discover -s tests -t .``
"""
from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from lockedin import bubbles, models, paths, reports

from tests._fixtures import make_bubble


@contextmanager
def temp_home():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        for sub in ("ASSETS", "REPORTS", "config"):
            (home / sub).mkdir(parents=True, exist_ok=True)
        yield home


@contextmanager
def canned_model(text: str):
    """Replace models.stream_chat with a generator yielding ``text`` in two chunks."""
    orig = models.stream_chat

    def fake(home, messages, system=None, temperature=0.3, *, claude_token=""):
        mid = len(text) // 2
        yield text[:mid]
        yield text[mid:]

    models.stream_chat = fake
    try:
        yield
    finally:
        models.stream_chat = orig


def run_chat(home, slug, prompt, page_context):
    """Drive reports.chat_stream and return the terminal event dict."""
    done = None
    for ev in reports.chat_stream(home, slug, "overview",
                                  [{"role": "user", "content": prompt}],
                                  page_context=page_context):
        if ev["type"] == "done":
            done = ev
        elif ev["type"] == "error":
            raise AssertionError("unexpected error event: " + ev["detail"])
    assert done is not None, "no done event"
    return done


PAGE = ("# Diffusion Models\n\n## Overview\n\nOld overview text.\n\n"
        "## Key Papers\n\n- [[a]]\n- [[b]]\n")


# --------------------------------------------------------------------------- #
# _splice_section — the duplication bug lived here
# --------------------------------------------------------------------------- #
class SpliceSection(unittest.TestCase):
    def test_full_page_under_section_replaces_not_appends(self):
        # Model returned a whole page (H1) under a section edit -> must REPLACE, not duplicate.
        out = reports._splice_section(PAGE, "## [Overview]",
                                      "# Diffusion Models\n\nBrief intro.\n\n### Links\n[[a]]")
        self.assertEqual(out.count("# Diffusion Models"), 1)
        self.assertIn("Brief intro.", out)
        self.assertNotIn("Old overview text.", out)

    def test_bracketed_heading_matches_real_section(self):
        out = reports._splice_section(PAGE, "## [Overview]", "## Overview\n\nNew text.")
        self.assertEqual(out.count("## Overview"), 1)
        self.assertIn("New text.", out)
        self.assertNotIn("Old overview text.", out)
        self.assertIn("## Key Papers", out)  # other section untouched

    def test_reattaches_missing_heading(self):
        out = reports._splice_section(PAGE, "## Key Papers", "- [[a]]\n- [[b]]\n- [[c]]")
        self.assertEqual(out.count("## Key Papers"), 1)
        self.assertIn("[[c]]", out)

    def test_unknown_section_appends_once(self):
        out = reports._splice_section(PAGE, "## My Ideas", "## My Ideas\n\nA thought.")
        self.assertEqual(out.count("## My Ideas"), 1)
        self.assertIn("A thought.", out)
        self.assertIn("Old overview text.", out)  # nothing destroyed


# --------------------------------------------------------------------------- #
# Tag parsing — tolerance + bare-<EDIT> rejection
# --------------------------------------------------------------------------- #
class TagParsing(unittest.TestCase):
    def test_newpage_without_closing_tag(self):
        raw = ('<NEWPAGE title="A">\n# A\nbody A\n'
               '<NEWPAGE title="B">\n# B\nbody B\n')  # neither closed
        found = [(m.group(1), m.group(2).strip()) for m in reports._NEWPAGE_RE.finditer(raw)]
        self.assertEqual([t for t, _ in found], ["A", "B"])
        self.assertIn("body A", found[0][1])
        self.assertNotIn("<NEWPAGE", found[0][1])

    def test_edit_without_closing_tag(self):
        raw = '<EDIT section="## Key Papers">\n## Key Papers\n- [[a]]'
        m = next((x for x in reports._EDIT_RE.finditer(raw)
                  if x.group(1) or x.group(2) == "true"), None)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "## Key Papers")
        self.assertIn("[[a]]", m.group(3))

    def test_bare_edit_is_not_a_valid_edit(self):
        raw = "Sure.\n<EDIT>\nTo edit one section:\n"
        valid = [x for x in reports._EDIT_RE.finditer(raw) if x.group(1) or x.group(2) == "true"]
        self.assertEqual(valid, [])

    def test_full_page_edit_flag_captured(self):
        raw = '<EDIT page="true">\n# T\n## X\nbody\n</EDIT>'
        m = next(reports._EDIT_RE.finditer(raw))
        self.assertIsNone(m.group(1))
        self.assertEqual(m.group(2), "true")


# --------------------------------------------------------------------------- #
# chat_stream end-to-end with a canned model
# --------------------------------------------------------------------------- #
class ChatStream(unittest.TestCase):
    def test_section_edit_does_not_duplicate_page(self):
        # The exact failure from the bug report: bracketed section + whole-page body.
        canned = ('Done.\n<EDIT section="## [Overview]">\n'
                  '# Diffusion Models\n\nA brief overview.\n\n'
                  '### Links\n[[generative-models-via-drifting]]\n</EDIT>')
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                bubbles.save_page(slug, "overview", PAGE)
            with canned_model(canned):
                done = run_chat(home, slug, "dedupe and shorten the overview", PAGE)
            content = done["edit_proposal"]["content"]
            self.assertEqual(content.count("# Diffusion Models"), 1, content)
            self.assertNotIn("Old overview text.", content)

    def test_commentary_stays_out_of_edit_content(self):
        canned = ('I removed the duplicate links.\n<EDIT section="## Key Papers">\n'
                  '## Key Papers\n- [[a]]\n- [[b]]\n</EDIT>')
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                bubbles.save_page(slug, "overview", PAGE)
            with canned_model(canned):
                done = run_chat(home, slug, "fix dupes", PAGE)
            self.assertIn("removed the duplicate", done["chat_text"].lower())
            self.assertNotIn("I removed the duplicate", done["edit_proposal"]["content"])

    def test_bare_edit_yields_no_proposal_and_scrubs_tag(self):
        canned = "Sure thing.\n<EDIT>\nTo edit one section:\n"
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                bubbles.save_page(slug, "overview", PAGE)
            with canned_model(canned):
                done = run_chat(home, slug, "help", PAGE)
            self.assertNotIn("edit_proposal", done)
            self.assertNotIn("<EDIT", done["chat_text"])

    def test_newpages_are_proposed_not_created(self):
        canned = ('Adding pages.\n'
                  '<NEWPAGE title="Paper One">\n# Paper One\nContent one.\n</NEWPAGE>\n'
                  '<NEWPAGE title="Paper Two">\n# Paper Two\nContent two.\n')  # 2nd unclosed
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                bubbles.save_page(slug, "overview", PAGE)
                before = {p["page_slug"] for p in bubbles.list_pages(slug)}
            with canned_model(canned):
                done = run_chat(home, slug, "add a page per paper", PAGE)
            titles = [p["title"] for p in done["new_page_proposals"]]
            self.assertEqual(titles, ["Paper One", "Paper Two"])
            with paths.use_root(home):
                after = {p["page_slug"] for p in bubbles.list_pages(slug)}
            self.assertEqual(before, after, "pages must NOT be created at proposal time")
            for p in done["new_page_proposals"]:
                self.assertNotIn("<NEWPAGE", p["content"])

    def test_full_page_edit_returns_whole_page(self):
        canned = '<EDIT page="true">\n# Diffusion Models\n\n## Overview\n\nFresh.\n</EDIT>'
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                bubbles.save_page(slug, "overview", PAGE)
            with canned_model(canned):
                done = run_chat(home, slug, "rewrite the page", PAGE)
            self.assertIsNone(done["edit_proposal"]["section"])
            self.assertIn("Fresh.", done["edit_proposal"]["content"])


# --------------------------------------------------------------------------- #
# Wikilink normalization on save
# --------------------------------------------------------------------------- #
class WikilinkNormalization(unittest.TestCase):
    def test_title_and_prefix_forms_resolve_to_slug(self):
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                a = bubbles.create_page(slug, "Paper Alpha")
                b = bubbles.create_page(slug, "Paper Beta")
                bubbles.save_page(slug, "overview",
                                  "# T\n\n## Key Papers\n- [[Paper Alpha]]\n- [[Key Papers/Paper Beta]]\n")
                stored = bubbles.get_page(slug, "overview")
            self.assertIn(f"[[{a}]]", stored)
            self.assertIn(f"[[{b}]]", stored)
            self.assertNotIn("Paper Alpha]]", stored)
            self.assertNotIn("Key Papers/", stored)

    def test_unknown_target_is_left_clean(self):
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                bubbles.save_page(slug, "overview", "# T\n\n[[Nonexistent Page]]\n")
                stored = bubbles.get_page(slug, "overview")
            self.assertIn("[[Nonexistent Page]]", stored)


class ChatTitle(unittest.TestCase):
    def test_clean_title_strips_quotes_markdown_punctuation(self):
        self.assertEqual(reports._clean_title('"✨ Diffusion Deep Dive".', "fb"), "✨ Diffusion Deep Dive")
        self.assertEqual(reports._clean_title("**Score-Based SDEs**", "fb"), "Score-Based SDEs")
        self.assertEqual(reports._clean_title("", "My Fallback"), "My Fallback")

    def test_generate_title_uses_model_output(self):
        msgs = [{"role": "user", "content": "Explain score matching"},
                {"role": "assistant", "content": "Score matching fits the gradient of log-density."}]
        with temp_home() as home, canned_model("🌊 Score Matching Basics"):
            title = reports.generate_chat_title(home, msgs)
        self.assertEqual(title, "🌊 Score Matching Basics")

    def test_generate_title_falls_back_on_empty_model_output(self):
        msgs = [{"role": "user", "content": "What is FLIPD and how does it estimate dimension?"}]
        with temp_home() as home, canned_model("   "):
            title = reports.generate_chat_title(home, msgs)
        self.assertTrue(title.startswith("What is FLIPD"))


class MathNormalization(unittest.TestCase):
    def test_inline_and_display_delimiters_converted(self):
        self.assertEqual(reports._normalize_math(r"the value \(x^2\) is"), "the value $x^2$ is")
        self.assertEqual(reports._normalize_math(r"\[ E = mc^2 \]"), "$$E = mc^2$$")

    def test_existing_dollar_math_untouched(self):
        src = "inline $a+b$ and display $$\\int_0^1 f$$"
        self.assertEqual(reports._normalize_math(src), src)

    def test_normalization_applied_in_chat_edit(self):
        canned = ('<EDIT section="## Overview">\n## Overview\n'
                  r'The score is \(\nabla_x \log p(x)\) here.' "\n</EDIT>")
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                bubbles.save_page(slug, "overview", PAGE)
            with canned_model(canned):
                done = run_chat(home, slug, "explain the score", PAGE)
            content = done["edit_proposal"]["content"]
            self.assertIn(r"$\nabla_x \log p(x)$", content)
            self.assertNotIn(r"\(", content)


if __name__ == "__main__":
    unittest.main()
