# mac-server-dashboard

Read-only status page for a Mac used as a home server. It shows:

- **Local LLMs**: GPU load and memory (with a short history graph), RAM and swap,
  Ollama (loaded models, recent requests) and any `mlx_lm.server` LaunchAgents
  (model, memory footprint, prompt cache, recent requests).
- **Scheduled jobs**: every LaunchAgent in `~/Library/LaunchAgents` with a
  `StartCalendarInterval` or `StartInterval` (e.g. a nightly backup): schedule, last and
  next run, last exit code and the last lines of its log (the command is shown by name only, and anything in the log that looks like a password, token or key is masked, since the page has no login). A job shows as **failed** when its
  last exit code isn't 0, and as **missed** when nothing was logged within 30 minutes of a
  scheduled run (calendar schedules with a log only). Lines that start with `YYYY-MM-DD HH:MM:SS` get their time shown.
- **GitHub Actions self-hosted runners** in `/Users/*/actions-runner*`: status,
  current job, recent jobs, warnings.

It is a single Python file using only the standard library. It works with the system
`/usr/bin/python3` (3.9) and needs no sudo. GPU data comes from `ioreg`, and per-process
memory comes from `footprint`.

## Run

```bash
python3 mac_server_dashboard.py        # http://0.0.0.0:8089
```

The JSON behind the page is served at `/api/status`.

| Env var         | Default                  |
| --------------- | ------------------------ |
| `MAC_DASH_PORT` | `8089`                   |
| `MAC_DASH_BIND` | `0.0.0.0`                |
| `OLLAMA_URL`    | `http://127.0.0.1:11434` |

## Install as a LaunchAgent

```bash
mkdir -p ~/mac-server-dashboard
cp mac_server_dashboard.py ~/mac-server-dashboard/
cp dk.pless.mac-server-dashboard.plist ~/Library/LaunchAgents/   # adjust paths inside first
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dk.pless.mac-server-dashboard.plist
```

To restart after an update, run `launchctl kickstart -k gui/$(id -u)/dk.pless.mac-server-dashboard`.
Logs go to `~/Library/Logs/mac-server-dashboard.log`.
