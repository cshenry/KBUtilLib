"""Community metabolic modeling utilities backed by the MSCommunity package.

This module wraps the standalone `mscommunity` package (community FBA, abundance
prediction, cross-feeding interactions, and Escher-map rendering) behind the
KBUtilLib composition architecture.  Everything here is a *skeleton* plus the
pure helpers and dependency gate: the solver-backed capabilities live in later
tasks.

Upstream APIs this module relies on
-----------------------------------
Pinned to https://github.com/ModelSEED/MSCommunity at commit
``2dcff16f8e20a5b2a2acbb28b60a8ad38ec924fc`` (2026-09-23).  The pin is a FLOOR:
before ``b14f50f`` (2026-09-01) a float abundance raised ``TypeError`` in
``MSCommunity.__init__`` and gapfill's solver string landed on ``MSGapfill``'s
``atp_gapfilling`` positional, silently running an ATP gapfill.  Do not develop
against an older checkout.

    mscommunity.commhelper.build_from_species_models(org_models, model_id=None,
        name=None, abundances=None, standardize=False, MSmodel=False,
        commkinetics=True, copy_models=True, climit=None, o2limit=None,
        printing=False) -> cobra.Model
        Copies each member model and rewrites compartments: member i (1-BASED)
        gets intracellular compartment ``c{i}``; the extracellular compartment
        is SHARED as ``e0``; member biomass reactions are renumbered
        ``bio2``, ``bio3``, ...

    mscommunity.mscommsim.MSCommunity(model=None, member_models=None,
        abundances=None, ids=None, kinetic_coeff=750, flux_limit=300,
        probs=None, climit=None, o2limit=None, lp_filename=None, printing=False,
        eleLimits=None, ID=None, close_member_drains=False)
        Attributes: ``.members`` (DictList of CommunityMember with ``.id``,
        ``.abundance``, ``.primary_biomass``, ``.biomass_cpd``, ``.index``,
        ``.biomass_drain_bounds``), ``.abundances``, ``.util`` (MSModelUtil),
        ``.solution``, ``.comm_growth``, ``.memGrowths``, ``.member_fluxes``,
        ``.exchange_fluxes``, ``.suboptimal_solution``, ``.primary_biomass``.
        Note ``member.biomass_drain_bounds`` is a ``(lower, upper)`` tuple
        snapshotted per member at construction (``mscommsim.py:153``).

    mscommunity.mscommviz has MODULE-LEVEL functions wrongly decorated with
        ``@staticmethod`` (``run_fba``, ``abundance_variability_analysis``,
        ``interactions``, ``visual_interactions``).  ``staticmethod`` objects
        are only directly callable on Python 3.10+; KBUtilLib supports >=3.9, so
        ALWAYS call them through :func:`_unwrap`.

CRITICAL HAZARD
---------------
``modelseedpy`` also ships a class named ``MSCommunity`` at
``modelseedpy/community/mscommunity.py``.  It is an older, DIVERGED copy exported
by ``from modelseedpy.community import *``.  It shares most method names, so
importing the wrong one produces plausible WRONG NUMBERS rather than an error.
:func:`_import_mscommunity` guards against it with a provenance gate.
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from kbutillib.core.errors import BackendUnavailableError, KBUtilLibError

from .kb_model_utils import KBModelUtils

__all__ = [
    "MSCommunityUtils",
    "MSCommunityUtilsImpl",
    "CommunityModel",
    "CommunityFBAResult",
    "CommunityMapArtifacts",
    "CommunityDependencyError",
    "CommunitySolverError",
    "CommunityVisualizationError",
]

logger = logging.getLogger(__name__)

# Pinned upstream commits (visibility only; NEVER enforced -- see the import
# helpers, which warn on mismatch but never raise or refuse the import).
PINNED_MSCOMMUNITY_COMMIT = "2dcff16f8e20a5b2a2acbb28b60a8ad38ec924fc"
PINNED_ESCHER_EDIT_COMMIT = "6474698c67e8b9a1e8ab44c38686b27821d20389"

# Marker strings surfaced in CommunityFBAResult.notes.
KINETICS_RELAXED_MARKER = "Kinetic constraints disabled"
NO_GROWTH_MARKER = "doesn't grow"

# Compiled compartment-suffix regex for project_member_fluxes.  Matched at the
# END of a reaction id only, so an id that merely contains "_c3" mid-string is
# never corrupted (unlike str.replace).
_COMPARTMENT_SUFFIX_RE = re.compile(r"_c(\d+)$")
_E0_SUFFIX_RE = re.compile(r"_e0$")
_BIOMASS_RE = re.compile(r"^bio\d+$")


# ── Exceptions ────────────────────────────────────────────────────────────────


class CommunityDependencyError(BackendUnavailableError):
    """Raised when the community-modeling backend dependency is unavailable.

    Subclasses :class:`~kbutillib.core.errors.BackendUnavailableError` (the same
    condition the cheminformatics and thermo backends already raise) so callers
    that catch ``BackendUnavailableError`` degrade gracefully rather than
    crashing ``import kbutillib``.
    """


class CommunitySolverError(KBUtilLibError):
    """Raised when a community FBA / optimization step fails at solve time."""


class CommunityVisualizationError(KBUtilLibError):
    """Raised when map rendering fails.

    Covers a missing rendering dependency (e.g. ``escher_edit``) or a community
    map that has no drawable members.
    """


# ── Import helpers ────────────────────────────────────────────────────────────


def _unwrap(fn: Any) -> Any:
    """Return the underlying function of a ``staticmethod``, else ``fn`` itself.

    ``mscommunity.mscommviz`` defines module-level functions wrongly decorated
    with ``@staticmethod``.  A bare ``staticmethod`` object is only directly
    callable on Python 3.10+; on Python 3.9 (which KBUtilLib still supports)
    ``staticmethod_obj(...)`` raises ``TypeError: 'staticmethod' object is not
    callable``.  Unwrapping to ``fn.__func__`` yields the plain function, which
    is callable on every supported interpreter.

    Args:
        fn: A callable, possibly a ``staticmethod`` object.

    Returns:
        ``fn.__func__`` when ``fn`` is a ``staticmethod``; otherwise ``fn``.
    """
    if isinstance(fn, staticmethod):
        return fn.__func__
    return fn


# Module-level cache of the resolved mscommunity module (or None).  A tri-state:
# `False` means "not yet resolved", `None` means "resolved as unavailable", and a
# module object means "resolved and importable + provenance-clean".
_MSCOMMUNITY_CACHE: Any = False
_ESCHER_EDIT_CACHE: Any = False


def _reset_mscommunity_cache() -> None:
    """Clear the cached import results (for tests that patch ``sys.modules``)."""
    global _MSCOMMUNITY_CACHE, _ESCHER_EDIT_CACHE
    _MSCOMMUNITY_CACHE = False
    _ESCHER_EDIT_CACHE = False


def _warn_commit_mismatch(package: str, repo_root: Path, pinned_sha: str) -> None:
    """Best-effort compare a checkout's HEAD SHA against the pin; warn on mismatch.

    Never raises: pinning here is for visibility, not enforcement, so a developer
    working ahead of the pin is not blocked.

    Args:
        package: Package name, for the log message.
        repo_root: Directory expected to be (or contain) the checkout's git root.
        pinned_sha: The commit SHA this module pins.
    """
    found_sha = _best_effort_head_sha(repo_root)
    if not found_sha:
        return
    # Compare on the shorter of the two lengths so a short vs full SHA still
    # matches when they share a prefix.
    n = min(len(found_sha), len(pinned_sha))
    if found_sha[:n].lower() != pinned_sha[:n].lower():
        logger.warning(
            "%s commit mismatch: pinned %s, found %s",
            package,
            pinned_sha,
            found_sha,
        )


def _best_effort_head_sha(repo_root: Path) -> Optional[str]:
    """Return a checkout's HEAD SHA, or ``None`` on any failure.

    Reads ``.git`` directly first (no subprocess), then falls back to
    ``git rev-parse HEAD``.  Both are wrapped so a missing/odd checkout simply
    yields ``None``.
    """
    try:
        git_dir = Path(repo_root) / ".git"
        head_file = git_dir / "HEAD"
        if head_file.is_file():
            head = head_file.read_text().strip()
            if head.startswith("ref:"):
                ref = head[len("ref:") :].strip()
                ref_file = git_dir / ref
                if ref_file.is_file():
                    return ref_file.read_text().strip()
                # Packed refs fallback.
                packed = git_dir / "packed-refs"
                if packed.is_file():
                    for line in packed.read_text().splitlines():
                        line = line.strip()
                        if not line or line.startswith(("#", "^")):
                            continue
                        sha, _, name = line.partition(" ")
                        if name == ref:
                            return sha
            elif head:
                # Detached HEAD stored as a raw SHA.
                return head
    except Exception:
        pass
    try:
        import subprocess

        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _import_mscommunity() -> Any:
    """Import and return the standalone ``mscommunity`` module, or ``None``.

    NEVER raises.  Resolution steps:

    1. Prefer an already-importable ``mscommunity`` on ``sys.path``.
    2. On ``ImportError``, resolve the checkout via ``dependencies.yaml`` and, if
       it contains a ``mscommunity`` package, insert its root at ``sys.path[0]``
       and retry.
    3. PROVENANCE GATE: accept only when the resolved ``MSCommunity`` class lives
       under the ``mscommunity.`` package; reject ``modelseedpy``'s superseded
       copy (which shares method names and would produce plausible wrong
       numbers).
    4. Best-effort commit-pin check (warn on mismatch, never raise).

    The result is cached module-level so repeated calls are cheap; use
    :func:`_reset_mscommunity_cache` to clear it in tests.

    Returns:
        The ``mscommunity`` module, or ``None`` if unavailable / provenance-bad.
    """
    global _MSCOMMUNITY_CACHE
    if _MSCOMMUNITY_CACHE is not False:
        return _MSCOMMUNITY_CACHE

    resolved_root: Optional[Path] = None
    module = None
    try:
        import mscommunity as module  # type: ignore
    except Exception:
        module = None

    if module is None:
        # Step 2: dependencies.yaml fallback (best-effort).
        try:
            from kbutillib.core.dependency_manager import get_dependency_path

            path = get_dependency_path("mscommunity")
        except Exception:
            path = None
        if path is not None:
            pkg_init = Path(path) / "mscommunity" / "__init__.py"
            if pkg_init.is_file():
                resolved_root = Path(path)
                if str(path) not in sys.path:
                    sys.path.insert(0, str(path))
                try:
                    import mscommunity as module  # type: ignore
                except Exception:
                    module = None

    if module is None:
        _MSCOMMUNITY_CACHE = None
        return None

    # Step 3: provenance gate on the resolved MSCommunity class.
    ms_class = _resolve_mscommunity_class(module)
    resolved_module = (
        getattr(ms_class, "__module__", "") if ms_class is not None else ""
    )
    if not resolved_module.startswith("mscommunity."):
        # Reject modelseedpy's superseded copy and anything else off-package.
        _MSCOMMUNITY_CACHE = None
        return None

    # Step 4: best-effort commit-pin check.
    if resolved_root is None:
        resolved_root = _module_repo_root(module)
    if resolved_root is not None:
        _warn_commit_mismatch("mscommunity", resolved_root, PINNED_MSCOMMUNITY_COMMIT)

    _MSCOMMUNITY_CACHE = module
    return module


def _resolve_mscommunity_class(module: Any) -> Any:
    """Return the ``MSCommunity`` class from the ``mscommunity`` module tree.

    Prefers a top-level ``MSCommunity`` attribute, then ``mscommsim.MSCommunity``.
    Returns ``None`` if neither can be resolved.
    """
    ms_class = getattr(module, "MSCommunity", None)
    if ms_class is not None:
        return ms_class
    try:
        from mscommunity import mscommsim  # type: ignore

        return getattr(mscommsim, "MSCommunity", None)
    except Exception:
        return None


def _module_repo_root(module: Any) -> Optional[Path]:
    """Return the checkout root for an imported package (its parent dir), or None."""
    try:
        module_file = getattr(module, "__file__", None)
        if module_file:
            # <root>/mscommunity/__init__.py -> <root>
            return Path(module_file).resolve().parent.parent
    except Exception:
        pass
    return None


def _import_escher_edit() -> Any:
    """Import and return the ``escher_edit`` module, or ``None``.

    NEVER raises.  Same shape as :func:`_import_mscommunity` with two
    differences:

    * ``editEscher`` uses a ``src/`` layout (repo ``editEscher``, distribution
      ``escher-edit``, import ``escher_edit`` under ``src/``), so the
      ``dependencies.yaml`` fallback inserts ``<path>/src`` -- NOT ``<path>`` --
      into ``sys.path``.
    * There is NO provenance gate: nothing else in the ecosystem exports the name
      ``escher_edit``, so the modelseedpy-style hazard does not exist here.

    Returns:
        The ``escher_edit`` module, or ``None`` if unavailable.
    """
    global _ESCHER_EDIT_CACHE
    if _ESCHER_EDIT_CACHE is not False:
        return _ESCHER_EDIT_CACHE

    resolved_root: Optional[Path] = None
    module = None
    try:
        import escher_edit as module  # type: ignore
    except Exception:
        module = None

    if module is None:
        try:
            from kbutillib.core.dependency_manager import get_dependency_path

            path = get_dependency_path("escher_edit")
        except Exception:
            path = None
        if path is not None:
            src_dir = Path(path) / "src"
            pkg_init = src_dir / "escher_edit" / "__init__.py"
            if pkg_init.is_file():
                resolved_root = Path(path)
                if str(src_dir) not in sys.path:
                    sys.path.insert(0, str(src_dir))
                try:
                    import escher_edit as module  # type: ignore
                except Exception:
                    module = None

    if module is None:
        _ESCHER_EDIT_CACHE = None
        return None

    if resolved_root is None:
        # <root>/src/escher_edit/__init__.py -> <root>
        try:
            module_file = getattr(module, "__file__", None)
            if module_file:
                resolved_root = Path(module_file).resolve().parent.parent.parent
        except Exception:
            resolved_root = None
    if resolved_root is not None:
        _warn_commit_mismatch("escher_edit", resolved_root, PINNED_ESCHER_EDIT_COMMIT)

    _ESCHER_EDIT_CACHE = module
    return module


def _mscommunity_unavailable_reason() -> Optional[str]:
    """Return ``None`` when ``mscommunity`` is usable, else an explanatory string.

    Two shapes:

    * not importable and not resolvable via ``dependencies.yaml``; or
    * a provenance rejection naming that the resolved class is modelseedpy's
      superseded copy -- this string CONTAINS the literal phrase
      ``"the superseded copy"`` (tests assert on it).
    """
    if _import_mscommunity() is not None:
        return None

    # Distinguish "not present at all" from "present but wrong provenance" by
    # probing what an unguarded import would resolve to.
    module = None
    try:
        import mscommunity as module  # type: ignore
    except Exception:
        module = None
    if module is None:
        try:
            from kbutillib.core.dependency_manager import get_dependency_path

            path = get_dependency_path("mscommunity")
        except Exception:
            path = None
        if path is not None and (Path(path) / "mscommunity" / "__init__.py").is_file():
            try:
                import mscommunity as module  # type: ignore
            except Exception:
                module = None

    if module is not None:
        ms_class = _resolve_mscommunity_class(module)
        resolved_module = (
            getattr(ms_class, "__module__", "") if ms_class is not None else ""
        )
        if not resolved_module.startswith("mscommunity."):
            return (
                "resolved MSCommunity is modelseedpy.community.MSCommunity (the "
                "superseded copy), not the standalone mscommunity package: "
                f"{resolved_module}"
            )

    return (
        "the mscommunity package is not importable and not resolvable via "
        "dependencies.yaml"
    )


def _escher_edit_unavailable_reason() -> Optional[str]:
    """Return ``None`` when ``escher_edit`` is usable, else an explanatory string."""
    if _import_escher_edit() is not None:
        return None
    return (
        "the escher_edit package is not importable and not resolvable via "
        "dependencies.yaml"
    )


# ── Pure helpers ──────────────────────────────────────────────────────────────


def normalize_abundances(abundances: dict[str, float]) -> dict[str, float]:
    """Return abundances rescaled to sum to 1.0.

    Args:
        abundances: Mapping of member id -> non-normalized abundance.

    Returns:
        A new dict with the same keys, each value divided by the total.

    Raises:
        ValueError: If ``abundances`` is empty or its total is non-positive.
    """
    if not abundances:
        raise ValueError("normalize_abundances: empty abundance mapping")
    total = sum(abundances.values())
    if total <= 0:
        raise ValueError(
            f"normalize_abundances: total abundance must be positive, got {total}"
        )
    return {k: v / total for k, v in abundances.items()}


def project_member_fluxes(
    fluxes: Mapping[str, float],
    member_index: int,
    member_biomass_id: Optional[str] = None,
) -> dict[str, float]:
    """Project community fluxes onto a single member's reaction namespace.

    Community models place member ``i`` (1-BASED) in intracellular compartment
    ``c{i}`` and share the extracellular compartment as ``e0``.  This rewrites
    the flux mapping into the member's own single-organism namespace:

    * ``_c{member_index}`` -> ``_c0`` (kept);
    * ``_c{j}`` for any other integer ``j`` -> dropped;
    * ``_e0`` (including ``EX_`` reactions) -> kept unchanged;
    * an id equal to ``member_biomass_id`` (e.g. ``"bio3"``) -> renamed to
      ``"bio1"`` (kept);
    * any other ``bio<digits>`` id -> dropped;
    * anything else -> kept unchanged.

    Args:
        fluxes: Mapping of community reaction id -> flux value.
        member_index: 1-based index of the member (compartment ``c{index}``).
        member_biomass_id: The member's renumbered community biomass id
            (e.g. ``"bio3"``), or ``None`` if no biomass rename is needed.

    Returns:
        A new dict in the member's single-organism reaction namespace.
    """
    result: dict[str, float] = {}
    for rxn_id, flux in fluxes.items():
        # Biomass ids first: they never carry a compartment suffix.
        if member_biomass_id is not None and rxn_id == member_biomass_id:
            result["bio1"] = flux
            continue
        if _BIOMASS_RE.match(rxn_id):
            # A different member's biomass -> drop.
            continue

        m = _COMPARTMENT_SUFFIX_RE.search(rxn_id)
        if m is not None:
            if int(m.group(1)) == member_index:
                result[rxn_id[: m.start()] + "_c0"] = flux
            # else: another member's intracellular reaction -> drop.
            continue

        # _e0 reactions (incl. EX_) and everything else -> keep unchanged.
        result[rxn_id] = flux
    return result


# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass
class CommunityModel:
    """A constructed community model plus the metadata needed to project it.

    Attributes:
        mscomm: The underlying ``mscommunity.mscommsim.MSCommunity`` instance.
        member_ids: Ordered member ids (index 0 -> community member 1).
        abundances: Normalized member abundances.
        source_model_ids: ``{member_id: source_model_id}`` (MEMBER FIRST) --
            maps each community member back to the single-organism model it was
            built from.
        kinetic_coeff: The community kinetics coefficient used at construction.
        abundances_were_supplied: Whether abundances came from the caller
            (vs. defaulted).
    """

    mscomm: Any
    member_ids: list[str]
    abundances: dict[str, float]
    source_model_ids: dict[str, str]
    kinetic_coeff: float
    abundances_were_supplied: bool = False

    def member_index(self, member_id: str) -> int:
        """Return the 1-based community index for ``member_id``.

        Args:
            member_id: A member id present in :attr:`member_ids`.

        Returns:
            The 1-based index (member ``i`` lives in compartment ``c{i}``).

        Raises:
            KeyError: If ``member_id`` is not a known member.
        """
        try:
            return self.member_ids.index(member_id) + 1
        except ValueError:
            raise KeyError(
                f"Unknown member id {member_id!r}; known members: {self.member_ids}"
            )


@dataclass
class CommunityMapArtifacts:
    """Paths and counts describing a rendered community Escher map.

    Attributes:
        map_json: Path to the Escher map JSON.
        svg: Path to a rendered SVG, or ``None``.
        html: Path to a rendered standalone HTML, or ``None``.
        n_members: Number of members drawn.
        n_compounds: Number of compounds drawn.
        n_blocks: Number of layout blocks emitted.
    """

    map_json: Path
    svg: Optional[Path]
    html: Optional[Path]
    n_members: int
    n_compounds: int
    n_blocks: int


@dataclass
class CommunityFBAResult:
    """Outcome of a community FBA run.

    Attributes:
        status: Solver status string.
        trustworthy: Whether the result is safe to report (e.g. kinetics were
            honored and the community actually grows).
        community_growth: Community-level growth rate (1/hr).
        member_growth: ``{member_id: growth_rate}``.
        exchange_fluxes: ``{reaction_id: flux}`` for community exchange reactions.
        fluxes: A ``pandas.Series`` indexed by string reaction id (float values,
            no NaNs).
        media_id: The media id used, or ``None``.
        kinetics_relaxed: Whether kinetic constraints were disabled for the run.
        notes: Free-form diagnostic notes (see ``KINETICS_RELAXED_MARKER`` /
            ``NO_GROWTH_MARKER``).
    """

    status: str
    trustworthy: bool
    community_growth: float
    member_growth: dict[str, float]
    exchange_fluxes: dict[str, float]
    fluxes: Any
    media_id: Optional[str]
    kinetics_relaxed: bool = False
    notes: list[str] = field(default_factory=list)


# ── Legacy implementation + composition wrapper ───────────────────────────────


class MSCommunityUtils(KBModelUtils):
    """Community metabolic-modeling utilities backed by the MSCommunity package.

    This is the legacy delegate class in the composition architecture; the
    solver-backed capabilities are added in later tasks.  See the module
    docstring for the pinned upstream MSCommunity APIs this relies on.
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialize community modeling utilities.

        Args:
            **kwargs: Additional keyword arguments passed to ``KBModelUtils``.
        """
        super().__init__(**kwargs)


class MSCommunityUtilsImpl:
    """Composition-based community modeling utilities.

    Holds ``env`` plus the sibling ``*Impl`` instances it composes with, and a
    legacy :class:`MSCommunityUtils` delegate built in a try/except so a missing
    optional dependency never crashes construction.  Availability additionally
    depends on the ``mscommunity`` package resolving (with correct provenance).
    """

    def __init__(self, env, model, fba, escher, biochem, **kwargs):
        self._env = env
        self._model = model
        self._fba = fba
        self._escher = escher
        self._biochem = biochem
        _kwargs = {
            "config_file": False,
            "token_file": None,
            "kbase_token_file": None,
        }
        try:
            _kwargs["token"] = env.get_token("kbase")
        except Exception:
            pass
        _kwargs.update(kwargs)
        try:
            self._delegate = MSCommunityUtils(**_kwargs)
        except Exception:
            self._delegate = None

    @property
    def env(self):
        return self._env

    @property
    def model(self):
        return self._model

    @property
    def fba(self):
        return self._fba

    @property
    def escher(self):
        return self._escher

    @property
    def biochem(self):
        return self._biochem

    @property
    def available(self) -> bool:
        """True only if the delegate initialized AND mscommunity is resolvable."""
        if self._delegate is None:
            return False
        return _mscommunity_unavailable_reason() is None

    @property
    def unavailable_reason(self):
        """Explain unavailability: dependency reason first, then delegate reason."""
        dep_reason = _mscommunity_unavailable_reason()
        if dep_reason is not None:
            return dep_reason
        if self._delegate is None:
            return "Optional dependencies cobra and/or modelseedpy are not installed"
        return None

    def __dir__(self) -> list:
        base = list(super().__dir__())
        if self._delegate is not None:
            try:
                delegate_attrs = [
                    a for a in dir(self._delegate) if not a.startswith("__")
                ]
                base.extend(a for a in delegate_attrs if a not in base)
            except Exception:
                pass
        return base

    def __getattr__(self, name):
        if self._delegate is None:
            raise RuntimeError(
                "MSCommunityUtilsImpl: delegate not initialized "
                "(missing cobrakbase/modelseedpy)"
            )
        return getattr(self._delegate, name)
