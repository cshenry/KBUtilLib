"""Batched invocation (t8): one backend call answering several items.

A dedicated module, deliberately separate from ``test_engine.py`` (per the
task prompt): everything here is specific to ``CouncilWorker``'s batching
seam -- ``_group_claimed`` / ``_run_group`` / ``_invoke_batch`` -- and to the
``_batch_n`` derived-ceiling arithmetic that decides whether batching runs at
all.

No test makes a network call, spends a token, needs a CLI login or needs a
GPU: every seat is a ``fake`` endpoint and every answer comes from a script
driven by :class:`BatchScript`, which understands both the ``item_key``
numbered-list request shape a real batch sends and the plain single-call
shape bisection bottoms out to -- the same script can drive either without
the test knowing which one a given row will take.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import sqlalchemy as sa

from kbutillib.domains.ai.council.engine import worker as worker_module
from kbutillib.domains.ai.council.engine.worker import CouncilWorker
from kbutillib.domains.ai.council.store import schema as sch
from kbutillib.domains.ai.council.types import Roster, SeatSpec, TransportError

from .engine_support import (
    InstrumentedBackend,
    critic_answer,
    endpoint_config,
    fake_seat,
    make_query,
    make_roster,
    make_task,
    meta_answer,
    proposer_answer,
    run,
)

# ---------------------------------------------------------------------------
# A FakeBackend script that understands BOTH request shapes
# ---------------------------------------------------------------------------

#: Matches "(item_key: <key>)\nAnnotation: <item_id> " in the batched user
#: prompt this test's query type renders -- see ``make_query()`` in
#: ``engine_support.py``, whose proposer/critic/meta templates all start
#: their body with "Annotation: $text", and every task here sets
#: ``text == item_id`` so the two can be told apart without a second marker.
_BLOCK_RE = re.compile(r"item_key:\s*([^)]+)\)\s*\nAnnotation:\s*(\S+)")


def _batched_pairs(user_text: str) -> List[Tuple[str, str]]:
    """``[(item_key, item_id), ...]`` from a batched user prompt, in order."""
    return _BLOCK_RE.findall(user_text or "")


WORKER_DEFAULTS: Dict[str, Any] = {
    "heartbeat_interval_s": 0.0,
    "poll_interval_s": 0.01,
    "retry_backoff_s": (0.02, 0.05),
    "retry_jitter": 0.0,
    "idle_grace_s": 0.3,
    "max_idle_s": 15.0,
}


def worker_for(store: Any, backends: Mapping[str, Any], **kwargs: Any) -> CouncilWorker:
    """A worker over *backends*, with test-fast timings (mirrors test_engine.py)."""
    settings = dict(WORKER_DEFAULTS)
    settings.update(kwargs)
    config = settings.pop("config", None)
    user = settings.pop("user", "alice")
    worker_id = settings.pop("worker_id", "w-1")
    if config is None:
        config = endpoint_config(**{name: {} for name in backends})
    return CouncilWorker(store, config, backends, user, worker_id, **settings)


#: A generous per-seat output ceiling for tests that need real multi-item
#: batching to engage.  ``fake_seat``'s own default (256) times 0.75 leaves
#: room for only 2-3 items at a realistic ``max_output_tokens_per_item``, far
#: fewer than several of these tests submit; this is a seat-level override,
#: not a change to the default a test that does not ask for it ever sees.
BIG_MAX_OUTPUT_TOKENS = 20_000


def _big_seat(seat_name: str, model: str, endpoint: str) -> SeatSpec:
    """``fake_seat``, with its output ceiling overridden upward.

    ``fake_seat`` hard-codes ``max_output_tokens=256`` ahead of its own
    ``**kwargs``, so it cannot be overridden by passing the same keyword --
    rebuilding via ``model_copy`` is the straightforward way round that
    without touching ``engine_support.py``.
    """
    return fake_seat(seat_name, model, endpoint=endpoint).model_copy(
        update={"max_output_tokens": BIG_MAX_OUTPUT_TOKENS}
    )


def big_roster(n_proposers: int = 3, **kwargs: Any) -> Roster:
    """Like ``make_roster``, but every seat's output ceiling is generous."""
    endpoint = kwargs.pop("endpoint", "fake1")
    return Roster(
        proposers=[
            _big_seat(f"proposer_{i}", f"p{i}", endpoint) for i in range(1, n_proposers + 1)
        ],
        critic=_big_seat("critic", "critic-model", endpoint),
        meta=_big_seat("meta", "meta-model", endpoint),
        **kwargs,
    )


