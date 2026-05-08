"""Data models for KBase job records.

Defines the canonical ``JobRecord`` dataclass and the ``JobState``
enum that mirrors the EE2 job-state vocabulary.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


class JobState(str, enum.Enum):
    """EE2 job states.

    Values match the strings returned by ``execution_engine2.check_job``.
    """

    CREATED = "created"
    ESTIMATING = "estimating"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"
    TERMINATED = "terminated"

    @classmethod
    def terminal_states(cls) -> frozenset["JobState"]:
        """Return the set of states from which a job cannot transition."""
        return frozenset({cls.COMPLETED, cls.ERROR, cls.TERMINATED})

    @property
    def is_terminal(self) -> bool:
        return self in self.terminal_states()


@dataclass
class JobRecord:
    """Local representation of a KBase EE2 job.

    Attributes:
        job_id: EE2 job identifier.
        method: Fully-qualified method name (e.g. ``"kb_msrec.run_modelseedrecon"``).
        params: JSON-serialisable parameters dict sent to EE2.
        state: Current :class:`JobState`.
        workspace_id: Optional workspace associated with the job.
        narrative_id: Optional narrative cell ID.
        created_at: UTC timestamp when the record was first stored locally.
        updated_at: UTC timestamp of the last local update.
        ee2_raw: Raw JSON dict from the last ``check_job`` response.
        error_message: Extracted error text when ``state`` is ``ERROR``.
        meta: Arbitrary user metadata (tags, notes, etc.).
    """

    job_id: str
    method: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    state: JobState = JobState.CREATED
    workspace_id: Optional[int] = None
    narrative_id: Optional[int] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ee2_raw: Dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)
