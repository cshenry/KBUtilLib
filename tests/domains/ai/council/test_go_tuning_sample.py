"""``go_tuning.sample`` -- pools, determinism, pilot isolation, refusals.

:func:`build_manifest` is a pure function of ``(matched, seed, plan)``, so
these tests build :class:`MatchedItem` rows directly rather than through a
store.  The store-backed path is covered by
:mod:`test_go_tuning_conflicts` and :mod:`test_go_tuning_script`.

The exact apportionment numbers asserted below are arithmetic, not
observations: a largest-remainder split of a known population has one answer,
and pinning it is how "proportional by source tool" stops being a claim
nobody checks.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from kbutillib.domains.ai.council.go_tuning.conflicts import Refused
from kbutillib.domains.ai.council.go_tuning.sample import (
    POOLS,
    Manifest,
    Plan,
    build_manifest,
    largest_remainder,
)

from .go_tuning_support import hash_of

SEED = "20261117"


def item(
    label: str,
    kind: str,
    *,
    data: str = "d0",
    source: str = "tool_a",
    sub_kind: Optional[str] = None,
) -> Any:
    """One :class:`MatchedItem`, keyed on the ``label``/``data`` pair."""
    from kbutillib.domains.ai.council.go_tuning.conflicts import MatchedItem

    candidate = frozenset({"GO:1"}) if kind != "alcf_declined" else frozenset()
    baseline = frozenset({"GO:2"}) if kind != "gomf_declined" else frozenset()
    if kind == "agree_assigned":
        baseline = candidate
    if kind == "agree_declined":
        candidate = baseline = frozenset()
    return MatchedItem(
        entity_hash=hash_of("entity", label),
        data_hash=hash_of("data", data),
        item_id=f"{label}-{data}",
        candidate_job_id="j-cand",
        baseline_item_id=f"b-{label}-{data}",
        baseline_job_id="j-base",
        candidate_selected=candidate,
        baseline_selected=baseline,
        candidate_answers=1,
        baseline_answers=1,
        source=source,
        evidence_text=f"annotation {label}",
        menu=({"id": "GO:1", "label": "one"},),
        kind=kind,
        item_sub_kind=sub_kind,
    )


def many(n: int, kind: str, *, prefix: str, source: str = "tool_a") -> List[Any]:
    """*n* single-menu rows of one class and source, one entity each."""
    return [item(f"{prefix}{i:04d}", kind, source=source) for i in range(n)]


def _entities(manifest: Manifest, pool: str) -> frozenset:
    return manifest.entity_hashes(pool)


# ---------------------------------------------------------------------------
# apportionment primitive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("total", [0, 1, 7, 75, 150, 401])
def test_largest_remainder_sums_to_the_total_exactly(total: int) -> None:
    out = largest_remainder(total, {"a": 150.0, "b": 50.0, "c": 3.0})
    assert sum(out.values()) == total
    assert set(out) == {"a", "b", "c"}


def test_largest_remainder_breaks_ties_by_name_not_dict_order() -> None:
    forward = largest_remainder(75, {"tool_a": 100.0, "tool_b": 100.0})
    backward = largest_remainder(75, {"tool_b": 100.0, "tool_a": 100.0})
    assert forward == backward == {"tool_a": 38, "tool_b": 37}


def test_a_zero_weight_class_gets_nothing() -> None:
    assert largest_remainder(10, {"a": 1.0, "b": 0.0}) == {"a": 10, "b": 0}


# ---------------------------------------------------------------------------
# the refusal floor
# ---------------------------------------------------------------------------


def test_fewer_than_100_conflicts_refuses_and_quotes_the_extra_items_needed() -> None:
    matched = (
        many(60, "disjoint", prefix="d")
        + many(39, "partial", prefix="p")
        + many(101, "agree_assigned", prefix="a")
    )
    with pytest.raises(Refused) as exc:
        build_manifest(matched, SEED)
    message = str(exc.value)
    assert "only 99 disjoint+partial conflicts" in message
    assert "floor of 100" in message
    assert "49.5%" in message and "99/200" in message
    # The actionable number: more ALCF items, not more conflicts.
    assert "more ALCF items" in message
    assert "150-item pilot" in message


def test_exactly_100_conflicts_is_accepted() -> None:
    matched = many(50, "disjoint", prefix="d") + many(50, "partial", prefix="p")
    manifest = build_manifest(matched, SEED)
    assert manifest.report["conflicts_available"] == 100


def test_no_conflicts_at_all_refuses_without_extrapolating_a_rate() -> None:
    with pytest.raises(Refused) as exc:
        build_manifest(many(10, "agree_assigned", prefix="a"), SEED)
    assert "no rate to extrapolate from" in str(exc.value)


def test_a_custom_min_conflicts_moves_the_floor() -> None:
    matched = many(30, "disjoint", prefix="d") + many(30, "partial", prefix="p")
    manifest = build_manifest(matched, SEED, Plan(min_conflicts=60, swap_slice=5))
    assert manifest.report["conflicts_available"] == 60


# ---------------------------------------------------------------------------
# the pilot
# ---------------------------------------------------------------------------


@pytest.fixture
def mixed_sources() -> List[Any]:
    """400 conflicts: disjoint 150/50 by source, partial 100/100."""
    return (
        many(150, "disjoint", prefix="da", source="tool_a")
        + many(50, "disjoint", prefix="db", source="tool_b")
        + many(100, "partial", prefix="pa", source="tool_a")
        + many(100, "partial", prefix="pb", source="tool_b")
    )


def test_pilot_strata_match_the_plan(mixed_sources) -> None:
    manifest = build_manifest(mixed_sources, SEED)
    pilot = manifest.report["pools"]["pilot"]
    assert pilot["requested"] == 150
    assert pilot["eligible"] == 400
    assert pilot["rows"] == 150
    assert pilot["shortfall"] == 0
    # 75/75 by class weight, then largest-remainder by source within class.
    assert pilot["allocation"] == {"disjoint": 75, "partial": 75}
    assert pilot["strata"] == {
        "disjoint|tool_a": 56,
        "disjoint|tool_b": 19,
        "partial|tool_a": 38,
        "partial|tool_b": 37,
    }
    assert sum(pilot["strata"].values()) == 150


def test_a_short_class_contributes_all_and_the_other_fills_the_rest() -> None:
    matched = many(300, "disjoint", prefix="d") + many(20, "partial", prefix="p")
    manifest = build_manifest(matched, SEED)
    pilot = manifest.report["pools"]["pilot"]
    assert pilot["allocation"] == {"disjoint": 130, "partial": 20}
    assert pilot["rows"] == 150


def test_between_100_and_149_conflicts_shrinks_the_pilot_and_reports_the_shortfall() -> None:
    matched = many(60, "disjoint", prefix="d") + many(60, "partial", prefix="p")
    manifest = build_manifest(matched, SEED)
    pilot = manifest.report["pools"]["pilot"]
    assert pilot["rows"] == 120
    assert pilot["shortfall"] == 30
    # The whole conflict set IS the pilot at this size, so the split is empty.
    assert manifest.report["entries_per_pool"]["tuning"] == 0
    assert manifest.report["entries_per_pool"]["heldout"] == 0


def test_declined_classes_can_be_given_weight_by_plan_alone(mixed_sources) -> None:
    """The plan parameter exists so a decline tranche needs no code change."""
    matched = mixed_sources + many(100, "alcf_declined", prefix="ad")
    plan = Plan(
        pilot_classes=("disjoint", "partial", "alcf_declined"),
        class_weights={"disjoint": 1.0, "partial": 1.0, "alcf_declined": 1.0},
    )
    manifest = build_manifest(matched, SEED, plan)
    assert manifest.report["pools"]["pilot"]["allocation"] == {
        "disjoint": 50, "partial": 50, "alcf_declined": 50
    }
    assert {e["class"] for e in manifest.pool("pilot")} == {
        "disjoint", "partial", "alcf_declined"
    }


# ---------------------------------------------------------------------------
# pilot isolation: pools are assigned on entity_hash
# ---------------------------------------------------------------------------


@pytest.fixture
def three_menus_each() -> List[Any]:
    """100 entities x 3 menus = 300 conflict rows, so entity groups matter."""
    rows: List[Any] = []
    for i in range(100):
        for j, data in enumerate(("d0", "d1", "d2")):
            kind = "disjoint" if j % 2 == 0 else "partial"
            rows.append(item(f"e{i:04d}", kind, data=data))
    return rows


def test_no_entity_hash_appears_in_two_of_pilot_tuning_heldout(three_menus_each) -> None:
    manifest = build_manifest(three_menus_each, SEED)
    pilot = _entities(manifest, "pilot")
    tuning = _entities(manifest, "tuning")
    heldout = _entities(manifest, "heldout")
    assert pilot and tuning and heldout
    assert not pilot & tuning
    assert not pilot & heldout
    assert not tuning & heldout


def test_rows_sharing_a_pilot_entity_are_withheld_and_recorded(three_menus_each) -> None:
    manifest = build_manifest(three_menus_each, SEED)
    withheld = manifest.report["excluded_pilot_entity"]
    assert withheld["rows"] > 0
    assert withheld["entities"] > 0
    assert len(withheld["keys"]) == withheld["rows"]
    pilot = _entities(manifest, "pilot")
    assert {k[0] for k in withheld["keys"]} <= pilot


def test_one_entity_with_several_menus_crossing_the_pilot_boundary() -> None:
    """A rare source gives the multi-menu entity a pilot allocation of exactly
    1 of its 3 rows, so the boundary is crossed deterministically."""
    multi = [item("multi", "disjoint", data=d, source="tool_rare")
             for d in ("d0", "d1", "d2")]
    matched = (
        many(200, "disjoint", prefix="d")
        + multi
        + many(200, "partial", prefix="p")
    )
    manifest = build_manifest(matched, SEED)
    assert manifest.report["pools"]["pilot"]["strata"]["disjoint|tool_rare"] == 1
    multi_entity = hash_of("entity", "multi")
    placed = {
        pool: [e for e in manifest.pool(pool) if e["entity_hash"] == multi_entity]
        for pool in POOLS
    }
    assert len(placed["pilot"]) == 1
    assert placed["tuning"] == [] and placed["heldout"] == []
    withheld = manifest.report["excluded_pilot_entity"]
    assert [k for k in withheld["keys"] if k[0] == multi_entity] != []
    assert len([k for k in withheld["keys"] if k[0] == multi_entity]) == 2


def test_the_split_is_60_40_over_entity_groups(three_menus_each) -> None:
    manifest = build_manifest(three_menus_each, SEED)
    split = manifest.report["entity_split"]
    groups = split["entity_groups"]
    assert groups == split["tuning_entities"] + split["heldout_entities"]
    # 60/40 by ENTITY count, within one entity of exact (per-stratum rounding).
    assert abs(split["tuning_entities"] - 0.6 * groups) <= 1.5
    # Every menu of an entity travels with it.
    for pool in ("tuning", "heldout"):
        for entity in _entities(manifest, pool):
            rows = [r for r in three_menus_each if r.entity_hash == entity]
            assert len([e for e in manifest.pool(pool) if e["entity_hash"] == entity]) == len(rows)


def test_realised_row_and_stratum_counts_are_reported(three_menus_each) -> None:
    manifest = build_manifest(three_menus_each, SEED)
    for pool in ("pilot", "tuning", "heldout"):
        report = manifest.report["pools"][pool]
        assert sum(report["strata"].values()) == report["rows"]
        assert report["entities"] <= report["rows"]


# ---------------------------------------------------------------------------
# guard slices
# ---------------------------------------------------------------------------


@pytest.fixture
def with_guards(mixed_sources) -> List[Any]:
    return (
        mixed_sources
        + many(50, "agree_assigned", prefix="aa")
        + many(30, "agree_declined", prefix="ad")
        + many(20, "alcf_declined", prefix="al")
        + many(20, "gomf_declined", prefix="gm")
    )


def test_each_guard_slice_takes_min_requested_eligible_and_records_both(with_guards) -> None:
    manifest = build_manifest(with_guards, SEED)
    regression = manifest.report["pools"]["regression"]
    assert regression["requested"] == 200
    assert regression["eligible"] == 50
    assert regression["rows"] == 50
    assert regression["shortfall"] == 150
    guard = manifest.report["pools"]["decline_guard"]
    assert guard["requested"] == 400
    assert guard["rows"] == min(400, guard["eligible"])
    assert guard["shortfall"] == max(400 - guard["rows"], 0)


def test_the_regression_slice_is_agree_assigned_only(with_guards) -> None:
    manifest = build_manifest(with_guards, SEED)
    assert {e["class"] for e in manifest.pool("regression")} == {"agree_assigned"}


def test_the_decline_guard_is_apportioned_by_population_proportion(with_guards) -> None:
    """Weights are the POPULATION class counts, not the eligible ones.

    Here the conflict classes are entirely consumed by pilot/tuning/heldout
    and ``agree_assigned`` by the regression slice, so the 70 eligible rows
    are 30 ``agree_declined`` and 20 each of the two declines.  Apportioning
    40 over 30/20/20 is 17/12/11 -- the +1 goes to ``alcf_declined`` on the
    name tie-break, which is the only reason that number is not 11.
    """
    manifest = build_manifest(with_guards, SEED, Plan(decline_guard_size=40))
    guard = manifest.report["pools"]["decline_guard"]
    assert guard["eligible"] == 70
    assert guard["rows"] == 40
    assert guard["allocation"] == {
        "agree_declined": 17, "alcf_declined": 12, "gomf_declined": 11
    }
    assert sum(guard["allocation"].values()) == 40
    assert {e["class"] for e in manifest.pool("decline_guard")} == {
        "agree_declined", "alcf_declined", "gomf_declined"
    }


def test_the_decline_guard_may_draw_conflict_rows_withheld_from_the_split(
    three_menus_each,
) -> None:
    """A row withheld for sharing a pilot entity is still part of the
    population, so it stays eligible for the population guard -- excluding it
    would bias the guard towards entities nothing else wanted."""
    matched = three_menus_each + many(40, "agree_declined", prefix="ad")
    manifest = build_manifest(matched, SEED)
    guard = manifest.pool("decline_guard")
    conflict_rows = [e for e in guard if e["class"] in ("disjoint", "partial")]
    assert conflict_rows
    withheld = {tuple(k) for k in manifest.report["excluded_pilot_entity"]["keys"]}
    assert all(
        (e["entity_hash"], e["data_hash"]) in withheld for e in conflict_rows
    )


def test_pools_are_sampled_without_replacement(with_guards) -> None:
    manifest = build_manifest(with_guards, SEED)
    ids = [e["id"] for e in manifest.entries]
    assert len(ids) == len(set(ids))
    placements: Dict[tuple, set] = {}
    for entry in manifest.entries:
        placements.setdefault((entry["entity_hash"], entry["data_hash"]), set()).add(
            entry["pool"]
        )
    assert all(len(pools) == 1 for pools in placements.values())


def test_guard_entity_overlap_is_reported_rather_than_hidden(with_guards) -> None:
    manifest = build_manifest(with_guards, SEED)
    overlap = manifest.report["guard_entity_overlap"]
    assert set(overlap) == {
        f"{guard}_x_{pool}"
        for guard in ("regression", "decline_guard")
        for pool in ("pilot", "tuning", "heldout")
    }
    assert all(isinstance(v, int) for v in overlap.values())


# ---------------------------------------------------------------------------
# A/B orientation and the swap slice
# ---------------------------------------------------------------------------


def test_the_swap_slice_adds_an_opposite_orientation_twin(mixed_sources) -> None:
    manifest = build_manifest(mixed_sources, SEED)
    swapped = [e for e in manifest.entries if e["swap"]]
    assert len(swapped) == 30
    assert manifest.report["swap_slice"] == {
        "requested": 30, "eligible": 150, "selected": 30, "shortfall": 0
    }
    base_by_key = {
        (e["entity_hash"], e["data_hash"]): e
        for e in manifest.entries
        if not e["swap"]
    }
    for twin in swapped:
        assert twin["id"].endswith(":swap")
        assert twin["id"] == f"{twin['item_id']}:swap"
        assert twin["pool"] == "pilot"
        base = base_by_key[(twin["entity_hash"], twin["data_hash"])]
        assert twin["alcf_is_A"] is not base["alcf_is_A"]


def test_only_pilot_rows_are_swapped(mixed_sources) -> None:
    manifest = build_manifest(mixed_sources, SEED)
    assert {e["pool"] for e in manifest.entries if e["swap"]} == {"pilot"}


def test_ab_order_is_drawn_per_item_and_both_orientations_occur(mixed_sources) -> None:
    manifest = build_manifest(mixed_sources, SEED)
    orientations = {e["alcf_is_A"] for e in manifest.entries if not e["swap"]}
    assert orientations == {True, False}
    # Drawn from the item's own key, so adding a row elsewhere cannot
    # re-orient it.
    extended = build_manifest(mixed_sources + many(5, "disjoint", prefix="zz"), SEED)
    before = {e["item_id"]: e["alcf_is_A"] for e in manifest.entries if not e["swap"]}
    after = {e["item_id"]: e["alcf_is_A"] for e in extended.entries if not e["swap"]}
    shared = set(before) & set(after)
    assert shared
    assert all(before[k] == after[k] for k in shared)


def test_a_swap_slice_larger_than_the_pilot_is_capped() -> None:
    matched = many(60, "disjoint", prefix="d") + many(60, "partial", prefix="p")
    manifest = build_manifest(matched, SEED, Plan(swap_slice=500))
    assert manifest.report["swap_slice"]["selected"] == 120
    assert manifest.report["swap_slice"]["shortfall"] == 380


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


def test_the_same_seed_gives_a_byte_identical_manifest(mixed_sources) -> None:
    first = build_manifest(mixed_sources, SEED)
    second = build_manifest(list(reversed(mixed_sources)), SEED)
    assert first.to_json() == second.to_json()
    assert first.manifest_id == second.manifest_id


def test_a_different_seed_gives_a_different_sample(mixed_sources) -> None:
    first = build_manifest(mixed_sources, SEED)
    other = build_manifest(mixed_sources, "20261118")
    assert first.manifest_id != other.manifest_id
    assert first.to_json() != other.to_json()
    # Same plan, so the SHAPE is identical even though the rows differ.
    assert first.report["entries_per_pool"] == other.report["entries_per_pool"]


def test_the_manifest_round_trips_to_a_file_byte_identically(tmp_path, mixed_sources) -> None:
    manifest = build_manifest(mixed_sources, SEED)
    first = manifest.write(tmp_path / "a" / "manifest.json")
    second = build_manifest(mixed_sources, SEED).write(tmp_path / "b" / "manifest.json")
    assert first.read_bytes() == second.read_bytes()


def test_the_manifest_records_the_seed_the_plan_and_the_code_commit(mixed_sources) -> None:
    manifest = build_manifest(mixed_sources, SEED, Plan(pilot_size=140))
    assert manifest.seed == SEED
    assert manifest.plan["pilot_size"] == 140
    assert manifest.plan["class_weights"] == {"disjoint": 0.5, "partial": 0.5}
    assert set(manifest.code_version) == {"package_version", "git_commit", "git_dirty"}
    data = manifest.as_dict()
    assert data["manifest_id"] == manifest.manifest_id
    assert len(data["entries"]) == len(manifest.entries)


def test_every_entry_carries_the_fields_the_manifest_promises(mixed_sources) -> None:
    manifest = build_manifest(mixed_sources, SEED)
    for entry in manifest.entries:
        assert set(entry) == {
            "id", "item_id", "entity_hash", "data_hash", "class", "sub_kind",
            "source", "pool", "alcf_is_A", "swap", "candidate_job_id",
            "baseline_item_id", "baseline_job_id",
        }
        assert entry["pool"] in POOLS
        assert isinstance(entry["alcf_is_A"], bool)
        assert isinstance(entry["swap"], bool)


# ---------------------------------------------------------------------------
# execution order
# ---------------------------------------------------------------------------


def test_execution_order_covers_every_entry_of_every_tranche(mixed_sources) -> None:
    manifest = build_manifest(mixed_sources, SEED)
    for pool in POOLS:
        order = manifest.execution_order[pool]
        ids = [e["id"] for e in manifest.pool(pool)]
        assert sorted(order) == sorted(ids)


def test_execution_order_interleaves_strata_rather_than_concatenating(mixed_sources) -> None:
    manifest = build_manifest(mixed_sources, SEED)
    by_id = {e["id"]: e for e in manifest.pool("tuning")}
    order = manifest.execution_order["tuning"]
    strata = [f"{by_id[i]['class']}|{by_id[i]['source']}" for i in order]
    changes = sum(1 for a, b in zip(strata, strata[1:]) if a != b)
    n_strata = len(set(strata))
    assert n_strata == 4
    # A concatenation would change stratum exactly n_strata - 1 times.
    assert changes > 3 * n_strata
    # And every stratum appears in the first fifth of the run.
    head = set(strata[: max(len(strata) // 5, 1)])
    assert head == set(strata)


def test_execution_order_is_stable_under_the_same_seed(mixed_sources) -> None:
    first = build_manifest(mixed_sources, SEED)
    second = build_manifest(mixed_sources, SEED)
    assert first.execution_order == second.execution_order


def test_extract_diagnostics_say_so_when_they_cannot_be_carried(mixed_sources) -> None:
    """A manifest sampled from a bare conflict list must not look like a
    manifest whose extractor found zero unstable keys."""
    manifest = build_manifest(mixed_sources, SEED)
    diagnostics = manifest.report["extract_diagnostics"]
    assert diagnostics["carried"] is False
    assert "NOT known" in diagnostics["note"]
    assert "unstable" not in diagnostics


def test_extract_diagnostics_are_carried_from_a_match_result(mixed_sources) -> None:
    from kbutillib.domains.ai.council.go_tuning.conflicts import MatchResult

    result = MatchResult(
        items=tuple(mixed_sources),
        unstable={("e1", "d1"): "unstable_gomf", ("e2", "d1"): "unstable_alcf"},
        excluded={"candidate:error": 3},
        candidate_only=(("e3", "d1"),),
        baseline_only=7,
        counts={},
    )
    manifest = build_manifest(result, SEED)
    diagnostics = manifest.report["extract_diagnostics"]
    assert diagnostics["carried"] is True
    assert diagnostics["unstable"] == {
        "count": 2, "by_side": {"unstable_alcf": 1, "unstable_gomf": 1}
    }
    assert diagnostics["excluded_rows"] == {"candidate:error": 3}
    assert diagnostics["candidate_only_keys"] == 1
    assert diagnostics["baseline_only_keys"] == 7
    # Sampling a MatchResult and sampling its items must draw the SAME rows.
    assert [e["id"] for e in manifest.entries] == [
        e["id"] for e in build_manifest(mixed_sources, SEED).entries
    ]
