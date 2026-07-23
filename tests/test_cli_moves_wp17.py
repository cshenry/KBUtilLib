"""WP17 — CLI → interfaces/cli/ + shim + __main__ retarget.

Tests that:
- New import paths (kbutillib.interfaces.cli) work correctly
- Old import paths (kbutillib.cli shim) still work
- All expected commands are registered under `main`
- kbu --help / python -m kbutillib --help work end-to-end
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# 1. New location imports
# ---------------------------------------------------------------------------


def test_new_main_importable() -> None:
    """kbutillib.interfaces.cli.main is importable from new location."""
    from kbutillib.interfaces.cli import main  # noqa: PLC0415

    assert callable(main)


def test_new_location_has_cap_cmd() -> None:
    """WP6 cap_cmd still present in interfaces.cli."""
    from kbutillib.interfaces.cli import cap_cmd  # noqa: PLC0415

    assert cap_cmd is not None


def test_new_location_has_new_capability_cmd() -> None:
    """WP6 new_capability_cmd still present in interfaces.cli."""
    from kbutillib.interfaces.cli import new_capability_cmd  # noqa: PLC0415

    assert new_capability_cmd is not None


def test_new_location_has_bootstrap_command() -> None:
    """bootstrap_command importable from interfaces.cli."""
    from kbutillib.interfaces.cli import bootstrap_command  # noqa: PLC0415

    assert callable(bootstrap_command)


def test_new_location_has_doctor_command() -> None:
    """doctor_command importable from interfaces.cli."""
    from kbutillib.interfaces.cli import doctor_command  # noqa: PLC0415

    assert callable(doctor_command)


def test_new_location_has_king_cmd() -> None:
    """king_cmd importable from interfaces.cli."""
    from kbutillib.interfaces.cli import king_cmd  # noqa: PLC0415

    assert callable(king_cmd)


# ---------------------------------------------------------------------------
# 2. Old shim imports (backward compatibility)
# ---------------------------------------------------------------------------


def test_shim_main_importable() -> None:
    """kbutillib.cli.main still importable via shim."""
    from kbutillib.cli import main  # noqa: PLC0415

    assert callable(main)


def test_shim_main_is_same_object() -> None:
    """Shim main and new location main are the same Click group."""
    from kbutillib.cli import main as shim_main  # noqa: PLC0415
    from kbutillib.interfaces.cli import main as new_main  # noqa: PLC0415

    assert shim_main is new_main


def test_shim_bootstrap_command() -> None:
    """bootstrap_command importable from shim kbutillib.cli."""
    from kbutillib.cli import bootstrap_command  # noqa: PLC0415

    assert callable(bootstrap_command)


def test_shim_doctor_command() -> None:
    """doctor_command importable from shim kbutillib.cli."""
    from kbutillib.cli import doctor_command  # noqa: PLC0415

    assert callable(doctor_command)


# ---------------------------------------------------------------------------
# 3. CLI runner — commands registered correctly
# ---------------------------------------------------------------------------

EXPECTED_COMMANDS = [
    "beril",
    "bootstrap",
    "buildplan",
    "cap",
    "doctor",
    "harness",
    "init",
    "init-notebook",
    "jobdaemon",
    "jobs",
    "king",
    "migrate",
    "model",
    "new-capability",
    "new-project",
    "notebook",
    "notebook-init",
    "researchos",
    "session",
    "set",
    "subproject",
    "update",
]


def test_main_has_all_commands() -> None:
    """All expected commands are registered on main."""
    from kbutillib.interfaces.cli import main  # noqa: PLC0415

    registered = list(main.commands.keys())
    for cmd in EXPECTED_COMMANDS:
        assert cmd in registered, f"Missing command: {cmd}"


def test_kbu_help_via_runner() -> None:
    """CliRunner invoking main --help exits 0 and shows key commands."""
    from kbutillib.interfaces.cli import main  # noqa: PLC0415

    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "bootstrap" in result.output
    assert "cap" in result.output
    assert "new-capability" in result.output
    assert "doctor" in result.output
    assert "king" in result.output


def test_kbu_help_via_runner_shim() -> None:
    """CliRunner invoking shim main --help also works."""
    from kbutillib.cli import main  # noqa: PLC0415

    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "kbu" in result.output.lower() or "kbutillib" in result.output.lower()


# ---------------------------------------------------------------------------
# 4. subprocess / __main__ entrypoint
# ---------------------------------------------------------------------------


def test_python_m_kbutillib_help() -> None:
    """``python -m kbutillib --help`` exits 0 and lists commands."""
    result = subprocess.run(
        [sys.executable, "-m", "kbutillib", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "bootstrap" in result.stdout
    assert "cap" in result.stdout
    assert "new-capability" in result.stdout


def test_no_overwrite_capabilities_py() -> None:
    """WP6 capabilities.py was NOT overwritten — it must still export cap_cmd."""
    from kbutillib.interfaces.cli.capabilities import cap_cmd  # noqa: PLC0415

    assert cap_cmd is not None


def test_no_overwrite_scaffold_py() -> None:
    """WP6 scaffold.py was NOT overwritten — it must still export new_capability_cmd."""
    from kbutillib.interfaces.cli.scaffold import new_capability_cmd  # noqa: PLC0415

    assert new_capability_cmd is not None
