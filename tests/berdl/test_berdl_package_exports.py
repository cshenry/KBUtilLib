"""Guard: every name in ``kbutillib.domains.kbase.berdl.__all__`` is importable.

The berdl subpackage is the surface a consumer opens first. If ``__all__``
advertises a name that is not actually bound on the package -- a typo, a
renamed function, a re-export that a refactor dropped -- ``from
kbutillib.domains.kbase.berdl import X`` fails at the call site with a
confusing error rather than here.

This closes that hole in two directions:

1. Every name in ``__all__`` resolves to a real (non-``None``) object on the
   package AND is importable via ``from ... import <name>`` -- the form a
   consumer actually writes.
2. The three public functions and the capability class this PRD exists to
   expose (``table_name``, ``union_view_sql``, ``bootstrap``, and
   ``ClearinghouseCapability`` with its named exceptions) are present in
   ``__all__`` -- so a future refactor cannot quietly drop them and push a
   consumer back into a private module path (the "only one place constructs a
   table name" rule breaks the moment ``table_name`` is not reachable from
   the package).
"""

from __future__ import annotations

import importlib

import pytest

import kbutillib.domains.kbase.berdl as berdl


def test_all_is_non_trivial() -> None:
    """``__all__`` exists, is a list/tuple, and is not accidentally empty."""
    assert isinstance(berdl.__all__, (list, tuple))
    assert len(berdl.__all__) > 0
    # No duplicate entries -- a duplicated name is a copy-paste smell.
    assert len(berdl.__all__) == len(set(berdl.__all__))


@pytest.mark.parametrize("name", berdl.__all__)
def test_every_all_entry_is_a_real_attribute(name: str) -> None:
    """Every name in ``__all__`` is bound on the package and is not ``None``."""
    assert hasattr(berdl, name), f"{name!r} is in __all__ but not bound on the package"
    assert getattr(berdl, name) is not None, f"{name!r} resolved to None"


@pytest.mark.parametrize("name", berdl.__all__)
def test_every_all_entry_is_importable(name: str) -> None:
    """``from kbutillib.domains.kbase.berdl import <name>`` works for every name.

    ``hasattr`` alone can pass for a name that a star-import machinery would
    still reject; exercise the exact ``from ... import`` form a consumer uses.
    """
    module = importlib.import_module("kbutillib.domains.kbase.berdl")
    obj = getattr(module, name, None)
    assert obj is not None, f"could not import {name!r} from the berdl package"


def test_public_surface_this_prd_owns_is_exported() -> None:
    """``table_name``, ``union_view_sql``, ``bootstrap`` and the capability +
    its named exceptions are all in ``__all__``.

    These are the names t1-t5 built that consumers need and that were missing
    before this task; asserting them by name (not just "``__all__`` is
    non-empty") is what keeps a refactor from silently regressing the export.
    """
    required = {
        "table_name",
        "union_view_sql",
        "bootstrap",
        "ClearinghouseCapability",
        "ClearinghouseWriteTargetMismatchError",
        "ClearinghouseLoadPostflightError",
        "ClearinghouseLedgerAmbiguousError",
    }
    missing = required - set(berdl.__all__)
    assert not missing, f"missing from berdl.__all__: {sorted(missing)}"
