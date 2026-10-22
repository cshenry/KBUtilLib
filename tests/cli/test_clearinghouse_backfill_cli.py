"""Tests for the ``kbu clearinghouse backfill`` verbs: list / run / shard / load / status.

Drives the real click CLI. No network, no live BERDL pod, no Spark, no Mongo:
the shard half runs for real against the shipped ``file-example`` preset and
its parquet fixture, and the ingest half is a fake ``ClearinghouseCapability``
injected by patching
:func:`kbutillib.interfaces.cli.clearinghouse._capability`, following the shape
of ``tests/cli/test_clearinghouse_operator.py``.

``$KBU_CLEARINGHOUSE_STATE_DIR`` is redirected under ``tmp_path`` wherever run
state is touched, so no test here can read or write the operator's real
``~/.kbu/clearinghouse/runs``.

Success criteria exercised here:
    (a) all five verbs emit the same versioned JSON envelope under --json
    (b) ``list`` shows the shipped ``file-example`` preset
    (c) ``run`` and ``load`` refuse off-pod, BEFORE any transport call, and
        the message names the locus
    (d) ``run``/``load`` end with the namespace-wide registry orphan check and
        exit non-zero on a finding, WITHOUT un-marking a verified unit
    (e) an ``incomplete`` unit exits non-zero; a ``partial`` one does not
    (f) ``--slice`` parsing, the key-range mapping and the non-mongo refusal
    (g) ``load --unit`` reaches the ingest and the verification
    (h) ``status`` merges the state file, run.json and the ledger over all
        sixteen tables
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import kbutillib.interfaces.cli.clearinghouse as ch
from kbutillib.cli import main
from kbutillib.domains.kbase.berdl import clearinghouse_backfill as cb
from kbutillib.domains.kbase.berdl import clearinghouse_ingest as ci
from kbutillib.domains.kbase.berdl import clearinghouse_schema as schema
from kbutillib.domains.kbase.berdl import clearinghouse_shard as cs

_ENVELOPE_KEYS = {"schema_version", "verb", "generated_at", "locus", "data", "warnings"}
_PRESET = "file-example"


@pytest.fixture()
def state_root(tmp_path, monkeypatch):
    root = tmp_path / "state"
    monkeypatch.setenv(ci.STATE_DIR_ENV, str(root))
    return root


class _FakeBackfillCapability:
    """A fake capability for the backfill ingest/verify half.

    Writes a real ledger line per batch (so ``status`` has something real to
    merge) and fails the test outright if a write reaches it while
    ``fail_on_write`` is set, which is how the dry-run gate is proven.
    """

    def __init__(
        self,
        *,
        locus="in_pod",
        verify_ok=True,
        orphans=None,
        fail_on_write=False,
    ):
        self._locus_value = locus
        self.verify_ok = verify_ok
        self._orphans = orphans or {"ok": True, "failures": []}
        self._fail_on_write = fail_on_write
        self.ingest_calls: list[dict] = []
        self.verify_calls: list[dict] = []
        self.orphan_calls = 0

    def _locus(self):
        return self._locus_value

    def ingest_shards(
        self,
        shard_dir,
        *,
        run_id=None,
        dry_run=False,
        reconcile=False,
        tenant=None,
        dataset=None,
        unit=None,
    ):
        self.ingest_calls.append(
            {
                "shard_dir": str(shard_dir),
                "run_id": run_id,
                "dry_run": dry_run,
                "reconcile": reconcile,
                "tenant": tenant,
                "dataset": dataset,
                "unit": unit,
            }
        )
        if not dry_run and self._fail_on_write:
            raise AssertionError(
                "a real write reached ingest_shards() -- the dry-run gate leaked!"
            )
        run = cs.read_run_manifest(Path(shard_dir))
        batches = [
            batch
            for batch in run.get("batches", [])
            if unit is None or batch.get("unit_id") == unit
        ]
        tables = []
        if not dry_run and run_id:
            ledger = ci.ExecutionLedger(ci.execution_state_dir(str(run_id)))
            for batch in batches:
                ledger.append(
                    {
                        "batch_id": batch["batch_id"],
                        "table": batch["table"],
                        "state": ci.LEDGER_COMMITTED,
                        "rows_in": batch["rows"],
                        "rows_dropped_present": 0,
                        "rows_ingested": batch["rows"],
                        "unit_id": batch.get("unit_id"),
                    }
                )
        for batch in batches:
            tables.append(
                {
                    "name": batch["table"],
                    "batch_id": batch["batch_id"],
                    "unit_id": batch.get("unit_id"),
                    "action": "would_ingest" if dry_run else "ingest",
                    "rows_in": batch["rows"],
                    "rows_dropped_present": 0,
                    "rows_ingested": batch["rows"],
                }
            )
        execution_id = run_id or "01EXECUTION00000000000000"
        return {
            "run_id": execution_id,
            "execution_id": execution_id,
            "source_run_id": run.get("run_id"),
            "namespace": f"{tenant}.{dataset}",
            "state_dir": str(ci.execution_state_dir(str(execution_id))),
            "dry_run": dry_run,
            "unit": unit,
            "lock": {"namespace": f"{tenant}.{dataset}", "pid": 1},
            "tables": tables,
        }

    def verify_run(self, shard_dir, *, run_id, unit=None, dataset=None):
        self.verify_calls.append(
            {
                "shard_dir": str(shard_dir),
                "run_id": run_id,
                "unit": unit,
                "dataset": dataset,
            }
        )
        run = cs.read_run_manifest(Path(shard_dir))
        units = [
            {
                "unit_id": entry["unit_id"],
                "ok": self.verify_ok,
                "missing_parameter_sets": [],
            }
            for entry in run.get("units", [])
            if unit is None or entry["unit_id"] == unit
        ]
        return {
            "run_id": run_id,
            "execution_id": run_id,
            "namespace": "kbaseincubator.clearinghouse",
            "unit": unit,
            "ok": self.verify_ok,
            "batches": [],
            "units": units,
            "tables": [],
            "discrepancies": (
                [] if self.verify_ok else [f"{shard_dir}: simulated unresolved batch"]
            ),
            "parameter_set_integrity": dict(self._orphans),
        }

    def parameter_set_orphans(self, **kwargs):
        self.orphan_calls += 1
        return dict(self._orphans)


def _runner() -> CliRunner:
    """A runner that keeps stdout and stderr APART.

    Under click < 8.2 the default is ``mix_stderr=True``, and with it
    ``result.stdout`` is stdout AND stderr interleaved -- which would make the
    output contract ("stdout under --json is exactly one JSON object, every
    warning on stderr") untestable, because the warnings would be in the
    string the test tries to parse. So the streams are split explicitly there.

    Click 8.2 REMOVED the parameter and made separate streams the only
    behaviour, so passing it raises ``TypeError``. Both generations satisfy
    pyproject's ``click >=8.0.1``, and the repo is tested against both, so the
    argument is probed rather than assumed: a bare ``CliRunner()`` on 8.2+ is
    exactly the runner this helper is asking for.
    """
    try:
        return CliRunner(mix_stderr=False)
    except TypeError:
        return CliRunner()


def _invoke(monkeypatch, argv, *, fake=None):
    if fake is not None:
        monkeypatch.setattr(ch, "_capability", lambda: fake)
    return _runner().invoke(main, argv, catch_exceptions=False)


def _all_output(result) -> str:
    """stdout plus stderr, for asserting on an ERROR message.

    A refusal is written to stderr (click does that for a ClickException), and
    with the streams split a test for the message has to look at both rather
    than silently passing because the text was nowhere.
    """
    stderr = result.stderr if result.stderr_bytes is not None else ""
    return result.stdout + stderr


def _assert_one_clean_envelope(stdout: str, verb: str, *, locus: str) -> dict:
    """The output contract: stdout under --json is EXACTLY one JSON object."""
    stripped = stdout.strip()
    parsed = json.loads(stripped)
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


def _shard_a_unit(tmp_path, *, run_id="01SHARDRUN00000000000000AB"):
    """Produce a real one-unit shard directory from the shipped preset."""
    out = tmp_path / "out"
    result = _runner().invoke(
        main,
        [
            "clearinghouse", "backfill", "shard", _PRESET,
            "--out", str(out), "--run-id", run_id, "--json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return out, cb.unit_dir_for(out, cs.SINGLE_UNIT)


# ==========================================================================
# (a)/(b) list
# ==========================================================================


def test_list_emits_one_envelope_showing_file_example(monkeypatch):
    result = _invoke(monkeypatch, ["clearinghouse", "backfill", "list", "--json"])
    assert result.exit_code == 0, result.output
    env = _assert_one_clean_envelope(result.stdout, "backfill-list", locus="anywhere")
    names = [row["name"] for row in env["data"]["presets"]]
    assert _PRESET in names
    row = next(r for r in env["data"]["presets"] if r["name"] == _PRESET)
    # name, adapter(s), target tables, pulls_from, needs_env -- all five.
    assert row["adapters"] == ["file"]
    assert schema.PARAMETER_SET_TABLE in row["tables"]
    assert "protein_result" in row["tables"]
    assert row["pulls_from"]
    assert row["needs_env"] == ["KBU_BACKFILL_EXAMPLE"]
    assert "error" not in row
    assert env["warnings"] == []


def test_list_runs_anywhere_including_off_pod(monkeypatch):
    """`list` must answer "what do I have to export" WITHOUT the environment a
    run would need, and without a pod."""
    fake = _FakeBackfillCapability(locus="off_pod")
    result = _invoke(monkeypatch, ["clearinghouse", "backfill", "list"], fake=fake)
    assert result.exit_code == 0, result.output
    assert _PRESET in result.stdout


def test_list_human_output_is_a_table(monkeypatch):
    result = _invoke(monkeypatch, ["clearinghouse", "backfill", "list"])
    assert result.exit_code == 0, result.output
    assert "name" in result.stdout
    assert _PRESET in result.stdout


# ==========================================================================
# (c) the in-pod-only refusals
# ==========================================================================


def test_run_refuses_off_pod_before_any_transport_call(monkeypatch, tmp_path):
    fake = _FakeBackfillCapability(locus="off_pod")
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "run", _PRESET, "--out", str(tmp_path / "o")],
        fake=fake,
    )
    assert result.exit_code != 0
    assert "IN-POD ONLY" in _all_output(result)
    assert "'off_pod'" in _all_output(result)  # the locus is NAMED as the reason
    # NOTHING happened: no ingest, no verify, not even the orphan check, and
    # no directory was created.
    assert fake.ingest_calls == []
    assert fake.verify_calls == []
    assert fake.orphan_calls == 0
    assert not (tmp_path / "o").exists()


def test_load_refuses_off_pod_before_any_transport_call(monkeypatch, tmp_path):
    _out, unit_dir = _shard_a_unit(tmp_path)
    fake = _FakeBackfillCapability(locus="off_pod")
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "load", str(unit_dir)],
        fake=fake,
    )
    assert result.exit_code != 0
    assert "IN-POD ONLY" in _all_output(result)
    assert "'off_pod'" in _all_output(result)
    assert fake.ingest_calls == []
    assert fake.orphan_calls == 0


def test_shard_and_status_are_not_in_pod_only(monkeypatch, tmp_path, state_root):
    """Sharding needs CPU and disk; status reads local files. Both run anywhere."""
    out, _unit_dir = _shard_a_unit(tmp_path)
    fake = _FakeBackfillCapability(locus="off_pod")
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "status", "--out", str(out), "--json"],
        fake=fake,
    )
    assert result.exit_code == 0, result.output
    _assert_one_clean_envelope(result.stdout, "backfill-status", locus="anywhere")


# ==========================================================================
# (a) shard
# ==========================================================================


def test_shard_emits_one_envelope_and_writes_real_shards(monkeypatch, tmp_path):
    out = tmp_path / "out"
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "shard", _PRESET, "--out", str(out), "--json"],
    )
    assert result.exit_code == 0, result.output
    env = _assert_one_clean_envelope(result.stdout, "backfill-shard", locus="anywhere")
    data = env["data"]
    assert data["preset"] == _PRESET
    assert data["unit_kind"] == cb.UNIT_KIND_SINGLE
    assert data["unit_count"] == 1
    assert data["units"][0]["action"] == "sharded"
    assert data["units"][0]["batches"] == 6
    # The shards physically exist, with their own inventory.
    unit_dir = cb.unit_dir_for(out, cs.SINGLE_UNIT)
    assert cs.run_manifest_path(unit_dir).is_file()
    assert cb.published_files(unit_dir)
    # It says plainly that nothing was ingested and no unit was recorded.
    assert any("nothing was ingested" in w for w in env["warnings"])
    assert cb.unit_record(cb.read_state(out), cs.SINGLE_UNIT)["state"] == (
        cb.UNIT_PENDING
    )


def test_shard_resume_reuses_a_published_unit(monkeypatch, tmp_path):
    out = tmp_path / "out"
    argv = ["clearinghouse", "backfill", "shard", _PRESET, "--out", str(out), "--json"]
    first = _invoke(monkeypatch, argv)
    assert first.exit_code == 0, first.output
    before = cb.published_files(cb.unit_dir_for(out, cs.SINGLE_UNIT))

    again = _invoke(monkeypatch, [*argv, "--resume"])
    assert again.exit_code == 0, again.output
    env = _assert_one_clean_envelope(again.stdout, "backfill-shard", locus="anywhere")
    assert env["data"]["units"][0]["action"] == "reused"
    # NOT ONE published batch was deleted.
    assert cb.published_files(cb.unit_dir_for(out, cs.SINGLE_UNIT)) == before


def test_shard_without_resume_refuses_an_already_published_unit(
    monkeypatch, tmp_path
):
    out = tmp_path / "out"
    argv = ["clearinghouse", "backfill", "shard", _PRESET, "--out", str(out)]
    assert _invoke(monkeypatch, argv).exit_code == 0
    again = _invoke(monkeypatch, argv)
    assert again.exit_code != 0
    assert "already holds a published" in _all_output(again)


def test_shard_limit_caps_records_and_reports_it(monkeypatch, tmp_path):
    out = tmp_path / "out"
    result = _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "shard", _PRESET,
            "--out", str(out), "--limit", "6", "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    env = _assert_one_clean_envelope(result.stdout, "backfill-shard", locus="anywhere")
    assert env["data"]["limit"] == 6
    run = cs.read_run_manifest(cb.unit_dir_for(out, cs.SINGLE_UNIT))
    assert {outcome["emitted"] for outcome in run["sources"]} == {6}
    assert all(outcome["exhausted"] is False for outcome in run["sources"])


# ==========================================================================
# (f) --slice
# ==========================================================================


def test_shard_refuses_slice_against_a_non_mongo_preset(monkeypatch, tmp_path):
    result = _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "shard", _PRESET,
            "--out", str(tmp_path / "o"), "--slice", "00",
        ],
    )
    assert result.exit_code != 0
    assert "'mongo'" in _all_output(result)
    assert "'file'" in _all_output(result)  # the offending adapter is NAMED


@pytest.mark.parametrize("spec", ["0F", "0", "g0", "00-", "ff-00", "00:0f"])
def test_run_rejects_a_malformed_slice(monkeypatch, tmp_path, spec, state_root):
    fake = _FakeBackfillCapability()
    result = _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "run", _PRESET,
            "--out", str(tmp_path / "o"), "--slice", spec,
        ],
        fake=fake,
    )
    assert result.exit_code != 0
    assert fake.ingest_calls == []


def test_a_mongo_manifest_slice_maps_to_the_locator_range(monkeypatch, tmp_path):
    """The CLI plans the units and rewrites min_key/max_key; no connection is
    made, because planning builds adapters without connecting."""
    manifest_path = tmp_path / "mongo.toml"
    manifest_path.write_text(
        """
[backfill]
pulls_from = ["mongo://example:27017/db.seq_protein"]

[[source]]
name = "mongo-protein"
adapter = "mongo"
uri = "mongodb://${MONGO_HOST:-example}:27017"
database = "db"
collection = "seq_protein"
sort_key = "_id"
entity_type = "protein"
kinds = ["entity"]

  [source.hash]
  raw_column = "aa"
""",
        encoding="utf-8",
    )
    backfill, manifest, _path, plan, preset = ch._resolve_backfill_target(
        str(manifest_path), slice_spec="0e-0f", limit=None
    )
    assert preset is None
    assert plan.kind == cb.UNIT_KIND_SLICE
    assert plan.units == ("0e", "0f")
    assert plan.key_range == ("0e", "10")
    rewritten = backfill.manifest_for_unit(manifest, plan, "0f")
    assert rewritten.sources[0].locator["min_key"] == "0f"
    assert rewritten.sources[0].locator["max_key"] == "10"
    assert rewritten.sources[0].locator["uri"] == "mongodb://example:27017"


# ==========================================================================
# (a)/(d)/(e) run
# ==========================================================================


def test_run_emits_one_envelope_and_records_the_unit_done(
    monkeypatch, tmp_path, state_root
):
    out = tmp_path / "out"
    fake = _FakeBackfillCapability()
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "run", _PRESET, "--out", str(out), "--json"],
        fake=fake,
    )
    assert result.exit_code == 0, result.output
    env = _assert_one_clean_envelope(result.stdout, "backfill-run", locus="in_pod")
    data = env["data"]
    assert data["preset"] == _PRESET
    assert data["unit_kind"] == cb.UNIT_KIND_SINGLE
    assert data["units"][0]["state"] == cb.UNIT_DONE
    assert data["incomplete_units"] == []
    assert data["registration"]["dataset"] == schema.NAMESPACE
    assert data["registration"]["tenant"] == schema.TENANT
    # shard -> ingest -> verify, then the record.
    assert len(fake.ingest_calls) == 1
    assert len(fake.verify_calls) == 1
    assert cb.unit_record(cb.read_state(out), cs.SINGLE_UNIT)["state"] == cb.UNIT_DONE


def test_run_defaults_out_under_clearinghouse_shards_and_the_run_id(
    monkeypatch, tmp_path, state_root
):
    """--out defaults to ~/clearinghouse-shards/<run_id>/ -- derived from the
    run id so a --resume finds the shards without being told where."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    fake = _FakeBackfillCapability()
    result = _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "run", _PRESET,
            "--run-id", "01RUNDEFAULT00000000000000", "--json",
        ],
        fake=fake,
    )
    assert result.exit_code == 0, result.output
    env = _assert_one_clean_envelope(result.stdout, "backfill-run", locus="in_pod")
    expected = (
        tmp_path / "home" / "clearinghouse-shards" / "01RUNDEFAULT00000000000000"
    )
    assert env["data"]["out"] == str(expected)
    assert cs.run_manifest_path(cb.unit_dir_for(expected, cs.SINGLE_UNIT)).is_file()


