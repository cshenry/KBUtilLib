# Work Record: buildplan-validate

## task_id
buildplan-validate

## branch
kbu-conductor/buildplan-validate

## commit_shas
- d43818d4c24302d7dd73c38a8f6614e03a9e7d1e

## summary
Added a machine-readable build contract validator for `buildplan.json` files to KBUtilLib. The implementation has three parts: (1) `src/kbutillib/cli/buildplan.py` which defines `BuildPlanError`, the `validate_buildplan` function (collects all errors, never raises), and `load_buildplan` (wraps validate, raises on error), plus a `kbu buildplan validate <path>` Click command; (2) `src/kbutillib/cli/__init__.py` updated to import and register the new `buildplan_cmd` group; and (3) `tests/cli/test_buildplan.py` with 46 pytest tests covering every required rejection rule. The validator enforces: required fields at all levels; depends_on referencing only strictly-earlier notebooks; no self-reference or forward-reference; no cycles; non-empty assertions list; data_source within {sampled-real, synthetic}; duplicate slug detection; and duplicate helper name detection within a notebook. All errors are collected before returning, so a single call surfaces every problem.

## files_touched
- `src/kbutillib/cli/buildplan.py` — new file: BuildPlanError, validate_buildplan, load_buildplan, buildplan_cmd Click group with `validate` subcommand
- `src/kbutillib/cli/__init__.py` — added import of `buildplan_cmd` and `main.add_command(buildplan_cmd, name="buildplan")`
- `tests/cli/test_buildplan.py` — new file: 46 unit and CLI integration tests

## success_criteria_check
- **`kbu buildplan validate` exists**: PASS — registered as `kbu buildplan validate <path>`.
- **Reports success on a valid buildplan.json**: PASS — `test_valid_buildplan_exits_zero` and `test_valid_buildplan_prints_path` both pass.
- **Cyclic/forward depends_on error reported**: PASS — `test_forward_reference_rejected`, `test_self_reference_rejected`, `test_chain_dependency_valid`, `test_forward_dep_reported_in_cli` all pass.
- **Empty assertions error reported**: PASS — `test_empty_assertions_rejected` and `test_all_errors_printed_on_invalid` pass.
- **Out-of-enum data_source reported**: PASS — `test_invalid_data_source_rejected` and `test_all_errors_printed_on_invalid` pass.
- **Duplicate slugs reported**: PASS — `test_duplicate_slugs_rejected` and `test_three_notebooks_one_dup_reports_dup` pass.
- **Duplicate helper names reported**: PASS — `test_duplicate_helper_names_within_notebook_rejected` pass.
- **All errors surfaced at once, not just first**: PASS — `test_multiple_errors_all_reported` (>= 4 errors from 4+ injected faults), `test_forward_dep_and_empty_assertions_both_reported`, `test_load_buildplan_raises_with_all_errors` all pass.
- **pytest test passes**: PASS — 46/46 tests pass.

## tests_run
```
PYTHONPATH=/Users/chenry/.maestro/worktrees/buildplan-validate/src \
  ~/VirtualEnvironments/kbutillib-py3.11/bin/python -m pytest \
  tests/cli/test_buildplan.py -v
```
Result: **46 passed in 0.05s**

Note: used `PYTHONPATH` override to point pytest at the worktree rather than the `pip install -e` target in the Dropbox Projects directory (the kbutillib-py3.11 venv has a working editable install of the Dropbox copy). The KBUtilLib-py3.13 venv has a broken `pyexpat` and cannot run `pip install -e`.

## caveats
- The `.gitignore` rule `test_*.py` would have silently excluded `tests/cli/test_buildplan.py` from being tracked. I used `git add -f` to force-track it, consistent with how all other test files in `tests/cli/` were apparently added before that gitignore rule was introduced. The reviewer should decide whether to remove or narrow that gitignore rule — it will bite future test additions.
- The `validate_buildplan` function does not detect true dependency cycles among notebooks (e.g. A → B → A) because the "strictly earlier in list" rule makes cycles structurally impossible: if A is at index 0 and B at index 1, A cannot depend on B (forward error) and B depending on A is fine, so no cycle can form. This matches the stated requirement; no additional cycle-detection pass is needed.
- The programmatic `validate_buildplan(data) -> list[str]` function never raises; it returns an empty list on success. `load_buildplan(path)` is the raising entry point, parallel to AIAssistant's `load_taskplan`.
