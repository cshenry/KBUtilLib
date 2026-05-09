"""Tests for ``kbu jobs`` CLI subcommands."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from kbutillib.cli import main
from kbutillib.kb_job_utils.state import JobRecord, JobState
from kbutillib.kb_job_utils.store import JobStore

# Patch targets for lazy imports inside _get_kbu()
_PATCH_ENV = "kbutillib.shared_env_utils.SharedEnvUtils"
_PATCH_EE2 = "kbutillib.kb_job_utils.utils.execution_engine2"


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def store_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def store(store_path):
    s = JobStore(db_path=Path(store_path))
    yield s
    s.close()


def _seed_jobs(store: JobStore) -> None:
    """Seed a few test jobs into the store."""
    store.upsert(JobRecord(
        job_id="job-aaa", method="mod.run", state=JobState.RUNNING,
    ))
    store.upsert(JobRecord(
        job_id="job-bbb", method="mod.analyze", state=JobState.COMPLETED,
    ))
    store.upsert(JobRecord(
        job_id="job-ccc", method="mod.check", state=JobState.QUEUED,
    ))
    store.upsert(JobRecord(
        job_id="job-ddd", method="mod.fail", state=JobState.ERROR,
        error_message="Something went wrong",
    ))


class TestJobsStatus:
    def test_status_existing_job(self, runner, store_path, store):
        store.upsert(JobRecord(
            job_id="j1", method="mod.run", state=JobState.RUNNING,
            workspace_id=42,
        ))
        store.close()
        result = runner.invoke(main, ["jobs", "--store-path", store_path, "status", "j1"])
        assert result.exit_code == 0
        assert "j1" in result.output
        assert "mod.run" in result.output
        assert "running" in result.output
        assert "42" in result.output

    def test_status_missing_job(self, runner, store_path, store):
        store.close()
        result = runner.invoke(main, ["jobs", "--store-path", store_path, "status", "nope"])
        assert "not found" in result.output


class TestJobsList:
    def test_list_all(self, runner, store_path, store):
        _seed_jobs(store)
        store.close()
        result = runner.invoke(main, ["jobs", "--store-path", store_path, "list"])
        assert result.exit_code == 0
        assert "job-aaa" in result.output
        assert "job-bbb" in result.output

    def test_list_active_flag(self, runner, store_path, store):
        _seed_jobs(store)
        store.close()
        result = runner.invoke(main, ["jobs", "--store-path", store_path, "list", "--active"])
        assert result.exit_code == 0
        assert "job-aaa" in result.output   # running
        assert "job-ccc" in result.output   # queued
        assert "job-bbb" not in result.output  # completed

    def test_list_status_filter(self, runner, store_path, store):
        _seed_jobs(store)
        store.close()
        result = runner.invoke(
            main, ["jobs", "--store-path", store_path, "list", "--status", "error"]
        )
        assert result.exit_code == 0
        assert "job-ddd" in result.output
        assert "job-aaa" not in result.output

    def test_list_unknown_status(self, runner, store_path, store):
        store.close()
        result = runner.invoke(
            main, ["jobs", "--store-path", store_path, "list", "--status", "bogus"]
        )
        assert "Unknown state" in result.output

    def test_list_empty(self, runner, store_path, store):
        store.close()
        result = runner.invoke(main, ["jobs", "--store-path", store_path, "list"])
        assert result.exit_code == 0
        assert "No jobs found" in result.output

    def test_list_limit(self, runner, store_path, store):
        _seed_jobs(store)
        store.close()
        result = runner.invoke(
            main, ["jobs", "--store-path", store_path, "list", "--limit", "2"]
        )
        assert result.exit_code == 0


class TestJobsSummary:
    def test_summary(self, runner, store_path, store):
        _seed_jobs(store)
        store.close()
        result = runner.invoke(main, ["jobs", "--store-path", store_path, "summary"])
        assert result.exit_code == 0
        assert "total:" in result.output

    def test_summary_empty(self, runner, store_path, store):
        store.close()
        result = runner.invoke(main, ["jobs", "--store-path", store_path, "summary"])
        assert result.exit_code == 0
        assert "No jobs in store" in result.output


class TestJobsRefresh:
    def test_refresh_specific_jobs(self, runner, store_path, store):
        store.upsert(JobRecord(job_id="j1", method="m.m", state=JobState.RUNNING))
        store.close()
        with patch(_PATCH_ENV) as mock_env_cls, \
             patch(_PATCH_EE2) as mock_ee2_cls:
            mock_env = MagicMock()
            mock_env.get_token.return_value = "fake"
            mock_env_cls.return_value = mock_env
            mock_ee2 = MagicMock()
            mock_ee2_cls.return_value = mock_ee2
            mock_ee2.check_jobs.return_value = {
                "job_states": {"j1": {"status": "completed"}}
            }
            result = runner.invoke(
                main,
                ["jobs", "--store-path", store_path, "refresh", "j1"],
            )
            assert result.exit_code == 0
            assert "Refreshed 1 job(s)" in result.output

    def test_refresh_all_flag(self, runner, store_path, store):
        store.upsert(JobRecord(job_id="j1", state=JobState.RUNNING))
        store.upsert(JobRecord(job_id="j2", state=JobState.COMPLETED))
        store.close()
        with patch(_PATCH_ENV) as mock_env_cls, \
             patch(_PATCH_EE2) as mock_ee2_cls:
            mock_env = MagicMock()
            mock_env.get_token.return_value = "fake"
            mock_env_cls.return_value = mock_env
            mock_ee2 = MagicMock()
            mock_ee2_cls.return_value = mock_ee2
            mock_ee2.check_jobs.return_value = {
                "job_states": {
                    "j1": {"status": "completed"},
                    "j2": {"status": "completed"},
                }
            }
            result = runner.invoke(
                main,
                ["jobs", "--store-path", store_path, "refresh", "--all"],
            )
            assert result.exit_code == 0
            assert "all" in result.output


class TestJobsCancel:
    def test_cancel_with_force(self, runner, store_path, store):
        store.upsert(JobRecord(job_id="j1", state=JobState.RUNNING))
        store.close()
        with patch(_PATCH_ENV) as mock_env_cls, \
             patch(_PATCH_EE2) as mock_ee2_cls:
            mock_env = MagicMock()
            mock_env.get_token.return_value = "fake"
            mock_env_cls.return_value = mock_env
            mock_ee2 = MagicMock()
            mock_ee2_cls.return_value = mock_ee2
            result = runner.invoke(
                main,
                ["jobs", "--store-path", store_path, "cancel", "j1", "--force"],
            )
            assert result.exit_code == 0
            assert "terminated" in result.output
            mock_ee2.cancel_job.assert_called_once()

    def test_cancel_without_force_aborts(self, runner, store_path, store):
        store.close()
        with patch(_PATCH_ENV) as mock_env_cls, \
             patch(_PATCH_EE2) as mock_ee2_cls:
            mock_env = MagicMock()
            mock_env.get_token.return_value = "fake"
            mock_env_cls.return_value = mock_env
            mock_ee2 = MagicMock()
            mock_ee2_cls.return_value = mock_ee2
            result = runner.invoke(
                main,
                ["jobs", "--store-path", store_path, "cancel", "j1"],
                input="n\n",
            )
            assert "Aborted" in result.output
            mock_ee2.cancel_job.assert_not_called()


class TestJobsForget:
    def test_forget_with_force(self, runner, store_path, store):
        store.upsert(JobRecord(job_id="j1", state=JobState.COMPLETED))
        store.upsert(JobRecord(job_id="j2", state=JobState.ERROR))
        store.close()
        result = runner.invoke(
            main,
            ["jobs", "--store-path", store_path, "forget", "j1", "j2", "--force"],
        )
        assert result.exit_code == 0
        assert "Removed 2 record(s)" in result.output

    def test_forget_missing_job(self, runner, store_path, store):
        store.close()
        result = runner.invoke(
            main,
            ["jobs", "--store-path", store_path, "forget", "nope", "--force"],
        )
        assert result.exit_code == 0
        assert "Removed 0 record(s)" in result.output


class TestJobsCleanup:
    def test_cleanup_with_force(self, runner, store_path, store):
        old_time = datetime.now(timezone.utc) - timedelta(days=60)
        rec = JobRecord(job_id="j1", state=JobState.COMPLETED)
        store.upsert(rec)
        store._conn.execute(
            "UPDATE jobs SET updated_at = ? WHERE job_id = ?",
            (old_time.isoformat(), "j1"),
        )
        store._conn.commit()
        store.close()
        with patch(_PATCH_ENV) as mock_env_cls, \
             patch(_PATCH_EE2) as mock_ee2_cls:
            mock_env = MagicMock()
            mock_env.get_token.return_value = "fake"
            mock_env_cls.return_value = mock_env
            mock_ee2 = MagicMock()
            mock_ee2_cls.return_value = mock_ee2
            result = runner.invoke(
                main,
                ["jobs", "--store-path", store_path, "cleanup", "--force"],
            )
            assert result.exit_code == 0
            assert "Cleaned up 1 record(s)" in result.output

    def test_cleanup_without_force_aborts(self, runner, store_path, store):
        store.close()
        with patch(_PATCH_ENV) as mock_env_cls, \
             patch(_PATCH_EE2) as mock_ee2_cls:
            mock_env = MagicMock()
            mock_env.get_token.return_value = "fake"
            mock_env_cls.return_value = mock_env
            mock_ee2 = MagicMock()
            mock_ee2_cls.return_value = mock_ee2
            result = runner.invoke(
                main,
                ["jobs", "--store-path", store_path, "cleanup"],
                input="n\n",
            )
            assert "Aborted" in result.output
