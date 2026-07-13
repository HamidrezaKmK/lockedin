#!/usr/bin/env bash
# Install lockedin-scientist directly from the active Scientist branch with uv.
set -euo pipefail
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/ and rerun this command." >&2
  exit 1
fi
uv tool install --force "git+https://github.com/HamidrezaKmK/lockedin.git@scientist"
echo "Installed lockedin-scientist from the scientist branch. Run: lockedin-scientist login --server https://your-lockedin.example"
