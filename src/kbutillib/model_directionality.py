"""Shim — re-exports from kbutillib.domains.modeling.model_directionality.

The full implementation now lives in:
    kbutillib.domains.modeling.model_directionality

This file is kept for backwards compatibility only.  All old imports continue
to work without modification.
"""

from kbutillib.domains.modeling.model_directionality import *  # noqa: F401,F403
from kbutillib.domains.modeling.model_directionality import (  # noqa: F401
    direction_conversion,
    directionality_from_bounds,
    biochem_directionality,
    combine_directionality_signals,
)

__all__ = [
    "direction_conversion",
    "directionality_from_bounds",
    "biochem_directionality",
    "combine_directionality_signals",
]
