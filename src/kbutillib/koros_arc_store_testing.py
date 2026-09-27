"""A reference in-memory test double for :class:`KorosArcStore`.

This module ships from KBUtilLib on purpose. Two separate KIND apps
(a genome-annotation explorer and a model-analysis explorer) test against the
:class:`~kbutillib.koros_arc_store.store.KorosArcStore` interface. Without a fake
shipped from here, each app would write its own — and a consumer-written fake
drifts toward whatever makes the consumer's tests pass, so the divergence
surfaces only at integration time, in a third repo, as somebody else's build
breaking. A PERMISSIVE FAKE IS WORSE THAN NO FAKE: it lets a consumer's tests
pass against behaviour the real store rejects.

Accordingly :class:`FakeKorosArcStore` ENFORCES the same validation rules as the
real store, it does not merely accept input. It does so by DELEGATING every
validation decision to the same helper code the real store runs (a disabled
:class:`~kbutillib.koros_arc_store.run_db.RunDatabase` instance held only for its
validators). Identity is never hashed here: ``record_id``, ``analysis_id`` and
``run_uid`` arrive pre-derived on the :class:`AnalysisRecord`, exactly as they do
on the real store's write path — producers derive them through the SHARED
:func:`~kbutillib.koros_arc_store.derive_analysis_id` /
:func:`~kbutillib.koros_arc_store.derive_record_id` helpers, so neither store
reimplements a hash and two producers cannot disagree about identity. What the
fake replaces is storage: plain in-memory dictionaries, not SQLite (decision
S14). The DDL itself (WAL, indexes, the BEGIN IMMEDIATE transaction, db-path
precedence) is exercised only through the contract suite's REAL-store
parameterisation, where those assertions are marked not-applicable for the fake
explicitly.

Kept in a SEPARATE module from :mod:`kbutillib.koros_arc_store` so importing the
store never drags a test double into production code.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from .koros_arc_store import (
    AnalysisRecord,
    ArcRecord,
    ProjectRecord,
    RecordNotFound,
    RunDatabase,
    check_contract_version,
)


class FakeKorosArcStore:
    """In-memory, dict-backed reference double for :class:`KorosArcStore`.

    It implements the SAME interface the real store defines
    (:meth:`list_projects` / :meth:`list_arcs` / :meth:`read_arc`;
    :meth:`record_analysis` / :meth:`list_analyses` / :meth:`read_detail` /
    :meth:`delete_record`) and enforces the SAME validation rules, delegating
    every validation decision and identity derivation to the real store's own
    helper code. Seed it with :meth:`seed_project`, :meth:`seed_arc` and
    :meth:`record_analysis` without touching a filesystem.

    The blob boundary is made OBSERVABLE: :attr:`blob_reads` counts every time a
    detail blob is opened. :meth:`list_analyses` and the summary path open none;
    :meth:`read_detail` opens exactly one. The contract suite asserts on this
    counter because it is the rule most likely to be violated invisibly.
    """

    def __init__(
        self,
        *,
        known_kinds: Optional[set] = None,
        enabled: bool = True,
    ) -> None:
        # A disabled RunDatabase held ONLY for its validators and its kind
        # classifier — it never connects to SQLite (enabled=False and no method
        # that opens a connection is ever called). This is how the fake enforces
        # byte-identical validation to the real store instead of reimplementing
        # it. The db_path is explicit and never touched.
        self._validator = RunDatabase(
            db_path="/dev/null/koros_arc_store_fake_never_opened.sqlite",
            known_kinds=known_kinds,
            enabled=False,
        )
        self.enabled = enabled

        # Runs-tree seed data: {project_name: (ProjectRecord, {slug: ArcRecord})}.
        self._projects: Dict[str, ProjectRecord] = {}
        self._arcs: Dict[str, Dict[str, ArcRecord]] = {}

        # Run-database seed data, keyed by record_id.
        #   _rows[record_id]       -> AnalysisRecord (a deep copy, verbatim subject)
        #   _blobs[record_id]      -> detail dict (only when detail was supplied)
        #   _updated_at[record_id] -> the row's updated_at, tracked alongside the
        #       record because updated_at is a stored column, NOT a field on the
        #       AnalysisRecord dataclass (parity with the real store, whose
        #       _row_to_record likewise omits it). It drives the S25 tie-break.
        self._rows: Dict[str, AnalysisRecord] = {}
        self._blobs: Dict[str, Dict[str, Any]] = {}
        self._updated_at: Dict[str, str] = {}

        # Fail-soft counter, mirroring the real store's public attribute.
        self.failed_write_count: int = 0

        # Observable blob-boundary instrument: incremented ONLY in read_detail.
        self.blob_reads: int = 0

    # ── runs-tree seeding + enumeration ────────────────────────────────────────

    def seed_project(self, record: ProjectRecord) -> None:
        """Register a :class:`ProjectRecord` for :meth:`list_projects`/:meth:`get_project`."""
        self._projects[record.name] = record
        self._arcs.setdefault(record.name, {})

    def seed_arc(self, record: ArcRecord) -> None:
        """Register an :class:`ArcRecord` under its project for arc enumeration."""
        self._arcs.setdefault(record.project, {})[record.slug] = record
        # A seeded arc implies its project exists; create a minimal placeholder
        # if the caller seeded the arc without seeding the project first.
        if record.project not in self._projects:
            self._projects[record.project] = ProjectRecord(
                name=record.project,
                path=record.path.parent.parent if record.path is not None else None,  # type: ignore[arg-type]
                arc_count=0,
            )

    def list_projects(self) -> List[ProjectRecord]:
        """Return every seeded project, ordered by name ascending (real-store order)."""
        return sorted(self._projects.values(), key=lambda p: p.name)

    def get_project(self, name: str) -> ProjectRecord:
        """Return the project named *name* or raise :class:`RecordNotFound`."""
        try:
            return self._projects[name]
        except KeyError as exc:
            raise RecordNotFound(f"no project named {name!r}") from exc

    def list_arcs(self, project: str) -> List[ArcRecord]:
        """Return every arc for *project*, ordered by slug ascending.

        A known project with no arcs yields an empty list; an unknown project
        raises :class:`RecordNotFound`, matching the real store.
        """
        if project not in self._projects:
            raise RecordNotFound(f"no project named {project!r}")
        arcs = self._arcs.get(project, {})
        return sorted(arcs.values(), key=lambda a: a.slug)

    def read_arc(self, project: str, slug: str) -> ArcRecord:
        """Return the arc ``<project>/arcs/<slug>`` or raise :class:`RecordNotFound`.

        An invalid arc (absent/unparseable provenance) is seeded as an
        ``ArcRecord`` with ``valid=False`` and read back as-is — never a raise,
        matching the real store's fail-soft enumeration.
        """
        arcs = self._arcs.get(project)
        if arcs is None or slug not in arcs:
            raise RecordNotFound(f"no arc {slug!r} in project {project!r}")
        return arcs[slug]

    # ── run-database write path (validating, fail-soft AFTER validation) ────────

    @property
    def db(self):  # pragma: no cover - parity accessor
        """Expose the validator instance for parity with ``KorosArcStore.db``."""
        return self._validator

    def record_analysis(
        self,
        project: Optional[str],
        arc: Optional[str],
        record: AnalysisRecord,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Insert or upsert a run record, optionally with a detail blob.

        Validation is delegated to the real store's helper code and RAISES on a
        malformed record (contract-version gate, structural validation,
        artifact-URI normalisation) exactly as the real store does. Everything
        after a validated record is a pure in-memory dict write, so the
        fail-soft envelope has nothing to fail against here — but the immutability
        guard (S17) is a REJECTION and re-raises, mirroring the real store.

        Upsert semantics (S6): overwrite every mutable field, preserve the
        original ``created_at``, advance ``updated_at``. ``analysis_id`` and
        ``run_uid`` are immutable on an existing ``record_id``. A re-record with
        no ``detail`` leaves any existing blob in place.
        """
        # These RAISE — identical helpers to the real store, so identical codes.
        check_contract_version(record.contract_version)
        self._validator._validate_structural(record)
        artifacts = self._validator._normalize_artifacts(record.artifacts or {})
        unknown_kind = self._validator._classify_kind(record.kind)

        if not self.enabled:
            return

        existing = self._rows.get(record.record_id)
        if existing is not None:
            # Immutability guard (S17): analysis_id and run_uid are frozen.
            if (
                existing.analysis_id != record.analysis_id
                or existing.run_uid != record.run_uid
            ):
                from .koros_arc_store import RecordValidationError

                raise RecordValidationError(
                    "immutable_field_changed",
                    "analysis_id and run_uid are immutable on an existing record_id",
                )
            created_at = existing.created_at
        else:
            created_at = record.created_at

        # Store verbatim subject and normalised artifacts; a deep copy so the
        # caller mutating the record afterwards cannot reach into our storage.
        stored = copy.deepcopy(record)
        stored.artifacts = artifacts
        stored.unknown_kind = unknown_kind
        stored.created_at = created_at
        stored.project = project
        stored.arc = arc
        self._rows[record.record_id] = stored
        # updated_at is a stored column, not a dataclass field — track it aside.
        self._updated_at[record.record_id] = self._utc_now()

        # Detail blob only when supplied; else leave any existing one (S6).
        if detail is not None:
            self._blobs[record.record_id] = copy.deepcopy(detail)

    @staticmethod
    def _utc_now() -> str:
        """Return an updated_at timestamp in the real store's format (S32).

        Delegated to the same helper the real store uses so the format cannot
        drift; imported lazily to keep the module's import surface minimal.
        """
        from .koros_arc_store.run_db import _utc_now

        return _utc_now()

    def delete_record(self, record_id: str) -> bool:
        """Hard-delete a run and its blob; return ``False`` (never raise) if absent.

        Cascades to the detail blob, mirroring the real store's manual cascade.
        """
        if not self.enabled:
            return False
        if record_id not in self._rows:
            return False
        del self._rows[record_id]
        self._blobs.pop(record_id, None)
        self._updated_at.pop(record_id, None)
        return True

    # ── run-database read path (opens NO blob) ─────────────────────────────────

    def list_analyses(
        self,
        project: Optional[str],
        arc: Optional[str],
        kind: Optional[str] = None,
        analysis_id: Optional[str] = None,
        latest_only: bool = False,
    ) -> List[AnalysisRecord]:
        """List run records, opening NO detail blob (blob_reads is untouched).

        Filters combine conjunctively (S34); ``project``/``arc`` of ``None``
        match the unattributed rows. Ordering and ``latest_only`` follow the S25
        tie-break (created_at DESC, updated_at DESC, record_id DESC), matching
        the real store row-for-row.
        """
        rows = [
            rec
            for rec in self._rows.values()
            if rec.project == project
            and rec.arc == arc
            and (kind is None or rec.kind == kind)
            and (analysis_id is None or rec.analysis_id == analysis_id)
        ]
        # S25 tie-break: newest first by created_at, then updated_at, then id.
        rows.sort(
            key=lambda r: (
                r.created_at,
                self._updated_at.get(r.record_id, ""),
                r.record_id,
            ),
            reverse=True,
        )
        results = [copy.deepcopy(r) for r in rows]
        if latest_only:
            seen = set()
            latest: List[AnalysisRecord] = []
            for rec in results:  # already newest-first per S25
                if rec.analysis_id in seen:
                    continue
                seen.add(rec.analysis_id)
                latest.append(rec)
            return latest
        return results

    def read_detail(self, record_id: str) -> Optional[Dict[str, Any]]:
        """Open and return the one detail blob for *record_id*.

        Returns the decoded blob, ``None`` for a record that EXISTS but has no
        detail, and RAISES :class:`RecordNotFound` when the ``record_id`` itself
        is unknown (S38 — the two cases are not collapsed). Increments
        :attr:`blob_reads` — the observable proof this is the ONLY blob-opening
        call.
        """
        if record_id not in self._rows:
            raise RecordNotFound(f"no run record with id {record_id!r}")
        self.blob_reads += 1
        blob = self._blobs.get(record_id)
        if blob is None:
            return None
        return copy.deepcopy(blob)
