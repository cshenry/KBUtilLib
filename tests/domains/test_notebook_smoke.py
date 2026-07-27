"""Smoke tests for domains.notebook — verifies import paths + basic API contract.

These tests are OFFLINE — no KBase services or filesystem changes.
Tests verify class presence and method signatures only.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# 1. Package-level imports from canonical domain path
# ---------------------------------------------------------------------------


def test_notebook_domain_importable() -> None:
    """kbutillib.domains.notebook package is importable."""
    import kbutillib.domains.notebook as notebook  # noqa: PLC0415
    assert notebook is not None


def test_notebook_cache_importable() -> None:
    """domains.notebook.Cache is importable."""
    from kbutillib.domains.notebook import Cache  # noqa: PLC0415
    assert Cache is not None


def test_notebook_cache_entry_importable() -> None:
    """domains.notebook.CacheEntry is importable."""
    from kbutillib.domains.notebook import CacheEntry  # noqa: PLC0415
    assert CacheEntry is not None


def test_notebook_experiment_store_importable() -> None:
    """domains.notebook.ExperimentStore is importable."""
    from kbutillib.domains.notebook import ExperimentStore  # noqa: PLC0415
    assert ExperimentStore is not None


def test_notebook_session_importable() -> None:
    """domains.notebook.NotebookSession is importable."""
    from kbutillib.domains.notebook import NotebookSession  # noqa: PLC0415
    assert NotebookSession is not None


def test_notebook_vector_store_importable() -> None:
    """domains.notebook.VectorStore is importable."""
    from kbutillib.domains.notebook import VectorStore  # noqa: PLC0415
    assert VectorStore is not None


def test_notebook_strain_store_importable() -> None:
    """domains.notebook.StrainStore is importable."""
    from kbutillib.domains.notebook import StrainStore  # noqa: PLC0415
    assert StrainStore is not None


# ---------------------------------------------------------------------------
# 2. Cache API contract
# ---------------------------------------------------------------------------


def test_cache_has_save() -> None:
    """Cache defines save method."""
    from kbutillib.domains.notebook.cache import Cache  # noqa: PLC0415
    assert hasattr(Cache, "save")
    assert callable(Cache.save)


def test_cache_has_load() -> None:
    """Cache defines load method."""
    from kbutillib.domains.notebook.cache import Cache  # noqa: PLC0415
    assert hasattr(Cache, "load")
    assert callable(Cache.load)


def test_cache_has_exists() -> None:
    """Cache defines exists method."""
    from kbutillib.domains.notebook.cache import Cache  # noqa: PLC0415
    assert hasattr(Cache, "exists")
    assert callable(Cache.exists)


def test_cache_has_list() -> None:
    """Cache defines list method."""
    from kbutillib.domains.notebook.cache import Cache  # noqa: PLC0415
    assert hasattr(Cache, "list")
    assert callable(Cache.list)


# ---------------------------------------------------------------------------
# 3. ExperimentStore API contract
# ---------------------------------------------------------------------------


def test_experiment_store_has_register() -> None:
    """ExperimentStore defines register method."""
    from kbutillib.domains.notebook.experiment_store import ExperimentStore  # noqa: PLC0415
    assert hasattr(ExperimentStore, "register")
    assert callable(ExperimentStore.register)


def test_experiment_store_has_get() -> None:
    """ExperimentStore defines get method."""
    from kbutillib.domains.notebook.experiment_store import ExperimentStore  # noqa: PLC0415
    assert hasattr(ExperimentStore, "get")
    assert callable(ExperimentStore.get)


def test_experiment_store_has_list() -> None:
    """ExperimentStore defines list method."""
    from kbutillib.domains.notebook.experiment_store import ExperimentStore  # noqa: PLC0415
    assert hasattr(ExperimentStore, "list")
    assert callable(ExperimentStore.list)


# ---------------------------------------------------------------------------
# 4. NotebookSession structural API
# ---------------------------------------------------------------------------


def test_notebook_session_has_for_notebook() -> None:
    """NotebookSession defines for_notebook classmethod/method."""
    from kbutillib.domains.notebook.session import NotebookSession  # noqa: PLC0415
    assert hasattr(NotebookSession, "for_notebook")
    assert callable(NotebookSession.for_notebook)


def test_notebook_session_has_cache() -> None:
    """NotebookSession defines cache property/method."""
    from kbutillib.domains.notebook.session import NotebookSession  # noqa: PLC0415
    assert hasattr(NotebookSession, "cache")


def test_notebook_session_has_experiments() -> None:
    """NotebookSession defines experiments property/method."""
    from kbutillib.domains.notebook.session import NotebookSession  # noqa: PLC0415
    assert hasattr(NotebookSession, "experiments")
