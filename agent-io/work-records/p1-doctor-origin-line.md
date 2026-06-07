# Work Record: p1-doctor-origin-line

## task_id
p1-doctor-origin-line

## branch
p1-doctor-origin-line

## commit_shas
(populated after commit)

## summary
Extended `kbu doctor` to print a `project origin:` info line at the end of its output. The new `_probe_project_origin()` function in `src/kbutillib/cli/init.py` reads `kbu-project.toml` from the current working directory via the existing `read_project_manifest` helper. If `[project].bootstrapped` is true it prints `project origin: bootstrap (<bootstrapped_at>)`; if the field is absent or false it prints `project origin: new-project (<created_at>)`; if no manifest exists it prints `project origin: (no kbu-project.toml in cwd)`. The line is informational only and never contributes to the exit code. One existing test that asserted exactly 5 doctor output lines was updated to expect 6 (5 `[STATUS]` probe lines + 1 origin info line).

## files_touched
- `src/kbutillib/cli/init.py` — added `read_project_manifest` import; added `_probe_project_origin()` function; added `click.echo(_probe_project_origin())` call in `doctor_command`
- `tests/cli/test_init.py` — updated `test_doctor_prints_one_line_per_probe` to expect 6 output lines and assert the 6th starts with `project origin:`
- `tests/cli/test_doctor_origin.py` — new test file with 9 tests covering all three origin branches via unit tests (patching `read_project_manifest`) and integration tests (via `kbu doctor` CLI)
- `agent-io/work-records/p1-doctor-origin-line.md` — this file

## success_criteria_check

- **`kbu doctor` in a directory with `[project].bootstrapped = true` produces `project origin: bootstrap (<timestamp>)`**
  PASS — `TestDoctorOriginLine::test_doctor_bootstrapped_origin_in_output` and `TestProbeProjectOriginViaManifestPatch::test_bootstrapped_true` both verify this.

- **Same command with manifest lacking `bootstrapped` (or false) produces `project origin: new-project (<timestamp>)`**
  PASS — `TestDoctorOriginLine::test_doctor_new_project_origin_in_output`, `test_bootstrapped_absent`, and `test_bootstrapped_false` cover both absent and explicitly-false cases.

- **Same command in directory with no `kbu-project.toml` produces `project origin: (no kbu-project.toml in cwd)`**
  PASS — `TestDoctorOriginLine::test_doctor_no_manifest_origin_in_output` and `test_no_manifest_file_not_found` verify this.

- **`pytest tests/` exits 0 with new tests and no regressions**
  PASS — all 277 CLI tests pass (including 9 new origin tests + 34 existing init/doctor tests). The full `tests/` run has a pre-existing collection error in `tests/test_upload_blob_file_streaming.py` due to missing `requests_toolbelt` dependency; this is unrelated to this task and pre-dates it.

## tests_run

```
python -m pytest tests/cli/test_doctor_origin.py -v
# 9 passed in 0.02s

python -m pytest tests/cli/test_init.py -v
# 34 passed in 0.08s

python -m pytest tests/cli/ -v
# 277 passed, 1 warning in 11.46s
```

## caveats
- `tests/test_upload_blob_file_streaming.py` fails to collect due to missing `requests_toolbelt`. This is a pre-existing issue unrelated to this task; running `pytest tests/cli/` (the scope of this PRD slice) is the appropriate target and passes clean.
- The origin line is printed as a plain string (no `[STATUS]` prefix) because the PRD specifies it as informational only — a missing manifest is not an error condition. This is consistent with acceptance criterion 34: "No behavior change beyond this line."
- `_probe_project_origin` is not a probe tuple (`(status, detail)`) but a plain string. This is intentional: adding it to the `probes` list would require giving it a `[STATUS]` classification and wiring it into the `any_fail` logic, which the PRD explicitly says should not happen.
