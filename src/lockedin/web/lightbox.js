/* Full-screen figure viewer, shared by every rendered surface: the SPA's Split and Read panes,
 * the owner preview, and public share pages.
 *
 * It lives in one file because those surfaces are two different codebases — the SPA (index.html)
 * and the server-generated preview (_render_preview_html) — and browser logic duplicated across
 * them has drifted in this repo before.
 *
 * Usage: LockedInLightbox.watch("#previewWrap") — one delegated listener, so re-rendered pages
 * keep working without re-binding.
 */
(function () {
  "use strict";

  var MAX_SCALE = 8, MIN_SCALE = 1, DOUBLE_TAP_SCALE = 2.5;
  var overlay, stage, img, cap, hint, hintTimer;
  var scale = 1, tx = 0, ty = 0;           // current transform
  var baseW = 0, baseH = 0;                // the fitted (scale 1) size, in CSS pixels
  var pointers = new Map(), pinchDist = 0, pinchMid = null, panFrom = null;
  var lastTap = 0, scrollY = 0;

  var CSS = [
    '.li-lb{position:fixed;inset:0;z-index:9999;display:none;background:rgba(8,10,16,.94);',
    '  backdrop-filter:blur(2px);align-items:center;justify-content:center;overscroll-behavior:contain}',
    '.li-lb.on{display:flex}',
    '.li-lb-stage{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;',
    '  overflow:hidden;touch-action:none;cursor:zoom-in}',
    '.li-lb-stage.zoomed{cursor:grab}.li-lb-stage.zoomed:active{cursor:grabbing}',
    '.li-lb-img{max-width:94vw;max-height:88vh;transform-origin:center center;will-change:transform;',
    '  user-select:none;-webkit-user-drag:none;image-orientation:from-image}',
    '.li-lb-img.smooth{transition:transform .18s ease-out}',
    '.li-lb-bar{position:absolute;top:0;left:0;right:0;display:flex;gap:8px;align-items:center;',
    '  padding:10px 12px;pointer-events:none}',
    '.li-lb-bar button{pointer-events:auto;min-width:38px;height:38px;border-radius:10px;cursor:pointer;',
    '  border:1px solid rgba(255,255,255,.22);background:rgba(20,24,34,.72);color:#eef2f8;',
    '  font-size:15px;line-height:1;font-family:inherit}',
    '.li-lb-bar button:hover{background:rgba(40,46,62,.9)}',
    '.li-lb-bar .li-lb-spacer{flex:1}',
    '.li-lb-zoom{pointer-events:auto;color:#aab4c6;font-size:12px;font-variant-numeric:tabular-nums;',
    '  padding:0 6px;min-width:46px;text-align:center}',
    '.li-lb-cap{position:absolute;left:0;right:0;bottom:0;padding:12px 16px calc(12px + env(safe-area-inset-bottom));',
    '  color:#eef2f8;font-size:14.5px;line-height:1.5;text-align:center;pointer-events:none;',
    '  background:linear-gradient(to top,rgba(8,10,16,.85),transparent)}',
    /* Sits with the caption, never near the controls: a transient tip, not another button. */
    '.li-lb-hint{position:absolute;left:50%;bottom:58px;transform:translateX(-50%);color:#c8d0de;',
    '  font-size:12px;padding:6px 12px;border-radius:999px;background:rgba(20,24,34,.8);',
    '  border:1px solid rgba(255,255,255,.14);opacity:0;transition:opacity .3s;pointer-events:none}',
    '.li-lb-hint.show{opacity:1}',
    '.li-lb-cap .katex{font-size:1.18em;color:inherit}',
    '@media(max-width:700px){.li-lb-img{max-width:100vw;max-height:82vh}}',
    /* A figure is a control on these surfaces, so say so on hover. */
    '#previewWrap img,#content img{cursor:zoom-in}',
  ].join("");

  function injectCss() {
    if (document.getElementById("li-lb-css")) return;
    var style = document.createElement("style");
    style.id = "li-lb-css";
    style.textContent = CSS;
    (document.head || document.documentElement).appendChild(style);
  }

  function build() {
    if (overlay) return;
    injectCss();
    overlay = document.createElement("div");
    overlay.className = "li-lb";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "Figure viewer");
    overlay.innerHTML =
      '<div class="li-lb-stage"><img class="li-lb-img" alt=""></div>' +
      '<div class="li-lb-bar">' +
      '<button type="button" data-act="out" title="Zoom out" aria-label="Zoom out">−</button>' +
      '<span class="li-lb-zoom">100%</span>' +
      '<button type="button" data-act="in" title="Zoom in" aria-label="Zoom in">+</button>' +
      '<button type="button" data-act="reset" title="Fit to screen" aria-label="Fit to screen">⤢</button>' +
      '<span class="li-lb-spacer"></span>' +
      '<button type="button" data-act="open" title="Open original in a new tab" aria-label="Open original">↗</button>' +
      '<button type="button" data-act="close" title="Close (Esc)" aria-label="Close">✕</button>' +
      "</div>" +
      '<div class="li-lb-cap"></div><div class="li-lb-hint"></div>';
    stage = overlay.querySelector(".li-lb-stage");
    img = overlay.querySelector(".li-lb-img");
    cap = overlay.querySelector(".li-lb-cap");
    hint = overlay.querySelector(".li-lb-hint");
    document.body.appendChild(overlay);
    wire();
  }

  function zoomLabel() {
    overlay.querySelector(".li-lb-zoom").textContent = Math.round(scale * 100) + "%";
    stage.classList.toggle("zoomed", scale > 1.001);
  }

  /* Keep the image from being dragged off-screen: pan is bounded by how much of it overflows. */
  function clamp() {
    var maxX = Math.max(0, (baseW * scale - stage.clientWidth) / 2);
    var maxY = Math.max(0, (baseH * scale - stage.clientHeight) / 2);
    tx = Math.min(maxX, Math.max(-maxX, tx));
    ty = Math.min(maxY, Math.max(-maxY, ty));
  }

  function apply(smooth) {
    clamp();
    img.classList.toggle("smooth", !!smooth);
    img.style.transform = "translate(" + tx + "px," + ty + "px) scale(" + scale + ")";
    zoomLabel();
  }

  /* Zoom about a viewport point so the pixel under the cursor/fingers stays put. */
  function zoomAt(next, clientX, clientY, smooth) {
    next = Math.min(MAX_SCALE, Math.max(MIN_SCALE, next));
    var rect = stage.getBoundingClientRect();
    var px = (clientX == null ? rect.width / 2 : clientX - rect.left) - rect.width / 2;
    var py = (clientY == null ? rect.height / 2 : clientY - rect.top) - rect.height / 2;
    var ratio = next / scale;
    tx = px - (px - tx) * ratio;
    ty = py - (py - ty) * ratio;
    scale = next;
    if (scale <= MIN_SCALE + 0.001) { scale = MIN_SCALE; tx = 0; ty = 0; }
    apply(smooth);
  }

  function showHint(text) {
    hint.textContent = text;
    hint.classList.add("show");
    clearTimeout(hintTimer);
    hintTimer = setTimeout(function () { hint.classList.remove("show"); }, 2200);
  }

  function measure() {
    var r = img.getBoundingClientRect();
    // Undo the current scale to recover the fitted size.
    baseW = r.width / scale;
    baseH = r.height / scale;
  }

  function open(src, alt) {
    build();
    scale = 1; tx = 0; ty = 0;
    img.classList.remove("smooth");
    img.style.transform = "";
    img.src = src;
    img.alt = alt || "";
    setCaption(alt);
    overlay.classList.add("on");
    // Lock the page behind the overlay without losing the reader's place.
    scrollY = window.scrollY || 0;
    document.body.style.overflow = "hidden";
    // Establish the transform up front so the viewer is in a consistent state even while the
    // image is still downloading; remeasure once it has real dimensions.
    apply(false);
    var finish = function () { measure(); apply(false); };
    if (img.complete && img.naturalWidth) finish();
    else img.addEventListener("load", finish, { once: true });
    showHint(matchMedia("(pointer:coarse)").matches
      ? "Pinch to zoom · double-tap to fit"          // short enough to stay on one line on a phone
      : "Scroll to zoom · drag to move · Esc to close");
    overlay.querySelector('[data-act="close"]').focus({ preventScroll: true });
  }

  function close() {
    if (!overlay || !overlay.classList.contains("on")) return;
    overlay.classList.remove("on");
    document.body.style.overflow = "";
    if (scrollY) window.scrollTo({ top: scrollY });
    pointers.clear(); panFrom = null; pinchMid = null;
    img.src = "";
  }

  function wire() {
    overlay.querySelector(".li-lb-bar").addEventListener("click", function (e) {
      var btn = e.target.closest("button"); if (!btn) return;
      var act = btn.dataset.act;
      if (act === "close") close();
      else if (act === "in") zoomAt(scale * 1.5, null, null, true);
      else if (act === "out") zoomAt(scale / 1.5, null, null, true);
      else if (act === "reset") { scale = 1; tx = 0; ty = 0; apply(true); }
      else if (act === "open" && img.src) window.open(img.src, "_blank", "noopener");
    });

    // A click on the backdrop closes; a click on the image does not.
    stage.addEventListener("click", function (e) {
      if (e.target === stage && scale <= MIN_SCALE + 0.001) close();
    });

    stage.addEventListener("wheel", function (e) {
      e.preventDefault();
      zoomAt(scale * (e.deltaY < 0 ? 1.12 : 1 / 1.12), e.clientX, e.clientY, false);
    }, { passive: false });

    stage.addEventListener("dblclick", function (e) {
      e.preventDefault();
      if (scale > MIN_SCALE + 0.001) { scale = 1; tx = 0; ty = 0; apply(true); }
      else zoomAt(DOUBLE_TAP_SCALE, e.clientX, e.clientY, true);
    });

    stage.addEventListener("pointerdown", function (e) {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      stage.setPointerCapture(e.pointerId);
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pointers.size === 2) {
        var p = [...pointers.values()];
        pinchDist = Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y);
        pinchMid = { x: (p[0].x + p[1].x) / 2, y: (p[0].y + p[1].y) / 2 };
        panFrom = null;
      } else if (pointers.size === 1) {
        panFrom = { x: e.clientX, y: e.clientY, tx: tx, ty: ty };
        // Touch devices get no dblclick event, so detect the double-tap ourselves.
        if (e.pointerType !== "mouse") {
          var now = Date.now();
          if (now - lastTap < 300) {
            if (scale > MIN_SCALE + 0.001) { scale = 1; tx = 0; ty = 0; apply(true); }
            else zoomAt(DOUBLE_TAP_SCALE, e.clientX, e.clientY, true);
            lastTap = 0; panFrom = null; return;
          }
          lastTap = now;
        }
      }
    });

    stage.addEventListener("pointermove", function (e) {
      if (!pointers.has(e.pointerId)) return;
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pointers.size === 2 && pinchDist) {
        var p = [...pointers.values()];
        var dist = Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y);
        var mid = { x: (p[0].x + p[1].x) / 2, y: (p[0].y + p[1].y) / 2 };
        // Pan by the midpoint's motion as well, so a pinch can reposition at the same time.
        tx += mid.x - pinchMid.x; ty += mid.y - pinchMid.y;
        pinchMid = mid;
        zoomAt(scale * (dist / pinchDist), mid.x, mid.y, false);
        pinchDist = dist;
      } else if (pointers.size === 1 && panFrom && scale > MIN_SCALE + 0.001) {
        e.preventDefault();
        tx = panFrom.tx + (e.clientX - panFrom.x);
        ty = panFrom.ty + (e.clientY - panFrom.y);
        apply(false);
      }
    });

    ["pointerup", "pointercancel", "pointerleave"].forEach(function (type) {
      stage.addEventListener(type, function (e) {
        pointers.delete(e.pointerId);
        if (pointers.size < 2) { pinchDist = 0; pinchMid = null; }
        if (!pointers.size) panFrom = null;
      });
    });

    window.addEventListener("keydown", function (e) {
      if (!overlay.classList.contains("on")) return;
      if (e.key === "Escape") { e.preventDefault(); close(); }
      else if (e.key === "+" || e.key === "=") zoomAt(scale * 1.5, null, null, true);
      else if (e.key === "-") zoomAt(scale / 1.5, null, null, true);
      else if (e.key === "0") { scale = 1; tx = 0; ty = 0; apply(true); }
    });

    window.addEventListener("resize", function () {
      if (overlay.classList.contains("on")) { measure(); apply(false); }
    });
  }

  /* A figure's caption is its Markdown alt text, so it arrives with math still in source form
   * ($…$). Render it the way the page itself would, using the surface's own macros. */
  var macrosSource = null;
  var MATH = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\$([^$\n]+?)\$|\\\(([\s\S]+?)\\\)/g;

  function escapeHtml(text) {
    return text.replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function currentMacros() {
    try {
      var m = typeof macrosSource === "function" ? macrosSource() : macrosSource;
      return m || {};
    } catch (_) { return {}; }
  }

  function setCaption(alt) {
    alt = alt || "";
    cap.textContent = alt;                      // the safe default, and the fallback below
    if (!alt || typeof katex === "undefined" || !MATH.test(alt)) return;
    MATH.lastIndex = 0;
    var macros = currentMacros(), html = "", last = 0, match, ok = true;
    while ((match = MATH.exec(alt))) {
      var display = match[1] !== undefined || match[2] !== undefined;
      var src = match[1] !== undefined ? match[1]
              : match[2] !== undefined ? match[2]
              : match[3] !== undefined ? match[3] : match[4];
      html += escapeHtml(alt.slice(last, match.index));
      try {
        // Non-source text is escaped above; KaTeX emits its own safe markup for the math.
        html += katex.renderToString(src, { displayMode: false, macros: macros, throwOnError: true });
      } catch (_) { ok = false; break; }        // malformed math: keep the readable raw caption
      last = match.index + match[0].length;
    }
    if (!ok) return;
    cap.innerHTML = html + escapeHtml(alt.slice(last));
  }

  var watched = new Set();

  function watch(selector, options) {
    if (options && options.macros !== undefined) macrosSource = options.macros;
    if (watched.has(selector)) return;
    watched.add(selector);
    document.addEventListener("click", function (e) {
      var target = e.target;
      if (!target || target.tagName !== "IMG") return;
      if (!target.closest(selector)) return;
      // A linked figure keeps its link; only bare figures open the viewer.
      if (target.closest("a")) return;
      e.preventDefault();
      open(target.currentSrc || target.src, target.getAttribute("alt") || "");
    });
  }

  window.LockedInLightbox = { watch: watch, open: open, close: close };
})();
