"""Tests for the ``mongo`` source adapter (clearinghouse shard stage).

Pure logic, no live database, no network, no credentials, and no ``pymongo`` or
``mongomock`` dependency -- a hand-built fake collection stands in for the whole
Mongo read boundary, exactly as ``test_clearinghouse_lakehouse_adapter.py`` fakes
the lakehouse boundary and ``test_clearinghouse_bootstrap.py`` fakes the load
boundary. These tests cover:

  - The adapter is registered and constructible via ``get_adapter`` (no longer
    planned-but-unbuilt), and reads through a collection's ``find`` /
    ``estimated_document_count``.
  - It streams a BATCHED, SORTED cursor -- never loading the collection -- and
    is resumable (``start_after``) and parallelisable across disjoint key ranges
    (``min_key`` / ``max_key``, half-open so slices never overlap).
  - It DECOMPRESSES a ``z_seq`` blob into a plain ``sequence`` field the manifest
    can map, tolerates ``_id``-only stub documents, and never fabricates a
    sequence for a stub.
  - Missing required locators (``database``, ``collection``) are rejected.
  - End to end through the sharder: ``seq_protein``-shaped documents (with a real
    zlib blob) shard to bronze ``protein_content`` parquet whose ``entity_hash``
    matches the REAL standardizer over the DECOMPRESSED sequence -- the Mongo
    ``_id`` is never trusted as the hash -- and ``sequence`` / ``seq_length`` are
    populated from the decoded blob.
"""

from __future__ import annotations

import zlib

import pyarrow.parquet as pq
import pytest

from kbutillib.domains.identity import standardizers
from kbutillib.domains.kbase.berdl import clearinghouse_manifest as cm
from kbutillib.domains.kbase.berdl import clearinghouse_schema as schema
from kbutillib.domains.kbase.berdl import clearinghouse_shard as cs


class FakeCursor:
    """A minimal cursor over an in-memory list honouring ``.sort(key, 1)``.

    ``find`` returns one of these; ``.sort`` records the sort call and returns
    ``self`` (chaining, like pymongo), and iteration yields the (optionally
    filtered) documents in sorted order.
    """

    def __init__(self, docs, *, batch_size, recorder):
        self._docs = docs
        self.batch_size = batch_size
        self._recorder = recorder

    def sort(self, key, direction):
        self._recorder["sort"] = (key, direction)
        self._docs = sorted(self._docs, key=lambda d: d[key])
        return self

    def __iter__(self):
        return iter(self._docs)


class FakeCollection:
    """A stand-in for a pymongo ``Collection`` over the read boundary.

    Holds a fixed document list, records the ``find`` filter and batch size (for
    the batching / range / resume assertions), applies the same ``$gt`` /
    ``$gte`` / ``$lt`` operators the real query filter uses, and returns a
    :class:`FakeCursor`. ``estimated_document_count`` returns a cheap metadata
    count.
    """

    def __init__(self, docs, *, count=None):
        self._docs = list(docs)
        self._count = len(self._docs) if count is None else count
        self.finds: list[dict] = []

    def estimated_document_count(self):
        return self._count

    def find(self, query_filter, *, batch_size=None):
        recorder: dict = {"filter": query_filter, "batch_size": batch_size}
        self.finds.append(recorder)
        docs = [d for d in self._docs if _matches(d, query_filter)]
        return FakeCursor(docs, batch_size=batch_size, recorder=recorder)


def _matches(doc, query_filter):
    """Apply the small subset of Mongo query operators the adapter emits."""
    for field, cond in query_filter.items():
        value = doc.get(field)
        if not isinstance(cond, dict):
            if value != cond:
                return False
            continue
        if "$gt" in cond and not value > cond["$gt"]:
            return False
        if "$gte" in cond and not value >= cond["$gte"]:
            return False
        if "$lt" in cond and not value < cond["$lt"]:
            return False
    return True


def _mongo_source(**locator):
    base = {"database": "database", "collection": "seq_protein"}
    base.update(locator)
    return cm.Source(
        name="protein-mongo",
        adapter="mongo",
        entity_type="protein",
        kinds=("content",),
        locator=base,
        hash_spec={"raw_column": "sequence"},
        content={"sequence": "sequence", "seq_length": "@len(sequence)"},
        result={},
    )


