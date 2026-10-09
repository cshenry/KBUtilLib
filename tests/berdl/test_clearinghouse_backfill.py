"""Tests for the backfill DRIVER: units, run identity, resume, limits, status.

Pure logic. No network, no live BERDL pod, no Spark, no Mongo. The shard half
runs for real against the shipped ``file-example`` preset and its parquet
fixture (pyarrow, CPU and disk, exactly as the stage is designed to); the
ingest half is a hand-built fake capability that records what it was asked and
writes a real ledger line per batch, so ``status`` has a real ledger to merge
and the unit-state machine is driven by the same three records the production
path reads.

``$KBU_CLEARINGHOUSE_STATE_DIR`` is redirected to ``tmp_path`` in every test
that touches run state, so nothing here can see or write the operator's real
``~/.kbu/clearinghouse/runs``.

What is covered here (the CLI surface is in tests/cli/):
    - slice parsing: 00, 00-0f, ff, and every malformed form
    - --slice maps to the mongo adapter's half-open min_key/max_key range
    - non-mongo adapters refuse --slice, and refuse restrict_to_unit
    - the optional units() hook and its single-unit default
    - --limit for both unit kinds (records per reader vs. number of units)
    - ${VAR}, ${VAR:-default}, ~ and ${MANIFEST_DIR} expansion; the
      unresolved-variable ManifestError naming the variable
    - the informational [backfill] table
    - units processed in order, each marked done only AFTER verification
    - a run killed mid-unit resumes that unit exactly once
    - a --limit-stopped unit is `partial`, and a later full run redoes it
    - an `incomplete` unit, and that --resume retries it
    - immutable run identity: --resume restores omissions, refuses conflicts,
      and scratch/production never share a run id
    - resume after a crash deletes no published batch
    - --retry-failed-archives reaches the adapter through the run context
    - durable adapter state survives a new context for the same (run, unit)
    - status merges the state file, run.json and the ledger, over 16 tables
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kbutillib.domains.kbase.berdl import clearinghouse_backfill as cb
from kbutillib.domains.kbase.berdl import clearinghouse_ingest as ci
from kbutillib.domains.kbase.berdl import clearinghouse_manifest as cm
from kbutillib.domains.kbase.berdl import clearinghouse_schema as schema
from kbutillib.domains.kbase.berdl import clearinghouse_shard as cs

_PRESET = "file-example"


@pytest.fixture()
def state_root(tmp_path, monkeypatch):
    """Redirect every run's state directory under tmp_path."""
    root = tmp_path / "state"
    monkeypatch.setenv(ci.STATE_DIR_ENV, str(root))
    return root


@pytest.fixture()
def preset_path():
    presets = {preset.name: preset for preset in cb.available_presets()}
    assert _PRESET in presets, f"shipped presets: {sorted(presets)}"
    return presets[_PRESET].path


# --------------------------------------------------------------------------
# A fake ingest capability that keeps a REAL ledger
# --------------------------------------------------------------------------


class _FakeIngestCapability:
    """Records ingest/verify calls and writes a real ExecutionLedger line.

    The ledger is real because ``status`` merges it: a fake that returned row
    counts without writing the ledger would let a status bug through. The lake
    is not real -- ``verify_run`` reports what ``verify_ok`` says -- which is
    what makes a verification FAILURE testable at all.
    """

    def __init__(
        self,
        *,
        locus="in_pod",
        verify_ok=True,
        orphans=None,
        fail_after_shard_on=(),
    ):
        self._locus_value = locus
        self.verify_ok = verify_ok
        self._orphans = orphans or {"ok": True, "failures": []}
        self.ingest_calls: list[dict] = []
        self.verify_calls: list[dict] = []
        self.orphan_calls = 0
        #: Shard dirs whose ingest raises, to simulate a kill after sharding.
        self.fail_after_shard_on = set(fail_after_shard_on)

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
        run = cs.read_run_manifest(Path(shard_dir))
        if Path(shard_dir).name in self.fail_after_shard_on:
            raise RuntimeError(
                f"simulated kill during ingest of {shard_dir} (the shards are "
                "published; nothing is recorded done)"
            )
        ledger = ci.ExecutionLedger(ci.execution_state_dir(str(run_id)))
        tables = []
        for batch in run.get("batches", []):
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
            tables.append(
                {
                    "name": batch["table"],
                    "batch_id": batch["batch_id"],
                    "unit_id": batch.get("unit_id"),
                    "action": "ingest",
                    "rows_in": batch["rows"],
                    "rows_dropped_present": 0,
                    "rows_ingested": batch["rows"],
                }
            )
        return {
            "run_id": run_id,
            "execution_id": run_id,
            "source_run_id": run.get("run_id"),
            "namespace": f"{tenant}.{dataset}",
            "state_dir": str(ci.execution_state_dir(str(run_id))),
            "dry_run": dry_run,
            "unit": unit,
            "lock": {"namespace": f"{tenant}.{dataset}", "pid": 1},
            "tables": tables,
        }

    def verify_run(self, shard_dir, *, run_id, unit=None):
        self.verify_calls.append(
            {"shard_dir": str(shard_dir), "run_id": run_id, "unit": unit}
        )
        run = cs.read_run_manifest(Path(shard_dir))
        units = [
            {"unit_id": entry["unit_id"], "ok": self.verify_ok,
             "missing_parameter_sets": []}
            for entry in run.get("units", [])
        ]
        discrepancies = (
            []
            if self.verify_ok
            else [f"{shard_dir}: simulated unresolved batch (retryable)"]
        )
        return {
            "run_id": run_id,
            "execution_id": run_id,
            "namespace": "kbaseincubator.clearinghouse",
            "unit": unit,
            "ok": self.verify_ok,
            "batches": [],
            "units": units,
            "tables": [],
            "discrepancies": discrepancies,
            "parameter_set_integrity": dict(self._orphans),
        }

    def parameter_set_orphans(self, **kwargs):
        self.orphan_calls += 1
        return dict(self._orphans)


def _mongo_manifest(**overrides) -> cm.Manifest:
    """A one-source mongo manifest, parsed, with no connection made."""
    locator = {
        "uri": "mongodb://example:27017",
        "database": "db",
        "collection": "seq_protein",
        "sort_key": "_id",
        **overrides,
    }
    return cm.Manifest(
        sources=(
            cm.Source(
                name="mongo-protein",
                adapter="mongo",
                entity_type="protein",
                kinds=("entity",),
                locator=locator,
                hash_spec={"raw_column": "aa"},
                content={},
                result={},
            ),
        )
    )


# ==========================================================================
# Slice parsing and the key-range mapping
# ==========================================================================


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("00", ("00", "00")), ("00-0f", ("00", "0f")), ("ff", ("ff", "ff")),
     ("a3-a3", ("a3", "a3")), ("00-ff", ("00", "ff"))],
)
def test_parse_slice_accepts_a_prefix_and_an_inclusive_range(spec, expected):
    assert cs.parse_slice(spec) == expected


@pytest.mark.parametrize(
    "spec",
    [
        "0F",      # uppercase: the key space is lowercase hex
        "00-0F",
        "0",       # one digit
        "000",     # three digits
        "g0",      # not hex
        "00-",     # dangling range
        "-0f",
        "ff-00",   # backwards
        "0f-00",
        "",
        "00:0f",
    ],
)
def test_parse_slice_rejects_malformed_input(spec):
    with pytest.raises(ValueError):
        cs.parse_slice(spec)


