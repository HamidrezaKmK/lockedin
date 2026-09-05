"""Every surface that documents the large-asset commands must document all of them.

`rm` was added to the guides, the README's neighbours, and the `assets` listing, but not to the
welcome screen a user sees when they type `lockedin-scientist` with no arguments — so the one
place a person looks first kept describing a two-way feature that had become three-way. Listing
commands in five places is only safe if something checks they agree.
"""
from __future__ import annotations

import contextlib
import io as _io
import pathlib
import unittest

from lockedin import reports
from lockedin.scientist_cli import welcome

COMMANDS = ("lockedin-scientist assets", "assets pull", "assets push", "assets rm")
REPO = pathlib.Path(__file__).resolve().parents[1]


def welcome_text() -> str:
    buffer = _io.StringIO()
    with contextlib.redirect_stdout(buffer):
        welcome()
    return buffer.getvalue()


class CommandSurfaceTests(unittest.TestCase):
    def test_the_welcome_screen_lists_every_asset_command(self):
        text = welcome_text()
        for command in COMMANDS:
            self.assertIn(command, text, f"`lockedin-scientist` with no arguments omits {command}")

    def test_the_web_help_lists_every_asset_command(self):
        guide = reports.guide_section("Scientist CLI")
        for command in COMMANDS:
            self.assertIn(command, guide)

    def test_the_agent_skill_lists_every_asset_command(self):
        guide = reports.guide_section("Editing Guide")
        for command in COMMANDS:
            self.assertIn(command, guide)

    def test_the_readme_lists_every_asset_command(self):
        readme = (REPO / "README.md").read_text()
        for command in COMMANDS:
            self.assertIn(command, readme)

    def test_the_welcome_screen_says_deleting_locally_is_not_enough(self):
        # The single most surprising thing about a large asset, and the reason rm exists.
        self.assertIn("deleting it locally does not", welcome_text())


if __name__ == "__main__":
    unittest.main()
