"""Tests for the ``lakehouse`` source adapter (clearinghouse shard stage).

Pure logic, no pod, no network, no credentials -- a hand-built fake capability
stands in for the whole lakehouse read boundary, exactly as
``test_clearinghouse_bootstrap.py`` fakes the load boundary. These tests cover:

  - The adapter is registered and constructible via ``get_adapter`` (no longer
    planned-but-unbuilt), and reads through ``BerdlCapability.query()``.
  - It reads a lake table by a FQN built from NAMED namespace constants, quoted
    with double quotes (Trino), never backticks.
  - In-pod it issues one ``SELECT *``; off-pod it PAGES with limit/offset and
    keeps paging on an exactly-full page (never truncating), stopping on a short
    page.
  - It emits NO INSERT/MERGE -- only SELECT/COUNT reads.
  - End to end through the sharder: rows read from a fake lake table shard to
    bronze parquet whose ``entity_hash`` matches the REAL standardizer, with the
    genome ``fasta_reference`` left UNPOPULATED (OP-A: no FASTA pointer in the
    source), never a substitute value.
"""

from __future__ import annotations

import pyarrow.parquet as pq
import pytest

from kbutillib.domains.identity import standardizers
from kbutillib.domains.kbase.berdl import clearinghouse_manifest as cm
from kbutillib.domains.kbase.berdl import clearinghouse_schema as schema
from kbutillib.domains.kbase.berdl import clearinghouse_shard as cs


class FakeCapability:
    """A stand-in for ``BerdlCapability`` over the read boundary.

    Records every SQL it is asked to run (for the no-INSERT/quoting assertions),
    returns canned rows, and can pretend to be in-pod or off-pod. Off-pod it
    honours limit/offset over a fixed row list so the paging loop is exercised
    exactly as it would be against the REST transport.
    """

    def __init__(self, rows, *, locus="in_pod", count=None):
        self._rows = list(rows)
        self._locus = locus
        self._count = len(self._rows) if count is None else count
        self.queries: list[dict] = []

    def locus(self) -> str:
        return self._locus

    def query(self, sql, *, params=None, limit=None, offset=0, **kwargs):
        self.queries.append(
            {"sql": sql, "params": params, "limit": limit, "offset": offset}
        )
        if "COUNT(*)" in sql.upper():
            rows = [{"n": self._count}]
        else:
            rows = self._rows
            if limit is not None:
                rows = rows[offset : offset + limit]
        if self._locus == "off_pod":
            # Mimic the REST transport's {'success', 'data', ...} envelope.
            return {"success": True, "data": rows}
        return rows


def _lakehouse_source(**locator):
    base = {"table": "genome_quality"}
    base.update(locator)
    return cm.Source(
        name="genome-lake",
        adapter="lakehouse",
        entity_type="genome",
        kinds=("entity",),
        locator=base,
        hash_spec={"precomputed": "genome_hash"},
        content={},
        result={},
    )


# --------------------------------------------------------------------------
# Registration + construction
# --------------------------------------------------------------------------


def test_lakehouse_is_registered_and_constructible():
    """`lakehouse` is now a real adapter -- get_adapter returns the class, not
    an AdapterNotImplementedError."""
    adapter = cs.get_adapter(_lakehouse_source())
    assert isinstance(adapter, cs.LakehouseSourceAdapter)
    assert adapter.adapter_name == "lakehouse"


def test_only_mongo_remains_planned_but_unbuilt():
    """After this task, only `mongo` is planned-but-unbuilt; `lakehouse` is built."""
    source = cm.Source(
        name="x",
        adapter="mongo",
        entity_type="gene",
        kinds=("entity",),
        locator={},
        hash_spec={"raw_column": "seq"},
        content={},
        result={},
    )
    with pytest.raises(cs.AdapterNotImplementedError):
        cs.get_adapter(source)


def test_missing_table_locator_is_rejected():
    source = _lakehouse_source()
    source.locator.pop("table")
    adapter = cs.LakehouseSourceAdapter(source, capability=FakeCapability([]))
    with pytest.raises(cs.ManifestSourceError) as exc:
        adapter.estimate_bytes()
    assert "table" in str(exc.value)


# --------------------------------------------------------------------------
# It reads through query() by a FQN from NAMED constants, double-quoted
# --------------------------------------------------------------------------


