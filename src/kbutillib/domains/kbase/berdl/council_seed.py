"""Read one archived snapshot back out of ``kbaseincubator.council`` as a seed.

THE LAKEHOUSE HALF of ``kbu council seed-store --from-lakehouse``. The store
half -- the rules about what may be imported and what it means -- is
:meth:`kbutillib.domains.ai.council.store.sqlite.SqliteStore.seed_from_rows`,
and the split is deliberate: the store layer must not depend on
``kbase.berdl`` (SQLAlchemy and PySpark are different optional extras, and a
seed must be possible from any source that can produce rows of the archived
shape), while the completeness test a seed depends on lives here.

WHY A SEED IS WORTH A MODULE. ``calls.prompt_hash`` is the reuse key and
``find_reusable`` serves any ``status='ok'`` row carrying it, so a wiped store
is a wiped CACHE: re-running the ADP1+PMI set (19,276 items) at the same query
version and roster against an empty store re-pays on the order of 199M
tokens, where the intact store costs approximately nothing -- the same
mechanism that made the retry rounds cost 0.30% of the run, and that charged
475 re-paid items when ``go_mf@3 -> @4`` re-hashed every prompt. This path is
what makes archiving a store and then deleting it a safe thing to do.

THREE REFUSALS LIVE HERE, and each is a call into existing code rather than a
second opinion:

* An INCOMPLETE snapshot is refused by
  :func:`~.council_archive.require_complete_delivery`. There is exactly ONE
  completeness test in this repo and that is it; a second one would be free to
  disagree, and the failure it would permit -- seeding a cache from provenance
  that reads complete and is not -- is a cache with holes nobody can see.
* ``live=0`` rows and non-``ok`` rows are excluded by ``seed_from_rows``.
  They are pre-filtered here too (so a 203,821-row ``calls`` table is not
  dragged through the seam for the ~82,000 rows that matter), but the
  AUTHORITATIVE filter is the store's: ``select_reusable`` does not filter
  ``live``, so a superseded answer that reached a reuse cache WOULD be served
  as a current one, and that guarantee may not depend on which reader built
  the rows.
* A second origin is refused by ``seed_from_rows``: every row this module
  reads carries its ``(store_id, snapshot_id)``, so a mapping assembled from
  two snapshots is detectable before anything is written.

NOT VERIFIED AGAINST A LIVE POD. Reads go through the sink seam
(:mod:`council_sink`), so this runs against ``SqliteCouncilSink`` for real and
against ``BerdlCouncilSink`` only where a pod exists; the BERDL transport has
been broken on kbhub since the 2026-10-19 image (KBUtilLib#91). Anything
needing the pod is AWAITING OPERATOR ACCEPTANCE.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .council_archive import require_complete_delivery

__all__ = [
    "SEED_TABLES",
    "read_seed_rows",
]

#: The archived tables a seed reads, in the order ``seed_from_rows`` inserts
#: them. ``attempts``, ``endpoint_state`` and ``workers`` are deliberately NOT
#: read: an attempt records what the ORIGINAL run's try cost, the endpoint
#: counters are mutable cumulative totals that would charge this machine for
#: another machine's spend, and a worker row describes a process that no
#: longer exists. All three stay queryable in the lakehouse, which is where a
#: question about them belongs.
SEED_TABLES: tuple[str, ...] = ("query_types", "jobs", "items", "calls")


def read_seed_rows(
    sink: Any,
    *,
    store_id: str,
    snapshot_id: str,
    query_name: str | None = None,
    query_version: str | None = None,
    state_root: Any = None,
) -> dict[str, Any]:
    """Read one COMPLETE snapshot's reusable rows, ready for ``seed_from_rows``.

    Args:
        sink: A council sink (see :mod:`council_sink`).
        store_id: The source store's declared archive identity.
        snapshot_id: The source snapshot's verified digest. ONE snapshot: a
            seed of "whatever is current" is not reproducible, and the
            projection helpers that answer "current" are a different question
            (:func:`~.council_archive.current_projection`).
        query_name: Restrict to one council kind. A reuse hit is only valid at
            the same ``(query_name, query_version)`` because the version is
            inside ``prompt_hash``, so scoping is the caller's to declare.
        query_version: Restrict to one query version. Requires *query_name*.
        state_root: Root of the delivery state directories, for the
            completeness check.

    Returns:
        ``{"origin", "scope", "rows", "counts", "receipt"}``. ``rows`` is the
        ``{table: [row, ...]}`` mapping ``seed_from_rows`` takes, with every
        row exactly as the archive holds it -- identity columns included, so
        the store can refuse a second origin.

    Raises:
        DeliveryIncompleteError: this snapshot is not completely archived.
        ValueError: *query_version* without *query_name*.
    """
    if query_version is not None and not query_name:
        raise ValueError(
            "query_version without query_name does not scope anything: "
            "versions are per query type, so `4` alone names no rows"
        )
    # The ONE completeness test. A snapshot whose `attempts` never committed
    # reads as a complete archive to every query that does not join it.
    completion = require_complete_delivery(
        sink,
        store_id=store_id,
        snapshot_id=snapshot_id,
        state_root=state_root,
    )

    identity = {"store_id": store_id, "snapshot_id": snapshot_id}
    calls = [
        row
        for row in sink.select_rows(
            "calls", where={**identity, "status": "ok"}, order_by=("call_id",)
        )
        # Pre-filter only. The authoritative live filter is the store's --
        # see this module's docstring.
        if _is_live(row.get("live"))
        and _in_scope(row, query_name=query_name, query_version=query_version)
    ]
    job_ids = sorted({str(row["job_id"]) for row in calls})
    jobs = (
        sink.select_rows("jobs", where={**identity, "job_id": job_ids})
        if job_ids
        else []
    )
    items = (
        sink.select_rows("items", where={**identity, "job_id": job_ids})
        if job_ids
        else []
    )
    wanted = {(str(row["job_id"]), str(row["item_id"])) for row in calls}
    items = [row for row in items if (str(row["job_id"]), str(row["item_id"])) in wanted]
    types = _query_types_for(sink, identity, jobs)

    rows: dict[str, Sequence[Mapping[str, Any]]] = {
        "query_types": types,
        "jobs": jobs,
        "items": items,
        "calls": calls,
    }
    return {
        "origin": {"store_id": store_id, "snapshot_id": snapshot_id},
        "scope": {"query_name": query_name, "query_version": query_version},
        "rows": rows,
        "counts": {table: len(rows[table]) for table in SEED_TABLES},
        "receipt": completion,
    }


def _is_live(value: Any) -> bool:
    """Whether an archived ``live`` value means live.

    SQLite stores booleans as 0/1 and the archive carries what the snapshot
    held, so this sees ``1``, ``True`` or ``'1'``. A NULL is treated as LIVE:
    the store column is ``NOT NULL`` with a ``True`` default, so a NULL here
    is an archive artefact and not a statement that the row was superseded.
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "t", "yes")


