"""The council daemon: one long-running worker per machine store.

``kbu council run`` works one job and exits.  That is the right shape for a
20-item experiment and the wrong shape for a 20k-item batch against two
interactive subscriptions, which takes days and must survive a closed laptop
lid, a lost terminal and an agent session ending.  The daemon is the same
``CouncilWorker`` over the SAME queue, detached from any terminal, working
EVERY job in the store rather than one.

Five properties are load-bearing, and each exists because of something that
went wrong in a design that did not have it:

**One council per store, enforced by a file lock.**  The daemon holds
:class:`~.execlock.ExecutionLock` (kind ``daemon``) for its whole life, and so
do ``kbu council run`` (kind ``run``) and ``evaluate()`` (kind ``evaluate``).
Two workers on one store would claim the same calls and a migration would
change the schema under a running worker.  There is no lease and no fencing
generation -- the kernel releases an ``flock`` the instant its holder dies, so
a killed daemon strands nothing and a sleeping laptop needs no TTL.

**No store is shared.**  A store is one machine's local SQLite file (review
round 2), so this daemon coordinates with nothing: there is no cross-host
election, no port and no server.  The queue it drains is the queue on its own
disk.

**Readiness is per (endpoint, served model), and it is re-probed.**  An
endpoint being reachable is not evidence that it serves the model a seat
names, and a route that was down when the daemon started will usually come
back -- so a route is probed at start, every
:attr:`DaemonTimings.probe_s`, when a halt is cleared and when a new job
introduces it, and the probe's own failure text is written onto the waiting
calls so ``kbu council jobs status`` can say WHY nothing is moving.

**The working directory is the council's, never the caller's.**  Both CLI
backends read project context out of the cwd, and a daemon started from an
agent's worktree that inherited that worktree would answer council questions
with that repository in scope.  :meth:`CouncilDaemon.run_foreground` chdirs
into the council directory before it serves anything.

**Stopping is bounded and costs no retry.**  ``SIGTERM`` stops claiming and
lets in-flight calls finish for up to :attr:`DaemonTimings.drain_s`; at the
deadline unfinished HTTP tasks are cancelled, every unfinished CLI process
GROUP is killed and reaped, and each still-claimed call is -- in one
transaction -- given an ``interrupted`` attempt, has its claim token
invalidated and goes back to pending **without** its retry count moving.  A
daemon being stopped is not the call's fault, and spending its retry budget
on an operator's ``systemctl restart`` is how a long batch dies of attrition.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import logging
import multiprocessing
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .engine.worker import CouncilWorker
from .execlock import CONTROLLER_LOCK_FILENAME, ExecutionLock, LockHeld
from .servable import probe_endpoint, seat_state
from .store.errors import StaleClaim
from .types import CouncilError, CouncilException

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TIMINGS",
    "LOG_FILENAME",
    "CouncilDaemon",
    "DaemonError",
    "DaemonTimings",
    "LockHeld",
]

#: Where a detached daemon's stdout and stderr are APPENDED, in the council
#: directory beside the store and the lock.  Appended rather than truncated:
#: the reason the last daemon died is the thing you want when the next one
#: will not start.
LOG_FILENAME = "daemon.log"

#: The ``calls.error`` kind written for a route whose probe failed, chosen
#: because ``store.queries.BLOCKER_BY_ERROR_KIND`` maps it to the
#: ``waiting_for_model`` blocker -- which is exactly what it is.
ROUTE_ERROR_KIND = "model_unavailable"

#: The ``attempts.outcome_kind`` written for a call the stop deadline cut off.
#: Deliberately NOT one of ``types.ERROR_KINDS``: the call did not fail, it was
#: interrupted, and filing it as a ``timeout`` would make a restarted daemon
#: look like a flaky endpoint.
INTERRUPTED_OUTCOME = "interrupted"

#: What a call interrupted mid-flight spent.  ``None`` would mean "nothing";
#: an absent key would mean "not asked".  Neither is true -- the call WAS made
#: and what it cost is unknowable -- and a budgeted endpoint has to be able to
#: tell the difference (see :meth:`CouncilDaemon._interrupt_claimed`).
UNKNOWN_USAGE: Dict[str, Any] = {"unknown": True}


class DaemonError(CouncilException):
    """The daemon could not be started, stopped or inspected.

    Not a :class:`LockHeld`: "something else is already running" is a normal,
    expected answer with its own type and its own holder record.  This is for
    everything else -- a child that died before it took the lock, a ``stop``
    aimed at a holder that is not a daemon.
    """


@dataclass(frozen=True)
class DaemonTimings:
    """Every clock the daemon runs on, in one injectable record.

    The production defaults are the specified ones.  They are gathered here
    and passed in rather than read from module constants because the tests
    spawn real subprocesses and wait on real signals and real deadlines: with
    a 120-second drain compiled in, proving that the deadline interrupts a
    hung call would cost two minutes of wall clock per test.

    Attributes:
        heartbeat_s: how often the ``workers`` row is stamped.  Also the unit
            the suspension detector measures in (a wall-clock gap of more
            than twice this did not happen while we were awake).
        probe_s: how often every route's readiness is re-probed.
        idle_poll_s: how long the loop waits for new work when the queue has
            nothing it can run.
        drain_s: how long in-flight calls are given to finish after a stop is
            requested, before they are interrupted.
        stop_wait_s: how long ``stop`` waits for the lock to come free.
            Longer than *drain_s* on purpose -- the daemon is allowed its
            whole drain window plus the time to tidy up.
        start_wait_s: how long ``start`` waits for its detached child to take
            the lock before reporting that it did not.
        restart_delay_s: how long the controller waits after noticing a queue
            worker died UNEXPECTEDLY before it restarts that worker.  A
            daemon being stopped deliberately never waits this out -- see
            :meth:`CouncilDaemon._supervise_tick`.
        supervise_poll_s: how often the controller reconciles each queue's
            desired state (dead-child restart, a per-queue stop marker)
            against reality.  Independent of *idle_poll_s*, which governs one
            worker's own claim loop.
    """

    heartbeat_s: float = 30.0
    probe_s: float = 60.0
    idle_poll_s: float = 10.0
    drain_s: float = 120.0
    stop_wait_s: float = 130.0
    start_wait_s: float = 30.0
    restart_delay_s: float = 5.0
    supervise_poll_s: float = 2.0

    def scaled(self, **overrides: float) -> "DaemonTimings":
        """This record with some fields replaced."""
        return replace(self, **overrides)


#: The specified production clocks.  ``kbu council daemon`` uses these; only a
#: test passes anything else.
DEFAULT_TIMINGS = DaemonTimings()


@dataclass(frozen=True)
class _Route:
    """One ``(endpoint, served model)`` pair, in the shape a probe reads.

    :func:`~.servable.seat_state` is written against a ``SeatSpec``, but a
    daemon's routes do not all come from a roster: most come from CALL ROWS,
    which carry the endpoint, its kind and the served and canonical model but
    no seat object.  This is the subset of the seat interface that function
    actually reads, so one readiness implementation serves both sources --
    the alternative (reconstructing a ``SeatSpec`` from a call row) would
    invent generation parameters nobody probed.
    """

    endpoint: str
    endpoint_kind: str
    model: str
    canonical_model: str = ""
    seat_name: str = ""
    verified: bool = True

    @property
    def key(self) -> Tuple[str, str]:
        return (self.endpoint, self.model)


@dataclass
class _QueueChild:
    """The controller's own record of one ``(queue, index)`` worker process.

    Not the ``workers`` row -- that is the child's OWN registration, written
    from inside its own process once it is actually claiming.  This is the
    CONTROLLER's bookkeeping of the OS process it spawned, which is what lets
    it tell an unexpected death from a deliberate stop.
    """

    queue: str
    index: int
    process: Any
    args: Tuple[Any, ...] = field(default_factory=tuple)
    #: Set once the controller itself has stopped this child (a per-queue
    #: stop, or the final drain-everything on the controller's own SIGTERM).
    #: While true, :meth:`CouncilDaemon._supervise_tick` never restarts it.
    stopped: bool = False
    #: Monotonic time this child was FIRST observed dead since its last
    #: (re)start, or ``None`` while it is alive.  Drives the restart delay.
    dead_since: Optional[float] = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _run_loop(coro: Any) -> Any:
    """Run *coro* on a loop this call owns, and close it.

    Deliberately not ``asyncio.run``: that sets the thread's current event
    loop to ``None`` on the way out, and other modules in this repository
    still call ``asyncio.get_event_loop()``.  Same reasoning as
    ``CouncilUtils._run_sync``'s no-loop branch.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _route_of(call: Mapping[str, Any]) -> _Route:
    """The route one call row needs."""
    return _Route(
        endpoint=str(call["endpoint"]),
        endpoint_kind=str(call.get("endpoint_kind") or ""),
        model=str(call.get("served_model") or ""),
        canonical_model=str(call.get("canonical_model") or ""),
        seat_name=str(call.get("seat_name") or ""),
    )


def _serve_queue_child(
    council_config_source: Any,
    store_url_override: Optional[str],
    queue: str,
    index: int,
    timings: DaemonTimings,
) -> Dict[str, Any]:
    """Entry point for ONE queue worker, run in its own ``spawn``ed process.

    The controller's per-queue children are launched through
    :mod:`multiprocessing` (``spawn``) rather than through another
    ``subprocess.Popen`` of the shipped CLI argv: a spawned interpreter
    shares nothing with its parent, which is exactly the isolation a second
    OS process is for (Q1: the stdin stall this whole redesign exists to fix
    was store I/O blocking the loop that fed a CLI subprocess), and it needs
    no new command-line surface to carry the queue name and worker index --
    they are plain, picklable arguments.

    Rebuilds the council facade fresh from *council_config_source* (a file
    path for any real deployment; a plain mapping is also accepted here,
    since ``multiprocessing`` pickles it across the spawn boundary rather
    than needing to spell it on a command line the way the top-level
    detached spawn does), then runs exactly :meth:`CouncilDaemon._serve`,
    scoped to this one ``(queue, index)`` partition.
    """
    from kbutillib.domains.ai.council_utils import CouncilUtils

    council = CouncilUtils(
        council_config=council_config_source,
        store_url=store_url_override,
        config_file=False,
        token_file=None,
        kbase_token_file=None,
    )
    daemon = CouncilDaemon(council, timings=timings, queue=queue, worker_index=index)
    return daemon.run_foreground()