def grant_cap(monkeypatch: Any, seat: SeatSpec, cap: int) -> None:
    """Make *seat* a VERIFIED CLI/model combination for this test only.

    ``VERIFIED_OUTPUT_CAPS`` is empty in production (see its docstring in
    ``worker.py``): no CLI backend forwards an output-token argument today.
    Tests that need batching to actually engage grant one seat a cap through
    ``monkeypatch.setitem``, which undoes itself at test teardown -- the
    table is never left mutated for another test.
    """
    monkeypatch.setitem(
        worker_module.VERIFIED_OUTPUT_CAPS, (seat.endpoint_kind, seat.model), cap
    )


class BatchScript:
    """A script for :class:`~...backends.testing.FakeBackend` that answers
    both a batched (``item_key``-wrapped) request and a plain single-call
    request -- the shape bisection bottoms out to.

    Args:
        pick: the menu id every proposer/meta answer picks.
        poison_item_ids: item ids that make ANY invocation containing them
            fail (transport error), batched or solo -- isolating one via
            bisection is what the bisection tests exercise.
        fail_if_members_at_least: fail any BATCHED invocation with at least
            this many members, whatever items it carries (used to drive
            repeated whole-invocation failures down to singles without
            naming a specific item).
        fail_usage: usage to attach to a FAILED result (a provider can report
            spend on a call it still failed).
        missing_item_ids / invalid_item_ids: items whose answer is omitted /
            schema-invalid in a batched reply.  A SOLO call for one of these
            items also fails (an invalid payload, "omit" having no solo
            equivalent): a row that can never be answered must stay that way
            once isolated, whether by bisection or because its batch-mates
            settled first.
        duplicate_first: repeat the first answer in a batched reply under
            its own ``item_key`` (first must win).
        unknown_item_key: an extra answer element keyed by a value that
            names no call row (must be dropped).
        poison_seat: when given, poisoning/failure only applies to this
            ``seat_name`` -- other seats answer normally regardless.
    """

    def __init__(
        self,
        *,
        pick: str = "A:1",
        poison_item_ids: Sequence[str] = (),
        fail_if_members_at_least: int = 0,
        fail_usage: Optional[Dict[str, Any]] = None,
        missing_item_ids: Sequence[str] = (),
        invalid_item_ids: Sequence[str] = (),
        duplicate_first: bool = False,
        unknown_item_key: Optional[str] = None,
        poison_seat: Optional[str] = None,
    ) -> None:
        self.pick = pick
        self.poison_item_ids = set(poison_item_ids)
        self.fail_if_members_at_least = fail_if_members_at_least
        self.fail_usage = fail_usage
        self.missing_item_ids = set(missing_item_ids)
        self.invalid_item_ids = set(invalid_item_ids)
        self.duplicate_first = duplicate_first
        self.unknown_item_key = unknown_item_key
        self.poison_seat = poison_seat
        #: Set by the most recent failing call, for the test to read the
        #: usage a FAILED result reported (FakeBackend's own error path
        #: carries no usage at all -- see ``FailingWithUsage`` below).
        self.last_failure: Optional[TransportError] = None

    def _answer_for(self, role: str) -> Dict[str, Any]:
        if role == "proposer":
            return proposer_answer(self.pick)
        if role == "critic":
            return critic_answer()
        if role == "meta":
            return meta_answer(self.pick)
        raise AssertionError(f"unexpected role {role!r}")

    def __call__(self, req: Any) -> Any:
        pairs = _batched_pairs(req.user or "")
        if not pairs:
            # A solo, unwrapped call -- bisection's leaf shape.  trace_id is
            # "<item_id>|<role>|<seat_name>" for a single call (see
            # ``_invoke``); item_key == item_id here, there being only one.
            item_id = (req.trace_id or "").split("|")[0]
            pairs = [(item_id, item_id)]
            batched = False
        else:
            batched = True

        applies = self.poison_seat is None or req.seat.seat_name == self.poison_seat
        item_ids = {item_id for _key, item_id in pairs}
        if applies and (item_ids & self.poison_item_ids):
            raise TransportError("poison item present in this invocation")
        if applies and batched and len(pairs) >= max(1, self.fail_if_members_at_least) and self.fail_if_members_at_least:
            raise TransportError("simulated whole-invocation transport failure")

        if not batched:
            item_id = pairs[0][1]
            if item_id in self.missing_item_ids or item_id in self.invalid_item_ids:
                # Solo has no notion of "omitted from the reply" -- the
                # closest honest equivalent is an answer that cannot satisfy
                # ANY role's schema, so a row that can never be answered
                # stays that way even once isolated alone (bisection, or a
                # bucket that shrank to one because its batch-mates settled).
                return {"picked": "not-a-list"}
            return self._answer_for(req.role)

        answers: List[Dict[str, Any]] = []
        for key, item_id in pairs:
            if item_id in self.missing_item_ids:
                continue
            if item_id in self.invalid_item_ids:
                answers.append({"item_key": key, "picked": "not-a-list"})
                continue
            answers.append({"item_key": key, **self._answer_for(req.role)})
        if self.duplicate_first and answers:
            wrong = dict(answers[0])
            wrong.update(self._answer_for(req.role))
            if req.role == "proposer":
                wrong["picked"] = [{"id": "B:2", "confidence": "high", "why": "wrong"}]
            answers.append(wrong)
        if self.unknown_item_key:
            answers.append({"item_key": self.unknown_item_key, **self._answer_for(req.role)})
        return {"answers": answers}


