# Work record: kbutillib-store-load-client

- **task_id**: kbutillib-store-load-client
- **branch**: maestro/developer/storeload-kbutillib-client
- **base_sha**: 5c0943e (KBUtilLib `main`)
- **commit_shas** (chronological):
  - 6b83fe2 (`feat(kbdl-service): add submit_store_load and KBDLNotAuthorizedError`)

## summary

Extended `KBDLServiceUtils` in `src/kbutillib/domains/external/kbdl_service_utils.py`
with the store-load capability described by the PRD. Added
`submit_store_load(source_job_ids)`, which builds the
`{"schema_version": "1", "job_type": "KBDLStoreLoad", "params":
{"source_job_ids": [...]}}` envelope and posts it via the module's
existing `_submit` helper (same pattern as `submit_genome_annotation`,
`submit_build_genome`, `submit_model_reconstruction`,
`submit_fitness_model_analysis`, `submit_skani`, `submit_checkm2`) --
placed immediately after `submit_checkm2` and before
`submit_build_skani_db`. Because `submit_and_wait`/`poll_until_terminal`
are already generic over `job_type`/`job_id`, no changes were needed
there for the new job type to flow through them; a test demonstrates
this explicitly (`test_submit_and_wait_works_with_store_load_job_type`).

Added `KBDLNotAuthorizedError(KBDLServiceError)`, raised by
`_raise_for_status` on HTTP 403, and wired it in immediately after the
401 branch (before the 404 branch), mirroring the existing 400/401/503
branch style. Kept it deliberately distinct from the existing
`KBDLAuthenticationError` (401): 401 means the token itself was
rejected, 403 means the token is valid but the caller lacks permission
-- the module's own docstring already documents this exact
conflation hazard for the 401/503 pair, so the same reasoning is now
recorded for 401/403 in both the class docstring and the module's
"Errors" section.

Updated the module docstring: the job-submission envelope paragraph now
says "nine job types" and lists `KBDLStoreLoad`, the `JOB_TYPE_*`
constants comment now says "nine", a new `JOB_TYPE_STORE_LOAD =
"KBDLStoreLoad"` constant was added, and the "Errors" list gained a 403
entry. The endpoint list (`POST /jobs` etc.) was already generic and
needed no change since `KBDLStoreLoad` submits through the same `POST
/jobs` route as every other job type. The "Deliberately NOT
implemented" section was reviewed and needed no change -- it covers
ACL/grant and requeue calls, neither of which store-load touches.
`KBDLNotAuthorizedError` was added to `__all__` next to
`KBDLAuthenticationError`.

