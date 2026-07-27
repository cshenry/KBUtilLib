"""Shim: re-exports from ``kbutillib.domains.cheminformatics.base``.

.. deprecated::
    Import from ``kbutillib.domains.cheminformatics.base`` instead.
"""

from kbutillib.domains.cheminformatics.base import (
    BackendUnavailableError,
    ExpansionBackend,
    ExpansionResult,
    PredictedCompound,
    PredictedReaction,
)

__all__ = [
    "BackendUnavailableError",
    "ExpansionBackend",
    "ExpansionResult",
    "PredictedCompound",
    "PredictedReaction",
]
