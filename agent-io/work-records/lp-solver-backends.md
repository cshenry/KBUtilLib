# Work Record: lp-solver-backends

## task_id
lp-solver-backends (branch-derived; no numeric task-id was supplied in the envelope)

## branch
task/lp-solver-backends

## commit_shas
- 5850a9bf2d58c61befd4d4d05fb84606cd8d812b — feat(lp-solver): add solver_backends module for Remote LP-Solver service

## summary
Implemented `kbutillib/services/lp_solver/solver_backends.py` (plus a minimal
package `__init__.py`), the correctness core for the Remote LP-Solver service
described in `agent-io/prds/remote-lp-solver/fullprompt.md`. It exposes a
single stable interface `solve(lp_path, solver, time_limit, threads) -> dict`
returning exactly the external result schema (`status`, `objective_value`,
`variables`, `solver`, `solve_time_s`, `error`), with two backends — Gurobi
(`gurobipy`) and native CPLEX (`cplex`, not `docplex`, per S4) — behind that
one interface. Outcomes are mapped via an explicit whitelist
(OPTIMAL/MIP_optimal→optimal, INFEASIBLE→infeasible, UNBOUNDED→unbounded,
TIME_LIMIT→timeout); anything else, or any exception anywhere in the solve
path (bad solver name, unreadable LP, solver crash), comes back as
`status="error"` with a populated `error` string and `error=None` on every
non-error status. Gurobi's ambiguous `INF_OR_UNBD` (status 4) triggers a
single in-place re-solve with `DualReductions=0`, budgeted to the *remaining*
portion of the original `time_limit` (no extra slack), with `solve_time_s`
covering both runs. Timeouts return every variable + the incumbent objective
when the solver has a feasible incumbent (`SolCount>0` / `is_primal_feasible()`),
else `None`/`{}`. `time_limit` defaults to 3600s when omitted and is clamped
to `[0, 7200]`; `threads` defaults to 1 when omitted/non-positive. Variable
names are pulled from the solver's own name attribute (Gurobi `Var.VarName`,
CPLEX `variables.get_names()` + `solution.get_values()`), preserving a
byte-identical round-trip to the submitted LP text. Added
`tests/test_lp_solver_solver_backends.py` with small hand-built LP fixtures
covering optimal/infeasible/unbounded/timeout/INF_OR_UNBD/forced-error paths,
each solver-specific case gated with `pytest.importorskip` (S14) so the suite
skips cleanly on machines without `gurobipy`/`cplex` (this dev machine has
neither — both packages only live on H100).

## files_touched
- `src/kbutillib/services/lp_solver/__init__.py` (new)
- `src/kbutillib/services/lp_solver/solver_backends.py` (new)
- `tests/test_lp_solver_solver_backends.py` (new)

## success_criteria_check
- `solver_backends.py` exists with a `solve(lp_path, solver, time_limit, threads)` function returning a dict with exactly the six external-schema keys — **pass**. `test_result_schema_has_exactly_six_keys` and every per-status test assert `set(result.keys()) == {status, objective_value, variables, solver, solve_time_s, error}`; the top-level `solve()` also defensively re-projects onto exactly those six keys before returning.
- Supports gurobipy and native cplex — **pass**. Two backend functions (`_solve_gurobi`, `_solve_cplex`), dispatched by name; `_solve_cplex` imports `cplex` (never `docplex`), matching S4.
- Unknown outcomes map to `status="error"` with populated `error` — **pass**. Both backends have an `else` branch after the whitelist lookup that sets `status="error"` and a descriptive `error` string; `test_unsupported_solver_name_returns_error` exercises the dispatch-layer version of this without needing a solver installed; per-backend residual-status paths (e.g. a lingering `INF_OR_UNBD`, `SUBOPTIMAL`, license failure) route through the same `else` branch but aren't independently unit-tested here since forcing those specific solver-internal codes deterministically would require a live license/solver and isn't practical to author blind.
- INF_OR_UNBD re-solves with `DualReductions=0` — **pass, uncertain on the exact trigger reliability**. The code path is implemented exactly as specified (in-place `DualReductions=0`, remaining-time budget, `solve_time_s` spans both `optimize()` calls) and `test_gurobi_inf_or_unbd_disambiguates_to_unbounded` uses the classic free-variable/equality-constraint construction that Gurobi's default presolve is documented to report as status 4. I could not execute this test on this machine (no gurobipy) to confirm Gurobi's presolve actually reports `INF_OR_UNBD` rather than resolving straight to `UNBOUNDED` on the installed Gurobi version — the assertion (`status == "unbounded"`, `error is None`) holds either way, but it only proves the *re-solve code path itself* is correct if the INF_OR_UNBD branch is actually entered on the H100 Gurobi build. Flagging as a caveat below.
- Accompanying tests assert per-status behavior and variable-name round-trip and skip cleanly when solvers are unavailable — **pass**. Verified directly: `pytest tests/test_lp_solver_solver_backends.py -v` on this machine (no gurobipy/cplex) shows 2 passed (dispatch-layer, solver-independent) + 11 skipped (each with a `pytest.importorskip` reason message), 0 failed.

