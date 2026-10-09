"""Tests for the shipped Mongo backfill presets (the full protein preload).

Five presets ship as package data under
``src/kbutillib/domains/kbase/berdl/backfills/``: ``mongo-protein-seq``,
``mongo-protein-bakta``, ``mongo-protein-kofam``, ``mongo-protein-kofam2`` and
``mongo-protein-rast2``. None of them can be RUN from here -- they read a live
Mongo 8 store on poplar -- so every assertion below is made against a hand-built
fake collection (:class:`~tests.berdl.test_clearinghouse_mongo_adapter.FakeCollection`,
reused rather than re-fabricated) or against the manifest text itself. No
network, no ``pymongo``, no credentials.

What is covered:

  - Every preset PARSES and PLANS -- ``load_manifest`` + ``shard_plan`` --
    which is the whole off-pod check: planning a mongo preset touches no
    collection and iterates no record, so it is valid to run anywhere.
  - The locator keys each preset declares resolve: the ``join`` spec, the
    ``require_fields`` list, the ``decompress`` mapping.
  - Every RESULT source declares a ``parameter_set``, and it is the default
    run (:data:`~kbutillib.domains.identity.DEFAULT_PARAMETER_SET_HASH`).
  - Each preset SHARDS a small fake sample into EXACTLY the tables
    ``backfill list`` says it targets, with ``protein`` hashes equal to
    ``standardizers.entity_hash("protein", sequence)`` -- computed from the
    sequence, never from the Mongo ``_id`` (which the fixtures deliberately
    make a DIFFERENT digest, so a preset that trusted ``_id`` would fail).
  - PARITY with ``examples/clearinghouse_example_seed.toml``: every source the
    presets share with the hand-written lake-5 seed, matched by
    ``(collection, entity_type, kinds)``, agrees with it on its hash rule, its
    field mappings, its parameter set, its result type and version, and its
    result ``source`` label. The seed and ``mongo-protein-bakta`` are one
    mapping of one collection and must not drift.
"""

from __future__ import annotations

import hashlib
import zlib
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from kbutillib.domains import identity
from kbutillib.domains.identity import standardizers
from kbutillib.domains.kbase.berdl import clearinghouse_backfill as cb
from kbutillib.domains.kbase.berdl import clearinghouse_manifest as cm
from kbutillib.domains.kbase.berdl import clearinghouse_schema as schema
from kbutillib.domains.kbase.berdl import clearinghouse_shard as cs

from .test_clearinghouse_mongo_adapter import FakeCollection

#: The presets this task ships. Named explicitly rather than globbed, so a
#: preset that silently stops being installed as package data fails here.
MONGO_PRESETS = (
    "mongo-protein-seq",
    "mongo-protein-bakta",
    "mongo-protein-kofam",
    "mongo-protein-kofam2",
    "mongo-protein-rast2",
)

#: The lake-5 seed the bakta preset must not drift from.
SEED_PATH = (
    Path(cs.__file__).resolve().parent / "examples" / "clearinghouse_example_seed.toml"
)

#: Three distinct amino-acid sequences; the whole fake store is built from them.
SEQUENCES = ("MKAILVGADTPRQ", "GADTPRQWYFMSS", "MSSSHHHHNNQQL")


def _mongo_id(sequence: str) -> str:
    """A 64-hex ``_id`` that is deliberately NOT the clearinghouse hash.

    The store's ``_id`` is a sha256 of the protein, but whether it equals what
    ``identity.standardizers`` computes is UNVERIFIED -- so every preset
    recomputes the identity from the sequence. These fixtures make the two
    digests differ, which is what turns that into a testable claim instead of a
    coincidence.
    """
    return hashlib.sha256(f"mongo-id:{sequence}".encode("utf-8")).hexdigest()


def _preset_manifest(name: str) -> cm.Manifest:
    path, resolved = cb.resolve_manifest_path(name)
    assert resolved == name, f"{name!r} did not resolve as a SHIPPED preset"
    return cm.load_manifest(path)


# --------------------------------------------------------------------------
# The fake store
# --------------------------------------------------------------------------


