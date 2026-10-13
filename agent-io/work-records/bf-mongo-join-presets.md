# Work record: `bf-mongo-join-presets`

- **Role:** developer
- **Repo:** KBUtilLib
- **Base commit:** `da20530`
- **Branch:** `maestro/developer/shared-context-this-prompt-is-se-bf-mongo-join-presets`
- **Date:** 2026-10-09

## Precondition check (lake-5 convergence) — ALL FOUR PASS

Checked against the worktree, not the prompt:

| Check | Result |
|---|---|
| (a) every `<type>_result` config's columns include `parameter_set_hash` | PASS — all five result configs carry it |
| (b) `clearinghouse_schema` exports `PARAMETER_SET_TABLE` (= `"parameter_set"`) and `table_configs()` returns a config for it | PASS — **16** configs, the sixteenth named `parameter_set`, unpartitioned, columns `parameter_set_hash, canonical_json, observed_at, ingest_batch_id` |
| (c) `kbutillib.domains.identity` exports `parameter_set_hash` and `DEFAULT_PARAMETER_SET_HASH` | PASS — `DEFAULT_PARAMETER_SET_HASH = 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a` |
| (d) `examples/clearinghouse_example_seed.toml` exists | PASS — 6,169 bytes |

Tables were iterated with `table_configs()` throughout, never `ENTITY_TYPES × KINDS`.

## What was built

### 1. The `join` locator (`clearinghouse_shard.py`)

- `MongoJoinSpec` dataclass + `MONGO_JOIN_DEFAULT_ON`, `_MONGO_JOIN_KEYS`,
  `MONGO_SKIP_NO_JOIN_MATCH`, `MONGO_SKIP_MISSING_PREFIX`
  (`clearinghouse_shard.py:1107`–`1144`).
- `MongoSourceAdapter._join_spec()` (`clearinghouse_shard.py:1376`) parses
  `{collection, on, fields, decompress}` with every key rejected **by name** —
  a `field`-for-`fields` typo would otherwise join nothing onto every record
  and look like a lost collection.
- `MongoSourceAdapter._join_page()` (`clearinghouse_shard.py:1705`) issues
  **exactly one** `find({on: {"$in": [...page ids...]}}, projection)` per page
  against the join collection, deduplicating the id list, projecting only
  `on` + the mapped input fields, merging each match under its *output* name,
  and counting a record with no match under `no-join-match`.
- `_get_join_collection()` (`clearinghouse_shard.py:1578`) resolves the join
  collection from the same database as the primary one, injectable via a new
  `join_collection=` constructor keyword — the same seam `collection=` already
  gave tests.
- `_prepare_row()` gained a `join` argument and zlib-decodes `join.decompress`
  **in place** under the output name (the merge already renamed the field, so
  there is no input field left to pop).
- `iter_records()` now consumes the cursor a page at a time via the new
  module-level `_mongo_pages()` generator (page = `batch_size`, the
  granularity the cursor already fetches at — no second tunable that could
  disagree with it). With no `join` declared the paging is pure chunking:
  same documents, same order, same memory ceiling. One `find` on the primary
  collection either way (asserted).

### 2. The `require_fields` locator

- `_require_fields()` (`clearinghouse_shard.py:1454`), `_satisfies()` and
  `_skip()` on the adapter; `_mongo_is_absent()` at module level.
- Absent = `None` **or** an empty str/bytes/list/tuple/mapping/set. A number
  or boolean — including `0` and `False` — is PRESENT: a requirement is about
  whether the store recorded a value, and dropping a legitimate `0` would be
  a silent loss no count could explain.
- Counted under `missing:<field>`, **first failing requirement only**, so the
  counts sum to records dropped rather than to failed (record, field) pairs.
- **The check runs twice, deliberately.** A requirement naming a field the
  join or a `decompress` *produces* can only be judged after the merge; one
  naming a plain primary field is judged before it, which is what keeps
  `protein_to_rast2`'s ~28% stubs from driving ~112M needless sequence
  lookups. Both report the same reason key, so where a record was dropped is
  invisible in `skipped()`.
- The `--limit` budget is now claimed **after** the skip checks (matching the
  arc adapter), so a skipped record does not spend a row of the allowance. No
  behaviour change for a source that skips nothing.

### 3. Five presets, shipped as package data

