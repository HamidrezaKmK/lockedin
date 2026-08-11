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
import re
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from lockedin import assets, bubbles, models, paths, reports, server, service, tagger

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
    def test_legacy_auto_suggestions_are_purged_without_touching_real_bubbles(self):
        with temp_home() as home:
            with paths.use_root(home):
                stale = bubbles.propose_bubble("Old model suggestion")
                stale_dir = paths.bubble_dir(stale)
                stale_dir.mkdir(parents=True)
                (stale_dir / "old.md").write_text("obsolete")
                real = bubbles.create_bubble("My actual research")

                self.assertEqual(bubbles.purge_legacy_auto_suggestions(), [stale])
                self.assertNotIn(stale, bubbles.load_registry())
                self.assertFalse(stale_dir.exists())
                self.assertTrue(bubbles.load_registry()[real]["approved"])
                self.assertEqual(bubbles.purge_legacy_auto_suggestions(), [])

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

    def test_all_bubbles_scans_asset_metadata_once(self):
        with temp_home() as home:
            with paths.use_root(home):
                bubbles.create_bubble("first topic")
                bubbles.create_bubble("second topic")
                assets.save_asset(b"%PDF-1", "one.pdf", tags=["first topic"])
                assets.save_asset(b"%PDF-1", "two.pdf", tags=["second topic"])
                original = assets.list_assets
                calls = 0

                def counted_list_assets():
                    nonlocal calls
                    calls += 1
                    return original()

                assets.list_assets = counted_list_assets
                try:
                    rows = bubbles.all_bubbles()
                finally:
                    assets.list_assets = original
        self.assertEqual(calls, 1)
        self.assertEqual({row["slug"] for row in rows}, {"first-topic", "second-topic"})

    def test_archived_bubbles_are_reversible_and_hidden_from_active_inventory(self):
        with temp_home() as home:
            active = service.create_bubble(home, "Active topic")
            archived = service.create_bubble(home, "Archived topic")
            with paths.use_root(home):
                bubbles.ensure_pages(archived)
                bubbles.save_page(archived, "overview", "# Preserved\n")
            result = service.set_bubble_archived(home, archived, True)
            self.assertTrue(result["archived"])
            self.assertEqual([b["slug"] for b in service.list_bubbles(home)], [active])
            self.assertEqual([b["slug"] for b in service.list_bubbles(home, archived=True)], [archived])
            self.assertEqual(service.bubble_detail(home, archived)["content"], "# Preserved\n")
            result = service.set_bubble_archived(home, archived, False)
            self.assertFalse(result["archived"])
            self.assertEqual({b["slug"] for b in service.list_bubbles(home)}, {active, archived})


# --------------------------------------------------------------------------- #
# Copying papers between bubbles (service.migrate_papers). Membership is a tag on a shared
# asset, so a migration must add to the destination and never disturb the source.
# --------------------------------------------------------------------------- #
class PaperMigration(unittest.TestCase):
    def _workspace(self, home):
        """Two bubbles; one paper in the source at a non-default relevance."""
        with paths.use_root(home):
            source = bubbles.create_bubble("Source topic")
            dest = bubbles.create_bubble("Dest topic")
            pdf_id = assets.save_asset(b"%PDF-1", "paper.pdf", tags=[bubbles.tag_for_slug(source)])
            bubbles.set_pdf_bubble_score(source, pdf_id, 2)
        return source, dest, pdf_id

    def _score(self, home, slug, pdf_id):
        with paths.use_root(home):
            for m in bubbles.pdfs_for_bubble(slug):
                if m["pdf_id"] == pdf_id:
                    return int(m["bubble_score"])
        return None

    def test_copy_adds_to_destination_and_leaves_the_source_untouched(self):
        with temp_home() as home:
            source, dest, pdf_id = self._workspace(home)
            out = service.migrate_papers(home, source, dest, [{"pdf_id": pdf_id, "score": 4}])
            self.assertEqual(out["migrated"], [pdf_id])
            self.assertEqual(self._score(home, dest, pdf_id), 4)
            # The source keeps both the paper and its own relevance.
            self.assertEqual(self._score(home, source, pdf_id), 2)

    def test_a_paper_already_in_the_destination_keeps_its_relevance(self):
        with temp_home() as home:
            source, dest, pdf_id = self._workspace(home)
            service.migrate_papers(home, source, dest, [{"pdf_id": pdf_id, "score": 3}])
            out = service.migrate_papers(home, source, dest, [{"pdf_id": pdf_id, "score": 1}])
            self.assertEqual(out["migrated"], [])
            self.assertEqual(out["skipped"], [pdf_id])
            # Not re-scored behind the user's back: the picker never showed this row.
            self.assertEqual(self._score(home, dest, pdf_id), 3)

    def test_an_asset_outside_the_source_bubble_is_never_tagged(self):
        with temp_home() as home:
            source, dest, _ = self._workspace(home)
            with paths.use_root(home):
                other = assets.save_asset(b"%PDF-1", "unrelated.pdf", tags=["something else"])
            out = service.migrate_papers(home, source, dest, [{"pdf_id": other}])
            self.assertEqual(out["migrated"], [])
            self.assertEqual(out["skipped"], [other])
            with paths.use_root(home):
                self.assertNotIn(dest, assets.load_meta(other).get("idea_bubbles", []))

    def test_same_bubble_and_unknown_bubble_are_rejected(self):
        with temp_home() as home:
            source, dest, pdf_id = self._workspace(home)
            with self.assertRaises(ValueError):
                service.migrate_papers(home, source, source, [])
            with self.assertRaises(KeyError):
                service.migrate_papers(home, source, "no-such-bubble", [])

    def test_a_rejected_score_tags_nothing_at_all(self):
        # Tagging happens before scoring, so an out-of-range score must be caught up front or the
        # paper lands in the destination unscored — a half-applied migration.
        with temp_home() as home:
            source, dest, pdf_id = self._workspace(home)
            with self.assertRaises(ValueError):
                service.migrate_papers(home, source, dest, [{"pdf_id": pdf_id, "score": 9}])
            with paths.use_root(home):
                self.assertNotIn(dest, assets.load_meta(pdf_id).get("idea_bubbles", []))
                self.assertEqual(bubbles.pdfs_for_bubble(dest), [])

    def test_a_renamed_destination_still_receives_the_paper_under_its_own_slug(self):
        # The phantom-slug regression: tagging by display name would split the bubble.
        with temp_home() as home:
            source, dest, pdf_id = self._workspace(home)
            with paths.use_root(home):
                bubbles.rename_bubble(dest, "Completely Different Name")
            service.migrate_papers(home, source, dest, [{"pdf_id": pdf_id, "score": 5}])
            with paths.use_root(home):
                slugs = assets.load_meta(pdf_id).get("idea_bubbles", [])
        self.assertIn(dest, slugs)
        self.assertNotIn("completely-different-name", slugs)

    def test_destination_inventory_reflects_the_new_relevance(self):
        # _lockedin_papers.md is what a Scientist session reads; set_pdf_bubble_score alone does
        # not refresh it, so the batch must rewrite it at the end.
        with temp_home() as home:
            source, dest, pdf_id = self._workspace(home)
            service.migrate_papers(home, source, dest, [{"pdf_id": pdf_id, "score": 4}])
            with paths.use_root(home):
                inventory = (paths.bubble_dir(dest) / "_lockedin_papers.md").read_text()
        self.assertIn("[Relevance 4]", inventory)
        self.assertIn(pdf_id, inventory)


