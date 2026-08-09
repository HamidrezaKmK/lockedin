"""Local web server — the single multi-user UI.

All real work goes through :mod:`lockedin.service`; this module is HTTP + auth glue. Each
request operates on the logged-in user's workspace (``data/users/<user>/``).

Streaming endpoints (chat / generate / edit) run the model in a dedicated worker thread and
hand events back over a queue — the same pattern ocd uses. This keeps the per-user path
context (a contextvar) consistent for the whole generator, including the final save, which
would otherwise be at risk if Starlette resumed the generator on a different threadpool worker.

Launch with ``lockedin serve``.
"""
import contextvars
import json
import logging
import os
import queue
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from . import assets, auth, bubbles, landing, models, paths, service, tagger, workspaces
from . import scientist_sync


# Display-math environments (numbered) vs theorem-like environments (boxed). Shared by the
# reference builder and the two preprocess passes so their ordering rules can't drift.
_DISP_ENVS = r'align\*?|alignat\*?|gather\*?|multline\*?|equation\*?'
_THEO_ENVS = r'theorem|lemma|corollary|definition|proposition|assumption|remark|proof'
_LABEL_RE = re.compile(r'\\label\{([^}]+)\}')


_CITE_RE = re.compile(r'\\cite\{([^}]+)\}')
_REQUEST_WORKSPACE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "lockedin_request_workspace", default=None)
# Keep this equal to ``scientist_cli.SCIENTIST_CLIENT_VERSION``. Bump both when a Scientist
# release needs an installed client refresh; the dependency-free installed client cannot import
# package metadata from this server.
SCIENTIST_CLIENT_VERSION = "2026.08.09.8"


def _build_refs(pages: "list[dict]", bibliography: "dict | None" = None) -> dict:
    """Build the bubble-wide reference registry from pages in manifest order.

    ``pages`` — ordered ``[{"page_slug","content"}, ...]``. Returns
    ``{"eq": {label: n}, "thm": {label: {env, number}}, "thmStart": {page_slug: {env: count}},
    "citeMap": {key: n}, "citeOrder": [key], "bibliography": {key: entry}}``.
    Equation numbers are label-keyed and run across the whole bubble in document order
    (``$$`` blocks and display environments merged by position). Theorems are numbered
    positionally per env (proof unnumbered); ``thmStart`` records each env's running count
    *before* a given page, so one page can be re-numbered in isolation yet stay globally
    consistent. The ordering rules mirror the SPA's ``buildRefs`` (index.html) exactly — keep
    the two in lockstep or a \\tag number and its \\eqref will disagree.
    """
    eq: dict[str, int] = {}
    eq_idx = 0
    thm: dict[str, dict] = {}
    thm_counts: dict[str, int] = {}
    thm_start: dict[str, dict] = {}
    fig: dict[str, dict] = {}
    fig_idx = 0
    fig_start: dict[str, int] = {}
    bibliography = bibliography or {}
    cite_order: list[str] = []
    cite_map: dict[str, int] = {}
    env_re = re.compile(r'\\begin\{(' + _DISP_ENVS + r')\}([\s\S]*?)\\end\{(?:' + _DISP_ENVS + r')\}')
    dd_re = re.compile(r'\$\$([\s\S]+?)\$\$')
    theo_re = re.compile(
        r'\\begin\{(' + _THEO_ENVS + r')\}(?:\[([^\]]*)\])?([\s\S]*?)\\end\{(?:' + _THEO_ENVS + r')\}',
        re.IGNORECASE)
    figure_re = re.compile(r'!\[([^\]\n]*)\]\([^)]+\)')
    for pg in pages:
        slug = pg["page_slug"]
        content = pg.get("content", "") or ""
        thm_start[slug] = dict(thm_counts)
        fig_start[slug] = fig_idx
        for fm in figure_re.finditer(content):
            fig_idx += 1
            label = _LABEL_RE.search(fm.group(1))
            if label and label.group(1) not in fig:
                fig[label.group(1)] = {"number": fig_idx, "page_slug": slug}
        # Equations: collect $$ blocks and display environments, count labels in document order.
        cands: list[tuple[int, str]] = []
        for m in dd_re.finditer(content):
            cands.append((m.start(), m.group(1)))
        for m in env_re.finditer(content):
            cands.append((m.start(), m.group(2)))
        cands.sort(key=lambda x: x[0])
        for _, inner in cands:
            for lm in _LABEL_RE.finditer(inner):
                name = lm.group(1)
                if name not in eq:
                    eq_idx += 1
                    eq[name] = eq_idx
        # Theorems: number positionally per env (proof unnumbered), record labeled ones.
        for m in theo_re.finditer(content):
            env = m.group(1).lower()
            inner = m.group(3)
            numbered = env != "proof"
            if numbered:
                thm_counts[env] = thm_counts.get(env, 0) + 1
            n = thm_counts.get(env, 0) if numbered else 0
            for lm in _LABEL_RE.finditer(inner):
                thm[lm.group(1)] = {"env": env, "number": n}
        for m in _CITE_RE.finditer(content):
            for key in [k.strip() for k in m.group(1).split(",") if k.strip()]:
                if key in bibliography and key not in cite_map:
                    cite_map[key] = len(cite_order) + 1
                    cite_order.append(key)
    return {"eq": eq, "thm": thm, "thmStart": thm_start, "fig": fig, "figStart": fig_start,
            "citeMap": cite_map, "citeOrder": cite_order, "bibliography": bibliography}


def _preprocess_figure_refs(md: str, figure_labels: dict) -> str:
    """Render a figure reference as a styled, non-navigating number."""
    def replace(match: "re.Match") -> str:
        figure = figure_labels.get(match.group(1))
        if not figure:
            return '<span class="fig-ref">Figure ?</span>'
        return f'<span class="fig-ref">Figure {figure["number"]}</span>'
    return re.sub(r'\\figref\{([^}]+)\}', replace, md)


def _bubble_bibliography(home, slug: str) -> dict:
    out: dict[str, dict] = {}
    for meta in service.bubble_detail(home, slug).get("assets", []):
        for entry in assets.parse_bibtex_entries(meta.get("bibliography", "")):
            key = entry["key"]
            if key not in out:
                out[key] = {"key": key, "text": assets.format_bibtex_entry(entry),
                            "type": entry.get("type", ""), "fields": entry.get("fields", {}),
                            "pdf_id": meta.get("pdf_id", "")}
    return out


def _bubble_refs(home, slug: str, all_pages: list) -> dict:
    """Read every page's stored content (manifest order) and build the reference registry."""
    return _build_refs([{"page_slug": p["page_slug"],
                         "content": service.get_page(home, slug, p["page_slug"])}
                        for p in all_pages],
                       bibliography=_bubble_bibliography(home, slug))


def _visible_pages(pages: list[dict]) -> list[dict]:
    return [p for p in pages if not p.get("hidden")]


def _preprocess_theorems(md: str, thm_labels: dict, start_counts: dict) -> "tuple[str, list[dict]]":
    """Extract \\begin{theorem|lemma|...} environments; return (processed_md, theo_store).

    ``start_counts`` seeds each env's counter with the count accumulated on earlier pages
    (from _build_refs' ``thmStart``) so numbering is bubble-wide; ``thm_labels`` is the global
    label→{env,number} map used to resolve \\thmref across pages. Mirrors the SPA's
    renderMarkdown Pass 0. Runs *after* _preprocess_equations (same order as the JS passes).
    """
    _RE = re.compile(
        r'\\begin\{(' + _THEO_ENVS + r')\}(?:\[([^\]]*)\])?([\s\S]*?)\\end\{(?:' + _THEO_ENVS + r')\}',
        re.IGNORECASE,
    )
    counts: dict[str, int] = dict(start_counts)
    theo_store: list[dict] = []

    def _replace(m: re.Match) -> str:
        env = m.group(1).lower()
        opt_title = (m.group(2) or "").strip()
        inner = m.group(3)
        numbered = env != "proof"
        if numbered:
            counts[env] = counts.get(env, 0) + 1
        n = counts.get(env, 0) if numbered else 0
        cap = env.capitalize()
        if numbered:
            title = f"{cap} {n} ({opt_title})" if opt_title else f"{cap} {n}"
        else:
            title = f"{cap} ({opt_title})" if opt_title else cap
        # Strip \label{thm:name} from inner content (it's already recorded in thm_labels).
        clean = re.sub(r'\\label\{[^}]+\}', '', inner).strip()
        theo_store.append({"env": env, "title": title, "inner": clean, "proof": not numbered})
        return f"\n\n@@TH{len(theo_store) - 1}@@\n\n"

    md = _RE.sub(_replace, md)

    def _thmref(m: re.Match) -> str:
        t = thm_labels.get(m.group(1))
        if not t:
            return m.group(0)
        return f'<span class="thm-ref">{t["env"].capitalize()} {t["number"]}</span>'

    md = re.sub(r'\\thmref\{([^}]+)\}', _thmref, md)
    # Resolve \thmref inside theorem/proof bodies too. Their \eqref/\ref and in-math \thmref
    # were already handled by the equations pass, which ran on the full content first.
    for entry in theo_store:
        entry["inner"] = re.sub(r'\\thmref\{([^}]+)\}', _thmref, entry["inner"])
    return md, theo_store


