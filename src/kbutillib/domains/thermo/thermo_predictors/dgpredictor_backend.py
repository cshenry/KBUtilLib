"""DGPredictor backend for ML-based thermodynamic prediction.

Requires the ``dgpredictor`` package.  Raises ``ImportError`` at construction
if the package is not installed (causes the dispatcher to skip this backend).
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class DGPredictorBackend:
    """ML-based thermodynamic predictions via DGPredictor.

    Raises:
        ImportError: If ``dgpredictor`` is not installed.
    """

    def __init__(self) -> None:
        """Initialise the DGPredictor backend.

        Raises:
            ImportError: If ``dgpredictor`` is not installed.
        """
        import dgpredictor  # noqa: F401 — fail fast if missing

        self._model = None

    def _get_model(self):  # type: ignore[return]
        if self._model is None:
            import dgpredictor  # noqa: PLC0415
            self._model = dgpredictor.load_default_model()
        return self._model

    def predict_compound_deltag(
        self,
        compound_id: str,
        *,
        ph: float = 7.0,
        ionic_strength: float = 0.1,
        temperature: float = 298.15,
    ) -> Optional[float]:
        """Predict deltaGf via DGPredictor ML model."""
        try:
            model = self._get_model()
            return float(model.predict_compound(compound_id))
        except Exception as exc:  # noqa: BLE001
            logger.debug("DGPredictorBackend.predict_compound_deltag failed: %s", exc)
            return None

    def predict_reaction_deltag(
        self,
        reaction_id: str,
        *,
        ph: float = 7.0,
        ionic_strength: float = 0.1,
        temperature: float = 298.15,
    ) -> Optional[float]:
        """Predict deltaGr via DGPredictor ML model."""
        try:
            model = self._get_model()
            return float(model.predict_reaction(reaction_id))
        except Exception as exc:  # noqa: BLE001
            logger.debug("DGPredictorBackend.predict_reaction_deltag failed: %s", exc)
            return None


__all__ = ["DGPredictorBackend"]