def test_run_passes_the_dataset_through_to_the_ingest(
    monkeypatch, tmp_path, state_root
):
    fake = _FakeBackfillCapability()
    result = _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "run", _PRESET,
            "--out", str(tmp_path / "out"), "--dataset", "clearinghouse_scratch",
            "--json",
        ],
        fake=fake,
    )
    assert result.exit_code == 0, result.output
    assert fake.ingest_calls[0]["dataset"] == "clearinghouse_scratch"
    assert fake.ingest_calls[0]["tenant"] == schema.TENANT


def test_run_resume_refuses_a_different_dataset(monkeypatch, tmp_path, state_root):
    """A scratch run and a production run never share a run id or a ledger."""
    out = tmp_path / "out"
    fake = _FakeBackfillCapability()
    first = _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "run", _PRESET, "--out", str(out),
            "--run-id", "01SCRATCHRUN00000000000000",
            "--dataset", "clearinghouse_scratch", "--json",
        ],
        fake=fake,
    )
    assert first.exit_code == 0, first.output

    conflicting = _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "run", _PRESET, "--out", str(out),
            "--run-id", "01SCRATCHRUN00000000000000",
            "--dataset", "clearinghouse", "--resume",
        ],
        fake=fake,
    )
    assert conflicting.exit_code != 0
    assert "CONFLICTS" in _all_output(conflicting)
    assert "clearinghouse_scratch" in _all_output(conflicting)


