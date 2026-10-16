"""The council engine: the decision rule, and the worker that drives it.

Two halves, deliberately in one file because they are two views of one
contract:

* :class:`TestDecisionRule` and friends exercise ``decide()`` directly -- no
  store, no event loop, no backend.  Every row of the rule table in the
  design is a test here.
* the worker classes drive real ``SqliteStore`` rows through
  ``CouncilWorker`` with ``FakeBackend`` seats: reuse, single-flight, retry,
  halts, budgets, shared-account concurrency, cancellation, stale claims,
  priority and order independence.

No test makes a network call, spends a token, needs a CLI login or needs a
GPU.  Every seat is a ``fake`` endpoint and every answer comes from a script.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional, Sequence

import pytest

from kbutillib.domains.ai.council.engine import CouncilWorker, evaluate
from kbutillib.domains.ai.council.engine.decide import (
    APPROVING_VERDICTS,
    SettledCall,
    critic_approves,
    decide,
)
from kbutillib.domains.ai.council.engine.worker import _validate_answer
from kbutillib.domains.ai.council.store.errors import StaleClaim
from kbutillib.domains.ai.council.types import (
    AuthError,
    BillingGuardRefused,
    CouncilError,
    CouncilTask,
    CouncilVerdict,
    InvalidOutput,
    MenuEntry,
    ModelUnavailable,
    QuotaExhausted,
    Roster,
    RosterError,
    SeatSpec,
    TransportError,
)

from .engine_support import (
    InFlight,
    InstrumentedBackend,
    critic_answer,
    endpoint_config,
    fake_seat,
    make_query,
    make_roster,
    make_task,
    menu,
    meta_answer,
    proposer_answer,
    run,
    scripted,
)

# ---------------------------------------------------------------------------
# Settled-call builders for the decision-rule tests
# ---------------------------------------------------------------------------


def proposer(seat: str, *ids: str, **kwargs: Any) -> SettledCall:
    """A settled proposer call that picked *ids*."""
    return SettledCall(
        role="proposer",
        seat_name=seat,
        status="ok",
        call_id=f"call-{seat}",
        parsed=proposer_answer(*ids, **kwargs),
        usage={"prompt_tokens": 10, "completion_tokens": 5},
    )


def failed(seat: str, role: str = "proposer", kind: str = "transport") -> SettledCall:
    """A settled call that failed."""
    return SettledCall(
        role=role,
        seat_name=seat,
        status="error",
        call_id=f"call-{seat}",
        error=CouncilError(kind=kind, message="the endpoint fell over"),
    )


def critic(**kwargs: Any) -> SettledCall:
    """A settled critic call."""
    return SettledCall(
        role="critic",
        seat_name="critic",
        status="ok",
        call_id="call-critic",
        parsed=critic_answer(**kwargs),
    )


def meta(*ids: str, **kwargs: Any) -> SettledCall:
    """A settled meta call."""
    return SettledCall(
        role="meta",
        seat_name="meta",
        status="ok",
        call_id="call-meta",
        parsed=meta_answer(*ids, **kwargs),
    )


def run_rule(
    proposers: Sequence[Optional[SettledCall]],
    critic_call: Optional[SettledCall] = None,
    meta_call: Optional[SettledCall] = None,
    *,
    task: Any = None,
    roster: Any = None,
) -> tuple:
    """Call ``decide`` over the standard three-seat council."""
    return decide(
        task or make_task(),
        roster or make_roster(3),
        make_query(),
        list(proposers),
        critic_call,
        meta_call,
    )


# ---------------------------------------------------------------------------
# The decision rule
# ---------------------------------------------------------------------------


class TestCriticApproval:
    """The three clauses of approval, each on its own."""

    @pytest.mark.parametrize("verdict", APPROVING_VERDICTS)
    def test_a_clean_consistent_verdict_approves(self, verdict: str) -> None:
        assert critic_approves(critic_answer(verdict=verdict)) is True

    @pytest.mark.parametrize("verdict", ["ambiguous", "inconsistent", "", "nonsense"])
    def test_any_other_verdict_does_not_approve(self, verdict: str) -> None:
        assert critic_approves(critic_answer(verdict=verdict)) is False

    def test_an_overpick_withholds_approval(self) -> None:
        assert critic_approves(critic_answer(overpicked=["A:1"])) is False

    def test_a_missed_id_withholds_approval(self) -> None:
        assert critic_approves(critic_answer(missed=["B:2"])) is False

    def test_no_critic_at_all_does_not_approve(self) -> None:
        assert critic_approves(None) is False


class TestDecisionRule:
    """One test per row of the design's decision table."""

    def test_unanimous_and_approved_skips_meta(self) -> None:
        """{A:3} with an approving critic: decided, with no meta call."""
        what, verdict = run_rule(
            [proposer(f"proposer_{i}", "A:1") for i in (1, 2, 3)], critic()
        )
        assert what == "verdict"
        assert verdict.status == "ok"
        assert verdict.selected_ids == ["A:1"]
        assert verdict.votes == {"A:1": 3}
        assert verdict.n_valid == 3
        assert verdict.decided_by == "unanimous"
        assert verdict.meta_skipped is True
        assert verdict.final_confidence == "high"
        assert verdict.meta is None
        assert verdict.decline_reason is None

    def test_a_partial_majority_goes_to_meta(self) -> None:
        """{A:3, B:2} with n_valid 3: meta runs even though A is unanimous."""
        what, verdict = run_rule(
            [
                proposer("proposer_1", "A:1", "B:2"),
                proposer("proposer_2", "A:1", "B:2"),
                proposer("proposer_3", "A:1"),
            ],
            critic(),
        )
        assert (what, verdict) == ("need_meta", None)

    def test_the_partial_majority_is_decided_once_meta_answers(self) -> None:
        what, verdict = run_rule(
            [
                proposer("proposer_1", "A:1", "B:2"),
                proposer("proposer_2", "A:1", "B:2"),
                proposer("proposer_3", "A:1"),
            ],
            critic(),
            meta("A:1"),
        )
        assert what == "verdict"
        assert verdict.decided_by == "meta"
        assert verdict.meta_skipped is False
        assert verdict.selected_ids == ["A:1"]
        assert verdict.votes == {"A:1": 3, "B:2": 2}
        assert verdict.final_confidence == "medium"

    def test_empty_votes_with_an_approving_critic_is_decided(self) -> None:
        """Nobody picked anything and the critic approved: no meta."""
        what, verdict = run_rule(
            [proposer(f"proposer_{i}") for i in (1, 2, 3)],
            critic(contentless=True),
        )
        assert what == "verdict"
        assert verdict.status == "ok"
        assert verdict.selected_ids == []
        assert verdict.votes == {}
        assert verdict.decided_by == "unanimous"
        assert verdict.meta_skipped is True

    def test_empty_votes_are_contentless_when_a_majority_says_so(self) -> None:
        what, verdict = run_rule(
            [
                proposer("proposer_1", no_content=True),
                proposer("proposer_2", no_content=True),
                proposer("proposer_3"),
            ],
            critic(),
        )
        assert verdict.decline_reason == "contentless"

    def test_an_ambiguous_critic_sends_the_item_to_meta(self) -> None:
        what, verdict = run_rule(
            [proposer(f"proposer_{i}", "A:1") for i in (1, 2, 3)],
            critic(verdict="ambiguous"),
        )
        assert (what, verdict) == ("need_meta", None)

    def test_a_missed_id_sends_the_item_to_meta(self) -> None:
        what, verdict = run_rule(
            [proposer(f"proposer_{i}", "A:1") for i in (1, 2, 3)],
            critic(missed=["C:3"]),
        )
        assert (what, verdict) == ("need_meta", None)

    def test_an_overpick_sends_the_item_to_meta(self) -> None:
        what, verdict = run_rule(
            [proposer(f"proposer_{i}", "A:1") for i in (1, 2, 3)],
            critic(overpicked=["A:1"]),
        )
        assert (what, verdict) == ("need_meta", None)

    def test_a_failed_proposer_fails_the_item_and_stops_the_council(self) -> None:
        what, verdict = run_rule(
            [
                proposer("proposer_1", "A:1"),
                failed("proposer_2"),
                proposer("proposer_3", "A:1"),
            ],
            critic(),
            meta("A:1"),
        )
        assert what == "verdict"
        assert verdict.status == "error"
        assert verdict.selected_ids == []
        assert verdict.decided_by is None
        assert verdict.decline_reason is None
        assert verdict.final_confidence is None
        # The critic and meta answers were available and were NOT used.
        assert verdict.critic is None
        assert verdict.meta is None
        kinds = [(e["stage"], e["seat"], e["kind"]) for e in verdict.errors]
        assert kinds == [("proposer", "proposer_2", "transport")]
        # The picks that WERE cast stay visible.
        assert [b.status for b in verdict.ballots] == ["ok", "error", "ok"]
        assert verdict.votes == {"A:1": 2}

    def test_a_failed_proposer_waits_for_a_slow_sibling(self) -> None:
        what, verdict = run_rule(
            [proposer("proposer_1", "A:1"), failed("proposer_2"), None]
        )
        assert (what, verdict) == ("waiting", None)

    def test_an_outstanding_proposer_waits(self) -> None:
        what, verdict = run_rule([proposer("proposer_1", "A:1"), None, None])
        assert (what, verdict) == ("waiting", None)

    def test_a_failed_critic_fails_the_item_and_skips_meta(self) -> None:
        what, verdict = run_rule(
            [proposer(f"proposer_{i}", "A:1") for i in (1, 2, 3)],
            failed("critic", role="critic", kind="timeout"),
            meta("A:1"),
        )
        assert what == "verdict"
        assert verdict.status == "error"
        assert verdict.meta is None
        assert verdict.meta_skipped is True
        assert [(e["stage"], e["kind"]) for e in verdict.errors] == [
            ("critic", "timeout")
        ]

    def test_a_failed_meta_fails_the_item(self) -> None:
        what, verdict = run_rule(
            [
                proposer("proposer_1", "A:1"),
                proposer("proposer_2", "A:1"),
                proposer("proposer_3"),
            ],
            critic(),
            failed("meta", role="meta", kind="invalid_output"),
        )
        assert what == "verdict"
        assert verdict.status == "error"
        assert verdict.selected_ids == []
        assert verdict.decided_by is None
        assert verdict.meta_skipped is False
        assert [(e["stage"], e["kind"]) for e in verdict.errors] == [
            ("meta", "invalid_output")
        ]

    def test_a_duplicate_pick_is_counted_once(self) -> None:
        what, verdict = run_rule(
            [
                proposer("proposer_1", "A:1", "A:1"),
                proposer("proposer_2", "A:1"),
                proposer("proposer_3", "A:1"),
            ],
            critic(),
        )
        assert what == "verdict"
        assert verdict.votes == {"A:1": 3}
        assert [p.id for p in verdict.ballots[0].picks] == ["A:1"]
        assert verdict.decided_by == "unanimous"

    def test_an_off_menu_pick_is_dropped_and_recorded(self) -> None:
        what, verdict = run_rule(
            [
                proposer("proposer_1", "A:1", "GO:9999999"),
                proposer("proposer_2", "A:1"),
                proposer("proposer_3", "A:1"),
            ],
            critic(),
        )
        assert what == "verdict"
        assert verdict.votes == {"A:1": 3}
        assert verdict.selected_ids == ["A:1"]
        # The ballot stays ok: an off-menu id is a note, not a failure.
        assert verdict.ballots[0].status == "ok"
        assert [p.id for p in verdict.ballots[0].picks] == ["A:1"]
        off_menu = [e for e in verdict.errors if e["kind"] == "off_menu"]
        assert len(off_menu) == 1
        assert off_menu[0]["stage"] == "proposer"
        assert off_menu[0]["seat"] == "proposer_1"
        assert "GO:9999999" in off_menu[0]["message"]

    def test_an_off_menu_meta_id_is_dropped_and_recorded(self) -> None:
        what, verdict = run_rule(
            [
                proposer("proposer_1", "A:1"),
                proposer("proposer_2", "B:2"),
                proposer("proposer_3", "A:1"),
            ],
            critic(),
            meta("A:1", "NOT:ON:MENU"),
        )
        assert what == "verdict"
        assert verdict.selected_ids == ["A:1"]
        off_menu = [e for e in verdict.errors if e["kind"] == "off_menu"]
        assert [(e["stage"], e["seat"]) for e in off_menu] == [("meta", "meta")]

    def test_an_empty_menu_is_decided_with_zero_calls(self) -> None:
        what, verdict = run_rule([], task=make_task(menu=[]))
        assert what == "verdict"
        assert verdict.status == "ok"
        assert verdict.selected_ids == []
        assert verdict.decline_reason == "no_candidates"
        assert verdict.decided_by == "deterministic_empty"
        assert verdict.n_valid == 0
        assert verdict.ballots == []
        assert verdict.usage == {}

    def test_a_contentless_tie_is_broken_by_the_critic(self) -> None:
        """Two of four proposers flag no content: the critic decides."""
        roster = make_roster(4)
        flags = [True, True, False, False]
        proposers = [
            proposer(f"proposer_{i}", no_content=flag)
            for i, flag in enumerate(flags, start=1)
        ]
        what, verdict = run_rule(
            proposers, critic(contentless=True), roster=roster
        )
        assert verdict.decline_reason == "contentless"

        what, verdict = run_rule(
            proposers, critic(contentless=False), roster=roster
        )
        assert verdict.decline_reason == "no_candidate_matched"

    def test_the_minority_content_flag_counterexample(self) -> None:
        """[True, False, False] with empty picks is NOT contentless.

        The counterexample the design names: one of three proposers says the
        string has no function, nobody picked anything, and the critic
        approves.  2*1 < 3, so the flag is False and the item declined for
        want of a matching candidate -- not because the input was empty.
        """
        what, verdict = run_rule(
            [
                proposer("proposer_1", no_content=True),
                proposer("proposer_2"),
                proposer("proposer_3"),
            ],
            critic(contentless=True),
        )
        assert what == "verdict"
        assert verdict.status == "ok"
        assert verdict.selected_ids == []
        assert verdict.decline_reason == "no_candidate_matched"

    def test_selected_ids_are_in_menu_order_on_the_skip_path(self) -> None:
        """The menu is C:3, A:1, B:2; the proposers name them backwards."""
        what, verdict = run_rule(
            [
                proposer("proposer_1", "B:2", "A:1", "C:3"),
                proposer("proposer_2", "A:1", "C:3", "B:2"),
                proposer("proposer_3", "C:3", "B:2", "A:1"),
            ],
            critic(),
        )
        assert what == "verdict"
        assert verdict.selected_ids == ["C:3", "A:1", "B:2"]
        # Votes keep FIRST-VOTE order, which is what the vote block renders in.
        assert list(verdict.votes) == ["B:2", "A:1", "C:3"]

    def test_selected_ids_are_in_menu_order_on_the_meta_path(self) -> None:
        what, verdict = run_rule(
            [
                proposer("proposer_1", "A:1"),
                proposer("proposer_2", "B:2"),
                proposer("proposer_3", "A:1"),
            ],
            critic(),
            meta("B:2", "A:1", "C:3"),
        )
        assert verdict.selected_ids == ["C:3", "A:1", "B:2"]

    def test_meta_may_select_nothing(self) -> None:
        what, verdict = run_rule(
            [
                proposer("proposer_1", "A:1"),
                proposer("proposer_2", "B:2"),
                proposer("proposer_3", "A:1"),
            ],
            critic(),
            meta(no_content=True),
        )
        assert verdict.status == "ok"
        assert verdict.selected_ids == []
        assert verdict.decline_reason == "contentless"
        assert verdict.decided_by == "meta"

    def test_a_reply_with_no_picked_list_fails_its_seat(self) -> None:
        broken = SettledCall(
            role="proposer", seat_name="proposer_2", status="ok", parsed={"picked": None}
        )
        what, verdict = run_rule(
            [proposer("proposer_1", "A:1"), broken, proposer("proposer_3", "A:1")],
            critic(),
        )
        assert verdict.status == "error"
        assert [e["kind"] for e in verdict.errors] == ["invalid_output"]

    def test_usage_is_reported_per_seat(self) -> None:
        reused = SettledCall(
            role="proposer",
            seat_name="proposer_3",
            status="ok",
            parsed=proposer_answer("A:1"),
            usage={"prompt_tokens": 0, "completion_tokens": 0},
            reused=True,
        )
        what, verdict = run_rule(
            [proposer("proposer_1", "A:1"), proposer("proposer_2", "A:1"), reused],
            critic(),
        )
        assert verdict.usage["proposer_1"] == {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "reused": False,
        }
        assert verdict.usage["proposer_3"]["reused"] is True
        assert "critic" in verdict.usage
        assert "meta" not in verdict.usage

    def test_the_verdict_carries_the_query_and_roster_identity(self) -> None:
        roster = make_roster(3)
        what, verdict = run_rule(
            [proposer(f"proposer_{i}", "A:1") for i in (1, 2, 3)],
            critic(),
            roster=roster,
        )
        assert verdict.query == {"name": "engine_test", "version": "1"}
        assert verdict.roster_fingerprint == roster.fingerprint()
        assert verdict.decided_at.endswith("+00:00")

    def test_calls_may_arrive_out_of_seat_order(self) -> None:
        """A store read comes back in created_at order, not seat order."""
        what, verdict = run_rule(
            [
                proposer("proposer_3", "A:1"),
                proposer("proposer_1", "A:1", "B:2"),
                proposer("proposer_2", "A:1"),
            ],
            critic(),
        )
        assert what == "need_meta"
        # Ballots are in ROSTER order whatever order the calls came in.
        what, verdict = run_rule(
            [
                proposer("proposer_3", "A:1"),
                proposer("proposer_1", "A:1"),
                proposer("proposer_2", "A:1"),
            ],
            critic(),
        )
        assert [b.seat for b in verdict.ballots] == [
            "proposer_1",
            "proposer_2",
            "proposer_3",
        ]


