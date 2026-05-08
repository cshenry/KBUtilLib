"""SQLite-backed local job store.

Persists :class:`JobRecord` objects to ``~/.kbjobs/kbjobs.db`` so that
job metadata survives process restarts and can be queried offline.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .state import JobRecord, JobState

_DEFAULT_DB_DIR = Path.home() / ".kbjobs"
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "kbjobs.db"

_CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    method       TEXT NOT NULL DEFAULT '',
    params       TEXT NOT NULL DEFAULT '{}',
    state        TEXT NOT NULL DEFAULT 'created',
    workspace_id INTEGER,
    narrative_id INTEGER,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    ee2_raw      TEXT NOT NULL DEFAULT '{}',
    error_message TEXT,
    meta         TEXT NOT NULL DEFAULT '{}'
);
"""

_CREATE_INDEX_SQL = """\
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs (state);
"""


def _iso(dt: datetime) -> str:
    """Format a datetime as an ISO-8601 string."""
    return dt.isoformat()


def _parse_iso(text: str) -> datetime:
    """Parse an ISO-8601 string back to a timezone-aware datetime."""
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class JobStore:
    """Thin SQLite wrapper for local job records.

    Args:
        db_path: Path to the SQLite database file.  Defaults to
            ``~/.kbjobs/kbjobs.db``.  The parent directory is created
            automatically if it does not exist.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        if db_path is None:
            db_path = _DEFAULT_DB_PATH
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(_CREATE_TABLE_SQL)
        self._conn.execute(_CREATE_INDEX_SQL)
        self._conn.commit()

    # ── helpers ──────────────────────────────────────────────────────────

    def _row_to_record(self, row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            job_id=row["job_id"],
            method=row["method"],
            params=json.loads(row["params"]),
            state=JobState(row["state"]),
            workspace_id=row["workspace_id"],
            narrative_id=row["narrative_id"],
            created_at=_parse_iso(row["created_at"]),
            updated_at=_parse_iso(row["updated_at"]),
            ee2_raw=json.loads(row["ee2_raw"]),
            error_message=row["error_message"],
            meta=json.loads(row["meta"]),
        )

    # ── public API ───────────────────────────────────────────────────────

    def upsert(self, record: JobRecord) -> None:
        """Insert or update a job record."""
        record.updated_at = datetime.now(timezone.utc)
        self._conn.execute(
            """\
            INSERT INTO jobs (job_id, method, params, state, workspace_id,
                              narrative_id, created_at, updated_at,
                              ee2_raw, error_message, meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                method       = excluded.method,
                params       = excluded.params,
                state        = excluded.state,
                workspace_id = excluded.workspace_id,
                narrative_id = excluded.narrative_id,
                updated_at   = excluded.updated_at,
                ee2_raw      = excluded.ee2_raw,
                error_message= excluded.error_message,
                meta         = excluded.meta
            """,
            (
                record.job_id,
                record.method,
                json.dumps(record.params),
                record.state.value,
                record.workspace_id,
                record.narrative_id,
                _iso(record.created_at),
                _iso(record.updated_at),
                json.dumps(record.ee2_raw),
                record.error_message,
                json.dumps(record.meta),
            ),
        )
        self._conn.commit()

    def get(self, job_id: str) -> Optional[JobRecord]:
        """Retrieve a single job record by ID, or ``None``."""
        cur = self._conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def list_by_state(self, state: JobState) -> List[JobRecord]:
        """Return all records matching the given state."""
        cur = self._conn.execute(
            "SELECT * FROM jobs WHERE state = ? ORDER BY updated_at DESC",
            (state.value,),
        )
        return [self._row_to_record(r) for r in cur.fetchall()]

    def list_active(self) -> List[JobRecord]:
        """Return all records that are NOT in a terminal state."""
        terminal = tuple(s.value for s in JobState.terminal_states())
        placeholders = ",".join("?" * len(terminal))
        cur = self._conn.execute(
            f"SELECT * FROM jobs WHERE state NOT IN ({placeholders}) ORDER BY updated_at DESC",
            terminal,
        )
        return [self._row_to_record(r) for r in cur.fetchall()]

    def list_all(self) -> List[JobRecord]:
        """Return every stored job record, newest first."""
        cur = self._conn.execute("SELECT * FROM jobs ORDER BY updated_at DESC")
        return [self._row_to_record(r) for r in cur.fetchall()]

    def delete(self, job_id: str) -> bool:
        """Delete a job record.  Returns True if a row was removed."""
        cur = self._conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
