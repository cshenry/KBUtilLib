"""The pure data layer for the Models and Analyses app.

:func:`build_portfolio` and :func:`build_arc_models` take a store — the real
:class:`~kbutillib.koros_arc_store.store.KorosArcStore` or the shipped
:class:`~kbutillib.koros_arc_store_testing.FakeKorosArcStore` — and return the
endpoint JSON as plain dicts. NOTHING here imports a web framework, and NOTHING
here imports ``pathlib`` to walk the runs tree: every read of the runs tree goes
through the store. That is an import boundary a reviewer can confirm mechanically
(see tests/models_and_analyses/test_layering.py).

The response schemas are part of the contract (binding): the frontend and any
later client bind to the exact JSON shape, and a schema test asserts it.

Counting rule (binding, Chris 2026-09-24): counts are DISTINCT on
``analysis_id``, everywhere. A count is a count of ANALYSES, not runs. An arc
holding one analysis re-run six times reports ONE. ``list_analyses(...,
latest_only=True)`` is the collapsed view used for every count; the expanded
per-run view comes from ``list_analyses(..., analysis_id=<id>)``.

Blob discipline (binding): both endpoints are answerable from summary columns and
the record ``payload`` alone. NEITHER calls ``read_detail`` — a test asserts the
store's ``blob_reads`` counter stays at zero across both.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..arc_context import APP_ID, app_us
from ..koros_arc_store import (
    CONTRACT_VERSION,
    AnalysisRecord,
    ArcRecord,
)
from ..koros_arc_store.exceptions import ContractVersionMismatch
from .metrics import (
    ConfusionMatrix,
    compute_confusion_matrix,
    extract_gene_pairs_from_payload,
)
from .prefixes import (
    MODEL_BUILD_KINDS,
    PORTFOLIO_BUCKETS,
    classify_kind,
)

# The kind that carries measured RB-TnSeq fitness (the experiment side of the
# agreement metric). fitness_prop is the PROPAGATED variant and is deliberately
# NOT this — propagated fitness must never stand in for measured (anti-laundering).
FITNESS_ANALYSIS_KIND = "kbdl.fitness_analysis"

# The state-directory literal. It appears in EXACTLY ONE place (binding S16):
# resolve_app_state_dir(). Never interpolate kind-apps inside $KING_STATE.
_STATE_SUBDIR = Path("state") / APP_ID


def resolve_app_state_dir() -> Path:
    """Return this app's state directory, creating parents if missing (S16).

    Precedence: ``$KING_STATE/state/models-and-analyses`` when ``$KING_STATE`` is
    set, else ``~/kind-apps/state/models-and-analyses``. The ``kind-apps`` literal
    lives HERE and nowhere else. It is NEVER interpolated inside ``$KING_STATE`` —
    on the pod ``$KING_STATE`` already ends in ``kind-apps``, and doubling it is
    how a bad path survived two review rounds. The app id comes from
    :data:`APP_ID`, never spelled by hand. ``$KING_STATE`` and the home directory
    are read at CALL time, not import time, so the environment is honoured.
    """
    king_state = os.environ.get("KING_STATE")
    if king_state:
        state_dir = Path(king_state) / _STATE_SUBDIR
    else:
        state_dir = Path.home() / "kind-apps" / _STATE_SUBDIR
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def check_startup_contract_version(
    king_contract_version: Optional[int],
) -> None:
    """Gate app startup on the CAC contract version (invariant I5).

    The app targets integer :data:`CONTRACT_VERSION` (1). If it can see KING's
    contract version (a non-``None`` integer), it is compared by INTEGER EQUALITY:
    equal proceeds; ANY difference refuses startup by raising
    :class:`~kbutillib.koros_arc_store.exceptions.ContractVersionMismatch`, whose
    message names BOTH versions.

    The success criteria settle the gate: ANY difference refuses startup — there
    is NO minor/warn-and-proceed branch (this matches
    ``conformance.check_contract_version``, which says explicitly "NO minor/warn
    branch"). When ``king_contract_version`` is ``None`` (KING not visible — the
    standalone / ``--no-king`` case), the check is a no-op: standalone operation
    is invariant I1 and must not be gated on a version it cannot see.
    """
    if king_contract_version is None:
        return
    if king_contract_version != CONTRACT_VERSION:
        raise ContractVersionMismatch(
            expected=CONTRACT_VERSION, found=king_contract_version
        )


# ── portfolio ──────────────────────────────────────────────────────────────────


def _empty_bucket_counts() -> Dict[str, int]:
    """Return a fresh zeroed count map over :data:`PORTFOLIO_BUCKETS`."""
    return {bucket: 0 for bucket in PORTFOLIO_BUCKETS}


def _tier_of(record: AnalysisRecord) -> str:
    """Return a record's trust tier, defaulting defensively to ``hypothesis``."""
    return record.trust_tier or "hypothesis"


