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

async function api(request, baseUrl, method, pathname, data, headers) {
  const response = await request.fetch(`${baseUrl}${pathname}`, { method, data, headers, failOnStatusCode: false });
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
async function workerPoll(request, baseUrl, token, slug, worker, workspaceId) {
  const headers = {
    Authorization: `Bearer ${token}`,
    "X-LockedIn-Scientist-Version": CLIENT_VERSION,
    "X-LockedIn-Worker": worker.id,
    "X-LockedIn-Worker-Label": worker.label,
  };
  if (worker.status) headers["X-LockedIn-Worker-Status"] = worker.status;
  if (workspaceId) headers["X-LockedIn-Workspace"] = workspaceId;
  const response = await request.fetch(
    `${baseUrl}/api/scientist/v2/bubbles/${slug}/manifest`, { headers, failOnStatusCode: false });
  return response.status();
}

/** A Scientist-side call, carrying the bearer token and the worker's presence headers. */
async function scientistApi(request, baseUrl, token, method, pathname, data, workspaceId) {
  const headers = {
    Authorization: `Bearer ${token}`,
    "X-LockedIn-Scientist-Version": CLIENT_VERSION,
    "X-LockedIn-Worker": WORKER_ID,
    "X-LockedIn-Worker-Label": WORKER_LABEL,
  };
  if (workspaceId) headers["X-LockedIn-Workspace"] = workspaceId;
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
    // A shared (non-personal) workspace, not the owner's Personal one: a personal workspace
    // cannot take a second member, and a later step needs to invite a guest into this same
    // bubble to prove agents are now filtered per owner.
    const workspaceId = (await api(context.request, baseUrl, "POST", "/api/workspaces",
      { name: "Agents E2E Workspace" })).workspace.id;
    const wsHeaders = { "X-LockedIn-Workspace": workspaceId };
    step(`shared workspace ${workspaceId} created`);
    const created = await api(context.request, baseUrl, "POST", "/api/bubbles", { name: "Agent Demo" }, wsHeaders);
    const slug = created.slug;
    await api(context.request, baseUrl, "POST", `/api/bubbles/${slug}/approve`, { instructions: "" }, wsHeaders);
    step(`bubble ${slug} created and approved`);

    const detail = await api(context.request, baseUrl, "GET", `/api/bubbles/${slug}`, undefined, wsHeaders);
    const pageSlug = detail.bubble.home || "overview";
    const targetSentence = "The variance term vanishes in the limit.";
    const filler = Array.from(
      { length: 20 }, (_, index) => `Background paragraph ${index}: ${"context ".repeat(10)}`,
    ).join("\n\n");
    const content = `# Agent Demo\n\n${filler}\n\n${targetSentence}\n\n${filler}\n`;
    await api(context.request, baseUrl, "PUT", `/api/bubbles/${slug}/pages/${pageSlug}`,
      { content, base_mtime: null }, wsHeaders);
    step(`page ${pageSlug} written with the target sentence`);

    const token = await scientistToken(context.request, baseUrl);
    step("authorized a Scientist client");

    assert.equal(await workerPoll(context.request, baseUrl, token, slug,
      { id: WORKER_ID, label: WORKER_LABEL, status: "running" }, workspaceId), 200);
    step("the worker polled once and counts as live");

    const registered = await scientistApi(context.request, baseUrl, token, "POST",
      `/api/scientist/v2/bubbles/${slug}/agents`,
      { name: "Ada", role: "reviewer", goal: "answer marks about the variance bound",
        personality: "terse", vendor: "agy", conversation: "conv-1",
        model: "gemini-3.8-flash-low", worker_id: WORKER_ID, project_label: "demo" }, workspaceId);
    const agent = registered.agent;
    assert.equal(agent.name, "Ada");
    step("registered agent Ada through the Scientist route");

    const page = await context.newPage();
    page.on("pageerror", error => { throw error; });
    let dialogAccepted = false;
    page.on("dialog", d => {
      dialogAccepted = true;
      d.accept(d.type() === "prompt" ? "temporary-agents-password" : undefined);
    });
    await page.goto(`${baseUrl}/#w/${workspaceId}/bubble/${slug}/${pageSlug}`, { waitUntil: "domcontentloaded" });
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
    assert.doesNotMatch(await adaRow.innerText(), /reviewer/i,
      "the compact agent row must not spend a second line on the role");
    const adaMore = page.getByRole("button", { name: "Show details for Ada", exact: true });
    const moreGeometry = await adaMore.evaluate(node => {
      const button=node.getBoundingClientRect(),glyph=node.querySelector("svg").getBoundingClientRect();
      return {width:button.width,height:button.height,
        dx:(button.left+button.width/2)-(glyph.left+glyph.width/2),
        dy:(button.top+button.height/2)-(glyph.top+glyph.height/2)};
    });
    assert.ok(Math.abs(moreGeometry.dx)<1&&Math.abs(moreGeometry.dy)<1,
      `three-dot glyph is not centered in its button: ${JSON.stringify(moreGeometry)}`);
    assert.ok(moreGeometry.width<=30&&moreGeometry.height<=30,
      `three-dot button is not compact: ${JSON.stringify(moreGeometry)}`);
    await adaMore.click();
    const adaProfile = page.locator(`#agent-profile-${agent.id}`);
    await adaProfile.waitFor({ state: "visible", timeout: 2_000 });
    const profileText = await adaProfile.innerText();
    for (const expected of ["reviewer", "answer marks about the variance bound", "terse",
      "agy", "gemini-3.8-flash-low", "demo"]) assert.match(profileText, new RegExp(expected, "i"));
    assert.equal(await adaMore.getAttribute("aria-expanded"), "true");
    await shoot(page, "presence-agents");
    step("the compact presence row expands Ada's complete profile from its three-dot button");

    // A selected agent is also a direct messaging surface. Queuing from here creates an ordinary
    // worker turn, and its answer stays in the same direct-message thread.
    await adaRow.click();
    const agentChat = page.getByRole("dialog", { name: "Direct messages with Ada" });
    await agentChat.waitFor({ state: "visible", timeout: 2_000 });
    const directBox = page.getByLabel("Message Ada");
    await directBox.fill("Give me a one-line status.");
    // A presence/jobs poll used to rebuild this whole menu, throwing away focus, caret and scroll
    // even though the draft value happened to be copied into a new textarea.
    await directBox.evaluate(node => { node.dataset.composerIdentity = "original"; });
    await directBox.focus();
    await page.waitForTimeout(5_500);
    assert.equal(await directBox.evaluate(node => node === document.activeElement), true,
      "polling must not interrupt typing in the direct-message composer");
    assert.equal(await directBox.getAttribute("data-composer-identity"), "original",
      "polling must preserve the composer node rather than rebuilding it");
    assert.equal(await directBox.inputValue(), "Give me a one-line status.");
    const [messageResponse] = await Promise.all([
      page.waitForResponse(response => response.request().method() === "POST"
        && new URL(response.url()).pathname === `/api/bubbles/${slug}/agents/${agent.id}/messages`),
      agentChat.getByRole("button", { name: "Queue turn", exact: true }).click(),
    ]);
    assert.ok(messageResponse.ok(), `direct message failed: ${await messageResponse.text()}`);
    const directJob = (await messageResponse.json()).job;
    const directBeat = await scientistApi(context.request, baseUrl, token, "POST",
      `/api/scientist/v2/bubbles/${slug}/agents/heartbeat`,
      { worker_id: WORKER_ID, agents: [{ id: agent.id, attached: false }], running_job_ids: [] }, workspaceId);
    assert.equal(directBeat.jobs[0].id, directJob.id);
    assert.equal(directBeat.jobs[0].mark.surface, "direct");
    await scientistApi(context.request, baseUrl, token, "POST",
      `/api/scientist/v2/bubbles/${slug}/jobs/${directJob.id}/start`, { worker_id: WORKER_ID }, workspaceId);
    await scientistApi(context.request, baseUrl, token, "POST",
      `/api/scientist/v2/bubbles/${slug}/jobs/${directJob.id}/reply`,
      { text: "The variance review is ready." }, workspaceId);
    await page.waitForFunction(() => {
      const history = document.querySelector(".agent-message-history");
      return history && history.textContent.includes("The variance review is ready.");
    }, { timeout: 9_000 });
    assert.equal(await directBox.evaluate(node => document.activeElement === node), true,
      "the stable composer must regain focus after the queued turn");

    // Continue the same direct thread with another ordinary turn; the first exchange stays put.
    await directBox.fill("What should I do next?");
    const [followupResponse] = await Promise.all([
      page.waitForResponse(response => response.request().method() === "POST"
        && new URL(response.url()).pathname === `/api/bubbles/${slug}/agents/${agent.id}/messages`),
      agentChat.getByRole("button", { name: "Queue turn", exact: true }).click(),
    ]);
    const followupJob = (await followupResponse.json()).job;
    const followupBeat = await scientistApi(context.request, baseUrl, token, "POST",
      `/api/scientist/v2/bubbles/${slug}/agents/heartbeat`,
      { worker_id: WORKER_ID, agents: [{ id: agent.id, attached: false }], running_job_ids: [] }, workspaceId);
    assert.equal(followupBeat.jobs[0].id, followupJob.id);
    await scientistApi(context.request, baseUrl, token, "POST",
      `/api/scientist/v2/bubbles/${slug}/jobs/${followupJob.id}/start`, { worker_id: WORKER_ID }, workspaceId);
    await scientistApi(context.request, baseUrl, token, "POST",
      `/api/scientist/v2/bubbles/${slug}/jobs/${followupJob.id}/reply`,
      { text: "Review the updated bound." }, workspaceId);
    await page.waitForFunction(() => {
      const history = document.querySelector(".agent-message-history");
      return history && history.textContent.includes("The variance review is ready.")
        && history.textContent.includes("What should I do next?")
        && history.textContent.includes("Review the updated bound.");
    }, { timeout: 9_000 });
    step("queued and displayed a multi-turn direct thread from the agent list");

    await page.keyboard.press("Escape");
    await page.mouse.click(700, 500);
    await presenceMenu.waitFor({ state: "hidden", timeout: 2_000 }).catch(() => {});
    if (await presenceMenu.count()) await presenceMenu.evaluate(node => node.remove());

    // The chalk-talk prompt can go straight to a registered agent; copying remains available
    // for agents outside LockedIn, but is no longer a required detour for Ada.
    await page.evaluate(route => { location.hash = route; }, `#w/${workspaceId}/bubble/${slug}`);
    const addTalk = page.locator("[data-newtalk]");
    await addTalk.waitFor({ state: "visible", timeout: 10_000 });
    await addTalk.click();
    await page.locator("[data-auto]").click();
    const talkPrompt = page.getByRole("heading", { name: "Ask an agent for a chalk talk" }).locator("..");
    await talkPrompt.locator('[data-f="topic"]').fill("the vanishing variance term");
    await talkPrompt.locator('[data-f="notes"]').fill("Keep it to four slides.");
    const expectedTalkPrompt = await talkPrompt.locator("[data-out]").innerText();
    await talkPrompt.getByRole("button", { name: "Assign", exact: true }).click();
    const promptAgentMenu = page.locator(".tk-agentmenu");
    await promptAgentMenu.waitFor({ state: "visible", timeout: 3_000 });
    assert.ok(await promptAgentMenu.evaluate(node => Number(getComputedStyle(node).zIndex)) > 970,
      "the agent picker must appear above the chalk-talk prompt");
    const [talkAssignResponse] = await Promise.all([
      page.waitForResponse(response => response.request().method() === "POST"
        && new URL(response.url()).pathname === `/api/bubbles/${slug}/agents/${agent.id}/messages`),
      promptAgentMenu.locator("button.tk-am[data-agent]").first().click(),
    ]);
    assert.ok(talkAssignResponse.ok(), `chalk-talk assignment failed: ${await talkAssignResponse.text()}`);
    const talkJob = (await talkAssignResponse.json()).job;
    assert.equal(talkJob.instruction, expectedTalkPrompt,
      "Assign must queue exactly the prompt shown beside Copy");
    await scientistApi(context.request, baseUrl, token, "POST",
      `/api/scientist/v2/bubbles/${slug}/jobs/${talkJob.id}/start`, { worker_id: WORKER_ID }, workspaceId);
    await scientistApi(context.request, baseUrl, token, "POST",
      `/api/scientist/v2/bubbles/${slug}/jobs/${talkJob.id}/reply`,
      { text: "The chalk talk is synced." }, workspaceId);
    await page.evaluate(route => { location.hash = route; },
      `#w/${workspaceId}/bubble/${slug}/${pageSlug}`);
    await page.waitForFunction(expected => document.querySelector("#previewWrap")?.textContent.includes(expected),
      targetSentence, { timeout: 10_000 });
    step("assigned the generated chalk-talk prompt directly to Ada");

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
    const [assignResponse] = await Promise.all([
      page.waitForResponse(response => response.request().method() === "POST"
        && new URL(response.url()).pathname === `/api/bubbles/${slug}/jobs`),
      agentMenu.locator("button.tk-am[data-agent]").first().click(),
    ]);
    assert.ok(assignResponse.ok(),
      `assigning Ada failed (${assignResponse.status()}): ${await assignResponse.text()}`);
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
      { worker_id: WORKER_ID, agents: [{ id: agent.id, attached: false }], running_job_ids: [] }, workspaceId);
    assert.equal(heartbeat.jobs.length, 1, `expected exactly one queued job, got ${JSON.stringify(heartbeat.jobs)}`);
    const job = heartbeat.jobs[0];
    assert.match(job.mark.quote, /variance term/, "the job's mark pointer is missing the quoted text");
    step("heartbeat handed the worker one queued job");

    await scientistApi(context.request, baseUrl, token, "POST",
      `/api/scientist/v2/bubbles/${slug}/jobs/${job.id}/start`, { worker_id: WORKER_ID }, workspaceId);
    await page.waitForSelector(".tk-note[data-jobkey] .tk-job.running", { timeout: 9_000 });
    step("the chip turned running within one poll cycle");

    await scientistApi(context.request, baseUrl, token, "POST",
      `/api/scientist/v2/bubbles/${slug}/jobs/${job.id}/reply`,
      { text: "I wrote the bound in one line." }, workspaceId);
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

    // ---- step 7: a second user, invited into the same workspace, only ever sees their own
    // agents. Ada is the owner's — a guest with no agents of their own must see none of her,
    // even on the exact same bubble and the exact same mark she already answered. ----
    const guestUsername = `agents-e2e-guest-${Date.now()}`;
    const guestPassword = "temporary-guest-password";
    await api(context.request, baseUrl, "POST", "/api/signup", { username: guestUsername, password: guestPassword });
    await api(context.request, baseUrl, "PUT", `/api/admin/users/${guestUsername}/approval`, { approved: true });
    await api(context.request, baseUrl, "POST", `/api/workspaces/${workspaceId}/members`, { username: guestUsername });
    step(`invited ${guestUsername} into the shared workspace`);

    const guestContext = await browser.newContext({ viewport: { width: 1500, height: 950 } });
    await api(guestContext.request, baseUrl, "POST", "/api/login", { username: guestUsername, password: guestPassword });
    const guestPage = await guestContext.newPage();
    guestPage.on("pageerror", error => { throw error; });
    await guestPage.goto(`${baseUrl}/#w/${workspaceId}/bubble/${slug}/${pageSlug}`, { waitUntil: "domcontentloaded" });
    await guestPage.waitForFunction(expected => {
      const node = document.querySelector("#previewWrap");
      return node && node.textContent && node.textContent.includes(expected);
    }, targetSentence, { timeout: 30_000 });
    step("the invited guest opened the very same bubble page");

    const guestAgentsSeg = guestPage.locator(".presence-seg").nth(1);
    await guestAgentsSeg.waitFor({ state: "visible", timeout: 10_000 });
    assert.equal((await guestAgentsSeg.locator(".presence-count").innerText()).trim(), "0",
      "the guest's own presence pill must read zero agents even though Ada exists for the owner");
    step("the guest's presence pill reads zero agents while the owner's still shows Ada");

    assert.equal(await guestPage.locator(".tk-note[data-jobkey] .tk-job").count(), 0,
      "the guest must not see the owner's completed job chip");
    const guestOverview = await api(guestContext.request, baseUrl, "GET",
      `/api/bubbles/${slug}/agents`, undefined, wsHeaders);
    assert.deepEqual(guestOverview.agents || [], [], "the guest API must not expose Ada");
    assert.deepEqual((guestOverview.jobs && guestOverview.jobs.by_mark) || {}, {},
      "the guest API must not expose any of the owner's jobs");
    step("the guest sees neither Ada nor her completed job in the UI or API");
    await guestContext.close();

    // Keep one unanswered mark around for the secure-mode UI checks below. Its ordinary assign
    // button is a cleaner assertion than reusing the completed job's redo menu after navigation.
    const secureTarget = "Background paragraph 2:";
    await selectPreview(page, secureTarget);
    await page.waitForTimeout(150);
    const securePicker = page.locator(".tk-pop");
    await securePicker.waitFor({ state: "visible", timeout: 5_000 });
    await securePicker.locator("[data-pin]").click();
    const secureMark = page.locator(".tk-note", { hasText: secureTarget });
    await secureMark.locator("[data-assign]").waitFor({ state: "visible", timeout: 10_000 });
    step("pinned a second, unanswered mark for the secure-mode assignment check");

    // ---- step 8: Stop agents is reversible, but still confirms before revoking clients,
    // cancelling work, and stopping sync. Agent identities and conversations must survive. ----
    const sideSwitch = page.locator("#sideSecureSwitch");
    assert.equal(await page.locator("#sideSecureIcon").getAttribute("href"), "#li-i-lock-open",
      "enabled agents must show the open lock");
    await sideSwitch.waitFor({ state: "visible", timeout: 5_000 });
    assert.equal(await sideSwitch.getAttribute("aria-checked"), "false", "secure mode starts off");
    await sideSwitch.click();
    const stopDialog = page.getByRole("dialog", { name: "Stop agents confirmation" });
    await stopDialog.waitFor({ state: "visible", timeout: 2_000 });
    const consequences = (await stopDialog.innerText()).toLowerCase();
    for (const word of ["revoked", "retained", "cancelled", "sync workers", "recovery command"]) {
      assert.ok(consequences.includes(word), `the confirmation is missing ${word}:\n${consequences}`);
    }
    await stopDialog.getByRole("button", { name: "Cancel" }).click();
    assert.equal(await sideSwitch.getAttribute("aria-checked"), "false",
      "cancelling the confirmation must leave agents running");
    await sideSwitch.click();
    await stopDialog.getByRole("button", { name: "Yes, stop agents" }).click();
    await page.waitForFunction(() => document.getElementById("sideSecureSwitch")?.getAttribute("aria-checked") === "true",
      { timeout: 5_000 });
    step("the Stop agents switch required an explicit consequences confirmation");

    await page.waitForSelector("#secureModeBanner", { timeout: 5_000 });
    assert.match(await page.locator("#secureModeBanner").innerText(), /agents stopped/i);
    assert.equal(await page.locator("#sideSecureIcon").getAttribute("href"), "#li-i-lock",
      "stopped agents must show the closed lock");
    assert.equal(await page.locator("#secureModeBanner button").count(), 0,
      "the stopped-agents banner must not keep a top-right Settings button");
    step("the banner appeared on the bubble view without a reload");

    await page.locator('.navbtn[data-view="settings"]').click();
    await page.waitForSelector("#secureModeSection", { timeout: 10_000 });
    await page.waitForFunction(() =>
      document.querySelector("#secureModeSection input[type=checkbox]")?.checked === true, { timeout: 5_000 });
    assert.equal(await page.locator("#sideSecureSwitch").getAttribute("aria-checked"), "true",
      "the sidebar switch must still read on while looking at Settings");
    step("the Settings card's own toggle reflects the sidebar switch, with no reload between them");

    // Back to the bubble (SPA history, not a reload): Ada remains as a stopped identity, her
    // worker is absent, and the unanswered mark cannot dispatch while secure mode is on.
    await page.goBack({ waitUntil: "domcontentloaded" });
    await page.waitForFunction(expected => {
      const node = document.querySelector("#previewWrap");
      return node && node.textContent && node.textContent.includes(expected);
    }, targetSentence, { timeout: 30_000 });
    await page.waitForFunction(() => {
      const seg = document.querySelectorAll(".presence-seg")[1];
      return !!seg && seg.classList.contains("sync-dead");
    }, { timeout: 10_000 });
    step("back on the bubble, the pill's agents segment turned dead-coloured");

    assert.equal((await page.locator(".presence-seg").nth(1).locator(".presence-count").innerText()).trim(), "1");
    const stoppedAssign = secureMark.locator("[data-assign]");
    if (await stoppedAssign.count()) {
      await stoppedAssign.click();
      const stoppedMenu = page.locator(".tk-agentmenu");
      await stoppedMenu.waitFor({ state: "visible", timeout: 2_000 });
      assert.match(await stoppedMenu.innerText(), /secure mode is on/i);
      assert.equal(await stoppedMenu.locator("[data-agent]").isDisabled(), true);
      await page.keyboard.press("Escape");
    }
    const stoppedOverview = await api(context.request, baseUrl, "GET",
      `/api/bubbles/${slug}/agents`, undefined, wsHeaders);
    assert.equal(stoppedOverview.agents.length, 1);
    assert.equal(stoppedOverview.agents[0].name, "Ada");
    assert.equal(stoppedOverview.agents[0].status, "stopped");
    assert.equal(stoppedOverview.agents[0].personality, "terse");
    assert.equal(stoppedOverview.agents[0].conversation, "conv-1");
    assert.equal(stoppedOverview.agents[0].revive_required, true);
    const revokedPoll = await context.request.fetch(
      `${baseUrl}/api/scientist/v2/bubbles/${slug}/manifest`, {
        headers: { Authorization: `Bearer ${token}`, "X-LockedIn-Workspace": workspaceId,
          "X-LockedIn-Scientist-Version": CLIENT_VERSION }, failOnStatusCode: false });
    assert.equal(revokedPoll.status(), 401, "the old Scientist token must be revoked");
    step("Ada's persona was retained while her worker and old Scientist authorization were stopped");

    // Turning the setting off requires the password. Ada becomes offline and her popup mints a
    // one-use recovery line that updates, reauthorizes and resumes the original folder.
    await sideSwitch.click();
    await page.waitForFunction(() => document.getElementById("sideSecureSwitch")?.getAttribute("aria-checked") === "false",
      { timeout: 5_000 });
    assert.equal(await page.locator("#sideSecureIcon").getAttribute("href"), "#li-i-lock-open",
      "turning stop-agents off must restore the open lock");
    await page.waitForFunction(() => !document.getElementById("secureModeBanner"), { timeout: 5_000 });
    await page.waitForFunction(() => {
      const seg = document.querySelectorAll(".presence-seg")[1];
      return !!seg && !seg.classList.contains("sync-dead");
    }, { timeout: 10_000 });
    assert.equal((await page.locator(".presence-seg").nth(1).locator(".presence-count").innerText()).trim(), "1");
    await page.locator(".presence-seg").nth(1).click();
    const stoppedAda = page.locator(".presence-item.presence-agent", { hasText: "Ada" });
    await stoppedAda.waitFor({ state: "visible", timeout: 5_000 });
    assert.match(await stoppedAda.innerText(), /offline/i);
    await stoppedAda.click();
    const recoveryChat = page.getByRole("dialog", { name: "Direct messages with Ada" });
    const recoveryCode = recoveryChat.locator(".agent-revive-copy");
    await recoveryCode.waitFor({ state: "visible", timeout: 5_000 });
    const recoveryTabs = recoveryChat.locator(".agent-revive-tabs");
    assert.deepEqual(await recoveryTabs.locator("button").allInnerTexts(), ["macOS", "Linux", "Windows"],
      "recovery must always expose every target OS instead of guessing from the browser");
    assert.equal(await recoveryTabs.locator("button.active").innerText(), "Linux",
      "a remote-friendly Linux command is the deterministic default");
    const unixRecovery = await recoveryCode.innerText();
    assert.match(unixRecovery, /setup\/.+\.sh/,
      "Linux recovery must use a fresh setup ticket, not a revoked cached token");
    await recoveryTabs.getByRole("button", { name: "Windows", exact: true }).click();
    assert.match(await recoveryCode.innerText(), /setup\/.+\.ps1/,
      "choosing Windows must switch to the PowerShell recovery command");
    await recoveryTabs.getByRole("button", { name: "macOS", exact: true }).click();
    assert.equal(await recoveryCode.innerText(), unixRecovery,
      "choosing macOS must switch back to the Unix recovery command");
    assert.match(await recoveryChat.innerText(), /upgrades Scientist.*reauthorizes this laptop/is);
    step("password-confirmed disable exposed explicit macOS, Linux, and Windows recovery commands");

    // Retirement is available from the agent dialog, calls the owner-scoped delete route, and
    // immediately removes the agent from the bubble without requiring a page reload.
    dialogAccepted = false;
    const [retireResponse] = await Promise.all([
      page.waitForResponse(response => response.request().method() === "DELETE"
        && new URL(response.url()).pathname === `/api/bubbles/${slug}/agents/${agent.id}`),
      recoveryChat.getByRole("button", { name: "Retire Ada", exact: true }).click(),
    ]);
    assert.ok(retireResponse.ok(), `retire failed: ${await retireResponse.text()}`);
    assert.equal(dialogAccepted, true, "retiring an agent must require confirmation");
    await page.waitForFunction(() => {
      const seg = document.querySelectorAll(".presence-seg")[1];
      return !!seg && seg.querySelector(".presence-count")?.textContent === "0";
    }, { timeout: 5_000 });
    assert.equal(await page.getByRole("dialog", { name: "Direct messages with Ada" }).count(), 0,
      "retiring an agent must close its dialog");
    step("Ada was retired from the dialog and disappeared from the bubble immediately");

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
