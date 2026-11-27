"""lw-1: the ``method`` table, the seven joining tables, their write path,
the delivery manifest and ``entity_ids``.

The lake is :class:`_MiniLake`, an in-memory stand-in that STORES what
``load()`` is handed and answers the SELECT shapes the write path emits
(``IN (...)``, ``BETWEEN``, ``DESCRIBE``), so fill-only and conflict claims are
earned rather than asserted.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest

from kbutillib.domains.kbase.berdl import clearinghouse_delivery as cd
from kbutillib.domains.kbase.berdl import clearinghouse_ingest as ci
from kbutillib.domains.kbase.berdl import clearinghouse_links as cl
from kbutillib.domains.kbase.berdl import clearinghouse_schema as schema
from kbutillib.domains.kbase.berdl.capability import BerdlLoadRefusedError
from kbutillib.domains.kbase.berdl.clearinghouse_capability import (
    ClearinghouseCapability,
    ClearinghouseSchemaPreflightError,
)

from . import delivery_fixture as fx
from .test_clearinghouse_alias_reads import _ReadFake, row as alias_row

FIXTURE = Path(__file__).parent / "fixtures" / "delivery_v1"
WRITE_NS = f"{schema.TENANT}.{schema.NAMESPACE}"

#: sha256 of json.dumps(table_configs(), sort_keys=True) at the base commit
#: be432e7 -- the original twenty-one, which lw-1 must not change.
BASE_21_SHA256 = "bd195a4bbadff8050b68c42e22c35ee466540d1e9e01f4a324ff61f76f0c8d5a"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(ci.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setenv(ci.LOCK_DIR_ENV, str(tmp_path / "locks"))


# ---------------------------------------------------------------------------
# the in-memory lake
# ---------------------------------------------------------------------------


class _MiniLake:
    spark_session = None  # no Spark: survivors computed by the pure function

    def __init__(self, *, rows=None, absent_tables=()):
        self.rows: dict[str, list[dict]] = {t: [dict(r) for r in rs] for t, rs in (rows or {}).items()}
        self.absent = set(absent_tables)
        self.load_calls: list[dict] = []
        self.queries: list[str] = []

    def locus(self):
        return "in_pod"

    def resolve_write_namespace(self, *, dataset, tenant):
        return WRITE_NS

    def resolve_probe_namespace(self, *, dataset, tenant):
        return WRITE_NS

    def load(self, **kwargs):
        self.load_calls.append(kwargs)
        reports = []
        for table in kwargs["tables"]:
            name = table["name"]
            payload = kwargs["dataframes"][name]
            new = payload.rows if hasattr(payload, "rows") else list(payload)
            self.rows.setdefault(name, []).extend(dict(r) for r in new)
            reports.append({"name": name, "row_count": len(self.rows[name]), "new_snapshot_id": 1000 + len(self.load_calls)})
        return {"ingest_result": {"success": True}, "tables": reports}

    @staticmethod
    def _table_of(sql):
        m = re.search(r"FROM\s+(\S+)", sql) or re.search(r"DESCRIBE\s+(\S+)", sql)
        return m.group(1).split(".")[-1].strip("`\"") if m else ""

    def query(self, sql, *, params=None, engine=None, **kwargs):
        self.queries.append(sql)
        flat = " ".join(sql.split())
        table = self._table_of(flat)
        if flat.startswith("DESCRIBE"):
            if table in self.absent:
                raise RuntimeError("table not found")
            for config in schema.table_configs():
                if config["name"] == table:
                    return [{"col_name": p.strip().split()[0]} for p in config["schema_sql"].split(",")]
            raise RuntimeError("table not found")
        m = re.match(r"SELECT (DISTINCT )?(.+?) FROM \S+", flat)
        assert m, flat
        columns = [c.strip() for c in m.group(2).split(",")]
        out = list(self.rows.get(table, []))
        where = flat.split(" WHERE ", 1)[1] if " WHERE " in flat else ""
        for col, vals in re.findall(r"(\w+) IN \(([^)]*)\)", where):
            allowed = set(re.findall(r"'((?:[^']|'')*)'", vals))
            out = [r for r in out if r.get(col) in allowed]
        for col, lo, hi in re.findall(r"(\w+) BETWEEN '([^']*)' AND '([^']*)'", where):
            out = [r for r in out if r.get(col) is not None and lo <= r[col] <= hi]
        projected = [{c: r.get(c) for c in columns} for r in out]
        if m.group(1):
            seen, unique = set(), []
            for r in projected:
                k = tuple(sorted(r.items()))
                if k not in seen:
                    seen.add(k)
                    unique.append(r)
            projected = unique
        return projected


def _entities():
    rows = {}
    for table, rs in fx.table_rows():
        if table.endswith("_entity"):
            rows[table] = [dict(r, observed_at=fx.OBSERVED_AT, ingest_batch_id="PRE") for r in rs]
    return rows


def _lake(**kw):
    rows = _entities()
    rows.update(kw.pop("rows", {}))
    return _MiniLake(rows=rows, **kw)


def _cap(lake):
    return ClearinghouseCapability(capability=lake)


def _rows_for(table):
    return copy.deepcopy(dict(fx.table_rows())[table])


def _link(table, **over):
    row = _rows_for(table)[0]
    row.update(over)
    return row


def _register(cap, table, rows, **kw):
    return cap.register_links(table, rows, methods=kw.pop("methods", fx.method_rows()), **kw)


def _refused(cap, table, rows, pattern, lake, **kw):
    """Submit the violating rows; assert the refusal, the rule in its message and
    that load() was never called."""
    before = len(lake.load_calls)
    with pytest.raises(cl.LinkRefusedError, match=pattern) as info:
        _register(cap, table, rows, **kw)
    assert len(lake.load_calls) == before, "a refused call must write nothing"
    assert "Nothing has been written" in str(info.value)
    return str(info.value)


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


def test_table_configs_original_21_unchanged_then_method_then_seven_joins():
    configs = schema.table_configs()
    assert len(configs) == 29
    first = configs[:21]
    assert hashlib.sha256(json.dumps(first, sort_keys=True).encode()).hexdigest() == BASE_21_SHA256
    assert [c["name"] for c in configs[21:]] == ["method", *schema.JOIN_TABLES]
    assert schema.JOIN_TABLES == (
        "function_x_ontology_term",
        "ontology_term_x_ontology_term",
        "function_x_function",
        "protein_x_function",
        "gene_x_protein",
        "gene_x_genome",
        "genome_x_genome",
    )


def test_method_columns_in_order_and_unpartitioned():
    config = next(c for c in schema.table_configs() if c["name"] == "method")
    assert "partition_by" not in config
    cols = [p.strip().split() for p in config["schema_sql"].split(",")]
    assert cols == [
        ["method_name", "STRING"], ["algorithm", "STRING"], ["algorithm_version", "STRING"],
        ["parameters_json", "STRING"], ["parameter_set_hash", "STRING"], ["source_dataset", "STRING"],
        ["description", "STRING"], ["observed_at", "TIMESTAMP"], ["ingest_batch_id", "STRING"],
    ]


def test_every_join_table_has_the_generic_columns_partitioned_by_method_name():
    expected = [
        ["subject_hash", "STRING"], ["object_hash", "STRING"], ["relation", "STRING"],
        ["method_name", "STRING"], ["link_key", "STRING"], ["metadata", "STRING"],
        ["observed_at", "TIMESTAMP"], ["ingest_batch_id", "STRING"],
    ]
    by_name = {c["name"]: c for c in schema.table_configs()}
    for table in schema.JOIN_TABLES:
        assert [p.strip().split() for p in by_name[table]["schema_sql"].split(",")] == expected
        assert by_name[table]["partition_by"] == "method_name"


def test_method_and_join_tables_are_not_entity_typed_and_in_no_view():
    assert schema.METHOD_TABLE == "method"
    for name in ("method", *schema.JOIN_TABLES):
        assert name not in schema.ENTITY_TYPES
        assert name not in schema._KINDS
    for view in schema._VIEW_KINDS:
        sql = schema.union_view_sql(view, fqn_prefix="a.b")
        for name in ("method", *schema.JOIN_TABLES):
            assert f"`{name}`" not in sql


def test_join_endpoints_and_vocabularies_are_closed():
    assert schema.JOIN_TABLE_ENDPOINTS == {
        "function_x_ontology_term": ("function", "ontology_term"),
        "ontology_term_x_ontology_term": ("ontology_term", "ontology_term"),
        "function_x_function": ("function", "function"),
        "protein_x_function": ("protein", "function"),
        "gene_x_protein": ("gene", "protein"),
        "gene_x_genome": ("gene", "genome"),
        "genome_x_genome": ("genome", "genome"),
    }
    assert schema.JOIN_RELATIONS["function_x_ontology_term"] == ("label", "exact_synonym", "definition", "maps_to")
    assert schema.JOIN_RELATIONS["ontology_term_x_ontology_term"] == (
        "exact_match", "close_match", "broad_match", "narrow_match", "related_match", "xref")
    assert set(schema.SYMMETRIC_RELATIONS) == {"alias", "ani", "exact_match", "close_match", "related_match"}
    assert "xref" not in schema.SYMMETRIC_RELATIONS


def test_ingest_key_modes_and_range_columns():
    assert ci.key_columns_for_table("method") == ("method_name",)
    assert ci.range_column_for_table("method") == "method_name"
    for table in schema.JOIN_TABLES:
        assert ci.key_columns_for_table(table) == (
            "subject_hash", "object_hash", "relation", "method_name", "link_key")
        assert ci.range_column_for_table(table) == "subject_hash"
    assert [t[0] for t in ci.resolvable_targets("gene_x_genome")] == ["subject_hash", "object_hash", "method_name"]
    assert [t[1] for t in ci.resolvable_targets("gene_x_genome")] == ["gene_entity", "genome_entity", "method"]


def test_new_alias_sources_and_display_prefixes():
    for source in ("cdm_protein", "uniref90", "uniref50", "uniparc", "protein_md5",
                   "cdm_genome", "bvbrc_genome", "fitness_org", "img_taxon"):
        assert source in schema.ALIAS_SOURCES
    assert schema.ALIAS_DISPLAY_PREFIXES == {
        "cdm_protein": "CDM", "cdm_genome": "CDM", "gtdb_accession": "GTDB",
        "ncbi_assembly": "NCBI", "bvbrc_genome": "PATRIC", "uniref90": "UniRef90",
        "uniref50": "UniRef50", "uniparc": "UniParc", "protein_md5": "MD5",
        "kbdl_object": "KBDL", "fitness_org": "FB", "img_taxon": "IMG",
    }
    schema.validate_alias("protein", fx.P1, "uniref90", "UniRef90_P12345", "present")
    schema.validate_alias("genome", fx.G1, "img_taxon", "2500000000", "present")
    with pytest.raises(ValueError):
        schema.validate_alias("genome", fx.G1, "uniref90", "x", "present")


def test_gene_content_still_has_protein_entity_hash():
    assert "protein_entity_hash" in schema.column_names("gene", "content")


# ---------------------------------------------------------------------------
# stop-list
# ---------------------------------------------------------------------------

STOPLIST = [
    "hypothetical protein", "conserved hypothetical protein", "uncharacterized protein",
    "uncharacterised protein", "putative uncharacterized protein", "putative protein",
    "predicted protein", "protein of unknown function", "unknown function",
    "unknown protein", "unknown", "hypothetical", "domain of unknown function",
]


def test_stoplist_version_and_exact_membership():
    assert schema.STOPLIST_VERSION == 1
    assert schema.FUNCTION_STOPLIST == frozenset(STOPLIST)


@pytest.mark.parametrize("text", STOPLIST)
def test_every_stoplist_string_is_stoplisted(text):
    assert schema.is_stoplisted_function(text)
    # standardization: case, whitespace, one trailing period
    assert schema.is_stoplisted_function(f"  {text.upper()}. ")
    assert schema.is_stoplisted_function(text.replace(" ", "  "))


def test_family_names_are_loaded_not_stoplisted():
    assert not schema.is_stoplisted_function("DUF1234 domain-containing protein")
    assert not schema.is_stoplisted_function("hypothetical protein, putative kinase")
    assert not schema.is_stoplisted_function("hypothetical protein..")  # only ONE period is stripped


# ---------------------------------------------------------------------------
# register_methods: refusals and fill-only
# ---------------------------------------------------------------------------


def _method(**over):
    row = copy.deepcopy(fx.method_rows()[0])
    row.update(over)
    return row


def _m_refused(cap, lake, rows, pattern):
    with pytest.raises(cl.LinkRefusedError, match=pattern) as info:
        cap.register_methods(rows)
    assert lake.load_calls == [] or all(c["tables"][0]["name"] != "method" for c in lake.load_calls)
    assert "Nothing has been written" in str(info.value)


@pytest.mark.parametrize(
    "name",
    ["Bakta@1/x", "bakta/x", "bakta@1", "bakta@/x", "bakta@1/", "-bakta@1/x", "bakta@1/-x", "bakta@1/X", "bakta@1/a b", ""],
)
def test_register_methods_refuses_a_name_outside_the_pattern(name):
    lake = _lake()
    _m_refused(_cap(lake), lake, [_method(method_name=name)], "method_name|identity")


def test_register_methods_refuses_a_name_over_128_characters():
    lake = _lake()
    name = "a@1/" + "b" * 125
    assert len(name) == 129
    _m_refused(_cap(lake), lake, [_method(method_name=name)], "at most 128")


def test_register_methods_accepts_the_measured_poplar_names():
    lake = _lake()
    rows = [
        _method(method_name="bakta@1.11.4/kbdl-default"),
        _method(method_name="transyt@sha256.0123abcd/default", algorithm="transyt", algorithm_version="sha256:0123abcd"),
    ]
    assert cap_written(_cap(lake), rows) == ["bakta@1.11.4/kbdl-default", "transyt@sha256.0123abcd/default"]


def cap_written(cap, rows):
    return cap.register_methods(rows)["written"]


@pytest.mark.parametrize("column", ["algorithm", "algorithm_version", "parameter_set_hash", "source_dataset", "parameters_json"])
def test_register_methods_refuses_a_null_in_an_identity_column(column):
    lake = _lake()
    _m_refused(_cap(lake), lake, [_method(**{column: None})], f"NULL {column}")


def test_register_methods_refuses_a_parameter_set_hash_that_does_not_match_its_json():
    lake = _lake()
    _m_refused(_cap(lake), lake, [_method(parameter_set_hash="0" * 64)], "does not match the hash recomputed")


def test_register_methods_refuses_non_canonical_parameters_json():
    lake = _lake()
    _m_refused(_cap(lake), lake, [_method(parameters_json='{"b":"1","a":"2"}')], "not canonical")


def test_register_methods_refuses_a_caller_supplied_clock():
    lake = _lake()
    _m_refused(_cap(lake), lake, [_method(observed_at="2026-01-01T00:00:00Z")], "caller-supplied 'observed_at'")


def test_register_methods_refuses_a_conflicting_redefinition_within_the_call():
    lake = _lake()
    a = _method()
    b = _method(algorithm_version="9.9")
    _m_refused(_cap(lake), lake, [a, b], "conflicting redefinition")


@pytest.mark.parametrize("column,value", [
    ("algorithm", "other"), ("algorithm_version", "2.0"), ("source_dataset", "snapshot-other"),
    ("parameters_json", PARAMS := '{"e_value":"1e-9"}'),
])
def test_register_methods_refuses_a_conflicting_redefinition_against_committed(column, value):
    lake = _lake()
    cap = _cap(lake)
    cap.register_methods([_method()])
    over = {column: value}
    if column == "parameters_json":
        over["parameter_set_hash"] = fx.parameter_set_hash(json.loads(value))
    before = len(lake.rows["method"])
    with pytest.raises(cl.LinkRefusedError, match="conflicting redefinition"):
        cap.register_methods([_method(**over)])
    assert len(lake.rows["method"]) == before


def test_register_methods_writes_the_registry_row_when_new_and_is_fill_only():
    lake = _lake()
    cap = _cap(lake)
    first = cap.register_methods(fx.method_rows())
    assert len(first["written"]) == 3
    assert {r["parameter_set_hash"] for r in lake.rows["parameter_set"]} == {
        m["parameter_set_hash"] for m in fx.method_rows()}
    unknown = next(r for r in lake.rows["parameter_set"] if r["canonical_json"] == fx.PARAMS_UNKNOWN)
    assert unknown
    loads = len(lake.load_calls)
    second = cap.register_methods(fx.method_rows())
    assert second["written"] == [] and len(second["already_present"]) == 3
    assert len(lake.load_calls) == loads
    assert len(lake.rows["method"]) == 3
    assert all(r["observed_at"] and r["ingest_batch_id"] for r in lake.rows["method"])


def test_register_methods_is_in_pod_only():
    class _Off(_MiniLake):
        def locus(self):
            return "off_pod"

    with pytest.raises(BerdlLoadRefusedError):
        _cap(_Off()).register_methods(fx.method_rows())


def test_register_methods_holds_the_namespace_writer_lock(tmp_path):
    lake = _lake()
    held = ci.NamespaceLock(WRITE_NS)
    held.acquire(execution_id="01HELDELDELDELDELDELDELDEL")
    try:
        with pytest.raises(Exception, match="(?i)lock|held|ingest"):
            _cap(lake).register_methods(fx.method_rows())
    finally:
        held.release()
    assert lake.load_calls == []


# ---------------------------------------------------------------------------
# register_links: one test per refusal rule
# ---------------------------------------------------------------------------


def test_refusal_unknown_table():
    lake = _lake()
    with pytest.raises(cl.LinkRefusedError, match="unknown join table 'gene_x_gene'"):
        _cap(lake).register_links("gene_x_gene", [])
    assert lake.load_calls == []


def test_refusal_method_name_with_no_method_row():
    lake = _lake()
    cap = _cap(lake)
    row = _link("gene_x_protein", method_name="ghost@1/none")
    with pytest.raises(cl.LinkRefusedError, match="no method row") as info:
        cap.register_links("gene_x_protein", [row])
    assert "link row 0" in str(info.value) and "ghost@1/none" in str(info.value)
    assert lake.load_calls == []


def test_a_method_in_the_same_call_or_already_committed_satisfies_the_rule():
    lake = _lake()
    cap = _cap(lake)
    row = _link("gene_x_protein")
    assert cap.register_links("gene_x_protein", [row], methods=fx.method_rows())["written"] == 1
    lake2 = _lake()
    cap2 = _cap(lake2)
    cap2.register_methods(fx.method_rows())
    assert cap2.register_links("gene_x_protein", [row])["written"] == 1


def test_refusal_relation_outside_the_vocabulary():
    lake = _lake()
    msg = _refused(_cap(lake), "gene_x_protein", [_link("gene_x_protein", relation="derives")], "outside the gene_x_protein vocabulary", lake)
    assert "link row 0" in msg


@pytest.mark.parametrize("column", ["subject_hash", "object_hash", "relation", "method_name", "link_key"])
def test_refusal_null_in_any_key_field(column):
    lake = _lake()
    _refused(_cap(lake), "gene_x_protein", [_link("gene_x_protein", **{column: None})], f"NULL {column}", lake)


@pytest.mark.parametrize(
    "table,drop",
    [
        ("function_x_ontology_term", "basis"),
        ("function_x_ontology_term", "field"),
        ("ontology_term_x_ontology_term", "source_predicate"),
        ("ontology_term_x_ontology_term", "asserting_curie"),
        ("ontology_term_x_ontology_term", "asserted_curie"),
        ("function_x_function", "via_term"),
        ("gene_x_protein", "gene_id"),
        ("gene_x_protein", "translation_table"),
        ("gene_x_genome", "gene_id"),
        ("gene_x_genome", "contig_hash"),
        ("gene_x_genome", "begin"),
        ("gene_x_genome", "end"),
        ("gene_x_genome", "strand"),
        ("gene_x_genome", "partial_begin"),
        ("gene_x_genome", "partial_end"),
        ("genome_x_genome", "ani"),
        ("genome_x_genome", "af_query"),
        ("genome_x_genome", "af_reference"),
        ("genome_x_genome", "original_orientation"),
    ],
)
def test_refusal_missing_required_metadata_key(table, drop):
    lake = _lake()
    row = _link(table)
    meta = json.loads(row["metadata"])
    del meta[drop]
    _refused(_cap(lake), table, [dict(row, metadata=cl.canonical_metadata(meta))], r"missing required metadata key|metadata\.", lake)


def test_refusal_inferred_function_ontology_link_needs_non_empty_evidence():
    lake = _lake()
    row = dict(fx.table_rows()[0][1][1])
    row["metadata"] = cl.canonical_metadata({"basis": "inferred", "evidence": {}})
    _refused(_cap(lake), "function_x_ontology_term", [row], "evidence must be a non-empty object", lake)


@pytest.mark.parametrize("relation,basis", [("label", "inferred"), ("exact_synonym", "inferred"), ("definition", "inferred"), ("maps_to", "definition")])
def test_refusal_relation_basis_mismatch(relation, basis):
    lake = _lake()
    meta = {"basis": basis, "field": "name", "evidence": {"s": "1"}}
    row = {"subject_hash": fx.F1, "object_hash": fx.T1, "relation": relation, "method_name": fx.M_ONTO,
           "link_key": "" if basis == "definition" else "a" * 64, "metadata": cl.canonical_metadata(meta)}
    _refused(_cap(lake), "function_x_ontology_term", [row], "requires metadata.basis", lake)


@pytest.mark.parametrize("bad", ['{"b":"1","a":"2"}', '{"a": "1"}', '{"a":"1" }'])
def test_refusal_non_canonical_metadata_json(bad):
    lake = _lake()
    _refused(_cap(lake), "function_x_function", [_link("function_x_function", metadata=bad)], "non-canonical metadata JSON", lake)


def test_refusal_metadata_with_a_float():
    lake = _lake()
    row = _link("genome_x_genome", metadata='{"ani":97.5,"af_query":"0.9","af_reference":"0.9","original_orientation":"x"}')
    _refused(_cap(lake), "genome_x_genome", [row], "float", lake)


@pytest.mark.parametrize(
    "table,relation",
    [("genome_x_genome", "ani"), ("function_x_function", "alias"), ("ontology_term_x_ontology_term", "exact_match"),
     ("ontology_term_x_ontology_term", "close_match"), ("ontology_term_x_ontology_term", "related_match")],
)
def test_refusal_symmetric_row_not_in_byte_order(table, relation):
    lake = _lake()
    base = _rows_for(table)[0]
    row = dict(base, relation=relation, subject_hash=base["object_hash"], object_hash=base["subject_hash"])
    _refused(_cap(lake), table, [row], "subject_hash < object_hash in byte order", lake)


def test_symmetric_row_in_order_is_accepted_and_xref_is_directional():
    lake = _lake()
    cap = _cap(lake)
    assert _register(cap, "genome_x_genome", _rows_for("genome_x_genome"))["written"] == 1
    xref = _rows_for("ontology_term_x_ontology_term")[0]
    reverse = dict(xref, subject_hash=xref["object_hash"], object_hash=xref["subject_hash"])
    assert _register(cap, "ontology_term_x_ontology_term", [xref, reverse])["written"] == 2


def test_symmetric_self_pair_is_skipped_and_counted():
    lake = _lake()
    base = _rows_for("genome_x_genome")[0]
    self_pair = dict(base, object_hash=base["subject_hash"])
    report = _register(_cap(lake), "genome_x_genome", [self_pair, base])
    assert report["skipped_self_pairs"] == 1 and report["written"] == 1


@pytest.mark.parametrize(
    "patch,rule",
    [
        ({"begin": 300, "end": 300}, "begin < end"),
        ({"begin": 301, "end": 300}, "begin < end"),
        ({"begin": 0, "end": 300}, "1-based"),
        ({"begin": 1.5, "end": 300}, "float"),
        ({"strand": 0}, "strand must be 1 or -1"),
        ({"strand": 2}, "strand must be 1 or -1"),
        ({"contig_hash": "abc"}, "contig_hash must be 64"),
        ({"contig_hash": "A" * 64}, "contig_hash must be 64"),
        ({"partial_begin": "no"}, "partial_begin"),
        ({"partial_end": 1}, "partial_end"),
        ({"gene_id": "KBDL:aaaaaaaaaaaaaaaa.2.2"}, "gene_id must equal link_key"),
        ({"gene_id": "bare"}, "gene_id must equal link_key"),
    ],
)
def test_refusal_gene_x_genome_coordinate_rules(patch, rule):
    lake = _lake()
    row = _link("gene_x_genome")
    meta = json.loads(row["metadata"])
    meta.update(patch)
    try:
        text = cl.canonical_metadata(meta)
    except ValueError:  # a float cannot even be canonicalized: hand over raw JSON
        text = json.dumps(meta, sort_keys=True, separators=(",", ":"))
    _refused(_cap(lake), "gene_x_genome", [dict(row, metadata=text)], rule, lake)


def test_refusal_gene_id_not_namespaced():
    lake = _lake()
    bare = "gene-1"
    row = _link("gene_x_genome", link_key=bare)
    row["metadata"] = cl.canonical_metadata({**json.loads(row["metadata"]), "gene_id": bare})
    _refused(_cap(lake), "gene_x_genome", [row], "not namespaced", lake)


def test_refusal_same_key_different_metadata_within_a_batch():
    lake = _lake()
    a = _link("gene_x_protein")
    b = dict(a, metadata=cl.canonical_metadata({"gene_id": a["link_key"], "translation_table": 4}))
    _refused(_cap(lake), "gene_x_protein", [a, b], "same key .* different metadata", lake)


def test_byte_identical_rows_collapse_within_a_batch():
    lake = _lake()
    a = _link("gene_x_protein")
    report = _register(_cap(lake), "gene_x_protein", [a, dict(a)])
    assert report["written"] == 1 and report["collapsed_identical"] == 1


def test_refusal_same_key_different_metadata_against_committed_rows():
    lake = _lake()
    cap = _cap(lake)
    a = _link("gene_x_protein")
    _register(cap, "gene_x_protein", [a])
    changed = dict(a, metadata=cl.canonical_metadata({"gene_id": a["link_key"], "translation_table": 4}))
    before = len(lake.rows["gene_x_protein"])
    with pytest.raises(cl.LinkRefusedError, match="committed row but different metadata"):
        cap.register_links("gene_x_protein", [changed])
    assert len(lake.rows["gene_x_protein"]) == before


@pytest.mark.parametrize("column,etype_table", [("subject_hash", "gene_entity"), ("object_hash", "protein_entity")])
def test_orphan_endpoint_is_refused_at_the_write_boundary(column, etype_table):
    lake = _lake()
    lake.rows[etype_table] = []
    msg = _refused(_cap(lake), "gene_x_protein", [_link("gene_x_protein")], "orphan", lake)
    assert column in msg


# ---------------------------------------------------------------------------
# acceptance, fill-only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", schema.JOIN_TABLES)
def test_a_valid_row_in_each_of_the_seven_tables_round_trips(table):
    lake = _lake()
    rows = _rows_for(table)
    report = _register(_cap(lake), table, rows)
    assert report["written"] == len(rows)
    stored = lake.rows[table]
    assert [{k: r[k] for k in rows[0]} for r in stored][0] == rows[0]
    assert all(r["observed_at"] and r["ingest_batch_id"] for r in stored)
    assert set(stored[0]) == set(schema.join_table_column_names(table))


def test_fill_only_the_same_batch_twice_writes_zero_rows_the_second_time():
    lake = _lake()
    cap = _cap(lake)
    rows = _rows_for("gene_x_genome")
    assert _register(cap, "gene_x_genome", rows)["written"] == 1
    loads = len(lake.load_calls)
    again = cap.register_links("gene_x_genome", rows)
    assert again["written"] == 0 and again["already_present"] == 1
    assert len(lake.load_calls) == loads
    assert len(lake.rows["gene_x_genome"]) == 1


def test_one_pair_under_two_methods_writes_two_rows():
    lake = _lake()
    cap = _cap(lake)
    a = _link("gene_x_protein")
    b = dict(a, method_name=fx.M_KOFAM)
    report = cap.register_links("gene_x_protein", [a, b], methods=fx.method_rows())
    assert report["written"] == 2
    assert {r["method_name"] for r in lake.rows["gene_x_protein"]} == {fx.M_BAKTA, fx.M_KOFAM}


def test_link_rows_with_a_caller_supplied_clock_are_refused():
    lake = _lake()
    _refused(_cap(lake), "gene_x_protein", [_link("gene_x_protein", observed_at="2026-01-01T00:00:00Z")], "caller-supplied 'observed_at'", lake)


# ---------------------------------------------------------------------------
# ingest path: orphan refusal on BOTH endpoints and the method, schema preflight
# ---------------------------------------------------------------------------


def _fresh_delivery(tmp_path, drop_entity=None):
    out = tmp_path / "delivery"
    tables = [(t, r) for t, r in fx.table_rows() if t != drop_entity]
    cd.build_delivery(out, tables, producer=fx.PRODUCER, observed_at=fx.OBSERVED_AT, standardizer_version=fx.STANDARDIZER_VERSION)
    return out


def _unvalidated_inventory(out):
    """run.json WITHOUT delivery validation, so the lake-side refusal is what is tested."""
    manifest = json.loads((out / cd.DELIVERY_FILE).read_text())
    from kbutillib.domains.kbase.berdl.clearinghouse_shard import RUN_MANIFEST_VERSION

    run = {
        "run_manifest_version": RUN_MANIFEST_VERSION, "run_id": manifest["delivery_id"],
        "created_at": fx.OBSERVED_AT, "slices": [], "units": [], "skipped": [], "sources": [],
        "batches": [{**{k: b[k] for k in ("table", "batch_id", "file", "rows", "bytes", "key_column", "hash_min", "hash_max", "sha256")},
                     "rows_deduplicated": 0, "source": ["t"], "slice": None, "unit_id": None, "attempt_id": "d",
                     **({"method_names": b["method_names"]} if "method_names" in b else {})}
                    for b in manifest["batches"]],
    }
    (out / "run.json").write_text(json.dumps(run))


def test_the_whole_golden_delivery_ingests_and_a_second_ingest_writes_nothing(tmp_path):
    out = _fresh_delivery(tmp_path)
    cd.write_run_inventory(out)
    lake = _MiniLake()
    cap = _cap(lake)
    first = cap.ingest_shards(out)
    assert all(b["action"] == "ingest" for b in first["tables"]), first["batches"]
    for table in ("method", *schema.JOIN_TABLES):
        assert len(lake.rows[table]) >= 1
    order = [c["tables"][0]["name"] for c in lake.load_calls]
    assert order.index("parameter_set") < order.index("method") < order.index("genome_entity")
    assert order.index("genome_entity") < order.index("genome_alias") < order.index("gene_x_genome")
    loads = len(lake.load_calls)
    second = cap.ingest_shards(out)
    assert len(lake.load_calls) == loads
    assert all(b["rows_ingested"] == 0 for b in second["tables"])


@pytest.mark.parametrize("missing", ["protein_entity", "gene_entity"])
def test_ingest_refuses_a_link_batch_whose_endpoint_entity_is_absent(tmp_path, missing):
    out = _fresh_delivery(tmp_path, drop_entity=missing)
    _unvalidated_inventory(out)
    lake = _MiniLake()
    report = _cap(lake).ingest_shards(out)
    refused = [b for b in report["tables"] if b["action"] == "refused"]
    assert {b["name"] for b in refused} >= {"gene_x_protein"}
    assert any(missing in f or "resolve" in f for b in refused for f in b["join_assertions"]["failures"])
    assert "gene_x_protein" not in lake.rows


def test_ingest_refuses_a_link_batch_whose_method_is_absent(tmp_path):
    out = _fresh_delivery(tmp_path)
    manifest = json.loads((out / cd.DELIVERY_FILE).read_text())
    _unvalidated_inventory(out)
    run = json.loads((out / "run.json").read_text())
    run["batches"] = [b for b in run["batches"] if b["table"] != "method"]
    (out / "run.json").write_text(json.dumps(run))
    lake = _MiniLake()
    report = _cap(lake).ingest_shards(out)
    refused = {b["name"] for b in report["tables"] if b["action"] == "refused"}
    assert set(schema.JOIN_TABLES) <= refused
    assert manifest


def test_lake5_preflight_requires_method_and_the_join_tables_a_batch_targets(tmp_path):
    out = _fresh_delivery(tmp_path)
    cd.write_run_inventory(out)
    with pytest.raises(ClearinghouseSchemaPreflightError, match="method: table does not exist"):
        _cap(_MiniLake(absent_tables=["method"])).ingest_shards(out)
    with pytest.raises(ClearinghouseSchemaPreflightError, match="gene_x_genome: table does not exist"):
        _cap(_MiniLake(absent_tables=["gene_x_genome"])).ingest_shards(out)


def test_lake5_preflight_does_not_demand_link_tables_of_a_run_without_links():
    cap = _cap(_MiniLake(absent_tables=["method", *schema.JOIN_TABLES]))
    cap._assert_lake5_schema(WRITE_NS, tables=["gene_entity"])
    with pytest.raises(ClearinghouseSchemaPreflightError):
        cap._assert_lake5_schema(WRITE_NS, tables=["gene_x_protein"])


def test_a_null_key_field_in_a_link_batch_fails_the_join_assertions():
    import pyarrow as pa

    rows = _rows_for("gene_x_protein")
    rows[0]["link_key"] = None
    batch = pa.Table.from_pylist(rows)
    report = ci.join_assertions(batch, table="gene_x_protein", batch_id="B", resolvable={fx.GN1},
                                resolvable_extra={"object_hash": {fx.P1}, "method_name": {fx.M_BAKTA}})
    assert not report["ok"] and any("NULL link_key" in f for f in report["failures"])


def test_parameter_set_orphans_covers_method_parameter_set_hash():
    lake = _lake()
    cap = _cap(lake)
    lake.rows["method"] = [dict(m, observed_at="t", ingest_batch_id="b") for m in fx.method_rows()]
    report = cap.parameter_set_orphans()
    assert not report["ok"]
    missing = {e["parameter_set_hash"] for e in report["missing_from_registry"]}
    assert missing == {m["parameter_set_hash"] for m in fx.method_rows()}
    assert all(e["entity_types"] == ["method"] for e in report["missing_from_registry"])
    lake.rows["parameter_set"] = [
        {"parameter_set_hash": m["parameter_set_hash"], "canonical_json": m["parameters_json"]} for m in fx.method_rows()]
    assert cap.parameter_set_orphans()["ok"]


# ---------------------------------------------------------------------------
# delivery manifest: the golden fixture
# ---------------------------------------------------------------------------


def test_golden_delivery_fixture_is_checked_in_and_valid():
    assert (FIXTURE / "delivery.json").is_file()
    manifest = cd.validate_delivery(FIXTURE)
    assert manifest["delivery_schema_version"] == 1 == cd.DELIVERY_SCHEMA_VERSION
    assert [b["stage"] for b in manifest["batches"]] == sorted(
        (b["stage"] for b in manifest["batches"]), key=cd.STAGES.index)
    assert {b["table"] for b in manifest["batches"]} >= {"method", *schema.JOIN_TABLES}
    methods = next(b for b in manifest["batches"] if b["table"] == "method")
    assert methods["key_column"] == "method_name"
    link = next(b for b in manifest["batches"] if b["table"] == "gene_x_genome")
    assert link["key_column"] == "subject_hash" and link["method_names"] == [fx.M_BAKTA]


def test_golden_fixture_round_trips_a_rebuild_on_content_and_batch_ids(tmp_path):
    golden = json.loads((FIXTURE / "delivery.json").read_text())
    rebuilt = fx.build(tmp_path / "rebuilt")
    assert rebuilt["delivery_id"] == golden["delivery_id"]
    assert rebuilt["schemas"] == golden["schemas"]
    strip = lambda m: [{k: b[k] for k in b if k not in ("sha256", "bytes")} for b in m["batches"]]
    assert strip(rebuilt) == strip(golden)


def test_golden_fixture_rows_pass_the_same_row_rules_as_register_links():
    lake = _lake()
    cap = _cap(lake)
    for table in schema.JOIN_TABLES:
        assert _register(cap, table, _rows_for(table))["written"] >= 1


def test_golden_fixture_ingests_through_the_consumer(tmp_path):
    work = tmp_path / "copy"
    shutil.copytree(FIXTURE, work)
    cd.write_run_inventory(work)
    lake = _MiniLake()
    report = _cap(lake).ingest_shards(work)
    assert {b["action"] for b in report["tables"]} == {"ingest"}


def test_run_inventory_carries_method_and_link_entries(tmp_path):
    work = tmp_path / "copy"
    shutil.copytree(FIXTURE, work)
    run = json.loads(cd.write_run_inventory(work).read_text())
    by_table = {b["table"]: b for b in run["batches"]}
    assert by_table["method"]["key_column"] == "method_name"
    assert by_table["gene_x_protein"]["key_column"] == "subject_hash"
    assert by_table["gene_x_protein"]["method_names"] == [fx.M_BAKTA]
    entries = ci.derive_execution_inventory(run, execution_id="E")
    derived = {e["table"]: e for e in entries}
    assert derived["gene_x_protein"]["method_names"] == [fx.M_BAKTA]
    assert [e["table"] for e in entries][:2] == ["parameter_set", "method"]
    assert [e["table"] for e in entries][-1] in schema.JOIN_TABLES


def _mutated(tmp_path, mutate):
    work = tmp_path / "mut"
    shutil.copytree(FIXTURE, work)
    path = work / cd.DELIVERY_FILE
    manifest = json.loads(path.read_text())
    mutate(manifest, work)
    path.write_text(json.dumps(manifest))
    return work


@pytest.mark.parametrize(
    "mutate,pattern",
    [
        (lambda m, w: m.update(delivery_schema_version=2), "delivery_schema_version"),
        (lambda m, w: m["batches"].reverse(), "order must be|dependency order"),
        (lambda m, w: m["batches"][3].update(sha256="0" * 64), "sha256"),
        (lambda m, w: m["batches"][3].update(content_sha256="0" * 64), "content_sha256"),
        (lambda m, w: m["batches"][3].update(batch_id="0" * 26), "batch"),
        (lambda m, w: m["batches"][-1].update(method_names=["x@1/y"]), "method_names"),
        (lambda m, w: m["schemas"].update(method=[["method_name", "STRING"]]), "schema"),
        (lambda m, w: m["batches"][1].update(hash_min="a@1/a"), "hash_min"),
    ],
)
def test_validate_delivery_refuses_contract_violations(tmp_path, mutate, pattern):
    with pytest.raises(cd.DeliveryError, match=pattern):
        cd.validate_delivery(_mutated(tmp_path, mutate))


def test_validate_delivery_refuses_a_link_citing_a_method_not_delivered(tmp_path):
    rows = [(t, r) for t, r in fx.table_rows()]
    rows = [(t, [m for m in r if m["method_name"] != fx.M_KOFAM] if t == "method" else r) for t, r in rows]
    cd.build_delivery(tmp_path / "d", rows, producer="p", observed_at=fx.OBSERVED_AT, standardizer_version="2.0")
    with pytest.raises(cd.DeliveryError, match="not in this delivery"):
        cd.validate_delivery(tmp_path / "d")


def test_validate_delivery_refuses_an_orphan_endpoint(tmp_path):
    rows = [(t, [r for r in rs if r["entity_hash"] != fx.P1] if t == "protein_entity" else rs) for t, rs in fx.table_rows()]
    rows = [(t, rs) for t, rs in rows if not (t == "protein_entity" and not rs)]
    cd.build_delivery(tmp_path / "d", rows, producer="p", observed_at=fx.OBSERVED_AT, standardizer_version="2.0")
    with pytest.raises(cd.DeliveryError, match="orphan"):
        cd.validate_delivery(tmp_path / "d")


def test_validate_delivery_refuses_an_out_of_order_symmetric_row(tmp_path):
    rows = []
    for t, rs in fx.table_rows():
        if t == "genome_x_genome":
            rs = [dict(rs[0], subject_hash=rs[0]["object_hash"], object_hash=rs[0]["subject_hash"])]
        rows.append((t, rs))
    cd.build_delivery(tmp_path / "d", rows, producer="p", observed_at=fx.OBSERVED_AT, standardizer_version="2.0")
    with pytest.raises(cd.DeliveryError, match="byte order"):
        cd.validate_delivery(tmp_path / "d")


def test_batch_id_derivation_is_deterministic_and_ulid_shaped():
    digest = cd.content_sha256([{"a": "1", "ingest_batch_id": "ignored"}])
    assert digest == cd.content_sha256([{"a": "1", "ingest_batch_id": "other"}])
    bid = cd.batch_id_for("method", digest)
    assert ci.ULID_RE.match(bid) and bid == cd.batch_id_for("method", digest)
    assert bid != cd.batch_id_for("gene_x_protein", digest)


# ---------------------------------------------------------------------------
# entity_ids
# ---------------------------------------------------------------------------

HA = "aa" * 32
HB = "bb" * 32


def _ids_cap(rows):
    fake = _ReadFake(rows)
    return ClearinghouseCapability(capability=fake), fake


def test_entity_ids_lists_prefixed_present_ids_sorted():
    rows = [
        alias_row(HA, "present", 1, src="gtdb_accession", aid="GB_GCA_1.1"),
        alias_row(HA, "present", 1, src="ncbi_assembly", aid="GCF_1.1"),
        alias_row(HA, "present", 1, src="cdm_genome", aid="CDM-1"),
        alias_row(HA, "present", 1, src="fitness_org", aid="Keio"),
        alias_row(HA, "present", 1, src="img_taxon", aid="2500"),
        alias_row(HA, "present", 1, src="bvbrc_genome", aid="83333.1"),
        alias_row(HA, "present", 1, src="ncbi_taxon", aid="562"),  # no display prefix
        alias_row(HB, "present", 1, src="gtdb_accession", aid="OTHER"),
    ]
    cap, fake = _ids_cap(rows)
    assert cap.entity_ids("genome", HA) == [
        "CDM:CDM-1", "FB:Keio", "GTDB:GB_GCA_1.1", "IMG:2500", "NCBI:GCF_1.1", "PATRIC:83333.1"]
    assert all("all_alias" not in sql and "CREATE" not in sql.upper() for sql in fake.sql)
    assert all("genome_alias" in sql for sql in fake.sql)


def test_entity_ids_drops_withdrawn_and_ids_reassigned_elsewhere():
    rows = [
        alias_row(HA, "present", 1, src="gtdb_accession", aid="WITHDRAWN"),
        alias_row(HA, "withdrawn", 2, src="gtdb_accession", aid="WITHDRAWN"),
        alias_row(HA, "present", 1, src="gtdb_accession", aid="MOVED"),
        alias_row(HB, "present", 2, src="gtdb_accession", aid="MOVED"),
        alias_row(HA, "present", 1, src="gtdb_accession", aid="KEPT"),
    ]
    cap, _ = _ids_cap(rows)
    assert cap.entity_ids("genome", HA) == ["GTDB:KEPT"]
    assert cap.entity_ids("genome", HB) == ["GTDB:MOVED"]


def test_entity_ids_protein_prefixes():
    rows = [alias_row(HA, "present", 1, src=s, aid=a, etype="protein") for s, a in
            [("cdm_protein", "P1"), ("uniref90", "U90"), ("uniref50", "U50"), ("uniparc", "UPI1"), ("protein_md5", "ab")]]
    cap, _ = _ids_cap(rows)
    assert cap.entity_ids("protein", HA) == ["CDM:P1", "MD5:ab", "UniParc:UPI1", "UniRef50:U50", "UniRef90:U90"]


def test_entity_ids_unknown_entity_type_raises():
    cap, _ = _ids_cap([])
    with pytest.raises(ValueError):
        cap.entity_ids("bogus", HA)
