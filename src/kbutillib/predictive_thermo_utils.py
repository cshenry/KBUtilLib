"""Backward-compatibility shim — predictive_thermo_utils.

The canonical implementation has moved to::

    kbutillib.domains.thermo.predictive_thermo_utils

All public names are re-exported here so existing code continues to work
without any modifications.
"""

from kbutillib.domains.thermo.predictive_thermo_utils import (  # noqa: F401
    PredictiveThermoUtils,
    PredictiveThermoUtilsImpl,
)

__all__ = [
    "PredictiveThermoUtils",
    "PredictiveThermoUtilsImpl",
]
