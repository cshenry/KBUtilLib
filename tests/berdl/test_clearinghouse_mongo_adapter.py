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
  - The ``join`` locator merges a SECOND collection's fields into every record
    with exactly ONE ``$in`` lookup per page (never one per record), projects
    only the fields it maps, zlib-decodes the ones it declares, and SKIPS AND
    COUNTS a record with no join match under ``no-join-match``.
  - The ``require_fields`` locator skips and counts a record missing a field,
    or holding ``None`` / an empty value in one, under ``missing:<field>`` --
    the ~28% ``_id``-only stubs of ``protein_to_rast2`` -- while leaving a
    legitimate ``0`` alone.
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

    def find(self, query_filter, projection=None, *, batch_size=None):
        recorder: dict = {
            "filter": query_filter,
            "projection": projection,
            "batch_size": batch_size,
        }
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
        if "$in" in cond and value not in cond["$in"]:
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


def _annotation_source(**locator):
    """A ``protein_result`` source over an annotation collection.

    The shape the ``join`` locator exists for: the primary collection carries
    the annotation and no sequence, so ``[source.hash]`` names a column only
    the join can supply.
    """
    base = {"database": "database", "collection": "seq_protein_kofam"}
    base.update(locator)
    return cm.Source(
        name="kofam-mongo",
        adapter="mongo",
        entity_type="protein",
        kinds=("result",),
        locator=base,
        hash_spec={"raw_column": "sequence"},
        content={},
        result={
            "result_type": "@const(annotation)",
            "source": "@const(kofam/mongo-seq_protein_kofam)",
            "result_type_version": "@const(1.0)",
            "payload": "@json(ko)",
            "parameter_set": {},
        },
    )


_JOIN = {"collection": "seq_protein", "fields": {"sequence": "z_seq"}}
_JOIN_ZLIB = dict(_JOIN, decompress=["sequence"])


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


# --------------------------------------------------------------------------
# The `join` locator: one $in per page
# --------------------------------------------------------------------------


def _ids(n, start=0):
    return [f"{i:064x}" for i in range(start, start + n)]


def test_join_issues_exactly_one_in_query_per_page():
    """The join is ONE lookup per page, not one per record.

    Seven primary documents at a page size of three is three pages, so three
    ``$in`` queries -- and every id of each page appears in exactly one of
    them, so nothing is looked up twice or missed.
    """
    ids = _ids(7)
    primary = FakeCollection([{"_id": i, "ko": "K00001"} for i in ids])
    join = FakeCollection([{"_id": i, "z_seq": f"MK{i[-1]}"} for i in ids])
    adapter = cs.MongoSourceAdapter(
        _annotation_source(join=_JOIN, batch_size=3),
        collection=primary,
        join_collection=join,
    )
    rows = list(adapter.iter_records())

    assert len(rows) == 7
    # Three pages of 3, 3, 1 -- three lookups, not seven.
    assert len(join.finds) == 3
    queried = []
    for find, expected in zip(join.finds, (3, 3, 1)):
        in_list = find["filter"]["_id"]["$in"]
        assert len(in_list) == expected
        queried.extend(in_list)
    # Every id looked up exactly once, across the three pages.
    assert sorted(queried) == sorted(ids)
    # The projection moves `_id` and the joined field, and nothing else.
    assert join.finds[0]["projection"] == {"_id": 1, "z_seq": 1}


def test_join_merges_the_looked_up_fields_into_each_record():
    primary = FakeCollection([{"_id": "a" * 64, "ko": "K00001"}])
    join = FakeCollection([{"_id": "a" * 64, "z_seq": "MKQL"}])
    adapter = cs.MongoSourceAdapter(
        _annotation_source(join=_JOIN), collection=primary, join_collection=join
    )
    (row,) = list(adapter.iter_records())
    # The annotation's own fields survive, and the joined field arrives under
    # its OUTPUT name (`sequence`), not the join collection's (`z_seq`).
    assert row["ko"] == "K00001"
    assert row["sequence"] == "MKQL"
    assert "z_seq" not in row


