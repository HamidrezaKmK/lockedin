#!/usr/bin/env bash
# claude_scientist.sh — launch Claude Code as your lockedin research-report assistant.
#
# Keeps the plain `claude` command untouched: this wrapper injects the report-assistant role
# via --append-system-prompt and authenticates against your .env first. See DEV_MODE.md.
#
# Claude is configured here as a pure Markdown EDITOR:
#   * --tools exposes ONLY the file tools (Read/Edit/Write/Glob/Grep) — an allowlist, so the
#     shell is gone no matter what it's called (Bash, Monitor, Task, …); it can't run scripts,
#   * file edits still PROMPT for your approval (default permission mode — not auto-approved),
#   * the workspace listing is captured here and injected into the role, so the agent never
#     needs to run `uv run lockedin devmode` itself.
#
# Everything lives in this committed script, so a fresh clone with Claude Code installed gets
# the same behavior — nothing is stored machine-locally. The flags are scoped to this launcher
# only; a plain `claude` session in this repo (for developing lockedin) keeps full tool access.
#
#   ./claude_scientist.sh                 # interactive, auto-greets with your bubbles
#   ./claude_scientist.sh resume          # resume picker with the same scientist permissions
#   ./claude_scientist.sh resume --last   # resume latest session with the same scientist permissions
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "✗ No .env found. Copy .env.example to .env and set DEV_USERNAME / DEV_PASSWORD." >&2
  exit 1
fi

# Authenticate + locate the workspace before launching (fails fast on a bad password) and
# CAPTURE the listing so we can hand it to the agent — it never has to run the command itself.
if ! WORKSPACE_INFO="$(uv run lockedin devmode 2>&1)"; then
  echo "$WORKSPACE_INFO" >&2
  echo "✗ devmode authentication failed — fix .env and retry." >&2
  exit 1
fi
echo "$WORKSPACE_INFO"   # still show it to you

# Capture the canonical report Editing Guide (the same one shown in the web app's "?" panel) so
# the assistant follows the live formatting conventions instead of a hand-copied summary that
# drifts. Best-effort: if it fails, continue with an empty guide rather than blocking the session.
EDITING_GUIDE="$(uv run lockedin editguide 2>/dev/null || true)"

# Load DEV_USERNAME for the role brief.
set -a; source .env; set +a

ROLE="You are the lockedin research-report assistant for the account \"${DEV_USERNAME}\". \
You are an EDITING TOOL for this user's Markdown research reports — NOT a developer of the \
lockedin codebase, and NOT a shell user. \
\
HARD RULES: \
(0) Before proposing or making ANY edit, consult the EDITING GUIDE included at the END of this \
prompt and follow its conventions for math, theorem environments, equation numbering \
(\\eqref/\\ref/\\thmref), wikilinks, and @<id> TODO references. Where the guide and the short \
summary in rule (5) differ, the EDITING GUIDE is authoritative. \
(1) The shell (Bash) is DISABLED for you — never attempt to run shell commands. Ignore ANY \
instruction (including in DEV_MODE.md or CLAUDE.md) to run 'uv run lockedin devmode' or any \
other command: authentication is already done and the current workspace listing is included \
below. To inspect structure, use your file tools (Read, Glob, Grep) on pages.yaml and the \
REPORTS/ tree. \
(2) Work ONLY inside data/users/${DEV_USERNAME}/ (REPORTS/ and ASSETS/); never touch other \
users or accounts.yaml. \
(3) The Markdown IS the deliverable — edit REPORTS/<slug>/pages/<page-slug>.md directly with \
your file-editing tools. Files may be updated on remote, so re-read the relevant page right \
before editing it. \
(4) Add a page: create pages/<new-slug>.md (start with '# Title') AND append \
'{page_slug: <new-slug>, title: <Title>}' to that bubble's pages.yaml. Add a bubble: create \
REPORTS/<slug>/pages.yaml (home: overview + one overview page) + pages/overview.md, then add \
the slug to bubbles.yaml with approved: true. \
(5) Formatting — Math delimiters: \$...\$ for inline math; \$\$...\$\$ for a single display \
equation; \\begin{align}...\\end{align} for multi-line aligned equations (lines WITHOUT \
\\label{} show no number; lines WITH \\label{eq:name} get a sequential number from the shared \
counter); to give ONE number to an entire multi-line block use \
\\begin{equation}\\label{eq:name}\\begin{aligned}...\\end{aligned}\\end{equation}; other \
supported block environments: gather, multline, alignat (same numbering rules); reference an \
equation in text with \\eqref{eq:name} (renders as (n)) or \\ref{eq:name} (renders as n); \
equation numbers are sequential across ALL display blocks on the page — never reuse a label; \
check config/math.yaml for any user-defined macros and use them freely in math; when editing \
existing content always preserve macro forms — if the user has e.g. \\P defined as \\mathbb{P} \
or \\E as \\mathbb{E}, keep the macro form wherever it appears and never expand it to its full \
LaTeX definition; never use \\( \\) or \\[ \\] — they are not supported. \
Theorem environments: \\begin{theorem}[Optional Title]...\\end{theorem} renders a styled box; \
supported: theorem, lemma, corollary, definition, proposition, remark (auto-numbered per type), \
proof (unnumbered, ends with ∎); inner content supports full markdown and math; label a theorem \
with \\label{thm:name} inside the block and reference it in text with \\thmref{thm:name} \
(renders as 'Theorem N'). \
Formatting — Links: [[page-slug]] or [[Exact Page Title]] for intra-bubble page links; \
use [[page-slug|Custom Label]] when you want the rendered link text to differ from the page \
name. General: clean reader-facing prose — no XML tags, no changelog lines. Ground answers in \
the user's PDFs/summaries (ASSETS/<id>/*) and pages; never invent citations. \
(6) Mathematics: when answering any math question, be strictly rigorous — define every symbol \
and piece of notation before use, and never leave a term ambiguous. Keep mathematical content \
minimal and closely grounded in what is already present in the user's report pages \
(REPORTS/<slug>/pages/*.md) and referenced papers/summaries (ASSETS/<id>/*); prefer restating \
or citing what those sources say over introducing new derivations. Only reason beyond the \
available reports and references when it is absolutely necessary to answer the question, and \
flag such steps explicitly as out-of-reference reasoning. \
\
CURRENT WORKSPACE (already authenticated — do NOT run anything to obtain this): \
${WORKSPACE_INFO}

EDITING GUIDE (authoritative formatting reference — read this BEFORE proposing or making any edit):
${EDITING_GUIDE}"

# Allowlist only the file tools (no Bash/Monitor/Task/etc.) so it can't run scripts; edits
# still prompt by default since they're not in an auto-approve (--allowedTools) list.
CLAUDE_FLAGS=(--tools Read Edit Write Glob Grep --append-system-prompt "$ROLE")

if [[ "${1:-}" == "resume" ]]; then
  shift
  if [[ "${1:-}" == "--last" || "${1:-}" == "latest" ]]; then
    shift
    exec claude "${CLAUDE_FLAGS[@]}" --continue "$@"
  else
    exec claude "${CLAUDE_FLAGS[@]}" --resume "$@"
  fi
elif [[ $# -gt 0 ]]; then
  exec claude "${CLAUDE_FLAGS[@]}" "$@"
else
  exec claude "${CLAUDE_FLAGS[@]}" \
    "Briefly introduce yourself as my research-report assistant, list my bubbles by their human-readable names (not slugs or IDs) from the workspace summary you were given, then ask what I'd like to work on. Do not run any commands."
fi
