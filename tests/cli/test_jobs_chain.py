"""Tests for ``kbu jobs chain`` CLI subcommands."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from kbutillib.cli import main
from kbutillib.kb_job_utils.pipeline import (
    ChainStep,
    PipelineState,
    PipelineStatus,
)
from kbutillib.kb_job_utils.state import JobRecord, JobState
from kbutillib.kb_job_utils.store import JobStore

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


def _mock_context():
    """Return mock env and ee2 instances."""
    mock_env = MagicMock()
    mock_env.get_token.return_value = "fake"
    mock_ee2 = MagicMock()
    mock_ee2.run_job.side_effect = lambda p: f"job-{id(p) % 100000}"
    return mock_env, mock_ee2


def _seed_pipeline(store: JobStore, **overrides) -> PipelineState:
    """Seed a pipeline + its step jobs into the store."""
    defaults = dict(
        pipeline_id="pipe001",
        spec=[
            ChainStep(params={"method": "a.a", "params": [{}]}, name="step-0"),
            ChainStep(params={"method": "b.b", "params": [{}]}, name="step-1"),
        ],
        status=PipelineStatus.RUNNING,
        current_step=0,
        total_steps=2,
        created_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    p = PipelineState(**defaults)
    store.upsert_pipeline(p)

    # Also seed the job for step 0
    job = JobRecord(
        job_id="job-step0",
        method="a.a",
        state=JobState.RUNNING,
        meta={"pipeline_id": p.pipeline_id, "pipeline_step": 0},
    )
    store.upsert(job)
    return p


class TestChainSubmit:
    def test_submit_from_file(self, runner, store_path, tmp_path):
        steps_file = tmp_path / "steps.json"
        steps_file.write_text(json.dumps([
            {"method": "mod.step0", "params": [{}]},
            {"method": "mod.step1", "params": [{}]},
        ]))

        with patch(_PATCH_ENV) as mock_env_cls, \
             patch(_PATCH_EE2) as mock_ee2_cls:
            mock_env, mock_ee2 = _mock_context()
            mock_env_cls.return_value = mock_env
            mock_ee2_cls.return_value = mock_ee2

            result = runner.invoke(
                main,
                ["jobs", "--store-path", store_path, "chain", "submit", str(steps_file)],
            )
            assert result.exit_code == 0, result.output
            assert "Pipeline:" in result.output
            assert "Steps:    2" in result.output

    def test_submit_from_stdin(self, runner, store_path):
        data = json.dumps({
            "steps": [{"method": "mod.run", "params": [{}]}],
            "name": "stdin-pipe",
            "project": "proj",
        })

        with patch(_PATCH_ENV) as mock_env_cls, \
             patch(_PATCH_EE2) as mock_ee2_cls:
            mock_env, mock_ee2 = _mock_context()
            mock_env_cls.return_value = mock_env
            mock_ee2_cls.return_value = mock_ee2

            result = runner.invoke(
                main,
                ["jobs", "--store-path", store_path, "chain", "submit", "-"],
                input=data,
            )
            assert result.exit_code == 0, result.output
            assert "Pipeline:" in result.output
            assert "Steps:    1" in result.output

    def test_submit_invalid_json(self, runner, store_path, tmp_path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not json")

        result = runner.invoke(
            main,
            ["jobs", "--store-path", store_path, "chain", "submit", str(bad_file)],
        )
        assert "Invalid JSON" in result.output

    def test_submit_empty_steps(self, runner, store_path, tmp_path):
        empty_file = tmp_path / "empty.json"
        empty_file.write_text("[]")

        result = runner.invoke(
            main,
            ["jobs", "--store-path", store_path, "chain", "submit", str(empty_file)],
        )
        assert "No steps" in result.output


class TestChainList:
    def test_list_all(self, runner, store_path, store):
        _seed_pipeline(store, pipeline_id="p1")
        _seed_pipeline(store, pipeline_id="p2", status=PipelineStatus.COMPLETED)
        store.close()

        result = runner.invoke(
            main,
            ["jobs", "--store-path", store_path, "chain", "list"],
        )
        assert result.exit_code == 0
        assert "p1" in result.output
        assert "p2" in result.output

    def test_list_active(self, runner, store_path, store):
        _seed_pipeline(store, pipeline_id="p1", status=PipelineStatus.RUNNING)
        _seed_pipeline(store, pipeline_id="p2", status=PipelineStatus.COMPLETED)
        store.close()

        result = runner.invoke(
            main,
            ["jobs", "--store-path", store_path, "chain", "list", "--active"],
        )
        assert result.exit_code == 0
        assert "p1" in result.output
        assert "p2" not in result.output

    def test_list_by_status(self, runner, store_path, store):
        _seed_pipeline(store, pipeline_id="p1", status=PipelineStatus.RUNNING)
        _seed_pipeline(store, pipeline_id="p2", status=PipelineStatus.ERROR)
        store.close()

        result = runner.invoke(
            main,
            ["jobs", "--store-path", store_path, "chain", "list", "--status", "error"],
        )
        assert result.exit_code == 0
        assert "p2" in result.output
        assert "p1" not in result.output

    def test_list_empty(self, runner, store_path, store):
        store.close()
        result = runner.invoke(
            main,
            ["jobs", "--store-path", store_path, "chain", "list"],
        )
        assert result.exit_code == 0
        assert "No pipelines found" in result.output

    def test_list_unknown_status(self, runner, store_path, store):
        store.close()
        result = runner.invoke(
            main,
            ["jobs", "--store-path", store_path, "chain", "list", "--status", "bogus"],
        )
        assert "Unknown pipeline status" in result.output


class TestChainStatus:
    def test_status_existing(self, runner, store_path, store):
        _seed_pipeline(store, pipeline_id="p1", name="My Pipeline", project="proj")
        store.close()

        result = runner.invoke(
            main,
            ["jobs", "--store-path", store_path, "chain", "status", "p1"],
        )
        assert result.exit_code == 0
        assert "p1" in result.output
        assert "My Pipeline" in result.output
        assert "proj" in result.output
        assert "step-0" in result.output

    def test_status_missing(self, runner, store_path, store):
        store.close()
        result = runner.invoke(
            main,
            ["jobs", "--store-path", store_path, "chain", "status", "nope"],
        )
        assert "not found" in result.output


class TestChainCancel:
    def test_cancel_with_force(self, runner, store_path, store):
        _seed_pipeline(store, pipeline_id="p1")
        store.close()

        with patch(_PATCH_ENV) as mock_env_cls, \
             patch(_PATCH_EE2) as mock_ee2_cls:
            mock_env, mock_ee2 = _mock_context()
            mock_env_cls.return_value = mock_env
            mock_ee2_cls.return_value = mock_ee2

            result = runner.invoke(
                main,
                ["jobs", "--store-path", store_path, "chain", "cancel", "p1", "--force"],
            )
            assert result.exit_code == 0
            assert "terminated" in result.output

    def test_cancel_without_force_aborts(self, runner, store_path, store):
        _seed_pipeline(store, pipeline_id="p1")
        store.close()

        with patch(_PATCH_ENV) as mock_env_cls, \
             patch(_PATCH_EE2) as mock_ee2_cls:
            mock_env, mock_ee2 = _mock_context()
            mock_env_cls.return_value = mock_env
            mock_ee2_cls.return_value = mock_ee2

            result = runner.invoke(
                main,
                ["jobs", "--store-path", store_path, "chain", "cancel", "p1"],
                input="n\n",
            )
            assert "Aborted" in result.output

    def test_cancel_nonexistent(self, runner, store_path, store):
        store.close()

        with patch(_PATCH_ENV) as mock_env_cls, \
             patch(_PATCH_EE2) as mock_ee2_cls:
            mock_env, mock_ee2 = _mock_context()
            mock_env_cls.return_value = mock_env
            mock_ee2_cls.return_value = mock_ee2

            result = runner.invoke(
                main,
                ["jobs", "--store-path", store_path, "chain", "cancel", "nope", "--force"],
            )
            assert "not found" in result.output


class TestChainAdvance:
    def test_advance_no_pipelines(self, runner, store_path, store):
        store.close()

        with patch(_PATCH_ENV) as mock_env_cls, \
             patch(_PATCH_EE2) as mock_ee2_cls:
            mock_env, mock_ee2 = _mock_context()
            mock_env_cls.return_value = mock_env
            mock_ee2_cls.return_value = mock_ee2

            result = runner.invoke(
                main,
                ["jobs", "--store-path", store_path, "chain", "advance"],
            )
            assert result.exit_code == 0
            assert "No pipelines advanced" in result.output

    def test_advance_with_ready_pipeline(self, runner, store_path, store):
        _seed_pipeline(store, pipeline_id="p1")
        # Mark step 0 as completed
        job = store.get("job-step0")
        job.state = JobState.COMPLETED
        store.upsert(job)
        store.close()

        with patch(_PATCH_ENV) as mock_env_cls, \
             patch(_PATCH_EE2) as mock_ee2_cls:
            mock_env, mock_ee2 = _mock_context()
            mock_env_cls.return_value = mock_env
            mock_ee2_cls.return_value = mock_ee2

            result = runner.invoke(
                main,
                ["jobs", "--store-path", store_path, "chain", "advance"],
            )
            assert result.exit_code == 0
            assert "p1" in result.output
