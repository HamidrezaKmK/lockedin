"""Regression coverage for per-account theme availability."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lockedin import server, service


class AestheticsConfigTests(unittest.TestCase):
    def test_landing_endpoint_reads_current_yaml_instead_of_startup_snapshot(self):
        app = server.build_app()
        endpoint = next(route.endpoint for route in app.routes if route.path == "/api/landing")
        with patch("lockedin.server.landing.load_landing", return_value={"hero": {"kicker": "Fresh"}}):
            response = endpoint()
        self.assertIn(b"Fresh", response.body)

    def test_landing_always_offers_the_repository_clone_call_to_action(self):
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn('class="open-source-panel"', source)
        self.assertIn("git clone https://github.com/HamidrezaKmK/lockedin.git", source)
        self.assertIn('class="clone-link"', source)

    def test_enabled_themes_persist_and_require_one_choice(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            self.assertEqual(service.load_aesthetics_config(home)["themes"], list(service.THEMES))
            self.assertEqual(service.save_aesthetics_config(home, ["dark", "pink"])["themes"],
                             ["dark", "pink"])
            self.assertEqual(service.load_aesthetics_config(home)["themes"], ["dark", "pink"])
            with self.assertRaises(ValueError):
                service.save_aesthetics_config(home, [])

    def test_shared_preview_offers_only_dark_and_light(self):
        html = server._render_preview_html(
            name="Test", page="overview", all_pages=[{"page_slug": "overview", "title": "Overview"}],
            content="# Test", slug="test", link_base="/share/token", asset_base="/share/token/assets",
            show_back=False, themes=["dark", "pink"])
        self.assertIn('const THEMES=["dark", "light"];', html)
        self.assertNotIn('const THEMES=["dark", "pink"];', html)

    def test_private_preview_keeps_its_workspace_on_page_links(self):
        html = server._render_preview_html(
            name="Test", page="overview", all_pages=[
                {"page_slug": "overview", "title": "Overview"},
                {"page_slug": "methods", "title": "Methods"},
            ], content="See [[methods]]", slug="test", link_base="/api/bubbles/test/preview",
            asset_base="/api/bubbles/test/assets", show_back=True, workspace_id="research")
        self.assertIn('/api/bubbles/test/preview/methods?workspace=research', html)

    def test_preview_restarts_gif_figures_and_supports_centered_text(self):
        html = server._render_preview_html(
            name="Test", page="overview", all_pages=[{"page_slug": "overview", "title": "Overview"}],
            content='![Animation](/api/bubbles/test/assets/demo.gif)\n\n<div class="centered-text">Centered</div>',
            slug="test", link_base="/x", asset_base="/x/assets", show_back=False)
        self.assertIn("lockedin_gif", html)
        self.assertIn(".centered-text{text-align:center}", html)
        self.assertIn('p > img:only-child[alt]', html)
        self.assertIn("figcaption", html)
        self.assertIn("katex.renderToString(match[1]", html)
        self.assertIn("captionStore", html)
        self.assertIn("@@LI_CAP", html)

    def test_preview_resolves_portable_relative_report_figure_links(self):
        html = server._render_preview_html(
            name="Test", page="overview", all_pages=[{"page_slug": "overview", "title": "Overview"}],
            content='![A figure](assets/figure.png)', slug="test", link_base="/share/token",
            asset_base="/share/token/assets", show_back=False)
        self.assertIn("/share/token/assets/figure.png", html)

    def test_live_editor_preview_preserves_all_resources_between_typing_updates(self):
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn('if(S.bubble){\n      s=s.replace(/(!\\[[^\\]\\n]*\\]\\()assets\\/', source)
        self.assertNotIn('if(S.workspaceId){\n      s=s.replace(/\\/api\\/bubbles', source)
        self.assertIn("const savedResources=restartGifs?null:collectPreviewResources(S.previewEl);", source)
        self.assertIn("else restorePreviewResources(S.previewEl,savedResources);", source)
        self.assertIn('el.closest("picture,video,audio,iframe,object,embed")', source)
        self.assertIn('root.querySelectorAll("[src],[data],[srcset],picture")', source)
        self.assertIn("requestAnimationFrame(restoreScroll)", source)
        self.assertIn("setTimeout(()=>updatePreview({restartGifs:true}),200);", source)
        self.assertIn("setTimeout(()=>updatePreview({restartGifs:true}),150);", source)

    def test_authenticated_bubble_assets_are_never_cdn_cached(self):
        source = (Path(server.__file__).read_text())
        self.assertIn('"Cache-Control": "private, no-store"', source)

    def test_preview_reverse_sync_uses_source_offsets(self):
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn("function withSourceMarkers(md)", source)
        self.assertIn("function bindPreviewSourceOffsets(root)", source)
        self.assertIn('closest("[data-source-start]")', source)
        self.assertNotIn("function _findWordOffset", source)

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

    def test_browser_restores_a_valid_workspace_and_recovers_from_stale_storage(self):
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn('const WORKSPACE_STORAGE_KEY="li_workspace"', source)
        self.assertIn('const savedWorkspace=localStorage.getItem(WORKSPACE_STORAGE_KEY);', source)
        self.assertIn('localStorage.setItem(WORKSPACE_STORAGE_KEY,id);', source)
        self.assertIn('localStorage.removeItem(WORKSPACE_STORAGE_KEY);', source)
        self.assertIn('if(!savedWorkspace || (e.status!==403&&e.status!==404))return;', source)

    def test_library_defaults_to_the_requires_attention_filter(self):
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn('const REQUIRES_ATTENTION="__requires_attention__"', source)
        self.assertIn('`Requires attention (${attentionCount})`', source)
        self.assertIn('if(S.assetBubble==null) S.assetBubble=REQUIRES_ATTENTION', source)
        self.assertIn('const UNASSIGNED_BUBBLE="__unassigned__"', source)
        self.assertIn('`Unassigned (${unassignedCount})`', source)
        self.assertIn('"Every paper is assigned to at least one bubble."', source)

    def test_library_upload_can_attach_a_paper_to_multiple_bubbles_with_relevance(self):
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn('"Add to bubbles (optional)"', source)
        self.assertIn('buildAssetBubbleMembershipPanel({', source)
        self.assertIn('"Add to bubble…"', source)
        self.assertIn('"Promote relevance"', source)
        self.assertIn('"/add-pdf"', source)
        self.assertIn('"/papers/"+encodeURIComponent(d.pdf_id)', source)

    def test_bubble_assets_modal_previews_images_and_gifs(self):
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn('const previewable=/\\.(?:apng|avif|gif|jpe?g|png|svg|webp)$/i.test(item.name);', source)
        self.assertIn('class:"asset-file-preview"', source)
        self.assertIn('alt:"Preview: "+item.name', source)
        self.assertIn('.asset-file-preview img{width:100%;height:100%;object-fit:contain', source)

    def test_review_highlights_use_inline_markdown_markers(self):
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn('function addInlineCommentMarker(md,anchor,id)', source)
        self.assertIn("CodeMirror's document selection is the sole source of truth", source)
        self.assertIn("if(main&&main.from!==main.to)sel={from:Math.min(main.from,main.to)", source)
        self.assertIn('marker="\\\\comment{"+id+"}{"', source)
        self.assertIn('lockedin-review-markers', source)
        self.assertIn('s=stripCommentMarkers(s);', source)
        self.assertIn('if(S.comments&&S.comments.length)await loadComments();', source)

    def test_server_preview_strips_review_markers_before_math_rendering(self):
        html = server._render_preview_html(
            name="Test", page="overview",
            all_pages=[{"page_slug": "overview", "title": "Overview"}],
            content="See \\comment{thread-1}{$x^2$}.",
            slug="test", link_base="/api/bubbles/test/preview",
            asset_base="/api/bubbles/test/assets", show_back=False,
        )
        self.assertNotIn("\\comment{", html)
        self.assertIn("x^2", html)

    def test_library_card_attention_action_uses_compact_labels(self):
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn('a.attention_flag?"Clear":"Review"', source)
        self.assertIn('title:a.attention_flag?"Clear attention flag":"Mark as requiring attention"', source)

    def test_migrate_picker_rows_survive_the_global_full_width_input_rule(self):
        """The base ``input,textarea,select`` rule sets width:100%.

        A bare checkbox therefore stretches across its whole row and draws its tick centred in
        that box, which put every row's checkbox at a different x. The picker must size its
        checkbox explicitly and lay the row out as a grid so the columns line up.
        """
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn("width:100%", source.split("input,textarea,select{")[1][:200])
        self.assertIn(".migrate-row input[type=checkbox]{width:16px", source)
        self.assertIn(".migrate-row{display:grid;grid-template-columns:auto minmax(0,1fr) auto auto auto", source)
        # The title cell must be the only flexible column, and must be able to shrink to ellipsis.
        self.assertIn(".migrate-title{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}", source)
        # A <label> row would forward a click on the relevance select to the checkbox.
        self.assertIn('el("div",{class:"migrate-row"', source)

    def test_bubbles_view_reaches_the_migrate_picker(self):
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn('onclick:()=>viewMigratePapers()},"Migrate resources")', source)
        self.assertIn('if(h==="migrate"){viewMigratePapers({noRoute:true});return;}', source)
        self.assertIn('"/api/bubbles/"+dest+"/migrate-papers"', source)

    def test_migrate_rows_link_each_paper_to_its_asset_page_in_a_new_tab(self):
        """A vague title must be checkable without losing the selections already made."""
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn('el("a",{class:"migrate-open",href:workspaceAssetRoute(a.pdf_id),target:"_blank"', source)
        self.assertIn('rel:"noopener"', source)
        # workspaceAssetRoute keeps the active workspace in the hash, so the new tab opens the
        # asset in the same workspace rather than falling back to Personal.
        self.assertIn('"#w/"+encodeURIComponent(S.workspaceId)+"/asset/"', source)

    def test_bubbles_view_has_a_reversible_archive_filter(self):
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn('bubbleFilter:"active"', source)
        self.assertIn('filter==="active"?"Show archive":"Show active"', source)
        self.assertIn('api("/api/bubbles?archived="+(filter==="archived"))', source)
        self.assertIn('b.archived?"Restore":"Archive"', source)
