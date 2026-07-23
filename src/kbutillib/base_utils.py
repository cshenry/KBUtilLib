"""Backward-compatible shim — real implementation lives in kbutillib.core.base_utils.

Do not add logic here; import from kbutillib.core.base_utils directly for new code.
"""

from kbutillib.core.base_utils import (  # noqa: F401
    BaseUtils,
    script_dir,
    script_path,
)

__all__ = [
    "BaseUtils",
    "script_dir",
    "script_path",
]
