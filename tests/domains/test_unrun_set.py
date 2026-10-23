"""``unrun_set`` -- the un-run derivation, tested as the pure function it is.

NO CLEARINGHOUSE, NO COUNCIL, NO NUMPY. That is the whole point of
:mod:`kbutillib.domains.genome.ontology_mapping.unrun` being a separate module
from ``ontology_mapping_utils`` (which imports ``go_text``, and with it
numpy/scikit-learn/joblib): the partition arithmetic is the part with a known
recorded answer, so it must be testable on any host.

The recorded answer is the 2026-10-20 run over the ADP1+PMI priority set:
20,036 distinct definitions = 19,276 submit + 760 abstain, the two disjoint.
The corpus itself is not in this repository, so the fixture here reproduces
its SHAPE at those exact counts (see :class:`TestTheRecordedPartition`); what
is being pinned is the arithmetic and the disjointness, which is what went
wrong -- the 760 were dropped and read as an unexplained coverage gap.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from kbutillib.domains.genome.ontology_mapping.unrun import (
    UNRUN_PARTITIONS,
    done_hashes,
    unrun_items,
    unrun_set,
)

#: A parameter-set hash shape, so a `done` row looks like a real one.
PSH_A = "a" * 64
PSH_B = "b" * 64


def _hash(n: int) -> str:
    """A 64-char lowercase-hex entity hash, distinct per ``n``."""
    return f"{n:064x}"


def _universe(*rows: Any) -> List[Dict[str, Any]]:
    """``function_content``-shaped universe rows: ``entity_hash`` + ``definition``."""
    return [
        {
            "entity_hash": entity_hash,
            "definition": definition,
            "standardizer_version": "1.0.0",
        }
        for entity_hash, definition in rows
    ]


def _done(*entity_hashes: str, parameter_set_hash: str = PSH_A) -> List[Dict[str, Any]]:
    """``current_state("function")``-shaped rows for the given hashes."""
    return [
        {
            "entity_hash": entity_hash,
            "entity_type": "function",
            "result_type": "go_molecular_function",
            "source": "council/go_mf@1",
            "parameter_set_hash": parameter_set_hash,
        }
        for entity_hash in entity_hashes
    ]


def _ranked(*rows: Any) -> Dict[str, Any]:
    """``function_to_go``-shaped output.

    Each row is ``(query_id, text, abstain_reason_or_None)``; a non-abstained
    row gets one plausible candidate, which is all the partition cares about.
    """
    results = []
    for query_id, text, reason in rows:
        candidates = (
            []
            if reason is not None
            else [
                {
                    "go_id": "GO:0003824",
                    "name": "catalytic activity",
                    "def": "Catalysis of a biochemical reaction.",
                    "score": 0.5,
                    "height": 3,
                    "rank": 1,
                }
            ]
        )
        results.append(
            {
                "query_id": query_id,
                "text": text,
                "source": "",
                "abstained": reason is not None,
                "abstain_reason": reason,
                "candidates": candidates,
            }
        )
    return {"mapping_hash": "f" * 64, "mapping_version": "test", "results": results}


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


class TestShape:
    def test_it_returns_exactly_the_three_partitions(self) -> None:
        universe = _universe((_hash(1), "glucokinase"))
        parts = unrun_set(universe, [], _ranked((_hash(1), "glucokinase", None)))
        assert set(parts) == set(UNRUN_PARTITIONS) == {"submit", "abstain", "fanout"}

    def test_a_submitted_item_carries_the_join_key_and_its_menu(self) -> None:
        universe = _universe((_hash(1), "glucokinase"))
        parts = unrun_set(universe, [], _ranked((_hash(1), "glucokinase", None)))
        (item,) = parts["submit"]
        # query_id IS the clearinghouse entity_hash, carried through unchanged.
        # Measured 2026-10-21: the council's own items.entity_hash matched 0 of
        # 19,268 clearinghouse hashes; the definition text matched all of them.
        assert item["query_id"] == _hash(1)
        assert item["text"] == "glucokinase"
        assert item["item_id"] == "t0"
        assert item["candidates"][0]["go_id"] == "GO:0003824"

    def test_an_empty_universe_partitions_to_three_empty_lists(self) -> None:
        parts = unrun_set([], [], _ranked())
        assert parts == {"submit": [], "abstain": [], "fanout": []}


# ---------------------------------------------------------------------------
# Criterion: the abstained set is in `abstain` and never in `submit`
# ---------------------------------------------------------------------------


class TestAbstainIsRecordedAndNeverSubmitted:
    def test_an_abstained_function_is_in_abstain_and_not_in_submit(self) -> None:
        universe = _universe(
            (_hash(1), "glucokinase"),
            (_hash(2), "hypothetical protein"),
        )
        ranked = _ranked(
            (_hash(1), "glucokinase", None),
            (_hash(2), "hypothetical protein", "placeholder function"),
        )
        parts = unrun_set(universe, [], ranked)
        assert [row["query_id"] for row in parts["submit"]] == [_hash(1)]
        assert [row["query_id"] for row in parts["abstain"]] == [_hash(2)]

    def test_the_decline_reason_is_already_in_the_published_spelling(self) -> None:
        universe = _universe((_hash(2), "hypothetical protein"))
        ranked = _ranked((_hash(2), "hypothetical protein", "placeholder function"))
        (row,) = unrun_set(universe, [], ranked)["abstain"]
        assert row["abstain_reason"] == "placeholder function"
        assert row["decline_reason"] == "abstained:placeholder function"

    def test_no_surviving_candidate_is_a_decline_not_an_empty_menu(self) -> None:
        """A row retrieval did not flag but left nothing to choose from.

        For the council this is the same outcome as an abstention -- there is
        no menu to pick from -- so it must be recorded as a decline rather
        than submitted with an empty menu.
        """
        universe = _universe((_hash(3), "very general activity"))
        ranked = _ranked((_hash(3), "very general activity", None))
        ranked["results"][0]["candidates"] = []
        parts = unrun_set(universe, [], ranked)
        assert parts["submit"] == []
        assert parts["abstain"][0]["decline_reason"].startswith("abstained:")

    def test_an_abstained_function_is_not_in_fanout_either(self) -> None:
        universe = _universe(
            (_hash(1), "glucokinase"),
            (_hash(2), "hypothetical protein"),
        )
        ranked = _ranked(
            (_hash(1), "glucokinase", None),
            (_hash(2), "hypothetical protein", "placeholder function"),
        )
        parts = unrun_set(universe, [], ranked)
        assert [row["query_id"] for row in parts["fanout"]] == [_hash(1)]


# ---------------------------------------------------------------------------
# Criterion: one text shared by three query_ids -> once in submit, thrice out
# ---------------------------------------------------------------------------


class TestFanout:
    def test_three_query_ids_on_one_text_submit_once_and_fan_out_three_times(
        self,
    ) -> None:
        shared = "ATP synthase subunit alpha"
        universe = _universe(
            (_hash(1), shared), (_hash(2), shared), (_hash(3), shared)
        )
        ranked = _ranked(
            (_hash(1), shared, None), (_hash(2), shared, None), (_hash(3), shared, None)
        )
        parts = unrun_set(universe, [], ranked)
        assert len(parts["submit"]) == 1
        assert len(parts["fanout"]) == 3
        assert {row["query_id"] for row in parts["fanout"]} == {
            _hash(1),
            _hash(2),
            _hash(3),
        }
        # All three point at the ONE paid answer.
        (item,) = parts["submit"]
        assert {row["item_id"] for row in parts["fanout"]} == {item["item_id"]}

    def test_every_non_declined_function_appears_in_fanout_exactly_once(self) -> None:
        """Including the ones whose text is unique.

        ``fanout`` is the COMPLETE query_id -> item_id map, not just the
        duplicates: stage 5 needs a result row per function, and a map that
        covered only the shared texts would leave the unique ones unjoinable.
        """
        universe = _universe(
            (_hash(1), "shared"), (_hash(2), "shared"), (_hash(3), "unique")
        )
        ranked = _ranked(
            (_hash(1), "shared", None),
            (_hash(2), "shared", None),
            (_hash(3), "unique", None),
        )
        parts = unrun_set(universe, [], ranked)
        ids = [row["query_id"] for row in parts["fanout"]]
        assert sorted(ids) == sorted([_hash(1), _hash(2), _hash(3)])
        assert len(ids) == len(set(ids))
        assert len(parts["submit"]) == 2

    def test_the_representative_is_the_lowest_hash_and_is_deterministic(self) -> None:
        shared = "ATP synthase subunit alpha"
        forward = unrun_set(
            _universe((_hash(7), shared), (_hash(2), shared)),
            [],
            _ranked((_hash(7), shared, None), (_hash(2), shared, None)),
        )
        reversed_ = unrun_set(
            _universe((_hash(2), shared), (_hash(7), shared)),
            [],
            _ranked((_hash(2), shared, None), (_hash(7), shared, None)),
        )
        assert forward["submit"][0]["query_id"] == _hash(2)
        assert forward == reversed_


# ---------------------------------------------------------------------------
# Criterion: done at THIS parameter set excludes; at another, it does not
# ---------------------------------------------------------------------------


class TestDoneExcludesOnlyAtThisParameterSet:
    def test_a_current_state_result_excludes_the_function(self) -> None:
        universe = _universe((_hash(1), "glucokinase"), (_hash(2), "glucose oxidase"))
        ranked = _ranked(
            (_hash(1), "glucokinase", None), (_hash(2), "glucose oxidase", None)
        )
        parts = unrun_set(universe, _done(_hash(1)), ranked)
        assert [row["query_id"] for row in parts["submit"]] == [_hash(2)]

    def test_the_same_function_at_a_different_hash_is_still_un_run(self) -> None:
        """The caller filters by parameter_set_hash; this function does not.

        ``done`` is the current-state read ALREADY filtered to this run's
        parameter set, so a row at a different hash simply is not in the set
        handed over -- and the function stays un-run. Both halves are asserted
        here because this is the partition key the whole design rests on.
        """
        universe = _universe((_hash(1), "glucokinase"))
        ranked = _ranked((_hash(1), "glucokinase", None))
        at_this_hash = _done(_hash(1), parameter_set_hash=PSH_A)
        at_another_hash = [
            row
            for row in _done(_hash(1), parameter_set_hash=PSH_B)
            if row["parameter_set_hash"] == PSH_A  # the run's own filter
        ]
        assert unrun_set(universe, at_this_hash, ranked)["submit"] == []
        assert len(unrun_set(universe, at_another_hash, ranked)["submit"]) == 1

    def test_done_accepts_rows_or_bare_hashes(self) -> None:
        universe = _universe((_hash(1), "glucokinase"))
        ranked = _ranked((_hash(1), "glucokinase", None))
        assert unrun_set(universe, _done(_hash(1)), ranked)["submit"] == []
        assert unrun_set(universe, [_hash(1)], ranked)["submit"] == []

    def test_done_hashes_refuses_a_row_without_an_entity_hash(self) -> None:
        with pytest.raises(ValueError, match="no entity_hash"):
            done_hashes([{"source": "council/go_mf@1"}])

    def test_a_done_function_is_not_ranked_at_all(self) -> None:
        """``unrun_items`` is what stage 1 feeds to ``function_to_go``.

        A function already answered at this parameter set must not be ranked,
        because ranking 47,341 functions to throw 28,000 away is the cost this
        whole derivation exists to avoid.
        """
        universe = _universe((_hash(1), "glucokinase"), (_hash(2), "glucose oxidase"))
        items = unrun_items(universe, _done(_hash(1)))
        assert [row["query_id"] for row in items] == [_hash(2)]


# ---------------------------------------------------------------------------
# Criterion: the recorded ADP1+PMI partition
# ---------------------------------------------------------------------------

#: The measured ADP1+PMI partition from the completed 2026-10-20 run.
RECORDED_SUBMIT = 19_276
RECORDED_ABSTAIN = 760
RECORDED_DISTINCT = 20_036


@pytest.fixture(scope="module")
def partition() -> Dict[str, Any]:
    """The recorded partition's SHAPE, at its exact recorded counts."""
    universe = _universe(
        *(
            (_hash(i), f"a specific enzymatic activity {i}")
            for i in range(RECORDED_SUBMIT)
        ),
        *(
            (_hash(RECORDED_SUBMIT + i), f"hypothetical protein {i}")
            for i in range(RECORDED_ABSTAIN)
        ),
    )
    ranked = _ranked(
        *(
            (_hash(i), f"a specific enzymatic activity {i}", None)
            for i in range(RECORDED_SUBMIT)
        ),
        *(
            (
                _hash(RECORDED_SUBMIT + i),
                f"hypothetical protein {i}",
                "placeholder function",
            )
            for i in range(RECORDED_ABSTAIN)
        ),
    )
    return unrun_set(universe, [], ranked)