## tests_run
- `ruff check src/kbutillib/services/lp_solver tests/test_lp_solver_solver_backends.py` → pass ("All checks passed!").
- `ruff format --check ...` → pass after running `ruff format` once to normalize the new files to repo style (kept, no logic changes).
- `python3 -m pytest tests/test_lp_solver_solver_backends.py -v` → 2 passed, 11 skipped, 0 failed. The 11 skips are exactly the gurobi/cplex-gated cases (`pytest.importorskip`, S14) — this machine has neither package installed, matching `pip3 show gurobipy` / `python3 -c "import cplex"` both failing before I started.
- Did **not** run the full repo test suite (`pytest tests/`) — out of scope for this task's file footprint and the existing suite is large/slow; ran only the new test module plus ruff, both clean.
- Could not execute the solver-specific correctness assertions (optimal objective value, infeasible/unbounded detection, timeout-with-incumbent behavior, INF_OR_UNBD disambiguation) end-to-end against a real Gurobi/CPLEX license, since neither is installed on primary-laptop (per the PRD, only H100 has them). These will only actually run — and can only be truly verified — on H100 or wherever gurobipy/cplex are installed.

## caveats
- **INF_OR_UNBD trigger not empirically confirmed.** The re-solve *logic* is implemented per spec, but I could not run it against real Gurobi to confirm the chosen test LP (`x + y = 5` with both variables free, minimizing `x`) actually elicits Gurobi's ambiguous status 4 on the version installed on H100, versus Gurobi resolving it directly to `UNBOUNDED` without hitting the special-cased branch. If a future run on H100 shows the test passing "for the wrong reason" (i.e., status was never 4), the LP fixture may need adjusting (e.g. tweaking presolve settings or constraint structure) to reliably force the ambiguous code path — the production code itself doesn't need to change, only the test's ability to exercise it.
- **Timeout test timing is inherently environment-dependent.** `test_near_zero_time_limit_times_out` uses a hand-generated 30-variable 0/1 knapsack MILP (weight≈profit, capacity≈half the total, a classically hard instance shape) with `time_limit=1e-6`. This is designed to reliably produce a `TIME_LIMIT` status on any real hardware, but I could not execute it to confirm; if H100 solves it "too fast" on some future run (unlikely at 1e-6s but not impossible for a trivial instance), the fixture size/hardness may need bumping.
- **`threads`/`time_limit` defaulting lives in `solver_backends.py`, not just the (not-yet-built) `worker.py`/`app.py` layer.** The PRD's module layout describes `worker.py` as computing `threads_per_solve` from service config and `app.py`/config as owning `default_time_limit`/`max_time_limit` (S11, S15). Since this task's scope is only `solver_backends.py` and those sibling modules don't exist yet, I implemented the Acceptance-Criterion-8 default/clamp (`DEFAULT_TIME_LIMIT=3600`, `MAX_TIME_LIMIT=7200`) and a `threads` default of 1 directly inside `solve()` so the module is independently correct and testable now. This is naturally idempotent with whatever `worker.py`/`app.py` later also enforce from config, so there's no conflict — just a note that the constants are currently only defined in this module and not (yet) sourced from `remote_solver.*` config, since `config.yaml` was explicitly out of scope for this task.
- **A sibling in-flight task (`job_store.py`) shares this package directory.** I noticed `~/.maestro/worktrees/lp-solver-job-store` (a differently-named, seemingly recycled worktree) has an uncommitted `src/kbutillib/services/lp_solver/job_store.py`. I did not touch, read, or coordinate with that file/worktree — my `__init__.py` is intentionally import-free so it won't conflict with however that module lands. Whoever merges both branches should confirm `__init__.py` doesn't need updating to expose `job_store` symbols (out of scope here).
- Per instructions, `config.yaml`, `kbutillib/__init__.py` (top-level), and `toolkit.py` were not touched — confirmed via `git status --short` showing only the three new files.
