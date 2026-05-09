# KBJobUtils

KBJobUtils provides a facade for KBase EE2 job operations with local SQLite tracking.

## Phase 1 API

### Core methods

- `run_job(method, params, ...)` -- submit a job to EE2 and persist locally.
- `check_job(job_id)` / `check_jobs(job_ids)` -- query EE2 and update local store.
- `cancel_job(job_id)` -- cancel a running EE2 job.
- `get_job_logs(job_id, skip_lines=0)` -- retrieve log lines from EE2.
- `get_record(job_id)` -- local-only lookup (no EE2 call).
- `list_active()` / `list_all()` -- query local store.

### Data models

- `JobState` -- enum: `CREATED`, `ESTIMATING`, `QUEUED`, `RUNNING`, `COMPLETED`, `ERROR`, `TERMINATED`.
- `JobRecord` -- dataclass with job_id, method, params, state, timestamps, metadata.
- `JobStore` -- SQLite wrapper at `~/.kbjobs/kbjobs.db`.

## Phase 2 API

### Refresh and cleanup

- `refresh_active()` -- re-check all non-terminal jobs against EE2. Returns list of updated records.
- `refresh_all()` -- re-check every job against EE2.
- `cleanup(older_than_days=30, terminal_only=True)` -- delete old records from local store. Returns count deleted.

### Watcher

An opt-in, in-process background thread that periodically calls `refresh_active()`.

```python
kbu = KBJobUtils(env=env, kb_version="prod")

# Start the watcher (idempotent)
watcher = kbu.start_watcher(
    interval=300,           # seconds between passes (min 30)
    on_change=my_callback,  # optional: called(old_rec, new_rec) on transitions
    daemon=True,            # thread daemon flag
)

# Observability
print(watcher.runs, watcher.errors, watcher.last_run_at)

# Stop cleanly
kbu.stop_watcher(timeout=5.0)  # returns True if stopped, False if timed out

# Property access
kbu.watcher  # returns Watcher or None
```

The `on_change` callback receives two `JobRecord` objects: one with the old state and one with the new state. Exceptions in the callback are logged and do not kill the watcher thread.

## CLI: `kbu jobs`

Subcommand group for inspecting and managing tracked KBase EE2 jobs.

### Common options

- `--store-path PATH` -- override `~/.kbjobs/kbjobs.db`.
- `--kb-version {prod,appdev,ci}` -- KBase environment (default: prod).

### Subcommands

| Command | Description |
|---------|-------------|
| `kbu jobs status <JOB_ID>` | Show detailed status for a single job. |
| `kbu jobs list [--status S] [--active] [--limit N]` | Tabular list of jobs. |
| `kbu jobs summary` | Per-status job counts. |
| `kbu jobs refresh [JOB_IDs...] [--all] [--active]` | Force refresh from EE2. Default: active only. |
| `kbu jobs logs <JOB_ID> [--skip N] [--follow]` | Stream job log lines. `--follow` polls until terminal. |
| `kbu jobs cancel <JOB_ID> [--force]` | Cancel a job on EE2. Confirmation prompt unless `--force`. |
| `kbu jobs forget <JOB_ID>... [--force]` | Delete local records (does NOT cancel on server). |
| `kbu jobs cleanup [--older-than-days N] [--all-statuses] [--force]` | Remove old records from local store. |

## CLI: `kbu jobdaemon`

Run the KBJobUtils watcher in the foreground until SIGINT/SIGTERM.

```
kbu jobdaemon [--interval 300] [--store-path PATH] [--kb-version prod] [--log-level INFO]
```

- Constructs a `KBJobUtils`, starts the watcher with `daemon=False`.
- Handles SIGINT and SIGTERM for clean shutdown.
- Logs a summary (runs, errors, last_run_at) on exit.