class BubbleRelevance(unittest.TestCase):
    def test_legacy_membership_defaults_to_score_five_and_sorts(self):
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                low = assets.save_asset(b"%PDF-1", "b.pdf", title="B", tags=["Diffusion Models"])
                high = assets.save_asset(b"%PDF-1", "a.pdf", title="A", tags=["Diffusion Models"])
                bubbles.set_pdf_bubble_score(slug, low, 2)
                detail = bubbles.bubble_detail(slug)
        self.assertEqual(detail["assets"][0]["pdf_id"], high)
        self.assertEqual(detail["assets"][0]["bubble_score"], 5)
        self.assertEqual(detail["assets"][1]["pdf_id"], low)
        self.assertEqual(detail["assets"][1]["bubble_score"], 2)

    def test_add_remove_and_score_update_maintain_bubble_scores(self):
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                pid = assets.save_asset(b"%PDF-1", "a.pdf", title="A")
                bubbles.add_pdf_to_bubble(slug, pid)
                meta = assets.load_meta(pid)
                self.assertIn(slug, meta["idea_bubbles"])
                self.assertEqual(assets.bubble_scores(meta)[slug], 5)
                bubbles.set_pdf_bubble_score(slug, pid, 3)
                self.assertEqual(assets.load_meta(pid)["bubble_scores"][slug], 3)
                bubbles.remove_pdf_from_bubble(slug, pid)
                meta = assets.load_meta(pid)
        self.assertNotIn(slug, meta.get("idea_bubbles", []))
        self.assertNotIn(slug, meta.get("bubble_scores", {}))

    def test_invalid_score_is_rejected(self):
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                pid = assets.save_asset(b"%PDF-1", "a.pdf", title="A", tags=["Diffusion Models"])
                with self.assertRaises(ValueError):
                    bubbles.set_pdf_bubble_score(slug, pid, 6)

    def test_chat_context_uses_relevance_labels(self):
        captured = {}

        def fake(home, messages, system=None, temperature=0.3, *, claude_token=""):
            captured["system"] = system or ""
            yield "ok"

        orig = models.stream_chat
        models.stream_chat = fake
        try:
            with temp_home() as home:
                slug = make_bubble(home)
                pid = service.save_asset(home, b"%PDF-1", "a.pdf", title="Attached",
                                         tags=["Diffusion Models"])
                with paths.use_root(home):
                    assets.save_summary(pid, "attached summary")
                run_chat(home, slug, "summarize", PAGE)
        finally:
            models.stream_chat = orig
        self.assertIn("## Relevance 5", captured["system"])
        self.assertIn("### [Relevance 5] Attached", captured["system"])