def _as_text(value: Any) -> str | None:
    """One archived value as text, for a scope comparison.

    MEASURED, not defensive. These columns are declared ``STRING``, and
    SQLite's type-affinity rules give a column named ``STRING`` NUMERIC
    affinity (the name contains none of ``INT``, ``CHAR``, ``CLOB``, ``TEXT``,
    ``BLOB``, ``REAL``, ``FLOA``, ``DOUB``), so the local staging sink hands a
    digit-only value back as a NUMBER. ``query_version`` is exactly that: the
    production run's is ``"4"`` and reads back as the integer ``4``, and
    ``4 != "4"`` would scope a seed to zero rows while reporting success.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _in_scope(
    row: Mapping[str, Any], *, query_name: str | None, query_version: str | None
) -> bool:
    """Whether one archived call is inside the declared scope.

    Scoped on the CALL's own ``query_name``/``query_version``. A legacy row
    with neither is kept and left for the store to resolve against its job --
    this module does not hold the job index, and dropping such a row here
    would silently narrow the cache.
    """
    if query_name is not None and _as_text(row.get("query_name")) not in (
        None,
        query_name,
    ):
        return False
    if query_version is not None and _as_text(row.get("query_version")) not in (
        None,
        query_version,
    ):
        return False
    return True


def _query_types_for(
    sink: Any, identity: Mapping[str, Any], jobs: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The ``query_types`` rows the imported jobs were run against.

    Read by ``(name, version)`` pairs taken from the jobs rather than by
    reading the whole table: a store accumulates query types, and importing
    one a seeded job never used would put a spec in the destination that
    nothing there refers to.
    """
    names = sorted({str(job["query_name"]) for job in jobs if job.get("query_name")})
    if not names:
        return []
    wanted = {
        (_as_text(job.get("query_name")), _as_text(job.get("query_version")))
        for job in jobs
    }
    rows = sink.select_rows("query_types", where={**identity, "name": names})
    return [
        row
        for row in rows
        if (_as_text(row.get("name")), _as_text(row.get("version"))) in wanted
    ]
