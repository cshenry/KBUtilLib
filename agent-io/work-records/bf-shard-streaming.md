# Work record: bf-shard-streaming

- Task: `bf-shard-streaming` (developer)
- Repo: KBUtilLib
- Branch: `maestro/developer/shared-context-this-prompt-is-se-bf-shard-streaming`
- Base commit: `d4042e6` (`fix(clearinghouse): make the write-target guard satisfiable, drop params on Spark`)
- Date: 2026-10-08

## Precondition (clearinghouse-lake-5-convergence): PASSED, all four checks

Checked against the worktree, not assumed:

| Check | Result |
|---|---|
| (a) every `<type>_result` config's columns include `parameter_set_hash` | PASS — all five `<type>_result` `schema_sql` strings carry it |
| (b) `clearinghouse_schema` exports `PARAMETER_SET_TABLE` and `table_configs()` has sixteen configs | PASS — `PARAMETER_SET_TABLE == "parameter_set"`, `len(table_configs()) == 16`, the sixteenth named `parameter_set` with `parameter_set_hash, canonical_json, observed_at, ingest_batch_id` and no `partition_by` |
| (c) `kbutillib.domains.identity` exports `parameter_set_hash` and `DEFAULT_PARAMETER_SET_HASH` | PASS (also `canonical_parameter_set`) |
| (d) `examples/clearinghouse_example_seed.toml` exists | PASS |

Tables are iterated with `table_configs()` / `schema.column_names()` throughout; nothing
in the new code composes a table name or enumerates `ENTITY_TYPES x KINDS`.

## What was built

### `src/kbutillib/domains/kbase/berdl/clearinghouse_shard.py` (the bulk)

1. **Bounded memory by external sort.** `_RunSpool` (`clearinghouse_shard.py:1215`)
   replaces the old `spills: list[list[dict]]`. Records stream into a sort buffer
   bounded by `target_bytes`; at the bound the buffer is bucketed by hash range,
   each bucket sorted and appended as one **sorted run** parquet file under
   `spill_dir` (which the old code created and never used). Each range is then
   produced by a k-way merge of its runs — `_merge_ranges` (`:1456`) — dropping
   duplicates and cutting output batches as the stream goes by. A range is never
   materialised.
   - The merge frontier is bounded **absolutely**, not relative to source size:
     `_MERGE_ROW_BUDGET = 4096` rows total, enforced by shrinking the per-run read
     batch as runs are added (`_RunSpool._read_batch_rows`).
   - `_MAX_MERGE_FANIN = 64`: a range with more runs is reduced in multi-pass
     groups first (`_RunSpool._reduce_fanin`), so neither file descriptors nor the
     per-run read batch degrade with source size. **This went beyond the prompt**:
     a single-pass merge at 264M records would open every run at once and floor the
     read at one row per decode. Reduction passes do *not* deduplicate — the winner
     rule is applied once, on the final merge, so an intermediate pass cannot
     discard a row whose winner has not been seen yet.
2. **`ingest_batch_id` is a per-batch ULID.** `new_ulid()` (`:1028`) — dependency-free,
   48-bit ms timestamp + 80 random bits, Crockford base32, 26 chars, monotonic within
   a process under a lock. Each batch file is `<ingest_batch_id>.parquet` and the id is
   stamped on every row of that batch (`_write_batch`, `:1573`). Replaces
   `f"{source_name}:{table}"`, which was neither unique nor monotonic — and
   `batch-NNNN` names from a counter that restarted at 0 per call, so two sources
   into one table overwrote each other's `batch-0000.parquet`.
3. **`observed_at` is never null.** `resolve_observed_at()` (`:1105`) normalises a
   declared `observed_at_column` value (ISO-8601 string incl. `Z`, `datetime`, `date`)
   to **naive UTC** — naive because every `observed_at` column is declared `TIMESTAMP`,
   not `TIMESTAMP WITH TIME ZONE`; a tz-aware value would make pyarrow infer
   `timestamp[us, tz=UTC]`. Verified end to end: the written parquet column is
   `timestamp[us]`. Without the column, the run's start time is stamped — one value
   for the whole run. A row whose declared column is null (or blank, or unparseable)
   is skipped and counted.
