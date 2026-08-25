# Work record: kbutillib-bakta-outdir-fix

## task_id
kbutillib-bakta-outdir-fix

## branch
maestro/developer/kbutillib-bakta-outdir-fix

## commit_shas
- `099f30c25a1ffe2d90c7fe4ef6f67ea279505261` — `fix(bakta): stop pre-creating the bakta_proteins --output directory`

(Single commit on this branch, base `main` at `ffe8d5bc653e34bfd2d0ff1ba2abf6b912ca8bd3`.)

## summary
`BaktaUtils._run_bakta` (`src/kbutillib/domains/genome/annotation/bakta_utils.py`) called
`outdir.mkdir(parents=True, exist_ok=True)` before invoking `bakta_proteins --output <outdir>`.
`bakta_proteins` refuses an `--output` directory that already exists, so every real Bakta run
failed — a defect invisible to the existing unit suite because it mocks `subprocess.run` and
never observes filesystem state at invocation time. This was caught on poplar by the
real-dependency guard in KBDL's `tests/test_annotator_guards_local.py`
(`KBDLJobRunningPrototype`, commit `487c746`). The fix creates only `outdir.parent` (already
satisfied by the caller's `tempfile.TemporaryDirectory` in `annotate()`, which creates `tmp`
before `outdir = tmp / "bakta_out"` is ever referenced) and leaves `outdir` itself for
`bakta_proteins` to create. The Docker-mode shared-parent invariant
(`outdir.parent.resolve() != work` check) is unchanged and still holds, since it only inspects
`outdir.parent`, never `outdir` itself, so it is unaffected by no longer pre-creating `outdir`.
This is the third defect of this same shape in this codebase (CLI_PROKKA nucleotide/protein
mismatch, DRAM2 gene-id rejection, now this) — a wrapper feeding a tool an input it structurally
cannot accept, hidden by fully-mocked tests.

## files_touched
- `src/kbutillib/domains/genome/annotation/bakta_utils.py` — `_run_bakta`: replaced
  `outdir.mkdir(parents=True, exist_ok=True)` with `outdir.parent.mkdir(parents=True,
  exist_ok=True)`, plus docstring/comment updates explaining why.
- `tests/annotators/test_bakta_utils.py` — added
  `TestRunBaktaCommandShape.test_native_outdir_not_pre_created_only_parent_exists` and
  `TestRunBaktaCommandShape.test_docker_outdir_not_pre_created_only_parent_exists`.

## success_criteria_check
- **`_run_bakta` no longer creates the `--output` directory it passes to `bakta_proteins`;
  only the parent is created.** PASS — `outdir.mkdir(...)` replaced with
  `outdir.parent.mkdir(...)`; verified by the two new tests observing `outdir.exists() is
  False` at the moment `subprocess.run` is invoked, for both code paths.
- **The Docker-mode shared-parent invariant still holds and both the Docker and native command
  paths are unchanged apart from that.** PASS — the `outdir.parent.resolve() != work` check and
  both `cmd` construction blocks are byte-for-byte unchanged; only the line above them changed.
  Confirmed via `git diff` (see commit) — no other lines in either branch were touched.
- **A test proves `outdir` does not exist at subprocess-invocation time while `outdir.parent`
  does, for both paths, and that test is demonstrated to fail against the old behaviour.**
  PASS — both new tests intercept `subprocess.run` via a `side_effect` fake that records
  `outdir.exists()` and `outdir.parent.exists()` at call time, then asserts `False`/`True`
  respectively. Falsification check performed: temporarily reverted the fix back to
  `outdir.mkdir(parents=True, exist_ok=True)`, re-ran
  `pytest tests/annotators/test_bakta_utils.py -q -k outdir_not_pre_created`, and both new
  tests failed with `assert True is False` on the `outdir_exists` check — confirming the tests
  are load-bearing, not vacuous. Then restored the fix and re-ran the full file (35/35 pass).
- **No test that passed on the base commit fails on the branch.** PASS — full-suite run below
  reproduces exactly the same 1 failure and 6 errors listed as pre-existing/known-failing in
  the task envelope (all from the absent cobra/escher modeling stack), no new failures.

## tests_run
- `python -m pytest tests/annotators/test_bakta_utils.py -q` — **35 passed** (includes the 2
  new tests), run twice: once against the fix, and once (mid-verification, uncommitted
  scratch edit) against the reverted old `mkdir(outdir)` behaviour where the 2 new tests failed
  as expected (falsification check).
- `python -m pytest tests/ -q --continue-on-collection-errors` (exact baseline command,
  wall time ~275s, well under the 600000ms Bash timeout) —
  **1 failed, 2838 passed, 379 skipped, 6 errors** in 275.28s.
  - Failure: `tests/modeling/test_ms_reconstruction_utils.py::test_default_mode_uses_db_fallback`
    — matches the baseline's known-failing list exactly.
  - Errors: `tests/biochem/test_escher_utils.py`, `tests/notebook/helpers`, and all 4
    `tests/modeling/test_comprehensive_gapfill_wrapper.py::*` cases — matches the baseline's
    known-failing list exactly (absent cobra/escher modeling stack).
  - Passed count is 3 higher and skipped count is 1 lower than the recorded baseline
    (2835/380 → 2838/379). 2 of those extra passes are the new tests added in this commit; the
    third pass / one-fewer-skip is not attributable to any change in this diff (the touched
    file `bakta_utils.py` only affects `_run_bakta`, not skip conditions elsewhere) and is most
    plausibly pre-existing environment-dependent skip-condition variance (e.g. a
    `docker image inspect` / tool-availability probe) rather than a regression — no test that
    passed at baseline failed here, which is the stated success criterion.
- Environment: fresh venv at
  `/private/tmp/.../scratchpad/venv-kbutillib-bakta-fix`, built via
  `python3 -m venv <v> && <v>/bin/pip install -e "<worktree>[dev,all]"`. Confirmed
  `import kbutillib; kbutillib.__file__` resolves to this worktree (not a stale
  `~/VirtualEnvironments/` install) and `tomli-w` is present. Did not reuse any pre-existing venv.

## caveats
- Did not run Bakta for real, build Docker images, or reach poplar — no such route exists from
  this host, and the task explicitly excludes it. The KBDL real-dependency guard
  (`KBDLJobRunningPrototype`) that originally caught this defect lives in the KBDL repo, which
  was out of scope and untouched; it should go green on poplar once this branch merges to
  `main` and is picked up there, but that could not be verified from this host.
- Did not investigate the +1 unexplained pass / -1 skip discrepancy between the measured
  baseline (2835/380) and this run (2838/379) beyond confirming it does not include any
  newly-failing test and is not caused by any line this diff touches — flagged above for
  visibility rather than treated as blocking, per the stated success criterion ("no test that
  passed on the base commit fails on the branch").
- Scope fence honored: no changes to `KofamscanUtils`, the Dockerfiles, or the KBDL repo; no
  refactor of `_run_bakta` beyond the single line (plus docstring/comment) needed for the fix.
