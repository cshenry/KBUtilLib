# Work Record: lp-solver-client

## task_id
task/lp-solver-client

## branch
task/lp-solver-client

## commit_shas
- 82d9343cd7a0ad54e11e97f320d107a91793c2d5

## summary

Implemented the KBUtilLib client for the Remote LP-Solver service
(`kbutillib/ms_remote_solver_utils.py`), per
`agent-io/prds/remote-lp-solver/fullprompt.md` ("Repo & module layout",
"Wire format & polling", the Confront-hardened specifics block, and
Acceptance Criteria 1, 10, 17-19). Before writing any code I read the merged
server (`src/kbutillib/services/lp_solver/app.py`) in this worktree to pin
the exact wire contract: `POST {base_url}/solve` takes the gzip-compressed
LP text as the request body (`Content-Type: text/plain; charset=utf-8`,
`Content-Encoding: gzip`) with `solver`/`time_limit` as **query parameters**
on that POST (confirmed directly from the FastAPI route signature
`async def solve(request, solver=None, time_limit=None, x_api_key=...)`),
and `GET /result/{job_id}` returns 200 + JSON with a `status` field carrying
`queued`/`running` or the terminal status, 404 for unknown job ids.

`MSRemoteSolverUtils(SharedEnvUtils)` gzip-compresses the LP (from raw text
or a file path), POSTs it with the query-param contract above, then polls
`GET {base_url}/result/{job_id}` every `poll_interval` (default 2.0s) with
+/-10% jitter until a terminal status, returning the service's result dict
verbatim (`status`/`objective_value`/`variables`/`solver`/`solve_time_s`/`error`).
A client-side `TimeoutError` is raised only after
`min(requested_time_limit, max_time_limit) + 90s` has elapsed since
submission -- the 90s grace lives as a module-level constant
(`_CLIENT_WAIT_GRACE_S`) read fresh on every call, so tests can shrink it via
`monkeypatch` without touching the documented production default. All
config is read via `get_config_value("remote_solver.*", ...)`
(`base_url`, `timeout`, `poll_interval`, `api_key`, `default_time_limit`,
`max_time_limit`); `x-api-key` is sent only when `api_key` is truthy.

A companion `MSRemoteSolverUtilsImpl` (holds `env`, lazy delegate, token
copy) mirrors `ArgoUtilsImpl` exactly -- same `_ensure_delegate()` /
`__getattr__` delegation shape, same defensive `token` copy from
`env.get_token("kbase")` (kept for pattern parity even though this service's
auth is the config-driven `api_key`, not a kbase token).

Registration: added a guarded `try/except ImportError` block for both
`MSRemoteSolverUtils` and `MSRemoteSolverUtilsImpl` in `src/kbutillib/__init__.py`
(added to both `__all__` lists, in the same relative position as the
`kb_berdl_utils`/`ArgoUtils` entries), and a lazy `@property remote_solver`
on `KBUtilLib` in `src/kbutillib/toolkit.py` (TYPE_CHECKING import,
`self._remote_solver = None` backing field, and the property itself --
placed immediately before the `patric` property, matching the file's
existing ordering convention). `config.yaml`'s `remote_solver.*` keys were
already present from the merged service task and were not touched.

Added one integration-test module (`tests/test_ms_remote_solver_utils.py`)
using a stdlib `http.server.ThreadingHTTPServer` stub (no FastAPI/uvicorn
or real service dependency) with three tests: (1) happy path -- asserts the
gzip body decompresses to the exact submitted LP text, `Content-Type`/
`Content-Encoding` headers are correct, `solver`/`time_limit` arrive as
query parameters (not body/headers), the client polls through two
non-terminal responses before returning the terminal dict verbatim; (2)
`TimeoutError` path -- stub always returns `running`; `_CLIENT_WAIT_GRACE_S`
is monkeypatched down to 0.05s so the test runs in milliseconds while still
exercising the real poll loop and raise path; (3) a small unit test for the
`lp_text_or_path` dual-mode (raw text vs. file path) argument.

## files_touched

- `src/kbutillib/ms_remote_solver_utils.py` (new)
- `src/kbutillib/__init__.py` (guarded import + `__all__` entries for
  `MSRemoteSolverUtils`/`MSRemoteSolverUtilsImpl`)
- `src/kbutillib/toolkit.py` (TYPE_CHECKING import, `_remote_solver` backing
  field, `remote_solver` lazy property)
- `tests/test_ms_remote_solver_utils.py` (new)
- `agent-io/work-records/lp-solver-client.md` (this file)

## success_criteria_check