class FailingWithUsage(InstrumentedBackend):
    """Wraps a :class:`BatchScript` so a FAILED result still reports usage.

    ``FakeBackend``'s own error path never attaches usage (a script raising
    is a pure transport failure in its view), but a real CLI can charge for
    an invocation it ultimately failed, which is exactly what confront round
    1 point (f) requires the engine to record rather than discard.
    """

    def __init__(self, script: BatchScript, usage: Dict[str, Any], **kwargs: Any) -> None:
        super().__init__(script, **kwargs)
        self._usage = dict(usage)

    async def complete(self, req: Any) -> Any:
        result = await super().complete(req)
        if not result.ok:
            return result.model_copy(update={"usage": dict(self._usage)})
        return result


def fake(script: BatchScript, **kwargs: Any) -> InstrumentedBackend:
    return InstrumentedBackend(script, **kwargs)


def tasks_for(*item_ids: str) -> List[Any]:
    """One task per id, whose evidence TEXT is the id -- see ``_BLOCK_RE``."""
    return [make_task(item_id, text=item_id) for item_id in item_ids]


def calls_of(store: Any, job_id: str, role: Optional[str] = None, seat_name: Optional[str] = None) -> list:
    rows = store.get_calls(job_id)
    return [
        r
        for r in rows
        if (role is None or r["role"] == role)
        and (seat_name is None or r["seat_name"] == seat_name)
    ]


def attempts_of(store: Any) -> List[Dict[str, Any]]:
    with store.engine.begin() as conn:
        return [dict(r._mapping) for r in conn.execute(sa.select(sch.attempts))]


# ---------------------------------------------------------------------------
# Derived ceiling (pure, no event loop)
# ---------------------------------------------------------------------------


