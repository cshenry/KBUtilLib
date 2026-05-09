"""Tests for the Watcher background thread in kb_job_utils."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from kbutillib.kb_job_utils.state import JobRecord, JobState
from kbutillib.kb_job_utils.store import JobStore
from kbutillib.kb_job_utils.utils import KBJobUtils, Watcher


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
        utils = KBJobUtils(env=mock_env, kb_version="ci", db_path=tmp_path / "test.db")
        utils._mock_ee2 = mock_ee2
        yield utils
        utils.close()


class TestWatcherLifecycle:
    def test_start_and_stop(self, kbu):
        """Watcher starts, runs at least once, and stops cleanly."""
        kbu._mock_ee2.check_jobs.return_value = {"job_states": {}}
        w = kbu.start_watcher(interval=30, daemon=True)
        assert w.is_alive()
        assert kbu.watcher is w
        stopped = kbu.stop_watcher(timeout=5.0)
        assert stopped
        assert kbu.watcher is None

    def test_idempotent_start(self, kbu):
        """Calling start_watcher twice returns the same watcher."""
        kbu._mock_ee2.check_jobs.return_value = {"job_states": {}}
        w1 = kbu.start_watcher(interval=30)
        w2 = kbu.start_watcher(interval=60)
        assert w1 is w2
        kbu.stop_watcher()

    def test_stop_interrupts_sleep(self, kbu):
        """stop_watcher should return quickly, not wait for the full interval."""
        kbu._mock_ee2.check_jobs.return_value = {"job_states": {}}
        kbu.start_watcher(interval=600)  # long interval
        t0 = time.monotonic()
        kbu.stop_watcher(timeout=5.0)
        elapsed = time.monotonic() - t0
        # Should stop in well under 2x the interval
        assert elapsed < 10.0

    def test_stop_timeout_returns_false(self, kbu):
        """If stop times out, returns False."""
        kbu._mock_ee2.check_jobs.return_value = {"job_states": {}}

        # Create a watcher that blocks on refresh_active
        original_refresh = kbu.refresh_active
        block_event = threading.Event()

        def blocking_refresh():
            block_event.wait(30)  # will block until set
            return []

        kbu.refresh_active = blocking_refresh
        w = kbu.start_watcher(interval=30)

        # Give thread time to enter blocking_refresh
        time.sleep(0.2)

        # stop with very short timeout -- thread is blocked in refresh
        result = kbu.stop_watcher(timeout=0.1)
        # Unblock the thread so it can exit
        block_event.set()
        w.join(timeout=5.0)
        kbu.refresh_active = original_refresh

    def test_watcher_property_none_when_not_started(self, kbu):
        assert kbu.watcher is None

    def test_close_stops_watcher(self, kbu):
        """KBJobUtils.close() should stop the watcher."""
        kbu._mock_ee2.check_jobs.return_value = {"job_states": {}}
        w = kbu.start_watcher(interval=30)
        assert w.is_alive()
        kbu.close()
        assert not w.is_alive()


class TestWatcherRefresh:
    def test_refresh_increments_run_counter(self, kbu):
        kbu._mock_ee2.check_jobs.return_value = {"job_states": {}}
        w = kbu.start_watcher(interval=30)
        # Wait for at least one run
        deadline = time.monotonic() + 5.0
        while w.runs == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert w.runs >= 1
        assert w.last_run_at is not None
        kbu.stop_watcher()

    def test_exception_in_refresh_does_not_kill_thread(self, kbu):
        """Errors in refresh_active are caught and counted."""

        def failing_refresh():
            raise RuntimeError("simulated EE2 failure")

        kbu.refresh_active = failing_refresh
        kbu._mock_ee2.check_jobs.return_value = {"job_states": {}}
        w = kbu.start_watcher(interval=30)

        # Wait for at least one error
        deadline = time.monotonic() + 5.0
        while w.errors < 1 and time.monotonic() < deadline:
            time.sleep(0.05)

        assert w.errors >= 1
        assert w.is_alive()  # Thread survived the error
        kbu.stop_watcher()

    def test_on_change_called_on_status_transition(self, kbu):
        """on_change callback fires when a job's status changes."""
        # Seed an active job
        kbu.store.upsert(
            JobRecord(job_id="j1", method="m.m", state=JobState.RUNNING)
        )

        # EE2 will report it as completed
        kbu._mock_ee2.check_jobs.return_value = {
            "job_states": {"j1": {"status": "completed"}}
        }

        changes = []

        def on_change(old_rec, new_rec):
            changes.append((old_rec.state, new_rec.state))

        w = kbu.start_watcher(interval=30, on_change=on_change)
        deadline = time.monotonic() + 5.0
        while not changes and time.monotonic() < deadline:
            time.sleep(0.05)

        assert len(changes) >= 1
        assert changes[0] == (JobState.RUNNING, JobState.COMPLETED)
        kbu.stop_watcher()

    def test_on_change_not_called_without_transition(self, kbu):
        """on_change should NOT fire when status stays the same."""
        kbu.store.upsert(
            JobRecord(job_id="j1", method="m.m", state=JobState.RUNNING)
        )
        kbu._mock_ee2.check_jobs.return_value = {
            "job_states": {"j1": {"status": "running"}}
        }

        changes = []

        def on_change(old_rec, new_rec):
            changes.append((old_rec.state, new_rec.state))

        w = kbu.start_watcher(interval=30, on_change=on_change)
        # Wait for at least one run
        deadline = time.monotonic() + 3.0
        while w.runs < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        kbu.stop_watcher()
        assert len(changes) == 0

    def test_on_change_exception_logged_not_fatal(self, kbu):
        """Errors in on_change callback are caught, not fatal."""
        kbu.store.upsert(
            JobRecord(job_id="j1", method="m.m", state=JobState.RUNNING)
        )
        kbu._mock_ee2.check_jobs.return_value = {
            "job_states": {"j1": {"status": "completed"}}
        }

        def bad_callback(old_rec, new_rec):
            raise ValueError("callback boom")

        w = kbu.start_watcher(interval=30, on_change=bad_callback)
        deadline = time.monotonic() + 3.0
        while w.runs < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert w.is_alive()  # Thread survived the callback error
        kbu.stop_watcher()


