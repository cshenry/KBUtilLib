"""The per-user run database and the read/write API over it.

A single per-user SQLite database is the store both KIND apps read and write.
Its names are deliberately domain-neutral — a genome-annotation app and a
model-analysis app share one database, so nothing here is named for annotation,
genomes, models or flux.

Two-tier schema (the crux):

  * ``runs`` carries only NAVIGABLE dimensions plus PRECOMPUTED summary
    statistics — roughly one row per run. This is what a dashboard sorts,
    filters and aggregates on, so opening it must never touch a blob.
  * ``subject_detail`` carries the gene-/reaction-level detail as ONE JSON blob
    per record. :meth:`RunDatabase.read_detail` is the ONLY call that opens a
    blob; :meth:`RunDatabase.list_analyses` and every summary query must not.

Read/write asymmetry (S10, S22): a WRITE that fails logs a greppable warning,
increments an in-process counter and RETURNS — a dashboard gap is far cheaper
than a failed scientific run. A READ against a database that cannot be opened
RAISES — a failed read masquerading as "no data" is exactly the confusion the
no-hardcoded-fallback rule exists to prevent.

The engine is SQLite with a JSON (TEXT) blob column, chosen against the real
write pattern — a KBDL client writing while a dashboard reads. WAL is enabled at
initialisation so a reader is not blocked by the writer (S36).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .exceptions import RecordNotFound, RecordValidationError
from .identity import canonical_json, derive_analysis_id, derive_record_id
from .records import (
    BRIDGE_KINDS,
    STATUS_VALUES,
    TRUST_TIERS,
    AnalysisRecord,
)

# The logger name is pinned so tests can assert against it (S21).
logger = logging.getLogger("kbutillib.koros_arc_store")

# Stable, greppable prefix on every soft-failed write (S21).
SOFT_FAIL_PREFIX = "koros_arc_store_write_soft_fail:"

# Default database path when neither the constructor nor $KBDL_RUN_DB supplies
# one (S22).
DEFAULT_DB_RELPATH = Path(".kbdl") / "runs.sqlite"

# A well-formed kind is a namespace plus at least one segment (S5).
_KIND_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")

# ISO 8601 UTC with microseconds and a literal Z (S32).
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

# Artifact URI vocabulary (S19).
_ABS_PATH_RE = re.compile(r"^/")
_OBJ_URI_RE = re.compile(r"^obj://([A-Za-z0-9._-]+)$")

# Payload size smell threshold (S29): logged, never rejected.
_PAYLOAD_WARN_BYTES = 64 * 1024

# unknown_kind flag values (S43).
KIND_KNOWN = 0
KIND_UNKNOWN_WELLFORMED = 1
KIND_MALFORMED = 2


def resolve_db_path(db_path: Union[str, Path, None] = None) -> Path:
    """Resolve the run-database path by the pinned precedence (S22).

    Order: an explicit ``db_path`` argument, else ``$KBDL_RUN_DB`` when set and
    non-empty, else ``~/.kbdl/runs.sqlite``. The parent directory is NOT created
    here — that happens on the write path so failure takes the soft-fail route.
    """
    if db_path is not None:
        return Path(db_path)
    env = os.environ.get("KBDL_RUN_DB")
    if env:
        return Path(env)
    return Path.home() / DEFAULT_DB_RELPATH


def _utc_now() -> str:
    """Return the current UTC time as ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (S32)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _default_pod_enabled() -> bool:
    """Whether the database is enabled by default in this environment.

    Enabled by default on pods; configurable elsewhere. A pod is detected via
    ``KB_POD`` / ``KBDL_POD`` being truthy, and the explicit ``KBDL_RUN_DB_ENABLED``
    override wins over both.
    """
    override = os.environ.get("KBDL_RUN_DB_ENABLED")
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes", "on"}
    for var in ("KB_POD", "KBDL_POD"):
        if os.environ.get(var, "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False


class RunDatabase:
    """The SQLite-backed run database: schema, validation and the read/write API.

    :class:`~kbutillib.koros_arc_store.store.KorosArcStore` composes an instance
    of this and delegates its record methods to it, so the same names are
    reachable on the store the two apps hold. Kept separate here so the runs-tree
    enumeration and the run database stay legible as distinct concerns.
    """

    def __init__(
        self,
        db_path: Union[str, Path, None] = None,
        *,
        known_kinds: Optional[set] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self.db_path: Path = resolve_db_path(db_path)
        # known_kinds is IMMUTABLE for the store's lifetime (S23); copy it.
        self._known_kinds: Optional[frozenset] = (
            frozenset(known_kinds) if known_kinds is not None else None
        )
        self.enabled: bool = _default_pod_enabled() if enabled is None else enabled
        # In-process soft-fail counter the caller can read (S21).
        self.failed_write_count: int = 0
        self._initialized: bool = False

    # ── connection + schema ────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        """Open a connection to the database, creating the parent dir if needed.

        Directory or connection failure here is a WRITE concern for callers on
        the write path (which wrap this in the soft-fail handler) and a RAISE for
        callers on the read path (which let it propagate).
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create tables, pragmas and indexes if absent (idempotent, S36)."""
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                record_id                 TEXT PRIMARY KEY,
                analysis_id               TEXT NOT NULL,
                run_uid                   TEXT NOT NULL,
                project                   TEXT,
                arc_slug                  TEXT,
                subject                   TEXT NOT NULL,
                kind                      TEXT NOT NULL,
                producer                  TEXT NOT NULL,
                producer_version          TEXT NOT NULL,
                status                    TEXT NOT NULL,
                trust_tier                TEXT NOT NULL,
                provenance                TEXT NOT NULL,
                contract_version          INTEGER NOT NULL,
                unknown_kind              INTEGER NOT NULL DEFAULT 0,
                artifacts                 TEXT NOT NULL,
                payload                   TEXT,
                created_at                TEXT NOT NULL,
                updated_at                TEXT NOT NULL,
                subject_feature_count     INTEGER,
                method_count              INTEGER,
                consistency_overall       REAL,
                consistency_metric_version TEXT,
                ic_corpus_version         TEXT
            );

            CREATE TABLE IF NOT EXISTS subject_detail (
                record_id   TEXT PRIMARY KEY,
                detail_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_runs_analysis_created
                ON runs (analysis_id, created_at DESC);
            """
        )
        conn.commit()

    def _ensure_initialized_for_write(self, conn: sqlite3.Connection) -> None:
        if not self._initialized:
            self._ensure_schema(conn)
            self._initialized = True

    # ── validation ──────────────────────────────────────────────────────────

    def _classify_kind(self, kind: str) -> int:
        """Return the unknown_kind flag for *kind* (S5/S43).

        0 known; 1 well-formed but outside a supplied ``known_kinds``; 2
        malformed. A malformed or foreign kind is STORED AND COUNTED, never
        refused — one unknown kind must not stall a reader.
        """
        if not _KIND_RE.match(kind):
            return KIND_MALFORMED
        if self._known_kinds is not None and kind not in self._known_kinds:
            return KIND_UNKNOWN_WELLFORMED
        return KIND_KNOWN

    @staticmethod
    def _normalize_artifact_uri(name: str, uri: str) -> str:
        """Validate one artifact URI and normalise file URIs (S8/S19).

        Accepts an absolute POSIX path, ``file://`` or ``file:///`` (normalised
        to ``file://`` + absolute path), or ``obj://<object_id>``. Anything else
        raises ``RecordValidationError(bad_artifact_uri)``.
        """
        if not isinstance(uri, str) or not uri:
            raise RecordValidationError(
                "bad_artifact_uri", f"artifact {name!r} has a non-string or empty URI"
            )
        if uri.startswith("file://"):
            # file:// or file:/// both accepted; strip the scheme to the path.
            remainder = uri[len("file://") :]
            # file:///abs -> /abs ; file://host/abs is not a local path we accept
            if remainder.startswith("/"):
                path = remainder
            else:
                # file://something without a leading slash on the path body:
                # only the empty-host form (file://) yielding an abs path counts.
                raise RecordValidationError(
                    "bad_artifact_uri",
                    f"artifact {name!r} file URI is not an absolute path: {uri!r}",
                )
            return "file://" + path
        if _OBJ_URI_RE.match(uri):
            return uri
        if _ABS_PATH_RE.match(uri):
            return uri
        raise RecordValidationError(
            "bad_artifact_uri",
            f"artifact {name!r} has an unsupported URI: {uri!r}",
        )

    def _validate_provenance(self, trust_tier: str, provenance: Dict[str, Any]) -> None:
        """Validate the provenance dict against the tier (S26).

        Any tier other than ``verified`` REQUIRES a non-empty provenance dict
        carrying the bridge kind and its metric. ``bridge_kind`` names the
        BRIDGE (one of five values), never a tier.
        """
        if trust_tier == "verified":
            # verified may omit provenance; but if present, it must still be a dict.
            if provenance and not isinstance(provenance, dict):
                raise RecordValidationError(
                    "missing_provenance", "provenance must be a dict when present"
                )
            return
        if not isinstance(provenance, dict) or not provenance:
            raise RecordValidationError(
                "missing_provenance",
                f"tier {trust_tier!r} requires a non-empty provenance dict",
            )
        bridge_kind = provenance.get("bridge_kind")
        if not isinstance(bridge_kind, str) or bridge_kind not in BRIDGE_KINDS:
            raise RecordValidationError(
                "bad_bridge_kind",
                f"provenance bridge_kind must be one of {sorted(BRIDGE_KINDS)}, "
                f"got {bridge_kind!r}",
            )
        metric = provenance.get("metric")
        if (
            not isinstance(metric, dict)
            or "name" not in metric
            or "value" not in metric
        ):
            raise RecordValidationError(
                "missing_provenance",
                "provenance metric must be a dict with name and value",
            )

    def _validate_structural(self, record: AnalysisRecord) -> None:
        """Structural validation that REJECTS (raises) rather than flags.

        A record failing this is rejected; an unknown *kind* is not a structural
        failure (it is flagged and stored). Codes are stable (S37).
        """
        if not record.run_uid:
            raise RecordValidationError(
                "missing_run_uid", "run_uid is required and was empty"
            )
        if record.status not in STATUS_VALUES:
            raise RecordValidationError(
                "bad_status",
                f"status must be one of {sorted(STATUS_VALUES)} (lower-case), "
                f"got {record.status!r}",
            )
        if record.trust_tier not in TRUST_TIERS:
            raise RecordValidationError(
                "bad_trust_tier",
                f"trust_tier must be one of {sorted(TRUST_TIERS)}, "
                f"got {record.trust_tier!r}",
            )
        if not _TIMESTAMP_RE.match(record.created_at or ""):
            raise RecordValidationError(
                "bad_timestamp",
                f"created_at must be YYYY-MM-DDTHH:MM:SS.ffffffZ, "
                f"got {record.created_at!r}",
            )
        self._validate_provenance(record.trust_tier, record.provenance or {})

    def _normalize_artifacts(self, artifacts: Dict[str, str]) -> Dict[str, str]:
        if not isinstance(artifacts, dict):
            raise RecordValidationError(
                "bad_artifact_uri", "artifacts must be a dict[str, str]"
            )
        return {
            name: self._normalize_artifact_uri(name, uri)
            for name, uri in artifacts.items()
        }

    # ── the write path (fail-soft) ────────────────────────────────────────────

    def record_analysis(
        self,
        project: Optional[str],
        arc: Optional[str],
        record: AnalysisRecord,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Insert or upsert a run record, optionally with a detail blob.

        Structural validation (S37 codes) RAISES — a malformed record is the
        caller's bug and must surface. Everything after a validated record —
        database access, directory creation, the write itself — is FAIL-SOFT
        (S10): on failure it logs a greppable warning, increments
        :attr:`failed_write_count` and RETURNS, so a pipeline is never broken by
        a dashboard-store hiccup.

        Upsert semantics (S6): overwrite every mutable field and preserve the
        original ``created_at``; set ``updated_at`` to now. ``analysis_id`` and
        ``run_uid`` are IMMUTABLE — a write changing either on an existing
        ``record_id`` is REJECTED (S17 immutable_field_changed). A re-record with
        no ``detail`` leaves any existing blob in place.
        """
        # Validation raises — it is not part of the fail-soft envelope.
        self._validate_structural(record)
        artifacts = self._normalize_artifacts(record.artifacts or {})
        unknown_kind = self._classify_kind(record.kind)

        # payload size smell (S29): warn, never reject.
        payload_json = None
        if record.payload is not None:
            payload_json = json.dumps(record.payload)
            if len(payload_json.encode("utf-8")) > _PAYLOAD_WARN_BYTES:
                logger.warning(
                    "koros_arc_store_payload_large: record_id=%s bytes=%d",
                    record.record_id,
                    len(payload_json.encode("utf-8")),
                )

        provenance_json = json.dumps(record.provenance or {})
        artifacts_json = json.dumps(artifacts)
        detail_json = json.dumps(detail) if detail is not None else None
        now = _utc_now()

        if not self.enabled:
            # Disabled: silently a no-op, not a failure.
            return

        conn = None
        try:
            conn = self._connect()
            self._ensure_initialized_for_write(conn)
            existing = conn.execute(
                "SELECT analysis_id, run_uid, created_at FROM runs WHERE record_id = ?",
                (record.record_id,),
            ).fetchone()

            if existing is not None:
                # Immutability guard (S17): analysis_id and run_uid are frozen.
                if (
                    existing["analysis_id"] != record.analysis_id
                    or existing["run_uid"] != record.run_uid
                ):
                    raise RecordValidationError(
                        "immutable_field_changed",
                        "analysis_id and run_uid are immutable on an existing record_id",
                    )
                created_at = existing["created_at"]
            else:
                created_at = record.created_at

            conn.execute(
                """
                INSERT INTO runs (
                    record_id, analysis_id, run_uid, project, arc_slug, subject,
                    kind, producer, producer_version, status, trust_tier,
                    provenance, contract_version, unknown_kind, artifacts, payload,
                    created_at, updated_at, subject_feature_count, method_count,
                    consistency_overall, consistency_metric_version, ic_corpus_version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(record_id) DO UPDATE SET
                    project=excluded.project,
                    arc_slug=excluded.arc_slug,
                    subject=excluded.subject,
                    kind=excluded.kind,
                    producer=excluded.producer,
                    producer_version=excluded.producer_version,
                    status=excluded.status,
                    trust_tier=excluded.trust_tier,
                    provenance=excluded.provenance,
                    contract_version=excluded.contract_version,
                    unknown_kind=excluded.unknown_kind,
                    artifacts=excluded.artifacts,
                    payload=excluded.payload,
                    updated_at=excluded.updated_at,
                    subject_feature_count=excluded.subject_feature_count,
                    method_count=excluded.method_count,
                    consistency_overall=excluded.consistency_overall,
                    consistency_metric_version=excluded.consistency_metric_version,
                    ic_corpus_version=excluded.ic_corpus_version
                """,
                (
                    record.record_id,
                    record.analysis_id,
                    record.run_uid,
                    project,
                    arc,
                    record.subject,
                    record.kind,
                    record.producer,
                    record.producer_version,
                    record.status,
                    record.trust_tier,
                    provenance_json,
                    record.contract_version,
                    unknown_kind,
                    artifacts_json,
                    payload_json,
                    created_at,
                    now,
                    record.subject_feature_count,
                    record.method_count,
                    record.consistency_overall,
                    record.consistency_metric_version,
                    record.ic_corpus_version,
                ),
            )
            # Detail blob only when supplied; else leave any existing one (S6).
            if detail_json is not None:
                conn.execute(
                    """
                    INSERT INTO subject_detail (record_id, detail_json)
                    VALUES (?, ?)
                    ON CONFLICT(record_id) DO UPDATE SET detail_json=excluded.detail_json
                    """,
                    (record.record_id, detail_json),
                )
            conn.commit()
        except RecordValidationError:
            # Immutability violation is a rejection, not a soft failure — re-raise.
            if conn is not None:
                conn.close()
            raise
        except Exception as exc:  # noqa: BLE001 - deliberate fail-soft catch-all
            self.failed_write_count += 1
            logger.warning(
                "%s record_id=%s db_path=%s error=%r",
                SOFT_FAIL_PREFIX,
                record.record_id,
                self.db_path,
                exc,
            )
            return
        finally:
            if conn is not None:
                conn.close()

    def delete_record(self, record_id: str) -> bool:
        """Hard-delete a run row and its detail blob in one transaction (S24).

        Returns ``True`` when the runs row existed and was deleted, ``False``
        (never an error) when the id was absent — a delete is something a person
        does twice by accident. Manual cascade, not an FK pragma, so behaviour
        does not depend on ``PRAGMA foreign_keys``. This is a write, so it is
        fail-soft: a database that cannot be opened returns ``False``.
        """
        if not self.enabled:
            return False
        conn = None
        try:
            conn = self._connect()
            self._ensure_initialized_for_write(conn)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM subject_detail WHERE record_id = ?", (record_id,))
            cur = conn.execute("DELETE FROM runs WHERE record_id = ?", (record_id,))
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted
        except Exception as exc:  # noqa: BLE001 - fail-soft
            self.failed_write_count += 1
            logger.warning(
                "%s delete record_id=%s db_path=%s error=%r",
                SOFT_FAIL_PREFIX,
                record_id,
                self.db_path,
                exc,
            )
            return False
        finally:
            if conn is not None:
                conn.close()

    # ── the read path (raises on an unopenable database) ───────────────────────

    def _open_for_read(self) -> sqlite3.Connection:
        """Open the database for reading, RAISING if it cannot be opened (S10).

        A failed read must not masquerade as "no data". If the file does not
        exist we still open it (SQLite creates an empty file), initialise the
        schema, and return empty results — that is "empty", which is legitimate
        and distinct from "unopenable".
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        self._ensure_schema(conn)
        return conn

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> AnalysisRecord:
        """Build an :class:`AnalysisRecord` from a runs row — NEVER a blob."""
        return AnalysisRecord(
            record_id=row["record_id"],
            analysis_id=row["analysis_id"],
            run_uid=row["run_uid"],
            kind=row["kind"],
            created_at=row["created_at"],
            producer=row["producer"],
            producer_version=row["producer_version"],
            subject=row["subject"],
            status=row["status"],
            artifacts=json.loads(row["artifacts"]) if row["artifacts"] else {},
            payload=json.loads(row["payload"]) if row["payload"] else None,
            trust_tier=row["trust_tier"],
            provenance=json.loads(row["provenance"]) if row["provenance"] else {},
            contract_version=row["contract_version"],
            project=row["project"],
            arc=row["arc_slug"],
            subject_feature_count=row["subject_feature_count"],
            method_count=row["method_count"],
            consistency_overall=row["consistency_overall"],
            consistency_metric_version=row["consistency_metric_version"],
            ic_corpus_version=row["ic_corpus_version"],
            unknown_kind=row["unknown_kind"],
        )

    def list_analyses(
        self,
        project: Optional[str],
        arc: Optional[str],
        kind: Optional[str] = None,
        analysis_id: Optional[str] = None,
        latest_only: bool = False,
    ) -> List[AnalysisRecord]:
        """List run records for a project/arc, opening NO detail blob.

        Filters (S34): ``kind`` and ``analysis_id`` combine conjunctively;
        ``latest_only`` applies AFTER filtering and returns exactly one row per
        ``analysis_id`` — the newest by the S25 tie-break (created_at DESC,
        updated_at DESC, record_id DESC). ``project``/``arc`` of ``None`` match
        the unattributed rows (both columns NULL).
        """
        conn = self._open_for_read()
        try:
            clauses = []
            params: List[Any] = []
            if project is None:
                clauses.append("project IS NULL")
            else:
                clauses.append("project = ?")
                params.append(project)
            if arc is None:
                clauses.append("arc_slug IS NULL")
            else:
                clauses.append("arc_slug = ?")
                params.append(arc)
            if kind is not None:
                clauses.append("kind = ?")
                params.append(kind)
            if analysis_id is not None:
                clauses.append("analysis_id = ?")
                params.append(analysis_id)
            where = " AND ".join(clauses)
            # Note: SELECT lists only runs columns — subject_detail is never joined.
            rows = conn.execute(
                f"SELECT * FROM runs WHERE {where} "
                "ORDER BY created_at DESC, updated_at DESC, record_id DESC",
                params,
            ).fetchall()
        finally:
            conn.close()

        records = [self._row_to_record(r) for r in rows]
        if latest_only:
            seen = set()
            latest: List[AnalysisRecord] = []
            for rec in records:  # already newest-first per S25
                if rec.analysis_id in seen:
                    continue
                seen.add(rec.analysis_id)
                latest.append(rec)
            return latest
        return records

    def read_detail(self, record_id: str) -> Optional[Dict[str, Any]]:
        """Open and return the detail blob for *record_id* — the ONLY blob read.

        Returns the decoded blob, ``None`` for a record that EXISTS but has no
        detail (legitimate — detail is optional on write), and RAISES
        :class:`RecordNotFound` when the ``record_id`` itself is unknown (S38 —
        the two cases are deliberately not collapsed).
        """
        conn = self._open_for_read()
        try:
            exists = conn.execute(
                "SELECT 1 FROM runs WHERE record_id = ?", (record_id,)
            ).fetchone()
            if exists is None:
                raise RecordNotFound(f"no run record with id {record_id!r}")
            row = conn.execute(
                "SELECT detail_json FROM subject_detail WHERE record_id = ?",
                (record_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return json.loads(row["detail_json"])

    def has_index(self, name: str = "idx_runs_analysis_created") -> bool:
        """Return whether the named index exists — used by tests (S36)."""
        conn = self._open_for_read()
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                (name,),
            ).fetchone()
        finally:
            conn.close()
        return row is not None

    # ── identity helpers (re-exported for the shared-helper guarantee) ─────────

    @staticmethod
    def derive_analysis_id(kind: str, subject: str, significant_params: Any) -> str:
        """Delegate to the module's shared :func:`identity.derive_analysis_id`."""
        return derive_analysis_id(kind, subject, significant_params)

    @staticmethod
    def derive_record_id(analysis_id: str, run_uid: str) -> str:
        """Delegate to the module's shared :func:`identity.derive_record_id`."""
        return derive_record_id(analysis_id, run_uid)

    @staticmethod
    def canonical_json(value: Any) -> str:
        """Delegate to the module's shared :func:`identity.canonical_json`."""
        return canonical_json(value)
