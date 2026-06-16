# Work Record: task-a-venv-provisioning-doctor-v2

## task_id
task-a-venv-provisioning-doctor-v2

## branch
kbu-friction/venv-provisioning-doctor-v2

## commit_shas
- 6c1f06454989cc4f8075983e901c5cd339961c16

## summary

This task fixes two test-quality defects identified by the reviewer on the prior branch (kbu-friction/venv-provisioning-doctor). The production code on that branch was reviewed-good and was not modified.

Defect 1 — `src/kbutillib/cli/manifest.py` and `src/kbutillib/cli/migrate.py` both contained unconditional module-level `import tomli_w` statements. Since `tomli_w` is absent from the canonical `KBUtilLib-py3.13` venv, any import of `kbutillib.cli.init` (and by extension `kbutillib.cli.__init__`) failed with `ModuleNotFoundError`, killing all 6 probe tests before they could run. The fix moves the `import tomli_w` inside the two write functions in `manifest.py` (`write_project_manifest`, `write_subproject_manifest`) and inside `_add_layout_shared_dirs` in `migrate.py`, guarded by a try/except that raises a clear error message only when the write path is actually exercised. Read paths and `kbu doctor` now work cleanly without `tomli_w` installed.

Defect 2 — `test_probe_reports_missing_dep_name` patched `builtins.__import__` to intercept `kbutillib.ms_reconstruction_utils`, but `_probe_fba_imports` iterates its module list in order and tries `ms_fba_utils` first. In the canonical venv, `ms_fba_utils` fails on the absent `cobra` dependency before the patch can intercept `ms_reconstruction_utils`, causing an early return with the wrong dependency name. The fix stubs `kbutillib.ms_fba_utils` as a successful `types.ModuleType` in `sys.modules` via `patch.dict`, so the probe passes it and advances to `ms_reconstruction_utils`. The real import chain for `ms_reconstruction_utils` then hits `kb_ws_utils` → `requests_toolbelt` (absent), yielding `ModuleNotFoundError(name='requests_toolbelt')` as asserted.

A companion fix adds `PYTHONPATH` injection to `_run_import_subprocess` in `TestOptionalImportBanner`, prepending the worktree's `src/` so subprocess tests pick up the task-branch `__init__.py` (with its single-summary-line behavior) rather than the wip-installed version (which emits verbose per-module lines).

## files_touched
- `src/kbutillib/cli/manifest.py` — lazy `tomli_w` import in both write functions
- `src/kbutillib/cli/migrate.py` — lazy `tomli_w` import in `_add_layout_shared_dirs`
- `tests/test_task_a_venv_doctor.py` — fix `test_probe_reports_missing_dep_name` mock strategy; add `PYTHONPATH` injection to `_run_import_subprocess`; add `import types` and `_SRC_ROOT` constant

## success_criteria_check

From the task envelope:

- **Defect 1 fixed: `kbutillib.cli.init` imports cleanly without `tomli_w`** — PASS. Verified with `PYTHONPATH=src python -c "from kbutillib.cli.init import _probe_fba_imports; print('import OK')"` in the canonical venv. The summary line `[KBUtilLib] 15 optional modules unavailable: ...` is emitted (from `__init__.py`), then `import OK`.

- **Defect 2 fixed: `test_probe_reports_missing_dep_name` passes deterministically** — PASS. Test uses `patch.dict(sys.modules, {"kbutillib.ms_fba_utils": stub_fba})` instead of `builtins.__import__` patching, and passes consistently.

- **6 new tests are no longer dead-on-arrival** — PASS. All 6 probe tests now run: 5 PASS, 1 SKIP (`test_probe_passes_when_modules_available` skips because `modelseedpy`/`cobrakbase` are absent — correct behavior).

- **`test_probe_warns_on_missing_tomli_w` and `test_probe_runs_on_current_platform` pass** — PASS. Both were previously broken by the manifest.py import error; now pass.

- **`TestOptionalImportBanner` subprocess tests pass** — PASS. With `PYTHONPATH` injection, subprocesses see the task-branch `__init__.py` and emit the single summary line.

- **`test_composition_smoke.py` not broken** — PASS. Pre-existing failures (`test_is_ref_valid_and_invalid`, `test_facade_argo_deferred`, 3 ERRORs on `requests_toolbelt`/`httpx` absence) are unchanged from the baseline. No new failures introduced.

## tests_run

```
# New tests
PYTHONPATH=src ~/VirtualEnvironments/KBUtilLib-py3.13/bin/python \
  -m pytest tests/test_task_a_venv_doctor.py -v

Result: 17 passed, 2 skipped in 2.05s
  Skipped: test_probe_passes_when_modules_available (modelseedpy/cobrakbase absent)
           test_probe_passes_when_tomli_w_available (tomli_w absent — tests WARN path)

# Composition smoke tests (regression check)
PYTHONPATH=src ~/VirtualEnvironments/KBUtilLib-py3.13/bin/python \
  -m pytest tests/test_composition_smoke.py -v

Result: 5 passed, 14 skipped, 2 failed, 3 errors
  Pre-existing failures (same on base branch):
    FAILED: TestKBWSUtils::test_is_ref_valid_and_invalid (requests_toolbelt absent)
    FAILED: TestCleanRoomConstruction::test_facade_argo_deferred (httpx absent)
    ERROR: TestKBGenomeUtils::test_reverse_complement (requests_toolbelt absent)
    ERROR: TestKBGenomeUtils::test_translate_sequence (requests_toolbelt absent)
    ERROR: TestKBAnnotationUtils::test_translate_term_to_modelseed_known_term (requests_toolbelt absent)
```

## caveats

1. The worktree's `src/` must be on `PYTHONPATH` (or the venv must have the worktree installed) for the canonical venv to pick up these changes during testing. The `conftest.py` already does `sys.path.insert(0, ...)` for in-process tests; the subprocess tests now inject `PYTHONPATH` explicitly.

2. `migrate.py` was not part of the original reviewer failure report, but it had the same unconditional `import tomli_w` pattern that would have manifested as soon as `kbu migrate` was imported in the canonical venv. Fixed as part of the same sweep.

3. The two skipped tests (`test_probe_passes_when_modules_available`, `test_probe_passes_when_tomli_w_available`) require optional dependencies not in the canonical venv. They are correctly guarded with `pytest.importorskip` and will run in a full-deps venv.

4. The `test_probe_fails_on_missing_dep_ms_fba_utils` and `test_probe_handles_other_exceptions` tests still use `builtins.__import__` patching. This is correct: they intercept the probe's own explicit `__import__(mod)` call, not the module's internal import machinery. The distinction matters: patching `builtins.__import__` intercepts the literal `__import__("kbutillib.ms_fba_utils")` call in the probe code, not every `import` statement inside the module being loaded.