def test_run_resume_adopts_the_run_id_from_the_state_file(
    monkeypatch, tmp_path, state_root
):
    out = tmp_path / "out"
    fake = _FakeBackfillCapability()
    first = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "run", _PRESET, "--out", str(out), "--json"],
        fake=fake,
    )
    run_id = json.loads(first.stdout)["data"]["run_id"]

    # No --run-id: the state file at --out is what identifies the run.
    again = _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "run", _PRESET, "--out", str(out),
            "--resume", "--json",
        ],
        fake=fake,
    )
    assert again.exit_code == 0, again.output
    env = _assert_one_clean_envelope(again.stdout, "backfill-run", locus="in_pod")
    assert env["data"]["run_id"] == run_id
    assert env["data"]["units"][0]["action"] == "skipped-done"


def test_run_resume_with_nothing_to_resume_is_refused(
    monkeypatch, tmp_path, state_root
):
    result = _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "run", _PRESET,
            "--out", str(tmp_path / "nowhere"), "--resume",
        ],
        fake=_FakeBackfillCapability(),
    )
    assert result.exit_code != 0
    assert "found no run to resume" in _all_output(result)


def test_a_limit_stopped_unit_is_partial_warned_about_and_exits_zero(
    monkeypatch, tmp_path, state_root
):
    """--limit is the outcome the operator ASKED FOR, so it must not look like
    a failure -- but it must not look like `done` either."""
    out = tmp_path / "out"
    result = _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "run", _PRESET, "--out", str(out),
            "--limit", "9", "--json",
        ],
        fake=_FakeBackfillCapability(),
    )
    assert result.exit_code == 0, result.output
    env = _assert_one_clean_envelope(result.stdout, "backfill-run", locus="in_pod")
    assert env["data"]["units"][0]["state"] == cb.UNIT_PARTIAL
    assert env["data"]["incomplete_units"] == []
    assert any("PARTIAL" in w for w in env["warnings"])
    assert cb.unit_record(cb.read_state(out), cs.SINGLE_UNIT)["state"] == (
        cb.UNIT_PARTIAL
    )


