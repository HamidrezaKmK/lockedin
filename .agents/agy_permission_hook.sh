#!/usr/bin/env bash
# agy_permission_hook.sh — PreToolUse permission gate for the lockedin report assistant.
#
# Wired in by .agents/hooks.json (PreToolUse, matcher "*"), so it runs for EVERY tool the
# Antigravity CLI (`agy`) is about to call when launched via ../agy_scientist.sh. agy passes the
# pending tool call as JSON on stdin: {"toolCall":{"name":"<tool>","args":{...}}, ...}. We print
# a decision on stdout: {"decision":"allow|ask|deny","reason":"..."}.
#
# This is the PROJECT-LOCAL permission layer (the global ~/.gemini/antigravity-cli/settings.json
# is never touched). agy's real write tools are write_to_file / replace_file_content /
# multi_replace_file_content (there is NO "write_file"), so a global ask-rule on "write_file"
# never fires and edits go through silently. We gate the real tool names instead:
#
#   * run_command (the shell)                              -> deny
#   * write_to_file / replace_file_content /               -> ask, and we render the proposed
#     multi_replace_file_content                                BEFORE->AFTER diff into the prompt
#   * everything else (view_file, list_dir, grep_search,   -> allow
#     search_web, read_url_content, ...)
#
# Why the diff is embedded in the prompt: the agy CLI shows only a plain "approve this tool call?"
# confirmation for hook-gated edits — it has no separate colored diff/accept-reject review for
# these edit tools. So we compute a unified diff from the tool's own args (the old snippet vs the
# proposed replacement) and put it in the `reason`, which agy displays in the approval prompt.
#
# NOTE: deliberately not `set -e`/`pipefail` — a non-matching grep/parse must never abort before
# the case prints a decision (empty output would be read as allow).
set -u

payload="$(cat)"

# --- tool name (jq -> python3 -> grep) ---
name=""
if command -v jq >/dev/null 2>&1; then
  name="$(printf '%s' "$payload" | jq -r '.toolCall.name // empty' 2>/dev/null || true)"
fi
if [[ -z "$name" ]] && command -v python3 >/dev/null 2>&1; then
  name="$(printf '%s' "$payload" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("toolCall",{}).get("name","") or "")
except Exception: print("")' 2>/dev/null || true)"
fi
if [[ -z "$name" ]]; then
  name="$(printf '%s' "$payload" | grep -oE '"toolCall":\{.*' | grep -oE '"name":"[a-zA-Z_]+"' | head -1 | sed 's/.*:"//;s/"$//' || true)"
fi

# Optional audit log (set AGY_HOOK_LOG=/path to enable).
if [[ -n "${AGY_HOOK_LOG:-}" ]]; then
  printf 'tool=%s\n' "$name" >> "$AGY_HOOK_LOG" 2>/dev/null || true
fi

emit_ask_with_diff() {
  # Build {"decision":"ask","reason":"<diff>"} with a unified diff computed from the tool args.
  # Falls back to a plain ask if python3 is unavailable or args can't be parsed.
  if command -v python3 >/dev/null 2>&1; then
    AGY_PAYLOAD="$payload" python3 <<'PY' && return 0
import os, sys, json, difflib
try:
    p = json.loads(os.environ["AGY_PAYLOAD"])
    tc = p.get("toolCall", {})
    name = tc.get("name", "")
    a = tc.get("args", {}) or {}
    def ci(d, *keys):
        low = {k.lower(): v for k, v in d.items()}
        for k in keys:
            if k.lower() in low: return low[k.lower()]
        return None
    target = ci(a, "TargetFile", "AbsolutePath", "Path", "FilePath") or "(file)"
    pieces = []
    def diff(old, new, label):
        old = old if isinstance(old, str) else ""
        new = new if isinstance(new, str) else ""
        d = difflib.unified_diff(old.splitlines(), new.splitlines(),
                                 fromfile=label+" (current)", tofile=label+" (proposed)", lineterm="")
        return "\n".join(d)
    if name == "replace_file_content":
        pieces.append(diff(ci(a, "TargetContent") or "", ci(a, "ReplacementContent") or "", os.path.basename(str(target))))
    elif name == "multi_replace_file_content":
        chunks = ci(a, "ReplacementChunks", "Chunks", "Replacements") or []
        if isinstance(chunks, list) and chunks:
            for i, c in enumerate(chunks):
                if isinstance(c, dict):
                    pieces.append("chunk %d:\n%s" % (i + 1, diff(ci(c, "TargetContent") or "", ci(c, "ReplacementContent") or "", os.path.basename(str(target)))))
        else:
            pieces.append(diff(ci(a, "TargetContent") or "", ci(a, "ReplacementContent") or "", os.path.basename(str(target))))
    elif name == "write_to_file":
        content = ci(a, "CodeContent", "Content", "FileContent", "Contents", "Text") or ""
        try:
            existed = os.path.isfile(target)
            old = open(target, encoding="utf-8").read() if existed else ""
        except Exception:
            old = ""
        pieces.append(("(new file)\n" if not old else "") + diff(old, content if isinstance(content, str) else "", os.path.basename(str(target))))
    body = "\n".join(x for x in pieces if x).strip()
    if not body:
        body = "(no diff preview available)"
    # bound size so the prompt stays readable
    if len(body) > 6000:
        body = body[:6000] + "\n... (diff truncated)"
    reason = "Proposed edit to %s — review and approve:\n%s" % (target, body)
    print(json.dumps({"decision": "ask", "reason": reason}))
except Exception:
    sys.exit(1)
PY
  fi
  # Fallback: plain ask without a diff.
  echo '{"decision":"ask","reason":"Review this edit to your report and approve it."}'
}

case "$name" in
  run_command)
    echo '{"decision":"deny","reason":"The lockedin report assistant has no shell. Use your file tools (view_file, list_dir, grep_search, write_to_file, replace_file_content) instead."}'
    ;;
  write_to_file|replace_file_content|multi_replace_file_content)
    emit_ask_with_diff
    ;;
  *)
    echo '{"decision":"allow"}'
    ;;
esac
