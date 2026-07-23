"""Base protocol for thermodynamic predictor backends.

All concrete backends must implement the :class:`ThermoPredictor` protocol
so the :class:`~kbutillib.domains.thermo.predictive_thermo_utils.PredictiveThermoUtils`
dispatcher can treat them uniformly.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class ThermoPredictor(Protocol):
    """Protocol that every thermodynamic-prediction backend must satisfy."""

    def predict_compound_deltag(
        self,
        compound_id: str,
        *,
        ph: float = 7.0,
        ionic_strength: float = 0.1,
        temperature: float = 298.15,
    ) -> Optional[float]:
        """Predict standard Gibbs free energy of formation (kJ/mol).

        Args:
            compound_id: Compound identifier (ModelSEED / KEGG / InChI).
            ph: pH.
            ionic_strength: Ionic strength in mol/L.
            temperature: Temperature in Kelvin.

        Returns:
            deltaGf in kJ/mol, or ``None`` if the backend cannot estimate it.
        """
        ...  # pragma: no cover

    def predict_reaction_deltag(
        self,
        reaction_id: str,
        *,
        ph: float = 7.0,
        ionic_strength: float = 0.1,
        temperature: float = 298.15,
    ) -> Optional[float]:
        """Predict standard Gibbs free energy change for a reaction (kJ/mol).

        Args:
            reaction_id: Reaction identifier.
            ph: pH.
            ionic_strength: Ionic strength in mol/L.
            temperature: Temperature in Kelvin.

        Returns:
            deltaGr in kJ/mol, or ``None`` if the backend cannot estimate it.
        """
        ...  # pragma: no cover


__all__ = ["ThermoPredictor"]