def test_an_incomplete_unit_exits_nonzero_and_is_retried_on_resume(
    monkeypatch, tmp_path, state_root
):
    out = tmp_path / "out"
    failing = _FakeBackfillCapability(verify_ok=False)
    first = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "run", _PRESET, "--out", str(out), "--json"],
        fake=failing,
    )
    # Non-zero EVEN THOUGH every emitted batch was ingested: "the rows I wrote
    # are correct" is not "the unit is finished".
    assert first.exit_code == 1
    env = _assert_one_clean_envelope(first.stdout, "backfill-run", locus="in_pod")
    assert env["data"]["units"][0]["state"] == cb.UNIT_INCOMPLETE
    assert env["data"]["incomplete_units"] == [cs.SINGLE_UNIT]
    assert env["data"]["units"][0]["reasons"]
    run_id = env["data"]["run_id"]
    before = cb.published_files(cb.unit_dir_for(out, cs.SINGLE_UNIT))

    healthy = _FakeBackfillCapability(verify_ok=True)
    again = _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "run", _PRESET, "--out", str(out),
            "--run-id", run_id, "--resume", "--json",
        ],
        fake=healthy,
    )
    assert again.exit_code == 0, again.output
    env2 = _assert_one_clean_envelope(again.stdout, "backfill-run", locus="in_pod")
    assert env2["data"]["units"][0]["state"] == cb.UNIT_DONE
    # The retry reconciled before touching anything and deleted NO published
    # batch.
    assert healthy.ingest_calls[0]["reconcile"] is True
    assert cb.published_files(cb.unit_dir_for(out, cs.SINGLE_UNIT)) == before


