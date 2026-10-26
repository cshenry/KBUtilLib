"""The council store: one local SQLite file, and the only implementation.

One file per machine, on LOCAL disk, holding that machine's jobs, items, call
rows and verdicts.  There is no shared store and no second dialect (review
round 2), so this module and :mod:`.schema` are the whole persistence layer.

Four SQLite-specific things are handled here and nowhere else:

* **Pragmas.**  ``journal_mode=WAL`` so a reader never blocks the worker's
  writes, ``foreign_keys=ON`` because SQLite leaves them off by default and
  the schema relies on them, and ``busy_timeout=30000`` so a foreground run
  and a daemon contending for the same file wait rather than fail.
* **BEGIN IMMEDIATE.**  pysqlite's default deferred transaction upgrades to a
  write lock only at the first write, which turns a read-then-write (claim a
  call, count tokens, advance an item) into a lost update under contention.
  The connection is put in autocommit and this class emits its own ``BEGIN
  IMMEDIATE``, taking the write lock up front.
* **Refusing a synced path.**  A live SQLite database is a file plus a
  write-ahead log; a file syncer uploads them mid-transaction and later
  "resolves" the pair into a corrupt database.  A path with a Dropbox
  component is refused at open time rather than discovered as a corrupted
  council database weeks later.
* **Refusing a network filesystem.**  NFS, CIFS/SMB, sshfs and friends either
  do not implement the POSIX locks SQLite needs or implement them in a way
  that silently does not hold, which corrupts a WAL database under
  concurrency.  The filesystem type of the store path's nearest existing
  ancestor is checked at open time (see :func:`refuse_network_filesystem`).
* **The store's declared ARCHIVE IDENTITY.**  A store that is going to be
  archived into ``kbaseincubator.council`` needs a durable ``store_id``,
  because ``job_id`` is unique only WITHIN one store (which is also why
  ``endpoint_state.user`` is the fixed literal :data:`STORE_IDENTITY`).  That
  id is persisted INSIDE the database file -- see "Archive identity" further
  down -- so a file copy carries it, and :meth:`SqliteStore.snapshot` and
  :meth:`SqliteStore.export` refuse to run without it.

The queue half -- claiming, completing, releasing and reclaiming calls, and
the job control built on them -- is here too, but its SQL is not: every
statement it runs is built in :mod:`.queries`, which holds the queue's
*meaning* without any locking, and this module contributes the lock (``BEGIN
IMMEDIATE``, above) that makes a claim atomic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import sqlite3
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
)

import sqlalchemy as sa

from . import queries as q
from . import schema as sch
from .errors import (
    JobCancelled,
    QueryTypeExists,
    SchemaVersionMismatch,
    SeedDestinationNotEmpty,
    SeedError,
    SeedMultiOrigin,
    StaleClaim,
    StoreError,
    StorePathRefused,
)

__all__ = [
    "SqliteStore",
    "STORE_IDENTITY",
    "SEED_JOB_STATUS",
    "SEED_PROVENANCE_KEY",
    "SEED_TABLES",
    "STORE_IDENTITY_TABLE",
    "STORE_IDENTITY_RECORD_VERSION",
    "POPLAR_COUNCIL_STORE_ID",
    "StoreIdentityError",
    "StoreIdentityMissing",
    "StoreIdentityMismatch",
    "StoreIdentityAlreadyDeclared",
    "validate_store_id",
    "declare_identity_once",
    "NETWORK_FILESYSTEMS",
    "ALLOW_NETWORK_FS_ENV",
    "filesystem_type_of",
    "nearest_existing_ancestor",
    "refuse_network_filesystem",
]

logger = logging.getLogger(__name__)

#: Re-exported from :mod:`.queries`: the fixed ``endpoint_state.user`` value
#: every row in a store is keyed under.  See that module for why.
STORE_IDENTITY = q.STORE_IDENTITY

#: Window ``job_status`` reports throughput over.
_THROUGHPUT_WINDOW = timedelta(minutes=10)


# ---------------------------------------------------------------------------
# Archive identity
# ---------------------------------------------------------------------------
#
# WHY A STORE NEEDS A DECLARED ID.  Every row archived into the
# ``kbaseincubator.council`` Iceberg namespace is keyed on ``store_id``
# (see ``kbutillib.domains.kbase.berdl.council_schema``), because ``job_id``
# is unique only WITHIN one store -- the same reason ``endpoint_state.user``
# is hard-coded to the literal ``STORE_IDENTITY`` above rather than being a
# per-person key.  Three failure modes follow from getting the id wrong, and
# each one is silent:
#
#   * A PATH-DERIVED id breaks every join the moment the store moves off
#     ``/scratch`` -- and the production store lived on a volume that was at
#     83% of 8.8T, so moving it was always going to happen.
#   * A FRESH id per copy breaks idempotency: re-archiving the same snapshot
#     from a moved file would duplicate every row under a second id instead
#     of landing on the same primary key.
#   * ONE id shared between two INDEPENDENTLY WRITABLE forks silently merges
#     two unrelated histories, and the collision surfaces as a primary-key
#     clash on rows that look legitimate.
#
# And it cannot be corrected after the fact: the clearinghouse slot key
# ``(entity_hash, entity_type, result_type, source, parameter_set_hash)``
# deliberately EXCLUDES ``result_type_version``, so once a payload carrying
# this id lands, a corrected payload in the same slot is silently dropped as
# already-present.  So the id is DECLARED BY A HUMAN, ONCE, and never
# changes.
#
# THERE IS DELIBERATELY NO AUTOMATIC DEFAULT.  Not the hostname, not the
# store path, not a ``uuid4``, not a digest of anything.  An automatic value
# would be indistinguishable from a correct one right up until the first
# store moved, at which point every already-written clearinghouse payload
# would be unjoinable and unfixable.  :func:`validate_store_id` therefore
# also REFUSES values that look automatically derived (a filesystem path, a
# bare uuid, a database filename), because the cheapest way to get an
# automatic default back is for somebody to pass one in by hand.
#
# WHERE THE RECORD LIVES.  Inside the database file, in its own
# :data:`STORE_IDENTITY_TABLE` table -- not in the config, not in a sidecar.
# Config is per-machine and would not travel with a copied file; a sidecar
# would be dropped by every ``cp`` that did not know about it.  Binding the
# record to the database is what makes "a move or a read-only snapshot
# RETAINS the identity" true by construction rather than by procedure.
#
# WHY THAT TABLE IS NOT IN :mod:`.schema`.  It is deliberately outside the
# versioned ``MetaData`` and carries its OWN record version
# (:data:`STORE_IDENTITY_RECORD_VERSION`), for two reasons.  First, every
# store that already exists -- including the one this is for -- was created
# at ``SCHEMA_VERSION = 2`` without it, and ``migrate()`` adds columns and
# indexes, not tables; an identity you can only get by migrating is an
# identity the existing store cannot have.  Second, the identity must be
# READABLE from a store whose schema version this code does not accept
# (``check_version=False``), because "which store did these bytes come
# from" is exactly the question asked about an old file.
#
# The table is APPEND-ONLY and the current identity is its highest
# ``declared_seq``.  A fork's re-declaration therefore keeps the superseded
# id visible instead of erasing it, which is the only way a later reader can
# tell a fork from a store that always had its current id.

#: Name of the identity table inside the store database.
STORE_IDENTITY_TABLE = "store_identity"

#: Version of the identity RECORD shape (not of the council schema).  Bumped
#: only if the record's own columns change; see the note above for why it is
#: versioned separately from :data:`.schema.SCHEMA_VERSION`.
STORE_IDENTITY_RECORD_VERSION = 1

#: The id declared for the EXISTING poplar council store (the one that
#: produced the 2026-10-19 function_to_go run), named here so the one-off
#: migration quotes a constant rather than a string literal typed twice.
#:
#: This is NOT a default and nothing applies it automatically.  The
#: documented, idempotent one-off is::
#:
#:     python -c "from kbutillib.domains.ai.council.store.sqlite import \
#:         declare_identity_once, POPLAR_COUNCIL_STORE_ID; \
#:         print(declare_identity_once(
#:             '/scratch/chenry/council/council.sqlite',
#:             POPLAR_COUNCIL_STORE_ID, declared_by='chenry'))"
#:
#: Running it twice is a no-op; running it against a store already carrying
#: a DIFFERENT id raises :class:`StoreIdentityAlreadyDeclared` rather than
#: overwriting, and exits non-zero.
POPLAR_COUNCIL_STORE_ID = "poplar/council-1"

#: The longest a declared store id may be.  Not a storage limit -- an id this
#: long is a sign somebody pasted a path.
_MAX_STORE_ID_LEN = 128

#: Characters a declared store id may contain.  ``/`` is allowed because the
#: declared ids are namespaced (``poplar/council-1``); whitespace is not,
#: because an id with a space in it will be silently mangled by every shell
#: that touches it.
_STORE_ID_ALLOWED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

#: A bare ``uuid4().hex`` -- 32 hex characters with no structure.
_BARE_UUID_HEX = re.compile(r"^[0-9a-fA-F]{32}$")

#: A canonical dashed UUID.
_DASHED_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: Sentinel meaning "use the identity this store was opened with".  A plain
#: ``None`` cannot mean that, because ``None`` is the meaningful value "no
#: configured identity was supplied, so do not compare".
_USE_CONFIGURED = object()


class StoreIdentityError(StoreError):
    """Base class for every archive-identity refusal."""


class StoreIdentityMissing(StoreIdentityError):
    """The store carries no declared identity, so it may not be archived.

    Raised by :meth:`SqliteStore.snapshot` and :meth:`SqliteStore.export`.
    Deliberately NOT auto-assigned: a snapshot taken under an invented id is
    worse than no snapshot, because it looks joinable.
    """


class StoreIdentityMismatch(StoreIdentityError):
    """The configured identity and the stored identity disagree.

    NEVER auto-healed.  A mismatch means somebody copied a store and kept
    writing to it -- which is exactly the independently-writable-fork case
    that must not be papered over -- or pointed a config at the wrong file.
    Both values are named in the message so the operator can tell which.
    """


class StoreIdentityAlreadyDeclared(StoreIdentityError):
    """A different identity is already declared for this store.

    :meth:`SqliteStore.assign_store_identity` is idempotent for the SAME id
    and refuses a different one.  Replacing a declared identity is the
    independently-writable-fork case and needs the explicit act,
    :meth:`SqliteStore.redeclare_store_identity`.
    """


def validate_store_id(value: Any) -> str:
    """Return *value* as a valid declared store id, or raise.

    The whole value of ``store_id`` is that a human declared it and it never
    changes, so this refuses not only malformed values but values that look
    AUTOMATICALLY DERIVED -- a filesystem path, a bare uuid, a database
    filename.  Those are the shapes an automatic default would have, and
    letting one in by hand reintroduces exactly the failure the absence of a
    default exists to prevent.

    Args:
        value: The candidate id.

    Returns:
        The id, stripped of surrounding whitespace.

    Raises:
        ValueError: The value is not a non-empty string, contains
            whitespace or a disallowed character, is too long, looks like a
            filesystem path or a database filename, or is a bare uuid.
    """
    if not isinstance(value, str):
        raise ValueError(
            f"store_id must be a string a human declared, got "
            f"{type(value).__name__}"
        )
    text = value.strip()
    if not text:
        raise ValueError(
            "store_id is empty; it must be declared explicitly (there is "
            "deliberately no automatic default -- see "
            "kbutillib.domains.ai.council.store.sqlite's 'Archive identity' "
            "note)"
        )
    if len(text) > _MAX_STORE_ID_LEN:
        raise ValueError(
            f"store_id {text[:40]!r}... is {len(text)} characters, over the "
            f"{_MAX_STORE_ID_LEN} limit; an id this long is usually a pasted "
            "path"
        )
    if text.startswith("/") or text.startswith("~") or text.startswith("."):
        raise ValueError(
            f"store_id {text!r} looks like a filesystem path. A path-derived "
            "id breaks every archived join the moment the store moves, which "
            "is the one thing a declared id exists to prevent. Declare a "
            "stable name instead, e.g. 'poplar/council-1'."
        )
    if text.endswith(".sqlite") or text.endswith(".db"):
        raise ValueError(
            f"store_id {text!r} is a database filename, not a declared "
            "identity. Declare a stable name instead, e.g. "
            "'poplar/council-1'."
        )
    if _BARE_UUID_HEX.match(text) or _DASHED_UUID.match(text):
        raise ValueError(
            f"store_id {text!r} is a uuid. A fresh id per copy breaks "
            "idempotency: re-archiving the same snapshot would duplicate "
            "every row under a second id. Declare a stable name instead, "
            "e.g. 'poplar/council-1'."
        )
    if not _STORE_ID_ALLOWED.match(text):
        raise ValueError(
            f"store_id {text!r} must match {_STORE_ID_ALLOWED.pattern} -- "
            "letters, digits, '.', '_', '-' and '/' only, starting with a "
            "letter or digit. No whitespace: an id with a space in it is "
            "mangled by every shell that touches it."
        )
    return text


def declare_identity_once(
    url: str,
    store_id: str,
    *,
    declared_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Declare *store_id* for the store at *url*, idempotently.

    The documented one-off migration path, and the only runnable entry point
    for it -- see :data:`POPLAR_COUNCIL_STORE_ID` for the exact command.
    Opens with ``check_version=False`` on purpose: a store whose schema
    version this code does not accept still needs an identity, because
    "which store did these bytes come from" is precisely the question asked
    about an old file.

    Idempotent: declaring the id a store already carries returns the existing
    record and writes nothing.

    Args:
        url: The store's SQLite URL or bare path.
        store_id: The id to declare, validated by :func:`validate_store_id`.
        declared_by: Who declared it.  Recorded as-is; ``None`` falls back to
            the OS user, since the point of the field is accountability.

    Returns:
        The current identity record.

    Raises:
        ValueError: *store_id* is not a valid declared id.
        StoreIdentityAlreadyDeclared: the store already carries a DIFFERENT
            id.  Not overwritten -- see
            :meth:`SqliteStore.redeclare_store_identity`.
    """
    with SqliteStore(url, check_version=False) as store:
        return store.assign_store_identity(store_id, declared_by=declared_by)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _jsonify(value: Any) -> Any:
    """Reduce *value* to something :data:`sqlalchemy.JSON` can serialise.

    Accepts pydantic models (a ``CouncilVerdict``, a ``Roster``, a list of
    ``MenuEntry``) as well as plain containers, so callers never have to
    remember which shape a column wants.
    """
    if value is None:
        return None
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if isinstance(value, Mapping):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonify(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _prepare(table: sa.Table, row: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate *row*'s keys against *table* and JSON-reduce its JSON columns.

    An unknown key raises rather than being dropped: silently ignoring a
    misspelled column is how a field quietly stops being persisted.
    """
    columns = table.c
    unknown = sorted(set(row) - set(columns.keys()))
    if unknown:
        raise ValueError(
            f"unknown {table.name} column(s) {unknown}; "
            f"known columns are {sorted(columns.keys())}"
        )
    out: Dict[str, Any] = {}
    for key, value in row.items():
        column_type = columns[key].type
        if isinstance(column_type, sa.JSON):
            out[key] = _jsonify(value)
            continue
        if (
            isinstance(column_type, sch.UtcDateTime)
            and isinstance(value, datetime)
            and value.tzinfo is None
        ):
            # UtcDateTime refuses this too, but SQLAlchemy would wrap that in a
            # StatementError at execute time.  Catching it here gives the
            # caller a plain ValueError naming the column they got wrong.
            raise ValueError(
                f"naive datetime for {table.name}.{key}: {value!r}. Council "
                "timestamps must be timezone-aware UTC "
                "(use datetime.now(timezone.utc))"
            )
        out[key] = value
    return out


def _filled(table: sa.Table, row: Mapping[str, Any]) -> Dict[str, Any]:
    """Return *row* widened to every column of *table*.

    ``executemany`` compiles one statement for the whole batch, so the dicts
    must all carry the same keys; widening here means a caller can pass
    heterogeneous partial rows.  Missing values fall back to the column's
    scalar default and then to ``None`` -- a ``None`` in a NOT NULL column is
    meant to surface as an IntegrityError, not to be papered over.
    """
    prepared = _prepare(table, row)
    full: Dict[str, Any] = {}
    for column in table.c:
        if column.name in prepared:
            full[column.name] = prepared[column.name]
        elif column.default is not None and getattr(column.default, "is_scalar", False):
            full[column.name] = column.default.arg
        else:
            full[column.name] = None
    return full


def _export_row(row: Any) -> Dict[str, Any]:
    """One result row as a JSON-serialisable dict, for :meth:`SqliteStore.export`.

    Distinct from :func:`_dict` on purpose: that one hands rows to the
    council's own code, which wants real ``datetime`` objects back.  An
    export has to survive ``json.dumps``, so timestamps become ISO-8601
    strings here and nothing else is touched -- JSON columns are already
    plain Python by the time SQLAlchemy returns them.
    """
    out: Dict[str, Any] = {}
    for key, value in row._mapping.items():
        out[key] = value.isoformat() if isinstance(value, datetime) else value
    return out


def _dict(row: Any) -> Dict[str, Any]:
    return dict(row._mapping)


def _dicts(result: Any) -> List[Dict[str, Any]]:
    return [dict(row._mapping) for row in result]


def _version_sort_key(version: str) -> tuple:
    """Natural sort key for a query-type version string.

    Lexicographic order puts ``"9"`` after ``"10"``, which would make
    ``get_query_type(name)`` return a superseded spec the first time a query
    type reaches version 10.  Digit runs are compared as numbers.
    """
    parts = re.findall(r"\d+|\D+", version or "")
    return tuple((1, int(p)) if p.isdigit() else (0, p) for p in parts)


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _stale_claim(call_id: str, because: str) -> StaleClaim:
    return StaleClaim(
        f"refusing to settle call {call_id}: {because}. The reply is discarded "
        "rather than written over the current claimant's work."
    )


def _serves(worker: Mapping[str, Any], endpoint: str) -> bool:
    """Does *worker* serve *endpoint*?

    ``workers.endpoints`` is the list a worker registered with.  An empty or
    absent list means "did not say", which is read as serving everything: the
    worker IS running, so reporting ``no_daemon`` against it would send the
    user to start a second one for no reason.
    """
    declared = worker.get("endpoints")
    if not declared:
        return True
    return endpoint in set(declared)


def _snapshot_row_counts(path: Path) -> Dict[str, int]:
    """``{table: row count}`` read out of a snapshot FILE.

    Opened read-only with ``sqlite3`` rather than through this store, so the
    counts describe the bytes that were written and cannot pick up a write
    transaction the caller still has open.  A table absent from the file is
    omitted rather than reported as zero: "no such table" and "a table with
    no rows" are different facts about a snapshot.
    """
    counts: Dict[str, int] = {}
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        present = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for name in sch.TABLE_NAMES:
            if name in present:
                counts[name] = int(
                    connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                )
    finally:
        connection.close()
    return counts


# ---------------------------------------------------------------------------
# Seeding a reuse-only store from archived rows
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS, IN MONEY.  The store is not only a record, it is the REUSE
# CACHE: ``calls.prompt_hash`` is the reuse key and :func:`queries.select_reusable`
# serves any ``status='ok'`` row carrying it, across jobs and users.  So wiping
# a store destroys answers somebody paid for -- re-running the ADP1+PMI set
# (19,276 items) at the same query version and roster against an EMPTY store
# re-pays on the order of 199M tokens, where the intact store costs
# approximately nothing.  :meth:`SqliteStore.seed_from_rows` turns "wiping a
# store loses money" into "wiping a store is free", which is the only reason
# archiving a store and then deleting it is a safe thing to do.
#
# FOUR TABLES, AND WHY NOT THE OTHER FOUR.  ``calls`` is the cache; ``jobs``,
# ``items`` and ``query_types`` come along because the imported calls carry
# foreign keys to them (``fk_calls_item`` and ``items.job_id``) and would not
# be referentially valid without them.  ``attempts`` is NOT imported: it
# records what each try cost, which is a fact about the ORIGINAL run and is
# archived in the lakehouse, and importing it would make a seeded store look
# as though this machine had spent that money.  ``endpoint_state`` is
# deliberately not imported for the same reason and a sharper one -- its
# counters are MUTABLE CUMULATIVE totals, so importing them would charge this
# machine's budget for another machine's spend.  ``workers`` describes
# processes that no longer exist.

#: The ``jobs.status`` every imported job carries.  Not one of the queue's
#: own statuses (``queued``/``running``/``done``/``cancelled``/``paused``) on
#: purpose: a seeded job is not work, and a reader seeing it should not think
#: this machine ran it.
SEED_JOB_STATUS = "seeded"

#: The ``jobs.options`` key an imported job's seed provenance is recorded
#: under.  The record lives INSIDE the store (rather than only in a receipt
#: file beside it) because that is what makes a second seed able to tell "this
#: store already holds exactly this seed" from "this store holds somebody
#: else's work", which is the difference between an idempotent no-op and a
#: silent identity collision.
SEED_PROVENANCE_KEY = "kbu_seed"

#: The tables :meth:`SqliteStore.seed_from_rows` reads from its input, in
#: insert order (parents before children).
SEED_TABLES: tuple[str, ...] = ("query_types", "jobs", "items", "calls")

#: Tables whose presence in a destination means it is NOT empty.
#: ``query_types`` is excluded deliberately: it is an immutable registry that
#: every initialised store legitimately carries (``put_query_type`` is
#: idempotent for an identical spec and raises for a conflicting one), so its
#: rows are not evidence of work.
_SEED_EMPTINESS_TABLES: tuple[str, ...] = ("jobs", "items", "calls", "attempts")

#: ``{table: {archived column: store column}}`` -- the archive's one rename.
#: ``items.entity_hash`` is published as ``council_entity_hash`` so nobody
#: joins it to a clearinghouse ``entity_hash`` (the two matched 0 of 19,268
#: times); coming back the other way it is the store column it always was.
_SEED_RENAMES: Dict[str, Dict[str, str]] = {
    "items": {"council_entity_hash": "entity_hash"},
}

#: Archive identity columns, which are the seed's ORIGIN and never a store
#: column.  Named here rather than imported from ``kbase.berdl``: the store
#: layer does not depend on the lakehouse layer, and a seed must work from any
#: source that can produce rows of this shape.
_SEED_IDENTITY_COLUMNS: tuple[str, ...] = (
    "store_id",
    "snapshot_id",
    "snapshot_seq",
    "store_host",
    "loaded_at",
    "load_batch_id",
)


def _seed_datetime(column: str, value: Any) -> Optional[datetime]:
    """Parse one archived TIMESTAMP back into an aware UTC datetime.

    Accepts what either sink can hand back: a ``datetime`` (the pod sink's
    TIMESTAMP columns), or the TEXT a SQLite store holds.  SQLAlchemy's SQLite
    DATETIME writes ``YYYY-MM-DD HH:MM:SS.ffffff`` and DISCARDS ``tzinfo``
    (see :class:`.schema.UtcDateTime`), so an archived timestamp is naive text
    that is known to be UTC -- it is re-tagged, never shifted.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise SeedError(
                f"{column}: {value!r} is not a timestamp this seed can read. "
                "Archived TIMESTAMP columns are ISO-8601; a value that is not "
                "parseable would otherwise be imported as NULL and read as "
                "'never answered'."
            ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _seed_json(column: str, value: Any) -> Any:
    """Parse one archived JSON column back into Python, PRESERVING JSON null.

    The archive reads the store's JSON columns as RAW TEXT, because the store
    writes a Python ``None`` into an ``sa.JSON`` column as the TEXT ``'null'``
    while a real SQL NULL is NULL, and a typed read collapses the two.  Coming
    back, ``'null'`` must therefore parse to ``None`` and be re-written as
    JSON null -- which is what ``_prepare`` does with a ``None`` in a JSON
    column.  Round-tripping ``calls.normalised`` through here is what keeps a
    reused answer's record of what was repaired in it intact; losing that
    column silently changes what a reused answer reports about itself, because
    the reuse path falls back to the SOURCE's ``normalised`` entries when
    re-normalisation produces no new repairs.
    """
    if value is None:
        return None
    if isinstance(value, (Mapping, list, tuple, bool, int, float)):
        return value
    text = str(value)
    if text == "":
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise SeedError(
            f"{column}: {text[:80]!r} is not JSON. An archived JSON column "
            "that cannot be parsed must not be imported as a bare string: "
            "the reuse path would then read a repair record as free text."
        ) from exc


def _seed_text(value: Any) -> Optional[str]:
    """One archived value as the text a scope comparison can use.

    MEASURED, not defensive.  The archived tables declare these columns
    ``STRING``, and SQLite's type-affinity rules give a column declared
    ``STRING`` NUMERIC affinity (the name contains none of ``INT``, ``CHAR``,
    ``CLOB``, ``TEXT``, ``BLOB``, ``REAL``, ``FLOA``, ``DOUB``) -- so the
    local staging sink hands a digit-only value back as a NUMBER.
    ``query_version`` is exactly that: the production run's is ``"4"``, which
    reads back as the integer ``4``, and ``4 != "4"`` would silently scope a
    seed to nothing.  The stored column is ``String(64)`` with TEXT affinity,
    so the value written INTO the store is text either way; only the
    comparison needs this.
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


def _seed_bool(value: Any) -> Optional[bool]:
    """Coerce an archived BOOLEAN back to ``bool``.

    SQLite stores booleans as 0/1 integers and the archive carries whatever
    the snapshot held, so ``live`` arrives as ``0``/``1``, ``False``/``True``
    or (through a text-typed column) ``'0'``/``'1'``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "t", "yes"):
        return True
    if text in ("0", "false", "f", "no", ""):
        return False
    raise SeedError(f"{value!r} is not a boolean an archived row may carry")


def _seed_row(table: sa.Table, row: Mapping[str, Any]) -> Dict[str, Any]:
    """One archived row as a store row of *table*: renamed, typed, identity dropped.

    Unknown columns are DROPPED rather than refused, and that is the one place
    this path is permissive: the archived tables carry the six identity
    columns and two deliberate ``query_name`` denormalisations that have no
    store column, and a future archive column must not make a seed impossible
    to run.  Every column that DOES map is type-converted rather than passed
    through, because a JSON column bound as raw text would be double-encoded
    and a naive datetime is refused outright by :class:`.schema.UtcDateTime`.
    """
    renames = _SEED_RENAMES.get(table.name, {})
    out: Dict[str, Any] = {}
    for key, value in row.items():
        name = renames.get(key, key)
        if name in _SEED_IDENTITY_COLUMNS or name not in table.c:
            continue
        column_type = table.c[name].type
        if isinstance(column_type, sa.JSON):
            out[name] = _seed_json(f"{table.name}.{name}", value)
        elif isinstance(column_type, sch.UtcDateTime):
            out[name] = _seed_datetime(f"{table.name}.{name}", value)
        elif isinstance(column_type, sa.Boolean):
            out[name] = _seed_bool(value)
        elif isinstance(column_type, sa.String):
            # The staging sink's STRING columns have NUMERIC affinity, so a
            # digit-only value (`query_version`, every `"4"` in the production
            # run) comes back as a number.  Coerced here rather than left to
            # the destination column's TEXT affinity, so the row this method
            # returns is already the row the store holds.
            out[name] = _seed_text(value)
        else:
            out[name] = value
    return out


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class SqliteStore:
    """A council store backed by one local SQLite file.

    Args:
        url: A SQLAlchemy SQLite URL (``sqlite:////abs/path/council.sqlite``)
            or a bare filesystem path, which is treated as one.  ``~`` is
            expanded.  ``:memory:`` is accepted for tests.
        create_parents: Create the database file's parent directories.  On by
            default: the store's whole job on a fresh machine is to come up
            without ceremony.
        check_version: Verify the stored schema version at open time.  Pass
            ``False`` from ``kbu council db migrate``, which must be able to
            open precisely the databases this check rejects.
        store_id: The CONFIGURED archive identity -- what the caller believes
            this store is (typically ``CouncilConfig.store_id``).  Optional,
            and there is deliberately NO automatic default (see the
            "Archive identity" note in this module).  When supplied it is
            validated immediately and then CHECKED against the identity
            stored inside the database by :meth:`snapshot` and
            :meth:`export`, which refuse on a disagreement rather than
            auto-healing it.  Passing it does NOT assign it: assignment is an
            explicit act (:meth:`assign_store_identity`).

    Raises:
        StorePathRefused: the path has a Dropbox component.
        SchemaVersionMismatch: the file carries a different schema version.
        ValueError: *store_id* was supplied and is not a valid declared id.
    """

    #: Dialect name, for callers that branch on it.
    dialect = "sqlite"

    def __init__(
        self,
        url: str,
        *,
        create_parents: bool = True,
        check_version: bool = True,
        store_id: Optional[str] = None,
    ) -> None:
        #: The configured identity, validated at open time so a typo in
        #: config fails when the store is opened rather than when an archive
        #: run is half-finished.  ``None`` means "no configured identity was
        #: supplied", which is NOT the same as "the store has none".
        self.configured_store_id = (
            None if store_id is None else validate_store_id(store_id)
        )
        self.url, self.path = _resolve_sqlite_url(url)
        if create_parents and self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self._local = threading.local()
        self._engine = sa.create_engine(self.url, future=True)
        _install_sqlite_pragmas(self._engine)

        if check_version:
            self._assert_schema_version()

    # -- plumbing ---------------------------------------------------------

    @property
    def engine(self) -> sa.Engine:
        """The underlying SQLAlchemy engine (tests and migrations use it)."""
        return self._engine

    def close(self) -> None:
        """Dispose of the connection pool.

        Not part of the store contract -- it exists so a test or a CLI run can
        release the file handle deterministically rather than at GC time.
        """
        self._engine.dispose()

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sa.Connection]:
        """Run a block in one transaction, yielding the connection.

        Re-entrant: a nested call joins the transaction already in progress
        rather than deadlocking against its own write lock.  That is what lets
        the worker complete a call, record its attempt, add tokens and advance
        the item as one unit while each of those is also usable alone.

        *immediate* emits ``BEGIN IMMEDIATE`` (the default), taking SQLite's
        write lock before the first read so a read-then-write cannot lose an
        update to a concurrent worker.
        """
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            yield existing
            return
        conn = self._engine.connect()
        conn.info["council_begin_immediate"] = immediate
        self._local.conn = conn
        try:
            with conn.begin():
                yield conn
        finally:
            self._local.conn = None
            conn.close()

    @contextmanager
    def _read(self) -> Iterator[sa.Connection]:
        """A read-only connection, joining an open transaction if there is one."""
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            yield existing
            return
        with self._engine.connect() as conn:
            conn.info["council_begin_immediate"] = False
            yield conn

    # -- schema -----------------------------------------------------------

    def init_schema(self) -> None:
        """Create every table and stamp the schema version.

        Idempotent: running it on an initialised store does nothing.
        """
        sch.metadata.create_all(self._engine)
        with self.transaction() as conn:
            row = conn.execute(sa.select(sch.schema_version.c.version)).first()
            if row is None:
                conn.execute(
                    sch.schema_version.insert().values(version=sch.SCHEMA_VERSION)
                )
            elif int(row[0]) != sch.SCHEMA_VERSION:
                raise _version_mismatch(int(row[0]))

    def schema_version(self) -> int:
        """Return the stored schema version, or 0 if the store is not initialised."""
        inspector = sa.inspect(self._engine)
        if not inspector.has_table(sch.schema_version.name):
            return 0
        with self._read() as conn:
            row = conn.execute(sa.select(sch.schema_version.c.version)).first()
        return int(row[0]) if row is not None else 0

    def migrate(self) -> int:
        """Bring the database up to :data:`.schema.SCHEMA_VERSION`.

        An unstamped database is simply created (``init_schema()``).  A
        version 1 store -- the shared-store/self-hosted-models schema -- is
        brought to version 2 by :meth:`_migrate_v1_to_v2`, which drops exactly
        the five tables in :data:`.schema.REMOVED_IN_V2` and re-keys
        ``endpoint_state`` onto :data:`STORE_IDENTITY`, inside ONE
        transaction.  Every job, item, call, attempt, workers and query_types
        row is kept untouched.  Running it twice is a no-op, because the
        second run sees version 2 already.

        A database stamped NEWER than this code is refused: downgrading would
        silently drop columns a newer council wrote.

        Both an already-current store and a freshly-migrated v1-to-v2 one also
        get :meth:`add_missing_columns` and :meth:`add_missing_indexes`
        applied, at the same point: a store built by an earlier binary of the
        SAME schema version can be missing either one, and neither omission
        changes the version stamp.
        """
        current = self.schema_version()
        if current == 0:
            self.init_schema()
            return sch.SCHEMA_VERSION
        if current == sch.SCHEMA_VERSION:
            self.add_missing_columns()
            self.add_missing_indexes()
            return current
        if current == 1:
            self._migrate_v1_to_v2()
            self.add_missing_columns()
            self.add_missing_indexes()
            return sch.SCHEMA_VERSION
        raise _version_mismatch(current)

    def _migrate_v1_to_v2(self) -> None:
        """Drop the five retired tables and re-key ``endpoint_state``.

        One transaction, so a store is either fully v1 or fully v2 and never
        half-migrated.  Three things happen in it, in this order:

        1. ``endpoint_state`` rows are merged onto :data:`STORE_IDENTITY`.  A
           v1 store keyed them on the submitting user, and the counters are
           the same machine's spend however many labels it was recorded
           under: epoch and lifetime tokens are SUMMED, the HIGHEST budget
           epoch is kept (a lower one would resurrect a budget that was
           already reset), and every halt is kept, each message prefixed with
           the label it came from, because a halt is a live reason not to
           call an endpoint and discarding any of them would resume a call
           somebody stopped.
        2. ``DROP TABLE IF EXISTS`` for each of
           :data:`.schema.REMOVED_IN_V2`.  ``IF EXISTS`` because a store
           created by one of the builds between them may be missing some.
        3. The version stamp is replaced with 2.
        """
        with self.transaction() as conn:
            self._merge_endpoint_state(conn)
            for table_name in sch.REMOVED_IN_V2:
                conn.execute(sa.text(f"DROP TABLE IF EXISTS {table_name}"))
            conn.execute(sch.schema_version.delete())
            conn.execute(
                sch.schema_version.insert().values(version=sch.SCHEMA_VERSION)
            )

    @staticmethod
    def _merge_endpoint_state(conn: sa.Connection) -> None:
        """Collapse every ``endpoint_state`` row onto :data:`STORE_IDENTITY`."""
        rows = _dicts(conn.execute(sa.select(sch.endpoint_state)))
        by_endpoint: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            by_endpoint.setdefault(str(row["endpoint"]), []).append(row)

        merged: List[Dict[str, Any]] = []
        for endpoint, group in by_endpoint.items():
            # Sorted so the merge is deterministic: the combined halt message
            # must not depend on the order SQLite happened to return rows in.
            group.sort(key=lambda r: str(r["user"]))
            halted = [r for r in group if r["halt_kind"]]
            halt_kind = halted[0]["halt_kind"] if halted else None
            halt_message: Optional[str] = None
            if len(halted) == 1:
                halt_message = halted[0]["halt_message"]
            elif halted:
                halt_message = "; ".join(
                    f"[{r['user']}] {r['halt_message'] or r['halt_kind']}"
                    for r in halted
                )
            updated = [r["updated_at"] for r in group if r["updated_at"] is not None]
            merged.append(
                {
                    "user": STORE_IDENTITY,
                    "endpoint": endpoint,
                    "budget_epoch": max(int(r["budget_epoch"] or 0) for r in group),
                    "epoch_tokens": sum(int(r["epoch_tokens"] or 0) for r in group),
                    "lifetime_tokens": sum(
                        int(r["lifetime_tokens"] or 0) for r in group
                    ),
                    "halt_kind": halt_kind,
                    "halt_message": halt_message,
                    "updated_at": max(updated) if updated else None,
                }
            )

        if not merged:
            return
        conn.execute(sch.endpoint_state.delete())
        conn.execute(
            sch.endpoint_state.insert(),
            [_filled(sch.endpoint_state, row) for row in merged],
        )

    def add_missing_columns(self) -> List[str]:
        """Add any of :data:`.schema.ADDITIVE_COLUMNS` this store lacks.

        A store created by an earlier build of the SAME schema version has
        every table and the right stamp but is missing a column added later
        (``calls.normalised``).  ``metadata.create_all`` will not add a column
        to an existing table, and the version stamp is not wrong, so the
        version check cannot catch it -- hence this.

        Every column in that list is nullable with no default, and the code
        that reads it treats NULL as "nothing to say", so adding one is safe
        on a live file and changes no existing row.  Idempotent; returns the
        columns it added, which is what ``kbu council db migrate`` reports.
        """
        inspector = sa.inspect(self._engine)
        added: List[str] = []
        for table_name, column_name in sch.ADDITIVE_COLUMNS:
            if not inspector.has_table(table_name):
                continue
            present = {col["name"] for col in inspector.get_columns(table_name)}
            if column_name in present:
                continue
            column = sch.metadata.tables[table_name].c[column_name]
            ddl = column.type.compile(self._engine.dialect)
            with self.transaction() as conn:
                conn.execute(
                    sa.text(f'ALTER TABLE {table_name} ADD COLUMN "{column_name}" {ddl}')
                )
            added.append(f"{table_name}.{column_name}")
        return added

    def add_missing_indexes(self) -> List[str]:
        """Create any index :mod:`.schema` declares that this store lacks.

        The index-shaped twin of :meth:`add_missing_columns`, and exists for
        the identical reason: a store created by an earlier build of the SAME
        schema version has every table and the right stamp but can be missing
        an index added later (``ix_calls_status_seat_not_before``, added to
        support the seat-filtered claim scan).  ``metadata.create_all`` only
        creates indexes for tables it is ALSO creating, so it does nothing for
        a table that already exists -- hence this, mirroring why
        ``add_missing_columns`` exists at all.

        Deliberately general over every index the schema declares, on every
        table, rather than naming ``ix_calls_status_seat_not_before``
        specifically: a future index added the same way would reopen the
        identical gap if this only knew about today's one.  It stays
        strictly additive, though -- nothing here drops, rebuilds or reorders
        an existing index, so ``ix_calls_status_not_before`` and every other
        index already present are left exactly as they are.

        Each statement is ``CREATE INDEX IF NOT EXISTS`` (``CREATE UNIQUE
        INDEX IF NOT EXISTS`` for a unique one, and the partial-index
        ``WHERE`` clause is preserved), so a second run -- or a run against a
        store that already has every index -- is a no-op pushed down into
        SQLite itself rather than one this code has to get right by reading
        the catalog first. Idempotent; returns the index names it created,
        which is what ``kbu council db migrate`` reports.
        """
        inspector = sa.inspect(self._engine)
        created: List[str] = []
        for table in sch.metadata.tables.values():
            if not inspector.has_table(table.name):
                continue
            present = {idx["name"] for idx in inspector.get_indexes(table.name)}
            for index in table.indexes:
                if index.name in present:
                    continue
                ddl = sa.schema.CreateIndex(index, if_not_exists=True)
                with self.transaction() as conn:
                    conn.execute(ddl)
                created.append(str(index.name))
        return created

    def _assert_schema_version(self) -> None:
        current = self.schema_version()
        # 0 means "not initialised yet", which is not a mismatch: init_schema()
        # is how a new store comes into existence.
        if current not in (0, sch.SCHEMA_VERSION):
            raise _version_mismatch(current)

    # -- query types ------------------------------------------------------

    def put_query_type(
        self,
        name: str,
        version: str,
        spec: Mapping[str, Any],
        source: str = "user",
        created_by: Optional[str] = None,
    ) -> None:
        """Save a query type.  Immutable: re-putting a DIFFERENT spec raises.

        Re-putting an identical spec is a no-op, which is what makes a
        bootstrap that registers the builtin query types safe to run on every
        start.

        Raises:
            QueryTypeExists: ``(name, version)`` exists with a different spec.
        """
        payload = _jsonify(spec)
        with self.transaction() as conn:
            existing = conn.execute(
                sa.select(sch.query_types.c.spec).where(
                    sch.query_types.c.name == name,
                    sch.query_types.c.version == version,
                )
            ).first()
            if existing is not None:
                if _canonical(existing[0]) == _canonical(payload):
                    return
                raise QueryTypeExists(
                    f"query type {name!r} version {version!r} already exists with a "
                    "different spec; query types are immutable -- save a new version"
                )
            conn.execute(
                sch.query_types.insert(),
                _filled(
                    sch.query_types,
                    {
                        "name": name,
                        "version": version,
                        "status": "active",
                        "spec": payload,
                        "source": source,
                        "created_by": created_by,
                        "created_at": _utcnow(),
                    },
                ),
            )

    def get_query_type(
        self, name: str, version: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Return one query type, or the latest ACTIVE version when *version* is None."""
        with self._read() as conn:
            if version is not None:
                row = conn.execute(
                    sa.select(sch.query_types).where(
                        sch.query_types.c.name == name,
                        sch.query_types.c.version == version,
                    )
                ).first()
                return _dict(row) if row is not None else None
            rows = _dicts(
                conn.execute(
                    sa.select(sch.query_types).where(
                        sch.query_types.c.name == name,
                        sch.query_types.c.status == "active",
                    )
                )
            )
        if not rows:
            return None
        return max(
            rows,
            key=lambda r: (_version_sort_key(r["version"]), r["created_at"] or _EPOCH),
        )

    def list_query_types(self, include_retired: bool = False) -> List[Dict[str, Any]]:
        """Return every query type, active ones only unless *include_retired*."""
        stmt = sa.select(sch.query_types)
        if not include_retired:
            stmt = stmt.where(sch.query_types.c.status == "active")
        with self._read() as conn:
            rows = _dicts(conn.execute(stmt))
        rows.sort(key=lambda r: (r["name"], _version_sort_key(r["version"])))
        return rows

    def retire_query_type(self, name: str, version: str) -> bool:
        """Mark a query type retired.  Returns False if there was no such row.

        Retiring hides it from ``get_query_type(name)`` and the default
        listing; the row stays, because jobs already decided against it must
        keep resolving.
        """
        with self.transaction() as conn:
            result = conn.execute(
                sch.query_types.update()
                .where(
                    sch.query_types.c.name == name,
                    sch.query_types.c.version == version,
                )
                .values(status="retired")
            )
        return bool(result.rowcount)

    # -- jobs, items, calls -----------------------------------------------

    def create_job(
        self,
        job: Mapping[str, Any],
        items: Sequence[Mapping[str, Any]],
        proposer_calls: Sequence[Mapping[str, Any]],
    ) -> str:
        """Write a job, its items and its first round of calls in ONE transaction.

        All or nothing by design: a job whose items landed but whose proposer
        calls did not is a job that will sit at 0% forever with no error
        anywhere, which is the worst failure mode a queue can have.

        Returns:
            The job id (generated if the job dict did not carry one).
        """
        job_row = dict(job)
        job_id = job_row.get("job_id") or uuid.uuid4().hex
        now = _utcnow()
        job_row.update(job_id=job_id)
        job_row.setdefault("submitted_at", now)
        job_row.setdefault("submitted_host", socket.gethostname())
        job_row.setdefault("status", "queued")
        job_row.setdefault("priority", 100)
        job_row.setdefault("n_items", len(items))

        item_rows = [
            _filled(
                sch.items,
                {
                    "item_order": order,
                    "status": "pending",
                    **dict(item),
                    "job_id": job_id,
                },
            )
            for order, item in enumerate(items)
        ]
        call_rows = [
            _filled(sch.calls, self._call_defaults(call, job_row, now))
            for call in proposer_calls
        ]

        with self.transaction() as conn:
            conn.execute(sch.jobs.insert(), _filled(sch.jobs, job_row))
            if item_rows:
                conn.execute(sch.items.insert(), item_rows)
            if call_rows:
                conn.execute(sch.calls.insert(), call_rows)
        return job_id

    @staticmethod
    def _call_defaults(
        call: Mapping[str, Any], job_row: Mapping[str, Any], now: datetime
    ) -> Dict[str, Any]:
        """Fill the parts of a call row that follow from its job."""
        row = dict(call)
        row.setdefault("call_id", uuid.uuid4().hex)
        row["job_id"] = job_row["job_id"]
        row.setdefault("status", "pending")
        row.setdefault("upstream_digest", "")
        row.setdefault("attempt_count", 0)
        row.setdefault("live", True)
        row.setdefault("created_at", now)
        row.setdefault("submitted_by", job_row.get("submitted_by"))
        row.setdefault("query_name", job_row.get("query_name"))
        row.setdefault("query_version", job_row.get("query_version"))
        return row

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Return the job row, tombstoned or not, or None."""
        with self._read() as conn:
            row = conn.execute(
                sa.select(sch.jobs).where(sch.jobs.c.job_id == job_id)
            ).first()
        return _dict(row) if row is not None else None

    def list_jobs(
        self,
        user: Optional[str] = None,
        status: Optional[str] = None,
        include_deleted: bool = False,
    ) -> List[Dict[str, Any]]:
        """Return jobs newest first, excluding tombstoned ones by default."""
        stmt = sa.select(sch.jobs)
        if user is not None:
            stmt = stmt.where(sch.jobs.c.submitted_by == user)
        if status is not None:
            stmt = stmt.where(sch.jobs.c.status == status)
        if not include_deleted:
            stmt = stmt.where(sch.jobs.c.deleted_at.is_(None))
        stmt = stmt.order_by(sch.jobs.c.submitted_at.desc(), sch.jobs.c.job_id)
        with self._read() as conn:
            return _dicts(conn.execute(stmt))

    def get_items(
        self, job_id: str, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return a job's items in submission order."""
        stmt = sa.select(sch.items).where(sch.items.c.job_id == job_id)
        if status is not None:
            stmt = stmt.where(sch.items.c.status == status)
        stmt = stmt.order_by(sch.items.c.item_order, sch.items.c.item_id)
        with self._read() as conn:
            return _dicts(conn.execute(stmt))

    def get_calls(
        self, job_id: str, item_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return a job's call rows, optionally for one item only."""
        stmt = sa.select(sch.calls).where(sch.calls.c.job_id == job_id)
        if item_id is not None:
            stmt = stmt.where(sch.calls.c.item_id == item_id)
        stmt = stmt.order_by(sch.calls.c.created_at, sch.calls.c.call_id)
        with self._read() as conn:
            return _dicts(conn.execute(stmt))

    def add_calls(self, calls: Sequence[Mapping[str, Any]]) -> List[str]:
        """Append call rows -- the critic and meta rounds -- and return their ids.

        Refuses any call belonging to a cancelled job: cancellation is
        terminal, and adding a call to a cancelled job would resurrect work
        the user has already stopped paying for.
        """
        if not calls:
            return []
        now = _utcnow()
        with self.transaction() as conn:
            job_ids = {c.get("job_id") for c in calls}
            if None in job_ids:
                raise ValueError("every call row must carry a job_id")
            jobs_by_id = {
                row["job_id"]: row
                for row in _dicts(
                    conn.execute(
                        sa.select(sch.jobs).where(sch.jobs.c.job_id.in_(job_ids))
                    )
                )
            }
            missing = sorted(job_ids - set(jobs_by_id))
            if missing:
                raise ValueError(f"no such job(s): {missing}")
            for job_id, job_row in jobs_by_id.items():
                if job_row["status"] == "cancelled":
                    raise JobCancelled(
                        f"job {job_id} is cancelled; no further calls may be added"
                    )
            rows = [
                _filled(sch.calls, self._call_defaults(c, jobs_by_id[c["job_id"]], now))
                for c in calls
            ]
            conn.execute(sch.calls.insert(), rows)
        return [row["call_id"] for row in rows]

    def settle_item(
        self,
        job_id: str,
        item_id: str,
        status: str,
        verdict: Any = None,
    ) -> None:
        """Record an item's final status and verdict.

        Raises:
            JobCancelled: the job was cancelled; the item is already settled
                as ``cancelled`` and must stay that way.
            ValueError: there is no such item.
        """
        with self.transaction() as conn:
            job_row = conn.execute(
                sa.select(sch.jobs.c.status).where(sch.jobs.c.job_id == job_id)
            ).first()
            if job_row is None:
                raise ValueError(f"no such job: {job_id}")
            if job_row[0] == "cancelled":
                raise JobCancelled(
                    f"job {job_id} is cancelled; item {item_id} cannot be settled"
                )
            result = conn.execute(
                sch.items.update()
                .where(sch.items.c.job_id == job_id, sch.items.c.item_id == item_id)
                .values(
                    status=status,
                    verdict=_jsonify(verdict),
                    decided_at=_utcnow(),
                )
            )
            if not result.rowcount:
                raise ValueError(f"no such item: {job_id}/{item_id}")

    # -- endpoint state ---------------------------------------------------
    #
    # Every row is keyed on STORE_IDENTITY, never on a provenance label.  The
    # `user` keyword on each of these methods is a DEPRECATED no-op kept so an
    # older caller still runs; it is ignored, deliberately, because honouring
    # it would split one machine's spend into per-label ledgers and let a
    # rename reset a budget (review round 3).

    def get_endpoint_state(
        self, endpoint: str, *, user: Optional[str] = None
    ) -> Dict[str, Any]:
        """Return this store's budget/halt row for *endpoint*.

        A never-seen endpoint returns a zeroed, un-halted row rather than
        None: every caller asks "is this halted and how much have I spent?",
        and a None would put the same two-line default at every call site.
        """
        with self._read() as conn:
            row = conn.execute(
                sa.select(sch.endpoint_state).where(
                    sch.endpoint_state.c.user == STORE_IDENTITY,
                    sch.endpoint_state.c.endpoint == endpoint,
                )
            ).first()
        if row is None:
            return {
                "user": STORE_IDENTITY,
                "endpoint": endpoint,
                "budget_epoch": 0,
                "epoch_tokens": 0,
                "lifetime_tokens": 0,
                "halt_kind": None,
                "halt_message": None,
                "updated_at": None,
            }
        return _dict(row)

    def add_tokens(
        self, endpoint: str, n: int, *, user: Optional[str] = None
    ) -> Dict[str, Any]:
        """Add *n* tokens to both the current epoch and the lifetime total."""
        n = int(n)
        with self.transaction() as conn:
            row = self._endpoint_row(conn, endpoint)
            conn.execute(
                self._endpoint_update(endpoint).values(
                    epoch_tokens=row["epoch_tokens"] + n,
                    lifetime_tokens=row["lifetime_tokens"] + n,
                    updated_at=_utcnow(),
                )
            )
        return self.get_endpoint_state(endpoint)

    def set_halt(
        self,
        endpoint: str,
        kind: str,
        message: str = "",
        *,
        user: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Halt an endpoint: no further calls are claimed for it."""
        with self.transaction() as conn:
            self._endpoint_row(conn, endpoint)
            conn.execute(
                self._endpoint_update(endpoint).values(
                    halt_kind=kind, halt_message=message, updated_at=_utcnow()
                )
            )
        return self.get_endpoint_state(endpoint)

    def reset_halt(
        self, endpoint: str, *, user: Optional[str] = None
    ) -> Dict[str, Any]:
        """Clear a halt.  Token counters are NEVER touched.

        Clearing a halt says "the problem is fixed, try again"; it does not
        say "the tokens were not spent".  Resetting the budget is
        :meth:`reset_budget`, a separate and deliberate act.
        """
        with self.transaction() as conn:
            self._endpoint_row(conn, endpoint)
            conn.execute(
                self._endpoint_update(endpoint).values(
                    halt_kind=None, halt_message=None, updated_at=_utcnow()
                )
            )
        return self.get_endpoint_state(endpoint)

    def reset_budget(
        self, endpoint: str, *, user: Optional[str] = None
    ) -> Dict[str, Any]:
        """Start a new budget epoch: epoch tokens to 0, lifetime kept.

        Also clears a halt that was caused by the budget itself, since the
        reason for it no longer holds.  A quota, auth or billing halt is left
        alone -- a new epoch does not fix an expired credential.
        """
        with self.transaction() as conn:
            row = self._endpoint_row(conn, endpoint)
            values: Dict[str, Any] = {
                "budget_epoch": row["budget_epoch"] + 1,
                "epoch_tokens": 0,
                "updated_at": _utcnow(),
            }
            if row["halt_kind"] in ("budget_exhausted", "budget_usage_unknown"):
                values["halt_kind"] = None
                values["halt_message"] = None
            conn.execute(self._endpoint_update(endpoint).values(**values))
        return self.get_endpoint_state(endpoint)

    def _endpoint_row(self, conn: sa.Connection, endpoint: str) -> Dict[str, Any]:
        """Return the endpoint's row, inserting a zeroed one if absent."""
        row = conn.execute(
            sa.select(sch.endpoint_state).where(
                sch.endpoint_state.c.user == STORE_IDENTITY,
                sch.endpoint_state.c.endpoint == endpoint,
            )
        ).first()
        if row is not None:
            return _dict(row)
        values = _filled(
            sch.endpoint_state,
            {
                "user": STORE_IDENTITY,
                "endpoint": endpoint,
                "budget_epoch": 0,
                "epoch_tokens": 0,
                "lifetime_tokens": 0,
                "updated_at": _utcnow(),
            },
        )
        conn.execute(sch.endpoint_state.insert(), values)
        return values

    @staticmethod
    def _endpoint_update(endpoint: str) -> Any:
        return sch.endpoint_state.update().where(
            sch.endpoint_state.c.user == STORE_IDENTITY,
            sch.endpoint_state.c.endpoint == endpoint,
        )

    # -- workers ----------------------------------------------------------

    def register_worker(self, row: Mapping[str, Any]) -> str:
        """Insert or refresh a worker row; returns the worker id."""
        values = dict(row)
        worker_id = values.get("worker_id") or uuid.uuid4().hex
        now = _utcnow()
        values.update(worker_id=worker_id)
        values.setdefault("kind", "foreground")
        values.setdefault("host", socket.gethostname())
        values.setdefault("started_at", now)
        values.setdefault("heartbeat_at", now)
        filled = _filled(sch.workers, values)
        with self.transaction() as conn:
            existing = conn.execute(
                sa.select(sch.workers.c.worker_id).where(
                    sch.workers.c.worker_id == worker_id
                )
            ).first()
            if existing is None:
                conn.execute(sch.workers.insert(), filled)
            else:
                update = {k: v for k, v in filled.items() if k != "worker_id"}
                conn.execute(
                    sch.workers.update()
                    .where(sch.workers.c.worker_id == worker_id)
                    .values(**update)
                )
        return worker_id

    def heartbeat_worker(self, worker_id: str, now: Optional[datetime] = None) -> bool:
        """Stamp a worker as alive.  Returns False if it is not registered."""
        with self.transaction() as conn:
            result = conn.execute(
                sch.workers.update()
                .where(sch.workers.c.worker_id == worker_id)
                .values(heartbeat_at=now or _utcnow())
            )
        return bool(result.rowcount)

    def live_workers(
        self,
        user: Optional[str] = None,
        now: Optional[datetime] = None,
        max_age: timedelta = timedelta(minutes=2),
    ) -> List[Dict[str, Any]]:
        """Return workers that have heartbeated within *max_age*.

        *user* is optional so job_status can ask "is ANY worker serving this
        endpoint?" across the shared store as well as "is MY daemon alive?".
        """
        cutoff = (now or _utcnow()) - max_age
        stmt = sa.select(sch.workers).where(sch.workers.c.heartbeat_at >= cutoff)
        if user is not None:
            stmt = stmt.where(sch.workers.c.user == user)
        stmt = stmt.order_by(sch.workers.c.heartbeat_at.desc())
        with self._read() as conn:
            return _dicts(conn.execute(stmt))

    def remove_worker(self, worker_id: str) -> bool:
        """Delete a worker row.  Returns False if it was already gone."""
        with self.transaction() as conn:
            result = conn.execute(
                sch.workers.delete().where(sch.workers.c.worker_id == worker_id)
            )
        return bool(result.rowcount)

    def stop_requested(self, worker_id: str) -> bool:
        """Has this worker been asked to stop?"""
        with self._read() as conn:
            row = conn.execute(
                sa.select(sch.workers.c.stop_requested_at).where(
                    sch.workers.c.worker_id == worker_id
                )
            ).first()
        return bool(row is not None and row[0] is not None)

    # -- search -----------------------------------------------------------

    def search_calls(
        self,
        query_name: Optional[str] = None,
        canonical_model: Optional[str] = None,
        entity_hash: Optional[str] = None,
        data_hash: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Find call rows across every job and user.  Newest first.

        This is the "what has already been asked?" query -- the same index the
        reuse path uses, exposed for a human looking at a shared store.
        """
        stmt = sa.select(sch.calls)
        for column, value in (
            (sch.calls.c.query_name, query_name),
            (sch.calls.c.canonical_model, canonical_model),
            (sch.calls.c.entity_hash, entity_hash),
            (sch.calls.c.data_hash, data_hash),
            (sch.calls.c.status, status),
        ):
            if value is not None:
                stmt = stmt.where(column == value)
        stmt = stmt.order_by(sch.calls.c.created_at.desc(), sch.calls.c.call_id).limit(
            limit
        )
        with self._read() as conn:
            return _dicts(conn.execute(stmt))

    # -- the queue --------------------------------------------------------
    #
    # Call rows ARE the queue.  Every statement below is built in
    # :mod:`.queries`, which holds the queue's meaning without any locking;
    # the only thing this class adds is the locking strategy, which is
    # ``transaction()``'s BEGIN IMMEDIATE.

    def claim_calls(
        self,
        worker_id: str,
        endpoints: Set[str],
        limit: int,
        now: Optional[datetime] = None,
        *,
        user: Optional[str] = None,
        seats: Optional[Set[str]] = None,
        roles: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Atomically claim up to *limit* due pending calls for this worker.

        Claims across the WHOLE store: every non-cancelled, non-paused,
        non-tombstoned job, whatever provenance label its ``submitted_by``
        carries.  A store is one machine's and its queue is one queue, so
        filtering on that label would strand every pending call the moment
        somebody edited config ``user`` (review round 3).  *user* is a
        deprecated no-op kept for older callers and is ignored.

        *seats* and *roles*, when given (not ``None``), restrict the claim to
        calls on one of those seats / roles -- a partitioned worker's queue.
        Both default to ``None``, which claims across every seat and role
        exactly as before.

        The select and the stamping happen inside one ``BEGIN IMMEDIATE``
        transaction, which is what makes the claim atomic: the write lock is
        taken before the first read, so two workers cannot both see the same
        pending row.

        Returns:
            The claimed rows, each carrying the ``claim_token`` its completion
            must quote.  Empty when there is nothing due -- which is the
            worker's signal to sleep, not an error.
        """
        if limit <= 0 or not endpoints:
            return []
        now = now or _utcnow()

        with self.transaction() as conn:
            rows = _dicts(
                conn.execute(
                    q.select_claimable(
                        endpoints, now, limit, seats=seats, roles=roles
                    )
                )
            )
            if not rows:
                return []

            params: List[Dict[str, Any]] = []
            claimed: List[Dict[str, Any]] = []
            for row in rows:
                # One token per call: a token shared across the batch would
                # let a worker settle a call it no longer holds by quoting a
                # sibling's.
                token = uuid.uuid4().hex
                params.append(
                    {
                        "b_call_id": row["call_id"],
                        "b_claim_token": token,
                        "b_claimed_by": worker_id,
                        "b_claimed_at": now,
                        # No lease generation: a daemon is excluded by the
                        # file lock in :mod:`..execlock`, not by a lease row,
                        # so there is no generation to supersede.
                        "b_lease_generation": None,
                    }
                )
                claimed.append(
                    {
                        **row,
                        "status": "claimed",
                        "claim_token": token,
                        "claimed_by": worker_id,
                        "claimed_at": now,
                        "lease_generation": None,
                    }
                )
            conn.execute(q.claim_update(), params)
        return claimed

    def complete_call(
        self,
        call_id: str,
        claim_token: str,
        status: str,
        reply_text: Optional[str] = None,
        reply_parsed: Optional[Mapping[str, Any]] = None,
        fresh_usage: Optional[Mapping[str, Any]] = None,
        error: Optional[Mapping[str, Any]] = None,
        reused_from: Optional[str] = None,
        normalised: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> None:
        """Record a claimed call's outcome.

        ``reply_parsed`` is the schema-valid reply in the query type's OWN
        field names, before postprocessing or menu filtering: the
        post-processed shape would be useless to a later query-type version
        that maps the same answer differently, and the reply is the thing
        that was paid for.

        ``normalised`` is the repair record ``engine/normalise.py`` produced
        for that reply, or ``None``/empty when the model answered in the
        shape its role's schema asked for.  When it is non-empty,
        ``reply_parsed`` is the REPAIRED answer -- the one the council read --
        and ``reply_text`` is still the untouched original, so the pair plus
        this record holds both.

        Raises:
            StaleClaim: the row was reclaimed and re-handed out (its token
                changed), or the completing daemon's lease generation has
                since been superseded.  Either way the reply is discarded
                rather than written over whatever the current claimant is
                doing.
            ValueError: no such call, or *status* is not ``ok``/``error``.
        """
        if status not in ("ok", "error"):
            raise ValueError(
                f"complete_call status must be 'ok' or 'error', got {status!r}"
            )

        with self.transaction() as conn:
            call = self._claimed_call(conn, call_id, claim_token)
            result = conn.execute(
                q.complete_call_update(
                    call_id,
                    claim_token,
                    status=status,
                    reply_text=reply_text,
                    reply_parsed=_jsonify(reply_parsed),
                    normalised=_jsonify(
                        [dict(entry) for entry in normalised] if normalised else None
                    ),
                    fresh_usage=_jsonify(fresh_usage),
                    error=_jsonify(error),
                    reused_from=reused_from,
                    finished_at=_utcnow(),
                )
            )
            if not result.rowcount:  # pragma: no cover - the token check caught it
                raise _stale_claim(call_id, "the claim was lost mid-completion")

    def release_call(
        self,
        call_id: str,
        claim_token: str,
        not_before: Optional[datetime] = None,
        increment_attempt: bool = True,
    ) -> None:
        """Put a claimed call back to pending, clearing its token.

        This is the retry path: a transport error or a timeout releases the
        call with a *not_before* backoff so the next claim does not hit the
        same endpoint immediately.  *increment_attempt* is False when the
        release is not the call's fault -- a worker shutting down, or an
        endpoint halting -- so it does not burn the retry budget.

        Raises:
            StaleClaim: the token is not the row's current one.
            ValueError: no such call.
        """
        with self.transaction() as conn:
            self._claimed_call(conn, call_id, claim_token)
            result = conn.execute(
                q.release_call_update(
                    call_id,
                    claim_token,
                    not_before=not_before,
                    increment_attempt=increment_attempt,
                )
            )
            if not result.rowcount:  # pragma: no cover - the token check caught it
                raise _stale_claim(call_id, "the claim was lost mid-release")

    def note_call_error(
        self, call_id: str, error: Optional[Mapping[str, Any]]
    ) -> None:
        """Record a call's last error WITHOUT changing its status.

        The engine's way of putting a reason on a call it is about to RELEASE
        rather than settle: ``job_status`` reports ``waiting_for_model`` and
        ``configuration_mismatch`` off ``calls.error`` on pending rows, and
        neither ``release_call`` (whose signature takes no error) nor
        ``complete_call`` (which settles the row) can write it.  Call it
        inside the same ``transaction()`` as the release -- ``transaction()``
        is re-entrant, so the two are one unit.

        Passing ``None`` clears the column, which is how a call that has
        stopped failing stops reporting a blocker.

        Raises:
            ValueError: no such call.
        """
        with self.transaction() as conn:
            result = conn.execute(q.note_call_error_update(call_id, _jsonify(error)))
            if not result.rowcount:
                raise ValueError(f"no such call: {call_id}")

    def set_job_status(
        self,
        job_id: str,
        status: str,
        only_if: Optional[Sequence[str]] = None,
    ) -> bool:
        """Move a job's status, optionally only from one of *only_if*.

        The queue itself never changes a job's status: ``queued -> running``
        on the first claim and ``-> done`` when the last item settles are the
        worker's calls, and it makes them inside the transaction that settles
        the call or the item.  *only_if* is how those transitions refuse a job
        that has since been cancelled, cancellation being terminal.

        Returns:
            True if a row was moved; False if the guard refused it.

        Raises:
            ValueError: no such job.
        """
        with self.transaction() as conn:
            exists = conn.execute(
                sa.select(sch.jobs.c.job_id).where(sch.jobs.c.job_id == job_id)
            ).first()
            if exists is None:
                raise ValueError(f"no such job: {job_id}")
            result = conn.execute(q.set_job_status_update(job_id, status, only_if))
        return bool(result.rowcount)

    @staticmethod
    def _claimed_call(
        conn: sa.Connection, call_id: str, claim_token: str
    ) -> Dict[str, Any]:
        """Return the call row, asserting *claim_token* is still its live one.

        A cleared token (the row was completed, released or reclaimed) is a
        stale claim just as much as a different one, which is also what makes
        completing the same call twice an error rather than a double write.
        """
        row = conn.execute(q.select_call(call_id)).first()
        if row is None:
            raise ValueError(f"no such call: {call_id}")
        call = _dict(row)
        current = call["claim_token"]
        if current is None or current != claim_token:
            raise _stale_claim(
                call_id,
                f"its claim token is {'cleared' if current is None else 'different'} "
                f"(the call is {call['status']!r})",
            )
        return call

    def next_uncharged_attempt_no(self, call_id: str) -> int:
        """A free NEGATIVE ``attempts.attempt_no`` for *call_id*: -1, -2, ...

        ``attempts`` is keyed ``(call_id, attempt_no)`` and the engine derives
        that number from ``calls.attempt_count`` -- correct for every outcome
        that CHARGES the call an attempt, which is every outcome but one.  The
        council daemon's stop deadline records an ``interrupted`` attempt
        without charging one (review round 3: a daemon being stopped is not
        the call's fault), so ``attempt_count`` does not move and the number
        the call's NEXT real try will use is still ``attempt_count + 1``.
        Filing the interrupted row there would collide with that try -- and a
        collision inside the settle transaction would lose the answer the call
        eventually gave.

        So an uncharged attempt is filed below zero.  The engine's sequence is
        1, 2, 3...; this one hands out -1, -2, ... in order, so a call
        interrupted twice gets two rows, nothing ever collides, and the SIGN
        is the record of which attempts cost the call a retry and which did
        not.

        NOT part of the :class:`~..protocols.CouncilStore` contract: it is a
        convenience over a table the contract already owns, and its one caller
        has a fallback for a store that lacks it.
        """
        with self._read() as conn:
            lowest = conn.execute(
                sa.select(sa.func.min(sch.attempts.c.attempt_no)).where(
                    sch.attempts.c.call_id == call_id
                )
            ).scalar()
        return min(int(lowest or 0), 0) - 1

    def record_attempt(
        self,
        call_id: str,
        attempt_no: int,
        started_at: datetime,
        finished_at: datetime,
        outcome_kind: str,
        usage: Optional[Mapping[str, Any]] = None,
        worker_id: Optional[str] = None,
        queued_at: Optional[datetime] = None,
    ) -> None:
        """Record one try at a call.

        ``queued_at`` writes the additive ``attempts.queued_at`` column added
        for the per-queue worker's service-time split (``t2``); left
        ``None`` (its default, and the column's own default) by any caller
        that never measured a queue wait, which is "nothing to say" and not
        the same claim as zero.

        Raises:
            ValueError: any argument carries prompt text -- a dict, at any
                depth, with a ``system`` or ``user`` key.  The attempts table
                is the audit trail and is readable by every user of a shared
                store; see :func:`.queries.refuse_prompt_text`.
        """
        # Pass every parameter this call received, derived from the call's
        # own arguments rather than hand-copied -- `locals()` here, before
        # any local variable is defined, is exactly the parameter dict minus
        # `self`.  A hand-maintained kwargs list silently stops covering a
        # parameter the moment one is added and this call site is not
        # updated (that is the bug this guards against): `queued_at` was
        # added to the signature without being added here.  Deriving the
        # dict instead makes a future parameter covered automatically, with
        # no second place to remember.
        q.refuse_prompt_text(**{k: v for k, v in locals().items() if k != "self"})
        with self.transaction() as conn:
            conn.execute(
                sch.attempts.insert(),
                _filled(
                    sch.attempts,
                    {
                        "call_id": call_id,
                        "attempt_no": int(attempt_no),
                        "started_at": started_at,
                        "finished_at": finished_at,
                        "outcome_kind": outcome_kind,
                        "usage": usage,
                        "worker_id": worker_id,
                        "queued_at": queued_at,
                    },
                ),
            )

    def find_reusable(self, prompt_hash: str) -> Optional[Dict[str, Any]]:
        """Return an already-answered call with this prompt hash, or None.

        Crosses jobs AND users, including tombstoned jobs: the prompt hash is
        built from the reuse namespace and the canonical model rather than
        from an endpoint alias, so a question one user has already paid for is
        not asked again by the next, and deleting a job does not throw the
        answer away.
        """
        if not prompt_hash:
            return None
        with self._read() as conn:
            row = conn.execute(q.select_reusable(prompt_hash)).first()
        return _dict(row) if row is not None else None

    # -- seeding the reuse cache ------------------------------------------

    def seed_records(self) -> List[Dict[str, Any]]:
        """Every DISTINCT seed this store was built from, newest-written last.

        Read out of the imported jobs' ``options[SEED_PROVENANCE_KEY]``, which
        is where :meth:`seed_from_rows` records what it imported.  A store
        nobody seeded returns ``[]``; a reuse-only store returns exactly one
        record.
        """
        with self._read() as conn:
            rows = _dicts(
                conn.execute(sa.select(sch.jobs.c.job_id, sch.jobs.c.options))
            )
        out: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for row in rows:
            options = row.get("options")
            if not isinstance(options, Mapping):
                continue
            record = options.get(SEED_PROVENANCE_KEY)
            if not isinstance(record, Mapping):
                continue
            key = _canonical({k: v for k, v in record.items() if k != "seeded_at"})
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(record))
        return out

    def seed_from_rows(
        self,
        rows: Mapping[str, Sequence[Mapping[str, Any]]],
        *,
        store_id: str,
        snapshot_id: str,
        snapshot_seq: Optional[int] = None,
        query_name: Optional[str] = None,
        query_version: Optional[str] = None,
        seeded_by: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Rebuild a REUSE-ONLY store from one store's archived rows.

        *rows* is ``{table: [archived row, ...]}`` over
        :data:`SEED_TABLES`, in the column shape ``council_schema`` declares
        (identity columns and the ``query_name`` denormalisations are dropped,
        ``council_entity_hash`` is renamed back, JSON/TIMESTAMP/BOOLEAN values
        are converted).  Nothing here reads a lakehouse: the store layer does
        not depend on ``kbase.berdl``, and any source that can produce rows of
        that shape can seed a store.  The lakehouse reader is
        ``kbutillib.domains.kbase.berdl.council_seed.read_seed_rows``.

        SEVEN RULES, each of which costs money or correctness if broken.

        1. ONE ``store_id`` AND ONE ``snapshot_id``, into an EMPTY
           destination.  Multi-origin seeding is REFUSED
           (:class:`.errors.SeedMultiOrigin`) until a deterministic identity
           remapping contract exists: ``calls.call_id`` is an UNQUALIFIED
           primary key carrying foreign keys to ``items``/``jobs``, while the
           lakehouse guarantees only STORE-SCOPED identity, so dropping
           ``store_id`` can collide two calls onto one id.
        2. EVERY ``status='ok'`` ANSWER IS IMPORTED REGARDLESS OF
           DISPOSITION.  A pilot or trial answer is still a paid answer, and
           restricting the import to ``disposition='published'`` would discard
           legitimate cached work.  This method is given rows and asks nothing
           about dispositions; the identity rows the imported calls need
           (``jobs``, ``items``, ``query_types``) come with them so the result
           is referentially valid.
        3. ``live=0`` ROWS ARE EXCLUDED HERE.
           :func:`queries.select_reusable` accepts ``status='ok'`` across all
           jobs and does NOT filter ``live``, so a superseded answer imported
           into a reuse cache WOULD BE SERVED as a current one.  The filter
           therefore belongs in this method, and it is applied to the rows as
           given rather than trusted to the caller.
        4. NO IMPORTED ROW IS CLAIMABLE, enforced twice.  Only
           ``status='ok'`` calls are imported, and
           :func:`queries.select_claimable` claims only ``status='pending'``
           rows -- so there is nothing of the right status to claim.  AND
           every imported job is TOMBSTONED (``deleted_at`` set) with
           ``status`` :data:`SEED_JOB_STATUS`, and ``select_claimable``
           additionally requires ``jobs.deleted_at IS NULL`` -- so even a
           pending call later attached to a seeded job could not be claimed.
           Tombstoning is exactly the right existing mechanism rather than a
           new one: ``select_reusable`` deliberately INCLUDES tombstoned jobs
           ("deleting a job must not throw away an answer somebody paid for"),
           and ``list_jobs`` excludes them, so an imported job is reusable and
           invisible to the queue at the same time.
        5. ``normalised`` IS CARRIED THROUGH INTACT, including the difference
           between JSON null and a repair list -- see :func:`_seed_json`.
        6. A reuse hit is only valid at the SAME query version, because
           ``query_version`` is inside ``prompt_hash``.  *query_name* /
           *query_version* scope the import, and what was seeded is RECORDED
           in each imported job's ``options[SEED_PROVENANCE_KEY]`` and
           returned in the receipt.
        7. IDEMPOTENT.  Seeding the SAME ``(store_id, snapshot_id, scope)``
           into a store that holds exactly that seed and nothing else is a
           NO-OP returning ``"imported": False`` -- not a refusal, which would
           make rule 1's empty-destination requirement and idempotency
           contradict each other.

        Args:
            rows: ``{table: [archived row, ...]}``.  A table absent from the
                mapping is treated as empty.
            store_id: The source store's DECLARED archive identity.
            snapshot_id: The source snapshot's verified digest.  The caller is
                responsible for having established that the snapshot is
                COMPLETELY archived -- there is one completeness test,
                ``council_archive.require_complete_delivery``, and the
                lakehouse reader calls it.
            snapshot_seq: The snapshot's sequence, recorded if given.
            query_name: Import only calls of this council kind.
            query_version: Import only calls of this query version.  Requires
                *query_name*.
            seeded_by: Who ran the seed.  Recorded for accountability.
            now: Timestamp to record.  Defaults to now, UTC.

        Returns:
            A receipt: ``origin``, ``scope``, ``imported``, per-table
            ``counts``, ``skipped`` (by reason) and ``seeded_at``.

        Raises:
            SeedMultiOrigin: the rows or the destination carry a second
                origin.
            SeedDestinationNotEmpty: the destination holds work that is not
                this seed.
            SeedError: a row the import needs is missing or unreadable.
            QueryTypeExists: an incoming query type conflicts with one the
                destination already holds.
        """
        origin_store = str(store_id or "").strip()
        origin_snapshot = str(snapshot_id or "").strip()
        if not origin_store or not origin_snapshot:
            raise SeedError(
                "a seed names ONE store_id and ONE snapshot_id; got "
                f"store_id={store_id!r}, snapshot_id={snapshot_id!r}. Neither "
                "is ever derived from a path or a filename -- every archived "
                "row is keyed on them."
            )
        if query_version is not None and not query_name:
            raise SeedError(
                "query_version without query_name does not scope anything: "
                "versions are per query type, so `4` alone names no rows"
            )
        stamp = now or _utcnow()
        scope = {"query_name": query_name, "query_version": query_version}
        origin = {
            "store_id": origin_store,
            "snapshot_id": origin_snapshot,
            "snapshot_seq": None if snapshot_seq is None else int(snapshot_seq),
        }

        self._refuse_second_origin(rows, origin_store, origin_snapshot)

        record = {
            "store_id": origin_store,
            "snapshot_id": origin_snapshot,
            "snapshot_seq": origin["snapshot_seq"],
            "query_name": query_name,
            "query_version": query_version,
            "seeded_by": seeded_by or os.environ.get("USER") or "unknown",
            "seeded_at": stamp.isoformat(),
        }
        identity = _canonical({k: v for k, v in record.items() if k != "seeded_at"})

        existing = self.seed_records()
        foreign = self._non_seed_counts()
        if existing:
            keys = {
                _canonical({k: v for k, v in one.items() if k != "seeded_at"})
                for one in existing
            }
            if keys != {identity}:
                raise SeedMultiOrigin(
                    f"this store was already seeded from {sorted(keys)!r} and "
                    f"this seed is {identity!r}. One store, one origin: an "
                    "imported row keeps its source call_id, which is unique "
                    "only within its own store."
                )
            if not any(foreign.values()):
                return {
                    "origin": origin,
                    "scope": scope,
                    "imported": False,
                    "idempotent": True,
                    "reason": "this store already holds exactly this seed",
                    "counts": self._seed_counts(),
                    "skipped": {},
                    "seeded_at": stamp.isoformat(),
                    "record": existing[0],
                }
        if any(foreign.values()):
            raise SeedDestinationNotEmpty(
                f"the destination store holds {foreign!r} row(s) that are not "
                "part of this seed. A seed rebuilds an EMPTY store's reuse "
                "cache: imported rows keep their source job_id/call_id, so "
                "importing on top of existing work can collide two different "
                "calls onto one primary key."
            )

        plan = self._plan_seed(rows, scope=scope)
        job_rows = [
            _filled(
                sch.jobs,
                {
                    **row,
                    "status": SEED_JOB_STATUS,
                    # Rule 4's second enforcement, and the reason an imported
                    # answer stays reusable anyway: select_claimable refuses a
                    # tombstoned job, select_reusable includes one.
                    "deleted_at": stamp,
                    "n_items": len(plan["items_by_job"].get(row["job_id"], ())),
                    "options": {
                        **(row.get("options") or {}),
                        SEED_PROVENANCE_KEY: record,
                    },
                },
            )
            for row in plan["jobs"]
        ]
        item_rows = [_filled(sch.items, row) for row in plan["items"]]
        call_rows = [_filled(sch.calls, row) for row in plan["calls"]]

        with self.transaction() as conn:
            for row in plan["query_types"]:
                self._seed_query_type(conn, row)
            if job_rows:
                conn.execute(sch.jobs.insert(), job_rows)
            if item_rows:
                conn.execute(sch.items.insert(), item_rows)
            if call_rows:
                conn.execute(sch.calls.insert(), call_rows)

        return {
            "origin": origin,
            "scope": scope,
            "imported": True,
            "idempotent": False,
            "counts": {
                "query_types": len(plan["query_types"]),
                "jobs": len(job_rows),
                "items": len(item_rows),
                "calls": len(call_rows),
            },
            "skipped": plan["skipped"],
            "reusable_prompt_hashes": plan["reusable_prompt_hashes"],
            "seeded_at": stamp.isoformat(),
            "record": record,
        }

    @staticmethod
    def _refuse_second_origin(
        rows: Mapping[str, Sequence[Mapping[str, Any]]],
        store_id: str,
        snapshot_id: str,
    ) -> None:
        """Refuse rows carrying an identity other than the declared one.

        The archived rows lead with ``(store_id, snapshot_id)``, so a caller
        that read two snapshots -- or two stores -- into one mapping is
        detectable HERE, before anything is written, rather than as a primary
        key collision part-way through an insert.
        """
        stores: Set[str] = set()
        snapshots: Set[str] = set()
        for table in SEED_TABLES:
            for row in rows.get(table) or ():
                if row.get("store_id") is not None:
                    stores.add(str(row["store_id"]))
                if row.get("snapshot_id") is not None:
                    snapshots.add(str(row["snapshot_id"]))
        for label, found, declared in (
            ("store_id", stores, store_id),
            ("snapshot_id", snapshots, snapshot_id),
        ):
            if found and found != {declared}:
                raise SeedMultiOrigin(
                    f"the rows offered to this seed carry {label} "
                    f"{sorted(found)!r} and the seed declares {declared!r}. "
                    "ONE store and ONE snapshot per seed: call_id is "
                    "store-scoped, so merging origins can collide two "
                    "different calls onto one id."
                )

    def _plan_seed(
        self,
        rows: Mapping[str, Sequence[Mapping[str, Any]]],
        *,
        scope: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Decide exactly which rows are imported, and why the rest are not.

        Separate from the write so the whole decision is one readable pass and
        so the receipt's ``skipped`` counts come from the same place the
        filtering does.
        """
        jobs_by_id = {
            str(row["job_id"]): _seed_row(sch.jobs, row)
            for row in rows.get("jobs") or ()
        }
        job_query = {
            job_id: (_seed_text(row.get("query_name")), _seed_text(row.get("query_version")))
            for job_id, row in jobs_by_id.items()
        }
        wanted_name = _seed_text(scope.get("query_name"))
        wanted_version = _seed_text(scope.get("query_version"))

        skipped = {
            "not_ok": 0,
            "not_live": 0,
            "out_of_scope": 0,
            "no_prompt_hash": 0,
        }
        kept: List[Dict[str, Any]] = []
        hashes: Set[str] = set()
        for raw in rows.get("calls") or ():
            if str(raw.get("status") or "") != "ok":
                skipped["not_ok"] += 1
                continue
            if _seed_bool(raw.get("live")) is False:
                # Rule 3.  find_reusable does not filter `live`, so a
                # superseded answer imported here would be served as current.
                skipped["not_live"] += 1
                continue
            call = _seed_row(sch.calls, raw)
            job_id = str(call.get("job_id") or "")
            fallback_name, fallback_version = job_query.get(job_id, (None, None))
            name = _seed_text(call.get("query_name")) or _seed_text(fallback_name)
            version = _seed_text(call.get("query_version")) or _seed_text(
                fallback_version
            )
            if wanted_name is not None and name != wanted_name:
                skipped["out_of_scope"] += 1
                continue
            if wanted_version is not None and version != wanted_version:
                skipped["out_of_scope"] += 1
                continue
            if not call.get("prompt_hash"):
                # Imported anyway -- it is a paid answer and part of the
                # record -- but counted, because it can never be a reuse HIT:
                # find_reusable keys on prompt_hash and returns None for an
                # empty one.
                skipped["no_prompt_hash"] += 1
            else:
                hashes.add(str(call["prompt_hash"]))
            kept.append(call)

        job_ids = {str(call["job_id"]) for call in kept}
        missing_jobs = sorted(job_ids - set(jobs_by_id))
        if missing_jobs:
            raise SeedError(
                f"no jobs row was offered for {missing_jobs!r}, which "
                f"{len(kept)} imported call(s) refer to. An imported call "
                "without its job is not referentially valid (items.job_id and "
                "fk_calls_item), and the store has foreign keys ON."
            )

        items_by_job: Dict[str, List[Dict[str, Any]]] = {}
        wanted_items = {(str(c["job_id"]), str(c["item_id"])) for c in kept}
        seen_items: Set[tuple] = set()
        item_rows: List[Dict[str, Any]] = []
        for raw in rows.get("items") or ():
            key = (str(raw.get("job_id")), str(raw.get("item_id")))
            if key not in wanted_items or key in seen_items:
                continue
            seen_items.add(key)
            item = _seed_row(sch.items, raw)
            item_rows.append(item)
            items_by_job.setdefault(key[0], []).append(item)
        missing_items = sorted(wanted_items - seen_items)
        if missing_items:
            raise SeedError(
                f"no items row was offered for {missing_items[:5]!r} "
                f"({len(missing_items)} in total), which imported calls refer "
                "to through fk_calls_item."
            )

        query_type_rows = []
        wanted_types = {
            job_query[job_id] for job_id in job_ids if job_id in job_query
        }
        for raw in rows.get("query_types") or ():
            typed = _seed_row(sch.query_types, raw)
            key = (_seed_text(typed.get("name")), _seed_text(typed.get("version")))
            if key in wanted_types:
                query_type_rows.append(typed)

        return {
            "query_types": query_type_rows,
            "jobs": [jobs_by_id[job_id] for job_id in sorted(job_ids)],
            "items": item_rows,
            "items_by_job": items_by_job,
            "calls": kept,
            "skipped": skipped,
            "reusable_prompt_hashes": len(hashes),
        }

    @staticmethod
    def _seed_query_type(conn: sa.Connection, row: Mapping[str, Any]) -> None:
        """Insert one imported query type, or verify the one already there.

        Query types are immutable (``put_query_type``): an identical spec is a
        no-op and a DIFFERENT spec under the same ``(name, version)`` raises,
        because a finished job's verdicts only mean anything against the exact
        spec that produced them -- and a reuse hit is only valid at the same
        version for the same reason.
        """
        existing = conn.execute(
            sa.select(sch.query_types.c.spec).where(
                sch.query_types.c.name == row.get("name"),
                sch.query_types.c.version == row.get("version"),
            )
        ).first()
        if existing is not None:
            if _canonical(existing[0]) != _canonical(row.get("spec")):
                raise QueryTypeExists(
                    f"query type {row.get('name')!r} version "
                    f"{row.get('version')!r} is already in the destination "
                    "with a DIFFERENT spec; query types are immutable, and a "
                    "reuse hit is only valid against the spec that produced it"
                )
            return
        conn.execute(sch.query_types.insert(), _filled(sch.query_types, dict(row)))

    def _non_seed_counts(self) -> Dict[str, int]:
        """Row counts, per table, of work that is NOT part of a seed.

        A seeded job is identified by its provenance record, so this is "what
        would collide": jobs this store ran itself, and the items, calls and
        attempts hanging off them.
        """
        seeded = sa.select(sch.jobs.c.job_id).where(
            sa.func.json_extract(sch.jobs.c.options, f"$.{SEED_PROVENANCE_KEY}")
            .is_not(None)
        )
        with self._read() as conn:
            seeded_ids = {row[0] for row in conn.execute(seeded)}
            counts = {
                "jobs": self._count(
                    conn, sch.jobs, sch.jobs.c.job_id.notin_(seeded_ids)
                ),
                "items": self._count(
                    conn, sch.items, sch.items.c.job_id.notin_(seeded_ids)
                ),
                "calls": self._count(
                    conn, sch.calls, sch.calls.c.job_id.notin_(seeded_ids)
                ),
            }
            # attempts.call_id has a foreign key to calls, so an attempt whose
            # call belongs to no seeded job is the only kind that can exist
            # outside a seed.
            seed_calls = sa.select(sch.calls.c.call_id).where(
                sch.calls.c.job_id.in_(seeded_ids)
            )
            counts["attempts"] = self._count(
                conn, sch.attempts, sch.attempts.c.call_id.notin_(seed_calls)
            )
        return counts

    def _seed_counts(self) -> Dict[str, int]:
        """Row counts, per seeded table, of what a seed put here."""
        counts = self._row_counts()
        return {name: counts.get(name, 0) for name in _SEED_EMPTINESS_TABLES}

    @staticmethod
    def _count(conn: sa.Connection, table: sa.Table, predicate: Any) -> int:
        return int(
            conn.execute(
                sa.select(sa.func.count()).select_from(table).where(predicate)
            ).scalar_one()
        )

    def reclaim_stale(
        self, now: Optional[datetime] = None, max_age: timedelta = timedelta(minutes=10)
    ) -> int:
        """Return calls held by dead workers to the queue; return how many.

        A claim is abandoned when it is older than *max_age* and its worker
        has no heartbeat newer than ``now - max_age``.  The token is cleared,
        so the dead worker's process -- if it is somehow still alive -- gets a
        ``StaleClaim`` rather than overwriting the reply of whoever picked the
        call up next.
        """
        now = now or _utcnow()
        with self.transaction() as conn:
            result = conn.execute(q.reclaim_stale_update(now, max_age))
        return int(result.rowcount or 0)

    # -- job control ------------------------------------------------------

    def job_status(self, job_id: str) -> Dict[str, Any]:
        """Progress, outstanding work and blockers for one job.

        Keys:
            ``job_id``, ``status``: the job's own identity and state, so a
                caller does not need a second ``get_job``.
            ``n_items``: the job's stored item count.
            ``by_status``: item counts keyed by item status.
            ``percent_complete``: settled items / ``n_items`` * 100, to one
                decimal.  A job with no items reads 100.0 -- there is nothing
                outstanding, and 0.0 would make it look stuck forever.
            ``pending_calls_by_role_endpoint``: ``{role: {endpoint: count}}``
                over PENDING calls only.  A claimed call is work in flight,
                not work waiting.
            ``blockers``: see :meth:`_blockers`.
            ``throughput_last_10min``: calls of this job that finished in the
                last ten minutes.

        Raises:
            ValueError: no such job.
        """
        now = _utcnow()
        with self._read() as conn:
            row = conn.execute(
                sa.select(sch.jobs).where(sch.jobs.c.job_id == job_id)
            ).first()
            if row is None:
                raise ValueError(f"no such job: {job_id}")
            job = _dict(row)
            by_status = {
                str(r[0]): int(r[1])
                for r in conn.execute(q.select_item_status_counts(job_id))
            }
            pending = _dicts(conn.execute(q.select_pending_call_facets(job_id)))
            throughput = int(
                conn.execute(
                    q.count_calls_finished_since(job_id, now - _THROUGHPUT_WINDOW)
                ).scalar()
                or 0
            )

        n_items = int(job["n_items"] or 0)
        settled = sum(n for s, n in by_status.items() if s in q.SETTLED_ITEM_STATUSES)
        by_role_endpoint: Dict[str, Dict[str, int]] = {}
        for call in pending:
            per_endpoint = by_role_endpoint.setdefault(str(call["role"]), {})
            endpoint = str(call["endpoint"])
            per_endpoint[endpoint] = per_endpoint.get(endpoint, 0) + 1

        return {
            "job_id": job_id,
            "status": job["status"],
            "n_items": n_items,
            "by_status": by_status,
            "percent_complete": round(settled / n_items * 100, 1) if n_items else 100.0,
            "pending_calls_by_role_endpoint": by_role_endpoint,
            "blockers": self._blockers(job, pending, now),
            "throughput_last_10min": throughput,
        }

    def _blockers(
        self,
        job: Mapping[str, Any],
        pending: Sequence[Mapping[str, Any]],
        now: datetime,
    ) -> List[Dict[str, Any]]:
        """Why a job with outstanding work is not progressing.

        Each blocker is ``{"kind", "endpoint", "detail"}``.  Four kinds come
        from four different places, and they are reported INDEPENDENTLY --
        an endpoint can be both halted and unserved, and suppressing either
        fact would send the user to fix the wrong thing:

        * ``unservable`` -- the job was submitted with ``allow_unavailable``
          against an endpoint this machine could not serve, and it has
          outstanding calls on it.  Read from the JOB's own ``options``,
          where submit recorded it: the queue is per machine, so until that
          endpoint works here the calls wait, and nothing else in the store
          records why.
        * ``halted`` -- ``endpoint_state`` says the endpoint is halted
          (quota, auth, billing or budget).
        * ``waiting_for_model`` / ``configuration_mismatch`` -- pending calls
          whose LAST error names that kind (see
          :data:`.queries.BLOCKER_BY_ERROR_KIND`).
        * ``no_daemon`` -- the endpoint has pending calls and NO live worker
          in this store serves it, so nothing is going to run them.

        None of the three is scoped by ``jobs.submitted_by``: halts and
        workers belong to the store, not to a provenance label, and scoping
        them by one would report ``no_daemon`` about an endpoint a running
        worker was already draining.
        """
        endpoints = sorted({str(c["endpoint"]) for c in pending})
        if not endpoints:
            return []

        blockers: List[Dict[str, Any]] = []

        # Unservable endpoints, as recorded on the job at submit.  Reported
        # for EVERY recorded endpoint, not only the ones with calls pending
        # right now: a roster's critic and meta have no call rows until their
        # item's proposers have answered, and "the endpoint this job's critic
        # sits on does not work here" is a blocker from the moment the job is
        # queued rather than a surprise two rounds later.  (A job with no
        # outstanding work at all returned above.)
        unservable = dict((job.get("options") or {}).get("unservable_endpoints") or {})
        for endpoint in sorted(unservable):
            blockers.append(
                {
                    "kind": "unservable",
                    "endpoint": endpoint,
                    "detail": (
                        f"{unservable[endpoint] or 'unavailable on this machine'} "
                        "(the job was submitted with allow_unavailable, so its calls "
                        "wait until this endpoint works here; the queue is per "
                        "machine, so no other host will pick them up)"
                    ),
                }
            )

        # Halts.  One lookup per endpoint rather than one query: the endpoints
        # of a job are its roster's, so this is a handful of rows, and reusing
        # the contract method keeps the halt semantics in one place.
        for endpoint in endpoints:
            state = self.get_endpoint_state(endpoint)
            if state["halt_kind"]:
                detail = state["halt_kind"]
                if state["halt_message"]:
                    detail = f"{detail}: {state['halt_message']}"
                blockers.append(
                    {"kind": "halted", "endpoint": endpoint, "detail": detail}
                )

        # Last-error blockers, counted per (blocker kind, endpoint, error kind).
        tallies: Dict[tuple, int] = {}
        for call in pending:
            kind = q.error_kind(call["error"])
            blocker = q.BLOCKER_BY_ERROR_KIND.get(kind or "")
            if blocker is None:
                continue
            key = (blocker, str(call["endpoint"]), kind)
            tallies[key] = tallies.get(key, 0) + 1
        for (blocker, endpoint, kind), count in sorted(tallies.items()):
            blockers.append(
                {
                    "kind": blocker,
                    "endpoint": endpoint,
                    "detail": f"{count} pending call(s) last failed with {kind}",
                }
            )

        # No worker.  A worker that declared no endpoints is treated as
        # serving all of them: it is running, so "no daemon" would be a lie.
        live = self.live_workers(None, now)
        for endpoint in endpoints:
            if any(_serves(worker, endpoint) for worker in live):
                continue
            blockers.append(
                {
                    "kind": "no_daemon",
                    "endpoint": endpoint,
                    "detail": (
                        f"no live worker serves {endpoint!r}; start one with "
                        "`kbu council run` or the council daemon"
                    ),
                }
            )
        return blockers

    def results(
        self, job_id: str, include_pending: bool = False
    ) -> List[Dict[str, Any]]:
        """A job's items with their verdicts, in submission order.

        Settled items only by default: a pending item has no verdict, and
        returning it with a null one invites a caller to treat "not decided
        yet" as "decided nothing".
        """
        with self._read() as conn:
            return _dicts(conn.execute(q.select_results(job_id, include_pending)))

    # -- archive identity -------------------------------------------------
    #
    # See the "Archive identity" note near the top of this module for why a
    # store needs a declared id at all, why there is no automatic default,
    # and why the record lives in its own table outside the versioned
    # MetaData.

    def _create_store_identity_table(self, conn: sa.Connection) -> None:
        """Create the identity table if it is absent.  WRITE path only.

        Called by the two writers and never by a reader: a reader that
        created the table could not run against a read-only snapshot, and
        reading the identity off a read-only file is a thing that has to work
        (see :meth:`store_identity`).
        """
        conn.exec_driver_sql(
            f"CREATE TABLE IF NOT EXISTS {STORE_IDENTITY_TABLE} ("
            "declared_seq INTEGER PRIMARY KEY AUTOINCREMENT, "
            "record_version INTEGER NOT NULL, "
            "store_id TEXT NOT NULL, "
            "declared_at TEXT NOT NULL, "
            "declared_by TEXT, "
            "supersedes TEXT)"
        )

    def _store_identity_table_exists(self, conn: sa.Connection) -> bool:
        row = conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (STORE_IDENTITY_TABLE,),
        ).first()
        return row is not None

    def store_identity(self) -> Optional[Dict[str, Any]]:
        """Return this store's declared identity record, or ``None``.

        READ-ONLY, and safe on a store whose identity table does not exist
        (every store created before this feature) -- an absent table reads as
        "no identity declared", not as an error, and nothing is written.

        The record is the APPEND-ONLY table's highest ``declared_seq``, so a
        fork's re-declaration supersedes the previous row without erasing it.

        Returns:
            ``{"store_id", "declared_at", "declared_by", "record_version",
            "declared_seq", "supersedes"}`` for the current record, or
            ``None`` when this store carries no declared identity.
        """
        with self._read() as conn:
            if not self._store_identity_table_exists(conn):
                return None
            row = conn.exec_driver_sql(
                "SELECT declared_seq, record_version, store_id, declared_at, "
                f"declared_by, supersedes FROM {STORE_IDENTITY_TABLE} "
                "ORDER BY declared_seq DESC LIMIT 1"
            ).first()
        if row is None:
            return None
        return {
            "declared_seq": row[0],
            "record_version": row[1],
            "store_id": row[2],
            "declared_at": row[3],
            "declared_by": row[4],
            "supersedes": row[5],
        }

    def assign_store_identity(
        self,
        store_id: str,
        *,
        declared_by: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Declare this store's archive identity.  IDEMPOTENT.

        Declaring the id the store already carries writes nothing and returns
        the existing record, so a migration script may be run any number of
        times.  Declaring a DIFFERENT id over an existing one RAISES: that is
        the independently-writable-fork case, and it needs the explicit act
        (:meth:`redeclare_store_identity`) rather than happening because
        somebody re-ran a command with a changed argument.

        Args:
            store_id: The id to declare, validated by
                :func:`validate_store_id`.  Nothing here invents one.
            declared_by: Who declared it.  ``None`` falls back to the OS
                user; the field exists for accountability, so it is never
                left blank.
            now: Timestamp to record.  Defaults to now, UTC.

        Returns:
            The current identity record, as :meth:`store_identity` returns.

        Raises:
            ValueError: *store_id* is not a valid declared id.
            StoreIdentityAlreadyDeclared: a DIFFERENT id is already declared.
        """
        declared = validate_store_id(store_id)
        existing = self.store_identity()
        if existing is not None:
            if existing["store_id"] == declared:
                return existing
            raise StoreIdentityAlreadyDeclared(
                f"this store already declares store_id "
                f"{existing['store_id']!r} (declared "
                f"{existing['declared_at']} by "
                f"{existing['declared_by']!r}); refusing to replace it with "
                f"{declared!r}. A declared identity never changes -- every "
                "row already archived under the old id is keyed on it, and "
                "the clearinghouse payload carrying it is frozen on first "
                "write. If this really is an independently writable FORK of "
                "that store, say so explicitly with "
                "redeclare_store_identity()."
            )
        return self._write_store_identity(
            declared,
            declared_by=declared_by,
            now=now,
            supersedes=None,
        )

    def redeclare_store_identity(
        self,
        store_id: str,
        *,
        declared_by: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Declare a NEW identity for an independently writable fork.

        THE EXPLICIT ACT, and the only way a declared identity is ever
        replaced.  A copied store file carries the original identity, which
        is correct for a MOVE or a read-only snapshot -- the identity must
        survive those.  It is wrong the moment the copy starts taking writes
        of its own: two independently writable stores sharing one id silently
        merge two unrelated histories into one archived table.

        The previous id is recorded in ``supersedes`` and its row is left in
        place, because a later reader has no other way to tell a fork from a
        store that always had its current id.

        Idempotent in the same way as :meth:`assign_store_identity`:
        re-declaring the id already current writes nothing.

        Args:
            store_id: The fork's NEW id, validated by
                :func:`validate_store_id`.  Must differ from the current one
                unless it is the current one (in which case this is a no-op).
            declared_by: Who declared it.  ``None`` falls back to the OS user.
            now: Timestamp to record.  Defaults to now, UTC.

        Returns:
            The new current identity record.

        Raises:
            ValueError: *store_id* is not a valid declared id.
        """
        declared = validate_store_id(store_id)
        existing = self.store_identity()
        if existing is not None and existing["store_id"] == declared:
            return existing
        return self._write_store_identity(
            declared,
            declared_by=declared_by,
            now=now,
            supersedes=None if existing is None else existing["store_id"],
        )

    def _write_store_identity(
        self,
        store_id: str,
        *,
        declared_by: Optional[str],
        now: Optional[datetime],
        supersedes: Optional[str],
    ) -> Dict[str, Any]:
        """Append one identity record.  The only writer of that table."""
        when = (now or _utcnow()).astimezone(timezone.utc).isoformat()
        who = declared_by or os.environ.get("USER") or "unknown"
        with self.transaction() as conn:
            self._create_store_identity_table(conn)
            conn.exec_driver_sql(
                f"INSERT INTO {STORE_IDENTITY_TABLE} "
                "(record_version, store_id, declared_at, declared_by, "
                "supersedes) VALUES (?, ?, ?, ?, ?)",
                (
                    STORE_IDENTITY_RECORD_VERSION,
                    store_id,
                    when,
                    who,
                    supersedes,
                ),
            )
        record = self.store_identity()
        assert record is not None  # just written
        return record

    def require_store_identity(
        self,
        *,
        operation: str,
        store_id: Any = _USE_CONFIGURED,
    ) -> Dict[str, Any]:
        """Return the stored identity, or refuse.  The archive gate.

        Two refusals, and NEITHER is auto-healed:

        * no identity is declared at all -- the store may not be archived,
          because every archived row is keyed on an id that does not exist;
        * a configured identity disagrees with the stored one -- which means
          either somebody copied a store and kept writing to it, or a config
          is pointed at the wrong file.  The message names BOTH values so the
          operator can tell which, and nothing is rewritten: silently
          preferring either side is how two histories get merged.

        Args:
            operation: What is being refused ("snapshot", "export", ...).
                Quoted in the message, so the refusal says what did not run.
            store_id: The configured identity to compare against.  Defaults
                to the one this store was opened with
                (``self.configured_store_id``).  Pass ``None`` explicitly to
                skip the comparison and only require presence.

        Returns:
            The stored identity record.

        Raises:
            StoreIdentityMissing: no identity is declared.
            StoreIdentityMismatch: the configured and stored ids differ.
        """
        configured = (
            self.configured_store_id if store_id is _USE_CONFIGURED else store_id
        )
        record = self.store_identity()
        if record is None:
            raise StoreIdentityMissing(
                f"refusing to {operation} {self.url!r}: this store carries NO "
                "declared archive identity, and every row archived from it "
                "is keyed on store_id. Declare one explicitly first -- "
                "store.assign_store_identity('<declared-id>', "
                "declared_by='<you>') -- or, for the existing poplar store, "
                "run the documented one-off (see POPLAR_COUNCIL_STORE_ID). "
                "Nothing is assigned automatically: an invented id looks "
                "joinable and cannot be corrected once a clearinghouse "
                "payload carries it."
            )
        if configured is not None and configured != record["store_id"]:
            raise StoreIdentityMismatch(
                f"refusing to {operation} {self.url!r}: the CONFIGURED "
                f"store_id is {configured!r} but the identity STORED in this "
                f"database is {record['store_id']!r} (declared "
                f"{record['declared_at']} by {record['declared_by']!r}). "
                "These must agree and this is NOT auto-healed. A mismatch "
                "usually means a store file was copied and then written to, "
                "which is the one case that must not be papered over -- if "
                "this really is an independently writable fork, declare its "
                "new identity explicitly with redeclare_store_identity(); if "
                "the config is pointed at the wrong file, fix the config."
            )
        return record

    def snapshot(
        self,
        destination: Any,
        *,
        store_id: Any = _USE_CONFIGURED,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """Write a consistent read-only snapshot of this store, and describe it.

        Uses SQLite's own BACKUP API rather than copying the file: a live
        store is a database plus a write-ahead log, and a plain ``cp`` of the
        pair mid-transaction produces a database that opens and is wrong.
        The snapshot carries the identity record with it, because the record
        is a table inside the file -- which is the whole point of putting it
        there.

        REFUSES before writing anything when this store has no declared
        identity, or when the configured and stored identities disagree (see
        :meth:`require_store_identity`).

        Args:
            destination: Path to write the snapshot to.
            store_id: Configured identity to check.  See
                :meth:`require_store_identity`.
            overwrite: Permit replacing an existing destination file.  Off by
                default: silently overwriting somebody's archive source is
                not a thing this should do by accident.

        Returns:
            A manifest dict: ``store_id`` and the full ``identity`` record,
            the destination ``path``, ``bytes``, the VERIFIED
            ``snapshot_id`` (the sha256 of the snapshot file as written,
            which is what ``council_schema``'s ``snapshot_id`` column holds),
            ``store_host``, ``taken_at``, and per-table ``row_counts``.

        Raises:
            StoreIdentityMissing: no identity is declared.
            StoreIdentityMismatch: configured and stored ids differ.
            FileExistsError: the destination exists and *overwrite* is False.
            StoreError: this is an in-memory store, which has no file to
                snapshot.
        """
        identity = self.require_store_identity(
            operation="snapshot", store_id=store_id
        )
        if self.path is None:
            raise StoreError(
                "refusing to snapshot an in-memory store: there is no file "
                "to snapshot and nothing would be archivable from the result"
            )
        target = Path(destination).expanduser()
        if target.exists() and not overwrite:
            raise FileExistsError(
                f"refusing to overwrite {str(target)!r}; pass overwrite=True "
                "if replacing it is really what you mean"
            )
        target.parent.mkdir(parents=True, exist_ok=True)

        # The backup API runs against the live database and produces a
        # transactionally consistent copy, WAL included.
        source = sqlite3.connect(str(self.path))
        try:
            written = sqlite3.connect(str(target))
            try:
                source.backup(written)
            finally:
                written.close()
        finally:
            source.close()

        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)

        return {
            "store_id": identity["store_id"],
            "identity": identity,
            "path": str(target),
            "bytes": target.stat().st_size,
            "snapshot_id": digest.hexdigest(),
            "store_host": socket.gethostname(),
            "taken_at": _utcnow().isoformat(),
            # Counted FROM THE SNAPSHOT, not from this store.  The live
            # counts can differ: `_row_counts` reads through this store's
            # connection, which JOINS a write transaction the caller already
            # has open (``transaction()`` is re-entrant), so it sees
            # uncommitted rows that the backup API deliberately leaves out.
            # A manifest whose row counts describe a state the snapshot does
            # not contain is the same class of silent wrongness as the torn
            # file copy this method exists to prevent -- and it is worse,
            # because an archiver reconciling against it would conclude the
            # snapshot was short.
            "row_counts": _snapshot_row_counts(target),
        }

    def export(
        self,
        destination: Any,
        *,
        store_id: Any = _USE_CONFIGURED,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """Export every store table as JSONL into *destination*, and describe it.

        One ``<table>.jsonl`` per table of :data:`.schema.TABLE_NAMES`, plus a
        ``MANIFEST.json`` carrying the declared identity, a per-file sha256
        and a per-table row count.  The identity is in the manifest rather
        than only in the filenames because a directory of JSONL carries no
        database to read it back out of -- which is exactly why an export,
        like a snapshot, may not run without one.

        REFUSES before writing anything when this store has no declared
        identity, or when the configured and stored identities disagree.

        Args:
            destination: Directory to write into.  Created if absent.
            store_id: Configured identity to check.  See
                :meth:`require_store_identity`.
            overwrite: Permit writing into a directory that already holds an
                export (i.e. a ``MANIFEST.json``).

        Returns:
            The manifest dict, as also written to ``MANIFEST.json``.

        Raises:
            StoreIdentityMissing: no identity is declared.
            StoreIdentityMismatch: configured and stored ids differ.
            FileExistsError: the destination already holds an export and
                *overwrite* is False.
        """
        identity = self.require_store_identity(
            operation="export", store_id=store_id
        )
        target = Path(destination).expanduser()
        manifest_path = target / "MANIFEST.json"
        if manifest_path.exists() and not overwrite:
            raise FileExistsError(
                f"refusing to overwrite the export already in "
                f"{str(target)!r}; pass overwrite=True if replacing it is "
                "really what you mean"
            )
        target.mkdir(parents=True, exist_ok=True)

        files: Dict[str, Any] = {}
        with self._read() as conn:
            for name in sch.TABLE_NAMES:
                table = sch.metadata.tables[name]
                path = target / f"{name}.jsonl"
                rows = 0
                digest = hashlib.sha256()
                with path.open("w", encoding="utf-8") as handle:
                    for row in conn.execute(sa.select(table)):
                        line = json.dumps(
                            _export_row(row), sort_keys=True, default=str
                        )
                        handle.write(line + "\n")
                        digest.update((line + "\n").encode("utf-8"))
                        rows += 1
                files[f"{name}.jsonl"] = {
                    "rows": rows,
                    "sha256": digest.hexdigest(),
                }

        manifest = {
            "store_id": identity["store_id"],
            "identity": identity,
            "store_host": socket.gethostname(),
            "exported_at": _utcnow().isoformat(),
            "schema_version": sch.SCHEMA_VERSION,
            "files": files,
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return manifest

    def _row_counts(self) -> Dict[str, int]:
        """``{table: row count}`` over every table of the council schema.

        THESE ARE THE LIVE STORE'S COUNTS AND MUST NOT BE USED TO DESCRIBE A
        SNAPSHOT.  ``_read()`` goes through this store's connection, and
        :meth:`transaction` is RE-ENTRANT, so a call made inside a write
        transaction the caller already has open sees UNCOMMITTED rows --
        which the sqlite3 backup API deliberately leaves out of a snapshot.
        :meth:`snapshot` used to build its manifest from here and reported 2
        rows where its own snapshot held 1; it now uses
        :func:`_snapshot_row_counts`, which reads the written file.
        """
        counts: Dict[str, int] = {}
        with self._read() as conn:
            for name in sch.TABLE_NAMES:
                table = sch.metadata.tables[name]
                counts[name] = int(
                    conn.execute(
                        sa.select(sa.func.count()).select_from(table)
                    ).scalar_one()
                )
        return counts

    def cancel_job(self, job_id: str, user: Optional[str] = None) -> None:
        """Cancel a job in ONE transaction.  Terminal and idempotent.

        The job goes to ``cancelled``, every unsettled item to ``cancelled``
        and every PENDING call to ``superseded`` (``live`` false).  Claimed
        calls are left alone: a worker is already paying for them and their
        replies are stored and stay reusable, which is the cheapest outcome
        available once the money is spent.  ``add_calls`` and ``settle_item``
        refuse the job from here on.

        *user* is a deprecated no-op.  A store is single-user and
        ``submitted_by`` is a provenance label, so there is no ownership to
        enforce (review round 2): refusing on it would lock a user out of
        their own jobs after a rename.

        Raises:
            ValueError: no such job.
        """
        now = _utcnow()
        with self.transaction() as conn:
            job = self._job(conn, job_id)
            if job["status"] == "cancelled":
                return
            conn.execute(q.cancel_job_update(job_id))
            conn.execute(q.cancel_unsettled_items_update(job_id, now))
            conn.execute(q.supersede_pending_calls_update(job_id))

    def delete_job(self, job_id: str, user: Optional[str] = None) -> None:
        """Tombstone a cancelled or done job.  No row is physically removed.

        Only ``deleted_at`` is set, so the job's calls stay in the store and
        stay reusable: a later job of yours may be the one that benefits from
        an answer this one paid for.  ``list_jobs`` hides it unless asked.

        *user* is a deprecated no-op, for the same reason as in
        :meth:`cancel_job`.

        Raises:
            ValueError: no such job, or it is still running/queued/paused --
                cancel it first, so there is no worker still writing to it.
        """
        with self.transaction() as conn:
            job = self._job(conn, job_id)
            if job["status"] not in ("cancelled", "done"):
                raise ValueError(
                    f"job {job_id} is {job['status']!r}; only a cancelled or done "
                    "job may be deleted -- cancel it first"
                )
            conn.execute(q.tombstone_job_update(job_id, _utcnow()))

    @staticmethod
    def _job(conn: sa.Connection, job_id: str) -> Dict[str, Any]:
        """Return the job row, or raise if there is no such job."""
        row = conn.execute(
            sa.select(sch.jobs).where(sch.jobs.c.job_id == job_id)
        ).first()
        if row is None:
            raise ValueError(f"no such job: {job_id}")
        return _dict(row)


# ---------------------------------------------------------------------------
# URL handling and engine setup
# ---------------------------------------------------------------------------


def _version_mismatch(found: int) -> SchemaVersionMismatch:
    return SchemaVersionMismatch(
        f"this council store is schema version {found}, but this KBUtilLib "
        f"expects version {sch.SCHEMA_VERSION}. Run `kbu council db migrate` "
        "to bring it up to date."
    )


def _canonical(value: Any) -> str:
    """Canonical JSON of *value*, for comparing two query-type specs."""
    from ..types import canonical_json

    return canonical_json(value)


def _resolve_sqlite_url(
    url: str, *, filesystem_type: Callable[[Path], Optional[str]] = None
) -> tuple:
    """Return ``(sqlalchemy_url, path)`` for *url*, refusing bad locations.

    Accepts a SQLAlchemy URL or a bare filesystem path, because a config file
    and a ``--store`` flag disagree about which they hold and neither caller
    should have to care.  ``path`` is None for an in-memory database.

    Two refusals, in this order: a Dropbox path component (never overridable),
    then a network filesystem (see :func:`refuse_network_filesystem`).

    Args:
        filesystem_type: the injection seam for the second refusal -- a
            callable taking a path and returning its filesystem type.
            Defaults to :func:`filesystem_type_of`.  Tests substitute THIS
            and nothing else, so the path normalisation above it is the
            shipped code either way.
    """
    text = (url or "").strip()
    if not text:
        raise StorePathRefused("a council store URL or path is required, got an empty string")

    if text.lower().startswith("sqlite"):
        scheme, sep, rest = text.partition(":///")
        if not sep:
            # "sqlite://" with no path is pysqlite's in-memory database.
            return text, None
        path_str = rest
    else:
        scheme, path_str = "sqlite", text

    if path_str in ("", ":memory:"):
        return "sqlite://", None

    # "sqlite:///~/x.sqlite" leaves a leading slash in front of the tilde.
    if path_str.startswith("/~"):
        path_str = path_str[1:]
    path = Path(path_str).expanduser()

    # Both the literal path and, when it is absolute, where it actually points:
    # a symlink into a synced tree is the same hazard as naming one directly.
    candidates = [path]
    if path.is_absolute():
        candidates.append(Path(os.path.realpath(path)))
    for candidate in candidates:
        for part in candidate.parts:
            lowered = part.lower()
            # Business Dropbox installs are named e.g. "Dropbox (Argonne
            # National Laboratory)"; the hazard is identical, so match both.
            if lowered == "dropbox" or lowered.startswith("dropbox ("):
                raise StorePathRefused(
                    f"refusing a sqlite council store inside a synced directory: "
                    f"{str(candidate)!r} (component {part!r}). A live SQLite "
                    "database is a file plus a write-ahead log, and file sync "
                    "corrupts the pair; set `store:` to a path on local disk."
                )

    refuse_network_filesystem(
        path, filesystem_type=filesystem_type or filesystem_type_of
    )

    # Deliberately NOT resolve()d: the caller's own spelling of the path is what
    # goes back in the URL, so a test (or a user) that asks where the file is
    # gets the place they named rather than a symlink's target.
    return f"sqlite:///{path}", path


# ---------------------------------------------------------------------------
# The network-filesystem refusal
# ---------------------------------------------------------------------------

#: Filesystem types a SQLite council store may not live on.  Every one of them
#: either does not implement the POSIX advisory locks SQLite's WAL mode relies
#: on, or implements them in a way that silently does not hold across clients
#: -- which does not fail loudly, it corrupts the database.
NETWORK_FILESYSTEMS = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smbfs",
        "smb2",
        "smb3",
        "afpfs",
        "webdav",
        "fuse.sshfs",
        "sshfs",
        "9p",
        "lustre",
        "gpfs",
        "beegfs",
        "ceph",
        "fuse.ceph",
    }
)

#: Set this to ``1`` to open a store on a network filesystem anyway.  It exists
#: because an operator who knows their NFS mount is single-client is entitled
#: to that call; the Dropbox refusal has no such escape, because no spelling of
#: "I know what I am doing" makes a file syncer safe for a WAL pair.
ALLOW_NETWORK_FS_ENV = "KBU_COUNCIL_ALLOW_NETWORK_FS"


def nearest_existing_ancestor(path: Path) -> Optional[Path]:
    """The closest existing directory at or above *path*.

    The store file itself usually does not exist yet -- creating it is the
    point -- so the filesystem question has to be asked of the directory it
    will land in, or of that directory's parent if it is also still to be
    created.
    """
    candidate = path if path.is_absolute() else Path(os.path.abspath(path))
    for ancestor in [candidate, *candidate.parents]:
        if ancestor.exists():
            return ancestor
    return None


def filesystem_type_of(path: Path) -> Optional[str]:
    """The filesystem type of *path*, which must EXIST, lowercased.

    This is the whole injection seam for :func:`refuse_network_filesystem`:
    one small function, platform-specific, that answers one question about one
    existing directory.  Everything else in the refusal -- which types are
    refused, the override, finding the directory to ask about -- is
    platform-neutral and sits above it.

    ``None`` means "could not tell", not "fine": the caller treats it as
    allowed-with-a-warning, because refusing a store because this function
    could not answer would break a perfectly good local disk on a platform
    nobody tested.

    Linux reads ``/proc/self/mounts`` and takes the LONGEST matching mount
    point, which is what makes a store under a network mount nested inside a
    local one report the network type rather than the local one.  macOS asks
    ``stat -f %T``, which is ``statfs``'s ``f_fstypename`` (see
    :func:`_macos_fstype` for why not :mod:`ctypes`).
    """
    if sys.platform == "darwin":
        return _macos_fstype(path)
    return _linux_fstype(path)


def _linux_fstype(path: Path) -> Optional[str]:
    """The type of the longest mount point in ``/proc/self/mounts`` under *path*."""
    try:
        mounts = Path("/proc/self/mounts").read_text()
    except OSError:
        return None
    target = os.path.realpath(path)
    best: Optional[str] = None
    best_len = -1
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        # The kernel octal-escapes spaces and tabs in mount points.
        point = fields[1].replace("\\040", " ").replace("\\011", "\t")
        if target == point or target.startswith(point.rstrip("/") + "/"):
            if len(point) > best_len:
                best, best_len = fields[2].lower(), len(point)
    return best


def _macos_fstype(path: Path) -> Optional[str]:
    """``statfs(2)``'s ``f_fstypename`` for *path*, read via ``stat -f %T``.

    ``stat -f %T`` is exactly ``f_fstypename`` -- "nfs", "smbfs", "apfs" --
    and is the sanctioned way to ask for it from outside C.  Deliberately NOT
    ``ctypes`` into ``libc.statfs``: that means hard-coding the byte offset of
    that field inside ``struct statfs``, whose preceding fields vary by
    architecture and SDK, and a wrong offset would return plausible-looking
    rubbish rather than nothing -- which would refuse a good local disk or
    admit a network mount.  A subprocess once per store open is cheap enough
    to be worth not guessing.
    """
    try:
        import subprocess

        out = subprocess.run(
            ["stat", "-f", "%T", str(path)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if out.returncode != 0:
            return None
        value = (out.stdout or "").strip().lower()
        return value or None
    except Exception:  # noqa: BLE001 - "could not tell" is an allowed answer
        return None


def refuse_network_filesystem(
    path: Path,
    *,
    filesystem_type: Callable[[Path], Optional[str]] = filesystem_type_of,
) -> None:
    """Refuse *path* if it is on a filesystem in :data:`NETWORK_FILESYSTEMS`.

    Overridable with :data:`ALLOW_NETWORK_FS_ENV`, unlike the Dropbox refusal.
    An UNDETERMINABLE type is allowed with a logged warning: the check exists
    to catch the mistake users actually make, and failing closed on a platform
    whose filesystem type this code cannot read would refuse good local disks.
    """
    if os.environ.get(ALLOW_NETWORK_FS_ENV, "").strip() == "1":
        return
    # The store file usually does not exist yet, and nor may its directory --
    # creating them is what the caller is about to do -- so the question goes
    # to the nearest ancestor that IS there.
    ancestor = nearest_existing_ancestor(path)
    found = (filesystem_type(ancestor) or "").strip().lower() if ancestor else ""
    if not found:
        logger.warning(
            "council store: could not determine the filesystem type of %s; "
            "opening it anyway. A SQLite store on a network filesystem (NFS, "
            "SMB, sshfs, ...) can be corrupted by locks that do not hold.",
            ancestor or path,
        )
        return
    if found in NETWORK_FILESYSTEMS:
        raise StorePathRefused(
            f"refusing a sqlite council store on a {found!r} filesystem: "
            f"{str(path)!r}. SQLite's WAL mode needs POSIX locks that a "
            "network filesystem either does not implement or does not honour "
            "across clients, which corrupts the database rather than failing. "
            "Set `store:` to a path on local disk (for example "
            "~/.kbutillib/council/council.sqlite). If you know this mount is "
            f"used by one machine only, set {ALLOW_NETWORK_FS_ENV}=1."
        )


def _install_sqlite_pragmas(engine: sa.Engine) -> None:
    """Attach the connection and transaction behaviour SQLite needs.

    ``isolation_level = None`` turns OFF pysqlite's own implicit transaction
    handling so this module can choose ``BEGIN`` or ``BEGIN IMMEDIATE`` per
    transaction; without it, pysqlite would open a deferred transaction behind
    our back and ``BEGIN IMMEDIATE`` would be a nested-transaction error.  It
    is also what lets ``PRAGMA journal_mode=WAL`` run, since that pragma
    cannot execute inside a transaction.
    """

    @sa.event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:  # pragma: no cover - driver hook
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    @sa.event.listens_for(engine, "begin")
    def _on_begin(conn: sa.Connection) -> None:  # pragma: no cover - driver hook
        if conn.info.get("council_begin_immediate", False):
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            conn.exec_driver_sql("BEGIN")
