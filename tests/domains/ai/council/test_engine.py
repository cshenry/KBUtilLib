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

from typing import Any, Dict, Optional, Sequence

import pytest

from kbutillib.domains.ai.council.engine.decide import (
    APPROVING_VERDICTS,
    SettledCall,
    critic_approves,
    decide,
)
from kbutillib.domains.ai.council.types import CouncilError

from .engine_support import (
    critic_answer,
    make_query,
    make_roster,
    make_task,
    meta_answer,
    proposer_answer,
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
