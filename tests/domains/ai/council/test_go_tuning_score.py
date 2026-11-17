"""``go_tuning.score`` -- the held-out scorer, the guards and the stop rule.

Every test here builds its two (or three) finished ``go_mf`` stores from
:mod:`.go_tuning_score_support`, whose flip structure is chosen so the
expected primaries, gain and guard rates are arithmetic a reader can check by
hand -- 8 of 10 heldout items right against 4 of 10 is 0.8 vs 0.4 and a gain
of 0.4, and nothing in the scorer is allowed to produce those numbers by
coincidence.

THE PILOT TESTS ARE THE POINT OF THIS MODULE AS MUCH AS THE ARITHMETIC IS.  A
locked test set is lost silently: a leak shows up as a pilot number that looks
fine, months later, with no way to tell it apart from a real one.  So
``score`` is asserted to REFUSE the pilot -- by name, under a pool alias, and
when a manifest puts a pilot item in a scored pool -- rather than merely to
filter it out.

Nothing here makes a network call, spends a token or needs a GPU.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

import pytest

pytest.importorskip("sqlalchemy")

from kbutillib.domains.ai.council.go_tuning.conflicts import Refused  # noqa: E402
from kbutillib.domains.ai.council.go_tuning.ledger import Ledger  # noqa: E402
from kbutillib.domains.ai.council.go_tuning.score import (  # noqa: E402
    BOOTSTRAP_SEED,
    MAX_CANDIDATES,
    MAX_WITHOUT_IMPROVEMENT,
    RunRef,
    Score,
    audit,
    measure_noise,
    paired_bootstrap,
    score,
    stop_rule,
)

from . import go_tuning_score_support as S  # noqa: E402

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def manifest() -> Dict[str, Any]:
    return S.manifest_of(S.corpus_entries())


@pytest.fixture()
def run_dir(tmp_path: Any) -> Any:
    return tmp_path / "run"


def build(tmp_path: Any, name: str, answers: Dict[str, Any], **kwargs: Any) -> RunRef:
    """One run of the corpus, in a store of its own."""
    url = S.run_store(tmp_path / f"{name}.sqlite", f"job-{name}", answers, **kwargs)
    return RunRef(url, f"job-{name}")


def gaining_pair(tmp_path: Any, **candidate: Any) -> Any:
    """``(candidate, baseline)``: 8 of 10 right against 4 of 10.

    ``candidate`` keyword arguments are forwarded to
    :func:`..go_tuning_score_support.answers` for the candidate only, which is
    how the guard tests spoil exactly one slice and leave the rest gaining.
    """
    cand = build(
        tmp_path,
        "cand",
        S.answers(S.CANDIDATE_RIGHT, pilot_right=S.PILOT[:3], **candidate),
    )
    base = build(
        tmp_path, "base", S.answers(S.BASELINE_RIGHT, pilot_right=S.PILOT[:1])
    )
    return cand, base


def score_it(
    manifest: Dict[str, Any],
    candidate: RunRef,
    baseline: RunRef,
    *,
    gold: Optional[Dict[str, Any]] = None,
    noise_floor: Optional[float] = 0.0,
    band: str = "trusted",
    **kwargs: Any,
) -> Score:
    return score(
        candidate.store,
        candidate.job_id,
        baseline.store,
        baseline.job_id,
        S.corpus_gold() if gold is None else gold,
        manifest,
        noise_floor,
        band=band,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# the pilot is locked
# ---------------------------------------------------------------------------


class TestScoreRefusesThePilot:
    def test_naming_the_pilot_pool_is_refused(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        cand, base = gaining_pair(tmp_path)
        with pytest.raises(Refused) as exc:
            score_it(manifest, cand, base, pools=("heldout", "pilot"))
        assert "LOCKED TEST SET" in str(exc.value)

    def test_an_unknown_pool_is_refused_rather_than_ignored(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        cand, base = gaining_pair(tmp_path)
        with pytest.raises(Refused) as exc:
            score_it(manifest, cand, base, pools=("heldout", "piIot"))
        assert "piIot" in str(exc.value)

    def test_pilot_items_are_never_scored_even_when_gold_covers_them(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        cand, base = gaining_pair(tmp_path)
        result = score_it(
            manifest,
            cand,
            base,
            gold=S.corpus_gold(pools=("heldout", "pilot")),
        )
        scored = set(result.scored["item_ids"])
        assert scored and not scored & set(S.PILOT)
        assert result.pool == "heldout"
        assert "pilot" not in result.scored["pools"]

    def test_a_pilot_item_smuggled_into_heldout_is_refused(self, tmp_path: Any) -> None:
        """The defence has to survive a MANIFEST that puts the item in both.

        Pool assignment is the sampler's, and a sampler bug (or a hand-edited
        manifest) that listed one item twice would otherwise leak the locked
        set through a pool name score() does trust.
        """
        entries = S.corpus_entries()
        entries.append(S.entry(S.PILOT[0], "heldout"))
        leaky = S.manifest_of(entries)
        cand, base = gaining_pair(tmp_path)
        with pytest.raises(Refused) as exc:
            score_it(leaky, cand, base)
        assert S.PILOT[0] in exc.value.ids


class TestAuditReadsOnlyThePilot:
    def test_audit_scores_the_pilot_and_nothing_else(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        cand, base = gaining_pair(tmp_path)
        result = audit(
            cand,
            base,
            S.corpus_gold(provenance="human", pools=("heldout", "pilot")),
            manifest,
            noise_floor=0.0,
        )
        assert result.pool == "pilot"
        assert result.audit is True
        assert set(result.scored["item_ids"]) == set(S.PILOT)
        # 3 of 4 right against 1 of 4.
        assert result.primary["candidate"] == pytest.approx(0.75)
        assert result.primary["baseline"] == pytest.approx(0.25)
        assert result.primary["gain"] == pytest.approx(0.5)

    def test_audit_refuses_gold_that_is_not_chris_s(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        cand, base = gaining_pair(tmp_path)
        with pytest.raises(Refused) as exc:
            audit(cand, base, S.corpus_gold(pools=("pilot",)), manifest)
        assert "did not come from Chris" in str(exc.value)
        assert set(exc.value.ids) == set(S.PILOT)

    def test_audit_reports_no_guard_verdict(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        """An audit is pool pilot only, so the guards are not part of it."""
        cand, base = gaining_pair(tmp_path)
        result = audit(
            cand, base, S.corpus_gold(provenance="human", pools=("pilot",)), manifest
        )
        assert result.gain["guards"] is None
        assert set(result.gain["checks"]) == {
            "gain_over_noise",
            "bootstrap_lower_positive",
        }
        assert result.decline["candidate"] is None
        assert result.regression["candidate"] is None
        assert result.incumbent["replaced"] is False

    def test_audit_is_recorded_with_audit_true(
        self, tmp_path: Any, manifest: Dict[str, Any], run_dir: Any
    ) -> None:
        cand, base = gaining_pair(tmp_path)
        ledger = Ledger(run_dir)
        audit(
            cand,
            base,
            S.corpus_gold(provenance="human", pools=("pilot",)),
            manifest,
            ledger=ledger,
        )
        records = ledger.records("score")
        assert len(records) == 1
        assert records[0]["audit"] is True
        assert records[0]["pool"] == "pilot"
        # An audit is not a candidate attempt, so the stop rule ignores it.
        assert stop_rule(ledger)["candidates"] == 0


# ---------------------------------------------------------------------------
# the metrics
# ---------------------------------------------------------------------------


class TestKnownFlipStructure:
    def test_primary_gain_and_guards(self, tmp_path: Any, manifest: Dict[str, Any]) -> None:
        cand, base = gaining_pair(tmp_path)
        result = score_it(manifest, cand, base, agreed=S.AGREED)

        assert result.scored["n"] == 10
        assert result.primary["candidate"] == pytest.approx(0.8)
        assert result.primary["baseline"] == pytest.approx(0.4)
        assert result.primary["gain"] == pytest.approx(0.4)
        assert result.primary["candidate_exact"] == 8
        assert result.primary["baseline_exact"] == 4

        assert result.decline["n"] == 6
        assert result.decline["candidate"] == pytest.approx(0.0)
        assert result.decline["baseline"] == pytest.approx(0.0)
        assert result.regression["n"] == 6
        assert result.regression["candidate"] == pytest.approx(0.0)
        assert result.regression["baseline"] == pytest.approx(0.0)

        assert result.bootstrap["groups"] == 5
        assert result.bootstrap["lower"] > 0.0
        assert result.gain["checks"] == {
            "gain_over_noise": True,
            "bootstrap_lower_positive": True,
            "decline_guard": True,
            "regression_guard": True,
        }
        assert result.is_gain is True
        assert result.gain["reasons"] == []

    def test_a_flip_is_set_inequality_not_partial_credit(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        """A SUPERSET of the gold set is a miss, exactly like a subset is.

        The product's answer is the set; a metric that gave partial credit
        would let a candidate that systematically over-picks read as an
        improvement.
        """
        superset = {
            item_id: ["GO:0005524", "GO:0016887"] for item_id in S.HELDOUT
        }
        cand = build(tmp_path, "cand", {**S.answers(), **superset})
        base = build(tmp_path, "base", S.answers(S.BASELINE_RIGHT))
        result = score_it(manifest, cand, base)
        assert result.primary["candidate"] == pytest.approx(0.0)

    def test_declines_count_only_ok_empty_sets(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        cand, base = gaining_pair(tmp_path, declines=3)
        result = score_it(manifest, cand, base)
        assert result.decline["candidate_declined"] == 3
        assert result.decline["candidate"] == pytest.approx(0.5)
        assert result.decline["baseline_declined"] == 0

    def test_the_candidate_s_regression_flips_are_against_the_fresh_baseline(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        """C15: a drift both runs share is not charged to the candidate.

        Both runs flip the same three regression items away from the agreed
        set, so the candidate has introduced nothing -- its gated rate is 0
        while the fresh baseline's drift from the extract is 0.5.
        """
        cand = build(
            tmp_path, "cand", S.answers(S.CANDIDATE_RIGHT, regression_flips=3)
        )
        base = build(
            tmp_path, "base", S.answers(S.BASELINE_RIGHT, regression_flips=3)
        )
        result = score_it(manifest, cand, base, agreed=S.AGREED)
        assert result.regression["candidate"] == pytest.approx(0.0)
        assert result.regression["baseline"] == pytest.approx(0.5)
        assert result.regression["candidate_vs_agreed"] == pytest.approx(0.5)
        assert result.is_gain is True

    def test_without_agreed_sets_the_baseline_s_own_drift_is_reported_unknown(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        cand, base = gaining_pair(tmp_path)
        result = score_it(manifest, cand, base)
        assert result.regression["baseline"] is None
        assert "no `agreed` sets were supplied" in result.regression["baseline_unavailable"]

    def test_secondary_metrics_are_reported(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        cand, base = gaining_pair(tmp_path)
        result = score_it(manifest, cand, base)
        conflict = result.secondary["conflict_rate"]
        # The two runs differ on exactly the four heldout items one got right
        # and the other did not; the guard slices agree.
        assert conflict["n"] == 22
        assert conflict["differing"] == 4
        assert conflict["by_pool"]["regression"] == pytest.approx(0.0)
        assert "argo_tokens" in result.secondary["tokens"]["candidate"]
        assert "attempts" in result.secondary["latency"]["candidate"]


class TestNoiseIsNotAGain:
    def test_equal_primaries_with_different_sets_are_not_a_gain(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        """The candidate is as accurate as the baseline and merely disagrees.

        Four right each, but not the SAME four -- which is what two runs of
        one unchanged prompt look like.  The gain is exactly zero and both
        gain tests fail.
        """
        cand = build(tmp_path, "cand", S.answers(("h01", "h03", "h05", "h07")))
        base = build(tmp_path, "base", S.answers(S.BASELINE_RIGHT))
        result = score_it(manifest, cand, base, noise_floor=0.0)
        assert result.primary["gain"] == pytest.approx(0.0)
        assert result.secondary["conflict_rate"]["differing"] == 8
        assert result.is_gain is False
        assert result.gain["checks"]["gain_over_noise"] is False
        assert result.gain["checks"]["bootstrap_lower_positive"] is False

    def test_a_one_item_gain_below_twice_the_noise_floor_is_not_a_gain(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        cand = build(tmp_path, "cand", S.answers(S.BASELINE_RIGHT + ("h01",)))
        base = build(tmp_path, "base", S.answers(S.BASELINE_RIGHT))
        result = score_it(manifest, cand, base, noise_floor=0.06)
        assert result.primary["gain"] == pytest.approx(0.1)
        assert result.gain["checks"]["gain_over_noise"] is False
        assert "twice the noise floor" in result.gain["reasons"][0]
        assert result.is_gain is False

    def test_the_same_gain_above_the_floor_is_only_blocked_by_the_interval(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        """Test 1 and test 2 are independent, and both are required.

        One item better out of ten clears ``2 * 0.01``, so the noise test
        passes -- and the interval still refuses it, because a gain carried by
        one of five entity groups is not distinguishable from drift.
        """
        cand = build(tmp_path, "cand", S.answers(S.BASELINE_RIGHT + ("h01",)))
        base = build(tmp_path, "base", S.answers(S.BASELINE_RIGHT))
        result = score_it(manifest, cand, base, noise_floor=0.01)
        assert result.gain["checks"]["gain_over_noise"] is True
        assert result.gain["checks"]["bootstrap_lower_positive"] is False
        assert result.bootstrap["lower"] == pytest.approx(0.0)
        assert result.is_gain is False


class TestGuardsVetoAGain:
    def test_the_decline_guard_vetoes(self, tmp_path: Any, manifest: Dict[str, Any]) -> None:
        cand, base = gaining_pair(tmp_path, declines=3)
        result = score_it(manifest, cand, base, agreed=S.AGREED)
        assert result.primary["gain"] == pytest.approx(0.4)
        assert result.gain["checks"]["gain_over_noise"] is True
        assert result.gain["checks"]["bootstrap_lower_positive"] is True
        assert result.gain["checks"]["regression_guard"] is True
        assert result.gain["checks"]["decline_guard"] is False
        assert result.is_gain is False
        assert any("decline rate" in r for r in result.gain["reasons"])

    def test_the_regression_guard_vetoes(self, tmp_path: Any, manifest: Dict[str, Any]) -> None:
        cand, base = gaining_pair(tmp_path, regression_flips=3)
        result = score_it(manifest, cand, base, agreed=S.AGREED)
        assert result.primary["gain"] == pytest.approx(0.4)
        assert result.gain["checks"]["gain_over_noise"] is True
        assert result.gain["checks"]["bootstrap_lower_positive"] is True
        assert result.gain["checks"]["decline_guard"] is True
        assert result.gain["checks"]["regression_guard"] is False
        assert result.is_gain is False
        assert any("regression flip rate" in r for r in result.gain["reasons"])

    def test_a_decline_shift_inside_the_tolerance_does_not_veto(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        """One decline in six is 0.167, which is NOT inside the 0.02 tolerance.

        So the smallest slice that can shift at all already vetoes -- which is
        a statement about the guard being too small, and the reason the real
        plan asks for 400 decline-guard rows.  The tolerance is checked
        directly instead.
        """
        cand, base = gaining_pair(tmp_path, declines=1)
        result = score_it(manifest, cand, base)
        assert result.decline["candidate"] == pytest.approx(1 / 6)
        assert result.decline["limit"] == pytest.approx(0.02)
        assert result.gain["checks"]["decline_guard"] is False

    def test_an_empty_guard_slice_fails_its_test_rather_than_passing(
        self, tmp_path: Any
    ) -> None:
        """A guard with no items has not shown anything held."""
        manifest = S.manifest_of(
            [S.entry(i, "heldout", entity=S.HELDOUT_ENTITY[i]) for i in S.HELDOUT]
        )
        cand, base = gaining_pair(tmp_path)
        result = score_it(manifest, cand, base, pools=("heldout",))
        assert result.decline["candidate"] is None
        assert result.gain["checks"]["decline_guard"] is False
        assert result.gain["checks"]["regression_guard"] is False
        assert result.is_gain is False
        assert any("decline guard has no items" in r for r in result.gain["reasons"])


# ---------------------------------------------------------------------------
# the bootstrap
# ---------------------------------------------------------------------------


def _irregular() -> Any:
    """``(groups, candidate, baseline)``: seven groups of uneven size and sign.

    Three items improve as one group, two regress as another, and the rest are
    level -- so the resample distribution is wide enough that two seeds land
    on different quantiles.
    """
    spec = {
        "g0": [(1, 0)] * 3,
        "g1": [(1, 0)],
        "g2": [(0, 0)] * 4,
        "g3": [(0, 1)] * 2,
        "g4": [(1, 1)],
        "g5": [(1, 0), (0, 1)],
        "g6": [(0, 0)],
    }
    groups: Dict[str, str] = {}
    candidate: Dict[str, bool] = {}
    baseline: Dict[str, bool] = {}
    index = 0
    for group, pairs in spec.items():
        for cand, base in pairs:
            item_id = f"i{index:02d}"
            index += 1
            groups[item_id] = group
            candidate[item_id] = bool(cand)
            baseline[item_id] = bool(base)
    return groups, candidate, baseline


class TestPairedBootstrap:
    def test_the_interval_is_reproducible_with_its_seed(self) -> None:
        candidate = {f"i{n}": n % 3 != 0 for n in range(40)}
        baseline = {f"i{n}": n % 5 != 0 for n in range(40)}
        groups = {f"i{n}": f"g{n // 4}" for n in range(40)}
        first = paired_bootstrap(candidate, baseline, groups)
        second = paired_bootstrap(candidate, baseline, groups)
        assert first == second
        assert first["seed"] == BOOTSTRAP_SEED
        assert first["resamples"] == 1000
        assert first["groups"] == 10

    def test_the_seed_is_load_bearing(self) -> None:
        """A different seed draws a different resample set.

        Over an IRREGULAR corpus -- uneven group sizes and deltas of both
        signs -- because a regular one produces the same nearest-rank
        quantiles for every seed and would prove nothing either way.
        """
        groups, candidate, baseline = _irregular()
        bounds = {
            (
                paired_bootstrap(candidate, baseline, groups, seed=seed)["lower"],
                paired_bootstrap(candidate, baseline, groups, seed=seed)["upper"],
            )
            for seed in (BOOTSTRAP_SEED, 1, 3, 7, 11, 99)
        }
        assert len(bounds) > 1
        points = {
            paired_bootstrap(candidate, baseline, groups, seed=seed)["point"]
            for seed in (BOOTSTRAP_SEED, 7)
        }
        assert len(points) == 1, "the seed must not move the observed gain"

    def test_the_global_rng_does_not_move_the_interval(self) -> None:
        """Seeding is local, so other code's RNG use cannot change a verdict."""
        import random

        candidate = {f"i{n}": n % 3 != 0 for n in range(40)}
        baseline = {f"i{n}": n % 5 != 0 for n in range(40)}
        groups = {f"i{n}": f"g{n // 4}" for n in range(40)}
        reference = paired_bootstrap(candidate, baseline, groups)
        random.seed(1)
        [random.random() for _ in range(100)]
        assert paired_bootstrap(candidate, baseline, groups) == reference

    def test_grouping_is_by_entity_not_by_item(self) -> None:
        """Ten items in one group resample as ONE observation.

        Every resample draws the same single group, so the interval collapses
        to the point estimate -- which is the honest answer for a corpus that
        contains one entity, and the opposite of what per-item resampling
        would claim.
        """
        candidate = {f"i{n}": True for n in range(10)}
        baseline = {f"i{n}": False for n in range(10)}
        one_group = paired_bootstrap(candidate, baseline, {f"i{n}": "e0" for n in range(10)})
        assert one_group["groups"] == 1
        assert one_group["lower"] == pytest.approx(1.0)

    def test_an_empty_comparison_has_no_interval(self) -> None:
        out = paired_bootstrap({}, {}, {})
        assert out["point"] is None
        assert out["lower"] is None and out["upper"] is None

    def test_score_s_interval_is_reproducible(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        cand, base = gaining_pair(tmp_path)
        first = score_it(manifest, cand, base)
        second = score_it(manifest, cand, base)
        assert first.bootstrap == second.bootstrap
        assert first.scored["digest"] == second.scored["digest"]


# ---------------------------------------------------------------------------
# the noise floor
# ---------------------------------------------------------------------------


class TestMeasureNoise:
    def test_the_floor_is_over_the_union_of_the_guard_slices(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        a = build(tmp_path, "base0a", S.answers(S.BASELINE_RIGHT))
        b = build(
            tmp_path,
            "base0b",
            S.answers(S.BASELINE_RIGHT, declines=1, regression_flips=1),
        )
        report = measure_noise(a, b, manifest)
        # 6 regression + 6 decline-guard keys; one of each moved.
        assert report["n"] == 12
        assert report["flipped"] == 2
        assert report["noise_floor"] == pytest.approx(2 / 12)
        assert report["per_slice"]["regression"]["rate"] == pytest.approx(1 / 6)
        assert report["per_slice"]["decline_guard"]["rate"] == pytest.approx(1 / 6)
        assert report["query"] == "go_mf@4"

    def test_two_identical_baselines_have_a_zero_floor(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        a = build(tmp_path, "base0a", S.answers(S.BASELINE_RIGHT))
        b = build(tmp_path, "base0b", S.answers(S.BASELINE_RIGHT))
        assert measure_noise(a, b, manifest)["noise_floor"] == pytest.approx(0.0)

    def test_the_same_job_twice_is_refused(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        a = build(tmp_path, "base0a", S.answers(S.BASELINE_RIGHT))
        with pytest.raises(Refused) as exc:
            measure_noise(a, a, manifest)
        assert "measures zero by construction" in str(exc.value)

    def test_two_different_query_versions_are_refused(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        a = build(tmp_path, "base0a", S.answers(S.BASELINE_RIGHT))
        b = build(
            tmp_path,
            "cand0",
            S.answers(S.BASELINE_RIGHT),
            query=("go_mf", "7"),
        )
        with pytest.raises(Refused) as exc:
            measure_noise(a, b, manifest)
        assert "not noise" in str(exc.value)

    def test_a_manifest_with_no_guard_slices_is_refused(self, tmp_path: Any) -> None:
        bare = S.manifest_of([S.entry(S.HELDOUT[0], "heldout")])
        a = build(tmp_path, "base0a", S.answers(S.BASELINE_RIGHT))
        b = build(tmp_path, "base0b", S.answers(S.BASELINE_RIGHT))
        with pytest.raises(Refused) as exc:
            measure_noise(a, b, bare)
        assert "guard slices" in str(exc.value)


# ---------------------------------------------------------------------------
# C15's comparability rule
# ---------------------------------------------------------------------------


class TestRunsMustBeComparable:
    def test_a_missing_item_is_refused(self, tmp_path: Any, manifest: Dict[str, Any]) -> None:
        cand, _ = gaining_pair(tmp_path)
        short = S.answers(S.BASELINE_RIGHT)
        short.pop(S.HELDOUT[3])
        base = build(tmp_path, "short", short)
        with pytest.raises(Refused) as exc:
            score_it(manifest, cand, base)
        assert "no result for" in str(exc.value)
        assert S.HELDOUT[3] in exc.value.ids

    def test_an_item_that_did_not_settle_ok_is_refused(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        answers = S.answers(S.BASELINE_RIGHT)
        answers[S.DECLINE_GUARD[0]] = {"status": "error", "with_verdict": False}
        cand, _ = gaining_pair(tmp_path)
        base = build(tmp_path, "errored", answers)
        with pytest.raises(Refused) as exc:
            score_it(manifest, cand, base)
        assert "did not settle ok" in str(exc.value)

    def test_an_ok_item_with_no_verdict_is_refused_not_read_as_a_decline(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        answers = S.answers(S.BASELINE_RIGHT)
        answers[S.DECLINE_GUARD[0]] = None
        cand, _ = gaining_pair(tmp_path)
        base = build(tmp_path, "noverdict", answers)
        with pytest.raises(Refused) as exc:
            score_it(manifest, cand, base)
        assert "did not settle ok" in str(exc.value)

    def test_a_different_menu_for_one_item_is_refused(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        answers = S.answers(S.BASELINE_RIGHT)
        answers[S.HELDOUT[0]] = {
            "selected": list(S.TRUTH[S.HELDOUT[0]]),
            "menu": list(S.MENU[:2]),
        }
        cand, _ = gaining_pair(tmp_path)
        base = build(tmp_path, "othermenu", answers)
        with pytest.raises(Refused) as exc:
            score_it(manifest, cand, base)
        assert "a different question" in str(exc.value)
        assert S.HELDOUT[0] in exc.value.ids

    def test_a_different_evidence_string_for_one_item_is_refused(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        answers = S.answers(S.BASELINE_RIGHT)
        answers[S.HELDOUT[0]] = {
            "selected": list(S.TRUTH[S.HELDOUT[0]]),
            "evidence": {"text": "something else entirely", "source": "prokka"},
        }
        cand, _ = gaining_pair(tmp_path)
        base = build(tmp_path, "otherevidence", answers)
        with pytest.raises(Refused):
            score_it(manifest, cand, base)

    def test_an_unknown_job_is_refused(self, tmp_path: Any, manifest: Dict[str, Any]) -> None:
        cand, base = gaining_pair(tmp_path)
        with pytest.raises(Refused) as exc:
            score_it(manifest, cand, RunRef(base.store, "no-such-job"))
        assert "no job" in str(exc.value)


class TestUnscorable:
    def test_no_gold_at_all_is_unscorable(self, tmp_path: Any, manifest: Dict[str, Any]) -> None:
        cand, base = gaining_pair(tmp_path)
        with pytest.raises(Refused) as exc:
            score_it(manifest, cand, base, gold={})
        assert "UNSCORABLE" in str(exc.value)

    def test_gold_that_is_entirely_off_menu_is_unscorable(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        cand, base = gaining_pair(tmp_path)
        with pytest.raises(Refused) as exc:
            score_it(manifest, cand, base, gold=S.corpus_gold(off_menu=S.HELDOUT))
        assert "UNSCORABLE" in str(exc.value)


class TestTheBandGatesJudgeGold:
    def test_not_trusted_refuses_judge_derived_gold(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        cand, base = gaining_pair(tmp_path)
        with pytest.raises(Refused) as exc:
            score_it(manifest, cand, base, band="not_trusted")
        assert "not_trusted" in str(exc.value)
        assert set(exc.value.ids) == set(S.HELDOUT)

    def test_not_trusted_admits_chris_marked_gold(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        cand, base = gaining_pair(tmp_path)
        result = score_it(
            manifest,
            cand,
            base,
            gold=S.corpus_gold(provenance="human"),
            band="not_trusted",
        )
        assert result.primary["gain"] == pytest.approx(0.4)

    def test_a_trusted_band_admits_judge_gold(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        cand, base = gaining_pair(tmp_path)
        assert score_it(manifest, cand, base, band="trusted").primary["n"] == 10


class TestGoldShapes:
    def test_a_gold_object_from_the_marks_reader_is_accepted(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        from kbutillib.domains.ai.council.go_tuning.table import Gold, GoldRecord

        gold = Gold(
            manifest_id="m-test",
            scope="pilot",
            marks={i: "R" for i in S.HELDOUT},
            records=tuple(
                GoldRecord(
                    item_id=i,
                    mark="R",
                    provenance="judge",
                    ids=tuple(S.TRUTH[i]),
                    off_menu=False,
                )
                for i in S.HELDOUT
            ),
        )
        cand, base = gaining_pair(tmp_path)
        assert score_it(manifest, cand, base, gold=gold).primary["n"] == 10

    def test_a_bare_id_list_is_accepted_as_on_menu_gold(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        cand, base = gaining_pair(tmp_path)
        bare = {i: list(S.TRUTH[i]) for i in S.HELDOUT}
        assert score_it(manifest, cand, base, gold=bare).primary["n"] == 10


# ---------------------------------------------------------------------------
# C19: the incumbent
# ---------------------------------------------------------------------------


class TestTheIncumbent:
    def test_the_incumbent_starts_as_the_fresh_baseline(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        cand, base = gaining_pair(tmp_path)
        result = score_it(manifest, cand, base, agreed=S.AGREED)
        assert result.incumbent["label"] == "go_mf@4"
        assert result.incumbent["primary"] == pytest.approx(0.4)
        assert result.incumbent["on_menu_misses"] == 6
        assert result.replaces_incumbent is True

    def test_a_gaining_candidate_that_ties_the_incumbent_does_not_replace_it(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        """C19: ties keep the older version."""
        cand, base = gaining_pair(tmp_path)
        incumbent = build(
            tmp_path,
            "inc",
            S.answers(S.CANDIDATE_RIGHT, wrong=S.ALSO_WRONG),
        )
        result = score_it(
            manifest, cand, base, agreed=S.AGREED, incumbent=incumbent, incumbent_label="go_mf@6"
        )
        assert result.is_gain is True
        assert result.incumbent["label"] == "go_mf@6"
        assert result.incumbent["primary"] == pytest.approx(0.8)
        assert result.incumbent["higher_primary"] is False
        assert result.replaces_incumbent is False
        assert "C19 keeps the older version" in result.incumbent["detail"]

    def test_a_gaining_candidate_below_the_incumbent_does_not_replace_it(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        cand, base = gaining_pair(tmp_path)
        incumbent = build(tmp_path, "inc", S.answers(S.HELDOUT[:9]))
        result = score_it(manifest, cand, base, agreed=S.AGREED, incumbent=incumbent)
        assert result.is_gain is True
        assert result.incumbent["primary"] == pytest.approx(0.9)
        assert result.replaces_incumbent is False
        assert "is not above the" in result.incumbent["detail"]

    def test_the_incumbent_is_re_measured_on_this_score_s_own_subset(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        """Gold grows between iterations, so a stored primary will not do.

        Here half the heldout gold is off-menu, so only five items are
        labelled -- and the incumbent's primary is reported over those five,
        not over the ten it would have scored on a fuller gold set.
        """
        cand, base = gaining_pair(tmp_path)
        incumbent = build(tmp_path, "inc", S.answers(S.HELDOUT[:5]))
        result = score_it(
            manifest,
            cand,
            base,
            gold=S.corpus_gold(off_menu=S.HELDOUT[5:]),
            incumbent=incumbent,
        )
        assert result.scored["n"] == 5
        assert result.incumbent["n"] == 5
        assert result.incumbent["primary"] == pytest.approx(1.0)
        assert result.incumbent["subset_digest"] == result.scored["digest"]
        assert result.incumbent["measured"].startswith("re-measured")

    def test_a_candidate_without_a_gain_never_replaces_the_incumbent(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        cand = build(tmp_path, "cand", S.answers(S.CANDIDATE_RIGHT, declines=3))
        base = build(tmp_path, "base", S.answers(S.BASELINE_RIGHT))
        result = score_it(manifest, cand, base)
        assert result.is_gain is False
        assert result.incumbent["higher_primary"] is True
        assert result.replaces_incumbent is False
        assert result.incumbent["detail"] == "the candidate did not pass the gain rule"


class TestOffMenuAccounting:
    def test_the_share_is_off_menu_over_everything_still_wrong(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        cand, base = gaining_pair(tmp_path)
        result = score_it(manifest, cand, base, gold=S.corpus_gold(off_menu=S.HELDOUT[:4]))
        # Six labelled items remain; the incumbent (the fresh baseline) gets
        # h04 and h06 right, so four are on-menu misses.
        assert result.heldout["off_menu_gold_rows"] == 4
        assert result.heldout["incumbent_on_menu_misses"] == 4
        assert result.heldout["off_menu_share"] == pytest.approx(0.5)

    def test_a_zero_denominator_reports_no_share_rather_than_zero_over_zero(
        self, tmp_path: Any, manifest: Dict[str, Any]
    ) -> None:
        perfect = build(tmp_path, "perfect", S.answers(S.HELDOUT))
        other = build(tmp_path, "other", S.answers(S.HELDOUT))
        result = score_it(manifest, perfect, other)
        assert result.heldout["remaining_failures"] == 0
        assert result.heldout["off_menu_share"] is None


# ---------------------------------------------------------------------------
# the ledger record
# ---------------------------------------------------------------------------


class TestTheScoreRecord:
    def test_a_score_is_recorded_with_the_fields_the_stop_rule_reads(
        self, tmp_path: Any, manifest: Dict[str, Any], run_dir: Any
    ) -> None:
        cand, base = gaining_pair(tmp_path)
        ledger = Ledger(run_dir)
        result = score_it(manifest, cand, base, agreed=S.AGREED, ledger=ledger)
        records = ledger.records("score")
        assert len(records) == 1
        record = records[0]
        assert record["audit"] is False
        assert record["pool"] == "heldout"
        assert record["replaces_incumbent"] is True
        assert record["gain"]["is_gain"] is True
        assert record["candidate"]["job_id"] == cand.job_id
        assert record["baseline"]["job_id"] == base.job_id
        assert record["heldout"]["off_menu_share"] == result.heldout["off_menu_share"]
        assert record["metrics"] == result.as_dict()


# ---------------------------------------------------------------------------
# the stop rule
# ---------------------------------------------------------------------------


def record_score(
    tmp_path: Any,
    manifest: Dict[str, Any],
    ledger: Ledger,
    name: str,
    *,
    candidate_right: Sequence[str],
    gold: Optional[Dict[str, Any]] = None,
    candidate: Optional[Dict[str, Any]] = None,
) -> Score:
    """Score one synthetic candidate into *ledger*, and return the score."""
    cand = build(
        tmp_path, f"cand-{name}", S.answers(candidate_right, **(candidate or {}))
    )
    base = build(tmp_path, f"base-{name}", S.answers(S.BASELINE_RIGHT))
    return score_it(
        manifest, cand, base, gold=gold, agreed=S.AGREED, ledger=ledger
    )


class TestStopRule:
    def test_an_empty_ledger_does_not_stop(self, run_dir: Any) -> None:
        decision = stop_rule(Ledger(run_dir))
        assert decision["stop"] is False
        assert decision["reason"] is None
        assert decision["candidates"] == 0
        assert decision["incumbent"] == "go_mf@4"

    def test_one_candidate_without_a_gain_does_not_stop(
        self, tmp_path: Any, manifest: Dict[str, Any], run_dir: Any
    ) -> None:
        ledger = Ledger(run_dir)
        result = record_score(
            tmp_path, manifest, ledger, "a", candidate_right=S.BASELINE_RIGHT
        )
        assert result.is_gain is False
        decision = stop_rule(ledger)
        assert decision["stop"] is False
        assert decision["consecutive_without_improvement"] == 1

    def test_three_candidates_without_a_gain_stop_the_run(
        self, tmp_path: Any, manifest: Dict[str, Any], run_dir: Any
    ) -> None:
        ledger = Ledger(run_dir)
        for name in ("a", "b", "c"):
            result = record_score(
                tmp_path, manifest, ledger, name, candidate_right=S.BASELINE_RIGHT
            )
            assert result.is_gain is False
        decision = stop_rule(ledger)
        assert decision["stop"] is True
        assert decision["reason"] == "no_improvement_streak"
        assert decision["consecutive_without_improvement"] == MAX_WITHOUT_IMPROVEMENT
        assert decision["consecutive_without_gain"] == MAX_WITHOUT_IMPROVEMENT
        stops = ledger.records("stop")
        assert len(stops) == 1
        assert stops[0]["reason"] == "no_improvement_streak"
        assert stops[0]["source"] == "stop_rule"

    def test_the_streak_resets_only_on_a_replacement(
        self, tmp_path: Any, manifest: Dict[str, Any], run_dir: Any
    ) -> None:
        ledger = Ledger(run_dir)
        for name in ("a", "b"):
            record_score(tmp_path, manifest, ledger, name, candidate_right=S.BASELINE_RIGHT)
        winner = record_score(
            tmp_path, manifest, ledger, "c", candidate_right=S.CANDIDATE_RIGHT
        )
        assert winner.replaces_incumbent is True
        record_score(tmp_path, manifest, ledger, "d", candidate_right=S.BASELINE_RIGHT)
        decision = stop_rule(ledger)
        assert decision["stop"] is False
        assert decision["candidates"] == 4
        assert decision["consecutive_without_improvement"] == 1
        assert decision["incumbent"] == "go_mf@4"
        assert ledger.records("stop") == []

    def test_off_menu_gold_above_half_the_remaining_failures_stops_the_run(
        self, tmp_path: Any, manifest: Dict[str, Any], run_dir: Any
    ) -> None:
        """Six off-menu rows against four labelled ones the incumbent gets right.

        The denominator is 6 + 0, so the share is 1.0: everything still wrong
        under the incumbent is wrong because the right term is not on the
        menu, and no prompt change can reach it.
        """
        ledger = Ledger(run_dir)
        result = record_score(
            tmp_path,
            manifest,
            ledger,
            "a",
            candidate_right=S.BASELINE_RIGHT,
            gold=S.corpus_gold(off_menu=("h01", "h03", "h05", "h07", "h08", "h09")),
        )
        assert result.heldout["off_menu_share"] == pytest.approx(1.0)
        decision = stop_rule(ledger)
        assert decision["stop"] is True
        assert decision["reason"] == "off_menu_ceiling"
        assert decision["consecutive_without_improvement"] == 1
        assert "retrieval problem" in decision["detail"]

    def test_an_off_menu_share_at_exactly_half_does_not_stop(
        self, tmp_path: Any, manifest: Dict[str, Any], run_dir: Any
    ) -> None:
        """The rule is strictly greater than 0.5."""
        ledger = Ledger(run_dir)
        result = record_score(
            tmp_path,
            manifest,
            ledger,
            "a",
            candidate_right=S.BASELINE_RIGHT,
            gold=S.corpus_gold(off_menu=S.HELDOUT[:4]),
        )
        assert result.heldout["off_menu_share"] == pytest.approx(0.5)
        assert stop_rule(ledger)["stop"] is False

    def test_eight_candidates_exhaust_the_budget(
        self, tmp_path: Any, manifest: Dict[str, Any], run_dir: Any
    ) -> None:
        """Every one of them improves, so only the budget can fire."""
        ledger = Ledger(run_dir)
        for index in range(MAX_CANDIDATES):
            result = record_score(
                tmp_path,
                manifest,
                ledger,
                f"w{index}",
                candidate_right=S.CANDIDATE_RIGHT,
            )
            assert result.replaces_incumbent is True
        decision = stop_rule(ledger)
        assert decision["stop"] is True
        assert decision["reason"] == "candidate_budget"
        assert decision["candidates"] == MAX_CANDIDATES
        assert decision["consecutive_without_improvement"] == 0

    def test_the_stop_is_not_recorded_twice(
        self, tmp_path: Any, manifest: Dict[str, Any], run_dir: Any
    ) -> None:
        ledger = Ledger(run_dir)
        for name in ("a", "b", "c"):
            record_score(tmp_path, manifest, ledger, name, candidate_right=S.BASELINE_RIGHT)
        first = stop_rule(ledger)
        second = stop_rule(ledger)
        assert first["recorded"] is not None
        assert second["recorded"] is None
        assert len(ledger.records("stop")) == 1

    def test_record_false_decides_without_writing(
        self, tmp_path: Any, manifest: Dict[str, Any], run_dir: Any
    ) -> None:
        ledger = Ledger(run_dir)
        for name in ("a", "b", "c"):
            record_score(tmp_path, manifest, ledger, name, candidate_right=S.BASELINE_RIGHT)
        decision = stop_rule(ledger, record=False)
        assert decision["stop"] is True
        assert decision["recorded"] is None
        assert ledger.records("stop") == []

    def test_every_reason_that_fires_is_reported_not_only_the_first(
        self, tmp_path: Any, manifest: Dict[str, Any], run_dir: Any
    ) -> None:
        ledger = Ledger(run_dir)
        for name in ("a", "b", "c"):
            record_score(
                tmp_path,
                manifest,
                ledger,
                name,
                candidate_right=S.BASELINE_RIGHT,
                gold=S.corpus_gold(off_menu=("h01", "h03", "h05", "h07", "h08", "h09")),
            )
        decision = stop_rule(ledger)
        reasons = [r["reason"] for r in decision["reasons"]]
        assert reasons == ["no_improvement_streak", "off_menu_ceiling"]
        assert decision["reason"] == "no_improvement_streak"


def test_the_fixture_manifest_has_the_shape_the_sampler_writes() -> None:
    """The hand-made entries must not drift from :func:`..sample.build_manifest`.

    Everything above feeds the scorer a manifest built here rather than one
    the sampler produced, because a sampler small enough to run in a test
    cannot be made to fill all four pools with a chosen flip structure.  The
    risk that buys is a fixture that stops resembling the real artefact, so
    the KEY SET is pinned against the real one: a renamed ``item_id`` or
    ``entity_hash`` would otherwise pass here and fail only in production.
    """
    from kbutillib.domains.ai.council.go_tuning.sample import Plan, build_manifest

    from .test_go_tuning_adjudicate import corpus

    real = build_manifest(
        corpus(6),
        "seed-1",
        Plan(pilot_size=6, swap_slice=2, regression_size=0, decline_guard_size=0, min_conflicts=2),
    )
    assert set(real.entries[0]) == set(S.entry("x", "heldout"))
    assert {"pool", "item_id", "entity_hash"} <= set(real.entries[0])
    assert set(real.as_dict()) >= {"manifest_id", "entries"}


def test_the_recorded_thresholds_are_the_ones_enforced(tmp_path: Any) -> None:
    """An unmeasured noise floor is enforced as zero, and must read that way.

    ``noise_floor=None`` means nobody measured it, which is worth recording as
    ``None`` -- but the rule still applies a floor of zero, so a record whose
    only threshold field said ``None`` would show a reader a limit nothing
    actually used.
    """
    manifest = S.manifest_of(S.corpus_entries())
    cand, base = gaining_pair(tmp_path)
    result = score_it(manifest, cand, base, noise_floor=None)
    thresholds = result.gain["thresholds"]
    assert result.noise_floor is None
    assert thresholds["noise_floor"] is None
    assert thresholds["gain_must_exceed"] == pytest.approx(0.0)
    assert thresholds["regression_limit"] == pytest.approx(0.02)
    assert result.regression["limit"] == pytest.approx(0.02)
    assert result.gain["checks"]["gain_over_noise"] is True
