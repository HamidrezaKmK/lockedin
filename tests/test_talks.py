"""Chalk talks: decks, the marks left on them, and what a syncing agent is handed.

Deterministic — no network, no LLM. Every case here is a bug this feature actually had.
"""
from __future__ import annotations

import base64
import unittest
from unittest.mock import patch
from pathlib import Path

from lockedin import bubbles, feedback, paths, scientist_sync, talks

from tests.test_editing_logic import temp_home


DECK = """\
<!-- slide: kind=setup, date=2026-08-27, v=1 -->
# What we're changing

*So the rest has somewhere to stand.*

Sample the noise level uniformly in log-SNR space.

---

<!-- slide: kind=derivation, date=2026-08-27, v=1 -->
# The residual term survives

*Every step is exact except one.*

1. $\\lambda(t) = \\log(\\bar\\alpha_t / (1-\\bar\\alpha_t))$
2. Here I assume $w(\\lambda) \\to \\text{const}$, which kills the variance term.
"""


class TalkTests(unittest.TestCase):
    def setUp(self):
        self.ctx = temp_home()
        self.home = self.ctx.__enter__()
        with paths.use_root(self.home):
            self.slug = bubbles.create_bubble("Diffusion noise schedules")
            bubbles.approve_bubble(self.slug)
            bubbles.ensure_pages(self.slug)
            self.talk = talks.create_talk(self.slug, "Why the variance term doesn't vanish",
                                          date="2026-08-27", body=DECK)

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    # -- parsing ---------------------------------------------------------------
    def test_a_deck_round_trips_through_parse_and_render(self):
        with paths.use_root(self.home):
            slides = talks.parse_deck(talks.read_deck(self.slug, self.talk))
        self.assertEqual([s["title"] for s in slides],
                         ["What we're changing", "The residual term survives"])
        self.assertEqual(slides[1]["kind"], "derivation")
        self.assertEqual(slides[0]["sub"], "So the rest has somewhere to stand.")
        # Re-rendering and re-parsing must not lose or invent a slide.
        again = talks.parse_deck(talks.render_deck(slides))
        self.assertEqual([s["title"] for s in again], [s["title"] for s in slides])

    # -- anchoring -------------------------------------------------------------
    def test_a_mark_anchors_by_quote_and_orphans_loudly_when_its_text_goes(self):
        with paths.use_root(self.home):
            note = talks.add_note(self.slug, self.talk, slide=1, kind="bad", author="pi",
                                  quote="Here I assume $w(\\lambda) \\to \\text{const}$",
                                  text="Only true in the high-SNR tail.")
            detail = talks.talk_detail(self.slug, self.talk)
            self.assertFalse(detail["notes"][0]["orphan"])
            # The context is stored so a repeated sentence resolves to the right occurrence.
            self.assertTrue(note["prefix"])

            talks.apply_slide_source(self.slug, self.talk, 1,
                                     "# The residual term survives\n\n"
                                     "No constancy assumption is made.")
            detail = talks.talk_detail(self.slug, self.talk)
            # Losing a reviewer's objection silently is the one unacceptable failure.
            self.assertTrue(detail["notes"][0]["orphan"])
        self.assertEqual(detail["notes"][0]["quote"],
                             "Here I assume $w(\\lambda) \\to \\text{const}$")

    def test_a_slide_can_use_a_custom_section_label(self):
        with paths.use_root(self.home):
            talks.apply_slide_source(self.slug, self.talk, 1,
                                     "# The residual term survives\n\nA new framing.",
                                     kind="Literature review")
            detail = talks.talk_detail(self.slug, self.talk)
        self.assertEqual(detail["slides"][1]["kind"], "Literature review")

    def test_talk_card_title_and_explanation_can_change_without_rewriting_slides(self):
        with paths.use_root(self.home):
            before = talks.read_deck(self.slug, self.talk)
            updated = talks.update_talk_metadata(self.slug, self.talk,
                                                 title="Variance survives", intent="The short version.")
            after = talks.read_deck(self.slug, self.talk)
        self.assertEqual(updated["title"], "Variance survives")
        self.assertEqual(updated["intent"], "The short version.")
        self.assertEqual(before, after)

    def test_a_title_and_subtitle_are_markable_slide_source(self):
        with paths.use_root(self.home):
            title_note = talks.add_note(self.slug, self.talk, slide=0, kind="q", author="pi",
                                        quote="What we're changing", text="Sharper title?")
            sub_note = talks.add_note(self.slug, self.talk, slide=0, kind="more", author="pi",
                                      quote="So the rest has somewhere to stand.", text="Explain this.")
            detail = talks.talk_detail(self.slug, self.talk)
            editable = detail["slides"][0]["edit_source"]
        self.assertFalse(next(n for n in detail["notes"] if n["id"] == title_note["id"])["orphan"])
        self.assertFalse(next(n for n in detail["notes"] if n["id"] == sub_note["id"])["orphan"])
        self.assertIn(f"<comment-begin={title_note['id']}>What we're changing", editable)
        self.assertIn(f"<comment-begin={sub_note['id']}>So the rest has somewhere to stand.", editable)

    def test_talk_revision_changes_when_remote_deck_or_notes_change(self):
        """The browser can tell that another collaborator updated an open talk."""
        with paths.use_root(self.home):
            initial = talks.talk_detail(self.slug, self.talk)["revision"]
            talks.add_note(self.slug, self.talk, slide=0, kind="q", author="pi",
                           quote="Sample the noise level", text="Which range?")
            after_note = talks.talk_detail(self.slug, self.talk)["revision"]
            talks.apply_slide_source(self.slug, self.talk, 0,
                                     "# What we're changing\n\nA remote rewrite.")
            after_deck = talks.talk_detail(self.slug, self.talk)["revision"]
        self.assertNotEqual(initial, after_note)
        self.assertNotEqual(after_note, after_deck)

    # -- threads ---------------------------------------------------------------
    def test_only_the_last_turn_of_a_thread_can_be_edited(self):
        with paths.use_root(self.home):
            note = talks.add_note(self.slug, self.talk, slide=0, kind="q", author="pi",
                                  quote="Sample the noise level", text="Uniformly in what?")
            talks.reply_note(self.slug, self.talk, note["id"], "agent", "In log-SNR.")
            # The agent answered last, so the reviewer's earlier turn is frozen: editing it
            # would leave that answer replying to words that no longer exist.
            with self.assertRaises(ValueError):
                talks.edit_note(self.slug, self.talk, note["id"], "reworded", author="pi")
            # ...but the agent may still correct its own last turn.
            talks.edit_note(self.slug, self.talk, note["id"], "In log-SNR space.", author="agent")
            msgs = talks.load_notes(self.slug, self.talk)["notes"][note["id"]]["messages"]
            self.assertEqual([m["body"] for m in msgs], ["Uniformly in what?", "In log-SNR space."])

    # -- resolution ------------------------------------------------------------
    def test_resolving_deletes_the_mark_completely(self):
        with paths.use_root(self.home):
            note = talks.add_note(self.slug, self.talk, slide=1, kind="bad", author="pi",
                                  quote="which kills the variance term", text="Not in the tail.")
            talks.save_note_image(self.slug, self.talk, note["id"], b"\x89PNG\r\n\x1a\nfake")
            self.assertTrue(talks.note_image_path(self.slug, self.talk, note["id"]).exists())

            talks.resolve_marks(self.slug, self.talk, [note["id"]])

            # Gone: sidecar entry and snapshot both, and nothing archived anywhere. An
            # addressed mark that lingers — even as history — is context bloat for every
            # future agent.
            self.assertEqual(talks.load_notes(self.slug, self.talk)["notes"], {})
            self.assertFalse(talks.note_image_path(self.slug, self.talk, note["id"]).exists())


