"""Reusable store-contract fixtures, parametrised over every implementation.

The council has two stores -- a local SQLite file and a shared PostgreSQL
database -- and they must behave identically, because a call row written by
one is read by the other.  Testing them against two separate suites would let
them drift, so the contract lives here once and every test that imports
:func:`make_store` runs against both.

Which implementations run:

* **sqlite** -- always, in ``tmp_path``.
* **postgresql** -- only when ``KBU_COUNCIL_TEST_PG_DSN`` names a reachable
  database, and only once :mod:`...store.postgres` exists.  Skipped with a
  stated reason otherwise, so a run on a laptop with no PostgreSQL is a skip
  rather than a failure, and nobody has to remember a flag.

Each test gets a FRESH database: a new ``tmp_path`` directory for SQLite, a
new PostgreSQL schema (dropped afterwards) for PostgreSQL.  Tests may not
assume anything the previous test left behind.

Nothing here makes a network call, spends a token, needs a CLI login or needs
a GPU -- except the PostgreSQL branch, which talks to the database the
operator pointed it at and is off by default.
"""

from __future__ import annotations

import importlib.util
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional
from urllib.parse import quote

import pytest

#: Operator sets this to a libpq DSN / SQLAlchemy URL to switch PostgreSQL on.
PG_DSN_ENV = "KBU_COUNCIL_TEST_PG_DSN"

def pg_dsn() -> str:
    """The configured PostgreSQL DSN, or an empty string."""
    return (os.environ.get(PG_DSN_ENV) or "").strip()


def pg_skip_reason() -> Optional[str]:
    """Why the PostgreSQL parametrisation cannot run, or None if it can."""
    if not pg_dsn():
        return f"{PG_DSN_ENV} is not set"
    if importlib.util.find_spec("psycopg") is None:
        return "psycopg 3 is not installed (pip install 'kbutillib[council]')"
    # PostgresStore is a later milestone.  Checking for the module rather than
    # letting the import blow up keeps this a stated skip until it lands, and
    # turns itself off the moment it does.
    if importlib.util.find_spec("kbutillib.domains.ai.council.store.postgres") is None:
        return "kbutillib.domains.ai.council.store.postgres is not implemented yet"
    return None


def _backend_params() -> List[Any]:
    params = [pytest.param("sqlite", id="sqlite")]
    reason = pg_skip_reason()
    params.append(
        pytest.param(
            "postgresql",
            id="postgresql",
            marks=pytest.mark.skipif(reason is not None, reason=reason or ""),
        )
    )
    return params


@pytest.fixture(params=_backend_params())
def store_backend(request: pytest.FixtureRequest) -> str:
    """The name of the store implementation under test."""
    return request.param


@pytest.fixture
def store_url(store_backend: str, tmp_path: Any) -> Iterator[Callable[..., str]]:
    """A callable ``(name="council") -> url`` for the backend under test.

    The same *name* always yields the same database, so a test can close a
    store and reopen the identical one; a different *name* yields a separate,
    equally fresh database.
    """
    if store_backend == "sqlite":

        def _sqlite_url(name: str = "council") -> str:
            return f"sqlite:///{tmp_path}/{name}/council.sqlite"

        # A yield in either branch makes this a generator fixture, so the
        # SQLite branch must yield too rather than return.
        yield _sqlite_url
        return

    dsn = pg_dsn()
    prefix = f"kbu_council_test_{uuid.uuid4().hex[:12]}"
    created: Dict[str, str] = {}

    import sqlalchemy as sa

    admin = sa.create_engine(dsn, future=True)

    def _pg_url(name: str = "council") -> str:
        schema = created.get(name)
        if schema is None:
            schema = f"{prefix}_{name}"
            with admin.begin() as conn:
                conn.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            created[name] = schema
        separator = "&" if "?" in dsn else "?"
        # libpq's `options` is the one way to pin a search_path that works
        # regardless of what keyword arguments PostgresStore happens to take.
        return f"{dsn}{separator}options={quote('-csearch_path=' + schema)}"

    yield _pg_url

    with admin.begin() as conn:
        for schema in created.values():
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    admin.dispose()


