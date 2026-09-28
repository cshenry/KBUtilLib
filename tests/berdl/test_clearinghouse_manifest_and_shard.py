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
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from kbutillib.domains.identity import standardizers
from kbutillib.domains.kbase.berdl import clearinghouse_manifest as cm
from kbutillib.domains.kbase.berdl import clearinghouse_shard as cs

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


def test_planned_adapters_are_declared_but_unbuilt():
    """`mongo` and `lakehouse` are declared adapter names but not built here."""
    for name in ("mongo", "lakehouse"):
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
        with pytest.raises(cs.AdapterNotImplementedError):
            cs.get_adapter(source)


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