class _DaemonWorker(CouncilWorker):
    """A :class:`CouncilWorker` that hands back a call its ROUTE may not run.

    ``claim_calls`` filters by endpoint, which is the only granularity the
    queue has: the claim is one atomic UPDATE and a join onto this machine's
    readiness is not something SQL can express.  The daemon's readiness is per
    ``(endpoint, served model)``, so an endpoint serving three models of which
    one has gone can still legitimately be claimed for -- and the call for the
    missing model must be handed straight back rather than spent.

    This is the same mechanism the base class already uses to scope a run to
    one job (see ``CouncilWorker._claim``), applied to a second filter.
    """

    def __init__(
        self,
        *args: Any,
        admits: Any,
        route_wait_s: float,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        #: ``(row) -> (admitted, why not)``, supplied by the daemon so the
        #: readiness map stays in one place and is re-probed on one clock.
        self._admits = admits
        self._route_wait_s = float(route_wait_s)

    def _claim(
        self, limit: int, job_id: Optional[str], summary: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        rows = super()._claim(limit, job_id, summary)
        kept: List[Dict[str, Any]] = []
        for row in rows:
            admitted, why = self._admits(row)
            if admitted:
                kept.append(row)
                continue
            # The base class has already counted this row against its
            # endpoint's concurrency budget, so give the slot back before
            # releasing the row -- otherwise the budget leaks one slot per
            # rejected claim and the daemon slowly stops claiming anything.
            self._limit_by_endpoint[row["endpoint"]].assigned = max(
                0, self._limit_by_endpoint[row["endpoint"]].assigned - 1
            )
            self._return_unrun(row, why)
            summary["released"] += 1
        return kept

    def _return_unrun(self, row: Mapping[str, Any], why: str) -> None:
        """Put a claimed-but-unrunnable call back, deferred to the next probe.

        ``increment_attempt=False``: nothing is wrong with the call, and the
        route may well be back by the time it is due again.  The ``not_before``
        is the probe interval rather than zero, because a row released as due
        is re-claimed on the next pass and the loop would spin at the speed of
        SQLite.  The REASON is not written here -- the probe pass that decided
        the route was unusable wrote it onto the row already, and writing it
        twice would mean two different texts for one fact.
        """
        try:
            self.store.release_call(
                row["call_id"],
                row["claim_token"],
                _utcnow() + timedelta(seconds=self._route_wait_s),
                increment_attempt=False,
            )
        except StaleClaim:  # pragma: no cover - reclaimed under us
            logger.debug("council daemon: %s was reclaimed before release", row["call_id"])
        logger.debug(
            "council daemon: handed back %s (%s/%s): %s",
            row["call_id"],
            row["endpoint"],
            row.get("served_model"),
            why,
        )


class CouncilDaemon:
    """The per-machine council daemon.

    Args:
        council: a :class:`~kbutillib.domains.ai.council_utils.CouncilUtils`.
            Duck-typed rather than imported, because the facade imports this
            package: the daemon needs ``config()``, ``require_schema()``,
            ``store_url()``, ``user()``, ``endpoint_api()`` and
            ``execution_lock()``, and taking them structurally keeps the
            import graph one-directional.
        timings: every clock the daemon runs on.  Defaults to
            :data:`DEFAULT_TIMINGS`, the specified production values.
        child_argv: the command ``start`` spawns.  Defaults to this
            interpreter running ``kbu council ... daemon start --foreground``
            with the configuration and store this object was built with.
        profiles: which configured profiles' seats to keep probed even when no
            job needs them, so ``status`` and the first submit of the day do
            not wait on a cold probe.  Defaults to every configured profile.
        queue: when given, THIS object is one QUEUE WORKER -- it serves only
            the named queue's seats/roles/concurrency and takes that queue's
            partition lock, rather than the store-wide lock.  ``None`` (the
            default) is every pre-queue-mode caller's shape: unchanged.
        worker_index: this worker's index within *queue* (``0..workers-1``).
            Meaningless without *queue*; defaults to ``0`` when *queue* is
            given and this is omitted.
        queue_child_target: the callable the CONTROLLER spawns one per
            ``(queue, index)`` via :mod:`multiprocessing`.  Defaults to the
            module-level :func:`_serve_queue_child`.  Overridable so a test
            can register its scripted api type inside each spawned child
            before delegating to the real one -- a spawned interpreter
            shares no process-global state with the test that started it.
    """

    def __init__(
        self,
        council: Any,
        timings: Optional[DaemonTimings] = None,
        *,
        child_argv: Optional[Sequence[str]] = None,
        profiles: Optional[Sequence[str]] = None,
        worker_id: Optional[str] = None,
        queue: Optional[str] = None,
        worker_index: Optional[int] = None,
        queue_child_target: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.council = council
        self.timings = timings or DEFAULT_TIMINGS
        self.child_argv = list(child_argv) if child_argv else None
        self._profiles = list(profiles) if profiles is not None else None
        self.worker_id = worker_id or f"{socket.gethostname()}-daemon-{uuid.uuid4().hex[:8]}"
        #: Non-``None`` means this object IS one queue worker (see the class
        #: docstring).  Read only by :meth:`run_foreground`/:meth:`status`;
        #: everything else keeps working unscoped exactly as before.
        self.queue: Optional[str] = queue
        self.worker_index: Optional[int] = worker_index if queue is not None else None
        self._queue_child_target = queue_child_target

        #: ``{(endpoint, model): seat_state report}`` as of the last probe.
        self.readiness: Dict[Tuple[str, str], Dict[str, Any]] = {}
        #: The endpoints this daemon currently claims calls for.
        self.served: Set[str] = set()
        #: ``{endpoint: halt_kind}`` as last seen, so a NEW halt is logged once
        #: and a CLEARED one forces an immediate re-probe.
        self._halts: Dict[str, Optional[str]] = {}
        #: How many times a wall-clock gap looked like a suspend/resume.
        self.resumes = 0
        #: Clock sources, as attributes so a test can make time jump without
        #: suspending the machine it runs on.
        self._wall = time.time
        self._mono = time.monotonic
        self._last_wall: Optional[float] = None
        self._last_mono: Optional[float] = None
        self._worker: Optional[_DaemonWorker] = None
        #: This worker's own seat/role/concurrency partition, set by
        #: :meth:`_run_as_queue_worker` once the named queue's config is
        #: resolved.  ``None`` claims across every seat and role and uses the
        #: account's full capacity -- unchanged from before queue mode, and
        #: what a legacy/controller-less daemon always uses.
        self._scope_seats: Optional[Set[str]] = None
        self._scope_roles: Optional[Set[str]] = None
        self._scope_concurrency: Optional[int] = None
        #: Set by the signal handler.  A plain threading event rather than an
        #: ``asyncio.Event`` because the handler is installed before the loop
        #: exists (see :meth:`run_foreground`).
        self._stop_flag = threading.Event()
        self._loop: Optional[Any] = None
        self._async_stop: Optional[asyncio.Event] = None
        #: The controller's live children, ``(queue, index) -> _QueueChild``.
        #: Populated only inside the controller's own process, by
        #: :meth:`_serve_controller`; a fresh "operator-side" object (what
        #: ``status()``/``stop(queue=...)`` are normally called on) never
        #: has this, which is why those read the filesystem instead.
        self._controller_children: Dict[Tuple[str, int], _QueueChild] = {}

    # -- where the daemon lives -------------------------------------------

    @property
    def lock(self) -> ExecutionLock:
        """The execution lock guarding this store.  The facade's, not a second one."""
        return self.council.execution_lock()

    @property
    def directory(self) -> Path:
        """The council directory: where the lock, the log and the store live."""
        return self.lock.directory

    @property
    def log_path(self) -> Path:
        return self.directory / LOG_FILENAME

    # -- status ------------------------------------------------------------

    def status(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Who holds the lock, and what this store's ``workers`` rows say.

        Reads, and never writes or creates: ``status`` on a machine that has
        never run a council must not be the thing that creates its directory.

        A worker row is ``live`` when its heartbeat is under two minutes old
        and ``stale`` otherwise.  Stale rows are REPORTED rather than hidden:
        a row left by a daemon the kernel killed is the evidence that it was
        killed, and the lock being free is what says nothing is running now.
        """
        now = now or _utcnow()
        holder = self.lock.probe()
        rows: List[Dict[str, Any]] = []
        try:
            store = self.council.require_schema()
        except Exception as exc:  # noqa: BLE001 - status reports, never raises
            logger.debug("council daemon status: store unreadable: %s", exc)
            store = None
        if store is not None:
            for row in store.live_workers(None, now, timedelta(days=36500)):
                beat = row.get("heartbeat_at")
                age = (now - beat).total_seconds() if beat else None
                rows.append(
                    {
                        **dict(row),
                        "heartbeat_age_s": None if age is None else round(age, 1),
                        "state": "live" if age is not None and age < 120 else "stale",
                    }
                )
        result: Dict[str, Any] = {
            "store": self.council.store_url(),
            "directory": str(self.directory),
            "lock": str(self.lock.path),
            "log": str(self.log_path),
            "running": holder is not None,
            "holder": holder,
            "workers": rows,
        }
        queues = self._queues_config()
        if queues:
            result["controller"] = self._probe_controller()
            result["queues"] = {
                name: self._queue_status(name, qcfg, store, rows)
                for name, qcfg in sorted(queues.items())
            }
        return result

    def _queue_status(
        self,
        name: str,
        qcfg: Any,
        store: Any,
        worker_rows: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """One queue's entry in :meth:`status`: its children, and its seats'
        claimed/in-flight/pending counts -- so "nothing is moving" names
        WHICH queue, and a dead child is distinguishable from an idle one.

        ``claimed`` and ``in_flight`` are reported as the SAME number: the
        store has no status distinct from ``claimed`` for "an attempt is
        actively running" (only ``attempts`` rows, which are per TRY, not per
        outstanding call), so there is no second, more specific count to
        give without inventing one.  Both keys are kept so a reader looking
        for either name finds it.
        """
        children: List[Dict[str, Any]] = []
        pids: List[int] = []
        stopped = self._stop_marker_path(name).exists()
        for index in range(qcfg.workers):
            holder = self._probe_partition(name, index)
            pid = holder.get("pid") if holder else None
            alive = bool(isinstance(pid, int) and _alive(pid))
            children.append(
                {"queue": name, "index": index, "pid": pid, "alive": alive, "stopped": stopped}
            )
            if isinstance(pid, int):
                pids.append(pid)

        last_heartbeat = None
        for row in worker_rows:
            if row.get("pid") in pids:
                beat = row.get("heartbeat_at")
                if beat is not None and (last_heartbeat is None or beat > last_heartbeat):
                    last_heartbeat = beat

        seats = set(qcfg.seats)
        pending = 0
        claimed = 0
        if store is not None:
            try:
                for job in store.list_jobs():
                    if job.get("status") in ("done", "cancelled"):
                        continue
                    for call in store.get_calls(job["job_id"]):
                        if not call.get("live", True):
                            continue
                        if call.get("seat_name") not in seats:
                            continue
                        status_ = call.get("status")
                        if status_ == "pending":
                            pending += 1
                        elif status_ == "claimed":
                            claimed += 1
            except Exception as exc:  # noqa: BLE001 - status reports, never raises
                logger.debug("council daemon status: queue %s calls unreadable: %s", name, exc)
        return {
            "seats": sorted(seats),
            "children": children,
            "pending": pending,
            "claimed": claimed,
            "in_flight": claimed,
            "last_heartbeat": str(last_heartbeat) if last_heartbeat is not None else None,
        }

    # -- start -------------------------------------------------------------

    def start(self, foreground: bool = False, queue: Optional[str] = None) -> Dict[str, Any]:
        """Start the daemon.  Detached by default; in-process with *foreground*.

        The detached form spawns ONE child in a session of its own
        (``start_new_session=True``, so no terminal hangup and no controlling
        tty reaches it) with stdout and stderr appended to
        :data:`LOG_FILENAME`, and then waits until that child is the lock's
        holder.  Waiting is the point: a ``start`` that returned as soon as
        ``Popen`` did would report success for a child that was about to die
        of a configuration error, and the user would learn that from an empty
        ``status`` minutes later.

        When the configuration declares ``queues``, this child becomes a
        CONTROLLER rather than a lone worker: it holds the ``controller``
        lock and launches one process per configured queue (see
        :meth:`run_foreground`), so the readiness this method waits for is
        the CONTROLLER lock rather than the store-wide one.

        Args:
            foreground: run in this process rather than spawning a detached
                child.
            queue: start only this ALREADY-CONFIGURED queue against an
                ALREADY-RUNNING controller (a per-queue restart after
                :meth:`stop` was called with a queue name).  Mutually
                exclusive with *foreground*; every other case starts (or
                restarts) everything.

        Raises:
            LockHeld: another council process already holds the lock.  The
                message names its kind, pid and start time.
            DaemonError: the child exited, or never took the lock; or
                *queue* names an unconfigured queue or no controller is up.
        """
        if queue is not None:
            return self._start_one_queue(queue)
        if foreground:
            return self.run_foreground()

        queues = self._queues_config()
        if queues:
            holder = self._probe_controller() or self.lock.probe()
        else:
            holder = self.lock.probe()
        if holder is not None:
            raise LockHeld(self.lock.held_message("daemon", holder), holder)

        self.directory.mkdir(parents=True, exist_ok=True)
        # A fresh controller incarnation starts every configured queue: any
        # per-queue stop marker left by a PREVIOUS controller is cleared here
        # rather than inside stop() itself, so "start everything" always
        # means everything (point 8's "no queues -> today's behaviour" and
        # the all-or-nothing initial start both assume a clean slate).
        for name in queues:
            self._stop_marker_path(name).unlink(missing_ok=True)
        argv = list(self.child_argv or self._default_child_argv())
        with open(self.log_path, "ab") as log:
            log.write(
                f"\n=== council daemon starting {_utcnow().isoformat()} "
                f"argv={argv} ===\n".encode("utf-8")
            )
            log.flush()
            child = subprocess.Popen(  # noqa: S603 - argv is built here, never user text
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                cwd=str(self.directory),
                start_new_session=True,
                close_fds=True,
            )

        deadline = self._mono() + self.timings.start_wait_s
        while self._mono() < deadline:
            holder = self._probe_controller() if queues else self.lock.probe()
            if holder is not None and holder.get("pid") == child.pid:
                return {
                    "started": True,
                    "pid": child.pid,
                    "foreground": False,
                    "lock": str(self.directory / CONTROLLER_LOCK_FILENAME) if queues else str(self.lock.path),
                    "log": str(self.log_path),
                    "holder": holder,
                    "queues": sorted(queues) or None,
                }
            code = child.poll()
            if code is not None:
                raise DaemonError(
                    f"the council daemon child (pid {child.pid}) exited {code} "
                    f"before it took the execution lock. The last lines of "
                    f"{self.log_path}:\n{self._log_tail()}"
                )
            time.sleep(0.05)

        holder = self._probe_controller() if queues else self.lock.probe()
        child.terminate()
        raise DaemonError(
            f"the council daemon child (pid {child.pid}) did not take the "
            f"execution lock within {self.timings.start_wait_s:g}s"
            + (f"; it is held by {holder}" if holder else "")
            + f". The last lines of {self.log_path}:\n{self._log_tail()}"
        )

    def _default_child_argv(self) -> List[str]:
        """``kbu council [--config C] [--store S] daemon start --foreground``.

        Spelled as ``<this interpreter> -m kbutillib`` rather than as ``kbu``
        so the child runs in the SAME environment as the parent: a venv whose
        ``bin`` is not on the daemon's ``PATH`` is the normal case on this
        fleet, and resolving ``kbu`` off ``PATH`` is how a daemon ends up
        running a different installation of the library than the one that
        started it.
        """
        argv = [sys.executable, "-m", "kbutillib", "council"]
        source = getattr(self.council, "council_config_source", None)
        if isinstance(source, Mapping):
            # An in-memory configuration cannot be handed to another process,
            # and spawning a child that would silently load a DIFFERENT
            # configuration -- the packaged default -- is the worst available
            # answer: it would probe endpoints nobody asked for and work a
            # store nobody named.  Refused rather than approximated.
            raise DaemonError(
                "this council was built from an in-memory configuration, which "
                "cannot be passed to a detached child: the child would load the "
                "packaged default instead and work a different store. Use "
                "run_foreground() (`kbu council daemon start --foreground`), or "
                "build the council from a configuration FILE."
            )
        if isinstance(source, (str, Path)):
            argv += ["--config", str(source)]
        override = getattr(self.council, "store_url_override", None)
        if override:
            argv += ["--store", str(override)]
        return argv + ["daemon", "start", "--foreground"]

    def _log_tail(self, lines: int = 20) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(no log)"
        return "\n".join(text.splitlines()[-lines:]) or "(empty log)"

    # -- stop --------------------------------------------------------------

    def stop(self, queue: Optional[str] = None) -> Dict[str, Any]:
        """Stop the daemon holding this store's lock, and wait for it to go.

        Signals ONLY a holder whose recorded kind is ``daemon`` (review round
        3).  A ``kbu council run`` in somebody's terminal and a ``db migrate``
        halfway through dropping a table are also lock holders, and
        ``SIGTERM``-ing either because they were in the way is not this verb's
        business -- so it says what is running and stops.

        Args:
            queue: stop only this queue's children, draining and stopping
                them and leaving every sibling queue running.  ``None`` (the
                default) stops everything -- the whole controller, or the
                legacy single daemon, exactly as before queue mode existed.
        """
        if queue is not None:
            return self._stop_one_queue(queue)
        if self._queues_config():
            return self._stop_controller()
        holder = self.lock.probe()
        if holder is None:
            return {
                "stopped": False,
                "running": False,
                "detail": (
                    f"no council process holds {str(self.lock.path)!r}; "
                    "no daemon is running against this store"
                ),
            }
        kind = str(holder.get("kind") or "unknown")
        pid = holder.get("pid")
        if kind != "daemon":
            raise DaemonError(
                f"the execution lock {str(self.lock.path)!r} is held by a council "
                f"{kind} (pid {pid}), not a daemon. `daemon stop` only signals a "
                f"daemon; stop the {kind} where it is running, or wait for it."
            )
        if not isinstance(pid, int) or pid <= 0:
            raise DaemonError(
                f"a council daemon holds {str(self.lock.path)!r} but its record "
                f"names no usable pid ({pid!r}); it cannot be signalled. Its "
                "lock is released when the process dies."
            )
        if not _alive(pid):
            # The kernel frees an flock when its holder dies, so this is a
            # narrow race rather than a stale-lease situation: the process
            # went between the probe and now.
            return {
                "stopped": False,
                "running": False,
                "pid": pid,
                "detail": f"pid {pid} is no longer running; its lock is already free",
            }

        os.kill(pid, signal.SIGTERM)
        deadline = self._mono() + self.timings.stop_wait_s
        while self._mono() < deadline:
            if self.lock.probe() is None:
                _reap(pid)
                return {
                    "stopped": True,
                    "running": False,
                    "pid": pid,
                    "detail": f"sent SIGTERM to pid {pid}; the lock is free",
                }
            time.sleep(0.05)
        return {
            "stopped": False,
            "running": True,
            "pid": pid,
            "detail": (
                f"sent SIGTERM to pid {pid} but the lock was still held after "
                f"{self.timings.stop_wait_s:g}s; it may be draining a long call. "
                "Check `kbu council daemon status`."
            ),
        }

    def _stop_controller(self) -> Dict[str, Any]:
        """Stop the WHOLE controller: every queue's children, then itself.

        Exactly :meth:`stop`'s shape, one level up: find the controller's
        pid (via its OWN lock file, not the store-wide one -- a controller
        holds the store-wide lock SHARED, so that file's single-holder
        record reads ``unknown``), signal it, and wait for the controller
        lock to come free.  The controller's own SIGTERM handler is
        :meth:`request_stop`, shared with every other kind -- it drains and
        stops every child itself (see :meth:`_serve_controller`) before
        releasing its lock.
        """
        path = self.directory / CONTROLLER_LOCK_FILENAME
        holder = self._probe_controller()
        if holder is None:
            return {
                "stopped": False,
                "running": False,
                "detail": (
                    f"no council controller holds {str(path)!r}; "
                    "no daemon is running against this store"
                ),
            }
        pid = holder.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            raise DaemonError(
                f"a council controller holds {str(path)!r} but its record names "
                f"no usable pid ({pid!r}); it cannot be signalled. Its lock is "
                "released when the process dies."
            )
        if not _alive(pid):
            return {
                "stopped": False,
                "running": False,
                "pid": pid,
                "detail": f"pid {pid} is no longer running; its lock is already free",
            }
        os.kill(pid, signal.SIGTERM)
        deadline = self._mono() + self.timings.stop_wait_s
        while self._mono() < deadline:
            if self._probe_controller() is None:
                _reap(pid)
                return {
                    "stopped": True,
                    "running": False,
                    "pid": pid,
                    "detail": f"sent SIGTERM to pid {pid}; the lock is free",
                }
            time.sleep(0.05)
        return {
            "stopped": False,
            "running": True,
            "pid": pid,
            "detail": (
                f"sent SIGTERM to pid {pid} but the lock was still held after "
                f"{self.timings.stop_wait_s:g}s; it may be draining a long call. "
                "Check `kbu council daemon status`."
            ),
        }

    def _stop_one_queue(self, queue: str) -> Dict[str, Any]:
        """Stop only *queue*'s children; every sibling queue keeps running.

        Writes the "deliberately stopped" marker FIRST, before signalling
        anything: the controller's own supervision loop (see
        :meth:`_supervise_tick`) checks this file to tell an operator's stop
        from a crash, and a child that died in the gap between the signal
        and the marker existing would otherwise be restarted out from under
        this call.  Then each live child for *queue* is signalled directly
        (found via its OWN partition lock, exactly as :meth:`stop` finds the
        whole daemon's pid via the store-wide one) -- the controller does not
        need to be asked to relay this; each child already drains and
        interrupts on its own ``SIGTERM``, unchanged from a lone daemon's.
        """
        queues = self._queues_config()
        qcfg = queues.get(queue)
        if qcfg is None:
            raise DaemonError(f"unknown queue {queue!r}; configured: {sorted(queues)}")
        controller = self._probe_controller()
        if controller is None:
            return {
                "stopped": False,
                "running": False,
                "queue": queue,
                "detail": (
                    "no council controller is running against this store; "
                    f"queue {queue!r} is not running"
                ),
            }
        marker = self._stop_marker_path(queue)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        pids: List[int] = []
        for index in range(qcfg.workers):
            holder = self._probe_partition(queue, index)
            pid = holder.get("pid") if holder else None
            if isinstance(pid, int) and _alive(pid):
                pids.append(pid)
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        deadline = self._mono() + self.timings.stop_wait_s
        while self._mono() < deadline:
            if all(self._probe_partition(queue, i) is None for i in range(qcfg.workers)):
                for pid in pids:
                    _reap(pid)
                return {
                    "stopped": True,
                    "running": False,
                    "queue": queue,
                    "detail": f"queue {queue!r} drained and stopped",
                }
            time.sleep(0.05)
        return {
            "stopped": False,
            "running": True,
            "queue": queue,
            "detail": (
                f"queue {queue!r} was signalled but did not fully stop within "
                f"{self.timings.stop_wait_s:g}s; it may be draining a long call."
            ),
        }

    def _start_one_queue(self, queue: str) -> Dict[str, Any]:
        """Re-enable *queue* after a deliberate :meth:`stop`.

        Clears the stop marker and waits for the controller's own
        supervision loop (:meth:`_supervise_tick`, polling every
        ``supervise_poll_s``) to notice and relaunch that queue's children --
        this call never spawns a process itself, because only the controller
        may (judgement call (e): a second launcher could hold a non-
        conflicting partition lock under a DIFFERENT config and jointly
        over-commit the account the lock cannot see).
        """
        queues = self._queues_config()
        qcfg = queues.get(queue)
        if qcfg is None:
            raise DaemonError(f"unknown queue {queue!r}; configured: {sorted(queues)}")
        controller = self._probe_controller()
        if controller is None:
            raise DaemonError(
                "no council controller is running against this store; start the "
                f"daemon first (`daemon start`) before starting queue {queue!r}"
            )
        self._stop_marker_path(queue).unlink(missing_ok=True)
        deadline = self._mono() + self.timings.start_wait_s
        while self._mono() < deadline:
            holders = [self._probe_partition(queue, i) for i in range(qcfg.workers)]
            if all(
                h is not None and isinstance(h.get("pid"), int) and _alive(h["pid"])
                for h in holders
            ):
                return {"started": True, "queue": queue, "detail": f"queue {queue!r} is running"}
            time.sleep(0.05)
        raise DaemonError(
            f"queue {queue!r} did not come up within {self.timings.start_wait_s:g}s of "
            "clearing its stop marker; is the controller still running? Check "
            "`kbu council daemon status`."
        )

    # -- queue-mode plumbing: config, markers and lock probes ---------------

    def _queues_config(self) -> Dict[str, Any]:
        """``{name: QueueConfig}``, or ``{}`` for an unreadable/unpartitioned
        configuration.  Never raises: every caller treats "no queues" and "the
        configuration could not say" identically -- both mean "behave exactly
        as today"."""
        try:
            return dict(self.council.config().queues or {})
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.debug("council daemon: configuration unreadable: %s", exc)
            return {}

    def _stop_marker_path(self, queue: str) -> Path:
        """The file whose PRESENCE means "this queue was deliberately
        stopped; do not auto-restart it" (see :meth:`_supervise_tick`).

        Scoped to one controller incarnation: a fresh :meth:`start` (with no
        queue named) clears every marker before spawning the controller, so
        a stop this operator forgot about from a previous run never silently
        disables a queue forever.
        """
        return self.directory / f"queue-{queue}.stopped"

    @staticmethod
    def _probe_lock_file(path: Path) -> Optional[Dict[str, Any]]:
        """Who holds *path* (a controller or partition lock file), or ``None``.

        The same try-the-flock technique :class:`~.execlock.ExecutionLock`
        uses for the store-wide lock (never trust a stale record without
        confirming the kernel still enforces it), duplicated here rather than
        called into that module: ``execlock.py`` exposes no public probe for
        an individual partition or the controller lock, only for the
        store-wide file, and this module does not extend that one's surface.
        """
        if not path.exists():
            return None
        try:
            handle = open(path, "r+", encoding="utf-8")
        except OSError:
            return None
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                try:
                    text = path.read_text(encoding="utf-8").strip()
                    if text:
                        parsed = json.loads(text.splitlines()[0])
                        if isinstance(parsed, dict):
                            return parsed
                except (OSError, ValueError, IndexError):
                    pass
                return {"kind": "unknown", "pid": None}
            # Acquired it ourselves: nobody holds it.  Release at once.
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return None
        finally:
            handle.close()

    def _probe_controller(self) -> Optional[Dict[str, Any]]:
        """Who holds the controller lock, or ``None``."""
        return self._probe_lock_file(self.directory / CONTROLLER_LOCK_FILENAME)

    def _probe_partition(self, queue: str, index: int) -> Optional[Dict[str, Any]]:
        """Who holds ``(queue, index)``'s own partition lock, or ``None``.

        The filename convention (``council-<queue>-<index>.lock``) is
        :class:`~.execlock.ExecutionLock`'s, documented in its module
        docstring as the partition lock's naming scheme; reproduced here
        rather than imported because the class exposes no accessor for it.
        """
        path = self.directory / f"council-{queue}-{index}.lock"
        return self._probe_lock_file(path)

    # -- the foreground loop -----------------------------------------------

    def run_foreground(self) -> Dict[str, Any]:
        """Hold the lock and serve this store until ``SIGTERM``/``SIGINT``.

        This is the daemon.  ``start`` without ``--foreground`` is a wrapper
        that spawns a child which calls exactly this, and ``--foreground`` is
        what a systemd unit, a launchd job, ``tmux`` or ``nohup`` runs -- the
        daemon does not care which, because process supervision is the
        machine's job and differs on every machine in this fleet.  The SAME
        rule holds whether this becomes a lone worker, a controller or one
        queue worker beneath a controller (confront round 1 (d)): the three
        paths share this one entry point and diverge only on what they
        acquire and serve.

        Three shapes, decided here:

        * ``self.queue`` set -- THIS object is one queue worker (spawned by a
          controller's :meth:`_launch_queue_child`, or built directly by a
          caller that wants one partition in isolation).  Takes that
          partition's lock and serves only its seats.
        * the configuration declares ``queues`` -- a CONTROLLER: takes the
          ``controller`` lock and launches/supervises one child per
          configured queue (see :meth:`_serve_controller`).
        * neither -- exactly the pre-queue-mode daemon: the store-wide
          ``daemon`` lock, serving every seat.  Unchanged.

        Raises:
            LockHeld: another council process holds a lock this needs.
        """
        # The handlers go on FIRST, before the lock and before the loop.  A
        # ``SIGTERM`` in the window between taking the lock and installing
        # them would get the default action -- death -- and a daemon that dies
        # with the lock held and calls claimed is exactly what the drain and
        # the interrupt pass exist to prevent.  The handler sets a plain
        # threading flag until there is a loop to wake; :meth:`_serve` reads
        # that flag on the way in, so a signal that arrived early is honoured
        # rather than lost.
        self._stop_flag.clear()
        with _signals(self):
            if self.queue is not None:
                return self._run_as_queue_worker()

            queues = self._queues_config()
            if not queues:
                with self.lock.acquire("daemon") as record:
                    # Before ANYTHING is served: both CLI backends read
                    # project context out of the cwd, and a daemon inheriting
                    # the directory it was launched from would answer
                    # council questions with somebody's repository in scope.
                    # The council directory holds the store, the lock and
                    # the log and nothing a CLI would read as context.
                    os.chdir(self.directory)
                    return _run_loop(self._serve(record))

            with self.lock.acquire("controller") as record:
                os.chdir(self.directory)
                return _run_loop(self._serve_controller(record, queues))

    def _run_as_queue_worker(self) -> Dict[str, Any]:
        """Serve exactly ``self.queue``'s seats, holding its partition lock.

        Reuses :meth:`_serve` unchanged: the only difference from the
        unpartitioned daemon is that :attr:`_scope_seats`/``_scope_roles``/
        ``_scope_concurrency`` are set first, which :meth:`_worker_for`,
        :meth:`_pending_calls` and :meth:`_profile_routes` all already read
        (``None`` there, which is what every pre-queue-mode caller leaves
        them at, means "unscoped" exactly as before).
        """
        queues = self._queues_config()
        qcfg = queues.get(self.queue)
        if qcfg is None:
            raise DaemonError(f"unknown queue {self.queue!r}; configured: {sorted(queues)}")
        index = self.worker_index if self.worker_index is not None else 0
        if not (0 <= index < qcfg.workers):
            raise DaemonError(
                f"worker index {index} is out of range for queue {self.queue!r} "
                f"(workers={qcfg.workers})"
            )
        self._scope_seats = set(qcfg.seats)
        self._scope_roles = set(qcfg.roles) if qcfg.roles else None
        self._scope_concurrency = int(qcfg.concurrency)
        with self.lock.acquire("daemon", partition=(self.queue, index)) as record:
            os.chdir(self.directory)
            return _run_loop(self._serve(record))

    # -- the controller ------------------------------------------------------

    async def _serve_controller(
        self, record: Mapping[str, Any], queues: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Launch every configured queue's children, then supervise them.

        A fresh controller incarnation always starts with a clean slate
        (every per-queue stop marker is cleared by :meth:`start` before this
        child is even spawned), so the launch below is unconditionally
        ALL-OR-NOTHING (confront (c)): if any child fails to take its
        partition lock within ``start_wait_s``, every child already started
        by THIS call is drained and stopped and the whole controller raises,
        rather than leaving some queues consuming work and others not.

        Thereafter this is a plain reconcile loop: every ``supervise_poll_s``
        it asks each child whether it is alive and whether its queue's stop
        marker says it should be, and acts on the difference (see
        :meth:`_supervise_tick`).  The controller's OWN ``SIGTERM`` (via
        :meth:`request_stop`, the same handler every other kind uses) sets
        the same ``stop`` event this loop watches, at which point it drains
        and stops every child before returning -- preserving the bounded
        drain contract one level up: each CHILD's own ``_serve`` already
        implements it unchanged, this only has to ask them all to stop and
        wait.
        """
        stop = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        self._async_stop = stop
        if self._stop_flag.is_set():
            stop.set()
        summary: Dict[str, Any] = {
            "worker_id": self.worker_id,
            "pid": os.getpid(),
            "store": self.council.store_url(),
            "lock": str(self.directory / CONTROLLER_LOCK_FILENAME),
            "cwd": os.getcwd(),
            "queues": sorted(queues),
            "started_at": str(record.get("started_at") or ""),
        }
        children: Dict[Tuple[str, int], _QueueChild] = {}
        try:
            try:
                for name, qcfg in sorted(queues.items()):
                    for index in range(qcfg.workers):
                        children[(name, index)] = await self._launch_queue_child(name, index)
                await self._await_all_ready(children)
            except Exception:
                await self._drain_and_stop_all(children)
                raise

            self._controller_children = children
            if not stop.is_set():
                await _wait_for(stop, self.timings.supervise_poll_s)
            while not stop.is_set():
                await self._supervise_tick(children)
                await _wait_for(stop, self.timings.supervise_poll_s)
            await self._drain_and_stop_all(children)
        finally:
            self._loop = None
            self._async_stop = None
        return summary

    async def _launch_queue_child(self, queue: str, index: int) -> "_QueueChild":
        """Spawn one ``(queue, index)`` worker via ``multiprocessing`` and
        return its bookkeeping record.  See :func:`_serve_queue_child`."""
        ctx = multiprocessing.get_context("spawn")
        source = getattr(self.council, "council_config_source", None)
        override = getattr(self.council, "store_url_override", None)
        target = self._queue_child_target or _serve_queue_child
        args: Tuple[Any, ...] = (source, override, queue, index, self.timings)
        process = ctx.Process(target=target, args=args, name=f"council-queue-{queue}-{index}")
        process.start()
        return _QueueChild(queue=queue, index=index, process=process, args=args)

    async def _await_all_ready(self, children: Dict[Tuple[str, int], "_QueueChild"]) -> None:
        """Block until every child in *children* holds its partition lock.

        Raises :class:`DaemonError` naming whichever child(ren) failed --
        either by exiting before taking their lock, or by never taking it
        within ``start_wait_s``.  The caller (:meth:`_serve_controller`)
        drains and stops every child, started or not, on this failure.
        """
        deadline = self._mono() + self.timings.start_wait_s
        pending = set(children)
        while pending and self._mono() < deadline:
            for key in list(pending):
                child = children[key]
                if not child.process.is_alive():
                    raise DaemonError(
                        f"queue worker {child.queue!r}/{child.index} (pid "
                        f"{child.process.pid}) exited {child.process.exitcode} "
                        "before it took its partition lock"
                    )
                holder = self._probe_partition(*key)
                if holder is not None and holder.get("pid") == child.process.pid:
                    pending.discard(key)
            if pending:
                await asyncio.sleep(0.05)
        if pending:
            names = ", ".join(f"{q}/{i}" for q, i in sorted(pending))
            raise DaemonError(
                f"queue worker(s) {names} did not take their partition lock "
                f"within {self.timings.start_wait_s:g}s"
            )

    async def _drain_and_stop_all(self, children: Dict[Tuple[str, int], "_QueueChild"]) -> None:
        """Signal every child and wait (bounded) for them all to go.

        Each child drains and interrupts its OWN claimed calls on this
        ``SIGTERM`` -- unchanged, inherited :meth:`_serve`/:meth:`_shutdown`
        behaviour -- so this only has to ask and wait, never repeat that
        logic.
        """
        for child in children.values():
            self._signal_stop_child(child)
        deadline = self._mono() + self.timings.stop_wait_s
        remaining = {k: c for k, c in children.items() if c.process.is_alive()}
        while remaining and self._mono() < deadline:
            for key in list(remaining):
                if not remaining[key].process.is_alive():
                    remaining[key].process.join(timeout=0.1)
                    del remaining[key]
            if remaining:
                await asyncio.sleep(0.05)
        for child in remaining.values():
            logger.warning(
                "council controller: queue %s/%s (pid %s) did not stop within "
                "%.0fs; killing it",
                child.queue,
                child.index,
                child.process.pid,
                self.timings.stop_wait_s,
            )
            try:
                child.process.kill()
            except Exception:  # noqa: BLE001 - best effort
                pass
            child.process.join(timeout=5.0)

    @staticmethod
    def _signal_stop_child(child: "_QueueChild") -> None:
        if child.process.is_alive() and child.process.pid:
            try:
                os.kill(child.process.pid, signal.SIGTERM)
            except OSError:
                pass

    async def _supervise_tick(self, children: Dict[Tuple[str, int], "_QueueChild"]) -> None:
        """Reconcile every child's desired state against its actual one.

        Per child, per confront (b):

        * stop marker present -- a DELIBERATE per-queue stop.  Signalled if
          still alive; never restarted while the marker stands, however it
          dies.
        * marker absent, alive -- nothing to do.
        * marker absent, dead, and this child was previously marked
          ``stopped`` -- an operator just cleared the marker
          (:meth:`_start_one_queue`): restart AT ONCE, no delay, because the
          5-second delay below is for an UNEXPECTED death, not a requested
          one.
        * marker absent, dead, unexpectedly -- restart after
          ``restart_delay_s``, indefinitely, logging the failure (visible
          in :meth:`status` meanwhile via its dead/absent child entry) and
          triggering an immediate stale-claim reclaim so its orphaned work is
          not stuck behind the full ten-minute lease.
        """
        for key, child in list(children.items()):
            marker = self._stop_marker_path(child.queue)
            desired_stopped = marker.exists()
            alive = child.process.is_alive()
            if desired_stopped:
                if alive:
                    self._signal_stop_child(child)
                child.stopped = True
                child.dead_since = None
                continue
            if alive:
                child.dead_since = None
                continue
            if child.stopped:
                child.stopped = False
                child.dead_since = None
                self._safely_reclaim()
                children[key] = await self._launch_queue_child(child.queue, child.index)
                continue
            now = self._mono()
            if child.dead_since is None:
                child.dead_since = now
                logger.warning(
                    "council controller: queue %s/%s (pid %s) died unexpectedly; "
                    "restarting in %.0fs",
                    child.queue,
                    child.index,
                    child.process.pid,
                    self.timings.restart_delay_s,
                )
                self._safely_reclaim()
                continue
            if now - child.dead_since >= self.timings.restart_delay_s:
                children[key] = await self._launch_queue_child(child.queue, child.index)

    def _safely_reclaim(self) -> None:
        try:
            self.council.require_schema().reclaim_stale()
        except Exception as exc:  # noqa: BLE001 - best effort, never fatal
            logger.debug("council controller: could not reclaim stale claims: %s", exc)

    async def _serve(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        """The loop, with the lock already held and the cwd already moved."""
        store = self.council.require_schema()
        stop = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        self._async_stop = stop
        if self._stop_flag.is_set():
            # A signal arrived before this loop existed.  Honour it: one pass
            # with nothing claimed, then a clean shutdown.
            stop.set()
        summary: Dict[str, Any] = {
            "worker_id": self.worker_id,
            "pid": os.getpid(),
            "store": self.council.store_url(),
            "lock": str(self.lock.path),
            # Reported because "the daemon is not running in the directory it
            # was launched from" is the whole point of the chdir above, and a
            # property nobody can observe is a property nobody can keep.
            "cwd": os.getcwd(),
            "passes": 0,
            "claimed": 0,
            "finished": 0,
            "interrupted": 0,
            "resumes": 0,
            "started_at": str(record.get("started_at") or ""),
        }

        try:
            try:
                last_probe = 0.0
                while not stop.is_set():
                    if self._tick_clock():
                        # A wall-clock gap this big did not happen while we
                        # were awake: the machine suspended.  Reclaim before
                        # the next heartbeat, so a claim this daemon lost to a
                        # sleeping laptop is back in the queue before anything
                        # reports the daemon as healthy again.
                        store.reclaim_stale()
                        summary["resumes"] += 1

                    now = self._mono()
                    if now - last_probe >= self.timings.probe_s or self._halt_cleared(store):
                        await self._refresh_readiness(store)
                        last_probe = now

                    worker = self._worker_for(store)
                    passed = await self._work_once(worker, stop)
                    summary["passes"] += 1
                    summary["claimed"] += int(passed.get("claimed") or 0)
                    summary["finished"] += int(passed.get("finished") or 0)
                    for blocker in passed.get("blockers") or []:
                        logger.debug(
                            "council daemon: blocked: %s %s: %s",
                            blocker.get("kind"),
                            blocker.get("endpoint"),
                            blocker.get("detail"),
                        )
                    if stop.is_set():
                        break
                    store.heartbeat_worker(self.worker_id, _utcnow())
                    await _wait_for(stop, self.timings.idle_poll_s)
            finally:
                summary["interrupted"] = self._shutdown(store)
        finally:
            # The loop is about to be closed, so the handler must stop
            # reaching for it: a ``call_soon_threadsafe`` on a closed loop
            # raises inside a signal handler, where nothing can report it.
            self._loop = None
            self._async_stop = None
        summary["resumes"] = max(int(summary["resumes"]), self.resumes)
        return summary

    async def _work_once(self, worker: _DaemonWorker, stop: asyncio.Event) -> Dict[str, Any]:
        """One ``CouncilWorker.work`` pass, bounded by the stop deadline.

        ``work(until_idle=True)`` returns when the store has nothing this
        daemon can run, which is what makes the daemon a sequence of passes
        rather than one infinite call: between passes the readiness is
        re-probed, a new job's routes are discovered and the heartbeat is
        stamped even when the queue is empty.

        When a stop is requested mid-pass the worker stops claiming and
        gathers what is in flight, so ``work`` returns of its own accord once
        the last in-flight call answers.  The deadline is the bound on THAT:
        at :attr:`DaemonTimings.drain_s` the pass is cancelled, which
        propagates into the api types' own awaits -- a CLI child kills its
        process group on cancellation, and an HTTP request simply stops.
        """
        task = asyncio.ensure_future(worker.work(stop_event=stop, until_idle=True))
        waiter = asyncio.ensure_future(stop.wait())
        try:
            done, _pending = await asyncio.wait(
                {task, waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            if task not in done:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task), timeout=self.timings.drain_s
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "council daemon: in-flight calls did not finish within "
                        "%.0fs of the stop request; interrupting them",
                        self.timings.drain_s,
                    )
                    task.cancel()
                    await self._kill_cli_children()
                    await asyncio.gather(task, return_exceptions=True)
                    return {"claimed": 0, "finished": 0, "blockers": [], "drained": False}
            return dict(task.result())
        finally:
            waiter.cancel()

    @staticmethod
    async def _kill_cli_children() -> int:
        """Kill and reap every CLI child this process still has running.

        Cancelling the task that awaits a CLI child unwinds the coroutine; it
        does not touch the child, which is in a session of its own (that is
        what lets a timeout take its helpers down with it).  So the group is
        killed explicitly -- through ``llm``, the one public wrapper, which is
        what owns those children; no council module reaches into
        ``llm.apis`` itself.
        """
        from .. import llm

        try:
            return int(await llm.kill_cli_children())
        except Exception as exc:  # noqa: BLE001 - the drain must still finish
            # Whatever went wrong here, the interrupt pass that puts the
            # claimed calls back is still owed to the queue, so this is
            # reported and not raised.
            logger.warning("council daemon: could not kill the CLI children: %s", exc)
            return 0

    # -- shutdown ----------------------------------------------------------

    def _shutdown(self, store: Any) -> int:
        """Interrupt what is still claimed, then leave the ``workers`` table.

        Order matters and is the specified one: the calls are released FIRST
        and the worker row is removed afterwards, because ``job_status``
        reports ``no_daemon`` for an endpoint with pending calls and no live
        worker -- removing the row first would, for the length of the
        interrupt pass, make every call this daemon was holding look like work
        nothing is going to run.
        """
        interrupted = self._interrupt_claimed(store)
        try:
            store.remove_worker(self.worker_id)
        except Exception as exc:  # noqa: BLE001 - never block the lock release
            logger.warning("council daemon: could not remove the workers row: %s", exc)
        return interrupted

    def _interrupt_claimed(self, store: Any) -> int:
        """Return every call still claimed by this daemon to the queue.

        Per call, in ONE transaction (review round 3):

        * an ``attempts`` row with outcome ``interrupted`` -- the audit trail
          has to show that the call was made, because the subscription or the
          gateway may well have charged for it;
        * usage KEPT when it is known and marked ``unknown`` when it is not.
          A cancelled call's cost is unknowable: the answer never arrived, so
          no token count came with it;
        * ``budget_usage_unknown`` set on a BUDGETED endpoint whose usage is
          unknown, because a budget that cannot be measured cannot be
          enforced -- the same rule, and the same halt kind, the worker
          applies to a completed call that reported no usage;
        * the claim token invalidated and the call back to pending with its
          retry count UNCHANGED.  Being stopped is not the call's fault.

        The attempt row is filed under a NEGATIVE attempt number (see
        ``SqliteStore.next_uncharged_attempt_no``): the retry count
        deliberately does not move, so ``attempt_count + 1`` is still the
        number the call's next real try will use, and filing this row there
        would make that try's settle transaction fail on a duplicate key --
        losing the answer the call eventually gave.
        """
        rows = self._claimed_by_me(store)
        if not rows:
            return 0
        now = _utcnow()
        done = 0
        for row in rows:
            usage = self._interrupted_usage(row)
            unknown = bool(usage.get("unknown"))
            endpoint = str(row["endpoint"])
            budget = self._budget_of(endpoint)
            started = row.get("claimed_at") or now
            try:
                with store.transaction():
                    store.record_attempt(
                        row["call_id"],
                        self._uncharged_attempt_no(store, row),
                        started,
                        now,
                        INTERRUPTED_OUTCOME,
                        usage=dict(usage),
                        worker_id=self.worker_id,
                    )
                    if budget and unknown:
                        store.set_halt(
                            endpoint,
                            "budget_usage_unknown",
                            f"a call to {endpoint!r} was interrupted by a daemon "
                            f"stop with its usage unmeasured, and the endpoint has "
                            f"a {budget}-token budget; an unmeasurable budget is "
                            "unenforceable. Reconcile the spend and `kbu council "
                            "endpoints reset-halt` to continue.",
                        )
                    store.release_call(
                        row["call_id"],
                        row["claim_token"],
                        None,
                        increment_attempt=False,
                    )
            except StaleClaim:
                # Another worker reclaimed it (the ten-minute stale sweep) --
                # it is already back in the queue, which is the outcome this
                # method exists to produce.
                logger.debug(
                    "council daemon: %s was already reclaimed", row["call_id"]
                )
                continue
            except Exception as exc:  # noqa: BLE001 - one bad row must not strand the rest
                logger.warning(
                    "council daemon: could not interrupt %s: %s", row["call_id"], exc
                )
                continue
            done += 1
        logger.info("council daemon: returned %d interrupted call(s) to the queue", done)
        return done

    def _claimed_by_me(self, store: Any) -> List[Dict[str, Any]]:
        """Every live call row this daemon's worker id still holds."""
        try:
            rows = store.search_calls(status="claimed", limit=100000)
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.warning("council daemon: could not list claimed calls: %s", exc)
            return []
        return [
            dict(row)
            for row in rows
            if row.get("claimed_by") == self.worker_id and row.get("claim_token")
        ]

    @staticmethod
    def _interrupted_usage(row: Mapping[str, Any]) -> Dict[str, Any]:
        """What an interrupted call spent: what was measured, else ``unknown``.

        ``calls.fresh_usage`` is written by ``complete_call``, so a row that
        is still CLAIMED normally has none -- but a provider whose api type
        recorded usage before the cancellation landed would, and throwing that
        away to write ``unknown`` would lose a real number.
        """
        usage = row.get("fresh_usage")
        if isinstance(usage, Mapping) and usage:
            return dict(usage)
        return dict(UNKNOWN_USAGE)

    def _budget_of(self, endpoint: str) -> int:
        """The endpoint's ``max_total_tokens``, or 0 when it has no budget."""
        try:
            return int(dict(self.council.config().endpoint(endpoint)).get("max_total_tokens") or 0)
        except Exception:  # noqa: BLE001 - an unconfigured endpoint has no budget
            return 0

    @staticmethod
    def _uncharged_attempt_no(store: Any, row: Mapping[str, Any]) -> int:
        """A free attempt number that no CHARGED attempt will ever use."""
        nxt = getattr(store, "next_uncharged_attempt_no", None)
        if callable(nxt):
            return int(nxt(row["call_id"]))
        # pragma: no cover - SqliteStore has it; a store that does not gets
        # the same below-zero convention without the uniqueness guarantee.
        return -(int(row.get("attempt_count") or 0) + 1)

    # -- the worker --------------------------------------------------------

    def _worker_for(self, store: Any) -> _DaemonWorker:
        """This daemon's worker, rebuilt only when the served set changes.

        Rebuilt rather than mutated because ``CouncilWorker`` derives its
        shared-account concurrency groups from its endpoint set at
        construction; reaching in to change that set afterwards would leave
        the semaphores sized for the old one.  The worker ID is this daemon's
        and does not change, so the ``workers`` row stays one row across
        rebuilds and the claims of the previous worker object are still this
        daemon's.
        """
        worker = self._worker
        if worker is not None and set(worker.backends) == self.served:
            return worker
        worker = _DaemonWorker(
            store,
            self.council.config(),
            sorted(self.served),
            self.council.user(),
            self.worker_id,
            worker_kind="daemon",
            heartbeat_interval_s=self.timings.heartbeat_s,
            poll_interval_s=min(1.0, self.timings.idle_poll_s),
            admits=self._admits,
            route_wait_s=self.timings.probe_s,
            # ``None`` for every pre-queue-mode caller and for the
            # controller-less legacy daemon -- unscoped, unchanged.  Set by
            # :meth:`_run_as_queue_worker` for a queue worker.
            seats=self._scope_seats,
            roles=self._scope_roles,
            concurrency=self._scope_concurrency,
        )
        self._worker = worker
        worker.register()
        logger.info(
            "council daemon %s serving %s", self.worker_id, sorted(self.served) or "(nothing)"
        )
        return worker

    def _admits(self, row: Mapping[str, Any]) -> Tuple[bool, str]:
        """May this claimed call's ROUTE be run right now?

        ``ready`` runs.  ``unknown`` runs only for a call whose OWN job was
        submitted with ``allow_unverified`` -- read from that job every time,
        so one job's permission can never admit another job's unknown calls
        (review round 3), even though both are in this one queue and may be in
        flight at the same moment.  ``unavailable`` never runs.

        A route with no report at all is admitted: it is a route that appeared
        since the last probe, and the worker's own per-call gate will decide
        it.  Refusing it instead would stall a new job for up to a probe
        interval for no better reason than cache coldness.
        """
        route = _route_of(row)
        report = self.readiness.get(route.key)
        if report is None:
            return True, ""
        state = str(report.get("state") or "unknown")
        if state == "ready":
            return True, ""
        detail = str(report.get("detail") or "no detail")
        if state == "unknown":
            if self._job_options(row).get("allow_unverified"):
                return True, ""
            return False, (
                f"this machine cannot confirm that {route.endpoint!r} serves "
                f"{route.model!r} ({detail}), and job {row['job_id']} was not "
                "submitted with allow_unverified"
            )
        return False, detail

    def _job_options(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        """The permissions of the JOB this call belongs to.

        ``_pending_calls`` rides the job's options along on each row, so the
        probe pass answers from what it already read; a row that arrived
        another way (straight out of ``claim_calls``) is answered by reading
        the job.  Either way the authority is the job's own options and never
        this daemon's flags, which is what keeps one job's
        ``allow_unverified`` from admitting another job's unknown calls.
        """
        options = row.get("job_options")
        if isinstance(options, Mapping):
            return CouncilWorker.job_permissions({"options": dict(options)})
        job = None
        try:
            job = self.council.require_schema().get_job(row["job_id"])
        except Exception as exc:  # noqa: BLE001 - treat as "no permission"
            logger.debug("council daemon: could not read job options: %s", exc)
        return CouncilWorker.job_permissions(job)

    # -- readiness ---------------------------------------------------------

    async def _refresh_readiness(self, store: Any) -> Dict[Tuple[str, str], Dict[str, Any]]:
        """Re-probe every route, recompute the served set, and record why not.

        One probe per DISTINCT endpoint (five seats on one gateway cost one
        free ``/models`` GET), then one report per ``(endpoint, model)`` read
        off it.  Nothing here can spend a token: ``probe_endpoint`` lists
        models, runs ``--version`` and reads billing config files, and never
        calls ``complete()``.
        """
        pending = self._pending_calls(store)
        routes = self._routes(pending)
        reports: Dict[Tuple[str, str], Dict[str, Any]] = {}
        by_endpoint: Dict[str, List[_Route]] = {}
        for route in routes.values():
            by_endpoint.setdefault(route.endpoint, []).append(route)

        for endpoint, group in sorted(by_endpoint.items()):
            kind = next((r.endpoint_kind for r in group if r.endpoint_kind), "")
            api = self._api_for(endpoint, kind)
            probe = await probe_endpoint(endpoint, kind, api)
            for route in group:
                # ``allow_unverified=True``: the ``verified: false`` refusal is
                # the ROSTER's (``CouncilWorker._check_roster``), not the
                # endpoint's, so this reads only what the endpoint said -- the
                # same choice ``CouncilUtils._servable_seats`` makes.
                reports[route.key] = seat_state("", route, probe, allow_unverified=True)
        self.readiness = reports
        self.served = self._served_endpoints(reports, pending)
        self._record_route_blockers(store, pending)
        self._note_halts(store, sorted(by_endpoint))
        return reports

    def _served_endpoints(
        self,
        reports: Mapping[Tuple[str, str], Mapping[str, Any]],
        pending: Sequence[Mapping[str, Any]],
    ) -> Set[str]:
        """Which endpoints this daemon will claim calls for at all.

        An endpoint with at least one ``ready`` route, plus one whose only
        usable routes are ``unknown`` and which has a pending call belonging
        to a job that carries ``allow_unverified``.  The per-CALL decision is
        still :meth:`_admits`'s -- this set only decides what is worth asking
        the queue for, and claiming nothing for an endpoint no job may use
        saves a claim and a release per pass.
        """
        permitted = {
            str(call["endpoint"])
            for call in pending
            if bool(dict(call.get("job_options") or {}).get("allow_unverified"))
        }
        served: Set[str] = set()
        for (endpoint, _model), report in reports.items():
            state = str(report.get("state") or "unknown")
            if state == "ready" or (state == "unknown" and endpoint in permitted):
                served.add(endpoint)
        return served

    def _record_route_blockers(self, store: Any, pending: Sequence[Mapping[str, Any]]) -> None:
        """Put the latest probe failure on each waiting call, or take it off.

        ``kbu council jobs status`` reads its ``waiting_for_model`` blocker off
        ``calls.error`` on PENDING rows, and nothing else in the store can
        say why a route this machine cannot serve is not moving.  So the
        probe's own detail is written there -- and CLEARED again the moment a
        probe succeeds, which is what makes "a route unavailable at start
        becomes eligible when its probe succeeds" visible rather than merely
        true.
        """
        for call in pending:
            if call.get("status") != "pending":
                continue
            admitted, why = self._admits(call)
            had = str(dict(call.get("error") or {}).get("kind") or "")
            if admitted:
                if had == ROUTE_ERROR_KIND:
                    self._safely(store.note_call_error, call["call_id"], None)
                continue
            error = CouncilError(kind=ROUTE_ERROR_KIND, message=why, retryable=True)
            self._safely(store.note_call_error, call["call_id"], error.model_dump())

    def _note_halts(self, store: Any, endpoints: Iterable[str]) -> None:
        """Log each NEW halt once, and remember what is halted.

        Once, because the loop runs every few seconds and a halted endpoint
        stays halted until somebody clears it: logging it per pass would bury
        everything else in ``daemon.log``.  The remembered state is also what
        :meth:`_halt_cleared` reads, so ``kbu council endpoints reset-halt``
        is followed by a probe rather than by up to a probe interval of
        nothing happening.
        """
        for endpoint in endpoints:
            try:
                state = store.get_endpoint_state(endpoint)
            except Exception as exc:  # noqa: BLE001
                logger.debug("council daemon: endpoint state unreadable: %s", exc)
                continue
            kind = state.get("halt_kind") or None
            if kind and self._halts.get(endpoint) != kind:
                logger.warning(
                    "council daemon: endpoint %s halted (%s): %s",
                    endpoint,
                    kind,
                    state.get("halt_message") or "no message",
                )
            self._halts[endpoint] = kind

    def _halt_cleared(self, store: Any) -> bool:
        """Has a halt this daemon knew about been cleared since the last pass?

        The daemon and ``kbu council endpoints reset-halt`` are two processes
        with no channel between them (a store is a file, not a server), so
        "immediately after reset-halt" is implemented as the daemon noticing
        on its next pass that an endpoint it had recorded as halted no longer
        is.  That is a few seconds rather than up to a probe interval, which
        is what the requirement is for.
        """
        for endpoint, known in list(self._halts.items()):
            if not known:
                continue
            try:
                current = store.get_endpoint_state(endpoint).get("halt_kind") or None
            except Exception:  # noqa: BLE001
                continue
            if current is None:
                logger.info(
                    "council daemon: endpoint %s is no longer halted; re-probing",
                    endpoint,
                )
                self._halts[endpoint] = None
                return True
        return False

    def _api_for(self, endpoint: str, kind: str) -> Any:
        """The api type instance to probe *endpoint* with, or ``None``.

        ``None`` is a legitimate answer and :func:`~.servable.probe_endpoint`
        handles it: an endpoint this installation has no api type for is
        ``unknown``, not broken, and inventing a verdict about it would be a
        verdict on calls that cannot happen.
        """
        try:
            return self.council.endpoint_api(endpoint, kind)
        except Exception as exc:  # noqa: BLE001 - a bad kind is not fatal here
            logger.debug("council daemon: no api type for %r (%r): %s", endpoint, kind, exc)
            return None

    # -- what there is to do -----------------------------------------------

    def _pending_calls(self, store: Any) -> List[Dict[str, Any]]:
        """Every live, outstanding call in the store, with its job's options.

        The whole store, not one job and not one provenance label: the daemon
        drains the queue, and the queue is the store's (review round 3).  The
        job's ``options`` ride along on each row because the per-call
        permission is read from them and re-reading the job per call would be
        one SELECT per call per pass.

        Restricted to :attr:`_scope_seats` for a queue worker -- "each child
        probes only the routes its own seats need" -- so a queue's children
        never probe, claim-reason-write-blockers-onto, or otherwise take
        responsibility for a call that belongs to a DIFFERENT queue.  ``None``
        (every caller before queue mode existed, and the unpartitioned
        legacy daemon) is unscoped, unchanged.
        """
        rows: List[Dict[str, Any]] = []
        for job in store.list_jobs():
            if job["status"] in ("done", "cancelled"):
                continue
            options = dict(job.get("options") or {})
            for call in store.get_calls(job["job_id"]):
                if not call.get("live", True):
                    continue
                if call.get("status") not in ("pending", "claimed"):
                    continue
                if self._scope_seats is not None and call.get("seat_name") not in self._scope_seats:
                    continue
                row = dict(call)
                row["job_options"] = options
                rows.append(row)
        return rows

    def _routes(self, pending: Sequence[Mapping[str, Any]]) -> Dict[Tuple[str, str], _Route]:
        """Every route to keep probed: the queue's, plus the profiles' seats.

        The queue's, because a call whose route is never probed is a call the
        daemon can neither run nor explain.  The profiles', because a route
        nothing needs yet is the route the next submit will need, and a
        ``doctor`` that has to wait for a cold probe is a ``doctor`` nobody
        runs.
        """
        routes: Dict[Tuple[str, str], _Route] = {}
        for call in pending:
            route = _route_of(call)
            routes.setdefault(route.key, route)
        for route in self._profile_routes():
            routes.setdefault(route.key, route)
        return routes

    def _profile_routes(self) -> List[_Route]:
        """The seats of every configured profile (or of *profiles*), as routes.

        Restricted to :attr:`_scope_seats` for a queue worker, for the same
        reason :meth:`_pending_calls` is: a queue's children keep only their
        OWN seats warm, never the whole roster's.
        """
        try:
            config = self.council.config()
        except Exception as exc:  # noqa: BLE001 - a bad config is the CLI's error
            logger.debug("council daemon: configuration unreadable: %s", exc)
            return []
        names = self._profiles
        if names is None:
            names = sorted(getattr(config, "profiles", {}) or {})
        routes: List[_Route] = []
        for name in names:
            try:
                roster = config.roster(name)
            except Exception as exc:  # noqa: BLE001 - one bad profile is not fatal
                logger.debug("council daemon: profile %r unusable: %s", name, exc)
                continue
            for _role, seat in roster.seats():
                if self._scope_seats is not None and seat.seat_name not in self._scope_seats:
                    continue
                routes.append(
                    _Route(
                        endpoint=seat.endpoint,
                        endpoint_kind=seat.endpoint_kind,
                        model=seat.model,
                        canonical_model=seat.canonical_model,
                        seat_name=seat.seat_name,
                    )
                )
        return routes

    # -- signals -----------------------------------------------------------

    def request_stop(self, signum: int = 0, _frame: Any = None) -> None:
        """Stop claiming and start draining.  Safe from a signal handler.

        Two steps because the handler may run before there is a loop to wake:
        the flag is always set (``_serve`` reads it on the way in), and the
        loop is woken only if one is running.  Going through
        ``call_soon_threadsafe`` both sets the ``asyncio`` event and WAKES the
        loop, so a daemon idling on a ten-second poll reacts at once instead
        of in ten seconds.
        """
        if signum:
            logger.info(
                "council daemon: %s received; no new claims, draining in-flight calls",
                signal.Signals(signum).name,
            )
        self._stop_flag.set()
        loop, stop = self._loop, self._async_stop
        if loop is not None and stop is not None:
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:  # pragma: no cover - the loop closed first
                pass

    # -- clocks ------------------------------------------------------------

    def _tick_clock(self) -> bool:
        """Record this pass's clocks; return True if the machine slept.

        Suspension is detected as the wall clock having advanced MORE than the
        monotonic clock did, by more than twice the heartbeat interval: on
        Linux ``CLOCK_MONOTONIC`` does not run while the machine is suspended
        and ``CLOCK_REALTIME`` does, so the difference between the two deltas
        is the time spent asleep.  Comparing wall-clock deltas alone would
        read a single long busy pass -- a 20-minute CLI call -- as a resume.
        """
        wall, mono = self._wall(), self._mono()
        last_wall, last_mono = self._last_wall, self._last_mono
        self._last_wall, self._last_mono = wall, mono
        if last_wall is None or last_mono is None:
            return False
        slept = (wall - last_wall) - (mono - last_mono)
        if slept > 2 * self.timings.heartbeat_s:
            self.resumes += 1
            logger.info(
                "council daemon: a %.0fs wall-clock gap looks like a suspend/resume; "
                "reclaiming stale claims before the next heartbeat",
                slept,
            )
            return True
        return False

    @staticmethod
    def _safely(call: Any, *args: Any) -> None:
        """Run a store write whose failure must not end the loop."""
        try:
            call(*args)
        except Exception as exc:  # noqa: BLE001 - logged, never raised
            logger.debug("council daemon: store write failed: %s", exc)


# ---------------------------------------------------------------------------
# Process plumbing
# ---------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    """Does a process with this pid exist?  (Signal 0 asks without sending.)"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists; it just is not ours.  Reporting "gone" would make `stop`
        # claim the lock was free when something is holding it.
        return True
    return True


def _reap(pid: int, timeout: float = 5.0) -> None:
    """Reap *pid* if it is this process's own child; ignore it if not.

    A ``stop`` issued from the process that did the ``start`` (a test, a
    notebook, a script that starts a daemon and stops it again) is the parent
    of the daemon it just signalled, and an unreaped child is a zombie that
    still answers ``kill(pid, 0)`` -- so "is it gone?" would read as "no" for
    ever.  In the normal case -- ``kbu council daemon stop`` in a different
    process -- the daemon is not our child at all, ``waitpid`` says so at
    once, and there is nothing to do: it was reparented to init when its own
    starter exited, and init reaps it.

    Polled rather than done once, because the lock comes free a moment BEFORE
    the process is gone: the kernel drops the ``flock`` when the descriptor
    closes, which is during interpreter shutdown, so the first ``waitpid``
    usually finds the child still alive.  Bounded rather than blocking, so a
    daemon stuck in its own shutdown cannot hang ``stop``.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            reaped, _status = os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            return
        if reaped == pid or time.monotonic() >= deadline:
            return
        time.sleep(0.02)


class _signals:
    """``SIGTERM``/``SIGINT`` ask *daemon* to stop; the old handlers come back.

    ``signal.signal`` rather than ``loop.add_signal_handler`` because the
    daemon's loop is created and closed inside one call: an asyncio signal
    handler outlives its loop's close and would fire ``call_soon`` on a closed
    loop the next time a test ran one.  Restoring the previous handlers on the
    way out is what lets a test drive the loop in-process without taking
    pytest's own handlers with it.
    """

    def __init__(self, daemon: "CouncilDaemon") -> None:
        self._daemon = daemon
        self._previous: Dict[int, Any] = {}

    def __enter__(self) -> "_signals":
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._previous[sig] = signal.signal(sig, self._daemon.request_stop)
            except ValueError:  # pragma: no cover - not the main thread
                logger.debug("council daemon: cannot install a %s handler here", sig)
        return self

    def __exit__(self, *_exc: Any) -> None:
        for sig, handler in self._previous.items():
            try:
                signal.signal(sig, handler)
            except ValueError:  # pragma: no cover - see __enter__
                pass


async def _wait_for(stop: asyncio.Event, seconds: float) -> None:
    """Wait up to *seconds*, returning early if *stop* is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=max(0.0, seconds))
    except asyncio.TimeoutError:
        return
