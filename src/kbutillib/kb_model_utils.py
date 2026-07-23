"""Shim — re-exports from kbutillib.domains.modeling.kb_model_utils.

The full implementation now lives in:
    kbutillib.domains.modeling.kb_model_utils

This file is kept for backwards compatibility only.  All old imports continue
to work without modification.
"""

from kbutillib.domains.modeling.kb_model_utils import *  # noqa: F401,F403
from kbutillib.domains.modeling.kb_model_utils import (  # noqa: F401
    KBModelUtils,
    KBModelUtilsImpl,
)

__all__ = [
    "KBModelUtils",
    "KBModelUtilsImpl",
]