def _fake_store() -> dict[str, FakeCollection]:
    """The five surveyed collections, in their surveyed shapes, over SEQUENCES.

    ``seq_protein`` holds the sequence as a real zlib blob (so the decompress
    path is exercised, not stubbed); ``seq_protein_bakta`` holds it in
    plaintext ``aa``; the three annotation collections hold NO sequence at all,
    which is exactly why they need the ``join``.
    """
    ids = {seq: _mongo_id(seq) for seq in SEQUENCES}
    return {
        "seq_protein": FakeCollection(
            [
                {"_id": ids[seq], "z_seq": zlib.compress(seq.encode("utf-8"))}
                for seq in SEQUENCES
            ]
        ),
        "seq_protein_bakta": FakeCollection(
            [
                {
                    "_id": ids[seq],
                    "aa": seq,
                    "length": len(seq),
                    "type": "cds",
                    "product": f"product of {seq[:4]}",
                    "gene": f"gene{index}",
                    "genes": [f"gene{index}"],
                    "db_xrefs": [f"UniRef90_X{index}"],
                    "psc": {"uniref90_id": f"UniRef90_X{index}", "identity": "0.9"},
                    "pscc": {"uniref50_id": f"UniRef50_X{index}"},
                }
                for index, seq in enumerate(SEQUENCES)
            ]
        ),
        "seq_protein_kofam": FakeCollection(
            [
                {"_id": ids[seq], "ko": f"K{index:05d}"}
                for index, seq in enumerate(SEQUENCES)
            ]
        ),
        "seq_protein_kofam2": FakeCollection(
            [
                {"_id": ids[seq], "kos": [f"K{index:05d}", f"K{index + 1:05d}"]}
                for index, seq in enumerate(SEQUENCES)
            ]
        ),
        "protein_to_rast2": FakeCollection(
            [
                {
                    "_id": ids[seq],
                    "function": f"role of {seq[:4]}",
                    # The survey's quality sub-fields are STRINGS, not numbers.
                    "quality": {"hit_count": "3", "failed": "0"},
                    "annotations": [["role", "rast", "2026-09-24"]],
                }
                for seq in SEQUENCES
            ]
        ),
    }


def _adapter_for(
    manifest: cm.Manifest, plan: cm.TablePlan, store: dict[str, FakeCollection]
) -> cs.MongoSourceAdapter:
    """Build the plan's adapter with the store's collections injected.

    The join collection is injected too: an injected primary collection builds
    no client, so there is nothing to resolve a second collection from (see
    ``MongoSourceAdapter._get_join_collection``).
    """
    source = next(s for s in manifest.sources if s.name == plan.source_name)
    join = cs.MongoSourceAdapter(source)._join_spec()
    return cs.MongoSourceAdapter(
        source,
        collection=store[source.locator["collection"]],
        join_collection=None if join is None else store[join.collection],
    )


def _shard_preset(name: str, out_root: Path) -> tuple[cm.Manifest, list[cm.TablePlan]]:
    """Shard one preset's whole manifest off a fake store, as one unit.

    Mirrors what :func:`clearinghouse_shard.shard_manifest` does -- registry
    batch first, then one merge per target table -- but builds the adapters
    here so the fake collections can be injected. ``shard_manifest`` itself
    constructs its own adapters, which would connect.
    """
    manifest = _preset_manifest(name)
    plans = cm.shard_plan(manifest)
    store = _fake_store()
    context = cs.ShardRunContext.build(target_bytes=4096)
    cs.write_parameter_set_registry_shard(
        plans, out_root, context=context, already_known=set()
    )
    for table in dict.fromkeys(plan.table for plan in plans):
        work = [
            (plan, _adapter_for(manifest, plan, store))
            for plan in plans
            if plan.table == table
        ]
        cs.shard_table(work, out_root, context=context)
    return manifest, plans


def _rows(out_root: Path, table: str) -> list[dict]:
    rows: list[dict] = []
    for path in sorted((out_root / table).glob("*.parquet")):
        rows.extend(pq.read_table(str(path)).to_pylist())
    return rows


# --------------------------------------------------------------------------
# Shipped, parsing, planning -- off-pod, without iterating
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", MONGO_PRESETS)
def test_preset_is_shipped_as_package_data(name):
    presets = {preset.name: preset for preset in cb.available_presets()}
    assert name in presets
    assert presets[name].path.is_file()


@pytest.mark.parametrize("name", MONGO_PRESETS)
def test_preset_parses_and_plans_off_pod_without_iterating(name):
    """Planning a mongo preset touches no collection and reads no document.

    The adapters are built (as ``get_adapter`` does during validation) and
    their locators resolved, but no ``find`` and no
    ``estimated_document_count`` is issued -- which is why this runs on a
    machine with no route to poplar at all.
    """
    manifest = _preset_manifest(name)
    plans = cm.shard_plan(manifest)
    assert plans, f"{name} planned no tables"
    for plan in plans:
        source = next(s for s in manifest.sources if s.name == plan.source_name)
        assert source.adapter == "mongo"
        adapter = cs.get_adapter(source)
        # Locators resolve, and nothing connected: no collection, no client.
        adapter._join_spec()
        adapter._require_fields()
        adapter._decompress_spec()
        assert adapter._collection is None
        assert adapter._client is None
        # No record was read, so `iter_records` was never entered.
        assert adapter.exhausted() is None


