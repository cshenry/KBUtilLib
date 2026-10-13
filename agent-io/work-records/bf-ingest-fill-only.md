# bf-ingest-fill-only — work record

**Task:** make the clearinghouse shard ingest read the shards on the pod's own
disk and hand them to `load()` as DataFrames, and make every load idempotent by
filtering each batch against what the lake already holds.

**Repo:** KBUtilLib · **Base commit:** `1870e0a` · **Branch:**
`maestro/developer/shared-context-this-prompt-is-se-bf-ingest-fill-only`

---

## Precondition check (clearinghouse-lake-5 convergence) — PASSED

Run against the worktree, not assumed:

| Check | Result |
|---|---|
| (a) every `<type>_result` config's columns include `parameter_set_hash` | PASS — all five result configs' `schema_sql` carries it |
| (b) `clearinghouse_schema` exports `PARAMETER_SET_TABLE` and `table_configs()` returns a config for it | PASS — `PARAMETER_SET_TABLE == "parameter_set"`, **16** configs, the sixteenth is `parameter_set` |
| (c) `kbutillib.domains.identity` exports `parameter_set_hash` and `DEFAULT_PARAMETER_SET_HASH` | PASS |
| (d) `examples/clearinghouse_example_seed.toml` exists | PASS |

`parameter_set_orphans()` (lake-5's namespace-wide registry orphan check) was
found at `clearinghouse_capability.py:1080` and is **reused**, not
reimplemented.

## What I built

### New module: `src/kbutillib/domains/kbase/berdl/clearinghouse_ingest.py`

The ingest mechanics that are not transport calls, so they can be proven by
test rather than argued from a run log:

- `key_columns_for_table()` / `range_column_for_table()` — the three key modes.
  The result mode **imports** `SLOT_KEY_COLUMNS` from `clearinghouse_derivation`
  (public alias already existed at `clearinghouse_derivation.py:151`); it is
  never restated.
- `fill_only_survivors(batch, existing_keys, key_columns)` — **the pure
  statement of fill-only** and the specification the Spark anti-join mirrors.
  Keeps row order and schema, keeps within-batch duplicates, and treats a NULL
  anywhere in a key tuple as *unmatched* (SQL semantics — a Python set would
  match `None == None` and the two implementations would disagree about real
  data).
- `resolve_batch_path()`, `file_sha256()`, `read_batch_table()`,
  `verify_batch_file()` — path containment, sha256, parquet readability, row
  count and per-row `ingest_batch_id`, all before the file's contents are used.
- `read_run_inventory()` — requires `<shard_dir>/run.json` and refuses a
  `run_manifest_version` this code does not implement.
- `derive_execution_inventory()` — a **load execution's** own inventory: fresh
  batch ULIDs, source run/batch/sha256 as provenance, units in publication
  order with each unit's registry batch first regardless of `run.json` order.
  `adopt_source_batch_ids=True` for a shard-and-load that is one operation.
- `ExecutionLedger` — append-only, **fsynced** JSON-lines ledger at
  `$KBU_CLEARINGHOUSE_STATE_DIR/<execution_id>/ingest_ledger.jsonl` (default
  `~/.kbu/clearinghouse/runs/<id>/`); `bind_execution()` / `write_execution()`
  persist the immutable binding and refuse a resume against another namespace
  or another shard run.
- `NamespaceLock` / `namespace_lock()` — an OS `flock` on a file in the shared
  `${KBU_CLEARINGHOUSE_LOCK_DIR:-/global_share/KBaseUtilities/clearinghouse-locks}`,
  holder/host/pid/start recorded in the file, dead holder reported and taken
  over, and **no private fallback** (refuses if the directory is unusable).
- `validate_batch_id()` / `validate_hex64()` — ULID and `^[0-9a-f]{64}$`
  validation immediately before a value is inlined into Spark SQL.

### Rewritten: `ClearinghouseCapability.ingest_shards()` (`clearinghouse_capability.py:1869`)

The defect is gone: no `bronze_path`, no `paths={'bronze_base': ...}`. Flow per
call — off-pod refusal → read `run.json` → dev-1206 write-target assertion →
**lake-5 schema preflight** → resolve/bind the execution → (dry-run returns
here, having written nothing) → acquire the namespace lock → persist the
binding → **reconcile every `started` batch** → ingest in inventory order.

Per batch: integrity checks → retag `ingest_batch_id` to the execution's id →
fill-only read of the target's keys inside `[hash_min, hash_max]` →
materialise survivors once (`cache()`+`count()`) → `expected_rows == 0`
commits **with no `load()` call** → otherwise a durable `started` line
(namespace, sha256, expected_rows) → `load(dataframes={table: survivors})` →
`committed` line with `rows_in` / `rows_dropped_present` / `rows_ingested` /
`snapshot_id`. An integrity failure ledgers `failed` and the run carries on.

New helpers: `_dry_run_reports`, `_ingest_inventory`,
`_assert_registry_committed_through` (registry across units),
`_ingest_one_batch`, `_existing_keys`, `_fill_only_survivors`,
`_spark_session`, `_assert_lake5_schema`, `_live_columns`,
`_reconcile_started_batches`, `_count_batch_rows`.

New errors: `ClearinghouseReconcileMismatchError`,
`ClearinghouseSchemaPreflightError`, `ClearinghouseRegistryOrderError`.

**The any-snapshot heuristic is deleted.** `_reconcile_started_batch()` (which
treated *any* snapshot on the table as proof a batch landed — always true on a
populated corpus, so it proved nothing) is replaced by
`COUNT(*) WHERE ingest_batch_id = '<validated ULID>'` with the
equal → committed / zero → retry / anything-else → stop-and-report rule.

### Rewritten: `verify_run()`

Per-batch proof instead of a table-wide count: every batch in the execution's
inventory needs a terminal `committed` line **and** an exact live
`ingest_batch_id` count equal to its `rows_ingested`; missing, failed,
unresolved and *excess* all block completion. Adds the **unit registry check**
(every `parameter_set_hash` a unit's result batches carry must be in the
registry) and keeps the recorded-snapshot confirmation and the namespace-wide
`parameter_set_orphans()` fold. Returns `ok`, `batches`, `units`, and the
`tables` roll-up the published CLI contract renders.

### `BerdlCapability.spark_session()` (`capability.py:326`)

One small addition: DataFrame-mode `load()` needs a DataFrame, and only the
session can build one from rows the driver read off local disk. In-pod only,
refusing off-pod with `BerdlLoadRefusedError`. When a collaborator exposes no
session the capability falls back to the pure function and passes the survivor
rows as a list of dicts — the same DataFrame-mode payload shape
`_write_parameter_set_registry()` already uses.

### CLI `kbu clearinghouse load` (`interfaces/cli/clearinghouse.py`)

Keeps its local-directory argument and the early off-pod refusal. `--run-id`
now names the **execution** (omitted → new execution; given → resume), help
and docstrings updated. The envelope gains `execution_id`, `source_run_id`,
`state_dir` and `lock`; a lock taken over from a dead holder becomes a
warning. **The namespace-wide orphan check runs at the end of every `load`**:
its report goes in `data.parameter_set_integrity`, `_render` already prints
such findings in full as `failure:` lines, and `_exit_on_integrity_failure`
(called after `_emit`) makes the exit non-zero without touching the per-batch
results.

### Docs

`agent-io/docs/kbu-clearinghouse-cli.md` — the `load`/`verify` rows, the
resumability section (ledger location, execution vs shard run, reconcile rule,
preflight, lock), the fill-only section, and the walkthrough/recovery commands.

## Dead code removed

`_read_ledger_states`, `_read_ledger_records`, `_append_ledger_line`,
`_discover_shards`, `_shard_format`, `_now_iso`, `_LEDGER_STARTED/_INGESTED/_FAILED`
(all were bronze-mode / beside-the-shards machinery). `LEDGER_FILENAME` stays
as a re-export of the canonical constant in `clearinghouse_ingest` so there is
one definition of the name. `order_shards_for_load` / `_shard_load_sort_key`
are kept (exported from `berdl/__init__`) with the docstring corrected to stop
referring to the deleted discovery helper.

## Tests

**New: `tests/berdl/test_clearinghouse_ingest.py` — 55 tests.** `_FakeLake` is
an in-memory stand-in that *stores* what `load()` is handed and answers the
four read shapes (DESCRIBE, the range key read, the per-batch-id count, the
registry `IN` lookup); a call recorder could not show idempotence, so the
"second ingest writes nothing" result is earned rather than asserted.
`_FakeSpark`/`_FakeDataFrame` drive the real glue with a `left_anti` join that
*is* the pure function, so the engine path and the specification cannot drift.

Covers: run.json required; off-pod refusal before any read; every batch via
`dataframes=` and never a bronze path; a second ingest of the same shard dir
appending zero rows with no `load()` call; the pure function (all-present,
none-present, partial, result slot keys, registry hashes, null keys,
within-batch duplicates, missing-key refusal); integrity (manifest version,
path escape, sha256, row count, per-row batch id); the ledger under the state
dir and *not* beside the shards; dry-run writing nothing; schema preflight
refusing a result table without `parameter_set_hash` and a target without the
registry, with zero `load()` calls; registry-first with the registry listed
**last**; zero registry rows on re-run; reconcile (match → committed, zero →
retry the same batch, other → stop), reconcile-before-anything-else, and the
refusal without `--reconcile`; crash-after-append → resume keeps, reconciles
and verifies; a new execution over landed files verifying with zero rows;
scratch→production promotion with fresh ids and provenance; a concurrent write
between count and append not changing `expected_rows`; lock contention from two
state dirs, dead-holder takeover, unwritable lock dir; verify by
`rows_ingested`, excess detection, blocking on a failed batch, the unit
registry check, and the orphan fold.

**Rewritten/relocated:** the `ingest_shards`/`verify_run` tests left
`tests/berdl/test_clearinghouse_capability.py` (they encoded the replaced
behaviour — bronze mode, a ledger beside the shards, the any-snapshot
reconcile, directory scanning — and `_FakeWriteCapability` cannot answer the
new reads); a pointer comment says where they went, and the guards they proved
(dev 1206 incl. dry-run, dev 1194, skip/refuse/reconcile, off-pod) are
re-proven on the new fake. `test_run_json_is_not_mistaken_for_a_shard`
(shard-streaming) now asserts the claim through the inventory instead of the
deleted `_discover_shards`. The live pod test builds a real `run.json`, checks
the execution ledger, calls `verify_run`, and asserts a second ingest appends
nothing. The CLI operator fake gained `parameter_set_orphans`, an optional
`run_id` and the new result shape, plus four new tests (new execution vs
resume, orphan → non-zero exit with results intact, lock takeover warning).

## Verification

The repo's cached baseline (`build-baselines/kbutillib.json`, 99 nodeids,
captured 2026-09-13 in a different minimal venv) is **stale for this worker
venv**, exactly as the dispatcher warned, so I did the detached-worktree
comparison instead and left the cached file untouched (overwriting it with
this venv's 359-failure set would make it wrong for the environment it was
captured in).

