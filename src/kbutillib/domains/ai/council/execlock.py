"""The council's execution lock: one ``flock`` on one file beside the store.

Exactly one council process may be executing against a store at a time -- a
daemon, a ``kbu council run``, a ``kbu council evaluate`` or a ``db migrate``.
Version 1 enforced that with a ``daemon_lease`` row in the database.  Review
round 2 removed it, and this replaced it, because a row cannot do the job a
file lock does:

* A lease has a TTL, so a crashed holder keeps it until the TTL lapses.  An
  ``flock`` is released by the kernel the instant the holding process dies,
  however it dies, so a killed daemon never strands the store.
* A lease needs the database open to be read, which is exactly what
  ``db migrate`` cannot assume -- it may be about to open a store whose
  schema version this code refuses.
* A store is one machine's local file (there is no shared store any more), so
  a lock that is only meaningful on one host is not a limitation.

Both operations are NON-BLOCKING by design.  Waiting would turn "another
council is running" into a hang with no output, and the answer a user wants is
the name and pid of what is already running so they can decide, not a prompt
that sits there.

Usage::

    lock = ExecutionLock(store_path.parent)
    with lock.acquire("run"):          # LockHeld if somebody else has it
        ...                            # released on the way out, and on crash

    holder = lock.probe()              # None, or who holds it
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import socket
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Union

__all__ = [
    "ExecutionLock",
    "LockHeld",
    "LOCK_FILENAME",
    "LOCK_KINDS",
]

logger = logging.getLogger(__name__)

#: The lock file's name, in the store's own directory.  Named for the daemon
#: because the daemon is the long-running holder; the other three kinds take
#: the same lock precisely so they cannot run beside it.
LOCK_FILENAME = "daemon.lock"

#: What may hold the lock.  A closed set: an unknown kind in the file would
#: tell a user nothing about what to stop, so it is refused on the way in.
LOCK_KINDS = ("daemon", "run", "evaluate", "migrate")


class LockHeld(RuntimeError):
    """Another council process holds the execution lock.

    A ``RuntimeError`` rather than a council exception type: this is not a
    store failure, a config failure or a model failure, and every caller
    treats it the same way -- report who holds it and stop.

    Carries :attr:`holder`, the same dict :meth:`ExecutionLock.probe` returns,
    or ``None`` when the holder's record could not be read (a holder that has
    taken the lock but not yet written its line).
    """

    def __init__(self, message: str, holder: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.holder = holder


class ExecutionLock:
    """An exclusive ``flock`` on ``daemon.lock`` in *directory*.

    Args:
        directory: where the lock file lives -- the store file's own
            directory, so one store has one lock and two stores on one machine
            do not exclude each other.
        store_path: the store this lock guards, recorded in the holder's line
            purely so ``probe()`` can say which store is busy.  Defaults to
            ``directory/council.sqlite``.

    The lock file is created if absent and is NEVER deleted: unlinking it
    while another process held an open descriptor on it would let a third
    process create a fresh file and lock that instead, and both would then
    believe they were alone.  An empty leftover lock file is harmless.
    """

    def __init__(
        self,
        directory: Union[str, Path],
        store_path: Optional[Union[str, Path]] = None,
    ) -> None:
        self.directory = Path(directory).expanduser()
        self.path = self.directory / LOCK_FILENAME
        self.store_path = (
            Path(store_path).expanduser()
            if store_path is not None
            else self.directory / "council.sqlite"
        )

    # -- acquiring --------------------------------------------------------

    @contextmanager
    def acquire(self, kind: str) -> Iterator[Dict[str, Any]]:
        """Hold the lock for the duration of the block, or raise immediately.

        Non-blocking: if another process has it, this raises
        :class:`LockHeld` naming that holder's kind, pid and start time rather
        than waiting.

        On the way in the holder's record -- pid, kind, start time and store
        path -- is written to the file as one line of JSON, so
        :meth:`probe` can say WHO is running rather than only that somebody
        is.  The record is written AFTER the lock is taken, which is why
        ``probe`` tolerates an empty file.

        Yields:
            The holder record that was written.

        Raises:
            LockHeld: another process holds the lock.
            ValueError: *kind* is not one of :data:`LOCK_KINDS`.
            OSError: the lock file's directory cannot be created or opened.
        """
        if kind not in LOCK_KINDS:
            raise ValueError(
                f"unknown execution-lock kind {kind!r}; expected one of {list(LOCK_KINDS)}"
            )
        self.directory.mkdir(parents=True, exist_ok=True)
        # "a+" rather than "w": truncating on open would wipe the CURRENT
        # holder's record before we knew whether we could have the lock.
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                holder = self.probe()
                raise LockHeld(self._held_message(kind, holder), holder) from None
            record = self._record(kind)
            self._write(handle, record)
            try:
                yield record
            finally:
                # The record is cleared but the lock file stays (see the class
                # docstring).  Clearing it means a later probe says "free"
                # rather than naming a process that has finished.
                self._clear(handle)
        finally:
            handle.close()

    # -- probing ----------------------------------------------------------

    def probe(self) -> Optional[Dict[str, Any]]:
        """Who holds the lock, or ``None`` if nobody does.

        Non-blocking and read-only: it does not create the lock file and does
        not disturb a holder.  Implemented by TRYING the lock rather than by
        reading the file, because a stale record left by a process that was
        killed between taking the lock and writing its line -- or by one whose
        clear did not run -- would otherwise read as a live holder.  The record
        is only consulted once the kernel has said the lock is held.

        A held lock whose record is unreadable (empty, truncated, not JSON)
        comes back as ``{"kind": "unknown", ...}`` rather than ``None``: the
        lock IS held, and saying "free" because the line was unreadable is the
        one wrong answer available.
        """
        if not self.path.exists():
            return None
        try:
            handle = open(self.path, "r+", encoding="utf-8")
        except OSError:
            return None
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                return self._read(handle)
            # We got it, so nobody held it.  Release at once: probe() is a
            # question, not a claim.
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return None
        finally:
            handle.close()

    # -- internals --------------------------------------------------------

    def _record(self, kind: str) -> Dict[str, Any]:
        return {
            "kind": kind,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "store": str(self.store_path),
            "lock": str(self.path),
        }

    @staticmethod
    def _write(handle: Any, record: Dict[str, Any]) -> None:
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    @staticmethod
    def _clear(handle: Any) -> None:
        try:
            handle.seek(0)
            handle.truncate()
            handle.flush()
        except OSError as exc:  # pragma: no cover - a closed/full filesystem
            logger.debug("council execlock: could not clear the holder record: %s", exc)

    def _read(self, handle: Any) -> Dict[str, Any]:
        """The holder record from an ALREADY-KNOWN-held lock file."""
        try:
            handle.seek(0)
            text = handle.read().strip()
        except OSError:
            text = ""
        if text:
            try:
                parsed = json.loads(text.splitlines()[0])
                if isinstance(parsed, dict):
                    return parsed
            except (ValueError, IndexError):
                pass
        return {
            "kind": "unknown",
            "pid": None,
            "host": None,
            "started_at": None,
            "store": str(self.store_path),
            "lock": str(self.path),
        }

    def held_message(self, wanted: str, holder: Optional[Dict[str, Any]] = None) -> str:
        """The refusal :meth:`acquire` would raise, for a caller that PROBED.

        The council daemon's ``start`` probes before it spawns a child, so
        that "something else is already running" is reported by the command
        the user typed rather than by a detached child in a log file.  It
        needs the same wording :meth:`acquire` raises -- two texts for one
        refusal is how a user ends up reading the less helpful one.
        """
        return self._held_message(wanted, self.probe() if holder is None else holder)

    def _held_message(self, wanted: str, holder: Optional[Dict[str, Any]]) -> str:
        who = "another council process"
        if holder:
            kind = holder.get("kind") or "unknown"
            pid = holder.get("pid")
            started = holder.get("started_at")
            who = f"a council {kind}"
            if pid:
                who = f"{who} (pid {pid})"
            if started:
                who = f"{who} started at {started}"
        return (
            f"refusing to start a council {wanted}: {who} already holds the "
            f"execution lock {str(self.path)!r}. One council at a time per "
            "store -- two would claim the same calls and a migration would "
            "change the schema under a running worker. Stop it, or wait for "
            "it to finish."
        )
