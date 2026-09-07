#!/usr/bin/env node
/**
 * Real-browser review lifecycle regression.
 *
 * The test starts LockedIn against a disposable data root, signs up a disposable owner,
 * creates one bubble, and drives the production SPA in system Chrome. Nothing under the
 * repository's real data/ directory is read or changed.
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright-core";

const REPO = process.cwd();
const CHROME = process.env.LOCKEDIN_E2E_CHROME || "/usr/bin/google-chrome";
const SERVER_TIMEOUT_MS = 30_000;
const EDITOR = ".toastui-editor-md-container .ProseMirror";

function step(message) {
  process.stdout.write(`review-e2e: ${message}\n`);
}

async function freePort() {
  const socket = net.createServer();
  await new Promise((resolve, reject) => {
    socket.once("error", reject);
    socket.listen(0, "127.0.0.1", resolve);
  });
  const address = socket.address();
  const port = typeof address === "object" && address ? address.port : 0;
  await new Promise(resolve => socket.close(resolve));
  assert.ok(port, "could not allocate a local test port");
  return port;
}

async function waitForServer(baseUrl, child, output) {
  const deadline = Date.now() + SERVER_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`LockedIn test server exited early (${child.exitCode})\n${output()}`);
    }
    try {
      // The health route is intentionally session-protected; a prompt 401 still proves the
      // HTTP application is fully accepting requests. Only connection/5xx failures mean wait.
      const response = await fetch(`${baseUrl}/api/health`);
      if (response.status < 500) return;
    } catch (_) {
      // Startup is still in progress.
    }
    await delay(100);
  }
  throw new Error(`Timed out waiting for LockedIn test server\n${output()}`);
}

async function stopProcess(child) {
  if (!child || child.exitCode !== null) return;
  child.kill("SIGTERM");
  await Promise.race([
    new Promise(resolve => child.once("exit", resolve)),
    delay(3_000).then(() => child.kill("SIGKILL")),
  ]);
}

async function api(request, baseUrl, method, pathname, data) {
  const response = await request.fetch(`${baseUrl}${pathname}`, {
    method,
    data,
    failOnStatusCode: false,
  });
  const raw = await response.text();
  let body = {};
  try { body = raw ? JSON.parse(raw) : {}; } catch (_) { body = { raw }; }
  assert.ok(
    response.ok(),
    `${method} ${pathname} failed (${response.status()}): ${raw}`,
  );
  return body;
}

async function editorText(page) {
  return page.locator(EDITOR).evaluate(node => node.innerText);
}

async function selectSource(page, selected, occurrence = 0) {
  await page.locator(EDITOR).evaluate(
    (node, { selected, occurrence }) => {
      node.focus({ preventScroll: true });
      const spans = [];
      const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
      let source = "", textNode;
      while ((textNode = walker.nextNode())) {
        const value = String(textNode.nodeValue || "");
        if (!value) continue;
        spans.push({ node: textNode, start: source.length, end: source.length + value.length });
        source += value;
      }
      let start = -1;
      for (let count = 0, from = 0; count <= occurrence; count += 1) {
        start = source.indexOf(selected, from);
        if (start < 0) break;
        from = start + selected.length;
      }
      if (start < 0) throw new Error(`Selection text not found: ${selected}`);
      const end = start + selected.length;
      const first = spans.find(span => span.end > start);
      const last = [...spans].reverse().find(span => span.start < end);
      if (!first || !last) throw new Error("Could not map selection into ProseMirror DOM");
      const range = document.createRange();
      range.setStart(first.node, Math.max(0, start - first.start));
      range.setEnd(last.node, Math.min(last.end - last.start, end - last.start));
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      document.dispatchEvent(new Event("selectionchange", { bubbles: true }));
      node.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
      const rect = range.getBoundingClientRect();
      const candidates = [
        node,
        node.closest(".toastui-editor-md-container"),
        node.closest(".toastui-editor-main"),
        node.parentElement,
      ].filter(Boolean);
      const scroller = candidates.find(item => item.scrollHeight > item.clientHeight) || candidates[0];
      if (rect.height && scroller) scroller.scrollTop +=
        rect.top - scroller.getBoundingClientRect().top - 160;
    },
    { selected, occurrence },
  );
  // The SPA snapshots the CodeMirror range in requestAnimationFrame.
  await page.waitForTimeout(50);
}

async function replaceEditorText(page, content) {
  const editor = page.locator(EDITOR);
  await editor.click();
  await page.keyboard.press("Control+A");
  await page.keyboard.insertText(content);
}

async function placeSourceCaretAtEnd(page) {
  await page.locator(EDITOR).evaluate(node => {
    node.focus({ preventScroll: true });
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    let last = null, textNode;
    while ((textNode = walker.nextNode())) {
      if (String(textNode.nodeValue || "").length) last = textNode;
    }
    if (!last) throw new Error("Could not find the final ProseMirror source text node");
    const range = document.createRange();
    range.setStart(last, last.nodeValue.length);
    range.collapse(true);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    document.dispatchEvent(new Event("selectionchange", { bubbles: true }));
    node.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
  });
}

/** Select rendered text in the preview pane.
 *
 *  Marks are made on the rendered page, not on the Markdown source: `startMarkFromPreview`
 *  only fires for a selection inside `#previewWrap`. Selecting in the editor — which is what
 *  this suite used to do, back when a comment was a button on the source pane — opens nothing.
 */