All under `src/kbutillib/domains/kbase/berdl/backfills/` (already covered by
the `backfills/*.toml` glob in `pyproject.toml:232` — no packaging change
needed). Every one: `adapter = "mongo"`,
`uri = "${KBU_MONGO_URI:-mongodb://poplar.cels.anl.gov:27017}"`,
`database = "database"`, `sort_key = "_id"`, `[backfill] needs_env = []`.

| Preset | Sources | Tables |
|---|---|---|
| `mongo-protein-seq` | `seq_protein` → protein entity+content, `decompress = { sequence = "z_seq" }`, hash `raw_column = "sequence"` | `protein_entity`, `protein_content` |
| `mongo-protein-bakta` | `seq_protein_bakta` → protein entity+content (from `aa`), protein result, function entity+content (`require_fields = ["product"]`) | `parameter_set`, `protein_{entity,content,result}`, `function_{entity,content}` |
| `mongo-protein-kofam` | `seq_protein_kofam` → protein result; join `seq_protein`; `require_fields = ["ko"]` | `parameter_set`, `protein_result` |
| `mongo-protein-kofam2` | `seq_protein_kofam2` → protein result; join `seq_protein`; `require_fields = ["kos"]` | `parameter_set`, `protein_result` |
| `mongo-protein-rast2` | `protein_to_rast2` → protein result (join `seq_protein`) + function entity+content (no join — the role text is local); both `require_fields = ["function"]` | `parameter_set`, `protein_result`, `function_{entity,content}` |

Every result source declares `parameter_set = {}`, `result_type =
"@const(annotation)"`, `result_type_version = "@const(1.0)"`, and a
provenance `source` label (`bakta/mongo-seq_protein_bakta`,
`kofam/mongo-seq_protein_kofam`, `kofam/mongo-seq_protein_kofam2`,
`rast/mongo-protein_to_rast2`).

`kbu clearinghouse backfill list` prints all eight presets with no error row.

## Divergences reconciled (prompt vs tree)

1. **`@column(...)` vs the bare column form.** The prompt specified
   `sequence=@column(aa)`, `definition=@column(product)` etc. The lake-5 seed
   (`examples/clearinghouse_example_seed.toml`) and `file-example.toml` both
   write a plain passthrough as the **bare** form (`sequence = "aa"`). Per the
   task's "ADOPT LAKE-5'S CHOICE" rule I used the bare form in every preset.
   The two parse to the identical `Derivation(name="column", args=(col,))`, so
   this is spelling only — and the parity test compares `(name, args)`, not
   raw text, so neither spelling can defeat or falsely trip it.
2. **`pulls_from = ["poplar Mongo"]`.** Honoured literally as the first
   element, with the `mongodb://host:port/database.collection` form added
   after it — the shape every existing preset uses, and the thing an operator
   reading `backfill list` actually needs. The join collection is listed too
   (marked `(join)`): the join reads it as surely as the cursor reads the
   primary.
3. **`SLOT_KEY_COLUMNS`.** The standing rule said to add a public alias "if
   only a private name exists". It already exists —
   `clearinghouse_derivation.py:151`, re-exported at
   `clearinghouse_shard.py:4711`. Nothing added, nothing restated; this task
   never keys a result row itself.
4. **`skipped()` "added by bf-shard-streaming".** Already on the base class
   (`clearinghouse_shard.py:293`). `MongoSourceAdapter` gained only its own
   `_skip()` helper, mirroring the arc and kbdl_hash adapters.
5. **Source names in `mongo-protein-bakta`.** Kept identical to the seed's
   (`mongo-protein-bakta-protein` / `-result` / `-function`) so the two files
   are visibly one mapping.
6. **`require_fields` on the bakta function source.** The seed has none. This
   is not a parity violation: `require_fields` is an adapter **locator** key,
   not part of the hash rule / mappings / parameter set / result type / source
   label the parity test compares. Asserted in the preset tests.

## Parity with the lake-5 seed

`tests/berdl/test_clearinghouse_mongo_presets.py::test_every_preset_source_shared_with_the_lake5_seed_matches_it`
loads both TOMLs, indexes each by `(collection, entity_type, kinds)`, and for
every shared key asserts equality of: `hash_spec`, the normalised
`[source.content]` mapping, the normalised `[source.result]` mapping,
`parameter_set`, and (called out individually so a drift names itself)
`source`, `result_type`, `result_type_version`, plus `observed_at_column`.
**Three sources are shared** (all three bakta sources) and the test asserts
that count, so it cannot silently prove nothing. A companion test asserts the
premise — both files cover exactly `seq_protein_bakta`.

