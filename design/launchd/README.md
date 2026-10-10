# launchd templates (Phase C)

This directory contains the launchd plist template used to host the
project-owned worker process. It is **opt-in and not installed** by
this revision.

## Files

- `com.memory-agent.worker.plist` — template. Copy to
  `~/Library/LaunchAgents/` (or `/Library/LaunchDaemons/` for a
  system-level install), edit the placeholders, then
  `launchctl load -w …`.

## What it does

- Starts the worker on boot (`RunAtLoad=true`).
- Restarts the worker on crash (`KeepAlive.Crashed=true`); the worker
  itself owns timing and schedules via `worker_schedule.json`.
- Forwards stdout / stderr to a log file you choose.

## What it deliberately does NOT do

- **No launchd `StartCalendarInterval` / `StartInterval`**. The
  schedule lives inside the worker (`worker_schedule.json`); launchd
  only restarts the process when it dies. This keeps the worker
  portable to other OS service managers (systemd, Windows SCM,
  supervisord) without rewriting the schedule.

## Placeholders

| Token | Replace with |
|---|---|
| `<VAULT_PATH>` | absolute path to the vault (e.g. `/Users/you/memory-agent/vault`) |
| `<WORKER_ID>` | stable worker identifier (default: `worker_local`) |
| `<PYTHON_BIN>` | absolute path to the Python interpreter (e.g. `/usr/local/bin/python3.12`) |
| `<SOURCE_ROOT>` | absolute path to the memory-agent source root (e.g. `/Users/you/memory-agent`) |
| `<LOG_STDIO>` | path to stdout log (e.g. `<VAULT_PATH>/logs/worker.log`) |
| `<LOG_STDIO_ERR>` | path to stderr log (e.g. `<VAULT_PATH>/logs/worker.err.log`) |

## Removal

```bash
launchctl unload -w ~/Library/LaunchAgents/com.memory-agent.worker.plist
```

The plist is plain text and contains no secrets; deleting the file
along with the unload is sufficient.