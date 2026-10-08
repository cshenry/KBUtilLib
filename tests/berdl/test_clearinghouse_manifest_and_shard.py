"""Tests for the bootstrap-load manifest, its validator, and the bronze sharder.

Pure logic, no pod, no network, no credentials -- the whole point of the shard
stage (see :mod:`kbutillib.domains.kbase.berdl.clearinghouse_shard`). These
tests cover:

  - Five plan-time rejection cases, each naming the offending key.
  - The mapping half of a manifest is adapter-independent (one mapping block
    validates identically under all three declared adapter names).
  - A tiny fixture source shards to bronze parquet whose ``entity_hash`` column
    equals :func:`standardizers.entity_hash(entity_type, raw)` row for row --
    checked against the REAL standardizer, not a stub.
  - Shard output is laid out one directory per target table with deterministic
    ``batch-<NNNN>.parquet`` names, sorted and range-disjoint.
  - THE PARAMETER SET on a ``[source.result]`` block: the plan-time gate
    (required, must be an inline table, must pass the parameter-set rule --
    note TOML parses ``1.0e-5`` as a float, which is rejected), the hash
    stamped on every sharded result row, the ``parameter_set`` REGISTRY shard
    a run writes (one row per distinct set), and the shared TRANSYT
    result-boundary rule at plan time AND per row in the sharder.
  - A REAL-PATH end-to-end case driving ``cs.shard_manifest`` with nothing
    monkeypatched and asserting against the parquet bytes read back off disk.
"""

from __future__ import annotations

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from kbutillib.domains.identity import (
    DEFAULT_PARAMETER_SET_HASH,
    ParameterSetError,
    parameter_set_hash,
    standardizers,
)
from kbutillib.domains.kbase.berdl import clearinghouse_manifest as cm
from kbutillib.domains.kbase.berdl import clearinghouse_shard as cs
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    PARAMETER_SET_TABLE,
    parameter_set_column_names,
)

# --------------------------------------------------------------------------
# (a) Five plan-time rejection cases, each naming the offending key
# --------------------------------------------------------------------------


def _plan(toml_text: str) -> list[cm.TablePlan]:
    return cm.shard_plan(cm.load_manifest(toml_text))


def test_reject_content_column_sequence_for_genome():
    """A [source.content] naming `sequence` for genome is rejected at plan time
    with the legal column list in the message."""
    toml_text = """
    [[source]]
    name = "bad-genome"
    adapter = "file"
    path = "x.parquet"
    entity_type = "genome"
    kinds = ["content"]
    [source.hash]
    precomputed = "genome_hash"
    [source.content]
    sequence = "seq"
    """
    with pytest.raises(cm.ManifestError) as exc:
        _plan(toml_text)
    message = str(exc.value)
    assert "sequence" in message
    assert "genome" in message
    # The message must carry the legal column list -- e.g. fasta_reference.
    assert "fasta_reference" in message


def test_reject_unknown_entity_type():
    toml_text = """
    [[source]]
    name = "bad-type"
    adapter = "file"
    path = "x.parquet"
    entity_type = "plasmid"
    kinds = ["entity"]
    [source.hash]
    raw_column = "seq"
    """
    with pytest.raises(cm.ManifestError) as exc:
        _plan(toml_text)
    message = str(exc.value)
    assert "plasmid" in message
    assert "entity_type" in message


def test_reject_unknown_kind():
    toml_text = """
    [[source]]
    name = "bad-kind"
    adapter = "file"
    path = "x.parquet"
    entity_type = "gene"
    kinds = ["entity", "sidecar"]
    [source.hash]
    raw_column = "seq"
    """
    with pytest.raises(cm.ManifestError) as exc:
        _plan(toml_text)
    message = str(exc.value)
    assert "sidecar" in message
    assert "kind" in message


def test_reject_result_block_with_empty_source():
    """An empty result `source` is rejected at plan time: it is the partition
    column and an empty partition value is a permanent scar."""
    toml_text = """
    [[source]]
    name = "bad-result"
    adapter = "file"
    path = "x.parquet"
    entity_type = "gene"
    kinds = ["result"]
    [source.hash]
    raw_column = "seq"
    [source.result]
    source = "   "
    result_type = "@const(functional_annotation)"
    """
    with pytest.raises(cm.ManifestError) as exc:
        _plan(toml_text)
    message = str(exc.value)
    assert "source" in message
    assert "partition" in message


def test_reject_unknown_derivation():
    """An @derivation outside the closed vocabulary is rejected by name."""
    toml_text = """
    [[source]]
    name = "bad-deriv"
    adapter = "file"
    path = "x.parquet"
    entity_type = "gene"
    kinds = ["content"]
    [source.hash]
    raw_column = "seq"
    [source.content]
    seq_length = "@reverse(seq)"
    """
    with pytest.raises(cm.ManifestError) as exc:
        _plan(toml_text)
    message = str(exc.value)
    assert "@reverse" in message
    # The message lists the closed vocabulary.
    assert "@len" in message and "@hash" in message