def test_parse_slice_rejects_a_non_string():
    with pytest.raises(ValueError):
        cs.parse_slice(None)  # type: ignore[arg-type]


def test_slice_prefixes_defaults_to_all_256_units():
    """No --slice is a 256-UNIT run, not one unsliced read of the collection."""
    assert cs.slice_prefixes(None) == tuple(f"{i:02x}" for i in range(256))
    assert cs.slice_prefixes("00-0f") == tuple(f"{i:02x}" for i in range(16))
    assert cs.slice_prefixes("ff") == ("ff",)


def test_unit_key_range_is_half_open_and_omits_max_for_ff():
    assert cs.unit_key_range("00") == ("00", "01")
    assert cs.unit_key_range("fe") == ("fe", "ff")
    # "ff" has no successor prefix, so the range is OPEN at the top -- that is
    # what makes the 256 ranges cover the space exactly once.
    assert cs.unit_key_range("ff") == ("ff", None)
    with pytest.raises(ValueError):
        cs.unit_key_range("00-0f")


def test_the_256_unit_ranges_tile_the_key_space_exactly_once():
    ranges = [cs.unit_key_range(prefix) for prefix in cs.slice_prefixes(None)]
    for (_lo, hi), (next_lo, _next_hi) in zip(ranges, ranges[1:]):
        assert hi == next_lo, "adjacent ranges must abut with no gap or overlap"
    assert ranges[0][0] == "00"
    assert ranges[-1][1] is None


def test_slice_key_range_reports_the_whole_covered_range():
    assert cs.slice_key_range("00-0f") == ("00", "10")
    assert cs.slice_key_range("fe-ff") == ("fe", None)
    assert cs.slice_key_range(None) == (None, None)


def test_slice_maps_to_the_mongo_adapters_half_open_locator_range():
    manifest = _mongo_manifest()
    plan = cb.plan_units(manifest, slice_spec="07-08")
    assert plan.kind == cb.UNIT_KIND_SLICE
    assert plan.units == ("07", "08")

    first = cb.manifest_for_unit(manifest, plan, "07")
    assert first.sources[0].locator["min_key"] == "07"
    assert first.sources[0].locator["max_key"] == "08"
    adapter = cs.get_adapter(first.sources[0])
    assert adapter._query_filter() == {"_id": {"$gte": "07", "$lt": "08"}}


def test_the_ff_unit_omits_max_key_entirely():
    manifest = _mongo_manifest()
    plan = cb.plan_units(manifest, slice_spec="ff")
    rewritten = cb.manifest_for_unit(manifest, plan, "ff")
    assert rewritten.sources[0].locator["min_key"] == "ff"
    assert "max_key" not in rewritten.sources[0].locator
    adapter = cs.get_adapter(rewritten.sources[0])
    assert adapter._query_filter() == {"_id": {"$gte": "ff"}}


def test_a_units_range_overrides_a_manifests_own_narrower_range():
    """An explicit per-unit instruction WINS; it is not ANDed into the locator."""
    manifest = _mongo_manifest(min_key="aa", max_key="ab")
    plan = cb.plan_units(manifest, slice_spec="07")
    rewritten = cb.manifest_for_unit(manifest, plan, "07")
    assert rewritten.sources[0].locator["min_key"] == "07"
    assert rewritten.sources[0].locator["max_key"] == "08"

    # The adapter hook reaches the same conclusion without a manifest rewrite.
    adapter = cs.get_adapter(manifest.sources[0])
    assert adapter._query_filter() == {"_id": {"$gte": "aa", "$lt": "ab"}}
    adapter.restrict_to_unit("07")
    assert adapter._query_filter() == {"_id": {"$gte": "07", "$lt": "08"}}


def test_plan_units_rejects_a_malformed_slice_as_a_backfill_error():
    with pytest.raises(cb.BackfillError, match="not a valid slice"):
        cb.plan_units(_mongo_manifest(), slice_spec="0F")


# ==========================================================================
# The units() hook and non-mongo refusal of --slice
# ==========================================================================


def test_a_file_manifest_is_one_unit(preset_path):
    plan = cb.plan_units(cm.load_manifest(preset_path))
    assert plan.kind == cb.UNIT_KIND_SINGLE
    assert plan.units == (cs.SINGLE_UNIT,)


def test_non_mongo_adapters_refuse_slice_naming_the_adapter(preset_path):
    manifest = cm.load_manifest(preset_path)
    with pytest.raises(cb.BackfillError) as excinfo:
        cb.plan_units(manifest, slice_spec="00")
    message = str(excinfo.value)
    assert "'mongo'" in message
    assert "'file'" in message  # the offending adapter is NAMED


def test_the_units_hook_defaults_to_one_unit_and_refuses_another(preset_path):
    source = cm.load_manifest(preset_path).sources[0]
    adapter = cs.get_adapter(source)
    assert adapter.units() == (cs.SINGLE_UNIT,)
    assert adapter.unit() is None
    # Its own label, and None, are accepted and are no-ops.
    adapter.restrict_to_unit(cs.SINGLE_UNIT)
    adapter.restrict_to_unit(None)
    with pytest.raises(cs.ManifestSourceError) as excinfo:
        adapter.restrict_to_unit("00")
    assert "ONE unit" in str(excinfo.value)


def test_the_mongo_adapter_decomposes_into_256_units():
    adapter = cs.get_adapter(_mongo_manifest().sources[0])
    assert adapter.units() == tuple(f"{i:02x}" for i in range(256))


def test_an_adapter_with_its_own_units_drives_the_plan(monkeypatch, preset_path):
    """A units()-exposing adapter makes the plan ADAPTER-kind, and --limit then
    caps the NUMBER OF UNITS rather than records inside one."""
    labels = ("archive-a", "archive-b", "archive-c")
    monkeypatch.setattr(cs.FileSourceAdapter, "units", lambda self: labels)
    monkeypatch.setattr(
        cs.FileSourceAdapter, "restrict_to_unit", lambda self, unit: None
    )
    manifest = cm.load_manifest(preset_path)

    plan = cb.plan_units(manifest)
    assert plan.kind == cb.UNIT_KIND_ADAPTER
    assert plan.units == labels
    assert plan.units_dropped_by_limit == 0

    capped = cb.plan_units(manifest, limit=2)
    assert capped.units == labels[:2]
    assert capped.units_dropped_by_limit == 1
    # An adapter unit has NO record cap -- --limit already meant something.
    context = cb.BackfillRunContext(
        run_id="R", unit="archive-a", unit_kind=capped.kind,
        out_dir=Path("/tmp"), state_dir=Path("/tmp"), tenant="t",
        dataset="d", limit=2,
    )
    assert context.record_limit("any-source") is None


def test_get_adapter_binds_the_run_context_and_the_unit():
    context = cb.BackfillRunContext(
        run_id="R", unit="07", unit_kind=cb.UNIT_KIND_SLICE,
        out_dir=Path("/tmp"), state_dir=Path("/tmp"), tenant="t", dataset="d",
    )
    adapter = cs.get_adapter(
        _mongo_manifest().sources[0], run_context=context, unit="07"
    )
    assert adapter.run_context is context
    assert adapter.unit() == "07"


