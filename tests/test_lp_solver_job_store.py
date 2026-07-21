"""Tests for kbutillib.services.lp_solver.job_store.LPJobStore.

Solver-independent: exercises the SQLite/temp-file state machine only,
never gurobipy/cplex, so this suite runs in any environment (S14 in
agent-io/prds/remote-lp-solver/fullprompt.md). Always uses a temp DB
path/tmp dir -- never the real ``~/.lp-solver``.
"""

import time

import pytest

from kbutillib.services.lp_solver.job_store import (
    ORPHAN_ERROR_MESSAGE,
    LPJobStore,
)

SAMPLE_LP = "Maximize\n obj: x\nSubject To\n c1: x <= 1\nEnd\n"


@pytest.fixture
def store(tmp_path):
    """An LPJobStore rooted entirely under a pytest temp directory."""
    s = LPJobStore(db_path=tmp_path / "jobs.sqlite", tmp_dir=tmp_path / "tmp")
    yield s
    s.close()


class TestCreate:
    def test_create_returns_uuid4_string(self, store):
        job_id = store.create(SAMPLE_LP)
        assert isinstance(job_id, str)
        # UUID4 string form: 8-4-4-4-12 hex digits, version nibble '4'.
        parts = job_id.split("-")
        assert [len(p) for p in parts] == [8, 4, 4, 4, 12]
        assert parts[2][0] == "4"

    def test_create_writes_lp_temp_file(self, store, tmp_path):
        job_id = store.create(SAMPLE_LP)
        lp_path = tmp_path / "tmp" / f"{job_id}.lp"
        assert lp_path.exists()
        assert lp_path.read_text() == SAMPLE_LP

    def test_create_inserts_queued_row(self, store):
        job_id = store.create(SAMPLE_LP, solver="gurobi")
        row = store.get(job_id)
        assert row["status"] == "queued"
        assert row["solver"] == "gurobi"
        assert row["start_ts"] is None
        assert row["end_ts"] is None
        assert row["result"] is None
        assert row["error"] is None


class TestStateMachine:
    def test_create_claim_running_done(self, store, tmp_path):
        job_id = store.create(SAMPLE_LP, solver="gurobi")

        claimed = store.claim_next()
        assert claimed["job_id"] == job_id
        assert claimed["solver"] == "gurobi"
        assert claimed["lp_path"] == str(tmp_path / "tmp" / f"{job_id}.lp")

        row = store.get(job_id)
        assert row["status"] == "running"
        assert row["start_ts"] is not None

        result = {"status": "optimal", "objective_value": 1.0, "variables": {"x": 1.0}}
        store.mark_done(job_id, result)

        row = store.get(job_id)
        assert row["status"] == "done"
        assert row["end_ts"] is not None
        assert row["result"] == result
        assert row["error"] is None

    def test_create_claim_running_error(self, store):
        job_id = store.create(SAMPLE_LP)
        store.claim_next()

        store.mark_error(job_id, "solver crashed")

        row = store.get(job_id)
        assert row["status"] == "error"
        assert row["error"] == "solver crashed"
        assert row["end_ts"] is not None

    def test_claim_next_returns_none_when_empty(self, store):
        assert store.claim_next() is None

    def test_claim_next_claims_oldest_first(self, store):
        first = store.create(SAMPLE_LP)
        time.sleep(0.01)
        second = store.create(SAMPLE_LP)

        claimed = store.claim_next()
        assert claimed["job_id"] == first

        claimed2 = store.claim_next()
        assert claimed2["job_id"] == second

    def test_claim_next_skips_non_queued_jobs(self, store):
        job_id = store.create(SAMPLE_LP)
        store.claim_next()  # -> running
        assert store.claim_next() is None

    def test_mark_running_sets_start_ts(self, store):
        job_id = store.create(SAMPLE_LP)
        store.mark_running(job_id)
        row = store.get(job_id)
        assert row["status"] == "running"
        assert row["start_ts"] is not None

    def test_mark_running_is_idempotent_on_start_ts(self, store):
        job_id = store.create(SAMPLE_LP)
        store.mark_running(job_id)
        first_start = store.get(job_id)["start_ts"]
        store.mark_running(job_id)
        assert store.get(job_id)["start_ts"] == first_start

    def test_get_missing_job_returns_none(self, store):
        assert store.get("does-not-exist") is None

    def test_mark_done_removes_lp_temp_file(self, store, tmp_path):
        job_id = store.create(SAMPLE_LP)
        lp_path = tmp_path / "tmp" / f"{job_id}.lp"
        assert lp_path.exists()

        store.mark_done(job_id, {"status": "optimal"})
        assert not lp_path.exists()

    def test_mark_error_removes_lp_temp_file(self, store, tmp_path):
        job_id = store.create(SAMPLE_LP)
        lp_path = tmp_path / "tmp" / f"{job_id}.lp"
        assert lp_path.exists()

        store.mark_error(job_id, "boom")
        assert not lp_path.exists()


