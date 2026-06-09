"""Deterministic regression tests for the chat + save pipeline.

No network / no LLM: the model is replaced with a canned response so every case is exact and
reproducible.

The AI report-editing feature (``<EDIT>``/``<NEWPAGE>`` tags, section splicing, generate) was
removed — the web chat is now strictly READ-ONLY (discussion grounded in the reports + papers),
and editing is done by the user directly in the Markdown editor. These tests pin what remains:

* the chat returns prose only — never an edit/new-page proposal, and stray tags are scrubbed;
* math-delimiter normalization (``\\(..\\)`` → ``$..$``);
* wikilink normalization on save (title/prefix -> real slug);
* chat-title cleanup + fallback.

Run: ``uv run python -m unittest discover -s tests -t .``
"""
from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from lockedin import bubbles, models, paths, reports, server

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
    """Drive reports.chat_stream and return the terminal ``done`` event dict."""
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
# Read-only chat — discussion only, never proposes edits
# --------------------------------------------------------------------------- #
class ReadOnlyChat(unittest.TestCase):
    def test_chat_returns_prose_only(self):
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                bubbles.save_page(slug, "overview", PAGE)
            with canned_model("Score matching fits the gradient of the log-density."):
                done = run_chat(home, slug, "explain score matching", PAGE)
        # No edit/new-page machinery survives — the chat only carries text.
        self.assertNotIn("edit_proposal", done)
        self.assertNotIn("new_page_proposals", done)
        self.assertIn("full_response", done)
        self.assertIn("Score matching", done["chat_text"])

    def test_stray_edit_tags_are_scrubbed_from_display(self):
        # A weak model may still echo a stray tag despite the read-only prompt — it must not
        # leak raw XML into the displayed message.
        canned = 'Sure. <EDIT section="## Overview">stuff</EDIT> Done.'
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                bubbles.save_page(slug, "overview", PAGE)
            with canned_model(canned):
                done = run_chat(home, slug, "add stuff", PAGE)
        self.assertNotIn("<EDIT", done["chat_text"])
        self.assertNotIn("</EDIT>", done["chat_text"])

    def test_chat_never_writes_the_page(self):
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                bubbles.save_page(slug, "overview", PAGE)
            with canned_model("Here is some text you could paste: new paragraph."):
                run_chat(home, slug, "rewrite the overview", PAGE)
            with paths.use_root(home):
                self.assertEqual(bubbles.get_page(slug, "overview"), PAGE)


# --------------------------------------------------------------------------- #
# Wikilink normalization on save (lives in bubbles.save_page)
# --------------------------------------------------------------------------- #
class WikilinkNormalization(unittest.TestCase):
    def test_title_and_prefix_forms_resolve_to_slug(self):
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                a = bubbles.create_page(slug, "Paper Alpha")
                b = bubbles.create_page(slug, "Paper Beta")
                bubbles.save_page(slug, "overview",
                                  "# T\n\nSee [[Paper Alpha]] and [[Key Papers/Paper Beta]].\n")
                stored = bubbles.get_page(slug, "overview")
        self.assertIn(f"[[{a}]]", stored)
        self.assertIn(f"[[{b}]]", stored)

    def test_unknown_target_is_left_clean(self):
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                bubbles.save_page(slug, "overview", "# T\n\n[[Nonexistent Page]]\n")
                stored = bubbles.get_page(slug, "overview")
        self.assertIn("[[Nonexistent Page]]", stored)


# --------------------------------------------------------------------------- #
# Bubble identity: the slug is identity, the display name is cosmetic. Re-tagging
# a PDF into an existing bubble (register_user_tags -> create_bubble on upload) must
# NOT clobber a rename or its share state, and membership must follow the slug.
# --------------------------------------------------------------------------- #
class BubbleIdentity(unittest.TestCase):
    def test_retagging_preserves_rename_and_share(self):
        with temp_home() as home:
            with paths.use_root(home):
                slug = bubbles.create_bubble("diffusion models")
                bubbles.set_share_active(slug, True)
                bubbles.rename_bubble(slug, "Diffusion & Flow Models")
                # Uploading a file tagged with the bubble re-runs create_bubble for that slug.
                bubbles.create_bubble(bubbles.tag_for_slug(slug))
                entry = bubbles.load_registry()[slug]
        self.assertEqual(entry["name"], "Diffusion & Flow Models")   # rename survives
        self.assertTrue(entry.get("share_active"))                   # share survives
        self.assertTrue(entry["approved"])

    def test_tag_for_slug_round_trips_after_rename(self):
        with temp_home() as home:
            with paths.use_root(home):
                slug = bubbles.create_bubble("diffusion models")
                bubbles.rename_bubble(slug, "Totally Different Name")
                tag = bubbles.tag_for_slug(slug)
        # The membership tag must still slugify back to the original slug, never the new name.
        from lockedin import assets
        self.assertEqual(assets.slug_of(tag), slug)