def test_reject_raw_column_for_genome():
    """A genome source declaring raw_column is rejected at plan time.

    A genome's identity comes from its contig SET; handing one column value to
    the genome standardizer iterates it character by character into a wrong
    hash (see clearinghouse_manifest._plan_hash). The message must name the
    offending key and point at the precomputed / KBDLHashGenomes routes.
    """
    toml_text = """
    [[source]]
    name = "bad-genome-raw"
    adapter = "file"
    path = "x.parquet"
    entity_type = "genome"
    kinds = ["entity"]
    [source.hash]
    raw_column = "dna_sequence"
    """
    with pytest.raises(cm.ManifestError) as exc:
        _plan(toml_text)
    message = str(exc.value)
    assert "bad-genome-raw" in message  # names the offending key
    assert "raw_column" in message
    assert "genome" in message
    # names the sanctioned routes
    assert "precomputed" in message
    assert "KBDLHashGenomes" in message


def test_accept_precomputed_for_genome():
    """The other side of the gate: a genome source declaring precomputed PASSES
    shard_plan(). precomputed stays legal for genome -- it is the sanctioned
    route the shipped example uses -- so the gate must not block it."""
    toml_text = """
    [[source]]
    name = "good-genome-precomputed"
    adapter = "file"
    path = "x.parquet"
    entity_type = "genome"
    kinds = ["entity"]
    [source.hash]
    precomputed = "genome_hash"
    """
    plans = _plan(toml_text)
    assert plans, "a precomputed genome source must produce a plan"
    assert all(p.entity_type == "genome" for p in plans)
    # the hash mode planned is precomputed, on the named column
    assert all(p.hash_source == ("precomputed", "genome_hash", "genome") for p in plans)


# --------------------------------------------------------------------------
# The mapping half is adapter-independent
# --------------------------------------------------------------------------


def test_mapping_half_is_adapter_independent():
    """One mapping block validates identically under every declared adapter
    name; only the source-locating keys differ."""
    mapping = """
    entity_type = "gene"
    kinds = ["entity", "content"]
    [source.hash]
    raw_column = "dna_sequence"
    [source.content]
    sequence = "dna_sequence"
    seq_length = "@len(dna_sequence)"
    """
    resolved: list[list[cm.TablePlan]] = []
    for adapter in cs.ADAPTER_NAMES:
        toml_text = f"""
        [[source]]
        name = "same-mapping"
        adapter = "{adapter}"
        path = "wherever"
        {mapping}
        """
        plans = _plan(toml_text)
        resolved.append(plans)

    # The resolved mapping (tables, kinds, columns, hash spec) is identical
    # across all three adapter names -- the adapter never touches the mapping.
    reference = resolved[0]
    for plans in resolved[1:]:
        assert [p.table for p in plans] == [p.table for p in reference]
        assert [sorted(p.columns) for p in plans] == [
            sorted(p.columns) for p in reference
        ]
        assert [p.hash_source for p in plans] == [p.hash_source for p in reference]


def test_all_declared_adapters_are_built():
    """Every declared adapter name now maps to a real class.

    `lakehouse` was built first, then `mongo` (see
    tests/berdl/test_clearinghouse_lakehouse_adapter.py and
    tests/berdl/test_clearinghouse_mongo_adapter.py); no declared name remains
    planned-but-unbuilt, so get_adapter constructs one for each rather than
    raising AdapterNotImplementedError.
    """
    for name in cs.ADAPTER_NAMES:
        assert name in cs._ADAPTER_CLASSES
        source = cm.Source(
            name="x",
            adapter=name,
            entity_type="gene",
            kinds=("entity",),
            locator={},
            hash_spec={"raw_column": "seq"},
            content={},
            result={},
        )
        assert isinstance(cs.get_adapter(source), cs.SourceAdapter)


def test_unknown_adapter_name_rejected():
    source = cm.Source(
        name="x",
        adapter="ftp",
        entity_type="gene",
        kinds=("entity",),
        locator={},
        hash_spec={"raw_column": "seq"},
        content={},
        result={},
    )
    with pytest.raises(cm.ManifestError):
        cs.get_adapter(source)


# --------------------------------------------------------------------------
# (b) A fixture source shards to bronze parquet whose entity_hash equals the
#     REAL standardizer row for row
# --------------------------------------------------------------------------


#: A tiny set of DNA sequences whose hashes span the hex space enough to land
#: in more than one range when we force a small target_bytes.
_FIXTURE_SEQS = [
    "ACGTACGTAC",
    "TTTTGGGGCC",
    "GATTACAGAT",
    "CCCCAAAATT",
    "ACGTTGCAAA",
    "GGGGCCCCTT",
    "ATATATATAT",
    "CGCGCGCGCG",
]


def _write_fixture_parquet(path, seqs):
    table = pa.table({"dna_sequence": seqs})
    pq.write_table(table, str(path))