def test_join_decompresses_the_fields_it_declares():
    """A joined `z_seq` blob is zlib-decoded in place under its output name."""
    seq = "MKAILVGADTPRQWY"
    primary = FakeCollection([{"_id": "b" * 64, "ko": "K00002"}])
    join = FakeCollection(
        [{"_id": "b" * 64, "z_seq": zlib.compress(seq.encode("utf-8"))}]
    )
    adapter = cs.MongoSourceAdapter(
        _annotation_source(join=_JOIN_ZLIB),
        collection=primary,
        join_collection=join,
    )
    (row,) = list(adapter.iter_records())
    assert row["sequence"] == seq


def test_join_miss_is_skipped_and_counted():
    """A record with no document in the join collection is dropped and counted.

    An annotation whose protein is absent from `seq_protein` cannot be tied to
    a protein identity at all, so it is a counted drop rather than a fabricated
    hash.
    """
    ids = _ids(4)
    primary = FakeCollection([{"_id": i, "ko": "K1"} for i in ids])
    # Only two of the four proteins exist in the join collection.
    join = FakeCollection([{"_id": ids[0], "z_seq": "M"}, {"_id": ids[2], "z_seq": "K"}])
    adapter = cs.MongoSourceAdapter(
        _annotation_source(join=_JOIN), collection=primary, join_collection=join
    )
    rows = list(adapter.iter_records())
    assert [r["_id"] for r in rows] == [ids[0], ids[2]]
    assert adapter.skipped() == {cs.MONGO_SKIP_NO_JOIN_MATCH: 2}
    # A join miss is not a short read: the cursor still ran off the end.
    assert adapter.exhausted() is True


def test_join_document_missing_the_projected_field_sets_no_output_field():
    """A match that exists but carries no `z_seq` yields no `sequence`.

    The same rule `decompress` applies to a stub: the adapter never fabricates
    a value, and the mapping fails that record instead.
    """
    primary = FakeCollection([{"_id": "c" * 64, "ko": "K1"}])
    join = FakeCollection([{"_id": "c" * 64}])
    adapter = cs.MongoSourceAdapter(
        _annotation_source(join=_JOIN_ZLIB),
        collection=primary,
        join_collection=join,
    )
    (row,) = list(adapter.iter_records())
    assert "sequence" not in row
    assert adapter.skipped() == {}


def test_join_needs_an_injected_join_collection_when_the_primary_is_injected():
    """Injecting the primary collection builds no client, so the join
    collection cannot be resolved -- and says so by name."""
    primary = FakeCollection([{"_id": "d" * 64, "ko": "K1"}])
    adapter = cs.MongoSourceAdapter(
        _annotation_source(join=_JOIN), collection=primary
    )
    with pytest.raises(cs.ManifestSourceError) as exc:
        list(adapter.iter_records())
    assert "join_collection" in str(exc.value)


def test_join_on_defaults_to_id_and_is_overridable():
    primary = FakeCollection([{"_id": "e" * 64, "md5": "m1", "ko": "K1"}])
    join = FakeCollection([{"md5": "m1", "z_seq": "MK"}])
    adapter = cs.MongoSourceAdapter(
        _annotation_source(join=dict(_JOIN, on="md5")),
        collection=primary,
        join_collection=join,
    )
    (row,) = list(adapter.iter_records())
    assert row["sequence"] == "MK"
    assert join.finds[0]["filter"] == {"md5": {"$in": ["m1"]}}
    assert join.finds[0]["projection"] == {"md5": 1, "z_seq": 1}
    # The default, for comparison.
    assert cs.MongoSourceAdapter(
        _annotation_source(join=_JOIN)
    )._join_spec().on == cs.MONGO_JOIN_DEFAULT_ON


def test_duplicate_join_keys_in_one_page_are_looked_up_once():
    """Two annotations of one protein cost one entry in the $in list."""
    shared = "f" * 64
    primary = FakeCollection(
        [{"_id": shared, "ko": "K1"}, {"_id": shared, "ko": "K2"}]
    )
    join = FakeCollection([{"_id": shared, "z_seq": "MK"}])
    adapter = cs.MongoSourceAdapter(
        _annotation_source(join=_JOIN), collection=primary, join_collection=join
    )
    rows = list(adapter.iter_records())
    assert len(rows) == 2
    assert join.finds[0]["filter"] == {"_id": {"$in": [shared]}}


