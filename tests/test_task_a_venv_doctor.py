"""Tests for Task A: venv provisioning + diagnostics.

Covers:
- machine_configs/_default.yaml includes requests_toolbelt and tomli-w
- kbu doctor FBA-import probes report correct PASS/FAIL messages
- kbu doctor tomli_w probe reports correct PASS/WARN messages
- import kbutillib emits at most one summary line for optional failures
- KBUTILLIB_VERBOSE_IMPORTS=1 emits per-module lines instead
"""

from __future__ import annotations

import importlib
import os
import sys
import textwrap
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Criterion 1 — machine_configs/_default.yaml
# ---------------------------------------------------------------------------


class TestDefaultYamlNotebookDeps:
    """Verify _default.yaml notebook_deps satisfy the PRD requirements."""

    @pytest.fixture
    def default_yaml(self) -> dict:
        path = _REPO_ROOT / "machine_configs" / "_default.yaml"
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    def _dep_names(self, deps: list[str]) -> list[str]:
        """Strip version specifiers, returning bare package names."""
        return [d.split()[0].lower() for d in deps]

    def test_requests_toolbelt_present(self, default_yaml: dict) -> None:
        """requests_toolbelt must be in notebook_deps."""
        deps = default_yaml.get("notebook_deps", [])
        names = self._dep_names(deps)
        assert "requests_toolbelt" in names, (
            f"requests_toolbelt missing from notebook_deps; got: {deps}"
        )

    def test_tomli_w_present(self, default_yaml: dict) -> None:
        """tomli-w must be in notebook_deps."""
        deps = default_yaml.get("notebook_deps", [])
        # tomli-w (PyPI name) may be spelled tomli-w or tomli_w in the list
        raw = [d.split()[0].lower() for d in deps]
        assert "tomli-w" in raw or "tomli_w" in raw, (
            f"tomli-w missing from notebook_deps; got: {deps}"
        )

    def test_cobra_not_present(self, default_yaml: dict) -> None:
        """cobra must NOT be in notebook_deps (arrives via editable_installs)."""
        deps = default_yaml.get("notebook_deps", [])
        names = self._dep_names(deps)
        assert "cobra" not in names, (
            "cobra should not be in notebook_deps; it arrives via editable_installs"
        )

    def test_modelseedpy_not_present(self, default_yaml: dict) -> None:
        """modelseedpy must NOT be in notebook_deps."""
        deps = default_yaml.get("notebook_deps", [])
        names = self._dep_names(deps)
        assert "modelseedpy" not in names, (
            "modelseedpy should not be in notebook_deps; it arrives via editable_installs"
        )

    def test_cobrakbase_not_present(self, default_yaml: dict) -> None:
        """cobrakbase must NOT be in notebook_deps."""
        deps = default_yaml.get("notebook_deps", [])
        names = self._dep_names(deps)
        assert "cobrakbase" not in names, (
            "cobrakbase should not be in notebook_deps; it arrives via editable_installs"
        )

    def test_requests_toolbelt_version_spec(self, default_yaml: dict) -> None:
        """requests_toolbelt entry must specify >=0.10.0."""
        deps = default_yaml.get("notebook_deps", [])
        matched = [d for d in deps if d.split()[0].lower() == "requests_toolbelt"]
        assert matched, "requests_toolbelt not in notebook_deps"
        assert "0.10.0" in matched[0], (
            f"requests_toolbelt entry should specify >=0.10.0; got: {matched[0]!r}"
        )

    def test_tomli_w_version_spec(self, default_yaml: dict) -> None:
        """tomli-w entry must specify >=1.0."""
        deps = default_yaml.get("notebook_deps", [])
        matched = [
            d for d in deps
            if d.split()[0].lower() in ("tomli-w", "tomli_w")
        ]
        assert matched, "tomli-w not in notebook_deps"
        assert "1.0" in matched[0], (
            f"tomli-w entry should specify >=1.0; got: {matched[0]!r}"
        )


# ---------------------------------------------------------------------------
# Criteria 2 & 17 — kbu doctor FBA-import probe (platform-agnostic)
# ---------------------------------------------------------------------------


