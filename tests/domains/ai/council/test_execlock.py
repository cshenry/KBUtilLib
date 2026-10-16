"""The council's execution lock -- one ``flock`` on ``daemon.lock``.

Nothing is monkeypatched here: every test drives the real
:class:`~kbutillib.domains.ai.council.execlock.ExecutionLock` against a real
file in ``tmp_path``.  Exclusion is checked BOTH within one interpreter (two
``flock``s on two open file descriptions of the same file conflict, which is
what makes a second ``kbu council run`` in the same process fail) and across
two real ``spawn`` processes, because a lock that only excluded objects inside
one Python process would be no lock at all.

``flock`` is a POSIX facility; this module is Linux/macOS only, like the rest
of the fleet.  No test here makes a network call, spends a token, needs a CLI
login or needs a GPU.
"""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from kbutillib.domains.ai.council.execlock import (
    LOCK_FILENAME,
    LOCK_KINDS,
    ExecutionLock,
    LockHeld,
)

# ---------------------------------------------------------------------------
# Cross-process harness
# ---------------------------------------------------------------------------


def hold_in_subprocess(directory, kind, taken, release, results):
    """Take the lock in a brand-new interpreter and hold it until told.

    A module-level function because ``spawn`` pickles the target by qualified
    name and re-imports it; the child shares nothing with the parent but the
    directory path, which is the point.
    """
    try:
        lock = ExecutionLock(directory)
        with lock.acquire(kind) as record:
            taken.set()
            release.wait(timeout=60)
            results.put(("held", record["pid"]))
    except BaseException as exc:  # noqa: BLE001 - reported, never raised blind
        results.put(("error", f"{type(exc).__name__}: {exc}"))


def try_in_subprocess(directory, kind, results):
    """Try the lock once, non-blocking, and report what happened."""
    try:
        lock = ExecutionLock(directory)
        with lock.acquire(kind):
            results.put(("acquired", None))
    except LockHeld as exc:
        results.put(("held-by-other", str(exc)))
    except BaseException as exc:  # noqa: BLE001 - reported, see above
        results.put(("error", f"{type(exc).__name__}: {exc}"))


# ---------------------------------------------------------------------------
# Acquiring
# ---------------------------------------------------------------------------


class TestAcquire:
    def test_the_lock_file_lands_beside_the_store(self, tmp_path: Path) -> None:
        lock = ExecutionLock(tmp_path / "council")
        with lock.acquire("run"):
            assert (tmp_path / "council" / LOCK_FILENAME).exists()
        assert lock.path == tmp_path / "council" / LOCK_FILENAME

    def test_a_missing_directory_is_created(self, tmp_path: Path) -> None:
        lock = ExecutionLock(tmp_path / "not" / "yet")
        with lock.acquire("daemon"):
            assert lock.path.exists()

    def test_the_record_names_pid_kind_start_and_store(self, tmp_path: Path) -> None:
        lock = ExecutionLock(tmp_path, tmp_path / "council.sqlite")
        with lock.acquire("evaluate") as record:
            assert record["kind"] == "evaluate"
            assert record["pid"] == os.getpid()
            assert record["started_at"]
            assert record["store"] == str(tmp_path / "council.sqlite")
            # And it is on disk, so another process can read it.
            on_disk = json.loads(lock.path.read_text().splitlines()[0])
            assert on_disk == record

    @pytest.mark.parametrize("kind", LOCK_KINDS)
    def test_every_declared_kind_may_hold_it(self, tmp_path: Path, kind: str) -> None:
        with ExecutionLock(tmp_path).acquire(kind) as record:
            assert record["kind"] == kind

    def test_an_unknown_kind_is_refused_before_anything_is_created(
        self, tmp_path: Path
    ) -> None:
        """An unrecognised kind in the file would tell a user nothing."""
        lock = ExecutionLock(tmp_path / "fresh")
        with pytest.raises(ValueError, match="unknown execution-lock kind"):
            with lock.acquire("supervise"):
                pass
        assert not lock.path.exists()

    def test_the_lock_is_released_at_the_end_of_the_block(self, tmp_path: Path) -> None:
        lock = ExecutionLock(tmp_path)
        with lock.acquire("run"):
            pass
        with lock.acquire("run"):
            pass

    def test_the_lock_is_released_when_the_block_raises(self, tmp_path: Path) -> None:
        """A crash inside a council run must not strand the store."""
        lock = ExecutionLock(tmp_path)
        with pytest.raises(ZeroDivisionError):
            with lock.acquire("run"):
                1 / 0
        assert lock.probe() is None
        with lock.acquire("daemon"):
            pass

    def test_the_lock_file_survives_release(self, tmp_path: Path) -> None:
        """Unlinking it would let a second process lock a different inode."""
        lock = ExecutionLock(tmp_path)
        with lock.acquire("run"):
            pass
        assert lock.path.exists()
        assert lock.path.read_text().strip() == ""

    def test_two_stores_in_two_directories_do_not_exclude_each_other(
        self, tmp_path: Path
    ) -> None:
        first = ExecutionLock(tmp_path / "a")
        second = ExecutionLock(tmp_path / "b")
        with first.acquire("run"):
            with second.acquire("run"):
                assert first.probe() is not None
                assert second.probe() is not None