`pytest` was not installed in `.task-venv`; I installed it there (9.1.1) —
this task's own venv, nothing shared.

Base worktree: `git worktree add --detach /tmp/base-1870e0a 1870e0a`.
Identical command in the same interpreter on both sides (13 modules whose
*collection* fails on missing optional deps — duckdb, pandas, torch, rdkit —
are `--ignore`d identically on both):

```
python -m pytest -q -p no:randomly \
  --ignore=tests/berdl/test_clearinghouse_derivation.py \
  --ignore=tests/berdl/test_clearinghouse_parity_script.py \
  --ignore=tests/biochem/test_escher_utils.py \
  --ignore=tests/biochem/test_ms_biochem_deltag.py \
  --ignore=tests/biochem/test_ms_reaction_similarity.py \
  --ignore=tests/external/test_prott5_utils.py \
  --ignore=tests/kbase/test_kb_annotation_utils_parquet.py \
  --ignore=tests/modeling/test_comprehensive_gapfill_wrapper.py \
  --ignore=tests/modeling/test_ms_community_utils_live.py \
  --ignore=tests/modeling/test_ms_community_utils.py \
  --ignore=tests/modeling/test_ms_reconstruction_utils.py \
  --ignore=tests/modeling/test_ms_remote_solver_utils.py \
  --ignore=tests/notebook
```

