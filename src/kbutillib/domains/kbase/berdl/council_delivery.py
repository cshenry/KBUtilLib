"""COUNCIL-DELIVERY v1 and the restartable scanner that drives the archiver.

THE RAIL. A completed run's store snapshot plus a manifest is dropped into a
Dropbox job folder. Dropbox syncs it to kbhub, whose pod can see the folder. A
restartable loop there picks it up, verifies it, archives it, publishes it and
records the two outcomes separately.

THIS MODULE IS DELIBERATELY SEPARATE FROM :mod:`council_archive`: it is the
piece with NO DATABASE IN IT and therefore the piece most worth testing alone.
Everything about batching, ledgering, resume and reconciliation stays in the
archiver and is CALLED from here -- ``archive_snapshot``, ``ArchiveLedger``,
``delivery_state_dir``, ``reconcile_delivery``, ``receipt``. Nothing is
re-implemented.

WHY A NEW MANIFEST. The existing snapshot ``MANIFEST.json`` is a single
compressed/uncompressed checksum record for ONE file; it is not a multi-file
delivery descriptor and must not be pressed into service as one. v1
(:func:`build_manifest`) carries: ``delivery_id``; ``store_id``;
``snapshot_id`` and ``snapshot_file``; ``snapshot_seq``; the snapshot's schema
version; ``store_host``; producer ``created_at`` AND ``deadline_at``, both
UTC; a RELATIVE file inventory of ``(path, bytes, sha256)``; and optionally
the publication-bundle identity.

(``snapshot_id``, ``snapshot_file`` and ``store_host`` are additions to the
field list the design named, and all three are forced rather than chosen. The
archiver keys every archived row AND its whole delivery-state directory on
``(store_id, snapshot_id)`` (``council_archive.delivery_state_dir``), and
``prepare_snapshot`` verifies the uncompressed digest against a declared
``snapshot_id`` before anything is archived -- so a delivery that could not
say which of its files is the snapshot, or what that snapshot's verified
digest is, could not be archived at all. ``store_host`` is in the archive's
own identity block on every row -- "where the snapshot was taken" -- and the
delivery is the ONLY thing in the rail that knows it; without it every row
archived through this loop would carry an empty host, silently.)

THE FOUR CONTRACT RULES, each here because of an observed failure:

1. A MANIFEST'S PRESENCE IS NOT PROOF OF ARRIVAL. The trigger is "the manifest
   exists AND every file it lists is present and sha256-matches"
   (:func:`verify_arrival`). A sync client has already reported a directory up
   to date while one of three files was absent on the consumer, and nothing
   guarantees a sync client preserves the producer's write order -- so the
   marker can arrive BEFORE the bytes it describes.
2. A FIRST MISMATCH MEANS "STILL SYNCING": the outcome is ``retry``, and
   progress is PERSISTED (:class:`DeliveryProgress`) so a retry does not
   re-digest the files it has already verified. On a ~900 MB snapshot that is
   the difference between a cheap tick and a tick that re-reads the whole
   delivery every time.
3. PAST THE DEADLINE IT IS A FAILED DELIVERY, raised with evidence: which
   file, expected vs observed size, expected vs observed digest
   (:class:`DeliveryNotArrivedError`). Neither extreme is safe alone -- alarm
   on the first mismatch and you alarm on every normal sync lag; treat it as
   transient forever and a genuinely lost file waits silently. ONLY ELAPSED
   TIME distinguishes them.

   THE DEADLINE COMES FROM THE MANIFEST'S ``deadline_at``. For a LEGACY
   mtime-based manifest (no ``deadline_at``) the computed deadline is FROZEN
   ON FIRST OBSERVATION and persisted, because CONSUMER-SIDE MTIME CAN RESET
   ON SYNC: otherwise every tick re-reads a fresh mtime, a 6-hour deadline
   never expires, and a genuinely lost delivery waits forever while looking
   healthy.
4. ARCHIVAL AND PUBLICATION OUTCOMES ARE PERSISTED SEPARATELY and each
   incomplete stage is retried EVEN WHEN THE OTHER HAS A RECEIPT. An archive
   receipt can certify the archive while publication failed; skipping the
   delivery forever on the strength of that receipt loses the clearinghouse
   assertion silently. AND A RECEIPT ON DISK IS A CACHE WHOSE IDENTITY MUST
   MATCH THE MANIFEST DIGEST -- never an authority. A receipt whose
   ``delivery_digest`` disagrees is DISCARDED and its stage re-run; re-running
   is safe because the archiver's ledger resolves an already-landed batch from
   its ``load_batch_id`` rather than appending it twice.

THE SCANNER MUST SCAN, NOT WATCH. kbhub has NO USER SYSTEMD, so a restartable
daemon there is booked to a Jupyter startup routine and is restarted at
arbitrary points -- it CANNOT assume it observed every file arrival. So on
every tick :meth:`CouncilDeliveryScanner.tick` ENUMERATES the deliveries
present in the drop directory, resolves each against the archiver's load
ledger and the persisted outcomes, and completes whatever is incomplete.
Correctness comes from reconciling disk against the lakehouse, NEVER from
having seen an event. An event-driven design is wrong in a way that is
invisible in testing and shows up as a run that silently never loaded.

SAFE TO RUN CONCURRENTLY WITH ITSELF. Two instances racing to START the same
delivery is the hazard the archiver's own resume logic does not cover (it
refuses to double-WRITE a batch, with ``AmbiguousBatchError``, which is a
refusal and not a plan), so each delivery is claimed with an ``O_EXCL`` lock
file inside its state directory (:class:`DeliveryLock`). The loser SKIPS that
delivery this tick and picks it up on the next one.

THE REAL LATENCY IS A TICK INTERVAL PLUS A DROPBOX SYNC, NOT SECONDS. With
the default :data:`DEFAULT_POLL_SECONDS` of 300 and a ~900 MB snapshot to
sync, "the run finished" to "the rows are in the lake" is tens of minutes at
best. Nothing here is interactive and nothing should be written as though it
were.

STATE COMPOSES WITH THE ARCHIVER'S, it does not shadow it. Both live in
``council_archive.delivery_state_dir(store_id=..., snapshot_id=...,
root=state_root)``, so one delivery has ONE directory holding the archiver's
``delivery.json`` + ``archive_ledger.jsonl`` and this module's
:data:`PROGRESS_FILENAME` + :data:`OUTCOMES_FILENAME` + :data:`LOCK_FILENAME`.
``state_root`` honours the archiver's own ``KBU_COUNCIL_ARCHIVE_DIR``
override through ``resolve_state_root``.

OPERATOR INSTALLATION IS NOT A COMMITTED LOOP, AND THE REGISTRATION STEP IS A
NAMED GAP. A loop in a repository is not an installed restartable service.
:data:`OPERATOR_INSTALL` records what is KNOWN (kbhub has no user systemd;
restartable daemons there are booked to Jupyter startup routines; what
evidence demonstrates restart recovery) and states plainly that THIS
REPOSITORY DOES NOT CONTAIN the concrete registration command for that
facility -- it is nowhere in the tree. It is not guessed at here: a wrong
registration command reads exactly like a right one until the pod restarts and
nothing comes back. Albert owns that step.

NOTHING HERE IS VERIFIED AGAINST A LIVE BERDL POD. The transport has been
broken on kbhub since the 2026-10-19 image (KBUtilLib#91). Implemented and
tested locally against ``SqliteCouncilSink``; anything needing the pod is
AWAITING OPERATOR ACCEPTANCE and a mocked load is never an acceptance.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .council_archive import (
    ArchiveLedger,
    archive_snapshot,
    delivery_state_dir,
    prepare_snapshot,
    receipt as archive_receipt,
    reconcile_delivery,
    resolve_state_root,
)

__all__ = [
    "DELIVERY_SCHEMA_VERSION",
    "MANIFEST_FILENAME",
    "PROGRESS_FILENAME",
    "OUTCOMES_FILENAME",
    "LOCK_FILENAME",
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_LEGACY_DEADLINE",
    "MANIFEST_FIELDS",
    "STAGE_ARCHIVE",
    "STAGE_PUBLISH",
    "OUTCOME_OK",
    "OUTCOME_FAILED",
    "OUTCOME_NOT_APPLICABLE",
    "OPERATOR_INSTALL",
    "CouncilDeliveryError",
    "DeliveryManifestError",
    "DeliveryNotArrivedError",
    "ReceiptIdentityError",
    "DeliveryLocked",
    "file_sha256",
    "build_manifest",
    "manifest_digest",
    "write_manifest",
    "read_manifest",
    "DeliveryProgress",
    "DeliveryOutcomes",
    "DeliveryLock",
    "verify_arrival",
    "CouncilDeliveryScanner",
]


#: Bumped only on a BREAKING change to the manifest's field set. The digest
#: covers it, so a consumer that read a v1 delivery cannot be handed a v2 one
#: under the same identity.
DELIVERY_SCHEMA_VERSION = 1

#: The delivery descriptor's filename inside a drop directory. DELIBERATELY
#: NOT ``MANIFEST.json``: that name is already taken by the snapshot's own
#: single-file checksum record, which travels in the same directory, and two
#: different documents under one name is how a consumer ends up validating the
#: wrong one.
MANIFEST_FILENAME = "COUNCIL-DELIVERY.json"

#: Verification progress, in the delivery's state directory.
PROGRESS_FILENAME = "delivery_progress.json"

#: The two stage outcomes, in the delivery's state directory.
OUTCOMES_FILENAME = "delivery_outcomes.json"

#: The ``O_EXCL`` claim one scanner instance takes on one delivery.
LOCK_FILENAME = "scanner.lock"

#: Seconds between ticks. Five minutes, not seconds: see the module
#: docstring's latency paragraph. A delivery is a ~900 MB Dropbox sync.
DEFAULT_POLL_SECONDS = 300

#: How long after a LEGACY manifest's first observation the delivery is
#: considered failed. Only used when the manifest carries no ``deadline_at``,
#: and FROZEN on first observation -- never recomputed from a fresh mtime.
DEFAULT_LEGACY_DEADLINE = timedelta(hours=6)

#: Every field of a v1 manifest's content, in canonical order. Declared once:
#: the digest is taken over this content, and a second field list could
#: disagree with it, which would mean two identities for one delivery.
MANIFEST_FIELDS: tuple[str, ...] = (
    "schema_version",
    "delivery_id",
    "store_id",
    "snapshot_id",
    "snapshot_file",
    "snapshot_seq",
    "snapshot_schema_version",
    "store_host",
    "created_at",
    "deadline_at",
    "files",
    "bundle",
)

#: The two stages, persisted SEPARATELY and retried independently.
STAGE_ARCHIVE = "archive"
STAGE_PUBLISH = "publish"

OUTCOME_OK = "ok"
OUTCOME_FAILED = "failed"

#: What ``publish`` records when the delivery declares NO bundle identity.
#: Deliberately its own value and not :data:`OUTCOME_OK`: "there was nothing
#: to publish" and "the clearinghouse assertion landed" are different facts,
#: and conflating them is how a missing assertion reads as a finished
#: delivery.
OUTCOME_NOT_APPLICABLE = "not_applicable"

#: WHAT AN OPERATOR MUST DO, AND THE ONE THING THIS REPOSITORY CANNOT TELL
#: THEM. Held as data rather than prose in a docstring so the CLI and the
#: tests can read the same statement.
OPERATOR_INSTALL: dict[str, Any] = {
    "known": [
        "kbhub has NO user systemd. Do not propose or write a systemd unit: "
        "there is no user instance to install it into.",
        "A restartable daemon on kbhub is booked to a Jupyter startup "
        "routine, so the loop is restarted at arbitrary points and must "
        "reconcile disk against the lakehouse on every tick rather than "
        "relying on having seen a file arrive.",
        "The loop itself is `CouncilDeliveryScanner(...).run()`. It needs "
        "four explicit settings and takes no defaults from the environment "
        "beyond the archiver's own KBU_COUNCIL_ARCHIVE_DIR: drop_dir, "
        "state_root, scratch_dir and poll_seconds.",
        "A second instance is safe: each delivery is claimed with an O_EXCL "
        "lock in its state directory, and the loser skips it for one tick.",
    ],
    "evidence_of_restart_recovery": [
        "Drop a complete delivery and a second, INCOMPLETE one. Kill the "
        "loop. Start a FRESH instance. It must complete exactly the "
        "incomplete one, which demonstrates the reconcile-not-react "
        "property: the fresh process observed neither arrival.",
        "Delete the archive receipt from the delivery's state directory and "
        "run one tick. The receipt must be reproduced IDENTICALLY (modulo "
        "its issued_at) and the sink's row count must not change -- proving "
        "the receipt is re-exported from the load ledger rather than "
        "recomputed by re-archiving.",
        "`reconcile_delivery(sink, ArchiveLedger(state_dir))` must report "
        "`complete: true` for every delivery the loop reports as archived.",
    ],
    "GAP": (
        "THE CONCRETE REGISTRATION COMMAND FOR KBHUB'S JUPYTER STARTUP "
        "FACILITY IS NOT DETERMINABLE FROM THIS REPOSITORY. Nothing in the "
        "tree names that facility, its startup directory, or the form a "
        "registered routine takes -- there is no reference to a Jupyter "
        "startup path, an IPython profile startup directory or a pod "
        "init hook anywhere in it. It is NOT invented here: a wrong "
        "registration command reads exactly like a right one until the pod "
        "restarts and nothing comes back, which is the failure this whole "
        "scanner exists to make impossible. Albert owns determining and "
        "recording it; until then the loop is runnable by hand and is NOT "
        "an installed service."
    ),
}


class CouncilDeliveryError(RuntimeError):
    """A delivery could not be read, verified or completed."""


class DeliveryManifestError(CouncilDeliveryError):
    """A manifest is not a well-formed COUNCIL-DELIVERY v1 document.

    Refused rather than partially read. Every field this document carries is
    load-bearing -- the inventory is what makes arrival checkable, the
    deadline is what separates "still syncing" from "lost", and the identity
    is what a receipt is matched against -- so a manifest missing or
    misshaping any of them cannot be processed into a weaker but still valid
    delivery.
    """


class DeliveryNotArrivedError(CouncilDeliveryError):
    """The deadline passed and the delivery is still not fully present.

    Carries :attr:`evidence`: one entry per offending file with its expected
    and observed size and digest. "A file is missing" is not actionable on its
    own; "this file is 0 of 912,334,848 bytes" tells an operator whether the
    sync stalled or the producer wrote the wrong thing.
    """

    def __init__(self, message: str, evidence: Sequence[Mapping[str, Any]]) -> None:
        self.evidence = [dict(entry) for entry in evidence]
        super().__init__(message)


class ReceiptIdentityError(CouncilDeliveryError):
    """A persisted receipt's identity does not match the manifest's digest.

    A RECEIPT IS A CACHE, NEVER AN AUTHORITY. One left behind by a different
    delivery -- or by an edited manifest -- would certify work that was never
    done for THIS delivery, and the stage would then be skipped forever.
    """


class DeliveryLocked(CouncilDeliveryError):
    """Another scanner instance holds this delivery's claim."""


