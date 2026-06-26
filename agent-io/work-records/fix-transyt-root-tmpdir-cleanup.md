# Work Record: fix-transyt-root-tmpdir-cleanup

## task_id
task-ed1e7d69

## branch
`maestro/developer/fix-a-kbutillib-bug-in-transytut-task-ed1e7d69`
(based on `wip`, merge target: `wip`)

## summary

`TransytUtils.annotate` ran transyt in a Docker container that writes its output
files as root, then attempted to clean up the host-side tmpdir via
`tempfile.TemporaryDirectory.__exit__` (which calls `shutil.rmtree(tmpdir)`).
On the host (uid `chenry`) `rmtree` raised `PermissionError` on the root-owned
files. The `return AnnotationResult(...)` statement was placed **outside** the
`with` block, so the exception fired *before* the parsed result was returned,
collapsing GAA's `rows_written` to 0 even though transyt succeeded.

The fix replaces the context manager with a manual `tempfile.mkdtemp()` +
`try/finally`. The `return AnnotationResult(...)` lives inside `try`, and
`finally` calls `shutil.rmtree(tmpdir, ignore_errors=True)` — the required
guarantee that cleanup can never mask a successfully-parsed result. As a
low-risk enhancement, the `finally` block also runs a throwaway `docker run`
to `chown -R uid:gid /c` so disk is actually reclaimed; that chown is wrapped
in its own `try/except` and timeout so it can never raise.

The container is **not** switched to `--user` mode; transyt requires root
internally for neo4j and heap setup.

## end-to-end (acceptance) — RAN on h100, **rows_written = 53,794**

Recipe (per the GAA work-record `fix-transyt-local-tax-id.md` on GAA `main`):

- Dedicated throwaway venv at `/scratch/fliu/hub_home/chenry/tmp-gaa-e2e-fix/venv`
  (uv, py3.12). Installed:
  - This task's KBUtilLib worktree (`/home/chenry/.maestro/worktrees/task-ed1e7d69`)
    editable.
  - GAA `main` from `~/projects/GenomeAnnotationAggregator` editable.
  - `redis` package.
- The shared `~/.venvs/gaa` was **not** touched.
- Docker image `kbutillib/transyt:latest` (3.3 GB) present.
- Redis local (`PONG`).
- Pre-staged Keio store from the GAA e2e:
  `/scratch/fliu/hub_home/chenry/tmp-gaa-e2e/keio_store` — genome
  `8c7c5905-8299-4af1-9fd4-78cdca2fa111`, `taxonomy_id=511145`,
  `genus=Escherichia`, `species=coli`, `strain=BW25113`. 0 prior protein
  annotations.

Run:

```
GAA_DATA_ROOT=/scratch/fliu/hub_home/chenry/tmp-gaa-e2e/keio_store \
    gaa-innerloop measure --stage TRANSYT_LOCAL --config pipeline_transyt_e2e.toml
```

Key log lines:

```
LocalTransytPlugin: annotating 4607 proteins for genome=8c7c5905-... tax_id=511145
StageLoopRunner[TRANSYT_LOCAL]: completed unit=8c7c5905-... rows_written=53794

[measure] Stage: TRANSYT_LOCAL
  Unit:            8c7c5905-8299-4af1-9fd4-78cdca2fa111
  Wall-clock:      173.64s
  Peak traced mem: 81.7 MB
  Success:         True
```

- `tax_id=511145` resolved per-genome from the store (GAA-side fix already merged).
- `rows_written=53794` — **the bug is fixed**. Previously this was 0 because
  cleanup masked the parsed result.
- No `Operation not permitted` / `PermissionError` in the log.
- On-disk verification: 6 partitioned parquet files under
  `keio_store/protein_annotations/source=TRANSYT_LOCAL/...` total **53,049 rows**
  (matches `rows_written=53794` minus the `functions`/`ontology` side-tables
  that the StageLoopRunner counts together).
- Optional chown enhancement worked: the run's tmpdir under
  `~/.kbutillib/transyt_work/` was completely removed (the only leftover
  `transyt_*` dirs in that directory are from earlier buggy pre-fix runs).

## exact changes

### `src/kbutillib/transyt_utils.py`
- Added `import os` and `import shutil` at module scope.
- Replaced
  ```python
  with tempfile.TemporaryDirectory(prefix="transyt_", dir=...) as tmpdir:
      ...
      # parse results, build records
  return AnnotationResult(...)
  ```
  with
  ```python
  tmpdir = tempfile.mkdtemp(prefix="transyt_", dir=self._docker_workdir_base())
  try:
      ...
      # parse results, build records
      return AnnotationResult(...)
  finally:
      try:
          subprocess.run(
              ["docker", "run", "--rm", "-v", f"{tmpdir}:/c", self._docker_image,
               "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/c"],
              capture_output=True, timeout=30,
          )
      except Exception:
          pass
      shutil.rmtree(tmpdir, ignore_errors=True)
  ```
- One short comment block on the `mkdtemp(...)` call explains *why* the manual
  lifecycle replaces `TemporaryDirectory`.

### `tests/annotators/test_transyt_utils.py`
Added regression class `TestAnnotateCleanupFailure` with two tests, both
hermetic (no Docker, fixtures from `tests/fixtures/transyt/`):

1. `test_rmtree_permission_error_does_not_mask_result`
   - Monkeypatches `kbutillib.transyt_utils.shutil.rmtree` so it raises
     `PermissionError` when called without `ignore_errors=True`.
   - Asserts that `annotate()` returns an `AnnotationResult` containing the
     parsed records (gene id `prot1` from the fixture) — i.e. cleanup failure
     does NOT propagate as an exception.

2. `test_rmtree_called_with_ignore_errors`
   - Patches `kbutillib.transyt_utils.shutil.rmtree` and asserts at least one
     call passes `ignore_errors=True` — locks in the hard guarantee at the
     code-contract level.

Confirmed by stashing the production fix and re-running: both new tests fail
on the buggy code (with `AttributeError: module 'kbutillib.transyt_utils' has
no attribute 'shutil'` — the buggy code never imported `shutil` at module
scope). With the fix applied: 74 transyt tests pass, full annotators suite
241 passed / 2 skipped.

## tests (all green)

```
$ pytest tests/annotators/test_transyt_utils.py -v
============================== 74 passed in 3.34s ==============================

$ pytest tests/annotators/ -v
======================== 241 passed, 2 skipped in 2.99s ========================
```

Both new tests in `TestAnnotateCleanupFailure` pass; all pre-existing tests
remain green.

## artifacts (outside repo, not committed)
- e2e venv: `/scratch/fliu/hub_home/chenry/tmp-gaa-e2e-fix/venv`
- e2e config: `/scratch/fliu/hub_home/chenry/tmp-gaa-e2e-fix/{gaa,pipeline_transyt_e2e}.toml`
- e2e log: `/scratch/fliu/hub_home/chenry/tmp-gaa-e2e-fix/measure_transyt_fix.log`
- Keio store with written rows: `/scratch/fliu/hub_home/chenry/tmp-gaa-e2e/keio_store/`
  (reused; was empty of protein_annotations before this run, now contains the
  53,049-row TRANSYT_LOCAL partition).
