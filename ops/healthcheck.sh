#!/usr/bin/env bash
# lockedin health monitor.
#
# Intended for the lockedin-monitor.timer systemd user unit, but safe to run by hand.
# It derives all paths from this repository and uses environment variables for URLs.

set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

export PATH="${PATH:-/usr/local/bin:/usr/bin:/bin}:$HOME/.local/bin"

LOG="${LOCKEDIN_MONITOR_LOG:-$ROOT/ops/monitor.log}"
STATE="${LOCKEDIN_MONITOR_STATE:-$ROOT/ops/.state}"
FLAGS="$STATE/incidents"
HEARTBEAT="$STATE/last_heartbeat"
HEARTBEAT_EVERY="${LOCKEDIN_HEARTBEAT_EVERY:-3600}"
RESTART_WAIT="${LOCKEDIN_RESTART_WAIT:-6}"
CLAUDE_TIMEOUT="${LOCKEDIN_CLAUDE_TIMEOUT:-120}"

LOCKEDIN_LOCAL_URL="${LOCKEDIN_LOCAL_URL:-http://127.0.0.1:8080/}"
LOCKEDIN_PUBLIC_URL="${LOCKEDIN_PUBLIC_URL:-}"
LOCKEDIN_SLACKBOT_ENABLED="${LOCKEDIN_SLACKBOT_ENABLED:-0}"
LOCKEDIN_OLLAMA_ENABLED="${LOCKEDIN_OLLAMA_ENABLED:-1}"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434/}"

mkdir -p "$FLAGS"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >>"$LOG"; }

http_ok() {
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$1" 2>/dev/null)
    [ "$code" = "$2" ]
}

unit_active() { systemctl --user is-active --quiet "$1"; }

escalate() {
    local name="$1" unit="$2" scope="$3" flag="$FLAGS/$name"
    [ -f "$flag" ] && return 0
    : >"$flag"

    if ! command -v claude >/dev/null 2>&1; then
        log "DIAGNOSIS-SKIPPED $name - claude CLI not found"
        return 0
    fi

    local journal recent prompt diag
    if [ "$scope" = "system" ]; then
        journal=$(journalctl -u "$unit" -n 40 --no-pager 2>/dev/null)
    else
        journal=$(journalctl --user -u "$unit" -n 40 --no-pager 2>/dev/null)
    fi
    recent=$(tail -n 25 "$LOG" 2>/dev/null)
    prompt="You are a terse ops assistant. Do not use tools. Analyze only the text below.
The systemd service '$unit' (lockedin '$name') failed its health check and an automatic
restart did not bring it back. In 3-5 lines give the most likely root cause and the single
best fix command.

=== recent monitor.log ===
$recent

=== journalctl $unit (last 40) ===
$journal"
    diag=$(timeout "$CLAUDE_TIMEOUT" claude -p "$prompt" 2>&1)
    log "CLAUDE-DIAGNOSIS ($name):"
    printf '%s\n' "$diag" | sed 's/^/    /' >>"$LOG"
}

clear_incident() {
    local name="$1" flag="$FLAGS/$name"
    if [ -f "$flag" ]; then
        log "RECOVERED $name - healthy again"
        rm -f "$flag"
    fi
}

heal_http_user() {
    local name="$1" unit="$2" url="$3" expect="$4"
    if http_ok "$url" "$expect"; then clear_incident "$name"; printf 'ok'; return 0; fi
    log "DOWN $name - $url did not return $expect; restarting $unit"
    systemctl --user restart "$unit" 2>>"$LOG"
    sleep "$RESTART_WAIT"
    if http_ok "$url" "$expect"; then
        log "RESTARTED $name - healthy after restarting $unit"
        rm -f "$FLAGS/$name"
        printf 'ok'
        return 0
    fi
    log "ATTENTION $name still DOWN after restarting $unit"
    escalate "$name" "$unit" user
    printf 'down'
    return 1
}

heal_unit_only() {
    local name="$1" unit="$2"
    if unit_active "$unit"; then clear_incident "$name"; printf 'ok'; return 0; fi
    log "DOWN $name - $unit not active; restarting"
    systemctl --user restart "$unit" 2>>"$LOG"
    sleep "$RESTART_WAIT"
    if unit_active "$unit"; then
        log "RESTARTED $name - active after restarting $unit"
        rm -f "$FLAGS/$name"
        printf 'ok'
        return 0
    fi
    log "ATTENTION $name still inactive after restarting $unit"
    escalate "$name" "$unit" user
    printf 'down'
    return 1
}

check_ollama() {
    local name="ollama" unit="ollama.service"
    if http_ok "$OLLAMA_URL" 200; then clear_incident "$name"; printf 'ok'; return 0; fi
    log "DOWN ollama - $OLLAMA_URL unreachable; attempting sudo restart"
    if sudo -n systemctl restart "$unit" 2>>"$LOG"; then
        sleep "$RESTART_WAIT"
        if http_ok "$OLLAMA_URL" 200; then
            log "RESTARTED ollama - healthy after sudo restart"
            rm -f "$FLAGS/$name"
            printf 'ok'
            return 0
        fi
    else
        log "NOTE ollama restart needs passwordless sudo or manual intervention"
    fi
    log "ATTENTION ollama still DOWN"
    escalate "$name" "$unit" system
    printf 'down'
    return 1
}

server=$(heal_http_user server lockedin-serve.service "$LOCKEDIN_LOCAL_URL" 200)

if [ -n "$LOCKEDIN_PUBLIC_URL" ]; then
    if [ "$server" = "ok" ]; then
        web=$(heal_http_user webpage lockedin-tunnel.service "$LOCKEDIN_PUBLIC_URL" 200)
    else
        web="blocked"
        log "SKIP webpage check - local server is down"
    fi
else
    web="disabled"
fi

if [ "$LOCKEDIN_SLACKBOT_ENABLED" = "1" ]; then
    slackbot=$(heal_unit_only slackbot lockedin-slackbot.service)
else
    slackbot="disabled"
fi

if [ "$LOCKEDIN_OLLAMA_ENABLED" = "1" ]; then
    ollama=$(check_ollama)
else
    ollama="disabled"
fi

summary="server=$server webpage=$web slackbot=$slackbot ollama=$ollama"

if [ "$server" = "ok" ] && { [ "$web" = "ok" ] || [ "$web" = "disabled" ]; } \
    && { [ "$slackbot" = "ok" ] || [ "$slackbot" = "disabled" ]; } \
    && { [ "$ollama" = "ok" ] || [ "$ollama" = "disabled" ]; }; then
    now=$(date +%s)
    last=$(cat "$HEARTBEAT" 2>/dev/null || echo 0)
    if [ $(( now - last )) -ge "$HEARTBEAT_EVERY" ]; then
        log "HEARTBEAT healthy ($summary)"
        printf '%s' "$now" >"$HEARTBEAT"
    fi
fi

exit 0
