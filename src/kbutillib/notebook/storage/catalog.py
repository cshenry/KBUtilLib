"""SQLite catalog — DDL, connection management, and schema migration."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

CURRENT_SCHEMA_VERSION = 1

DDL = """\
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ============================================================
-- CATALOG META
-- ============================================================
CREATE TABLE IF NOT EXISTS catalog_meta (
    schema_version INTEGER NOT NULL,
    created_at     TIMESTAMP NOT NULL,
    project_name   TEXT
);

-- ============================================================
-- EXPERIMENTS
-- ============================================================
CREATE TABLE IF NOT EXISTS experiments (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK(kind IN ('sample','computation','external')),
    payload_json  TEXT NOT NULL,
    notebook      TEXT,
    description   TEXT,
    created_at    TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS experiment_parents (
    child_id   TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    parent_id  TEXT NOT NULL REFERENCES experiments(id),
    PRIMARY KEY (child_id, parent_id)
);
CREATE INDEX IF NOT EXISTS idx_experiment_parents_parent ON experiment_parents(parent_id);

-- ============================================================
-- STRAINS
-- ============================================================
CREATE TABLE IF NOT EXISTS strains (
    id              TEXT PRIMARY KEY,
    parent_genome   TEXT,
    description     TEXT
);
CREATE TABLE IF NOT EXISTS mutations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    strain_id         TEXT NOT NULL REFERENCES strains(id) ON DELETE CASCADE,
    kind              TEXT NOT NULL,
    target_kind       TEXT NOT NULL,
    target_id         TEXT NOT NULL,
    target_namespace  TEXT NOT NULL,
    source_organism   TEXT,
    source_gene       TEXT,
    description       TEXT
);
CREATE INDEX IF NOT EXISTS idx_mutations_strain ON mutations(strain_id);

-- ============================================================
-- VECTORS
-- ============================================================
CREATE TABLE IF NOT EXISTS vectors (
    id                TEXT PRIMARY KEY,
    experiment_id     TEXT NOT NULL REFERENCES experiments(id),
    type_domain       TEXT NOT NULL,
    type_scale        TEXT NOT NULL,
    type_projection   TEXT,
    entity_kind       TEXT NOT NULL,
    entity_namespace  TEXT NOT NULL,
    columns_json      TEXT NOT NULL,
    n_entities        INTEGER NOT NULL,
    n_columns         INTEGER NOT NULL,
    parquet_path      TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    derivation        TEXT,
    created_at        TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vectors_experiment ON vectors(experiment_id);
CREATE INDEX IF NOT EXISTS idx_vectors_type ON vectors(type_domain, type_scale);
CREATE INDEX IF NOT EXISTS idx_vectors_entity ON vectors(entity_kind, entity_namespace);
CREATE TABLE IF NOT EXISTS vector_parents (
    child_id   TEXT NOT NULL REFERENCES vectors(id) ON DELETE CASCADE,
    parent_id  TEXT NOT NULL REFERENCES vectors(id),
    PRIMARY KEY (child_id, parent_id)
);
CREATE INDEX IF NOT EXISTS idx_vector_parents_parent ON vector_parents(parent_id);

-- ============================================================
-- CACHE OBJECTS (generic blobs)
-- ============================================================
CREATE TABLE IF NOT EXISTS cache_objects (
    id             TEXT PRIMARY KEY,
    type           TEXT NOT NULL,
    blob_path      TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    n_bytes        INTEGER NOT NULL,
    metadata_json  TEXT,
    created_at     TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_type ON cache_objects(type);
CREATE INDEX IF NOT EXISTS idx_cache_hash ON cache_objects(content_hash);

-- ============================================================
-- ACCESS LOG (provenance)
-- ============================================================
CREATE TABLE IF NOT EXISTS access_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id         TEXT NOT NULL,
    object_kind       TEXT NOT NULL CHECK(object_kind IN ('cache','vector')),
    op                TEXT NOT NULL CHECK(op IN ('write','read','delete')),
    notebook          TEXT,
    cell_index        INTEGER,
    cell_source_hash  TEXT,
    content_hash      TEXT,
    timestamp         TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_access_object ON access_log(object_id, object_kind);
CREATE INDEX IF NOT EXISTS idx_access_notebook ON access_log(notebook, timestamp);
"""


class Catalog:
    """Manages the SQLite catalog connection and DDL."""

    def __init__(self, db_path: Path, project_name: Optional[str] = None) -> None:
        self.db_path = db_path
        self.project_name = project_name
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._open()
        return self._conn

    def _open(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        # PRAGMAs must be set before DDL
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        self._ensure_schema(conn)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create tables if needed and handle schema migration."""
        # Check if catalog_meta exists
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='catalog_meta'"
        ).fetchone()
        if row is None:
            # Fresh database — run full DDL
            conn.executescript(DDL)
            conn.execute(
                "INSERT INTO catalog_meta (schema_version, created_at, project_name) "
                "VALUES (?, ?, ?)",
                (CURRENT_SCHEMA_VERSION, datetime.now(timezone.utc).isoformat(), self.project_name),
            )
            conn.commit()
            return

        # Existing database — check version for future migration
        meta = conn.execute("SELECT schema_version FROM catalog_meta").fetchone()
        if meta is None:
            # Meta table exists but no rows — insert
            conn.execute(
                "INSERT INTO catalog_meta (schema_version, created_at, project_name) "
                "VALUES (?, ?, ?)",
                (CURRENT_SCHEMA_VERSION, datetime.now(timezone.utc).isoformat(), self.project_name),
            )
            conn.commit()
            return

        version = meta["schema_version"]
        if version < CURRENT_SCHEMA_VERSION:
            self._migrate(conn, version)

    def _migrate(self, conn: sqlite3.Connection, from_version: int) -> None:
        """Run incremental migrations. Extend as schema_version increases."""
        # No migrations yet — version 1 is the only version.
        conn.execute(
            "UPDATE catalog_meta SET schema_version = ?",
            (CURRENT_SCHEMA_VERSION,),
        )
        conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def log_access(
        self,
        *,
        object_id: str,
        object_kind: str,
        op: str,
        notebook: Optional[str] = None,
        cell_index: Optional[int] = None,
        cell_source_hash: Optional[str] = None,
        content_hash: Optional[str] = None,
    ) -> None:
        """Insert one row into the access_log table."""
        self.conn.execute(
            "INSERT INTO access_log "
            "(object_id, object_kind, op, notebook, cell_index, cell_source_hash, content_hash, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                object_id,
                object_kind,
                op,
                notebook,
                cell_index,
                cell_source_hash,
                content_hash,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()
