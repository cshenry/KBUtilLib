"""Smoke tests for domains.external — verifies import paths + basic API contract.

These tests are OFFLINE — no network calls to UniProt/PATRIC/BVBRC/RCSB are made.
Tests verify class presence, method signatures, and constants.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# 1. Submodule-level imports from canonical domain path
# ---------------------------------------------------------------------------


def test_kb_uniprot_utils_submodule_importable() -> None:
    """domains.external.kb_uniprot_utils is importable."""
    from kbutillib.domains.external import kb_uniprot_utils  # noqa: PLC0415
    assert kb_uniprot_utils is not None


def test_patric_ws_utils_submodule_importable() -> None:
    """domains.external.patric_ws_utils is importable."""
    from kbutillib.domains.external import patric_ws_utils  # noqa: PLC0415
    assert patric_ws_utils is not None


# ---------------------------------------------------------------------------
# 2. KBUniProtUtils structural API
# ---------------------------------------------------------------------------


def test_kb_uniprot_utils_class_importable() -> None:
    """KBUniProtUtils is importable from canonical path."""
    from kbutillib.domains.external.kb_uniprot_utils import KBUniProtUtils  # noqa: PLC0415
    assert KBUniProtUtils is not None
    assert callable(KBUniProtUtils)


def test_kb_uniprot_utils_impl_importable() -> None:
    """KBUniProtUtilsImpl is importable from canonical path."""
    from kbutillib.domains.external.kb_uniprot_utils import KBUniProtUtilsImpl  # noqa: PLC0415
    assert KBUniProtUtilsImpl is not None


def test_kb_uniprot_utils_has_get_uniprot_info() -> None:
    """KBUniProtUtils defines get_uniprot_info method."""
    from kbutillib.domains.external.kb_uniprot_utils import KBUniProtUtils  # noqa: PLC0415
    assert hasattr(KBUniProtUtils, "get_uniprot_info")
    assert callable(KBUniProtUtils.get_uniprot_info)


def test_kb_uniprot_utils_has_get_protein_sequence() -> None:
    """KBUniProtUtils defines get_protein_sequence method."""
    from kbutillib.domains.external.kb_uniprot_utils import KBUniProtUtils  # noqa: PLC0415
    assert hasattr(KBUniProtUtils, "get_protein_sequence")
    assert callable(KBUniProtUtils.get_protein_sequence)


def test_kb_uniprot_utils_has_get_pdb_ids() -> None:
    """KBUniProtUtils defines get_pdb_ids method."""
    from kbutillib.domains.external.kb_uniprot_utils import KBUniProtUtils  # noqa: PLC0415
    assert hasattr(KBUniProtUtils, "get_pdb_ids")
    assert callable(KBUniProtUtils.get_pdb_ids)


# ---------------------------------------------------------------------------
# 3. PatricWSUtils structural API + constants
# ---------------------------------------------------------------------------


def test_patric_ws_utils_class_importable() -> None:
    """PatricWSUtils is importable from canonical path."""
    from kbutillib.domains.external.patric_ws_utils import PatricWSUtils  # noqa: PLC0415
    assert PatricWSUtils is not None
    assert callable(PatricWSUtils)


def test_patric_ws_utils_impl_importable() -> None:
    """PatricWSUtilsImpl is importable from canonical path."""
    from kbutillib.domains.external.patric_ws_utils import PatricWSUtilsImpl  # noqa: PLC0415
    assert PatricWSUtilsImpl is not None


def test_patric_ws_utils_object_types_is_dict() -> None:
    """PatricWSUtils.OBJECT_TYPES is a dict (constant, no network needed)."""
    from kbutillib.domains.external.patric_ws_utils import PatricWSUtils  # noqa: PLC0415
    assert isinstance(PatricWSUtils.OBJECT_TYPES, dict)
    assert len(PatricWSUtils.OBJECT_TYPES) > 0


def test_patric_ws_utils_workspace_urls_is_dict() -> None:
    """PatricWSUtils.WORKSPACE_URLS is a dict (constant, no network needed)."""
    from kbutillib.domains.external.patric_ws_utils import PatricWSUtils  # noqa: PLC0415
    assert isinstance(PatricWSUtils.WORKSPACE_URLS, dict)
    assert len(PatricWSUtils.WORKSPACE_URLS) > 0


def test_patric_ws_utils_has_get_object() -> None:
    """PatricWSUtils defines get_object method."""
    from kbutillib.domains.external.patric_ws_utils import PatricWSUtils  # noqa: PLC0415
    assert hasattr(PatricWSUtils, "get_object")
    assert callable(PatricWSUtils.get_object)


# ---------------------------------------------------------------------------
# 4. Domain __all__ contract
# ---------------------------------------------------------------------------


def test_external_domain_all_non_empty() -> None:
    """domains.external.__all__ is non-empty."""
    import kbutillib.domains.external as external  # noqa: PLC0415
    assert isinstance(external.__all__, (list, tuple))
    assert len(external.__all__) > 0


def test_external_domain_all_contains_uniprot() -> None:
    """domains.external.__all__ contains KBUniProtUtils."""
    import kbutillib.domains.external as external  # noqa: PLC0415
    assert "KBUniProtUtils" in external.__all__
    assert "KBUniProtUtilsImpl" in external.__all__