class PublisherPdfFallback(unittest.TestCase):
    def test_hal_inria_repository_record_becomes_direct_pdf_url(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "best_oa_location": {
                        "landing_page_url": "https://inria.hal.science/inria-00274768",
                        "pdf_url": None,
                    },
                    "locations": [],
                }

        with patch("lockedin.assets.httpx.get", return_value=Response()):
            fallback = assets._open_access_pdf_fallback(
                "https://dl.acm.org/doi/pdf/10.1145/1399504.1360691?download=true")
        self.assertEqual(fallback, "https://inria.hal.science/inria-00274768/document")


class AssetRequiresAttentionDefaults(unittest.TestCase):
    def test_new_assets_require_attention_until_explicitly_cleared(self):
        with temp_home() as home:
            pid = service.save_asset(home, b"%PDF-1", "a.pdf", title="A",
                                     tags=["Diffusion Models"])
            meta = service.get_asset(home, pid)
            self.assertTrue(meta["attention_flag"])
            with paths.use_root(home):
                self.assertEqual([a["pdf_id"] for a in assets.requires_attention()], [pid])
            service.update_asset(home, pid, attention_flag=False)
            with paths.use_root(home):
                self.assertEqual(assets.requires_attention(), [])


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


class DisplayMathOpenerNormalization(unittest.TestCase):
    """Guard: Toast UI treats ``$$`` + a letter as its own custom-block widget opener.

    Its parser opens a widget on a line matching ``/^(\\$\\$)(\\s*[a-zA-Z])+/`` that has no second
    ``$$`` on the same line, and closes it *only* on a line equal to ``$$``. Multi-line display math
    like ``$$K_\\theta=...,`` therefore opened a block that never closed, and the editor painted the
    rest of the page with the widget background. Saving moves such an opener onto its own line.
    """

    # The real rules, lifted verbatim from toastui-editor-all.min.js.
    OPENS = re.compile(r"^(\$\$)(\s*[a-zA-Z])+")
    SAME_LINE = re.compile(r"^(\$\$)(\s*[a-zA-Z])+.*(\$\$)")
    CLOSES = re.compile(r"^\$\$$")

    def unclosed_widget_line(self, text: str):
        """Return the line number that opens a never-closed widget block, else None."""
        opened = None
        for number, line in enumerate(text.split("\n"), start=1):
            if opened is None:
                if self.OPENS.match(line) and not self.SAME_LINE.match(line):
                    opened = number
            elif self.CLOSES.match(line):
                opened = None
        return opened

    def test_multiline_display_math_no_longer_runs_away(self):
        src = ("Fix one generator\n\n"
               "$$K_\\theta=U_\\theta^\\ast S_\\theta U_\\theta,\n"
               "\\qquad\n"
               "S_\\theta=M_\\theta-M_\\theta^\\top,$$\n\n"
               "## A heading that used to be swallowed\n")
        self.assertEqual(self.unclosed_widget_line(src), 3)      # the bug, before normalization
        fixed = bubbles.normalize_display_math(src)
        self.assertIsNone(self.unclosed_widget_line(fixed))
        self.assertEqual(fixed.count("$$"), src.count("$$"))     # no delimiter invented or lost
        self.assertIn("$$\nK_\\theta=", fixed)

    def test_safe_math_is_left_exactly_as_written(self):
        # Single-line display math closes on its own line; a non-letter opener never starts a
        # widget. Neither should be reformatted, so ordinary pages keep their diffs clean.
        for src in ("text $$E = mc^2$$ tail\n",
                    "$$\\operatorname{Cay}(K_\\theta)\n=(I-K)^{-1}(I+K).$$\n",
                    "$$\n\\alpha\n+\\beta\n$$\n",
                    "inline $a+b$ only\n"):
            with self.subTest(src=src):
                self.assertEqual(bubbles.normalize_display_math(src), src)
                self.assertIsNone(self.unclosed_widget_line(src))

    def test_normalization_is_idempotent(self):
        src = "$$A_{ki}:=\\langle u_k,\\varphi_i\\rangle,\n\\qquad i>N.$$\n"
        once = bubbles.normalize_display_math(src)
        self.assertEqual(bubbles.normalize_display_math(once), once)

    def test_math_inside_fenced_code_is_never_rewritten(self):
        src = ("```\n$$K_\\theta=U,\n\\qquad x\n$$\n```\n")
        self.assertEqual(bubbles.normalize_display_math(src), src)

    def test_saving_a_page_normalizes_the_opener_on_disk(self):
        with temp_home() as home:
            slug = make_bubble(home)
            body = "$$K_\\theta=U_\\theta,\n\\qquad R>0,$$\n\n## Later section\n"
            with paths.use_root(home):
                bubbles.save_page(slug, "overview", body)
                stored = bubbles.get_page(slug, "overview")
        self.assertIn("$$\nK_\\theta=U_\\theta,", stored)
        self.assertIsNone(self.unclosed_widget_line(stored))
        self.assertIn("## Later section", stored)


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