def test_shard_entity_hash_matches_real_standardizer(tmp_path):
    src_path = tmp_path / "V2_Genes.parquet"
    _write_fixture_parquet(src_path, _FIXTURE_SEQS)

    toml_text = f"""
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
    """
    manifest = cm.load_manifest(toml_text)
    out_root = tmp_path / "bronze"
    # Force a small target so more than one shard is produced.
    reports = cs.shard_manifest(manifest, out_root, target_bytes=64)

    # Collect every entity_hash written for gene_entity, in order.
    entity_dir = out_root / "gene_entity"
    written_hashes: list[str] = []
    for report in sorted((r for r in reports if r.table == "gene_entity"),
                         key=lambda r: r.batch_id):
        table = pq.read_table(str(report.path))
        written_hashes.extend(table.column("entity_hash").to_pylist())

    expected = sorted(
        standardizers.entity_hash("gene", seq) for seq in _FIXTURE_SEQS
    )
    # Every written hash equals the REAL standardizer's, and the full set is
    # exactly the fixture's hashes (checked against the real function, not a
    # stub), globally sorted across shards.
    assert written_hashes == expected
    for h in written_hashes:
        assert len(h) == 64 and h == h.lower()


def test_shard_layout_and_deterministic_names(tmp_path):
    src_path = tmp_path / "V2_Genes.parquet"
    _write_fixture_parquet(src_path, _FIXTURE_SEQS)

    toml_text = f"""
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
    """
    manifest = cm.load_manifest(toml_text)
    out_root = tmp_path / "bronze"
    reports = cs.shard_manifest(manifest, out_root, target_bytes=64)

    # One directory per target table.
    dirs = {p.name for p in out_root.iterdir() if p.is_dir()}
    assert dirs == {"gene_entity", "gene_content"}

    # Deterministic batch-<NNNN>.parquet names, contiguous from 0000 per table.
    for table in ("gene_entity", "gene_content"):
        files = sorted(p.name for p in (out_root / table).iterdir())
        expected_names = [
            f"batch-{i:04d}.parquet" for i in range(len(files))
        ]
        assert files == expected_names

    # A re-run reproduces byte-identical batch ids (deterministic).
    out_root2 = tmp_path / "bronze2"
    reports2 = cs.shard_manifest(manifest, out_root2, target_bytes=64)
    assert [(r.table, r.batch_id, r.hash_min, r.hash_max) for r in reports] == [
        (r.table, r.batch_id, r.hash_min, r.hash_max) for r in reports2
    ]


def test_shards_are_sorted_and_disjoint(tmp_path):
    """The sort/range invariant is enforced across the emitted shards."""
    src_path = tmp_path / "V2_Genes.parquet"
    _write_fixture_parquet(src_path, _FIXTURE_SEQS)
    toml_text = f"""
    [[source]]
    name = "fixture-genes"
    adapter = "file"
    path = "{src_path}"
    format = "parquet"
    entity_type = "gene"
    kinds = ["entity"]
    [source.hash]
    raw_column = "dna_sequence"
    """
    manifest = cm.load_manifest(toml_text)
    out_root = tmp_path / "bronze"
    reports = [
        r for r in cs.shard_manifest(manifest, out_root, target_bytes=64)
        if r.table == "gene_entity"
    ]
    assert len(reports) >= 1
    prev_max = None
    for report in reports:
        assert report.hash_min <= report.hash_max
        if prev_max is not None:
            assert report.hash_min > prev_max
        prev_max = report.hash_max


def test_precomputed_hash_is_recanonicalised(tmp_path):
    """A precomputed digest is re-canonicalised (lowercased) through
    encode_entity_hash and never trusted raw."""
    upper_hash = "A" * 64
    src_path = tmp_path / "V2_Genomes.parquet"
    pq.write_table(pa.table({"genome_hash": [upper_hash], "fasta_path": ["/x"]}),
                   str(src_path))
    toml_text = f"""
    [[source]]
    name = "fixture-genomes"
    adapter = "file"
    path = "{src_path}"
    format = "parquet"
    entity_type = "genome"
    kinds = ["content"]
    [source.hash]
    precomputed = "genome_hash"
    [source.content]
    fasta_reference = "fasta_path"
    """
    manifest = cm.load_manifest(toml_text)
    out_root = tmp_path / "bronze"
    reports = cs.shard_manifest(manifest, out_root, target_bytes=64)
    table = pq.read_table(str(reports[0].path))
    written = table.column("entity_hash").to_pylist()[0]
    assert written == "a" * 64  # lowercased, not the raw uppercase


def test_worked_example_manifest_validates():
    """The shipped worked-example manifest passes plan-time validation."""
    from pathlib import Path

    example = (
        Path(cm.__file__).parent / "examples" / "gaa_store_parquet_dump.toml"
    )
    plans = cm.shard_plan(cm.load_manifest(example))
    tables = {p.table for p in plans}
    assert "gene_content" in tables
    assert "gene_result" in tables
    assert "genome_content" in tables