class TestDerivedCeiling:
    def test_no_verified_cap_forces_singles(self, store: Any, caplog: Any) -> None:
        worker = worker_for(store, {"fake1": fake(BatchScript())}, batch_size=5)
        seat = make_roster(3).proposers[0]
        with caplog.at_level(logging.WARNING):
            n = worker._batch_n(seat, "proposer", rows_in_hand=5)
        assert n == 1
        assert "no verified" in caplog.text.lower()

    def test_unknown_per_item_output_forces_singles(
        self, store: Any, monkeypatch: Any, caplog: Any
    ) -> None:
        worker = worker_for(store, {"fake1": fake(BatchScript())}, batch_size=5)
        seat = make_roster(3).proposers[0]
        grant_cap(monkeypatch, seat, 100_000)
        with caplog.at_level(logging.WARNING):
            n = worker._batch_n(seat, "proposer", rows_in_hand=5)
        assert n == 1
        assert "per-item output" in caplog.text.lower()

    def test_ceiling_below_batch_size_reduces_and_logs_both_numbers(
        self, store: Any, monkeypatch: Any, caplog: Any
    ) -> None:
        worker = worker_for(
            store, {"fake1": fake(BatchScript())}, batch_size=20, max_output_tokens_per_item=1000
        )
        seat = _big_seat("proposer_1", "p1", "fake1")
        grant_cap(monkeypatch, seat, 20000)
        with caplog.at_level(logging.WARNING):
            n = worker._batch_n(seat, "proposer", rows_in_hand=20)
        # effective_output_cap=20000, per_item=1000 -> derived_ceiling=15
        assert n == 15
        assert "derived_ceiling=15" in caplog.text
        assert "batch_size=20" in caplog.text

    def test_ceiling_below_one_refuses(self, store: Any, monkeypatch: Any, caplog: Any) -> None:
        worker = worker_for(
            store,
            {"fake1": fake(BatchScript())},
            batch_size=10,
            max_output_tokens_per_item=1_000_000,
        )
        seat = _big_seat("proposer_1", "p1", "fake1")
        grant_cap(monkeypatch, seat, 20000)
        with caplog.at_level(logging.WARNING):
            n = worker._batch_n(seat, "proposer", rows_in_hand=10)
        assert n == 1
        assert "refus" in caplog.text.lower()

    def test_fewer_rows_in_hand_than_batch_size_is_simply_the_rows_in_hand(
        self, store: Any, monkeypatch: Any
    ) -> None:
        worker = worker_for(
            store, {"fake1": fake(BatchScript())}, batch_size=20, max_output_tokens_per_item=10
        )
        seat = _big_seat("proposer_1", "p1", "fake1")
        grant_cap(monkeypatch, seat, 20000)
        assert worker._batch_n(seat, "proposer", rows_in_hand=3) == 3

    def test_logged_only_once_per_seat_role_per_run(self, store: Any, caplog: Any) -> None:
        worker = worker_for(store, {"fake1": fake(BatchScript())}, batch_size=5)
        seat = make_roster(3).proposers[0]
        with caplog.at_level(logging.WARNING):
            worker._batch_n(seat, "proposer", rows_in_hand=5)
            worker._batch_n(seat, "proposer", rows_in_hand=5)
            worker._batch_n(seat, "proposer", rows_in_hand=5)
        assert caplog.text.count("no VERIFIED effective") == 1


# ---------------------------------------------------------------------------
# Grouping / batch_size=1 base-commit reproduction
# ---------------------------------------------------------------------------


class TestGroupClaimed:
    def test_batch_size_1_groups_one_row_each(self, store: Any) -> None:
        worker = worker_for(store, {"fake1": fake(BatchScript())})  # batch_size default 1
        assert worker.batch_size == 1
        roster = make_roster(3)
        job_id = worker.submit(make_query(), tasks_for("item-1", "item-2"), roster)
        rows = calls_of(store, job_id, role="proposer")
        groups = worker._group_claimed(rows)
        assert groups == [[row] for row in rows]

    def test_homogeneous_rows_group_together_up_to_n(
        self, store: Any, monkeypatch: Any
    ) -> None:
        worker = worker_for(
            store, {"fake1": fake(BatchScript())}, batch_size=3, max_output_tokens_per_item=10
        )
        roster = make_roster(3)
        for _role, seat in roster.seats():
            grant_cap(monkeypatch, seat, 1_000_000)
        job_id = worker.submit(
            make_query(), tasks_for("item-1", "item-2", "item-3", "item-4", "item-5"), roster
        )
        rows = calls_of(store, job_id, role="proposer", seat_name="proposer_1")
        assert len(rows) == 5
        groups = worker._group_claimed(rows)
        sizes = sorted(len(g) for g in groups)
        assert sizes == [2, 3]  # batch_size=3 caps each group; 5 = 3 + 2


# ---------------------------------------------------------------------------
# 1:N settlement from one batched reply
# ---------------------------------------------------------------------------


