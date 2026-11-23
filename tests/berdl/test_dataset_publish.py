"""``berdl.dataset_publish`` -- the pod-side publisher for KBDL's dataset mirror.

FAKE PORTS, REAL PARQUET. The three ports are faked (there is no BERDL pod,
no remote Spark cluster and no KBDL here), but every file the publisher reads
is a REAL zstd parquet file written by pyarrow: the row counts, sha256s,
sizes and Arrow schema in the manifest are measured from actual bytes, so a
manifest that claims the wrong thing fails here rather than in front of a
partner. The thin in-pod adapters (``InPodLakehouse``, ``InPodStaging``) are
deliberately not tested -- they need the pod, and the rule that makes them
safe (:func:`validate_staging_root`, :func:`require_prefix_under_root`) is a
PURE FUNCTION tested directly below rather than through a fake.

THE TWO FIXTURES THAT CARRY THE MOST WEIGHT:

* ``test_publish_pins_the_target_even_when_the_head_advances`` -- the fake
  lakehouse COMMITS A NEW SNAPSHOT between the status read and the export.
  The export, both counts and the manifest must all still name the original
  target, and ``current_snapshot_id`` must be read exactly once.
* ``test_status_unknown_for_a_rollback_older_by_committed_at`` -- the
  published snapshot is OLDER than the target by ``committed_at`` while not
  being its ancestor at all. A timestamp-based check reads that as
  "unchanged" and skips the re-export; the parent walk reads it as
  ``unknown`` and proceeds.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("requests", reason="the KBDL client's error classes need requests")

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from kbutillib.domains.external.kbdl_service_utils import (  # noqa: E402
    KBDLConflictError,
    KBDLNotFoundError,
)
from kbutillib.domains.kbase.berdl import dataset_publish as dp  # noqa: E402

# Iceberg snapshot ids above 2**53, which is the whole reason they travel as
# strings: a JSON number would be corrupted by a double round-trip.
S_ROOT = "9007199254740993"
S_PUB = "9007199254740994"
S_MID = "9007199254740995"
S_HEAD = "9007199254740996"
S_FORK = "9007199254740997"

DATASET = "genome_clearhouse.genome_quality"
ROOT = "users-general-warehouse/chenry/_exports_staging"
COMMITTED = "2026-11-20T10:00:00Z"
#: A frozen export clock, so ``exported_at`` is reproducible in assertions.
NOW = datetime(2026, 11, 21, 18, 30, tzinfo=timezone.utc)


# ── Real parquet, written by pyarrow ─────────────────────────────────────


def parquet_bytes(rows: int = 3, variant: str = "a") -> bytes:
    """A real zstd parquet file, as the bytes a staging download would fetch."""
    if variant == "a":
        table = pa.table(
            {
                "genome_id": [f"g{index}" for index in range(rows)],
                "released": [True] * rows,
            }
        )
    else:
        table = pa.table({"distances": [f"d{index}" for index in range(rows)]})
    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression="zstd")
    return buffer.getvalue()


def snap(
    snapshot_id: str,
    parent_id: Optional[str],
    operation: str = "append",
    committed_at: str = COMMITTED,
    records: int = 3,
    size: int = 4096,
    summary: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """One ``<fqn>.snapshots`` row, with summary values as STRINGS."""
    if summary is None:
        summary = {"total-records": str(records), "total-files-size": str(size)}
    return {
        "snapshot_id": snapshot_id,
        "parent_id": parent_id,
        "committed_at": committed_at,
        "operation": operation,
        "summary": dict(summary),
    }


# ── The fakes ─────────────────────────────────────────────────────────────


class FakeLakehouse:
    """A :class:`dp.LakehousePort` that records every call.

    ``advance_to`` is the interesting knob: it makes the table commit a new
    snapshot the instant ``current_snapshot_id`` is read, which is the race a
    publisher that re-reads the head would lose.
    """

    def __init__(
        self,
        snapshots: Sequence[Mapping[str, Any]],
        head: str,
        staging: Optional["FakeStaging"] = None,
        payload: Optional[Mapping[str, bytes]] = None,
        spark_count: Any = None,
        advance_to: Optional[str] = None,
        export_error: Optional[BaseException] = None,
    ) -> None:
        self._snapshots = [dict(row) for row in snapshots]
        self._head = head
        self.staging = staging
        self.payload = dict(payload or {})
        self._spark_count = spark_count
        self._advance_to = advance_to
        self.export_error = export_error
        self.head_reads = 0
        self.snapshot_reads = 0
        self.export_calls: List[Dict[str, Any]] = []
        self.count_calls: List[tuple] = []

    def current_snapshot_id(self, fqn: str) -> str:
        self.head_reads += 1
        head = self._head
        if self._advance_to is not None:
            self._head = self._advance_to
        return head

    def snapshots(self, fqn: str) -> List[Dict[str, Any]]:
        self.snapshot_reads += 1
        return [dict(row) for row in self._snapshots]

    def spark_count(self, fqn: str, snapshot_id: str) -> int:
        self.count_calls.append((fqn, snapshot_id))
        if callable(self._spark_count):
            return int(self._spark_count(snapshot_id))
        if self._spark_count is not None:
            return int(self._spark_count)
        for row in self._snapshots:
            if row["snapshot_id"] == snapshot_id:
                return int(row["summary"]["total-records"])
        raise AssertionError(f"no snapshot {snapshot_id} to count")

    def export(
        self, fqn: str, snapshot_id: str, staging_uri: str, n_files: int
    ) -> None:
        self.export_calls.append(
            {
                "fqn": fqn,
                "snapshot_id": snapshot_id,
                "uri": staging_uri,
                "n_files": n_files,
            }
        )
        if self.export_error is not None:
            raise self.export_error
        prefix = staging_uri.split(f"s3a://{dp.STAGING_BUCKET}/", 1)[1]
        assert self.staging is not None
        self.staging.put(prefix, self.payload)


class FakeStaging:
    """A :class:`dp.StagingPort` over a dict, enforcing the REAL prefix guard."""

    def __init__(
        self, root: str = ROOT, objects: Optional[Mapping[str, bytes]] = None
    ) -> None:
        self.root = dp.validate_staging_root(root)
        self.objects: Dict[str, bytes] = dict(objects or {})
        self.list_calls: List[str] = []
        self.download_calls: List[str] = []
        self.delete_calls: List[str] = []
        self.delete_error: Optional[BaseException] = None

    def put(self, prefix: str, payload: Mapping[str, bytes]) -> None:
        for name, data in payload.items():
            self.objects[prefix + name] = data

    def _under(self, prefix: str) -> List[str]:
        return sorted(name for name in self.objects if name.startswith(prefix))

    def list(self, prefix: str) -> List[str]:
        self.list_calls.append(prefix)
        dp.require_prefix_under_root(prefix, self.root)
        return self._under(prefix)

    def download(self, prefix: str, local_dir: Path) -> List[Path]:
        self.download_calls.append(prefix)
        dp.require_prefix_under_root(prefix, self.root)
        target = Path(local_dir)
        target.mkdir(parents=True, exist_ok=True)
        paths = []
        for name in self._under(prefix):
            destination = target / Path(name).name
            destination.write_bytes(self.objects[name])
            paths.append(destination)
        return paths

    def delete(self, prefix: str) -> None:
        self.delete_calls.append(prefix)
        dp.require_prefix_under_root(prefix, self.root)
        if self.delete_error is not None:
            raise self.delete_error
        for name in self._under(prefix):
            del self.objects[name]


class FakeKBDL:
    """A :class:`dp.KBDLPort`: content-addressed uploads, pollable jobs, a registry."""

    def __init__(
        self,
        latest: Optional[Mapping[str, Any]] = None,
        fail_filenames: Sequence[str] = (),
        stored_ids: Optional[Mapping[str, str]] = None,
        register: Optional[Mapping[str, Any]] = None,
        register_error: Optional[BaseException] = None,
        drop_object_id: Sequence[str] = (),
        dedup_filenames: Sequence[str] = (),
    ) -> None:
        self.latest = None if latest is None else dict(latest)
        self.fail_filenames = set(fail_filenames)
        self.stored_ids = dict(stored_ids or {})
        self.register = dict(register or {"version": 3, "dataset": DATASET})
        self.register_error = register_error
        self.drop_object_id = set(drop_object_id)
        # Filenames the service answers 201 for: the content already exists,
        # so no KBDLUploadObject job is queued and there is nothing to poll.
        self.dedup_filenames = set(dedup_filenames)
        self.uploads: List[Dict[str, Any]] = []
        self.polls: List[List[str]] = []
        self.registrations: List[tuple] = []
        self.version_reads: List[tuple] = []
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def get_dataset_version(self, name: str, version: str) -> Dict[str, Any]:
        self.version_reads.append((name, version))
        if self.latest is None:
            raise KBDLNotFoundError(f"dataset {name!r} has no registered versions")
        return dict(self.latest)

    def upload_object(
        self,
        file: Any,
        object_type: str,
        name: str,
        visibility: str,
        filename: Optional[str] = None,
        transform: str = "none",
    ) -> Dict[str, Any]:
        data = (
            bytes(file)
            if isinstance(file, (bytes, bytearray))
            else Path(file).read_bytes()
        )
        sha256 = hashlib.sha256(data).hexdigest()
        job_id = f"job-{len(self.uploads)}"
        self.uploads.append(
            {
                "object_type": object_type,
                "name": name,
                "visibility": visibility,
                "filename": filename,
                "transform": transform,
                "sha256": sha256,
                "job_id": job_id,
                "bytes": data,
            }
        )
        label = filename or name
        if label in self.dedup_filenames:
            self.uploads[-1]["job_id"] = None
            return {"object_id": self.stored_ids.get(label, sha256)}
        result: Dict[str, Any] = {}
        if label not in self.drop_object_id:
            result["object_id"] = self.stored_ids.get(label, sha256)
        self._jobs[job_id] = {
            "state": "failed" if label in self.fail_filenames else "completed",
            "result": result,
        }
        return {"job_id": job_id, "expected_object_id": sha256}

    def poll_jobs_until_terminal(
        self, job_ids: Sequence[str], **kwargs: Any
    ) -> Dict[str, Dict[str, Any]]:
        self.polls.append(list(job_ids))
        return {job_id: dict(self._jobs[job_id]) for job_id in job_ids}

    def publish_dataset_version(
        self, name: str, manifest_object_id: str
    ) -> Dict[str, Any]:
        self.registrations.append((name, manifest_object_id))
        if self.register_error is not None:
            raise self.register_error
        return dict(self.register)

    def manifest_upload(self) -> Dict[str, Any]:
        """The one ``DatasetManifest`` upload, or an assertion failure."""
        manifests = [
            record
            for record in self.uploads
            if record["object_type"] == dp.OBJECT_TYPE_MANIFEST
        ]
        assert len(manifests) == 1, (
            f"expected one manifest upload, got {len(manifests)}"
        )
        return manifests[0]


# ── Catalog fixtures ──────────────────────────────────────────────────────

README_TEXT = "# genome_clearhouse.genome_quality\n\nA test readme.\n"


def write_catalog(
    directory: Path,
    approved: bool = True,
    target_file_bytes: int = dp.DEFAULT_TARGET_FILE_BYTES,
) -> Path:
    """A one-entry catalog plus its README, in a temp directory."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "genome_quality.md").write_text(README_TEXT, encoding="utf-8")
    by = "Chris Henry" if approved else ""
    on = "2026-11-21" if approved else ""
    (directory / "datasets.toml").write_text(
        "\n".join(
            [
                f'[datasets."{DATASET}"]',
                'tenant = "kbaseincubator"',
                'namespace = "genome_clearhouse"',
                'table = "genome_quality"',
                f"target_file_bytes = {target_file_bytes}",
                'readme_file = "genome_quality.md"',
                f'readme_approved_by = "{by}"',
                f'readme_approved_on = "{on}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return directory / "datasets.toml"


def build(
    tmp_path: Path,
    snapshots: Optional[Sequence[Mapping[str, Any]]] = None,
    head: str = S_HEAD,
    latest: Optional[Mapping[str, Any]] = None,
    payload: Optional[Mapping[str, bytes]] = None,
    approved: bool = True,
    root: Optional[str] = ROOT,
    staging_objects: Optional[Mapping[str, bytes]] = None,
    kbdl: Optional[FakeKBDL] = None,
    **lakehouse_kwargs: Any,
) -> tuple:
    """A publisher wired to the three fakes, with a temp catalog and workdir."""
    if snapshots is None:
        snapshots = [snap(S_HEAD, None)]
    if payload is None:
        payload = {"part-00000-abc.zstd.parquet": parquet_bytes(3), "_SUCCESS": b""}
    catalog = write_catalog(tmp_path / "catalog", approved=approved)
    staging = FakeStaging(objects=staging_objects)
    lake = FakeLakehouse(
        snapshots, head, staging=staging, payload=payload, **lakehouse_kwargs
    )
    client = FakeKBDL(latest=latest) if kbdl is None else kbdl
    publisher = dp.DatasetPublisher(
        lake,
        staging,
        client,
        catalog_path=catalog,
        staging_root=root,
        workdir_root=tmp_path / "work",
        env={},
        now=lambda: NOW,
    )
    return publisher, lake, staging, client


# ── The staging root and prefix rules (PURE functions, tested directly) ──


def test_an_unset_staging_root_is_refused() -> None:
    """No default. ``None`` and blank are both refusals naming the setting."""
    for value in (None, "", "   "):
        with pytest.raises(dp.StagingRootError) as caught:
            dp.validate_staging_root(value)
        assert dp.STAGING_ROOT_ENV in str(caught.value)


def test_a_tenant_warehouse_staging_root_is_refused() -> None:
    """``tenant-general-warehouse/`` is someone else's data, and this port deletes."""
    with pytest.raises(dp.StagingRootError) as caught:
        dp.validate_staging_root(
            "tenant-general-warehouse/kbaseincubator/_exports_staging"
        )
    assert "tenant-general-warehouse/" in str(caught.value)


def test_a_root_without_the_exports_staging_suffix_is_refused() -> None:
    with pytest.raises(dp.StagingRootError):
        dp.validate_staging_root("users-general-warehouse/chenry/exports")
    with pytest.raises(dp.StagingRootError):
        dp.validate_staging_root("_exports_staging")


def test_a_valid_root_is_normalised_without_its_trailing_slash() -> None:
    assert dp.validate_staging_root(ROOT + "/") == ROOT
    assert dp.validate_staging_root(ROOT) == ROOT


def test_require_staging_root_reads_the_environment() -> None:
    assert dp.require_staging_root({dp.STAGING_ROOT_ENV: ROOT}) == ROOT
    with pytest.raises(dp.StagingRootError):
        dp.require_staging_root({})


def test_the_staging_port_refuses_a_prefix_outside_the_root() -> None:
    """Including the root ITSELF: a delete there would hit concurrent exports."""
    assert dp.require_prefix_under_root(f"{ROOT}/x/", ROOT) == f"{ROOT}/x/"
    for prefix in (
        "tenant-general-warehouse/kbaseincubator/genome_clearhouse/",
        "users-general-warehouse/chenry/_exports_staging_other/x/",
        ROOT,
    ):
        with pytest.raises(dp.StagingPrefixError):
            dp.require_prefix_under_root(prefix, ROOT)


def test_every_staging_method_goes_through_the_guard(tmp_path: Path) -> None:
    staging = FakeStaging()
    for call in (
        lambda: staging.list("elsewhere/x/"),
        lambda: staging.download("elsewhere/x/", tmp_path),
        lambda: staging.delete("elsewhere/x/"),
    ):
        with pytest.raises(dp.StagingPrefixError):
            call()


def test_the_spark_uri_for_a_prefix() -> None:
    assert dp.staging_uri(f"{ROOT}/a/b/") == f"s3a://cdm-lake/{ROOT}/a/b/"


# ── status: the five outcomes ─────────────────────────────────────────────


def test_status_no_versions(tmp_path: Path) -> None:
    publisher, lake, _, client = build(tmp_path, latest=None)
    verdict = publisher.status(DATASET)
    assert verdict["status"] == "no_versions"
    assert verdict["target"] == S_HEAD
    assert verdict["operations"] == []
    assert client.version_reads == [(DATASET, "latest")]


def test_status_unchanged(tmp_path: Path) -> None:
    publisher, _, _, _ = build(
        tmp_path,
        snapshots=[snap(S_HEAD, None)],
        latest={"version": 1, "source_snapshot_id": S_HEAD},
    )
    verdict = publisher.status(DATASET)
    assert verdict["status"] == "unchanged"
    assert verdict["operations"] == []


def test_status_compaction_only(tmp_path: Path) -> None:
    """Every snapshot after the published one is a ``replace``: no rows changed."""
    publisher, _, _, _ = build(
        tmp_path,
        snapshots=[
            snap(S_PUB, S_ROOT, operation="append"),
            snap(S_MID, S_PUB, operation="replace"),
            snap(S_HEAD, S_MID, operation="replace"),
        ],
        latest={"version": 1, "source_snapshot_id": S_PUB},
    )
    verdict = publisher.status(DATASET)
    assert verdict["status"] == "compaction_only"
    assert [step["snapshot_id"] for step in verdict["operations"]] == [S_HEAD, S_MID]
    assert {step["operation"] for step in verdict["operations"]} == {"replace"}


def test_status_changed_lists_the_operations(tmp_path: Path) -> None:
    publisher, _, _, _ = build(
        tmp_path,
        snapshots=[
            snap(S_PUB, S_ROOT, operation="append"),
            snap(S_MID, S_PUB, operation="append"),
            snap(S_HEAD, S_MID, operation="replace"),
        ],
        latest={"version": 1, "source_snapshot_id": S_PUB},
    )
    verdict = publisher.status(DATASET)
    assert verdict["status"] == "changed"
    assert [
        (step["snapshot_id"], step["operation"]) for step in verdict["operations"]
    ] == [
        (S_HEAD, "replace"),
        (S_MID, "append"),
    ]


def test_status_unknown_for_a_broken_parent_chain(tmp_path: Path) -> None:
    """The target's parent is not in ``snapshots`` (expired), so ancestry is unknown."""
    publisher, _, _, _ = build(
        tmp_path,
        snapshots=[snap(S_HEAD, S_MID, operation="append")],
        latest={"version": 1, "source_snapshot_id": S_PUB},
    )
    verdict = publisher.status(DATASET)
    assert verdict["status"] == "unknown"
    assert [step["snapshot_id"] for step in verdict["operations"]] == [S_HEAD]


def test_status_unknown_for_a_rollback_older_by_committed_at(tmp_path: Path) -> None:
    """THE ROLLBACK CASE a clock-based check gets backwards.

    The published snapshot ``S_FORK`` was committed LATER than the current
    target ``S_PUB``, but a rollback made ``S_PUB`` current again, so the
    published id is not an ancestor of the target. A ``committed_at``
    comparison reads "the target is older, so nothing new, skip"; the parent
    walk reads ``unknown`` and a publish therefore proceeds.
    """
    publisher, _, _, _ = build(
        tmp_path,
        snapshots=[
            snap(S_ROOT, None, committed_at="2026-11-01T00:00:00Z"),
            snap(S_PUB, S_ROOT, committed_at="2026-11-02T00:00:00Z"),
            snap(S_FORK, S_ROOT, committed_at="2026-11-09T00:00:00Z"),
        ],
        head=S_PUB,
        latest={"version": 1, "source_snapshot_id": S_FORK},
    )
    verdict = publisher.status(DATASET)
    assert verdict["status"] == "unknown"
    assert verdict["target"] == S_PUB
    # The published snapshot really is the NEWER one by timestamp: this test
    # would pass vacuously if the fixture did not have that shape.
    assert verdict["operations"][0]["committed_at"] < "2026-11-09T00:00:00Z"


def test_status_target_may_be_pinned_by_the_caller(tmp_path: Path) -> None:
    publisher, lake, _, _ = build(
        tmp_path,
        snapshots=[snap(S_PUB, S_ROOT), snap(S_HEAD, S_PUB)],
        latest={"version": 1, "source_snapshot_id": S_PUB},
    )
    verdict = publisher.status(DATASET, target=S_PUB)
    assert verdict["status"] == "unchanged"
    assert lake.head_reads == 0


def test_status_accepts_a_nested_source_snapshot_id(tmp_path: Path) -> None:
    publisher, _, _, _ = build(
        tmp_path,
        snapshots=[snap(S_HEAD, None)],
        latest={"version": 1, "source": {"snapshot_id": S_HEAD}},
    )
    assert publisher.status(DATASET)["status"] == "unchanged"


def test_a_404_by_attribute_also_reads_as_no_versions(tmp_path: Path) -> None:
    """A client that raises its own error class is still understood."""

    class HttpError(Exception):
        status_code = 404

    publisher, _, _, client = build(tmp_path)

    def raise_404(name: str, version: str) -> Dict[str, Any]:
        raise HttpError("not found")

    client.get_dataset_version = raise_404  # type: ignore[assignment]
    assert publisher.status(DATASET)["status"] == "no_versions"


def test_a_non_404_version_read_error_is_not_swallowed(tmp_path: Path) -> None:
    publisher, _, _, client = build(tmp_path)

    def raise_500(name: str, version: str) -> Dict[str, Any]:
        raise RuntimeError("gateway exploded")

    client.get_dataset_version = raise_500  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="gateway exploded"):
        publisher.status(DATASET)


# ── status: the warnings half ─────────────────────────────────────────────


def test_status_lists_leftover_staging_without_deleting_it(tmp_path: Path) -> None:
    leftovers = {f"{ROOT}/{DATASET}/{S_MID}-abc/part-00000-x.zstd.parquet": b"stale"}
    publisher, _, staging, _ = build(tmp_path, staging_objects=leftovers)
    verdict = publisher.status(DATASET)
    assert len(verdict["warnings"]) == 1
    assert "leftover" in verdict["warnings"][0]
    assert staging.delete_calls == []
    assert set(staging.objects) == set(leftovers)


def test_status_says_so_when_the_staging_root_is_unset(tmp_path: Path) -> None:
    publisher, _, staging, _ = build(tmp_path, root=None)
    verdict = publisher.status(DATASET)
    assert verdict["status"] == "no_versions"
    assert any(dp.STAGING_ROOT_ENV in warning for warning in verdict["warnings"])
    assert staging.list_calls == []


# ── Helpers for reading what a publish produced ──────────────────────────


def manifest_of(client: FakeKBDL) -> Dict[str, Any]:
    """The manifest EXACTLY as uploaded, parsed from the uploaded bytes."""
    return json.loads(client.manifest_upload()["bytes"].decode("utf-8"))


def state_of(tmp_path: Path, target: str = S_HEAD) -> Dict[str, Any]:
    return json.loads((workdir_of(tmp_path, target) / "state.json").read_text())


def workdir_of(tmp_path: Path, target: str = S_HEAD) -> Path:
    return tmp_path / "work" / DATASET / target


def seed_workdir(
    tmp_path: Path,
    phase: str,
    data: Optional[bytes] = None,
    target: str = S_HEAD,
    schema: int = 1,
    state_text: Optional[str] = None,
    junk: bool = False,
) -> Path:
    """A workdir as an interrupted run would have left it."""
    workdir = workdir_of(tmp_path, target)
    workdir.mkdir(parents=True, exist_ok=True)
    if junk:
        (workdir / "junk.txt").write_text("left over from a run that died")
    files: List[Dict[str, Any]] = []
    if data is not None:
        part = workdir / "part-00000.parquet"
        part.write_bytes(data)
        files = [
            {
                "name": part.name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "row_count": pq.ParquetFile(part).metadata.num_rows,
            }
        ]
    rows = files[0]["row_count"] if files else 0
    state = {
        "schema": schema,
        "dataset": DATASET,
        "target": target,
        "phase": phase,
        "files": files,
        "counts": {
            "snapshot_total_records": rows,
            "spark_count": rows,
            "pyarrow_count": rows,
        },
        "manifest_sha256": None,
        "version": None,
    }
    text = (
        json.dumps(state, indent=2, sort_keys=True)
        if state_text is None
        else state_text
    )
    (workdir / "state.json").write_text(text, encoding="utf-8")
    return workdir


# ── publish: the happy path, and the pin ─────────────────────────────────


def test_publish_exports_verifies_uploads_and_registers(tmp_path: Path) -> None:
    publisher, lake, staging, client = build(tmp_path)
    report = publisher.publish(DATASET)

    assert report["ok"] is True
    assert report["registered"] is True
    assert report["version"] == 3
    assert report["status"] == "no_versions"
    assert report["skipped"] is False
    assert report["counts"] == {
        "snapshot_total_records": 3,
        "spark_count": 3,
        "pyarrow_count": 3,
    }
    assert [record["name"] for record in report["files"]] == ["part-00000.parquet"]
    assert [step["step"] for step in report["steps"]] == [
        "export",
        "download",
        "staging_deleted",
        "renamed",
        "verified",
        "manifest_built",
        "uploaded",
        "registered",
    ]
    assert report["warnings"] == []
    # The staging prefix is this run's, under the root, and it is gone.
    assert report["staging_prefix"].startswith(f"{ROOT}/{DATASET}/{S_HEAD}-")
    assert staging.delete_calls == [report["staging_prefix"]]
    assert staging.objects == {}
    # The workdir is removed on success; keep_workdir is for failures.
    assert not workdir_of(tmp_path).exists()
    assert report["workdir_deleted"] is True
    assert client.registrations == [(DATASET, report["manifest_object_id"])]
    assert lake.export_calls[0]["n_files"] == 1


def test_publish_keeps_the_workdir_when_asked(tmp_path: Path) -> None:
    publisher, _, _, _ = build(tmp_path)
    report = publisher.publish(DATASET, keep_workdir=True)
    assert report["workdir_deleted"] is False
    assert state_of(tmp_path)["phase"] == "registered"
    assert state_of(tmp_path)["version"] == 3


def test_publish_pins_the_target_even_when_the_head_advances(tmp_path: Path) -> None:
    """The table commits a new snapshot the moment the head is read.

    ``current_snapshot_id`` is read ONCE, and the export, both counts and the
    manifest all name the snapshot that read returned -- never the newer one.
    """
    publisher, lake, _, client = build(
        tmp_path,
        snapshots=[snap(S_PUB, None), snap(S_HEAD, S_PUB), snap(S_FORK, S_HEAD)],
        head=S_HEAD,
        advance_to=S_FORK,
    )
    report = publisher.publish(DATASET)

    assert lake.head_reads == 1
    assert report["target"] == S_HEAD
    assert [call["snapshot_id"] for call in lake.export_calls] == [S_HEAD]
    assert {snapshot_id for _, snapshot_id in lake.count_calls} == {S_HEAD}
    manifest = manifest_of(client)
    assert manifest["source"]["snapshot_id"] == S_HEAD
    assert S_HEAD in report["staging_prefix"]


def test_publish_proceeds_on_unknown(tmp_path: Path) -> None:
    publisher, lake, _, client = build(
        tmp_path,
        snapshots=[snap(S_HEAD, S_MID)],
        latest={"version": 1, "source_snapshot_id": S_PUB},
    )
    report = publisher.publish(DATASET)
    assert report["status"] == "unknown"
    assert report["would_skip"] is False
    assert report["registered"] is True
    assert len(lake.export_calls) == 1


@pytest.mark.parametrize(
    ("snapshots", "expected"),
    [
        ([snap(S_HEAD, S_PUB)], "unchanged"),
        (
            [snap(S_PUB, S_ROOT), snap(S_HEAD, S_PUB, operation="replace")],
            "compaction_only",
        ),
    ],
)
def test_unchanged_and_compaction_only_stop_unless_forced(
    tmp_path: Path, snapshots: List[Dict[str, Any]], expected: str
) -> None:
    published = S_HEAD if expected == "unchanged" else S_PUB
    publisher, lake, staging, client = build(
        tmp_path,
        snapshots=snapshots,
        latest={"version": 1, "source_snapshot_id": published},
    )
    report = publisher.publish(DATASET)
    assert report["status"] == expected
    assert report["would_skip"] is True
    assert report["skipped"] is True
    assert report["ok"] is True
    assert "force" in report["reason"]
    assert lake.export_calls == []
    assert client.uploads == []
    assert client.registrations == []
    assert staging.download_calls == []

    # ... and force overrides exactly that skip, nothing else.
    forced = publisher.publish(DATASET, force=True)
    assert forced["would_skip"] is False
    assert forced["registered"] is True
    assert len(lake.export_calls) == 1


# ── publish: the dry run ─────────────────────────────────────────────────


def test_dry_run_touches_no_port_and_still_reports_everything(tmp_path: Path) -> None:
    """On an UNCHANGED dataset: the full report, ``would_skip`` true, no I/O."""
    publisher, lake, staging, client = build(
        tmp_path,
        snapshots=[snap(S_HEAD, S_PUB)],
        latest={"version": 1, "source_snapshot_id": S_HEAD},
    )
    report = publisher.publish(DATASET, dry_run=True)

    assert report["dry_run"] is True
    assert report["status"] == "unchanged"
    assert report["would_skip"] is True
    assert report["target"] == S_HEAD
    assert report["snapshot_total_records"] == 3
    assert report["snapshot_total_files_size"] == 4096
    assert report["n_files"] == 1
    assert report["staging_prefix"] == f"{ROOT}/{DATASET}/{S_HEAD}-<uuid4>/"
    assert report["readme_approved"] is True
    assert report["workdir_state"] is None

    assert lake.export_calls == []
    assert lake.count_calls == []
    assert staging.list_calls == []
    assert staging.download_calls == []
    assert staging.delete_calls == []
    assert client.uploads == []
    assert client.polls == []
    assert client.registrations == []
    assert not workdir_of(tmp_path).exists()


def test_dry_run_reports_the_workdir_phase_without_touching_it(tmp_path: Path) -> None:
    publisher, _, _, _ = build(tmp_path)
    workdir = seed_workdir(tmp_path, "exported", parquet_bytes(3))
    before = (workdir / "state.json").read_bytes()

    report = publisher.publish(DATASET, dry_run=True)
    assert report["workdir_state"] == {
        "schema": 1,
        "phase": "exported",
        "target": S_HEAD,
        "matches_target": True,
        "manifest_sha256": None,
    }
    # A dry run may not delete a workdir someone is about to resume.
    assert (workdir / "state.json").read_bytes() == before
    assert (workdir / "part-00000.parquet").exists()


def test_an_unapproved_readme_refuses_a_publish_but_not_a_dry_run(
    tmp_path: Path,
) -> None:
    publisher, lake, _, client = build(tmp_path, approved=False)

    dry = publisher.publish(DATASET, dry_run=True)
    assert dry["readme_approved"] is False
    assert dry["ok"] is True
    assert lake.export_calls == []

    with pytest.raises(dp.ReadmeNotApprovedError) as caught:
        publisher.publish(DATASET)
    message = str(caught.value)
    assert "readme_approved_by" in message and "readme_approved_on" in message
    assert lake.export_calls == []
    assert client.uploads == []


def test_a_dry_run_reports_an_unset_staging_root_instead_of_raising(
    tmp_path: Path,
) -> None:
    publisher, _, _, _ = build(tmp_path, root=None)
    report = publisher.publish(DATASET, dry_run=True)
    assert report["staging_root"] is None
    assert dp.STAGING_ROOT_ENV in report["staging_root_error"]
    assert report["staging_prefix"] is None
    assert report["n_files"] == 1


@pytest.mark.parametrize(
    "root", [None, "tenant-general-warehouse/kbaseincubator/_exports_staging"]
)
def test_a_real_publish_refuses_an_unset_or_tenant_warehouse_root(
    tmp_path: Path, root: Optional[str]
) -> None:
    _, lake, staging, client = build(tmp_path, root=None)
    publisher = dp.DatasetPublisher(
        lake,
        staging,
        client,
        catalog_path=write_catalog(tmp_path / "catalog"),
        staging_root=root,
        workdir_root=tmp_path / "work",
        env={},
    )
    with pytest.raises(dp.StagingRootError):
        publisher.publish(DATASET)
    assert lake.export_calls == []
    assert client.uploads == []


# ── publish: every abort, and staging cleaned up on all of them ──────────


def test_a_three_way_count_disagreement_aborts_before_any_upload(
    tmp_path: Path,
) -> None:
    publisher, lake, staging, client = build(tmp_path, spark_count=99)
    with pytest.raises(dp.CountMismatchError) as caught:
        publisher.publish(DATASET)
    assert "99" in str(caught.value)
    assert client.uploads == []
    assert client.registrations == []
    assert staging.delete_calls and staging.objects == {}


def test_a_summary_without_total_records_aborts(tmp_path: Path) -> None:
    publisher, lake, _, client = build(
        tmp_path,
        snapshots=[snap(S_HEAD, None, summary={"total-files-size": "4096"})],
    )
    with pytest.raises(dp.SnapshotSummaryError) as caught:
        publisher.publish(DATASET)
    assert "total-records" in str(caught.value)
    assert lake.export_calls == []
    assert client.uploads == []


def test_a_summary_without_total_files_size_aborts(tmp_path: Path) -> None:
    publisher, lake, _, _ = build(
        tmp_path, snapshots=[snap(S_HEAD, None, summary={"total-records": "3"})]
    )
    with pytest.raises(dp.SnapshotSummaryError) as caught:
        publisher.publish(DATASET)
    assert "total-files-size" in str(caught.value)
    assert lake.export_calls == []


def test_zero_total_records_is_refused(tmp_path: Path) -> None:
    """``"0"`` is a PRESENT value, so this is the empty-table refusal."""
    publisher, lake, _, _ = build(tmp_path, snapshots=[snap(S_HEAD, None, records=0)])
    with pytest.raises(dp.EmptyTableError):
        publisher.publish(DATASET)
    assert lake.export_calls == []


def test_an_export_with_zero_parquet_files_aborts(tmp_path: Path) -> None:
    """The remote-Spark failure mode: a prefix holding only ``_SUCCESS``."""
    publisher, _, staging, client = build(tmp_path, payload={"_SUCCESS": b""})
    with pytest.raises(dp.ExportVerificationError) as caught:
        publisher.publish(DATASET)
    assert "parquet" in str(caught.value)
    assert client.uploads == []
    assert staging.delete_calls and staging.objects == {}


def test_staging_is_deleted_when_the_export_itself_raises(tmp_path: Path) -> None:
    publisher, lake, staging, client = build(
        tmp_path, export_error=RuntimeError("spark session died")
    )
    with pytest.raises(RuntimeError, match="spark session died"):
        publisher.publish(DATASET)
    assert len(staging.delete_calls) == 1
    assert (
        staging.delete_calls[0]
        == lake.export_calls[0]["uri"].split(f"s3a://{dp.STAGING_BUCKET}/", 1)[1]
    )
    assert client.uploads == []


def test_a_failed_delete_is_a_warning_and_does_not_mask_the_export_error(
    tmp_path: Path,
) -> None:
    publisher, _, staging, _ = build(
        tmp_path, export_error=RuntimeError("spark session died")
    )
    staging.delete_error = RuntimeError("minio unreachable")
    with pytest.raises(RuntimeError, match="spark session died"):
        publisher.publish(DATASET)


def test_staging_is_deleted_when_an_upload_fails(tmp_path: Path) -> None:
    client = FakeKBDL(fail_filenames=["part-00000.parquet"])
    publisher, _, staging, _ = build(tmp_path, kbdl=client)
    with pytest.raises(dp.UploadNotConfirmedError) as caught:
        publisher.publish(DATASET)
    assert "NOTHING has been registered" in str(caught.value)
    assert staging.delete_calls and staging.objects == {}
    # NOTHING registered, and the workdir KEPT so the retry reuses the manifest.
    assert client.registrations == []
    assert state_of(tmp_path)["phase"] == "manifest_built"


def test_an_object_id_that_is_not_the_sha256_aborts(tmp_path: Path) -> None:
    client = FakeKBDL(stored_ids={"part-00000.parquet": "f" * 64})
    publisher, _, _, _ = build(tmp_path, kbdl=client)
    with pytest.raises(dp.ObjectIdMismatchError) as caught:
        publisher.publish(DATASET)
    assert "f" * 64 in str(caught.value)
    assert client.registrations == []


def test_a_confirmed_upload_with_no_object_id_aborts(tmp_path: Path) -> None:
    client = FakeKBDL(drop_object_id=[dp.MANIFEST_FILENAME])
    publisher, _, _, _ = build(tmp_path, kbdl=client)
    with pytest.raises(dp.ObjectIdMismatchError) as caught:
        publisher.publish(DATASET)
    assert "no object id" in str(caught.value)
    assert client.registrations == []


def test_every_upload_is_private_and_correctly_typed(tmp_path: Path) -> None:
    publisher, _, _, client = build(tmp_path)
    publisher.publish(DATASET)
    assert [record["visibility"] for record in client.uploads] == ["private", "private"]
    assert [record["object_type"] for record in client.uploads] == [
        dp.OBJECT_TYPE_FILE,
        dp.OBJECT_TYPE_MANIFEST,
    ]
    assert [record["filename"] for record in client.uploads] == [
        "part-00000.parquet",
        dp.MANIFEST_FILENAME,
    ]
    # The display names carry the dataset and the pinned snapshot.
    assert all(f"{DATASET}/{S_HEAD}/" in record["name"] for record in client.uploads)


def test_a_201_dedup_upload_queues_no_job_and_still_publishes(tmp_path: Path) -> None:
    """``POST /objects`` answers 201 when the content already exists."""
    client = FakeKBDL(dedup_filenames=["part-00000.parquet"])
    publisher, _, _, _ = build(tmp_path, kbdl=client)
    report = publisher.publish(DATASET)
    assert report["registered"] is True
    assert client.polls == [["job-1"]]


def test_two_part_files_are_renamed_in_sorted_order_and_success_is_ignored(
    tmp_path: Path,
) -> None:
    """Spark's uuid-bearing names become ``part-NNNNN.parquet``, in order."""
    publisher, _, _, client = build(
        tmp_path,
        payload={
            "part-00001-zzz-c000.zstd.parquet": parquet_bytes(1),
            "part-00000-aaa-c000.zstd.parquet": parquet_bytes(2),
            "_SUCCESS": b"",
        },
    )
    report = publisher.publish(DATASET)
    assert [record["name"] for record in report["files"]] == [
        "part-00000.parquet",
        "part-00001.parquet",
    ]
    assert [record["row_count"] for record in report["files"]] == [2, 1]
    manifest = manifest_of(client)
    assert manifest["row_count"] == 3
    assert sum(entry["row_count"] for entry in manifest["files"]) == 3


def test_files_with_different_arrow_schemas_abort(tmp_path: Path) -> None:
    publisher, _, _, client = build(
        tmp_path,
        payload={
            "part-00000-aaa.zstd.parquet": parquet_bytes(2, variant="a"),
            "part-00001-bbb.zstd.parquet": parquet_bytes(1, variant="b"),
        },
    )
    with pytest.raises(dp.ExportVerificationError, match="Arrow schema"):
        publisher.publish(DATASET)
    assert client.uploads == []


# ── publish: the durable record and every resume ─────────────────────────


def test_state_json_is_written_atomically_with_exactly_the_listed_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replacements: List[tuple] = []
    real_replace = os.replace

    def recording_replace(src: Any, dst: Any, **kwargs: Any) -> None:
        replacements.append((str(src), str(dst)))
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", recording_replace)

    client = FakeKBDL(register_error=TimeoutError("registration timed out"))
    publisher, _, _, _ = build(tmp_path, kbdl=client)
    with pytest.raises(TimeoutError):
        publisher.publish(DATASET)

    workdir = workdir_of(tmp_path)
    state = state_of(tmp_path)
    assert sorted(state) == sorted(dp.STATE_KEYS)
    assert state["schema"] == 1
    assert state["dataset"] == DATASET
    assert state["target"] == S_HEAD
    assert state["version"] is None
    assert dp.OBJECT_ID_RE.match(state["manifest_sha256"])

    # Every state.json write landed through os.replace from a temp file in the
    # SAME directory, and no temp file survives.
    state_writes = [pair for pair in replacements if pair[1].endswith("state.json")]
    assert len(state_writes) == 2  # exported, then manifest_built
    for source, destination in state_writes:
        assert Path(source).parent == Path(destination).parent
        assert ".tmp" in source
    assert not list(workdir.glob("*.tmp")) and not list(workdir.glob(".*.tmp"))


def test_a_registration_timeout_leaves_phase_manifest_built(tmp_path: Path) -> None:
    """The next run must re-POST THE SAME id, so the manifest is kept."""
    client = FakeKBDL(register_error=TimeoutError("registration timed out"))
    publisher, _, _, _ = build(tmp_path, kbdl=client)
    with pytest.raises(TimeoutError):
        publisher.publish(DATASET)
    state = state_of(tmp_path)
    assert state["phase"] == "manifest_built"
    assert state["version"] is None
    assert (workdir_of(tmp_path) / "manifest.json").is_file()
    assert client.registrations == [(DATASET, state["manifest_sha256"])]


def test_a_rerun_at_phase_exported_calls_export_zero_times(tmp_path: Path) -> None:
    data = parquet_bytes(3)
    seed_workdir(tmp_path, "exported", data)
    publisher, lake, staging, client = build(tmp_path)

    report = publisher.publish(DATASET)
    assert report["resumed_phase"] == "exported"
    assert lake.export_calls == []
    assert staging.download_calls == []
    assert report["registered"] is True
    # It still built and verified a manifest off the files already on disk.
    manifest = manifest_of(client)
    dp.validate_manifest(manifest)
    assert manifest["files"][0]["object_id"] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("force", [False, True])
def test_a_rerun_at_phase_manifest_built_reuses_the_exact_manifest(
    tmp_path: Path, force: bool
) -> None:
    """Including WITH force: an unregistered manifest's id is the next POST's id.

    A rebuilt manifest would carry a new ``exported_at``, so a new sha256, so
    a second name for identical content -- and the registry could not tell
    the retry from a new export.
    """
    first_client = FakeKBDL(register_error=TimeoutError("registration timed out"))
    publisher, _, _, _ = build(tmp_path, kbdl=first_client)
    with pytest.raises(TimeoutError):
        publisher.publish(DATASET)
    manifest_sha256 = state_of(tmp_path)["manifest_sha256"]
    manifest_bytes = (workdir_of(tmp_path) / "manifest.json").read_bytes()

    second_client = FakeKBDL()
    resumed, lake, staging, _ = build(tmp_path, kbdl=second_client)
    report = resumed.publish(DATASET, force=force)

    assert report["resumed_phase"] == "manifest_built"
    assert {step["step"] for step in report["steps"]} >= {"manifest_reused", "uploaded"}
    assert "manifest_built" not in {step["step"] for step in report["steps"]}
    assert lake.export_calls == []
    assert staging.download_calls == []
    assert report["manifest_object_id"] == manifest_sha256
    assert second_client.registrations == [(DATASET, manifest_sha256)]
    assert second_client.manifest_upload()["bytes"] == manifest_bytes
    assert second_client.manifest_upload()["sha256"] == manifest_sha256


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"state_text": "{not json at all"}, "unparseable"),
        ({"schema": 2}, "a schema this publisher does not know"),
        ({"target": S_MID}, "a different target"),
    ],
)
def test_an_unusable_state_json_restarts_from_a_clean_workdir(
    tmp_path: Path, kwargs: Dict[str, Any], why: str
) -> None:
    target = kwargs.pop("target", S_HEAD)
    seed_workdir(
        tmp_path, "exported", parquet_bytes(3), target=target, junk=True, **kwargs
    )
    publisher, lake, _, client = build(tmp_path)

    report = publisher.publish(DATASET, keep_workdir=True)
    assert report["resumed_phase"] is None, why
    assert len(lake.export_calls) == 1
    assert report["registered"] is True
    # The workdir was DELETED before the re-export: nothing from the dead run
    # survives to be mistaken for this one's output.
    assert not (workdir_of(tmp_path) / "junk.txt").exists()


