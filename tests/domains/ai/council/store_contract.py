"""Reusable store-contract fixtures.

The council has ONE store, a single-user SQLite file on local disk (review
round 2 removed the shared PostgreSQL store, so there is no second
implementation to keep in step and no second parametrisation).  The contract
lives here once because several test modules need the same fixtures, not
because it runs against several backends.

Each test gets a FRESH database in its own ``tmp_path`` directory.  Tests may
not assume anything the previous test left behind.

Nothing here makes a network call, spends a token, needs a CLI login or needs
a GPU.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List

import pytest


@pytest.fixture(params=[pytest.param("sqlite", id="sqlite")])
def store_backend(request: pytest.FixtureRequest) -> str:
    """The name of the store implementation under test.

    One value.  Kept as a parametrised fixture so the test ids still read
    ``[sqlite]`` and a future second implementation has somewhere to go.
    """
    return request.param


@pytest.fixture
def store_url(store_backend: str, tmp_path: Any) -> Iterator[Callable[..., str]]:
    """A callable ``(name="council") -> url`` for the store under test.

    The same *name* always yields the same database, so a test can close a
    store and reopen the identical one; a different *name* yields a separate,
    equally fresh database.
    """

    def _sqlite_url(name: str = "council") -> str:
        return f"sqlite:///{tmp_path}/{name}/council.sqlite"

    yield _sqlite_url


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


def claim_in_subprocess(url, worker_id, endpoints, limit, barrier, results):
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
                rows = store.claim_calls(worker_id, set(endpoints), limit)
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
