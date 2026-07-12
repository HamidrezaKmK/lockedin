#!/usr/bin/env bash
# scientist.sh - unified lockedin research-report assistant launcher.
#
# Run it, authenticate through DEV_USERNAME/DEV_PASSWORD in .env, pick:
#
#   codex <bubble-slug>
#   claude <bubble-slug>
#   agy <bubble-slug>
#
# The selected session receives a backend-generated context for that bubble only.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "x No .env found. Copy .env.example to .env and set DEV_USERNAME / DEV_PASSWORD." >&2
  exit 1
fi

echo "lockedin scientist launcher"
echo "Authenticating with DEV_USERNAME / DEV_PASSWORD from .env..."
if ! BUBBLE_LIST="$(uv run lockedin scientist-list 2>&1)"; then
  echo "$BUBBLE_LIST" >&2
  exit 1
fi
echo "$BUBBLE_LIST"
echo

if [[ $# -ge 2 ]]; then
  MODEL="$1"
  BUBBLE="$2"
else
  read -r -p "Enter <model> <bubble-slug> where model is codex, claude, or agy: " MODEL BUBBLE
fi

MODEL="${MODEL:-}"
BUBBLE="${BUBBLE:-}"
case "$MODEL" in
  codex|claude|agy) ;;
  *)
    echo "x Unknown model '$MODEL'. Use codex, claude, or agy." >&2
    exit 1
    ;;
esac
if [[ -z "$BUBBLE" ]]; then
  echo "x Missing bubble slug/name." >&2
  exit 1
fi

echo "Generating bubble-scoped context for '$BUBBLE'..."
if ! BUBBLE_CONTEXT="$(uv run lockedin scientist-context "$BUBBLE" 2>&1)"; then
  echo "$BUBBLE_CONTEXT" >&2
  exit 1
fi
EDITING_GUIDE="$(uv run lockedin editguide 2>/dev/null || true)"

set -a
source .env
set +a
DEV_USERNAME="${DEV_USERNAME:-}"

ROLE="$(cat <<ROLE_EOF
You are the lockedin research-report assistant for the account "${DEV_USERNAME}".
You are an EDITING TOOL for this user's Markdown research reports - NOT a developer of the
lockedin codebase.

HARD RULES:
(0) Before proposing or making ANY edit, consult the EDITING GUIDE included before the generated
bubble context in this prompt and follow its conventions for math, theorem environments,
equation numbering, wikilinks, TODO references, and citations.
(1) Work only on the ACTIVE BUBBLE in the GENERATED BUBBLE CONTEXT below. This context was built
by lockedin's backend using the same bubble membership filter as the web chat.
(2) Do not inspect unrelated local assets. Direct reads of ASSETS are allowed only for paper ids
and paths explicitly listed in the ACTIVE BUBBLE CONTEXT. Prefer the generated summaries first;
read full text/PDF only when the user asks for deeper inspection or the summary is insufficient.
(3) Work only inside data/users/${DEV_USERNAME}/. Never touch other users, account registries, or
repository source code.
(4) The Markdown is the deliverable. Edit REPORTS/<slug>/pages/<page-slug>.md directly when the
user approves edits. Re-read a page immediately before editing because it may have changed.
(5) Read only the active bubble citation file listed in the context when citations are relevant.
Never invent BibTeX keys. Prefer higher-relevance papers for reading, retrieval, comparison, and
citation unless the user explicitly asks otherwise.
(6) Every edit must be preceded by a concise prose description of the intended change. If your
tooling does not show an approval prompt automatically, explicitly ask the user before writing.
(7) Web research is allowed and important when the user asks for new papers, recent results,
external sources, or broader context. Prioritize the active bubble's existing assets first, then
use web search/deep research to find and evaluate new resources. Clearly distinguish claims from
local assets versus web sources, include source links for web findings, and ask before importing
or editing any new source into the user's reports.
(8) For math, be rigorous: define every symbol before use, keep new mathematical content grounded
in the active bubble's reports, listed papers, and cited web sources, and flag out-of-reference
reasoning.
(9) Terminal conversation must be readable without a math renderer. In replies to the user, do
not emit LaTeX commands, math delimiters, or raw equation source. Use plain language and Unicode
math notation where helpful (for example, x², ∇f, α ∈ R). Use the Markdown/LaTeX conventions from
the Editing Guide only when writing to a bubble report file or showing the exact text of an
approved report edit.

STARTUP TASK:
Briefly introduce yourself as my lockedin research-report assistant for the active bubble, mention
that papers are prioritized by relevance score, and ask what I would like to work on. Do not
summarize every attached paper at startup.

EDITING GUIDE:
${EDITING_GUIDE}

GENERATED BUBBLE CONTEXT:
Paper summaries in this generated context may be clipped to keep startup reliable. Use the
listed allowed paths for full text/PDF deep-reading when the user asks for detail.

${BUBBLE_CONTEXT}
ROLE_EOF
)"

GREETING="Briefly introduce yourself as my lockedin research-report assistant for the active bubble, mention that papers are prioritized by relevance score, then ask what I would like to work on."

case "$MODEL" in
  claude)
    if ! command -v claude >/dev/null 2>&1; then
      echo "x Claude Code CLI not found on PATH." >&2
      exit 1
    fi
    exec claude --tools Read Edit Write Glob Grep WebSearch WebFetch --append-system-prompt "$ROLE" "$GREETING"
    ;;
  codex)
    if ! command -v codex >/dev/null 2>&1; then
      echo "x Codex CLI not found on PATH." >&2
      exit 1
    fi
    exec codex \
      --cd "$PWD" \
      --sandbox read-only \
      --ask-for-approval on-request \
      --search \
      --disable multi_agent \
      -c "developer_instructions=$ROLE" \
      "$GREETING"
    ;;
  agy)
    if ! command -v agy >/dev/null 2>&1; then
      echo "x Antigravity CLI (agy) not found on PATH." >&2
      exit 1
    fi
    HOOK="$PWD/.agents/agy_permission_hook.sh"
    HOOKS_JSON="$PWD/.agents/hooks.json"
    if [[ ! -x "$HOOK" ]]; then
      echo "x Missing permission hook: $HOOK" >&2
      exit 1
    fi
    DESIRED_HOOKS="$(cat <<JSON
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
)"
    if [[ ! -f "$HOOKS_JSON" ]] || [[ "$(cat "$HOOKS_JSON")" != "$DESIRED_HOOKS" ]]; then
      printf '%s\n' "$DESIRED_HOOKS" > "$HOOKS_JSON"
    fi
    AGY_FLAGS=()
    if [[ -n "${AGY_MODEL:-}" ]]; then
      AGY_FLAGS+=(--model "$AGY_MODEL")
    fi
    exec agy "${AGY_FLAGS[@]}" -i "${GREETING}

${ROLE}"
    ;;
esac