class ProjectHandoffTests(unittest.TestCase):
    """What a syncing Scientist worker may read, and what it may write."""

    def setUp(self):
        self.ctx = temp_home()
        self.home = self.ctx.__enter__()
        with paths.use_root(self.home):
            self.slug = bubbles.create_bubble("Diffusion noise schedules")
            bubbles.approve_bubble(self.slug)
            bubbles.ensure_pages(self.slug)
            self.talk = talks.create_talk(self.slug, "Variance", date="2026-08-27", body=DECK)

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def test_generated_sidecars_never_reach_the_project(self):
        with paths.use_root(self.home):
            talks.add_note(self.slug, self.talk, slide=0, kind="q", author="pi",
                           quote="Sample the noise level", text="Uniformly in what?")
        names = set(scientist_sync._files(self.home, self.slug))
        self.assertIn(f"reports/talks/{self.talk}.md", names)
        self.assertIn("feedback/OPEN.md", names)
        # Raw marks, history and snapshots would sit in every future agent's context forever.
        for leaked in (f"reports/talks/{self.talk}.notes.yaml",
                       f"reports/talks/{self.talk}.history.yaml",
                       "reports/talks/talks.yaml"):
            self.assertNotIn(leaked, names)

    def test_a_region_mark_promises_a_picture_only_when_one_exists(self):
        """A live agent went looking for a snapshot that was never captured, and stalled."""
        with paths.use_root(self.home):
            bare = talks.add_note(self.slug, self.talk, slide=0, kind="more", author="pi",
                                  rect={"x": 8, "y": 30, "w": 55, "h": 40}, text="say more here")
            body = feedback.open_markdown(self.slug).decode()
            self.assertIn("no picture was captured", body)
            self.assertNotIn("see the picture", body.split("## ", 1)[1])

            talks.save_note_image(self.slug, self.talk, bare["id"], b"\x89PNG\r\n\x1a\nfake")
            body = feedback.open_markdown(self.slug).decode()
            self.assertIn("see the picture", body)
            self.assertNotIn("no picture was captured", body)

    def test_feedback_disappears_entirely_once_nothing_is_open(self):
        with paths.use_root(self.home):
            note = talks.add_note(self.slug, self.talk, slide=0, kind="q", author="pi",
                                  quote="Sample the noise level", text="Uniformly in what?")
        self.assertIn("feedback/OPEN.md", scientist_sync._files(self.home, self.slug))
        with paths.use_root(self.home):
            talks.resolve_marks(self.slug, self.talk, [note["id"]])
            self.assertIsNone(feedback.open_markdown(self.slug))
        # An agent opening a clean bubble should see no feedback machinery at all.
        self.assertNotIn("feedback/OPEN.md", scientist_sync._files(self.home, self.slug))

    def test_an_agent_creates_a_talk_by_writing_one_file(self):
        rel = "reports/talks/2026-08-28-does-cosine-help.md"
        deck = b"<!-- slide: kind=setup, date=2026-08-28, v=1 -->\n# Does cosine help?\n\n*A test.*\n"
        self.assertTrue(scientist_sync.writable_path(self.slug, rel))
        result = scientist_sync.apply_writes(self.home, self.slug, [{
            "path": rel, "content_b64": base64.b64encode(deck).decode(),
            "base_revision": scientist_sync.revision(b"")}])
        self.assertEqual(result["conflicts"], [])
        with paths.use_root(self.home):
            listed = {t["id"]: t for t in talks.list_talks(self.slug)}
        # The registry entry is derived from the file, so there is no second step to forget.
        self.assertIn("2026-08-28-does-cosine-help", listed)
        self.assertEqual(listed["2026-08-28-does-cosine-help"]["title"], "Does cosine help?")
        self.assertEqual(listed["2026-08-28-does-cosine-help"]["date"], "2026-08-28")

    def test_a_pushed_slide_cannot_resolve_marks(self):
        """Resolution is the user's act alone; a pushed header carrying `resolves=` is inert.

        Agents may edit the text a mark points at and reply in its thread — stranding a mark
        as an orphan is allowed, deleting it is not. The attribute is still consumed from the
        stored deck so it cannot linger and confuse.
        """
        with paths.use_root(self.home):
            n1 = talks.add_note(self.slug, self.talk, slide=1, kind="bad", author="pi",
                                quote="which kills the variance term", text="Not in the tail.")
            n2 = talks.add_note(self.slug, self.talk, slide=1, kind="q", author="pi",
                                quote="Here I assume", text="Where did that come from?")
        revised = DECK.replace(
            "<!-- slide: kind=derivation, date=2026-08-27, v=1 -->",
            f"<!-- slide: kind=derivation, date=2026-08-27, "
            f"resolves={n1['id']},{n2['id']} -->"
        ).replace("2. Here I assume $w(\\lambda) \\to \\text{const}$, which kills the variance term.",
                  "2. No constancy assumption is needed; the covariance term is exact.")
        rel = f"reports/talks/{self.talk}.md"
        result = scientist_sync.apply_writes(self.home, self.slug, [{
            "path": rel, "content_b64": base64.b64encode(revised.encode()).decode(),
            "base_revision": scientist_sync.revision(
                (paths.bubble_talk_path(self.slug, self.talk)
                 if False else Path("/dev/null")).read_bytes()
                if False else self._deck_bytes())}])
        self.assertEqual(result["conflicts"], [])
        with paths.use_root(self.home):
            # Both marks survive the push untouched — the edit landed, the deletion did not.
            kept = talks.load_notes(self.slug, self.talk)["notes"]
            self.assertEqual(set(kept), {n1["id"], n2["id"]})
            slides = talks.parse_deck(talks.read_deck(self.slug, self.talk))
            self.assertIn("No constancy assumption", slides[1]["body"])
            # The dead directive is consumed, not left lying in the stored deck — and neither
            # is the legacy v= attribute a stale deck may still carry.
            self.assertNotIn("resolves=", talks.read_deck(self.slug, self.talk))
            self.assertNotIn("v=", talks.read_deck(self.slug, self.talk))
            # The edit stranded n1's text: an orphan, loudly — never a deletion.
            detail = talks.talk_detail(self.slug, self.talk)
            flags = {n["id"]: n["orphan"] for n in detail["notes"]}
            self.assertTrue(flags[n1["id"]])

    def test_a_no_op_push_with_a_resolves_header_changes_nothing(self):
        """The old clear-without-editing path is gone: the attribute is dropped, the mark stays."""
        with paths.use_root(self.home):
            note = talks.add_note(self.slug, self.talk, slide=0, kind="q", author="pi",
                                  quote="Sample the noise level", text="Answered ages ago.")
        revised = DECK.replace("<!-- slide: kind=setup, date=2026-08-27, v=1 -->",
                               f"<!-- slide: kind=setup, date=2026-08-27, resolves={note['id']} -->")
        result = scientist_sync.apply_writes(self.home, self.slug, [{
            "path": f"reports/talks/{self.talk}.md",
            "content_b64": base64.b64encode(revised.encode()).decode(),
            "base_revision": scientist_sync.revision(self._deck_bytes())}])
        self.assertEqual(result["conflicts"], [])
        with paths.use_root(self.home):
            self.assertIn(note["id"], talks.load_notes(self.slug, self.talk)["notes"])
            self.assertNotIn("resolves=", talks.read_deck(self.slug, self.talk))

    def test_a_scientist_reply_is_consumed_into_its_thread_without_resolving_the_mark(self):
        """The deck is the worker's only write surface; its reply block is self-erasing."""
        with paths.use_root(self.home):
            note = talks.add_note(self.slug, self.talk, slide=1, kind="bad", author="pi",
                                  quote="which kills the variance term", text="Not in the tail.")
            base = self._deck_bytes()
        reply = ("\n<!-- lockedin-reply: " + note["id"] + " -->\n"
                 "The covariance term is exact; I replaced that approximation on slide 2.\n"
                 "<!-- /lockedin-reply -->\n")
        write = {"path": f"reports/talks/{self.talk}.md",
                 "content_b64": base64.b64encode(base + reply.encode()).decode(),
                 "base_revision": scientist_sync.revision(base)}
        result = scientist_sync.apply_writes(self.home, self.slug, [write], actor="talks")
        self.assertEqual(result["conflicts"], [])
        self.assertIn("content_b64", result["applied"][0])
        # A lost response retries with an obsolete deck revision. It is rejected before it can
        # duplicate the answer; the worker adopts the canonical bytes from that conflict.
        again = scientist_sync.apply_writes(self.home, self.slug, [write])
        self.assertEqual(len(again["conflicts"]), 1)
        self.assertEqual(again["conflicts"][0]["reason"], "stale revision")
        with paths.use_root(self.home):
            stored = talks.read_deck(self.slug, self.talk)
            thread = talks.load_notes(self.slug, self.talk)["notes"][note["id"]]
        self.assertNotIn("lockedin-reply", stored)
        self.assertEqual([(m["author"], m["body"]) for m in thread["messages"]], [
            ("pi", "Not in the tail."),
            ("agent on behalf of talks", "The covariance term is exact; I replaced that approximation on slide 2."),
        ])
        self.assertTrue(thread["messages"][-1]["agent"])

    def test_an_unknown_reply_target_rejects_the_whole_deck_write(self):
        with paths.use_root(self.home):
            base = self._deck_bytes()
        raw = base + b"\n<!-- lockedin-reply: no-such-note -->\nNope.\n<!-- /lockedin-reply -->\n"
        result = scientist_sync.apply_writes(self.home, self.slug, [{
            "path": f"reports/talks/{self.talk}.md",
            "content_b64": base64.b64encode(raw).decode(),
            "base_revision": scientist_sync.revision(base),
        }])
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertIn("no such chalk-talk mark", result["conflicts"][0]["reason"])
        with paths.use_root(self.home):
            self.assertEqual(talks.read_deck(self.slug, self.talk).encode(), base)

    def _deck_bytes(self):
        with paths.use_root(self.home):
            return paths.bubble_talk_path(self.slug, self.talk).read_bytes()

    def test_an_agent_cannot_push_the_generated_sidecars(self):
        for rel in (f"reports/talks/{self.talk}.notes.yaml",
                    f"reports/talks/{self.talk}.history.yaml",
                    "reports/talks/talks.yaml",
                    "reports/talks/nested/deck.md"):
            self.assertFalse(scientist_sync.writable_path(self.slug, rel), rel)


