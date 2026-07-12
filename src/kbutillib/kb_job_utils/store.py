"""SQLite-backed local job store.

Persists :class:`JobRecord` objects to ``~/.kbjobs/kbjobs.db`` so that
job metadata survives process restarts and can be queried offline.

Schema versions:
    1 -- Phase 1: ``jobs`` table only.
    2 -- Phase 3: adds ``pipelines`` table + indexes.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .pipeline import ChainStep, PipelineState, PipelineStatus
from .state import JobRecord, JobState

_DEFAULT_DB_DIR = Path.home() / ".kbjobs"
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "kbjobs.db"

_CURRENT_SCHEMA_VERSION = 2

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

_CREATE_PIPELINES_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS pipelines (
    pipeline_id    TEXT PRIMARY KEY,
    name           TEXT,
    project        TEXT,
    tags_json      TEXT NOT NULL DEFAULT '[]',
    spec_json      TEXT NOT NULL,
    status         TEXT NOT NULL,
    current_step   INTEGER NOT NULL DEFAULT 0,
    total_steps    INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    last_advanced_at TEXT,
    finished_at    TEXT
);
"""

_CREATE_PIPELINES_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_pipelines_status ON pipelines(status);",
    "CREATE INDEX IF NOT EXISTS idx_pipelines_project ON pipelines(project);",
]


def _iso(dt: datetime) -> str:
    """Format a datetime as an ISO-8601 string."""
    return dt.isoformat()


def _parse_iso(text: str) -> datetime:
    """Parse an ISO-8601 string back to a timezone-aware datetime."""
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_iso_opt(text: Optional[str]) -> Optional[datetime]:
    """Parse an optional ISO-8601 string."""
    if text is None:
        return None
    return _parse_iso(text)