def test_a_state_json_whose_files_do_not_verify_restarts_clean(tmp_path: Path) -> None:
    """The recorded sha256 is the whole point of the record."""
    workdir = seed_workdir(tmp_path, "exported", parquet_bytes(3))
    (workdir / "part-00000.parquet").write_bytes(parquet_bytes(2))
    publisher, lake, _, _ = build(tmp_path)
    report = publisher.publish(DATASET)
    assert report["resumed_phase"] is None
    assert len(lake.export_calls) == 1
    assert report["counts"]["pyarrow_count"] == 3


def test_a_manifest_built_state_with_a_missing_manifest_restarts_clean(
    tmp_path: Path,
) -> None:
    data = parquet_bytes(3)
    workdir = seed_workdir(tmp_path, "manifest_built", data)
    state = json.loads((workdir / "state.json").read_text())
    state["manifest_sha256"] = "a" * 64
    (workdir / "state.json").write_text(json.dumps(state))
    publisher, lake, _, _ = build(tmp_path)
    report = publisher.publish(DATASET)
    assert report["resumed_phase"] is None
    assert len(lake.export_calls) == 1


def test_a_registered_state_is_reported_and_republished_only_under_force(
    tmp_path: Path,
) -> None:
    workdir = seed_workdir(tmp_path, "exported", parquet_bytes(3))
    state = json.loads((workdir / "state.json").read_text())
    state.update(phase="registered", version=7, manifest_sha256="b" * 64)
    (workdir / "state.json").write_text(json.dumps(state))

    publisher, lake, _, client = build(tmp_path)
    report = publisher.publish(DATASET)
    assert report["skipped"] is True
    assert report["version"] == 7
    assert lake.export_calls == []
    assert client.uploads == []

    forced = publisher.publish(DATASET, force=True, keep_workdir=True)
    assert forced["resumed_phase"] is None
    assert len(lake.export_calls) == 1
    assert forced["version"] == 3


