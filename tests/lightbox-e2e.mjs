#!/usr/bin/env node
/**
 * Real-browser figure-viewer regression.
 *
 * Drives the production SPA and a public share page in system Chrome against a disposable data
 * root, and checks the shared viewer on both: click a figure to open it full-screen, zoom with the
 * wheel, pan by dragging, and close. The two surfaces are different codebases loading one
 * lightbox.js, so this asserts the behaviour on each.
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
import zlib from "node:zlib";
import { chromium } from "playwright-core";

const REPO = process.cwd();
const CHROME = process.env.LOCKEDIN_E2E_CHROME || "/usr/bin/google-chrome";
const SHOTS = process.env.LOCKEDIN_E2E_SHOTS || "";
const SERVER_TIMEOUT_MS = 30_000;

// A large figure, like a real experiment plot: it must be shrunk to fit on open, and
// zooming past that must make it overflow the screen so panning has somewhere to go.
const FIGURE = (() => {
  const w = 1200, h = 900;
  const raw = Buffer.alloc((w * 3 + 1) * h);
  for (let y = 0; y < h; y++) {
    raw[y * (w * 3 + 1)] = 0;
    for (let x = 0; x < w; x++) {
      const o = y * (w * 3 + 1) + 1 + x * 3;
      raw[o] = (x * 255 / w) | 0; raw[o + 1] = (y * 255 / h) | 0; raw[o + 2] = 160;
    }
  }
  const chunk = (type, data) => {
    const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
    const body = Buffer.concat([Buffer.from(type), data]);
    const crcTable = [];
    for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; crcTable[n] = c >>> 0; }
    let crc = 0xffffffff;
    for (const b of body) crc = crcTable[(crc ^ b) & 0xff] ^ (crc >>> 8);
    const crcBuf = Buffer.alloc(4); crcBuf.writeUInt32BE((crc ^ 0xffffffff) >>> 0);
    return Buffer.concat([len, body, crcBuf]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8; ihdr[9] = 2; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr), chunk("IDAT", zlib.deflateSync(raw)), chunk("IEND", Buffer.alloc(0)),
  ]);
})();

function step(message) { process.stdout.write(`lightbox-e2e: ${message}\n`); }

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

const state = page => page.evaluate(() => {
  const img = document.querySelector(".li-lb-img");
  const overlay = document.querySelector(".li-lb");
  const t = img && getComputedStyle(img).transform;
  const m = t && t !== "none" ? t.match(/matrix\(([^)]+)\)/) : null;
  const parts = m ? m[1].split(",").map(Number) : null;
  return {
    open: !!overlay && overlay.classList.contains("on"),
    scale: parts ? parts[0] : null,
    x: parts ? parts[4] : null,
    y: parts ? parts[5] : null,
    zoomLabel: document.querySelector(".li-lb-zoom")?.textContent || "",
    caption: document.querySelector(".li-lb-cap")?.textContent || "",
    captionKatex: document.querySelectorAll(".li-lb-cap .katex").length,
    bodyLocked: document.body.style.overflow === "hidden",
  };
});

/** Open a figure, zoom it, pan it, and close it. Used against both surfaces. */
async function exerciseViewer(page, surface) {
  const figure = page.locator("#previewWrap img, #content img").first();
  await figure.waitFor({ state: "visible", timeout: 10_000 });
  await figure.click();

  let s = await state(page);
  assert.ok(s.open, `${surface}: clicking a figure must open the viewer`);
  assert.equal(s.bodyLocked, true, `${surface}: the page behind must not scroll`);
  // The viewer reuses the figure's own rendered caption, so it carries the same numbering.
  assert.match(s.caption, /^Figure 1: ?Reward/, `${surface}: the figure caption should carry over`);
  // The caption is Markdown alt text, so its math must render rather than showing raw $…$.
  assert.ok(s.captionKatex > 0, `${surface}: caption math should render via KaTeX: ${s.caption}`);
  assert.ok(!s.caption.includes("$"), `${surface}: raw math delimiters leaked: ${s.caption}`);
  // \label{} is a cross-reference anchor; the page hides it and so must the viewer.
  assert.ok(!/\\label|fig:rollout/.test(s.caption), `${surface}: a \\label leaked: ${s.caption}`);
  assert.ok(s.caption.includes("\u03c4") || s.caption.includes("Q"),
    `${surface}: rendered math should contain its symbols: ${s.caption}`);
  assert.ok(Math.abs(s.scale - 1) < 0.01, `${surface}: it should open fitted, got ${s.scale}`);
  await shoot(page, `${surface}-open`);

  // Wheel zoom, centred on the cursor.
  const box = await page.locator(".li-lb-stage").boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  for (let i = 0; i < 8; i++) await page.mouse.wheel(0, -120);
  await page.waitForTimeout(120);
  s = await state(page);
  assert.ok(s.scale > 1.5, `${surface}: the wheel should zoom in, got ${s.scale}`);
  assert.match(s.zoomLabel, /\d+%/, `${surface}: the zoom level should be shown`);
  await shoot(page, `${surface}-zoomed`);

  // Drag to pan while zoomed.
  const before = await state(page);
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 - 90, box.y + box.height / 2 - 60, { steps: 12 });
  await page.mouse.up();
  await page.waitForTimeout(120);
  s = await state(page);
  assert.ok(Math.abs(s.x - before.x) > 5 || Math.abs(s.y - before.y) > 5,
    `${surface}: dragging should pan the figure (${before.x},${before.y} -> ${s.x},${s.y})`);
  step(`${surface}: open, wheel-zoom to ${Math.round(s.scale * 100)}%, and drag-pan all work`);

  // "Fit to screen" returns to the initial state.
  await page.locator('.li-lb-bar [data-act="reset"]').click();
  await page.waitForTimeout(250);
  s = await state(page);
  assert.ok(Math.abs(s.scale - 1) < 0.01 && Math.abs(s.x) < 1 && Math.abs(s.y) < 1,
    `${surface}: fit-to-screen should reset the view, got ${JSON.stringify(s)}`);

  await page.keyboard.press("Escape");
  await page.waitForTimeout(150);
  s = await state(page);
  assert.equal(s.open, false, `${surface}: Escape must close the viewer`);
  assert.equal(s.bodyLocked, false, `${surface}: closing must restore page scrolling`);
  step(`${surface}: fit-to-screen resets and Escape closes`);
}