# --------------------------------------------------------------------------- #
# TODOs — CRUD, reference counting, the delete-when-unreferenced guard, and the
# @<id> resolution baked into the shared preview/share renderer.
# --------------------------------------------------------------------------- #
class TodosCrud(unittest.TestCase):
    def test_todo_autocomplete_only_suggests_open_items(self):
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn(".filter(t=>!t.done).sort((a,b)=>Number(a.id)-Number(b.id))", source)
        self.assertNotIn('detail:(t.done?"Done · ":"")+t.title', source)

    def test_ids_autoincrement_and_crud_round_trips(self):
        with temp_home() as home:
            t1 = service.add_todo(home, "First", "n1")
            t2 = service.add_todo(home, "Second")
            self.assertEqual((t1["id"], t2["id"]), (1, 2))
            self.assertEqual([t["id"] for t in service.list_todos(home)], [1, 2])
            self.assertEqual(service.list_todos(home)[0]["ref_count"], 0)
            service.update_todo(home, 1, title="First!", done=True)
            got = service.get_todo(home, 1)
            self.assertEqual(got["title"], "First!")
            self.assertTrue(got["done"])
            self.assertTrue(service.delete_todo(home, 2))
            self.assertIsNone(service.get_todo(home, 2))
            t2b = service.add_todo(home, "Second again")
            self.assertEqual(t2b["id"], 2)
            self.assertFalse(service.delete_todo(home, 999))  # missing id → no-op


class TodoReferenceGuard(unittest.TestCase):
    def test_ref_count_and_delete_guard(self):
        with temp_home() as home:
            slug = make_bubble(home)
            for i in range(5):  # ids 1..5
                service.add_todo(home, f"todo {i+1}")
            with paths.use_root(home):
                # @1 referenced once; @50 must NOT count toward @5.
                bubbles.save_page(slug, "overview", "# T\n\nTrack @1 here. Unrelated @50.\n")
            by_id = {t["id"]: t for t in service.list_todos(home)}
            self.assertEqual(by_id[1]["ref_count"], 1)
            self.assertEqual(by_id[5]["ref_count"], 0)   # @50 didn't bleed into @5
            # Referenced TODO can't be deleted; unreferenced one can.
            with self.assertRaises(ValueError):
                service.delete_todo(home, 1)
            self.assertTrue(service.delete_todo(home, 2))

    def test_delete_compacts_ids_and_rewrites_shifted_references(self):
        with temp_home() as home:
            slug = make_bubble(home)
            service.add_todo(home, "one")
            service.add_todo(home, "two")
            service.add_todo(home, "three")
            with paths.use_root(home):
                bubbles.save_page(slug, "overview", "# T\n\nKeep tracking @3 here.\n")

            self.assertTrue(service.delete_todo(home, 2))
            remaining = service.list_todos(home)
            self.assertEqual([(t["id"], t["title"]) for t in remaining], [(1, "one"), (2, "three")])
            with paths.use_root(home):
                self.assertIn("@2 here", bubbles.get_page(slug, "overview"))
                self.assertNotIn("@3 here", bubbles.get_page(slug, "overview"))

    def test_get_todo_lists_reference_locations(self):
        with temp_home() as home:
            slug = make_bubble(home)
            service.add_todo(home, "Referenced")
            with paths.use_root(home):
                bubbles.save_page(slug, "overview", "# T\n\nSee @1.\n")
            got = service.get_todo(home, 1)
            self.assertEqual(got["ref_count"], 1)
            self.assertEqual(got["refs"][0]["page_slug"], "overview")


class TodoRefRendering(unittest.TestCase):
    TODOS = {1: {"id": 1, "title": "Fix bug", "done": False},
             2: {"id": 2, "title": "Done one", "done": True}}

    def _render(self, **kw):
        return server._render_preview_html(
            name="B", page="P", all_pages=[{"page_slug": "p", "title": "P"}],
            content="See @1 and @2 and @99.", slug="s",
            link_base="/x", asset_base="/x/assets", show_back=False, todos=self.TODOS, **kw)

    def test_linked_in_owner_preview(self):
        html = self._render(todo_link_base="/#todos")
        self.assertIn("/#todos/1", html)       # known id links to the SPA detail view
        self.assertIn("Fix bug", html)
        self.assertIn("~~", html)              # done todo rendered struck-through
        self.assertIn("@99", html)             # unknown id left literal

    def test_styled_text_in_share_mode(self):
        html = self._render(todo_link_base=None)
        self.assertIn("todoref", html)         # styled non-link span
        self.assertNotIn("/#todos/1", html)    # no login-gated link on the public page


