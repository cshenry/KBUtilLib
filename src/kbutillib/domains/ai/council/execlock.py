"""The council's execution lock: shared flocks admit WORKERS, one exclusive
flock excludes a MIGRATION.

Exactly one council process held this exclusively at a time in version 1 -- a
daemon, a ``kbu council run``, a ``kbu council evaluate`` or a ``db migrate``.
That used a ``daemon_lease`` row in the database.  Review round 2 removed it,
and this replaced it, because a row cannot do the job a file lock does:

* A lease has a TTL, so a crashed holder keeps it until the TTL lapses.  An
  ``flock`` is released by the kernel the instant the holding process dies,
  however it dies, so a killed daemon never strands the store.
* A lease needs the database open to be read, which is exactly what
  ``db migrate`` cannot assume -- it may be about to open a store whose
  schema version this code refuses.
* A store is one machine's local file (there is no shared store any more), so
  a lock that is only meaningful on one host is not a limitation.

This redesign (the per-queue-worker split) widened the single EXCLUSIVE
holder to admit several concurrent WORKER processes on one store.  That reads
as a weakening of the guarantee above, so the reasoning is recorded here
rather than only in a commit message:

* Two workers claiming the same call was the original worry, and this lock
  was never what prevented that -- the claim protocol does.  A claim is taken
  under ``BEGIN IMMEDIATE`` with a per-row ``claim_token`` and a
  ``status == 'pending'`` guard baked into the UPDATE itself
  (``store/queries.py``'s ``claim_update``, around line 236), so no number of
  concurrent workers can take the same row: the database refuses every UPDATE
  but the first. This lock does not need to guard against that, for any
  number of workers.
* A migration changing the schema out from under a running worker has no
  other backstop, which is exactly why ``migrate`` alone stays EXCLUSIVE on
  the store-wide lock, and why a worker's SHARED hold still conflicts with a
  ``migrate`` attempt.

So: ``daemon`` and ``run`` take the store-wide lock SHARED (``LOCK_SH``) when
they name a *partition*, which makes them a queue worker, and ALSO take their
own lock scoped to ``(queue_name, worker_index)`` EXCLUSIVELY, so two workers
can never believe they are the same one.  Unpartitioned ``run``, an old-style
``daemon``, and ``evaluate`` all stay EXCLUSIVE on the store-wide lock exactly
as before, so none of them can ever coexist with a queue worker, in either
direction.  ``migrate`` is always EXCLUSIVE.  A ``controller`` holds the
store-wide lock SHARED for as long as it supervises queue workers -- so it,
too, excludes and is excluded by the unpartitioned/legacy kinds -- plus its
own persistent EXCLUSIVE lock, so two controllers never run against one store
at once.  Queue workers are only ever launched beneath a single controller.

The store-wide lock file keeps its existing name and inode, ``daemon.lock``,
rather than being renamed to something like ``council.lock``: an old binary
and a new binary would each take "the" store lock under a different name and
never see each other, which is a silent double-council.

Both operations are NON-BLOCKING by design.  Waiting would turn "another
council is running" into a hang with no output, and the answer a user wants is
the name and pid of what is already running so they can decide, not a prompt
that sits there.

Usage::

    lock = ExecutionLock(store_path.parent)
    with lock.acquire("run"):                           # LockHeld if somebody
        ...                                              # else has it

    with lock.acquire("run", partition=("seat-a", 0)):  # a queue worker:
        ...                                              # shared store-wide,
                                                           # exclusive on its
                                                           # own partition

    holder = lock.probe()              # None, or who holds it
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import socket
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

__all__ = [
    "ExecutionLock",
    "LockHeld",
    "LOCK_FILENAME",
    "CONTROLLER_LOCK_FILENAME",
    "LOCK_KINDS",
]

logger = logging.getLogger(__name__)

#: The store-wide lock file's name, in the store's own directory.  Named for
#: the daemon because the daemon was historically the long-running holder;
#: kept unchanged by this redesign (see the module docstring) so an old and a
#: new binary always contend for the same file.
LOCK_FILENAME = "daemon.lock"

#: The controller's own singleton lock file, in the store's own directory.
#: Distinct from ``LOCK_FILENAME``: a controller holds the store-wide lock
#: SHARED (same as a queue worker) and additionally needs a lock that excludes
#: OTHER controllers, which shared mode on the store-wide file cannot do on
#: its own (two SHARED holders are meant to coexist).
CONTROLLER_LOCK_FILENAME = "controller.lock"

#: What may hold the store-wide lock.  A closed set: an unknown kind in the
#: file would tell a user nothing about what to stop, so it is refused on the
#: way in.  ``controller`` is the persistent supervisor queue workers are
#: launched beneath.
LOCK_KINDS = ("daemon", "run", "evaluate", "migrate", "controller")

#: Kinds that may name a partition and take the store-wide lock SHARED instead
#: of EXCLUSIVE.  ``migrate``, ``evaluate`` and ``controller`` are never
#: partitioned -- a migration and an evaluate run are store-wide operations,
#: and the controller is the store-wide supervisor itself.
_PARTITIONABLE_KINDS = ("daemon", "run")

#: ``(queue_name, worker_index)`` -- the two values a queue worker is scoped
#: to.  ``worker_index`` runs ``0..workers-1`` for a queue declaring more than
#: one worker.
Partition = Tuple[str, int]


class LockHeld(RuntimeError):
    """Another council process holds the execution lock.

    A ``RuntimeError`` rather than a council exception type: this is not a
    store failure, a config failure or a model failure, and every caller
    treats it the same way -- report who holds it and stop.

    Carries :attr:`holder`, the same dict :meth:`ExecutionLock.probe` returns
    (the FIRST holder, when more than one is named in the message), or
    ``None`` when the holder's record could not be read (a holder that has
    taken the lock but not yet written its line).
    """

    def __init__(self, message: str, holder: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.holder = holder


class ExecutionLock:
    """Flocks on ``daemon.lock`` (and, for a queue worker, its own partition
    file) in *directory*.

    Args:
        directory: where the lock files live -- the store file's own
            directory, so one store has one set of locks and two stores on
            one machine do not exclude each other.
        store_path: the store this lock guards, recorded in the holder's line
            purely so ``probe()`` can say which store is busy.  Defaults to
            ``directory/council.sqlite``.

    No lock file created by this class is ever deleted while it might still
    be meaningful: unlinking the store-wide file or a partition file while
    another process held an open descriptor on it would let a third process
    create a fresh file and lock that instead, and both would then believe
    they were alone.  An empty leftover lock file is harmless.  (A worker's
    own SHARED-registration file, used only to enumerate shared holders, is
    the one exception -- see :meth:`_register_shared_holder`: it is unique to
    one acquisition, nothing else ever opens it by name, and a crash leaves it
    behind only until the next enumeration notices the kernel already freed
    it.)
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
    def acquire(
        self, kind: str, partition: Optional[Partition] = None
    ) -> Iterator[Dict[str, Any]]:
        """Hold the lock for the duration of the block, or raise immediately.

        Non-blocking: if another process has it, this raises
        :class:`LockHeld` naming that holder (or, for a store-wide refusal
        while several workers hold it SHARED, every one of them) rather than
        waiting.

        Args:
            kind: one of :data:`LOCK_KINDS`.
            partition: ``(queue_name, worker_index)``, naming this acquisition
                as one child of a queue.  Only meaningful for ``kind`` in
                ``("daemon", "run")`` -- passing it for any other kind is a
                :class:`ValueError`.  When given, the store-wide lock is
                taken SHARED (so other queue workers and the controller may
                hold it at the same time) and this partition's OWN lock is
                taken EXCLUSIVELY (so two workers can never believe they are
                the same ``(queue_name, worker_index)``).  Omitted (the
                default), this is the original, unpartitioned, EXCLUSIVE
                acquisition on the store-wide lock -- the call shape every
                existing caller (``daemon.py``, the CLI run path,
                ``evaluate()``) already uses, unchanged.

        On the way in, the holder's record -- pid, kind, start time, store
        path and (for a queue worker) partition -- is written as one line of
        JSON to the file that record belongs to, so :meth:`probe` and a
        refusal message can say WHO is running rather than only that somebody
        is.

        Yields:
            The holder record that was written.

        Raises:
            LockHeld: another process holds a lock this acquisition needs.
            ValueError: *kind* is not one of :data:`LOCK_KINDS`, or
                *partition* was given for a *kind* that cannot take one.
            OSError: the lock file's directory cannot be created or opened.
        """
        if kind not in LOCK_KINDS:
            raise ValueError(
                f"unknown execution-lock kind {kind!r}; expected one of {list(LOCK_KINDS)}"
            )
        if partition is not None and kind not in _PARTITIONABLE_KINDS:
            raise ValueError(
                f"a partition is only meaningful for kind in {list(_PARTITIONABLE_KINDS)}, "
                f"not {kind!r}"
            )
        self.directory.mkdir(parents=True, exist_ok=True)

        if kind == "controller":
            with self._acquire_controller() as record:
                yield record
            return

        if partition is not None:
            with self._acquire_partitioned_worker(kind, partition) as record:
                yield record
            return

        # Unpartitioned: the original single EXCLUSIVE holder, unchanged --
        # ``migrate``, ``evaluate``, an old-style ``daemon`` and an
        # unpartitioned ``run`` all take this branch.
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                raise self._refuse_store_wide(kind) from None
            record = self._record(kind)
            self._write(handle, record)
            try:
                yield record
            finally:
                # The record is cleared but the lock file stays (see the
                # class docstring).  Clearing it means a later probe says
                # "free" rather than naming a process that has finished.
                self._clear(handle)
        finally:
            handle.close()

    @contextmanager
    def _acquire_partitioned_worker(
        self, kind: str, partition: Partition
    ) -> Iterator[Dict[str, Any]]:
        """A queue worker: SHARED on the store-wide lock, EXCLUSIVE on its own."""
        main_handle = open(self.path, "a+", encoding="utf-8")
        try:
            try:
                fcntl.flock(main_handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                raise self._refuse_store_wide(kind, partition) from None

            partition_path = self._partition_path(partition)
            record = self._record(kind, partition=partition, lock_path=partition_path)
            shared_entry = self._register_shared_holder(record)
            try:
                partition_handle = open(partition_path, "a+", encoding="utf-8")
                try:
                    try:
                        fcntl.flock(
                            partition_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                        )
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EAGAIN):
                            raise
                        holder = self._read_path(partition_path)
                        raise LockHeld(
                            self._partition_held_message(kind, partition, holder),
                            holder,
                        ) from None
                    self._write(partition_handle, record)
                    try:
                        yield record
                    finally:
                        self._clear(partition_handle)
                finally:
                    partition_handle.close()
            finally:
                self._unregister_shared_holder(shared_entry)
        finally:
            main_handle.close()

    @contextmanager
    def _acquire_controller(self) -> Iterator[Dict[str, Any]]:
        """The store's one persistent controller: SHARED store-wide, plus its
        own EXCLUSIVE singleton lock so two controllers never coexist."""
        main_handle = open(self.path, "a+", encoding="utf-8")
        try:
            try:
                fcntl.flock(main_handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                raise self._refuse_store_wide("controller") from None

            controller_path = self.directory / CONTROLLER_LOCK_FILENAME
            record = self._record("controller", lock_path=controller_path)
            shared_entry = self._register_shared_holder(record)
            try:
                controller_handle = open(controller_path, "a+", encoding="utf-8")
                try:
                    try:
                        fcntl.flock(
                            controller_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                        )
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EAGAIN):
                            raise
                        holder = self._read_path(controller_path)
                        raise LockHeld(
                            self._controller_held_message(holder), holder
                        ) from None
                    self._write(controller_handle, record)
                    try:
                        yield record
                    finally:
                        self._clear(controller_handle)
                finally:
                    controller_handle.close()
            finally:
                self._unregister_shared_holder(shared_entry)
        finally:
            main_handle.close()

    # -- probing ----------------------------------------------------------

    def probe(self) -> Optional[Dict[str, Any]]:
        """Who holds the STORE-WIDE lock, or ``None`` if nobody does.

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

        When the store-wide lock is held SHARED by one or more queue workers
        or a controller, this still reports "held" (the EXCLUSIVE probe
        attempt still fails), but the single record returned may read as
        ``"unknown"`` -- SHARED holders register themselves for enumeration
        elsewhere (see :meth:`_live_shared_holders`, used by a refusal
        message) rather than by writing the store-wide file's content, since
        more than one of them may be holding it at once.  Use the message on
        a :class:`LockHeld` raised by :meth:`acquire` to see every one of
        them.
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
                return self._read_path(self.path)
            # We got it, so nobody held it.  Release at once: probe() is a
            # question, not a claim.
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return None
        finally:
            handle.close()

    # -- internals: records -------------------------------------------------

    def _record(
        self,
        kind: str,
        partition: Optional[Partition] = None,
        lock_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "kind": kind,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "store": str(self.store_path),
            "lock": str(lock_path if lock_path is not None else self.path),
        }
        if partition is not None:
            record["partition"] = [partition[0], partition[1]]
        return record

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

    def _read_path(self, path: Path, lock_path: Optional[Path] = None) -> Dict[str, Any]:
        """The holder record from an ALREADY-KNOWN-held lock file at *path*."""
        try:
            text = path.read_text(encoding="utf-8").strip()
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
            "lock": str(lock_path if lock_path is not None else path),
        }

    # -- internals: partition locks ------------------------------------------

    def _partition_path(self, partition: Partition) -> Path:
        queue_name, index = partition
        return self.directory / f"council-{queue_name}-{index}.lock"

    # -- internals: shared-holder enumeration --------------------------------
    #
    # The store-wide lock, held SHARED by any number of queue workers and the
    # controller at once, cannot use the single-line-in-one-file scheme the
    # EXCLUSIVE kinds use -- two processes truncating and writing the same
    # file concurrently would stomp on each other.  Instead each SHARED
    # holder registers itself as its OWN uniquely named file, exclusively
    # flocked for as long as it holds the store-wide lock.  Enumerating
    # holders means trying each file's lock: acquirable means the registering
    # process is gone (stale, cleaned up on sight, exactly like every other
    # staleness check in this module), not acquirable means a live holder
    # whose record is read for display.

    _SHARED_HOLDERS_DIRNAME = "daemon.lock.shared"

    def _shared_holders_dir(self) -> Path:
        return self.directory / self._SHARED_HOLDERS_DIRNAME

    def _register_shared_holder(self, record: Dict[str, Any]) -> Tuple[Path, Any]:
        directory = self._shared_holders_dir()
        directory.mkdir(parents=True, exist_ok=True)
        token = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        path = directory / f"{token}.json"
        handle = open(path, "a+", encoding="utf-8")
        # Always free: nothing else has ever had this exact, brand-new path
        # open.
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._write(handle, record)
        return path, handle

    @staticmethod
    def _unregister_shared_holder(entry: Tuple[Path, Any]) -> None:
        path, handle = entry
        try:
            handle.close()  # releases the flock; nothing else ever held it
        finally:
            try:
                path.unlink()
            except OSError:
                pass

    def _live_shared_holders(self) -> List[Dict[str, Any]]:
        directory = self._shared_holders_dir()
        if not directory.exists():
            return []
        holders: List[Dict[str, Any]] = []
        for entry in sorted(directory.iterdir()):
            if entry.suffix != ".json":
                continue
            try:
                handle = open(entry, "r+", encoding="utf-8")
            except OSError:
                continue
            stale = False
            try:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    holders.append(self._read_path(entry))
                else:
                    # We got it -- the registering holder is gone. Stale.
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    stale = True
            finally:
                handle.close()
            if stale:
                try:
                    entry.unlink()
                except OSError:
                    pass
        return holders

    # -- internals: refusal messages -----------------------------------------

    def _refuse_store_wide(
        self, wanted: str, partition: Optional[Partition] = None
    ) -> LockHeld:
        """Build (not raise) the :class:`LockHeld` for a failed store-wide
        attempt, EXCLUSIVE or SHARED, naming every SHARED holder when there
        are any rather than just the one the legacy single-holder file names.
        """
        holders = self._live_shared_holders()
        if holders:
            return LockHeld(self._shared_held_message(wanted, holders, partition), holders[0])
        holder = self.probe()
        return LockHeld(self._held_message(wanted, holder), holder)

    def _shared_held_message(
        self,
        wanted: str,
        holders: List[Dict[str, Any]],
        partition: Optional[Partition] = None,
    ) -> str:
        descriptions = []
        for holder in holders:
            kind = holder.get("kind") or "unknown"
            pid = holder.get("pid")
            holder_partition = holder.get("partition")
            who = f"a council {kind}"
            if holder_partition:
                who = f"{who} (partition {holder_partition[0]}/{holder_partition[1]})"
            if pid:
                who = f"{who} (pid {pid})"
            descriptions.append(who)
        who_text = "; ".join(descriptions) if descriptions else "another council process"
        wanted_text = f"council {wanted}"
        if partition is not None:
            wanted_text = f"{wanted_text} for partition {partition[0]}/{partition[1]}"
        return (
            f"refusing to start a {wanted_text}: {who_text} already hold the "
            f"execution lock {str(self.path)!r} in shared mode. An exclusive "
            "operation cannot run while any worker holds it -- a migration "
            "would change the schema under a running worker. Stop them, or "
            "wait for them to finish."
        )

    def _partition_held_message(
        self, wanted: str, partition: Partition, holder: Optional[Dict[str, Any]]
    ) -> str:
        queue_name, index = partition
        who = "another council worker"
        if holder:
            kind = holder.get("kind") or "unknown"
            pid = holder.get("pid")
            who = f"a council {kind}"
            if pid:
                who = f"{who} (pid {pid})"
        return (
            f"refusing to start a council {wanted} for partition "
            f"{queue_name}/{index}: {who} already holds that partition's "
            f"execution lock {str(self._partition_path(partition))!r}. Two "
            "workers for the same (queue, index) would claim to be the same "
            "worker. Stop it, or wait for it to finish."
        )

    def _controller_held_message(self, holder: Optional[Dict[str, Any]]) -> str:
        who = "another council controller"
        if holder:
            pid = holder.get("pid")
            if pid:
                who = f"a council controller (pid {pid})"
        return (
            "refusing to start a council controller: "
            f"{who} already holds the controller lock "
            f"{str(self.directory / CONTROLLER_LOCK_FILENAME)!r}. One "
            "controller per store -- its active queue configuration is "
            "immutable while any of its workers is running. Stop it, or "
            "wait for it to finish."
        )

    def held_message(self, wanted: str, holder: Optional[Dict[str, Any]] = None) -> str:
        """The refusal :meth:`acquire` would raise, for a caller that PROBED.

        The council daemon's ``start`` probes before it spawns a child, so
        that "something else is already running" is reported by the command
        the user typed rather than by a detached child in a log file.  It
        needs the same wording :meth:`acquire` raises -- two texts for one
        refusal is how a user ends up reading the less helpful one.

        Unchanged in call shape: it still answers for the single-holder,
        store-wide, unpartitioned case every existing caller uses.
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