4. **Dedup with a defined winner, across the whole merge.** `_KeySpec` / `_make_sort_key`
   / `_make_dedup_key` (`:1130`–`:1200`). The dedup identity is the sort key's own
   prefix, which is what makes the duplicate check an adjacent-row comparison rather
   than a dict of every key seen.
   - result: the **imported** `clearinghouse_derivation.SLOT_KEY_COLUMNS`, then
     `observed_at` DESCENDING, then canonical payload bytes ASCENDING; first wins.
   - entity/content: `entity_hash`, then canonical mapped-row bytes, then
     `observed_at` ascending as a final tiebreak (a strict refinement of the spec,
     added only so an exact tie is reproducible rather than input-order dependent).
   - registry: `parameter_set_hash`.
   - `last_key` deliberately survives a batch cut, so dedup is across the whole
     merge of a range, not per batch. A trailing run of duplicates after the final
     cut is attributed to the last batch written, so per-batch `rows_deduplicated`
     sums to the rows actually dropped.
5. **Skip accounting.** `SourceAdapter.skipped() -> dict[str, int]` on the ABC with a
   default of `{}`; the sharder merges adapter skips with its own
   (`SKIP_NULL_OBSERVED_AT`, `SKIP_SHARD_VALUE_ERROR`) per source into `run.json`.
   `ShardValueError` is now a counted skip rather than a raise; `ParameterSetError`
   from the per-row TRANSYT validator still propagates (an existing test pins that).
6. **Run manifest.** `publish_unit()` (`:1835`) writes `<out>/run.json` with
   `run_manifest_version, run_id, created_at, kbutillib_version, standardizer_version,
   observed_at, manifest_name, manifest_sha256, slices, units, batches, skipped,
   sources`. Each batch entry carries `table, batch_id, file (relative), rows,
   rows_deduplicated, key_column, hash_min, hash_max, sha256, source (sorted list),
   slice, unit_id, attempt_id`. Written temp + `os.replace`, append-only: the
   run-level header is the first unit's and is preserved verbatim, so re-reading and
   re-serialising leaves every earlier unit's entry byte-identical.
   `manifest_sha256` is of the file bytes when a path is supplied, else of
   `canonical_manifest_text()` — a deterministic sorted-key rendering of the parsed
   manifest (tomli-w is not a dependency, so this is hand-rolled; it is a canonical
   rendering for hashing, not a round-trippable serialiser).
7. **`@len`.** `len(value)` for list/tuple, `len(str(value))` otherwise. The worked
   example's `n_contigs = "@len(contig_ids)"` recorded 14 for a two-contig genome.
8. **Per-source exhaustion.** `SourceAdapter.exhausted() -> bool | None`:
   `None` never iterated, `False` entered but not drained, `True` only when the
   adapter's own loop ran off the end. All three adapters set it. Recorded in
   `run.json` separately from `emitted`/`accepted`.
9. **Winner scope / unit structure.** `shard_table()` (`:1289`) takes
   `[(plan, adapter), ...]` for ONE table and merges them together, so the result
   winner rule and the entity/content first-row rule apply **across** a unit's
   sources. `shard_manifest()` groups plans by table (`_group_plans_by_table`) and is
   one UNIT of a run; it takes `run_id`, `slice_label`, `unit_id`, `attempt_id`,
   `manifest_path`, `observed_at` and `metrics`, all optional, so existing callers
   are unaffected. `shard_source(plan, adapter, out_root, target_bytes=...)` keeps its
   old signature and is now a single-plan wrapper over `shard_table`.
