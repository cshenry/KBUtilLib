"""Unified predictive-thermodynamics facade with backend dispatch.

:class:`PredictiveThermoUtils` is the single entry point for estimating
reaction transformed Gibbs free energies (and, where supported, compound
formation energies and pKa / microspecies properties) across several backends:

* ``equilibrator`` — real component-contribution predictor (optional dep).
* ``modelseed_db`` — baked eQuilibrator deltaG from Andrew Freiburger's
  ModelSEED Database fork (~10.6k compounds / ~43k reactions, EQU-tagged;
  read directly from the TSVs, no dependency). Preferred within the ModelSEED
  biochemistry universe.
* ``modelseed``    — ModelSEED-DB lookups via the existing ThermoUtils (always
  available; no new dependency).
* ``dgpredictor``  — Maranas-lab reaction ΔG predictor (subprocess-isolated;
  configure via ``thermo.dgpredictor.repo_path`` / ``DGPREDICTOR_REPO``).
* ``molgpk``       — pKa / major-microspecies predictor via OPAM2
  (subprocess-isolated; configure via ``thermo.molgpk.repo_path`` /
  ``MOLGPK_REPO``).

Dispatch model
--------------
* ``backend="name"`` targets a single backend explicitly.
* ``backend=None`` (default) walks a priority order and returns the first
  backend that produces a numeric estimate, recording in ``warnings`` which
  backends were skipped/failed. The default order favors the rigorous
  predictor when present and falls back to database lookups:
  ``equilibrator -> dgpredictor -> modelseed``.

Nothing here ever fabricates a thermodynamic value: if no backend can produce
one, the returned estimate has ``dg_prime is None`` / ``dgf is None`` with
explanatory warnings.

This module follows KBUtilLib's composition convention: the
:class:`PredictiveThermoUtils` class is for standalone use, while
:class:`PredictiveThermoUtilsImpl` is the composition wrapper registered on the
:class:`kbutillib.toolkit.KBUtilLib` toolkit.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from .shared_env_utils import SharedEnvUtils
from .thermo_predictors import (
    CompoundThermoEstimate,
    DGPredictorBackend,
    EquilibratorBackend,
    ModelSEEDBackend,
    ModelSEEDDBBackend,
    MolGPKBackend,
    ReactionThermoEstimate,
)
from .thermo_predictors.base import BackendUnavailableError

#: Default priority order for reaction ΔG'° dispatch.
DEFAULT_REACTION_ORDER: Sequence[str] = (
    "equilibrator",
    "dgpredictor",
    "modelseed_db",
    "modelseed",
)
#: Default priority order for compound ΔGf dispatch.
DEFAULT_COMPOUND_ORDER: Sequence[str] = (
    "equilibrator",
    "modelseed_db",
    "modelseed",
)


class PredictiveThermoUtils(SharedEnvUtils):
    """Backend-dispatching predictive thermodynamics utilities.

    Args:
        thermo_utils: Optional :class:`kbutillib.thermo_utils.ThermoUtils` (or
            ``ThermoUtilsImpl``) used by the ModelSEED backend. If ``None``, a
            ``ThermoUtils`` is lazily constructed when first needed.
        **kwargs: Passed to :class:`SharedEnvUtils`.
    """

    def __init__(self, thermo_utils: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._thermo_utils = thermo_utils
        self._backends: Optional[Dict[str, Any]] = None

    # ── backend wiring ──────────────────────────────────────────────────

    @property
    def thermo_utils(self) -> Any:
        """Lazily build a ThermoUtils for the ModelSEED backend if needed."""
        if self._thermo_utils is None:
            from .thermo_utils import ThermoUtils

            self._thermo_utils = ThermoUtils(
                config_file=False, token_file=None, kbase_token_file=None
            )
        return self._thermo_utils

    def _build_backends(self) -> Dict[str, Any]:
        """Instantiate all backends (cheap; heavy deps stay deferred)."""
        return {
            "equilibrator": EquilibratorBackend(logger=self),
            "dgpredictor": DGPredictorBackend(
                config_resolver=self.get_config_value, logger=self
            ),
            "molgpk": MolGPKBackend(
                config_resolver=self.get_config_value, logger=self
            ),
            "modelseed_db": ModelSEEDDBBackend(
                config_resolver=self.get_config_value, logger=self
            ),
            "modelseed": ModelSEEDBackend(self.thermo_utils, logger=self),
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
                f"Unknown thermo backend '{name}'. "
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

    # ── reaction ────────────────────────────────────────────────────────

    def reaction_dg_prime(
        self,
        reaction_id: str,
        stoichiometry: Optional[Mapping[str, float]] = None,
        backend: Optional[str] = None,
        ph: float = 7.0,
        ionic_strength: float = 0.25,
        temperature: float = 298.15,
        p_mg: float = 14.0,
        **kwargs: Any,
    ) -> ReactionThermoEstimate:
        """Estimate a reaction's standard transformed Gibbs free energy.

        Args:
            reaction_id: Reaction identifier. For the ModelSEED backend this is
                a ``rxnNNNNN`` accession; for equilibrator the ``stoichiometry``
                is used (namespaced compound ids) and this only labels the
                result.
            stoichiometry: Mapping of compound id -> signed coefficient. May be
                ``None`` when targeting the ModelSEED backend (which resolves the
                reaction by accession).
            backend: Force a single backend by name, or ``None`` to walk the
                default priority order.
            ph, ionic_strength, temperature, p_mg: Physiological conditions.
            **kwargs: Forwarded to the backend.

        Returns:
            A :class:`ReactionThermoEstimate`. If no backend produces a value,
            ``dg_prime`` is ``None`` with warnings listing what was tried.
        """
        stoich = dict(stoichiometry or {})
        order = self._resolve_order(backend, DEFAULT_REACTION_ORDER)
        attempted_warnings: List[str] = []
        last: Optional[ReactionThermoEstimate] = None

        for name in order:
            be = self.backends.get(name)
            if be is None:
                attempted_warnings.append(f"[{name}] not registered")
                continue
            if "reaction_dg" not in be.capabilities:
                attempted_warnings.append(f"[{name}] does not provide reaction ΔG")
                continue
            if not be.available:
                attempted_warnings.append(f"[{name}] unavailable: {be.unavailable_reason}")
                continue
            try:
                est = be.reaction_dg_prime(
                    reaction_id,
                    stoich,
                    ph=ph,
                    ionic_strength=ionic_strength,
                    temperature=temperature,
                    p_mg=p_mg,
                    **kwargs,
                )
            except BackendUnavailableError as exc:
                attempted_warnings.append(f"[{name}] unavailable: {exc}")
                continue
            except Exception as exc:  # defensive: never let one backend break dispatch
                attempted_warnings.append(f"[{name}] errored: {type(exc).__name__}: {exc}")
                continue
            last = est
            if est.is_estimated:
                if backend is None and len(order) > 1:
                    est.warnings.insert(0, f"estimated by backend '{name}'")
                return est
            attempted_warnings.append(
                f"[{name}] produced no value"
                + (f": {est.warnings[-1]}" if est.warnings else "")
            )

        # No backend produced a numeric value: return an empty-but-honest result.
        result = last or ReactionThermoEstimate(
            reaction_id=reaction_id,
            backend=order[0] if order else "none",
            ph=ph,
            ionic_strength=ionic_strength,
            temperature=temperature,
            p_mg=p_mg,
        )
        result.warnings.extend(attempted_warnings)
        return result

    # ── compound ────────────────────────────────────────────────────────

    def compound_dgf(
        self,
        compound_id: str,
        backend: Optional[str] = None,
        ph: float = 7.0,
        ionic_strength: float = 0.25,
        temperature: float = 298.15,
        **kwargs: Any,
    ) -> CompoundThermoEstimate:
        """Estimate a compound's standard formation energy / microspecies.

        Args:
            compound_id: Compound identifier (namespace is backend-specific).
            backend: Force a single backend, or ``None`` for priority order.
            ph, ionic_strength, temperature: Conditions.
            **kwargs: Forwarded to the backend.

        Returns:
            A :class:`CompoundThermoEstimate`. ``dgf`` is ``None`` (with
            warnings) if no backend produced a value.
        """
        order = self._resolve_order(backend, DEFAULT_COMPOUND_ORDER)
        attempted_warnings: List[str] = []
        last: Optional[CompoundThermoEstimate] = None

        for name in order:
            be = self.backends.get(name)
            if be is None:
                attempted_warnings.append(f"[{name}] not registered")
                continue
            if "compound_dgf" not in be.capabilities:
                attempted_warnings.append(f"[{name}] does not provide compound ΔGf")
                continue
            if not be.available:
                attempted_warnings.append(f"[{name}] unavailable: {be.unavailable_reason}")
                continue
            try:
                est = be.compound_dgf(
                    compound_id,
                    ph=ph,
                    ionic_strength=ionic_strength,
                    temperature=temperature,
                    **kwargs,
                )
            except BackendUnavailableError as exc:
                attempted_warnings.append(f"[{name}] unavailable: {exc}")
                continue
            except Exception as exc:
                attempted_warnings.append(f"[{name}] errored: {type(exc).__name__}: {exc}")
                continue
            last = est
            if est.dgf is not None:
                if backend is None and len(order) > 1:
                    est.warnings.insert(0, f"estimated by backend '{name}'")
                return est
            attempted_warnings.append(
                f"[{name}] produced no value"
                + (f": {est.warnings[-1]}" if est.warnings else "")
            )

        result = last or CompoundThermoEstimate(
            compound_id=compound_id,
            backend=order[0] if order else "none",
            ph=ph,
            ionic_strength=ionic_strength,
            temperature=temperature,
        )
        result.warnings.extend(attempted_warnings)
        return result

    # ── microspecies / pKa (molgpk only) ────────────────────────────────

    def compound_microspecies(
        self,
        compound_id: str,
        ph: float = 7.0,
        **kwargs: Any,
    ) -> CompoundThermoEstimate:
        """Estimate pKa values and the major microspecies for a compound.

        Routed exclusively to the ``molgpk`` backend (OPAM2 / MolGpKa-on-MSDB).

        Args:
            compound_id: Compound identifier accepted by molGPK.
            ph: pH at which to report the predominant microspecies.
            **kwargs: Forwarded to the backend.

        Returns:
            A :class:`CompoundThermoEstimate`. If molGPK is not configured, the
            result carries a warning and empty ``pka_values`` (no fabrication).
        """
        be = self.get_backend("molgpk")
        est = CompoundThermoEstimate(compound_id=compound_id, backend="molgpk", ph=ph)
        if not be.available:
            est.warnings.append(f"[molgpk] unavailable: {be.unavailable_reason}")
            return est
        try:
            return be.compound_dgf(compound_id, ph=ph, **kwargs)
        except BackendUnavailableError as exc:
            est.warnings.append(f"[molgpk] unavailable: {exc}")
            return est

    def compounds_microspecies(
        self,
        compound_ids: Sequence[str],
        ph: float = 7.0,
        **kwargs: Any,
    ) -> List[CompoundThermoEstimate]:
        """Batch pKa / major-microspecies for many compounds in one molGPK call.

        This is the efficient path for annotating a compound set: the molGPK
        model is loaded once for the whole list instead of once per compound.
        Routed exclusively to the ``molgpk`` backend.

        Args:
            compound_ids: Compound identifiers (SMILES/InChI) accepted by molGPK.
            ph: pH at which to report the predominant microspecies.
            **kwargs: Forwarded to the backend (e.g. ``tph``).

        Returns:
            A list of :class:`CompoundThermoEstimate`, positionally aligned with
            ``compound_ids``. If molGPK is unavailable, every entry carries a
            warning and empty ``pka_values`` (no fabrication).
        """
        ids = list(compound_ids)
        be = self.get_backend("molgpk")
        if not be.available:
            reason = be.unavailable_reason
            return [
                CompoundThermoEstimate(
                    compound_id=cid,
                    backend="molgpk",
                    ph=ph,
                    warnings=[f"[molgpk] unavailable: {reason}"],
                )
                for cid in ids
            ]
        try:
            return be.compounds_dgf(ids, ph=ph, **kwargs)
        except BackendUnavailableError as exc:
            return [
                CompoundThermoEstimate(
                    compound_id=cid,
                    backend="molgpk",
                    ph=ph,
                    warnings=[f"[molgpk] unavailable: {exc}"],
                )
                for cid in ids
            ]


class PredictiveThermoUtilsImpl:
    """Composition wrapper for :class:`PredictiveThermoUtils`.

    Holds ``env`` and a ``thermo`` delegate instead of inheriting from
    :class:`SharedEnvUtils`, matching the pattern used by
    :class:`kbutillib.thermo_utils.ThermoUtilsImpl`. Delegates all other
    attribute access to an internal :class:`PredictiveThermoUtils` instance.

    Args:
        env: A :class:`SharedEnvUtils` instance.
        thermo: The toolkit's existing ThermoUtils(Impl) instance, reused for
            the ModelSEED backend.
        **kwargs: Forwarded to the internal ``PredictiveThermoUtils``.
    """

    def __init__(self, env: Any, thermo: Any, **kwargs: Any) -> None:
        self._env = env
        self._thermo = thermo
        _kwargs = {
            "config_file": False,
            "token_file": None,
            "kbase_token_file": None,
        }
        _kwargs.update(kwargs)
        self._delegate = PredictiveThermoUtils(thermo_utils=thermo, **_kwargs)

    @property
    def env(self) -> Any:
        return self._env

    @property
    def thermo(self) -> Any:
        return self._thermo

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)