class TestSettledCallFromRow:
    """``from_call_row`` is the only place ``postprocess`` is applied."""

    def test_an_ok_row_is_postprocessed(self) -> None:
        query = make_query()
        call = SettledCall.from_call_row(
            {
                "call_id": "c1",
                "role": "proposer",
                "seat_name": "proposer_1",
                "status": "ok",
                "reply_parsed": proposer_answer("A:1"),
                "fresh_usage": {"prompt_tokens": 3, "completion_tokens": 4},
                "reused_from": None,
            },
            query,
        )
        assert call.status == "ok"
        assert [p["id"] for p in call.parsed["picked"]] == ["A:1"]
        assert call.reused is False

    def test_a_reused_row_is_marked_reused(self) -> None:
        call = SettledCall.from_call_row(
            {
                "role": "proposer",
                "seat_name": "proposer_1",
                "status": "ok",
                "reply_parsed": proposer_answer("A:1"),
                "reused_from": "another-call",
            },
            make_query(),
        )
        assert call.reused is True

    def test_an_error_row_keeps_its_error(self) -> None:
        call = SettledCall.from_call_row(
            {
                "role": "proposer",
                "seat_name": "proposer_1",
                "status": "error",
                "error": {"kind": "quota_exhausted", "message": "no quota"},
            },
            make_query(),
        )
        assert call.status == "error"
        assert call.error.kind == "quota_exhausted"
        assert call.parsed is None

    def test_a_postprocess_that_raises_fails_the_seat(self) -> None:
        class Exploding:
            name = "boom"
            version = "1"

            def postprocess(self, role: str, parsed: Dict[str, Any]) -> Dict[str, Any]:
                raise RuntimeError("cannot read this answer")

        call = SettledCall.from_call_row(
            {
                "role": "proposer",
                "seat_name": "proposer_1",
                "status": "ok",
                "reply_parsed": {"anything": 1},
            },
            Exploding(),
        )
        assert call.status == "error"
        assert call.error.kind == "invalid_output"
        assert "cannot read this answer" in call.error.message


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------

