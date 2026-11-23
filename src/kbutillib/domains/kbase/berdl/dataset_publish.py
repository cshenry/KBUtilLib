"""The POD-SIDE PUBLISHER for KBDL's dataset mirror: pinned, verified, resumable.

WHAT THIS PUBLISHES. KBDL (a separate service) serves versioned,
full-snapshot parquet copies of selected BERDL lakehouse (Iceberg) tables to
external partners. A version is registered by ``POST
/datasets/{name}/versions`` with ``{"manifest_object_id": ...}`` AFTER the
parquet files and the JSON manifest have been uploaded as KBDL objects of
types ``"TableSnapshotFile"`` and ``"DatasetManifest"`` with visibility
``"private"``. REGISTRATION is what makes them public, so an aborted publish
discloses nothing -- which is why every upload here is private and why
nothing is registered until all of it is confirmed stored.

A 202 FROM ``POST /objects`` IS NOT STORAGE. The object exists only when its
``KBDLUploadObject`` job reaches ``"completed"``, so step 9 polls every job
and refuses to register if any of them did not complete. Two bulk loads lost
uploads silently by treating the 202 as success (see
:meth:`kbutillib.domains.external.kbdl_service_utils.KBDLServiceUtils.upload_object`);
this module does not repeat that.

PORTS, NOT A POD. The logic is written against three
:class:`typing.Protocol` ports -- :class:`LakehousePort`,
:class:`StagingPort`, :class:`KBDLPort` -- which are injected into
:class:`DatasetPublisher`. The real implementations (:class:`InPodLakehouse`,
:class:`InPodStaging`) are DELIBERATELY THIN and are not unit-tested: they
need a BERDL pod, a remote Spark cluster and MinIO. Everything that can be
got wrong -- lineage, pinning, counting, manifest shape, resume, refusal --
is in the port-free half and is tested off-pod with fakes and real parquet.
:class:`KBDLPort` is a Protocol rather than an import of
``KBDLServiceUtils`` so the real client's methods satisfy it structurally:
``get_dataset_version`` and ``publish_dataset_version`` are added to that
client by the NEXT task in this PRD, and this module must not wait for them.

THE EXPORT ROUTE IS NOT ``file://`` (Albert's method, proven 2026-11-20). The
BERDL Spark cluster is REMOTE: a ``file://`` write lands on the executors,
not in the pod, so the pod sees an empty directory and a naive exporter
reports success. Instead Spark writes zstd parquet to the publisher's OWN
MinIO staging prefix -- outside the tenant warehouse -- which is then
downloaded to a local workdir, verified, and deleted.

ONE PINNED TARGET, READ ONCE. :meth:`DatasetPublisher.publish` reads
``current_snapshot_id`` EXACTLY ONCE and every later read, export, count and
manifest field uses that id. A table that commits between the status check
and the export would otherwise produce a manifest whose row count, file set
and snapshot id come from different snapshots, and nothing downstream could
detect it.

LINEAGE IS ``parent_id``, NEVER ``committed_at``.
:meth:`DatasetPublisher.status` walks parent links from the target toward the
published snapshot. A timestamp comparison gets a ROLLBACK exactly backwards:
after a rollback the current snapshot is OLDER than the published one by
``committed_at`` while not being its descendant at all, which reads as
"unchanged" to a clock-based check and as ``"unknown"`` here -- the honest
answer, which proceeds to a full re-export rather than skipping.

WHAT A RETRY REUSES. The workdir holds ``state.json``, written atomically
(temp file plus :func:`os.replace`). Once a manifest exists for a target and
has not been registered, a retry re-uploads and re-POSTs THE SAME MANIFEST
BYTES and therefore the same object id -- including under ``force``. A
rebuilt manifest would have a new ``exported_at``, hence a new sha256, hence
a second name for identical content, and the registry would be unable to tell
the retry from a new export.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

__all__ = [
    # Errors
    "DatasetPublishError",
    "DatasetCatalogError",
    "DatasetUnknownError",
    "StagingRootError",
    "StagingPrefixError",
    "ReadmeNotApprovedError",
    "SnapshotSummaryError",
    "EmptyTableError",
    "ExportVerificationError",
    "CountMismatchError",
    "ManifestSchemaError",
    "UploadNotConfirmedError",
    "ObjectIdMismatchError",
    "RegistrationError",
    "InsufficientSpaceError",
    "LakehouseHistoryError",
    # Ports
    "LakehousePort",
    "StagingPort",
    "KBDLPort",
    # Real adapters
    "InPodLakehouse",
    "InPodStaging",
    # Catalog
    "DatasetEntry",
    "CATALOG_PATH",
    "load_catalog",
    "get_dataset",
    "read_readme",
    # Pure helpers
    "validate_staging_root",
    "require_staging_root",
    "require_prefix_under_root",
    "staging_uri",
    "canonical_manifest_bytes",
    "validate_manifest",
    # The publisher
    "DatasetPublisher",
]


# ── Names, shapes and limits (schema 1 is NORMATIVE) ──────────────────────

#: A dataset name: ``<namespace>.<table>``, both lowercase-snake.
DATASET_NAME_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
#: One lakehouse identifier component (tenant, namespace or table).
IDENTIFIER_RE = re.compile(r"^[a-z0-9_]+$")
#: An Iceberg snapshot id, as a DECIMAL STRING. Never a JSON number: Iceberg
#: ids exceed 2**53 and a JSON float round-trip silently corrupts them.
SNAPSHOT_ID_RE = re.compile(r"^[0-9]{1,20}$")
#: An ISO-8601 UTC timestamp with a ``Z`` suffix and at most microseconds.
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
#: A published parquet part name. Export names are renamed to this form so a
#: manifest never carries a Spark-generated uuid that varies per re-export.
PART_NAME_RE = re.compile(r"^part-[0-9]{5}\.parquet$")
#: A KBDL content object id: the sha256 hex digest of the object's bytes.
OBJECT_ID_RE = re.compile(r"^[0-9a-f]{64}$")

#: The Iceberg operations a snapshot may carry.
SNAPSHOT_OPERATIONS = frozenset({"append", "overwrite", "delete", "replace"})
#: The one operation that changes no rows -- a compaction/rewrite.
COMPACTION_OPERATION = "replace"

MANIFEST_SCHEMA = 1
STATE_SCHEMA = 1
#: The EXACT key set of ``state.json``. Not a minimum: the durable record is
#: compared by key set, so an extra key is a different file.
STATE_KEYS: Tuple[str, ...] = (
    "schema",
    "dataset",
    "target",
    "phase",
    "files",
    "counts",
    "manifest_sha256",
    "version",
)
STATE_PHASES = ("exported", "manifest_built", "registered")

#: A parquet part must be at least a parquet footer and at most 4 GiB.
MIN_FILE_BYTES = 12
MAX_FILE_BYTES = 4294967296
#: The serialised manifest ceiling (1 MiB) and the README ceiling.
MAX_MANIFEST_BYTES = 1048576
MAX_README_CHARS = 65536

DEFAULT_TARGET_FILE_BYTES = 268435456

#: Env settings. ``KBU_DATASET_STAGING_ROOT`` is REQUIRED for a real publish
#: and has NO default: the wrong value writes export files into a tenant's
#: warehouse, which is someone else's data surface.
STAGING_ROOT_ENV = "KBU_DATASET_STAGING_ROOT"
WORKDIR_ENV = "KBU_DATASET_WORKDIR"
DEFAULT_WORKDIR = "~/kbu-dataset-work"
#: A staging root must end with this, and must not be in the tenant warehouse.
STAGING_ROOT_SUFFIX = "/_exports_staging"
FORBIDDEN_STAGING_ROOT_PREFIX = "tenant-general-warehouse/"
STAGING_BUCKET = "cdm-lake"

OBJECT_TYPE_FILE = "TableSnapshotFile"
OBJECT_TYPE_MANIFEST = "DatasetManifest"
#: Every upload is PRIVATE. Registration is what publishes.
UPLOAD_VISIBILITY = "private"
MANIFEST_FILENAME = "MANIFEST.json"
EXPORTER_TOOL = "kbu dataset publish"
VERIFICATION_METHOD = (
    "snapshot summary total-records, Spark count(*) at the pinned snapshot, "
    "and pyarrow parquet footer row counts"
)


# ── Errors ────────────────────────────────────────────────────────────────


class DatasetPublishError(Exception):
    """Base class for every refusal and abort in this module."""


class DatasetCatalogError(DatasetPublishError):
    """``datasets.toml`` (or a README beside it) is missing or malformed."""


class DatasetUnknownError(DatasetCatalogError):
    """The requested dataset name is not in the catalog."""


class StagingRootError(DatasetPublishError):
    """``KBU_DATASET_STAGING_ROOT`` is unset, or is not a usable root."""


class StagingPrefixError(DatasetPublishError):
    """A staging operation named a prefix outside the configured root."""


class ReadmeNotApprovedError(DatasetPublishError):
    """A README approval field is empty, so nothing may be published."""


class SnapshotSummaryError(DatasetPublishError):
    """The target snapshot's summary lacks a field the publish depends on."""


class EmptyTableError(DatasetPublishError):
    """The pinned snapshot holds zero rows; v1 does not publish empty tables."""


class ExportVerificationError(DatasetPublishError):
    """The exported file set is not publishable (missing, oversized, mixed)."""


class CountMismatchError(DatasetPublishError):
    """The three independent row counts do not agree."""


class ManifestSchemaError(DatasetPublishError):
    """A manifest failed the schema-1 self-check."""


class UploadNotConfirmedError(DatasetPublishError):
    """An upload job did not reach ``"completed"``; nothing was registered."""


