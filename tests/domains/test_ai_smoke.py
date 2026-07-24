"""Smoke tests for domains.ai — verifies import paths + basic API contract.

These tests are OFFLINE — no httpx/argo API calls are made.
ArgoUtils requires httpx which may not be installed; tests use importorskip
for modules with optional deps and verify class-level structure only.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# 1. Top-level domain package importable
# ---------------------------------------------------------------------------


def test_ai_domain_importable() -> None:
    """kbutillib.domains.ai package is importable."""
    import kbutillib.domains.ai as ai_dom  # noqa: PLC0415
    assert ai_dom is not None


def test_ai_domain_has_module_map() -> None:
    """domains.ai._MODULE_MAP is a non-empty dict."""
    import kbutillib.domains.ai as ai_dom  # noqa: PLC0415
    assert isinstance(ai_dom._MODULE_MAP, dict)
    assert len(ai_dom._MODULE_MAP) > 0


def test_ai_domain_all_non_empty() -> None:
    """domains.ai.__all__ is a non-empty sequence."""
    import kbutillib.domains.ai as ai_dom  # noqa: PLC0415
    assert isinstance(ai_dom.__all__, (list, tuple))
    assert len(ai_dom.__all__) > 0


def test_ai_domain_all_contains_argo_utils() -> None:
    """domains.ai.__all__ advertises ArgoUtils."""
    import kbutillib.domains.ai as ai_dom  # noqa: PLC0415
    assert "ArgoUtils" in ai_dom.__all__
    assert "ArgoUtilsImpl" in ai_dom.__all__


def test_ai_domain_all_contains_ai_curation_utils() -> None:
    """domains.ai.__all__ advertises AICurationUtils."""
    import kbutillib.domains.ai as ai_dom  # noqa: PLC0415
    assert "AICurationUtils" in ai_dom.__all__


# ---------------------------------------------------------------------------
# 2. ArgoUtils (requires httpx) — use importorskip
# ---------------------------------------------------------------------------


def test_argo_utils_importable() -> None:
    """ArgoUtils is importable when httpx is available."""
    httpx = pytest.importorskip("httpx", reason="httpx not installed; skipping ArgoUtils tests")
    from kbutillib.domains.ai.argo_utils import ArgoUtils  # noqa: PLC0415
    assert ArgoUtils is not None
    assert callable(ArgoUtils)


def test_argo_utils_impl_importable() -> None:
    """ArgoUtilsImpl is importable when httpx is available."""
    pytest.importorskip("httpx", reason="httpx not installed; skipping ArgoUtils tests")
    from kbutillib.domains.ai.argo_utils import ArgoUtilsImpl  # noqa: PLC0415
    assert ArgoUtilsImpl is not None


def test_argo_utils_has_llm_label() -> None:
    """argo_utils module exports llm_label when httpx is available."""
    pytest.importorskip("httpx", reason="httpx not installed; skipping ArgoUtils tests")
    from kbutillib.domains.ai.argo_utils import llm_label  # noqa: PLC0415
    assert llm_label is not None


# ---------------------------------------------------------------------------
# 3. AICurationUtils (optional dep) — use importorskip
# ---------------------------------------------------------------------------


def test_ai_curation_utils_importable() -> None:
    """AICurationUtils is importable when ai_curation deps are available."""
    try:
        from kbutillib.domains.ai.ai_curation_utils import AICurationUtils  # noqa: PLC0415
        assert AICurationUtils is not None
    except ImportError:
        pytest.skip("ai_curation_utils deps not installed")


# ---------------------------------------------------------------------------
# 4. KBPLMUtils (optional dep — requires bio/ML libs) — use try/except
# ---------------------------------------------------------------------------


def test_kb_plm_utils_importable() -> None:
    """KBPLMUtils is importable when PLM deps are available."""
    try:
        from kbutillib.domains.ai.kb_plm_utils import KBPLMUtils  # noqa: PLC0415
        assert KBPLMUtils is not None
    except ImportError:
        pytest.skip("kb_plm_utils deps not installed")
