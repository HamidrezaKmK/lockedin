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

async function addComment(page, exactSelection, message) {
  await selectSource(page, exactSelection);
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
  await page.getByRole("button", { name: "Add comment to selected text" }).click();
  const composer = page.locator(
    '#reviewWrap .review-compose textarea[placeholder="Add a review comment…"]',
  );
  try {
    await composer.waitFor({ state: "visible", timeout: 2_000 });
  } catch (error) {
    const toastText = await page.locator("#toast").textContent().catch(() => "");
    throw new Error(
      `Comment composer did not open; toast=${JSON.stringify(toastText)} ` +
      `selection=${JSON.stringify(selectionDiagnostic)}\n${error.message}`,
    );
  }
  await composer.fill(message);
  const responsePromise = page.waitForResponse(response => {
    const url = new URL(response.url());
    return response.request().method() === "POST" && /\/comments$/.test(url.pathname);
  });
  await page.getByRole("button", { name: "Comment", exact: true }).click();
  const response = await responsePromise;
  assert.equal(response.status(), 200, await response.text());
  try {
    await page.waitForFunction(expected => {
      const highlight = globalThis.CSS?.highlights?.get("lockedin-review");
      return !!highlight && Array.from(highlight).some(range => range.toString() === expected);
    }, exactSelection, { timeout: 5_000 });
  } catch (error) {
    const ranges = await highlightedText(page);
    const visibleSource = await editorText(page);
    throw new Error(
      `${error.message}\nExpected highlight: ${JSON.stringify(exactSelection)}` +
      `\nActual highlights: ${JSON.stringify(ranges)}` +
      `\nVisible source contains selection: ${visibleSource.includes(exactSelection)}`,
    );
  }
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
      "Flow-matching remains stable with $x_{t+1}=f(x_t)$ and keeps the review exact.";
    const deleteSelection = "This disposable sentence will receive a deletable review.";
    const unanchorSelection = "This selected passage will be removed and become unanchored.";
    const filler = Array.from(
      { length: 90 },
      (_, index) => `Background paragraph ${index}: ${"context ".repeat(14)}`,
    ).join("\n\n");
    const initialContent =
      `# Review lifecycle\n\n${filler}\n\nEmoji offset guard 😀 precedes this review.\n\n${exactSelection}\n\n` +
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
    const modePicker = page.locator('#ptabs select[title="Page view mode"]');
    if (!(await page.locator(EDITOR).isVisible())) {
      await modePicker.selectOption("edit");
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

    const requestStart = requests.length;
    const navigationsBefore = mainNavigations;
    await addComment(page, exactSelection, "Keep this explanation concise.");
    await page.waitForTimeout(350);
    const lifecycleRequests = requests.slice(requestStart);
    assert.equal(
      lifecycleRequests.filter(item => item.method === "POST" && /\/comments$/.test(item.path)).length,
      1,
      `comment creation should use one mutation request: ${JSON.stringify(lifecycleRequests)}`,
    );
    assert.equal(
      lifecycleRequests.filter(item =>
        item.method === "PUT" && item.path === `/api/bubbles/${slug}/pages/${pageSlug}`
      ).length,
      0,
      `comment creation must not trigger a second page save: ${JSON.stringify(lifecycleRequests)}`,
    );
    assert.equal(
      lifecycleRequests.filter(item =>
        item.method === "GET" &&
        (item.path === `/api/bubbles/${slug}/pages/${pageSlug}` || /\/comments$/.test(item.path))
      ).length,
      0,
      `comment creation must apply its response without a follow-up read: ${JSON.stringify(lifecycleRequests)}`,
    );
    assert.equal(mainNavigations, navigationsBefore, "comment creation reloaded the page");
    assert.deepEqual(await highlightedText(page), [exactSelection]);
    step("exact LaTeX selection became the only highlighted body in one request");

    await clickExactHighlight(page, exactSelection);
    const selectedThread = page.locator("#reviewWrap .review-thread.selected[open]");
    assert.match(await selectedThread.textContent(), /Keep this explanation concise\./);
    step("clicking the highlighted body opened its matching conversation");

    const firstSource = (
      await api(context.request, baseUrl, "GET", `/api/bubbles/${slug}/pages/${pageSlug}`)
    ).content;
    const firstMatch = firstSource.match(/\\comment\{([^}]+)\}\{Flow-matching/);
    assert.ok(firstMatch, "server response did not install the canonical comment wrapper");
    const firstId = firstMatch[1];

    const overlapStart = requests.length;
    await selectSource(page, "remains stable");
    await page.getByRole("button", { name: "Add comment to selected text" }).click();
    await page.waitForFunction(() => {
      const toast = document.querySelector("#toast");
      return toast && !toast.classList.contains("hide") && /overlap|nested|intersect/i.test(toast.textContent);
    });
    assert.equal(
      requests.slice(overlapStart).filter(item => item.method === "POST" && /\/comments$/.test(item.path)).length,
      0,
      "an intersecting comment reached the server",
    );
    step("intersecting comment selection was rejected before mutation");

    await clickExactHighlight(page, exactSelection);
    const scrollBeforeResolve = await sourceScrollTop(page);
    assert.ok(scrollBeforeResolve > 0, "resolve scroll-preservation check did not reach the long page body");

    // Hold an already-fetched comments response open across Resolve. The authoritative Resolve
    // response must win even when an older GET arrives afterward.
    const commentsPath = `/api/bubbles/${slug}/pages/${pageSlug}/comments`;
    let releaseStaleComments;
    const staleCommentsGate = new Promise(resolve => { releaseStaleComments = resolve; });
    let staleCommentsStarted;
    const staleCommentsStart = new Promise(resolve => { staleCommentsStarted = resolve; });
    let staleCommentsFinished;
    const staleCommentsDone = new Promise(resolve => { staleCommentsFinished = resolve; });
    await page.route(`**${commentsPath}`, async route => {
      if (route.request().method() !== "GET") return route.continue();
      const response = await route.fetch();
      staleCommentsStarted();
      await staleCommentsGate;
      await route.fulfill({ response });
      staleCommentsFinished();
    });
    await api(context.request, baseUrl, "POST", `${commentsPath}/${firstId}/replies`, {
      body: "Delayed stale-response regression fixture.",
    });
    await Promise.race([
      staleCommentsStart,
      delay(8_000).then(() => { throw new Error("poll did not start the delayed comments GET"); }),
    ]);
    const resolveResponse = page.waitForResponse(response =>
      response.request().method() === "PATCH" &&
      new URL(response.url()).pathname.endsWith(`/comments/${firstId}`),
    );
    await page
      .locator(`#reviewWrap .review-thread[data-review-id="${firstId}"]`)
      .getByRole("button", { name: "Resolve", exact: true })
      .click();
    assert.equal((await resolveResponse).status(), 200);
    await page.waitForFunction(id => {
      const source = document.querySelector(".toastui-editor-md-container .ProseMirror")?.innerText || "";
      return !source.includes(`\\comment{${id}}{`);
    }, firstId);
    const scrollAfterResolve = await sourceScrollTop(page);
    assert.ok(
      Math.abs(scrollAfterResolve - scrollBeforeResolve) <= 3,
      `resolve moved editor scroll from ${scrollBeforeResolve} to ${scrollAfterResolve}`,
    );
    assert.ok((await editorText(page)).includes(exactSelection), "resolve removed the selected body");
    releaseStaleComments();
    await staleCommentsDone;
    await page.waitForTimeout(100);
    assert.equal(
      await page.locator(`#reviewWrap .review-thread[data-review-id="${firstId}"]`).count(),
      1,
      "a stale comments GET overwrote the authoritative Resolve response",
    );
    await page.unroute(`**${commentsPath}`);
    step("resolve removed only the wrapper and preserved editor scroll/body");

    await addComment(page, deleteSelection, "Delete this disposable review.");
    await clickExactHighlight(page, deleteSelection);
    const secondSource = (
      await api(context.request, baseUrl, "GET", `/api/bubbles/${slug}/pages/${pageSlug}`)
    ).content;
    const secondMatch = secondSource.match(/\\comment\{([^}]+)\}\{This disposable sentence/);
    assert.ok(secondMatch, "second canonical wrapper is missing");
    const secondId = secondMatch[1];
    const scrollBeforeDelete = await sourceScrollTop(page);
    assert.ok(scrollBeforeDelete > 0, "delete scroll-preservation check did not reach the long page body");
    page.once("dialog", dialog => dialog.accept());
    const deleteResponse = page.waitForResponse(response =>
      response.request().method() === "DELETE" &&
      new URL(response.url()).pathname.endsWith(`/comments/${secondId}`),
    );
    await page
      .locator(`#reviewWrap .review-thread[data-review-id="${secondId}"]`)
      .getByRole("button", { name: "Delete", exact: true })
      .click();
    assert.equal((await deleteResponse).status(), 200);
    await page.waitForFunction(id => {
      const source = document.querySelector(".toastui-editor-md-container .ProseMirror")?.innerText || "";
      return !source.includes(`\\comment{${id}}{`);
    }, secondId);
    const scrollAfterDelete = await sourceScrollTop(page);
    assert.ok(
      Math.abs(scrollAfterDelete - scrollBeforeDelete) <= 3,
      `delete moved editor scroll from ${scrollBeforeDelete} to ${scrollAfterDelete}`,
    );
    assert.ok((await editorText(page)).includes(deleteSelection), "delete removed the selected body");
    const comments = await api(
      context.request,
      baseUrl,
      "GET",
      `/api/bubbles/${slug}/pages/${pageSlug}/comments`,
    );
    assert.ok(!comments.threads.some(thread => thread.id === secondId), "deleted review remains in sidecar");
    step("delete removed wrapper and conversation without moving the editor");

    await addComment(page, unanchorSelection, "Keep this thread after its passage is removed.");
    const attachedSource = (
      await api(context.request, baseUrl, "GET", `/api/bubbles/${slug}/pages/${pageSlug}`)
    ).content;
    const unanchorMatch = attachedSource.match(
      /\\comment\{([^}]+)\}\{This selected passage will be removed and become unanchored\.\}/,
    );
    assert.ok(unanchorMatch, "unanchoring fixture wrapper is missing");
    const unanchorId = unanchorMatch[1];
    const completeWrapper = `\\comment{${unanchorId}}{${unanchorSelection}}`;
    await selectSource(page, completeWrapper);
    const unanchorSave = page.waitForResponse(response =>
      response.request().method() === "PUT" &&
      new URL(response.url()).pathname === `/api/bubbles/${slug}/pages/${pageSlug}`,
      { timeout: 7_000 },
    );
    await page.keyboard.press("Backspace");
    assert.equal((await unanchorSave).status(), 200);
    const unanchoredThread = page.locator(
      `#reviewWrap .review-thread[data-review-id="${unanchorId}"]`,
    );
    await unanchoredThread.getByText("Unanchored", { exact: true }).waitFor();
    assert.ok(
      !(await highlightedText(page)).some(text => text.includes(unanchorSelection)),
      "deleted passage kept a review highlight",
    );
    const afterUnanchorPage = (
      await api(context.request, baseUrl, "GET", `/api/bubbles/${slug}/pages/${pageSlug}`)
    ).content;
    const afterUnanchorComments = await api(
      context.request,
      baseUrl,
      "GET",
      `/api/bubbles/${slug}/pages/${pageSlug}/comments`,
    );
    assert.ok(!afterUnanchorPage.includes(completeWrapper), "GET recreated a removed wrapper");
    assert.ok(!afterUnanchorPage.includes(unanchorSelection), "deleted selected body returned on GET");
    assert.equal(
      afterUnanchorComments.threads.find(thread => thread.id === unanchorId)?.anchor_state,
      "unanchored",
    );
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector(EDITOR, { state: "visible", timeout: 30_000 });
    await page
      .locator(`#reviewWrap .review-thread[data-review-id="${unanchorId}"]`)
      .getByText("Unanchored", { exact: true })
      .waitFor();
    assert.ok(!(await editorText(page)).includes(completeWrapper), "reload resurrected a wrapper");
    assert.ok(
      !(await highlightedText(page)).some(text => text.includes(unanchorSelection)),
      "reloaded unanchored review regained a highlight",
    );
    step("deleted wrapper/body stayed unanchored through GET and full reload");

    const serverCopyBeforeMalformed = (
      await api(context.request, baseUrl, "GET", `/api/bubbles/${slug}/pages/${pageSlug}`)
    ).content;
    await placeSourceCaretAtEnd(page);
    await page.keyboard.insertText("\n\\comment{broken-variable-id}{never closed");
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