# ==========================================================================
# --limit semantics
# ==========================================================================


def test_limit_caps_records_per_reader_so_every_table_gets_the_sample(
    tmp_path, preset_path
):
    """A --limit sample must be COHERENT: the same first N records in every
    table, not N rows of the first table and none of the rest."""
    manifest = cm.load_manifest(preset_path)
    plan = cb.plan_units(manifest, limit=7)
    cb.shard_units(
        manifest=manifest, manifest_path=preset_path, plan=plan,
        out=tmp_path / "out", run_id="RUNLIMIT", limit=7, resume=False,
        tenant="t", dataset="d",
    )
    run = cs.read_run_manifest(cb.unit_dir_for(tmp_path / "out", cs.SINGLE_UNIT))
    emitted = {outcome["table"]: outcome["emitted"] for outcome in run["sources"]}
    assert set(emitted.values()) == {7}, emitted
    assert len(emitted) == 5  # five plans across the preset's three sources
    # None of them is exhausted, which is what makes the unit `partial`.
    assert all(outcome["exhausted"] is False for outcome in run["sources"])


def test_no_limit_drains_every_source_to_exhaustion(tmp_path, preset_path):
    manifest = cm.load_manifest(preset_path)
    plan = cb.plan_units(manifest)
    cb.shard_units(
        manifest=manifest, manifest_path=preset_path, plan=plan,
        out=tmp_path / "out", run_id="RUNFULL", limit=None, resume=False,
        tenant="t", dataset="d",
    )
    run = cs.read_run_manifest(cb.unit_dir_for(tmp_path / "out", cs.SINGLE_UNIT))
    assert all(outcome["exhausted"] is True for outcome in run["sources"])
    assert {outcome["emitted"] for outcome in run["sources"]} == {48}


def test_a_limit_above_the_source_size_still_reaches_exhaustion(
    tmp_path, preset_path
):
    """--limit is a CAP, not a target: a limit nobody hits is exhaustion, and
    exhaustion is `done`."""
    manifest = cm.load_manifest(preset_path)
    plan = cb.plan_units(manifest, limit=10_000)
    cb.shard_units(
        manifest=manifest, manifest_path=preset_path, plan=plan,
        out=tmp_path / "out", run_id="RUNBIG", limit=10_000, resume=False,
        tenant="t", dataset="d",
    )
    unit_dir = cb.unit_dir_for(tmp_path / "out", cs.SINGLE_UNIT)
    state, _reasons = cb.unit_outcome(
        unit_dir, verification={"discrepancies": []}, limit=10_000
    )
    assert state == cb.UNIT_DONE


def test_slice_unit_limits_are_per_unit_not_per_run():
    """Each slice gets its OWN --limit allowance: --slice 00-0f --limit 1000 is
    sixteen 1,000-document samples, not 1,000 from slice 00."""
    contexts = [
        cb.BackfillRunContext(
            run_id="R", unit=unit, unit_kind=cb.UNIT_KIND_SLICE,
            out_dir=Path("/tmp"), state_dir=Path("/tmp"), tenant="t",
            dataset="d", limit=1000,
        )
        for unit in ("00", "01")
    ]
    assert [c.record_limit("mongo-protein") for c in contexts] == [1000, 1000]


# ==========================================================================
# Manifest placeholder expansion and the [backfill] table
# ==========================================================================


_PLACEHOLDER_MANIFEST = """
[[source]]
name = "p"
adapter = "file"
path = "{path}"
format = "parquet"
entity_type = "gene"
kinds = ["entity"]

  [source.hash]
  raw_column = "dna_sequence"
"""


def _locator_path(path_value: str, env: dict | None = None) -> str:
    manifest = cm.load_manifest(
        _PLACEHOLDER_MANIFEST.format(path=path_value), env=env or {}
    )
    return manifest.sources[0].locator["path"]


def test_plain_var_expands_from_the_environment():
    assert _locator_path("${ROOT}/g.parquet", {"ROOT": "/data"}) == "/data/g.parquet"


def test_default_form_expands_when_the_variable_is_unset():
    assert _locator_path("${ROOT:-/fallback}/g.parquet", {}) == "/fallback/g.parquet"


def test_default_form_prefers_the_environment_when_set():
    assert (
        _locator_path("${ROOT:-/fallback}/g.parquet", {"ROOT": "/data"})
        == "/data/g.parquet"
    )


def test_an_empty_default_is_legal():
    assert _locator_path("${ROOT:-}/g.parquet", {}) == "/g.parquet"


def test_a_leading_tilde_is_expanded():
    expanded = _locator_path("~/clearinghouse/g.parquet", {})
    assert expanded == str(Path("~/clearinghouse/g.parquet").expanduser())
    assert not expanded.startswith("~")


def test_a_tilde_arriving_from_a_default_is_also_expanded():
    expanded = _locator_path("${ROOT:-~/box}/g.parquet", {})
    assert expanded == str(Path("~/box").expanduser()) + "/g.parquet"


def test_a_non_leading_tilde_is_left_alone():
    """A tilde is a legal filename character; only a LEADING one is a home ref."""
    assert _locator_path("/data/back~up/g.parquet", {}) == "/data/back~up/g.parquet"


def test_an_unresolved_variable_is_a_manifest_error_naming_it():
    with pytest.raises(cm.ManifestError) as excinfo:
        _locator_path("${ARC_ROOT}/g.parquet", {})
    message = str(excinfo.value)
    assert "ARC_ROOT" in message
    assert "'path'" in message  # the offending KEY is named too
    assert "'p'" in message     # and the source


def test_several_unresolved_variables_are_all_named():
    with pytest.raises(cm.ManifestError) as excinfo:
        _locator_path("${A}/${B}/g.parquet", {})
    assert "A, B" in str(excinfo.value)


def test_expansion_reaches_nested_locator_values():
    manifest = cm.load_manifest(
        """
[[source]]
name = "m"
adapter = "mongo"
uri = "mongodb://${HOST}:27017"
database = "db"
collection = "c"
entity_type = "protein"
kinds = ["entity"]
decompress = { sequence = "${SEQ_FIELD:-z_seq}" }
hosts = ["${HOST}", "static"]

  [source.hash]
  raw_column = "aa"
""",
        env={"HOST": "poplar"},
    )
    locator = manifest.sources[0].locator
    assert locator["uri"] == "mongodb://poplar:27017"
    assert locator["decompress"] == {"sequence": "z_seq"}
    assert locator["hosts"] == ["poplar", "static"]


def test_the_mapping_half_is_never_expanded():
    """A @const(${VAR}) would make one manifest write different partition
    values on different machines, which is the opposite of the point."""
    manifest = cm.load_manifest(
        """
[[source]]
name = "r"
adapter = "file"
path = "/x.parquet"
format = "parquet"
entity_type = "protein"
kinds = ["result"]

  [source.hash]
  raw_column = "aa"

  [source.result]
  result_type = "@const(annotation)"
  source = "@const(${TOOL})"
  result_type_version = "@const(1.0)"
  payload = "@json(product)"
  parameter_set = {}
""",
        env={"TOOL": "bakta"},
    )
    assert manifest.sources[0].result["source"] == "@const(${TOOL})"