WORKER_DEFAULTS: Dict[str, Any] = {
    # Fast but not instant: the engine's real backoff is 2 s then 8 s (pinned
    # by TestBackoff below), which would make the retry test wait for it.
    "heartbeat_interval_s": 0.0,
    "poll_interval_s": 0.01,
    "retry_backoff_s": (0.02, 0.05),
    "retry_jitter": 0.0,
    "idle_grace_s": 0.3,
    "max_idle_s": 15.0,
}


def worker_for(
    store: Any,
    backends: Mapping[str, Any],
    *,
    user: str = "alice",
    worker_id: str = "w-1",
    config: Any = None,
    **kwargs: Any,
) -> CouncilWorker:
    """A worker over *backends*, with test-fast timings."""
    settings = dict(WORKER_DEFAULTS)
    settings.update(kwargs)
    if config is None:
        config = endpoint_config(**{name: {} for name in backends})
    return CouncilWorker(store, config, backends, user, worker_id, **settings)


def fake(script: Any, **kwargs: Any) -> InstrumentedBackend:
    """An instrumented fake backend driven by a ``(role, seat)`` table."""
    return InstrumentedBackend(scripted(script), **kwargs)


def agreeing_script(*ids: str, **critic_kwargs: Any) -> Dict[Any, Any]:
    """Every proposer picks *ids*; the critic approves."""
    return {
        "proposer": proposer_answer(*ids),
        "critic": critic_answer(**critic_kwargs),
        "meta": meta_answer(*ids),
    }


def split_script() -> Dict[Any, Any]:
    """A vote table of {A:3, B:2}: unanimous on A, split on B."""
    return {
        ("proposer", "proposer_1"): proposer_answer("A:1", "B:2"),
        ("proposer", "proposer_2"): proposer_answer("A:1", "B:2"),
        ("proposer", "proposer_3"): proposer_answer("A:1"),
        "critic": critic_answer(),
        "meta": meta_answer("A:1"),
    }


def calls_of(store: Any, job_id: str, role: Optional[str] = None) -> list:
    rows = store.get_calls(job_id)
    return [r for r in rows if role is None or r["role"] == role]


def item_of(store: Any, job_id: str, item_id: str = "item-1") -> Dict[str, Any]:
    return next(r for r in store.get_items(job_id) if r["item_id"] == item_id)


def verdict_of(store: Any, job_id: str, item_id: str = "item-1") -> CouncilVerdict:
    row = item_of(store, job_id, item_id)
    assert row["verdict"] is not None, f"item {item_id} is {row['status']}, not decided"
    return CouncilVerdict.model_validate(row["verdict"])


class TestSubmit:
    """What a job looks like the instant it exists, and what is refused."""

    def test_a_job_lands_with_one_pending_call_per_proposer(self, store: Any) -> None:
        backend = fake(agreeing_script("A:1"))
        worker = worker_for(store, {"fake1": backend})
        roster = make_roster(3)
        job_id = worker.submit(make_query(), [make_task()], roster, label="a batch")

        job = store.get_job(job_id)
        assert job["status"] == "queued"
        assert job["submitted_by"] == "alice"
        assert job["roster_fingerprint"] == roster.fingerprint()
        assert job["n_items"] == 1
        assert sorted(job["roster"]["route_fingerprints"]) == [
            "critic",
            "meta",
            "proposer_1",
            "proposer_2",
            "proposer_3",
        ]

        items = store.get_items(job_id)
        assert [row["status"] for row in items] == ["pending"]
        assert items[0]["entity_hash"] and items[0]["data_hash"]

        calls = calls_of(store, job_id)
        assert [row["role"] for row in calls] == ["proposer"] * 3
        assert all(row["status"] == "pending" for row in calls)
        assert all(row["upstream_digest"] == "" for row in calls)
        # One prompt hash per seat: same question, different model.
        assert len({row["prompt_hash"] for row in calls}) == 3
        assert all(row["route_fingerprint"] for row in calls)
        # Nothing was asked of any model yet.
        assert backend.calls == []

    def test_an_empty_menu_is_settled_at_submit_with_no_calls(self, store: Any) -> None:
        backend = fake(agreeing_script("A:1"))
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(
            make_query(), [make_task(menu=[])], make_roster(3), label="empty"
        )

        assert calls_of(store, job_id) == []
        assert backend.calls == []
        verdict = verdict_of(store, job_id)
        assert verdict.decline_reason == "no_candidates"
        assert verdict.decided_by == "deterministic_empty"
        # Every item is settled, so the job is done on arrival rather than
        # queued behind calls that will never exist.
        assert store.get_job(job_id)["status"] == "done"

    def test_an_identical_repeat_of_an_item_is_collapsed(self, store: Any) -> None:
        worker = worker_for(store, {"fake1": fake(agreeing_script("A:1"))})
        task = make_task()
        job_id = worker.submit(make_query(), [task, task], make_roster(3))
        assert len(store.get_items(job_id)) == 1
        assert store.get_job(job_id)["n_items"] == 1
        assert len(calls_of(store, job_id)) == 3

    def test_a_repeated_item_id_with_different_content_is_refused(
        self, store: Any
    ) -> None:
        worker = worker_for(store, {"fake1": fake(agreeing_script("A:1"))})
        with pytest.raises(ValueError, match="DIFFERENT content"):
            worker.submit(
                make_query(),
                [make_task(text="one"), make_task(text="two")],
                make_roster(3),
            )
        assert store.list_jobs(user="alice") == []

    def test_a_duplicate_menu_id_is_refused(self, store: Any) -> None:
        worker = worker_for(store, {"fake1": fake(agreeing_script("A:1"))})
        duplicated = menu("A:1", "B:2", "A:1")
        with pytest.raises(ValueError, match="more than once"):
            worker.submit(make_query(), [make_task(menu=duplicated)], make_roster(3))
        assert store.list_jobs(user="alice") == []

    def test_an_unverified_seat_is_refused(self, store: Any) -> None:
        worker = worker_for(store, {"fake1": fake(agreeing_script("A:1"))})
        roster = make_roster(3)
        roster.proposers[1] = fake_seat("proposer_2", "p2", verified=False)
        with pytest.raises(RosterError, match="verified"):
            worker.submit(make_query(), [make_task()], roster)
        assert store.list_jobs(user="alice") == []

    def test_an_unverified_seat_may_be_allowed_explicitly(self, store: Any) -> None:
        worker = worker_for(
            store, {"fake1": fake(agreeing_script("A:1"))}, allow_unverified=True
        )
        roster = make_roster(3)
        roster.proposers[1] = fake_seat("proposer_2", "p2", verified=False)
        job_id = worker.submit(make_query(), [make_task()], roster)
        assert len(calls_of(store, job_id)) == 3

    def test_a_non_independent_roster_is_refused_at_submit(self, store: Any) -> None:
        worker = worker_for(store, {"fake1": fake(agreeing_script("A:1"))})
        roster = make_roster(3)
        # Mutating a built Roster skips pydantic's validator; submit re-runs it.
        roster.proposers[1] = fake_seat("proposer_2", "p1")
        with pytest.raises(RosterError, match="independent"):
            worker.submit(make_query(), [make_task()], roster)

    def test_a_refusing_cli_billing_preflight_stops_the_job(self, store: Any) -> None:
        class RefusingCli(InstrumentedBackend):
            async def preflight(self) -> Dict[str, Any]:
                return {"state": "refused", "detail": "would bill the API, not the plan"}

        backend = RefusingCli(scripted(agreeing_script("A:1")), kind="claude_cli")
        worker = worker_for(store, {"cli": backend})
        roster = Roster(
            proposers=[
                fake_seat("proposer_1", "p1"),
                SeatSpec(
                    seat_name="proposer_2",
                    endpoint="cli",
                    endpoint_kind="claude_cli",
                    model="sonnet",
                    canonical_model="anthropic/claude-sonnet-5",
                ),
            ],
            critic=fake_seat("critic", "critic-model"),
            meta=fake_seat("meta", "meta-model"),
        )
        with pytest.raises(BillingGuardRefused, match="would bill the API"):
            worker.submit(make_query(), [make_task()], roster)
        assert store.list_jobs(user="alice") == []

    def test_an_unknown_cli_billing_state_stops_the_job_unless_allowed(
        self, store: Any
    ) -> None:
        class UnsureCli(InstrumentedBackend):
            async def preflight(self) -> Dict[str, Any]:
                return {"state": "unknown", "detail": "no login file"}

        backend = UnsureCli(scripted(agreeing_script("A:1")), kind="codex_cli")
        roster = Roster(
            proposers=[
                fake_seat("proposer_1", "p1"),
                SeatSpec(
                    seat_name="proposer_2",
                    endpoint="cli",
                    endpoint_kind="codex_cli",
                    model="gpt",
                    canonical_model="openai/gpt-5.5",
                ),
            ],
            critic=fake_seat("critic", "critic-model"),
            meta=fake_seat("meta", "meta-model"),
        )
        with pytest.raises(BillingGuardRefused, match="could not confirm"):
            worker_for(store, {"cli": backend}).submit(
                make_query(), [make_task()], roster
            )
        job_id = worker_for(
            store, {"cli": backend}, allow_unverified=True
        ).submit(make_query(), [make_task()], roster)
        assert len(calls_of(store, job_id)) == 2