def _resolve_refs_in_math(src: str, eq_nums: dict, thm_labels: dict) -> str:
    """Resolve \\eqref/\\ref/\\thmref that appear INSIDE math to KaTeX-renderable text.

    KaTeX doesn't know these commands, so an in-math reference used to break the whole
    equation. We bake in the resolved value as plain math: \\eqref→(n), \\ref→n,
    \\thmref→\\text{Env n}. Out-of-math references are handled separately (styled spans).
    """
    src = re.sub(r'\\eqref\{([^}]+)\}', lambda m: f'({eq_nums.get(m.group(1), "?")})', src)
    src = re.sub(r'\\ref\{([^}]+)\}', lambda m: str(eq_nums.get(m.group(1), "?")), src)

    def _thm(m: "re.Match") -> str:
        t = thm_labels.get(m.group(1))
        if not t:
            return m.group(0)
        return r'\text{' + t["env"].capitalize() + ' ' + str(t["number"]) + '}'

    return re.sub(r'\\thmref\{([^}]+)\}', _thm, src)


def _preprocess_equations(md: str, eq_nums: dict, thm_labels: dict) -> str:
    """Inject \\tag{n} for labeled equations and resolve \\eqref{}/\\ref{} references.

    ``eq_nums`` is the bubble-wide label→number map (built by _build_refs), so references
    resolve across pages. Each display ``\\label{name}`` becomes ``\\tag{n}`` and environments
    are starred to suppress KaTeX's auto-counter. Math regions are then stashed and their
    in-math references resolved to plain math (see _resolve_refs_in_math) before the remaining
    out-of-math ``\\eqref``/``\\ref`` become styled spans — so KaTeX never sees a raw reference
    and the span substitution can't corrupt math. ``thm_labels`` is only consulted for a
    \\thmref written inside math. Pure rendering transform — source .md files are untouched.
    """
    _ENV = r'\\begin\{(' + _DISP_ENVS + r')\}([\s\S]*?)\\end\{(?:' + _DISP_ENVS + r')\}'

    def _retag(inner: str) -> str:
        # Labeled → \tag{n}; an unknown label (e.g. typed but not yet saved) is dropped so it
        # never reaches KaTeX (which errors on a bare \label).
        return re.sub(r'\\label\{([^}]+)\}',
                      lambda lm: f'\\tag{{{eq_nums[lm.group(1)]}}}' if lm.group(1) in eq_nums else '',
                      inner)

    def _star(env: str) -> str:
        """Return the starred form of an environment name (idempotent)."""
        return env if env.endswith('*') else env + '*'

    md = re.sub(r'\$\$([\s\S]+?)\$\$', lambda m: f'$${_retag(m.group(1))}$$', md)
    md = re.sub(_ENV, lambda m: f'\\begin{{{_star(m.group(1))}}}{_retag(m.group(2))}\\end{{{_star(m.group(1))}}}', md)

    # Stash every math region, resolve its in-math refs, then restore — the out-of-math span
    # substitution below then can't touch math, and KaTeX receives only resolved numbers.
    stored: list[str] = []

    def _stash(m: "re.Match") -> str:
        stored.append(_resolve_refs_in_math(m.group(0), eq_nums, thm_labels))
        return f'@@MR{len(stored) - 1}@@'

    md = re.sub(_ENV, _stash, md)
    md = re.sub(r'\$\$[\s\S]+?\$\$', _stash, md)
    md = re.sub(r'\\\[[\s\S]+?\\\]', _stash, md)
    md = re.sub(r'\\\([\s\S]+?\\\)', _stash, md)
    md = re.sub(r'\$[^\$\n]+?\$', _stash, md)

    md = re.sub(r'\\eqref\{([^}]+)\}',
                lambda m: f'<span class="eq-ref">({eq_nums.get(m.group(1), "?")})</span>', md)
    md = re.sub(r'\\ref\{([^}]+)\}',
                lambda m: f'<span class="eq-ref">{eq_nums.get(m.group(1), "?")}</span>', md)

    md = re.sub(r'@@MR(\d+)@@', lambda m: stored[int(m.group(1))], md)
    return md


def _preprocess_citations(md: str, refs: dict) -> str:
    cite_map = refs.get("citeMap", {}) if refs else {}

    def repl(m: re.Match) -> str:
        labels = []
        for key in [k.strip() for k in m.group(1).split(",") if k.strip()]:
            labels.append(str(cite_map[key]) if key in cite_map else f"?{key}")
        return f'<span class="cite-ref">[{", ".join(labels)}]</span>'

    return _CITE_RE.sub(repl, md)


def _references_markdown(refs: dict) -> str:
    order = refs.get("citeOrder", []) if refs else []
    bibliography = refs.get("bibliography", {}) if refs else {}
    rows = []
    for key in order:
        entry = bibliography.get(key)
        if entry:
            rows.append(f'<p class="bibitem"><span class="bibnum">[{refs["citeMap"][key]}]</span> {entry.get("text", key)}</p>')
    if not rows:
        return ""
    return ('\n\n<section class="references-box">\n<h1>References</h1>\n'
            + "\n".join(rows) + "\n</section>")


def _render_preview_html(*, name: str, page: str, all_pages: list, content: str, slug: str,
                         link_base: str, asset_base: str, show_back: bool,
                         workspace_id: "str | None" = None,
                         todos: "dict | None" = None,
                         todo_link_base: "str | None" = None,
                         macros: "dict | None" = None,
                         refs: "dict | None" = None,
                         themes: "list[str] | None" = None) -> str:
    """Build the standalone rendered-page HTML shared by the owner preview and public share pages.

    ``link_base``  — prefix for intra-bubble nav + wikilinks (e.g. ``/api/bubbles/<slug>/preview``
                     or ``/share/<token>``).
    ``asset_base`` — prefix images resolve to (``/share/<token>/assets`` rewrites the stored
                     ``/api/bubbles/<slug>/assets`` URLs so figures load without a login).
    ``refs``       — the bubble-wide reference registry (from _build_refs) so equation/theorem
                     numbers and \\eqref/\\thmref resolve across pages. ``page`` is the current
                     page slug, used to look up its theorem-counter offset. If omitted, refs are
                     built from this page alone (single-page fallback for tests/standalone use).
    """
    # Private preview pages receive their workspace through a URL query parameter because a
    # new browser tab cannot carry the SPA's request header. Keep it on every page/wikilink;
    # otherwise the first preview page is right but navigation falls back to Personal workspace.
    def page_url(page_slug: str) -> str:
        url = f"{link_base}/{page_slug}"
        return f"{url}?workspace={quote(workspace_id, safe='')}" if workspace_id else url

    nav_links = " &nbsp;|&nbsp; ".join(
        f'<a href="{page_url(p["page_slug"])}">{p["title"]}</a>' for p in all_pages)

    def resolve_wikilink(m):
        raw = m.group(1).strip()
        if "|" in raw:
            target, display = raw.split("|", 1)
            target = target.strip()
            display = display.strip()
        else:
            target = raw
            display = None
        match = next((p for p in all_pages
                      if p["page_slug"] == target or p["title"].lower() == target.lower()), None)
        if match:
            label = display if display else match["title"]
            return f'[{label}]({page_url(match["page_slug"])})'
        return m.group(0)

    # Resolve @<id> TODO references to a label "@<id> <title>" (strikethrough if done). With a
    # todo_link_base (owner preview) it links to the SPA's TODO detail; in share mode (no base)
    # it renders as styled non-link text. Unknown ids are left literal. Tally is by integer, so
    # @50 never matches @5.
    todos_map = todos or {}

    def resolve_todoref(m):
        tid = int(m.group(1))
        todo = todos_map.get(tid)
        if not todo:
            return m.group(0)
        label = f"@{tid} {todo.get('title', '')}".rstrip()
        if todo.get("done"):
            label = f"~~{label}~~"
        if todo_link_base:
            return f'[{label}]({todo_link_base}/{tid})'
        return f'<span class="todoref">{label}</span>'

    content = re.sub(r'@(\d+)', resolve_todoref, content)
    content = re.sub(r'\[\[([^\]]+)\]\]', resolve_wikilink, content)
    # Number equations FIRST (on the full content, incl. theorem/proof interiors) so equations
    # inside theorems join the bubble-wide numbering and \eqref to them resolves; THEN stash the
    # theorem boxes. Matches the SPA renderMarkdown pass order. Numbers come from the bubble-wide
    # registry (refs) so references work across pages; fall back to this page alone if absent.
    if refs is None:
        refs = _build_refs([{"page_slug": page, "content": content}])
    eq_map = refs.get("eq", {})
    thm_map = refs.get("thm", {})
    fig_map = refs.get("fig", {})
    thm_start = refs.get("thmStart", {}).get(page, {})
    fig_start = refs.get("figStart", {}).get(page, 0)
    content = _preprocess_equations(content, eq_map, thm_map)
    content, theo_store = _preprocess_theorems(content, thm_map, thm_start)
    content = _preprocess_figure_refs(content, fig_map)
    content = _preprocess_citations(content, refs)
    for entry in theo_store:
        entry["inner"] = _preprocess_citations(entry["inner"], refs)
        entry["inner"] = _preprocess_figure_refs(entry["inner"], fig_map)
    content += _references_markdown(refs)
    md = content
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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700&family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;1,8..60,400&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@11/marked.min.js"></script>
<style>
:root{{
  --font-ui:'Syne',system-ui,sans-serif;
  --font-reading:'Source Serif 4',Georgia,serif;
  --font-mono:'JetBrains Mono',ui-monospace,monospace;
}}
:root,body.theme-dark{{
  --bg:#0d1018;--ink:#e8ecf4;--muted:#8a94a8;--line:#252d3d;--panel:#1c2230;
  --accent:#9b80ff;--accent2:#4dd9b8;--shadow:rgba(0,0,0,.35);
  --scroll-track:#0d1018;--scroll-thumb:#6957ad;--scroll-thumb-hover:#8c73e8;
  --ref-accent:#b59cff;
}}
body.theme-light{{
  --bg:#f8fafc;--ink:#171b24;--muted:#526073;--line:#9eacbe;--panel:#cfd8e6;
  --accent:#5b3ee8;--accent2:#087f69;--shadow:rgba(17,24,39,.18);
  --scroll-track:#f8fafc;--scroll-thumb:#7d8ca2;--scroll-thumb-hover:#5b3ee8;
  --ref-accent:#6d4aff;
}}
body.theme-pink{{
  --bg:#fff0fa;--ink:#57113e;--muted:#9b4b7d;--line:#ff94d2;--panel:#ffc7e8;
  --accent:#ff1493;--accent2:#00b8a9;--shadow:rgba(255,20,147,.18);
  --scroll-track:#fff0fa;--scroll-thumb:#ff85cf;--scroll-thumb-hover:#ff1493;
  --ref-accent:var(--accent2);
}}
body.theme-techno{{
  --bg:#020704;--ink:#d7f4dc;--muted:#82b58a;--line:#174f25;--panel:#0b1b10;
  --accent:#35c85a;--accent2:#8bd68f;--shadow:rgba(53,200,90,.16);
  --scroll-track:#020704;--scroll-thumb:#247a38;--scroll-thumb-hover:#35c85a;
  --ref-accent:var(--accent2);
}}
body.theme-pearl{{
  --bg:#fffdf7;--ink:#3f3f3b;--muted:#7c7168;--line:#d8d2c4;--panel:#f3f0e8;
  --accent:#d9480f;--accent2:#f06d3a;--shadow:rgba(217,72,15,.16);
  --scroll-track:#fffdf7;--scroll-thumb:#d2b48c;--scroll-thumb-hover:#d9480f;
  --ref-accent:var(--accent2);
}}
*{{box-sizing:border-box}}
html,body,*{{scrollbar-width:thin;scrollbar-color:var(--scroll-thumb) var(--scroll-track)}}
html::-webkit-scrollbar,body::-webkit-scrollbar,*::-webkit-scrollbar{{width:10px;height:10px}}
html::-webkit-scrollbar-track,body::-webkit-scrollbar-track,*::-webkit-scrollbar-track{{background:var(--scroll-track)}}
html::-webkit-scrollbar-thumb,body::-webkit-scrollbar-thumb,*::-webkit-scrollbar-thumb{{background:var(--scroll-thumb);
  border-radius:999px;border:3px solid transparent;background-clip:content-box}}
