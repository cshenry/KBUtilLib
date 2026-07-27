"""Notebook engine — provenanced cache, typed vectors, experiment tracking.

Public API::

    from kbutillib.notebook import NotebookSession

    session = NotebookSession.for_notebook()
    session.cache.save("name", obj)
    obj = session.cache.load("name")
"""

from .cache import Cache, CacheEntry
from .experiment_store import ExperimentStore, StrainStore
from .session import NotebookSession
from .vector_store import VectorStore

__all__ = [
    "NotebookSession",
    "Cache",
    "CacheEntry",
    "VectorStore",
    "ExperimentStore",
    "StrainStore",
]
