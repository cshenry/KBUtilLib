"""SQLite-backed job store for the Remote LP-Solver service.

Owns all persistence for the async LP-solver service: the SQLite jobs
table and the per-job LP temp files written to disk. Multiple solve
subprocesses plus the FastAPI process touch the same database
concurrently, so the connection is opened in WAL mode with a
busy-timeout (retry-on-busy) per
``agent-io/prds/remote-lp-solver/fullprompt.md`` ("Persistence &
lifecycle" / "Confront-hardened specifics" S5).

Job lifecycle: ``queued -> running -> done | error``. Both terminal
states end the job; only ``running`` survives a service restart badly
(:meth:`LPJobStore.reap_orphans_on_startup` fixes that up). Completed
jobs (``done``/``error``) older than 48h are removed by
:meth:`LPJobStore.sweep_expired`, called opportunistically by the
service on each request rather than a background timer (S12).

The database path and temp-file directory both default under
``~/.lp-solver/`` but are independently overridable so tests (and any
future caller) can point at a temp directory instead of touching the
real service state.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# Definitive terminal error set on any job left ``running`` when the
# service starts back up (S15 / Acceptance Criteria 15).
ORPHAN_ERROR_MESSAGE = "service restarted during solve"

# Completed results (``done``/``error``) older than this are swept.
RESULT_TTL_SECONDS = 48 * 60 * 60

_DEFAULT_BASE_DIR = Path.home() / ".lp-solver"
_DEFAULT_DB_PATH = _DEFAULT_BASE_DIR / "jobs.sqlite"
_DEFAULT_TMP_DIR = _DEFAULT_BASE_DIR / "tmp"

_TERMINAL_STATUSES = ("done", "error")

_CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    solver      TEXT,
    submit_ts   REAL NOT NULL,
    start_ts    REAL,
    end_ts      REAL,
    result_json TEXT,
    error       TEXT
);
"""

_CREATE_INDEX_SQL = """\
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);
"""


