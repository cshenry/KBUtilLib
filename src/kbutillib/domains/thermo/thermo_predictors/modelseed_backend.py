"""ModelSEED-database backend (adapter over the existing ThermoUtils).

This backend exposes the project's pre-existing ModelSEED deltaG lookups
(:class:`kbutillib.thermo_utils.ThermoUtils`) through the common
:class:`kbutillib.thermo_predictors.base.ThermoBackend` interface. It adds no
new dependency and is always available wherever ``ThermoUtils`` works, so it
serves as the dependable fallback in the dispatcher's priority order.

It is a *lookup* backend, not a predictor: it returns values that exist in the
ModelSEED database and reports ``missing_compounds`` (with no fabricated
number) when they do not. Identifiers are ModelSEED accessions (``cpdNNNNN`` /
``rxnNNNNN``).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .base import (
    BackendUnavailableError,
    CompoundThermoEstimate,
    ReactionThermoEstimate,
)


class ModelSEEDBackend:
    """Adapter exposing ModelSEED-DB deltaG lookups as a ThermoBackend.

    Args:
        thermo_utils: A :class:`kbutillib.thermo_utils.ThermoUtils` (or
            ``ThermoUtilsImpl``) instance to delegate lookups to.
        logger: Optional logger (duck-typed ``log_*`` / ``logging.Logger``).
    """

    name = "modelseed"

    def __init__(self, thermo_utils: Any, logger: Any = None) -> None:
        self._thermo = thermo_utils
        self._logger = logger

    def _log_warning(self, msg: str) -> None:
        if self._logger is None:
            return
        warn = getattr(self._logger, "log_warning", None) or getattr(
            self._logger, "warning", None
        )
        if callable(warn):
            warn(msg)

    @property
    def available(self) -> bool:
        """Available whenever a ThermoUtils delegate is present."""
        return self._thermo is not None

    @property
    def unavailable_reason(self) -> Optional[str]:
        """Reason string when unavailable, else ``None``."""
        if self._thermo is None:
            return "ModelSEED backend has no ThermoUtils delegate"
        return None

    @property
    def capabilities(self) -> "frozenset[str]":
        """Capabilities provided by ModelSEED lookups."""
        return frozenset({"reaction_dg", "compound_dgf"})

    def _require(self) -> Any:
        if self._thermo is None:
            raise BackendUnavailableError(
                "ModelSEED backend has no ThermoUtils delegate"
            )
        return self._thermo

    def reaction_dg_prime(
        self,
        reaction_id: str,
        stoichiometry: Mapping[str, float],
        ph: float = 7.0,
        ionic_strength: float = 0.25,
        temperature: float = 298.15,
        p_mg: float = 14.0,
        **kwargs: Any,
    ) -> ReactionThermoEstimate:
        """Look up a reaction's standard deltaG from the ModelSEED database.

        Args:
            reaction_id: ModelSEED reaction accession (e.g. ``rxn00001``). The
                ``stoichiometry`` mapping is unused here because the ModelSEED
                lookup resolves the reaction by its accession; it is accepted
                for interface compatibility.
            ph, ionic_strength, temperature, p_mg: Recorded on the result for
                provenance. ModelSEED stored values are at standard conditions;
                this backend does not re-transform them.
            **kwargs: Passed through to ``calculate_reaction_deltag``
                (e.g. ``use_compound_formation``, ``require_all_compounds``).

        Returns:
            A :class:`ReactionThermoEstimate`. On a lookup failure, ``dg_prime``
            is ``None`` with an explanatory warning (no fabricated value).

        Raises:
            BackendUnavailableError: If no ThermoUtils delegate is present.
        """
        thermo = self._require()
        estimate = ReactionThermoEstimate(
            reaction_id=reaction_id,
            backend=self.name,
            ph=ph,
            ionic_strength=ionic_strength,
            temperature=temperature,
            p_mg=p_mg,
        )
        try:
            result = thermo.calculate_reaction_deltag(reaction_id, **kwargs)
        except Exception as exc:
            estimate.warnings.append(
                f"ModelSEED reaction lookup failed ({type(exc).__name__}: {exc})"
            )
            self._log_warning(
                f"ModelSEED reaction lookup failed for {reaction_id}: {exc}"
            )
            return estimate

        if isinstance(result, dict):
            dg = result.get("deltag")
            estimate.dg_prime = float(dg) if dg is not None else None
            err = result.get("deltag_error")
            estimate.dg_prime_uncertainty = float(err) if err is not None else None
            estimate.equation = result.get("equation")
            for warning in result.get("warnings", []) or []:
                estimate.warnings.append(str(warning))
            for missing in result.get("missing_compounds", []) or []:
                # missing entries may be dicts or strings depending on caller
                if isinstance(missing, dict):
                    cid = missing.get("compound_id") or missing.get("id") or str(missing)
                else:
                    cid = str(missing)
                estimate.missing_compounds.append(cid)
            estimate.raw = result
        return estimate

    def compound_dgf(
        self,
        compound_id: str,
        ph: float = 7.0,
        ionic_strength: float = 0.25,
        temperature: float = 298.15,
        **kwargs: Any,
    ) -> CompoundThermoEstimate:
        """Look up a compound's standard formation energy from ModelSEED.

        Args:
            compound_id: ModelSEED compound accession (e.g. ``cpd00002``).
            ph, ionic_strength, temperature: Recorded on the result for
                provenance.
            **kwargs: Ignored (accepted for interface compatibility).

        Returns:
            A :class:`CompoundThermoEstimate`. ``dgf`` is ``None`` (with a
            warning) if ModelSEED has no value (no fabricated number).

        Raises:
            BackendUnavailableError: If no ThermoUtils delegate is present.
        """
        thermo = self._require()
        estimate = CompoundThermoEstimate(
            compound_id=compound_id,
            backend=self.name,
            ph=ph,
            ionic_strength=ionic_strength,
            temperature=temperature,
        )
        try:
            dgf = thermo.get_compound_deltag(compound_id)
        except Exception as exc:
            estimate.warnings.append(
                f"ModelSEED compound lookup failed ({type(exc).__name__}: {exc})"
            )
            self._log_warning(
                f"ModelSEED compound lookup failed for {compound_id}: {exc}"
            )
            return estimate

        if dgf is None:
            estimate.warnings.append(
                f"ModelSEED has no formation energy for '{compound_id}'"
            )
        else:
            estimate.dgf = float(dgf)
        return estimate