# ==========================================================================
# THE DEMO SEED MANIFEST (examples/clearinghouse_example_seed.toml)
# ==========================================================================
#
# Unlike the worked example above, this manifest describes a LIVE store and
# is meant to be RUN -- by `kbu clearinghouse backfill run
# mongo-protein-bakta --slice 00 --limit 12000`, against a scratch dataset
# first and then production (OP-C3 in the operator runbook). A sibling PRD's
# `mongo-protein-bakta` preset is tested to map this collection exactly as
# this file does, so the two must agree.
#
# These tests PLAN it off-pod, with NO MONGO CONNECTION. That is possible
# because planning is pure manifest work: `load_manifest` parses the TOML
# and `shard_plan` validates columns, derivations and the parameter set
# against the schema module, none of which opens a cursor (the `uri`,
# `database` and `collection` keys are adapter locator values that only
# `MongoSourceAdapter` ever dereferences, at shard time). So no skip is
# needed here -- if these tests ever start requiring a driver, that is a
# regression in the plan/read split, not an environment problem.
#
# Why this is worth testing at all: a seed manifest whose derivation is
# misspelled or whose column is not in the schema fails at PLAN time, and
# the cheapest place to learn that is here -- not against the live Mongo
# store, and emphatically not after a partial write into the lake.


def _seed_manifest_path():
    from pathlib import Path

    return (
        Path(cm.__file__).parent / "examples" / "clearinghouse_example_seed.toml"
    )


def _seed_plans():
    return cm.shard_plan(cm.load_manifest(_seed_manifest_path()))


def test_demo_seed_manifest_exists_and_is_shipped():
    """The seed mapping ships INSIDE the package, beside the worked example.

    It is operational input to the backfill command, not documentation
    parked in agent-io/, so it has to travel with an installed KBUtilLib.
    """
    assert _seed_manifest_path().is_file()


def test_demo_seed_manifest_plans_off_pod():
    """It parses and plans with no Mongo connection and no pod.

    Five plans across three sources: protein entity+content, protein
    result, function entity+content.
    """
    plans = _seed_plans()
    assert {p.table for p in plans} == {
        "protein_entity",
        "protein_content",
        "protein_result",
        "function_entity",
        "function_content",
    }


def test_demo_seed_has_exactly_the_three_declared_sources():
    """Three sources over ONE collection -- the join is expressed as
    several sources, not as a join inside the adapter."""
    manifest = cm.load_manifest(_seed_manifest_path())
    assert len(manifest.sources) == 3
    for source in manifest.sources:
        assert source.adapter == "mongo"
        assert source.locator["collection"] == "seq_protein_bakta"
        assert source.locator["database"] == "database"
        assert source.locator["uri"] == "mongodb://poplar.cels.anl.gov:27017"


def test_demo_seed_reads_slice_00_as_a_half_open_range():
    """Slice 00 of 256: ``_id`` in ["00", "01").

    A half-open range on the hex ``_id`` is a UNIFORM 1/256 sample, not a
    first-N prefix -- which is what makes the seed representative. The keys
    are MongoSourceAdapter's own ``sort_key``/``min_key``/``max_key``.
    """
    manifest = cm.load_manifest(_seed_manifest_path())
    for source in manifest.sources:
        assert source.locator["sort_key"] == "_id"
        assert source.locator["min_key"] == "00"
        assert source.locator["max_key"] == "01"


def test_demo_seed_recomputes_identity_rather_than_trusting_mongo_id():
    """Every hash spec uses ``raw_column``, never ``precomputed``.

    Mongo's ``_id`` is also a 64-hex sha256, but whether it equals what the
    standardizers compute from the sequence is unverified -- so the seed
    recomputes it.
    """
    manifest = cm.load_manifest(_seed_manifest_path())
    for source in manifest.sources:
        assert "precomputed" not in source.hash_spec
        assert source.hash_spec["raw_column"] in {"aa", "product"}


def test_demo_seed_result_carries_the_provenance_source_label():
    """The result source label, result_type and version are as specified.

    ``bakta/mongo-seq_protein_bakta``'s second segment is a PROVENANCE
    label, not a Bakta release: the store recorded no tool version, and
    inventing one would be a fabricated fact in the partition column.
    """
    (result_plan,) = [p for p in _seed_plans() if p.kind == "result"]
    assert (
        cm.constant_result_source(result_plan.columns)
        == "bakta/mongo-seq_protein_bakta"
    )
    assert result_plan.columns["result_type"].args[0] == "annotation"
    assert result_plan.columns["result_type_version"].args[0] == "1.0"


