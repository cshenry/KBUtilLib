"""MolGpKa backend for pKa-adjusted thermodynamic predictions.

Requires the ``molgpka`` package.  Raises ``ImportError`` at construction
if the package is not installed (causes the dispatcher to skip this backend).
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class MolgpkaBackend:
    """pKa-adjusted species energy predictions via MolGpKa.

    Raises:
        ImportError: If ``molgpka`` is not installed.
    """

    def __init__(self) -> None:
        """Initialise the MolGpKa backend.

        Raises:
            ImportError: If ``molgpka`` is not installed.
        """
        import molgpka  # noqa: F401 — fail fast if missing

        self._predictor = None

    def _get_predictor(self):  # type: ignore[return]
        if self._predictor is None:
            import molgpka  # noqa: PLC0415
            self._predictor = molgpka.Predictor()
        return self._predictor

    def predict_compound_deltag(
        self,
        compound_id: str,
        *,
        ph: float = 7.0,
        ionic_strength: float = 0.1,
        temperature: float = 298.15,
    ) -> Optional[float]:
        """Predict pKa-adjusted deltaGf for *compound_id*."""
        try:
            predictor = self._get_predictor()
            return float(predictor.predict(compound_id, ph=ph))
        except Exception as exc:  # noqa: BLE001
            logger.debug("MolgpkaBackend.predict_compound_deltag failed: %s", exc)
            return None

    def predict_reaction_deltag(
        self,
        reaction_id: str,
        *,
        ph: float = 7.0,
        ionic_strength: float = 0.1,
        temperature: float = 298.15,
    ) -> Optional[float]:
        """MolGpKa operates on compounds; reaction deltaG is not supported."""
        return None


__all__ = ["MolgpkaBackend"]
