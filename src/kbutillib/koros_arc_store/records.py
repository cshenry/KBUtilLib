"""Typed records for the KOROS runs tree.

Defines the three record shapes that both consumer apps compile against —
:class:`ProjectRecord`, :class:`ArcRecord` and :class:`ArcProvenance` — plus
:func:`parse_provenance`, which turns a ``PROVENANCE.json`` payload into an
``ArcProvenance`` (or ``None`` with a stable reason code when invalid).

The shapes are pinned; do not add or remove fields casually. Keys that appear
in ``PROVENANCE.json`` but are outside the documented set are preserved verbatim
in :attr:`ArcProvenance.raw` — upstream adds fields, and dropping them silently
is how a parser becomes the reason a future feature is impossible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Stable invalid-reason codes (never prose) ──────────────────────────────
MISSING_RUN_ID = "missing_run_id"
MISSING_CREATED_AT = "missing_created_at"
JSON_PARSE_ERROR = "json_parse_error"
FILE_ABSENT = "file_absent"
NOT_AN_OBJECT = "not_an_object"

# The set of PROVENANCE.json keys that map onto named ArcProvenance fields.
# Every other key is preserved verbatim in ``raw``.
_DOCUMENTED_KEYS = frozenset(
    {
        "run_id",
        "created_at",
        "inputs",
        "tool_versions",
        "compute_targets",
        "run_name",
        "created_by",
        "init_provenance",
        "fair_inputs_ref",
        "offlimits_list_ref",
        "non_overlap_statement",
        "frame_novelty",
        "trace_file",
        "trace_format",
        "project",
        "role",
        "leg_of",
        "parent",
    }
)


@dataclass
class ArcProvenance:
    """A parsed ``PROVENANCE.json`` record.

    ``run_id`` and ``created_at`` are required. ``inputs``, ``tool_versions``
    and ``compute_targets`` are required keys that may be empty — and empty is
    the common case (all 26 live arcs at time of writing). Every other field is
    optional and ``None`` when absent. ``raw`` holds every key outside the
    documented set, verbatim.
    """

    run_id: str
    created_at: str
    inputs: List[Any] = field(default_factory=list)
    tool_versions: Dict[str, Any] = field(default_factory=dict)
    compute_targets: List[Any] = field(default_factory=list)
    run_name: Optional[str] = None
    created_by: Optional[str] = None
    init_provenance: Optional[Any] = None
    fair_inputs_ref: Optional[Any] = None
    offlimits_list_ref: Optional[Any] = None
    non_overlap_statement: Optional[Any] = None
    frame_novelty: Optional[Any] = None
    trace_file: Optional[str] = None
    trace_format: Optional[str] = None
    project: Optional[str] = None
    role: Optional[str] = None
    leg_of: Optional[str] = None
    parent: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProjectRecord:
    """A KOROS project: an immediate subdirectory of the runs root.

    ``arc_count`` is the number of arcs under ``<project>/arcs/``. A project
    with no ``arcs/`` directory and a project with an empty ``arcs/`` directory
    both report ``arc_count == 0`` and are deliberately indistinguishable.
    """

    name: str
    path: Path
    arc_count: int


@dataclass
class ArcRecord:
    """A single arc under ``<project>/arcs/<slug>/``.

    ``slug`` is the directory name verbatim — no lower-casing, no rewriting.
    ``provenance`` is the parsed record, or ``None`` when the arc's
    ``PROVENANCE.json`` is absent or unparseable. ``valid`` and
    ``invalid_reason`` (a stable code, never prose) report that outcome.
    """

    project: str
    slug: str
    path: Path
    provenance: Optional[ArcProvenance]
    valid: bool
    invalid_reason: Optional[str]


def parse_provenance(data: Any) -> tuple[Optional[ArcProvenance], Optional[str]]:
    """Turn a decoded ``PROVENANCE.json`` payload into an ``ArcProvenance``.

    Returns ``(provenance, None)`` on success or ``(None, reason_code)`` when
    the payload is invalid. This function never raises for content problems —
    JSON decoding and file access are the caller's responsibility.

    Validity rules (pinned):
      * the payload must be a JSON object → else ``not_an_object``
      * ``run_id`` is required → else ``missing_run_id``
      * ``created_at`` is required → else ``missing_created_at``
      * ``inputs``/``tool_versions``/``compute_targets`` may be empty (common)
      * every documented field is optional and defaults to ``None``/empty
      * unknown keys are preserved verbatim in ``raw``
    """
    if not isinstance(data, dict):
        return None, NOT_AN_OBJECT

    run_id = data.get("run_id")
    if not run_id:
        return None, MISSING_RUN_ID
    created_at = data.get("created_at")
    if not created_at:
        return None, MISSING_CREATED_AT

    raw = {k: v for k, v in data.items() if k not in _DOCUMENTED_KEYS}

    provenance = ArcProvenance(
        run_id=run_id,
        created_at=created_at,
        inputs=data.get("inputs") or [],
        tool_versions=data.get("tool_versions") or {},
        compute_targets=data.get("compute_targets") or [],
        run_name=data.get("run_name"),
        created_by=data.get("created_by"),
        init_provenance=data.get("init_provenance"),
        fair_inputs_ref=data.get("fair_inputs_ref"),
        offlimits_list_ref=data.get("offlimits_list_ref"),
        non_overlap_statement=data.get("non_overlap_statement"),
        frame_novelty=data.get("frame_novelty"),
        trace_file=data.get("trace_file"),
        trace_format=data.get("trace_format"),
        project=data.get("project"),
        role=data.get("role"),
        leg_of=data.get("leg_of"),
        parent=data.get("parent"),
        raw=raw,
    )
    return provenance, None
