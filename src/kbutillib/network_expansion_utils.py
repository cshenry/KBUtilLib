"""Unified network-expansion facade with backend dispatch.

:class:`NetworkExpansionUtils` is the single entry point for *in-silico reaction
prediction / metabolic network expansion* across several backends:

* ``pickaxe``    — Tyo/Henry-lab ``minedatabase`` (Pickaxe) SMARTS-operator
  expansion (optional dep + bundled rule data).
* ``retrorules`` — RetroRules SMARTS operators applied with RDKit (optional dep
  + a RetroRules flat-rules TSV on disk).

Dispatch model
--------------
* ``backend="name"`` targets a single backend explicitly.
* ``backend=None`` (default) walks a priority order and returns the first
  backend that produces a non-empty expansion, recording in ``warnings`` which
  backends were skipped/failed. The default order favors Pickaxe (a full
  network-expansion tool) and falls back to RetroRules:
  ``pickaxe -> retrorules``.

Nothing here ever fabricates a reaction or a compound: if no backend can expand,
the returned :class:`ExpansionResult` is empty with explanatory warnings.

This module follows KBUtilLib's composition convention: the
:class:`NetworkExpansionUtils` class is for standalone use, while
:class:`NetworkExpansionUtilsImpl` is the composition wrapper registered on the
:class:`kbutillib.toolkit.KBUtilLib` toolkit.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from .cheminformatics import (
    ExpansionResult,
    PickaxeBackend,
    RetroRulesBackend,
)
from .cheminformatics.base import BackendUnavailableError
from .shared_env_utils import SharedEnvUtils

#: Default priority order for network-expansion dispatch.
DEFAULT_EXPANSION_ORDER: Sequence[str] = (
    "pickaxe",
    "retrorules",
)


class NetworkExpansionUtils(SharedEnvUtils):
    """Backend-dispatching cheminformatics network-expansion utilities.

    Args:
        **kwargs: Passed to :class:`SharedEnvUtils`.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._backends: Optional[Dict[str, Any]] = None

    # ── backend wiring ──────────────────────────────────────────────────

    def _build_backends(self) -> Dict[str, Any]:
        """Instantiate all backends (cheap; heavy deps stay deferred)."""
        return {
            "pickaxe": PickaxeBackend(
                config_resolver=self.get_config_value, logger=self
            ),
            "retrorules": RetroRulesBackend(
                config_resolver=self.get_config_value, logger=self
            ),
        }

    @property
    def backends(self) -> Dict[str, Any]:
        """Mapping of backend name -> backend instance (lazily built)."""
        if self._backends is None:
            self._backends = self._build_backends()
        return self._backends

    def get_backend(self, name: str) -> Any:
        """Return a backend by name.

        Args:
            name: Backend identifier.

        Returns:
            The backend instance.

        Raises:
            KeyError: If no backend with that name is registered.
        """
        try:
            return self.backends[name]
        except KeyError:
            raise KeyError(
                f"Unknown expansion backend '{name}'. "
                f"Available: {sorted(self.backends)}"
            )

    def backend_status(self) -> Dict[str, Dict[str, Any]]:
        """Return availability + capabilities for every backend.

        Useful for diagnostics and for tests that assert graceful degradation.

        Returns:
            ``{name: {"available": bool, "reason": str|None,
            "capabilities": [str, ...]}}``.
        """
        status: Dict[str, Dict[str, Any]] = {}
        for name, backend in self.backends.items():
            status[name] = {
                "available": bool(backend.available),
                "reason": backend.unavailable_reason,
                "capabilities": sorted(backend.capabilities),
            }
        return status

    def _resolve_order(
        self, backend: Optional[str], default: Sequence[str]
    ) -> List[str]:
        if backend is not None:
            return [backend]
        return list(default)

    # ── expansion ───────────────────────────────────────────────────────

    def expand(
        self,
        seed_smiles: Mapping[str, str],
        generations: int = 1,
        backend: Optional[str] = None,
        **kwargs: Any,
    ) -> ExpansionResult:
        """Expand a seed compound set into a predicted reaction network.

        Args:
            seed_smiles: Mapping of compound id -> SMILES for the seed set.
            generations: Number of expansion rounds to perform.
            backend: Force a single backend by name, or ``None`` to walk the
                default priority order (``pickaxe -> retrorules``).
            **kwargs: Forwarded to the backend (rule set selection, diameter,
                direction, processes, etc.).

        Returns:
            An :class:`ExpansionResult`. If no backend produces a non-empty
            expansion, the result is empty with warnings listing what was tried.
        """
        order = self._resolve_order(backend, DEFAULT_EXPANSION_ORDER)
        attempted_warnings: List[str] = []
        last: Optional[ExpansionResult] = None

        for name in order:
            be = self.backends.get(name)
            if be is None:
                attempted_warnings.append(f"[{name}] not registered")
                continue
            if "expand" not in be.capabilities:
                attempted_warnings.append(f"[{name}] does not provide expansion")
                continue
            if not be.available:
                attempted_warnings.append(
                    f"[{name}] unavailable: {be.unavailable_reason}"
                )
                continue
            try:
                result = be.expand(seed_smiles, generations=generations, **kwargs)
            except BackendUnavailableError as exc:
                attempted_warnings.append(f"[{name}] unavailable: {exc}")
                continue
            except Exception as exc:  # defensive: never crash the facade
                attempted_warnings.append(
                    f"[{name}] errored: {type(exc).__name__}: {exc}"
                )
                continue
            # Prepend the dispatch trace to the backend's own warnings.
            result.warnings = attempted_warnings + result.warnings
            last = result
            if result.is_expanded:
                return result
            # Non-empty seeds but no reactions: keep trying lower-priority
            # backends, but remember this result in case all come back empty.
            attempted_warnings.append(
                f"[{name}] produced no reactions; trying next backend"
            )

        if last is not None:
            return last

        empty = ExpansionResult(backend=backend or "none", generations=generations)
        empty.warnings = attempted_warnings or ["no expansion backend available"]
        return empty


class NetworkExpansionUtilsImpl:
    """Composition wrapper for :class:`NetworkExpansionUtils`.

    Holds ``env`` instead of inheriting from :class:`SharedEnvUtils`, matching
    the pattern used by :class:`kbutillib.thermo_utils.ThermoUtilsImpl`.
    Delegates all other attribute access to an internal
    :class:`NetworkExpansionUtils` instance.

    Args:
        env: A :class:`SharedEnvUtils` instance.
        **kwargs: Forwarded to the internal ``NetworkExpansionUtils``.
    """

    def __init__(self, env: Any, **kwargs: Any) -> None:
        self._env = env
        _kwargs = {
            "config_file": False,
            "token_file": None,
            "kbase_token_file": None,
        }
        _kwargs.update(kwargs)
        self._delegate = NetworkExpansionUtils(**_kwargs)

    @property
    def env(self) -> Any:
        return self._env

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)
