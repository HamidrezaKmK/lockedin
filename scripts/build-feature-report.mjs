#!/usr/bin/env node

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath, pathToFileURL } from "node:url";
import { chromium } from "playwright-core";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(scriptDir, "..");
const source = path.join(repo, "docs", "FEATURE_REPORT.md");
const css = path.join(repo, "docs", "feature-report.css");
const output = path.join(repo, "docs", "LockedIn-Feature-Report.pdf");
const buildDir = path.join(repo, "tests", ".tmp", "feature-report");
const html = path.join(buildDir, "feature-report.html");
const chrome = process.env.LOCKEDIN_E2E_CHROME || "/usr/bin/google-chrome";

for (const required of [source, css, chrome]) {
  assert.ok(fs.existsSync(required), `Required file does not exist: ${required}`);
}
fs.mkdirSync(buildDir, { recursive: true });

execFileSync("pandoc", [
  source,
  "--from=gfm+raw_html",
  "--to=html5",
  "--standalone",
  "--embed-resources",
  `--resource-path=${path.dirname(source)}`,
  `--css=${css}`,
  `--output=${html}`,
], { cwd: repo, stdio: "inherit" });

const browser = await chromium.launch({
  executablePath: chrome,
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.goto(pathToFileURL(html).href, { waitUntil: "load" });
  await page.emulateMedia({ media: "print" });
  await page.pdf({
    path: output,
    format: "A4",
    printBackground: true,
    preferCSSPageSize: true,
    displayHeaderFooter: true,
    headerTemplate: "<span></span>",
    footerTemplate: '<div style="font:8px system-ui;color:#7a8792;text-align:center;width:100%"><span class="pageNumber"></span></div>',
    margin: { top: "15mm", right: "14mm", bottom: "17mm", left: "14mm" },
  });
} finally {
  await browser.close();
}

process.stdout.write(`Wrote ${path.relative(repo, output)}\n`);
