# Work record: clh2-adapter-mongo

**Task:** Build the `mongo` source adapter for the clearinghouse bronze shard
stage in KBUtilLib.
**Branch:** `maestro/developer/task-clh2-adapter-mongo`
**Base commit:** 393d715 "feat(berdl): build the lakehouse SourceAdapter for
clearinghouse sharding"
**Date:** 2026-09-30

## What was built

A `MongoSourceAdapter` implementing the `SourceAdapter` interface in
`src/kbutillib/domains/kbase/berdl/clearinghouse_shard.py`, registered in
`_ADAPTER_CLASSES` under the name `"mongo"`. It reads ONE MongoDB collection
with a batched, sorted cursor and yields each document as a plain dict row for
the sharder to standardize/hash/sort/write. It never writes to Iceberg and does
no hashing/sorting itself — the adapter contract, unchanged.

- `iter_records()` — streams `collection.find(filter, batch_size=...).sort(sort_key, 1)`;
  never loads the collection. Decompresses configured `z_seq`-style blobs into a
  plain string field the manifest can map.
- `estimate_bytes()` — `estimated_document_count() * MONGO_BYTES_PER_DOC`
  (cheap metadata count, no scan; min 1).
- Lazy connection: `pymongo` is imported and `MongoClient` opened only on first
  read (`_get_collection`), exactly like the lakehouse adapter defers its pod
  import — so constructing the adapter during manifest validation touches no
  driver and opens no connection. `pymongo` is NOT imported at module scope.

### Locator keys the adapter reads (all from the manifest; no machine hardcoded)
- `uri` OR (`host` default `localhost`, `port` default `27017`) — connection.
  No credential key (auth is disabled on the surveyed store).
- `database` (required), `collection` (required).
- `batch_size` (optional, default `MONGO_BATCH_SIZE=5000`).
- `decompress` (optional) — `{output_field: input_field}`; zlib-decompresses the
  input blob to UTF-8 and exposes it under output_field, dropping the raw field.
- `sort_key` (optional, default `_id`) — the stable sort/paging key.
- `min_key` / `max_key` (optional) — half-open `[min_key, max_key)` slice for
  disjoint parallel reads.
- `start_after` (optional) — strict `$gt` resume point for a killed run.

Resumability and parallelism (both required by the prompt at 420M-doc scale) are
delivered by the sort-key range/resume filter: a killed run resumes with
`start_after`; the sharder runs disjoint `[min_key, max_key)` slices in parallel
with no overlap (half-open ranges meet at a boundary without both reading it).

## OP-B fact sheet — what it gave me and what I did with it

The OP-B fact sheet WAS attached (surveyed live by Miles on `bioseed_mongo` on
poplar, 2026-09-30). I did not stop. I authored against it, not an assumed schema.

- **Database:** literally `"database"`. **Store:** MongoDB 8.0.8 in Docker
  container `bioseed_mongo` on poplar, `mongodb://localhost:27017`, auth
  DISABLED. Reachable only from poplar/h100 localhost — **NOT reachable from my
  worktree**, and I did not attempt to reach it. Tests use a hand-built fake.
- **Sequences OR annotations:** BOTH, in SEPARATE collections joined by the
  sha256 `_id`:
  - `seq_protein` (420.8M docs): `{ _id: <64-hex sha256>, z_seq: <zlib blob> }`,
    sequence only. Blob starts `0x789c`; `zlib.decompress` → uppercase AA string.
  - `protein_to_rast2` (399.9M docs): annotations only; fields OPTIONAL (~28%
    `_id`-only stubs); `quality` sub-fields stored as STRINGS.
- **Count correction:** sized batching/estimates against 420M, not the prompt's
  "300M".

### How I handled the JOIN (the design question OP-B opened)
An "annotated protein set" is a JOIN across collections, not one collection. I
did NOT put the join inside the adapter — that would fight the sharder's
range-cutting and break the "one adapter = one collection" seam. Instead the
join is expressed as SEVERAL manifest sources, one per collection, each hashing
on the same protein identity:
- `protein_content` / `protein_entity` ← `seq_protein`, with
  `[source.decompress] sequence = "z_seq"` and `raw_column = "sequence"`.
- `protein_result` ← `protein_to_rast2`.

This keeps the adapter a pure per-collection reader and leaves the sharder
untouched.

