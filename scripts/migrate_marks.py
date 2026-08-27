"""Give every existing review thread one of the five marks.

Threads predate the shared vocabulary, so they carry no `kind` and would all render as the
neutral "? I don't follow" — which is wrong, and misleading to an agent, because the kind is
what tells it whether to re-derive, re-explain, expand, keep or cut.

There is no inference here worth trusting: the mapping below was made by reading each thread.
Run with --apply to write; without it, this prints the plan and changes nothing.

    LOCKEDIN_HOME=<root> uv run python scripts/migrate_marks.py [--apply]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from lockedin import paths

# thread id -> mark, one line per thread, decided by reading it.
DECIDED = {
    # "this paragraph is very janky, we should fix it later" — the prose is wrong as written.
    "k3vV7coz": "bad",
    # "use larger batches for FID evaluations" — asks for more work, not a correction.
    "5Qx3qdUm": "more",
    # "strange wording" — the sentence is wrong as written.
    "oFuptmXp": "bad",
    # "very long sentence" — asks for it to go.
    "CaXU2_kd": "cut",
}
FALLBACK = "q"   # "I don't follow" — the mark that asks for an explanation, not a change


def main(apply: bool) -> int:
    root = paths.base_root() / "data" / "workspaces"
    if not root.exists():
        print(f"no workspaces under {root}")
        return 1
    touched = 0
    for path in sorted(root.glob("*/REPORTS/*/comments/*.json")):
        try:
            data = json.loads(path.read_text())
        except Exception as exc:                     # a sidecar we cannot read is left alone
            print(f"  !! unreadable, skipped: {path} ({exc})")
            continue
        changed = False
        for thread in data.get("threads", []):
            if thread.get("kind"):
                continue
            tid = str(thread.get("id") or "")
            kind = DECIDED.get(tid, FALLBACK)
            first = ((thread.get("messages") or [{}])[0].get("body") or "").replace("\n", " ")
            mark = "decided" if tid in DECIDED else "fallback"
            print(f"  {kind:5} ({mark})  {path.parent.parent.name}/{path.stem}  {tid}"
                  f"  “{first[:56]}”")
            thread["kind"] = kind
            changed = True
            touched += 1
        if changed and apply:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(path)
    print(f"\n{touched} thread(s) {'updated' if apply else 'would be updated'}")
    if not apply:
        print("re-run with --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--apply" in sys.argv))
