"""NotebookSession — entry point for the notebook engine."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .cache import Cache
from .detect import detect_notebook_environment, detect_notebook_name
from .experiment_store import ExperimentStore, StrainStore
from .manifest import Manifest
from .schema.entity import EntityKind, EntityRef
from .schema.experiment import Computation
from .schema.validation import ValidationIssue, ValidationReport
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
        self._manifest: Optional[Manifest] = None

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

    @property
    def manifest(self) -> Manifest:
        if self._manifest is None:
            self._manifest = Manifest(self)
        return self._manifest

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_entities(self) -> ValidationReport:
        """Walk registered experiments, strains, and vectors; resolve EntityRefs.

        Returns a ValidationReport summarizing all issues found.
        """
        issues: list[ValidationIssue] = []
        checked_experiments = 0
        checked_strains = 0
        checked_vectors = 0
        checked_entity_refs = 0

        # Local cache of loaded namespace blobs (avoid redundant I/O)
        _ns_cache: dict[str, Any] = {}

        def _load_ns(namespace: str) -> Any:
            if namespace not in _ns_cache:
                _ns_cache[namespace] = self.cache.load(namespace, default=None)
            return _ns_cache[namespace]

        def _resolve_ref(ref: EntityRef, context: str) -> bool:
            """Attempt to resolve ref. Returns True on success, appends issue on failure."""
            nonlocal checked_entity_refs
            blob = _load_ns(ref.namespace)
            if blob is None:
                issues.append(
                    ValidationIssue(
                        kind="missing_namespace",
                        context=context,
                        ref=ref,
                        detail=f"Namespace blob {ref.namespace!r} not found in cache",
                    )
                )
                return False

            found = False
            if ref.kind == EntityKind.GENE:
                # MSGenome style: .features list
                if hasattr(blob, "features"):
                    found = any(
                        getattr(f, "id", None) == ref.id for f in blob.features
                    )
                elif hasattr(blob, "genes"):
                    found = any(
                        getattr(g, "id", None) == ref.id for g in blob.genes
                    )
                elif isinstance(blob, dict):
                    features = blob.get("features") or blob.get("genes") or []
                    found = any(
                        (item.get("id") if isinstance(item, dict) else getattr(item, "id", None)) == ref.id
                        for item in features
                    )
            elif ref.kind == EntityKind.REACTION:
                if hasattr(blob, "reactions"):
                    found = any(
                        getattr(r, "id", None) == ref.id for r in blob.reactions
                    )
                elif isinstance(blob, dict):
                    found = any(
                        (item.get("id") if isinstance(item, dict) else getattr(item, "id", None)) == ref.id
                        for item in blob.get("reactions", [])
                    )
            elif ref.kind == EntityKind.METABOLITE:
                if hasattr(blob, "metabolites"):
                    found = any(
                        getattr(m, "id", None) == ref.id for m in blob.metabolites
                    )
                elif isinstance(blob, dict):
                    found = any(
                        (item.get("id") if isinstance(item, dict) else getattr(item, "id", None)) == ref.id
                        for item in blob.get("metabolites", [])
                    )

            if found:
                checked_entity_refs += 1
                return True
            else:
                issues.append(
                    ValidationIssue(
                        kind="missing_entity",
                        context=context,
                        ref=ref,
                        detail=f"Entity {ref.id!r} ({ref.kind.value}) not found in namespace {ref.namespace!r}",
                    )
                )
                return False

        # --- Walk Strains ---
        for strain in self.strains.list():
            checked_strains += 1
            for i, mut in enumerate(strain.mutations):
                ctx = f"Strain {strain.id!r} mutation #{i} target"
                _resolve_ref(mut.target, ctx)

        # --- Walk Experiments ---
        all_experiments = self.experiments.list()
        exp_index: dict[str, Any] = {e.id: e for e in all_experiments}

        for exp in all_experiments:
            checked_experiments += 1

            # Check parents exist
            for pid in exp.parents:
                if pid not in exp_index:
                    issues.append(
                        ValidationIssue(
                            kind="missing_parent_experiment",
                            context=f"Experiment {exp.id!r} parent",
                            detail=f"Parent experiment {pid!r} not found in catalog",
                        )
                    )

            # Computation.derived_from_sample
            if isinstance(exp.payload, Computation) and exp.payload.derived_from_sample:
                dsid = exp.payload.derived_from_sample
                if dsid not in exp_index:
                    issues.append(
                        ValidationIssue(
                            kind="missing_derived_sample",
                            context=f"Computation {exp.id!r} derived_from_sample",
                            detail=f"Referenced experiment {dsid!r} not found in catalog",
                        )
                    )
                elif exp_index[dsid].kind != "sample":
                    issues.append(
                        ValidationIssue(
                            kind="wrong_kind",
                            context=f"Computation {exp.id!r} derived_from_sample",
                            detail=f"Expected kind='sample', got {exp_index[dsid].kind!r} for {dsid!r}",
                        )
                    )

        # --- Walk Vectors (namespace check only) ---
        for vec in self.vectors.list():
            checked_vectors += 1
            blob = _load_ns(vec.entity_namespace)
            if blob is None:
                issues.append(
                    ValidationIssue(
                        kind="missing_namespace",
                        context=f"Vector {vec.id!r} entity_namespace",
                        detail=f"Namespace blob {vec.entity_namespace!r} not found in cache",
                    )
                )

        return ValidationReport(
            issues=issues,
            checked_experiments=checked_experiments,
            checked_strains=checked_strains,
            checked_vectors=checked_vectors,
            checked_entity_refs=checked_entity_refs,
        )

    def close(self) -> None:
        """Close the catalog connection."""
        if self._catalog is not None:
            self._catalog.close()
            self._catalog = None