def test_default_namespace_is_the_named_source_constant():
    """With no explicit namespace, the FQN uses the named source constant
    (kbaseincubator.genome_clearhouse), never a literal at the call site."""
    cap = FakeCapability([{"genome_hash": "a" * 64}])
    adapter = cs.LakehouseSourceAdapter(_lakehouse_source(), capability=cap)
    list(adapter.iter_records())
    select = next(q for q in cap.queries if q["sql"].startswith("SELECT *"))
    # Each segment of the dotted namespace + table is double-quoted (Trino),
    # NOT backticked.
    assert '"kbaseincubator"."genome_clearhouse"."genome_quality"' in select["sql"]
    assert "`" not in select["sql"]
    # And the constant itself is what supplied the namespace.
    assert schema.SOURCE_GENOME_CLEARHOUSE_NAMESPACE == "kbaseincubator.genome_clearhouse"


def test_explicit_namespace_locator_is_honoured():
    cap = FakeCapability([{"genome_hash": "b" * 64}])
    source = _lakehouse_source(namespace="kbaseincubator.other_ns", table="t1")
    adapter = cs.LakehouseSourceAdapter(source, capability=cap)
    list(adapter.iter_records())
    select = next(q for q in cap.queries if q["sql"].startswith("SELECT *"))
    assert '"kbaseincubator"."other_ns"."t1"' in select["sql"]


def test_no_insert_or_merge_is_ever_emitted():
    """The adapter is read-side: every SQL it runs is a SELECT/COUNT read;
    no INSERT or MERGE targeting any table appears."""
    cap = FakeCapability([{"genome_hash": "c" * 64}], count=3)
    adapter = cs.LakehouseSourceAdapter(_lakehouse_source(), capability=cap)
    adapter.estimate_bytes()
    list(adapter.iter_records())
    assert cap.queries  # something ran
    for q in cap.queries:
        upper = q["sql"].upper()
        assert "INSERT" not in upper
        assert "MERGE" not in upper
        assert upper.lstrip().startswith("SELECT")


# --------------------------------------------------------------------------
# Paging: in-pod one shot; off-pod pages, never truncating on a full page
# --------------------------------------------------------------------------


def test_in_pod_reads_in_one_select_no_paging():
    rows = [{"genome_hash": f"{i:064x}"} for i in range(10)]
    cap = FakeCapability(rows, locus="in_pod")
    adapter = cs.LakehouseSourceAdapter(_lakehouse_source(), capability=cap)
    out = list(adapter.iter_records())
    assert len(out) == 10
    selects = [q for q in cap.queries if q["sql"].startswith("SELECT *")]
    assert len(selects) == 1
    # In-pod issues no limit/offset paging.
    assert selects[0]["limit"] is None


def test_off_pod_pages_and_does_not_truncate_on_full_page(monkeypatch):
    """Off-pod paging keeps going while a page is EXACTLY the cap length and
    stops only on a short page -- a full page must not end the stream."""
    # Force a tiny page size so a small fixture spans several pages.
    monkeypatch.setattr(cs, "OFF_POD_PAGE_SIZE", 3)
    rows = [{"genome_hash": f"{i:064x}"} for i in range(7)]  # 3 + 3 + 1
    cap = FakeCapability(rows, locus="off_pod")
    adapter = cs.LakehouseSourceAdapter(_lakehouse_source(), capability=cap)
    out = list(adapter.iter_records())
    # All 7 rows read despite two exactly-full pages preceding the short one.
    assert [r["genome_hash"] for r in out] == [r["genome_hash"] for r in rows]
    paged = [q for q in cap.queries if q["sql"].startswith("SELECT *")]
    # Pages at offsets 0, 3, 6 (the last returns 1 < 3 and stops).
    assert [q["offset"] for q in paged] == [0, 3, 6]
    assert all(q["limit"] == 3 for q in paged)
    # Off-pod paging orders by the hash column for a stable window.
    assert 'ORDER BY "genome_hash"' in paged[0]["sql"]