class TestWorkToVerdict:
    """The happy paths, end to end over a real store."""

    def test_a_unanimous_item_is_decided_without_a_meta_call(self, store: Any) -> None:
        backend = fake(agreeing_script("A:1"))
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        summary = run(worker.work(job_id=job_id, until_idle=True))

        assert summary["idle"] is True
        verdict = verdict_of(store, job_id)
        assert verdict.status == "ok"
        assert verdict.selected_ids == ["A:1"]
        assert verdict.decided_by == "unanimous"
        assert verdict.meta_skipped is True
        assert [row["role"] for row in calls_of(store, job_id)] == [
            "proposer",
            "proposer",
            "proposer",
            "critic",
        ]
        assert [req.role for req in backend.calls].count("meta") == 0
        assert store.get_job(job_id)["status"] == "done"
        assert item_of(store, job_id)["status"] == "ok"

    def test_a_split_vote_runs_the_meta_round(self, store: Any) -> None:
        backend = fake(split_script())
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        run(worker.work(job_id=job_id, until_idle=True))

        verdict = verdict_of(store, job_id)
        assert verdict.decided_by == "meta"
        assert verdict.votes == {"A:1": 3, "B:2": 2}
        assert verdict.selected_ids == ["A:1"]
        roles = [row["role"] for row in calls_of(store, job_id)]
        assert roles == ["proposer"] * 3 + ["critic", "meta"]
        assert store.get_job(job_id)["status"] == "done"

    def test_the_critic_and_meta_prompts_carry_the_vote_table(self, store: Any) -> None:
        backend = fake(split_script())
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        run(worker.work(job_id=job_id, until_idle=True))

        critic_request = next(req for req in backend.calls if req.role == "critic")
        assert "A:1: 3" in critic_request.user
        assert "B:2: 2" in critic_request.user
        assert "proposer_1 [high]" in critic_request.user
        meta_request = next(req for req in backend.calls if req.role == "meta")
        assert "A:1: 3" in meta_request.user
        # The critic's own audit is shown to meta, as JSON.
        assert '"verdict": "consistent"' in meta_request.user or (
            '"verdict":"consistent"' in meta_request.user
        )

    def test_the_job_becomes_running_on_the_first_claim(self, store: Any) -> None:
        backend = fake(agreeing_script("A:1"), delay=0.05)
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))

        async def drive() -> str:
            task = asyncio.ensure_future(worker.work(job_id=job_id, until_idle=True))
            seen = "queued"
            for _ in range(200):
                await asyncio.sleep(0.01)
                seen = store.get_job(job_id)["status"]
                if seen == "running":
                    break
            await task
            return seen

        assert run(drive()) == "running"
        assert store.get_job(job_id)["status"] == "done"

    def test_a_foreground_worker_registers_and_heartbeats(self, store: Any) -> None:
        worker = worker_for(store, {"fake1": fake(agreeing_script("A:1"))})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        run(worker.work(job_id=job_id, until_idle=True))

        workers = store.live_workers("alice")
        assert [row["worker_id"] for row in workers] == ["w-1"]
        row = workers[0]
        assert row["kind"] == "foreground"
        assert row["endpoints"] == ["fake1"]
        assert row["heartbeat_at"] is not None
        first = row["heartbeat_at"]

        beats = []
        run(
            worker.work(
                job_id=job_id, until_idle=True, heartbeat=lambda: beats.append(1)
            )
        )
        assert beats, "the heartbeat callback was never invoked"
        assert store.live_workers("alice")[0]["heartbeat_at"] >= first

    def test_evaluate_decides_one_item_in_the_foreground(self, store: Any) -> None:
        backend = fake(agreeing_script("A:1"))
        verdict = evaluate(
            make_task(),
            make_query(),
            make_roster(3),
            store,
            {"fake1": backend},
            user="alice",
            config=endpoint_config(fake1={}),
            **WORKER_DEFAULTS,
        )
        assert verdict.selected_ids == ["A:1"]
        assert verdict.decided_by == "unanimous"
        assert len(store.list_jobs(user="alice")) == 1


class TestReuseAndSingleFlight:
    """The two ways the worker avoids paying twice for one question."""

    def test_a_second_job_asking_the_same_question_makes_no_calls(
        self, store: Any
    ) -> None:
        backend = fake(agreeing_script("A:1"))
        worker = worker_for(store, {"fake1": backend})
        query, roster = make_query(), make_roster(3)

        first = worker.submit(query, [make_task()], roster, label="first")
        run(worker.work(job_id=first, until_idle=True))
        spent = len(backend.calls)
        assert spent == 4  # three proposers and a critic

        second = worker.submit(query, [make_task()], roster, label="second")
        run(worker.work(job_id=second, until_idle=True))

        assert len(backend.calls) == spent, "the second job asked a model something"
        assert verdict_of(store, second).selected_ids == ["A:1"]
        rows = calls_of(store, second)
        assert len(rows) == 4
        assert all(row["reused_from"] for row in rows)
        assert all(row["status"] == "ok" for row in rows)
        assert all(
            row["fresh_usage"] == {"prompt_tokens": 0, "completion_tokens": 0}
            for row in rows
        )
        # The reuse is recorded in the verdict too.
        assert all(seat["reused"] for seat in verdict_of(store, second).usage.values())

    def test_two_items_with_identical_prompts_make_one_call_per_seat(
        self, store: Any
    ) -> None:
        """Single-flight, not store reuse: nothing has been answered yet.

        Both items' calls are claimed in one batch and the backend holds each
        answer for 50 ms, so every follower is waiting on a leader that has
        not completed -- there is no ``ok`` row for ``find_reusable`` to find.
        """
        backend = fake(agreeing_script("A:1"), delay=0.05)
        worker = worker_for(store, {"fake1": backend})
        tasks = [make_task("item-1"), make_task("item-2")]
        job_id = worker.submit(make_query(), tasks, make_roster(3))
        assert len(calls_of(store, job_id)) == 6
        hashes = {row["prompt_hash"] for row in calls_of(store, job_id)}
        assert len(hashes) == 3, "the two items must ask identical questions"

        run(worker.work(job_id=job_id, until_idle=True))

        proposer_requests = [req for req in backend.calls if req.role == "proposer"]
        assert len(proposer_requests) == 3
        assert backend.monitor.peak <= 3
        for item_id in ("item-1", "item-2"):
            assert verdict_of(store, item_id=item_id, job_id=job_id).selected_ids == [
                "A:1"
            ]
        # Four calls were paid for in total: three proposer seats and one
        # critic.  The second item's critic asks an identical question too --
        # same votes, same ballots -- so it reuses the first item's answer.
        assert len(backend.calls) == 4
        reused = [row for row in calls_of(store, job_id) if row["reused_from"]]
        assert len(reused) == 4


class TestFailuresAndRetries:
    """Transport retries, terminal failures, and the sibling rule."""

    def test_a_transport_error_is_retried_and_then_succeeds(self, store: Any) -> None:
        script = {
            ("proposer", "proposer_1"): [
                TransportError("the socket went away"),
                proposer_answer("A:1"),
            ],
            "proposer": proposer_answer("A:1"),
            "critic": critic_answer(),
        }
        backend = fake(script)
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        run(worker.work(job_id=job_id, until_idle=True))

        verdict = verdict_of(store, job_id)
        assert verdict.status == "ok"
        assert verdict.selected_ids == ["A:1"]
        row = next(
            r for r in calls_of(store, job_id) if r["seat_name"] == "proposer_1"
        )
        assert row["status"] == "ok"
        assert row["attempt_count"] == 1, "exactly one failed attempt was charged"
        asked = [
            req for req in backend.calls if req.seat.seat_name == "proposer_1"
        ]
        assert len(asked) == 2

    def test_three_transport_failures_fail_the_call_and_the_item(
        self, store: Any
    ) -> None:
        script = {
            ("proposer", "proposer_1"): TransportError("still down"),
            "proposer": proposer_answer("A:1"),
            "critic": critic_answer(),
        }
        backend = fake(script)
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        run(worker.work(job_id=job_id, until_idle=True))

        verdict = verdict_of(store, job_id)
        assert verdict.status == "error"
        assert [e["kind"] for e in verdict.errors] == ["transport"]
        row = next(
            r for r in calls_of(store, job_id) if r["seat_name"] == "proposer_1"
        )
        assert row["status"] == "error"
        assert row["error"]["kind"] == "transport"
        assert row["attempt_count"] == 2  # two releases, then a terminal failure
        assert (
            len([req for req in backend.calls if req.seat.seat_name == "proposer_1"])
            == 3
        )
        # No critic was ever created for a council that lost a seat.
        assert calls_of(store, job_id, "critic") == []

    def test_an_answer_that_fails_the_schema_fails_its_seat(self, store: Any) -> None:
        script = {
            ("proposer", "proposer_1"): {"picked": "A:1"},  # not an array
            "proposer": proposer_answer("A:1"),
            "critic": critic_answer(),
        }
        worker = worker_for(store, {"fake1": fake(script)})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        run(worker.work(job_id=job_id, until_idle=True))

        verdict = verdict_of(store, job_id)
        assert verdict.status == "error"
        assert [e["kind"] for e in verdict.errors] == ["invalid_output"]
        assert calls_of(store, job_id, "critic") == [], "a critic call was created"
        assert calls_of(store, job_id, "meta") == []
        row = next(
            r for r in calls_of(store, job_id) if r["seat_name"] == "proposer_1"
        )
        assert row["status"] == "error"
        assert "answer schema" in row["error"]["message"]

    def test_a_truncated_answer_fails_its_seat(self, store: Any) -> None:
        class Truncating(InstrumentedBackend):
            async def complete(self, req: Any) -> Any:
                result = await super().complete(req)
                if req.seat.seat_name == "proposer_2" and result.ok:
                    return result.model_copy(update={"truncated": True})
                return result

        backend = Truncating(scripted(agreeing_script("A:1")))
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        run(worker.work(job_id=job_id, until_idle=True))

        verdict = verdict_of(store, job_id)
        assert verdict.status == "error"
        assert [(e["seat"], e["kind"]) for e in verdict.errors] == [
            ("proposer_2", "invalid_output")
        ]

    def test_a_failed_seat_waits_for_its_slow_sibling(self, store: Any) -> None:
        """The error verdict is written only once every seat is terminal."""
        script = {
            ("proposer", "proposer_1"): InvalidOutput("nonsense"),
            "proposer": proposer_answer("A:1"),
            "critic": critic_answer(),
        }
        worker = worker_for(store, {"fake1": fake(script)})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))

        # All three seats are claimed, as one in-flight batch.  Two of them
        # settle -- the failing proposer_1 and one sibling -- and the third
        # stays claimed, which is exactly what a slow seat looks like.
        claimed = {row["seat_name"]: row for row in store.claim_calls("w-1", {"fake1"}, 3
        )}
        assert sorted(claimed) == ["proposer_1", "proposer_2", "proposer_3"]
        run(worker.execute(claimed["proposer_1"]))
        run(worker.execute(claimed["proposer_2"]))

        assert item_of(store, job_id)["status"] == "pending"
        assert worker.advance(job_id, "item-1") == "waiting"
        assert calls_of(store, job_id, "critic") == []
        slow_row = next(
            r for r in calls_of(store, job_id) if r["seat_name"] == "proposer_3"
        )
        assert slow_row["status"] == "claimed"

        run(worker.execute(claimed["proposer_3"]))

        verdict = verdict_of(store, job_id)
        assert verdict.status == "error"
        # Both the failure and the sibling that finished later are recorded.
        assert [(b.seat, b.status) for b in verdict.ballots] == [
            ("proposer_1", "error"),
            ("proposer_2", "ok"),
            ("proposer_3", "ok"),
        ]
        assert verdict.votes == {"A:1": 2}


