"""Tests for JobStore pipeline CRUD and schema migration v1→v2."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kbutillib.kb_job_utils.pipeline import (
    ChainStep,
    PipelineState,
    PipelineStatus,
)
from kbutillib.kb_job_utils.state import JobRecord, JobState
from kbutillib.kb_job_utils.store import JobStore


@pytest.fixture
def store(tmp_path):
    """Create a fresh JobStore (schema v2)."""
    s = JobStore(db_path=tmp_path / "test.db")
    yield s
    s.close()


def _make_pipeline(**overrides) -> PipelineState:
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
    return PipelineState(**defaults)


class TestPipelineCRUD:
    def test_upsert_and_get(self, store):
        p = _make_pipeline()
        store.upsert_pipeline(p)
        loaded = store.get_pipeline("pipe001")
        assert loaded is not None
        assert loaded.pipeline_id == "pipe001"
        assert loaded.status == PipelineStatus.RUNNING
        assert loaded.total_steps == 2
        assert len(loaded.spec) == 2
        assert loaded.spec[0].name == "step-0"

    def test_get_missing(self, store):
        assert store.get_pipeline("nonexistent") is None

    def test_upsert_updates_existing(self, store):
        p = _make_pipeline()
        store.upsert_pipeline(p)
        p.status = PipelineStatus.COMPLETED
        p.current_step = 1
        p.finished_at = datetime(2025, 6, 2, tzinfo=timezone.utc)
        store.upsert_pipeline(p)
        loaded = store.get_pipeline("pipe001")
        assert loaded.status == PipelineStatus.COMPLETED
        assert loaded.current_step == 1
        assert loaded.finished_at is not None

    def test_delete(self, store):
        store.upsert_pipeline(_make_pipeline())
        assert store.delete_pipeline("pipe001")
        assert store.get_pipeline("pipe001") is None

    def test_delete_missing(self, store):
        assert not store.delete_pipeline("nope")

    def test_list_all(self, store):
        store.upsert_pipeline(_make_pipeline(pipeline_id="p1"))
        store.upsert_pipeline(_make_pipeline(pipeline_id="p2"))
        result = store.list_pipelines()
        assert len(result) == 2

    def test_list_by_status(self, store):
        store.upsert_pipeline(_make_pipeline(pipeline_id="p1", status=PipelineStatus.RUNNING))
        store.upsert_pipeline(_make_pipeline(pipeline_id="p2", status=PipelineStatus.COMPLETED))
        running = store.list_pipelines(status=PipelineStatus.RUNNING)
        assert len(running) == 1
        assert running[0].pipeline_id == "p1"

    def test_list_by_project(self, store):
        store.upsert_pipeline(_make_pipeline(pipeline_id="p1", project="alpha"))
        store.upsert_pipeline(_make_pipeline(pipeline_id="p2", project="beta"))
        result = store.list_pipelines(project="alpha")
        assert len(result) == 1
        assert result[0].pipeline_id == "p1"

    def test_list_since(self, store):
        store.upsert_pipeline(_make_pipeline(
            pipeline_id="p1",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        ))
        store.upsert_pipeline(_make_pipeline(
            pipeline_id="p2",
            created_at=datetime(2025, 6, 15, tzinfo=timezone.utc),
        ))
        cutoff = datetime(2025, 6, 1, tzinfo=timezone.utc)
        result = store.list_pipelines(since=cutoff)
        assert len(result) == 1
        assert result[0].pipeline_id == "p2"

    def test_list_limit(self, store):
        for i in range(5):
            store.upsert_pipeline(_make_pipeline(pipeline_id=f"p{i}"))
        result = store.list_pipelines(limit=2)
        assert len(result) == 2

    def test_list_order_desc(self, store):
        store.upsert_pipeline(_make_pipeline(
            pipeline_id="p_old",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        ))
        store.upsert_pipeline(_make_pipeline(
            pipeline_id="p_new",
            created_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        ))
        result = store.list_pipelines()
        assert result[0].pipeline_id == "p_new"

    def test_tags_roundtrip(self, store):
        p = _make_pipeline(tags=["genomics", "batch"])
        store.upsert_pipeline(p)
        loaded = store.get_pipeline("pipe001")
        assert loaded.tags == ["genomics", "batch"]

    def test_name_and_project_roundtrip(self, store):
        p = _make_pipeline(name="My Pipeline", project="proj-x")
        store.upsert_pipeline(p)
        loaded = store.get_pipeline("pipe001")
        assert loaded.name == "My Pipeline"
        assert loaded.project == "proj-x"


class TestSchemaMigration:
    def test_fresh_db_is_v2(self, tmp_path):
        """A fresh database should be created at schema version 2."""
        store = JobStore(db_path=tmp_path / "fresh.db")
        cur = store._conn.execute("PRAGMA user_version;")
        assert cur.fetchone()[0] == 2
        # Pipelines table should exist
        cur2 = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pipelines';"
        )
        assert cur2.fetchone() is not None
        store.close()

    def test_v1_migration_to_v2(self, tmp_path):
        """A v1 database (jobs table, no user_version) migrates to v2."""
        db_path = tmp_path / "v1.db"
        # Create a v1-style database manually
        conn = sqlite3.connect(str(db_path))
        conn.execute("""\
            CREATE TABLE jobs (
                job_id       TEXT PRIMARY KEY,
                method       TEXT NOT NULL DEFAULT '',
                params       TEXT NOT NULL DEFAULT '{}',
                state        TEXT NOT NULL DEFAULT 'created',
                workspace_id INTEGER,
                narrative_id INTEGER,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                ee2_raw      TEXT NOT NULL DEFAULT '{}',
                error_message TEXT,
                meta         TEXT NOT NULL DEFAULT '{}'
            );
        """)
        conn.execute("CREATE INDEX idx_jobs_state ON jobs (state);")
        # Insert a job record
        conn.execute(
            "INSERT INTO jobs (job_id, method, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("j1", "mod.run", "running",
             "2025-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"),
        )
        conn.commit()
        conn.close()

        # Now open with JobStore — should migrate
        store = JobStore(db_path=db_path)

        # Schema version should be 2
        cur = store._conn.execute("PRAGMA user_version;")
        assert cur.fetchone()[0] == 2

        # Jobs table should still work
        job = store.get("j1")
        assert job is not None
        assert job.method == "mod.run"
        assert job.state == JobState.RUNNING

        # Pipelines table should exist and be usable
        p = _make_pipeline()
        store.upsert_pipeline(p)
        loaded = store.get_pipeline("pipe001")
        assert loaded is not None

        store.close()

    def test_v2_db_is_noop(self, tmp_path):
        """Opening a v2 database should not re-run migration."""
        db_path = tmp_path / "v2.db"
        store1 = JobStore(db_path=db_path)
        store1.upsert_pipeline(_make_pipeline())
        store1.close()

        store2 = JobStore(db_path=db_path)
        loaded = store2.get_pipeline("pipe001")
        assert loaded is not None
        store2.close()

    def test_future_version_raises(self, tmp_path):
        """Opening a database with a future schema version raises RuntimeError."""
        db_path = tmp_path / "future.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 99;")
        conn.commit()
        conn.close()

        with pytest.raises(RuntimeError, match="newer than"):
            JobStore(db_path=db_path)
