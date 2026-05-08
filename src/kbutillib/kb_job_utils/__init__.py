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

Phase 2 (future): polling loop, callbacks, batch helpers.
"""

from .state import JobRecord, JobState
from .store import JobStore
from .utils import KBJobUtils

__all__ = [
    "JobRecord",
    "JobState",
    "JobStore",
    "KBJobUtils",
]
