"""Backward-compatible shim — real implementation lives in kbutillib.core.shared_env_utils.

Do not add logic here; import from kbutillib.core.shared_env_utils directly for new code.
"""

from kbutillib.core.shared_env_utils import (  # noqa: F401
    DEFAULT_CONFIG_FILE,
    KBUTILLIB_DIR,
    SharedEnvUtils,
)

__all__ = [
    "SharedEnvUtils",
    "KBUTILLIB_DIR",
    "DEFAULT_CONFIG_FILE",
]
