"""Tests for kbutillib.cli.buildplan — buildplan.json validator and CLI command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from kbutillib.cli import main
from kbutillib.cli.buildplan import BuildPlanError, load_buildplan, validate_buildplan


# ── helpers ────────────────────────────────────────────────────────────────────


def _write_buildplan(tmp_path: Path, data: dict) -> Path:
    """Write *data* as buildplan.json in *tmp_path* and return the path."""
    p = tmp_path / "buildplan.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _minimal_helper(name: str = "my_helper") -> dict:
    """Return a minimal valid helper dict."""
    return {
        "name": name,
        "signature": f"{name}(x: int) -> int",
        "contract": "Returns x plus one.",
        "test": {
            "data_source": "synthetic",
            "data_spec": "x=5",
            "assertions": ["result == 6"],
        },
    }


def _minimal_notebook(slug: str, depends_on: list[str] | None = None) -> dict:
    """Return a minimal valid notebook dict."""
    return {
        "slug": slug,
        "purpose": f"Purpose of {slug}.",
        "depends_on": depends_on or [],
        "helpers": [_minimal_helper()],
    }


def _valid_buildplan() -> dict:
    """Return a fully valid buildplan dict."""
    return {
        "subproject": "my_analysis",
        "notebooks": [
            _minimal_notebook("01_load"),
            _minimal_notebook("02_process", depends_on=["01_load"]),
        ],
    }


def _invoke_validate(path: str | Path) -> object:
    """Run ``kbu buildplan validate <path>`` via the CLI runner.

    Click 8.x mixes stdout and stderr into ``result.output`` by default, so
    all assertions read from ``result.output``.
    """
    runner = CliRunner()
    return runner.invoke(
        main,
        ["buildplan", "validate", str(path)],
        catch_exceptions=False,
    )


# ── validate_buildplan (unit tests on the programmatic API) ────────────────────


class TestValidBuildplan:
    def test_valid_returns_empty_errors(self) -> None:
        errors = validate_buildplan(_valid_buildplan())
        assert errors == []

    def test_valid_load_buildplan_returns_dict(self, tmp_path: Path) -> None:
        p = _write_buildplan(tmp_path, _valid_buildplan())
        result = load_buildplan(p)
        assert result["subproject"] == "my_analysis"
        assert len(result["notebooks"]) == 2

    def test_no_depends_on_empty_list(self) -> None:
        data = _valid_buildplan()
        data["notebooks"][1]["depends_on"] = []
        errors = validate_buildplan(data)
        assert errors == []

    def test_data_source_sampled_real_accepted(self) -> None:
        data = _valid_buildplan()
        data["notebooks"][0]["helpers"][0]["test"]["data_source"] = "sampled-real"
        errors = validate_buildplan(data)
        assert errors == []


class TestMissingTopLevelFields:
    def test_missing_subproject(self) -> None:
        data = _valid_buildplan()
        del data["subproject"]
        errors = validate_buildplan(data)
        assert any("subproject" in e for e in errors)

    def test_missing_notebooks(self) -> None:
        data = _valid_buildplan()
        del data["notebooks"]
        errors = validate_buildplan(data)
        assert any("notebooks" in e for e in errors)

    def test_non_dict_input(self) -> None:
        errors = validate_buildplan(["not", "a", "dict"])
        assert any("JSON object" in e for e in errors)

    def test_empty_subproject_string(self) -> None:
        data = _valid_buildplan()
        data["subproject"] = ""
        errors = validate_buildplan(data)
        assert any("subproject" in e for e in errors)


class TestNotebookFieldValidation:
    def test_missing_slug(self) -> None:
        data = _valid_buildplan()
        del data["notebooks"][0]["slug"]
        errors = validate_buildplan(data)
        assert any("slug" in e for e in errors)

    def test_missing_purpose(self) -> None:
        data = _valid_buildplan()
        del data["notebooks"][0]["purpose"]
        errors = validate_buildplan(data)
        assert any("purpose" in e for e in errors)

    def test_missing_depends_on(self) -> None:
        data = _valid_buildplan()
        del data["notebooks"][0]["depends_on"]
        errors = validate_buildplan(data)
        assert any("depends_on" in e for e in errors)

    def test_missing_helpers(self) -> None:
        data = _valid_buildplan()
        del data["notebooks"][0]["helpers"]
        errors = validate_buildplan(data)
        assert any("helpers" in e for e in errors)

    def test_notebook_not_dict(self) -> None:
        data = _valid_buildplan()
        data["notebooks"][0] = "not a dict"
        errors = validate_buildplan(data)
        assert any("JSON object" in e for e in errors)


class TestDuplicateSlugs:
    def test_duplicate_slugs_rejected(self) -> None:
        data = {
            "subproject": "sp",
            "notebooks": [
                _minimal_notebook("01_load"),
                _minimal_notebook("01_load"),  # duplicate
            ],
        }
        errors = validate_buildplan(data)
        assert any("duplicate slug" in e for e in errors)
        assert any("01_load" in e for e in errors)

    def test_three_notebooks_one_dup_reports_dup(self) -> None:
        data = {
            "subproject": "sp",
            "notebooks": [
                _minimal_notebook("01_load"),
                _minimal_notebook("02_process"),
                _minimal_notebook("01_load"),  # duplicate of first
            ],
        }
        errors = validate_buildplan(data)
        dup_errors = [e for e in errors if "duplicate slug" in e]
        assert len(dup_errors) == 1

    def test_unique_slugs_ok(self) -> None:
        data = {
            "subproject": "sp",
            "notebooks": [
                _minimal_notebook("01_load"),
                _minimal_notebook("02_process"),
                _minimal_notebook("03_report"),
            ],
        }
        errors = validate_buildplan(data)
        assert not any("duplicate" in e for e in errors)


class TestDependsOnValidation:
    def test_valid_backward_dependency(self) -> None:
        data = {
            "subproject": "sp",
            "notebooks": [
                _minimal_notebook("01_load"),
                _minimal_notebook("02_process", depends_on=["01_load"]),
            ],
        }
        errors = validate_buildplan(data)
        assert errors == []

    def test_forward_reference_rejected(self) -> None:
        """depends_on referencing a notebook that appears LATER is rejected."""
        data = {
            "subproject": "sp",
            "notebooks": [
                _minimal_notebook("01_load", depends_on=["02_process"]),
                _minimal_notebook("02_process"),
            ],
        }
        errors = validate_buildplan(data)
        assert any("forward reference" in e for e in errors)
        assert any("02_process" in e for e in errors)

    def test_self_reference_rejected(self) -> None:
        """depends_on referencing its own slug is rejected."""
        data = {
            "subproject": "sp",
            "notebooks": [
                _minimal_notebook("01_load", depends_on=["01_load"]),
            ],
        }
        errors = validate_buildplan(data)
        assert any("self-reference" in e for e in errors)
        assert any("01_load" in e for e in errors)

    def test_unknown_dependency_rejected(self) -> None:
        """depends_on referencing a non-existent slug is rejected."""
        data = {
            "subproject": "sp",
            "notebooks": [
                _minimal_notebook("01_load", depends_on=["nonexistent"]),
            ],
        }
        errors = validate_buildplan(data)
        assert any("unknown" in e for e in errors)
        assert any("nonexistent" in e for e in errors)

    def test_depends_on_not_a_list_rejected(self) -> None:
        data = _valid_buildplan()
        data["notebooks"][1]["depends_on"] = "01_load"  # string, not list
        errors = validate_buildplan(data)
        assert any("depends_on" in e and "list" in e for e in errors)

    def test_chain_dependency_valid(self) -> None:
        """A → B → C chain (each depends on strictly earlier) is valid."""
        data = {
            "subproject": "sp",
            "notebooks": [
                _minimal_notebook("01_a"),
                _minimal_notebook("02_b", depends_on=["01_a"]),
                _minimal_notebook("03_c", depends_on=["02_b"]),
            ],
        }
        errors = validate_buildplan(data)
        assert errors == []


class TestHelperValidation:
    def test_missing_helper_name(self) -> None:
        data = _valid_buildplan()
        del data["notebooks"][0]["helpers"][0]["name"]
        errors = validate_buildplan(data)
        assert any("name" in e for e in errors)

    def test_missing_helper_signature(self) -> None:
        data = _valid_buildplan()
        del data["notebooks"][0]["helpers"][0]["signature"]
        errors = validate_buildplan(data)
        assert any("signature" in e for e in errors)

    def test_missing_helper_contract(self) -> None:
        data = _valid_buildplan()
        del data["notebooks"][0]["helpers"][0]["contract"]
        errors = validate_buildplan(data)
        assert any("contract" in e for e in errors)

    def test_missing_helper_test(self) -> None:
        data = _valid_buildplan()
        del data["notebooks"][0]["helpers"][0]["test"]
        errors = validate_buildplan(data)
        assert any("'test'" in e for e in errors)

    def test_helper_not_dict(self) -> None:
        data = _valid_buildplan()
        data["notebooks"][0]["helpers"][0] = "not a dict"
        errors = validate_buildplan(data)
        assert any("JSON object" in e for e in errors)

    def test_duplicate_helper_names_within_notebook_rejected(self) -> None:
        data = _valid_buildplan()
        data["notebooks"][0]["helpers"] = [
            _minimal_helper("load_data"),
            _minimal_helper("load_data"),  # duplicate within same notebook
        ]
        errors = validate_buildplan(data)
        assert any("duplicate helper name" in e for e in errors)
        assert any("load_data" in e for e in errors)

    def test_duplicate_helper_names_across_notebooks_ok(self) -> None:
        """Same helper name in different notebooks is allowed."""
        data = {
            "subproject": "sp",
            "notebooks": [
                {
                    "slug": "01_load",
                    "purpose": "Load.",
                    "depends_on": [],
                    "helpers": [_minimal_helper("load_data")],
                },
                {
                    "slug": "02_process",
                    "purpose": "Process.",
                    "depends_on": ["01_load"],
                    "helpers": [_minimal_helper("load_data")],  # same name, different notebook
                },
            ],
        }
        errors = validate_buildplan(data)
        assert not any("duplicate helper name" in e for e in errors)


class TestTestValidation:
    def test_empty_assertions_rejected(self) -> None:
        data = _valid_buildplan()
        data["notebooks"][0]["helpers"][0]["test"]["assertions"] = []
        errors = validate_buildplan(data)
        assert any("assertions" in e and "empty" in e for e in errors)

    def test_missing_assertions_rejected(self) -> None:
        data = _valid_buildplan()
        del data["notebooks"][0]["helpers"][0]["test"]["assertions"]
        errors = validate_buildplan(data)
        assert any("assertions" in e for e in errors)

    def test_invalid_data_source_rejected(self) -> None:
        data = _valid_buildplan()
        data["notebooks"][0]["helpers"][0]["test"]["data_source"] = "fake-source"
        errors = validate_buildplan(data)
        assert any("data_source" in e for e in errors)
        assert any("fake-source" in e for e in errors)

    def test_missing_data_source_rejected(self) -> None:
        data = _valid_buildplan()
        del data["notebooks"][0]["helpers"][0]["test"]["data_source"]
        errors = validate_buildplan(data)
        assert any("data_source" in e for e in errors)

    def test_missing_data_spec_rejected(self) -> None:
        data = _valid_buildplan()
        del data["notebooks"][0]["helpers"][0]["test"]["data_spec"]
        errors = validate_buildplan(data)
        assert any("data_spec" in e for e in errors)

    def test_assertions_not_list_rejected(self) -> None:
        data = _valid_buildplan()
        data["notebooks"][0]["helpers"][0]["test"]["assertions"] = "not a list"
        errors = validate_buildplan(data)
        assert any("assertions" in e and "list" in e for e in errors)


class TestAllErrorsSurface:
    """Prove that ALL errors are collected, not just the first."""

    def test_multiple_errors_all_reported(self) -> None:
        """A buildplan with 4 distinct errors produces 4+ error strings."""
        data = {
            "subproject": "sp",
            "notebooks": [
                {
                    "slug": "01_load",
                    "purpose": "Load.",
                    "depends_on": [],
                    "helpers": [
                        {
                            "name": "helper_a",
                            "signature": "helper_a() -> None",
                            "contract": "Does something.",
                            "test": {
                                "data_source": "bad-source",   # error 1: invalid data_source
                                "data_spec": "x=1",
                                "assertions": [],               # error 2: empty assertions
                            },
                        },
                        {
                            "name": "helper_a",               # error 3: duplicate helper name
                            "signature": "helper_a() -> None",
                            "contract": "Does something else.",
                            "test": {
                                "data_source": "synthetic",
                                "data_spec": "x=2",
                                "assertions": ["result is not None"],
                            },
                        },
                    ],
                },
                {
                    "slug": "01_load",                        # error 4: duplicate slug
                    "purpose": "Load again.",
                    "depends_on": ["03_future"],              # error 5: unknown/forward dep
                    "helpers": [],
                },
            ],
        }
        errors = validate_buildplan(data)
        assert len(errors) >= 4, (
            f"Expected at least 4 errors, got {len(errors)}: {errors}"
        )

    def test_forward_dep_and_empty_assertions_both_reported(self) -> None:
        """Forward depends_on and empty assertions are both surfaced in one pass."""
        data = {
            "subproject": "sp",
            "notebooks": [
                {
                    "slug": "01_load",
                    "purpose": "Load.",
                    "depends_on": ["02_process"],    # forward reference error
                    "helpers": [
                        {
                            "name": "h1",
                            "signature": "h1() -> None",
                            "contract": "Does stuff.",
                            "test": {
                                "data_source": "synthetic",
                                "data_spec": "x=1",
                                "assertions": [],    # empty assertions error
                            },
                        }
                    ],
                },
                _minimal_notebook("02_process"),
            ],
        }
        errors = validate_buildplan(data)
        has_forward = any("forward reference" in e for e in errors)
        has_empty_assertions = any("empty" in e for e in errors)
        assert has_forward, f"Expected forward reference error; got: {errors}"
        assert has_empty_assertions, f"Expected empty assertions error; got: {errors}"

    def test_load_buildplan_raises_with_all_errors(self, tmp_path: Path) -> None:
        """load_buildplan raises BuildPlanError carrying all errors."""
        data = {
            "subproject": "sp",
            "notebooks": [
                {
                    "slug": "nb1",
                    "purpose": "P.",
                    "depends_on": [],
                    "helpers": [
                        {
                            "name": "h",
                            "signature": "h() -> None",
                            "contract": "C.",
                            "test": {
                                "data_source": "bad",   # error 1
                                "data_spec": "x",
                                "assertions": [],       # error 2
                            },
                        }
                    ],
                }
            ],
        }
        p = _write_buildplan(tmp_path, data)
        with pytest.raises(BuildPlanError) as exc_info:
            load_buildplan(p)
        assert len(exc_info.value.errors) >= 2


# ── CLI integration tests ──────────────────────────────────────────────────────


class TestCLIValidate:
    def test_valid_buildplan_exits_zero(self, tmp_path: Path) -> None:
        p = _write_buildplan(tmp_path, _valid_buildplan())
        result = _invoke_validate(p)
        assert result.exit_code == 0, result.output
        assert "OK" in result.output

    def test_valid_buildplan_prints_path(self, tmp_path: Path) -> None:
        p = _write_buildplan(tmp_path, _valid_buildplan())
        result = _invoke_validate(p)
        assert str(p) in result.output

    def test_invalid_exits_nonzero(self, tmp_path: Path) -> None:
        data = _valid_buildplan()
        data["notebooks"][0]["helpers"][0]["test"]["assertions"] = []
        p = _write_buildplan(tmp_path, data)
        result = _invoke_validate(p)
        assert result.exit_code != 0

    def test_all_errors_printed_on_invalid(self, tmp_path: Path) -> None:
        """CLI prints every error, not just the first."""
        data = {
            "subproject": "sp",
            "notebooks": [
                {
                    "slug": "nb1",
                    "purpose": "P.",
                    "depends_on": [],
                    "helpers": [
                        {
                            "name": "h",
                            "signature": "h() -> None",
                            "contract": "C.",
                            "test": {
                                "data_source": "bad-source",  # error 1
                                "data_spec": "x",
                                "assertions": [],             # error 2
                            },
                        }
                    ],
                }
            ],
        }
        p = _write_buildplan(tmp_path, data)
        result = _invoke_validate(p)
        assert result.exit_code != 0
        # Both errors should appear in output (Click mixes stdout+stderr by default)
        assert "data_source" in result.output
        assert "assertions" in result.output

    def test_forward_dep_reported_in_cli(self, tmp_path: Path) -> None:
        data = {
            "subproject": "sp",
            "notebooks": [
                _minimal_notebook("01_load", depends_on=["02_process"]),
                _minimal_notebook("02_process"),
            ],
        }
        p = _write_buildplan(tmp_path, data)
        result = _invoke_validate(p)
        assert result.exit_code != 0
        assert "forward reference" in result.output

    def test_invalid_json_exits_nonzero(self, tmp_path: Path) -> None:
        bad_json = tmp_path / "buildplan.json"
        bad_json.write_text("{not valid json", encoding="utf-8")
        result = _invoke_validate(bad_json)
        assert result.exit_code != 0

    def test_buildplan_help_registered(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["buildplan", "--help"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "validate" in result.output

    def test_validate_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main, ["buildplan", "validate", "--help"], catch_exceptions=False
        )
        assert result.exit_code == 0