# --------------------------------------------------------------------------
# Registration + construction
# --------------------------------------------------------------------------


def test_mongo_is_registered_and_constructible():
    """`mongo` is now a real adapter -- get_adapter returns the class, not an
    AdapterNotImplementedError."""
    adapter = cs.get_adapter(_mongo_source())
    assert isinstance(adapter, cs.MongoSourceAdapter)
    assert adapter.adapter_name == "mongo"


def test_no_planned_adapter_remains_unbuilt():
    """After this task, every name in ADAPTER_NAMES maps to a real class, so
    get_adapter never raises AdapterNotImplementedError for a declared name."""
    for name in cs.ADAPTER_NAMES:
        assert name in cs._ADAPTER_CLASSES


def test_construction_imports_no_driver_and_opens_no_connection():
    """Building the adapter (as get_adapter does during validation) neither
    imports pymongo nor connects -- the collection stays unresolved until a read.
    """
    adapter = cs.get_adapter(_mongo_source())
    # No collection injected and no read performed: the lazy handle is still None.
    assert adapter._collection is None
    assert adapter._client is None


# --------------------------------------------------------------------------
# Missing required locators
# --------------------------------------------------------------------------


def test_missing_database_locator_is_rejected():
    source = _mongo_source()
    source.locator.pop("database")
    adapter = cs.MongoSourceAdapter(source, collection=FakeCollection([]))
    with pytest.raises(cs.ManifestSourceError) as exc:
        # database is only needed when connecting; force the code path that
        # reads it by resolving the name directly.
        adapter._database_name()
    assert "database" in str(exc.value)


def test_missing_collection_locator_is_rejected():
    source = _mongo_source()
    source.locator.pop("collection")
    adapter = cs.MongoSourceAdapter(source, collection=FakeCollection([]))
    with pytest.raises(cs.ManifestSourceError) as exc:
        adapter._collection_name()
    assert "collection" in str(exc.value)


# --------------------------------------------------------------------------
# Batched, sorted cursor
# --------------------------------------------------------------------------


def test_reads_batched_and_sorted_ascending_on_sort_key():
    docs = [
        {"_id": f"{i:064x}", "z_seq": "MK"} for i in (3, 1, 2, 0)
    ]
    cap = FakeCollection(docs)
    adapter = cs.MongoSourceAdapter(_mongo_source(batch_size=7), collection=cap)
    out = list(adapter.iter_records())
    # Sorted ascending on _id despite unsorted input.
    assert [r["_id"] for r in out] == sorted(d["_id"] for d in docs)
    # The find carried the requested batch_size (a batched cursor, not a load).
    assert cap.finds[0]["batch_size"] == 7
    # The cursor was sorted ascending on the sort key (recorded in the same dict).
    assert cap.finds[0]["sort"] == ("_id", 1)


def test_default_batch_size_is_the_module_default():
    cap = FakeCollection([{"_id": "a" * 64, "z_seq": "M"}])
    adapter = cs.MongoSourceAdapter(_mongo_source(), collection=cap)
    list(adapter.iter_records())
    assert cap.finds[0]["batch_size"] == cs.MONGO_BATCH_SIZE


def test_nonpositive_batch_size_is_rejected():
    cap = FakeCollection([])
    adapter = cs.MongoSourceAdapter(_mongo_source(batch_size=0), collection=cap)
    with pytest.raises(cs.ManifestSourceError):
        list(adapter.iter_records())


# --------------------------------------------------------------------------
# Resume + parallel range slicing
# --------------------------------------------------------------------------


def test_start_after_resumes_strictly_after_the_key():
    docs = [{"_id": f"{i:064x}", "z_seq": "M"} for i in range(5)]
    cap = FakeCollection(docs)
    resume = f"{2:064x}"
    adapter = cs.MongoSourceAdapter(
        _mongo_source(start_after=resume), collection=cap
    )
    out = list(adapter.iter_records())
    # Strictly greater than the resume key: ids 3 and 4 only.
    assert [r["_id"] for r in out] == [f"{i:064x}" for i in (3, 4)]
    assert cap.finds[0]["filter"] == {"_id": {"$gt": resume}}


