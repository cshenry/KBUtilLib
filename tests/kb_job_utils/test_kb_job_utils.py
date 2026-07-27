"""Tests for the kb_job_utils package: state, store, and utils modules."""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kbutillib.kb_job_utils.state import JobRecord, JobState
from kbutillib.kb_job_utils.store import JobStore
from kbutillib.kb_job_utils.utils import KBJobUtils


# ── JobState tests ───────────────────────────────────────────────────────────


class TestJobState:
    def test_values_match_ee2(self):
        assert JobState.CREATED.value == "created"
        assert JobState.QUEUED.value == "queued"
        assert JobState.RUNNING.value == "running"
        assert JobState.COMPLETED.value == "completed"
        assert JobState.ERROR.value == "error"
        assert JobState.TERMINATED.value == "terminated"

    def test_terminal_states(self):
        terminal = JobState.terminal_states()
        assert JobState.COMPLETED in terminal
        assert JobState.ERROR in terminal
        assert JobState.TERMINATED in terminal
        assert JobState.RUNNING not in terminal
        assert JobState.QUEUED not in terminal

    def test_is_terminal(self):
        assert JobState.COMPLETED.is_terminal
        assert JobState.ERROR.is_terminal
        assert not JobState.RUNNING.is_terminal
        assert not JobState.CREATED.is_terminal

    def test_from_string(self):
        assert JobState("running") == JobState.RUNNING
        assert JobState("error") == JobState.ERROR


# ── JobRecord tests ──────────────────────────────────────────────────────────


class TestJobRecord:
    def test_defaults(self):
        rec = JobRecord(job_id="abc123")
        assert rec.job_id == "abc123"
        assert rec.method == ""
        assert rec.state == JobState.CREATED
        assert rec.params == {}
        assert rec.workspace_id is None
        assert rec.error_message is None
        assert isinstance(rec.created_at, datetime)
        assert rec.created_at.tzinfo is not None

    def test_all_fields(self):
        now = datetime.now(timezone.utc)
        rec = JobRecord(
            job_id="j1",
            method="mod.meth",
            params={"x": 1},
            state=JobState.RUNNING,
            workspace_id=42,
            narrative_id=7,
            created_at=now,
            updated_at=now,
            ee2_raw={"status": "running"},
            error_message=None,
            meta={"tag": "test"},
        )
        assert rec.method == "mod.meth"
        assert rec.workspace_id == 42
        assert rec.meta["tag"] == "test"


# ── JobStore tests ───────────────────────────────────────────────────────────


@pytest.fixture
def job_store(tmp_path):
    """Create a JobStore backed by a temp directory."""
    store = JobStore(db_path=tmp_path / "test.db")
    yield store
    store.close()