# ── Registration: a tombstone is never a success ─────────────────────────


def test_a_409_carrying_a_tombstone_is_reported_as_an_error(tmp_path: Path) -> None:
    client = FakeKBDL(
        register_error=KBDLConflictError(
            "that manifest belongs to a retired version (tombstone 2026-11-01)"
        )
    )
    publisher, _, _, _ = build(tmp_path, kbdl=client)
    with pytest.raises(dp.RegistrationError) as caught:
        publisher.publish(DATASET)
    assert "RETIRED" in str(caught.value)
    # The workdir is kept, because nothing was registered.
    assert state_of(tmp_path)["phase"] == "manifest_built"


def test_a_409_without_a_tombstone_propagates_unchanged(tmp_path: Path) -> None:
    client = FakeKBDL(register_error=KBDLConflictError("version already exists"))
    publisher, _, _, _ = build(tmp_path, kbdl=client)
    with pytest.raises(KBDLConflictError):
        publisher.publish(DATASET)


def test_a_200_body_carrying_a_tombstone_is_reported_as_an_error(
    tmp_path: Path,
) -> None:
    client = FakeKBDL(register=({"version": 4, "tombstone": "retired 2026-11-02"}))
    publisher, _, _, _ = build(tmp_path, kbdl=client)
    with pytest.raises(dp.RegistrationError, match="TOMBSTONE"):
        publisher.publish(DATASET)


