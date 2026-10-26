# Work record: wire-sweep-phase

task_id: wire-sweep-phase
branch: maestro/developer/kbdl-arc-indexer-wire-sweep-phase

## commit_shas
- 779cabd feat: wire KBDL index phase into sweep loop; add kbu kbdl-index verb
- (this work-record committed on top)

## STATUS: BLOCKED ON THE DRY RUN (koros secret-guard refusal)
The dry-run command was refused by the PreToolUse hook. Verbatim:

    PreToolUse:Bash hook error: koros secret-guard: this would expose a credential (token / key / .env). koros NEVER prints or reads secrets into context — use the value from the environment programmatically, never echo/cat/read it. If you must edit your keys, do it in your own terminal or the KING settings panel, not through the agent.

Per amendment A I stopped at once and did not reword or retry. The command
contained `--token-file ~/.kbase/token`; the token is only passed as a path and
is never read by me. NO dry-run counts exist, NO overlap numbers were measured by this
task (the conductor's measured figures, 51 / 517, are from the coverage report), and the
real index was NOT run (stop rule D, and the dry run did not happen). Nothing was written to ~/.kbdl/runs.sqlite.
A human can run:
`kbu kbdl-index --dry-run --json --token-file ~/.kbase/token` (this also prints
dated_from, undated, attribution by source, and replace-vs-duplicate with 5 examples each).

## summary
Adds `kbdl_index/run.py` (shared index run: fresh resolver + attributor, one list_jobs for job_meta,
optional bounded fail-soft check_job for finished_at, CaptureStore dry run, replace-vs-duplicate overlap
check via the store's public list_* API); `index_swept_jobs` gains an optional `job_types` filter;
`sweep.py` gains `--index` (opt-in) and `run_pass` (sweep, then index; each phase fail-soft;
single-threaded); new `kbu kbdl-index` verb with --dry-run, --job-type, --token-file, --fetch-finished-at,
--max-finished-calls, --json.

## files_touched
- src/kbutillib/kbdl_index/run.py (new), indexer.py, sweep.py
- src/kbutillib/interfaces/cli/kbdl_index.py (new), __init__.py, README.md
- tests/kbdl_index/test_run_phase.py (new, 11 tests)

## success_criteria_check
- Sweep loop runs an index phase after sweeping; CLI verb with --dry-run and job-type filter: pass (offline tests; phase is opt-in via --index).
- Dry run over full ledger executed, counts recorded: FAIL (blocked by secret-guard, see above).
- Overlap checked, real index not performed if non-zero: real index not performed; overlap tool built and tested but not run live. Conductor-measured overlap is 51 (>0).
- be836b9 is an ancestor of this branch (git merge-base --is-ancestor): pass. The deployed tree
  (/global_share) was not checked; it lacks kbdl_index entirely per the conductor.
- No threading introduced: pass.
- No base-passing test fails: pass (below).

## tests_run
- `pytest tests/kbdl_index`: 46 passed.
- Full baseline command: 170 failed, 110 errors (280), 6020 passed; the set of FAILED/ERROR node ids equals baseline2_failing.txt exactly (comm -23 empty).

## caveats
- `--index` is opt-in, not default: the daemon would otherwise write rows that duplicate the 51 existing rows once deployed. Enabling it on the pod is a human decision after the overlap one.
- check_job's finished-at field name is unverified (tries finished_at, completed_at); fail-soft if absent.
- Dry run reuses the indexer with a capture store (dry_run=False internally) so exact record_ids are known; the report is relabelled would_write.
- Existing-row scan reaches only (project, arc) pairs in the runs tree plus unattributed.
