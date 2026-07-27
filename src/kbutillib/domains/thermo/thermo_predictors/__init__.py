"""Predictive thermodynamics backends for KBUtilLib.

This subpackage provides a pluggable set of backends that estimate standard
transformed Gibbs free energies of reaction (and, where supported, compound
formation energies and pKa / microspecies properties) from chemical structure
or identifier.

Design
------
The existing :class:`kbutillib.thermo_utils.ThermoUtils` performs ModelSEED
database lookups and ion-transfer accounting. It does *not* predict
thermodynamic values for compounds/reactions that are absent from the
ModelSEED database. This subpackage layers prediction on top of that
foundation without changing existing behavior:

* Each backend implements :class:`ThermoBackend` (a structural protocol).
* Backends are *optional*: a backend that cannot import its dependency, or
  whose external tool is not configured, reports ``available == False`` and a
  human-readable ``unavailable_reason`` instead of raising at import time.
* :class:`kbutillib.predictive_thermo_utils.PredictiveThermoUtils` dispatches
  to backends by name or by a priority order, and degrades gracefully.

Backends
--------
``equilibrator``
    Real, MIT-licensed component-contribution predictor
    (``equilibrator-api``). Wired and validated.
``modelseed``
    Adapter over the existing :class:`ThermoUtils` ModelSEED-DB lookups; serves
    as the always-on fallback (no new dependency).
``dgpredictor``
    Maranas/Tyo-lab group-contribution reaction predictor (Andrew Freiburger's
    fork). Runs out-of-process against a local clone; reports
    ``available == False`` until the repo + trained model are configured.
``molgpk``
    Tyo-lab microscopic-pKa / formation-energy predictor (molGPK / OPAM2).
    Interface is stubbed pending its public release; reports
    ``available == False`` until configured. It never fabricates values.

Nothing in this subpackage imports a heavy/optional dependency at module import
time; all such imports are deferred into the backend that needs them.
"""

from .base import (
    BackendUnavailableError,
    CompoundThermoEstimate,
    ReactionThermoEstimate,
    ThermoBackend,
)
from .dgpredictor_backend import DGPredictorBackend
from .equilibrator_backend import EquilibratorBackend
from .modelseed_backend import ModelSEEDBackend
from .modelseed_db_backend import ModelSEEDDBBackend
from .molgpk_backend import MolGPKBackend

__all__ = [
    "BackendUnavailableError",
    "CompoundThermoEstimate",
    "ReactionThermoEstimate",
    "ThermoBackend",
    "DGPredictorBackend",
    "EquilibratorBackend",
    "ModelSEEDBackend",
    "ModelSEEDDBBackend",
    "MolGPKBackend",
]