async function selectPreview(page, selected, occurrence = 0, through = null) {
  return page.locator("#previewWrap").evaluate(
    (node, { selected, occurrence, through }) => {
      const spans = [];
      const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
      let text = "", textNode;
      while ((textNode = walker.nextNode())) {
        const value = String(textNode.nodeValue || "");
        if (!value) continue;
        spans.push({ node: textNode, start: text.length, end: text.length + value.length });
        text += value;
      }
      let start = -1;
      for (let count = 0, from = 0; count <= occurrence; count += 1) {
        start = text.indexOf(selected, from);
        if (start < 0) break;
        from = start + selected.length;
      }
      if (start < 0) throw new Error(`Selection text not found in the rendered page: ${selected}`);
      // `through` extends the range to a later anchor, which is how a selection spanning
      // typeset math is expressed: the rendered form of the math is not knowable from here.
      let end = start + selected.length;
      if (through) {
        const to = text.indexOf(through, end);
        if (to < 0) throw new Error(`Range end not found in the rendered page: ${through}`);
        end = to + through.length;
      }
      const first = spans.find(span => span.end > start);
      const last = [...spans].reverse().find(span => span.start < end);
      if (!first || !last) throw new Error("Could not map selection into the rendered DOM");
      const range = document.createRange();
      range.setStart(first.node, Math.max(0, start - first.start));
      range.setEnd(last.node, Math.min(last.end - last.start, end - last.start));
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      document.dispatchEvent(new Event("selectionchange"));
      node.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
      return range.toString();
    },
    { selected, occurrence, through },
  );
}

async function addComment(page, exactSelection, message) {
  const rendered = await selectPreview(page, exactSelection);
  await page.waitForTimeout(150);
  const selectionDiagnostic = await page.evaluate(() => {
    const native = window.getSelection();
    const result = {
      nativeText: native ? native.toString() : "",
      activeClass: document.activeElement?.className || "",
    };
    try {
      if (typeof S !== "undefined") {
        result.reviewSelection = S.reviewSelection || null;
        result.editorSelection = S.editor?.getSelection?.() || null;
      }
    } catch (error) {
      result.stateError = String(error);
    }
    return result;
  });
  const composer = page.locator(".tk-pop textarea");
  try {
    await composer.waitFor({ state: "visible", timeout: 2_000 });
  } catch (error) {
    const toastText = await page.locator("#toast").textContent().catch(() => "");
    throw new Error(
      `The mark picker did not open; toast=${JSON.stringify(toastText)} ` +
      `selection=${JSON.stringify(selectionDiagnostic)}\n${error.message}`,
    );
  }
  // Opening the composer moves focus off the editor, which clears the browser's own selection.
  // The chosen text must stay visibly marked so the reviewer can still see what they are
  // commenting on while they type.
  await page.waitForFunction(expected => {
    const highlight = globalThis.CSS?.highlights?.get("lockedin-review-draft");
    return !!highlight && Array.from(highlight).some(range => range.toString() === expected);
  }, rendered, { timeout: 5_000 });
  await composer.fill(message);
  await page.waitForFunction(expected => {
    const highlight = globalThis.CSS?.highlights?.get("lockedin-review-draft");
    return !!highlight && Array.from(highlight).some(range => range.toString() === expected);
  }, rendered, { timeout: 2_000 });
  const responsePromise = page.waitForResponse(response => {
    const url = new URL(response.url());
    return response.request().method() === "POST" && /\/comments$/.test(url.pathname);
  });
  await page.locator(".tk-pop [data-pin]").click();
  const response = await responsePromise;
  assert.equal(response.status(), 200, await response.text());
  try {
    await page.waitForFunction(expected => {
      const highlight = globalThis.CSS?.highlights?.get("lockedin-review");
      return !!highlight && Array.from(highlight).some(range => range.toString() === expected);
    }, rendered, { timeout: 5_000 });
  } catch (error) {
    const ranges = await highlightedText(page);
    const visibleSource = await editorText(page);
    throw new Error(
      `${error.message}\nExpected highlight: ${JSON.stringify(rendered)}` +
      `\nActual highlights: ${JSON.stringify(ranges)}` +
      `\nVisible source contains selection: ${visibleSource.includes(exactSelection)}`,
    );
  }
  // Once the thread exists, its own anchored highlight takes over and the draft paint must go.
  await page.waitForFunction(() => !globalThis.CSS?.highlights?.get("lockedin-review-draft"),
    undefined, { timeout: 5_000 });
  return response;
}

