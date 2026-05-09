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

## Phase 3: Pipelines (Linear Chains)

### Overview

A **pipeline** is an ordered list of EE2 job parameter dicts. Each step's job is submitted only after the previous step completes successfully. Pipeline advancement is **passive** -- it happens whenever `refresh_active()` is called (manually, via CLI, or via the watcher from Phase 2).

Key properties:
- **Linear chains only.** No branching, no parallel steps.
- **Fail-fast.** If any step ends in `ERROR` or `TERMINATED`, the pipeline moves to that terminal state and no further steps are submitted.
- **Passive advancement.** No new background thread -- the existing watcher's `refresh_active()` call advances pipelines automatically.

### Data Models

- `PipelineStatus` -- enum: `PENDING`, `RUNNING`, `COMPLETED`, `ERROR`, `TERMINATED`.
- `PipelineState` -- dataclass with pipeline_id, spec (list of ChainStep), status, current_step, total_steps, timestamps, name, project, tags.
- `ChainStep` -- dataclass with params (EE2 run_job dict), optional name and app_id.

Pipeline IDs are 12-character hex strings generated from `uuid4().hex[:12]`.

### Schema v2 Migration

Phase 3 introduces schema version 2, which adds a `pipelines` table. Migration from v1 to v2 is automatic and non-destructive -- the existing `jobs` table is not modified. The migration creates:

```sql
CREATE TABLE pipelines (
    pipeline_id    TEXT PRIMARY KEY,
    name           TEXT,
    project        TEXT,
    tags_json      TEXT NOT NULL DEFAULT '[]',
    spec_json      TEXT NOT NULL,
    status         TEXT NOT NULL,
    current_step   INTEGER NOT NULL DEFAULT 0,
    total_steps    INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    last_advanced_at TEXT,
    finished_at    TEXT
);
```

Forward compatibility: opening a database with a schema version newer than the current code raises `RuntimeError`.

### API

```python
kbu = KBJobUtils(env=env, kb_version="prod")

# Submit a pipeline from a list of EE2 param dicts
pipeline = kbu.submit_chain(
    steps=[
        {"method": "mod.step1", "params": [{"ref": "1/2"}]},
        {"method": "mod.step2", "params": [{"ref": "3/4"}]},
        {"method": "mod.step3", "params": [{"ref": "5/6"}]},
    ],
    name="My 3-step pipeline",
    project="my-project",
    tags=["batch", "genomics"],
)
# pipeline.status == PipelineStatus.RUNNING
# pipeline.current_step == 0 (first step's job is already submitted)

# Or submit from pre-built ChainStep objects
from kbutillib.kb_job_utils import ChainStep
steps = [
    ChainStep(params={"method": "a.a", "params": [{}]}, name="Assemble"),
    ChainStep(params={"method": "b.b", "params": [{}]}, name="Annotate"),
]
pipeline = kbu.submit_chain(steps)

# Query pipelines
kbu.get_pipeline(pipeline.pipeline_id)
kbu.list_pipelines(status=PipelineStatus.RUNNING, project="my-project")

# Cancel a pipeline (idempotent on terminal pipelines)
kbu.cancel_pipeline(pipeline.pipeline_id)

# Manual advancement (usually not needed -- refresh_active does this)
changed = kbu.advance_pipelines()
```

The `refresh_active()` method now calls `advance_pipelines()` after refreshing individual job states. This means the existing watcher thread from Phase 2 automatically advances pipelines on each tick.

### CLI: `kbu jobs chain`

Nested subcommand group under `kbu jobs`:

| Command | Description |
|---------|-------------|
| `kbu jobs chain submit FILE` | Submit a pipeline from a JSON file. Use `-` for stdin. JSON is a list of param dicts or an object with `steps`, `name`, `project`, `tags`. |
| `kbu jobs chain list [--status S] [--project P] [--since DT] [--limit N] [--active]` | List pipelines in a table. `--active` shows non-terminal only. |
| `kbu jobs chain status PIPELINE_ID` | Show pipeline detail with per-step job table. |
| `kbu jobs chain cancel PIPELINE_ID [--force]` | Cancel a running pipeline. Confirmation prompt unless `--force`. |
| `kbu jobs chain advance` | Force a one-shot `advance_pipelines()` call. |

### Examples

Submit a pipeline from a file:
```bash
cat > steps.json << 'EOF'
{
  "name": "Assembly Pipeline",
  "project": "genome-project",
  "steps": [
    {"method": "kb_megahit.run_megahit", "params": [{"read_lib": "1/2/3"}]},
    {"method": "RAST_SDK.annotate_genome", "params": [{"genome_ref": "output_of_step1"}]}
  ]
}
EOF
kbu jobs chain submit steps.json
```

Check pipeline status:
```bash
kbu jobs chain status abc123456789
```

List active pipelines:
```bash
kbu jobs chain list --active
```

Force advancement:
```bash
kbu jobs chain advance
```
