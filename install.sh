#!/usr/bin/env bash
# Install only the dependency-free lockedin-scientist client from the released main branch.
set -euo pipefail
BRANCH="main"
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
client_tmp="$(mktemp)"
vendors_tmp="$(mktemp)"
trap 'rm -f "$client_tmp" "$vendors_tmp"' EXIT
commit="$(curl -fsSL "https://api.github.com/repos/HamidrezaKmK/lockedin/commits/$BRANCH" | sed -n 's/^[[:space:]]*"sha": "\([0-9a-f]\{40\}\)".*/\1/p' | head -n 1)"
if [ -z "$commit" ]; then
  echo "Could not resolve the current LockedIn Scientist release from main." >&2
  exit 1
fi
# Fetch an immutable commit URL rather than a branch URL: raw GitHub branch responses can be
# served from an older CDN cache immediately after a release.
source_root="https://raw.githubusercontent.com/HamidrezaKmK/lockedin/$commit/src/lockedin"
curl -fsSL "$source_root/scientist_cli.py" -o "$client_tmp"
curl -fsSL "$source_root/agent_vendors.py" -o "$vendors_tmp"
changed=0
if ! cmp -s "$vendors_tmp" "$root/agent_vendors.py"; then
  install -m 0644 "$vendors_tmp" "$root/agent_vendors.py"
  changed=1
fi
if ! cmp -s "$client_tmp" "$root/scientist_cli.py"; then
  install -m 0644 "$client_tmp" "$root/scientist_cli.py"
  changed=1
fi
printf '#!/usr/bin/env bash\nexec %q %q "$@"\n' "$PYTHON" "$root/scientist_cli.py" > "$bin/lockedin-scientist"
chmod 0755 "$bin/lockedin-scientist"
ln -sf lockedin-scientist "$bin/lockedin_scientist"
echo "Updated command: $bin/lockedin-scientist"
echo "Updated source:  $root/scientist_cli.py"
echo "Source commit:   $commit"
if [ "$changed" -eq 1 ]; then
  "$bin/lockedin-scientist" upgrade-workers
else
  echo "Already current; running workers and vendor integrations were left untouched."
fi