class TestBackoff:
    """The shipped backoff values, independent of the fast test timings."""

    def test_the_default_backoff_is_two_then_eight_seconds(self, store: Any) -> None:
        worker = CouncilWorker(store, None, {}, "alice", "w-1")
        assert worker.retry_backoff_s == (2.0, 8.0)
        assert worker.max_attempts == 3
        # +-25% jitter, and the last value repeats for any further attempt.
        assert 1.5 <= worker._backoff(1) <= 2.5
        assert 6.0 <= worker._backoff(2) <= 10.0
        assert 6.0 <= worker._backoff(3) <= 10.0

    def test_jitter_can_be_switched_off(self, store: Any) -> None:
        worker = CouncilWorker(
            store, None, {}, "alice", "w-1", retry_backoff_s=(3.0,), retry_jitter=0.0
        )
        assert worker._backoff(1) == 3.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def blockers_of(store: Any, job_id: str) -> list:
    return store.job_status(job_id)["blockers"]


def blocker_kinds(store: Any, job_id: str) -> set:
    return {b["kind"] for b in blockers_of(store, job_id)}


class TestHaltsPauseRatherThanFail:
    """A shut endpoint pauses a job; it does not fail its items."""

    @pytest.mark.parametrize(
        "failure, kind",
        [
            (QuotaExhausted("over quota for today"), "quota_exhausted"),
            (AuthError("the token expired"), "auth"),
            (BillingGuardRefused("wrong billing mode"), "billing_guard"),
        ],
    )
    def test_a_halting_failure_stops_the_endpoint_and_keeps_the_call(
        self, store: Any, failure: Any, kind: str
    ) -> None:
        script = {
            ("proposer", "proposer_1"): [failure, proposer_answer("A:1")],
            "proposer": proposer_answer("A:1"),
            "critic": critic_answer(),
        }
        backend = fake(script)
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        summary = run(worker.work(job_id=job_id, until_idle=True))

        assert store.get_endpoint_state("fake1")["halt_kind"] == kind
        row = next(
            r for r in calls_of(store, job_id) if r["seat_name"] == "proposer_1"
        )
        assert row["status"] == "pending", "the call was failed instead of paused"
        assert row["error"]["kind"] == kind
        assert row["attempt_count"] == 0, "a halt charged the call an attempt"
        assert row["claim_token"] is None

        # The item is NOT in error: nothing is wrong with it.
        assert item_of(store, job_id)["status"] == "pending"
        assert "halted" in blocker_kinds(store, job_id)
        assert any(b["kind"] == "halted" for b in summary["blockers"])

        # Clearing the halt and running again finishes the job, with no
        # re-submission of anything.
        store.reset_halt("fake1")
        run(worker.work(job_id=job_id, until_idle=True))
        assert store.get_job(job_id)["status"] == "done"
        assert verdict_of(store, job_id).selected_ids == ["A:1"]
        assert blockers_of(store, job_id) == []

    def test_a_halted_endpoint_is_not_claimed_from(self, store: Any) -> None:
        backend = fake(agreeing_script("A:1"))
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        store.set_halt("fake1", "quota_exhausted", "manually halted")

        summary = run(worker.work(job_id=job_id, until_idle=True))
        assert backend.calls == []
        assert summary["claimed"] == 0
        assert [b["kind"] for b in summary["blockers"]] == ["halted"]


class TestBudgets:
    """The soft token budget, and what happens when it cannot be measured."""

    def test_passing_the_budget_halts_the_endpoint_and_reports_the_overshoot(
        self, store: Any
    ) -> None:
        backend = InstrumentedBackend(
            scripted(agreeing_script("A:1")),
            usage={"prompt_tokens": 4, "completion_tokens": 4},
        )
        worker = worker_for(
            store,
            {"fake1": backend},
            config=endpoint_config(fake1={"max_total_tokens": 5}),
        )
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        run(worker.work(job_id=job_id, until_idle=True))

        state = store.get_endpoint_state("fake1")
        assert state["halt_kind"] == "budget_exhausted"
        assert "overshoot" in state["halt_message"]
        # One call of 8 tokens against a 5-token budget: the threshold is
        # soft, so the overshoot is real and is reported.
        assert state["epoch_tokens"] >= 8
        assert f"overshoot {state['epoch_tokens'] - 5}" in state["halt_message"]
        assert "halted" in blocker_kinds(store, job_id)
        assert item_of(store, job_id)["status"] == "pending"

    def test_reset_budget_resumes_the_job_and_keeps_the_lifetime_total(
        self, store: Any
    ) -> None:
        backend = InstrumentedBackend(
            scripted(agreeing_script("A:1")),
            usage={"prompt_tokens": 4, "completion_tokens": 4},
        )
        worker = worker_for(
            store,
            {"fake1": backend},
            config=endpoint_config(fake1={"max_total_tokens": 5}),
        )
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        run(worker.work(job_id=job_id, until_idle=True))
        spent = store.get_endpoint_state("fake1")["epoch_tokens"]
        assert spent

        state = store.reset_budget("fake1")
        assert state["halt_kind"] is None
        assert state["epoch_tokens"] == 0
        assert state["lifetime_tokens"] >= spent
        assert state["budget_epoch"] == 1

        run(worker.work(job_id=job_id, until_idle=True))
        # The new epoch is spent again at once, which is the budget working.
        assert store.get_endpoint_state("fake1")["lifetime_tokens"] > spent
        assert store.get_items(job_id)[0]["status"] in ("ok", "pending")

    def test_an_unmeasurable_budget_halts_the_endpoint(self, store: Any) -> None:
        backend = InstrumentedBackend(
            scripted(agreeing_script("A:1")), drop_usage=True
        )
        worker = worker_for(
            store,
            {"fake1": backend},
            config=endpoint_config(fake1={"max_total_tokens": 1000}),
        )
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        run(worker.work(job_id=job_id, until_idle=True))

        state = store.get_endpoint_state("fake1")
        assert state["halt_kind"] == "budget_usage_unknown"
        assert "no usage" in state["halt_message"]
        # The answer that arrived is still stored -- it was paid for.
        answered = [r for r in calls_of(store, job_id) if r["status"] == "ok"]
        assert answered
        assert answered[0]["fresh_usage"] is None

    def test_an_unbudgeted_endpoint_is_never_halted_by_tokens(self, store: Any) -> None:
        backend = InstrumentedBackend(
            scripted(agreeing_script("A:1")),
            usage={"prompt_tokens": 10_000, "completion_tokens": 10_000},
        )
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        run(worker.work(job_id=job_id, until_idle=True))
        assert store.get_endpoint_state("fake1")["halt_kind"] is None
        assert store.get_job(job_id)["status"] == "done"
        assert store.get_endpoint_state("fake1")["lifetime_tokens"] == 80_000


class TestConcurrency:
    """Two endpoints, one account, one limit."""

    def test_endpoints_sharing_an_account_share_one_limit(self, store: Any) -> None:
        monitor = InFlight()
        first = InstrumentedBackend(
            scripted(agreeing_script("A:1")), delay=0.01, monitor=monitor
        )
        second = InstrumentedBackend(
            scripted(agreeing_script("A:1")), delay=0.01, monitor=monitor
        )
        config = endpoint_config(
            fake1={"account": "one-subscription", "max_concurrency": 3},
            fake2={"account": "one-subscription", "max_concurrency": 2},
        )
        worker = worker_for(store, {"fake1": first, "fake2": second}, config=config)
        roster = Roster(
            proposers=[
                fake_seat("proposer_1", "p1", endpoint="fake1"),
                fake_seat("proposer_2", "p2", endpoint="fake1"),
                fake_seat("proposer_3", "p3", endpoint="fake2"),
                fake_seat("proposer_4", "p4", endpoint="fake2"),
            ],
            critic=fake_seat("critic", "critic-model", endpoint="fake1"),
            meta=fake_seat("meta", "meta-model", endpoint="fake2"),
        )
        # Distinct evidence per item, so nothing is reused and every seat is
        # really asked: 3 items x 4 proposers.
        tasks = [make_task(f"item-{n}", text=f"protein {n}") for n in (1, 2, 3)]
        job_id = worker.submit(make_query(), tasks, roster)
        run(worker.work(job_id=job_id, until_idle=True))

        assert store.get_job(job_id)["status"] == "done"
        assert monitor.total == 15  # 12 proposer calls plus 3 critics
        assert monitor.peak <= 2, (
            "the shared account ran "
            f"{monitor.peak} calls at once against a limit of 2"
        )
        assert monitor.current == 0

    def test_the_shared_limit_is_the_smallest_declared(self, store: Any) -> None:
        config = endpoint_config(
            fake1={"account": "acct", "max_concurrency": 7},
            fake2={"account": "acct", "max_concurrency": 2},
            fake3={"max_concurrency": 5},
        )
        worker = worker_for(
            store,
            {"fake1": fake({}), "fake2": fake({}), "fake3": fake({})},
            config=config,
        )
        shared = worker._limit_by_endpoint["fake1"]
        assert shared is worker._limit_by_endpoint["fake2"]
        assert shared.capacity == 2
        assert worker._limit_by_endpoint["fake3"].capacity == 5
        # Free slots are the sum over the GROUPS, not over the endpoints.
        assert worker._free_slots() == 7


