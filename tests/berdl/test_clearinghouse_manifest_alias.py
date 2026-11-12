"""The ``alias`` manifest kind: plan-time refusals and the sharder's alias rows."""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from kbutillib.domains.kbase.berdl import clearinghouse_manifest as cm
from kbutillib.domains.kbase.berdl import clearinghouse_schema as schema
from kbutillib.domains.kbase.berdl import clearinghouse_shard as cs

_FASTA_ID = "a" * 64


def _toml(entries: str, *, kinds='["entity", "alias"]', path="x.parquet", etype="genome"):
    return f"""
    [[source]]
    name = "alias-src"
    adapter = "file"
    path = "{path}"
    format = "parquet"
    entity_type = "{etype}"
    kinds = {kinds}
    [source.hash]
    precomputed = "genome_hash"
    {entries}
    """


_GOOD = """
    [[source.alias]]
    alias_source = "kbdl_fasta_object"
    alias_id = "fasta_object_id"
    [[source.alias]]
    alias_source = "ncbi_assembly"
    alias_id = "@column(assembly_accession)"
"""


def _plan(text):
    return cm.shard_plan(cm.load_manifest(text))


def test_kinds_includes_alias_and_mirrors_schema():
    assert "alias" in cm.KINDS
    assert cm.KINDS == tuple(schema._KINDS)


def test_alias_preset_plans_a_type_alias_table_plan():
    plans = _plan(_toml(_GOOD))
    alias = [p for p in plans if p.kind == "alias"]
    assert len(alias) == 1
    assert alias[0].table == "genome_alias"
    assert [a for a, _ in alias[0].aliases] == ["kbdl_fasta_object", "ncbi_assembly"]


def test_unknown_alias_source_refused_naming_legal_values():
    text = _toml('[[source.alias]]\nalias_source = "bogus"\nalias_id = "x"')
    with pytest.raises(cm.ManifestError) as exc:
        _plan(text)
    msg = str(exc.value)
    assert "bogus" in msg
    for legal in schema.ALIAS_SOURCES:
        assert legal in msg


def test_unparseable_alias_id_derivation_refused():
    text = _toml(
        '[[source.alias]]\nalias_source = "kbdl_fasta_object"\n'
        'alias_id = "@nonsense(x)"'
    )
    with pytest.raises(cm.ManifestError):
        _plan(text)


@pytest.mark.parametrize(
    "column", ["observation", "entity_hash", "observed_at", "ingest_batch_id", "entity_type"]
)
def test_sharder_filled_columns_cannot_be_mapped(column):
    text = _toml(
        '[[source.alias]]\nalias_source = "kbdl_fasta_object"\n'
        f'alias_id = "fasta_object_id"\n{column} = "whatever"'
    )
    with pytest.raises(cm.ManifestError) as exc:
        _plan(text)
    assert column in str(exc.value)
    assert "sharder" in str(exc.value)


def test_observation_refusal_explains_withdrawal_path():
    text = _toml(
        '[[source.alias]]\nalias_source = "kbdl_fasta_object"\n'
        'alias_id = "fasta_object_id"\nobservation = "withdrawn"'
    )
    with pytest.raises(cm.ManifestError) as exc:
        _plan(text)
    assert "'observation'" in str(exc.value)
    assert "observe_alias" in str(exc.value)


def test_alias_kind_with_no_block_refused():
    with pytest.raises(cm.ManifestError):
        _plan(_toml(""))


def test_alias_block_without_alias_kind_refused():
    with pytest.raises(cm.ManifestError):
        _plan(_toml(_GOOD, kinds='["entity"]'))


def test_alias_block_as_single_table_refused():
    text = _toml(
        '[source.alias]\nalias_source = "kbdl_fasta_object"\nalias_id = "x"'
    )
    with pytest.raises(cm.ManifestError):
        _plan(text)


def _shard(tmp_path, entries, rows):
    src = tmp_path / "in.parquet"
    pq.write_table(pa.table(rows), str(src))
    manifest = cm.load_manifest(_toml(entries, path=str(src)))
    out = tmp_path / "bronze"
    reports = cs.shard_manifest(manifest, out)
    alias_rows = []
    for r in reports:
        if r.table == "genome_alias":
            alias_rows.extend(pq.read_table(str(r.path)).to_pylist())
    return alias_rows


def test_sharder_emits_present_on_every_alias_row(tmp_path):
    rows = _shard(
        tmp_path,
        _GOOD,
        {
            "genome_hash": ["1" * 64, "2" * 64],
            "fasta_object_id": [_FASTA_ID, "b" * 64],
            "assembly_accession": ["GCF_000000001.1", "GCF_000000002.1"],
        },
    )
    assert len(rows) == 4
    assert {r["observation"] for r in rows} == {"present"}
    assert {r["entity_type"] for r in rows} == {"genome"}


def test_null_producing_concat_drops_the_row_not_the_value(tmp_path):
    entries = """
    [[source.alias]]
    alias_source = "kbdl_fasta_object"
    alias_id = "fasta_object_id"
    [[source.alias]]
    alias_source = "ncbi_assembly"
    alias_id = "@concat(GCF_, acc, .1, ver)"
    """
    rows = _shard(
        tmp_path,
        entries,
        {
            "genome_hash": ["1" * 64, "2" * 64],
            "fasta_object_id": [_FASTA_ID, "b" * 64],
            "acc": ["000000001", None],
            "ver": ["", ""],
        },
    )
    ids = [r["alias_id"] for r in rows]
    assert not any("None" in i for i in ids)
    assert len(rows) == 3
    assert [r for r in rows if r["alias_source"] == "ncbi_assembly"][0][
        "alias_id"
    ] == "GCF_000000001.1"
