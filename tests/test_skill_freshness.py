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

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lockedin import reports
from lockedin.scientist_cli import (
    GUIDES,
    SKILL_VERSION,
    VENDOR_SKILL_BOOTSTRAP,
    MANAGED_VENDOR_SKILL_MARKER,
    skill_document,
    write_skill_bundle,
)

SCIENTIST_CLI_SOURCE = Path(__file__).resolve().parents[1] / "src" / "lockedin" / "scientist_cli.py"


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

    def test_the_router_points_an_agent_at_the_agents_guide(self):
        self.assertIn("guides/agents.md", skill_document())

    def test_the_agents_guide_covers_every_agent_subcommand(self):
        guide = GUIDES["agents.md"]
        for expected in ("agent register", "agent reply", "agent fail", "agent chat",
                         "agent revive", "agent reset", "agent retire"):
            self.assertIn(expected, guide)

    def test_skill_version_has_reached_the_agents_feature(self):
        self.assertGreaterEqual(SKILL_VERSION, 42)

    def test_agents_guide_uses_actual_cli_name(self):
        # With the env var set, the guide should use the dev shim name
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            with mock.patch.dict(os.environ, {"LOCKEDIN_SCIENTIST_CLI_NAME": "lockedin-scientist-dev"}):
                write_skill_bundle(tmpdir_path, "## Editing Guide\n\ntext", {})
                agents_guide = (tmpdir_path / "guides" / "agents.md").read_text()
                self.assertIn("lockedin-scientist-dev agent register", agents_guide)
                # Verify the exact substring does NOT appear (dev name contains the base name as a prefix)
                self.assertNotIn("lockedin-scientist agent register", agents_guide)

    def test_bootstrap_and_router_resolve_the_project_root_without_requiring_git(self):
        # A real Windows run: a user connected a plain folder that was not a git repository, and
        # the agent refused with "not a Git repository, so the required project root cannot be
        # resolved" because both documents named `.git` as the primary way to find the root. The
        # root must be findable by walking up for `.lockedin/config/binding.json`, with git kept
        # only as the worktree fallback.
        for document, label in ((VENDOR_SKILL_BOOTSTRAP, "bootstrap"), (skill_document(), "router")):
            self.assertIn(".lockedin/config/binding.json", document, f"{label} must name the root marker file")
            self.assertIn("git is not required", document.lower(),
                          f"{label} must say git is not required to find the project root")

    def test_neither_document_defines_the_root_as_the_git_directory_first(self):
        # These are the exact old phrasings that made `.git` the *primary* rule, one per document.
        # Their absence proves the fix landed, not just that new wording was added alongside it.
        self.assertNotIn("the directory containing the repository's shared", VENDOR_SKILL_BOOTSTRAP,
                         "the bootstrap must not define the root as the directory containing the shared .git")
        self.assertNotIn("the directory holding the shared", skill_document(),
                         "the router must not define the root as the directory holding the shared .git")

    def test_the_worktree_fallback_still_names_the_git_command(self):
        # Git remains the fallback for the worktree case, where `.lockedin/` genuinely cannot be
        # found by walking up from the working directory (it lives in the main checkout instead).
        git_common_dir_command = "git rev-parse --path-format=absolute --git-common-dir"
        self.assertIn(git_common_dir_command, VENDOR_SKILL_BOOTSTRAP)
        self.assertIn(git_common_dir_command, skill_document())

    def test_bootstrap_still_carries_its_vendor_marker_and_front_matter(self):
        self.assertIn(MANAGED_VENDOR_SKILL_MARKER, VENDOR_SKILL_BOOTSTRAP)
        self.assertIn("name: lockedin-scientist", VENDOR_SKILL_BOOTSTRAP)

    def test_skill_version_has_reached_the_git_optional_root_fix(self):
        self.assertGreaterEqual(SKILL_VERSION, 44)

    def test_skill_version_carries_direct_turns_and_talk_local_theorems(self):
        self.assertGreaterEqual(SKILL_VERSION, 48)
        self.assertIn("send you a direct\nmessage", GUIDES["agents.md"])
        feedback = GUIDES["feedback.md"]
        self.assertIn("Those counters and labels are **talk-local**", feedback)
        self.assertIn(r"\thmref{thm:key}", feedback)

    def test_skill_version_carries_agent_revival(self):
        self.assertGreaterEqual(SKILL_VERSION, 49)
        self.assertIn("one-use recovery command", GUIDES["agents.md"])

    def test_editing_reference_guide_distinguishes_page_and_talk_theorem_scope(self):
        guide = reports.guide_section("Editing Guide")
        self.assertIn("numbering continues across that talk's slides", guide)
        self.assertIn("does not enter or read the\nreport-page registry", guide)

    def test_web_help_covers_direct_agent_turns_and_chalk_talk_theorems(self):
        agents_help = reports.guide_section("Agents")
        for expected in ("Queue turn", "one real agent turn", "open lock", "closed lock"):
            self.assertIn(expected, agents_help)
        chalk_help = reports.guide_section("Chalk talks")
        self.assertIn(r"\begin{theorem}[Title]", chalk_help)
        self.assertIn("another slide or inside math", chalk_help)
        self.assertIn("This namespace is deliberately local", chalk_help)

    def test_agents_guide_uses_default_cli_name_without_env(self):
        # Without the env var, the guide should use the default app name
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            with mock.patch.dict(os.environ, {}, clear=False):
                # Ensure the env var is not set
                os.environ.pop("LOCKEDIN_SCIENTIST_CLI_NAME", None)
                write_skill_bundle(tmpdir_path, "## Editing Guide\n\ntext", {})
                agents_guide = (tmpdir_path / "guides" / "agents.md").read_text()
                self.assertIn("lockedin-scientist agent register", agents_guide)