# ---------------------------------------------------------------------------
# Exclusion
# ---------------------------------------------------------------------------


class TestExclusion:
    def test_a_second_acquire_on_the_same_file_is_refused(self, tmp_path: Path) -> None:
        """Two open file descriptions on one file conflict, as flock promises."""
        held = ExecutionLock(tmp_path)
        other = ExecutionLock(tmp_path)
        with held.acquire("daemon"):
            with pytest.raises(LockHeld) as excinfo:
                with other.acquire("run"):
                    pass
        message = str(excinfo.value)
        assert "daemon" in message
        assert str(os.getpid()) in message
        assert str(held.path) in message

    def test_the_refusal_carries_the_holder(self, tmp_path: Path) -> None:
        held = ExecutionLock(tmp_path)
        with held.acquire("evaluate"):
            with pytest.raises(LockHeld) as excinfo:
                with ExecutionLock(tmp_path).acquire("migrate"):
                    pass
        holder = excinfo.value.holder
        assert holder is not None
        assert holder["kind"] == "evaluate"
        assert holder["pid"] == os.getpid()

    def test_the_refusal_says_which_kind_was_wanted(self, tmp_path: Path) -> None:
        with ExecutionLock(tmp_path).acquire("daemon"):
            with pytest.raises(LockHeld, match="council migrate"):
                with ExecutionLock(tmp_path).acquire("migrate"):
                    pass

    def test_a_refused_acquire_does_not_disturb_the_holders_record(
        self, tmp_path: Path
    ) -> None:
        held = ExecutionLock(tmp_path)
        with held.acquire("daemon") as record:
            with pytest.raises(LockHeld):
                with ExecutionLock(tmp_path).acquire("run"):
                    pass
            assert json.loads(held.path.read_text().splitlines()[0]) == record

    def test_exclusion_holds_across_two_real_processes(self, tmp_path: Path) -> None:
        ctx = multiprocessing.get_context("spawn")
        taken, release = ctx.Event(), ctx.Event()
        results = ctx.Queue()
        holder = ctx.Process(
            target=hold_in_subprocess,
            args=(str(tmp_path), "daemon", taken, release, results),
            daemon=True,
        )
        holder.start()
        try:
            assert taken.wait(timeout=60), "the holding child never took the lock"
            # The parent is a different process entirely, and is refused.
            with pytest.raises(LockHeld) as excinfo:
                with ExecutionLock(tmp_path).acquire("migrate"):
                    pass
            assert str(holder.pid) in str(excinfo.value)

            # And so is a third, freshly spawned process.
            challenger = ctx.Process(
                target=try_in_subprocess,
                args=(str(tmp_path), "run", results),
                daemon=True,
            )
            challenger.start()
            outcome, detail = results.get(timeout=60)
            challenger.join(timeout=30)
            assert outcome == "held-by-other", detail
            assert "daemon" in detail
        finally:
            release.set()
            holder.join(timeout=30)
            if holder.is_alive():  # pragma: no cover - only on a real hang
                holder.terminate()

    def test_the_kernel_releases_a_killed_holders_lock(self, tmp_path: Path) -> None:
        """The whole reason this is a file lock and not a database lease.

        A ``daemon_lease`` row held by a killed daemon stayed held until its
        TTL lapsed.  An ``flock`` goes the instant the process does.
        """
        ctx = multiprocessing.get_context("spawn")
        taken, release = ctx.Event(), ctx.Event()
        results = ctx.Queue()
        holder = ctx.Process(
            target=hold_in_subprocess,
            args=(str(tmp_path), "daemon", taken, release, results),
            daemon=True,
        )
        holder.start()
        assert taken.wait(timeout=60), "the holding child never took the lock"
        assert ExecutionLock(tmp_path).probe() is not None

        holder.kill()
        holder.join(timeout=30)
        assert not holder.is_alive()

        # No TTL, no reclaim, no wait: it is simply free.
        with ExecutionLock(tmp_path).acquire("migrate") as record:
            assert record["kind"] == "migrate"


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


