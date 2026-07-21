"""Bounded async worker pool for the Remote LP-Solver service.

Drives :func:`kbutillib.services.lp_solver.solver_backends.solve` from the
``queued`` jobs sitting in a :class:`~kbutillib.services.lp_solver.job_store.LPJobStore`,
per ``agent-io/prds/remote-lp-solver/fullprompt.md`` ("Repo & module layout" /
"Concurrency, threads, timeouts" / Confront-hardened spec **S6**).

Design notes
------------
- **Subprocess isolation (S6).** ``gurobipy``/``cplex`` are in-process C APIs
  that cannot be interrupted cleanly from a thread, so every solve runs in
  its own short-lived Python subprocess (``python -c <snippet>``) that
  dynamically imports a *solve entrypoint* (``module:function``, defaulting
  to ``kbutillib.services.lp_solver.solver_backends:solve``) and writes its
  JSON result to a temp file this pool then reads back. Tests substitute a
  stub entrypoint to exercise the same subprocess-launch/watchdog/job-store
  glue without a real solver installed.
- **Watchdog.** A wedged/looping solver subprocess is sent ``SIGTERM`` at
  ``time_limit + 60s`` and ``SIGKILL`` 5s later if it hasn't exited; the job
  is marked ``error`` either way.
- **Per-job ``time_limit``.** :class:`~kbutillib.services.lp_solver.job_store.LPJobStore`
  only persists ``solver`` alongside a job (no ``time_limit`` column), so
  this module keeps a small JSON side-car file per job
  (``<meta_dir>/{job_id}.meta.json``) written by the API layer (``app.py``)
  at submission time and consumed here at claim time -- this survives a
  service restart (unlike an in-process dict) without touching
  ``job_store.py``'s schema, which is owned by a sibling task.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Set, Union

from . import solver_backends
from .job_store import LPJobStore

logger = logging.getLogger(__name__)

__all__ = [
    "LPWorkerPool",
    "compute_threads_per_solve",
    "clamp_time_limit",
    "write_job_meta",
    "read_job_meta",
    "remove_job_meta",
    "DEFAULT_MAX_CONCURRENT_SOLVES",
    "DEFAULT_TIME_LIMIT",
    "DEFAULT_MAX_TIME_LIMIT",
    "WATCHDOG_GRACE_SECONDS",
    "WATCHDOG_KILL_SECONDS",
    "DEFAULT_SOLVE_ENTRYPOINT",
]

# ---------------------------------------------------------------------------
# Defaults (fullprompt.md "Concurrency, threads, timeouts" / S11 / S13).
# ---------------------------------------------------------------------------

DEFAULT_MAX_CONCURRENT_SOLVES = 5
# Reuse solver_backends' constants as the single source of truth for the
# documented 3600s/7200s defaults -- worker.py's copies are what get applied
# when the *service config* omits default_time_limit/max_time_limit; the
# constants inside solver_backends remain its own unconditional safety net.
DEFAULT_TIME_LIMIT = solver_backends.DEFAULT_TIME_LIMIT
DEFAULT_MAX_TIME_LIMIT = solver_backends.MAX_TIME_LIMIT

WATCHDOG_GRACE_SECONDS = 60.0  # SIGTERM at time_limit + this
WATCHDOG_KILL_SECONDS = 5.0  # SIGKILL this long after SIGTERM if still alive

_IDLE_POLL_SECONDS = 0.5  # how often the dispatcher checks for new/free work

DEFAULT_SOLVE_ENTRYPOINT = "kbutillib.services.lp_solver.solver_backends:solve"

PathLike = Union[str, "os.PathLike[str]"]


def compute_threads_per_solve(
    max_concurrent_solves: int, cpu_count: Optional[int] = None
) -> int:
    """Compute ``threads_per_solve`` per fullprompt.md S11.

    ``max(1, floor(os.cpu_count() / max_concurrent_solves))`` using logical
    cores. ``cpu_count`` is injectable for deterministic tests.
    """
    resolved_cpu_count = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    resolved_max_concurrent = max(1, int(max_concurrent_solves))
    return max(1, math.floor(resolved_cpu_count / resolved_max_concurrent))


def clamp_time_limit(
    time_limit: Optional[float],
    default_time_limit: float = DEFAULT_TIME_LIMIT,
    max_time_limit: float = DEFAULT_MAX_TIME_LIMIT,
) -> float:
    """Apply the service-config clamp policy (Acceptance Criterion 8).

    ``None`` -> *default_time_limit*; any value is clamped to
    ``[0, max_time_limit]``.
    """
    if time_limit is None:
        time_limit = default_time_limit
    return max(0.0, min(float(time_limit), float(max_time_limit)))


# ---------------------------------------------------------------------------
# Per-job time_limit side-car file (see module docstring).
# ---------------------------------------------------------------------------


def _meta_path(meta_dir: Path, job_id: str) -> Path:
    return Path(meta_dir) / f"{job_id}.meta.json"


def write_job_meta(
    meta_dir: PathLike, job_id: str, *, time_limit: Optional[float]
) -> None:
    """Persist the caller-requested ``time_limit`` for *job_id*.

    Called by ``app.py`` right after ``job_store.create()`` returns the new
    ``job_id``, before responding to the client.
    """
    meta_dir_path = Path(meta_dir)
    meta_dir_path.mkdir(parents=True, exist_ok=True)
    _meta_path(meta_dir_path, job_id).write_text(json.dumps({"time_limit": time_limit}))


def read_job_meta(meta_dir: PathLike, job_id: str) -> Dict[str, Any]:
    """Read back the side-car metadata for *job_id*, or ``{}`` if absent/corrupt."""
    try:
        return json.loads(_meta_path(Path(meta_dir), job_id).read_text())
    except (FileNotFoundError, OSError, ValueError):
        return {}


def remove_job_meta(meta_dir: PathLike, job_id: str) -> None:
    """Best-effort removal of *job_id*'s side-car metadata file."""
    _meta_path(Path(meta_dir), job_id).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Subprocess solve isolation (S6).
