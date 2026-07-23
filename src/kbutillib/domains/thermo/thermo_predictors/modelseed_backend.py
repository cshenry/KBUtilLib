"""ModelSEED backend for thermodynamic prediction.

Adapts the stored deltaG values from the ModelSEED biochemistry database.
This is the legacy fallback backend; it requires ``modelseedpy``.
"""

from __future__ import annotations

import logging
from typing import Optional

from .base import ThermoPredictor

logger = logging.getLogger(__name__)


class ModelseedBackend:
    """Adapts :class:`~kbutillib.thermo_utils.ThermoUtils` as a predictor.

    Uses the stored ModelSEED deltaG values for compounds and reactions.
    Requires ``modelseedpy``; raises ``ImportError`` at construction if
    the package is not installed.
    """

    # Confirm this class satisfies the ThermoPredictor protocol at runtime
    if not hasattr(ThermoPredictor, '__protocol_attrs__'):
        pass  # noqa: PIE790  # protocol check skipped on older Python

    def __init__(self) -> None:
        """Initialise the ModelSEED backend.

        Raises:
            ImportError: If ``modelseedpy`` is not installed.
        """
        import modelseedpy  # noqa: F401 — intentional: fail fast if missing
        self._utils = None  # lazy

    def _get_utils(self):  # type: ignore[return]
        """Return a lazily initialised ThermoUtils instance."""
        if self._utils is None:
            from kbutillib.domains.thermo.thermo_utils import (
                ThermoUtils,  # noqa: PLC0415
            )
            self._utils = ThermoUtils(config_file=False, token_file=None, kbase_token_file=None)
        return self._utils

    def predict_compound_deltag(
        self,
        compound_id: str,
        *,
        ph: float = 7.0,
        ionic_strength: float = 0.1,
        temperature: float = 298.15,
    ) -> Optional[float]:
        """Return stored ModelSEED deltaGf for *compound_id* or ``None``."""
        try:
            return self._get_utils().get_compound_deltag(compound_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("ModelseedBackend.predict_compound_deltag failed: %s", exc)
            return None

    def predict_reaction_deltag(
        self,
        reaction_id: str,
        *,
        ph: float = 7.0,
        ionic_strength: float = 0.1,
        temperature: float = 298.15,
    ) -> Optional[float]:
        """Return calculated ModelSEED deltaGr for *reaction_id* or ``None``."""
        try:
            result = self._get_utils().calculate_reaction_deltag(
                reaction_id, use_compound_formation=True, require_all_compounds=False
            )
            return result.get("deltag")
        except Exception as exc:  # noqa: BLE001
            logger.debug("ModelseedBackend.predict_reaction_deltag failed: %s", exc)
            return None


__all__ = ["ModelseedBackend"]
