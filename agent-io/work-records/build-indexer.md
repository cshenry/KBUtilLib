# Work record: build-indexer

task_id: build-indexer
branch: maestro/developer/kbdl-arc-indexer-build-indexer

## commit_shas
- 6b5b089 feat: KBDL swept-job indexer into the KOROS arc store
- (this work-record is committed on top)

## summary
Adds `src/kbutillib/kbdl_index/indexer.py` with `index_swept_jobs(store, resolver, attributor, ledger_path, results_root, dry_run=False, job_meta=None) -> IndexReport`, writing one AnalysisRecord per KBDL BuildGenome/GenomeAnnotation job (run_uid = job id, so re-indexing replaces). Uses the existing attributor and subject resolver unchanged. No network calls.

## Conductor-directed amendments (A-E), implemented as directed
- A. Population is the union of ledger entries of indexable type plus `<results_root>/<JobType>/<job_id>.json.gz` files with no ledger entry (file-only = recovered, status ok). Report has `from_ledger` and `from_files_only`.
- B. Extra optional `job_meta` arg. created_at precedence: job_meta finished_at, job_meta submitted_at, ledger submitted_at; source recorded in `provenance['created_at_source']`. No date: record not written, counted `undated`; no wall clock or mtime. Normalised to `YYYY-MM-DDTHH:MM:SS.ffffffZ` (UTC).
- C. 'failed' only when `job_meta[job_id]['state']` is failed. Ledger 'expired' -> expired record, no result artifact.
- D. Report carries resolver ambiguity count and attributor `disagreeing_jobs` / `multi_arc_discarded`.
- E. Tests for each added.

## files_touched
- src/kbutillib/kbdl_index/indexer.py (new)
- tests/kbdl_index/test_indexer.py (new, 15 tests)
- agent-io/work-records/build-indexer.md

## success_criteria_check
- index_swept_jobs/IndexReport with seen/written/unresolved/skipped/expired/attribution counts: pass.
- Nine named tests plus A/B/C tests, asserting on queried row counts: pass (15 in test_indexer.py, 35 in tests/kbdl_index).
- Same job twice = one row; re-index after attribution moves the row (absent from list_analyses(None, None)): pass.
- UploadObject yields zero rows: pass.
- No base-passing test fails: pass (see tests_run).

## tests_run
- `pytest tests/kbdl_index`: 35 passed.
- Full baseline command: 170 failed, 110 errors (=280, same count as baseline), 6009 passed. Every FAILED id is in baseline2_failing.txt (checked with comm). The 110 errors were counted, not id-diffed.

## caveats
1. STATUS CONFLICT (needs a decision): the task says status 'expired', but the arc store's `STATUS_VALUES` is {ok, failed, partial}; 'expired' is rejected by validation (soft-fail, so the row would silently not exist). I did not change the frozen store contract. Expired jobs are written with status 'failed' (constant `EXPIRED_STATUS` in indexer.py), `payload['result_state'] = provenance['result_state'] = 'expired'`, and no result artifact. If the contract is widened, change that one constant and the test.
2. Expired/failed jobs with no result file have no resolvable subject; subject is `kbdl-job:<job_id>`, counted as expired, not unresolved.
3. 'homology' tier requires provenance bridge_kind + metric; annotation records use bridge_kind 'sequence_homolog', metric feature_count (int). Verified tier (build_genome) carries no bridge.
4. Ledger outcome other than saved/expired (e.g. 'error') with no file: counted as `ledger_errors`, not written. A ledger 'saved' whose file is missing counts as unresolved. If a job appears on several ledger lines, the last wins; an existing file takes precedence over ledger outcome.
5. Store soft-failures are detected through `failed_write_count` and counted as `write_failed` rather than `records_written`.
6. significant_params = {job_type, object_id}; floats are stringified defensively (none occur today). Non-indexable job types are only counted from the ledger, not from files.
