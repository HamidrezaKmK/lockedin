"""Local web server — the single multi-user UI.

All real work goes through :mod:`lockedin.service`; this module is HTTP + auth glue. Each
request operates on the logged-in user's workspace (``data/users/<user>/``).

Streaming endpoints (chat / generate / edit) run the model in a dedicated worker thread and
hand events back over a queue — the same pattern ocd uses. This keeps the per-user path
context (a contextvar) consistent for the whole generator, including the final save, which
would otherwise be at risk if Starlette resumed the generator on a different threadpool worker.

Launch with ``lockedin serve``.
"""
import json
import logging
import os
import queue
import re
import threading
from pathlib import Path
from typing import Optional

from . import auth, bubbles, news, paths, service, tagger


def _preprocess_equations(md: str) -> str:
    """Number \\label{} equations in display math and resolve \\eqref{}/\\ref{} references.

    Scans all $$...$$ blocks for \\label{name}, assigns sequential numbers, replaces
    \\label{name} with \\tag{n} (so KaTeX renders the number), and replaces \\eqref{name}
    / \\ref{name} in surrounding text with the corresponding number as a styled span.
    Pure rendering transform — the source .md files are never modified.
    """
    eq_nums: dict[str, int] = {}
    idx = 0
    for m in re.finditer(r'\$\$([\s\S]+?)\$\$', md):
        lm = re.search(r'\\label\{([^}]+)\}', m.group(1))
        if lm and lm.group(1) not in eq_nums:
            idx += 1
            eq_nums[lm.group(1)] = idx
    if not eq_nums:
        return md

    def _tag(m: re.Match) -> str:
        inner = m.group(1)
        lm = re.search(r'\\label\{([^}]+)\}', inner)
        if lm and lm.group(1) in eq_nums:
            return "$$" + inner.replace(lm.group(0), f'\\tag{{{eq_nums[lm.group(1)]}}}', 1) + "$$"
        return m.group(0)

    md = re.sub(r'\$\$([\s\S]+?)\$\$', _tag, md)
    md = re.sub(r'\\eqref\{([^}]+)\}',
                lambda m: f'<span class="eq-ref">({eq_nums.get(m.group(1), "?")})</span>', md)
    md = re.sub(r'\\ref\{([^}]+)\}',
                lambda m: f'<span class="eq-ref">{eq_nums.get(m.group(1), "?")}</span>', md)
    return md


