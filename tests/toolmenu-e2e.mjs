#!/usr/bin/env node
/**
 * Real-browser regression for the bubble workspace's ⋮ tools menu.
 *
 * Assets, Overleaf, preview/sharing, Papers, the view modes and Edit titles were once six
 * separate controls spread over two rows — several of them hidden on phones, which reached
 * Papers and Overleaf through floating pill buttons instead. They now live behind one ⋮ button
 * in the page toolbar, on every screen size. This drives that against a disposable data root:
 * the toolbar carries only `+`, ⋮ and ⛶, the dropdown groups every area, switching view mode
 * from it works, Papers opens as a popup, toggling the public link rewrites the menu in place,
 * clicking outside closes it, and the whole thing still works at phone width.
 * Nothing under the repository's real data/ directory is read or changed.
 *
 * Screenshots land in LOCKEDIN_E2E_SHOTS when set, for eyeballing the visual result.
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
const SHOTS = process.env.LOCKEDIN_E2E_SHOTS || "";
const SERVER_TIMEOUT_MS = 30_000;

function step(message) {
  process.stdout.write(`toolmenu-e2e: ${message}\n`);
}

async function freePort() {
  const socket = net.createServer();
  await new Promise((resolve, reject) => {
    socket.once("error", reject);
    socket.listen(0, "127.0.0.1", resolve);
  });
  const { port } = socket.address();
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
      const response = await fetch(`${baseUrl}/api/health`);
      if (response.status < 500) return;
    } catch (_) { /* still starting */ }
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
  const response = await request.fetch(`${baseUrl}${pathname}`, { method, data, failOnStatusCode: false });
  const raw = await response.text();
  let body = {};
  try { body = raw ? JSON.parse(raw) : {}; } catch (_) { body = { raw }; }
  assert.ok(response.ok(), `${method} ${pathname} failed (${response.status()}): ${raw}`);
  return body;
}

