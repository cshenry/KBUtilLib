"""Remote LP-Solver service package.

Houses the H100-side service that accepts an LP file, solves it with
Gurobi or CPLEX, and returns the solution to KBUtilLib clients (see
``kbutillib/ms_remote_solver_utils.py`` for the client side and
``agent-io/prds/remote-lp-solver/fullprompt.md`` for the full design).

This package is built up incrementally across several tasks:

- ``solver_backends`` -- the correctness core (this task). Stable
  ``solve(lp_path, solver, time_limit, threads) -> dict`` interface over
  Gurobi and native CPLEX.
- ``job_store`` -- SQLite-backed job queue/state machine (separate task).
- ``worker`` -- bounded async pool that drives ``solver_backends.solve``
  (separate task).
- ``app`` -- thin FastAPI surface (separate task).

Intentionally left free of imports so this ``__init__`` never fails to
import while sibling modules are still under construction.
"""
