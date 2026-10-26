# Work record: skip-existing

- task_id: skip-existing
- branch: maestro/developer/kbdl-arc-indexer-skip-existing
- commit_shas: 5001f0a8e5b41430c607876f0badc4bbf6e9b6f9 (feature + tests); work-record committed after it (see git log)

## Provenance
Not in the original taskplan. Chris approved it on 2026-10-26 after reading the dry-run result (8,098 would-write; 515 jobs already have rows from 'koros.fitness-genome-analysis' / 'kbdl-job-api-backfill'; overlap replace=0, duplicate=515). Decision: existing rows win and are left untouched.

## Summary
`index_swept_jobs` gains `skip_existing_run_uids`; matching jobs are not written and are counted in `IndexReport.skipped_existing` (shown in `summary()`). `run.py` gets `existing_rows_by_run_uid(store)` (the single public-API traversal: every project/arc plus `list_analyses(None, None)`), now shared with `overlap_vs_existing` via a new `existing=` argument, and `foreign_run_uids()` which excludes any run_uid having only `producer == 'kbdl-job-indexer'` rows so the indexer's own rows keep the upsert-by-record_id path (re-attribution still works). `run_index(skip_existing=True)` builds the set; `kbu kbdl-index --skip-existing/--no-skip-existing` (default on) passes it; dry-run output adds `skip_existing`/`skipped_existing` next to the overlap numbers. `sweep.index_phase` calls `run_index` with default, so it skips by default. No record_id/analysis_id derivation changed.

## files_touched
- src/kbutillib/kbdl_index/indexer.py
- src/kbutillib/kbdl_index/run.py
- src/kbutillib/interfaces/cli/kbdl_index.py
- tests/kbdl_index/test_indexer.py (4 new tests)
- tests/kbdl_index/test_run_phase.py (existing duplicate-overlap CLI test now passes --no-skip-existing to keep its intent)

## success_criteria_check
1. indexer param + skipped_existing in report/summary: pass.
2. set built from public read API, shared traversal: pass.
3. CLI flag default on, dry-run duplicate=0, skipped shown; sweep uses default: pass (sweep inherits via run_index default; no dedicated sweep test beyond existing test_loop_pass passing).
4. own-producer rows excluded; both directions tested: pass.
5. no id derivation changes: pass.
Tests: foreign row blocks write (row count and row content unchanged, skipped_existing==1); --no-skip behaviour writes the duplicate; own row not in skip set and re-attribution moves it (one row under arc); CLI dry run default duplicate=0, skipped=1, and --no-skip-existing gives duplicate=1.

## tests_run
- tests/kbdl_index: 50 passed.
- Full baseline command (268 s): 170 failed + 110 errors = 280 ids, identical set to baseline2_failing.txt (no new failures, none fixed).

## caveats
- In a dry run the overlap section shows duplicate=0 by construction because skipped jobs are never captured; the effect is visible via `skipped_existing`.
- A run_uid with both an indexer row and a foreign row is skipped (any foreign row blocks).
- Not run against real data (per instructions).