# ── The shipped catalog and the two README files ─────────────────────────

APPROVAL_FIELDS = ("readme_approved_by", "readme_approved_on")
SHIPPED_ENTRY_KEYS = {
    "tenant",
    "namespace",
    "table",
    "target_file_bytes",
    "readme_file",
    *APPROVAL_FIELDS,
}


def test_the_shipped_catalog_ships_both_v1_datasets(tmp_path: Path) -> None:
    catalog = dp.load_catalog()
    assert sorted(catalog) == [
        "genome_clearhouse.genome_quality",
        "genome_clearhouse.skani_distances",
    ]
    for name, entry in catalog.items():
        assert entry.tenant == "kbaseincubator"
        assert entry.namespace == "genome_clearhouse"
        assert entry.name == f"{entry.namespace}.{entry.table}"
        assert entry.fqn == f"kbaseincubator.{name}"
        assert entry.target_file_bytes == 268435456
        assert entry.readme_file == f"{entry.table}.md"


def test_the_shipped_catalog_ships_both_approval_fields_empty(tmp_path: Path) -> None:
    """And carries NO other approval-shaped field: the gate is these two."""
    raw = dp._tomllib().loads(dp.CATALOG_PATH.read_text(encoding="utf-8"))
    assert sorted(raw) == ["datasets"]
    for name, body in raw["datasets"].items():
        assert set(body) == SHIPPED_ENTRY_KEYS, name
        for field in APPROVAL_FIELDS:
            assert body[field] == "", f"{name}.{field} must ship EMPTY"
        extra = [
            key
            for key in body
            if key not in APPROVAL_FIELDS
            and any(
                word in key
                for word in ("approv", "release", "steward", "authoris", "authoriz")
            )
        ]
        assert extra == [], f"{name} carries an unexpected authorisation field: {extra}"
    for entry in dp.load_catalog().values():
        assert entry.readme_approved is False


