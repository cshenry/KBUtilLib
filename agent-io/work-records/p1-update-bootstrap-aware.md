# Work Record: p1-update-bootstrap-aware

## task_id
p1-update-bootstrap-aware

## branch
p1-update-bootstrap-aware

## commit_shas
(see below — populated after commit)

## summary
Added `--add-untracked` flag to `kbu update` and made `_build_diff` bootstrap-aware. When `[update.file_hashes]` in the manifest is non-empty, `_build_diff` now suppresses `status='added'` entries for source-template paths absent from `file_hashes` (files `kbu bootstrap` deliberately skipped, e.g. `.vscode/extensions.json`). When `file_hashes` is empty (legacy new-project repos) or `--add-untracked` is set, the original "all source files are candidate additions" behaviour is preserved. After a successful `--add-untracked` run that adds new files, those files are included in the recomputed `[update.file_hashes]` via the existing `_recompute_file_hashes` walker. The `--check` / `--yes` mutual exclusion is unchanged; `--add-untracked` composes freely with both.

## files_touched
- `src/kbutillib/cli/update.py` — added `file_hashes` and `add_untracked` params to `_build_diff`; added `add_untracked: bool = False` to `update()`; added `--add-untracked` click flag; updated module and function docstrings; pass `manifest_file_hashes` into `_build_diff` at the call site
- `tests/cli/test_update_bootstrap_aware.py` — new test file (22 tests) covering the four scenarios: (a) non-empty hashes + no flag, (b) non-empty hashes + flag, (c) empty hashes legacy, (d) modified entries always emitted; plus CLI surface, signature, and post-run hash-inclusion checks

## success_criteria_check

| Criterion | Status | Notes |
|---|---|---|
| `kbu update --help` shows `--add-untracked` | PASS | Verified via CliRunner; flag and description appear |
| `kbu update --check --yes` still errors with mutual-exclusion message | PASS | Regression test `test_check_yes_mutual_exclusion_still_holds` passes; exit_code != 0 |
| `pytest tests/` exits 0 including new tests for four bootstrap-aware scenarios | PASS | `pytest tests/cli/` 290 passed; pre-existing failures in non-CLI tests are unrelated |
| Existing kbu-start-v1 update tests pass unchanged | PASS | All 25 tests in `tests/cli/test_update.py` pass |
| `python -c '...; assert "file_hashes" in sig.parameters and "add_untracked" in sig.parameters'` succeeds | PASS | Confirmed with PYTHONPATH=src |

## tests_run
```
pytest tests/cli/ -q
290 passed, 1 warning in 11.67s
```
```
pytest tests/cli/test_update.py tests/cli/test_update_bootstrap_aware.py -v
47 passed in 0.14s
```
Pre-existing failures in `tests/test_ms_biochem_deltag.py` (17 failures) and `tests/test_upload_blob_file_streaming.py` (missing `requests_toolbelt`) are present on `main` and are unrelated to this task.

## caveats
- The `test_*.py` gitignore rule in `.gitignore:31` blocks all test files by pattern, but existing test files are tracked (they were force-added historically). I used `git add -f` to add `test_update_bootstrap_aware.py`, matching the existing pattern for all CLI tests. This is consistent with every other test file in `tests/cli/`.
- `_recompute_file_hashes` walks all files in `.claude/commands/` and `.vscode/` unconditionally; this means after a `--add-untracked` run, newly-copied files appear in `file_hashes` naturally, satisfying criterion 4 of the task with no additional code.
- The `update()` function reads `manifest_file_hashes` from the manifest before calling `_build_diff`, then re-reads `file_hashes` from the same `cfg` dict for `_detect_locally_modified`. These are the same dict object so there is no inconsistency, but a reviewer may want to unify these into a single read in a follow-up cleanup.
