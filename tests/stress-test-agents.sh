#!/usr/bin/env bash
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

runtime_tmp="$repo_root/tests/.tmp/runtime"
mkdir -p "$runtime_tmp"
export LOCKEDIN_QUIET_TEST_HTTP=1

restore_terminal_mouse() {
  if [[ -t 1 ]]; then
    printf '\033[?1000l\033[?1002l\033[?1003l\033[?1006l'
  fi
}
trap restore_terminal_mouse EXIT INT TERM

paid=false
for argument in "$@"; do
  if [[ "$argument" == "--paid" ]]; then
    paid=true
  fi
done

if [[ "$paid" != true ]]; then
  echo "Refusing to run subscription-backed checks without --paid." >&2
  echo "Usage: tests/stress-test-agents.sh --paid [codex|claude|agy|opencode] [live_review_gate options]" >&2
  exit 2
fi

report_dir="$repo_root/tests/.tmp/reports"
mkdir -p "$report_dir"
report_path="${LOCKEDIN_STRESS_REPORT:-$report_dir/agent-stress-$(date -u +%Y%m%dT%H%M%SZ).log}"

run_suite() {
  echo "LockedIn agent stress report"
  echo "started_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "repository: $repo_root"
  printf "arguments:"
  printf " %q" "$@"
  echo
  echo "commit: $(git rev-parse --verify HEAD 2>/dev/null || echo unavailable)"
  echo "python: $(python --version 2>&1)"
  echo "node: $(node --version 2>&1)"
  echo "codex: $(codex --version 2>&1)"
  echo "claude: $(claude --version 2>&1)"
  echo "agy: $(agy --version 2>&1 | head -1)"
  echo "opencode: $(${OPENCODE_BIN:-$HOME/.opencode/bin/opencode} --version 2>&1)"
  echo
  echo "[1/5] Static and provider-adapter regression suite"
  env -u TMPDIR uv run python -m unittest \
    tests.test_agent_vendors \
    tests.test_agents_client \
    tests.test_agents \
    tests.test_setup_link \
    tests.test_scientist \
    tests.test_skill_freshness \
    tests.test_live_review_gate -v
  echo
  echo "[2/5] Persistent worker and managed-turn self-protection"
  env -u TMPDIR uv run python -m unittest tests.test_agent_worker_persistence -v
  echo
  echo "[3/5] Browser-free agent lifecycle simulation"
  node tests/agents-e2e.mjs
  echo
  echo "[4/5] Setup and linked-worktree simulation"
  node tests/scientist-setup-e2e.mjs
  echo
  echo "[5/5] Paid cheap-model editing, clarity, cost, and worktree gate"
  TMPDIR="$runtime_tmp" uv run python tests/live_review_gate.py "$@"
  echo
  echo "completed_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "PASS: every automated and paid stress gate completed"
}

set +e
( set -e; run_suite "$@" ) 2>&1 | tee "$report_path"
suite_status=${PIPESTATUS[0]}
set -e

echo "Report: $report_path"
exit "$suite_status"