def test_manifest_dir_resolves_to_the_manifest_files_own_directory(
    tmp_path, preset_path
):
    manifest_file = tmp_path / "nested" / "m.toml"
    manifest_file.parent.mkdir()
    manifest_file.write_text(
        _PLACEHOLDER_MANIFEST.format(path="${MANIFEST_DIR}/g.parquet"),
        encoding="utf-8",
    )
    manifest = cm.load_manifest(manifest_file, env={})
    assert manifest.sources[0].locator["path"] == str(
        manifest_file.parent.resolve() / "g.parquet"
    )


def test_manifest_dir_cannot_be_overridden_by_the_environment(tmp_path):
    manifest_file = tmp_path / "m.toml"
    manifest_file.write_text(
        _PLACEHOLDER_MANIFEST.format(path="${MANIFEST_DIR}/g.parquet"),
        encoding="utf-8",
    )
    manifest = cm.load_manifest(manifest_file, env={"MANIFEST_DIR": "/hijacked"})
    assert "/hijacked" not in manifest.sources[0].locator["path"]


def test_manifest_dir_is_unavailable_for_a_manifest_parsed_from_text():
    with pytest.raises(cm.ManifestError, match="MANIFEST_DIR"):
        _locator_path("${MANIFEST_DIR}/g.parquet", {})


def test_the_shipped_preset_points_at_its_own_shipped_fixture(preset_path):
    manifest = cm.load_manifest(preset_path)
    for source in manifest.sources:
        resolved = Path(source.locator["path"])
        assert resolved.is_file(), resolved
        assert resolved.parent == preset_path.parent


def test_the_backfill_table_is_parsed_and_is_not_a_locator():
    manifest = cm.load_manifest(
        """
[backfill]
pulls_from = ["mongo://poplar:27017/db.c"]
needs_env = ["ARC_ROOT", "KBU_TOKEN"]

[[source]]
name = "p"
adapter = "file"
path = "/x.parquet"
format = "parquet"
entity_type = "gene"
kinds = ["entity"]

  [source.hash]
  raw_column = "dna_sequence"
""",
        env={},
    )
    assert manifest.backfill is not None
    assert manifest.backfill.pulls_from == ("mongo://poplar:27017/db.c",)
    assert manifest.backfill.needs_env == ("ARC_ROOT", "KBU_TOKEN")
    # It is top-level, so it was never a candidate locator key -- and the
    # source's locator is untouched by it.
    assert set(manifest.sources[0].locator) == {"path", "format"}


def test_a_manifest_without_a_backfill_table_has_none():
    manifest = cm.load_manifest(
        _PLACEHOLDER_MANIFEST.format(path="/x.parquet"), env={}
    )
    assert manifest.backfill is None


@pytest.mark.parametrize(
    "table",
    [
        'pulls_from = "not-a-list"',
        "needs_env = [1, 2]",
        'needs_env = [""]',
        'unknown_key = ["x"]',
    ],
)
def test_a_malformed_backfill_table_is_refused(table):
    with pytest.raises(cm.ManifestError, match=r"\[backfill\]"):
        cm.load_manifest(
            "[backfill]\n"
            + table
            + "\n"
            + _PLACEHOLDER_MANIFEST.format(path="/x.parquet"),
            env={},
        )


# ==========================================================================
# Presets
# ==========================================================================


def test_the_file_example_preset_is_shipped_and_describable():
    rows = {row["name"]: row for row in map(cb.describe_preset, cb.available_presets())}
    assert _PRESET in rows
    row = rows[_PRESET]
    assert "error" not in row
    assert row["adapters"] == ["file"]
    # The registry table is reported FIRST, because the sharder writes it
    # before any result batch.
    assert row["tables"][0] == schema.PARAMETER_SET_TABLE
    assert "protein_result" in row["tables"]
    assert row["needs_env"] == ["KBU_BACKFILL_EXAMPLE"]
    assert row["pulls_from"]


def test_every_target_table_a_preset_names_is_a_real_clearinghouse_table():
    known = {config["name"] for config in schema.table_configs()}
    for preset in cb.available_presets():
        for table in cb.describe_preset(preset)["tables"]:
            assert table in known, (preset.name, table)


def test_resolve_manifest_path_takes_a_preset_name_or_a_path(tmp_path, preset_path):
    assert cb.resolve_manifest_path(_PRESET) == (preset_path, _PRESET)
    explicit = tmp_path / "m.toml"
    explicit.write_text("x", encoding="utf-8")
    assert cb.resolve_manifest_path(str(explicit)) == (explicit, None)
    with pytest.raises(cb.BackfillError, match="neither a shipped"):
        cb.resolve_manifest_path("no-such-preset")


# ==========================================================================
# The run, unit by unit
# ==========================================================================


def _drive(
    cap,
    *,
    preset_path,
    out,
    run_id,
    limit=None,
    resume=False,
    dataset="clearinghouse",
    retry_failed_archives=False,
):
    manifest = cm.load_manifest(preset_path)
    plan = cb.plan_units(manifest, limit=limit)
    cb.register_run(
        run_id=run_id, out=out, tenant="kbaseincubator", dataset=dataset,
        manifest=manifest, manifest_path=preset_path, slice_spec=None,
        limit=limit, preset=_PRESET, unit_kind=plan.kind, resume=resume,
    )
    Path(out).mkdir(parents=True, exist_ok=True)
    cb.record_planned_units(out, run_id=run_id, units=plan.units)
    return cb.run_backfill(
        cap, manifest=manifest, manifest_path=preset_path, plan=plan, out=out,
        run_id=run_id, limit=limit, resume=resume, tenant="kbaseincubator",
        dataset=dataset, retry_failed_archives=retry_failed_archives,
    )


def test_a_unit_is_sharded_ingested_verified_then_recorded_done(
    tmp_path, preset_path, state_root
):
    cap = _FakeIngestCapability()
    out = tmp_path / "out"
    result = _drive(cap, preset_path=preset_path, out=out, run_id="R1")

    assert [u["state"] for u in result["units"]] == [cb.UNIT_DONE]
    # The ORDER is the contract: ingest, then verify, then the record.
    assert len(cap.ingest_calls) == 1
    assert len(cap.verify_calls) == 1
    assert cap.verify_calls[0]["run_id"] == cap.ingest_calls[0]["run_id"]
    # The target was passed through, not defaulted inside the driver.
    assert cap.ingest_calls[0]["dataset"] == "clearinghouse"
    assert cap.ingest_calls[0]["tenant"] == "kbaseincubator"

    recorded = cb.unit_record(cb.read_state(out), cs.SINGLE_UNIT)
    assert recorded["state"] == cb.UNIT_DONE
    assert recorded["execution_id"]
    assert recorded["recorded_at"]


def test_units_are_processed_in_plan_order(tmp_path, preset_path, state_root):
    """Each unit is finished before the next starts, so a kill loses one unit."""
    manifest = cm.load_manifest(preset_path)
    plan = cb.UnitPlan(kind=cb.UNIT_KIND_ADAPTER, units=("u1", "u2", "u3"))
    out = tmp_path / "out"
    seen: list[str] = []

    def fake_shard(unit_manifest, unit_dir, **kwargs):
        seen.append(Path(unit_dir).name)
        return cs.shard_manifest(
            unit_manifest, unit_dir, **{**kwargs, "adapter_unit": None}
        )

    cap = _FakeIngestCapability()
    result = cb.run_backfill(
        cap, manifest=manifest, manifest_path=preset_path, plan=plan, out=out,
        run_id="RORDER", limit=None, resume=False, tenant="kbaseincubator",
        dataset="clearinghouse", shard_fn=fake_shard,
    )
    assert seen == ["u1", "u2", "u3"]
    assert [u["unit"] for u in result["units"]] == ["u1", "u2", "u3"]
    assert all(u["state"] == cb.UNIT_DONE for u in result["units"])
    # Every unit has its OWN execution id, hence its own ledger.
    ids = {u["execution_id"] for u in result["units"]}
    assert len(ids) == 3