class SkillTextEncodingTests(unittest.TestCase):
    """Pins the Windows cp1252 crash: a real Windows 11 / Python 3.14 ``connect`` run died in
    ``write_skill_bundle`` with ``UnicodeEncodeError: 'charmap' codec can't encode character
    '\\U0001f916'`` because ``Path.write_text``/``read_text``/``open`` without an explicit
    ``encoding=`` fall back to the *locale* encoding (cp1252 on a typical Windows box), while
    every string this client writes is UTF-8. That crash aborted `connect` after the binding was
    written but before the skill was, leaving a project half set up. Every text file operation in
    this module has the same latent failure, not just the one that happened to be hit first.
    """

    # A call that must never specify an encoding (there should be none). Kept so a future,
    # genuinely-exempt call site has somewhere to go instead of weakening the regex below.
    ALLOWED_WITHOUT_ENCODING: set[str] = set()

    def test_every_write_text_and_read_text_call_is_explicitly_utf8(self):
        source = SCIENTIST_CLI_SOURCE.read_text(encoding="utf-8")
        # One call may span multiple lines (e.g. a multi-line write_text(json.dumps(...))), so
        # match from the method name up to its balanced closing paren rather than to end-of-line.
        offenders = []
        for method in ("write_text", "read_text"):
            for match in re.finditer(rf"\.{method}\(", source):
                start = match.end()
                depth = 1
                end = start
                while depth and end < len(source):
                    if source[end] == "(":
                        depth += 1
                    elif source[end] == ")":
                        depth -= 1
                    end += 1
                call_args = source[start:end]
                if "encoding=" not in call_args:
                    line_no = source.count("\n", 0, match.start()) + 1
                    site = f"{method} at line {line_no}"
                    if site not in self.ALLOWED_WITHOUT_ENCODING:
                        offenders.append(site)
        self.assertEqual(offenders, [],
                          "every Path.write_text/read_text call must pass encoding=\"utf-8\" "
                          "explicitly, or it silently uses the locale encoding (cp1252 on "
                          "Windows) and can crash on any non-ASCII character we write: " +
                          ", ".join(offenders))

    def test_write_skill_bundle_survives_an_ascii_locale(self):
        # Force the *locale* encoding away from UTF-8 the way it actually happens on a real
        # Windows box: Path.write_text()/read_text() with no encoding= fall back to
        # locale.getpreferredencoding(False). On Linux that is driven by LC_ALL/LANG, and
        # PYTHONUTF8=0 / PYTHONCOERCECLOCALE=0 stop Python's own UTF-8 mode and locale coercion
        # from silently upgrading it back to UTF-8.
        env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
        with tempfile.TemporaryDirectory() as tmpdir:
            code = (
                "import sys; sys.path.insert(0, " + repr(str(SCIENTIST_CLI_SOURCE.parent.parent)) + ")\n"
                "from pathlib import Path\n"
                "from lockedin.scientist_cli import write_skill_bundle\n"
                "write_skill_bundle(Path(" + repr(tmpdir) + "), '## Editing Guide\\n\\nbody', {})\n"
            )
            result = subprocess.run([sys.executable, "-c", code], env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0,
                             f"write_skill_bundle crashed under an ASCII locale "
                             f"(this is the Windows cp1252 crash, reproduced with ASCII):\n"
                             f"{result.stderr}")
            guides_dir = Path(tmpdir) / "guides"
            for name in GUIDES:
                body = (guides_dir / name).read_text(encoding="utf-8")
                self.assertTrue(body, f"guides/{name} should not be empty")
            skill = (Path(tmpdir) / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn(f"lockedin-scientist-skill: {SKILL_VERSION}", skill)


if __name__ == "__main__":
    unittest.main()
