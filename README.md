# mac-server-dashboard

Read-only status page for a Mac used as a home server. It shows:

- **Local LLMs**: GPU load and memory (with a short history graph), RAM and swap,
  Ollama (loaded models, recent requests) and any `mlx_lm.server` LaunchAgents
  (model, memory footprint, prompt cache, recent requests).
- **GitHub Actions self-hosted runners** in `/Users/*/actions-runner*`: status,
  current job, recent jobs, warnings.

It is a single Python file using only the standard library. It works with the system
`/usr/bin/python3` (3.9) and needs no sudo. GPU data comes from `ioreg`, and per-process
memory comes from `footprint`.

## Run

```bash
python3 runner_dashboard.py        # http://0.0.0.0:8089
```

The JSON behind the page is served at `/api/status`.

| Env var            | Default                   |
| ------------------ | ------------------------- |
| `RUNNER_DASH_PORT` | `8089`                    |
| `RUNNER_DASH_BIND` | `0.0.0.0`                 |
| `OLLAMA_URL`       | `http://127.0.0.1:11434`  |

## Install as a LaunchAgent

```bash
mkdir -p ~/runner-dashboard
cp runner_dashboard.py ~/runner-dashboard/
cp dk.pless.runner-dashboard.plist ~/Library/LaunchAgents/   # adjust paths inside first
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dk.pless.runner-dashboard.plist
```

To restart after an update, run `launchctl kickstart -k gui/$(id -u)/dk.pless.runner-dashboard`.
Logs go to `~/Library/Logs/runner-dashboard.log`.