class TestBatchedSettlement:
    def _worker(self, store: Any, script: BatchScript, monkeypatch: Any, **kwargs: Any) -> Any:
        backend = fake(script)
        worker = worker_for(store, {"fake1": backend}, batch_size=20, **kwargs)
        roster = big_roster(3)
        for _role, seat in roster.seats():
            grant_cap(monkeypatch, seat, 1_000_000)
        return worker, roster, backend

    def test_n_rows_settle_from_one_reply(self, store: Any, monkeypatch: Any) -> None:
        worker, roster, backend = self._worker(
            store, BatchScript(), monkeypatch, max_output_tokens_per_item=50
        )
        items = [f"item-{i}" for i in range(6)]
        job_id = worker.submit(make_query(), tasks_for(*items), roster)
        run(worker.work(job_id=job_id, until_idle=True))

        proposer_1_rows = calls_of(store, job_id, role="proposer", seat_name="proposer_1")
        assert len(proposer_1_rows) == 6
        assert all(row["status"] == "ok" for row in proposer_1_rows)
        # One invocation answered all six -- not six separate calls.
        proposer_1_reqs = [r for r in backend.calls if r.seat.seat_name == "proposer_1"]
        assert len(proposer_1_reqs) == 1
        for item_id in items:
            assert store.get_job(job_id)["status"] in ("running", "done")
        assert store.get_job(job_id)["status"] == "done"

    def test_missing_k_item_keys_settles_n_minus_k_and_releases_k(
        self, store: Any, monkeypatch: Any
    ) -> None:
        script = BatchScript(missing_item_ids={"item-1", "item-2"})
        worker, roster, backend = self._worker(
            store,
            script,
            monkeypatch,
            max_output_tokens_per_item=50,
            # A backoff far longer than this test's runtime: ``work()``
            # loops until NOTHING is claimable right now even without
            # ``until_idle``, so without a long backoff the released rows'
            # own retry can be reclaimed before this call returns, which
            # would charge a second attempt and defeat the "exactly one
            # release" assertion below.
            retry_backoff_s=(1000.0, 1000.0),
        )
        items = [f"item-{i}" for i in range(5)]
        job_id = worker.submit(make_query(), tasks_for(*items), roster)
        run(worker.work(job_id=job_id, until_idle=True))

        rows = {r["item_id"]: r for r in calls_of(store, job_id, role="proposer", seat_name="proposer_1")}
        for item_id in ("item-0", "item-3", "item-4"):
            assert rows[item_id]["status"] == "ok"
        for item_id in ("item-1", "item-2"):
            assert rows[item_id]["status"] == "pending"
            assert rows[item_id]["attempt_count"] == 1

    def test_unknown_item_key_is_dropped_and_nothing_is_written_for_it(
        self, store: Any, monkeypatch: Any
    ) -> None:
        script = BatchScript(unknown_item_key="not-a-real-call-id")
        worker, roster, backend = self._worker(
            store, script, monkeypatch, max_output_tokens_per_item=50
        )
        job_id = worker.submit(make_query(), tasks_for("item-0", "item-1"), roster)
        run(worker.work(job_id=job_id, until_idle=True))
        rows = calls_of(store, job_id, role="proposer", seat_name="proposer_1")
        assert all(row["status"] == "ok" for row in rows)
        # No row anywhere carries the bogus key -- there is nothing it COULD
        # be written to, which is exactly the point.
        assert all(row["call_id"] != "not-a-real-call-id" for row in rows)

    def test_duplicate_item_key_takes_the_first(self, store: Any, monkeypatch: Any) -> None:
        script = BatchScript(duplicate_first=True)
        worker, roster, backend = self._worker(
            store, script, monkeypatch, max_output_tokens_per_item=50
        )
        job_id = worker.submit(make_query(), tasks_for("item-0", "item-1"), roster)
        run(worker.work(job_id=job_id, until_idle=True))
        rows = {r["item_id"]: r for r in calls_of(store, job_id, role="proposer", seat_name="proposer_1")}
        first = rows["item-0"]
        assert first["status"] == "ok"
        assert first["reply_parsed"]["picked"][0]["id"] == "A:1", (
            "the FIRST scripted answer (A:1) must win over the duplicate (B:2)"
        )

    def test_keyed_but_invalid_payload_releases_only_that_row(
        self, store: Any, monkeypatch: Any
    ) -> None:
        script = BatchScript(invalid_item_ids={"item-1"})
        worker, roster, backend = self._worker(
            store,
            script,
            monkeypatch,
            max_output_tokens_per_item=50,
            retry_backoff_s=(1000.0, 1000.0),
        )
        job_id = worker.submit(make_query(), tasks_for("item-0", "item-1", "item-2"), roster)
        run(worker.work(job_id=job_id, until_idle=True))

        rows = {r["item_id"]: r for r in calls_of(store, job_id, role="proposer", seat_name="proposer_1")}
        assert rows["item-0"]["status"] == "ok"
        assert rows["item-2"]["status"] == "ok"
        assert rows["item-1"]["status"] == "pending"
        assert rows["item-1"]["attempt_count"] == 1

    def test_missing_and_invalid_both_exhaust_at_max_attempts_and_settle_error(
        self, store: Any, monkeypatch: Any
    ) -> None:
        script = BatchScript(missing_item_ids={"item-0"}, invalid_item_ids={"item-1"})
        worker, roster, backend = self._worker(
            store, script, monkeypatch, max_output_tokens_per_item=50, max_attempts=2
        )
        job_id = worker.submit(make_query(), tasks_for("item-0", "item-1"), roster)
        run(worker.work(job_id=job_id, until_idle=True))

        rows = {r["item_id"]: r for r in calls_of(store, job_id, role="proposer", seat_name="proposer_1")}
        assert rows["item-0"]["status"] == "error"
        assert rows["item-0"]["error"]["kind"] == "missing"
        assert rows["item-1"]["status"] == "error"
        assert rows["item-1"]["error"]["kind"] == "invalid_output"
        # One release, then a terminal settle -- the same shape max_attempts=2
        # produces for the single-call path (see test_engine.py).
        assert rows["item-0"]["attempt_count"] == 1
        assert rows["item-1"]["attempt_count"] == 1


