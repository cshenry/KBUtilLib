"""Cheminformatics network-expansion backends for KBUtilLib.

This subpackage provides a pluggable set of backends that perform *in-silico
reaction prediction / metabolic network expansion*: given seed compounds and a
set of reaction rules (SMARTS reaction operators), they enumerate the reactions
and product compounds the rules generate.

Design
------
* Each backend implements :class:`ExpansionBackend` (a structural protocol).
* Backends are *optional*: a backend that cannot import its dependency, or
  whose external data/tool is not configured, reports ``available == False`` and
  a human-readable ``unavailable_reason`` instead of raising at import time.
* :class:`kbutillib.network_expansion_utils.NetworkExpansionUtils` dispatches to
  backends by name or by a priority order, and degrades gracefully.

Backends
--------
``pickaxe``
    Tyo/Henry-lab ``minedatabase`` (Pickaxe) metabolic network expansion via
    SMARTS reaction operators. Optional dependency; reports
    ``available == False`` until ``minedatabase`` and its rule data are
    installed/located. Validated against the canonical tyo-nu/MINE-Database
    rule sets.
``retrorules``
    Applies RetroRules SMARTS operators with RDKit. Optional; reports
    ``available == False`` until RDKit and a RetroRules flat-rules TSV are
    present. Validated against the rr02 dump.

Nothing in this subpackage imports a heavy/optional dependency at module import
time; all such imports are deferred into the backend that needs them. No backend
ever fabricates a structure or a rule match.
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
