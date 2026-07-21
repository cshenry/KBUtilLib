"""Behavioral tests for kbutillib.services.lp_solver.solver_backends.

All tests feed small, hand-built LP-format problems with mathematically
known outcomes straight to ``solve()`` -- no cobra/optlang model
generation involved (per the PRD, the service is solver-only and never
imports cobra).

Correctness tests that actually invoke Gurobi/CPLEX are gated behind
``pytest.importorskip`` and skip with a note when the corresponding
package isn't installed (fullprompt.md S14): this dev environment has
neither ``gurobipy`` nor ``cplex`` (both only live on H100), so on this
machine every solver-specific case below is expected to *skip*, not
fail. The dispatch-layer tests (unsupported-solver / schema shape) need
no solver and always run.

Test inventory (mirrors fullprompt.md "Testing Decisions" + Acceptance
Criteria 1-9):
  T1  known-optimal LP -> status "optimal", correct objective_value, and
      `variables` keyed by the exact submitted variable name (name
      round-trip). Parametrized over gurobi/cplex.
  T2  known-infeasible LP -> status "infeasible" (never silently retried
      on the other solver -- each param only ever invokes its own
      backend). Parametrized over gurobi/cplex.
  T3  known-unbounded LP -> status "unbounded". Parametrized over
      gurobi/cplex.
  T4  a moderately-hard 0/1 knapsack MILP with a near-zero time_limit ->
      status "timeout"; when a feasible incumbent exists, all variables
      and the incumbent objective are returned, else None/{}.
      Parametrized over gurobi/cplex.
  T5  Gurobi INF_OR_UNBD disambiguation: a classic free-variable/
      equality-constraint LP that Gurobi's default presolve reports as
      the ambiguous INF_OR_UNBD (status 4) resolves, via the in-place
      DualReductions=0 re-solve, to a definitive "unbounded" -- never
      "error" -- with solve_time_s reflecting both runs. Gurobi-only
      (CPLEX has no INF_OR_UNBD quirk, per the PRD).
  T6  an unsupported/unrecognized solver name -> status "error" with a
      populated `error` string, never a silent "optimal". Runs
      unconditionally (no solver package required) since it exercises
      the dispatch layer, not a backend.
  T7  a nonexistent LP file path -> status "error" with a populated
      `error` string (exception-safety net). Parametrized over
      gurobi/cplex.
  T8  the returned dict always has exactly the six external-schema keys
      and no others, on both the solver-backed and dispatch-error paths.
"""

from __future__ import annotations

import random

import pytest

from kbutillib.services.lp_solver.solver_backends import solve

RESULT_KEYS = {
    "status",
    "objective_value",
    "variables",
    "solver",
    "solve_time_s",
    "error",
}

SOLVERS = ["gurobi", "cplex"]


def _require_solver(name: str) -> None:
    """Skip (with a note) unless the given solver package is importable (S14)."""
    if name == "gurobi":
        pytest.importorskip(
            "gurobipy", reason="gurobipy not installed in this environment"
        )
    elif name == "cplex":
        pytest.importorskip(
            "cplex", reason="native cplex package not installed in this environment"
        )
    else:  # pragma: no cover - guards test authoring mistakes
        raise ValueError(f"unknown solver fixture name: {name!r}")


def _assert_schema(result: dict) -> None:
    assert set(result.keys()) == RESULT_KEYS


# ---------------------------------------------------------------------------
# LP fixtures with known outcomes
# ---------------------------------------------------------------------------

# Maximize x s.t. x <= 5 (default variable lower bound is 0) -> optimal x=5.
_OPTIMAL_LP = "Maximize\n obj: x\nSubject To\n c1: x <= 5\nEnd\n"
_OPTIMAL_VAR = "x"
_OPTIMAL_OBJ = 5.0

# x >= 1 and x <= 0 simultaneously -> infeasible.
_INFEASIBLE_LP = "Minimize\n obj: x\nSubject To\n c1: x >= 1\n c2: x <= 0\nEnd\n"

# Maximize x with only a (redundant) lower bound and no upper bound -> unbounded.
_UNBOUNDED_LP = "Maximize\n obj: x\nSubject To\n c1: x >= 0\nEnd\n"

# Classic Gurobi INF_OR_UNBD trigger: two free variables tied together by a
# single equality constraint. Minimizing x is genuinely unbounded (set
# y = 5 - x for any x), but Gurobi's default presolve/dual-reductions report
# this ambiguously as INF_OR_UNBD (status 4) rather than a definitive answer.
_INF_OR_UNBD_LP = "Minimize\n obj: x\nSubject To\n c1: x + y = 5\nFree\n x\n y\nEnd\n"


def _knapsack_lp_text(n: int = 30, seed: int = 42) -> tuple[str, list[str]]:
    """A moderately-hard 0/1 knapsack MILP (weight ~ profit -> nontrivial B&B).

    Returns the LP text plus the ordered list of variable names it declares,
    so tests can check the returned `variables` dict against the full set.
    """
    rng = random.Random(seed)
    profits = [rng.randint(50, 100) for _ in range(n)]
    weights = [max(1, p + rng.randint(-5, 5)) for p in profits]
    capacity = sum(weights) // 2
    names = [f"x{i}" for i in range(n)]

    obj_terms = " + ".join(f"{profits[i]} {names[i]}" for i in range(n))
    weight_terms = " + ".join(f"{weights[i]} {names[i]}" for i in range(n))
    binaries = " ".join(names)

    lp_text = (
        "Maximize\n"
        f" obj: {obj_terms}\n"
        "Subject To\n"
        f" cap: {weight_terms} <= {capacity}\n"
        "Binary\n"
        f" {binaries}\n"
        "End\n"
    )
    return lp_text, names


