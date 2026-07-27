"""Thin ``remote_solve`` helper for submitting a built cobra/optlang problem
to the Remote LP-Solver service.

Per ``agent-io/prds/tmfa-weighted-concfit/fullprompt.md`` (Module 4, Confront
resolutions #7-#9/#11, Acceptance Criteria 11-15, all in the ModelSEEDpy
repo's ``agent-io/``), this module owns exactly the glue between an
in-memory cobra/optlang model and the existing
:meth:`~kbutillib.domains.modeling.ms_remote_solver_utils.MSRemoteSolverUtils.solve_lp`
client:

1. **Guard** -- a quadratic objective requires a QP-capable backend
   (gurobi/cplex; GLPK cannot hold one at all). A purely linear model may
   use any backend whose problem writes a valid LP, including GLPK.
2. **Serialize** -- the built problem to LP text via a temp ``.lp`` file,
   unlinked in a ``finally`` block. LP format is name-based, so
   ModelSEEDpy/cobra variable names survive the round-trip with no
   MPS-style truncation.
3. **Solve** -- delegates, unchanged, to ``solve_lp``.
4. **Wrap** -- returns a :class:`RemoteSolveResult`.

``remote_solve`` never modifies ``ms_remote_solver_utils.solve_lp`` or the
remote service; it only calls them. Per Confront resolution #9 / Acceptance
Criterion 13, this helper lives only in KBUtilLib -- ModelSEEDpy (or any
other caller) never imports it in the other direction.

Honest limitation: because this never optimizes the local model (the
remote service does the actual solve), the local optlang variables'
``.primal`` is **not** populated by this call. Read fitted values via
``RemoteSolveResult.value(...)``, not ``variable.primal``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Union

__all__ = [
    "RemoteSolveResult",
    "remote_solve",
    "QP_CAPABLE_MODULE_PREFIXES",
]

# Accept iff the model's optlang backend module name starts with one of
# these (Confront resolution #3 / Acceptance Criteria 5 & 11). GLPK, SCIPY,
# and the "hybrid" (OSQP-routed) interface are rejected for a quadratic
# objective.
QP_CAPABLE_MODULE_PREFIXES = ("optlang.gurobi_interface", "optlang.cplex_interface")


class RemoteSolveResult:
    """Wraps the result dict returned by ``MSRemoteSolverUtils.solve_lp``.

    Exposes ``.status``, ``.objective_value``, ``.values`` (name -> float,
    the raw ``variables`` payload verbatim), the raw payload (``.raw``),
    and a convenience ``.value(var_or_name)`` lookup.

    Honest limitation: this object never touches the local optlang model,
    so the local model's variables' ``.primal`` is NOT populated by a
    ``remote_solve`` call -- read fitted values via ``result.value(...)``,
    not ``variable.primal``.
    """

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.raw = payload
        self.status = payload.get("status")
        self.objective_value = payload.get("objective_value")
        self.values: Dict[str, float] = dict(payload.get("variables") or {})
        self.solver = payload.get("solver")
        self.solve_time_s = payload.get("solve_time_s")
        self.error = payload.get("error")

    def value(self, var_or_name: Union[str, Any]) -> float:
        """Return the solved value for an optlang ``Variable`` or a name.

        Args:
            var_or_name: An optlang ``Variable`` (its ``.name`` is used) or
                a plain string variable name.

        Returns:
            ``self.values[name]``.
        """
        name = var_or_name if isinstance(var_or_name, str) else var_or_name.name
        return self.values[name]

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"RemoteSolveResult(status={self.status!r}, "
            f"objective_value={self.objective_value!r}, "
            f"n_values={len(self.values)})"
        )


def _backend_module_name(model: Any) -> str:
    """Best-effort optlang backend module name for ``model.solver``.

    Real optlang models expose ``model.solver.interface`` as the backend
    *module* (e.g. ``optlang.gurobi_interface``), which has no
    ``__module__`` attribute of its own -- so this falls back to
    ``type(model.solver).__module__``, which reliably resolves to the same
    string. The primary lookup is kept first so a test double that sets
    ``interface.__module__`` directly (as the Confront-resolution wording
    literally describes) is also honored.
    """
    interface = getattr(model.solver, "interface", None)
    module_name = getattr(interface, "__module__", None)
    if not module_name:
        module_name = type(model.solver).__module__
    return module_name or ""


def _is_qp_capable_backend(model: Any) -> bool:
    """True iff the model's backend is gurobi or cplex."""
    module_name = _backend_module_name(model)
    return any(module_name.startswith(prefix) for prefix in QP_CAPABLE_MODULE_PREFIXES)