class TestBlockersTheWorkerWrites:
    """The two blockers that only exist because the worker records them.

    ``job_status`` reads both off ``calls.error`` on a PENDING row.  Nothing
    in the store contract can write that column, so these two tests are the
    proof that the worker does -- no test here touches the column.
    """

    def test_model_unavailable_leaves_the_call_pending_and_waiting(
        self, store: Any
    ) -> None:
        script = {
            ("proposer", "proposer_1"): ModelUnavailable("vllm has not loaded p1 yet"),
            "proposer": proposer_answer("A:1"),
            "critic": critic_answer(),
        }
        worker = worker_for(store, {"fake1": fake(script)})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        summary = run(worker.work(job_id=job_id, until_idle=True))

        row = next(
            r for r in calls_of(store, job_id) if r["seat_name"] == "proposer_1"
        )
        assert row["status"] == "pending"
        assert row["error"]["kind"] == "model_unavailable"
        assert row["attempt_count"] == 0, "waiting for a model is not an attempt"
        assert row["not_before"] is not None
        assert row["not_before"] > utcnow()

        blockers = blockers_of(store, job_id)
        waiting = [b for b in blockers if b["kind"] == "waiting_for_model"]
        assert waiting, f"job_status reported {blockers}"
        assert waiting[0]["endpoint"] == "fake1"
        assert any(b["kind"] == "waiting_for_model" for b in summary["blockers"])
        assert item_of(store, job_id)["status"] == "pending"
        assert store.get_job(job_id)["status"] == "running"

    def test_a_route_that_no_longer_matches_parks_the_call(self, store: Any) -> None:
        """A second worker whose config routes the seat elsewhere."""
        submitter = worker_for(store, {"fake1": fake(agreeing_script("A:1"))})
        job_id = submitter.submit(make_query(), [make_task()], make_roster(3))

        moved = worker_for(
            store,
            {"fake1": fake(agreeing_script("A:1"))},
            worker_id="w-2",
            config=endpoint_config(fake1={"account": "somebody-elses-account"}),
        )
        summary = run(moved.work(job_id=job_id, until_idle=True))

        rows = calls_of(store, job_id)
        assert all(row["status"] == "pending" for row in rows)
        assert all(row["error"]["kind"] == "configuration_mismatch" for row in rows)
        assert all(row["attempt_count"] == 0 for row in rows)
        assert all(row["not_before"] > utcnow() + timedelta(minutes=4) for row in rows)

        blockers = blockers_of(store, job_id)
        mismatched = [b for b in blockers if b["kind"] == "configuration_mismatch"]
        assert mismatched, f"job_status reported {blockers}"
        assert mismatched[0]["endpoint"] == "fake1"
        assert any(
            b["kind"] == "configuration_mismatch" for b in summary["blockers"]
        )
        # Nothing was asked of any model, and no item failed.
        assert item_of(store, job_id)["status"] == "pending"

    def test_the_submitting_worker_still_matches_its_own_routes(
        self, store: Any
    ) -> None:
        worker = worker_for(store, {"fake1": fake(agreeing_script("A:1"))})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        run(worker.work(job_id=job_id, until_idle=True))
        assert blockers_of(store, job_id) == []
        assert verdict_of(store, job_id).status == "ok"


class TestASecondWorkerReadsTheQueryTypeBack:
    """A worker that did not submit the job renders from the STORE's spec."""

    def test_a_second_worker_finishes_a_job_it_did_not_submit(
        self, store: Any
    ) -> None:
        from kbutillib.domains.ai.council.queries import QueryRegistry

        query = make_query()
        QueryRegistry(store).register(query, created_by="test")
        submitter = worker_for(store, {"fake1": fake(agreeing_script("A:1"))})
        job_id = submitter.submit(query, [make_task()], make_roster(3))

        # A brand-new worker, with no memory of the query type at all.
        second = worker_for(
            store, {"fake1": fake(agreeing_script("A:1"))}, worker_id="w-2"
        )
        assert second._queries == {}
        run(second.work(job_id=job_id, until_idle=True))

        assert verdict_of(store, job_id).selected_ids == ["A:1"]
        assert verdict_of(store, job_id).query == {"name": "engine_test", "version": "1"}


# ---------------------------------------------------------------------------
# The real builtin query type, end to end
# ---------------------------------------------------------------------------

#: A plausible GO menu, deliberately NOT in id order: the verdict must come
#: back in MENU order, and an id-sorted menu would hide a bug that sorted.
GO_MENU = [
    MenuEntry(
        id="GO:0004565",
        label="beta-galactosidase activity",
        description="Catalysis of the hydrolysis of terminal non-reducing "
        "beta-D-galactose residues in beta-D-galactosides.",
        rank=1,
        score=0.91,
    ),
    MenuEntry(
        id="GO:0016787",
        label="hydrolase activity",
        description="Catalysis of the hydrolysis of various bonds.",
        rank=2,
        score=0.62,
    ),
    MenuEntry(
        id="GO:0030246",
        label="carbohydrate binding",
        description="Binding to a carbohydrate.",
        rank=3,
        score=0.41,
    ),
]


def go_proposer_answer(*go_ids: str) -> Dict[str, Any]:
    """A reply in the BUILTIN's own vocabulary: ``go_id``, not ``id``."""
    return {
        "picked": [
            {
                "go_id": go_id,
                "confidence": "high",
                "why": "the annotation names the activity",
            }
            for go_id in go_ids
        ],
        "overall_confidence": "high",
        "no_functional_content": False,
        "notes": "",
    }


class TestTheBuiltinGoCouncil:
    """One item through the SHIPPED go_mf query type, read back from a store.

    No in-test query type, no monkeypatch, no injected renderer: the spec is
    seeded with ``QueryRegistry.seed_builtins()``, read back with
    ``get("go_mf")``, and what the backend receives is asserted to be the
    builtin's own prompt text.
    """

    def test_a_go_item_is_decided_with_the_builtin_prompts(self, store: Any) -> None:
        from kbutillib.domains.ai.council.queries import QueryRegistry

        registry = QueryRegistry(store)
        registry.seed_builtins()
        go = registry.get("go_mf")
        assert go.name == "go_mf"
        assert go.renderer == "go_mf_v3@1"
        assert go.id_field == "go_id"
        assert go.entity_field == "text"

        # The council agrees on the specific activity; the critic approves.
        script = {
            "proposer": go_proposer_answer("GO:0004565"),
            "critic": critic_answer(verdict="strongly_consistent"),
            "meta": {
                "final_go_ids": ["GO:0004565"],
                "final_confidence": "high",
                "no_functional_content": False,
                "reasoning": "not reached",
            },
        }
        backend = fake(script)
        worker = worker_for(store, {"fake1": backend})
        task = CouncilTask(
            item_id="prot-1",
            evidence={
                "text": "beta-galactosidase (EC 3.2.1.23)",
                "source": "prokka",
            },
            menu=GO_MENU,
        )
        job_id = worker.submit(go, [task], make_roster(3), label="go batch")
        run(worker.work(job_id=job_id, until_idle=True))

        # -- the verdict ---------------------------------------------------
        verdict = verdict_of(store, job_id, item_id="prot-1")
        assert verdict.status == "ok"
        assert verdict.selected_ids == ["GO:0004565"]
        assert verdict.votes == {"GO:0004565": 3}
        assert verdict.decided_by == "unanimous"
        assert verdict.meta_skipped is True
        assert verdict.query == {"name": "go_mf", "version": "1"}
        assert [row["role"] for row in calls_of(store, job_id)] == [
            "proposer",
            "proposer",
            "proposer",
            "critic",
        ]

        # -- the prompts the models actually received ----------------------
        proposer_request = next(req for req in backend.calls if req.role == "proposer")
        assert proposer_request.system == go.roles["proposer"].system
        assert proposer_request.system.startswith(
            "You assign Gene Ontology molecular-function terms"
        )
        assert 'Annotation string: "beta-galactosidase (EC 3.2.1.23)"' in (
            proposer_request.user
        )
        assert "Produced by: prokka" in proposer_request.user
        # The builtin renderer's candidate block: rank | id | score | name.
        assert (
            "   1 | GO:0004565 | 0.910 | beta-galactosidase activity"
            in proposer_request.user
        )
        assert proposer_request.json_schema == go.answer_schema("proposer")
        assert "go_id" in str(proposer_request.json_schema)

        critic_request = next(req for req in backend.calls if req.role == "critic")
        assert critic_request.system == go.roles["critic"].system
        assert "GO:0004565: 3" in critic_request.user
        # The critic's menu block drops the score and the definition.
        assert "   1 | GO:0004565 | beta-galactosidase activity" in critic_request.user

        # -- the domain vocabulary never reached the engine ----------------
        # The stored reply is the ORIGINAL, in the builtin's own field names;
        # the ballot is generic.
        answered = next(r for r in calls_of(store, job_id) if r["role"] == "proposer")
        assert answered["reply_parsed"]["picked"][0]["go_id"] == "GO:0004565"
        assert [pick.id for pick in verdict.ballots[0].picks] == ["GO:0004565"]

    def test_the_builtin_meta_round_reads_final_go_ids(self, store: Any) -> None:
        """The meta path, where the adapter's rename actually matters."""
        from kbutillib.domains.ai.council.queries import QueryRegistry

        registry = QueryRegistry(store)
        registry.seed_builtins()
        go = registry.get("go_mf")

        script = {
            ("proposer", "proposer_1"): go_proposer_answer(
                "GO:0004565", "GO:0030246"
            ),
            ("proposer", "proposer_2"): go_proposer_answer("GO:0004565"),
            ("proposer", "proposer_3"): go_proposer_answer("GO:0004565"),
            "critic": critic_answer(verdict="consistent"),
            "meta": {
                # Out of menu order, and naming an id that is not on the menu.
                "final_go_ids": ["GO:0030246", "GO:0004565", "GO:9999999"],
                "final_confidence": "medium",
                "no_functional_content": False,
                "reasoning": "both functions are supported",
            },
        }
        worker = worker_for(store, {"fake1": fake(script)})
        task = CouncilTask(
            item_id="prot-2",
            evidence={"text": "beta-galactosidase, lectin domain", "source": "prokka"},
            menu=GO_MENU,
        )
        job_id = worker.submit(go, [task], make_roster(3))
        run(worker.work(job_id=job_id, until_idle=True))

        verdict = verdict_of(store, job_id, item_id="prot-2")
        assert verdict.decided_by == "meta"
        # MENU order, not the order meta named them, and the off-menu id gone.
        assert verdict.selected_ids == ["GO:0004565", "GO:0030246"]
        assert verdict.final_confidence == "medium"
        off_menu = [e for e in verdict.errors if e["kind"] == "off_menu"]
        assert [(e["stage"], e["message"].split("'")[1]) for e in off_menu] == [
            ("meta", "GO:9999999")
        ]


