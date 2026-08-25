/**
 * The preview's source markers must never split a block they are meant to annotate.
 *
 * `withSourceMarkers` injects `<!--li-src:N-->` lines so a click in the rendered pane can find
 * its offset in the Markdown. A marker is an HTML comment, and an HTML block does NOT lazily
 * continue a blockquote: a marker emitted between two `>` lines closes the quote, so the next
 * `>` opens a fresh one and one ten-line quote renders as ten separate quote boxes. This pins
 * that only a quote's first line is marked, while headings and list items still are.
 *
 * Run: node tests/source-markers.mjs
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const html = fs.readFileSync(path.join(repo, "src/lockedin/web/index.html"), "utf8");
const start = html.indexOf("function withSourceMarkers");
const end = html.indexOf("function bindPreviewSourceOffsets", start);
assert.ok(start >= 0 && end > start, "could not locate withSourceMarkers");

const context = {};
vm.createContext(context);
vm.runInContext(`${html.slice(start, end)}\nglobalThis.withSourceMarkers = withSourceMarkers;`, context);
const mark = md => context.withSourceMarkers(md);
const markers = md => (mark(md).match(/<!--li-src:\d+-->/g) || []).length;

const quote = [
  "> **A quote that spans several lines.**",
  ">",
  "> Its second paragraph wraps across",
  "> three separate source lines and must",
  "> still render as one block.",
].join("\n");

// One marker for the whole quote, and none between its lines.
assert.equal(markers(quote), 1, `a blockquote must receive exactly one marker:\n${mark(quote)}`);
for (const line of mark(quote).split("\n")) {
  assert.ok(!/^<!--li-src:\d+-->$/.test(line) || mark(quote).indexOf(line) === 0,
    "no marker may appear after the first line of a quote");
}
assert.ok(!/>[^\n]*\n<!--li-src/.test(mark(quote)),
  `a marker must never follow a quoted line:\n${mark(quote)}`);

// A quote after a heading (no blank line between) still gets its opening marker.
const tight = "## Heading\n> quoted right after the heading\n> and continued\n";
assert.equal(markers(tight), 2, `heading + quote start must both be marked:\n${mark(tight)}`);

// Two quotes separated by a blank line are two blocks, so two markers.
const twice = "> first quote\n\n> second quote\n";
assert.equal(markers(twice), 2, `separate quotes each get a marker:\n${mark(twice)}`);

// A lazy continuation (unquoted line inside a quote) must not restart the quote.
const lazy = "> quoted line\nlazy continuation of the same quote\n> back to quoted\n";
assert.equal(markers(lazy), 1, `a lazily continued quote stays one block:\n${mark(lazy)}`);

// Headings, list items, and paragraphs keep the granularity they had.
assert.equal(markers("# Title\n\nA paragraph.\n"), 2, "heading and paragraph are marked");
assert.equal(markers("- one\n- two\n- three\n"), 3, "every list item keeps its own marker");
assert.equal(markers("1. one\n2. two\n"), 2, "ordered items keep their markers");

// Fenced code is never marked inside, so a `>` in a code block cannot confuse the state.
const fenced = "```\n> not a quote\n> still code\n```\n\nAfter.\n";
assert.equal(markers(fenced), 2, `code fences stay unmarked inside:\n${mark(fenced)}`);

// The markers must record real offsets into the original source.
const offsets = [...mark("# A\n\n> quote\n").matchAll(/<!--li-src:(\d+)-->/g)].map(m => +m[1]);
assert.deepEqual(offsets, [0, 5], "offsets must point at the start of each block");

process.stdout.write("source-markers: all marker-placement checks passed\n");