10. **Registry per unit.** `write_parameter_set_registry_shard()` (`:1690`) emits only
    the sets this unit is the first to discover; `discovered_parameter_sets()` reads
    the earlier units' immutable registry batch files (not `run.json`, which cannot
    drift from itself). Keyed/sorted/bounded on `parameter_set_hash`, `key_column`
    recorded, non-null `observed_at`, per-batch ULID. Hash and canonical text come
    from the plan (identity's `canonical_parameter_set`/`parameter_set_hash` via
    `clearinghouse_parameters.result_parameter_set_identity`), never recomputed here.
    New `ShardRegistryError` fires if any `parameter_set_hash` stamped on a result row
    has no registry row explaining it — raised **before** the unit is published, so an
    orphan never enters the inventory.
11. **Relaxed invariant.** `_assert_sorted_and_disjoint(reports, table, key_column)`
    now requires sorted and `hashes[0] >= prev_max` — non-overlapping except for a
    shared boundary value — and takes the key column so it also guards the registry.
12. **Unit selector.** `read_run_manifest()`, `unit_ids()`, `batches_for_unit()`,
    `run_manifest_path()`, `discovered_parameter_sets()` — what ingest and
    verification will address one unit with.
13. Adapters gained `estimate_rows()` / `estimate_rows_method()`: parquet footer
    `num_rows` (file; `None` for jsonl/tsv), `estimated_document_count()` (mongo),
    `COUNT(*)` (lakehouse).
14. `ShardMetrics` — the sharder's own record of what it held
    (`max_sort_buffer_rows`, `max_batch_buffer_rows`, `max_resident_rows`,
    `sorted_runs`, `merge_passes`, `spill_dir_bytes`, `rows_per_range`, …). The
    merge frontier's residency is **measured** inside `_RunSpool._iter_run`, not
    bounded on paper.

### `clearinghouse_derivation.py`
Public aliases `SLOT_KEY_COLUMNS` and `ORDER_COLUMNS` over the private tuples, so the
sharder imports the slot key instead of restating it (the standing rule).

### `clearinghouse_manifest.py`
New optional top-level source key `observed_at_column`, added to `_MAPPING_KEYS` so it
does **not** fall through to the adapter locator (where it would be silently inert),
validated in `_parse_source`, carried on `Source` and `TablePlan`. Docstring updated.

### `src/kbutillib/interfaces/cli/clearinghouse.py`
- `plan` no longer iterates: `sum(1 for _ in adapter.iter_records())` is gone, replaced
  by `estimate_rows()` + `estimate_method` in the envelope, with a warning and
  `row_count: null` when no cheap estimate exists.
- `shard` envelope gained `run_id`, `unit_id`, `run_manifest`, `skipped`, `sources`,
  and per-shard `sha256`, `rows_deduplicated`, `key_column`, `source`, `unit_id`,
  `attempt_id`, `slice`; it passes `manifest_path` down so `manifest_sha256` is of
  the real bytes.

## Verification

Commands and exit codes, all run in this worktree's `.task-venv` (python 3.12.x,
pytest 9.1.1):

| Command | Exit | Result |
|---|---|---|
| `python -m pytest -q tests/berdl/ tests/cli/test_clearinghouse.py tests/cli/test_clearinghouse_operator.py` | 1 | **756 passed, 9 failed, 5 skipped** — the 9 are the pre-existing failures listed below |
| `python -m pytest -q tests/ <11 --ignore>` (branch) | 1 | 359 failed, **3319 passed**, 424 skipped, 9 errors |
| `python -m pytest -q tests/ <11 --ignore>` (base commit, clean worktree at `d4042e6`) | 1 | 359 failed, 3279 passed, 424 skipped, 9 errors |
| `comm -13 base-failures branch-failures` | 0 | **empty — zero regressions** |
| `python -m ruff check <the 7 files this task touches>` | 0 | All checks passed |

