"""Chalk talks: decks, the marks left on them, and what a syncing agent is handed.

Deterministic — no network, no LLM. Every case here is a bug this feature actually had.
"""
from __future__ import annotations

import base64
import unittest
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

            talks.revise_slide(self.slug, self.talk, 1, why="rewritten",
                               body="No constancy assumption is made.")
            detail = talks.talk_detail(self.slug, self.talk)
            # Losing a reviewer's objection silently is the one unacceptable failure.
            self.assertTrue(detail["notes"][0]["orphan"])
            self.assertEqual(detail["notes"][0]["quote"],
                             "Here I assume $w(\\lambda) \\to \\text{const}$")

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
    def test_a_revision_deletes_the_marks_it_answers_and_keeps_why(self):
        with paths.use_root(self.home):
            note = talks.add_note(self.slug, self.talk, slide=1, kind="bad", author="pi",
                                  quote="which kills the variance term", text="Not in the tail.")
            talks.save_note_image(self.slug, self.talk, note["id"], b"\x89PNG\r\n\x1a\nfake")
            self.assertTrue(talks.note_image_path(self.slug, self.talk, note["id"]).exists())

            talks.revise_slide(self.slug, self.talk, 1, why="you marked it wrong",
                               body="The residual is O(1) there.", resolves=[note["id"]])

            # Gone: sidecar entry and snapshot both. An addressed mark that lingers is the
            # clutter this design exists to avoid.
            self.assertEqual(talks.load_notes(self.slug, self.talk)["notes"], {})
            self.assertFalse(talks.note_image_path(self.slug, self.talk, note["id"]).exists())
            # Kept: what the mark asked for, in the version history.
            version = talks.load_history(self.slug, self.talk)["versions"][-1]
            self.assertEqual(version["marks"][0]["mark"], "bad")
            self.assertEqual(version["marks"][0]["comment"], "Not in the tail.")
            self.assertIn("you marked it wrong", version["why"])
            self.assertEqual(talks.parse_deck(talks.read_deck(self.slug, self.talk))[1]["version"], 2)


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

    def test_feedback_disappears_entirely_once_nothing_is_open(self):
        with paths.use_root(self.home):
            note = talks.add_note(self.slug, self.talk, slide=0, kind="q", author="pi",
                                  quote="Sample the noise level", text="Uniformly in what?")
        self.assertIn("feedback/OPEN.md", scientist_sync._files(self.home, self.slug))
        with paths.use_root(self.home):
            talks.revise_slide(self.slug, self.talk, 0, why="answered", body="Now explained.",
                               resolves=[note["id"]])
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

    def test_a_pushed_slide_resolves_the_marks_its_header_names(self):
        """The file is the agent's whole interface, so it must be able to finish the job.

        A live Claude session revised a slide and wrote `resolves=n1,n2` into the slide header
        unprompted, then could not clear the marks because resolution needed a second HTTP call
        it had no way to make. The push honours the header instead.
        """
        with paths.use_root(self.home):
            n1 = talks.add_note(self.slug, self.talk, slide=1, kind="bad", author="pi",
                                quote="which kills the variance term", text="Not in the tail.")
            n2 = talks.add_note(self.slug, self.talk, slide=1, kind="q", author="pi",
                                quote="Here I assume", text="Where did that come from?")
        revised = DECK.replace(
            "<!-- slide: kind=derivation, date=2026-08-27, v=1 -->",
            f"<!-- slide: kind=derivation, date=2026-08-27, v=1, "
            f"resolves={n1['id']},{n2['id']}, why=re-derived without the assumption -->"
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
            self.assertEqual(talks.load_notes(self.slug, self.talk)["notes"], {})
            slides = talks.parse_deck(talks.read_deck(self.slug, self.talk))
            self.assertEqual(slides[1]["version"], 2)
            self.assertEqual(slides[0]["version"], 1)   # untouched slides are not bumped
            version = talks.load_history(self.slug, self.talk)["versions"][-1]
            self.assertIn("re-derived", version["why"])
            self.assertEqual({m["mark"] for m in version["marks"]}, {"bad", "q"})
            # The directive is consumed, not left lying in the stored deck.
            self.assertNotIn("resolves=", talks.read_deck(self.slug, self.talk))

    def _deck_bytes(self):
        with paths.use_root(self.home):
            return paths.bubble_talk_path(self.slug, self.talk).read_bytes()

    def test_an_agent_cannot_push_the_generated_sidecars(self):
        for rel in (f"reports/talks/{self.talk}.notes.yaml",
                    f"reports/talks/{self.talk}.history.yaml",
                    "reports/talks/talks.yaml",
                    "reports/talks/nested/deck.md"):
            self.assertFalse(scientist_sync.writable_path(self.slug, rel), rel)


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
