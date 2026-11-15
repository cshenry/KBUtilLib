"""The light, KBase-free ModelSEED reconstruction layer.

This module is the reconstruction surface KBDL (and any KBase CTS job) calls:
given a SUPPLIED ``MSTemplate`` and ``MSGenome`` it builds a model natively with
ModelSEEDpy, runs ATP correction, gap-fills per requested medium, and reports a
structured :class:`ReconstructionResult`. It contains no KBDL import and no KBDL
format knowledge -- KBDL owns its documents, its wire contract and its reports,
and hands this layer three plain ModelSEEDpy objects (template, media, genome).

Stage boundaries, FIXED by PRD ``kbdl-recon-thin-layer-v1`` (confront round 2).
Every per-stage field on :class:`ReconstructionResult` names the stage it belongs
to, and these are the definitions those names mean:

DRAFT
    The fully assembled, UNCORRECTED model: annotation-supported reactions plus
    spontaneous/universal reactions, biomass and its auxiliary reactions,
    exchanges/drains, and objective selection -- but BEFORE any ATP-specific
    maintenance/scaffold insertion and before any ATP solve. Exactly what
    ``MSBuilder.build_base_model`` returns; ``ATPM`` is NOT yet present.
POST-ATP
    After every ATP-correction mutation, before requested-medium gap-fill.
POST-GAP-FILL
    After sequential integration for all requested media, before FVA or
    reporting.

Why ``MSBuilder.build()`` is never called
    Upstream ``build()`` (``msbuilder.py:992`` at the pinned revision) runs
    ``build_base_model``, adds ``ATPM``, runs ``MSATPCorrection`` AND calls
    ``MSGapfill.run_gapfilling`` unconditionally, returning a 4-tuple. It
    therefore collapses all three stage boundaries into one call and takes the
    gap-fill medium out of the caller's hands. This layer drives the three
    stages itself from ``build_base_model``.

Keyword arguments, everywhere, without exception
    The ModelSEEDpy fork and upstream BOTH report version ``0.4.2``/``0.4.3``
    while their signatures differ: the fork's ``build(model_or_id, index, ...)``
    returns a bare model, upstream's ``build(model_or_id, gapfill_media, ...)``
    returns a 4-tuple. A version string does not identify the code, so every
    call into ModelSEEDpy from this module passes its arguments BY KEYWORD. A
    positional call that silently rebinds is exactly the defect this layer
    replaced: ``builder.build(base_model, "0", False, False)`` bound ``"0"`` to
    ``gapfill_media`` and left ``annotate_with_rast`` at its default ``True``,
    i.e. a live RAST network call nobody asked for.
    ``tests/test_recon_layer.py::test_no_positional_modelseedpy_calls`` enforces
    this by AST over this file, so it cannot regress silently.

Measurements this module is built on are in
``agent-io/work-records/task-d1302dc4.md`` (raw dump in
``agent-io/research/task-d1302dc4-recon-env-smoke-measurements.json``); the
pinned ModelSEEDpy revision and the alias-matching change are in that repo's
``agent-io/work-records/msp-alias-roles.md``. Comments below cite the section
letter of any measurement they rely on.
"""

import ast
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import pandas as pd

from .kb_model_utils import KBModelUtils

__all__ = [
    "DEFAULT_GENOME_CLASSIFIER_ID",
    "DEFAULT_ONTOLOGY_TERM",
    "GENOME_CLASSES_WITH_DEDICATED_TEMPLATE",
    "GENOME_CLASS_TO_TEMPLATE_KEY",
    "GPR_STRING_SUPPORT",
    "RECONSTRUCTION_DATA_DEPENDENCIES",
    "SOLVER_PREFERENCE",
    "UNKNOWN_REVISION",
    "GapfillRecord",
    "MissingBiochemistryError",
    "MissingSolverError",
    "ModelSEEDpyCapabilityError",
    "MSReconstructionUtils",
    "MSReconstructionUtilsImpl",
    "ReconstructionEnvironment",
    "ReconstructionEnvironmentError",
    "ReconstructionResult",
    "classify_genome_class",
    "initialize_reconstruction_environment",
    "resolve_distribution_provenance",
    "template_key_for_genome_class",
    "verify_alias_matching",
]

logger = logging.getLogger(__name__)

#: Capability marker read by downstream callers (notably the KBDL model-reconstruction
#: service) to decide whether a complex-bearing job can run against this install.
#: ``1`` = :meth:`MSReconstructionUtils._add_reactions_from_gene_mapping` accepts
#: verbatim GPR strings as ``reaction_gene_hash`` values, not just lists of gene ids.
GPR_STRING_SUPPORT = 1

#: Ontology key the layer reads genome functions from. KBDL folds the union of
#: function strings from every annotation source under ONE ontology key, and
#: passes that key in; ``"RAST"`` is the default because it is what
#: ``MSBuilder.__init__`` and ``MSGenomeClassifier.classify`` both default to.
#: It is an EXPLICIT parameter everywhere in this module, never an implicit
#: default inside ModelSEEDpy.
DEFAULT_ONTOLOGY_TERM = "RAST"

#: Value used for a provenance revision that the installed distribution does not
#: record. It is never synthesised from anything else (not from a version string,
#: not from a git checkout that happens to be nearby): an unknown revision is
#: reported as unknown.
UNKNOWN_REVISION = "unknown"

#: DATA dependencies the reconstruction layer resolves through
#: ``~/.kbutillib/dependencies.yaml``.
#:
#: Deliberately excludes ``modelseedpy`` and ``cobrakbase``, which that file also
#: declares. ``DependencyManager.initialize_dependencies`` does
#: ``sys.path.insert(0, dep_path)`` for EVERY declared entry
#: (``core/dependency_manager.py:141``), so initialising the whole file prepends
#: the ModelSEEDpy FORK checkout to ``sys.path`` and SILENTLY SHADOWS a pinned
#: upstream install -- observed directly: a process printing ``modelseedpy 0.4.3``
#: before the call printed ``0.4.2`` after it (task-d1302dc4 record, reconciliation
#: item 10). Any job that "installs the pinned ModelSEEDpy and then initialises
#: the dependency manager" is running the fork. Restricting the set is the fix.
RECONSTRUCTION_DATA_DEPENDENCIES: Tuple[str, ...] = (
    "ModelSEEDDatabase",
    "cb_annotation_ontology_api",
)

#: LP backends tried in order, each confirmed by a REAL SOLVE and never by
#: availability alone (KBDL issue #35).
#:
#: GLPK leads because it needs no licence and carried ATP correction to
#: completion in 50.9 s against Gurobi's 59.3 s on the same model (measurement
#: (b)). Gurobi works on poplar but only below major version 14: the site
#: licence server rejects 14 outright, and -- the trap worth knowing -- with
#: ``gurobipy`` 14 installed even a GLPK solve fails, because cobra picks its
#: interface when the ``Model`` is constructed, before ``model.solver`` is
#: assigned. Pin ``gurobipy<14`` wherever it is declared.
SOLVER_PREFERENCE: Tuple[str, ...] = ("glpk", "gurobi")

#: Genome class letter (as returned by ``MSGenomeClassifier.classify``) to the
#: template key this layer asks for.
#:
#: ``"C"`` (Cyanobacteria) maps to ``"gn"`` under the NO-DEDICATED-TEMPLATE rule:
#: there is no cyano template to load, and the old behaviour -- returning early
#: with "Cyanobacteria not yet supported. Skipping genome." and a ``None`` model
#: -- made a classifier letter silently decide that a genome got no model at all.
#: Any class with no dedicated template, including one this map does not list at
#: all, resolves to ``"gn"`` via :func:`template_key_for_genome_class`.
GENOME_CLASS_TO_TEMPLATE_KEY: Mapping[str, str] = {
    "P": "gp",
    "N": "gn",
    "A": "ar",
    "C": "gn",
}

#: Classifier letters that DO have a dedicated template of their own. Anything
#: outside this set takes the no-dedicated-template route to ``"gn"``, and the
#: fact that it did is recorded in the report row rather than left implicit.
GENOME_CLASSES_WITH_DEDICATED_TEMPLATE: Tuple[str, ...] = ("P", "N", "A")

#: Human-readable class name per letter, for report rows.
GENOME_CLASS_NAMES: Mapping[str, str] = {
    "P": "Gram Positive",
    "N": "Gram Negative",
    "A": "Archaea",
    "C": "Cyanobacteria",
}


# ---------------------------------------------------------------------------
# Classified errors
#
# Three DISTINCT classes, so a caller can tell a licence/solver problem from a
# missing database from an under-featured ModelSEEDpy without string-matching a
# message. Each carries its own remedy, and each refuses to claim a cause it has
# not established -- in particular a MISSING FILE is reported as a missing file,
# NOT as KBDL issue #41. #41 is specifically "modelseedpy's config.cfg default
# was used", and the fix for that (seed the biochemistry from the dependency
# manager) does nothing for a database that is genuinely not on disk.
# ---------------------------------------------------------------------------


class ReconstructionEnvironmentError(RuntimeError):
    """Base class for reconstruction-environment faults.

    Catch this to catch all three; catch the subclasses to act on one.
    """


class MissingSolverError(ReconstructionEnvironmentError):
    """No LP backend completed a real solve.

    Raised by :func:`initialize_reconstruction_environment` after PROBING each
    candidate backend with an actual optimisation whose answer is checked -- not
    after reading ``optlang.available_solvers``, which reports Gurobi as
    available on a host whose licence server refuses it.
    """


class MissingBiochemistryError(ReconstructionEnvironmentError):
    """The ModelSEED biochemistry database could not be resolved or loaded.

    Three separate situations reach this, and the message says which:

    * ``~/.kbutillib/dependencies.yaml`` does not declare ``ModelSEEDDatabase``;
    * it declares it at a path that does not exist on disk;
    * the declared path exists but loading the biochemistry from it failed.

    None of these is reported as KBDL issue #41. #41 is the narrower claim that
    *modelseedpy's own ``config.cfg`` default was used* -- ``data/ModelSEEDDatabase``
    upstream, ``/deps/ModelSEEDDatabase`` in the fork -- and this layer never
    takes that route, so it is never the explanation for a failure here.
    """


class ModelSEEDpyCapabilityError(ReconstructionEnvironmentError):
    """The installed ModelSEEDpy cannot do something this layer requires.

    Raised when a BEHAVIOURAL check fails, not when a signature looks wrong. The
    distinction is the whole point: both the fork and upstream report version
    ``0.4.2``/``0.4.3``, a parameter can be accepted and ignored, and the
    alias-matching commit this layer depends on is not identifiable from any
    version string. So the check builds a model and looks at the genes on it.
    """


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def resolve_distribution_provenance(distribution_name: str) -> Tuple[str, str]:
    """Read a distribution's version and resolved revision from its metadata.

    Args:
        distribution_name: Installed distribution name, e.g. ``"modelseedpy"``.

    Returns:
        ``(version, revision)``. ``version`` is the distribution's declared
        version, or :data:`UNKNOWN_REVISION` if the distribution is not
        installed. ``revision`` is the VCS commit recorded in the install's
        ``direct_url.json`` (``vcs_info.commit_id``, written by pip for a
        ``git+...`` install), or :data:`UNKNOWN_REVISION`.

    A revision is returned ONLY when the installed distribution records one.
    A plain PyPI wheel records none; an editable install
    (``pip install -e .``) records ``dir_info.editable`` and no commit. Both get
    :data:`UNKNOWN_REVISION`. Nothing is inferred from a nearby git checkout --
    the working tree a source directory happens to sit in is not evidence about
    the code that was imported, and a version string is not evidence at all
    (fork and upstream both report ``0.4.2``/``0.4.3`` with different
    signatures). Reporting ``"unknown"`` where the install is silent is the
    honest answer and is what makes the populated cases trustworthy.
    """
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        dist = distribution(distribution_name)
    except PackageNotFoundError:
        return UNKNOWN_REVISION, UNKNOWN_REVISION

    version = dist.version or UNKNOWN_REVISION
    revision = UNKNOWN_REVISION
    raw = dist.read_text("direct_url.json")
    if raw:
        try:
            commit = json.loads(raw).get("vcs_info", {}).get("commit_id")
        except ValueError:
            commit = None
        if commit:
            revision = str(commit)
    return version, revision


def _supported_keyword_arguments(
    target: Any,
    requested: Mapping[str, Tuple[Any, Any]],
    label: str,
) -> Dict[str, Any]:
    """Keep only the keywords ``target`` accepts; raise if a REQUESTED one is absent.

    Args:
        target: The callable about to be invoked.
        requested: ``{keyword: (value, unrequested_default)}``. A keyword whose
            value equals its ``unrequested_default`` is treated as not asked for.
        label: Human name of the callable, for the error message.

    Returns:
        The subset of ``{keyword: value}`` that ``target`` can actually take.

    Raises:
        ModelSEEDpyCapabilityError: A keyword with a non-default value is not in
            ``target``'s signature.

    This exists because ModelSEEDpy's fork and upstream differ in their
    SIGNATURES while reporting the same version, so some parameters exist in one
    tree and not the other -- ``MSGapfill.__init__``'s ``base_media`` and
    ``base_media_target_element`` are fork-only. Dropping an unrequested
    parameter is free; dropping one the caller explicitly set would silently
    change the computation, so that raises instead.
    """
    import inspect

    parameters = inspect.signature(target).parameters
    accepts_var_keyword = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    supported: Dict[str, Any] = {}
    unsupported: List[str] = []
    for name, (value, unrequested_default) in requested.items():
        if name in parameters or accepts_var_keyword:
            supported[name] = value
        elif value != unrequested_default:
            unsupported.append(f"{name}={value!r}")
    if unsupported:
        raise ModelSEEDpyCapabilityError(
            f"the installed modelseedpy's {label} does not accept "
            f"{unsupported}, so these settings cannot be honoured. They exist in "
            "the ModelSEEDpy fork but not at the revision this layer pins; a "
            "version string does not distinguish the two."
        )
    return supported


#: Trailing compartment index on a model reaction id: ``_c0`` in ``rxn00029_c0``.
_COMPARTMENT_SUFFIX = re.compile(r"_[a-z]\d+$")


def _base_reaction_id(reaction_id: str) -> str:
    """Strip a model reaction id's compartment suffix.

    Args:
        reaction_id: Either a model id (``"rxn00029_c0"``) or a base id
            (``"rxn00029"``).

    Returns:
        The base id.

    Needed because the two id forms are NOT interchangeable where it matters.
    ``GapfillingPkg`` compares the gap-fill blacklist against
    ``template_reaction.reference_id`` and against the base-id capture group of
    its own compartment-suffix regex -- i.e. BASE ids -- so a blacklist entry of
    ``"rxn00029_c0"`` matches nothing at all and the reaction is gap-filled in
    anyway. Observed exactly that way: an exclusion list of ``["rxn00029_c0"]``
    was passed as the blacklist and ``rxn00029_c0`` still arrived in the model.
    """
    return _COMPARTMENT_SUFFIX.sub("", reaction_id)


