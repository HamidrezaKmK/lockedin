#!/usr/bin/env bash
# agy_scientist.sh — launch the Antigravity CLI (`agy`) as your lockedin research-report assistant.
#
# Mirrors claude_scientist.sh / codex_scientist.sh, but `agy` has no --append-system-prompt,
# --tools allowlist, or --policy flag. Instead everything is PROJECT-LOCAL via the committed
# .agents/ folder, which agy auto-loads from the project root — your global agy config
# (~/.gemini/antigravity-cli/settings.json) is never read or modified by this launcher:
#
#   * .agents/hooks.json + .agents/agy_permission_hook.sh — a PreToolUse permission gate that
#       DENIES the shell (run_command) and forces an APPROVAL PROMPT WITH A DIFF on every write
#       tool (write_to_file / replace_file_content / multi_replace_file_content). This is what
#       fixes "agy edits without asking": the global settings can only ask for a tool literally
#       named "write_file", which agy doesn't have — so writes slip through. The hook gates the
#       REAL tool names instead, so no report page is ever written without you reviewing the diff.
#   * the report-assistant ROLE + your authenticated workspace listing + the live Editing Guide
#       are injected as the opening interactive prompt (agy -i), so the agent never runs devmode.
#
# Pick the model with AGY_MODEL (run `agy models` for the exact names), e.g.
#   AGY_MODEL="Gemini 3.1 Pro (High)"     ./agy_scientist.sh
#   AGY_MODEL="Gemini 3.5 Flash (Medium)" ./agy_scientist.sh
#   AGY_MODEL="Claude Opus 4.6 (Thinking)" ./agy_scientist.sh
# Unset → agy uses whatever model your global config already selects.
#
# Everything lives in the repo (this script + .agents/), so a fresh clone with agy installed gets
# the same behavior — nothing is stored machine-locally (only your .env credentials are per-machine).
#
#   ./agy_scientist.sh                 # interactive, auto-greets with your bubbles
#   ./agy_scientist.sh "do X"          # one-shot kickoff request, then continues interactively
#   ./agy_scientist.sh resume          # resume the most recent conversation
#   ./agy_scientist.sh resume <id>     # resume a conversation by its ID
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "✗ No .env found. Copy .env.example to .env and set DEV_USERNAME / DEV_PASSWORD." >&2
  exit 1
fi
if ! command -v agy >/dev/null 2>&1; then
  echo "✗ Antigravity CLI (agy) not found on PATH." >&2
  exit 1
fi

# Authenticate + locate the workspace before launching (fails fast on a bad password) and
# CAPTURE the listing so we can hand it to the agent — it never has to run the command itself.
if ! WORKSPACE_INFO="$(uv run lockedin devmode 2>&1)"; then
  echo "$WORKSPACE_INFO" >&2
  echo "✗ devmode authentication failed — fix .env and retry." >&2
  exit 1
fi
echo "$WORKSPACE_INFO"

# Live report Editing Guide (the same one shown in the web app's "?" panel). Best-effort: if it
# fails, continue with an empty guide rather than blocking the session.
EDITING_GUIDE="$(uv run lockedin editguide 2>/dev/null || true)"

# Load DEV_USERNAME for the role brief.
set -a; source .env; set +a

# Ensure the project-local permission hook is wired with an ABSOLUTE path (agy's hooks.json wants
# an absolute command). Regenerated idempotently so a fresh clone at a different path self-heals,
# and an already-correct file is left untouched (no git churn).
HOOK="$PWD/.agents/agy_permission_hook.sh"
HOOKS_JSON="$PWD/.agents/hooks.json"
if [[ ! -x "$HOOK" ]]; then
  echo "✗ Missing permission hook: $HOOK" >&2
  exit 1
fi
DESIRED_HOOKS=$(cat <<JSON
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "$HOOK"
          }
        ]
      }
    ]
  }
}
JSON
)
if [[ ! -f "$HOOKS_JSON" ]] || [[ "$(cat "$HOOKS_JSON")" != "$DESIRED_HOOKS" ]]; then
  printf '%s\n' "$DESIRED_HOOKS" > "$HOOKS_JSON"
fi