async function highlightedText(page) {
  return page.evaluate(() => {
    const highlight = globalThis.CSS?.highlights?.get("lockedin-review");
    return highlight ? Array.from(highlight, range => range.toString()) : [];
  });
}

async function textColorHighlightText(page) {
  return page.evaluate(() => Array.from(globalThis.CSS?.highlights?.keys?.() || [])
    .filter(name => String(name).startsWith("lockedin-textcolor-"))
    .flatMap(name => Array.from(globalThis.CSS.highlights.get(name), range => range.toString())));
}

async function clickExactHighlight(page, exactText) {
  const point = await page.evaluate(expected => {
    const highlight = globalThis.CSS?.highlights?.get("lockedin-review");
    if (!highlight) return null;
    const range = Array.from(highlight).find(item => item.toString() === expected);
    if (!range) return null;
    const rect = Array.from(range.getClientRects()).find(item => item.width > 0 && item.height > 0);
    return rect ? { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 } : null;
  }, exactText);
  assert.ok(point, `no clickable highlight rectangle for ${JSON.stringify(exactText)}`);
  await page.mouse.click(point.x, point.y);
  await page.waitForSelector("#reviewWrap .review-thread.selected[open]");
}

async function sourceScrollTop(page) {
  return page.locator(EDITOR).evaluate(node => {
    const candidates = [
      node,
      node.closest(".toastui-editor-md-container"),
      node.closest(".toastui-editor-main"),
      node.parentElement,
    ].filter(Boolean);
    const scroller = candidates.find(item => item.scrollHeight > item.clientHeight) || candidates[0];
    return scroller ? scroller.scrollTop : 0;
  });
}

