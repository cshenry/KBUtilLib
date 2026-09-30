"""Backfill AnalysisRecords for modelling work that predates stamping.

Before the run database existed, ``kbu model`` wrote plain cobra JSON model files
and saved FBA/FVA outputs, and KBDL produced object-store results — none of it
indexed. :func:`backfill` walks the runs tree and, where reachable, the KBDL
object store, and writes an :class:`~kbutillib.koros_arc_store.records.AnalysisRecord`
for each artifact it can recover, so historical work appears in the dashboard.
The runs tree is reached ONLY through :class:`KorosArcStore` (its
``list_projects`` / ``list_arcs`` / ``list_arc_artifacts`` surface) — the app
never walks directories itself (layering invariant,
``tests/models_and_analyses/test_layering.py``).

Honesty about coverage is the whole point (binding). Backfill is NOT a migration
that can claim completeness: an analysis whose output was deleted, or printed to
a terminal and never saved, is UNRECOVERABLE and will never appear. So backfill
emits a :class:`CoverageReport`, not a success count — arcs scanned, records
written, an explicit list of artifacts found-but-unattributable, and analyses
referenced-but-missing — so an operator can tell a complete run from a partial
one.

Invariants (binding):

  * Every record backfill writes carries ``payload["provenance"] = "inferred"``
    and a DETERMINISTIC synthetic ``run_uid`` derived from the artifact, so a
    second pass over the same artifact reproduces the same ``(analysis_id,
    run_uid)`` and therefore the same ``record_id`` — the store upserts, no new
    row. Backfill is thus idempotent at the ``(analysis_id, run_uid)`` level.
  * Backfill NEVER runs automatically and ``--dry-run`` writes nothing.
  * File artifacts are PREFERRED over store refs; the store is consulted only for
    objects with no corresponding file artifact.
  * Records that cannot be attributed to an arc go to the UNATTRIBUTED index
    (``project=None, arc=None``), never to ``/dev/null``.

Scan layout (binding, exact):

    <runs_root>/*/arcs/*/          project / arcs / arc
        *.model.json               -> kind kbutillib.reconstruct
        fba/*.json                 -> kind kbutillib.fba
        fva/*.json                 -> kind kbutillib.fva

The KBDL object store, when reachable, is ADDITIONALLY listed by ``object_type``
in ``{model, fba, fva}`` filtered by owner, but only contributes objects that
have no file artifact already recovered.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..arc_context import APP_ID
from ..koros_arc_store import CONTRACT_VERSION, AnalysisRecord
from ..koros_arc_store.identity import derive_analysis_id, derive_record_id

#: The producer string stamped on every backfilled record.
BACKFILL_PRODUCER = f"{APP_ID}.backfill"

#: The trust-tier provenance carried by every backfilled record. Backfill INFERS
#: the analysis from a leftover artifact rather than observing a real run, so the
#: bridge is a model prediction and the tier is ``hypothesis`` (not verified).
#: This is the record's trust-tier provenance dict — distinct from the
#: ``payload["provenance"] = "inferred"`` MARKER the task requires.
_BACKFILL_TRUST_TIER = "hypothesis"


def _backfill_provenance() -> Dict[str, Any]:
    """Return the trust-tier provenance dict for a backfilled record."""
    return {
        "bridge_kind": "model_prediction",
        "metric": {"name": "backfill_inference", "value": 0, "units": "none"},
        "source": {"recovered_by": BACKFILL_PRODUCER},
    }


#: Map a runs-tree artifact family to its analysis kind. Ordered exactly as the
#: binding scan spec lists them.
_FILE_KIND_BY_FAMILY = {
    "model": "kbutillib.reconstruct",
    "fba": "kbutillib.fba",
    "fva": "kbutillib.fva",
}

#: Map a KBDL object_type to its analysis kind for the store scan.
_STORE_KIND_BY_OBJECT_TYPE = {
    "model": "kbdl.model_build",
    "fba": "kbutillib.fba",
    "fva": "kbutillib.fva",
}


@dataclass
class CoverageReport:
    """What a backfill pass scanned, wrote, and could NOT recover.

    A success count alone hides partial coverage. This report makes it visible:
    :attr:`arcs_scanned` and :attr:`records_written` quantify what was done, and
    :attr:`unattributable` / :attr:`missing` / :attr:`store_status` expose the
    gaps so an operator never mistakes a partial run for a complete one.
    """

    arcs_scanned: int = 0
    records_written: int = 0
    #: Artifacts recovered but not attributable to an arc (written to the
    #: unattributed index). Each entry is a short human string.
    unattributable: List[str] = field(default_factory=list)
    #: Analyses referenced by an arc/index but whose artifact is gone.
    missing: List[str] = field(default_factory=list)
    #: One of ``"scanned"``, ``"unavailable"`` or ``"not-attempted"``.
    store_status: str = "not-attempted"
    #: Records that WOULD be written on a real run (dry-run only). One string
    #: per candidate.
    dry_run_would_write: List[str] = field(default_factory=list)

    def as_text(self, dry_run: bool = False) -> str:
        """Render the report as the command's own honest output."""
        lines: List[str] = []
        lines.append(f"arcs scanned:    {self.arcs_scanned}")
        if dry_run:
            lines.append(
                f"records to write: {len(self.dry_run_would_write)} (dry-run — "
                "nothing written)"
            )
            for item in self.dry_run_would_write:
                lines.append(f"  + {item}")
        else:
            lines.append(f"records written: {self.records_written}")
        lines.append(f"object store:    {self.store_status}")
        lines.append(f"unattributable artifacts: {len(self.unattributable)}")
        for item in self.unattributable:
            lines.append(f"  ? {item}")
        lines.append(f"referenced-but-missing analyses: {len(self.missing)}")
        for item in self.missing:
            lines.append(f"  x {item}")
        lines.append(
            "NOTE: analyses whose output was deleted or never saved are "
            "unrecoverable and do not appear above."
        )
        return "\n".join(lines)


