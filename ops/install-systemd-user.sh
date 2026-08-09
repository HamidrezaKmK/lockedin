#!/usr/bin/env bash
# Render lockedin systemd user units for this clone.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
SYSTEMD_USER_DIR="${SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
ENV_FILE="${LOCKEDIN_ENV_FILE:-$ROOT/ops/lockedin.env}"
TEMPLATE_DIR="$ROOT/ops/systemd"

escape_sed_replacement() {
    printf '%s' "$1" | sed 's/[\/&|]/\\&/g'
}

render_template() {
    local src="$1" dest="$2"
    local root_escaped env_escaped
    root_escaped="$(escape_sed_replacement "$ROOT")"
    env_escaped="$(escape_sed_replacement "$ENV_FILE")"
    sed \
        -e "s|__LOCKEDIN_ROOT__|$root_escaped|g" \
        -e "s|__LOCKEDIN_ENV_FILE__|$env_escaped|g" \
        "$src" >"$dest"
}

mkdir -p "$SYSTEMD_USER_DIR"

if [ ! -f "$ENV_FILE" ]; then
    mkdir -p "$(dirname -- "$ENV_FILE")"
    cp "$ROOT/ops/lockedin.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "Created $ENV_FILE from ops/lockedin.env.example."
    echo "Edit it before enabling optional tunnel, Slack, or public URL monitoring."
fi

for template in "$TEMPLATE_DIR"/*.service.template; do
    unit_name="$(basename "$template" .template)"
    render_template "$template" "$SYSTEMD_USER_DIR/$unit_name"
    echo "Wrote $SYSTEMD_USER_DIR/$unit_name"
done

cp "$TEMPLATE_DIR/lockedin-monitor.timer" "$SYSTEMD_USER_DIR/lockedin-monitor.timer"
echo "Wrote $SYSTEMD_USER_DIR/lockedin-monitor.timer"

systemctl --user daemon-reload

cat <<EOF

Systemd user units are installed for this clone.

Recommended next commands:
  systemctl --user enable --now lockedin-serve.service
  systemctl --user enable --now lockedin-monitor.timer

Optional services:
  systemctl --user enable --now lockedin-tunnel.service
  systemctl --user enable --now lockedin-slackbot.service

For boot persistence after logout:
  loginctl enable-linger "$USER"

Useful checks:
  systemctl --user status lockedin-serve.service
  journalctl --user -u lockedin-serve.service -f
  $ROOT/ops/healthcheck.sh

EOF
