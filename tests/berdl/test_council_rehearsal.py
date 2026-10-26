"""The acceptance rehearsal's OWN logic -- the three-state report and its refusals.

THESE TESTS NEED NO SNAPSHOT and belong in the default suite. The rehearsal
itself reads a 896 MB production snapshot and is driven by
``scripts/council/acceptance_rehearsal.py``; what is pinned here is the part
of it that decides WHAT A STEP'S OUTCOME MEANS, which is the part that could
silently turn a skipped check into a pass.

WHY THIS IS WORTH A TEST FILE. The whole value of the rehearsal is that a
step which could not check something says so. The single most damaging bug it
could have is a step with zero assertions reading as ``ASSERTED`` -- that is
exactly the "silently passes over something it could not check" failure the
target exists to prevent, and it would be invisible in a 500-line report. So
:meth:`Step.settle`'s rule is pinned here term by term, as is
:func:`require_scratch_target`, which is the only thing standing between a
one-way ``assign_store_identity`` and the production store file.
"""

from __future__ import annotations

import pytest

from kbutillib.domains.kbase.berdl.council_rehearsal import (
    ASSERTED,
    FAILED,
    NOT_ASSERTED,
    PRODUCTION_STORE_PATHS,
    REHEARSAL_STORE_ID,
    SCRATCH_PREFIXES,
    Rehearsal,
    RehearsalRefused,
    Step,
    in_pod_leg,
    render_report,
    require_scratch_target,
)


class TestStepSettles:
    """A step's state is DERIVED from its assertions, never assumed."""

    def test_a_step_with_no_assertions_is_never_asserted(self) -> None:
        # THE ONE THAT MATTERS. A step that checked nothing established
        # nothing, and reading that as a pass is the failure the whole report
        # shape exists to prevent.
        step = Step(number="5", name="produce the bundle")
        assert step.settle() == NOT_ASSERTED
        assert step.reason, "an unassertable step must carry its reason"

    def test_every_assertion_true_is_asserted(self) -> None:
        step = Step(number="3", name="direct query")
        step.check("21 jobs", 21, 21)
        step.check("203,821 calls", 203821, 203821)
        assert step.settle() == ASSERTED
        assert step.reason == ""

    def test_one_false_assertion_fails_the_step(self) -> None:
        step = Step(number="3", name="direct query")
        step.check("21 jobs", 21, 21)
        step.check("203,821 calls", 203821, 88465)
        assert step.settle() == FAILED
        # The FAILING claim is named, not just counted: a report that said
        # "1 assertion failed" would send a human back to the log.
        assert "203,821 calls" in step.reason

    def test_an_explicit_not_asserted_survives_later_assertions(self) -> None:
        # A step that declared itself unassertable cannot be talked out of it
        # by an assertion that happened to hold anyway.
        step = Step(number="6", name="publish")
        step.not_asserted("run directory absent")
        step.check("something incidental", 1, 1)
        assert step.settle() == NOT_ASSERTED
        assert step.reason == "run directory absent"

    def test_an_explicit_failure_survives_passing_assertions(self) -> None:
        step = Step(number="2", name="delivery")
        step.check("the manifest is v1", 1, 1)
        step.fail("SnapshotConflictError: ...")
        assert step.settle() == FAILED

    def test_check_compares_by_equality_and_records_both_values(self) -> None:
        step = Step(number="1", name="bootstrap")
        held = step.check("counts match", {"ok": 2}, {"ok": 2})
        assert held.ok is True
        assert held.expected == {"ok": 2} and held.measured == {"ok": 2}
        # expected AND measured are carried even when it holds: the report is
        # a measurement of the snapshot, not only a pass/fail list.
        assert step.as_dict()["assertions"][0]["measured"] == {"ok": 2}

    def test_check_honours_an_explicit_ok_override(self) -> None:
        step = Step(number="3", name="direct query")
        # Used where the claim is an inequality -- "the total is NOT the live
        # subset" -- which equality cannot express.
        assert step.check("total != live", True, 203821 != 88465, ok=True).ok
        assert not step.check("total != live", True, False, ok=False).ok

    def test_findings_and_notes_are_carried_separately(self) -> None:
        step = Step(number="3", name="direct query")
        step.check("fine", 1, 1)
        step.find("reply_parsed stores None as the text 'null'")
        step.note("measured on the snapshot directly")
        payload = step.as_dict()
        # A FINDING is a defect for a human to decide about and must not be
        # mistaken for a passing assertion or buried in a note.
        assert payload["findings"] == [
            "reply_parsed stores None as the text 'null'"
        ]
        assert payload["notes"] == ["measured on the snapshot directly"]
        assert payload["state"] == ASSERTED, "a finding does not fail a step"