# ---------------------------------------------------------------------------
# Usage attribution and the account budget
# ---------------------------------------------------------------------------


class TestUsageAttribution:
    def test_usage_is_recorded_once_with_batch_size_and_member_ids(
        self, store: Any, monkeypatch: Any
    ) -> None:
        script = BatchScript()
        backend = InstrumentedBackend(script, usage={"prompt_tokens": 40, "completion_tokens": 120})
        worker = worker_for(store, {"fake1": backend}, batch_size=20, max_output_tokens_per_item=50)
        roster = big_roster(3)
        for _role, seat in roster.seats():
            grant_cap(monkeypatch, seat, 1_000_000)
        job_id = worker.submit(make_query(), tasks_for("item-0", "item-1", "item-2", "item-3"), roster)
        run(worker.work(job_id=job_id, until_idle=True))

        rows = calls_of(store, job_id, role="proposer", seat_name="proposer_1")
        call_ids = {r["call_id"] for r in rows}
        attempts = [a for a in attempts_of(store) if a["call_id"] in call_ids]
        with_invocation = [a for a in attempts if isinstance(a["usage"], dict) and "invocation_id" in a["usage"]]
        assert len(with_invocation) == 4, "every member's attempt references the invocation"
        invocation_ids = {a["usage"]["invocation_id"] for a in with_invocation}
        assert len(invocation_ids) == 1
        assert all(a["usage"]["batch_size"] == 4 for a in with_invocation)
        assert all(sorted(a["usage"]["member_call_ids"]) == sorted(call_ids) for a in with_invocation)
        carrying_real_usage = [a for a in with_invocation if a["usage"].get("completion_tokens") is not None]
        assert len(carrying_real_usage) == 1, "exactly one anchor attempt carries the real usage"
        assert carrying_real_usage[0]["usage"]["completion_tokens"] == 120

    def test_a_failed_invocations_usage_is_recorded_too(self, store: Any, monkeypatch: Any) -> None:
        script = BatchScript(fail_if_members_at_least=2)
        backend = FailingWithUsage(
            script, usage={"prompt_tokens": 30, "completion_tokens": 60}
        )
        worker = worker_for(
            store, {"fake1": backend}, batch_size=3, max_output_tokens_per_item=50, max_attempts=5
        )
        roster = big_roster(3)
        for _role, seat in roster.seats():
            grant_cap(monkeypatch, seat, 1_000_000)
        job_id = worker.submit(make_query(), tasks_for("item-0", "item-1", "item-2"), roster)
        run(worker.work(job_id=job_id, until_idle=True))

        attempts = attempts_of(store)
        failed_with_usage = [
            a
            for a in attempts
            if isinstance(a["usage"], dict)
            and a["usage"].get("completion_tokens") == 60
            and a["outcome_kind"] == "transport"
        ]
        assert failed_with_usage, "a failed invocation's spend must still be recorded"
        # Grouped by invocation_id: each invocation's member rows all agree
        # on batch_size and member_call_ids, and exactly one of them (the
        # anchor) carries the real number.
        by_invocation: Dict[str, List[Dict[str, Any]]] = {}
        for a in attempts:
            usage = a["usage"]
            if isinstance(usage, dict) and "invocation_id" in usage and a["attempt_no"] < 0:
                by_invocation.setdefault(usage["invocation_id"], []).append(usage)
        assert by_invocation, "no uncharged ancestor-failure attempt was recorded"
        for usages in by_invocation.values():
            batch_size = usages[0]["batch_size"]
            assert len(usages) == batch_size
            anchors = [u for u in usages if u.get("completion_tokens") is not None]
            assert len(anchors) == 1
            assert anchors[0]["completion_tokens"] == 60

    def test_assigned_moves_by_one_per_batch_not_n(self, store: Any, monkeypatch: Any) -> None:
        backend = fake(BatchScript())
        config = endpoint_config(fake1={"account": "acct", "max_concurrency": 1})
        # Partitioned to ONE seat: the job also has proposer_2/proposer_3
        # rows due at the same priority/order, and WITHOUT this the claim's
        # row LIMIT would return an unordered MIX across seats (ties go to a
        # random call_id), making which one seat's group survives the
        # account's single slot non-deterministic.  Scoping to proposer_1
        # makes the five rows this test cares about the only ones claimable
        # at all.
        worker = worker_for(
            store,
            {"fake1": backend},
            batch_size=10,
            max_output_tokens_per_item=50,
            config=config,
            seats={"proposer_1"},
        )
        roster = big_roster(3, endpoint="fake1")
        for _role, seat in roster.seats():
            grant_cap(monkeypatch, seat, 1_000_000)
        job_id = worker.submit(
            make_query(), tasks_for(*(f"item-{i}" for i in range(5))), roster
        )

        limit = worker._limits["acct"]
        summary: Dict[str, Any] = {"claimed": 0, "finished": 0, "released": 0, "blockers": [], "idle": False}
        free = worker._free_slots()
        kept = worker._claim(free, job_id, summary)

        proposer_1_rows = [r for r in kept if r["seat_name"] == "proposer_1"]
        assert len(proposer_1_rows) == 5, "all five rows for one seat claimed in one round"
        assert limit.assigned == 1, "one batch is one in-flight unit, not five"


