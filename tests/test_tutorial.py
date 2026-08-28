"""Every new workspace opens with the Tutorial bubble — the product demoing itself."""
from __future__ import annotations

import unittest

from lockedin import bubbles, paths, talks, tutorial, workspaces

from tests.test_editing_logic import temp_home


class TutorialSeedTests(unittest.TestCase):
    def setUp(self):
        self.ctx = temp_home()
        self.home = self.ctx.__enter__()

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def _seeded(self):
        with paths.use_root(self.home):
            slug = tutorial.seed()
            self.assertIsNotNone(slug)
            return slug

    def test_the_bubble_shows_every_feature(self):
        slug = self._seeded()
        with paths.use_root(self.home):
            info = bubbles.bubble_detail(slug)
            pages = bubbles.list_pages(slug)
            decks = talks.list_talks(slug)
            notes = talks.open_notes_for_agent(slug)
            threads = bubbles.list_comments(slug, "the-five-marks")["threads"]

        # premise, multi-page document, two decks
        self.assertIn("worked example", info["abstract"])
        self.assertTrue(info["goal"])
        self.assertGreaterEqual(len(pages), 4)
        self.assertEqual(len(decks), 2)

        # open marks of every flavour: text, region, drawing, plus an agent reply in a thread
        kinds = {n["mark"] for n in notes}
        self.assertIn("bad", kinds)
        self.assertIn("q", kinds)
        self.assertIn("good", kinds)
        self.assertIn("ink", kinds)
        anchor_types = {n["anchor"]["type"] for n in notes}
        self.assertEqual(anchor_types, {"text", "region", "drawing"})
        replied = [n for n in notes if len(n["conversation"]) > 1]
        self.assertTrue(replied)

        # the drawing ships with its picture: thumbnail in the app, snapshot for the agent
        ink = [n for n in notes if n["mark"] == "ink"][0]
        self.assertIn("screenshot", ink)

        # the page comment thread carries the agent's answer
        self.assertEqual(len(threads), 1)
        self.assertEqual(len(threads[0]["messages"]), 2)

    def test_the_history_carries_a_resolved_mark(self):
        slug = self._seeded()
        with paths.use_root(self.home):
            deck = [t for t in talks.list_talks(slug) if "marks-beat" in t["id"]][0]
            detail = talks.talk_detail(slug, deck["id"])
        s2 = detail["slides"][1]
        self.assertEqual(s2["version"], 2)
        self.assertEqual(len(s2["history"]), 1)
        self.assertEqual(s2["history"][0]["marks"][0]["mark"], "bad")
        # the resolved ✗ is deleted, not archived
        self.assertFalse([n for n in detail["notes"] if n["slide"] == 1])

    def test_no_note_is_born_an_orphan(self):
        slug = self._seeded()
        with paths.use_root(self.home):
            for t in talks.list_talks(slug):
                for n in talks.talk_detail(slug, t["id"])["notes"]:
                    self.assertFalse(n["orphan"], f"{t['id']}/{n['id']} is an orphan at birth")

    def test_workspace_creation_seeds_it_and_deletion_sticks(self):
        rec = workspaces.create("alice", "Fresh workspace")
        home = workspaces.workspace_home(rec["id"])
        with paths.use_root(home):
            slugs = [b["slug"] for b in bubbles.all_bubbles()]
            self.assertEqual(len(slugs), 1)
            bubbles.delete_bubble(slugs[0])
            self.assertEqual(bubbles.all_bubbles(), [])
