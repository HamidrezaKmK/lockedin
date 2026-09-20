#!/usr/bin/env python3
"""Opt-in paid smoke test for the generated Scientist editing contract.

This is intentionally excluded from unittest discovery. It invokes real provider
subscriptions only when --paid is present, and confines edits to a disposable
Git repository and linked worktree beneath tests/.tmp.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lockedin import reports  # noqa: E402
from lockedin.scientist_cli import write_skill_bundle  # noqa: E402

BASELINES = {
    "codex": {"input": 103_288, "output": 1_380},
    "agy": {"input": 165_346, "output": 1_799},
}


@dataclass
class ProbeResult:
    provider: str
    case: str
    reply: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    seconds: float


def run(command: list[str], *, cwd: Path, timeout: int = 300, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def git(*args: str, cwd: Path) -> None:
    completed = run(["git", *args], cwd=cwd, timeout=30)
    if completed.returncode:
        raise RuntimeError(completed.stderr or completed.stdout)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def recursive_values(value: Any, key_names: set[str]) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in key_names:
                found.append(child)
            found.extend(recursive_values(child, key_names))
    elif isinstance(value, list):
        for child in value:
            found.extend(recursive_values(child, key_names))
    return found


def last_text(values: list[Any]) -> str:
    for value in reversed(values):
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for key in ("text", "content", "message", "result"):
                child = value.get(key)
                if isinstance(child, str) and child.strip():
                    return child.strip()
    return ""


def usage_from(value: Any) -> tuple[int, int, int]:
    def maximum(keys: set[str]) -> int:
        numbers = [item for item in recursive_values(value, keys) if isinstance(item, (int, float))]
        return int(max(numbers, default=0))

    return (
        maximum({"input_tokens", "inputTokens", "prompt_tokens", "promptTokens"}),
        maximum({"output_tokens", "outputTokens", "completion_tokens", "completionTokens"}),
        maximum({"cached_input_tokens", "cachedInputTokens", "cache_read_input_tokens", "cacheReadInputTokens", "cache_read_tokens", "cacheReadTokens", "cached_tokens", "cachedTokens"}),
    )


def parse_codex(stdout: str) -> tuple[str, int, int, int]:
    events = [json.loads(line) for line in stdout.splitlines() if line.strip().startswith("{")]
    reply = last_text([
        event.get("item", {}) for event in events
        if event.get("type") == "item.completed"
        and event.get("item", {}).get("type") == "agent_message"
    ])
    usage_events = [event for event in events if event.get("type") == "turn.completed"]
    inp, out, cached = usage_from(usage_events[-1] if usage_events else events)
    return reply, inp, out, cached


def parse_agy(stdout: str) -> tuple[str, int, int, int]:
    payload = json.loads(stdout)
    reply = last_text(recursive_values(payload, {"result", "final", "response", "text"}))
    inp, out, cached = usage_from(payload)
    return reply, inp, out, cached


def parse_claude(stdout: str) -> tuple[str, int, int, int]:
    payload = json.loads(stdout)
    reply = last_text(recursive_values(payload, {"result", "final", "response", "text"}))
    inp, out, cached = usage_from(payload)
    return reply, inp, out, cached



def parse_opencode(stdout: str) -> tuple[str, int, int, int]:
    events = [json.loads(line) for line in stdout.splitlines() if line.strip().startswith("{")]
    reply = last_text([
        event.get("part", {}) for event in events if event.get("type") == "text"
    ])
    finishes = [event.get("part", {}) for event in events if event.get("type") == "step_finish"]
    tokens = finishes[-1].get("tokens", {}) if finishes else {}
    cache = tokens.get("cache", {}) if isinstance(tokens.get("cache"), dict) else {}
    return reply, int(tokens.get("input", 0)), int(tokens.get("output", 0)), int(cache.get("read", 0))


def provider_binary(provider: str) -> str | None:
    return shutil.which(provider) or (str(path) if provider == "opencode" and (path := Path.home() / ".opencode/bin/opencode").is_file() else None)
def sentence_count(reply: str) -> int:
    return len([part for part in re.split(r"(?<=[.!?])(?:\s+|$)", reply.strip()) if part.strip()])


def assert_reply(result: ProbeResult, *, clarification: bool = False) -> None:
    if not result.reply:
        raise AssertionError(f"{result.provider}/{result.case}: empty reply")
    if sentence_count(result.reply) > 2:
        raise AssertionError(f"{result.provider}/{result.case}: reply exceeds two sentences: {result.reply!r}")
    if re.search(r"\b(reviewer|subagent|process|steps?)\b", result.reply, re.I):
        raise AssertionError(f"{result.provider}/{result.case}: internal process leaked into reply")
    if clarification and not result.reply.rstrip().endswith("?"):
        raise AssertionError(f"{result.provider}/{result.case}: expected a direct clarification question: {result.reply!r}")


def fixture(base: Path) -> tuple[Path, Path, Path, Path]:
    main = base / "main"
    worktree = base / "worktree"
    nested = worktree / "nested" / "session"
    main.mkdir(parents=True)
    git("init", "-q", cwd=main)
    git("config", "user.email", "live-gate@lockedin.invalid", cwd=main)
    git("config", "user.name", "LockedIn live gate", cwd=main)
    write(main / "README.md", "Disposable LockedIn live gate.\n")
    git("add", "README.md", cwd=main)
    git("commit", "-qm", "fixture", cwd=main)
    git("worktree", "add", "-qb", "live-review-worktree", str(worktree), cwd=main)

    sentinel = main / ".lockedin" / "config" / "binding.json"
    write(sentinel, '{"bubble":"WRONG-MAIN-CHECKOUT"}\n')
    (worktree / ".lockedin").mkdir(parents=True)
    write_skill_bundle(worktree / ".lockedin", reports.guide_section("Editing Guide"), {})
    write(worktree / ".lockedin" / "IDEA.md", "# Test bubble\n\nOnly make the exact requested edit.\n")
    write(worktree / ".lockedin" / "config" / "binding.json", '{"bubble":"DISPOSABLE-WORKTREE"}\n')
    nested.mkdir(parents=True)

    write(worktree / ".lockedin/reports/talks/talk-codex/slides.md", "# Signal\n\nThe estimate is noisey.\n")
    write(worktree / ".lockedin/reports/talks/talk-agy/slides.md", "# Estimator\n\nThe estimate is biassed.\n")
    write(worktree / ".lockedin/reports/talks/talk-claude/slides.md", "# Horizon\n\nContinue untill convergence.\n")
    write(worktree / ".lockedin/reports/talks/talk-opencode/slides.md", "# Gradient\n\nThe estimate is consistant.\n")
    write(worktree / ".lockedin/reports/pages/codex-ambiguous.md", "# Rates\n\nFirst coefficient: 0.10.\n\nSecond coefficient: 0.20.\n")
    write(worktree / ".lockedin/reports/pages/agy-ambiguous.md", "# Thresholds\n\nFirst threshold: 0.30.\n\nSecond threshold: 0.70.\n")
    write(worktree / ".lockedin/reports/pages/claude-ambiguous.md", "# Rates\n\nFirst rate: 0.40.\n\nSecond rate: 0.80.\n")
    write(worktree / ".lockedin/reports/pages/opencode-ambiguous.md", "# Entries\n\nFirst entry: 0.25.\n\nSecond entry: 0.75.\n")

    verifier = worktree / ".test-bin" / "lockedin-scientist"
    write(verifier, """#!/usr/bin/env python3
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
with (root / ".test-sync-calls").open("a", encoding="utf-8") as stream:
    stream.write(" ".join(sys.argv[1:]) + "\\n")