async function shoot(page, name) {
  if (!SHOTS) return;
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`) });
}

async function main() {
  assert.ok(fs.existsSync(CHROME), `Chrome is not installed at ${CHROME}`);
  const dataRoot = fs.mkdtempSync(path.join(os.tmpdir(), "lockedin-toolmenu-e2e-"));
  const port = await freePort();
  const baseUrl = `http://127.0.0.1:${port}`;
  let serverOutput = "";
  let child, browser;

  try {
    child = spawn("uv", ["run", "lockedin", "serve", "--host", "127.0.0.1", "--port", String(port)], {
      cwd: REPO,
      env: { ...process.env, LOCKEDIN_HOME: dataRoot, LOCKEDIN_INSECURE_COOKIE: "1", PYTHONUNBUFFERED: "1" },
      stdio: ["ignore", "pipe", "pipe"],
    });
    child.stdout.on("data", chunk => { serverOutput += chunk; });
    child.stderr.on("data", chunk => { serverOutput += chunk; });
    await waitForServer(baseUrl, child, () => serverOutput);
    step("disposable server is ready");

    browser = await chromium.launch({
      executablePath: CHROME, headless: true,
      args: ["--no-sandbox", "--disable-dev-shm-usage"],
    });
    const context = await browser.newContext({ viewport: { width: 1500, height: 950 } });
    const username = `toolmenu-e2e-${Date.now()}`;
    await api(context.request, baseUrl, "POST", "/api/signup", { username, password: "temporary-toolmenu-password" });
    const { slug } = await api(context.request, baseUrl, "POST", "/api/bubbles", { name: "Tool Menu E2E" });
    await api(context.request, baseUrl, "POST", `/api/bubbles/${slug}/approve`, { instructions: "" });

    const detail = await api(context.request, baseUrl, "GET", `/api/bubbles/${slug}`);
    const homePage = detail.bubble.home;

    const page = await context.newPage();
    page.on("pageerror", error => { throw error; });
    await page.goto(`${baseUrl}/#bubble/${slug}/${homePage}`, { waitUntil: "domcontentloaded" });

    const trigger = page.locator(".hdr-cluster .toolmenu-btn");
    await trigger.waitFor({ state: "visible", timeout: 10_000 });
    // The whole point of the collapse: nothing a bubble can do sits loose in the toolbar.
    const rowText = await page.locator("#ptabs").innerText();
    for (const gone of ["Assets", "Overleaf", "Papers", "Preview", "Sharing"]) {
      assert.ok(!rowText.includes(gone), `"${gone}" must not sit loose in the toolbar:\n${rowText}`);
    }
    assert.equal(await page.locator(".ptab-new").innerText(), "+", "page creation is a bare +");
    assert.equal(await page.locator("#ptabs > *").count(), 3,
      "the toolbar row is exactly: + , the tab list, the right-hand controls");
    assert.equal(await page.locator(".ptab-controls > *").count(), 1,
      "the right-hand controls are one card, not several");
    assert.equal(await page.locator(".tabrow-group > *").count(), 3,
      "that card is the editor switch, focus, and the marks switch");
    assert.ok(await page.locator("#ptabs > *").first().evaluate(n => n.classList.contains("ptab-new")),
      "page creation must be the leftmost thing in the row");
    assert.equal(await page.locator(".toolmenu-panel").count(), 0, "the menu starts closed");
    await shoot(page, "toolmenu-collapsed");
    step("the toolbar carries +, the tabs, and the two pane switches");

    await trigger.click();
    const panel = page.locator(".toolmenu-panel");
    await panel.waitFor({ state: "visible", timeout: 2_000 });
    // The group headings are uppercased by CSS, so compare case-insensitively throughout.
    const closed = (await panel.innerText()).toLowerCase();
    for (const expected of ["edit titles",
                            "this bubble", "bubble home", "papers", "assets",
                            "overleaf", "link a project", "sharing", "preview page",
                            "public link", "off"]) {
      assert.ok(closed.includes(expected), `the menu is missing ${expected}:\n${closed}`);
    }
    assert.ok(!closed.includes("open shared page"), `an unshared bubble has no public link:\n${closed}`);
    await shoot(page, "toolmenu-open");
    step("the dropdown groups this bubble, Overleaf and sharing");

    // The two switches the named modes were combinations of live in the tab row. Close the
    // dropdown first — while it is open its panel covers the row it sits above.
    await page.mouse.click(220, 700);
    await panel.waitFor({ state: "detached", timeout: 2_000 });
    const host = page.locator("#editorHost");
    await page.locator("#paneLeftToggle").click();
    assert.ok(await host.evaluate(n => n.classList.contains("mode-split")),
      "opening the editor pane must put it beside the rendered page");
    await page.locator("#paneLeftToggle").click();
    assert.ok(await host.evaluate(n => n.classList.contains("mode-view")),
      "closing it must leave the rendered page alone");
    step("the editor pane opens and closes from the tab row");

    await trigger.click();
    await panel.waitFor({ state: "visible", timeout: 2_000 });

    // Papers is a popup on the assets modal's frame, not an anchored dropdown.
    await page.locator(".toolmenu-item", { hasText: "Papers" }).click();
    // A hidden #helpModal also carries .overlay, so address this dialog by its label.
    const papersOverlay = page.getByRole("dialog", { name: "Papers in this bubble" });
    const papers = papersOverlay.locator(".papers-modal-body");
    await papers.waitFor({ state: "visible", timeout: 5_000 });
    assert.match(await papersOverlay.locator(".asset-modal-header").innerText(), /Papers/,
      "the popup must announce itself as Papers");
    await shoot(page, "toolmenu-papers");
    await papersOverlay.click({ position: { x: 5, y: 5 } });
    await papers.waitFor({ state: "detached", timeout: 2_000 });
    step("Papers opens as a popup and closes on the backdrop");

    await trigger.click();
    await panel.waitFor({ state: "visible", timeout: 2_000 });

    // Toggling sharing rewrites the open menu in place.
    await page.locator(".toolmenu-item", { hasText: "Public link" }).click();
    await page.locator(".toolmenu-item", { hasText: "Open shared page" })
      .waitFor({ state: "visible", timeout: 5_000 });
    const shared = (await panel.innerText()).toLowerCase();
    assert.ok(/public link\s*\non\b/.test(shared), `the public link must read as on:\n${shared}`);
    assert.ok(!shared.includes("preview page"),
      `the owner preview is redundant once the public link is live:\n${shared}`);
    await shoot(page, "toolmenu-sharing");
    step("toggling the public link rewrites the menu in place");

    await page.mouse.click(220, 700);
    await panel.waitFor({ state: "detached", timeout: 2_000 });
    assert.ok(!(await trigger.evaluate(node => node.classList.contains("on"))),
      "closing the menu must un-press the trigger");
    step("clicking outside closes the dropdown");

    // A phone gets the same one control surface, and none of the retired floating buttons.
    await page.setViewportSize({ width: 390, height: 800 });
    await page.reload({ waitUntil: "domcontentloaded" });
    const phoneTrigger = page.locator(".hdr-cluster .toolmenu-btn");
    await phoneTrigger.waitFor({ state: "visible", timeout: 10_000 });
    assert.ok(await page.locator(".ptab-new").isVisible(), "a phone must be able to add a page");
    assert.equal(await page.locator(".mobile-bubble-actions").count(), 0,
      "the floating mobile pill buttons must be gone");
    await phoneTrigger.click();
    await panel.waitFor({ state: "visible", timeout: 2_000 });
    const phoneMenu = (await panel.innerText()).toLowerCase();
    for (const expected of ["papers", "overleaf", "public link"]) {
      assert.ok(phoneMenu.includes(expected), `the phone menu is missing ${expected}:\n${phoneMenu}`);
    }
    const clipped = await panel.evaluate(node => {
      const box = node.getBoundingClientRect();
      return box.right > window.innerWidth + 1 || box.left < -1 || box.width < 100;
    });
    assert.ok(!clipped, "the panel must fit within a phone viewport");
    await shoot(page, "toolmenu-phone");
    step("the same menu works at phone width, with no floating buttons left");

    process.stdout.write("toolmenu-e2e: all bubble tool-menu checks passed\n");
  } finally {
    if (browser) await browser.close().catch(() => {});
    await stopProcess(child);
    fs.rmSync(dataRoot, { recursive: true, force: true });
  }
}

main().catch(error => {
  process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
  process.exitCode = 1;
});