def test_a_verification_failure_is_incomplete_and_is_not_recorded_done(
    tmp_path, preset_path, state_root
):
    cap = _FakeIngestCapability(verify_ok=False)
    out = tmp_path / "out"
    result = _drive(cap, preset_path=preset_path, out=out, run_id="RBAD")

    assert [u["state"] for u in result["units"]] == [cb.UNIT_INCOMPLETE]
    assert cb.exit_states(result["units"]) == [cs.SINGLE_UNIT]
    recorded = cb.unit_record(cb.read_state(out), cs.SINGLE_UNIT)
    assert recorded["state"] == cb.UNIT_INCOMPLETE
    assert recorded["reasons"]


def test_an_incomplete_unit_is_retried_on_resume_and_can_then_complete(
    tmp_path, preset_path, state_root
):
    out = tmp_path / "out"
    failing = _FakeIngestCapability(verify_ok=False)
    first = _drive(failing, preset_path=preset_path, out=out, run_id="RRETRY")
    assert first["units"][0]["state"] == cb.UNIT_INCOMPLETE

    healthy = _FakeIngestCapability(verify_ok=True)
    second = _drive(
        healthy, preset_path=preset_path, out=out, run_id="RRETRY", resume=True
    )
    assert second["units"][0]["state"] == cb.UNIT_DONE
    assert cb.exit_states(second["units"]) == []
    # The retry REUSED the published shards rather than regenerating them.
    assert second["units"][0]["action"] == "reused-shards"
    # And it kept the same execution id, so the ledger continued.
    assert (
        second["units"][0]["execution_id"] == first["units"][0]["execution_id"]
    )


def test_partial_exits_zero_while_incomplete_exits_nonzero():
    """--limit is an ASKED-FOR outcome; `incomplete` is not."""
    assert cb.exit_states([{"unit": "a", "state": cb.UNIT_PARTIAL}]) == []
    assert cb.exit_states([{"unit": "a", "state": cb.UNIT_DONE}]) == []
    assert cb.exit_states([{"unit": "a", "state": cb.UNIT_INCOMPLETE}]) == ["a"]


def test_a_limit_stopped_unit_is_partial_and_a_later_full_run_redoes_it(
    tmp_path, preset_path, state_root
):
    out = tmp_path / "out"
    cap = _FakeIngestCapability()
    limited = _drive(cap, preset_path=preset_path, out=out, run_id="RLIM", limit=7)
    assert limited["units"][0]["state"] == cb.UNIT_PARTIAL
    assert cb.unit_record(cb.read_state(out), cs.SINGLE_UNIT)["state"] == (
        cb.UNIT_PARTIAL
    )

    # A later run WITHOUT --limit processes the partial unit again (a `done`
    # unit would have been skipped) and finishes it.
    full_out = tmp_path / "out-full"
    full = _drive(cap, preset_path=preset_path, out=full_out, run_id="RLIM2")
    assert full["units"][0]["state"] == cb.UNIT_DONE

    # ... and a --resume of the LIMITED run also reprocesses it, because
    # `partial` is not `done`.
    resumed = _drive(
        cap, preset_path=preset_path, out=out, run_id="RLIM", resume=True
    )
    assert resumed["units"][0]["action"] != "skipped-done"


def test_a_done_unit_is_skipped_on_resume(tmp_path, preset_path, state_root):
    out = tmp_path / "out"
    cap = _FakeIngestCapability()
    _drive(cap, preset_path=preset_path, out=out, run_id="RSKIP")
    ingests_before = len(cap.ingest_calls)

    again = _drive(
        cap, preset_path=preset_path, out=out, run_id="RSKIP", resume=True
    )
    assert again["units"][0]["action"] == "skipped-done"
    assert len(cap.ingest_calls) == ingests_before  # nothing re-ingested


# ==========================================================================
# A run killed mid-unit resumes that unit EXACTLY ONCE
# ==========================================================================


def test_a_run_killed_after_shard_resumes_that_unit_exactly_once(
    tmp_path, preset_path, state_root
):
    out = tmp_path / "out"
    plan = cb.UnitPlan(kind=cb.UNIT_KIND_ADAPTER, units=("u1", "u2"))
    manifest = cm.load_manifest(preset_path)

    def fake_shard(unit_manifest, unit_dir, **kwargs):
        return cs.shard_manifest(
            unit_manifest, unit_dir, **{**kwargs, "adapter_unit": None}
        )

    # u1 lands; the kill happens in u2's ingest, AFTER its shards published.
    killed = _FakeIngestCapability(fail_after_shard_on={"u2"})
    with pytest.raises(RuntimeError, match="simulated kill"):
        cb.run_backfill(
            killed, manifest=manifest, manifest_path=preset_path, plan=plan,
            out=out, run_id="RKILL", limit=None, resume=False,
            tenant="kbaseincubator", dataset="clearinghouse",
            shard_fn=fake_shard,
        )
    state = cb.read_state(out)
    assert cb.unit_record(state, "u1")["state"] == cb.UNIT_DONE
    # u2 is NOT done -- it was never verified -- but its execution id survived
    # the kill, which is what lets the resume continue its ledger.
    u2 = cb.unit_record(state, "u2")
    assert u2["state"] != cb.UNIT_DONE
    killed_execution_id = u2["execution_id"]
    assert killed_execution_id

    published_before = cb.published_files(cb.unit_dir_for(out, "u2"))
    assert published_before

    resumed_cap = _FakeIngestCapability()
    result = cb.run_backfill(
        resumed_cap, manifest=manifest, manifest_path=preset_path, plan=plan,
        out=out, run_id="RKILL", limit=None, resume=True,
        tenant="kbaseincubator", dataset="clearinghouse", shard_fn=fake_shard,
    )
    # u1 was skipped; u2 was done EXACTLY ONCE.
    assert [u["action"] for u in result["units"]] == [
        "skipped-done",
        "reused-shards",
    ]
    assert [u["unit"] for u in result["units"]] == ["u1", "u2"]
    assert [c["shard_dir"] for c in resumed_cap.ingest_calls] == [
        str(cb.unit_dir_for(out, "u2"))
    ]
    assert cb.unit_record(cb.read_state(out), "u2")["state"] == cb.UNIT_DONE
    # The resume continued the SAME execution, and asked ingest to reconcile.
    assert resumed_cap.ingest_calls[0]["run_id"] == killed_execution_id
    assert resumed_cap.ingest_calls[0]["reconcile"] is True
    # NOT ONE PUBLISHED BATCH WAS DELETED.
    assert cb.published_files(cb.unit_dir_for(out, "u2")) == published_before
    for path in published_before:
        assert path.is_file()


