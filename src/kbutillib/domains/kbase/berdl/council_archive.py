"""The GENERIC council archiver: a verified snapshot in, ``council.*`` rows out.

THIS MODULE KNOWS NOTHING ABOUT GO, FUNCTIONS OR THE CLEARINGHOUSE. It
archives councils, whatever they are about: ``query_name``/``query_version``
is the only council-kind discriminator anywhere in a council store, and this
code reads it as an opaque label. Selection -- which jobs are a RESULT -- is
a separate layer (:mod:`council_disposition`) and this component writes NO
dispositions.

SNAPSHOT-VERSIONED ARCHIVAL, WHICH IS NOT APPEND-ONLY-BY-JOB. Rows are
archived as IMMUTABLE SNAPSHOT VERSIONS keyed
``(store_id, snapshot_id, <source primary key>)``, where ``snapshot_id`` is
the VERIFIED UNCOMPRESSED DIGEST of the snapshot. A CURRENT PROJECTION is
selected by explicit ``snapshot_seq`` (:func:`current_projection`), never by
``loaded_at``.

An earlier draft of this component said "append-only, idempotent on
``(store_id, job_id)``". That has NO coherent behaviour for a SECOND snapshot
of a still-running store, which is why it was replaced. A later snapshot
re-contains old jobs, possibly-changed paused pilots, tombstones and NEW
CALLS UNDER OLD JOBS -- and ``endpoint_state`` is keyed ``(user, endpoint)``
with mutable cumulative counters, so it is shared by every job in the store
and changes between ANY two snapshots. Skipping known jobs loses later
changes; appending changed rows under the old key violates the declared keys
and DOUBLE-COUNTS SPEND; replacing them is not append-only. Versioning by
snapshot is the only one of the four that is both faithful and idempotent.

THE LOAD LEDGER -- WHAT ESTABLISHES COMPLETION. There is NO single Iceberg
commit for a namespace: the tables commit INDEPENDENTLY, so a crash after
``calls`` and before ``attempts`` leaves convincing but INCOMPLETE
provenance. It reads as a complete archive to every query that does not join
``attempts``. Checking only ``jobs`` gives a false no-op; replaying the
snapshot duplicates committed rows.

So a DELIVERY is persisted (:class:`ArchiveLedger`), bound to the delivery
digest, the namespace and the schema version, recording DETERMINISTIC
table/batch ids with EXPECTED ROW COUNTS AND DIGESTS. On restart each
ambiguous append is resolved FROM ITS BATCH IDENTITY (every archived row
carries its batch's ``load_batch_id``) BEFORE being retried. A delivery is
COMPLETE only when EVERY table reconciles, and only a complete delivery is
publishable (:func:`require_complete_delivery`). Receipts are re-exported
from that completion record (:func:`receipt`), never recomputed.

The shape is ``clearinghouse_ingest``'s, deliberately: its ledger and its
ambiguous-append recovery are the same problem against the same write path,
and a second design would be free to drift from it.

ROLLBACK IS NOT IMPLEMENTED. ``load_batch_id`` exists for idempotency and
provenance only. Nothing here undoes a committed append, and no docstring in
this module says otherwise.

ARCHIVE FAITHFULLY -- each of these is a measured failure, not a preference:

* EVERY JOB in the snapshot is archived, including cancelled, pilot and
  trial. The production store's two CANCELLED 19,276-item jobs are part of
  the record of what was attempted; dropping them here would make the
  disposition layer's "cancelled" declaration refer to nothing.
* ``calls.live`` and ``jobs.deleted_at`` are CARRIED AS COLUMNS and never
  acted on. The production snapshot holds 203,821 calls of which 82,270 are
  the run's live ones, and all ~115,000 null-``reply_text`` calls are
  ``live=0``. Filtering here destroys the only record of what was superseded
  and breaks reuse rehydration -- and ``find_reusable`` does not filter
  ``live`` either, so any filtering of superseded answers belongs wherever a
  reuse cache is built.
* JSON COLUMNS ARE READ AS RAW TEXT, not through SQLAlchemy's ``sa.JSON``.
  The store's JSON columns are ``sa.JSON`` with ``none_as_null=False``, so a
  Python ``None`` is stored as the TEXT ``'null'`` while a real SQL NULL is
  NULL -- and a typed read collapses both to ``None``. Measured on the
  production store: ``calls.reply_parsed`` holds 20,203 substantive objects
  and 188 rows of the literal ``'null'``, so ``reply_parsed IS NOT NULL`` is
  true for EVERY row. Reading raw text keeps the two apart, which is the
  difference between "the model answered null" and "this call never answered".
* Exactly the columns ``council_schema`` declares are projected, including
  the two deliberate ``query_name`` denormalisations, and a CONTRADICTORY
  non-NULL ``query_name`` is REJECTED rather than preferred.
* ``role_brief`` is DERIVED from ``query_types.spec``. A spec shape the
  extractor cannot read is recorded as
  :data:`~.council_schema.ROLE_BRIEF_UNEXTRACTABLE`, never guessed: a guessed
  mission text is indistinguishable from a real one and would misattribute
  instructions to a model that never received them.
* ``endpoint_state`` is written FIRST -- see :data:`ARCHIVER_TABLES`.

BATCHES ARE SIZED BY BYTES, NOT BY ROW COUNT. Spark Connect's
``localRelationCacheThreshold`` is 1 MiB and uploads above it fail
deterministically with "Not enough bytes in channel". ``calls`` rows carry
``reply_text`` and vary by orders of magnitude, so a fixed row count is
either wastefully small or deterministically broken. See
:data:`MAX_BATCH_BYTES`.

NOTHING HERE IS VERIFIED AGAINST A LIVE BERDL POD. The transport has been
broken on kbhub since the 2026-10-19 image (KBUtilLib#91). Implemented and
tested locally against a real SQL sink; anything needing the pod is AWAITING
OPERATOR ACCEPTANCE and a mocked load is never an acceptance.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .council_schema import (
    ARCHIVED_TABLES,
    COUNCIL_NAMESPACE,
    COUNCIL_ENTITY_HASH_COLUMN,
    IDENTITY_COLUMN_NAMES,
    JOB_DISPOSITION_TABLE,
    ROLE_BRIEF_TABLE,
    ROLE_BRIEF_UNEXTRACTABLE,
    ContradictoryQueryNameError,
    attempt_query_name,
    column_names,
    item_query_name,
    table_primary_key,
)
from .council_sink import encode_row

__all__ = [
    "ARCHIVER_TABLES",
    "NOT_WRITTEN_BY_THE_ARCHIVER",
    "STATE_DIR_ENV",
    "DEFAULT_STATE_ROOT",
    "DELIVERY_FILENAME",
    "LEDGER_FILENAME",
    "LEDGER_STARTED",
    "LEDGER_COMMITTED",
    "LEDGER_FAILED",
    "MAX_BATCH_BYTES",
    "SPARK_LOCAL_RELATION_CACHE_THRESHOLD_BYTES",
    "ARCHIVE_SCHEMA_VERSION",
    "CouncilArchiveError",
    "SnapshotConflictError",
    "DeliveryBindingError",
    "DeliveryIncompleteError",
    "AmbiguousBatchError",
    "SnapshotPrepareError",
    "prepare_snapshot",
    "snapshot_row_counts",
    "role_brief_rows",
    "build_inventory",
    "ArchiveLedger",
    "archive_snapshot",
    "reconcile_delivery",
    "is_publishable",
    "require_complete_delivery",
    "receipt",
    "current_projection",
    "reconciliation_report",
]


# --------------------------------------------------------------------------
# What this component writes, and in what order
# --------------------------------------------------------------------------

#: ``job_disposition`` is NOT written here. Selection is a separate layer
#: (:mod:`council_disposition`), and an archiver that also declared what a
#: job's output was FOR would make "the archive says this is published" a
#: statement nobody authored. The table's DDL still belongs to this namespace
#: and is created by ``council_schema.bootstrap``; only its ROWS come from
#: elsewhere.
NOT_WRITTEN_BY_THE_ARCHIVER: tuple[str, ...] = (JOB_DISPOSITION_TABLE,)

#: The tables one archive run writes, IN WRITE ORDER.
#:
#: ``endpoint_state`` IS FIRST, AND THAT IS NOT COSMETIC. It is the ONLY
#: per-gateway token record that exists anywhere: the counters live in the
#: store and nowhere else, so a store wiped before they are archived loses
#: the data the cost-calibration work needs, permanently and unrecoverably.
#: Writing it before anything that could read as completion means a delivery
#: that dies early still carries the one table that cannot be reconstructed.
#:
#: After it, the store mirrors in dependency order, then the derived
#: ``role_brief`` last -- it is a convenience over ``query_types.spec``,
#: which by then is already archived in full, so a crash before it loses
#: nothing that is not still recoverable from the namespace.
ARCHIVER_TABLES: tuple[str, ...] = (
    "endpoint_state",
    "query_types",
    "jobs",
    "items",
    "calls",
    "attempts",
    "workers",
    ROLE_BRIEF_TABLE,
)

#: Small archived tables the snapshot-conflict probe reads, cheapest first.
#: A conflict check must not scan ``calls``; every store that has anything to
#: conflict about has rows in at least one of these.
_CONFLICT_PROBE_TABLES: tuple[str, ...] = (
    "endpoint_state",
    "workers",
    "query_types",
    "jobs",
)

#: The ARCHIVE-side schema version a delivery is bound to. Bumped when the
#: archived column set or the delivery's own semantics change, which is what
#: makes resuming a delivery written by an older build refuse rather than
#: reconcile counts computed against a different shape.
ARCHIVE_SCHEMA_VERSION = 1


# --------------------------------------------------------------------------
# Where delivery state lives
# --------------------------------------------------------------------------

#: Environment variable naming the ROOT under which deliveries keep state.
#: Mirrors ``clearinghouse_ingest.STATE_DIR_ENV``, and for the same reason:
#: the ledger must NOT live beside the snapshot. A snapshot is a large file on
#: scratch that gets moved or deleted; the ledger is the only record of what
#: has been written to the lake and has to outlive it.
STATE_DIR_ENV = "KBU_COUNCIL_ARCHIVE_DIR"

#: Default state root when :data:`STATE_DIR_ENV` is unset.
DEFAULT_STATE_ROOT = "~/.kbu/council/archive/runs"

#: The delivery binding file inside a delivery's state directory.
DELIVERY_FILENAME = "delivery.json"

#: The JSON-lines ledger file inside a delivery's state directory.
LEDGER_FILENAME = "archive_ledger.jsonl"

#: Written BEFORE an append, with ``expected_rows`` and ``expected_digest``
#: already computed from the materialised batch. NON-TERMINAL: a lone
#: ``started`` means the process died around the append and whether the rows
#: landed is UNKNOWN until reconcile asks the table.
LEDGER_STARTED = "started"

#: Terminal: the batch's rows are in the table (or there were none, in which
#: case no append was made at all).
LEDGER_COMMITTED = "committed"

#: Terminal: the batch was refused or its append raised. A failed batch
#: blocks completion; it is NOT silently retried.
LEDGER_FAILED = "failed"

#: Spark Connect's ``localRelationCacheThreshold``. An upload above it fails
#: DETERMINISTICALLY with "Not enough bytes in channel", so it is a hard
#: ceiling and not a tuning hint.
SPARK_LOCAL_RELATION_CACHE_THRESHOLD_BYTES = 1 << 20

#: The byte ceiling one batch is sized to. Half the Spark threshold, because
#: the measure here is the encoded JSON size of the rows and the on-wire
#: Arrow payload is not the same number -- the margin is what keeps an
#: estimate from becoming a deterministic failure. ``calls`` rows carry
#: ``reply_text`` and vary by orders of magnitude, which is why batches are
#: sized by BYTES and never by row count.
MAX_BATCH_BYTES = SPARK_LOCAL_RELATION_CACHE_THRESHOLD_BYTES // 2

#: A batch always carries at least one row, even one larger than
#: :data:`MAX_BATCH_BYTES`. A single oversized row cannot be split, and
#: silently dropping it would lose a reply; it is reported in the batch
#: inventory as oversized so an operator sees it before the pod does.
_MIN_BATCH_ROWS = 1


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class CouncilArchiveError(RuntimeError):
    """An archive run could not proceed."""


class SnapshotConflictError(CouncilArchiveError):
    """This snapshot's identity disagrees with one already archived.

    Two shapes, both refused:

    * the same ``snapshot_seq`` is already archived for this store under a
      DIFFERENT ``snapshot_id`` -- the sequence is the only thing that orders
      snapshots (a digest has no order and ``loaded_at`` is not an order
      either, because replaying an older snapshot after a newer one is
      legitimate), so two snapshots sharing a sequence makes the current
      projection undefined;
    * the same ``snapshot_id`` is already archived under a DIFFERENT
      ``snapshot_seq`` -- the same bytes cannot sit at two points in one
      store's history.
    """


class DeliveryBindingError(CouncilArchiveError):
    """A delivery was resumed against the wrong snapshot, namespace or version.

    A delivery's ``expected_rows`` and per-batch digests are statements about
    ONE snapshot written into ONE namespace at ONE archive schema version.
    Resuming it against another would compare counts from a table the rows
    were never written to.
    """


class DeliveryIncompleteError(CouncilArchiveError):
    """A delivery was asked to be publishable and is not.

    NAMES THE TABLES that did not reconcile. "incomplete" and "calls
    reconciled but attempts holds 0 of 4,211 rows" are the same exit status,
    and only the second says whether the archive is resumable or broken.
    """


class AmbiguousBatchError(CouncilArchiveError):
    """A batch's rows PARTIALLY landed and the append cannot be resolved.

    The ledger says ``started``; the table holds some but not all of the
    batch's rows. Retrying would duplicate the ones that landed and there is
    no rollback, so this stops and says which batch. Resolving it is an
    operator decision about a specific ``load_batch_id``, not something an
    archiver may guess at.
    """


class SnapshotPrepareError(CouncilArchiveError):
    """A snapshot could not be made ready to archive.

    Raised for an absent file, a decompression failure, or an uncompressed
    digest that disagrees with the ``snapshot_id`` the manifest declares. In
    every case the partial output is REMOVED before this is raised: a
    half-decompressed store opens fine in SQLite and is wrong, which is the
    exact silent failure the backup API exists to avoid.
    """


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Getting a snapshot ready
# --------------------------------------------------------------------------


def _file_sha256(path: Path, *, chunk_bytes: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_bytes), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_snapshot(
    source: Any,
    *,
    snapshot_id: str,
    scratch_dir: Any,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Make *source* ready to archive under a DECLARED scratch path.

    Handles a plain snapshot file and an ``.xz``-compressed one (the
    production snapshot is ``council-snapshot-20261023.sqlite.xz``). The
    uncompressed bytes are digested and checked against *snapshot_id*: the
    ``snapshot_id`` every archived row is keyed on is the VERIFIED
    UNCOMPRESSED DIGEST, so a snapshot whose bytes do not match the manifest
    is not the snapshot the manifest describes and nothing may be archived
    from it.

    FAILS CLEAN. On any failure the partial output under *scratch_dir* is
    removed before raising. A half-written SQLite file opens without error
    and contains an inconsistent view, so leaving one behind invites exactly
    the silent wrong answer this whole path exists to prevent.

    Args:
        source: The snapshot file, compressed or not.
        snapshot_id: The expected uncompressed sha256, from the snapshot
            manifest.
        scratch_dir: The DECLARED directory to decompress into. Never a
            temp dir chosen here: the pod holds hundreds of GB but which
            filesystem absorbs ~900 MB is an operator's decision.
        overwrite: Permit replacing an existing prepared file.

    Returns:
        ``{"path", "bytes", "snapshot_id", "decompressed", "verified"}``.

    Raises:
        SnapshotPrepareError: absent source, decompression failure, or a
            digest that disagrees with *snapshot_id*.
    """
    origin = Path(source).expanduser()
    if not origin.is_file():
        raise SnapshotPrepareError(f"{origin}: no such snapshot file")
    scratch = Path(scratch_dir).expanduser()
    scratch.mkdir(parents=True, exist_ok=True)

    compressed = origin.suffix == ".xz"
    if not compressed:
        digest = _file_sha256(origin)
        if digest != snapshot_id:
            raise SnapshotPrepareError(
                f"{origin}: sha256 is {digest!r} but the manifest declares "
                f"snapshot_id {snapshot_id!r}. These are not the same bytes; "
                "every archived row would be keyed on a digest that does not "
                "describe them."
            )
        return {
            "path": str(origin),
            "bytes": origin.stat().st_size,
            "snapshot_id": digest,
            "decompressed": False,
            "verified": True,
        }

    target = scratch / origin.with_suffix("").name
    if target.exists() and not overwrite:
        raise SnapshotPrepareError(
            f"refusing to overwrite {str(target)!r}; pass overwrite=True if "
            "replacing it is really what you mean"
        )
    try:
        import lzma

        with lzma.open(origin, "rb") as reader, target.open("wb") as writer:
            shutil.copyfileobj(reader, writer, length=1 << 20)
        digest = _file_sha256(target)
        if digest != snapshot_id:
            raise SnapshotPrepareError(
                f"{origin}: decompressed sha256 is {digest!r} but the "
                f"manifest declares snapshot_id {snapshot_id!r}."
            )
    except SnapshotPrepareError:
        target.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001 - every failure must leave nothing
        target.unlink(missing_ok=True)
        raise SnapshotPrepareError(
            f"{origin}: could not be decompressed to {target}: {exc}. The "
            "partial output has been removed -- a torn SQLite file opens "
            "fine and is wrong."
        ) from exc
    return {
        "path": str(target),
        "bytes": target.stat().st_size,
        "snapshot_id": digest,
        "decompressed": True,
        "verified": True,
    }


