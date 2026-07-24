"""S8 CLI tests for ``kbu verab``.

Uses :class:`click.testing.CliRunner` so no subprocess is needed and the
facade can be monkeypatched freely.

Tests deliberately avoid importing RDKit or minedatabase, following the
global test convention from ``tests/test_network_expansion.py``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from kbutillib.cli import main
from kbutillib.cli.verab import verab_cmd


# ---------------------------------------------------------------------------
# Helpers / canned results
# ---------------------------------------------------------------------------

def _canned_discovery() -> Any:
    """Return a fake VerabDiscoveryResult-like object (duck-typed)."""
    from kbutillib.cheminformatics.verab.models import VerabDiscoveryResult, VerabRuleMatch

    match = VerabRuleMatch(
        operator="ruleCANNED",
        reaction_id="rxn_canned",
        backend="pickaxe",
        reactant_ids=["cpd_vanillate"],
        product_ids=["cpd_protocatechuate", "cpd_formaldehyde"],
        method="rdkit_transform",
        confidence=1.0,
        ec_hint="1.14.13.82",
    )
    return VerabDiscoveryResult(
        rule_set="metacyc_generalized",
        generations=1,
        seeds=[{"id": "cpd_vanillate", "smiles": "COc1cc(C(=O)O)ccc1O"}],
        matches=[match],
        operators=["ruleCANNED"],
        expansion_summary={"n_compounds": 5, "n_reactions": 2},
        warnings=[],
    )


def _canned_screening_report() -> Any:
    """Return a fake ScreeningReport-like object (duck-typed)."""
    from kbutillib.cheminformatics.verab.models import ScreeningRecord, ScreeningReport

    rec = ScreeningRecord(
        source_msid="cpd00137",
        source_smiles="COc1cc(C(=O)O)ccc1O",
        operator="ruleCANNED",
        product_smiles="Oc1ccc(C(=O)O)cc1O",
        product_in_db="cpd00006",
        reaction_in_db="rxn00001",
        has_downstream_pathway=True,
    )
    return ScreeningReport(n_source_compounds=1, records=[rec])


def _canned_king_artifacts() -> dict:
    return {
        "outdir": "/tmp/king_verab",
        "files": ["seeds.tsv", "seeds.csv", "manifest.json"],
        "n_operators": 1,
        "n_seeds": 5,
    }


# ---------------------------------------------------------------------------
# Group-level help
# ---------------------------------------------------------------------------


class TestVerabGroupHelp:
    """``kbu verab --help`` must exit 0 and mention verAB in the help text."""

    def test_group_help_exit_zero(self):
        runner = CliRunner()
        result = runner.invoke(main, ["verab", "--help"])
        assert result.exit_code == 0, (
            f"'kbu verab --help' exited {result.exit_code}; output:\n{result.output}"
        )

    def test_group_help_mentions_verab(self):
        runner = CliRunner()
        result = runner.invoke(main, ["verab", "--help"])
        # The help text must include the canonical casing "verAB"
        assert "verAB" in result.output, (
            f"Expected 'verAB' in help output; got:\n{result.output}"
        )

    def test_group_lists_subcommands(self):
        runner = CliRunner()
        result = runner.invoke(main, ["verab", "--help"])
        for sub in ("discover", "enumerate", "screen", "emit-king"):
            assert sub in result.output, (
                f"Subcommand '{sub}' not listed in 'kbu verab --help'; output:\n{result.output}"
            )


# ---------------------------------------------------------------------------
# Subcommand --help exits 0
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subcmd", ["discover", "enumerate", "screen", "emit-king"])
def test_subcommand_help_exit_zero(subcmd: str):
    runner = CliRunner()
    result = runner.invoke(main, ["verab", subcmd, "--help"])
    assert result.exit_code == 0, (
        f"'kbu verab {subcmd} --help' exited {result.exit_code};\n{result.output}"
    )


def test_discover_help_exit_zero():
    """Explicit test per plan: discover --help → exit 0."""
    runner = CliRunner()
    result = runner.invoke(main, ["verab", "discover", "--help"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# --json invocation with monkeypatched facade
# ---------------------------------------------------------------------------


class TestDiscoverJson:
    """``kbu verab discover --json`` emits valid JSON on stdout, exit 0
    when operators are found (exit 1 when empty)."""

    def _invoke_discover_json(self, discovery_return_value):
        runner = CliRunner()
        with patch("kbutillib.cli.verab._get_toolkit") as mock_get_toolkit:
            fake_toolkit = MagicMock()
            fake_toolkit.verab.discover_rules.return_value = discovery_return_value
            mock_get_toolkit.return_value = fake_toolkit
            result = runner.invoke(main, ["verab", "discover", "--json"])
        return result

    def test_json_output_is_valid(self):
        result = self._invoke_discover_json(_canned_discovery())
        # Must not raise
        parsed = json.loads(result.output)
        assert isinstance(parsed, dict)

    def test_json_output_has_operators(self):
        result = self._invoke_discover_json(_canned_discovery())
        parsed = json.loads(result.output)
        assert "operators" in parsed
        assert "ruleCANNED" in parsed["operators"]

    def test_exit_0_when_operators_found(self):
        result = self._invoke_discover_json(_canned_discovery())
        assert result.exit_code == 0, (
            f"Expected exit 0 with operators found; got {result.exit_code}"
        )

    def test_exit_1_when_no_operators(self):
        from kbutillib.cheminformatics.verab.models import VerabDiscoveryResult
        empty = VerabDiscoveryResult(
            rule_set="metacyc_generalized",
            generations=1,
            operators=[],
        )
        result = self._invoke_discover_json(empty)
        assert result.exit_code == 1, (
            f"Expected exit 1 with no operators; got {result.exit_code}"
        )

    def test_error_path_emits_json_error_and_exit_2(self):
        runner = CliRunner()
        with patch("kbutillib.cli.verab._get_toolkit") as mock_get_toolkit:
            fake_toolkit = MagicMock()
            fake_toolkit.verab.discover_rules.side_effect = RuntimeError("pickaxe down")
            mock_get_toolkit.return_value = fake_toolkit
            result = runner.invoke(main, ["verab", "discover", "--json"])
        assert result.exit_code == 2
        parsed = json.loads(result.output)
        assert "error" in parsed
        assert "pickaxe down" in parsed["error"]


class TestEnumerateJson:
    """``kbu verab enumerate --json`` emits valid JSON with n_compounds."""

    def test_json_output_valid(self):
        runner = CliRunner()
        canned_compounds = [
            {"id": "cpd_guaiacol", "name": "guaiacol", "smiles": "COc1ccccc1O", "formula": "C7H8O2"},
        ]
        with patch("kbutillib.cli.verab._get_toolkit") as mock_get_toolkit:
            fake_toolkit = MagicMock()
            fake_toolkit.verab.enumerate_methoxy_aromatics.return_value = canned_compounds
            mock_get_toolkit.return_value = fake_toolkit
            result = runner.invoke(main, ["verab", "enumerate", "--json"])

        assert result.exit_code == 0, f"exit {result.exit_code}:\n{result.output}"
        parsed = json.loads(result.output)
        assert parsed["n_compounds"] == 1
        assert parsed["compounds"][0]["id"] == "cpd_guaiacol"


class TestScreenJson:
    """``kbu verab screen --json`` emits valid JSON ScreeningReport dict."""

    def test_json_output_valid(self):
        runner = CliRunner()
        with patch("kbutillib.cli.verab._get_toolkit") as mock_get_toolkit:
            fake_toolkit = MagicMock()
            fake_toolkit.verab.screen.return_value = _canned_screening_report()
            mock_get_toolkit.return_value = fake_toolkit
            result = runner.invoke(main, ["verab", "screen", "--json"])

        assert result.exit_code == 0, f"exit {result.exit_code}:\n{result.output}"
        parsed = json.loads(result.output)
        assert "records" in parsed
        assert parsed["n_source_compounds"] == 1


class TestEmitKingJson:
    """``kbu verab emit-king --json`` emits valid JSON artifacts dict."""

    def test_json_output_valid(self):
        runner = CliRunner()
        with patch("kbutillib.cli.verab._get_toolkit") as mock_get_toolkit:
            fake_toolkit = MagicMock()
            fake_toolkit.verab.emit_king_workflow.return_value = _canned_king_artifacts()
            mock_get_toolkit.return_value = fake_toolkit
            result = runner.invoke(main, ["verab", "emit-king", "--json"])

        assert result.exit_code == 0, f"exit {result.exit_code}:\n{result.output}"
        parsed = json.loads(result.output)
        assert parsed["n_seeds"] == 5
        assert "manifest.json" in parsed["files"]


# ---------------------------------------------------------------------------
# Other CLI commands still registered (no regression)
# ---------------------------------------------------------------------------


class TestOtherCommandsStillRegistered:
    """Adding `verab` must not remove any other registered subcommands."""

    @pytest.mark.parametrize(
        "cmd",
        ["king", "model", "bootstrap", "doctor", "init", "jobs", "session",
         "set", "update", "migrate", "buildplan", "harness", "beril",
         "researchos", "notebook", "subproject"],
    )
    def test_command_still_registered(self, cmd: str):
        runner = CliRunner()
        result = runner.invoke(main, [cmd, "--help"])
        assert result.exit_code == 0, (
            f"'{cmd} --help' exited {result.exit_code} — may have been removed;\n"
            f"{result.output}"
        )

    def test_verab_registered_in_main(self):
        """verab must appear in `kbu --help`."""
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert "verab" in result.output, (
            f"'verab' not in `kbu --help`; output:\n{result.output}"
        )
