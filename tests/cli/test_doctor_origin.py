"""Tests for the `kbu doctor` project-origin probe (Acceptance Criterion 34).

Three branches:
1. kbu-project.toml with [project].bootstrapped = true  → 'project origin: bootstrap (<ts>)'
2. kbu-project.toml with bootstrapped absent (or False)  → 'project origin: new-project (<ts>)'
3. No kbu-project.toml in cwd                            → 'project origin: (no kbu-project.toml in cwd)'
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from kbutillib.cli import main
from kbutillib.cli.init import _probe_project_origin


# ---------------------------------------------------------------------------
# Unit tests for _probe_project_origin (patch read_project_manifest in init)
# ---------------------------------------------------------------------------


class TestProbeProjectOriginViaManifestPatch:
    """Tests that patch read_project_manifest directly to avoid cwd dependency.

    This is the cleanest approach: _probe_project_origin calls
    read_project_manifest(Path.cwd()), so we patch the function in the
    kbutillib.cli.init namespace.
    """

    def test_bootstrapped_true(self) -> None:
        ts = "2026-06-06T15:30:00Z"
        manifest = {
            "project": {
                "name": "test_project",
                "created_at": ts,
                "bootstrapped": True,
                "bootstrapped_at": ts,
            }
        }
        with patch("kbutillib.cli.init.read_project_manifest", return_value=manifest):
            result = _probe_project_origin()
        assert result == f"project origin: bootstrap ({ts})"

    def test_bootstrapped_absent(self) -> None:
        ts = "2026-06-05T12:00:00Z"
        manifest = {
            "project": {
                "name": "test_project",
                "created_at": ts,
            }
        }
        with patch("kbutillib.cli.init.read_project_manifest", return_value=manifest):
            result = _probe_project_origin()
        assert result == f"project origin: new-project ({ts})"

    def test_bootstrapped_false(self) -> None:
        ts = "2026-06-05T12:00:00Z"
        manifest = {
            "project": {
                "name": "test_project",
                "created_at": ts,
                "bootstrapped": False,
            }
        }
        with patch("kbutillib.cli.init.read_project_manifest", return_value=manifest):
            result = _probe_project_origin()
        assert result == f"project origin: new-project ({ts})"

    def test_no_manifest_file_not_found(self) -> None:
        with patch(
            "kbutillib.cli.init.read_project_manifest",
            side_effect=FileNotFoundError("no manifest"),
        ):
            result = _probe_project_origin()
        assert result == "project origin: (no kbu-project.toml in cwd)"


# ---------------------------------------------------------------------------
# Integration tests via kbu doctor CLI output
# ---------------------------------------------------------------------------


class TestDoctorOriginLine:
    """Verify the origin line appears in `kbu doctor` output."""

    def _run_doctor_with_manifest(self, manifest_return_value: dict | None) -> str:
        """Run `kbu doctor` with mocked probes + mocked read_project_manifest.

        Returns the raw combined output string.
        """
        def _mock_subproc(cmd, **kwargs):  # noqa: ANN001
            from unittest.mock import MagicMock
            r = MagicMock()
            r.returncode = 0
            if cmd and "jupyter" in cmd[0] and "kernelspec" in cmd:
                r.stdout = json.dumps({"kernelspecs": {}})
            else:
                r.stdout = ""
            r.stderr = ""
            return r

        patches: list = [
            patch("kbutillib.cli.init.shutil.which", return_value=None),
            patch("kbutillib.cli.init.subprocess.run", side_effect=_mock_subproc),
        ]
        if manifest_return_value is None:
            patches.append(
                patch(
                    "kbutillib.cli.init.read_project_manifest",
                    side_effect=FileNotFoundError("no manifest"),
                )
            )
        else:
            patches.append(
                patch(
                    "kbutillib.cli.init.read_project_manifest",
                    return_value=manifest_return_value,
                )
            )

        runner = CliRunner()
        with patches[0], patches[1], patches[2]:
            result = runner.invoke(main, ["doctor"], catch_exceptions=False)
        return result.output

    def test_doctor_bootstrapped_origin_in_output(self) -> None:
        ts = "2026-06-06T15:30:00Z"
        manifest = {
            "project": {
                "name": "test_project",
                "created_at": ts,
                "bootstrapped": True,
                "bootstrapped_at": ts,
            }
        }
        output = self._run_doctor_with_manifest(manifest)
        assert f"project origin: bootstrap ({ts})" in output

    def test_doctor_new_project_origin_in_output(self) -> None:
        ts = "2026-06-05T12:00:00Z"
        manifest = {
            "project": {
                "name": "test_project",
                "created_at": ts,
            }
        }
        output = self._run_doctor_with_manifest(manifest)
        assert f"project origin: new-project ({ts})" in output

    def test_doctor_no_manifest_origin_in_output(self) -> None:
        output = self._run_doctor_with_manifest(None)
        assert "project origin: (no kbu-project.toml in cwd)" in output

    def test_doctor_origin_line_is_last_non_empty_line(self) -> None:
        """The origin line appears after all [STATUS] probe lines."""
        output = self._run_doctor_with_manifest(None)
        lines = [ln for ln in output.strip().splitlines() if ln.strip()]
        assert lines[-1].startswith("project origin:"), (
            f"Expected last line to be project origin, got: {lines[-1]!r}"
        )

    def test_doctor_origin_does_not_affect_exit_code(self) -> None:
        """origin probe is informational; a missing manifest does not cause exit 1."""
        def _mock_subproc(cmd, **kwargs):  # noqa: ANN001
            from unittest.mock import MagicMock
            r = MagicMock()
            r.returncode = 0
            if cmd and "jupyter" in cmd[0] and "kernelspec" in cmd:
                r.stdout = json.dumps({"kernelspecs": {}})
            else:
                r.stdout = ""
            r.stderr = ""
            return r

        # Set up so all 5 status probes pass/skip (no FAIL)
        from kbutillib.cli.init import _write_marker
        import tempfile, os
        with tempfile.TemporaryDirectory() as cfg_dir:
            fake_python = Path(cfg_dir) / "venv" / "bin" / "python"
            fake_python.parent.mkdir(parents=True)
            fake_python.touch()
            fake_python.chmod(0o755)

            runner = CliRunner()
            with (
                patch("kbutillib.cli.init.shutil.which", return_value=None),
                patch("kbutillib.cli.init.subprocess.run", side_effect=_mock_subproc),
                patch(
                    "kbutillib.cli.init.read_project_manifest",
                    side_effect=FileNotFoundError("no manifest"),
                ),
                patch(
                    "kbutillib.cli.init.init_status",
                    return_value=0,  # init probe passes
                ),
                patch(
                    "kbutillib.cli.init._probe_init_done",
                    return_value=("PASS", "init marker present"),
                ),
            ):
                result = runner.invoke(main, ["doctor"], catch_exceptions=False)

        # Origin line absent from manifest should NOT cause exit 1
        assert "project origin: (no kbu-project.toml in cwd)" in result.output