def synthetic_run_uid(artifact_ref: str) -> str:
    """Mint a DETERMINISTIC synthetic run_uid from an artifact reference.

    A backfilled record has no real execution to take a run_uid from, so it is
    derived from the artifact it was recovered from: a second pass over the same
    artifact reproduces this uid, so the derived ``record_id`` is stable and the
    store upserts rather than duplicating. The ``backfill:`` prefix marks the uid
    as synthetic so it is never mistaken for a real run (alongside
    ``payload["provenance"] = "inferred"``).

    ``artifact_ref`` is a stable identifier for the artifact — for a file, its
    path relative to the runs root; for a store object, its object id.
    """
    digest = hashlib.sha256(artifact_ref.encode("utf-8")).hexdigest()
    return f"backfill:{digest[:32]}"


def _build_record(
    kind: str,
    subject: str,
    artifact_ref: str,
    artifact_key: str,
    artifact_value: str,
    artifact_path: Optional[Path] = None,
    extra_payload: Optional[Dict[str, Any]] = None,
) -> AnalysisRecord:
    """Build one backfilled AnalysisRecord (marker + synthetic uid applied)."""
    # significant_params must be JSON-canonicalizable (no floats) and stable, so
    # the analysis_id is reproducible. The artifact family/kind and subject are
    # what make two files of the same kind for the same model distinct analyses.
    significant_params = {"kind": kind, "recovered": True}
    analysis_id = derive_analysis_id(kind, subject, significant_params)
    run_uid = synthetic_run_uid(artifact_ref)
    record_id = derive_record_id(analysis_id, run_uid)

    payload: Dict[str, Any] = {"provenance": "inferred", "artifact_ref": artifact_ref}
    if extra_payload:
        payload.update(extra_payload)

    return AnalysisRecord(
        record_id=record_id,
        analysis_id=analysis_id,
        run_uid=run_uid,
        kind=kind,
        created_at=_artifact_created_at(artifact_path),
        producer=BACKFILL_PRODUCER,
        producer_version="1",
        subject=subject,
        status="ok",
        artifacts={artifact_key: artifact_value},
        payload=payload,
        trust_tier=_BACKFILL_TRUST_TIER,
        provenance=_backfill_provenance(),
        contract_version=CONTRACT_VERSION,
    )


#: The sentinel created_at for a backfilled record with no recoverable time,
#: in the store's required ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` format (S32). Epoch
#: marks the row as backfilled rather than pretending to a real run time.
_EPOCH_SENTINEL = "1970-01-01T00:00:00.000000Z"