class JobStore:
    """Thin SQLite wrapper for local job records and pipeline records.

    Args:
        db_path: Path to the SQLite database file.  Defaults to
            ``~/.kbjobs/kbjobs.db``.  The parent directory is created
            automatically if it does not exist.

    Schema migration is handled automatically on connection:
    - A fresh database is initialised to the current schema version.
    - An older database is migrated forward one version at a time.
    - A database with a schema version newer than what this code knows
      raises :class:`RuntimeError`.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        if db_path is None:
            db_path = _DEFAULT_DB_PATH
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._init_schema()

    # ── schema management ──────────────────────────────────────────────

    def _init_schema(self) -> None:
        """Create or migrate the database schema."""
        version = self._get_schema_version()

        if version > _CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {version} is newer than the "
                f"maximum supported version {_CURRENT_SCHEMA_VERSION}. "
                f"Upgrade KBUtilLib to open this database."
            )

        if version == 0:
            # Fresh database — create everything at current version.
            self._conn.execute(_CREATE_TABLE_SQL)
            self._conn.execute(_CREATE_INDEX_SQL)
            self._conn.execute(_CREATE_PIPELINES_TABLE_SQL)
            for idx_sql in _CREATE_PIPELINES_INDEXES_SQL:
                self._conn.execute(idx_sql)
            self._conn.execute(
                f"PRAGMA user_version = {_CURRENT_SCHEMA_VERSION};"
            )
            self._conn.commit()
            return

        # Incremental migrations
        if version < 2:
            self._migrate_v1_to_v2()

        self._conn.commit()

    def _get_schema_version(self) -> int:
        """Return the current schema version from PRAGMA user_version.

        A brand-new database returns 0. If the ``jobs`` table already
        exists but user_version is 0, we treat it as version 1 (the
        Phase 1 schema predates the user_version pragma).
        """
        cur = self._conn.execute("PRAGMA user_version;")
        version = cur.fetchone()[0]
        if version == 0:
            # Check if jobs table already exists (Phase 1 DB without version pragma)
            cur2 = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs';"
            )
            if cur2.fetchone() is not None:
                return 1  # Pre-existing Phase 1 database
        return version

    def _migrate_v1_to_v2(self) -> None:
        """Migrate from schema v1 (jobs only) to v2 (+ pipelines table)."""
        self._conn.execute(_CREATE_PIPELINES_TABLE_SQL)
        for idx_sql in _CREATE_PIPELINES_INDEXES_SQL:
            self._conn.execute(idx_sql)
        self._conn.execute(
            f"PRAGMA user_version = {_CURRENT_SCHEMA_VERSION};"
        )

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

    def list_by_workspace(self, workspace_id: int) -> List[JobRecord]:
        """Return all records associated with *workspace_id*, newest first.

        Used by the narrative-provenance audit render
        (:meth:`~kbutillib.kb_ws_utils.KBWSUtilsImpl.append_app_run_audit`)
        to scope the audit block to a single workspace's jobs.
        """
        cur = self._conn.execute(
            "SELECT * FROM jobs WHERE workspace_id = ? ORDER BY updated_at DESC",
            (workspace_id,),
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

    # ── pipeline CRUD ─────────────────────────────────────────────────

    def _row_to_pipeline(self, row: sqlite3.Row) -> PipelineState:
        """Convert a database row to a PipelineState."""
        return PipelineState(
            pipeline_id=row["pipeline_id"],
            spec=[ChainStep.from_dict(s) for s in json.loads(row["spec_json"])],
            status=PipelineStatus(row["status"]),
            current_step=row["current_step"],
            total_steps=row["total_steps"],
            created_at=_parse_iso(row["created_at"]),
            last_advanced_at=_parse_iso_opt(row["last_advanced_at"]),
            finished_at=_parse_iso_opt(row["finished_at"]),
            name=row["name"],
            project=row["project"],
            tags=json.loads(row["tags_json"]),
        )

    def upsert_pipeline(self, state: PipelineState) -> None:
        """Insert or update a pipeline record."""
        self._conn.execute(
            """\
            INSERT INTO pipelines (pipeline_id, name, project, tags_json,
                                   spec_json, status, current_step,
                                   total_steps, created_at,
                                   last_advanced_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pipeline_id) DO UPDATE SET
                name             = excluded.name,
                project          = excluded.project,
                tags_json        = excluded.tags_json,
                spec_json        = excluded.spec_json,
                status           = excluded.status,
                current_step     = excluded.current_step,
                total_steps      = excluded.total_steps,
                last_advanced_at = excluded.last_advanced_at,
                finished_at      = excluded.finished_at
            """,
            (
                state.pipeline_id,
                state.name,
                state.project,
                json.dumps(state.tags),
                json.dumps([s.to_dict() for s in state.spec]),
                state.status.value,
                state.current_step,
                state.total_steps,
                _iso(state.created_at),
                _iso(state.last_advanced_at) if state.last_advanced_at else None,
                _iso(state.finished_at) if state.finished_at else None,
            ),
        )
        self._conn.commit()

    def get_pipeline(self, pipeline_id: str) -> Optional[PipelineState]:
        """Retrieve a single pipeline record by ID, or ``None``."""
        cur = self._conn.execute(
            "SELECT * FROM pipelines WHERE pipeline_id = ?", (pipeline_id,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_pipeline(row)

    def list_pipelines(
        self,
        *,
        status: Optional[PipelineStatus] = None,
        project: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> List[PipelineState]:
        """List pipeline records with optional filters.

        Default sort: ``created_at`` descending.
        """
        clauses: List[str] = []
        params: List[str] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(_iso(since))

        where = ""
        if clauses:
            where = "WHERE " + " AND ".join(clauses)

        sql = f"SELECT * FROM pipelines {where} ORDER BY created_at DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"

        cur = self._conn.execute(sql, params)
        return [self._row_to_pipeline(r) for r in cur.fetchall()]

    def delete_pipeline(self, pipeline_id: str) -> bool:
        """Delete a pipeline record. Returns True if a row was removed."""
        cur = self._conn.execute(
            "DELETE FROM pipelines WHERE pipeline_id = ?", (pipeline_id,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
