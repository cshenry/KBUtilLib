"""eQuilibrator backend for thermodynamic prediction.

Requires the ``equilibrator-api`` package.  Raises ``ImportError`` at
construction if the package is not installed (causes the dispatcher to
skip this backend and fall through to the next one).
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class EquilibratorBackend:
    """Group-contribution thermodynamic predictions via eQuilibrator.

    Raises:
        ImportError: If ``equilibrator_api`` is not installed.
    """

    def __init__(self) -> None:
        """Initialise the eQuilibrator backend.

        Raises:
            ImportError: If ``equilibrator_api`` is not installed.
        """
        import equilibrator_api  # noqa: F401 — fail fast if missing

        self._cc = None  # lazy-initialise ComponentContribution

    def _get_cc(self):  # type: ignore[return]
        """Return a lazily initialised ComponentContribution instance."""
        if self._cc is None:
            from equilibrator_api import ComponentContribution  # noqa: PLC0415
            self._cc = ComponentContribution()
        return self._cc

    def predict_compound_deltag(
        self,
        compound_id: str,
        *,
        ph: float = 7.0,
        ionic_strength: float = 0.1,
        temperature: float = 298.15,
    ) -> Optional[float]:
        """Predict deltaGf for *compound_id* via group-contribution.

        Returns ``None`` if the compound is not in the eQuilibrator database.
        """
        try:
            cc = self._get_cc()
            # eQuilibrator uses KEGG identifiers; do a best-effort lookup
            cpd = cc.get_compound(compound_id)
            if cpd is None:
                return None
            result = cc.standard_dg_formation(cpd)
            if result is None:
                return None
            # result is a pint Quantity; convert to float kJ/mol
            return float(result.magnitude)
        except Exception as exc:  # noqa: BLE001
            logger.debug("EquilibratorBackend.predict_compound_deltag failed: %s", exc)
            return None

    def predict_reaction_deltag(
        self,
        reaction_id: str,
        *,
        ph: float = 7.0,
        ionic_strength: float = 0.1,
        temperature: float = 298.15,
    ) -> Optional[float]:
        """Predict deltaGr for *reaction_id* via group-contribution.

        Returns ``None`` if the reaction cannot be evaluated.
        """
        try:
            cc = self._get_cc()
            rxn = cc.parse_reaction_formula(reaction_id)
            result = cc.standard_dg_prime(rxn)
            if result is None:
                return None
            return float(result.magnitude)
        except Exception as exc:  # noqa: BLE001
            logger.debug("EquilibratorBackend.predict_reaction_deltag failed: %s", exc)
            return None


__all__ = ["EquilibratorBackend"]