def test_resume_deletes_unpublished_files_and_keeps_published_ones(
    tmp_path, preset_path
):
    manifest = cm.load_manifest(preset_path)
    plan = cb.plan_units(manifest)
    out = tmp_path / "out"
    cb.shard_units(
        manifest=manifest, manifest_path=preset_path, plan=plan, out=out,
        run_id="RCLEAN", limit=None, resume=False, tenant="t", dataset="d",
    )
    unit_dir = cb.unit_dir_for(out, cs.SINGLE_UNIT)
    published = cb.published_files(unit_dir)
    assert published

    # A kill mid-shard leaves a batch file no inventory names.
    stray = unit_dir / "protein_entity" / "batch-9999.parquet"
    stray.write_bytes(b"truncated-garbage")

    deleted = cb.delete_unpublished(unit_dir)
    assert deleted == ["protein_entity/batch-9999.parquet"]
    assert not stray.exists()
    for path in published:
        assert path.is_file()
    assert cb.published_files(unit_dir) == published


def test_shard_resume_reuses_a_published_unit_without_resharding(
    tmp_path, preset_path
):
    manifest = cm.load_manifest(preset_path)
    plan = cb.plan_units(manifest)
    out = tmp_path / "out"
    first = cb.shard_units(
        manifest=manifest, manifest_path=preset_path, plan=plan, out=out,
        run_id="RS1", limit=None, resume=False, tenant="t", dataset="d",
    )
    assert first[0]["action"] == "sharded"

    again = cb.shard_units(
        manifest=manifest, manifest_path=preset_path, plan=plan, out=out,
        run_id="RS1", limit=None, resume=True, tenant="t", dataset="d",
    )
    assert again[0]["action"] == "reused"
    assert again[0]["deleted"] == []


def test_sharding_into_a_published_unit_without_resume_is_refused(
    tmp_path, preset_path
):
    manifest = cm.load_manifest(preset_path)
    plan = cb.plan_units(manifest)
    out = tmp_path / "out"
    cb.shard_units(
        manifest=manifest, manifest_path=preset_path, plan=plan, out=out,
        run_id="RS2", limit=None, resume=False, tenant="t", dataset="d",
    )
    with pytest.raises(cb.BackfillError, match="already holds a published"):
        cb.shard_units(
            manifest=manifest, manifest_path=preset_path, plan=plan, out=out,
            run_id="RS2", limit=None, resume=False, tenant="t", dataset="d",
        )


# ==========================================================================
# Immutable run identity
# ==========================================================================


def _register(preset_path, out, **overrides):
    manifest = cm.load_manifest(preset_path)
    kwargs = {
        "run_id": "RID",
        "out": out,
        "tenant": "kbaseincubator",
        "dataset": "clearinghouse",
        "manifest": manifest,
        "manifest_path": preset_path,
        "slice_spec": None,
        "limit": None,
        "preset": _PRESET,
        "unit_kind": cb.UNIT_KIND_SINGLE,
        "resume": False,
    }
    kwargs.update(overrides)
    return cb.register_run(**kwargs)


def test_a_run_registration_records_the_whole_identity(
    tmp_path, preset_path, state_root
):
    out = tmp_path / "out"
    registration = _register(preset_path, out, limit=12, slice_spec=None)
    assert registration["out"] == str(out.resolve())
    assert registration["tenant"] == "kbaseincubator"
    assert registration["dataset"] == "clearinghouse"
    assert len(registration["manifest_digest"]) == 64
    assert registration["standardizer_version"]
    assert registration["limit"] == 12
    assert registration["slice"] is None
    assert registration["preset"] == _PRESET
    names = [source["name"] for source in registration["sources"]]
    assert names == ["file-example-protein", "file-example-result",
                     "file-example-function"]
    # It is persisted in the STATE directory, beside the ledger -- not in --out.
    assert cb.registration_path("RID").is_file()
    assert cb.registration_path("RID").parent == ci.execution_state_dir("RID")


def test_source_identities_exclude_the_per_unit_range_keys():
    manifest = _mongo_manifest(min_key="07", max_key="08", start_after="07ab")
    identity = cb.source_identities(manifest)[0]
    assert set(identity["locator"]) == {"uri", "database", "collection", "sort_key"}


def test_resume_of_an_unregistered_run_is_refused(tmp_path, preset_path, state_root):
    with pytest.raises(cb.BackfillError, match="no registration"):
        _register(preset_path, tmp_path / "out", resume=True)


def test_resume_refuses_a_different_dataset_so_scratch_never_meets_production(
    tmp_path, preset_path, state_root
):
    out = tmp_path / "out"
    _register(preset_path, out, dataset="clearinghouse_scratch")
    with pytest.raises(cb.BackfillError) as excinfo:
        _register(preset_path, out, dataset="clearinghouse", resume=True)
    message = str(excinfo.value)
    assert "dataset" in message
    assert "clearinghouse_scratch" in message  # the registered value is NAMED


@pytest.mark.parametrize(
    ("field", "value"),
    [("out", "elsewhere"), ("slice", "00"), ("tenant", "someone_else")],
)
def test_resume_refuses_any_conflicting_identity_field(
    tmp_path, preset_path, state_root, field, value
):
    out = tmp_path / "out"
    _register(preset_path, out)
    overrides = {"resume": True, "out": out}
    if field == "out":
        overrides["out"] = tmp_path / value
    elif field == "slice":
        overrides["slice_spec"] = value
    else:
        overrides[field] = value
    with pytest.raises(cb.BackfillError, match="CONFLICTS"):
        _register(preset_path, **overrides)


def test_resume_refuses_a_changed_manifest_digest(tmp_path, preset_path, state_root):
    out = tmp_path / "out"
    _register(preset_path, out)
    altered = tmp_path / "altered.toml"
    altered.write_text(
        preset_path.read_text(encoding="utf-8") + "\n# a changed byte\n",
        encoding="utf-8",
    )
    with pytest.raises(cb.BackfillError, match="manifest_digest"):
        _register(preset_path, out, manifest_path=altered, resume=True)


def test_resume_accepts_a_changed_limit_and_keeps_its_history(
    tmp_path, preset_path, state_root
):
    """Dropping --limit is how a partial unit gets finished, so it is the
    intended workflow rather than a conflict."""
    out = tmp_path / "out"
    _register(preset_path, out, limit=7)
    merged = _register(preset_path, out, limit=None, resume=True)
    assert merged["limit"] is None
    assert merged["limit_history"] == [7, None]


def test_status_finds_a_run_from_its_out_path(tmp_path, preset_path, state_root):
    out = tmp_path / "out"
    _register(preset_path, out)
    out.mkdir(parents=True, exist_ok=True)
    # Route 1: the state file at the root of --out.
    cb.record_planned_units(out, run_id="RID", units=("all",))
    assert cb.find_run_for_out(out) == "RID"
    # Route 2: the registration, after the state file is gone.
    cb.state_path(out).unlink()
    assert cb.find_run_for_out(out) == "RID"
    assert cb.find_run_for_out(tmp_path / "nowhere") is None


# ==========================================================================
# The run context: durable state and the retry flag
# ==========================================================================


