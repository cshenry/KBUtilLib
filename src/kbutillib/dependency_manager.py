"""Backward-compatible shim — real implementation lives in kbutillib.core.dependency_manager.

Do not add logic here; import from kbutillib.core.dependency_manager directly for new code.
"""

from kbutillib.core.dependency_manager import (  # noqa: F401
    DEFAULT_DEPENDENCIES_FILE,
    KBUTILLIB_DIR,
    DependencyManager,
    get_data_path,
    get_dependency_manager,
    get_dependency_path,
)

__all__ = [
    "DependencyManager",
    "KBUTILLIB_DIR",
    "DEFAULT_DEPENDENCIES_FILE",
    "get_dependency_manager",
    "get_dependency_path",
    "get_data_path",
]
