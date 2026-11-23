"""``kbu dataset`` -- the CLI over KBDL's dataset mirror.

TWO KINDS OF DOUBLE, CHOSEN ON PURPOSE.

* THE ``pull`` TESTS DRIVE THE REAL CLIENT. They build an actual
  :class:`~kbutillib.domains.external.kbdl_service_utils.KBDLServiceUtils`
  and replace only its ``requests.Session``
  (:class:`FakeSession` over :class:`DatasetService`), so the real
  ``download_object`` -- its streaming, its resume-by-digest and its
  content-addressing check -- is the code under test, and the parquet it
  serves is REAL zstd parquet written by pyarrow with sha256 object ids
  measured from the actual bytes. A fake client object would have made every
  digest and footer assertion here vacuous.
* THE ``list``/``hold``/``prune`` TESTS use :class:`RecordingClient`, because
  what they assert is which calls the CLI makes with which arguments (that
  ``prune`` without ``--apply`` makes exactly one, that it carries
  ``assume_retired``, that a held version is never retired). The client's own
  HTTP behaviour for those routes is covered in
  ``tests/external/test_kbdl_dataset_client.py``.

Nothing here reaches a network and no KBDL server is required: the dataset
routes are being built in parallel and are not deployed.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("requests")

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402
import requests  # noqa: E402
from click.testing import CliRunner  # noqa: E402

from kbutillib.domains.kbase.berdl import dataset_publish as dp  # noqa: E402
from kbutillib.interfaces.cli import dataset as dataset_cli  # noqa: E402
from kbutillib.interfaces.cli.dataset import dataset_cmd  # noqa: E402

DATASET = "genome_clearhouse.genome_quality"
TEST_BASE_URL = "http://127.0.0.1:8791"
SNAPSHOT = "9007199254740994"


# ---------------------------------------------------------------------------
# Real parquet, and the manifest measured from it
# ---------------------------------------------------------------------------


def write_parquet(path: Path, rows: int) -> bytes:
    """Write a real zstd parquet file of ``rows`` rows and return its bytes."""
    table = pa.table(
        {
            "genome_id": [f"g{i}" for i in range(rows)],
            "completeness": [50.0 + i for i in range(rows)],
        }
    )
    pq.write_table(table, path, compression="zstd")
    return path.read_bytes()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_version(tmp_path: Path, row_counts=(4, 3)) -> dict[str, Any]:
    """A version record, its manifest, and the object bytes behind both.

    The parquet is real and every object id is the measured sha256 of the
    bytes served for it, so a digest assertion in a test below is an
    assertion about actual bytes rather than about a fixture's bookkeeping.
    """
    staging = tmp_path / "_fixture"
    staging.mkdir(exist_ok=True)
    objects: dict[str, bytes] = {}
    files = []
    for index, rows in enumerate(row_counts):
        name = f"part-{index:05d}.parquet"
        data = write_parquet(staging / name, rows)
        object_id = sha256(data)
        objects[object_id] = data
        files.append(
            {
                "object_id": object_id,
                "name": name,
                "size_bytes": len(data),
                "row_count": rows,
            }
        )
    total = sum(row_counts)
    manifest = {
        "manifest_schema": 1,
        "dataset": DATASET,
        "source": {
            "tenant": "kbaseincubator",
            "namespace": "genome_clearhouse",
            "table": "genome_quality",
            "snapshot_id": SNAPSHOT,
            "snapshot_committed_at": "2026-11-20T10:00:00Z",
            "snapshot_operation": "append",
        },
        "format": {"file_format": "parquet", "compression": "zstd"},
        "row_count": total,
        "schema": [
            {"name": "genome_id", "type": "string", "nullable": True},
            {"name": "completeness", "type": "double", "nullable": True},
        ],
        "files": files,
        "verification": {
            "method": dp.VERIFICATION_METHOD,
            "snapshot_total_records": total,
            "spark_count": total,
            "pyarrow_count": total,
        },
        "readme": "# genome_quality\n\nOne row per genome.\n",
        "exported_at": "2026-11-20T10:05:00Z",
        "exporter": {
            "tool": dp.EXPORTER_TOOL,
            "kbutillib_version": "0.0.0",
            "pyarrow_version": pa.__version__,
        },
    }
    manifest_bytes = dp.canonical_manifest_bytes(manifest)
    manifest_object_id = sha256(manifest_bytes)
    objects[manifest_object_id] = manifest_bytes
    record = {
        "name": DATASET,
        "version": 3,
        "state": "PUBLISHED",
        "manifest_object_id": manifest_object_id,
        "source_snapshot_id": SNAPSHOT,
        "row_count": total,
        "total_bytes": sum(len(objects[f["object_id"]]) for f in files),
        "published_at": "2026-11-20T10:06:00Z",
        "published_by": "chenry",
        "hold_reason": None,
        "retired_at": None,
        "retire_reason": None,
    }
    return {
        "record": record,
        "manifest": manifest,
        "manifest_bytes": manifest_bytes,
        "manifest_object_id": manifest_object_id,
        "objects": objects,
        "files": files,
    }


def test_the_fixture_manifest_is_a_valid_schema_1_manifest(tmp_path):
    """The happy-path fixture is not a hand-waved dict.

    If it did not pass the publisher's own ``validate_manifest`` then the pull
    tests would be verifying a manifest no publish could ever produce.
    """
    dp.validate_manifest(build_version(tmp_path)["manifest"])


def test_the_cli_part_name_pattern_is_the_publishers_pattern():
    """``pull`` spells the part-name pattern locally (to stay import-cheap).

    A drift between the two would let a published part fail its own pull, so
    the two are asserted equal rather than trusted to stay in step.
    """
    assert dataset_cli.PART_NAME_RE.pattern == dp.PART_NAME_RE.pattern


# ---------------------------------------------------------------------------
# The fake HTTP layer (the ONLY fake in the pull tests)
# ---------------------------------------------------------------------------


class JSONResponse:
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code} error", response=self)


class ContentResponse:
    """A streamed ``GET /objects/{id}/content`` response."""

    def __init__(self, data: bytes):
        self.status_code = 200
        self._data = data
        self.text = ""

    @property
    def ok(self) -> bool:
        return True

    def iter_content(self, chunk_size: int = 1):
        for start in range(0, len(self._data), max(1, chunk_size)):
            yield self._data[start : start + max(1, chunk_size)]

    def json(self):  # pragma: no cover -- content is bytes, never JSON
        raise ValueError("not JSON")

    def raise_for_status(self):  # pragma: no cover -- always 200
        return None


class ErrorResponse:
    def __init__(self, status_code: int = 500):
        self.status_code = status_code
        self.text = "boom"

    @property
    def ok(self) -> bool:
        return False

    def json(self):
        raise ValueError("not JSON")

    def raise_for_status(self):
        raise requests.HTTPError(f"{self.status_code} error", response=self)


class DatasetService:
    """A fake KBDL serving the dataset routes and object content.

    ``fail_once`` makes the next content request for an object fail (an
    interrupted download); ``on_content`` runs a callback just before an
    object's bytes are served, which is how the "``--dest`` appeared between
    verification and the rename" race is produced without patching the code
    under test.
    """

    def __init__(self, fixture: dict[str, Any]):
        self.fixture = fixture
        self.objects = dict(fixture["objects"])
        self.record = dict(fixture["record"])
        self.manifest = fixture["manifest"]
        self.gone = False
        self.fail_once: set[str] = set()
        self.on_content: dict[str, Any] = {}
        self.calls: list[tuple[str, str]] = []

    def replace_manifest(self, manifest: dict[str, Any]) -> str:
        """Serve a MUTATED manifest, re-keyed by its own sha256.

        A test that only edited ``self.manifest`` would change nothing: the
        pull reads its file list out of the MANIFEST OBJECT's bytes it
        downloaded and verified, never out of the route's JSON body. So the
        object has to be re-registered under its new digest and the record
        pointed at it -- which is also what a real re-export would do.
        """
        self.objects.pop(self.record["manifest_object_id"], None)
        data = dp.canonical_manifest_bytes(manifest)
        manifest_object_id = sha256(data)
        self.objects[manifest_object_id] = data
        self.record = {**self.record, "manifest_object_id": manifest_object_id}
        self.manifest = manifest
        return manifest_object_id

    def request(self, method, url, **kwargs):
        path = url[len(TEST_BASE_URL) :]
        self.calls.append((method, path))
        if path.startswith("/objects/") and path.endswith("/content"):
            object_id = path[len("/objects/") : -len("/content")]
            hook = self.on_content.pop(object_id, None)
            if hook is not None:
                hook()
            if object_id in self.fail_once:
                self.fail_once.discard(object_id)
                return ErrorResponse(500)
            if object_id not in self.objects:
                return JSONResponse(404, {"detail": "no such object"})
            return ContentResponse(self.objects[object_id])
        if path in (
            f"/datasets/{DATASET}/latest",
            f"/datasets/{DATASET}/versions/{self.record['version']}",
        ):
            body = {"version": self.record, "manifest": self.manifest}
            if self.gone:
                body = {**body, "tombstone": {"retire_reason": "bad export"}}
                return JSONResponse(410, body)
            return JSONResponse(200, body)
        if path.startswith(f"/datasets/{DATASET}/versions/"):
            return JSONResponse(404, {"detail": "no such version"})
        raise AssertionError(f"DatasetService has no route for {method} {path}")


class FakeSession:
    def __init__(self, service: DatasetService):
        self.service = service

    def request(self, method, url, **kwargs):
        return self.service.request(method, url, **kwargs)


def make_real_client(service: DatasetService):
    """A REAL ``KBDLServiceUtils`` whose only fake is the HTTP session."""
    from kbutillib.domains.external.kbdl_service_utils import KBDLServiceUtils

    return KBDLServiceUtils(
        base_url=TEST_BASE_URL,
        session=FakeSession(service),
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        token="tok-dataset",
    )


@pytest.fixture()
def off_pod(monkeypatch):
    """Pin the locus so the envelope is deterministic wherever this runs."""
    monkeypatch.setattr(dataset_cli, "_locus", lambda: "off_pod")


@pytest.fixture()
def service(tmp_path, monkeypatch, off_pod):
    """A fake KBDL, with the CLI's client factory pointed at a real client."""
    svc = DatasetService(build_version(tmp_path))
    monkeypatch.setattr(dataset_cli, "_client", lambda: make_real_client(svc))
    return svc


