"""The LAKEHOUSE-SOURCED clearinghouse publisher: a pinned snapshot plus a bundle in.

HOW THIS DIFFERS FROM ``genome.ontology_mapping.council_publish``, which has
the same basename and must not be confused with it: that module is
RUN-DIRECTORY-SOURCED -- it is the verb behind ``kbu ontomap council-publish``
and reads the verdict parquet a finished run's export wrote beside its
``PLAN.json``. THIS module is LAKEHOUSE-SOURCED: it reads the archived
deliberation out of ``kbaseincubator.council`` plus the campaign's publication
bundle, and reconstructs that same verdict parquet from durable data. Nothing
here needs the run directory, the scratch export, or the store file to still
exist. That is the whole point: the clearinghouse publish becomes reproducible
and re-runnable years later, and the SQLite store leaves the critical path
entirely.

IT WRITES NO LOADER AND NO SECOND MANIFEST FORMAT. Having built the rows, it
hands them to the EXISTING producer path -- ``render_manifest`` plus
``shard_plan`` plus ``shard_manifest``, driven by the shipped
``function-to-go-council`` preset -- and the consumer half stays exactly what
it already is: ``kbu clearinghouse load`` (fill-only, idempotent,
ledger-resumable, with an integrity gate and a ``--dry-run`` operator gate)
and ``kbu clearinghouse verify``. The NEW code here is the lakehouse READ, the
three-gate selection, the winner resolution and the guarded join. Everything
downstream is reuse.

THREE GATES, ALL REQUIRED, and only the first is this module's own:

1. ``query_name == 'go_mf'`` -- :func:`require_query_kind`. A council store is
   not GO-specific: ``query_name``/``query_version`` is the only council-kind
   discriminator anywhere in it, so publishing a job of another kind into
   ``function_result`` would assert GO molecular-function terms that no
   council ever selected.
2. the job is LISTED IN THE CAMPAIGN'S PUBLICATION BUNDLE; and
3. the job's disposition is ``published``.

Gates 2 and 3 are ``council_bundle.require_publishable`` -- the ONE two-gate
test in this codebase, which delegates gate 3 to
``council_disposition.require_published``. They are not re-implemented here:
neither gate implies the other, a second copy would be free to disagree, and a
job with NO disposition row must never read as published. The two cancelled
19,276-item jobs in the production store look authoritative by label, by size
and by status, and one of them carries 49 genuine but superseded verdicts.

PINNED TO ONE COMPLETED SNAPSHOT. Publication reads ONE ``snapshot_id``,
which must be marked complete in the archiver's load ledger
(``council_archive.require_complete_delivery``, again the only completeness
test there is). Never "whatever is current": that makes a re-publish
non-reproducible, and the nine namespace tables commit INDEPENDENTLY, so a
partial archive reads as a complete one to every query that does not join the
missing table.

WINNERS RESOLVE BY THE BUNDLE'S DECLARED ORDER -- ``ordered_job_ids`` --
never by ``parent_job_id`` (which does not totally order siblings and does not
separate campaigns), never by insertion order and never by latest timestamp.
The fold is ``verdicts.verdicts_by_item``, the same function the
run-directory exporter folds its jobs with, so "a later job supersedes an
earlier one for the same ``item_id``" means one thing in both paths.

THE KEY IS THE CLEARINGHOUSE ``entity_hash``, which arrives as the bundle's
``query_id`` and is re-canonicalised through
``clearinghouse_schema.encode_entity_hash``. NEVER a text join, and NEVER
``council_entity_hash``: the council's own per-item hash matched the
clearinghouse hash 0 of 19,268 times in the production run, so a loader
joining on it would load nothing and look like it had worked.

THE ZERO-JOIN GUARD RUNS BEFORE THE JOIN, NOT ON ITS RESULT, and that is the
subtle part rather than a detail. A relationship test evaluated over an empty
join passes VACUOUSLY -- "every joined row has a non-null key" and "the joined
rows all resolve" are both true of zero rows -- which is exactly how this
defect class survives having tests. :func:`guard_join` is therefore evaluated
on the SOURCE side (the bundle's submitted inventory) and on the council-side
key space read INDEPENDENTLY (which ``item_id``s actually carry an ``ok``
verdict), and it delegates to ``clearinghouse_ingest.join_assertions``, which
is the one implementation of those four assertions and reports a COUNT for
each. A non-empty input whose join would resolve nothing fails whatever the
coverage floor.

WHAT IS NOT TOUCHED. The 19,268 rows already loaded as
``result_type='go_council_mapping'`` are frozen by fill-only and cannot be
upgraded in place -- the clearinghouse slot key deliberately EXCLUDES
``result_type_version``, so a corrected payload in the same slot is silently
dropped as already-present. This module writes :data:`RESULT_TYPE`
(``go_molecular_function``), the preset's own result type, and issues no
Iceberg delete against anything.

NOTHING HERE IS VERIFIED AGAINST A LIVE BERDL POD. The transport has been
broken on kbhub since the 2026-10-19 image (KBUtilLib#91), and the load is the
consumer machine's step in any case. Everything in this module runs off-pod:
it reads a sink, writes a parquet and writes a shard directory. The LOAD is
reported as ``awaiting operator acceptance`` and never as success, and a
mocked load is never an acceptance.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .clearinghouse_ingest import (
    DEFAULT_COVERAGE_FLOOR,
    join_assertions,
    raise_for_join_report,
)
from .clearinghouse_schema import encode_entity_hash
from .council_archive import require_complete_delivery

__all__ = [
    "RESULT_TYPE",
    "ENTITY_TYPE",
    "TARGET_TABLE",
    "GATE_QUERY_NAME",
    "FROZEN_RESULT_TYPE",
    "GUARD_BATCH_ID",
    "CouncilPublishError",
    "WrongQueryKindError",
    "UnresolvedRosterError",
    "select_verdicts",
    "require_query_kind",
    "resolvable_query_ids",
    "guard_join",
    "lakehouse_export_rows",
    "write_publish_plan",
    "publish_from_lakehouse",
    "receipt",
]


#: The clearinghouse ``result_type`` this publisher writes. MUST equal the
#: ``@const`` in the shipped ``function-to-go-council`` preset's
#: ``[source.result]``; a test reads the preset and asserts the two agree.
#: Stated here rather than parsed out of the TOML because the guard needs the
#: slot tuple BEFORE a manifest is rendered -- the same reason
#: ``ontology_mapping.council_publish`` restates ``PUBLISH.json``'s filename
#: and pins it with a test.
RESULT_TYPE = "go_molecular_function"

#: The clearinghouse ``entity_type``. ``function``, not ``protein``: a council
#: verdict is a statement about a FUNCTION DESCRIPTION and the GO terms it
#: selected. Which proteins carry that function is a different join, in
#: ``protein_result``, and nothing here touches it.
ENTITY_TYPE = "function"

#: The one clearinghouse table this publisher writes.
TARGET_TABLE = "function_result"

#: The council kind gate -- the ONE gate this module adds itself.
GATE_QUERY_NAME = "go_mf"

#: The result type of the 19,268 rows ALREADY loaded, which are frozen and
#: must not be touched. Named so that a reader of this module can see that it
#: is a DIFFERENT value from :data:`RESULT_TYPE`, and so a test can assert
#: this publisher never emits it.
FROZEN_RESULT_TYPE = "go_council_mapping"

#: The batch id the pre-join guard reports under. The guard covers the WHOLE
#: submitted inventory in one evaluation rather than per shard batch, because
#: the thing being asserted is a property of the campaign's key set and the
#: sharder has not run yet.
GUARD_BATCH_ID = "council-publish-preflight"


class CouncilPublishError(RuntimeError):
    """This publisher will not publish."""


class WrongQueryKindError(CouncilPublishError):
    """A job named for publication is not of the required council kind.

    GATE 1, and the only one this module owns. ``query_name`` is the only
    council-kind discriminator in a council store -- nothing in the store is
    GO-specific -- so a job of another kind resolved into ``function_result``
    would publish GO molecular-function terms that no council selected. NAMES
    the job and the kind it actually carries.
    """


class UnresolvedRosterError(CouncilPublishError):
    """The run identity's ``roster_fingerprint`` could not be resolved.

    ``verdicts.export_rows`` requires it on every row and refuses a blank
    (:data:`~...verdicts.EXPORT_IDENTITY_FIELDS`): a published row that could
    not say which roster produced it cannot be reconciled against the
    parameter set it lands under. Raised rather than defaulted, because the
    cases that produce it -- gated jobs disagreeing about their roster, or an
    archive whose ``jobs.roster_fingerprint`` is NULL -- are both facts a
    human has to resolve.
    """


# --------------------------------------------------------------------------
# Reading the archive
# --------------------------------------------------------------------------


def _json_value(value: Any) -> Any:
    """One archived JSON column as Python, PRESERVING the JSON-null case.

    The archive reads the store's JSON columns as RAW TEXT, because the store
    writes a Python ``None`` into an ``sa.JSON`` column as the TEXT ``'null'``
    while a real SQL NULL is NULL, and a typed read collapses the two. So
    ``'null'`` parses to ``None`` here and a SQL NULL stays ``None`` -- the
    same answer by two different routes, which is correct for a reader that
    only asks "is there a verdict object".
    """
    if value is None:
        return None
    if isinstance(value, (Mapping, list)):
        return value
    text = str(value)
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _text(value: Any) -> str:
    """One archived value as text.

    The archived columns are declared ``STRING``, which gives a column of that
    name NUMERIC affinity in the local staging sink, so a digit-only value
    (``query_version`` is ``"4"`` in the production run) reads back as a
    number. Every comparison and every published cell goes through here.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _jobs_by_id(
    sink: Any, *, store_id: str, snapshot_id: str, job_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """The archived ``jobs`` rows for *job_ids*, keyed by id."""
    if not job_ids:
        return {}
    rows = sink.select_rows(
        "jobs",
        where={
            "store_id": store_id,
            "snapshot_id": snapshot_id,
            "job_id": sorted(set(job_ids)),
        },
    )
    return {_text(row["job_id"]): dict(row) for row in rows}


def require_query_kind(
    jobs: Mapping[str, Mapping[str, Any]],
    job_ids: Iterable[str],
    *,
    query_name: str = GATE_QUERY_NAME,
) -> list[str]:
    """GATE 1: every job is of council kind *query_name*, or refuse NAMING it.

    A job the archive does not hold under this ``(store_id, snapshot_id)`` is
    ALSO a refusal here rather than a pass: "the snapshot has no such job" and
    "the job is the wrong kind" both mean this campaign cannot resolve it, and
    treating an absent row as a satisfied gate is how an unpinned read gets in.

    Raises:
        WrongQueryKindError: a job is absent from the snapshot, or carries a
            different ``query_name``.
    """
    wanted = [str(job_id) for job_id in job_ids]
    problems: list[str] = []
    for job_id in wanted:
        row = jobs.get(job_id)
        if row is None:
            problems.append(
                f"{job_id}: NOT in this snapshot's archived jobs. A job the "
                "pinned snapshot does not hold cannot be resolved from it."
            )
            continue
        found = _text(row.get("query_name"))
        if found != query_name:
            problems.append(f"{job_id}: query_name is {found!r}, not {query_name!r}")
    if problems:
        raise WrongQueryKindError(
            f"refusing to publish: {len(problems)} of {len(wanted)} job(s) "
            f"are not council kind {query_name!r}.\n  " + "\n  ".join(problems)
            + "\n  query_name is the ONLY council-kind discriminator in a "
            "council store -- nothing in it is GO-specific -- so resolving a "
            f"job of another kind into {TARGET_TABLE} would publish GO "
            "molecular-function terms no council selected."
        )
    return wanted


def _resolve_roster_fingerprint(
    jobs: Mapping[str, Mapping[str, Any]],
    job_ids: Sequence[str],
    *,
    declared: str | None,
    plan: Mapping[str, Any],
) -> str:
    """The run identity's ``roster_fingerprint``, from the most specific source.

    In order: the caller's explicit value; the single distinct value the gated
    jobs carry; the plan's own, if it recorded one. A disagreement among the
    gated jobs is REFUSED rather than resolved by picking one -- two rosters
    in one campaign is a fact about the campaign, and publishing one of them
    on every row would misattribute half the verdicts.
    """
    if declared:
        return str(declared)
    found = sorted(
        {
            _text(jobs[job_id].get("roster_fingerprint"))
            for job_id in job_ids
            if job_id in jobs and _text(jobs[job_id].get("roster_fingerprint"))
        }
    )
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise UnresolvedRosterError(
            f"the gated jobs carry {len(found)} different "
            f"roster_fingerprint values ({found!r}). One campaign is one "
            "bench: publishing one of them on every row would misattribute "
            "the verdicts of the others. Pass roster_fingerprint= "
            "explicitly if this really is one campaign."
        )
    from_plan = _text(plan.get("roster_fingerprint"))
    if from_plan:
        return from_plan
    raise UnresolvedRosterError(
        "no roster_fingerprint is resolvable: the gated jobs' archived rows "
        "carry none and the bundle's plan records none. export_rows refuses "
        "a blank one, because a published row that cannot say which roster "
        "produced it cannot be reconciled against its parameter set."
    )


def select_verdicts(
    sink: Any,
    bundle: Mapping[str, Any],
    *,
    snapshot_id: str | None = None,
    state_root: Any = None,
    query_name: str = GATE_QUERY_NAME,
    roster_fingerprint: str | None = None,
) -> dict[str, Any]:
    """THE SELECTION: three gates, one pinned snapshot, winners by declared order.

    Args:
        sink: A council sink (see :mod:`council_sink`).
        bundle: The campaign's publication bundle (see
            ``genome.ontology_mapping.council_bundle``).
        snapshot_id: Override the snapshot the bundle names. Still ONE
            snapshot, and still checked for completeness.
        state_root: Root of the archiver's delivery state, for the
            completeness check.
        query_name: The council kind gate 1 requires.
        roster_fingerprint: Override the run identity's roster fingerprint.

    Returns:
        ``{"store_id", "snapshot_id", "ordered_job_ids", "verdicts",
        "identity", "completion", "jobs"}``. ``verdicts`` is the
        ``item_id -> settled item`` mapping ``verdicts.export_rows`` consumes.

    Raises:
        DeliveryIncompleteError: the pinned snapshot is not completely
            archived.
        WrongQueryKindError: gate 1.
        JobNotInBundleError: gate 2.
        JobNotPublishableError: gate 3.
    """
    from ...genome.ontology_mapping.council_bundle import ordered_job_ids, require_publishable
    from ...genome.ontology_mapping.verdicts import verdicts_by_item

    store_id = str(bundle["store_id"])
    pinned = str(snapshot_id or bundle["snapshot_id"])
    # PIN FIRST. Everything below reads at (store_id, pinned), so a snapshot
    # that is not completely archived is refused before a single row is read.
    completion = require_complete_delivery(
        sink, store_id=store_id, snapshot_id=pinned, state_root=state_root
    )

    ordered = ordered_job_ids(bundle)
    if not ordered:
        raise CouncilPublishError(
            f"the bundle for campaign {bundle.get('campaign_id')!r} lists no "
            "jobs in its winner order, so nothing resolves. An empty "
            "publication set is not 'everything is fine'."
        )
    jobs = _jobs_by_id(sink, store_id=store_id, snapshot_id=pinned, job_ids=ordered)

    # GATE 1 -- this module's own.
    require_query_kind(jobs, ordered, query_name=query_name)
    # GATES 2 and 3 -- the ONE two-gate test, which delegates gate 3 to
    # council_disposition.require_published. Not re-implemented here.
    require_publishable(sink, bundle, ordered, snapshot_id=pinned)

    # WINNERS BY THE BUNDLE'S DECLARED ORDER. Folded job by job in that
    # order, so a later job overwrites an earlier one's entry for the same
    # item_id -- which is how a retried item names its retry job.
    settled: dict[str, dict[str, Any]] = {}
    per_job: dict[str, int] = {}
    for job_id in ordered:
        rows = sink.select_rows(
            "items",
            where={"store_id": store_id, "snapshot_id": pinned, "job_id": job_id},
            order_by=("item_id",),
        )
        per_job[job_id] = len(rows)
        verdicts_by_item(
            [
                {
                    "item_id": _text(row["item_id"]),
                    "status": _text(row.get("status")),
                    "verdict": _json_value(row.get("verdict")) or {},
                    "decided_at": row.get("decided_at"),
                    "job_id": _text(row.get("job_id")) or job_id,
                    # Carried for the published ``function_text`` cell. The
                    # council store's items hold the entity text in
                    # ``evidence``; the bundle's fanout carries only the
                    # (query_id, item_id) pair.
                    "evidence": _json_value(row.get("evidence")),
                }
                for row in rows
            ],
            job_id=job_id,
            into=settled,
        )
    texts = _item_texts(sink, store_id=store_id, snapshot_id=pinned, job_ids=ordered)

    plan = dict(bundle.get("plan") or {})
    identity = {
        "query_name": _text(bundle.get("query_name")) or query_name,
        "query_version": _text(bundle.get("query_version")),
        "roster_fingerprint": _resolve_roster_fingerprint(
            jobs, ordered, declared=roster_fingerprint, plan=plan
        ),
        "mapping_hash": _text(bundle.get("mapping_hash")),
    }
    return {
        "store_id": store_id,
        "snapshot_id": pinned,
        "ordered_job_ids": ordered,
        "verdicts": settled,
        "item_texts": texts,
        "identity": identity,
        "completion": completion,
        "jobs": jobs,
        "items_read_per_job": per_job,
    }


def _item_texts(
    sink: Any, *, store_id: str, snapshot_id: str, job_ids: Sequence[str]
) -> dict[str, str]:
    """``{item_id: entity text}`` from the archived ``items.evidence``.

    The published ``function_text`` cell is the evidence of what was asked,
    and the bundle's fanout carries only ``(query_id, item_id)``. Read in
    declared job order so a later job's text supersedes an earlier one's for
    the same item, exactly as its verdict does.
    """
    out: dict[str, str] = {}
    for job_id in job_ids:
        for row in sink.select_rows(
            "items",
            where={"store_id": store_id, "snapshot_id": snapshot_id, "job_id": job_id},
            columns=("item_id", "evidence"),
            order_by=("item_id",),
        ):
            evidence = _json_value(row.get("evidence"))
            if isinstance(evidence, Mapping):
                text = evidence.get("text")
                if text is not None:
                    out[_text(row["item_id"])] = str(text)
    return out


# --------------------------------------------------------------------------
# The guard -- BEFORE the join, never on its result
# --------------------------------------------------------------------------


def resolvable_query_ids(
    bundle: Mapping[str, Any], verdicts: Mapping[str, Mapping[str, Any]]
) -> set[str]:
    """The campaign's key space that the COUNCIL SIDE can actually answer.

    Read INDEPENDENTLY of the join, which is the point: it is built from the
    archive's own verdict key space (which ``item_id``s settled ``ok``) plus
    the bundle's abstentions, and never from the rows a join returned. An
    abstention IS resolvable -- it publishes a row with ``council = null`` and
    its decline reason -- so counting it as unresolved would make the coverage
    number mean something else.
    """
    from ...genome.ontology_mapping.council_bundle import abstentions

    answered = {
        item_id
        for item_id, item in (verdicts or {}).items()
        if str((item or {}).get("status") or "") == "ok"
    }
    out = {str(row["query_id"]) for row in abstentions(bundle)}
    for pair in bundle.get("fanout") or ():
        if str(pair.get("item_id") or "") in answered:
            out.add(str(pair.get("query_id") or ""))
    return {query_id for query_id in out if query_id}


def guard_join(
    bundle: Mapping[str, Any],
    verdicts: Mapping[str, Mapping[str, Any]],
    *,
    parameter_set_hash: str,
    source: str,
    result_type: str = RESULT_TYPE,
    entity_type: str = ENTITY_TYPE,
    coverage_floor: float = DEFAULT_COVERAGE_FLOOR,
) -> dict[str, Any]:
    """The four pre-commit assertions, over the SOURCE side, with a count each.

    Delegates to ``clearinghouse_ingest.join_assertions`` -- the one
    implementation of these four -- by building the batch the campaign WOULD
    write: one row per submitted ``query_id``, carrying the real slot tuple
    ``(entity_hash, entity_type, result_type, source, parameter_set_hash)``.
    So "at most one source row per target slot" is asserted against the slot
    key the loader will actually use, not against a paraphrase of it.

    The four, each reported with its count:

    1. NON-EMPTY INPUT. A campaign that submitted nothing is a producer
       failure, not a clean load.
    2. NON-NULL KEYS. Every ``query_id`` is re-canonicalised through
       ``encode_entity_hash``, so a truncated, uppercased or empty key becomes
       a NULL key here and fails as a count -- rather than landing as a
       plausible wrong hash.
    3. AT MOST ONE SOURCE ROW PER TARGET SLOT.
    4. COVERAGE AT OR ABOVE THE FLOOR, against
       :func:`resolvable_query_ids`. The floor defaults to the EXISTING
       ``clearinghouse_ingest.DEFAULT_COVERAGE_FLOOR`` (1.0); no new tolerance
       policy is invented here. ZERO resolution is fatal whatever the floor --
       a non-empty input that resolves nothing is a WRONG JOIN KEY, not an
       idempotent no-op.

    NOTE for the measured production campaign: 19,276 query_ids were submitted
    and 8 never settled, so its coverage is 0.999585 and a publish of it at
    the default floor REFUSES, naming the eight. Lowering the floor is the
    operator's explicit act, exactly as ``--coverage-floor`` is on the load
    side; those eight residual items are the interesting number and a run that
    hid them would be the failure.

    Raises:
        ClearinghouseJoinAssertionError: any assertion failed. The message
            carries every count.
    """
    import pyarrow as pa

    from ...genome.ontology_mapping.council_bundle import bundle_query_ids

    submitted = [str(query_id) for query_id in bundle_query_ids(bundle)]
    keys: list[str | None] = []
    malformed: list[str] = []
    for query_id in submitted:
        try:
            keys.append(encode_entity_hash(query_id))
        except Exception:  # noqa: BLE001 - a bad key becomes a NULL key, counted
            # A malformed key is carried as NULL rather than dropped: dropping
            # it would shrink the input and could empty it, and assertion 2
            # exists to COUNT exactly this.
            keys.append(None)
            malformed.append(query_id)
    batch = pa.table(
        {
            "entity_hash": pa.array(keys, type=pa.string()),
            "entity_type": pa.array([entity_type] * len(keys), type=pa.string()),
            "result_type": pa.array([result_type] * len(keys), type=pa.string()),
            "source": pa.array([str(source)] * len(keys), type=pa.string()),
            "parameter_set_hash": pa.array(
                [str(parameter_set_hash)] * len(keys), type=pa.string()
            ),
        }
    )
    resolvable = {
        encode_entity_hash(query_id)
        for query_id in resolvable_query_ids(bundle, verdicts)
        if query_id in set(submitted) and query_id not in set(malformed)
    }
    report = join_assertions(
        batch,
        table=TARGET_TABLE,
        batch_id=GUARD_BATCH_ID,
        resolvable=resolvable,
        coverage_floor=coverage_floor,
    )
    report = {
        **report,
        "submitted_query_ids": len(submitted),
        "distinct_submitted_query_ids": len(set(submitted)),
        "malformed_query_ids": malformed[:10],
        "malformed_query_id_count": len(malformed),
        "evaluated": "source-side, before the join",
    }
    # Raised through the ingest path's own raiser, so the exception type a
    # caller catches is the same one the load side raises for the same four.
    raise_for_join_report(report)
    return report


# --------------------------------------------------------------------------
# The rows
# --------------------------------------------------------------------------


def lakehouse_export_rows(
    bundle: Mapping[str, Any],
    selection: Mapping[str, Any],
    *,
    texts: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build the export rows -- ``verdicts.EXPORT_COLUMNS`` shape, from the lake.

    THE SHAPE IS NOT RE-DERIVED HERE. ``verdicts.export_rows`` builds it,
    including every ``council`` cell through ``verdicts.council_reference``
    and ``council = null`` for every retrieval abstention, so the
    lakehouse-sourced export is byte-compatible with what the run-directory
    exporter writes. A second row builder would be free to drift from it, and
    the clearinghouse slot it writes into is frozen on first write.

    ``function_text`` FOR AN ABSTENTION IS NOT IN THE LAKEHOUSE. A retrieval
    abstention has no council item, so no ``items.evidence`` holds its text,
    and the bundle carries only its ``query_id`` and decline reason. Decided
    rows get their text from ``items.evidence``; an abstention's is ``''``
    unless *texts* supplies it. That difference is REPORTED in the receipt
    (``abstentions_without_text``) rather than smoothed over, because the slot
    is frozen on first write and an empty text published once stays empty.

    Args:
        bundle: The campaign's bundle.
        selection: :func:`select_verdicts`'s return value.
        texts: ``{query_id: function text}``, for abstentions (and as an
            override for decided rows). A caller holding the run's ranked or
            abstain file passes it.

    Returns:
        ``verdicts.export_rows``'s ``{"rows", "counts"}``, plus
        ``abstentions_without_text``.
    """
    from ...genome.ontology_mapping.council_bundle import abstentions
    from ...genome.ontology_mapping.verdicts import export_rows

    by_query = {str(k): str(v) for k, v in dict(texts or {}).items()}
    item_texts = dict(selection.get("item_texts") or {})

    abstain_rows = []
    missing_text: list[str] = []
    for row in abstentions(bundle):
        query_id = str(row["query_id"])
        text = by_query.get(query_id, "")
        if not text:
            missing_text.append(query_id)
        abstain_rows.append(
            {
                "query_id": query_id,
                "text": text,
                "abstain_reason": row.get("abstain_reason"),
                "decline_reason": row.get("decline_reason"),
            }
        )
    fanout_rows = [
        {
            "query_id": str(pair["query_id"]),
            "item_id": str(pair["item_id"]),
            "text": by_query.get(
                str(pair["query_id"]), item_texts.get(str(pair["item_id"]), "")
            ),
        }
        for pair in bundle.get("fanout") or ()
    ]

    built = export_rows(
        abstain=abstain_rows,
        fanout=fanout_rows,
        verdicts=selection["verdicts"],
        identity=selection["identity"],
        job_ids=selection["ordered_job_ids"],
        retry_job_ids=[str(jid) for jid in bundle.get("retry_job_ids") or ()],
        abstained_at=str(
            (bundle.get("plan") or {}).get("generated_at") or ""
        ) or None,
        store_id=selection["store_id"],
    )
    return {**built, "abstentions_without_text": missing_text}


# --------------------------------------------------------------------------
# The hand-off
# --------------------------------------------------------------------------


def write_publish_plan(bundle: Mapping[str, Any], publish_dir: Any) -> Path:
    """Write the bundle's own ``PLAN.json`` into *publish_dir*, or verify it.

    The existing producer path reads the run identity out of ``PLAN.json``,
    and the bundle CARRIES that plan whole -- which is what lets a re-publish
    need no run directory. Written here rather than requiring the original
    one, and an existing file with DIFFERENT content is refused rather than
    overwritten: the parameter-set hash in it is the partition key, and
    silently replacing it would publish under a partition the receipt beside
    it does not describe.

    Raises:
        CouncilPublishError: the bundle carries no plan, or *publish_dir*
            holds a different one.
    """
    from ...ai.council.quote import PLAN_FILENAME

    plan = dict(bundle.get("plan") or {})
    if not plan:
        raise CouncilPublishError(
            f"the bundle for campaign {bundle.get('campaign_id')!r} carries "
            "no plan, so the run identity -- the query reference, the two "
            "digests and the parameter_set_hash -- is unknown. That identity "
            "IS the partition key; it is not derivable from the archive."
        )
    target = Path(publish_dir).expanduser() / PLAN_FILENAME
    text = json.dumps(plan, indent=2, sort_keys=True, default=str) + "\n"
    if target.is_file():
        existing = target.read_text(encoding="utf-8")
        if existing != text:
            raise CouncilPublishError(
                f"refusing to replace {target}: it holds a DIFFERENT plan "
                "from the one this bundle carries. The parameter_set_hash in "
                "it is the partition key, and a publish record beside a "
                "replaced plan would describe a partition nothing wrote."
            )
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    partial.write_text(text, encoding="utf-8")
    partial.replace(target)
    return target


def publish_from_lakehouse(
    sink: Any,
    bundle: Mapping[str, Any],
    *,
    publish_dir: Any,
    verdicts_path: Any = None,
    snapshot_id: str | None = None,
    state_root: Any = None,
    query_name: str = GATE_QUERY_NAME,
    roster_fingerprint: str | None = None,
    texts: Mapping[str, str] | None = None,
    coverage_floor: float = DEFAULT_COVERAGE_FLOOR,
    target_bytes: int | None = None,
) -> dict[str, Any]:
    """Gate, guard, rebuild and shard one campaign -- the whole producer half.

    THE ORDER IS THE DESIGN:

    1. :func:`select_verdicts` -- pin the snapshot (refusing an incomplete
       one), apply all three gates, resolve winners by the bundle's declared
       order.
    2. :func:`guard_join` -- the four assertions, on the SOURCE side, BEFORE
       anything is joined or written. A non-empty input that resolves nothing
       stops here.
    3. :func:`lakehouse_export_rows` -- the join, through
       ``verdicts.export_rows``.
    4. ``verdicts.write_verdict_parquet`` -- the same artefact the
       run-directory exporter writes, with the same declared schema.
    5. ``genome.ontology_mapping.council_publish.council_publish`` -- the
       EXISTING render + plan-gate + shard path and the shipped preset. No new
       manifest format and no new loader.
    6. the receipt.

    THE LOAD IS NOT PERFORMED AND IS NOT CLAIMED. Steps 1-5 all run off-pod.
    ``kbu clearinghouse load`` and ``kbu clearinghouse verify`` are the
    consumer machine's verbs; the receipt reports the load as AWAITING
    OPERATOR ACCEPTANCE and names the two commands.

    Args:
        sink: A council sink.
        bundle: The campaign's publication bundle.
        publish_dir: Where ``PLAN.json``, ``MANIFEST.toml``, ``PUBLISH.json``
            and ``shard/`` go. Written from the bundle, so no original run
            directory is needed.
        verdicts_path: Where to write the rebuilt verdict parquet. Default:
            ``<publish_dir>/verdicts.parquet``.
        snapshot_id: Override the snapshot the bundle names.
        state_root: Root of the archiver's delivery state.
        query_name: Gate 1's required council kind.
        roster_fingerprint: Override the run identity's roster fingerprint.
        texts: ``{query_id: function text}`` -- see
            :func:`lakehouse_export_rows`.
        coverage_floor: The guard's floor. Defaults to the existing
            ``DEFAULT_COVERAGE_FLOOR`` of 1.0.
        target_bytes: Target uncompressed bytes per shard batch.

    Returns:
        The receipt (see :func:`receipt`), including ``publish`` -- the
        existing publish record -- and ``guard``.
    """
    from ...genome.ontology_mapping.council_publish import council_publish
    from ...genome.ontology_mapping.verdicts import write_verdict_parquet

    out = Path(publish_dir).expanduser()
    selection = select_verdicts(
        sink,
        bundle,
        snapshot_id=snapshot_id,
        state_root=state_root,
        query_name=query_name,
        roster_fingerprint=roster_fingerprint,
    )
    plan = dict(bundle.get("plan") or {})
    guard = guard_join(
        bundle,
        selection["verdicts"],
        parameter_set_hash=str(bundle.get("parameter_set_hash") or ""),
        source=str(plan.get("source") or f"council/{bundle.get('query') or ''}"),
        coverage_floor=coverage_floor,
    )
    built = lakehouse_export_rows(bundle, selection, texts=texts)
    parquet = Path(verdicts_path).expanduser() if verdicts_path else out / "verdicts.parquet"
    written = write_verdict_parquet(built["rows"], parquet)
    write_publish_plan(bundle, out)
    published = council_publish(
        out,
        verdicts=parquet,
        target_bytes=target_bytes,
    )
    return receipt(
        bundle,
        selection=selection,
        guard=guard,
        built=built,
        parquet=written,
        publish=published,
    )


def receipt(
    bundle: Mapping[str, Any],
    *,
    selection: Mapping[str, Any],
    guard: Mapping[str, Any],
    built: Mapping[str, Any],
    parquet: Mapping[str, Any],
    publish: Mapping[str, Any],
) -> dict[str, Any]:
    """The publish receipt, RECONCILED ON DISTINCT ``query_id``s.

    ITEMS, RESULT ROWS AND ``query_id``s ARE THREE DIFFERENT NUMBERS and the
    receipt says all three. "Loaded 19,268" is not a complete reconciliation
    when 19,276 were submitted: the eight residual inputs are the interesting
    number and a receipt that omitted them would hide them. One item can carry
    several GO terms, and one item can fan out to several ``query_id``s, so a
    ``function_result`` row count is neither an item count nor a ``query_id``
    count.

    RECONCILED ON DISTINCT ``query_id``s, because summing across jobs
    double-counts every retried item: the retry jobs re-run a subset of the
    chunks, which is how a previous manifest reported 19,405 items where the
    truth over unique functions is 19,276 submitted and 19,268 decided.

    THE TARGET TABLE'S SNAPSHOT ID IS NOT KNOWN HERE and is reported as
    ``None`` with its reason. It is assigned by the Iceberg commit that
    ``kbu clearinghouse load`` makes IN THE POD; this half of stage 5 runs
    off-pod and claiming a value for it would be inventing one.
    """
    from ...genome.ontology_mapping.council_bundle import abstentions, bundle_query_ids

    submitted = {str(query_id) for query_id in bundle_query_ids(bundle)}
    rows = list(built.get("rows") or ())
    with_verdict = {str(row["query_id"]) for row in rows}
    resolvable = resolvable_query_ids(bundle, selection["verdicts"])
    unresolved = sorted(submitted - with_verdict)
    declines: dict[str, int] = {}
    for row in rows:
        reason = row.get("decline_reason")
        if reason:
            declines[str(reason)] = declines.get(str(reason), 0) + 1
    counts = dict(publish.get("counts") or {})
    return {
        "schema_version": 1,
        "campaign_id": str(bundle.get("campaign_id") or ""),
        "bundle_digest": str(bundle.get("bundle_digest") or ""),
        "store_id": selection["store_id"],
        "snapshot_id": selection["snapshot_id"],
        "ordered_job_ids": list(selection["ordered_job_ids"]),
        "result_type": RESULT_TYPE,
        "entity_type": ENTITY_TYPE,
        "target_table": TARGET_TABLE,
        # The three numbers, reconciled on DISTINCT query_ids.
        "query_ids_submitted": len(submitted),
        "query_ids_with_verdict": len(with_verdict),
        "query_ids_unresolved": len(unresolved),
        "unresolved_query_ids": unresolved[:50],
        "query_ids_resolvable": len(resolvable),
        "reconciled": len(with_verdict) + len(unresolved) == len(submitted),
        "reconciled_on": "distinct query_id",
        # ROWS, which are neither items nor query_ids.
        "export_rows": len(rows),
        "result_rows_written": counts.get("result_rows_sharded"),
        "parquet": dict(parquet),
        # ITEMS, which the bundle's submitted inventory counts and the row
        # count cannot.
        "items_submitted": (bundle.get("submitted") or {}).get("counts", {}).get("items"),
        "inputs_submitted": (bundle.get("submitted") or {}).get("counts", {}).get("inputs"),
        "abstentions": len(abstentions(bundle)),
        "abstentions_without_text": list(built.get("abstentions_without_text") or ()),
        "declines_by_reason": declines,
        "export_counts": dict(built.get("counts") or {}),
        "parameter_set_hash": str(bundle.get("parameter_set_hash") or ""),
        "guard": dict(guard),
        "publish": dict(publish),
        "frozen_result_type_untouched": FROZEN_RESULT_TYPE,
        # THE POD HALF. Named, never claimed.
        "target_table_snapshot_id": None,
        "load": {
            "state": "awaiting operator acceptance",
            "why": (
                "the Iceberg commit that assigns the target table's snapshot "
                "id is made by `kbu clearinghouse load` IN THE POD. This half "
                "of stage 5 runs off-pod and nothing here has been verified "
                "against a live BERDL pod (KBUtilLib#91); a mocked load is "
                "never an acceptance."
            ),
            "load": (publish.get("hand_off") or {}).get("load"),
            "verify": (publish.get("hand_off") or {}).get("verify"),
        },
    }