def test_min_max_key_is_a_half_open_disjoint_slice():
    """A [min_key, max_key) slice is half-open so adjacent parallel slices meet
    at a boundary without both reading it."""
    docs = [{"_id": f"{i:064x}", "z_seq": "M"} for i in range(6)]
    cap = FakeCollection(docs)
    lo, hi = f"{2:064x}", f"{4:064x}"
    adapter = cs.MongoSourceAdapter(
        _mongo_source(min_key=lo, max_key=hi), collection=cap
    )
    out = list(adapter.iter_records())
    # 2 and 3 included; 4 excluded (belongs to the next slice); nothing overlaps.
    assert [r["_id"] for r in out] == [f"{i:064x}" for i in (2, 3)]
    assert cap.finds[0]["filter"] == {"_id": {"$gte": lo, "$lt": hi}}


def test_two_adjacent_slices_cover_the_space_without_overlap():
    docs = [{"_id": f"{i:064x}", "z_seq": "M"} for i in range(6)]
    mid = f"{3:064x}"
    lower = cs.MongoSourceAdapter(
        _mongo_source(max_key=mid), collection=FakeCollection(docs)
    )
    upper = cs.MongoSourceAdapter(
        _mongo_source(min_key=mid), collection=FakeCollection(docs)
    )
    lower_ids = [r["_id"] for r in lower.iter_records()]
    upper_ids = [r["_id"] for r in upper.iter_records()]
    # Disjoint and together the whole space.
    assert set(lower_ids).isdisjoint(upper_ids)
    assert sorted(lower_ids + upper_ids) == [f"{i:064x}" for i in range(6)]


def test_no_range_reads_the_whole_collection():
    docs = [{"_id": f"{i:064x}", "z_seq": "M"} for i in range(3)]
    cap = FakeCollection(docs)
    adapter = cs.MongoSourceAdapter(_mongo_source(), collection=cap)
    out = list(adapter.iter_records())
    assert len(out) == 3
    assert cap.finds[0]["filter"] == {}


# --------------------------------------------------------------------------
# Decompression + stub tolerance
# --------------------------------------------------------------------------


def test_zseq_blob_is_decompressed_into_a_sequence_field():
    seq = "MKAILVGADTPRQ"
    blob = zlib.compress(seq.encode("utf-8"))
    assert blob[:2] == b"\x78\x9c"  # zlib magic, matching the survey
    cap = FakeCollection([{"_id": "a" * 64, "z_seq": blob}])
    adapter = cs.MongoSourceAdapter(
        _mongo_source(decompress={"sequence": "z_seq"}), collection=cap
    )
    (row,) = list(adapter.iter_records())
    assert row["sequence"] == seq
    # The raw compressed field is dropped once decoded.
    assert "z_seq" not in row


def test_plain_string_zseq_passes_through_for_a_fake_double():
    """A fake double may yield an already-plain sequence; decode is a no-op on a
    str so a test need not compress."""
    cap = FakeCollection([{"_id": "b" * 64, "z_seq": "MKQ"}])
    adapter = cs.MongoSourceAdapter(
        _mongo_source(decompress={"sequence": "z_seq"}), collection=cap
    )
    (row,) = list(adapter.iter_records())
    assert row["sequence"] == "MKQ"


def test_stub_document_missing_the_blob_yields_no_sequence():
    """~28% of annotation docs are _id-only stubs; a doc missing the decompress
    input field is yielded unchanged with no fabricated sequence."""
    cap = FakeCollection([{"_id": "c" * 64}])
    adapter = cs.MongoSourceAdapter(
        _mongo_source(decompress={"sequence": "z_seq"}), collection=cap
    )
    (row,) = list(adapter.iter_records())
    assert "sequence" not in row
    assert row == {"_id": "c" * 64}


def test_bad_decompress_spec_is_rejected():
    cap = FakeCollection([{"_id": "d" * 64, "z_seq": "M"}])
    adapter = cs.MongoSourceAdapter(
        _mongo_source(decompress={"sequence": 123}), collection=cap
    )
    with pytest.raises(cs.ManifestSourceError):
        list(adapter.iter_records())