| | base `1870e0a` | branch |
|---|---|---|
| failed | **359** | **359** |
| passed | 3242 | 3298 |
| skipped | 424 | 424 |
| errors | 9 | 9 |

`comm -13 <sorted base FAILED> <sorted branch FAILED>` → **empty**: no test
that failed only on the branch. `comm -23` is also empty, so the two failure
sets are identical by name. Both runs exited 0 from the shell's point of view
(pytest's own exit code is non-zero on both, equally, because of the 359
environment failures).

Targeted: `python -m pytest tests/berdl/test_clearinghouse_ingest.py -q` →
**55 passed**, exit 0. `tests/berdl/test_clearinghouse_capability.py` → 169
passed. `tests/berdl tests/cli` (minus the two duckdb modules) → the only
failures are the pre-existing base ones.

The static guard `test_no_in_lake_insert_or_merge_targets_a_clearinghouse_table`
still passes — the new code issues only `SELECT`/`DESCRIBE` and one
`load()`.

## Reconciled against the tree (prompt said → tree held → what I did)

1. **"The result slot key... If only a private name exists, add a public alias
   `SLOT_KEY_COLUMNS`."** A public `SLOT_KEY_COLUMNS` already existed
   (`clearinghouse_derivation.py:151`, added by lake-5). Imported it; added
   nothing.