class ObjectIdMismatchError(DatasetPublishError):
    """KBDL stored an object under an id that is not the bytes' sha256."""


class RegistrationError(DatasetPublishError):
    """``POST /datasets/{name}/versions`` refused in a way that is not a retry."""


class InsufficientSpaceError(DatasetPublishError):
    """The workdir filesystem has too little free space to hold the export."""


class LakehouseHistoryError(DatasetPublishError):
    """The table has no usable history, so there is no current snapshot."""


# ── Ports ─────────────────────────────────────────────────────────────────


class LakehousePort(Protocol):
    """The four things this publisher needs from the Iceberg lakehouse.

    ``fqn`` is always ``"<tenant>.<namespace>.<table>"`` (the tenant is the
    Spark catalog). SNAPSHOT IDS TRAVEL AS STRINGS through every one of these
    methods -- see :data:`SNAPSHOT_ID_RE`.
    """

    def current_snapshot_id(self, fqn: str) -> str:
        """The snapshot a reader of the table sees now."""
        ...

    def snapshots(self, fqn: str) -> List[Dict[str, Any]]:
        """Every snapshot, as ``{snapshot_id, parent_id, committed_at,
        operation, summary}``; ``parent_id`` is ``None`` at the root and
        ``summary`` is a ``dict[str, str]``."""
        ...

    def spark_count(self, fqn: str, snapshot_id: str) -> int:
        """``count(*)`` of the table AS OF ``snapshot_id``."""
        ...

    def export(
        self, fqn: str, snapshot_id: str, staging_uri: str, n_files: int
    ) -> None:
        """Write the pinned snapshot as ``n_files`` zstd parquet files."""
        ...


class StagingPort(Protocol):
    """Object-store access to the publisher's own staging prefix.

    Every method REFUSES a prefix outside the configured staging root: the
    same client can reach a tenant's warehouse, and a delete is not
    recoverable.
    """

    def list(self, prefix: str) -> List[str]:
        """Object names under ``prefix``, recursively."""
        ...

    def download(self, prefix: str, local_dir: Path) -> List[Path]:
        """Fetch everything under ``prefix`` into ``local_dir``."""
        ...

    def delete(self, prefix: str) -> None:
        """Remove every object under ``prefix``."""
        ...


class KBDLPort(Protocol):
    """The subset of ``KBDLServiceUtils`` this publisher calls.

    A Protocol, NOT an import: ``upload_object`` and
    ``poll_jobs_until_terminal`` already exist on the real client, while
    ``get_dataset_version`` and ``publish_dataset_version`` are added by the
    next task of this PRD. Writing against the shape lets both halves land
    independently, and lets the tests here run with no HTTP client at all.
    """

    def upload_object(
        self,
        file: Any,
        object_type: str,
        name: str,
        visibility: str,
        filename: Optional[str] = None,
        transform: str = "none",
    ) -> Dict[str, Any]:
        """``POST /objects``. A 202 returns a ``job_id``; a 201 (server-side
        dedup) returns an ``object_id`` and queues no job."""
        ...

    def poll_jobs_until_terminal(
        self, job_ids: Sequence[str], **kwargs: Any
    ) -> Dict[str, Dict[str, Any]]:
        """Wait until every job id is terminal; returns ``{id: status}``."""
        ...

    def get_dataset_version(self, name: str, version: str) -> Dict[str, Any]:
        """A registered version (``"latest"`` for the newest). 404 if none."""
        ...

    def publish_dataset_version(
        self, name: str, manifest_object_id: str
    ) -> Dict[str, Any]:
        """``POST /datasets/{name}/versions`` with the manifest's object id."""
        ...


# ── Pure helpers: staging root, URIs, timestamps, digests ─────────────────


def validate_staging_root(root: Optional[str]) -> str:
    """Return the normalised staging root, or raise :class:`StagingRootError`.

    A PURE FUNCTION, and the only place the two rules live:

    * it must NOT start with ``"tenant-general-warehouse/"`` -- that prefix is
      a tenant's managed warehouse, i.e. someone else's data, and an export
      that wrote there (or a cleanup that deleted there) would be damage this
      module has no business being able to do; and
    * it must end with ``"/_exports_staging"`` -- a marked, disposable
      sub-prefix, so that the delete-after-download step can only ever be
      pointed at scratch.

    There is NO DEFAULT. A default staging root is a guess about which
    warehouse the publisher owns, and the cost of guessing wrong is writing
    into live tenant data; an unset setting is a clear refusal instead.
    Trailing slashes are stripped, so a root is always ``"...x"`` and prefixes
    under it are always ``"...x/..."``.
    """
    if root is None or not str(root).strip():
        raise StagingRootError(
            f"{STAGING_ROOT_ENV} is not set. It has no default: it names the "
            "object-store prefix this publisher may write to and delete from "
            f"(e.g. users-general-warehouse/<user>{STAGING_ROOT_SUFFIX}), and "
            "guessing it wrong would write into a tenant's warehouse."
        )
    normalised = str(root).strip().rstrip("/")
    if normalised.startswith(FORBIDDEN_STAGING_ROOT_PREFIX):
        raise StagingRootError(
            f"{STAGING_ROOT_ENV}={normalised!r} is inside "
            f"{FORBIDDEN_STAGING_ROOT_PREFIX!r}, a TENANT-MANAGED warehouse. "
            "The staging root must be a prefix this publisher owns, because "
            "every publish deletes everything under the prefix it exported to."
        )
    if not normalised.endswith(STAGING_ROOT_SUFFIX):
        raise StagingRootError(
            f"{STAGING_ROOT_ENV}={normalised!r} must end with "
            f"{STAGING_ROOT_SUFFIX!r} (e.g. "
            f"users-general-warehouse/<user>{STAGING_ROOT_SUFFIX}). The marked "
            "suffix is what keeps the delete-after-download step aimed at "
            "scratch and nowhere else."
        )
    return normalised


def require_staging_root(env: Optional[Mapping[str, str]] = None) -> str:
    """:func:`validate_staging_root` of ``$KBU_DATASET_STAGING_ROOT``."""
    environ = os.environ if env is None else env
    return validate_staging_root(environ.get(STAGING_ROOT_ENV))


def staging_uri(prefix: str, bucket: str = STAGING_BUCKET) -> str:
    """The Spark URI for an object-store ``prefix``: ``s3a://<bucket>/<prefix>``."""
    return f"s3a://{bucket}/{prefix}"


def require_prefix_under_root(prefix: str, root: str) -> str:
    """Refuse any prefix that is not strictly under ``root/``.

    The root ITSELF is refused as well: a delete of the root would take out
    every concurrent export's staging directory, not just this run's.
    """
    if not prefix.startswith(root + "/"):
        raise StagingPrefixError(
            f"staging prefix {prefix!r} is not under {root + '/'!r}. This port "
            "lists, downloads and DELETES, and the same client can reach every "
            "other prefix in the bucket."
        )
    return prefix


def _require_snapshot_id(value: Any, where: str) -> str:
    """A snapshot id as a decimal STRING, or a refusal naming where it came from.

    Checked rather than trusted because it is interpolated into Spark SQL
    (Iceberg's ``VERSION AS OF`` takes no bind parameter) and because it ends
    up in the manifest, where a non-decimal id would fail the schema-1
    self-check only after a full export.
    """
    text = str(value)
    if not SNAPSHOT_ID_RE.match(text):
        raise DatasetPublishError(
            f"{where} is {value!r}, which is not a decimal snapshot id "
            f"({SNAPSHOT_ID_RE.pattern}). Snapshot ids travel as strings."
        )
    return text