html:hover::-webkit-scrollbar-thumb,body:hover::-webkit-scrollbar-thumb,*:hover::-webkit-scrollbar-thumb{{background:var(--scroll-thumb-hover);
  background-clip:content-box}}
body{{background:var(--bg);color:var(--ink);max-width:860px;margin:0 auto;padding:24px 32px;
     font:16px/1.75 var(--font-reading)}}
nav{{font-family:var(--font-ui);font-size:13px;font-weight:600;margin-bottom:28px;
     padding-bottom:12px;border-bottom:1px solid var(--line);color:var(--muted)}}
nav a{{color:var(--accent);text-decoration:none}} nav a:hover{{text-decoration:underline}}
h1,h2,h3,h4{{position:relative;font-family:var(--font-reading);font-weight:600;letter-spacing:-.01em}}
h1:hover .anchor,h2:hover .anchor,h3:hover .anchor,h4:hover .anchor{{opacity:.55}}
.anchor{{position:absolute;left:-1.1em;opacity:0;text-decoration:none;color:var(--accent);
         cursor:pointer;font-size:.8em;padding-right:.3em}}
.anchor:hover{{opacity:1!important}}
h1{{font-size:30px;margin-top:0}}
h2{{font-size:22px;border-bottom:1px solid var(--line);padding-bottom:5px;margin-top:2em}}
h3{{font-size:18px}} h4{{font-size:16px}}
p{{margin:.65em 0}}
code{{background:var(--panel);padding:2px 6px;border-radius:4px;font-family:var(--font-mono);font-size:.84em}}
pre{{background:var(--panel);padding:14px;border-radius:10px;overflow:auto;
     box-shadow:0 2px 12px var(--shadow)}}
pre code{{font-family:var(--font-mono);font-size:13px}}
a{{color:var(--accent)}} img{{max-width:100%;border-radius:8px;box-shadow:0 2px 12px var(--shadow)}}
figure{{margin:1.1em 0;text-align:center}} figure img{{display:block;margin:0 auto}}
figcaption{{margin:.55em auto 0;max-width:92%;font-style:italic;line-height:1.45;color:var(--muted)}}
.figure-number{{font-style:normal;font-weight:600;color:var(--ink)}}
table{{display:block;width:max-content;max-width:100%;overflow-x:auto;border-collapse:collapse;margin:14px 0;font-size:15px}}
th,td{{border:1px solid var(--line);padding:7px 11px;text-align:left}}
thead th{{background:var(--panel);font-family:var(--font-ui);font-size:13px;font-weight:600}}
blockquote{{margin:14px 0;padding:8px 16px;border-left:3px solid var(--accent);color:var(--muted);
            background:var(--panel);border-radius:0 8px 8px 0}}
blockquote p{{margin:5px 0}}
.katex-display{{overflow-x:auto;overflow-y:hidden;max-width:100%}}
/* Take the \tag out of KaTeX's absolute right:0 so a wide equation can't run under it: the
   number trails the centred body and a too-wide equation just scrolls. Mirrors the SPA rule. */
.katex-display>.katex>.katex-html>.tag{{position:static;margin-left:1.2em}}
/* An equation's own number shares the accent + tabular figures with its cross-references. */
.katex .tag,.katex-display .tag{{color:var(--accent);font-feature-settings:"tnum" 1;font-variant-numeric:tabular-nums}}
/* Cross-references: colored inline labels that go quiet on hover. */
.eq-ref,.thm-ref,.fig-ref,.cite-ref{{font-family:var(--font-ui);font-weight:600;color:var(--accent);
  font-feature-settings:"tnum" 1;font-variant-numeric:tabular-nums;white-space:nowrap;
  padding:0 .14em;border-radius:4px;transition:color .14s ease}}
.eq-ref:hover,.thm-ref:hover,.fig-ref:hover,.cite-ref:hover{{color:var(--ink)}}
.text-color{{color:var(--tc)}}
.centered-text{{text-align:center}}
.bibitem{{display:grid;grid-template-columns:auto 1fr;gap:.65em;margin:.5em 0;line-height:1.55}}
.bibnum{{font-family:var(--font-ui);font-weight:700;color:var(--accent);
  font-feature-settings:"tnum" 1;font-variant-numeric:tabular-nums}}
.references-box{{margin:2.2em 0 1.2em;padding:1.05em 1.2em 1.15em;
  border-radius:7px;background:
    linear-gradient(135deg,color-mix(in srgb,var(--ref-accent) 13%,transparent),transparent 58%),
    var(--panel);
  border:1px solid color-mix(in srgb,var(--ref-accent) 32%,var(--line));
  box-shadow:0 14px 34px -28px rgba(0,0,0,.9)}}
.references-box h1{{margin:.05em 0 .55em;padding:0;border:none;
  font-family:var(--font-ui);font-size:15px;font-weight:700;letter-spacing:.08em;
  text-transform:uppercase;color:var(--ref-accent)}}
.references-box .bibitem:last-child{{margin-bottom:0}}
/* Theorem / lemma / proof boxes: refined journal callouts, one jewel tone per class (--env). */
.math-env{{--env:var(--accent);position:relative;margin:1.6em 0;padding:.85em 1.15em .85em 1.4em;
  border-radius:4px 12px 12px 4px;
  background:linear-gradient(90deg,color-mix(in srgb,var(--env) 9%,transparent),transparent 42%),var(--panel);
  border:1px solid color-mix(in srgb,var(--env) 24%,var(--line));
  box-shadow:0 10px 30px -22px rgba(0,0,0,.8)}}
.math-env::before{{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;border-radius:4px 0 0 4px;
  background:linear-gradient(var(--env),color-mix(in srgb,var(--env) 35%,transparent))}}
.math-env-title{{font-family:var(--font-ui);font-weight:700;font-size:.74em;letter-spacing:.14em;
  text-transform:uppercase;color:var(--env);margin-bottom:.45em;
  font-feature-settings:"tnum" 1;font-variant-numeric:tabular-nums}}
.math-env.definition,.math-env.proposition{{--env:var(--accent2)}}
.math-env.assumption{{--env:var(--accent2)}}
.math-env.remark{{--env:var(--muted)}}
.math-env.proof{{--env:color-mix(in srgb,var(--muted) 55%,var(--line))}}
.math-env.proof .math-env-title{{letter-spacing:.12em;opacity:.85}}
.math-env .math-env-qed{{text-align:right;margin-top:.5em;color:var(--env);opacity:.6;font-size:1.15em;line-height:1}}
.todoref{{color:var(--accent);font-weight:500;background:var(--panel);padding:1px 5px;border-radius:4px;
          font-family:var(--font-ui);font-size:.9em}}
