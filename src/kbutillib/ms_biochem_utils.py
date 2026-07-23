"""Shim — re-exports from kbutillib.domains.biochem.ms_biochem_utils.

The full implementation now lives in:
    kbutillib.domains.biochem.ms_biochem_utils

This file is kept for backwards compatibility only.  All old imports continue
to work without modification.
"""

from kbutillib.domains.biochem.ms_biochem_utils import *  # noqa: F401,F403
from kbutillib.domains.biochem.ms_biochem_utils import (  # noqa: F401
    MSBiochemUtils,
    MSBiochemUtilsImpl,
    compartment_types,
)

__all__ = [
    "MSBiochemUtils",
    "MSBiochemUtilsImpl",
    "compartment_types",
]