class SlideCitationTests(unittest.TestCase):
    """A citation means the same thing on a slide as in the document."""

    def setUp(self):
        self.ctx = temp_home()
        self.home = self.ctx.__enter__()
        with paths.use_root(self.home):
            self.slug = bubbles.create_bubble("Diffusion noise schedules")
            bubbles.approve_bubble(self.slug)
            bubbles.ensure_pages(self.slug)

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def test_a_key_cited_only_on_a_slide_still_gets_a_number(self):
        """Otherwise `\\cite{}` on a slide renders as `?key` forever.

        The reference registry used to scan report pages only, so a source introduced in a talk
        had no number and no link — which is exactly when an agent cites one.
        """
        from lockedin import server
        with paths.use_root(self.home):
            talks.create_talk(self.slug, "Setup", date="2026-08-27", body=(
                "<!-- slide: kind=setup, date=2026-08-27, v=1 -->\n"
                "# Setup\n\n*Why this matters.*\n\nFollowing \\cite{ho2020denoising}.\n"))
            pages = bubbles.list_pages(self.slug)
        with patch.object(server, "_bubble_bibliography", return_value={
                "ho2020denoising": {"key": "ho2020denoising", "text": "Ho et al.",
                                    "type": "inproceedings", "fields": {}, "pdf_id": "abc123"}}):
            refs = server._bubble_refs(self.home, self.slug, pages)
        self.assertEqual(refs["citeMap"], {"ho2020denoising": 1})
        self.assertEqual(refs["bibliography"]["ho2020denoising"]["pdf_id"], "abc123")