### `_id` is NOT trusted as `entity_hash`
Mongo's `_id` and the clearinghouse `entity_hash` are both 64-hex sha256, but
whether they are the same VALUE is unverified. The fact sheet flags this
explicitly. So the `seq_protein` manifest hashes on the DECOMPRESSED SEQUENCE
(`raw_column = "sequence"`), letting `kbutillib.domains.identity.standardizers`
recompute the identity — never `precomputed` on `_id`. Whether the two agree is
a spot-check for the manifest's derivation, deliberately not hardcoded. The
end-to-end test asserts the bronze `entity_hash` equals
`standardizers.entity_hash("protein", <decompressed seq>)` and that `_id` never
leaks into a bronze row.

## Prompt/tree reconciliations

1. **"Register mongo, update AdapterNotImplementedError + the _ADAPTER_CLASSES
   comment to match reality."** Done. I also updated the `SourceAdapter` class
   docstring, `ADAPTER_NAMES` comment, module docstring line, and
   `get_adapter`'s docstring/error text — all of which said mongo was
   "planned but not built".
2. **Two pre-existing tests asserted mongo is planned-but-unbuilt** and so
   asserted `get_adapter(mongo)` raises `AdapterNotImplementedError`:
   - `tests/berdl/test_clearinghouse_lakehouse_adapter.py::test_only_mongo_remains_planned_but_unbuilt`
   - `tests/berdl/test_clearinghouse_manifest_and_shard.py::test_planned_adapters_are_declared_but_unbuilt`
   These encoded the pre-task reality. Building mongo makes those assertions
   false by design (the prompt says that branch "must stop firing" for mongo).
   I updated both to assert the new truth — mongo and lakehouse are both built
   and `get_adapter` returns a real adapter — renaming them
   `test_mongo_and_lakehouse_are_both_built` and `test_all_declared_adapters_are_built`.
   I did NOT weaken or skip them; I inverted the now-obsolete expectation to
   match shipped behavior.
3. **"RANGE-CUTTING A STREAM" section is background, not a spec** — the sharder
   already implements range-cutting on main. I did NOT add a second range-cutter
   in the adapter.
4. **pymongo / mongomock absence.** Neither is installed in any available venv.
   The prompt requires the tests use a fake/in-memory double — a hard
   requirement, not a fallback — so the tests use a hand-built `FakeCollection`
   / `FakeCursor` (mirroring the lakehouse test's `FakeCapability`) and import
   no mongo driver. The adapter's lazy `pymongo` import means the module and the
   tests load without pymongo present.

## Files changed

- `src/kbutillib/domains/kbase/berdl/clearinghouse_shard.py` — added
  `MongoSourceAdapter`, `_zlib_decode`, `MONGO_BATCH_SIZE`, `MONGO_BYTES_PER_DOC`;
  registered `"mongo"` in `_ADAPTER_CLASSES`; corrected the now-stale
  planned-but-unbuilt wording in the module docstring, `SourceAdapter` docstring,
  `ADAPTER_NAMES` comment, `AdapterNotImplementedError` docstring, and
  `get_adapter` docstring/error text.
- `tests/berdl/test_clearinghouse_mongo_adapter.py` — NEW, 20 tests, hand-built
  fake collection; no live DB, no driver.
- `tests/berdl/test_clearinghouse_lakehouse_adapter.py` — updated one obsolete
  test (see reconciliation #2).
- `tests/berdl/test_clearinghouse_manifest_and_shard.py` — updated one obsolete
  test (see reconciliation #2).

## Verification

Run in the `kbdl` venv (pytest 9.1.1, fully provisioned; the worktree's
`.task-venv` lacks pytest), forcing `PYTHONPATH` to the worktree `src`:

    PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest tests/berdl/ -q

- Base commit 393d715 (before my change): `420 passed, 5 skipped`.
- My branch: `440 passed, 5 skipped`, **exit 0**. (+20 new mongo tests; the 2
  updated tests still pass under their new assertions.)

New mongo tests alone: `20 passed`.

No test that passed on 393d715 fails on the branch. The suite was not run for
green; the berdl area is fully green in this venv, and the baseline for it is
the 420-pass figure above (the repo-wide `build-baselines/kbutillib.json`
records 99 known failures captured in a *minimal* venv missing optional deps —
none in the berdl clearinghouse area, which I ran in full).
