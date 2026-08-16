#!/usr/bin/env node
/**
 * Real-browser regression for the two-tab workspace bug.
 *
 * The workspace selection used to live only in localStorage, which every tab of a browser shares.
 * So a tab open on workspace A, refreshed after another tab switched to workspace B, reloaded
 * itself against B — and its bubble came back 404. The fix puts the workspace in the URL
 * (`#w/<workspace>/bubble/<slug>`), the only per-tab memory a refresh survives.
 *
 * Drives system Chrome against a disposable data root with two workspaces, two bubbles, and two
 * real tabs in one browser context (so they share localStorage exactly as a user's tabs do).
 *
 * Screenshots land in LOCKEDIN_E2E_SHOTS when set.
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

function step(message) { process.stdout.write(`workspace-route-e2e: ${message}\n`); }

async function freePort() {
  const socket = net.createServer();
  await new Promise(r => socket.listen(0, "127.0.0.1", r));
  const { port } = socket.address();
  await new Promise(r => socket.close(r));
  return port;
}

async function waitForServer(baseUrl, child, output) {
  const deadline = Date.now() + SERVER_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`server exited early\n${output()}`);
    try { const r = await fetch(`${baseUrl}/api/health`); if (r.status < 500) return; } catch (_) {}
    await delay(100);
  }
  throw new Error(`timed out waiting for server\n${output()}`);
}

async function shoot(page, name) {
  if (!SHOTS) return;
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`) });
}

/** What the app currently believes it is showing. */
const state = page => page.evaluate(() => ({
  hash: location.hash,
  title: document.querySelector("#bubbleTitle,.bubble-title,h1")?.textContent?.trim() || "",
  toast: document.querySelector("#toast")?.textContent?.trim() || "",
}));