def test_demo_seed_result_payload_carries_the_bakta_annotation_fields():
    """``@json(...)`` collects the annotation fields, in declared order."""
    (result_plan,) = [p for p in _seed_plans() if p.kind == "result"]
    payload = result_plan.columns["payload"]
    assert payload.name == "json"
    assert list(payload.args) == [
        "product",
        "gene",
        "genes",
        "db_xrefs",
        "psc",
        "pscc",
        "type",
    ]


def test_demo_seed_result_declares_a_default_parameter_set():
    """``parameter_set = {}`` -- a DEFAULT run, hashing to the default hash.

    The store recorded no parameters, so nothing was overridden on top of
    the tool's defaults. This is the exact value OP-C3's promotion gate
    checks on every seeded result row, so it is pinned here against the
    shipped constant rather than a retyped digest.
    """
    from kbutillib.domains.identity import DEFAULT_PARAMETER_SET_HASH

    (result_plan,) = [p for p in _seed_plans() if p.kind == "result"]
    assert result_plan.parameter_set == {}
    assert result_plan.parameter_set_json == "{}"
    assert result_plan.parameter_set_hash == DEFAULT_PARAMETER_SET_HASH


def test_demo_seed_declares_no_resource_parameters():
    """No resource parameter reaches the parameter set.

    Threads, memory, paths, batch sizes and hostnames describe HOW a run
    was executed, not WHAT was computed, so they must never key a result.
    The seed's parameter set is empty, which satisfies this trivially --
    asserted anyway so that a later edit adding `batch_size` or a host to
    it fails here rather than silently forking every slot.
    """
    (result_plan,) = [p for p in _seed_plans() if p.kind == "result"]
    forbidden = {
        "threads",
        "cpus",
        "memory",
        "mem",
        "path",
        "paths",
        "batch_size",
        "hostname",
        "host",
        "uri",
    }
    assert not (set(result_plan.parameter_set or {}) & forbidden)


# ==========================================================================
# THE PARAMETER SET on a [source.result] block
# ==========================================================================
#
# A result is keyed by (entity, result_type, source, PARAMETER_SET_HASH), so
# every result source must declare which parameter set it represents. These
# tests cover the plan-time gate, the hash stamped on sharded rows, the
# parameter-set REGISTRY shard, and the shared TRANSYT result-boundary rule.


def _result_toml(
    *,
    name="fixture-result",
    path="x.parquet",
    entity_type="gene",
    source_value="@const(bakta/1.9)",
    parameter_set="{}",
    extra="",
):
    """Build a one-source result manifest, parameterised where tests vary it.

    ``parameter_set`` is raw TOML text so a test can omit the key entirely
    (pass ``None``) or declare a value TOML parses as a float.
    """
    ps_line = "" if parameter_set is None else f"parameter_set = {parameter_set}"
    return f"""
    [[source]]
    name = "{name}"
    adapter = "file"
    path = "{path}"
    format = "parquet"
    entity_type = "{entity_type}"
    kinds = ["result"]
    [source.hash]
    raw_column = "dna_sequence"
    [source.result]
    source = "{source_value}"
    result_type = "@const(functional_annotation)"
    payload = "@json(function_id)"
    {ps_line}
    {extra}
    """


def test_reject_result_without_parameter_set():
    """A result source with no parameter_set is rejected at plan time, and the
    message says {} is how a DEFAULT run is declared."""
    with pytest.raises(cm.ManifestError) as exc:
        _plan(_result_toml(parameter_set=None))
    message = str(exc.value)
    assert "parameter_set" in message
    assert "required" in message
    # The fix is named: {} declares a default run.
    assert "{}" in message
    assert "DEFAULT" in message


def test_reject_result_mapping_parameter_set_hash():
    """parameter_set_hash is filled by the sharder and may not be mapped."""
    with pytest.raises(cm.ManifestError) as exc:
        _plan(_result_toml(extra='parameter_set_hash = "@const(deadbeef)"'))
    message = str(exc.value)
    assert "parameter_set_hash" in message
    assert "filled by the sharder" in message


def test_reject_result_parameter_set_with_a_float():
    """TOML parses 1.0e-5 as a float; the parameter-set rule rejects floats, so
    this fails at PLAN time with the path to the offending value named."""
    with pytest.raises(cm.ManifestError) as exc:
        _plan(_result_toml(parameter_set="{ evalue = 1.0e-5 }"))
    message = str(exc.value)
    assert "evalue" in message
    assert "float not allowed" in message
    assert "pass decimals as strings" in message
    # And it names the manifest location, once.
    assert message.count("[source.result].parameter_set") == 1


def test_reject_result_parameter_set_that_is_not_a_table():
    """parameter_set is DATA (an inline table), never a @derivation string."""
    with pytest.raises(cm.ManifestError) as exc:
        _plan(_result_toml(parameter_set='"@const(evalue)"'))
    message = str(exc.value)
    assert "parameter_set" in message
    assert "inline TOML table" in message


