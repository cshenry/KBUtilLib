"""``go_tuning.calibrate`` -- agreement, its interval, the slices, the band.

The Wilson numbers are pinned against HAND-COMPUTED values rather than
against whatever the implementation produces, because the band gates a
tuning run on the lower bound and a transposed term in the formula would
move that bound by more than the gap between ``conditional`` and
``trusted``.  ``85/100 -> 0.767..0.907`` is the worked example from the PRD;
``17/20`` is the same point estimate on a fifth of the evidence, and the
test asserts that the two land in DIFFERENT bands -- which is the whole
reason an interval is in the rule.

The band boundaries are tested one at a time, including the case the order of
the clauses exists for: agreement well above 0.85 with a lower bound below
0.75 must come out ``conditional``, not ``not_trusted``.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("openpyxl")

from kbutillib.domains.ai.council.go_tuning import adjudicate as A  # noqa: E402
from kbutillib.domains.ai.council.go_tuning import calibrate as C  # noqa: E402
from kbutillib.domains.ai.council.go_tuning import table as T  # noqa: E402
from kbutillib.domains.ai.council.go_tuning.conflicts import Refused  # noqa: E402
from kbutillib.domains.ai.council.go_tuning.ledger import Ledger  # noqa: E402

from .go_tuning_table_support import (  # noqa: E402
    mark,
    material,
    results_for,
    row_ids,
)

SPEC_HASH = "a" * 64
ROSTER = "b" * 64


# ---------------------------------------------------------------------------
# Wilson
# ---------------------------------------------------------------------------


class TestWilson:
    def test_the_prd_s_worked_example(self) -> None:
        """85/100 -> about 0.767 .. 0.907."""
        lower, upper = C.wilson(85, 100)
        assert lower == pytest.approx(0.7671, abs=5e-4)
        assert upper == pytest.approx(0.9069, abs=5e-4)

    def test_the_same_proportion_on_less_evidence_is_a_wider_interval(self) -> None:
        wide_lower, wide_upper = C.wilson(17, 20)
        tight_lower, tight_upper = C.wilson(85, 100)
        assert wide_lower == pytest.approx(0.6396, abs=5e-4)
        assert wide_upper == pytest.approx(0.9476, abs=5e-4)
        assert wide_upper - wide_lower > tight_upper - tight_lower
        # And THAT is why the band gates on the lower bound: identical point
        # estimates, different verdicts.
        assert C.band_for(0.85, tight_lower, 100) == "trusted"
        assert C.band_for(0.85, wide_lower, 20) == "conditional"

    @pytest.mark.parametrize("successes,total", [(0, 10), (10, 10), (1, 1)])
    def test_the_interval_stays_inside_the_unit_interval_at_the_extremes(
        self, successes, total
    ) -> None:
        lower, upper = C.wilson(successes, total)
        assert 0.0 <= lower <= upper <= 1.0

    def test_no_observations_is_no_interval_rather_than_a_wide_one(self) -> None:
        assert C.wilson(0, 0) == (None, None)

    def test_an_agreement_exposes_the_same_numbers(self) -> None:
        slice_ = C.Agreement(right=85, wrong=15)
        assert (slice_.n, slice_.agreement) == (100, 0.85)
        payload = slice_.as_dict()
        assert payload["wilson_lower"] == pytest.approx(0.7671, abs=5e-4)
        assert payload["wilson_upper"] == pytest.approx(0.9069, abs=5e-4)


# ---------------------------------------------------------------------------
# the bands
# ---------------------------------------------------------------------------


class TestBands:
    def test_nothing_marked_is_insufficient_not_not_trusted(self) -> None:
        assert C.band_for(None, None, 0) == "insufficient"
        unmeasured = C.agreement_of(["?", "?", ""])
        assert unmeasured.n == 0
        assert unmeasured.agreement is None
        lower, _upper = unmeasured.interval
        assert C.band_for(unmeasured.agreement, lower, unmeasured.n) == "insufficient"

    @pytest.mark.parametrize(
        "right,wrong,expected",
        [
            (85, 15, "trusted"),      # agreement 0.850, lower 0.767
            (84, 16, "conditional"),  # agreement 0.840 -- below the 0.85 floor
            (17, 3, "conditional"),   # agreement 0.850, lower 0.640 -- too few
            (70, 30, "conditional"),  # agreement exactly 0.70
            (69, 31, "not_trusted"),  # agreement 0.690
            (0, 10, "not_trusted"),
            # A PERFECT score on ten rows is still only conditional: the
            # lower bound is 0.722, which is what "we have not measured it
            # yet" looks like when every row happened to be right.
            (10, 0, "conditional"),
        ],
    )
    def test_the_boundaries(self, right, wrong, expected) -> None:
        slice_ = C.Agreement(right=right, wrong=wrong)
        lower, _upper = slice_.interval
        assert C.band_for(slice_.agreement, lower, slice_.n) == expected

    def test_high_agreement_with_a_weak_lower_bound_is_conditional(self) -> None:
        """The reason the clauses are ordered: 0.90 at n=10 is not trusted...

        ...but it must not fall through to ``not_trusted`` either, which is
        what a literal "0.70 to 0.85" reading of ``conditional`` would do.
        """
        slice_ = C.Agreement(right=9, wrong=1)
        lower, _upper = slice_.interval
        assert slice_.agreement == 0.9
        assert lower < C.TRUSTED_LOWER
        assert C.band_for(slice_.agreement, lower, slice_.n) == "conditional"

    def test_every_band_name_is_declared(self) -> None:
        assert set(C.BANDS) == {"insufficient", "trusted", "conditional", "not_trusted"}


# ---------------------------------------------------------------------------
# the report over a marked table
# ---------------------------------------------------------------------------


@pytest.fixture()
def marked(tmp_path: Any) -> Dict[str, Any]:
    """A pilot table written, fully marked, and read back.

    Half the rows are given to the go_mf side so the per-side slice has both
    values populated, and the marks are chosen so the two sides' agreement
    DIFFERS -- a self-preference check that cannot distinguish the sides is
    not a check.
    """
    manifest, items = material()
    ids = row_ids(manifest)
    favour_gomf = set(ids[3:])
    results = results_for(manifest, items, wrong_items=favour_gomf)
    T.write_pilot(results, manifest, items, tmp_path / "p.xlsx", tmp_path / "p.md")
    marks = {item_id: "R" for item_id in ids}
    # Every go_mf-favoured row is wrong, so that side's agreement is 0.
    for item_id in favour_gomf:
        marks[item_id] = "W"
    mark(tmp_path / "p.xlsx", marks)
    gold = T.read_marks(tmp_path / "p.xlsx", manifest)
    return {
        "manifest": manifest,
        "items": items,
        "results": results,
        "gold": gold,
        "ids": ids,
        "favour_gomf": favour_gomf,
        "xlsx": tmp_path / "p.xlsx",
    }


@pytest.fixture()
def marked_all_right(tmp_path: Any) -> Dict[str, Any]:
    """The same table marked ``R`` throughout, so the band ADMITS a tranche.

    Six of six is ``conditional`` rather than ``trusted`` -- the Wilson lower
    bound on six observations is 0.610 -- and ``conditional`` is what the
    gate needs, so this is the fixture for the gate tests.
    """
    manifest, items = material()
    ids = row_ids(manifest)
    results = results_for(manifest, items)
    T.write_pilot(results, manifest, items, tmp_path / "p.xlsx", tmp_path / "p.md")
    mark(tmp_path / "p.xlsx", {item_id: "R" for item_id in ids})
    return {
        "manifest": manifest,
        "items": items,
        "results": results,
        "gold": T.read_marks(tmp_path / "p.xlsx", manifest),
        "ids": ids,
    }


def _report(marked: Dict[str, Any], **kwargs: Any) -> C.CalibrationReport:
    return C.report(
        marked["results"],
        marked["gold"],
        marked["manifest"],
        adjudicate_spec_hash=SPEC_HASH,
        roster_fingerprint=ROSTER,
        **kwargs,
    )


class TestReport:
    def test_overall_agreement_is_r_over_r_plus_w(self, marked) -> None:
        report = _report(marked)
        counts = marked["gold"].counts
        assert report.overall.right == counts["R"]
        assert report.overall.wrong == counts["W"]
        assert report.overall.agreement == pytest.approx(
            counts["R"] / (counts["R"] + counts["W"])
        )

    def test_the_per_side_split_is_the_self_preference_check(self, marked) -> None:
        report = _report(marked)
        assert set(report.by_side) <= {"alcf", "gomf", "both_acceptable", "neither"}
        assert report.by_side["gomf"].n > 0
        assert report.by_side["gomf"].agreement == 0.0
        assert report.by_side["alcf"].agreement == 1.0
        assert (
            report.by_side["alcf"].n + report.by_side["gomf"].n
            + sum(
                s.n
                for name, s in report.by_side.items()
                if name not in ("alcf", "gomf")
            )
            == report.overall.n
        )

    def test_the_slices_partition_the_marked_rows(self, marked) -> None:
        report = _report(marked)
        for slices in (report.by_class, report.by_source, report.by_side):
            assert sum(s.n for s in slices.values()) == report.overall.n
        assert set(report.by_source) == {"prokka", "rast"}
        assert set(report.by_class) <= {"disjoint", "partial"}

    def test_question_marks_are_in_neither_the_numerator_nor_the_denominator(
        self, tmp_path
    ) -> None:
        manifest, items = material()
        ids = row_ids(manifest)
        results = results_for(manifest, items)
        T.write_pilot(results, manifest, items, tmp_path / "p.xlsx", tmp_path / "p.md")
        marks = {item_id: "R" for item_id in ids}
        marks[ids[0]] = "?"
        mark(tmp_path / "p.xlsx", marks)
        gold = T.read_marks(tmp_path / "p.xlsx", manifest)
        report = C.report(
            results,
            gold,
            manifest,
            adjudicate_spec_hash=SPEC_HASH,
            roster_fingerprint=ROSTER,
        )
        assert report.overall.n == len(ids) - 1
        assert report.overall.agreement == 1.0
        assert report.counts["?"] == 1

    def test_a_w_without_ids_still_counts_against_agreement(self, tmp_path) -> None:
        """It supplies no gold, which is not the same as supplying no signal."""
        manifest, items = material()
        ids = row_ids(manifest)
        results = results_for(manifest, items)
        T.write_pilot(results, manifest, items, tmp_path / "p.xlsx", tmp_path / "p.md")
        marks = {item_id: "R" for item_id in ids}
        marks[ids[0]] = "W"
        mark(tmp_path / "p.xlsx", marks)
        gold = T.read_marks(tmp_path / "p.xlsx", manifest)
        assert gold.excluded[ids[0]] == "wrong_without_ids"
        report = C.report(
            results,
            gold,
            manifest,
            adjudicate_spec_hash=SPEC_HASH,
            roster_fingerprint=ROSTER,
        )
        assert report.overall.n == len(ids)
        assert report.overall.wrong == 1
        assert report.gold["records"] == len(ids) - 1

    def test_an_unmarked_row_refuses_the_whole_report(self, tmp_path) -> None:
        """C12: a partial pass must not read as a finished one."""
        manifest, items = material()
        ids = row_ids(manifest)
        results = results_for(manifest, items)
        T.write_pilot(results, manifest, items, tmp_path / "p.xlsx", tmp_path / "p.md")
        mark(tmp_path / "p.xlsx", {item_id: "R" for item_id in ids[:-1]})
        gold = T.read_marks(tmp_path / "p.xlsx", manifest)
        assert gold.unreviewed == (ids[-1],)
        with pytest.raises(Refused, match="no mark"):
            C.report(
                results,
                gold,
                manifest,
                adjudicate_spec_hash=SPEC_HASH,
                roster_fingerprint=ROSTER,
            )

    def test_a_missing_binding_is_refused(self, marked) -> None:
        for spec, roster in ((SPEC_HASH, ""), ("", ROSTER)):
            with pytest.raises(Refused, match="bound to"):
                C.report(
                    marked["results"],
                    marked["gold"],
                    marked["manifest"],
                    adjudicate_spec_hash=spec,
                    roster_fingerprint=roster,
                )

    def test_gold_from_another_manifest_is_refused(self, marked) -> None:
        other, _items = material(seed="seed-9")
        with pytest.raises(Refused, match="manifest"):
            C.report(
                marked["results"],
                marked["gold"],
                other,
                adjudicate_spec_hash=SPEC_HASH,
                roster_fingerprint=ROSTER,
            )

    def test_the_off_menu_and_both_acceptable_counts_are_reported(self, marked) -> None:
        report = _report(marked)
        assert report.off_menu_flags == sum(
            1 for r in marked["results"] if not r.swap and r.correct_term_off_menu
        )
        assert report.both_acceptable == sum(
            1 for r in marked["results"] if not r.swap and r.side == "both_acceptable"
        )


class TestWeightedAgreement:
    def test_the_weighted_figure_uses_the_population_marginals(self, marked) -> None:
        shares = C.population_shares(marked["items"])
        assert sum(shares["class"].values()) == pytest.approx(1.0)
        assert sum(shares["source"].values()) == pytest.approx(1.0)
        report = _report(marked, population=shares)
        assert report.weights_from == "population"
        assert report.weighted_agreement is not None
        assert 0.0 <= report.weighted_agreement <= 1.0
        assert report.weight_cells, "the class|source cells should be reported"
        for cell in report.weight_cells.values():
            assert cell["weight"] >= 0.0

    def test_both_figures_are_reported_and_can_differ(self, marked) -> None:
        """Reweighting a pilot whose easy class is over-represented moves it."""
        shares = {
            "class": {"disjoint": 0.9, "partial": 0.1},
            "source": {"prokka": 0.5, "rast": 0.5},
        }
        skewed = _report(marked, population=shares)
        flat = _report(
            marked,
            population={
                "class": {"disjoint": 0.5, "partial": 0.5},
                "source": {"prokka": 0.5, "rast": 0.5},
            },
        )
        assert skewed.overall.agreement == flat.overall.agreement
        assert skewed.weighted_agreement is not None
        assert flat.weighted_agreement is not None

    def test_with_no_population_the_fallback_says_so(self, marked) -> None:
        report = _report(marked)
        assert report.weights_from == "manifest population x marked rows"
        assert report.weighted_agreement is not None

    def test_a_cell_the_pilot_never_drew_contributes_nothing(self, marked) -> None:
        report = _report(
            marked,
            population={
                "class": {"disjoint": 0.3, "partial": 0.2, "agree_assigned": 0.5},
                "source": {"prokka": 0.4, "rast": 0.4, "unseen_tool": 0.2},
            },
        )
        assert all("agree_assigned" not in k for k in report.weight_cells)
        assert all("unseen_tool" not in k for k in report.weight_cells)


class TestSwapFlips:
    def test_a_stable_swap_pair_flips_nothing(self, tmp_path) -> None:
        manifest, items = material()
        ids = row_ids(manifest)
        results = results_for(manifest, items)
        T.write_pilot(results, manifest, items, tmp_path / "p.xlsx", tmp_path / "p.md")
        mark(tmp_path / "p.xlsx", {i: "R" for i in ids})
        gold = T.read_marks(tmp_path / "p.xlsx", manifest)
        report = C.report(
            results,
            gold,
            manifest,
            adjudicate_spec_hash=SPEC_HASH,
            roster_fingerprint=ROSTER,
        )
        assert report.swap.pairs > 0
        assert (report.swap.final_ids, report.swap.verdict) == (0, 0)
        assert report.swap.rate == 0.0
        lower, upper = report.swap.interval
        assert lower == 0.0 and upper > 0.0

    def test_the_three_flip_counts_are_separate(self, tmp_path) -> None:
        manifest, items = material()
        ids = row_ids(manifest)
        results = results_for(manifest, items, flip_swap=True)
        T.write_pilot(results, manifest, items, tmp_path / "p.xlsx", tmp_path / "p.md")
        mark(tmp_path / "p.xlsx", {i: "R" for i in ids})
        gold = T.read_marks(tmp_path / "p.xlsx", manifest)
        report = C.report(
            results,
            gold,
            manifest,
            adjudicate_spec_hash=SPEC_HASH,
            roster_fingerprint=ROSTER,
        )
        payload = report.swap.as_dict()
        assert payload["pairs"] == report.swap.pairs > 0
        assert payload["flipped_final_ids"] == report.swap.pairs
        assert payload["flipped_verdict"] == report.swap.pairs
        assert payload["flip_rate"] == 1.0
        assert set(payload) >= {
            "flipped_final_ids",
            "flipped_verdict",
            "flipped_off_menu",
            "wilson_lower",
            "wilson_upper",
        }

    def test_an_off_menu_flag_can_flip_without_the_verdict(self) -> None:
        """The three severities are three counts, not one with three names."""
        swap = C.SwapFlips(pairs=10, final_ids=0, verdict=0, off_menu=4)
        assert swap.rate == 0.0
        assert swap.as_dict()["flipped_off_menu"] == 4

    def test_no_pairs_is_no_rate(self) -> None:
        swap = C.SwapFlips(pairs=0, final_ids=0, verdict=0, off_menu=0)
        assert swap.rate is None
        assert swap.interval == (None, None)


# ---------------------------------------------------------------------------
# the markdown, and the ledger record
# ---------------------------------------------------------------------------


class TestMarkdown:
    def test_the_report_renders_every_section(self, marked) -> None:
        text = _report(marked).to_markdown()
        for heading in (
            "# go_mf adjudication calibration",
            "## Agreement",
            "### By class",
            "### By source tool",
            "### By favoured side (self-preference check)",
            "## Position swap",
            "## Counts",
        ):
            assert heading in text, heading
        assert "band:" in text
        assert "Wilson 95%" in text
        assert SPEC_HASH in text and ROSTER in text
        assert "population-weighted" in text


class TestLedgerRecord:
    def test_the_report_is_recorded_as_kind_calibration(self, tmp_path, marked) -> None:
        ledger = Ledger(tmp_path / "run")
        report = _report(marked)
        written = C.record(ledger, report)
        assert written["kind"] == "calibration"
        assert written["manifest_id"] == marked["manifest"].manifest_id
        assert written["adjudicate_spec_hash"] == SPEC_HASH
        assert written["roster_fingerprint"] == ROSTER
        assert written["band"] == report.band
        assert written["marks"] == dict(marked["gold"].marks)
        assert written["report"]["overall"]["n"] == report.overall.n
        assert [r["kind"] for r in ledger.records()] == ["calibration"]

    def test_the_recorded_band_and_bindings_satisfy_the_tranche_gate(
        self, tmp_path, marked_all_right
    ) -> None:
        """What the record is FOR: ``calibration_gate`` must accept it (C13)."""
        ledger = Ledger(tmp_path / "run")
        report = _report(marked_all_right)
        assert report.band == "conditional", report.band
        C.record(ledger, report)
        gate = A.calibration_gate(
            ledger,
            manifest_id=marked_all_right["manifest"].manifest_id,
            spec_hash=SPEC_HASH,
            roster_fingerprint=ROSTER,
            required_ids=marked_all_right["ids"],
        )
        assert gate["ok"] is True, gate["reason"]
        assert gate["band"] == report.band
        assert gate["marks"] == len(marked_all_right["ids"])

    def test_a_not_trusted_report_does_not_open_the_gate(self, tmp_path, marked) -> None:
        """The 3-right/3-wrong fixture: recorded, matched, and still refused."""
        ledger = Ledger(tmp_path / "run")
        report = _report(marked)
        assert report.band == "not_trusted", report.band
        C.record(ledger, report)
        gate = A.calibration_gate(
            ledger,
            manifest_id=marked["manifest"].manifest_id,
            spec_hash=SPEC_HASH,
            roster_fingerprint=ROSTER,
            required_ids=marked["ids"],
        )
        assert gate["ok"] is False
        assert "not_trusted" in gate["reason"]

    def test_a_record_bound_to_another_roster_does_not_open_the_gate(
        self, tmp_path, marked_all_right
    ) -> None:
        ledger = Ledger(tmp_path / "run")
        C.record(ledger, _report(marked_all_right))
        gate = A.calibration_gate(
            ledger,
            manifest_id=marked_all_right["manifest"].manifest_id,
            spec_hash=SPEC_HASH,
            roster_fingerprint="c" * 64,
            required_ids=marked_all_right["ids"],
        )
        assert gate["ok"] is False
        assert "no calibration record matches" in gate["reason"]


class TestSpotCheckImport:
    def test_marks_are_imported_without_a_full_report(self, tmp_path) -> None:
        """The spot-check path: a small table's marks straight into the ledger."""
        manifest, items = material()
        results = results_for(manifest, items)
        wanted = row_ids(manifest)[:3]
        T.write_spot_check(
            results, manifest, items, wanted, tmp_path / "s.xlsx", tmp_path / "s.md"
        )
        mark(tmp_path / "s.xlsx", {i: "R" for i in wanted})
        gold = T.read_marks(tmp_path / "s.xlsx", manifest)

        ledger = Ledger(tmp_path / "run")
        written = C.record_marks(
            ledger, gold, adjudicate_spec_hash=SPEC_HASH, roster_fingerprint=ROSTER
        )
        assert written["kind"] == "calibration"
        assert written["band"] == "conditional", "3 of 3 is not yet trusted"
        assert set(written["marks"]) == set(wanted)
        assert written["report"]["scope"] == "spot_check"

        gate = A.calibration_gate(
            ledger,
            manifest_id=manifest.manifest_id,
            spec_hash=SPEC_HASH,
            roster_fingerprint=ROSTER,
            required_ids=wanted,
        )
        assert gate["ok"] is True, gate["reason"]

    def test_an_unmarked_spot_check_row_is_refused(self, tmp_path) -> None:
        manifest, items = material()
        results = results_for(manifest, items)
        wanted = row_ids(manifest)[:3]
        T.write_spot_check(
            results, manifest, items, wanted, tmp_path / "s.xlsx", tmp_path / "s.md"
        )
        mark(tmp_path / "s.xlsx", {wanted[0]: "R"})
        gold = T.read_marks(tmp_path / "s.xlsx", manifest)
        with pytest.raises(Refused, match="no\n?\\s*mark|no mark"):
            C.record_marks(
                ledger := Ledger(tmp_path / "run"),
                gold,
                adjudicate_spec_hash=SPEC_HASH,
                roster_fingerprint=ROSTER,
            )
        assert ledger.records() == []

    def test_all_wrong_spot_check_marks_record_not_trusted(self, tmp_path) -> None:
        manifest, items = material()
        results = results_for(manifest, items)
        wanted = row_ids(manifest)[:3]
        T.write_spot_check(
            results, manifest, items, wanted, tmp_path / "s.xlsx", tmp_path / "s.md"
        )
        mark(tmp_path / "s.xlsx", {i: "W" for i in wanted})
        gold = T.read_marks(tmp_path / "s.xlsx", manifest)
        ledger = Ledger(tmp_path / "run")
        written = C.record_marks(
            ledger, gold, adjudicate_spec_hash=SPEC_HASH, roster_fingerprint=ROSTER
        )
        assert written["band"] == "not_trusted"
        gate = A.calibration_gate(
            ledger,
            manifest_id=manifest.manifest_id,
            spec_hash=SPEC_HASH,
            roster_fingerprint=ROSTER,
            required_ids=wanted,
        )
        assert gate["ok"] is False
        assert "not_trusted" in gate["reason"]
