"""Targeted branch-coverage tests (P23) for kbutillib.toolkit.

Covers lazy-property caching (``standardize``, ``pdb``), the ``remote_solve``
thin-wrapper delegation, and the ``_missing_dependency_error`` hint builder.
All tests patch the collaborator at its *source* module (the lazy properties
use function-local imports), so no optional/heavy dependency is required.
"""

from __future__ import annotations

from typing import Any

import pytest

from kbutillib import KBUtilLib
from kbutillib.toolkit import _missing_dependency_error


class _CountingFake:
    """A fake replacement for a lazily-constructed backend that records how
    many times it was instantiated."""

    instantiations = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        type(self).instantiations += 1
        self.args = args
        self.kwargs = kwargs


class TestStandardizeLazyCaching:
    """toolkit.py:193-200 — ``standardize`` builds once and caches."""

    def test_standardize_is_cached_across_accesses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kbutillib.domains.modeling.model_standardization_utils as mod

        _CountingFake.instantiations = 0
        monkeypatch.setattr(mod, "ModelStandardizationUtilsImpl", _CountingFake)

        app = KBUtilLib()
        first = app.standardize
        second = app.standardize

        assert isinstance(first, _CountingFake)
        assert first is second
        assert _CountingFake.instantiations == 1


class TestPdbLazyCaching:
    """toolkit.py:355-359 — ``pdb`` builds once and caches.

    ``rcsb_pdb_utils`` imports ``aiohttp`` at module scope, which is not
    installed in this environment. Rather than importing the real module
    (which would fail before we could patch it), inject a stand-in module
    into ``sys.modules`` under its dotted name so ``toolkit.py``'s local
    ``from .domains.external.rcsb_pdb_utils import RCSBPDBUtilsImpl`` finds
    our fake without ever touching the real file or requiring aiohttp.
    """

    def test_pdb_is_cached_across_accesses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys
        import types

        _CountingFake.instantiations = 0
        fake_mod = types.ModuleType("kbutillib.domains.external.rcsb_pdb_utils")
        fake_mod.RCSBPDBUtilsImpl = _CountingFake
        monkeypatch.setitem(
            sys.modules, "kbutillib.domains.external.rcsb_pdb_utils", fake_mod
        )

        app = KBUtilLib()
        first = app.pdb
        second = app.pdb

        assert isinstance(first, _CountingFake)
        assert first is second
        assert _CountingFake.instantiations == 1


class TestRemoteSolveDelegation:
    """toolkit.py:306-338 — ``remote_solve`` is a thin wrapper around the
    module-level ``remote_solve`` helper, called with ``.remote_solver``."""

    def test_remote_solve_delegates_with_expected_args(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kbutillib.domains.modeling.ms_remote_solve_utils as mod

        calls = []
        sentinel = object()

        def fake_remote_solve(
            remote_solver: Any, model: Any, solver: Any = None, time_limit: Any = None
        ) -> Any:
            calls.append((remote_solver, model, solver, time_limit))
            return sentinel

        monkeypatch.setattr(mod, "remote_solve", fake_remote_solve)

        app = KBUtilLib()
        model_obj = object()
        result = app.remote_solve(model_obj, solver="gurobi", time_limit=30)

        assert result is sentinel
        assert len(calls) == 1
        remote_solver_arg, model_arg, solver_arg, time_limit_arg = calls[0]
        assert remote_solver_arg is app.remote_solver
        assert model_arg is model_obj
        assert solver_arg == "gurobi"
        assert time_limit_arg == 30


class TestMissingDependencyError:
    """toolkit.py:451-465 — extra-specific hint vs generic ``[all]`` hint."""

    def test_known_module_maps_to_specific_extra_hint(self) -> None:
        exc = ModuleNotFoundError("No module named 'rdkit'", name="rdkit")
        result = _missing_dependency_error("some_prop", exc)
        assert isinstance(result, ImportError)
        assert "reaction_similarity" in str(result)

    def test_unknown_missing_module_falls_back_to_all_extra(self) -> None:
        exc = ModuleNotFoundError("mystery")
        assert exc.name is None
        result = _missing_dependency_error("some_prop", exc)
        assert isinstance(result, ImportError)
        assert "KBUtilLib[all]" in str(result)