class SlideCaptionTests(unittest.TestCase):
    def test_maths_in_a_figure_caption_cannot_break_out_of_the_tag(self):
        """A live agent wrote `![... $\\lambda$ ...](assets/x.png)` and the slide leaked HTML.

        Alt text becomes an attribute, so stashing and re-rendering maths inside it injects
        markup into the attribute. The SPA already guarded report pages; slides did not.
        """
        js = (Path(__file__).resolve().parents[1] / "src/lockedin/web/talks.js").read_text()
        self.assertIn("@@LICAP", js)
        self.assertIn("captions.push(cap)", js)
        # The guard has to run before the math pass, or it is pointless.
        self.assertLess(js.index("@@LICAP"), js.index("stashMath(guarded)"))


class ClientScanTests(unittest.TestCase):
    def test_the_client_scans_and_pushes_a_locally_written_deck(self):
        """Writing one file is the documented way to create a talk, and it did not work.

        The client's notion of pushable report content predated chalk talks, so a deck an agent
        wrote was never scanned — it stayed local forever while the server would have taken it.
        """
        from lockedin import scientist_cli
        src = Path(scientist_cli.__file__).read_text()
        scan = src[src.index("def _report_paths"):src.index("def unsynced_figures")]
        self.assertIn('"talks"', scan)
        self.assertIn('endswith(".md")', scan)   # sidecars beside a deck stay the server's
        self.assertIn('"reports/talks/"', src)   # and the push/prune filters know about them


