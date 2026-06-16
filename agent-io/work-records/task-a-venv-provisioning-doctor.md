# Work Record: Task A — Venv Provisioning + Diagnostics

## task_id
kbu-friction/venv-provisioning-doctor (no Maestro task_id; issued interactively)

## branch
kbu-friction/venv-provisioning-doctor

## commit_shas
- eed285a  feat(task-a): venv provisioning + diagnostics (kbu-friction PRD)

## summary

Added `requests_toolbelt >=0.10.0` and `tomli-w >=1.0` to `machine_configs/_default.yaml` `notebook_deps` so freshly provisioned notebook venvs carry the full KBUtilLib dependency closure (belt-and-suspenders beside the editable install). Cobra/modelseedpy/cobrakbase were intentionally left out (they arrive via editable_installs). Extended `cli/init.py` with two new doctor probes — `_probe_fba_imports()` wraps import of `ms_fba_utils` and `ms_reconstruction_utils` with the exact error-message contract from the PRD, and `_probe_tomli_w()` issues a WARN when tomli_w is absent from the current interpreter; both probes are platform-agnostic (no macOS gate). Collapsed the ~20 per-module optional-import failure lines in `src/kbutillib/__init__.py` to a single stderr summary by default (`[KBUtilLib] N optional modules unavailable: ... (set KBUTILLIB_VERBOSE_IMPORTS=1 for detail)`), with per-module detail restored under `KBUTILLIB_VERBOSE_IMPORTS=1`. Added a 19-test suite committed with `git add -f` (the repo `.gitignore` ignores `test_*.py`).

## files_touched
- `machine_configs/_default.yaml` — added `requests_toolbelt >=0.10.0` and `tomli-w >=1.0` to `notebook_deps`
- `src/kbutillib/__init__.py` — redesigned `_import_error()` to collect into `_OPTIONAL_IMPORT_FAILURES`, added `_flush_import_errors()`, called at end of optional-import block; added `import os` at top
- `src/kbutillib/cli/init.py` — added `_probe_fba_imports()` and `_probe_tomli_w()` probe functions; wired both into `doctor_command` probe list
- `tests/test_task_a_venv_doctor.py` — new test file (git add -f)

## success_criteria_check

1. `machine_configs/_default.yaml` `notebook_deps` includes `requests_toolbelt >=0.10.0` and `tomli-w >=1.0`; cobra/modelseedpy/cobrakbase are NOT added.
   **PASS** — both entries present with version specifiers; none of the forbidden packages appear. Verified by `TestDefaultYamlNotebookDeps` (7 tests).

2. `kbu doctor` wraps FBA module imports; prints `[FAIL] fba-import: missing dependency: {e.name}` on ModuleNotFoundError; prints exception type + first line for other errors; remains runnable when optional modules absent.
   **PASS** — `_probe_fba_imports()` implements exact message format. Verified by `TestProbeFbaImports` (4 tests passing). Doctor command does not gate on optional-module presence.

3. `import kbutillib` emits at most one summary line by default; per-module detail under `KBUTILLIB_VERBOSE_IMPORTS=1`.
   **PASS** — subprocess tests confirm at most 1 `[KBUtilLib]` line in default mode; verbose mode emits `Failed to import` per-module lines. Verified by `TestOptionalImportBanner` (4 tests).

14. `kbu doctor` attempts `import tomli_w`; warns on failure that CLI venv needs reconciliation.
    **PASS** — `_probe_tomli_w()` returns WARN with reconciliation message. Verified by `TestProbeTomliW` (3 tests).

17. FBA-import and tomli_w checks run on Linux (not macOS-gated).
    **PASS** — both probe functions contain no `sys.platform`, `_is_darwin()`, or `_is_macos_or_override()` guards. Statically verified by `test_probe_runs_on_current_platform` in both test classes.

## tests_run
- `pytest tests/test_composition_smoke.py -v` — 9 passed, 15 skipped (skips are expected: modelseedpy/cobrakbase/cobra not installed in this venv). **PASS**
- `pytest tests/test_task_a_venv_doctor.py -v` — 18 passed, 1 skipped. The skip is `test_probe_passes_when_modules_available` which calls `pytest.importorskip("modelseedpy")` — expected since modelseedpy is not in this venv. **PASS**

## caveats
- The WARN status for the tomli_w probe is a new status value not previously used by `doctor_command`. It does NOT cause `any_fail = True`, so `kbu doctor` exits 0 when tomli_w is the only issue. This is intentional (it's a warning, not a hard failure) but the reviewer should confirm this is the desired UX.
- The `_flush_import_errors()` call uses `_OPTIONAL_IMPORT_FAILURES.clear()` after emitting the summary; if tests call `importlib.reload(kbutillib)` the list starts fresh each reload.
- Task A only (criteria 1, 2, 3, 14, 17). Tasks B, C, D, E (unified util.py template, skill-doc corrections, reference exemplar, KB_AUTH_TOKEN injection) are not in scope for this branch.
