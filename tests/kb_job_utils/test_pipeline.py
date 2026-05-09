"""Unit tests for PipelineState, ChainStep, and PipelineStatus."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kbutillib.kb_job_utils.pipeline import (
    ChainStep,
    PipelineState,
    PipelineStatus,
)


class TestPipelineStatus:
    def test_values(self):
        assert PipelineStatus.PENDING.value == "pending"
        assert PipelineStatus.RUNNING.value == "running"
        assert PipelineStatus.COMPLETED.value == "completed"
        assert PipelineStatus.ERROR.value == "error"
        assert PipelineStatus.TERMINATED.value == "terminated"

    def test_is_terminal(self):
        assert PipelineStatus.COMPLETED.is_terminal()
        assert PipelineStatus.ERROR.is_terminal()
        assert PipelineStatus.TERMINATED.is_terminal()
        assert not PipelineStatus.PENDING.is_terminal()
        assert not PipelineStatus.RUNNING.is_terminal()

    def test_from_string(self):
        assert PipelineStatus("running") == PipelineStatus.RUNNING
        assert PipelineStatus("error") == PipelineStatus.ERROR


class TestChainStep:
    def test_defaults(self):
        step = ChainStep(params={"method": "mod.run", "params": [{}]})
        assert step.params == {"method": "mod.run", "params": [{}]}
        assert step.name is None
        assert step.app_id is None

    def test_all_fields(self):
        step = ChainStep(
            params={"method": "mod.run"},
            name="Step 1",
            app_id="mod/run",
        )
        assert step.name == "Step 1"
        assert step.app_id == "mod/run"

    def test_to_dict(self):
        step = ChainStep(params={"x": 1}, name="s1", app_id="a/b")
        d = step.to_dict()
        assert d == {"params": {"x": 1}, "name": "s1", "app_id": "a/b"}

    def test_to_dict_minimal(self):
        step = ChainStep(params={"x": 1})
        d = step.to_dict()
        assert d == {"params": {"x": 1}}
        assert "name" not in d
        assert "app_id" not in d

    def test_from_dict(self):
        d = {"params": {"x": 1}, "name": "s1", "app_id": "a/b"}
        step = ChainStep.from_dict(d)
        assert step.params == {"x": 1}
        assert step.name == "s1"
        assert step.app_id == "a/b"

    def test_roundtrip(self):
        original = ChainStep(params={"method": "m.m", "params": [{"ref": "1/2"}]},
                             name="Build", app_id="m/m")
        rebuilt = ChainStep.from_dict(original.to_dict())
        assert rebuilt.params == original.params
        assert rebuilt.name == original.name
        assert rebuilt.app_id == original.app_id


class TestPipelineState:
    def _make_pipeline(self, **overrides) -> PipelineState:
        defaults = dict(
            pipeline_id="abc123456789",
            spec=[ChainStep(params={"method": "m.m", "params": [{}]})],
            status=PipelineStatus.RUNNING,
            current_step=0,
            total_steps=1,
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        defaults.update(overrides)
        return PipelineState(**defaults)

    def test_defaults(self):
        p = self._make_pipeline()
        assert p.pipeline_id == "abc123456789"
        assert p.status == PipelineStatus.RUNNING
        assert p.last_advanced_at is None
        assert p.finished_at is None
        assert p.name is None
        assert p.project is None
        assert p.tags == []

    def test_is_terminal(self):
        assert not self._make_pipeline(status=PipelineStatus.RUNNING).is_terminal()
        assert self._make_pipeline(status=PipelineStatus.COMPLETED).is_terminal()
        assert self._make_pipeline(status=PipelineStatus.ERROR).is_terminal()
        assert self._make_pipeline(status=PipelineStatus.TERMINATED).is_terminal()

    def test_to_dict(self):
        p = self._make_pipeline(
            name="test",
            project="proj1",
            tags=["a", "b"],
            last_advanced_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
        )
        d = p.to_dict()
        assert d["pipeline_id"] == "abc123456789"
        assert d["status"] == "running"
        assert d["name"] == "test"
        assert d["project"] == "proj1"
        assert d["tags"] == ["a", "b"]
        assert d["last_advanced_at"] is not None

    def test_roundtrip(self):
        p = self._make_pipeline(
            spec=[
                ChainStep(params={"method": "a.a"}, name="step-0"),
                ChainStep(params={"method": "b.b"}, name="step-1"),
            ],
            total_steps=2,
            name="my pipeline",
            project="proj",
            tags=["tag1"],
            last_advanced_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
            finished_at=datetime(2025, 1, 3, tzinfo=timezone.utc),
        )
        rebuilt = PipelineState.from_dict(p.to_dict())
        assert rebuilt.pipeline_id == p.pipeline_id
        assert rebuilt.status == p.status
        assert rebuilt.current_step == p.current_step
        assert rebuilt.total_steps == p.total_steps
        assert rebuilt.name == p.name
        assert rebuilt.project == p.project
        assert rebuilt.tags == p.tags
        assert rebuilt.last_advanced_at == p.last_advanced_at
        assert rebuilt.finished_at == p.finished_at
        assert len(rebuilt.spec) == 2
        assert rebuilt.spec[0].name == "step-0"

    def test_from_dict_minimal(self):
        d = {
            "pipeline_id": "x",
            "spec": [{"params": {"method": "m.m"}}],
            "status": "pending",
            "current_step": 0,
            "total_steps": 1,
            "created_at": "2025-01-01T00:00:00+00:00",
        }
        p = PipelineState.from_dict(d)
        assert p.pipeline_id == "x"
        assert p.status == PipelineStatus.PENDING
        assert p.last_advanced_at is None
        assert p.finished_at is None
        assert p.tags == []