@pytest.mark.parametrize("name", MONGO_PRESETS)
def test_preset_describes_cleanly_and_names_real_tables(name):
    presets = {preset.name: preset for preset in cb.available_presets()}
    row = cb.describe_preset(presets[name])
    assert "error" not in row
    assert row["adapters"] == ["mongo"]
    assert row["needs_env"] == []
    assert row["pulls_from"]
    known = {config["name"] for config in schema.table_configs()}
    assert set(row["tables"]) <= known


@pytest.mark.parametrize("name", MONGO_PRESETS)
def test_every_result_source_declares_a_default_parameter_set(name):
    """`parameter_set` is REQUIRED on a result source after lake-5, and every
    one of these stores recorded no parameters -- so `{}`, which hashes to
    DEFAULT_PARAMETER_SET_HASH."""
    manifest = _preset_manifest(name)
    results = [plan for plan in cm.shard_plan(manifest) if plan.kind == "result"]
    for plan in results:
        assert plan.parameter_set == {}
        assert plan.parameter_set_hash == identity.DEFAULT_PARAMETER_SET_HASH
        assert plan.parameter_set_column is None
    if results:
        # The registry table is the sixteenth table and is reported first.
        presets = {preset.name: preset for preset in cb.available_presets()}
        assert cb.describe_preset(presets[name])["tables"][0] == (
            schema.PARAMETER_SET_TABLE
        )


def test_the_join_presets_declare_the_sequence_lookup():
    """The three sequence-less collections all join `seq_protein` on `_id`."""
    for name in ("mongo-protein-kofam", "mongo-protein-kofam2", "mongo-protein-rast2"):
        manifest = _preset_manifest(name)
        joined = []
        for source in manifest.sources:
            join = cs.MongoSourceAdapter(source)._join_spec()
            if join is None:
                continue
            joined.append(source.name)
            assert join.collection == "seq_protein"
            assert join.on == cs.MONGO_JOIN_DEFAULT_ON
            assert join.fields == {"sequence": "z_seq"}
            assert join.decompress == ("sequence",)
            # And the identity is hashed from what the join supplies.
            assert source.hash_spec == {"raw_column": "sequence"}
        assert joined, f"{name} declared no join"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("mongo-protein-bakta", {"mongo-protein-bakta-function": ("product",)}),
        ("mongo-protein-kofam", {"mongo-protein-kofam-result": ("ko",)}),
        ("mongo-protein-kofam2", {"mongo-protein-kofam2-result": ("kos",)}),
        (
            "mongo-protein-rast2",
            {
                "mongo-protein-rast2-result": ("function",),
                "mongo-protein-rast2-function": ("function",),
            },
        ),
    ],
)
def test_presets_require_the_fields_their_stubs_lack(name, expected):
    manifest = _preset_manifest(name)
    declared = {
        source.name: cs.MongoSourceAdapter(source)._require_fields()
        for source in manifest.sources
        if cs.MongoSourceAdapter(source)._require_fields()
    }
    assert declared == expected


# --------------------------------------------------------------------------
# Sharding a fake sample: exactly the declared tables, hashed from sequences
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", MONGO_PRESETS)
def test_preset_shards_a_fake_sample_into_exactly_its_declared_tables(name, tmp_path):
    out_root = tmp_path / "bronze"
    manifest, plans = _shard_preset(name, out_root)
    presets = {preset.name: preset for preset in cb.available_presets()}
    declared = set(cb.describe_preset(presets[name])["tables"])
    written = {path.name for path in out_root.iterdir() if path.is_dir()}
    assert written == declared


@pytest.mark.parametrize("name", MONGO_PRESETS)
def test_preset_protein_hashes_come_from_the_sequence_not_the_mongo_id(
    name, tmp_path
):
    """Every protein row's entity_hash is the REAL standardizer over the
    sequence -- which the fixtures make a different digest from `_id`."""
    out_root = tmp_path / "bronze"
    manifest, plans = _shard_preset(name, out_root)
    expected = {standardizers.entity_hash("protein", seq) for seq in SEQUENCES}
    # The fixtures' `_id` values are NOT these digests, so a preset that
    # trusted `_id` would fail here rather than pass by coincidence.
    assert expected.isdisjoint({_mongo_id(seq) for seq in SEQUENCES})

    protein_tables = [
        plan.table for plan in plans if plan.entity_type == "protein"
    ]
    assert protein_tables, f"{name} wrote no protein table"
    for table in set(protein_tables):
        rows = _rows(out_root, table)
        assert rows
        assert {row["entity_hash"] for row in rows} == expected
        assert all(row["entity_hash"] is not None for row in rows)


