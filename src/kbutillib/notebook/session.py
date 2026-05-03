"""NotebookSession — entry point for the notebook engine."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .cache import Cache
from .detect import detect_notebook_environment, detect_notebook_name
from .experiment_store import ExperimentStore, StrainStore
from .storage.blobs import BlobStore
from .storage.catalog import Catalog
from .storage.vectors import VectorStorage
from .vector_store import VectorStore


class NotebookSession:
    """Entry point. Holds catalog connection, paths, env detection.

    Usage::

        session = NotebookSession.for_notebook()
        session.cache.save("my_data", {"key": "value"})
        obj = session.cache.load("my_data")
    """

    def __init__(
        self,
        kbcache_dir: Path,
        notebook_name: Optional[str] = None,
        project_name: Optional[str] = None,
    ) -> None:
        self._kbcache_dir = Path(kbcache_dir)
        self._notebook_name = notebook_name
        self._project_name = project_name
        self.in_notebook = detect_notebook_environment()

        # Ensure .kbcache/ exists
        self._kbcache_dir.mkdir(parents=True, exist_ok=True)

        # Lazy-init backing stores
        self._catalog: Optional[Catalog] = None
        self._cache: Optional[Cache] = None
        self._vectors: Optional[VectorStore] = None
        self._experiments: Optional[ExperimentStore] = None
        self._strains: Optional[StrainStore] = None

    @classmethod
    def for_notebook(
        cls,
        notebook_file: Optional[str] = None,
        *,
        project_name: Optional[str] = None,
    ) -> "NotebookSession":
        """Detect notebook context, locate-or-create .kbcache/, open catalog.

        If *notebook_file* is not given, auto-detection is attempted.
        The ``.kbcache/`` directory is placed alongside the notebook
        (sibling of ``*.ipynb``), or in the current working directory
        if detection fails.
        """
        if notebook_file:
            nb_path = Path(notebook_file).resolve()
            notebook_name = nb_path.stem
            base_dir = nb_path.parent
        else:
            notebook_name = detect_notebook_name()
            base_dir = Path.cwd()

        kbcache_dir = base_dir / ".kbcache"
        return cls(
            kbcache_dir=kbcache_dir,
            notebook_name=notebook_name,
            project_name=project_name,
        )

    # ------------------------------------------------------------------
    # Properties (lazy init)
    # ------------------------------------------------------------------

    @property
    def notebook_name(self) -> Optional[str]:
        return self._notebook_name

    @property
    def kbcache_dir(self) -> Path:
        return self._kbcache_dir

    def _get_catalog(self) -> Catalog:
        if self._catalog is None:
            db_path = self._kbcache_dir / "catalog.sqlite"
            self._catalog = Catalog(db_path, project_name=self._project_name)
        return self._catalog

    @property
    def cache(self) -> Cache:
        if self._cache is None:
            blob_store = BlobStore(self._kbcache_dir / "blobs")
            self._cache = Cache(
                self._get_catalog(),
                blob_store,
                notebook_name=self._notebook_name,
            )
        return self._cache

    @property
    def vectors(self) -> VectorStore:
        if self._vectors is None:
            vec_storage = VectorStorage(self._kbcache_dir / "vectors")
            self._vectors = VectorStore(
                self._get_catalog(),
                vec_storage,
                notebook_name=self._notebook_name,
            )
        return self._vectors

    @property
    def experiments(self) -> ExperimentStore:
        if self._experiments is None:
            self._experiments = ExperimentStore(self._get_catalog())
        return self._experiments

    @property
    def strains(self) -> StrainStore:
        if self._strains is None:
            self._strains = StrainStore(self._get_catalog())
        return self._strains

    def close(self) -> None:
        """Close the catalog connection."""
        if self._catalog is not None:
            self._catalog.close()
            self._catalog = None