def test_plan_carries_canonical_text_and_hash_computed_once():
    """The plan carries the validated set as BOTH canonical text and hash,
    computed at plan time against the live identity functions."""
    plans = _plan(_result_toml(parameter_set='{ evalue = "1e-5", mode = "fast" }'))
    (plan,) = plans
    assert plan.parameter_set == {"evalue": "1e-5", "mode": "fast"}
    # Canonical: keys sorted at every depth, no insignificant whitespace.
    assert plan.parameter_set_json == '{"evalue":"1e-5","mode":"fast"}'
    assert plan.parameter_set_hash == parameter_set_hash(
        {"evalue": "1e-5", "mode": "fast"}
    )


def test_plan_default_run_hashes_to_the_default_hash():
    (plan,) = _plan(_result_toml(parameter_set="{}"))
    assert plan.parameter_set == {}
    assert plan.parameter_set_json == "{}"
    assert plan.parameter_set_hash == DEFAULT_PARAMETER_SET_HASH
    assert plan.parameter_set_hash == parameter_set_hash({})


def test_non_result_kinds_carry_no_parameter_set():
    """An entity/content plan has no parameter set -- only results are keyed
    by one, and a non-null value there would be meaningless."""
    toml_text = """
    [[source]]
    name = "fixture-genes"
    adapter = "file"
    path = "x.parquet"
    entity_type = "gene"
    kinds = ["entity", "content"]
    [source.hash]
    raw_column = "dna_sequence"
    [source.content]
    sequence = "dna_sequence"
    """
    for plan in _plan(toml_text):
        assert plan.parameter_set is None
        assert plan.parameter_set_json is None
        assert plan.parameter_set_hash is None


# --------------------------------------------------------------------------
# The shared TRANSYT result-boundary validator, at MANIFEST PLANNING
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["transyt", "transyt_local", "TranSyT", "TRANSYT_LOCAL"])
def test_transyt_source_refuses_default_parameter_set_at_plan_time(tool):
    """A TRANSYT result has no default run: {} is refused, case-insensitively
    on the tool component of `source`."""
    with pytest.raises(cm.ManifestError) as exc:
        _plan(_result_toml(source_value=f"@const({tool}/1.0)", parameter_set="{}"))
    message = str(exc.value)
    assert "taxonomy_id" in message
    assert "TRANSYT" in message


@pytest.mark.parametrize(
    "value",
    [
        '{ taxonomy_id = "" }',        # empty
        "{ taxonomy_id = 562 }",       # an integer, not a string
        '{ taxonomy_id = "-562" }',    # negative
        '{ taxonomy_id = "0562" }',    # leading zero
        '{ taxonomy_id = "56a" }',     # not a decimal
        '{ taxonomy_id = "5.62" }',    # not a decimal
        "{ taxonomy_id = true }",      # a bool is not a string
        '{ other_key = "562" }',       # the key itself is missing
    ],
)
def test_transyt_rejects_bad_taxonomy_id_at_plan_time(value):
    with pytest.raises(cm.ManifestError) as exc:
        _plan(_result_toml(source_value="@const(transyt/1.0)", parameter_set=value))
    assert "taxonomy_id" in str(exc.value)


def test_transyt_accepts_a_decimal_taxonomy_id_at_plan_time():
    (plan,) = _plan(
        _result_toml(
            source_value="@const(transyt/1.0)",
            parameter_set='{ taxonomy_id = "562" }',
        )
    )
    assert plan.parameter_set == {"taxonomy_id": "562"}
    assert plan.parameter_set_hash == parameter_set_hash({"taxonomy_id": "562"})


def test_non_transyt_source_accepts_a_default_parameter_set():
    """Every other tool gets the generic validation only -- {} is a perfectly
    good default run for bakta."""
    (plan,) = _plan(_result_toml(source_value="@const(bakta/1.9)", parameter_set="{}"))
    assert plan.parameter_set_hash == DEFAULT_PARAMETER_SET_HASH


def test_transyt_with_a_derived_source_is_checked_per_row_by_the_sharder(tmp_path):
    """When `source` is a derivation the tool is unknown at plan time, so the
    manifest PLANS and the sharder applies the same shared validator per row.

    This is the only place the two halves of 4c can diverge, so it is pinned:
    the plan succeeds, and the shard of a row whose resolved source turns out
    to be TRANSYT raises with taxonomy_id named.
    """
    src_path = tmp_path / "ann.parquet"
    pq.write_table(
        pa.table(
            {
                "dna_sequence": ["ACGTACGTAC"],
                "algorithm": ["TranSyT/1.0"],
                "function_id": ["f1"],
            }
        ),
        str(src_path),
    )
    toml_text = _result_toml(
        path=str(src_path), source_value="@lower(algorithm)", parameter_set="{}"
    )
    manifest = cm.load_manifest(toml_text)
    # Plan time cannot know the tool -> it plans.
    (plan,) = cm.shard_plan(manifest)
    assert plan.parameter_set == {}
    # The sharder resolves `source` per row and refuses it there.
    with pytest.raises(ParameterSetError) as exc:
        cs.shard_manifest(manifest, tmp_path / "bronze")
    assert "taxonomy_id" in str(exc.value)
    assert "transyt/1.0" in str(exc.value)


