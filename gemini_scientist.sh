#!/usr/bin/env bash
# gemini_scientist.sh — launch Gemini CLI as your lockedin research-report assistant.
#
# Similar to claude_scientist.sh, this wrapper injects the report-assistant role
# via the -i (prompt-interactive) flag and authenticates against your .env first.
#
#   ./gemini_scientist.sh                 # interactive, auto-greets with your bubbles

set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "✗ No .env found. Copy .env.example to .env and set DEV_USERNAME / DEV_PASSWORD." >&2
  exit 1
fi

# Authenticate + locate the workspace before launching (fails fast on a bad password).
if ! uv run lockedin devmode; then
  echo "✗ devmode authentication failed — fix .env and retry." >&2
  exit 1
fi

# Load DEV_USERNAME for the role brief.
set -a; source .env; set +a

ROLE="You are the lockedin research-report assistant for the account \"${DEV_USERNAME}\". \
Your primary mandate is to manage this user's research reports on disk. \
Read DEV_MODE.md in this repo and follow it exactly. \
You are NOT here to develop the lockedin codebase — you are a research assistant. \
Work only inside data/users/${DEV_USERNAME}/ (REPORTS/ and ASSETS/); never touch other users. \
Markdown is the deliverable: edit REPORTS/<slug>/pages/*.md directly. Use \$...\$ / \$\$...\$\$ for \
math only, [[page-slug]] for internal links, and clean reader-facing prose (no XML tags, no \
changelog lines). Ground answers in the user's PDFs and summaries (ASSETS/<id>/*) and pages; never \
invent citations. Run 'uv run lockedin devmode' anytime to list the user's bubbles and pages."

if [[ $# -gt 0 ]]; then
  exec gemini -i "$ROLE" "$@"
else
  exec gemini -i "$ROLE" \
    "Briefly introduce yourself as my research-report assistant, run 'uv run lockedin devmode' to show my bubbles, then ask what I'd like to work on."
fi
