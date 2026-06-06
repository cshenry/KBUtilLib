"""Job monitoring for kb_app_runner.

:class:`JobMonitor` polls EE2 in batches using the fixed
:meth:`~kbutillib.kb_job_utils.KBJobUtils.check_jobs` and returns
:class:`JobReport` objects once all submitted handles have reached a
terminal state.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Literal

if TYPE_CHECKING:
    from ..kb_job_utils import KBJobUtils

logger = logging.getLogger(__name__)

# EE2 states that indicate a job is still active.
_NON_TERMINAL = frozenset({"created", "estimating", "queued", "running"})
_TERMINAL_SUCCESS = frozenset({"completed"})
_TERMINAL_ERROR = frozenset({"error", "terminated"})


@dataclass
class JobHandle:
    """A lightweight reference to a submitted EE2 job.

    Attributes:
        job_id: The EE2 job identifier.
        app_id: The KBase app identifier that was run.
        wsid: Numeric workspace ID associated with the job.
        meta: Caller-supplied metadata (tags, notes, etc.).
    """

    job_id: str
    app_id: str = ""
    wsid: int = 0
    meta: dict = field(default_factory=dict)


@dataclass
class JobReport:
    """Result of monitoring a single :class:`JobHandle`.

    Attributes:
        handle: The :class:`JobHandle` this report describes.
        state: Terminal state: ``"completed"`` or ``"error"``.
            Non-terminal states appear only in intermediate :meth:`JobMonitor.status`
            calls; ``wait_all`` always returns terminal reports.
        result: Raw EE2 result dict when ``state == "completed"``.
        error: Error message when ``state == "error"``.
        tail: Last N log lines from the job container (populated on error).
    """

    handle: JobHandle
    state: Literal["queued", "running", "completed", "error"]
    result: dict | None = None
    error: str | None = None
    tail: list[str] = field(default_factory=list)


class JobMonitor:
    """Batch job monitor using :meth:`~kbutillib.kb_job_utils.KBJobUtils.check_jobs`.

    Args:
        job_utils: A :class:`~kbutillib.kb_job_utils.KBJobUtils` instance.
        poll_interval: Seconds between poll rounds (default 30 s).
        log_tail_lines: Number of log lines to tail on job failure (default 50).
    """

    def __init__(
        self,
        job_utils: "KBJobUtils",
        poll_interval: float = 30.0,
        log_tail_lines: int = 50,
    ) -> None:
        self._jobs = job_utils
        self._poll_interval = poll_interval
        self._log_tail_lines = log_tail_lines

    # ── public API ────────────────────────────────────────────────────────

    def wait_all(
        self,
        handles: list[JobHandle],
        *,
        on_progress: Callable[[JobReport], None] | None = None,
    ) -> list[JobReport]:
        """Block until every handle reaches a terminal EE2 state.

        Uses :meth:`~kbutillib.kb_job_utils.KBJobUtils.check_jobs` (batch)
        rather than per-job ``check_job`` calls.  On each poll round, newly
        terminal handles are removed from the active set.

        On a transition to ``error``, :meth:`get_job_logs` is called once and
        the last :attr:`log_tail_lines` lines are placed in
        :attr:`JobReport.tail`.

        Args:
            handles: Submitted :class:`JobHandle` objects to monitor.
            on_progress: Optional callback invoked for each handle as it
                reaches a terminal state.

        Returns:
            One :class:`JobReport` per handle, in the same order.
        """
        if not handles:
            return []

        report_map: dict[str, JobReport] = {}
        pending: set[str] = {h.job_id for h in handles}
        handle_map: dict[str, JobHandle] = {h.job_id: h for h in handles}

        while pending:
            records = self._jobs.check_jobs(list(pending))

            for jid, record in records.items():
                status: str = record.state.value if hasattr(record.state, "value") else str(record.state)

                if status in _NON_TERMINAL:
                    continue  # still active

                handle = handle_map[jid]

                if status in _TERMINAL_SUCCESS:
                    report = JobReport(
                        handle=handle,
                        state="completed",
                        result=record.ee2_raw,
                    )
                else:
                    # error or terminated
                    tail = self._fetch_tail(jid)
                    report = JobReport(
                        handle=handle,
                        state="error",
                        error=record.error_message or status,
                        tail=tail,
                    )

                report_map[jid] = report
                pending.discard(jid)

                if on_progress is not None:
                    on_progress(report)

            if pending:
                logger.debug(
                    "JobMonitor: %d/%d handles still pending; sleeping %.0fs",
                    len(pending),
                    len(handles),
                    self._poll_interval,
                )
                time.sleep(self._poll_interval)

        # Return in original order.
        return [report_map[h.job_id] for h in handles]

    def status(self, handles: list[JobHandle]) -> list[JobReport]:
        """Return a single-round status snapshot for *handles* (non-blocking).

        Handles that have not yet reached a terminal state are returned with
        state ``"queued"`` or ``"running"``.
        """
        if not handles:
            return []

        job_ids = [h.job_id for h in handles]
        records = self._jobs.check_jobs(job_ids)
        reports: list[JobReport] = []

        for handle in handles:
            record = records.get(handle.job_id)
            if record is None:
                reports.append(
                    JobReport(handle=handle, state="queued", error="unknown job_id")
                )
                continue

            status: str = record.state.value if hasattr(record.state, "value") else str(record.state)

            if status in _TERMINAL_ERROR:
                tail = self._fetch_tail(handle.job_id)
                reports.append(
                    JobReport(
                        handle=handle,
                        state="error",
                        error=record.error_message or status,
                        tail=tail,
                    )
                )
            elif status in _TERMINAL_SUCCESS:
                reports.append(
                    JobReport(handle=handle, state="completed", result=record.ee2_raw)
                )
            elif status == "running":
                reports.append(JobReport(handle=handle, state="running"))
            else:
                reports.append(JobReport(handle=handle, state="queued"))

        return reports

    # ── internals ─────────────────────────────────────────────────────────

    def _fetch_tail(self, job_id: str) -> list[str]:
        """Return the last ``log_tail_lines`` lines from the EE2 job log.

        Returns an empty list if the RPC fails (best-effort; don't let a
        log-fetch error mask the original job failure).
        """
        try:
            raw = self._jobs.get_job_logs(job_id, latest=True)
            lines_raw = raw.get("lines", [])
            lines = [
                entry.get("line", "") if isinstance(entry, dict) else str(entry)
                for entry in lines_raw
            ]
            return lines[-self._log_tail_lines :]
        except Exception as exc:
            logger.warning("get_job_logs(%s) failed: %s", job_id, exc)
            return []