class TestCancellation:
    """A cancelled job is terminal, and a reply that beats the cancel is kept."""

    def test_a_reply_arriving_after_a_cancel_is_stored_and_changes_nothing(
        self, store: Any
    ) -> None:
        backend = fake(agreeing_script("A:1"))
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))

        claimed = store.claim_calls("w-1", {"fake1"}, 1)
        assert len(claimed) == 1
        store.cancel_job(job_id)

        # The claimed call is already being paid for, so its reply is stored.
        assert run(worker.execute(claimed[0])) == "ok"

        row = next(
            r for r in calls_of(store, job_id) if r["call_id"] == claimed[0]["call_id"]
        )
        assert row["status"] == "ok"
        assert row["reply_parsed"]["picked"][0]["id"] == "A:1"
        # And it stays reusable for everyone else's future jobs.
        assert store.find_reusable(row["prompt_hash"])["call_id"] == row["call_id"]

        # Nothing else moved: no critic call, no verdict, job still cancelled.
        assert calls_of(store, job_id, "critic") == []
        assert store.get_job(job_id)["status"] == "cancelled"
        assert item_of(store, job_id)["status"] == "cancelled"
        assert item_of(store, job_id)["verdict"] is None
        superseded = [r for r in calls_of(store, job_id) if r["status"] == "superseded"]
        assert len(superseded) == 2
        assert all(row["live"] is False for row in superseded)

    def test_a_cancelled_job_is_not_claimed_from(self, store: Any) -> None:
        backend = fake(agreeing_script("A:1"))
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        store.cancel_job(job_id)

        summary = run(worker.work(job_id=job_id, until_idle=True))
        assert summary["claimed"] == 0
        assert backend.calls == []


class TestStaleClaims:
    """A reply may only be written by the worker that currently holds the call."""

    def test_completing_with_a_stale_token_is_refused_by_the_store(
        self, store: Any
    ) -> None:
        worker = worker_for(store, {"fake1": fake(agreeing_script("A:1"))})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        claimed = store.claim_calls("w-1", {"fake1"}, 1)[0]

        with pytest.raises(StaleClaim):
            store.complete_call(claimed["call_id"], "not-the-token", "ok")

    def test_the_worker_discards_a_reply_it_no_longer_owns(self, store: Any) -> None:
        backend = fake(agreeing_script("A:1"))
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        claimed = store.claim_calls("w-1", {"fake1"}, 1)[0]

        # The call is taken back from under this worker -- which is what
        # reclaim_stale does to a worker that looked dead.
        store.release_call(claimed["call_id"], claimed["claim_token"])

        assert run(worker.execute(claimed)) == "stale"
        row = next(
            r for r in calls_of(store, job_id) if r["call_id"] == claimed["call_id"]
        )
        assert row["status"] == "pending"
        assert row["reply_parsed"] is None
        assert row["reply_text"] is None
        assert item_of(store, job_id)["status"] == "pending"


class TestOneLiveCallPerSeatAndUpstream:
    """advance() is idempotent, and the store refuses a second live call."""

    def test_advancing_twice_creates_one_critic_call(self, store: Any) -> None:
        worker = worker_for(store, {"fake1": fake(split_script())})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        for row in store.claim_calls("w-1", {"fake1"}, 3):
            run(worker.execute(row))

        assert len(calls_of(store, job_id, "critic")) == 1
        for _ in range(3):
            assert worker.advance(job_id, "item-1") == "need_critic"
        assert len(calls_of(store, job_id, "critic")) == 1

    def test_the_store_refuses_a_duplicate_live_call(self, store: Any) -> None:
        import sqlalchemy as sa

        worker = worker_for(store, {"fake1": fake(split_script())})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        for row in store.claim_calls("w-1", {"fake1"}, 3):
            run(worker.execute(row))
        critic = calls_of(store, job_id, "critic")[0]

        duplicate = {
            key: critic[key]
            for key in (
                "job_id",
                "item_id",
                "role",
                "seat_name",
                "endpoint",
                "endpoint_kind",
                "upstream_digest",
                "prompt_hash",
            )
        }
        with pytest.raises(sa.exc.IntegrityError):
            store.add_calls([duplicate])


class TestPriority:
    """A later, higher-priority job is served first."""

    def test_a_high_priority_job_is_claimed_before_an_earlier_one(
        self, store: Any
    ) -> None:
        backend = fake(agreeing_script("A:1"))
        worker = worker_for(
            store,
            {"fake1": backend},
            config=endpoint_config(fake1={"max_concurrency": 1}),
        )
        query, roster = make_query(), make_roster(3)
        low = worker.submit(
            query,
            [make_task("lo-1", text="the low priority protein")],
            roster,
            label="low",
            priority=1,
        )
        high = worker.submit(
            query,
            [make_task("hi-1", text="the urgent protein")],
            roster,
            label="high",
            priority=500,
        )
        assert store.get_job(low)["submitted_at"] <= store.get_job(high)["submitted_at"]

        # No job scope: the worker takes whatever the queue offers next.
        run(worker.work(until_idle=True))

        order = [req.trace_id.split("|")[0] for req in backend.calls]
        assert set(order) == {"hi-1", "lo-1"}
        last_high = max(index for index, item in enumerate(order) if item == "hi-1")
        first_low = min(index for index, item in enumerate(order) if item == "lo-1")
        assert last_high < first_low, f"claim order was {order}"
        assert store.get_job(low)["status"] == "done"
        assert store.get_job(high)["status"] == "done"


class TestStaleUpstreamDigest:
    """A downstream answer whose upstream changed is ignored, not counted."""

    def test_a_critic_answer_is_dropped_when_a_proposer_reply_changes(
        self, store: Any
    ) -> None:
        import sqlalchemy as sa

        from kbutillib.domains.ai.council.store import schema as sch

        worker = worker_for(store, {"fake1": fake(split_script())})
        job_id = worker.submit(make_query(), [make_task()], make_roster(3))
        # Proposers, then the critic: a split vote, so the item needs meta and
        # is therefore NOT settled yet.
        for row in store.claim_calls("w-1", {"fake1"}, 3):
            run(worker.execute(row))
        critic_rows = store.claim_calls("w-1", {"fake1"}, 1)
        assert [row["role"] for row in critic_rows] == ["critic"]
        run(worker.execute(critic_rows[0]))

        critic = calls_of(store, job_id, "critic")[0]
        assert critic["status"] == "ok"
        old_digest = critic["upstream_digest"]
        assert len(calls_of(store, job_id, "meta")) == 1
        assert item_of(store, job_id)["status"] == "pending"

        # A proposer is reclaimed and answers differently -- so the table the
        # critic audited no longer exists.  Written directly because the
        # store has no "unsettle this call" verb; the point under test is what
        # the ENGINE does with the digest, not how the row got that way.
        target = next(
            r for r in calls_of(store, job_id) if r["seat_name"] == "proposer_3"
        )
        with store.engine.begin() as conn:
            conn.execute(
                sa.update(sch.calls)
                .where(sch.calls.c.call_id == target["call_id"])
                .values(reply_parsed=proposer_answer("C:3"))
            )

        assert worker.advance(job_id, "item-1") == "need_critic"

        critics = calls_of(store, job_id, "critic")
        assert len(critics) == 2, "the stale critic answer was reused"
        fresh = [row for row in critics if row["upstream_digest"] != old_digest]
        assert len(fresh) == 1
        assert fresh[0]["status"] == "pending"
        # Still undecided: decide() saw no critic for the CURRENT table.
        assert item_of(store, job_id)["status"] == "pending"


# ---------------------------------------------------------------------------
# Order independence
# ---------------------------------------------------------------------------
#
# The same script, the same items, three completely different orders of
# execution.  If any of the engine's state lived in the order calls happen to
# be worked in rather than in the store, this is what finds it.

#: Seven items covering the skip path, the meta path, a failed seat, the
#: deterministic empty menu and an off-menu pick.
ORDER_ANSWERS: Dict[str, Dict[Any, Any]] = {
    "skip-unanimous": {
        "proposer": proposer_answer("A:1"),
        "critic": critic_answer(),
    },
    "skip-contentless": {
        "proposer": proposer_answer(no_content=True),
        "critic": critic_answer(contentless=True),
    },
    "meta-split": {
        ("proposer", "proposer_1"): proposer_answer("A:1", "B:2"),
        ("proposer", "proposer_2"): proposer_answer("A:1", "B:2"),
        ("proposer", "proposer_3"): proposer_answer("A:1"),
        "critic": critic_answer(),
        "meta": meta_answer("B:2"),
    },
    "meta-critic-objects": {
        "proposer": proposer_answer("A:1"),
        "critic": critic_answer(verdict="ambiguous"),
        "meta": meta_answer("C:3", "A:1"),
    },
    "fail-seat": {
        ("proposer", "proposer_2"): InvalidOutput("the model answered in prose"),
        "proposer": proposer_answer("A:1"),
        "critic": critic_answer(),
    },
    "offmenu": {
        ("proposer", "proposer_1"): proposer_answer("A:1", "ZZ:9"),
        "proposer": proposer_answer("A:1"),
        "critic": critic_answer(),
    },
    # No answers at all: an empty menu is decided at submit.
    "empty-menu": {},
}

