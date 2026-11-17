"""``go_tuning.conflicts`` -- matching by key, admission, coalescing, classes.

Every test here runs against REAL ``SqliteStore`` files (two of them), because
the behaviour under test is a join between two stores and the interesting
failures -- an id that matches nothing, a cancelled row read as an answer, a
retry preferred over its original -- all live in the reader.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

pytest.importorskip("sqlalchemy")

from kbutillib.domains.ai.council.go_tuning import conflicts as C  # noqa: E402

from .go_tuning_support import hash_of, make_store, row, two_stores  # noqa: E402


def _job(job_id: str, rows, query=("go_mf", "4")) -> Dict[str, Any]:
    return {"job_id": job_id, "query": query, "rows": list(rows)}


def _by_entity(result: C.MatchResult) -> Dict[str, C.MatchedItem]:
    """Index the result by entity LABEL, so assertions read in test terms."""
    return {
        label: item
        for label, item in (
            (lbl, it)
            for it in result.items
            for lbl in [_label_of(it.entity_hash)]
            if lbl is not None
        )
    }


_LABELS = (
    "agree_assigned", "agree_declined", "alcf_declined", "gomf_declined",
    "disjoint", "partial_alcf_sup", "partial_gomf_sup", "partial_crossed",
    "dup", "h100", "unstable", "only_cand", "only_base", "no_verdict",
    "errored", "pending",
)
_BY_HASH = {hash_of("entity", lbl): lbl for lbl in _LABELS}


def _label_of(entity_hash: str):
    return _BY_HASH.get(entity_hash)


# ---------------------------------------------------------------------------
# classification, as a pure function
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "candidate,baseline,expected,expected_sub",
    [
        (["GO:1"], ["GO:1"], "agree_assigned", None),
        ([], [], "agree_declined", None),
        ([], ["GO:1"], "alcf_declined", None),
        (["GO:1"], [], "gomf_declined", None),
        (["GO:1"], ["GO:2"], "disjoint", None),
        (["GO:1", "GO:2"], ["GO:1"], "partial", "alcf_superset"),
        (["GO:1"], ["GO:1", "GO:2"], "partial", "gomf_superset"),
        (["GO:1", "GO:2"], ["GO:1", "GO:3"], "partial", "crossed"),
    ],
)
def test_classify_returns_exactly_one_class_and_the_right_sub_kind(
    candidate, baseline, expected, expected_sub
) -> None:
    matched = {"candidate_selected": candidate, "baseline_selected": baseline}
    assert C.classify(matched) == expected
    assert C.sub_kind(matched) == expected_sub
    assert C.classify(matched) in C.CLASSES


def test_sub_kind_is_none_off_a_partial_and_a_sub_kind_otherwise() -> None:
    assert C.sub_kind({"candidate_selected": [], "baseline_selected": []}) is None
    assert C.sub_kind(
        {"candidate_selected": ["GO:1", "GO:2"], "baseline_selected": ["GO:2"]}
    ) in C.PARTIAL_SUB_KINDS


# ---------------------------------------------------------------------------
# the six classes end to end, over two stores
# ---------------------------------------------------------------------------

_SIX_CLASSES = (
    # (entity label, candidate selection, baseline selection)
    ("agree_assigned", ["GO:1"], ["GO:1"]),
    ("agree_declined", [], []),
    ("alcf_declined", [], ["GO:1"]),
    ("gomf_declined", ["GO:1"], []),
    ("disjoint", ["GO:1"], ["GO:2"]),
    ("partial_alcf_sup", ["GO:1", "GO:2"], ["GO:1"]),
    ("partial_gomf_sup", ["GO:1"], ["GO:1", "GO:2"]),
    ("partial_crossed", ["GO:1", "GO:2"], ["GO:1", "GO:3"]),
)


@pytest.fixture
def six_classes(tmp_path):
    cand = [
        row(f"c{i:02d}", label, selected=sel)
        for i, (label, sel, _) in enumerate(_SIX_CLASSES)
    ]
    base = [
        row(f"b{i:02d}", label, selected=sel)
        for i, (label, _, sel) in enumerate(_SIX_CLASSES)
    ]
    curl, cjobs, burl, bjobs = two_stores(
        tmp_path, [_job("j-cand", cand)], [_job("j-base", base)]
    )
    return C.match(curl, cjobs, burl, bjobs)


def test_all_six_classes_are_found_and_counted(six_classes) -> None:
    result = six_classes
    assert len(result.items) == len(_SIX_CLASSES)
    assert result.counts == {
        "agree_assigned": 1,
        "agree_declined": 1,
        "alcf_declined": 1,
        "gomf_declined": 1,
        "disjoint": 1,
        "partial": 3,
        "unstable_alcf": 0,
        "unstable_gomf": 0,
    }
    # Every class in CLASSES is reported, present or not.
    assert set(C.CLASSES) <= set(result.counts)


def test_each_item_carries_its_class_sub_kind_and_both_provenances(six_classes) -> None:
    items = _by_entity(six_classes)
    assert items["disjoint"].kind == "disjoint"
    assert items["partial_alcf_sup"].item_sub_kind == "alcf_superset"
    assert items["partial_gomf_sup"].item_sub_kind == "gomf_superset"
    assert items["partial_crossed"].item_sub_kind == "crossed"
    assert items["agree_assigned"].item_sub_kind is None
    one = items["disjoint"]
    assert one.candidate_job_id == "j-cand" and one.baseline_job_id == "j-base"
    assert one.item_id.startswith("c") and one.baseline_item_id.startswith("b")
    assert one.source == "tool_a"
    assert [m["id"] for m in one.menu] == ["GO:1", "GO:2"]


def test_conflicts_are_disjoint_and_partial_only(six_classes) -> None:
    assert {i.kind for i in six_classes.conflicts} == {"disjoint", "partial"}
    assert len(six_classes.conflicts) == 4


# ---------------------------------------------------------------------------
# C4: the key is (entity_hash, data_hash) and item_id is ignored
# ---------------------------------------------------------------------------


def test_item_ids_differing_between_stores_still_match_by_key(tmp_path) -> None:
    """The real shape: Bench A's 12-hex id is a PREFIX of the entity hash, so
    it never equals the baseline's id for the same protein."""
    entity = hash_of("entity", "disjoint")
    curl, cjobs, burl, bjobs = two_stores(
        tmp_path,
        [_job("j-cand", [row(entity[:12], "disjoint", selected=["GO:1"])])],
        [_job("j-base", [row("go-mf-item-98765", "disjoint", selected=["GO:2"])])],
    )
    result = C.match(curl, cjobs, burl, bjobs)
    assert len(result.items) == 1
    item = result.items[0]
    assert item.item_id == entity[:12]
    assert item.baseline_item_id == "go-mf-item-98765"
    assert item.kind == "disjoint"
    assert not result.candidate_only and not result.baseline_only


