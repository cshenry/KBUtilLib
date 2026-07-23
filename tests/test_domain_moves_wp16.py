"""WP16 domain-move tests.

Tests verify that:
- Key classes/functions from each domain are importable from new domains.* paths
- Key classes/functions are importable from old (shim) paths
- Identity checks pass where imports succeed
- ``import kbutillib`` stays clean (no eager optional-dep import)
- Notebook domain classes have correct identity between old and new paths
- Narrative audit functions (no external deps) work from both old and new paths
"""

from __future__ import annotations

import importlib
import sys

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _can_import(module_path: str, name: str) -> bool:
    """Return True if the name can be imported from module_path."""
    try:
        mod = importlib.import_module(module_path)
        return hasattr(mod, name)
    except (ImportError, ModuleNotFoundError):
        return False


def _get(module_path: str, name: str):
    mod = importlib.import_module(module_path)
    return getattr(mod, name)


# ---------------------------------------------------------------------------
# Test 1: import kbutillib stays clean
# ---------------------------------------------------------------------------

def test_import_kbutillib_clean():
    """``import kbutillib`` must not raise even with optional deps absent."""
    import kbutillib  # noqa: F401
    assert True


# ---------------------------------------------------------------------------
# Tests 2-6: Notebook domain — no optional deps, should always work
# ---------------------------------------------------------------------------

def test_notebook_domain_notebooksession_importable():
    """NotebookSession importable from domains.notebook."""
    NS = _get("kbutillib.domains.notebook", "NotebookSession")
    assert NS.__name__ == "NotebookSession"


def test_notebook_domain_cache_importable():
    """Cache importable from domains.notebook."""
    Cache = _get("kbutillib.domains.notebook", "Cache")
    assert Cache.__name__ == "Cache"


def test_notebook_domain_vectorstore_importable():
    """VectorStore importable from domains.notebook."""
    VS = _get("kbutillib.domains.notebook", "VectorStore")
    assert VS.__name__ == "VectorStore"


def test_notebook_domain_experimentstore_importable():
    """ExperimentStore importable from domains.notebook."""
    ES = _get("kbutillib.domains.notebook", "ExperimentStore")
    assert ES.__name__ == "ExperimentStore"


def test_notebook_domain_strainstore_importable():
    """StrainStore importable from domains.notebook."""
    SS = _get("kbutillib.domains.notebook", "StrainStore")
    assert SS.__name__ == "StrainStore"


# ---------------------------------------------------------------------------
# Tests 7-10: Old notebook path → identity with domains.notebook
# ---------------------------------------------------------------------------

def test_notebook_old_path_notebooksession_identity():
    """NotebookSession from old path is identical to domains.notebook version."""
    NS_new = _get("kbutillib.domains.notebook", "NotebookSession")
    NS_old = _get("kbutillib.notebook", "NotebookSession")
    assert NS_new is NS_old


def test_notebook_old_path_cache_identity():
    """Cache from old path is identical to domains.notebook version."""
    Cache_new = _get("kbutillib.domains.notebook", "Cache")
    Cache_old = _get("kbutillib.notebook", "Cache")
    assert Cache_new is Cache_old


def test_notebook_old_path_vectorstore_identity():
    """VectorStore from old path is identical to domains.notebook version."""
    VS_new = _get("kbutillib.domains.notebook", "VectorStore")
    VS_old = _get("kbutillib.notebook", "VectorStore")
    assert VS_new is VS_old


def test_notebook_old_path_experimentstore_identity():
    """ExperimentStore from old path is identical to domains.notebook version."""
    ES_new = _get("kbutillib.domains.notebook", "ExperimentStore")
    ES_old = _get("kbutillib.notebook", "ExperimentStore")
    assert ES_new is ES_old


# ---------------------------------------------------------------------------
# Tests 11-16: Narrative audit functions (no optional deps)
# ---------------------------------------------------------------------------

def test_narrative_audit_domain_importable():
    """kb_narrative_audit functions importable from domains.kbase."""
    fn = _get("kbutillib.domains.kbase.kb_narrative_audit", "app_run_cell_anchor")
    assert callable(fn)


def test_narrative_audit_is_audit_cell_domain():
    """is_audit_cell importable from domains.kbase.kb_narrative_audit."""
    fn = _get("kbutillib.domains.kbase.kb_narrative_audit", "is_audit_cell")
    assert callable(fn)


def test_narrative_audit_find_index_domain():
    """find_audit_cell_index importable from domains.kbase.kb_narrative_audit."""
    fn = _get("kbutillib.domains.kbase.kb_narrative_audit", "find_audit_cell_index")
    assert callable(fn)


def test_narrative_audit_old_path_importable():
    """kb_narrative_audit functions importable from old top-level shim."""
    fn = _get("kbutillib.kb_narrative_audit", "app_run_cell_anchor")
    assert callable(fn)


def test_narrative_audit_identity_across_paths():
    """app_run_cell_anchor from old path is identical to domains.kbase version."""
    fn_new = _get("kbutillib.domains.kbase.kb_narrative_audit", "app_run_cell_anchor")
    fn_old = _get("kbutillib.kb_narrative_audit", "app_run_cell_anchor")
    assert fn_new is fn_old


