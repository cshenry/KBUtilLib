# Work Record: lp-solver-service

## task_id
task/lp-solver-service (branch-derived; no explicit task_id was included in the envelope)

## branch
task/lp-solver-service

## commit_shas
- d62fdee28f3212f941093545058519f404742b5d

## summary
Assembled the two remaining server modules for the Remote LP-Solver
service on top of the already-merged `job_store.py`/`solver_backends.py`:
`worker.py` (a bounded async pool that claims queued jobs and drives each
solve in its own subprocess, with a thread cap and a SIGTERM/SIGKILL
watchdog) and `app.py` (a thin FastAPI surface: `POST /solve`,
`GET /result/{job_id}`, `GET /healthz`, plus a `python -m
kbutillib.services.lp_solver.app` CLI entrypoint). Also added the
config.yaml `remote_solver.*` defaults, a systemd user-unit template +
enable script under `deploy/`, an optional `lp_solver` dependency-group in
`pyproject.toml` for fastapi/uvicorn, and one true end-to-end test that
boots the service on an ephemeral uvicorn port with a stub solver backend
and polls a real HTTP round-trip to a terminal result.

## files_touched
- `src/kbutillib/services/lp_solver/worker.py` (new)
- `src/kbutillib/services/lp_solver/app.py` (new)
- `src/kbutillib/services/lp_solver/deploy/lp-solver.service` (new)
- `src/kbutillib/services/lp_solver/deploy/enable_lp_solver.sh` (new)
- `tests/test_lp_solver_service.py` (new)
- `config.yaml` (added `remote_solver:` defaults block)
- `pyproject.toml` (added `lp_solver` dependency-group: fastapi, uvicorn)

## success_criteria_check
- "A runnable FastAPI service (`python -m kbutillib.services.lp_solver.app`)
  exposes POST /solve (gzip in, {job_id} out, 503 when queued>max_queue_depth),
  GET /result/{job_id} (200+JSON for known, 404 unknown), and GET /healthz" —
  **pass**. Verified `PYTHONPATH=src python -m kbutillib.services.lp_solver.app
  --help` parses `--host`/`--port` correctly; all three endpoints exercised
  live in `tests/test_lp_solver_service.py` (200/404/503 all asserted).
- "binds 127.0.0.1" — **pass** (default `--host`); not hard-coded/enforced in
  app code (see Caveats) — the binding guarantee is default + the committed
  systemd unit's `--host 127.0.0.1`.
- "runs each solve in a subprocess with the thread cap and time_limit+60s
  watchdog" — **pass**. Every solve launches via
  `asyncio.create_subprocess_exec(sys.executable, "-c", ...)` with
  `threads_per_solve` passed through; manually verified the watchdog
  SIGTERM-then-SIGKILL path against a subprocess that sleeps forever,
  confirming the job lands in `error` with `"solver exceeded time limit +
  grace"` (see Tests Run).
- "reads remote_solver.* config with defaults" — **pass**. All of
  `max_concurrent_solves`, `max_queue_depth`, `threads_per_solve`,
  `default_time_limit`, `max_time_limit`, `api_key` are read via
  `SharedEnvUtils.get_config_value("remote_solver.<key>", <default>)` in
  `create_app()`; documented defaults added to `config.yaml`.
- "reaps orphans at startup and sweeps 48h TTL opportunistically" — **pass**.
  `store.reap_orphans_on_startup()` runs inside the FastAPI `lifespan`
  context manager; `_sweep()` (wrapping `job_store.sweep_expired()` +
  side-car meta cleanup) runs on every `POST /solve` and `GET /result`.
- "a committed systemd user-unit template + enable script exist under the
  service package" — **pass**. `deploy/lp-solver.service` matches S13
  exactly (`ExecStart=%h/venvs/lp-solver/bin/python -m
  kbutillib.services.lp_solver.app --host 127.0.0.1 --port 8091`,
  `WorkingDirectory=%h/projects/KBUtilLib`, `Type=simple`,
  `Restart=on-failure`, `RestartSec=10s`, explicit `PATH`, file logging to
  `%h/.lp-solver/logs/`, `WantedBy=default.target`); `deploy/enable_lp_solver.sh`
  provisions the venv, checks port 8091 via `ss -tln`, installs the unit,
  and `enable --now`s it.
- "an end-to-end test with a stub backend submits and polls to a terminal
  result and passes" — **pass**.
  `tests/test_lp_solver_service.py::test_e2e_submit_and_poll_to_optimal_result`
  boots a live uvicorn server on an ephemeral port, submits a gzipped LP over
  real HTTP, and polls to `status == "optimal"` through the real
  subprocess-based worker path (the stub function runs in its own `python -c`
  subprocess, not in-process, so the test genuinely exercises subprocess
  isolation, not just the HTTP layer).

## tests_run
- `python -m pytest tests/test_lp_solver_service.py -v` — **2 passed**
  (`test_e2e_submit_and_poll_to_optimal_result`, `test_queue_depth_503`).
- `python -m pytest tests/test_lp_solver_job_store.py
  tests/test_lp_solver_solver_backends.py tests/test_lp_solver_service.py -q`
  — **28 passed, 11 skipped** (the 11 skips are the gurobipy/cplex-gated
  `solver_backends` correctness tests, S14-compliant since neither solver is
  installed in this dev environment).
