"""VerabUtils facade — unified entry point for the verAB methoxy-aromatic
Pickaxe rule-discovery and genome-screening workflow.

This module mirrors the composition pattern established by
:mod:`kbutillib.network_expansion_utils` (NetworkExpansionUtils /
NetworkExpansionUtilsImpl) and :mod:`kbutillib.thermo_utils`
(ThermoUtils / ThermoUtilsImpl):

* :class:`VerabUtils` is the standalone public facade.
* :class:`VerabUtilsImpl` is the composition wrapper registered on
  :class:`kbutillib.toolkit.KBUtilLib` as :attr:`~KBUtilLib.verab`.

Neither class imports RDKit or minedatabase at module load time.  Heavy
dependencies are deferred to the sub-package modules they belong to.

Lazy dependency resolution
--------------------------
The toolkit constructs ``VerabUtilsImpl`` on first access to ``kbu.verab``.
Because many of the dependent facades (``biochem``, ``model``, ``genome``,
``annotation``) have expensive or fallible constructors, dependencies are
resolved **lazily** — the facade is only constructed when a method that
requires it is first called.  In practice this means:

* The toolkit passes *getter callables* (lambdas that return the facade
  on first access of the respective toolkit property).
* Tests that construct ``VerabUtils`` or ``VerabUtilsImpl`` directly may
  pass either a resolved instance *or* a callable; both are supported.

Usage via the toolkit::

    kbu = KBUtilLib()
    # Phase 1 — rule discovery
    seeds = kbu.verab.seed_compounds()
    result = kbu.verab.discover_rules(generations=1)
    # Phase 2 — substructure scan + screening
    methoxy_cpds = kbu.verab.enumerate_methoxy_aromatics()
    report = kbu.verab.screen(rule_operators=result.operators, compounds=methoxy_cpds)
    # KING artifact emission
    paths = kbu.verab.emit_king_workflow("/tmp/king_run")
    # Introspection
    print(kbu.verab.status())
"""

from __future__ import annotations

import importlib.util
import logging
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from .cheminformatics.base import BackendUnavailableError
from .cheminformatics.verab.models import (
    ScreeningReport,
    VerabDiscoveryResult,
)
from .cheminformatics.verab.smarts import SEED_COMPOUNDS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RDKit / minedatabase availability probes (module-level cache, lazy)
# ---------------------------------------------------------------------------

_RDKIT_CHECKED: bool = False
_RDKIT_AVAILABLE: bool = False


def _rdkit_available() -> bool:
    """Return True iff ``rdkit`` can be imported (result cached after first call)."""
    global _RDKIT_CHECKED, _RDKIT_AVAILABLE
    if not _RDKIT_CHECKED:
        _RDKIT_AVAILABLE = importlib.util.find_spec("rdkit") is not None
        _RDKIT_CHECKED = True
    return _RDKIT_AVAILABLE


def _minedatabase_available() -> bool:
    """Return True iff ``minedatabase`` (pickaxe) can be found on sys.path."""
    return importlib.util.find_spec("minedatabase") is not None


# ---------------------------------------------------------------------------
# Internal: lazy-resolve a dependency that may be a value or a getter callable
# ---------------------------------------------------------------------------

# A "DependencySource" is either the resolved value (any object) or a callable
# that returns the resolved value on first call.  Using callables lets the
# toolkit defer construction of expensive facades until they are actually needed.
_DependencySource = Union[Any, Callable[[], Any]]


def _resolve(dep: Optional[_DependencySource]) -> Any:
    """Return the resolved dependency.

    * If *dep* is ``None``, returns ``None``.
    * If *dep* is callable, calls it and returns the result.
    * Otherwise returns *dep* directly.
    """
    if dep is None:
        return None
    if callable(dep):
        return dep()
    return dep


# ---------------------------------------------------------------------------
# VerabUtils — standalone public facade
# ---------------------------------------------------------------------------