# --------------------------------------------------------------------------- #
# Asset BibTeX — optional metadata, user-global duplicate-key guard, previews,
# and rendered \cite{} references.
# --------------------------------------------------------------------------- #
class AssetModelMetadata(unittest.TestCase):
    def test_model_metadata_is_saved_as_title_and_author_search_fields(self):
        with temp_home() as home:
            with paths.use_root(home):
                pid = assets.save_asset(b"%PDF-1", "paper.pdf", title="Uploaded name")
                assets.save_text(pid, "Canonical Paper Title\nAda Lovelace, Grace Hopper\nAbstract")
            original = models.complete
            models.complete = lambda *args, **kwargs: (
                '{"title":"Canonical Paper Title","authors":["Ada Lovelace","Grace Hopper"]}')
            try:
                meta = tagger.extract_paper_metadata(home, pid)
            finally:
                models.complete = original
        self.assertEqual(meta["title"], "Uploaded name")  # never overwrite a user-visible title
        self.assertEqual(meta["extracted_title"], "Canonical Paper Title")
        self.assertEqual(meta["authors"], ["Ada Lovelace", "Grace Hopper"])
        self.assertTrue(meta["metadata_extracted"])

    def test_background_metadata_update_preserves_bibtex_saved_during_ingest(self):
        bib = "@article{quickadd, title={Quick Add}, author={Ada}, year={2026}}"
        with temp_home() as home:
            with paths.use_root(home):
                pid = assets.save_asset(b"%PDF-1", "paper.pdf", title="Uploaded name")
                assets.save_text(pid, "Canonical Paper Title")
            original = models.complete

            def complete_during_bibtex_save(*args, **kwargs):
                service.update_asset_bibliography(home, pid, bib)
                return '{"title":"Canonical Paper Title","authors":["Ada"]}'

            models.complete = complete_during_bibtex_save
            try:
                meta = tagger.extract_paper_metadata(home, pid)
            finally:
                models.complete = original
        self.assertEqual(meta["bibliography"], bib)
        self.assertEqual(meta["extracted_title"], "Canonical Paper Title")

    def test_asset_can_be_created_with_bibtex(self):
        bib = "@article{libraryadd, title={Library Add}, author={Ada}, year={2026}}"
        with temp_home() as home:
            pid = service.save_asset(home, b"%PDF-1", "paper.pdf", bibliography=bib)
            self.assertEqual(service.get_asset(home, pid)["bibliography"], bib)

    def test_resummarize_requires_a_working_active_model(self):
        with temp_home() as home:
            pid = service.save_asset(home, b"%PDF-1", "paper.pdf")
            with self.assertRaises(service.ModelUnavailableError):
                service.resummarize_asset(home, pid)

    def test_resummarize_refreshes_the_cached_summary(self):
        with temp_home() as home:
            pid = service.save_asset(home, b"%PDF-1", "paper.pdf")
            original_health, original_summarize = models.health_check, tagger.summarize_pdf
            models.health_check = lambda *args, **kwargs: {"ok": True}
            tagger.summarize_pdf = lambda *args, **kwargs: "Fresh summary"
            try:
                self.assertEqual(service.resummarize_asset(home, pid), "Fresh summary")
            finally:
                models.health_check, tagger.summarize_pdf = original_health, original_summarize
            self.assertTrue(service.get_asset(home, pid)["summarized"])


class AssetBibtex(unittest.TestCase):
    BIB1 = "@article{bases4spaces, title={Bases for Spaces}, author={Ada Lovelace}, year={1843}, journal={Notes}}"
    BIB2 = "@book{otherkey, title={Other Book}, author={Grace Hopper}, year={1952}, publisher={Press}}"

    def test_bibtex_round_trip_and_clear(self):
        with temp_home() as home:
            with paths.use_root(home):
                pid = assets.save_asset(b"%PDF-1", "a.pdf", title="A")
            meta = service.update_asset_bibliography(home, pid, self.BIB1)
            self.assertIn("bases4spaces", meta["bibliography"])
            preview = service.preview_bibtex(home, self.BIB1)
            self.assertEqual(preview["entries"][0]["key"], "bases4spaces")
            self.assertIn("Bases for Spaces", preview["entries"][0]["text"])
            meta = service.update_asset_bibliography(home, pid, "")
            self.assertEqual(meta["bibliography"], "")

    def test_generated_citation_file_tracks_bibtex_edits(self):
        with temp_home() as home:
            slug = make_bubble(home)
            pid = service.save_asset(home, b"%PDF-1", "a.pdf", title="A",
                                     tags=["Diffusion Models"])
            service.update_asset_bibliography(home, pid, self.BIB1)
            cite_path = home / "REPORTS" / slug / "_lockedin_papers.md"
            self.assertIn("bases4spaces", cite_path.read_text())
            service.update_asset_bibliography(home, pid, self.BIB2)
            text = cite_path.read_text()
            self.assertIn("otherkey", text)
            self.assertNotIn("bases4spaces", text)
            service.update_asset_bibliography(home, pid, "")
            text = cite_path.read_text()
            self.assertIn("No BibTeX is saved", text)
            self.assertIn("## [Relevance 5] A", text)
            self.assertIn(pid, text)
            self.assertNotIn("otherkey", text)

    def test_generated_citation_file_includes_assets_without_bibtex(self):
        with temp_home() as home:
            slug = make_bubble(home)
            pid = service.save_asset(home, b"%PDF-1", "unindexed.pdf", title="Unindexed Paper",
                                     tags=["Diffusion Models"], url_source="https://example.test/paper.pdf")
            cite_path = home / "REPORTS" / slug / "_lockedin_papers.md"
            text = cite_path.read_text()
        self.assertIn("## [Relevance 5] Unindexed Paper", text)
        self.assertIn(pid, text)
        self.assertIn("https://example.test/paper.pdf", text)
        self.assertIn("No BibTeX is saved", text)

    def test_duplicate_key_on_another_asset_is_rejected(self):
        with temp_home() as home:
            with paths.use_root(home):
                p1 = assets.save_asset(b"%PDF-1", "a.pdf", title="A")
                p2 = assets.save_asset(b"%PDF-1", "b.pdf", title="B")
            service.update_asset_bibliography(home, p1, self.BIB1)
            with self.assertRaises(assets.DuplicateBibKeyError) as cm:
                service.update_asset_bibliography(home, p2, self.BIB1)
            self.assertEqual(cm.exception.key, "bases4spaces")

    def test_duplicate_key_inside_same_field_is_rejected(self):
        with temp_home() as home:
            with paths.use_root(home):
                pid = assets.save_asset(b"%PDF-1", "a.pdf", title="A")
            dup = self.BIB1 + "\n\n@book{bases4spaces, title={Duplicate}}"
            with self.assertRaises(assets.DuplicateBibKeyError):
                service.update_asset_bibliography(home, pid, dup)

    def test_page_can_cite_key_from_asset_attached_to_bubble(self):
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                pid = assets.save_asset(b"%PDF-1", "a.pdf", title="A", tags=["Diffusion Models"])
            service.update_asset_bibliography(home, pid, self.BIB1)
            service.save_page(home, slug, "overview", "# T\n\nUse \\cite{bases4spaces}.\n")
            with paths.use_root(home):
                self.assertIn("\\cite{bases4spaces}", bubbles.get_page(slug, "overview"))

    def test_bubble_refs_include_pdf_id_for_citation_links(self):
        with temp_home() as home:
            slug = make_bubble(home)
            pid = service.save_asset(home, b"%PDF-1", "a.pdf", title="A",
                                     tags=["Diffusion Models"])
            service.update_asset_bibliography(home, pid, self.BIB1)
            service.save_page(home, slug, "overview", "# T\n\nUse \\cite{bases4spaces}.\n")
            refs = server._bubble_refs(home, slug, service.list_pages(home, slug))
            self.assertEqual(refs["bibliography"]["bases4spaces"]["pdf_id"], pid)

    def test_page_can_save_unattached_citation_as_unresolved(self):
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                pid = assets.save_asset(b"%PDF-1", "a.pdf", title="A")
            service.update_asset_bibliography(home, pid, self.BIB1)
            service.save_page(home, slug, "overview", "# T\n\nUse \\cite{bases4spaces}.\n")
            with paths.use_root(home):
                self.assertIn("\\cite{bases4spaces}", bubbles.get_page(slug, "overview"))

    def test_page_can_save_unknown_citation_as_unresolved(self):
        with temp_home() as home:
            slug = make_bubble(home)
            service.save_page(home, slug, "overview", "# T\n\nUse \\cite{missing}.\n")
            with paths.use_root(home):
                self.assertIn("\\cite{missing}", bubbles.get_page(slug, "overview"))