def _runner() -> CliRunner:
    """A runner that keeps stdout and stderr APART, on any supported click.

    The stdout discipline is the thing most of these tests assert -- exactly
    one JSON object on stdout and every warning on stderr -- so a runner that
    merged the two would make those assertions meaningless. click < 8.2 needs
    ``mix_stderr=False``; click >= 8.2 removed the argument and separates them
    always.
    """
    try:
        return CliRunner(mix_stderr=False)
    except TypeError:  # pragma: no cover -- click >= 8.2
        return CliRunner()


def run(*args):
    return _runner().invoke(dataset_cmd, list(args))


def one_json_object(output: str) -> dict[str, Any]:
    """Parse stdout as EXACTLY ONE JSON object, and prove it is only one.

    ``json.loads`` on trailing garbage would raise, but a SECOND object
    printed after the first would not be caught by a naive
    ``loads(output.splitlines()[0])`` -- so the whole of stdout is parsed and
    the line count asserted.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one line of stdout, got {lines!r}"
    parsed = json.loads(output)
    assert isinstance(parsed, dict)
    assert set(parsed) == {
        "schema_version",
        "verb",
        "generated_at",
        "locus",
        "data",
        "warnings",
    }
    assert parsed["schema_version"] == 1
    return parsed


# ---------------------------------------------------------------------------
# pull -- the happy path
# ---------------------------------------------------------------------------


def test_pull_installs_the_verified_version_at_dest(tmp_path, service):
    dest = tmp_path / "out"
    result = run("pull", DATASET, "--dest", str(dest), "--json")
    assert result.exit_code == 0, result.stderr

    envelope = one_json_object(result.stdout)
    assert envelope["verb"] == "pull"
    data = envelope["data"]
    assert data["dataset"] == DATASET
    assert data["version"] == 3
    assert data["row_count"] == 7
    assert [f["name"] for f in data["files"]] == [
        "part-00000.parquet",
        "part-00001.parquet",
    ]
    assert all(f["resumed"] is False for f in data["files"])

    # The install happened, and the working directory is gone (it WAS the
    # installed directory -- one rename, not a copy).
    assert dest.is_dir()
    assert not os.path.lexists(str(dest) + dataset_cli.PARTIAL_SUFFIX)
    assert sorted(p.name for p in dest.iterdir()) == [
        "MANIFEST.json",
        "README.md",
        "part-00000.parquet",
        "part-00001.parquet",
    ]
    # pull.json never survives into the installed directory.
    assert not (dest / dataset_cli.PULL_STATE_FILENAME).exists()


def test_pull_writes_the_manifest_objects_exact_bytes(tmp_path, service):
    """MANIFEST.json must be the OBJECT's bytes, never a re-serialisation.

    The partner verifies their copy the same way this client does --
    ``sha256(MANIFEST.json) == manifest_object_id`` -- and any re-encoding
    (key order, separators, unicode escaping) breaks that while leaving a
    file that parses to the same dict. The digest is the only check that
    catches it.
    """
    dest = tmp_path / "out"
    assert run("pull", DATASET, "--dest", str(dest), "--json").exit_code == 0
    written = (dest / "MANIFEST.json").read_bytes()
    assert written == service.fixture["manifest_bytes"]
    assert sha256(written) == service.record["manifest_object_id"]


def test_pull_writes_the_readme_from_the_manifest(tmp_path, service):
    dest = tmp_path / "out"
    assert run("pull", DATASET, "--dest", str(dest), "--json").exit_code == 0
    assert (dest / "README.md").read_text() == service.manifest["readme"]


def test_pull_installed_parquet_is_readable_with_the_declared_rows(
    tmp_path, service
):
    dest = tmp_path / "out"
    assert run("pull", DATASET, "--dest", str(dest), "--json").exit_code == 0
    rows = [
        pq.ParquetFile(str(dest / f["name"])).metadata.num_rows
        for f in service.manifest["files"]
    ]
    assert rows == [4, 3]


def test_pull_a_numbered_version_uses_the_versions_route(tmp_path, service):
    dest = tmp_path / "out"
    result = run("pull", DATASET, "--version", "3", "--dest", str(dest), "--json")
    assert result.exit_code == 0, result.stderr
    assert (
        "GET",
        f"/datasets/{DATASET}/versions/3",
    ) in service.calls


# ---------------------------------------------------------------------------
# pull -- every refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["empty_dir", "file", "symlink", "broken_symlink"])
def test_pull_refuses_any_existing_dest(tmp_path, service, kind):
    """An EMPTY directory is still an existing path, and so is a symlink.

    ``os.path.lexists`` is the check precisely so a broken symlink -- the one
    thing ``Path.exists()`` reports absent -- is refused too: renaming onto
    it would follow it somewhere nobody asked for.
    """
    dest = tmp_path / "out"
    if kind == "empty_dir":
        dest.mkdir()
    elif kind == "file":
        dest.write_text("")
    elif kind == "symlink":
        other = tmp_path / "other"
        other.mkdir()
        dest.symlink_to(other)
    else:
        dest.symlink_to(tmp_path / "nowhere")

    result = run("pull", DATASET, "--dest", str(dest), "--json")
    assert result.exit_code != 0
    assert "already exists" in result.stderr
    assert result.stdout.strip() == ""
    # Nothing was downloaded and no working directory was made.
    assert not os.path.lexists(str(dest) + dataset_cli.PARTIAL_SUFFIX)
    assert service.calls == []


def test_pull_refuses_a_retired_version_and_prints_the_tombstone(
    tmp_path, service
):
    service.gone = True
    dest = tmp_path / "out"
    result = run("pull", DATASET, "--dest", str(dest), "--json")
    assert result.exit_code != 0
    assert "RETIRED" in result.stderr
    assert "bad export" in result.stderr
    assert not os.path.lexists(dest)
    assert not os.path.lexists(str(dest) + dataset_cli.PARTIAL_SUFFIX)


def test_pull_fails_the_digest_check_on_one_corrupted_byte(tmp_path, service):
    """One flipped byte in a part file, and the pull refuses.

    The manifest still names the correct object id, so this is the real
    ``download_object`` content-addressing check firing on real bytes.
    """
    object_id = service.manifest["files"][0]["object_id"]
    data = bytearray(service.objects[object_id])
    data[len(data) // 2] ^= 0x01
    service.objects[object_id] = bytes(data)

    dest = tmp_path / "out"
    result = run("pull", DATASET, "--dest", str(dest), "--json")
    assert result.exit_code != 0
    assert object_id in result.stderr
    assert not os.path.lexists(dest)
    workdir = Path(str(dest) + dataset_cli.PARTIAL_SUFFIX)
    assert workdir.is_dir()
    assert (workdir / dataset_cli.PULL_STATE_FILENAME).is_file()
    # The failed file was NOT left behind as a poisoned resume candidate.
    assert not (workdir / "part-00000.parquet").exists()


def test_pull_fails_the_footer_check_on_a_row_count_mismatch(tmp_path, service):
    """Intact bytes that are the WRONG export: digest passes, footer does not.

    The manifest declares 99 rows for a file whose parquet footer says 4. The
    bytes hash to their object id, so nothing upstream of this catches it --
    only the footer read does.
    """
    mutated = json.loads(json.dumps(service.manifest))
    mutated["files"][0]["row_count"] = 99
    mutated["row_count"] = 102
    service.replace_manifest(mutated)

    dest = tmp_path / "out"
    result = run("pull", DATASET, "--dest", str(dest), "--json")
    assert result.exit_code != 0
    assert "parquet footer" in result.stderr
    assert "99" in result.stderr
    assert not os.path.lexists(dest)


def test_pull_refuses_manifest_bytes_that_do_not_hash_to_the_id(
    tmp_path, service
):
    """The manifest object is content-addressed like any other object."""
    manifest_id = service.record["manifest_object_id"]
    service.objects[manifest_id] = b'{"files": [], "readme": "x"}'

    dest = tmp_path / "out"
    result = run("pull", DATASET, "--dest", str(dest), "--json")
    assert result.exit_code != 0
    assert manifest_id in result.stderr
    assert not os.path.lexists(dest)
    assert not (
        Path(str(dest) + dataset_cli.PARTIAL_SUFFIX) / "MANIFEST.json"
    ).exists()


def test_pull_refuses_a_part_name_the_manifest_should_not_be_able_to_choose(
    tmp_path, service
):
    """A manifest does not get to name the file it is written to."""
    mutated = json.loads(json.dumps(service.manifest))
    mutated["files"][0]["name"] = "../../escape.parquet"
    service.replace_manifest(mutated)

    dest = tmp_path / "out"
    result = run("pull", DATASET, "--dest", str(dest), "--json")
    assert result.exit_code != 0
    assert "published part name" in result.stderr
    assert not (tmp_path.parent / "escape.parquet").exists()


def test_pull_refuses_a_leftover_workdir_for_a_different_manifest(
    tmp_path, service
):
    """A W from another version is refused BY NAME and never adopted."""
    dest = tmp_path / "out"
    workdir = Path(str(dest) + dataset_cli.PARTIAL_SUFFIX)
    workdir.mkdir()
    (workdir / dataset_cli.PULL_STATE_FILENAME).write_text(
        json.dumps({"manifest_object_id": "f" * 64})
    )
    (workdir / "part-00000.parquet").write_bytes(b"someone else's bytes")

    result = run("pull", DATASET, "--dest", str(dest), "--json")
    assert result.exit_code != 0
    assert str(workdir) in result.stderr
    assert "not a resumable pull" in result.stderr
    # Refused, not cleaned up: it is not this tool's directory to delete.
    assert (workdir / "part-00000.parquet").read_bytes() == b"someone else's bytes"
    assert not os.path.lexists(dest)


def test_pull_refuses_a_leftover_workdir_with_no_pull_json(tmp_path, service):
    dest = tmp_path / "out"
    workdir = Path(str(dest) + dataset_cli.PARTIAL_SUFFIX)
    workdir.mkdir()
    result = run("pull", DATASET, "--dest", str(dest), "--json")
    assert result.exit_code != 0
    assert "not a resumable pull" in result.stderr


def test_pull_refuses_when_dest_appears_between_verification_and_the_rename(
    tmp_path, service
):
    """The re-check exists for exactly this race, and W survives it.

    ``--dest`` is created by the service's own callback while the LAST file
    is being served, i.e. after the first-pass existence check and before the
    install. The rename must not happen, nothing at ``--dest`` may be
    disturbed, and everything verified must still be in W.
    """
    dest = tmp_path / "out"
    last_object = service.manifest["files"][-1]["object_id"]

    def appear():
        dest.mkdir()
        (dest / "someone-elses-work.txt").write_text("do not clobber me")

    service.on_content[last_object] = appear

    result = run("pull", DATASET, "--dest", str(dest), "--json")
    assert result.exit_code != 0
    assert "appeared while this pull was verifying" in result.stderr

    # The pre-existing path is untouched.
    assert (dest / "someone-elses-work.txt").read_text() == "do not clobber me"
    # W is intact, with every verified file in it.
    workdir = Path(str(dest) + dataset_cli.PARTIAL_SUFFIX)
    assert sorted(p.name for p in workdir.iterdir()) == [
        "MANIFEST.json",
        "README.md",
        "part-00000.parquet",
        "part-00001.parquet",
    ]


# ---------------------------------------------------------------------------
# pull -- interruption and resume
# ---------------------------------------------------------------------------


def test_an_interrupted_pull_leaves_nothing_at_dest_and_resumes_on_rerun(
    tmp_path, service
):
    """Fail on the SECOND file, then re-run.

    Two things must hold. Nothing may exist at ``--dest`` after the failure:
    a partner reading that path must never see a half-download. And the
    re-run must RESUME -- the first file and the manifest are already there
    and hash correctly, so they are not fetched again, which is the whole
    point of a working directory that survives.
    """
    dest = tmp_path / "out"
    first, second = (f["object_id"] for f in service.manifest["files"])
    service.fail_once.add(second)

    first_result = run("pull", DATASET, "--dest", str(dest), "--json")
    assert first_result.exit_code != 0, first_result.stdout
    assert "re-running this command resumes it" in first_result.stderr
    assert not os.path.lexists(dest)

    workdir = Path(str(dest) + dataset_cli.PARTIAL_SUFFIX)
    assert (workdir / "MANIFEST.json").is_file()
    assert (workdir / "part-00000.parquet").is_file()
    assert not (workdir / "part-00001.parquet").exists()
    assert (workdir / dataset_cli.PULL_STATE_FILENAME).is_file()

    downloads_before = [
        path for method, path in service.calls if path.endswith("/content")
    ]
    assert f"/objects/{first}/content" in downloads_before

    service.calls.clear()
    second_result = run("pull", DATASET, "--dest", str(dest), "--json")
    assert second_result.exit_code == 0, second_result.stderr

    data = one_json_object(second_result.stdout)["data"]
    assert data["workdir_resumed"] is True
    assert data["manifest_resumed"] is True
    assert [f["resumed"] for f in data["files"]] == [True, False]
    assert data["row_count"] == 7

    # The resumed objects were NOT re-downloaded.
    redownloads = [path for method, path in service.calls if path.endswith("/content")]
    assert redownloads == [f"/objects/{second}/content"]
    assert dest.is_dir()
    assert not os.path.lexists(workdir)


def test_a_resume_re_downloads_a_file_that_no_longer_hashes_correctly(
    tmp_path, service
):
    """The resume is the DIGEST, not the file's presence.

    A truncated file left by a killed run is not evidence of anything, so it
    is re-downloaded rather than trusted.
    """
    dest = tmp_path / "out"
    first = service.manifest["files"][0]["object_id"]
    workdir = Path(str(dest) + dataset_cli.PARTIAL_SUFFIX)
    workdir.mkdir()
    (workdir / dataset_cli.PULL_STATE_FILENAME).write_text(
        json.dumps({"manifest_object_id": service.record["manifest_object_id"]})
    )
    (workdir / "part-00000.parquet").write_bytes(
        service.objects[first][: len(service.objects[first]) // 2]
    )

    result = run("pull", DATASET, "--dest", str(dest), "--json")
    assert result.exit_code == 0, result.stderr
    data = one_json_object(result.stdout)["data"]
    assert data["files"][0]["resumed"] is False
    assert (dest / "part-00000.parquet").read_bytes() == service.objects[first]


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class RecordingClient:
    """Records every dataset call made on it and returns canned answers."""

    def __init__(self, *, datasets=None, versions=None, reclaim=None):
        self._datasets = datasets or []
        self._versions = versions or []
        self._reclaim = reclaim or {
            "deleted": [],
            "kept_foreign": [],
            "kept_busy": [],
            "bytes_freed": 0,
        }
        self.calls: list[tuple[str, tuple, dict]] = []
        self.retire_errors: dict[str, Exception] = {}

    def list_datasets(self):
        self.calls.append(("list_datasets", (), {}))
        return self._datasets

    def list_dataset_versions(self, name):
        self.calls.append(("list_dataset_versions", (name,), {}))
        return self._versions

    def retire_dataset_version(self, name, version, reason):
        self.calls.append(
            ("retire_dataset_version", (name, version), {"reason": reason})
        )
        if version in self.retire_errors:
            raise self.retire_errors[version]
        return {"name": name, "version": version, "state": "RETIRED"}

    def hold_dataset_version(self, name, version, reason):
        self.calls.append(
            ("hold_dataset_version", (name, version), {"reason": reason})
        )
        return {"name": name, "version": version, "hold_reason": reason}

    def reclaim_dataset(self, name, dry_run=True, assume_retired=None):
        self.calls.append(
            (
                "reclaim_dataset",
                (name,),
                {"dry_run": dry_run, "assume_retired": assume_retired},
            )
        )
        return self._reclaim

    def named(self, name: str) -> list[tuple[str, tuple, dict]]:
        return [call for call in self.calls if call[0] == name]


@pytest.fixture()
def recorder(monkeypatch, off_pod):
    def install(**kwargs):
        client = RecordingClient(**kwargs)
        monkeypatch.setattr(dataset_cli, "_client", lambda: client)
        return client

    return install


def test_list_emits_exactly_one_envelope(recorder):
    recorder(
        datasets=[
            {
                "name": DATASET,
                "source": "kbaseincubator.genome_clearhouse.genome_quality",
                "latest_version": 3,
                "latest_published_at": "2026-11-20T10:00:00Z",
                "version_count": 3,
            }
        ]
    )
    result = run("list", "--json")
    assert result.exit_code == 0, result.stderr
    envelope = one_json_object(result.stdout)
    assert envelope["verb"] == "list"
    assert envelope["data"]["datasets"][0]["name"] == DATASET
    assert envelope["warnings"] == []


def test_list_warns_about_a_dataset_with_no_registered_version(recorder):
    recorder(datasets=[{"name": DATASET, "latest_version": None}])
    result = run("list", "--json")
    envelope = one_json_object(result.stdout)
    assert any("has ever been registered" in w for w in envelope["warnings"])
    assert "warning:" in result.stderr


def test_list_human_output_renders_the_same_dict(monkeypatch, recorder):
    """Output-contract rule 1: one built structure, two presentations."""
    recorder(datasets=[{"name": DATASET, "latest_version": 3}])
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(dataset_cli, "_render", seen.append)
    assert run("list").exit_code == 0
    assert len(seen) == 1
    assert seen[0]["verb"] == "list"
    assert seen[0]["data"]["datasets"] == [{"name": DATASET, "latest_version": 3}]


# ---------------------------------------------------------------------------
# hold
# ---------------------------------------------------------------------------


def test_hold_with_a_reason_places_the_hold(recorder):
    client = recorder()
    result = run("hold", DATASET, "2", "--reason", "partner audit", "--json")
    assert result.exit_code == 0, result.stderr
    envelope = one_json_object(result.stdout)
    assert envelope["verb"] == "hold"
    assert envelope["data"]["action"] == "hold"
    assert client.named("hold_dataset_version") == [
        ("hold_dataset_version", (DATASET, "2"), {"reason": "partner audit"})
    ]


def test_hold_release_sends_reason_none(recorder):
    client = recorder()
    result = run("hold", DATASET, "2", "--release", "--json")
    assert result.exit_code == 0, result.stderr
    assert one_json_object(result.stdout)["data"]["action"] == "release"
    assert client.named("hold_dataset_version")[0][2] == {"reason": None}


def test_hold_with_both_reason_and_release_is_a_usage_error(recorder):
    client = recorder()
    result = run("hold", DATASET, "2", "--reason", "x", "--release")
    assert result.exit_code == 2
    assert "exactly one of --reason" in result.stderr
    assert client.calls == []


def test_hold_with_neither_reason_nor_release_is_a_usage_error(recorder):
    client = recorder()
    result = run("hold", DATASET, "2")
    assert result.exit_code == 2
    assert "exactly one of --reason" in result.stderr
    assert client.calls == []


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


def versions_fixture():
    """Five versions, newest first, as the service returns them.

    Version 2 is HELD and version 1 is already RETIRED, so a ``--keep 2``
    plan is "retire 3, keep 4 and 5, never touch 2, ignore 1".
    """
    def row(version, state="PUBLISHED", hold_reason=None):
        return {
            "name": DATASET,
            "version": version,
            "state": state,
            "manifest_object_id": f"{version:064d}",
            "source_snapshot_id": SNAPSHOT,
            "row_count": 7,
            "total_bytes": 4096,
            "published_at": f"2026-11-{10 + version:02d}T10:00:00Z",
            "published_by": "chenry",
            "hold_reason": hold_reason,
            "retired_at": None,
            "retire_reason": None,
        }

    return [
        row(5),
        row(4),
        row(3),
        row(2, hold_reason="partner audit in progress"),
        row(1, state="RETIRED"),
    ]


def test_prune_keep_zero_is_a_usage_error(recorder):
    client = recorder(versions=versions_fixture())
    result = run("prune", DATASET, "--keep", "0")
    assert result.exit_code == 2
    assert "--keep must be at least 1" in result.stderr
    assert client.calls == []


def test_prune_keep_negative_is_a_usage_error(recorder):
    client = recorder(versions=versions_fixture())
    assert run("prune", DATASET, "--keep", "-1").exit_code == 2
    assert client.calls == []


def test_prune_without_apply_changes_nothing_and_previews_the_reclaim(recorder):
    """The dry run's ``assume_retired`` IS the plan, exactly.

    A preview that called reclaim without it would report the reclaim of an
    empty retire list -- i.e. nothing -- and tell the operator nothing about
    the bytes ``--apply`` would free.
    """
    client = recorder(
        versions=versions_fixture(),
        reclaim={
            "deleted": ["a" * 64],
            "kept_foreign": ["b" * 64],
            "kept_busy": [{"object_id": "c" * 64, "reason": "download in flight"}],
            "bytes_freed": 2048,
        },
    )
    result = run("prune", DATASET, "--keep", "2", "--json")
    assert result.exit_code == 0, result.stderr

    envelope = one_json_object(result.stdout)
    data = envelope["data"]
    assert data["applied"] is False
    assert data["retire_versions"] == ["3"]
    assert data["held_versions"] == ["2"]
    assert data["published_versions"] == ["2", "3", "4", "5"]
    assert data["reclaim"]["bytes_freed"] == 2048
    assert any("dry run" in w for w in envelope["warnings"])

    assert client.named("retire_dataset_version") == []
    assert client.named("reclaim_dataset") == [
        (
            "reclaim_dataset",
            (DATASET,),
            {"dry_run": True, "assume_retired": ["3"]},
        )
    ]


def test_prune_without_apply_previews_an_empty_plan_as_an_empty_plan(recorder):
    client = recorder(versions=versions_fixture())
    result = run("prune", DATASET, "--keep", "10", "--json")
    assert result.exit_code == 0, result.stderr
    assert one_json_object(result.stdout)["data"]["retire_versions"] == []
    assert client.named("reclaim_dataset")[0][2]["assume_retired"] == []


def test_prune_with_apply_retires_exactly_the_plan_then_reclaims_once(recorder):
    """Retire-then-reclaim, never the reverse, and never a held version."""
    client = recorder(versions=versions_fixture())
    result = run("prune", DATASET, "--keep", "2", "--apply", "--json")
    assert result.exit_code == 0, result.stderr

    data = one_json_object(result.stdout)["data"]
    assert data["applied"] is True
    assert [r["version"] for r in data["retired"]] == ["3"]
    assert data["error"] is None

    retires = client.named("retire_dataset_version")
    assert [call[1][1] for call in retires] == ["3"]
    assert "prune --keep 2" in retires[0][2]["reason"]

    reclaims = client.named("reclaim_dataset")
    assert len(reclaims) == 1
    assert reclaims[0][2] == {"dry_run": False, "assume_retired": None}

    # Order: every retire precedes the reclaim.
    names = [call[0] for call in client.calls]
    assert names.index("reclaim_dataset") > max(
        index for index, name in enumerate(names) if name == "retire_dataset_version"
    )


def test_prune_with_apply_retires_oldest_first_and_keeps_the_newest_n(recorder):
    client = recorder(versions=versions_fixture())
    assert run("prune", DATASET, "--keep", "1", "--apply", "--json").exit_code == 0
    retired = [call[1][1] for call in client.named("retire_dataset_version")]
    assert retired == ["3", "4"]  # 5 is the newest, 2 is held, 1 is retired


def test_prune_with_apply_never_retires_a_held_version(recorder):
    """Even when ``--keep 1`` would otherwise sweep it up."""
    client = recorder(versions=versions_fixture())
    run("prune", DATASET, "--keep", "1", "--apply", "--json")
    assert "2" not in [
        call[1][1] for call in client.named("retire_dataset_version")
    ]


def test_prune_with_apply_stops_at_the_first_retire_error(recorder):
    """A stale plan is not pressed through, and the failure is non-zero."""
    client = recorder(versions=versions_fixture())
    client.retire_errors["3"] = RuntimeError("409 version is held")
    result = run("prune", DATASET, "--keep", "1", "--apply", "--json")
    assert result.exit_code == 1
    data = one_json_object(result.stdout)["data"]
    assert data["retired"] == []
    assert "retiring version 3 failed" in data["error"]
    # Stopped: version 4 was never attempted.
    assert [call[1][1] for call in client.named("retire_dataset_version")] == ["3"]
    # Reclaim still ran, over what did retire (nothing).
    assert len(client.named("reclaim_dataset")) == 1


def test_prune_human_output_renders_the_same_dict(monkeypatch, recorder):
    recorder(versions=versions_fixture())
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(dataset_cli, "_render", seen.append)
    assert run("prune", DATASET, "--keep", "2").exit_code == 0
    assert len(seen) == 1
    assert seen[0]["data"]["retire_versions"] == ["3"]


# ---------------------------------------------------------------------------
# status / publish -- the locus gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["status", "publish"])
def test_status_and_publish_refuse_off_pod_before_any_call(
    monkeypatch, verb, recorder
):
    """The refusal names the locus and nothing is constructed behind it.

    ``_publisher()`` would reach for a Spark session and a MinIO client, so
    the gate has to come first; the test proves it does by making
    ``_publisher`` explode if it is ever called.
    """
    recorder()
    monkeypatch.setattr(dataset_cli, "_locus", lambda: "off_pod")

    def exploding_publisher():  # pragma: no cover -- must never run
        raise AssertionError("the off-pod refusal must precede _publisher()")

    monkeypatch.setattr(dataset_cli, "_publisher", exploding_publisher)
    result = run(verb, DATASET, "--json")
    assert result.exit_code != 0
    assert "IN-POD ONLY" in result.stderr
    assert "'off_pod'" in result.stderr
    assert result.stdout.strip() == ""


@pytest.mark.parametrize("verb", ["status", "publish"])
def test_the_off_pod_refusal_carries_a_BerdlLoadRefusedError(verb):
    """The same error type the clearinghouse load verbs refuse with.

    It is the ClickException's ``__cause__`` rather than what propagates,
    so the CLI exits cleanly without a traceback while a programmatic caller
    can still see what kind of refusal it was.
    """
    import click

    from kbutillib.domains.kbase.berdl.capability import BerdlLoadRefusedError

    with pytest.raises(click.ClickException) as caught:
        dataset_cli._require_in_pod_or_refuse(verb, "off_pod")
    assert isinstance(caught.value.__cause__, BerdlLoadRefusedError)
    assert f"kbu dataset {verb} is IN-POD ONLY" in str(caught.value)


def test_in_pod_lets_the_gate_through():
    assert dataset_cli._require_in_pod_or_refuse("publish", "in_pod") is None


class FakePublisher:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def status(self, name):
        self.calls.append(("status", {"name": name}))
        return {
            "status": "changed",
            "target": SNAPSHOT,
            "operations": [],
            "warnings": ["leftover staging objects listed, not deleted"],
        }

    def publish(self, name, dry_run=False, force=False, keep_workdir=False):
        self.calls.append(
            (
                "publish",
                {
                    "name": name,
                    "dry_run": dry_run,
                    "force": force,
                    "keep_workdir": keep_workdir,
                },
            )
        )
        return {
            "dataset": name,
            "dry_run": dry_run,
            "force": force,
            "status": "changed",
            "skipped": dry_run,
            "would_skip": False,
            "target": SNAPSHOT,
            "workdir": "/tmp/w",
            "steps": [],
            "warnings": [],
        }


@pytest.fixture()
def in_pod_publisher(monkeypatch):
    monkeypatch.setattr(dataset_cli, "_locus", lambda: "in_pod")
    publisher = FakePublisher()
    monkeypatch.setattr(dataset_cli, "_publisher", lambda: publisher)
    return publisher


def test_status_emits_exactly_one_envelope_in_pod(in_pod_publisher):
    result = run("status", DATASET, "--json")
    assert result.exit_code == 0, result.stderr
    envelope = one_json_object(result.stdout)
    assert envelope["verb"] == "status"
    assert envelope["locus"] == "in_pod"
    assert envelope["data"]["status"] == "changed"
    assert envelope["data"]["dataset"] == DATASET
    # The publisher's warnings are lifted into the envelope, not left in data.
    assert envelope["warnings"] == [
        "leftover staging objects listed, not deleted"
    ]
    assert "warnings" not in envelope["data"]


def test_publish_passes_every_flag_through_and_emits_one_envelope(
    in_pod_publisher,
):
    result = run(
        "publish", DATASET, "--dry-run", "--force", "--keep-workdir", "--json"
    )
    assert result.exit_code == 0, result.stderr
    envelope = one_json_object(result.stdout)
    assert envelope["verb"] == "publish"
    assert in_pod_publisher.calls == [
        (
            "publish",
            {
                "name": DATASET,
                "dry_run": True,
                "force": True,
                "keep_workdir": True,
            },
        )
    ]


def test_publish_defaults_are_all_off(in_pod_publisher):
    assert run("publish", DATASET, "--json").exit_code == 0
    assert in_pod_publisher.calls[0][1] == {
        "name": DATASET,
        "dry_run": False,
        "force": False,
        "keep_workdir": False,
    }


# ---------------------------------------------------------------------------
# Registration and the contract across every verb
# ---------------------------------------------------------------------------


def test_kbu_dataset_is_registered_on_the_main_cli():
    from kbutillib.interfaces.cli import main

    assert "dataset" in main.commands
    assert sorted(main.commands["dataset"].commands) == [
        "hold",
        "list",
        "prune",
        "publish",
        "pull",
        "status",
    ]


def test_every_verb_emits_one_object_with_its_own_verb_name(
    tmp_path, monkeypatch, service, in_pod_publisher
):
    """One object per verb, each naming itself, across the whole group.

    ``service`` supplies the real-client HTTP fake for ``pull`` and
    ``in_pod_publisher`` the in-pod half; ``list``/``hold``/``prune`` are
    answered by a recording client installed here.
    """
    client = RecordingClient(datasets=[], versions=versions_fixture())
    real = dataset_cli._client

    def route():
        return client

    monkeypatch.setattr(dataset_cli, "_client", route)

    invocations = {
        "list": ("list", "--json"),
        "status": ("status", DATASET, "--json"),
        "publish": ("publish", DATASET, "--json"),
        "hold": ("hold", DATASET, "2", "--release", "--json"),
        "prune": ("prune", DATASET, "--keep", "2", "--json"),
    }
    for verb, args in invocations.items():
        result = run(*args)
        assert result.exit_code == 0, (verb, result.output)
        assert one_json_object(result.stdout)["verb"] == verb

    monkeypatch.setattr(dataset_cli, "_client", real)
    result = run("pull", DATASET, "--dest", str(tmp_path / "pulled"), "--json")
    assert result.exit_code == 0, result.stderr
    assert one_json_object(result.stdout)["verb"] == "pull"
