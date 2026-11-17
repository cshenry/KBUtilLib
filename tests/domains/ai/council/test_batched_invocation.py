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

import json
import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import sqlalchemy as sa

from kbutillib.domains.ai.council.engine import worker as worker_module
from kbutillib.domains.ai.council.engine.worker import CouncilWorker
from kbutillib.domains.ai.council.store import schema as sch
from kbutillib.domains.ai.council.types import (
    Roster,
    SchemaIncompatible,
    SeatSpec,
    TransportError,
)

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
        # NOTE on why item-0 asserts a set membership rather than a fixed
        # label: ``BatchScript.__call__`` (above) has no solo equivalent of
        # "omitted from a batched reply" -- a retry that gets isolated to a
        # solo call answers an item in ``missing_item_ids`` with the same
        # schema-invalid payload it uses for ``invalid_item_ids`` (see the
        # comment at lines 215-224). Whether a given retry lands solo or
        # stays batched is a timing accident of
        # ``_group_claimed``/``_split_bucket`` under this fixture's
        # ``max_concurrency``, not something this test controls or should
        # care about. So "item-0" (missing) can legitimately settle as
        # either "missing" (if its retry was regrouped into a batch) or
        # "invalid_output" (if it was retried solo) -- both are a faithful
        # record of what actually happened on that attempt, and the engine
        # is correct either way.
        #
        # "item-1" has NO such race: it is only in ``invalid_item_ids``, so
        # every path (batched or solo) always hands it a schema-invalid
        # payload and it is never omitted -- ``__call__`` above has no
        # branch that drops an ``invalid_item_ids`` member from a batched
        # reply. So item-1's kind stays pinned to "invalid_output" exactly;
        # relaxing it to the same two-member set as item-0 would also
        # accept a real defect (e.g. a batched-reply item_key mix-up that
        # swapped which of the two rows got dropped) without the test ever
        # noticing.
        script = BatchScript(missing_item_ids={"item-0"}, invalid_item_ids={"item-1"})
        worker, roster, backend = self._worker(
            store, script, monkeypatch, max_output_tokens_per_item=50, max_attempts=2
        )
        job_id = worker.submit(make_query(), tasks_for("item-0", "item-1"), roster)
        run(worker.work(job_id=job_id, until_idle=True))

        rows = {r["item_id"]: r for r in calls_of(store, job_id, role="proposer", seat_name="proposer_1")}
        assert rows["item-0"]["status"] == "error"
        assert rows["item-0"]["error"]["kind"] in {"missing", "invalid_output"}
        assert rows["item-1"]["status"] == "error"
        assert rows["item-1"]["error"]["kind"] == "invalid_output"
        # One release, then a terminal settle -- the same shape max_attempts=2
        # produces for the single-call path (see test_engine.py).  Charging
        # exactly one attempt (not zero) is what proves each item's retry
        # was actually driven through the backend rather than the row
        # settling without ever being retried.
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

    def test_the_anchor_keeps_the_cli_cache_breakdown(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """Issue #92 item 3: batching must not throw the cache split away.

        A CLI seat's usage carries two extra INFORMATIONAL keys --
        ``cache_creation_prompt_tokens`` and ``cache_read_prompt_tokens`` --
        which are a breakdown of ``prompt_tokens`` and are already inside it.
        For a subscription seat that breakdown IS the cost story: on a real
        critic call ~62,000 billed input tokens were ~38,000 cache_creation
        plus ~24,000 cache_read.

        They go on the ANCHOR attempt only, like the real counts: a batch has
        exactly one invocation and one bill, and copying the numbers onto
        every member invites a reader to sum them and multiply the true cost
        by ``len(members)``.
        """
        usage = {
            "prompt_tokens": 62_000,
            "completion_tokens": 4_752,
            "cache_creation_prompt_tokens": 38_000,
            "cache_read_prompt_tokens": 23_998,
        }
        script = BatchScript()
        backend = InstrumentedBackend(script, usage=dict(usage))
        worker = worker_for(
            store, {"fake1": backend}, batch_size=20, max_output_tokens_per_item=50
        )
        roster = big_roster(3)
        for _role, seat in roster.seats():
            grant_cap(monkeypatch, seat, 10_000_000)
        job_id = worker.submit(
            make_query(), tasks_for("item-0", "item-1", "item-2", "item-3"), roster
        )
        run(worker.work(job_id=job_id, until_idle=True))

        rows = calls_of(store, job_id, role="proposer", seat_name="proposer_1")
        call_ids = {r["call_id"] for r in rows}
        members = [
            a["usage"]
            for a in attempts_of(store)
            if a["call_id"] in call_ids
            and isinstance(a["usage"], dict)
            and "invocation_id" in a["usage"]
        ]
        assert len(members) == 4, "every member's attempt references the invocation"

        anchors = [u for u in members if u.get("completion_tokens") is not None]
        assert len(anchors) == 1, "exactly one anchor attempt carries real usage"
        anchor = anchors[0]
        assert anchor["prompt_tokens"] == 62_000
        assert anchor["cache_creation_prompt_tokens"] == 38_000
        assert anchor["cache_read_prompt_tokens"] == 23_998
        # Still a SUBSET of prompt_tokens, not an addition to it: nothing in
        # the audit trail licenses summing all three.
        assert (
            anchor["cache_creation_prompt_tokens"] + anchor["cache_read_prompt_tokens"]
            <= anchor["prompt_tokens"]
        )

        for member in members:
            if member is anchor:
                continue
            assert "cache_creation_prompt_tokens" not in member
            assert "cache_read_prompt_tokens" not in member
            assert "prompt_tokens" not in member

    def test_a_usage_without_cache_keys_adds_neither(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """An HTTP seat reports no cache keys, and none are invented.

        A 0 here would read as "this call used no cache", which is a claim
        about a provider that never made it.  Absent must stay absent.
        """
        backend = InstrumentedBackend(
            BatchScript(), usage={"prompt_tokens": 40, "completion_tokens": 120}
        )
        worker = worker_for(
            store, {"fake1": backend}, batch_size=20, max_output_tokens_per_item=50
        )
        roster = big_roster(3)
        for _role, seat in roster.seats():
            grant_cap(monkeypatch, seat, 1_000_000)
        job_id = worker.submit(
            make_query(), tasks_for("item-0", "item-1", "item-2"), roster
        )
        run(worker.work(job_id=job_id, until_idle=True))

        anchors = [
            a["usage"]
            for a in attempts_of(store)
            if isinstance(a["usage"], dict)
            and "invocation_id" in a["usage"]
            and a["usage"].get("completion_tokens") is not None
        ]
        assert anchors, "no anchor attempt was recorded"
        for anchor in anchors:
            assert anchor["prompt_tokens"] == 40
            assert "cache_creation_prompt_tokens" not in anchor
            assert "cache_read_prompt_tokens" not in anchor

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


# ---------------------------------------------------------------------------
# A schema refusal is settled, not bisected (t2b)
# ---------------------------------------------------------------------------


class SchemaRefusingScript(BatchScript):
    """A :class:`BatchScript` whose *refuse_roles* refuse the ROLE SCHEMA.

    Mirrors what ``CodexCliBackend._run`` does when ``strict_problems``
    reports a non-empty lint (t2): the whole invocation comes back
    ``schema_incompatible`` WITHOUT the CLI being spawned, and it does so for
    every invocation of that role whatever items it carries and however many
    members it has -- because the refusal is a property of the role's answer
    schema, which a half and a singleton carry unchanged.
    """

    def __init__(self, *, refuse_roles: Sequence[str] = ("proposer",), **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.refuse_roles = set(refuse_roles)

    def __call__(self, req: Any) -> Any:
        if req.role in self.refuse_roles:
            raise SchemaIncompatible(
                "$: required must list every key in properties (missing: notes)"
            )
        return super().__call__(req)


def member_counts(backend: Any, seat_name: str) -> List[int]:
    """Members per invocation this backend saw for *seat_name*, in order.

    ``0`` means an UNWRAPPED single call -- the shape bisection bottoms out
    to -- so a list containing a zero is direct evidence of a singleton
    fallback, and one containing a number between 1 and the original batch
    size is evidence of a half.
    """
    return [
        len(_batched_pairs(req.user or ""))
        for req in backend.calls
        if req.seat.seat_name == seat_name
    ]


class TestSchemaRefusedBatch:
    """A refused batch settles its members; it is NOT bisected.

    The defect this pins (confront round 1, stall 3): the batch runner used
    to bisect EVERY ``batch_failed`` outcome without consulting whether the
    error could survive a retry, so one configuration error cost ``2n - 1``
    invocations -- every one of them refused identically -- before the rows
    reached the terminal state the FIRST refusal already justified.
    """

    def _worker(self, store: Any, script: BatchScript, monkeypatch: Any, **kwargs: Any) -> Any:
        backend = fake(script)
        worker = worker_for(
            store, {"fake1": backend}, batch_size=10, max_output_tokens_per_item=50, **kwargs
        )
        roster = big_roster(3)
        for _role, seat in roster.seats():
            grant_cap(monkeypatch, seat, 1_000_000)
        return worker, roster, backend

    def test_every_member_is_settled_and_the_backend_is_never_asked_again(
        self, store: Any, monkeypatch: Any
    ) -> None:
        worker, roster, backend = self._worker(
            store,
            SchemaRefusingScript(refuse_roles=("proposer",)),
            monkeypatch,
            # A max_attempts above 1 is what makes the assertion mean
            # something: the rows settle terminally because the ERROR is
            # terminal, not because their attempt budget ran out.
            max_attempts=3,
            retry_backoff_s=(1000.0, 1000.0),
        )
        items = ["item-1", "item-2", "item-3", "item-4"]
        job_id = worker.submit(make_query(), tasks_for(*items), roster)
        run(worker.work(job_id=job_id, until_idle=True))

        rows = calls_of(store, job_id, role="proposer")
        assert len(rows) == 12  # 3 proposer seats x 4 items
        for row in rows:
            assert row["status"] == "error", row["item_id"]
            assert row["error"]["kind"] == "schema_incompatible"
            assert row["error"]["retryable"] is False

        # EXACTLY ONE failed attempt per member, and a CHARGED (positive)
        # one -- not the negative uncharged ancestor-failure row a bisected
        # invocation files, because there is no later solo attempt to
        # reserve the budget for.
        by_call: Dict[str, List[Dict[str, Any]]] = {}
        for attempt in attempts_of(store):
            by_call.setdefault(attempt["call_id"], []).append(attempt)
        for row in rows:
            mine = by_call.get(row["call_id"], [])
            assert len(mine) == 1, f"{row['item_id']}: {len(mine)} attempts"
            assert mine[0]["attempt_no"] == 1
            assert mine[0]["outcome_kind"] == "schema_incompatible"
            # The invocation stays reconstructible from any member, exactly
            # as it is for a settled or a bisected one.
            assert mine[0]["usage"]["batch_size"] == 4

        # ONE invocation per seat, each carrying all four members: no half
        # was ever asked and no singleton fallback was ever made.
        for seat_name in ("proposer_1", "proposer_2", "proposer_3"):
            assert member_counts(backend, seat_name) == [4], seat_name
        assert len(backend.calls) == 3, "something else was asked a model"

        # Every item was ADVANCED, so the job finished rather than waiting
        # on rows nothing will ever answer.  "error" (not "ok"): no proposer
        # seat voted, so there is nothing for a critic or meta to audit.
        item_rows = store.get_items(job_id)
        assert len(item_rows) == 4
        assert all(row["status"] == "error" for row in item_rows)
        assert store.get_job(job_id)["status"] == "done"

    def test_a_non_schema_whole_invocation_failure_still_bisects(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """The other half of the branch: everything else behaves as before.

        Same four items and the same batch size as the refusal test above,
        failing the whole invocation for a reason a retry COULD survive --
        so the halves and then the singletons are asked, which is the
        behaviour the base commit has and this task must not change.
        """
        worker, roster, backend = self._worker(
            store,
            BatchScript(poison_item_ids={"poison"}, poison_seat="proposer_1"),
            monkeypatch,
            # The DEFAULT fast backoff, so the isolated poison row's own
            # retry happens inside this run and it reaches its terminal
            # state -- the shape ``TestBisection`` already pins.
            max_attempts=2,
        )
        items = ["item-1", "item-2", "poison", "item-4"]
        job_id = worker.submit(make_query(), tasks_for(*items), roster)
        run(worker.work(job_id=job_id, until_idle=True))

        counts = member_counts(backend, "proposer_1")
        assert counts[0] == 4, "the whole batch was asked first"
        assert 2 in counts, "the halves were asked -- bisection still happens"
        assert 0 in counts, "bisection still bottoms out in single calls"

        rows = {r["item_id"]: r for r in calls_of(store, job_id, role="proposer", seat_name="proposer_1")}
        assert rows["poison"]["status"] == "error"
        assert rows["poison"]["error"]["kind"] == "transport"
        for item_id in ("item-1", "item-2", "item-4"):
            assert rows[item_id]["status"] == "ok"


# ---------------------------------------------------------------------------
# The raw reply lands on the ANCHOR attempt only (KBUtilLib#92 item 1)
# ---------------------------------------------------------------------------


class WrapperInvalidScript(BatchScript):
    """A script whose BATCHED replies ignore the wrapper schema.

    One batched invocation carries one reply, so a reply that is not the
    ``{"answers": [...]}`` shape fails the invocation as a whole without
    judging any item -- the ``batch_failed`` outcome the caller bisects.  It
    is the whole-invocation failure that HAS a body: a transport failure or a
    timeout usually has none, and a schema refusal never gets as far as the
    model.

    Solo calls fall through to :class:`BatchScript`, so bisection still
    bottoms out in rows that answer.
    """

    #: What a batched invocation of this script comes back with.
    PROSE = {"notes": "I will answer in prose instead, thank you"}

    @property
    def wrapper_invalid_text(self) -> str:
        """The exact ``LLMResult.text`` a batched invocation produced."""
        return json.dumps(self.PROSE, sort_keys=True)

    def __call__(self, req: Any) -> Any:
        if _batched_pairs(req.user or ""):
            return dict(self.PROSE)
        return super().__call__(req)


class RecordingBatchScript(BatchScript):
    """A :class:`BatchScript` that remembers the TEXT of every batched reply.

    ``FakeBackend`` serialises a script's answer with
    ``json.dumps(answer, sort_keys=True)``, so what this records is
    byte-identical to the ``LLMResult.text`` the engine received -- which is
    what lets a test assert that ``attempts.reply_text`` holds the model's
    reply VERBATIM rather than something re-serialised from the parsed
    elements.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.batched_replies: List[str] = []

    def __call__(self, req: Any) -> Any:
        answer = super().__call__(req)
        if _batched_pairs(req.user or ""):
            self.batched_replies.append(json.dumps(answer, sort_keys=True))
        return answer


def batched_attempts_by_invocation(store: Any) -> Dict[str, List[Dict[str, Any]]]:
    """``{invocation_id: [attempt rows]}`` for every BATCHED attempt.

    Keyed off the ``invocation_id`` every member's usage payload carries, so
    solo attempts (bisection's leaves, which have no invocation) are excluded
    without the test having to guess which rows those are.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    for row in attempts_of(store):
        usage = row.get("usage") or {}
        if isinstance(usage, Mapping) and "invocation_id" in usage:
            out.setdefault(str(usage["invocation_id"]), []).append(row)
    return out


def _anchor_reply(rows: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """The reply_text the anchor of this invocation actually stored.

    Read out of the rows so :func:`check_reply_on_anchor_only` can be reused
    for "whatever the anchor holds, no other member holds anything" without
    the test having to rebuild the reply string; the tests that care about
    WHAT it holds assert that separately against the script's own record.
    """
    anchor_id = str((rows[0]["usage"] or {})["anchor_call_id"])
    for row in rows:
        if str(row["call_id"]) == anchor_id:
            return row["reply_text"]
    raise AssertionError("the anchor's own attempt is missing")


def check_reply_on_anchor_only(
    rows: Sequence[Mapping[str, Any]], expected: Optional[str]
) -> Dict[str, Any]:
    """Assert ONE invocation wrote *expected* on its anchor attempt and NULL
    on every other member, and return the anchor row.

    The anchor is read from the usage payload rather than from member order:
    ``anchor_call_id`` is what the audit trail actually points the other
    members at, so this checks the column against the same answer a reader of
    the store would get.
    """
    assert rows, "no attempts were recorded for this invocation"
    anchor_ids = {str((row["usage"] or {})["anchor_call_id"]) for row in rows}
    assert len(anchor_ids) == 1, f"members disagree about the anchor: {anchor_ids}"
    (anchor_id,) = anchor_ids
    anchors = [row for row in rows if str(row["call_id"]) == anchor_id]
    assert len(anchors) == 1, "the anchor's own attempt is missing"
    assert anchors[0]["reply_text"] == expected
    for row in rows:
        if str(row["call_id"]) == anchor_id:
            continue
        assert row["reply_text"] is None, (
            f"member {row['call_id']} duplicated the invocation's reply; it must "
            "refer to the anchor through usage['anchor_call_id'] instead"
        )
    return dict(anchors[0])


class TestRawReplyOnTheAnchor:
    """One invocation, one reply, recorded ONCE.

    A batched invocation's reply produced N members' outcomes, so when
    anything about it fails the whole reply is the evidence and it belongs to
    the invocation rather than to any one member.  It is written on the
    ANCHOR attempt -- even when the anchor's own outcome is ``ok`` -- and
    every other member's ``reply_text`` stays NULL and points at the anchor
    through the ``anchor_call_id`` its usage payload already carried.

    What is stored is always what the MODEL SENT.  Parsed or repaired
    elements are never serialised back into the column: the point of it is to
    show what arrived.
    """

    def _worker(self, store: Any, script: Any, monkeypatch: Any, **kwargs: Any) -> Any:
        backend = fake(script)
        worker = worker_for(
            store, {"fake1": backend}, batch_size=10, max_output_tokens_per_item=50, **kwargs
        )
        roster = big_roster(3)
        for _role, seat in roster.seats():
            grant_cap(monkeypatch, seat, 1_000_000)
        return worker, roster, backend

    def test_a_whole_wrapper_failure_stores_the_reply_on_its_anchor(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """The reply that failed the WHOLE invocation is kept.

        No member row is completed by this path -- the claims are held for
        the bisection -- so no ``calls.reply_text`` is written for any of
        them and the anchor's attempt is the only record of what came back.
        """
        script = WrapperInvalidScript()
        worker, roster, backend = self._worker(store, script, monkeypatch)
        job_id = worker.submit(
            make_query(), tasks_for("item-1", "item-2", "item-3"), roster
        )
        run(worker.work(job_id=job_id, until_idle=True))

        invocations = batched_attempts_by_invocation(store)
        assert invocations, "no batched invocation was recorded"
        for rows in invocations.values():
            anchor = check_reply_on_anchor_only(rows, script.wrapper_invalid_text)
            # The uncharged negative attempt_no of a whole-invocation
            # failure: the reply is captured without spending any row's
            # single-item attempt budget.
            assert anchor["attempt_no"] < 0
            assert anchor["outcome_kind"] == "invalid_output"
        # Bisection still bottomed out in solo calls that answered, and those
        # attempts record no reply of their own.
        solo = [
            row
            for row in attempts_of(store)
            if not isinstance(row.get("usage"), Mapping)
            or "invocation_id" not in (row.get("usage") or {})
        ]
        assert solo, "bisection must still reach single calls"
        assert all(row["reply_text"] is None for row in solo if row["outcome_kind"] == "ok")

    def test_a_partly_invalid_batch_stores_it_on_an_anchor_that_is_ok(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """THE EXCEPTION, stated: an 'ok' attempt carries the reply.

        The wrapper was valid and every member was judged individually, so
        the anchor's own element was fine and its call settled ``ok``.  The
        reply is still the only evidence for the member that did NOT settle,
        and it is one reply -- so it goes on the anchor.

        Claims are handed out in ``item_order`` (see
        ``queries.select_claimable``), so the anchor is item-1's call and the
        invalid item is deliberately the last one.
        """
        script = RecordingBatchScript(invalid_item_ids=["item-3"])
        worker, roster, backend = self._worker(
            store, script, monkeypatch, max_attempts=1
        )
        job_id = worker.submit(
            make_query(), tasks_for("item-1", "item-2", "item-3"), roster
        )
        run(worker.work(job_id=job_id, until_idle=True))

        three = [
            rows
            for rows in batched_attempts_by_invocation(store).values()
            if len(rows) == 3
        ]
        assert three, "no three-member invocation was recorded"
        for rows in three:
            anchor = check_reply_on_anchor_only(rows, _anchor_reply(rows))
            assert anchor["outcome_kind"] == "ok", (
                "the anchor's own element was valid -- that is exactly the case "
                "this test exists for"
            )
            # VERBATIM: byte-identical to a reply the backend really sent,
            # and it still carries the invalid element rather than anything
            # normalise() made of it.
            assert anchor["reply_text"] in script.batched_replies
            assert "not-a-list" in anchor["reply_text"]
            invalid = [r for r in rows if r["outcome_kind"] == "invalid_output"]
            assert len(invalid) == 1
            assert invalid[0]["reply_text"] is None

    def test_a_missing_member_stores_it_on_the_anchor_too(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """An omitted answer is judged like an invalid one, and the reply --
        the thing that shows the answer really is absent rather than
        mislaid -- is kept once, on the anchor."""
        script = RecordingBatchScript(missing_item_ids=["item-3"])
        worker, roster, backend = self._worker(
            store, script, monkeypatch, max_attempts=1
        )
        job_id = worker.submit(
            make_query(), tasks_for("item-1", "item-2", "item-3"), roster
        )
        run(worker.work(job_id=job_id, until_idle=True))

        three = [
            rows
            for rows in batched_attempts_by_invocation(store).values()
            if len(rows) == 3
        ]
        assert three, "no three-member invocation was recorded"
        for rows in three:
            anchor = check_reply_on_anchor_only(rows, _anchor_reply(rows))
            assert anchor["outcome_kind"] == "ok"
            assert anchor["reply_text"] in script.batched_replies
            missing = [r for r in rows if r["outcome_kind"] == "missing"]
            assert len(missing) == 1
            assert missing[0]["reply_text"] is None
            # The stored reply really is the one with the gap in it: the
            # absent member's item_key appears nowhere in it.
            assert str(missing[0]["call_id"]) not in anchor["reply_text"]

    def test_a_fully_valid_batch_records_no_reply_text_at_all(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """Nothing failed, so there is nothing the call rows do not already
        say: each member's own ``calls.reply_text`` holds its element."""
        worker, roster, backend = self._worker(store, BatchScript(), monkeypatch)
        job_id = worker.submit(
            make_query(), tasks_for("item-1", "item-2", "item-3"), roster
        )
        run(worker.work(job_id=job_id, until_idle=True))

        rows = attempts_of(store)
        assert rows
        assert all(row["reply_text"] is None for row in rows)

    def test_a_schema_refused_batch_has_no_reply_to_record(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """A schema refusal carries NO text, and none is invented.

        ``schema_incompatible`` is raised before the backend runs anything --
        a ``CodexCliBackend`` that cannot serve a role's answer schema never
        spawns the CLI (t2) -- so the result has no body at all.  The anchor
        rule is applied to this path like the other two, and on this path it
        correctly writes nothing: ``reply_text`` is NULL for every member,
        including the anchor.
        """
        worker, roster, backend = self._worker(
            store,
            SchemaRefusingScript(refuse_roles=("proposer",)),
            monkeypatch,
            max_attempts=3,
        )
        job_id = worker.submit(make_query(), tasks_for("item-1", "item-2"), roster)
        run(worker.work(job_id=job_id, until_idle=True))

        rows = attempts_of(store)
        assert rows, "the refusal must still be audited"
        assert all(row["outcome_kind"] == "schema_incompatible" for row in rows)
        assert all(row["reply_text"] is None for row in rows)