class BubbleAssetExplorer(unittest.TestCase):
    def test_lists_reads_and_deletes_bubble_assets(self):
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                bubbles.save_bubble_image(slug, "notes.py", b"print('hello')\n")
            entries = service.list_bubble_assets(home, slug)
            self.assertEqual(entries[0]["name"], "notes.py")
            self.assertEqual(service.bubble_text_asset(home, slug, "notes.py"), "print('hello')\n")
            self.assertTrue(service.delete_bubble_asset(home, slug, "notes.py"))
            self.assertEqual(service.list_bubble_assets(home, slug), [])

    def test_rejects_binary_text_view_and_path_traversal(self):
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                bubbles.save_bubble_image(slug, "data.bin", b"\xff\x00")
            with self.assertRaises(ValueError):
                service.bubble_text_asset(home, slug, "data.bin")
            self.assertFalse(service.delete_bubble_asset(home, slug, "../data.bin"))


class CitationRendering(unittest.TestCase):
    BIB = {
        "bases4spaces": {"key": "bases4spaces", "text": "Ada Lovelace. \"Bases for Spaces\". Notes. 1843."},
        "otherkey": {"key": "otherkey", "text": "Grace Hopper. \"Other Book\". Press. 1952."},
    }
    P1 = "Uses \\cite{bases4spaces}.\n"
    P2 = "Also \\cite{otherkey,bases4spaces} and unknown \\cite{missing}.\n"
    PAGES = [{"page_slug": "p1", "title": "One"}, {"page_slug": "p2", "title": "Two"}]

    def _refs(self):
        return server._build_refs([{"page_slug": "p1", "content": self.P1},
                                   {"page_slug": "p2", "content": self.P2}],
                                  bibliography=self.BIB)

    def _render(self, page, content):
        return server._render_preview_html(
            name="B", page=page, all_pages=self.PAGES, content=content, slug="s",
            link_base="/x", asset_base="/x/assets", show_back=False, refs=self._refs())

    def test_registry_numbers_citations_by_first_appearance(self):
        refs = self._refs()
        self.assertEqual(refs["citeOrder"], ["bases4spaces", "otherkey"])
        self.assertEqual(refs["citeMap"], {"bases4spaces": 1, "otherkey": 2})

    def test_citations_and_shared_references_render(self):
        html = self._render("p2", self.P2)
        self.assertIn('<span class="cite-ref">[2, 1]</span>', html)
        self.assertIn('<span class="cite-ref">[?missing]</span>', html)
        self.assertIn('class="references-box"', html)
        self.assertIn("<h1>References</h1>", html)
        self.assertIn('<span class="bibnum">[1]</span>', html)
        self.assertIn("Bases for Spaces", html)
        self.assertIn('<span class="bibnum">[2]</span>', html)
        self.assertIn("Other Book", html)

    def test_references_section_appears_on_page_without_local_cite(self):
        html = self._render("p1", "No citation on this page.\n")
        self.assertIn('class="references-box"', html)
        self.assertIn("Bases for Spaces", html)
        self.assertIn("Other Book", html)

    def test_no_references_section_without_known_cites(self):
        refs = server._build_refs([{"page_slug": "p1", "content": "Only \\cite{missing}."}],
                                  bibliography=self.BIB)
        html = server._render_preview_html(
            name="B", page="p1", all_pages=[{"page_slug": "p1", "title": "One"}],
            content="Only \\cite{missing}.", slug="s", link_base="/x",
            asset_base="/x/assets", show_back=False, refs=refs)
        self.assertNotIn('class="references-box"', html)
        self.assertIn("[?missing]", html)