def test_mongo_protein_seq_content_is_the_decompressed_sequence(tmp_path):
    out_root = tmp_path / "bronze"
    _shard_preset("mongo-protein-seq", out_root)
    rows = {row["entity_hash"]: row for row in _rows(out_root, "protein_content")}
    for seq in SEQUENCES:
        row = rows[standardizers.entity_hash("protein", seq)]
        assert row["sequence"] == seq
        assert row["seq_length"] == len(seq)
    # No registry table: this preset declares no result source, so there is no
    # parameter set to register.
    assert not (out_root / schema.PARAMETER_SET_TABLE).exists()


def test_mongo_protein_bakta_seeds_its_own_proteins_and_its_functions(tmp_path):
    """A seed of this preset ALONE yields joinable protein entities."""
    out_root = tmp_path / "bronze"
    _shard_preset("mongo-protein-bakta", out_root)
    content = {row["entity_hash"]: row for row in _rows(out_root, "protein_content")}
    for seq in SEQUENCES:
        assert content[standardizers.entity_hash("protein", seq)]["sequence"] == seq
    results = _rows(out_root, "protein_result")
    assert {row["source"] for row in results} == {"bakta/mongo-seq_protein_bakta"}
    assert {row["result_type"] for row in results} == {"annotation"}
    assert {row["parameter_set_hash"] for row in results} == {
        identity.DEFAULT_PARAMETER_SET_HASH
    }
    # The payload carries Bakta's output, including the cluster sub-documents.
    assert all('"psc"' in row["payload"] for row in results)
    # The function entity is hashed from the SAME product text it defines.
    functions = {row["entity_hash"]: row for row in _rows(out_root, "function_content")}
    for row in functions.values():
        assert row["entity_hash"] == standardizers.entity_hash(
            "function", row["definition"]
        )


@pytest.mark.parametrize(
    ("name", "label", "payload_key"),
    [
        ("mongo-protein-kofam", "kofam/mongo-seq_protein_kofam", "ko"),
        ("mongo-protein-kofam2", "kofam/mongo-seq_protein_kofam2", "kos"),
        ("mongo-protein-rast2", "rast/mongo-protein_to_rast2", "function"),
    ],
)
def test_joined_presets_write_results_keyed_on_the_joined_sequence(
    name, label, payload_key, tmp_path
):
    """The sequence-less collections produce fully keyed result rows.

    The entity hash can only have come from the JOINED, decompressed
    `seq_protein` blob -- the primary collection carries no sequence at all.
    """
    out_root = tmp_path / "bronze"
    _shard_preset(name, out_root)
    rows = _rows(out_root, "protein_result")
    assert {row["entity_hash"] for row in rows} == {
        standardizers.entity_hash("protein", seq) for seq in SEQUENCES
    }
    assert {row["source"] for row in rows} == {label}
    assert {row["result_type"] for row in rows} == {"annotation"}
    assert {row["result_type_version"] for row in rows} == {"1.0"}
    assert all(f'"{payload_key}"' in row["payload"] for row in rows)
    # The registry batch explaining the parameter_set_hash was written.
    registry = _rows(out_root, schema.PARAMETER_SET_TABLE)
    assert [row["parameter_set_hash"] for row in registry] == [
        identity.DEFAULT_PARAMETER_SET_HASH
    ]
    assert registry[0]["canonical_json"] == "{}"


def test_rast2_function_source_needs_no_join_and_defines_the_role(tmp_path):
    out_root = tmp_path / "bronze"
    _shard_preset("mongo-protein-rast2", out_root)
    rows = _rows(out_root, "function_content")
    assert rows
    for row in rows:
        assert row["definition"].startswith("role of ")
        assert row["entity_hash"] == standardizers.entity_hash(
            "function", row["definition"]
        )


