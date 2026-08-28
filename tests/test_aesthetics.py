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
        # `private` keeps shared caches (the tunnel included) out; `no-cache` makes the browser
        # revalidate — an authenticated request — before every use. Deliberately NOT `no-store`:
        # that made figures un-cacheable outright, so every page re-render re-downloaded all of
        # them and the reading view visibly flashed.
        source = (Path(server.__file__).read_text())
        self.assertIn('"Cache-Control": "private, no-cache"', source)
        self.assertNotIn('"Cache-Control": "private, no-store"', source)

    def test_open_readers_watch_the_assets_signal_and_retry_with_fresh_img_nodes(self):
        # restartGifs:true is what forces fresh <img> nodes — the resource-reuse path would put
        # the same broken element straight back.
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn('if(S.lastAssetsMtime!==null&&r.assets_mtime!==S.lastAssetsMtime) '
                      'updatePreview({restartGifs:true});', source)
        self.assertIn('S.lastAssetsMtime=r.assets_mtime;', source)
        self.assertIn('S.lastAssetsMtime=null;', source)

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

    def test_the_bubble_toolbar_is_one_control_surface_on_every_screen(self):
        """Papers and Edit titles live in the \u22ee menu, which phones get too.

        A phone previously could not create a page or change view mode at all (both controls were
        display:none) and reached Papers/Overleaf only through floating pill buttons. Those two
        surfaces are gone; if either class comes back, so has the split.

        View modes are gone as well: \u25e7 and \u25e8 in the tab row toggle the two panes those
        three names were combinations of.
        """
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        for retired in (".mobile-workspace-control", ".desktop-only-tool",
                        ".mobile-bubble-actions", ".mobile-sheet", "buildMobileBubbleTools",
                        "papers-menu", "data-papers-toggle"):
            self.assertNotIn(retired, source, f"{retired} should have been removed")
        # The row: page creation at the far left, then the tabs, then the pane toggles.
        self.assertIn('class:"ptab-new"', source)
        for gone in ("function setViewMode", "function loadViewMode", "function toggleReview",
                     "function reviewThreadNode", "function startComment("):
            self.assertNotIn(gone, source, f"{gone} is dead and should have been removed")
        self.assertIn('id:"paneLeftToggle"', source)
        self.assertIn('id:"paneRightToggle"', source)
        # Every control in the tab row shares one accent fill, defined once so they cannot drift.
        self.assertIn(".ptab-new,.tabrow-group{", source)
        # The dropdown is a descendant of the group, so the group must never clip its overflow.
        group = source[source.index(".tabrow-group{"):source.index(".tabrow-group{") + 120]
        self.assertNotIn("overflow:hidden", group)
        self.assertNotIn("overflow:clip", group)
        self.assertIn("background:var(--accent);color:var(--tabrow-ink)}", source)
        # Dark's row keeps the page tokens, where the accent is a mid purple rather than the pale
        # tint the other themes use — white glyphs read better on it than near-black ones.
        self.assertIn("body.theme-dark .editor-pane>.ptabs{--tabrow-ink:#fff}", source)
        # The ⋮ is the presence pill's fourth segment now, not a tab-row button.
        self.assertIn('el("div",{class:"hdr-cluster"},S.presenceEl,S.toolsMenu)', source)
        self.assertIn('id:"bubbleFocusToggle"', source)
        # The menu is built once per bubble and re-homed by refreshTabs, which rebuilds that row
        # on every page switch and every poll — rebuilding it there would drop an open panel.
        self.assertIn("S.toolsMenu=buildBubbleToolsMenu(slug,b);", source)
        # Papers is a popup on the assets modal's frame, not an anchored dropdown.
        self.assertIn("function openBubblePapers(slug)", source)
        self.assertIn('class:"diffbody asset-modal-body papers-modal-body col"', source)

    def test_browser_restores_a_valid_workspace_and_recovers_from_stale_storage(self):
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn('const WORKSPACE_STORAGE_KEY="li_workspace"', source)
        self.assertIn('const savedWorkspace=localStorage.getItem(WORKSPACE_STORAGE_KEY);', source)
        self.assertIn('localStorage.setItem(WORKSPACE_STORAGE_KEY,id);', source)
        self.assertIn('localStorage.removeItem(WORKSPACE_STORAGE_KEY);', source)
        self.assertIn('if(!S.workspaceId || (e.status!==403&&e.status!==404))return;', source)

    def test_every_route_carries_its_workspace_so_two_tabs_can_differ(self):
        """localStorage is shared by every tab, so it cannot be what a refresh reads."""
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        # The prefix is built once and applied to every navigation.
        self.assertIn('function routePrefix(){ return S.workspaceId?"w/"'
                      '+encodeURIComponent(S.workspaceId)+"/":""; }', source)
        self.assertIn('function pushRoute(hash){ history.pushState(null,"",routeHref(hash)); '
                      '_appliedHash=location.hash; }', source)
        # A route that arrives without a workspace is rewritten in place, on every entry point:
        # a reload, back/forward, and an in-page hash assignment (which fires only hashchange).
        self.assertIn('else if(S.workspaceId) history.replaceState(null,"",routeHref(h));', source)
        self.assertIn('window.addEventListener("hashchange",routeChanged);', source)
        # The URL is parsed back out ahead of every other route, and before /api/me is asked.
        self.assertIn('const wm=h.match(/^w\\/([^/]*)(?:\\/(.*))?$/);', source)
        self.assertIn('const routed=(location.hash.match(/^#w\\/([^/]*)/)||[])[1];', source)
        self.assertIn('S.workspaceId=(routed?decodeURIComponent(routed):savedWorkspace)||null;', source)

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
        self.assertIn('/\\.(?:apng|avif|gif|jpe?g|png|svg|webp)$/i.test(item.name)', source)
        self.assertIn('class:"asset-file-preview"', source)
        self.assertIn('alt:"Preview: "+item.name', source)
        self.assertIn('.asset-file-preview img{width:100%;height:100%;object-fit:contain', source)
        # A nested figure has no servable URL, so it must not attempt a (broken) thumbnail.
        self.assertIn('const previewable=item.servable!==false&&', source)

    def test_every_rendered_surface_loads_the_one_figure_viewer(self):
        # The SPA and the server-rendered preview/share page are separate codebases; browser logic
        # duplicated across them has drifted in this repo before, so both must load the same file.
        viewer = (Path(server.WEB_DIR) / "lightbox.js").read_text()
        self.assertIn("window.LockedInLightbox", viewer)
        for name in ("watch:", "open:", "close:"):
            self.assertIn(name, viewer)

        # A caption is the figure's Markdown alt text, so the viewer needs each surface's macros
        # to render the math it carries instead of showing raw $…$.
        self.assertIn("katex.renderToString", viewer)

        spa = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn('<script src="/lightbox.js"></script>', spa)
        # #previewWrap is the rendered pane in both Split and Read modes.
        self.assertIn('window.LockedInLightbox.watch("#previewWrap",{macros:()=>S.mathMacros})', spa)

        html = server._render_preview_html(
            name="Figures", page="overview", all_pages=[], slug="figures",
            content="![shot](assets/a.png)",
            link_base="/share/tok", asset_base="/share/tok/assets", show_back=False)
        self.assertIn('<script src="/lightbox.js"></script>', html)
        self.assertIn('window.LockedInLightbox.watch("#content",{macros:_macros})', html)

    def test_review_comments_use_server_owned_markers_and_dom_body_ranges(self):
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn("function parseCommentWrappers(value)", source)
        self.assertIn("function snapshotReviewSelection()", source)
        self.assertIn("function editorVisibleTextIndex(root,source)", source)
        self.assertIn("const start=source.indexOf(value,cursor)", source)
        self.assertIn("function serverSelectionOffsets(source,start,end)", source)
        self.assertIn("selection_start:offsets.start,selection_end:offsets.end", source)
        self.assertIn("function applyAuthoritativeReviewResponse(result,state,", source)
        self.assertIn("epoch===(S.reviewMutationEpoch||0)", source)
        self.assertIn("That selection cuts through a comment tag", source)
        # The preview renders wrappers as highlights rather than stripping them; only a surface
        # with no marks column (share pages, previews) strips.
        self.assertIn("s=opts.markComments?markCommentWrappers(s):stripCommentMarkers(s);", source)
        self.assertIn('Cannot save: "+error.message', source)
        self.assertNotIn("function addInlineCommentMarker", source)
        self.assertNotIn("await doSave({}); await loadComments()", source)
        self.assertNotIn("caretPositionFromPoint", source)

    def test_server_preview_strips_review_markers_before_math_rendering(self):
        html = server._render_preview_html(
            name="Test", page="overview",
            all_pages=[{"page_slug": "overview", "title": "Overview"}],
            content="See <comment-begin=thread-1>$x^2$<comment-end=thread-1>.",
            slug="test", link_base="/api/bubbles/test/preview",
            asset_base="/api/bubbles/test/assets", show_back=False,
        )
        self.assertNotIn("comment-begin", html)
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
        self.assertIn('function workspaceAssetRoute(pdfId){ return routeHref('
                      '"asset/"+encodeURIComponent(pdfId)); }', source)

    def test_bubbles_view_has_a_reversible_archive_filter(self):
        source = (Path(server.WEB_DIR) / "index.html").read_text()
        self.assertIn('bubbleFilter:"active"', source)
        self.assertIn('filter==="active"?"Show archive":"Show active"', source)
        self.assertIn('api("/api/bubbles?archived="+(filter==="archived"))', source)
        self.assertIn('b.archived?"Restore":"Archive"', source)