**No lake-5 choice had to be overridden**: the seed and the required mapping
agreed on every compared field. The only difference was the bare-vs-`@column`
spelling in item 1 above, where lake-5's form was adopted.

## Verification

Commands and exit codes, all in `.task-venv` (python 3.12.3, pytest 9.1.1):

```
.task-venv/bin/python -m pytest tests/berdl/test_clearinghouse_mongo_adapter.py -q   ->  49 passed,  exit 0
.task-venv/bin/python -m pytest tests/berdl/test_clearinghouse_mongo_presets.py -q   ->  44 passed,  exit 0
.task-venv/bin/python -m pytest tests/berdl -q                                        -> 1043 passed, 5 skipped, exit 0
```

**Regression baseline — captured fresh, not from cache.** The same pytest
invocation was run on this worktree at `da20530` *before any edit* and again
on the branch, in the same venv:

```
/tmp/pytest-cmd.sh   # pytest tests -q, with 11 --ignore paths (see below)
  base   (da20530):  358 failed, 3654 passed, 424 skipped, 9 errors   exit 1
  branch          :  358 failed, 3727 passed, 424 skipped, 9 errors   exit 1
comm -13 base-failures branch-failures  ->  EMPTY  (no new failure)
comm -23 base-failures branch-failures  ->  EMPTY  (none disappeared)
```

Compared **by nodeid name**, not count: the two failure sets are identical.
`+73` passing tests = the 29 added to `test_clearinghouse_mongo_adapter.py`
plus the 44 in the new `test_clearinghouse_mongo_presets.py`. All 358
pre-existing failures are in `tests/cli/` and `tests/` top level and trace to
optional scientific deps absent from this venv; **`tests/berdl/` is fully
green at base and on the branch**.

Eleven test modules cannot be *collected* in this venv at all (missing
`pandas`, `httpx`, `torch`, rdkit, etc.) and were excluded with `--ignore`:
`tests/biochem/test_escher_utils.py`,
`tests/biochem/test_ms_biochem_deltag.py`,
`tests/biochem/test_ms_reaction_similarity.py`,
`tests/external/test_prott5_utils.py`,
`tests/kbase/test_kb_annotation_utils_parquet.py`,
`tests/modeling/test_comprehensive_gapfill_wrapper.py`,
`tests/modeling/test_ms_community_utils{,_live}.py`,
`tests/modeling/test_ms_reconstruction_utils.py`,
`tests/modeling/test_ms_remote_solver_utils.py`, `tests/notebook`.

`tests/berdl/test_clearinghouse_live.py`'s static fence forbidding
`INSERT`/`MERGE`/`INSERT...SELECT` against a clearinghouse table still passes.
Nothing in this change writes SQL: the one write path remains
`BerdlCapability.load()` → `data_lakehouse_ingest`.

## What I could not verify

- **No live Mongo.** Every assertion is against hand-built fakes. The
  surveyed field shapes (`z_seq`, `aa`, `ko`, `kos`, `function`/`quality`/
  `annotations`, the `psc`/`pscc` sub-documents) are taken from Miles's
  2026-09-24 read-only survey as relayed in the task prompt and were not
  re-checked against the store. If a field is named differently in reality,
  the fix is a one-line mapping change in the preset, not a code change.
- **The `$in` page size against the real store.** `batch_size`
  (`MONGO_BATCH_SIZE = 5000`) is reused as the page size. 5,000-element `$in`
  lists over the `_id` index are expected to be fine but were not measured;
  the key is per-source tunable if a scratch run says otherwise.
- **`build-baselines/kbutillib.json` was left untouched.** It records
  `base_commit 0f84803` (2026-09-13), 99 `known_failing` nodeids, and a
  different `--ignore` set, captured in a different minimal venv whose own
  note says "on a fully-provisioned environment this set will differ". It
  disagrees with this venv by both commit and environment, and the task
  prompt directed a fresh two-sided comparison rather than trusting the cache
  — which is what was done. Overwriting it would claim authority over an
  environment I cannot speak for.
- **One pre-existing lint finding left alone**: `F401` unused
  `clearinghouse_schema as schema` import in
  `tests/berdl/test_clearinghouse_mongo_adapter.py`, present at `da20530`
  (verified with `git show`). Out of scope. The repo as a whole has 397 ruff
  findings and is not `ruff format`-clean, so neither was treated as a gate;
  my new code adds no finding of its own.