@pytest.mark.parametrize(
    "bad",
    [
        "seq_protein",
        {"fields": {"sequence": "z_seq"}},
        {"collection": "seq_protein"},
        {"collection": "seq_protein", "fields": {}},
        {"collection": "seq_protein", "fields": {"sequence": 1}},
        {"collection": "seq_protein", "fields": {"sequence": "z_seq"}, "on": ""},
        {"collection": "seq_protein", "fields": {"sequence": "z_seq"}, "field": {}},
        {
            "collection": "seq_protein",
            "fields": {"sequence": "z_seq"},
            "decompress": "sequence",
        },
        {
            "collection": "seq_protein",
            "fields": {"sequence": "z_seq"},
            # Names the join collection's own field, not the OUTPUT name.
            "decompress": ["z_seq"],
        },
    ],
)
def test_bad_join_spec_is_rejected(bad):
    adapter = cs.MongoSourceAdapter(
        _annotation_source(join=bad), collection=FakeCollection([])
    )
    with pytest.raises(cs.ManifestSourceError):
        list(adapter.iter_records())


# --------------------------------------------------------------------------
# The `require_fields` locator
# --------------------------------------------------------------------------


def test_require_fields_drops_stubs_and_counts_them_by_field():
    """~28% of protein_to_rast2 is _id-only stubs; each is a counted drop."""
    docs = [
        {"_id": _ids(1, 0)[0], "function": "hypothetical protein"},
        {"_id": _ids(1, 1)[0]},  # a stub: no `function` at all
        {"_id": _ids(1, 2)[0], "function": None},  # present but null
        {"_id": _ids(1, 3)[0], "function": ""},  # present but empty
        {"_id": _ids(1, 4)[0], "function": "DNA gyrase"},
    ]
    source = _annotation_source(
        collection="protein_to_rast2", require_fields=["function"]
    )
    adapter = cs.MongoSourceAdapter(source, collection=FakeCollection(docs))
    rows = list(adapter.iter_records())
    assert [r["function"] for r in rows] == ["hypothetical protein", "DNA gyrase"]
    assert adapter.skipped() == {f"{cs.MONGO_SKIP_MISSING_PREFIX}function": 3}


def test_require_fields_treats_an_empty_list_as_missing_but_keeps_a_zero():
    """`kos` was null in the sampled kofam2 document, and an empty list says the
    same thing -- but a legitimate 0 is a recorded value and must survive."""
    docs = [
        {"_id": _ids(1, 0)[0], "kos": ["K00001"]},
        {"_id": _ids(1, 1)[0], "kos": []},
        {"_id": _ids(1, 2)[0], "kos": {}},
        {"_id": _ids(1, 3)[0], "kos": 0},
        {"_id": _ids(1, 4)[0], "kos": False},
    ]
    source = _annotation_source(
        collection="seq_protein_kofam2", require_fields=["kos"]
    )
    adapter = cs.MongoSourceAdapter(source, collection=FakeCollection(docs))
    rows = list(adapter.iter_records())
    assert [r["kos"] for r in rows] == [["K00001"], 0, False]
    assert adapter.skipped() == {f"{cs.MONGO_SKIP_MISSING_PREFIX}kos": 2}


def test_each_record_is_counted_under_the_first_requirement_it_fails():
    """So the counts sum to records dropped, not to failed (record, field) pairs."""
    docs = [{"_id": _ids(1, 0)[0]}]
    source = _annotation_source(require_fields=["ko", "other"])
    adapter = cs.MongoSourceAdapter(source, collection=FakeCollection(docs))
    assert list(adapter.iter_records()) == []
    assert adapter.skipped() == {f"{cs.MONGO_SKIP_MISSING_PREFIX}ko": 1}


