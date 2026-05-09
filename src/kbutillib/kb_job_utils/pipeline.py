"""Data models for KBase job pipelines (linear chains).

Defines the canonical :class:`PipelineState` dataclass,
:class:`PipelineStatus` enum, and :class:`ChainStep` dataclass
for representing ordered sequences of EE2 jobs that execute
one after another.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class PipelineStatus(str, enum.Enum):
    """Pipeline lifecycle states.

    Values:
        PENDING: Created but no step submitted yet (transient).
        RUNNING: One of its steps is currently in flight.
        COMPLETED: All steps succeeded.
        ERROR: A step ended in ERROR.
        TERMINATED: A step was canceled / pipeline was canceled.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"
    TERMINATED = "terminated"

    def is_terminal(self) -> bool:
        """Return True if this status represents a final state."""
        return self in (PipelineStatus.COMPLETED, PipelineStatus.ERROR,
                        PipelineStatus.TERMINATED)


@dataclass
class ChainStep:
    """A single step in a pipeline chain.

    Attributes:
        params: The EE2 ``run_job`` params dict.
        name: Optional human-readable name for the step.
        app_id: Optional KBase app id (convenience for display;
            otherwise derived from params).
    """

    params: Dict[str, Any]
    name: Optional[str] = None
    app_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        d: Dict[str, Any] = {"params": self.params}
        if self.name is not None:
            d["name"] = self.name
        if self.app_id is not None:
            d["app_id"] = self.app_id
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ChainStep":
        """Deserialize from a dict."""
        return cls(
            params=d["params"],
            name=d.get("name"),
            app_id=d.get("app_id"),
        )


@dataclass
class PipelineState:
    """Local representation of a linear job pipeline.

    Attributes:
        pipeline_id: Short unique identifier (``uuid4().hex[:12]``).
        spec: Ordered list of :class:`ChainStep` objects.
        status: Current :class:`PipelineStatus`.
        current_step: Index of the step currently running or last completed.
        total_steps: ``len(spec)`` at submission time.
        created_at: UTC timestamp when the pipeline was created.
        last_advanced_at: UTC timestamp of the last step advancement.
        finished_at: UTC timestamp when the pipeline reached a terminal state.
        name: Optional human-readable pipeline name.
        project: Optional project identifier for filtering.
        tags: Arbitrary string tags for filtering/display.
    """

    pipeline_id: str
    spec: List[ChainStep]
    status: PipelineStatus
    current_step: int
    total_steps: int
    created_at: datetime
    last_advanced_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    name: Optional[str] = None
    project: Optional[str] = None
    tags: List[str] = field(default_factory=list)

    def is_terminal(self) -> bool:
        """Return True if the pipeline has reached a final state."""
        return self.status.is_terminal()

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "pipeline_id": self.pipeline_id,
            "spec": [s.to_dict() for s in self.spec],
            "status": self.status.value,
            "current_step": self.current_step,
            "total_steps": self.total_steps,
            "created_at": self.created_at.isoformat(),
            "last_advanced_at": (self.last_advanced_at.isoformat()
                                 if self.last_advanced_at else None),
            "finished_at": (self.finished_at.isoformat()
                            if self.finished_at else None),
            "name": self.name,
            "project": self.project,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PipelineState":
        """Deserialize from a dict."""

        def _parse_dt(val: Optional[str]) -> Optional[datetime]:
            if val is None:
                return None
            dt = datetime.fromisoformat(val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        return cls(
            pipeline_id=d["pipeline_id"],
            spec=[ChainStep.from_dict(s) for s in d["spec"]],
            status=PipelineStatus(d["status"]),
            current_step=d["current_step"],
            total_steps=d["total_steps"],
            created_at=_parse_dt(d["created_at"]),  # type: ignore[arg-type]
            last_advanced_at=_parse_dt(d.get("last_advanced_at")),
            finished_at=_parse_dt(d.get("finished_at")),
            name=d.get("name"),
            project=d.get("project"),
            tags=d.get("tags", []),
        )
