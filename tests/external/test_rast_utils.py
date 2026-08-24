"""Offline unit tests for rast_utils.py.

Test strategy
-------------
The RPC layer (``modelseedpy.core.rpcclient.RPCClient.call``) is mocked
throughout via ``unittest.mock.patch.object``. No test in this module makes
a real network call. The gated real-network integration test lives in
``test_rast_utils_live.py`` and is skipped by default.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("modelseedpy", reason="modelseedpy required for RastUtils tests")

from modelseedpy.core.rpcclient import ServerError  # noqa: E402

from kbutillib.domains.external.rast_utils import (  # noqa: E402
    RastServiceError,
    RastUtils,
    RastUtilsImpl,
    _chunked,
    _split_role_terms,
)
from kbutillib.domains.genome.annotation.annotator_utils import (  # noqa: E402
    ToolUnavailableError,
)


def _make_utils(**kwargs) -> RastUtils:
    """Create RastUtils with no file discovery."""
    return RastUtils(
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        **kwargs,
    )


def _rpc_result(features):
    """Build a canned ``GenomeAnnotation.run_pipeline`` result list."""
    return [{"features": features, "analysis_events": [{"stage": "kmer_v2"}]}]


# ---------------------------------------------------------------------------
# is_available()
# ---------------------------------------------------------------------------


class TestIsAvailable:
    """RastUtils.is_available() contract — no network, no import side effects."""

    def test_returns_true_when_modelseedpy_importable(self):
        assert _make_utils().is_available() is True

    def test_returns_false_when_modelseedpy_not_importable(self):
        ru = _make_utils()
        with patch(
            "kbutillib.domains.external.rast_utils.importlib.util.find_spec",
            return_value=None,
        ):
            assert ru.is_available() is False

    def test_does_not_touch_the_network(self):
        # is_available() must never call requests / RPCClient.call.
        ru = _make_utils()
        with patch("requests.post") as mock_post:
            ru.is_available()
            mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# annotate() — validation before the RPC layer is touched
# ---------------------------------------------------------------------------


class TestAnnotateValidation:
    def test_raises_on_empty_proteins(self):
        ru = _make_utils()
        with pytest.raises(ValueError, match="must not be empty"):
            ru.annotate(proteins={})

    def test_raises_on_blank_sequence(self):
        ru = _make_utils()
        with pytest.raises(ValueError, match="blank sequence"):
            ru.annotate(proteins={"g1": "MKTAY", "g2": "  "})

    def test_raises_on_nucleotide_input(self):
        ru = _make_utils()
        # "U" (uracil) is valid DNA/RNA but not one of the 20 canonical amino
        # acids (or B/Z/X), so this trips the protein alphabet guard. Unlike
        # "ACGT", which is *also* valid as four amino-acid single-letter
        # codes (Ala/Cys/Gly/Thr) and would pass _guard_protein silently.
        nuc = "U" * 100
        with pytest.raises(ValueError, match="protein"):
            ru.annotate(proteins={"g1": nuc})

    def test_raises_on_non_positive_chunk_size(self):
        ru = _make_utils()
        with pytest.raises(ValueError, match="chunk_size"):
            ru.annotate(proteins={"g1": "MKTAY"}, chunk_size=0)

    def test_raises_tool_unavailable_when_modelseedpy_absent(self):
        ru = _make_utils()
        with patch.object(ru, "is_available", return_value=False):
            with pytest.raises(ToolUnavailableError):
                ru.annotate(proteins={"g1": "MKTAY"})


# ---------------------------------------------------------------------------
# annotate() — request shaping + result parsing (RPC mocked)
# ---------------------------------------------------------------------------


class TestAnnotateRequestShaping:
    def test_sends_all_proteins_in_one_call_by_default(self):
        ru = _make_utils()
        proteins = {"g1": "MKTAY", "g2": "MNFST"}
        with patch(
            "modelseedpy.core.rpcclient.RPCClient.call",
            return_value=_rpc_result([]),
        ) as mock_call:
            ru.annotate(proteins)
        assert mock_call.call_count == 1
        method, params = mock_call.call_args[0]
        assert method == "GenomeAnnotation.run_pipeline"
        features_arg, stages_arg = params
        sent_ids = {f["id"] for f in features_arg["features"]}
        assert sent_ids == {"g1", "g2"}
        for f in features_arg["features"]:
            assert f["protein_translation"] == proteins[f["id"]]
        stage_names = [s["name"] for s in stages_arg["stages"]]
        assert stage_names == ["annotate_proteins_kmer_v2", "annotate_proteins_similarity"]

    def test_chunk_size_splits_into_multiple_calls(self):
        ru = _make_utils()
        proteins = {f"g{i}": "MKTAY" for i in range(5)}
        with patch(
            "modelseedpy.core.rpcclient.RPCClient.call",
            return_value=_rpc_result([]),
        ) as mock_call:
            result = ru.annotate(proteins, chunk_size=2)
        assert mock_call.call_count == 3  # 2 + 2 + 1
        assert result.parameters["num_calls"] == 3
        assert result.parameters["chunk_size"] == 2
        # Every id must have been sent exactly once across all calls.
        sent_ids = []
        for call in mock_call.call_args_list:
            _, params = call[0]
            sent_ids.extend(f["id"] for f in params[0]["features"])
        assert sorted(sent_ids) == sorted(proteins.keys())

    def test_timeout_config_applied_to_rpc_client(self):
        ru = _make_utils()
        ru._timeout = 42
        rast_client = ru._build_rast_client()
        assert rast_client.rpc_client.timeout == 42

    def test_timeout_configurable_via_get_config_value(self):
        ru = _make_utils(config={"rast": {"timeout": 99}})
        assert ru._timeout == 99


class TestAnnotateResultParsing:
    def test_parses_function_into_records(self):
        ru = _make_utils()
        features = [
            {"id": "g1", "function": "Some enzyme (EC 1.1.1.1)"},
            {"id": "g2", "function": ""},
            {"id": "g3"},
        ]
        with patch(
            "modelseedpy.core.rpcclient.RPCClient.call",
            return_value=_rpc_result(features),
        ):
            result = ru.annotate({"g1": "MKTAY", "g2": "MNFST", "g3": "MPQRS"})
        assert result.tool == "rast"
        assert result.tool_version is None
        assert result.db_version is None
        gene_ids = {rec.gene_id for rec in result.records}
        # g2 (empty function) and g3 (no function key) are zero-hit and absent.
        assert gene_ids == {"g1"}
        [rec] = result.records
        assert [t.value for t in rec.terms] == ["Some enzyme (EC 1.1.1.1)"]
        assert all(t.namespace == "RAST" for t in rec.terms)

    def test_multi_role_function_splits_into_separate_rast_terms(self):
        ru = _make_utils()
        function = (
            "Phosphopantetheine adenylyltransferase (EC 2.7.7.3) / "
            "Dephospho-CoA kinase (EC 2.7.1.24)"
        )
        features = [{"id": "g1", "function": function}]
        with patch(
            "modelseedpy.core.rpcclient.RPCClient.call",
            return_value=_rpc_result(features),
        ):
            result = ru.annotate({"g1": "MKTAY"})
        [rec] = result.records
        values = [t.value for t in rec.terms]
        assert values == [
            "Phosphopantetheine adenylyltransferase (EC 2.7.7.3)",
            "Dephospho-CoA kinase (EC 2.7.1.24)",
        ]
        assert all(t.namespace == "RAST" for t in rec.terms)

    def test_split_terms_false_keeps_single_combined_term(self):
        ru = _make_utils()
        function = "Role A / Role B"
        features = [{"id": "g1", "function": function}]
        with patch(
            "modelseedpy.core.rpcclient.RPCClient.call",
            return_value=_rpc_result(features),
        ):
            result = ru.annotate({"g1": "MKTAY"}, split_terms=False)
        [rec] = result.records
        assert [t.value for t in rec.terms] == [function]

    def test_command_field_describes_the_call(self):
        ru = _make_utils()
        with patch(
            "modelseedpy.core.rpcclient.RPCClient.call",
            return_value=_rpc_result([]),
        ):
            result = ru.annotate({"g1": "MKTAY"})
        assert "GenomeAnnotation.run_pipeline" in result.command
        assert "tutorial.theseed.org" in result.command


# ---------------------------------------------------------------------------
# annotate() — failure paths
# ---------------------------------------------------------------------------


class TestAnnotateFailures:
    def test_server_error_raises_rast_service_error(self):
        ru = _make_utils()
        with patch(
            "modelseedpy.core.rpcclient.RPCClient.call",
            side_effect=ServerError("JSONRPCError", -32000, "boom"),
        ):
            with pytest.raises(RastServiceError, match="RAST server returned an error"):
                ru.annotate({"g1": "MKTAY"})

    def test_network_error_raises_rast_service_error(self):
        import requests

        ru = _make_utils()
        with patch(
            "modelseedpy.core.rpcclient.RPCClient.call",
            side_effect=requests.exceptions.ConnectionError("no route"),
        ):
            with pytest.raises(RastServiceError, match="Network error"):
                ru.annotate({"g1": "MKTAY"})

    def test_empty_result_raises_rast_service_error(self):
        ru = _make_utils()
        with patch("modelseedpy.core.rpcclient.RPCClient.call", return_value=None):
            with pytest.raises(RastServiceError, match="empty or malformed"):
                ru.annotate({"g1": "MKTAY"})

    def test_result_missing_features_key_raises_rast_service_error(self):
        ru = _make_utils()
        with patch(
            "modelseedpy.core.rpcclient.RPCClient.call",
            return_value=[{"analysis_events": []}],
        ):
            with pytest.raises(RastServiceError, match="missing the expected"):
                ru.annotate({"g1": "MKTAY"})

    def test_non_list_result_raises_rast_service_error(self):
        ru = _make_utils()
        with patch(
            "modelseedpy.core.rpcclient.RPCClient.call",
            return_value={"features": []},
        ):
            with pytest.raises(RastServiceError, match="empty or malformed"):
                ru.annotate({"g1": "MKTAY"})


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestSplitRoleTerms:
    def test_splits_on_slash(self):
        terms = _split_role_terms("Role A / Role B")
        assert [t.value for t in terms] == ["Role A", "Role B"]

    def test_splits_on_semicolon(self):
        terms = _split_role_terms("Role A; Role B")
        assert [t.value for t in terms] == ["Role A", "Role B"]

    def test_splits_on_at(self):
        terms = _split_role_terms("Role A @ Role B")
        assert [t.value for t in terms] == ["Role A", "Role B"]

    def test_does_not_split_on_fat_arrow(self):
        # "=> " is in aux_rast_result's (dead-code) delimiter set but NOT in
        # RastClient.annotate_genome's live one; this module follows the
        # live path.
        terms = _split_role_terms("Role A => Role B")
        assert [t.value for t in terms] == ["Role A => Role B"]

    def test_single_role_no_delimiter(self):
        terms = _split_role_terms("Just one role")
        assert [t.value for t in terms] == ["Just one role"]

    def test_all_terms_namespaced_rast(self):
        terms = _split_role_terms("A / B / C")
        assert all(t.namespace == "RAST" for t in terms)
        assert all(t.id is None for t in terms)


class TestChunked:
    def test_exact_multiple(self):
        items = [(f"g{i}", "M") for i in range(4)]
        chunks = _chunked(items, 2)
        assert chunks == [items[0:2], items[2:4]]

    def test_remainder_chunk(self):
        items = [(f"g{i}", "M") for i in range(5)]
        chunks = _chunked(items, 2)
        assert [len(c) for c in chunks] == [2, 2, 1]

    def test_chunk_size_larger_than_input(self):
        items = [("g0", "M")]
        assert _chunked(items, 10) == [items]


# ---------------------------------------------------------------------------
# RastUtilsImpl (composition wrapper, mirrors PatricWSUtilsImpl)
# ---------------------------------------------------------------------------


class TestRastUtilsImpl:
    def test_delegates_is_available(self):
        impl = RastUtilsImpl(env=object())
        assert impl.is_available() is True

    def test_env_property(self):
        sentinel = object()
        impl = RastUtilsImpl(env=sentinel)
        assert impl.env is sentinel

    def test_delegates_annotate(self):
        impl = RastUtilsImpl(env=object())
        with patch(
            "modelseedpy.core.rpcclient.RPCClient.call",
            return_value=_rpc_result([{"id": "g1", "function": "Role A"}]),
        ):
            result = impl.annotate({"g1": "MKTAY"})
        assert result.tool == "rast"