def _context(tmp_path, **overrides):
    kwargs = {
        "run_id": "RCTX",
        "unit": "07",
        "unit_kind": cb.UNIT_KIND_SLICE,
        "out_dir": tmp_path / "out",
        "state_dir": tmp_path / "state" / "RCTX",
        "tenant": "kbaseincubator",
        "dataset": "clearinghouse",
    }
    kwargs.update(overrides)
    return cb.BackfillRunContext(**kwargs)


def test_durable_adapter_state_round_trips_and_survives_a_new_context(tmp_path):
    first = _context(tmp_path)
    assert first.get_state("mongo-protein") == {}
    first.put_state("mongo-protein", {"hash_job_id": "job-123", "watermark": "07ff"})

    # A NEW context for the same (run, unit) -- i.e. the process restarted --
    # reads it back. This is the whole point: the genome-archive adapter must
    # not resubmit a hash job it already submitted.
    second = _context(tmp_path)
    assert second.get_state("mongo-protein") == {
        "hash_job_id": "job-123",
        "watermark": "07ff",
    }


def test_durable_state_is_keyed_by_both_source_and_unit(tmp_path):
    unit_07 = _context(tmp_path, unit="07")
    unit_08 = _context(tmp_path, unit="08")
    unit_07.put_state("s1", {"v": "07-s1"})
    unit_07.put_state("s2", {"v": "07-s2"})
    unit_08.put_state("s1", {"v": "08-s1"})
    assert unit_07.get_state("s1") == {"v": "07-s1"}
    assert unit_07.get_state("s2") == {"v": "07-s2"}
    assert unit_08.get_state("s1") == {"v": "08-s1"}
    assert unit_08.get_state("s2") == {}