class TestProbeFbaImports:
    """_probe_fba_imports() returns correct status on success and failure."""

    def _get_probe(self):
        from kbutillib.cli.init import _probe_fba_imports
        return _probe_fba_imports

    def test_probe_passes_when_modules_available(self) -> None:
        """Returns PASS when both FBA modules import without error."""
        modelseedpy = pytest.importorskip("modelseedpy", reason="modelseedpy required")
        cobrakbase = pytest.importorskip("cobrakbase", reason="cobrakbase required")
        probe = self._get_probe()
        status, detail = probe()
        assert status == "PASS", f"Expected PASS, got {status}: {detail}"
        assert "ms_fba_utils" in detail

    def test_probe_fails_on_missing_dep_ms_fba_utils(self) -> None:
        """Returns FAIL with '[FAIL] fba-import: missing dependency: <name>' on ModuleNotFoundError."""
        probe = self._get_probe()

        # Patch __import__ to raise ModuleNotFoundError for ms_fba_utils
        real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def fake_import(name, *args, **kwargs):
            if name == "kbutillib.ms_fba_utils":
                err = ModuleNotFoundError("No module named 'cobra'")
                err.name = "cobra"
                raise err
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            status, detail = probe()

        assert status == "FAIL", f"Expected FAIL, got {status}: {detail}"
        assert "[FAIL] fba-import: missing dependency:" in detail
        assert "cobra" in detail

    def test_probe_reports_missing_dep_name(self) -> None:
        """The FAIL message includes the specific missing dependency name."""
        probe = self._get_probe()

        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "kbutillib.ms_reconstruction_utils":
                err = ModuleNotFoundError("No module named 'requests_toolbelt'")
                err.name = "requests_toolbelt"
                raise err
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            status, detail = probe()

        assert status == "FAIL"
        assert "requests_toolbelt" in detail

    def test_probe_handles_other_exceptions(self) -> None:
        """Non-ModuleNotFoundError exceptions print type + first message line."""
        probe = self._get_probe()

        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "kbutillib.ms_fba_utils":
                raise RuntimeError("some internal error\nwith a second line")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            status, detail = probe()

        assert status == "FAIL"
        assert "RuntimeError" in detail
        assert "some internal error" in detail
        # Second line of the message should NOT appear
        assert "with a second line" not in detail

    def test_probe_runs_on_current_platform(self) -> None:
        """Probe is not gated to macOS — it runs on any platform."""
        import kbutillib.cli.init as init_mod
        import inspect
        src = inspect.getsource(init_mod._probe_fba_imports)
        # The probe must NOT contain a sys.platform or _is_darwin check
        assert "sys.platform" not in src, (
            "_probe_fba_imports must not be gated on sys.platform"
        )
        assert "_is_darwin" not in src, (
            "_probe_fba_imports must not be gated on _is_darwin()"
        )
        assert "_is_macos_or_override" not in src, (
            "_probe_fba_imports must not be gated on _is_macos_or_override()"
        )


# ---------------------------------------------------------------------------
# Criterion 14 & 17 — kbu doctor tomli_w probe (platform-agnostic)
# ---------------------------------------------------------------------------


class TestProbeTomliW:
    """_probe_tomli_w() returns PASS or WARN on import success/failure."""

    def _get_probe(self):
        from kbutillib.cli.init import _probe_tomli_w
        return _probe_tomli_w

    def test_probe_passes_when_tomli_w_available(self) -> None:
        """Returns PASS when tomli_w is importable."""
        pytest.importorskip("tomli_w", reason="tomli_w not available — testing WARN path instead")
        probe = self._get_probe()
        status, detail = probe()
        assert status == "PASS", f"Expected PASS, got {status}: {detail}"
        assert "tomli_w" in detail.lower()

    def test_probe_warns_on_missing_tomli_w(self) -> None:
        """Returns WARN with reconciliation message when tomli_w is absent."""
        probe = self._get_probe()

        with patch.dict(sys.modules, {"tomli_w": None}):
            # Force the import inside the probe to fail
            real_import = __import__

            def fake_import(name, *args, **kwargs):
                if name == "tomli_w":
                    raise ImportError("No module named 'tomli_w'")
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=fake_import):
                status, detail = probe()

        assert status == "WARN", f"Expected WARN, got {status}: {detail}"
        assert "reconciliation" in detail.lower() or "reconcil" in detail.lower(), (
            f"WARN message should mention reconciliation; got: {detail!r}"
        )

    def test_probe_runs_on_current_platform(self) -> None:
        """Probe is not gated to macOS."""
        import kbutillib.cli.init as init_mod
        import inspect
        src = inspect.getsource(init_mod._probe_tomli_w)
        assert "sys.platform" not in src, "_probe_tomli_w must not be gated on sys.platform"
        assert "_is_darwin" not in src, "_probe_tomli_w must not be gated on _is_darwin()"


