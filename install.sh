#!/usr/bin/env bash
# Install only the dependency-free lockedin-scientist client from the Scientist branch.
set -euo pipefail
BRANCH="${LOCKEDIN_SCIENTIST_BRANCH:-scientist}"
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Python 3.11+ is required (set PYTHON to its executable and rerun)." >&2
  exit 1
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  echo "Python 3.11+ is required." >&2
  exit 1
fi
root="${XDG_DATA_HOME:-$HOME/.local/share}/lockedin-scientist/client"
bin="$HOME/.local/bin"
mkdir -p "$root" "$bin"
tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
url="https://raw.githubusercontent.com/HamidrezaKmK/lockedin/$BRANCH/src/lockedin/scientist_cli.py"
curl -fsSL "$url" -o "$tmp"
install -m 0644 "$tmp" "$root/scientist_cli.py"
printf '#!/usr/bin/env bash\nexec %q %q "$@"\n' "$PYTHON" "$root/scientist_cli.py" > "$bin/lockedin-scientist"
chmod 0755 "$bin/lockedin-scientist"
ln -sf lockedin-scientist "$bin/lockedin_scientist"
echo "Installed only lockedin-scientist. Ensure $bin is on PATH, then run: lockedin-scientist login --server https://your-lockedin.example"
