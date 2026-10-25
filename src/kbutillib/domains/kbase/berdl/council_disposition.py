"""Curated job selection for ``kbaseincubator.council`` -- what a job's output is FOR.

WHY THIS LAYER EXISTS. The council store CANNOT tell you which jobs are a
result. For the ADP1+PMI run it holds 21 jobs over 19,283 entities as 59,493
items: trials at ``go_mf@1-3``, THREE 500-item pilots that used THREE
DIFFERENT ROSTERS, two CANCELLED 19,276-item jobs that look authoritative by
label and by size (one of which carries 49 genuine, since-superseded
verdicts), and the twelve that are the real dataset (ten chunks plus two
retry rounds). Neither ``jobs.status`` nor ``jobs.deleted_at`` carries the
distinction: a cancelled monolith and a published chunk are both rows a
reader has no way to rank, and the biggest, most authoritative-looking job in
the store is one of the two that must never be loaded.

THE RULE THAT MATTERS MOST -- ABSENCE IS NOT CONSENT. A job with NO
disposition row is NOT publishable. :func:`published_jobs` returns nothing
for an undispositioned job, and :func:`require_published` refuses one by
name. If absence read as an implicit ``published``, the two cancelled
19,276-item look-alikes would load into the clearinghouse, which is the
precise failure this layer exists to prevent -- and it could not be undone:
the clearinghouse slot key ``(entity_hash, entity_type, result_type, source,
parameter_set_hash)`` deliberately EXCLUDES ``result_type_version``, so a
payload shape is frozen on first write and a corrected payload in the same
slot is silently dropped as already-present.

NECESSARY BUT NOT SUFFICIENT. A ``published`` disposition is not the whole
publication gate. The publisher requires BOTH that a job is listed in its
campaign's publication bundle AND that its disposition is ``published``
(see ``genome.ontology_mapping.council_bundle``). Twelve jobs marked
``published`` do not by themselves define a campaign or a unique winner once
a second published council overlaps the same functions. Nothing in this
module knows about bundles, and nothing here may be documented as the whole
gate.

TWO RECORDS, ONE JOB EACH -- read this before adding a third.

1. :class:`DispositionLedger` -- an append-only, fsynced JSON-lines file of
   DECLARATIONS. This is the provenance record: who declared what, when, and
   from which source. Lines are appended and never rewritten, so a
   re-declaration (someone re-declares a pilot as published) KEEPS the
   superseded line and the question "who said this was publishable" stays
   answerable. The ledger is the authority on declarations.

2. The ``council.job_disposition`` table, whose DDL ``council_schema``
   declares. Its primary key is ``(store_id, snapshot_id, job_id)`` -- ONE
   disposition per job per snapshot -- so it physically cannot hold a
   declaration history, and writing two rows for one job would violate the
   key the archiver and every reader rely on. It therefore carries the
   CURRENT projection of the ledger, written per snapshot by
   :func:`write_dispositions`.

The split is forced by that key, not chosen for symmetry. A single record
cannot be both append-only-by-declaration and unique-by-job.

IDEMPOTENCY. :func:`seed_from_provenance` re-reading the same file changes
NOTHING: a declaration identical to the job's current one (same disposition
and same reason) appends no line, so ``declared_at`` is not disturbed and
the ledger does not grow. Only a CHANGE appends, and it appends with the new
``declared_by`` and ``declared_at`` rather than overwriting the old ones.

THE SEED IS DATA, NOT CODE. ``read_provenance`` derives every disposition
from the provenance file's own structure -- its ``LOAD_THESE`` list and each
job's ``disposition`` prose -- and REFUSES a prose shape it cannot classify
(:class:`ProvenanceFormatError`) rather than guessing. No job id is
transcribed into this module, and none may be: a hand-copied id is
indistinguishable from a correct one and the first reader to notice would be
a clearinghouse query months later.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ...ai.council.store.sqlite import validate_store_id
from .council_schema import JOB_DISPOSITION_TABLE, JOB_DISPOSITIONS

__all__ = [
    "JOB_DISPOSITIONS",
    "PUBLISHED",
    "LEDGER_FILENAME",
    "STATE_DIR_ENV",
    "DEFAULT_STATE_ROOT",
    "resolve_ledger_path",
    "DispositionError",
    "UnknownDispositionError",
    "ProvenanceFormatError",
    "JobNotPublishableError",
    "JobDeclaration",
    "DispositionLedger",
    "validate_disposition",
    "read_provenance",
    "seed_from_provenance",
    "declare",
    "disposition_rows",
    "write_dispositions",
    "dispositions",
    "published_jobs",
    "require_published",
]

#: The one disposition that permits publication. Named rather than spelled at
#: each call site so a reader greps one constant and finds every gate.
PUBLISHED = "published"

#: The ledger file inside a declaration state directory.
LEDGER_FILENAME = "job_disposition_ledger.jsonl"

#: Environment variable naming the ROOT under which declaration ledgers live.
#: Mirrors ``clearinghouse_ingest.STATE_DIR_ENV``: the ledger must NOT live
#: beside the thing it describes. A council store is EPHEMERAL (the
#: production one lived on ``/scratch``) while the declarations about its jobs
#: are the only record of which outputs are a result, so they have to outlive
#: it.
STATE_DIR_ENV = "KBU_COUNCIL_DISPOSITION_DIR"

#: Default ledger root when :data:`STATE_DIR_ENV` is unset. Under the user's
#: home, never under ``/scratch`` and never in Dropbox (a ledger that syncs
#: to the fleet gets conflicted copies, and "which copy declared this" is the
#: one question it must not raise).
DEFAULT_STATE_ROOT = "~/.kbu/council/dispositions"

#: Markers mapped to the EXCLUDING dispositions, scanned by first occurrence
#: in a job's ``disposition`` prose (see :func:`_classify_excluded`).
#:
#: ``superseded`` is listed and is NOT redundant with ``cancelled``: the
#: cancelled monolith's prose says "cancelled monolithic attempt ... Its 49
#: verdicts are real but superseded by the chunk that re-ran those items",
#: naming BOTH. Which one wins is decided by POSITION, not by this tuple's
#: order, precisely so that a prose which leads with its reason is classified
#: by its reason.
_EXCLUDED_MARKERS: tuple[str, ...] = ("cancelled", "pilot", "trial", "superseded")

#: What a job's ``disposition`` prose says when the job IS part of the
#: published dataset. Matched at the START of the prose, because "DO NOT
#: LOAD" also contains "LOAD".
_LOAD_PREFIX = "LOAD"
_EXCLUDE_PREFIX = "DO NOT LOAD"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class DispositionError(ValueError):
    """A disposition could not be read, declared or resolved."""


class UnknownDispositionError(DispositionError):
    """A disposition value outside :data:`JOB_DISPOSITIONS`.

    The enum is enforced in Python at the write boundary because the DDL
    path emits no constraints at all (``council_schema``'s ``schema_sql``
    emits no ``NOT NULL`` and no ``CHECK``), exactly as that module enforces
    hash width at the boundary and not in the DDL. A sixth value would reach
    an append-only Iceberg table and every reader's ``disposition ==
    'published'`` test would silently answer "no" for it forever.
    """


class ProvenanceFormatError(DispositionError):
    """A provenance file could not be mapped onto the disposition enum.

    Raised rather than guessed. The seed file is prose written by a human
    about 21 jobs; a shape this module cannot classify means the file says
    something new, and the only safe reading of "something new" about
    publishability is to stop.
    """


class JobNotPublishableError(RuntimeError):
    """A job named for publication is not declared ``published``.

    NAMES THE JOB ID. The message is the whole diagnostic: "this job is not
    publishable" and "job 477d6c53... is declared cancelled" are the same
    exit status, and only the second one tells an operator whether they
    picked the wrong job or forgot to declare it.
    """


# --------------------------------------------------------------------------
# The enum
# --------------------------------------------------------------------------


def validate_disposition(value: Any) -> str:
    """Return *value* as a recognised disposition, or refuse.

    Raises:
        UnknownDispositionError: *value* is not one of
            :data:`JOB_DISPOSITIONS`.
    """
    text = str(value).strip() if value is not None else ""
    if text not in JOB_DISPOSITIONS:
        raise UnknownDispositionError(
            f"{text!r} is not a council job disposition; expected one of "
            f"{list(JOB_DISPOSITIONS)!r}. The enum is enforced here and not "
            "in the DDL, so an unrecognised value would otherwise reach an "
            "append-only table where no reader would ever match it."
        )
    return text


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def resolve_ledger_path(store_id: str, *, root: Any = None) -> Path:
    """Where one store's declaration ledger lives.

    KEYED ON ``store_id``, not on the store's path: a store that moves off
    ``/scratch`` keeps its declared id, and a path-keyed ledger would
    silently become a second, empty one the first time it moved -- which
    reads as "nothing is publishable" and is indistinguishable from a store
    whose jobs nobody has declared.

    Args:
        store_id: The store's DECLARED archive identity.
        root: Override the root directory. Defaults to
            :data:`STATE_DIR_ENV`, then :data:`DEFAULT_STATE_ROOT`.
    """
    base = root or os.environ.get(STATE_DIR_ENV) or DEFAULT_STATE_ROOT
    # A declared store_id may contain "/" (e.g. "poplar/council-1"), which is
    # a directory separator here on purpose: it keeps one machine's stores
    # together and never escapes `base`, because validate_store_id refuses
    # "..", absolute paths and anything outside its allowed pattern.
    safe = validate_store_id(store_id)
    return Path(base).expanduser().joinpath(safe, LEDGER_FILENAME)


# --------------------------------------------------------------------------
# Reading the provenance file
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class JobDeclaration:
    """One job's declared disposition, as read out of a provenance file.

    Attributes:
        job_id: The store's job id, taken verbatim from the file.
        disposition: One of :data:`JOB_DISPOSITIONS`, DERIVED from the
            file's structure -- never transcribed.
        reason: The file's own prose, kept whole. The reason column exists so
            that "why is this not published" is answerable without the file.
    """

    job_id: str
    disposition: str
    reason: str


def _classify_excluded(job_id: str, prose: str) -> str:
    """Map a ``DO NOT LOAD`` prose onto an excluding disposition.

    FIRST-OCCURRENCE WINS. The prose leads with its reason, and one of the
    real entries names two markers ("cancelled monolithic attempt ... but
    superseded by the chunk that re-ran those items"). Scanning by position
    rather than by a fixed marker order is what makes that entry classify as
    ``cancelled`` -- which is what it is -- instead of by whichever marker a
    tuple happened to list first.

    Raises:
        ProvenanceFormatError: no marker appears at all.
    """
    lowered = prose.lower()
    found = [
        (lowered.index(marker), marker)
        for marker in _EXCLUDED_MARKERS
        if marker in lowered
    ]
    if not found:
        raise ProvenanceFormatError(
            f"job {job_id}: the provenance file excludes it with "
            f"{prose!r}, which names none of {list(_EXCLUDED_MARKERS)!r}, so "
            "this module cannot tell WHICH excluding disposition it is. "
            "Refusing to guess -- add the word to the file's prose, or map "
            "the new shape explicitly."
        )
    found.sort()
    return validate_disposition(found[0][1])


def read_provenance(path: Any) -> list[JobDeclaration]:
    """Read a provenance JSON file into declarations, deriving each disposition.

    The file shape this reads (``AUTHORITATIVE-JOB-PROVENANCE.json``, checked
    into ``tests/berdl/fixtures/``) carries two independent statements about
    publishability, and BOTH are read:

    * a top-level ``LOAD_THESE`` list of the published job ids;
    * a per-job ``disposition`` prose beginning ``LOAD`` or ``DO NOT LOAD``.

    A DISAGREEMENT between the two is REFUSED, not reconciled. They are
    written by hand in one file and a job listed in ``LOAD_THESE`` whose
    prose says ``DO NOT LOAD`` means the file itself does not know -- and the
    cost of picking the wrong side is loading a cancelled 19,276-item job
    into a slot that cannot be corrected.

    Args:
        path: The provenance JSON file.

    Returns:
        One :class:`JobDeclaration` per job, in file order.

    Raises:
        ProvenanceFormatError: The file is not a mapping, carries no
            ``jobs`` list, repeats a job id, has a job with no ``job_id`` or
            no ``disposition``, carries a prose that classifies to nothing,
            or disagrees with its own ``LOAD_THESE``.
    """
    source = Path(path).expanduser()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProvenanceFormatError(f"{source}: unreadable provenance file: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ProvenanceFormatError(
            f"{source}: a provenance file must be a JSON object, got "
            f"{type(raw).__name__}"
        )
    jobs = raw.get("jobs")
    if not isinstance(jobs, Sequence) or isinstance(jobs, (str, bytes)) or not jobs:
        raise ProvenanceFormatError(
            f"{source}: no non-empty 'jobs' list. There is nothing to seed "
            "and an empty seed would leave every job undispositioned, which "
            "reads as 'nothing is publishable' rather than as an error."
        )
    load_these = raw.get("LOAD_THESE")
    declared_published: set[str] | None = (
        {str(job_id) for job_id in load_these}
        if isinstance(load_these, Sequence) and not isinstance(load_these, (str, bytes))
        else None
    )

    declarations: list[JobDeclaration] = []
    seen: set[str] = set()
    for index, entry in enumerate(jobs):
        if not isinstance(entry, Mapping):
            raise ProvenanceFormatError(
                f"{source}: jobs[{index}] is {type(entry).__name__}, not an object"
            )
        job_id = str(entry.get("job_id") or "").strip()
        if not job_id:
            raise ProvenanceFormatError(f"{source}: jobs[{index}] has no job_id")
        if job_id in seen:
            raise ProvenanceFormatError(
                f"{source}: job {job_id} appears twice. Which line is the "
                "declaration is not decidable from the file."
            )
        seen.add(job_id)
        prose = str(entry.get("disposition") or "").strip()
        if not prose:
            raise ProvenanceFormatError(
                f"{source}: job {job_id} carries no 'disposition' prose, so "
                "the file states nothing about whether it is publishable. An "
                "absent statement is not 'published' (see this module's "
                "ABSENCE IS NOT CONSENT note)."
            )
        if prose.upper().startswith(_EXCLUDE_PREFIX):
            disposition = _classify_excluded(job_id, prose)
        elif prose.upper().startswith(_LOAD_PREFIX):
            disposition = PUBLISHED
        else:
            raise ProvenanceFormatError(
                f"job {job_id}: disposition prose {prose!r} begins with "
                f"neither {_LOAD_PREFIX!r} nor {_EXCLUDE_PREFIX!r}, so "
                "whether the file publishes this job is not decidable."
            )
        if declared_published is not None:
            listed = job_id in declared_published
            if listed != (disposition == PUBLISHED):
                raise ProvenanceFormatError(
                    f"{source}: job {job_id} is "
                    f"{'IN' if listed else 'NOT in'} LOAD_THESE but its "
                    f"prose resolves to {disposition!r}. The file "
                    "contradicts itself and nothing here will pick a side."
                )
        declarations.append(
            JobDeclaration(job_id=job_id, disposition=disposition, reason=prose)
        )

    if declared_published is not None:
        missing = sorted(declared_published - seen)
        if missing:
            raise ProvenanceFormatError(
                f"{source}: LOAD_THESE names job(s) {missing!r} that have no "
                "entry in 'jobs', so there is no reason or status for them."
            )
    return declarations


# --------------------------------------------------------------------------
# The append-only declaration ledger
# --------------------------------------------------------------------------


class DispositionLedger:
    """One append-only, fsynced JSON-lines record of disposition declarations.

    Same shape and same discipline as
    ``clearinghouse_ingest.ExecutionLedger``: every line is flushed AND
    ``fsync``ed before the call returns, lines are appended and never
    rewritten, and the LAST line for a job id is its current declaration.
    The earlier lines are the answer to "who said this was a pilot before
    somebody published it", which is the one question a disposition change
    must not destroy.

    A malformed line is SKIPPED rather than fatal, for the same reason it is
    there: a crash mid-write leaves at most one truncated trailing line, and
    failing every later read over it would be worse than ignoring it.
    """

    def __init__(self, path: Any) -> None:
        self.path = Path(path).expanduser()

    # -- writing ---------------------------------------------------------

    def append(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Append one declaration, fsynced, and return it as written."""
        line = dict(record)
        line.setdefault("declared_at", _utcnow_iso())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return line

    # -- reading ---------------------------------------------------------

    def records(self) -> list[dict[str, Any]]:
        """Every declaration ever appended, in order. Nothing is collapsed."""
        if not self.path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(record, dict):
                continue
            if not record.get("job_id") or not record.get("disposition"):
                continue
            out.append(record)
        return out

    def current(self) -> dict[str, dict[str, Any]]:
        """The LAST declaration per job id -- the current projection."""
        out: dict[str, dict[str, Any]] = {}
        for record in self.records():
            out[str(record["job_id"])] = record
        return out

    def history(self, job_id: str) -> list[dict[str, Any]]:
        """Every declaration ever made about one job, oldest first."""
        return [r for r in self.records() if str(r.get("job_id")) == str(job_id)]


def declare(
    ledger: DispositionLedger,
    *,
    job_id: str,
    disposition: str,
    reason: str,
    declared_by: str,
    declared_at: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Declare one job's disposition. Idempotent on an identical declaration.

    A declaration whose ``disposition`` AND ``reason`` already match the
    job's current one appends NOTHING: ``declared_at`` is left as it was, so
    re-running a seed does not restamp a decision nobody changed. A CHANGE
    appends a new line carrying the new ``declared_by`` and ``declared_at``;
    the superseded line stays in the file.

    Args:
        ledger: The declaration ledger to append to.
        job_id: The store's job id.
        disposition: One of :data:`JOB_DISPOSITIONS`.
        reason: Why. Stored verbatim.
        declared_by: WHO is declaring this. Never defaulted -- a declaration
            with no author is not a provenance record, and the one question
            this ledger exists to answer is who said what.
        declared_at: ISO-8601 timestamp. Defaults to now (UTC).
        source: Optional provenance of the declaration itself, e.g. the
            seed file it came from.

    Returns:
        ``{"job_id", "disposition", "outcome", "record", "previous"}`` where
        ``outcome`` is ``"added"``, ``"changed"`` or ``"unchanged"``.

    Raises:
        UnknownDispositionError: *disposition* is outside the enum.
        DispositionError: *declared_by* is blank.
    """
    value = validate_disposition(disposition)
    who = str(declared_by or "").strip()
    if not who:
        raise DispositionError(
            f"job {job_id}: declared_by is required. A disposition with no "
            "author cannot answer 'who decided this was publishable', which "
            "is the whole reason the declaration is recorded."
        )
    previous = ledger.current().get(str(job_id))
    text = str(reason or "")
    if (
        previous is not None
        and str(previous.get("disposition")) == value
        and str(previous.get("reason") or "") == text
    ):
        return {
            "job_id": str(job_id),
            "disposition": value,
            "outcome": "unchanged",
            "record": previous,
            "previous": previous,
        }
    record = ledger.append(
        {
            "job_id": str(job_id),
            "disposition": value,
            "reason": text,
            "declared_by": who,
            **({"declared_at": declared_at} if declared_at else {}),
            **({"source": source} if source else {}),
            **(
                {
                    "supersedes": {
                        "disposition": previous.get("disposition"),
                        "declared_by": previous.get("declared_by"),
                        "declared_at": previous.get("declared_at"),
                    }
                }
                if previous is not None
                else {}
            ),
        }
    )
    return {
        "job_id": str(job_id),
        "disposition": value,
        "outcome": "added" if previous is None else "changed",
        "record": record,
        "previous": previous,
    }


def seed_from_provenance(
    ledger: DispositionLedger,
    path: Any,
    *,
    declared_by: str,
    declared_at: str | None = None,
) -> dict[str, Any]:
    """Seed *ledger* from a provenance JSON file. Re-seeding changes nothing.

    Args:
        ledger: The declaration ledger to seed.
        path: The provenance JSON file (see :func:`read_provenance`).
        declared_by: WHO is declaring these. Required.
        declared_at: ISO-8601 timestamp for the declarations this call
            makes. Defaults to now (UTC) per appended line.

    Returns:
        A report: ``source``, ``declared_by``, per-disposition ``counts``
        over the whole file, the ``added`` / ``changed`` / ``unchanged`` job
        ids this call produced, and ``published`` -- the ids the file
        declares publishable.
    """
    declarations = read_provenance(path)
    source = str(Path(path).expanduser())
    outcomes: dict[str, list[str]] = {"added": [], "changed": [], "unchanged": []}
    counts: dict[str, int] = {name: 0 for name in JOB_DISPOSITIONS}
    for declaration in declarations:
        counts[declaration.disposition] += 1
        result = declare(
            ledger,
            job_id=declaration.job_id,
            disposition=declaration.disposition,
            reason=declaration.reason,
            declared_by=declared_by,
            declared_at=declared_at,
            source=source,
        )
        outcomes[result["outcome"]].append(declaration.job_id)
    return {
        "source": source,
        "declared_by": declared_by,
        "jobs": len(declarations),
        "counts": counts,
        "published": [d.job_id for d in declarations if d.disposition == PUBLISHED],
        "excluded": [d.job_id for d in declarations if d.disposition != PUBLISHED],
        **outcomes,
    }


# --------------------------------------------------------------------------
# The lakehouse projection
# --------------------------------------------------------------------------


def disposition_rows(
    ledger: DispositionLedger,
    *,
    store_id: str,
    snapshot_id: str,
    snapshot_seq: int,
    store_host: str,
    load_batch_id: str,
    loaded_at: str | None = None,
) -> list[dict[str, Any]]:
    """Project *ledger*'s CURRENT declarations as ``job_disposition`` rows.

    One row per job, which is what the table's declared primary key
    ``(store_id, snapshot_id, job_id)`` permits. The declaration HISTORY
    stays in the ledger; it is deliberately not flattened into this table,
    because two rows for one job would break that key for every reader.
    """
    stamp = loaded_at or _utcnow_iso()
    rows: list[dict[str, Any]] = []
    for job_id, record in sorted(ledger.current().items()):
        rows.append(
            {
                "store_id": store_id,
                "snapshot_id": snapshot_id,
                "snapshot_seq": int(snapshot_seq),
                "store_host": store_host,
                "loaded_at": stamp,
                "load_batch_id": load_batch_id,
                "job_id": job_id,
                "disposition": validate_disposition(record.get("disposition")),
                "reason": record.get("reason"),
                "declared_by": record.get("declared_by"),
                "declared_at": record.get("declared_at"),
            }
        )
    return rows


def write_dispositions(
    sink: Any,
    ledger: DispositionLedger,
    *,
    store_id: str,
    snapshot_id: str,
    snapshot_seq: int,
    store_host: str,
    load_batch_id: str,
    loaded_at: str | None = None,
) -> dict[str, Any]:
    """Write *ledger*'s current projection into ``council.job_disposition``.

    REFUSES to write a second time for the same ``(store_id, snapshot_id)``.
    The table's key permits one row per job per snapshot, so appending a
    second projection would duplicate every job and leave no way to tell
    which row a reader should believe.

    Raises:
        DispositionError: rows for this ``(store_id, snapshot_id)`` already
            exist.
    """
    existing = sink.count_rows(
        JOB_DISPOSITION_TABLE,
        where={"store_id": store_id, "snapshot_id": snapshot_id},
    )
    if existing:
        raise DispositionError(
            f"refusing to write dispositions for store {store_id!r} snapshot "
            f"{snapshot_id!r}: {existing} row(s) are already archived under "
            "that key. job_disposition is keyed (store_id, snapshot_id, "
            "job_id) -- one disposition per job per snapshot -- so a second "
            "projection would duplicate every job and no reader could tell "
            "which row to believe."
        )
    rows = disposition_rows(
        ledger,
        store_id=store_id,
        snapshot_id=snapshot_id,
        snapshot_seq=snapshot_seq,
        store_host=store_host,
        load_batch_id=load_batch_id,
        loaded_at=loaded_at,
    )
    result = sink.append_rows(JOB_DISPOSITION_TABLE, rows)
    return {
        "store_id": store_id,
        "snapshot_id": snapshot_id,
        "snapshot_seq": int(snapshot_seq),
        "rows": len(rows),
        "published": [r["job_id"] for r in rows if r["disposition"] == PUBLISHED],
        "append_result": result,
    }


# --------------------------------------------------------------------------
# The resolver, and the gate
# --------------------------------------------------------------------------


def _current_snapshot(
    sink: Any, *, store_id: str | None, snapshot_id: str | None
) -> dict[str, str] | None:
    """Resolve which snapshot's dispositions a read should see.

    With an explicit *snapshot_id*, that one. Without, the HIGHEST
    ``snapshot_seq`` for the store -- the current projection. NOT a union
    over snapshots: a job declared ``published`` in snapshot 1 and
    re-declared ``pilot`` in snapshot 2 would otherwise resolve as
    publishable forever, which is exactly the silent carry-forward this
    layer exists to prevent.

    Returns ``None`` when the table holds nothing matching, which reads as
    "no job is publishable" and never as "every job is".
    """
    if snapshot_id is not None:
        return {"snapshot_id": snapshot_id}
    where: dict[str, Any] = {}
    if store_id is not None:
        where["store_id"] = store_id
    rows = sink.select_rows(
        JOB_DISPOSITION_TABLE,
        where=where or None,
        columns=("store_id", "snapshot_id", "snapshot_seq"),
        order_by=("snapshot_seq",),
    )
    if not rows:
        return None
    if store_id is None:
        stores = {str(row["store_id"]) for row in rows}
        if len(stores) > 1:
            raise DispositionError(
                "the job_disposition table holds dispositions for "
                f"{len(stores)} stores ({sorted(stores)!r}) and no store_id "
                "was given, so 'the current snapshot' is not one snapshot. "
                "Name the store."
            )
    latest = max(rows, key=lambda row: int(row["snapshot_seq"]))
    return {
        "store_id": str(latest["store_id"]),
        "snapshot_id": str(latest["snapshot_id"]),
    }


def dispositions(
    sink: Any,
    *,
    store_id: str | None = None,
    snapshot_id: str | None = None,
    job_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Return ``job_disposition`` rows for one snapshot.

    See :func:`_current_snapshot` for which snapshot that is when
    *snapshot_id* is not given.
    """
    scope = _current_snapshot(sink, store_id=store_id, snapshot_id=snapshot_id)
    if scope is None:
        return []
    where = dict(scope)
    if store_id is not None:
        where["store_id"] = store_id
    if job_ids is not None:
        if not list(job_ids):
            return []
        where["job_id"] = list(job_ids)
    return sink.select_rows(
        JOB_DISPOSITION_TABLE, where=where, order_by=("job_id",)
    )


def published_jobs(
    sink: Any,
    *,
    store_id: str | None = None,
    query_name: str | None = None,
    snapshot_id: str | None = None,
) -> list[str]:
    """Return ONLY the job ids declared ``published``.

    An undispositioned job is absent from the result. That is the property
    this function exists for: a consumer that iterated the store's jobs and
    filtered out the ones it recognised as bad would load the two cancelled
    19,276-item look-alikes, because they look authoritative by every signal
    the store carries.

    Args:
        sink: A council sink (see :mod:`council_sink`).
        store_id: Restrict to one store. With several stores archived and no
            ``store_id``, resolving "the current snapshot" is refused.
        query_name: Restrict to jobs of one council kind. Resolved by
            reading the archived ``jobs`` table for the SAME snapshot --
            ``job_disposition`` carries no ``query_name`` column, and
            inventing one would denormalise a third copy of the only
            council-kind discriminator in the store. A snapshot whose
            ``jobs`` table is not archived therefore yields NOTHING for a
            ``query_name`` filter, which is the honest answer: the archive
            does not say what kind those jobs were.
        snapshot_id: Read one specific snapshot instead of the current one.

    Returns:
        The published job ids, sorted.
    """
    rows = dispositions(sink, store_id=store_id, snapshot_id=snapshot_id)
    published = sorted(
        str(row["job_id"]) for row in rows if str(row["disposition"]) == PUBLISHED
    )
    if query_name is None or not published:
        return published
    scope = _current_snapshot(sink, store_id=store_id, snapshot_id=snapshot_id)
    if scope is None:
        return []
    where = dict(scope)
    if store_id is not None:
        where["store_id"] = store_id
    where["job_id"] = published
    where["query_name"] = query_name
    matched = sink.select_rows("jobs", where=where, columns=("job_id",))
    keep = {str(row["job_id"]) for row in matched}
    return [job_id for job_id in published if job_id in keep]


def require_published(
    sink: Any,
    job_ids: Iterable[str],
    *,
    store_id: str | None = None,
    snapshot_id: str | None = None,
) -> list[str]:
    """Return *job_ids* unchanged, or refuse, NAMING every job that is not published.

    This is the gate a publisher calls. It refuses two distinct cases and
    says which:

    * the job HAS a disposition and it is not ``published`` -- the message
      names the disposition, so an operator can see they picked the pilot;
    * the job has NO disposition row at all -- the message says so, because
      "undeclared" and "declared cancelled" need different fixes and the
      first one is the dangerous default this layer removes.

    Raises:
        JobNotPublishableError: any job in *job_ids* is not published.
        DispositionError: *job_ids* is empty.
    """
    wanted = [str(job_id) for job_id in job_ids]
    if not wanted:
        raise DispositionError(
            "require_published was given no job ids. An empty publication "
            "set is not 'everything is fine' -- it means the caller resolved "
            "nothing, which is the state an undispositioned store produces."
        )
    rows = {
        str(row["job_id"]): row
        for row in dispositions(
            sink, store_id=store_id, snapshot_id=snapshot_id, job_ids=wanted
        )
    }
    problems: list[str] = []
    for job_id in wanted:
        row = rows.get(job_id)
        if row is None:
            problems.append(
                f"{job_id}: NO disposition row -- it has never been declared. "
                "An undeclared job is not publishable; absence is not an "
                "implicit 'published'."
            )
        elif str(row["disposition"]) != PUBLISHED:
            problems.append(
                f"{job_id}: declared {str(row['disposition'])!r}"
                + (f" ({row['reason']})" if row.get("reason") else "")
            )
    if problems:
        raise JobNotPublishableError(
            "refusing to publish: "
            + str(len(problems))
            + " of "
            + str(len(wanted))
            + " job(s) are not declared 'published'.\n  "
            + "\n  ".join(problems)
        )
    return wanted
