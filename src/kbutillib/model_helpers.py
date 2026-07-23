"""Shim — re-exports from kbutillib.domains.modeling.model_helpers.

The full implementation now lives in:
    kbutillib.domains.modeling.model_helpers

This file is kept for backwards compatibility only.  All old imports continue
to work without modification.
"""

from kbutillib.domains.modeling.model_helpers import *  # noqa: F401,F403
from kbutillib.domains.modeling.model_helpers import (  # noqa: F401
    _parse_id,
    _check_and_convert_model,
)

__all__ = [
    "_parse_id",
    "_check_and_convert_model",
]