def test_a_join_miss_and_a_stub_are_counted_not_loaded(tmp_path):
    """The two drop reasons, through a real preset rather than a hand source."""
    manifest = _preset_manifest("mongo-protein-rast2")
    (plan,) = [p for p in cm.shard_plan(manifest) if p.table == "protein_result"]
    source = next(s for s in manifest.sources if s.name == plan.source_name)
    kept, missing = SEQUENCES[0], SEQUENCES[1]
    primary = FakeCollection(
        [
            {"_id": _mongo_id(kept), "function": "DNA gyrase"},
            {"_id": _mongo_id(missing), "function": "RecA"},
            {"_id": _mongo_id(SEQUENCES[2])},  # an _id-only stub
        ]
    )
    # The second protein is absent from seq_protein: a join miss.
    join = FakeCollection(
        [{"_id": _mongo_id(kept), "z_seq": zlib.compress(kept.encode("utf-8"))}]
    )
    adapter = cs.MongoSourceAdapter(
        source, collection=primary, join_collection=join
    )
    reports = cs.shard_source(plan, adapter, tmp_path / "bronze", target_bytes=4096)
    rows = [row for report in reports for row in pq.read_table(str(report.path)).to_pylist()]
    assert [row["entity_hash"] for row in rows] == [
        standardizers.entity_hash("protein", kept)
    ]
    assert adapter.skipped() == {
        cs.MONGO_SKIP_NO_JOIN_MATCH: 1,
        f"{cs.MONGO_SKIP_MISSING_PREFIX}function": 1,
    }


# --------------------------------------------------------------------------
# Parity with the lake-5 example seed
# --------------------------------------------------------------------------


def _normalise(block, where):
    """A mapping block as ``{column: (derivation_name, args)}``.

    Compared SEMANTICALLY, not as text: a bare ``"aa"`` and an explicit
    ``"@column(aa)"`` parse to the same derivation and are the same mapping, so
    the parity check cannot be defeated (or falsely tripped) by a spelling
    choice.
    """
    return {
        column: (derivation.name, derivation.args)
        for column, value in block.items()
        if column not in ("parameter_set", "parameter_set_column")
        for derivation in [cm.parse_derivation(value, where=f"{where}.{column}")]
    }


def _by_shared_key(manifest):
    """Index a manifest's sources by ``(collection, entity_type, kinds)``."""
    return {
        (
            source.locator.get("collection"),
            source.entity_type,
            tuple(source.kinds),
        ): source
        for source in manifest.sources
    }


def test_every_preset_source_shared_with_the_lake5_seed_matches_it():
    """The seed and the presets are ONE mapping of one collection.

    Matched by ``(collection, entity_type, kinds)``, a shared source must agree
    on its hash rule, its field mappings, its parameter set, its result type
    and version, and its result ``source`` label. Commented-out sources in
    either file are not parsed and so are ignored, as intended.
    """
    seed = _by_shared_key(cm.load_manifest(SEED_PATH))
    assert seed, "the lake-5 example seed parsed no sources"

    shared = 0
    for name in MONGO_PRESETS:
        for key, source in _by_shared_key(_preset_manifest(name)).items():
            if key not in seed:
                continue
            shared += 1
            other = seed[key]
            where = f"{name}:{source.name} vs seed:{other.name} {key}"
            assert source.hash_spec == other.hash_spec, f"hash rule: {where}"
            assert _normalise(source.content, "content") == _normalise(
                other.content, "content"
            ), f"content mapping: {where}"
            assert _normalise(source.result, "result") == _normalise(
                other.result, "result"
            ), f"result mapping: {where}"
            assert source.result.get("parameter_set") == other.result.get(
                "parameter_set"
            ), f"parameter_set: {where}"
            # Called out individually so a drift in one names itself.
            for key_name in ("source", "result_type", "result_type_version"):
                assert source.result.get(key_name) == other.result.get(
                    key_name
                ), f"{key_name}: {where}"
            assert (
                source.observed_at_column == other.observed_at_column
            ), f"observed_at_column: {where}"

    # The check is worthless if it matched nothing. All three bakta sources are
    # shared with the seed.
    assert shared == 3, f"expected 3 shared sources, matched {shared}"


def test_the_seed_and_the_bakta_preset_cover_the_same_collection():
    """Guards the parity test's premise: the two files are about one collection.

    If the seed were ever repointed at another collection, the parity test
    above would match nothing and silently prove nothing -- so the premise is
    asserted separately.
    """
    seed_collections = {
        source.locator.get("collection")
        for source in cm.load_manifest(SEED_PATH).sources
    }
    preset_collections = {
        source.locator.get("collection")
        for source in _preset_manifest("mongo-protein-bakta").sources
    }
    assert seed_collections == preset_collections == {"seq_protein_bakta"}
