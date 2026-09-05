"""The guide only reaches an agent if SKILL.md is regenerated.

A project rewrites its SKILL.md when the version marker in its copy stops matching
``SKILL_VERSION`` — so editing the guide text without moving that constant changes what the
server serves while every existing project keeps handing its agent the old document. That is not
a hypothetical: it is how an agent came to diagnose a deliberately-unsynced 1.66 GB asset as a
failed upload, and to propose re-encoding videos that a command it had never heard of could send
as they were.

These pin the two halves: the marker is derived from the constant rather than typed, and the
generated skill actually carries the guidance it is supposed to carry.
"""
from __future__ import annotations

import unittest

from lockedin import reports
from lockedin.scientist_cli import SKILL_VERSION, skill_document


class SkillFreshnessTests(unittest.TestCase):
    def test_the_version_marker_is_derived_from_the_constant(self):
        # If these drift, a project either pins itself to a stale guide forever or rewrites the
        # skill on every five-second sync.
        document = skill_document()
        self.assertIn(f"<!-- lockedin-scientist-skill: {SKILL_VERSION} -->", document)
        self.assertEqual(document.splitlines()[0],
                         f"<!-- lockedin-scientist-skill: {SKILL_VERSION} -->")

    def test_only_one_marker_is_emitted(self):
        self.assertEqual(skill_document().count("lockedin-scientist-skill:"), 1)

    def test_the_staleness_check_matches_what_is_written(self):
        # The check is a substring test for this exact string; prove the written document
        # satisfies it, so a fresh project is never considered stale on its next sync.
        self.assertIn(f"lockedin-scientist-skill: {SKILL_VERSION}", skill_document())

    def test_the_editing_guide_still_carries_the_large_file_commands(self):
        # The agent-facing guide is the Editing Guide; the CLI section never reaches SKILL.md.
        guide = reports.guide_section("Editing Guide")
        for expected in ("## Large files", "lockedin-scientist assets",
                         "assets pull", "assets push", "assets rm"):
            self.assertIn(expected, guide,
                          "the agent's guide must say how to move a large file; "
                          "if you changed this text, bump SKILL_VERSION so projects pick it up")


if __name__ == "__main__":
    unittest.main()