EXPECTED_GENOME_QUALITY_README = """DRAFT -- not approved for publication

# genome_clearhouse.genome_quality

One row per genome in the KBase genome clearinghouse, with its genome quality metrics.
This file set is a full snapshot of the lakehouse table `kbaseincubator.genome_clearhouse.genome_quality`,
pinned to the Iceberg snapshot id recorded in `MANIFEST.json` (`source.snapshot_id`).

## Columns to read first

- `released` (boolean). `true` means the genome is part of the released genome inventory.
  1,634,825 SPIRE metagenome bins that SPIRE itself did not release are kept in the table with `released = false`;
  they were flagged, not deleted, so nothing is lost. Count genomes with `released = true`.
- `gtdb_release` (string). For a genome drawn from GTDB, the GTDB release it came from (`r220` or `r232`).
  It is not set for genomes from other sources.

## Provenance and verification

The manifest lists every file with its SHA-256 (equal to its KBDL object id), size and row count,
and records the row count checked three ways at export time.
"""

EXPECTED_SKANI_DISTANCES_README = """DRAFT -- not approved for publication

# genome_clearhouse.skani_distances

skani average-nucleotide-identity results for the genomes in `genome_clearhouse.genome_quality`.
This file set is a full snapshot of the lakehouse table `kbaseincubator.genome_clearhouse.skani_distances`,
pinned to the Iceberg snapshot id recorded in `MANIFEST.json` (`source.snapshot_id`).

## This is NOT a pairwise distance table

There is one row per genome per reference database (`db_name`).
Each row carries `n_hits`, `top_hit` and `top_ani`, and a `distances` string column that holds the serialised list of hits.
To get pairs, parse `distances`.

## Relationship to genome_quality

Join on the genome identifier. SPIRE bins with `released = false` in `genome_quality` may have no row here.

## Provenance and verification

The manifest lists every file with its SHA-256 (equal to its KBDL object id), size and row count,
and records the row count checked three ways at export time.
"""


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("genome_quality.md", EXPECTED_GENOME_QUALITY_README),
        ("skani_distances.md", EXPECTED_SKANI_DISTANCES_README),
    ],
)
def test_the_shipped_readmes_are_byte_for_byte_as_specified(
    filename: str, expected: str
) -> None:
    """Byte for byte, nothing added. The manifest embeds this exact text.

    These files are what an external partner reads, so a drifted word is a
    drifted published document; the comparison is on BYTES rather than on a
    normalised string so trailing whitespace and line endings are covered too.
    """
    path = dp.CATALOG_DIR / filename
    assert path.read_bytes() == expected.encode("utf-8")
    assert expected.startswith("DRAFT -- not approved for publication\n")


