# alcf-bench-report

- task_id: alcf-bench-report
- branch: maestro/developer/kbu-alcf-alcf-bench-report
- commit_shas: 63a371d (feature + tests); work-record commit follows

## summary
Added scripts/council/alcf_bench_report.py (computation in importable functions, I/O in load_job/main) and tests/domains/ai/council/test_alcf_bench_report_script.py (16 tests, synthetic verdict sets, no store). Output order: worksheet (file via --worksheet, counts on stdout), coverage, agreement, profile, historical population context, failure inventory. No pass/fail verdict; summary says agreement is a diagnostic.

## files_touched
- scripts/council/alcf_bench_report.py
- tests/domains/ai/council/test_alcf_bench_report_script.py
- agent-io/work-records/alcf-bench-report.md

## success_criteria_check
All criteria: pass by test (see test names), except the store-reading path (load_job) which is untested against a real SqliteStore (no store here): uncertain.

## tests_run
- new file: 16 passed.
- full suite (baseline command): 4 failed, 7982 passed, 576 skipped; the 4 failures are exactly the known base failures by node id.

## caveats / judgment calls
- Baseline eligibility "go_mf@4" = jobs.query_name 'go_mf' and query_version '4' (the yaml says version '1'; job-level version is what the prompt's "v4" matches).
- Refusal set: duplicate eligible match; baseline row with same item_id but different entity_hash/data_hash; baseline job not go_mf@4; job not found; empty --baseline-jobs. A candidate item with no baseline row at all, or only a non-ok baseline row, is reported as unmatched (not refused) and excluded.
- "matched" coverage count = candidate items with an eligible ok baseline, regardless of candidate status; agreement/profile admit only candidate-ok.
- Proposer count = distinct seat_name of role 'proposer' in the job's calls; unknown -> decided_by deltas suppressed.
- 95% Wilson interval printed for both the candidate and baseline decline proportions.
- Meta invocation rate = items with a meta call row / items with any call (candidate job).
- Halt reasons read from endpoint_state per candidate endpoint.