class TestProbe:
    def test_an_unused_directory_probes_as_free(self, tmp_path: Path) -> None:
        assert ExecutionLock(tmp_path / "never-used").probe() is None

    def test_a_held_lock_probes_as_its_holder(self, tmp_path: Path) -> None:
        lock = ExecutionLock(tmp_path)
        with lock.acquire("run") as record:
            holder = ExecutionLock(tmp_path).probe()
        assert holder == record

    def test_a_released_lock_probes_as_free(self, tmp_path: Path) -> None:
        lock = ExecutionLock(tmp_path)
        with lock.acquire("run"):
            pass
        assert lock.probe() is None

    def test_probe_creates_nothing(self, tmp_path: Path) -> None:
        lock = ExecutionLock(tmp_path / "untouched")
        assert lock.probe() is None
        assert not lock.path.exists()
        assert not lock.path.parent.exists()

    def test_probe_does_not_take_the_lock_it_asks_about(self, tmp_path: Path) -> None:
        """A question, not a claim: a probe must not lock out the next run."""
        lock = ExecutionLock(tmp_path)
        with lock.acquire("run"):
            pass
        assert lock.probe() is None
        with lock.acquire("daemon"):
            pass
        assert lock.probe() is None

    def test_a_stale_record_on_a_free_lock_probes_as_free(self, tmp_path: Path) -> None:
        """Left by a process killed before its clear ran.  The KERNEL decides.

        Reading the file to decide would make this record look like a live
        daemon for ever, which is the failure mode the lease had.
        """
        lock = ExecutionLock(tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        lock.path.write_text(
            json.dumps({"kind": "daemon", "pid": 999999, "started_at": "2020-01-01"})
            + "\n"
        )
        assert lock.probe() is None
        with lock.acquire("migrate"):
            pass

    def test_a_held_lock_with_an_unreadable_record_still_probes_as_held(
        self, tmp_path: Path
    ) -> None:
        """Saying "free" because the line was unreadable is the one wrong answer."""
        lock = ExecutionLock(tmp_path)
        with lock.acquire("daemon"):
            # Simulate a holder that took the lock but had not yet written its
            # line, by emptying the file from underneath it.
            with open(lock.path, "r+", encoding="utf-8") as handle:
                handle.truncate()
            holder = ExecutionLock(tmp_path).probe()
        assert holder is not None
        assert holder["kind"] == "unknown"
