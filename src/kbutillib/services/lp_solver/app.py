"""Thin FastAPI surface for the Remote LP-Solver service.

Implements ``POST /solve``, ``GET /result/{job_id}``, and ``GET /healthz``
per ``agent-io/prds/remote-lp-solver/fullprompt.md`` ("Wire format &
polling" / "Auth & network binding" / Confront-hardened specifics **S7-S9**,
**S12**, **S17**). This module is deliberately thin: all persistence lives
in :mod:`kbutillib.services.lp_solver.job_store` and all solve orchestration
lives in :mod:`kbutillib.services.lp_solver.worker` -- this file is HTTP
glue, gzip inflation, the queue-depth backstop, and the optional API-key
check.

Run directly::

    python -m kbutillib.services.lp_solver.app --host 127.0.0.1 --port 8091

Configuration is read from ``~/.kbutillib/config.yaml`` under
``remote_solver.*`` via :class:`~kbutillib.shared_env_utils.SharedEnvUtils`
(S15); every key falls back to its documented default when unset.
"""

from __future__ import annotations

import argparse
import gzip
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional, Union

from fastapi import FastAPI, Header, HTTPException, Request

from kbutillib.shared_env_utils import SharedEnvUtils

from . import worker
from .job_store import LPJobStore

logger = logging.getLogger(__name__)

__all__ = ["create_app", "main"]

PathLike = Union[str, "Path"]

# Matches job_store.py's own default base directory (S5) -- kept as an
# independent constant here (rather than reaching into job_store's private
# attributes) so app.py never depends on job_store internals beyond its
# public API.
_DEFAULT_BASE_DIR = Path.home() / ".lp-solver"

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8091


# ---------------------------------------------------------------------------
# Queue-depth accounting (S17): counts only `queued` jobs, `running` excluded.
# job_store.py exposes no count method, so this reads the same SQLite file
# job_store already opens in WAL mode (safe for concurrent readers) using the
# documented schema (job_store.py's `_CREATE_TABLE_SQL` / fullprompt.md
# "Persistence & lifecycle") rather than reimplementing or reaching into
# job_store's private connection.
# ---------------------------------------------------------------------------


