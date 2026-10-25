# Work record: measure-bridge-coverage

- task_id: measure-bridge-coverage
- branch: maestro/developer/kbdl-arc-indexer-measure-bridge-coverage

## commit_shas
See `git log ec78ea7..HEAD` on the branch (script commit f617acb, then the commit adding report and this record).

## summary
Added a read-only measurement script and ran it on kbhub. Q3 and Q4 are answered with explicit counts. Q1 and Q2 are BLOCKED: the KBase token at ~/.kbase/token is rejected by the auth service (401), so list_objects() failed. I did not touch the token. Rerunning the script with a valid token fills them in.

## files_touched
- agent-io/research/kbdl-bridge-coverage/measure_bridge_coverage.py
- agent-io/research/kbdl-bridge-coverage/report.md
- agent-io/work-records/measure-bridge-coverage.md

## success_criteria_check
- report.md exists on branch: pass.
- Q1 exactly-one/multiple/none counts: FAIL (blocked by 401). File-side numbers (3302 files, 2203 prefixes) are present.
- Q2 colliding prefix count: FAIL (blocked by 401).
- Q3 overlap count: pass, 51 (ledger-scoped), 517 (wider population); differs from the 92 in the brief, explained in the report.
- Q4 per-job-type exactly-one / more-than-one / none: pass, two populations stated; round-1 annotation number confirmed, BuildGenome corrected 49 to 51.
- No arc-store write and no submit_*: pass; only list_analyses and a mode=ro sqlite count.

## tests_run
No test suite run: read-only measurement script, no tests added, baseline not needed. The script ran end to end (rc=0) three times; list_objects raised 401 each time.

## caveats
- KorosArcStore needed an explicit runs_root; KOROS_HOME is unset in the agent shell, so the script defaults to ~/Dropbox/Science/runs.
- Result files now number 3302 annotation (brief said 2997); the ledger has 185126 lines.
- A hand-written summary section is appended to report.md; regenerating overwrites it.
