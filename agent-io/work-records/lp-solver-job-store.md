# Work Record: lp-solver-job-store

## task_id
lp-solver-job-store (branch-derived; no explicit task_id was included in the envelope)

## branch
task/lp-solver-job-store

## commit_shas
- 171c1749d7a990bd93c47c3cc2c183c51fb02a26

## summary
Implemented `kbutillib/services/lp_solver/job_store.py`, the SQLite-backed
job store for the Remote LP-Solver service described in
`agent-io/prds/remote-lp-solver/fullprompt.md` ("Persistence & lifecycle" and
the S5/S12/S15 "Confront-hardened specifics"). `LPJobStore` owns the jobs
table and per-job LP temp files behind the interface `create`, `claim_next`,
`mark_running`, `mark_done`, `mark_error`, `get`, `sweep_expired`, and
`reap_orphans_on_startup`. The DB is opened in WAL mode with a connection
`timeout` + `PRAGMA busy_timeout` (retry-on-busy) because multiple solve
subprocesses and the API process will touch it concurrently; `claim_next`
uses an explicit `BEGIN IMMEDIATE` transaction so two callers can never claim
the same queued job. `job_id` is a UUIDv4 string. `create()` writes the LP
text to `<tmp_dir>/{job_id}.lp`; `mark_done`/`mark_error` delete that file
(interpreting "on completion" as either terminal outcome, not only success —
see Caveats), and `sweep_expired`/`reap_orphans_on_startup` also
best-effort-delete it for defense in depth. `db_path` and `tmp_dir` are both
independently injectable constructor args (defaulting to
`~/.lp-solver/jobs.sqlite` and `~/.lp-solver/tmp`), so tests never touch the
real service state. Per the task's merge-conflict-avoidance instruction, no
`kbutillib/services/lp_solver/__init__.py` was created — the package resolves
today as an implicit Python namespace package, which is sufficient for both
the tests in this branch and for the sibling task (which owns `__init__.py`)
to merge in cleanly afterward.

## files_touched
- `src/kbutillib/services/lp_solver/job_store.py` (new)
- `tests/test_lp_solver_job_store.py` (new)

## success_criteria_check
- "`kbutillib/services/lp_solver/job_store.py` exists exposing
  create/claim_next/mark_running/mark_done/mark_error/get/sweep_expired/
  reap_orphans_on_startup" — **pass**. All eight methods are implemented with
  exactly those names/signatures (`create(lp, solver=None) -> job_id`,
  `claim_next() -> dict|None`, `mark_running(job_id)`,
  `mark_done(job_id, result)`, `mark_error(job_id, error)`, `get(job_id)`,
  `sweep_expired(ttl_seconds=...) -> list[str]`,
  `reap_orphans_on_startup() -> list[str]`).
- "over a WAL-mode SQLite DB with UUIDv4 string job_ids and per-job LP temp
  files" — **pass**. `PRAGMA journal_mode=WAL` + `PRAGMA busy_timeout` are set
  at connect time (asserted by `TestWalMode::test_db_opened_in_wal_mode`);
  `create()` returns `str(uuid.uuid4())` (asserted by
  `TestCreate::test_create_returns_uuid4_string`); LP text is written to
  `<tmp_dir>/{job_id}.lp` (asserted by
  `TestCreate::test_create_writes_lp_temp_file`).
- "tests (solver-independent, temp DB) verify state transitions" — **pass**.
  `TestStateMachine` covers create→claim(→running)→done and
  create→claim(→running)→error, plus `mark_running` idempotency and
  claim-race exclusion (`test_claim_next_skips_non_queued_jobs`). No test
  imports gurobipy/cplex/solver_backends.
- "48h TTL sweep" — **pass**. `TestSweepExpired` backdates `end_ts` past and
  within the 48h boundary and confirms removal only past it, and confirms
  `queued`/`running` jobs are never swept regardless of age.
