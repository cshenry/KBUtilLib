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

import contextlib
import io
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from kbutillib.core.capability import capability
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

    # ── Private accessors ──────────────────────────────────────────────────

    def _require_mscommunity(self) -> Any:
        """Return the resolved ``mscommunity`` module, or raise if unavailable.

        Raises:
            CommunityDependencyError: with :func:`_mscommunity_unavailable_reason`
                as its message when the package is absent or provenance-bad.
        """
        module = _import_mscommunity()
        if module is None:
            raise CommunityDependencyError(_mscommunity_unavailable_reason())
        return module

    def _require_escher_edit(self) -> Any:
        """Return the resolved ``escher_edit`` module, or raise if unavailable.

        Raises:
            CommunityVisualizationError: naming ``escher_edit`` and its
                ``dependencies.yaml`` entry when the package cannot be resolved.
        """
        module = _import_escher_edit()
        if module is None:
            raise CommunityVisualizationError(
                "escher_edit is not available; the community figure cannot be "
                "rendered. Install it or declare its checkout under the "
                "'escher_edit' entry in dependencies.yaml. "
                f"({_escher_edit_unavailable_reason()})"
            )
        return module

    def _resolve_member_model(self, member):
        """Resolve one community-member input into a cobra model in COMMUNITY order.

        ``member_models`` accepts cobra models, ``MSModelUtil`` objects, or
        workspace refs (strings), resolved exactly as the rest of the modeling
        domain does: strings go through :meth:`get_model` and everything else
        through :meth:`_check_and_convert_model`.  The returned object is the raw
        ``cobra.Model`` (``MSModelUtil.model``), which is what
        ``build_from_species_models`` merges.

        Args:
            member: A cobra model, ``MSModelUtil``, or workspace ref string.

        Returns:
            A ``cobra.Model``.
        """
        if isinstance(member, str):
            mdlutl = self.get_model(member)
        else:
            mdlutl = self._check_and_convert_model(member)
        return mdlutl.model

    # ── Construction ───────────────────────────────────────────────────────

    @capability(
        domain="community",
        summary="Merge single-organism models into a community metabolic model.",
        tags=("community", "construction"),
        visibility="public",
    )
    def build_community(
        self,
        member_models,
        abundances=None,
        model_id=None,
        name=None,
        kinetic_coeff=750,
        element_limits=None,
        printing=False,
        build_solver="glpk",
        final_solver=None,
    ) -> "CommunityModel":
        """Build a community model from single-organism member models.

        The input models are copied and merged by
        ``mscommunity.commhelper.build_from_species_models``, which renames each
        member's compartments (member ``i`` -> ``c{i}``, shared ``e0``) and
        renumbers biomass reactions (``bio2``, ``bio3``, ...).  Because that copy
        destroys the mapping from input model to community member, each input
        model's id is captured BEFORE the merge into ``source_model_ids``.

        ``close_member_drains`` is DERIVED, never a free parameter: it is set to
        ``abundances is not None``.  Declaring an abundance vector is the same
        statement as "make it bind", and with a member's biomass drain open the
        member can synthesise biomass and discard it, inflating the CommKinetics
        right-hand side (upstream measured 83% of one member's biomass leaving
        through the drain).

        Args:
            member_models: List of cobra models, ``MSModelUtil`` objects, or
                workspace ref strings.
            abundances: Optional ``{member_id: abundance}``.  When supplied it is
                normalized with :func:`normalize_abundances` and made to bind;
                when omitted upstream defaults to a uniform split.
            model_id: Optional community model id (becomes the cobra model's
                ``.id`` and is echoed into the provenance notes).
            name: Optional community model name (becomes the cobra model's
                ``.name`` and is echoed into the provenance notes).
            kinetic_coeff: Community kinetics coefficient (default 750).
            element_limits: Optional per-element uptake limits (upstream
                ``eleLimits``).
            printing: Forwarded to upstream for verbose construction output.
            build_solver: Solver used while populating the merged model
                (default ``"glpk"``); forwarded to ``build_from_species_models``.
                Upstream builds under GLPK then does one clean rebuild because
                incremental constraint addition into optlang's Gurobi interface is
                superlinear.
            final_solver: Solver installed on the finished model, or ``None`` to
                keep the cobra default; forwarded to ``build_from_species_models``.

        Returns:
            A :class:`CommunityModel` handle.

        Raises:
            CommunityDependencyError: If ``mscommunity`` is unavailable.
        """
        self._require_mscommunity()
        from mscommunity.commhelper import build_from_species_models  # noqa: WPS433
        from mscommunity.mscommsim import MSCommunity  # noqa: WPS433

        # Capture each input model's id BEFORE the merge copies and renames them.
        cobra_models = []
        input_ids: list[str] = []
        for member in member_models:
            cobra_model = self._resolve_member_model(member)
            cobra_models.append(cobra_model)
            input_ids.append(cobra_model.id)

        abundances_were_supplied = abundances is not None
        norm_abundances = (
            normalize_abundances(dict(abundances))
            if abundances_were_supplied
            else None
        )

        # Forward the solver arguments through build_from_species_models (the
        # MSCommunity constructor does not accept them), then hand the finished
        # model to MSCommunity(model=...).  This also lets model_id/name land on
        # the cobra model's .id/.name.
        merged = build_from_species_models(
            cobra_models,
            model_id=model_id,
            name=name,
            abundances=norm_abundances,
            printing=printing,
            build_solver=build_solver,
            final_solver=final_solver,
        )

        comm = MSCommunity(
            model=merged,
            abundances=norm_abundances,
            ids=input_ids,
            kinetic_coeff=kinetic_coeff,
            eleLimits=element_limits,
            printing=printing,
            ID=model_id,
            close_member_drains=abundances_were_supplied,
        )

        # member_ids in COMMUNITY INDEX ORDER; assert consistency with each
        # member's .index so member_index() is trustworthy.
        member_ids = [m.id for m in comm.members]
        for i, m in enumerate(comm.members, start=1):
            member_index = getattr(m, "index", i)
            assert member_index == i, (
                f"member {m.id!r} reports index {member_index} but is at community "
                f"position {i}; member_index() would be unreliable"
            )

        # Map each community member back to the source model it was built from.
        # Community order follows the abundance/notes iteration order, which is
        # the input order preserved through ids=input_ids.
        source_model_ids = {
            mid: sid for mid, sid in zip(member_ids, input_ids)
        }

        handle = CommunityModel(
            mscomm=comm,
            member_ids=member_ids,
            abundances=dict(comm.abundances),
            source_model_ids=source_model_ids,
            kinetic_coeff=kinetic_coeff,
            abundances_were_supplied=abundances_were_supplied,
        )
        return handle

    @capability(
        domain="community",
        summary="Load and re-wrap a previously saved community model.",
        tags=("community", "construction"),
        visibility="public",
    )
    def load_community(
        self,
        id_or_ref,
        ws=None,
        member_ids=None,
    ) -> "CommunityModel":
        """Load a saved community model and reconstruct its provenance.

        The model is loaded via :meth:`get_model` and wrapped as
        ``MSCommunity(model=..., ids=member_ids)``.  Provenance is recovered in a
        fixed order, and ALL THREE routes are attempted before raising:

        0. Validate ``kbutil.community`` schema_version first (accept ``1``).
        1. ``model.notes["kbutil.community"]`` -- our own JSON blob, which also
           restores ``source_model_ids``, ``abundances`` and ``kinetic_coeff``.
        2. The explicit ``member_ids`` argument.
        3. Upstream's ``model.notes["member_biomass_cpds"]`` (a dict keyed by
           model id).

        A handle rebuilt from route 2 or 3 has an EMPTY ``source_model_ids``;
        that is expected, and :meth:`render_member_map` reports it as a named
        ``ValueError`` rather than a traceback.

        Args:
            id_or_ref: Workspace ref / id of the saved community model.
            ws: Optional workspace override.
            member_ids: Optional explicit ordered member ids (recovery route 2).

        Returns:
            A :class:`CommunityModel` handle.

        Raises:
            ValueError: If the ``kbutil.community`` schema_version is unsupported,
                or if none of the three recovery routes yields member ids.
            CommunityDependencyError: If ``mscommunity`` is unavailable.
        """
        self._require_mscommunity()

        mdlutl = self.get_model(id_or_ref, ws)
        model = mdlutl.model
        notes = getattr(model, "notes", {}) or {}

        recovered_member_ids: Optional[list[str]] = None
        source_model_ids: dict[str, str] = {}
        abundances: dict[str, float] = {}
        kinetic_coeff: float = 750
        abundances_were_supplied = False

        # Route 1: our own JSON provenance blob (schema-validated first).
        raw_blob = notes.get("kbutil.community")
        if raw_blob:
            blob = json.loads(raw_blob) if isinstance(raw_blob, str) else raw_blob
            schema_version = blob.get("schema_version")
            if schema_version != 1:
                raise ValueError(
                    f"Unsupported kbutil.community schema_version: {schema_version}"
                )
            recovered_member_ids = list(blob.get("member_ids") or []) or None
            source_model_ids = dict(blob.get("source_model_ids") or {})
            abundances = dict(blob.get("abundances") or {})
            kinetic_coeff = blob.get("kinetic_coeff", 750)
            abundances_were_supplied = bool(blob.get("abundances_were_supplied", False))

        # Route 2: explicit member_ids argument.
        if recovered_member_ids is None and member_ids is not None:
            recovered_member_ids = list(member_ids)

        # Route 3: upstream's member_biomass_cpds (dict keyed by model id).
        if recovered_member_ids is None:
            member_biomass_cpds = notes.get("member_biomass_cpds")
            if member_biomass_cpds:
                recovered_member_ids = list(member_biomass_cpds.keys())

        if recovered_member_ids is None:
            raise ValueError(
                "Cannot reconstruct community provenance: no kbutil.community "
                "notes, no member_ids argument, and no member_biomass_cpds in "
                "model.notes."
            )

        from mscommunity.mscommsim import MSCommunity  # noqa: WPS433

        comm = MSCommunity(model=model, ids=recovered_member_ids)
        live_member_ids = [m.id for m in comm.members]
        if not abundances:
            abundances = dict(comm.abundances)
        if not kinetic_coeff:
            kinetic_coeff = getattr(comm, "kinCoef", 750)

        return CommunityModel(
            mscomm=comm,
            member_ids=live_member_ids,
            abundances=abundances,
            source_model_ids=source_model_ids,
            kinetic_coeff=kinetic_coeff,
            abundances_were_supplied=abundances_were_supplied,
        )

    @capability(
        domain="community",
        summary="Persist a community model with kbutil.community provenance.",
        tags=("community", "construction"),
        visibility="public",
    )
    def save_community(
        self,
        comm: "CommunityModel",
        workspace=None,
        objid=None,
        suffix=None,
    ):
        """Save a community model, writing ``kbutil.community`` provenance first.

        Persisting the captured ``member_ids`` / ``source_model_ids`` is
        load-bearing, not bookkeeping: :meth:`build_community` captures them
        precisely because upstream's merge destroys them, and without persisting
        the capture a save/load cycle throws it away again -- after which
        :meth:`render_member_map` cannot resolve a member's source model.  The
        blob is stored as a JSON STRING because workspace notes values are not
        reliably structured.

        The model is saved through the existing :meth:`save_model` path as an
        ordinary ``KBaseFBA.FBAModel`` (a community model IS an FBA model; a new
        workspace type would be invisible to every tool that already reads
        models).

        Args:
            comm: The :class:`CommunityModel` to save.
            workspace: Optional workspace to save into (no-workspace returns the
                model data, per :meth:`save_model`).
            objid: Optional object id.
            suffix: Optional id suffix.

        Returns:
            Whatever :meth:`save_model` returns.

        Raises:
            CommunityDependencyError: If ``mscommunity`` is unavailable.
        """
        self._require_mscommunity()
        model = comm.mscomm.util.model
        provenance = {
            "schema_version": 1,
            "member_ids": list(comm.member_ids),
            "source_model_ids": dict(comm.source_model_ids),
            "abundances": dict(comm.abundances),
            "kinetic_coeff": comm.kinetic_coeff,
            "abundances_were_supplied": comm.abundances_were_supplied,
        }
        if model.notes is None:
            model.notes = {}
        model.notes["kbutil.community"] = json.dumps(provenance)

        mdlutl = self._check_and_convert_model(model)
        if objid is None:
            objid = comm.mscomm.id
        return self.save_model(mdlutl, workspace=workspace, objid=objid, suffix=suffix)

    @capability(
        domain="community",
        summary="Export a community model to an SBML file.",
        tags=("community", "construction"),
        visibility="public",
    )
    def export_community_sbml(
        self,
        comm: "CommunityModel",
        path,
        overwrite=True,
    ) -> Path:
        """Export a community model to SBML at ``path``.

        Delegates to ``comm.mscomm.to_sbml``, which already creates parent
        directories.  Overwrites by default.

        Args:
            comm: The :class:`CommunityModel` to export.
            path: Destination SBML path.
            overwrite: When ``False``, raise if ``path`` already exists.

        Returns:
            The destination :class:`~pathlib.Path`.

        Raises:
            FileExistsError: If ``overwrite`` is ``False`` and ``path`` exists.
            CommunityDependencyError: If ``mscommunity`` is unavailable.
        """
        self._require_mscommunity()
        out_path = Path(path)
        if not overwrite and out_path.exists():
            raise FileExistsError(
                f"Refusing to overwrite existing SBML file {out_path} "
                "(pass overwrite=True to replace it)."
            )
        comm.mscomm.to_sbml(str(out_path))
        return out_path

    # ── Simulation ─────────────────────────────────────────────────────────

    def _resolve_media(self, media):
        """Resolve a media id/name/object into a media object (or ``None``).

        Args:
            media: A media object, an id/name string, or ``None``.

        Returns:
            The resolved media object, or ``None`` when ``media`` is ``None``.
        """
        if media is None:
            return None
        if isinstance(media, str):
            return self.get_media(media)
        return media

    @capability(
        domain="community",
        summary="Run community FBA and surface trustworthiness + relaxed kinetics.",
        tags=("community", "simulation", "fba"),
        visibility="public",
    )
    def run_community_fba(
        self,
        comm: "CommunityModel",
        media=None,
        pfba=False,
        min_member_growth=0.0,
    ) -> "CommunityFBAResult":
        """Run community FBA and return a trustworthiness-aware result.

        There is deliberately NO ``fva_reactions`` argument: upstream accepts one
        but its result shape is unspecified and :class:`CommunityFBAResult` has no
        field for it; callers who need it use
        ``comm.mscomm.run_fba(..., fva_reactions=...)`` directly.

        MSCommunity announces its most consequential silent behaviour by
        ``print()``, not by raising: on a zero-growth community it removes the
        ``_commKin`` constraints, re-solves, and PROCEEDS with the
        constraint-free result, printing ``"Kinetic constraints disabled ..."``.
        The delegate call is wrapped in :func:`contextlib.redirect_stdout`; the
        captured text is scanned for :data:`KINETICS_RELAXED_MARKER` and
        :data:`NO_GROWTH_MARKER`, matches are re-emitted through logging and
        appended to ``result.notes``, and :attr:`CommunityFBAResult.kinetics_relaxed`
        is set when the kinetics marker appears.  Non-matching output is logged at
        DEBUG and discarded (this also keeps upstream's print noise out of
        notebooks).

        Args:
            comm: The :class:`CommunityModel` to simulate.
            media: A media object, id/name, or ``None`` (model default).
            pfba: Whether to run pFBA.
            min_member_growth: SteadyCom-style per-member growth floor.  Default
                ``0.0`` means NO floor (upstream's historical default of 1 never
                reached the LP, so enforcing one would silently change every
                existing result).  When positive, the per-member constraint is
                installed inside the model's context manager so it does not leak.

        Returns:
            A :class:`CommunityFBAResult`.

        Raises:
            CommunitySolverError: If ``min_member_growth > 0`` is combined with a
                community built from caller-supplied fixed abundances (an
                over-determined system).
            CommunityDependencyError: If ``mscommunity`` is unavailable.
        """
        self._require_mscommunity()

        # OVER-SPECIFICATION REFUSAL: fixing composition AND imposing a per-member
        # growth floor over-determines the system.  A named refusal is far better
        # than an infeasible LP.
        if min_member_growth > 0 and comm.abundances_were_supplied:
            raise CommunitySolverError(
                "run_community_fba: min_member_growth="
                f"{min_member_growth} was requested on a community built with "
                "caller-supplied fixed abundances (abundances_were_supplied=True). "
                "Fixing composition while also imposing a per-member growth floor "
                "over-determines the system; drop either the growth floor "
                "(min_member_growth=0) or the fixed abundances to resolve this."
            )

        media_obj = self._resolve_media(media)
        media_id = media_obj.id if media_obj is not None else "<model-default>"

        mscomm = comm.mscomm
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            if min_member_growth > 0:
                # Install the SteadyCom floor inside the model's context manager
                # (exactly as mscommviz.run_fba does) so it reverts on exit.
                from optlang.symbolics import Zero  # noqa: WPS433

                with mscomm.util.model:
                    for member in mscomm.members:
                        cons_name = f"{member.id}_minMemGrowth"
                        if cons_name in mscomm.util.model.constraints:
                            mscomm.util.model.remove_cons_vars(
                                mscomm.util.model.constraints[cons_name]
                            )
                        coef = {
                            member.primary_biomass.forward_variable: 1,
                            member.primary_biomass.reverse_variable: -1,
                        }
                        mscomm.util.create_constraint(
                            mscomm.util.model.problem.Constraint(
                                Zero, name=cons_name, lb=min_member_growth, ub=None
                            ),
                            coef=coef,
                        )
                    mscomm.run_fba(media_obj, pfba=pfba)
            else:
                mscomm.run_fba(media_obj, pfba=pfba)

        notes = self._scan_solver_output(buf.getvalue())

        solution = mscomm.solution
        fluxes = solution.fluxes
        result = CommunityFBAResult(
            status=solution.status,
            trustworthy=not bool(mscomm.suboptimal_solution),
            community_growth=mscomm.comm_growth,
            member_growth=dict(mscomm.memGrowths),
            exchange_fluxes=dict(mscomm.exchange_fluxes),
            fluxes=fluxes,
            media_id=media_id,
            kinetics_relaxed=KINETICS_RELAXED_MARKER
            in "\n".join(notes),
            notes=notes,
        )
        return result

    def _scan_solver_output(self, captured: str) -> list[str]:
        """Scan captured stdout for the marker lines, logging and collecting them.

        Matching lines (containing :data:`KINETICS_RELAXED_MARKER` or
        :data:`NO_GROWTH_MARKER`) are re-emitted through :data:`logger` and
        returned; non-matching lines are logged at DEBUG and discarded.

        Args:
            captured: The captured stdout text.

        Returns:
            The list of marker lines found (in order).
        """
        notes: list[str] = []
        for line in captured.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if KINETICS_RELAXED_MARKER in stripped or NO_GROWTH_MARKER in stripped:
                logger.warning("MSCommunity: %s", stripped)
                notes.append(stripped)
            else:
                logger.debug("MSCommunity stdout: %s", stripped)
        return notes

    @capability(
        domain="community",
        summary="Predict member abundances without mutating the community by default.",
        tags=("community", "simulation"),
        visibility="public",
    )
    def predict_abundances(
        self,
        comm: "CommunityModel",
        media=None,
        pfba=True,
        regularization=True,
        update=False,
        determinize=False,
    ):
        """Predict member relative abundances; NON-MUTATING by default.

        Non-mutation is the module's central semantic promise.  Before
        delegating, EXACTLY TWO things are snapshotted -- restoring too little
        leaves hidden mutation, restoring too much fights upstream's own
        bookkeeping:

        (a) the ``comm.mscomm.abundances`` mapping;
        (b) the ``{metabolite: coefficient}`` stoichiometry of the SINGLE
            community primary biomass reaction, ``comm.mscomm.primary_biomass``
            (the only reaction ``set_abundance`` touches).  Members' own
            ``primary_biomass`` reactions are NOT modified by ``set_abundance``
            and must NOT be snapshotted.

        Both are restored in a ``finally`` block with
        ``add_metabolites(..., combine=False)`` unless ``update=True``.

        Args:
            comm: The :class:`CommunityModel`.
            media: A media object, id/name, or ``None``.
            pfba: Whether to run pFBA (forwarded).
            regularization: Whether to regularize (forwarded).
            update: When ``True``, keep the predicted abundances (mutating) and
                skip the restore.
            determinize: Whether to determinize the per-member split (forwarded).

        Returns:
            ``{member_id: abundance}``, or ``None`` when upstream returns ``None``
            (a community that does not grow).

        Raises:
            CommunityDependencyError: If ``mscommunity`` is unavailable.
        """
        self._require_mscommunity()
        media_obj = self._resolve_media(media)
        mscomm = comm.mscomm

        abundance_snapshot = dict(mscomm.abundances) if mscomm.abundances else {}
        primary_biomass = mscomm.primary_biomass
        biomass_snapshot = {
            met: coef for met, coef in primary_biomass.metabolites.items()
        }

        try:
            result = mscomm.predict_abundances(
                media=media_obj,
                pfba=pfba,
                regularization=regularization,
                update_abundances=update,
                determinize=determinize,
            )
        finally:
            if not update:
                # Restore the two -- and only the two -- snapshotted things.
                mscomm.abundances = abundance_snapshot
                for member in mscomm.members:
                    if member.id in abundance_snapshot:
                        member.abundance = abundance_snapshot[member.id]
                primary_biomass.add_metabolites(biomass_snapshot, combine=False)

        if result is None:
            return None
        # Record which subroutine produced the vector so a returned abundance
        # vector says what made it.  The return contract is a plain
        # ``{member_id: float}`` mapping (no notes field), so the provenance is
        # logged rather than folded into the mapping, which would corrupt it.
        logger.info(
            "predict_abundances produced %d abundances (regularization=%s, "
            "determinize=%s)",
            len(result),
            regularization,
            determinize,
        )
        return result

    @capability(
        domain="community",
        summary="Run MICOM tradeoff FBA and return a single community result.",
        tags=("community", "simulation", "micom"),
        visibility="public",
    )
    def run_micom(
        self,
        comm: "CommunityModel",
        media,
        tradeoff=0.6,
    ) -> "CommunityFBAResult":
        """Run MICOM tradeoff FBA on ONE medium and return ONE result.

        Upstream's ``micom()`` accepts a medium or a sequence and always returns
        a list; this unwraps the single element so callers sweeping media loop
        rather than every caller unwrapping.

        Before delegating, QP-solver capability is checked by READING upstream's
        own table (``mscommunity.mscommsim._QP_CAPABLE`` and ``_pick_qp_backend``,
        which are MODULE-level names -- NOT attributes on the MSCommunity
        instance).  If neither is importable, optlang's ``available_solvers`` is
        tested against gurobi / cplex / osqp / hybrid.  If no QP backend is
        available a :class:`CommunitySolverError` naming those four is raised
        before the tradeoff loop.

        Args:
            comm: The :class:`CommunityModel`.
            media: A single media object or id/name.
            tradeoff: MICOM tradeoff (default 0.6).

        Returns:
            A :class:`CommunityFBAResult`.

        Raises:
            CommunitySolverError: If no QP-capable solver is available.
            CommunityDependencyError: If ``mscommunity`` is unavailable.
        """
        self._require_mscommunity()
        if not self._qp_backend_available():
            raise CommunitySolverError(
                "run_micom minimizes a quadratic objective, but no QP-capable "
                "solver is available (any of gurobi, cplex, osqp, hybrid); "
                "install one of them to run MICOM."
            )

        media_obj = self._resolve_media(media)
        mscomm = comm.mscomm
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            solutions = mscomm.micom(media_obj, tradeoff=tradeoff)

        # micom() always returns a list; unwrap the single element.
        solution = solutions[0] if isinstance(solutions, (list, tuple)) else solutions
        notes = self._scan_solver_output(buf.getvalue())

        media_id = media_obj.id if media_obj is not None else "<model-default>"
        return CommunityFBAResult(
            status=solution.status,
            trustworthy=not bool(mscomm.suboptimal_solution),
            community_growth=mscomm.comm_growth,
            member_growth=dict(mscomm.memGrowths),
            exchange_fluxes=dict(mscomm.exchange_fluxes),
            fluxes=solution.fluxes,
            media_id=media_id,
            kinetics_relaxed=KINETICS_RELAXED_MARKER in "\n".join(notes),
            notes=notes,
        )

    def _qp_backend_available(self) -> bool:
        """Return whether a QP-capable solver is available.

        Prefers reading upstream's own ``_pick_qp_backend`` / ``_QP_CAPABLE``
        (module-level in ``mscommunity.mscommsim``); falls back to probing
        optlang's ``available_solvers`` against gurobi / cplex / osqp / hybrid
        only if upstream ever removes them.
        """
        try:
            from mscommunity import mscommsim  # noqa: WPS433

            pick = getattr(mscommsim, "_pick_qp_backend", None)
            if pick is not None:
                return pick() is not None
            qp_capable = getattr(mscommsim, "_QP_CAPABLE", None)
            if qp_capable is not None:
                import optlang  # noqa: WPS433

                avail = optlang.available_solvers
                return any(avail.get(v, False) for v in qp_capable.values())
        except Exception:
            pass
        # Fallback: probe optlang directly for the four documented backends.
        try:
            import optlang  # noqa: WPS433

            avail = optlang.available_solvers
            return any(
                avail.get(name, False) for name in ("GUROBI", "CPLEX", "OSQP")
            )
        except Exception:
            return False

    @capability(
        domain="community",
        summary="Measure per-member solo/interacting growth, reopening drains safely.",
        tags=("community", "simulation"),
        visibility="public",
    )
    def test_member_growth(
        self,
        comm: "CommunityModel",
        media=None,
        interacting=True,
    ):
        """Measure each member's solo/interacting growth.

        Delegates to ``comm.mscomm.test_individual_species`` and returns a
        ``DataFrame`` indexed by member id.

        REOPEN THE DRAINS FIRST when needed, and this is not optional.  With
        ``close_member_drains=True``, ``test_individual_species(interacting=False)``
        reads ZERO GROWTH for every member: it disables the other members (which
        zeroes the community biomass ``bio1``), and with each member's drain shut
        ``bio1`` is the only outlet for the tested member's biomass.  Upstream
        fixed this for ``_solo_max_batch`` but NOT for
        ``test_individual_species``.  So when
        ``comm.mscomm.close_member_drains`` is ``True`` AND ``interacting`` is
        ``False``, every member's drain bounds are restored from
        ``member.biomass_drain_bounds`` inside the model's context manager
        (``with comm.mscomm.util.model:``) before delegating, so they revert on
        exit.  The reopening is recorded in the returned DataFrame's ``.attrs``.

        ``predict_abundances`` and regularization go through ``_solo_max_batch``
        and are already safe; no guard is added there.

        Args:
            comm: The :class:`CommunityModel`.
            media: A media object, id/name, or ``None``.
            interacting: Whether members interact (``True``) or are tested solo.

        Returns:
            A ``pandas.DataFrame`` indexed by member id.

        Raises:
            CommunityDependencyError: If ``mscommunity`` is unavailable.
        """
        self._require_mscommunity()
        media_obj = self._resolve_media(media)
        mscomm = comm.mscomm

        drains_reopened = False
        if getattr(mscomm, "close_member_drains", False) and not interacting:
            drains_reopened = True
            with mscomm.util.model:
                for member in mscomm.members:
                    drain = getattr(member, "biomass_drain", None)
                    bounds = getattr(member, "biomass_drain_bounds", None)
                    if drain is not None and bounds is not None:
                        drain.bounds = tuple(bounds)
                df = mscomm.test_individual_species(
                    media=media_obj, interacting=interacting
                )
        else:
            df = mscomm.test_individual_species(
                media=media_obj, interacting=interacting
            )

        # Index by member id when the "Species" column is present.
        if "Species" in getattr(df, "columns", []):
            df = df.set_index("Species")
        df.attrs["drains_reopened"] = drains_reopened
        return df

    @capability(
        domain="community",
        summary="Gapfill a community model toward a target on a medium.",
        tags=("community", "gapfilling"),
        visibility="public",
    )
    def gapfill_community(
        self,
        comm: "CommunityModel",
        media=None,
        target=None,
        templates=None,
        models=None,
        solver="glpk",
    ):
        """Gapfill a community model and return the integrated solution.

        Forwards to upstream ``comm.mscomm.gapfill`` by keyword using its REAL
        parameter names ``default_gapfill_templates=`` and
        ``default_gapfill_models=``.  Upstream fixed the solver argument in
        ``b14f50f`` (it used to land on ``MSGapfill``'s 7th positional,
        ``atp_gapfilling``, so a truthy solver string silently ran an ATP
        gapfill); the keyword path is correct against the pinned tip.

        Args:
            comm: The :class:`CommunityModel`.
            media: A media object, id/name, or ``None``.
            target: Optional gapfill target reaction id (defaults to the
                community primary biomass upstream).
            templates: Gapfill templates (upstream ``default_gapfill_templates``).
            models: Gapfill models (upstream ``default_gapfill_models``).
            solver: Solver name (default ``"glpk"``).

        Returns:
            The integrated gapfill solution from upstream.

        Raises:
            CommunityDependencyError: If ``mscommunity`` is unavailable.
        """
        self._require_mscommunity()
        media_obj = self._resolve_media(media)
        return comm.mscomm.gapfill(
            media=media_obj,
            target=target,
            default_gapfill_templates=templates,
            default_gapfill_models=models,
            solver=solver,
        )

    # ── Visualization ──────────────────────────────────────────────────────

    @capability(
        domain="community",
        summary="Compute cross-feeding / exchanged-metabolite tables for a community.",
        tags=("community", "visualization", "cross-feeding"),
        visibility="public",
    )
    def cross_feeding_table(
        self,
        comm: "CommunityModel",
        result=None,
        media=None,
        flux_threshold=1.0,
        msdb=None,
        msdb_path=None,
        ignore_mets=None,
    ):
        """Return upstream's ``(cross_feeding_df, exchanged_mets_df)`` pair.

        Delegates to ``mscommunity.mscommviz.interactions`` THROUGH
        :func:`_unwrap` (the function is wrongly ``@staticmethod``-decorated and
        would not be directly callable on Python 3.9).  ``visualize`` and
        ``show_figure`` are forced ``False`` -- this computes the tables only, no
        figure.  The biochemistry database is passed by the KEYWORD ``msdb_path=``
        (or ``msdb=``), never positionally: upstream touches ``msdb`` only inside
        its ``if visualize:`` branch, so the table itself does not need it, but it
        is forwarded anyway for symmetry with the figure path.

        Args:
            comm: The :class:`CommunityModel`.
            result: An optional :class:`CommunityFBAResult` whose ``solution`` /
                fluxes seed the interaction analysis; ``None`` lets upstream solve.
            media: A media object, id/name, or ``None``.
            flux_threshold: Minimum absolute flux for a metabolite to count as
                exchanged (upstream default 1).
            msdb: Optional biochemistry database object (forwarded as ``msdb=``).
            msdb_path: Optional biochemistry database path (forwarded as
                ``msdb_path=``).
            ignore_mets: Optional iterable of metabolite ids to ignore.

        Returns:
            The upstream ``(cross_feeding_df, exchanged_mets_df)`` tuple of
            ``pandas.DataFrame`` objects, unchanged.

        Raises:
            CommunityDependencyError: If ``mscommunity`` is unavailable.
        """
        module = self._require_mscommunity()
        from mscommunity import mscommviz  # noqa: WPS433

        media_obj = self._resolve_media(media)
        solution = getattr(result, "solution", None) if result is not None else None
        interactions = _unwrap(mscommviz.interactions)
        return interactions(
            comm.mscomm,
            solution=solution,
            media=media_obj,
            flux_threshold=flux_threshold,
            msdb=msdb,
            msdb_path=msdb_path,
            visualize=False,
            show_figure=False,
            ignore_mets=ignore_mets,
        )

    @capability(
        domain="community",
        summary="Adapt a community cross-feeding table into per-member flux dicts.",
        tags=("community", "visualization", "cross-feeding"),
        visibility="public",
    )
    def fluxes_by_member(
        self,
        comm: "CommunityModel",
        result=None,
        min_abs_flux=0.0,
    ) -> dict[str, dict[str, float]]:
        """Adapt the cross-feeding table into ``{member: {compound: flux}}``.

        This is THE ADAPTER between the two upstream packages and is PUBLIC on
        purpose: it is the seam, and the first thing to inspect when a figure
        looks wrong.

        The cross-feeding DataFrame's index is compound ids (upstream has already
        stripped the trailing ``_e0``); its columns are the member ids PLUS a
        column literally named ``"Environment"``.  No sign flip is applied:
        ``escher_edit`` documents "negative = consumed, positive = excreted" and
        MSCommunity fills each member column from that member's net flux with the
        same convention, so the conventions already agree.

        Compounds carried ONLY by the ``"Environment"`` column -- no member has a
        non-zero flux for them -- are excluded entirely (from the result and from
        the downstream compound-name / count derivations).  The ``"Environment"``
        column itself is dropped: passing it through as a member would draw the
        medium as an organism.

        Args:
            comm: The :class:`CommunityModel`.
            result: Optional :class:`CommunityFBAResult` forwarded to
                :meth:`cross_feeding_table`.
            min_abs_flux: Drop cells whose absolute flux is below this.

        Returns:
            ``{member_id: {compound_id: flux}}``.  A member with no surviving
            compounds maps to ``{}``.

        Raises:
            CommunityDependencyError: If ``mscommunity`` is unavailable.
        """
        cross_feeding_df, _ = self.cross_feeding_table(comm, result=result)

        columns = list(cross_feeding_df.columns)
        member_cols = [c for c in columns if c != "Environment"]

        result_map: dict[str, dict[str, float]] = {c: {} for c in member_cols}
        for compound_id in cross_feeding_df.index:
            row = cross_feeding_df.loc[compound_id]
            # Only MEMBER columns are consulted.  A compound carried solely by the
            # Environment column contributes to no member here and so is dropped
            # from the result entirely -- which is exactly the intent (drawing the
            # medium as an organism is the failure this prevents).
            for col in member_cols:
                val = row[col]
                if val is None:
                    continue
                fval = float(val)
                if fval == 0.0 or abs(fval) < min_abs_flux:
                    continue
                result_map[col][str(compound_id)] = fval
        return result_map

    @capability(
        domain="community",
        summary="Render the primary community cross-feeding Escher figure.",
        tags=("community", "visualization", "escher"),
        visibility="public",
    )
    def render_community_map(
        self,
        comm: "CommunityModel",
        result_or_results,
        output_path,
        *,
        min_abs_flux=0.0,
        skip_amino_acids=False,
        label_compounds="id",
        member_groups=None,
        member_legend=None,
        style=None,
        svg=True,
        html=True,
        map_name=None,
    ) -> "CommunityMapArtifacts":
        """Render the primary community cross-feeding figure via ``escher_edit``.

        Args:
            comm: The :class:`CommunityModel`.
            result_or_results: Either one :class:`CommunityFBAResult` or a
                ``{label: result}`` mapping.  A mapping produces one captioned
                block per label, assembled in SORTED LABEL ORDER (labels must be
                unique, non-empty strings); a single result produces one
                unlabelled block.
            output_path: Destination for the Escher map JSON.
            min_abs_flux: Minimum absolute flux for a member exchange to be drawn.
            skip_amino_acids: When ``True``, hide the amino-acid / common-metabolite
                skip list (``escher_edit.filter_map.DEFAULT_SKIP_NAMES``).  Defaults
                ``False`` -- silently hiding metabolites is not a default anyone
                should get without asking.
            label_compounds: What the STATIC figure calls each compound.  ``"id"``
                (default) keeps upstream's ModelSEED ids; ``"name"`` rewrites node
                labels to display names (widening the columns first so the layout
                reserves room); ``"name_id"`` uses ``"<name> (<id>)"``.
            member_groups: Optional ``{member: group}`` for palette grouping.
            member_legend: Optional legend spec forwarded to the SVG processing.
            style: An ``escher_edit.MapStyle`` instance.  Defaults to
                ``MapStyle()``.  The SAME instance is passed to
                ``build_escher_map(style=)`` and ``render_map_svg(layout=)``.  A
                caller-supplied style is never mutated.
            svg: Whether to render the SVG (and, with ``html``, the HTML page).
            html: Whether to also emit the interactive HTML page beside the SVG.
            map_name: Optional map name forwarded to ``build_escher_map``.

        Returns:
            A :class:`CommunityMapArtifacts` (``svg`` / ``html`` are ``None`` when
            not requested).

        Raises:
            CommunityVisualizationError: If ``escher_edit`` is unavailable, or if
                the community produced no above-threshold exchanges at the given
                ``min_abs_flux`` (no drawable members).
            CommunityDependencyError: If ``mscommunity`` is unavailable.
        """
        escher_edit = self._require_escher_edit()

        if style is None:
            style = escher_edit.MapStyle()

        skip_names = (
            escher_edit.filter_map.DEFAULT_SKIP_NAMES if skip_amino_acids else None
        )

        # ── Stage 1: build one block of member reactions per condition. ──────
        # Normalize to an ordered list of (label, result); a bare result yields
        # a single ("", result) pair rendered as an UNLABELLED block.
        multi = isinstance(result_or_results, Mapping)
        if multi:
            labels = list(result_or_results.keys())
            for label in labels:
                if not isinstance(label, str) or not label:
                    raise ValueError(
                        "render_community_map: multi-condition block labels must "
                        f"be unique non-empty strings; got {label!r}"
                    )
            if len(set(labels)) != len(labels):
                raise ValueError(
                    "render_community_map: multi-condition block labels must be "
                    f"unique; got {labels!r}"
                )
            # SORTED LABEL ORDER: a figure must not depend on dict insertion order.
            ordered = sorted(labels)
            conditions = [(label, result_or_results[label]) for label in ordered]
        else:
            conditions = [("", result_or_results)]

        # Compound-name map is resolved over the UNION of member-carried compounds
        # across all conditions, once, and reused for every block.
        per_condition_fbm: list[tuple[str, dict[str, dict[str, float]]]] = []
        union_compounds: set[str] = set()
        for label, result in conditions:
            fbm = self.fluxes_by_member(comm, result=result, min_abs_flux=min_abs_flux)
            per_condition_fbm.append((label, fbm))
            for compounds in fbm.values():
                union_compounds.update(compounds.keys())

        compound_names = self._resolve_compound_names(union_compounds)

        blocks = []
        for label, fbm in per_condition_fbm:
            members = escher_edit.build_member_reactions(
                fbm,
                model_id=label,
                compound_names=compound_names,
                min_abs_flux=min_abs_flux,
                skip_names=skip_names,
            )
            blocks.append((label, members))

        # ── label_compounds="name"/"name_id": widen columns BEFORE layout. ───
        # escher_edit draws node labels from bigg_id, so display names need extra
        # horizontal room reserved from the NAMES (ids are ~8 chars, names ~2.9x
        # wider).  Never mutate the caller-supplied style; derive a new one.
        display_names = None
        if label_compounds in ("name", "name_id"):
            display_names = self._compound_display_names(
                union_compounds, compound_names, label_compounds
            )
            w = style.fitted_column_dx(display_names.values())
            style = escher_edit.MapStyle(input_column_dx=-w, output_column_dx=w)

        # ── Stage 2: build the Escher map. ───────────────────────────────────
        # blocks as [(label, members), ...] for several conditions; a BARE member
        # list for one.
        if multi:
            map_input = blocks
        else:
            map_input = blocks[0][1]

        try:
            escher_map = escher_edit.build_escher_map(
                map_input,
                compound_names=compound_names,
                map_name=map_name or "community_exchange_map",
                style=style,
            )
        except ValueError as exc:
            raise CommunityVisualizationError(
                "render_community_map: the community produced no above-threshold "
                f"exchanges at min_abs_flux={min_abs_flux}, so the map has no "
                f"drawable members. ({exc})"
            ) from exc

        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(escher_map))

        n_members = len(comm.member_ids)
        n_compounds = len(union_compounds)
        n_blocks = len(blocks)

        svg_path: Optional[Path] = None
        html_path: Optional[Path] = None
        if svg:
            svg_path = out_path.with_suffix(".svg")
            # BUILD THE COLOUR MAPPING EXACTLY ONCE, from comm.member_ids, and
            # pass the SAME mapping to every render.
            colors = escher_edit.palette.member_colors(
                comm.member_ids, groups=member_groups
            )
            escher_edit.render_map_svg(
                escher_map,
                out_path=str(svg_path),
                dashed=True,
                layout=style,
                html=html,
                member_colors=colors,
                member_groups=member_groups,
                member_legend=member_legend,
            )
            if html:
                html_path = svg_path.with_suffix(".html")
            # label_compounds != "id": rewrite the static <text> labels post-render.
            if display_names is not None:
                self._rewrite_node_labels(svg_path, display_names)

        return CommunityMapArtifacts(
            map_json=out_path,
            svg=svg_path,
            html=html_path,
            n_members=n_members,
            n_compounds=n_compounds,
            n_blocks=n_blocks,
        )

    def _resolve_compound_names(self, compound_ids) -> dict[str, str]:
        """Resolve ``{compound_id: name}`` through the biochem sibling.

        Falls back to using each id as its own name when the biochem sibling is
        unavailable or raises -- a map reading ``cpd00027`` is worse than one
        reading ``D-Glucose`` and far better than no map.  NEVER raises.

        Args:
            compound_ids: Iterable of compound ids to resolve.

        Returns:
            ``{compound_id: display_name}`` for every input id.
        """
        names: dict[str, str] = {}
        biochem = getattr(self, "biochem", None)
        for cid in compound_ids:
            cid = str(cid)
            resolved = None
            if biochem is not None:
                try:
                    cpd = biochem.get_compound_by_id(cid)
                    resolved = getattr(cpd, "name", None) if cpd is not None else None
                except Exception:
                    resolved = None
            names[cid] = resolved or cid
        return names

    @staticmethod
    def _compound_display_names(
        compound_ids, compound_names: dict[str, str], label_compounds: str
    ) -> dict[str, str]:
        """Build ``{compound_id: label}`` for the static-figure rewrite.

        Args:
            compound_ids: Iterable of compound ids.
            compound_names: ``{compound_id: name}`` from
                :meth:`_resolve_compound_names`.
            label_compounds: ``"name"`` -> the name; ``"name_id"`` ->
                ``"<name> (<id>)"``.

        Returns:
            ``{compound_id: display_label}``.
        """
        out: dict[str, str] = {}
        for cid in compound_ids:
            cid = str(cid)
            name = compound_names.get(cid, cid)
            if label_compounds == "name_id":
                out[cid] = f"{name} ({cid})"
            else:
                out[cid] = name
        return out

    @staticmethod
    def _rewrite_node_labels(svg_path: Path, display_names: dict[str, str]) -> None:
        """Rewrite ``<text class="node-label label">`` contents in a rendered SVG.

        escher_edit draws node labels from ``bigg_id`` (the compound id), so a map
        rendered with ``label_compounds="name"`` still reads ``cpd00027`` in the
        static SVG until the text is rewritten here.  Uses BeautifulSoup (already
        an ``escher_edit`` dependency).  Best-effort: a missing bs4 or an
        unreadable file leaves the SVG untouched rather than failing the render.

        Args:
            svg_path: Path to the rendered SVG.
            display_names: ``{compound_id: display_label}`` to substitute.
        """
        try:
            from bs4 import BeautifulSoup  # noqa: WPS433
        except Exception:
            logger.debug("bs4 unavailable; leaving node labels as ids")
            return
        try:
            markup = svg_path.read_text()
        except Exception:
            return
        soup = BeautifulSoup(markup, "xml")
        for text_el in soup.find_all("text", class_="node-label label"):
            current = text_el.get_text()
            if current in display_names:
                text_el.string = display_names[current]
        svg_path.write_text(str(soup))

    @capability(
        domain="community",
        summary="Render a single member's projected metabolism on an Escher map.",
        tags=("community", "visualization", "escher"),
        visibility="public",
    )
    def render_member_map(
        self,
        comm: "CommunityModel",
        member_id,
        map,
        output_path,
        result=None,
        media=None,
        **escher_kwargs,
    ) -> Path:
        """Render one member's projected metabolism on a single-organism map.

        The community map has one net organism reaction per member and NO
        intracellular reactions, so it cannot answer "what is this member's
        metabolism doing".  This projects the community solution onto the member's
        own single-organism namespace and renders it on the member's SOURCE model
        (Escher maps are keyed on single-organism ids, ``_c0`` / ``_e0``, which is
        what :func:`project_member_fluxes` produces).

        Args:
            comm: The :class:`CommunityModel`.
            member_id: The member to render.
            map: The Escher map spec passed through to the escher renderer.
            output_path: Destination HTML path.
            result: Optional :class:`CommunityFBAResult`; when ``None`` a community
                FBA is run to obtain fluxes.
            media: A media object, id/name, or ``None`` (used only when ``result``
                is ``None``).
            **escher_kwargs: Forwarded to ``escher.create_map_html2``.

        Returns:
            The ``output_path`` as a :class:`~pathlib.Path`.

        Raises:
            KeyError: If ``member_id`` is not a known member.
            ValueError: If ``comm.source_model_ids`` has no entry for the member
                (a community rebuilt by ``load_community``'s fallback routes).
            CommunityDependencyError: If ``mscommunity`` is unavailable.
        """
        self._require_mscommunity()

        # KeyError for an unknown member (member_index raises KeyError).
        i = comm.member_index(member_id)

        if member_id not in comm.source_model_ids:
            raise ValueError(
                f"No source model id recorded for member {member_id}; cannot "
                "render member projection."
            )

        if result is not None:
            fluxes = result.fluxes
            media_id = getattr(result, "media_id", None)
            community_growth = getattr(result, "community_growth", float("nan"))
        else:
            result = self.run_community_fba(comm, media=media)
            fluxes = result.fluxes
            media_id = result.media_id
            community_growth = result.community_growth

        # pandas.Series -> plain dict for project_member_fluxes.
        flux_map = dict(fluxes.items()) if hasattr(fluxes, "items") else dict(fluxes)

        member = comm.mscomm.members.get_by_id(member_id)
        member_biomass_id = member.primary_biomass.id
        projected = project_member_fluxes(
            flux_map, i, member_biomass_id=member_biomass_id
        )

        source_model = self.get_model(comm.source_model_ids[member_id])

        self.escher.create_map_html2(
            source_model, map, output_path, flux=projected, **escher_kwargs
        )

        out_path = Path(output_path)
        self._inject_member_banner(
            out_path,
            member_id=member_id,
            community_id=getattr(comm.mscomm, "id", "?"),
            media_id=media_id if media_id is not None else "<model-default>",
            community_growth=community_growth,
        )
        return out_path

    @staticmethod
    def _inject_member_banner(
        html_path: Path,
        *,
        member_id: str,
        community_id: str,
        media_id: str,
        community_growth: float,
    ) -> None:
        """Inject the projected-member banner into a rendered map HTML.

        The banner is inserted after the opening ``<body>`` tag, or PREPENDED to
        the document when no ``<body>`` is found (rather than failing).

        Args:
            html_path: Path to the rendered HTML.
            member_id: The rendered member's id.
            community_id: The community model id.
            media_id: The media id used for the solution.
            community_growth: The community growth rate (1/hr).
        """
        try:
            growth_str = f"{float(community_growth):.4f}"
        except (TypeError, ValueError):
            growth_str = "nan"
        banner = (
            f'<div class="kbutil-member-banner">Member {member_id} projected from '
            f"community {community_id} on medium {media_id} (community growth "
            f"{growth_str}/hr). Projected member view: fluxes are this member's "
            "slice of a community solution. EX_ exchange reactions are "
            "community-level and cannot be attributed to one member.</div>"
        )
        try:
            markup = html_path.read_text()
        except Exception:
            return
        lower = markup.lower()
        idx = lower.find("<body")
        if idx != -1:
            # Insert after the end of the opening <body ...> tag.
            close = markup.find(">", idx)
            if close != -1:
                markup = markup[: close + 1] + banner + markup[close + 1 :]
            else:
                markup = banner + markup
        else:
            markup = banner + markup
        html_path.write_text(markup)

    @capability(
        domain="community",
        summary="Build a cross-feeding MultiDiGraph keyed on metabolite id.",
        tags=("community", "visualization", "cross-feeding", "graph"),
        visibility="public",
    )
    def cross_feeding_graph(
        self,
        comm: "CommunityModel",
        result=None,
        min_abs_flux=1e-4,
        **table_kwargs,
    ):
        """Build a cross-feeding ``networkx.MultiDiGraph`` from the table.

        It MUST be a ``MultiDiGraph``, not a ``DiGraph``: two members commonly
        trade several metabolites, and a ``DiGraph`` holds at most one edge per
        ordered pair, so every exchange after the first would be silently
        overwritten.  Each edge is KEYED ON THE METABOLITE ID so a specific
        transfer is addressable.

        Nodes: one per member plus a node literally named ``"Environment"`` (kept
        here -- this is a graph of exchange, not a drawing).  Edges: one directed
        edge per donor -> consumer -> metabolite triple, keyed on the metabolite
        id, with attributes ``metabolite``, ``flux`` (signed) and ``abs_flux``.
        Edges below ``min_abs_flux`` are dropped.

        Args:
            comm: The :class:`CommunityModel`.
            result: Optional :class:`CommunityFBAResult` forwarded to
                :meth:`cross_feeding_table`.
            min_abs_flux: Drop edges whose absolute flux is below this.
            **table_kwargs: Forwarded to :meth:`cross_feeding_table`
                (``media``, ``flux_threshold``, ``msdb``, ``msdb_path``,
                ``ignore_mets``).

        Returns:
            A ``networkx.MultiDiGraph``.

        Raises:
            CommunityDependencyError: If ``networkx`` (or ``mscommunity``) is
                unavailable.
        """
        try:
            import networkx as nx  # noqa: WPS433
        except Exception as exc:
            raise CommunityDependencyError(
                "networkx is required to build a cross-feeding graph but is not "
                "importable."
            ) from exc

        cross_feeding_df, _ = self.cross_feeding_table(
            comm, result=result, **table_kwargs
        )

        graph = nx.MultiDiGraph()
        columns = list(cross_feeding_df.columns)
        for col in columns:
            graph.add_node(col)

        # For each metabolite, a producer (positive/excreted) donates to every
        # consumer (negative/consumed) of the same metabolite.
        for compound_id in cross_feeding_df.index:
            row = cross_feeding_df.loc[compound_id]
            producers: list[tuple[str, float]] = []
            consumers: list[str] = []
            for col in columns:
                val = row[col]
                if val is None:
                    continue
                fval = float(val)
                if abs(fval) < min_abs_flux:
                    continue
                if fval > 0:
                    producers.append((col, fval))
                elif fval < 0:
                    consumers.append(col)
            for donor, flux in producers:
                for consumer in consumers:
                    graph.add_edge(
                        donor,
                        consumer,
                        key=str(compound_id),
                        metabolite=str(compound_id),
                        flux=flux,
                        abs_flux=abs(flux),
                    )
        return graph


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
        # Inject the sibling Impls the visualization methods reach via ``self``
        # (``self.escher.create_map_html2`` and ``self.biochem.get_compound_by_id``).
        # The delegate is a plain KBModelUtils and does not otherwise know its
        # siblings; wiring them here keeps render_member_map / render_community_map
        # working without a facade change.
        if self._delegate is not None:
            self._delegate.escher = escher
            self._delegate.biochem = biochem

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