# --------------------------------------------------------------------------
# Digests and the manifest
# --------------------------------------------------------------------------


def file_sha256(path: Any, *, chunk_bytes: int = 1 << 20) -> str:
    """The sha256 of a file, read in chunks.

    Chunked because a delivered snapshot is ~900 MB and a pod has other
    tenants; reading it whole to hash it is a measurable, avoidable spike.
    """
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_bytes), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc(value: Any) -> datetime:
    """Parse a declared timestamp as an aware UTC instant, or refuse.

    A NAIVE timestamp is REFUSED rather than assumed to be UTC: the producer
    and the consumer are different machines in different timezones, and
    assuming would turn a 6-hour deadline into one that had already passed or
    was hours away, with no symptom either way.
    """
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value or "").strip()
        if not text:
            raise DeliveryManifestError("a required timestamp is empty")
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DeliveryManifestError(
                f"{text!r} is not an ISO-8601 timestamp. COUNCIL-DELIVERY "
                "timestamps are UTC ISO-8601; one that cannot be parsed "
                "cannot be compared to a deadline."
            ) from exc
    if moment.tzinfo is None:
        raise DeliveryManifestError(
            f"{value!r} carries no timezone. The producer and the consumer "
            "are different machines: a naive timestamp read as UTC would "
            "move a deadline by hours with no symptom."
        )
    return moment.astimezone(timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def build_manifest(
    drop_dir: Any,
    *,
    delivery_id: str,
    store_id: str,
    snapshot_id: str,
    snapshot_file: str,
    snapshot_seq: int,
    snapshot_schema_version: int,
    store_host: str = "",
    files: Iterable[Any] | None = None,
    created_at: Any = None,
    deadline_at: Any = None,
    deadline: timedelta | None = None,
    bundle: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a COUNCIL-DELIVERY v1 manifest by DIGESTING the files in *drop_dir*.

    The inventory is measured, never declared: a producer that typed a size
    would be describing what it meant to write.

    Args:
        drop_dir: The directory holding the delivery's files.
        delivery_id: This delivery's id. DECLARED, never derived from the
            directory name -- a renamed directory must not become a second
            delivery.
        store_id: The store's DECLARED archive identity.
        snapshot_id: The snapshot's VERIFIED UNCOMPRESSED digest. Every
            archived row is keyed on it.
        snapshot_file: Which inventory entry IS the snapshot, as a relative
            path. Required: the archiver has to be handed one file.
        snapshot_seq: The explicit monotonic sequence for this snapshot.
        snapshot_schema_version: The snapshot's store schema version.
        store_host: Where the snapshot was taken. Carried because it is in
            the archive's identity block on every row and the delivery is the
            only thing in the rail that knows it.
        files: Relative paths to inventory. Default: every file in
            *drop_dir*, recursively, except the manifest itself.
        created_at: Producer timestamp, UTC. Defaults to now.
        deadline_at: The deadline, UTC. Preferred over *deadline*.
        deadline: A duration from *created_at*, if no absolute deadline is
            given. Omit both to write a LEGACY manifest with no deadline at
            all, whose deadline the consumer freezes on first observation.
        bundle: Optional publication-bundle identity -- ``campaign_id``,
            ``bundle_digest`` and the relative ``path`` of the bundle file.

    Raises:
        DeliveryManifestError: a required field is blank, a listed file is
            absent, or *snapshot_file* is not in the inventory.
    """
    root = Path(drop_dir).expanduser()
    if not root.is_dir():
        raise DeliveryManifestError(f"{root}: no such drop directory")
    created = _utc(created_at) if created_at is not None else _utcnow()
    if deadline_at is not None:
        deadline_text: str | None = _utc(deadline_at).isoformat()
    elif deadline is not None:
        deadline_text = (created + deadline).isoformat()
    else:
        deadline_text = None

    if files is None:
        candidates = sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file() and path.name != MANIFEST_FILENAME
        )
    else:
        candidates = [str(entry) for entry in files]

    inventory: list[dict[str, Any]] = []
    for relative in candidates:
        target = root / relative
        if not target.is_file():
            raise DeliveryManifestError(
                f"{relative!r} is listed for this delivery but is not a file "
                f"under {root}. An inventory is a measurement, not a wish."
            )
        inventory.append(
            {
                "path": relative,
                "bytes": target.stat().st_size,
                "sha256": file_sha256(target),
            }
        )
    paths = [entry["path"] for entry in inventory]
    if str(snapshot_file) not in paths:
        raise DeliveryManifestError(
            f"snapshot_file {snapshot_file!r} is not in the inventory "
            f"({paths!r}). The archiver is handed ONE file; a delivery that "
            "cannot say which one is not archivable."
        )
    for label, value in (
        ("delivery_id", delivery_id),
        ("store_id", store_id),
        ("snapshot_id", snapshot_id),
    ):
        if not str(value or "").strip():
            raise DeliveryManifestError(
                f"{label} is required and is derived from nothing -- not from "
                "the directory name, not from the filename. Every archived "
                "row is keyed on store_id and snapshot_id."
            )
    manifest: dict[str, Any] = {
        "schema_version": DELIVERY_SCHEMA_VERSION,
        "delivery_id": str(delivery_id),
        "store_id": str(store_id),
        "snapshot_id": str(snapshot_id),
        "snapshot_file": str(snapshot_file),
        "snapshot_seq": int(snapshot_seq),
        "snapshot_schema_version": int(snapshot_schema_version),
        "store_host": str(store_host or ""),
        "created_at": created.isoformat(),
        "deadline_at": deadline_text,
        "files": inventory,
        "bundle": dict(bundle) if bundle else None,
    }
    if set(manifest) != set(MANIFEST_FIELDS):
        raise DeliveryManifestError(
            "build_manifest: the content does not match MANIFEST_FIELDS. "
            f"Extra: {sorted(set(manifest) - set(MANIFEST_FIELDS))!r}; "
            f"missing: {sorted(set(MANIFEST_FIELDS) - set(manifest))!r}."
        )
    return manifest


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    """The delivery's IDENTITY: sha256 over the canonicalised manifest.

    Canonical means sorted KEYS and no whitespace slack; list ELEMENTS keep
    their order. This is the value a persisted receipt is matched against, so
    an edited manifest produces a different identity and every cached outcome
    beside it stops counting -- which is the point.
    """
    payload = {key: manifest.get(key) for key in MANIFEST_FIELDS}
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_manifest(manifest: Mapping[str, Any], path: Any) -> Path:
    """Write a manifest ATOMICALLY, through a ``.partial`` rename.

    Atomic because the consumer polls this exact path: a reader that caught a
    half-written manifest would parse it -- or fail to -- and either way would
    be making a decision about a document nobody finished writing.
    """
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    with partial.open("w", encoding="utf-8") as handle:
        json.dump(dict(manifest), handle, indent=2, sort_keys=True, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    partial.replace(target)
    return target


def read_manifest(path: Any) -> dict[str, Any]:
    """Read and VALIDATE a COUNCIL-DELIVERY v1 manifest.

    Raises:
        DeliveryManifestError: the file is absent, is not JSON, is not an
            object, declares another schema version, omits a required field,
            or carries a malformed inventory entry.
    """
    target = Path(path).expanduser()
    if not target.is_file():
        raise DeliveryManifestError(f"{target}: no delivery manifest")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise DeliveryManifestError(f"{target}: not valid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise DeliveryManifestError(
            f"{target}: expected a JSON object, got {type(raw).__name__}"
        )
    version = raw.get("schema_version")
    if int(version or 0) != DELIVERY_SCHEMA_VERSION:
        raise DeliveryManifestError(
            f"{target}: schema_version is {version!r} and this build reads "
            f"{DELIVERY_SCHEMA_VERSION}. A delivery descriptor is not read "
            "best-effort: the field set IS the contract."
        )
    missing = [
        field
        for field in MANIFEST_FIELDS
        if field not in ("deadline_at", "bundle", "store_host")
        and raw.get(field) in (None, "")
    ]
    if missing:
        raise DeliveryManifestError(
            f"{target}: omits {missing!r}. ``deadline_at``, ``bundle`` and "
            "``store_host`` are the only fields that may be absent or empty "
            "-- an absent deadline is the LEGACY form, whose deadline the "
            "consumer freezes on first observation, and an empty store_host "
            "is an honest 'not captured' rather than a guess."
        )
    files = raw.get("files")
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)) or not files:
        raise DeliveryManifestError(
            f"{target}: ``files`` must be a non-empty list of "
            "(path, bytes, sha256) entries. A delivery with no inventory "
            "cannot have its arrival checked at all, which is the one thing "
            "this document exists for."
        )
    for entry in files:
        if not isinstance(entry, Mapping):
            raise DeliveryManifestError(
                f"{target}: an inventory entry is {type(entry).__name__}, "
                "not an object"
            )
        absent = [key for key in ("path", "bytes", "sha256") if entry.get(key) in (None, "")]
        if absent:
            raise DeliveryManifestError(
                f"{target}: inventory entry {dict(entry)!r} omits {absent!r}. "
                "All three are checked on arrival; a missing one would read "
                "as a satisfied check."
            )
        if ".." in str(entry["path"]).split("/") or str(entry["path"]).startswith("/"):
            raise DeliveryManifestError(
                f"{target}: inventory path {entry['path']!r} is not RELATIVE "
                "and contained. A delivery describes its own directory; an "
                "absolute or escaping path would have the consumer digest "
                "something nobody delivered."
            )
    # Parse the timestamps so a malformed one fails HERE rather than at the
    # moment a deadline is compared.
    _utc(raw["created_at"])
    if raw.get("deadline_at"):
        _utc(raw["deadline_at"])
    return dict(raw)


# --------------------------------------------------------------------------
# Persisted state: progress, outcomes, the lock
# --------------------------------------------------------------------------


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    with partial.open("w", encoding="utf-8") as handle:
        json.dump(dict(payload), handle, indent=2, sort_keys=True, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    partial.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


class DeliveryProgress:
    """One delivery's FROZEN deadline and its per-file verification progress.

    Two jobs, one file, because both answer "what did we already establish
    about this delivery":

    * THE FROZEN DEADLINE. Written on FIRST OBSERVATION and never rewritten.
      For a manifest carrying ``deadline_at`` this is simply that value
      recorded; for a LEGACY manifest it is
      ``mtime(manifest) + legacy_deadline`` computed ONCE. Consumer-side mtime
      can RESET on sync, so a deadline recomputed every tick never expires and
      a genuinely lost delivery waits forever while looking healthy.
    * THE VERIFIED SET. Files already digest-matched, by
      ``(path, bytes, sha256)``. A retry does not re-digest them, which on a
      ~900 MB snapshot is the difference between a cheap tick and one that
      re-reads the whole delivery.
    """

    def __init__(self, state_dir: Any) -> None:
        self.state_dir = Path(state_dir).expanduser()
        self.path = self.state_dir / PROGRESS_FILENAME

    def read(self) -> dict[str, Any]:
        return _read_json(self.path)

    def freeze_deadline(
        self,
        manifest: Mapping[str, Any],
        *,
        manifest_path: Any,
        legacy_deadline: timedelta = DEFAULT_LEGACY_DEADLINE,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return this delivery's deadline record, freezing it if it is new.

        IDEMPOTENT AND WRITE-ONCE. A record already on disk is returned
        unchanged even if the manifest's mtime has moved since -- that is the
        whole purpose.
        """
        existing = self.read()
        if existing.get("deadline_at"):
            return existing
        declared = manifest.get("deadline_at")
        if declared:
            record = {
                "deadline_at": _utc(declared).isoformat(),
                "deadline_source": "manifest",
                "first_observed_at": (now or _utcnow()).isoformat(),
                "verified": existing.get("verified") or {},
            }
        else:
            # THE LEGACY FORM. mtime is read EXACTLY ONCE, here.
            mtime = datetime.fromtimestamp(
                Path(manifest_path).expanduser().stat().st_mtime, tz=timezone.utc
            )
            record = {
                "deadline_at": (mtime + legacy_deadline).isoformat(),
                "deadline_source": "legacy-mtime",
                "observed_mtime": mtime.isoformat(),
                "first_observed_at": (now or _utcnow()).isoformat(),
                "verified": existing.get("verified") or {},
            }
        _write_json(self.path, record)
        return record

    def verified(self) -> dict[str, dict[str, Any]]:
        record = self.read().get("verified")
        return dict(record) if isinstance(record, Mapping) else {}

    def record_verified(self, entries: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        """Merge newly-verified files into the persisted set."""
        record = self.read()
        verified = dict(record.get("verified") or {})
        verified.update({str(k): dict(v) for k, v in entries.items()})
        record["verified"] = verified
        _write_json(self.path, record)
        return record


class DeliveryOutcomes:
    """The archive and publication outcomes, persisted SEPARATELY.

    Each stage carries its own state, its own receipt and the DELIVERY
    IDENTITY it was established for. A stage whose recorded identity does not
    match the manifest's digest is DISCARDED on read (and reported), because a
    receipt is a cache and never an authority -- one left by a different
    delivery would certify work nobody did for this one and the stage would be
    skipped forever.
    """

    def __init__(self, state_dir: Any) -> None:
        self.state_dir = Path(state_dir).expanduser()
        self.path = self.state_dir / OUTCOMES_FILENAME

    def read_raw(self) -> dict[str, Any]:
        return _read_json(self.path)

    def read(self, *, identity: str) -> dict[str, Any]:
        """Stages whose recorded identity matches, plus the rejected names.

        Returns ``{"stages": {...}, "rejected": [stage, ...]}``.
        """
        raw = self.read_raw()
        stages: dict[str, Any] = {}
        rejected: list[str] = []
        for stage in (STAGE_ARCHIVE, STAGE_PUBLISH):
            record = raw.get(stage)
            if not isinstance(record, Mapping):
                continue
            if str(record.get("delivery_digest") or "") != str(identity):
                rejected.append(stage)
                continue
            stages[stage] = dict(record)
        return {"stages": stages, "rejected": rejected}

    def require(self, stage: str, *, identity: str) -> dict[str, Any]:
        """One stage's record, or raise if it is there under another identity.

        Raises:
            ReceiptIdentityError: the record exists and was established for a
                DIFFERENT delivery digest.
        """
        raw = self.read_raw()
        record = raw.get(stage)
        if not isinstance(record, Mapping):
            return {}
        found = str(record.get("delivery_digest") or "")
        if found != str(identity):
            raise ReceiptIdentityError(
                f"the persisted {stage!r} outcome in {self.path} was "
                f"established for delivery digest {found!r} and this manifest "
                f"is {identity!r}. A receipt is a CACHE, never an authority: "
                "trusting this one would certify work that was never done for "
                "this delivery and the stage would be skipped forever."
            )
        return dict(record)

    def record(
        self,
        stage: str,
        *,
        identity: str,
        state: str,
        receipt: Mapping[str, Any] | None = None,
        detail: str | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        raw = self.read_raw()
        raw[stage] = {
            "state": str(state),
            "delivery_digest": str(identity),
            "receipt": dict(receipt) if receipt is not None else None,
            "detail": detail,
            "at": at or _utcnow().isoformat(),
        }
        _write_json(self.path, raw)
        return dict(raw[stage])

    def drop(self, stage: str) -> None:
        """Forget one stage's record. Used when it is rejected on identity."""
        raw = self.read_raw()
        if stage in raw:
            raw.pop(stage)
            _write_json(self.path, raw)


class DeliveryLock:
    """An ``O_EXCL`` claim on ONE delivery, for ONE tick.

    THE RACE THIS COVERS IS NOT THE ARCHIVER'S. ``archive_snapshot`` already
    refuses to double-WRITE a batch -- it resolves a batch left ``started``
    from its ``load_batch_id`` and raises ``AmbiguousBatchError`` on a partial
    landing -- but that is a REFUSAL, not a plan: two scanners starting the
    same delivery at the same instant would have one of them stop with an
    error. So they do not start it at the same instant.

    ``O_EXCL`` rather than ``fcntl`` because the lock has to work when the two
    instances are two PROCESSES on one pod and the state directory may be on a
    filesystem whose advisory locks are not trustworthy; a stale lock is
    reclaimed by age (:meth:`acquire`'s ``stale_after``) rather than by
    assuming a dead holder.
    """

    def __init__(self, state_dir: Any, *, holder: str | None = None) -> None:
        self.state_dir = Path(state_dir).expanduser()
        self.path = self.state_dir / LOCK_FILENAME
        self.holder = holder or f"{os.getpid()}"
        self._held = False

    def acquire(self, *, stale_after: timedelta = timedelta(hours=2)) -> bool:
        """Claim the delivery. ``False`` means somebody else holds it."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            handle = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = _utcnow() - datetime.fromtimestamp(
                    self.path.stat().st_mtime, tz=timezone.utc
                )
            except OSError:
                return False
            if age < stale_after:
                return False
            # A lock older than `stale_after` belonged to a process that is
            # gone (the pod restarted mid-tick, which is the normal case
            # here). Reclaimed by AGE and never by checking a pid: a pid on a
            # restarted pod can be alive and be somebody else entirely.
            try:
                self.path.unlink()
            except OSError:
                return False
            return self.acquire(stale_after=stale_after)
        with os.fdopen(handle, "w", encoding="utf-8") as writer:
            writer.write(json.dumps({"holder": self.holder, "at": _utcnow().isoformat()}))
        self._held = True
        return True

    def release(self) -> None:
        if self._held:
            self._held = False
            try:
                self.path.unlink()
            except OSError:
                pass

    def __enter__(self) -> "DeliveryLock":
        if not self.acquire():
            raise DeliveryLocked(f"{self.path} is held by another scanner")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


# --------------------------------------------------------------------------
# Arrival
# --------------------------------------------------------------------------


def verify_arrival(
    manifest: Mapping[str, Any],
    drop_dir: Any,
    *,
    progress: DeliveryProgress,
    manifest_path: Any,
    legacy_deadline: timedelta = DEFAULT_LEGACY_DEADLINE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Has every file the manifest lists arrived, and is it still allowed not to?

    THE TRIGGER IS NOT THE MANIFEST'S PRESENCE. A sync client has reported a
    directory up to date while one of three files was absent on the consumer,
    and nothing guarantees it preserves the producer's write order -- so the
    marker can arrive before the bytes it describes. Arrival means every
    listed file is present, is the declared size and sha256-matches.

    Returns:
        ``{"state": "complete"|"retry", "deadline_at", "deadline_source",
        "verified", "pending", "evidence", "checked", "reused"}``.
        ``state`` is ``retry`` for a FIRST mismatch: that is a normal sync
        lag, not an alarm.

    Raises:
        DeliveryNotArrivedError: the frozen deadline has passed and files are
            still missing or mismatched. Carries per-file expected vs observed
            size and digest.
    """
    root = Path(drop_dir).expanduser()
    moment = now or _utcnow()
    record = progress.freeze_deadline(
        manifest,
        manifest_path=manifest_path,
        legacy_deadline=legacy_deadline,
        now=moment,
    )
    deadline = _utc(record["deadline_at"])
    already = progress.verified()

    evidence: list[dict[str, Any]] = []
    newly: dict[str, dict[str, Any]] = {}
    verified: list[str] = []
    reused: list[str] = []
    for entry in manifest["files"]:
        relative = str(entry["path"])
        expected_bytes = int(entry["bytes"])
        expected_sha = str(entry["sha256"])
        cached = already.get(relative)
        if (
            isinstance(cached, Mapping)
            and int(cached.get("bytes") or -1) == expected_bytes
            and str(cached.get("sha256") or "") == expected_sha
        ):
            # Already established for THIS (path, bytes, sha256). Progress is
            # persisted precisely so a retry does not start from nothing.
            verified.append(relative)
            reused.append(relative)
            continue
        target = root / relative
        if not target.is_file():
            evidence.append(
                {
                    "path": relative,
                    "problem": "absent",
                    "expected_bytes": expected_bytes,
                    "observed_bytes": None,
                    "expected_sha256": expected_sha,
                    "observed_sha256": None,
                }
            )
            continue
        observed_bytes = target.stat().st_size
        if observed_bytes != expected_bytes:
            # Size first: a partially-synced 900 MB file is the common case
            # and digesting it to learn that is pure waste.
            evidence.append(
                {
                    "path": relative,
                    "problem": "size",
                    "expected_bytes": expected_bytes,
                    "observed_bytes": observed_bytes,
                    "expected_sha256": expected_sha,
                    "observed_sha256": None,
                }
            )
            continue
        observed_sha = file_sha256(target)
        if observed_sha != expected_sha:
            evidence.append(
                {
                    "path": relative,
                    "problem": "digest",
                    "expected_bytes": expected_bytes,
                    "observed_bytes": observed_bytes,
                    "expected_sha256": expected_sha,
                    "observed_sha256": observed_sha,
                }
            )
            continue
        verified.append(relative)
        newly[relative] = {"bytes": observed_bytes, "sha256": observed_sha}
    if newly:
        progress.record_verified(newly)

    result = {
        "deadline_at": record["deadline_at"],
        "deadline_source": record.get("deadline_source"),
        "verified": sorted(verified),
        "pending": [entry["path"] for entry in evidence],
        "evidence": evidence,
        "checked": len(manifest["files"]),
        "reused": sorted(reused),
    }
    if not evidence:
        return {**result, "state": "complete"}
    if moment >= deadline:
        raise DeliveryNotArrivedError(
            f"delivery {manifest['delivery_id']!r} of store "
            f"{manifest['store_id']!r} is PAST its deadline "
            f"({record['deadline_at']}, source "
            f"{record.get('deadline_source')!r}) with "
            f"{len(evidence)} of {len(manifest['files'])} file(s) still not "
            "arrived: "
            + "; ".join(
                f"{item['path']} ({item['problem']}): expected "
                f"{item['expected_bytes']} bytes / {item['expected_sha256'][:12]}..., "
                f"observed {item['observed_bytes']} bytes / "
                f"{(item['observed_sha256'] or 'none')[:12]}..."
                for item in evidence
            )
            + ". A first mismatch is a sync lag; past the deadline it is a "
            "FAILED DELIVERY, and only elapsed time distinguishes them.",
            evidence,
        )
    return {**result, "state": "retry"}


# --------------------------------------------------------------------------
# The scanner
# --------------------------------------------------------------------------


class CouncilDeliveryScanner:
    """ENUMERATES and RECONCILES the drop directory every tick. Never reacts.

    kbhub has no user systemd, so this loop is booked to a Jupyter startup
    routine and is restarted at arbitrary points -- it cannot assume it
    observed any file arrival. So every tick lists the deliveries present,
    resolves each against the archiver's load ledger and the persisted stage
    outcomes, and completes whatever is incomplete. An event-driven design is
    wrong in a way that is invisible in testing and shows up as a run that
    silently never loaded.

    THE REAL LATENCY IS A TICK INTERVAL PLUS A DROPBOX SYNC, not seconds:
    with the default 300-second interval and a ~900 MB snapshot, "the run
    finished" to "the rows are in the lake" is tens of minutes at best.

    THE LOAD IS NOT PERFORMED BY THIS CLASS AND IS NOT CLAIMED BY IT. It
    archives into the sink it is given and, when the delivery declares a
    bundle identity, runs the clearinghouse PRODUCER half (the shard run).
    ``kbu clearinghouse load`` stays the consumer's explicit verb.

    Args:
        drop_dir: The directory Dropbox syncs deliveries into. EXPLICIT.
        state_root: Root of the delivery state directories -- the SAME root
            the archiver uses, so one delivery has one state directory.
            Defaults through ``council_archive.resolve_state_root`` (which
            honours ``KBU_COUNCIL_ARCHIVE_DIR``).
        scratch_dir: Where a compressed snapshot is decompressed. EXPLICIT:
            the pod holds hundreds of GB but which filesystem absorbs ~900 MB
            is an operator's decision, not this code's.
        sink: A council sink (see :mod:`council_sink`).
        poll_seconds: Seconds between ticks.
        legacy_deadline: How long a LEGACY (no ``deadline_at``) delivery has,
            measured from the manifest's mtime AT FIRST OBSERVATION.
        publisher: ``(sink, bundle, *, publish_dir, state_root) -> receipt``.
            Defaults to
            :func:`~.council_publish.publish_from_lakehouse`, the
            lakehouse-sourced publisher.
        holder: A label for this instance, written into its lock files.
    """

    def __init__(
        self,
        *,
        drop_dir: Any,
        scratch_dir: Any,
        sink: Any,
        state_root: Any = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        legacy_deadline: timedelta = DEFAULT_LEGACY_DEADLINE,
        publisher: Callable[..., Mapping[str, Any]] | None = None,
        holder: str | None = None,
    ) -> None:
        self.drop_dir = Path(drop_dir).expanduser()
        self.scratch_dir = Path(scratch_dir).expanduser()
        self.state_root = resolve_state_root(state_root)
        self.sink = sink
        self.poll_seconds = float(poll_seconds)
        self.legacy_deadline = legacy_deadline
        self.publisher = publisher
        self.holder = holder or f"{os.uname().nodename}:{os.getpid()}"

    # -- enumeration ----------------------------------------------------

    def deliveries(self) -> list[Path]:
        """Every delivery directory present, by its manifest. SORTED.

        A directory with no manifest is not a delivery: the producer writes
        the manifest, and until it is there nothing says what the directory is
        supposed to contain. A directory with one IS a delivery whether or not
        this process ever saw it appear -- which is the whole point.
        """
        if not self.drop_dir.is_dir():
            return []
        found = {
            path.parent
            for path in self.drop_dir.rglob(MANIFEST_FILENAME)
            if path.is_file()
        }
        return sorted(found)

    def state_dir_for(self, manifest: Mapping[str, Any]) -> Path:
        """This delivery's state directory -- the ARCHIVER's, not a parallel one."""
        return delivery_state_dir(
            store_id=str(manifest["store_id"]),
            snapshot_id=str(manifest["snapshot_id"]),
            root=self.state_root,
        )

    # -- one delivery ---------------------------------------------------

    def reconcile(self, drop: Any, *, now: datetime | None = None) -> dict[str, Any]:
        """Bring ONE delivery as far forward as it can go, and report.

        Returns a report whose ``state`` is one of ``complete`` (both stages
        settled), ``retry`` (still syncing, inside the deadline), ``failed``
        (the deadline passed, or a stage raised) or ``locked`` (another
        instance holds it this tick). Never raises for a per-delivery failure:
        a scanner that unwound on one bad delivery would stop completing the
        good ones, which is the opposite of reconciling.
        """
        moment = now or _utcnow()
        path = Path(drop).expanduser()
        manifest_path = path / MANIFEST_FILENAME
        try:
            manifest = read_manifest(manifest_path)
        except DeliveryManifestError as exc:
            return {
                "drop_dir": str(path),
                "state": "failed",
                "stage": "manifest",
                "detail": str(exc),
            }
        identity = manifest_digest(manifest)
        state_dir = self.state_dir_for(manifest)
        lock = DeliveryLock(state_dir, holder=self.holder)
        if not lock.acquire():
            return {
                "drop_dir": str(path),
                "delivery_id": manifest["delivery_id"],
                "state": "locked",
                "detail": f"another scanner holds {lock.path}",
            }
        try:
            return self._reconcile_locked(
                path,
                manifest,
                manifest_path=manifest_path,
                identity=identity,
                state_dir=state_dir,
                now=moment,
            )
        finally:
            lock.release()

    def _reconcile_locked(
        self,
        drop: Path,
        manifest: Mapping[str, Any],
        *,
        manifest_path: Path,
        identity: str,
        state_dir: Path,
        now: datetime,
    ) -> dict[str, Any]:
        progress = DeliveryProgress(state_dir)
        outcomes = DeliveryOutcomes(state_dir)
        report: dict[str, Any] = {
            "drop_dir": str(drop),
            "delivery_id": manifest["delivery_id"],
            "store_id": manifest["store_id"],
            "snapshot_id": manifest["snapshot_id"],
            "delivery_digest": identity,
            "state_dir": str(state_dir),
            "rejected_receipts": [],
        }

        # A receipt recorded under ANOTHER delivery digest is discarded, not
        # trusted. Re-running the stage is safe: the archiver resolves an
        # already-landed batch from its load_batch_id.
        for stage in (STAGE_ARCHIVE, STAGE_PUBLISH):
            try:
                outcomes.require(stage, identity=identity)
            except ReceiptIdentityError as exc:
                outcomes.drop(stage)
                report["rejected_receipts"].append({"stage": stage, "detail": str(exc)})

        try:
            arrival = verify_arrival(
                manifest,
                drop,
                progress=progress,
                manifest_path=manifest_path,
                legacy_deadline=self.legacy_deadline,
                now=now,
            )
        except DeliveryNotArrivedError as exc:
            return {
                **report,
                "state": "failed",
                "stage": "arrival",
                "detail": str(exc),
                "evidence": exc.evidence,
            }
        report["arrival"] = arrival
        if arrival["state"] != "complete":
            return {**report, "state": "retry", "stage": "arrival"}

        settled = outcomes.read(identity=identity)["stages"]
        # TWO STAGES, RETRIED INDEPENDENTLY. An archive receipt does not
        # excuse a failed publish, and a failed publish does not re-archive.
        archive_state = str((settled.get(STAGE_ARCHIVE) or {}).get("state") or "")
        if archive_state == OUTCOME_OK:
            # THE RECEIPT IS A CACHE AND THE LAKEHOUSE IS THE AUTHORITY, so a
            # cached `ok` is re-reconciled against the tables every tick
            # rather than trusted on its identity match alone. Correctness
            # here comes from reconciling disk against the lakehouse; a
            # receipt that matched its manifest while the rows were gone is
            # exactly the state that would otherwise be invisible.
            #
            # A disagreement is reported and NOT papered over by re-appending:
            # the ledger says every batch committed, so a retry would skip
            # them all and there is no rollback. "The ledger says committed
            # and the table is empty" is a decision about this delivery, the
            # same posture `AmbiguousBatchError` takes.
            status = reconcile_delivery(self.sink, ArchiveLedger(state_dir))
            if status["complete"]:
                report[STAGE_ARCHIVE] = {**settled[STAGE_ARCHIVE], "action": "cached"}
            else:
                report[STAGE_ARCHIVE] = {
                    **settled[STAGE_ARCHIVE],
                    "state": OUTCOME_FAILED,
                    "action": "receipt-disagrees-with-lakehouse",
                    "detail": (
                        "the persisted archive receipt says this delivery "
                        "reconciled, and the namespace now says it has not: "
                        f"{status['incomplete']!r} did not reconcile "
                        f"({ {table: status['tables'][table] for table in status['incomplete'] if table in status['tables']} }). "
                        "A receipt is a CACHE, never an authority. This is "
                        "NOT retried by re-appending: the load ledger says "
                        "every batch committed, so a retry would skip them "
                        "all, and there is no rollback."
                    ),
                }
        else:
            report[STAGE_ARCHIVE] = self._archive(
                manifest, drop, identity=identity, outcomes=outcomes
            )
        if str(report[STAGE_ARCHIVE].get("state")) != OUTCOME_OK:
            return {**report, "state": "failed", "stage": STAGE_ARCHIVE}

        publish_state = str((settled.get(STAGE_PUBLISH) or {}).get("state") or "")
        if publish_state in (OUTCOME_OK, OUTCOME_NOT_APPLICABLE):
            report[STAGE_PUBLISH] = {**settled[STAGE_PUBLISH], "action": "cached"}
        else:
            report[STAGE_PUBLISH] = self._publish(
                manifest, drop, identity=identity, outcomes=outcomes
            )
        if str(report[STAGE_PUBLISH].get("state")) == OUTCOME_FAILED:
            return {**report, "state": "failed", "stage": STAGE_PUBLISH}
        return {**report, "state": "complete"}

    def _archive(
        self,
        manifest: Mapping[str, Any],
        drop: Path,
        *,
        identity: str,
        outcomes: DeliveryOutcomes,
    ) -> dict[str, Any]:
        """Archive the delivered snapshot through the REAL archiver.

        Nothing about batching, ledgering or reconciliation happens here. The
        snapshot is prepared (which VERIFIES its uncompressed digest against
        the declared ``snapshot_id``) and handed to ``archive_snapshot``,
        whose own resume logic decides what still needs writing.
        """
        try:
            prepared = prepare_snapshot(
                drop / str(manifest["snapshot_file"]),
                snapshot_id=str(manifest["snapshot_id"]),
                scratch_dir=self.scratch_dir,
                overwrite=True,
            )
            report = archive_snapshot(
                self.sink,
                {
                    "store_id": manifest["store_id"],
                    "snapshot_id": manifest["snapshot_id"],
                    "store_host": manifest.get("store_host") or "",
                    "path": prepared["path"],
                },
                snapshot_seq=int(manifest["snapshot_seq"]),
                state_root=self.state_root,
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never raised past here
            return outcomes.record(
                STAGE_ARCHIVE,
                identity=identity,
                state=OUTCOME_FAILED,
                detail=f"{type(exc).__name__}: {exc}",
            )
        if not report.get("complete"):
            return outcomes.record(
                STAGE_ARCHIVE,
                identity=identity,
                state=OUTCOME_FAILED,
                detail=f"the delivery did not reconcile: {report.get('status')!r}",
            )
        # THE RECEIPT IS RE-EXPORTED FROM THE LOAD LEDGER, never recomputed:
        # two receipts for one delivery that disagreed would be worse than
        # none.
        ledger = ArchiveLedger(
            delivery_state_dir(
                store_id=str(manifest["store_id"]),
                snapshot_id=str(manifest["snapshot_id"]),
                root=self.state_root,
            )
        )
        return outcomes.record(
            STAGE_ARCHIVE,
            identity=identity,
            state=OUTCOME_OK,
            receipt=archive_receipt(self.sink, ledger),
        )

    def _publish(
        self,
        manifest: Mapping[str, Any],
        drop: Path,
        *,
        identity: str,
        outcomes: DeliveryOutcomes,
    ) -> dict[str, Any]:
        """Publish the campaign, or record NOT APPLICABLE -- never ``ok``.

        A delivery that declares no bundle identity has no clearinghouse
        assertion to make, and saying so is a DIFFERENT fact from saying the
        assertion landed. Conflating them is how a missing assertion reads as
        a finished delivery.
        """
        bundle_identity = manifest.get("bundle")
        if not bundle_identity:
            return outcomes.record(
                STAGE_PUBLISH,
                identity=identity,
                state=OUTCOME_NOT_APPLICABLE,
                detail="the delivery declares no publication-bundle identity",
            )
        try:
            bundle = self._read_bundle(drop, bundle_identity)
            publish = self.publisher or _default_publisher()
            result = publish(
                self.sink,
                bundle,
                publish_dir=str(
                    self.state_dir_for(manifest) / "publish"
                ),
                state_root=str(self.state_root),
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never raised past here
            return outcomes.record(
                STAGE_PUBLISH,
                identity=identity,
                state=OUTCOME_FAILED,
                detail=f"{type(exc).__name__}: {exc}",
            )
        return outcomes.record(
            STAGE_PUBLISH, identity=identity, state=OUTCOME_OK, receipt=dict(result)
        )

    @staticmethod
    def _read_bundle(drop: Path, bundle_identity: Mapping[str, Any]) -> dict[str, Any]:
        """Read the delivered bundle file, digest-verified and identity-checked.

        ``read_bundle`` verifies the bundle's OWN content digest; this
        additionally checks it against the digest the DELIVERY declared, so a
        bundle swapped for another well-formed one is caught.
        """
        from ...genome.ontology_mapping.council_bundle import read_bundle

        relative = str(bundle_identity.get("path") or "")
        if not relative:
            raise CouncilDeliveryError(
                "the delivery's bundle identity names no path, so the bundle "
                "cannot be read from the drop directory"
            )
        bundle = read_bundle(drop / relative)
        declared = str(bundle_identity.get("bundle_digest") or "")
        if declared and str(bundle.get("bundle_digest")) != declared:
            raise CouncilDeliveryError(
                f"the delivered bundle's digest {bundle.get('bundle_digest')!r} "
                f"is not the {declared!r} this delivery declares. A bundle is "
                "immutable once written; a different one here would publish a "
                "campaign nobody authored."
            )
        return bundle

    # -- the loop -------------------------------------------------------

    def tick(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Reconcile EVERY delivery present, once. The whole unit of work."""
        moment = now or _utcnow()
        reports = [self.reconcile(drop, now=moment) for drop in self.deliveries()]
        counts: dict[str, int] = {}
        for report in reports:
            state = str(report.get("state"))
            counts[state] = counts.get(state, 0) + 1
        return {
            "at": moment.isoformat(),
            "drop_dir": str(self.drop_dir),
            "deliveries": len(reports),
            "counts": counts,
            "reports": reports,
        }

    def run(
        self,
        *,
        max_ticks: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> list[dict[str, Any]]:
        """Tick forever, or *max_ticks* times.

        ``max_ticks`` and the injected *sleep* exist so this loop is testable
        and so an operator can run one reconciliation pass by hand. There is
        no watch mode and there will not be one -- see the class docstring.
        """
        ticks: list[dict[str, Any]] = []
        count = 0
        while max_ticks is None or count < max_ticks:
            ticks.append(self.tick())
            count += 1
            if max_ticks is not None and count >= max_ticks:
                break
            sleep(self.poll_seconds)
        return ticks


def _default_publisher() -> Callable[..., Mapping[str, Any]]:
    """The lakehouse-sourced publisher, imported lazily.

    Lazily so this module -- the one with no database in it -- stays
    importable and testable without pyarrow or the clearinghouse stack.
    """
    from .council_publish import publish_from_lakehouse

    return publish_from_lakehouse
