"""Smoke tests for domains.biochem — verifies import paths + basic API contract.

These tests are OFFLINE — no network, no modelseedpy required for import-level checks.
MSBiochemUtilsImpl can be instantiated for structural tests; MSBiochemUtils raises
at __init__ when modelseedpy is missing, so we only test class-level attributes there.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# 1. Core classes importable from canonical domain path
# ---------------------------------------------------------------------------


def test_ms_biochem_utils_importable() -> None:
    """MSBiochemUtils is importable from the canonical domain path."""
    from kbutillib.domains.biochem import MSBiochemUtils  # noqa: PLC0415
    assert MSBiochemUtils is not None
    assert callable(MSBiochemUtils)


def test_ms_biochem_utils_impl_importable() -> None:
    """MSBiochemUtilsImpl is importable from the canonical domain path."""
    from kbutillib.domains.biochem import MSBiochemUtilsImpl  # noqa: PLC0415
    assert MSBiochemUtilsImpl is not None


def test_search_compounds_schema_importable() -> None:
    """SearchCompoundsInput/Output schemas are importable."""
    from kbutillib.domains.biochem.schemas import SearchCompoundsInput, SearchCompoundsOutput  # noqa: PLC0415
    assert SearchCompoundsInput is not None
    assert SearchCompoundsOutput is not None


def test_get_compound_by_id_schema_importable() -> None:
    """GetCompoundByIdInput/Output schemas are importable."""
    from kbutillib.domains.biochem.schemas import GetCompoundByIdInput, GetCompoundByIdOutput  # noqa: PLC0415
    assert GetCompoundByIdInput is not None
    assert GetCompoundByIdOutput is not None


def test_get_reaction_by_id_schema_importable() -> None:
    """GetReactionByIdInput/Output schemas are importable."""
    from kbutillib.domains.biochem.schemas import GetReactionByIdInput, GetReactionByIdOutput  # noqa: PLC0415
    assert GetReactionByIdInput is not None
    assert GetReactionByIdOutput is not None


# ---------------------------------------------------------------------------
# 2. MSBiochemUtilsImpl structural API (class-level, no modelseedpy needed)
# ---------------------------------------------------------------------------


def test_ms_biochem_utils_impl_has_available() -> None:
    """MSBiochemUtilsImpl defines `available` descriptor."""
    from kbutillib.domains.biochem import MSBiochemUtilsImpl  # noqa: PLC0415
    assert hasattr(MSBiochemUtilsImpl, "available")


def test_ms_biochem_utils_impl_has_unavailable_reason() -> None:
    """MSBiochemUtilsImpl defines `unavailable_reason` descriptor."""
    from kbutillib.domains.biochem import MSBiochemUtilsImpl  # noqa: PLC0415
    assert hasattr(MSBiochemUtilsImpl, "unavailable_reason")


def test_ms_biochem_utils_impl_has_search_compounds() -> None:
    """MSBiochemUtilsImpl has search_compounds method."""
    from kbutillib.domains.biochem import MSBiochemUtilsImpl  # noqa: PLC0415
    assert hasattr(MSBiochemUtilsImpl, "search_compounds")
    assert callable(MSBiochemUtilsImpl.search_compounds)


def test_ms_biochem_utils_impl_has_get_compound_by_id() -> None:
    """MSBiochemUtilsImpl has get_compound_by_id method."""
    from kbutillib.domains.biochem import MSBiochemUtilsImpl  # noqa: PLC0415
    assert hasattr(MSBiochemUtilsImpl, "get_compound_by_id")
    assert callable(MSBiochemUtilsImpl.get_compound_by_id)


def test_ms_biochem_utils_impl_has_get_reaction_by_id() -> None:
    """MSBiochemUtilsImpl has get_reaction_by_id method."""
    from kbutillib.domains.biochem import MSBiochemUtilsImpl  # noqa: PLC0415
    assert hasattr(MSBiochemUtilsImpl, "get_reaction_by_id")
    assert callable(MSBiochemUtilsImpl.get_reaction_by_id)


# ---------------------------------------------------------------------------
# 3. Compartment types constant
# ---------------------------------------------------------------------------


def test_compartment_types_is_dict() -> None:
    """compartment_types exported from biochem domain is a dict."""
    from kbutillib.domains.biochem import compartment_types  # noqa: PLC0415
    assert isinstance(compartment_types, dict)
    assert len(compartment_types) > 0


def test_compartment_types_has_cytosol() -> None:
    """compartment_types maps 'cytosol' to 'c'."""
    from kbutillib.domains.biochem import compartment_types  # noqa: PLC0415
    assert compartment_types.get("cytosol") == "c"


# ---------------------------------------------------------------------------
# 4. Domain __all__ contract
# ---------------------------------------------------------------------------


def test_biochem_domain_all_contains_expected() -> None:
    """domains.biochem.__all__ contains the core public names."""
    from kbutillib import domains  # noqa: PLC0415
    import kbutillib.domains.biochem as bio  # noqa: PLC0415
    expected = {"MSBiochemUtils", "MSBiochemUtilsImpl"}
    assert expected.issubset(set(bio.__all__))
