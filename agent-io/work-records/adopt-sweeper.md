# Work record: adopt-sweeper

- task_id: adopt-sweeper
- branch: maestro/developer/kbdl-arc-indexer-adopt-sweeper
- commit_shas: d4ae0bbd6e2647ab119f7d0c563688052b8c383a (feature), plus the trailing work-record commit

## summary
Moved the untracked ~/kbdl_sweep/sweep.py into the library as src/kbutillib/kbdl_index/sweep.py. The five design properties, docstring, ROOT/LEDGER/MIRROR constants and all logic are unchanged. The only edits are the import block (the /global_share sys.path hack was removed) and the removal of locus_prefix and PREFIX. These now live in src/kbutillib/kbdl_index/subject.py, and sweep.py imports locus_prefix from there. scripts/kbdl_sweep_launcher.py is a thin launcher with identical CLI flags.

## files_touched
- src/kbutillib/kbdl_index/__init__.py, sweep.py, subject.py (new)
- scripts/kbdl_sweep_launcher.py (new)
- tests/kbdl_index/__init__.py, test_sweep.py (new)
- agent-io/work-records/adopt-sweeper.md

## success_criteria_check
- sweep.py and subject.py exist: pass
- locus_prefix defined once: pass (grep "def locus_prefix" src finds only subject.py; a test asserts identity)
- ROOT/LEDGER/MIRROR byte-identical: pass (the three assignment lines match the original; the MIRROR line number moved from 53 to 52)
- offline tests for resumability and locus_prefix pass: pass
- no regressions: pass for the scoped run. The full suite was not run.

## tests_run
- /home/chenry/venvs/kbu-modeling/bin/python -m pytest -q -p no:cacheprovider tests/kbdl_index tests/koros_arc_store: 194 passed, 2 skipped. The baseline command with these paths appended was not used verbatim, so the baseline --ignore flags were not applied; none of the ignored files are in these paths.
- Full suite not run. It is only new files plus no edits to existing ones, so there is no regression surface.

## caveats
- The launcher is NOT installed on the pod. Per the conductor override, nothing under ~/kbdl_sweep/ was touched and daemon pid 415 was left alone. Installing it (copy scripts/kbdl_sweep_launcher.py over ~/kbdl_sweep/sweep.py) is a post-deploy step for a human, once the deployed kbutillib tree contains kbdl_index.
- The launcher relies on kbutillib being importable (installed or on PYTHONPATH). The original's hardcoded /global_share sys.path insert was dropped.
- Not fixed, as instructed: the stale KBASE_AUTH_TOKEN env var.
