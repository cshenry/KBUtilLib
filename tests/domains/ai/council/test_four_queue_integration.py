"""Acceptance test for t11 (council-per-seat-queues-v1): the whole design,
end to end, over the roster shape that actually failed in production --
three chair seats (one fast seat on its own account, two seats sharing a
second account) plus a fourth queue serving the critic and meta roles on a
third account.

This module touches no production code.  Every property here is exercised
through the real, shipped :class:`CouncilWorker` -- partitioned by ``seats``/
``roles`` exactly as ``daemon.py``'s per-queue children are, with each
partition driven as its own worker object over ONE shared store -- which is
the same mechanism a real multi-process deployment uses, minus the OS
process boundary itself (already covered end to end, with real spawned
children, by ``test_daemon.py::TestQueueMode``).  What is new here is the
RICHER roster: two queues sharing one account, a combined critic/meta queue,
and all of it running at once.

Nothing here makes a network call, spends a token, needs a CLI login or
needs a GPU: every seat is a ``fake`` endpoint and every answer comes from a
script, exactly as the rest of ``tests/domains/ai/council`` works.

Batching coverage (t11's criterion 5) is exercised at the ``CouncilWorker``
level only, per the task brief: batching is deliberately disabled in
production today (``VERIFIED_OUTPUT_CAPS`` is an intentionally empty table,
and ``daemon.py`` does not yet thread ``batch_size``/
``max_output_tokens_per_item`` from ``QueueConfig`` into the worker -- that
is t13, still unbuilt).  A daemon-level batching assertion cannot pass until
t13 lands; see the work-record.
"""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytest
import sqlalchemy as sa

from kbutillib.domains.ai.council.config import load_council_config
from kbutillib.domains.ai.council.engine import CouncilWorker
from kbutillib.domains.ai.council.engine import worker as worker_module
from kbutillib.domains.ai.council.queries import QueryRegistry
from kbutillib.domains.ai.council.store import schema as sch
from kbutillib.domains.ai.council.types import ConfigError, CouncilVerdict, Roster

from .engine_support import (
    InFlight,
    InstrumentedBackend,
    critic_answer,
    endpoint_config,
    fake_seat,
    make_query,
    make_task,
    meta_answer,
    proposer_answer,
    run,
    scripted,
)

# ---------------------------------------------------------------------------
# The shared roster: three chairs across two accounts, plus a combined
# critic/meta queue on a third -- the shape that starved in production.
# ---------------------------------------------------------------------------

WORKER_DEFAULTS: Dict[str, Any] = {
    "heartbeat_interval_s": 0.0,
    "poll_interval_s": 0.01,
    "retry_backoff_s": (0.02, 0.05),
    "retry_jitter": 0.0,
    "idle_grace_s": 0.2,
    "max_idle_s": 6.0,
}


def _config() -> Dict[str, Any]:
    """The dict-form endpoint mapping for this roster's three accounts.

    ``shared_b`` and ``shared_c`` declare the SAME ``account`` -- that is the
    "two seats sharing a second account" half of the production roster.
    ``fast`` and ``review`` each get their own.
    """
    return endpoint_config(
        fast={"account": "acct-fast", "max_concurrency": 4},
        shared_b={"account": "acct-shared", "max_concurrency": 2},
        shared_c={"account": "acct-shared", "max_concurrency": 2},
        review={"account": "acct-review", "max_concurrency": 4},
    )


def _roster() -> Roster:
    """Three chairs across two accounts, plus a combined critic/meta seat."""
    return Roster(
        proposers=[
            fake_seat("proposer_1", "p1", endpoint="fast"),
            fake_seat("proposer_2", "p2", endpoint="shared_b"),
            fake_seat("proposer_3", "p3", endpoint="shared_c"),
        ],
        critic=fake_seat("critic", "critic-model", endpoint="review"),
        meta=fake_seat("meta", "meta-model", endpoint="review"),
    )


def _worker(
    store: Any, backends: Dict[str, Any], *, config: Any, worker_id: str, **kwargs: Any
) -> CouncilWorker:
    """A partitioned queue worker over *backends*, with test-fast timings."""
    settings = dict(WORKER_DEFAULTS)
    settings.update(kwargs)
    return CouncilWorker(store, config, backends, "alice", worker_id, **settings)