async function main() {
  assert.ok(fs.existsSync(CHROME), `Chrome is not installed at ${CHROME}`);
  const dataRoot = fs.mkdtempSync(path.join(os.tmpdir(), "lockedin-lightbox-e2e-"));
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
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const user = `lightbox-e2e-${Date.now()}`;
    const post = (p, data) => context.request.fetch(`${baseUrl}${p}`, { method: "POST", data });
    await post("/api/signup", { username: user, password: "temporary-password" });
    const { slug } = await (await post("/api/bubbles", { name: "Figure Viewer" })).json();
    await post(`/api/bubbles/${slug}/approve`, { instructions: "" });

    const uploaded = await context.request.fetch(`${baseUrl}/api/bubbles/${slug}/assets`, {
      method: "POST",
      multipart: { file: { name: "rollout.png", mimeType: "image/png", buffer: FIGURE } },
    });
    assert.ok(uploaded.ok(), `upload failed: ${uploaded.status()}`);

    const detail = await (await context.request.fetch(`${baseUrl}/api/bubbles/${slug}`)).json();
    const home = detail.bubble.home || "overview";
    const current = await (await context.request.fetch(`${baseUrl}/api/bubbles/${slug}/pages/${home}`)).json();
    await context.request.fetch(`${baseUrl}/api/bubbles/${slug}/pages/${home}`, {
      method: "PUT",
      data: { content: `# Figures\n\n![Reward $\\frac{1}{\\tau_R}\\nabla_a Q^\\pi(s,a)$ over training. \\label{fig:rollout}](assets/rollout.png)\n`,
              base_mtime: current.mtime ?? null },
    });

    // --- surface 1: the SPA's rendered pane (Split, then Read) ---
    const page = await context.newPage();
    page.on("pageerror", e => { throw e; });
    await page.goto(`${baseUrl}/#bubble/${slug}`, { waitUntil: "domcontentloaded" });
    await page.locator("#previewWrap").waitFor({ state: "visible", timeout: 15_000 });
    await exerciseViewer(page, "split");

    await page.selectOption("#viewModeSelect", "view").catch(async () => {
      // The selector has no stable id in every build; fall back to the visible <select>.
      const select = page.locator("select").filter({ hasText: "Read" }).first();
      await select.selectOption({ label: "👁 Read" });
    });
    await page.waitForTimeout(600);
    await exerciseViewer(page, "read");

    // --- surface 2: the public share page (no session at all) ---
    const shared = await (await post(`/api/bubbles/${slug}/share`, { active: true })).json();
    const token = shared.share_token;
    assert.ok(token, "sharing did not return a token");
    const anon = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const sharePage = await anon.newPage();
    sharePage.on("pageerror", e => { throw e; });
    await sharePage.goto(`${baseUrl}/share/${token}/${home}`, { waitUntil: "domcontentloaded" });
    await exerciseViewer(sharePage, "share");
    await anon.close();

    step("all figure-viewer checks passed on split, read, and share");
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