def test_durable_state_tolerates_an_unreadable_file(tmp_path):
    context = _context(tmp_path)
    path = context.state_file("s1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert context.get_state("s1") == {}


def test_durable_state_file_names_are_path_safe(tmp_path):
    context = _context(tmp_path, unit="../escape")
    path = context.put_state("a/b source", {"v": 1})
    assert ".." not in path.name
    assert "/" not in path.name
    assert path.parent == context.state_dir / "adapter-state"


def test_retry_failed_archives_reaches_the_adapter_through_the_run_context(
    tmp_path, preset_path, state_root
):
    """The flag is carried on the context the adapters are bound to -- it is
    never read from the environment or a global."""
    seen: list[bool] = []

    class _Recording(cs.FileSourceAdapter):
        def iter_records(self):
            seen.append(bool(getattr(self.run_context, "retry_failed_archives")))
            yield from super().iter_records()

    manifest = cm.load_manifest(preset_path)
    plan = cb.plan_units(manifest)
    original = dict(cs._ADAPTER_CLASSES)
    cs._ADAPTER_CLASSES["file"] = _Recording
    try:
        cb.shard_units(
            manifest=manifest, manifest_path=preset_path, plan=plan,
            out=tmp_path / "out", run_id="RFLAG", limit=None, resume=False,
            tenant="t", dataset="d", retry_failed_archives=True,
        )
    finally:
        cs._ADAPTER_CLASSES.clear()
        cs._ADAPTER_CLASSES.update(original)
    assert seen and all(seen), seen


def test_the_run_context_is_bound_to_every_adapter_the_sharder_builds(
    tmp_path, preset_path
):
    bound: list[object] = []

    class _Recording(cs.FileSourceAdapter):
        def iter_records(self):
            bound.append(self.run_context)
            yield from super().iter_records()

    manifest = cm.load_manifest(preset_path)
    plan = cb.plan_units(manifest)
    original = dict(cs._ADAPTER_CLASSES)
    cs._ADAPTER_CLASSES["file"] = _Recording
    try:
        cb.shard_units(
            manifest=manifest, manifest_path=preset_path, plan=plan,
            out=tmp_path / "out", run_id="RBIND", limit=None, resume=False,
            tenant="t", dataset="d",
        )
    finally:
        cs._ADAPTER_CLASSES.clear()
        cs._ADAPTER_CLASSES.update(original)
    assert bound and all(isinstance(c, cb.BackfillRunContext) for c in bound)
    assert {c.run_id for c in bound} == {"RBIND"}


def test_a_plain_shard_run_has_no_context_and_is_unlimited(tmp_path, preset_path):
    """`kbu clearinghouse shard` binds no context; everything degrades to
    "unlimited and nothing persisted" rather than requiring one."""
    manifest = cm.load_manifest(preset_path)
    cs.shard_manifest(manifest, tmp_path / "plain", manifest_path=preset_path)
    run = cs.read_run_manifest(tmp_path / "plain")
    assert all(outcome["exhausted"] is True for outcome in run["sources"])


# ==========================================================================
# status
# ==========================================================================


def test_status_merges_the_state_file_run_json_and_the_ledger(
    tmp_path, preset_path, state_root
):
    out = tmp_path / "out"
    cap = _FakeIngestCapability()
    _drive(cap, preset_path=preset_path, out=out, run_id="RSTAT")

    report = cb.status_report(run_id="RSTAT", out=out)
    assert report["run_id"] == "RSTAT"
    assert report["totals"][cb.UNIT_DONE] == 1
    assert report["pending_units"] == []
    assert report["registration"]["preset"] == _PRESET

    (unit,) = report["units"]
    assert unit["unit"] == cs.SINGLE_UNIT
    assert unit["state"] == cb.UNIT_DONE
    # from run.json. The total is read from the inventory rather than written
    # as 48 x 5: the two FUNCTION tables deduplicate (48 rows carry only ten
    # distinct `product` strings, hence ten function identities), which is the
    # sharder's first-row rule doing its job.
    assert unit["batches"] == 6
    run = cs.read_run_manifest(cb.unit_dir_for(out, cs.SINGLE_UNIT))
    by_table = {batch["table"]: batch["rows"] for batch in run["batches"]}
    assert by_table["protein_entity"] == 48
    assert by_table["function_entity"] == 10
    assert by_table[schema.PARAMETER_SET_TABLE] == 1
    assert unit["rows_sharded"] == sum(by_table.values())
    assert unit["shard_run_id"] == "RSTAT"
    # from the ledger
    assert unit["rows_ingested"] == unit["rows_sharded"]

    # ALL SIXTEEN TABLES are reported, the registry included.
    assert len(report["tables"]) == 16
    by_name = {row["name"]: row for row in report["tables"]}
    assert set(by_name) == {c["name"] for c in schema.table_configs()}
    assert by_name[schema.PARAMETER_SET_TABLE]["is_registry"] is True
    assert by_name[schema.PARAMETER_SET_TABLE]["rows_ingested"] == 1
    assert by_name["protein_entity"]["rows_ingested"] == 48
    assert by_name["protein_result"]["rows_ingested"] == 48
    assert by_name["function_entity"]["rows_ingested"] == 10
    # A table this manifest does not write is reported as zero, not omitted.
    assert by_name["genome_entity"]["rows_ingested"] == 0
    assert report["unexpected_tables"] == []


def test_status_reports_registry_rows_per_run_alongside_the_other_tables(
    tmp_path, preset_path, state_root
):
    out = tmp_path / "out"
    _drive(_FakeIngestCapability(), preset_path=preset_path, out=out, run_id="RREG")
    report = cb.status_report(run_id="RREG", out=out)
    registry = next(
        row for row in report["tables"] if row["name"] == schema.PARAMETER_SET_TABLE
    )
    assert registry["rows_ingested"] >= 1
    assert registry["batches"] == 1


def test_status_reports_the_adapter_watermark_and_the_source_outcomes(
    tmp_path, preset_path, state_root
):
    out = tmp_path / "out"
    _drive(_FakeIngestCapability(), preset_path=preset_path, out=out, run_id="RWM")
    # An adapter persisted a watermark through the run context.
    context = cb.BackfillRunContext(
        run_id="RWM", unit=cs.SINGLE_UNIT, unit_kind=cb.UNIT_KIND_SINGLE,
        out_dir=out, state_dir=ci.execution_state_dir("RWM"),
        tenant="t", dataset="d",
    )
    context.put_state("file-example-protein", {"watermark": "fixture-protein-047"})

    report = cb.status_report(run_id="RWM", out=out)
    marks = report["watermarks"]
    # The per-source exhaustion the inventory recorded ...
    assert marks[f"{cs.SINGLE_UNIT}::file-example-protein"]["exhausted"] is True
    # ... and whatever the adapter chose to persist, reported as-is.
    persisted = [
        value
        for value in marks.values()
        if value.get("adapter_state", {}).get("watermark")
    ]
    assert persisted and persisted[0]["adapter_state"]["watermark"] == (
        "fixture-protein-047"
    )


def test_status_reports_planned_units_that_have_not_started(
    tmp_path, preset_path, state_root
):
    out = tmp_path / "out"
    out.mkdir(parents=True)
    cb.record_planned_units(out, run_id="RPEND", units=("00", "01", "02"))
    report = cb.status_report(run_id="RPEND", out=out)
    assert report["pending_units"] == ["00", "01", "02"]
    assert report["totals"][cb.UNIT_PENDING] == 3
    assert report["units"] == []


def test_status_reports_skips_by_reason(tmp_path, preset_path, state_root):
    out = tmp_path / "out"
    _drive(_FakeIngestCapability(), preset_path=preset_path, out=out, run_id="RSKIPS")
    # The sharder records per-reason skip counts in run.json; inject one so the
    # merge is exercised against the real shape.
    unit_dir = cb.unit_dir_for(out, cs.SINGLE_UNIT)
    run_path = cs.run_manifest_path(unit_dir)
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["skipped"] = [
        {"source": "file-example-protein", "reason": "null-observed_at", "count": 3},
        {"source": "file-example-result", "reason": "null-observed_at", "count": 2},
        {"source": "file-example-result", "reason": "shard-value-error", "count": 1},
    ]
    run_path.write_text(json.dumps(run), encoding="utf-8")

    report = cb.status_report(run_id="RSKIPS", out=out)
    assert report["skipped"] == [
        {"reason": "null-observed_at", "count": 5},
        {"reason": "shard-value-error", "count": 1},
    ]


def test_status_reports_a_partial_unit_with_its_reason(
    tmp_path, preset_path, state_root
):
    out = tmp_path / "out"
    _drive(
        _FakeIngestCapability(), preset_path=preset_path, out=out,
        run_id="RSTATLIM", limit=5,
    )
    report = cb.status_report(run_id="RSTATLIM", out=out)
    assert report["totals"][cb.UNIT_PARTIAL] == 1
    (unit,) = report["units"]
    assert unit["state"] == cb.UNIT_PARTIAL
    assert any("--limit 5" in reason for reason in unit["reasons"])


def test_status_flags_a_ledger_table_outside_the_schema(
    tmp_path, preset_path, state_root
):
    out = tmp_path / "out"
    _drive(_FakeIngestCapability(), preset_path=preset_path, out=out, run_id="RODD")
    execution_id = cb.unit_record(cb.read_state(out), cs.SINGLE_UNIT)["execution_id"]
    ci.ExecutionLedger(ci.execution_state_dir(execution_id)).append(
        {
            "batch_id": "01ODDBATCH0000000000000000",
            "table": "not_a_clearinghouse_table",
            "state": ci.LEDGER_COMMITTED,
            "rows_ingested": 9,
        }
    )
    report = cb.status_report(run_id="RODD", out=out)
    assert report["unexpected_tables"] == ["not_a_clearinghouse_table"]
    # ... and it was NOT folded into any table's total: the schema tables add
    # up to exactly what the unit sharded, with the odd 9 rows excluded.
    expected = sum(
        batch["rows"]
        for batch in cs.read_run_manifest(
            cb.unit_dir_for(out, cs.SINGLE_UNIT)
        )["batches"]
    )
    assert sum(row["rows_ingested"] for row in report["tables"]) == expected


# ==========================================================================
# The state file itself
# ==========================================================================


def test_the_state_file_is_rewritten_atomically_and_keeps_unit_order(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    for unit in ("02", "00", "01"):
        cb.record_unit(out, run_id="RORD", unit=unit, entry={"state": cb.UNIT_DONE})
    state = cb.read_state(out)
    assert state["unit_order"] == ["02", "00", "01"]
    assert set(state["units"]) == {"00", "01", "02"}
    assert state["run_id"] == "RORD"
    # No temp file was left behind.
    assert [p.name for p in out.iterdir()] == [cb.BACKFILL_STATE_FILE]


def test_an_unreadable_state_file_is_refused_rather_than_ignored(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    cb.state_path(out).write_text("{truncated", encoding="utf-8")
    with pytest.raises(cb.BackfillError, match="not readable as JSON"):
        cb.read_state(out)


def test_unit_record_of_an_unknown_unit_is_pending(tmp_path):
    assert cb.unit_record({}, "00") == {"state": cb.UNIT_PENDING}


def test_unit_dir_names_are_path_safe(tmp_path):
    assert cb.unit_dir_for(tmp_path, "../../etc").parent == tmp_path / "units"
    assert ".." not in cb.unit_dir_for(tmp_path, "../../etc").name


def test_resolve_out_defaults_under_the_run_id(tmp_path):
    assert cb.resolve_out(None, "RID") == (
        Path(cb.DEFAULT_OUT_ROOT).expanduser() / "RID"
    )
    assert cb.resolve_out(str(tmp_path / "x"), "RID") == tmp_path / "x"


def test_unit_outcome_requires_source_outcomes_to_exist(tmp_path):
    """A unit is not `done` on the strength of an empty report."""
    unit_dir = tmp_path / "empty"
    unit_dir.mkdir()
    state, reasons = cb.unit_outcome(
        unit_dir, verification={"discrepancies": []}, limit=None
    )
    assert state == cb.UNIT_INCOMPLETE
    assert reasons


def test_a_verification_discrepancy_beats_a_limit_stop(tmp_path, preset_path):
    """A unit whose rows cannot be proven is `incomplete`, and --limit does not
    excuse it."""
    manifest = cm.load_manifest(preset_path)
    plan = cb.plan_units(manifest, limit=3)
    out = tmp_path / "out"
    cb.shard_units(
        manifest=manifest, manifest_path=preset_path, plan=plan, out=out,
        run_id="RPREC", limit=3, resume=False, tenant="t", dataset="d",
    )
    unit_dir = cb.unit_dir_for(out, cs.SINGLE_UNIT)
    state, reasons = cb.unit_outcome(
        unit_dir, verification={"discrepancies": ["batch x unresolved"]}, limit=3
    )
    assert state == cb.UNIT_INCOMPLETE
    assert reasons == ["batch x unresolved"]
