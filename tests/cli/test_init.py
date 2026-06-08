"""Tests for kbutillib.cli.init — ``kbu init`` and ``kbu doctor`` subcommands."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from kbutillib.cli import main
from kbutillib.cli.init import (
    _marker_path,
    _parse_virtual_env_from_activate,
    _read_marker,
    _write_marker,
    init_status,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invoke(*args: str, env: dict | None = None) -> Any:
    """Invoke kbu CLI with the given args via CliRunner.

    Uses catch_exceptions=False so unexpected exceptions surface as
    test failures rather than hidden exit-code 1s.  SystemExit is still
    captured by the runner (exit_code reflects it).
    """
    runner = CliRunner()
    return runner.invoke(main, list(args), catch_exceptions=False, env=env)


# ---------------------------------------------------------------------------
# Marker file helpers
# ---------------------------------------------------------------------------


class TestMarkerPath:
    def test_xdg_config_home_respected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        custom = tmp_path / "xdg_config"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(custom))
        p = _marker_path()
        assert p == custom / "kbu" / "init_done.json"

    def test_default_path_without_xdg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        p = _marker_path()
        assert p.name == "init_done.json"
        assert p.parent.name == "kbu"
        # Should be somewhere under home config
        assert ".config" in str(p)


class TestWriteMarker:
    def test_schema_fields_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        _write_marker(
            kbutillib_repo_path="/fake/kbutillib",
            kbutillib_commit="a" * 40,
            venv_manager="venvman",
            venv_python="/fake/venv/bin/python",
            jupyter_kernel_name="kbutillib",
        )
        marker = _read_marker()
        assert marker is not None
        assert marker["version"] == 1
        assert "initialized_at" in marker
        assert marker["initialized_at"].endswith("Z"), "timestamp must end with Z"
        assert marker["kbutillib_repo_path"] == "/fake/kbutillib"
        assert marker["kbutillib_commit"] == "a" * 40
        assert marker["venv_manager"] == "venvman"
        assert marker["venv_python"] == "/fake/venv/bin/python"
        assert marker["jupyter_kernel_name"] == "kbutillib"

    def test_creates_parent_dirs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        xdg = tmp_path / "deep" / "config"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        _write_marker(
            kbutillib_repo_path="/x",
            kbutillib_commit="b" * 40,
            venv_manager=".venv",
            venv_python="/x/.venv/bin/python",
        )
        assert (xdg / "kbu" / "init_done.json").exists()


# ---------------------------------------------------------------------------
# init_status exit codes
# ---------------------------------------------------------------------------


class TestInitStatus:
    def test_returns_1_when_marker_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
        assert init_status() == 1

    def test_returns_0_when_marker_present_and_python_executable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        fake_python = tmp_path / "venv_bin" / "python"
        fake_python.parent.mkdir(parents=True)
        fake_python.touch()
        fake_python.chmod(0o755)
        _write_marker(
            kbutillib_repo_path="/fake",
            kbutillib_commit="c" * 40,
            venv_manager=".venv",
            venv_python=str(fake_python),
        )
        assert init_status() == 0

    def test_returns_2_when_marker_present_but_python_gone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        _write_marker(
            kbutillib_repo_path="/fake",
            kbutillib_commit="d" * 40,
            venv_manager=".venv",
            venv_python="/nonexistent/path/python",
        )
        assert init_status() == 2


# ---------------------------------------------------------------------------
# kbu init --status CLI command
# ---------------------------------------------------------------------------


class TestInitStatusCli:
    def test_exit_1_when_no_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
        result = _invoke("init", "--status")
        assert result.exit_code == 1

    def test_exit_0_when_marker_and_python_executable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        fake_python = tmp_path / "venv_bin" / "python"
        fake_python.parent.mkdir(parents=True)
        fake_python.touch()
        fake_python.chmod(0o755)
        _write_marker(
            kbutillib_repo_path="/fake",
            kbutillib_commit="e" * 40,
            venv_manager=".venv",
            venv_python=str(fake_python),
        )
        result = _invoke("init", "--status")
        assert result.exit_code == 0

    def test_exit_2_when_marker_but_python_gone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        _write_marker(
            kbutillib_repo_path="/fake",
            kbutillib_commit="f" * 40,
            venv_manager=".venv",
            venv_python="/nonexistent/python",
        )
        result = _invoke("init", "--status")
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Non-macOS platform check
# ---------------------------------------------------------------------------


class TestNonDarwinPlatform:
    def test_non_darwin_without_override_exits_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.delenv("KBU_PLATFORM_OVERRIDE", raising=False)
        result = _invoke("init")
        assert result.exit_code == 1

    def test_non_darwin_without_override_prints_v1_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.delenv("KBU_PLATFORM_OVERRIDE", raising=False)
        result = _invoke("init")
        assert "v1 currently targets macOS" in result.output
        assert "python -m venv" in result.output

    def test_non_darwin_with_override_proceeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("KBU_PLATFORM_OVERRIDE", "force")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

        fake_python = tmp_path / ".venv" / "bin" / "python"
        fake_python.parent.mkdir(parents=True)
        fake_python.touch()
        fake_python.chmod(0o755)

        def _mock_subproc(cmd, **kwargs):  # noqa: ANN001
            result = MagicMock()
            result.returncode = 0
            result.stdout = "a" * 40 + "\n"
            result.stderr = ""
            return result

        with (
            patch("kbutillib.cli.init._create_plain_venv", return_value=fake_python),
            patch("kbutillib.cli.init.subprocess.run", side_effect=_mock_subproc),
            patch("kbutillib.cli.init._kbutillib_commit", return_value="a" * 40),
        ):
            result = _invoke("init")

        # Should not exit with the macOS-only message
        assert "v1 currently targets macOS" not in result.output
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# venvman detection path
# ---------------------------------------------------------------------------


class TestVenvmanDetection:
    def test_venvman_subprocess_uses_correct_args(self, tmp_path: Path) -> None:
        """_run_venvman calls subprocess.run with the exact right args."""
        from kbutillib.cli.init import _run_venvman

        activate_sh = tmp_path / "activate.sh"
        activate_sh.write_text(
            'VIRTUAL_ENV="' + str(tmp_path / "venv") + '"\nexport VIRTUAL_ENV\n',
            encoding="utf-8",
        )
        fake_python = tmp_path / "venv" / "bin" / "python"
        fake_python.parent.mkdir(parents=True)
        fake_python.touch()
        fake_python.chmod(0o755)

        captured: list[list] = []

        def _mock(cmd, **kwargs):  # noqa: ANN001
            captured.append(list(cmd))
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with patch("kbutillib.cli.init.subprocess.run", side_effect=_mock):
            returned_path, returned_err = _run_venvman(tmp_path)

        assert len(captured) == 1
        assert captured[0] == [
            "venvman",
            "create",
            "--project",
            "kbutillib",
            "--dir",
            str(tmp_path),
            "--python",
            "3.11",
        ]
        assert returned_path == fake_python
        assert returned_err == ""

    def test_venvman_present_uses_venvman(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When venvman is on PATH on macOS, venvman path is taken."""
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

        fake_python = tmp_path / "venv" / "bin" / "python"
        fake_python.parent.mkdir(parents=True)
        fake_python.touch()
        fake_python.chmod(0o755)

        venvman_calls: list[list] = []

        def _mock_run_venvman(repo_root: Path) -> tuple[Path, str]:
            venvman_calls.append([
                "venvman", "create", "--project", "kbutillib",
                "--dir", str(repo_root), "--python", "3.11",
            ])
            return fake_python, ""

        def _mock_subproc(cmd, **kwargs):  # noqa: ANN001
            result = MagicMock()
            result.returncode = 0
            result.stdout = "a" * 40 + "\n"
            result.stderr = ""
            return result

        with (
            patch("kbutillib.cli.init.shutil.which", return_value="/usr/local/bin/venvman"),
            patch("kbutillib.cli.init._run_venvman", side_effect=_mock_run_venvman),
            patch("kbutillib.cli.init.subprocess.run", side_effect=_mock_subproc),
            patch("kbutillib.cli.init._kbutillib_commit", return_value="a" * 40),
        ):
            result = _invoke("init")

        assert result.exit_code == 0
        assert len(venvman_calls) == 1
        call = venvman_calls[0]
        assert call[0] == "venvman"
        assert call[1] == "create"
        assert "--project" in call and call[call.index("--project") + 1] == "kbutillib"
        assert "--python" in call and call[call.index("--python") + 1] == "3.11"
        assert "--dir" in call

        # Confirm marker records venvman as manager
        marker = _read_marker()
        assert marker is not None
        assert marker["venv_manager"] == "venvman"