""")
    verifier.chmod(0o755)
    for guide in (worktree / ".lockedin/SKILL.md", worktree / ".lockedin/guides/reports.md"):
        text = guide.read_text(encoding="utf-8")
        guide.write_text(text.replace("lockedin-scientist await-sync", f"{verifier} await-sync"),
                         encoding="utf-8")

    return main, worktree, nested, sentinel


def invoke(provider: str, case: str, prompt: str, cwd: Path, args: argparse.Namespace) -> ProbeResult:
    started = time.monotonic()
    binary = provider_binary(provider) or provider
    env = os.environ.copy()
    env["PATH"] = str(cwd.parents[1] / ".test-bin") + os.pathsep + env.get("PATH", "")

    if provider == "codex":
        command = [
            binary, "exec", "--ephemeral", "-C", str(cwd),
            "--dangerously-bypass-approvals-and-sandbox",
            "-c", 'model_reasoning_effort="low"',
            "-c", 'model_verbosity="low"',
            "-m", args.codex_model, "--json", prompt,
        ]
        completed = run(command, cwd=cwd, timeout=args.timeout, env=env)
        parser = parse_codex
    elif provider == "claude":
        command = [
            binary, "--print", prompt, "--output-format", "json",
            "--add-dir", str(cwd), "--model", args.claude_model,
            "--effort", "low", "--max-budget-usd", str(args.claude_max_budget_usd),
            "--permission-mode", "bypassPermissions", "--permission-prompts", "none",
            "--dangerously-skip-permissions", "--no-session-persistence",
        ]
        completed = run(command, cwd=cwd, timeout=args.timeout, env=env)
        parser = parse_claude
    elif provider == "opencode":
        command = [
            binary, "run", "--format", "json", "--dir", str(cwd),
            "--model", args.opencode_model, "--variant", args.opencode_variant,
            "--auto", prompt,
        ]
        completed = run(command, cwd=cwd, timeout=args.timeout, env=env)
        parser = parse_opencode
    else:
        command = [
            binary, "--print", prompt, "--output-format", "json",
            "--add-dir", str(cwd),
            "--model", args.agy_model, "--effort", "low", "--mode", "accept-edits",
            "--sandbox", "--dangerously-skip-permissions",
        ]
        completed = run(command, cwd=cwd, timeout=args.timeout, env=env)
        parser = parse_agy
    elapsed = time.monotonic() - started
    if completed.returncode:
        detail = (completed.stderr or completed.stdout)[-4000:]
        raise RuntimeError(f"{provider}/{case} exited {completed.returncode}:\n{detail}")
    reply, inp, out, cached = parser(completed.stdout)
    print(f"{provider}/{case}: input={inp} cached={cached} output={out} reply={reply!r}", flush=True)
    return ProbeResult(provider, case, reply, inp, out, cached, elapsed)


def check_provider(provider: str, worktree: Path, nested: Path, sentinel: Path,
                   sentinel_hash: str, args: argparse.Namespace) -> list[ProbeResult]:
    if provider == "codex":
        clear_rel = ".lockedin/reports/talks/talk-codex/slides.md"
        old, new = "noisey", "noisy"
        ambiguous_rel = ".lockedin/reports/pages/codex-ambiguous.md"
        noun = "coefficient"
    elif provider == "agy":
        clear_rel = ".lockedin/reports/talks/talk-agy/slides.md"
        old, new = "biassed", "biased"
        ambiguous_rel = ".lockedin/reports/pages/agy-ambiguous.md"
        noun = "value"
    elif provider == "opencode":
        clear_rel = ".lockedin/reports/talks/talk-opencode/slides.md"
        old, new = "consistant", "consistent"
        ambiguous_rel = ".lockedin/reports/pages/opencode-ambiguous.md"
        noun = "entry"
    else:
        clear_rel = ".lockedin/reports/talks/talk-claude/slides.md"
        old, new = "untill", "until"
        ambiguous_rel = ".lockedin/reports/pages/claude-ambiguous.md"
        noun = "rate"

    invocation = "Use the installed lockedin-scientist skill." if provider == "opencode" else "Invoke $lockedin-scientist."
    clear_prompt = (
        f"Active session directory: {nested}. {invocation} In `{clear_rel}`, replace exactly `{old}` with `{new}`. "
        "This is an unambiguous mechanical edit. Return only the user-facing job reply."
    )
    clear = invoke(provider, "exact-edit", clear_prompt, nested, args)
    clear_text = (worktree / clear_rel).read_text(encoding="utf-8")
    if old in clear_text or clear_text.count(new) != 1:
        raise AssertionError(f"{provider}: exact edit was not limited to {old!r} -> {new!r}")
    assert_reply(clear)

    ambiguous_path = worktree / ambiguous_rel
    sync_log = worktree / ".test-sync-calls"
    sync_calls = sync_log.read_text(encoding="utf-8").splitlines() if sync_log.exists() else []
    if not any("await-sync" in call and Path(clear_rel).name in call for call in sync_calls):
        raise AssertionError(f"{provider}: reported success without confirming the edited file via await-sync")

    before_hash = digest(ambiguous_path)
    before_mtime = ambiguous_path.stat().st_mtime_ns
    ambiguous_prompt = (
        f"Active session directory: {nested}. {invocation} In `{ambiguous_rel}`, update the {noun}. "
        "Return only the user-facing job reply."
    )
    ambiguous = invoke(provider, "ambiguous", ambiguous_prompt, nested, args)
    if digest(ambiguous_path) != before_hash or ambiguous_path.stat().st_mtime_ns != before_mtime:
        raise AssertionError(f"{provider}: ambiguous request modified its file")
    assert_reply(ambiguous, clarification=True)

    if digest(sentinel) != sentinel_hash:
        raise AssertionError(f"{provider}: touched the main checkout's misleading binding")
    input_ceiling = args.agy_max_input_tokens if provider == "agy" else args.max_input_tokens
    if clear.input_tokens > input_ceiling or ambiguous.input_tokens > input_ceiling:
        raise AssertionError(f"{provider}: input-token ceiling exceeded")
    output_ceiling = args.claude_max_output_tokens if provider == "claude" else args.max_output_tokens
    if clear.output_tokens > output_ceiling or ambiguous.output_tokens > output_ceiling:
        raise AssertionError(f"{provider}: output-token ceiling exceeded")
    return [clear, ambiguous]


def print_results(results: list[ProbeResult]) -> None:
    print("provider  case         input    cached  output  seconds  reply")
    for result in results:
        compact = re.sub(r"\s+", " ", result.reply)
        print(f"{result.provider:8}  {result.case:12}  {result.input_tokens:7}  "
              f"{result.cached_tokens:7}  {result.output_tokens:6}  {result.seconds:7.1f}  {compact}")
    print("\nExact-edit reduction from the captured pre-fix baseline:")
    for result in results:
        if result.case != "exact-edit" or not result.input_tokens:
            continue
        baseline = BASELINES.get(result.provider)
        if not baseline:
            print(f"  {result.provider}: no pre-fix paid baseline recorded")
            continue
        reduction = 100 * (baseline["input"] - result.input_tokens) / baseline["input"]
        print(f"  {result.provider}: {reduction:.1f}% fewer input tokens "
              f"({baseline['input']:,} -> {result.input_tokens:,})")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paid", action="store_true", help="required acknowledgement that real subscriptions are used")
    providers = ("all", "codex", "claude", "agy", "opencode")
    parser.add_argument("provider_name", nargs="?", choices=providers,
                        help="optional provider subset, e.g. agy")
    parser.add_argument("--provider", dest="provider_option", choices=providers,
                        help="flag form of the optional provider subset")
    parser.add_argument("--codex-model", default="gpt-5.6-luna")
    parser.add_argument("--claude-model", default="haiku")
    parser.add_argument("--claude-max-budget-usd", type=float, default=0.10)
    parser.add_argument("--agy-model", default="gemini-3.8-flash-low")
    parser.add_argument("--agy-max-input-tokens", type=int, default=160_000)
    parser.add_argument("--claude-max-output-tokens", type=int, default=3_500)
    parser.add_argument("--opencode-model", default="opencode/muse-spark-1.3-contributor-free")
    parser.add_argument("--opencode-variant", default="low")
    parser.add_argument("--max-input-tokens", type=int, default=140_000)
    parser.add_argument("--max-output-tokens", type=int, default=1_500)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--keep", action="store_true", help="retain the disposable fixture for diagnosis")
    args = parser.parse_args(argv)
    if args.provider_name and args.provider_option and args.provider_name != args.provider_option:
        parser.error("positional provider and --provider disagree")
    args.provider = args.provider_option or args.provider_name or "all"
    return args


def main() -> int:
    args = parse_args()
    if not args.paid:
        print("Refusing to invoke paid models without --paid.", file=sys.stderr)
        return 2
    selected = ("codex", "claude", "agy", "opencode") if args.provider == "all" else (args.provider,)
    missing = [name for name in ("git", *selected) if provider_binary(name) is None]
    if missing:
        print(f"Missing required command(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    temp_parent = REPO_ROOT / "tests" / ".tmp"
    temp_parent.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="live-review-gate-", dir=temp_parent))
    print(f"Disposable fixture: {base}")
    results: list[ProbeResult] = []
    try:
        main_checkout, worktree, nested, sentinel = fixture(base)
        sentinel_hash = digest(sentinel)
        providers = selected
        for provider in providers:
            results.extend(check_provider(provider, worktree, nested, sentinel, sentinel_hash, args))
        print_results(results)
        print("PASS: exact edits, clarity gate, linked-worktree isolation, concise replies, and token ceilings")
        return 0
    finally:
        if args.keep:
            print(f"Kept fixture: {base}")
        else:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
