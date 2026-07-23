"""Cheminformatics network-expansion backends for KBUtilLib.

This subpackage provides a pluggable set of backends that perform *in-silico
reaction prediction / metabolic network expansion*: given seed compounds and a
set of reaction rules (SMARTS reaction operators), they enumerate the reactions
and product compounds the rules generate.

Canonical location
------------------
``kbutillib.domains.cheminformatics`` is the **single source of truth** for all
cheminformatics types, backends, and verAB utilities.

The legacy path ``kbutillib.cheminformatics`` is a re-export shim; all new code
should import from this module.
"""

from .base import (
    BackendUnavailableError,
    ExpansionBackend,
    ExpansionResult,
    PredictedCompound,
    PredictedReaction,
)
from .pickaxe_backend import PickaxeBackend
from .retrorules_backend import RetroRulesBackend

__all__ = [
    "BackendUnavailableError",
    "ExpansionBackend",
    "ExpansionResult",
    "PredictedCompound",
    "PredictedReaction",
    "PickaxeBackend",
    "RetroRulesBackend",
]