class VerabUtils:
    """Backend-agnostic facade for the verAB methoxy-aromatic rule-discovery
    and genome-screening workflow.

    Composes the five KBUtilLib sub-utilities needed for the full pipeline:

    * ``network_expansion`` — PickaxeBackend-based compound-network expansion.
    * ``biochem``           — ModelSEED biochemistry DB (compound/reaction lookup).
    * ``model``             — Metabolic model utility (pathway membership checks).
    * ``genome``            — Genome utility (workspace genome access).
    * ``annotation``        — Annotation utility (EC/ontology term resolution).

    All dependencies are duck-typed collaborators; pass fakes (or callables
    returning fakes) in tests.

    Args:
        network_expansion: Resolved facade *or* a callable returning one (lazy).
        biochem: Resolved facade *or* a callable returning one (lazy).
        model: Resolved facade *or* a callable returning one (lazy).
        genome: Resolved facade *or* a callable returning one (lazy).
        annotation: Resolved facade *or* a callable returning one (lazy).
    """

    def __init__(
        self,
        network_expansion: Optional[_DependencySource] = None,
        biochem: Optional[_DependencySource] = None,
        model: Optional[_DependencySource] = None,
        genome: Optional[_DependencySource] = None,
        annotation: Optional[_DependencySource] = None,
    ) -> None:
        # Store as-is; resolve lazily on first use via _get_* helpers below.
        self._network_expansion_src = network_expansion
        self._biochem_src = biochem
        self._model_src = model
        self._genome_src = genome
        self._annotation_src = annotation

        # Resolved-instance cache (None = not yet resolved)
        self.__ne: Any = _UNSET
        self.__biochem: Any = _UNSET
        self.__model: Any = _UNSET
        self.__genome: Any = _UNSET
        self.__annotation: Any = _UNSET

    # ── lazy dependency accessors ─────────────────────────────────────────

    def _get_network_expansion(self) -> Any:
        if self.__ne is _UNSET:
            self.__ne = _resolve(self._network_expansion_src)
        return self.__ne

    def _get_biochem(self) -> Any:
        if self.__biochem is _UNSET:
            self.__biochem = _resolve(self._biochem_src)
        return self.__biochem

    def _get_model(self) -> Any:
        if self.__model is _UNSET:
            self.__model = _resolve(self._model_src)
        return self.__model

    def _get_genome(self) -> Any:
        if self.__genome is _UNSET:
            self.__genome = _resolve(self._genome_src)
        return self.__genome

    def _get_annotation(self) -> Any:
        if self.__annotation is _UNSET:
            self.__annotation = _resolve(self._annotation_src)
        return self.__annotation

    # ── Phase 1 ─────────────────────────────────────────────────────────

    def seed_compounds(self) -> List[Dict[str, Any]]:
        """Return the 5 canonical verAB seed compound dicts.

        Returns:
            List of dicts with keys ``id``, ``name``, ``smiles``,
            ``inchikey``, ``kegg``.
        """
        return list(SEED_COMPOUNDS)

    def discover_rules(
        self,
        *,
        generations: int = 1,
        rule_set: str = "metacyc_generalized",
        seeds: Optional[Sequence[Dict[str, Any]]] = None,
        backend: str = "pickaxe",
    ) -> VerabDiscoveryResult:
        """Expand seed compounds and identify verAB O-demethylation operators.

        Requires the ``network_expansion`` dependency (PickaxeBackend) and —
        for RDKit-confirmed matching — RDKit.  Degrades to text-only matching
        when RDKit is absent.

        Args:
            generations: Number of Pickaxe expansion generations.
            rule_set: Pickaxe rule-set identifier.
            seeds: Override the default 5-compound seed list.
            backend: Expansion backend to use (passed to the expander).

        Returns:
            A :class:`~kbutillib.cheminformatics.verab.models.VerabDiscoveryResult`.

        Raises:
            BackendUnavailableError: If ``network_expansion`` is not set or
                Pickaxe is unavailable.
        """
        ne = self._get_network_expansion()
        if ne is None:
            raise BackendUnavailableError(
                "VerabUtils.discover_rules requires a network_expansion "
                "facade (e.g. kbu.network_expansion).  None was provided."
            )

        from .cheminformatics.verab.rule_discovery import discover_verab_rules

        return discover_verab_rules(
            ne,
            generations=generations,
            rule_set=rule_set,
            seeds=seeds,
            backend=backend,
        )

    def emit_king_workflow(
        self,
        outdir: Any,
        *,
        discovery: Optional[VerabDiscoveryResult] = None,
    ) -> Dict[str, Any]:
        """Write a reproducible KING coscientist input directory.

        If *discovery* is ``None`` an empty :class:`VerabDiscoveryResult` with
        the default rule_set/seeds is used (seeds only; no operators).

        Args:
            outdir: Destination directory path (str or Path). Created if absent.
            discovery: A completed :class:`VerabDiscoveryResult`, or ``None``
                to write a seed-only directory.

        Returns:
            Dict with keys ``outdir``, ``files``, ``n_operators``, ``n_seeds``.
        """
        from .cheminformatics.verab.king_artifacts import emit_king_workflow

        if discovery is None:
            discovery = VerabDiscoveryResult(
                rule_set="metacyc_generalized",
                generations=1,
                seeds=list(SEED_COMPOUNDS),
            )

        return emit_king_workflow(outdir, discovery, seeds=discovery.seeds or None)

    # ── Phase 2 ─────────────────────────────────────────────────────────

    def enumerate_methoxy_aromatics(
        self,
        *,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Scan the biochem DB for methoxy-aromatic compounds via RDKit substructure.

        Requires RDKit to be installed.  Raises :class:`BackendUnavailableError`
        when RDKit is absent.

        Args:
            limit: Stop after *limit* hits (None = full scan).

        Returns:
            List of compound dicts with keys ``id``, ``name``, ``smiles``,
            ``formula``.

        Raises:
            BackendUnavailableError: If RDKit is not installed, or if the
                ``biochem`` dependency is not set.
        """
        if not _rdkit_available():
            raise BackendUnavailableError(
                "VerabUtils.enumerate_methoxy_aromatics requires RDKit. "
                "Install with `conda install -c conda-forge rdkit` or "
                "`pip install rdkit`."
            )

        biochem = self._get_biochem()
        if biochem is None:
            raise BackendUnavailableError(
                "VerabUtils.enumerate_methoxy_aromatics requires a biochem "
                "facade.  None was provided."
            )

        from .cheminformatics.verab.substructure import MethoxyAromaticFilter

        f = MethoxyAromaticFilter()
        result = f.enumerate_from_biochem(biochem, limit=limit)
        return result.get("compounds", [])

    def screen(
        self,
        *,
        rule_operators: Optional[Sequence[str]] = None,
        compounds: Optional[Sequence[Dict[str, Any]]] = None,
        genomes: Optional[Sequence[Any]] = None,
        generations: int = 1,
    ) -> ScreeningReport:
        """Run discovered rule(s) on methoxy-aromatic compounds and answer
        the four Phase-2 questions for each predicted product.

        Args:
            rule_operators: Operator/rule ids discovered by :meth:`discover_rules`.
            compounds: Methoxy-aromatic compound dicts (each with ``"id"`` and
                ``"smiles"``).  Defaults to the 5 SEED_COMPOUNDS if not provided.
            genomes: Sequence of genome objects for degradation prediction.
                Pass ``None`` to skip genome-level prediction.
            generations: Number of expansion generations.

        Returns:
            A :class:`~kbutillib.cheminformatics.verab.models.ScreeningReport`.

        Raises:
            BackendUnavailableError: If ``network_expansion`` or ``biochem``
                is not available.
        """
        ne = self._get_network_expansion()
        if ne is None:
            raise BackendUnavailableError(
                "VerabUtils.screen requires a network_expansion facade.  "
                "None was provided."
            )

        biochem = self._get_biochem()
        if biochem is None:
            raise BackendUnavailableError(
                "VerabUtils.screen requires a biochem facade.  "
                "None was provided."
            )

        from .cheminformatics.verab import screening

        cpds = list(compounds) if compounds is not None else list(SEED_COMPOUNDS)
        ops = list(rule_operators) if rule_operators is not None else []
        model = self._get_model()

        report = screening.screen_products(
            expander=ne,
            operators=ops,
            compounds=cpds,
            biochem=biochem,
            models=[model] if model is not None else None,
            generations=generations,
        )

        # Optionally attach genome predictions
        if genomes:
            genome_preds = screening.predict_genome_degradation(
                operators=ops,
                ec_hint="1.14.13.82",
                genomes=genomes,
                annotation=self._get_annotation(),
            )
            report.genome_predictions = genome_preds

        return report

    # ── Introspection ────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        """Return a diagnostic dict describing what is available.

        Keys:
            ``rdkit``              — bool, RDKit importable.
            ``minedatabase``       — bool, minedatabase importable.
            ``network_expansion``  — bool, expander dependency configured.
            ``biochem``            — bool, biochem dependency configured.
            ``model``              — bool, model dependency configured.
            ``genome``             — bool, genome dependency configured.
            ``annotation``         — bool, annotation dependency configured.
            ``seed_count``         — int, number of canonical seed compounds.
            ``backends``           — dict from the expander's ``backend_status()``
              if available, else ``{}``.

        Note: ``True`` means the dependency source is configured (either a
        resolved instance or a getter callable was provided); it does NOT
        guarantee the dependency will construct without error.
        """
        # Check whether each source is configured (non-None) WITHOUT resolving it
        ne_configured = self._network_expansion_src is not None
        biochem_configured = self._biochem_src is not None
        model_configured = self._model_src is not None
        genome_configured = self._genome_src is not None
        annotation_configured = self._annotation_src is not None

        backends: Dict[str, Any] = {}
        # Only try to get backend_status if the network_expansion source is already
        # resolved (i.e. was passed as a direct instance and is cached).
        if self.__ne is not _UNSET and self.__ne is not None:
            ne = self.__ne
            if hasattr(ne, "backend_status"):
                try:
                    backends = ne.backend_status()
                except Exception as exc:
                    logger.debug("backend_status() failed: %s", exc)

        return {
            "rdkit": _rdkit_available(),
            "minedatabase": _minedatabase_available(),
            "network_expansion": ne_configured,
            "biochem": biochem_configured,
            "model": model_configured,
            "genome": genome_configured,
            "annotation": annotation_configured,
            "seed_count": len(SEED_COMPOUNDS),
            "backends": backends,
        }


# Sentinel for "not yet resolved" — distinct from None (which means "not available")
class _UnsetType:
    """Sentinel class used to distinguish 'not yet resolved' from None."""

    _instance: Optional["_UnsetType"] = None

    def __new__(cls) -> "_UnsetType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<UNSET>"


_UNSET = _UnsetType()


# ---------------------------------------------------------------------------
# VerabUtilsImpl — composition wrapper for KBUtilLib toolkit
# ---------------------------------------------------------------------------


class VerabUtilsImpl:
    """Composition wrapper for :class:`VerabUtils`.

    Holds ``env`` instead of inheriting from :class:`~kbutillib.shared_env_utils.SharedEnvUtils`,
    matching the pattern used by :class:`~kbutillib.network_expansion_utils.NetworkExpansionUtilsImpl`
    and :class:`~kbutillib.thermo_utils.ThermoUtilsImpl`.  Delegates all other
    attribute access to an internal :class:`VerabUtils` instance.

    Accepts either resolved instances or *getter callables* for each dependency
    so that expensive toolkit facades are only constructed on first use.

    Args:
        env: A :class:`~kbutillib.shared_env_utils.SharedEnvUtils` instance.
        network_expansion: Resolved facade or callable → resolved facade.
        biochem: Resolved facade or callable → resolved facade.
        model: Resolved facade or callable → resolved facade.
        genome: Resolved facade or callable → resolved facade.
        annotation: Resolved facade or callable → resolved facade.
    """

    def __init__(
        self,
        env: Any,
        *,
        network_expansion: Optional[_DependencySource] = None,
        biochem: Optional[_DependencySource] = None,
        model: Optional[_DependencySource] = None,
        genome: Optional[_DependencySource] = None,
        annotation: Optional[_DependencySource] = None,
    ) -> None:
        self._env = env
        self._delegate = VerabUtils(
            network_expansion=network_expansion,
            biochem=biochem,
            model=model,
            genome=genome,
            annotation=annotation,
        )

    @property
    def env(self) -> Any:
        return self._env

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)
