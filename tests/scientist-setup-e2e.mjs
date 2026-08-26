#!/usr/bin/env node
/**
 * Real-browser regression for a bubble's 🤖 "connect an agent" dialog.
 *
 * Connecting a project used to be five commands with two ids transcribed by hand. The dialog
 * hands over one line instead. This drives it against a disposable data root: the robot sits
 * beside the presence chip, the dialog mints a link, the OS tabs swap between the curl and the
 * PowerShell form, the snippet carries the ticket, Copy reports success, and the script the link
 * points at actually serves. Nothing under the repository's real data/ directory is touched.
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
  process.stdout.write(`scientist-setup-e2e: ${message}\n`);
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
  const dataRoot = fs.mkdtempSync(path.join(os.tmpdir(), "lockedin-setup-e2e-"));
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
    const context = await browser.newContext({
      viewport: { width: 1500, height: 950 },
      permissions: ["clipboard-read", "clipboard-write"],
    });
    const username = `setup-e2e-${Date.now()}`;
    await api(context.request, baseUrl, "POST", "/api/signup", { username, password: "temporary-setup-password" });
    const { slug } = await api(context.request, baseUrl, "POST", "/api/bubbles", { name: "Setup E2E" });
    await api(context.request, baseUrl, "POST", `/api/bubbles/${slug}/approve`, { instructions: "" });

    const page = await context.newPage();
    page.on("pageerror", error => { throw error; });
    await page.goto(`${baseUrl}/#bubble/${slug}`, { waitUntil: "domcontentloaded" });

    // The robot belongs beside the presence chip: that chip says who is syncing this bubble.
    const robot = page.locator(".bubble-title .robot-btn");
    await robot.waitFor({ state: "visible", timeout: 10_000 });
    const order = await page.locator(".bubble-title > *").evaluateAll(
      nodes => nodes.map(node => node.className));
    const chip = order.findIndex(name => String(name).includes("presence"));
    const bot = order.findIndex(name => String(name).includes("robot-btn"));
    assert.ok(chip >= 0 && bot === chip + 1, `the robot must sit next to the presence chip: ${order}`);
    await shoot(page, "setup-robot");
    step("the robot sits beside the presence chip");

    await robot.click();
    const dialog = page.getByRole("dialog", { name: "Connect an agent" });
    const snippet = dialog.locator(".setup-snippet");
    await dialog.waitFor({ state: "visible", timeout: 5_000 });
    await page.waitForFunction(
      () => !/Preparing/.test(document.querySelector(".setup-snippet")?.textContent || ""),
      null, { timeout: 10_000 });

    const unix = await snippet.innerText();
    const ticket = (unix.match(/setup\/([\w-]+)\.sh/) || [])[1];
    assert.ok(ticket, `the snippet must carry a ticket: ${unix}`);
    assert.ok(unix.startsWith("curl "), `a unix snippet must curl: ${unix}`);
    assert.ok(unix.includes(baseUrl), `the link must point at this server: ${unix}`);
    // Say plainly what the link is — it authorizes whoever runs it.
    assert.match(await dialog.locator(".setup-note").innerText(), /single-use/i);
    await shoot(page, "setup-dialog");
    step("the dialog mints a link and says what it is");

    await dialog.locator(".help-tab", { hasText: "Windows" }).click();
    const win = await snippet.innerText();
    assert.ok(win.includes("irm ") && win.includes(".ps1"), `Windows must get PowerShell: ${win}`);
    assert.ok(win.includes(ticket), "both shells must use the same ticket");
    await dialog.locator(".help-tab", { hasText: "macOS" }).click();
    assert.equal(await snippet.innerText(), unix, "switching back restores the curl form");
    step("the OS tabs swap between curl and PowerShell");

    await dialog.locator("button", { hasText: /^Copy$/ }).click();
    await page.waitForFunction(
      () => /Copied/.test([...document.querySelectorAll(".create-dialog-footer button")]
        .map(node => node.textContent).join(" ")), null, { timeout: 5_000 });
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), unix,
      "the copied text must be the snippet itself");
    step("Copy puts the snippet on the clipboard and says so in the button");

    // The link is not decoration: it has to serve a runnable script.
    const script = await context.request.get(`${baseUrl}/setup/${ticket}.sh`);
    assert.ok(script.ok(), `the setup script must serve (${script.status()})`);
    const text = await script.text();
    for (const expected of ["install.sh | bash", "lockedin-scientist connect", "< /dev/tty", slug]) {
      assert.ok(text.includes(expected), `the served script is missing ${expected}:\n${text}`);
    }
    assert.ok(!text.includes("li_sc_"), "serving the script must not leak the token");
    step("the link serves a script that installs, connects, and can still prompt");

    // Same one control surface on a phone, where this flow matters most.
    await page.setViewportSize({ width: 390, height: 800 });
    await shoot(page, "setup-dialog-phone");
    const box = await dialog.locator(".setup-dialog").boundingBox();
    assert.ok(box && box.width <= 390, `the dialog must fit a phone: ${JSON.stringify(box)}`);
    step("the dialog fits a phone viewport");

    process.stdout.write("scientist-setup-e2e: all connect-an-agent checks passed\n");
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
