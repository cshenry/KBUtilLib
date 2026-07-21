"""Solver-backend correctness core for the Remote LP-Solver service.

This is the deepest module in ``kbutillib.services.lp_solver`` (see
``agent-io/prds/remote-lp-solver/fullprompt.md``, "Repo & module layout").
It owns:

- reading an LP file,
- setting the solver's native time limit and thread count,
- running the solve,
- the Gurobi ``INF_OR_UNBD`` disambiguation re-solve, and
- extracting ``{var_name: value}`` for *every* variable via the solver's
  own name attribute (Gurobi ``Var.VarName``, CPLEX
  ``variables.get_names()``).

It deliberately knows nothing about jobs, HTTP, or SQLite -- those live in
sibling modules (``job_store``, ``worker``, ``app``) built separately.

Public interface
-----------------
``solve(lp_path, solver=None, time_limit=None, threads=None) -> dict``

The returned dict (a plain ``dict`` -- "SolveResult" is not a distinct
type, per fullprompt.md S1) always has exactly these keys and no others:

``status``
    One of ``"optimal"``, ``"infeasible"``, ``"unbounded"``,
    ``"timeout"``, ``"error"``.
``objective_value``
    ``float`` for ``optimal`` and for ``timeout`` with a feasible
    incumbent; ``None`` otherwise.
``variables``
    ``{var_name: value}`` for every variable the solver reports, for
    ``optimal`` and for ``timeout`` with a feasible incumbent; ``{}``
    otherwise.
``solver``
    ``"gurobi"`` or ``"cplex"`` -- the engine that actually ran.
``solve_time_s``
    Wall-clock solve time (``float``), including both runs when the
    Gurobi ``INF_OR_UNBD`` re-solve fires.
``error``
    ``None`` for every non-error status; a populated string describing
    the raw solver status/exception when ``status == "error"``.

Status mapping is an explicit whitelist. Anything not on the whitelist
(numeric trouble, interrupted, suboptimal, license failure, an exception
raised anywhere in the solve) maps to ``status == "error"`` with the raw
solver code/message preserved in ``error`` -- never a silently-wrong
``"optimal"``.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

__all__ = ["solve", "DEFAULT_TIME_LIMIT", "MAX_TIME_LIMIT"]

# ---------------------------------------------------------------------------
# Result-schema keys (S1) -- kept as a single source of truth.
# ---------------------------------------------------------------------------

_RESULT_KEYS = (
    "status",
    "objective_value",
    "variables",
    "solver",
    "solve_time_s",
    "error",
)

# ---------------------------------------------------------------------------
# Time-limit defaults / clamp (Acceptance Criterion 8).
# ---------------------------------------------------------------------------

DEFAULT_TIME_LIMIT = 3600.0  # seconds, applied when the caller omits time_limit
MAX_TIME_LIMIT = 7200.0  # seconds, ceiling caller values are clamped to


def _resolve_time_limit(time_limit: Optional[float]) -> float:
    """Apply the default/clamp policy from fullprompt.md Acceptance Criterion 8."""
    if time_limit is None:
        time_limit = DEFAULT_TIME_LIMIT
    time_limit = float(time_limit)
    return max(0.0, min(time_limit, MAX_TIME_LIMIT))


def _resolve_threads(threads: Optional[int]) -> int:
    """Normalize the caller-supplied thread count to a positive int.

    ``worker.py`` (a sibling module, built separately) is responsible for
    computing ``threads_per_solve`` from service config (S11); this
    function only guards against ``None``/non-positive values reaching a
    solver's native thread parameter.
    """
    if threads is None or threads < 1:
        return 1
    return int(threads)


def _error_result(
    solver_name: str, solve_time_s: float, message: str
) -> Dict[str, Any]:
    """Build a fully-formed error result -- the universal safety net."""
    return {
        "status": "error",
        "objective_value": None,
        "variables": {},
        "solver": solver_name,
        "solve_time_s": solve_time_s,
        "error": message,
    }


# ---------------------------------------------------------------------------
# Gurobi backend
# ---------------------------------------------------------------------------


def _solve_gurobi(lp_path: str, time_limit: float, threads: int) -> Dict[str, Any]:
    start = time.monotonic()

    try:
        import gurobipy as gp
        from gurobipy import GRB
    except Exception as exc:  # pragma: no cover - exercised only without gurobipy
        return _error_result("gurobi", 0.0, f"gurobipy import failed: {exc}")

    # Explicit whitelist (fullprompt.md "Result schema & status mapping").
    # GRB.INF_OR_UNBD (4) is handled separately below, never looked up here.
    status_whitelist = {
        GRB.OPTIMAL: "optimal",
        GRB.INFEASIBLE: "infeasible",
        GRB.UNBOUNDED: "unbounded",
        GRB.TIME_LIMIT: "timeout",
    }

    try:
        env = gp.Env(empty=True)
        env.setParam("OutputFlag", 0)
        env.start()
        model = gp.read(lp_path, env=env)
        model.Params.TimeLimit = time_limit
        model.Params.Threads = threads
        model.optimize()
        status_code = model.Status

        if status_code == GRB.INF_OR_UNBD:
            # S3 / Acceptance Criterion 4: re-solve once with DualReductions=0
            # to definitively distinguish infeasible vs unbounded, staying
            # within the ORIGINAL time_limit (no extra slack) -- give the
            # re-solve only what's left of the original budget.
            elapsed = time.monotonic() - start
            remaining = max(0.0, time_limit - elapsed)
            model.Params.DualReductions = 0
            model.Params.TimeLimit = remaining
            model.optimize()
            status_code = model.Status

        solve_time_s = time.monotonic() - start
        mapped = status_whitelist.get(status_code)

        if mapped == "optimal":
            variables = {v.VarName: v.X for v in model.getVars()}
            objective_value = model.ObjVal
            error = None
        elif mapped == "timeout":
            # S2 / Acceptance Criterion 7: return the full incumbent when one
            # exists; otherwise None/{} -- never partially omit variables.
            if model.SolCount > 0:
                variables = {v.VarName: v.X for v in model.getVars()}
                objective_value = model.ObjVal
            else:
                variables = {}
                objective_value = None
            error = None
        elif mapped in ("infeasible", "unbounded"):
            variables = {}
            objective_value = None
            error = None
        else:
            # Anything unrecognized (including a residual INF_OR_UNBD,
            # SUBOPTIMAL, NUMERIC, INTERRUPTED, license issues, etc.)
            mapped = "error"
            variables = {}
            objective_value = None
            error = f"unrecognized gurobi status code: {status_code!r}"

        return {
            "status": mapped,
            "objective_value": objective_value,
            "variables": variables,
            "solver": "gurobi",
            "solve_time_s": solve_time_s,
            "error": error,
        }
    except Exception as exc:
        solve_time_s = time.monotonic() - start
        return _error_result("gurobi", solve_time_s, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# CPLEX backend (native `cplex`, not `docplex` -- S4)
# ---------------------------------------------------------------------------


def _solve_cplex(lp_path: str, time_limit: float, threads: int) -> Dict[str, Any]:
    start = time.monotonic()

    try:
        import cplex
    except Exception as exc:  # pragma: no cover - exercised only without cplex
        return _error_result("cplex", 0.0, f"cplex import failed: {exc}")

    try:
        c = cplex.Cplex()
        # Silence all CPLEX output streams (mirrors Gurobi's OutputFlag=0).
        c.set_log_stream(None)
        c.set_error_stream(None)
        c.set_warning_stream(None)
        c.set_results_stream(None)

        c.read(lp_path)
        c.parameters.timelimit.set(time_limit)
        c.parameters.threads.set(threads)
        c.solve()

        solve_time_s = time.monotonic() - start

        st = c.solution.status
        status_code = c.solution.get_status()

        # Explicit whitelist built from the named CPLEX status constants so
        # it's robust across CPLEX versions (getattr guards constants that
        # don't exist in a given version). LP-only names (optimal,
        # infeasible, unbounded, abort_time_limit) and their MIP
        # counterparts (MIP_optimal, MIP_infeasible, MIP_unbounded,
        # MIP_time_limit_feasible/infeasible) both map, per fullprompt.md
        # ("MIP_optimal -> optimal", "TIME_LIMIT -> timeout").
        status_whitelist = {
            getattr(st, "optimal", None): "optimal",
            getattr(st, "MIP_optimal", None): "optimal",
            getattr(st, "infeasible", None): "infeasible",
            getattr(st, "MIP_infeasible", None): "infeasible",
            getattr(st, "unbounded", None): "unbounded",
            getattr(st, "MIP_unbounded", None): "unbounded",
            getattr(st, "abort_time_limit", None): "timeout",
            getattr(st, "MIP_time_limit_feasible", None): "timeout",
            getattr(st, "MIP_time_limit_infeasible", None): "timeout",
        }
        status_whitelist.pop(None, None)

        mapped = status_whitelist.get(status_code)

        if mapped in ("optimal", "timeout") and c.solution.is_primal_feasible():
            names = c.variables.get_names()
            values = c.solution.get_values()
            variables = dict(zip(names, values))
            objective_value = c.solution.get_objective_value()
            error = None
        elif mapped == "timeout":
            # Timed out with no feasible incumbent (S2).
            variables = {}
            objective_value = None
            error = None
        elif mapped in ("infeasible", "unbounded"):
            variables = {}
            objective_value = None
            error = None
        else:
            mapped = "error"
            variables = {}
            objective_value = None
            error = (
                f"unrecognized cplex status: {c.solution.get_status_string()!r} "
                f"(code {status_code!r})"
            )

        return {
            "status": mapped,
            "objective_value": objective_value,
            "variables": variables,
            "solver": "cplex",
            "solve_time_s": solve_time_s,
            "error": error,
        }
    except Exception as exc:
        solve_time_s = time.monotonic() - start
        return _error_result("cplex", solve_time_s, f"{type(exc).__name__}: {exc}")


_BACKENDS = {
    "gurobi": _solve_gurobi,
    "cplex": _solve_cplex,
}


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def solve(
    lp_path: str,
    solver: Optional[str] = None,
    time_limit: Optional[float] = None,
    threads: Optional[int] = None,
) -> Dict[str, Any]:
    """Solve an LP/MILP file with Gurobi or CPLEX and return a plain dict.

    Args:
        lp_path: Path to an LP-format file on disk (the caller/service layer
            owns writing it there; this function only reads it).
        solver: ``"gurobi"`` or ``"cplex"`` (case-insensitive). ``None``
            defaults to ``"gurobi"`` (Acceptance Criterion 6).
        time_limit: Caller-requested wall-clock time limit in seconds.
            ``None`` applies ``DEFAULT_TIME_LIMIT`` (3600s); any value is
            clamped to ``[0, MAX_TIME_LIMIT]`` (7200s) before being passed
            to the solver's native time-limit parameter (Acceptance
            Criterion 8).
        threads: Caller-requested thread cap, applied via the solver's
            native thread parameter. ``None`` or non-positive defaults to 1.

    Returns:
        A dict with exactly the keys ``status``, ``objective_value``,
        ``variables``, ``solver``, ``solve_time_s``, ``error`` -- see the
        module docstring for the full contract. Never raises: any
        unexpected failure (bad solver name, unreadable LP file, solver
        exception) comes back as ``status == "error"`` with a populated
        ``error`` string, per the "never a silent optimal" requirement.
    """
    resolved_solver = (solver or "gurobi").strip().lower()

    if resolved_solver not in _BACKENDS:
        return _error_result(
            resolved_solver,
            0.0,
            f"unsupported solver {solver!r}; expected 'gurobi' or 'cplex'",
        )

    try:
        resolved_time_limit = _resolve_time_limit(time_limit)
        resolved_threads = _resolve_threads(threads)
        backend_fn = _BACKENDS[resolved_solver]
        result = backend_fn(lp_path, resolved_time_limit, resolved_threads)
    except Exception as exc:  # ultimate safety net -- should be unreachable
        return _error_result(resolved_solver, 0.0, f"{type(exc).__name__}: {exc}")

    # Defensive: guarantee the exact external schema regardless of what a
    # backend returned (S1).
    return {key: result.get(key) for key in _RESULT_KEYS}
