"""domains.thermo — thermodynamic utilities namespace.

Re-exports the key public names from all submodules so callers can do::

    from kbutillib.domains.thermo import (
        ThermoUtils,
        ThermoUtilsImpl,
        PredictiveThermoUtils,
        PredictiveThermoUtilsImpl,
        ThermoBackend,
    )
"""

from __future__ import annotations

from .predictive_thermo_utils import PredictiveThermoUtils, PredictiveThermoUtilsImpl
from .thermo_predictors.base import ThermoBackend
from .thermo_utils import ThermoUtils, ThermoUtilsImpl

# Legacy alias: ThermoPredictor → ThermoBackend (for any code referencing the old name)
ThermoPredictor = ThermoBackend

__all__ = [
    "ThermoUtils",
    "ThermoUtilsImpl",
    "PredictiveThermoUtils",
    "PredictiveThermoUtilsImpl",
    "ThermoBackend",
    "ThermoPredictor",
]
