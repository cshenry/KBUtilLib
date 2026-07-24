"""Backward-compatibility shim — thermo_predictors.

The canonical implementation has moved to::

    kbutillib.domains.thermo.thermo_predictors

All public names are re-exported here so existing code continues to work
without any modifications.
"""

from kbutillib.domains.thermo.thermo_predictors import (  # noqa: F401
    BackendUnavailableError,
    CompoundThermoEstimate,
    DGPredictorBackend,
    EquilibratorBackend,
    ModelSEEDBackend,
    ModelSEEDDBBackend,
    MolGPKBackend,
    ReactionThermoEstimate,
    ThermoBackend,
)

# Legacy alias — ThermoPredictor was the old Protocol name before the PR rename.
ThermoPredictor = ThermoBackend  # noqa: F401

__all__ = [
    "ThermoBackend",
    "ThermoPredictor",
    "BackendUnavailableError",
    "CompoundThermoEstimate",
    "ReactionThermoEstimate",
    "ModelSEEDBackend",
    "ModelSEEDDBBackend",
    "EquilibratorBackend",
    "DGPredictorBackend",
    "MolGPKBackend",
]
