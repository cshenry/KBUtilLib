# Work record: client-catchup

## task_id
client-catchup

## branch
maestro/developer/bg-client-catchup

## commit_shas
1. `0fc7d55953857571f5deef8fe6c1804c8548964e` — feat(kbdl): add BuildGenome job type, narrow genome-annotation contract

Base commit: `a7699a16b31667eb5ba02d9a769ba8fdb31506d9`.

## summary
Brought `KBDLServiceUtils` (`src/kbutillib/domains/external/kbdl_service_utils.py`) up to the current KBDL service contract's two remaining gaps: added the seventh job type `KBDLBuildGenome` via `JOB_TYPE_BUILD_GENOME` + `submit_build_genome()`, and narrowed `submit_genome_annotation()` to the service's updated `genome`/`genome_ref` contract. `submit_build_genome` validates client-side, before any request is sent, that exactly one of `fasta`/`genbank`/`archive` is given (with `genbank`+`fasta` as the one documented legal pairing), that `gff` only accompanies `fasta`, and that `skani_db` is present — all via `ValueError`, matching the module's existing `ValueError`-for-client-side-validation convention (seen elsewhere in `kb_uniprot_utils.py`, `rast_utils.py`, etc.). `submit_genome_annotation` was rewritten from a `**params` catch-all to an explicit signature (`tools`, `genome`, `genome_ref`, `tax_id`) with no catch-all remainder, so the removed `fasta`/`gff`/`genbank`/`features`/`kbase_genome_id` parameters now raise `TypeError` if passed rather than being silently forwarded to the server. Refreshed the module docstring's job-type counts and list (six → seven, `KBDLBuildGenome` added) and added a short contract note explaining the genome-annotation narrowing. Corresponding test coverage was added/updated in `tests/external/test_kbdl_service_utils.py`.

## files_touched
- `src/kbutillib/domains/external/kbdl_service_utils.py`
- `tests/external/test_kbdl_service_utils.py`
- `agent-io/work-records/client-catchup.md` (this file)

## ALREADY-PRESENT vs ADDED-BY-THIS-TASK

The sibling PRD `kbdl-checkm2-job-type-v1` had already landed its own catch-up on this exact file (commit `616f422`, `docs`-recorded in `agent-io/work-records/checkm2-kbdl-client-update.md`), and that work was present at this task's base commit (`a7699a1`, the tip of `main`). Read the file first; nothing from that landed work was duplicated or reverted.

**Already present at base (untouched by this task):**
- `JOB_TYPE_CHECKM2 = "KBDLCheckM2"` and `submit_checkm2(**params)`.
- `upload_object`'s `transform` parameter (defaulting to `"none"`, included in form data only when non-default) and its caller-uploadable-vs-operator-registered object-type docs (including `CheckM2DB`).
- The module docstring's pin at KBDLJobRunningPrototype commit `a024b55` (already refreshed off the stale `e0b2dda` pin cited in this task's prompt — that specific instruction was a no-op here since the checkm2 catch-up had already fixed it) and its "five job types" → "six job types" refresh.
- The `kbdl-atp-safe-at-scale-v1` contract note about `KBDLFitnessModelAnalysis`/`KBDLModelReconstruction` result-payload fields.
- All lifecycle methods (`list_jobs`, `check_job`, `get_job_result`, `clear_job`, `poll_until_terminal`, `submit_and_wait`), the object-store methods, and all typed error classes — unchanged, not part of this task's scope.
- The port fix (`8790` → `8791`) in the test file, from the checkm2 task's separate commit `eea2930`.

