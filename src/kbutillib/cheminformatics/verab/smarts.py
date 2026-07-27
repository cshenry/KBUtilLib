"""Shim: re-exports from ``kbutillib.domains.cheminformatics.verab.smarts``."""

from kbutillib.domains.cheminformatics.verab.smarts import (
    METHOXY_AROMATIC_SMARTS,
    METHOXY_AROMATIC_SMARTS_STRICT,
    SEED_COMPOUNDS,
    VERAB_ODEMETHYLATION_SMARTS,
)

__all__ = [
    "VERAB_ODEMETHYLATION_SMARTS",
    "METHOXY_AROMATIC_SMARTS",
    "METHOXY_AROMATIC_SMARTS_STRICT",
    "SEED_COMPOUNDS",
]