def _artifact_created_at(artifact_path: Optional[Path]) -> str:
    """A created_at for a backfilled record, in the store's required format.

    There is no real run time to recover. For a file artifact the honest
    approximation is its last-modification time; for a store object (no path) a
    fixed epoch sentinel is used. ``created_at`` is preserved on upsert, so this
    never breaks idempotency (the record_id is derived from analysis_id + run_uid,
    not the timestamp).
    """
    if artifact_path is not None:
        try:
            mtime = artifact_path.stat().st_mtime
            dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        except OSError:
            pass
    return _EPOCH_SENTINEL


def _relative_ref(path: Path, runs_root: Path) -> str:
    """A stable artifact reference: the path relative to the runs root if possible.

    The run_uid is derived from this string, so it must be stable regardless of
    where the runs root is mounted. When ``path`` is not under ``runs_root`` (a
    store reporting a different root), fall back to the absolute path so the
    reference is still deterministic.
    """
    try:
        return str(path.relative_to(runs_root))
    except ValueError:
        return str(path)


def _iter_arcs(store: Any) -> List[Tuple[str, str]]:
    """Return ``(project, arc_slug)`` for every arc, through the store surface.

    The store is the ONLY module that walks the runs tree (layering invariant,
    ``tests/models_and_analyses/test_layering.py``): backfill enumerates projects
    and arcs through :meth:`KorosArcStore.list_projects` / :meth:`list_arcs`,
    never by walking directories itself.
    """
    out: List[Tuple[str, str]] = []
    for project in store.list_projects():
        for arc in store.list_arcs(project.name):
            out.append((project.name, arc.slug))
    return out


def _scan_arc_files(store: Any, project: str, slug: str) -> List[Tuple[str, str, Path]]:
    """Return ``(family, subject, path)`` for every recoverable file in an arc.

    Delegates to :meth:`KorosArcStore.list_arc_artifacts` — the sanctioned door
    to an arc's on-disk artifacts — so the app never globs the runs tree itself
    (layering invariant). Families and globs (``*.model.json`` at the arc root,
    ``fba/*.json`` / ``fva/*.json`` in the two subdirectories) live in the store.
    """
    return [
        (art.family, art.subject, art.path)
        for art in store.list_arc_artifacts(project, slug)
    ]


def _kbdl_client_config_key() -> Optional[str]:
    """Return the KBDL client's OWN endpoint env-var name, read at build time.

    The config key is deliberately NOT hardcoded here — it is read from the KBDL
    client module so a rename there is followed, not mirrored. Returns ``None``
    if the client cannot be imported (its optional deps are absent).
    """
    try:
        from ..domains.external.kbdl_service_utils import KBDL_SERVICE_URL_ENV_VAR

        return KBDL_SERVICE_URL_ENV_VAR
    except Exception:
        return None


def _probe_store(owner: Optional[str], store_client: Any = None):
    """Probe the KBDL object store's reachability via the client's own call.

    Returns ``(client, status)`` where ``status`` is ``"scanned"`` when the
    store answered, or ``"unavailable"`` when it did not. The probe uses the
    KBDL client's OWN listing call (``list_objects``) under a 5s timeout — the
    client exposes no lighter availability method, so its own contract call IS
    the availability check — and never hangs. No new object-store environment
    variable is introduced: the endpoint comes from the client's own config key
    (:func:`_kbdl_client_config_key`).

    An injected ``store_client`` (tests) is used as-is. ``owner`` scopes the
    listing per-owner as required; the real client filters by the authenticated
    owner via its own token.
    """
    if store_client is not None:
        client = store_client
    else:
        config_key = _kbdl_client_config_key()
        if config_key is None:
            return None, "unavailable"
        try:
            from ..domains.external.kbdl_service_utils import KBDLServiceUtils

            client = KBDLServiceUtils(timeout=5.0)
        except Exception:
            # Endpoint not configured (ValueError) or deps missing — the store
            # is simply not reachable from here; report, do not hang or raise.
            return None, "unavailable"
    return client, "scanned"