@pytest.fixture
def make_store(store_url: Callable[..., str]) -> Iterator[Callable[..., Any]]:
    """A callable ``(name="council", *, init=True, **kwargs) -> CouncilStore``.

    Every store it hands out is closed when the test ends, so a test can open
    the same database twice -- which is exactly how "a second store object
    reads the same schema version" is checked -- without leaking file handles
    or connections.
    """
    from kbutillib.domains.ai.council.store import open_store

    opened: List[Any] = []

    def _make(name: str = "council", *, init: bool = True, **kwargs: Any) -> Any:
        store = open_store(store_url(name), **kwargs)
        opened.append(store)
        if init:
            store.init_schema()
        return store

    yield _make

    for store in opened:
        close = getattr(store, "close", None)
        if callable(close):
            close()


@pytest.fixture
def store(make_store: Callable[..., Any]) -> Any:
    """One initialised, empty store -- the common case."""
    return make_store()


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------
#
# Deliberately plain dicts keyed by column name: that is the store's own
# interface, and building rows through a model here would test the model
# rather than the store.


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def job_row(**overrides: Any) -> Dict[str, Any]:
    """A minimally valid job row."""
    row: Dict[str, Any] = {
        "label": "test job",
        "query_name": "go_term_mapping",
        "query_version": "3",
        "submitted_by": "alice",
        "submitted_host": "testhost",
        "roster": {"proposers": ["proposer_1", "proposer_2"], "critic": "critic"},
        "roster_fingerprint": "f" * 64,
        "profile_name": "argo",
        "options": {"max_concurrency": 2},
    }
    row.update(overrides)
    return row


def item_row(item_id: str = "item-1", **overrides: Any) -> Dict[str, Any]:
    """A minimally valid item row."""
    row: Dict[str, Any] = {
        "item_id": item_id,
        "entity_hash": "a" * 64,
        "data_hash": "b" * 64,
        "evidence": {"text": "a hypothetical protein", "source": "test"},
        "menu": [{"id": "GO:0003674", "label": "molecular_function"}],
    }
    row.update(overrides)
    return row


def call_row(
    item_id: str = "item-1",
    seat_name: str = "proposer_1",
    role: str = "proposer",
    **overrides: Any,
) -> Dict[str, Any]:
    """A minimally valid pending call row (no job_id: create_job supplies it)."""
    row: Dict[str, Any] = {
        "item_id": item_id,
        "role": role,
        "seat_name": seat_name,
        "endpoint": "argo",
        "endpoint_kind": "argo",
        "served_model": "gpt5.1",
        "canonical_model": "openai/gpt-5.1",
        "route_fingerprint": "c" * 64,
        "entity_hash": "a" * 64,
        "data_hash": "b" * 64,
        "prompt_hash": "d" * 64,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Concurrency harness
# ---------------------------------------------------------------------------


def claim_in_subprocess(url, user, worker_id, endpoints, limit, barrier, results):
    """Drain the queue from a store THIS process opened, and report what it got.

    The entry point of the no-double-claim test's child processes.  It lives
    in this module rather than in the test module because
    ``multiprocessing``'s ``spawn`` start method pickles the target by
    qualified name and re-imports it in a brand new interpreter -- which is
    the point of the test: the children share nothing with the parent but the
    database file's URL, so the locking they rely on is the store's own and
    not some artefact of a shared Python object.

    Nothing is monkeypatched, no store object crosses the process boundary,
    and no lock of this harness's own serialises the claims.  The barrier only
    lines the children up so that they contend; it is released before the
    first claim.

    The outcome goes back as ``(worker_id, [call_id, ...], error_or_None)``.
    A failure is REPORTED rather than raised, because a child that dies
    silently leaves its sibling blocked on the barrier and the parent reading
    a hang instead of a reason.
    """
    claimed = []
    error = None
    try:
        from kbutillib.domains.ai.council.store import open_store

        store = open_store(url)
        try:
            if barrier is not None:
                barrier.wait(timeout=120)
            while True:
                rows = store.claim_calls(user, worker_id, set(endpoints), limit)
                if not rows:
                    # Empty means drained, not "busy": a claim that collides
                    # with the other process WAITS for the write lock
                    # (busy_timeout) rather than coming back empty.
                    break
                claimed.extend(str(row["call_id"]) for row in rows)
                # Yield the writer lock between batches.  Without this one
                # child wins the lock back so consistently that it drains
                # nearly the whole queue alone, and the select-then-stamp
                # window -- the only place a double claim could happen -- is
                # contended once instead of on every batch.
                time.sleep(0.002)
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
    except BaseException as exc:  # noqa: BLE001 - reported, see docstring
        error = f"{type(exc).__name__}: {exc}"
    results.put((worker_id, claimed, error))