# ---------------------------------------------------------------------------

# Executed via `python -c` in its own subprocess. Dynamically imports the
# configured solve entrypoint so tests can substitute a stub without a real
# solver installed, while production uses solver_backends.solve unchanged.
_SOLVE_SUBPROCESS_SNIPPET = """
import importlib
import json
import sys

entrypoint, lp_path, solver, time_limit, threads, result_path = sys.argv[1:7]
module_name, func_name = entrypoint.split(":")
fn = getattr(importlib.import_module(module_name), func_name)
result = fn(
    lp_path,
    solver=(solver or None),
    time_limit=float(time_limit),
    threads=int(threads),
)
with open(result_path, "w") as fh:
    json.dump(result, fh)
"""


class _WatchdogKill(Exception):
    """Raised internally when the watchdog had to SIGTERM/SIGKILL a solve."""


class LPWorkerPool:
    """Bounded async pool that pulls ``queued`` jobs and drives solves.

    Args:
        job_store: The shared :class:`LPJobStore` instance (constructed and
            owned by ``app.py``).
        meta_dir: Directory holding per-job ``time_limit`` side-car files
            (see module docstring). Independently overridable so tests never
            touch real service state.
        max_concurrent_solves: Bounded pool size (default 5).
        threads_per_solve: Per-solve thread cap passed to
            ``solver_backends.solve``. Defaults to
            :func:`compute_threads_per_solve`.
        default_time_limit / max_time_limit: Service-config clamp policy
            (Acceptance Criterion 8), applied to whatever ``time_limit`` was
            recorded in the job's side-car metadata (or omitted).
        solve_entrypoint: ``"module:function"`` dynamically imported inside
            each solve subprocess. Defaults to
            ``kbutillib.services.lp_solver.solver_backends:solve``; tests
            override with a stub.
        subprocess_cwd: Optional explicit working directory for solve
            subprocesses. ``None`` inherits this process's cwd.
    """

    def __init__(
        self,
        job_store: LPJobStore,
        meta_dir: PathLike,
        *,
        max_concurrent_solves: int = DEFAULT_MAX_CONCURRENT_SOLVES,
        threads_per_solve: Optional[int] = None,
        default_time_limit: float = DEFAULT_TIME_LIMIT,
        max_time_limit: float = DEFAULT_MAX_TIME_LIMIT,
        solve_entrypoint: str = DEFAULT_SOLVE_ENTRYPOINT,
        subprocess_cwd: Optional[PathLike] = None,
        idle_poll_seconds: float = _IDLE_POLL_SECONDS,
    ) -> None:
        self.job_store = job_store
        self.meta_dir = Path(meta_dir)
        self.meta_dir.mkdir(parents=True, exist_ok=True)

        self.max_concurrent_solves = max(1, int(max_concurrent_solves))
        self.threads_per_solve = (
            int(threads_per_solve)
            if threads_per_solve is not None
            else compute_threads_per_solve(self.max_concurrent_solves)
        )
        self.default_time_limit = float(default_time_limit)
        self.max_time_limit = float(max_time_limit)
        self.solve_entrypoint = solve_entrypoint
        self.subprocess_cwd = str(subprocess_cwd) if subprocess_cwd else None
        self.idle_poll_seconds = idle_poll_seconds

        self._semaphore = asyncio.Semaphore(self.max_concurrent_solves)
        self._tasks: Set[asyncio.Task] = set()
        self._dispatcher_task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background dispatcher loop. Call once per app lifespan."""
        self._stop_event = asyncio.Event()
        self._dispatcher_task = asyncio.create_task(self._dispatch_loop())

    async def stop(self) -> None:
        """Stop dispatching new jobs and wait for in-flight solves to finish."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._dispatcher_task is not None:
            await self._dispatcher_task
            self._dispatcher_task = None
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ── dispatch loop ────────────────────────────────────────────────

    async def _dispatch_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            if self._semaphore.locked():
                await self._sleep_or_stop()
                continue

            claimed = self.job_store.claim_next()
            if claimed is None:
                await self._sleep_or_stop()
                continue

            await self._semaphore.acquire()
            task = asyncio.create_task(self._run_job(claimed))
            self._tasks.add(task)
            task.add_done_callback(self._on_job_task_done)

    def _on_job_task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        self._semaphore.release()
        exc = task.exception() if not task.cancelled() else None
        if exc is not None:
            logger.error("Unhandled exception in LP-solver job task", exc_info=exc)

    async def _sleep_or_stop(self) -> None:
        assert self._stop_event is not None
        try:
            await asyncio.wait_for(
                self._stop_event.wait(), timeout=self.idle_poll_seconds
            )
        except asyncio.TimeoutError:
            pass

    # ── one job ──────────────────────────────────────────────────────

    async def _run_job(self, claimed: Dict[str, Any]) -> None:
        job_id = claimed["job_id"]
        solver = claimed.get("solver")
        lp_path = claimed["lp_path"]

        meta = read_job_meta(self.meta_dir, job_id)
        time_limit = clamp_time_limit(
            meta.get("time_limit"), self.default_time_limit, self.max_time_limit
        )
        result_path = self.meta_dir / f"{job_id}.result.json"

        try:
            result = await self._solve_in_subprocess(
                lp_path, solver, time_limit, result_path
            )
        except _WatchdogKill:
            self.job_store.mark_error(
                job_id, "solver exceeded time limit + grace"
            )
        except Exception as exc:  # never let a job hang forever (user story 6/9)
            self.job_store.mark_error(job_id, f"{type(exc).__name__}: {exc}")
        else:
            if result is None:
                self.job_store.mark_error(
                    job_id, "solver subprocess produced no result"
                )
            else:
                self.job_store.mark_done(job_id, result)
        finally:
            remove_job_meta(self.meta_dir, job_id)
            result_path.unlink(missing_ok=True)

    async def _solve_in_subprocess(
        self,
        lp_path: str,
        solver: Optional[str],
        time_limit: float,
        result_path: Path,
    ) -> Optional[Dict[str, Any]]:
        # Inherit this process's module-resolution path so a solve subprocess
        # can always import both the solve entrypoint and (in tests) a stub
        # module living outside the installed package -- matches whatever
        # already resolves `kbutillib` for *this* process.
        env = os.environ.copy()
        inherited = os.pathsep.join(p for p in sys.path if p)
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (inherited, env.get("PYTHONPATH", "")) if part
        )

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _SOLVE_SUBPROCESS_SNIPPET,
            self.solve_entrypoint,
            lp_path,
            solver or "",
            repr(time_limit),
            str(self.threads_per_solve),
            str(result_path),
            cwd=self.subprocess_cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        watchdog_deadline = time_limit + WATCHDOG_GRACE_SECONDS
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=watchdog_deadline
            )
        except asyncio.TimeoutError:
            await self._hard_kill(proc)
            raise _WatchdogKill()

        if proc.returncode != 0:
            message = (stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"solve subprocess exited {proc.returncode}: {message[-2000:]}"
            )

        if not result_path.exists():
            return None
        return json.loads(result_path.read_text())

    @staticmethod
    async def _hard_kill(proc: "asyncio.subprocess.Process") -> None:
        """SIGTERM, then SIGKILL after :data:`WATCHDOG_KILL_SECONDS` (S6)."""
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=WATCHDOG_KILL_SECONDS)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