def test_a_different_data_hash_is_a_different_key(tmp_path) -> None:
    """Two menus for one protein are two items, not a hash mismatch."""
    curl, cjobs, burl, bjobs = two_stores(
        tmp_path,
        [_job("j-cand", [
            row("c1", "dup", data="d0", selected=["GO:1"]),
            row("c2", "dup", data="d1", selected=["GO:1"]),
        ])],
        [_job("j-base", [
            row("b1", "dup", data="d0", selected=["GO:2"]),
            row("b2", "dup", data="d1", selected=["GO:1"]),
        ])],
    )
    result = C.match(curl, cjobs, burl, bjobs)
    assert {i.kind for i in result.items} == {"disjoint", "agree_assigned"}
    assert len({i.entity_hash for i in result.items}) == 1
    assert len({i.data_hash for i in result.items}) == 2


def test_one_entity_across_several_baseline_jobs_coalesces_to_one_answer(tmp_path) -> None:
    """~29 baseline rows per entity is the measured production shape.  All that
    agree collapse to one, keeping the lexicographically lowest provenance."""
    curl, cjobs, burl, bjobs = two_stores(
        tmp_path,
        [_job("j-cand", [row("c1", "dup", selected=["GO:1"])])],
        [
            _job("j-base-9", [row("zzz", "dup", selected=["GO:2"])]),
            _job("j-base-1", [
                row("b-aa", "dup", selected=["GO:2"]),
                row("b-zz", "dup", selected=["GO:2"]),
            ]),
        ],
    )
    result = C.match(curl, cjobs, burl, bjobs)
    assert len(result.items) == 1
    item = result.items[0]
    assert item.baseline_answers == 3
    assert item.candidate_answers == 1
    assert item.duplicate_count == 2
    # Lowest (job_id, item_id): j-base-1 sorts before j-base-9, then b-aa.
    assert (item.baseline_job_id, item.baseline_item_id) == ("j-base-1", "b-aa")
    assert item.kind == "disjoint"


