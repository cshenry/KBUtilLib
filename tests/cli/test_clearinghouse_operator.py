"""Tests for the ``kbu clearinghouse`` OPERATOR verbs: plan / shard / load / verify.

Pure logic, no network, no live BERDL pod, no Spark. The SHARD-stage verbs
(``plan``, ``shard``) run against a tiny real parquet fixture through the real
manifest parser and sharder -- these need only pyarrow, CPU and disk, exactly as
the stage is designed to. The INGEST-stage verbs (``load``, ``verify``) drive the
click CLI against a hand-built fake ``ClearinghouseCapability`` injected by
patching :func:`kbutillib.interfaces.cli.clearinghouse._capability`, following the
read-verb tests' shape. The fake FAILS THE TEST if any write reaches it during a
dry-run, mirroring the bootstrap dry-run test's guarantee.

Success criteria exercised here:
    (a) ``load`` off-pod refuses BEFORE any transport call, message names the locus.
    (b) ``load --dry-run`` runs the pre-write assertion and writes nothing
        (asserted by a fake that fails if any write reaches it).
    (c) ``plan`` on an invalid manifest exits non-zero with the offending key
        named, and writes nothing.
    (d) all four verbs emit the same versioned JSON envelope under --json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from click.testing import CliRunner

import kbutillib.interfaces.cli.clearinghouse as ch
from kbutillib.cli import main

_ENVELOPE_KEYS = {"schema_version", "verb", "generated_at", "locus", "data", "warnings"}

_FIXTURE_SEQS = [
    "ACGTACGTAC",
    "TTTTGGGGCC",
    "GATTACAGAT",
    "CCCCAAAATT",
    "ACGTTGCAAA",
    "GGGGCCCCTT",
]


def _write_fixture_manifest(tmp_path: Path) -> Path:
    """Write a tiny valid parquet source and a manifest naming it; return the manifest path."""
    src_path = tmp_path / "genes.parquet"
    pq.write_table(pa.table({"dna_sequence": _FIXTURE_SEQS}), str(src_path))
    manifest_path = tmp_path / "manifest.toml"
    manifest_path.write_text(
        f"""
[[source]]
name = "fixture-genes"
adapter = "file"
path = "{src_path}"
format = "parquet"
entity_type = "gene"
kinds = ["entity", "content"]

[source.hash]
raw_column = "dna_sequence"

