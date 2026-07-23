"""Backward-compatibility shim — thermo_predictors.

The canonical implementation has moved to::

    kbutillib.domains.thermo.thermo_predictors

All public names are re-exported here so existing code continues to work
without any modifications.
"""

from kbutillib.domains.thermo.thermo_predictors import get_backend  # noqa: F401
from kbutillib.domains.thermo.thermo_predictors.base import (
    ThermoPredictor,  # noqa: F401
)
from kbutillib.domains.thermo.thermo_predictors.dgpredictor_backend import (  # noqa: F401
    DGPredictorBackend,
)
from kbutillib.domains.thermo.thermo_predictors.equilibrator_backend import (  # noqa: F401
    EquilibratorBackend,
)
from kbutillib.domains.thermo.thermo_predictors.modelseed_backend import (  # noqa: F401
    ModelseedBackend,
)
from kbutillib.domains.thermo.thermo_predictors.molgpka_backend import (  # noqa: F401
    MolgpkaBackend,
)

__all__ = [
    "ThermoPredictor",
    "get_backend",
    "ModelseedBackend",
    "EquilibratorBackend",
    "DGPredictorBackend",
    "MolgpkaBackend",
]