#: What every strategy must produce, item by item.  Asserted against, not
#: just compared across runs: three runs agreeing on the wrong answer is not
#: order independence, it is a consistent bug.
EXPECTED_VERDICTS: Dict[str, Dict[str, Any]] = {
    "skip-unanimous": {
        "status": "ok",
        "selected_ids": ["A:1"],
        "decline_reason": None,
        "decided_by": "unanimous",
        "votes": {"A:1": 3},
        "meta_skipped": True,
        "errors": [],
    },
    "skip-contentless": {
        "status": "ok",
        "selected_ids": [],
        "decline_reason": "contentless",
        "decided_by": "unanimous",
        "votes": {},
        "meta_skipped": True,
        "errors": [],
    },
    "meta-split": {
        "status": "ok",
        "selected_ids": ["B:2"],
        "decline_reason": None,
        "decided_by": "meta",
        "votes": {"A:1": 3, "B:2": 2},
        "meta_skipped": False,
        "errors": [],
    },
    "meta-critic-objects": {
        "status": "ok",
        "selected_ids": ["C:3", "A:1"],
        "decline_reason": None,
        "decided_by": "meta",
        "votes": {"A:1": 3},
        "meta_skipped": False,
        "errors": [],
    },
    "fail-seat": {
        "status": "error",
        "selected_ids": [],
        "decline_reason": None,
        "decided_by": None,
        "votes": {"A:1": 2},
        "meta_skipped": True,
        "errors": [("proposer_2", "invalid_output")],
    },
    "offmenu": {
        "status": "ok",
        "selected_ids": ["A:1"],
        "decline_reason": None,
        "decided_by": "unanimous",
        "votes": {"A:1": 3},
        "meta_skipped": True,
        "errors": [("proposer_1", "off_menu")],
    },
    "empty-menu": {
        "status": "ok",
        "selected_ids": [],
        "decline_reason": "no_candidates",
        "decided_by": "deterministic_empty",
        "votes": {},
        "meta_skipped": True,
        "errors": [],
    },
}


def order_tasks() -> list:
    """The seven items, each with its own evidence so nothing is reused."""
    tasks = []
    for item_id in ORDER_ANSWERS:
        entries = [] if item_id == "empty-menu" else menu()
        tasks.append(make_task(item_id, text=f"protein for {item_id}", menu=entries))
    return tasks


def order_script(request: Any) -> Any:
    """Answer by (item, role, seat), from :data:`ORDER_ANSWERS`."""
    item_id, role, seat = request.trace_id.split("|")
    table = ORDER_ANSWERS[item_id]
    answer = table.get((role, seat), table.get(role))
    if answer is None:
        raise AssertionError(f"no scripted answer for {request.trace_id}")
    return answer


def verdict_summary(store: Any, job_id: str) -> Dict[str, Dict[str, Any]]:
    """The facts a verdict must agree on whatever order it was reached in."""
    summary: Dict[str, Dict[str, Any]] = {}
    for row in store.get_items(job_id):
        assert row["status"] in ("ok", "error"), (
            f"item {row['item_id']} is {row['status']}"
        )
        verdict = CouncilVerdict.model_validate(row["verdict"])
        summary[row["item_id"]] = {
            "status": verdict.status,
            "selected_ids": verdict.selected_ids,
            "decline_reason": verdict.decline_reason,
            "decided_by": verdict.decided_by,
            "votes": verdict.votes,
            "meta_skipped": verdict.meta_skipped,
            "errors": sorted((e["seat"], e["kind"]) for e in verdict.errors),
        }
    return summary


def split_roster() -> Roster:
    """A roster whose seats straddle two endpoints."""
    return Roster(
        proposers=[
            fake_seat("proposer_1", "p1", endpoint="fake1"),
            fake_seat("proposer_2", "p2", endpoint="fake1"),
            fake_seat("proposer_3", "p3", endpoint="fake2"),
        ],
        critic=fake_seat("critic", "critic-model", endpoint="fake2"),
        meta=fake_seat("meta", "meta-model", endpoint="fake1"),
    )


class TestOrderIndependence:
    """One script, seven items, three orders of execution, one answer."""

    def _prepare(self, make_store: Any, name: str) -> tuple:
        from kbutillib.domains.ai.council.queries import QueryRegistry

        store = make_store(name)
        query = make_query()
        # Registered so that a worker which did not submit the job can render
        # it -- strategy B uses two of those.
        QueryRegistry(store).register(query, created_by="test")
        return store, query

    def test_one_call_at_a_time_in_queue_order(self, make_store: Any) -> None:
        store, query = self._prepare(make_store, "serial")
        backend = InstrumentedBackend(order_script)
        worker = worker_for(
            store,
            {"fake1": backend, "fake2": backend},
            config=endpoint_config(
                fake1={"account": "one", "max_concurrency": 1},
                fake2={"account": "one", "max_concurrency": 1},
            ),
        )
        job_id = worker.submit(query, order_tasks(), split_roster())
        run(worker.work(job_id=job_id, until_idle=True))

        assert backend.monitor.peak == 1
        assert verdict_summary(store, job_id) == EXPECTED_VERDICTS
        assert store.get_job(job_id)["status"] == "done"

    def test_one_endpoint_at_a_time(self, make_store: Any) -> None:
        """Seat by seat: each worker may only claim for its own endpoint."""
        store, query = self._prepare(make_store, "by-endpoint")
        first = InstrumentedBackend(order_script)
        second = InstrumentedBackend(order_script)
        workers = [
            (
                worker_for(store, {"fake1": first}, worker_id="w-fake1"),
                {"fake1"},
            ),
            (
                worker_for(store, {"fake2": second}, worker_id="w-fake2"),
                {"fake2"},
            ),
        ]
        job_id = workers[0][0].submit(query, order_tasks(), split_roster())

        for _ in range(400):
            progressed = False
            for worker, endpoints in workers:
                worker.register()
                rows = store.claim_calls(worker.worker_id, endpoints, 1)
                for row in rows:
                    run(worker.execute(row))
                    progressed = True
            if not progressed:
                break
        else:  # pragma: no cover - a runaway loop is a bug, not a slow test
            raise AssertionError("the job never drained")

        assert verdict_summary(store, job_id) == EXPECTED_VERDICTS

    def test_shuffled(self, make_store: Any) -> None:
        """Every claimable call at once, executed in a shuffled order."""
        import random as _random

        store, query = self._prepare(make_store, "shuffled")
        backend = InstrumentedBackend(order_script, delay=0.002)
        worker = worker_for(
            store,
            {"fake1": backend, "fake2": backend},
            config=endpoint_config(fake1={}, fake2={}),
        )
        job_id = worker.submit(query, order_tasks(), split_roster())
        worker.register()
        rng = _random.Random(20261015)

        async def drain() -> None:
            for _ in range(400):
                rows = store.claim_calls(worker.worker_id, {"fake1", "fake2"}, 50)
                if not rows:
                    return
                rng.shuffle(rows)
                await asyncio.gather(*(worker.execute(row) for row in rows))
            raise AssertionError("the job never drained")  # pragma: no cover

        run(drain())
        assert verdict_summary(store, job_id) == EXPECTED_VERDICTS

    def test_the_three_orders_agree(self, make_store: Any) -> None:
        """The comparison the other three tests make possible.

        Each strategy is asserted against ``EXPECTED_VERDICTS`` above, so
        this is the same fact stated as the task states it: the verdicts are
        identical whichever way the calls were worked.
        """
        store_a, query_a = self._prepare(make_store, "cmp-serial")
        backend_a = InstrumentedBackend(order_script)
        worker_a = worker_for(
            store_a,
            {"fake1": backend_a, "fake2": backend_a},
            config=endpoint_config(
                fake1={"account": "one", "max_concurrency": 1},
                fake2={"account": "one", "max_concurrency": 1},
            ),
        )
        job_a = worker_a.submit(query_a, order_tasks(), split_roster())
        run(worker_a.work(job_id=job_a, until_idle=True))

        store_b, query_b = self._prepare(make_store, "cmp-parallel")
        backend_b = InstrumentedBackend(order_script, delay=0.002)
        worker_b = worker_for(
            store_b,
            {"fake1": backend_b, "fake2": backend_b},
            config=endpoint_config(fake1={}, fake2={}),
        )
        job_b = worker_b.submit(query_b, order_tasks(), split_roster())
        run(worker_b.work(job_id=job_b, until_idle=True))

        assert verdict_summary(store_a, job_a) == verdict_summary(store_b, job_b)
        assert len(verdict_summary(store_a, job_a)) == 7


class TestAnswerValidation:
    """Both halves of the schema check, including the no-jsonschema fallback."""

    def test_a_good_answer_validates(self) -> None:
        schema = make_query().answer_schema("proposer")
        assert _validate_answer(proposer_answer("A:1"), schema) is None

    def test_a_missing_required_key_is_reported(self) -> None:
        schema = make_query().answer_schema("proposer")
        reply = proposer_answer("A:1")
        del reply["no_functional_content"]
        complaint = _validate_answer(reply, schema)
        assert complaint and "no_functional_content" in complaint

    def test_a_wrong_type_is_reported(self) -> None:
        schema = make_query().answer_schema("proposer")
        complaint = _validate_answer({**proposer_answer("A:1"), "picked": "A:1"}, schema)
        assert complaint and "schema" in complaint

    def test_a_non_object_answer_is_reported(self) -> None:
        complaint = _validate_answer(["A:1"], make_query().answer_schema("proposer"))
        assert complaint and "not a JSON object" in complaint

    def test_the_fallback_check_runs_without_jsonschema(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``import jsonschema`` raises when sys.modules holds None for it."""
        import sys

        monkeypatch.setitem(sys.modules, "jsonschema", None)
        with pytest.raises(ImportError):
            import jsonschema  # noqa: F401

        schema = make_query().answer_schema("proposer")
        assert _validate_answer(proposer_answer("A:1"), schema) is None

        reply = proposer_answer("A:1")
        del reply["picked"]
        assert "picked" in (_validate_answer(reply, schema) or "")

        wrong = {**proposer_answer("A:1"), "picked": "A:1"}
        assert "expected an array" in (_validate_answer(wrong, schema) or "")

        flag = {**proposer_answer("A:1"), "no_functional_content": "yes"}
        assert "expected a boolean" in (_validate_answer(flag, schema) or "")

        notes = {**proposer_answer("A:1"), "notes": 7}
        assert "expected a string" in (_validate_answer(notes, schema) or "")