[source.content]
sequence = "dna_sequence"
seq_length = "@len(dna_sequence)"
""",
        encoding="utf-8",
    )
    return manifest_path


class _FakeIngestCapability:
    """Fake ClearinghouseCapability for the ingest-stage verbs.

    Records every ingest/verify call. If ``fail_on_write`` is set, ANY real
    write (a non-dry-run ingest reaching ``ingest_shards``) fails the test
    outright -- this is how the dry-run test proves nothing was written.

    ``orphans`` is the namespace-wide parameter-set registry report the ``load``
    verb runs at the end of every invocation; its default is a sound registry.
    """

    def __init__(self, locus="in_pod", *, fail_on_write=False, orphans=None):
        self._locus_value = locus
        self._fail_on_write = fail_on_write
        self._orphans = orphans or {"ok": True, "failures": []}
        self.ingest_calls: list[dict] = []
        self.verify_calls: list[dict] = []
        self.orphan_calls = 0
        self.pre_write_asserted = False

    def _locus(self):
        return self._locus_value

    def ingest_shards(
        self,
        shard_dir,
        *,
        run_id=None,
        dry_run=False,
        reconcile=False,
        coverage_floor=None,
        transfer_deadline_seconds=None,
    ):
        # The two DECLARED thresholds are recorded, not just tolerated: the
        # CLI's whole job for them is to hand the operator's declaration down
        # to the capability, so a signature that swallowed them would let the
        # option become a no-op silently.
        self.ingest_calls.append(
            {
                "shard_dir": str(shard_dir),
                "run_id": run_id,
                "dry_run": dry_run,
                "reconcile": reconcile,
                "coverage_floor": coverage_floor,
                "transfer_deadline_seconds": transfer_deadline_seconds,
            }
        )
        # The pre-write assertion runs in dry-run too (it is the operator's last
        # gate); model that it happened.
        self.pre_write_asserted = True
        if not dry_run and self._fail_on_write:
            raise AssertionError(
                "a real write reached ingest_shards() -- the dry-run gate leaked!"
            )
        action = "would_ingest" if dry_run else "ingest"
        execution_id = run_id or "01EXECUTION00000000000000"
        return {
            "run_id": execution_id,
            "execution_id": execution_id,
            "source_run_id": "01SHARDRUN0000000000000000"[:26],
            "namespace": "kbaseincubator.clearinghouse",
            "state_dir": f"/tmp/state/{execution_id}",
            "dry_run": dry_run,
            "lock": {"namespace": "kbaseincubator.clearinghouse", "pid": 1},
            "coverage_floor": 1.0 if coverage_floor is None else coverage_floor,
            "transfer_deadline_seconds": (
                6 * 60 * 60
                if transfer_deadline_seconds is None
                else transfer_deadline_seconds
            ),
            "tables": [
                {
                    "name": "gene_entity",
                    "batch_id": "01BATCH000000000000000000",
                    "unit_id": "u1",
                    "action": action,
                    "rows_in": 6,
                    "rows_dropped_present": 0,
                    "rows_ingested": 6,
                }
            ],
        }

    def parameter_set_orphans(self, **kwargs):
        self.orphan_calls += 1
        return dict(self._orphans)

    def verify_run(self, shard_dir, *, run_id, unit=None):
        self.verify_calls.append(
            {"shard_dir": str(shard_dir), "run_id": run_id, "unit": unit}
        )
        return {
            "run_id": run_id,
            "execution_id": run_id,
            "source_run_id": "01SHARDRUN000000000000000",
            "namespace": "kbaseincubator.clearinghouse",
            "unit": unit,
            "ok": True,
            "batches": [
                {
                    "batch_id": "01BATCH000000000000000000",
                    "name": "gene_entity",
                    "unit_id": "u1",
                    "ledger_rows": 6,
                    "live_rows": 6,
                    "ok": True,
                }
            ],
            "units": [{"unit_id": "u1", "ok": True, "missing_parameter_sets": []}],
            "tables": [
                {
                    "name": "gene_entity",
                    "ledger_rows": 6,
                    "live_rows": 6,
                    "snapshot_ids": ["1001"],
                    "ok": True,
                }
            ],
            "parameter_set_hashes": [],
            "discrepancies": [],
        }


def _invoke(monkeypatch, argv, *, fake=None):
    monkeypatch.setattr(ch, "_capability", lambda: fake)
    return CliRunner().invoke(main, argv, catch_exceptions=False)


def _assert_one_clean_envelope(stdout: str, verb: str, *, locus: str) -> dict:
    stripped = stdout.strip()
    parsed = json.loads(stripped)  # raises if stdout is not one clean object
    decoder = json.JSONDecoder()
    _obj, end = decoder.raw_decode(stripped)
    assert stripped[end:].strip() == "", "trailing content after the JSON object"
    assert set(parsed) == _ENVELOPE_KEYS
    assert parsed["schema_version"] == ch.SCHEMA_VERSION
    assert parsed["verb"] == verb
    assert parsed["locus"] == locus
    assert isinstance(parsed["data"], dict)
    assert isinstance(parsed["warnings"], list)
    assert isinstance(parsed["generated_at"], str) and parsed["generated_at"]
    return parsed


# --------------------------------------------------------------------------
# (d) all four verbs emit the same versioned JSON envelope under --json
# --------------------------------------------------------------------------


def test_plan_emits_one_envelope(monkeypatch, tmp_path):
    manifest = _write_fixture_manifest(tmp_path)
    result = _invoke(monkeypatch, ["clearinghouse", "plan", str(manifest), "--json"])
    assert result.exit_code == 0, result.output
    env = _assert_one_clean_envelope(result.stdout, "plan", locus="anywhere")
    # One TablePlan per (source, kind): entity + content for the one source.
    tables = {t["table"] for t in env["data"]["tables"]}
    assert tables == {"gene_entity", "gene_content"}
    for t in env["data"]["tables"]:
        assert t["row_count"] == len(_FIXTURE_SEQS)  # counted, not guessed
        assert t["estimated_shards"] >= 1
        assert t["target_shard_bytes"] > 0


def test_shard_emits_one_envelope_and_writes_shards(monkeypatch, tmp_path):
    manifest = _write_fixture_manifest(tmp_path)
    out_dir = tmp_path / "bronze"
    result = _invoke(
        monkeypatch,
        [
            "clearinghouse",
            "shard",
            str(manifest),
            "--out",
            str(out_dir),
            "--target-bytes",
            "64",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    env = _assert_one_clean_envelope(result.stdout, "shard", locus="anywhere")
    assert env["data"]["shard_count"] == len(env["data"]["shards"])
    assert env["data"]["shard_count"] >= 1
    # The shards physically exist on disk.
    for shard in env["data"]["shards"]:
        assert Path(shard["path"]).is_file()


def test_load_emits_one_envelope(monkeypatch, tmp_path):
    shard_dir = tmp_path / "bronze"
    shard_dir.mkdir()
    fake = _FakeIngestCapability(locus="in_pod")
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "load", str(shard_dir), "--json"],
        fake=fake,
    )
    assert result.exit_code == 0, result.output
    env = _assert_one_clean_envelope(result.stdout, "load", locus="in_pod")
    assert env["data"]["dry_run"] is False
    assert len(fake.ingest_calls) == 1


def test_load_without_a_run_id_starts_a_new_execution(monkeypatch, tmp_path):
    """Omitting --run-id means a NEW load execution, not a resume of a run id
    derived from the directory name: one shard run can be loaded more than
    once, and each load is proven by its own per-batch counts."""
    shard_dir = tmp_path / "bronze"
    shard_dir.mkdir()
    fake = _FakeIngestCapability(locus="in_pod")
    result = _invoke(
        monkeypatch, ["clearinghouse", "load", str(shard_dir)], fake=fake
    )
    assert result.exit_code == 0, result.output
    assert fake.ingest_calls[0]["run_id"] is None


def test_load_resumes_the_execution_named_by_run_id(monkeypatch, tmp_path):
    shard_dir = tmp_path / "bronze"
    shard_dir.mkdir()
    fake = _FakeIngestCapability(locus="in_pod")
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "load", str(shard_dir), "--run-id", "01EXEC"],
        fake=fake,
    )
    assert result.exit_code == 0, result.output
    assert fake.ingest_calls[0]["run_id"] == "01EXEC"


def test_load_runs_the_namespace_orphan_check_and_exits_nonzero_on_a_finding(
    monkeypatch, tmp_path
):
    """lake-5's namespace-wide registry check runs at the END of every load.

    An orphan another writer left is a real failure of the namespace, so the
    exit code says so -- but it does not un-mark the batches THIS load proved:
    the per-batch results are reported exactly as the ingest returned them.
    """
    shard_dir = tmp_path / "bronze"
    shard_dir.mkdir()
    finding = "gene_result: 2 parameter_set_hash values with no registry row"
    fake = _FakeIngestCapability(
        locus="in_pod",
        orphans={"ok": False, "failures": [finding], "missing_from_registry": ["x"]},
    )
    result = _invoke(
        monkeypatch, ["clearinghouse", "load", str(shard_dir)], fake=fake
    )
    assert fake.orphan_calls == 1
    assert result.exit_code != 0
    # Printed IN FULL, and the load's own result still stands.
    assert finding in result.output
    assert "ingest" in result.output


def test_load_reports_a_lock_taken_over_from_a_dead_holder_as_a_warning(
    tmp_path,
):
    """A dead holder's lock is taken over -- and the operator is TOLD.

    Built directly rather than through the runner because the envelope is the
    contract: a warning belongs in ``warnings`` (where a dashboard reads it)
    as well as on stderr.
    """
    fake = _FakeIngestCapability(locus="in_pod")
    real_ingest = fake.ingest_shards

    def with_takeover(*args, **kwargs):
        result = real_ingest(*args, **kwargs)
        result["lock"] = {
            **result["lock"],
            "took_over_from": {"pid": 999999, "host": "deadhost"},
        }
        return result

    fake.ingest_shards = with_takeover
    envelope = ch._build_load(
        fake,
        shard_dir=str(tmp_path),
        run_id=None,
        dry_run=False,
        reconcile=False,
    )
    assert envelope["data"]["lock"]["took_over_from"]["pid"] == 999999
    assert any("taken over" in warning for warning in envelope["warnings"])


def test_verify_emits_one_envelope(monkeypatch, tmp_path):
    shard_dir = tmp_path / "bronze"
    shard_dir.mkdir()
    fake = _FakeIngestCapability(locus="in_pod")
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "verify", str(shard_dir), "--json"],
        fake=fake,
    )
    assert result.exit_code == 0, result.output
    env = _assert_one_clean_envelope(result.stdout, "verify", locus="in_pod")
    assert len(fake.verify_calls) == 1


# --------------------------------------------------------------------------
# (a) load off-pod refuses BEFORE any transport call, naming the locus
# --------------------------------------------------------------------------


def test_load_offpod_refuses_before_any_transport_call(monkeypatch, tmp_path):
    shard_dir = tmp_path / "bronze"
    shard_dir.mkdir()
    fake = _FakeIngestCapability(locus="off_pod", fail_on_write=True)
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "load", str(shard_dir)],
        fake=fake,
    )
    assert result.exit_code != 0
    # The message names the locus as the reason.
    assert "off_pod" in result.output
    assert "IN-POD ONLY" in result.output
    # ingest_shards was NEVER reached -- refusal is early, no transport call.
    assert fake.ingest_calls == []


def test_verify_offpod_refuses_before_any_transport_call(monkeypatch, tmp_path):
    shard_dir = tmp_path / "bronze"
    shard_dir.mkdir()
    fake = _FakeIngestCapability(locus="off_pod")
    result = _invoke(
        monkeypatch, ["clearinghouse", "verify", str(shard_dir)], fake=fake
    )
    assert result.exit_code != 0
    assert "off_pod" in result.output
    assert fake.verify_calls == []


# --------------------------------------------------------------------------
# (b) load --dry-run runs the pre-write assertion and writes nothing
# --------------------------------------------------------------------------


def test_load_dry_run_writes_nothing(monkeypatch, tmp_path):
    shard_dir = tmp_path / "bronze"
    shard_dir.mkdir()
    # fail_on_write makes ANY real write fail the test; a clean dry-run proves
    # nothing was written.
    fake = _FakeIngestCapability(locus="in_pod", fail_on_write=True)
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "load", str(shard_dir), "--dry-run", "--json"],
        fake=fake,
    )
    assert result.exit_code == 0, result.output
    env = _assert_one_clean_envelope(result.stdout, "load", locus="in_pod")
    assert env["data"]["dry_run"] is True
    # The pre-write assertion (the operator's last gate) ran.
    assert fake.pre_write_asserted is True
    # Exactly one ingest_shards call, and it was a dry run (no write raised).
    assert len(fake.ingest_calls) == 1
    assert fake.ingest_calls[0]["dry_run"] is True
    # The degraded/advisory nature is surfaced in warnings.
    assert env["warnings"], "dry-run must carry a warning that nothing was written"


# --------------------------------------------------------------------------
# (c) plan on an invalid manifest exits non-zero, names the key, writes nothing
# --------------------------------------------------------------------------


def test_plan_invalid_manifest_exits_nonzero_naming_key(monkeypatch, tmp_path):
    # 'sequence' is not a legal genome content column -> ManifestError names it.
    src_path = tmp_path / "g.parquet"
    pq.write_table(pa.table({"dna_sequence": _FIXTURE_SEQS}), str(src_path))
    manifest = tmp_path / "bad.toml"
    manifest.write_text(
        f"""
