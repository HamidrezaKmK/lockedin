# ops

Portable service templates and monitoring for running lockedin from any clone path.

## Environment

Copy the example env file and edit it for your deployment:

```bash
cp ops/lockedin.env.example ops/lockedin.env
```

`ops/lockedin.env` is git-ignored. It controls the systemd services and monitor:

- `LOCKEDIN_PORT=8080` sets the local server port used by the generated unit.
- `LOCKEDIN_LOCAL_URL=http://127.0.0.1:8080/` is always checked by the monitor.
- `LOCKEDIN_PUBLIC_URL=` controls public tunnel monitoring. Leave it empty to skip public checks.
- `LOCKEDIN_NEWS_ENABLED=0` keeps the Claude-powered news crawler off by default.
- `LOCKEDIN_SLACKBOT_ENABLED=1` enables Slack bot monitoring when you also enable the service.
- `LOCKEDIN_OLLAMA_ENABLED=0` skips Ollama monitoring if you are not using local Qwen.

For a Cloudflare named tunnel, put tunnel secrets or arguments in `ops/tunnel.env`:

```bash
CLOUDFLARE_TUNNEL_TOKEN=your-token
# or:
# TUNNEL_TOKEN=your-token
# or:
# CLOUDFLARED_TUNNEL_ARGS=run your-tunnel-name
```

`ops/tunnel.env`, logs, and monitor state are git-ignored.

## Install Systemd User Units

Render the templates into `~/.config/systemd/user`:

```bash
./ops/install-systemd-user.sh
```

The script derives the repository root from its own location, creates `ops/lockedin.env` if
missing, renders unit files with the actual clone path, and runs:

```bash
systemctl --user daemon-reload
```

It does not automatically enable every service. Start the required web server first:

```bash
systemctl --user enable --now lockedin-serve.service
systemctl --user enable --now lockedin-monitor.timer
```

Optional services:

```bash
systemctl --user enable --now lockedin-tunnel.service
systemctl --user enable --now lockedin-slackbot.service
loginctl enable-linger "$USER"
```

Enable lingering if you want user services to survive logout and start at boot.

## Units

Generated units:

- `lockedin-serve.service`: runs `uv run lockedin serve --host 127.0.0.1 --port ${LOCKEDIN_PORT}`.
- `lockedin-tunnel.service`: runs `cloudflared` from `ops/tunnel.env`.
- `lockedin-slackbot.service`: runs `uv run lockedin slackbot`.
- `lockedin-monitor.service`: runs `ops/healthcheck.sh`.
- `lockedin-monitor.timer`: runs the monitor every 2 minutes.

## Healthcheck

Run one check manually:

```bash
./ops/healthcheck.sh
```

The monitor always checks `LOCKEDIN_LOCAL_URL`. It checks the public tunnel only when
`LOCKEDIN_PUBLIC_URL` is non-empty; otherwise the summary reports `webpage=disabled`.
Missing Claude CLI does not break monitoring. If a restart fails and `claude` is installed, the
monitor appends one bounded diagnosis to `ops/monitor.log`.

## Status And Logs

```bash
systemctl --user status lockedin-serve.service
systemctl --user status lockedin-monitor.timer
journalctl --user -u lockedin-serve.service -f
journalctl --user -u lockedin-tunnel.service -f
tail -f ops/monitor.log
```

After changing a unit file or re-running the installer:

```bash
systemctl --user daemon-reload
systemctl --user restart lockedin-serve.service
```
