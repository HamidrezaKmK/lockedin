/* Chalk talks — dated slide decks the agent writes, and the five marks you leave on them.
 *
 * Served as its own file for the same reason lightbox.js is: the SPA and any server-rendered
 * surface must not grow two drifting copies of this. It owns an overlay rather than a pane, so
 * it can be added to the bubble without reaching into the tab/pane machinery.
 *
 * The one load-bearing subtlety is anchoring. You select *rendered* text, but a note must
 * anchor to the *markdown source* — that is the only address the agent shares with you. So a
 * selection is mapped back to a source substring (tolerating the emphasis characters markdown
 * eats) before it is ever sent; if it cannot be mapped, the mark is refused rather than stored
 * against text that does not exist.
 */
(function () {
  "use strict";

  const KINDS = {
    bad:  { icon: "mark-bad",  label: "wrong",   color: "var(--bad)" },
    q:    { icon: "mark-q",    label: "unclear", color: "var(--warn)" },
    more: { icon: "mark-more", label: "deeper",  color: "var(--accent2)" },
    good: { icon: "mark-good", label: "good",    color: "var(--good)" },
    cut:  { icon: "mark-cut",  label: "cut",     color: "var(--muted)" },
    // Created by the draw tool, not the picker: the drawing is the message.
    ink:  { icon: "mark-ink",  label: "drawn",   color: "var(--text-color-4, #ff7aa2)" },
  };
  /* The five verdicts are drawn marks, not text glyphs: `✗ ✓ ✂ ✍` picked up emoji presentation
     on several platforms and changed shape between phone and desktop. Each KIND now carries an
     icon name, and `glyph` is the rendered <svg> so every existing template literal is unchanged. */
  for (const k in KINDS) {
    Object.defineProperty(KINDS[k], "glyph", {
      get() { return window.LIIcon ? LIIcon.html(this.icon, "mark") : ""; },
    });
  }
  const ORDER = ["bad", "q", "more", "good", "cut"];
  /** Icon markup for use inside this file's template literals. */
  const LI_IC = (name, cls) => (window.LIIcon ? LIIcon.html(name, cls) : "");

  const S = { slug: null, name: "", owner: "", workspaceId: "", view: "home", talk: null, data: null, bubble: null,
              slide: 0, kind: "q", pending: null, editPremise: false, suppressRoute: false,
              edit: false, editorObj: null, notes: true, user: "", syncState: "synced",
              syncTimer: null, syncBusy: false };
  let root = null;

  const api = async (path, opts = {}) => {
    // Chalk talks live in the active workspace just like pages do. Without this header the
    // browser falls back to each account's Personal workspace, so collaborators see different
    // copies of the same-looking talk and replies appear to vanish between accounts.
    const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    if (S.workspaceId) headers["X-LockedIn-Workspace"] = S.workspaceId;
    const r = await fetch(path, { credentials: "include", ...opts, headers });
    if (!r.ok) throw new Error((await r.text()) || r.status);
    return r.json();
  };
  /** Same job as index.html's workspaceUrl, for the URLs the browser fetches by itself.
   *  An <img src> or an <a href> cannot carry the X-LockedIn-Workspace header that api() sets,
   *  so those requests fall back to the viewer's Personal workspace — and 404, or quietly serve
   *  the wrong workspace's copy. Anything the browser resolves on its own goes through here. */
  const wsUrl = path => !S.workspaceId ? path
    : path + (path.includes("?") ? "&" : "?") + "workspace=" + encodeURIComponent(S.workspaceId);
  const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const h = (html) => { const d = document.createElement("div"); d.innerHTML = html; return d; };
  const displayAuthor = author => {
    const name = String(author || "");
    // "you" exists only in old seeded data. It means the workspace owner, not the person
    // currently reading a shared workspace.
    const owner = S.owner || M.owner || "";
    if (name === "you") return owner || name;
    if (name === "agent on behalf of you") return owner ? "agent on behalf of " + owner : name;
    return name;
  };

  /* ------------------------------------------------------------------ styles */
  function injectStyles() {
    if (document.getElementById("talks-css")) return;
    const st = document.createElement("style");
    st.id = "talks-css";
    st.textContent = `
/* Height from --vvh, not from inset:0. A fixed element resolves inset against the *layout*
   viewport, which on iOS Safari is taller than what you can actually see — the address and tab
   bars sit over the bottom of it. The slide pager and the six tools live at the very bottom of
   this column, so on an iPad they ended up underneath Safari's own chrome and could not be
   reached at all. --vvh tracks visualViewport.height and is already kept current on resize.
   Over-constrained top/bottom/height means bottom is ignored, which is what we want here. */
.tk-overlay{position:fixed;inset:0;height:var(--vvh,100%);z-index:900;background:var(--bg);
  display:flex;flex-direction:column;
  font-family:var(--font-ui);letter-spacing:.005em}
.tk-overlay.tk-inline{position:static;inset:auto;z-index:auto;height:100%;min-height:0;
  background:transparent;border-radius:var(--radius);overflow:hidden}
.tk-overlay.tk-inline .tk-top{background:transparent;border-bottom:0;padding:0 4px 4px;min-height:0}
.tk-overlay.tk-inline .tk-list{padding:4px 4px 30px}
.tk-top{display:flex;align-items:center;gap:11px;padding:9px 18px;border-bottom:1px solid var(--line);
  background:var(--panel);flex:0 0 auto;min-height:52px}
.tk-crumb{font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tk-crumb .dim{color:var(--muted);font-weight:400}
.tk-crumb .back{color:var(--accent);cursor:pointer}
.tk-sp{flex:1}
.tk-overlay button:where(:not(.tk-edithost *)){font:inherit;cursor:pointer;border:1px solid var(--line-strong);background:var(--panel2);
  color:var(--ink);border-radius:9px;padding:6px 11px;min-height:34px}
.tk-overlay button:where(:not(.tk-edithost *)):hover{border-color:var(--accent);background:var(--panel)}
.tk-overlay button.pri:where(:not(.tk-edithost *)){background:var(--accent);color:var(--on-accent);border-color:var(--accent);font-weight:600}
.tk-body{flex:1;min-height:0;display:flex;flex-direction:column}

.tk-list{flex:1;overflow:auto;padding:20px 30px 40px;display:flex;flex-direction:column;gap:22px}
.tk-dim{font-size:12px;color:var(--muted)}
.tk-premise{border:1px solid color-mix(in srgb,var(--accent) 40%,var(--line));border-radius:12px;
  background:linear-gradient(180deg,color-mix(in srgb,var(--accent) 9%,var(--panel)),var(--panel));
  padding:18px 20px;box-shadow:var(--shadow-sm)}
.tk-premise.tk-unset{background:var(--panel);border-style:dashed}
.tk-premise.tk-unset>p{max-width:68ch;margin-top:0}
.tk-premise-top{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.tk-premise-top .tk-byline{margin:0;flex:1;min-width:0;font-size:12px;color:var(--muted)}
.tk-premise-top button{padding:3px 10px;min-height:0;font-size:11.5px}
.tk-premise .tk-abstract{font-family:var(--font-reading);font-size:16px;line-height:1.6;
  color:var(--ink);overflow:visible}
.tk-premise .tk-abstract p:last-child,.tk-goal .tk-md p:last-child{margin-bottom:0}
/* The goal is a different kind of sentence from the abstract — a commitment rather than a
   description — so it is coloured rather than merely indented. */
.tk-goal .tk-md{font-family:var(--font-reading);font-size:15.5px;line-height:1.55;overflow:visible;
  color:var(--accent2)}
.tk-hint{font-size:11.5px;color:var(--muted);margin:14px 0 6px}
.tk-hint code{font-family:var(--font-mono);background:var(--panel2);padding:1px 5px;border-radius:5px}
.tk-preview{border:1px solid var(--line);border-radius:9px;background:var(--panel2);padding:11px 13px;
  font-family:var(--font-reading);font-size:14.5px;line-height:1.6;max-height:190px;overflow:auto}
.tk-goal{margin-top:12px;padding-top:11px;border-top:1px dashed var(--line);display:flex;gap:10px;
  font-family:var(--font-reading);font-size:15.5px}
.tk-goal b{font-size:14px;line-height:1.5;padding-top:2px;flex:0 0 auto}
.tk-byline{margin-top:11px;font-size:12px;color:var(--muted);display:flex;align-items:center;gap:9px}
.tk-byline button{padding:3px 9px;min-height:0;font-size:11.5px}
.tk-pages{display:flex;gap:10px;overflow-x:auto;overflow-y:hidden;padding-bottom:6px;
  scroll-snap-type:x proximity;scrollbar-width:thin;
  /* Twelve pages overflow this strip, and the last visible card was sliced flat against the
     container edge — it read as a clipping bug rather than "there is more this way". */
  -webkit-mask-image:linear-gradient(90deg,#000 calc(100% - 40px),transparent 100%);
  mask-image:linear-gradient(90deg,#000 calc(100% - 40px),transparent 100%)}
.tk-pg{flex:0 0 208px;height:88px;border:1px solid var(--line-strong);border-radius:10px;
  background:var(--panel);padding:12px 13px;cursor:pointer;scroll-snap-align:start;
  display:flex;flex-direction:column;overflow:hidden}
.tk-pg:hover{border-color:var(--accent)}
.tk-pg .t{font-weight:600;font-size:13.5px;margin-bottom:4px;line-height:1.3;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.tk-pg .m{font-size:11.5px;color:var(--muted);margin-top:auto;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tk-pg .tk-pgmark{color:var(--warn);font-weight:600}
.tk-talks{display:flex;flex-direction:column;gap:11px}
.tk-lab{display:block;font:500 10.5px var(--font-ui);letter-spacing:.18em;text-transform:uppercase;
  color:var(--muted);margin:14px 0 6px}
.tk-ta{width:100%;background:var(--panel2);border:1px solid var(--line-strong);border-radius:9px;
  color:var(--ink);font:inherit;font-size:14px;padding:9px;resize:vertical;outline:none;
  font-family:var(--font-reading)}
.tk-ta:focus{border-color:var(--accent)}
.tk-out{margin:0;background:var(--panel2);border:1px solid var(--line-strong);border-radius:9px;
  padding:12px 13px;font-family:var(--font-mono);font-size:12.5px;line-height:1.6;
  white-space:pre-wrap;color:var(--ink);max-height:190px;overflow:auto}
:root{--tk-caret:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6.2 14.6 12 8.8l5.8 5.8'/%3E%3C/svg%3E")}
/* The bubble home is prose and lists, not an editor, so it takes the same column cap as every
   other view. The prose inside it has no measure of its own: a card whose text stops half way
   across it reads as broken, so the text fills the card and the card is what is bounded. */
.tk-list{max-width:var(--content-max,1400px)}
.tk-band{display:flex;align-items:center;gap:10px;margin-bottom:4px;flex-wrap:wrap}
.tk-band .tk-addwrap{margin-left:auto}
@media (max-width:700px){
  /* Four things — eyebrow, rule, count, action — do not fit on one 358px line without the
     button riding over the rule. The eyebrow and its rule keep the first line; the action takes
     the second, full width, where it is also an easier target. */
  .tk-band{row-gap:10px}
  .tk-band .rule{flex:1 1 24px;min-width:24px}
  .tk-band .tk-addwrap{flex:1 0 100%}
  .tk-band .tk-addtalk{width:100%;justify-content:center;min-height:40px}
  /* With the action wrapped onto its own line the band is now two rows tall, so it needs a real
     gap under it — otherwise the button and the first card below read as one control. */
  .tk-band{margin-bottom:12px}
  /* The page strip: 88px of card for two short lines left a hollow middle on a phone. */
  .tk-pg{flex:0 0 190px;height:auto;min-height:74px}
  .tk-pg .m{margin-top:6px}
  .tk-list{gap:26px}
}
.tk-root .li-ic,.tk-slide .li-ic,.tk-note .li-ic{width:1.05em;height:1.05em;flex:0 0 auto;
  stroke:currentColor;fill:none;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round;
  vertical-align:-.18em}
.tk-root .li-ic.mark,.tk-slide .li-ic.mark{stroke-width:2.1}
.tk-x .li-ic,.tk-del .li-ic{width:15px;height:15px;vertical-align:-.2em}
.tk-band b{font:500 10.5px var(--font-ui);letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}
.tk-band .rule{flex:1;height:1px;background:var(--line)}
.tk-card{display:flex;gap:15px;border:1px solid var(--line-strong);border-radius:11px;background:var(--panel);
  padding:13px 15px;cursor:pointer}
.tk-card:hover{border-color:var(--accent);background:var(--panel2)}
.tk-card{position:relative}
.tk-card .tk-del{position:absolute;top:9px;right:10px;padding:1px 8px;min-height:0;font-size:12px;
  color:var(--muted);border-color:transparent;background:none;opacity:0;transition:opacity .15s}
.tk-card:hover .tk-del{opacity:1}
.tk-card .tk-del:hover{color:var(--bad);border-color:var(--bad);background:none}
.tk-card .d{flex:0 0 88px;font:500 12px var(--font-mono);color:var(--muted);padding-top:2px}
.tk-card .t{font-weight:600;font-size:15px;margin-bottom:4px}
.tk-card .i{font-family:var(--font-reading);font-size:14px;color:var(--muted);line-height:1.5}
.tk-meta{display:flex;gap:7px;margin-top:9px;font-size:11.5px;color:var(--muted);flex-wrap:wrap}
.tk-tag{border:1px solid var(--line-strong);border-radius:999px;padding:2px 8px}
.tk-tag.open{border-color:color-mix(in srgb,var(--warn) 60%,var(--line));color:var(--warn);
  background:color-mix(in srgb,var(--warn) 12%,transparent)}
.tk-tag.done{border-color:color-mix(in srgb,var(--good) 50%,var(--line));color:var(--good)}
.tk-empty{border:1px dashed var(--line-strong);border-radius:11px;padding:18px;color:var(--muted);
  font-family:var(--font-reading);font-size:15px;line-height:1.6}
.tk-empty>*{max-width:68ch}
.tk-empty p{margin:0 0 .85em}
.tk-empty p:last-child{margin-bottom:0}

.tk-stage{flex:1;min-height:0;display:flex}
.tk-col{flex:1;min-width:0;display:flex;flex-direction:column;padding:18px 24px 10px;overflow:auto}
/* margin:auto, not justify-content:center — auto margins collapse to zero when the slide
   overflows, so a tall slide scrolls from its top instead of clipping it. */
/* flex-grow, not min-height. A short slide grows to fill whatever height the frame actually
   has — a full canvas to draw on, sized by the screen rather than by a constant — and there is
   nothing to overflow, which is what sank the fixed floor: it pushed small frames into forced
   scrolling. A slide taller than the frame is unchanged: grow only distributes *free* space,
   so the column scrolls it exactly as before. */
.tk-slide{width:100%;max-width:720px;margin:auto;background:var(--panel);border:1px solid var(--line-strong);
  border-radius:14px;box-shadow:var(--shadow);padding:34px 40px 32px;position:relative;
  flex:0 0 auto;min-height:min(56vh,520px);display:flex;flex-direction:column}
.tk-slide .tk-md{flex:1}
.tk-slide .kind{position:absolute;top:-10px;left:22px;font:500 9.5px var(--font-mono);
  letter-spacing:.14em;text-transform:uppercase;background:var(--accent);color:var(--on-accent);
  padding:3px 9px;border-radius:999px}
.tk-stamp{position:absolute;top:-10px;right:18px;display:flex;gap:6px}
.tk-stamp span{font:500 10px var(--font-mono);background:var(--panel2);border:1px solid var(--line);
  color:var(--muted);padding:3px 9px;border-radius:999px}
.tk-slide h2{font-size:20px;font-weight:600;letter-spacing:-.015em;margin:2px 0 8px;line-height:1.3}
.tk-slide .sub{font-family:var(--font-reading);color:var(--muted);font-size:14px;line-height:1.5;
  margin-bottom:24px}
.tk-md{position:relative;overflow:visible;font-family:var(--font-reading);
  font-size:15.5px;line-height:1.65}
.tk-md p{margin:0 0 15px}
.tk-md ol,.tk-md ul{margin:0 0 15px;padding-left:28px}
.tk-md li{margin-bottom:11px;padding-left:4px}
.tk-md img{max-width:100%;border-radius:9px}
.tk-md code{font-family:var(--font-mono);font-size:.88em;background:var(--panel2);padding:1px 5px;
  border-radius:5px}
.tk-cites{white-space:nowrap}
.tk-cite{color:var(--accent2);text-decoration:none;border-bottom:1px dotted var(--accent2)}
.tk-cite:hover{color:var(--ink);border-bottom-color:var(--ink)}
.tk-cite.unresolved{color:var(--warn);border-bottom:1px dotted var(--warn);cursor:help}
.tk-md .tk-math{white-space:normal}
.tk-md .katex-display{margin:10px 0;overflow-x:auto;overflow-y:hidden}
.tk-md blockquote{border-left:3px solid var(--accent2);margin:0 0 15px;padding:10px 16px;
  background:color-mix(in srgb,var(--accent2) 8%,transparent);border-radius:0 9px 9px 0}
/* The provisional mark, held while the composer is open. */
/* A pending mark appears in both a slide (.tk-md) and the regular document preview, so this
   cannot be scoped to .tk-md — otherwise the browser's default bright-yellow <mark> leaks in. */
mark.tk-pending{background:color-mix(in srgb,var(--accent) 34%,transparent);color:inherit;
  border-radius:3px;padding:0 1px;box-shadow:0 0 0 1px color-mix(in srgb,var(--accent) 62%,transparent)}
.tk-pending-region{position:absolute;border:1.5px dashed var(--accent);border-radius:7px;
  background:color-mix(in srgb,var(--accent) 9%,transparent);pointer-events:none;
  animation:tk-pulse 1.6s ease-in-out infinite}
@keyframes tk-pulse{0%,100%{opacity:1}50%{opacity:.55}}
mark.tk-anno{background:color-mix(in srgb,var(--kc,var(--accent)) 26%,transparent);color:inherit;
  border-radius:3px;padding:0 1px;box-shadow:0 1px 0 var(--kc,var(--accent));cursor:pointer}
/* Outline only. A filled box over prose competes with the prose, and two overlapping fills
   are unreadable — the border carries the colour and the text underneath stays legible. */
.tk-region{position:absolute;border:1.5px solid var(--kc);border-radius:7px;background:none;
  cursor:pointer}
.tk-region:hover{background:color-mix(in srgb,var(--kc) 8%,transparent)}
/* Inside the box, not hanging off it: a badge at top:-11px is clipped the moment the region
   sits near the top of the slide or against the scrolling edge. */
.tk-region b{position:absolute;top:-1px;left:-1px;font:600 9.5px var(--font-mono);background:var(--kc);
  color:var(--bg);border-radius:7px 0 7px 0;padding:1px 5px;line-height:1.5;pointer-events:none}
/* Overlapping regions must stay individually readable, so the fill stays faint and the border
   carries the colour. The focused one lifts above the rest. */
.tk-region.focus{background:color-mix(in srgb,var(--kc) 12%,transparent);z-index:2}
/* Armed region mode. The dashed inset outlines exactly what can be boxed — the whole slide,
   not just its body — because "I hit mark region and cannot see what I may select" is the
   confusing part, not the drag itself. No tint: the slide has to stay readable while you aim. */
.tk-draw{position:absolute;inset:0;cursor:crosshair;z-index:4;border-radius:14px;
  outline:1.5px dashed color-mix(in srgb,var(--accent) 70%,transparent);outline-offset:-7px;
  touch-action:none}
/* While a draw layer is up the finger is a pen. touch-action stops the browser scrolling, but
   iOS long-press still selects the text *underneath* the layer and pops its Copy/Look Up
   callout mid-stroke; selection has to be switched off for the duration. The whole page, not
   just the overlay: on an iPad the second finger or the heel of the hand lands on the topbar or
   the notes pane just as often, and a selection blooming anywhere is the same interruption.
   :has() keys it to the layer's existence, so there is no mode flag to forget to clear. */
body:has(.tk-inkdraw),body:has(.tk-draw){
  -webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
.tk-draw::after{content:"drag a box over any part of this slide \u00b7 Esc to cancel";
  position:absolute;left:50%;bottom:9px;transform:translateX(-50%);font:500 11px var(--font-ui);
  letter-spacing:.02em;background:var(--accent);color:var(--on-accent);padding:4px 12px;border-radius:999px;
  white-space:nowrap;pointer-events:none;box-shadow:var(--shadow-sm)}
.tk-drawbox{position:absolute;border:1.5px solid var(--accent);border-radius:7px;
  background:color-mix(in srgb,var(--accent) 10%,transparent)}

/* Freehand ink. The stored strokes repaint on a canvas glued over the slide; it never
   takes pointer events, so text selection and the other marks keep working beneath it. */
.tk-ink{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:3}
.tk-inkdraw{position:absolute;inset:0;width:100%;height:100%;cursor:crosshair;z-index:6;
  border-radius:14px;outline:1.5px dashed color-mix(in srgb,var(--kc,#ff7aa2) 70%,transparent);
  outline-offset:-7px;touch-action:none}
.tk-inkbar{position:absolute;left:50%;bottom:9px;transform:translateX(-50%);z-index:7;
  display:flex;align-items:center;gap:8px;background:var(--panel2);border:1px solid var(--line);
  border-radius:999px;padding:5px 8px 5px 14px;box-shadow:var(--shadow);white-space:nowrap;
  font:500 11.5px var(--font-ui)}
.tk-inkbar b{color:var(--muted);font-weight:500}
.tk-inkbar button{padding:3px 10px;min-height:0;font-size:11.5px;border-radius:999px}
.tk-slide.tk-capturing .tk-ink{display:none}
.tk-slide.tk-capturing .tk-ink.tk-capture-target{display:block}

/* Manual editing. The editor card takes the slide's place and its full height: editing is
   a mode, not a popup, and the deck footer keeps working so you can restructure the whole
   talk without leaving it. */
.tk-editcard{width:100%;max-width:720px;margin:0 auto;flex:1;min-height:0;display:flex;flex-direction:column;
  background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);
  overflow:hidden;position:relative}
.tk-edithead{display:flex;align-items:center;gap:9px;padding:11px 14px;border-bottom:1px solid var(--line);
  background:color-mix(in srgb,var(--panel2) 70%,var(--panel))}
.tk-edithead .tk-cnt{margin-left:0}
input.tk-ekind{width:152px;flex:0 1 152px;font:500 9.5px var(--font-mono);letter-spacing:.14em;
  text-transform:uppercase;background:var(--accent);color:var(--on-accent);padding:3px 10px;
  border-radius:999px;border:0;cursor:text;appearance:none;outline:none;
  transition:none;box-shadow:none}
input.tk-ekind::placeholder{color:color-mix(in srgb,var(--bg) 58%,transparent)}
.tk-edithost{flex:1;min-height:0}
.tk-edithost .toastui-editor-defaultUI{border:0;border-radius:0;height:100%}
.tk-editnote{font-size:11.5px;color:var(--muted);padding:7px 14px;border-top:1px solid var(--line)}
.tk-editnote code{font-family:var(--font-mono);background:var(--panel2);padding:1px 5px;border-radius:5px}
.tk-danger:hover{border-color:var(--bad);color:var(--bad)}
/* The slide tools are one instrument, so they get one body — the same pill grammar as the
   presence cluster: glyph-only segments, dividers as borders, words in the tooltips. */
.tk-seg{display:inline-flex;align-items:stretch;border:1px solid var(--line-strong);border-radius:999px;
  overflow:hidden;background:var(--panel2)}
.tk-seg button{border:0;border-radius:0;background:none;min-height:34px;padding:6px 14px;
  font-size:15px;line-height:1;white-space:nowrap}
.tk-seg button+button{border-left:1px solid var(--line)}
.tk-seg button:hover{border-color:var(--line);background:color-mix(in srgb,var(--accent) 13%,transparent)}
.tk-seg button.tk-danger:hover{background:color-mix(in srgb,var(--bad) 13%,transparent);color:var(--bad);
  border-left-color:var(--line)}
.tk-notestoggle.off{color:var(--muted)}
.tk-notestoggle{flex:0 0 auto}
/* Notes off = clean reading: the pane goes, and so does every kind of ink on the slide.
   Inline marks keep their text and only lose their paint — display:none would eat words. */
.tk-overlay.no-notes .tk-gutter{display:none}
.tk-overlay.no-notes .tk-region,.tk-overlay.no-notes .tk-ink{display:none}
.tk-overlay.no-notes mark.tk-anno{background:none;box-shadow:none;cursor:inherit}
.tk-addwrap{position:relative}
.tk-addmenu{position:absolute;right:0;top:calc(100% + 6px);z-index:30;min-width:250px;
  background:var(--panel2);border:1px solid var(--line);border-radius:11px;box-shadow:var(--shadow);
  padding:6px;display:flex;flex-direction:column;gap:4px}
/* display:flex would silently defeat the hidden attribute — the menu was always open. */
.tk-addmenu[hidden]{display:none}
.tk-addmenu button{display:flex;flex-direction:column;align-items:flex-start;gap:2px;text-align:left;
  border:0;background:none;padding:9px 11px;border-radius:8px}
.tk-addmenu button:hover{background:color-mix(in srgb,var(--accent) 14%,transparent);border:0}
.tk-addmenu button span{font-size:11.5px;color:var(--muted)}

/* Wrap rather than crush. The row carries a pager and a six-segment instrument; how much space
   that has is set by the *column*, not the window, so it runs out at whatever width the notes
   gutter happens to leave — an iPad in portrait is the worst case, where a 344px side gutter
   leaves the pill 81px to hold 262px of buttons and four tools end up outside the column
   entirely. flex:0 0 auto stops the instrument being shrunk below its contents; wrapping gives
   it its own line the moment it no longer fits, at any width. */
.tk-foot{display:flex;flex-wrap:wrap;align-items:center;gap:11px;padding:16px 4px 4px;width:100%;
  max-width:720px;margin:0 auto;flex:0 0 auto}
.tk-foot .tk-seg{flex:0 0 auto}
.tk-overlay .tk-fab{display:none}
/* Shown while the bubble's own content is in flight. The shell arrives before the idea, the
   document and the talks do, and a half-drawn page reads as a broken one — so the space they
   will occupy says it is working instead of sitting empty. */
.tk-loading{padding:22px 4px;display:flex;flex-direction:column;gap:16px}
.tk-loading .tk-lrow{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:13px}
.tk-spin{width:14px;height:14px;flex:none;border-radius:50%;border:2px solid var(--line);
  border-top-color:var(--accent);animation:tk-spin .7s linear infinite}
@keyframes tk-spin{to{transform:rotate(360deg)}}
.tk-skel{border:1px solid var(--line);border-radius:12px;background:var(--panel);
  padding:18px;display:flex;flex-direction:column;gap:11px}
.tk-skel i{display:block;height:10px;border-radius:999px;background:var(--line);
  animation:tk-pulse 1.4s ease-in-out infinite}
.tk-skel i:nth-child(2){width:72%;animation-delay:.15s}
.tk-skel i:nth-child(3){width:45%;animation-delay:.3s}
@keyframes tk-pulse{0%,100%{opacity:.45}50%{opacity:.9}}
@media(prefers-reduced-motion:reduce){.tk-spin,.tk-skel i{animation:none}}
.tk-dots{display:flex;gap:6px;align-items:center}
.tk-overlay .tk-dot{width:9px;height:9px;border-radius:50%;background:var(--line);cursor:pointer;
  border:0;padding:0;min-height:0;flex:0 0 auto}
.tk-overlay .tk-dot:hover{background:var(--muted)}
.tk-overlay .tk-dot.on{background:var(--accent);width:26px;border-radius:999px}
.tk-overlay .tk-dot.note{background:var(--warn)}
/* nowrap + no shrink: as a flex item this had been compressed to 11px, which broke "2 / 4"
   across three lines and made the pager twice as tall as the buttons beside it. */
.tk-cnt{font:500 11px var(--font-mono);color:var(--muted);margin-left:auto;
  white-space:nowrap;flex:0 0 auto}
.tk-resync{color:var(--good);border-color:color-mix(in srgb,var(--good) 45%,var(--line))!important;
  font-size:20px!important;line-height:1!important;padding:5px 10px!important;min-width:42px}
.tk-resync.stale{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 65%,var(--line))!important;
  background:color-mix(in srgb,var(--warn) 10%,transparent)}
.tk-resync.checking{color:var(--warn);animation:tk-resync-spin .8s linear infinite}
@keyframes tk-resync-spin{to{transform:rotate(360deg)}}

.tk-gutter{flex:0 0 344px;border-left:1px solid var(--line);overflow:auto;padding:0 18px 28px;
  display:flex;flex-direction:column;gap:18px;background:color-mix(in srgb,var(--panel) 55%,var(--bg))}
/* A full-width band at the very top of the pane, not a floating label under a void — the
   negative margins pull it over the gutter's padding so it reads as the pane's own header. */
.tk-gh{font:500 10.5px var(--font-ui);letter-spacing:.14em;text-transform:uppercase;color:var(--muted);
  display:flex;align-items:center;gap:8px;padding:14px 18px 12px;margin:0 -18px 6px;
  background:color-mix(in srgb,var(--panel2) 55%,transparent);
  border-bottom:1px solid color-mix(in srgb,var(--line) 80%,transparent)}
.tk-gh .tk-tag{font-size:10px;letter-spacing:.04em;padding:2px 9px}
.tk-note{flex:0 0 auto;border:1px solid var(--line);border-left:4px solid var(--kc);border-radius:11px;
  background:var(--panel);padding:18px 18px 20px;cursor:pointer;overflow:hidden;min-width:0}
.tk-note:hover{border-color:var(--accent);border-left-color:var(--kc)}
.tk-note.orphan{border-style:dashed}
.tk-note .hd{display:flex;align-items:center;gap:7px;margin-bottom:14px;font-size:11.5px;color:var(--muted)}
.tk-badge{font:500 10.5px var(--font-mono);padding:2px 7px;border-radius:999px;letter-spacing:.02em;
  background:color-mix(in srgb,var(--kc) 20%,transparent);color:var(--kc)}
.tk-note .qt{font-family:var(--font-reading);font-size:12.5px;color:var(--muted);
  border-left:2px solid var(--line);padding-left:11px;margin-bottom:16px;font-style:italic;
  overflow-wrap:anywhere;word-break:break-word;hyphens:none;min-width:0}
.tk-turn+.tk-turn{margin-top:16px}
.tk-turn .tk-who{font:500 11px var(--font-ui);color:var(--muted);margin-bottom:5px}
.tk-turn.agent .tk-who{color:var(--accent)}
.tk-turn .tk-said{font-size:13.5px;line-height:1.5;overflow-wrap:anywhere;word-break:break-word;
  min-width:0}
.tk-turn.agent .tk-said{color:var(--muted);border-left:2px solid
  color-mix(in srgb,var(--accent) 45%,transparent);padding-left:10px}
.tk-note .tx{font-size:13.5px;line-height:1.45;overflow-wrap:anywhere;word-break:break-word}
.tk-note .tk-id{margin-left:auto;font:400 10px var(--font-mono);opacity:.55}
.tk-note .tk-dim{opacity:.5;font-style:italic}
.tk-note .tk-acts{display:flex;gap:8px;margin-top:18px}
.tk-note .tk-acts button{padding:3px 10px;min-height:0;font-size:11.5px}
.tk-editta{width:100%;min-height:64px;background:var(--panel2);border:1px solid var(--accent);
  border-radius:8px;color:var(--ink);font:inherit;font-size:13.5px;padding:8px;resize:vertical;
  outline:none;font-family:var(--font-reading)}
.tk-note .tk-shot{display:block;width:100%;margin-top:16px;border:1px solid var(--line);
  border-radius:8px;cursor:zoom-in}
/* Capture styling: the picture must show the raw slide and exactly one mark. Earlier marks are
   removed outright, not dimmed — a snapshot carrying three overlapping boxes cannot tell an
   agent which one the comment beside it refers to. */
.tk-slide.tk-capturing .tk-anno{background:transparent;box-shadow:none;outline:none}
.tk-slide.tk-capturing .tk-region{display:none}
.tk-slide.tk-capturing .tk-region.tk-capture-target{display:block;opacity:1}
.tk-slide.tk-capturing .tk-anno.tk-capture-target{
  background:color-mix(in srgb,var(--kc) 42%,transparent);box-shadow:0 2px 0 var(--kc);
  outline:2px solid var(--kc);outline-offset:1px;border-radius:3px}

.tk-note .rp{margin-top:9px;padding-top:8px;border-top:1px dashed var(--line);font-size:12.5px;
  color:var(--muted)}
.tk-note .rp b{color:var(--accent);font-size:11px;letter-spacing:.04em}

.tk-pop{position:fixed;border:1px solid var(--accent);border-radius:12px;background:var(--panel2);
  box-shadow:var(--shadow);padding:11px;width:316px;z-index:960}
.tk-kinds{display:flex;gap:6px;margin-bottom:9px}
.tk-kb{flex:1;border:1px solid var(--line-strong);border-radius:9px;background:var(--panel);padding:7px 0 6px;
  text-align:center;cursor:pointer;min-height:0}
.tk-kb .g{font-size:17px;line-height:1.05}
.tk-kb .l{font-size:9.5px;color:var(--muted);margin-top:2px}
.tk-kb.sel{border-color:var(--kc);background:color-mix(in srgb,var(--kc) 16%,var(--panel))}
.tk-kb.sel .l{color:var(--kc)}
.tk-pop .qt{font-family:var(--font-reading);font-size:12.5px;color:var(--muted);
  border-left:2px solid var(--accent);padding-left:8px;margin:0 0 9px;font-style:italic;
  max-height:46px;overflow:hidden;overflow-wrap:anywhere;word-break:break-word}
.tk-pop textarea{width:100%;height:54px;background:var(--panel);border:1px solid var(--line-strong);
  border-radius:9px;color:var(--ink);font:inherit;font-size:13px;padding:8px;resize:none;outline:none}
.tk-pop .row{display:flex;gap:8px;margin-top:9px;align-items:center}
.tk-pop .hint{font-size:11px;color:var(--muted);flex:1}

.tk-sheet{flex:1;overflow:auto;padding:20px 26px 30px}
.tk-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
.tk-mini{aspect-ratio:16/10;border:1px solid var(--line);border-radius:10px;background:var(--panel);
  padding:11px 12px;position:relative;overflow:hidden;cursor:pointer}
.tk-mini:hover,.tk-mini.cur{border-color:var(--accent)}
.tk-mini .n{font:500 10px var(--font-mono);color:var(--muted)}
.tk-mini .t{font-weight:600;font-size:13px;margin:5px 0 7px;line-height:1.3}
.tk-mini .s{font-family:var(--font-reading);font-size:11.5px;color:var(--muted);line-height:1.35}
.tk-mini .nb{position:absolute;right:9px;bottom:8px;display:flex;gap:4px}
.tk-mini .nb i{font:500 10px var(--font-mono);font-style:normal;padding:2px 6px;border-radius:999px;
  background:color-mix(in srgb,var(--kc) 22%,transparent);color:var(--kc)}

.tk-modal{position:fixed;inset:0;background:rgba(4,6,11,.66);z-index:970;display:flex;
  align-items:center;justify-content:center;padding:30px}
.tk-mcard{width:min(780px,100%);max-height:86vh;overflow:auto;background:var(--panel);
  border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);padding:20px 22px;
  position:relative}
.tk-mcard h3{margin:0 0 4px;font-size:17px;font-weight:600}
.tk-mcard .msub{color:var(--muted);font-size:13px;margin-bottom:16px}
.tk-x{position:absolute;top:14px;right:16px}
.tk-json{font-family:var(--font-mono);font-size:12px;line-height:1.55;white-space:pre-wrap;
  background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:14px;
  max-height:60vh;overflow:auto;color:var(--ink)}
.tk-jsonwrap{position:relative}
.tk-jsonwrap .tk-json{margin:0;padding-top:40px}
.tk-json-copy{position:absolute;top:8px;right:8px;z-index:1;min-height:25px!important;padding:3px 8px!important;
  font-size:11px!important;line-height:1!important}

@media(max-width:900px){
  /* The notes pane is a right-edge drawer on a phone, not a bottom sheet. A slide is tall and
     narrow here, so width is the cheaper thing to spend, and the collapsed handle stops sitting
     on top of the pager and the draw button. Opening moves the transform, which the compositor
     carries on its own thread; the old sheet animated max-height, which it cannot, and every
     open juddered. */
  .tk-overlay{--tk-handle:44px}
  /* Absolute inside the stage rather than fixed to the viewport, so the drawer begins under the
     talk's own header instead of covering its title and ⋮ menu. No magic offset to keep in step
     with the header's height. */
  .tk-stage{position:relative}
  .tk-gutter{position:absolute;top:0;right:0;bottom:0;left:auto;flex:none;
    width:min(86vw,340px);max-height:none;gap:14px;
    border-left:1px solid var(--accent);border-top:0;border-radius:16px 0 0 16px;
    background:var(--panel);z-index:20;
    padding:calc(13px + env(safe-area-inset-top)) calc(13px + env(safe-area-inset-right))
            calc(13px + env(safe-area-inset-bottom)) calc(var(--tk-handle) + 10px);
    overflow:hidden;overscroll-behavior:contain;
    box-shadow:-20px 0 44px -26px rgba(0,0,0,.9);
    transform:translateX(calc(100% - var(--tk-handle)));
    transition:transform .24s cubic-bezier(.22,.61,.36,1)}
  .tk-overlay.notes-open .tk-gutter{transform:translateX(0);overflow-y:auto}
  .tk-overlay:not(.notes-open) .tk-note,.tk-overlay:not(.notes-open) .tk-empty-notes{display:none}
  /* The header becomes the drawer's spine: a full-height grab strip down the edge your thumb
     meets first. Vertical writing rather than a rotated box, so it is still a normal flex
     container and the tag and caret keep laying themselves out.
     touch-action:none so a sideways drag on the spine is ours, and not the browser scrolling
     the deck underneath it. */
  .tk-gh{position:absolute;left:0;top:0;bottom:0;width:var(--tk-handle);box-sizing:border-box;
    margin:0;padding:calc(16px + env(safe-area-inset-top)) 0 calc(16px + env(safe-area-inset-bottom));
    gap:12px;writing-mode:vertical-rl;justify-content:flex-start;
    border-bottom:0;border-right:1px solid color-mix(in srgb,var(--line) 80%,transparent);
    border-radius:16px 0 0 16px;cursor:grab;z-index:3;
    touch-action:none;-webkit-user-select:none;user-select:none}
  .tk-gh:active{cursor:grabbing}
  /* Points the way it wants to be pulled, and turns to face the way out once it is open. */
  .tk-gh::after{content:"";margin:0;width:15px;height:15px;flex:0 0 auto;
    background:currentColor;color:var(--accent);transition:transform .2s ease;
    transform:rotate(-90deg);
    -webkit-mask:var(--tk-caret) center/contain no-repeat;mask:var(--tk-caret) center/contain no-repeat}
  .tk-overlay.notes-open .tk-gh::after{transform:rotate(90deg)}
  /* The inset for the spine sits on the slide, not on the column, so that hiding the pane
     widens the slide without also resizing the footer underneath it. The footer keeps one
     width in both states: a pager that reflows when you toggle the pane moves the very button
     you just pressed out from under your thumb. */
  .tk-col{padding:12px 12px 10px 12px}
  /* Width, not a right margin: the slide is width:100%, so a margin pushes it under the spine
     instead of pulling its edge in. */
  .tk-slide{width:calc(100% - var(--tk-handle) + 2px);margin-left:0;margin-right:auto}
  .tk-overlay.no-notes .tk-slide{width:100%}
  .tk-pop{width:calc(100vw - 24px);left:12px!important}
  .tk-list{padding:14px 13px 30px}
  /* 40px of inset on a 358px card leaves about thirty characters a line. */
  .tk-slide{padding:26px 20px 22px;border-radius:12px}
  .tk-slide .kind{left:16px}
  /* Editing on a phone. With the keyboard up the visible height is ~508px, and of the column
     the editor was getting 39px of it — the syntax hint below it took 89 and the pager another
     98, so there was more chrome explaining the editor than there was editor. Drop the hint
     (it is guidance you need once, and it stays on larger screens) and drop the slide dots and
     the counter, which leaves the arrows, resync and "stop editing" on one row instead of two.
     That is ~140px back, and the typing area goes from 39px to something you can write in. */
  .tk-editing .tk-editnote{display:none}
  .tk-editing .tk-foot .tk-dots,
  .tk-editing .tk-foot .tk-cnt{display:none}
  .tk-edithost{min-height:150px}
}
/* Ink strokes and region boxes are stored as percentages of the slide box, so they ride the
   box rather than the words: the same slide at another width reflows, and a circle drawn around
   a term lands in the middle of a different sentence. A snapshot is taken at the moment of
   drawing and is the only faithful record of it, so that is where a drawing lives — on every
   screen, not just the narrow ones. Keeping it live on a desktop only meant the misalignment
   was subtler there. Text marks are quote-anchored, not box-anchored, so they reflow correctly
   and stay on the slide.
   The :not(.tk-capturing) guard keeps this clear of the capture path, which deliberately
   reveals exactly one overlay while html2canvas reads the slide. */
.tk-slide:not(.tk-capturing) .tk-ink,
.tk-slide:not(.tk-capturing) .tk-region{display:none}
/* Phones. The slide tools are six segments and the pager is another four controls; on one row
   that is ~390px of content in ~270px of space, so the pill was rendering 86px wide with 262px
   of buttons inside it and four of the six were simply off-screen — unreachable, not just ugly.
   Wrap the row and give the instrument its own full-width line, segments sharing the width. */
@media(max-width:560px){
  /* Right padding clears the FAB's column so the → pager button is never underneath it. */
  /* One row, not two. Four controls and the slide dots share what is left after the column
     reserved for the pencil and the spine, so the buttons are tighter and the dots are the
     thing that gives: they shrink and scroll rather than pushing the row onto a second line.
     The height that buys goes back to the slide, which is what people are here to read. */
  .tk-foot{flex-wrap:nowrap;gap:6px;padding:8px calc(var(--tk-handle) + 58px) 2px 2px}
  .tk-foot>button{padding:5px 8px;min-height:32px}
  .tk-foot .tk-cnt{margin-left:4px}
  /* On phones the six tools left the footer: as a second wrapped row they slid behind the
     notes sheet. They live behind a floating button now — the pill becomes a vertical popover
     above it, same buttons, same handlers, just summoned instead of resident. */
  .tk-foot .tk-seg{display:none;position:fixed;right:14px;bottom:132px;z-index:22;
    flex-direction:column;border-radius:14px;box-shadow:var(--shadow)}
  .tk-overlay.tools-open .tk-foot .tk-seg{display:flex}
  .tk-foot .tk-seg button{padding:0 14px;min-height:46px;font-size:17px}
  .tk-foot .tk-seg button+button{border-left:0;border-top:1px solid var(--line)}
  /* .tk-overlay .tk-fab, for specificity: the overlay's generic button rule (class+element)
     out-ranked a lone class and repainted the FAB as one more grey square. */
  .tk-overlay .tk-fab{display:flex;position:fixed;right:calc(var(--tk-handle) + 13px);bottom:22px;z-index:22;
    width:46px;height:46px;border-radius:50%;align-items:center;justify-content:center;
    min-height:0;padding:0;background:var(--accent);border:1px solid var(--accent);
    color:var(--bg);font-size:19px;box-shadow:var(--shadow)}
  .tk-overlay.tools-open .tk-fab{background:var(--panel2);color:var(--ink);border-color:var(--line)}
  /* Out of the way when something bigger is happening: the sheet expanded, or a draw layer up
     (its corner is drawing surface, not chrome). */
  .tk-overlay.notes-open .tk-fab,.tk-overlay.notes-open .tk-foot .tk-seg,
  body:has(.tk-inkdraw) .tk-fab,body:has(.tk-draw) .tk-overlay .tk-fab{display:none}
  .tk-dots{flex:1 1 auto;min-width:0;flex-wrap:nowrap;overflow:hidden;gap:5px}
  .tk-overlay .tk-dot{width:7px;height:7px}
  .tk-overlay .tk-dot.on{width:20px}
  .tk-resync{min-width:34px}
}`;
    document.head.append(st);
  }

  /* ------------------------------------------------------------- snapshotting */
  // A mark is stored with a picture of the slide as the reviewer saw it. The quote says which
  // words; the picture says what the slide *looked* like — where the figure sat, what was
  // beside what — which is the one thing a text anchor can never carry, and the only thing
  // that makes a region mark on a plot mean anything at all.
  //
  // The capture happens *after* the note is saved and the deck re-rendered, so what gets drawn
  // is exactly what the stored anchor resolves to rather than a transient browser selection.
  const H2C = "https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js";
  let h2cLoading = null;
  function loadCapture() {
    if (window.html2canvas) return Promise.resolve(window.html2canvas);
    if (h2cLoading) return h2cLoading;
    h2cLoading = new Promise((res, rej) => {
      const sc = document.createElement("script");
      sc.src = H2C;
      sc.onload = () => res(window.html2canvas);
      sc.onerror = () => rej(new Error("capture library unavailable"));
      document.head.append(sc);
    });
    return h2cLoading;
  }

  // The theme is built on color-mix(), which Chrome resolves to `color(srgb r g b)` — a
  // notation html2canvas cannot parse, so a capture of an unmodified lockedin surface throws.
  // Resolve those to plain rgba() inline for the duration of the shot, then put it all back.
  const COLOR_FN = /color\((?:srgb|display-p3)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*\/\s*([\d.]+))?\)/g;
  const COLOR_PROPS = ["color", "backgroundColor", "backgroundImage", "borderTopColor",
    "borderRightColor", "borderBottomColor", "borderLeftColor", "outlineColor", "boxShadow",
    "textDecorationColor", "caretColor", "columnRuleColor", "fill", "stroke"];

  function flattenColors(rootEl) {
    const undo = [];
    const conv = v => v.replace(COLOR_FN, (m, r, g, b, a) =>
      `rgba(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)}, ${a === undefined ? 1 : a})`);
    const walk = el => {
      const cs = getComputedStyle(el);
      COLOR_PROPS.forEach(prop => {
        const val = cs[prop];
        if (!val || val.indexOf("color(") < 0) return;
        undo.push([el, prop, el.style[prop]]);
        el.style[prop] = conv(val);
      });
      for (const child of el.children) walk(child);
    };
    walk(rootEl);
    return () => undo.forEach(([el, prop, was]) => { el.style[prop] = was; });
  }

  async function captureNote(noteId) {
    const slide = root && root.querySelector(".tk-slide");
    if (!slide) return;
    let h2c;
    try { h2c = await loadCapture(); } catch (e) { return; }   // offline: the note still stands
    // Dim everything except this mark, so the picture answers "which one" without a caption.
    slide.classList.add("tk-capturing");
    const target = slide.querySelector(
      `.tk-anno[data-note="${noteId}"],.tk-region[data-note="${noteId}"],.tk-ink[data-note="${noteId}"]`);
    if (target) target.classList.add("tk-capture-target");
    // Ink is painted in a rAF after render. The first capture ever was slow enough (fetching
    // html2canvas) to hide the race; every later one could clone the DOM before the strokes
    // landed and ship a blank drawing. Flush a frame, then repaint the target outright.
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    if (target && target.classList.contains("tk-ink")) {
      const note = (S.talk && S.talk.notes || []).find(n => n.id === noteId);
      if (note && note.paths) paintInk(target, slide, note.paths);
    }
    const restore = flattenColors(slide);
    try {
      const canvas = await h2c(slide, {
        backgroundColor: getComputedStyle(document.body).getPropertyValue("--panel") || "#161b25",
        scale: Math.min(2, window.devicePixelRatio || 1),
        logging: false, useCORS: true,
      });
      await api(`/api/bubbles/${S.slug}/talks/${S.talk.talk.id}/notes/${noteId}/shot.png`,
                { method: "PUT", body: JSON.stringify({ image_b64: canvas.toDataURL("image/png") }) });
    } catch (e) {
      // A failed capture must never lose the mark — the note is already saved.
      console.warn("chalk talks: snapshot failed", e);
    } finally {
      restore();
      slide.classList.remove("tk-capturing");
      if (target) target.classList.remove("tk-capture-target");
    }
  }

  /* -------------------------------------------------- markdown + math render */
  // Math is stashed behind opaque tokens BEFORE marked.js runs, exactly as the SPA does for
  // report pages. Without this, markdown eats the underscores and backslashes inside a formula
  // ($w(\lambda)_{t}$ becomes italics) and KaTeX is handed rubble. Each formula is then
  // rendered into a span that remembers its own markdown source, which is what makes a
  // selection over rendered math translatable back into a quote the agent can find.
  const MATH_TOKEN = i => `@@LIMATH${i}@@`;

  // Citations work on a slide exactly as they do on a report page: \cite{key} renders as the
  // numbered marker the bubble already assigns that key, and clicking it opens the PDF. The
  // numbering comes from the bubble's shared reference map, so [3] means the same paper on a
  // slide as it does in the document — a slide that numbered its own sources independently
  // would be actively misleading.
  let REFS = null;
  async function loadRefs(slug) {
    try { REFS = await api(`/api/bubbles/${encodeURIComponent(slug)}/refs`); }
    catch (e) { REFS = null; }
  }
  function citationsToHtml(md) {
    return md.replace(/\\cite\{([^}]*)\}/g, (_, raw) => {
      const map = (REFS && REFS.citeMap) || {};
      const bib = (REFS && REFS.bibliography) || {};
      const parts = raw.split(",").map(k => k.trim()).filter(Boolean).map(k => {
        const n = Object.prototype.hasOwnProperty.call(map, k) ? map[k] : null;
        const pdf = (bib[k] || {}).pdf_id;
        const label = n === null ? "?" + esc(k) : String(n);
        // Unresolved keys stay visible as ?key rather than silently vanishing: a citation that
        // disappears is worse than one that is obviously broken.
        return pdf
          ? `<a class="tk-cite" href="${wsUrl(`/api/assets/${encodeURIComponent(pdf)}/pdf`)}" target="_blank"
               rel="noopener" title="${esc((bib[k] || {}).text || k)}">${label}</a>`
          : `<span class="tk-cite unresolved" title="${esc(k)}">${label}</span>`;
      });
      return `<span class="tk-cites">[${parts.join(", ")}]</span>`;
    });
  }

  function stashMath(md) {
    const found = [];
    const take = (src, display) => { found.push({ src, display }); return MATH_TOKEN(found.length - 1); };
    let out = md.replace(/\$\$([\s\S]+?)\$\$/g, (m, body) => take(m, true));
    out = out.replace(/(^|[^\\$])\$([^$\n]+?)\$/g, (m, pre, body) => pre + take("$" + body + "$", false));
    return { text: out, found };
  }

  // `assets/<file>` is the portable report syntax, and a slide is stored in the same bubble as
  // the pages — so a figure written the documented way must resolve here too. Without this the
  // browser asks for a path relative to the SPA route and gets a 404.
  function resolveAssetLinks(md) {
    const ws = (location.hash.match(/^#w\/([^/]+)/) || [])[1];
    const q = ws ? `?workspace=${encodeURIComponent(ws)}` : "";
    const url = file =>
      `/api/bubbles/${encodeURIComponent(S.slug)}/assets/${encodeURIComponent(file)}${q}`;
    // Matched on the link, not the caption: a caption containing `]` — which any LaTeX
    // interval does, `$\\lambda \\in [-10, 8]$` — defeats a caption-shaped pattern, and the
    // figure then silently 404s. This is exactly how a real agent wrote one.
    return String(md)
      .replace(/\]\(assets\/([^\s)]+)\)/g, (_, file) => `](${url(file)})`)
      // Agents write raw <img> too, especially when a caption carries maths. A figure that
      // silently 404s is worse than one that renders plainly, so cover both spellings.
      .replace(/(<img\b[^>]*?\bsrc=["'])assets\/([^"']+)(["'])/gi,
               (_, open, file, close) => open + url(file) + close);
  }

  // The same full-screen viewer the report pages use. `watch` takes a selector and delegates
  // from the document, so this is idempotent and survives every re-render.
  function watchFigures() {
    if (!window.LockedInLightbox) return;
    try { window.LockedInLightbox.watch(".tk-md"); } catch (e) { /* the viewer is optional */ }
  }

  function renderMarkdown(md, into) {
    // Image alt text becomes an HTML attribute, so a `$…$` inside it must not be stashed and
    // re-rendered as KaTeX — doing so injects markup into the attribute and breaks out of the
    // tag. The SPA guards report pages the same way; a slide caption with maths in it leaked
    // raw HTML onto the slide until it did too.
    const captions = [];
    const guarded = resolveAssetLinks(md).replace(/!\[([\s\S]*?)\](?=\()/g, (_, cap) => {
      captions.push(cap);
      return `![@@LICAP${captions.length - 1}@@]`;
    });
    // The shared editor's colour tool writes the same \textcolor wrapper pages use; render it
    // the same way. After the math stash, so a coloured formula keeps its placeholder intact.
    const { text: stashed, found } = stashMath(guarded);
    const text = stashed.replace(/\\textcolor\{(#[0-9a-fA-F]{3,8})\}\{([^{}]*)\}/g,
      (_, c, t) => `<span style="color:${c}">${t}</span>`);
    let html;
    try { html = window.marked ? window.marked.parse(text, { breaks: false }) : esc(text); }
    catch (e) { html = esc(text); }
    found.forEach((_, i) => {
      html = html.split(MATH_TOKEN(i)).join(`<span class="tk-math" data-i="${i}"></span>`);
    });
    html = citationsToHtml(html);
    captions.forEach((cap, i) => {
      html = html.split(`@@LICAP${i}@@`).join(esc(cap));
    });
    into.innerHTML = html;
    watchFigures();
    into.querySelectorAll(".tk-math").forEach(node => {
      const m = found[Number(node.dataset.i)];
      if (!m) return;
      // The span carries its own source, so anchoring can recover it from a selection.
      node.dataset.md = m.src;
      const body = m.display ? m.src.slice(2, -2) : m.src.slice(1, -1);
      if (!window.katex) { node.textContent = m.src; return; }
      try {
        node.innerHTML = window.katex.renderToString(body, {
          displayMode: m.display, throwOnError: false,
          macros: (window.S && window.S.mathMacros) || undefined,
        });
      } catch (e) { node.textContent = m.src; }
    });
  }

  /* --------------------------------------------------------------- anchoring */
  // Map a selection made over *rendered* text back to a substring of the markdown source.
  // Markdown eats emphasis characters, so an exact match often fails on text the reader sees
  // as continuous; allow those characters to reappear between any two selected characters.
  function findInSource(source, text) {
    const want = text.replace(/\s+/g, " ").trim();
    if (!want) return null;
    const direct = source.indexOf(want);
    if (direct >= 0) return want;
    const pat = want.split("").map(ch => {
      const c = ch.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      return ch === " " ? "[\\s]+" : c;
    }).join("[*_`~]*");
    const m = new RegExp(pat).exec(source);
    return m ? m[0] : null;
  }

  function inkColor() {
    const v = getComputedStyle(document.body).getPropertyValue("--text-color-4").trim();
    return v || "#ff7aa2";
  }
  function paintInk(cv, hostEl, paths) {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    cv.width = Math.max(1, Math.round(hostEl.clientWidth * dpr));
    cv.height = Math.max(1, Math.round(hostEl.clientHeight * dpr));
    const ctx = cv.getContext("2d");
    ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.strokeStyle = inkColor();
    ctx.lineWidth = Math.max(2, cv.width / 480);
    ctx.lineCap = ctx.lineJoin = "round";
    (paths || []).forEach(pts => {
      if (!pts || pts.length < 2) return;
      ctx.beginPath();
      pts.forEach((pt, i) => {
        const x = pt.x * cv.width / 100, y = pt.y * cv.height / 100;
        if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
      });
      ctx.stroke();
    });
  }

  function paintAnchors(mdEl, slide, notes) {
    const slideEl = (mdEl.closest && mdEl.closest(".tk-slide")) || mdEl;
    notes.forEach(n => {
      if (n.paths && n.paths.length) {
        const cv = document.createElement("canvas");
        cv.className = "tk-ink";
        cv.dataset.note = n.id;
        slideEl.appendChild(cv);
        // Sized after layout: clientWidth is 0 until the slide is actually in the document.
        requestAnimationFrame(() => paintInk(cv, slideEl, n.paths));
        return;
      }
      if (n.rect) {
        const d = document.createElement("div");
        d.className = "tk-region";
        d.dataset.note = n.id;
        d.style.cssText = `--kc:${KINDS[n.kind].color};left:${n.rect.x}%;top:${n.rect.y}%;` +
                          `width:${n.rect.w}%;height:${n.rect.h}%`;
        d.innerHTML = `<b>${KINDS[n.kind].glyph}</b>`;
        d.onclick = () => focusNote(n.id);
        slideEl.appendChild(d);
        return;
      }
      if (n.orphan || !n.quote) return;
      highlightQuote(mdEl, n.quote, n, n.occurrence || 1, id => focusNote(id));
    });
  }

  // Collapsed-whitespace view of a container's text, each character remembering its origin
  // node and offset, so any match maps straight back to (node, offset) pairs.
  function flattenText(mdEl) {
    const nodes = [], chars = [];
    const walker = document.createTreeWalker(mdEl, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) nodes.push(node);
    let pendingSpace = false, started = false;
    nodes.forEach((nd, ni) => {
      const text = nd.nodeValue || "";
      for (let i = 0; i < text.length; i++) {
        if (/\s/.test(text[i])) { pendingSpace = started; continue; }
        if (pendingSpace) { chars.push({ c: " ", ni: -1, i: -1 }); pendingSpace = false; }
        chars.push({ c: text[i], ni, i });
        started = true;
      }
    });
    return { nodes, chars, flat: chars.map(ch => ch.c).join("") };
  }

  // A regex that finds a *source* quote inside *rendered* text: markdown's emphasis and code
  // markers become optional (rendering may have eaten them — but `code_span_here` keeps its
  // underscores, so deleting them outright broke every code-span quote), whitespace collapses,
  // and an inline formula becomes a bounded wildcard bridging whatever KaTeX rendered it as.
  function quotePattern(quote) {
    const collapsed = String(quote).replace(/\s+/g, " ").trim();
    let pat = "";
    let i = 0;
    while (i < collapsed.length) {
      const ch = collapsed[i];
      if (ch === "$") {
        const close = collapsed.indexOf("$", i + 1);
        if (close > i) { pat += "[\\s\\S]{0,160}?"; i = close + 1; continue; }
      }
      const esc2 = ch.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      pat += /[*_`~]/.test(ch) ? esc2 + "?" : esc2;
      i += 1;
    }
    try { return new RegExp(pat, "g"); } catch (e) { return null; }
  }

  // Wrap the k-th rendered occurrence of a quote in <mark>, wherever its text lives. A quote
  // routinely crosses inline elements — code, bold, a citation — so every intersected text
  // segment gets its own mark.
  function highlightQuote(mdEl, quote, n, occurrence, onFocus) {
    const { nodes, chars, flat } = flattenText(mdEl);
    const re = quotePattern(quote);
    if (!re) return false;
    let m = null, k = 0;
    while ((m = re.exec(flat))) {
      k += 1;
      if (k >= (occurrence || 1)) break;
      if (m.index === re.lastIndex) re.lastIndex++;   // zero-width safety
    }
    if (!m) return false;
    let s = m.index, e = m.index + m[0].length - 1;
    while (s <= e && chars[s].ni < 0) s++;
    while (e >= s && chars[e].ni < 0) e--;
    if (s > e) return false;
    const start = chars[s], end = chars[e];
    for (let ni = start.ni; ni <= end.ni; ni++) {
      const nd = nodes[ni];
      const from = ni === start.ni ? start.i : 0;
      const to = ni === end.ni ? end.i + 1 : (nd.nodeValue || "").length;
      if (to <= from || !(nd.nodeValue || "").slice(from, to).trim()) continue;
      const r = document.createRange();
      r.setStart(nd, from);
      r.setEnd(nd, to);
      const mk = document.createElement("mark");
      mk.className = "tk-anno";
      mk.dataset.note = n.id;
      mk.style.setProperty("--kc", (KINDS[n.kind] || KINDS.q).color);
      try {
        r.surroundContents(mk);
      } catch (err) {
        // A second range may cross a mark already painted into the DOM. Extracting and putting
        // that fragment inside this mark splits the first mark around the overlap, preserving
        // both annotations instead of silently dropping the later one.
        try {
          mk.appendChild(r.extractContents());
          r.insertNode(mk);
        } catch (nestedErr) { continue; }
      }
      mk.onclick = ev => { ev.stopPropagation(); if (onFocus) onFocus(n.id); };
    }
    return true;
  }
  function focusNote(id) {
    const card = root.querySelector(`.tk-note[data-note="${id}"]`);
    if (!card) return;
    card.scrollIntoView({ block: "center", behavior: "smooth" });
    card.style.borderColor = "var(--accent)";
    setTimeout(() => (card.style.borderColor = ""), 900);
  }

  /* --------------------------------------------------------------- the mark card */
  // Deliberately one function for both surfaces. A mark on a slide and a mark on a page are the
  // same object to the person who left it — same author, same id, same five kinds, same right
  // to be reworded or withdrawn — so they must not drift into two lookalike cards.
  function noteCard(m) {
    const k = KINDS[m.kind] || KINDS.q;
    const msgs = m.messages || [];
    const lastIdx = msgs.length - 1;
    const turn = (msg, i) => `<div class="tk-turn${msg.agent ? " agent" : ""}">
      <div class="tk-who">${msg.agent ? LI_IC("agent") + " " : ""}${esc(displayAuthor(msg.author))}${
        msg.edited_at ? ' <span class="tk-dim">· edited</span>' : ""}</div>
      <div class="tk-said"${i === lastIdx && !msg.agent ? ' data-last="1"' : ""}>${esc(msg.body || "")}</div>
    </div>`;
    return `<div class="tk-note${m.orphan ? " orphan" : ""}" data-note="${esc(m.id)}"
        style="--kc:${k.color}">
      <div class="hd"><span class="tk-badge">${k.glyph} ${k.label}</span>
        <span class="tk-id" title="the id an agent sees">${esc(m.id)}</span></div>
      <div class="qt">${m.orphanNote || ""}${m.quote
        ? "\u201c" + esc(m.quote) + "\u201d"
        : (m.kind === "ink" ? LI_IC("mark-ink","mark") + " drawn on the slide"
                                            : LI_IC("marquee") + " region on the slide")}</div>
      ${msgs.length ? msgs.map(turn).join("")
        : `<div class="tk-turn"><div class="tk-said tk-dim" data-last="1">no comment</div></div>`}
      ${m.image ? `<img class="tk-shot" alt="the slide as you marked it" src="${esc(m.image)}">` : ""}
      <div class="tk-acts">
        <button data-reply="${esc(m.id)}">reply</button>
        ${(!msgs.length || !(msgs[lastIdx] || {}).agent)
          ? `<button data-edit="${esc(m.id)}">edit</button>` : ""}
        <button data-drop="${esc(m.id)}">remove</button>
      </div>
    </div>`;
  }

  // Editing is confined to your own last turn: rewriting anything the other side has already
  // answered would leave that answer replying to words that no longer exist.
  function wireCard(host, { onEdit, onDelete, onReply }) {
    host.querySelectorAll(".tk-shot").forEach(im => (im.onclick = e => {
      e.stopPropagation();
      if (window.LockedInLightbox) window.LockedInLightbox.open(im.src, im.alt);
      else window.open(im.src, "_blank", "noopener");
    }));
    host.querySelectorAll("[data-drop]").forEach(b => (b.onclick = e => {
      e.stopPropagation();
      if (onDelete) onDelete(b.dataset.drop);
    }));
    const compose = (card, initial, done) => {
      const acts = card.querySelector(".tk-acts");
      const ta = document.createElement("textarea");
      ta.className = "tk-editta";
      ta.value = initial;
      acts.before(ta);
      acts.innerHTML = '<button class="pri" data-save="1">Save</button>' +
                       '<button data-cancel="1">Cancel</button>';
      const leave = () => { if (ta.isConnected) ta.remove(); };
      acts.querySelector("[data-cancel]").onclick = ev => { ev.stopPropagation(); leave(); done(null); };
      acts.querySelector("[data-save]").onclick = ev => {
        ev.stopPropagation(); const v = ta.value.trim(); leave(); done(v);
      };
      ta.onkeydown = ev => { if (ev.key === "Escape") { ev.stopPropagation(); leave(); done(null); } };
      ta.focus();
    };
    host.querySelectorAll("[data-edit]").forEach(b => (b.onclick = e => {
      e.stopPropagation();
      const card = b.closest(".tk-note");
      if (card.querySelector("textarea")) return;
      const last = card.querySelector('[data-last="1"]');
      const was = last && !last.classList.contains("tk-dim") ? last.textContent : "";
      if (last) last.style.display = "none";
      compose(card, was, v => { if (onEdit) onEdit(v === null ? null : b.dataset.edit, v); });
    }));
    host.querySelectorAll("[data-reply]").forEach(b => (b.onclick = e => {
      e.stopPropagation();
      const card = b.closest(".tk-note");
      if (card.querySelector("textarea")) return;
      compose(card, "", v => { if (onReply) onReply(v === null ? null : b.dataset.reply, v); });
    }));
  }

  /* ------------------------------------------------------------------ views */
  function publishRoute() {
    if (!S.onRoute || S.suppressRoute) return;
    if ((S.view === "deck" || S.view === "sheet") && S.talk && S.talk.talk)
      S.onRoute({talkId: S.talk.talk.id, slide: S.slide, edit: !!S.edit});
    else if (S.view === "home") S.onRoute(null);
  }

  async function loadHome(updateRoute = true) {
    stopTalkSync();
    S.edit = false;
    const [b, t] = await Promise.all([
      api(`/api/bubbles/${encodeURIComponent(S.slug)}`),
      api(`/api/bubbles/${encodeURIComponent(S.slug)}/talks`),
    ]);
    S.bubble = b.bubble; S.data = t;
    S.name = S.bubble.name || S.slug;
    S.view = "home";
    render();
    if (updateRoute) publishRoute();
  }
  // `keepSlide` matters after pinning or removing a mark: reloading the deck must not throw the
  // reader back to slide 1, which is where they were emphatically not looking.
  async function loadTalk(id, keepSlide) {
    if (!REFS) await loadRefs(S.slug);
    S.talk = await api(`/api/bubbles/${encodeURIComponent(S.slug)}/talks/${encodeURIComponent(id)}`);
    S.syncState = "synced";
    if (!keepSlide) S.slide = 0;
    S.slide = Math.min(S.slide, Math.max(0, S.talk.slides.length - 1));
    S.view = "deck";
    render();
    startTalkSync();
  }

  function syncTitle() {
    if (S.syncState === "stale") return "Remote changes are ready — refresh this slide deck";
    if (S.syncState === "checking") return "Checking the remote deck…";
    return "In sync with the remote deck — refresh now";
  }
  function setTalkSync(state) {
    S.syncState = state;
    document.querySelectorAll("[data-resync]").forEach(button => {
      button.className = "tk-resync " + state;
      button.title = syncTitle();
      button.setAttribute("aria-label", syncTitle());
    });
  }
  function stopTalkSync() {
    if (S.syncTimer) clearInterval(S.syncTimer);
    S.syncTimer = null;
  }
  function startTalkSync() {
    stopTalkSync();
    S.syncTimer = setInterval(checkTalkSync, 5000);
  }
  async function checkTalkSync() {
    if (!S.talk || S.view !== "deck" || S.syncBusy) return;
    const id = S.talk.talk.id;
    const known = S.talk.revision;
    try {
      const remote = await api(`/api/bubbles/${encodeURIComponent(S.slug)}/talks/${encodeURIComponent(id)}`);
      if (!S.talk || S.talk.talk.id !== id || S.view !== "deck") return;
      if (remote.revision && known && remote.revision !== known) setTalkSync("stale");
    } catch (e) {
      // A transient polling failure must not pretend the local view is out of date.
    }
  }
  async function resyncTalk() {
    if (!S.talk || S.syncBusy) return;
    if (S.edit && !leaveEditOk()) return;
    const id = S.talk.talk.id;
    S.syncBusy = true;
    setTalkSync("checking");
    try {
      await loadTalk(id, true);
      toast("Slides refreshed — in sync with remote");
    } catch (e) {
      setTalkSync("stale");
      toast("Couldn’t refresh slides: " + e.message);
    } finally {
      S.syncBusy = false;
    }
  }
  const notesOn = i => (S.talk.notes || []).filter(n => n.slide === i);
  const openCount = () => (S.talk.notes || []).length;

  function renderHome() {
    const b = S.bubble || {};
    const pages = b.pages || [];
    const talks = (S.data.talks || []);
    const total = talks.reduce((a, t) => a + (t.open || 0), 0);
    const has = b.abstract || b.goal;
    const el = h(`<div class="tk-list">
      <div>
        <div class="tk-band"><b>The idea</b><div class="rule"></div></div>
        ${has ? `<div class="tk-premise">
            <div class="tk-premise-top">
              <span class="tk-byline">${LI_IC("agent")} the agent's understanding${b.premise_revised_at
                ? ", revised " + esc(b.premise_revised_at.slice(0, 10)) : ""}</span>
              <button data-editp="1">${LI_IC("pencil")} edit goals</button>
            </div>
            <div class="tk-md tk-abstract"></div>
            ${b.goal ? `<div class="tk-goal"><b title="Goal">${LI_IC("check-circle")}</b>
              <div class="tk-md tk-goalbody"></div></div>` : ""}
          </div>`
          : `<div class="tk-premise tk-unset">
            <p><b>Nothing states what this bubble is for yet.</b> One paragraph and a goal — the
            agent's own understanding of the work, kept short so it stays skimmable. Its being
            subtly wrong is the cheapest and highest-value thing you can catch.</p>
            <div class="tk-byline"><button class="pri" data-editp="1">Write it</button></div>
          </div>`}
      </div>
      <div>
        <div class="tk-band"><b>The document</b><div class="rule"></div>
          <span class="tk-dim">${pages.length} page${pages.length === 1 ? "" : "s"}</span></div>
        <div class="tk-pages">${pages.map(p => `<div class="tk-pg" data-page="${esc(p.page_slug)}">
          <div class="t">${esc(p.title || p.page_slug)}</div>
          <div class="m">${pageStats(p)}</div></div>`).join("")}</div>
      </div>
      <div>
        <div class="tk-band"><b>Chalk talks</b><div class="rule"></div>
          <span class="tk-dim">${total} open note${total === 1 ? "" : "s"}</span>
          <div class="tk-addwrap"><button class="tk-addtalk" data-newtalk="1">+ add chalk talk</button>
            <div class="tk-addmenu" hidden>
              <button data-manual="1">${LI_IC("pencil")} Write it yourself<span>opens a blank slide in the editor</span></button>
              <button data-auto="1">${LI_IC("agent")} Ask an agent<span>hands you a prompt to paste</span></button>
            </div></div></div>
        <div class="tk-talklist"></div>
      </div>
    </div>`).firstChild;
    // Rendered through the same pipeline as a slide, so math is stashed before marked.js can
    // eat the backslashes and is then typeset by KaTeX.
    const abs = el.querySelector(".tk-abstract");
    if (abs) renderMarkdown(b.abstract || "*No statement of the idea yet.*", abs);
    const goal = el.querySelector(".tk-goalbody");
    if (goal) renderMarkdown(b.goal || "", goal);
    el.querySelector(".tk-talklist").append(renderList());
    const nt = el.querySelector("[data-newtalk]");
    const menu = el.querySelector(".tk-addmenu");
    if (nt) nt.onclick = e => { e.stopPropagation(); menu.hidden = !menu.hidden; };
    if (menu) {
      if (!S.menuCloser) {   // one global closer, not one per render
        S.menuCloser = true;
        document.addEventListener("click", () =>
          document.querySelectorAll(".tk-addmenu").forEach(mn => (mn.hidden = true)));
      }
      menu.querySelector("[data-auto]").onclick = () => { menu.hidden = true; askForTalk(); };
      menu.querySelector("[data-manual]").onclick = () => { menu.hidden = true; startManualTalk(); };
    }
    const ep = el.querySelector("[data-editp]");
    if (ep) ep.onclick = () => editPremise();
    el.querySelectorAll(".tk-pg").forEach(p => (p.onclick = () => {
      if (S.onPage) S.onPage(p.dataset.page);
    }));
    return el;
  }

  // A talk you write yourself: one blank slide, straight into the editor. The registry title
  // follows the first slide's heading the moment you rename it by hand.
  async function startManualTalk() {
    const today = new Date().toISOString().slice(0, 10);
    const r = await api(`/api/bubbles/${encodeURIComponent(S.slug)}/talks`, {
      method: "POST",
      body: JSON.stringify({ title: "Untitled talk",
        body: `<!-- slide: kind=setup, date=${today}, v=1 -->\n# Untitled talk\n` }),
    });
    await loadTalk(r.id);
    S.edit = true;
    render();
  }

  // The agent is in another window, so the useful thing this button can do is hand you words to
  // paste. It asks for the two things only you know — the topic and any steer — and keeps the
  // format and the slide rules out of sight: those live in SKILL.md, which the agent has
  // already read. A prompt that recites the file format to someone who is not going to write
  // the file is not a prompt, it is a manual.
  function askForTalk() {
    const compose = (topic, notes) => {
      const t = topic.trim() || "<topic>";
      const n = notes.trim();
      return `Write me a chalk talk in the "${S.name}" bubble about ${t}.` +
             (n ? `\n\n${n}` : "") +
             `\n\nFollow the chalk-talk instructions in .lockedin/SKILL.md, and sync it when done.`;
    };
    const m = h(`<div class="tk-modal"><div class="tk-mcard">
      <button class="tk-x" data-x="1" aria-label="Close">${LI_IC("close")}</button>
      <h3>Ask an agent for a chalk talk</h3>
      <div class="msub">Say what you want explained. The agent already knows the format and the
        rules for a good slide — you do not need to repeat them.</div>
      <label class="tk-lab">What should it be about?</label>
      <input class="tk-ta" data-f="topic" placeholder="why the variance term doesn’t vanish">
      <label class="tk-lab">Anything to steer it? <span class="tk-dim">optional</span></label>
      <textarea class="tk-ta" data-f="notes" rows="3"
        placeholder="e.g. keep it to five slides · I mainly care about the λ &lt; −6 regime · assume I know the ELBO"></textarea>
      <label class="tk-lab">Paste this to your agent</label>
      <pre class="tk-out" data-out="1"></pre>
      <div class="row" style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
        <button data-x="1">Close</button><button class="pri" data-copy="1">Copy</button></div>
    </div></div>`).firstChild;
    const topic = m.querySelector('[data-f="topic"]');
    const notes = m.querySelector('[data-f="notes"]');
    const out = m.querySelector("[data-out]");
    const paint = () => { out.textContent = compose(topic.value, notes.value); };
    topic.oninput = notes.oninput = paint;
    paint();
    const shut = () => m.remove();
    m.onclick = e => { if (e.target === m) shut(); };
    m.querySelectorAll("[data-x]").forEach(x => (x.onclick = shut));
    m.querySelector("[data-copy]").onclick = async () => {
      try { await navigator.clipboard.writeText(out.textContent); }
      catch (e) {
        const sel = getSelection(), r = document.createRange();
        r.selectNodeContents(out); sel.removeAllRanges(); sel.addRange(r);
        document.execCommand("copy");
      }
      const b = m.querySelector("[data-copy]");
      b.textContent = "Copied";
      setTimeout(() => (b.textContent = "Copy"), 1400);
    };
    document.body.append(m);
    setTimeout(() => topic.focus(), 30);
  }

  function editPremise() {
    const b = S.bubble || {};
    const m = h(`<div class="tk-modal"><div class="tk-mcard">
      <button class="tk-x" data-x="1" aria-label="Close">${LI_IC("close")}</button>
      <h3>What is this bubble about?</h3>
      <div class="msub">One paragraph, and one line for the goal. Keep it short — this is the
        thing everyone reads first, including every agent that joins.</div>
      <label class="tk-lab">The idea</label>
      <textarea class="tk-ta" data-f="abstract" rows="5">${esc(b.abstract || "")}</textarea>
      <label class="tk-lab">Goal</label>
      <textarea class="tk-ta" data-f="goal" rows="2">${esc(b.goal || "")}</textarea>
      <div class="tk-hint">Markdown and LaTeX — <code>$x$</code> inline,
        <code>$$x$$</code> display. Preview:</div>
      <div class="tk-md tk-preview"></div>
      <div class="row" style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
        <button data-x="1">Cancel</button><button class="pri" data-save="1">Save</button></div>
    </div></div>`).firstChild;
    const close2 = () => m.remove();
    m.onclick = e => { if (e.target === m) close2(); };
    m.querySelectorAll("[data-x]").forEach(x => (x.onclick = close2));
    m.querySelector("[data-save]").onclick = async () => {
      await api(`/api/bubbles/${encodeURIComponent(S.slug)}/premise`, {
        method: "PUT",
        body: JSON.stringify({
          abstract: m.querySelector('[data-f="abstract"]').value,
          goal: m.querySelector('[data-f="goal"]').value,
        }),
      });
      close2();
      await loadHome();
    };
    const prev = m.querySelector(".tk-preview");
    const paint = () => {
      const a = m.querySelector('[data-f="abstract"]').value;
      const g = m.querySelector('[data-f="goal"]').value;
      renderMarkdown((a || "*(nothing yet)*") + (g ? `\n\n**Goal** — ${g}` : ""), prev);
    };
    m.querySelectorAll("textarea").forEach(t => (t.oninput = paint));
    paint();
    document.body.append(m);
    setTimeout(() => m.querySelector("textarea").focus(), 30);
  }

  // A talk's card title and explanation are its invitation to read the deck. Keep them
  // editable without rewriting the agent-authored first slide underneath it.
  async function editTalkTitles() {
    let talks = (S.data && S.data.talks) || [];
    if (!talks.length) {
      const listed = await api(`/api/bubbles/${encodeURIComponent(S.slug)}/talks`);
      talks = listed.talks || [];
    }
    if (!talks.length) { toast("Add a chalk talk first."); return; }
    let active = (S.talk && S.talk.talk && S.talk.talk.id) || talks[0].id;
    const m = h(`<div class="tk-modal"><div class="tk-mcard">
      <button class="tk-x" data-x="1" aria-label="Close">${LI_IC("close")}</button>
      <h3>Edit chalk talk titles</h3>
      <div class="msub">This changes the title and explanation on the chalk-talk list. It does not rewrite the slides.</div>
      <label class="tk-lab">Chalk talk</label>
      <select class="tk-ta" data-f="talk">${talks.map(t =>
        `<option value="${esc(t.id)}"${t.id === active ? " selected" : ""}>${esc(t.title || t.id)}</option>`).join("")}</select>
      <label class="tk-lab">Title</label><input class="tk-ta" data-f="title" maxlength="200">
      <label class="tk-lab">Explanation <span class="tk-dim">optional</span></label>
      <textarea class="tk-ta" data-f="intent" rows="3" maxlength="600"></textarea>
      <div class="row" style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
        <button data-x="1">Cancel</button><button class="pri" data-save="1">Save</button></div>
    </div></div>`).firstChild;
    const select = m.querySelector('[data-f="talk"]');
    const title = m.querySelector('[data-f="title"]');
    const intent = m.querySelector('[data-f="intent"]');
    const fill = () => {
      active = select.value;
      const t = talks.find(x => x.id === active) || {};
      title.value = t.title || ""; intent.value = t.intent || "";
    };
    select.onchange = fill; fill();
    const close2 = () => m.remove();
    m.onclick = e => { if (e.target === m) close2(); };
    m.querySelectorAll("[data-x]").forEach(x => (x.onclick = close2));
    m.querySelector("[data-save]").onclick = async () => {
      const save = m.querySelector("[data-save]"); save.disabled = true;
      try {
        const result = await api(`/api/bubbles/${encodeURIComponent(S.slug)}/talks/${encodeURIComponent(active)}`, {
          method: "PATCH", body: JSON.stringify({ title: title.value.trim(), intent: intent.value.trim() }),
        });
        const updated = result.talk;
        if (S.data && S.data.talks)
          S.data.talks = S.data.talks.map(t => t.id === active ? {...t, ...updated} : t);
        if (S.talk && S.talk.talk && S.talk.talk.id === active) S.talk.talk = {...S.talk.talk, ...updated};
        close2(); render(); toast("Chalk talk title saved");
      } catch (e) { save.disabled = false; toast(e.message); }
    };
    document.body.append(m);
    setTimeout(() => title.focus(), 30);
  }

  function renderList() {
    const talks = (S.data.talks || []);
    const body = h(`<div class="tk-talks">
      ${talks.length ? "" : `<div class="tk-empty"><p>No talks yet. A chalk talk is how the agent
        explains an idea whose correctness needs your judgement — a derivation, a design
        trade-off — as a few slides you can mark up, rather than burying it in a report page.</p>
        <p>Ask an agent working on this bubble to write one.</p></div>`}
      ${talks.map(t => `<div class="tk-card" data-id="${esc(t.id)}">
        <button class="tk-del" data-del="${esc(t.id)}" title="delete this talk">${LI_IC("trash")}</button>
        <div class="d">${esc(t.date)}</div>
        <div style="flex:1;min-width:0">
          <div class="t">${esc(t.title)}</div>
          <div class="i">${esc(t.intent || "")}</div>
          <div class="tk-meta">
            <span class="tk-tag">${t.slides} slide${t.slides === 1 ? "" : "s"}</span>
            ${t.open ? `<span class="tk-tag open">${t.open} open note${t.open === 1 ? "" : "s"}</span>`
                     : (t.notes ? `<span class="tk-tag done">all notes closed</span>` : "")}
            ${t.kicker ? `<span class="tk-tag">${esc(t.kicker)}</span>` : ""}
            ${t.landed ? `<span class="tk-tag done">${LI_IC("corner-down-right")} landed in ${esc(t.landed)}</span>` : ""}
          </div>
        </div></div>`).join("")}
    </div>`).firstChild;
    body.querySelectorAll(".tk-card").forEach(c => (c.onclick = () => loadTalk(c.dataset.id)));
    body.querySelectorAll(".tk-del").forEach(b => (b.onclick = async e => {
      e.stopPropagation();
      const t = talks.find(x => x.id === b.dataset.del) || {};
      const notes = t.open ? ` and its ${t.open} open note${t.open === 1 ? "" : "s"}` : "";
      if (!confirm(`Delete the talk “${t.title}”${notes}? Its slides and marks all go.`)) return;
      await api(`/api/bubbles/${S.slug}/talks/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" });
      await loadHome();
    }));
    return body;
  }

  function renderDeck() {
    if (S.edit) return renderDeckEdit();
    const sl = S.talk.slides[S.slide];
    if (!sl) return h(`<div class="tk-list"><div class="tk-empty">This talk has no slides yet.
      <br><br><button class="pri" data-editdeck="1">${LI_IC("pencil")} edit</button></div></div>`).firstChild;
    const mine = notesOn(S.slide);
    const wrap = h(`<div class="tk-stage">
      <div class="tk-col">
        <div class="tk-slide">
          <span class="kind">${esc(sl.kind)}</span>
          <span class="tk-stamp"><span>${esc(sl.date || "")}</span></span>
          <h2>${esc(sl.title)}</h2>
          ${sl.sub ? `<div class="sub">${esc(sl.sub)}</div>` : ""}
          <div class="tk-md"></div>
        </div>
        <div class="tk-foot">
          <div class="tk-dots">${S.talk.slides.map((s, i) =>
            `<button class="tk-dot${i === S.slide ? " on" : ""}${notesOn(i).some(n => n.status === "open") ? " note" : ""}"
              data-i="${i}" title="${esc(s.title)}"></button>`).join("")}</div>
          ${S.talk.slides.length > 8 ? `<span class="tk-cnt">${S.slide + 1} / ${S.talk.slides.length}</span>` : ""}
          <button data-nav="-1" aria-label="Previous slide">${LI_IC("arrow-left")}</button><button class="tk-resync ${S.syncState}" data-resync="1"
            title="${esc(syncTitle())}" aria-label="${esc(syncTitle())}">${LI_IC("refresh")}</button><button data-nav="1" aria-label="Next slide">${LI_IC("arrow-right")}</button>
          <button data-notes="1" class="tk-notestoggle${S.notes ? "" : " off"}"
            title="${S.notes ? "hide" : "show"} the notes pane and the marks on the slide"
            aria-pressed="${S.notes ? "true" : "false"}">${LI_IC(S.notes ? "panel-right" : "panel-left")}</button>
          <div class="tk-seg">
            <button data-draw="1" title="mark region — drag a box over the slide">${LI_IC("marquee")}</button>
            <button data-ink="1" title="draw — freehand strokes become the feedback">${LI_IC("mark-ink","mark")}</button>
            <button data-editdeck="1" title="edit this slide's markdown by hand">${LI_IC("pencil")}</button>
            <button data-add="1" title="add a blank slide after this one" aria-label="Add a slide">${LI_IC("plus")}</button>
            <button data-del="1" class="tk-danger" title="delete this slide and its marks">${LI_IC("trash")}</button>
          </div>
          <button class="tk-fab" data-fab="1" title="slide tools" aria-label="slide tools"
            aria-haspopup="true">${LI_IC("pencil")}</button>
        </div>
      </div>
      <div class="tk-gutter"></div>
    </div>`).firstChild;

    const md = wrap.querySelector(".tk-md");
    const annotationSurface = wrap.querySelector(".tk-slide");
    renderMarkdown(sl.body, md);
    // A slide's title and subtitle are source text too, not decorative chrome. Paint and
    // select across this whole surface so they can carry the same review marks as its body.
    paintAnchors(annotationSurface, sl, mine);
    annotationSurface.addEventListener("mouseup", onSelect);

    wrap.querySelectorAll("[data-nav]").forEach(b =>
      (b.onclick = () => go(S.slide + Number(b.dataset.nav))));
    wrap.querySelector("[data-resync]").onclick = resyncTalk;
    wrap.querySelector("[data-draw]").onclick = startDraw;
    wrap.querySelector("[data-ink]").onclick = startInk;
    const fab = wrap.querySelector("[data-fab]");
    if (fab) {
      fab.onclick = () => root.classList.toggle("tools-open");
      // Picking any tool is an answer; the popover's job is done.
      wrap.querySelector(".tk-seg").addEventListener("click",
        () => root.classList.remove("tools-open"));
    }
    wrap.querySelector("[data-add]").onclick = () => addSlideAfter(S.slide);
    wrap.querySelector("[data-del]").onclick = () => deleteSlideAt(S.slide);
    wrap.querySelector("[data-notes]").onclick = () => { S.notes = !S.notes; render(); };
    wrap.querySelectorAll(".tk-dot").forEach(d => (d.onclick = () => go(Number(d.dataset.i))));
    wrap.querySelector(".tk-gutter").append(renderGutter(mine));
    return wrap;
  }

  /** What a page card says under its title: how much is written, what is unanswered, how
   *  fresh. "home page" was the only thing there, and it is the one fact you learn once and
   *  never need again. Open marks lead when there are any — they are the thing asking for you.
   */
  function pageStats(p) {
    const bits = [];
    if (p.open_marks) bits.push(`<b class="tk-pgmark">${p.open_marks} open</b>`);
    bits.push(p.chars ? readingLength(p.chars) : "empty");
    if (p.edited_at) bits.push(esc(agoLabel(p.edited_at)));
    return bits.join(" · ");
  }
  /** How long the page is, in the unit a reader actually thinks in. ~1000 characters is roughly
   *  a minute of reading; below that, say "a note" rather than pretend to precision. */
  function readingLength(chars) {
    const mins = Math.round(chars / 1000);
    return mins < 1 ? "a note" : mins + " min read";
  }
  const shortCount = n => n >= 10000 ? Math.round(n / 1000) + "k"
                        : n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, "") + "k" : String(n);
  /** Coarse on purpose: "3d" is what you want from a card, and an exact stamp would invite
   *  reading it as precision the mtime does not really carry. */
  function agoLabel(epochSeconds) {
    const secs = Math.max(0, Date.now() / 1000 - epochSeconds);
    if (secs < 90) return "just now";
    const mins = secs / 60;
    if (mins < 60) return Math.round(mins) + "m ago";
    const hours = mins / 60;
    if (hours < 24) return Math.round(hours) + "h ago";
    const days = hours / 24;
    if (days < 30) return Math.round(days) + "d ago";
    const months = days / 30;
    return months < 12 ? Math.round(months) + "mo ago" : Math.round(months / 12) + "y ago";
  }

  function renderGutter(mine) {
    const shot = n => n.image
      ? wsUrl(`/api/bubbles/${S.slug}/talks/${S.talk.talk.id}/notes/${n.id}/shot.png`) : "";
    const el = h(`<div style="display:contents">
      <div class="tk-gh">Your notes · slide ${S.slide + 1}<span class="tk-sp"></span>
        ${openCount() ? `<span class="tk-tag open">${openCount()} open</span>`
                      : `<span class="tk-tag done">all closed</span>`}</div>
      ${mine.length ? "" : `<div class="tk-empty tk-empty-notes" style="font-size:14px">
        Select any text on the slide — or hit <b>${LI_IC("marquee")} mark region</b> and drag a box —
        then pick one of <b>${ORDER.map(k=>KINDS[k].glyph).join(" ")}</b>. Or <b>${LI_IC("mark-ink","mark")} draw</b> on the slide and let the
        drawing say it.</div>`}
      ${mine.map(n => noteCard({ ...n, orphanNote: n.anchorLost ? LI_IC("warning") + " text moved · " : "",
                                 image: shot(n),
                                 messages: (n.messages || []).map(msg => ({ ...msg,
                                   agent: !!msg.agent })) })).join("")}
      <div style="margin-top:auto;padding-top:10px">
        <button data-payload="1" style="width:100%">${LI_IC("agent")} Feedback YAML</button></div>
    </div>`);
    wireCard(el, {
      onDelete: async id => {
        await api(`/api/bubbles/${S.slug}/talks/${S.talk.talk.id}/notes/${id}`, { method: "DELETE" });
        await loadTalk(S.talk.talk.id, true);
      },
      onEdit: async (id, text) => {
        if (id === null) { render(); return; }
        await api(`/api/bubbles/${S.slug}/talks/${S.talk.talk.id}/notes/${id}`,
                  { method: "PATCH", body: JSON.stringify({ text }) });
        await loadTalk(S.talk.talk.id, true);
      },
      onReply: async (id, text) => {
        if (id === null || !text) { render(); return; }
        await api(`/api/bubbles/${S.slug}/talks/${S.talk.talk.id}/notes/${id}/replies`,
                  { method: "POST", body: JSON.stringify({ text }) });
        await loadTalk(S.talk.talk.id, true);
      },
    });
    el.style.display = "contents";   // let the cards be the gutter's own flex items
    el.querySelector("[data-payload]").onclick = showPayload;
    el.querySelectorAll(".tk-note").forEach(c => (c.onclick = () => {
      const m = root.querySelector(`.tk-anno[data-note="${c.dataset.note}"],.tk-region[data-note="${c.dataset.note}"],.tk-ink[data-note="${c.dataset.note}"]`);
      if (m) m.scrollIntoView({ block: "center", behavior: "smooth" });
    }));
    const gh = el.querySelector(".tk-gh");
    // Desktop only: on a phone the pointer handlers below own the gesture, and a click after
    // them would toggle the pane straight back.
    gh.onclick = () => { if (innerWidth > 900) root.classList.toggle("notes-open"); };
    bindSheetDrag(gh, root);
    return el;
  }

  /** Let the notes drawer be pulled out by its spine, not just poked by the caret.
   *
   *  On a phone the pane is a right-edge drawer, and the gesture people reach for is dragging
   *  it out. The caret alone was a ~12px target. The spine follows the finger while it moves —
   *  a drawer that jumps only on release does not feel attached to the hand — and on release
   *  snaps to whichever end it was heading for. A press that never travels is still a tap, so
   *  the old toggle keeps working.
   *
   *  It follows the finger with `transform`, never `width`: a width animation relayouts every
   *  note card on every frame, which is what made the old sheet judder. Transform is composited
   *  and costs nothing per frame, and the inline one is dropped on release so the class governs
   *  again and the CSS transition plays out the snap.
   */
  function bindSheetDrag(gh, root) {
    const SNAP = 44, TAP = 6;
    // Resolved per gesture, not here: this runs while the header is still detached — the caller
    // appends it to the gutter afterwards — so binding against closest() at this point found
    // nothing and silently dropped both the drag and the tap.
    let sheet = null;
    let startX = 0, dx = 0, shut = 0, wasOpen = false, dragging = false;
    const handle = () =>
      parseFloat(getComputedStyle(sheet).getPropertyValue("--tk-handle")) || 44;

    gh.addEventListener("pointerdown", e => {
      if (innerWidth > 900) return;          // the gutter is a column here, not a drawer
      sheet = gh.closest(".tk-gutter,#reviewWrap");   // the doc page's marks pane is a drawer too
      if (!sheet) return;
      dragging = true; dx = 0; startX = e.clientX;
      wasOpen = root.classList.contains("notes-open");
      shut = Math.max(0, sheet.getBoundingClientRect().width - handle());
      sheet.style.transition = "none";       // follow the finger, don't ease behind it
      try { gh.setPointerCapture(e.pointerId); } catch (err) { /* capture is a nicety */ }
    });

    gh.addEventListener("pointermove", e => {
      if (!dragging) return;
      dx = e.clientX - startX;               // leftwards (opening) is negative
      const x = Math.max(0, Math.min(shut, (wasOpen ? 0 : shut) + dx));
      sheet.style.transform = `translateX(${x}px)`;
      // Reveal the notes as it travels, so what you are dragging out is what you see.
      root.classList.toggle("notes-open", x < shut - 8);
      e.preventDefault();
    });

    const settle = () => {
      if (!dragging) return;
      dragging = false;
      sheet.style.transition = ""; sheet.style.transform = "";
      root.classList.toggle("notes-open",
        Math.abs(dx) < TAP ? !wasOpen : (wasOpen ? dx < SNAP : dx < -SNAP));
    };
    gh.addEventListener("pointerup", settle);
    gh.addEventListener("pointercancel", settle);
  }


  /* ------------------------------------------------------------- manual editing */
  // The same markdown editor the document uses, pointed at one slide. Every open mark arrives
  // materialised as <comment-begin=id>…<comment-end=id> — the tag pair report pages use — so the
  // text a mark points at is visible while you rewrite it, and moves with your edit.
  function destroyEditor() {
    if (S.editorHandle) { try { S.editorHandle.dispose(); } catch (e) {} S.editorHandle = null; }
    else if (S.editorObj) { try { S.editorObj.destroy(); } catch (e) {} }
    S.editorObj = null;
  }
  const editDirty = () => {
    try {
      const kindSel = document.querySelector(".tk-ekind");
      const kindChanged = kindSel && S.editKind != null && kindSel.value !== S.editKind;
      return kindChanged || (S.editorObj && S.editorObj.getMarkdown() !== (S.editInitial || ""));
    } catch (e) { return false; }
  };
  function leaveEditOk() {
    return !editDirty() || confirm("Discard the unsaved changes to this slide?");
  }

  async function addSlideAfter(i) {
    const r = await api(`/api/bubbles/${S.slug}/talks/${S.talk.talk.id}/slides`,
                        { method: "POST", body: JSON.stringify({ after: i }) });
    S.edit = true;
    await loadTalk(S.talk.talk.id, true);
    go(r.index);
  }

  async function deleteSlideAt(i) {
    const sl = S.talk.slides[i];
    const n = notesOn(i).length;
    if (!confirm(`Delete slide ${i + 1} — “${sl.title}”` +
                 (n ? ` — and its ${n} mark${n === 1 ? "" : "s"}?` : "?"))) return;
    await api(`/api/bubbles/${S.slug}/talks/${S.talk.talk.id}/slides/${i}`, { method: "DELETE" });
    await loadTalk(S.talk.talk.id, true);
  }

  function renderDeckEdit() {
    const sl = S.talk.slides[S.slide];
    if (!sl) {
      const empty = h(`<div class="tk-list"><div class="tk-empty">This talk has no slides.
        <br><br><button class="pri" data-add0="1">+ add the first slide</button></div></div>`).firstChild;
      empty.querySelector("[data-add0]").onclick = () => addSlideAfter(-1);
      return empty;
    }
    const wrap = h(`<div class="tk-stage tk-editing">
      <div class="tk-col">
        <div class="tk-editcard">
          <div class="tk-edithead">
            <input class="tk-ekind" list="tk-kind-options" value="${esc(sl.kind)}"
              title="Section label — choose a suggestion or type your own" aria-label="Slide section">
            <datalist id="tk-kind-options">
              ${["setup","derivation","evidence","comparison","implementation","ask"].map(k =>
                `<option value="${k}">`).join("")}
            </datalist>
            <span class="tk-sp"></span>
            <button class="pri" data-save="1">Save slide</button>
          </div>
          <div class="tk-edithost"></div>
          <div class="tk-editnote">Type any section label above. The first line is the slide title:
            <code># title</code>, then an optional <code>*subtitle*</code> line. Marks appear as
            <code>&lt;comment-begin=id&gt;…&lt;comment-end=id&gt;</code> — edit inside them and the mark follows it.</div>
        </div>
        <div class="tk-foot">
          <div class="tk-dots">${S.talk.slides.map((s, i) =>
            `<button class="tk-dot${i === S.slide ? " on" : ""}${notesOn(i).length ? " note" : ""}"
              data-i="${i}" title="${esc(s.title)}"></button>`).join("")}</div>
          ${S.talk.slides.length > 8 ? `<span class="tk-cnt">${S.slide + 1} / ${S.talk.slides.length}</span>` : ""}
          <button data-nav="-1" aria-label="Previous slide">${LI_IC("arrow-left")}</button><button class="tk-resync ${S.syncState}" data-resync="1"
            title="${esc(syncTitle())}" aria-label="${esc(syncTitle())}">${LI_IC("refresh")}</button><button data-nav="1" aria-label="Next slide">${LI_IC("arrow-right")}</button>
          <span class="tk-sp"></span>
          <button data-editdeck="1" title="leave edit mode">${LI_IC("close")} stop editing</button>
        </div>
      </div>
    </div>`).firstChild;

    const host = wrap.querySelector(".tk-edithost");
    S.editInitial = sl.edit_source || "";
    S.editKind = sl.kind;

    // One save path for both affordances: the toolbar chip saves in place (the deck reloads
    // and edit mode re-opens on fresh source), the header button saves and leaves.
    const saveSlide = async (exit) => {
      if (!editDirty() && exit) { S.edit = false; render(); return; }
      if (S.editorHandle) S.editorHandle.setSync("saving");
      try {
        await api(`/api/bubbles/${S.slug}/talks/${S.talk.talk.id}/slides/${S.slide}/source`, {
          method: "PUT",
          body: JSON.stringify({ text: S.editorObj.getMarkdown(),
                                 kind: wrap.querySelector(".tk-ekind").value }),
        });
        S.edit = !exit;
        await loadTalk(S.talk.talk.id, true);
        toast("Saved");
      } catch (e) {
        if (S.editorHandle) S.editorHandle.setSync("stale");
        toast(e.message);
      }
    };

    // The document's editor, pointed at one slide: index.html owns the construction so the two
    // surfaces cannot drift. The bare fallback exists only for a standalone mount.
    if (window.LockedInEditor) {
      S.editorHandle = window.LockedInEditor.create(host, { initialValue: S.editInitial,
                                                            bubble: S.slug,
                                                            onSave: () => saveSlide(false) });
      S.editorObj = S.editorHandle.editor;
    } else {
      S.editorHandle = null;
      S.editorObj = new toastui.Editor({
        el: host, height: "100%", theme: "dark", initialEditType: "markdown",
        previewStyle: "tab", initialValue: S.editInitial, usageStatistics: false, autofocus: false,
        toolbarItems: [["heading", "bold", "italic", "code", "link", "quote", "ul", "ol", "table"]],
      });
    }

    wrap.querySelector("[data-save]").onclick = () => saveSlide(true);
    // A kind change never touches the editor, so tell the chip ourselves.
    wrap.querySelector(".tk-ekind").oninput = () =>
      S.editorHandle && S.editorHandle.setSync("stale");
    const guarded = fn => () => { if (leaveEditOk()) fn(); };
    wrap.querySelectorAll("[data-nav]").forEach(b =>
      (b.onclick = guarded(() => go(S.slide + Number(b.dataset.nav)))));
    wrap.querySelector("[data-resync]").onclick = resyncTalk;
    wrap.querySelectorAll(".tk-dot").forEach(d =>
      (d.onclick = guarded(() => go(Number(d.dataset.i)))));
    return wrap;
  }

  function renderSheet() {
    const el = h(`<div class="tk-sheet"><div class="tk-grid">
      ${S.talk.slides.map((s, i) => {
        const by = {};
        notesOn(i).forEach(n => (by[n.kind] = (by[n.kind] || 0) + 1));
        return `<div class="tk-mini${i === S.slide ? " cur" : ""}" data-i="${i}">
          <div class="n">${String(i + 1).padStart(2, "0")} · ${esc(s.kind)}</div>
          <div class="t">${esc(s.title)}</div>
          <div class="s">${esc(s.sub || "")}</div>
          <div class="nb">${Object.entries(by).map(([k, c]) =>
            `<i style="--kc:${KINDS[k].color}">${KINDS[k].glyph}${c}</i>`).join("")}</div>
        </div>`;
      }).join("")}
    </div></div>`).firstChild;
    el.querySelectorAll(".tk-mini").forEach(m => (m.onclick = () => {
      S.slide = Number(m.dataset.i); S.view = "deck"; render();
    }));
    return el;
  }

  function go(i) {
    if (i < 0 || i >= S.talk.slides.length) return;
    S.slide = i; render();
  }

  /** Horizontal flick anywhere on the deck turns the page, the way fingers expect slides to
   *  work. Observation only — passive listeners, no preventDefault — so nothing here can slow
   *  scrolling; the flick is recognised after the fact. Deliberately narrow about when: a
   *  quick, mostly-horizontal, short-lived gesture, and never while editing, drawing, choosing
   *  a mark, selecting text for one, or over something that scrolls sideways itself (a wide
   *  code block, the thumbnail strip) — a swipe there is that element's scroll, not ours. */
  function bindSlideSwipe(el) {
    let t0 = 0, x0 = 0, y0 = 0, live = false;
    el.addEventListener("touchstart", e => {
      live = false;
      if (e.touches.length !== 1 || S.view !== "deck" || S.edit) return;
      if (document.querySelector(".tk-inkdraw,.tk-draw,.tk-pop,.tk-modal")) return;
      for (let n = e.target; n && n !== el; n = n.parentElement) {
        if (n.classList && n.classList.contains("tk-gutter")) return;   // the drawer owns this drag
        if (n.scrollWidth > n.clientWidth + 4 && /auto|scroll/.test(getComputedStyle(n).overflowX)) return;
      }
      t0 = Date.now(); x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; live = true;
    }, { passive: true });
    // Deliberately non-passive: Chrome turns a horizontal flick the page cannot scroll into a
    // history navigation — the exact gesture as "previous slide", and root overscroll-behavior
    // does not reach it. Once the gesture is decisively horizontal, preventing the move cancels
    // the browser's gesture for this touch. Until then nothing is prevented, so vertical
    // scrolling never pays for this.
    el.addEventListener("touchmove", e => {
      if (!live) return;
      const t = e.touches[0];
      const dx = Math.abs(t.clientX - x0), dy = Math.abs(t.clientY - y0);
      if (dx > 18 && dx > 1.8 * dy) e.preventDefault();
    }, { passive: false });
    el.addEventListener("touchend", e => {
      if (!live) return;
      live = false;
      if (String(getSelection())) return;        // a long-press selection is a mark being born
      const t = e.changedTouches[0];
      const dx = t.clientX - x0, dy = t.clientY - y0;
      if (Date.now() - t0 > 500 || Math.abs(dx) < 70 || Math.abs(dy) > 50) return;
      go(S.slide + (dx < 0 ? 1 : -1));
    }, { passive: true });
  }

  /* ------------------------------------------------------------- mark picker */
  // A selection that clips into rendered math must be widened to the whole formula: half of a
  // KaTeX subtree is not a substring of anything the agent wrote.
  function widenToMath(range, md) {
    const climb = node => {
      let el = node && node.nodeType === 3 ? node.parentNode : node;
      while (el && el !== md) {
        if (el.classList && el.classList.contains("tk-math")) return el;
        el = el.parentNode;
      }
      return null;
    };
    const a = climb(range.startContainer), b = climb(range.endContainer);
    if (a) range.setStartBefore(a);
    if (b) range.setEndAfter(b);
    return range;
  }

  // Read a selection back as *markdown*: every rendered formula contributes its own source
  // rather than the glyphs KaTeX drew for it.
  function selectionSource(range) {
    const frag = range.cloneContents();
    frag.querySelectorAll(".tk-math").forEach(n =>
      n.replaceWith(document.createTextNode(" " + (n.dataset.md || "") + " ")));
    return frag.textContent.replace(/\s+/g, " ").trim();
  }

  function onSelect(event) {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) return;
    const surface = event && event.currentTarget || root.querySelector(".tk-slide");
    if (!surface || !surface.contains(sel.anchorNode) || !surface.contains(sel.focusNode)) return;
    // Keep section/date chrome out of the annotation address space, while treating title,
    // subtitle and body as one editable slide surface.
    const selectedEl = sel.anchorNode.nodeType === Node.TEXT_NODE ? sel.anchorNode.parentElement : sel.anchorNode;
    if (!selectedEl || !selectedEl.closest("h2,.sub,.tk-md")) return;
    const range = widenToMath(sel.getRangeAt(0).cloneRange(), surface);
    const text = selectionSource(range);
    if (text.length < 2) return;
    clearPending();
    const quote = findInSource(S.talk.slides[S.slide].source, text);
    if (!quote) {
      toast("Couldn’t anchor that selection to the slide source — try selecting whole words.");
      return;
    }
    const r = range.getBoundingClientRect();
    // Which occurrence of this text the reader actually selected. "repeated phrase" marked in
    // its third appearance must not anchor — or paint — on the first.
    let occurrence = 1;
    try {
      const { nodes, chars, flat } = flattenText(surface);
      const probe = range.cloneRange(); probe.collapse(true);
      let pos = flat.length;
      for (let ci = 0; ci < chars.length; ci++) {
        const ch = chars[ci];
        if (ch.ni < 0) continue;
        // comparePoint: -1 before the collapsed probe, 0 at it, 1 after — the first
        // character at-or-after the selection start is the first non-negative one.
        if (probe.comparePoint(nodes[ch.ni], ch.i) >= 0) { pos = ci; break; }
      }
      const re = quotePattern(quote);
      let m, k = 0;
      while (re && (m = re.exec(flat))) {
        if (m.index >= pos + 2) break;   // small tolerance for the leading edge
        k += 1;
        if (m.index === re.lastIndex) re.lastIndex++;
      }
      occurrence = Math.max(1, k);
    } catch (e) { /* first occurrence is still a sane default */ }
    S.pending = { quote, occurrence };
    paintPendingRange(range);
    if (window.getSelection) window.getSelection().removeAllRanges();
    openPicker(r.left, r.bottom + 8);
  }

  function openPicker(x, y) {
    closePicker();
    const p = h(`<div class="tk-pop">
      <div class="tk-kinds">${ORDER.map(k => `<div class="tk-kb${k === S.kind ? " sel" : ""}" data-k="${k}"
        style="--kc:${KINDS[k].color}"><div class="g">${KINDS[k].glyph}</div>
        <div class="l">${KINDS[k].label}</div></div>`).join("")}</div>
      <div class="qt">${S.pending.quote ? "“" + esc(S.pending.quote.slice(0, 120)) + "”"
                                        : LI_IC("marquee") + " region on the slide"}</div>
      <textarea placeholder="optional — say more"></textarea>
      <div class="row"><span class="hint">a mark alone is enough</span>
        <button data-cancel="1">Esc</button><button class="pri" data-pin="1">Pin note</button></div>
    </div>`).firstChild;
    p.style.left = Math.max(10, Math.min(x, innerWidth - 326)) + "px";
    p.style.top = Math.min(y, innerHeight - 220) + "px";
    document.body.append(p);
    p.querySelectorAll(".tk-kb").forEach(b => (b.onclick = () => {
      S.kind = b.dataset.k;
      p.querySelectorAll(".tk-kb").forEach(o => o.classList.toggle("sel", o === b));
    }));
    p.querySelector("[data-cancel]").onclick = dismissPicker;
    p.querySelector("[data-pin]").onclick = async () => {
      const text = p.querySelector("textarea").value.trim();
      const payload = { slide: S.slide, kind: S.kind, text };
      if (S.pending.quote) {
        payload.quote = S.pending.quote;
        payload.occurrence = S.pending.occurrence || 1;
      }
      if (S.pending.rect) {
        payload.rect = S.pending.rect;
        const slideEl = root.querySelector(".tk-slide");
        if (slideEl) payload.covers = textUnderRect(slideEl, S.pending.rect).filter(Boolean);
      }
      closePicker(); S.pending = null;
      S.notes = true;
      const created = await api(`/api/bubbles/${S.slug}/talks/${S.talk.talk.id}/notes`,
                                { method: "POST", body: JSON.stringify(payload) });
      if (innerWidth <= 900) root.classList.add("notes-open");
      await loadTalk(S.talk.talk.id, true);
      if (payload.rect) {
        await captureNote(created.note.id);
        await loadTalk(S.talk.talk.id, true);
      }
    };
    setTimeout(() => p.querySelector("textarea").focus(), 30);
  }
  function closePicker() {
    document.querySelectorAll(".tk-pop").forEach(p => p.remove());
  }

  function dismissPicker() {
    closePicker();
    clearPending();
    S.pending = null;
  }

  /** Dismiss whichever draw layer is up, via the teardown it published on itself.
   *  The modes are exclusive by construction — ink's canvas sits above region's layer, so
   *  starting one under the other left it unreachable and looking broken. */
  function cancelDrawModes() {
    document.querySelectorAll(".tk-inkdraw,.tk-draw").forEach(l => l._cancel && l._cancel());
  }

  function clearPending() {
    document.querySelectorAll(".tk-pending-region").forEach(box => box.remove());
    document.querySelectorAll("mark.tk-pending").forEach(mk => {
      const parent = mk.parentNode;
      while (mk.firstChild) parent.insertBefore(mk.firstChild, mk);
      mk.remove();
      parent.normalize();
    });
  }

  function paintPendingRange(range) {
    try {
      const mk = document.createElement("mark");
      mk.className = "tk-pending";
      range.surroundContents(mk);
    } catch (e) {
      // A range spanning element boundaries cannot be wrapped in one node; extract and re-insert.
      try {
        const mk = document.createElement("mark");
        mk.className = "tk-pending";
        mk.appendChild(range.extractContents());
        range.insertNode(mk);
      } catch (e2) { /* worst case the mark is simply not previewed */ }
    }
  }

  function startDraw() {
    // The slide, not just its body: a mark is often *about* the title, the kind chip, or the
    // whitespace between two steps. It is also the element that gets photographed, so the
    // stored rectangle and the picture share one coordinate system.
    const md = root.querySelector(".tk-slide");
    if (!md) return;
    cancelDrawModes();                    // switching from ✍ mid-thought is a cancel, not a bug
    clearPending();
    // A selection made just before entering survives the user-select switch-off and keeps its
    // handles on screen through the whole drag; drop it at the door.
    getSelection().removeAllRanges();
    const layer = document.createElement("div");
    layer.className = "tk-draw";
    md.appendChild(layer);
    let box = null, x0 = 0, y0 = 0;

    const teardown = () => {
      getSelection().removeAllRanges();   // no phantom selection re-materializing on exit
      layer.remove();
      removeEventListener("pointermove", move, true);
      removeEventListener("pointerup", up, true);
      removeEventListener("keydown", esc, true);
    };
    layer._cancel = teardown;
    // The release is listened for on the window, not the layer: dragging past the edge of the
    // slide is the normal way to box something that touches it, and a mouseup the layer never
    // sees used to leave the draw mode armed with a stranded rectangle on screen.
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(v, hi));
    // Percentages on an absolutely positioned child resolve against the containing block's
    // *padding* box, so the drag has to be measured against the same thing — the border width
    // getBoundingClientRect includes would otherwise skew every stored rectangle.
    const at = e => {
      const r = md.getBoundingClientRect();
      const bw = (r.width - md.clientWidth) / 2, bh = (r.height - md.clientHeight) / 2;
      const b = { left: r.left + bw, top: r.top + bh,
                  width: md.clientWidth, height: md.clientHeight };
      return [clamp(e.clientX - b.left, 0, b.width), clamp(e.clientY - b.top, 0, b.height), b];
    };
    const move = e => {
      if (!box) return;
      const [x, y] = at(e);
      box.style.left = Math.min(x, x0) + "px"; box.style.top = Math.min(y, y0) + "px";
      box.style.width = Math.abs(x - x0) + "px"; box.style.height = Math.abs(y - y0) + "px";
    };
    const up = e => {
      if (!box) { teardown(); return; }
      const [x, y, b] = at(e);
      const w = Math.abs(x - x0), hh = Math.abs(y - y0);
      const geom = box.style.cssText;
      teardown();
      if (w < 12 || hh < 12) return;
      S.pending = { rect: { x: +(Math.min(x, x0) / b.width * 100).toFixed(2),
                            y: +(Math.min(y, y0) / b.height * 100).toFixed(2),
                            w: +(w / b.width * 100).toFixed(2),
                            h: +(hh / b.height * 100).toFixed(2) } };
      // Re-home the box on the slide itself so it outlives the draw layer and stays visible
      // for as long as you are writing about it.
      const keep = document.createElement("div");
      keep.className = "tk-pending-region";
      keep.style.cssText = geom;
      md.appendChild(keep);   // md is the slide here
      openPicker(e.clientX, Math.min(e.clientY + 10, innerHeight - 230));
    };
    const esc = e => { if (e.key === "Escape") { teardown(); e.stopPropagation(); } };

    // Pointer events, not mouse events: a browser synthesizes mouse events from taps but never
    // from a touch *drag*, so on an iPhone or iPad the box could not be drawn at all — this is
    // a drag-only tool. Capturing the pointer keeps the drag alive past the slide's edge, which
    // is the normal way to box something that touches it.
    layer.onpointerdown = e => {
      if (!e.isPrimary) return;            // a second finger mid-drag is not a second box
      const [x, y] = at(e);
      x0 = x; y0 = y;
      box = document.createElement("div"); box.className = "tk-drawbox"; layer.appendChild(box);
      try { layer.setPointerCapture(e.pointerId); } catch (err) { /* capture is a nicety */ }
      addEventListener("pointermove", move, true);
      addEventListener("pointerup", up, true);
      e.preventDefault();
    };
    addEventListener("keydown", esc, true);
  }

  /* ------------------------------------------------------------ freehand ink */
  // The words under a stroke, in document order. A closed stroke rings its content, so its
  // interior is sampled too — the loop's own line usually misses the word it surrounds.
  function textUnderStrokes(slide, paths) {
    // Region overlays are clickable and sit over the prose; a caret probe through one returns
    // the box, not the words beneath. Lift them out of hit testing for the duration.
    const lifted = [...slide.querySelectorAll(".tk-region")].map(el => {
      const was = el.style.pointerEvents;
      el.style.pointerEvents = "none";
      return () => { el.style.pointerEvents = was; };
    });
    try {
      return textUnderStrokesInner(slide, paths);
    } finally { lifted.forEach(fn => fn()); }
  }
  function textUnderStrokesInner(slide, paths) {
    const r = slide.getBoundingClientRect();
    const caret = (cx, cy) => {
      if (document.caretPositionFromPoint) return document.caretPositionFromPoint(cx, cy);
      const rr = document.caretRangeFromPoint && document.caretRangeFromPoint(cx, cy);
      return rr ? { offsetNode: rr.startContainer, offset: rr.startOffset } : null;
    };
    return paths.map(pts => {
      // The pointer's own samples are as sparse as the hand was fast; walk each segment at
      // ~0.8% steps so a quick stroke still touches every word under it.
      const samples = [];
      for (let i = 0; i < pts.length; i++) {
        samples.push(pts[i]);
        if (i + 1 >= pts.length) break;
        const d = Math.hypot(pts[i + 1].x - pts[i].x, pts[i + 1].y - pts[i].y);
        const n = Math.min(40, Math.floor(d / 0.8));
        for (let k = 1; k <= n; k++)
          samples.push({ x: pts[i].x + (pts[i + 1].x - pts[i].x) * k / (n + 1),
                         y: pts[i].y + (pts[i + 1].y - pts[i].y) * k / (n + 1) });
      }
      const a = pts[0], z = pts[pts.length - 1];
      if (pts.length > 4 && Math.hypot(a.x - z.x, a.y - z.y) < 12) {
        // Inside the loop itself, not its bounding box — a live agy session compared the
        // fallback against the picture and called out the words the box grazed but the ink
        // never enclosed.
        const inside = (x, y) => {
          let hit = false;
          for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
            const pi = pts[i], pj = pts[j];
            if ((pi.y > y) !== (pj.y > y) &&
                x < (pj.x - pi.x) * (y - pi.y) / (pj.y - pi.y) + pi.x) hit = !hit;
          }
          return hit;
        };
        const xs = pts.map(q => q.x), ys = pts.map(q => q.y);
        const x1 = Math.min(...xs), x2 = Math.max(...xs);
        const y1 = Math.min(...ys), y2 = Math.max(...ys);
        for (let gy = 1; gy <= 5; gy++) for (let gx = 1; gx <= 7; gx++) {
          const sx = x1 + (x2 - x1) * gx / 8, sy = y1 + (y2 - y1) * gy / 6;
          if (inside(sx, sy)) samples.push({ x: sx, y: sy });
        }
      }
      const hits = [], seen = new Set();
      for (const pt of samples) {
        const pos = caret(r.left + pt.x / 100 * r.width, r.top + pt.y / 100 * r.height);
        const node = pos && pos.offsetNode;
        if (!node || node.nodeType !== 3 || !slide.contains(node.parentNode)) continue;
        const text = node.nodeValue || "";
        let i = pos.offset, j = pos.offset;
        while (i > 0 && !/\s/.test(text[i - 1])) i--;
        while (j < text.length && !/\s/.test(text[j])) j++;
        const w = text.slice(i, j).trim();
        const key = (node.parentNode.className || "") + ":" + i + ":" + w;
        if (w && !seen.has(key)) { seen.add(key); hits.push({ node, i, w }); }
      }
      // Reading order, not sampling order — "Five marks, … Nothing else." rather than the
      // scrambled path the pointer happened to take.
      hits.sort((p1, p2) => p1.node === p2.node
        ? p1.i - p2.i
        : (p1.node.compareDocumentPosition(p2.node) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));
      return hits.map(h2 => h2.w).join(" ").slice(0, 160);
    });
  }

  function textUnderRect(slide, rect) {
    const pts = [];
    for (let gx = 0; gx <= 5; gx++) for (let gy = 0; gy <= 5; gy++)
      pts.push({ x: rect.x + rect.w * gx / 5, y: rect.y + rect.h * gy / 5 });
    return textUnderStrokes(slide, [pts]);
  }

  // Draw anything over the slide — cross a line out, arrow a paragraph somewhere else, circle
  // the weak step, write in the margin. Done pins a single ✍ mark whose snapshot carries the
  // strokes; to the agent the picture IS the feedback.
  function startInk() {
    const slide = root.querySelector(".tk-slide");
    if (!slide) return;
    cancelDrawModes();                  // ✍ over an armed 🖍 replaces it
    clearPending();
    getSelection().removeAllRanges();   // same reason as startDraw: stale handles outlive the mode
    const layer = document.createElement("canvas");
    layer.className = "tk-inkdraw";
    layer.style.setProperty("--kc", inkColor());
    // Belt over touch-action's braces: preventing the raw touch default here stops Safari
    // running double-tap disambiguation between quick pen-downs — the beat it spends wondering
    // whether two fast strokes are a zoom is the "delay between strokes". Pointer events are
    // unaffected; the listeners die with the layer.
    const eat = e => e.preventDefault();
    layer.addEventListener("touchstart", eat, { passive: false });
    layer.addEventListener("touchmove", eat, { passive: false });
    slide.appendChild(layer);
    const bar = h(`<div class="tk-inkbar"><b>draw on the slide · Esc cancels</b>
      <button data-undo="1">${LI_IC("undo")} undo</button>
      <button class="pri" data-done="1">${LI_IC("check")} done</button></div>`).firstChild;
    slide.appendChild(bar);

    const paths = [];
    let cur = null;
    const repaint = () => paintInk(layer, slide, paths.concat(cur ? [cur] : []));
    repaint();   // also sizes the canvas; paintInk owns the geometry

    // While the pen is down, draw only the segment it just added. The full repaint ran on
    // every sample, and assigning a canvas's width — even unchanged — wipes and reallocates
    // its backing store, so each sample cost a ~10MB reallocation plus a from-scratch redraw
    // of every stroke so far. Handwriting is dozens of strokes: input lagged, samples dropped,
    // and every pen lift hitched. Incremental segments make a sample O(1); paintInk remains
    // the one true renderer for undo, replay and capture, and the segments match its geometry.
    const ctx = layer.getContext("2d");
    const seg = (a, b) => {
      ctx.beginPath();
      ctx.moveTo(a.x * layer.width / 100, a.y * layer.height / 100);
      ctx.lineTo(b.x * layer.width / 100, b.y * layer.height / 100);
      ctx.stroke();
    };
    // Style and geometry are set once per stroke, at pen-down: getComputedStyle (inside
    // inkColor) and getBoundingClientRect both force style/layout work, and neither belongs in
    // the per-sample path. Nothing can move the slide mid-stroke — touch-action is none.
    let rect = null;
    const armStroke = () => {
      rect = layer.getBoundingClientRect();
      ctx.strokeStyle = ctx.fillStyle = inkColor();
      ctx.lineWidth = Math.max(2, layer.width / 480);
      ctx.lineCap = ctx.lineJoin = "round";
    };
    const pos = e => {
      const r = rect || layer.getBoundingClientRect();
      return { x: +((e.clientX - r.left) / r.width * 100).toFixed(2),
               y: +((e.clientY - r.top) / r.height * 100).toFixed(2) };
    };
    const teardown = () => {
      getSelection().removeAllRanges();   // no phantom selection re-materializing on exit
      layer.remove(); bar.remove();
      removeEventListener("keydown", esc, true);
    };
    layer._cancel = teardown;
    layer.onpointerdown = e => {
      if (!e.isPrimary) return;
      layer.setPointerCapture(e.pointerId);
      armStroke();
      cur = [pos(e)];
      // Ink at the instant of contact — a dot, so the pen never touches down onto nothing.
      // It also makes a plain tap a mark: the dot on an i is a stroke that never moves.
      const p0 = cur[0];
      ctx.beginPath();
      ctx.arc(p0.x * layer.width / 100, p0.y * layer.height / 100, ctx.lineWidth / 2, 0, 7);
      ctx.fill();
      e.preventDefault();
    };
    layer.onpointermove = e => {
      if (!cur) return;
      // Coalesced events: a Pencil samples faster than the browser dispatches, and the extras
      // ride along on the event — without them fast handwriting arrives as chords, not curves.
      const events = e.getCoalescedEvents ? e.getCoalescedEvents() : [e];
      for (const ev of events) {
        const pt = pos(ev);
        const last = cur[cur.length - 1];
        if (Math.abs(pt.x - last.x) + Math.abs(pt.y - last.y) < 0.35) continue;   // thin the samples
        cur.push(pt); seg(last, pt);
      }
    };
    layer.onpointerup = () => {
      if (!cur) return;
      // The stroke is already on the canvas; nothing to redraw on lift. A tap is kept as a
      // degenerate two-point path — paintInk's round caps render that as the same dot, so the
      // replay, the undo and the snapshot all agree with what the pen left behind.
      if (cur.length === 1) cur.push({ x: cur[0].x + 0.01, y: cur[0].y });
      paths.push(cur);
      cur = null;
    };
    layer.onpointercancel = () => { cur = null; repaint(); };   // palm-rejected: erase the fragment
    bar.querySelector("[data-undo]").onclick = () => { paths.pop(); repaint(); };
    bar.querySelector("[data-done]").onclick = e => {
      if (!paths.length) { teardown(); return; }
      bar.remove();
      layer.style.pointerEvents = "none";
      const covers = textUnderStrokes(slide, paths).filter(Boolean);
      inkComposer(e.clientX, Math.min(e.clientY + 10, innerHeight - 200), async text => {
        teardown();
        S.notes = true;
        const created = await api(`/api/bubbles/${S.slug}/talks/${S.talk.talk.id}/notes`, {
          method: "POST",
          body: JSON.stringify({ slide: S.slide, kind: "ink", paths, text, covers }),
        });
        if (innerWidth <= 900) root.classList.add("notes-open");
        await loadTalk(S.talk.talk.id, true);
        // The snapshot is the message here, so it is captured with the strokes repainted on
        // the fresh render — same lifecycle as a region mark's picture.
        await captureNote(created.note.id);
        await loadTalk(S.talk.talk.id, true);
      }, () => teardown());
    };
    const esc = e => { if (e.key === "Escape") { teardown(); e.stopPropagation(); } };
    addEventListener("keydown", esc, true);
  }

  // The five-kind picker makes no sense here — the drawing already is the mark. Just an
  // optional sentence and a pin.
  function inkComposer(x, y, onPin, onCancel) {
    closePicker();
    const p2 = h(`<div class="tk-pop">
      <div class="tk-kinds"><div class="tk-kb sel" style="--kc:${KINDS.ink.color};flex:1">
        <div class="g">${KINDS.ink.glyph}</div><div class="l">the drawing is the feedback</div></div></div>
      <textarea placeholder="optional — say what the drawing asks for"></textarea>
      <div class="row"><span class="hint">the drawing alone is enough</span>
        <button data-cancel="1">Esc</button><button class="pri" data-pin="1">Pin drawing</button></div>
    </div>`).firstChild;
    p2.style.left = Math.max(10, Math.min(x, innerWidth - 326)) + "px";
    p2.style.top = Math.min(y, innerHeight - 200) + "px";
    document.body.append(p2);
    const bail = () => { p2.remove(); if (onCancel) onCancel(); };
    p2.querySelector("[data-cancel]").onclick = bail;
    p2.querySelector("[data-pin]").onclick = () => {
      const text = p2.querySelector("textarea").value.trim();
      p2.remove();
      onPin(text);
    };
    setTimeout(() => p2.querySelector("textarea").focus(), 30);
    const esc2 = e => { if (e.key === "Escape") { bail(); removeEventListener("keydown", esc2, true); } };
    addEventListener("keydown", esc2, true);
  }

  /* -------------------------------------------------------- modals + chrome */
  function toast(msg) {
    const t = h(`<div style="position:fixed;left:50%;bottom:34px;transform:translateX(-50%);
      background:var(--panel);border:1px solid var(--accent);border-radius:10px;padding:10px 16px;
      z-index:980;font-size:13.5px;box-shadow:var(--shadow)">${esc(msg)}</div>`).firstChild;
    document.body.append(t);
    setTimeout(() => t.remove(), 3200);
  }

  async function showPayload() {
    const data = await api(`/api/bubbles/${encodeURIComponent(S.slug)}/talk-notes`);
    const payload = JSON.stringify(data.open_notes, null, 2);
    const m = h(`<div class="tk-modal"><div class="tk-mcard">
      <button class="tk-x" data-x="1" aria-label="Close">${LI_IC("close")}</button>
      <h3>Feedback YAML the agent can address and index</h3>
      <div class="msub">The Scientist receives this feedback YAML through its manifest poll and
        can address these marks in the linked source.</div>
      <div class="tk-jsonwrap"><button class="tk-json-copy" data-copy="1" title="Copy feedback YAML">Copy</button>
        <pre class="tk-json">${esc(payload)}</pre></div>
    </div></div>`).firstChild;
    const close = () => m.remove();
    m.onclick = e => { if (e.target === m) close(); };
    m.querySelector("[data-x]").onclick = close;
    m.querySelector("[data-copy]").onclick = async () => {
      const copy = m.querySelector("[data-copy]");
      try { await navigator.clipboard.writeText(payload); }
      catch (e) {
        const range = document.createRange(), sel = getSelection();
        range.selectNodeContents(m.querySelector(".tk-json")); sel.removeAllRanges(); sel.addRange(range);
        document.execCommand("copy"); sel.removeAllRanges();
      }
      copy.textContent = "Copied";
      setTimeout(() => (copy.textContent = "Copy"), 1400);
    };
    document.body.append(m);
  }

  function render() {
    if (!root || !root.querySelector(".tk-top") || !root.querySelector(".tk-body")) return;
    if (S.onView) S.onView(S.view);
    closePicker();
    clearPending();
    destroyEditor();
    root.classList.toggle("no-notes", !S.notes);
    document.querySelectorAll(".tk-modal").forEach(x => x.remove());
    const top = root.querySelector(".tk-top");
    const body = root.querySelector(".tk-body");
    body.innerHTML = "";
    if (S.view === "home") {
      top.innerHTML = "";
      body.append(renderHome());
    } else {
      const t = S.talk.talk;
      // One affordance, on the left. Leaving the talk is the title row's ‹ now, so this row only
      // has to offer the deck's other view — and the open count belongs beside the notes it
      // counts, not in the corner furthest from them.
      // In the contact sheet the slides themselves are the way back — clicking one opens it —
      // so a "back to slides" link would be a second door to a room you are already standing in.
      top.innerHTML = `<div class="tk-crumb">
          ${S.view === "sheet" ? "" : `<span class="back" data-sheet="1">all slides</span>`}
          <span class="dim">${S.view === "sheet" ? "" : " · "}${esc(t.date)} · ${esc(t.title)}</span></div>`;
      body.append(S.view === "sheet" ? renderSheet() : renderDeck());
    }
    // Root-wide: the edit toggle sits in the deck footer, not the title row.
    const on = (sel, fn) => { const b = root.querySelector(sel); if (b) b.onclick = fn; };
    on("[data-close]", close);
    on("[data-back]", loadHome);
    on("[data-sheet]", () => {
      if (S.edit && !leaveEditOk()) return;
      S.edit = false; S.view = S.view === "sheet" ? "deck" : "sheet"; render();
    });
    on("[data-editdeck]", () => {
      if (S.edit && !leaveEditOk()) return;
      S.edit = !S.edit; render();
    });
    publishRoute();
  }

  function onKey(e) {
    if (!root) return;
    if (e.key === "Escape") {
      if (document.querySelector(".tk-pop")) { dismissPicker(); return; }
      if (document.querySelector(".tk-modal")) {
        document.querySelectorAll(".tk-modal").forEach(x => x.remove()); return;
      }
      if (S.edit) { if (leaveEditOk()) { S.edit = false; render(); } return; }
      // Escape cancels what you opened — a picker, a modal, edit mode — and stops there. It
      // used to fall through to leaving the deck, so a stray Esc while reading threw away your
      // place in the talk. Leaving is deliberate: "all slides", or the ‹ in the title.
      return;
    }
    if (S.edit) return;   // the editor owns every key, including the arrows
    if (/INPUT|TEXTAREA/.test((document.activeElement || {}).tagName || "")) return;
    if (S.view !== "deck") return;
    if (e.key === "ArrowRight") go(S.slide + 1);
    if (e.key === "ArrowLeft") go(S.slide - 1);
  }

  function close() {
    stopTalkSync();
    closePicker();
    document.querySelectorAll(".tk-modal").forEach(x => x.remove());
    if (root) root.remove();
    root = null;
    document.removeEventListener("keydown", onKey);
  }

  // Mounted *into* the bubble's own pane rather than floating over it. The bubble's home is
  // not a place you visit on top of the bubble — it is what the bubble looks like before you
  // pick a page, so it gets the same real estate a page would.
  async function mount(host, slug, opts) {
    injectStyles();
    S.slug = slug;
    S.name = (opts && opts.name) || slug;
    S.user = (opts && opts.user) || "";
    S.owner = (opts && opts.owner) || "";
    S.workspaceId = (opts && opts.workspaceId) || "";
    S.onPage = (opts && opts.onPage) || null;
    S.onView = (opts && opts.onView) || null;
    S.onRoute = (opts && opts.onRoute) || null;
    S.view = "home";
    host.innerHTML = "";
    root = h(`<div class="tk-overlay tk-inline"><div class="tk-top"></div>
      <div class="tk-body"></div></div>`).firstChild;
    host.append(root);
    // Painted before the first await, replaced by loadHome's render: the shell used to arrive
    // seconds ahead of the content and look like a page that had failed to finish.
    root.querySelector(".tk-body").append(h(`<div class="tk-loading">
      <div class="tk-lrow"><span class="tk-spin"></span>Loading this bubble’s idea, document and chalk talks…</div>
      <div class="tk-skel"><i></i><i></i><i></i></div>
      <div class="tk-skel"><i></i><i></i><i></i></div>
    </div>`).firstChild);
    bindSlideSwipe(root);
    document.addEventListener("keydown", onKey);
    S.suppressRoute = true;
    try {
      await loadHome(false);
      if (opts && opts.talkId) {
        S.slide = Math.max(0, Number(opts.slide) || 0);
        S.edit = !!opts.edit;
        await loadTalk(opts.talkId, true);
      }
    }
    catch (e) {
      root.querySelector(".tk-body").innerHTML =
        `<div class="tk-list"><div class="tk-empty">Couldn’t load this bubble: ${esc(e.message)}</div></div>`;
    }
    finally {
      S.suppressRoute = false;
      publishRoute();
    }
  }

  /* =====================================================================================
   * Marks on report pages.
   *
   * Same five kinds, same picker, same gutter card as a chalk-talk slide — but the anchor is
   * the `<comment-begin=id>…<comment-end=id>` pair already in the page source, not a quoted string in a
   * sidecar. That difference is deliberate and it is the whole reason pages differ from slides:
   * a page is hand-edited constantly, and a wrapper *moves with the text it surrounds* while a
   * quote silently stops matching. The id in the wrapper is the mark's identity.
   *
   * This file owns the vocabulary and the look. Offsets, the wrapper and its storage stay in
   * the SPA, which already has all three.
   * ===================================================================================== */
  const M = { kind: "q", gutter: null, preview: null, handlers: null, user: "", owner: "" };

  function markPicker(x, y, quote, onPick) {
    injectStyles();   // a page view may never have opened a deck
    closePicker();
    const p = h(`<div class="tk-pop">
      <div class="tk-kinds">${ORDER.map(k => `<div class="tk-kb${k === M.kind ? " sel" : ""}"
        data-k="${k}" style="--kc:${KINDS[k].color}"><div class="g">${KINDS[k].glyph}</div>
        <div class="l">${KINDS[k].label}</div></div>`).join("")}</div>
      <div class="qt">“${esc(String(quote || "").slice(0, 120))}”</div>
      <textarea placeholder="optional — say more"></textarea>
      <div class="row"><span class="hint">a mark alone is enough</span>
        <button data-cancel="1">Esc</button><button class="pri" data-pin="1">Pin mark</button></div>
    </div>`).firstChild;
    p.style.left = Math.max(10, Math.min(x, innerWidth - 326)) + "px";
    p.style.top = Math.min(y, innerHeight - 230) + "px";
    document.body.append(p);
    p.querySelectorAll(".tk-kb").forEach(b => (b.onclick = () => {
      M.kind = b.dataset.k;
      p.querySelectorAll(".tk-kb").forEach(o => o.classList.toggle("sel", o === b));
    }));
    const bail = () => { closePicker(); clearPending(); };
    p.querySelector("[data-cancel]").onclick = bail;
    p.querySelector("[data-pin]").onclick = () => {
      const text = p.querySelector("textarea").value.trim();
      const kind = M.kind;
      bail();
      onPick(kind, text);
    };
    setTimeout(() => p.querySelector("textarea").focus(), 30);
    const esc2 = e => { if (e.key === "Escape") { bail(); removeEventListener("keydown", esc2, true); } };
    addEventListener("keydown", esc2, true);
  }

  // Pages and slides deliberately share this painter. Page source tags are stripped before
  // Markdown becomes HTML, then their quote anchors are painted here. That matters because two
  // ranges can cross: HTML cannot express crossing <mark> tags, whereas Range extraction below
  // preserves both visible annotations.
  // `onFocus` lets the page view answer a click on a highlight itself — it may have to open the
  // marks column before there is a card to scroll to, which this module cannot know about.
  function paintMarks(previewEl, threads, onFocus) {
    injectStyles();   // a page view may never have opened a deck
    if (!previewEl || !previewEl.isConnected) return;
    const focusPageNote = onFocus || (id => {
      const card = M.gutter && M.gutter.querySelector(`.tk-note[data-note="${id}"]`);
      if (!card) return;
        card.scrollIntoView({ block: "center", behavior: "smooth" });
        card.style.borderColor = "var(--accent)";
        setTimeout(() => (card.style.borderColor = ""), 900);
    });
    (threads || []).forEach(thread => {
      const anchor = thread.anchor || {};
      if (thread.status === "resolved" || thread.anchor_state === "unanchored" || !anchor.quote) return;
      highlightQuote(previewEl, anchor.quote,
        {id: thread.id, kind: (KINDS[thread.kind] ? thread.kind : "q")}, anchor.occurrence || 1,
        focusPageNote);
    });
  }

  function markGutter(gutterEl, threads, handlers) {
    injectStyles();   // a page view may never have opened a deck
    if (!gutterEl) return;
    if (gutterEl.querySelector("textarea")) {   // someone is mid-edit; do not pull the rug
      M.handlers = handlers || M.handlers;
      return;
    }
    M.gutter = gutterEl; M.handlers = handlers || {};
    const list = (threads || []).map(t => {
      const loose = t.anchor_state && t.anchor_state !== "attached";
      return { id: t.id, kind: t.kind || "q", author: "",
               quote: (t.anchor || {}).quote || "",
               messages: (t.messages || []).map(msg => ({ ...msg, agent: !!msg.agent })),
               orphan: loose, orphanNote: loose ? LI_IC("warning") + " its text was deleted · " : "" };
    });
    gutterEl.innerHTML = `
      <div class="tk-gh">Your marks<span class="tk-sp"></span>
        <span style="font:400 10px var(--font-mono)">${list.length}</span></div>
      ${list.length ? "" : `<div class="tk-empty" style="font-size:14px">
        Select any text in the rendered page and pick one of <b>${ORDER.map(k=>KINDS[k].glyph).join(" ")}</b>.</div>`}
      ${list.map(noteCard).join("")}`;
    wireCard(gutterEl, {
      onDelete: id => M.handlers.onDelete && M.handlers.onDelete(id),
      onEdit: (id, text) => M.handlers.onEdit && M.handlers.onEdit(id, text),
      onReply: (id, text) => M.handlers.onReply && M.handlers.onReply(id, text),
    });
  }

  window.LockedInTalks = { mount, close, home: loadHome, editTitles: editTalkTitles };
  window.LockedInMarks = { picker: markPicker, paint: paintMarks, gutter: markGutter,
                           // The doc page borrows the bottom-sheet gesture: its marks pane is
                           // the same header-plus-cards the chalk-talk gutter is. `open` names
                           // the class this instance toggles, since the two sheets style theirs
                           // from different roots.
                           sheet: (gutterEl, rootEl) => {
                             const gh = gutterEl && gutterEl.querySelector(".tk-gh");
                             if (gh && !gh._sheetBound) { gh._sheetBound = true; bindSheetDrag(gh, rootEl); }
                           },
                           setUser: (user, owner) => { M.user = String(user || ""); M.owner = String(owner || ""); },
                           pendingRange: paintPendingRange, clearPending };
})();