class HiddenPreviewPages(unittest.TestCase):
    def test_page_hidden_flag_round_trips_in_manifest(self):
        with temp_home() as home:
            slug = make_bubble(home)
            ps = service.create_page(home, slug, "Draft")
            service.set_page_hidden(home, slug, ps, True)
            pages = service.list_pages(home, slug)
            self.assertTrue(next(p for p in pages if p["page_slug"] == ps)["hidden"])

    def test_hidden_pages_are_left_out_of_preview_nav_and_refs(self):
        pages = [{"page_slug": "visible", "title": "Visible"},
                 {"page_slug": "draft", "title": "Draft", "hidden": True}]
        visible = server._visible_pages(pages)
        refs = server._build_refs([{"page_slug": "visible", "content": "No cite here."}],
                                  bibliography=CitationRendering.BIB)
        html = server._render_preview_html(
            name="B", page="visible", all_pages=visible,
            content="See [[draft|draft page]].", slug="s", link_base="/x",
            asset_base="/x/assets", show_back=False, refs=refs)
        self.assertIn("Visible", html)
        self.assertNotIn("/x/draft", html)
        self.assertNotIn('class="references-box"', html)


# --------------------------------------------------------------------------- #
# Cross-page (bubble-wide) equation & theorem references + in-math \eqref/\thmref.
# Numbering is pure Python (server._build_refs), so it's a deterministic guard.
# --------------------------------------------------------------------------- #
class CrossPageReferences(unittest.TestCase):
    P1 = ("$$ a = b \\label{eq:one} $$\n\n"
          "\\begin{theorem}\\label{thm:a}\nFirst.\n\\end{theorem}\n")
    P2 = ("See \\eqref{eq:one} and \\thmref{thm:a}.\n\n"
          "$$ c = d \\label{eq:two} $$\n\n"
          "\\begin{theorem}\\label{thm:b}\nSecond.\n\\end{theorem}\n\n"
          "Inline use: $$ x = y \\cdot \\eqref{eq:one} \\label{eq:three} $$\n")
    PAGES = [{"page_slug": "p1", "title": "One"}, {"page_slug": "p2", "title": "Two"}]

    def _refs(self):
        return server._build_refs([{"page_slug": "p1", "content": self.P1},
                                   {"page_slug": "p2", "content": self.P2}])

    def test_registry_numbers_globally(self):
        refs = self._refs()
        # Equations numbered across both pages in document order.
        self.assertEqual(refs["eq"], {"eq:one": 1, "eq:two": 2, "eq:three": 3})
        # Theorem counter continues onto page 2.
        self.assertEqual(refs["thm"]["thm:a"], {"env": "theorem", "number": 1})
        self.assertEqual(refs["thm"]["thm:b"], {"env": "theorem", "number": 2})
        # Page 2 starts with one theorem already counted on page 1.
        self.assertEqual(refs["thmStart"]["p1"], {})
        self.assertEqual(refs["thmStart"]["p2"], {"theorem": 1})

    def _render(self, page, content):
        return server._render_preview_html(
            name="B", page=page, all_pages=self.PAGES, content=content, slug="s",
            link_base="/x", asset_base="/x/assets", show_back=False, refs=self._refs())

    def test_eqref_and_thmref_resolve_across_pages(self):
        html = self._render("p2", self.P2)
        self.assertIn('<span class="eq-ref">(1)</span>', html)   # \eqref{eq:one} from page 1
        self.assertIn('<span class="thm-ref">Theorem 1</span>', html)  # \thmref{thm:a} from page 1
        self.assertIn("Theorem 2", html)                          # this page's theorem box continues

    def test_tag_number_matches_eqref_number(self):
        # eq:one renders as \tag{1} on page 1 and as (1) when \eqref'd on page 2 — they agree.
        self.assertIn(r"\tag{1}", self._render("p1", self.P1))
        self.assertIn('<span class="eq-ref">(1)</span>', self._render("p2", self.P2))

    def test_in_math_eqref_is_resolved_not_raw(self):
        html = self._render("p2", self.P2)
        # The in-math \eqref{eq:one} is baked to (1); no raw \eqref command survives anywhere.
        self.assertNotIn(r"\eqref{", html)
        self.assertIn(r"\tag{3}", html)   # the same inline block's own label became \tag{3}

    def test_in_math_thmref_is_resolved_not_raw(self):
        # \thmref inside a $$ block resolves to \text{Theorem n} (KaTeX-renderable), never raw.
        content = "$$ y = \\thmref{thm:a} $$\n"
        html = self._render("p2", content)
        self.assertNotIn(r"\thmref{", html)
        self.assertIn(r"\text{Theorem 1}", html)

    def test_assumption_environment_is_numbered_and_referenceable(self):
        content = ("\\begin{assumption}[Regularity]\\label{asm:regular}\n"
                   "The solution is smooth.\n\\end{assumption}\n\n"
                   "See \\thmref{asm:regular}.")
        refs = server._build_refs([{"page_slug": "p1", "content": content}])
        self.assertEqual(refs["thm"]["asm:regular"], {"env": "assumption", "number": 1})
        html = server._render_preview_html(
            name="B", page="p1", all_pages=[{"page_slug": "p1", "title": "One"}],
            content=content, slug="s", link_base="/x", asset_base="/x/assets",
            show_back=False, refs=refs)
        self.assertIn("Assumption 1 (Regularity)", html)
        self.assertIn('<span class="thm-ref">Assumption 1</span>', html)