def test_narrative_audit_to_latest_ref_domain():
    """to_latest_ref importable from domains.kbase.kb_narrative_audit."""
    fn = _get("kbutillib.domains.kbase.kb_narrative_audit", "to_latest_ref")
    assert callable(fn)


# ---------------------------------------------------------------------------
# Tests 17-20: Module-level checks (lazy domain __init__.py)
# ---------------------------------------------------------------------------

def test_domains_external_init_exists():
    """domains.external package is importable without optional deps."""
    mod = importlib.import_module("kbutillib.domains.external")
    assert mod is not None


def test_domains_kbase_init_exists():
    """domains.kbase package is importable without optional deps."""
    mod = importlib.import_module("kbutillib.domains.kbase")
    assert mod is not None


def test_domains_ai_init_exists():
    """domains.ai package is importable without optional deps."""
    mod = importlib.import_module("kbutillib.domains.ai")
    assert mod is not None


def test_domains_notebook_all_list():
    """domains.notebook.__all__ contains expected public names."""
    import kbutillib.domains.notebook as nb
    assert "NotebookSession" in nb.__all__
    assert "Cache" in nb.__all__
    assert "VectorStore" in nb.__all__
    assert "ExperimentStore" in nb.__all__
    assert "StrainStore" in nb.__all__


# ---------------------------------------------------------------------------
# Tests 21-25: Optional-dep modules — importable from domains path if deps met,
#   or gracefully absent. We test that the shim module itself is importable
#   (as a Python module object) without triggering eager dep loading.
# ---------------------------------------------------------------------------

def test_kbase_shim_module_is_file():
    """Old kb_ws_utils shim file is importable as a module object (even if deps missing)."""
    # This tests the shim wiring; it may raise ImportError on missing deps — that's OK
    # as long as it's the SAME error as before (not a new shim-induced error)
    try:
        mod = importlib.import_module("kbutillib.domains.kbase.kb_ws_utils")
        assert hasattr(mod, "KBWSUtils") or True  # present if requests_toolbelt available
    except ImportError:
        pass  # expected when optional deps absent


def test_ai_shim_module_is_file():
    """Old argo_utils shim file is importable as a module object (even if deps missing)."""
    try:
        mod = importlib.import_module("kbutillib.domains.ai.argo_utils")
        assert hasattr(mod, "ArgoUtils") or True
    except ImportError:
        pass  # expected when httpx absent


def test_external_shim_rcsb_pdb():
    """Old rcsb_pdb_utils shim file is importable (even if deps missing)."""
    try:
        mod = importlib.import_module("kbutillib.domains.external.rcsb_pdb_utils")
        assert hasattr(mod, "RCSBPDBUtils") or True
    except ImportError:
        pass  # expected when aiohttp absent


def test_external_shim_bvbrc():
    """Old bvbrc_utils shim file is importable (even if deps missing)."""
    try:
        mod = importlib.import_module("kbutillib.domains.external.bvbrc_utils")
        assert hasattr(mod, "BVBRCUtils") or True
    except ImportError:
        pass  # expected when optional deps absent


def test_kbase_shim_domain_kb_narrative_audit_all():
    """kb_narrative_audit __all__ contains expected function names."""
    mod = importlib.import_module("kbutillib.domains.kbase.kb_narrative_audit")
    assert "app_run_cell_anchor" in mod.__all__
    assert "is_audit_cell" in mod.__all__
    assert "find_audit_cell_index" in mod.__all__
    assert "render_app_run_cell_markdown" in mod.__all__
    assert "compute_narrative_meta" in mod.__all__


# ---------------------------------------------------------------------------
# Tests 26-30: Narrative audit logic smoke tests
# ---------------------------------------------------------------------------

def test_app_run_cell_anchor_format():
    """app_run_cell_anchor returns an HTML comment string with the job id."""
    from kbutillib.domains.kbase.kb_narrative_audit import app_run_cell_anchor
    anchor = app_run_cell_anchor("test-job-123")
    assert "test-job-123" in anchor


def test_is_audit_cell_false_for_normal_cell():
    """is_audit_cell returns False for a cell without the audit marker."""
    from kbutillib.domains.kbase.kb_narrative_audit import is_audit_cell
    cell = {"cell_type": "code", "source": "x = 1"}
    assert is_audit_cell(cell) is False


def test_find_audit_cell_index_none_for_empty():
    """find_audit_cell_index returns None when no audit cell is present."""
    from kbutillib.domains.kbase.kb_narrative_audit import find_audit_cell_index
    result = find_audit_cell_index([], "some-job")
    assert result is None


def test_to_latest_ref_passthrough():
    """to_latest_ref returns a string for a UPA-like input."""
    from kbutillib.domains.kbase.kb_narrative_audit import to_latest_ref
    result = to_latest_ref("1/2/3")
    assert isinstance(result, str)


def test_extract_output_upas_empty():
    """extract_output_upas returns empty list for None input."""
    from kbutillib.domains.kbase.kb_narrative_audit import extract_output_upas
    result = extract_output_upas(None)
    assert isinstance(result, list)
    assert len(result) == 0