def _has_quadratic_objective(model: Any) -> bool:
    """Best-effort detection of a quadratic (degree > 1) objective."""
    try:
        expression = model.solver.objective.expression
    except AttributeError:
        return False
    if expression is None:
        return False

    try:
        import sympy

        expanded = sympy.expand(expression)
        free_symbols = expanded.free_symbols
        if not free_symbols:
            return False
        degree = sympy.Poly(expanded, *free_symbols).total_degree()
        return degree > 1
    except Exception:
        # Fallback: sympy/optlang render squared terms literally as "**2".
        return "**2" in str(expression)


def _serialize_to_lp(model: Any) -> str:
    """Write ``model``'s built problem to a temp ``.lp`` file and return its text.

    Per Confront resolution #7 / Acceptance Criterion 12: serialize via
    ``tempfile.NamedTemporaryFile(delete=False, suffix=".lp")``, read the
    contents, then unlink the file in a ``finally`` block.

    Gurobi/cplex expose a native ``problem.write(path)`` that preserves the
    quadratic-objective and binary sections natively. GLPK's ``.problem``
    is a raw SWIG pointer with no Python ``.write`` method, so for backends
    without a native writer this falls back to optlang's own
    backend-agnostic ``model.solver.to_lp()`` (used only for the purely
    linear case the QP guard below already restricts non-QP backends to).
    """
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".lp")
    tmp_path = tmp.name
    tmp.close()
    try:
        problem = model.solver.problem
        if hasattr(problem, "write"):
            update = getattr(problem, "update", None)
            if callable(update):
                update()
            problem.write(tmp_path)
        else:
            Path(tmp_path).write_text(model.solver.to_lp())
        return Path(tmp_path).read_text()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def remote_solve(
    remote_solver: Any,
    model: Any,
    solver: Optional[str] = None,
    time_limit: Optional[float] = None,
) -> RemoteSolveResult:
    """Serialize ``model`` to LP and solve it via the Remote LP-Solver service.

    Does not modify ``model``, ``MSRemoteSolverUtils.solve_lp``, or the
    remote service -- it only serializes the already-built problem and
    calls the existing client.

    Args:
        remote_solver: An ``MSRemoteSolverUtils``-shaped client (or its
            ``MSRemoteSolverUtilsImpl`` composition wrapper), exposing
            ``solve_lp(lp_text, solver=..., time_limit=...)``.
        model: A cobra/optlang model whose ``model.solver.problem`` has
            already been built (e.g. via ``FullThermoPkg.build_package``).
        solver: ``"gurobi"`` / ``"cplex"`` / ``None`` (service default,
            currently Gurobi).
        time_limit: Solver time limit in seconds, forwarded verbatim.

    Returns:
        A :class:`RemoteSolveResult` wrapping the service's response.
        ``.primal`` on ``model``'s own optlang variables is NOT populated
        by this call -- read fitted values via ``result.value(...)``.

    Raises:
        NotImplementedError: If ``model``'s objective is quadratic and the
            backend is not gurobi/cplex (GLPK cannot hold a quadratic
            objective at all; other non-QP-capable backends are rejected
            the same way).
    """
    if _has_quadratic_objective(model) and not _is_qp_capable_backend(model):
        raise NotImplementedError(
            "remote_solve: a quadratic objective requires a QP-capable "
            "solver backend (gurobi/cplex) -- set model.solver to 'gurobi' "
            "or 'cplex' before building the objective/calling remote_solve "
            "(GLPK cannot hold a quadratic objective)"
        )

    lp_text = _serialize_to_lp(model)
    payload = remote_solver.solve_lp(lp_text, solver=solver, time_limit=time_limit)
    return RemoteSolveResult(payload)
