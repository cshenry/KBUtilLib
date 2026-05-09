"""KBase job submission and tracking utilities.

This package provides helpers for submitting jobs to the KBase
Execution Engine 2 (EE2) service and tracking them in a local
SQLite database at ``~/.kbjobs/kbjobs.db``.

Phase 1 scope:
- Submit jobs (``run_job``)
- Retrieve job state from EE2 (``check_job``, ``check_jobs``)
- Persist / query local job records via SQLite
- Cancel jobs (``cancel_job``)
- Retrieve job logs (``get_job_logs``)

Phase 2 scope:
- Refresh active / all jobs (``refresh_active``, ``refresh_all``)
- Cleanup old records (``cleanup``)
- Opt-in background watcher thread (``start_watcher`` / ``stop_watcher``)
- ``kbu jobs`` and ``kbu jobdaemon`` CLI subcommands

Phase 3 scope:
- Linear job pipelines (``submit_chain``, ``advance_pipelines``)
- Pipeline CRUD (``get_pipeline``, ``list_pipelines``, ``cancel_pipeline``)
- ``kbu jobs chain`` CLI subcommand group
"""

from .pipeline import ChainStep, PipelineState, PipelineStatus
from .state import JobRecord, JobState
from .store import JobStore
from .utils import KBJobUtils, Watcher

__all__ = [
    "ChainStep",
    "JobRecord",
    "JobState",
    "JobStore",
    "KBJobUtils",
    "PipelineState",
    "PipelineStatus",
    "Watcher",
]