def test_a_primary_only_requirement_is_checked_before_the_join():
    """A stub never costs a sequence lookup: the $in list holds survivors only.

    At a 28% stub rate over 399.9M documents this is ~112M lookups not issued,
    which is the whole reason the check runs where it does.
    """
    ids = _ids(4)
    primary = FakeCollection(
        [
            {"_id": ids[0], "function": "DNA gyrase"},
            {"_id": ids[1]},
            {"_id": ids[2]},
            {"_id": ids[3], "function": "RecA"},
        ]
    )
    join = FakeCollection([{"_id": i, "z_seq": "MK"} for i in ids])
    source = _annotation_source(
        collection="protein_to_rast2",
        require_fields=["function"],
        join=_JOIN,
    )
    adapter = cs.MongoSourceAdapter(
        source, collection=primary, join_collection=join
    )
    rows = list(adapter.iter_records())
    assert len(rows) == 2
    # Only the two surviving ids were looked up.
    assert join.finds[0]["filter"] == {"_id": {"$in": [ids[0], ids[3]]}}
    assert adapter.skipped() == {f"{cs.MONGO_SKIP_MISSING_PREFIX}function": 2}


def test_a_requirement_the_join_produces_is_checked_after_the_merge():
    """It cannot be judged earlier, and reports the same reason key either way."""
    ids = _ids(2)
    primary = FakeCollection([{"_id": i, "ko": "K1"} for i in ids])
    # The second protein's join document exists but carries no blob.
    join = FakeCollection([{"_id": ids[0], "z_seq": "MK"}, {"_id": ids[1]}])
    source = _annotation_source(require_fields=["sequence"], join=_JOIN)
    adapter = cs.MongoSourceAdapter(
        source, collection=primary, join_collection=join
    )
    rows = list(adapter.iter_records())
    assert [r["_id"] for r in rows] == [ids[0]]
    assert adapter.skipped() == {f"{cs.MONGO_SKIP_MISSING_PREFIX}sequence": 1}


def test_a_requirement_a_primary_decompress_produces_is_checked_after_it():
    primary = FakeCollection(
        [
            {"_id": _ids(1, 0)[0], "z_seq": "MK"},
            {"_id": _ids(1, 1)[0]},  # no blob -> no `sequence` after decode
        ]
    )
    source = _mongo_source(
        decompress={"sequence": "z_seq"}, require_fields=["sequence"]
    )
    adapter = cs.MongoSourceAdapter(source, collection=primary)
    rows = list(adapter.iter_records())
    assert [r["sequence"] for r in rows] == ["MK"]
    assert adapter.skipped() == {f"{cs.MONGO_SKIP_MISSING_PREFIX}sequence": 1}


@pytest.mark.parametrize("bad", ["ko", ["ko", 1], ["ko", ""], 7])
def test_bad_require_fields_is_rejected(bad):
    adapter = cs.MongoSourceAdapter(
        _annotation_source(require_fields=bad), collection=FakeCollection([])
    )
    with pytest.raises(cs.ManifestSourceError):
        list(adapter.iter_records())


def test_a_skipped_record_does_not_spend_the_limit_budget():
    """The --limit allowance counts rows yielded, not documents examined."""

    class _Context:
        def record_limit(self, source_name):
            return 2

    docs = [
        {"_id": _ids(1, 0)[0]},
        {"_id": _ids(1, 1)[0], "ko": "K1"},
        {"_id": _ids(1, 2)[0]},
        {"_id": _ids(1, 3)[0], "ko": "K2"},
        {"_id": _ids(1, 4)[0], "ko": "K3"},
    ]
    source = _annotation_source(require_fields=["ko"])
    adapter = cs.MongoSourceAdapter(source, collection=FakeCollection(docs))
    adapter.bind_run_context(_Context())
    rows = list(adapter.iter_records())
    assert [r["ko"] for r in rows] == ["K1", "K2"]
    # Stopped on the budget, not on the end of the source.
    assert adapter.exhausted() is False


def test_paging_preserves_order_and_completeness_without_a_join():
    """With no join the paging is pure chunking: same documents, same order."""
    ids = _ids(7)
    cap = FakeCollection([{"_id": i, "z_seq": "M"} for i in reversed(ids)])
    adapter = cs.MongoSourceAdapter(_mongo_source(batch_size=3), collection=cap)
    out = list(adapter.iter_records())
    assert [r["_id"] for r in out] == ids
    assert adapter.exhausted() is True
    # One find on the primary collection; the paging is client-side chunking of
    # the cursor, not a second query per page.
    assert len(cap.finds) == 1
