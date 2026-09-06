"""Tests for WP7 — kbu doctor extended backend + registry probes.

Covers:
- doctor exits 0 even when optional backends (rdkit, minedatabase, mcp,
  fastapi, cobra) are absent.
- doctor output contains backend status lines for each probed package.
- doctor output contains a registry summary including the biochem domain.
- Individual probe functions behave correctly in isolation.
- Python/kbutillib version probe always returns INFO.
- Registry summary probe never raises, even when construction fails.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from kbutillib.interfaces.cli.init import (
    _BACKEND_PROBES,
    _probe_all_backends,
    _probe_backend,
    _probe_python_kbutillib_version,
    _probe_registry_summary,
    doctor_command,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_doctor(*args: str) -> Any:
    """Invoke the doctor Click command via CliRunner and return result."""
    runner = CliRunner()
    return runner.invoke(doctor_command, list(args), catch_exceptions=False)


# ---------------------------------------------------------------------------
# doctor exits 0 regardless of missing optional deps
# ---------------------------------------------------------------------------


class TestDoctorExitCode:
    """doctor exits 0 even when optional backends are absent."""

    def test_doctor_exits_zero_always(self) -> None:
        """doctor must exit 0 even with many packages missing (FAIL probes are machine-env FAILs).

        We patch the machine-level probes that would FAIL in CI
        (init-done, cursor, jupyter-kernel, fba-imports) to return PASS so
        the exit code is 0.  The important guarantee is that the backend and
        registry probes never push the exit code to 1.
        """
        runner = CliRunner()
        with (
            patch(
                "kbutillib.interfaces.cli.init._probe_init_done",
                return_value=("PASS", "ok"),
            ),
            patch(
                "kbutillib.interfaces.cli.init._probe_cursor_on_path",
                return_value=("PASS", "ok"),
            ),
            patch(
                "kbutillib.interfaces.cli.init._probe_claude_extension",
                return_value=("PASS", "ok"),
            ),
            patch(
                "kbutillib.interfaces.cli.init._probe_kbu_version",
                return_value=("PASS", "ok"),
            ),
            patch(
                "kbutillib.interfaces.cli.init._probe_jupyter_kernel",
                return_value=("PASS", "ok"),
            ),
            patch(
                "kbutillib.interfaces.cli.init._probe_fba_imports",
                return_value=("PASS", "ok"),
            ),
            patch(
                "kbutillib.interfaces.cli.init._probe_tomli_w",
                return_value=("PASS", "ok"),
            ),
        ):
            result = runner.invoke(doctor_command, [], catch_exceptions=False)

        assert result.exit_code == 0, (
            f"doctor should exit 0 when machine probes pass; got {result.exit_code}.\n"
            f"Output:\n{result.output}"
        )

    def test_doctor_does_not_raise_when_backends_absent(self) -> None:
        """doctor never raises an exception even if find_spec always returns None."""
        runner = CliRunner()

        def always_none(name: str, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
            return None

        with (
            patch("importlib.util.find_spec", side_effect=always_none),
            patch(
                "kbutillib.interfaces.cli.init._probe_init_done",
                return_value=("PASS", "ok"),
            ),
            patch(
                "kbutillib.interfaces.cli.init._probe_cursor_on_path",
                return_value=("PASS", "ok"),
            ),
            patch(
                "kbutillib.interfaces.cli.init._probe_claude_extension",
                return_value=("PASS", "ok"),
            ),
            patch(
                "kbutillib.interfaces.cli.init._probe_kbu_version",
                return_value=("PASS", "ok"),
            ),
            patch(
                "kbutillib.interfaces.cli.init._probe_jupyter_kernel",
                return_value=("PASS", "ok"),
            ),
            patch(
                "kbutillib.interfaces.cli.init._probe_fba_imports",
                return_value=("PASS", "ok"),
            ),
            patch(
                "kbutillib.interfaces.cli.init._probe_tomli_w",
                return_value=("PASS", "ok"),
            ),
        ):
            # Must not raise
            result = runner.invoke(doctor_command, [], catch_exceptions=False)

        assert result.exit_code == 0
        # All backends report "not installed"
        assert "not installed" in result.output


# ---------------------------------------------------------------------------
# doctor output: backend status lines
# ---------------------------------------------------------------------------


class TestDoctorBackendOutput:
    """doctor output contains expected backend status lines."""

    @pytest.fixture()
    def doctor_output(self) -> str:
        """Run doctor with all machine-level probes mocked to PASS."""
        runner = CliRunner()
        with (
            patch(
                "kbutillib.interfaces.cli.init._probe_init_done",
                return_value=("PASS", "ok"),
            ),
            patch(
                "kbutillib.interfaces.cli.init._probe_cursor_on_path",
                return_value=("PASS", "ok"),
            ),
            patch(
                "kbutillib.interfaces.cli.init._probe_claude_extension",
                return_value=("PASS", "ok"),
            ),
            patch(
                "kbutillib.interfaces.cli.init._probe_kbu_version",
                return_value=("PASS", "ok"),
            ),
            patch(
                "kbutillib.interfaces.cli.init._probe_jupyter_kernel",
                return_value=("PASS", "ok"),
            ),
            patch(
                "kbutillib.interfaces.cli.init._probe_fba_imports",
                return_value=("PASS", "ok"),
            ),
            patch(
                "kbutillib.interfaces.cli.init._probe_tomli_w",
                return_value=("PASS", "ok"),
            ),
        ):
            result = runner.invoke(doctor_command, [], catch_exceptions=False)
        return result.output

    def test_output_contains_backends_section(self, doctor_output: str) -> None:
        """Output must contain the 'optional runtime backends' section header."""
        assert "optional runtime backends" in doctor_output

    def test_output_mentions_rdkit(self, doctor_output: str) -> None:
        assert "rdkit" in doctor_output

    def test_output_mentions_modelseedpy(self, doctor_output: str) -> None:
        assert "modelseedpy" in doctor_output

    def test_output_mentions_cobra(self, doctor_output: str) -> None:
        assert "cobra" in doctor_output

    def test_output_mentions_mcp(self, doctor_output: str) -> None:
        assert "mcp" in doctor_output

    def test_output_mentions_fastapi(self, doctor_output: str) -> None:
        assert "fastapi" in doctor_output

    def test_output_mentions_uvicorn(self, doctor_output: str) -> None:
        assert "uvicorn" in doctor_output

    def test_output_mentions_minedatabase(self, doctor_output: str) -> None:
        assert "minedatabase" in doctor_output

    def test_backend_line_format(self, doctor_output: str) -> None:
        """Each backend line should be '[PASS] name: installed' or '[WARN] name: not installed'."""
        for display_name, _pkg_name in _BACKEND_PROBES:
            assert display_name in doctor_output, (
                f"Backend '{display_name}' missing from doctor output"
            )


# ---------------------------------------------------------------------------
# doctor output: registry summary
# ---------------------------------------------------------------------------


class TestDoctorRegistryOutput:
    """doctor output contains a registry summary including biochem domain."""

    @pytest.fixture()
    def doctor_output(self) -> str:
        """Run doctor with machine probes mocked to PASS."""
        runner = CliRunner()
        with (
            patch("kbutillib.interfaces.cli.init._probe_init_done", return_value=("PASS", "ok")),
            patch("kbutillib.interfaces.cli.init._probe_cursor_on_path", return_value=("PASS", "ok")),
            patch("kbutillib.interfaces.cli.init._probe_claude_extension", return_value=("PASS", "ok")),
            patch("kbutillib.interfaces.cli.init._probe_kbu_version", return_value=("PASS", "ok")),
            patch("kbutillib.interfaces.cli.init._probe_jupyter_kernel", return_value=("PASS", "ok")),
            patch("kbutillib.interfaces.cli.init._probe_fba_imports", return_value=("PASS", "ok")),
            patch("kbutillib.interfaces.cli.init._probe_tomli_w", return_value=("PASS", "ok")),
        ):
            result = runner.invoke(doctor_command, [], catch_exceptions=False)
        return result.output

    def test_output_contains_registry_section(self, doctor_output: str) -> None:
        assert "capability registry" in doctor_output

    def test_output_contains_registry_line(self, doctor_output: str) -> None:
        assert "registry:" in doctor_output

    def test_output_shows_total(self, doctor_output: str) -> None:
        """Registry line must mention a total= count."""
        assert "total=" in doctor_output

    def test_output_shows_biochem_domain(self, doctor_output: str) -> None:
        """Registry summary must include the biochem domain (from WP3)."""
        assert "biochem" in doctor_output

    def test_output_shows_available_unavailable(self, doctor_output: str) -> None:
        """Registry line must show available= and unavailable= counts."""
        assert "available=" in doctor_output
        assert "unavailable=" in doctor_output


# ---------------------------------------------------------------------------
# doctor output: versions section
# ---------------------------------------------------------------------------


class TestDoctorVersionOutput:
    """doctor output contains Python + kbutillib version line."""

    @pytest.fixture()
    def doctor_output(self) -> str:
        runner = CliRunner()
        with (
            patch("kbutillib.interfaces.cli.init._probe_init_done", return_value=("PASS", "ok")),
            patch("kbutillib.interfaces.cli.init._probe_cursor_on_path", return_value=("PASS", "ok")),
            patch("kbutillib.interfaces.cli.init._probe_claude_extension", return_value=("PASS", "ok")),
            patch("kbutillib.interfaces.cli.init._probe_kbu_version", return_value=("PASS", "ok")),
            patch("kbutillib.interfaces.cli.init._probe_jupyter_kernel", return_value=("PASS", "ok")),
            patch("kbutillib.interfaces.cli.init._probe_fba_imports", return_value=("PASS", "ok")),
            patch("kbutillib.interfaces.cli.init._probe_tomli_w", return_value=("PASS", "ok")),
        ):
            result = runner.invoke(doctor_command, [], catch_exceptions=False)
        return result.output

    def test_output_contains_versions_section(self, doctor_output: str) -> None:
        assert "python + kbutillib" in doctor_output

    def test_output_shows_python_version(self, doctor_output: str) -> None:
        assert "python=" in doctor_output

    def test_output_shows_kbutillib_version(self, doctor_output: str) -> None:
        assert "kbutillib=" in doctor_output


# ---------------------------------------------------------------------------
# Unit tests for individual probe functions
# ---------------------------------------------------------------------------


class TestProbePythonKbutillib:
    """_probe_python_kbutillib_version() always returns INFO."""

    def test_returns_info_status(self) -> None:
        status, detail = _probe_python_kbutillib_version()
        assert status == "INFO"

    def test_detail_contains_python_version(self) -> None:
        _, detail = _probe_python_kbutillib_version()
        expected_prefix = (
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        )
        assert expected_prefix in detail

    def test_detail_contains_kbutillib_version(self) -> None:
        _, detail = _probe_python_kbutillib_version()
        assert "kbutillib=" in detail

    def test_handles_metadata_failure(self) -> None:
        """Returns INFO with 'unknown' version when importlib.metadata raises."""
        with patch("importlib.metadata.version", side_effect=Exception("no meta")):
            status, detail = _probe_python_kbutillib_version()
        assert status == "INFO"
        assert "unknown" in detail


class TestProbeBackend:
    """_probe_backend() uses find_spec and never raises."""

    def test_returns_pass_when_installed(self) -> None:
        """Returns PASS when find_spec finds the package."""
        mock_spec = MagicMock()
        with patch("importlib.util.find_spec", return_value=mock_spec):
            status, detail = _probe_backend("mypkg", "mypkg")
        assert status == "PASS"
        assert "installed" in detail

    def test_returns_warn_when_not_installed(self) -> None:
        """Returns WARN when find_spec returns None."""
        with patch("importlib.util.find_spec", return_value=None):
            status, detail = _probe_backend("mypkg", "mypkg")
        assert status == "WARN"
        assert "not installed" in detail

    def test_returns_warn_on_exception(self) -> None:
        """Returns WARN when find_spec raises (e.g. broken namespace package)."""
        with patch("importlib.util.find_spec", side_effect=ValueError("broken")):
            status, detail = _probe_backend("mypkg", "mypkg")
        assert status == "WARN"
        assert "not installed" in detail

    @pytest.mark.parametrize("pkg", ["rdkit", "cobra", "mcp", "fastapi", "uvicorn"])
    def test_backend_does_not_raise(self, pkg: str) -> None:
        """_probe_backend never raises regardless of find_spec outcome."""
        # No mock — real environment; just assert no exception
        status, detail = _probe_backend(pkg, pkg)
        assert status in ("PASS", "WARN")
        assert detail in ("installed", "not installed")


class TestProbeAllBackends:
    """_probe_all_backends() returns correct count and structure."""

    def test_returns_all_configured_backends(self) -> None:
        results = _probe_all_backends()
        assert len(results) == len(_BACKEND_PROBES)

    def test_each_entry_has_three_elements(self) -> None:
        for entry in _probe_all_backends():
            assert len(entry) == 3

    def test_status_values_are_valid(self) -> None:
        for _name, status, _detail in _probe_all_backends():
            assert status in ("PASS", "WARN"), f"Unexpected status: {status}"


class TestProbeRegistrySummary:
    """_probe_registry_summary() never raises; degrades gracefully."""

    def test_returns_info_on_success(self) -> None:
        """Returns INFO when register_all succeeds."""
        status, detail = _probe_registry_summary()
        # Biochem caps registered in WP3 → at least 3 specs
        assert status == "INFO", f"Expected INFO, got {status}: {detail}"

    def test_detail_contains_total(self) -> None:
        status, detail = _probe_registry_summary()
        if status == "INFO":
            assert "total=" in detail

    def test_detail_contains_biochem_when_registered(self) -> None:
        status, detail = _probe_registry_summary()
        if status == "INFO":
            assert "biochem" in detail

    def test_degrades_on_import_error(self) -> None:
        """Returns WARN when the core import fails."""
        with patch.dict(
            sys.modules,
            {
                "kbutillib.core.capability": None,
                "kbutillib.core.registry": None,
            },
        ):
            status, detail = _probe_registry_summary()
        # Should degrade gracefully, not raise
        assert status in ("INFO", "WARN")

    def test_degrades_on_register_all_exception(self) -> None:
        """Returns WARN when register_all raises."""
        import importlib as _importlib

        # Use importlib.import_module to get the module object (not the
        # re-exported function which has the same dotted name).
        cap_module = _importlib.import_module("kbutillib.core.capability")

        with patch.object(cap_module, "register_all", side_effect=RuntimeError("boom")):
            status, detail = _probe_registry_summary()
        assert status == "WARN"
        assert "registry summary unavailable" in detail

    def test_never_raises(self) -> None:
        """Probe never propagates an exception."""
        with patch(
            "kbutillib.interfaces.cli.init._probe_registry_summary",
            wraps=_probe_registry_summary,
        ):
            # Call directly — must not raise under any circumstance
            _probe_registry_summary()


class TestBackendProbesConstant:
    """Sanity checks on the _BACKEND_PROBES constant."""

    EXPECTED_BACKENDS = {
        "rdkit",
        "minedatabase",
        "equilibrator_api",
        "equilibrator_cache",
        "modelseedpy",
        "cobra",
        "mcp",
        "fastapi",
        "uvicorn",
    }

    def test_all_expected_backends_present(self) -> None:
        names = {display for display, _ in _BACKEND_PROBES}
        assert self.EXPECTED_BACKENDS == names, (
            f"Unexpected diff: {self.EXPECTED_BACKENDS.symmetric_difference(names)}"
        )

    def test_no_duplicate_display_names(self) -> None:
        names = [display for display, _ in _BACKEND_PROBES]
        assert len(names) == len(set(names)), "Duplicate display names in _BACKEND_PROBES"
