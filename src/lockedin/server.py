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

from . import auth, paths, service, tagger


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
                         Header, HTTPException, UploadFile)
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

    class AssetPatch(BaseModel):
        title: Optional[str] = None
        tags: Optional[list[str]] = None
        notes: Optional[str] = None
        url_source: Optional[str] = None
        attention_flag: Optional[bool] = None

    class BubbleIn(BaseModel):
        name: str

    class ApproveIn(BaseModel):
        instructions: str = ""

    class BubbleRenameIn(BaseModel):
        name: str

    class PageContentIn(BaseModel):
        content: str

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

    # ---- auth plumbing ----
    def current_user(lockedin_session: Optional[str] = Cookie(default=None)) -> str:
        user = auth.session_user(lockedin_session)
        if not user:
            raise HTTPException(status_code=401, detail="Please log in.")
        return user

    # Per-request Claude token (browser-held; never stored server-side). One dependency so the
    # AI endpoints don't each re-read the header.
    def claude_token(x_claude_token: str = Header(default="")) -> str:
        return x_claude_token

    def home_of(user: str) -> Path:
        return paths.user_home(user)

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
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/help")
    def get_help():
        from . import reports as _r
        return {"guide": _r.APP_USAGE_GUIDE}

    # ---- auth ----
    @app.post("/api/signup")
    def signup(creds: Credentials):
        try:
            user = auth.create_user(creds.username, creds.password)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        service.ensure_workspace(home_of(user))
        return _auth_response(user)

    @app.post("/api/login")
    def login(creds: Credentials):
        username = creds.username.strip().lower()
        if not auth.verify_password(username, creds.password):
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        return _auth_response(username)

    @app.post("/api/logout")
    def logout(lockedin_session: Optional[str] = Cookie(default=None)):
        auth.end_session(lockedin_session)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/api/me")
    def me(user: str = Depends(current_user)):
        return {"user": user, "model": service.get_model_config(home_of(user))}

    @app.get("/api/health")
    def health(user: str = Depends(current_user), tok: str = Depends(claude_token)):
        return service.model_health(home_of(user), claude_token=tok)

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

    @app.delete("/api/bubbles/{slug}")
    def delete_bubble(slug: str, user: str = Depends(current_user)):
        service.delete_bubble(home_of(user), slug)
        return {"ok": True}

    # ---- pages (per-bubble mini-wiki) ----
    @app.get("/api/bubbles/{slug}/pages/{page}")
    def get_page(slug: str, page: str, user: str = Depends(current_user)):
        return {"content": service.get_page(home_of(user), slug, page)}

    @app.get("/api/bubbles/{slug}/preview/{page}")
    def preview_page(slug: str, page: str, user: str = Depends(current_user)):
        """Serve a standalone HTML page — full-screen rendered preview with working intra-bubble links."""
        from fastapi.responses import HTMLResponse

        home = home_of(user)
        content = service.get_page(home, slug, page)
        all_pages = service.list_pages(home, slug)
        name = service.bubble_detail(home, slug)["name"]
        nav_links = " &nbsp;|&nbsp; ".join(
            f'<a href="/api/bubbles/{slug}/preview/{p["page_slug"]}">{p["title"]}</a>'
            for p in all_pages)
        # replace [[page-slug]] and [[Page Title]] with real links
        def resolve_wikilink(m):
            target = m.group(1).strip()
            match = next((p for p in all_pages
                          if p["page_slug"] == target or p["title"].lower() == target.lower()), None)
            if match:
                return f'[{match["title"]}](/api/bubbles/{slug}/preview/{match["page_slug"]})'
            return m.group(0)
        md = _preprocess_equations(re.sub(r'\[\[([^\]]+)\]\]', resolve_wikilink, content))
        html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} — {page}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@11/marked.min.js"></script>
<style>
:root{{font:16px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
      --bg:#0f1115;--ink:#e6e9ef;--muted:#9aa3b2;--line:#2a2f3a;--panel:#1e222b;--accent:#7c9cff}}
body.light{{--bg:#f4f6fb;--ink:#1a1d24;--muted:#6b7280;--line:#d0d7e3;--panel:#eef1f7;--accent:#3b5bdb}}
body{{background:var(--bg);color:var(--ink);max-width:860px;margin:0 auto;padding:24px 32px}}
nav{{font-size:13px;margin-bottom:24px;padding-bottom:12px;border-bottom:1px solid var(--line);color:var(--muted)}}
nav a{{color:var(--accent);text-decoration:none}} nav a:hover{{text-decoration:underline}}
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
#theme-toggle{{position:fixed;top:14px;right:16px;background:var(--panel);border:1px solid var(--line);
               color:var(--ink);border-radius:8px;padding:5px 10px;cursor:pointer;font-size:13px;z-index:10}}
#back-btn{{position:fixed;top:14px;left:16px;background:var(--panel);border:1px solid var(--line);
           color:var(--ink);border-radius:8px;padding:5px 10px;cursor:pointer;font-size:13px;z-index:10}}
</style></head><body>
<button id="back-btn" onclick="window.history.length>1?window.history.back():window.close()">← Back</button>
<button id="theme-toggle" onclick="toggleTheme()">☀️ Light</button>
<nav><b>{name}</b> &nbsp;|&nbsp; {nav_links}</nav>
<div id="content"></div>
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
document.getElementById("content").innerHTML=marked.parse({repr(md)});
renderMathInElement(document.body,{{throwOnError:false,delimiters:[
  {{left:"$$",right:"$$",display:true}},{{left:"\\\\[",right:"\\\\]",display:true}},
  {{left:"\\\\(",right:"\\\\)",display:false}},{{left:"$",right:"$",display:false}}]}});
</script></body></html>"""
        return HTMLResponse(html)

    @app.post("/api/bubbles/{slug}/pages")
    def create_page(slug: str, body: PageCreateIn, user: str = Depends(current_user)):
        if not body.title.strip():
            raise HTTPException(status_code=400, detail="Page title required.")
        return {"page_slug": service.create_page(home_of(user), slug, body.title)}

    @app.put("/api/bubbles/{slug}/pages/{page}")
    def put_page(slug: str, page: str, body: PageContentIn, user: str = Depends(current_user)):
        service.save_page(home_of(user), slug, page, body.content)
        return {"ok": True}

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
    def chat_title(slug: str, body: ChatTitleIn, user: str = Depends(current_user),
                   tok: str = Depends(claude_token)):
        return {"title": service.generate_chat_title(home_of(user), body.messages, claude_token=tok)}

    # ---- streamed: read-only research chat ----
    @app.post("/api/bubbles/{slug}/chat")
    def bubble_chat(slug: str, body: ChatIn, user: str = Depends(current_user),
                    tok: str = Depends(claude_token)):
        home = home_of(user)
        return _stream(lambda: service.chat(home, slug, body.page, body.messages,
                                            body.page_context, body.deep_read_ids,
                                            claude_token=tok))

    return app


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    logger.info("Starting lockedin server on http://%s:%d", host, port)
    uvicorn.run(build_app(), host=host, port=port)
