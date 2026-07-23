"""Regression tests for WP10: core/ physical move + backward-compatible shims.

Verifies that:
- Old import paths (kbutillib.base_utils, etc.) still resolve correctly.
- New import paths (kbutillib.core.base_utils, etc.) work.
- Old and new paths resolve to the SAME class objects (identity).
- kbutillib.core package exports the moved classes.
- Top-level kbutillib package import still works.
- KBUtilLib() constructs without error.
"""

import pytest


# ---------------------------------------------------------------------------
# 1. Old import paths still work
# ---------------------------------------------------------------------------


def test_old_base_utils_import():
    """kbutillib.base_utils.BaseUtils is importable from the old path."""
    from kbutillib.base_utils import BaseUtils  # noqa: F401

    assert BaseUtils is not None


def test_old_shared_env_utils_import():
    """kbutillib.shared_env_utils.SharedEnvUtils is importable from the old path."""
    from kbutillib.shared_env_utils import SharedEnvUtils  # noqa: F401

    assert SharedEnvUtils is not None


def test_old_dependency_manager_import():
    """kbutillib.dependency_manager.DependencyManager is importable from the old path."""
    from kbutillib.dependency_manager import DependencyManager  # noqa: F401

    assert DependencyManager is not None


def test_old_dependency_manager_module_functions():
    """Module-level helper functions are still accessible at the old shim path."""
    from kbutillib.dependency_manager import (  # noqa: F401
        get_data_path,
        get_dependency_manager,
        get_dependency_path,
    )

    assert callable(get_dependency_manager)
    assert callable(get_dependency_path)
    assert callable(get_data_path)


# ---------------------------------------------------------------------------
# 2. New import paths work
# ---------------------------------------------------------------------------


def test_new_base_utils_import():
    """kbutillib.core.base_utils.BaseUtils is importable from the new core path."""
    from kbutillib.core.base_utils import BaseUtils  # noqa: F401

    assert BaseUtils is not None


def test_new_shared_env_utils_import():
    """kbutillib.core.shared_env_utils.SharedEnvUtils importable from the new core path."""
    from kbutillib.core.shared_env_utils import SharedEnvUtils  # noqa: F401

    assert SharedEnvUtils is not None


def test_new_dependency_manager_import():
    """kbutillib.core.dependency_manager.DependencyManager importable from the new core path."""
    from kbutillib.core.dependency_manager import DependencyManager  # noqa: F401

    assert DependencyManager is not None


# ---------------------------------------------------------------------------
# 3. Old and new paths resolve to the SAME class object (identity)
# ---------------------------------------------------------------------------


def test_base_utils_same_object():
    """Old and new BaseUtils paths must resolve to the identical class object."""
    from kbutillib.base_utils import BaseUtils as OldBaseUtils
    from kbutillib.core.base_utils import BaseUtils as NewBaseUtils

    assert OldBaseUtils is NewBaseUtils, (
        "BaseUtils from old path is not the same object as from new core path"
    )


def test_shared_env_utils_same_object():
    """Old and new SharedEnvUtils paths must resolve to the identical class object."""
    from kbutillib.core.shared_env_utils import SharedEnvUtils as NewSharedEnvUtils
    from kbutillib.shared_env_utils import SharedEnvUtils as OldSharedEnvUtils

    assert OldSharedEnvUtils is NewSharedEnvUtils, (
        "SharedEnvUtils from old path is not the same object as from new core path"
    )


def test_dependency_manager_same_object():
    """Old and new DependencyManager paths must resolve to the identical class object."""
    from kbutillib.core.dependency_manager import DependencyManager as NewDM
    from kbutillib.dependency_manager import DependencyManager as OldDM

    assert OldDM is NewDM, (
        "DependencyManager from old path is not the same object as from new core path"
    )


# ---------------------------------------------------------------------------
# 4. kbutillib.core package re-exports the moved classes
# ---------------------------------------------------------------------------


def test_core_package_exports_base_utils():
    """kbutillib.core exports BaseUtils."""
    from kbutillib.core import BaseUtils  # noqa: F401

    assert BaseUtils is not None


def test_core_package_exports_shared_env_utils():
    """kbutillib.core exports SharedEnvUtils."""
    from kbutillib.core import SharedEnvUtils  # noqa: F401

    assert SharedEnvUtils is not None


def test_core_package_exports_dependency_manager():
    """kbutillib.core exports DependencyManager."""
    from kbutillib.core import DependencyManager  # noqa: F401

    assert DependencyManager is not None


# ---------------------------------------------------------------------------
# 5. Top-level kbutillib import does not raise
# ---------------------------------------------------------------------------


def test_import_kbutillib_top_level():
    """Importing kbutillib itself does not raise."""
    import kbutillib  # noqa: F401

    assert kbutillib is not None


# ---------------------------------------------------------------------------
# 6. KBUtilLib() constructs without error
# ---------------------------------------------------------------------------


def test_kbutillib_constructs():
    """KBUtilLib() can be instantiated without error."""
    from kbutillib import KBUtilLib

    obj = KBUtilLib()
    assert obj is not None


# ---------------------------------------------------------------------------
# 7. Instances from old path work correctly (isinstance / subclass)
# ---------------------------------------------------------------------------


def test_base_utils_instantiation():
    """BaseUtils imported from old shim path can be instantiated."""
    from kbutillib.base_utils import BaseUtils

    obj = BaseUtils()
    assert isinstance(obj, BaseUtils)
    assert hasattr(obj, "logger")


def test_dependency_manager_instantiation():
    """DependencyManager imported from old shim path can be instantiated."""
    from kbutillib.dependency_manager import DependencyManager

    dm = DependencyManager(auto_init=False)
    assert isinstance(dm, DependencyManager)
    assert hasattr(dm, "dependency_paths")


def test_shared_env_utils_is_base_utils_subclass():
    """SharedEnvUtils (from either path) is a subclass of BaseUtils."""
    from kbutillib.base_utils import BaseUtils
    from kbutillib.shared_env_utils import SharedEnvUtils

    assert issubclass(SharedEnvUtils, BaseUtils)