[[source]]
name = "bad-genome"
adapter = "file"
path = "{src_path}"
format = "parquet"
entity_type = "genome"
kinds = ["content"]

[source.hash]
raw_column = "dna_sequence"

[source.content]
sequence = "dna_sequence"
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "bronze"
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "plan", str(manifest)],
        fake=None,
    )
    assert result.exit_code != 0
    # The offending key is named in the message.
    assert "sequence" in result.output
    # plan writes nothing: no bronze dir is created.
    assert not out_dir.exists()


def test_shard_invalid_manifest_exits_nonzero_and_writes_nothing(monkeypatch, tmp_path):
    src_path = tmp_path / "g.parquet"
    pq.write_table(pa.table({"dna_sequence": _FIXTURE_SEQS}), str(src_path))
    manifest = tmp_path / "bad.toml"
    manifest.write_text(
        f"""
[[source]]
name = "bad-type"
adapter = "file"
path = "{src_path}"
format = "parquet"
entity_type = "plasmid"
kinds = ["entity"]

[source.hash]
raw_column = "dna_sequence"
""",
        encoding="utf-8",
    )
    out_dir = tmp_path / "bronze"
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "shard", str(manifest), "--out", str(out_dir)],
        fake=None,
    )
    assert result.exit_code != 0
    assert "entity_type" in result.output
    # No shard directory was produced on the invalid plan.
    assert not out_dir.exists()


