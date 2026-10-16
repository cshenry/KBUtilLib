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

The queue half -- claiming, completing, releasing and reclaiming calls, and
the job control built on them -- is here too, but its SQL is not: every
statement it runs is built in :mod:`.queries`, which holds the queue's
*meaning* without any locking, and this module contributes the lock (``BEGIN
IMMEDIATE``, above) that makes a claim atomic.
"""

from __future__ import annotations

import os
import re
import socket
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Set

import sqlalchemy as sa

from . import queries as q
from . import schema as sch
from .errors import (
    JobCancelled,
    QueryTypeExists,
    SchemaVersionMismatch,
    StaleClaim,
    StorePathRefused,
)

__all__ = ["SqliteStore", "STORE_IDENTITY"]

#: Re-exported from :mod:`.queries`: the fixed ``endpoint_state.user`` value
#: every row in a store is keyed under.  See that module for why.
STORE_IDENTITY = q.STORE_IDENTITY

#: Window ``job_status`` reports throughput over.
_THROUGHPUT_WINDOW = timedelta(minutes=10)


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

    Raises:
        StorePathRefused: the path has a Dropbox component.
        SchemaVersionMismatch: the file carries a different schema version.
    """

    #: Dialect name, for callers that branch on it.
    dialect = "sqlite"

    def __init__(
        self,
        url: str,
        *,
        create_parents: bool = True,
        check_version: bool = True,
    ) -> None:
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
        """
        current = self.schema_version()
        if current == 0:
            self.init_schema()
            return sch.SCHEMA_VERSION
        if current == sch.SCHEMA_VERSION:
            self.add_missing_columns()
            return current
        if current == 1:
            self._migrate_v1_to_v2()
            self.add_missing_columns()
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
    ) -> List[Dict[str, Any]]:
        """Atomically claim up to *limit* due pending calls for this worker.

        Claims across the WHOLE store: every non-cancelled, non-paused,
        non-tombstoned job, whatever provenance label its ``submitted_by``
        carries.  A store is one machine's and its queue is one queue, so
        filtering on that label would strand every pending call the moment
        somebody edited config ``user`` (review round 3).  *user* is a
        deprecated no-op kept for older callers and is ignored.

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
            rows = _dicts(conn.execute(q.select_claimable(endpoints, now, limit)))
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

    def record_attempt(
        self,
        call_id: str,
        attempt_no: int,
        started_at: datetime,
        finished_at: datetime,
        outcome_kind: str,
        usage: Optional[Mapping[str, Any]] = None,
        worker_id: Optional[str] = None,
    ) -> None:
        """Record one try at a call.

        Raises:
            ValueError: any argument carries prompt text -- a dict, at any
                depth, with a ``system`` or ``user`` key.  The attempts table
                is the audit trail and is readable by every user of a shared
                store; see :func:`.queries.refuse_prompt_text`.
        """
        q.refuse_prompt_text(
            call_id=call_id,
            attempt_no=attempt_no,
            started_at=started_at,
            finished_at=finished_at,
            outcome_kind=outcome_kind,
            usage=usage,
            worker_id=worker_id,
        )
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

        Each blocker is ``{"kind", "endpoint", "detail"}``.  Three kinds come
        from three different places, and they are reported INDEPENDENTLY --
        an endpoint can be both halted and unserved, and suppressing either
        fact would send the user to fix the wrong thing:

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


def _resolve_sqlite_url(url: str) -> tuple:
    """Return ``(sqlalchemy_url, path)`` for *url*, refusing synced locations.

    Accepts a SQLAlchemy URL or a bare filesystem path, because a config file
    and a ``--store`` flag disagree about which they hold and neither caller
    should have to care.  ``path`` is None for an in-memory database.
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
                    "corrupts the pair; put the store outside Dropbox, or use a "
                    "PostgreSQL URL."
                )

    # Deliberately NOT resolve()d: the caller's own spelling of the path is what
    # goes back in the URL, so a test (or a user) that asks where the file is
    # gets the place they named rather than a symlink's target.
    return f"sqlite:///{path}", path


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