class CardOverflowTests(unittest.TestCase):
    def test_a_quoted_formula_can_break_inside_its_card(self):
        """Raw LaTeX has almost no break opportunities and ran out of the card.

        The quote is deliberately shown unrendered — rendering it would stop it looking like
        the text it anchors to — so it must be allowed to break anywhere instead.
        """
        js = (Path(__file__).resolve().parents[1] / "src/lockedin/web/talks.js").read_text()
        block = js[js.index(".tk-note .qt{"):js.index(".tk-note .qt{") + 320]
        self.assertIn("overflow-wrap:anywhere", block)
        self.assertIn("word-break:break-word", block)


class RouteSurfaceTests(unittest.TestCase):
    def test_every_talk_route_the_ui_calls_exists(self):
        """Two cleanups have now silently deleted a route by cutting a source range.

        The snapshot upload disappeared and only a live browser run noticed, because nothing
        asserted the surface. This walks the app's real routing table.
        """
        from lockedin import server
        app = server.build_app()
        seen = {(m, r.path) for r in app.routes for m in getattr(r, "methods", []) or []}
        required = [
            ("GET", "/api/bubbles/{slug}/talks"),
            ("POST", "/api/bubbles/{slug}/talks"),
            ("GET", "/api/bubbles/{slug}/talks/{talk_id}"),
            ("PATCH", "/api/bubbles/{slug}/talks/{talk_id}"),
            ("DELETE", "/api/bubbles/{slug}/talks/{talk_id}"),
            ("POST", "/api/bubbles/{slug}/talks/{talk_id}/notes"),
            ("PATCH", "/api/bubbles/{slug}/talks/{talk_id}/notes/{note_id}"),
            ("POST", "/api/bubbles/{slug}/talks/{talk_id}/notes/{note_id}/replies"),
            ("DELETE", "/api/bubbles/{slug}/talks/{talk_id}/notes/{note_id}"),
            ("PUT", "/api/bubbles/{slug}/talks/{talk_id}/notes/{note_id}/shot.png"),
            ("GET", "/api/bubbles/{slug}/talks/{talk_id}/notes/{note_id}/shot.png"),
            ("PUT", "/api/bubbles/{slug}/talks/{talk_id}/slides/{slide}/source"),
            ("POST", "/api/bubbles/{slug}/talks/{talk_id}/slides"),
            ("DELETE", "/api/bubbles/{slug}/talks/{talk_id}/slides/{slide}"),
            ("GET", "/api/bubbles/{slug}/talk-notes"),
            ("PUT", "/api/bubbles/{slug}/premise"),
        ]
        for method, path in required:
            self.assertIn((method, path), seen, f"{method} {path} is missing")


class SlideFigureTests(unittest.TestCase):
    def test_a_slide_resolves_assets_links_like_a_report_page(self):
        """`assets/x.png` in a slide used to 404: only page rendering rewrote the link."""
        js = (Path(__file__).resolve().parents[1] / "src/lockedin/web/talks.js").read_text()
        self.assertIn("function resolveAssetLinks(md)", js)
        self.assertIn("/api/bubbles/${encodeURIComponent(S.slug)}/assets/", js)
        # Anchored on the link, not the caption: a caption holding `]` (any LaTeX interval)
        # defeated a caption-shaped pattern and the figure silently 404'd.
        self.assertIn(r"\]\(assets\/([^\s)]+)\)", js)
        # And the same viewer the pages use, by selector so re-renders need no re-binding.
        self.assertIn('window.LockedInLightbox.watch(".tk-md")', js)