def _count_queued_jobs(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout=5000;")
        row = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'queued'"
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _sweep(job_store: LPJobStore, meta_dir: Path) -> None:
    """``job_store.sweep_expired()`` plus clean-up of orphaned meta side-cars (S12)."""
    for job_id in job_store.sweep_expired():
        worker.remove_job_meta(meta_dir, job_id)


def _job_to_response(job: Dict[str, Any], job_id: str) -> Dict[str, Any]:
    """Map a job_store row to the GET /result response body (S8 / AC11).

    - ``done`` -> the stored ``SolveResult`` dict verbatim (already has the
      exact external schema).
    - ``error`` (a worker/orchestration failure, not a produced result) ->
      a synthesized dict with the same schema so callers never have to
      special-case the two kinds of "error".
    - ``queued``/``running`` -> a minimal non-terminal status payload.
    """
    status = job["status"]
    if status == "done":
        return job["result"]
    if status == "error":
        solve_time_s: Optional[float] = None
        if job.get("start_ts") is not None and job.get("end_ts") is not None:
            solve_time_s = job["end_ts"] - job["start_ts"]
        return {
            "status": "error",
            "objective_value": None,
            "variables": {},
            "solver": job.get("solver"),
            "solve_time_s": solve_time_s,
            "error": job.get("error"),
        }
    return {"job_id": job_id, "status": status}


def _check_api_key(configured_key: Optional[str], provided_key: Optional[str]) -> None:
    """Enforce the optional ``x-api-key`` header (S7 / Acceptance Criterion 18).

    Only checked when ``remote_solver.api_key`` is configured; unset entirely
    means no auth is required (localhost-bind-is-the-boundary, per the PRD).
    """
    if not configured_key:
        return
    if provided_key != configured_key:
        raise HTTPException(status_code=401, detail="missing or invalid x-api-key")


def create_app(
    *,
    base_dir: Optional[PathLike] = None,
    env: Optional[SharedEnvUtils] = None,
    solve_entrypoint: Optional[str] = None,
    subprocess_cwd: Optional[PathLike] = None,
) -> FastAPI:
    """Build the Remote LP-Solver FastAPI app.

    Kept as a factory (rather than a module-level ``app`` singleton) so
    importing this module never has the side effect of touching
    ``~/.lp-solver`` -- tests call ``create_app(base_dir=tmp_path / ...)``
    to get a fully isolated instance, and ``main()`` calls it once for the
    real service.

    Args:
        base_dir: Root directory for the job DB/temp files/side-car meta
            (default ``~/.lp-solver``, matching ``job_store.py``'s own
            default). Tests override with a ``tmp_path`` subdirectory.
        env: Optional pre-built :class:`SharedEnvUtils` (tests inject one
            with ``config_file=False`` for a hermetic run); defaults to
            ``SharedEnvUtils()``, which reads ``~/.kbutillib/config.yaml``
            (falling back to the repo's root ``config.yaml``) per S15.
        solve_entrypoint: ``"module:function"`` dynamically imported inside
            each solve subprocess (see ``worker.py``). Defaults to the real
            ``solver_backends.solve``; tests substitute a stub.
        subprocess_cwd: Optional explicit cwd for solve subprocesses.
    """
    env = env or SharedEnvUtils()
    base_dir_path = Path(base_dir) if base_dir is not None else _DEFAULT_BASE_DIR
    db_path = base_dir_path / "jobs.sqlite"
    tmp_dir = base_dir_path / "tmp"
    meta_dir = base_dir_path / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)

    max_concurrent_solves = max(
        1, int(env.get_config_value("remote_solver.max_concurrent_solves", 5))
    )
    max_queue_depth = max(
        0, int(env.get_config_value("remote_solver.max_queue_depth", 50))
    )
    configured_threads = env.get_config_value("remote_solver.threads_per_solve", None)
    threads_per_solve = (
        int(configured_threads)
        if configured_threads is not None
        else worker.compute_threads_per_solve(max_concurrent_solves)
    )
    default_time_limit = float(
        env.get_config_value(
            "remote_solver.default_time_limit", worker.DEFAULT_TIME_LIMIT
        )
    )
    max_time_limit = float(
        env.get_config_value(
            "remote_solver.max_time_limit", worker.DEFAULT_MAX_TIME_LIMIT
        )
    )
    api_key = env.get_config_value("remote_solver.api_key", None)

    store = LPJobStore(db_path=db_path, tmp_dir=tmp_dir)
    pool = worker.LPWorkerPool(
        store,
        meta_dir,
        max_concurrent_solves=max_concurrent_solves,
        threads_per_solve=threads_per_solve,
        default_time_limit=default_time_limit,
        max_time_limit=max_time_limit,
        solve_entrypoint=solve_entrypoint or worker.DEFAULT_SOLVE_ENTRYPOINT,
        subprocess_cwd=subprocess_cwd,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        reaped = store.reap_orphans_on_startup()
        if reaped:
            logger.info("Reaped %d orphaned job(s) on startup", len(reaped))
        await pool.start()
        try:
            yield
        finally:
            await pool.stop()
            store.close()

    app = FastAPI(title="KBUtilLib Remote LP-Solver", lifespan=lifespan)
    app.state.job_store = store
    app.state.worker_pool = pool
    app.state.meta_dir = meta_dir
    app.state.db_path = db_path

    @app.get("/healthz")
    async def healthz() -> Dict[str, str]:
        return {"status": "ok"}

    @app.post("/solve")
    async def solve(
        request: Request,
        solver: Optional[str] = None,
        time_limit: Optional[float] = None,
        x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    ) -> Dict[str, str]:
        _check_api_key(api_key, x_api_key)
        _sweep(store, meta_dir)

        queued_count = _count_queued_jobs(db_path)
        if queued_count >= max_queue_depth:
            raise HTTPException(status_code=503, detail="job queue full")

        raw_body = await request.body()
        try:
            lp_text = gzip.decompress(raw_body).decode("utf-8")
        except OSError as exc:
            raise HTTPException(
                status_code=400, detail=f"expected gzip-encoded body: {exc}"
            )

        job_id = store.create(lp_text, solver=solver)
        worker.write_job_meta(meta_dir, job_id, time_limit=time_limit)
        return {"job_id": job_id}

    @app.get("/result/{job_id}")
    async def get_result(
        job_id: str,
        x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    ) -> Dict[str, Any]:
        _check_api_key(api_key, x_api_key)
        _sweep(store, meta_dir)

        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job_id")
        return _job_to_response(job, job_id)

    return app


def main(argv: Optional[list] = None) -> None:
    """CLI entrypoint: ``python -m kbutillib.services.lp_solver.app``."""
    import uvicorn

    parser = argparse.ArgumentParser(
        prog="kbutillib.services.lp_solver.app",
        description="Run the Remote LP-Solver FastAPI service.",
    )
    parser.add_argument(
        "--host",
        default=_DEFAULT_HOST,
        help=f"Bind host (default: {_DEFAULT_HOST}; keep this localhost-only, "
        "see fullprompt.md 'Auth & network binding').",
    )
    parser.add_argument(
        "--port", type=int, default=_DEFAULT_PORT, help=f"Bind port (default: {_DEFAULT_PORT})"
    )
    args = parser.parse_args(argv)

    application = create_app()
    uvicorn.run(application, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
