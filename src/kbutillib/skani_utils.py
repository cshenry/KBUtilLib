"""Shim — re-exports from kbutillib.domains.genome.skani_utils.

The full implementation now lives in:
    kbutillib.domains.genome.skani_utils

This file is kept for backwards compatibility only.  All old imports continue
to work without modification.
"""

from kbutillib.domains.genome.skani_utils import *  # noqa: F401,F403
from kbutillib.domains.genome.skani_utils import (  # noqa: F401
    SKANIUtils,
    SKANIUtilsImpl,
)

__all__ = [
    "SKANIUtils",
    "SKANIUtilsImpl",
]
