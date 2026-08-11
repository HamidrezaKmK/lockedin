import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const html = fs.readFileSync(path.join(repo, "src/lockedin/web/index.html"), "utf8");
const start = html.indexOf("function commentSourceLocation");
const end = html.indexOf("function validateEditorMarkup", start);
assert.ok(start >= 0 && end > start, "could not locate the browser text-color parser");

const context = {};
vm.createContext(context);
vm.runInContext(
  `${html.slice(start, end)}\nglobalThis.parseTextColorWrappers = parseTextColorWrappers;`,
  context,
);

const source = String.raw`\textcolor{red}{First $\frac{a}{b}$ and \{literal\}}
\textcolor{#8bd3ff}{second {line}}`;
const wrappers = context.parseTextColorWrappers(source);
assert.deepEqual(Array.from(wrappers, wrapper => wrapper.color), ["red", "#8bd3ff"]);
assert.equal(wrappers[0].body, String.raw`First $\frac{a}{b}$ and \{literal\}`);
assert.equal(wrappers[1].body, "second {line}");
assert.equal(
  context.parseTextColorWrappers(String.raw`\textcolor{red}{one}\textcolor{blue}{two}`).length,
  2,
  "adjacent colors must remain valid",
);

for (const [markup, code] of [
  [String.raw`\textcolor{red}{open`, "unclosed_textcolor"],
  [String.raw`\textcolor{red;display:block}{text}`, "invalid_textcolor"],
  [String.raw`\textcolor{red}{one \textcolor{blue}{two}}`, "intersecting_textcolors"],
]) {
  assert.throws(
    () => context.parseTextColorWrappers(markup),
    error => error?.code === code && error.line >= 1 && error.column >= 1,
    `${code} was not reported consistently by the browser parser`,
  );
}

console.log("Browser text-color parser parity fixtures passed.");