def test_a_duplicated_candidate_entity_also_coalesces(tmp_path) -> None:
    curl, cjobs, burl, bjobs = two_stores(
        tmp_path,
        [_job("j-cand", [
            row("c-zz", "dup", selected=["GO:1"]),
            row("c-aa", "dup", selected=["GO:1"]),
        ])],
        [_job("j-base", [row("b1", "dup", selected=["GO:1"])])],
    )
    result = C.match(curl, cjobs, burl, bjobs)
    assert len(result.items) == 1
    assert result.items[0].candidate_answers == 2
    assert result.items[0].item_id == "c-aa"
    assert result.items[0].kind == "agree_assigned"


# ---------------------------------------------------------------------------
# C3: admission is item status AND verdict status ok; the rest is REPORTED
# ---------------------------------------------------------------------------


def test_error_pending_and_no_verdict_rows_are_excluded_and_reported(tmp_path) -> None:
    curl, cjobs, burl, bjobs = two_stores(
        tmp_path,
        [_job("j-cand", [
            row("c1", "disjoint", selected=["GO:1"]),
            row("c2", "errored", status="error"),
            row("c3", "pending", status="pending"),
            row("c4", "no_verdict", with_verdict=False),
            row("c5", "agree_declined", verdict_status="error"),
        ])],
        [_job("j-base", [
            row("b1", "disjoint", selected=["GO:2"]),
            row("b2", "errored", selected=["GO:1"]),
            row("b3", "pending", selected=["GO:1"]),
            row("b4", "no_verdict", selected=["GO:1"]),
            row("b5", "agree_declined", selected=["GO:1"]),
        ])],
    )
    result = C.match(curl, cjobs, burl, bjobs)
    assert [i.item_id for i in result.items] == ["c1"]
    assert result.excluded == {
        "candidate:error": 1,
        "candidate:no_verdict": 1,
        "candidate:pending": 1,
        "candidate:verdict:error": 1,
    }
    # The four baseline rows those keys would have matched are counted as
    # baseline-only, not silently dropped.
    assert result.baseline_only == 4


def test_the_measured_h100_shape_drops_cancelled_rows_and_go_mf3(tmp_path) -> None:
    """Entity 009948a3814c on h100: a cancelled go_mf@3 job AND a cancelled
    go_mf@4 job sit beside the one good go_mf@4 answer."""
    curl, cjobs, burl, bjobs = two_stores(
        tmp_path,
        [_job("j-cand", [row("c1", "h100", selected=["GO:2"])])],
        [
            _job("j-v3", [row("b-v3", "h100", selected=["GO:9"], status="cancelled")],
                 query=("go_mf", "3")),
            _job("j-v4-cancelled", [row("b-x", "h100", selected=["GO:9"], status="cancelled")]),
            _job("j-v4-good", [row("b-good", "h100", selected=["GO:1"])]),
        ],
    )
    # Baseline jobs DISCOVERED, which is how the production baseline is named.
    result = C.match(curl, cjobs, burl, None)
    assert set(result.baseline_job_ids) == {"j-v4-cancelled", "j-v4-good"}
    assert len(result.items) == 1
    item = result.items[0]
    assert item.baseline_job_id == "j-v4-good"
    assert item.baseline_selected == frozenset({"GO:1"})
    assert item.baseline_answers == 1
    assert item.kind == "disjoint"
    # The go_mf@3 job is never read, so its row is not in `excluded`; the
    # cancelled go_mf@4 row is.
    assert result.excluded == {"baseline:cancelled": 1}


def test_discovery_ignores_jobs_of_other_query_types(tmp_path) -> None:
    curl, cjobs, burl, _ = two_stores(
        tmp_path,
        [_job("j-cand", [row("c1", "disjoint", selected=["GO:1"])])],
        [
            _job("j-other", [row("b-other", "disjoint", selected=["GO:5"])],
                 query=("go_term_mapping", "3")),
            _job("j-v4", [row("b1", "disjoint", selected=["GO:2"])]),
        ],
    )
    result = C.match(curl, cjobs, burl, None)
    assert result.baseline_job_ids == ("j-v4",)
    assert result.items[0].baseline_selected == frozenset({"GO:2"})


