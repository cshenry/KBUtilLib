"""WP13 — biochem god-module split: shim + domain import regression tests.

Tests
-----
1.  Old path import: ``from kbutillib.ms_biochem_utils import MSBiochemUtilsImpl`` works.
2.  New path import: ``from kbutillib.domains.biochem.ms_biochem_utils import MSBiochemUtilsImpl`` works.
3.  Both paths resolve to the same class object (identity check).
4.  Old path: ``MSBiochemUtils`` importable.
5.  New path: ``MSBiochemUtils`` importable — same class.
6.  ``compartment_types`` dict importable from old path.
7.  ``compartment_types`` dict importable from new path — same object.
8.  WP3 schemas still importable from ``kbutillib.domains.biochem``.
9.  New domain __init__ re-exports MSBiochemUtilsImpl.
10. ``import kbutillib`` clean (no ImportError).
11. ``KBUtilLib().biochem`` attribute exists.
12. ``__all__`` in shim contains the expected public names.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# 1. Old (shim) path — MSBiochemUtilsImpl
# ---------------------------------------------------------------------------

def test_old_path_impl_importable():
    """Old path import of MSBiochemUtilsImpl must not raise."""
    from kbutillib.ms_biochem_utils import MSBiochemUtilsImpl  # noqa: F401


# ---------------------------------------------------------------------------
# 2. New (domain) path — MSBiochemUtilsImpl
# ---------------------------------------------------------------------------

def test_new_path_impl_importable():
    """New domain path import of MSBiochemUtilsImpl must not raise."""
    from kbutillib.domains.biochem.ms_biochem_utils import MSBiochemUtilsImpl  # noqa: F401


# ---------------------------------------------------------------------------
# 3. Same-object check for MSBiochemUtilsImpl
# ---------------------------------------------------------------------------

def test_impl_same_object():
    """Old and new paths must resolve to the identical class."""
    from kbutillib.ms_biochem_utils import MSBiochemUtilsImpl as OLD
    from kbutillib.domains.biochem.ms_biochem_utils import MSBiochemUtilsImpl as NEW
    assert OLD is NEW, "MSBiochemUtilsImpl should be the same object via both import paths"


# ---------------------------------------------------------------------------
# 4. Old path — MSBiochemUtils
# ---------------------------------------------------------------------------

def test_old_path_base_importable():
    """Old path import of legacy MSBiochemUtils must not raise."""
    from kbutillib.ms_biochem_utils import MSBiochemUtils  # noqa: F401


# ---------------------------------------------------------------------------
# 5. New path — MSBiochemUtils, same object
# ---------------------------------------------------------------------------

def test_base_same_object():
    """MSBiochemUtils must be the same class via old and new paths."""
    from kbutillib.ms_biochem_utils import MSBiochemUtils as OLD
    from kbutillib.domains.biochem.ms_biochem_utils import MSBiochemUtils as NEW
    assert OLD is NEW


# ---------------------------------------------------------------------------
# 6. Old path — compartment_types
# ---------------------------------------------------------------------------

def test_old_path_compartment_types():
    from kbutillib.ms_biochem_utils import compartment_types
    assert isinstance(compartment_types, dict)
    assert "cytosol" in compartment_types


# ---------------------------------------------------------------------------
# 7. New path — compartment_types, same object
# ---------------------------------------------------------------------------

def test_new_path_compartment_types_same():
    from kbutillib.ms_biochem_utils import compartment_types as OLD
    from kbutillib.domains.biochem.ms_biochem_utils import compartment_types as NEW
    assert OLD is NEW


# ---------------------------------------------------------------------------
# 8. WP3 schemas still importable from kbutillib.domains.biochem
# ---------------------------------------------------------------------------

def test_wp3_schemas_still_importable():
    from kbutillib.domains.biochem import (
        GetCompoundByIdInput,
        GetCompoundByIdOutput,
        GetReactionByIdInput,
        GetReactionByIdOutput,
        SearchCompoundsInput,
        SearchCompoundsOutput,
    )
    assert SearchCompoundsInput is not None
    assert SearchCompoundsOutput is not None
    assert GetCompoundByIdInput is not None
    assert GetCompoundByIdOutput is not None
    assert GetReactionByIdInput is not None
    assert GetReactionByIdOutput is not None


# ---------------------------------------------------------------------------
# 9. domains/biochem __init__ re-exports MSBiochemUtilsImpl
# ---------------------------------------------------------------------------

def test_domain_init_exports_impl():
    from kbutillib.domains.biochem import MSBiochemUtilsImpl
    assert MSBiochemUtilsImpl is not None


# ---------------------------------------------------------------------------
# 10. Top-level kbutillib import clean
# ---------------------------------------------------------------------------

def test_kbutillib_import_clean():
    import kbutillib  # noqa: F401


# ---------------------------------------------------------------------------
# 11. KBUtilLib().biochem attribute exists
# ---------------------------------------------------------------------------

def test_kbutillib_biochem_attr():
    from kbutillib import KBUtilLib
    kbu = KBUtilLib()
    assert hasattr(kbu, "biochem"), "KBUtilLib instance must have a .biochem attribute"


# ---------------------------------------------------------------------------
# 12. Shim __all__ contains expected public names
# ---------------------------------------------------------------------------

def test_shim_all_contents():
    import kbutillib.ms_biochem_utils as shim
    assert hasattr(shim, "__all__"), "Shim must define __all__"
    assert "MSBiochemUtils" in shim.__all__
    assert "MSBiochemUtilsImpl" in shim.__all__
    assert "compartment_types" in shim.__all__