def _render_preview_html(*, name: str, page: str, all_pages: list, content: str, slug: str,
                         link_base: str, asset_base: str, show_back: bool) -> str:
    """Build the standalone rendered-page HTML shared by the owner preview and public share pages.

    ``link_base``  — prefix for intra-bubble nav + wikilinks (e.g. ``/api/bubbles/<slug>/preview``
                     or ``/share/<token>``).
    ``asset_base`` — prefix images resolve to (``/share/<token>/assets`` rewrites the stored
                     ``/api/bubbles/<slug>/assets`` URLs so figures load without a login).
    """
    nav_links = " &nbsp;|&nbsp; ".join(
        f'<a href="{link_base}/{p["page_slug"]}">{p["title"]}</a>' for p in all_pages)

    def resolve_wikilink(m):
        target = m.group(1).strip()
        match = next((p for p in all_pages
                      if p["page_slug"] == target or p["title"].lower() == target.lower()), None)
        return f'[{match["title"]}]({link_base}/{match["page_slug"]})' if match else m.group(0)

    md = _preprocess_equations(re.sub(r'\[\[([^\]]+)\]\]', resolve_wikilink, content))
    # point figure URLs at the right (possibly public) asset route
    md = md.replace(f"/api/bubbles/{slug}/assets/", f"{asset_base}/")

    # Return to the editor, not a browser history step: the preview opens in its own tab and
    # navigating between pages inside it builds history, so history.back() would just walk those
    # preview pages. Close the tab (refocusing the editor tab); if the tab can't self-close
    # (e.g. opened/refreshed directly, not via script), fall back to the editor's SPA route.
    back_js = (f"window.close();"
               f"setTimeout(function(){{location.href='/#bubble/{slug}'}},120)")
    back_btn = (f'<button id="back-btn" onclick="{back_js}">← Back to editor</button>'
                if show_back else "")
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} — {page}</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🔒</text></svg>">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@11/marked.min.js"></script>
<style>
:root{{font:16px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
      --bg:#0f1115;--ink:#e6e9ef;--muted:#9aa3b2;--line:#2a2f3a;--panel:#1e222b;--accent:#7c9cff}}
body.light{{--bg:#f4f6fb;--ink:#1a1d24;--muted:#6b7280;--line:#d0d7e3;--panel:#eef1f7;--accent:#3b5bdb}}
body{{background:var(--bg);color:var(--ink);max-width:860px;margin:0 auto;padding:24px 32px}}
nav{{font-size:13px;margin-bottom:24px;padding-bottom:12px;border-bottom:1px solid var(--line);color:var(--muted)}}
nav a{{color:var(--accent);text-decoration:none}} nav a:hover{{text-decoration:underline}}
h1,h2,h3,h4{{position:relative}}
h1:hover .anchor,h2:hover .anchor,h3:hover .anchor,h4:hover .anchor{{opacity:.55}}
.anchor{{position:absolute;left:-1.1em;opacity:0;text-decoration:none;color:var(--accent);
         cursor:pointer;font-size:.8em;padding-right:.3em}}
.anchor:hover{{opacity:1!important}}
h1{{font-size:28px}} h2{{font-size:21px;border-bottom:1px solid var(--line);padding-bottom:4px}}
code{{background:var(--panel);padding:2px 6px;border-radius:4px}}
pre{{background:var(--panel);padding:14px;border-radius:8px;overflow:auto}}
a{{color:var(--accent)}} img{{max-width:100%;border-radius:8px}}
table{{display:block;width:max-content;max-width:100%;overflow-x:auto;border-collapse:collapse;margin:12px 0}}
th,td{{border:1px solid var(--line);padding:6px 10px;text-align:left}}
thead th{{background:var(--panel)}}
blockquote{{margin:12px 0;padding:6px 14px;border-left:3px solid var(--accent);color:var(--muted);
            background:var(--panel);border-radius:0 8px 8px 0}}
blockquote p{{margin:6px 0}}
.katex-display{{overflow-x:auto}}
.eq-ref{{color:var(--accent);font-weight:500}}
#copied{{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--panel);
         border:1px solid var(--line);padding:8px 14px;border-radius:8px;font-size:13px;opacity:0;
         transition:opacity .2s;pointer-events:none}}
#copied.show{{opacity:1}}
#theme-toggle{{position:fixed;top:14px;right:16px;background:var(--panel);border:1px solid var(--line);
               color:var(--ink);border-radius:8px;padding:5px 10px;cursor:pointer;font-size:13px;z-index:10}}
#back-btn{{position:fixed;top:14px;left:16px;background:var(--panel);border:1px solid var(--line);
           color:var(--ink);border-radius:8px;padding:5px 10px;cursor:pointer;font-size:13px;z-index:10}}