def test_off_pod_exact_multiple_stops_on_trailing_empty_page(monkeypatch):
    """When the row count is an exact multiple of the page size, the last full
    page is followed by an empty page that ends the stream (no infinite loop)."""
    monkeypatch.setattr(cs, "OFF_POD_PAGE_SIZE", 3)
    rows = [{"genome_hash": f"{i:064x}"} for i in range(6)]  # 3 + 3 + 0
    cap = FakeCapability(rows, locus="off_pod")
    adapter = cs.LakehouseSourceAdapter(_lakehouse_source(), capability=cap)
    out = list(adapter.iter_records())
    assert len(out) == 6
    paged = [q for q in cap.queries if q["sql"].startswith("SELECT *")]
    assert [q["offset"] for q in paged] == [0, 3, 6]


# --------------------------------------------------------------------------
# estimate_bytes uses a cheap COUNT(*)
# --------------------------------------------------------------------------


def test_estimate_bytes_uses_count_star():
    cap = FakeCapability([], count=5000)
    adapter = cs.LakehouseSourceAdapter(_lakehouse_source(), capability=cap)
    est = adapter.estimate_bytes()
    assert est == 5000 * cs.LAKEHOUSE_BYTES_PER_ROW
    count_q = next(q for q in cap.queries if "COUNT(*)" in q["sql"].upper())
    assert '"kbaseincubator"."genome_clearhouse"."genome_quality"' in count_q["sql"]


def test_estimate_bytes_is_at_least_one_on_empty_source():
    cap = FakeCapability([], count=0)
    adapter = cs.LakehouseSourceAdapter(_lakehouse_source(), capability=cap)
    assert adapter.estimate_bytes() >= 1


# --------------------------------------------------------------------------
# End to end through the sharder: fasta_reference stays UNPOPULATED (OP-A)
# --------------------------------------------------------------------------


def _shard_one(plan, adapter, out_root):
    return cs.shard_source(plan, adapter, out_root, target_bytes=64)


def test_lakehouse_genome_shards_with_fasta_reference_unpopulated(tmp_path, monkeypatch):
    """A genome source read via the lakehouse adapter shards to bronze parquet.

    OP-A found NO FASTA pointer in kbaseincubator.genome_clearhouse, so a genome
    manifest maps no fasta_reference; the column exists in the schema but is left
    NULL for every row -- never a substitute value. The precomputed hash is
    re-canonicalised (lowercased) through encode_entity_hash, matching the real
    boundary helper.
    """
    # Inject the fake capability into whatever the sharder constructs, by
    # patching get_adapter to build a lakehouse adapter over the fake.
    upper_hashes = ["A" * 64, "B" * 64, "C" * 64]
    rows = [{"genome_hash": h, "genome_id": f"SAMN{i}"} for i, h in enumerate(upper_hashes)]
    cap = FakeCapability(rows, locus="in_pod")

    toml_text = """
    [[source]]
    name = "genome-lake"
    adapter = "lakehouse"
    table = "genome_quality"
    entity_type = "genome"
    kinds = ["content"]
    [source.hash]
    precomputed = "genome_hash"
    [source.content]
    taxon_id = "@const(unknown)"
    """
    manifest = cm.load_manifest(toml_text)
    plans = cm.shard_plan(manifest)
    (plan,) = [p for p in plans if p.kind == "content"]

    adapter = cs.LakehouseSourceAdapter(plan_source(manifest, plan), capability=cap)
    reports = _shard_one(plan, adapter, tmp_path / "bronze")

    # Read every written row of genome_content back.
    written = []
    for report in reports:
        table = pq.read_table(str(report.path))
        written.extend(table.to_pylist())

    assert len(written) == 3
    # entity_hash re-canonicalised to lowercase, matching encode_entity_hash.
    got_hashes = sorted(r["entity_hash"] for r in written)
    assert got_hashes == sorted(schema.encode_entity_hash(h) for h in upper_hashes)
    # fasta_reference is present in the schema but UNPOPULATED (None) -- never a
    # substitute value (no empty string, no synthesized URL, no accession).
    for r in written:
        assert "fasta_reference" in r
        assert r["fasta_reference"] is None


def plan_source(manifest, plan):
    for source in manifest.sources:
        if source.name == plan.source_name:
            return source
    raise AssertionError("source not found for plan")


def test_fasta_reference_is_a_real_content_column_left_null():
    """Guard the OP-A finding at the schema level: genome_content DOES carry a
    fasta_reference column (so leaving it null is a deliberate omission, not a
    missing column), and no other genome content column stands in for it."""
    columns = dict(schema.table_columns("genome", "content"))
    assert "fasta_reference" in columns
    assert columns["fasta_reference"] == "STRING"
