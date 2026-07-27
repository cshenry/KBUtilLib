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
    "NetworkExpansionUtils",
    "NetworkExpansionUtilsImpl",
    "VerabUtils",
    "VerabUtilsImpl",
]


def __getattr__(name: str):  # noqa: ANN001
    """Lazy loader for heavier sub-modules."""
    import importlib

    _lazy = {
        "NetworkExpansionUtils": ("network_expansion_utils", "NetworkExpansionUtils"),
        "NetworkExpansionUtilsImpl": ("network_expansion_utils", "NetworkExpansionUtilsImpl"),
        "VerabUtils": ("verab.facade", "VerabUtils"),
        "VerabUtilsImpl": ("verab.facade", "VerabUtilsImpl"),
    }
    if name in _lazy:
        mod_path, attr = _lazy[name]
        module = importlib.import_module(f"kbutillib.domains.cheminformatics.{mod_path}")
        return getattr(module, attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