# ---------------------------------------------------------------------------
# Bisection on a whole-invocation failure
# ---------------------------------------------------------------------------


class TestBisection:
    def test_a_poison_item_is_isolated_while_its_batch_mates_settle(
        self, store: Any, monkeypatch: Any
    ) -> None:
        script = BatchScript(poison_item_ids={"poison"}, poison_seat="proposer_1")
        worker = worker_for(
            store,
            {"fake1": fake(script)},
            batch_size=10,
            max_output_tokens_per_item=50,
            max_attempts=2,
        )
        roster = big_roster(3)
        for _role, seat in roster.seats():
            grant_cap(monkeypatch, seat, 1_000_000)
        items = ["item-1", "item-2", "poison", "item-4"]
        job_id = worker.submit(make_query(), tasks_for(*items), roster)
        run(worker.work(job_id=job_id, until_idle=True))

        rows = {r["item_id"]: r for r in calls_of(store, job_id, role="proposer", seat_name="proposer_1")}
        for item_id in ("item-1", "item-2", "item-4"):
            assert rows[item_id]["status"] == "ok"
            assert rows[item_id]["attempt_count"] == 0, (
                "a batch-mate must never be charged for the poison item's "
                "ancestor failures"
            )
        assert rows["poison"]["status"] == "error"
        assert rows["poison"]["error"]["kind"] == "transport"
        assert rows["poison"]["attempt_count"] == 1, (
            "the poison item is charged its OWN attempts once isolated alone, "
            "under the ordinary max_attempts=2 gate (one release, one terminal "
            "failure)"
        )
        # The ancestor (batch-level) failures were audited without charging
        # any attempt_count -- negative, uncharged attempt rows exist for the
        # poison item beyond its one charged singleton attempt.
        poison_attempts = [a for a in attempts_of(store) if a["call_id"] == rows["poison"]["call_id"]]
        uncharged = [a for a in poison_attempts if a["attempt_no"] < 0]
        assert uncharged, "ancestor bisection failures must be audited, uncharged"


# ---------------------------------------------------------------------------
# The running mean
# ---------------------------------------------------------------------------