async function main() {
  assert.ok(fs.existsSync(CHROME), `Chrome is not installed at ${CHROME}`);
  const dataRoot = fs.mkdtempSync(path.join(os.tmpdir(), "lockedin-ws-route-e2e-"));
  const port = await freePort();
  const baseUrl = `http://127.0.0.1:${port}`;
  let out = "", child, browser;

  try {
    child = spawn("uv", ["run", "lockedin", "serve", "--host", "127.0.0.1", "--port", String(port)], {
      cwd: REPO,
      env: { ...process.env, LOCKEDIN_HOME: dataRoot, LOCKEDIN_INSECURE_COOKIE: "1", PYTHONUNBUFFERED: "1" },
      stdio: ["ignore", "pipe", "pipe"],
    });
    child.stdout.on("data", c => { out += c; });
    child.stderr.on("data", c => { out += c; });
    await waitForServer(baseUrl, child, () => out);
    step("disposable server is ready");

    browser = await chromium.launch({ executablePath: CHROME, headless: true,
      args: ["--no-sandbox", "--disable-dev-shm-usage"] });
    // ONE context: both tabs share cookies and localStorage, which is the whole point.
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const user = `ws-route-e2e-${Date.now()}`;
    const post = (p, data, headers) =>
      context.request.fetch(`${baseUrl}${p}`, { method: "POST", data, headers });
    await post("/api/signup", { username: user, password: "temporary-password" });

    // Workspace A is Personal; workspace B is a second, shared one.
    const me = await (await context.request.fetch(`${baseUrl}/api/me`)).json();
    const wsA = me.personal_workspace_id;
    const wsB = (await (await post("/api/workspaces", { name: "Second" })).json()).workspace.id;
    assert.ok(wsA && wsB && wsA !== wsB, `two distinct workspaces are required: ${wsA} / ${wsB}`);

    const makeBubble = async (workspace, name) => {
      const headers = { "X-LockedIn-Workspace": workspace };
      const { slug } = await (await post("/api/bubbles", { name }, headers)).json();
      await post(`/api/bubbles/${slug}/approve`, { instructions: "" }, headers);
      return slug;
    };
    const slugA = await makeBubble(wsA, "Alpha Bubble");
    const slugB = await makeBubble(wsB, "Beta Bubble");
    step(`workspace A holds ${slugA}, workspace B holds ${slugB}`);

    // --- tab 1 opens the workspace-A bubble ---
    const tab1 = await context.newPage();
    tab1.on("pageerror", e => { throw e; });
    await tab1.goto(`${baseUrl}/#w/${wsA}/bubble/${slugA}`, { waitUntil: "domcontentloaded" });
    await tab1.locator("#previewWrap").waitFor({ state: "visible", timeout: 15_000 });
    let s1 = await state(tab1);
    assert.match(s1.hash, new RegExp(`^#w/${wsA}/bubble/${slugA}`), `tab 1 route: ${s1.hash}`);
    step("tab 1 opened the workspace-A bubble");

    // --- tab 2 opens the workspace-B bubble, which is also a workspace switch for the browser ---
    const tab2 = await context.newPage();
    tab2.on("pageerror", e => { throw e; });
    await tab2.goto(`${baseUrl}/#w/${wsB}/bubble/${slugB}`, { waitUntil: "domcontentloaded" });
    await tab2.locator("#previewWrap").waitFor({ state: "visible", timeout: 15_000 });
    const s2 = await state(tab2);
    assert.match(s2.hash, new RegExp(`^#w/${wsB}/bubble/${slugB}`), `tab 2 route: ${s2.hash}`);
    // localStorage now holds B — exactly the state that used to poison tab 1.
    const remembered = await tab2.evaluate(() => localStorage.getItem("li_workspace"));
    assert.equal(remembered, wsB, "the shared remembered workspace should be B");
    step("tab 2 opened the workspace-B bubble and the shared memory now says B");

    // --- the bug: refresh tab 1 ---
    await tab1.reload({ waitUntil: "domcontentloaded" });
    await tab1.locator("#previewWrap").waitFor({ state: "visible", timeout: 15_000 });
    await tab1.waitForTimeout(500);
    s1 = await state(tab1);
    await shoot(tab1, "tab1-after-reload");
    assert.match(s1.hash, new RegExp(`^#w/${wsA}/bubble/${slugA}`),
      `tab 1 must reload into its own workspace, got ${s1.hash}`);
    assert.ok(!/not found|no such|error/i.test(s1.toast),
      `tab 1 reloaded with an error toast: ${s1.toast}`);
    const active1 = await tab1.evaluate(() => window.__wsProbe);
    step("tab 1 survived the refresh in workspace A" + (active1 ? ` (${active1})` : ""));

    // Tab 2 must be equally unbothered by tab 1 having re-asserted A.
    await tab2.reload({ waitUntil: "domcontentloaded" });
    await tab2.locator("#previewWrap").waitFor({ state: "visible", timeout: 15_000 });
    await tab2.waitForTimeout(500);
    const s2b = await state(tab2);
    assert.match(s2b.hash, new RegExp(`^#w/${wsB}/bubble/${slugB}`),
      `tab 2 must reload into its own workspace, got ${s2b.hash}`);
    assert.ok(!/not found|no such|error/i.test(s2b.toast),
      `tab 2 reloaded with an error toast: ${s2b.toast}`);
    step("tab 2 survived the refresh in workspace B");

    // --- a route with no workspace still works, and gets one written into the URL ---
    const plain = await context.newPage();
    plain.on("pageerror", e => { throw e; });
    await plain.goto(`${baseUrl}/#bubbles`, { waitUntil: "domcontentloaded" });
    await plain.waitForTimeout(800);
    const sp = await state(plain);
    assert.match(sp.hash, /^#w\/[^/]+\/bubbles$/, `a bare route should gain its workspace: ${sp.hash}`);
    step("a legacy route without a workspace still resolves and is upgraded in place");

    // --- a stale workspace in the URL falls back instead of bricking the tab ---
    const stale = await context.newPage();
    stale.on("pageerror", e => { throw e; });
    await stale.goto(`${baseUrl}/#w/deadbeefdeadbeef/bubbles`, { waitUntil: "domcontentloaded" });
    await stale.waitForTimeout(1200);
    const ss = await state(stale);
    assert.ok(!/^#w\/deadbeefdeadbeef/.test(ss.hash), `a stale workspace should be dropped: ${ss.hash}`);
    assert.ok(await stale.locator("#app").isVisible(), "the app must still load for a stale link");
    step("a stale workspace link falls back to Personal rather than bricking the tab");

    step("all workspace-routing checks passed");
  } finally {
    if (browser) await browser.close().catch(() => {});
    if (child && child.exitCode === null) {
      child.kill("SIGTERM");
      await Promise.race([new Promise(r => child.once("exit", r)), delay(3000)]);
    }
    fs.rmSync(dataRoot, { recursive: true, force: true });
  }
}

main().catch(error => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
