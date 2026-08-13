"""Unit tests for the `remote_solve` helper (ms_remote_solve_utils.py).

Per ``ModelSEEDpy/agent-io/prds/tmfa-weighted-concfit/fullprompt.md``
(Module 4, "Testing Decisions" / Acceptance Criteria 11-15): mock
``solve_lp`` so these tests carry no live-server dependency, assert
``RemoteSolveResult`` mapping (`.value`/`.objective_value`/`.status`
pass-through), assert the non-QP-backend guard rejects a quadratic
objective, and mark the live linear round-trip test skipped unless the
remote-solver config/URL is explicitly enabled.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kbutillib.domains.modeling.ms_remote_solve_utils import (
    QP_CAPABLE_MODULE_PREFIXES,
    RemoteSolveResult,
    remote_solve,
)

_TERMINAL_PAYLOAD = {
    "status": "optimal",
    "objective_value": 42.0,
    "variables": {"x": 1.5, "y": -2.25},
    "solver": "gurobi",
    "solve_time_s": 0.05,
    "error": None,
}


# ── RemoteSolveResult mapping (Acceptance Criteria 12, 14) ───────────────


class _FakeVariable:
    """Stand-in for an optlang Variable -- only `.name` is used by `.value`."""

    def __init__(self, name: str) -> None:
        self.name = name


def test_remote_solve_result_passthrough_and_value_lookup():
    result = RemoteSolveResult(_TERMINAL_PAYLOAD)

    assert result.status == "optimal"
    assert result.objective_value == 42.0
    assert result.solver == "gurobi"
    assert result.solve_time_s == 0.05
    assert result.error is None
    assert result.values == {"x": 1.5, "y": -2.25}
    assert result.raw == _TERMINAL_PAYLOAD

    # String lookup.
    assert result.value("x") == 1.5
    # optlang-Variable-shaped lookup (`.value` reads `.name`, not identity).
    assert result.value(_FakeVariable("y")) == -2.25


def test_remote_solve_result_value_missing_raises_keyerror():
    result = RemoteSolveResult(_TERMINAL_PAYLOAD)
    with pytest.raises(KeyError):
        result.value("not_a_variable")


# ── Guard: quadratic objective requires gurobi/cplex (AC 11, 14) ─────────


def _fake_model(*, backend_module: str, expression, problem=None):
    """Build a minimal fake `model.solver.{interface,objective,problem}`.

    `interface.__module__` is set directly on a plain instance, matching
    the Confront-resolution wording (`model.solver.interface.__module__`)
    literally, per `_backend_module_name`'s primary lookup.
    """
    interface = SimpleNamespace()
    interface.__module__ = backend_module
    solver = SimpleNamespace(
        interface=interface,
        objective=SimpleNamespace(expression=expression),
        problem=problem if problem is not None else Mock(),
    )
    return SimpleNamespace(solver=solver)


def test_remote_solve_guard_rejects_quadratic_on_non_qp_backend():
    sympy = pytest.importorskip("sympy", reason="sympy required to build a quadratic objective")

    x = sympy.Symbol("x")
    quadratic_expression = x**2 + 1

    model = _fake_model(
        backend_module="optlang.glpk_interface",
        expression=quadratic_expression,
    )
    remote_solver = Mock()

    with pytest.raises(NotImplementedError, match="QP-capable"):
        remote_solve(remote_solver, model)

    # The guard must fire before ever contacting the remote service.
    remote_solver.solve_lp.assert_not_called()


@pytest.mark.parametrize("qp_module", QP_CAPABLE_MODULE_PREFIXES)
def test_remote_solve_accepts_quadratic_on_qp_capable_backend(qp_module):
    sympy = pytest.importorskip("sympy", reason="sympy required to build a quadratic objective")

    x = sympy.Symbol("x")
    quadratic_expression = x**2 + 1

    fake_problem = Mock()

    def _write(path):
        with open(path, "w") as fh:
            fh.write("\\* fake quadratic LP *\\\nEnd\n")

    fake_problem.write.side_effect = _write

    model = _fake_model(
        backend_module=qp_module,
        expression=quadratic_expression,
        problem=fake_problem,
    )
    remote_solver = Mock()
    remote_solver.solve_lp.return_value = dict(_TERMINAL_PAYLOAD)

    result = remote_solve(remote_solver, model, solver="cplex", time_limit=60)

    assert isinstance(result, RemoteSolveResult)
    assert result.status == "optimal"
    remote_solver.solve_lp.assert_called_once()
    called_args, called_kwargs = remote_solver.solve_lp.call_args
    assert "fake quadratic LP" in called_args[0]
    assert called_kwargs == {"solver": "cplex", "time_limit": 60}
    fake_problem.write.assert_called_once()


def test_remote_solve_linear_objective_ok_on_non_qp_backend():
    """A purely linear objective may use any backend (incl. GLPK) -- AC 11."""
    sympy = pytest.importorskip("sympy", reason="sympy required to build a quadratic objective")

    x = sympy.Symbol("x")
    linear_expression = 2 * x + 1

    fake_problem = Mock()

    def _write(path):
        with open(path, "w") as fh:
            fh.write("\\* fake linear LP *\\\nEnd\n")

    fake_problem.write.side_effect = _write

    model = _fake_model(
        backend_module="optlang.glpk_interface",
        expression=linear_expression,
        problem=fake_problem,
    )
    remote_solver = Mock()
    remote_solver.solve_lp.return_value = dict(_TERMINAL_PAYLOAD)

    result = remote_solve(remote_solver, model)

    assert result.status == "optimal"
    remote_solver.solve_lp.assert_called_once()


# ── Temp-file lifecycle (Confront resolution #7 / Acceptance Criterion 12) ──


def test_remote_solve_removes_temp_lp_file_after_read():
    sympy = pytest.importorskip("sympy", reason="sympy required to build a quadratic objective")

    written_paths = []

    def _write(path):
        written_paths.append(path)
        with open(path, "w") as fh:
            fh.write("\\* fake LP *\\\nEnd\n")

    fake_problem = Mock()
    fake_problem.write.side_effect = _write

    model = _fake_model(
        backend_module="optlang.glpk_interface",
        expression=sympy.Symbol("x"),
        problem=fake_problem,
    )
    remote_solver = Mock()
    remote_solver.solve_lp.return_value = dict(_TERMINAL_PAYLOAD)

    remote_solve(remote_solver, model)

    assert len(written_paths) == 1
    assert not os.path.exists(written_paths[0]), (
        "temp .lp file must be unlinked after being read"
    )


# ── Deterministic linear fixture against a real GLPK-backed cobra model ──


def _build_deterministic_linear_model():
    """A tiny, hand-solvable linear cobra/GLPK model.

    R1: -> A (0..10), R2: A -> (0..10), objective max R2. Mass balance
    (A: R1 - R2 = 0) forces R1 == R2 at optimum, so the analytic optimum
    is R1 = R2 = 10, objective = 10.
    """
    cobra = pytest.importorskip("cobra")

    model = cobra.Model("deterministic_linear_fixture")
    model.solver = "glpk"
    met_a = cobra.Metabolite("A")
    r1 = cobra.Reaction("R1")
    r1.lower_bound = 0
    r1.upper_bound = 10
    r1.add_metabolites({met_a: -1})
    r2 = cobra.Reaction("R2")
    r2.lower_bound = 0
    r2.upper_bound = 10
    r2.add_metabolites({met_a: 1})
    model.add_reactions([r1, r2])
    model.objective = "R2"
    return model


def test_remote_solve_serializes_real_glpk_model_and_wraps_deterministic_result():
    """AC 15: a deterministic linear fixture backs the unit-level assertion.

    Serializes a real GLPK-backed cobra model to LP (exercising the actual
    `_serialize_to_lp` temp-file code path, not a mock `.problem`), and
    checks the wrapped result against the fixture's known analytic optimum
    -- with `solve_lp` mocked, so no live server is required.
    """
    model = _build_deterministic_linear_model()
    r1 = model.reactions.get_by_id("R1")
    r2 = model.reactions.get_by_id("R2")

    remote_solver = Mock()
    remote_solver.solve_lp.return_value = {
        "status": "optimal",
        "objective_value": 10.0,
        "variables": {r1.id: 10.0, r2.id: 10.0},
        "solver": "glpk",
        "solve_time_s": 0.001,
        "error": None,
    }

    result = remote_solve(remote_solver, model, solver=None, time_limit=None)

    remote_solver.solve_lp.assert_called_once()
    lp_text_sent = remote_solver.solve_lp.call_args[0][0]
    assert "R1" in lp_text_sent and "R2" in lp_text_sent

    assert result.objective_value == 10.0
    assert result.value(model.solver.variables[r1.id]) == 10.0
    assert result.value(model.solver.variables[r2.id]) == 10.0


# ── Live round-trip (opt-in only; Acceptance Criterion 15) ───────────────


@pytest.mark.skipif(
    os.environ.get("KBUTILLIB_REMOTE_SOLVER_LIVE_TEST") != "1",
    reason=(
        "Live Remote LP-Solver round-trip is opt-in only -- set "
        "KBUTILLIB_REMOTE_SOLVER_LIVE_TEST=1 (and a reachable "
        "remote_solver.base_url in ~/.kbutillib/config.yaml) to run it."
    ),
)
def test_remote_solve_live_round_trip_matches_local_glpk_solve():
    """Opt-in integration smoke: remote-solve the deterministic fixture and
    compare against the local GLPK solve of the same model."""
    from kbutillib.domains.modeling.ms_remote_solver_utils import MSRemoteSolverUtils

    model = _build_deterministic_linear_model()
    local_solution = model.optimize()

    live_client = MSRemoteSolverUtils()
    result = remote_solve(live_client, model)

    assert result.status == "optimal"
    assert result.objective_value == pytest.approx(
        local_solution.objective_value, rel=1e-6
    )
