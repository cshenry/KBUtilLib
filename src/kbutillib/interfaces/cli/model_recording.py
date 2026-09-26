"""Analysis-record stamping for the ``kbu model`` verbs.

The four modeling verbs (``reconstruct``, ``gapfill``, ``fba``, ``fva``) each
record one :class:`~kbutillib.koros_arc_store.AnalysisRecord` onto the KOROS arc
they run in.  Recording is a SIDE EFFECT on stderr and the per-user run
database, never on stdout: the ``--json`` payload of every verb must stay
byte-identical to the base commit, so the caller emits its JSON first and only
then calls :func:`record_model_analysis` (S17 hook point).

Identity is NOT derived here.  ``analysis_id`` and ``record_id`` come from
:func:`kbutillib.koros_arc_store.derive_analysis_id` /
:func:`~kbutillib.koros_arc_store.derive_record_id` — the shared helpers exist so
two independent producers agree, and re-deriving locally is exactly the failure
they prevent (confront round 3, T1).  No ``sha256`` over kind/subject/params/
run_uid appears in this module.

Trust tier is ``hypothesis`` for all four verbs, without exception: the CAC
defines ``hypothesis`` as "KOROS/model-generated (argued, not proven)", and a
reconstruction, a gapfilled reaction, an FBA flux and an FVA range are all
model-generated.  This is a contract requirement, not a per-run judgement — the
anti-laundering rule (CROSS_APP_COMMUNICATION.md section A) forbids model output
ever being restated at a higher tier.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from kbutillib import arc_context
from kbutillib.koros_arc_store import (
    CONTRACT_VERSION,
    AnalysisRecord,
    KorosArcStore,
    KorosArcStoreError,
    RunsRootResolutionError,
    derive_analysis_id,
    derive_record_id,
)

# Environment variable carrying the ONE run_uid minted per process.  Exported so
# subprocesses (e.g. a script driven from another CLI) inherit it: one CLI
# process invocation is one run (Chris, 2026-09-24), and a run that spawns work
# must not fragment into several run_uids.
RUN_UID_ENV = "KBU_RUN_UID"

# All four verbs are model-generated, so the tier is fixed (CAC section A).
_MODEL_TRUST_TIER = "hypothesis"

# The CAC bridge kind for model-generated evidence.  ``model_prediction`` is the
# only one of the five documented bridge kinds that names model output; the store
# rejects any tier other than ``verified`` whose provenance lacks a valid
# ``bridge_kind`` and ``metric`` (S26).
_MODEL_BRIDGE_KIND = "model_prediction"

# Normative per-kind payload schema (confront round 2). A writer emitting an
# unlisted key or omitting a listed one must FAIL rather than write a record the
# reader cannot interpret. The shared store is domain-agnostic and holds no
# per-kind map (identity.py:103), so this contract is enforced here, in the
# writer, which is where the task body assigns it.
_PAYLOAD_SCHEMA: Dict[str, frozenset] = {
    "kbutillib.reconstruct": frozenset(
        {"template", "n_reactions", "n_genes", "atp_safe"}
    ),
    "kbutillib.gapfill": frozenset({"media", "objective", "reactions_added"}),
    "kbutillib.fba": frozenset({"media", "objective", "objective_value"}),
    "kbutillib.fva": frozenset({"media", "fraction_of_optimum"}),
}

_PRODUCER = arc_context.APP_ID


def _validate_payload_schema(kind: str, payload: Dict[str, Any]) -> None:
    """Reject a payload whose keys do not match the normative per-kind schema.

    Raises :class:`ValueError` when *kind* is one of the four modeling kinds and
    *payload* has an unlisted or missing key. A kind not in the schema map is not
    this writer's concern and is left alone.
    """
    expected = _PAYLOAD_SCHEMA.get(kind)
    if expected is None:
        return
    actual = frozenset(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(
            f"payload for {kind} violates the normative schema: "
            f"missing={missing} unlisted={extra}"
        )


def mint_run_uid() -> str:
    """Mint the process run_uid ONCE and export it, returning the cached value.

    One CLI process invocation is one run: the first call generates a uuid4,
    stores it in :data:`RUN_UID_ENV` so every subsequent call in this process
    (and any subprocess that inherits the environment) returns the SAME value.
    A retry of a run reuses its run_uid (replace); a genuine re-run is a fresh
    process, a fresh uid, a new row under the same analysis_id.

    Call this once from the CLI entry point (the ``kbu`` group callback), not
    per subcommand — minting per analysis would make "delete that run" stop
    meaning what a scientist means by it.
    """
    existing = os.environ.get(RUN_UID_ENV)
    if existing:
        return existing
    run_uid = uuid.uuid4().hex
    os.environ[RUN_UID_ENV] = run_uid
    return run_uid


def _run_uid() -> str:
    """Return the process run_uid, minting it if the entry point did not.

    The entry point mints it, but a direct unit-test call or a stray code path
    must still get a stable per-process value rather than an empty run_uid the
    store would reject with ``missing_run_uid``.
    """
    return mint_run_uid()


def _db_timestamp() -> str:
    """Return now as ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (the run-db timestamp shape).

    NOT :func:`kbutillib.interfaces.cli.manifest.now_utc_iso` — that omits the
    microseconds the run database's timestamp validation requires.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _provenance(bridge_metric: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Build a store-valid provenance dict for a model-generated record.

    The store requires (S26) a ``bridge_kind`` naming the bridge and a
    ``metric`` dict with ``name`` and ``value``.  The reasoning/arc provenance
    the tier demands is carried alongside under ``evidence`` — it is non-empty
    for every one of the four verbs, which is the contract this task asserts.
    """
    provenance: Dict[str, Any] = {
        "bridge_kind": _MODEL_BRIDGE_KIND,
        "metric": bridge_metric,
    }
    provenance.update(evidence)
    return provenance


def record_model_analysis(
    *,
    kind: str,
    subject: str,
    significant_params: Dict[str, Any],
    payload: Dict[str, Any],
    artifacts: Dict[str, str],
    provenance_evidence: Dict[str, Any],
    bridge_metric: Dict[str, Any],
    status: str,
    arc_explicit: Optional[str],
) -> None:
    """Stamp one analysis record onto the resolved KOROS arc, fail-soft.

    Resolves the arc with :func:`arc_context.resolve_current_arc` (passing
    *arc_explicit* straight through).  When it returns ``None`` NO record is
    written: the reason is surfaced via :func:`arc_context.warn_not_indexed` and
    the verb completes normally.  When it resolves, an :class:`AnalysisRecord` is
    built with the SHARED identity helpers and written via
    :meth:`KorosArcStore.record_analysis`, whose write path is itself fail-soft
    (a locked/absent/read-only database degrades to a warning).

    This never raises for a recording problem — recording is a side effect the
    user never asked for and must never fail a pipeline run.  Structural
    validation errors from the store (a caller bug) are the one thing allowed to
    surface, and they cannot arise from these four callers because the record
    shape is fixed here.
    """
    # A payload-schema violation is a CALLER BUG, not a runtime/environment
    # failure, so it raises rather than degrading to a warning — the same posture
    # the store takes for its own structural validation. It cannot arise from the
    # four real callers (their payloads are shaped to the schema); the gate exists
    # so a future edit that drifts the payload fails loudly instead of writing a
    # record the reader cannot interpret.
    _validate_payload_schema(kind, payload)

    resolved = arc_context.resolve_current_arc(arc_explicit)
    if resolved is None:
        arc_context.warn_not_indexed(
            "no KOROS arc resolved (pass --arc, set KOROS_ARC, or run inside "
            "an arc directory)"
        )
        return
    project, arc = resolved

    _write_record(
        project=project,
        arc=arc,
        kind=kind,
        subject=subject,
        significant_params=significant_params,
        payload=payload,
        artifacts=artifacts,
        provenance=_provenance(bridge_metric, provenance_evidence),
        status=status,
    )


def _write_record(
    *,
    project: Optional[str],
    arc: Optional[str],
    kind: str,
    subject: str,
    significant_params: Dict[str, Any],
    payload: Dict[str, Any],
    artifacts: Dict[str, str],
    provenance: Dict[str, Any],
    status: str,
) -> None:
    """Derive identity, build the record and write it — every failure fail-soft."""
    try:
        run_uid = _run_uid()
        analysis_id = derive_analysis_id(kind, subject, significant_params)
        record_id = derive_record_id(analysis_id, run_uid)
        record = AnalysisRecord(
            record_id=record_id,
            analysis_id=analysis_id,
            run_uid=run_uid,
            kind=kind,
            created_at=_db_timestamp(),
            producer=_PRODUCER,
            producer_version=_producer_version(),
            subject=subject,
            status=status,
            artifacts=artifacts,
            payload=payload,
            trust_tier=_MODEL_TRUST_TIER,
            provenance=provenance,
            contract_version=CONTRACT_VERSION,
        )
        store = KorosArcStore()
        store.record_analysis(project, arc, record)
    except RunsRootResolutionError as exc:
        # The arc resolved but the store cannot open the runs root now: treat as
        # not-indexed rather than crashing the verb.
        arc_context.warn_not_indexed(f"runs root unresolvable at write time: {exc}")
    except KorosArcStoreError as exc:
        # Any other store-level error (never the fail-soft write path, which
        # returns) — surface as a warning, never fail the run.
        arc_context.warn_not_indexed(f"analysis record rejected: {exc}")


def _producer_version() -> str:
    """Return the KBUtilLib version stamped as the producer_version."""
    try:
        import kbutillib

        return str(getattr(kbutillib, "__version__", "unknown"))
    except Exception:  # pragma: no cover - defensive
        return "unknown"


# ── canonical model identity (S8 / artifact vocabulary) ─────────────────────


def model_id_for_path(model_path: str) -> str:
    """Return the ``artifacts.model_id`` ref for a local cobra-JSON model.

    Canonical model identity is ``model:<ns>:<id>`` with ``ns in {kbdl, file}``;
    for a cobra JSON on disk the namespace is ``file`` and the id is the absolute
    normalised path.  The store's artifact-URI vocabulary (S8/S19) is the
    authority on what an artifact REF may be — a bare absolute path, ``file://``
    or ``obj://<id>``, and NOTHING else — so a ``file``-namespace model_id is
    carried as that absolute path (the path IS the id).  A KBDL-backed model
    would carry ``obj://<object_id>`` here instead; these CLI verbs only produce
    file-backed cobra JSON, so only the ``file`` form arises.
    """
    return str(Path(model_path).resolve())


def file_uri(path: str) -> str:
    """Return the bare absolute path artifact ref for a local file (S19)."""
    return str(Path(path).resolve())