class FigureReferences(unittest.TestCase):
    P1 = r"![First figure \label{fig:first}](/first.png)"
    P2 = r"See \figref{fig:first}. ![Second figure \label{fig:second}](/second.png)"

    def _refs(self):
        return server._build_refs([{"page_slug": "p1", "content": self.P1},
                                   {"page_slug": "p2", "content": self.P2}])

    def test_figures_are_numbered_and_labeled_across_pages(self):
        refs = self._refs()
        self.assertEqual(refs["figStart"], {"p1": 0, "p2": 1})
        self.assertEqual(refs["fig"]["fig:first"], {"number": 1, "page_slug": "p1"})
        self.assertEqual(refs["fig"]["fig:second"], {"number": 2, "page_slug": "p2"})
        self.assertEqual(len({entry["number"] for entry in refs["fig"].values()}), len(refs["fig"]))

    def test_figure_reference_and_caption_are_rendered(self):
        html = server._render_preview_html(
            name="B", page="p2", all_pages=[{"page_slug": "p1", "title": "One"},
                                             {"page_slug": "p2", "title": "Two"}],
            content=self.P2, slug="s", link_base="/x", asset_base="/x/assets",
            show_back=False, refs=self._refs())
        self.assertIn('class="fig-ref">Figure 1</span>', html)
        self.assertIn('marker.textContent="Figure "+number+": "', html)
        self.assertIn('figure.id="fig-"+encodeURIComponent(label)', html)


class PrivateReviewComments(unittest.TestCase):
    def test_legacy_review_is_migrated_to_inline_markers(self):
        with temp_home() as home:
            slug = make_bubble(home)
            service.save_page(home, slug, "overview", "Before selected material after.")
            start = len("Before ")
            thread = service.create_comment(home, slug, "overview", "alice", "Review", {
                "start": start, "quote": "selected material", "prefix": "Before ", "suffix": " after"})
            comments = service.list_comments(home, slug, "overview")
            marker = bubbles.comment_marker(thread["id"])
            self.assertIn(marker + "selected material" + marker, service.get_page(home, slug, "overview"))
            self.assertEqual(comments["threads"][0]["id"], thread["id"])

    def test_comment_markers_strip_without_changing_math_source(self):
        marker = bubbles.comment_marker("abc")
        source = marker + "$x^2$" + marker
        self.assertEqual(bubbles.strip_comment_markers(source), "$x^2$")

    def test_thread_lifecycle_and_author_only_message_editing(self):
        with temp_home() as home:
            slug = make_bubble(home)
            anchor = {"start": 3, "quote": "important text", "prefix": "An ", "suffix": " follows"}
            thread = service.create_comment(home, slug, "overview", "alice", "Needs a citation", anchor)
            self.assertEqual(thread["status"], "open")
            reply = service.reply_comment(home, slug, "overview", thread["id"], "bob", "I will add one")
            with self.assertRaises(PermissionError):
                service.edit_comment_message(home, slug, "overview", thread["id"], reply["id"], "alice", "Changed")
            edited = service.edit_comment_message(home, slug, "overview", thread["id"], reply["id"], "bob", "Citation added")
            self.assertEqual(edited["body"], "Citation added")
            service.set_comment_status(home, slug, "overview", thread["id"], "resolved", "alice")
            data = service.list_comments(home, slug, "overview")
            self.assertEqual(data["threads"][0]["status"], "resolved")
            self.assertTrue(service.delete_comment(home, slug, "overview", thread["id"]))
            self.assertEqual(service.list_comments(home, slug, "overview")["threads"], [])

    def test_deleting_a_page_removes_its_review_sidecar(self):
        with temp_home() as home:
            slug = make_bubble(home)
            page = service.create_page(home, slug, "Draft")
            service.create_comment(home, slug, page, "alice", "Review this", {"start": 0, "quote": "# Draft"})
            with paths.use_root(home):
                comment_path = paths.bubble_page_comments_path(slug, page)
                self.assertTrue(comment_path.exists())
            self.assertTrue(service.delete_page(home, slug, page))
            with paths.use_root(home):
                self.assertFalse(comment_path.exists())


if __name__ == "__main__":
    unittest.main()