ROLE="You are the lockedin research-report assistant for the account \"${DEV_USERNAME}\". \
You are an EDITING TOOL for this user's Markdown research reports — NOT a developer of the \
lockedin codebase, and NOT a shell user. \
\
HARD RULES: \
(0) Before proposing or making ANY edit, consult the EDITING GUIDE included at the END of this \
prompt and follow its conventions for math, theorem environments, equation numbering \
(\\eqref/\\ref/\\thmref), wikilinks, and @<id> TODO references. Where the guide and the short \
summary in rule (6) differ, the EDITING GUIDE is authoritative. \
(1) You have NO shell: the run_command tool is DENIED by a permission hook — never try to run \
shell commands, scripts, 'uv run lockedin devmode', package managers, tests, or git. \
Authentication is already done and the current workspace listing is included below. To inspect \
structure, use your file tools (view_file, list_dir, grep_search) on pages.yaml and the REPORTS/ \
tree. \
(2) Work ONLY inside data/users/${DEV_USERNAME}/ (REPORTS/ and ASSETS/); never touch other \
users or accounts.yaml. The user's BUBBLES are ONLY the slugs listed in CURRENT WORKSPACE below \
(each is a folder under REPORTS/) — never present repository source files (e.g. *.py) or \
CLAUDE.md architecture modules as 'bubbles'. \
(3) Every edit is gated for your safety: when you call a write tool (write_to_file, \
replace_file_content, multi_replace_file_content) the user gets an approval prompt showing a \
diff and approves or rejects it. ALWAYS describe in prose the change you intend to make first, \
then make the edit so the user can review the diff. \
(4) The Markdown IS the deliverable — edit REPORTS/<slug>/pages/<page-slug>.md directly with \
your file tools. Files may be updated elsewhere, so re-read the relevant page right before \
editing it. \
(5) Add a page: create pages/<new-slug>.md (start with '# Title') AND append \
'{page_slug: <new-slug>, title: <Title>}' to that bubble's pages.yaml. Add a bubble: create \
REPORTS/<slug>/pages.yaml (home: overview + one overview page) + pages/overview.md, then add \
the slug to bubbles.yaml with approved: true. \
(6) Formatting — Math delimiters: \$...\$ for inline math; \$\$...\$\$ for a single display \
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
Links: [[page-slug]] or [[Exact Page Title]] for intra-bubble page links; use \
[[page-slug|Custom Label]] when the rendered link text should differ from the page name. \
General: clean reader-facing prose — no XML tags, no changelog lines. Ground answers in the \
user's PDFs/summaries (ASSETS/<id>/*) and pages; never invent citations. \
(7) Mathematics: be strictly rigorous — define every symbol and piece of notation before use, \
and never leave a term ambiguous. Keep new mathematical content minimal and closely grounded in \
what is already present in the user's report pages (REPORTS/<slug>/pages/*.md) and referenced \
papers/summaries (ASSETS/<id>/*); prefer restating or citing what those sources say over \
introducing new derivations. Only reason beyond the available reports and references when it is \
absolutely necessary, and flag such steps explicitly as out-of-reference reasoning. \
\
CURRENT WORKSPACE (already authenticated — do NOT run anything to obtain this): \
${WORKSPACE_INFO}

EDITING GUIDE (authoritative formatting reference — read this BEFORE proposing or making any edit):
${EDITING_GUIDE}"

GREETING="Now, briefly introduce yourself as my research-report assistant, list my bubbles by their human-readable names (not slugs or IDs) from the CURRENT WORKSPACE section above, then ask what I'd like to work on. Do not run any commands and do not describe source code."

AGY_FLAGS=()
if [[ -n "${AGY_MODEL:-}" ]]; then
  AGY_FLAGS+=(--model "$AGY_MODEL")
fi

if [[ "${1:-}" == "resume" ]]; then
  shift
  if [[ $# -eq 0 || "${1:-}" == "--last" || "${1:-}" == "latest" ]]; then
    exec agy "${AGY_FLAGS[@]}" --continue
  else
    exec agy "${AGY_FLAGS[@]}" --conversation "$1"
  fi
elif [[ $# -gt 0 ]]; then
  exec agy "${AGY_FLAGS[@]}" -i "${ROLE}

MY REQUEST: $*"
else
  exec agy "${AGY_FLAGS[@]}" -i "${ROLE}

${GREETING}"
fi