# ---------------------------------------------------------------------------
# C4: a side that disagrees with ITSELF is unstable, not a conflict
# ---------------------------------------------------------------------------


def test_disagreeing_baseline_answers_become_unstable_gomf(tmp_path) -> None:
    curl, cjobs, burl, bjobs = two_stores(
        tmp_path,
        [_job("j-cand", [row("c1", "unstable", selected=["GO:1"])])],
        [
            _job("j-base-1", [row("b1", "unstable", selected=["GO:2"])]),
            _job("j-base-2", [row("b2", "unstable", selected=["GO:3"])]),
        ],
    )
    result = C.match(curl, cjobs, burl, bjobs)
    assert result.items == ()
    key = (hash_of("entity", "unstable"), hash_of("data", "d0"))
    assert result.unstable == {key: "unstable_gomf"}
    assert result.counts["unstable_gomf"] == 1
    assert result.counts["unstable_alcf"] == 0
    assert all(result.counts[cls] == 0 for cls in C.CLASSES)


def test_disagreeing_candidate_answers_become_unstable_alcf(tmp_path) -> None:
    curl, cjobs, burl, bjobs = two_stores(
        tmp_path,
        [
            _job("j-cand-1", [row("c1", "unstable", selected=["GO:1"])]),
            _job("j-cand-2", [row("c2", "unstable", selected=["GO:7"])]),
        ],
        [_job("j-base", [row("b1", "unstable", selected=["GO:2"])])],
    )
    result = C.match(curl, cjobs, burl, bjobs)
    assert result.items == ()
    assert set(result.unstable.values()) == {"unstable_alcf"}


def test_both_sides_unstable_is_labelled_for_the_side_under_test(tmp_path) -> None:
    curl, cjobs, burl, bjobs = two_stores(
        tmp_path,
        [
            _job("j-cand-1", [row("c1", "unstable", selected=["GO:1"])]),
            _job("j-cand-2", [row("c2", "unstable", selected=["GO:7"])]),
        ],
        [
            _job("j-base-1", [row("b1", "unstable", selected=["GO:2"])]),
            _job("j-base-2", [row("b2", "unstable", selected=["GO:3"])]),
        ],
    )
    result = C.match(curl, cjobs, burl, bjobs)
    assert set(result.unstable.values()) == {"unstable_alcf"}


def test_agreeing_answers_from_several_jobs_are_not_unstable(tmp_path) -> None:
    curl, cjobs, burl, bjobs = two_stores(
        tmp_path,
        [_job("j-cand", [row("c1", "dup", selected=["GO:1"])])],
        [
            _job("j-base-1", [row("b1", "dup", selected=["GO:2"])]),
            _job("j-base-2", [row("b2", "dup", selected=["GO:2"])]),
        ],
    )
    result = C.match(curl, cjobs, burl, bjobs)
    assert result.unstable == {}
    assert result.items[0].baseline_answers == 2


# ---------------------------------------------------------------------------
# one-sided keys and refusals
# ---------------------------------------------------------------------------


def test_one_sided_keys_are_counted_not_matched(tmp_path) -> None:
    curl, cjobs, burl, bjobs = two_stores(
        tmp_path,
        [_job("j-cand", [
            row("c1", "disjoint", selected=["GO:1"]),
            row("c2", "only_cand", selected=["GO:1"]),
        ])],
        [_job("j-base", [
            row("b1", "disjoint", selected=["GO:2"]),
            row("b2", "only_base", selected=["GO:1"]),
        ])],
    )
    result = C.match(curl, cjobs, burl, bjobs)
    assert len(result.items) == 1
    assert result.candidate_only == ((hash_of("entity", "only_cand"), hash_of("data", "d0")),)
    assert result.baseline_only == 1


def test_a_named_baseline_job_that_is_not_go_mf4_refuses(tmp_path) -> None:
    curl, cjobs, burl, bjobs = two_stores(
        tmp_path,
        [_job("j-cand", [row("c1", "disjoint", selected=["GO:1"])])],
        [_job("j-v3", [row("b1", "disjoint", selected=["GO:2"])], query=("go_mf", "3"))],
    )
    with pytest.raises(C.Refused) as exc:
        C.match(curl, cjobs, burl, bjobs)
    assert "go_mf@4" in str(exc.value)
    assert exc.value.ids == ["j-v3"]