# --------------------------------------------------------------------------
# stdout discipline + human render parity (matches the read-verb tests)
# --------------------------------------------------------------------------


def test_load_json_stdout_is_clean_when_warnings_present(monkeypatch, tmp_path):
    shard_dir = tmp_path / "bronze"
    shard_dir.mkdir()
    fake = _FakeIngestCapability(locus="in_pod")
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "load", str(shard_dir), "--dry-run", "--json"],
        fake=fake,
    )
    # stdout parses whole as one object => the dry-run warning did not leak onto
    # stdout as a loose line; it is only inside the envelope + on stderr.
    parsed = json.loads(result.stdout.strip())
    assert parsed["warnings"]
    assert "no rows were ingested" in result.stderr


def test_load_human_render_receives_same_dict_as_serialiser(monkeypatch, tmp_path):
    shard_dir = tmp_path / "bronze"
    shard_dir.mkdir()

    json_result = _invoke(
        monkeypatch,
        ["clearinghouse", "load", str(shard_dir), "--json"],
        fake=_FakeIngestCapability(locus="in_pod"),
    )
    serialised = json.loads(json_result.stdout.strip())

    captured = {}
    monkeypatch.setattr(ch, "_render", lambda env: captured.__setitem__("env", env))
    human_result = _invoke(
        monkeypatch,
        ["clearinghouse", "load", str(shard_dir)],
        fake=_FakeIngestCapability(locus="in_pod"),
    )
    assert human_result.exit_code == 0, human_result.output
    got = captured["env"]
    for key in _ENVELOPE_KEYS - {"generated_at"}:
        assert got[key] == serialised[key], key


