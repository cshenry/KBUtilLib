"""Smoke tests for kbutillib.core — verifies canonical imports + real behavior.

Renamed from test_core_moves_wp10.py. Drops the trivially-duplicate "old/new path"
import-only tests (both pointed at kbutillib.core.*). Keeps:
- core package export contract
- BaseUtils / SharedEnvUtils / DependencyManager instantiation
- isinstance / subclass assertions
- KBUtilLib() top-level construction
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# 1. Core canonical import paths
# ---------------------------------------------------------------------------


def test_base_utils_importable() -> None:
    """BaseUtils is importable from kbutillib.core.base_utils."""
    from kbutillib.core.base_utils import BaseUtils  # noqa: PLC0415
    assert BaseUtils is not None
    assert callable(BaseUtils)


def test_shared_env_utils_importable() -> None:
    """SharedEnvUtils is importable from kbutillib.core.shared_env_utils."""
    from kbutillib.core.shared_env_utils import SharedEnvUtils  # noqa: PLC0415
    assert SharedEnvUtils is not None


def test_dependency_manager_importable() -> None:
    """DependencyManager is importable from kbutillib.core.dependency_manager."""
    from kbutillib.core.dependency_manager import DependencyManager  # noqa: PLC0415
    assert DependencyManager is not None


def test_dependency_manager_module_functions() -> None:
    """Module-level helpers in dependency_manager are callable."""
    from kbutillib.core.dependency_manager import (  # noqa: PLC0415
        get_data_path,
        get_dependency_manager,
        get_dependency_path,
    )
    assert callable(get_dependency_manager)
    assert callable(get_dependency_path)
    assert callable(get_data_path)


# ---------------------------------------------------------------------------
# 2. kbutillib.core package re-exports the core classes
# ---------------------------------------------------------------------------


def test_core_package_exports_base_utils() -> None:
    """kbutillib.core re-exports BaseUtils."""
    from kbutillib.core import BaseUtils  # noqa: PLC0415
    assert BaseUtils is not None


def test_core_package_exports_shared_env_utils() -> None:
    """kbutillib.core re-exports SharedEnvUtils."""
    from kbutillib.core import SharedEnvUtils  # noqa: PLC0415
    assert SharedEnvUtils is not None


def test_core_package_exports_dependency_manager() -> None:
    """kbutillib.core re-exports DependencyManager."""
    from kbutillib.core import DependencyManager  # noqa: PLC0415
    assert DependencyManager is not None


# ---------------------------------------------------------------------------
# 3. Top-level kbutillib import does not raise
# ---------------------------------------------------------------------------


def test_import_kbutillib_top_level() -> None:
    """Importing kbutillib itself does not raise."""
    import kbutillib  # noqa: PLC0415
    assert kbutillib is not None


# ---------------------------------------------------------------------------
# 4. KBUtilLib() constructs without error
# ---------------------------------------------------------------------------


def test_kbutillib_constructs() -> None:
    """KBUtilLib() can be instantiated without error."""
    from kbutillib import KBUtilLib  # noqa: PLC0415
    obj = KBUtilLib()
    assert obj is not None


# ---------------------------------------------------------------------------
# 5. Instance-level behavior assertions
# ---------------------------------------------------------------------------


def test_base_utils_instantiation() -> None:
    """BaseUtils can be instantiated; has logger attribute."""
    from kbutillib.core.base_utils import BaseUtils  # noqa: PLC0415
    obj = BaseUtils()
    assert isinstance(obj, BaseUtils)
    assert hasattr(obj, "logger")


def test_dependency_manager_instantiation() -> None:
    """DependencyManager can be instantiated; has dependency_paths attribute."""
    from kbutillib.core.dependency_manager import DependencyManager  # noqa: PLC0415
    dm = DependencyManager(auto_init=False)
    assert isinstance(dm, DependencyManager)
    assert hasattr(dm, "dependency_paths")


def test_shared_env_utils_is_base_utils_subclass() -> None:
    """SharedEnvUtils is a subclass of BaseUtils (inheritance contract)."""
    from kbutillib.core.base_utils import BaseUtils  # noqa: PLC0415
    from kbutillib.core.shared_env_utils import SharedEnvUtils  # noqa: PLC0415
    assert issubclass(SharedEnvUtils, BaseUtils)
