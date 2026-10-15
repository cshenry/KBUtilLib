"""Local SQLite implementation of the council store.

This is the default store: one file per user, on local disk, holding that
user's jobs, items, call rows and verdicts plus the leases that stop two
workers doing the same work.  :class:`~.postgres.PostgresStore` implements the
same contract against a shared database; both sit on the single schema in
:mod:`.schema`, so a call row written by one is readable by the other.

Three SQLite-specific things are handled here and nowhere else:

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

Queue operations (claiming, completing and settling calls, and the job
control built on them) are deliberately absent: they raise
``NotImplementedError("store-queue-ops")`` and land in the next milestone.
Everything the submit path and the CLI need -- schema, query types, job and
item CRUD, endpoint budget state, workers, leases and search -- is here.
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

from . import schema as sch
from .errors import (
    JobCancelled,
    QueryTypeExists,
    SchemaVersionMismatch,
    StorePathRefused,
)

__all__ = ["SqliteStore"]

#: Raised by every method the next milestone fills in, with this exact
#: message so a partially-built store is obvious in a traceback.
_QUEUE_OPS = "store-queue-ops"


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

        At version 1 there is nothing to migrate from, so this is
        ``init_schema()`` for an unstamped database and a no-op for a current
        one.  A database stamped NEWER than this code is refused: downgrading
        would silently drop columns a newer council wrote.
        """
        current = self.schema_version()
        if current == 0:
            self.init_schema()
            return sch.SCHEMA_VERSION
        if current == sch.SCHEMA_VERSION:
            return current
        raise _version_mismatch(current)

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

    def get_endpoint_state(self, user: str, endpoint: str) -> Dict[str, Any]:
        """Return the (user, endpoint) budget/halt row.

        A never-seen endpoint returns a zeroed, un-halted row rather than
        None: every caller asks "is this halted and how much have I spent?",
        and a None would put the same two-line default at every call site.
        """
        with self._read() as conn:
            row = conn.execute(
                sa.select(sch.endpoint_state).where(
                    sch.endpoint_state.c.user == user,
                    sch.endpoint_state.c.endpoint == endpoint,
                )
            ).first()
        if row is None:
            return {
                "user": user,
                "endpoint": endpoint,
                "budget_epoch": 0,
                "epoch_tokens": 0,
                "lifetime_tokens": 0,
                "halt_kind": None,
                "halt_message": None,
                "updated_at": None,
            }
        return _dict(row)

    def add_tokens(self, user: str, endpoint: str, n: int) -> Dict[str, Any]:
        """Add *n* tokens to both the current epoch and the lifetime total."""
        n = int(n)
        with self.transaction() as conn:
            row = self._endpoint_row(conn, user, endpoint)
            conn.execute(
                self._endpoint_update(user, endpoint).values(
                    epoch_tokens=row["epoch_tokens"] + n,
                    lifetime_tokens=row["lifetime_tokens"] + n,
                    updated_at=_utcnow(),
                )
            )
        return self.get_endpoint_state(user, endpoint)

    def set_halt(
        self, user: str, endpoint: str, kind: str, message: str = ""
    ) -> Dict[str, Any]:
        """Halt an endpoint for one user: no further calls are claimed for it."""
        with self.transaction() as conn:
            self._endpoint_row(conn, user, endpoint)
            conn.execute(
                self._endpoint_update(user, endpoint).values(
                    halt_kind=kind, halt_message=message, updated_at=_utcnow()
                )
            )
        return self.get_endpoint_state(user, endpoint)

    def reset_halt(self, user: str, endpoint: str) -> Dict[str, Any]:
        """Clear a halt.  Token counters are NEVER touched.

        Clearing a halt says "the problem is fixed, try again"; it does not
        say "the tokens were not spent".  Resetting the budget is
        :meth:`reset_budget`, a separate and deliberate act.
        """
        with self.transaction() as conn:
            self._endpoint_row(conn, user, endpoint)
            conn.execute(
                self._endpoint_update(user, endpoint).values(
                    halt_kind=None, halt_message=None, updated_at=_utcnow()
                )
            )
        return self.get_endpoint_state(user, endpoint)

    def reset_budget(self, user: str, endpoint: str) -> Dict[str, Any]:
        """Start a new budget epoch: epoch tokens to 0, lifetime kept.

        Also clears a halt that was caused by the budget itself, since the
        reason for it no longer holds.  A quota, auth or billing halt is left
        alone -- a new epoch does not fix an expired credential.
        """
        with self.transaction() as conn:
            row = self._endpoint_row(conn, user, endpoint)
            values: Dict[str, Any] = {
                "budget_epoch": row["budget_epoch"] + 1,
                "epoch_tokens": 0,
                "updated_at": _utcnow(),
            }
            if row["halt_kind"] in ("budget_exhausted", "budget_usage_unknown"):
                values["halt_kind"] = None
                values["halt_message"] = None
            conn.execute(self._endpoint_update(user, endpoint).values(**values))
        return self.get_endpoint_state(user, endpoint)

    def _endpoint_row(
        self, conn: sa.Connection, user: str, endpoint: str
    ) -> Dict[str, Any]:
        """Return the (user, endpoint) row, inserting a zeroed one if absent."""
        row = conn.execute(
            sa.select(sch.endpoint_state).where(
                sch.endpoint_state.c.user == user,
                sch.endpoint_state.c.endpoint == endpoint,
            )
        ).first()
        if row is not None:
            return _dict(row)
        values = _filled(
            sch.endpoint_state,
            {
                "user": user,
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
    def _endpoint_update(user: str, endpoint: str) -> Any:
        return sch.endpoint_state.update().where(
            sch.endpoint_state.c.user == user,
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

    def request_stop(self, user: str) -> bool:
        """Ask the user's daemon to stop, via the holder of its lease.

        Addressed through the lease rather than by worker id because the
        caller is a CLI that knows the user, not which process currently holds
        the daemon role.  Returns False if the user has no lease holder.
        """
        with self.transaction() as conn:
            lease = conn.execute(
                sa.select(sch.daemon_lease.c.holder_worker_id).where(
                    sch.daemon_lease.c.user == user
                )
            ).first()
            if lease is None or lease[0] is None:
                return False
            result = conn.execute(
                sch.workers.update()
                .where(sch.workers.c.worker_id == lease[0])
                .values(stop_requested_at=_utcnow())
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

    # -- leases -----------------------------------------------------------

    def acquire_daemon_lease(
        self,
        user: str,
        worker_id: str,
        now: Optional[datetime] = None,
        ttl: timedelta = timedelta(minutes=2),
    ) -> Optional[int]:
        """Take the user's single-daemon lease, or return None if it is held.

        Succeeds only when there is no row or the existing one has expired --
        including for the CURRENT holder, which must use
        :meth:`renew_daemon_lease` instead.  Each successful acquisition
        increments ``generation``, which never resets: that is how a call
        claimed by a superseded daemon is recognised and refused when its late
        reply arrives.
        """
        now = now or _utcnow()
        with self.transaction() as conn:
            return self._acquire(
                conn,
                sch.daemon_lease,
                sch.daemon_lease.c.user == user,
                {"user": user},
                worker_id,
                now,
                ttl,
            )

    def renew_daemon_lease(
        self,
        user: str,
        worker_id: str,
        generation: int,
        now: Optional[datetime] = None,
        ttl: timedelta = timedelta(minutes=2),
    ) -> bool:
        """Extend the lease.  False once the generation has been superseded."""
        now = now or _utcnow()
        with self.transaction() as conn:
            return self._renew(
                conn,
                sch.daemon_lease,
                sch.daemon_lease.c.user == user,
                worker_id,
                generation,
                now,
                ttl,
            )

    def release_daemon_lease(self, user: str, worker_id: str, generation: int) -> bool:
        """Give the lease up.  The generation is kept, never rolled back."""
        with self.transaction() as conn:
            return self._release(
                conn,
                sch.daemon_lease,
                sch.daemon_lease.c.user == user,
                worker_id,
                generation,
            )

    #: The gpu_lease table is a singleton; this is its only row id.
    GPU_LEASE_ID = 1

    def acquire_gpu_lease(
        self,
        worker_id: str,
        now: Optional[datetime] = None,
        ttl: timedelta = timedelta(minutes=2),
    ) -> Optional[int]:
        """Take the fleet-wide GPU residency lease, or None if it is held."""
        now = now or _utcnow()
        with self.transaction() as conn:
            return self._acquire(
                conn,
                sch.gpu_lease,
                sch.gpu_lease.c.id == self.GPU_LEASE_ID,
                {"id": self.GPU_LEASE_ID},
                worker_id,
                now,
                ttl,
            )

    def renew_gpu_lease(
        self,
        worker_id: str,
        generation: int,
        now: Optional[datetime] = None,
        ttl: timedelta = timedelta(minutes=2),
    ) -> bool:
        """Extend the GPU lease.  False once the generation has been superseded."""
        now = now or _utcnow()
        with self.transaction() as conn:
            return self._renew(
                conn,
                sch.gpu_lease,
                sch.gpu_lease.c.id == self.GPU_LEASE_ID,
                worker_id,
                generation,
                now,
                ttl,
            )

    def release_gpu_lease(self, worker_id: str, generation: int) -> bool:
        """Give the GPU lease up, keeping the generation."""
        with self.transaction() as conn:
            return self._release(
                conn,
                sch.gpu_lease,
                sch.gpu_lease.c.id == self.GPU_LEASE_ID,
                worker_id,
                generation,
            )

    # The three lease primitives are shared because daemon_lease and gpu_lease
    # differ only in their key column: one lease per user, one lease per fleet.
    @staticmethod
    def _acquire(
        conn: sa.Connection,
        table: sa.Table,
        key: Any,
        key_values: Mapping[str, Any],
        worker_id: str,
        now: datetime,
        ttl: timedelta,
    ) -> Optional[int]:
        row = conn.execute(sa.select(table).where(key)).first()
        expires_at = now + ttl
        if row is None:
            values = _filled(
                table,
                {
                    **key_values,
                    "holder_worker_id": worker_id,
                    "generation": 1,
                    "expires_at": expires_at,
                },
            )
            conn.execute(table.insert(), values)
            return 1
        current = _dict(row)
        if current["expires_at"] is not None and current["expires_at"] > now:
            return None
        generation = int(current["generation"] or 0) + 1
        conn.execute(
            table.update()
            .where(key)
            .values(
                holder_worker_id=worker_id,
                generation=generation,
                expires_at=expires_at,
            )
        )
        return generation

    @staticmethod
    def _renew(
        conn: sa.Connection,
        table: sa.Table,
        key: Any,
        worker_id: str,
        generation: int,
        now: datetime,
        ttl: timedelta,
    ) -> bool:
        result = conn.execute(
            table.update()
            .where(
                key,
                table.c.holder_worker_id == worker_id,
                table.c.generation == generation,
            )
            .values(expires_at=now + ttl)
        )
        return bool(result.rowcount)

    @staticmethod
    def _release(
        conn: sa.Connection,
        table: sa.Table,
        key: Any,
        worker_id: str,
        generation: int,
    ) -> bool:
        # Expiring the row rather than deleting it is what keeps `generation`
        # monotonic across a clean handover.  The expiry is the epoch and not
        # "now": a released lease is free for a successor whose clock the
        # releasing process does not share.
        result = conn.execute(
            table.update()
            .where(
                key,
                table.c.holder_worker_id == worker_id,
                table.c.generation == generation,
            )
            .values(holder_worker_id=None, expires_at=_EPOCH)
        )
        return bool(result.rowcount)

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

    # -- queue operations: the next milestone ------------------------------
    #
    # Declared here so the class satisfies the CouncilStore protocol and so a
    # caller that reaches one gets a named, searchable failure instead of an
    # AttributeError from somewhere deep in the worker loop.

    def claim_calls(
        self,
        user: str,
        worker_id: str,
        endpoints: Set[str],
        limit: int,
        now: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Atomically claim due pending calls for this worker."""
        raise NotImplementedError(_QUEUE_OPS)

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
    ) -> None:
        """Record a claimed call's outcome."""
        raise NotImplementedError(_QUEUE_OPS)

    def release_call(
        self,
        call_id: str,
        claim_token: str,
        not_before: Optional[datetime] = None,
        increment_attempt: bool = True,
    ) -> None:
        """Put a claimed call back on the queue."""
        raise NotImplementedError(_QUEUE_OPS)

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
        """Record one try at a call.  Refuses any argument carrying prompt text."""
        raise NotImplementedError(_QUEUE_OPS)

    def find_reusable(self, prompt_hash: str) -> Optional[Dict[str, Any]]:
        """Return an already-answered call with this prompt hash, from any user."""
        raise NotImplementedError(_QUEUE_OPS)

    def reclaim_stale(
        self, now: Optional[datetime] = None, max_age: timedelta = timedelta(minutes=10)
    ) -> int:
        """Return calls claimed by dead workers to the queue."""
        raise NotImplementedError(_QUEUE_OPS)

    def job_status(self, job_id: str) -> Dict[str, Any]:
        """Progress, pending work and blockers for one job."""
        raise NotImplementedError(_QUEUE_OPS)

    def results(self, job_id: str, include_pending: bool = False) -> List[Dict[str, Any]]:
        """Decided items with their verdicts."""
        raise NotImplementedError(_QUEUE_OPS)

    def cancel_job(self, job_id: str, user: str) -> None:
        """Stop a job: terminal, and it supersedes every pending call."""
        raise NotImplementedError(_QUEUE_OPS)

    def delete_job(self, job_id: str, user: str) -> None:
        """Tombstone a cancelled or finished job; no row is removed."""
        raise NotImplementedError(_QUEUE_OPS)


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
