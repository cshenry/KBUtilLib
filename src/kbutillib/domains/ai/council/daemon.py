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
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .engine.worker import CouncilWorker
from .execlock import ExecutionLock, LockHeld
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
    """

    heartbeat_s: float = 30.0
    probe_s: float = 60.0
    idle_poll_s: float = 10.0
    drain_s: float = 120.0
    stop_wait_s: float = 130.0
    start_wait_s: float = 30.0

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
    """

    def __init__(
        self,
        council: Any,
        timings: Optional[DaemonTimings] = None,
        *,
        child_argv: Optional[Sequence[str]] = None,
        profiles: Optional[Sequence[str]] = None,
        worker_id: Optional[str] = None,
    ) -> None:
        self.council = council
        self.timings = timings or DEFAULT_TIMINGS
        self.child_argv = list(child_argv) if child_argv else None
        self._profiles = list(profiles) if profiles is not None else None
        self.worker_id = worker_id or f"{socket.gethostname()}-daemon-{uuid.uuid4().hex[:8]}"

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
        return {
            "store": self.council.store_url(),
            "directory": str(self.directory),
            "lock": str(self.lock.path),
            "log": str(self.log_path),
            "running": holder is not None,
            "holder": holder,
            "workers": rows,
        }

    # -- start -------------------------------------------------------------

    def start(self, foreground: bool = False) -> Dict[str, Any]:
        """Start the daemon.  Detached by default; in-process with *foreground*.

        The detached form spawns ONE child in a session of its own
        (``start_new_session=True``, so no terminal hangup and no controlling
        tty reaches it) with stdout and stderr appended to
        :data:`LOG_FILENAME`, and then waits until that child is the lock's
        holder.  Waiting is the point: a ``start`` that returned as soon as
        ``Popen`` did would report success for a child that was about to die
        of a configuration error, and the user would learn that from an empty
        ``status`` minutes later.

        Raises:
            LockHeld: another council process already holds the lock.  The
                message names its kind, pid and start time.
            DaemonError: the child exited, or never took the lock.
        """
        if foreground:
            return self.run_foreground()

        holder = self.lock.probe()
        if holder is not None:
            raise LockHeld(self.lock.held_message("daemon", holder), holder)

        self.directory.mkdir(parents=True, exist_ok=True)
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
            holder = self.lock.probe()
            if holder is not None and holder.get("pid") == child.pid:
                return {
                    "started": True,
                    "pid": child.pid,
                    "foreground": False,
                    "lock": str(self.lock.path),
                    "log": str(self.log_path),
                    "holder": holder,
                }
            code = child.poll()
            if code is not None:
                raise DaemonError(
                    f"the council daemon child (pid {child.pid}) exited {code} "
                    f"before it took the execution lock. The last lines of "
                    f"{self.log_path}:\n{self._log_tail()}"
                )
            time.sleep(0.05)

        holder = self.lock.probe()
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

    def stop(self) -> Dict[str, Any]:
        """Stop the daemon holding this store's lock, and wait for it to go.

        Signals ONLY a holder whose recorded kind is ``daemon`` (review round
        3).  A ``kbu council run`` in somebody's terminal and a ``db migrate``
        halfway through dropping a table are also lock holders, and
        ``SIGTERM``-ing either because they were in the way is not this verb's
        business -- so it says what is running and stops.
        """
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

    # -- the foreground loop -----------------------------------------------

    def run_foreground(self) -> Dict[str, Any]:
        """Hold the lock and serve this store until ``SIGTERM``/``SIGINT``.

        This is the daemon.  ``start`` without ``--foreground`` is a wrapper
        that spawns a child which calls exactly this, and ``--foreground`` is
        what a systemd unit, a launchd job, ``tmux`` or ``nohup`` runs -- the
        daemon does not care which, because process supervision is the
        machine's job and differs on every machine in this fleet.

        Raises:
            LockHeld: another council process holds the execution lock.
        """
        with self.lock.acquire("daemon") as record:
            # Before ANYTHING is served: both CLI backends read project
            # context out of the cwd, and a daemon inheriting the directory it
            # was launched from would answer council questions with somebody's
            # repository in scope.  The council directory holds the store, the
            # lock and the log and nothing a CLI would read as context.
            os.chdir(self.directory)
            return _run_loop(self._serve(record))

    async def _serve(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        """The loop, with the lock already held and the cwd already moved."""
        store = self.council.require_schema()
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        summary: Dict[str, Any] = {
            "worker_id": self.worker_id,
            "pid": os.getpid(),
            "store": self.council.store_url(),
            "lock": str(self.lock.path),
            "passes": 0,
            "claimed": 0,
            "finished": 0,
            "interrupted": 0,
            "resumes": 0,
            "started_at": str(record.get("started_at") or ""),
        }

        with _signals(loop, stop):
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
        from ... import llm

        return int(await llm.kill_cli_children())

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

        The attempt number is the next unused one rather than
        ``attempt_count + 1``: the retry count deliberately does not move, so
        the number the call's next real try will use is still free and filing
        this row under it would collide.
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
                        self._next_attempt_no(store, row),
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
    def _next_attempt_no(store: Any, row: Mapping[str, Any]) -> int:
        """The next free ``attempts.attempt_no`` for this call."""
        nxt = getattr(store, "next_attempt_no", None)
        if callable(nxt):
            return int(nxt(row["call_id"]))
        return int(row.get("attempt_count") or 0) + 1  # pragma: no cover - SqliteStore has it

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
        """The seats of every configured profile (or of *profiles*), as routes."""
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


class _signals:
    """``SIGTERM``/``SIGINT`` set *stop*, and the previous handlers come back.

    ``signal.signal`` rather than ``loop.add_signal_handler`` because the
    daemon's loop is created and closed inside one call: an asyncio signal
    handler outlives its loop's close and would fire ``call_soon`` on a closed
    loop the next time a test ran one.  Restoring the previous handlers on the
    way out is what lets a test drive the loop in-process without taking
    pytest's own handlers with it.
    """

    def __init__(self, loop: Any, stop: asyncio.Event) -> None:
        self._loop = loop
        self._stop = stop
        self._previous: Dict[int, Any] = {}

    def __enter__(self) -> "_signals":
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._previous[sig] = signal.signal(sig, self._handle)
            except ValueError:  # pragma: no cover - not the main thread
                logger.debug("council daemon: cannot install a %s handler here", sig)
        return self

    def __exit__(self, *_exc: Any) -> None:
        for sig, handler in self._previous.items():
            try:
                signal.signal(sig, handler)
            except ValueError:  # pragma: no cover - see __enter__
                pass

    def _handle(self, signum: int, _frame: Any) -> None:
        logger.info(
            "council daemon: %s received; no new claims, draining in-flight calls",
            signal.Signals(signum).name,
        )
        # The handler runs on the main thread between bytecodes, which may be
        # anywhere -- including inside the loop's own select().  Going through
        # call_soon_threadsafe both sets the event and WAKES the loop, so a
        # daemon idling on a ten-second poll reacts at once instead of in ten
        # seconds.
        self._loop.call_soon_threadsafe(self._stop.set)


async def _wait_for(stop: asyncio.Event, seconds: float) -> None:
    """Wait up to *seconds*, returning early if *stop* is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=max(0.0, seconds))
    except asyncio.TimeoutError:
        return
