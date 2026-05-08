"""High-level KBase job utilities.

:class:`KBJobUtils` composes a :class:`~kbutillib.shared_env_utils.SharedEnvUtils`
instance (for tokens / config) and a :class:`JobStore` (for local persistence)
to provide a convenient facade over EE2 operations.

Design decisions
~~~~~~~~~~~~~~~~
* **Composition over inheritance** -- holds ``SharedEnvUtils``, does not subclass it.
* Token retrieved via ``env.get_token(namespace="kbase")``.
* No internal polling loop (Phase 2).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..installed_clients.execution_engine2Client import execution_engine2
from ..kbase_endpoints import service_url
from ..shared_env_utils import SharedEnvUtils
from .state import JobRecord, JobState
from .store import JobStore

logger = logging.getLogger(__name__)


class KBJobUtils:
    """Facade for KBase EE2 job operations with local SQLite tracking.

    Args:
        env: A :class:`SharedEnvUtils` instance (or compatible subclass)
            used to obtain the KBase auth token and configuration.
        kb_version: KBase environment name (``"prod"``, ``"appdev"``, ``"ci"``).
        db_path: Override for the SQLite database location.
            Defaults to ``~/.kbjobs/kbjobs.db``.
    """

    def __init__(
        self,
        env: SharedEnvUtils,
        kb_version: str = "prod",
        db_path: Optional[Path] = None,
    ) -> None:
        self._env = env
        self._kb_version = kb_version
        self._ee2_url = service_url("ee2", kb_version)
        self._token = env.get_token(namespace="kbase")
        self._ee2 = execution_engine2(
            url=self._ee2_url, token=self._token
        )
        self._store = JobStore(db_path=db_path)

    # ── properties ───────────────────────────────────────────────────────

    @property
    def store(self) -> JobStore:
        """Access the underlying :class:`JobStore`."""
        return self._store

    @property
    def ee2_client(self) -> execution_engine2:
        """Access the raw EE2 client."""
        return self._ee2

    # ── job submission ───────────────────────────────────────────────────

    def run_job(
        self,
        method: str,
        params: List[Any],
        *,
        app_id: Optional[str] = None,
        workspace_id: Optional[int] = None,
        service_ver: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> JobRecord:
        """Submit a job to EE2 and persist the record locally.

        Args:
            method: Fully-qualified method name
                (e.g. ``"kb_msrec.run_modelseedrecon"``).
            params: Positional params list for the method.
            app_id: Optional KBase app id.
            workspace_id: Optional workspace to associate.
            service_ver: Optional service version / git hash.
            meta: Arbitrary metadata stored in the local record.

        Returns:
            A :class:`JobRecord` with the ``job_id`` populated.
        """
        run_params: Dict[str, Any] = {
            "method": method,
            "params": params,
        }
        if app_id is not None:
            run_params["app_id"] = app_id
        if workspace_id is not None:
            run_params["wsid"] = workspace_id
        if service_ver is not None:
            run_params["service_ver"] = service_ver

        logger.info("Submitting EE2 job: %s", method)
        job_id = self._ee2.run_job(run_params)
        logger.info("EE2 job submitted: %s", job_id)

        record = JobRecord(
            job_id=job_id,
            method=method,
            params=run_params,
            state=JobState.QUEUED,
            workspace_id=workspace_id,
            meta=meta or {},
        )
        self._store.upsert(record)
        return record

    # ── job status ───────────────────────────────────────────────────────

    def check_job(self, job_id: str) -> JobRecord:
        """Query EE2 for the current state of *job_id* and update local store.

        Returns:
            Updated :class:`JobRecord`.
        """
        raw = self._ee2.check_job({"job_id": job_id})
        record = self._store.get(job_id)
        if record is None:
            record = JobRecord(job_id=job_id)

        record.state = JobState(raw.get("status", "created"))
        record.ee2_raw = raw
        if record.state == JobState.ERROR:
            errtext = raw.get("errormsg", "")
            if not errtext and "error" in raw:
                errtext = str(raw["error"])
            record.error_message = errtext
        record.updated_at = datetime.now(timezone.utc)
        self._store.upsert(record)
        return record

    def check_jobs(self, job_ids: List[str]) -> Dict[str, JobRecord]:
        """Batch-check multiple jobs.  Returns a dict keyed by job_id."""
        raw = self._ee2.check_jobs({"job_ids": job_ids})
        results: Dict[str, JobRecord] = {}
        job_states = raw.get("job_states", {})
        for jid, state_raw in job_states.items():
            record = self._store.get(jid)
            if record is None:
                record = JobRecord(job_id=jid)
            record.state = JobState(state_raw.get("status", "created"))
            record.ee2_raw = state_raw
            if record.state == JobState.ERROR:
                errtext = state_raw.get("errormsg", "")
                if not errtext and "error" in state_raw:
                    errtext = str(state_raw["error"])
                record.error_message = errtext
            record.updated_at = datetime.now(timezone.utc)
            self._store.upsert(record)
            results[jid] = record
        return results

    # ── job control ──────────────────────────────────────────────────────

    def cancel_job(self, job_id: str) -> JobRecord:
        """Cancel a running EE2 job and update local record."""
        self._ee2.cancel_job({"job_id": job_id})
        logger.info("Cancelled EE2 job: %s", job_id)
        record = self._store.get(job_id)
        if record is None:
            record = JobRecord(job_id=job_id)
        record.state = JobState.TERMINATED
        record.updated_at = datetime.now(timezone.utc)
        self._store.upsert(record)
        return record

    # ── job logs ─────────────────────────────────────────────────────────

    def get_job_logs(
        self, job_id: str, skip_lines: int = 0
    ) -> Dict[str, Any]:
        """Retrieve log lines for a job from EE2.

        Args:
            job_id: The EE2 job identifier.
            skip_lines: Number of initial log lines to skip.

        Returns:
            Raw response dict from ``execution_engine2.get_job_logs``.
        """
        return self._ee2.get_job_logs(
            {"job_id": job_id, "skip_lines": skip_lines}
        )

    # ── local queries (delegated to store) ───────────────────────────────

    def get_record(self, job_id: str) -> Optional[JobRecord]:
        """Retrieve a locally-stored record without contacting EE2."""
        return self._store.get(job_id)

    def list_active(self) -> List[JobRecord]:
        """Return all locally-stored non-terminal jobs."""
        return self._store.list_active()

    def list_all(self) -> List[JobRecord]:
        """Return every locally-stored job record."""
        return self._store.list_all()

    # ── cleanup ──────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying database connection."""
        self._store.close()