class PageMarkTests(unittest.TestCase):
    """Marks on report pages share the five kinds but keep the wrapper as their anchor."""

    def setUp(self):
        self.ctx = temp_home()
        self.home = self.ctx.__enter__()
        with paths.use_root(self.home):
            self.slug = bubbles.create_bubble("Diffusion noise schedules")
            bubbles.approve_bubble(self.slug)
            bubbles.ensure_pages(self.slug)
            bubbles.save_page(self.slug, "overview", "# Overview\n\nThe score is trivial here.\n")

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def test_a_mark_alone_is_a_complete_comment(self):
        with paths.use_root(self.home):
            content = bubbles.get_page(self.slug, "overview")
            at = content.index("trivial")
            result = bubbles.create_comment_state(
                self.slug, "overview", "pi", "", content=content, base_mtime=None,
                selection_start=at, selection_end=at + len("trivial"), kind="q")
            thread = result["thread"]
        # "?" on a sentence says everything it needs to; prose is optional once a kind is picked.
        self.assertEqual(thread["kind"], "q")
        self.assertEqual(thread["messages"], [])
        self.assertEqual(thread["anchor"]["quote"], "trivial")

    def test_an_unknown_kind_is_refused(self):
        with paths.use_root(self.home):
            content = bubbles.get_page(self.slug, "overview")
            with self.assertRaises(ValueError):
                bubbles.create_comment_state(
                    self.slug, "overview", "pi", "x", content=content, base_mtime=None,
                    selection_start=0, selection_end=1, kind="shrug")

    def test_a_prose_less_and_kind_less_comment_is_still_refused(self):
        with paths.use_root(self.home):
            content = bubbles.get_page(self.slug, "overview")
            with self.assertRaises(ValueError):
                bubbles.create_comment_state(
                    self.slug, "overview", "pi", "", content=content, base_mtime=None,
                    selection_start=0, selection_end=1, kind="")

    def test_page_marks_and_slide_marks_land_in_one_feedback_file(self):
        with paths.use_root(self.home):
            content = bubbles.get_page(self.slug, "overview")
            at = content.index("trivial")
            bubbles.create_comment_state(self.slug, "overview", "pi", "Trivial by what measure?",
                                         content=content, base_mtime=None, selection_start=at,
                                         selection_end=at + len("trivial"), kind="q")
            talk = talks.create_talk(self.slug, "Variance", date="2026-08-27", body=DECK)
            talks.add_note(self.slug, talk, slide=0, kind="bad", author="pi",
                           quote="Sample the noise level", text="Not uniformly.")
            body = feedback.open_markdown(self.slug).decode()
        self.assertIn("*(report page)*", body)
        self.assertIn("*(chalk talk", body)
        self.assertIn("Trivial by what measure?", body)
        self.assertIn("Not uniformly.", body)
        # The five kinds are spelled out once, at the top, for both surfaces.
        self.assertIn("I don't follow", body)
        self.assertIn("this is wrong", body)


if __name__ == "__main__":
    unittest.main()


