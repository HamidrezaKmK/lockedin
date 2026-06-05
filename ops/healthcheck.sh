#!/usr/bin/env bash
# lockedin health monitor — deterministic, token-free.
#
# Run every couple of minutes by the lockedin-monitor.timer systemd user unit.
# Checks the four services, auto-restarts any that are down (via systemctl --user),
# and only escalates to a one-shot `claude -p` diagnosis when a restart fails to
# bring a service back. All output goes to ops/monitor.log.
#
# Targets:
#   server   — FastAPI app on 127.0.0.1:8080  (unit: lockedin-serve, user-managed)
#   webpage  — public URL https://lockedin.codes via the tunnel (unit: lockedin-tunnel)
#   slackbot — Slack socket-mode bot (unit: lockedin-slackbot, no HTTP port)
#   ollama   — local qwen backend on 127.0.0.1:11434 (SYSTEM service; not restarted here)

set -u

# A predictable PATH so uv / claude / node / systemctl resolve under systemd.
export PATH="/home/hamid/.local/bin:/home/hamid/.nvm/versions/node/v22.22.3/bin:/usr/local/bin:/usr/bin:/bin"

ROOT="/home/hamid/projects/lockedin"
LOG="$ROOT/ops/monitor.log"
STATE="$ROOT/ops/.state"
FLAGS="$STATE/incidents"
HEARTBEAT="$STATE/last_heartbeat"
HEARTBEAT_EVERY=3600          # log a "still healthy" line at most once per hour
RESTART_WAIT=6                # seconds to wait after a restart before re-checking
CLAUDE_TIMEOUT=120            # hard cap on the diagnosis call

mkdir -p "$FLAGS"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >>"$LOG"; }

# --- probes ----------------------------------------------------------------
http_ok() {  # url expected_code
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$1" 2>/dev/null)
    [ "$code" = "$2" ]
}
unit_active() { systemctl --user is-active --quiet "$1"; }

# --- escalation (one bounded claude call per incident) ---------------------
escalate() {  # name unit scope(user|system)
    local name="$1" unit="$2" scope="$3" flag="$FLAGS/$name"
    [ -f "$flag" ] && return 0      # already escalated for this ongoing incident
    : >"$flag"
    local journal recent prompt diag
    if [ "$scope" = "system" ]; then
        journal=$(journalctl -u "$unit" -n 40 --no-pager 2>/dev/null)
    else
        journal=$(journalctl --user -u "$unit" -n 40 --no-pager 2>/dev/null)
    fi
    recent=$(tail -n 25 "$LOG" 2>/dev/null)
    prompt="You are a terse ops assistant. Do NOT use any tools — analyze only the text below.
The systemd service '$unit' (lockedin '$name') failed its health check and an automatic
restart did NOT bring it back. In 3-5 lines give the most likely root cause and the single
best fix command.

=== recent monitor.log ===
$recent

=== journalctl $unit (last 40) ===
$journal"
    diag=$(timeout "$CLAUDE_TIMEOUT" claude -p "$prompt" 2>&1)
    log "CLAUDE-DIAGNOSIS ($name):"
    printf '%s\n' "$diag" | sed 's/^/    /' >>"$LOG"
}

clear_incident() {  # name -> log RECOVERED if there was an open incident
    local name="$1" flag="$FLAGS/$name"
    if [ -f "$flag" ]; then
        log "RECOVERED $name — healthy again"
        rm -f "$flag"
    fi
}

# --- per-service handlers --------------------------------------------------
# Returns 0 if healthy at end, 1 if still down. Sets global ${name}_state=ok|down|blocked.
heal_http_user() {  # name unit url expect
    local name="$1" unit="$2" url="$3" expect="$4"
    if http_ok "$url" "$expect"; then clear_incident "$name"; printf 'ok'; return 0; fi
    log "DOWN $name — $url did not return $expect; restarting $unit"
    systemctl --user restart "$unit" 2>>"$LOG"
    sleep "$RESTART_WAIT"
    if http_ok "$url" "$expect"; then
        log "RESTARTED $name — healthy after restarting $unit"
        rm -f "$FLAGS/$name"; printf 'ok'; return 0
    fi
    log "ATTENTION $name still DOWN after restarting $unit"
    escalate "$name" "$unit" user
    printf 'down'; return 1
}

heal_unit_only() {  # name unit  (no HTTP endpoint — liveness == unit active)
    local name="$1" unit="$2"
    if unit_active "$unit"; then clear_incident "$name"; printf 'ok'; return 0; fi
    log "DOWN $name — $unit not active; restarting"
    systemctl --user restart "$unit" 2>>"$LOG"
    sleep "$RESTART_WAIT"
    if unit_active "$unit"; then
        log "RESTARTED $name — active after restarting $unit"
        rm -f "$FLAGS/$name"; printf 'ok'; return 0
    fi
    log "ATTENTION $name still inactive after restarting $unit"
    escalate "$name" "$unit" user
    printf 'down'; return 1
}

check_ollama() {  # system service: best-effort sudo restart, else escalate
    local name="ollama" unit="ollama.service"
    if http_ok "http://127.0.0.1:11434/" 200; then clear_incident "$name"; printf 'ok'; return 0; fi
    log "DOWN ollama — 127.0.0.1:11434 unreachable; attempting sudo restart"
    if sudo -n systemctl restart "$unit" 2>>"$LOG"; then
        sleep "$RESTART_WAIT"
        if http_ok "http://127.0.0.1:11434/" 200; then
            log "RESTARTED ollama — healthy after sudo restart"; rm -f "$FLAGS/$name"; printf 'ok'; return 0
        fi
    else
        log "NOTE ollama restart needs passwordless sudo (not configured) — escalating"
    fi
    log "ATTENTION ollama still DOWN"
    escalate "$name" "$unit" system
    printf 'down'; return 1
}

# --- run -------------------------------------------------------------------
server=$(heal_http_user server lockedin-serve.service "http://127.0.0.1:8080/" 200)

# The public URL can only work if the local server is up; don't blame/restart the
# tunnel for a server outage.
if [ "$server" = "ok" ]; then
    web=$(heal_http_user webpage lockedin-tunnel.service "https://lockedin.codes/" 200)
else
    web="blocked"
    log "SKIP webpage check — local server is down (fix server first)"
fi

slackbot=$(heal_unit_only slackbot lockedin-slackbot.service)
ollama=$(check_ollama)

summary="server=$server webpage=$web slackbot=$slackbot ollama=$ollama"

# Hourly heartbeat when everything is healthy, so the log shows liveness without spam.
if [ "$server" = "ok" ] && [ "$web" = "ok" ] && [ "$slackbot" = "ok" ] && [ "$ollama" = "ok" ]; then
    now=$(date +%s)
    last=$(cat "$HEARTBEAT" 2>/dev/null || echo 0)
    if [ $(( now - last )) -ge "$HEARTBEAT_EVERY" ]; then
        log "HEARTBEAT all healthy ($summary)"
        printf '%s' "$now" >"$HEARTBEAT"
    fi
fi

exit 0