No import of `kbdl_service` was added anywhere (verified both by the
module's own static-AST test and by grep -- see Tests below); the
request/response shapes for the new job type are encoded directly in
this module per the wire-contract-only convention already documented
at the top of the file.

Added tests to `tests/external/test_kbdl_service_utils.py` in the
existing style (mocked `FakeSession`/`FakeResponse` transport, no real
network calls):

- Added a `("submit_store_load", "KBDLStoreLoad", {"source_job_ids":
  [...]})` case to the existing
  `test_submit_each_job_type_issues_expected_envelope_and_returns_job_id`
  parametrization, so the same generic assertion (`POST /jobs` with the
  exact envelope, returns `job_id`) that already covers the other six
  submit methods now also covers `submit_store_load`.
- `test_401_and_403_are_distinguishable_typed_errors`: HTTP 401 raises
  `KBDLAuthenticationError`, HTTP 403 raises `KBDLNotAuthorizedError`,
  and asserts the two classes are not equal and neither is a subclass
  of the other (mirroring the existing `test_401_and_503_...` test's
  structure), plus asserts `KBDLNotAuthorizedError` subclasses
  `KBDLServiceError`.
- `test_submit_and_wait_works_with_store_load_job_type`: exercises
  `submit_and_wait("KBDLStoreLoad", {"source_job_ids": [...]})` end to
  end against the fake transport, asserting both the posted envelope
  and the returned result.

## files_touched

- `src/kbutillib/domains/external/kbdl_service_utils.py` (module docstring, `__all__`, `JOB_TYPE_STORE_LOAD`, `KBDLNotAuthorizedError`, `_raise_for_status` 403 branch, `submit_store_load` method)
- `tests/external/test_kbdl_service_utils.py` (import of `KBDLNotAuthorizedError`, new parametrize case, two new test functions)

## success_criteria_check

- "KBDLServiceUtils exposes submit_store_load which posts the envelope
  ... to POST /jobs and returns the job_id, demonstrated against a
  mocked transport" -- **pass**. Covered by the added parametrize case
  in `test_submit_each_job_type_issues_expected_envelope_and_returns_job_id`
  and by `test_submit_and_wait_works_with_store_load_job_type`.
- "a KBDLNotAuthorizedError subclassing KBDLServiceError is raised on
  HTTP 403 and is a different class from the KBDLAuthenticationError
  still raised on HTTP 401, both demonstrated by tests" -- **pass**.
  Covered by `test_401_and_403_are_distinguishable_typed_errors`.
- "the module imports kbdl_service nowhere at any scope" -- **pass**.
  The module adds no `kbdl_service` import; the pre-existing static-AST
  test (`test_module_source_never_imports_kbdl_service`) and dynamic
  `sys.modules` check (`test_importing_module_does_not_import_kbdl_service`)
  both still pass against the edited file, and `grep -n kbdl_service`
  on the module shows only the pre-existing docstring prose references,
  no `import` statements.
- "no test that passed at the base commit fails on the branch" --
  **pass**, by same-environment A/B comparison (see Tests below): the
  one pre-existing failure and six pre-existing collection errors
  (all in `modeling/`, `biochem/`, `notebook/` -- solver/`cobra`-
  dependent, orthogonal to this module) are identical on both base and
  branch; passed count increased by exactly 3 (the tests this task
  added), skipped count is identical.

## tests_run

All runs used a fresh venv (`pip install -e ".[dev]"`), pinned
interpreter, `PYTHONPATH` stripped, per the supplied baseline recipe.

- `env -u PYTHONPATH <venv>/bin/python -m pytest tests/external/test_kbdl_service_utils.py -q`
  -- fast-iteration run against the branch: **51 passed** (48
  pre-existing + 3 new).
- `env -u PYTHONPATH <venv-branch>/bin/python -m pytest tests/ -q --continue-on-collection-errors`
  (branch, worktree `storeload-kbutillib-client`) --
  **2966 passed, 1 failed, 399 skipped, 6 errors** in ~249s.
- `env -u PYTHONPATH <venv-baseline>/bin/python -m pytest tests/ -q --continue-on-collection-errors`
  (unmodified base commit 5c0943e, separate worktree
  `storeload-baseline-KBUtilLib`, separate fresh venv) --
  **2963 passed, 1 failed, 399 skipped, 6 errors** in ~434s, for an
  apples-to-apples same-environment comparison.
- Delta: +3 passed (exactly this task's new tests), identical failed/
  errors/skipped between base and branch. The single failure
  (`tests/modeling/test_ms_reconstruction_utils.py::test_default_mode_uses_db_fallback`)
  and all six collection errors (`biochem/test_escher_utils.py`,
  `notebook/helpers` missing `cobra`, four
  `modeling/test_comprehensive_gapfill_wrapper.py` cases) are identical
  on both sides and match the task envelope's description of
  pre-existing, orthogonal, solver/`cobra`-dependent failures.
  Note: my locally-measured base numbers (2963 passed / 399 skipped)
  differ from the conductor-captured baseline figures in the task
  envelope (2983 passed / 380 skipped) -- likely a dependency-version
  or platform difference between the venv the conductor captured
  against and the one built here -- but since I re-measured the base
  commit myself in the same venv/interpreter/flags used for the branch,
  the regression judgment (zero regressions, +3 new passing tests) is
  reliable regardless of that absolute-count discrepancy.

## caveats

- The task's numbered list mentioned "six existing submit_* methods"
  (excluding `submit_build_skani_db`); `submit_store_load` was placed
  right after `submit_checkm2` and before `submit_build_skani_db` to
  sit beside that named group without disturbing the existing method
  order.
- `submit_store_load`'s docstring cites
  `kbdl_service.schemas.store_load.KBDLStoreLoadParams` as the params
  reference, following the exact citation convention every other
  `submit_*` docstring in this module already uses (e.g.
  `kbdl_service.schemas.skani.KBDLSKANIParams`) -- this is prose only,
  not an import; the module still imports `kbdl_service` nowhere.
- Did not touch the "Deliberately NOT implemented" docstring section;
  reviewed it and confirmed store-load isn't referenced there and
  doesn't need to be.
- Two scratch venvs were built under the session scratchpad
  (`/private/tmp/.../scratchpad/venv-storeload`,
  `.../venv-baseline`) purely for local verification; nothing under
  them was committed.