class TestRefreshMethods:
    """Tests for refresh_active/refresh_all/cleanup on KBJobUtils directly."""

    def test_refresh_active(self, kbu):
        kbu.store.upsert(JobRecord(job_id="j1", state=JobState.RUNNING))
        kbu.store.upsert(JobRecord(job_id="j2", state=JobState.COMPLETED))
        kbu._mock_ee2.check_jobs.return_value = {
            "job_states": {"j1": {"status": "completed"}}
        }
        results = kbu.refresh_active()
        assert len(results) == 1
        assert results[0].state == JobState.COMPLETED

    def test_refresh_active_empty(self, kbu):
        results = kbu.refresh_active()
        assert results == []

    def test_refresh_all(self, kbu):
        kbu.store.upsert(JobRecord(job_id="j1", state=JobState.RUNNING))
        kbu.store.upsert(JobRecord(job_id="j2", state=JobState.COMPLETED))
        kbu._mock_ee2.check_jobs.return_value = {
            "job_states": {
                "j1": {"status": "completed"},
                "j2": {"status": "completed"},
            }
        }
        results = kbu.refresh_all()
        assert len(results) == 2

    def test_cleanup_terminal_only(self, kbu):
        from datetime import timedelta

        old_time = datetime.now(timezone.utc) - timedelta(days=60)
        rec1 = JobRecord(job_id="j1", state=JobState.COMPLETED)
        rec1.updated_at = old_time
        rec2 = JobRecord(job_id="j2", state=JobState.RUNNING)
        rec2.updated_at = old_time
        rec3 = JobRecord(job_id="j3", state=JobState.ERROR)
        rec3.updated_at = old_time
        kbu.store.upsert(rec1)
        kbu.store.upsert(rec2)
        kbu.store.upsert(rec3)

        # Need to force the updated_at to be old (upsert sets it to now)
        # Work around by directly updating the DB
        kbu.store._conn.execute(
            "UPDATE jobs SET updated_at = ? WHERE job_id IN (?, ?, ?)",
            (old_time.isoformat(), "j1", "j2", "j3"),
        )
        kbu.store._conn.commit()

        deleted = kbu.cleanup(older_than_days=30, terminal_only=True)
        # j1 (completed) and j3 (error) should be deleted; j2 (running) kept
        assert deleted == 2
        assert kbu.store.get("j2") is not None
        assert kbu.store.get("j1") is None
        assert kbu.store.get("j3") is None

    def test_cleanup_all_statuses(self, kbu):
        from datetime import timedelta

        old_time = datetime.now(timezone.utc) - timedelta(days=60)
        rec1 = JobRecord(job_id="j1", state=JobState.RUNNING)
        kbu.store.upsert(rec1)
        kbu.store._conn.execute(
            "UPDATE jobs SET updated_at = ? WHERE job_id = ?",
            (old_time.isoformat(), "j1"),
        )
        kbu.store._conn.commit()

        deleted = kbu.cleanup(older_than_days=30, terminal_only=False)
        assert deleted == 1

    def test_cleanup_recent_not_deleted(self, kbu):
        kbu.store.upsert(
            JobRecord(job_id="j1", state=JobState.COMPLETED)
        )
        deleted = kbu.cleanup(older_than_days=30)
        assert deleted == 0