def test_every_shipped_readme_is_readable_through_the_catalog() -> None:
    for entry in dp.load_catalog().values():
        text = dp.read_readme(entry)
        assert text.startswith("DRAFT -- not approved for publication")
        assert len(text) <= dp.MAX_README_CHARS


def test_a_readme_file_outside_the_catalog_directory_is_refused(tmp_path: Path) -> None:
    entry = dp.DatasetEntry(
        name=DATASET,
        tenant="kbaseincubator",
        namespace="genome_clearhouse",
        table="genome_quality",
        target_file_bytes=dp.DEFAULT_TARGET_FILE_BYTES,
        readme_file="../../../etc/hostname",
        readme_approved_by="x",
        readme_approved_on="y",
    )
    with pytest.raises(dp.DatasetCatalogError, match="outside the catalog"):
        dp.read_readme(entry, catalog_dir=tmp_path)


def test_an_unknown_dataset_name_is_refused() -> None:
    with pytest.raises(dp.DatasetUnknownError):
        dp.get_dataset("genome_clearhouse.not_a_table")


def test_a_catalog_name_that_is_not_namespace_dot_table_is_refused(
    tmp_path: Path,
) -> None:
    path = tmp_path / "datasets.toml"
    path.write_text(
        '[datasets."genome_clearhouse.genome_quality"]\n'
        'tenant = "kbaseincubator"\n'
        'namespace = "genome_clearhouse"\n'
        'table = "other_table"\n'
        'readme_file = "x.md"\n'
    )
    with pytest.raises(dp.DatasetCatalogError, match="must equal namespace.table"):
        dp.load_catalog(path)