</style></head><body>
{back_btn}
<button id="theme-toggle" onclick="toggleTheme()">☀️ Light</button>
<nav><b>{name}</b> &nbsp;|&nbsp; {nav_links}</nav>
<div id="content"></div>
<div id="copied">🔗 Link copied</div>
<script>
(function(){{
  const t=localStorage.getItem("preview_theme");
  if(t==="light"){{document.body.classList.add("light");document.getElementById("theme-toggle").textContent="🌙 Dark";}}
}})();
function toggleTheme(){{
  const light=document.body.classList.toggle("light");
  document.getElementById("theme-toggle").textContent=light?"🌙 Dark":"☀️ Light";
  localStorage.setItem("preview_theme",light?"light":"dark");
}}
// Stash every math span before marked.js runs so it can't mangle the LaTeX
// (e.g. underscores → <em>), then render KaTeX from the untouched source.
// Mirrors the editor side pane's renderMarkdown() so preview/share match it exactly.
(function(){{
  const store=[]; let s={repr(md)};
  const stash=(re,display)=>{{ s=s.replace(re,(m,p1)=>{{ store.push({{src:p1,display}}); return "@@M"+(store.length-1)+"@@"; }}); }};
  stash(/\\$\\$([\\s\\S]+?)\\$\\$/g,true);
  stash(/\\\\\\[([\\s\\S]+?)\\\\\\]/g,true);
  stash(/\\\\\\(([\\s\\S]+?)\\\\\\)/g,false);
  stash(/\\$([^\\$\\n]+?)\\$/g,false);
  let html=marked.parse(s);
  html=html.replace(/@@M(\\d+)@@/g,(m,i)=>{{ const it=store[+i];
    try{{ return katex.renderToString(it.src,{{displayMode:it.display,throwOnError:false}}); }}
    catch(e){{ return '<span style="color:#ff7a7a">'+it.src+'</span>'; }} }});
  document.getElementById("content").innerHTML=html;
}})();
// Give every heading a stable id + a click-to-copy section anchor; deep-link via #id.
(function(){{
  const seen={{}};
  function slugify(s){{
    let base=(s||"").toLowerCase().trim().replace(/[^\\w\\s-]/g,"").replace(/\\s+/g,"-").replace(/-+/g,"-")||"section";
    let id=base, n=2; while(seen[id]) id=base+"-"+(n++); seen[id]=1; return id;
  }}
  function flash(){{ const c=document.getElementById("copied"); c.classList.add("show");
    clearTimeout(c._t); c._t=setTimeout(()=>c.classList.remove("show"),1600); }}
  document.querySelectorAll("#content h1,#content h2,#content h3,#content h4").forEach(h=>{{
    if(!h.id) h.id=slugify(h.textContent);
    const a=document.createElement("a"); a.className="anchor"; a.textContent="🔗";
    a.href="#"+h.id; a.title="Copy link to this section";
    a.onclick=e=>{{ e.preventDefault();
      const url=location.origin+location.pathname+"#"+h.id;
      history.replaceState(null,"","#"+h.id);
      (navigator.clipboard?navigator.clipboard.writeText(url):Promise.reject()).then(flash,()=>{{
        const t=document.createElement("textarea"); t.value=url; document.body.appendChild(t);
        t.select(); try{{document.execCommand("copy");flash();}}catch(_){{}} t.remove(); }});
    }};
    h.prepend(a);
  }});
  if(location.hash){{ const el=document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if(el) setTimeout(()=>el.scrollIntoView({{behavior:"smooth",block:"start"}}),60); }}
}})();
</script></body></html>"""


logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).with_name("web")
COOKIE = "lockedin_session"

PUBLIC_ORIGINS = [o.strip() for o in os.environ.get("LOCKEDIN_CORS_ORIGINS", "").split(",") if o.strip()]
CROSS_SITE = bool(PUBLIC_ORIGINS)

# Mark the session cookie Secure (HTTPS-only) by default — correct behind Cloudflare and on
# localhost/127.0.0.1 (treated as secure contexts by modern browsers). Set
# LOCKEDIN_INSECURE_COOKIE=1 only for the rare case of serving plain HTTP on a LAN IP.
SECURE_COOKIES = os.environ.get("LOCKEDIN_INSECURE_COOKIE", "") not in ("1", "true", "yes")


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def build_app():
    from fastapi import (BackgroundTasks, Cookie, Depends, FastAPI, File, Form,
                         HTTPException, UploadFile)
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
    from pydantic import BaseModel

    app = FastAPI(title="lockedin — research assistant")
    if CROSS_SITE:
        app.add_middleware(CORSMiddleware, allow_origins=PUBLIC_ORIGINS,
                           allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    else:
        app.add_middleware(CORSMiddleware, allow_origins=["*"],
                           allow_methods=["*"], allow_headers=["*"])

    # ---- request models ----
    class Credentials(BaseModel):
        username: str
        password: str

    class AccountIn(BaseModel):
        current_password: str
        new_username: Optional[str] = None
        new_password: Optional[str] = None

    class ApprovalIn(BaseModel):
        approved: bool

    class ShareIn(BaseModel):
        active: bool

    class AssetPatch(BaseModel):
        title: Optional[str] = None
        tags: Optional[list[str]] = None
        notes: Optional[str] = None
        url_source: Optional[str] = None
        attention_flag: Optional[bool] = None
        suggested_tags: Optional[list[str]] = None

    class AssetUrlIn(BaseModel):
        url: str
        title: str = ""
        tags: str = ""

    class BubbleIn(BaseModel):
        name: str

    class ApproveIn(BaseModel):
        instructions: str = ""

    class BubbleRenameIn(BaseModel):
        name: str

    class AddPdfIn(BaseModel):
        pdf_id: str

    class PageContentIn(BaseModel):
        content: str
        # Optimistic-concurrency token: the page mtime the editor's content was loaded
        # from. When present and stale, the save is rejected (409) instead of clobbering
        # an external edit. Omitted (None) on first save / forced overwrite.
        base_mtime: float | None = None

    class PageCreateIn(BaseModel):
        title: str

    class PageRenameIn(BaseModel):
        title: str

    class ChatIn(BaseModel):
        messages: list[dict]
        page: str
        page_context: str = ""
        deep_read_ids: list[str] = []

    class SaveSessionIn(BaseModel):
        session_id: str
        title: str
        messages: list[dict]

    class ModelConfigIn(BaseModel):
        config: dict

    class ActiveIn(BaseModel):
        active: str

    class NewsInstructionsIn(BaseModel):
        instructions: list[dict]

    class NewsChatIn(BaseModel):
        message: str = ""
        model: Optional[str] = None
        since: Optional[str] = None
        until: Optional[str] = None

    # ---- auth plumbing ----
    def current_user(lockedin_session: Optional[str] = Cookie(default=None)) -> str:
        user = auth.session_user(lockedin_session)
        if not user:
            raise HTTPException(status_code=401, detail="Please log in.")
        if not auth.is_approved(user):
            auth.end_session(lockedin_session)
            raise HTTPException(status_code=403, detail="Account is waiting for approval.")
        return user

    def home_of(user: str) -> Path:
        return paths.user_home(user)

    def require_news(user: str = Depends(current_user)) -> str:
        """Gate the premium news feature; viewing is allowed, crawling additionally needs the switch."""
        if not auth.is_news_enabled(user):
            raise HTTPException(status_code=403,
                                detail="News is a premium feature not enabled for this account.")
        return user

    def _auth_response(user: str):
        token = auth.new_session(user)
        resp = JSONResponse({"user": user})
        if CROSS_SITE:
            resp.set_cookie(COOKIE, token, httponly=True, samesite="none", secure=True,
                            max_age=7 * 24 * 3600)
        else:
            resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=SECURE_COOKIES,
                            max_age=7 * 24 * 3600)
        return resp

    def _stream(generator_factory):
        """Run a dict-yielding generator in a worker thread; forward events as SSE."""
        def gen():
            q: queue.Queue = queue.Queue()

            def worker():
                try:
                    for ev in generator_factory():
                        q.put(ev)
                except Exception as e:  # noqa: BLE001
                    q.put({"type": "error", "detail": str(e)})
                finally:
                    q.put(None)

            threading.Thread(target=worker, daemon=True).start()
            while True:
                ev = q.get()
                if ev is None:
                    break
                yield _sse(ev)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- static ----
    @app.get("/")
    def index():
        # no-cache: the browser must revalidate before reusing a cached copy, so SPA
        # updates land immediately (it can still 304 when unchanged). Without this,
        # FileResponse sets no Cache-Control and browsers may serve a stale SPA.
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/help")
    def get_help():
        from . import reports as _r
        return {"guide": _r.APP_USAGE_GUIDE}

    # ---- public share (NO auth — gated only by the unlisted token + the bubble's active flag) ----
    @app.get("/share/{token}")
    def share_root(token: str):
        from fastapi.responses import RedirectResponse
        tgt = service.share_target(token)
        if not tgt:
            raise HTTPException(status_code=404, detail="This share link is not active.")
        home, slug = tgt
        home_page = service.bubble_detail(home, slug)["home"] or "overview"
        return RedirectResponse(url=f"/share/{token}/{home_page}")

    @app.get("/share/{token}/{page}")
    def share_page(token: str, page: str):
        from fastapi.responses import HTMLResponse
        tgt = service.share_target(token)
        if not tgt:
            raise HTTPException(status_code=404, detail="This share link is not active.")
        home, slug = tgt
        all_pages = service.list_pages(home, slug)
        if not any(p["page_slug"] == page for p in all_pages):
            raise HTTPException(status_code=404, detail="No such page.")
        html = _render_preview_html(
            name=service.bubble_detail(home, slug)["name"], page=page,
            all_pages=all_pages, content=service.get_page(home, slug, page),
            slug=slug, link_base=f"/share/{token}",
            asset_base=f"/share/{token}/assets", show_back=False)
        return HTMLResponse(html)

    @app.get("/share/{token}/assets/{filename}")
    def share_asset(token: str, filename: str):
        tgt = service.share_target(token)
        if not tgt:
            raise HTTPException(status_code=404, detail="This share link is not active.")
        home, slug = tgt
        safe = Path(filename).name
        if safe != filename or not safe:
            raise HTTPException(status_code=400, detail="Bad filename.")
        p = service.bubble_asset_path(home, slug, safe)
        if not p.exists():
            raise HTTPException(status_code=404, detail="No such image.")
        return FileResponse(p, headers={"Content-Disposition": "inline"})

    # ---- auth ----
    @app.post("/api/signup")
    def signup(creds: Credentials):
        try:
            user = auth.create_user(creds.username, creds.password)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        service.ensure_workspace(home_of(user))
        if not auth.is_approved(user):
            return JSONResponse({"pending": True, "user": user,
                                 "message": "Account created. An admin must approve it before login."},
                                status_code=202)
        return _auth_response(user)

    @app.post("/api/login")
    def login(creds: Credentials):
        username = creds.username.strip().lower()
        if not auth.verify_password(username, creds.password):
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        if not auth.is_approved(username):
            raise HTTPException(status_code=403, detail="Account is waiting for admin approval.")
        return _auth_response(username)

    @app.post("/api/logout")
    def logout(lockedin_session: Optional[str] = Cookie(default=None)):
        auth.end_session(lockedin_session)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/api/me")
    def me(user: str = Depends(current_user)):
        return {"user": user, "model": service.get_model_config(home_of(user)),
                "news_enabled": auth.is_news_enabled(user), "admin": auth.is_admin(user)}

    @app.post("/api/account")
    def update_account(body: AccountIn, user: str = Depends(current_user)):
        """Change username and/or password. Requires the current password."""
        try:
            final = service.update_account(
                user, current_password=body.current_password,
                new_username=(body.new_username or "").strip(),
                new_password=body.new_password or "")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # Sessions were repointed to the new name in-memory, so the existing cookie still works.
        return {"user": final}

    @app.get("/api/admin/users")
    def admin_users(user: str = Depends(current_user)):
        if not auth.is_admin(user):
            raise HTTPException(status_code=403, detail="Admin access required.")
        return {"users": auth.list_users()}

    @app.put("/api/admin/users/{username}/approval")
    def admin_user_approval(username: str, body: ApprovalIn, user: str = Depends(current_user)):
        if not auth.is_admin(user):
            raise HTTPException(status_code=403, detail="Admin access required.")
        target = username.strip().lower()
        if target == user and not body.approved:
            raise HTTPException(status_code=400, detail="You cannot revoke your own access.")
        try:
            auth.set_approved(target, body.approved)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"users": auth.list_users()}

    @app.delete("/api/admin/users/{username}")
    def admin_delete_user(username: str, user: str = Depends(current_user)):
        if not auth.is_admin(user):
            raise HTTPException(status_code=403, detail="Admin access required.")
        target = username.strip().lower()
        if target == user:
            raise HTTPException(status_code=400, detail="You cannot delete your own account.")
        try:
            service.delete_account(target)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"users": auth.list_users()}

    @app.get("/api/health")
    def health(live: bool = False, user: str = Depends(current_user)):
        return service.model_health(home_of(user), live=live)

    # ---- model settings ----
    @app.get("/api/settings/model")
    def get_model(user: str = Depends(current_user)):
        return {"config": service.get_model_config(home_of(user))}

    @app.put("/api/settings/model")
    def put_model(body: ModelConfigIn, user: str = Depends(current_user)):
        return {"config": service.save_model_config(home_of(user), body.config)}

    @app.put("/api/settings/model/active")
    def put_active(body: ActiveIn, user: str = Depends(current_user)):
        try:
            return {"config": service.set_active_provider(home_of(user), body.active)}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ---- news (premium background crawler) ----
    @app.get("/api/news")
    def get_news(user: str = Depends(require_news)):
        return service.list_news(home_of(user))

    @app.get("/api/news/status")
    def news_status(user: str = Depends(require_news)):
        return service.news_status(home_of(user))

    @app.get("/api/news/models")
    def news_models(user: str = Depends(require_news)):
        return {"models": service.news_models()}

    @app.get("/api/news/instructions")
    def get_news_instructions(user: str = Depends(require_news)):
        return {"instructions": service.get_news_instructions(home_of(user))}

    @app.put("/api/news/instructions")
    def put_news_instructions(body: NewsInstructionsIn, user: str = Depends(require_news)):
        return {"instructions": service.save_news_instructions(home_of(user), body.instructions)}

    @app.get("/api/news/session")
    def news_session(user: str = Depends(require_news)):
        return {"session": service.news_session(home_of(user))}

    @app.get("/api/news/chats")
    def news_chats_list(user: str = Depends(require_news)):
        return {"chats": service.list_news_chats(home_of(user))}

    @app.get("/api/news/chats/{sid}")
    def news_chat_get(sid: str, user: str = Depends(require_news)):
        rec = service.get_news_chat(home_of(user), sid)
        if not rec:
            raise HTTPException(status_code=404, detail="No such crawl chat.")
        return {"chat": rec}

    @app.delete("/api/news/chats/{sid}")
    def news_chat_delete(sid: str, user: str = Depends(require_news)):
        service.delete_news_chat(home_of(user), sid)
        return {"ok": True}

    @app.post("/api/news/chat")
    def news_chat(body: NewsChatIn, user: str = Depends(require_news)):
        if not news.news_globally_enabled():
            raise HTTPException(status_code=503,
                                detail="News is off. Start the server with LOCKEDIN_NEWS_ENABLED=1.")
        home = home_of(user)
        return _stream(lambda: service.news_chat(home, body.message, body.model,
                                                 body.since, body.until))

    @app.post("/api/news/accept")
    def news_accept(user: str = Depends(require_news)):
        return service.accept_news(home_of(user))

    @app.post("/api/news/discard")
    def news_discard(user: str = Depends(require_news)):
        return service.discard_news(home_of(user))

    @app.post("/api/news/{item_id}/dismiss")
    def dismiss_news(item_id: str, user: str = Depends(require_news)):
        if not service.dismiss_news(home_of(user), item_id):
            raise HTTPException(status_code=404, detail="No such news item.")
        return {"ok": True}

    # ---- assets ----
    @app.get("/api/assets")
    def list_assets(user: str = Depends(current_user)):
        return {"assets": service.list_assets(home_of(user))}

    @app.post("/api/assets/upload")
    async def upload_asset(
        background_tasks: BackgroundTasks,
        user: str = Depends(current_user),
        file: UploadFile = File(...),
        title: str = Form(""),
        tags: str = Form(""),
        url_source: str = Form(""),
    ):
        if not file.filename:
            raise HTTPException(status_code=400, detail="No file provided.")
        pdf_bytes = await file.read()
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        home = home_of(user)
        pdf_id = service.save_asset(home, pdf_bytes, file.filename, title=title,
                                    tags=tag_list, url_source=url_source)
        # any user-supplied tag becomes an approved bubble immediately
        if tag_list:
            service.register_user_tags(home, tag_list)
        background_tasks.add_task(tagger.run_ingest, home, pdf_id, bool(tag_list))
        return {"pdf_id": pdf_id, "attention_flag": not bool(tag_list)}

    @app.post("/api/assets/upload-url")
    def upload_asset_url(
        body: AssetUrlIn,
        background_tasks: BackgroundTasks,
        user: str = Depends(current_user),
    ):
        url = body.url.strip()
        if not url:
            raise HTTPException(status_code=400, detail="No URL provided.")
        tag_list = [t.strip() for t in body.tags.split(",") if t.strip()]
        home = home_of(user)
        try:
            pdf_id = service.fetch_and_save_asset(home, url, title=body.title, tags=tag_list)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Couldn't fetch that link: {e}")
        # any user-supplied tag becomes an approved bubble immediately
        if tag_list:
            service.register_user_tags(home, tag_list)
        background_tasks.add_task(tagger.run_ingest, home, pdf_id, bool(tag_list))
        return {"pdf_id": pdf_id, "attention_flag": not bool(tag_list)}

    @app.get("/api/assets/{pdf_id}")
    def get_asset(pdf_id: str, user: str = Depends(current_user)):
        try:
            return {"meta": service.get_asset(home_of(user), pdf_id)}
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="No such asset.")

    @app.patch("/api/assets/{pdf_id}")
    def patch_asset(pdf_id: str, body: AssetPatch, user: str = Depends(current_user)):
        try:
            meta = service.update_asset(home_of(user), pdf_id,
                                        **body.model_dump(exclude_none=True))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="No such asset.")
        return {"meta": meta}

    @app.delete("/api/assets/{pdf_id}")
    def del_asset(pdf_id: str, user: str = Depends(current_user)):
        if not service.delete_asset(home_of(user), pdf_id):
            raise HTTPException(status_code=404, detail="No such asset.")
        return {"ok": True}

    @app.get("/api/assets/{pdf_id}/summary")
    def get_summary(pdf_id: str, user: str = Depends(current_user)):
        return {"summary": service.asset_summary(home_of(user), pdf_id)}

    @app.get("/api/assets/{pdf_id}/pdf")
    def get_pdf(pdf_id: str, user: str = Depends(current_user)):
        p = service.asset_pdf_path(home_of(user), pdf_id)
        if not p.exists():
            raise HTTPException(status_code=404, detail="No such PDF.")
        return FileResponse(p, media_type="application/pdf",
                            headers={"Content-Disposition": "inline"})

    @app.get("/api/attention")
    def attention(user: str = Depends(current_user)):
        return {"assets": service.attention_queue(home_of(user))}

    # ---- bubbles ----
    @app.get("/api/bubbles")
    def list_bubbles(user: str = Depends(current_user)):
        return {"bubbles": service.list_bubbles(home_of(user))}

    @app.post("/api/bubbles")
    def create_bubble(body: BubbleIn, user: str = Depends(current_user)):
        try:
            slug = service.create_bubble(home_of(user), body.name)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"slug": slug}

    @app.get("/api/bubbles/{slug}")
    def get_bubble(slug: str, user: str = Depends(current_user)):
        return {"bubble": service.bubble_detail(home_of(user), slug)}

    @app.patch("/api/bubbles/{slug}")
    def rename_bubble(slug: str, body: BubbleRenameIn, user: str = Depends(current_user)):
        try:
            return {"bubble": service.rename_bubble(home_of(user), slug, body.name)}
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/bubbles/{slug}/approve")
    def approve_bubble(slug: str, body: ApproveIn, user: str = Depends(current_user)):
        return {"bubble": service.approve_bubble(home_of(user), slug, body.instructions)}

    @app.post("/api/bubbles/{slug}/add-pdf")
    def add_pdf_to_bubble(slug: str, body: AddPdfIn, user: str = Depends(current_user)):
        try:
            return {"bubble": service.add_pdf_to_bubble(home_of(user), slug, body.pdf_id)}
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="No such asset.")

    @app.post("/api/bubbles/{slug}/remove-pdf")
    def remove_pdf_from_bubble(slug: str, body: AddPdfIn, user: str = Depends(current_user)):
        try:
            return {"bubble": service.remove_pdf_from_bubble(home_of(user), slug, body.pdf_id)}
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="No such asset.")

    @app.delete("/api/bubbles/{slug}")
    def delete_bubble(slug: str, user: str = Depends(current_user)):
        service.delete_bubble(home_of(user), slug)
        return {"ok": True}

    @app.post("/api/bubbles/{slug}/share")
    def set_share(slug: str, body: ShareIn, user: str = Depends(current_user)):
        """Toggle the bubble's unlisted public share link (stable token)."""
        return service.set_bubble_share(home_of(user), slug, body.active)

    # ---- pages (per-bubble mini-wiki) ----
    @app.get("/api/bubbles/{slug}/pages/{page}")
    def get_page(slug: str, page: str, user: str = Depends(current_user)):
        return {"content": service.get_page(home_of(user), slug, page)}

    @app.get("/api/bubbles/{slug}/preview/{page}")
    def preview_page(slug: str, page: str, user: str = Depends(current_user)):
        """Serve a standalone HTML page — full-screen rendered preview with working intra-bubble links."""
        from fastapi.responses import HTMLResponse

        home = home_of(user)
        html = _render_preview_html(
            name=service.bubble_detail(home, slug)["name"], page=page,
            all_pages=service.list_pages(home, slug), content=service.get_page(home, slug, page),
            slug=slug, link_base=f"/api/bubbles/{slug}/preview",
            asset_base=f"/api/bubbles/{slug}/assets", show_back=True)
        return HTMLResponse(html)

    @app.post("/api/bubbles/{slug}/pages")
    def create_page(slug: str, body: PageCreateIn, user: str = Depends(current_user)):
        if not body.title.strip():
            raise HTTPException(status_code=400, detail="Page title required.")
        return {"page_slug": service.create_page(home_of(user), slug, body.title)}

    @app.put("/api/bubbles/{slug}/pages/{page}")
    def put_page(slug: str, page: str, body: PageContentIn, user: str = Depends(current_user)):
        try:
            mtime = service.save_page(home_of(user), slug, page, body.content, body.base_mtime)
        except bubbles.PageConflict as e:
            # 409: the editor's base mtime is stale — an external edit landed first.
            raise HTTPException(status_code=409, detail="Page changed on disk",
                                headers={"X-Disk-Mtime": repr(e.disk_mtime)})
        return {"ok": True, "page_mtime": mtime}

    @app.get("/api/bubbles/{slug}/poll")
    def bubble_poll(slug: str, page: str, user: str = Depends(current_user)):
        return service.page_poll(home_of(user), slug, page)

    @app.patch("/api/bubbles/{slug}/pages/{page}")
    def patch_page(slug: str, page: str, body: PageRenameIn, user: str = Depends(current_user)):
        service.rename_page(home_of(user), slug, page, body.title)
        return {"ok": True}

    @app.delete("/api/bubbles/{slug}/pages/{page}")
    def del_page(slug: str, page: str, user: str = Depends(current_user)):
        try:
            ok = service.delete_page(home_of(user), slug, page)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not ok:
            raise HTTPException(status_code=404, detail="No such page.")
        return {"ok": True}

    # ---- figures ----
    @app.post("/api/bubbles/{slug}/assets")
    async def upload_bubble_image(slug: str, user: str = Depends(current_user),
                                  file: UploadFile = File(...)):
        if not file.filename:
            raise HTTPException(status_code=400, detail="No file provided.")
        url = service.save_bubble_image(home_of(user), slug, file.filename, await file.read())
        return {"url": url}

    @app.get("/api/bubbles/{slug}/assets/{filename}")
    def get_bubble_image(slug: str, filename: str, user: str = Depends(current_user)):
        safe = Path(filename).name
        if safe != filename or not safe:
            raise HTTPException(status_code=400, detail="Bad filename.")
        p = service.bubble_asset_path(home_of(user), slug, safe)
        if not p.exists():
            raise HTTPException(status_code=404, detail="No such image.")
        return FileResponse(p, headers={"Content-Disposition": "inline"})

    # ---- chat sessions ----
    @app.get("/api/bubbles/{slug}/chats")
    def list_chats(slug: str, user: str = Depends(current_user)):
        return {"sessions": service.list_chat_sessions(home_of(user), slug)}

    @app.get("/api/bubbles/{slug}/chats/{session_id}")
    def get_chat(slug: str, session_id: str, user: str = Depends(current_user)):
        s = service.get_chat_session(home_of(user), slug, session_id)
        if s is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        return {"session": s}

    @app.put("/api/bubbles/{slug}/chats/{session_id}")
    def save_chat(slug: str, session_id: str, body: SaveSessionIn,
                  user: str = Depends(current_user)):
        service.save_chat_session(home_of(user), slug, session_id, body.title, body.messages)
        return {"ok": True}

    @app.delete("/api/bubbles/{slug}/chats/{session_id}")
    def del_chat(slug: str, session_id: str, user: str = Depends(current_user)):
        service.delete_chat_session(home_of(user), slug, session_id)
        return {"ok": True}

    # ---- chat title (short, cute, model-generated) ----
    class ChatTitleIn(BaseModel):
        messages: list[dict]

    @app.post("/api/bubbles/{slug}/chats/title")
    def chat_title(slug: str, body: ChatTitleIn, user: str = Depends(current_user)):
        return {"title": service.generate_chat_title(home_of(user), body.messages)}

    # ---- streamed: read-only research chat ----
    @app.post("/api/bubbles/{slug}/chat")
    def bubble_chat(slug: str, body: ChatIn, user: str = Depends(current_user)):
        home = home_of(user)
        return _stream(lambda: service.chat(home, slug, body.page, body.messages,
                                            body.page_context, body.deep_read_ids))

    return app


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    logger.info("Starting lockedin server on http://%s:%d", host, port)
    uvicorn.run(build_app(), host=host, port=port)
