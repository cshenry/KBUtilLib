"""Tests for pipeline advancement logic with mocked EE2 client."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from kbutillib.kb_job_utils.pipeline import (
    ChainStep,
    PipelineState,
    PipelineStatus,
)
from kbutillib.kb_job_utils.state import JobRecord, JobState
from kbutillib.kb_job_utils.utils import KBJobUtils


@pytest.fixture
def mock_env():
    env = MagicMock()
    env.get_token.return_value = "fake-token"
    return env


@pytest.fixture
def kbu(mock_env, tmp_path):
    """Create a KBJobUtils with mocked EE2 client."""
    with patch("kbutillib.kb_job_utils.utils.execution_engine2") as mock_ee2_cls:
        mock_ee2 = MagicMock()
        mock_ee2_cls.return_value = mock_ee2
        # Each run_job call returns a unique job ID
        mock_ee2.run_job.side_effect = lambda p: f"job-{p.get('method', 'x')}-{id(p) % 10000}"
        utils = KBJobUtils(env=mock_env, kb_version="ci", db_path=tmp_path / "test.db")
        utils._mock_ee2 = mock_ee2
        yield utils
        utils.close()


def _make_steps(n: int = 3) -> list:
    """Create n EE2 param dicts."""
    return [
        {"method": f"mod.step{i}", "params": [{"input": i}]}
        for i in range(n)
    ]


class TestSubmitChain:
    def test_submit_creates_pipeline(self, kbu):
        steps = _make_steps(3)
        pipeline = kbu.submit_chain(steps, name="test-pipe", project="proj")
        assert pipeline.status == PipelineStatus.RUNNING
        assert pipeline.current_step == 0
        assert pipeline.total_steps == 3
        assert pipeline.name == "test-pipe"
        assert pipeline.project == "proj"
        # First job should have been submitted
        kbu._mock_ee2.run_job.assert_called_once()

    def test_submit_persists_pipeline(self, kbu):
        pipeline = kbu.submit_chain(_make_steps(2))
        loaded = kbu.get_pipeline(pipeline.pipeline_id)
        assert loaded is not None
        assert loaded.pipeline_id == pipeline.pipeline_id

    def test_submit_chain_step_objects(self, kbu):
        steps = [
            ChainStep(params={"method": "a.a", "params": [{}]}, name="first"),
            ChainStep(params={"method": "b.b", "params": [{}]}, name="second"),
        ]
        pipeline = kbu.submit_chain(steps)
        assert pipeline.total_steps == 2
        assert pipeline.spec[0].name == "first"

    def test_submit_empty_raises(self, kbu):
        with pytest.raises(ValueError, match="at least one step"):
            kbu.submit_chain([])

    def test_submit_stores_job_with_pipeline_meta(self, kbu):
        pipeline = kbu.submit_chain(_make_steps(2))
        job = kbu._find_pipeline_step_job(pipeline.pipeline_id, 0)
        assert job is not None
        assert job.meta["pipeline_id"] == pipeline.pipeline_id
        assert job.meta["pipeline_step"] == 0


class TestAdvancePipelines:
    def _setup_3step_pipeline(self, kbu):
        """Submit a 3-step pipeline and return (pipeline, first_job)."""
        steps = _make_steps(3)
        pipeline = kbu.submit_chain(steps, name="3step")
        first_job = kbu._find_pipeline_step_job(pipeline.pipeline_id, 0)
        return pipeline, first_job

    def test_full_3step_chain(self, kbu):
        """3-step chain: advance through all steps to COMPLETED."""
        pipeline, job0 = self._setup_3step_pipeline(kbu)
        pid = pipeline.pipeline_id

        # Mark step 0 as completed
        job0.state = JobState.COMPLETED
        kbu.store.upsert(job0)

        # Advance → step 1 submitted
        changed = kbu.advance_pipelines()
        assert len(changed) == 1
        p = kbu.get_pipeline(pid)
        assert p.current_step == 1
        assert p.status == PipelineStatus.RUNNING

        # Mark step 1 as completed
        job1 = kbu._find_pipeline_step_job(pid, 1)
        assert job1 is not None
        job1.state = JobState.COMPLETED
        kbu.store.upsert(job1)

        # Advance → step 2 submitted
        changed = kbu.advance_pipelines()
        assert len(changed) == 1
        p = kbu.get_pipeline(pid)
        assert p.current_step == 2
        assert p.status == PipelineStatus.RUNNING

        # Mark step 2 as completed
        job2 = kbu._find_pipeline_step_job(pid, 2)
        assert job2 is not None
        job2.state = JobState.COMPLETED
        kbu.store.upsert(job2)

        # Advance → pipeline COMPLETED
        changed = kbu.advance_pipelines()
        assert len(changed) == 1
        p = kbu.get_pipeline(pid)
        assert p.status == PipelineStatus.COMPLETED
        assert p.finished_at is not None

    def test_step_failure_stops_pipeline(self, kbu):
        """Step 1 fails → pipeline goes ERROR, no further steps."""
        pipeline, job0 = self._setup_3step_pipeline(kbu)
        pid = pipeline.pipeline_id

        # Step 0 completes
        job0.state = JobState.COMPLETED
        kbu.store.upsert(job0)
        kbu.advance_pipelines()

        # Step 1 fails
        job1 = kbu._find_pipeline_step_job(pid, 1)
        job1.state = JobState.ERROR
        job1.error_message = "boom"
        kbu.store.upsert(job1)

        changed = kbu.advance_pipelines()
        assert len(changed) == 1
        p = kbu.get_pipeline(pid)
        assert p.status == PipelineStatus.ERROR
        assert p.finished_at is not None

        # No step 2 job should exist
        job2 = kbu._find_pipeline_step_job(pid, 2)
        assert job2 is None

    def test_step_cancellation_stops_pipeline(self, kbu):
        """Step 1 terminated → pipeline goes TERMINATED."""
        pipeline, job0 = self._setup_3step_pipeline(kbu)
        pid = pipeline.pipeline_id

        # Step 0 completes
        job0.state = JobState.COMPLETED
        kbu.store.upsert(job0)
        kbu.advance_pipelines()

        # Step 1 is terminated
        job1 = kbu._find_pipeline_step_job(pid, 1)
        job1.state = JobState.TERMINATED
        kbu.store.upsert(job1)

        changed = kbu.advance_pipelines()
        assert len(changed) == 1
        p = kbu.get_pipeline(pid)
        assert p.status == PipelineStatus.TERMINATED

    def test_idempotent_no_transition(self, kbu):
        """advance_pipelines() is a no-op when nothing has changed."""
        pipeline, job0 = self._setup_3step_pipeline(kbu)

        # Step 0 is still running → no advancement
        changed = kbu.advance_pipelines()
        assert changed == []

        # Call again → still no change
        changed = kbu.advance_pipelines()
        assert changed == []

    def test_idempotent_after_completion(self, kbu):
        """advance_pipelines() returns [] when pipeline is already terminal."""
        steps = [{"method": "mod.single", "params": [{}]}]
        pipeline = kbu.submit_chain(steps)
        pid = pipeline.pipeline_id

        job0 = kbu._find_pipeline_step_job(pid, 0)
        job0.state = JobState.COMPLETED
        kbu.store.upsert(job0)

        changed = kbu.advance_pipelines()
        assert len(changed) == 1

        # Again — pipeline is COMPLETED, no change
        changed = kbu.advance_pipelines()
        assert changed == []

    def test_multiple_pipelines_advanced(self, kbu):
        """Two pipelines can advance in the same call."""
        p1 = kbu.submit_chain(_make_steps(2), name="p1")
        p2 = kbu.submit_chain(_make_steps(2), name="p2")

        # Complete step 0 of both
        j1 = kbu._find_pipeline_step_job(p1.pipeline_id, 0)
        j1.state = JobState.COMPLETED
        kbu.store.upsert(j1)

        j2 = kbu._find_pipeline_step_job(p2.pipeline_id, 0)
        j2.state = JobState.COMPLETED
        kbu.store.upsert(j2)

        changed = kbu.advance_pipelines()
        assert len(changed) == 2


class TestCancelPipeline:
    def test_cancel_running_pipeline(self, kbu):
        pipeline = kbu.submit_chain(_make_steps(3))
        pid = pipeline.pipeline_id

        result = kbu.cancel_pipeline(pid)
        assert result.status == PipelineStatus.TERMINATED
        assert result.finished_at is not None

    def test_cancel_terminal_pipeline_noop(self, kbu):
        pipeline = kbu.submit_chain([{"method": "m.m", "params": [{}]}])
        pid = pipeline.pipeline_id

        # Complete it
        job0 = kbu._find_pipeline_step_job(pid, 0)
        job0.state = JobState.COMPLETED
        kbu.store.upsert(job0)
        kbu.advance_pipelines()

        # Now cancel — should be no-op
        p = kbu.get_pipeline(pid)
        assert p.status == PipelineStatus.COMPLETED
        result = kbu.cancel_pipeline(pid)
        assert result.status == PipelineStatus.COMPLETED  # unchanged

    def test_cancel_nonexistent_raises(self, kbu):
        with pytest.raises(KeyError):
            kbu.cancel_pipeline("nonexistent")


class TestRefreshActiveAdvances:
    def test_refresh_active_calls_advance(self, kbu):
        """refresh_active() should advance pipelines after refreshing jobs."""
        pipeline = kbu.submit_chain([{"method": "m.m", "params": [{}]}])
        pid = pipeline.pipeline_id

        # Mark the job as completed in the store
        job0 = kbu._find_pipeline_step_job(pid, 0)
        job0.state = JobState.COMPLETED
        kbu.store.upsert(job0)

        # Mock check_jobs to return empty (no active jobs to refresh)
        kbu._mock_ee2.check_jobs.return_value = {"job_states": {}}

        kbu.refresh_active()

        p = kbu.get_pipeline(pid)
        assert p.status == PipelineStatus.COMPLETED


class TestListPipelines:
    def test_list_all(self, kbu):
        kbu.submit_chain(_make_steps(2), name="p1")
        kbu.submit_chain(_make_steps(2), name="p2")
        result = kbu.list_pipelines()
        assert len(result) == 2

    def test_list_by_status(self, kbu):
        kbu.submit_chain(_make_steps(1), name="p1")
        result = kbu.list_pipelines(status=PipelineStatus.RUNNING)
        assert len(result) == 1
        result = kbu.list_pipelines(status=PipelineStatus.COMPLETED)
        assert len(result) == 0

    def test_get_pipeline(self, kbu):
        pipeline = kbu.submit_chain(_make_steps(2))
        loaded = kbu.get_pipeline(pipeline.pipeline_id)
        assert loaded is not None
        assert loaded.pipeline_id == pipeline.pipeline_id