1. `kbu.remote_solver.solve_lp(lp_text_or_path, solver=None, time_limit=None)` reachable via the facade, gzip-POSTs with correct headers, polls at 2s +/-10% jitter to terminal, returns the exact 6-key dict, raises TimeoutError only after `min(requested, max) + 90s` -- **PASS**: `kbu.remote_solver` verified live (manual smoke test showed correct `MSRemoteSolverUtilsImpl` type + `base_url` resolution from config.yaml); wire behavior verified by `tests/test_ms_remote_solver_utils.py::test_solve_lp_gzip_submit_query_params_and_poll_to_terminal` and `::test_solve_lp_raises_timeout_error_when_never_terminal`.
2. `solver`/`time_limit` sent as query parameters on `POST /solve`, matching the merged `app.py` route signature exactly -- **PASS**: confirmed by reading `services/lp_solver/app.py`'s `async def solve(request, solver=None, time_limit=None, ...)` before writing the client; test asserts `stub.captured["query"]` contains both.
3. `GET /result/{job_id}` polling loop returns the service's result dict verbatim on a terminal status -- **PASS**: test's `result_provider` returns non-terminal for the first two polls, terminal on the third; `result == _TERMINAL_RESULT` (dict equality, exact 6 keys).
4. `x-api-key` sent only when `remote_solver.api_key` is configured (non-empty) -- **PASS** (by construction/inspection): `self.headers`/poll-request headers only include `x-api-key` when `self.api_key` is truthy, mirroring `ArgoUtils`'s pattern. Not covered by an explicit test (the PRD's testing-decisions section only calls out gzip/query-params/poll-loop/timeout as required test assertions); flagged as a caveat below.
5. `MSRemoteSolverUtils`/`MSRemoteSolverUtilsImpl` registered in `__init__.py` and `toolkit.py` mirroring the `ArgoUtils`/`ArgoUtilsImpl` pattern exactly -- **PASS**: same guarded-import shape, same lazy-property shape (`if self._remote_solver is None: ... self._remote_solver = MSRemoteSolverUtilsImpl(self.env)`), same `__all__` list placement pattern.
6. Stubbed-server integration test verifies gzip submission, poll-to-terminal, and the timeout path, and passes -- **PASS**: `python -m pytest tests/test_ms_remote_solver_utils.py -v` -> 3 passed.
7. Runs without a real solver or real service -- **PASS**: the stub is a stdlib `http.server` instance; no `gurobipy`/`cplex`/FastAPI/uvicorn import is required for this test module.

## tests_run

```
cd /Users/chenry/.maestro/worktrees/lp-solver-client
python -m pytest tests/test_ms_remote_solver_utils.py -v
# 3 passed in 2.27s

python -m pytest tests/test_ms_remote_solver_utils.py tests/test_lp_solver_service.py tests/test_lp_solver_job_store.py -q
# 29 passed in 5.03s (no regressions in the neighboring lp-solver modules)

python -c "import kbutillib; kbu = kbutillib.KBUtilLib(config_file=False, token_file=None, kbase_token_file=None); print(type(kbu.remote_solver), kbu.remote_solver.base_url)"
# MSRemoteSolverUtilsImpl, base_url=http://127.0.0.1:8091 (repo config.yaml default) -- confirms facade wiring end-to-end
```

Also attempted the full repo test suite (`python -m pytest tests/ -q`, 2211
tests collected). It ran to ~32% (well past this task's own tests, which
sort near the front alphabetically under `tests/` root) with 2 pre-existing
failures in `tests/cli/` (venvman/init-related, unrelated to
`ms_remote_solver_utils`/`__init__.py`/`toolkit.py`) and then stalled for
several minutes on what appears to be a slow/hanging CLI subprocess test
(`tests/cli/test_init.py::TestVenvmanDetection` or a neighbor) unrelated to
this change. I killed that run rather than block indefinitely on a
pre-existing, unrelated environment issue; confirmed via `git stash` that
these lint/test characteristics (both the ruff I001 import-order and F401
warnings in `__init__.py`/`toolkit.py`, and the general slowness of the
full CLI suite) predate this change.

`ruff check` on the touched files reports only pre-existing `I001`
(import-block sort order) and one pre-existing `F401` (unused `Optional` in
`toolkit.py`) findings that exist identically on unmodified `main` (verified
via `git stash`/`git stash pop`); no new lint findings were introduced by
this change.

## caveats

- The `x-api-key` header behavior (send only when `remote_solver.api_key`
  is configured) is implemented and matches `ArgoUtils`'s pattern but is
  not covered by an explicit assertion in the integration test, since the
  task's stated test scope was gzip/query-params/poll-loop/timeout. This
  is low-risk (a two-line conditional, structurally identical to
  `ArgoUtils.headers`) but flagged for the reviewer's awareness.
- `lp_text_or_path` disambiguates "text" vs. "path" by checking
  `os.path.isfile(...)` on the string; this means LP text that happens to
  be a short, single-line string matching an existing file path on disk
  would be (mis)read as a file. This matches the natural reading of the
  PRD's `solve_lp(lp_text_or_path, ...)` signature and is the same
  heuristic used informally elsewhere in the codebase for "text-or-path"
  parameters; flagged as a judgment call rather than an explicit PRD
  requirement.
- The full 2211-test repo suite was not run to completion (see `tests_run`
  above) due to a pre-existing slow/hanging test unrelated to this change;
  targeted runs (this module's tests + the sibling lp-solver client/service
  modules) all pass cleanly.
- Did not modify `config.yaml` (`remote_solver.*` keys already existed from
  the merged service task), per the task's explicit instruction not to
  re-add them.
