# Work record: disabled-store-guard

- task_id: disabled-store-guard
- branch: maestro/developer/kbdl-arc-indexer-disabled-store-guard
- commit_shas: b40cbaee110f09104ccbf24a55d8fd096eeaeabe (fix), plus the trailing work-record commit

## summary
A real (non-dry) kbdl index against a store whose RunDatabase.enabled is False now raises StoreDisabledError (message names KBDL_RUN_DB_ENABLED / KB_POD) before any write. The check is explicit (store.db.enabled) in index_swept_jobs and also at the top of run_index, so the CLI fails before any KBDL network call. Stores without .db (CaptureStore used by dry runs, fakes) are unaffected, so dry runs need no enabled DB. The counting loop also skips counting a write made while disabled. The CLI converts the error to click.ClickException (non-zero exit, no traceback). The gate and its defaults are unchanged.

## files_touched
- src/kbutillib/kbdl_index/indexer.py
- src/kbutillib/kbdl_index/run.py
- src/kbutillib/interfaces/cli/kbdl_index.py
- tests/kbdl_index/test_run_phase.py

## success_criteria_check
1. raise before writing, gate named, dry run exempt: pass (tests).
2. honest counting: pass (disabled writes skipped in count).
3. clean CLI error: pass (test asserts exit!=0, gate in output, no Traceback).
4. sweep --index fail-soft: pass; run_pass prints "INDEX FAILED StoreDisabledError: <gate message>" and continues (test).
5. record_id/skip-existing/attribution untouched: pass.

## tests_run
- pytest tests/kbdl_index tests/koros_arc_store tests/cli (--ignore=tests/cli/test_council_cli.py, which needs sqlalchemy): kbdl_index and koros_arc_store all pass; 11 failures in tests/cli/test_ontomap_cli.py, all present in baseline2_failing.txt (not mine). New tests: disabled raises + zero rows (queried from runs table), dry run on disabled works, CLI exit non-zero with gate, enabled records_written == row delta, run_pass logs INDEX FAILED.
- Full baseline suite NOT run (about 286 s; scoped runs chosen).

## caveats
- Existing kbdl_index tests already set KBDL_RUN_DB_ENABLED / db_enabled=True; none relied on the no-op.
- The index-phase failure is logged via print in run_pass (existing mechanism), not a logger.