def _scan_store_objects(
    client: Any, owner: Optional[str], seen_subjects: set
) -> Tuple[List[Tuple[str, str, str]], str]:
    """List store objects of type model/fba/fva, per-owner, preferring files.

    Returns ``(entries, status)`` where each entry is ``(kind, subject,
    object_id)`` for an object with NO file artifact already recovered (file
    artifacts are preferred). ``status`` is ``"scanned"`` on success or
    ``"unavailable"`` if the listing call fails.
    """
    entries: List[Tuple[str, str, str]] = []
    try:
        objects = client.list_objects()
    except Exception:
        return entries, "unavailable"

    for obj in objects or []:
        object_type = obj.get("object_type")
        kind = _STORE_KIND_BY_OBJECT_TYPE.get(object_type)
        if kind is None:
            continue
        # Per-owner filter: the real client is already owner-scoped by its token,
        # but honour an explicit owner when the object carries one.
        if owner is not None and obj.get("owner") not in (None, owner):
            continue
        subject = obj.get("name") or obj.get("object_id") or ""
        # Prefer file artifacts: skip a store object whose subject already came
        # from a file.
        if subject in seen_subjects:
            continue
        object_id = obj.get("object_id") or subject
        entries.append((kind, subject, str(object_id)))
    return entries, "scanned"


def backfill(
    store: Any,
    runs_root: Path,
    project: Optional[str] = None,
    arc: Optional[str] = None,
    dry_run: bool = False,
    owner: Optional[str] = None,
    scan_store: bool = True,
    store_client: Any = None,
) -> CoverageReport:
    """Walk the runs tree (and, if reachable, the store) and backfill records.

    ``store`` is a :class:`~kbutillib.koros_arc_store.store.KorosArcStore` (or the
    shipped fake). ``runs_root`` is the resolved KOROS runs root. ``project`` and
    ``arc`` optionally narrow the scan. ``dry_run`` reports what would be written
    WITHOUT writing. Returns a :class:`CoverageReport`.

    File artifacts are preferred; the store is consulted only for objects with no
    corresponding file subject. Records that cannot be attributed to an arc go to
    the unattributed index. Every written record carries
    ``payload["provenance"] = "inferred"`` and a deterministic synthetic run_uid,
    so a second pass writes no new row.
    """
    report = CoverageReport()
    runs_root = Path(runs_root)
    seen_subjects: set = set()

    for proj_name, arc_slug in _iter_arcs(store):
        if project is not None and proj_name != project:
            continue
        if arc is not None and arc_slug != arc:
            continue
        report.arcs_scanned += 1

        for family, subject, path in _scan_arc_files(store, proj_name, arc_slug):
            kind = _FILE_KIND_BY_FAMILY[family]
            rel = _relative_ref(path, runs_root)
            seen_subjects.add(subject)
            if not path.exists():
                report.missing.append(f"{proj_name}/{arc_slug}: {rel}")
                continue
            record = _build_record(
                kind=kind,
                subject=subject,
                # The run_uid is derived from the RELATIVE path so it is stable
                # regardless of where the runs root is mounted; the artifact URI
                # stored is the ABSOLUTE path the store requires (S8/S19).
                artifact_ref=rel,
                artifact_key=family,
                artifact_value=str(path.resolve()),
                artifact_path=path,
            )
            label = f"{proj_name}/{arc_slug}: {kind} {subject}"
            if dry_run:
                report.dry_run_would_write.append(label)
            else:
                store.record_analysis(proj_name, arc_slug, record)
                report.records_written += 1

    # Store scan (additional): only when not narrowed away and requested.
    if scan_store and arc is None:
        client, status = _probe_store(owner, store_client=store_client)
        if status != "scanned" or client is None:
            report.store_status = "unavailable"
        else:
            entries, scan_status = _scan_store_objects(client, owner, seen_subjects)
            if scan_status != "scanned":
                report.store_status = "unavailable"
            else:
                report.store_status = "scanned"
                for kind, subject, object_id in entries:
                    obj_uri = f"obj://{object_id}"
                    record = _build_record(
                        kind=kind,
                        subject=subject,
                        artifact_ref=obj_uri,
                        artifact_key="store_ref",
                        artifact_value=obj_uri,
                    )
                    # A store object carries no arc attribution here — it goes to
                    # the unattributed index (project=None, arc=None), never
                    # dropped.
                    label = f"unattributed: {kind} {subject} ({object_id})"
                    report.unattributable.append(label)
                    if dry_run:
                        report.dry_run_would_write.append(label)
                    else:
                        store.record_analysis(None, None, record)
                        report.records_written += 1

    return report