class TestScratchRefusals:
    """``assign_store_identity`` is one-way, so the target is checked first."""

    @pytest.mark.parametrize("path", PRODUCTION_STORE_PATHS)
    def test_the_production_and_shared_paths_are_refused_by_name(
        self, path: str
    ) -> None:
        with pytest.raises(RehearsalRefused, match="production or shared"):
            require_scratch_target(path, what="store copy")

    def test_a_path_outside_scratch_is_refused(self, tmp_path) -> None:
        # tmp_path is under /tmp on most hosts, which IS an accepted prefix,
        # so an unambiguously outside path is used instead.
        with pytest.raises(RehearsalRefused, match="not under"):
            require_scratch_target("/home/someone/council.sqlite", what="work root")

    def test_a_scratch_path_is_accepted_and_returned_resolved(self) -> None:
        target = require_scratch_target("/scratch/chenry/t11/x.sqlite", what="sink")
        assert str(target) == "/scratch/chenry/t11/x.sqlite"

    def test_the_rehearsal_refuses_a_non_scratch_work_root(self) -> None:
        with pytest.raises(RehearsalRefused):
            Rehearsal(snapshot="/scratch/x.sqlite", work_root="/home/chenry/t11")

    def test_the_declared_store_id_is_not_the_reserved_production_one(self) -> None:
        # 'poplar/council-1' is reserved and its assignment is Chris's
        # decision; an archived row keyed on it cannot be corrected once a
        # clearinghouse payload carries it.
        assert REHEARSAL_STORE_ID.startswith("rehearsal/")
        assert REHEARSAL_STORE_ID != "poplar/council-1"

    def test_tmp_and_scratch_are_the_accepted_prefixes(self) -> None:
        assert SCRATCH_PREFIXES == ("/scratch/", "/tmp/")


class TestInPodLeg:
    """A mocked load is NEVER an acceptance, so the leg reports, never claims."""

    def test_the_leg_is_awaiting_operator_acceptance_and_never_success(self) -> None:
        leg = in_pod_leg()
        assert leg["state"] == "awaiting operator acceptance"
        assert "success" not in leg["state"]
        assert leg["production_clearinghouse_rows_written"] == 0

    def test_it_names_every_independent_reason_the_pod_is_unreachable(self) -> None:
        why = " ".join(in_pod_leg()["why"])
        assert "KBUtilLib#91" in why
        assert "not bootstrapped" in why
        assert "not a Maestro or AgentForge worker" in why

    def test_it_says_what_would_close_the_leg(self) -> None:
        closes = in_pod_leg()["what_would_close_it"]
        assert "smoke test" in closes and "DEPLOYED" in closes
        assert "mocked load is NEVER an acceptance" in closes


class TestRender:
    """The report is read by a human, so every state has to be visible in it."""

    def _report(self) -> dict:
        asserted = Step(number="1", name="bootstrap")
        asserted.check("nine tables", 9, 9)
        failed = Step(number="3", name="direct query")
        failed.check("203,821 calls", 203821, 88465)
        skipped = Step(number="5", name="bundle")
        skipped.not_asserted("run directory absent: ['/scratch/chenry']")
        return {
            "steps": [s.as_dict() for s in (asserted, failed, skipped)],
            "summary": {
                ASSERTED: 1,
                FAILED: 1,
                NOT_ASSERTED: 1,
                "assertions_held": 1,
                "assertions_total": 2,
                "verdict": "FAILED",
            },
            "in_pod_leg": in_pod_leg(),
        }

    def test_each_state_and_its_reason_appear_in_the_text(self) -> None:
        text = render_report(self._report())
        assert "STATE: ASSERTED" in text
        assert "STATE: FAILED" in text
        assert "STATE: NOT ASSERTED" in text
        assert "run directory absent" in text

    def test_a_failing_assertion_shows_BOTH_values(self) -> None:
        text = render_report(self._report())
        # Evidence, not a verdict: a reader must be able to see what was
        # expected and what was measured without re-running anything.
        assert "expected=203,821" in text and "measured=88,465" in text

    def test_the_in_pod_leg_is_rendered_and_is_not_a_pass(self) -> None:
        text = render_report(self._report())
        assert "LIVE IN-POD LEG: awaiting operator acceptance" in text