async function main() {
  assert.ok(fs.existsSync(CHROME), `Chrome is not installed at ${CHROME}`);
  const dataRoot = fs.mkdtempSync(path.join(os.tmpdir(), "lockedin-review-e2e-"));
  const port = await freePort();
  const baseUrl = `http://127.0.0.1:${port}`;
  let serverOutput = "";
  let child;
  let browser;

  try {
    child = spawn(
      "uv",
      ["run", "lockedin", "serve", "--host", "127.0.0.1", "--port", String(port)],
      {
        cwd: REPO,
        env: {
          ...process.env,
          LOCKEDIN_HOME: dataRoot,
          LOCKEDIN_INSECURE_COOKIE: "1",
          PYTHONUNBUFFERED: "1",
        },
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    child.stdout.on("data", chunk => { serverOutput += chunk; });
    child.stderr.on("data", chunk => { serverOutput += chunk; });
    await waitForServer(baseUrl, child, () => serverOutput);
    step("disposable server is ready");

    browser = await chromium.launch({
      executablePath: CHROME,
      headless: true,
      args: ["--no-sandbox", "--disable-dev-shm-usage"],
    });
    const context = await browser.newContext({ viewport: { width: 1500, height: 950 } });
    const username = `review-e2e-${Date.now()}`;
    await api(context.request, baseUrl, "POST", "/api/signup", {
      username,
      password: "temporary-review-password",
    });
    const created = await api(context.request, baseUrl, "POST", "/api/bubbles", {
      name: "Review lifecycle E2E",
    });
    const slug = created.slug;
    // Keep this setup explicit even if user-created bubbles default to approved. It makes the
    // browser fixture resilient to deployments that still preserve the older approval gate.
    await api(context.request, baseUrl, "POST", `/api/bubbles/${slug}/approve`, {
      instructions: "",
    });
    const detail = await api(context.request, baseUrl, "GET", `/api/bubbles/${slug}`);
    const pageSlug = detail.bubble.home || "overview";
    const exactSelection =
      "Flow-matching remains stable across edits and keeps the review exact.";
    const mathSelection =
      "Typeset math like $x_{t+1}=f(x_t)$ sits inside this sentence.";
    const deleteSelection = "This disposable sentence will receive a deletable review.";
    const unanchorSelection = "This selected passage will be removed and become unanchored.";
    const filler = Array.from(
      { length: 90 },
      (_, index) => `Background paragraph ${index}: ${"context ".repeat(14)}`,
    ).join("\n\n");
    const initialContent =
      `# Review lifecycle\n\n${filler}\n\nEmoji offset guard 😀 precedes this review.\n\n${exactSelection}\n\n${mathSelection}\n\n` +
      `${"More report context. ".repeat(120)}\n\n${deleteSelection}\n\n${unanchorSelection}\n`;
    await api(
      context.request,
      baseUrl,
      "PUT",
      `/api/bubbles/${slug}/pages/${pageSlug}`,
      { content: initialContent, base_mtime: null },
    );

    await context.addInitScript(slugForSettings => {
      localStorage.setItem(`lockedin:viewMode:${slugForSettings}`, "edit");
      localStorage.setItem(`lockedin:review:${slugForSettings}`, "1");
    }, slug);
    const page = await context.newPage();
    const requests = [];
    let mainNavigations = 0;
    page.on("request", request => {
      const url = new URL(request.url());
      if (url.origin === baseUrl) {
        requests.push({ method: request.method(), path: url.pathname, at: Date.now() });
      }
    });
    page.on("framenavigated", frame => {
      if (frame === page.mainFrame()) mainNavigations += 1;
    });
    page.on("console", message => {
      if (message.type() === "error") serverOutput += `\n[browser] ${message.text()}`;
    });
    await page.goto(`${baseUrl}/#bubble/${slug}/${pageSlug}`, { waitUntil: "domcontentloaded" });
    try {
      await page.waitForSelector(EDITOR, { state: "attached", timeout: 30_000 });
    } catch (error) {
      const diagnostics = await page.evaluate(() => ({
        hash: location.hash,
        toastEditorLoaded: typeof globalThis.toastui?.Editor === "function",
        editorHost: !!document.querySelector("#editorHost"),
        editorWrapText: document.querySelector("#editorWrap")?.textContent?.slice(0, 160) || "",
        mainText: document.querySelector("#main")?.textContent?.slice(0, 500) || "",
      }));
      throw new Error(`${error.message}\nBrowser diagnostics: ${JSON.stringify(diagnostics)}`);
    }
    if (!(await page.locator(EDITOR).isVisible())) {
      await page.locator("#paneLeftToggle").click();
      await page.locator(EDITOR).waitFor({ state: "visible" });
    }
    await page.waitForFunction(expected => {
      const node = document.querySelector(".toastui-editor-md-container .ProseMirror");
      return node?.innerText?.includes(expected);
    }, exactSelection);
    if (process.env.LOCKEDIN_E2E_DEBUG) {
      const dump = await page.locator(EDITOR).evaluate(node => ({
        outerHTML: node.outerHTML.slice(0, 1800),
        textContent: node.textContent.slice(0, 1200),
        innerText: node.innerText.slice(0, 1200),
        ownKeys: Object.keys(node),
        descriptorKeys: node.pmViewDesc ? Object.keys(node.pmViewDesc) : [],
        descriptorViewKeys: node.pmViewDesc?.view ? Object.keys(node.pmViewDesc.view) : [],
        children: Array.from(node.children).slice(0, 8).map(child => ({
          tag: child.tagName,
          className: child.className,
          textContent: child.textContent,
          innerText: child.innerText,
          html: child.outerHTML.slice(0, 500),
        })),
      }));
      process.stdout.write(`review-e2e DOM: ${JSON.stringify(dump)}\n`);
    }
    await page.waitForTimeout(400);
    step("authenticated report editor loaded");

    // setSync used to write SYNC_ICON[state] — a bare word like "check" or "pencil" — straight
    // into the button's textContent, and every call site but this one already went through ic().
    // It only broke on the first state change, which is why it looked like the mark had gone
    // missing rather than never arriving. Drive stale -> saving -> synced by editing, so the
    // check covers a real transition rather than only the button's initial mount.
    const syncButton = page.locator("#syncToolbarButton");
    const syncIconState = () => syncButton.evaluate(node => ({
      text: node.textContent.trim(),
      hasIcon: !!node.querySelector("svg,.li-ic"),
      className: node.className,
    }));
    await page.locator(EDITOR).click();
    await page.waitForFunction(() =>
      !!(document.activeElement && document.activeElement.closest(".ProseMirror")));
    await page.keyboard.press("End");
    await page.keyboard.type(" ");
    await page.waitForFunction(() =>
      document.querySelector("#syncToolbarButton")?.className.includes("stale"));
    let syncState = await syncIconState();
    assert.notEqual(syncState.text, "pencil",
      "the sync button must not print the literal icon name as text");
    assert.ok(syncState.hasIcon, "the sync button must render an icon element, not bare text");
    await page.waitForFunction(() =>
      document.querySelector("#syncToolbarButton")?.className.includes("synced"),
    null, { timeout: 5_000 });
    syncState = await syncIconState();
    assert.notEqual(syncState.text, "check",
      "the sync button must never print the literal word \"check\"");
    assert.ok(syncState.hasIcon, "the sync button must render an icon element after autosave completes");
    step("sync toolbar button shows an icon, never literal text, across a state change");

    // Ctrl/Cmd+B toggles the sidebar from anywhere in the app, including with the caret in the
    // report editor: main removed Toast UI's own Mod-b (bold) via blockEditorShortcuts, which
    // swallows it in the capture phase before the editor's keymap runs, so bold no longer
    // competes for the chord and the sidebar answers unconditionally instead.
    const sideCollapsed = () => page.evaluate(() => document.body.classList.contains("side-collapsed"));
    await page.evaluate(() => document.activeElement && document.activeElement.blur());
    const collapsedAtStart = await sideCollapsed();
    await page.keyboard.press("Control+b");
    await page.waitForFunction(was =>
      document.body.classList.contains("side-collapsed") !== was, collapsedAtStart);
    assert.equal(await sideCollapsed(), !collapsedAtStart,
      "Ctrl+B must toggle the sidebar from an ordinary view");
    await page.keyboard.press("Control+b");
    await page.waitForFunction(was =>
      document.body.classList.contains("side-collapsed") === was, collapsedAtStart);
    step("Ctrl+B toggles the sidebar from an ordinary view");

    await page.locator(EDITOR).click();
    await page.waitForFunction(() =>
      !!(document.activeElement && document.activeElement.closest(".ProseMirror")));
    const markdownBeforeBold = await page.evaluate(() => globalThis.S?.editor?.getMarkdown?.() || "");
    const collapsedBeforeBold = await sideCollapsed();
    await page.keyboard.press("Control+b");
    await page.waitForFunction(was =>
      document.body.classList.contains("side-collapsed") !== was, collapsedBeforeBold);
    assert.equal(await sideCollapsed(), !collapsedBeforeBold,
      "Ctrl+B must toggle the sidebar even with the caret inside the report editor");
    const markdownAfterBold = await page.evaluate(() => globalThis.S?.editor?.getMarkdown?.() || "");
    assert.equal(markdownAfterBold, markdownBeforeBold,
      "Ctrl+B must not insert bold markup into the report while toggling the sidebar");
    await page.keyboard.press("Control+b");
    await page.waitForFunction(was =>
      document.body.classList.contains("side-collapsed") === was, collapsedBeforeBold);
    step("Ctrl+B toggles the sidebar with the caret inside the editor, without inserting bold");

    // Ctrl+Shift+` toggles the marks pane even with the caret inside the report editor —
    // that is the whole reason it is bound in the capture phase, ahead of the editor's own
    // keymap. Linux's modifier is Control, matched by Playwright's "Control" name.
    const editorHost = page.locator("#editorHost");
    // This viewport is wide enough that the marks pane defaults closed (it only defaults open
    // on a phone-width bubble). Force it open first so the chord's before/after is unambiguous.
    if (await editorHost.evaluate(node => node.classList.contains("marks-off"))) {
      await page.locator("#paneRightToggle").click();
      await page.waitForFunction(() => !document.querySelector("#editorHost")?.classList.contains("marks-off"));
    }
    await page.locator(EDITOR).click();
    await page.waitForFunction(() =>
      !!(document.activeElement && document.activeElement.closest(".ProseMirror")));
    assert.ok(!(await editorHost.evaluate(node => node.classList.contains("marks-off"))),
      "the marks pane starts open");
    await page.keyboard.press("Control+Shift+`");
    await page.waitForFunction(() =>
      document.querySelector("#editorHost")?.classList.contains("marks-off"));
    assert.ok(await editorHost.evaluate(node => node.classList.contains("marks-off")),
      "Ctrl+Shift+` must hide the marks pane, even while the caret is in the editor");
    await page.keyboard.press("Control+Shift+`");
    await page.waitForFunction(() =>
      !document.querySelector("#editorHost")?.classList.contains("marks-off"));
    assert.ok(!(await editorHost.evaluate(node => node.classList.contains("marks-off"))),
      "pressing it again must bring the marks pane back");
    step("Ctrl+Shift+` toggles the marks pane from inside the editor");

    // Alt+Enter toggles the same focused workspace #bubbleFocusToggle does, from anywhere in
    // the bubble view, including with the caret still in the editor from the check above.
    const app = page.locator("#app");
    assert.ok(!(await app.evaluate(node => node.classList.contains("bubble-focus"))),
      "focused workspace starts closed");
    await page.keyboard.press("Alt+Enter");
    await page.waitForFunction(() => document.getElementById("app")?.classList.contains("bubble-focus"));
    assert.ok(await app.evaluate(node => node.classList.contains("bubble-focus")),
      "Alt+Enter must enter the focused workspace");
    await page.keyboard.press("Alt+Enter");
    await page.waitForFunction(() => !document.getElementById("app")?.classList.contains("bubble-focus"));
    assert.ok(!(await app.evaluate(node => node.classList.contains("bubble-focus"))),
      "pressing it again must leave the focused workspace");
    step("Alt+Enter toggles the focused workspace from inside the editor");

    // Right Alt is AltGr on most non-US layouts, and browsers report it inconsistently:
    // Linux delivers key "AltGraph" with altKey false, so getModifierState is the only way
    // to see it, while Windows delivers Ctrl+Alt held together. Playwright's keyboard.press
    // cannot reliably produce AltGraph modifier state, so dispatch synthetic keydown events
    // from the page instead, on document.activeElement so they still reach the capture-phase
    // listener bound on document.
    async function dispatchEnter(init) {
      await page.evaluate(init => {
        document.activeElement.dispatchEvent(new KeyboardEvent("keydown", {
          code: "Enter", key: "Enter", bubbles: true, cancelable: true, ...init,
        }));
      }, init);
    }
    // Probed on an inert code (not Enter) so this check alone never trips the real handler
    // and consumes a toggle before the assertions below run.
    const altGraphWorks = await page.evaluate(() => {
      let seen = false;
      const probe = e => { seen = e.getModifierState("AltGraph"); };
      document.addEventListener("keydown", probe, { capture: true, once: true });
      document.activeElement.dispatchEvent(new KeyboardEvent("keydown", {
        code: "KeyQ", key: "q", bubbles: true, cancelable: true, modifierAltGraph: true,
      }));
      document.removeEventListener("keydown", probe, { capture: true });
      return seen;
    });
    if (altGraphWorks) {
      await dispatchEnter({ modifierAltGraph: true });
      await page.waitForFunction(() => document.getElementById("app")?.classList.contains("bubble-focus"));
      assert.ok(await app.evaluate(node => node.classList.contains("bubble-focus")),
        "AltGr reported as AltGraph (Linux-style) must enter the focused workspace");
      await dispatchEnter({ modifierAltGraph: true });
      await page.waitForFunction(() => !document.getElementById("app")?.classList.contains("bubble-focus"));
      assert.ok(!(await app.evaluate(node => node.classList.contains("bubble-focus"))),
        "pressing it again must leave the focused workspace");
    } else {
      step("this Chromium's getModifierState(\"AltGraph\") did not honour modifierAltGraph in the init dict; skipping the Linux AltGr shape");
    }
    await dispatchEnter({ ctrlKey: true, altKey: true });
    await page.waitForFunction(() => document.getElementById("app")?.classList.contains("bubble-focus"));
    assert.ok(await app.evaluate(node => node.classList.contains("bubble-focus")),
      "AltGr reported as Ctrl+Alt (Windows-style) must enter the focused workspace");
    await dispatchEnter({ ctrlKey: true, altKey: true });
    await page.waitForFunction(() => !document.getElementById("app")?.classList.contains("bubble-focus"));
    assert.ok(!(await app.evaluate(node => node.classList.contains("bubble-focus"))),
      "pressing it again must leave the focused workspace");
    await dispatchEnter({ altKey: true });
    await page.waitForFunction(() => document.getElementById("app")?.classList.contains("bubble-focus"));
    assert.ok(await app.evaluate(node => node.classList.contains("bubble-focus")),
      "plain left Alt+Enter must still enter the focused workspace, unchanged by the AltGr fix");
    await dispatchEnter({ altKey: true });
    await page.waitForFunction(() => !document.getElementById("app")?.classList.contains("bubble-focus"));
    assert.ok(!(await app.evaluate(node => node.classList.contains("bubble-focus"))),
      "pressing it again must leave the focused workspace");
    step("AltGr, in both its Linux and Windows shapes, and plain left Alt all toggle the focused workspace");

    // Inside focus mode the sidebar is not on screen at all, so e5e3d3b left Ctrl+B quiet
    // there (applySideCollapsed writes gridTemplateColumns inline, which outranks
    // #app.bubble-focus's single grid track, so toggling the sidebar from there used to snap
    // the document into the vanished sidebar's 220px). That idle key now drives the document
    // page's own left pane instead — the same signal applyPanes flips, #editorHost's
    // mode-split/mode-view class — while leaving the sidebar itself untouched.
    await page.keyboard.press("Alt+Enter");
    await page.waitForFunction(() => document.getElementById("app")?.classList.contains("bubble-focus"));
    const collapsedInFocus = await sideCollapsed();
    const paneLeftInFocus = await editorHost.evaluate(node => node.classList.contains("mode-split"));
    await page.keyboard.press("Control+b");
    await page.waitForFunction(was =>
      document.querySelector("#editorHost")?.classList.contains("mode-split") !== was, paneLeftInFocus);
    assert.equal(await sideCollapsed(), collapsedInFocus,
      "Ctrl+B must do nothing to the sidebar while the focused workspace is on");
    assert.equal(await editorHost.evaluate(node => node.classList.contains("mode-split")), !paneLeftInFocus,
      "Ctrl+B must toggle the document page's left pane while the focused workspace is on");
    assert.ok(await page.locator("#app").evaluate(node => node.classList.contains("bubble-focus")),
      "Ctrl+B must not exit the focused workspace either");
    await page.keyboard.press("Control+b");
    await page.waitForFunction(was =>
      document.querySelector("#editorHost")?.classList.contains("mode-split") === was, paneLeftInFocus);
    assert.equal(await sideCollapsed(), collapsedInFocus,
      "pressing it again must still leave the sidebar alone");
    assert.equal(await editorHost.evaluate(node => node.classList.contains("mode-split")), paneLeftInFocus,
      "pressing it again must flip the left pane back");
    step("Ctrl+B toggles the document page's left pane, not the sidebar, while the focused workspace is on");

    // tests/review-e2e.mjs never opens a chalk talk deck, so the case where focus mode is on
    // but there is no left pane at all (LockedInTalks' notes pane rather than #editorHost) is
    // not covered here; that surface would need a deck fixture this file does not have.
    await page.keyboard.press("Alt+Enter");
    await page.waitForFunction(() => !document.getElementById("app")?.classList.contains("bubble-focus"));
    const collapsedAfterFocus = await sideCollapsed();
    await page.keyboard.press("Control+b");
    await page.waitForFunction(was =>
      document.body.classList.contains("side-collapsed") !== was, collapsedAfterFocus);
    assert.equal(await sideCollapsed(), !collapsedAfterFocus,
      "leaving focus mode must restore Ctrl+B's sidebar toggle");
    await page.keyboard.press("Control+b");
    await page.waitForFunction(was =>
      document.body.classList.contains("side-collapsed") === was, collapsedAfterFocus);
    step("Ctrl+B goes back to toggling the sidebar once the focused workspace is off");

    const colorSelection = "Emoji offset guard 😀 precedes this review.";
    // Swatch hexes come from the active theme's --text-color-N variables, so the test reads
    // them from the live palette instead of pinning values that change with the theme.
    const swatches = await page.evaluate(() =>
      Array.from(document.querySelectorAll(".color-tool .color-swatch"), node => node.title));
    assert.ok(swatches.length >= 2, `expected a text-color palette, saw ${JSON.stringify(swatches)}`);
    const [firstColor, secondColor] = swatches;
    await selectSource(page, colorSelection);
    await page.getByTitle("Color selected text").click();
    await page.getByTitle(firstColor, { exact: true }).click();
    try {
      await page.waitForFunction(expected =>
        document.querySelector(".toastui-editor-md-container .ProseMirror")?.innerText.includes(expected),
      `\\textcolor{${firstColor}}{${colorSelection}}`, { timeout: 2_000 });
    } catch (error) {
      const diagnostic = await page.evaluate(() => ({
        toast: document.querySelector("#toast")?.textContent || "",
        markdown: globalThis.S?.editor?.getMarkdown?.() || "",
        selection: globalThis.S?.colorSelection || null,
      }));
      throw new Error(`text-color insertion did not update the editor: ${JSON.stringify(diagnostic)}\n${error.message}`);
    }
    await page.waitForTimeout(800);
    await page.waitForFunction(expected => Array.from(globalThis.CSS?.highlights?.keys?.() || [])
      .filter(name => String(name).startsWith("lockedin-textcolor-"))
      .some(name => Array.from(globalThis.CSS.highlights.get(name)).some(range => range.toString() === expected)),
    colorSelection);
    assert.deepEqual(await textColorHighlightText(page), [colorSelection]);
    const coloredSource = (await api(
      context.request, baseUrl, "GET", `/api/bubbles/${slug}/pages/${pageSlug}`,
    )).content;
    assert.ok(coloredSource.includes(`\\textcolor{${firstColor}}{${colorSelection}}`));
    const colorOverlapStart = requests.length;
    await selectSource(page, "offset guard");
    await page.getByTitle("Color selected text").click();
    await page.getByTitle(secondColor, { exact: true }).click();
    await page.waitForFunction(() => /overlap|nested|intersect/i.test(document.querySelector("#toast")?.textContent || ""));
    assert.equal(
      requests.slice(colorOverlapStart).filter(item =>
        item.method === "PUT" && item.path === `/api/bubbles/${slug}/pages/${pageSlug}`
      ).length,
      0,
      "an intersecting text color reached the server",
    );
    step("text colors highlight only their exact body and reject overlap before saving");

    // A mark must anchor to a range in the source, so a selection that straddles typeset math
    // cannot be mapped back and the picker stays shut rather than offering a mark that could
    // not be placed.
    await selectPreview(page, "Typeset math like", 0, "sits inside this sentence.");
    await page.waitForTimeout(400);
    assert.equal(await page.locator(".tk-pop").count(), 0,
      "a selection running through typeset math must not open the picker");
    await page.evaluate(() => getSelection().removeAllRanges());
    step("a selection that straddles typeset math cannot become a mark");

    /* The mark lifecycle — draft, commit, open, resolve, delete, reload — used to be covered
       from here down. That coverage was written against the review UI this product no longer
       has: comments were painted with the CSS Highlight API, opened a `.review-thread` in the
       gutter, and were composed in a textarea on the source pane. Marks replaced all three —
       they are DOM elements painted onto the rendered page, their gutter is `.tk-note`, and
       they are composed in the picker. Re-asserting the old mechanism would have meant a test
       that passes without describing the product, so it is gone rather than reworded.

       What remains below is what still holds: text colours, the guard above, and the malformed
       wrapper. A browser test for the mark lifecycle is a real gap and wants writing fresh. */


    const serverCopyBeforeMalformed = (
      await api(context.request, baseUrl, "GET", `/api/bubbles/${slug}/pages/${pageSlug}`)
    ).content;
    await placeSourceCaretAtEnd(page);
    await page.keyboard.insertText("\n<comment-begin=broken-variable-id>never closed");
    await page.waitForFunction(() =>
      document.querySelector(".toastui-editor-md-container .ProseMirror")?.innerText
        .includes("broken-variable-id"),
    );
    const renderError = page.locator(".review-editor-error");
    await renderError.waitFor();
    assert.match(await renderError.textContent(), /line\s+\d+.*column\s+\d+/i);
    const malformedRequestStart = requests.length;
    await page.locator("#syncToolbarButton").click();
    await page.waitForTimeout(450);
    assert.equal(
      requests.slice(malformedRequestStart).filter(item =>
        item.method === "PUT" && item.path === `/api/bubbles/${slug}/pages/${pageSlug}`
      ).length,
      0,
      "manual save sent malformed review markup to the server",
    );
    const serverCopyAfterMalformed = (
      await api(context.request, baseUrl, "GET", `/api/bubbles/${slug}/pages/${pageSlug}`)
    ).content;
    assert.equal(serverCopyAfterMalformed, serverCopyBeforeMalformed, "malformed save changed server content");
    step("malformed wrapper showed line/column error and blocked manual save");

    await replaceEditorText(page, serverCopyBeforeMalformed);
    await page.waitForFunction(() => !document.querySelector(".review-render-error"));

    // Away from any bubble page or deck, the chord's guard must leave it doing nothing: no
    // #editorHost to toggle, no LockedInTalks deck open, and no error thrown reaching for either.
    await page.locator('.navbtn[data-view="home"]').click();
    await page.waitForFunction(() => !document.querySelector("#editorHost"));
    await page.keyboard.press("Control+Shift+`");
    await page.waitForTimeout(200);
    assert.equal(await page.locator("#editorHost").count(), 0,
      "the home view must not grow a marks pane out of the chord");
    assert.ok(await page.locator('.navbtn[data-view="home"]').evaluate(node => node.classList.contains("active")),
      "the chord must not disturb the home view when no bubble surface is on screen");
    step("Ctrl+Shift+` is inert on a non-bubble view");

    // Same guard, same view: S.bubble is unset on the home view, so Alt+Enter must not
    // enter the focused workspace either.
    await page.keyboard.press("Alt+Enter");
    await page.waitForTimeout(200);
    assert.ok(!(await page.locator("#app").evaluate(node => node.classList.contains("bubble-focus"))),
      "the home view must not enter the focused workspace out of the chord");
    step("Alt+Enter is inert on a non-bubble view");

    // The bubble *home* (the page/chalk-talk listing, before any page is opened) satisfies
    // S.bubble just like a document page does, but it has no #bubbleFocusToggle button — so
    // the chord must stay inert there too, not just on the app-level home view above.
    await page.goto(`${baseUrl}/#bubble/${slug}`, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => !document.querySelector("#editorHost"));
    assert.equal(await page.locator("#bubbleFocusToggle").count(), 0,
      "the bubble home must not have a full-screen toggle to guard");
    await page.keyboard.press("Alt+Enter");
    await page.waitForTimeout(200);
    assert.ok(!(await page.locator("#app").evaluate(node => node.classList.contains("bubble-focus"))),
      "the bubble home must not enter the focused workspace out of the chord");
    step("Alt+Enter is inert on the bubble home too");

    await context.close();
    step("all browser review lifecycle checks passed");
  } catch (error) {
    if (serverOutput) process.stderr.write(`\n--- test server/browser output ---\n${serverOutput}\n`);
    throw error;
  } finally {
    if (browser) await browser.close().catch(() => {});
    await stopProcess(child);
    fs.rmSync(dataRoot, { recursive: true, force: true });
  }
}

main().catch(error => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