2. **"ledger at `$KBU_CLEARINGHOUSE_STATE_DIR/<run_id>/`" with `run_id`
   required** vs **round-2 "`load --run-id R` names the EXECUTION: omitted →
   new execution".** Round 2 is later and binding, so `run_id` is now
   `str | None = None` and *is* the execution id; the ledger path is unchanged
   in shape (`<state root>/<run_id>/`). The parameter keeps the name `run_id`
   so the CLI flag and the published envelope key do not churn.
3. **"the ledger lives in the shard dir (~208, ~1224)" / line numbers from
   `e93972e`.** Stale as the dispatcher said; used the orientation's numbers
   and found everything by name.
4. **"within `backfill run` the shard run and its first execution share one
   id".** There is no `backfill run` verb in this tree (a different task's
   surface), so I exposed the behaviour as
   `ingest_shards(..., adopt_source_batch_ids=True)`, which pins the execution
   id to the shard run id and skips the batch-id rewrite. Nothing calls it yet;
   it is tested directly.
5. **"terminal lines record ... snapshot_id" with state `ingested`.** The
   round-1 amendment says "terminal **committed** ledger line", so the terminal
   success state is now `committed`. No ledger from the old location/schema can
   exist in production (the path changed and the verb never shipped a run), so
   no migration is attempted.
6. **"`--reconcile` ... `>0` means landed, `0` means re-ingest"** vs round-2's
   **equal/zero/other** rule. Round 2 governs: a count that is neither the
   expectation nor zero stops the run.
7. **Namespace orphan check "if it exists only inline inside verify/health,
   factor it into a callable".** It was already a public method
   (`parameter_set_orphans`, line 1080). Called it; changed none of its rules.
8. **"No test that passed on the base commit may fail on your branch"** vs the
   task's own instruction to replace the bronze-mode ingest, move the ledger
   and delete the any-snapshot heuristic. Those three are mutually exclusive
   with the old tests of exactly that behaviour, so I rewrote those tests to
   the new contract (and preserved every *guard* they proved) rather than
   weakening or skipping anything. This is the one place where the regression
   rule and the task scope could not both be satisfied literally; the deleted
   assertions are listed under "Tests" above.

## What I did not do / could not verify

- **No live-pod verification.** Everything here is proven against fakes and
  the pure functions. `tests/berdl/test_clearinghouse_live.py` is updated to
  the new contract (run.json, execution ledger, `verify_run`, double-ingest)
  but it is pod-gated and **skipped** off-pod, so the real
  `data_lakehouse_ingest` DataFrame-mode append, the real `DESCRIBE` row shape,
  the real Spark `left_anti`, and `/global_share/...` lock-dir writability are
  all **unverified on hardware**. The `DESCRIBE` parse (`col_name`, skipping
  `#`-prefixed rows) is inferred from Spark's documented output, not measured.
- **The arrow fallback in `_fill_only_survivors`** (no Spark session) is
  exercised only by tests, because the real `BerdlCapability` now always
  exposes `spark_session()`. It is there so a test double or a future
  non-Spark transport gets the identical answer, and it is documented as such.
- **`build-baselines/kbutillib.json` was not updated** — see Verification.
- **The sharder was not modified** (read-only, as instructed).
- No linter ran: `ruff`/`pyflakes` are not installed in this venv and
  `pyproject`'s lint session needs network. I removed the imports and helpers
  I made unused by hand and checked by grep.
