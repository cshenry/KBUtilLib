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
from enum import IntEnum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

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


@dataclass
class ArcArtifact:
    """A recoverable file artifact under one arc, as enumerated by the store.

    ``family`` is one of ``"model"``, ``"fba"`` or ``"fva"`` — the artifact
    kind by the backfill scan contract. ``subject`` is the model/output stem
    (a ``*.model.json`` file yields the name with the suffix stripped; an
    ``fba``/``fva`` output yields the file stem). ``path`` is absolute.
    """

    family: str
    subject: str
    path: Path


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


# ── Run-database record types (Responsibility 2) ───────────────────────────────

# The four trust tiers, DECREASING in trust. The enum value orders them so a
# caller can ask for "homology-or-better" by comparison. Lower value == MORE
# trusted. These four are the only tiers; the schema stores the string form.
VERIFIED = "verified"
HOMOLOGY = "homology"
HYPOTHESIS = "hypothesis"
OPINION = "opinion"


class TrustTier(IntEnum):
    """The four trust tiers, ordered so a lower value is MORE trusted.

    The ordering exists so callers can ask for "homology-or-better": a record
    at ``TrustTier.VERIFIED`` or ``TrustTier.HOMOLOGY`` is homology-or-better
    because their integer values are ``<= TrustTier.HOMOLOGY``. Do NOT confuse
    the integer with a quality score — it is a rank, most-trusted first.
    """

    VERIFIED = 0
    HOMOLOGY = 1
    HYPOTHESIS = 2
    OPINION = 3

    @classmethod
    def from_str(cls, value: str) -> "TrustTier":
        """Parse a tier string to a :class:`TrustTier` or raise ``ValueError``."""
        try:
            return _TIER_BY_NAME[value]
        except KeyError as exc:
            raise ValueError(f"unknown trust tier: {value!r}") from exc

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name.lower()


_TIER_BY_NAME: Dict[str, TrustTier] = {t.name.lower(): t for t in TrustTier}

# The set of valid tier strings, for structural validation.
TRUST_TIERS = frozenset(_TIER_BY_NAME)

# The valid status values (S9): lower-case, exact.
STATUS_VALUES = frozenset({"ok", "failed", "partial"})

# The five permitted bridge kinds (S26). These name the BRIDGE, not the tier.
BRIDGE_KINDS = frozenset(
    {
        "id_join",
        "sequence_homolog",
        "profile_hmm",
        "model_prediction",
        "user_assertion",
    }
)


def floor_tier(tiers: Iterable[str]) -> str:
    """Return the FLOOR — the least-trusted — of the contributing tiers.

    A record summarising several findings carries the floor: the least-trusted
    contributing tier. Adding a lower-tier finding LOWERS the floor; adding a
    higher-tier one NEVER raises it. This is the ONLY way a summary tier is
    produced — there is deliberately no API that raises a tier (anti-laundering).

    Raises ``ValueError`` on an empty input (a summary of nothing has no floor)
    or on an unrecognised tier string.
    """
    ranks = [TrustTier.from_str(t) for t in tiers]
    if not ranks:
        raise ValueError("floor_tier requires at least one contributing tier")
    # Least trusted == highest integer rank.
    return str(max(ranks))


@dataclass
class AnalysisRecord:
    """One run of one analysis, as stored in the run database.

    The shape is FROZEN and shared with the peer app being written against this
    module (S17); do not add, remove or retype fields casually.

    ``record_id``, ``analysis_id`` and ``run_uid`` are the split identity:
    ``analysis_id`` groups every run of one logical analysis, ``record_id`` is
    the per-run primary key, and ``run_uid`` distinguishes a re-run (new uid →
    new row) from a retry (same uid → replace). All three are producer-supplied;
    the module's shared helpers derive ``analysis_id`` and ``record_id`` so two
    producers agree.

    ``payload`` is the record's own OPAQUE payload — small, never interpreted
    here. The large gene-/reaction-level detail goes to the separate blob tier
    via the ``detail`` argument of :meth:`record_analysis`, never onto this
    record.

    The summary columns (``subject_feature_count`` … ``ic_corpus_version``) are
    OPTIONAL and PRODUCER-SUPPLIED; this module computes none of them.
    """

    record_id: str
    analysis_id: str
    run_uid: str
    kind: str
    created_at: str
    producer: str
    producer_version: str
    subject: str
    status: str
    artifacts: Dict[str, str]
    payload: Optional[Dict[str, Any]]
    trust_tier: str
    provenance: Dict[str, Any]
    contract_version: int
    project: Optional[str] = None
    arc: Optional[str] = None
    subject_feature_count: Optional[int] = None
    method_count: Optional[int] = None
    consistency_overall: Optional[float] = None
    consistency_metric_version: Optional[str] = None
    ic_corpus_version: Optional[str] = None
    # 0 known, 1 well-formed but outside a supplied known_kinds, 2 malformed
    # (S43). Populated by the store on write and on read-back.
    unknown_kind: int = 0
