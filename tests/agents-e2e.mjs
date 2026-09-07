#!/usr/bin/env node
/**
 * Real-browser regression for agents: named CLI conversations a mark can be handed to.
 *
 * Starts LockedIn against a disposable data root, signs up a disposable owner, creates a bubble
 * and a page, mints a Scientist token, registers a worker and an agent through the v2 sync
 * routes, then drives the production SPA in system Chrome through the whole lifecycle of a mark
 * assigned to that agent: pin -> assign -> queued -> running -> done, plus retiring the agent
 * from the presence menu. Nothing under the repository's real data/ directory is read or changed.
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
const WORKER_ID = "w-e2e";
const WORKER_LABEL = "demo";

function step(message) {
  process.stdout.write(`agents-e2e: ${message}\n`);
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
async function workerPoll(request, baseUrl, token, slug, worker) {
  const headers = {
    Authorization: `Bearer ${token}`,
    "X-LockedIn-Scientist-Version": CLIENT_VERSION,
    "X-LockedIn-Worker": worker.id,
    "X-LockedIn-Worker-Label": worker.label,
  };
  if (worker.status) headers["X-LockedIn-Worker-Status"] = worker.status;
  const response = await request.fetch(
    `${baseUrl}/api/scientist/v2/bubbles/${slug}/manifest`, { headers, failOnStatusCode: false });
  return response.status();
}

/** A Scientist-side call, carrying the bearer token and the worker's presence headers. */
async function scientistApi(request, baseUrl, token, method, pathname, data) {
  const headers = {
    Authorization: `Bearer ${token}`,
    "X-LockedIn-Scientist-Version": CLIENT_VERSION,
    "X-LockedIn-Worker": WORKER_ID,
    "X-LockedIn-Worker-Label": WORKER_LABEL,
  };
  const response = await request.fetch(`${baseUrl}${pathname}`, { method, data, headers, failOnStatusCode: false });
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

/** Select rendered text in the preview pane and let the picker's mouseup handler see it. */
async function selectPreview(page, selected, occurrence = 0) {
  return page.locator("#previewWrap").evaluate(
    (node, { selected, occurrence }) => {
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
      const end = start + selected.length;
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
    { selected, occurrence },
  );
}

async function main() {
  assert.ok(fs.existsSync(CHROME), `Chrome is not installed at ${CHROME}`);
  const dataRoot = fs.mkdtempSync(path.join(os.tmpdir(), "lockedin-agents-e2e-"));
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
    const username = `agents-e2e-${Date.now()}`;
    await api(context.request, baseUrl, "POST", "/api/signup", { username, password: "temporary-agents-password" });
    const created = await api(context.request, baseUrl, "POST", "/api/bubbles", { name: "Agent Demo" });
    const slug = created.slug;
    await api(context.request, baseUrl, "POST", `/api/bubbles/${slug}/approve`, { instructions: "" });
    step(`bubble ${slug} created and approved`);

    const detail = await api(context.request, baseUrl, "GET", `/api/bubbles/${slug}`);
    const pageSlug = detail.bubble.home || "overview";
    const targetSentence = "The variance term vanishes in the limit.";
    const filler = Array.from(
      { length: 20 }, (_, index) => `Background paragraph ${index}: ${"context ".repeat(10)}`,
    ).join("\n\n");
    const content = `# Agent Demo\n\n${filler}\n\n${targetSentence}\n\n${filler}\n`;
    await api(context.request, baseUrl, "PUT", `/api/bubbles/${slug}/pages/${pageSlug}`,
      { content, base_mtime: null });
    step(`page ${pageSlug} written with the target sentence`);

    const token = await scientistToken(context.request, baseUrl);
    step("authorized a Scientist client");

    assert.equal(await workerPoll(context.request, baseUrl, token, slug,
      { id: WORKER_ID, label: WORKER_LABEL, status: "running" }), 200);
    step("the worker polled once and counts as live");

    const registered = await scientistApi(context.request, baseUrl, token, "POST",
      `/api/scientist/v2/bubbles/${slug}/agents`,
      { name: "Ada", role: "reviewer", goal: "answer marks about the variance bound",
        personality: "terse", vendor: "agy", conversation: "conv-1",
        model: "gemini-3.8-flash-low", worker_id: WORKER_ID, project_label: "demo" });
    const agent = registered.agent;
    assert.equal(agent.name, "Ada");
    step("registered agent Ada through the Scientist route");

    const page = await context.newPage();
    page.on("pageerror", error => { throw error; });
    let dialogAccepted = false;
    page.on("dialog", d => { dialogAccepted = true; d.accept(); });
    await page.goto(`${baseUrl}/#bubble/${slug}/${pageSlug}`, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(expected => {
      const node = document.querySelector("#previewWrap");
      return node && node.textContent && node.textContent.includes(expected);
    }, targetSentence, { timeout: 30_000 });
    step("bubble page loaded in the browser");

    // ---- step 3: the presence pill's syncing segment reports the agent ----
    const card = page.locator(".presence .presence-group-card");
    await card.waitFor({ state: "visible", timeout: 10_000 });
    const syncSeg = page.locator(".presence-seg").nth(1);
    await page.waitForFunction(() => {
      const seg = document.querySelectorAll(".presence-seg")[1];
      return !!seg && /1 agent\b/.test(seg.getAttribute("title") || "");
    }, { timeout: 10_000 });
    assert.match(await syncSeg.getAttribute("title"), /1 agent\b/,
      "the syncing segment must mention the one registered agent");
    await syncSeg.click();
    const presenceMenu = page.locator(".presence-menu");
    await presenceMenu.waitFor({ state: "visible", timeout: 2_000 });
    const adaRow = page.locator(".presence-item.presence-agent", { hasText: "Ada" });
    await adaRow.waitFor({ state: "visible", timeout: 2_000 });
    await shoot(page, "presence-agents");
    step("the presence menu lists Ada under her worker");

    await page.keyboard.press("Escape");
    await page.mouse.click(700, 500);
    await presenceMenu.waitFor({ state: "hidden", timeout: 2_000 }).catch(() => {});
    if (await presenceMenu.count()) await presenceMenu.evaluate(node => node.remove());

    // ---- step 4: pin a mark on the sentence, then assign it to Ada from its card ----
    const rightToggle = page.locator("#paneRightToggle");
    if (await rightToggle.count() && !(await rightToggle.evaluate(node => node.classList.contains("on")))) {
      await rightToggle.click();
    }
    await selectPreview(page, targetSentence);
    await page.waitForTimeout(150);
    const pop = page.locator(".tk-pop");
    await pop.waitFor({ state: "visible", timeout: 5_000 });
    await pop.locator("[data-pin]").click();
    step("pinned the mark");

    const pinnedCard = page.locator(".tk-note[data-jobkey]").first();
    await pinnedCard.waitFor({ state: "visible", timeout: 10_000 });
    const assignBtn = pinnedCard.locator("[data-assign]");
    await assignBtn.waitFor({ state: "visible", timeout: 5_000 });
    await assignBtn.click();
    const agentMenu = page.locator(".tk-agentmenu");
    await agentMenu.waitFor({ state: "visible", timeout: 2_000 });
    await agentMenu.locator("button.tk-am[data-agent]").first().click();
    step("picked Ada from the assign menu");

    const jobKeyCard = page.locator(".tk-note[data-jobkey]").filter({ has: page.locator(".tk-job") });
    await jobKeyCard.first().waitFor({ state: "visible", timeout: 10_000 });
    const chip = jobKeyCard.first().locator(".tk-job");
    await page.waitForFunction(() => {
      const el = document.querySelector(".tk-note[data-jobkey] .tk-job");
      return !!el && el.classList.contains("queued") && el.textContent.includes("Ada");
    }, { timeout: 10_000 });
    await shoot(page, "chip-queued");
    step("the mark card shows a queued chip naming Ada");

    const mark = page.locator(".tk-note[data-jobkey]").first();
    const jobKey = await mark.getAttribute("data-jobkey");
    assert.ok(jobKey, "the mark card is missing its job key");

    // ---- step 5: run the job through the Scientist routes ----
    const heartbeat = await scientistApi(context.request, baseUrl, token, "POST",
      `/api/scientist/v2/bubbles/${slug}/agents/heartbeat`,
      { worker_id: WORKER_ID, agents: [{ id: agent.id, attached: false }], running_job_ids: [] });
    assert.equal(heartbeat.jobs.length, 1, `expected exactly one queued job, got ${JSON.stringify(heartbeat.jobs)}`);
    const job = heartbeat.jobs[0];
    assert.match(job.mark.quote, /variance term/, "the job's mark pointer is missing the quoted text");
    step("heartbeat handed the worker one queued job");

    await scientistApi(context.request, baseUrl, token, "POST",
      `/api/scientist/v2/bubbles/${slug}/jobs/${job.id}/start`, { worker_id: WORKER_ID });
    await page.waitForSelector(".tk-note[data-jobkey] .tk-job.running", { timeout: 9_000 });
    step("the chip turned running within one poll cycle");

    await scientistApi(context.request, baseUrl, token, "POST",
      `/api/scientist/v2/bubbles/${slug}/jobs/${job.id}/reply`,
      { text: "I wrote the bound in one line." });
    await page.waitForSelector(".tk-note[data-jobkey] .tk-job.done", { timeout: 9_000 });
    await page.waitForFunction(() => {
      const card = document.querySelector(".tk-note[data-jobkey]");
      if (!card) return false;
      return Array.from(card.querySelectorAll(".tk-turn.agent")).some(turn =>
        turn.textContent.includes("Ada") && turn.textContent.includes("I wrote the bound"));
    }, { timeout: 9_000 });
    await shoot(page, "chip-done");
    step("the chip turned done and the agent's reply landed in the thread");

    // ---- step 6: the chip still opens a reassign menu; the card's own assign button is gone
    // now that Ada has answered, since redoing an answered mark belongs to the chip's "redo
    // with" menu, not to a second assign button sitting next to a reply that already exists ----
    const doneChip = page.locator(".tk-note[data-jobkey] .tk-job.done").first();
    await doneChip.click();
    const doneMenu = page.locator(".tk-agentmenu");
    await doneMenu.waitFor({ state: "visible", timeout: 2_000 });
    assert.match(await doneMenu.locator(".tk-am-title").innerText(), /Done by Ada/i);
    await page.keyboard.press("Escape");
    await doneMenu.waitFor({ state: "hidden", timeout: 2_000 });

    assert.equal(await page.locator(".tk-note[data-jobkey] [data-assign]").count(), 0,
      "the card's assign button must be gone once an agent has answered");
    step("the chip still offers redo, and the card's own assign button is gone now that Ada answered");

    // ---- step 7: retire Ada from the presence menu ----
    await syncSeg.click();
    await presenceMenu.waitFor({ state: "visible", timeout: 2_000 });
    await adaRow.waitFor({ state: "visible", timeout: 2_000 });
    await adaRow.click();
    const retireBtn = page.locator(".presence-detail button", { hasText: "retire" });
    await retireBtn.waitFor({ state: "visible", timeout: 2_000 });
    await retireBtn.click();
    await page.waitForTimeout(400);
    assert.ok(dialogAccepted, "retiring an agent must ask for confirmation");
    const afterRetire = await api(context.request, baseUrl, "GET", `/api/bubbles/${slug}/agents`);
    assert.equal((afterRetire.agents || []).length, 0, "the agent must be gone after retiring");
    step("Ada was retired through the presence menu");

    step("all agents checks passed");
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
