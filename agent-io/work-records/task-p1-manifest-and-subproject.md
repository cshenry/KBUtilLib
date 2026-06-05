# Work Record: task-p1-manifest-and-subproject

## task_id
task-p1-manifest-and-subproject

## branch
task-p1-manifest-and-subproject

## commit_shas
- e9b0f2164aa77851b32ec3b791ee76702115b2fe

## summary

Implemented the shared TOML manifest helper (`src/kbutillib/cli/manifest.py`) and the `kbu subproject` CLI subcommand group (`src/kbutillib/cli/subproject.py`) per PRD `kbu-start-v1`. The manifest module provides `read_project_manifest`, `write_project_manifest`, `read_subproject_manifest`, `write_subproject_manifest`, `now_utc_iso`, `sha256_file`, `append_session_ref`, and `append_notebook_entry_or_update`. The subproject module implements the full 8-state machine (plan → p-review → build → b-review → run → synthesize → s-review → complete), all precondition validators per the round-2 confront table, the verdict HTML comment parser, and the five CLI subcommands (create, list, status, advance, set-status). The `kbu subproject` group was registered in the existing CLI `__init__.py`. `tomli-w` was added to `pyproject.toml` as a dependency (Python 3.11+ stdlib `tomllib` handles reads). All 66 tests pass.

## files_touched

- `src/kbutillib/cli/manifest.py` — new; shared TOML I/O helpers
- `src/kbutillib/cli/subproject.py` — new; state machine + CLI subcommands
- `src/kbutillib/cli/__init__.py` — modified; registered `subproject_cmd`
- `pyproject.toml` — modified; added `tomli-w >=1.0` to dependencies
- `tests/cli/conftest.py` — new; `tmp_kbu_project` and `tmp_subproject` fixtures
- `tests/cli/test_manifest.py` — new; 23 tests covering manifest round-trips, helpers
- `tests/cli/test_subproject.py` — new; 43 tests covering state machine, CLI output, preconditions

## success_criteria_check

- **pytest tests/cli/test_manifest.py tests/cli/test_subproject.py -v passes**: PASS — 66/66 tests pass.
- **`kbu subproject --help` lists create, list, status, advance, set-status**: PASS — verified via `PYTHONPATH=src python -m kbutillib subproject --help`.
- **`kbu subproject create foo` in an empty temp project creates `subprojects/foo/kbu-subproject.toml` with `status = "plan"`**: PASS — covered by `TestSubprojectHelp::test_create_in_empty_temp_project` and `TestCreate::test_creates_manifest_with_plan_status`.
- **`kbu subproject advance foo` with no RESEARCH_PLAN.md exits non-zero and stderr contains `missing-artifact`**: PASS — covered by `TestSubprojectHelp::test_advance_without_research_plan_stderr_missing_artifact`.
- **Manifest schema uses `[subproject].status` and has no `[artifacts.notebooks]` field**: PASS — covered by `TestSubprojectManifestRoundTrip::test_status_field_key_is_status`, `test_status_count_equals_one`, `test_no_artifacts_notebooks_key`.
- **manifest.py exists and exports all 8 required names**: PASS — all 8 names (`read_project_manifest`, `write_project_manifest`, `read_subproject_manifest`, `write_subproject_manifest`, `now_utc_iso`, `sha256_file`, `append_session_ref`, `append_notebook_entry_or_update`) are importable from `kbutillib.cli.manifest`.

## tests_run

```
pytest tests/cli/test_manifest.py tests/cli/test_subproject.py -v
66 passed, 4 warnings in 0.30s
```

Warnings are pre-existing DeprecationWarning about invalid escape sequences in `ms_biochem_utils.py` and `kb_model_utils.py` — unrelated to this task.

## caveats

- `_find_project_root` walks up from `cwd` looking for `kbu-project.toml`. In the tests, commands are invoked with `os.chdir(root)` to simulate being inside a project. This is consistent with how tier-2 skills will call `kbu subproject` from the project root.
- The `advance` command uses `sys.exit(1)` directly (instead of `ctx.exit(1)`) for the precondition failure path. This ensures the Click `CliRunner` captures a non-zero exit code in tests. The rest of the error paths use `ctx.exit(1)` consistently.
- `tomllib` is Python 3.11+ stdlib. The project `requires-python = ">=3.9"` in pyproject.toml, but manifest.py imports `tomllib` unconditionally. This is acceptable because the PRD explicitly states "Use tomllib for read (Python 3.11+)" and the development targets are Python 3.11 machines. If 3.9/3.10 support is needed in future, add a `tomli` fallback conditional.