class ManualEditTests(unittest.TestCase):
    """The human's pen: hand-editing slides with marks materialised as wrappers."""

    def setUp(self):
        self.ctx = temp_home()
        self.home = self.ctx.__enter__()
        with paths.use_root(self.home):
            self.slug = bubbles.create_bubble("Diffusion noise schedules")
            bubbles.approve_bubble(self.slug)
            bubbles.ensure_pages(self.slug)
            self.talk = talks.create_talk(self.slug, "Why the variance term doesn't vanish",
                                          date="2026-08-27", body=DECK)

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def test_edit_source_materialises_marks_as_comment_wrappers(self):
        with paths.use_root(self.home):
            talks.add_note(self.slug, self.talk, slide=1, kind="bad", author="pi",
                           quote="kills the variance term", text="does it?")
            detail = talks.talk_detail(self.slug, self.talk)
        src = detail["slides"][1]["edit_source"]
        self.assertIn("<comment-begin=n1>kills the variance term<comment-end=n1>", src)
        # The slide header comment is the app's bookkeeping, not the editor's business.
        self.assertNotIn("<!-- slide:", src)

    def test_saving_a_wrapper_moves_the_mark_with_the_edited_text(self):
        with paths.use_root(self.home):
            talks.add_note(self.slug, self.talk, slide=1, kind="bad", author="pi",
                           quote="kills the variance term", text="does it?")
            detail = talks.talk_detail(self.slug, self.talk)
            edited = detail["slides"][1]["edit_source"].replace(
                "<comment-begin=n1>kills the variance term<comment-end=n1>",
                "<comment-begin=n1>silences the variance term<comment-end=n1>")
            talks.apply_slide_source(self.slug, self.talk, 1, edited)
            after = talks.talk_detail(self.slug, self.talk)
        note = after["notes"][0]
        self.assertEqual(note["quote"], "silences the variance term")
        self.assertFalse(note["orphan"])
        # No wrapper syntax may leak into the stored deck.
        self.assertNotIn("comment-begin", after["slides"][1]["body"])

    def test_overlapping_wrappers_keep_both_marks_visible_and_saveable(self):
        with paths.use_root(self.home):
            talks.add_note(self.slug, self.talk, slide=1, kind="q", author="pi",
                           quote="I assume $w(\\lambda) \\to \\text{const}$, which kills",
                           text="This premise needs a condition.")
            talks.add_note(self.slug, self.talk, slide=1, kind="more", author="pi",
                           quote="which kills the variance term.",
                           text="Explain this consequence.")
            detail = talks.talk_detail(self.slug, self.talk)
            source = detail["slides"][1]["edit_source"]
            self.assertIn("<comment-begin=n1>", source)
            self.assertIn("<comment-begin=n2>", source)
            # The ranges cross, so the first close appears before the second close.
            self.assertLess(source.index("<comment-end=n1>"), source.index("<comment-end=n2>"))
            talks.apply_slide_source(self.slug, self.talk, 1, source)
            after = talks.talk_detail(self.slug, self.talk)
        self.assertEqual({note["quote"] for note in after["notes"]}, {
            "I assume $w(\\lambda) \\to \\text{const}$, which kills",
            "which kills the variance term."})
        self.assertTrue(all(not note["orphan"] for note in after["notes"]))

    def test_an_unchanged_save_keeps_the_slide_date(self):
        with paths.use_root(self.home):
            detail = talks.talk_detail(self.slug, self.talk)
            talks.apply_slide_source(self.slug, self.talk, 0,
                                     detail["slides"][0]["edit_source"])
            after = talks.talk_detail(self.slug, self.talk)
        self.assertEqual(after["slides"][0]["date"], "2026-08-27")

    def test_renaming_the_first_slide_renames_the_talk(self):
        with paths.use_root(self.home):
            detail = talks.talk_detail(self.slug, self.talk)
            # The registry deliberately keeps its own title against agent pushes; a hand
            # rename of slide 1 is the owner speaking and must win.
            talks.save_index(self.slug, {"talks": [
                {**talks.load_index(self.slug)["talks"][0], "title": "What we're changing"}]})
            edited = detail["slides"][0]["edit_source"].replace(
                "# What we're changing", "# Uniform in log-SNR")
            talks.apply_slide_source(self.slug, self.talk, 0, edited)
            self.assertEqual(talks.load_index(self.slug)["talks"][0]["title"],
                             "Uniform in log-SNR")

    def test_a_separator_inside_a_slide_is_refused(self):
        with paths.use_root(self.home):
            with self.assertRaises(ValueError):
                talks.apply_slide_source(self.slug, self.talk, 0, "# T\n\nbefore\n\n---\n\nafter")

    def test_insert_slide_shifts_marks_on_later_slides(self):
        with paths.use_root(self.home):
            talks.add_note(self.slug, self.talk, slide=1, kind="q", author="pi",
                           quote="The residual term survives")
            pos = talks.insert_slide(self.slug, self.talk, after=0)
            detail = talks.talk_detail(self.slug, self.talk)
        self.assertEqual(pos, 1)
        self.assertEqual([s["title"] for s in detail["slides"]],
                         ["What we're changing", "New slide", "The residual term survives"])
        note = detail["notes"][0]
        self.assertEqual(note["slide"], 2)          # followed its slide
        self.assertFalse(note["orphan"])

    def test_delete_slide_takes_its_marks_and_shifts_the_rest(self):
        with paths.use_root(self.home):
            talks.add_note(self.slug, self.talk, slide=0, kind="cut", author="pi",
                           quote="Sample the noise level")
            talks.add_note(self.slug, self.talk, slide=1, kind="q", author="pi",
                           quote="The residual term survives")
            self.assertTrue(talks.delete_slide(self.slug, self.talk, 0))
            detail = talks.talk_detail(self.slug, self.talk)
        self.assertEqual([s["title"] for s in detail["slides"]],
                         ["The residual term survives"])
        self.assertEqual(len(detail["notes"]), 1)   # slide 0's mark died with it
        self.assertEqual(detail["notes"][0]["slide"], 0)
        self.assertFalse(detail["notes"][0]["orphan"])

    def test_malformed_wrappers_are_left_alone_not_eaten(self):
        clean, found = talks._parse_wrappers(
            "a <comment-begin=n1>good<comment-end=n1> b <comment-begin=n2>unclosed")
        self.assertEqual(found[0]["id"], "n1")
        self.assertEqual(clean, "a good b <comment-begin=n2>unclosed")
        # unbalanced braces in a body are unremarkable now
        clean, found = talks._parse_wrappers(
            "<comment-begin=n3>a { lone } brace { mess<comment-end=n3>")
        self.assertEqual(found[0]["body"], "a { lone } brace { mess")
        self.assertEqual(clean, "a { lone } brace { mess")


