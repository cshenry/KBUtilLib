"""Shim — re-exports from kbutillib.domains.modeling.ms_template_utils.

The full implementation now lives in:
    kbutillib.domains.modeling.ms_template_utils

This file is kept for backwards compatibility only.  All old imports continue
to work without modification.
"""

from kbutillib.domains.modeling.ms_template_utils import *  # noqa: F401,F403
from kbutillib.domains.modeling.ms_template_utils import (  # noqa: F401
    MSTemplateUtils,
    MSTemplateUtilsImpl,
)

__all__ = [
    "MSTemplateUtils",
    "MSTemplateUtilsImpl",
]
