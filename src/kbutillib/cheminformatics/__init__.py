"""Shim: re-exports everything from ``kbutillib.domains.cheminformatics``.

.. deprecated::
    Import from ``kbutillib.domains.cheminformatics`` instead.
    This module is kept for backward compatibility only.
"""

from kbutillib.domains.cheminformatics import (
    BackendUnavailableError,
    ExpansionBackend,
    ExpansionResult,
    PickaxeBackend,
    PredictedCompound,
    PredictedReaction,
    RetroRulesBackend,
)

__all__ = [
    "BackendUnavailableError",
    "ExpansionBackend",
    "ExpansionResult",
    "PickaxeBackend",
    "PredictedCompound",
    "PredictedReaction",
    "RetroRulesBackend",
]