# --------------------------------------------------------------------------
# plan never iterates source records
# --------------------------------------------------------------------------


def test_plan_never_iterates_source_records(monkeypatch, tmp_path):
    """``plan`` must be a cheap preview, not a full scan.

    It used to count rows with ``sum(1 for _ in adapter.iter_records())``,
    which on the real 264M-document protein store costs the same read as the
    sharding run it previews -- so the verb was unusable on the only sources
    that matter. The guard is an adapter whose ``iter_records`` RAISES: if
    ``plan`` touches it, this test fails loudly rather than quietly getting
    slower.
    """
    from kbutillib.domains.kbase.berdl import clearinghouse_shard as cs

    manifest = _write_fixture_manifest(tmp_path)

    class _NeverIterate(cs.FileSourceAdapter):
        def iter_records(self):
            raise AssertionError("plan iterated the source records")

    monkeypatch.setattr(cs, "get_adapter", lambda source: _NeverIterate(source))
    result = _invoke(monkeypatch, ["clearinghouse", "plan", str(manifest), "--json"])
    assert result.exit_code == 0, result.output
    env = json.loads(result.stdout.strip())
    for table in env["data"]["tables"]:
        # The parquet footer gives an EXACT count for no scan at all, and the
        # envelope names the mechanism so the number can be judged.
        assert table["row_count"] == len(_FIXTURE_SEQS)
        assert table["estimate_method"] == "parquet-footer-num-rows"


