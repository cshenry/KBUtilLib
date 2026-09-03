# Work record: kbutillib-client-submit

- **task_id**: kbutillib-client-submit
- **branch**: maestro/developer/skanidb-kbutillib-client-submit
- **commit_shas** (chronological):
  - e5dfe72e738d4e028afc2502087573b791a07541 (`feat(kbdl): add client-side submit for KBDLBuildSKANIDB jobs`)

## summary

Made the `KBDLBuildSKANIDB` job type reachable from the KBDL service
client in `src/kbutillib/domains/external/kbdl_service_utils.py`. Added
the `JOB_TYPE_BUILD_SKANI_DB = "KBDLBuildSKANIDB"` constant beside the
other `JOB_TYPE_*` constants, and a `submit_build_skani_db(self,
**params: Any) -> str` method that delegates to `self._submit(
JOB_TYPE_BUILD_SKANI_DB, params)`, matching the shape (docstring style,
signature, return contract) of the adjacent `submit_skani` /
`submit_checkm2` methods exactly. Params documented per the task:
`sources` (list of `{"object_id": ...}` refs), `name` (str), and
`visibility` ("public"/"private"). Refreshed the module's job-type-count
prose (module docstring's "seven job types" -> "eight job types" in two
places, plus the `#:` comment above the `JOB_TYPE_*` block) so it no
longer undercounts now that an eighth job type exists. Added a
parametrized test case to the existing
`test_submit_each_job_type_issues_expected_envelope_and_returns_job_id`
table in `tests/external/test_kbdl_service_utils.py`, stubbing the
transport the same way the sibling `submit_skani`/`submit_checkm2`
cases do. Did not add a reference-registration or requeue client
method — both are called out in the module docstring as deliberately
out of scope, and the task explicitly said not to add them.

## files_touched

- `src/kbutillib/domains/external/kbdl_service_utils.py`
- `tests/external/test_kbdl_service_utils.py`

## success_criteria_check

- `JOB_TYPE_BUILD_SKANI_DB == 'KBDLBuildSKANIDB'` defined: **pass** —
  added at line 162 (after `JOB_TYPE_BUILD_GENOME`, before
  `JOB_TYPE_UPLOAD_OBJECT`), value exactly `"KBDLBuildSKANIDB"`.
- `submit_build_skani_db(**params)` delegates to `_submit` with that job
  type, matching the shape of `submit_skani`/`submit_checkm2`: **pass**
  — same `def submit_build_skani_db(self, **params: Any) -> str:`
  signature, same one-line-return-`self._submit(...)` body, same
  docstring style (one-line summary + a "See
  `kbdl_service.schemas.<module>.<Params>`" reference line naming the
  params).
- Job-type-count prose updated: **pass** — the module docstring's
  "for the seven job types" sentence (now "eight job types", also lists
  `KBDLBuildSKANIDB` in the enumerated names), the "submits one of the
  seven job types" sentence under `submit_and_wait`'s bullet (now
  "eight"), and the `#:` comment above the constants block (now "The
  eight job types accepted by...") were all updated. Grepped the whole
  module and repo for other "seven job"/"six job" occurrences after the
  edit — none remained.
- No reference-registration or requeue client method added: **pass** —
  only the constant and `submit_build_skani_db` were added; no new
  method for reference registration or requeue exists anywhere in the
  diff.
- New tests pass and no test that passed on the base commit fails:
  **pass** — see `tests_run` below; the failing/erroring set after the
  change is byte-for-byte the same 7 pre-existing baseline node ids,
  and total passed count went from 2937 (baseline) to 2938 (one new
  parametrize case added, all other counts unchanged).

## tests_run

1. Targeted file first (fast iteration):
   ```
   cd /Users/chenry/.maestro/worktrees/skanidb-kbutillib-client-submit && env -u PYTHONPATH /Users/chenry/VirtualEnvironments/skanidb-client-submit/bin/python -m pytest tests/external/test_kbdl_service_utils.py -q
   ```
   Result: `48 passed in 17.04s`

2. Full suite (mandatory per envelope), hermetic venv built fresh
   against this worktree, `pip install -e .[dev]`:
   ```
   cd /Users/chenry/.maestro/worktrees/skanidb-kbutillib-client-submit && env -u PYTHONPATH /Users/chenry/VirtualEnvironments/skanidb-client-submit/bin/python -m pytest tests/ -q --continue-on-collection-errors
   ```
   Result: `1 failed, 2938 passed, 399 skipped, 266 warnings, 6 errors in 255.89s (0:04:15)`

   The 1 failed + 6 errors are exactly the 7 baseline node ids called
   out in the envelope:
   - `tests/modeling/test_ms_reconstruction_utils.py::test_default_mode_uses_db_fallback` (FAILED)
   - `tests/biochem/test_escher_utils.py` (collection ERROR — cobra not installed)
   - `tests/notebook/helpers` (collection ERROR — cobra not installed)
   - `tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_returns_correct_shape` (ERROR)
   - `tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_solutions_nonempty` (ERROR)
   - `tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_model_grows` (ERROR)
   - `tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_reaction_count_increases` (ERROR)

   Passed count is 2938 (baseline 2937 + 1 new parametrize case from
   the added `submit_build_skani_db` test entry); failed/error/skipped
   counts are unchanged (1/6/399) versus baseline. No regression.

## caveats

- The base commit already carried a `JOB_TYPE_BUILD_GENOME` constant
  and `submit_build_genome` method that the task prompt's enumeration
  of "currently existing" constants didn't mention (it listed
  GENOME_ANNOTATION, MODEL_RECONSTRUCTION, FITNESS_MODEL_ANALYSIS,
  SKANI, CHECKM2, UPLOAD_OBJECT — six, but the file actually had seven
  before this change, including BUILD_GENOME). I treated the file as
  ground truth over the prompt's enumeration and updated the "seven"
  counts to "eight" (six existing constants plus BUILD_GENOME already
  present, plus the new BUILD_SKANI_DB = eight total), which is
  consistent with what the module's own docstring said before my edit
  ("the seven job types").
- No new client method for reference registration or job requeue was
  added, per the explicit instruction not to; the module docstring
  already documents both as deliberately out of scope in its
  "Deliberately NOT implemented" section, which I left untouched.
