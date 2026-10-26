# deploy-merge-20261026 (scratch clone only)

Branch deploy/main-20260926: merge of dropbox/main (b1cbc40) into 3b105a1.

## Commits
- a0551c2 merge commit (parents 3b105a1, b1cbc40)
- 5de8343 fix(koros-arc-store): describe/scope methods soft-fail via failed_write_count/logger (+ tests/koros_arc_store/test_describe_soft_fail.py; 4 tests fail without the fix, pass with it)
- a2f9305 test: reconcile two main-side tests with the pod's defaults

## Conflict resolutions
1. run_db.py: single add/add hunk, kept BOTH (pod describe_record/_PROJECT_SCOPE/scope_description methods + main record_submission/list_submissions). Auto-merged parts checked: _ensure_schema creates scope_description AND job_submissions; _default_pod_enabled returns True unless KBDL_RUN_DB_ENABLED says otherwise (pod 46c789b behaviour kept); imports/constants (logger, failed_write_count, canonical_json) serve both sides.
2. test_clearinghouse_ingest.py: kept both test blocks (union merge vs base d19d674; 91 main tests + 4 pod tests = 95). Main had ALREADY fixed the --dataset namespace bug independently (write_ns threaded through _existing_keys, _live_columns, _count_batch_rows, verify_run, ...); src auto-merged cleanly with one definition of each of _existing_keys/verify_run/_assert_write_target and no conflict markers, so no src change was needed. verify_run has both unit and dataset keywords.
3. test_clearinghouse_operator.py: fake verify_run(self, shard_dir, *, run_id, unit=None, dataset=None), matching the merged real signature; the recorded dict keeps main's "unit".

Note: my first resolution of conflict 2 mis-placed a separator (left a stray "=======" line, SyntaxError found by the first AFTER run); the merge commit was rebuilt (amend) with the corrected file before the final runs.

## Pre-existing pod bug (separate commit 5de8343)
self._failed_writes -> self.failed_write_count (4 sites) and log.warning -> logger.warning (2 sites) in describe_record / set_scope_description.

## Test comparison
Command: the base command from the envelope plus -rfE, interpreter /home/chenry/venvs/kbu-modeling/bin/python, -p no:cacheprovider. Identical ignore set both runs (14 modules: tests/berdl/test_clearinghouse_derivation.py, test_clearinghouse_parity_script.py, test_council_schema.py, test_council_disposition.py; tests/cli/test_council_cli.py; tests/domains/ai/council/{test_approval,test_batched_invocation,test_council_utils,test_four_queue_integration,test_quote,test_quote_plan}.py; tests/domains/test_ontomap_{council_plan,council_run,unrun_bundle}.py). Both runs exported KBDL_RUN_DB=<tmp file> and KOROS_HOME=<tmp dir> so the real ~/.kbdl/runs.sqlite and runs tree were not touched.
- BEFORE (worktree at 3b105a1): 80 failed, 5236 passed, 391 skipped, 1 xfailed, 94 errors (174 failing ids).
- AFTER (final HEAD a2f9305): 168 failed, 6230 passed, 422 skipped, 1 xfailed, 110 errors.
- Fixed by merge: 0 ids. Failing-before ids still fail after.
- ids failing AFTER not BEFORE: 104, explained:
  - 101 are in 5 test files NEW on main (test_ontomap_cli council tests, test_rate_pacer, test_ontomap_council_export/publish/status); they fail identically on a pristine dropbox/main worktree: ModuleNotFoundError sqlalchemy in the kbu-modeling venv (same cause as the ignored council modules). Not a merge effect.
  - 3 are tests/interfaces/test_mcp_server.py::TestUnavailableHandler: pass in isolation and in tests/interfaces, fail in the full run (asyncio.get_event_loop() has no loop). Reproduced as event-loop pollution by running new main file tests/literature/test_europepmc.py before them (3 failed). Test-isolation fragility from a main-side test, not a code regression; left unfixed.
- Genuine merge interactions found and fixed (commit a2f9305): test_run_phase.py disabled-store tests (4) assumed run DB off by default, which the pod's 46c789b reverses -> fixture sets KBDL_RUN_DB_ENABLED=0; test_kbdl_service_utils::test_status_many hardcoded 127.0.0.1:8791 vs pod TEST_BASE_URL poplar -> asserts against TEST_BASE_URL.

## Smoke checks (item 4)
The explicit smoke command was refused by the koros secret-guard hook (false positive); not reworded to evade. Covered indirectly: tests/kbdl_index passes (imports of the package), the test_run_phase disabled-store tests asserted enabled-by-default is True before being fixed (pod default survives), and tests/koros_arc_store (describe/scope, record_submission/list_submissions) pass.