def test_derived_transyt_source_with_a_taxonomy_id_shards(tmp_path):
    """The other side of the per-row gate: a declared taxonomy_id passes."""
    src_path = tmp_path / "ann.parquet"
    pq.write_table(
        pa.table(
            {
                "dna_sequence": ["ACGTACGTAC"],
                "algorithm": ["TranSyT/1.0"],
                "function_id": ["f1"],
            }
        ),
        str(src_path),
    )
    manifest = cm.load_manifest(
        _result_toml(
            path=str(src_path),
            source_value="@lower(algorithm)",
            parameter_set='{ taxonomy_id = "562" }',
        )
    )
    reports = cs.shard_manifest(manifest, tmp_path / "bronze")
    result = [r for r in reports if r.table == "gene_result"]
    assert result and result[0].rows == 1


# --------------------------------------------------------------------------
# Sharded result rows carry the hash; the run writes a registry shard
# --------------------------------------------------------------------------


def test_two_result_sources_write_hashes_and_a_two_row_registry_shard(tmp_path):
    """A run with two result sources -- {} and {"taxonomy_id": "562"} -- stamps
    each table's rows with its own hash and writes ONE 'parameter_set' shard
    holding exactly two rows, one per distinct set.

    The two sources target DIFFERENT entity types on purpose: the sharder
    names every shard batch-<NNNN> counting from zero per shard_source() call,
    so two sources feeding one table would overwrite each other's batch-0000
    (a pre-existing property of the sharder, unrelated to parameter sets).
    """
    src_path = tmp_path / "ann.parquet"
    pq.write_table(
        pa.table(
            {
                "dna_sequence": ["ACGTACGTAC", "TTTTGGGGCC"],
                "function_id": ["f1", "f2"],
            }
        ),
        str(src_path),
    )
    toml_text = (
        _result_toml(
            name="default-run",
            path=str(src_path),
            entity_type="gene",
            source_value="@const(bakta/1.9)",
            parameter_set="{}",
        )
        + _result_toml(
            name="transyt-run",
            path=str(src_path),
            entity_type="protein",
            source_value="@const(transyt/1.0)",
            parameter_set='{ taxonomy_id = "562" }',
        )
    )
    out_root = tmp_path / "bronze"
    reports = cs.shard_manifest(cm.load_manifest(toml_text), out_root)

    default_hash = parameter_set_hash({})
    transyt_hash = parameter_set_hash({"taxonomy_id": "562"})

    gene_rows = pq.read_table(str(out_root / "gene_result" / "batch-0000.parquet"))
    assert set(gene_rows.column("parameter_set_hash").to_pylist()) == {default_hash}
    protein_rows = pq.read_table(
        str(out_root / "protein_result" / "batch-0000.parquet")
    )
    assert set(protein_rows.column("parameter_set_hash").to_pylist()) == {
        transyt_hash
    }

    # Exactly ONE registry shard, with exactly two rows -- one per distinct set.
    registry_reports = [r for r in reports if r.table == PARAMETER_SET_TABLE]
    assert len(registry_reports) == 1
    assert registry_reports[0].rows == 2
    registry = pq.read_table(str(registry_reports[0].path)).to_pylist()
    assert {row["parameter_set_hash"] for row in registry} == {
        default_hash,
        transyt_hash,
    }
    by_hash = {row["parameter_set_hash"]: row for row in registry}
    assert by_hash[default_hash]["canonical_json"] == "{}"
    assert by_hash[transyt_hash]["canonical_json"] == '{"taxonomy_id":"562"}'
    # Provenance is filled the way the sharder fills it everywhere else.
    assert by_hash[default_hash]["observed_at"] is None
    assert by_hash[default_hash]["ingest_batch_id"] == (
        f"default-run:{PARAMETER_SET_TABLE}"
    )
    # The registry shard is written and reported BEFORE any result shard.
    assert reports[0].table == PARAMETER_SET_TABLE
    # And it carries exactly the registry's declared columns, in DDL order.
    assert pq.read_table(str(registry_reports[0].path)).column_names == list(
        parameter_set_column_names()
    )


def test_two_sources_sharing_a_parameter_set_write_one_registry_row(tmp_path):
    """One row per DISTINCT set, not per source: two sources both declaring {}
    produce a single registry row."""
    src_path = tmp_path / "ann.parquet"
    pq.write_table(
        pa.table({"dna_sequence": ["ACGTACGTAC"], "function_id": ["f1"]}),
        str(src_path),
    )
    toml_text = _result_toml(
        name="a", path=str(src_path), entity_type="gene", parameter_set="{}"
    ) + _result_toml(
        name="b", path=str(src_path), entity_type="protein", parameter_set="{}"
    )
    out_root = tmp_path / "bronze"
    reports = cs.shard_manifest(cm.load_manifest(toml_text), out_root)
    (registry,) = [r for r in reports if r.table == PARAMETER_SET_TABLE]
    assert registry.rows == 1


