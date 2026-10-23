"""The verdict export: one row per INPUT ``query_id``, as parquet.

Stage 5 of the standing ``function_to_go`` pipeline asks one question -- *what
did this run decide about every function it was given* -- and the answer is a
join of three things stage 1 and stage 4 already wrote:

* **abstain** -- ``abstain_file`` from :meth:`~.ontology_mapping_utils.OntologyMappingUtils.council_plan`,
  one row per un-run function RETRIEVAL declined, already carrying
  ``decline_reason`` in the ``"abstained:<reason>"`` spelling.
* **fanout** -- ``fanout_file`` from the same stage: the complete
  ``query_id -> item_id`` map for every un-run, non-declined function. A text
  shared by three ``query_id``s was answered ONCE and is published three
  times, which is why the fanout file exists and the submit file is not what
  this stage reads.
* **verdicts** -- the council store's own item rows, keyed by ``item_id``.

THIS MODULE IS PURE. No store, no clearinghouse, no numpy -- the same split,
and for the same reason, as :mod:`.unrun`: :mod:`.ontology_mapping_utils`
imports :mod:`.go_text` (numpy / scikit-learn / joblib), so a test of the row
derivation living there would be SKIPPED on every host without the retrieval
stack, and the row derivation is precisely the part worth regression-testing.
:func:`write_verdict_parquet` is the single exception, and it imports pyarrow
INSIDE the function so this module stays importable without it.

THE JOIN KEY IS ``query_id``, which is the clearinghouse's own ``entity_hash``
carried through from the ranked file into the fanout and abstain records.
Measured 2026-10-21: the council's own ``items.entity_hash`` matched 0 of
19,268 clearinghouse hashes while the definition TEXT matched 19,268 of
19,268. The texts travel on these rows as ``function_text`` -- the published
payload, never the key -- and nothing here ever joins on one.

WHAT IS WRITTEN AND WHAT IS WITHHELD. Every input ``query_id`` gets a row,
including all three decline classes:

* ``abstained:<reason>`` -- retrieval declined. Cheap to produce, and 760 of
  them went missing once by being dropped here.
* ``no_candidate_matched`` -- the council looked and the right term was never
  on the menu. The completed run had 4,893 of these, each a full five-seat
  pass.
* ``contentless`` -- the council found no functional content in the text.

All three are written with ``go_ids = []``. Recording them is what stops stage
1 re-selecting them forever: an ABSENT row is indistinguishable from "not yet
run", because un-run is defined as the absence of a current-state result row.

The one thing WITHHELD is an item with no verdict -- every call failed, or it
never settled. That is not a result; it is genuinely un-run, and the next
plan must re-select it. :func:`export_rows` counts those in
``counts["withheld"]`` so ``rows + withheld == inputs`` is checkable rather
than inferred.

PARQUET, NOT JSONL AND NOT GZIPPED JSONL, for three reasons that are all
checkable in the chain that consumes this file:
``FileSourceAdapter._format()`` accepts exactly ``parquet | jsonl | tsv`` and
``FileSourceAdapter._iter_jsonl`` opens with a plain ``path.open("r")``, so
gzipped JSONL is not readable by the manifest path at all;
``FileSourceAdapter.estimate_rows()`` is EXACT from a parquet footer, where it
deliberately returns ``None`` for text; and
:func:`~kbutillib.domains.kbase.berdl.clearinghouse_ingest.read_batch_table`
refuses anything but parquet downstream.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from kbutillib.domains.ai.council.engine.decide import content_flag

__all__ = [
    "EXPORT_COLUMNS",
    "EXPORT_IDENTITY_FIELDS",
    "DECLINE_CLASSES",
    "export_rows",
    "export_schema",
    "no_functional_content",
    "verdicts_by_item",
    "write_verdict_parquet",
]

#: Every column of the exported parquet, in write order. The clearinghouse
#: manifest preset names columns out of this list, so it is declared once and
#: :func:`export_schema` builds the pyarrow schema from it -- two column lists
#: could disagree, and the one the shard read would be whichever it happened
#: to open.
EXPORT_COLUMNS: Tuple[str, ...] = (
    "query_id",
    "function_text",
    "genomes",
    "go_ids",
    "go_confidences",
    "final_confidence",
    "decided_by",
    "no_functional_content",
    "decline_reason",
    "n_valid_proposers",
    "votes",
    "decided_at",
    "query_name",
    "query_version",
    "roster_fingerprint",
    "mapping_hash",
    "job_ids",
    "retried",
)

#: The RUN IDENTITY columns -- the same value on every row of one export.
#: ``roster_fingerprint`` is PROVENANCE and not a key: the bench partitions
#: the corpus and the transport does not, so a seat re-routed to a different
#: gateway must not re-partition all 47,341 clearinghouse functions (see
#: :func:`~kbutillib.domains.ai.council.types.bench_fingerprint`). It rides
#: along here so a published row can still say which roster produced it.
EXPORT_IDENTITY_FIELDS: Tuple[str, ...] = (
    "query_name",
    "query_version",
    "roster_fingerprint",
    "mapping_hash",
)

#: The three decline classes, each written as a row with ``go_ids = []``.
#: ``abstained`` is a PREFIX -- the published value is
#: ``"abstained:<reason>"`` -- because the retrieval reason (a placeholder
#: function, or no candidate surviving the GO height filter) is the whole
#: value of recording the abstention at all.
DECLINE_CLASSES: Tuple[str, ...] = (
    "abstained",
    "no_candidate_matched",
    "contentless",
)

#: Stated confidences, weakest first. Used to break a tie in
#: :func:`_modal_confidence` toward the LOWER value.
_CONFIDENCE_ORDER: Tuple[str, ...] = ("low", "medium", "high")


# ---------------------------------------------------------------------------
# The row derivation
# ---------------------------------------------------------------------------


def export_rows(
    *,
    abstain: Iterable[Mapping[str, Any]],
    fanout: Iterable[Mapping[str, Any]],
    verdicts: Mapping[str, Mapping[str, Any]],
    identity: Mapping[str, Any],
    job_ids: Sequence[str] = (),
    abstained_at: Optional[str] = None,
    genomes: Optional[Mapping[str, Sequence[str]]] = None,
    retry_job_ids: Sequence[str] = (),
) -> Dict[str, Any]:
    """Build the export's rows, one per input ``query_id``.

    Pure: no store, no clearinghouse, no I/O.

    Args:
        abstain: the plan's ``abstain_file`` rows -- ``{"query_id", "text",
            "source", "abstain_reason", "decline_reason"}``. ``decline_reason``
            is already in the ``"abstained:<reason>"`` spelling and is used as
            written rather than re-derived here.
        fanout: the plan's ``fanout_file`` rows -- ``{"query_id", "item_id",
            "text"}``. One row per un-run non-declined function, so a text
            shared by three ``query_id``s yields three export rows from one
            paid answer.
        verdicts: ``item_id -> {"status", "verdict", "decided_at",
            "job_id"}``, as :func:`verdicts_by_item` reduces a council store
            read. An ``item_id`` absent from this mapping, or present with a
            status other than ``"ok"``, is WITHHELD: it has no verdict, so it
            is not a result and the next plan must re-select it.
        identity: the run identity -- :data:`EXPORT_IDENTITY_FIELDS`.
        job_ids: every job id this export read, written on every row.
        abstained_at: ``decided_at`` for an abstained row -- the time
            retrieval declined it, i.e. the plan's ``generated_at``. It must
            be non-null: the shard's
            :func:`~kbutillib.domains.kbase.berdl.clearinghouse_shard.resolve_observed_at`
            SKIPS a row whose declared ``observed_at`` is null, which would
            drop every abstention at shard time -- the exact failure recording
            them exists to prevent. Defaults to now.
        genomes: optional ``query_id -> [genome_id, ...]``. The clearinghouse
            has no function-to-genome association table and the ranked file's
            ``source`` is the annotation TOOL (``go_mf``'s "Produced by"), not
            a genome, so there is nothing in-pipeline to fill this from today;
            a caller holding the map passes it, and the column is otherwise an
            empty list rather than absent.
        retry_job_ids: job ids belonging to the run's retry pass. A row whose
            verdict came from one of them is marked ``retried``.

    Returns:
        ``{"rows": [...], "counts": {...}}``. ``counts`` carries ``inputs``,
        ``rows``, ``withheld`` (so ``rows + withheld == inputs``), ``abstained``,
        ``decided``, ``with_go_ids``, ``retried`` and one count per council
        decline class.

    Raises:
        ValueError: ``identity`` omits a required field, or one ``query_id``
            arrives in both the abstain and the fanout record (the two
            partitions are disjoint by construction, so an overlap means the
            two files are from different plans and the export would publish
            two answers for one function).
    """
    missing_identity = [key for key in EXPORT_IDENTITY_FIELDS if not identity.get(key)]
    if missing_identity:
        raise ValueError(
            f"export_rows: the run identity omits {missing_identity}. Every "
            "published row carries the identity of the run that produced it; "
            "a row that could not say which query version and mapping_hash "
            "decided it cannot be reconciled against the parameter set it "
            "lands under."
        )

    identity_cells = {key: str(identity[key]) for key in EXPORT_IDENTITY_FIELDS}
    all_job_ids = [str(jid) for jid in job_ids]
    retry = {str(jid) for jid in retry_job_ids}
    genome_map = {str(k): list(v or ()) for k, v in dict(genomes or {}).items()}
    declined_at = _iso(abstained_at) or _now_iso()

    rows: List[Dict[str, Any]] = []
    counts = {
        "inputs": 0,
        "rows": 0,
        "withheld": 0,
        "abstained": 0,
        "decided": 0,
        "with_go_ids": 0,
        "retried": 0,
        "no_candidate_matched": 0,
        "contentless": 0,
        "no_candidates": 0,
    }
    seen: Dict[str, str] = {}

    def _claim(query_id: str, partition: str) -> None:
        previous = seen.get(query_id)
        if previous is not None:
            raise ValueError(
                f"query_id {query_id[:12]}... appears in both the {previous} "
                f"and the {partition} record. The two partitions are disjoint "
                "by construction, so this means the two files came from "
                "different plans; re-export against one plan rather than "
                "publishing two answers for one function."
            )
        seen[query_id] = partition

    for row in abstain or ():
        query_id = str(row.get("query_id") or "")
        if not query_id:
            raise ValueError(
                "an abstain row carries no query_id; query_id is THE join key "
                "into the clearinghouse and cannot be reconstructed from the "
                f"text. Row keys: {sorted(row)}"
            )
        _claim(query_id, "abstain")
        counts["inputs"] += 1
        counts["abstained"] += 1
        reason = str(row.get("abstain_reason") or "")
        rows.append(
            {
                "query_id": query_id,
                "function_text": str(row.get("text") or ""),
                "genomes": genome_map.get(query_id, []),
                "go_ids": [],
                "go_confidences": [],
                "final_confidence": None,
                "decided_by": None,
                # NOT False. ``no_functional_content`` is the COUNCIL's
                # judgement and no seat ever saw this text, so "the council
                # said there is no function here" would be a claim nobody
                # made. Null is the honest cell.
                "no_functional_content": None,
                "decline_reason": str(row.get("decline_reason") or f"abstained:{reason}"),
                "n_valid_proposers": 0,
                "votes": [],
                "decided_at": declined_at,
                **identity_cells,
                "job_ids": list(all_job_ids),
                "retried": False,
            }
        )

    for row in fanout or ():
        query_id = str(row.get("query_id") or "")
        if not query_id:
            raise ValueError(
                "a fanout row carries no query_id; query_id is THE join key "
                "into the clearinghouse and cannot be reconstructed from the "
                f"text. Row keys: {sorted(row)}"
            )
        _claim(query_id, "fanout")
        counts["inputs"] += 1
        item_id = str(row.get("item_id") or "")
        settled = verdicts.get(item_id)
        status = str((settled or {}).get("status") or "")
        if settled is None or status != "ok":
            # NO VERDICT: every call failed, the item never settled, or the
            # job was cancelled. Not a result, so not a row -- it is still
            # un-run and the next plan must re-select it.
            counts["withheld"] += 1
            continue
        verdict = dict((settled.get("verdict") or {}))
        go_ids = [str(gid) for gid in (verdict.get("selected_ids") or ())]
        decline = verdict.get("decline_reason")
        if decline:
            counts[str(decline)] = counts.get(str(decline), 0) + 1
        if go_ids:
            counts["with_go_ids"] += 1
        counts["decided"] += 1
        job_id = str(settled.get("job_id") or "")
        retried = job_id in retry
        if retried:
            counts["retried"] += 1
        rows.append(
            {
                "query_id": query_id,
                "function_text": str(row.get("text") or ""),
                "genomes": genome_map.get(query_id, []),
                "go_ids": go_ids,
                "go_confidences": _pick_confidences(verdict, go_ids),
                "final_confidence": _text_or_none(verdict.get("final_confidence")),
                "decided_by": _text_or_none(verdict.get("decided_by")),
                "no_functional_content": no_functional_content(verdict),
                "decline_reason": _text_or_none(decline),
                "n_valid_proposers": int(verdict.get("n_valid") or 0),
                "votes": _vote_table(verdict.get("votes")),
                "decided_at": _iso(
                    verdict.get("decided_at") or settled.get("decided_at")
                )
                or declined_at,
                **identity_cells,
                "job_ids": list(all_job_ids),
                "retried": retried,
            }
        )

    counts["rows"] = len(rows)
    return {"rows": rows, "counts": counts}


def verdicts_by_item(
    results: Iterable[Mapping[str, Any]],
    *,
    job_id: str = "",
    into: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Reduce one council job's result rows to ``item_id -> settled item``.

    Folded into *into* when given, so a run's several chunk jobs and its retry
    job collapse to ONE mapping. A later job OVERWRITES an earlier one's entry
    for the same ``item_id``, which is what makes the retry pass work: 105 of
    the 113 items that failed on a silently truncated ARGO response came back
    on a plain retry, and the retry's verdict is the run's answer.
    """
    out = into if into is not None else {}
    for row in results or ():
        item_id = str(row.get("item_id") or "")
        if not item_id:
            continue
        out[item_id] = {
            "status": str(row.get("status") or ""),
            "verdict": dict(row.get("verdict") or {}),
            "decided_at": row.get("decided_at"),
            "job_id": str(row.get("job_id") or job_id),
        }
    return out


def no_functional_content(verdict: Mapping[str, Any]) -> Optional[bool]:
    """Did the council find no functional content in this item's text?

    ``CouncilVerdict`` does not carry the flag directly -- it carries the
    CONSEQUENCE, ``decline_reason == "contentless"``, which
    :func:`~kbutillib.domains.ai.council.engine.decide._decline_reason` only
    sets when nothing was selected. So on an item that DID select a term the
    flag has to be read back off the reply that expressed it, by the same rule
    the decision used:

    * meta-decided -- meta's own ``no_functional_content``;
    * unanimous -- :func:`~kbutillib.domains.ai.council.engine.decide.content_flag`,
      the public majority-of-proposers rule with the critic breaking an exact
      tie. Reused rather than re-implemented for exactly the reason that
      function is public: a second, slightly different majority here would
      mean the council decided with one content rule and published another.

    ``None`` when no seat spoke (an empty menu decided deterministically, or a
    verdict shaped unlike either path): the council expressed no judgement and
    ``False`` would be a claim nobody made.
    """
    if str(verdict.get("decline_reason") or "") == "contentless":
        return True
    decided_by = str(verdict.get("decided_by") or "")
    if decided_by == "meta":
        return bool(dict(verdict.get("meta") or {}).get("no_functional_content"))
    if decided_by == "unanimous":
        ballots = list(verdict.get("ballots") or ())
        n_proposers = int(verdict.get("n_valid") or 0) or len(ballots)
        if not n_proposers:
            return None
        return content_flag(ballots, n_proposers, dict(verdict.get("critic") or {}))
    return None


# ---------------------------------------------------------------------------
# Cell helpers
# ---------------------------------------------------------------------------


def _pick_confidences(
    verdict: Mapping[str, Any], go_ids: Sequence[str]
) -> List[Optional[str]]:
    """One stated confidence per selected GO id, positionally aligned.

    The ONLY per-term confidence the council produces is
    :attr:`~kbutillib.domains.ai.council.types.Pick.confidence` on a
    proposer's ballot -- ``go_mf``'s meta reply carries a single
    ``final_confidence`` for the whole item and no per-id figure -- so this is
    read off the ballots.

    ``None`` for a term no surviving ballot picked, which happens when meta
    selected a menu entry the proposers did not vote for. That is not the same
    as a low confidence and must not be published as one.
    """
    stated: Dict[str, List[str]] = {}
    for ballot in verdict.get("ballots") or ():
        if not isinstance(ballot, Mapping) or str(ballot.get("status") or "ok") != "ok":
            continue
        for pick in ballot.get("picks") or ():
            if not isinstance(pick, Mapping):
                continue
            pick_id = str(pick.get("id") or "")
            if pick_id:
                stated.setdefault(pick_id, []).append(_confidence(pick.get("confidence")))
    return [_modal_confidence(stated.get(str(gid)) or []) for gid in go_ids]


def _modal_confidence(values: Sequence[str]) -> Optional[str]:
    """The most-stated confidence in *values*, ties broken toward the LOWER.

    Conservative on purpose: two proposers saying "high" and two saying "low"
    is not a high-confidence term, and a published confidence is read as a
    floor by whoever filters on it.
    """
    if not values:
        return None
    counts = {level: 0 for level in _CONFIDENCE_ORDER}
    for value in values:
        counts[value] += 1
    best = max(counts.values())
    for level in _CONFIDENCE_ORDER:
        if counts[level] == best:
            return level
    return None  # pragma: no cover - unreachable: best is one of the three


def _confidence(value: Any) -> str:
    """Coerce a stated confidence to the three a ballot allows.

    The same coercion
    :func:`~kbutillib.domains.ai.council.engine.decide._confidence` applied
    when the ballot was built, restated for a verdict read back out of the
    store as plain JSON.
    """
    text = str(value or "").strip().lower()
    return text if text in _CONFIDENCE_ORDER else "medium"


def _vote_table(votes: Any) -> List[Dict[str, Any]]:
    """The vote table as a LIST OF STRUCTS, not a map.

    ``pyarrow``'s ``MapArray.to_pylist()`` yields a list of ``(key, value)``
    TUPLES rather than a dict, so a map column read back through
    ``FileSourceAdapter._iter_parquet`` would hand the shard tuples to put in
    a JSON payload. A list of ``{"go_id", "votes"}`` structs round-trips as
    dicts and reads the same in the file as it does in the payload.

    Ordered by descending count then GO id, so the row is stable across two
    exports of one verdict.
    """
    if not isinstance(votes, Mapping):
        return []
    return [
        {"go_id": str(go_id), "votes": int(count or 0)}
        for go_id, count in sorted(
            votes.items(), key=lambda kv: (-int(kv[1] or 0), str(kv[0]))
        )
    ]


def _text_or_none(value: Any) -> Optional[str]:
    """*value* as a string, or ``None`` when it is absent or blank."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _iso(value: Any) -> Optional[str]:
    """An ISO-8601 UTC string from a datetime, a date or a string.

    Written as TEXT rather than a parquet timestamp because the consumer is
    :func:`~kbutillib.domains.kbase.berdl.clearinghouse_shard.resolve_observed_at`,
    which takes an ISO-8601 string (``Z`` included) or a datetime and
    normalises either to naive UTC itself -- so a string keeps one spelling
    across ``PLAN.json``, the store and this file instead of three.

    ONE SPELLING in the column, which is why a string that already parses is
    re-emitted rather than passed through: the store's verdicts carry
    ``+00:00`` (``datetime.isoformat``) while ``PLAN.json`` carries ``Z``, and
    ``decided_at`` holds values from both. Microseconds are kept when the
    source had them -- an ``observed_at`` is not a cell to quietly truncate.
    A string that does not parse is handed through untouched for
    ``resolve_observed_at`` to refuse by name.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return _utc_z(value)
    if isinstance(value, date):
        return f"{value.isoformat()}T00:00:00Z"
    text = str(value).strip()
    if not text:
        return None
    parsed = _parse_iso(text)
    return _utc_z(parsed) if parsed is not None else text


def _parse_iso(text: str) -> Optional[datetime]:
    """*text* as a datetime, or ``None`` when it is not ISO-8601.

    The trailing ``Z`` is swapped for ``+00:00`` first, which
    :meth:`datetime.fromisoformat` rejects before Python 3.11 and this package
    supports 3.9 -- the same swap
    :func:`~kbutillib.domains.kbase.berdl.clearinghouse_shard.resolve_observed_at`
    makes, for the same reason.
    """
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _utc_z(moment: datetime) -> str:
    """*moment* as UTC with a ``Z`` suffix, keeping microseconds if any.

    A naive datetime is ASSUMED to be UTC -- the council store's
    ``UtcDateTime`` hands back aware UTC and the shard makes the same
    assumption, so a local time without an offset is a defect at its source
    and cannot be disambiguated here.
    """
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)
    utc = aware.astimezone(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if utc.microsecond else "%Y-%m-%dT%H:%M:%SZ"
    return utc.strftime(fmt)


def _now_iso() -> str:
    """The current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return _utc_z(datetime.now(tz=timezone.utc))


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


def export_schema() -> Any:
    """The parquet schema, built from :data:`EXPORT_COLUMNS`.

    DECLARED rather than inferred. ``pyarrow`` infers a column's type from the
    values it is handed, so an export in which every row happened to decline
    would infer ``list<null>`` for ``go_ids`` and a shard reading two such
    files would see two different schemas for one source.
    """
    import pyarrow as pa  # noqa: PLC0415 -- keep this module import-cheap

    fields = {
        "query_id": pa.string(),
        "function_text": pa.string(),
        "genomes": pa.list_(pa.string()),
        "go_ids": pa.list_(pa.string()),
        "go_confidences": pa.list_(pa.string()),
        "final_confidence": pa.string(),
        "decided_by": pa.string(),
        "no_functional_content": pa.bool_(),
        "decline_reason": pa.string(),
        "n_valid_proposers": pa.int32(),
        "votes": pa.list_(
            pa.struct([("go_id", pa.string()), ("votes", pa.int32())])
        ),
        "decided_at": pa.string(),
        "query_name": pa.string(),
        "query_version": pa.string(),
        "roster_fingerprint": pa.string(),
        "mapping_hash": pa.string(),
        "job_ids": pa.list_(pa.string()),
        "retried": pa.bool_(),
    }
    return pa.schema([(name, fields[name]) for name in EXPORT_COLUMNS])


def write_verdict_parquet(
    rows: Sequence[Mapping[str, Any]], path: Union[str, Path]
) -> Dict[str, Any]:
    """Write *rows* as parquet, atomically, returning path/rows/bytes.

    Swapped into place through ``.partial``, the same discipline
    :meth:`~.ontology_mapping_utils.OntologyMappingUtils.write_ranked` and
    ``PLAN.json`` use: the shard reads this file by name, and a reader must
    never see a half-written one.
    """
    import pyarrow as pa  # noqa: PLC0415 -- keep this module import-cheap
    import pyarrow.parquet as pq  # noqa: PLC0415

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist([dict(row) for row in rows], schema=export_schema())
    tmp = path.with_name(path.name + ".partial")
    pq.write_table(table, str(tmp))
    tmp.replace(path)
    return {"path": str(path), "rows": table.num_rows, "bytes": path.stat().st_size}
