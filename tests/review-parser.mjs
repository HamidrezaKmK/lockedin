import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const html = fs.readFileSync(path.join(repo, "src/lockedin/web/index.html"), "utf8");
const start = html.indexOf("function commentSourceLocation");
const end = html.indexOf("function validateEditorCommentMarkup", start);
assert.ok(start >= 0 && end > start, "could not locate the browser review parser");

const context = {};
vm.createContext(context);
vm.runInContext(
  `${html.slice(start, end)}\nglobalThis.parseCommentWrappers = parseCommentWrappers;`,
  context,
);

const source = String.raw`\comment{x}{First $\frac{a}{b}$ and \{literal\}}
\comment{long-id_123}{second
line}`;
const wrappers = context.parseCommentWrappers(source);
assert.deepEqual(Array.from(wrappers, wrapper => wrapper.id), ["x", "long-id_123"]);
assert.equal(wrappers[0].body, String.raw`First $\frac{a}{b}$ and \{literal\}`);
assert.equal(wrappers[1].body, "second\nline");

assert.equal(context.parseCommentWrappers(String.raw`\\comment{x}{literal}`).length, 0);
assert.equal(
  context.parseCommentWrappers(String.raw`\comment{left}{one}\comment{right}{two}`).length,
  2,
  "adjacent comments must remain valid",
);

for (const [markup, code] of [
  [String.raw`\comment{x}{open`, "unclosed_comment"],
  [String.raw`\comment{x}{one} \comment{x}{two}`, "duplicate_comment"],
  [String.raw`\comment{x}{one \comment{y}{two}}`, "intersecting_comments"],
  [String.raw`\comment{not valid}{body}`, "invalid_comment_id"],
]) {
  assert.throws(
    () => context.parseCommentWrappers(markup),
    error => error?.code === code && error.line >= 1 && error.column >= 1,
    `${code} was not reported consistently by the browser parser`,
  );
}

// Markup that cannot parse *inside* code is documentation, not a broken wrapper. The app's own
// editing guide shows `\comment{<comment-id>}{...}` this way, and a report page explaining the
// syntax must stay saveable. Mirrors parse_comment_wrappers in bubbles.py.
for (const documented of [
  "Use `" + String.raw`\comment{<comment-id>}{...}` + "` to wrap a review.",
  "```\n" + String.raw`\comment{<comment-id>}{body}` + "\n```",
  "~~~md\n" + String.raw`\comment{not valid}{body}` + "\n~~~",
  "``a `" + String.raw`\comment{<id>}{x}` + "` b``",
]) {
  assert.equal(context.parseCommentWrappers(documented).length, 0,
    `documented syntax must not parse as a wrapper:\n${documented}`);
}

// ...but a well-formed wrapper is still a wrapper wherever it sits, so commenting on a code
// block keeps working and no stored page changes meaning.
assert.deepEqual(
  Array.from(context.parseCommentWrappers("```\n" + String.raw`\comment{ok}{code}` + "\n```"),
             wrapper => wrapper.id),
  ["ok"],
  "a valid wrapper inside a fence must still anchor",
);

// An unclosed fence swallows the rest of the document, and a typo in prose still reports itself.
assert.equal(context.parseCommentWrappers("```\n" + String.raw`\comment{<id>}{x}`).length, 0,
  "an unclosed fence swallows the rest of the document");
assert.throws(() => context.parseCommentWrappers(String.raw`\comment{not valid}{body}`),
  error => error.code === "invalid_comment_id");

console.log("Browser review parser parity fixtures passed.");