def test_plan_reports_a_null_row_count_rather_than_scanning_for_one(
    monkeypatch, tmp_path
):
    """An adapter with no cheap estimate gets ``row_count: null`` and a
    warning -- never a scan behind the operator's back."""
    from kbutillib.domains.kbase.berdl import clearinghouse_shard as cs

    manifest = _write_fixture_manifest(tmp_path)

    class _NoEstimate(cs.FileSourceAdapter):
        def iter_records(self):
            raise AssertionError("plan iterated the source records")

        def estimate_rows(self):
            return None

        def estimate_rows_method(self):
            return "unavailable"

    monkeypatch.setattr(cs, "get_adapter", lambda source: _NoEstimate(source))
    result = _invoke(monkeypatch, ["clearinghouse", "plan", str(manifest), "--json"])
    assert result.exit_code == 0, result.output
    # The warnings go to stderr and the envelope to stdout; pick the envelope
    # out by line rather than assuming the runner separates the two streams
    # (this click's CliRunner mixes them by default).
    env = json.loads(
        next(
            line
            for line in result.output.splitlines()
            if line.startswith("{")
        )
    )
    assert all(t["row_count"] is None for t in env["data"]["tables"])
    assert all(t["estimate_method"] == "unavailable" for t in env["data"]["tables"])
    assert env["warnings"]
    assert "warning:" in result.output


def test_plan_on_a_jsonl_source_has_no_cheap_estimate(tmp_path):
    """A text source carries no row-count metadata, so the file adapter says
    so rather than reading the file to find out."""
    from kbutillib.domains.kbase.berdl import clearinghouse_manifest as cm
    from kbutillib.domains.kbase.berdl import clearinghouse_shard as cs

    src = tmp_path / "genes.jsonl"
    src.write_text(
        "".join(json.dumps({"dna_sequence": s}) + "\n" for s in _FIXTURE_SEQS),
        encoding="utf-8",
    )
    toml_text = f"""
[[source]]
name = "jsonl-genes"
adapter = "file"
path = "{src}"
format = "jsonl"
entity_type = "gene"
kinds = ["entity"]

[source.hash]
raw_column = "dna_sequence"
"""
    (source,) = cm.load_manifest(toml_text).sources
    adapter = cs.get_adapter(source)
    assert adapter.estimate_rows() is None
    assert adapter.estimate_rows_method() == "unavailable"


def test_shard_envelope_carries_the_run_inventory(monkeypatch, tmp_path):
    """The ``shard`` envelope names the run, the unit and run.json, and every
    shard's sha256 matches the file on disk."""
    import hashlib

    manifest = _write_fixture_manifest(tmp_path)
    out_dir = tmp_path / "bronze"
    result = _invoke(
        monkeypatch,
        [
            "clearinghouse",
            "shard",
            str(manifest),
            "--out",
            str(out_dir),
            "--target-bytes",
            "512",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    env = json.loads(result.stdout.strip())
    data = env["data"]
    assert Path(data["run_manifest"]).is_file()
    assert data["run_id"] and data["unit_id"]
    assert data["skipped"] == []
    assert data["sources"]
    assert all(s["exhausted"] is True for s in data["sources"])
    for shard in data["shards"]:
        path = Path(shard["path"])
        assert shard["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert path.stem == shard["batch"]
        assert shard["source"] == ["fixture-genes"]
