# ops — lockedin service supervision & health monitor

The four lockedin processes run as **systemd *user* units**, supervised by a tiny health
monitor that auto-restarts anything that goes down and only spends LLM tokens when an automatic
restart *fails*. Lingering is enabled, so everything starts at boot and survives logout.

## Units (`~/.config/systemd/user/`)

| Unit | What it runs | Health check | Auto-restart |
|------|--------------|--------------|--------------|
| `lockedin-serve.service` | `uv run lockedin serve --host 127.0.0.1 --port 8080` (with `LOCKEDIN_NEWS_ENABLED=1`) | `GET http://127.0.0.1:8080/` → 200 | systemd `on-failure` **+** monitor |
| `lockedin-tunnel.service` | `cloudflared tunnel run` → `https://lockedin.codes` | `GET https://lockedin.codes/` → 200 | systemd `on-failure` **+** monitor |
| `lockedin-slackbot.service` | `uv run lockedin slackbot` (socket mode) | unit `is-active` (no HTTP port) | systemd `on-failure` **+** monitor |
| `lockedin-monitor.{service,timer}` | `ops/healthcheck.sh` every 2 min | — | — |

`ollama.service` is a **system** unit (not ours) with its own restart policy. The monitor
health-checks `http://127.0.0.1:11434/` and, if down, tries `sudo -n systemctl restart ollama`
— if passwordless sudo isn't configured it just logs an alert + escalates instead.

The cloudflare tunnel token lives in `ops/tunnel.env` (mode 0600, git-ignored) and is referenced
by the tunnel unit via `EnvironmentFile=`.

## The monitor (`ops/healthcheck.sh`)

Runs every 2 minutes via `lockedin-monitor.timer`. For each service:

1. **Healthy** → nothing (a single `HEARTBEAT` line is logged at most once/hour so you can see it's alive).
2. **Down** → log `DOWN …`, `systemctl --user restart` the unit, wait 6s, re-check.
3. **Recovered after restart** → log `RESTARTED …` / `RECOVERED …`.
4. **Still down after restart** → log `ATTENTION …` and run **one** bounded `claude -p` diagnosis
   (`timeout 120`, no tools) whose output is appended to the log. One escalation per incident —
   a per-service flag in `.state/incidents/` prevents re-escalating every tick until it recovers.

The public-URL check is skipped (not restarted) when the local server is down, so a server
outage doesn't get misattributed to the tunnel.

Token cost at rest: **zero**. Tokens are spent only when a restart fails to fix a service.

All activity → `ops/monitor.log` (git-ignored).

## Everyday commands

```bash
# Status / logs
systemctl --user status lockedin-serve lockedin-tunnel lockedin-slackbot
journalctl --user -u lockedin-serve -f          # live server log
tail -f ops/monitor.log                         # monitor decisions
systemctl --user list-timers lockedin-monitor.timer

# Manual control
systemctl --user restart lockedin-serve         # restart a service
systemctl --user stop lockedin-monitor.timer    # pause monitoring
/home/hamid/projects/lockedin/ops/healthcheck.sh # run one check now

# After editing a unit file
systemctl --user daemon-reload && systemctl --user restart <unit>
```

## Notes / gotchas

- **Never `pkill -f 'lockedin serve'`** from an interactive shell — the pattern matches your own
  command line and kills the shell. Use `systemctl --user stop lockedin-serve` (or kill by PID).
- Changing the serve **port** means editing both `lockedin-serve.service` (`--port`) and the
  Cloudflare tunnel's public-hostname route (see `DOMAIN_SETUP.md`), plus the URLs in
  `healthcheck.sh`.
- `healthcheck.sh`, this README, and the unit files are safe to commit; `tunnel.env`,
  `monitor.log`, and `.state/` are git-ignored (the first holds the tunnel secret).
- To test auto-heal: `systemctl --user stop lockedin-slackbot`, wait for the next monitor tick,
  and watch it get restarted in `ops/monitor.log`.