def _submit(store: Any, config: Any, roster: Roster, tasks: List[Any], **kwargs: Any) -> str:
    """Create a job via an unpartitioned, backend-less worker.

    Submission never touches a backend (``_effective_params`` falls back to
    provider-independent parameters when no api type can be resolved), so a
    dedicated admin worker with no backends at all is enough -- and keeps
    "who submitted this" separate from "which queue claims it".

    Registers the query type in the STORE first, not just in the admin
    worker's own in-memory cache: ``_query_for`` falls back to
    ``QueryRegistry(self.store).get(...)`` for any worker object that did
    not submit the job itself, which is every partitioned queue worker in
    this module.  ``put_query_type`` treats an identical re-put as a no-op
    (see ``QueryRegistry.seed_builtins``), so registering the same spec
    again for a second job in the same test is harmless.
    """
    query = make_query()
    QueryRegistry(store).register(query.to_spec())
    admin = CouncilWorker(store, config, {}, "alice", "admin")
    return admin.submit(query, tasks, roster, **kwargs)


def calls_of(
    store: Any,
    job_id: str,
    role: Optional[str] = None,
    seat_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    rows = store.get_calls(job_id)
    return [
        r
        for r in rows
        if (role is None or r["role"] == role) and (seat_name is None or r["seat_name"] == seat_name)
    ]


def attempts_of(store: Any, call_ids: Optional[set] = None) -> List[Dict[str, Any]]:
    with store.engine.begin() as conn:
        rows = [dict(r._mapping) for r in conn.execute(sa.select(sch.attempts))]
    if call_ids is None:
        return rows
    return [r for r in rows if r["call_id"] in call_ids]


def item_of(store: Any, job_id: str, item_id: str) -> Dict[str, Any]:
    return next(r for r in store.get_items(job_id) if r["item_id"] == item_id)


def _run_all(*coros: Any) -> Any:
    """Run several coroutines concurrently to completion, on one loop.

    NOT ``run(asyncio.gather(*coros))``: ``asyncio.gather`` schedules its
    arguments onto whatever loop ``asyncio.get_event_loop()`` returns AT
    CALL TIME, which -- called as a bare argument expression BEFORE
    ``run()`` creates its own loop -- is not the loop that ends up driving
    it, and raises "the future belongs to a different loop".  Building the
    ``gather`` call itself inside the coroutine ``run()`` executes keeps it
    on the one loop that is actually running.
    """

    async def _gather() -> Any:
        return await asyncio.gather(*coros)

    return run(_gather())


# ---------------------------------------------------------------------------
# 1. CONCURRENT PROGRESS, NO ACCOUNT OVER-COMMITMENT, REVIEW GATING,
#    INSTRUMENTATION -- all properties of the SAME run.
# ---------------------------------------------------------------------------


class TestConcurrentProgressAndAccountSafety:
    """The headline (criterion 1), the shared-account safety property
    (criterion 2) and the review-gating regression guard (criterion 3),
    exercised together because they are properties of one run, not three
    independent set-ups.

    Under the base-commit design (a single global claim summing free
    capacity across accounts) this test's headline assertion is exactly the
    failure that was measured in production: 92 calls claimed against ~20
    slots, 66 of them one account's, with a free seat making no progress.
    Here the fast seat (``proposer_1``, its own account, its own queue) and
    the two seats sharing the saturated account (``proposer_2``/
    ``proposer_3``, each its own queue, each capped to its OWN declared
    ``concurrency`` rather than the account's full budget) run concurrently
    against one store.
    """

    def test_fast_seat_progresses_while_the_shared_account_is_saturated(
        self, store: Any
    ) -> None:
        config = _config()
        roster = _roster()

        fast_monitor = InFlight()
        shared_monitor = InFlight()
        backend_fast = InstrumentedBackend(
            scripted({"proposer": proposer_answer("A:1")}), monitor=fast_monitor
        )
        # A real, nonzero delay: the only way two overlapping calls on the
        # shared account can ever show up together in InFlight.peak at all.
        backend_b = InstrumentedBackend(
            scripted({"proposer": proposer_answer("A:1")}), delay=0.1, monitor=shared_monitor
        )
        backend_c = InstrumentedBackend(
            scripted({"proposer": proposer_answer("A:1")}), delay=0.1, monitor=shared_monitor
        )
        backend_review = InstrumentedBackend(
            scripted({"critic": critic_answer(), "meta": meta_answer("A:1")})
        )

        n_items = 12
        tasks = [make_task(f"item-{i}", text=f"thing {i}") for i in range(n_items)]
        job_id = _submit(store, config, roster, tasks, label="headline")

        w_fast = _worker(
            store, {"fast": backend_fast}, config=config, worker_id="q-fast",
            seats={"proposer_1"}, concurrency=2,
        )
        # Each shared-account queue is its OWN worker/process with its own
        # declared concurrency (1) -- never the account's full budget (2).
        # The two together sum to exactly the account's max_concurrency,
        # which is what config-load validation would require of a real
        # deployment (see TestSharedAccountConcurrencyIsValidatedStatically
        # below).
        w_b = _worker(
            store, {"shared_b": backend_b}, config=config, worker_id="q-shared-b",
            seats={"proposer_2"}, concurrency=1,
        )
        w_c = _worker(
            store, {"shared_c": backend_c}, config=config, worker_id="q-shared-c",
            seats={"proposer_3"}, concurrency=1,
        )
        w_review = _worker(
            store, {"review": backend_review}, config=config, worker_id="q-review",
            roles={"critic", "meta"}, concurrency=2,
        )

        violations: List[str] = []

        async def _watch_review_gate(stop_event: asyncio.Event) -> None:
            """Continuously assert criterion 3 while the run is in flight.

            A single end-of-run snapshot could miss a transient violation; a
            call that appears and is then overwritten between two checks
            would read as clean.  Sampling every poll interval is cheap
            (small job, in-memory sqlite) and catches it either way.
            """
            while not stop_event.is_set():
                by_item: Dict[str, Dict[Tuple[str, str], Dict[str, Any]]] = {}
                for call in store.get_calls(job_id):
                    by_item.setdefault(call["item_id"], {})[
                        (call["role"], call["seat_name"])
                    ] = call
                for item_id, rows in by_item.items():
                    critic_row = rows.get(("critic", "critic"))
                    if critic_row is None:
                        continue
                    proposer_rows = [v for (role, _seat), v in rows.items() if role == "proposer"]
                    if len(proposer_rows) < 3 or any(
                        r["status"] not in ("ok", "error") for r in proposer_rows
                    ):
                        violations.append(
                            f"item {item_id}: a critic call exists before all 3 "
                            "of its chairs settled"
                        )
                await asyncio.sleep(0.01)

        async def _drive() -> None:
            stop_event = asyncio.Event()

            async def _deadline() -> None:
                await asyncio.sleep(0.5)
                stop_event.set()

            await asyncio.gather(
                w_fast.work(stop_event=stop_event, job_id=job_id),
                w_b.work(stop_event=stop_event, job_id=job_id),
                w_c.work(stop_event=stop_event, job_id=job_id),
                w_review.work(stop_event=stop_event, job_id=job_id),
                _watch_review_gate(stop_event),
                _deadline(),
            )

        run(_drive())

        assert not violations, violations

        fast_calls = calls_of(store, job_id, seat_name="proposer_1")
        assert len(fast_calls) == n_items
        assert all(c["status"] == "ok" for c in fast_calls), (
            "the fast seat's account is free the whole time; it must have "
            "finished every item inside the 0.5s window"
        )

        shared_calls = calls_of(store, job_id, seat_name="proposer_2") + calls_of(
            store, job_id, seat_name="proposer_3"
        )
        outstanding_shared = [c for c in shared_calls if c["status"] in ("pending", "claimed")]
        assert outstanding_shared, (
            "the shared account should still have work outstanding at the "
            "deadline -- if this fails, the test's own timing margin is too "
            "tight, not the fix (same caution as TestStarvationRegression "
            "in test_engine.py)"
        )

        # THE SAFETY PROPERTY (criterion 2): the two queues sharing
        # acct-shared, each capped at its own declared concurrency (1),
        # never together put more than 2 calls in flight -- the account's
        # real cap.  If either queue instead claimed against the account's
        # FULL max_concurrency (2) rather than its own share, this peak
        # could reach 4.
        assert shared_monitor.peak <= 2, (
            f"acct-shared's two queues (concurrency 1 each) must never put "
            f"more than 2 calls in flight at once; saw {shared_monitor.peak}"
        )
        assert shared_monitor.peak >= 1, "the two shared-account queues never overlapped at all"
        assert fast_monitor.peak <= 2

        # Instrumentation: queued_at/started_at are both written, and a call
        # that had to wait for the single per-queue slot started strictly
        # after it was queued (mirrors TestServiceTimeInstrumentation).
        shared_call_ids = {c["call_id"] for c in shared_calls}
        shared_attempts = attempts_of(store, shared_call_ids)
        assert shared_attempts
        for row in shared_attempts:
            assert row["queued_at"] is not None
            assert row["started_at"] is not None
            assert row["started_at"] >= row["queued_at"]
        waited = [a for a in shared_attempts if a["started_at"] > a["queued_at"]]
        assert waited, (
            "with concurrency 1 and 12 items per shared-account queue, at "
            "least one call must have waited for the slot its predecessor held"
        )


# ---------------------------------------------------------------------------
# 2. NON-UNANIMOUS ITEM REACHES A VERDICT, WITH CALLS ATTRIBUTED PER QUEUE.
# ---------------------------------------------------------------------------


class TestNonUnanimousVerdictAttribution:
    """Criterion 4: chair -> critic -> meta -> verdict, over all four queues,
    with every call's ``endpoint``/``claimed_by`` naming the queue that ran
    it.
    """

    def test_a_split_vote_runs_the_meta_round_with_calls_attributed_to_their_queue(
        self, store: Any
    ) -> None:
        config = _config()
        roster = _roster()

        # proposer_1 and proposer_2 pick {A:1, B:2}; proposer_3 picks only
        # A:1 -- a 3-2 split, exactly TestStarvationRegression's sibling
        # `split_script()` shape, but routed through this roster's named
        # queues instead of one worker.
        backend_fast = InstrumentedBackend(scripted({"proposer": proposer_answer("A:1", "B:2")}))
        backend_b = InstrumentedBackend(scripted({"proposer": proposer_answer("A:1", "B:2")}))
        backend_c = InstrumentedBackend(scripted({"proposer": proposer_answer("A:1")}))
        backend_review = InstrumentedBackend(
            scripted({"critic": critic_answer(), "meta": meta_answer("A:1")})
        )

        job_id = _submit(store, config, roster, [make_task()], label="split-vote")

        w_fast = _worker(
            store, {"fast": backend_fast}, config=config, worker_id="q-fast",
            seats={"proposer_1"}, concurrency=2,
        )
        w_b = _worker(
            store, {"shared_b": backend_b}, config=config, worker_id="q-shared-b",
            seats={"proposer_2"}, concurrency=1,
        )
        w_c = _worker(
            store, {"shared_c": backend_c}, config=config, worker_id="q-shared-c",
            seats={"proposer_3"}, concurrency=1,
        )
        w_review = _worker(
            store, {"review": backend_review}, config=config, worker_id="q-review",
            roles={"critic", "meta"}, concurrency=2,
        )

        _run_all(
            w_fast.work(job_id=job_id, until_idle=True),
            w_b.work(job_id=job_id, until_idle=True),
            w_c.work(job_id=job_id, until_idle=True),
            w_review.work(job_id=job_id, until_idle=True),
        )

        assert store.get_job(job_id)["status"] == "done"
        item = item_of(store, job_id, "item-1")
        assert item["status"] == "ok"
        verdict = CouncilVerdict.model_validate(item["verdict"])
        assert verdict.decided_by == "meta"
        assert verdict.votes == {"A:1": 3, "B:2": 2}
        assert verdict.selected_ids == ["A:1"]

        expected_queue = {
            "proposer_1": ("fast", "q-fast"),
            "proposer_2": ("shared_b", "q-shared-b"),
            "proposer_3": ("shared_c", "q-shared-c"),
            "critic": ("review", "q-review"),
            "meta": ("review", "q-review"),
        }
        calls = store.get_calls(job_id)
        assert len(calls) == 5
        for call in calls:
            endpoint, worker_id = expected_queue[call["seat_name"]]
            assert call["status"] == "ok"
            assert call["endpoint"] == endpoint
            assert call["claimed_by"] == worker_id, (
                f"{call['seat_name']} call was attributed to {call['claimed_by']!r}, "
                f"expected the {worker_id!r} queue"
            )


# ---------------------------------------------------------------------------
# 3. BATCHING AT THE WORKER LEVEL, OTHER QUEUES STILL PROGRESSING.
# ---------------------------------------------------------------------------

#: Matches the "### Item N (item_key: <call_id>)\n<rendered user>" block
#: `render_batch_user` wraps each batched member in; every query-type
#: template this module uses (see engine_support.py's `_CRITIC_USER`) starts
#: its body with "Annotation: ...", which is what the second group anchors
#: on.  Mirrors the identical pattern in test_batched_invocation.py.
_BLOCK_RE = re.compile(r"item_key:\s*([^)]+)\)\s*\nAnnotation:\s*(\S+)")


def _batched_item_keys(user_text: str) -> List[str]:
    return [key for key, _item_id in _BLOCK_RE.findall(user_text or "")]


def _review_script(req: Any) -> Dict[str, Any]:
    """Critic approves every item; one reply per invocation, batched or not.

    For a batched request (one whose rendered ``user`` carries the
    ``item_key`` markers `render_batch_user` writes), the reply must be the
    wrapped shape ``{"answers": [{"item_key": ..., **critic_answer()}, ...]}``
    -- see ``wrap_batch_answer_schema`` and `test_batched_invocation.py`'s
    identical ``BatchScript``.  A plain, unkeyed ``critic_answer()`` would
    satisfy neither role's schema for a batched member.
    """
    if req.role == "meta":
        return meta_answer("A:1")
    keys = _batched_item_keys(req.user or "")
    if not keys:
        return critic_answer()
    return {"answers": [{"item_key": key, **critic_answer()} for key in keys]}


def _grant_cap(monkeypatch: Any, seat: Any, cap: int) -> None:
    """Make *seat* a VERIFIED (endpoint_kind, model) combination for this
    test only -- see ``VERIFIED_OUTPUT_CAPS``'s own docstring: it is empty in
    production because no CLI backend forwards an output-token argument
    today, so nothing here can batch without this.
    """
    monkeypatch.setitem(worker_module.VERIFIED_OUTPUT_CAPS, (seat.endpoint_kind, seat.model), cap)


class TestBatchedReviewQueueProgressesAlongsideChairs:
    """Criterion 5, reframed exactly as the task brief requires: batching is
    disabled end to end in production right now (``VERIFIED_OUTPUT_CAPS`` is
    empty by design, and ``daemon.py`` does not thread ``batch_size`` into
    the worker yet -- that is t13).  So this exercises batching at the
    ``CouncilWorker`` level directly: ``VERIFIED_OUTPUT_CAPS`` monkeypatched,
    ``batch_size`` passed straight into the review queue's worker
    constructor, never through the daemon.

    Two phases, deliberately NOT one continuous run: phase 1 settles job 1's
    three chairs (fast, unbatched) so all five of its critic rows exist
    BEFORE the review queue ever claims anything -- otherwise the review
    queue's own poll could race ahead of the chairs and claim item 0's
    critic row alone, long before items 1-4 exist, which would produce
    several small invocations instead of the one this test is about. Phase 2
    then runs the review queue's single batched invocation (made
    deliberately slow) CONCURRENTLY with the three chair queues working a
    SECOND job, and proves the chairs are not blocked behind it.
    """

    def test_one_batched_invocation_settles_several_critics_while_chairs_keep_moving(
        self, store: Any, monkeypatch: Any
    ) -> None:
        config = _config()
        base_roster = _roster()
        # A generous per-seat output ceiling: fake_seat's own default (256)
        # leaves room for only a couple of items at a realistic per-item
        # estimate, far fewer than this test needs to batch.
        big_critic = base_roster.critic.model_copy(update={"max_output_tokens": 20_000})
        big_meta = base_roster.meta.model_copy(update={"max_output_tokens": 20_000})
        roster = base_roster.model_copy(update={"critic": big_critic, "meta": big_meta})
        _grant_cap(monkeypatch, big_critic, 1_000_000)
        _grant_cap(monkeypatch, big_meta, 1_000_000)

        backend_fast = InstrumentedBackend(scripted({"proposer": proposer_answer("A:1")}))
        backend_b = InstrumentedBackend(scripted({"proposer": proposer_answer("A:1")}))
        backend_c = InstrumentedBackend(scripted({"proposer": proposer_answer("A:1")}))
        review_monitor = InFlight()
        backend_review = InstrumentedBackend(_review_script, delay=0.2, monitor=review_monitor)

        job1_tasks = [make_task(f"job1-{i}", text=f"job1 thing {i}") for i in range(5)]
        job1 = _submit(store, config, roster, job1_tasks, label="batched-job")

        w_fast = _worker(
            store, {"fast": backend_fast}, config=config, worker_id="q-fast",
            seats={"proposer_1"}, concurrency=2,
        )
        w_b = _worker(
            store, {"shared_b": backend_b}, config=config, worker_id="q-shared-b",
            seats={"proposer_2"}, concurrency=1,
        )
        w_c = _worker(
            store, {"shared_c": backend_c}, config=config, worker_id="q-shared-c",
            seats={"proposer_3"}, concurrency=1,
        )

        # Phase 1: settle job 1's chairs only.  No review worker exists yet.
        _run_all(
            w_fast.work(job_id=job1, until_idle=True),
            w_b.work(job_id=job1, until_idle=True),
            w_c.work(job_id=job1, until_idle=True),
        )
        job1_critics = calls_of(store, job1, role="critic")
        assert len(job1_critics) == 5
        assert all(c["status"] == "pending" for c in job1_critics), (
            "all five of job1's critic rows must exist, and be untouched, "
            "before the review queue ever runs -- see the class docstring"
        )

        # Phase 2: a second job gives the chair queues real, ongoing work;
        # a small delay on the shared-account backends keeps that work
        # genuinely in flight rather than finishing before the batched
        # invocation even starts.
        backend_b.delay = 0.03
        backend_c.delay = 0.03
        job2_tasks = [make_task(f"job2-{i}", text=f"job2 thing {i}") for i in range(3)]
        job2 = _submit(store, config, roster, job2_tasks, label="trailing-job")

        w_review = _worker(
            store, {"review": backend_review}, config=config, worker_id="q-review",
            roles={"critic", "meta"}, concurrency=2, batch_size=10,
            max_output_tokens_per_item=50,
        )

        progressed_during_batch = {"seen": False}

        async def _watch() -> None:
            for _ in range(2000):
                if review_monitor.current >= 1:
                    job2_proposers = calls_of(store, job2, role="proposer")
                    if any(c["status"] == "ok" for c in job2_proposers):
                        progressed_during_batch["seen"] = True
                        return
                await asyncio.sleep(0.005)

        _run_all(
            w_fast.work(job_id=job2, until_idle=True),
            w_b.work(job_id=job2, until_idle=True),
            w_c.work(job_id=job2, until_idle=True),
            w_review.work(job_id=job1, until_idle=True),
            _watch(),
        )

        assert store.get_job(job1)["status"] == "done"

        critic_requests = [r for r in backend_review.calls if r.role == "critic"]
        job1_critics = calls_of(store, job1, role="critic")
        assert len(job1_critics) == 5
        assert all(c["status"] == "ok" for c in job1_critics)
        assert len(critic_requests) == 1, (
            "one invocation must have settled all five of job1's critic rows "
            f"-- saw {len(critic_requests)} separate critic requests"
        )
        assert len(_batched_item_keys(critic_requests[0].user or "")) == 5

        assert progressed_during_batch["seen"], (
            "the chair queues must make real progress on job2's items WHILE "
            "the single batched critic invocation for job1 is still in "
            "flight -- otherwise the review queue's batching is blocking "
            "the others, which is exactly what per-queue partitioning is "
            "supposed to prevent"
        )


# ---------------------------------------------------------------------------
# 4. RESTART SAFETY: a killed queue's claim returns to pending, no retry
#    spent, and a replacement worker finishes the job.
# ---------------------------------------------------------------------------


class TestRestartSafety:
    """Criterion 6.

    "Killed mid-run" is simulated at the point that matters -- a claim with
    nothing yet attempted against it -- by calling the worker's own
    ``_claim`` directly (the same sanctioned technique
    ``TestAssignedNeverExceedsCapacity`` in test_engine.py uses) and then
    simply abandoning that worker object: no ``_run_group``, no release, no
    further heartbeat.  That is exactly what a ``SIGKILL`` leaves behind in
    the store -- a claimed row and nothing else -- without risking this
    process's own exception handling running where a real kill would have
    allowed none.  ``store.reclaim_stale`` is then called with an explicit
    short ``max_age`` to stand in for the real wall-clock wait production
    takes before a claim is considered abandoned; every call site in
    ``daemon.py``/``worker.py`` reaches the SAME method, just with the
    default 10-minute window, which no test can afford to wait out for real.
    """

    def test_a_killed_queues_claim_returns_to_pending_and_a_replacement_finishes_the_job(
        self, store: Any
    ) -> None:
        config = _config()
        roster = _roster()

        backend_fast = InstrumentedBackend(scripted({"proposer": proposer_answer("A:1")}))
        backend_b = InstrumentedBackend(scripted({"proposer": proposer_answer("A:1")}))
        backend_c = InstrumentedBackend(scripted({"proposer": proposer_answer("A:1")}))
        backend_review = InstrumentedBackend(
            scripted({"critic": critic_answer(), "meta": meta_answer("A:1")})
        )

        tasks = [make_task(f"item-{i}", text=f"thing {i}") for i in range(3)]
        job_id = _submit(store, config, roster, tasks, label="restart-safety")

        # The queue that is about to "die": claims exactly its own
        # concurrency budget (1) and is abandoned before anything is ever
        # attempted against the claim.
        w_b_dead = _worker(
            store, {"shared_b": backend_b}, config=config, worker_id="q-shared-b-doomed",
            seats={"proposer_2"}, concurrency=1,
        )
        summary: Dict[str, Any] = {
            "claimed": 0, "finished": 0, "released": 0, "blockers": [], "idle": False,
        }
        free = w_b_dead._free_slots()
        assert free == 1
        claimed_rows = w_b_dead._claim(free, job_id, summary)
        assert len(claimed_rows) == 1
        stuck_call_id = claimed_rows[0]["call_id"]

        stuck = calls_of(store, job_id, seat_name="proposer_2")
        statuses = [c["status"] for c in stuck]
        assert statuses.count("claimed") == 1
        assert statuses.count("pending") == 2
        stuck_row = next(c for c in stuck if c["call_id"] == stuck_call_id)
        assert stuck_row["attempt_count"] == 0
        assert stuck_row["claim_token"] is not None
        assert attempts_of(store, {stuck_call_id}) == [], (
            "nothing was ever attempted for the doomed claim -- it died "
            "between claiming and running, exactly like a SIGKILL would leave it"
        )

        # The OTHER queues are not blocked by q-shared-b being dead: they
        # finish their own seats' work for every item regardless.
        w_fast = _worker(
            store, {"fast": backend_fast}, config=config, worker_id="q-fast",
            seats={"proposer_1"}, concurrency=2,
        )
        w_c = _worker(
            store, {"shared_c": backend_c}, config=config, worker_id="q-shared-c",
            seats={"proposer_3"}, concurrency=1,
        )
        _run_all(
            w_fast.work(job_id=job_id, until_idle=True),
            w_c.work(job_id=job_id, until_idle=True),
        )
        assert all(c["status"] == "ok" for c in calls_of(store, job_id, seat_name="proposer_1"))
        assert all(c["status"] == "ok" for c in calls_of(store, job_id, seat_name="proposer_3"))
        assert store.get_job(job_id)["status"] != "done", (
            "the job cannot be done yet -- proposer_2's items are still "
            "stuck behind the dead queue"
        )

        # Time passes (simulated, not waited for): the claim is reclaimed.
        reclaimed = store.reclaim_stale(max_age=timedelta(seconds=0))
        assert reclaimed >= 1
        after_reclaim = calls_of(store, job_id, seat_name="proposer_2")
        assert all(c["status"] == "pending" for c in after_reclaim)
        assert all(c["claim_token"] is None for c in after_reclaim)
        assert all(c["attempt_count"] == 0 for c in after_reclaim), (
            "a reclaimed claim must not cost the call a retry"
        )

        # A REPLACEMENT worker -- a fresh object, a fresh worker_id -- picks
        # the reclaimed call (and its never-claimed siblings) up and
        # finishes the job.
        w_b_replacement = _worker(
            store, {"shared_b": backend_b}, config=config, worker_id="q-shared-b-replacement",
            seats={"proposer_2"}, concurrency=1,
        )
        w_review = _worker(
            store, {"review": backend_review}, config=config, worker_id="q-review",
            roles={"critic", "meta"}, concurrency=2,
        )
        _run_all(
            w_b_replacement.work(job_id=job_id, until_idle=True),
            w_review.work(job_id=job_id, until_idle=True),
        )

        final = calls_of(store, job_id, seat_name="proposer_2")
        assert all(c["status"] == "ok" for c in final)
        settled_stuck = next(c for c in final if c["call_id"] == stuck_call_id)
        assert settled_stuck["claimed_by"] == "q-shared-b-replacement"
        # `attempt_count` only ever counts RETRIES (releases after a
        # failure); a call that succeeds on the one real try it ever got
        # never increments it -- so staying at 0 here is exactly the point:
        # the crash cost nothing, and neither did the one real attempt that
        # finally ran.
        assert settled_stuck["attempt_count"] == 0
        assert store.get_job(job_id)["status"] == "done"


# ---------------------------------------------------------------------------
# 5. The static half of criterion 2: config-load validation of the exact
#    shared-account shape this module runs, through the real CouncilConfig.
# ---------------------------------------------------------------------------


def _loadable_config(*, q_shared_b: int, q_shared_c: int) -> Dict[str, Any]:
    """A real, four-queue ``CouncilConfig`` source over this module's roster.

    Unlike :func:`_config` (the plain dict form ``CouncilWorker`` consumes
    directly), this goes through ``load_council_config``'s full validation,
    including ``_validate_queues``'s account-oversubscription check -- the
    STARTUP-TIME half of criterion 2, complementing the runtime InFlight
    proof in ``TestConcurrentProgressAndAccountSafety``.
    """

    def endpoint(name: str, account: str) -> Dict[str, Any]:
        return {
            "kind": "fake",
            "reuse_namespace": f"fake:{name}",
            "account": account,
            "max_concurrency": 2,
        }

    return {
        "endpoints": {
            "fast": endpoint("fast", "acct-fast"),
            "shared_b": endpoint("shared_b", "acct-shared"),
            "shared_c": endpoint("shared_c", "acct-shared"),
            "review": endpoint("review", "acct-review"),
        },
        "models": {
            "p1": {"endpoint": "fast", "canonical": "test/p1"},
            "p2": {"endpoint": "shared_b", "canonical": "test/p2"},
            "p3": {"endpoint": "shared_c", "canonical": "test/p3"},
            "cr": {"endpoint": "review", "canonical": "test/cr"},
            "mt": {"endpoint": "review", "canonical": "test/mt"},
        },
        "profiles": {
            "test": {"proposers": ["p1", "p2", "p3"], "critic": "cr", "meta": "mt"},
        },
        "default_profile": "test",
        "queues": {
            "q1": {"seats": ["proposer_1"], "concurrency": 2},
            "q2": {"seats": ["proposer_2"], "concurrency": q_shared_b},
            "q3": {"seats": ["proposer_3"], "concurrency": q_shared_c},
            "q4": {"seats": ["critic", "meta"], "concurrency": 2},
        },
    }


class TestSharedAccountConcurrencyIsValidatedStatically:
    """``load_council_config`` refuses a kind no installed api type answers
    to, so these two tests register ``fake`` for the duration of the test
    only -- the same registry-save/restore shape ``test_daemon.py`` uses for
    its own test api type, never left mutated for a later test.
    """

    @pytest.fixture(autouse=True)
    def _registered_fake_kind(self) -> Any:
        from kbutillib.domains.ai import llm

        saved_registry = dict(llm._REGISTRY)
        saved_cache = dict(llm._CACHE)
        llm.register_api_type("fake", lambda endpoint: object(), replace=True)
        try:
            yield
        finally:
            llm._REGISTRY.clear()
            llm._REGISTRY.update(saved_registry)
            llm._CACHE.clear()
            llm._CACHE.update(saved_cache)

    def test_a_config_whose_queue_sum_matches_the_shared_accounts_cap_loads(self) -> None:
        cfg = load_council_config(_loadable_config(q_shared_b=1, q_shared_c=1))
        assert cfg.queues["q2"].concurrency + cfg.queues["q3"].concurrency == 2

    def test_a_config_that_oversubscribes_the_shared_account_is_refused_by_name(self) -> None:
        with pytest.raises(ConfigError) as caught:
            load_council_config(_loadable_config(q_shared_b=1, q_shared_c=2))
        message = str(caught.value)
        assert "acct-shared" in message
        assert "q2" in message and "q3" in message