class LPJobStore:
    """Thin SQLite + temp-file wrapper for the LP-solver job queue.

    Args:
        db_path: Path to the SQLite database file. Defaults to
            ``~/.lp-solver/jobs.sqlite``. The parent directory is
            created automatically.
        tmp_dir: Directory holding per-job LP temp files. Defaults to
            ``~/.lp-solver/tmp``. Created automatically.
        busy_timeout: Seconds SQLite will retry before raising
            ``sqlite3.OperationalError: database is locked`` on a
            contended write. Applied both as the connection-level
            timeout and as ``PRAGMA busy_timeout``.
    """

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        tmp_dir: Optional[Union[str, Path]] = None,
        busy_timeout: float = 30.0,
    ) -> None:
        self._db_path = Path(db_path) if db_path is not None else _DEFAULT_DB_PATH
        self._tmp_dir = Path(tmp_dir) if tmp_dir is not None else _DEFAULT_TMP_DIR
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            str(self._db_path),
            timeout=busy_timeout,
            isolation_level=None,  # we manage transactions explicitly below
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout * 1000)};")
        self._init_schema()

    # ── schema ────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        self._conn.execute(_CREATE_TABLE_SQL)
        self._conn.execute(_CREATE_INDEX_SQL)

    # ── temp-file helpers ────────────────────────────────────────────

    def _lp_path(self, job_id: str) -> Path:
        """Return the per-job LP temp-file path for *job_id*."""
        return self._tmp_dir / f"{job_id}.lp"

    def _remove_lp_file(self, job_id: str) -> None:
        """Best-effort removal of a job's LP temp file."""
        self._lp_path(job_id).unlink(missing_ok=True)

    # ── row conversion ───────────────────────────────────────────────

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "job_id": row["job_id"],
            "status": row["status"],
            "solver": row["solver"],
            "submit_ts": row["submit_ts"],
            "start_ts": row["start_ts"],
            "end_ts": row["end_ts"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": row["error"],
        }

    # ── public API ───────────────────────────────────────────────────

    def create(self, lp: str, solver: Optional[str] = None) -> str:
        """Enqueue a new job for *lp* (LP-format text) and return its ID.

        The LP text is written to its own temp file at
        ``<tmp_dir>/{job_id}.lp``; the database only stores metadata,
        never the LP body itself.
        """
        job_id = str(uuid.uuid4())
        self._lp_path(job_id).write_text(lp)
        now = time.time()
        self._conn.execute(
            """\
            INSERT INTO jobs (job_id, status, solver, submit_ts, start_ts,
                              end_ts, result_json, error)
            VALUES (?, 'queued', ?, ?, NULL, NULL, NULL, NULL)
            """,
            (job_id, solver, now),
        )
        return job_id

    def claim_next(self) -> Optional[Dict[str, Any]]:
        """Atomically claim the oldest ``queued`` job and mark it ``running``.

        Returns a dict with ``job_id``, ``solver``, and ``lp_path`` (the
        path to the job's LP temp file), or ``None`` if no job is
        queued. The claim is done inside its own transaction so that
        concurrent callers (multiple solve subprocesses / the API
        process) never claim the same job twice.
        """
        self._conn.execute("BEGIN IMMEDIATE;")
        try:
            row = self._conn.execute(
                "SELECT job_id, solver FROM jobs WHERE status = 'queued' "
                "ORDER BY submit_ts ASC LIMIT 1"
            ).fetchone()
            if row is None:
                self._conn.execute("COMMIT;")
                return None

            job_id = row["job_id"]
            now = time.time()
            cur = self._conn.execute(
                "UPDATE jobs SET status = 'running', start_ts = ? "
                "WHERE job_id = ? AND status = 'queued'",
                (now, job_id),
            )
            if cur.rowcount == 0:
                # Lost the race to another claimant; nothing to return.
                self._conn.execute("COMMIT;")
                return None
            self._conn.execute("COMMIT;")
        except Exception:
            self._conn.execute("ROLLBACK;")
            raise

        return {
            "job_id": job_id,
            "solver": row["solver"],
            "lp_path": str(self._lp_path(job_id)),
        }

    def mark_running(self, job_id: str) -> None:
        """Explicitly transition *job_id* to ``running``.

        Idempotent: safe to call even if the job is already running
        (``start_ts`` is only set the first time). Used independently
        of :meth:`claim_next` when a caller already knows which job it
        is about to work on.
        """
        now = time.time()
        self._conn.execute(
            "UPDATE jobs SET status = 'running', "
            "start_ts = COALESCE(start_ts, ?) WHERE job_id = ?",
            (now, job_id),
        )

    def mark_done(self, job_id: str, result: Dict[str, Any]) -> None:
        """Transition *job_id* to ``done`` with *result*, and clean up its LP file."""
        now = time.time()
        self._conn.execute(
            "UPDATE jobs SET status = 'done', end_ts = ?, result_json = ?, "
            "error = NULL WHERE job_id = ?",
            (now, json.dumps(result), job_id),
        )
        self._remove_lp_file(job_id)

    def mark_error(self, job_id: str, error: str) -> None:
        """Transition *job_id* to ``error`` with *error*, and clean up its LP file."""
        now = time.time()
        self._conn.execute(
            "UPDATE jobs SET status = 'error', end_ts = ?, error = ? "
            "WHERE job_id = ?",
            (now, error, job_id),
        )
        self._remove_lp_file(job_id)

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Return the job dict for *job_id*, or ``None`` if unknown."""
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def sweep_expired(self, ttl_seconds: float = RESULT_TTL_SECONDS) -> List[str]:
        """Remove completed jobs (``done``/``error``) older than *ttl_seconds*.

        Removes both the database row and any leftover LP temp file
        (normally already deleted by :meth:`mark_done`/:meth:`mark_error`,
        but cleaned up here too in case a caller mutated the row some
        other way). Returns the list of removed ``job_id``\\s.
        """
        cutoff = time.time() - ttl_seconds
        placeholders = ",".join("?" * len(_TERMINAL_STATUSES))
        rows = self._conn.execute(
            f"SELECT job_id FROM jobs WHERE status IN ({placeholders}) "
            "AND end_ts IS NOT NULL AND end_ts < ?",
            (*_TERMINAL_STATUSES, cutoff),
        ).fetchall()
        job_ids = [r["job_id"] for r in rows]
        for job_id in job_ids:
            self._remove_lp_file(job_id)
            self._conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
        return job_ids

    def reap_orphans_on_startup(self) -> List[str]:
        """Transition any job left ``running`` to a definitive ``error``.

        Called once at service startup. A job stuck in ``running`` means
        its solver subprocess died along with the previous service
        process; there is no way to recover its result, so it is marked
        ``error`` with :data:`ORPHAN_ERROR_MESSAGE` rather than polling
        forever. Returns the list of reaped ``job_id``\\s.
        """
        now = time.time()
        rows = self._conn.execute(
            "SELECT job_id FROM jobs WHERE status = 'running'"
        ).fetchall()
        job_ids = [r["job_id"] for r in rows]
        for job_id in job_ids:
            self._conn.execute(
                "UPDATE jobs SET status = 'error', end_ts = ?, error = ? "
                "WHERE job_id = ?",
                (now, ORPHAN_ERROR_MESSAGE, job_id),
            )
            self._remove_lp_file(job_id)
        return job_ids

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()