# ── The manifest: schema 1, as published ─────────────────────────────────


def test_the_published_manifest_passes_the_schema_1_rules(tmp_path: Path) -> None:
    publisher, _, _, client = build(tmp_path)
    publisher.publish(DATASET)
    manifest = manifest_of(client)

    dp.validate_manifest(manifest)
    assert manifest["manifest_schema"] == 1
    assert manifest["dataset"] == DATASET
    # A STRING, never a JSON number: Iceberg ids exceed 2**53.
    assert isinstance(manifest["source"]["snapshot_id"], str)
    assert manifest["source"]["snapshot_id"] == S_HEAD
    assert manifest["source"]["snapshot_committed_at"] == COMMITTED
    assert manifest["source"]["snapshot_operation"] == "append"
    assert manifest["format"] == {"file_format": "parquet", "compression": "zstd"}
    assert [entry["name"] for entry in manifest["files"]] == ["part-00000.parquet"]
    assert all(dp.PART_NAME_RE.match(entry["name"]) for entry in manifest["files"])
    assert all(dp.OBJECT_ID_RE.match(entry["object_id"]) for entry in manifest["files"])
    assert manifest["schema"] == [
        {"name": "genome_id", "type": "string", "nullable": True},
        {"name": "released", "type": "bool", "nullable": True},
    ]
    assert manifest["verification"] == {
        "method": dp.VERIFICATION_METHOD,
        "snapshot_total_records": 3,
        "spark_count": 3,
        "pyarrow_count": 3,
    }
    assert manifest["readme"] == README_TEXT
    assert manifest["exported_at"] == "2026-11-21T18:30:00Z"
    assert manifest["exporter"]["tool"] == "kbu dataset publish"
    assert manifest["exporter"]["pyarrow_version"] == pa.__version__
    # NO version, publish time or publisher: those belong to the registry, and
    # leaving them out is what makes the sha256 name exactly what was exported.
    assert not {"version", "published_at", "publisher"} & set(manifest)
    # The uploaded object id IS the sha256 of the uploaded bytes.
    upload = client.manifest_upload()
    assert upload["sha256"] == hashlib.sha256(upload["bytes"]).hexdigest()
    assert upload["bytes"] == dp.canonical_manifest_bytes(manifest)