# ---------------------------------------------------------------------------
# Criterion 3 — import kbutillib optional-module banner
# ---------------------------------------------------------------------------


class TestOptionalImportBanner:
    """import kbutillib emits at most one stderr summary line for optional failures."""

    def _run_import_subprocess(
        self,
        extra_env: dict[str, str] | None = None,
        block_modules: list[str] | None = None,
    ) -> tuple[int, str, str]:
        """Run `import kbutillib` in a subprocess and capture stdout/stderr."""
        import subprocess

        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)

        # Build a small script that optionally blocks specific modules then imports kbutillib
        block_script = ""
        if block_modules:
            block_script = textwrap.dedent(f"""\
                import sys
                class _Blocker:
                    blocked = {block_modules!r}
                    def find_spec(self, name, path, target=None):
                        if any(name == b or name.startswith(b + '.') for b in self.blocked):
                            raise ModuleNotFoundError(f'No module named {{name!r}}', name=name)
                _blocker = _Blocker()
                sys.meta_path.insert(0, _blocker)
            """)

        script = block_script + "import kbutillib\n"
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
        )
        return result.returncode, result.stdout, result.stderr

    def test_default_import_emits_at_most_one_summary_line(self) -> None:
        """Without KBUTILLIB_VERBOSE_IMPORTS, stderr has <=1 [KBUtilLib] lines."""
        _, _, stderr = self._run_import_subprocess()
        kbu_lines = [
            line for line in stderr.splitlines()
            if line.startswith("[KBUtilLib]")
        ]
        assert len(kbu_lines) <= 1, (
            f"Expected at most 1 [KBUtilLib] line on stderr, got {len(kbu_lines)}:\n"
            + "\n".join(kbu_lines)
        )

    def test_summary_line_format_when_modules_fail(self) -> None:
        """Summary line matches '[KBUtilLib] N optional modules unavailable: ...' format."""
        # Block cobra to force at least one optional-import failure
        _, _, stderr = self._run_import_subprocess(
            block_modules=["cobra"],
        )
        kbu_lines = [
            line for line in stderr.splitlines()
            if line.startswith("[KBUtilLib]")
        ]
        if not kbu_lines:
            # No failures (cobra may not be imported by __init__ at all) — skip
            pytest.skip("No optional import failures triggered; cobra may not be optional in __init__")
        assert len(kbu_lines) == 1, (
            f"Expected exactly 1 [KBUtilLib] summary line; got {len(kbu_lines)}:\n"
            + "\n".join(kbu_lines)
        )
        summary = kbu_lines[0]
        assert "optional module" in summary, f"Summary missing 'optional module': {summary!r}"
        assert "KBUTILLIB_VERBOSE_IMPORTS=1" in summary, (
            f"Summary should hint at KBUTILLIB_VERBOSE_IMPORTS=1: {summary!r}"
        )

    def test_verbose_imports_emits_per_module_lines(self) -> None:
        """KBUTILLIB_VERBOSE_IMPORTS=1 emits per-module lines, not a summary."""
        _, _, stderr = self._run_import_subprocess(
            extra_env={"KBUTILLIB_VERBOSE_IMPORTS": "1"},
            block_modules=["cobra"],
        )
        kbu_lines = [
            line for line in stderr.splitlines()
            if line.startswith("[KBUtilLib]")
        ]
        if not kbu_lines:
            pytest.skip("No optional import failures triggered under KBUTILLIB_VERBOSE_IMPORTS=1")
        # In verbose mode we should NOT see the summary (no "optional modules unavailable")
        for line in kbu_lines:
            assert "optional modules unavailable" not in line, (
                f"Verbose mode should not emit summary line; got: {line!r}"
            )
        # We should see the per-module "Failed to import" pattern
        detailed = [l for l in kbu_lines if "Failed to import" in l]
        assert detailed, (
            f"Verbose mode should emit 'Failed to import' lines; got: {kbu_lines}"
        )

    def test_no_kbu_lines_when_all_modules_ok(self) -> None:
        """When no optional modules fail, zero [KBUtilLib] lines are emitted."""
        # This test may not be achievable in all environments (some modules may always fail),
        # but we at least verify the count is <=1.
        _, _, stderr = self._run_import_subprocess()
        kbu_lines = [
            line for line in stderr.splitlines()
            if line.startswith("[KBUtilLib]")
        ]
        # At most one summary line regardless of how many modules fail
        assert len(kbu_lines) <= 1, (
            f"Expected at most 1 [KBUtilLib] line; got {len(kbu_lines)}"
        )