def test_a_baseline_job_id_that_is_not_in_the_store_refuses(tmp_path) -> None:
    curl, cjobs, burl, _ = two_stores(
        tmp_path,
        [_job("j-cand", [row("c1", "disjoint", selected=["GO:1"])])],
        [_job("j-base", [row("b1", "disjoint", selected=["GO:2"])])],
    )
    with pytest.raises(C.Refused) as exc:
        C.match(curl, cjobs, burl, ["no-such-job"])
    assert exc.value.ids == ["no-such-job"]


def test_match_accepts_an_already_open_store(tmp_path) -> None:
    """The CLI passes URLs; a caller holding a store must not have to reopen it."""
    store, url = make_store(
        tmp_path / "one.sqlite",
        [
            _job("j-cand", [row("c1", "disjoint", selected=["GO:1"])],
                 query=("go_mf", "5")),
            _job("j-base", [row("b1", "disjoint", selected=["GO:2"])]),
        ],
    )
    try:
        result = C.match(store, ["j-cand"], store, ["j-base"])
        assert len(result.items) == 1 and result.items[0].kind == "disjoint"
        # Still usable: match must not have closed a store it did not open.
        assert store.get_job("j-cand") is not None
    finally:
        store.close()


def test_as_dict_is_json_ready_and_sorted(six_classes) -> None:
    import json

    item = _by_entity(six_classes)["partial_crossed"]
    data = item.as_dict()
    assert data["candidate_selected"] == ["GO:1", "GO:2"]
    assert data["baseline_selected"] == ["GO:1", "GO:3"]
    assert data["class"] == "partial" and data["sub_kind"] == "crossed"
    assert json.loads(json.dumps(data)) == data


# ---------------------------------------------------------------------------
# the legacy matcher is still here and still refuses
# ---------------------------------------------------------------------------


def test_match_items_still_pairs_on_item_id_and_refuses_duplicates() -> None:
    cand = [{"item_id": "i1", "entity_hash": "a", "data_hash": "b", "status": "ok",
             "verdict": {"status": "ok", "selected_ids": ["GO:1"]}}]
    base_row = dict(cand[0])
    job = {"job_id": "B1", "query_name": "go_mf", "query_version": "4",
           "items": [base_row]}
    out = C.match_items(cand, [job])
    assert list(out["pairs"]) == ["i1"]
    dup = {"job_id": "B2", "query_name": "go_mf", "query_version": "4",
           "items": [base_row]}
    with pytest.raises(C.Refused):
        C.match_items(cand, [job, dup])


def test_a_candidate_job_id_that_is_not_in_the_store_refuses(tmp_path) -> None:
    """A typo'd candidate job must not read as "the bench disagreed with
    nothing" -- an empty result is the one answer that looks like success."""
    curl, _, burl, bjobs = two_stores(
        tmp_path,
        [_job("j-cand", [row("c1", "disjoint", selected=["GO:1"])])],
        [_job("j-base", [row("b1", "disjoint", selected=["GO:2"])])],
    )
    with pytest.raises(C.Refused) as exc:
        C.match(curl, ["j-cnad"], burl, bjobs)
    assert exc.value.ids == ["j-cnad"]
    assert "candidate job not found" in str(exc.value)


def test_decided_by_and_decline_reason_are_carried_for_both_sides(tmp_path) -> None:
    """A decline a council reached and one a deterministic rule produced are
    not the same disagreement, so the manifest's source data must tell them
    apart."""
    curl, cjobs, burl, bjobs = two_stores(
        tmp_path,
        [_job("j-cand", [row("c1", "alcf_declined", selected=[],
                             decided_by="deterministic_empty",
                             decline_reason="nothing on the menu")])],
        [_job("j-base", [row("b1", "alcf_declined", selected=["GO:1"],
                             decided_by="meta")])],
    )
    result = C.match(curl, cjobs, burl, bjobs)
    item = result.items[0]
    assert item.kind == "alcf_declined"
    assert item.candidate_decided_by == "deterministic_empty"
    assert item.baseline_decided_by == "meta"
    assert item.candidate_decline_reason == "nothing on the menu"
    assert item.baseline_decline_reason is None
    data = item.as_dict()
    assert data["candidate_decline_reason"] == "nothing on the menu"
    assert data["baseline_decided_by"] == "meta"
