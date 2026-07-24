"""Smoke tests for domains.kbase — verifies import paths + basic API contract.

These tests are OFFLINE — no KBase service calls are made.
Tests verify module structure, class presence, and pure-Python utility functions.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# 1. Submodule-level imports from canonical domain path
# ---------------------------------------------------------------------------


def test_kbase_endpoints_importable() -> None:
    """domains.kbase.kbase_endpoints is importable."""
    from kbutillib.domains.kbase import kbase_endpoints  # noqa: PLC0415
    assert kbase_endpoints is not None


def test_kb_berdl_utils_importable() -> None:
    """domains.kbase.kb_berdl_utils is importable."""
    from kbutillib.domains.kbase import kb_berdl_utils  # noqa: PLC0415
    assert kb_berdl_utils is not None


# ---------------------------------------------------------------------------
# 2. kbase_endpoints pure-Python functions (no network)
# ---------------------------------------------------------------------------


def test_base_url_callable() -> None:
    """kbase_endpoints.base_url is callable."""
    from kbutillib.domains.kbase.kbase_endpoints import base_url  # noqa: PLC0415
    assert callable(base_url)


def test_base_url_ci_returns_string() -> None:
    """base_url('ci') returns a non-empty string."""
    from kbutillib.domains.kbase.kbase_endpoints import base_url  # noqa: PLC0415
    result = base_url("ci")
    assert isinstance(result, str)
    assert "ci.kbase.us" in result


def test_base_url_prod_returns_string() -> None:
    """base_url('prod') returns a non-empty string."""
    from kbutillib.domains.kbase.kbase_endpoints import base_url  # noqa: PLC0415
    result = base_url("prod")
    assert isinstance(result, str)
    assert "kbase.us" in result


def test_env_from_url_callable() -> None:
    """kbase_endpoints.env_from_url is callable."""
    from kbutillib.domains.kbase.kbase_endpoints import env_from_url  # noqa: PLC0415
    assert callable(env_from_url)


def test_env_from_url_ci() -> None:
    """env_from_url returns 'ci' for ci.kbase.us URL."""
    from kbutillib.domains.kbase.kbase_endpoints import env_from_url  # noqa: PLC0415
    result = env_from_url("https://ci.kbase.us/services/ws")
    assert result == "ci"


def test_env_from_url_prod() -> None:
    """env_from_url returns 'prod' for kbase.us URL."""
    from kbutillib.domains.kbase.kbase_endpoints import env_from_url  # noqa: PLC0415
    result = env_from_url("https://kbase.us/services/ws")
    assert result == "prod"


def test_narrative_url_callable() -> None:
    """kbase_endpoints.narrative_url is callable."""
    from kbutillib.domains.kbase.kbase_endpoints import narrative_url  # noqa: PLC0415
    assert callable(narrative_url)


def test_narrative_url_returns_string() -> None:
    """narrative_url('ci') returns a non-empty string."""
    from kbutillib.domains.kbase.kbase_endpoints import narrative_url  # noqa: PLC0415
    result = narrative_url("ci")
    assert isinstance(result, str)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# 3. KBBERDLUtils structural API
# ---------------------------------------------------------------------------


def test_kb_berdl_utils_class_importable() -> None:
    """KBBERDLUtils is importable from canonical path."""
    from kbutillib.domains.kbase.kb_berdl_utils import KBBERDLUtils  # noqa: PLC0415
    assert KBBERDLUtils is not None
    assert callable(KBBERDLUtils)


def test_kb_berdl_utils_impl_importable() -> None:
    """KBBERDLUtilsImpl is importable from canonical path."""
    from kbutillib.domains.kbase.kb_berdl_utils import KBBERDLUtilsImpl  # noqa: PLC0415
    assert KBBERDLUtilsImpl is not None


def test_kb_berdl_utils_has_query() -> None:
    """KBBERDLUtils defines query method."""
    from kbutillib.domains.kbase.kb_berdl_utils import KBBERDLUtils  # noqa: PLC0415
    assert hasattr(KBBERDLUtils, "query")
    assert callable(KBBERDLUtils.query)


def test_kb_berdl_utils_has_test_connection() -> None:
    """KBBERDLUtils defines test_connection method."""
    from kbutillib.domains.kbase.kb_berdl_utils import KBBERDLUtils  # noqa: PLC0415
    assert hasattr(KBBERDLUtils, "test_connection")
    assert callable(KBBERDLUtils.test_connection)


# ---------------------------------------------------------------------------
# 4. Domain __all__ contract
# ---------------------------------------------------------------------------


def test_kbase_domain_all_non_empty() -> None:
    """domains.kbase.__all__ is non-empty."""
    import kbutillib.domains.kbase as kbase  # noqa: PLC0415
    assert isinstance(kbase.__all__, (list, tuple))
    assert len(kbase.__all__) > 0


def test_kbase_domain_all_contains_berdl() -> None:
    """domains.kbase.__all__ contains KBBERDLUtils."""
    import kbutillib.domains.kbase as kbase  # noqa: PLC0415
    assert "KBBERDLUtils" in kbase.__all__
    assert "KBBERDLUtilsImpl" in kbase.__all__
