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

const wrap = (id, body) => `<comment-begin=${id}>${body}<comment-end=${id}>`;
context.markupIsDocumentation = () => false;   // error paths consult it lazily

const source = wrap("x", String.raw`First $\frac{a}{b}$ and even { a lone unbalanced brace`)
  + "\n" + wrap("long-id_123", "second\nline");
const wrappers = context.parseCommentWrappers(source);
assert.deepEqual(Array.from(wrappers, wrapper => wrapper.id), ["x", "long-id_123"]);
assert.equal(wrappers[0].body, String.raw`First $\frac{a}{b}$ and even { a lone unbalanced brace`);
assert.equal(wrappers[1].body, "second\nline");

// nesting parses cleanly, outer body spans the inner pair
const nested = wrap("outer", "head " + wrap("inner", "middle") + " tail");
const both = context.parseCommentWrappers(nested);
assert.deepEqual(Array.from(both, w => w.id).sort(), ["inner", "outer"]);

// Crossing ranges are also valid paired tags. They cannot be represented by nested HTML marks,
// so the document preview paints them with the same range renderer used by chalk talks.
const crossing = `<comment-begin=first>alpha <comment-begin=second>beta<comment-end=first> gamma<comment-end=second>`;
const crossed = context.parseCommentWrappers(crossing);
assert.deepEqual(Array.from(crossed, w => w.id), ["first", "second"]);
assert.equal(crossed[0].body, "alpha <comment-begin=second>beta");
assert.equal(crossed[1].body, "beta<comment-end=first> gamma");

// adjacency still fine
assert.equal(context.parseCommentWrappers(wrap("left", "one") + wrap("right", "two")).length, 2);

for (const [bad, code] of [
  ["<comment-begin=x>open", "unclosed_comment"],
  [wrap("x", "one") + wrap("x", "two"), "duplicate_comment"],
  ["stray <comment-end=zz> end", "stray_comment_end"],
]) {
  assert.throws(() => context.parseCommentWrappers(bad), err => err.code === code, bad);
}

console.log("review parser parity: ok");