class TestTheRecordedPartition:
    """20,036 distinct definitions = 19,276 submit + 760 abstain, disjoint.

    The counts are the measured ones from the completed 2026-10-20 run over
    the ADP1+PMI priority set (22,255 priority rows, 20,036 distinct
    ``entity_hash`` = 20,036 distinct definitions, so no fan-out in that
    corpus). The ADP1+PMI corpus itself is not in this repository, so this
    fixture reproduces its shape at those exact counts rather than its text.
    What it pins is the arithmetic and the DISJOINTNESS -- the 760 were
    dropped once and read as an unexplained coverage gap.
    """

    SUBMIT = RECORDED_SUBMIT
    ABSTAIN = RECORDED_ABSTAIN
    DISTINCT = RECORDED_DISTINCT

    def test_the_counts_are_the_recorded_ones(self, partition: Dict[str, Any]) -> None:
        assert len(partition["submit"]) == self.SUBMIT
        assert len(partition["abstain"]) == self.ABSTAIN
        assert len(partition["submit"]) + len(partition["abstain"]) == self.DISTINCT

    def test_submit_and_abstain_are_disjoint(self, partition: Dict[str, Any]) -> None:
        submitted = {row["query_id"] for row in partition["submit"]}
        abstained = {row["query_id"] for row in partition["abstain"]}
        assert not submitted & abstained
        assert len(submitted | abstained) == self.DISTINCT

    def test_no_definition_is_dropped(self, partition: Dict[str, Any]) -> None:
        """The union covers every distinct definition in the universe.

        This is the assertion that would have caught the 760: a coverage
        check, not a submit-count check.
        """
        covered = {row["text"] for row in partition["submit"]} | {
            row["text"] for row in partition["abstain"]
        }
        assert len(covered) == self.DISTINCT

    def test_this_corpus_needs_no_fan_out(self, partition: Dict[str, Any]) -> None:
        """20,036 distinct hashes = 20,036 distinct definitions, as measured."""
        assert len(partition["fanout"]) == self.SUBMIT
        assert len({row["item_id"] for row in partition["fanout"]}) == self.SUBMIT

    def test_item_ids_are_unique_across_the_whole_submit_set(
        self, partition: Dict[str, Any]
    ) -> None:
        """A repeated item_id would quote one item twice and publish one of two
        different answers; ``load_plan`` refuses a plan that does it."""
        ids = [row["item_id"] for row in partition["submit"]]
        assert len(set(ids)) == len(ids)


