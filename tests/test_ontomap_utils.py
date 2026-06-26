"""Unit tests for OntomapUtils MapResult->candidates translation.

These tests exercise the translation layer without GPU or an ontomap install.
The ontomap Pipeline is never actually loaded; instead, synthetic MapResult
fixtures are fed directly into the private ``_translate_map_result`` method and
into a mocked ``map_functions`` call.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from kbutillib.ontomap_utils import OntomapUtils


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_map_result(
    predictions: List[tuple],
    reaction_meta: Dict[str, Any],
    source_ec: Optional[str] = "1.1.1.1",
) -> Any:
    """Build a minimal synthetic ontomap.MapResult-alike namespace."""
    return SimpleNamespace(
        predictions=predictions,
        reaction_meta=reaction_meta,
        source_ec=source_ec,
    )


def _build_fixture(n: int = 20) -> Any:
    """Build a fixture with *n* candidate predictions in descending score order."""
    predictions = [
        (f"rxn_{i:03d}", round(1.0 - i * 0.03, 4))
        for i in range(n)
    ]
    reaction_meta = {
        rxn_id: {
            "name": f"Reaction {rxn_id}",
            "ec_list": [f"1.{i}.1.1", f"2.{i}.2.2"],
            "equation": f"A{i} <=> B{i}",
            "pathway": [f"pathway_{i}"],
            "ec_match_level": "exact" if i % 2 == 0 else "partial",
        }
        for i, (rxn_id, _) in enumerate(predictions)
    }
    return _make_map_result(predictions, reaction_meta, source_ec="1.1.1.1")


# ---------------------------------------------------------------------------
# OntomapUtils instance — no ontomap package needed
# ---------------------------------------------------------------------------


@pytest.fixture()
def utils():
    """OntomapUtils instantiated without touching ontomap or tokens."""
    return OntomapUtils(
        config_file=False,
        token_file=None,
        kbase_token_file=None,
    )


# ---------------------------------------------------------------------------
# _translate_map_result unit tests (pure translation, no I/O)
# ---------------------------------------------------------------------------


class TestTranslateMapResult:
    """Tests for OntomapUtils._translate_map_result."""

    def test_candidate_count_equals_top_k(self):
        """translate_map_result must return exactly top_k candidates."""
        top_k = 20
        fixture = _build_fixture(n=30)  # more predictions than top_k
        result = OntomapUtils._translate_map_result(
            query_id="g001",
            description="ATP synthase",
            map_result=fixture,
            top_k=top_k,
        )
        assert len(result["candidates"]) == top_k

    def test_candidate_count_capped_at_available(self):
        """When fewer predictions are available than top_k, return all available."""
        top_k = 50
        n_available = 5
        fixture = _build_fixture(n=n_available)
        result = OntomapUtils._translate_map_result(
            query_id="g001",
            description="short list",
            map_result=fixture,
            top_k=top_k,
        )
        assert len(result["candidates"]) == n_available

    def test_fused_score_descending_order(self):
        """Candidates must be in descending fused_score order."""
        top_k = 20
        fixture = _build_fixture(n=20)
        result = OntomapUtils._translate_map_result(
            query_id="g002",
            description="glucose kinase",
            map_result=fixture,
            top_k=top_k,
        )
        scores = [c["fused_score"] for c in result["candidates"]]
        assert scores == sorted(scores, reverse=True), (
            f"Candidates not in descending order: {scores}"
        )

    def test_rank_field_sequential(self):
        """Rank must start at 1 and increment by 1."""
        fixture = _build_fixture(n=5)
        result = OntomapUtils._translate_map_result(
            query_id="g003",
            description="test",
            map_result=fixture,
            top_k=5,
        )
        ranks = [c["rank"] for c in result["candidates"]]
        assert ranks == list(range(1, 6))

    def test_reaction_id_field_mapping(self):
        """reaction_id must come from the predictions tuple."""
        fixture = _build_fixture(n=3)
        result = OntomapUtils._translate_map_result(
            query_id="g004",
            description="test",
            map_result=fixture,
            top_k=3,
        )
        expected_ids = [p[0] for p in fixture.predictions[:3]]
        actual_ids = [c["reaction_id"] for c in result["candidates"]]
        assert actual_ids == expected_ids

    def test_ec_numbers_from_ec_list(self):
        """ec_numbers must be populated from the reaction_meta ec_list field."""
        predictions = [("rxn_A", 0.95)]
        meta = {"rxn_A": {"ec_list": ["3.1.2.3", "4.2.1.1"], "name": "Test Rxn",
                           "equation": "X <=> Y", "pathway": [], "ec_match_level": "exact"}}
        fixture = _make_map_result(predictions, meta)
        result = OntomapUtils._translate_map_result("q1", "desc", fixture, top_k=1)
        assert result["candidates"][0]["ec_numbers"] == ["3.1.2.3", "4.2.1.1"]

    def test_ec_numbers_string_normalised_to_list(self):
        """A bare string in ec_list must be wrapped in a list."""
        predictions = [("rxn_B", 0.80)]
        meta = {"rxn_B": {"ec_list": "3.1.2.3", "name": "Bare String EC",
                           "equation": "M <=> N", "pathway": [], "ec_match_level": None}}
        fixture = _make_map_result(predictions, meta)
        result = OntomapUtils._translate_map_result("q2", "desc", fixture, top_k=1)
        assert result["candidates"][0]["ec_numbers"] == ["3.1.2.3"]

    def test_name_field_mapping(self):
        """name must come from reaction_meta."""
        predictions = [("rxn_C", 0.75)]
        meta = {"rxn_C": {"name": "Phosphoglycerate kinase", "ec_list": [],
                           "equation": "", "pathway": [], "ec_match_level": None}}
        fixture = _make_map_result(predictions, meta)
        result = OntomapUtils._translate_map_result("q3", "desc", fixture, top_k=1)
        assert result["candidates"][0]["name"] == "Phosphoglycerate kinase"

    def test_equation_field_mapping(self):
        """equation must come from reaction_meta."""
        predictions = [("rxn_D", 0.70)]
        meta = {"rxn_D": {"ec_list": [], "name": "", "equation": "ATP + glucose => ADP + G6P",
                           "pathway": [], "ec_match_level": None}}
        fixture = _make_map_result(predictions, meta)
        result = OntomapUtils._translate_map_result("q4", "desc", fixture, top_k=1)
        assert result["candidates"][0]["equation"] == "ATP + glucose => ADP + G6P"

    def test_source_ec_propagated(self):
        """source_ec on the map_result must be copied to the output dict."""
        fixture = _build_fixture(n=3)
        result = OntomapUtils._translate_map_result("q5", "desc", fixture, top_k=3)
        assert result["source_ec"] == "1.1.1.1"

    def test_query_id_and_description_in_output(self):
        """query_id and description must be present and correct."""
        fixture = _build_fixture(n=2)
        result = OntomapUtils._translate_map_result(
            "gene_42", "NADH dehydrogenase", fixture, top_k=2
        )
        assert result["query_id"] == "gene_42"
        assert result["description"] == "NADH dehydrogenase"

    def test_confidence_band_high(self):
        """Scores >= 0.9 must yield 'high' confidence band."""
        predictions = [("rxn_H", 0.95)]
        meta = {"rxn_H": {"ec_list": [], "name": "", "equation": "", "pathway": [],
                           "ec_match_level": None}}
        fixture = _make_map_result(predictions, meta)
        result = OntomapUtils._translate_map_result("q6", "d", fixture, top_k=1)
        assert result["candidates"][0]["confidence_band"] == "high"

    def test_confidence_band_medium(self):
        """Scores in [0.7, 0.9) must yield 'medium' confidence band."""
        predictions = [("rxn_M", 0.80)]
        meta = {"rxn_M": {"ec_list": [], "name": "", "equation": "", "pathway": [],
                           "ec_match_level": None}}
        fixture = _make_map_result(predictions, meta)
        result = OntomapUtils._translate_map_result("q7", "d", fixture, top_k=1)
        assert result["candidates"][0]["confidence_band"] == "medium"

    def test_confidence_band_low(self):
        """Scores < 0.7 must yield 'low' confidence band."""
        predictions = [("rxn_L", 0.50)]
        meta = {"rxn_L": {"ec_list": [], "name": "", "equation": "", "pathway": [],
                           "ec_match_level": None}}
        fixture = _make_map_result(predictions, meta)
        result = OntomapUtils._translate_map_result("q8", "d", fixture, top_k=1)
        assert result["candidates"][0]["confidence_band"] == "low"

    def test_top1_margin_computed(self):
        """top1_margin on rank-1 candidate must equal score[0] - score[1]."""
        predictions = [("rxn_1", 0.90), ("rxn_2", 0.70), ("rxn_3", 0.50)]
        meta = {r: {"ec_list": [], "name": "", "equation": "", "pathway": [],
                    "ec_match_level": None} for r, _ in predictions}
        fixture = _make_map_result(predictions, meta)
        result = OntomapUtils._translate_map_result("q9", "d", fixture, top_k=3)
        rank1 = result["candidates"][0]
        assert rank1["top1_margin"] == pytest.approx(0.90 - 0.70)

    def test_top1_margin_none_when_single_candidate(self):
        """top1_margin must be None when only one candidate is present."""
        predictions = [("rxn_only", 0.85)]
        meta = {"rxn_only": {"ec_list": [], "name": "", "equation": "", "pathway": [],
                              "ec_match_level": None}}
        fixture = _make_map_result(predictions, meta)
        result = OntomapUtils._translate_map_result("q10", "d", fixture, top_k=1)
        assert result["candidates"][0]["top1_margin"] is None

    def test_empty_predictions_yields_empty_candidates(self):
        """When predictions is empty, candidates must be an empty list."""
        fixture = _make_map_result(predictions=[], reaction_meta={}, source_ec=None)
        result = OntomapUtils._translate_map_result("q11", "d", fixture, top_k=20)
        assert result["candidates"] == []
        assert result["source_ec"] is None

    def test_missing_meta_entry_defaults_gracefully(self):
        """When a reaction_id has no entry in reaction_meta, defaults are used."""
        predictions = [("rxn_missing", 0.65)]
        fixture = _make_map_result(predictions, reaction_meta={})  # no meta for rxn_missing
        result = OntomapUtils._translate_map_result("q12", "d", fixture, top_k=1)
        cand = result["candidates"][0]
        assert cand["reaction_id"] == "rxn_missing"
        assert cand["ec_numbers"] == []
        assert cand["name"] == ""
        assert cand["equation"] == ""


# ---------------------------------------------------------------------------
# map_functions integration test (Pipeline mocked)
# ---------------------------------------------------------------------------


class TestMapFunctions:
    """Tests for OntomapUtils.map_functions with a mocked Pipeline."""

    def _make_utils_with_mock_pipeline(self, pipeline_mock):
        """Return an OntomapUtils instance whose _get_pipeline returns the mock."""
        util = OntomapUtils(
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        util._pipeline = pipeline_mock  # pre-populate cache
        return util

    def test_map_functions_returns_one_result_per_description(self):
        """map_functions must return a list of the same length as descriptions."""
        descriptions = ["func A", "func B", "func C"]
        fixture = _build_fixture(n=20)

        pipeline_mock = MagicMock()
        pipeline_mock.side_effect = lambda descs: [fixture] * len(descs)

        util = self._make_utils_with_mock_pipeline(pipeline_mock)
        results = util.map_functions(descriptions, top_k=20)

        assert len(results) == 3

    def test_map_functions_auto_ids_when_none(self):
        """When ids is None, query_id must be '0', '1', '2' etc."""
        descriptions = ["func A", "func B"]
        fixture = _build_fixture(n=5)

        pipeline_mock = MagicMock()
        pipeline_mock.side_effect = lambda descs: [fixture] * len(descs)

        util = self._make_utils_with_mock_pipeline(pipeline_mock)
        results = util.map_functions(descriptions, top_k=5)

        assert results[0]["query_id"] == "0"
        assert results[1]["query_id"] == "1"

    def test_map_functions_uses_supplied_ids(self):
        """When ids are provided, they must appear as query_id in results."""
        descriptions = ["func X"]
        ids = ["gene_007"]
        fixture = _build_fixture(n=5)

        pipeline_mock = MagicMock()
        pipeline_mock.side_effect = lambda descs: [fixture] * len(descs)

        util = self._make_utils_with_mock_pipeline(pipeline_mock)
        results = util.map_functions(descriptions, ids=ids, top_k=5)

        assert results[0]["query_id"] == "gene_007"

    def test_map_functions_raises_on_mismatched_ids(self, utils):
        """Mismatched ids length must raise ValueError."""
        with pytest.raises(ValueError, match="ids length"):
            utils.map_functions(["a", "b"], ids=["only_one"])

    def test_map_functions_empty_descriptions_returns_empty(self, utils):
        """Empty descriptions list must return an empty list without touching Pipeline."""
        results = utils.map_functions([])
        assert results == []

    def test_map_functions_top_k_respected(self):
        """top_k must cap the number of candidates in every result."""
        descriptions = ["func Z"]
        fixture = _build_fixture(n=30)

        pipeline_mock = MagicMock()
        pipeline_mock.side_effect = lambda descs: [fixture] * len(descs)

        util = self._make_utils_with_mock_pipeline(pipeline_mock)
        results = util.map_functions(descriptions, top_k=7)

        assert len(results[0]["candidates"]) == 7

    def test_map_functions_raises_import_error_when_ontomap_missing(self, utils):
        """ImportError must be raised when ontomap is not installed."""
        with patch.dict("sys.modules", {"ontomap": None}):
            with pytest.raises(ImportError, match="ontomap is required"):
                utils.map_functions(["some description"])


# ---------------------------------------------------------------------------
# Module-level import test
# ---------------------------------------------------------------------------


def test_module_imports_without_ontomap():
    """ontomap_utils must be importable even when ontomap is absent."""
    import importlib
    import sys

    # Simulate ontomap being absent
    saved = sys.modules.pop("ontomap", None)
    sys.modules["ontomap"] = None  # type: ignore[assignment]
    try:
        import kbutillib.ontomap_utils as m
        importlib.reload(m)  # re-execute with blocked import
        assert hasattr(m, "OntomapUtils")
        assert hasattr(m, "OntomapUtilsImpl")
    finally:
        if saved is None:
            sys.modules.pop("ontomap", None)
        else:
            sys.modules["ontomap"] = saved
