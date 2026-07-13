#!/usr/bin/env bash
# Install the platform-specific lockedin-scientist binary from a GitHub Release.
set -euo pipefail
REPO="HamidrezaKmK/lockedin"
VERSION="${LOCKEDIN_SCIENTIST_VERSION:-latest}"
OS="$(uname -s)"; ARCH="$(uname -m)"
case "$OS/$ARCH" in
  Linux/x86_64) asset="lockedin-scientist-linux-x64";;
  Darwin/arm64) asset="lockedin-scientist-macos-arm64";;
  Darwin/x86_64) asset="lockedin-scientist-macos-x64";;
  *) echo "Unsupported platform $OS/$ARCH. Use Windows install.ps1 or build from source." >&2; exit 1;;
esac
base="${LOCKEDIN_SCIENTIST_RELEASE_BASE:-https://github.com/$REPO/releases/${VERSION}/download}"
dest="${HOME}/.local/bin"; mkdir -p "$dest"
tmp="$(mktemp)"; trap 'rm -f "$tmp" "$tmp.sha256"' EXIT
if ! curl -fsSL "$base/$asset" -o "$tmp"; then
  echo "No release asset for $asset yet. Check the Scientist release workflow, or use the clone + uv setup for now." >&2
  exit 1
fi
if ! curl -fsSL "$base/$asset.sha256" -o "$tmp.sha256"; then
  echo "Release checksum for $asset is not available yet. Check the Scientist release workflow." >&2
  exit 1
fi
expected="$(awk '{print $1}' "$tmp.sha256")"
actual="$(sha256sum "$tmp" | awk '{print $1}')"
[[ -n "$expected" && "$expected" == "$actual" ]] || { echo "Checksum verification failed." >&2; exit 1; }
install -m 0755 "$tmp" "$dest/lockedin-scientist"
ln -sf lockedin-scientist "$dest/lockedin_scientist"
echo "Installed $dest/lockedin-scientist. Ensure $dest is on PATH, then run: lockedin-scientist login --server https://your-lockedin.example"
