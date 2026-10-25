# Work record: stamp-submissions

- task_id: stamp-submissions
- branch: maestro/developer/kbdl-arc-indexer-stamp-submissions
- commit_shas: f12d135b31eda780d5e47e8ceff3f71d3b8a179d (feature), plus the trailing work-record commit

## summary
Adds a `job_submissions` table to the run DB with fail-soft `record_submission` (same enablement gate, busy timeout, schema-init retry, SOFT_FAIL_PREFIX and failed-write counter as `record_analysis`) and raising `list_submissions`. Both are delegated on `KorosArcStore` and mirrored on `FakeKorosArcStore`. `KBDLServiceUtils._submit` now calls a private `_stamp_submission` after the job id exists. It resolves the arc via `resolve_current_arc(explicit=self._arc)`, writes nothing on `None`, and otherwise records with `socket.gethostname()`. The whole helper is wrapped in try/except. The constructor gains optional `arc`. The envelope sent to the service is unchanged.

## files_touched
- src/kbutillib/koros_arc_store/run_db.py
- src/kbutillib/koros_arc_store/store.py
- src/kbutillib/koros_arc_store_testing.py
- src/kbutillib/domains/external/kbdl_service_utils.py
- tests/external/test_kbdl_submission_stamp.py (new)

## success_criteria_check
- RunDatabase/KorosArcStore expose both methods backed by job_submissions: pass.
- _submit stamps only when an arc resolves and returns the id unchanged on stamp failure: pass (tests for no arc, unwritable DB, patched resolver raising).
- Constructor accepts optional arc: pass.
- Envelope byte-identical: pass by inspection; `package` construction and the POST are untouched.
- Five named offline tests pass, asserting via list_submissions: pass (plus two extras: swallowed resolver exception, disabled gate).
- No base-passing test fails: pass. The full baseline command gave 280 failing node ids, all in the baseline file.

## tests_run
- tests/external tests/koros_arc_store: 392 passed, 7 skipped.
- Full baseline command, after the commit: failing node-id set compared with baseline2_failing.txt (280 ids): no new failures.

## caveats
- The submitted_at timestamp is generated in the client helper using the same format as `_utc_now` in run_db.py. That function is private, so the format was duplicated rather than imported.
- A failed stamp is logged by the helper at WARNING; when only the DB write fails, the store's own soft-fail log line also appears.
- The sqlite dir is created at the first write, as with record_analysis.
