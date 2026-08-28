"""Give every pre-existing workspace the Tutorial bubble new ones are born with.

``workspaces.create`` seeds the Tutorial bubble, so only workspaces made after that shipped
have one. Everyone who signed up earlier opens a blank workspace and is taught nothing. This
backfills them once.

Deliberately skips any workspace that already has a ``tutorial`` bubble: :func:`tutorial.seed`
is not idempotent — it would add a second copy of every page and deck — so the registry check
here is what makes re-running safe. A bubble someone deleted on purpose stays deleted only
until this runs again, which is why this is a one-shot backfill and not a startup sweep.

    LOCKEDIN_HOME=<root> uv run python scripts/seed_tutorial_backfill.py [--apply]
"""
from __future__ import annotations

import sys

from lockedin import bubbles, paths, tutorial, workspaces

SLUG = "tutorial"


def main(apply: bool) -> int:
    data = workspaces._load().get("workspaces", {})
    if not data:
        print("no workspaces registered")
        return 1
    seeded = skipped = failed = 0
    for wid, rec in sorted(data.items(), key=lambda kv: kv[1].get("created_at", "")):
        root = workspaces.workspace_home(wid)
        owner = rec.get("owner_user", "") or "you"
        label = f"{rec.get('name', wid)!r} ({owner})"
        if not root.exists():
            print(f"  !! no root on disk, skipped: {label}")
            failed += 1
            continue
        with paths.use_root(root):
            if SLUG in bubbles.load_registry():
                print(f"  skip     {label} — already has one")
                skipped += 1
                continue
            if not apply:
                print(f"  would seed {label}")
                seeded += 1
                continue
            slug = tutorial.seed(owner)
            if slug:
                print(f"  seeded   {label} -> {slug}")
                seeded += 1
            else:
                # tutorial.seed swallows its own errors, so a None is the only signal.
                print(f"  !! seeding failed, left untouched: {label}")
                failed += 1
    verb = "seeded" if apply else "would be seeded"
    print(f"\n{seeded} {verb}, {skipped} already had one, {failed} failed")
    if not apply:
        print("re-run with --apply to write")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main("--apply" in sys.argv))