def _summarize_arc_records(
    latest_records: List[AnalysisRecord],
) -> Dict[str, Any]:
    """Build the count + tier summary for one arc from its LATEST-only records.

    ``latest_records`` is ``list_analyses(..., latest_only=True)`` — one record
    per ``analysis_id`` — so every count here is DISTINCT on ``analysis_id``
    (counts of analyses, never runs). Counts are bucketed by
    :func:`classify_kind`; a foreign kind lands in ``other`` and is COUNTED, never
    dropped. Trust tiers are reported as a per-tier breakdown, NOT merged into one
    undifferentiated number (binding: do not aggregate across tiers).
    """
    counts = _empty_bucket_counts()
    # Per-tier analysis counts, so a hypothesis-tier model output is never merged
    # with measured experimental data into a single number.
    tier_counts: Dict[str, int] = {}
    most_recent: Optional[str] = None

    for rec in latest_records:
        counts[classify_kind(rec.kind)] += 1
        tier = _tier_of(rec)
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        if most_recent is None or rec.created_at > most_recent:
            most_recent = rec.created_at

    return {
        "counts": counts,
        "counts_are_distinct_on": "analysis_id",
        "tier_counts": tier_counts,
        "most_recent_analysis_at": most_recent,
    }


def _arc_node(
    store: Any,
    project: str,
    arc: ArcRecord,
) -> Dict[str, Any]:
    """Build one arc node for the portfolio: metadata, counts, empty flag, tiers.

    An arc with ZERO records is INCLUDED and flagged ``empty: true`` (never
    omitted). ``leg_of`` and ``parent`` are surfaced so the caller can nest a leg
    under its parent. All arc metadata comes from the arc's provenance; the store
    is the only door to the runs tree.
    """
    prov = arc.provenance
    latest = store.list_analyses(project, arc.slug, latest_only=True)
    summary = _summarize_arc_records(latest)
    total_analyses = len(latest)

    return {
        "slug": arc.slug,
        "project": project,
        "title": (prov.run_name if prov else None),
        "role": (prov.role if prov else None),
        "leg_of": (prov.leg_of if prov else None),
        "parent": (prov.parent if prov else None),
        "created_at": (prov.created_at if prov else None),
        "valid": arc.valid,
        "invalid_reason": arc.invalid_reason,
        "empty": total_analyses == 0,
        "total_analyses": total_analyses,
        "most_recent_analysis_at": summary["most_recent_analysis_at"],
        "counts": summary["counts"],
        "counts_are_distinct_on": summary["counts_are_distinct_on"],
        "tier_counts": summary["tier_counts"],
        "attribution": "attributed",
    }