class TestJobStore:
    def test_upsert_and_get(self, job_store):
        rec = JobRecord(job_id="j1", method="m.m", state=JobState.QUEUED)
        job_store.upsert(rec)
        loaded = job_store.get("j1")
        assert loaded is not None
        assert loaded.job_id == "j1"
        assert loaded.state == JobState.QUEUED
        assert loaded.method == "m.m"

    def test_get_missing_returns_none(self, job_store):
        assert job_store.get("nonexistent") is None

    def test_upsert_updates_existing(self, job_store):
        rec = JobRecord(job_id="j1", state=JobState.QUEUED)
        job_store.upsert(rec)
        rec.state = JobState.RUNNING
        job_store.upsert(rec)
        loaded = job_store.get("j1")
        assert loaded.state == JobState.RUNNING

    def test_list_by_state(self, job_store):
        job_store.upsert(JobRecord(job_id="j1", state=JobState.QUEUED))
        job_store.upsert(JobRecord(job_id="j2", state=JobState.RUNNING))
        job_store.upsert(JobRecord(job_id="j3", state=JobState.QUEUED))

        queued = job_store.list_by_state(JobState.QUEUED)
        assert len(queued) == 2
        assert {r.job_id for r in queued} == {"j1", "j3"}

    def test_list_active(self, job_store):
        job_store.upsert(JobRecord(job_id="j1", state=JobState.QUEUED))
        job_store.upsert(JobRecord(job_id="j2", state=JobState.COMPLETED))
        job_store.upsert(JobRecord(job_id="j3", state=JobState.RUNNING))
        job_store.upsert(JobRecord(job_id="j4", state=JobState.ERROR))

        active = job_store.list_active()
        active_ids = {r.job_id for r in active}
        assert active_ids == {"j1", "j3"}

    def test_list_all(self, job_store):
        job_store.upsert(JobRecord(job_id="j1", state=JobState.QUEUED))
        job_store.upsert(JobRecord(job_id="j2", state=JobState.COMPLETED))
        assert len(job_store.list_all()) == 2

    def test_delete(self, job_store):
        job_store.upsert(JobRecord(job_id="j1"))
        assert job_store.delete("j1")
        assert job_store.get("j1") is None

    def test_delete_missing_returns_false(self, job_store):
        assert not job_store.delete("nope")

    def test_params_roundtrip(self, job_store):
        rec = JobRecord(job_id="j1", params={"key": [1, 2, 3]})
        job_store.upsert(rec)
        loaded = job_store.get("j1")
        assert loaded.params == {"key": [1, 2, 3]}

    def test_meta_roundtrip(self, job_store):
        rec = JobRecord(job_id="j1", meta={"tag": "important", "count": 42})
        job_store.upsert(rec)
        loaded = job_store.get("j1")
        assert loaded.meta == {"tag": "important", "count": 42}

    def test_ee2_raw_roundtrip(self, job_store):
        raw = {"status": "running", "job_id": "j1", "some_field": True}
        rec = JobRecord(job_id="j1", ee2_raw=raw)
        job_store.upsert(rec)
        loaded = job_store.get("j1")
        assert loaded.ee2_raw == raw

    def test_db_file_created(self, tmp_path):
        db_path = tmp_path / "subdir" / "jobs.db"
        store = JobStore(db_path=db_path)
        store.upsert(JobRecord(job_id="j1"))
        store.close()
        assert db_path.exists()


# ── KBJobUtils tests ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_env():
    """Create a mock SharedEnvUtils."""
    env = MagicMock()
    env.get_token.return_value = "fake-token-123"
    return env


@pytest.fixture
def job_utils(mock_env, tmp_path):
    """Create a KBJobUtils instance with mocked EE2 client."""
    with patch(
        "kbutillib.kb_job_utils.utils.execution_engine2"
    ) as mock_ee2_cls:
        mock_ee2 = MagicMock()
        mock_ee2_cls.return_value = mock_ee2

        utils = KBJobUtils(
            env=mock_env, kb_version="ci", db_path=tmp_path / "test.db"
        )
        utils._mock_ee2 = mock_ee2  # stash for test assertions
        yield utils
        utils.close()