def test_run_ends_with_the_namespace_registry_orphan_check(
    monkeypatch, tmp_path, state_root
):
    out = tmp_path / "out"
    fake = _FakeBackfillCapability()
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "run", _PRESET, "--out", str(out), "--json"],
        fake=fake,
    )
    assert result.exit_code == 0, result.output
    env = _assert_one_clean_envelope(result.stdout, "backfill-run", locus="in_pod")
    assert env["data"]["parameter_set_integrity"] == {"ok": True, "failures": []}
    assert fake.orphan_calls >= 1


def test_a_registry_orphan_exits_nonzero_without_unmarking_the_verified_unit(
    monkeypatch, tmp_path, state_root
):
    """An orphan is a property of the NAMESPACE and may be another writer's.
    Retracting proof that this run's rows landed would be a lie told to make a
    different problem look tidier."""
    out = tmp_path / "out"
    orphans = {
        "ok": False,
        "failures": [
            "protein_result: 2 parameter_set_hash value(s) have no registry row",
            "parameter_set: 1 row whose canonical_json does not hash to its key",
        ],
    }
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "run", _PRESET, "--out", str(out), "--json"],
        fake=_FakeBackfillCapability(orphans=orphans),
    )
    assert result.exit_code == 1
    env = _assert_one_clean_envelope(result.stdout, "backfill-run", locus="in_pod")
    # The findings are printed IN FULL, not counted.
    assert env["data"]["parameter_set_integrity"]["failures"] == orphans["failures"]
    # The unit stays DONE, in the envelope and on disk.
    assert env["data"]["units"][0]["state"] == cb.UNIT_DONE
    assert cb.unit_record(cb.read_state(out), cs.SINGLE_UNIT)["state"] == cb.UNIT_DONE


def test_run_human_output_names_the_run_and_the_units(
    monkeypatch, tmp_path, state_root
):
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "run", _PRESET, "--out", str(tmp_path / "out")],
        fake=_FakeBackfillCapability(),
    )
    assert result.exit_code == 0, result.output
    assert "run_id=" in result.stdout
    assert "units: 1 (single)" in result.stdout


def test_run_json_stdout_is_clean_when_warnings_are_present(
    monkeypatch, tmp_path, state_root
):
    """Under --json, stdout is ONE object and every warning goes to stderr."""
    runner = _runner()
    monkeypatch.setattr(
        ch, "_capability", lambda: _FakeBackfillCapability()
    )
    result = runner.invoke(
        main,
        [
            "clearinghouse", "backfill", "run", _PRESET,
            "--out", str(tmp_path / "out"), "--limit", "4", "--json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    env = _assert_one_clean_envelope(result.stdout, "backfill-run", locus="in_pod")
    assert env["warnings"]


def test_retry_failed_archives_is_recorded_in_the_envelope(
    monkeypatch, tmp_path, state_root
):
    result = _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "run", _PRESET,
            "--out", str(tmp_path / "out"), "--retry-failed-archives", "--json",
        ],
        fake=_FakeBackfillCapability(),
    )
    assert result.exit_code == 0, result.output
    env = _assert_one_clean_envelope(result.stdout, "backfill-run", locus="in_pod")
    assert env["data"]["retry_failed_archives"] is True


