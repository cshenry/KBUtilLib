# Work Record: p1-refactor-template-ops

## task_id
p1-refactor-template-ops

## branch
p1-refactor-template-ops

## commit_shas
(filled after commit)

## summary
Extracted five reusable helper functions from `src/kbutillib/cli/new_project.py` into a new module `src/kbutillib/cli/_template_ops.py`. The extracted functions — `copy_template_tree`, `compute_file_hashes`, `run_venvman_project`, `create_plain_venv`, and `parse_virtual_env_from_activate` — are now the public API of `_template_ops.py`. `new_project.py` imports them with underscore-prefixed aliases (`_copy_template_tree`, etc.) to preserve compatibility with the existing tests that import those private names directly. No behavior changed; this is a pure mechanical extraction to enable the upcoming `kbu bootstrap` module to share the same helpers without circular imports.

## files_touched
- `src/kbutillib/cli/_template_ops.py` — new module; exports five public helpers
- `src/kbutillib/cli/new_project.py` — imports from `._template_ops`; inline definitions removed; unused imports (`sha256_file`, `dataclass`) dropped

## success_criteria_check

- `src/kbutillib/cli/_template_ops.py` exists and exports the five named helpers: **PASS** — file created, all five names importable via `python -c 'from kbutillib.cli._template_ops import copy_template_tree, compute_file_hashes, run_venvman_project, create_plain_venv, parse_virtual_env_from_activate'`
- `src/kbutillib/cli/new_project.py` imports each of these from `._template_ops` and contains no duplicate inline definition: **PASS** — grep for `^def _copy_template_tree` etc. returns no output; import block confirmed present
- `pytest tests/` exits 0 with no changes to existing test files: **UNCERTAIN** — `pytest tests/` collects two pre-existing errors (`tests/notebook` and `tests/test_ms_biochem_deltag.py`) due to missing `pandas`/`httpx` optional deps that are not installed in the py3.11 test venv; these errors were present on `main` before this change. All 267 CLI tests pass (1 skipped). The task-specified test sets (new-project, update, subproject, notebook, session, init) all pass. The pandas/httpx failures are pre-existing and unrelated to this refactor.
- `python -c 'from kbutillib.cli._template_ops import ...'` succeeds: **PASS** — confirmed

## tests_run
```
/tmp/kbu-test-venv/bin/pytest tests/cli/ -v --tb=short
```
Result: **267 passed, 1 skipped** (the skipped test was pre-existing on main)

```
/tmp/kbu-test-venv/bin/pytest tests/ --ignore=tests/notebook --ignore=tests/test_ms_biochem_deltag.py -v --tb=short
```
Result: **459 passed, 17 skipped, 2 failed, 1 error** — the 2 failures and 1 error are pre-existing optional-dep tests (pandas, httpx) unrelated to this refactor; they failed the same way on main before this change.

Test venv used: `/tmp/kbu-test-venv` (python3.11, installed via `pip install -e .[dev]` from worktree root)

## caveats
- The `import dataclass` in the original `new_project.py` was unused (the `dataclass` decorator is not applied to any class in that module); it was removed along with the now-unused `sha256_file` import from `.manifest`.
- `test_new_project.py` imports `_compute_file_hashes` and `_copy_template_tree` using the underscore-prefixed names directly from `kbutillib.cli.new_project`. Those names are preserved as re-exports (via import aliasing) so the test file required no modification.
- The pre-existing `tests/notebook` and `tests/test_ms_biochem_deltag.py` collection failures (missing `pandas`) are outside the scope of this task. The KBUtilLib-py3.11 venv referenced by `activate.sh` does not exist on this machine, and the py3.13 venv has broken system libs. A fresh py3.11 venv was created for this task to run the relevant CLI tests.