# ---------------------------------------------------------------------------
# venvman failure → fallback to python -m venv
# ---------------------------------------------------------------------------


class TestVenvmanFailureFallback:
    def test_venvman_failure_falls_back_to_plain_venv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

        fake_python = tmp_path / ".venv" / "bin" / "python"
        fake_python.parent.mkdir(parents=True)
        fake_python.touch()
        fake_python.chmod(0o755)

        def _mock_subproc(cmd, **kwargs):  # noqa: ANN001
            result = MagicMock()
            result.returncode = 0
            result.stdout = "a" * 40 + "\n"
            result.stderr = ""
            return result

        venvman_error_detail = "venvman: python 3.11 toolchain missing"

        with (
            patch("kbutillib.cli.init.shutil.which", return_value="/usr/local/bin/venvman"),
            patch(
                "kbutillib.cli.init._run_venvman",
                return_value=(None, venvman_error_detail),
            ),
            patch("kbutillib.cli.init._create_plain_venv", return_value=fake_python),
            patch("kbutillib.cli.init.subprocess.run", side_effect=_mock_subproc),
            patch("kbutillib.cli.init._kbutillib_commit", return_value="a" * 40),
        ):
            result = _invoke("init")

        assert result.exit_code == 0
        # Warning about venvman fallback should be in combined output and must
        # name the underlying venvman error (per AC — warning surfaces detail).
        assert "venvman" in result.output.lower() or "fall" in result.output.lower()
        assert venvman_error_detail in result.output
        # Marker should be written with .venv manager (fallback)
        marker = _read_marker()
        assert marker is not None
        assert marker["venv_manager"] == ".venv"

    def test_venvman_absent_uses_plain_venv_directly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When venvman is absent, plain .venv is used without any fallback message."""
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

        fake_python = tmp_path / ".venv" / "bin" / "python"
        fake_python.parent.mkdir(parents=True)
        fake_python.touch()
        fake_python.chmod(0o755)

        def _mock_subproc(cmd, **kwargs):  # noqa: ANN001
            result = MagicMock()
            result.returncode = 0
            result.stdout = "a" * 40 + "\n"
            result.stderr = ""
            return result

        with (
            patch("kbutillib.cli.init.shutil.which", return_value=None),
            patch("kbutillib.cli.init._create_plain_venv", return_value=fake_python),
            patch("kbutillib.cli.init.subprocess.run", side_effect=_mock_subproc),
            patch("kbutillib.cli.init._kbutillib_commit", return_value="a" * 40),
        ):
            result = _invoke("init")

        assert result.exit_code == 0
        # No "falling back" warning when venvman is simply absent
        assert "falling back" not in result.output.lower()
        # Marker should be written with .venv manager
        marker = _read_marker()
        assert marker is not None
        assert marker["venv_manager"] == ".venv"


# ---------------------------------------------------------------------------
# Marker schema validation after successful init
# ---------------------------------------------------------------------------


class TestMarkerAfterInit:
    def test_marker_written_on_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

        fake_python = tmp_path / ".venv" / "bin" / "python"
        fake_python.parent.mkdir(parents=True)
        fake_python.touch()
        fake_python.chmod(0o755)

        def _mock_subproc(cmd, **kwargs):  # noqa: ANN001
            result = MagicMock()
            result.returncode = 0
            result.stdout = "a" * 40 + "\n"
            result.stderr = ""
            return result

        with (
            patch("kbutillib.cli.init.shutil.which", return_value=None),
            patch("kbutillib.cli.init._create_plain_venv", return_value=fake_python),
            patch("kbutillib.cli.init.subprocess.run", side_effect=_mock_subproc),
            patch("kbutillib.cli.init._kbutillib_commit", return_value="a" * 40),
        ):
            result = _invoke("init")

        assert result.exit_code == 0
        marker = _read_marker()
        assert marker is not None
        assert marker["version"] == 1
        assert marker["venv_manager"] == ".venv"
        assert marker["jupyter_kernel_name"] == "kbutillib"
        assert marker["venv_python"] == str(fake_python)
        assert len(marker.get("kbutillib_commit", "")) > 0


# ---------------------------------------------------------------------------
# activate.sh parser
# ---------------------------------------------------------------------------


class TestParseActivateSh:
    def test_parses_double_quoted_virtual_env(self, tmp_path: Path) -> None:
        f = tmp_path / "activate.sh"
        venv_dir = tmp_path / "myenv"
        f.write_text(f'VIRTUAL_ENV="{venv_dir}"\nexport VIRTUAL_ENV\n', encoding="utf-8")
        result = _parse_virtual_env_from_activate(f)
        assert result == venv_dir

    def test_parses_unquoted_virtual_env(self, tmp_path: Path) -> None:
        f = tmp_path / "activate.sh"
        venv_dir = tmp_path / "myenv"
        f.write_text(f"VIRTUAL_ENV={venv_dir}\nexport VIRTUAL_ENV\n", encoding="utf-8")
        result = _parse_virtual_env_from_activate(f)
        assert result == venv_dir

    def test_returns_none_if_no_virtual_env_line(self, tmp_path: Path) -> None:
        f = tmp_path / "activate.sh"
        f.write_text("#!/bin/sh\nexport PATH\n", encoding="utf-8")
        assert _parse_virtual_env_from_activate(f) is None

    def test_returns_none_if_file_missing(self, tmp_path: Path) -> None:
        assert _parse_virtual_env_from_activate(tmp_path / "nosuchfile.sh") is None

    def test_parses_venv_subdir_with_env(self, tmp_path: Path, monkeypatch) -> None:
        """Current venvman format: composes path from VENV_SUBDIR + env var."""
        venv_root = tmp_path / "envroot"
        venv_root.mkdir()
        monkeypatch.setenv("VIRTUAL_ENVIRONMENT_DIRECTORY", str(venv_root))
        f = tmp_path / "activate.sh"
        f.write_text(
            'SCRIPT_DIR="$(pwd)"\nVENV_SUBDIR="myproject-py3.11"\n'
            'VENV_PATH="${VIRTUAL_ENVIRONMENT_DIRECTORY}/${VENV_SUBDIR}"\n',
            encoding="utf-8",
        )
        result = _parse_virtual_env_from_activate(f)
        assert result == venv_root / "myproject-py3.11"

    def test_venv_subdir_without_env_returns_none(self, tmp_path: Path, monkeypatch) -> None:
        """VENV_SUBDIR present but VIRTUAL_ENVIRONMENT_DIRECTORY unset → None."""
        monkeypatch.delenv("VIRTUAL_ENVIRONMENT_DIRECTORY", raising=False)
        f = tmp_path / "activate.sh"
        f.write_text('VENV_SUBDIR="myproject-py3.11"\n', encoding="utf-8")
        assert _parse_virtual_env_from_activate(f) is None

    def test_legacy_virtual_env_wins_over_venv_subdir(self, tmp_path: Path, monkeypatch) -> None:
        """If both formats appear, the literal VIRTUAL_ENV= line takes precedence."""
        monkeypatch.setenv("VIRTUAL_ENVIRONMENT_DIRECTORY", "/should/not/use")
        f = tmp_path / "activate.sh"
        legacy_path = tmp_path / "legacy_venv"
        f.write_text(
            f'VIRTUAL_ENV="{legacy_path}"\nVENV_SUBDIR="other"\n',
            encoding="utf-8",
        )
        assert _parse_virtual_env_from_activate(f) == legacy_path


# ---------------------------------------------------------------------------
# kbu doctor probes
# ---------------------------------------------------------------------------


class TestDoctorCommand:
    def test_doctor_prints_one_line_per_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))

        def _mock_subproc(cmd, **kwargs):  # noqa: ANN001
            r = MagicMock()
            r.returncode = 0
            if cmd and "jupyter" in cmd[0] and "kernelspec" in cmd:
                r.stdout = json.dumps({"kernelspecs": {}})
            else:
                r.stdout = ""
            r.stderr = ""
            return r

        with (
            patch("kbutillib.cli.init.shutil.which", return_value=None),
            patch("kbutillib.cli.init.subprocess.run", side_effect=_mock_subproc),
        ):
            result = _invoke("doctor")

        lines = [ln for ln in result.output.strip().splitlines() if ln.strip()]
        # 5 status-probe lines + 1 project-origin info line
        assert len(lines) == 6, f"Expected 6 output lines, got {len(lines)}: {lines}"
        probe_lines = lines[:5]
        for line in probe_lines:
            assert (
                line.startswith("[PASS]")
                or line.startswith("[FAIL]")
                or line.startswith("[SKIP]")
            ), f"Line does not start with status token: {line!r}"
        assert lines[5].startswith("project origin:"), (
            f"Last line should be project origin info: {lines[5]!r}"
        )

    def test_doctor_exits_0_when_all_pass_or_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

        fake_python = tmp_path / "venv_bin" / "python"
        fake_python.parent.mkdir(parents=True)
        fake_python.touch()
        fake_python.chmod(0o755)
        _write_marker(
            kbutillib_repo_path="/fake",
            kbutillib_commit="a" * 40,
            venv_manager=".venv",
            venv_python=str(fake_python),
        )

        def _mock_which(name: str) -> str | None:
            if name == "cursor":
                return "/usr/bin/cursor"
            if name == "kbu":
                return "/usr/bin/kbu"
            return None

        def _mock_subproc(cmd, **kwargs):  # noqa: ANN001
            r = MagicMock()
            r.returncode = 0
            if cmd and cmd[0] == "cursor" and "--list-extensions" in cmd:
                r.stdout = "anthropic.claude-code\n"
                r.stderr = ""
            elif cmd and cmd[0] == "kbu" and "--version" in cmd:
                r.stdout = "kbu, version 0.0.0\n"
                r.stderr = ""
            elif cmd and "jupyter" in cmd[0] and "kernelspec" in cmd:
                r.stdout = json.dumps({
                    "kernelspecs": {
                        "kbutillib": {"resource_dir": "/fake/kernels/kbutillib"}
                    }
                })
                r.stderr = ""
            else:
                r.stdout = ""
                r.stderr = ""
            return r

        with (
            patch("kbutillib.cli.init.shutil.which", side_effect=_mock_which),
            patch("kbutillib.cli.init.subprocess.run", side_effect=_mock_subproc),
        ):
            result = _invoke("doctor")

        assert result.exit_code == 0

    def test_doctor_exits_1_when_any_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No marker → init-done probe fails → exit 1
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))

        def _mock_subproc(cmd, **kwargs):  # noqa: ANN001
            r = MagicMock()
            r.returncode = 0
            if cmd and "jupyter" in cmd[0] and "kernelspec" in cmd:
                r.stdout = json.dumps({"kernelspecs": {}})
            else:
                r.stdout = ""
            r.stderr = ""
            return r

        with (
            patch("kbutillib.cli.init.shutil.which", return_value=None),
            patch("kbutillib.cli.init.subprocess.run", side_effect=_mock_subproc),
        ):
            result = _invoke("doctor")

        assert result.exit_code == 1

    def test_doctor_cursor_absent_marks_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))

        def _mock_subproc(cmd, **kwargs):  # noqa: ANN001
            r = MagicMock()
            r.returncode = 0
            if cmd and "jupyter" in cmd[0] and "kernelspec" in cmd:
                r.stdout = json.dumps({"kernelspecs": {}})
            else:
                r.stdout = ""
            r.stderr = ""
            return r

        with (
            patch("kbutillib.cli.init.shutil.which", return_value=None),
            patch("kbutillib.cli.init.subprocess.run", side_effect=_mock_subproc),
        ):
            result = _invoke("doctor")

        lines = result.output.strip().splitlines()
        cursor_line = next((l for l in lines if "cursor-on-path" in l), None)
        assert cursor_line is not None
        assert cursor_line.startswith("[FAIL]"), f"cursor-on-path line: {cursor_line!r}"

    def test_doctor_cursor_absent_skips_claude_extension_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If cursor is not on PATH, the claude-extension probe should be SKIP."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))

        def _mock_subproc(cmd, **kwargs):  # noqa: ANN001
            r = MagicMock()
            r.returncode = 0
            if cmd and "jupyter" in cmd[0] and "kernelspec" in cmd:
                r.stdout = json.dumps({"kernelspecs": {}})
            else:
                r.stdout = ""
            r.stderr = ""
            return r

        with (
            patch("kbutillib.cli.init.shutil.which", return_value=None),
            patch("kbutillib.cli.init.subprocess.run", side_effect=_mock_subproc),
        ):
            result = _invoke("doctor")

        lines = result.output.strip().splitlines()
        ext_line = next((l for l in lines if "claude-extension" in l), None)
        assert ext_line is not None
        assert ext_line.startswith("[SKIP]"), f"claude-extension line: {ext_line!r}"

    def test_doctor_cursor_present_extension_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))

        def _which(name: str) -> str | None:
            if name == "cursor":
                return "/usr/bin/cursor"
            return None

        def _mock_subproc(cmd, **kwargs):  # noqa: ANN001
            r = MagicMock()
            r.returncode = 0
            if cmd and cmd[0] == "cursor" and "--list-extensions" in cmd:
                r.stdout = "anthropic.claude-code\n"
                r.stderr = ""
            elif cmd and "jupyter" in cmd[0] and "kernelspec" in cmd:
                r.stdout = json.dumps({"kernelspecs": {}})
                r.stderr = ""
            else:
                r.stdout = ""
                r.stderr = ""
            return r

        with (
            patch("kbutillib.cli.init.shutil.which", side_effect=_which),
            patch("kbutillib.cli.init.subprocess.run", side_effect=_mock_subproc),
        ):
            result = _invoke("doctor")

        lines = result.output.strip().splitlines()
        ext_line = next((l for l in lines if "claude-extension" in l), None)
        assert ext_line is not None
        assert ext_line.startswith("[PASS]"), f"claude-extension line: {ext_line!r}"

    def test_doctor_cursor_present_extension_absent_is_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))

        def _which(name: str) -> str | None:
            if name == "cursor":
                return "/usr/bin/cursor"
            return None

        def _mock_subproc(cmd, **kwargs):  # noqa: ANN001
            r = MagicMock()
            r.returncode = 0
            if cmd and cmd[0] == "cursor" and "--list-extensions" in cmd:
                r.stdout = "ms-python.python\nother.ext\n"
                r.stderr = ""
            elif cmd and "jupyter" in cmd[0] and "kernelspec" in cmd:
                r.stdout = json.dumps({"kernelspecs": {}})
                r.stderr = ""
            else:
                r.stdout = ""
                r.stderr = ""
            return r

        with (
            patch("kbutillib.cli.init.shutil.which", side_effect=_which),
            patch("kbutillib.cli.init.subprocess.run", side_effect=_mock_subproc),
        ):
            result = _invoke("doctor")

        lines = result.output.strip().splitlines()
        ext_line = next((l for l in lines if "claude-extension" in l), None)
        assert ext_line is not None
        assert ext_line.startswith("[FAIL]"), f"claude-extension line: {ext_line!r}"

    def test_doctor_kbu_version_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))

        def _which(name: str) -> str | None:
            if name == "kbu":
                return "/usr/bin/kbu"
            return None

        def _mock_subproc(cmd, **kwargs):  # noqa: ANN001
            r = MagicMock()
            r.returncode = 0
            if cmd and cmd[0] == "kbu" and "--version" in cmd:
                r.stdout = "kbu, version 0.0.0\n"
                r.stderr = ""
            elif cmd and "jupyter" in cmd[0] and "kernelspec" in cmd:
                r.stdout = json.dumps({"kernelspecs": {}})
                r.stderr = ""
            else:
                r.stdout = ""
                r.stderr = ""
            return r

        with (
            patch("kbutillib.cli.init.shutil.which", side_effect=_which),
            patch("kbutillib.cli.init.subprocess.run", side_effect=_mock_subproc),
        ):
            result = _invoke("doctor")

        lines = result.output.strip().splitlines()
        kbu_line = next((l for l in lines if "kbu-version" in l), None)
        assert kbu_line is not None
        assert kbu_line.startswith("[PASS]"), f"kbu-version line: {kbu_line!r}"

    def test_doctor_kernel_registered_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))

        kernel_data = json.dumps({
            "kernelspecs": {
                "kbutillib": {"resource_dir": "/fake/kernels/kbutillib"}
            }
        })

        def _mock_subproc(cmd, **kwargs):  # noqa: ANN001
            r = MagicMock()
            r.returncode = 0
            if cmd and "jupyter" in cmd[0] and "kernelspec" in cmd:
                r.stdout = kernel_data
            else:
                r.stdout = ""
            r.stderr = ""
            return r

        with (
            patch("kbutillib.cli.init.shutil.which", return_value=None),
            patch("kbutillib.cli.init.subprocess.run", side_effect=_mock_subproc),
        ):
            result = _invoke("doctor")

        lines = result.output.strip().splitlines()
        kernel_line = next((l for l in lines if "jupyter-kernel" in l), None)
        assert kernel_line is not None
        assert kernel_line.startswith("[PASS]"), f"jupyter-kernel line: {kernel_line!r}"

    def test_doctor_kernel_not_registered_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))

        kernel_data = json.dumps({
            "kernelspecs": {
                "python3": {"resource_dir": "/fake/kernels/python3"}
            }
        })

        def _mock_subproc(cmd, **kwargs):  # noqa: ANN001
            r = MagicMock()
            r.returncode = 0
            if cmd and "jupyter" in cmd[0] and "kernelspec" in cmd:
                r.stdout = kernel_data
            else:
                r.stdout = ""
            r.stderr = ""
            return r

        with (
            patch("kbutillib.cli.init.shutil.which", return_value=None),
            patch("kbutillib.cli.init.subprocess.run", side_effect=_mock_subproc),
        ):
            result = _invoke("doctor")

        lines = result.output.strip().splitlines()
        kernel_line = next((l for l in lines if "jupyter-kernel" in l), None)
        assert kernel_line is not None
        assert kernel_line.startswith("[FAIL]"), f"jupyter-kernel line: {kernel_line!r}"


# ---------------------------------------------------------------------------
# kbu init --update
# ---------------------------------------------------------------------------


class TestInitUpdate:
    def test_update_fails_if_not_initialized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
        result = _invoke("init", "--update")
        assert result.exit_code == 1

    def test_update_runs_git_pull_and_pip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

        fake_python = tmp_path / "venv" / "bin" / "python"
        fake_python.parent.mkdir(parents=True)
        fake_python.touch()
        fake_python.chmod(0o755)
        _write_marker(
            kbutillib_repo_path=str(tmp_path / "kbutillib"),
            kbutillib_commit="a" * 40,
            venv_manager=".venv",
            venv_python=str(fake_python),
        )

        calls: list[list] = []

        def _mock_subproc(cmd, **kwargs):  # noqa: ANN001
            calls.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with patch("kbutillib.cli.init.subprocess.run", side_effect=_mock_subproc):
            result = _invoke("init", "--update")

        assert result.exit_code == 0
        # git pull should have been called
        assert any("git" in c[0] and "pull" in c for c in calls), \
            f"git pull not called; calls={calls}"
        # pip install --upgrade should have been called
        assert any("pip" in " ".join(c) and "--upgrade" in c for c in calls), \
            f"pip install --upgrade not called; calls={calls}"