# --------------------------------------------------------------------------
# estimate_bytes
# --------------------------------------------------------------------------


def test_estimate_bytes_uses_estimated_document_count():
    cap = FakeCollection([], count=420_000)
    adapter = cs.MongoSourceAdapter(_mongo_source(), collection=cap)
    assert adapter.estimate_bytes() == 420_000 * cs.MONGO_BYTES_PER_DOC


def test_estimate_bytes_is_at_least_one_on_empty_source():
    cap = FakeCollection([], count=0)
    adapter = cs.MongoSourceAdapter(_mongo_source(), collection=cap)
    assert adapter.estimate_bytes() >= 1


# --------------------------------------------------------------------------
# End to end through the sharder: hash from the DECOMPRESSED sequence
# --------------------------------------------------------------------------


def _plan_source(manifest, plan):
    for source in manifest.sources:
        if source.name == plan.source_name:
            return source
    raise AssertionError("source not found for plan")


def test_seq_protein_shards_to_protein_content_hashing_the_sequence(tmp_path):
    """A seq_protein-shaped source shards to bronze protein_content.

    The entity_hash is computed by the REAL standardizer over the DECOMPRESSED
    amino-acid sequence -- never the Mongo _id, which is a sha256 of unknown
    provenance -- and sequence / seq_length are populated from the decoded blob.
    """
    seqs = ["MKAILV", "GADTPRQWY", "MSSSHHHH"]
    docs = [
        {"_id": f"id{i}", "z_seq": zlib.compress(s.encode("utf-8"))}
        for i, s in enumerate(seqs)
    ]
    cap = FakeCollection(docs)

    toml_text = """
    [[source]]
    name = "protein-mongo"
    adapter = "mongo"
    database = "database"
    collection = "seq_protein"
    entity_type = "protein"
    kinds = ["content"]
    [source.hash]
    raw_column = "sequence"
    [source.decompress]
    sequence = "z_seq"
    [source.content]
    sequence = "sequence"
    seq_length = "@len(sequence)"
    """
    manifest = cm.load_manifest(toml_text)
    plans = cm.shard_plan(manifest)
    (plan,) = [p for p in plans if p.kind == "content"]

    adapter = cs.MongoSourceAdapter(_plan_source(manifest, plan), collection=cap)
    reports = cs.shard_source(plan, adapter, tmp_path / "bronze", target_bytes=64)

    written = []
    for report in reports:
        table = pq.read_table(str(report.path))
        written.extend(table.to_pylist())

    assert len(written) == 3
    # entity_hash matches the standardizer over the DECOMPRESSED sequence.
    expected = {standardizers.entity_hash("protein", s): s for s in seqs}
    by_hash = {r["entity_hash"]: r for r in written}
    assert set(by_hash) == set(expected)
    for digest, seq in expected.items():
        row = by_hash[digest]
        assert row["sequence"] == seq
        assert row["seq_length"] == len(seq)
    # No _id leaks into the bronze row -- only the mapped content columns.
    for r in written:
        assert "_id" not in r


def test_decompress_is_a_locator_key_not_a_mapping_key(tmp_path):
    """`decompress` lives in the adapter locator, not the mapping half; a manifest
    that declares it does not pollute the parsed content/result blocks."""
    toml_text = """
    [[source]]
    name = "protein-mongo"
    adapter = "mongo"
    database = "database"
    collection = "seq_protein"
    entity_type = "protein"
    kinds = ["content"]
    [source.hash]
    raw_column = "sequence"
    [source.decompress]
    sequence = "z_seq"
    [source.content]
    sequence = "sequence"
    """
    manifest = cm.load_manifest(toml_text)
    (source,) = manifest.sources
    # decompress landed in the locator, untouched by the mapping validator.
    assert source.locator["decompress"] == {"sequence": "z_seq"}
    assert "decompress" not in source.content
    # And it plans cleanly.
    plans = cm.shard_plan(manifest)
    assert [p.kind for p in plans] == ["content"]