# ---------------------------------------------------------------------------
# Refusals -- each one a failure mode that must not degrade into a silent drop
# ---------------------------------------------------------------------------


class TestRefusals:
    def test_an_un_run_function_with_no_ranked_row_is_a_refusal(self) -> None:
        universe = _universe((_hash(1), "glucokinase"), (_hash(2), "glucose oxidase"))
        ranked = _ranked((_hash(1), "glucokinase", None))
        with pytest.raises(ValueError, match="no ranked row"):
            unrun_set(universe, [], ranked)

    def test_a_universe_row_with_no_entity_hash_is_a_refusal(self) -> None:
        with pytest.raises(ValueError, match="no entity_hash"):
            unrun_items([{"definition": "glucokinase"}], [])

    def test_a_universe_row_with_no_definition_is_a_refusal(self) -> None:
        with pytest.raises(ValueError, match="no definition"):
            unrun_items([{"entity_hash": _hash(1)}], [])

    def test_one_hash_with_two_definitions_is_a_refusal(self) -> None:
        universe = _universe((_hash(1), "glucokinase"), (_hash(1), "glucose oxidase"))
        with pytest.raises(ValueError, match="two different definitions"):
            unrun_items(universe, [])

    def test_a_duplicated_identical_universe_row_is_collapsed_not_refused(
        self,
    ) -> None:
        universe = _universe((_hash(1), "glucokinase"), (_hash(1), "glucokinase"))
        assert len(unrun_items(universe, [])) == 1

    def test_ranked_as_a_mapping_without_results_is_a_refusal(self) -> None:
        with pytest.raises(ValueError, match="no `results` key"):
            unrun_set(_universe((_hash(1), "x")), [], {"mapping_hash": "f" * 64})


# ---------------------------------------------------------------------------
# Input spellings
# ---------------------------------------------------------------------------


class TestInputSpellings:
    def test_a_ranked_file_read_back_can_serve_as_the_universe(self) -> None:
        """``read_ranked`` rows spell them ``query_id``/``text``.

        Accepted so a plan can be re-derived from a ranked file alone, without
        a clearinghouse round trip.
        """
        universe = [{"query_id": _hash(1), "text": "glucokinase"}]
        parts = unrun_set(universe, [], _ranked((_hash(1), "glucokinase", None)))
        assert parts["submit"][0]["query_id"] == _hash(1)

    def test_ranked_may_be_a_bare_list_of_rows(self) -> None:
        ranked = _ranked((_hash(1), "glucokinase", None))
        parts = unrun_set(_universe((_hash(1), "glucokinase")), [], ranked["results"])
        assert len(parts["submit"]) == 1