def test_a_run_with_no_result_source_writes_no_registry_shard(tmp_path):
    """Nothing to explain, so no registry shard and no registry directory."""
    src_path = tmp_path / "V2_Genes.parquet"
    _write_fixture_parquet(src_path, _FIXTURE_SEQS)
    toml_text = f"""
    [[source]]
    name = "fixture-genes"
    adapter = "file"
    path = "{src_path}"
    format = "parquet"
    entity_type = "gene"
    kinds = ["entity"]
    [source.hash]
    raw_column = "dna_sequence"
    """
    out_root = tmp_path / "bronze"
    reports = cs.shard_manifest(cm.load_manifest(toml_text), out_root)
    assert all(r.table != PARAMETER_SET_TABLE for r in reports)
    assert not (out_root / PARAMETER_SET_TABLE).exists()


# --------------------------------------------------------------------------
# REAL-PATH: the real shard entry point, real parquet on disk, no monkeypatch
# --------------------------------------------------------------------------


def test_real_shard_entry_point_writes_hash_and_registry_to_disk(tmp_path):
    """Drive the REAL shard entry point end to end and read the bytes back.

    NOTHING in clearinghouse_shard or clearinghouse_manifest is monkeypatched:
    a real TOML manifest goes through the real cm.load_manifest ->
    cs.shard_manifest path, writes real bronze parquet into tmp_path, and this
    test then READS THE FILES FROM DISK and checks

      (a) every result row's parameter_set_hash equals the LIVE
          kbutillib.domains.identity.parameter_set_hash({"evalue": "1e-5"}),
      (b) a 'parameter_set' registry shard exists on disk carrying that hash
          with canonical_json '{"evalue":"1e-5"}'.

    A stub cannot satisfy this: the assertions are against file contents, and
    the expected hash is computed by the shipped identity function, not
    hard-coded here.
    """
    src_path = tmp_path / "V2_Gene_annotations.parquet"
    pq.write_table(
        pa.table(
            {
                "dna_sequence": list(_FIXTURE_SEQS),
                "function_id": [f"f{i}" for i in range(len(_FIXTURE_SEQS))],
            }
        ),
        str(src_path),
    )
    manifest_path = tmp_path / "manifest.toml"
    manifest_path.write_text(
        f"""
[[source]]
name = "real-path-annotations"
adapter = "file"
path = "{src_path}"
format = "parquet"
entity_type = "gene"
kinds = ["result"]

  [source.hash]
  raw_column = "dna_sequence"

  [source.result]
  source        = "@const(bakta/1.9)"
  result_type   = "@const(functional_annotation)"
  payload       = "@json(function_id)"
  parameter_set = {{ evalue = "1e-5" }}
""",
        encoding="utf-8",
    )

    out_root = tmp_path / "bronze"
    # Small target_bytes so more than one result shard is written and the
    # assertion covers every file, not just the first.
    reports = cs.shard_manifest(cm.load_manifest(manifest_path), out_root, target_bytes=64)

    expected_hash = parameter_set_hash({"evalue": "1e-5"})

    # (a) Read the result parquet back off disk.
    result_files = sorted((out_root / "gene_result").glob("*.parquet"))
    assert result_files, "the real shard path must have written result parquet"
    total_rows = 0
    for path in result_files:
        table = pq.read_table(str(path))
        hashes = table.column("parameter_set_hash").to_pylist()
        assert hashes, f"{path.name} has no rows"
        assert set(hashes) == {expected_hash}
        total_rows += len(hashes)
    assert total_rows == len(_FIXTURE_SEQS)

    # (b) Read the registry shard back off disk.
    registry_files = sorted((out_root / PARAMETER_SET_TABLE).glob("*.parquet"))
    assert len(registry_files) == 1
    registry = pq.read_table(str(registry_files[0])).to_pylist()
    assert len(registry) == 1
    assert registry[0]["parameter_set_hash"] == expected_hash
    assert registry[0]["canonical_json"] == '{"evalue":"1e-5"}'
    # Belt and braces: the stored text really does hash to the stored hash.
    assert parameter_set_hash(json.loads(registry[0]["canonical_json"])) == (
        registry[0]["parameter_set_hash"]
    )

    # THE ORPHAN CHECK, run against the bytes on disk: every distinct
    # parameter_set_hash appearing on a result row must have a registry row
    # explaining it. A non-empty difference here means a result exists whose
    # parameters nothing can name.
    result_hashes = {
        value
        for path in result_files
        for value in pq.read_table(str(path))
        .column("parameter_set_hash")
        .to_pylist()
    }
    registry_hashes = {row["parameter_set_hash"] for row in registry}
    assert result_hashes - registry_hashes == set()
