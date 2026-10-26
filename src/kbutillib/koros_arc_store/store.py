"""Read-only access to the KOROS runs tree.

Provides :func:`resolve_runs_root` (the runs-root resolution chain) and
:class:`KorosArcStore` (enumeration of projects and arcs).

Nothing here is specific to any KIND app's domain, and nothing imports the
KING/KOROS backend or any of the five KING/KOROS repos — the module binds to
the documented on-disk contract, not to any upstream implementation.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, replace
from datetime import datetime, timezone

from .archive import ARTIFACT_KEY, archive_payload
from .conformance import CONTRACT_VERSION
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .exceptions import RecordNotFound, RunsRootResolutionError
from .records import (
    FILE_ABSENT,
    JSON_PARSE_ERROR,
    AnalysisRecord,
    ArcArtifact,
    ArcRecord,
    ProjectRecord,
    parse_provenance,
)
from .run_db import RunDatabase

PROVENANCE_FILENAME = "PROVENANCE.json"


def resolve_runs_root(runs_root: Union[str, Path, None] = None) -> Path:
    """Resolve the KOROS runs root using the pinned resolution chain.

    Order (first that resolves to an existing directory wins):

      1. an explicit ``runs_root`` argument;
      2. ``$KOROS_HOME/runs`` — ``$KOROS_HOME`` is the *workspace root*, and
         runs live in its ``runs/`` subdirectory;
      3. ``$KING_KOROS_RUNS``;
      4. otherwise raise :class:`RunsRootResolutionError`.

    There is deliberately no hard-coded fallback. A machine with no KOROS
    workspace is a normal machine, and the honest response is to raise so an
    app can render "misconfigured" distinctly from "empty". Only the two
    environment variables named above are consulted.
    """
    if runs_root is not None:
        candidate = Path(runs_root)
        if candidate.is_dir():
            return candidate
        raise RunsRootResolutionError(
            f"explicit runs root does not exist or is not a directory: {candidate}"
        )

    koros_home = os.environ.get("KOROS_HOME")
    if koros_home:
        candidate = Path(koros_home) / "runs"
        if candidate.is_dir():
            return candidate

    king_koros_runs = os.environ.get("KING_KOROS_RUNS")
    if king_koros_runs:
        candidate = Path(king_koros_runs)
        if candidate.is_dir():
            return candidate

    raise RunsRootResolutionError(
        "could not resolve a KOROS runs root from --runs-root, "
        "$KOROS_HOME/runs, or $KING_KOROS_RUNS"
    )


class KorosArcStore:
    """Read-only enumeration of the KOROS runs tree AND the run database.

    Two responsibilities live behind one class because both consumer apps hold a
    single ``KorosArcStore`` and reach both surfaces through it:

      * **Runs-tree enumeration** (read-only): projects are immediate
        subdirectories of the runs root; arcs are the subdirectories of
        ``<project>/arcs/``. Enumeration is defensive — a project with no
        ``arcs/`` directory reports zero arcs, and an arc with an absent or
        unparseable ``PROVENANCE.json`` yields a record marked invalid rather
        than raising. One bad arc must not break the listing.
      * **The run database** (read + write): a per-user SQLite store both apps
        share. Its record methods (:meth:`record_analysis`, :meth:`list_analyses`,
        :meth:`read_detail`, :meth:`delete_record`) are delegated to a
        :class:`~kbutillib.koros_arc_store.run_db.RunDatabase` so the two-tier
        schema and the fail-soft write path stay in one place.

    ``runs_root`` is resolved eagerly, so a caller in an environment with no KOROS
    workspace gets a :class:`RunsRootResolutionError` from the constructor — the
    honest "misconfigured" signal, distinct from "empty". The run database is a
    separate resource resolved from ``db_path``/``$KBDL_RUN_DB``; it is not
    touched until a record method is called.
    """

    def __init__(
        self,
        runs_root: Union[str, Path, None] = None,
        *,
        db_path: Union[str, Path, None] = None,
        known_kinds: Optional[set] = None,
        db_enabled: Optional[bool] = None,
    ) -> None:
        self.runs_root: Path = resolve_runs_root(runs_root)
        self._db = RunDatabase(
            db_path=db_path, known_kinds=known_kinds, enabled=db_enabled
        )

    # ── projects ───────────────────────────────────────────────────────────

    def list_projects(self) -> List[ProjectRecord]:
        """Return every project under the runs root, ordered by name ascending."""
        projects = [
            self._project_record(entry)
            for entry in self.runs_root.iterdir()
            if entry.is_dir()
        ]
        projects.sort(key=lambda p: p.name)
        return projects

    def get_project(self, name: str) -> ProjectRecord:
        """Return the project named *name* or raise :class:`RecordNotFound`."""
        path = self.runs_root / name
        if not path.is_dir():
            raise RecordNotFound(f"no project named {name!r} under {self.runs_root}")
        return self._project_record(path)

    def _project_record(self, path: Path) -> ProjectRecord:
        return ProjectRecord(
            name=path.name,
            path=path,
            arc_count=len(self._arc_dirs(path)),
        )

    # ── arcs ─────────────────────────────────────────────────────────────────

    def list_arcs(self, project: str) -> List[ArcRecord]:
        """Return every arc for *project*, ordered by slug ascending.

        A project with no ``arcs/`` directory (or an empty one) yields an empty
        list rather than raising.
        """
        project_path = self.runs_root / project
        if not project_path.is_dir():
            raise RecordNotFound(f"no project named {project!r} under {self.runs_root}")
        arcs = [
            self._read_arc_dir(project, arc_dir)
            for arc_dir in self._arc_dirs(project_path)
        ]
        arcs.sort(key=lambda a: a.slug)
        return arcs

    def read_arc(self, project: str, slug: str) -> ArcRecord:
        """Return the arc ``<project>/arcs/<slug>``.

        Slug lookup is an exact, case-sensitive match. If the arc directory
        does not exist, raises :class:`RecordNotFound`. If it exists but its
        ``PROVENANCE.json`` is absent or unparseable, returns an ``ArcRecord``
        with ``valid=False`` and an ``invalid_reason`` — never ``None``, never
        a raise.
        """
        arc_dir = self.runs_root / project / "arcs" / slug
        if not arc_dir.is_dir():
            raise RecordNotFound(
                f"no arc {slug!r} in project {project!r} under {self.runs_root}"
            )
        return self._read_arc_dir(project, arc_dir)

    # ── arc artifacts ──────────────────────────────────────────────────────────

    #: The recoverable artifact families under an arc and how to find them,
    #: exactly as the backfill scan contract lists them: a top-level
    #: ``*.model.json`` model file, and ``fba/*.json`` / ``fva/*.json`` outputs.
    _ARTIFACT_FAMILIES = (
        ("model", "", "*.model.json"),
        ("fba", "fba", "*.json"),
        ("fva", "fva", "*.json"),
    )

    def list_arc_artifacts(self, project: str, slug: str) -> List["ArcArtifact"]:
        """Return the recoverable file artifacts under one arc.

        This is the sanctioned door to an arc's on-disk artifacts: the store is
        the only module that walks the runs tree, so a consumer (e.g. backfill)
        enumerates artifacts through here rather than globbing the tree itself.

        Each :class:`ArcArtifact` carries the ``family`` (``model``/``fba``/
        ``fva``), the ``subject`` (the model/output stem), and the absolute
        ``path``. Families and globs are the backfill scan contract:
        ``*.model.json`` at the arc root and ``fba/*.json`` / ``fva/*.json`` in
        the two subdirectories. A missing subdirectory yields no artifacts for
        that family; it never raises.

        Raises :class:`RecordNotFound` if the arc directory does not exist.
        """
        arc_dir = self.runs_root / project / "arcs" / slug
        if not arc_dir.is_dir():
            raise RecordNotFound(
                f"no arc {slug!r} in project {project!r} under {self.runs_root}"
            )
        return self._arc_artifacts(arc_dir)

    @classmethod
    def _arc_artifacts(cls, arc_dir: Path) -> List["ArcArtifact"]:
        """Return ``ArcArtifact`` entries under a known-existing arc directory."""
        found: List[ArcArtifact] = []
        for family, subdir, pattern in cls._ARTIFACT_FAMILIES:
            base = arc_dir / subdir if subdir else arc_dir
            if not base.is_dir():
                continue
            for path in sorted(base.glob(pattern)):
                if not path.is_file():
                    continue
                if family == "model":
                    subject = path.name[: -len(".model.json")]
                else:
                    subject = path.stem
                found.append(ArcArtifact(family=family, subject=subject, path=path))
        return found

    # ── internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _arc_dirs(project_path: Path) -> List[Path]:
        """Return the arc directories under ``<project>/arcs/``.

        A project with no ``arcs/`` subdirectory returns an empty list; it must
        never raise. This is the common shape for one of the live projects.
        """
        arcs_dir = project_path / "arcs"
        if not arcs_dir.is_dir():
            return []
        return [entry for entry in arcs_dir.iterdir() if entry.is_dir()]

    def _read_arc_dir(self, project: str, arc_dir: Path) -> ArcRecord:
        """Build an ``ArcRecord`` for a known-existing arc directory."""
        slug = arc_dir.name
        prov_path = arc_dir / PROVENANCE_FILENAME

        if not prov_path.is_file():
            return ArcRecord(
                project=project,
                slug=slug,
                path=arc_dir,
                provenance=None,
                valid=False,
                invalid_reason=FILE_ABSENT,
            )

        try:
            data = json.loads(prov_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return ArcRecord(
                project=project,
                slug=slug,
                path=arc_dir,
                provenance=None,
                valid=False,
                invalid_reason=JSON_PARSE_ERROR,
            )

        provenance, reason = parse_provenance(data)
        if provenance is None:
            return ArcRecord(
                project=project,
                slug=slug,
                path=arc_dir,
                provenance=None,
                valid=False,
                invalid_reason=reason,
            )

        return ArcRecord(
            project=project,
            slug=slug,
            path=arc_dir,
            provenance=provenance,
            valid=True,
            invalid_reason=None,
        )

    # ── run database (delegated to RunDatabase) ────────────────────────────────

    @property
    def db(self) -> RunDatabase:
        """The underlying run database, exposed for path/counter introspection."""
        return self._db

    @property
    def failed_write_count(self) -> int:
        """Count of soft-failed writes on the run database (S21)."""
        return self._db.failed_write_count

    def record_analysis(
        self,
        project: Optional[str],
        arc: Optional[str],
        record: AnalysisRecord,
        detail: Optional[Dict[str, Any]] = None,
        raw: Any = None,
    ) -> None:
        """Record one run — see :meth:`RunDatabase.record_analysis`.

        When *raw* is given it is gzip-archived under the archive root and the
        record's ``artifacts["result"]`` is pointed at the file. **Pass it
        whenever the payload exists nowhere else.** KBDL job results EXPIRE and
        annotation/model results have no second copy anywhere (see
        :mod:`kbutillib.koros_arc_store.archive`); a summary-only index entry
        means the science is gone when the service forgets it.

        Archiving is additive and soft-fails: if it cannot write, the record is
        still stored, unchanged apart from carrying no pointer.
        """
        if raw is not None:
            dest = archive_payload(record.kind, record.run_uid, raw)
            if dest is not None:
                record = replace(
                    record,
                    artifacts={**(record.artifacts or {}), ARTIFACT_KEY: str(dest)},
                )
        self._db.record_analysis(project, arc, record, detail)

    def record_submission(
        self,
        job_id: str,
        job_type: Optional[str],
        project: Optional[str],
        arc_slug: Optional[str],
        submitted_at: Optional[str],
        host: Optional[str],
    ) -> None:
        """Record a KBDL job's submitting arc -- see :meth:`RunDatabase.record_submission`."""
        self._db.record_submission(
            job_id, job_type, project, arc_slug, submitted_at, host
        )

    def list_submissions(self) -> Dict[str, Any]:
        """Map job_id to (project, arc_slug) -- see :meth:`RunDatabase.list_submissions`."""
        return self._db.list_submissions()

    def list_analyses(
        self,
        project: Optional[str],
        arc: Optional[str],
        kind: Optional[str] = None,
        analysis_id: Optional[str] = None,
        latest_only: bool = False,
    ) -> List[AnalysisRecord]:
        """List run records — see :meth:`RunDatabase.list_analyses`. Opens no blob."""
        return self._db.list_analyses(
            project, arc, kind=kind, analysis_id=analysis_id, latest_only=latest_only
        )

    def read_detail(self, record_id: str) -> Optional[Dict[str, Any]]:
        """Open the one detail blob for *record_id* — see :meth:`RunDatabase.read_detail`."""
        return self._db.read_detail(record_id)

    def delete_record(self, record_id: str) -> bool:
        """Hard-delete a run and its blob — see :meth:`RunDatabase.delete_record`."""
        return self._db.delete_record(record_id)

    # ── descriptions: what a record / arc / project actually IS ────────────
    #
    # The index could always say WHICH arc a record belonged to and never WHAT
    # it was. These are the three surfaces that close that, added 2026-10-04.

    def describe_record(
        self,
        record_id: str,
        description: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Attach prose/detail to an existing record — see
        :meth:`RunDatabase.describe_record`. This is the only way to describe a
        record that already exists, which is every record written before
        2026-10-04."""
        return self._db.describe_record(record_id, description=description, detail=detail)

    def describe_arc(
        self, project: str, arc: str, description: str, updated_by: Optional[str] = None
    ) -> bool:
        """Describe one arc: what it is and how it fits the wider study."""
        return self._db.set_scope_description(project, arc, description, updated_by)

    def describe_project(
        self, project: str, description: str, updated_by: Optional[str] = None
    ) -> bool:
        """Describe a project."""
        return self._db.set_scope_description(project, None, description, updated_by)

    def get_description(
        self, project: str, arc: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """The stored description for a project (``arc=None``) or an arc."""
        return self._db.get_scope_description(project, arc)

    def list_descriptions(self) -> List[Dict[str, Any]]:
        """Every project/arc description — use for a describe-coverage sweep."""
        return self._db.list_scope_descriptions()

    def describe_coverage(self) -> Dict[str, Any]:
        """How much of the index is actually described. The honest scorecard.

        Asking arcs to fill descriptions in is only useful if you can see what
        is still blank, so this reports the gap rather than a total: arcs and
        projects present in the runs tree with no description, and records with
        a NULL one.
        """
        described = {
            (d["project"], d["arc"]) for d in self.list_descriptions()
        }
        arcs_total = arcs_described = 0
        for proj in self.list_projects():
            for a in self.list_arcs(proj.name):
                arcs_total += 1
                if (proj.name, a.slug) in described:
                    arcs_described += 1
        recs = self._db.list_analyses(None, None)
        for proj in self.list_projects():
            for a in self.list_arcs(proj.name):
                recs.extend(self._db.list_analyses(proj.name, a.slug))
        with_desc = sum(1 for r in recs if (r.description or "").strip())
        return {
            "arcs_total": arcs_total,
            "arcs_described": arcs_described,
            "arcs_undescribed": arcs_total - arcs_described,
            "projects_described": sum(1 for p, a in described if a is None),
            "records_total": len(recs),
            "records_described": with_desc,
            "records_undescribed": len(recs) - with_desc,
        }

    # ── arc data lifecycle (inventory / dump / purge / archive) ─────────────
    #
    # An arc's data lives in TWO places -- index rows in the run database, and
    # gzip archives on disk that those rows point at. Anything that retires an
    # arc has to move or remove BOTH, or it leaves either orphaned archives
    # consuming disk with nothing referencing them, or rows pointing at files
    # that are gone. These four methods are the sanctioned door; callers should
    # not walk the archive tree themselves.
    #
    # Every destructive method defaults to ``dry_run=True``. Deleting an index
    # row is irreversible and deleting an archive can destroy the ONLY copy of a
    # result (a KBDL job result cannot be re-fetched once it has expired), so
    # the caller has to ask for it twice.

    def arc_data_inventory(
        self, project: Optional[str], arc: Optional[str]
    ) -> Dict[str, Any]:
        """What data this arc holds: record counts by kind, and archive bytes.

        Read-only, and the thing to call before any dump or purge. Pass
        ``(None, None)`` for the unattributed index.
        """
        records = self._db.list_analyses(project, arc)
        by_kind: Dict[str, int] = {}
        archives: List[str] = []
        missing: List[str] = []
        total = 0
        for r in records:
            by_kind[r.kind] = by_kind.get(r.kind, 0) + 1
            uri = (r.artifacts or {}).get(ARTIFACT_KEY)
            if not uri:
                continue
            path = Path(str(uri).replace("file://", "", 1))
            if path.is_file():
                archives.append(str(path))
                total += path.stat().st_size
            else:
                # A row pointing at a vanished archive. Reported rather than
                # ignored: it means the only copy is already gone.
                missing.append(str(path))
        return {
            "project": project,
            "arc": arc,
            "records": len(records),
            "records_by_kind": by_kind,
            "archives": len(archives),
            "archive_bytes": total,
            "archive_paths": archives,
            "archives_missing": missing,
        }

    def dump_arc_data(
        self,
        project: Optional[str],
        arc: Optional[str],
        dest: Path | str,
        *,
        include_archives: bool = True,
        move: bool = False,
    ) -> Dict[str, Any]:
        """Export this arc's records (and archives) to *dest*. Non-destructive.

        Writes ``dest/manifest.json`` -- every record in full, so the dump is
        self-describing and restorable without this database -- and, when
        *include_archives*, the gzip payloads under ``dest/archives/``.

        *move* RELOCATES the archives instead of copying (``os.replace`` where
        possible, else copy-then-unlink) and is how "archive an arc" reclaims
        live-store disk. It leaves the index rows alone: call :meth:`purge_arc_data`
        for those, or use :meth:`archive_arc`, which sequences both correctly.
        """
        dest = Path(dest).expanduser()
        records = self._db.list_analyses(project, arc)
        dest.mkdir(parents=True, exist_ok=True)
        copied = moved = failed = 0

        if include_archives:
            adir = dest / "archives"
            adir.mkdir(exist_ok=True)
            for r in records:
                uri = (r.artifacts or {}).get(ARTIFACT_KEY)
                if not uri:
                    continue
                src = Path(str(uri).replace("file://", "", 1))
                if not src.is_file():
                    continue
                target = adir / src.parent.name / src.name
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if move:
                        try:
                            os.replace(src, target)
                        except OSError:
                            # Different filesystem -- copy, then unlink only
                            # after the copy is on disk, so an interrupted move
                            # never loses the single surviving copy.
                            shutil.copy2(src, target)
                            src.unlink()
                        moved += 1
                    else:
                        shutil.copy2(src, target)
                        copied += 1
                except Exception:  # noqa: BLE001 -- report, never abort the dump
                    failed += 1

        manifest = {
            "contract_version": CONTRACT_VERSION,
            "project": project,
            "arc": arc,
            "dumped_at": datetime.now(timezone.utc).isoformat(),
            "archives_relocated": bool(move),
            "records": [asdict(r) for r in records],
        }
        (dest / "manifest.json").write_text(json.dumps(manifest, indent=2))
        return {
            "dest": str(dest),
            "records": len(records),
            "archives_copied": copied,
            "archives_moved": moved,
            "archives_failed": failed,
            "manifest": str(dest / "manifest.json"),
        }

    def purge_arc_data(
        self,
        project: Optional[str],
        arc: Optional[str],
        *,
        delete_archives: bool = False,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """Delete this arc's index rows, and optionally its archives.

        IRREVERSIBLE once *dry_run* is False. ``delete_archives`` is a SECOND
        opt-in because an archive is frequently the only surviving copy -- an
        expired KBDL result cannot be re-fetched -- so dropping the index row
        while keeping the payload is the recoverable choice and is the default.

        Returns what was (or would be) removed.
        """
        records = self._db.list_analyses(project, arc)
        paths = [
            Path(str((r.artifacts or {}).get(ARTIFACT_KEY, "")).replace("file://", "", 1))
            for r in records
            if (r.artifacts or {}).get(ARTIFACT_KEY)
        ]
        freed = sum(p.stat().st_size for p in paths if p.is_file())
        deleted_rows = deleted_files = 0
        if not dry_run:
            for r in records:
                if self._db.delete_record(r.record_id):
                    deleted_rows += 1
            if delete_archives:
                for p in paths:
                    try:
                        if p.is_file():
                            p.unlink()
                            deleted_files += 1
                    except Exception:  # noqa: BLE001 -- best effort, keep going
                        pass
        return {
            "project": project,
            "arc": arc,
            "dry_run": dry_run,
            "records_matched": len(records),
            "records_deleted": deleted_rows,
            "archives_matched": len(paths),
            "archives_deleted": deleted_files,
            "bytes_freed": freed if (not dry_run and delete_archives) else 0,
            "bytes_recoverable": freed,
        }

    def archive_arc(
        self,
        project: Optional[str],
        arc: Optional[str],
        dest: Path | str,
        *,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """Retire an arc: move its data out of the live store to *dest*.

        This is the "user archived an arc" flow. Ordering is the whole point and
        is not negotiable: **dump first, verify the manifest landed, only then
        purge.** Purging first, or purging on a failed dump, destroys data that
        in the KBDL case cannot be re-fetched. If the dump does not produce a
        manifest, this refuses to purge and says so.
        """
        inv = self.arc_data_inventory(project, arc)
        if dry_run:
            return {"dry_run": True, "would_dump_to": str(Path(dest).expanduser()),
                    "inventory": inv, "purged": False}
        dumped = self.dump_arc_data(project, arc, dest, include_archives=True, move=True)
        if not Path(dumped["manifest"]).is_file():
            return {"dry_run": False, "dumped": dumped, "purged": False,
                    "error": "dump produced no manifest; refusing to purge"}
        purged = self.purge_arc_data(
            project, arc, delete_archives=False, dry_run=False
        )  # archives were MOVED by the dump, so nothing left to delete
        return {"dry_run": False, "inventory": inv, "dumped": dumped,
                "purged": purged}
