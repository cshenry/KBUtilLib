"""High-level KBase job utilities.

:class:`KBJobUtils` composes a :class:`~kbutillib.shared_env_utils.SharedEnvUtils`
instance (for tokens / config) and a :class:`JobStore` (for local persistence)
to provide a convenient facade over EE2 operations.

Design decisions
~~~~~~~~~~~~~~~~
* **Composition over inheritance** -- holds ``SharedEnvUtils``, does not subclass it.
* Token retrieved via ``env.get_token(namespace="kbase")``.
* Watcher is opt-in, in-process background thread (Phase 2).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

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

    # ── refresh ──────────────────────────────────────────────────────────

    def refresh_active(self) -> List[JobRecord]:
        """Re-check all locally-stored non-terminal jobs against EE2.

        Returns:
            List of updated :class:`JobRecord` objects.
        """
        active = self._store.list_active()
        if not active:
            return []
        job_ids = [r.job_id for r in active]
        results = self.check_jobs(job_ids)
        return list(results.values())

    def refresh_all(self) -> List[JobRecord]:
        """Re-check every locally-stored job against EE2.

        Returns:
            List of updated :class:`JobRecord` objects.
        """
        all_jobs = self._store.list_all()
        if not all_jobs:
            return []
        job_ids = [r.job_id for r in all_jobs]
        results = self.check_jobs(job_ids)
        return list(results.values())

    # ── cleanup ──────────────────────────────────────────────────────────

    def cleanup(
        self,
        older_than_days: int = 30,
        terminal_only: bool = True,
    ) -> int:
        """Delete old job records from the local store.

        Args:
            older_than_days: Remove records older than this many days.
            terminal_only: If True (default), only remove records in
                terminal states.

        Returns:
            Number of records deleted.
        """
        cutoff = datetime.now(timezone.utc).timestamp() - (older_than_days * 86400)
        all_jobs = self._store.list_all()
        deleted = 0
        for rec in all_jobs:
            if terminal_only and not rec.state.is_terminal:
                continue
            if rec.updated_at.timestamp() < cutoff:
                self._store.delete(rec.job_id)
                deleted += 1
        return deleted

    # ── watcher ──────────────────────────────────────────────────────────

    def start_watcher(
        self,
        interval: int = 300,
        on_change: Optional[Callable[[JobRecord, JobRecord], None]] = None,
        daemon: bool = True,
    ) -> "Watcher":
        """Start a background thread that periodically calls refresh_active().

        Args:
            interval: Seconds between refresh passes (default 300, min 30).
            on_change: Optional callback(old_state, new_state) per job whose
                status changed during a refresh pass.  Errors in the callback
                are logged and do NOT kill the thread.
            daemon: Thread daemon flag (default True).

        Returns:
            The :class:`Watcher` instance.

        Idempotent -- calling ``start_watcher`` when one is already running
        returns the existing watcher.
        """
        if hasattr(self, "_watcher") and self._watcher is not None:
            if self._watcher.is_alive():
                return self._watcher
        interval = max(interval, 30)
        w = Watcher(self, interval=interval, on_change=on_change, daemon=daemon)
        self._watcher = w
        w.start()
        return w

    def stop_watcher(self, timeout: float = 5.0) -> bool:
        """Signal the watcher to stop and wait up to *timeout* seconds.

        Returns:
            True if the watcher stopped cleanly, False if it timed out.
        """
        w = getattr(self, "_watcher", None)
        if w is None:
            return True
        w.stop()
        w.join(timeout=timeout)
        alive = w.is_alive()
        if not alive:
            self._watcher = None
        return not alive

    @property
    def watcher(self) -> Optional["Watcher"]:
        """The current watcher, or None if not running."""
        w = getattr(self, "_watcher", None)
        if w is not None and not w.is_alive():
            self._watcher = None
            return None
        return w

    def close(self) -> None:
        """Stop the watcher (if running) and close the database connection."""
        self.stop_watcher()
        self._store.close()


class Watcher:
    """Background thread that periodically refreshes active jobs.

    Instantiated via :meth:`KBJobUtils.start_watcher` -- not typically
    created directly.
    """

    def __init__(
        self,
        kbu: KBJobUtils,
        interval: int = 300,
        on_change: Optional[Callable[[JobRecord, JobRecord], None]] = None,
        daemon: bool = True,
    ) -> None:
        self._kbu = kbu
        self._interval = interval
        self._on_change = on_change
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=daemon, name="kbjobs-watcher"
        )
        self.last_run_at: Optional[datetime] = None
        self.runs: int = 0
        self.errors: int = 0

    def start(self) -> None:
        """Start the watcher thread."""
        self._thread.start()

    def stop(self) -> None:
        """Signal the watcher to stop.  Non-blocking."""
        self._stop_event.set()

    def join(self, timeout: Optional[float] = None) -> None:
        """Wait for the watcher thread to finish."""
        self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        """Return True if the watcher thread is still running."""
        return self._thread.is_alive()

    def _run(self) -> None:
        """Main loop: refresh, diff, callback, sleep."""
        while not self._stop_event.is_set():
            try:
                # Snapshot pre-refresh states for active jobs
                pre_states: Dict[str, JobState] = {}
                for rec in self._kbu.list_active():
                    pre_states[rec.job_id] = rec.state

                refreshed = self._kbu.refresh_active()

                # Diff and emit on_change callbacks
                if self._on_change is not None:
                    for rec in refreshed:
                        old_state = pre_states.get(rec.job_id)
                        if old_state is not None and old_state != rec.state:
                            try:
                                # Build a synthetic "old" record for the callback
                                old_rec = JobRecord(
                                    job_id=rec.job_id,
                                    method=rec.method,
                                    state=old_state,
                                )
                                self._on_change(old_rec, rec)
                            except Exception:
                                logger.exception(
                                    "on_change callback error for job %s",
                                    rec.job_id,
                                )

                self.runs += 1
                self.last_run_at = datetime.now(timezone.utc)
            except Exception:
                logger.exception("Watcher refresh error")
                self.errors += 1

            self._stop_event.wait(self._interval)