# --------------------------------------------------------------------------- #
# Autosave optimistic-concurrency guard (bubbles.save_page base_mtime)
# --------------------------------------------------------------------------- #
class SaveConflictDetection(unittest.TestCase):
    def test_save_returns_new_mtime_and_no_base_always_writes(self):
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                m1 = bubbles.save_page(slug, "overview", "# A\n")
                # No base_mtime -> unconditional write (every existing caller relies on this).
                m2 = bubbles.save_page(slug, "overview", "# B\n")
                self.assertEqual(bubbles.get_page(slug, "overview"), "# B\n")
        self.assertIsInstance(m1, float)
        self.assertIsInstance(m2, float)

    def test_matching_base_mtime_saves(self):
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                base = bubbles.save_page(slug, "overview", "# A\n")
                # Editor's base matches disk -> save goes through.
                bubbles.save_page(slug, "overview", "# edited\n", base_mtime=base)
                self.assertEqual(bubbles.get_page(slug, "overview"), "# edited\n")

    def test_stale_base_mtime_raises_conflict_and_preserves_disk(self):
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                base = bubbles.save_page(slug, "overview", "# A\n")
                # Something else (e.g. dev-mode) writes the file out-of-band. Force a
                # distinct mtime so the guard is exercised regardless of fs resolution.
                bubbles.save_page(slug, "overview", "# external edit\n")
                page = paths.bubble_page_path(slug, "overview")
                os.utime(page, (base + 5, base + 5))
                # The editor still thinks it's based on the old mtime -> conflict, no clobber.
                with self.assertRaises(bubbles.PageConflict):
                    bubbles.save_page(slug, "overview", "# my stale edit\n", base_mtime=base)
                self.assertEqual(bubbles.get_page(slug, "overview"), "# external edit\n")


# --------------------------------------------------------------------------- #
# Chat titles
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Math normalization
# --------------------------------------------------------------------------- #
class MathNormalization(unittest.TestCase):
    def test_inline_and_display_delimiters_converted(self):
        self.assertEqual(reports._normalize_math(r"the value \(x^2\) is"), "the value $x^2$ is")
        self.assertEqual(reports._normalize_math(r"\[ E = mc^2 \]"), "$$E = mc^2$$")

    def test_existing_dollar_math_untouched(self):
        src = "inline $a+b$ and display $$\\int_0^1 f$$"
        self.assertEqual(reports._normalize_math(src), src)

    def test_normalization_applied_in_chat(self):
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                bubbles.save_page(slug, "overview", PAGE)
            with canned_model(r"The score is \(\nabla_x \log p(x)\) here."):
                done = run_chat(home, slug, "explain the score", PAGE)
        self.assertIn(r"$\nabla_x \log p(x)$", done["chat_text"])
        self.assertNotIn(r"\(", done["chat_text"])


class PreviewMathRendering(unittest.TestCase):
    """Guard: preview/share pages stash math BEFORE marked.js, mirroring the editor side
    pane. Running marked.parse first lets markdown mangle LaTeX (e.g. the two `_` in a line
    of display math become an <em> pair, dropping the subscripts) before KaTeX ever sees it."""

    EQ = (r"$$\delta E = \frac{1}{2} \int_0^1 dt \bigg|_{s=0} = "
          r"\langle V'(t), \gamma'(t) \rangle \bigg|_{s=0}$$")

    def _render(self):
        return server._render_preview_html(
            name="B", page="P", all_pages=[{"page_slug": "p", "title": "P"}],
            content=self.EQ, slug="s", link_base="/x", asset_base="/x/assets",
            show_back=False)

    def test_raw_latex_embedded_untouched(self):
        # The exact LaTeX source (underscores intact) must reach the client verbatim.
        self.assertIn(r"\bigg|_{s=0}", self._render())

    def test_stashes_before_marked_not_autorender(self):
        html = self._render()
        # Correct path: stash → marked.parse(s) → katex.renderToString from raw source.
        self.assertIn("katex.renderToString", html)
        self.assertIn("@@M", html)
        self.assertIn("marked.parse(s)", html)
        # Buggy path (marked first, then auto-render over the DOM) must be gone.
        self.assertNotIn("renderMathInElement", html)


if __name__ == "__main__":
    unittest.main()
