# Work record: measure-bridge-coverage

- task_id: measure-bridge-coverage
- branch: maestro/developer/kbdl-arc-indexer-measure-bridge-coverage

## commit_shas
See `git log ec78ea7..HEAD` on the branch (script commit f617acb, then the commit adding report and this record).

## summary
Added a read-only measurement script and ran it on kbhub. All four questions are answered. Q1: of 3302 annotation files, 3194 resolve to exactly one object, 0 to several, 108 to none. Q2: 0 colliding prefixes among 94051 objects. Q3: 51 overlapping rows (517 in the wider population). Q4: see report. Token: the pod env KBASE_AUTH_TOKEN is stale (401); SharedEnvUtils prefers it over ~/.kbase/token, so the script passes the file token explicitly via --token-file at Chris's direction; it is never printed or written.

## files_touched
- agent-io/research/kbdl-bridge-coverage/measure_bridge_coverage.py
- agent-io/research/kbdl-bridge-coverage/report.md
- agent-io/work-records/measure-bridge-coverage.md

## success_criteria_check
- report.md exists on branch: pass.
- Q1 exactly-one/multiple/none counts: pass, 3194 / 0 / 108 = 3302.
- Q2 colliding prefix count: pass, 0.
- Q3 overlap count: pass, 51 (ledger-scoped), 517 (wider population); differs from the 92 in the brief, explained in the report.
- Q4 per-job-type exactly-one / more-than-one / none: pass, two populations stated; round-1 annotation number confirmed, BuildGenome corrected 49 to 51.
- No arc-store write and no submit_*: pass; only list_analyses and a mode=ro sqlite count.

## tests_run
No test suite run: read-only measurement script, no tests added, baseline not needed. The script ran end to end (rc=0) three times; The final run, with the token passed explicitly, returned 94051 objects.

## caveats
- KorosArcStore needed an explicit runs_root; KOROS_HOME is unset in the agent shell, so the script defaults to ~/Dropbox/Science/runs.
- Result files now number 3302 annotation (brief said 2997); the ledger has 185126 lines.
- A hand-written summary section is appended to report.md; regenerating overwrites it.
