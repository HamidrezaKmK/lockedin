"""Create (or reset) the persistent ``unittest`` user as a reproducible qwen fixture.

  uv run python -m tests.setup_unittest_user

Creates account ``unittest`` / ``unittest`` in this checkout's ``data/users/``, sets its active
model to qwen, copies the two diffusion-paper assets from an existing local user (``username``),
creates+approves the ``diffusion-models`` bubble, and seeds an Overview page with intentionally
duplicated links (the state the editing flow must be able to clean up). Idempotent: re-running
wipes and rebuilds the user's workspace. Logging in as unittest/unittest lets you exercise the
exact scenario by hand.
"""
from __future__ import annotations

import shutil
import sys

from lockedin import auth, bubbles, paths

from tests._fixtures import (DIFFUSION_BUBBLE, REPO_DATA_USERS, seed_diffusion_workspace,
                             set_qwen, source_user_with_pdfs, write_overview)

OVERVIEW_WITH_DUPES = """\
# Diffusion Models

## Overview

Diffusion models are a powerful class of generative models.

## Key Papers

- [[generative-models-via-drifting]]
- [[a-geometric-view-of-data-complexity]]
- [[generative-models-via-drifting]]
- [[a-geometric-view-of-data-complexity]]
"""


def main() -> int:
    src = source_user_with_pdfs()
    if src is None:
        print("ERROR: no local user has the diffusion PDFs to copy. Upload them first.",
              file=sys.stderr)
        return 1

    home = paths.user_home("unittest")
    # reset: drop the workspace and any existing account record
    if home.exists():
        shutil.rmtree(home)
    accounts = auth.load_accounts()
    if "unittest" in accounts:
        accounts.pop("unittest")
        auth.save_accounts(accounts)

    auth.create_user("unittest", "unittest")
    set_qwen(home)
    pids = seed_diffusion_workspace(home, source_user=src)
    write_overview(home, OVERVIEW_WITH_DUPES)

    with paths.use_root(home):
        pages = [p["page_slug"] for p in bubbles.list_pages(DIFFUSION_BUBBLE)]
    print(f"✓ unittest user ready (PDFs from '{src}'): assets={pids}, "
          f"bubble='{DIFFUSION_BUBBLE}', pages={pages}")
    print(f"  workspace: {home}")
    print("  login: unittest / unittest  (active model: qwen)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
