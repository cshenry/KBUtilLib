"""Tests for WP6 CLI commands: ``kbu cap`` and ``kbu new-capability``.

All tests are **offline** — they use Click's :class:`~click.testing.CliRunner`
and do NOT require any network access or optional backend (modelseedpy, rdkit,
etc.).  The biochem capabilities register as unavailable in this environment;
they still appear in ``kbu cap list`` output.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def runner() -> CliRunner:
    """Click test runner with isolated environment."""
    return CliRunner()


@pytest.fixture(autouse=True)
def _clear_registry():
    """Clear the global registry before each test to avoid cross-test pollution."""
    from kbutillib.core.registry import get_registry

    reg = get_registry()
    reg.clear()
    yield
    reg.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invoke_main(*args: str, runner: CliRunner | None = None) -> "Result":  # type: ignore[name-defined]  # noqa: F821
    """Invoke the ``kbu`` root CLI group with the given args."""
    from kbutillib.cli import main  # type: ignore[attr-defined]

    r = runner or CliRunner()
    return r.invoke(main, list(args), catch_exceptions=False)


# ---------------------------------------------------------------------------
# ``kbu cap list`` tests
# ---------------------------------------------------------------------------


class TestCapList:
    def test_exits_zero(self, runner: CliRunner) -> None:
        result = _invoke_main("cap", "list", runner=runner)
        assert result.exit_code == 0, result.output

    def test_shows_three_biochem_caps(self, runner: CliRunner) -> None:
        result = _invoke_main("cap", "list", runner=runner)
        assert result.exit_code == 0
        output = result.output
        assert "biochem.search_compounds" in output
        assert "biochem.get_compound_by_id" in output
        assert "biochem.get_reaction_by_id" in output

    def test_shows_domain_column(self, runner: CliRunner) -> None:
        result = _invoke_main("cap", "list", runner=runner)
        assert "biochem" in result.output

    def test_filter_by_domain(self, runner: CliRunner) -> None:
        result = _invoke_main("cap", "list", "--domain", "biochem", runner=runner)
        assert result.exit_code == 0
        assert "biochem.search_compounds" in result.output

    def test_filter_by_nonexistent_domain_empty(self, runner: CliRunner) -> None:
        result = _invoke_main("cap", "list", "--domain", "nonexistent99", runner=runner)
        assert result.exit_code == 0
        assert "No capabilities found" in result.output

    def test_json_output_is_valid(self, runner: CliRunner) -> None:
        result = _invoke_main("cap", "list", "--json", runner=runner)
        assert result.exit_code == 0
        rows = json.loads(result.output)
        assert isinstance(rows, list)
        assert len(rows) >= 3
        names = {r["name"] for r in rows}
        assert "biochem.search_compounds" in names
        assert "biochem.get_compound_by_id" in names
        assert "biochem.get_reaction_by_id" in names

    def test_json_output_has_required_keys(self, runner: CliRunner) -> None:
        result = _invoke_main("cap", "list", "--json", runner=runner)
        rows = json.loads(result.output)
        required = {"name", "domain", "available", "summary", "tags", "visibility", "transports"}
        for row in rows:
            assert required <= set(row.keys()), f"Missing keys in {row}"

    def test_json_filter_by_tag(self, runner: CliRunner) -> None:
        result = _invoke_main("cap", "list", "--tag", "biochem", "--json", runner=runner)
        assert result.exit_code == 0
        rows = json.loads(result.output)
        for row in rows:
            assert "biochem" in row["tags"]


# ---------------------------------------------------------------------------
# ``kbu cap info`` tests
# ---------------------------------------------------------------------------


class TestCapInfo:
    def test_exits_zero_for_known_cap(self, runner: CliRunner) -> None:
        result = _invoke_main("cap", "info", "biochem.search_compounds", runner=runner)
        assert result.exit_code == 0, result.output

    def test_shows_name(self, runner: CliRunner) -> None:
        result = _invoke_main("cap", "info", "biochem.search_compounds", runner=runner)
        assert "biochem.search_compounds" in result.output

    def test_shows_domain(self, runner: CliRunner) -> None:
        result = _invoke_main("cap", "info", "biochem.search_compounds", runner=runner)
        assert "biochem" in result.output

    def test_shows_availability(self, runner: CliRunner) -> None:
        result = _invoke_main("cap", "info", "biochem.search_compounds", runner=runner)
        # Either "yes" or "no" must appear in the Available line
        assert "Available:" in result.output

    def test_shows_input_schema(self, runner: CliRunner) -> None:
        result = _invoke_main("cap", "info", "biochem.search_compounds", runner=runner)
        # SearchCompoundsInput has fields: query_identifiers, query_structures, query_formula
        assert "Input:" in result.output
        assert "SearchCompoundsInput" in result.output

    def test_shows_output_schema(self, runner: CliRunner) -> None:
        result = _invoke_main("cap", "info", "biochem.search_compounds", runner=runner)
        assert "Output:" in result.output

    def test_shows_tags(self, runner: CliRunner) -> None:
        result = _invoke_main("cap", "info", "biochem.search_compounds", runner=runner)
        assert "Tags:" in result.output

    def test_shows_transports(self, runner: CliRunner) -> None:
        result = _invoke_main("cap", "info", "biochem.search_compounds", runner=runner)
        assert "Transports:" in result.output

    def test_unknown_name_exits_nonzero(self, runner: CliRunner) -> None:
        result = runner.invoke(
            _get_main(),
            ["cap", "info", "does.not.exist"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0

    def test_get_compound_by_id_info(self, runner: CliRunner) -> None:
        result = _invoke_main("cap", "info", "biochem.get_compound_by_id", runner=runner)
        assert result.exit_code == 0
        assert "GetCompoundByIdInput" in result.output

    def test_get_reaction_by_id_info(self, runner: CliRunner) -> None:
        result = _invoke_main("cap", "info", "biochem.get_reaction_by_id", runner=runner)
        assert result.exit_code == 0
        assert "GetReactionByIdInput" in result.output


def _get_main():
    from kbutillib.cli import main  # type: ignore[attr-defined]

    return main


# ---------------------------------------------------------------------------
# ``kbu cap run`` tests
# ---------------------------------------------------------------------------


class TestCapRun:
    def test_unavailable_cap_exits_nonzero(self, runner: CliRunner) -> None:
        """Biochem is unavailable (no modelseedpy) — must exit non-zero."""
        result = runner.invoke(
            _get_main(),
            ["cap", "run", "biochem.search_compounds"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0

    def test_unknown_cap_exits_nonzero(self, runner: CliRunner) -> None:
        result = runner.invoke(
            _get_main(),
            ["cap", "run", "does.not.exist"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0

    def test_invalid_json_input_exits_nonzero(self, runner: CliRunner) -> None:
        result = runner.invoke(
            _get_main(),
            ["cap", "run", "biochem.search_compounds", "--json-input", "{bad json"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# ``kbu new-capability`` tests
# ---------------------------------------------------------------------------


class TestNewCapability:
    def test_exits_zero(self, runner: CliRunner, tmp_path: Path) -> None:
        # Provide a pyproject.toml so repo-root detection succeeds
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        result = runner.invoke(
            _get_main(),
            ["new-capability", "demo.hello", "--target-dir", str(tmp_path)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

    def test_creates_method_stub(self, runner: CliRunner, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        runner.invoke(
            _get_main(),
            ["new-capability", "demo.hello", "--target-dir", str(tmp_path)],
            catch_exceptions=False,
        )
        method_file = tmp_path / "src" / "kbutillib" / "domains" / "demo" / "hello_impl.py"
        assert method_file.exists(), f"Expected {method_file} to be created"
        content = method_file.read_text()
        assert "@capability" in content
        assert "demo.hello" in content
        assert "class DemoHelloImpl" in content

    def test_creates_schema_stub(self, runner: CliRunner, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        runner.invoke(
            _get_main(),
            ["new-capability", "demo.hello", "--target-dir", str(tmp_path)],
            catch_exceptions=False,
        )
        schema_file = tmp_path / "src" / "kbutillib" / "domains" / "demo" / "schemas_hello.py"
        assert schema_file.exists()
        content = schema_file.read_text()
        assert "HelloInput" in content
        assert "HelloOutput" in content

    def test_creates_test_stub(self, runner: CliRunner, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        runner.invoke(
            _get_main(),
            ["new-capability", "demo.hello", "--target-dir", str(tmp_path)],
            catch_exceptions=False,
        )
        test_file = tmp_path / "tests" / "test_demo_hello.py"
        assert test_file.exists()
        content = test_file.read_text()
        assert "TestHello" in content

    def test_does_not_overwrite_existing_files(self, runner: CliRunner, tmp_path: Path) -> None:
        """Running twice keeps the original file contents."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        target_dir = ["--target-dir", str(tmp_path)]

        # First run
        runner.invoke(
            _get_main(), ["new-capability", "demo.hello"] + target_dir, catch_exceptions=False
        )

        # Overwrite with sentinel
        method_file = tmp_path / "src" / "kbutillib" / "domains" / "demo" / "hello_impl.py"
        sentinel = "# SENTINEL DO NOT OVERWRITE\n"
        method_file.write_text(sentinel)

        # Second run
        result = runner.invoke(
            _get_main(), ["new-capability", "demo.hello"] + target_dir, catch_exceptions=False
        )
        assert result.exit_code == 0
        assert method_file.read_text() == sentinel
        assert "SKIP" in result.output

    def test_invalid_dotted_name_exits_nonzero(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(
            _get_main(),
            ["new-capability", "nodot", "--target-dir", str(tmp_path)],
            catch_exceptions=False,
        )
        assert result.exit_code != 0

    def test_three_part_name_exits_nonzero(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(
            _get_main(),
            ["new-capability", "a.b.c", "--target-dir", str(tmp_path)],
            catch_exceptions=False,
        )
        assert result.exit_code != 0

    def test_output_says_done(self, runner: CliRunner, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        result = runner.invoke(
            _get_main(),
            ["new-capability", "demo.hello", "--target-dir", str(tmp_path)],
            catch_exceptions=False,
        )
        assert "Done." in result.output


# ---------------------------------------------------------------------------
# ``kbu --help`` smoke test — existing commands must still appear
# ---------------------------------------------------------------------------


class TestHelpIntegrity:
    def test_help_exits_zero(self, runner: CliRunner) -> None:
        result = runner.invoke(_get_main(), ["--help"], catch_exceptions=False)
        assert result.exit_code == 0

    def test_help_shows_cap(self, runner: CliRunner) -> None:
        result = runner.invoke(_get_main(), ["--help"], catch_exceptions=False)
        assert "cap" in result.output

    def test_help_shows_new_capability(self, runner: CliRunner) -> None:
        result = runner.invoke(_get_main(), ["--help"], catch_exceptions=False)
        assert "new-capability" in result.output

    def test_help_shows_existing_doctor(self, runner: CliRunner) -> None:
        result = runner.invoke(_get_main(), ["--help"], catch_exceptions=False)
        assert "doctor" in result.output

    def test_help_shows_existing_init(self, runner: CliRunner) -> None:
        result = runner.invoke(_get_main(), ["--help"], catch_exceptions=False)
        assert "init" in result.output

    def test_help_shows_existing_king(self, runner: CliRunner) -> None:
        result = runner.invoke(_get_main(), ["--help"], catch_exceptions=False)
        assert "king" in result.output

    def test_cap_help_shows_subcommands(self, runner: CliRunner) -> None:
        result = runner.invoke(_get_main(), ["cap", "--help"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "list" in result.output
        assert "info" in result.output
        assert "run" in result.output
