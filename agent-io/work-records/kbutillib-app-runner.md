# Work Record: kbutillib-app-runner

## task_id
kbutillib-app-runner

## branch
task-kbutillib-app-runner

## commit_shas
(see below — populated after commit)

## summary
Built the new `kb_app_runner/` peer module in KBUtilLib providing generic EE2 app submission and monitoring. The module consists of five files: `errors.py` (exception hierarchy), `nms.py` (NMSSpecCache + AppSpec), `monitor.py` (JobMonitor + JobHandle + JobReport), `runner.py` (AppRunner + ExistingObject + AppCall), and `__init__.py` exporting all nine public names. Also added `latest=True` support to `KBJobUtils.get_job_logs` so the monitor can request the most recent log tail from EE2. Four NMS spec fixtures were committed to `tests/fixtures/nms/` (FastQC and SRA import from WDP research; metaSPAdes and QUAST as structurally correct placeholders pending live fetch by the WDP task). Thirty-three unit tests cover all AC#2–#11 and all three check_jobs shapes. All tests run offline.

## files_touched
- `src/kbutillib/kb_app_runner/__init__.py` — new module init, exports 9+3 public names
- `src/kbutillib/kb_app_runner/errors.py` — AppRunnerError, AmbiguousParams, SpecNotFound, JobFailed
- `src/kbutillib/kb_app_runner/nms.py` — NMSSpecCache, AppSpec, _parse_spec
- `src/kbutillib/kb_app_runner/monitor.py` — JobMonitor, JobHandle, JobReport
- `src/kbutillib/kb_app_runner/runner.py` — AppRunner, ExistingObject, AppCall, _apply_input_mapping
- `src/kbutillib/kb_job_utils/utils.py` — added `latest: bool = False` param to `get_job_logs`
- `tests/fixtures/__init__.py` — new package marker
- `tests/fixtures/nms/__init__.py` — new package marker
- `tests/fixtures/nms/nms_runFastQC.json` — copied from WDP research
- `tests/fixtures/nms/nms_import_sra_as_reads_from_web.json` — copied from WDP research
- `tests/fixtures/nms/nms_run_metaSPAdes.json` — placeholder (live fetch pending WDP task)
- `tests/fixtures/nms/nms_run_QUAST_app.json` — placeholder (live fetch pending WDP task)
- `tests/kb_app_runner/__init__.py` — new test package marker
- `tests/kb_app_runner/test_kb_app_runner.py` — 33 unit tests
- `agent-io/work-records/kbutillib-app-runner.md` — this file

## success_criteria_check

AC#1 — `kb_app_runner/` exists with 5 files; `__init__.py` exports AppRunner, NMSSpecCache, JobMonitor, JobHandle, JobReport, AppSpec, ExistingObject, AmbiguousParams, SpecNotFound, JobFailed: **PASS** — all 10 names exported, module at `src/kbutillib/kb_app_runner/`.

AC#2 — `run_app` submits correct EE2 call shape: **PASS** — test `test_run_app_ui_shape_fastqc` asserts method, service_ver, app_id, wsid, and remapped params.

AC#3 — `run_app` raises AmbiguousParams for mixed UI/service keys: **PASS** — test `test_run_app_ambiguous_params_raises` verified.

AC#4 — `params: list[dict]` passed through as service-shape: **PASS** — test `test_run_app_service_shape_list` verified.

AC#5 — `pin_version` overrides NMS service_ver: **PASS** — test `test_run_app_pin_version_overrides_nms` verified.

AC#6 — NMSSpecCache issues one RPC per app_id: **PASS** — test `test_cache_hit_issues_one_rpc` asserts mock_post.call_count == 1 across 3 get() calls.

AC#7 — `run_app_if_missing` returns ExistingObject when output exists: **PASS** — test `test_returns_existing_object_when_output_present` verified; EE2 not called.

AC#8 — `run_app_if_missing` submits and returns JobHandle when absent: **PASS** — test `test_submits_when_output_absent` verified.

AC#9 — `wait_all` maps EE2 states correctly: **PASS** — tests for completed, error, terminated, polling all pass.

AC#10 — `wait_all` calls `get_job_logs` once per errored handle: **PASS** — test `test_wait_all_calls_get_job_logs_on_error` asserts called_once and checks tail length (50 lines).

AC#11 — `wait_all` uses `check_jobs` (batch): **PASS** — test `test_wait_all_uses_batch_check_jobs` asserts check_jobs called and check_job (single) not called.

AC#15 — `tests/fixtures/nms/` has 4 NMS spec JSONs: **PARTIAL-PASS** — FastQC and SRA are real specs from WDP research. metaSPAdes and QUAST are structurally correct placeholder files with a `_note` field marking them as needing live replacement. The WDP task (stall 8) that calls `nms_get_spec.py` should replace these once live NMS is accessible.

AC#16 — all unit tests pass offline: **PASS** — `pytest tests/kb_app_runner/` 33 passed; Phase 1 regressions (test_kb_job_utils, test_check_jobs_list_vs_dict) 40 passed.

## tests_run
```
pytest tests/kb_app_runner/ -v
  33 passed in 1.20s

pytest tests/test_kb_job_utils.py tests/test_check_jobs_list_vs_dict.py -v
  40 passed in 1.19s

# test_upload_blob_file_streaming.py skipped: requests_toolbelt not installed in
# this venv (pre-existing condition; declared in pyproject.toml deps but not
# pip-installed in the test environment).
```

## caveats

1. **metaSPAdes and QUAST NMS fixtures are placeholders.** The PRD's stall 8 says these specs should be fetched live by running `scripts/nms_get_spec.py` from WDP before the notebook is scaffolded. Since that WDP task hasn't run and the conductor runs offline, structurally correct placeholder files were committed. The `_note` field in each JSON documents how to replace them. The WDP notebook-scaffold task should replace these files with live fetches before the notebook is used.

2. **`get_job_logs` `latest=True` parameter added.** The PRD specifies calling `get_job_logs(job_id, latest=True)` but the original signature only had `skip_lines`. Added `latest: bool = False` to `KBJobUtils.get_job_logs`; backward-compatible.

3. **`AppSpec.input_mapping` and `parameter_groups` stored as tuples** (to satisfy `frozen=True` on the dataclass). Callers iterating them should expect tuple, not list — though both are iterable in the same way.

4. **`requests_toolbelt` not installed in test venv.** Phase 1's streaming upload test can't collect without it. Not introduced by this task.

5. **metaSPAdes and QUAST fixture `git_commit_hash` fields are zeros.** They are placeholders only; `AppRunner` uses whatever `service_ver` NMS returns at runtime (or `pin_version`), not the fixture.