def _media_digest(media: Any) -> str:
    """A stable digest of a medium's COMPOSITION, independent of its id.

    Args:
        media: ``MSMedia``-like object exposing ``mediacompounds``.

    Returns:
        A ``sha256:`` prefixed 16-hex-digit digest over the sorted
        ``(compound id, lower bound, upper bound)`` triples, or
        ``"sha256:unavailable"`` if the object does not expose compounds.

    This is what makes a per-occurrence gap-fill record self-describing. Media
    ids are the CALLER's ids and this layer keeps them verbatim, so the same id
    can legitimately be requested twice -- and two different compositions can
    legitimately arrive under one id. The digest distinguishes those cases
    without the layer having to rename anything.
    """
    compounds = getattr(media, "mediacompounds", None)
    if compounds is None:
        return "sha256:unavailable"
    items = sorted(
        (
            str(getattr(cpd, "id", "")),
            float(getattr(cpd, "lower_bound", 0.0)),
            float(getattr(cpd, "upper_bound", 0.0)),
        )
        for cpd in compounds
    )
    payload = json.dumps(items, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GapfillRecord:
    """One gap-fill attempt, for one REQUESTED MEDIUM OCCURRENCE.

    One record per occurrence, in request order -- not one per distinct medium
    id. Requesting the same id twice yields two records with different
    ``request_index`` values, and the second one honestly reports what the
    second attempt did (typically ``"no_gaps"``: the first attempt already
    integrated the reactions it needed). The upstream alternative,
    ``MSGapfill.run_multi_gapfill``, returns a dict keyed by media object and so
    cannot represent a repeated request at all.

    Attributes:
        request_index: 0-based position of this occurrence in the ``media_objs``
            the caller passed. The sort key for the record tuple.
        media_id: The medium's id EXACTLY as the caller supplied it. The layer
            never renames, suffixes or de-duplicates a caller's id.
        media_digest: :func:`_media_digest` of the medium's composition, so two
            occurrences sharing an id can still be told apart.
        status: One of ``"integrated"`` (a solution was returned and
            integrated -- which can still leave ``additions`` empty, when the
            solution contained nothing the model did not already have),
            ``"no_gaps"`` (no solution was returned AND the model already met
            the objective on this medium), ``"no_solution"`` (no solution was
            returned and the model does not meet the objective) or
            ``"error:<ExceptionType>"`` (the attempt raised; the remaining
            occurrences are still attempted).
        additions: Reaction ids ADDED to the model by this occurrence, sorted.
        reversals: Reaction ids whose bounds this occurrence re-opened, sorted.
            Distinct from ``additions``: a reversal changes an existing reaction.
        objective_value: Objective value after this occurrence, or ``None`` if no
            solve produced one.
    """

    request_index: int
    media_id: str
    media_digest: str
    status: str
    additions: Tuple[str, ...]
    reversals: Tuple[str, ...]
    objective_value: Optional[float]


@dataclass(frozen=True)
class ReconstructionResult:
    """Structured facts about one reconstruction, STAGE BY STAGE.

    Every field is enumerated explicitly and every per-stage field names its
    stage in its own name (``draft_``, ``post_atp_``, ``post_gapfill_``), because
    "957 reactions" means three different things at the three stage boundaries
    and a report that does not say which is unreadable. See this module's
    docstring for the binding definitions of DRAFT, POST-ATP and POST-GAP-FILL.

    There are no ``getattr`` defaults anywhere in the construction of this
    object: a field is populated from something that was measured, or the
    reconstruction raises. A silently-defaulted provenance field is worse than a
    missing one, because it reads as a measurement.
    """

    # --- DRAFT: after build_base_model, before any ATP scaffold or solve ------
    draft_reaction_count: int
    draft_metabolite_count: int
    draft_gene_count: int
    draft_exchange_count: int
    draft_drain_count: int
    draft_biomass_ids: Tuple[str, ...]
    draft_objective_expression: str
    draft_atpm_present: bool

    # --- POST-ATP: after every ATP-correction mutation ------------------------
    post_atp_correction_ran: bool
    post_atp_correction_invocations: int
    post_atp_reaction_count: int
    post_atp_removed_reaction_ids: Tuple[str, ...]
    post_atp_core_gapfilling_count: Optional[int]
    post_atp_selected_media_ids: Tuple[str, ...]
    post_atp_test_count: Optional[int]
    post_atp_analysis: Mapping[str, Any]

    # --- POST-GAP-FILL: after sequential integration of every request --------
    post_gapfill_records: Tuple[GapfillRecord, ...]
    post_gapfill_reaction_count: Optional[int]
    post_gapfill_reaction_exclusion_list: Tuple[str, ...]

    # --- Object identities ----------------------------------------------------
    genome_id: str
    genome_class: Optional[str]
    template_id: str
    template_name: str
    core_template_id: Optional[str]
    core_template_name: Optional[str]

    # --- Build settings actually used -----------------------------------------
    ontology_term: str
    match_role_aliases: bool
    allow_incomplete_complexes: bool
    reactions_to_add_supplied: bool

    # --- Software provenance --------------------------------------------------
    modelseedpy_version: str
    modelseedpy_revision: str
    kbutillib_version: str
    kbutillib_revision: str
    solver: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        """Return the result as a plain JSON-serialisable dict.

        Returns:
            A dict with one key per field; ``post_gapfill_records`` becomes a
            list of per-occurrence dicts in request order.
        """
        from dataclasses import asdict

        return asdict(self)


@dataclass(frozen=True)
class ReconstructionEnvironment:
    """What :func:`initialize_reconstruction_environment` established.

    Attributes:
        dependency_config_path: The ``dependencies.yaml`` the paths were read
            from.
        dependency_paths: Resolved absolute path per entry of
            :data:`RECONSTRUCTION_DATA_DEPENDENCIES`. Every value is non-``None``
            -- the helper raises rather than return a ``None`` path, because a
            ``None`` from ``get_data_path`` is precisely what sends each consumer
            off to its own sibling-directory guess.
        dependency_manager: The resolved manager, to FORWARD EXPLICITLY as
            ``MSReconstructionUtils(dependency_manager=...)`` so per-instance
            ``get_data_path`` works.
        solver: The LP backend confirmed by a real solve.
        solver_probe_objective: The objective value that probe returned, so the
            claim "it solves" is backed by a number.
        biochemistry_loaded: Whether the ModelSEED biochemistry was loaded from
            ``dependency_paths["ModelSEEDDatabase"]``.
        biochemistry_compound_count: Compounds in the loaded biochemistry.
        biochemistry_reaction_count: Reactions in the loaded biochemistry.
        alias_matching_verified: Whether ``build_base_model(match_role_aliases=
            True)`` was OBSERVED to attach a gene through a role ALIAS.
        alias_matching_gene_ids: The gene ids that alias match actually produced.
        modelseedpy_version / modelseedpy_revision: From distribution metadata.
    """

    dependency_config_path: str
    dependency_paths: Mapping[str, str]
    dependency_manager: Any
    solver: str
    solver_probe_objective: float
    biochemistry_loaded: bool
    biochemistry_compound_count: Optional[int]
    biochemistry_reaction_count: Optional[int]
    alias_matching_verified: bool
    alias_matching_gene_ids: Tuple[str, ...]
    modelseedpy_version: str
    modelseedpy_revision: str


# ---------------------------------------------------------------------------
# Environment initialisation
# ---------------------------------------------------------------------------


def _probe_solver(backend: str) -> Optional[float]:
    """Confirm an LP backend by PERFORMING a solve, never by assuming.

    Args:
        backend: cobra/optlang backend name, e.g. ``"glpk"``.

    Returns:
        The objective value of a two-reaction problem whose answer is known to
        be ``10.0``, or ``None`` if the backend could not produce it.

    ``optlang.available_solvers`` reports Gurobi as available on a host whose
    licence server refuses the installed major version, so availability is not
    evidence (#35). The model's solver is set BEFORE optimising rather than
    relying on cobra's default interface, which is chosen at ``Model``
    construction time.
    """
    try:
        import cobra

        model = cobra.Model("kbu_solver_probe")
        metabolite = cobra.Metabolite("probe_c")
        source = cobra.Reaction("PROBE_SOURCE", lower_bound=0, upper_bound=10)
        source.add_metabolites({metabolite: 1})
        sink = cobra.Reaction("PROBE_SINK", lower_bound=0, upper_bound=1000)
        sink.add_metabolites({metabolite: -1})
        model.add_reactions([source, sink])
        model.objective = "PROBE_SINK"
        model.solver = backend
        solution = model.optimize()
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
        logger.debug("solver probe %s failed: %s", backend, exc)
        return None
    if solution.status != "optimal" or abs(solution.objective_value - 10.0) > 1e-6:
        return None
    return float(solution.objective_value)


def _two_role_alias_template() -> Any:
    """A two-role in-memory ``MSTemplate`` whose second role shares an ALIAS.

    Returns:
        An ``MSTemplate`` with one compartment, one compound, one reaction and
        one complex of two triggering roles. Role ``r1`` is named
        ``"First enzyme"``; role ``r2`` is named ``"Second enzyme"``. BOTH carry
        the alias ``"Shared alias"``, which matches NEITHER role's name.

    Built in memory on purpose: the check that uses it must not depend on a
    template download, a database, or a solver, so it can run as a precondition
    rather than as part of a job.
    """
    from modelseedpy.core.mstemplate import (
        MSTemplate,
        MSTemplateCompartment,
        MSTemplateMetabolite,
        MSTemplateReaction,
        MSTemplateSpecies,
        NewModelTemplateComplex,
        NewModelTemplateRole,
    )

    template = MSTemplate(template_id="kbu_alias_probe")
    template.compartments.append(MSTemplateCompartment("c", "Cytosol", 3))
    template.add_compounds([MSTemplateMetabolite("cpd_probe", "C", "probe")])
    species = MSTemplateSpecies("cpd_probe_c", 0, "c", "cpd_probe")
    template.add_comp_compounds([species])
    roles = [
        NewModelTemplateRole("r1", "First enzyme", aliases=["Shared alias"]),
        NewModelTemplateRole("r2", "Second enzyme", aliases=["Shared alias"]),
    ]
    template.add_roles(roles)
    complex_ = NewModelTemplateComplex("cpx_probe", "probe")
    for role in roles:
        complex_.add_role(role=role, triggering=True, optional=False)
    template.add_complexes([complex_])
    reaction = MSTemplateReaction("rxn_probe_c", "rxn_probe", "probe", "", 0, 1000)
    reaction.add_metabolites({species: -1})
    reaction.add_complexes([complex_])
    template.add_reactions([reaction])
    return template


def _genome_with_functions(
    functions: Sequence[str], ontology_term: str = DEFAULT_ONTOLOGY_TERM
) -> Any:
    """An in-memory ``MSGenome`` whose features carry the given function strings.

    Args:
        functions: One function string per feature; feature ids are ``g0``,
            ``g1``, ...
        ontology_term: Ontology key the functions are filed under.

    Returns:
        An ``MSGenome``.
    """
    from modelseedpy import MSGenome
    from modelseedpy.core.msgenome import MSFeature

    genome = MSGenome()
    features = []
    for index, function in enumerate(functions):
        feature = MSFeature(feature_id=f"g{index}", sequence="M")
        feature.add_ontology_term(ontology_term=ontology_term, value=function)
        features.append(feature)
    genome.add_features(feature_list=features)
    return genome


def verify_alias_matching(
    ontology_term: str = DEFAULT_ONTOLOGY_TERM,
) -> Tuple[str, ...]:
    """Check BEHAVIOURALLY that ``match_role_aliases=True`` matches on aliases.

    Args:
        ontology_term: Ontology key the probe genome's function is filed under.

    Returns:
        The gene ids that alias matching attached to the probe reaction --
        ``("g0",)`` on a capable install.

    Raises:
        ModelSEEDpyCapabilityError: If the keyword is rejected, or accepted and
            ignored, or if the no-alias control already matches (which would mean
            the check proves nothing).

    This tests BEHAVIOUR, not the signature. The probe genome carries exactly
    one function, ``"SHARED ALIAS"``, which is an ALIAS of both template roles
    and the NAME of neither. So:

    * with ``match_role_aliases=False`` the reaction must be ABSENT -- the
      control, which is what makes the positive result mean anything;
    * with ``match_role_aliases=True`` the reaction must be present AND carry
      gene ``g0``.

    Checking ``"match_role_aliases" in inspect.signature(...)`` instead would
    pass against any build that accepts and drops the keyword, and version
    strings are no help at all: the fork and upstream both report ``0.4.2`` /
    ``0.4.3`` with materially different builders.
    """
    try:
        from modelseedpy import MSBuilder
    except ImportError as exc:
        raise ModelSEEDpyCapabilityError(
            f"modelseedpy is not importable, so alias matching cannot be "
            f"verified: {exc}"
        ) from exc

    genome = _genome_with_functions(["SHARED ALIAS"], ontology_term=ontology_term)

    def _draft(**options: Any) -> Any:
        builder = MSBuilder(
            genome=genome,
            template=_two_role_alias_template(),
            ontology_term=ontology_term,
        )
        return builder.build_base_model(
            model_or_id="kbu_alias_probe_model",
            annotate_with_rast=False,
            **options,
        )

    try:
        control = _draft(match_role_aliases=False, allow_incomplete_complexes=False)
        matched = _draft(match_role_aliases=True, allow_incomplete_complexes=False)
    except TypeError as exc:
        raise ModelSEEDpyCapabilityError(
            "the installed modelseedpy's MSBuilder.build_base_model does not "
            "accept match_role_aliases/allow_incomplete_complexes. The "
            "reconstruction layer requires the alias-matching change on top of "
            f"upstream dev; install the pinned revision. Underlying error: {exc}"
        ) from exc

    if "rxn_probe_c0" in control.reactions:
        raise ModelSEEDpyCapabilityError(
            "the alias-matching check is not valid on this install: the control "
            "build with match_role_aliases=False already matched the probe "
            "reaction through a role NAME, so a positive result would prove "
            "nothing about aliases."
        )
    if "rxn_probe_c0" not in matched.reactions:
        raise ModelSEEDpyCapabilityError(
            "the installed modelseedpy accepts match_role_aliases=True but does "
            "not act on it: a gene whose only function is a role ALIAS did not "
            "produce the probe reaction. Install the pinned revision carrying "
            "the alias-matching change."
        )
    gene_ids = tuple(
        sorted(gene.id for gene in matched.reactions.get_by_id("rxn_probe_c0").genes)
    )
    if gene_ids != ("g0",):
        raise ModelSEEDpyCapabilityError(
            "the installed modelseedpy matched the probe reaction through an "
            f"alias but attached genes {gene_ids!r} instead of ('g0',)."
        )
    return gene_ids


def initialize_reconstruction_environment(
    dependency_config_path: Optional[Union[str, Path]] = None,
    data_dependencies: Sequence[str] = RECONSTRUCTION_DATA_DEPENDENCIES,
    solver_preference: Sequence[str] = SOLVER_PREFERENCE,
    load_biochemistry: bool = True,
    require_alias_matching: bool = True,
    ontology_term: str = DEFAULT_ONTOLOGY_TERM,
    scratch_dir: Optional[Union[str, Path]] = None,
) -> ReconstructionEnvironment:
    """Prepare this process to run reconstruction, or raise saying what is wrong.

    Args:
        dependency_config_path: ``dependencies.yaml`` to read. Defaults to
            ``~/.kbutillib/dependencies.yaml``.
        data_dependencies: Names that must resolve. Defaults to
            :data:`RECONSTRUCTION_DATA_DEPENDENCIES`; see that constant for why
            ``modelseedpy`` must stay out of this list.
        solver_preference: LP backends to probe, best first.
        load_biochemistry: Load the ModelSEED biochemistry now (~20 s) so the
            first reconstruction does not pay for it, and so a bad database is a
            startup error rather than a mid-job one.
        require_alias_matching: Run :func:`verify_alias_matching`. Leave ``True``
            unless the caller genuinely never passes ``match_role_aliases=True``.
        ontology_term: Ontology key used by the alias probe.
        scratch_dir: Where to write the reduced dependency config. Defaults to
            ``$TMPDIR`` or ``/tmp``.

    Returns:
        A frozen :class:`ReconstructionEnvironment` describing what was
        established, including the resolved
        :class:`~kbutillib.core.dependency_manager.DependencyManager` to forward
        explicitly into :class:`MSReconstructionUtils`.

    Raises:
        MissingBiochemistryError: A required data dependency is undeclared,
            absent from disk, or unloadable. The message says which; none of
            these is attributed to KBDL issue #41.
        MissingSolverError: No backend in ``solver_preference`` completed a real
            solve.
        ModelSEEDpyCapabilityError: ``require_alias_matching`` is set and the
            installed ModelSEEDpy does not actually match roles by alias.

    This is a PUBLIC helper because a KBase CTS job needs it before it can
    construct anything: ``MSBiochemUtils.__init__`` loads the biochemistry
    eagerly, so a job that has not resolved ``ModelSEEDDatabase`` first fails at
    construction with no useful message. It writes a REDUCED dependency config
    containing only ``data_dependencies`` -- see
    :data:`RECONSTRUCTION_DATA_DEPENDENCIES` for the ``sys.path`` shadowing that
    forces this -- and it returns the manager rather than installing a
    monkeypatch, which is how GAA's ``recon_driver`` does the same job.
    """
    import yaml

    from ...core.dependency_manager import (
        DEFAULT_DEPENDENCIES_FILE,
        DependencyManager,
    )

    config_path = Path(
        dependency_config_path
        if dependency_config_path is not None
        else DEFAULT_DEPENDENCIES_FILE
    ).expanduser()
    if not config_path.is_file():
        raise MissingBiochemistryError(
            f"dependency configuration {config_path} does not exist, so "
            f"{list(data_dependencies)} cannot be resolved. Create it with a "
            "'dependencies:' mapping giving a 'path:' for each."
        )

    raw = yaml.safe_load(config_path.read_text()) or {}
    declared = raw.get("dependencies", {}) or {}
    missing = [name for name in data_dependencies if name not in declared]
    if missing:
        raise MissingBiochemistryError(
            f"{config_path} declares no entry for {missing}. This is a missing "
            "declaration, NOT KBDL issue #41 -- the layer never falls back to "
            "modelseedpy's config.cfg default, so adding the entry is the fix."
        )

    subset = {name: declared[name] for name in data_dependencies}
    scratch = Path(scratch_dir or os.environ.get("TMPDIR") or "/tmp")
    scratch.mkdir(parents=True, exist_ok=True)
    reduced_config = scratch / "kbu_recon_dependencies.yaml"
    reduced_config.write_text(yaml.safe_dump({"dependencies": subset}))

    manager = DependencyManager(config_path=reduced_config, auto_init=False)
    manager.initialize_dependencies()

    resolved: Dict[str, str] = {}
    unresolved: List[str] = []
    for name in data_dependencies:
        path = manager.get_data_path(name)
        if path is None:
            unresolved.append(f"{name} (declared {declared[name].get('path')!r})")
        else:
            resolved[name] = str(path)
    if unresolved:
        raise MissingBiochemistryError(
            f"declared but not present on disk: {unresolved}. "
            "DependencyManager.get_data_path returned None for these, which is "
            "what makes every consumer fall back to a sibling-directory guess. "
            "This is a MISSING PATH, not KBDL issue #41."
        )

    solver = None
    probe_objective = None
    for candidate in solver_preference:
        probe_objective = _probe_solver(candidate)
        if probe_objective is not None:
            solver = candidate
            break
    if solver is None or probe_objective is None:
        raise MissingSolverError(
            f"none of {list(solver_preference)} completed a real solve. "
            "Availability was not consulted and is not evidence (#35): a "
            "licensed-looking Gurobi is refused by the poplar licence server at "
            "major version 14 and up, and with gurobipy 14 installed even a GLPK "
            "solve fails because cobra selects its interface at Model "
            "construction. Install GLPK (via optlang/swiglpk) or pin "
            "gurobipy<14."
        )
    import cobra

    # Make the probed backend the process default, so a Model constructed later
    # does not pick up a different interface at construction time.
    cobra.Configuration().solver = solver

    compound_count: Optional[int] = None
    reaction_count: Optional[int] = None
    if load_biochemistry:
        db_path = resolved["ModelSEEDDatabase"]
        try:
            from modelseedpy.biochem import modelseed_biochem as _msbiochem

            if _msbiochem.ModelSEEDBiochem.default_biochemistry is None:
                _msbiochem.ModelSEEDBiochem.default_biochemistry = (
                    _msbiochem.from_local(database_path=db_path)
                )
            biochem = _msbiochem.ModelSEEDBiochem.get()
        except Exception as exc:  # noqa: BLE001 - reclassified below
            raise MissingBiochemistryError(
                f"the ModelSEED biochemistry at {db_path} exists but could not "
                f"be loaded: {type(exc).__name__}: {exc}"
            ) from exc
        compound_count = len(biochem.compounds)
        reaction_count = len(biochem.reactions)

    alias_gene_ids: Tuple[str, ...] = ()
    if require_alias_matching:
        alias_gene_ids = verify_alias_matching(ontology_term=ontology_term)

    version, revision = resolve_distribution_provenance("modelseedpy")
    return ReconstructionEnvironment(
        dependency_config_path=str(config_path),
        dependency_paths=resolved,
        dependency_manager=manager,
        solver=solver,
        solver_probe_objective=probe_objective,
        biochemistry_loaded=load_biochemistry,
        biochemistry_compound_count=compound_count,
        biochemistry_reaction_count=reaction_count,
        alias_matching_verified=require_alias_matching,
        alias_matching_gene_ids=alias_gene_ids,
        modelseedpy_version=version,
        modelseedpy_revision=revision,
    )


# ---------------------------------------------------------------------------
# Genome classification
# ---------------------------------------------------------------------------

#: Classifier artifact upstream ships and this layer loads.
#:
#: OBSERVED in the pinned install's ``modelseedpy/data/ml``: a single
#: ``linear_svc_ACNP_RAST_full_11_11_2026.npz`` and NO pickles.
#: ``modelseedpy.helpers.get_classifier`` loads it via
#: ``load_classifier_from_npz`` with ``allow_pickle=False`` into an
#: ``MSLinearModel``, which is NOT an sklearn estimator -- so prediction needs no
#: scikit-learn and the scikit-learn-1.2.0 pin that the legacy ``*.pickle``
#: classifiers forced is gone. ``helpers.get_classifier`` retains a
#: download-on-demand pickle path for other ids; this id does not take it
#: (measurement (e)).
DEFAULT_GENOME_CLASSIFIER_ID = "linear_svc_ACNP_RAST_full_11_11_2026"


def template_key_for_genome_class(genome_class: str) -> str:
    """Map a classifier class letter to a template key.

    Args:
        genome_class: Letter from ``MSGenomeClassifier.classify``, e.g. ``"N"``.

    Returns:
        The template key to load: ``"gp"``, ``"gn"`` or ``"ar"``.

    Any class with no dedicated template -- ``"C"`` (Cyanobacteria), the
    classifier's ``"--"`` no-call, or a letter this layer has never seen --
    resolves to ``"gn"``. See :data:`GENOME_CLASS_TO_TEMPLATE_KEY`.
    """
    return GENOME_CLASS_TO_TEMPLATE_KEY.get(genome_class, "gn")


def classify_genome_class(
    genome: Any,
    genome_classifier: Optional[Any] = None,
    ontology_term: str = DEFAULT_ONTOLOGY_TERM,
    classifier_id: str = DEFAULT_GENOME_CLASSIFIER_ID,
) -> str:
    """Classify a genome, returning the raw class LETTER.

    Args:
        genome: ``MSGenome`` whose features carry functions under
            ``ontology_term``.
        genome_classifier: Pre-loaded ``MSGenomeClassifier``. Loaded from the
            packaged artifact when ``None``.
        ontology_term: Ontology key the classifier reads. Passed EXPLICITLY --
            the classifier reads ``feature.ontology_terms[ontology_term]`` and
            touches no sequence, so a genome annotated under another key
            classifies as nothing at all if this is wrong.
        classifier_id: Artifact id for ``modelseedpy.helpers.get_classifier``.

    Returns:
        The class letter, e.g. ``"N"``.
    """
    if genome_classifier is None:
        from modelseedpy.helpers import get_classifier

        genome_classifier = get_classifier(classifier_id=classifier_id)
    return genome_classifier.classify(
        genome_or_roles=genome, ontology_term=ontology_term
    )

# ---------------------------------------------------------------------------
# Pure measurement helpers
#
# Module-level functions rather than methods, because they depend on nothing
# about the utility instance -- and because the repo's existing build-path
# tests call ``build_metabolic_model`` UNBOUND against a lightweight fake
# ``self``, so every ``self.<helper>`` this method needs is another attribute
# that fake has to grow before it can test anything.
# ---------------------------------------------------------------------------

def _model_facts(model: Any) -> Dict[str, Any]:
    """Structural census of a cobra model, taken at one stage boundary.

    Args:
        model: ``cobra.Model``.

    Returns:
        Counts and ids. Every value is read directly off the model; there is no
        ``getattr`` default anywhere, because a defaulted count reads as a
        measurement.
    """
    ids = [rxn.id for rxn in model.reactions]
    return {
        "reactions": len(ids),
        "metabolites": len(model.metabolites),
        "genes": len(model.genes),
        "exchanges": sum(1 for i in ids if i.startswith("EX_")),
        "drains": sum(1 for i in ids if i.startswith(("DM_", "SK_"))),
        "biomass_ids": tuple(sorted(i for i in ids if i.startswith("bio"))),
        "objective": str(model.objective.expression),
        "atpm_present": any(i.startswith("ATPM") for i in ids),
        "reaction_ids": tuple(sorted(ids)),
    }


def _current_solver(model: Any) -> Optional[str]:
    """Name of the LP interface a model is actually using.

    Args:
        model: ``cobra.Model``.

    Returns:
        The optlang interface's short name (e.g. ``"glpk"``), or ``None`` if the
        model exposes no solver interface.
    """
    interface = getattr(model.solver, "interface", None)
    if interface is None:
        return None
    return str(interface.__name__).rsplit(".", 1)[-1].replace("_interface", "")


def _solution_changes_the_model(mdlutl: Any, solution: Mapping[str, Any]) -> bool:
    """Would integrating ``solution`` actually change the model?

    Args:
        mdlutl: ``MSModelUtil`` the solution would be integrated into.
        solution: An ``MSGapfill.run_gapfilling`` result.

    Returns:
        ``True`` if the solution names a reaction the model lacks, or a bound
        that ``integrate_gapfill_solution`` would re-open.

    The SECOND of the two guards against the destructive re-integration
    described in :meth:`MSReconstructionUtils.gapfill_metabolic_model`'s loop.
    The first -- skip the occurrence entirely when the model already meets the
    objective -- catches the common case. This one catches the remainder: a
    model that does NOT meet the objective, handed a solution that nonetheless
    contributes nothing. Integrating that still runs
    ``test_solution(keep_changes=True)`` over reactions the model already has,
    with nothing gained and reactions potentially lost.
    """
    for reaction_id in solution["new"]:
        if reaction_id not in mdlutl.model.reactions:
            return True
    for reaction_id, direction in solution["reversed"].items():
        if reaction_id not in mdlutl.model.reactions:
            return True
        reaction = mdlutl.model.reactions.get_by_id(reaction_id)
        # Mirrors integrate_gapfill_solution's own conditions exactly.
        if direction == ">" and reaction.upper_bound <= 0:
            return True
        if direction == "<" and reaction.lower_bound >= 0:
            return True
    return False


def _objective_value_on_media(
    mdlutl: Any, media: Any, objective: str
) -> Optional[float]:
    """Optimise ``objective`` on ``media`` and return its value.

    Args:
        mdlutl: ``MSModelUtil`` whose model is solved.
        media: Medium to constrain the exchanges to.
        objective: Reaction id to maximise.

    Returns:
        The objective value, or ``None`` if the solve produced no number.

    The objective is SET EXPLICITLY every time, which is the whole point. ATP
    correction leaves the model's objective on the ATP hydrolysis reaction --
    ``MSATPCorrection.build_tests`` does
    ``self.model.objective = self.atp_hydrolysis.id`` and never restores it --
    so a bare ``slim_optimize()`` after a build measures ATP flux and reports it
    as growth. That misread a POST-ATP model as already meeting its biomass
    objective on a medium it could not grow on, and recorded a gap-fill
    occurrence as ``"no_gaps"`` when it had real gaps.
    """
    mdlutl.pkgmgr.getpkg("KBaseMediaPkg").build_package(media)
    mdlutl.model.objective = objective
    value = mdlutl.model.slim_optimize()
    if value is None or value != value:  # NaN is not a measurement
        return None
    return float(value)


class MSReconstructionUtils(KBModelUtils):
    """Utilities for metabolic model reconstruction and gapfilling.

    Provides methods for building models from genomes, running gapfilling,
    and related metabolic modeling operations. This class translates the
    functionality from ModelSEEDRecon into a KBUtilLib-compatible interface
    with standard named arguments instead of params dictionaries.
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialize ModelSEED reconstruction utilities.

        Args:
            **kwargs: Additional keyword arguments passed to KBModelUtils
        """
        super().__init__(**kwargs)
        # NOTE: do NOT reset self.FBAModel here. KBModelUtils.__init__ already binds
        # it to the real cobrakbase FBAModel class; setting it to None afterwards
        # destroyed that binding, so save_model's
        #     isinstance(mdlutl.model, self.FBAModel)
        # raised "arg 2 must be a type". The kb_* SDK wrappers happened to mask this
        # by lazily calling _kbase_imports(), which rebinds it -- so only callers of
        # the callback-free core (build_metabolic_model) ever saw the failure.
        self._reconstruction_imports()
        self.version = "0.1.0.msrecon"
        self.native_ontology = False

    @property
    def module_dir(self) -> str:
        """Get the KBUtilLib repo-root directory (contains pyproject.toml / data/).

        Located by walking up from this source file rather than a fixed dirname
        depth: this module lives several packages deep (src/kbutillib/domains/
        modeling/), so the old dirname(dirname(src_dir)) resolved to src/kbutillib
        after the domains/ reorg and broke every module_dir consumer (the jinja
        report loader and ontology_data_dir's SSO-dictionary resolver).
        """
        from pathlib import Path
        here = Path(__file__).resolve()
        for anc in here.parents:
            if (anc / "pyproject.toml").is_file():
                return str(anc)
        return str(here.parents[3])

    @property
    def modelseedpy_data_dir(self) -> str:
        """Get the ModelSEEDpy data directory path.

        Returns:
            Path to ModelSEEDpy data directory
        """
        import modelseedpy
        return os.path.join(os.path.dirname(modelseedpy.__file__), "data")

    def _reconstruction_imports(self) -> None:
        """Import additional modules needed for reconstruction."""
        try:
            from modelseedpy import (
                MSATPCorrection,
                MSBuilder,
                MSGapfill,
                MSModelReport,
            )
            from modelseedpy.helpers import get_template

            self.MSBuilder = MSBuilder
            self.MSGapfill = MSGapfill
            self.MSATPCorrection = MSATPCorrection
            self.MSModelReport = MSModelReport
            self.get_template_helper = get_template

        except ImportError as e:
            self.log_error(f"Failed to import reconstruction modules: {e}")
            raise

    def _kbase_imports(self) -> None:
        """Import additional modules needed for kbase-related operations."""
        try:
            from cobrakbase.core.kbasefba import FBAModel
            self.FBAModel = FBAModel

        except ImportError as e:
            self.log_error(f"Failed to import kbase-related modules: {e}")
            raise

    def _add_reactions_from_gene_mapping(
        self,
        mdlutl: Any,
        builder: Any,
        reaction_gene_hash: Dict[str, Union[List[str], str]],
        template: Any,
        template_reactions_only: bool = False,
    ) -> List[Any]:
        """Add reactions to model based on reaction-to-gene mapping.

        Pulls reaction definitions from the template or ModelSEED database
        and adds them to the model with the specified gene associations.

        Args:
            mdlutl: MSModelUtil object for the model
            builder: MSBuilder object for creating reactions
            reaction_gene_hash: Dictionary mapping reaction IDs to EITHER a list of
                gene IDs OR a ready-made GPR string. The two value types are handled
                differently and never mixed:

                * **list** -- the isozyme shorthand. A new reaction gets
                  ``" or ".join(gene_ids)``; an existing reaction has each gene id
                  appended with ``" or "`` if it is not already a substring of the
                  current rule. This is the historical behaviour and is preserved
                  byte-for-byte.
                * **str** -- an arbitrary GPR (``"(gA and gB) or gC"``) used
                  verbatim, so enzyme complexes survive. A new reaction's rule is
                  the string as given. For an existing reaction the two rules are
                  OR'd with explicit parentheses --
                  ``"(" + existing + ") or (" + incoming + ")"`` -- unless one side
                  is empty, in which case the non-empty side is used as-is.

                Callers that need the gene ids back out of a str value must parse it
                with cobra (``cobra.core.gene.GPR.from_string(s).genes``) rather than
                splitting on ``" or "``.
            template: Template object for the model
            template_reactions_only: If True, only add reactions that exist in the
                template; the ModelSEED-database fallback (which would otherwise pull
                any mass-balanced reaction) is skipped, and reactions absent from the
                template are silently dropped. If False (default), reactions missing
                from the template fall back to any mass-balanced ("MI"/"CI"-free)
                ModelSEED-database reaction.

        Returns:
            List of reactions that were added to the model
        """
        SBO_ANNOTATION = "sbo"
        modelseeddb = self.biochem_db
        reactions_added = []

        for rxn_id, gene_spec in reaction_gene_hash.items():
            if rxn_id + "_c0" not in mdlutl.model.reactions:
                reaction = None
                template_reaction = None

                # Try to get reaction from template first
                if rxn_id + "_c" in template.reactions:
                    template_reaction = template.reactions.get_by_id(rxn_id + "_c")
                # Fall back to ModelSEED database (any mass-balanced reaction),
                # unless restricted to template reactions only.
                elif not template_reactions_only and rxn_id in modelseeddb.reactions:
                    rxnobj = modelseeddb.reactions.get_by_id(rxn_id)
                    if "MI" not in rxnobj.status and "CI" not in rxnobj.status:
                        template_reaction = rxnobj.to_template_reaction({0: "c", 1: "e"})

                if template_reaction:
                    # Add metabolites if needed
                    for m in template_reaction.metabolites:
                        if m.compartment not in builder.compartments:
                            builder.compartments[m.compartment] = (
                                builder.template.compartments.get_by_id(m.compartment)
                            )
                        if m.id not in builder.template_species_to_model_species:
                            model_metabolite = m.to_metabolite(builder.index)
                            builder.template_species_to_model_species[m.id] = model_metabolite
                            builder.base_model.add_metabolites([model_metabolite])

                    # Create the reaction
                    reaction = template_reaction.to_reaction(builder.base_model, builder.index)

                    # Build the GPR. A str value is a GPR the caller already
                    # computed (enzyme complexes included) and is used verbatim; a
                    # list is the isozyme shorthand and is OR'd as it always was.
                    if isinstance(gene_spec, str):
                        gpr = gene_spec
                    else:
                        gpr = " or ".join(gene_spec)
                    reaction.gene_reaction_rule = gpr
                    reaction.annotation[SBO_ANNOTATION] = "SBO:0000176"

                    mdlutl.model.add_reactions([reaction])
                    reactions_added.append(reaction)
                elif not template_reactions_only:
                    # In template-only mode, reactions absent from the template are
                    # expected and dropped silently (otherwise this floods stdout).
                    print(f"Reaction {rxn_id} not found in template or database!")
            else:
                # Update existing reaction with additional genes
                rxn = mdlutl.model.reactions.get_by_id(rxn_id + "_c0")
                existing_gpr = rxn.gene_reaction_rule
                if isinstance(gene_spec, str):
                    # Two GPRs, OR'd with explicit parentheses so neither side's
                    # internal "and" can be re-associated by the merge.
                    if not existing_gpr:
                        existing_gpr = gene_spec
                    elif gene_spec:
                        existing_gpr = "(" + existing_gpr + ") or (" + gene_spec + ")"
                else:
                    # Unchanged list merge, substring check and all. The
                    # `gene_id not in existing_gpr` test is a substring test, not a
                    # token test, so a gene id that is a substring of one already in
                    # the rule is silently skipped -- preserved deliberately for
                    # byte-compatibility; see agent-io/work-records/.
                    for gene_id in gene_spec:
                        if gene_id not in existing_gpr:
                            if existing_gpr:
                                existing_gpr += " or " + gene_id
                            else:
                                existing_gpr = gene_id
                rxn.gene_reaction_rule = existing_gpr

        return reactions_added

    def classify_genome(
        self,
        genome: Any,
        genome_classifier: Optional[Any] = None,
        ontology_term: str = DEFAULT_ONTOLOGY_TERM,
        classifier_id: str = DEFAULT_GENOME_CLASSIFIER_ID,
    ) -> Tuple[str, str]:
        """Classify a genome and resolve the template key to build against.

        Args:
            genome: ``MSGenome`` whose features carry functions under
                ``ontology_term``.
            genome_classifier: Pre-loaded ``MSGenomeClassifier``. When ``None``
                the packaged artifact named by ``classifier_id`` is loaded.
            ontology_term: Ontology key the classifier reads, passed EXPLICITLY.
            classifier_id: Classifier artifact id. Defaults to
                :data:`DEFAULT_GENOME_CLASSIFIER_ID`, the ``.npz`` upstream
                actually ships; see that constant for why the ``.npz`` matters
                (no pickle, no scikit-learn version pin).

        Returns:
            ``(genome_class, template_key)`` -- the raw class LETTER from the
            classifier and the template key to load. Classes with no dedicated
            template resolve to ``"gn"``; see
            :func:`template_key_for_genome_class`.
        """
        genome_class = classify_genome_class(
            genome=genome,
            genome_classifier=genome_classifier,
            ontology_term=ontology_term,
            classifier_id=classifier_id,
        )
        return genome_class, template_key_for_genome_class(genome_class)

    def build_metabolic_model(
        self,
        genome: Any,
        genome_classifier: Optional[Any] = None,
        base_model: Optional[Any] = None,
        model_id: Optional[str] = None,
        model_name: Optional[str] = None,
        gs_template: str = "auto",
        core_template: Optional[Any] = None,
        gs_template_obj: Optional[Any] = None,
        ontology_term: str = DEFAULT_ONTOLOGY_TERM,
        match_role_aliases: bool = False,
        allow_incomplete_complexes: bool = True,
        atp_safe: bool = True,
        forced_atp_list: List[str] = None,
        atp_medias: List[str] = None,
        load_default_medias: bool = True,
        max_gapfilling: int = 10,
        gapfilling_delta: float = 0,
        remove_noncore_genome_reactions: bool = False,
        reactions_to_add: Optional[Dict[str, Union[List[str], str]]] = None,
        template_reactions_only: bool = False,
        allow_all_non_grp_reactions: bool = False,
        atp_hydrolysis_id: str = "ATPM_c0",
        collect_result: bool = False,
    ) -> tuple:
        """Build a model NATIVELY from a supplied template and genome.

        Runs the DRAFT and POST-ATP stages (see the module docstring for the
        binding stage definitions). Gap-fill is a separate call --
        :meth:`gapfill_metabolic_model` -- so the caller chooses the media.

        Args:
            genome: ``MSGenome`` to build from. Its features must carry function
                strings under ``ontology_term``.
            genome_classifier: Pre-loaded ``MSGenomeClassifier``. Only consulted
                when classification is actually needed, i.e. when
                ``gs_template_obj`` is ``None`` AND ``gs_template == "auto"``;
                ``None`` is fine (and is the default) when a template is
                supplied.
            base_model: Optional ``cobra.Model`` to build into. Created from
                ``model_id``/``model_name`` when ``None``.
            model_id: Model id. Defaults to ``genome.id``.
            model_name: Model name. Defaults to ``genome.scientific_name``.
            gs_template: Template key (``"gp"``, ``"gn"``, ``"ar"``) or
                ``"auto"`` to classify. IGNORED when ``gs_template_obj`` is
                supplied.
            core_template: Core ``MSTemplate`` for ATP correction. SUPPLY IT:
                the fallback loads it through KBase, which a KBase-free CTS job
                cannot do.
            gs_template_obj: The genome-scale ``MSTemplate`` to build from.
                SUPPLYING THIS SKIPS CLASSIFICATION ENTIRELY -- no classifier is
                loaded, no template is fetched, and ``gs_template`` is not read.
                This is the KBDL path: KBDL assembles an ``MSTemplate`` from its
                own ``kbdl-template`` + ``kbdl-mapping`` documents and hands it
                over, so nothing here may second-guess it.
            ontology_term: Ontology key MSBuilder reads functions from, passed
                to ``MSBuilder`` EXPLICITLY rather than left to its default.
                KBDL folds the union of function strings from every annotation
                source under one key and names that key here.
            match_role_aliases: Opt in to matching genome functions against
                template role ALIASES as well as role names. Off by default
                because native ModelSEED templates alias 20,551 of 20,552 core
                roles with identifiers like ``msfr.1407``, so switching it on
                unconditionally would change every native build. KBDL's
                templates carry each mapping cluster's functions as aliases of
                one role, which is exactly what this is for. Requires the
                alias-matching revision of ModelSEEDpy; check for it up front
                with :func:`verify_alias_matching`.
            allow_incomplete_complexes: Admit a complex when only some of its
                triggering, non-optional subunits are matched. Upstream's
                default, kept.
            atp_safe: Run ATP correction.
            forced_atp_list: Media ids in which ATP production must be forced.
            atp_medias: ADDITIONAL ATP media beyond the packaged default set.
            load_default_medias: Include the packaged default ATP media
                (``atp_medias.tsv``). Against UPSTREAM ModelSEEDpy this layer
                loads them itself and passes ``load_default_medias=False`` to
                ``MSATPCorrection``, because upstream's own ``True`` path raises
                ``TypeError: 'bool' object is not callable``; against the fork,
                whose loader is a method, the flag is forwarded as given. See
                the implementation comment.
            max_gapfilling: Maximum core gap-filling allowed per ATP medium.
            gapfilling_delta: Acceptance window around the lowest gap-filling
                count.
            remove_noncore_genome_reactions: Remove non-core genome reactions
                (keeping ``bio``/``DM_``/``EX_``/``SK_``) before ATP correction.
            reactions_to_add: Optional reaction-id to gene-spec mapping injected
                after the draft, for callers that compute their own reaction set
                (the legacy ROLELESS path). A value may be a list of gene ids,
                OR'd into an isozyme GPR, or a ready-made GPR string used
                verbatim. See :meth:`_add_reactions_from_gene_mapping`.
            template_reactions_only: Restrict ``reactions_to_add`` to reactions
                present in the template, skipping the ModelSEED-database
                fallback.
            allow_all_non_grp_reactions: Forwarded to ``build_base_model``.
                Leave ``False``: forcing all spontaneous/universal reactions in
                adds reactions whose metabolites are absent, creating dead-end
                species.
            atp_hydrolysis_id: Reaction id of ATP hydrolysis, passed to
                ``MSATPCorrection`` EXPLICITLY. ``build_base_model`` does not add
                ``ATPM``, so this layer adds it with ``MSBuilder.add_atpm`` and
                then names it here rather than leaving upstream to do a SEED
                reaction search for it.
            collect_result: Also return a frozen :class:`ReconstructionResult`
                carrying the draft and post-ATP facts.

        Returns:
            ``(current_output, mdlutl)`` by default -- the report row and the
            ``MSModelUtil``, unchanged from before so existing callers keep
            working. With ``collect_result=True``, ``(current_output, mdlutl,
            result)`` where ``result`` is a :class:`ReconstructionResult` whose
            gap-fill fields are empty (nothing has been gap-filled yet) and
            whose draft/post-ATP fields are populated from measurements taken
            at those two boundaries.

        Raises:
            ValueError: If ``gs_template`` is not ``"auto"`` and names no
                known template key, or if classification is needed and no
                classifier could be used.
        """
        # Set defaults for mutable arguments
        if forced_atp_list is None:
            forced_atp_list = []
        if atp_medias is None:
            atp_medias = []
        if reactions_to_add is None:
            reactions_to_add = {}

        # Initialize output row
        current_output = {
            "Model": None,
            "Genome": None,
            "Genes": None,
            "Class": None,
            "Model genes": None,
            "Reactions": None,
            "Core GF": None,
            "GS GF": None,
            "Growth": None,
            "Comments": [],
        }

        # Set genome info
        gid = model_id if model_id else genome.id
        current_output["Genome"] = genome.id
        current_output["Genes"] = len(genome.features) if hasattr(genome, "features") else 0

        # ---- Template selection -------------------------------------------
        #
        # A SUPPLIED template short-circuits all of this. KBDL builds an
        # MSTemplate from its own template + mapping documents; classifying the
        # genome to pick a different one would discard it, and loading anything
        # through self.get_template() would reach for a KBase workspace that a
        # CTS job does not have. So when gs_template_obj is given, no classifier
        # is loaded and gs_template is never read.
        active_gs_template = gs_template_obj
        genome_class: Optional[str] = None
        if active_gs_template is None:
            template_type = gs_template
            if template_type == "auto":
                genome_class, template_type = self.classify_genome(
                    genome=genome,
                    genome_classifier=genome_classifier,
                    ontology_term=ontology_term,
                )
                current_output["Class"] = GENOME_CLASS_NAMES.get(
                    genome_class, f"Unrecognized genome class {genome_class}"
                )
                if genome_class not in GENOME_CLASSES_WITH_DEDICATED_TEMPLATE:
                    # NOT a skip, and NOT silent. The old code returned
                    # (row, None) for class "C" and for any letter it had not
                    # enumerated, so a classifier letter silently decided that a
                    # genome got no model at all. The no-dedicated-template rule
                    # builds against "gn" instead and records why in the row.
                    current_output["Comments"].append(
                        f"Genome class {genome_class!r} has no dedicated "
                        f"template; built against {template_type!r}."
                    )
            if template_type not in self.templates:
                raise ValueError(
                    f"gs_template={template_type!r} names no known template; "
                    f"known keys are {sorted(self.templates)}. Supply an "
                    "MSTemplate as gs_template_obj to bypass template lookup."
                )
            active_gs_template = self.get_template(self.templates[template_type], None)
        elif gs_template != "auto":
            current_output["Comments"].append(
                f"gs_template={gs_template!r} ignored: gs_template_obj was supplied."
            )

        # Core template for ATP correction. Only loaded when ATP correction will
        # actually run, so a caller that does not need it never touches KBase.
        if core_template is None and atp_safe:
            core_template = self.get_template(self.templates["core"], None)

        # Create base model if not provided
        if base_model is None:
            import cobra

            base_model = cobra.Model(
                id_or_model=gid,
                name=model_name if model_name else genome.scientific_name,
            )

        # ---- DRAFT ---------------------------------------------------------
        #
        # build_base_model, NEVER build(). Upstream build() adds ATPM, runs
        # MSATPCorrection AND calls MSGapfill.run_gapfilling itself, returning a
        # 4-tuple; it would collapse all three stage boundaries and choose the
        # gap-fill medium for us. build_base_model returns a bare cobra model and
        # stops exactly at the DRAFT boundary: on the committed H. influenzae
        # fixture against the pinned ModelSEEDpy it yields 957 reactions, 588
        # genes, 73 exchanges and a bio1 objective, with ATPM absent
        # (measurement (a)).
        #
        # Every argument is a KEYWORD. The call this replaced was
        # ``builder.build(base_model, "0", False, False)``, which against
        # upstream bound "0" to gapfill_media, False to index, and left
        # annotate_with_rast at its default True -- a live RAST network call.
        # annotate_with_rast is pinned False here: the genome arrives annotated,
        # and re-annotating it over the network is both wrong and slow.
        builder = self.MSBuilder(
            genome=genome,
            template=active_gs_template,
            ontology_term=ontology_term,
        )
        mdl = builder.build_base_model(
            model_or_id=base_model,
            index="0",
            allow_all_non_grp_reactions=allow_all_non_grp_reactions,
            annotate_with_rast=False,
            add_reaction_from_rast_annotation=True,
            match_role_aliases=match_role_aliases,
            allow_incomplete_complexes=allow_incomplete_complexes,
        )
        mdl.genome = genome
        mdl.template = active_gs_template
        current_output["Core GF"] = "NA"

        draft_facts = _model_facts(model=mdl)

        mdlutl = self.MSModelUtil.get(model=mdl)
        # Expose the initialised builder so callers can add more reactions (with
        # correct compartment/species maps) AFTER the build -- e.g. an ATP-safe
        # annotation overlay, which lives in GAA's recon_driver, not here.
        mdlutl.recon_builder = builder

        # Remove non-core genome reactions if requested
        if remove_noncore_genome_reactions:
            rxns_to_remove = []
            for rxn in mdlutl.model.reactions:
                if (
                    not mdlutl.is_core(rxn)
                    and rxn.id[0:3] != "bio"
                    and rxn.id[0:3] != "DM_"
                    and rxn.id[0:3] != "EX_"
                    and rxn.id[0:3] != "SK_"
                ):
                    rxns_to_remove.append(rxn.id)
            if rxns_to_remove:
                mdl.remove_reactions(rxns_to_remove)

        # ---- Roleless-path compensations, now CONDITIONAL ------------------
        #
        # Both of the following exist only for the ROLELESS path, where
        # build_base_model contributes no metabolic reactions and the caller
        # injects its whole reaction set through reactions_to_add. They are
        # gated on `reactions_to_add` for two independent reasons:
        #
        #   1. On a ROLE-BASED draft they are measured STRICT NO-OPS. Re-running
        #      build_non_metabolite_reactions returned 0 reactions and added
        #      none; add_exchanges_to_model added none. The draft stayed at 957
        #      reactions (measurement (f)). They cost a full template sweep and
        #      change nothing.
        #   2. build_base_model ALREADY calls both internally -- the
        #      spontaneous/universal sweep at msbuilder.py:1130 and
        #      add_exchanges_to_model at :1136. Running them again on a
        #      role-based build is a double application, which is the thing this
        #      gate exists to prevent. Both happen to be idempotent today
        #      (add_exchanges_to_model skips existing EX_ ids;
        #      build_non_metabolite_reactions skips reaction ids already in the
        #      model), but relying on that is relying on an implementation
        #      detail of someone else's library.
        #
        # On the roleless path they remain load-bearing and the ORDER between
        # them is load-bearing too:
        #
        #   * build_non_metabolite_reactions admits a spontaneous or universal
        #     template reaction only when one of its metabolites is already in
        #     builder.template_species_to_model_species. On the roleless path
        #     that map is empty when build_base_model runs it, so almost nothing
        #     qualifies; the reactions lost include membrane diffusion of O2
        #     (rxn05468), CO2 (rxn05467) and water (rxn05319), without which
        #     models cannot respire and instead "grow" by running cytochrome
        #     oxidase rxn14415 backwards to make O2 out of water
        #     (KBDLJobRunningPrototype#29). It must run AFTER
        #     _add_reactions_from_gene_mapping, which is what populates that map.
        #   * add_exchanges_to_model must run after the new diffusion reactions,
        #     so an extracellular species one of them introduces gets an
        #     exchange instead of becoming a flux dead-end. Without it the model
        #     reaches ATP correction with ZERO exchanges, ATP is 0 on all 54 ATP
        #     media, every medium records {"score": 1000, "failed": true},
        #     selected_media ends up empty, nothing raises, and the model ships
        #     with its energy-generating cycles intact (#28, cause 1).
        #
        # allow_all_non_grp_reactions stays False deliberately; see that
        # parameter's docstring.
        if reactions_to_add:
            self._add_reactions_from_gene_mapping(
                mdlutl,
                builder,
                reactions_to_add,
                active_gs_template,
                template_reactions_only=template_reactions_only,
            )
            non_metabolic_reactions = builder.build_non_metabolite_reactions(
                cobra_model=mdlutl.model, allow_all_non_grp_reactions=False
            )
            if non_metabolic_reactions:
                mdlutl.model.add_reactions(non_metabolic_reactions)
            self.MSBuilder.add_exchanges_to_model(model=mdlutl.model)

        # ---- POST-ATP ------------------------------------------------------
        atp_analysis: Dict[str, Any] = {}
        atp_removed: Tuple[str, ...] = ()
        atp_invocations = 0
        atp_test_count: Optional[int] = None
        core_gf_count: Optional[int] = None
        selected_media_ids: Tuple[str, ...] = ()
        if atp_safe:
            pre_atp_ids = {rxn.id for rxn in mdlutl.model.reactions}

            # ATPM is an ATP-specific scaffold, so by the PRD's stage definition
            # it belongs on the POST-ATP side of the DRAFT boundary -- and
            # build_base_model does not add it (measurement (a): ATPM ABSENT).
            # Adding it explicitly and then naming it via atp_hydrolysis_id is
            # what stops MSATPCorrection doing a SEED reaction search for a
            # reaction that is not there.
            self.MSBuilder.add_atpm(model=mdlutl.model)

            # Where the default ATP media get loaded depends on which
            # ModelSEEDpy is installed, and the difference is not cosmetic.
            #
            # UPSTREAM has a module-level function `load_default_medias` AND an
            # __init__ parameter of the same name. At msatpcorrection.py:115 the
            # call `load_default_medias(default_media_path)` resolves to the BOOL
            # PARAMETER, so for any truthy value it raises
            #     TypeError: 'bool' object is not callable
            # Upstream never trips over it because build() passes
            # load_default_medias=False and pre-loads the medias itself. This
            # layer used to pass the flag straight through with default True, so
            # every ATP-safe build against that revision raised before solving
            # (task-d1302dc4 record, caveat 4). So against upstream the media
            # are pre-loaded HERE and the flag is forced False -- exactly what
            # upstream's own build() does.
            #
            # The FORK has no module-level function at all; its loader is the
            # METHOD `self.load_default_medias`, which __init__ calls
            # unambiguously and which works. There the flag is forwarded as the
            # caller asked.
            #
            # Probing for the function rather than branching on a version string
            # is the point: both trees report 0.4.2/0.4.3.
            from modelseedpy.core import msatpcorrection as _msatpcorrection

            default_media_loader = getattr(
                _msatpcorrection, "load_default_medias", None
            )
            effective_atp_medias = list(atp_medias)
            forward_load_default_medias = False
            if load_default_medias:
                if callable(default_media_loader):
                    effective_atp_medias = (
                        default_media_loader(
                            default_media_path=self.modelseedpy_data_dir
                            + "/atp_medias.tsv"
                        )
                        + effective_atp_medias
                    )
                else:
                    forward_load_default_medias = True

            atpcorrection = self.MSATPCorrection(
                model_or_mdlutl=mdlutl,
                core_template=core_template,
                atp_medias=effective_atp_medias,
                load_default_medias=forward_load_default_medias,
                max_gapfilling=max_gapfilling,
                gapfilling_delta=gapfilling_delta,
                forced_media=forced_atp_list,
                atp_hydrolysis_id=atp_hydrolysis_id,
                default_media_path=self.modelseedpy_data_dir + "/atp_medias.tsv",
            )
            # EXACTLY ONCE. run_atp_correction() mutates the model (it disables
            # noncore reactions, gap-fills the core, then re-expands to genome
            # scale while filtering reactions that break ATP), so a second call
            # re-filters an already-filtered model.
            tests = atpcorrection.run_atp_correction()
            atp_invocations = 1
            atp_test_count = len(tests) if tests is not None else None
            core_gf_count = len(atpcorrection.cumulative_core_gapfilling)
            current_output["Core GF"] = core_gf_count
            selected_media = atpcorrection.selected_media or []
            selected_media_ids = tuple(media.id for media in selected_media)

            # Reactions ATP correction removed, by set difference over reaction
            # ids rather than by reading atpcorrection internals: the filter
            # zeroes bounds first and only removes a reaction when both bounds
            # end up zero, so the internal `filtered_noncore` list and the set
            # actually removed are different things.
            atp_removed = tuple(
                sorted(pre_atp_ids - {rxn.id for rxn in mdlutl.model.reactions})
            )
            atp_analysis = {
                "stage": "post-atp",
                "atp_hydrolysis_id": atp_hydrolysis_id,
                "test_count": atp_test_count,
                "core_gapfilling_count": core_gf_count,
                "selected_media_ids": list(selected_media_ids),
                # Keyed by media ID, not by the MSMedia OBJECT that
                # MSATPCorrection keys it by: post_atp_analysis has to survive
                # json.dumps, and an object key makes ReconstructionResult.to_dict
                # raise "keys must be str, int, float, bool or None".
                "media_gapfill_stats": {
                    (media.id if hasattr(media, "id") else str(media)): stats
                    for media, stats in atpcorrection.media_gapfill_stats.items()
                },
                "atp_media_count": len(effective_atp_medias),
                "forced_media": list(forced_atp_list),
                "removed_reaction_ids": list(atp_removed),
            }

            # Publish the ATP tests on the model util so downstream gap-fill can
            # be ATP-safe without recomputing them. MSModelUtil.atp_tests is the
            # attribute get_atp_tests caches into, so a caller may reach these
            # either way. Previously `tests` was computed here and dropped on the
            # floor, which left every genome-scale gap-fill free to re-add the
            # very cycles ATP correction had just filtered out (#28, cause 3).
            mdlutl.atp_tests = tests

        # Set model attributes
        mdlutl.get_attributes()["class"] = current_output["Class"]
        mdlutl.wsid = gid

        current_output["Reactions"] = mdlutl.nonexchange_reaction_count()
        current_output["Model genes"] = len(mdlutl.model.genes)

        if not collect_result:
            return current_output, mdlutl

        msp_version, msp_revision = resolve_distribution_provenance("modelseedpy")
        kbu_version, kbu_revision = resolve_distribution_provenance("KBUtilLib")
        result = ReconstructionResult(
            draft_reaction_count=draft_facts["reactions"],
            draft_metabolite_count=draft_facts["metabolites"],
            draft_gene_count=draft_facts["genes"],
            draft_exchange_count=draft_facts["exchanges"],
            draft_drain_count=draft_facts["drains"],
            draft_biomass_ids=draft_facts["biomass_ids"],
            draft_objective_expression=draft_facts["objective"],
            draft_atpm_present=draft_facts["atpm_present"],
            post_atp_correction_ran=bool(atp_safe),
            post_atp_correction_invocations=atp_invocations,
            post_atp_reaction_count=len(mdlutl.model.reactions),
            post_atp_removed_reaction_ids=atp_removed,
            post_atp_core_gapfilling_count=core_gf_count,
            post_atp_selected_media_ids=selected_media_ids,
            post_atp_test_count=atp_test_count,
            post_atp_analysis=atp_analysis,
            post_gapfill_records=(),
            post_gapfill_reaction_count=None,
            post_gapfill_reaction_exclusion_list=(),
            genome_id=genome.id,
            genome_class=genome_class,
            template_id=active_gs_template.id,
            template_name=active_gs_template.name,
            core_template_id=(
                core_template.id if core_template is not None else None
            ),
            core_template_name=(
                core_template.name if core_template is not None else None
            ),
            ontology_term=ontology_term,
            match_role_aliases=match_role_aliases,
            allow_incomplete_complexes=allow_incomplete_complexes,
            reactions_to_add_supplied=bool(reactions_to_add),
            modelseedpy_version=msp_version,
            modelseedpy_revision=msp_revision,
            kbutillib_version=kbu_version,
            kbutillib_revision=kbu_revision,
            solver=_current_solver(model=mdlutl.model),
        )
        return current_output, mdlutl, result

    def compute_ontology_model_changes(
        self,
        anno_ont: Any,
        annotation_priority: List[str] = None,
        ontology_events: Optional[List] = None,
        merge_annotations: bool = False,
    ) -> Dict[str, List[str]]:
        """Compute reaction-to-gene mapping from ontology annotations.

        This function analyzes annotation ontology to determine which reactions
        should be added to a model based on gene annotations.

        Args:
            anno_ont: Annotation ontology object (genome.annoont)
            annotation_priority: List of annotation priorities (e.g., ["RAST", "KEGG"])
            ontology_events: Specific ontology events to use for extension
            merge_annotations: Whether to merge annotations from multiple sources

        Returns:
            Dictionary mapping reaction IDs to lists of gene IDs
        """
        if annotation_priority is None:
            annotation_priority = []

        reaction_gene_hash = {}

        # Get ontology events if not provided
        if ontology_events is None and len(annotation_priority) > 0:
            ontology_events = anno_ont.get_events_from_priority_list(
                annotation_priority
            )

        # Compute reaction-to-gene mapping from ontology
        if ontology_events is not None or len(annotation_priority) > 0:
            gene_term_hash = anno_ont.get_gene_term_hash(
                ontology_events, None, merge_annotations, False
            )
            # Dropped: self.print_json_debug_file("gene_term_hash", gene_term_hash)
            # -- debug hook with no definition in the package (survives only in
            # kb_model_utils.py.bak), so this raised AttributeError unconditionally
            # and made the bulk-reconstruct path uncallable. Diagnosed by jplfaria
            # in PR #46. Pure debug output, so removal is the whole fix.

            for gene in gene_term_hash:
                for term in gene_term_hash[gene]:
                    if term.ontology.id != "SSO":
                        for rxn_id in term.msrxns:
                            if rxn_id not in reaction_gene_hash:
                                reaction_gene_hash[rxn_id] = []
                            if gene.id not in reaction_gene_hash[rxn_id]:
                                reaction_gene_hash[rxn_id].append(gene.id)

        return reaction_gene_hash

    def kb_build_metabolic_models(
        self,
        workspace: str,
        genome_refs: List[str] = None,
        run_gapfilling: bool = False,
        atp_safe: bool = True,
        forced_atp_list: List[str] = None,
        gapfilling_media_list: List[str] = None,
        suffix: str = ".mdl",
        core_template: str = "auto",
        gs_template: str = "auto",
        gs_template_ref: Optional[Union[str, Any]] = None,
        core_template_ref: Optional[Union[str, Any]] = None,
        template_reactions_only: bool = True,
        output_core_models: bool = False,
        automated_atp_evaluation: bool = True,
        atp_medias: List[str] = None,
        load_default_medias: bool = True,
        max_gapfilling: int = 10,
        gapfilling_delta: float = 0,
        return_model_objects: bool = False,
        return_data: bool = False,
        save_report_to_kbase: bool = True,
        change_to_complete: bool = False,
        gapfilling_mode: str = "Sequential",
        base_media: Optional[Any] = None,
        compound_list: Optional[List[str]] = None,
        base_media_target_element: str = "C",
        expression_refs: Optional[List[str]] = None,
        extend_model_with_ontology: bool = False,
        ontology_events: Optional[List] = None,
        save_models_to_kbase: bool = True,
        save_gapfilling_fba_to_kbase: bool = True,
        annotation_priority: List[str] = None,
        merge_annotations: bool = False,
    ) -> Dict:
        """Build metabolic models from genome references.

        Args:
            workspace: KBase workspace ID or name for output
            genome_refs: List of genome references to build models from
            run_gapfilling: Whether to run gapfilling after model construction
            atp_safe: Whether to apply ATP-safe constraints
            forced_atp_list: List of media IDs where ATP production should be forced
            gapfilling_media_list: List of media references for gapfilling
            suffix: Suffix to add to model IDs
            core_template: Core template type ("auto", "gp", "gn", "ar", etc.)
            gs_template: Gram-specific template type ("auto", "gp", "gn", "ar", etc.)
            gs_template_ref: Direct reference to GS template (workspace ref or object)
            core_template_ref: Direct reference to core template (workspace ref or object)
            template_reactions_only: Only include reactions from template
            output_core_models: Whether to output core models
            automated_atp_evaluation: Whether to run automated ATP evaluation
            atp_medias: List of ATP media references
            load_default_medias: Whether to load default ATP medias
            max_gapfilling: Maximum number of gapfilling iterations
            gapfilling_delta: Delta threshold for gapfilling
            return_model_objects: Whether to return model objects in output
            return_data: Whether to return data table in output
            save_report_to_kbase: Whether to save HTML report to KBase
            change_to_complete: Whether to change default media to complete
            gapfilling_mode: Gapfilling mode ("Sequential" or "Cumulative")
            base_media: Base media object for gapfilling
            compound_list: List of compounds for media supplementation
            base_media_target_element: Target element for base media
            expression_refs: Expression data references
            extend_model_with_ontology: Whether to extend model with ontology
            ontology_events: Ontology events for model extension
            save_models_to_kbase: Whether to save models to KBase workspace
            save_gapfilling_fba_to_kbase: Whether to save gapfilling FBA to KBase
            annotation_priority: List of annotation priorities
            merge_annotations: Whether to merge annotations

        Returns:
            Dictionary with report information and optionally model objects/data
        """
        if self.FBAModel is None:
            self._kbase_imports()
        # Set defaults for mutable arguments
        if genome_refs is None:
            genome_refs = []
        if forced_atp_list is None:
            forced_atp_list = []
        if atp_medias is None:
            atp_medias = []
        if annotation_priority is None:
            annotation_priority = []

        default_media = "KBaseMedia/AuxoMedia"
        if change_to_complete:
            default_media = "KBaseMedia/Complete"

        # Process genome list
        genome_refs = self.process_genome_list(genome_refs, workspace)

        # Process media list
        gapfilling_media_objs = self.process_media_list(
            gapfilling_media_list, default_media, workspace
        )

        # Load templates (using local variables, not class state)
        loaded_gs_template = None
        if gs_template_ref is not None and not isinstance(gs_template_ref, str):
            # User passed the GS template object directly
            loaded_gs_template = gs_template_ref
        elif gs_template_ref:
            # User passed a workspace reference
            loaded_gs_template = self.get_template(gs_template_ref, None)

        loaded_core_template = None
        if core_template_ref is not None and not isinstance(core_template_ref, str):
            # User passed the core template object directly
            loaded_core_template = core_template_ref
        elif core_template_ref:
            # User passed a workspace reference
            loaded_core_template = self.get_template(core_template_ref, None)
        else:
            # Load default core template
            loaded_core_template = self.get_template(self.templates["core"], None)

        # Initialize classifier
        genome_classifier = self.get_classifier()

        # Initialize output data table
        result_table = pd.DataFrame({})

        # Process each genome
        mdllist = []
        for i, gen_ref in enumerate(genome_refs):
            # Get genome with annotation ontology
            genome = self.get_msgenome_from_ontology(
                gen_ref, native_python_api=self.native_ontology, output_ws=workspace
            )

            gid = genome.id
            base_model = self.FBAModel(
                {"id": gid, "name": genome.scientific_name}
            )

            # Compute ontology-based reaction additions if requested (KBase-specific)
            reactions_to_add = {}
            remove_noncore = False
            if extend_model_with_ontology or len(annotation_priority) > 0:
                reactions_to_add = self.compute_ontology_model_changes(
                    anno_ont=genome.annoont,
                    annotation_priority=annotation_priority,
                    ontology_events=ontology_events,
                    merge_annotations=merge_annotations,
                )
                # Remove non-core reactions if RAST not in priority
                if "RAST" not in annotation_priority and "all" not in annotation_priority:
                    remove_noncore = True

            # Call the generalized build_metabolic_model function
            current_output, mdlutl = self.build_metabolic_model(
                genome=genome,
                genome_classifier=genome_classifier,
                base_model=base_model,
                model_id=gid + suffix,
                model_name=genome.scientific_name,
                gs_template=gs_template,
                core_template=loaded_core_template,
                gs_template_obj=loaded_gs_template,
                atp_safe=atp_safe,
                forced_atp_list=forced_atp_list,
                atp_medias=atp_medias,
                load_default_medias=load_default_medias,
                max_gapfilling=max_gapfilling,
                gapfilling_delta=gapfilling_delta,
                remove_noncore_genome_reactions=remove_noncore,
                reactions_to_add=reactions_to_add,
            )

            # Add comment about ontology changes if applicable
            if mdlutl is not None and (extend_model_with_ontology or len(annotation_priority) > 0):
                current_output["Comments"].append(
                    f"Extended model with ontology: added {len(reactions_to_add)} reaction mappings."
                )

            # Add KBase-specific output formatting
            current_output["Model"] = (
                f'{gid}{suffix}<br><a href="{gid}{suffix}-recon.html" target="_blank">'
                f'(see reconstruction report)</a><br><a href="{gid}{suffix}-full.html" '
                f'target="_blank">(see full view)</a>'
            )
            current_output["Genome"] = genome.annoont.info[10]["Name"]
            current_output["Genes"] = genome.annoont.info[10][
                "Number of Protein Encoding Genes"
            ]

            # Skip if model was not built (e.g., unsupported genome class)
            if mdlutl is None:
                result_table = pd.concat(
                    [result_table, pd.DataFrame([current_output])], ignore_index=True
                )
                continue

            # Add KBase-specific references
            mdlutl.model.genome_ref = self.wsinfo_to_ref(genome.annoont.info)
            mdlutl.wsid = gid + suffix

            # Set template refs (requires .info from KBase workspace objects)
            if loaded_core_template and hasattr(loaded_core_template, "info"):
                mdlutl.model.core_template_ref = str(loaded_core_template.info)
            if loaded_gs_template and hasattr(loaded_gs_template, "info"):
                mdlutl.model.template_ref = str(loaded_gs_template.info)

            # Get the GS template from the model (build_metabolic_model may have loaded it)
            model_gs_template = mdlutl.model.template

            genome_objs = {mdlutl: genome}

            # Handle expression data
            expression_objs = None
            if expression_refs:
                expression_objs = self.get_expression_objs(expression_refs, genome_objs)

            # Run gapfilling if requested
            current_output["GS GF"] = "NA"
            if run_gapfilling:
                self.kb_gapfill_metabolic_models(
                    workspace=workspace,
                    media_objs=gapfilling_media_objs,
                    model_objs=[mdlutl],
                    genome_objs=genome_objs,
                    expression_objs=expression_objs,
                    atp_safe=atp_safe,
                    suffix="",
                    default_objective="bio1",
                    output_data={mdlutl: current_output},
                    forced_atp_list=forced_atp_list,
                    templates=[model_gs_template],
                    internal_call=True,
                    gapfilling_mode=gapfilling_mode,
                    base_media=base_media,
                    compound_list=compound_list,
                    base_media_target_element=base_media_target_element,
                    save_models_to_kbase=save_models_to_kbase,
                    save_gapfilling_fba_to_kbase=save_gapfilling_fba_to_kbase,
                )
            else:
                if save_models_to_kbase:
                    self.save_model(mdlutl, workspace, None)
                mdlutl.model.objective = "bio1"
                mdlutl.pkgmgr.getpkg("KBaseMediaPkg").build_package(None)
                current_output["Growth"] = "Complete:" + str(mdlutl.model.slim_optimize())

            current_output["Reactions"] = mdlutl.nonexchange_reaction_count()
            current_output["Model genes"] = len(mdlutl.model.genes)

            result_table = pd.concat(
                [result_table, pd.DataFrame([current_output])], ignore_index=True
            )
            mdllist.append(mdlutl)

        # Build report
        output = {}
        self._build_dataframe_report(result_table, mdllist)

        if save_report_to_kbase:
            output = self.save_report_to_kbase()

        if return_data:
            output["data"] = result_table.to_json()

        if return_model_objects:
            output["model_objs"] = mdllist

        return output

    def gapfill_metabolic_model(
        self,
        mdlutl: Any,
        genome: Any,
        media_objs: Sequence[Any],
        templates: List[Any],
        core_template: Optional[Any] = None,
        source_models: Optional[List[Any]] = None,
        additional_tests: Optional[List[Dict]] = None,
        expression_obj: Optional[Any] = None,
        atp_safe: bool = True,
        reaction_exclusion_list: Optional[List[str]] = None,
        objective: str = "bio1",
        minimum_objective: float = 0.01,
        gapfilling_mode: str = "Sequential",
        base_media: Optional[Any] = None,
        base_media_target_element: str = "C",
        reaction_scores: Optional[Dict[str, float]] = None,
        prefilter: bool = False,
        binary_check: bool = False,
    ) -> tuple:
        """Gap-fill a model on each REQUESTED MEDIUM OCCURRENCE, in order.

        This is the POST-GAP-FILL stage: sequential integration across every
        requested medium (see the module docstring for the stage definitions).

        Args:
            mdlutl: ``MSModelUtil`` to gap-fill, normally the POST-ATP model
                from :meth:`build_metabolic_model`.
            genome: ``MSGenome`` the model was built from. Used only for
                expression weighting.
            media_objs: The requested media IN REQUEST ORDER. Their ids are used
                EXACTLY as given -- nothing here renames, suffixes or
                de-duplicates them -- and the SAME ID MAY APPEAR MORE THAN ONCE.
                Each occurrence is attempted and reported separately.
            templates: Templates supplying gap-fill candidate reactions.
            core_template: Core template for ATP tests. Supply it; the fallback
                loads it through KBase.
            source_models: Source models supplying candidate reactions.
            additional_tests: Extra constraint tests, prepended to the ATP tests.
            expression_obj: Expression data for reaction weighting.
            atp_safe: Compute ATP tests and pass them to gap-filling, so a
                solution cannot re-add the energy-generating cycles ATP
                correction removed.
            reaction_exclusion_list: Reaction ids gap-filling must NOT add.
                Either id form is accepted -- ``"rxn00029"`` or
                ``"rxn00029_c0"`` -- and BOTH are enforced as the BASE reaction,
                i.e. in every compartment. That is not a convenience, it is the
                only semantics the underlying blacklist has:
                ``GapfillingPkg`` compares the blacklist against
                ``template_reaction.reference_id``, a base id, so an entry
                carrying a compartment suffix silently matches nothing. Passing
                ``["rxn00029_c0"]`` straight through was measured letting
                ``rxn00029_c0`` into the model. The list is normalised to base
                ids for ``MSGapfill`` AND re-checked against what each
                occurrence actually ADDED, so a violation raises here rather
                than shipping. An excluded reaction already present in the DRAFT
                is not a violation -- it was not gap-filled in -- and is
                recorded as a comment instead.
            objective: Gap-fill target reaction id.
            minimum_objective: Minimum objective value a solution must reach.
            gapfilling_mode: Only ``"Sequential"`` is supported, and it is what
                this method does: each occurrence is solved and INTEGRATED
                before the next is attempted, so later media see the reactions
                earlier media added. Any other value raises. The parameter is
                kept because callers pass it, but it never worked against
                upstream anyway -- it used to be forwarded to
                ``run_multi_gapfill``, whose signature at the pinned revision
                has no ``gapfilling_mode`` at all, so the call raised TypeError.
            base_media: Base media object for gap-filling. FORK-ONLY on
                ``MSGapfill``; see the implementation comment.
            base_media_target_element: Target element for base media.
            reaction_scores: Reaction scores used to pick a gene for an added
                reaction.
            prefilter: Prefilter the gap-fill database with the test conditions
                before solving, on the FIRST occurrence only (prefiltering
                permanently narrows the shared gap-fill database, so repeating
                it per medium would restrict later media to what an earlier
                medium left behind -- upstream's own ``run_multi_gapfill`` only
                prefilters the first medium for the same reason).

                DEFAULTS TO ``False``, and NOT because ``prefilter=True`` is
                broken -- it is not. Both settings were measured working and
                returned the IDENTICAL solution (163 new reactions, 0 reversed,
                same id set); across repeated runs ``True`` took 41.1-50.8 s and
                ``False`` 35.0-47.4 s, so ``True`` was never the faster of the
                two while producing the same answer (measurement (d)). ``False``
                is therefore the default on cost alone. That timing ordering is
                a single-organism, single-medium observation and should not be
                read as a performance characterisation; a caller who wants
                prefiltering should just ask for it.
            binary_check: Reduce each solution to a minimal reaction set.

        Returns:
            ``(current_output, solutions, output_solution,
            output_solution_media, gapfill_records)``.

            ``gapfill_records`` is an ORDERED ``tuple`` of
            :class:`GapfillRecord`, ONE PER REQUESTED OCCURRENCE, in request
            order -- so a medium requested twice keeps both attempts, with
            distinct ``request_index`` values. ``solutions`` remains the
            historical dict keyed by media OBJECT for existing callers; note
            that it CANNOT represent a repeated request (two occurrences of one
            object collapse to one key), which is why the records exist.

        Raises:
            RuntimeError: If an occurrence ADDED a reaction the exclusion list
                forbids.

        ``MSGapfill.run_multi_gapfill`` is deliberately not used. At the pinned
        revision it takes neither ``integrate_solutions`` nor
        ``run_sensitivity_analysis`` nor ``gapfilling_mode`` (the keywords this
        method used to pass it, which would raise ``TypeError``), it does not
        integrate anything, and it returns a dict keyed by media object. This
        method drives ``run_gapfilling`` + ``integrate_gapfill_solution`` per
        occurrence instead, which is what makes sequential integration and
        per-occurrence reporting possible.

        GAA's ``gapfill_reaction_set`` is neither called nor ported: all
        modelling lives here now.
        """
        import cobra

        # Set defaults for mutable arguments
        if source_models is None:
            source_models = []
        if additional_tests is None:
            additional_tests = []
        if reaction_exclusion_list is None:
            reaction_exclusion_list = []
        if reaction_scores is None:
            reaction_scores = {}
        exclusion = tuple(reaction_exclusion_list)
        # Normalise to BASE ids: that is the only form the blacklist matches.
        excluded_base = frozenset(_base_reaction_id(rxn) for rxn in exclusion)
        if gapfilling_mode != "Sequential":
            raise ValueError(
                f"gapfilling_mode={gapfilling_mode!r} is not supported; this "
                "method integrates sequentially and only 'Sequential' is "
                "meaningful. Upstream's run_multi_gapfill has no "
                "gapfilling_mode parameter at the pinned revision, and its "
                "simultaneous branch is an empty loop."
            )

        # Load core template if not provided
        if core_template is None and atp_safe:
            core_template = self.get_template(self.templates["core"], None)

        # Initialize output
        current_output = {
            "Model": None,
            "Genome": None,
            "Genes": None,
            "Class": None,
            "Model genes": None,
            "Reactions": None,
            "Core GF": None,
            "GS GF": None,
            "Growth": None,
            "Comments": [],
        }

        # Compute tests for ATP-safe gapfilling.
        #
        # ``MSModelUtil.get_atp_tests`` is a FORK-ONLY method -- upstream's
        # msmodelutl.py has neither it nor an ``atp_tests`` attribute -- so
        # calling it unconditionally (as this method used to) raises
        # AttributeError against the pinned revision. The tests this layer wants
        # are the ones ATP correction already built and
        # :meth:`build_metabolic_model` published on the model util, so look
        # there first; fall back to the fork method only where it exists.
        atp_tests: List[Dict] = []
        if atp_safe:
            published = mdlutl.__dict__.get("atp_tests")
            if published:
                atp_tests = list(published)
            elif hasattr(mdlutl, "get_atp_tests"):
                atp_tests = mdlutl.get_atp_tests(
                    core_template=core_template,
                    atp_media_filename=self.modelseedpy_data_dir
                    + "/atp_medias.tsv",
                    recompute=False,
                )
            else:
                raise ModelSEEDpyCapabilityError(
                    "atp_safe=True but no ATP tests are available: this model "
                    "util carries none (build it with "
                    "build_metabolic_model(atp_safe=True), which publishes them) "
                    "and the installed modelseedpy has no "
                    "MSModelUtil.get_atp_tests to recompute them. Recomputing "
                    "them here is not an option -- MSATPCorrection.build_tests "
                    "needs the selected_media that only a full "
                    "run_atp_correction produces. Pass atp_safe=False with "
                    "explicit additional_tests to gap-fill without them, "
                    "accepting that a solution may re-add the energy-generating "
                    "cycles ATP correction removed."
                )

        all_tests = additional_tests + atp_tests

        # Create gapfilling object. blacklist is the exclusion list: MSGapfill
        # takes it into the GapfillingPkg so excluded candidates never enter the
        # LP in the first place.
        #
        # base_media / base_media_target_element are FORK-ONLY parameters:
        # upstream's MSGapfill.__init__ does not declare them, so passing them
        # unconditionally (as this method used to) raises TypeError against the
        # pinned revision. They are forwarded only where the installed signature
        # accepts them, and a caller who actually ASKED for a non-default value
        # on an install that cannot honour it gets a classified error rather than
        # silently losing the setting.
        optional_gapfill_kwargs = _supported_keyword_arguments(
            target=self.MSGapfill.__init__,
            requested={
                "base_media": (base_media, None),
                "base_media_target_element": (base_media_target_element, "C"),
            },
            label="MSGapfill.__init__",
        )
        msgapfill = self.MSGapfill(
            model_or_mdlutl=mdlutl,
            default_gapfill_templates=templates,
            default_gapfill_models=source_models,
            test_conditions=all_tests,
            blacklist=sorted(excluded_base),
            default_target=objective,
            minimum_obj=minimum_objective,
            **optional_gapfill_kwargs,
        )

        # Set reaction scores from genome
        msgapfill.reaction_scores = reaction_scores

        # Handle expression data
        if expression_obj:
            expression_scores = msgapfill.compute_reaction_weights_from_expression_data(
                omics_data=expression_obj, annoont=genome.annoont
            )
            for rxn_id in msgapfill.reaction_scores:
                for gene in msgapfill.reaction_scores[rxn_id]:
                    if gene in expression_scores:
                        msgapfill.reaction_scores[rxn_id][gene]["probability"] = (
                            expression_scores[gene] + 0.5
                        )

        # ---- Sequential integration, one occurrence at a time --------------
        # An excluded reaction already on the DRAFT was not gap-filled in, so it
        # is not a violation of a gap-fill exclusion. Record it and carry on;
        # silently deleting an annotation-supported reaction because a gap-fill
        # exclusion happened to name it would be a much worse answer.
        initial_ids = {rxn.id for rxn in mdlutl.model.reactions}
        pre_existing = sorted(
            rxn_id
            for rxn_id in initial_ids
            if _base_reaction_id(rxn_id) in excluded_base
        )
        if pre_existing:
            current_output["Comments"].append(
                "Excluded reactions already present before gap-fill (not added "
                f"by it, so left in place): {pre_existing}"
            )

        mdlutl.gfutl.cumulative_gapfilling = []
        growth_array = []
        solutions: Dict[Any, Any] = {}
        records: List[GapfillRecord] = []
        for request_index, media in enumerate(media_objs):
            media_id = media.id
            digest = _media_digest(media)
            before_ids = {rxn.id for rxn in mdlutl.model.reactions}
            cumulative: List[List[str]] = []
            status = "no_solution"
            objective_value: Optional[float] = None

            # SEQUENTIAL INTEGRATION MEANS GAP-FILLING ONLY WHAT IS STILL
            # MISSING. If the model already meets the objective on this medium --
            # because an earlier occurrence added what it needed -- there is
            # nothing to gap-fill, and this occurrence is recorded "no_gaps".
            #
            # Not an optimisation, though it does save a solve. Gap-filling
            # anyway is DESTRUCTIVE: MSGapfill clones its gap-fill model once in
            # __init__, from the pre-gap-fill model, so a later occurrence is
            # solved against a model that has none of the earlier additions and
            # returns a solution the real model has mostly already got.
            # Integrating that calls MSModelUtil.test_solution(keep_changes=True),
            # which judges a reaction "unneeded" by zeroing ONE bound and then
            # REMOVES the whole reaction -- so a reaction still carrying flux the
            # other way is deleted anyway. Measured on H. influenzae with the
            # same medium requested twice: glucose 0.6548 and succinate 0.4348
            # before the repeat, 0.0 on BOTH media after it.
            #
            # A caller who wants a medium gap-filled from scratch should
            # gap-fill a fresh model; re-gap-filling an already-satisfied model
            # cannot add information and can only take it away.
            pre_value = _objective_value_on_media(
                mdlutl=mdlutl, media=media, objective=objective
            )
            if pre_value is not None and pre_value >= minimum_objective:
                records.append(
                    GapfillRecord(
                        request_index=request_index,
                        media_id=media_id,
                        media_digest=digest,
                        status="no_gaps",
                        additions=(),
                        reversals=(),
                        objective_value=pre_value,
                    )
                )
                growth_array.append(media_id + ":" + str(pre_value))
                continue

            try:
                solution = msgapfill.run_gapfilling(
                    media=media,
                    target=objective,
                    minimum_obj=minimum_objective,
                    binary_check=binary_check,
                    # Prefilter on the first occurrence only; see the parameter
                    # docstring for why repeating it would narrow the database
                    # for every later medium.
                    prefilter=prefilter and request_index == 0,
                    check_for_growth=True,
                )
            except Exception as exc:  # noqa: BLE001 - recorded per occurrence
                logger.warning(
                    "gap-fill occurrence %s (media %s) raised %s: %s",
                    request_index,
                    media_id,
                    type(exc).__name__,
                    exc,
                )
                records.append(
                    GapfillRecord(
                        request_index=request_index,
                        media_id=media_id,
                        media_digest=digest,
                        status=f"error:{type(exc).__name__}",
                        additions=(),
                        reversals=(),
                        objective_value=None,
                    )
                )
                continue

            if solution is not None and not _solution_changes_the_model(
                mdlutl=mdlutl, solution=solution
            ):
                # A solution that would add nothing. Integrating it is actively
                # destructive -- see _solution_changes_the_model -- so treat it
                # as what it is: this medium has no gaps left.
                solution = None

            if solution is None:
                # Either no solution exists, or the model already meets the
                # objective without additions. Distinguish the two by asking the
                # model, rather than guessing.
                already = _objective_value_on_media(
                    mdlutl=mdlutl, media=media, objective=objective
                )
                objective_value = already
                if already is not None and already >= minimum_objective:
                    status = "no_gaps"
            else:
                msgapfill.integrate_gapfill_solution(
                    solution=solution, cumulative_solution=cumulative
                )
                solutions[media] = solution
                status = "integrated"
                objective_value = _objective_value_on_media(
                    mdlutl=mdlutl, media=media, objective=objective
                )
                if objective_value is not None:
                    growth_array.append(media_id + ":" + str(objective_value))

            after_ids = {rxn.id for rxn in mdlutl.model.reactions}
            additions = tuple(sorted(after_ids - before_ids))
            reversals = tuple(
                sorted(
                    rxn_id
                    for rxn_id, _direction in cumulative
                    if rxn_id not in additions
                )
            )

            # Re-check the exclusion list against what gap-filling ACTUALLY
            # ADDED, comparing BASE ids. The blacklist is meant to keep these
            # out of the LP; this confirms it on the object that ships, because
            # an excluded reaction arriving by any other route is a correctness
            # failure and must not be discoverable only downstream. Comparing
            # additions rather than the whole model is what keeps a
            # pre-existing, annotation-supported reaction from being blamed on
            # gap-fill.
            violations = sorted(
                rxn_id
                for rxn_id in (after_ids - initial_ids)
                if _base_reaction_id(rxn_id) in excluded_base
            )
            if violations:
                raise RuntimeError(
                    f"gap-fill occurrence {request_index} (media {media_id!r}) "
                    f"ADDED excluded reactions {violations}; "
                    f"reaction_exclusion_list was {list(exclusion)} "
                    f"(normalised to base ids {sorted(excluded_base)})"
                )

            records.append(
                GapfillRecord(
                    request_index=request_index,
                    media_id=media_id,
                    media_digest=digest,
                    status=status,
                    additions=additions,
                    reversals=reversals,
                    objective_value=objective_value,
                )
            )

        # Pick a representative flux distribution: the first occurrence that
        # grew, in request order.
        output_solution = None
        output_solution_media = None
        for record, media in zip(records, media_objs):
            if record.objective_value and record.objective_value > 0:
                mdlutl.pkgmgr.getpkg("KBaseMediaPkg").build_package(media)
                mdlutl.pkgmgr.getpkg("ElementUptakePkg").build_package({"C": 60})
                output_solution = cobra.flux_analysis.pfba(mdlutl.model)
                output_solution_media = media
                break

        solution_rxn_types = ["new", "reversed"]
        if output_solution and output_solution_media in solutions:
            gfsolution = solutions[output_solution_media]
            for rxn_type in solution_rxn_types:
                for rxn_id in gfsolution[rxn_type]:
                    if gfsolution[rxn_type][rxn_id] == ">":
                        output_solution.fluxes[rxn_id] = 1000
                    else:
                        output_solution.fluxes[rxn_id] = -1000

        current_output["Growth"] = "<br>".join(growth_array)
        current_output["GS GF"] = len(mdlutl.gfutl.cumulative_gapfilling)
        current_output["Reactions"] = mdlutl.nonexchange_reaction_count()
        current_output["Model genes"] = len(mdlutl.model.genes)

        return (
            current_output,
            solutions,
            output_solution,
            output_solution_media,
            tuple(records),
        )

    @staticmethod
    def with_gapfill_results(
        result: ReconstructionResult,
        mdlutl: Any,
        gapfill_records: Sequence[GapfillRecord],
        reaction_exclusion_list: Sequence[str] = (),
    ) -> ReconstructionResult:
        """Return a copy of ``result`` carrying the POST-GAP-FILL facts.

        Args:
            result: The :class:`ReconstructionResult` from
                :meth:`build_metabolic_model` with ``collect_result=True``,
                carrying draft and post-ATP facts.
            mdlutl: The model util AFTER gap-fill, measured for the
                post-gap-fill reaction count.
            gapfill_records: The records :meth:`gapfill_metabolic_model`
                returned, in request order.
            reaction_exclusion_list: The exclusion list that was enforced.

        Returns:
            A new frozen :class:`ReconstructionResult`. The input is unchanged --
            the dataclass is frozen precisely so a result cannot be edited after
            the stage it describes has passed.
        """
        from dataclasses import replace

        return replace(
            result,
            post_gapfill_records=tuple(gapfill_records),
            post_gapfill_reaction_count=len(mdlutl.model.reactions),
            post_gapfill_reaction_exclusion_list=tuple(reaction_exclusion_list),
        )

    def run_comprehensive_gapfill_on_model(
        self,
        mdlutl: Any,
        templates: List[Any],
        media: Optional[Any] = None,
        core_template: Optional[Any] = None,
        source_models: Optional[List[Any]] = None,
        additional_tests: Optional[List[Dict]] = None,
        atp_safe: bool = True,
        reaction_exclusion_list: Optional[List[str]] = None,
        objective: str = "bio1",
        minimum_objective: float = 0.01,
        base_media: Optional[Any] = None,
        base_media_target_element: str = "C",
        reaction_scores: Optional[Dict] = {},
    ) -> tuple:
        """Run comprehensive gapfilling on an existing reconstructed model.

        Comprehensive gapfilling first maximizes the number of the model's own
        reactions that can simultaneously carry flux on complete media, then finds
        the minimum-cost set of database reactions that sustains those active
        reactions while still producing biomass. The result is a biomass-positive
        model with the largest feasible fraction of its native reactions unblocked.

        This is a thin wrapper around MSGapfill.run_multi_gapfill with
        gapfilling_mode='Comprehensive'. It mirrors the construction of
        gapfill_metabolic_model exactly (ATP tests, MSGapfill construction,
        return tuple shape).

        Args:
            mdlutl: MSModelUtil object to gapfill (mutated in place)
            templates: List of template objects providing gapfill candidates
            media: Media object to gapfill on. If None, KBaseMedia/Complete is
                loaded and used (comprehensive gapfilling on complete media).
            core_template: Core template object (for ATP tests). Loaded from
                self.templates["core"] if not provided.
            source_models: Source models for additional reaction candidates
            additional_tests: Additional constraint tests for gapfilling
            atp_safe: Whether to compute and apply ATP-safe test conditions
            reaction_exclusion_list: Reaction IDs to exclude from gapfilling
            objective: Objective reaction ID to optimize (default: "bio1")
            minimum_objective: Minimum objective flux for gapfilling feasibility
            base_media: Base media object for element-uptake filtering
            base_media_target_element: Target element for base media filtering
            reaction_scores: Reaction score dict passed to MSGapfill

        Returns:
            Tuple of (current_output, solutions, output_solution, output_solution_media)
            where current_output is a dict recording Growth / GS GF / Reactions /
            Model genes; solutions is the per-media solution dict from
            run_multi_gapfill; output_solution is a pFBA Solution on the first
            growing medium (or None); and output_solution_media is that medium
            (or None). The mdlutl model is mutated in place.
        """
        import cobra

        # Set defaults for mutable arguments
        if source_models is None:
            source_models = []
        if additional_tests is None:
            additional_tests = []
        if reaction_exclusion_list is None:
            reaction_exclusion_list = []

        # Load core template if not provided
        if core_template is None:
            core_template = self.get_template(self.templates["core"], None)

        # Resolve complete media: if not provided, load KBaseMedia/Complete via
        # the same media-resolution path the rest of this module uses.
        if media is None:
            complete_media = self.get_media("KBaseMedia/Complete", None)
        else:
            complete_media = media

        # Initialize output dict (same shape as gapfill_metabolic_model)
        current_output = {
            "Model": None,
            "Genome": None,
            "Genes": None,
            "Class": None,
            "Model genes": None,
            "Reactions": None,
            "Core GF": None,
            "GS GF": None,
            "Growth": None,
            "Comments": [],
        }

        # Compute ATP-safe test conditions (mirrors gapfill_metabolic_model)
        atp_tests = []
        if atp_safe:
            atp_tests = mdlutl.get_atp_tests(
                core_template=core_template,
                atp_media_filename=self.modelseedpy_data_dir + "/atp_medias.tsv",
                recompute=False,
            )

        all_tests = additional_tests + atp_tests

        # Construct MSGapfill (identical call signature to gapfill_metabolic_model)
        msgapfill = self.MSGapfill(
            model_or_mdlutl=mdlutl,
            default_gapfill_templates=templates,
            default_gapfill_models=source_models,
            test_conditions=all_tests,
            blacklist=reaction_exclusion_list,
            default_target=objective,
            minimum_obj=minimum_objective,
            base_media=base_media,
            base_media_target_element=base_media_target_element,
        )

        # Set reaction scores
        msgapfill.reaction_scores = reaction_scores

        # Run comprehensive gapfilling on complete media
        mdlutl.gfutl.cumulative_gapfilling = []
        growth_array = []
        media_list = [complete_media]
        solutions = msgapfill.run_multi_gapfill(
            media_list=media_list,
            target=objective,
            default_minimum_objective=minimum_objective,
            gapfilling_mode="Comprehensive",
            binary_check=False,
            prefilter=True,
            run_sensitivity_analysis=True,
            integrate_solutions=True,
            # Comprehensive gapfilling adds many reactions collectively necessary
            # for activation maximization. Individual-KO pruning would incorrectly
            # classify them as individually removable. Disable pruning so the
            # activated reaction set is fully integrated.
            remove_unneeded_reactions=False,
        )

        output_solution = None
        output_solution_media = None
        # Guard against None return (solver infeasible / no gapfill solution)
        if solutions is None:
            solutions = {}
        for med in media_list:
            if med in solutions and "growth" in solutions[med]:
                growth_array.append(med.id + ":" + str(solutions[med]["growth"]))
                if solutions[med]["growth"] > 0 and output_solution is None:
                    mdlutl.pkgmgr.getpkg("KBaseMediaPkg").build_package(med)
                    mdlutl.pkgmgr.getpkg("ElementUptakePkg").build_package({"C": 60})
                    output_solution = cobra.flux_analysis.pfba(mdlutl.model)
                    output_solution_media = med

        solution_rxn_types = ["new", "reversed"]
        if output_solution and output_solution_media in solutions:
            gfsolution = solutions[output_solution_media]
            for rxn_type in solution_rxn_types:
                for rxn_id in gfsolution[rxn_type]:
                    if gfsolution[rxn_type][rxn_id] == ">":
                        output_solution.fluxes[rxn_id] = 1000
                    else:
                        output_solution.fluxes[rxn_id] = -1000

        current_output["Growth"] = "<br>".join(growth_array)
        current_output["GS GF"] = len(mdlutl.gfutl.cumulative_gapfilling)
        current_output["Reactions"] = mdlutl.nonexchange_reaction_count()
        current_output["Model genes"] = len(mdlutl.model.genes)

        return current_output, solutions, output_solution, output_solution_media

    def kb_gapfill_metabolic_models(
        self,
        workspace: str,
        media_list: Optional[List[str]] = None,
        media_objs: Optional[List[Any]] = None,
        genome_objs: Optional[Dict] = None,
        expression_refs: Optional[List[str]] = None,
        expression_objs: Optional[Dict] = None,
        model_list: Optional[List[str]] = None,
        model_objectives: Optional[List[str]] = None,
        model_objs: Optional[List[Any]] = None,
        atp_safe: bool = True,
        suffix: str = ".gf",
        forced_atp_list: Optional[List[str]] = None,
        templates: Optional[List[Any]] = None,
        core_template_ref: Optional[str] = None,
        source_models: Optional[List[Any]] = None,
        limit_medias: Optional[List[str]] = None,
        limit_objectives: Optional[List[str]] = None,
        limit_thresholds: Optional[List[float]] = None,
        is_max_limits: Optional[List[bool]] = None,
        minimum_objective: float = 0.01,
        reaction_exclusion_list: Optional[List[str]] = None,
        default_objective: str = "bio1",
        kbmodel_hash: Optional[Dict] = None,
        output_data: Optional[Dict] = None,
        internal_call: bool = False,
        atp_medias: Optional[List[str]] = None,
        load_default_atp_medias: bool = True,
        max_atp_gapfilling: int = 0,
        gapfilling_delta: float = 0,
        return_model_objects: bool = False,
        return_data: bool = False,
        save_report_to_kbase: bool = True,
        change_to_complete: bool = False,
        gapfilling_mode: str = "Sequential",
        base_media: Optional[Any] = None,
        compound_list: Optional[List[str]] = None,
        base_media_target_element: str = "C",
        save_models_to_kbase: bool = True,
        save_gapfilling_fba_to_kbase: bool = True,
    ) -> Dict:
        """Gapfill metabolic models to enable growth on specified media.

        This is a KBase-specific wrapper that handles workspace references
        and KBase-specific saving operations, delegating core gapfilling
        to gapfill_metabolic_model.

        Args:
            workspace: KBase workspace ID or name for output
            media_list: List of media references for gapfilling
            media_objs: Pre-loaded media objects (alternative to media_list)
            genome_objs: Dictionary mapping model utilities to genome objects
            expression_refs: Expression data references
            expression_objs: Pre-loaded expression objects
            model_list: List of model references to gapfill
            model_objectives: Objectives for each model
            model_objs: Pre-loaded model utility objects
            atp_safe: Whether to apply ATP-safe constraints
            suffix: Suffix to add to gapfilled model IDs
            forced_atp_list: List of media IDs where ATP production should be forced
            templates: List of templates for gapfilling
            core_template_ref: Reference to core template
            source_models: Source models for reaction candidates
            limit_medias: Media for additional growth constraints
            limit_objectives: Objectives for limit constraints
            limit_thresholds: Threshold values for limits
            is_max_limits: Whether limits are maximum constraints
            minimum_objective: Minimum objective value for gapfilling
            reaction_exclusion_list: Reactions to exclude from gapfilling
            default_objective: Default objective function
            kbmodel_hash: Hash of KBase model data
            output_data: Pre-initialized output data (for internal calls)
            internal_call: Whether this is an internal call from build_metabolic_models
            atp_medias: List of ATP media references
            load_default_atp_medias: Whether to load default ATP medias
            max_atp_gapfilling: Maximum ATP gapfilling iterations
            gapfilling_delta: Delta threshold for gapfilling
            return_model_objects: Whether to return model objects in output
            return_data: Whether to return data table in output
            save_report_to_kbase: Whether to save HTML report to KBase
            change_to_complete: Whether to change default media to complete
            gapfilling_mode: Gapfilling mode ("Sequential" or "Cumulative")
            base_media: Base media object for gapfilling
            compound_list: List of compounds for media supplementation
            base_media_target_element: Target element for base media
            save_models_to_kbase: Whether to save models to KBase workspace
            save_gapfilling_fba_to_kbase: Whether to save gapfilling FBA to KBase

        Returns:
            Dictionary with report information and optionally model objects/data
        """
        if self.FBAModel is None:
            self._kbase_imports()
        # Set defaults for mutable arguments
        if model_objectives is None:
            model_objectives = []
        if model_objs is None:
            model_objs = []
        if forced_atp_list is None:
            forced_atp_list = []
        if source_models is None:
            source_models = []
        if limit_medias is None:
            limit_medias = []
        if limit_objectives is None:
            limit_objectives = []
        if limit_thresholds is None:
            limit_thresholds = []
        if is_max_limits is None:
            is_max_limits = []
        if reaction_exclusion_list is None:
            reaction_exclusion_list = []
        if kbmodel_hash is None:
            kbmodel_hash = {}
        if atp_medias is None:
            atp_medias = []

        default_media = "KBaseMedia/AuxoMedia"
        base_comments = []
        if change_to_complete:
            base_comments.append("Changing default to complete.")
            default_media = "KBaseMedia/Complete"

        result_table = pd.DataFrame({})

        # Retrieve models if not provided
        if not model_objs or len(model_objs) == 0:
            model_objs = []
            if model_list:
                for mdl_ref in model_list:
                    model_objs.append(self.get_model(mdl_ref))

        # Retrieve genomes if not provided
        if not genome_objs:
            genome_objs = {}
            for mdl in model_objs:
                genome_objs[mdl] = self.get_msgenome_from_ontology(
                    mdl.model.genome_ref,
                    native_python_api=self.native_ontology,
                    output_ws=workspace,
                )

        # Retrieve expression data if not provided
        if not expression_objs and expression_refs:
            expression_objs = self.get_expression_objs(expression_refs, genome_objs)

        # Process media
        if not media_objs:
            media_objs = self.process_media_list(media_list, default_media, workspace)

        # Process compound list
        if compound_list:
            if not base_media:
                base_comments.append("No base media provided. Ignoring compound list.")
            else:
                for cpd in compound_list:
                    newmedia = self.MSMedia.from_dict(media_dict={cpd: 100})
                    newmedia.merge(base_media)
                    media_objs.append(newmedia)

        # Compile additional tests from limit constraints
        additional_tests = []
        for i, limit_media in enumerate(limit_medias):
            additional_tests.append(
                {
                    "objective": limit_objectives[i],
                    "media": self.get_media(limit_media, None),
                    "is_max_threshold": is_max_limits[i],
                    "threshold": limit_thresholds[i],
                }
            )

        # Get core template (using local variable, not class state)
        loaded_core_template = None
        if core_template_ref:
            loaded_core_template = self.get_template(core_template_ref, None)
        else:
            loaded_core_template = self.get_template(self.templates["core"], None)

        # Iterate over each model and run gapfilling
        for i, mdlutl in enumerate(model_objs):
            # Use provided output data if available (for internal calls)
            if output_data and mdlutl in output_data:
                current_output = output_data[mdlutl]
            else:
                current_output = {
                    "Model": None,
                    "Genome": None,
                    "Genes": None,
                    "Class": None,
                    "Model genes": None,
                    "Reactions": None,
                    "Core GF": None,
                    "GS GF": None,
                    "Growth": None,
                    "Comments": base_comments.copy(),
                }

            # Add KBase-specific output formatting
            current_output["Model"] = (
                f'{mdlutl.wsid}{suffix}<br><a href="{mdlutl.wsid}{suffix}-recon.html" '
                f'target="_blank">(see reconstruction report)</a><br>'
                f'<a href="{mdlutl.wsid}{suffix}-full.html" target="_blank">(see full view)</a>'
            )

            # Set the objective
            if i < len(model_objectives):
                if not model_objectives[i]:
                    model_objectives[i] = default_objective
            else:
                model_objectives.append(default_objective)

            # Get templates for this model
            model_templates = templates
            if not model_templates:
                model_templates = [self.get_template(mdlutl.model.template_ref)]

            # Get expression object for this model
            expression_obj = None
            if expression_objs and mdlutl in expression_objs:
                expression_obj = expression_objs[mdlutl]

            reaction_scores = genome_objs[mdlutl].annoont.get_reaction_gene_hash(
                feature_type="gene"
            )

            # Call the generalized gapfill_metabolic_model function. The fifth
            # element is the per-occurrence GapfillRecord tuple; this KBase
            # wrapper reports through the row and the solutions dict, so it only
            # keeps the records for the comment line below.
            (
                gf_output,
                solutions,
                output_solution,
                output_solution_media,
                gapfill_records,
            ) = self.gapfill_metabolic_model(
                mdlutl=mdlutl,
                genome=genome_objs[mdlutl],
                media_objs=media_objs,
                templates=model_templates,
                core_template=loaded_core_template,
                source_models=source_models,
                additional_tests=additional_tests,
                expression_obj=expression_obj,
                atp_safe=atp_safe,
                reaction_exclusion_list=reaction_exclusion_list,
                objective=model_objectives[i],
                minimum_objective=minimum_objective,
                gapfilling_mode=gapfilling_mode,
                base_media=base_media,
                base_media_target_element=base_media_target_element,
                reaction_scores=reaction_scores
            )

            # Merge gapfill output into current_output
            current_output["Growth"] = gf_output["Growth"]
            current_output["GS GF"] = gf_output["GS GF"]
            current_output["Reactions"] = gf_output["Reactions"]
            current_output["Model genes"] = gf_output["Model genes"]
            failed_occurrences = [
                f"{record.request_index}:{record.media_id}({record.status})"
                for record in gapfill_records
                if record.status.startswith("error:")
            ]
            if failed_occurrences:
                current_output["Comments"].append(
                    "Gap-fill occurrences that raised: "
                    + ", ".join(failed_occurrences)
                )

            # Save model to KBase
            if save_models_to_kbase:
                self.save_model(mdlutl, workspace, None, suffix)

            # Save FBA solution to KBase
            if save_gapfilling_fba_to_kbase and output_solution:
                self.save_solution_as_fba(
                    output_solution,
                    mdlutl,
                    output_solution_media,
                    mdlutl.wsid + ".fba",
                    workspace=workspace,
                    fbamodel_ref=str(workspace) + "/" + mdlutl.wsid,
                )

            if not internal_call:
                result_table = pd.concat(
                    [result_table, pd.DataFrame([current_output])], ignore_index=True
                )

        output = {}
        if not internal_call:
            self._build_dataframe_report(result_table, model_objs)
            if save_report_to_kbase:
                output = self.save_report_to_kbase()
            if return_data:
                output["data"] = result_table.to_json()
            if return_model_objects:
                output["model_objs"] = model_objs

        return output

    def _build_dataframe_report(
        self, table: pd.DataFrame, model_objs: Optional[List[Any]] = None
    ) -> None:
        """Build HTML report from results dataframe.

        Args:
            table: Pandas DataFrame with reconstruction/gapfilling results
            model_objs: List of model utility objects for generating model reports
        """
        import jinja2

        context = {"initial_model": table.iloc[0]["Model"] if len(table) > 0 else ""}

        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(self.module_dir + "/data/"),
            autoescape=jinja2.select_autoescape(["html", "xml"]),
        )

        try:
            html = env.get_template("ReportTemplate.html").render(context)
        except jinja2.TemplateNotFound:
            # Fallback if template not found
            html = "<html><body><h1>Model Reconstruction Report</h1></body></html>"

        os.makedirs(self.working_dir + "/html", exist_ok=True)

        if model_objs:
            for model in model_objs:
                try:
                    msmodrep = self.MSModelReport(model_or_mdlutl=model)
                    msmodrep.build_report(
                        self.working_dir + "/html/" + model.wsid + "-recon.html"
                    )
                    msmodrep.build_multitab_report(
                        self.working_dir + "/html/" + model.wsid + "-full.html"
                    )
                except Exception as e:
                    self.log_warning(f"Could not generate report for {model.wsid}: {e}")

        print("Output dir:", self.working_dir + "/html/index.html")
        with open(self.working_dir + "/html/index.html", "w") as f:
            f.write(html)

        # Create data table file
        json_str = '{"data":' + table.to_json(orient="records") + "}"
        with open(self.working_dir + "/html/data.json", "w") as f:
            f.write(json_str)

    # Backward compatibility aliases
    build_metabolic_models = kb_build_metabolic_models
    gapfill_metabolic_models = kb_gapfill_metabolic_models


# ── Composition-based implementation ─────────────────────────────────────

class MSReconstructionUtilsImpl:
    """Composition-based reconstruction utilities.

    Holds ``env`` and ``model`` instead of inheriting from ``KBModelUtils``.
    Delegates all method calls to an internal legacy instance.
    """

    def __init__(self, env, model, **kwargs):
        self._env = env
        self._model = model
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
            self._delegate = MSReconstructionUtils(**_kwargs)
        except Exception:
            self._delegate = None

    @property
    def env(self):
        return self._env

    @property
    def model(self):
        return self._model

    def __getattr__(self, name):
        if self._delegate is None:
            raise RuntimeError("MSReconstructionUtilsImpl: delegate not initialized")
        return getattr(self._delegate, name)
