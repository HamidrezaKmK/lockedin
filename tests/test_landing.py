from __future__ import annotations

import os
import tempfile
import unittest

from lockedin import landing, server


class LandingConfigTest(unittest.TestCase):
    def setUp(self):
        self._old_home = os.environ.get("LOCKEDIN_HOME")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["LOCKEDIN_HOME"] = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()
        if self._old_home is None:
            os.environ.pop("LOCKEDIN_HOME", None)
        else:
            os.environ["LOCKEDIN_HOME"] = self._old_home

    def write_landing(self, text: str) -> None:
        path = landing.landing_yaml_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_missing_file_uses_defaults(self):
        cfg = landing.load_landing()
        self.assertEqual(cfg["hero"]["title_accent"], "locked")
        self.assertGreaterEqual(len(cfg["components"]["features"]), 1)
        self.assertIn("curl -fsSL", cfg["scientist"]["platforms"][0]["command"])

    def test_partial_yaml_merges_with_defaults(self):
        self.write_landing("""
hero:
  lede: "Custom lede"
footer: "Custom footer"
""")
        cfg = landing.load_landing()
        self.assertEqual(cfg["hero"]["lede"], "Custom lede")
        self.assertEqual(cfg["footer"], "Custom footer")
        self.assertEqual(cfg["hero"]["title_rest"], "in")
        self.assertGreaterEqual(len(cfg["workflow"]["steps"]), 1)

    def test_scientist_install_section_is_configurable(self):
        self.write_landing("""
scientist:
  title: "Custom CLI"
  platforms:
    - title: "Unix"
      text: "A shell"
      command: "install-me"
  steps:
    - title: "Go"
      text: "Do it"
      command: "run-me"
""")
        cfg = landing.load_landing()
        self.assertEqual(cfg["scientist"]["title"], "Custom CLI")
        self.assertEqual(cfg["scientist"]["platforms"], [{"title": "Unix", "text": "A shell", "command": "install-me"}])
        self.assertEqual(cfg["scientist"]["steps"], [{"title": "Go", "text": "Do it", "command": "run-me"}])

    def test_retired_scientist_landing_commands_self_heal_to_v2(self):
        self.write_landing("""
scientist:
  title: "Custom CLI"
  platforms:
    - title: "Unix"
      text: "A shell"
      command: "curl -fsSL https://raw.githubusercontent.com/HamidrezaKmK/lockedin/scientist/install.sh | bash"
  steps:
    - title: "Start working"
      text: "Old workflow"
      command: "lockedin-scientist <codex|claude|agy> <bubble-name>"
""")
        cfg = landing.load_landing()
        self.assertEqual(cfg["scientist"]["title"], "Custom CLI")
        self.assertIn("/lockedin/main/install.sh", cfg["scientist"]["platforms"][0]["command"])
        # The replacement is whatever the current default flow is — today the three-step
        # hit-🤖 / paste / run-your-agent story, with the paste step carrying the command.
        self.assertEqual(len(cfg["scientist"]["steps"]), 3)
        self.assertIn("setup/", cfg["scientist"]["steps"][1]["command"])

    def test_invalid_yaml_falls_back_to_defaults(self):
        self.write_landing("hero: [")
        cfg = landing.load_landing()
        self.assertEqual(cfg["hero"]["title_accent"], "locked")
        self.assertEqual(cfg["auth"]["title"], "Enter your workspace")

    def test_invalid_types_are_normalized(self):
        self.write_landing("""
hero:
  kicker: 123
  points: nope
components:
  features:
    - icon: 42
      title: Feature
      text:
privacy:
  bullets:
    - one
    - 2
""")
        cfg = landing.load_landing()
        self.assertEqual(cfg["hero"]["kicker"], "123")
        self.assertEqual(cfg["hero"]["points"], [])
        self.assertEqual(cfg["components"]["features"][0]["icon"], "42")
        self.assertEqual(cfg["components"]["features"][0]["title"], "Feature")
        self.assertEqual(cfg["components"]["features"][0]["text"], "")
        self.assertEqual(cfg["privacy"]["bullets"], ["one", "2"])

    def test_server_reloads_landing_yaml_on_each_request(self):
        self.write_landing("footer: First\n")
        app = server.build_app()
        endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/landing")
        self.assertIn(b'"footer":"First"', endpoint().body)
        self.write_landing("footer: Second\n")
        self.assertIn(b'"footer":"Second"', endpoint().body)


if __name__ == "__main__":
    unittest.main()