class TestKBJobUtils:
    def test_init_uses_token(self, mock_env, tmp_path):
        with patch(
            "kbutillib.kb_job_utils.utils.execution_engine2"
        ) as mock_ee2_cls:
            KBJobUtils(env=mock_env, kb_version="prod", db_path=tmp_path / "t.db")
            mock_env.get_token.assert_called_with(namespace="kbase")
            mock_ee2_cls.assert_called_once_with(
                url="https://kbase.us/services/ee2", token="fake-token-123"
            )

    def test_run_job(self, job_utils):
        job_utils._mock_ee2.run_job.return_value = "new-job-id-42"

        record = job_utils.run_job(
            method="mymod.mymethod",
            params=[{"genome_ref": "123/456"}],
            workspace_id=99,
            meta={"note": "test run"},
        )

        assert record.job_id == "new-job-id-42"
        assert record.method == "mymod.mymethod"
        assert record.state == JobState.QUEUED
        assert record.workspace_id == 99
        assert record.meta == {"note": "test run"}

        # Verify it's persisted
        stored = job_utils.store.get("new-job-id-42")
        assert stored is not None
        assert stored.state == JobState.QUEUED

    def test_run_job_with_service_ver(self, job_utils):
        job_utils._mock_ee2.run_job.return_value = "j1"
        record = job_utils.run_job(
            method="mod.meth",
            params=[{}],
            service_ver="beta",
            app_id="mod/meth",
        )
        call_args = job_utils._mock_ee2.run_job.call_args[0][0]
        assert call_args["service_ver"] == "beta"
        assert call_args["app_id"] == "mod/meth"

    def test_check_job(self, job_utils):
        # Seed a record first
        job_utils.store.upsert(
            JobRecord(job_id="j1", method="mod.meth", state=JobState.QUEUED)
        )
        job_utils._mock_ee2.check_job.return_value = {
            "job_id": "j1",
            "status": "running",
        }

        record = job_utils.check_job("j1")
        assert record.state == JobState.RUNNING
        assert record.ee2_raw["status"] == "running"

        # Verify stored update
        stored = job_utils.store.get("j1")
        assert stored.state == JobState.RUNNING

    def test_check_job_error_extracts_message(self, job_utils):
        job_utils._mock_ee2.check_job.return_value = {
            "job_id": "j1",
            "status": "error",
            "errormsg": "Something went wrong",
        }

        record = job_utils.check_job("j1")
        assert record.state == JobState.ERROR
        assert record.error_message == "Something went wrong"

    def test_check_job_creates_record_if_missing(self, job_utils):
        """check_job should work even if the job wasn't submitted via run_job."""
        job_utils._mock_ee2.check_job.return_value = {
            "job_id": "external-j1",
            "status": "completed",
        }

        record = job_utils.check_job("external-j1")
        assert record.state == JobState.COMPLETED
        assert job_utils.store.get("external-j1") is not None

    def test_check_jobs_batch(self, job_utils):
        job_utils._mock_ee2.check_jobs.return_value = {
            "job_states": {
                "j1": {"status": "completed"},
                "j2": {"status": "running"},
            }
        }

        results = job_utils.check_jobs(["j1", "j2"])
        assert results["j1"].state == JobState.COMPLETED
        assert results["j2"].state == JobState.RUNNING

    def test_cancel_job(self, job_utils):
        job_utils.store.upsert(
            JobRecord(job_id="j1", state=JobState.RUNNING)
        )

        record = job_utils.cancel_job("j1")
        assert record.state == JobState.TERMINATED
        job_utils._mock_ee2.cancel_job.assert_called_once_with({"job_id": "j1"})

        stored = job_utils.store.get("j1")
        assert stored.state == JobState.TERMINATED

    def test_get_job_logs(self, job_utils):
        job_utils._mock_ee2.get_job_logs.return_value = {
            "lines": [{"line": "hello", "is_error": 0}],
            "last_line_number": 1,
        }

        logs = job_utils.get_job_logs("j1", skip_lines=0)
        assert logs["lines"][0]["line"] == "hello"

    def test_list_active(self, job_utils):
        job_utils.store.upsert(JobRecord(job_id="j1", state=JobState.RUNNING))
        job_utils.store.upsert(JobRecord(job_id="j2", state=JobState.COMPLETED))
        active = job_utils.list_active()
        assert len(active) == 1
        assert active[0].job_id == "j1"

    def test_list_all(self, job_utils):
        job_utils.store.upsert(JobRecord(job_id="j1", state=JobState.RUNNING))
        job_utils.store.upsert(JobRecord(job_id="j2", state=JobState.COMPLETED))
        assert len(job_utils.list_all()) == 2

    def test_get_record_local_only(self, job_utils):
        job_utils.store.upsert(JobRecord(job_id="j1", method="x.y"))
        rec = job_utils.get_record("j1")
        assert rec.method == "x.y"
        # Should NOT have called EE2
        job_utils._mock_ee2.check_job.assert_not_called()

    def test_ee2_client_property(self, job_utils):
        assert job_utils.ee2_client is job_utils._mock_ee2


# ── EE2 client vendoring test ───────────────────────────────────────────────


class TestEE2Client:
    def test_import(self):
        from kbutillib.installed_clients.execution_engine2Client import (
            execution_engine2,
        )

        assert execution_engine2 is not None

    def test_requires_url(self):
        from kbutillib.installed_clients.execution_engine2Client import (
            execution_engine2,
        )

        with pytest.raises(ValueError, match="url is required"):
            execution_engine2()