class TestSweepExpired:
    def test_sweep_expired_removes_old_terminal_jobs(self, store):
        job_id = store.create(SAMPLE_LP)
        store.mark_done(job_id, {"status": "optimal"})

        # Backdate end_ts well past the 48h TTL directly via the raw
        # connection (there is no public setter for this -- simulating
        # the passage of time is the point of the test).
        store._conn.execute(
            "UPDATE jobs SET end_ts = ? WHERE job_id = ?",
            (time.time() - 49 * 3600, job_id),
        )

        removed = store.sweep_expired()
        assert removed == [job_id]
        assert store.get(job_id) is None

    def test_sweep_expired_honors_ttl_boundary(self, store):
        job_id = store.create(SAMPLE_LP)
        store.mark_done(job_id, {"status": "optimal"})

        # Well within the 48h TTL -- must NOT be swept.
        store._conn.execute(
            "UPDATE jobs SET end_ts = ? WHERE job_id = ?",
            (time.time() - 3600, job_id),
        )

        removed = store.sweep_expired()
        assert removed == []
        assert store.get(job_id) is not None

    def test_sweep_expired_ignores_queued_and_running_jobs(self, store):
        queued_job = store.create(SAMPLE_LP)
        running_job = store.create(SAMPLE_LP)
        store.claim_next()  # claims queued_job (oldest) -> running

        # Force both jobs' timestamps far in the past; neither is
        # terminal, so sweep must leave them alone regardless of age.
        store._conn.execute(
            "UPDATE jobs SET submit_ts = ? WHERE job_id IN (?, ?)",
            (time.time() - 100 * 3600, queued_job, running_job),
        )

        removed = store.sweep_expired()
        assert removed == []
        assert store.get(queued_job) is not None
        assert store.get(running_job) is not None

    def test_sweep_expired_removes_leftover_lp_file(self, store, tmp_path):
        """Even if a temp file somehow survives past mark_done/mark_error,
        sweep_expired cleans it up too."""
        job_id = store.create(SAMPLE_LP)
        store.mark_done(job_id, {"status": "optimal"})

        # Recreate a leftover temp file to simulate the edge case.
        lp_path = tmp_path / "tmp" / f"{job_id}.lp"
        lp_path.write_text(SAMPLE_LP)

        store._conn.execute(
            "UPDATE jobs SET end_ts = ? WHERE job_id = ?",
            (time.time() - 49 * 3600, job_id),
        )
        store.sweep_expired()
        assert not lp_path.exists()


class TestReapOrphansOnStartup:
    def test_reap_orphans_marks_running_job_as_error(self, store):
        job_id = store.create(SAMPLE_LP)
        store.claim_next()  # -> running

        reaped = store.reap_orphans_on_startup()

        assert reaped == [job_id]
        row = store.get(job_id)
        assert row["status"] == "error"
        assert row["error"] == ORPHAN_ERROR_MESSAGE
        assert row["end_ts"] is not None

    def test_reap_orphans_leaves_queued_jobs_alone(self, store):
        job_id = store.create(SAMPLE_LP)

        reaped = store.reap_orphans_on_startup()

        assert reaped == []
        row = store.get(job_id)
        assert row["status"] == "queued"

    def test_reap_orphans_leaves_terminal_jobs_alone(self, store):
        done_job = store.create(SAMPLE_LP)
        store.mark_done(done_job, {"status": "optimal"})
        error_job = store.create(SAMPLE_LP)
        store.mark_error(error_job, "already failed")

        reaped = store.reap_orphans_on_startup()

        assert reaped == []
        assert store.get(done_job)["status"] == "done"
        assert store.get(error_job)["status"] == "error"
        assert store.get(error_job)["error"] == "already failed"

    def test_reap_orphans_removes_orphaned_lp_temp_file(self, store, tmp_path):
        job_id = store.create(SAMPLE_LP)
        store.claim_next()  # -> running
        lp_path = tmp_path / "tmp" / f"{job_id}.lp"
        assert lp_path.exists()

        store.reap_orphans_on_startup()
        assert not lp_path.exists()

    def test_reap_orphans_on_fresh_db_is_noop(self, tmp_path):
        s = LPJobStore(db_path=tmp_path / "fresh.sqlite", tmp_dir=tmp_path / "tmp")
        try:
            assert s.reap_orphans_on_startup() == []
        finally:
            s.close()


class TestWalMode:
    def test_db_opened_in_wal_mode(self, store):
        mode = store._conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert mode.lower() == "wal"

    def test_db_file_created_at_given_path(self, tmp_path):
        db_path = tmp_path / "nested" / "jobs.sqlite"
        s = LPJobStore(db_path=db_path, tmp_dir=tmp_path / "tmp")
        try:
            s.create(SAMPLE_LP)
            assert db_path.exists()
        finally:
            s.close()