**Regression framing.** I captured a same-commit base run rather than comparing against
the cached `build-baselines/kbutillib.json`, and that was deliberate: the cached baseline
was captured at `0f84803` in a minimal venv with 99 `known_failing` nodeids, whereas this
environment fails 359. All 99 cached names still fail here, but 260 of this environment's
failures are absent from the cache, so a name comparison against the cache alone would
have reported 260 false regressions. To get a trustworthy answer I added a detached
worktree at `HEAD` (`/tmp/bf-base`, outside Dropbox, removed afterwards) and ran the
identical command there. The two failure sets are **identical, 368 nodeids each**
(359 FAILED + 9 collection ERROR), and the branch passes 40 more tests. I did **not**
overwrite the committed baseline file: it was captured on different hardware with a
different dependency set, and replacing it would destroy information other machines
compare against.

The 359 failures and 9 errors are all missing optional dependencies in this task venv
(`tomli-w`, `pandas`, `torch`, `rdkit`, the pod/Spark stack). None are in `tests/berdl/`.
`pytest`, `duckdb` and `ruff` were installed into `.task-venv` (this task's own venv) to
run the suite at all; nothing else was touched.

**Pre-existing failures in the files this task touches** (present on `d4042e6` before any
edit, present after, unchanged):

```
tests/cli/test_clearinghouse.py::test_json_emits_exactly_one_envelope_object[health]
tests/cli/test_clearinghouse.py::test_json_stdout_has_no_diagnostic_lines[health]
tests/cli/test_clearinghouse.py::test_human_render_receives_same_dict_as_serialiser[health]
tests/cli/test_clearinghouse.py::test_offpod_stats_include_files_degrades
tests/cli/test_clearinghouse.py::test_content_all_types_uses_lossy_view_and_warns
tests/cli/test_clearinghouse.py::test_health_offpod_cannot_evaluate_warns
tests/cli/test_clearinghouse_operator.py::test_load_dry_run_writes_nothing
tests/cli/test_clearinghouse_operator.py::test_load_json_stdout_is_clean_when_warnings_present
tests/cli/test_clearinghouse_operator.py::test_plan_invalid_manifest_exits_nonzero_naming_key
```

Eight of the nine are one environment cause: this venv has click 8.1.6, whose
`CliRunner` mixes stderr into stdout (`mix_stderr=True`), so every test asserting that
`--json` stdout is one clean object fails whenever a warning is emitted. (I confirmed it
directly with a two-line click app.) The ninth is a genuine pre-existing assertion
mismatch: `test_plan_invalid_manifest_exits_nonzero_naming_key` expects "sequence" in
the error, but the `[source.hash] raw_column is not allowed for entity_type 'genome'`
rule now fires first. Neither is in scope here and neither was touched. One new test I
added (`test_plan_reports_a_null_row_count_rather_than_scanning_for_one`) emits a
warning, so it extracts the envelope line-wise rather than assuming separated streams.

Also smoke-tested the real CLI end to end on a throwaway manifest: `kbu clearinghouse
plan` (reports `estimate_method: parquet-footer-num-rows`, no scan) and `kbu
clearinghouse shard` (writes ULID-named batches, a `parameter_set` batch with
`key_column: parameter_set_hash`, and a `run.json` whose sha256s match disk). Confirmed
on the bytes that `observed_at` is `timestamp[us]` and non-null on all four tables.

## Tests added

`tests/berdl/test_clearinghouse_shard_streaming.py` — 36 tests, all passing:

- union of shards == deduplicated input with a small `target_bytes`; spill bytes > 0
- **one hash repeated 12,000 times** (≈12x the sort buffer) lands in one range:
  `max_sort_buffer_rows < rows/5`, `max_resident_rows <= _MERGE_ROW_BUDGET + 64`,
  and the range collapses to one row
- a range exceeding `_MAX_MERGE_FANIN` runs merges in several passes, output still
  exactly the deduplicated input in sorted order, residency still inside the budget
- every batch id a distinct ULID equal to its file stem, stamped on every row of its
  batch; ULIDs monotonic and collision-free over 2000 mints; Crockford alphabet
- two sources into one table: distinct files, nothing lost
- no null `observed_at` (run start, or the declared column); `observed_at_column`
  honoured for `Z`/offset/naive forms and normalised to UTC; it is mapping not locator;
  a null is skipped and counted as `null-observed_at`; an unparseable value likewise;
  an adapter's own `skipped()` merges into the same list
- consecutive duplicate hashes dropped (content), duplicate slots dropped by the
  imported slot key, one hash with several slots keeps every slot, **A/B/A keeps the
  newest A**, payload bytes break an `observed_at` tie deterministically from either
  input order, dedup survives a batch cut, per-batch counts sum to the real drop
- two sources in one unit sharing a slot → one row with the later `observed_at`, and
  the mixed batch's `source` is the sorted list of both; entity rows from two sources
  collapse; `shard_table` refuses plans for different tables
- adjacent batches share only a boundary hash (40 slots on one hash, several batches,
  all `split=True`); an unsorted shard fails the build
- `run.json`: every batch with a sha256 matching disk, relative `file` resolving from
  the root, ULID `unit_id`/`attempt_id`, real `kbutillib_version`, real
  `manifest_sha256`; per-source exhaustion; a `--limit` stop is **not** exhaustion;
  append-only across two units with the first unit's entries byte-identical and
  addressable through the unit selector; not mistaken for a shard by
  `_discover_shards`; in-memory fallback to `canonical_manifest_text`
- registry: two parameter sets → a two-row batch bounded on `parameter_set_hash`
  (recorded as such in `run.json`, written first); a second unit registers **only** the
  set it discovers and leaves the first unit's batch byte-identical; a unit discovering
  nothing new writes no batch; the orphan check against disk
- `@len` on list/tuple/str/int/None, and reaching the written parquet via `n_contigs`
- the GAA worked example still plans

`tests/cli/test_clearinghouse_operator.py` — 4 tests added: `plan` never iterates
(guarded by an adapter whose `iter_records` raises), `plan` reports `row_count: null`
plus a warning rather than scanning, a jsonl source has no cheap estimate, and the
`shard` envelope carries the run inventory with matching sha256s.

## Tests changed, and why

Two existing tests encoded design this task supersedes. Both were updated, not weakened:

1. `test_shard_layout_and_deterministic_names` asserted `batch-{i:04d}.parquet` names
   contiguous from 0000 and that a re-run reproduced byte-identical batch ids. The task
   requires `<ingest_batch_id>.parquet` with a per-batch ULID, which is deliberately
   **not** reproducible — a stable name is precisely what let two writers collide. It now
   asserts stems == batch ids, every id a ULID, ids unique within and disjoint across
   runs, and that the computed hash RANGES still reproduce exactly.
2. `test_two_result_sources_write_hashes_and_a_two_row_registry_shard` read
   `gene_result/batch-0000.parquet` by name and asserted the registry row's
   `observed_at is None` and `ingest_batch_id == "default-run:parameter_set"`. It now
   globs, and asserts the registry obeys the same non-null `observed_at` and per-batch
   ULID rules as every other table, plus `key_column == "parameter_set_hash"`. Its
   docstring's claim that two sources into one table would overwrite each other was
   also corrected — that is now a supported, tested case.

## Divergences between the prompt and the tree (reconciled, not kicked back)

| Prompt claimed | The tree held | What I did |
|---|---|---|
| defects "verified at KBUtilLib e93972e" | worktree is at `d4042e6`; every named defect was still present but at shifted lines | Found each by name. Actual locations: `shard_source` ~1079-1160, `_build_row` ~1124, `ingest_batch_id = f"..."` ~1113, `batch-NNNN` ~1140, `@len` ~925, CLI `sum(1 for _ in ...)` ~1047. |
| `@len` at ~911-913 | ~925 | Fixed at the real location. |
| `_build_row` at ~957-975 | ~1124 | Same. |
| `ingest_batch_id` at ~1083 | ~1113 | Same. |
| CLI `plan` scan at ~815 | ~1047 (`_build_plan`) | Same. |
| hash rules "in `src/kbutillib/domains/identity/standardizers.py`" | parameter-set identity actually lives in `kbutillib.domains.identity` (re-exported) and is reached through `clearinghouse_parameters.result_parameter_set_identity` | Used the shipped helpers via the plan, which already computes them once at plan time. |
| "The result slot key is IMPORTED … if only a private name exists, add a public alias" | only `_SLOT_KEY_COLUMNS` existed | Added `SLOT_KEY_COLUMNS` (and `ORDER_COLUMNS`) and imported it. |
| "the sharder emit a `parameter_set` shard per run … keep that" | `write_parameter_set_registry_shard` existed and wrote `batch-0000` with `observed_at=None` | Kept the function and its contract, rewrote it to per-unit immutable batches with the new provenance rules. |
| item 1: "skew-split as today (1.8x target)"; item 4: dedup "after sorting … per batch" | superseded by the Confront round-1 amendments (external merge, dedup across the whole merge) | Followed the amendments. `SKEW_SPLIT_FACTOR` is retained as the batch **cut** threshold, which preserves the constant's meaning (a range under 1.8x still yields one shard). |
| item 6: run.json "rewrites it atomically after each source completes" | superseded by Confront round-2 ("published … atomically when that unit finishes sharding") | One atomic publish per unit. |
| round 2: "A mixed batch's `source` field is the sorted list of contributing source names" | ambiguous once dedup can eliminate a source's only row | `source` lists the sources whose rows are actually **in** the batch. My first test asserted both names on a batch holding only the winner's row; I changed the test (gave each source a slot of its own as well as the shared one) rather than invent a "contributed to the merge but not the bytes" notion. |
| "the sharder emits a parameter_set shard with one row per distinct parameter set **used**" | per-row parameter sets do not exist yet (`parameter_set_column` is still rejected by the manifest) | Registry rows come from the plans (static), which is the only source of sets today; the new `ShardRegistryError` orphan check guards the per-row case if it lands, since the registry must be written before results. |

## What I did not do, and what I inferred rather than verified

- **Ingest-side enforcement of registry ordering per unit** ("its earlier registry batch
  must be committed before this unit's results load — ingest enforces that") is **not**
  built here. `ingest_shards` / `order_shards_for_load` already order
  `PARAMETER_SET_TABLE` ahead of every result table, which covers ordering within one
  shard directory, but per-unit commit gating belongs to the ingest task that owns
  `clearinghouse_capability.py`. This task supplies what that task needs: the unit
  selector (`unit_ids` / `batches_for_unit`) and `discovered_parameter_sets`.
- **No CLI slice flag.** `shard_manifest`/`shard_source` take `run_id`, `slice_label`,
  `unit_id` and `attempt_id`, and `run.json` records `slices`, but `kbu clearinghouse
  shard` exposes no `--slice`/`--run-id` option — the prompt scopes slice driving to
  "a later CLI task". Existing callers keep working unchanged.
- **Scale is extrapolated, not measured.** The largest run exercised here is 12,000
  records. The bounds are structural (sort buffer ≤ `target_bytes`; merge frontier ≤
  `_MERGE_ROW_BUDGET` rows and ≤ `_MAX_MERGE_FANIN` open files; one output batch ≤
  1.8x `target_bytes`) and the tests assert the measured high-water marks against them,
  but no run at hundreds of millions of records has been performed. `_MERGE_ROW_BUDGET`
  and `_MAX_MERGE_FANIN` are conventions chosen for those properties, not measurements —
  stated as such in their docstrings, as `DEFAULT_TARGET_BYTES` already is.
- **`canonical_manifest_text` is not a round-trippable TOML serialiser**, only a
  deterministic rendering for the provenance hash. tomli-w is not a dependency (and is
  in fact missing from this venv).
- **Naive-UTC `observed_at`** matches the `TIMESTAMP` DDL and the parquet column is
  `timestamp[us]`; I verified that on disk but have not verified how
  `data_lakehouse_ingest` coerces it on a real write — that needs the pod.
- A naive `datetime` or naive ISO string in a declared `observed_at_column` is
  **assumed** to be UTC. A source storing local times without an offset cannot be
  disambiguated at this layer and must be fixed at the source; the assumption is
  documented in `resolve_observed_at`.