def good_manifest() -> Dict[str, Any]:
    return {
        "manifest_schema": 1,
        "dataset": DATASET,
        "source": {
            "tenant": "kbaseincubator",
            "namespace": "genome_clearhouse",
            "table": "genome_quality",
            "snapshot_id": S_HEAD,
            "snapshot_committed_at": COMMITTED,
            "snapshot_operation": "append",
        },
        "format": {"file_format": "parquet", "compression": "zstd"},
        "row_count": 3,
        "schema": [{"name": "genome_id", "type": "string", "nullable": True}],
        "files": [
            {
                "object_id": "a" * 64,
                "name": "part-00000.parquet",
                "size_bytes": 1024,
                "row_count": 3,
            }
        ],
        "verification": {
            "method": "three ways",
            "snapshot_total_records": 3,
            "spark_count": 3,
            "pyarrow_count": 3,
        },
        "readme": "hello",
        "exported_at": "2026-11-21T18:30:00Z",
        "exporter": {
            "tool": "kbu dataset publish",
            "kbutillib_version": "0.1.0",
            "pyarrow_version": "25.0.1",
        },
    }


def test_the_reference_manifest_is_valid() -> None:
    dp.validate_manifest(good_manifest())


def test_an_unknown_top_level_key_is_ignored() -> None:
    manifest = good_manifest()
    manifest["future_extension"] = {"anything": [1, 2, None]}
    dp.validate_manifest(manifest)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda m: m.update(manifest_schema=2), id="schema-2"),
        pytest.param(lambda m: m.update(manifest_schema=True), id="schema-boolean"),
        pytest.param(
            lambda m: m["source"].update(snapshot_id=int(S_HEAD)),
            id="snapshot-id-number",
        ),
        pytest.param(
            lambda m: m["source"].update(snapshot_operation="compact"),
            id="unknown-operation",
        ),
        pytest.param(
            lambda m: m["source"].update(snapshot_committed_at="2026-11-20 10:00:00"),
            id="not-a-z-timestamp",
        ),
        pytest.param(lambda m: m["source"].pop("tenant"), id="source-missing-key"),
        pytest.param(lambda m: m["source"].update(extra="x"), id="source-extra-key"),
        pytest.param(
            lambda m: m.update(dataset="genome_clearhouse.other"), id="name-mismatch"
        ),
        pytest.param(lambda m: m.update(row_count=True), id="row-count-boolean"),
        pytest.param(lambda m: m.update(row_count=4), id="row-count-inconsistent"),
        pytest.param(lambda m: m.update(schema=[]), id="empty-schema"),
        pytest.param(lambda m: m.update(files=[]), id="no-files"),
        pytest.param(
            lambda m: m["files"][0].update(name="part-0.parquet"), id="bad-part-name"
        ),
        pytest.param(
            lambda m: m["files"][0].update(object_id="A" * 64), id="uppercase-object-id"
        ),
        pytest.param(
            lambda m: m["files"][0].update(size_bytes=4294967297), id="over-4-gib"
        ),
        pytest.param(lambda m: m["files"][0].update(size_bytes=4), id="under-12-bytes"),
        pytest.param(
            lambda m: m["files"].append(dict(m["files"][0])), id="duplicate-file"
        ),
        pytest.param(
            lambda m: m["verification"].update(spark_count=2),
            id="verification-mismatch",
        ),
        pytest.param(lambda m: m.update(readme=""), id="empty-readme"),
        pytest.param(lambda m: m.update(readme="x" * 65537), id="oversized-readme"),
        pytest.param(lambda m: m["exporter"].update(tool=""), id="empty-tool"),
        pytest.param(
            lambda m: m.update(format={"file_format": "parquet"}), id="short-format"
        ),
        pytest.param(lambda m: m.update(exported_at=None), id="null-value"),
        pytest.param(lambda m: m.pop("verification"), id="missing-required-key"),
    ],
)
def test_the_schema_1_rules_reject(mutate: Any) -> None:
    manifest = good_manifest()
    mutate(manifest)
    with pytest.raises(dp.ManifestSchemaError):
        dp.validate_manifest(manifest)


def test_canonical_serialisation_is_sorted_and_compact() -> None:
    assert dp.canonical_manifest_bytes({"b": 1, "a": "é"}) == b'{"a":"\xc3\xa9","b":1}'


def test_a_non_decimal_current_snapshot_id_is_refused(tmp_path: Path) -> None:
    """Snapshot ids are interpolated into Spark SQL, so they are checked."""
    publisher, lake, _, _ = build(tmp_path, head="snap-1; DROP TABLE x")
    with pytest.raises(dp.DatasetPublishError, match="not a decimal snapshot id"):
        publisher.publish(DATASET)
    assert lake.export_calls == []