class TestRunningMean:
    def test_the_mean_is_total_completion_tokens_over_total_requested_members(
        self, store: Any
    ) -> None:
        """The arithmetic itself, asserted directly (not merely "it runs")."""
        worker = worker_for(store, {"fake1": fake(BatchScript())})
        seat = fake_seat("proposer_1", "p1")
        worker._record_output_sample(seat.seat_name, "proposer", seat.model, 4, 80)
        worker._record_output_sample(seat.seat_name, "proposer", seat.model, 6, 120)
        assert worker._per_item_output(seat, "proposer") == 20.0  # (80 + 120) / (4 + 6)

    def test_failed_invocations_do_not_feed_the_running_mean(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """Drive a REAL failing invocation and prove it never touches the mean.

        ``max_output_tokens_per_item`` is configured so ``_batch_n`` never
        has to consult the mean for the CEILING decision in this test
        (keeping the scenario about the mean's own bookkeeping, not about
        whether batching engages) -- the mean's observation table is
        inspected directly, which a configured estimate does not mask.
        """
        seat_key = ("proposer_1", "proposer", "p1")
        worker = worker_for(
            store, {"fake1": fake(BatchScript())}, batch_size=10, max_output_tokens_per_item=1000
        )
        roster = big_roster(3)
        for _role, seat in roster.seats():
            grant_cap(monkeypatch, seat, 1_000_000)

        worker._record_output_sample(seat_key[0], seat_key[1], seat_key[2], 4, 80)
        baseline = worker._output_samples[seat_key]
        assert baseline == (80, 4)

        # Every invocation with >= 2 members fails and reports a LARGE usage
        # figure that must never be folded in: including it would DRAG the
        # mean down (a batch "costs" less per item on paper) and ENLARGE the
        # next ceiling -- a feedback loop pointed straight at the truncation
        # this heuristic exists to avoid.
        failing_backend = FailingWithUsage(
            BatchScript(fail_if_members_at_least=2),
            usage={"prompt_tokens": 1, "completion_tokens": 999},
        )
        worker.backends["fake1"] = failing_backend
        job_id = worker.submit(make_query(), tasks_for("bad-1", "bad-2", "bad-3"), roster)
        run(worker.work(job_id=job_id, until_idle=True))

        assert worker._output_samples[seat_key] == baseline, (
            "a failed invocation's usage must never be folded into the "
            "running mean"
        )


# ---------------------------------------------------------------------------
# Reuse stays per item
# ---------------------------------------------------------------------------


class TestReuseAcrossBatching:
    def test_an_item_answered_inside_a_batch_is_reusable_from_a_different_job(
        self, store: Any, monkeypatch: Any
    ) -> None:
        script = BatchScript()
        backend = fake(script)
        worker = worker_for(store, {"fake1": backend}, batch_size=10, max_output_tokens_per_item=50)
        roster = big_roster(3)
        for _role, seat in roster.seats():
            grant_cap(monkeypatch, seat, 1_000_000)

        first_job = worker.submit(
            make_query(), tasks_for("item-0", "item-1"), roster, label="first"
        )
        run(worker.work(job_id=first_job, until_idle=True))
        asked = len(backend.calls)
        assert asked > 0

        # A SECOND job asking an IDENTICAL item (same evidence, same menu,
        # same roster) must reuse the first job's answer rather than asking
        # the model again -- the property batching must not break, because
        # prompt_hash is computed per item, never from the batch.
        second_job = worker.submit(
            make_query(), tasks_for("item-0"), roster, label="second"
        )
        run(worker.work(job_id=second_job, until_idle=True))

        assert len(backend.calls) == asked, "the second job asked a model something"
        rows = calls_of(store, second_job, role="proposer", seat_name="proposer_1")
        assert len(rows) == 1
        assert rows[0]["status"] == "ok"
        assert rows[0]["reused_from"]


# ---------------------------------------------------------------------------
# batch_size=1 reproduces base-commit behaviour
# ---------------------------------------------------------------------------


class TestBatchSizeOneUnchanged:
    def test_explicit_batch_size_1_behaves_like_the_default(
        self, store: Any, monkeypatch: Any
    ) -> None:
        script = BatchScript()
        backend = fake(script)
        worker = worker_for(store, {"fake1": backend}, batch_size=1)
        roster = make_roster(3)
        # Even granting a cap must not matter: batch_size <= 1 short-circuits
        # _group_claimed before _batch_n is ever consulted.
        for _role, seat in roster.seats():
            grant_cap(monkeypatch, seat, 1_000_000)
        job_id = worker.submit(make_query(), tasks_for("item-0", "item-1", "item-2"), roster)
        run(worker.work(job_id=job_id, until_idle=True))

        rows = calls_of(store, job_id, role="proposer", seat_name="proposer_1")
        assert len(rows) == 3
        assert all(row["status"] == "ok" for row in rows)
        # One request PER ITEM, not one for all three -- the unbatched shape.
        proposer_1_reqs = [r for r in backend.calls if r.seat.seat_name == "proposer_1"]
        assert len(proposer_1_reqs) == 3
        assert all("item_key" not in (r.user or "") for r in proposer_1_reqs)
        assert store.get_job(job_id)["status"] == "done"