#copied{{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--panel);
         border:1px solid var(--line);padding:8px 14px;border-radius:8px;
         font-family:var(--font-ui);font-size:13px;opacity:0;
         transition:opacity .2s;pointer-events:none}}
#copied.show{{opacity:1}}
#theme-cycle{{position:fixed;top:14px;right:16px;background:var(--panel);border:1px solid var(--line);
               color:var(--ink);border-radius:8px;padding:6px 12px;cursor:pointer;
               font-family:var(--font-ui);font-size:13px;font-weight:600;z-index:10}}
#back-btn{{position:fixed;top:14px;left:16px;background:var(--panel);border:1px solid var(--line);
           color:var(--ink);border-radius:8px;padding:6px 12px;cursor:pointer;
           font-family:var(--font-ui);font-size:13px;font-weight:600;z-index:10}}
.page-credit{{margin-top:56px;padding-top:18px;border-top:1px solid var(--line);
  font-family:var(--font-ui);font-size:12px;color:var(--muted);text-align:center}}
.page-credit a{{color:var(--muted);text-decoration:none}}
.page-credit a:hover{{color:var(--accent)}}
@media(max-width:600px){{
  body{{padding:16px 18px}}
  #theme-cycle,#back-btn{{top:10px;padding:5px 10px;font-size:12px}}
  #back-btn{{left:10px}} #theme-cycle{{right:10px}}
  h1{{font-size:24px}} h2{{font-size:19px}}
}}
</style></head><body>
{back_btn}
<button id="theme-cycle" onclick="cycleTheme()" title="Cycle theme">🌙</button>
<nav><b>{name}</b> &nbsp;|&nbsp; {nav_links}</nav>
<div id="content"></div>
<footer class="page-credit">Made with 💜 + 🤖 by a PhD student</footer>
<div id="copied">🔗 Link copied</div>
<script>
(function(){{
  // Standalone owner previews and public shares intentionally stay restrained: they only
  // offer the universal Dark/Light pair, independent of the workspace's editor theme choices.
  const THEMES={json.dumps(["dark", "light"])};
  const LABELS={{dark:"🌙",light:"☀️",pink:"🦄",techno:"🤖",pearl:"⚪"}};
  window.applyPreviewTheme=function(name){{
    const theme=THEMES.includes(name)?name:THEMES[0];
    document.body.classList.remove(...THEMES.map(t=>"theme-"+t),"light");
    document.body.classList.add("theme-"+theme);
    document.getElementById("theme-cycle").textContent=LABELS[theme];
    localStorage.setItem("li_theme",theme);
  }};
  window.cycleTheme=function(){{
    const cur=localStorage.getItem("li_theme")||localStorage.getItem("preview_theme")||"dark";
    applyPreviewTheme(THEMES[(THEMES.indexOf(cur)+1)%THEMES.length]);
  }};
  applyPreviewTheme(localStorage.getItem("li_theme")||localStorage.getItem("preview_theme")||"dark");
}})();
// Render markdown + math + theorem environments.
// Equation numbering (\\label→\\tag) was already done server-side by _preprocess_equations.
// Theorem blocks were extracted server-side and injected as _theoStore below.
// This JS step: stash math → marked.parse → KaTeX restore → theorem block restore.
// Test marker: marked.parse(s) is the intended ordering; options are passed explicitly below.
// Mirrors renderMarkdown() in index.html so preview and split-view are always identical.
(function(){{
  const store=[], captionStore=[]; let s={repr(md)};
  const _macros={json.dumps(macros or {})};
  const _theoStore={json.dumps(theo_store)};
  const _figRefs={json.dumps(fig_map)};
  let _nextFigure={int(fig_start)};
  const escHtml=t=>String(t||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  const escAttr=t=>escHtml(t).replace(/"/g,"&quot;");
  const renderTextColor=src=>src.replace(/\\\\textcolor\\{{(#[0-9a-fA-F]{{3}}(?:[0-9a-fA-F]{{3}})?)\\}}\\{{([^{{}}\\n]*)\\}}/g,
    (_,color,text)=>'<span class="text-color" style="--tc:'+color+'">'+escHtml(text)+'</span>');
  const renderMath=it=>{{ try{{ return katex.renderToString(it.src,{{displayMode:it.display,macros:_macros,throwOnError:false}}); }}
    catch(e){{ return '<span style="color:#ff7a7a">'+it.src+'</span>'; }} }};
  const stash=(re,display)=>{{ s=s.replace(re,(m,p1)=>{{ store.push({{src:p1,display}}); return "@@M"+(store.length-1)+"@@"; }}); }};
  // marked places image alt text in an HTML attribute. Preserve it while math is stashed so an
  // inline `$...$` caption is restored as text and rendered below the figure, not inside HTML.
  s=s.replace(/!\[([^\]\\n]*)\](?=\()/g,(_,caption)=>{{ captionStore.push(caption); return "![@@LI_CAP"+(captionStore.length-1)+"@@]"; }});
  s=s.replace(/(\\\\begin\\{{(?:align\\*?|alignat\\*?|gather\\*?|multline\\*?|equation\\*?)\\}}[\\s\\S]*?\\\\end\\{{(?:align\\*?|alignat\\*?|gather\\*?|multline\\*?|equation\\*?)\\}})/g,(m,p1)=>{{ store.push({{src:p1,display:true}}); return "@@M"+(store.length-1)+"@@"; }});
  stash(/\\$\\$([\\s\\S]+?)\\$\\$/g,true);
  stash(/\\\\\\[([\\s\\S]+?)\\\\\\]/g,true);
  stash(/\\\\\\(([\\s\\S]+?)\\\\\\)/g,false);
  stash(/\\$([^\\$\\n]+?)\\$/g,false);
  s=renderTextColor(s);
  let html=marked.parse(s,{{breaks:false}});
  html=html.replace(/@@M(\\d+)@@/g,(m,i)=>renderMath(store[+i]));
  html=html.replace(/@@LI_CAP(\\d+)@@/g,(_,i)=>escAttr(captionStore[+i]||""));
  // Restore theorem/lemma/proof blocks: render inner content with math
  html=html.replace(/<p>@@TH(\\d+)@@<\\/p>/g,(m,i)=>{{
    const t=_theoStore[+i];
    const istore=[]; let is=t.inner;
    const istash=(re,disp)=>{{ is=is.replace(re,(_,p1)=>{{ istore.push({{src:p1,display:disp}}); return "@@IM"+(istore.length-1)+"@@"; }}); }};
    is=is.replace(/(\\\\begin\\{{(?:align\\*?|alignat\\*?|gather\\*?|multline\\*?|equation\\*?)\\}}[\\s\\S]*?\\\\end\\{{(?:align\\*?|alignat\\*?|gather\\*?|multline\\*?|equation\\*?)\\}})/g,(_,p1)=>{{ istore.push({{src:p1,display:true}}); return "@@IM"+(istore.length-1)+"@@"; }});
    istash(/\\$\\$([\\s\\S]+?)\\$\\$/g,true);
    istash(/\\\\\\[([\\s\\S]+?)\\\\\\]/g,true);
    istash(/\\\\\\(([\\s\\S]+?)\\\\\\)/g,false);
    istash(/\\$([^\\$\\n]+?)\\$/g,false);
    is=renderTextColor(is);
    let ih=marked.parse(is,{{breaks:false}});
    ih=ih.replace(/@@IM(\\d+)@@/g,(_,j)=>renderMath(istore[+j]));
    const qed=t.proof?'<div class="math-env-qed">∎</div>':'';
    return '<div class="math-env '+t.env+'"><div class="math-env-title">'+t.title+'</div>'+ih+qed+'</div>';
  }});
  const content=document.getElementById("content");
  content.innerHTML=html;
  // A Markdown image's alt text is its figure caption in previews and public shares.
  content.querySelectorAll("p > img:only-child[alt]").forEach(img=>{{
    _nextFigure++;
    const rawCaption=img.getAttribute("alt")||"";
    const labelMatch=/\\\\label\\{{([^}}]+)\\}}/.exec(rawCaption);
    const label=labelMatch&&labelMatch[1];
    const caption=rawCaption.replace(/\\\\label\\{{[^}}]+\\}}/g,"").trim();
    const number=label&&_figRefs[label]?_figRefs[label].number:_nextFigure;
    const paragraph=img.parentElement, figure=document.createElement("figure");
    if(label)figure.id="fig-"+encodeURIComponent(label);
    paragraph.replaceWith(figure); figure.append(img);
    const figcaption=document.createElement("figcaption");
    const marker=document.createElement("span"); marker.className="figure-number";
    marker.textContent="Figure "+number+": "; figcaption.append(marker);
    let last=0, match; const math=/[$]([^$\\n]+?)[$]/g;
    while((match=math.exec(caption))!==null){{
      if(match.index>last)figcaption.append(document.createTextNode(caption.slice(last,match.index)));
      try{{
        const node=document.createElement("span");
        node.innerHTML=katex.renderToString(match[1],{{displayMode:false,macros:_macros,throwOnError:false}});
        figcaption.append(node);
      }}catch(e){{ figcaption.append(document.createTextNode(match[0])); }}
      last=math.lastIndex;
    }}
    if(last<caption.length)figcaption.append(document.createTextNode(caption.slice(last)));
    figure.append(figcaption);
  }});
  // Every rendered bubble preview starts GIF figures at their first frame. The GIF's embedded
  // loop setting still controls repeated playback after that.
  content.querySelectorAll("img[src]").forEach(img=>{{
    const src=img.getAttribute("src")||"";
    if(!/\\.gif(?:[?#]|$)/i.test(src))return;
    img.src="";
    img.src=src+(src.includes("?")?"&":"?")+"lockedin_gif="+Date.now();
  }});
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
_SCIENTIST_DEVICES: dict[str, dict] = {}

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
    # One-time-compatible, idempotent repair for share links created before content became
    # workspace-owned. Existing links retain their tokens and now target Personal workspaces.
    service.migrate_share_index_to_workspaces()
    service.migrate_overleaf_fields()
    if CROSS_SITE:
        app.add_middleware(CORSMiddleware, allow_origins=PUBLIC_ORIGINS,
                           allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    else:
        app.add_middleware(CORSMiddleware, allow_origins=["*"],
                           allow_methods=["*"], allow_headers=["*"])

    def scientist_reinstall_detail() -> str:
        return ("LockedIn Scientist is out of date. Reinstall the newest version, then retry. "
                "macOS/Linux: curl -fsSL "
                "https://raw.githubusercontent.com/HamidrezaKmK/lockedin/main/install.sh | bash; "
                "Windows PowerShell: irm "
                "https://raw.githubusercontent.com/HamidrezaKmK/lockedin/main/install.ps1 | iex")

    @app.middleware("http")
    async def retired_scientist_v1(request, call_next):
        """Retire v1 safely while giving installed v1 clients an actionable upgrade response."""
        if request.url.path.startswith("/api/scientist/v1/"):
            return JSONResponse({"detail": scientist_reinstall_detail()}, status_code=426)
        return await call_next(request)

    @app.middleware("http")
    async def workspace_request_context(request, call_next):
        """Make the selected workspace available to the entire request.

        Setting a ContextVar in a synchronous FastAPI dependency does not reliably propagate to
        the synchronous endpoint handler. The ASGI request context does.
        """
        workspace_id = ((request.headers.get("X-LockedIn-Workspace")
                         or request.query_params.get("workspace") or "").strip() or None)
        token = _REQUEST_WORKSPACE.set(workspace_id)
        user = auth.session_user(request.cookies.get("lockedin_session"))
        if not user:
            bearer = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            user = auth.scientist_token_user(bearer) if bearer else None
        account_home = paths.user_home(user) if user else None
        try:
            with models.use_account_home(account_home):
                return await call_next(request)
        finally:
            _REQUEST_WORKSPACE.reset(token)

    # ---- request models ----
    class Credentials(BaseModel):
        username: str
        password: str

    class SlackLinkIn(BaseModel):
        slack_user_id: str

    class AccountIn(BaseModel):
        current_password: str
        new_username: Optional[str] = None
        new_password: Optional[str] = None

    class ApprovalIn(BaseModel):
        approved: bool

    class PremiumIn(BaseModel):
        premium: bool

    class SlackAskIn(BaseModel):
        slug: str
        question: str

    class ShareIn(BaseModel):
        active: bool

    class AssetPatch(BaseModel):
        title: Optional[str] = None
        tags: Optional[list[str]] = None
        notes: Optional[str] = None
        url_source: Optional[str] = None
        attention_flag: Optional[bool] = None

    class AssetBibtexIn(BaseModel):
        bibliography: str = ""

    class BibtexPreviewIn(BaseModel):
        bibliography: str = ""

    class AssetUrlIn(BaseModel):
        url: str
        title: str = ""
        tags: str = ""
        bibliography: str = ""

    class BubbleIn(BaseModel):
        name: str

    class ApproveIn(BaseModel):
        instructions: str = ""

    class BubbleRenameIn(BaseModel):
        name: str

    class BubbleArchiveIn(BaseModel):
        archived: bool

    class OverleafIn(BaseModel):
        project: str = ""

    class AddPdfIn(BaseModel):
        pdf_id: str

    class PaperScoreIn(BaseModel):
        score: int

    class MigratePapersIn(BaseModel):
        source: str
        items: list[dict] = []

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

    class PageHiddenIn(BaseModel):
        hidden: bool

    class PageOrderIn(BaseModel):
        page_slugs: list[str]

    class CommentCreateIn(BaseModel):
        body: str
        anchor: dict

    class CommentReplyIn(BaseModel):
        body: str

    class CommentEditIn(BaseModel):
        body: str

    class CommentStatusIn(BaseModel):
        status: str

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

    class MathConfigIn(BaseModel):
        macros: dict

    class AestheticsConfigIn(BaseModel):
        themes: list[str] = []

    class TodoIn(BaseModel):
        title: str
        note: str = ""

    class TodoUpdateIn(BaseModel):
        title: Optional[str] = None
        note: Optional[str] = None
        done: Optional[bool] = None

    class ScientistDeviceIn(BaseModel):
        client_name: str = "lockedin-scientist"

    class ScientistPushIn(BaseModel):
        writes: list[dict] = []

    class ScientistDeleteIn(BaseModel):
        deletes: list[dict] = []

    class ScientistPageCreateIn(BaseModel):
        bubble: str
        page_slug: str
        content_b64: str
        base_revision: str

    class ScientistFilesIn(BaseModel):
        paths: list[str] = []

    class WorkspaceIn(BaseModel):
        name: str

    class WorkspaceMemberIn(BaseModel):
        username: str

    class WorkspaceRoleIn(BaseModel):
        role: str

    # ---- auth plumbing ----
    def current_user(lockedin_session: Optional[str] = Cookie(default=None),
                     x_lockedin_workspace: Optional[str] = Header(default=None),
                     workspace: Optional[str] = None) -> str:
        user = auth.session_user(lockedin_session)
        if not user:
            raise HTTPException(status_code=401, detail="Please log in.")
        if not auth.is_approved(user):
            auth.end_session(lockedin_session)
            raise HTTPException(status_code=403, detail="Account is waiting for approval.")
        accounts = auth.load_accounts()
        account = accounts.get(user, {})
        personal = workspaces.migrate_legacy(user, account)
        if account.get("personal_workspace_id") != personal["id"]:
            accounts[user]["personal_workspace_id"] = personal["id"]
            auth.save_accounts(accounts)
        workspace_id = (x_lockedin_workspace or workspace or personal["id"]).strip()
        try:
            workspaces.resolve(user, workspace_id)
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except workspaces.WorkspaceError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return user

    def home_of(user: str) -> Path:
        return workspaces.workspace_home(active_workspace_id(user))

    def active_workspace_id(user: str) -> str:
        """The request selection, or the user's mandatory Personal workspace."""
        selected = _REQUEST_WORKSPACE.get()
        if selected:
            return selected
        accounts = auth.load_accounts()
        personal = workspaces.migrate_legacy(user, accounts.get(user, {}))
        if accounts.get(user, {}).get("personal_workspace_id") != personal["id"]:
            accounts[user]["personal_workspace_id"] = personal["id"]
            auth.save_accounts(accounts)
        return personal["id"]

    def scientist_client_version(x_lockedin_scientist_version: Optional[str] = Header(default=None)) -> None:
        if x_lockedin_scientist_version != SCIENTIST_CLIENT_VERSION:
            raise HTTPException(
                status_code=426,
                detail=scientist_reinstall_detail(),
            )

    def scientist_user(authorization: Optional[str] = Header(default=None),
                       x_lockedin_workspace: Optional[str] = Header(default=None),
                       workspace: Optional[str] = None,
                       _version: None = Depends(scientist_client_version)) -> str:
        token = (authorization or "").removeprefix("Bearer ").strip()
        user = auth.scientist_token_user(token)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid Scientist token.")
        accounts = auth.load_accounts(); rec = accounts.get(user, {})
        personal = workspaces.migrate_legacy(user, rec)
        workspace_id = (x_lockedin_workspace or workspace or personal["id"]).strip()
        try:
            workspaces.resolve(user, workspace_id)
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except workspaces.WorkspaceError as e:
            raise HTTPException(status_code=404, detail=str(e))
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

    def _require_slack_secret(secret: str | None) -> None:
        expected = os.environ.get("LOCKEDIN_SLACK_SHARED_SECRET") or os.environ.get("SLACK_BOT_TOKEN")
        if not expected:
            raise HTTPException(status_code=503, detail="Slack linking is not configured.")
        if not secret or not secrets.compare_digest(secret, expected):
            raise HTTPException(status_code=403, detail="Invalid Slack secret.")

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

    @app.get("/api/landing")
    def get_landing():
        # This is deliberately read at request time: landing.yaml is the site's editable public
        # copy, so changing it should be visible after a browser refresh without a deploy/restart.
        return JSONResponse(landing.load_landing(), headers={"Cache-Control": "no-cache"})

    @app.get("/api/help")
    def get_help():
        from . import reports as _r
        invite = (os.environ.get("SLACKBOT_INVITE_LINK") or "").strip()
        if invite and re.match(r"^https://join\.slack\.com/\S+$", invite):
            invite_md = f"[Join the lockedin Slack workspace]({invite}) to use the bot."
        else:
            invite_md = ("Ask the workspace admin for the Slack invite link. Operators can set "
                         "`SLACKBOT_INVITE_LINK` in the server environment to show it here.")
        sections = []
        for sec in _r.APP_USAGE_GUIDE_SECTIONS:
            item = dict(sec)
            item["content"] = item["content"].replace("{{SLACKBOT_INVITE}}", invite_md)
            sections.append(item)
        return {"sections": sections}

    # ---- public share (NO auth — gated only by the unlisted token + the bubble's active flag) ----
    @app.get("/share/{token}")
    def share_root(token: str):
        from fastapi.responses import RedirectResponse
        tgt = service.share_target(token)
        if not tgt:
            raise HTTPException(status_code=404, detail="This share link is not active.")
        home, slug = tgt
        detail = service.bubble_detail(home, slug)
        visible_pages = _visible_pages(detail.get("pages", []))
        if not visible_pages:
            raise HTTPException(status_code=404, detail="No visible pages.")
        home_page = detail.get("home") or visible_pages[0]["page_slug"]
        if not any(p["page_slug"] == home_page for p in visible_pages):
            home_page = visible_pages[0]["page_slug"]
        return RedirectResponse(url=f"/share/{token}/{home_page}")

    @app.get("/share/{token}/{page}")
    def share_page(token: str, page: str):
        from fastapi.responses import HTMLResponse
        tgt = service.share_target(token)
        if not tgt:
            raise HTTPException(status_code=404, detail="This share link is not active.")
        home, slug = tgt
        all_pages = service.list_pages(home, slug)
        visible_pages = _visible_pages(all_pages)
        if not any(p["page_slug"] == page for p in visible_pages):
            raise HTTPException(status_code=404, detail="No such page.")
        html = _render_preview_html(
            name=service.bubble_detail(home, slug)["name"], page=page,
            all_pages=visible_pages, content=service.get_page(home, slug, page),
            slug=slug, link_base=f"/share/{token}",
            asset_base=f"/share/{token}/assets", show_back=False,
            todos={t["id"]: t for t in service.list_todos(home)},  # share: styled text, no link
            macros=service.load_math_config(home).get("macros", {}),
            refs=_bubble_refs(home, slug, visible_pages),
            themes=service.load_aesthetics_config(home)["themes"])
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
        # Share URLs are explicitly public capabilities and may be cached independently of the
        # authenticated owner route below.
        return FileResponse(p, headers={"Content-Disposition": "inline",
                                        "Cache-Control": "public, max-age=3600"})

    # ---- auth ----
    @app.post("/api/signup")
    def signup(creds: Credentials):
        try:
            user = auth.create_user(creds.username, creds.password)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        rec = auth.load_accounts().get(user, {})
        personal = workspaces.ensure_personal(user, rec)
        service.ensure_workspace(workspaces.workspace_home(personal["id"]))
        return _auth_response(user)

    @app.post("/api/login")
    def login(creds: Credentials):
        username = creds.username.strip().lower()
        if not auth.verify_password(username, creds.password):
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        if not auth.is_approved(username):
            raise HTTPException(status_code=403, detail="Account is waiting for admin approval.")
        return _auth_response(username)

    @app.post("/api/slack/link")
    def slack_link(
        body: SlackLinkIn,
        user: str = Depends(current_user),
        x_lockedin_slack_secret: Optional[str] = Header(default=None),
    ):
        _require_slack_secret(x_lockedin_slack_secret)
        try:
            auth.link_slack_user(user, body.slack_user_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "user": user}

    @app.post("/api/slack/session")
    def slack_session(
        body: SlackLinkIn,
        x_lockedin_slack_secret: Optional[str] = Header(default=None),
    ):
        _require_slack_secret(x_lockedin_slack_secret)
        user = auth.user_for_slack(body.slack_user_id)
        if not user:
            raise HTTPException(status_code=404, detail="No lockedin account is linked to this Slack user.")
        if not auth.is_approved(user):
            raise HTTPException(status_code=403, detail="Account is waiting for admin approval.")
        return _auth_response(user)

    @app.post("/api/logout")
    def logout(lockedin_session: Optional[str] = Cookie(default=None)):
        auth.end_session(lockedin_session)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/api/me")
    def me(user: str = Depends(current_user)):
        rec = auth.load_accounts().get(user, {})
        return {"user": user, "model": service.get_model_config(home_of(user)),
                "premium": auth.is_premium(user),
                "premium_requested_at": rec.get("premium_requested_at", ""),
                "admin": auth.is_admin(user),
                "themes": service.load_aesthetics_config(home_of(user))["themes"],
                "workspace_id": active_workspace_id(user),
                "personal_workspace_id": rec.get("personal_workspace_id", "")}

    # ---- workspaces ----------------------------------------------------------
    @app.get("/api/workspaces")
    def list_workspaces(user: str = Depends(current_user)):
        rec = auth.load_accounts().get(user, {})
        return {"workspaces": workspaces.list_for_user(user),
                "personal_workspace_id": rec.get("personal_workspace_id", ""),
                "active_workspace_id": active_workspace_id(user)}

    @app.post("/api/workspaces")
    def create_workspace(body: WorkspaceIn, user: str = Depends(current_user)):
        try:
            item = workspaces.create(user, body.name, kind="shared")
        except workspaces.WorkspaceError as e:
            raise HTTPException(status_code=400, detail=str(e))
        service.ensure_workspace(workspaces.workspace_home(item["id"]))
        return {"workspace": item}

    @app.get("/api/workspaces/users")
    def workspace_invitable_users(user: str = Depends(current_user)):
        """Approved accounts an admin may add to the active workspace."""
        try:
            workspaces.resolve(user, active_workspace_id(user), admin=True)
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))
        return {"users": [row["username"] for row in auth.list_users()
                          if row.get("approved") and row["username"] != user]}

    @app.get("/api/workspaces/{workspace_id}/members")
    def workspace_members(workspace_id: str, user: str = Depends(current_user)):
        try:
            return {"members": workspaces.members(user, workspace_id)}
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except workspaces.WorkspaceError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/workspaces/{workspace_id}/members")
    def add_workspace_member(workspace_id: str, body: WorkspaceMemberIn,
                             user: str = Depends(current_user)):
        if not auth.is_approved(body.username.strip().lower()):
            raise HTTPException(status_code=400, detail="No approved LockedIn account has that username.")
        try:
            return {"workspace": workspaces.invite(user, workspace_id, body.username)}
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except workspaces.WorkspaceError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.put("/api/workspaces/{workspace_id}/members/{username}")
    def set_workspace_member_role(workspace_id: str, username: str, body: WorkspaceRoleIn,
                                  user: str = Depends(current_user)):
        try:
            return {"workspace": workspaces.set_role(user, workspace_id, username, body.role)}
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except workspaces.WorkspaceError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/api/workspaces/{workspace_id}/members/{username}")
    def delete_workspace_member(workspace_id: str, username: str, user: str = Depends(current_user)):
        try:
            return {"workspace": workspaces.remove_member(user, workspace_id, username)}
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except workspaces.WorkspaceError as e:
            raise HTTPException(status_code=400, detail=str(e))

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

    @app.post("/api/account/premium-request")
    def request_premium(user: str = Depends(current_user)):
        try:
            requested_at = auth.request_premium(user)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"premium": auth.is_premium(user), "premium_requested_at": requested_at}

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

    @app.put("/api/admin/users/{username}/premium")
    def admin_user_premium(username: str, body: PremiumIn, user: str = Depends(current_user)):
        if not auth.is_admin(user):
            raise HTTPException(status_code=403, detail="Admin access required.")
        target = username.strip().lower()
        try:
            auth.set_premium(target, body.premium)
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
        try:
            return {"config": service.save_model_config(home_of(user), body.config)}
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))

    @app.put("/api/settings/model/active")
    def put_active(body: ActiveIn, user: str = Depends(current_user)):
        try:
            return {"config": service.set_active_provider(home_of(user), body.active)}
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ---- math settings ----
    @app.get("/api/settings/math")
    def get_math(user: str = Depends(current_user)):
        return service.load_math_config(home_of(user))

    @app.put("/api/settings/math")
    def put_math(body: MathConfigIn, user: str = Depends(current_user)):
        return service.save_math_config(home_of(user), {"macros": body.macros})

    # ---- aesthetics settings ----
    @app.get("/api/settings/aesthetics")
    def get_aesthetics(user: str = Depends(current_user)):
        return service.load_aesthetics_config(home_of(user))

    @app.put("/api/settings/aesthetics")
    def put_aesthetics(body: AestheticsConfigIn, user: str = Depends(current_user)):
        try:
            return service.save_aesthetics_config(home_of(user), body.themes)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/slack/ask")
    def slack_ask(body: SlackAskIn, user: str = Depends(current_user)):
        """Plain-text Slack Q&A using the logged-in user's configured model and entitlements."""
        home = home_of(user)
        detail = service.bubble_detail(home, body.slug)
        pages = detail.get("pages") or []
        page = detail.get("home") or (pages[0]["page_slug"] if pages else "overview")
        messages = [{"role": "user", "content": body.question}]
        answer = ""
        for ev in service.chat(home, body.slug, page, messages):
            if ev.get("type") == "error":
                raise HTTPException(status_code=400, detail=ev.get("detail") or "Chat failed.")
            if ev.get("type") == "done":
                answer = ev.get("chat_text") or ev.get("full_response") or answer
        return {"answer": answer.strip()}

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
        bibliography: str = Form(""),
    ):
        if not file.filename:
            raise HTTPException(status_code=400, detail="No file provided.")
        if not title.strip():
            raise HTTPException(status_code=400, detail="A title is required.")
        pdf_bytes = await file.read()
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        home = home_of(user)
        try:
            pdf_id = service.save_asset(home, pdf_bytes, file.filename, title=title,
                                        tags=tag_list, url_source=url_source,
                                        bibliography=bibliography)
        except assets.DuplicateBibKeyError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except assets.BibtexError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # any user-supplied tag becomes an approved bubble immediately
        if tag_list:
            service.register_user_tags(home, tag_list)
        background_tasks.add_task(tagger.run_ingest, home, pdf_id)
        return {"pdf_id": pdf_id, "attention_flag": service.get_asset(home, pdf_id).get("attention_flag")}

    @app.post("/api/assets/upload-url")
    def upload_asset_url(
        body: AssetUrlIn,
        background_tasks: BackgroundTasks,
        user: str = Depends(current_user),
    ):
        url = body.url.strip()
        if not url:
            raise HTTPException(status_code=400, detail="No URL provided.")
        if not body.title.strip():
            raise HTTPException(status_code=400, detail="A title is required.")
        tag_list = [t.strip() for t in body.tags.split(",") if t.strip()]
        home = home_of(user)
        try:
            pdf_id = service.fetch_and_save_asset(home, url, title=body.title, tags=tag_list,
                                                   bibliography=body.bibliography)
        except assets.DuplicateBibKeyError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except assets.BibtexError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Couldn't fetch that link: {e}")
        # any user-supplied tag becomes an approved bubble immediately
        if tag_list:
            service.register_user_tags(home, tag_list)
        background_tasks.add_task(tagger.run_ingest, home, pdf_id)
        return {"pdf_id": pdf_id, "attention_flag": service.get_asset(home, pdf_id).get("attention_flag")}

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

    @app.put("/api/assets/{pdf_id}/bibliography")
    def put_asset_bibliography(pdf_id: str, body: AssetBibtexIn,
                               user: str = Depends(current_user)):
        try:
            meta = service.update_asset_bibliography(home_of(user), pdf_id, body.bibliography)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="No such asset.")
        except assets.DuplicateBibKeyError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except assets.BibtexError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"meta": meta}

    @app.post("/api/bibtex/preview")
    def preview_bibtex(body: BibtexPreviewIn, user: str = Depends(current_user)):
        try:
            return service.preview_bibtex(home_of(user), body.bibliography)
        except assets.BibtexError as e:
            return {"entries": [], "warning": str(e)}

    @app.delete("/api/assets/{pdf_id}")
    def del_asset(pdf_id: str, user: str = Depends(current_user)):
        if not service.delete_asset(home_of(user), pdf_id):
            raise HTTPException(status_code=404, detail="No such asset.")
        return {"ok": True}

    @app.get("/api/assets/{pdf_id}/summary")
    def get_summary(pdf_id: str, user: str = Depends(current_user)):
        return {"summary": service.asset_summary(home_of(user), pdf_id)}

    @app.post("/api/assets/{pdf_id}/resummarize")
    def resummarize_asset(pdf_id: str, user: str = Depends(current_user)):
        try:
            return {"summary": service.resummarize_asset(home_of(user), pdf_id)}
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="No such asset.")
        except service.ModelUnavailableError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Could not re-summarize this paper: {e}")

    @app.get("/api/assets/{pdf_id}/pdf")
    def get_pdf(pdf_id: str, user: str = Depends(current_user)):
        p = service.asset_pdf_path(home_of(user), pdf_id)
        if not p.exists():
            raise HTTPException(status_code=404, detail="No such PDF.")
        return FileResponse(p, media_type="application/pdf",
                            headers={"Content-Disposition": "inline"})

    # ---- installed Scientist client -------------------------------------------------
    @app.post("/api/scientist/v2/device")
    def scientist_device_start(body: ScientistDeviceIn, _version: None = Depends(scientist_client_version)):
        code = secrets.token_urlsafe(18)
        _SCIENTIST_DEVICES[code] = {"expires": time.time() + 600,
                                    "client_name": body.client_name[:120], "user": "", "token": ""}
        return {"device_code": code,
                "verification_uri": f"/api/scientist/v2/device/{code}",
                "expires_in": 600, "interval": 2}

    @app.get("/api/scientist/v2/device/{code}")
    def scientist_device_page(code: str):
        rec = _SCIENTIST_DEVICES.get(code)
        if not rec or rec["expires"] < time.time():
            raise HTTPException(status_code=404, detail="This device authorization expired.")
        # Device authorization may be opened in a browser with no session yet.  Route through
        # the normal SPA sign-in screen, retaining the code so boot() can approve it after login.
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=f"/?scientist_device={code}", status_code=303)

    @app.post("/api/scientist/v2/device/{code}/approve")
    def scientist_device_approve(code: str, user: str = Depends(current_user)):
        rec = _SCIENTIST_DEVICES.get(code)
        if not rec or rec["expires"] < time.time():
            raise HTTPException(status_code=404, detail="This device authorization expired.")
        rec["user"] = user
        rec["token"] = auth.new_scientist_token(user, rec["client_name"])
        from fastapi.responses import HTMLResponse
        return HTMLResponse("<p>LockedIn Scientist authorized. You may return to your terminal.</p>")

    @app.get("/api/scientist/v2/device/{code}/token")
    def scientist_device_token(code: str, _version: None = Depends(scientist_client_version)):
        rec = _SCIENTIST_DEVICES.get(code)
        if not rec or rec["expires"] < time.time():
            raise HTTPException(status_code=404, detail="This device authorization expired.")
        if not rec["token"]:
            return {"status": "pending"}
        token, user = rec["token"], rec["user"]
        _SCIENTIST_DEVICES.pop(code, None)
        return {"status": "authorized", "token": token, "user": user}

    @app.get("/api/scientist/v2/bubbles/{slug}/manifest")
    def scientist_manifest(slug: str, user: str = Depends(scientist_user)):
        try:
            return scientist_sync.manifest(home_of(user), slug)
        except KeyError:
            raise HTTPException(status_code=404, detail="No such approved bubble.")

    @app.post("/api/scientist/v2/bubbles/{slug}/files")
    def scientist_files(slug: str, body: ScientistFilesIn, user: str = Depends(scientist_user)):
        # Bound a request so a malformed client cannot turn this into an unbounded payload.
        if len(body.paths) > 500:
            raise HTTPException(status_code=400, detail="Request at most 500 files at once.")
        try:
            return scientist_sync.read_files(home_of(user), slug, body.paths)
        except KeyError:
            raise HTTPException(status_code=404, detail="No such approved bubble.")

    @app.get("/api/scientist/v2/bubbles")
    def scientist_bubbles(user: str = Depends(scientist_user)):
        """Small preflight inventory for the installed project-local client."""
        return {"bubbles": [{"slug": b["slug"], "name": b.get("name") or b["slug"],
                             "last_edited_at": b.get("last_edited_at") or ""}
                            for b in service.list_bubbles(home_of(user))]}

    @app.get("/api/scientist/v2/workspaces")
    def scientist_workspaces(user: str = Depends(scientist_user)):
        rec = auth.load_accounts().get(user, {})
        return {"workspaces": workspaces.list_for_user(user),
                "personal_workspace_id": rec.get("personal_workspace_id", "")}

    @app.get("/api/scientist/v2/guide")
    def scientist_guide(user: str = Depends(scientist_user)):
        """Canonical report-editing conventions for the generated project-local skill."""
        from . import reports
        return {"guide": reports.guide_section("Editing Guide"),
                "math_macros": service.load_math_config(home_of(user)).get("macros", {})}

    @app.post("/api/scientist/v2/bubbles/{slug}/push")
    def scientist_push(slug: str, body: ScientistPushIn, user: str = Depends(scientist_user)):
        return scientist_sync.apply_writes(home_of(user), slug, body.writes)

    @app.post("/api/scientist/v2/bubbles/{slug}/deletes")
    def scientist_delete(slug: str, body: ScientistDeleteIn, user: str = Depends(scientist_user)):
        """Remove report pages/figures a Scientist session deleted, manifest entry included."""
        return scientist_sync.apply_deletes(home_of(user), slug, body.deletes)

    @app.post("/api/scientist/v2/bubbles/{slug}/pages")
    def scientist_create_page(slug: str, body: ScientistPageCreateIn, user: str = Depends(scientist_user)):
        if body.bubble and body.bubble != slug:
            raise HTTPException(status_code=400, detail="Bubble path and body disagree.")
        return scientist_sync.register_page(home_of(user), slug, body.page_slug,
                                            body.content_b64, body.base_revision)

    # ---- bubbles ----
    @app.get("/api/bubbles")
    def list_bubbles(archived: bool = False, user: str = Depends(current_user)):
        return {"bubbles": service.list_bubbles(home_of(user), archived=archived)}

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

    @app.patch("/api/bubbles/{slug}/archive")
    def archive_bubble(slug: str, body: BubbleArchiveIn, user: str = Depends(current_user)):
        try:
            return {"bubble": service.set_bubble_archived(home_of(user), slug, body.archived)}
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))

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

    @app.patch("/api/bubbles/{slug}/papers/{pdf_id}")
    def set_pdf_bubble_score(slug: str, pdf_id: str, body: PaperScoreIn,
                             user: str = Depends(current_user)):
        try:
            return {"bubble": service.set_pdf_bubble_score(home_of(user), slug, pdf_id, body.score)}
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="No such asset.")
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/bubbles/{slug}/migrate-papers")
    def migrate_papers(slug: str, body: MigratePapersIn, user: str = Depends(current_user)):
        """Copy papers from ``body.source`` into ``slug`` (the destination) in one request."""
        try:
            return service.migrate_papers(home_of(user), body.source, slug, body.items)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="No such asset.")
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/api/bubbles/{slug}")
    def delete_bubble(slug: str, user: str = Depends(current_user)):
        service.delete_bubble(home_of(user), slug)
        return {"ok": True}

    @app.post("/api/bubbles/{slug}/share")
    def set_share(slug: str, body: ShareIn, user: str = Depends(current_user)):
        """Toggle the bubble's unlisted public share link (stable token)."""
        return service.set_bubble_share(home_of(user), slug, body.active)

    @app.put("/api/bubbles/{slug}/overleaf")
    def set_bubble_overleaf(slug: str, body: OverleafIn, user: str = Depends(current_user)):
        try:
            return {"bubble": service.set_bubble_overleaf(home_of(user), slug, body.project)}
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/api/bubbles/{slug}/overleaf")
    def clear_bubble_overleaf(slug: str, user: str = Depends(current_user)):
        try:
            return {"bubble": service.set_bubble_overleaf(home_of(user), slug, None)}
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))

    # ---- todos (global per-user; referenced from report pages as @<id>) ----
    @app.get("/api/todos")
    def list_todos(user: str = Depends(current_user)):
        return {"todos": service.list_todos(home_of(user))}

    @app.post("/api/todos")
    def create_todo(body: TodoIn, user: str = Depends(current_user)):
        return {"todo": service.add_todo(home_of(user), body.title, body.note)}

    @app.get("/api/todos/{tid}")
    def get_todo(tid: int, user: str = Depends(current_user)):
        todo = service.get_todo(home_of(user), tid)
        if todo is None:
            raise HTTPException(status_code=404, detail="No such TODO.")
        return {"todo": todo}

    @app.patch("/api/todos/{tid}")
    def update_todo(tid: int, body: TodoUpdateIn, user: str = Depends(current_user)):
        try:
            return {"todo": service.update_todo(home_of(user), tid,
                                                **body.model_dump(exclude_none=True))}
        except KeyError:
            raise HTTPException(status_code=404, detail="No such TODO.")

    @app.delete("/api/todos/{tid}")
    def delete_todo(tid: int, user: str = Depends(current_user)):
        try:
            return {"deleted": service.delete_todo(home_of(user), tid)}
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))

    # ---- pages (per-bubble mini-wiki) ----
    @app.get("/api/bubbles/{slug}/pages/{page}")
    def get_page(slug: str, page: str, user: str = Depends(current_user)):
        return {"content": service.get_page(home_of(user), slug, page)}

    @app.get("/api/bubbles/{slug}/refs")
    def get_refs(slug: str, user: str = Depends(current_user)):
        """Bubble-wide equation/theorem reference registry for the live editor preview.

        The SPA renders one page at a time but numbers/links references across the whole bubble,
        so it fetches this map (eq labels, thm labels, per-page theorem-counter offsets) and
        re-fetches it after saves and page add/remove. Mirrors the server-side _build_refs the
        preview/share HTML uses, so the editor preview and the standalone pages stay identical.
        """
        home = home_of(user)
        return _bubble_refs(home, slug, service.list_pages(home, slug))

    @app.get("/api/bubbles/{slug}/preview/{page}")
    def preview_page(slug: str, page: str, user: str = Depends(current_user)):
        """Serve a standalone HTML page — full-screen rendered preview with working intra-bubble links."""
        from fastapi.responses import HTMLResponse

        home = home_of(user)
        all_pages = service.list_pages(home, slug)
        visible_pages = _visible_pages(all_pages)
        if not any(p["page_slug"] == page for p in visible_pages):
            raise HTTPException(status_code=404, detail="No such page.")
        html = _render_preview_html(
            name=service.bubble_detail(home, slug)["name"], page=page,
            all_pages=visible_pages, content=service.get_page(home, slug, page),
            slug=slug, link_base=f"/api/bubbles/{slug}/preview",
            asset_base=f"/api/bubbles/{slug}/assets", show_back=True,
            workspace_id=active_workspace_id(user),
            todos={t["id"]: t for t in service.list_todos(home)},
            todo_link_base="/#todos",  # owner is logged in → link opens the SPA TODO manager
            macros=service.load_math_config(home).get("macros", {}),
            refs=_bubble_refs(home, slug, visible_pages),
            themes=service.load_aesthetics_config(home)["themes"])
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

    # ---- private review comments (never used by public preview/share routes) ----
    @app.get("/api/bubbles/{slug}/pages/{page}/comments")
    def get_comments(slug: str, page: str, user: str = Depends(current_user)):
        return service.list_comments(home_of(user), slug, page)

    @app.post("/api/bubbles/{slug}/pages/{page}/comments")
    def post_comment(slug: str, page: str, body: CommentCreateIn, user: str = Depends(current_user)):
        try:
            return {"thread": service.create_comment(home_of(user), slug, page, user, body.body, body.anchor)}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/bubbles/{slug}/pages/{page}/comments/{thread_id}/replies")
    def post_comment_reply(slug: str, page: str, thread_id: str, body: CommentReplyIn,
                           user: str = Depends(current_user)):
        try:
            return {"message": service.reply_comment(home_of(user), slug, page, thread_id, user, body.body)}
        except KeyError:
            raise HTTPException(status_code=404, detail="No such review thread.")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.patch("/api/bubbles/{slug}/pages/{page}/comments/{thread_id}/messages/{message_id}")
    def patch_comment_message(slug: str, page: str, thread_id: str, message_id: str,
                              body: CommentEditIn, user: str = Depends(current_user)):
        try:
            return {"message": service.edit_comment_message(home_of(user), slug, page, thread_id, message_id, user, body.body)}
        except KeyError:
            raise HTTPException(status_code=404, detail="No such review message.")
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.patch("/api/bubbles/{slug}/pages/{page}/comments/{thread_id}")
    def patch_comment_status(slug: str, page: str, thread_id: str, body: CommentStatusIn,
                             user: str = Depends(current_user)):
        try:
            return {"thread": service.set_comment_status(home_of(user), slug, page, thread_id, body.status, user)}
        except KeyError:
            raise HTTPException(status_code=404, detail="No such review thread.")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/api/bubbles/{slug}/pages/{page}/comments/{thread_id}")
    def del_comment(slug: str, page: str, thread_id: str, user: str = Depends(current_user)):
        if not service.delete_comment(home_of(user), slug, page, thread_id):
            raise HTTPException(status_code=404, detail="No such review thread.")
        return {"ok": True}

    @app.patch("/api/bubbles/{slug}/pages/{page}")
    def patch_page(slug: str, page: str, body: PageRenameIn, user: str = Depends(current_user)):
        service.rename_page(home_of(user), slug, page, body.title)
        return {"ok": True}

    @app.patch("/api/bubbles/{slug}/pages/{page}/hidden")
    def patch_page_hidden(slug: str, page: str, body: PageHiddenIn,
                          user: str = Depends(current_user)):
        try:
            service.set_page_hidden(home_of(user), slug, page, body.hidden)
        except KeyError:
            raise HTTPException(status_code=404, detail="No such page.")
        return {"ok": True, "hidden": body.hidden}

    @app.post("/api/bubbles/{slug}/pages/order")
    def reorder_pages(slug: str, body: PageOrderIn, user: str = Depends(current_user)):
        try:
            service.reorder_pages(home_of(user), slug, body.page_slugs)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
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

    @app.get("/api/bubbles/{slug}/assets")
    def list_bubble_assets(slug: str, user: str = Depends(current_user)):
        return {"assets": service.list_bubble_assets(home_of(user), slug)}

    @app.get("/api/bubbles/{slug}/assets/{filename}/text")
    def get_bubble_text_asset(slug: str, filename: str, user: str = Depends(current_user)):
        try:
            return {"filename": filename, "text": service.bubble_text_asset(home_of(user), slug, filename)}
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="No such asset.")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/api/bubbles/{slug}/assets/{filename}")
    def delete_bubble_asset(slug: str, filename: str, user: str = Depends(current_user)):
        if not service.delete_bubble_asset(home_of(user), slug, filename):
            raise HTTPException(status_code=404, detail="No such asset.")
        return {"ok": True}

    @app.get("/api/bubbles/{slug}/assets/{filename}")
    def get_bubble_image(slug: str, filename: str, user: str = Depends(current_user)):
        safe = Path(filename).name
        if safe != filename or not safe:
            raise HTTPException(status_code=400, detail="Bad filename.")
        p = service.bubble_asset_path(home_of(user), slug, safe)
        if not p.exists():
            raise HTTPException(status_code=404, detail="No such image.")
        # These assets belong to an authenticated user's private workspace.  Never let a CDN
        # cache a response keyed only by URL: it can be stale and can bypass the auth boundary.
        return FileResponse(p, headers={"Content-Disposition": "inline",
                                        "Cache-Control": "private, no-store"})

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
