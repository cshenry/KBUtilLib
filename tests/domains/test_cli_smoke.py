"""Smoke tests for kbutillib.interfaces.cli — verifies CLI structure + basic API.

These tests are OFFLINE — no processes are spawned, no services called.
Uses click.testing.CliRunner for invoking --help on command groups.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# 1. CLI imports from canonical path (interfaces.cli)
# ---------------------------------------------------------------------------


def test_main_importable() -> None:
    """kbutillib.interfaces.cli.main is importable and is a Click group."""
    from kbutillib.interfaces.cli import main  # noqa: PLC0415
    assert main is not None
    assert callable(main)


def test_cap_cmd_importable() -> None:
    """kbutillib.interfaces.cli.cap_cmd is importable."""
    from kbutillib.interfaces.cli import cap_cmd  # noqa: PLC0415
    assert cap_cmd is not None


def test_jobs_cmd_importable() -> None:
    """kbutillib.interfaces.cli.jobs_cmd is importable."""
    from kbutillib.interfaces.cli import jobs_cmd  # noqa: PLC0415
    assert jobs_cmd is not None


def test_model_cmd_importable() -> None:
    """kbutillib.interfaces.cli.model_cmd is importable."""
    from kbutillib.interfaces.cli import model_cmd  # noqa: PLC0415
    assert model_cmd is not None


def test_notebook_cmd_importable() -> None:
    """kbutillib.interfaces.cli.notebook_cmd is importable."""
    from kbutillib.interfaces.cli import notebook_cmd  # noqa: PLC0415
    assert notebook_cmd is not None


def test_king_cmd_importable() -> None:
    """kbutillib.interfaces.cli.king_cmd is importable."""
    from kbutillib.interfaces.cli import king_cmd  # noqa: PLC0415
    assert king_cmd is not None


# ---------------------------------------------------------------------------
# 2. CLI shim path still works (kbutillib.cli)
# ---------------------------------------------------------------------------


def test_cli_shim_main_importable() -> None:
    """kbutillib.cli shim re-exports main."""
    from kbutillib.cli import main as shim_main  # noqa: PLC0415
    from kbutillib.interfaces.cli import main as canon_main  # noqa: PLC0415
    assert shim_main is canon_main, "cli shim main is not the same object as interfaces.cli.main"


# ---------------------------------------------------------------------------
# 3. Click runner: --help exits cleanly
# ---------------------------------------------------------------------------


def test_main_help_exits_ok() -> None:
    """kbu --help exits with code 0."""
    from click.testing import CliRunner  # noqa: PLC0415
    from kbutillib.interfaces.cli import main  # noqa: PLC0415
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0


def test_cap_cmd_help_exits_ok() -> None:
    """kbu cap --help exits with code 0."""
    from click.testing import CliRunner  # noqa: PLC0415
    from kbutillib.interfaces.cli import cap_cmd  # noqa: PLC0415
    runner = CliRunner()
    result = runner.invoke(cap_cmd, ["--help"])
    assert result.exit_code == 0


def test_main_help_contains_kbutillib() -> None:
    """kbu --help output mentions KBUtilLib."""
    from click.testing import CliRunner  # noqa: PLC0415
    from kbutillib.interfaces.cli import main  # noqa: PLC0415
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert "kbu" in result.output.lower() or "kbutillib" in result.output.lower()


def test_model_cmd_help_exits_ok() -> None:
    """kbu model --help exits with code 0."""
    from click.testing import CliRunner  # noqa: PLC0415
    from kbutillib.interfaces.cli import model_cmd  # noqa: PLC0415
    runner = CliRunner()
    result = runner.invoke(model_cmd, ["--help"])
    assert result.exit_code == 0


def test_jobs_cmd_help_exits_ok() -> None:
    """kbu jobs --help exits with code 0."""
    from click.testing import CliRunner  # noqa: PLC0415
    from kbutillib.interfaces.cli import jobs_cmd  # noqa: PLC0415
    runner = CliRunner()
    result = runner.invoke(jobs_cmd, ["--help"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# 4. Additional CLI command groups
# ---------------------------------------------------------------------------


def test_researchos_cmd_importable() -> None:
    """kbutillib.interfaces.cli.researchos_cmd is importable."""
    from kbutillib.interfaces.cli import researchos_cmd  # noqa: PLC0415
    assert researchos_cmd is not None


def test_session_cmd_importable() -> None:
    """kbutillib.interfaces.cli.session_cmd is importable."""
    from kbutillib.interfaces.cli import session_cmd  # noqa: PLC0415
    assert session_cmd is not None


def test_new_capability_cmd_importable() -> None:
    """kbutillib.interfaces.cli.new_capability_cmd is importable."""
    from kbutillib.interfaces.cli import new_capability_cmd  # noqa: PLC0415
    assert new_capability_cmd is not None


def test_king_cmd_help_exits_ok() -> None:
    """kbu king --help exits with code 0."""
    from click.testing import CliRunner  # noqa: PLC0415
    from kbutillib.interfaces.cli import king_cmd  # noqa: PLC0415
    runner = CliRunner()
    result = runner.invoke(king_cmd, ["--help"])
    assert result.exit_code == 0