class DeckPushEchoTests(unittest.TestCase):
    """A deck push must report the *stored* deck, not the raw bytes it absorbed.

    absorb_push rewrites the file (headers canonicalised, resolves= consumed), so echoing
    revision(raw) left the worker tracking a revision the manifest never shows. Every cycle
    after a deck push then read as "remote changed" — and an agent's follow-up edit in that
    window was stashed as a conflict and overwritten with the server copy. A live haiku run
    lost its `resolves=` write to exactly this.
    """

    def setUp(self):
        self.ctx = temp_home()
        self.home = self.ctx.__enter__()
        with paths.use_root(self.home):
            self.slug = bubbles.create_bubble("Diffusion noise schedules")
            bubbles.approve_bubble(self.slug)
            bubbles.ensure_pages(self.slug)
            self.talk = talks.create_talk(self.slug, "Why the variance term doesn't vanish",
                                          date="2026-08-27", body=DECK)

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def test_push_echo_matches_manifest_and_returns_canonical_bytes(self):
        rel = f"reports/talks/{self.talk}.md"
        raw = DECK.replace("Here I assume", "Now I assume").encode()
        pushed = base64.b64encode(raw).decode()
        with paths.use_root(self.home):
            base = scientist_sync.revision(
                talks.read_deck(self.slug, self.talk).encode())
        result = scientist_sync.apply_writes(self.home, self.slug, [
            {"path": rel, "base_revision": base, "content_b64": pushed}])
        self.assertEqual(result["conflicts"], [])
        entry = result["applied"][0]
        with paths.use_root(self.home):
            stored = talks.read_deck(self.slug, self.talk).encode()
        # The echoed revision is the manifest's revision, and the canonical bytes ride along
        # whenever they differ from what was pushed — the client adopts them, so the next
        # cycle sees neither "local changed" nor "remote changed".
        self.assertEqual(entry["revision"], scientist_sync.revision(stored))
        if stored != raw:
            self.assertEqual(base64.b64decode(entry["content_b64"]), stored)


class InkNoteTests(unittest.TestCase):
    """✍ — freehand drawings as a first-class mark, chalk-talk-only."""

    def setUp(self):
        self.ctx = temp_home()
        self.home = self.ctx.__enter__()
        with paths.use_root(self.home):
            self.slug = bubbles.create_bubble("Diffusion noise schedules")
            bubbles.approve_bubble(self.slug)
            bubbles.ensure_pages(self.slug)
            self.talk = talks.create_talk(self.slug, "Why the variance term doesn't vanish",
                                          date="2026-08-27", body=DECK)

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    STROKES = [[{"x": 10, "y": 20}, {"x": 30, "y": 22}, {"x": 55, "y": 25}],
               [{"x": 60, "y": 70}, {"x": 61, "y": 90}]]

    def test_a_drawing_alone_is_a_complete_anchor(self):
        with paths.use_root(self.home):
            note = talks.add_note(self.slug, self.talk, slide=0, kind="ink", author="pi",
                                  paths=self.STROKES)
            detail = talks.talk_detail(self.slug, self.talk)
        self.assertEqual(len(note["paths"]), 2)
        self.assertFalse(detail["notes"][0]["orphan"])   # nothing to orphan: no quote

    def test_strokes_are_clamped_rounded_and_bounded(self):
        wild = [[{"x": -5, "y": 120.567}, {"x": 30.129, "y": 22}],
                [{"x": 1, "y": 1}]]                       # single point: not a stroke
        cleaned = talks._clean_paths(wild)
        self.assertEqual(cleaned, [[{"x": 0.0, "y": 100.0}, {"x": 30.13, "y": 22.0}]])
        self.assertIsNone(talks._clean_paths([]))
        self.assertIsNone(talks._clean_paths([[{"x": "junk"}]]))

    def test_feedback_and_agent_payload_point_at_the_picture(self):
        with paths.use_root(self.home):
            talks.add_note(self.slug, self.talk, slide=0, kind="ink", author="pi",
                           paths=self.STROKES, text="apply what I drew")
            blocks = "\n".join(talks.feedback_blocks(self.slug))
            payload = talks.open_notes_for_agent(self.slug)
        self.assertIn("✍", blocks)
        self.assertIn("freehand drawing", blocks)
        # No picture captured (no browser here) — the file must say so, not send the agent
        # hunting for a shot that does not exist.
        self.assertIn("no picture was captured", blocks)
        self.assertEqual(payload[0]["anchor"], {"type": "drawing", "strokes": 2})

    def test_covers_ride_along_as_the_text_fallback(self):
        with paths.use_root(self.home):
            talks.add_note(self.slug, self.talk, slide=0, kind="ink", author="pi",
                           paths=self.STROKES,
                           covers=["Sample the noise level", "uniformly in log-SNR"])
            blocks = "\n".join(talks.feedback_blocks(self.slug))
            payload = talks.open_notes_for_agent(self.slug)
        self.assertIn("the ink touches", blocks)
        self.assertIn("“Sample the noise level”", blocks)
        self.assertEqual(payload[0]["anchor"]["touches"],
                         ["Sample the noise level", "uniformly in log-SNR"])

    def test_an_empty_note_is_still_refused(self):
        with paths.use_root(self.home):
            with self.assertRaises(ValueError):
                talks.add_note(self.slug, self.talk, slide=0, kind="ink", author="pi",
                               paths=[[{"x": 1, "y": 1}]])   # cleans to nothing