def _iso_utc(value: Any) -> str:
    """Normalise a timestamp to ISO-8601 UTC with ``Z`` (:data:`TIMESTAMP_RE`).

    Accepts a ``datetime`` (naive is read as UTC, which is what Spark hands
    back for an Iceberg ``committed_at``) or a string in any form
    :func:`datetime.fromisoformat` understands, including one that already
    ends in ``Z``.
    """
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if TIMESTAMP_RE.match(text):
            return text
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DatasetPublishError(
                f"cannot read {value!r} as a timestamp: {exc}"
            ) from exc
    else:
        raise DatasetPublishError(
            f"cannot read {value!r} ({type(value).__name__}) as a timestamp"
        )
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    if moment.microsecond:
        return moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _is_json_int(value: Any) -> bool:
    """A JSON integer that is NOT a boolean (``True`` is an ``int`` in Python)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _optional_int(value: Any) -> Optional[int]:
    """An Iceberg summary value as an ``int``, or ``None`` if it is not one.

    Summary values arrive as STRINGS (``summary`` is ``map<string,string>``),
    so ``"0"`` must read as ``0`` and not as absent -- the zero-rows refusal
    depends on telling those apart.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """The ONE serialisation of a manifest: sorted keys, no spaces, UTF-8.

    Nobody re-serialises a manifest to verify it -- verification is
    ``sha256(object bytes) == manifest_object_id`` -- so this exists to make
    the bytes this publisher writes reproducible from the object it built,
    not to define a canonical form consumers must reproduce.
    """
    return json.dumps(
        dict(manifest), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


# ── The schema-1 self-check ───────────────────────────────────────────────


def _require_exact_keys(obj: Any, keys: Sequence[str], where: str) -> Mapping[str, Any]:
    if not isinstance(obj, Mapping):
        raise ManifestSchemaError(
            f"{where} must be an object, got {type(obj).__name__}"
        )
    actual = set(obj)
    expected = set(keys)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ManifestSchemaError(
            f"{where} must have EXACTLY the keys {sorted(expected)}: "
            f"missing={missing} unexpected={extra}"
        )
    for key in keys:
        if obj[key] is None:
            raise ManifestSchemaError(f"{where}.{key} is null; no value may be null")
    return obj


def _require_non_empty_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestSchemaError(f"{where} must be a non-empty string, got {value!r}")
    return value


def _require_int(
    value: Any, where: str, minimum: int = 0, maximum: Optional[int] = None
) -> int:
    if not _is_json_int(value):
        raise ManifestSchemaError(
            f"{where} must be a JSON integer (and not a boolean), got {value!r}"
        )
    if value < minimum:
        raise ManifestSchemaError(f"{where} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ManifestSchemaError(f"{where} must be <= {maximum}, got {value}")
    return value


def _require_match(pattern: "re.Pattern[str]", value: Any, where: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise ManifestSchemaError(
            f"{where} must match {pattern.pattern} as a string, got {value!r}"
        )
    return value


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    """Check a manifest against MANIFEST SCHEMA 1, raising on the first fault.

    The publisher runs this on its OWN output before writing anything, which
    is the point: the manifest's sha256 becomes the version's name, so a
    malformed manifest cannot be corrected later without renaming the
    version. KBDL validates independently; this catches it here, where the
    bytes can still be thrown away.

    Required top-level keys are checked exactly; any OTHER top-level key is a
    permitted extension and is NOT inspected at all. Everywhere else the key
    set is EXACT, and no value anywhere may be ``None``.
    """
    if not isinstance(manifest, Mapping):
        raise ManifestSchemaError(
            f"a manifest must be an object, got {type(manifest).__name__}"
        )
    required = (
        "manifest_schema",
        "dataset",
        "source",
        "format",
        "row_count",
        "schema",
        "files",
        "verification",
        "readme",
        "exported_at",
        "exporter",
    )
    missing = [key for key in required if key not in manifest]
    if missing:
        raise ManifestSchemaError(f"manifest is missing required keys: {missing}")
    for key in required:
        if manifest[key] is None:
            raise ManifestSchemaError(f"manifest.{key} is null; no value may be null")

    if manifest["manifest_schema"] != MANIFEST_SCHEMA or not _is_json_int(
        manifest["manifest_schema"]
    ):
        raise ManifestSchemaError(
            f"manifest_schema must be the integer {MANIFEST_SCHEMA}, got "
            f"{manifest['manifest_schema']!r}"
        )

    source = _require_exact_keys(
        manifest["source"],
        (
            "tenant",
            "namespace",
            "table",
            "snapshot_id",
            "snapshot_committed_at",
            "snapshot_operation",
        ),
        "source",
    )
    for key in ("tenant", "namespace", "table"):
        _require_match(IDENTIFIER_RE, source[key], f"source.{key}")
    # A STRING, deliberately: Iceberg snapshot ids exceed 2**53, so a JSON
    # number would be corrupted by any consumer that parses JSON numbers as
    # doubles -- silently, and only for some ids.
    _require_match(SNAPSHOT_ID_RE, source["snapshot_id"], "source.snapshot_id")
    _require_match(
        TIMESTAMP_RE, source["snapshot_committed_at"], "source.snapshot_committed_at"
    )
    if source["snapshot_operation"] not in SNAPSHOT_OPERATIONS:
        raise ManifestSchemaError(
            f"source.snapshot_operation must be one of {sorted(SNAPSHOT_OPERATIONS)}, "
            f"got {source['snapshot_operation']!r}"
        )

    dataset = _require_match(DATASET_NAME_RE, manifest["dataset"], "dataset")
    expected_dataset = f"{source['namespace']}.{source['table']}"
    if dataset != expected_dataset:
        raise ManifestSchemaError(
            f"dataset {dataset!r} must equal source.namespace + '.' + source.table "
            f"({expected_dataset!r})"
        )

    fmt = _require_exact_keys(
        manifest["format"], ("file_format", "compression"), "format"
    )
    if fmt["file_format"] != "parquet" or fmt["compression"] != "zstd":
        raise ManifestSchemaError(
            "format must be EXACTLY {'file_format': 'parquet', 'compression': "
            f"'zstd'}}, got {dict(fmt)!r}"
        )

    row_count = _require_int(manifest["row_count"], "row_count")

    fields = manifest["schema"]
    if not isinstance(fields, list) or not fields:
        raise ManifestSchemaError("schema must be a non-empty array of field objects")
    seen_fields: set = set()
    for index, field in enumerate(fields):
        where = f"schema[{index}]"
        field = _require_exact_keys(field, ("name", "type", "nullable"), where)
        name = _require_non_empty_str(field["name"], f"{where}.name")
        _require_non_empty_str(field["type"], f"{where}.type")
        if not isinstance(field["nullable"], bool):
            raise ManifestSchemaError(f"{where}.nullable must be a boolean")
        if name in seen_fields:
            raise ManifestSchemaError(f"{where}.name {name!r} is a duplicate")
        seen_fields.add(name)

    files = manifest["files"]
    if not isinstance(files, list) or not files:
        raise ManifestSchemaError("files must be a non-empty array of file objects")
    seen_ids: set = set()
    seen_names: set = set()
    file_rows = 0
    for index, entry in enumerate(files):
        where = f"files[{index}]"
        entry = _require_exact_keys(
            entry, ("object_id", "name", "size_bytes", "row_count"), where
        )
        object_id = _require_match(
            OBJECT_ID_RE, entry["object_id"], f"{where}.object_id"
        )
        name = _require_match(PART_NAME_RE, entry["name"], f"{where}.name")
        _require_int(
            entry["size_bytes"],
            f"{where}.size_bytes",
            minimum=MIN_FILE_BYTES,
            maximum=MAX_FILE_BYTES,
        )
        file_rows += _require_int(entry["row_count"], f"{where}.row_count")
        if object_id in seen_ids:
            raise ManifestSchemaError(f"{where}.object_id {object_id!r} is a duplicate")
        if name in seen_names:
            raise ManifestSchemaError(f"{where}.name {name!r} is a duplicate")
        seen_ids.add(object_id)
        seen_names.add(name)

    verification = _require_exact_keys(
        manifest["verification"],
        ("method", "snapshot_total_records", "spark_count", "pyarrow_count"),
        "verification",
    )
    _require_non_empty_str(verification["method"], "verification.method")
    for key in ("snapshot_total_records", "spark_count", "pyarrow_count"):
        _require_int(verification[key], f"verification.{key}")

    readme = _require_non_empty_str(manifest["readme"], "readme")
    if len(readme) > MAX_README_CHARS:
        raise ManifestSchemaError(
            f"readme is {len(readme)} characters, over the {MAX_README_CHARS} limit"
        )

    _require_match(TIMESTAMP_RE, manifest["exported_at"], "exported_at")

    exporter = _require_exact_keys(
        manifest["exporter"],
        ("tool", "kbutillib_version", "pyarrow_version"),
        "exporter",
    )
    for key in ("tool", "kbutillib_version", "pyarrow_version"):
        _require_non_empty_str(exporter[key], f"exporter.{key}")

    # Consistency: the three counts and the per-file sum are ONE number, or
    # the manifest is internally contradictory and the export is not
    # publishable whatever the files look like.
    if file_rows != row_count:
        raise ManifestSchemaError(
            f"sum(files[].row_count)={file_rows} != row_count={row_count}"
        )
    for key in ("snapshot_total_records", "spark_count", "pyarrow_count"):
        if verification[key] != row_count:
            raise ManifestSchemaError(
                f"verification.{key}={verification[key]} != row_count={row_count}"
            )

    size = len(canonical_manifest_bytes(manifest))
    if size > MAX_MANIFEST_BYTES:
        raise ManifestSchemaError(
            f"the serialised manifest is {size} bytes, over the "
            f"{MAX_MANIFEST_BYTES} byte limit"
        )


# ── The catalog: datasets.toml plus the README files beside it ────────────

#: The directory the shipped catalog and its READMEs live in (package data).
CATALOG_DIR = Path(__file__).resolve().parent
CATALOG_PATH = CATALOG_DIR / "datasets.toml"


@dataclass(frozen=True)
class DatasetEntry:
    """One publishable dataset, as declared in ``datasets.toml``.

    ``readme_approved_by``/``readme_approved_on`` SHIP EMPTY and are the only
    publication gate: there is deliberately no release-authorization or
    steward field, because publication rests on the PI's authority over the
    tables he manages. An empty field refuses a real publish; a dry run
    always reports, so the gate can never hide what a publish would do.
    """

    name: str
    tenant: str
    namespace: str
    table: str
    target_file_bytes: int
    readme_file: str
    readme_approved_by: str
    readme_approved_on: str

    @property
    def fqn(self) -> str:
        """``"<tenant>.<namespace>.<table>"`` -- the tenant is the Spark catalog."""
        return f"{self.tenant}.{self.namespace}.{self.table}"

    @property
    def readme_approved(self) -> bool:
        """True only when BOTH approval fields carry something."""
        return bool(self.readme_approved_by.strip() and self.readme_approved_on.strip())


def _tomllib() -> Any:
    try:
        import tomllib  # py 3.11+
    except ModuleNotFoundError:  # pragma: no cover - py3.10 and older
        import tomli as tomllib  # type: ignore[no-redef]
    return tomllib


def load_catalog(path: Optional[Path] = None) -> Dict[str, DatasetEntry]:
    """Read and VALIDATE the dataset catalog.

    Validation is not optional dressing: ``name`` must equal
    ``"<namespace>.<table>"`` and every identifier must be lowercase-snake,
    because the name is both the KBDL dataset path and the key the lakehouse
    fqn is built from. A catalog whose name and table disagree would publish
    one table's data under another's name.
    """
    catalog_path = CATALOG_PATH if path is None else Path(path)
    try:
        raw = _tomllib().loads(catalog_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DatasetCatalogError(f"cannot read catalog {catalog_path}: {exc}") from exc
    except ValueError as exc:
        raise DatasetCatalogError(
            f"catalog {catalog_path} is not valid TOML: {exc}"
        ) from exc

    datasets = raw.get("datasets")
    if not isinstance(datasets, Mapping) or not datasets:
        raise DatasetCatalogError(
            f"catalog {catalog_path} has no non-empty [datasets] table"
        )

    entries: Dict[str, DatasetEntry] = {}
    for name, body in datasets.items():
        if not DATASET_NAME_RE.match(str(name)):
            raise DatasetCatalogError(
                f"dataset name {name!r} must match {DATASET_NAME_RE.pattern}"
            )
        if not isinstance(body, Mapping):
            raise DatasetCatalogError(f"dataset {name!r} must be a table")
        required = ("tenant", "namespace", "table", "readme_file")
        for key in required:
            value = body.get(key)
            if not isinstance(value, str) or not value:
                raise DatasetCatalogError(
                    f"dataset {name!r} needs a non-empty string {key!r}"
                )
        for key in ("tenant", "namespace", "table"):
            if not IDENTIFIER_RE.match(str(body[key])):
                raise DatasetCatalogError(
                    f"dataset {name!r} field {key}={body[key]!r} must match "
                    f"{IDENTIFIER_RE.pattern}"
                )
        expected = f"{body['namespace']}.{body['table']}"
        if str(name) != expected:
            raise DatasetCatalogError(
                f"dataset name {name!r} must equal namespace.table ({expected!r})"
            )
        target_file_bytes = body.get("target_file_bytes", DEFAULT_TARGET_FILE_BYTES)
        if not _is_json_int(target_file_bytes) or target_file_bytes <= 0:
            raise DatasetCatalogError(
                f"dataset {name!r} target_file_bytes must be a positive integer, "
                f"got {target_file_bytes!r}"
            )
        for key in ("readme_approved_by", "readme_approved_on"):
            if not isinstance(body.get(key, ""), str):
                raise DatasetCatalogError(
                    f"dataset {name!r} field {key} must be a string (empty = unapproved)"
                )
        entries[str(name)] = DatasetEntry(
            name=str(name),
            tenant=str(body["tenant"]),
            namespace=str(body["namespace"]),
            table=str(body["table"]),
            target_file_bytes=int(target_file_bytes),
            readme_file=str(body["readme_file"]),
            readme_approved_by=str(body.get("readme_approved_by", "")),
            readme_approved_on=str(body.get("readme_approved_on", "")),
        )
    return entries


def get_dataset(name: str, path: Optional[Path] = None) -> DatasetEntry:
    """One catalog entry by name, or :class:`DatasetUnknownError`."""
    catalog = load_catalog(path)
    try:
        return catalog[name]
    except KeyError:
        raise DatasetUnknownError(
            f"dataset {name!r} is not in the catalog; known datasets: {sorted(catalog)}"
        ) from None


def read_readme(entry: DatasetEntry, catalog_dir: Optional[Path] = None) -> str:
    """The README text that goes into the manifest, read beside the catalog.

    ``readme_file`` is resolved RELATIVE to the catalog directory and must
    stay inside it: the catalog is package data, and a path that escaped it
    would put an arbitrary local file into a published manifest.
    """
    base = (CATALOG_DIR if catalog_dir is None else Path(catalog_dir)).resolve()
    path = (base / entry.readme_file).resolve()
    if path != base and base not in path.parents:
        raise DatasetCatalogError(
            f"dataset {entry.name!r} readme_file {entry.readme_file!r} resolves "
            f"outside the catalog directory {base}"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DatasetCatalogError(
            f"dataset {entry.name!r} readme_file {entry.readme_file!r} is not "
            f"readable at {path}: {exc}"
        ) from exc
    if not text.strip():
        raise DatasetCatalogError(
            f"dataset {entry.name!r} readme {path} is empty; the manifest's "
            "readme field may not be empty"
        )
    return text


# ── The real adapters (thin; they need a BERDL pod) ───────────────────────


class InPodLakehouse:
    """:class:`LakehousePort` over ``InPodTransport.spark_session()``.

    NOT UNIT-TESTED, deliberately: every method is one Spark SQL statement
    and a shape conversion, and running any of it needs a BERDL pod with a
    live remote Spark cluster. The logic that could be wrong lives in
    :class:`DatasetPublisher`, which never touches Spark.

    ``current_snapshot_id`` reads ``<fqn>.history`` rather than
    ``<fqn>.snapshots``: ``snapshots`` lists EVERY snapshot including ones a
    rollback has abandoned, so "the newest row in snapshots" is not what a
    reader of the table sees. The newest ``history`` row with
    ``is_current_ancestor = true`` is.
    """

    def __init__(self, transport: Any = None, bucket: str = STAGING_BUCKET) -> None:
        self._transport = transport
        self._bucket = bucket
        self._spark: Any = None

    def _session(self) -> Any:
        if self._spark is None:
            if self._transport is None:
                from .transports import InPodTransport

                self._transport = InPodTransport()
            self._spark = self._transport.spark_session()
        return self._spark

    @staticmethod
    def _pinned(fqn: str, snapshot_id: str) -> str:
        """``<fqn> VERSION AS OF <id>``, with the id checked, never trusted.

        The id is interpolated into SQL text (Iceberg's ``VERSION AS OF`` does
        not take a bind parameter), so it is matched against
        :data:`SNAPSHOT_ID_RE` first.
        """
        checked = _require_snapshot_id(snapshot_id, f"the snapshot id for {fqn}")
        return f"{fqn} VERSION AS OF {checked}"

    def current_snapshot_id(self, fqn: str) -> str:
        rows = (
            self._session()
            .sql(
                f"SELECT snapshot_id FROM {fqn}.history "
                "WHERE is_current_ancestor = true "
                "ORDER BY made_current_at DESC LIMIT 1"
            )
            .collect()
        )
        if not rows:
            raise LakehouseHistoryError(
                f"{fqn}.history has no row with is_current_ancestor = true, so "
                "the table has no current snapshot to pin a publish to"
            )
        return str(rows[0][0])

    def snapshots(self, fqn: str) -> List[Dict[str, Any]]:
        rows = (
            self._session()
            .sql(
                "SELECT snapshot_id, parent_id, committed_at, operation, summary "
                f"FROM {fqn}.snapshots"
            )
            .collect()
        )
        out: List[Dict[str, Any]] = []
        for row in rows:
            record = row.asDict(recursive=True)
            parent = record.get("parent_id")
            summary = record.get("summary") or {}
            out.append(
                {
                    "snapshot_id": str(record["snapshot_id"]),
                    "parent_id": None if parent is None else str(parent),
                    "committed_at": _iso_utc(record["committed_at"]),
                    "operation": str(record.get("operation") or ""),
                    "summary": {str(k): str(v) for k, v in dict(summary).items()},
                }
            )
        return out

    def spark_count(self, fqn: str, snapshot_id: str) -> int:
        rows = (
            self._session()
            .sql(f"SELECT count(*) FROM {self._pinned(fqn, snapshot_id)}")
            .collect()
        )
        return int(rows[0][0])

    def export(
        self, fqn: str, snapshot_id: str, staging_uri: str, n_files: int
    ) -> None:
        frame = self._session().sql(f"SELECT * FROM {self._pinned(fqn, snapshot_id)}")
        frame.repartition(int(n_files)).write.option("compression", "zstd").parquet(
            staging_uri
        )


class InPodStaging:
    """:class:`StagingPort` over ``InPodTransport.minio_client()``.

    NOT UNIT-TESTED for the same reason as :class:`InPodLakehouse` -- but the
    rule that makes it SAFE is: :func:`validate_staging_root` and
    :func:`require_prefix_under_root` are pure functions, tested directly,
    and every method here goes through them. This class holds no policy of
    its own.
    """

    def __init__(
        self,
        root: Optional[str] = None,
        client: Any = None,
        transport: Any = None,
        bucket: str = STAGING_BUCKET,
    ) -> None:
        self.root = (
            require_staging_root() if root is None else validate_staging_root(root)
        )
        self.bucket = bucket
        self._client = client
        self._transport = transport

    def _minio(self) -> Any:
        if self._client is None:
            if self._transport is None:
                from .transports import InPodTransport

                self._transport = InPodTransport()
            self._client = self._transport.minio_client()
        return self._client

    def uri(self, prefix: str) -> str:
        """The Spark URI for a prefix under this root."""
        return staging_uri(require_prefix_under_root(prefix, self.root), self.bucket)

    def list(self, prefix: str) -> List[str]:
        require_prefix_under_root(prefix, self.root)
        return [
            str(obj.object_name)
            for obj in self._minio().list_objects(self.bucket, prefix, recursive=True)
        ]

    def download(self, prefix: str, local_dir: Path) -> List[Path]:
        require_prefix_under_root(prefix, self.root)
        target = Path(local_dir)
        target.mkdir(parents=True, exist_ok=True)
        paths: List[Path] = []
        for name in self.list(prefix):
            # Spark writes the part files FLAT under the prefix, so the
            # basename is unique; a nested name would collide and is not a
            # shape this exporter produces.
            destination = target / Path(name).name
            self._minio().fget_object(self.bucket, name, str(destination))
            paths.append(destination)
        return paths

    def delete(self, prefix: str) -> None:
        require_prefix_under_root(prefix, self.root)
        for name in self.list(prefix):
            self._minio().remove_object(self.bucket, name)


# ── Shapes KBDL's answers arrive in ──────────────────────────────────────


def _exception_names(exc: BaseException) -> set:
    return {klass.__name__ for klass in type(exc).__mro__}


def _looks_like_status(exc: BaseException, code: int, name: str) -> bool:
    """Whether a client exception means HTTP ``code``.

    Matched by TYPE NAME rather than by importing
    ``kbutillib.domains.external.kbdl_service_utils``: that module is the
    other half of this PRD (it grows ``get_dataset_version`` next), it pulls
    in ``requests``, and :class:`KBDLPort` exists precisely so this module
    does not depend on it. The numeric attributes are checked too, so a
    client that raises its own error class still reads correctly.
    """
    if name in _exception_names(exc):
        return True
    for attribute in ("status_code", "status"):
        if getattr(exc, attribute, None) == code:
            return True
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == code


def _is_not_found(exc: BaseException) -> bool:
    """A 404 -- for ``get_dataset_version``, "this dataset has no versions"."""
    return _looks_like_status(exc, 404, "KBDLNotFoundError")


def _tombstone_of(value: Any) -> Optional[Any]:
    """A tombstone carried by a response body or an exception, if any.

    A tombstone on a ``POST /datasets/{name}/versions`` means THAT MANIFEST
    BELONGS TO A RETIRED VERSION: the id is permanently spent, so the retry
    that would normally be correct here can never succeed. It must surface as
    an error -- a retired version read as "already published" would leave the
    operator believing a withdrawn snapshot is live.
    """
    if isinstance(value, Mapping):
        if value.get("tombstone"):
            return value["tombstone"]
        for nested in ("detail", "error", "body"):
            found = _tombstone_of(value.get(nested))
            if found is not None:
                return found
        return None
    if isinstance(value, str) and "tombstone" in value.lower():
        return value
    if isinstance(value, BaseException):
        for probe in (getattr(value, "detail", None), *value.args):
            found = _tombstone_of(probe)
            if found is not None:
                return found
    return None


def _job_object_id(status: Mapping[str, Any]) -> Optional[str]:
    """The object id a completed ``KBDLUploadObject`` job registered."""
    if not isinstance(status, Mapping):
        return None
    result = status.get("result")
    if isinstance(result, Mapping):
        if isinstance(result.get("object_id"), str):
            return result["object_id"]
        ids = result.get("object_ids")
        if isinstance(ids, (list, tuple)) and ids and isinstance(ids[0], str):
            return ids[0]
    if isinstance(status.get("object_id"), str):
        return status["object_id"]
    return None


def _published_snapshot_of(record: Mapping[str, Any]) -> str:
    """The ``source_snapshot_id`` of a registered version record."""
    value = record.get("source_snapshot_id")
    if value is None:
        source = record.get("source")
        if isinstance(source, Mapping):
            value = source.get("snapshot_id")
    if value is None:
        raise DatasetPublishError(
            "the registered version record carries no source_snapshot_id, so "
            "lineage cannot be computed against it: "
            f"{sorted(record) if isinstance(record, Mapping) else record!r}"
        )
    return str(value)


# ── The publisher ─────────────────────────────────────────────────────────


class DatasetPublisher:
    """Status and publish for one KBDL dataset mirror, over injected ports.

    Everything that can be got wrong lives here and nothing here touches a
    pod: :class:`LakehousePort`, :class:`StagingPort` and :class:`KBDLPort`
    are the whole outside world.
    """

    def __init__(
        self,
        lakehouse: LakehousePort,
        staging: StagingPort,
        kbdl: KBDLPort,
        *,
        catalog_path: Optional[Path] = None,
        staging_root: Optional[str] = None,
        workdir_root: Optional[Path] = None,
        now: Optional[Any] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._lakehouse = lakehouse
        self._staging = staging
        self._kbdl = kbdl
        self._catalog_path = None if catalog_path is None else Path(catalog_path)
        self._catalog_dir = (
            CATALOG_DIR if catalog_path is None else Path(catalog_path).resolve().parent
        )
        self._staging_root_override = staging_root
        self._workdir_root_override = (
            None if workdir_root is None else Path(workdir_root)
        )
        self._now = now if now is not None else (lambda: datetime.now(timezone.utc))
        self._env: Mapping[str, str] = os.environ if env is None else env

    # -- configuration -----------------------------------------------------

    def _entry(self, name: str) -> DatasetEntry:
        return get_dataset(name, self._catalog_path)

    def _staging_root(self) -> str:
        if self._staging_root_override is not None:
            return validate_staging_root(self._staging_root_override)
        return require_staging_root(self._env)

    def _workdir_root(self) -> Path:
        if self._workdir_root_override is not None:
            return self._workdir_root_override.expanduser()
        return Path(self._env.get(WORKDIR_ENV) or DEFAULT_WORKDIR).expanduser()

    def _workdir(self, dataset: str, target: str) -> Path:
        return self._workdir_root() / dataset / target

    # -- lineage -----------------------------------------------------------

    def _snapshot_index(self, entry: DatasetEntry) -> Dict[str, Dict[str, Any]]:
        """``{snapshot_id: normalised snapshot}``, ids as strings throughout."""
        index: Dict[str, Dict[str, Any]] = {}
        for row in self._lakehouse.snapshots(entry.fqn):
            parent = row.get("parent_id")
            summary = row.get("summary") or {}
            snapshot_id = str(row["snapshot_id"])
            index[snapshot_id] = {
                "snapshot_id": snapshot_id,
                "parent_id": None if parent is None else str(parent),
                "committed_at": _iso_utc(row["committed_at"]),
                "operation": str(row.get("operation") or ""),
                "summary": {str(k): str(v) for k, v in dict(summary).items()},
            }
        return index

    def _published_snapshot_id(self, name: str) -> Optional[str]:
        """The latest REGISTERED version's source snapshot, or ``None``.

        A 404 is the "no versions yet" answer, not a failure: a dataset's
        first publish has nothing to compare against.
        """
        try:
            record = self._kbdl.get_dataset_version(name, "latest")
        except Exception as exc:  # noqa: BLE001 - re-raised unless it is a 404
            if _is_not_found(exc):
                return None
            raise
        if record is None:
            return None
        return _published_snapshot_of(record)

    def _lineage(
        self,
        entry: DatasetEntry,
        target: str,
        snapshots: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """The five-outcome lineage verdict: ``status``, ``target``, ``operations``.

        THE WALK IS ``parent_id`` ONLY. It starts at ``target`` and follows
        parent links, collecting each snapshot it passes, until it either
        finds the published snapshot as a parent (``reached``) or runs out of
        chain. ``committed_at`` is carried for the report and is NEVER
        consulted to decide anything -- see this module's docstring on
        rollback.
        """
        index = self._snapshot_index(entry) if snapshots is None else snapshots
        published = self._published_snapshot_id(entry.name)
        if published is None:
            return {"status": "no_versions", "target": target, "operations": []}
        if published == target:
            return {"status": "unchanged", "target": target, "operations": []}

        operations: List[Dict[str, Any]] = []
        seen: set = {target}
        current: Optional[str] = target
        reached = False
        while current is not None:
            snapshot = index.get(current)
            if snapshot is None:
                # The target itself, or a parent, is not in `snapshots`: the
                # chain is broken (expiry, a replaced table) and ancestry
                # cannot be established.
                break
            operations.append(
                {
                    "snapshot_id": snapshot["snapshot_id"],
                    "operation": snapshot["operation"],
                    "committed_at": snapshot["committed_at"],
                }
            )
            parent = snapshot["parent_id"]
            if parent is None:
                break
            if parent == published:
                reached = True
                break
            if parent in seen:  # pragma: no cover - defensive against a cycle
                break
            seen.add(parent)
            current = parent

        if not reached:
            return {"status": "unknown", "target": target, "operations": operations}
        if all(step["operation"] == COMPACTION_OPERATION for step in operations):
            return {
                "status": "compaction_only",
                "target": target,
                "operations": operations,
            }
        return {"status": "changed", "target": target, "operations": operations}

    def _staging_warnings(self, entry: DatasetEntry) -> List[str]:
        """Leftover staging prefixes: LISTED, never deleted.

        A leftover prefix is evidence of an interrupted export, and the one
        thing not to do with evidence is delete it automatically -- a
        concurrent run's live export looks exactly the same from here.
        """
        try:
            root = self._staging_root()
        except StagingRootError as exc:
            return [
                f"{STAGING_ROOT_ENV} is unset or invalid, so leftover staging "
                f"prefixes were NOT checked: {exc}"
            ]
        prefix = f"{root}/{entry.name}/"
        try:
            names = self._staging.list(prefix)
        except Exception as exc:  # noqa: BLE001 - a warning, not a failure
            return [f"could not list staging prefix {prefix}: {exc}"]
        if not names:
            return []
        shown = ", ".join(sorted(names)[:5])
        return [
            f"{len(names)} leftover staging object(s) under {prefix} from an "
            f"earlier run; NOT deleted (listed only): {shown}"
        ]

    def status(self, name: str, target: Optional[str] = None) -> Dict[str, Any]:
        """Where ``name`` stands relative to its latest registered version.

        ``target`` defaults to :meth:`LakehousePort.current_snapshot_id`,
        READ ONCE here and returned in the result, so a caller can pin a
        later publish to the same snapshot this verdict was computed for.

        One of five statuses: ``no_versions``, ``unchanged``,
        ``compaction_only`` (everything since the published snapshot is a
        ``replace``, i.e. compaction that changed no rows),
        ``changed``, or ``unknown`` (the parent chain ran out before the
        published snapshot was found -- a rollback, an expired snapshot, or a
        rebuilt table).
        """
        entry = self._entry(name)
        pinned = (
            str(self._lakehouse.current_snapshot_id(entry.fqn))
            if target is None
            else str(target)
        )
        verdict = self._lineage(entry, pinned)
        verdict["warnings"] = self._staging_warnings(entry)
        return verdict

    # -- the durable record ------------------------------------------------

    def _state_path(self, workdir: Path) -> Path:
        return workdir / "state.json"

    def _read_state(self, workdir: Path) -> Optional[Dict[str, Any]]:
        """``state.json`` parsed, or ``None`` if absent or unreadable. No writes."""
        try:
            raw = self._state_path(workdir).read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _peek_state(self, workdir: Path, target: str) -> Optional[Dict[str, Any]]:
        """What a dry run reports about the workdir. READ ONLY -- never deletes.

        A dry run must not change anything, and deleting a stale workdir is a
        change: the run it belongs to may be the one the operator is about to
        resume.
        """
        raw = self._read_state(workdir)
        if raw is None:
            return None
        return {
            "schema": raw.get("schema"),
            "phase": raw.get("phase"),
            "target": raw.get("target"),
            "matches_target": str(raw.get("target")) == str(target),
            "manifest_sha256": raw.get("manifest_sha256"),
        }

    def _write_state(self, workdir: Path, state: Mapping[str, Any]) -> Dict[str, Any]:
        """Write ``state.json`` ATOMICALLY, with EXACTLY :data:`STATE_KEYS`.

        Temp file in the SAME directory then :func:`os.replace`, which is
        atomic within a filesystem: a crash leaves either the old record or
        the new one, never a half-written one. A truncated ``state.json`` is
        the worst case for this module -- it would read as "resumable" and
        reuse files it cannot vouch for.
        """
        missing = [key for key in STATE_KEYS if key not in state]
        extra = [key for key in state if key not in STATE_KEYS]
        if missing or extra:  # pragma: no cover - a programming error here
            raise DatasetPublishError(
                f"state.json must have exactly {list(STATE_KEYS)}: "
                f"missing={missing} unexpected={extra}"
            )
        payload = {key: state[key] for key in STATE_KEYS}
        temp = workdir / f".state.json.{uuid.uuid4().hex}.tmp"
        temp.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temp, self._state_path(workdir))
        return payload

    def _usable_state(
        self, workdir: Path, dataset: str, target: str, force: bool
    ) -> Optional[Dict[str, Any]]:
        """The resumable record for this target, or ``None`` with a CLEAN workdir.

        Returning ``None`` always leaves an EMPTY workdir: an absent,
        unparseable, wrong-schema, wrong-target or unverifiable record means
        the files on disk cannot be vouched for, and a half-trusted export is
        worse than a re-export.

        A ``registered`` record is NOT a resume point. Without ``force`` the
        caller reports it and stops; with ``force`` it is discarded and the
        export starts over -- in contrast to ``manifest_built``, which is
        reused EVEN UNDER FORCE, because an unregistered manifest's id is
        still the name the next POST must use.
        """
        state = self._read_state(workdir)
        reason: Optional[str] = None
        if state is None:
            reason = "no readable state.json"
        elif state.get("schema") != STATE_SCHEMA:
            reason = f"state.json schema is {state.get('schema')!r}, not {STATE_SCHEMA}"
        elif sorted(state) != sorted(STATE_KEYS):
            reason = f"state.json keys are {sorted(state)}, not {sorted(STATE_KEYS)}"
        elif str(state.get("dataset")) != dataset or str(state.get("target")) != target:
            reason = (
                f"state.json records {state.get('dataset')!r}@{state.get('target')!r}, "
                f"not {dataset!r}@{target!r}"
            )
        elif state.get("phase") not in STATE_PHASES:
            reason = f"state.json phase {state.get('phase')!r} is not one of {list(STATE_PHASES)}"
        elif state["phase"] == "registered" and force:
            reason = "state.json is already registered and --force was given"

        if reason is None:
            assert state is not None
            if state["phase"] in ("exported", "manifest_built"):
                problem = self._verify_recorded_files(workdir, state)
                if problem is None and state["phase"] == "manifest_built":
                    problem = self._verify_recorded_manifest(workdir, state)
                reason = problem

        if reason is None:
            return dict(state) if state is not None else None

        shutil.rmtree(workdir, ignore_errors=True)
        workdir.mkdir(parents=True, exist_ok=True)
        return None

    def _verify_recorded_files(
        self, workdir: Path, state: Mapping[str, Any]
    ) -> Optional[str]:
        """Re-check every recorded file's sha256 and size, or say what is wrong."""
        files = state.get("files")
        if not isinstance(files, list) or not files:
            return "state.json records no files"
        for record in files:
            if not isinstance(record, Mapping):
                return "a state.json files entry is not an object"
            name = str(record.get("name"))
            path = workdir / name
            if not PART_NAME_RE.match(name):
                return f"state.json file name {name!r} is not a part-NNNNN.parquet name"
            if not path.is_file():
                return f"recorded file {name} is missing from the workdir"
            size = path.stat().st_size
            if size != record.get("size_bytes"):
                return (
                    f"recorded file {name} is {size} bytes, not "
                    f"{record.get('size_bytes')}"
                )
            if _sha256_file(path) != record.get("sha256"):
                return f"recorded file {name} does not match its recorded sha256"
        return None

    def _verify_recorded_manifest(
        self, workdir: Path, state: Mapping[str, Any]
    ) -> Optional[str]:
        """Re-check ``manifest.json`` against the recorded ``manifest_sha256``."""
        recorded = state.get("manifest_sha256")
        if not isinstance(recorded, str) or not OBJECT_ID_RE.match(recorded):
            return f"state.json manifest_sha256 {recorded!r} is not a sha256"
        path = workdir / "manifest.json"
        if not path.is_file():
            return "manifest.json is missing from the workdir"
        if _sha256_file(path) != recorded:
            return "manifest.json does not match the recorded manifest_sha256"
        return None

    # -- export, download, verification ------------------------------------

    def _export_and_download(
        self,
        entry: DatasetEntry,
        target: str,
        prefix: str,
        n_files: int,
        workdir: Path,
        report: Dict[str, Any],
    ) -> List[Path]:
        """Export to staging, download into the workdir, ALWAYS delete staging.

        The delete is in a ``finally``, so an export that raised, a download
        that raised and a clean run all leave the staging prefix gone. A
        failure of the DELETE itself is recorded as a warning rather than
        raised: it must not mask the original error, and
        :meth:`status` lists leftovers for exactly this case.
        """
        try:
            self._lakehouse.export(entry.fqn, target, staging_uri(prefix), n_files)
            report["steps"].append(
                {"step": "export", "prefix": prefix, "n_files": n_files}
            )
            downloaded = [
                Path(path) for path in self._staging.download(prefix, workdir)
            ]
            report["steps"].append(
                {"step": "download", "files": [path.name for path in downloaded]}
            )
            return downloaded
        finally:
            try:
                self._staging.delete(prefix)
                report["steps"].append({"step": "staging_deleted", "prefix": prefix})
            except Exception as exc:  # noqa: BLE001 - must not mask the real error
                report["warnings"].append(
                    f"could not delete staging prefix {prefix}: {exc}. It holds a "
                    "copy of the exported data and should be removed by hand."
                )

    def _rename_parts(self, downloaded: Sequence[Path], workdir: Path) -> List[Path]:
        """Rename the downloaded parquet files to ``part-NNNNN.parquet``.

        Spark's own names carry a per-write uuid, so a re-export of the SAME
        snapshot would produce a different manifest for identical data. The
        rename is two-phase (via temp names) because a Spark name can collide
        with a target name, and a one-phase rename would then overwrite a
        file it had not copied yet.

        ``_SUCCESS`` and anything else that is not ``*.parquet`` is ignored.
        """
        parquet = sorted(
            (path for path in downloaded if path.suffix == ".parquet"),
            key=lambda path: path.name,
        )
        if not parquet:
            raise ExportVerificationError(
                f"the export downloaded {len(downloaded)} file(s) but none of them "
                "is a .parquet file. The BERDL Spark cluster is REMOTE, so an "
                "export that wrote to the executors rather than to the staging "
                "prefix looks exactly like this."
            )
        staged: List[Path] = []
        for index, path in enumerate(parquet):
            temp = workdir / f".kbu-part-{index:05d}.parquet.tmp"
            os.replace(path, temp)
            staged.append(temp)
        final: List[Path] = []
        for index, temp in enumerate(staged):
            destination = workdir / f"part-{index:05d}.parquet"
            os.replace(temp, destination)
            size = destination.stat().st_size
            if size > MAX_FILE_BYTES:
                raise ExportVerificationError(
                    f"{destination.name} is {size} bytes, over the "
                    f"{MAX_FILE_BYTES} byte (4 GiB) per-file limit; raise "
                    "target_file_bytes down so the export is split further"
                )
            final.append(destination)
        return final

    def _arrow_schema(self, paths: Sequence[Path]) -> Any:
        """The ONE Arrow schema of a file set, or an abort if they disagree."""
        import pyarrow.parquet as pq

        schema = None
        for path in paths:
            found = pq.ParquetFile(path).schema_arrow
            if schema is None:
                schema = found
            elif not schema.equals(found):
                raise ExportVerificationError(
                    f"{path.name} has a different Arrow schema from the first "
                    "exported file; a dataset version is one schema, and a "
                    "manifest that declared one of them would be wrong about "
                    "the others"
                )
        if schema is None:  # pragma: no cover - callers pass a non-empty list
            raise ExportVerificationError("no parquet files to read a schema from")
        return schema

    def _verify_three_ways(
        self,
        entry: DatasetEntry,
        target: str,
        parts: Sequence[Path],
        total_records: int,
    ) -> Tuple[List[Dict[str, Any]], Any, Dict[str, int]]:
        """Per-file facts plus the THREE-WAY count check. Runs before any upload.

        The three counts are independent measurements of the same thing: what
        Iceberg's own snapshot summary claims, what Spark counts at the
        pinned snapshot, and what the parquet footers the partner will
        actually read say. Any disagreement means the export is not a
        faithful copy, and the only safe move is to abort BEFORE anything is
        uploaded -- a wrong count is not detectable downstream from the files
        alone.
        """
        import pyarrow.parquet as pq

        files: List[Dict[str, Any]] = []
        schema = None
        pyarrow_count = 0
        for path in parts:
            size = path.stat().st_size
            if size < MIN_FILE_BYTES:
                raise ExportVerificationError(
                    f"{path.name} is {size} bytes, too small to be a parquet file"
                )
            parquet_file = pq.ParquetFile(path)
            rows = int(parquet_file.metadata.num_rows)
            found = parquet_file.schema_arrow
            if schema is None:
                schema = found
            elif not schema.equals(found):
                raise ExportVerificationError(
                    f"{path.name} has a different Arrow schema from "
                    f"{parts[0].name}; all files in a version share one schema"
                )
            files.append(
                {
                    "name": path.name,
                    "sha256": _sha256_file(path),
                    "size_bytes": size,
                    "row_count": rows,
                }
            )
            pyarrow_count += rows

        counts = {
            "snapshot_total_records": int(total_records),
            "spark_count": int(self._lakehouse.spark_count(entry.fqn, target)),
            "pyarrow_count": pyarrow_count,
        }
        if len(set(counts.values())) != 1:
            raise CountMismatchError(
                "the three row counts disagree for "
                f"{entry.fqn} at snapshot {target}: {counts}. Nothing has been "
                "uploaded; the export is not a faithful copy of the snapshot."
            )
        return files, schema, counts

    # -- the manifest ------------------------------------------------------

    def _build_manifest(
        self,
        entry: DatasetEntry,
        target: str,
        snapshot: Mapping[str, Any],
        files: Sequence[Mapping[str, Any]],
        counts: Mapping[str, int],
        schema: Any,
        readme: str,
    ) -> Dict[str, Any]:
        """Assemble a schema-1 manifest. Carries NO version, time or publisher.

        Those three belong to the KBDL registry, not here: leaving them out
        is what makes the manifest's sha256 name EXACTLY what was exported,
        so the same export always has the same id and a retry is recognisable
        as a retry.
        """
        import pyarrow as pa

        return {
            "manifest_schema": MANIFEST_SCHEMA,
            "dataset": entry.name,
            "source": {
                "tenant": entry.tenant,
                "namespace": entry.namespace,
                "table": entry.table,
                "snapshot_id": str(target),
                "snapshot_committed_at": _iso_utc(snapshot["committed_at"]),
                "snapshot_operation": str(snapshot["operation"]),
            },
            "format": {"file_format": "parquet", "compression": "zstd"},
            "row_count": int(counts["snapshot_total_records"]),
            "schema": [
                {
                    "name": field.name,
                    "type": str(field.type),
                    "nullable": bool(field.nullable),
                }
                for field in schema
            ],
            "files": [
                {
                    "object_id": record["sha256"],
                    "name": record["name"],
                    "size_bytes": int(record["size_bytes"]),
                    "row_count": int(record["row_count"]),
                }
                for record in files
            ],
            "verification": {
                "method": VERIFICATION_METHOD,
                "snapshot_total_records": int(counts["snapshot_total_records"]),
                "spark_count": int(counts["spark_count"]),
                "pyarrow_count": int(counts["pyarrow_count"]),
            },
            "readme": readme,
            "exported_at": _iso_utc(self._now()),
            "exporter": {
                "tool": EXPORTER_TOOL,
                "kbutillib_version": _kbutillib_version(),
                "pyarrow_version": str(pa.__version__),
            },
        }

    # -- upload and registration -------------------------------------------

    def _upload(
        self,
        path_or_bytes: Any,
        object_type: str,
        display_name: str,
        filename: str,
        expected: str,
    ) -> Dict[str, Any]:
        """One ``POST /objects``, PRIVATE, with the expected object id recorded.

        A 201 (server-side dedup) means the object already exists and no job
        was queued; a 202 carries a ``job_id`` that must be polled. Either
        way the id must come out equal to the sha256 this publisher computed,
        which is what makes the manifest's ``files[].object_id`` a
        verification and not a claim.
        """
        response = self._kbdl.upload_object(
            file=path_or_bytes,
            object_type=object_type,
            name=display_name,
            visibility=UPLOAD_VISIBILITY,
            filename=filename,
        )
        response = response if isinstance(response, Mapping) else {}
        announced = response.get("expected_object_id")
        if isinstance(announced, str) and announced != expected:
            raise ObjectIdMismatchError(
                f"KBDL hashed {filename} to {announced}, but this publisher "
                f"recorded {expected}. The bytes on the wire are not the bytes "
                "the manifest describes; nothing has been registered."
            )
        return {
            "name": filename,
            "object_type": object_type,
            "visibility": UPLOAD_VISIBILITY,
            "job_id": response.get("job_id"),
            "object_id": response.get("object_id"),
            "expected_object_id": expected,
        }

    def _confirm_uploads(self, uploads: List[Dict[str, Any]], workdir: Path) -> None:
        """Poll every upload job and check every resulting id. Keeps the workdir.

        A 202 is not storage. If any job did not reach ``"completed"``,
        NOTHING is registered and the workdir is KEPT, because the next run
        must be able to re-upload the identical manifest rather than build a
        second one.
        """
        job_ids = [record["job_id"] for record in uploads if record["job_id"]]
        results = self._kbdl.poll_jobs_until_terminal(job_ids) if job_ids else {}
        results = results if isinstance(results, Mapping) else {}
        bad = {
            job_id: (results.get(job_id) or {}).get("state")
            for job_id in job_ids
            if (results.get(job_id) or {}).get("state") != "completed"
        }
        if bad:
            raise UploadNotConfirmedError(
                f"{len(bad)} of {len(job_ids)} upload job(s) did not reach "
                f"'completed': {bad}. A 202 is acceptance, not storage, so "
                "NOTHING has been registered. The workdir is kept at "
                f"{workdir} so the retry re-uploads the same manifest."
            )
        for record in uploads:
            observed = record["object_id"]
            if observed is None and record["job_id"]:
                observed = _job_object_id(results.get(record["job_id"]) or {})
            if observed is None:
                raise ObjectIdMismatchError(
                    f"KBDL confirmed the upload of {record['name']} but reported "
                    "no object id, so it cannot be checked against the sha256 "
                    f"{record['expected_object_id']}; nothing has been registered."
                )
            if observed != record["expected_object_id"]:
                raise ObjectIdMismatchError(
                    f"{record['name']} was stored as {observed}, not as its "
                    f"sha256 {record['expected_object_id']}; nothing has been "
                    "registered."
                )
            record["object_id"] = observed

    def _register(self, entry: DatasetEntry, manifest_sha256: str) -> Any:
        """``POST /datasets/{name}/versions``, once, with the manifest's id.

        A transport error or timeout is DELIBERATELY not caught: the phase
        stays ``manifest_built`` and the next run re-posts the SAME id, which
        is the only safe response to "the POST may or may not have landed".
        """
        try:
            response = self._kbdl.publish_dataset_version(entry.name, manifest_sha256)
        except Exception as exc:  # noqa: BLE001 - re-raised unless it is a tombstone
            tombstone = (
                _tombstone_of(exc)
                if _looks_like_status(exc, 409, "KBDLConflictError")
                else None
            )
            if tombstone is not None:
                raise RegistrationError(
                    f"KBDL refused manifest {manifest_sha256} for "
                    f"{entry.name}: it belongs to a RETIRED version "
                    f"({tombstone}). This is an error, not a successful retry "
                    "-- a retired version must never read as published. The "
                    "export must be rebuilt under a new manifest."
                ) from exc
            raise
        if _tombstone_of(response) is not None:
            raise RegistrationError(
                f"KBDL answered the registration of manifest {manifest_sha256} "
                f"for {entry.name} with a TOMBSTONE ({_tombstone_of(response)}); "
                "that manifest belongs to a retired version and this is not a "
                "successful publish."
            )
        return response

    # -- the verb ----------------------------------------------------------

    def _require_readme_approval(self, entry: DatasetEntry) -> None:
        """The ONE publication gate. Names the empty field, because that is the fix."""
        empty = [
            field
            for field, value in (
                ("readme_approved_by", entry.readme_approved_by),
                ("readme_approved_on", entry.readme_approved_on),
            )
            if not value.strip()
        ]
        if empty:
            raise ReadmeNotApprovedError(
                f"dataset {entry.name!r} is not approved for publication: "
                f"{' and '.join(empty)} is empty in the catalog entry "
                f"({CATALOG_PATH.name}). Publication rests on the PI's approval "
                f"of {entry.readme_file}; fill both fields in the catalog, in a "
                "commit made by the person approving it. A --dry-run needs no "
                "approval and reports the whole plan."
            )

    def publish(
        self,
        name: str,
        dry_run: bool = False,
        force: bool = False,
        keep_workdir: bool = False,
    ) -> Dict[str, Any]:
        """Export, verify, upload and register ONE pinned snapshot of ``name``.

        Returns a structured report of every step. Refusals raise (each with
        a message naming what to change); a ``would_skip`` outcome returns an
        exit-0 style report saying why nothing was done.

        ``dry_run`` ALWAYS returns a full report, whatever the status, and
        calls no export, staging, download or upload method of any port --
        including on an unapproved dataset, because a gate that hid the plan
        would make the approval decision harder rather than safer. Its
        ``staging_prefix`` is the TEMPLATE a real run would fill in (the real
        one ends in a fresh uuid4).

        ``force`` overrides only the ``unchanged``/``compaction_only`` skip.
        It does NOT cause a new manifest to be built while an unregistered
        one exists for this target: that manifest's sha256 is the id the next
        POST must carry.
        """
        entry = self._entry(name)

        # 1. THE PIN. One read, and every later step uses this id.
        target = _require_snapshot_id(
            self._lakehouse.current_snapshot_id(entry.fqn),
            f"current_snapshot_id({entry.fqn})",
        )

        # 2. Lineage against the published version, from the same snapshot
        #    listing the manifest's own source fields come from.
        snapshots = self._snapshot_index(entry)
        lineage = self._lineage(entry, target, snapshots=snapshots)
        would_skip = lineage["status"] in ("unchanged", "compaction_only") and not force

        snapshot = snapshots.get(target)
        summary = dict(snapshot["summary"]) if snapshot is not None else {}
        total_records = _optional_int(summary.get("total-records"))
        total_bytes = _optional_int(summary.get("total-files-size"))
        workdir = self._workdir(entry.name, target)
        try:
            root: Optional[str] = self._staging_root()
            root_error: Optional[str] = None
        except StagingRootError as exc:
            root, root_error = None, str(exc)

        report: Dict[str, Any] = {
            "dataset": entry.name,
            "dry_run": bool(dry_run),
            "force": bool(force),
            "target": target,
            "status": lineage["status"],
            "operations": lineage["operations"],
            "would_skip": would_skip,
            "snapshot_total_records": total_records,
            "snapshot_total_files_size": total_bytes,
            "n_files": (
                None
                if total_bytes is None
                else max(1, math.ceil(total_bytes / entry.target_file_bytes))
            ),
            "readme_approved": entry.readme_approved,
            "readme_approved_by": entry.readme_approved_by,
            "readme_approved_on": entry.readme_approved_on,
            "workdir": str(workdir),
            "workdir_state": self._peek_state(workdir, target),
            "staging_root": root,
            "staging_root_error": root_error,
            "staging_prefix": (
                None if root is None else f"{root}/{entry.name}/{target}-<uuid4>/"
            ),
            "warnings": [],
            "steps": [],
        }

        # 3. A DRY RUN ALWAYS REPORTS, and touches nothing.
        if dry_run:
            report["skipped"] = True
            report["ok"] = True
            return report

        if would_skip:
            report["skipped"] = True
            report["ok"] = True
            report["reason"] = (
                f"{entry.name} is {lineage['status']} at snapshot {target}: "
                "the published version already covers this data. Pass force=True "
                "to publish it anyway."
            )
            return report
        report["skipped"] = False

        # 4. The refusals, every one of them before anything is exported.
        self._require_readme_approval(entry)
        if root is None:
            raise StagingRootError(str(root_error))
        if total_records is None or total_bytes is None:
            missing = [
                field
                for field, value in (
                    ("total-records", total_records),
                    ("total-files-size", total_bytes),
                )
                if value is None
            ]
            raise SnapshotSummaryError(
                f"snapshot {target} of {entry.fqn} has no {', '.join(missing)} in "
                f"its summary (summary keys: {sorted(summary)}). This publisher "
                "FAILS CLOSED on that: total-records is one of the three counts "
                "the export is verified against, and total-files-size is what "
                "the file split and the free-space guard are computed from. "
                "Neither can be guessed. Re-check the snapshot with "
                f"'SELECT summary FROM {entry.fqn}.snapshots'."
            )
        if total_records == 0:
            raise EmptyTableError(
                f"snapshot {target} of {entry.fqn} holds 0 rows. v1 does not "
                "publish an empty table: an empty version is indistinguishable "
                "to a partner from a successful export of a broken pipeline."
            )
        readme = read_readme(entry, self._catalog_dir)

        # 5. The workdir and the durable record.
        workdir.mkdir(parents=True, exist_ok=True)
        state = self._usable_state(workdir, entry.name, target, force=force)
        report["resumed_phase"] = None if state is None else state["phase"]

        if state is not None and state["phase"] == "registered":
            report["skipped"] = True
            report["ok"] = True
            report["registered"] = True
            report["version"] = state["version"]
            report["manifest_object_id"] = state["manifest_sha256"]
            report["reason"] = (
                f"the workdir {workdir} records this target as already "
                f"registered as version {state['version']!r}. Pass force=True to "
                "discard that record and export again."
            )
            return report

        schema: Any = None
        if state is None:
            free = shutil.disk_usage(workdir).free
            if free < 2 * total_bytes:
                raise InsufficientSpaceError(
                    f"{workdir} has {free} bytes free, less than 2 x "
                    f"total-files-size ({2 * total_bytes}). The export is "
                    "downloaded in full before it is verified, and the manifest "
                    "is built from the downloaded files; this is a guard, not a "
                    "guarantee -- zstd parquet may be larger or smaller than the "
                    "snapshot's stored size."
                )

            # 6. Export to a fresh staging prefix, download, delete staging.
            prefix = f"{root}/{entry.name}/{target}-{uuid.uuid4()}/"
            n_files = max(1, math.ceil(total_bytes / entry.target_file_bytes))
            report["staging_prefix"] = prefix
            report["n_files"] = n_files
            downloaded = self._export_and_download(
                entry, target, prefix, n_files, workdir, report
            )
            parts = self._rename_parts(downloaded, workdir)
            report["steps"].append(
                {"step": "renamed", "files": [p.name for p in parts]}
            )

            # 7. Three-way verification, before any upload.
            files, schema, counts = self._verify_three_ways(
                entry, target, parts, total_records
            )
            state = self._write_state(
                workdir,
                {
                    "schema": STATE_SCHEMA,
                    "dataset": entry.name,
                    "target": target,
                    "phase": "exported",
                    "files": files,
                    "counts": counts,
                    "manifest_sha256": None,
                    "version": None,
                },
            )
            report["steps"].append({"step": "verified", "counts": counts})

        report["files"] = state["files"]
        report["counts"] = state["counts"]
        manifest_path = workdir / "manifest.json"

        # 8. Build the manifest -- ONLY if one does not already exist for this
        #    target. A rebuilt manifest would carry a new exported_at, hence a
        #    new sha256, hence a second name for identical content.
        if state["phase"] == "exported":
            if schema is None:
                schema = self._arrow_schema(
                    [workdir / record["name"] for record in state["files"]]
                )
            manifest = self._build_manifest(
                entry,
                target,
                snapshot or {},
                state["files"],
                state["counts"],
                schema,
                readme,
            )
            validate_manifest(manifest)
            data = canonical_manifest_bytes(manifest)
            temp = workdir / f".manifest.json.{uuid.uuid4().hex}.tmp"
            temp.write_bytes(data)
            os.replace(temp, manifest_path)
            state = self._write_state(
                workdir,
                {
                    "schema": STATE_SCHEMA,
                    "dataset": entry.name,
                    "target": target,
                    "phase": "manifest_built",
                    "files": state["files"],
                    "counts": state["counts"],
                    "manifest_sha256": _sha256_bytes(data),
                    "version": None,
                },
            )
            report["steps"].append(
                {"step": "manifest_built", "manifest_sha256": state["manifest_sha256"]}
            )
        else:
            report["steps"].append(
                {
                    "step": "manifest_reused",
                    "manifest_sha256": state["manifest_sha256"],
                }
            )

        manifest_bytes = manifest_path.read_bytes()
        manifest_sha256 = state["manifest_sha256"]
        if _sha256_bytes(manifest_bytes) != manifest_sha256:  # pragma: no cover
            raise ExportVerificationError(
                f"{manifest_path} does not hash to the recorded "
                f"{manifest_sha256}; refusing to register bytes that are not "
                "the ones state.json vouches for."
            )
        report["manifest_object_id"] = manifest_sha256

        # 9. Upload every file and the manifest, PRIVATE, and confirm the jobs.
        uploads: List[Dict[str, Any]] = []
        for record in state["files"]:
            uploads.append(
                self._upload(
                    workdir / record["name"],
                    OBJECT_TYPE_FILE,
                    f"{entry.name}/{target}/{record['name']}",
                    record["name"],
                    record["sha256"],
                )
            )
        uploads.append(
            self._upload(
                manifest_bytes,
                OBJECT_TYPE_MANIFEST,
                f"{entry.name}/{target}/{MANIFEST_FILENAME}",
                MANIFEST_FILENAME,
                manifest_sha256,
            )
        )
        self._confirm_uploads(uploads, workdir)
        report["uploads"] = uploads
        report["steps"].append({"step": "uploaded", "objects": len(uploads)})

        # 10. Registration -- the step that makes the objects public.
        response = self._register(entry, manifest_sha256)
        version = (
            response.get("version", response.get("version_id"))
            if isinstance(response, Mapping)
            else response
        )
        state = self._write_state(
            workdir,
            {
                "schema": STATE_SCHEMA,
                "dataset": entry.name,
                "target": target,
                "phase": "registered",
                "files": state["files"],
                "counts": state["counts"],
                "manifest_sha256": manifest_sha256,
                "version": version,
            },
        )
        report["steps"].append({"step": "registered", "version": version})
        report["registered"] = True
        report["version"] = version
        report["phase"] = "registered"

        if keep_workdir:
            report["workdir_deleted"] = False
        else:
            shutil.rmtree(workdir, ignore_errors=True)
            report["workdir_deleted"] = not workdir.exists()
        report["ok"] = True
        return report


def _kbutillib_version() -> str:
    """This package's version, for the manifest's ``exporter`` block."""
    try:
        from kbutillib import __version__  # type: ignore[attr-defined]

        return str(__version__)
    except Exception:  # pragma: no cover - a source tree without the attribute
        return "unknown"