# --------------------------------------------------------------------------
# Reading the snapshot -- RAW TEXT, never through sa.JSON
# --------------------------------------------------------------------------
#
# Opened read-only with sqlite3 rather than through SqliteStore, for two
# reasons. (1) The JSON/NULL distinction: the store's JSON columns are
# sa.JSON with none_as_null=False, so a Python None is stored as the TEXT
# 'null' and a typed read collapses it with a real SQL NULL. A raw read keeps
# them apart. (2) A snapshot may carry a schema version this build would
# refuse to open, and an archive must still be able to read it.


def _open_snapshot(path: Any) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{Path(path).expanduser()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


#: Store columns read for each archived table, as ``{archived: store}``.
#: Anything not listed is either an identity column, a denormalisation, or
#: derived. ``items.entity_hash`` is the ONE rename.
_RENAMES: dict[str, dict[str, str]] = {
    "items": {COUNCIL_ENTITY_HASH_COLUMN: "entity_hash"},
}

#: Archived columns that do NOT come from a store column of the same name.
_DERIVED: dict[str, tuple[str, ...]] = {
    "items": ("query_name",),
    "attempts": ("query_name",),
}


def _store_column(table: str, archived: str) -> str:
    return _RENAMES.get(table, {}).get(archived, archived)


def _source_columns(table: str) -> tuple[str, ...]:
    """The archived columns of *table* that are read straight from the store."""
    derived = set(_DERIVED.get(table, ()))
    return tuple(
        name
        for name in column_names(table)
        if name not in IDENTITY_COLUMN_NAMES and name not in derived
    )


def snapshot_row_counts(path: Any) -> dict[str, int]:
    """Row counts per STORE table in a snapshot -- the full store's counts.

    NOT live-only, and not filtered in any way. These are the numbers the
    reconciliation report compares written rows against.
    """
    connection = _open_snapshot(path)
    try:
        names = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        counts: dict[str, int] = {}
        for table in (
            "query_types",
            "jobs",
            "items",
            "calls",
            "attempts",
            "endpoint_state",
            "workers",
        ):
            if table in names:
                counts[table] = int(
                    connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                )
        return counts
    finally:
        connection.close()


def _job_query_names(connection: sqlite3.Connection) -> dict[str, str | None]:
    """``{job_id: query_name}`` -- the source of both denormalisations."""
    return {
        str(row["job_id"]): row["query_name"]
        for row in connection.execute("SELECT job_id, query_name FROM jobs")
    }


def _call_job_and_query(
    connection: sqlite3.Connection,
) -> dict[str, tuple[str | None, str | None]]:
    """``{call_id: (calls.query_name, job_id)}`` for the attempts join."""
    return {
        str(row["call_id"]): (row["query_name"], row["job_id"])
        for row in connection.execute("SELECT call_id, query_name, job_id FROM calls")
    }


def _read_table(
    connection: sqlite3.Connection,
    table: str,
    *,
    job_query_names: Mapping[str, str | None],
    call_index: Mapping[str, tuple[str | None, str | None]],
) -> Iterator[dict[str, Any]]:
    """Yield one archived row per store row of *table*, ordered by its PK.

    Ordered deterministically by the SOURCE primary key, because the batch
    digests in the delivery inventory are only reproducible on a resume if
    the read order is.
    """
    columns = _source_columns(table)
    order = ", ".join(table_primary_key(table)[2:])
    select = ", ".join(f'"{_store_column(table, name)}"' for name in columns)
    statement = f'SELECT {select} FROM "{table}" ORDER BY {order}'
    for row in connection.execute(statement):
        archived = {
            name: row[_store_column(table, name)] for name in columns
        }
        if table == "items":
            archived["query_name"] = item_query_name(
                job_query_name=job_query_names.get(str(archived["job_id"]))
            )
        elif table == "attempts":
            call_query_name, job_id = call_index.get(
                str(archived["call_id"]), (None, None)
            )
            # Raises ContradictoryQueryNameError on a non-NULL disagreement.
            # Deliberately NOT caught here: a wrong partition key is baked
            # into the physical layout and cannot be corrected without a full
            # rewrite, so the run stops.
            archived["query_name"] = attempt_query_name(
                call_query_name=call_query_name,
                job_query_name=job_query_names.get(str(job_id)),
            )
        yield archived


def role_brief_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """DERIVE ``role_brief`` rows from every ``query_types.spec`` in the store.

    One row per ``(query_name, query_version, role)``. ``mission_text`` is the
    role's system prompt -- what that seat was actually told to do -- and
    ``answer_schema`` is the JSON schema its reply was validated against.

    A SPEC SHAPE THAT CANNOT BE READ IS RECORDED, NOT GUESSED. When ``spec``
    is not an object, carries no ``roles`` mapping, or a role's entry has no
    readable ``system`` text, ``mission_text`` is
    :data:`~.council_schema.ROLE_BRIEF_UNEXTRACTABLE`. A guessed mission text
    is indistinguishable from a real one and would misattribute instructions
    to a model that never received them.

    A spec that is the JSON literal ``'null'`` is one of those shapes, and it
    is the one that would otherwise pass silently: the store writes a Python
    ``None`` as the TEXT ``'null'``, so this reads a real string and has to
    test the PARSED value for ``None`` rather than testing the column for SQL
    NULL.
    """
    rows: list[dict[str, Any]] = []
    for record in connection.execute(
        "SELECT name, version, spec FROM query_types ORDER BY name, version"
    ):
        name, version = str(record["name"]), str(record["version"])
        raw = record["spec"]
        spec: Any = None
        if isinstance(raw, (str, bytes)):
            try:
                spec = json.loads(raw)
            except (ValueError, TypeError):
                spec = None
        elif isinstance(raw, Mapping):
            spec = raw
        # `spec` is None here for a SQL NULL, for unparseable text, AND for
        # the JSON literal 'null' -- all three are unextractable shapes.
        roles = spec.get("roles") if isinstance(spec, Mapping) else None
        if not isinstance(roles, Mapping) or not roles:
            rows.append(
                {
                    "query_name": name,
                    "query_version": version,
                    "role": ROLE_BRIEF_UNEXTRACTABLE,
                    "mission_text": ROLE_BRIEF_UNEXTRACTABLE,
                    "answer_schema": None,
                }
            )
            continue
        for role in sorted(roles):
            entry = roles[role]
            mission = None
            schema = None
            if isinstance(entry, Mapping):
                candidate = entry.get("system")
                if isinstance(candidate, str) and candidate.strip():
                    mission = candidate
                schema = entry.get("schema")
            rows.append(
                {
                    "query_name": name,
                    "query_version": version,
                    "role": str(role),
                    "mission_text": mission
                    if mission is not None
                    else ROLE_BRIEF_UNEXTRACTABLE,
                    "answer_schema": schema,
                }
            )
    return rows


# --------------------------------------------------------------------------
# Batching, by BYTES
# --------------------------------------------------------------------------


def _row_bytes(table: str, row: Mapping[str, Any]) -> int:
    return len(
        json.dumps(encode_row(table, row), sort_keys=True, default=str).encode("utf-8")
    )


def _batch_digest(table: str, rows: Sequence[Mapping[str, Any]]) -> str:
    """A reproducible sha256 over one batch's canonicalised rows.

    Over the SOURCE columns only -- not the identity block. ``loaded_at`` is
    wall-clock and ``load_batch_id`` is this batch's own id, so including
    them would make the digest unreproducible on the resume that needs to
    compare it.
    """
    digest = hashlib.sha256()
    for row in rows:
        payload = {
            name: row.get(name)
            for name in column_names(table)
            if name not in IDENTITY_COLUMN_NAMES
        }
        digest.update(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        )
        digest.update(b"\x1e")
    return digest.hexdigest()


def batch_id(snapshot_id: str, table: str, index: int) -> str:
    """The DETERMINISTIC id of one batch.

    Deterministic so a resumed run computes the SAME id for the same batch
    and can therefore ask the table whether that batch's rows landed. A
    random id per attempt would make every interrupted append permanently
    ambiguous.
    """
    return f"{snapshot_id[:16]}-{table}-{index:05d}"


def _batched(
    table: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    max_bytes: int,
) -> Iterator[list[dict[str, Any]]]:
    """Group *rows* into batches whose encoded size stays under *max_bytes*."""
    batch: list[dict[str, Any]] = []
    size = 0
    for row in rows:
        row_size = _row_bytes(table, row)
        if batch and size + row_size > max_bytes and len(batch) >= _MIN_BATCH_ROWS:
            yield batch
            batch, size = [], 0
        batch.append(dict(row))
        size += row_size
    if batch:
        yield batch


# --------------------------------------------------------------------------
# The delivery and its ledger
# --------------------------------------------------------------------------


def resolve_state_root(root: Any = None) -> Path:
    base = root or os.environ.get(STATE_DIR_ENV) or DEFAULT_STATE_ROOT
    return Path(base).expanduser()


def delivery_state_dir(
    *, store_id: str, snapshot_id: str, root: Any = None
) -> Path:
    """One delivery's state directory: keyed on the store AND the snapshot."""
    return resolve_state_root(root) / store_id / snapshot_id


class ArchiveLedger:
    """One delivery's append-only, fsynced JSON-lines ledger.

    Same discipline as ``clearinghouse_ingest.ExecutionLedger``: every line
    is flushed AND ``fsync``ed before the call returns, lines are appended
    and never rewritten, and the LAST line for a batch id is its state. A
    ``started`` line still in the page cache when the process dies is worse
    than no ledger at all -- a resume would see no trace of a batch whose
    rows had landed and would append them twice, with no rollback to undo it.

    A malformed trailing line is skipped rather than fatal: a crash mid-write
    leaves at most one, and failing a resume over it would be absurd.
    """

    def __init__(self, state_dir: Any) -> None:
        self.state_dir = Path(state_dir).expanduser()
        self.path = self.state_dir / LEDGER_FILENAME
        self.delivery_path = self.state_dir / DELIVERY_FILENAME

    # -- the binding -----------------------------------------------------

    def write_delivery(self, binding: Mapping[str, Any]) -> dict[str, Any]:
        """Persist this delivery's binding and inventory. Idempotent-by-check."""
        record = dict(binding)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        existing = self.read_delivery()
        if existing:
            for key in ("store_id", "snapshot_id", "snapshot_seq", "namespace",
                        "archive_schema_version"):
                if existing.get(key) != record.get(key):
                    raise DeliveryBindingError(
                        f"delivery at {self.state_dir} is bound to "
                        f"{key}={existing.get(key)!r} and this run says "
                        f"{record.get(key)!r}. A delivery's expected row "
                        "counts and batch digests are statements about ONE "
                        "snapshot in ONE namespace at ONE archive schema "
                        "version; reconciling them against another compares "
                        "counts from a table the rows were never written to."
                    )
            return existing
        with self.delivery_path.open("w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def read_delivery(self) -> dict[str, Any]:
        if not self.delivery_path.is_file():
            return {}
        try:
            return json.loads(self.delivery_path.read_text(encoding="utf-8"))
        except (ValueError, TypeError):
            return {}

    # -- the lines -------------------------------------------------------

    def append(self, record: Mapping[str, Any]) -> dict[str, Any]:
        line = dict(record)
        line.setdefault("at", _utcnow_iso())
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return line

    def records(self) -> dict[str, dict[str, Any]]:
        """The LAST record per ``batch_id``."""
        out: dict[str, dict[str, Any]] = {}
        if not self.path.is_file():
            return out
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
            if not record.get("batch_id") or not record.get("state"):
                continue
            out[str(record["batch_id"])] = record
        return out

    def state_of(self, batch: str) -> str | None:
        record = self.records().get(batch)
        return None if record is None else str(record.get("state"))


def build_inventory(
    path: Any,
    *,
    snapshot_id: str,
    max_bytes: int = MAX_BATCH_BYTES,
) -> dict[str, Any]:
    """Derive a delivery's per-table batch inventory from the snapshot.

    This is the delivery's statement of what it is going to write: per table,
    the deterministic batch ids in order, each with its EXPECTED ROW COUNT
    and its EXPECTED DIGEST over the batch's source columns. Those three
    together are what makes an interrupted append resolvable -- a count alone
    cannot tell a partially-landed batch from a batch of a different size.

    Reads the whole snapshot once and materialises each table's batches, so a
    store whose ``calls`` table does not fit in memory needs a streaming
    variant; the production snapshot is ~900 MB and does.
    """
    connection = _open_snapshot(path)
    try:
        job_query_names = _job_query_names(connection)
        call_index = _call_job_and_query(connection)
        tables: dict[str, Any] = {}
        for table in ARCHIVER_TABLES:
            if table == ROLE_BRIEF_TABLE:
                rows: Iterable[Mapping[str, Any]] = role_brief_rows(connection)
            else:
                rows = _read_table(
                    connection,
                    table,
                    job_query_names=job_query_names,
                    call_index=call_index,
                )
            batches = []
            total = 0
            for index, batch in enumerate(_batched(table, rows, max_bytes=max_bytes)):
                size = sum(_row_bytes(table, row) for row in batch)
                batches.append(
                    {
                        "batch_id": batch_id(snapshot_id, table, index),
                        "table": table,
                        "index": index,
                        "expected_rows": len(batch),
                        "expected_digest": _batch_digest(table, batch),
                        "bytes": size,
                        "oversized": size > max_bytes and len(batch) == 1,
                    }
                )
                total += len(batch)
            tables[table] = {"expected_rows": total, "batches": batches}
        return {"tables": tables, "max_batch_bytes": max_bytes}
    finally:
        connection.close()


# --------------------------------------------------------------------------
# Conflict detection
# --------------------------------------------------------------------------


def _archived_snapshots(sink: Any, *, store_id: str) -> dict[str, int]:
    """``{snapshot_id: snapshot_seq}`` already archived for *store_id*.

    Probes the SMALL archived tables, cheapest first, and stops at the first
    that answers. A conflict check must never scan ``calls``.
    """
    for table in _CONFLICT_PROBE_TABLES:
        rows = sink.select_rows(
            table,
            where={"store_id": store_id},
            columns=("snapshot_id", "snapshot_seq"),
        )
        if rows:
            return {
                str(row["snapshot_id"]): int(row["snapshot_seq"]) for row in rows
            }
    return {}


def _check_snapshot_conflict(
    sink: Any, *, store_id: str, snapshot_id: str, snapshot_seq: int
) -> dict[str, int]:
    archived = _archived_snapshots(sink, store_id=store_id)
    for other_id, other_seq in archived.items():
        if other_seq == int(snapshot_seq) and other_id != snapshot_id:
            raise SnapshotConflictError(
                f"refusing to archive snapshot {snapshot_id!r} at "
                f"snapshot_seq {snapshot_seq}: store {store_id!r} already "
                f"has snapshot {other_id!r} at that sequence. snapshot_seq "
                "is the ONLY thing that orders a store's snapshots -- a "
                "digest has no order and loaded_at is not one either, since "
                "replaying an older snapshot after a newer one is "
                "legitimate -- so two snapshots sharing a sequence leaves "
                "the current projection undefined."
            )
        if other_id == snapshot_id and other_seq != int(snapshot_seq):
            raise SnapshotConflictError(
                f"refusing to archive snapshot {snapshot_id!r} at "
                f"snapshot_seq {snapshot_seq}: it is already archived for "
                f"store {store_id!r} at sequence {other_seq}. The same bytes "
                "cannot sit at two points in one store's history."
            )
    return archived


# --------------------------------------------------------------------------
# The archive run
# --------------------------------------------------------------------------


def archive_snapshot(
    sink: Any,
    manifest: Mapping[str, Any],
    *,
    snapshot_seq: int,
    path: Any = None,
    state_root: Any = None,
    namespace: str = COUNCIL_NAMESPACE,
    max_bytes: int = MAX_BATCH_BYTES,
    table_writer: Any = None,
) -> dict[str, Any]:
    """Archive one verified snapshot into the council namespace.

    IDENTITY COMES FROM THE MANIFEST, NEVER FROM THE FILE PATH.
    ``store_id``, ``snapshot_id`` and ``store_host`` are read out of
    *manifest* -- the dict ``SqliteStore.snapshot()`` returns -- because a
    path-derived id breaks every join the moment the store moves off
    ``/scratch``, and because the digest in the manifest is the VERIFIED one.

    RESUMABLE AND NOT REPLAYABLE. Each batch is written between a ``started``
    and a ``committed`` ledger line. A resume reads the ledger first: a batch
    already ``committed`` is skipped, and a batch left ``started`` is
    RESOLVED FROM ITS BATCH IDENTITY against the table -- all its rows
    present means it committed, none present means it is retried, and
    SOME present raises :class:`AmbiguousBatchError` rather than duplicating
    them. There is no rollback.

    Args:
        sink: A council sink (see :mod:`council_sink`).
        manifest: ``SqliteStore.snapshot()``'s manifest: ``store_id``,
            ``snapshot_id``, ``store_host``, ``path``.
        snapshot_seq: The EXPLICIT monotonic sequence for this snapshot.
        path: The snapshot file to read. Defaults to ``manifest["path"]``.
        state_root: Root for the delivery's state directory.
        namespace: The namespace this delivery is bound to (recorded, and
            checked on resume).
        max_bytes: Batch byte ceiling. See :data:`MAX_BATCH_BYTES`.
        table_writer: Optional ``(sink, table, rows) -> result`` override,
            used by the crash-recovery tests to interrupt the REAL archiver
            between two tables' commits. Defaults to ``sink.append_rows``.

    Returns:
        A report: ``delivery``, ``complete``, ``publishable``, per-table
        ``tables`` outcomes, ``reconciliation`` and ``resumed``.

    Raises:
        SnapshotConflictError: a conflicting ``snapshot_seq`` or digest.
        DeliveryBindingError: a resume against a different binding.
        AmbiguousBatchError: a partially-landed batch.
        ContradictoryQueryNameError: a call and its job disagree on a
            non-NULL ``query_name``.
    """
    store_id = str(manifest["store_id"])
    snapshot_id = str(manifest["snapshot_id"])
    store_host = str(manifest.get("store_host") or "")
    source = Path(path or manifest["path"]).expanduser()
    seq = int(snapshot_seq)

    _check_snapshot_conflict(
        sink, store_id=store_id, snapshot_id=snapshot_id, snapshot_seq=seq
    )

    state_dir = delivery_state_dir(
        store_id=store_id, snapshot_id=snapshot_id, root=state_root
    )
    ledger = ArchiveLedger(state_dir)
    inventory = build_inventory(source, snapshot_id=snapshot_id, max_bytes=max_bytes)
    binding = ledger.write_delivery(
        {
            "store_id": store_id,
            "snapshot_id": snapshot_id,
            "snapshot_seq": seq,
            "store_host": store_host,
            "namespace": namespace,
            "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
            "tables": list(ARCHIVER_TABLES),
            "not_written_here": list(NOT_WRITTEN_BY_THE_ARCHIVER),
            "created_at": _utcnow_iso(),
            "inventory": inventory,
        }
    )
    # A resume uses the ORIGINAL inventory: its batch ids and digests are
    # what the ledger's lines refer to.
    inventory = binding.get("inventory") or inventory

    write = table_writer if table_writer is not None else (
        lambda target_sink, table, rows: target_sink.append_rows(table, rows)
    )

    connection = _open_snapshot(source)
    resumed: list[str] = []
    outcomes: dict[str, Any] = {}
    try:
        job_query_names = _job_query_names(connection)
        call_index = _call_job_and_query(connection)
        for table in ARCHIVER_TABLES:
            planned = inventory["tables"][table]["batches"]
            if table == ROLE_BRIEF_TABLE:
                rows_iter: Iterable[Mapping[str, Any]] = role_brief_rows(connection)
            else:
                rows_iter = _read_table(
                    connection,
                    table,
                    job_query_names=job_query_names,
                    call_index=call_index,
                )
            written = 0
            skipped = 0
            per_batch: list[dict[str, Any]] = []
            for index, batch in enumerate(
                _batched(table, rows_iter, max_bytes=inventory["max_batch_bytes"])
            ):
                plan = planned[index] if index < len(planned) else {
                    "batch_id": batch_id(snapshot_id, table, index),
                    "expected_rows": len(batch),
                    "expected_digest": _batch_digest(table, batch),
                }
                this_batch = str(plan["batch_id"])
                state = ledger.state_of(this_batch)
                if state == LEDGER_COMMITTED:
                    skipped += int(plan["expected_rows"])
                    per_batch.append({"batch_id": this_batch, "action": "skipped"})
                    continue
                if state == LEDGER_STARTED:
                    # THE AMBIGUOUS APPEND. Resolve from the batch's identity
                    # BEFORE retrying it: every archived row carries its
                    # batch's load_batch_id.
                    landed = sink.count_rows(
                        table,
                        where={
                            "store_id": store_id,
                            "snapshot_id": snapshot_id,
                            "load_batch_id": this_batch,
                        },
                    )
                    if landed == int(plan["expected_rows"]):
                        ledger.append(
                            {
                                "batch_id": this_batch,
                                "table": table,
                                "state": LEDGER_COMMITTED,
                                "observed_rows": landed,
                                "resolved": "already-landed",
                            }
                        )
                        resumed.append(this_batch)
                        skipped += landed
                        per_batch.append(
                            {"batch_id": this_batch, "action": "resolved-committed"}
                        )
                        continue
                    if landed:
                        raise AmbiguousBatchError(
                            f"batch {this_batch!r} of table {table!r} holds "
                            f"{landed} of {plan['expected_rows']} expected "
                            "rows. The ledger says it started and the table "
                            "says it partly landed. Retrying would duplicate "
                            "the rows that did land and there is NO "
                            "ROLLBACK, so this stops. Resolving it is a "
                            "decision about this load_batch_id."
                        )
                    resumed.append(this_batch)
                rows = [
                    _with_identity(
                        table,
                        row,
                        store_id=store_id,
                        snapshot_id=snapshot_id,
                        snapshot_seq=seq,
                        store_host=store_host,
                        load_batch_id=this_batch,
                    )
                    for row in batch
                ]
                ledger.append(
                    {
                        "batch_id": this_batch,
                        "table": table,
                        "state": LEDGER_STARTED,
                        "expected_rows": int(plan["expected_rows"]),
                        "expected_digest": plan["expected_digest"],
                    }
                )
                try:
                    write(sink, table, rows)
                except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
                    ledger.append(
                        {
                            "batch_id": this_batch,
                            "table": table,
                            "state": LEDGER_FAILED,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    raise
                ledger.append(
                    {
                        "batch_id": this_batch,
                        "table": table,
                        "state": LEDGER_COMMITTED,
                        "observed_rows": len(rows),
                    }
                )
                written += len(rows)
                per_batch.append(
                    {"batch_id": this_batch, "action": "written", "rows": len(rows)}
                )
            outcomes[table] = {
                "written": written,
                "skipped": skipped,
                "expected": int(inventory["tables"][table]["expected_rows"]),
                "batches": per_batch,
            }
        report = reconciliation_report(
            connection,
            sink,
            store_id=store_id,
            snapshot_id=snapshot_id,
            inventory=inventory,
        )
    finally:
        connection.close()

    status = reconcile_delivery(sink, ledger)
    return {
        "delivery": {
            "store_id": store_id,
            "snapshot_id": snapshot_id,
            "snapshot_seq": seq,
            "namespace": namespace,
            "state_dir": str(state_dir),
        },
        "complete": status["complete"],
        "publishable": status["complete"],
        "tables": outcomes,
        "resumed": resumed,
        "reconciliation": report,
        "status": status,
    }


def _with_identity(
    table: str,
    row: Mapping[str, Any],
    *,
    store_id: str,
    snapshot_id: str,
    snapshot_seq: int,
    store_host: str,
    load_batch_id: str,
) -> dict[str, Any]:
    """Lead *row* with the six identity columns ``council_schema`` declares."""
    out = {
        "store_id": store_id,
        "snapshot_id": snapshot_id,
        "snapshot_seq": snapshot_seq,
        "store_host": store_host,
        "loaded_at": _utcnow_iso(),
        "load_batch_id": load_batch_id,
    }
    out.update(row)
    return out


# --------------------------------------------------------------------------
# Reconciliation and completion
# --------------------------------------------------------------------------


def reconcile_delivery(sink: Any, ledger: ArchiveLedger) -> dict[str, Any]:
    """Ask every table whether this delivery's rows are there. The completion test.

    A delivery is COMPLETE only when EVERY table in
    :data:`ARCHIVER_TABLES` reconciles: the table holds exactly the row count
    its inventory expects, under this delivery's ``(store_id, snapshot_id)``.
    Checking one table is what makes a crash between ``calls`` and
    ``attempts`` read as a complete archive.

    Returns:
        ``{"complete", "tables", "incomplete", "failed_batches",
        "started_batches"}``.
    """
    binding = ledger.read_delivery()
    if not binding:
        return {
            "complete": False,
            "tables": {},
            "incomplete": list(ARCHIVER_TABLES),
            "failed_batches": [],
            "started_batches": [],
            "reason": "no delivery binding",
        }
    store_id = str(binding["store_id"])
    snapshot_id = str(binding["snapshot_id"])
    inventory = binding.get("inventory") or {"tables": {}}
    records = ledger.records()
    failed = sorted(
        key for key, record in records.items()
        if str(record.get("state")) == LEDGER_FAILED
    )
    started = sorted(
        key for key, record in records.items()
        if str(record.get("state")) == LEDGER_STARTED
    )
    tables: dict[str, Any] = {}
    incomplete: list[str] = []
    for table in ARCHIVER_TABLES:
        expected = int(
            (inventory["tables"].get(table) or {}).get("expected_rows") or 0
        )
        observed = sink.count_rows(
            table, where={"store_id": store_id, "snapshot_id": snapshot_id}
        )
        ok = observed == expected
        tables[table] = {
            "expected_rows": expected,
            "observed_rows": observed,
            "reconciled": ok,
        }
        if not ok:
            incomplete.append(table)
    return {
        "complete": not incomplete and not failed and not started,
        "tables": tables,
        "incomplete": incomplete,
        "failed_batches": failed,
        "started_batches": started,
    }


def is_publishable(sink: Any, ledger: ArchiveLedger) -> bool:
    """Whether this delivery may be published from. Complete, or nothing."""
    return bool(reconcile_delivery(sink, ledger)["complete"])


def require_complete_delivery(
    sink: Any,
    *,
    store_id: str,
    snapshot_id: str,
    state_root: Any = None,
) -> dict[str, Any]:
    """Return the completion record for a snapshot, or REFUSE naming what is missing.

    This is the check a publisher calls -- including the publication bundle
    (t8), which must refuse a bundle naming an incompletely-archived
    ``snapshot_id``. There is exactly ONE completeness test and it is this
    one: a second one would be free to disagree, and the failure it would
    permit (publishing against provenance that reads complete and is not) is
    the one the ledger exists to prevent.

    Raises:
        DeliveryIncompleteError: no delivery, or an incomplete one. The
            message names every table that did not reconcile with its
            observed and expected counts.
    """
    ledger = ArchiveLedger(
        delivery_state_dir(
            store_id=store_id, snapshot_id=snapshot_id, root=state_root
        )
    )
    status = reconcile_delivery(sink, ledger)
    if status["complete"]:
        return receipt(sink, ledger)
    detail = ", ".join(
        f"{table}: {status['tables'][table]['observed_rows']} of "
        f"{status['tables'][table]['expected_rows']} rows"
        for table in status["incomplete"]
    ) or status.get("reason", "no delivery binding")
    raise DeliveryIncompleteError(
        f"snapshot {snapshot_id!r} of store {store_id!r} is NOT completely "
        f"archived and may not be published from. Unreconciled -- {detail}. "
        f"Batches left started: {status['started_batches']!r}; failed: "
        f"{status['failed_batches']!r}. The nine namespace tables commit "
        "INDEPENDENTLY, so a partial archive reads as a complete one to "
        "every query that does not join the missing table."
    )


def receipt(sink: Any, ledger: ArchiveLedger) -> dict[str, Any]:
    """RE-EXPORT the delivery's completion record. Never recomputed from scratch.

    A receipt is a statement that this delivery reconciled, so it is built
    from the delivery binding and the reconciliation that established it --
    not from a fresh count that might answer differently and leave two
    receipts for one delivery disagreeing.
    """
    binding = ledger.read_delivery()
    status = reconcile_delivery(sink, ledger)
    return {
        "store_id": binding.get("store_id"),
        "snapshot_id": binding.get("snapshot_id"),
        "snapshot_seq": binding.get("snapshot_seq"),
        "namespace": binding.get("namespace"),
        "archive_schema_version": binding.get("archive_schema_version"),
        "store_host": binding.get("store_host"),
        "complete": status["complete"],
        "tables": status["tables"],
        "not_written_here": binding.get("not_written_here") or [],
        "issued_at": _utcnow_iso(),
        "state_dir": str(ledger.state_dir),
    }


# --------------------------------------------------------------------------
# The current projection
# --------------------------------------------------------------------------


def current_projection(
    sink: Any,
    table: str,
    *,
    store_id: str,
    where: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Rows of *table* from the store's HIGHEST ``snapshot_seq``.

    Selected by EXPLICIT ``snapshot_seq``, never by ``loaded_at``: replaying
    an older snapshot after a newer one is legitimate, so load time does not
    order snapshots. Nothing is overwritten to produce this -- every snapshot
    stays independently queryable by naming its own ``snapshot_id``.
    """
    seqs = sink.select_rows(
        table, where={"store_id": store_id}, columns=("snapshot_id", "snapshot_seq")
    )
    if not seqs:
        return []
    latest = max(int(row["snapshot_seq"]) for row in seqs)
    snapshot_id = next(
        str(row["snapshot_id"])
        for row in seqs
        if int(row["snapshot_seq"]) == latest
    )
    predicate = {"store_id": store_id, "snapshot_id": snapshot_id}
    predicate.update(where or {})
    return sink.select_rows(table, where=predicate)


# --------------------------------------------------------------------------
# The per-run reconciliation report
# --------------------------------------------------------------------------


def reconciliation_report(
    connection: sqlite3.Connection,
    sink: Any,
    *,
    store_id: str,
    snapshot_id: str,
    inventory: Mapping[str, Any],
) -> dict[str, Any]:
    """Rows read and written per table, plus the counts that are easy to get wrong.

    DISTINCT-ENTITY COUNTS, NOT ROW SUMS. Summing ``items`` rows across jobs
    DOUBLE-COUNTS every retried item: the two retry jobs re-run a subset of
    the ten chunks, which is how a previous manifest reported 19,405 items
    where the truth over unique functions is 19,276 submitted and 19,268
    decided. Retry rounds are identified by ``jobs.parent_job_id``.

    The ``calls`` live/not-live split is reported for the same reason the
    column is archived: 203,821 rows of which 82,270 are the run's live ones,
    and a reader who did not see the split would read the row count as the
    run's size.

    ``endpoint_state`` rows are POINT-IN-TIME OBSERVATIONS of a running
    total, NEVER additive across snapshots -- see this module's docstring and
    the comment where they are written.
    """
    def _scalar(sql: str) -> int:
        return int(connection.execute(sql).fetchone()[0])

    read = {
        table: int((inventory["tables"].get(table) or {}).get("expected_rows") or 0)
        for table in ARCHIVER_TABLES
    }
    written = {
        table: sink.count_rows(
            table, where={"store_id": store_id, "snapshot_id": snapshot_id}
        )
        for table in ARCHIVER_TABLES
    }
    live = _scalar("SELECT COUNT(*) FROM calls WHERE live")
    not_live = _scalar("SELECT COUNT(*) FROM calls WHERE NOT live")
    items_by_status = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT status, COUNT(*) FROM items GROUP BY status"
        )
    }
    retry_jobs = [
        str(row[0])
        for row in connection.execute(
            "SELECT job_id FROM jobs WHERE parent_job_id IS NOT NULL ORDER BY job_id"
        )
    ]
    return {
        "store_id": store_id,
        "snapshot_id": snapshot_id,
        "rows_read": read,
        "rows_written": written,
        "reconciled": {
            table: read[table] == written[table] for table in ARCHIVER_TABLES
        },
        "calls": {"live": live, "not_live": not_live, "rows": live + not_live},
        "items_by_status": items_by_status,
        "items_rows": sum(items_by_status.values()),
        # The honest size of the dataset. Not the row sum: a retried item
        # appears under its chunk AND under its retry job.
        "distinct_items": _scalar("SELECT COUNT(DISTINCT item_id) FROM items"),
        "distinct_decided_items": _scalar(
            "SELECT COUNT(DISTINCT item_id) FROM items WHERE status = 'ok'"
        ),
        "retry_jobs": retry_jobs,
        "jobs": _scalar("SELECT COUNT(*) FROM jobs"),
        "cancelled_jobs": _scalar(
            "SELECT COUNT(*) FROM jobs WHERE status = 'cancelled'"
        ),
        "tombstoned_jobs": _scalar(
            "SELECT COUNT(*) FROM jobs WHERE deleted_at IS NOT NULL"
        ),
        "endpoint_state_note": (
            "point-in-time observations of a running total; NEVER additive "
            "across snapshots"
        ),
        "job_disposition": "not written by the archiver (see council_disposition)",
    }
