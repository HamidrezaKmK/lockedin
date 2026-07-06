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

from lockedin import assets, bubbles, models, paths, reports, server, service

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


# --------------------------------------------------------------------------- #
# TODOs — CRUD, reference counting, the delete-when-unreferenced guard, and the
# @<id> resolution baked into the shared preview/share renderer.
# --------------------------------------------------------------------------- #
class TodosCrud(unittest.TestCase):
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
            cite_path = home / "REPORTS" / slug / "_lockedin_citations.md"
            self.assertIn("bases4spaces", cite_path.read_text())
            service.update_asset_bibliography(home, pid, self.BIB2)
            text = cite_path.read_text()
            self.assertIn("otherkey", text)
            self.assertNotIn("bases4spaces", text)
            service.update_asset_bibliography(home, pid, "")
            text = cite_path.read_text()
            self.assertIn("No BibTeX entries", text)
            self.assertNotIn("otherkey", text)

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

    def test_page_cannot_cite_key_from_unattached_asset(self):
        with temp_home() as home:
            slug = make_bubble(home)
            with paths.use_root(home):
                pid = assets.save_asset(b"%PDF-1", "a.pdf", title="A")
            service.update_asset_bibliography(home, pid, self.BIB1)
            with self.assertRaises(service.CitationValidationError):
                service.save_page(home, slug, "overview", "# T\n\nUse \\cite{bases4spaces}.\n")

    def test_page_cannot_cite_unknown_key(self):
        with temp_home() as home:
            slug = make_bubble(home)
            with self.assertRaises(service.CitationValidationError):
                service.save_page(home, slug, "overview", "# T\n\nUse \\cite{missing}.\n")


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


if __name__ == "__main__":
    unittest.main()
