"""Tests for ``kbu jobdaemon`` CLI subcommand."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from kbutillib.cli import main

# Patch targets -- these are the modules where the names are looked up
_PATCH_ENV = "kbutillib.shared_env_utils.SharedEnvUtils"
_PATCH_KBU = "kbutillib.kb_job_utils.utils.KBJobUtils"


@pytest.fixture
def runner():
    return CliRunner()


class TestJobdaemonCmd:
    def test_jobdaemon_wires_start_and_stop(self, runner, tmp_path):
        """jobdaemon should call start_watcher and eventually stop_watcher."""
        store_path = str(tmp_path / "test.db")

        with patch(_PATCH_ENV) as mock_env_cls, \
             patch(_PATCH_KBU) as mock_kbu_cls:
            mock_env = MagicMock()
            mock_env_cls.return_value = mock_env

            mock_kbu = MagicMock()
            mock_kbu_cls.return_value = mock_kbu

            mock_watcher = MagicMock()
            mock_watcher.runs = 3
            mock_watcher.errors = 0
            mock_watcher.last_run_at = None
            mock_watcher.is_alive.side_effect = [True, False]
            mock_kbu.start_watcher.return_value = mock_watcher

            result = runner.invoke(
                main,
                ["jobdaemon", "--interval", "30", "--store-path", store_path,
                 "--kb-version", "ci", "--log-level", "WARNING"],
            )

            mock_kbu.start_watcher.assert_called_once_with(interval=30, daemon=False)
            mock_kbu.close.assert_called_once()

    def test_jobdaemon_default_options(self, runner, tmp_path):
        """jobdaemon with defaults should use interval=300 and prod."""
        store_path = str(tmp_path / "test.db")

        with patch(_PATCH_ENV) as mock_env_cls, \
             patch(_PATCH_KBU) as mock_kbu_cls:
            mock_env = MagicMock()
            mock_env_cls.return_value = mock_env

            mock_kbu = MagicMock()
            mock_kbu_cls.return_value = mock_kbu

            mock_watcher = MagicMock()
            mock_watcher.runs = 0
            mock_watcher.errors = 0
            mock_watcher.last_run_at = None
            mock_watcher.is_alive.side_effect = [False]
            mock_kbu.start_watcher.return_value = mock_watcher

            result = runner.invoke(
                main,
                ["jobdaemon", "--store-path", store_path],
            )

            mock_kbu.start_watcher.assert_called_once_with(interval=300, daemon=False)

    def test_jobdaemon_help(self, runner):
        result = runner.invoke(main, ["jobdaemon", "--help"])
        assert result.exit_code == 0
        assert "watcher" in result.output.lower() or "foreground" in result.output.lower()