**Added by this task:**
- `JOB_TYPE_BUILD_GENOME = "KBDLBuildGenome"` constant.
- `submit_build_genome(skani_db, fasta=None, genbank=None, archive=None, gff=None, **params)` with client-side validation (see summary) and a docstring following the module's existing per-job-type doc pattern.
- Rewrote `submit_genome_annotation` from `(self, **params)` to `(self, tools, genome=None, genome_ref=None, tax_id=None)`, with client-side validation that exactly one of `genome`/`genome_ref` is given, and removal of the `**params` catch-all so legacy field names are a hard `TypeError`.
- Module docstring: "six job types" → "seven job types" (two occurrences: the envelope-shape paragraph and the `submit_and_wait` bullet), "The six job types accepted by `POST /jobs`" comment → "seven", `JOB_TYPE_BUILD_GENOME` added to that comment block, and a new contract-note paragraph describing the genome-annotation narrowing.
- Test file: two new parametrize cases for `submit_build_genome` in the shared "one test per job type" table (plain `fasta` source, and the `genbank`+`fasta` pairing); updated the `submit_genome_annotation` parametrize case from the old `fasta`/`tools` shape to `genome_ref`/`tools`; added `test_submit_build_genome_rejects_invalid_source_combination_without_a_request` (5 parametrized invalid combos), `test_submit_build_genome_rejects_missing_skani_db_without_a_request`, `test_submit_genome_annotation_rejects_invalid_genome_source_without_a_request` (2 parametrized cases: neither given, both given), and `test_submit_genome_annotation_no_longer_accepts_legacy_parameters` (asserts both that the legacy names aren't in the signature and that passing any of them raises `TypeError` with zero HTTP calls made).

## success_criteria_check
- **`submit_build_genome` exists and constructs a well-formed `KBDLBuildGenome` envelope, rejecting zero or two genome sources and a missing `skani_db` before any request is sent** — pass. Method builds `{"schema_version": "1", "job_type": "KBDLBuildGenome", "params": {...}}` via the existing `_submit` helper (same envelope construction as every other `submit_*` method), and all validation (`skani_db is None`, invalid source-set membership, `gff` without `fasta`) raises `ValueError` before `_submit`/`self._request` is ever called. Verified by `test_submit_build_genome_rejects_invalid_source_combination_without_a_request` and `test_submit_build_genome_rejects_missing_skani_db_without_a_request`, both of which use a `FakeSession([])` (zero canned responses) and assert `session.calls == []` after the `ValueError`, i.e. no HTTP call was attempted.
- **`submit_genome_annotation` accepts `genome` or `genome_ref` and no longer exposes `fasta`/`gff`/`genbank`/`features`/`kbase_genome_id` parameters** — pass. Signature is now `(self, tools, genome=None, genome_ref=None, tax_id=None)` with no `**params` remainder, so those five names are not accepted at all — passing any raises `TypeError` (verified by `test_submit_genome_annotation_no_longer_accepts_legacy_parameters`, which checks both the signature and the actual `TypeError` at call time, with zero HTTP calls made in the process).
- **Any catch-up work already present from the CheckM2 PRD is left intact rather than duplicated or reverted, and the work record states what was already present versus what this task added** — pass. Verified by reading the file before editing (see the ALREADY-PRESENT vs ADDED-BY-THIS-TASK section above); `git diff` confirms no touch to `JOB_TYPE_CHECKM2`, `submit_checkm2`, or the `upload_object` transform work. `submit_checkm2`'s existing test entry in the shared parametrize table is unmodified.
- **No test that passed on the base commit fails** — pass. Full-suite run (below) shows the same 1 failed / 6 errors as the documented base, all at the same node ids, with 11 additional tests now passing (2911 → 2922) that this task added; nothing that passed at base now fails.

## tests_run
All runs used `~/VirtualEnvironments/bg-clientcatchup-envA/bin/python` (dedicated venv created for this task, editable-installed against this worktree with the `dev` extra for `pandas`/`ipykernel`/etc.), invoked by absolute interpreter path — no PATH-resolved `pytest`, no `PYTHONPATH`.

1. `python -m pytest tests/external/test_kbdl_service_utils.py -q` — **47 passed** (36 pre-existing + 11 new: 2 `submit_build_genome` parametrize cases, 5 invalid-source-combination cases, 1 missing-`skani_db` case, 2 invalid-genome-source cases, 1 legacy-parameter-rejection case).
2. `python -m pytest tests/ -q --continue-on-collection-errors` (exact documented baseline command) — **1 failed, 2922 passed, 399 skipped, 6 errors** in 253.46s (well inside the 600s Bash timeout budget, and close to the documented 248s baseline duration).
   - Base at `a7699a1` (per conductor-measured baseline): **1 failed / 2911 passed / 399 skipped / 6 errors**.
   - Failed/errored node ids on branch, verified identical set and count to the documented baseline list:
     - `tests/modeling/test_ms_reconstruction_utils.py::test_default_mode_uses_db_fallback` (failed)
     - `tests/biochem/test_escher_utils.py` (collection error)
     - `tests/notebook/helpers` (collection error, `ModuleNotFoundError: No module named 'cobra'`)
     - `tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_returns_correct_shape` (error)
     - `tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_solutions_nonempty` (error)
     - `tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_model_grows` (error)
     - `tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_reaction_count_increases` (error)
   - Passed count rose from 2911 to 2922: +11, exactly the 11 new tests this task added (see item 1 above) — no other test's pass/fail status moved. Skipped (399) and error count (6) unchanged.

## caveats
- My initial full-suite attempt (before installing the `dev` extra) showed 85 unrelated failures across `biochem`/`notebook`/`interfaces`/`core` tests that don't touch this file at all — that was an environment-setup gap in my own venv (missing `pandas`/etc. from `pip install .[dev]`, which the baseline venv had), not a code regression. Documented here for transparency; resolved before drawing any conclusions, and the final reported numbers above are from the corrected venv.
- `submit_build_genome`'s combination-validation treats `{"genbank", "fasta"}` as the sole legal two-source pairing per the task's literal wording ("an optional fasta alongside genbank"); `archive`+anything and `fasta`+`archive`+`genbank` (three-way) are all rejected as "two genome sources" violations. `gff` is accepted whenever `fasta` is present, including in the `genbank`+`fasta` pairing, since the task's rule ("gff legal ONLY alongside fasta") conditions only on `fasta`'s presence, not `genbank`'s absence — this is a judgment call since the task text doesn't explicitly address that specific triple combination, and no test exercises it either way.
- Did not touch `submit_and_wait`, `poll_until_terminal`, or any object-store method — out of scope for this task, and their existing tests are unmodified and still pass.
- Did not update the `a024b55` service-repo commit pin in the module docstring's endpoint list (line documenting `POST /jobs` etc.), since that pin describes the HTTP endpoint list unaffected by this task's changes (which only touch per-job-type `params` shapes) and the task's stale-pin instruction (which cited `e0b2dda`) was already satisfied by the prior checkm2 catch-up.
