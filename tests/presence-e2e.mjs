#!/usr/bin/env node
/**
 * Real-browser presence regression.
 *
 * Starts LockedIn against a disposable data root, signs up a disposable owner, registers a few
 * Scientist workers through the ordinary v2 sync endpoints (including one deliberately out of
 * date), and checks that the bubble editor's presence chip and dropdown report them. Nothing
 * under the repository's real data/ directory is read or changed.
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
const CLIENT_VERSION = fs
  .readFileSync(path.join(REPO, "src/lockedin/scientist_cli.py"), "utf8")
  .match(/SCIENTIST_CLIENT_VERSION = "([^"]+)"/)[1];

function step(message) {
  process.stdout.write(`presence-e2e: ${message}\n`);
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

/** Authorize a Scientist client exactly the way the installed CLI does. */
async function scientistToken(request, baseUrl) {
  const version = { "X-LockedIn-Scientist-Version": CLIENT_VERSION };
  const start = await request.fetch(`${baseUrl}/api/scientist/v2/device`, {
    method: "POST", headers: version, data: { client_name: "lockedin-scientist" },
  });
  const { device_code: code } = await start.json();
  const approved = await request.fetch(`${baseUrl}/api/scientist/v2/device/${code}/approve`, { method: "POST" });
  assert.ok(approved.ok(), `device approval failed (${approved.status()})`);
  const issued = await request.fetch(`${baseUrl}/api/scientist/v2/device/${code}/token`, { headers: version });
  const { token } = await issued.json();
  assert.ok(token, "no Scientist token was issued");
  return token;
}

/** One ordinary sync poll, carrying the presence headers a real worker sends. */
async function workerPoll(request, baseUrl, token, slug, worker, { version = CLIENT_VERSION } = {}) {
  const headers = {
    Authorization: `Bearer ${token}`,
    "X-LockedIn-Scientist-Version": version,
    "X-LockedIn-Worker": worker.id,
    "X-LockedIn-Worker-Label": worker.label,
  };
  if (worker.status) headers["X-LockedIn-Worker-Status"] = worker.status;
  if (worker.error) headers["X-LockedIn-Worker-Error"] = worker.error;
  const response = await request.fetch(
    `${baseUrl}/api/scientist/v2/bubbles/${slug}/manifest`, { headers, failOnStatusCode: false });
  return response.status();
}

async function shoot(page, name) {
  if (!SHOTS) return;
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`) });
}

async function main() {
  assert.ok(fs.existsSync(CHROME), `Chrome is not installed at ${CHROME}`);
  const dataRoot = fs.mkdtempSync(path.join(os.tmpdir(), "lockedin-presence-e2e-"));
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
    const username = `presence-e2e-${Date.now()}`;
    await api(context.request, baseUrl, "POST", "/api/signup", { username, password: "temporary-presence-password" });
    const { slug } = await api(context.request, baseUrl, "POST", "/api/bubbles", { name: "Presence E2E" });
    await api(context.request, baseUrl, "POST", `/api/bubbles/${slug}/approve`, { instructions: "" });

    const token = await scientistToken(context.request, baseUrl);
    step("authorized a Scientist client");

    // Three directories on one bubble: healthy, failing, and one that the server refuses.
    assert.equal(await workerPoll(context.request, baseUrl, token, slug,
      { id: "uid-healthy", label: "thesis-repo", status: "running" }), 200);
    assert.equal(await workerPoll(context.request, baseUrl, token, slug,
      { id: "uid-failing", label: "side-notes", status: "degraded",
        error: "server returned 409: conflict on reports/pages/overview.md" }), 200);
    assert.equal(await workerPoll(context.request, baseUrl, token, slug,
      { id: "uid-stale", label: "old-clone" }, { version: "2020.01.01.1" }), 426);
    step("registered three workers, one of them rejected as out of date");

    const page = await context.newPage();
    page.on("pageerror", error => { throw error; });
    const left = [];
    page.on("request", request => {
      if (request.method() === "DELETE") left.push(request.url());
    });
    await page.goto(`${baseUrl}/#bubble/${slug}`, { waitUntil: "domcontentloaded" });

    const chip = page.locator(".presence .presence-chip");
    await chip.waitFor({ state: "visible", timeout: 10_000 });
    assert.match(await chip.innerText(), /👥 1/, "the chip must count the viewer");
    assert.match(await chip.innerText(), /⚙ 3/, "the chip must count every worker directory");
    assert.ok(await chip.evaluate(node => node.classList.contains("trouble")),
      "an unhealthy worker must be visible on the collapsed chip");
    await shoot(page, "presence-chip");
    step("the collapsed chip counts people and workers and flags trouble");

    await chip.click();
    const menu = page.locator(".presence-menu");
    await menu.waitFor({ state: "visible", timeout: 2_000 });
    const menuText = await menu.innerText();
    for (const expected of [username, "thesis-repo", "side-notes", "old-clone",
                            "Two directories are syncing this bubble"]) {
      assert.ok(menuText.includes(expected), `dropdown is missing ${expected}:\n${menuText}`);
    }
    await shoot(page, "presence-menu");
    step("the dropdown lists people, every worker directory, and the duplicate warning");

    // A failing worker's reason is one click away, not buried in a log.
    await page.locator(".presence-item", { hasText: "side-notes" }).click();
    const detail = page.locator(".presence-detail");
    await detail.waitFor({ state: "visible", timeout: 2_000 });
    const detailText = await detail.innerText();
    assert.ok(detailText.includes("409"), `worker detail is missing its error:\n${detailText}`);
    assert.ok(detailText.includes(CLIENT_VERSION), `worker detail is missing the client version:\n${detailText}`);
    await shoot(page, "presence-detail");
    step("selecting a failing worker reveals the reason and its client version");

    const stale = page.locator(".presence-item", { hasText: "old-clone" });
    assert.ok(await stale.evaluate(node => node.classList.contains("dead")),
      "a rejected client must read as dead rather than disappear");
    assert.match(await stale.innerText(), /dead/);

    // Clicking away closes the dropdown; the chip alone remains.
    await page.mouse.click(700, 500);
    await menu.waitFor({ state: "hidden", timeout: 2_000 });
    step("clicking outside closes the dropdown");

    // Leaving the bubble releases the viewer immediately rather than waiting out the timeout.
    await page.evaluate(() => { location.hash = "#bubbles"; });
    await page.waitForTimeout(600);
    assert.ok(left.some(url => url.endsWith(`/api/bubbles/${slug}/presence`)),
      `leaving the bubble must release the viewer, saw: ${JSON.stringify(left)}`);
    assert.equal(await page.locator(".presence-chip").count(), 0,
      "the chip belongs to the bubble editor and must not survive it");
    step("leaving the bubble releases the viewer and removes the chip");

    step("all presence checks passed");
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
