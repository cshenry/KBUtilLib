# Work Record: task-p3-cli-notebook

## task_id
task-p3-cli-notebook

## branch
task-p3-cli-notebook

## commit_shas
- 57a3527c2ca80f1e3e24ec4f7d52d3e0a56e82d8
- 6ce41891f28c7f07d9a5f3b30a9b49c34fe01c2a

## summary
Implemented the `kbu notebook` subcommand group per PRD `kbu-start-v1` (AC #9, #35, #36). Created `src/kbutillib/cli/notebook.py` with `list_notebooks()`, `mark_run()`, and `exec_notebook()` functions backed by `nbclient.NotebookClient` for in-place execution; registered the group in `src/kbutillib/cli/__init__.py`; added `nbclient >=0.7` and `jupyter_client >=7.0` to `pyproject.toml` dependencies; and wrote 26 tests in `tests/cli/test_notebook.py` covering all required scenarios.

## files_touched
- `src/kbutillib/cli/notebook.py` — new file; `list_notebooks()`, `mark_run()`, `exec_notebook()`, Click group + three subcommands
- `src/kbutillib/cli/__init__.py` — import + register `notebook_cmd`
- `pyproject.toml` — add `nbclient >=0.7` and `jupyter_client >=7.0` to `[project].dependencies`
- `tests/cli/test_notebook.py` — new file; 26 tests across TestHelp, TestListNotebooks, TestMarkRun, TestExecNotebook

## success_criteria_check

| Criterion | Status | Notes |
|---|---|---|
| `pytest tests/cli/test_notebook.py -v` passes | PASS | 26/26 tests pass in 9.76s |
| `kbu notebook --help` lists list, mark-run, exec | PASS | Verified via `kbu notebook --help` |
| `kbu notebook list` with one subproject + two notebooks prints 3 lines | PASS | Covered by `test_single_subproject_two_notebooks_three_lines` |
| `kbu notebook exec` on passing notebook writes `.bak.<timestamp>.ipynb` | PASS | Covered by `test_backup_timestamp_format` (regex validates `\d{8}T\d{6}Z`) |
| `kbu notebook exec` updates `last_run_at` in subproject manifest | PASS | Covered by `test_exec_updates_manifest_last_run_at` |
| `KBU_NOTEBOOK_CELL_TIMEOUT=1` causes `time.sleep(5)` to fail with timeout | PASS | Covered by `test_cell_timeout_via_env_var` (raises `CellTimeoutError`) |

## tests_run
```
python -m pytest tests/cli/test_notebook.py -v
26 passed in 9.76s

python -m pytest tests/cli/ -v --ignore=tests/cli/test_notebook.py -q
168 passed in 1.63s
```
All existing CLI tests continue to pass.

## caveats
- The `.gitignore` in this repo contains `test_*.py` which would normally exclude test files. The existing test files were already force-tracked; `test_notebook.py` was similarly force-added with `git add -f` to match that convention. The `.gitignore` entry appears intentional (to keep scratch test files out of commits by default) while production tests are explicitly force-tracked.
- `exec_notebook` uses `nbclient.NotebookClient` with `timeout` as a per-cell timeout (the `timeout` traitlet controls per-cell execution timeout in nbclient). This matches the PRD's "per-cell timeout 600s" requirement.
- The `_select_kernel` function reads `[project].name` from `kbu-project.toml` and checks `find_kernel_specs()`. In test environments without a matching named kernel, it falls back to `python3` with a stderr warning, which is the specified behavior.
- Output truncation (`_truncate_outputs`) is applied post-execution before writing the notebook back; it operates on `stream` text and `execute_result`/`display_data` mime data fields.