- "orphan-on-restart -> error with message 'service restarted during solve'"
  — **pass**. `TestReapOrphansOnStartup::test_reap_orphans_marks_running_job_as_error`
  asserts `row["error"] == ORPHAN_ERROR_MESSAGE` where
  `ORPHAN_ERROR_MESSAGE = "service restarted during solve"` (byte-identical to
  the PRD's required string).
- "pass locally" — **pass**. `python3 -m pytest tests/test_lp_solver_job_store.py -v`
  → 24 passed, 0 failed, 0 skipped (see Tests Run below).
- "Do NOT modify config.yaml, __init__.py, or toolkit.py" — **pass**. None of
  those three files were touched; `git status`/`git show --stat` on the
  commit show only the two new files listed above.

## tests_run
- `python3 -m pytest tests/test_lp_solver_job_store.py -v` (from the worktree
  root, system `python3` = pyenv 3.11.14, no venv activation needed since
  `tests/conftest.py` prepends `<repo>/src` to `sys.path`) — **24 passed**.
- Did not run the full repo test suite (`pytest tests/`) or `noxfile.py`
  sessions (mypy/ruff/pre-commit) — no `ruff`/`mypy` executable was available
  in the ambient `pyenv` python or in the two candidate venvs
  (`~/VirtualEnvironments/kbutillib-py3.11`, `~/VirtualEnvironments/KBUtilLib-py3.13`),
  and running the full suite risked colliding with the sibling task's
  concurrently-changing files in the shared Dropbox repo. The reviewer should
  lint/mypy this file if the repo's CI does so as a gate.

## caveats
- **Temp-file deletion on `mark_error`, not just `mark_done`.** The PRD says
  LP temp files are "deleted on completion or by the TTL sweep," which is
  ambiguous about whether "completion" includes the `error` terminal state. I
  interpreted it as "job reached a terminal state" (both `done` and `error`)
  since the LP is never needed again once a job is definitively finished
  either way, and leaving it on disk after an `error` would be a silent leak.
  If the reviewer/PRD intends temp files to survive `error` (e.g. for
  post-mortem debugging), that's a one-line change to drop the
  `self._remove_lp_file(job_id)` call from `mark_error`.
- **No `__init__.py` created**, per the task's explicit instruction to avoid
  a merge conflict with the sibling task that owns
  `kbutillib/services/lp_solver/__init__.py` (and, transitively,
  `kbutillib/services/__init__.py`, which also does not yet exist). Verified
  that both `import kbutillib.services.lp_solver.job_store` and running
  pytest work today via Python's implicit namespace-package support — no
  stub file was needed for this task's own tests to pass. This does mean the
  package isn't `pip install`-able as a normal package until the sibling
  branch (or a subsequent merge) adds `__init__.py`; that is expected and
  owned by the sibling task per the coordinator's split.
- **`claim_next()` vs `mark_running()` overlap.** The PRD's three-state model
  (`queued -> running -> done | error`) has no separate "claimed but not yet
  started" state, so `claim_next()` must itself flip `queued -> running`
  atomically to prevent two workers from claiming the same job — it does
  this via `BEGIN IMMEDIATE` + a conditional `UPDATE ... WHERE status =
  'queued'` checked by `rowcount`. `mark_running()` is kept as a separate,
  idempotent, standalone setter per the task's explicit interface list, for
  callers that already know which job they're working and want to
  (re-)assert `running`/stamp `start_ts` without going through the
  claim-and-dequeue path. Documented in the module docstring for the next
  reader (likely `worker.py`'s author).
- **Result dict shape is caller-defined.** `mark_done(job_id, result)` stores
  whatever JSON-serializable dict is passed as `result_json`; this store does
  not validate it against the `SolveResult` schema (`status`,
  `objective_value`, `variables`, `solver`, `solve_time_s`, `error`) — that
  validation belongs to `solver_backends.py`/`worker.py`, out of this task's
  scope.
- **Did not touch** `config.yaml`, `__init__.py`, or `toolkit.py`, as
  instructed.
- **Sibling-task coordination:** confirmed via `git worktree list` in the
  Dropbox repo that no other worktree currently touches
  `src/kbutillib/services/lp_solver/`; this branch only adds
  `job_store.py` + its test file, so a merge with the sibling's
  `__init__.py`/`solver_backends.py` branch should be conflict-free.
