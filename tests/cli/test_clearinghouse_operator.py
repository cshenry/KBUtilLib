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
    """

    def __init__(self, locus="in_pod", *, fail_on_write=False):
        self._locus_value = locus
        self._fail_on_write = fail_on_write
        self.ingest_calls: list[dict] = []
        self.verify_calls: list[dict] = []
        self.pre_write_asserted = False

    def _locus(self):
        return self._locus_value

    def ingest_shards(self, shard_dir, *, run_id, dry_run=False, reconcile=False):
        self.ingest_calls.append(
            {
                "shard_dir": str(shard_dir),
                "run_id": run_id,
                "dry_run": dry_run,
                "reconcile": reconcile,
            }
        )
        # The pre-write assertion runs in dry-run too (it is the operator's last
        # gate); model that it happened.
        self.pre_write_asserted = True
        if not dry_run and self._fail_on_write:
            raise AssertionError(
                "a real write reached ingest_shards() -- the dry-run gate leaked!"
            )
        if dry_run:
            action = "would_ingest"
        else:
            action = "ingest"
        return {
            "run_id": run_id,
            "namespace": "kbaseincubator.clearinghouse",
            "dry_run": dry_run,
            "tables": [
                {"name": "gene_entity", "batch": "batch-0000", "action": action}
            ],
        }

    def verify_run(self, shard_dir, *, run_id):
        self.verify_calls.append({"shard_dir": str(shard_dir), "run_id": run_id})
        return {
            "run_id": run_id,
            "namespace": "kbaseincubator.clearinghouse",
            "tables": [
                {"name": "gene_entity", "ledger_rows": 6, "live_rows": 6, "ok": True}
            ],
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
