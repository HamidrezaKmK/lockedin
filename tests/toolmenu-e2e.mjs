#!/usr/bin/env node
/**
 * Real-browser regression for the bubble title row's ⋮ tools menu.
 *
 * Assets, Overleaf and preview/sharing used to be three separate button groups crowding the
 * title row; they now live behind one ⋮ button. This drives that menu against a disposable data
 * root: the row carries no loose tool buttons, the dropdown groups the three areas, toggling the
 * public link rewrites the menu in place (and lights the trigger's dot), and clicking outside
 * closes it. Nothing under the repository's real data/ directory is read or changed.
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

    const page = await context.newPage();
    page.on("pageerror", error => { throw error; });
    await page.goto(`${baseUrl}/#bubble/${slug}`, { waitUntil: "domcontentloaded" });

    const trigger = page.locator(".bubble-title .toolmenu-btn");
    await trigger.waitFor({ state: "visible", timeout: 10_000 });
    // The whole point of the collapse: the title row holds the chip and the ⋮, nothing else.
    const rowText = await page.locator(".bubble-title").innerText();
    for (const gone of ["Assets", "Overleaf", "Preview", "Sharing", "Open link"]) {
      assert.ok(!rowText.includes(gone), `"${gone}" must not sit loose in the title row:\n${rowText}`);
    }
    assert.equal(await page.locator(".toolmenu-panel").count(), 0, "the menu starts closed");
    await shoot(page, "toolmenu-collapsed");
    step("the title row carries one ⋮ button and no loose tool buttons");

    await trigger.click();
    const panel = page.locator(".toolmenu-panel");
    await panel.waitFor({ state: "visible", timeout: 2_000 });
    // The group headings are uppercased by CSS, so compare case-insensitively throughout.
    const closed = (await panel.innerText()).toLowerCase();
    for (const expected of ["overleaf", "link a project", "sharing", "preview page",
                            "public link", "off", "files", "bubble assets"]) {
      assert.ok(closed.includes(expected), `the menu is missing ${expected}:\n${closed}`);
    }
    assert.ok(!closed.includes("open shared page"), `an unshared bubble has no public link:\n${closed}`);
    await shoot(page, "toolmenu-open");
    step("the dropdown groups Overleaf, sharing and files");

    // Toggling sharing rewrites the open menu in place and lights the trigger's dot.
    await page.locator(".toolmenu-item", { hasText: "Public link" }).click();
    await page.locator(".toolmenu-item", { hasText: "Open shared page" })
      .waitFor({ state: "visible", timeout: 5_000 });
    const shared = (await panel.innerText()).toLowerCase();
    assert.ok(/public link\s*\non\b/.test(shared), `the public link must read as on:\n${shared}`);
    assert.ok(!shared.includes("preview page"),
      `the owner preview is redundant once the public link is live:\n${shared}`);
    assert.ok(await page.locator(".toolmenu").evaluate(node => node.classList.contains("sharing")),
      "a live public link must show on the collapsed trigger");
    await shoot(page, "toolmenu-sharing");
    step("toggling the public link rewrites the menu and marks the trigger");

    await page.locator("h2.section-title").click();
    await panel.waitFor({ state: "detached", timeout: 2_000 });
    assert.ok(!(await trigger.evaluate(node => node.classList.contains("on"))),
      "closing the menu must un-press the trigger");
    step("clicking outside closes the dropdown");

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