def _nest_legs(arc_nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Nest an arc under its parent when ``leg_of`` or ``parent`` is set.

    Returns the top-level arcs, each carrying a ``children`` list of the arcs
    whose ``leg_of``/``parent`` names it. An arc whose named parent is not present
    in the same project stays at top level (its ``leg_of``/``parent`` is still
    surfaced on the node), so a dangling reference never hides an arc.
    """
    by_slug = {node["slug"]: node for node in arc_nodes}
    for node in arc_nodes:
        node.setdefault("children", [])
    roots: List[Dict[str, Any]] = []
    for node in arc_nodes:
        parent_slug = node.get("leg_of") or node.get("parent")
        if parent_slug and parent_slug in by_slug and parent_slug != node["slug"]:
            by_slug[parent_slug]["children"].append(node)
        else:
            roots.append(node)
    return roots


def _unattributed_node(store: Any) -> Dict[str, Any]:
    """Build the portfolio node for records with no project/arc.

    The store holds records with no project/arc in a root-level unattributed
    index (both columns NULL). Most records have no arc today, so an app that
    hides them looks correct while being empty for the wrong reason. This node
    carries the SAME count shape as an arc and a DISTINCT ``attribution:
    "unattributed"`` badge (binding: unattributed is not empty).
    """
    latest = store.list_analyses(None, None, latest_only=True)
    summary = _summarize_arc_records(latest)
    return {
        "attribution": "unattributed",
        "total_analyses": len(latest),
        "empty": len(latest) == 0,
        "most_recent_analysis_at": summary["most_recent_analysis_at"],
        "counts": summary["counts"],
        "counts_are_distinct_on": summary["counts_are_distinct_on"],
        "tier_counts": summary["tier_counts"],
    }


def build_portfolio(store: Any) -> Dict[str, Any]:
    """Build the ``GET /api/portfolio`` response from *store*.

    Every project under the runs root, each with its arcs (legs nested under
    parents). Per arc: slug, title, role, leg_of, parent, created_at, the
    timestamp of the most recent analysis, per-bucket analysis counts (with a
    single ``other`` count for every foreign kind), a per-tier count breakdown,
    and an ``empty`` flag for arcs with zero records. Records with no arc are
    exposed under a distinct ``unattributed`` node, never omitted.

    Counts are DISTINCT on ``analysis_id``. The response ``app`` block carries the
    CAC identity from :mod:`arc_context` (never hardcoded).
    """
    projects_out: List[Dict[str, Any]] = []
    for project in store.list_projects():
        arc_nodes = [
            _arc_node(store, project.name, arc)
            for arc in store.list_arcs(project.name)
        ]
        projects_out.append(
            {
                "name": project.name,
                "arc_count": len(arc_nodes),
                "arcs": _nest_legs(arc_nodes),
            }
        )

    return {
        "app": {"id": APP_ID, "module": app_us(), "contract_version": CONTRACT_VERSION},
        "counts_are_distinct_on": "analysis_id",
        "projects": projects_out,
        "unattributed": _unattributed_node(store),
    }


# ── arc models ──────────────────────────────────────────────────────────────────


def _model_id_of(record: AnalysisRecord) -> Optional[str]:
    """Return a record's canonical model id, or ``None`` when it has no model.

    Grouping is STRICTLY by the model artifact (``artifacts.model_id``) — NEVER by
    the subject string, which drifts between producers and would duplicate or merge
    models wrongly (binding: GROUPING). The canonical grouping key is
    ``model:<ns>:<id>`` with ns in {kbdl, file}.

    The store validates every artifact VALUE as a URI (an absolute path,
    ``file://``, or ``obj://<id>`` — colons elsewhere are rejected), so the
    ``model:<ns>:<id>`` string cannot itself be an artifact value. The producer
    therefore stores the model as a valid artifact URI under key ``model_id`` and
    this derives the canonical id from it:

      * ``obj://<id>``                → ``model:kbdl:<id>``   (a run-database object)
      * an absolute path / ``file://`` → ``model:file:<path>`` (a file on disk)

    A record with no ``model_id`` artifact is not a model row and returns ``None``.
    """
    artifacts = record.artifacts or {}
    uri = artifacts.get("model_id")
    if not isinstance(uri, str) or not uri:
        return None
    if uri.startswith("obj://"):
        return "model:kbdl:" + uri[len("obj://") :]
    if uri.startswith("file://"):
        return "model:file:" + uri[len("file://") :]
    if uri.startswith("/"):
        return "model:file:" + uri
    # Any other shape (a store would have rejected it on write) — group by it
    # verbatim rather than dropping the record, so nothing silently disappears.
    return uri


def _display_name_of(record: AnalysisRecord) -> str:
    """Return a human-readable model/genome name (DISPLAY IS NOT IDENTITY).

    Group by model id, but DISPLAY the genome or model name — a table of
    ``model:file:/long/absolute/path.json`` is unreadable exactly when someone
    needs it. Prefers ``payload.model_name`` then ``payload.genome`` then
    ``subject``; the id stays available separately for copying.
    """
    payload = record.payload or {}
    for key in ("model_name", "genome", "subject_name"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return record.subject


def _is_inferred(record: AnalysisRecord) -> bool:
    """Return whether a record is flagged ``payload.provenance == "inferred"``."""
    payload = record.payload or {}
    return payload.get("provenance") == "inferred"


def _model_build_record(
    records: List[AnalysisRecord],
) -> Optional[AnalysisRecord]:
    """Pick the model-build record for a model group, if any.

    Prefers a record whose kind is a model-build kind (reconstruct / model_build);
    the group's build metadata (template, reaction/gene counts, display name) is
    read from it. Falls back to the first record so a group with only analysis
    runs still renders.
    """
    for rec in records:
        if rec.kind in MODEL_BUILD_KINDS:
            return rec
    return records[0] if records else None


def _int_or_none(value: Any) -> Optional[int]:
    """Coerce a payload value to int, or ``None`` when absent / not numeric."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _model_confusion_matrix(
    fitness_records: List[AnalysisRecord],
) -> Optional[ConfusionMatrix]:
    """Compute the per-model agreement matrix from its MEASURED fitness records.

    Uses only ``kbdl.fitness_analysis`` records (measured), never
    ``kbdl.fitness_prop`` (propagated) — substituting propagated for measured is
    the trust-tier laundering the design forbids. Returns ``None`` when the model
    has no measured fitness record (there is nothing to compare against).

    The prediction tier is the FLOOR across the contributing fitness records'
    tiers; the measurement side is verified. The matrix carries the floor of the
    two, labelled model-versus-experiment.
    """
    measured = [r for r in fitness_records if r.kind == FITNESS_ANALYSIS_KIND]
    if not measured:
        return None
    pairs = []
    tiers = []
    for rec in measured:
        pairs.extend(extract_gene_pairs_from_payload(rec.payload))
        tiers.append(_tier_of(rec))
    # Prediction tier is the floor of the model-side (fitness analysis) tiers; the
    # model prediction is at best hypothesis, so the floor rule keeps it there.
    from ..koros_arc_store import floor_tier

    prediction_tier = floor_tier(tiers) if tiers else "hypothesis"
    return compute_confusion_matrix(pairs, prediction_tier=prediction_tier)


def _model_row(
    store: Any,
    project: str,
    arc: str,
    model_id: str,
    latest_records: List[AnalysisRecord],
) -> Dict[str, Any]:
    """Build one model row: identity, build metadata, analyses, agreement matrix.

    ``latest_records`` are the arc's latest-only records that belong to this model
    (grouped by ``model_id``). One row per MODEL, not per record. The ``analyses``
    list is one line per ``analysis_id`` (the newest run), each carrying kind,
    status, timestamp, trust_tier, provenance, a ``runs`` count and the flag for
    ``payload.provenance == "inferred"``. Every run is dated in the expanded view,
    fetched per ``analysis_id`` from the store.
    """
    build = _model_build_record(latest_records)
    build_payload = (build.payload or {}) if build else {}

    analyses: List[Dict[str, Any]] = []
    for rec in latest_records:
        # Expanded per-run view: all runs of this analysis_id, dated (binding:
        # re-runs are kept, dated and grouped; counts are distinct on analysis_id).
        runs = store.list_analyses(project, arc, analysis_id=rec.analysis_id)
        analyses.append(
            {
                "analysis_id": rec.analysis_id,
                "kind": rec.kind,
                "status": rec.status,
                "created_at": rec.created_at,
                "trust_tier": _tier_of(rec),
                "provenance": rec.provenance or {},
                "inferred": _is_inferred(rec),
                "run_count": len(runs),
                "runs": [
                    {
                        "record_id": run.record_id,
                        "run_uid": run.run_uid,
                        "created_at": run.created_at,
                        "status": run.status,
                    }
                    for run in runs
                ],
            }
        )

    # Gapfill media: any gapfill record's payload media, collected across the group.
    gapfill_media: List[str] = []
    for rec in latest_records:
        media = (rec.payload or {}).get("gapfill_media")
        if isinstance(media, str) and media and media not in gapfill_media:
            gapfill_media.append(media)
        elif isinstance(media, list):
            for m in media:
                if isinstance(m, str) and m and m not in gapfill_media:
                    gapfill_media.append(m)

    matrix = _model_confusion_matrix(latest_records)

    # The row's trust tier is the FLOOR across its analyses — a summary never
    # rises above its least-trusted contributor.
    from ..koros_arc_store import floor_tier

    row_tier = (
        floor_tier([_tier_of(r) for r in latest_records])
        if latest_records
        else "hypothesis"
    )

    return {
        "model_id": model_id,
        "display_name": _display_name_of(build) if build else model_id,
        "subject": build.subject if build else None,
        "genome": build_payload.get("genome"),
        "template": build_payload.get("template"),
        "reaction_count": _int_or_none(build_payload.get("reaction_count")),
        "gene_count": _int_or_none(build_payload.get("gene_count")),
        "gapfill_media": gapfill_media,
        "trust_tier": row_tier,
        "inferred": any(_is_inferred(r) for r in latest_records),
        "analysis_count": len(latest_records),
        "counts_are_distinct_on": "analysis_id",
        "analyses": analyses,
        "agreement": matrix.to_dict() if matrix is not None else None,
    }


def build_arc_models(store: Any, project: str, arc: str) -> Dict[str, Any]:
    """Build the ``GET /api/arcs/{project}/{arc}/models`` response.

    One row per MODEL (grouped strictly by ``artifacts.model_id``), not one per
    record. Records with no ``model_id`` are counted under ``foreign`` (they are
    not model rows) so nothing is silently dropped. An arc with no records returns
    an empty ``models`` list rather than erroring.

    All counts are DISTINCT on ``analysis_id`` (``latest_only=True`` for the
    collapsed view); the expanded per-run view is fetched per ``analysis_id``.
    Answered from summary columns and payload alone — ``read_detail`` is never
    called.
    """
    latest = store.list_analyses(project, arc, latest_only=True)

    groups: Dict[str, List[AnalysisRecord]] = {}
    foreign = 0
    for rec in latest:
        model_id = _model_id_of(rec)
        if model_id is None:
            # A record with no model_id is not a model row. It is COUNTED
            # (never silently dropped) so a missing model_id is debuggable.
            foreign += 1
            continue
        groups.setdefault(model_id, []).append(rec)

    models = [
        _model_row(store, project, arc, model_id, records)
        for model_id, records in sorted(groups.items())
    ]

    return {
        "app": {"id": APP_ID, "module": app_us(), "contract_version": CONTRACT_VERSION},
        "project": project,
        "arc": arc,
        "counts_are_distinct_on": "analysis_id",
        "model_count": len(models),
        "foreign_record_count": foreign,
        "models": models,
    }