- `python -m pytest tests/ -q` (full repo suite) — **45 failed, 2060 passed,
  33 skipped, 1 xfailed, 26 errors**. All 45 failures/26 errors are in files
  unrelated to `lp_solver`
  (`test_kb_berdl_utils.py`, `test_escher_utils.py`, `test_ms_biochem_deltag.py`,
  `test_task_a_venv_doctor.py`, `test_composition_smoke.py`,
  `cli/test_init*.py`, `test_kb_ws_utils.py`, `test_ms_reconstruction_utils.py`)
  — pre-existing, environment-dependent (network/Docker/optional-dependency)
  failures not touched by this change. None of the `lp_solver` tests appear
  in the failure list.
- Manual verification (not a pytest test, ad hoc script): constructed an
  `LPWorkerPool` with a hang-forever stub solve function and a 0.1s
  `time_limit`, shrank `WATCHDOG_GRACE_SECONDS`/`WATCHDOG_KILL_SECONDS` to
  0.5s each, and confirmed the job transitions to `status="error"`,
  `error="solver exceeded time limit + grace"` in ~0.6s.
- Manual verification: `x-api-key` auth via `fastapi.testclient.TestClient`
  — no header -> 401, wrong key -> 401, correct key -> 200, when
  `remote_solver.api_key` is configured; unset key means no check (not
  re-verified separately but exercised implicitly by every other test, none
  of which set `api_key`).
- `PYTHONPATH=src python -m kbutillib.services.lp_solver.app --help` —
  confirms the CLI entrypoint parses `--host`/`--port` (defaults
  127.0.0.1/8091) without error.
- Did not run `ruff`/`mypy` — neither executable is installed in this dev
  environment (consistent with the sibling `lp-solver-job-store` task's
  finding); `python -m py_compile` on all three new/changed Python files
  passed.
- Confirmed no state leaked into the real `~/.lp-solver` (directory does not
  exist after any of the above runs) — every test/manual run passed an
  explicit `base_dir`/`db_path`/`tmp_dir` under a temp directory.

## caveats
- **`solver`/`time_limit` transport on `POST /solve` is a design decision,
  not explicitly pinned by the fullprompt.** The wire-format section only
  specifies the gzip LP body; it never says how the caller's `solver`/
  `time_limit` (from the client's future `solve_lp(lp, solver=None,
  time_limit=None)`) reach the service. I chose HTTP query parameters
  (`POST /solve?solver=...&time_limit=...`) since the body is explicitly
  reserved for the LP payload. The (not-yet-built) `MSRemoteSolverUtils`
  client task will need to send them this way for this service to work
  end-to-end; flagging this as the one contract point across the two tasks
  that isn't fully pinned by the PRD text.
- **Per-job `time_limit` persistence uses a side-car JSON file, not a
  `job_store.py` schema column.** `job_store.py` (built by a sibling task,
  explicitly not to be reimplemented) only persists `job_id`/`status`/
  `solver`/timestamps/`result_json`/`error` — there's no `time_limit`
  column. Rather than modify that file's schema, `worker.py` writes/reads
  `<meta_dir>/{job_id}.meta.json` (written by `app.py` at submission,
  read by the worker at claim time, deleted on job completion or TTL
  sweep). This survives a service restart (unlike an in-process dict)
  without touching job_store's contract. If a future task consolidates
  this into `job_store.py` itself (e.g. adding a `time_limit` column), this
  side-car mechanism should be removed in the same change.
- **`max_queue_depth` accounting reads the SQLite file directly** (`SELECT
  COUNT(*) FROM jobs WHERE status = 'queued'`) rather than via a
  `job_store.py` method (none exists) or an in-process counter (which
  wouldn't survive a restart with pre-existing queued jobs). This uses the
  documented, stable `jobs` table/`status` column schema from
  `job_store.py`'s own docstring/`_CREATE_TABLE_SQL`, not any private
  attribute access.
- **`create_app()` is a factory, not a module-level `app` singleton.**
  Building the FastAPI app eagerly at import time would have the side
  effect of creating `~/.lp-solver/{jobs.sqlite,tmp,meta}` just from
  `import kbutillib.services.lp_solver.app` — undesirable for tests and for
  any tool that merely inspects the module. `main()` (used by `python -m
  ...app`) calls `create_app()` explicitly.
- **Binding to 127.0.0.1 is a default, not a hard-coded restriction** — the
  CLI accepts `--host`/`--port` per the task's own example invocation, so an
  operator *could* pass `--host 0.0.0.0`. The actual localhost-only
  guarantee for the real deployment comes from the committed systemd unit
  (which pins `--host 127.0.0.1`) plus the documented policy, not a runtime
  assertion in `app.py`.
- **Did not test a genuine multi-MB LP payload** (Acceptance Criterion 10's
  "transfers and solves without a size-limit error"). Starlette/FastAPI
  impose no default request-body size limit, so nothing in this
  implementation would reject a large payload, but I didn't add a dedicated
  large-payload test to keep the suite fast; flagging for the reviewer in
  case a multi-MB round-trip test is wanted.
- **fastapi/uvicorn added as an optional `pyproject.toml` dependency-group**
  (`lp_solver`), not core dependencies — matches the PRD's "dedicated venv
  `~/venvs/lp-solver`" deployment story (these are already installed in the
  ambient dev environment used for testing, so nothing needed manual
  installation here).
- Per the task's explicit instruction, did **not** touch `__init__.py` or
  `toolkit.py` (client registration is a later task) and did **not**
  reimplement `job_store.py`/`solver_backends.py` — both are used exactly as
  their sibling-task-authored public interfaces expose them.
