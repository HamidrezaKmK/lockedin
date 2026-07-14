"""Regression coverage for per-account theme availability."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lockedin import server, service


class AestheticsConfigTests(unittest.TestCase):
    def test_enabled_themes_persist_and_require_one_choice(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            self.assertEqual(service.load_aesthetics_config(home)["themes"], list(service.THEMES))
            self.assertEqual(service.save_aesthetics_config(home, ["dark", "pink"])["themes"],
                             ["dark", "pink"])
            self.assertEqual(service.load_aesthetics_config(home)["themes"], ["dark", "pink"])
            with self.assertRaises(ValueError):
                service.save_aesthetics_config(home, [])

    def test_shared_preview_receives_only_enabled_themes(self):
        html = server._render_preview_html(
            name="Test", page="overview", all_pages=[{"page_slug": "overview", "title": "Overview"}],
            content="# Test", slug="test", link_base="/share/token", asset_base="/share/token/assets",
            show_back=False, themes=["dark", "pink"])
        self.assertIn('const THEMES=["dark", "pink"];', html)
        self.assertNotIn('const THEMES=["dark", "light", "pink", "techno", "pearl"];', html)

    def test_preview_restarts_gif_figures_and_supports_centered_text(self):
        html = server._render_preview_html(
            name="Test", page="overview", all_pages=[{"page_slug": "overview", "title": "Overview"}],
            content='![Animation](/api/bubbles/test/assets/demo.gif)\n\n<div class="centered-text">Centered</div>',
            slug="test", link_base="/x", asset_base="/x/assets", show_back=False)
        self.assertIn("lockedin_gif", html)
        self.assertIn(".centered-text{text-align:center}", html)

    def test_authenticated_bubble_assets_are_never_cdn_cached(self):
        source = (Path(server.__file__).read_text())
        self.assertIn('"Cache-Control": "private, no-store"', source)

    def test_mobile_focus_is_a_read_only_preview_without_desktop_layout_changes(self):
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn("#app.bubble-focus .ptabs,#app.bubble-focus #editorWrap{display:none!important}", source)
        self.assertIn("#app.bubble-focus #previewWrap{display:block!important;position:absolute;inset:0", source)
        self.assertIn('id:"mobileFocusExit"', source)

    def test_mobile_hides_workspace_controls_but_not_desktop_controls(self):
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn(".mobile-workspace-control{display:none!important}", source)
        self.assertIn('class:"small ghost mobile-workspace-control",onclick:newPage', source)
        self.assertIn('class:"small ghost mobile-workspace-control",title:"Page view mode"', source)