def test_an_unresolved_placeholder_fails_before_a_run_id_is_minted(
    monkeypatch, tmp_path, state_root
):
    manifest_path = tmp_path / "unset.toml"
    manifest_path.write_text(
        """
[[source]]
name = "p"
adapter = "file"
path = "${ARC_ROOT}/g.parquet"
format = "parquet"
entity_type = "gene"
kinds = ["entity"]

  [source.hash]
  raw_column = "dna_sequence"
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("ARC_ROOT", raising=False)
    fake = _FakeBackfillCapability()
    result = _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "run", str(manifest_path),
            "--out", str(tmp_path / "out"),
        ],
        fake=fake,
    )
    assert result.exit_code != 0
    assert "ARC_ROOT" in _all_output(result)
    assert fake.ingest_calls == []
    # Nothing was registered and no output directory was created.
    assert not (tmp_path / "out").exists()


def test_an_unknown_preset_is_a_clean_nonzero_naming_what_is_available(
    monkeypatch, tmp_path, state_root
):
    result = _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "run", "no-such-preset",
            "--out", str(tmp_path / "out"),
        ],
        fake=_FakeBackfillCapability(),
    )
    assert result.exit_code != 0
    assert "neither a shipped backfill preset" in _all_output(result)
    assert _PRESET in _all_output(result)


# ==========================================================================
# (a)/(g) load
# ==========================================================================


def test_load_emits_one_envelope_and_verifies_the_unit(
    monkeypatch, tmp_path, state_root
):
    _out, unit_dir = _shard_a_unit(tmp_path)
    fake = _FakeBackfillCapability()
    result = _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "load", str(unit_dir),
            "--run-id", "01LOADEXEC000000000000000A", "--json",
        ],
        fake=fake,
    )
    assert result.exit_code == 0, result.output
    env = _assert_one_clean_envelope(result.stdout, "backfill-load", locus="in_pod")
    data = env["data"]
    assert data["execution_id"] == "01LOADEXEC000000000000000A"
    assert data["tables"]
    assert data["units"] and all(u["ok"] for u in data["units"])
    assert data["unverified_units"] == []
    # Unlike `clearinghouse load`, this half also VERIFIES.
    assert len(fake.verify_calls) == 1


def test_load_records_the_mapping_from_shard_run_to_load_execution(
    monkeypatch, tmp_path, state_root
):
    """Loading another run's shards is a NEW load that keeps the shard batch
    ids and records the mapping -- which is what makes scratch-then-production
    promotion provable twice over."""
    _out, unit_dir = _shard_a_unit(tmp_path, run_id="01SHARDRUNX0000000000000AB")
    fake = _FakeBackfillCapability()
    result = _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "load", str(unit_dir),
            "--run-id", "01PRODLOAD0000000000000000", "--json",
        ],
        fake=fake,
    )
    assert result.exit_code == 0, result.output
    mapping = json.loads(result.stdout)["data"]["id_mapping"]
    assert mapping == {
        "shard_run_id": "01SHARDRUNX0000000000000AB",
        "load_execution_id": "01PRODLOAD0000000000000000",
    }


def test_load_unit_restricts_both_the_ingest_and_the_verification(
    monkeypatch, tmp_path, state_root
):
    _out, unit_dir = _shard_a_unit(tmp_path)
    unit_id = cs.read_run_manifest(unit_dir)["units"][0]["unit_id"]
    fake = _FakeBackfillCapability()
    result = _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "load", str(unit_dir),
            "--unit", unit_id, "--json",
        ],
        fake=fake,
    )
    assert result.exit_code == 0, result.output
    env = _assert_one_clean_envelope(result.stdout, "backfill-load", locus="in_pod")
    assert env["data"]["unit"] == unit_id
    assert fake.ingest_calls[0]["unit"] == unit_id
    assert fake.verify_calls[0]["unit"] == unit_id


def test_load_dry_run_writes_nothing_and_does_not_verify(
    monkeypatch, tmp_path, state_root
):
    _out, unit_dir = _shard_a_unit(tmp_path)
    fake = _FakeBackfillCapability(fail_on_write=True)
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "load", str(unit_dir), "--dry-run", "--json"],
        fake=fake,
    )
    assert result.exit_code == 0, result.output
    env = _assert_one_clean_envelope(result.stdout, "backfill-load", locus="in_pod")
    assert env["data"]["dry_run"] is True
    assert any("dry-run" in w for w in env["warnings"])
    # A dry run proves nothing about landing, so it does not pretend to verify.
    assert fake.verify_calls == []
    assert env["data"]["units"] == []


def test_load_exits_nonzero_on_an_unverified_unit(monkeypatch, tmp_path, state_root):
    _out, unit_dir = _shard_a_unit(tmp_path)
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "load", str(unit_dir), "--json"],
        fake=_FakeBackfillCapability(verify_ok=False),
    )
    assert result.exit_code == 1
    env = _assert_one_clean_envelope(result.stdout, "backfill-load", locus="in_pod")
    assert env["data"]["unverified_units"]
    assert env["data"]["discrepancies"]


def test_load_ends_with_the_registry_orphan_check_and_exits_nonzero_on_one(
    monkeypatch, tmp_path, state_root
):
    _out, unit_dir = _shard_a_unit(tmp_path)
    orphans = {"ok": False, "failures": ["protein_result: 1 orphan hash"]}
    fake = _FakeBackfillCapability(orphans=orphans)
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "load", str(unit_dir), "--json"],
        fake=fake,
    )
    assert result.exit_code == 1
    env = _assert_one_clean_envelope(result.stdout, "backfill-load", locus="in_pod")
    assert env["data"]["parameter_set_integrity"]["failures"] == orphans["failures"]
    assert fake.orphan_calls >= 1


def test_load_reconcile_is_passed_through(monkeypatch, tmp_path, state_root):
    _out, unit_dir = _shard_a_unit(tmp_path)
    fake = _FakeBackfillCapability()
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "load", str(unit_dir), "--reconcile", "--json"],
        fake=fake,
    )
    assert result.exit_code == 0, result.output
    assert fake.ingest_calls[0]["reconcile"] is True


# ==========================================================================
# (a)/(h) status
# ==========================================================================


def test_status_emits_one_envelope_merging_all_three_records(
    monkeypatch, tmp_path, state_root
):
    out = tmp_path / "out"
    fake = _FakeBackfillCapability()
    run = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "run", _PRESET, "--out", str(out), "--json"],
        fake=fake,
    )
    assert run.exit_code == 0, run.output
    run_id = json.loads(run.stdout)["data"]["run_id"]

    result = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "status", "--run-id", run_id,
         "--out", str(out), "--json"],
        fake=fake,
    )
    assert result.exit_code == 0, result.output
    env = _assert_one_clean_envelope(result.stdout, "backfill-status", locus="anywhere")
    data = env["data"]
    assert data["run_id"] == run_id
    assert data["totals"][cb.UNIT_DONE] == 1
    # the state file ...
    assert data["units"][0]["state"] == cb.UNIT_DONE
    # ... run.json ...
    assert data["units"][0]["batches"] == 6
    assert data["units"][0]["rows_sharded"] > 0
    # ... and the ledger.
    assert data["units"][0]["rows_ingested"] == data["units"][0]["rows_sharded"]
    # ALL SIXTEEN tables, the parameter-set registry included.
    assert len(data["tables"]) == 16
    by_name = {row["name"]: row for row in data["tables"]}
    assert set(by_name) == {c["name"] for c in schema.table_configs()}
    assert by_name[schema.PARAMETER_SET_TABLE]["rows_ingested"] >= 1
    assert by_name[schema.PARAMETER_SET_TABLE]["is_registry"] is True
    assert by_name["protein_entity"]["rows_ingested"] == 48


def test_status_finds_the_run_from_out_alone(monkeypatch, tmp_path, state_root):
    out = tmp_path / "out"
    fake = _FakeBackfillCapability()
    run = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "run", _PRESET, "--out", str(out), "--json"],
        fake=fake,
    )
    run_id = json.loads(run.stdout)["data"]["run_id"]
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "status", "--out", str(out), "--json"],
        fake=fake,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["run_id"] == run_id


def test_status_warns_about_partial_and_incomplete_units(
    monkeypatch, tmp_path, state_root
):
    out = tmp_path / "out"
    _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "run", _PRESET, "--out", str(out),
            "--limit", "5", "--json",
        ],
        fake=_FakeBackfillCapability(),
    )
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "status", "--out", str(out), "--json"],
        fake=_FakeBackfillCapability(),
    )
    assert result.exit_code == 0, result.output
    env = _assert_one_clean_envelope(result.stdout, "backfill-status", locus="anywhere")
    assert env["data"]["totals"][cb.UNIT_PARTIAL] == 1
    assert any("PARTIAL" in w for w in env["warnings"])


def test_status_with_neither_run_id_nor_out_is_refused(monkeypatch, state_root):
    result = _invoke(monkeypatch, ["clearinghouse", "backfill", "status"])
    assert result.exit_code != 0
    assert "--run-id" in _all_output(result)


def test_status_of_an_unknown_out_is_refused(monkeypatch, tmp_path, state_root):
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "status", "--out", str(tmp_path / "nope")],
    )
    assert result.exit_code != 0
    assert "found no run" in _all_output(result)


def test_status_of_a_shard_only_directory_reports_units_as_pending(
    monkeypatch, tmp_path, state_root
):
    """`backfill shard` records no unit state -- a unit is recorded only once
    its rows are proven -- so status must say pending, not done."""
    out, _unit_dir = _shard_a_unit(tmp_path)
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "status", "--out", str(out), "--json"],
    )
    assert result.exit_code == 0, result.output
    env = _assert_one_clean_envelope(result.stdout, "backfill-status", locus="anywhere")
    assert env["data"]["pending_units"] == [cs.SINGLE_UNIT]
    assert env["data"]["totals"][cb.UNIT_DONE] == 0
    assert all(row["rows_ingested"] == 0 for row in env["data"]["tables"])


def test_status_human_output_names_the_run_and_the_totals(
    monkeypatch, tmp_path, state_root
):
    out, _unit_dir = _shard_a_unit(tmp_path)
    result = _invoke(
        monkeypatch, ["clearinghouse", "backfill", "status", "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "run_id=" in result.stdout
    assert "units: " in result.stdout
    assert schema.PARAMETER_SET_TABLE in result.stdout


# ==========================================================================
# (a) the envelope contract across every verb
# ==========================================================================


def test_every_backfill_verb_emits_the_same_envelope_shape(
    monkeypatch, tmp_path, state_root
):
    out = tmp_path / "out"
    fake = _FakeBackfillCapability()
    invocations = [
        (["clearinghouse", "backfill", "list"], "backfill-list", "anywhere"),
        (
            ["clearinghouse", "backfill", "shard", _PRESET, "--out", str(out / "s")],
            "backfill-shard",
            "anywhere",
        ),
        (
            ["clearinghouse", "backfill", "run", _PRESET, "--out", str(out / "r")],
            "backfill-run",
            "in_pod",
        ),
    ]
    for argv, verb, locus in invocations:
        result = _invoke(monkeypatch, [*argv, "--json"], fake=fake)
        assert result.exit_code == 0, (argv, result.output)
        _assert_one_clean_envelope(result.stdout, verb, locus=locus)

    unit_dir = cb.unit_dir_for(out / "s", cs.SINGLE_UNIT)
    load = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "load", str(unit_dir), "--json"],
        fake=fake,
    )
    assert load.exit_code == 0, load.output
    _assert_one_clean_envelope(load.stdout, "backfill-load", locus="in_pod")

    status = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "status", "--out", str(out / "r"), "--json"],
        fake=fake,
    )
    assert status.exit_code == 0, status.output
    _assert_one_clean_envelope(status.stdout, "backfill-status", locus="anywhere")


def test_the_backfill_group_is_reachable_under_clearinghouse():
    result = _runner().invoke(
        main, ["clearinghouse", "backfill", "--help"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    for verb in ("list", "run", "shard", "load", "status"):
        assert verb in result.stdout


def test_human_render_receives_the_same_dict_the_serialiser_would_emit(
    monkeypatch, tmp_path, state_root
):
    """Output-contract rule 1: there is no second computation path."""
    captured: list[dict] = []
    monkeypatch.setattr(ch, "_render", lambda envelope: captured.append(envelope))
    fake = _FakeBackfillCapability()
    result = _invoke(
        monkeypatch,
        ["clearinghouse", "backfill", "run", _PRESET, "--out", str(tmp_path / "out")],
        fake=fake,
    )
    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert set(captured[0]) == _ENVELOPE_KEYS
    assert captured[0]["verb"] == "backfill-run"
    json.dumps(captured[0])  # the rendered object is exactly what --json emits


# ==========================================================================
# The `kbdl-genome-archives` preset: adapter units, FROZEN before any job
# ==========================================================================
#
# Covers the one thing the adapter's own tests cannot: that the CLI freezes the
# unit list where it has to -- after the run id and --out are resolved, and
# BEFORE register_run, record_planned_units and run_backfill (which is what
# submits the hash jobs). The adapter is wired to a fake KBDL client so no
# network, no poplar and no real job are involved.

_KBDL_PRESET = "kbdl-genome-archives"
_ARCHIVE_A = "a" * 64
_ARCHIVE_B = "b" * 64
_ARCHIVE_C = "c" * 64


class _FakeHashClient:
    """The KBDL surface the ``kbdl_hash`` adapter uses, and nothing else."""

    def __init__(self, archive_ids):
        self.objects = {
            # Listed in REVERSE lexicographic order, because the adapter's
            # sort is what makes a frozen list reproducible and a fixture that
            # was already sorted could not show it.
            object_id: {"object_id": object_id, "object_type": "GenomeArchive"}
            for object_id in sorted(archive_ids, reverse=True)
        }
        self.submitted: list[list[str]] = []
        self._counter = 0
        self._results: dict[str, dict] = {}

    def list_objects(self):
        return list(self.objects.values())

    def get_object_metadata(self, object_id):
        from kbutillib.domains.external import kbdl_service_utils as ksu

        if object_id not in self.objects:
            raise ksu.KBDLNotFoundError(f"no object {object_id}")
        return dict(self.objects[object_id])

    def submit_hash_genomes(self, archive_ids):
        import hashlib

        from kbutillib.domains.identity import standardizers

        ids = list(archive_ids)
        self._counter += 1
        job_id = f"job-{self._counter}"
        self.submitted.append(ids)
        self._results[job_id] = {
            "genomes": {
                "g1.fna": {
                    "entity_hash": hashlib.sha256(
                        f"{ids[0]}:g1.fna".encode()
                    ).hexdigest(),
                    "standardizer_version": standardizers.STANDARDIZER_VERSION,
                    "archive_object_id": ids[0],
                    "entry_name": "g1.fna",
                    "n_contigs": 1,
                    "total_length": 10,
                }
            },
            "errors": {},
        }
        return job_id

    def check_job(self, job_id):
        return {
            "job_id": job_id,
            "state": "completed",
            "completed_at": "2026-10-08T12:00:00Z",
        }

    def poll_until_terminal(self, job_id, **_kw):  # pragma: no cover - terminal
        return self.check_job(job_id)

    def get_job_result(self, job_id):
        return self._results[job_id]


@pytest.fixture()
def wired_kbdl_hash(monkeypatch):
    client = _FakeHashClient([_ARCHIVE_A, _ARCHIVE_B])

    class _Wired(cs.KbdlHashSourceAdapter):
        def __init__(self, source):
            super().__init__(source, client=client)

    monkeypatch.setitem(cs._ADAPTER_CLASSES, "kbdl_hash", _Wired)
    return client


def test_run_plans_one_adapter_unit_per_archive_and_freezes_the_list(
    monkeypatch, tmp_path, state_root, wired_kbdl_hash
):
    out = tmp_path / "out"
    result = _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "run", _KBDL_PRESET,
            "--out", str(out), "--json",
        ],
        fake=_FakeBackfillCapability(),
    )

    assert result.exit_code == 0, _all_output(result)
    env = _assert_one_clean_envelope(result.stdout, "backfill-run", locus="in_pod")
    data = env["data"]
    assert data["unit_kind"] == cb.UNIT_KIND_ADAPTER
    assert [u["unit"] for u in data["units"]] == [_ARCHIVE_A, _ARCHIVE_B]
    assert all(u["state"] == cb.UNIT_DONE for u in data["units"])
    # ONE hash job per archive, each naming only its own archive.
    assert wired_kbdl_hash.submitted == [[_ARCHIVE_A], [_ARCHIVE_B]]
    # and the list is on disk, sorted, for a resume to drive.
    assert cb.read_state(out)[cb.FROZEN_UNITS_FIELD] == [_ARCHIVE_A, _ARCHIVE_B]


def test_run_resume_drives_the_frozen_list_and_ignores_rediscovery(
    monkeypatch, tmp_path, state_root, wired_kbdl_hash
):
    """An archive uploaded mid-run does NOT join the run, and one that has
    disappeared is reported rather than quietly dropped."""
    out = tmp_path / "out"
    first = _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "run", _KBDL_PRESET,
            "--out", str(out), "--limit", "1", "--json",
        ],
        fake=_FakeBackfillCapability(),
    )
    assert first.exit_code == 0, _all_output(first)
    assert json.loads(first.stdout)["data"]["units"][0]["unit"] == _ARCHIVE_A

    # The store moves on between invocations: B vanishes, C appears.
    del wired_kbdl_hash.objects[_ARCHIVE_B]
    wired_kbdl_hash.objects[_ARCHIVE_C] = {
        "object_id": _ARCHIVE_C,
        "object_type": "GenomeArchive",
    }

    second = _invoke(
        monkeypatch,
        [
            "clearinghouse", "backfill", "run", _KBDL_PRESET,
            "--out", str(out), "--resume", "--json",
        ],
        fake=_FakeBackfillCapability(),
    )

    # STOPS with the terminal cause, naming the archive: B was frozen into this
    # run and is now unreachable. No envelope, because the run did not finish.
    assert second.exit_code != 0
    message = _all_output(second)
    assert _ARCHIVE_B in message
    assert "frozen" in message.lower()
    # The frozen list is UNCHANGED -- C did not join the run, and B was not
    # dropped from it to make the run look complete.
    state = cb.read_state(out)
    assert state[cb.FROZEN_UNITS_FIELD] == [_ARCHIVE_A, _ARCHIVE_B]
    assert _ARCHIVE_C not in state[cb.FROZEN_UNITS_FIELD]
    assert _ARCHIVE_B in state[cb.VANISHED_UNITS_FIELD]
    # A is still recorded done from the first invocation, so the resume did not
    # redo it and did not resubmit its hash job.
    assert cb.unit_record(state, _ARCHIVE_A)["state"] == cb.UNIT_DONE
    assert wired_kbdl_hash.submitted == [[_ARCHIVE_A]]