# ---------------------------------------------------------------------------
# T1 - known optimal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_known_optimal_lp(solver_name, tmp_path):
    _require_solver(solver_name)
    lp_path = tmp_path / "optimal.lp"
    lp_path.write_text(_OPTIMAL_LP)

    result = solve(str(lp_path), solver=solver_name, time_limit=30, threads=1)

    _assert_schema(result)
    assert result["status"] == "optimal"
    assert result["solver"] == solver_name
    assert result["error"] is None
    assert result["objective_value"] == pytest.approx(_OPTIMAL_OBJ)
    # Name round-trip: keys are byte-identical to the LP's own variable name.
    assert set(result["variables"].keys()) == {_OPTIMAL_VAR}
    assert result["variables"][_OPTIMAL_VAR] == pytest.approx(_OPTIMAL_OBJ)
    assert result["solve_time_s"] >= 0.0


# ---------------------------------------------------------------------------
# T2 - known infeasible
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_known_infeasible_lp(solver_name, tmp_path):
    _require_solver(solver_name)
    lp_path = tmp_path / "infeasible.lp"
    lp_path.write_text(_INFEASIBLE_LP)

    result = solve(str(lp_path), solver=solver_name, time_limit=30, threads=1)

    _assert_schema(result)
    assert result["status"] == "infeasible"
    assert result["solver"] == solver_name
    assert result["error"] is None
    assert result["objective_value"] is None
    assert result["variables"] == {}


# ---------------------------------------------------------------------------
# T3 - known unbounded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_known_unbounded_lp(solver_name, tmp_path):
    _require_solver(solver_name)
    lp_path = tmp_path / "unbounded.lp"
    lp_path.write_text(_UNBOUNDED_LP)

    result = solve(str(lp_path), solver=solver_name, time_limit=30, threads=1)

    _assert_schema(result)
    assert result["status"] == "unbounded"
    assert result["solver"] == solver_name
    assert result["error"] is None
    assert result["objective_value"] is None
    assert result["variables"] == {}


# ---------------------------------------------------------------------------
# T4 - timeout with/without incumbent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_near_zero_time_limit_times_out(solver_name, tmp_path):
    _require_solver(solver_name)
    lp_text, names = _knapsack_lp_text()
    lp_path = tmp_path / "knapsack.lp"
    lp_path.write_text(lp_text)

    result = solve(str(lp_path), solver=solver_name, time_limit=1e-6, threads=1)

    _assert_schema(result)
    assert result["status"] == "timeout"
    assert result["solver"] == solver_name
    assert result["error"] is None
    # S2: either a full incumbent (all variables + its objective) or,
    # if genuinely no incumbent was found in time, None/{} -- never a
    # partial variables dict.
    if result["objective_value"] is not None:
        assert set(result["variables"].keys()) == set(names)
    else:
        assert result["variables"] == {}


# ---------------------------------------------------------------------------
# T5 - Gurobi INF_OR_UNBD disambiguation (Gurobi-only; CPLEX has no such quirk)
# ---------------------------------------------------------------------------


def test_gurobi_inf_or_unbd_disambiguates_to_unbounded(tmp_path):
    _require_solver("gurobi")
    lp_path = tmp_path / "inf_or_unbd.lp"
    lp_path.write_text(_INF_OR_UNBD_LP)

    result = solve(str(lp_path), solver="gurobi", time_limit=30, threads=1)

    _assert_schema(result)
    # Mathematically this LP is unbounded (x -> -infinity, y = 5 - x free).
    # The point of this test is that Gurobi's ambiguous INF_OR_UNBD (status 4)
    # never leaks through as "error" -- the in-place DualReductions=0 re-solve
    # must produce this definitive answer.
    assert result["status"] == "unbounded"
    assert result["solver"] == "gurobi"
    assert result["error"] is None
    assert result["solve_time_s"] >= 0.0


# ---------------------------------------------------------------------------
# T6 - unsupported solver name -> error (no solver package required)
# ---------------------------------------------------------------------------


def test_unsupported_solver_name_returns_error(tmp_path):
    lp_path = tmp_path / "optimal.lp"
    lp_path.write_text(_OPTIMAL_LP)

    result = solve(str(lp_path), solver="not-a-real-solver", time_limit=5, threads=1)

    _assert_schema(result)
    assert result["status"] == "error"
    assert result["error"]
    assert result["objective_value"] is None
    assert result["variables"] == {}


# ---------------------------------------------------------------------------
# T7 - nonexistent LP file -> error (exception safety net)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_missing_lp_file_returns_error(solver_name, tmp_path):
    _require_solver(solver_name)
    missing_path = str(tmp_path / "does_not_exist.lp")

    result = solve(missing_path, solver=solver_name, time_limit=5, threads=1)

    _assert_schema(result)
    assert result["status"] == "error"
    assert result["solver"] == solver_name
    assert result["error"]
    assert result["objective_value"] is None
    assert result["variables"] == {}


# ---------------------------------------------------------------------------
# T8 - schema shape holds on every path (redundant-but-explicit sanity check)
# ---------------------------------------------------------------------------


def test_result_schema_has_exactly_six_keys(tmp_path):
    lp_path = tmp_path / "optimal.lp"
    lp_path.write_text(_OPTIMAL_LP)

    result = solve(str(lp_path), solver="not-a-real-solver", time_limit=5, threads=1)

    assert set(result.keys()) == RESULT_KEYS
    assert len(result) == 6
