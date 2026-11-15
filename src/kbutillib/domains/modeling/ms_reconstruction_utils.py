"""ModelSEED reconstruction utilities for building and gapfilling metabolic models.

This module provides utilities for metabolic model reconstruction and gapfilling,
translating the functionality from KB-ModelSEEDReconstruction/modelseedrecon.py
into a KBUtilLib-compatible format with standard named arguments.
"""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

import pandas as pd

from ...core.errors import (
    MissingBiochemistryError,
    MissingSolverError,
    ModelSEEDpyCapabilityError,
    ReconstructionEnvironmentError,
)
from .kb_model_utils import KBModelUtils

__all__ = [
    "GPR_STRING_SUPPORT",
    "GENOME_CLASS_TO_TEMPLATE_KEY",
    "DEFAULT_TEMPLATE_KEY",
    "RECONSTRUCTION_DEPENDENCIES",
    "DEFAULT_SOLVER_PREFERENCE",
    "DEFAULT_ONTOLOGY_TERM",
    "TEMPLATE_KEY_TO_GENOME_CLASS_LABEL",
    "UNKNOWN_REVISION",
    "MissingBiochemistryError",
    "MissingSolverError",
    "ModelSEEDpyCapabilityError",
    "ReconstructionEnvironmentError",
    "MSReconstructionUtils",
    "MSReconstructionUtilsImpl",
    "ReconstructionResult",
    "initialize_reconstruction_environment",
    "resolve_distribution_provenance",
    "verify_role_alias_matching",
]

logger = logging.getLogger(__name__)

#: Capability marker read by downstream callers (notably the KBDL model-reconstruction
#: service) to decide whether a complex-bearing job can run against this install.
#: ``1`` = :meth:`MSReconstructionUtils._add_reactions_from_gene_mapping` accepts
#: verbatim GPR strings as ``reaction_gene_hash`` values, not just lists of gene ids.
GPR_STRING_SUPPORT = 1


# ── Environment initialisation for the KBase-free reconstruction layer ──────
#
# Everything in this section is the ENVIRONMENT half of the light reconstruction
# layer (PRD kbdl-recon-thin-layer-v1): it establishes, by measurement, that the
# process can actually reconstruct a model before any reconstruction is
# attempted.  Each check exists because its absence has already produced a
# confusing failure:
#
#   * dependency forwarding -- every SharedEnvUtils instance builds its own
#     DependencyManager with auto_init=False, so get_data_path() returns None and
#     KBAnnotationUtils falls through to a sibling-directory path that does not
#     exist.  GAA papered over this by monkeypatching SharedEnvUtils.__init__;
#     here the paths are registered with the dependency manager instead.
#   * solver selection -- KBDL issue #35: poplar's Gurobi licence is a token
#     server that REJECTS gurobipy >= 14 ("license not valid for Gurobi version
#     14"), and with gurobipy 14 installed even a GLPK solve fails, because
#     cobra's default interface is optlang.gurobi_interface and the backend is
#     bound when the Model is constructed.  So the backend is chosen by a real
#     solve and cobra's default is set before anything builds a Model.
#   * alias matching -- the fork and upstream both report modelseedpy 0.4.2
#     while their MSBuilder.build() differs in arity and return type, so neither
#     a version nor a signature identifies the code.  The capability is
#     therefore established by building a two-role template and checking that a
#     gene actually arrives through an ALIAS.

#: Dependencies the reconstruction layer resolves through
#: ``~/.kbutillib/dependencies.yaml``.  Deliberately NOT including
#: ``modelseedpy``: ``DependencyManager.initialize_dependencies`` puts every
#: configured path on ``sys.path``, and that entry points at the FORK checkout,
#: which would shadow the installed upstream package.
RECONSTRUCTION_DEPENDENCIES = ("ModelSEEDDatabase", "cb_annotation_ontology_api")

#: LP backends to probe, in order.  GLPK first: it needs no licence and carried
#: ATP correction to completion in 50.9 s against Gurobi's 59.3 s
#: (agent-io/work-records/task-d1302dc4.md section (b)).
DEFAULT_SOLVER_PREFERENCE = ("glpk", "gurobi")

#: Genome class -> template key.  The same map ``build_metabolic_model`` applies
#: inline (see the ``template_type == "auto"`` branch), lifted out so the layer
#: and the legacy path cannot disagree.  ``"--"`` is the classifier's own
#: no-call marker.
GENOME_CLASS_TO_TEMPLATE_KEY = {
    "P": "gp",
    "N": "gn",
    "A": "ar",
    "--": "gn",
}

#: Human-readable genome-class label per template key.  ``build_metabolic_model``
#: reports one of these in ``current_output["Class"]``, which every legacy report
#: consumer reads, while :meth:`MSReconstructionUtils.classify_genome` returns the
#: key itself.
TEMPLATE_KEY_TO_GENOME_CLASS_LABEL = {
    "gp": "Gram Positive",
    "gn": "Gram Negative",
    "ar": "Archaea",
}

#: Ontology key the layer reads genome functions from by default.  KBDL folds the
#: union of every annotation source's function strings under ONE key, which is
#: what makes this a single value rather than a priority list.
DEFAULT_ONTOLOGY_TERM = "RAST"

#: Value reported for a provenance revision the installed distribution does not
#: record.  Never replaced by a guess: see
#: :func:`resolve_distribution_provenance`.
UNKNOWN_REVISION = "unknown"

#: Template key used for any class with no dedicated template.  ``KBModelUtils``
#: ships templates for core/gp/gn/ar only -- there is no cyanobacterial entry in
#: ``self.templates`` -- so class ``C`` resolves here rather than to a key that
#: would raise ``KeyError`` on lookup.
DEFAULT_TEMPLATE_KEY = "gn"

#: Role/function strings used by the behavioural alias check.  Chosen so that no
#: real genome or template can contain them.
_ALIAS_PROBE_FUNCTION = "KBUtilLib alias-only probe function 90001"
_ALIAS_PROBE_ROLE_NAME = "KBUtilLib probe role whose name no genome carries 90001"
_ALIAS_PROBE_NAMED_FUNCTION = "KBUtilLib name-matched probe function 90002"


def _probe_solver(backend: str) -> bool:
    """Return True only if ``backend`` completes a real LP solve.

    A two-reaction model with a known optimum, so the answer is checked and not
    merely the absence of an exception.  ``m.solver`` is assigned before
    ``optimize()``; the Model itself is constructed after
    ``cobra.Configuration().solver`` has been pointed at the candidate, because
    an installed-but-unlicensed Gurobi otherwise fails a GLPK solve too.

    Args:
        backend: optlang/cobra backend name, e.g. ``"glpk"`` or ``"gurobi"``.

    Returns:
        True if the solve returned status ``optimal`` with the expected
        objective value.
    """
    try:
        import cobra

        config = cobra.Configuration()
        previous = config.solver
        try:
            config.solver = backend
            model = cobra.Model("kbu_solver_probe")
            met = cobra.Metabolite("probe_c")
            src = cobra.Reaction("PROBE_SRC", lower_bound=0, upper_bound=10)
            src.add_metabolites({met: 1})
            snk = cobra.Reaction("PROBE_SNK", lower_bound=0, upper_bound=1000)
            snk.add_metabolites({met: -1})
            model.add_reactions([src, snk])
            model.objective = "PROBE_SNK"
            model.solver = backend
            solution = model.optimize()
        finally:
            try:
                config.solver = previous
            except Exception:  # noqa: BLE001 - restoring a broken default is not fatal
                pass
        return solution.status == "optimal" and abs(solution.objective_value - 10.0) < 1e-6
    except Exception:  # noqa: BLE001 - any failure means "this backend does not work here"
        return False


def _set_cobra_default_solver(backend: str) -> None:
    """Point cobra's default interface at ``backend``.

    Separate from :func:`_select_lp_solver` because it is the one step that
    mutates process-wide state, and because the order matters: cobra binds the
    solver interface when a ``Model`` is CONSTRUCTED, so this must happen
    before any model is built, not after.

    Args:
        backend: A backend already confirmed by :func:`_probe_solver`.
    """
    import cobra

    cobra.Configuration().solver = backend


def _select_lp_solver(
    solver: Optional[str] = None,
    preference: Sequence[str] = DEFAULT_SOLVER_PREFERENCE,
) -> Dict[str, Any]:
    """Choose an LP backend by probing, and make it cobra's default.

    Args:
        solver: Demand this backend specifically.  It is still probed; a named
            backend that does not solve is an error, never a silent fallback.
        preference: Backends to try in order when ``solver`` is None.

    Returns:
        ``{"solver": <name>, "probes": {<name>: <bool>, ...}}``.

    Raises:
        MissingSolverError: No candidate completed a real solve.
    """
    candidates = [solver] if solver else list(preference)
    probes: Dict[str, bool] = {}
    chosen: Optional[str] = None
    for backend in candidates:
        probes[backend] = _probe_solver(backend)
        if probes[backend] and chosen is None:
            chosen = backend
            break

    if chosen is None:
        raise MissingSolverError(
            "no LP backend completed a real solve: "
            + ", ".join(f"{k}={v}" for k, v in probes.items())
            + ". On poplar the Gurobi licence server rejects gurobipy >= 14 "
            "(KBDL issue #35); GLPK needs swiglpk and no licence."
        )

    _set_cobra_default_solver(chosen)
    return {"solver": chosen, "probes": probes}


def _build_alias_probe_template() -> Any:
    """Build the smallest MSTemplate that can demonstrate alias matching.

    Two roles, two single-role complexes, two reactions:

    * ``probe.role.alias`` -- its NAME appears in no genome; the probe function
      is only in its ``aliases``.  Reached only when alias matching is on.
    * ``probe.role.named`` -- its name IS a genome function, so it is matched
      either way and acts as the positive control that the probe template is
      itself wired correctly.

    Returns:
        An in-memory ``MSTemplate``.  No biochemistry database, no network and
        no solver are involved.
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

    template = MSTemplate(
        "kbu.alias.probe",
        name="KBUtilLibAliasProbeTemplate",
        domain="Bacteria",
        template_type="GenomeScale",
    )
    template.add_compartments(
        [
            MSTemplateCompartment("c", "Cytosol", 7.0, 0),
            MSTemplateCompartment("e", "Extracellular", 7.0, 1),
        ]
    )
    template.add_compounds(
        [
            MSTemplateMetabolite("cpd90001", "C", "probe compound A"),
            MSTemplateMetabolite("cpd90002", "C", "probe compound B"),
        ]
    )
    template.add_comp_compounds(
        [
            MSTemplateSpecies("cpd90001_c", 0, "c", "cpd90001"),
            MSTemplateSpecies("cpd90002_c", 0, "c", "cpd90002"),
        ]
    )

    alias_role = NewModelTemplateRole(
        "probe.role.alias",
        _ALIAS_PROBE_ROLE_NAME,
        aliases=[_ALIAS_PROBE_FUNCTION],
    )
    named_role = NewModelTemplateRole("probe.role.named", _ALIAS_PROBE_NAMED_FUNCTION)
    template.add_roles([alias_role, named_role])

    alias_complex = NewModelTemplateComplex("probe.cpx.alias", "alias probe complex")
    alias_complex.add_role(alias_role, triggering=True, optional=False)
    named_complex = NewModelTemplateComplex("probe.cpx.named", "named probe complex")
    named_complex.add_role(named_role, triggering=True, optional=False)
    template.add_complexes([alias_complex, named_complex])

    alias_reaction = MSTemplateReaction(
        "rxn90001_c", "rxn90001", "alias probe reaction", "probe", 0.0, 1000.0
    )
    alias_reaction.add_metabolites(
        {
            template.compcompounds.get_by_id("cpd90001_c"): -1,
            template.compcompounds.get_by_id("cpd90002_c"): 1,
        }
    )
    alias_reaction.add_complexes([alias_complex])
    named_reaction = MSTemplateReaction(
        "rxn90002_c", "rxn90002", "named probe reaction", "probe", 0.0, 1000.0
    )
    named_reaction.add_metabolites(
        {
            template.compcompounds.get_by_id("cpd90002_c"): -1,
            template.compcompounds.get_by_id("cpd90001_c"): 1,
        }
    )
    named_reaction.add_complexes([named_complex])
    template.add_reactions([alias_reaction, named_reaction])
    return template


def _build_alias_probe_genome() -> Any:
    """Build the two-feature MSGenome the alias probe template is matched against."""
    from modelseedpy import MSGenome
    from modelseedpy.core.msgenome import MSFeature

    genome = MSGenome()
    alias_feature = MSFeature("probe_gene_alias", "", description=_ALIAS_PROBE_FUNCTION)
    alias_feature.add_ontology_term("RAST", _ALIAS_PROBE_FUNCTION)
    named_feature = MSFeature(
        "probe_gene_named", "", description=_ALIAS_PROBE_NAMED_FUNCTION
    )
    named_feature.add_ontology_term("RAST", _ALIAS_PROBE_NAMED_FUNCTION)
    genome.add_features([alias_feature, named_feature])
    return genome


def _genes_from_alias_probe(match_role_aliases: bool) -> set:
    """Run the probe build once and return the gene ids that reached the model."""
    from modelseedpy import MSBuilder

    builder = MSBuilder(
        genome=_build_alias_probe_genome(),
        template=_build_alias_probe_template(),
    )
    model = builder.build_base_model(
        model_or_id="kbu_alias_probe_model",
        index="0",
        annotate_with_rast=False,
        match_role_aliases=match_role_aliases,
    )
    return {gene.id for gene in model.genes}


def verify_role_alias_matching() -> Dict[str, Any]:
    """Check BEHAVIOURALLY that MSBuilder can match a gene through a role alias.

    ``MSBuilder.build_base_model`` is called twice on the probe template, with
    ``match_role_aliases`` off and on, and the gene ids that reach the model are
    compared.  The capability is confirmed only if the alias-only gene is absent
    with the flag off and present with it on -- a signature check would pass
    against a tree where the keyword exists but is not forwarded to
    ``generate_reaction_complex_sets``, which is exactly the defect the upstream
    change fixed.

    ``annotate_with_rast=False`` is mandatory here: it defaults to True and
    would make a live RAST network call.

    Returns:
        ``{"matched": True, "genes_with_aliases": [...],
        "genes_without_aliases": [...]}``.

    Raises:
        ModelSEEDpyCapabilityError: ``build_base_model`` rejects the keyword, or
            accepts it without matching the alias.
    """
    try:
        without_aliases = _genes_from_alias_probe(match_role_aliases=False)
        with_aliases = _genes_from_alias_probe(match_role_aliases=True)
    except TypeError as exc:
        raise ModelSEEDpyCapabilityError(
            "installed modelseedpy MSBuilder.build_base_model does not accept "
            f"match_role_aliases: {exc}. The layer needs upstream ModelSEEDpy "
            "with the alias-matching change (cshenry/ModelSEEDpy kbdl-target "
            "e1155a41fa53b921185b20c6cfbd083e5e3739a4)."
        ) from exc

    report = {
        "genes_without_aliases": sorted(without_aliases),
        "genes_with_aliases": sorted(with_aliases),
    }
    if "probe_gene_named" not in without_aliases:
        raise ModelSEEDpyCapabilityError(
            "alias probe is not usable against this modelseedpy: the "
            "NAME-matched control gene did not reach the model either "
            f"({report}), so role matching itself is not working."
        )
    if "probe_gene_alias" in without_aliases:
        raise ModelSEEDpyCapabilityError(
            "alias probe cannot discriminate: the alias-only gene was matched "
            f"with match_role_aliases=False ({report})."
        )
    if "probe_gene_alias" not in with_aliases:
        raise ModelSEEDpyCapabilityError(
            "installed modelseedpy accepts match_role_aliases=True but does "
            f"not match a gene through a role alias ({report}). The keyword is "
            "probably not forwarded to generate_reaction_complex_sets."
        )
    report["matched"] = True
    return report


def _load_upstream_genome_classifier(classifier_id: Optional[str] = None) -> Any:
    """Load the genome classifier shipped with the installed modelseedpy.

    Prefers whatever ``.npz`` is present in ``modelseedpy/data/ml``: upstream
    ships ``linear_svc_ACNP_RAST_full_11_11_2026.npz``, loaded by
    ``load_classifier_from_npz`` with ``allow_pickle=False``, so the old
    ``scikit-learn == 1.2.0`` pin that the legacy ``*.pickle`` classifiers
    carried does not apply.

    Args:
        classifier_id: Explicit classifier id (file stem).  When None the
            shipped ``.npz`` is discovered.

    Returns:
        An ``MSGenomeClassifier``.

    Raises:
        ModelSEEDpyCapabilityError: No classifier artifact is available.
    """
    import modelseedpy
    from modelseedpy.helpers import get_classifier

    if classifier_id is None:
        ml_dir = Path(modelseedpy.__file__).parent / "data" / "ml"
        candidates = sorted(p.stem for p in ml_dir.glob("*.npz")) if ml_dir.is_dir() else []
        if not candidates:
            raise ModelSEEDpyCapabilityError(
                f"no genome classifier .npz shipped in {ml_dir}; "
                "classify_genome needs one (pass classifier_id= to use a "
                "downloadable legacy pickle instead)."
            )
        classifier_id = candidates[0]

    return get_classifier(classifier_id)


def _resolve_reconstruction_dependencies(
    dependency_config: Optional[Union[str, Path]] = None,
    dependencies: Iterable[str] = RECONSTRUCTION_DEPENDENCIES,
) -> Dict[str, Path]:
    """Resolve the layer's dependencies and forward them to the dependency manager.

    Only the requested names are initialised, so no other configured checkout
    (notably the forked ``modelseedpy``) reaches ``sys.path``.  The resolved
    paths are registered process-wide, which is what makes
    ``self.get_data_path("ModelSEEDDatabase")`` non-None inside instances
    constructed afterwards.

    Args:
        dependency_config: Explicit ``dependencies.yaml``.  None uses
            ``~/.kbutillib/dependencies.yaml`` and then the repo default.
        dependencies: Dependency names to resolve.

    Returns:
        Mapping of dependency name to resolved directory.

    Raises:
        MissingBiochemistryError: ``ModelSEEDDatabase`` does not resolve.
        ReconstructionEnvironmentError: Some other requested dependency does
            not resolve.
    """
    from ...core.dependency_manager import (
        DependencyManager,
        register_process_dependencies,
    )

    wanted = list(dependencies)
    manager = DependencyManager(
        config_path=Path(dependency_config) if dependency_config else None,
        auto_init=False,
    )
    declared = dict(manager.config)
    # Restrict before initialising: initialize_dependencies() sys.path-inserts
    # every entry it sees.
    manager.config = {name: declared[name] for name in wanted if name in declared}
    manager.initialize_dependencies()

    resolved: Dict[str, Path] = {}
    unresolved: List[str] = []
    for name in wanted:
        path = manager.get_data_path(name)
        if path is None:
            unresolved.append(name)
        else:
            resolved[name] = path

    if "ModelSEEDDatabase" in unresolved:
        raise MissingBiochemistryError(
            "dependency manager could not resolve 'ModelSEEDDatabase' from "
            f"{manager.config_path} (declares: {sorted(declared)}). The layer "
            "never falls back to modelseedpy's config.cfg default, which does "
            "not exist on poplar in either tree (KBDL issue #41)."
        )
    if unresolved:
        raise ReconstructionEnvironmentError(
            f"dependency manager could not resolve {unresolved} from "
            f"{manager.config_path} (declares: {sorted(declared)})."
        )

    register_process_dependencies(resolved)
    return resolved


def initialize_reconstruction_environment(
    *,
    dependency_config: Optional[Union[str, Path]] = None,
    dependencies: Iterable[str] = RECONSTRUCTION_DEPENDENCIES,
    solver: Optional[str] = None,
    solver_preference: Sequence[str] = DEFAULT_SOLVER_PREFERENCE,
    check_alias_matching: bool = True,
) -> Dict[str, Any]:
    """Prepare and verify the environment the reconstruction layer needs.

    Call this ONCE per process, BEFORE constructing ``MSReconstructionUtils``:
    the dependency forwarding it performs is what lets the constructor resolve
    ``ModelSEEDDatabase`` and ``cb_annotation_ontology_api``, and the solver
    choice must be cobra's default before any ``cobra.Model`` is built.

    Needs no KBase connection -- a KBase CTS job can run this with KBUtilLib and
    ModelSEEDpy alone.

    Args:
        dependency_config: Explicit ``dependencies.yaml``; None uses
            ``~/.kbutillib/dependencies.yaml``.
        dependencies: Dependency names to resolve and forward.
        solver: Demand a specific LP backend.  It is still probed by a real
            solve.  Note that Gurobi is usable on poplar only with
            ``gurobipy < 14``; the licence server rejects 14.
        solver_preference: Backends to probe in order when ``solver`` is None.
        check_alias_matching: Run the behavioural alias check.  Leave it on for
            anything that will build a model from a KBDL template.

    Returns:
        A report: ``dependency_config``, ``dependencies`` (name -> path string),
        ``solver``, ``solver_probes``, ``alias_matching``, and
        ``modelseedpy_file`` (which install was verified -- a version string
        does not identify this code).

    Raises:
        MissingBiochemistryError: ``ModelSEEDDatabase`` does not resolve.
        ReconstructionEnvironmentError: Another dependency does not resolve.
        MissingSolverError: No LP backend completed a real solve.
        ModelSEEDpyCapabilityError: Alias matching does not work.
    """
    resolved = _resolve_reconstruction_dependencies(
        dependency_config=dependency_config, dependencies=dependencies
    )
    solver_report = _select_lp_solver(solver=solver, preference=solver_preference)

    alias_report: Optional[Dict[str, Any]] = None
    if check_alias_matching:
        alias_report = verify_role_alias_matching()

    import modelseedpy

    report = {
        "dependency_config": str(dependency_config) if dependency_config else None,
        "dependencies": {name: str(path) for name, path in resolved.items()},
        "solver": solver_report["solver"],
        "solver_probes": solver_report["probes"],
        "alias_matching": alias_report,
        "modelseedpy_file": modelseedpy.__file__,
    }
    logger.info(
        "reconstruction environment ready: solver=%s dependencies=%s",
        report["solver"],
        sorted(resolved),
    )
    return report


def _draft_model_facts(*, model: Any) -> Dict[str, Any]:
    """Measure the DRAFT model at the stage boundary, before anything ATP-specific.

    Args:
        model: The fully assembled, uncorrected ``cobra.Model``.

    Returns:
        A dict with one key per ``draft_*`` field of :class:`ReconstructionResult`.

    Every value is read off the model directly rather than carried forward from
    a builder's bookkeeping, because the builder's counts are of what it OFFERED
    and the model is what it kept.
    """
    reaction_ids = [rxn.id for rxn in model.reactions]
    return {
        "reactions": len(reaction_ids),
        "metabolites": len(model.metabolites),
        "genes": len(model.genes),
        "exchanges": len(model.exchanges),
        "drains": sum(
            1 for rid in reaction_ids if rid.startswith(("DM_", "SK_"))
        ),
        "biomass_ids": tuple(sorted(r for r in reaction_ids if r.startswith("bio"))),
        "objective": str(model.objective.expression),
        "atpm_present": "ATPM_c0" in reaction_ids,
    }


# ── Provenance and the structured reconstruction result ────────────────────


def resolve_distribution_provenance(distribution_name: str) -> "tuple[str, str]":
    """Read a distribution's declared version and resolved VCS revision.

    Args:
        distribution_name: Installed distribution name, e.g. ``"modelseedpy"``.

    Returns:
        ``(version, revision)``.  ``version`` is the distribution's declared
        version, or :data:`UNKNOWN_REVISION` when the distribution is not
        installed.  ``revision`` is the commit recorded in the install's
        ``direct_url.json`` (``vcs_info.commit_id``, which pip writes for a
        ``git+...`` install), or :data:`UNKNOWN_REVISION`.

    A revision is reported ONLY when the installed distribution records one.
    A PyPI wheel records none; an editable install records
    ``dir_info.editable`` and no commit.  Both therefore report ``"unknown"``.
    Nothing is inferred from a git checkout that happens to sit nearby -- the
    working tree a source directory lives in is not evidence about the code
    that was imported, and the version string is not evidence at all: the fork
    and the pinned upstream differ in ``MSBuilder.build``'s arity and return
    type while reporting 0.4.2 and 0.4.3 respectively.  Reporting ``"unknown"``
    where the install is silent is what makes the populated cases worth
    trusting.
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


@dataclass(frozen=True)
class ReconstructionResult:
    """Structured facts about one reconstruction, STAGE BY STAGE.

    Every field is enumerated explicitly and every per-stage field names its
    stage in its own name, because "957 reactions" means three different things
    at the three stage boundaries and a report that does not say which is
    unreadable.  The boundaries are fixed by PRD ``kbdl-recon-thin-layer-v1``:

    * **DRAFT** -- the fully assembled, UNCORRECTED model: annotation-supported
      reactions, spontaneous/universal reactions, biomass and its auxiliary
      reactions, exchanges/drains and objective selection are all in place, but
      nothing ATP-specific has been inserted and no ATP solve has run.
    * **POST-ATP** -- after every ATP-correction mutation (the ATP hydrolysis
      scaffold, core gap-filling, the noncore filter and the re-expansion to
      genome scale), before any requested-medium gap-fill.
    * **POST-GAP-FILL** -- after sequential integration for every requested
      medium, before FVA or reporting.

    Nothing in this object is filled by a ``getattr`` default: a field is
    populated from something that was measured at its stage, or the
    reconstruction raises.  A silently-defaulted provenance field is worse than
    a missing one, because it reads as a measurement.
    """

    # ── DRAFT ─────────────────────────────────────────────────────────────
    draft_reaction_count: int
    """Reactions on the DRAFT model (every reaction, exchanges included)."""

    draft_metabolite_count: int
    """Metabolites on the DRAFT model."""

    draft_gene_count: int
    """Genes on the DRAFT model -- nonzero is what makes a build role-based."""

    draft_exchange_count: int
    """``cobra.Model.exchanges`` on the DRAFT model, i.e. the uptake capacity
    ATP correction will be handed (KBDLJobRunningPrototype#28, cause 1)."""

    draft_drain_count: int
    """``DM_``/``SK_`` reactions on the DRAFT model."""

    draft_biomass_ids: Tuple[str, ...]
    """Biomass reaction ids on the DRAFT model, sorted."""

    draft_objective_expression: str
    """``str(model.objective.expression)`` at the DRAFT boundary -- objective
    selection is part of the draft, not of correction."""

    draft_atpm_present: bool
    """Whether ``ATPM_c0`` is on the DRAFT model.  Expected ``False``:
    ``build_base_model`` does not add it and this layer never calls
    ``MSBuilder.build()``, which is where upstream adds it."""

    # ── POST-ATP ──────────────────────────────────────────────────────────
    post_atp_correction_ran: bool
    """POST-ATP: whether ATP correction was performed at all (``atp_safe``).
    False means the DRAFT facts are the final facts."""

    post_atp_correction_invocations: int
    """POST-ATP: how many times ``run_atp_correction`` was called.  Exactly ``1`` when it
    ran: the call mutates the model (disables noncore reactions, gap-fills the
    core, re-expands to genome scale while filtering ATP-breaking reactions),
    so a second call re-filters an already-filtered model."""

    post_atp_reaction_count: Optional[int]
    """POST-ATP: reactions after every ATP-correction mutation; ``None`` when
    correction did not run, so the DRAFT count is the live one."""

    post_atp_removed_reaction_ids: Tuple[str, ...]
    """Reaction ids present at DRAFT and absent POST-ATP, sorted.  Computed as
    a set difference over ids rather than read out of
    ``MSATPCorrection.filtered_noncore``: the filter zeroes bounds first and
    removes a reaction only when both bounds end up zero, so the internal list
    and the set actually removed are different things."""

    post_atp_core_gapfilling_count: Optional[int]
    """POST-ATP: ``len(MSATPCorrection.cumulative_core_gapfilling)``, i.e. how
    many reactions core gap-filling added during correction."""

    post_atp_selected_media_ids: Tuple[str, ...]
    """POST-ATP: ids of the ATP media ATP correction accepted, in its own order.  Empty
    with ``post_atp_correction_ran`` true means every medium failed."""

    post_atp_test_count: Optional[int]
    """Number of ATP tests ``run_atp_correction`` returned POST-ATP.  These are
    published on the model util so downstream gap-fill can be ATP-safe without
    recomputing them (#28, cause 3)."""

    post_atp_analysis: Mapping[str, Any]
    """The POST-ATP analysis, JSON-serialisable.  ``media_gapfill_stats`` is
    re-keyed by media ID: ``MSATPCorrection`` keys it by the ``MSMedia``
    OBJECT, which makes ``json.dumps`` raise."""

    # ── POST-GAP-FILL ─────────────────────────────────────────────────────
    post_gapfill_results: Tuple[Any, ...]
    """Per-medium gap-fill outcomes POST-GAP-FILL, in the order the caller
    requested the media.  ``build_metabolic_model`` always leaves this an empty
    tuple -- it stops at POST-ATP by design, and per-medium gap-fill with
    exclusions is :meth:`MSReconstructionUtils.gapfill_metabolic_model`'s
    stage, which populates it."""

    # ── Object identities ─────────────────────────────────────────────────
    genome_id: str
    """``MSGenome.id`` of the genome built from. Fixed at the DRAFT stage and
    unchanged by every later stage."""

    genome_class: Optional[str]
    """DRAFT stage: genome class recorded for the build, or ``None`` when the
    caller supplied the template and no classification was performed."""

    template_id: str
    """``id`` of the genome-scale ``MSTemplate`` the DRAFT was built from.
    Also the template POST-GAP-FILL integration defaults to."""

    template_name: str
    """``name`` of the DRAFT-stage genome-scale template."""

    core_template_id: Optional[str]
    """POST-ATP: ``id`` of the core ``MSTemplate`` ATP correction used, or
    ``None`` when correction did not run."""

    core_template_name: Optional[str]
    """POST-ATP: ``name`` of that core template."""

    # ── Build settings actually used ──────────────────────────────────────
    ontology_term: str
    """Ontology key ``MSBuilder`` read functions from (DRAFT stage).  KBDL folds
    every annotation source under one key, which is what makes this a single
    value."""

    match_role_aliases: bool
    """Whether ``build_base_model`` was asked to match genome functions against
    template role ALIASES as well as names (DRAFT stage)."""

    allow_incomplete_complexes: bool
    """Whether incomplete template complexes were admitted (DRAFT stage)."""

    reactions_to_add_supplied: bool
    """Whether the caller injected a reaction-to-gene mapping (DRAFT stage).
    False is the native role-based path; True is the legacy roleless path, and
    it is the condition under which the two roleless-path compensations run."""

    # ── Software provenance ───────────────────────────────────────────────
    modelseedpy_version: str
    """``modelseedpy``'s declared version, read once and true of every stage.
    Identifies the distribution, NOT the code: fork and upstream differ in
    behaviour at 0.4.2/0.4.3."""

    modelseedpy_revision: str
    """``modelseedpy``'s installed commit from ``direct_url.json``, or
    ``"unknown"``.  Never synthesised.  True of every stage: the same install
    performed the DRAFT, POST-ATP and POST-GAP-FILL work."""

    kbutillib_version: str
    """``KBUtilLib``'s declared version, true of every stage."""

    kbutillib_revision: str
    """``KBUtilLib``'s installed commit from ``direct_url.json``, or
    ``"unknown"`` -- which is what an editable install reports.  True of every
    stage."""

    def to_dict(self) -> Dict[str, Any]:
        """Return the result as a plain dict, one key per field.

        Returns:
            A dict suitable for ``json.dumps``: tuples become lists and
            ``post_atp_analysis`` is already string-keyed.
        """
        from dataclasses import asdict

        return asdict(self)


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

    def _apply_roleless_path_compensations(self, *, mdlutl: Any, builder: Any) -> None:
        """Re-run the two template sweeps the ROLELESS injection path needs.

        Args:
            mdlutl: The ``MSModelUtil`` wrapping the model under construction.
            builder: The initialised ``MSBuilder``, whose
                ``template_species_to_model_species`` map the first sweep's
                inclusion predicate reads.

        Called ONLY when ``reactions_to_add`` was supplied.  On a role-based
        draft both halves are strict no-ops (measured; see the call site), and
        both are kept verbatim for the legacy path because on that path they are
        the difference between a model that can respire and one that cannot.
        """
        #
        # ``MSBuilder.build_non_metabolite_reactions`` admits a spontaneous or
        # universal template reaction only when at least one of its metabolites
        # is already a key of ``builder.template_species_to_model_species``
        # (unless ``allow_all_non_grp_reactions=True``). ``build_base_model`` calls
        # it once, but on the ROLELESS gene-mapping path it adds no
        # metabolic reactions at all, so that map is still empty and almost
        # nothing qualifies: models were getting 3-12 of the 42
        # spontaneous/universal reactions in GramNeg V6. The ones lost include
        # membrane diffusion of O2 (``rxn05468``), CO2 (``rxn05467``) and water
        # (``rxn05319``), so the models could not respire -- they "grew" through
        # workarounds such as running cytochrome oxidase ``rxn14415`` backwards
        # to make O2 out of water. Measured effect of restoring just those three
        # diffusion reactions: max ATP per glucose went 1.0 -> 31.5 (Phaeo),
        # 0.5 -> 31.5 (Smeli), 10.1 -> 26.5 (Keio, vs 23.5 for iML1515)
        # (KBDLJobRunningPrototype#29).
        #
        # AFTER ``_add_reactions_from_gene_mapping``, because that is what
        # populates ``template_species_to_model_species`` (it writes every
        # metabolite it introduces into the map), so the inclusion predicate can
        # now admit reactions the first call could not.
        #
        # BEFORE ``add_exchanges_to_model``, so an extracellular species brought
        # in by a newly added diffusion reaction gets its exchange from the
        # single existing ``add_exchanges_to_model`` call below instead of
        # becoming a flux dead-end.
        #
        # ``allow_all_non_grp_reactions`` stays False DELIBERATELY. Forcing all
        # 42 in would guarantee the count but would add reactions whose
        # metabolites are absent, creating dead-end species and destroying the
        # coverage number as a diagnostic signal.
        #
        # Note this method RETURNS the reactions rather than adding them -- the
        # caller is responsible for ``add_reactions``, exactly as
        # ``build_base_model`` does internally. It already skips any reaction id present in the model
        # passed to it, so this cannot duplicate what the first call added.
        non_metabolic_reactions = builder.build_non_metabolite_reactions(
            cobra_model=mdlutl.model, allow_all_non_grp_reactions=False
        )
        if non_metabolic_reactions:
            mdlutl.model.add_reactions(non_metabolic_reactions)

        # Add exchange reactions for every extracellular metabolite, BEFORE ATP
        # correction. Load-bearing for ordering, not a convenience:
        # ``MSBuilder.build_base_model`` already calls ``add_exchanges_to_model``
        # once, but it does so from inside itself, before the caller has had a
        # chance to inject ``reactions_to_add``. On the RAST-role path that is
        # harmless (``build_metabolic_reactions`` runs earlier inside
        # ``build_base_model``, so the extracellular species are present). On a
        # ROLELESS build -- the path KBDL/GAA ``build_isolated_model`` uses --
        # the builder contributes no
        # metabolic reactions at all, every real reaction arrives above from
        # ``_add_reactions_from_gene_mapping``, and that method registers the
        # ``e0`` compartment but creates no exchanges. Without this call the
        # model reaches ATP correction with reactions but ZERO exchanges, so it
        # cannot take up any nutrient: ATP is 0 on all 54 ATP media, every
        # medium is recorded ``{"score": 1000, "failed": true}``, and
        # ``selected_media`` ends up empty. Nothing raises, and the model ships
        # with energy-generating cycles intact
        # (KBDLJobRunningPrototype#28, cause 1).
        #
        # ``add_exchanges_to_model`` is idempotent -- it skips any ``EX_`` id
        # already present -- so this is a strict no-op for the RAST-role path.
        self.MSBuilder.add_exchanges_to_model(model=mdlutl.model)

    def _base_model_keywords(
        self,
        *,
        base_model: Any,
        match_role_aliases: bool,
        allow_incomplete_complexes: bool,
    ) -> Dict[str, Any]:
        """Keyword arguments for the installed ``MSBuilder.build_base_model``.

        Args:
            base_model: The ``cobra.Model`` to build into.
            match_role_aliases: Caller's alias-matching request.
            allow_incomplete_complexes: Caller's incomplete-complex request.

        Returns:
            A kwargs dict carrying the four parameters every known ModelSEEDpy
            accepts, plus ``match_role_aliases`` / ``allow_incomplete_complexes``
            when the installed ``build_base_model`` accepts them.

        Raises:
            ModelSEEDpyCapabilityError: An unsupported keyword was requested at
                a non-default value, so honouring the request is impossible.

        The two alias/complex keywords are keyword-only and recent: the fork does
        not have them, and neither did upstream before the alias-matching change
        this layer pins.  Probing the signature is the only honest test --
        ``modelseedpy.__version__`` reads 0.4.2 on the fork and 0.4.3 on the pin
        while ``build()`` differs in arity and return type, so a version string
        identifies nothing.  Silently dropping a requested
        ``match_role_aliases=True`` would produce a model missing every
        alias-matched gene with nothing in the output saying so, which is why
        the mismatch raises instead.
        """
        import inspect

        keywords: Dict[str, Any] = {
            "model_or_id": base_model,
            "index": "0",
            "allow_all_non_grp_reactions": False,
            "annotate_with_rast": False,
        }

        requested = {
            "match_role_aliases": (match_role_aliases, False),
            "allow_incomplete_complexes": (allow_incomplete_complexes, True),
        }
        try:
            parameters = inspect.signature(
                self.MSBuilder.build_base_model
            ).parameters
        except (TypeError, ValueError):
            # Not introspectable (a Mock, a C wrapper). Forward both and let the
            # call itself be the test.
            keywords.update({name: value for name, (value, _) in requested.items()})
            return keywords

        accepts_anything = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        for name, (value, upstream_default) in requested.items():
            if accepts_anything or name in parameters:
                keywords[name] = value
            elif value != upstream_default:
                raise ModelSEEDpyCapabilityError(
                    f"{name}={value!r} was requested, but the installed "
                    f"MSBuilder.build_base_model does not accept it "
                    f"(signature: {tuple(parameters)}). Install the pinned "
                    f"ModelSEEDpy carrying the alias-matching change."
                )
        return keywords

    def build_metabolic_model(
        self,
        genome: Any,
        genome_classifier: Any,
        base_model: Optional[Any] = None,
        model_id: Optional[str] = None,
        model_name: Optional[str] = None,
        gs_template: str = "auto",
        core_template: Optional[Any] = None,
        gs_template_obj: Optional[Any] = None,
        atp_safe: bool = True,
        forced_atp_list: List[str] = None,
        atp_medias: List[str] = None,
        load_default_medias: bool = True,
        max_gapfilling: int = 10,
        gapfilling_delta: float = 0,
        remove_noncore_genome_reactions: bool = False,
        reactions_to_add: Optional[Dict[str, Union[List[str], str]]] = None,
        template_reactions_only: bool = False,
        ontology_term: str = DEFAULT_ONTOLOGY_TERM,
        match_role_aliases: bool = False,
        allow_incomplete_complexes: bool = True,
        return_reconstruction_result: bool = False,
    ) -> tuple:
        """Build a single metabolic model from an MSGenome object.

        This is a generalized function that builds a metabolic model without
        KBase-specific dependencies. It accepts an MSGenome directly rather
        than workspace references.

        The draft is built with ``MSBuilder.build_base_model``, which matches
        genome functions to template roles, forms complexes/GPRs and adds
        spontaneous/universal reactions, biomass, exchanges, drains and the
        objective.  ``MSBuilder.build()`` is NEVER called: upstream's ``build()``
        runs ``MSATPCorrection`` AND ``MSGapfill.run_gapfilling`` inside itself
        and returns a 4-tuple, so calling it would collapse the three stage
        boundaries this layer exists to separate and would gap-fill against
        media the caller never asked for.  Its second positional parameter is
        also ``gapfill_media`` upstream and ``index`` in the fork, which is why
        every call into ModelSEEDpy from this module uses keyword arguments.

        This method stops at POST-ATP.  Per-medium gap-fill is
        :meth:`gapfill_metabolic_model`'s stage.

        Args:
            genome: MSGenome object to build model from
            genome_classifier: Classifier object for determining template type
            base_model: Optional base FBAModel to build upon
            model_id: ID for the model (defaults to genome.id)
            model_name: Name for the model (defaults to genome.scientific_name)
            gs_template: Gram-specific template type ("auto", "gp", "gn", "ar", etc.)
            core_template: Core template object (loaded template, not reference)
            gs_template_obj: GS template object (loaded template, not reference)
            atp_safe: Whether to apply ATP-safe constraints
            forced_atp_list: List of media IDs where ATP production should be forced
            atp_medias: List of ATP media references
            load_default_medias: Whether to load default ATP medias
            max_gapfilling: Maximum number of gapfilling iterations
            gapfilling_delta: Delta threshold for gapfilling
            remove_noncore_genome_reactions: If True, remove non-core genome reactions
                (excludes bio, DM_, EX_, SK_ reactions)
            reactions_to_add: Dictionary mapping reaction IDs to EITHER a list of
                gene IDs (OR'd together into an isozyme GPR) OR a ready-made GPR
                string used verbatim, so enzyme complexes such as
                ``"(gA and gB) or gC"`` survive. Reactions are pulled from template
                or ModelSEED DB. See :meth:`_add_reactions_from_gene_mapping` for the
                exact merge rules when a reaction is already in the model.
            template_reactions_only: If True, reactions in ``reactions_to_add`` are only
                added when present in the template (the ModelSEED-DB fallback is skipped);
                reactions absent from the template are dropped silently. If False (default),
                reactions missing from the template fall back to any mass-balanced
                ModelSEED-DB reaction. Passed through to
                :meth:`_add_reactions_from_gene_mapping`.
            ontology_term: Ontology key ``MSBuilder`` reads genome functions
                from, and the key ``classify_genome`` classifies on. Passed
                explicitly rather than left to ModelSEEDpy's default so a
                caller that folds several annotation sources under one key (as
                KBDL does) does not have to rename it to ``"RAST"``.
            match_role_aliases: Match genome functions against template role
                ALIASES as well as role names. Opt-in because native ModelSEED
                templates alias nearly every role with an identifier such as
                ``msfr.1407``, so matching aliases on a native template is not
                free. Required for a KBDL template, whose roles carry the
                mapping cluster's member functions as aliases.
            allow_incomplete_complexes: Admit a template complex when only some
                of its subunits are present. Upstream's own default.
            return_reconstruction_result: Return the structured
                :class:`ReconstructionResult` in addition to the legacy pair.
                Left False by default because every existing caller unpacks two
                values.

        Returns:
            ``(current_output dict, MSModelUtil)`` -- or
            ``(current_output dict, None)`` when the genome was abandoned.  With
            ``return_reconstruction_result=True`` a third element, a
            :class:`ReconstructionResult`, is appended; its
            ``post_gapfill_results`` is an empty tuple because this method stops
            at POST-ATP.

        Raises:
            ModelSEEDpyCapabilityError: ``match_role_aliases=True`` or
                ``allow_incomplete_complexes=False`` was requested and the
                installed ``MSBuilder.build_base_model`` does not accept the
                corresponding keyword.
        """
        # Set defaults for mutable arguments
        if forced_atp_list is None:
            forced_atp_list = []
        if atp_medias is None:
            atp_medias = []
        if reactions_to_add is None:
            reactions_to_add = {}

        # Load the core template only if ATP correction is going to use it.
        #
        # ``self.templates["core"]`` is a KBase workspace reference, so this
        # fetch needs a workspace connection; performing it for a build that
        # will not run ATP correction turns an operation a KBase-free job can
        # complete into one it cannot.  A caller that supplies ``core_template``
        # never reaches here at all, which is the KBase-free path.
        if core_template is None and atp_safe:
            core_template = self.get_template(self.templates["core"], None)

        # Use local variable for GS template
        active_gs_template = gs_template_obj

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
        current_output["Genes"] = len(genome.features) if hasattr(genome, 'features') else 0

        template_type = gs_template

        # Classify the genome ONLY when we have to pick a template ourselves.
        #
        # A caller that supplies ``gs_template_obj`` has already decided which
        # template to build against -- KBDL, for one, builds its MSTemplate from
        # a kbdl-template/1 plus a kbdl-mapping document, and no classifier
        # verdict could select it. Classification costs a full feature sweep and
        # loads a classifier artifact, so running it to produce a key that is
        # then ignored is not merely redundant, it is a failure mode: the
        # classifier can raise in an environment the supplied template needs
        # nothing from.
        if template_type == "auto" and active_gs_template is None:
            template_type = self.classify_genome(
                genome=genome,
                ontology_term=ontology_term,
                classifier=genome_classifier,
            )
            # ``classify_genome`` resolves every class to a buildable key --
            # including the classifier's ``"--"`` no-call and class ``C``, which
            # the legacy inline branch abandoned the genome over. A KBase CTS job
            # has no operator standing by to choose a template for it.
            current_output["Class"] = TEMPLATE_KEY_TO_GENOME_CLASS_LABEL.get(
                template_type, template_type
            )

        # Load GS template if not provided
        if active_gs_template is None:
            active_gs_template = self.get_template(self.templates[template_type], None)

        # Create base model if not provided
        if base_model is None:
            import cobra
            base_model = cobra.Model(
                id_or_model=gid,
                name=model_name if model_name else genome.scientific_name
            )

        # Build the DRAFT model.
        #
        # ``build_base_model``, never ``build()``. See this method's docstring.
        #
        # ``annotate_with_rast=False`` because the genome arrives already
        # annotated: with it left at ModelSEEDpy's default True, ``MSBuilder``
        # constructs a ``RastClient`` and makes a live network call to re-annotate
        # it, which is both wrong (it discards the caller's annotations) and
        # unavailable to a KBase CTS job.
        #
        # ``add_reaction_from_rast_annotation`` is deliberately left at its
        # default True despite the name: it gates
        # ``MSBuilder.build_metabolic_reactions()``, i.e. the role-matched
        # reactions themselves, and has nothing to do with calling RAST.
        builder = self.MSBuilder(
            genome=genome,
            template=active_gs_template,
            ontology_term=ontology_term,
        )
        mdl = builder.build_base_model(
            **self._base_model_keywords(
                base_model=base_model,
                match_role_aliases=match_role_aliases,
                allow_incomplete_complexes=allow_incomplete_complexes,
            )
        )
        mdl.genome = genome
        mdl.template = active_gs_template
        current_output["Core GF"] = "NA"

        mdlutl = self.MSModelUtil.get(mdl)
        # Expose the initialised builder so callers can add more reactions (with
        # correct compartment/species maps) AFTER the build+gapfill — e.g. an
        # ATP-safe annotation overlay. The builder's base_model is this model.
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

        # Add reactions from reaction-to-gene mapping
        if reactions_to_add:
            self._add_reactions_from_gene_mapping(
                mdlutl, builder, reactions_to_add, active_gs_template,
                template_reactions_only=template_reactions_only,
            )

        # ── The two roleless-path compensations ───────────────────────────
        #
        # BOTH run ONLY when ``reactions_to_add`` was supplied, i.e. only on the
        # legacy roleless path they exist for.
        #
        # MEASURED (agent-io/work-records/task-d1302dc4.md section (f)): on a
        # role-based draft of H. influenzae against the native gram-negative
        # template, re-running ``build_non_metabolite_reactions`` returned 0
        # reactions and ``add_exchanges_to_model`` added none -- the draft stayed
        # at 957 reactions with the 73 exchanges and all 19 of the template's
        # spontaneous/universal reactions that ``build_base_model`` had already
        # put there. So on the native path they are strict no-ops that cost a
        # full template sweep, and #28/#29 are artifacts of the roleless path
        # rather than defects of the build.
        #
        # Gating them is not merely an optimisation. ``build_base_model`` makes
        # its own ``build_non_metabolite_reactions`` call and its own
        # ``add_exchanges_to_model`` call; running them again unconditionally
        # means every native build re-sweeps the template to confirm a no-op, and
        # any future divergence in the inclusion predicate between the two calls
        # would double-apply silently. Keeping them for ``reactions_to_add``
        # preserves the existing callers exactly -- the injection genuinely does
        # introduce metabolites and extracellular species that neither of
        # ``build_base_model``'s internal calls could have seen.
        if reactions_to_add:
            self._apply_roleless_path_compensations(mdlutl=mdlutl, builder=builder)

        draft_facts = None
        pre_atp_reaction_ids: set = set()
        if return_reconstruction_result:
            # DRAFT boundary: everything assembled, nothing ATP-specific yet.
            draft_facts = _draft_model_facts(model=mdlutl.model)
            pre_atp_reaction_ids = {rxn.id for rxn in mdlutl.model.reactions}

        # ── POST-ATP: ATP correction, with the SUPPLIED core template ─────
        atp_invocations = 0
        atp_test_count: Optional[int] = None
        core_gapfilling_count: Optional[int] = None
        selected_media_ids: tuple = ()
        atp_analysis: Dict[str, Any] = {}
        atp_removed_reaction_ids: tuple = ()
        if atp_safe:
            # Where the default ATP media get loaded depends on which
            # ModelSEEDpy is installed, and the difference is not cosmetic.
            #
            # UPSTREAM has BOTH a module-level function ``load_default_medias``
            # and an ``__init__`` parameter of the same name. Inside
            # ``MSATPCorrection.__init__`` the name resolves to the BOOL
            # PARAMETER, so ``load_default_medias(default_media_path)`` raises
            #     TypeError: 'bool' object is not callable
            # for any truthy value. Upstream never trips over it because its own
            # ``build()`` passes ``load_default_medias=False`` and pre-loads the
            # media itself. This layer used to forward the flag straight through
            # with default True, which made every ATP-safe build against upstream
            # raise before solving.
            #
            # The FORK has no module-level function; its loader is the METHOD
            # ``self.load_default_medias``, which ``__init__`` calls
            # unambiguously and which works. There the flag is forwarded as the
            # caller asked.
            #
            # Probing for the function rather than branching on a version string
            # is the point: the two trees report 0.4.2 and 0.4.3.
            effective_atp_medias = list(atp_medias)
            forward_load_default_medias = False
            if load_default_medias:
                from modelseedpy.core import msatpcorrection as _msatpcorrection

                default_media_loader = getattr(
                    _msatpcorrection, "load_default_medias", None
                )
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
                default_media_path=self.modelseedpy_data_dir + "/atp_medias.tsv",
            )
            # EXACTLY ONCE. ``run_atp_correction`` mutates the model -- it
            # disables noncore reactions, gap-fills the core against the supplied
            # core template, then re-expands to genome scale while filtering the
            # reactions that break ATP -- so a second call re-filters an
            # already-filtered model and the second verdict is not comparable to
            # the first.
            tests = atpcorrection.run_atp_correction()
            atp_invocations = 1
            atp_test_count = len(tests) if tests is not None else None
            core_gapfilling_count = len(atpcorrection.cumulative_core_gapfilling)
            current_output["Core GF"] = core_gapfilling_count

            # Publish the ATP tests on the model util so downstream gap-fill can
            # be ATP-safe without recomputing them. ``MSModelUtil.atp_tests`` is
            # the attribute ``get_atp_tests`` caches into, so a caller may reach
            # these either way. Previously ``tests`` was computed here and
            # dropped on the floor, which left every genome-scale gap-fill in
            # GAA's ``gapfill_reaction_set`` free to re-add the very cycles ATP
            # correction had just filtered out (KBDLJobRunningPrototype#28,
            # cause 3).
            mdlutl.atp_tests = tests

        if atp_safe and return_reconstruction_result:
            # POST-ATP boundary. Collected only for the structured result, so
            # the legacy two-value path reads nothing off MSATPCorrection it did
            # not read before.
            selected_media_ids = tuple(
                media.id for media in (atpcorrection.selected_media or [])
            )
            atp_removed_reaction_ids = tuple(
                sorted(
                    pre_atp_reaction_ids - {rxn.id for rxn in mdlutl.model.reactions}
                )
            )
            atp_analysis = {
                "stage": "post-atp",
                "atp_hydrolysis_id": atpcorrection.atp_hydrolysis.id,
                "test_count": atp_test_count,
                "core_gapfilling_count": core_gapfilling_count,
                "selected_media_ids": list(selected_media_ids),
                "atp_media_count": len(effective_atp_medias),
                "forced_media": list(forced_atp_list),
                "removed_reaction_ids": list(atp_removed_reaction_ids),
                # Keyed by media ID, not by the MSMedia OBJECT MSATPCorrection
                # keys it by: ``ReconstructionResult.to_dict`` has to survive
                # ``json.dumps``, and an object key makes it raise "keys must be
                # str, int, float, bool or None".
                "media_gapfill_stats": {
                    (media.id if hasattr(media, "id") else str(media)): stats
                    for media, stats in atpcorrection.media_gapfill_stats.items()
                },
            }
        # Set model attributes
        mdlutl.get_attributes()["class"] = current_output["Class"]
        mdlutl.wsid = gid

        current_output["Reactions"] = mdlutl.nonexchange_reaction_count()
        current_output["Model genes"] = len(mdlutl.model.genes)

        if not return_reconstruction_result:
            return current_output, mdlutl

        modelseedpy_version, modelseedpy_revision = resolve_distribution_provenance(
            "modelseedpy"
        )
        kbutillib_version, kbutillib_revision = resolve_distribution_provenance(
            "KBUtilLib"
        )
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
            post_atp_reaction_count=(
                len(mdlutl.model.reactions) if atp_safe else None
            ),
            post_atp_removed_reaction_ids=atp_removed_reaction_ids,
            post_atp_core_gapfilling_count=core_gapfilling_count,
            post_atp_selected_media_ids=selected_media_ids,
            post_atp_test_count=atp_test_count,
            post_atp_analysis=atp_analysis,
            # Empty by design: this method stops at POST-ATP. gapfill_metabolic_model
            # is the stage that fills this.
            post_gapfill_results=(),
            genome_id=genome.id,
            genome_class=current_output["Class"],
            template_id=active_gs_template.id,
            template_name=active_gs_template.name,
            core_template_id=(core_template.id if atp_safe else None),
            core_template_name=(core_template.name if atp_safe else None),
            ontology_term=ontology_term,
            match_role_aliases=match_role_aliases,
            allow_incomplete_complexes=allow_incomplete_complexes,
            reactions_to_add_supplied=bool(reactions_to_add),
            modelseedpy_version=modelseedpy_version,
            modelseedpy_revision=modelseedpy_revision,
            kbutillib_version=kbutillib_version,
            kbutillib_revision=kbutillib_revision,
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
        media_objs: List[Any],
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
        reaction_scores: Optional[Dict[str, float]] = {},
    ) -> tuple:
        """Gapfill a single metabolic model to enable growth on specified media.

        This is a generalized function that gapfills a metabolic model without
        KBase-specific dependencies. It accepts pre-loaded objects rather than
        workspace references.

        Args:
            mdlutl: MSModelUtil object to gapfill
            genome: MSGenome object associated with the model
            media_objs: List of media objects for gapfilling
            templates: List of template objects for gapfilling
            core_template: Core template object (for ATP tests)
            source_models: Source models for reaction candidates
            additional_tests: Additional constraint tests for gapfilling
            expression_obj: Expression data object for this model
            atp_safe: Whether to apply ATP-safe constraints
            reaction_exclusion_list: Reactions to exclude from gapfilling
            objective: Objective function to optimize
            minimum_objective: Minimum objective value for gapfilling
            gapfilling_mode: Gapfilling mode ("Sequential" or "Cumulative")
            base_media: Base media object for gapfilling
            base_media_target_element: Target element for base media

        Returns:
            Tuple of (current_output dict, solutions dict, output_solution, output_solution_media)
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

        # Compute tests for ATP safe gapfilling
        atp_tests = []
        if atp_safe:
            atp_tests = mdlutl.get_atp_tests(
                core_template=core_template,
                atp_media_filename=self.modelseedpy_data_dir + "/atp_medias.tsv",
                recompute=False,
            )


        all_tests = additional_tests + atp_tests

        # Create gapfilling object
        msgapfill = self.MSGapfill(
            mdlutl,
            templates,
            source_models,
            all_tests,
            blacklist=reaction_exclusion_list,
            default_target=objective,
            minimum_obj=minimum_objective,
            base_media=base_media,
            base_media_target_element=base_media_target_element,
        )

        # Set reaction scores from genome
        msgapfill.reaction_scores = reaction_scores

        # Handle expression data
        if expression_obj:
            expression_scores = msgapfill.compute_reaction_weights_from_expression_data(
                expression_obj, genome.annoont
            )
            for rxn_id in msgapfill.reaction_scores:
                for gene in msgapfill.reaction_scores[rxn_id]:
                    if gene in expression_scores:
                        msgapfill.reaction_scores[rxn_id][gene]["probability"] = (
                            expression_scores[gene] + 0.5
                        )

        # Run gapfilling in all conditions
        mdlutl.gfutl.cumulative_gapfilling = []
        growth_array = []
        solutions = msgapfill.run_multi_gapfill(
            media_objs,
            target=objective,
            default_minimum_objective=minimum_objective,
            binary_check=False,
            prefilter=True,
            check_for_growth=True,
            gapfilling_mode=gapfilling_mode,
            run_sensitivity_analysis=True,
            integrate_solutions=True,
        )

        output_solution = None
        output_solution_media = None
        # `run_multi_gapfill` returns None when no gapfill solution exists
        # (e.g. solver infeasible, model+media combination cannot grow). Guard
        # before treating `solutions` as a dict; otherwise `media in solutions`
        # raises TypeError.
        if solutions is None:
            solutions = {}
        for media in media_objs:
            if media in solutions and "growth" in solutions[media]:
                growth_array.append(media.id + ":" + str(solutions[media]["growth"]))
                if solutions[media]["growth"] > 0 and output_solution is None:
                    mdlutl.pkgmgr.getpkg("KBaseMediaPkg").build_package(media)
                    mdlutl.pkgmgr.getpkg("ElementUptakePkg").build_package({"C": 60})
                    output_solution = cobra.flux_analysis.pfba(mdlutl.model)
                    output_solution_media = media

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
            mdlutl,
            templates,
            source_models,
            all_tests,
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
            media_list,
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
                    newmedia = self.MSMedia.from_dict({cpd: 100})
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

            # Call the generalized gapfill_metabolic_model function
            gf_output, solutions, output_solution, output_solution_media = self.gapfill_metabolic_model(
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
                    msmodrep = self.MSModelReport(model)
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


    def classify_genome(
        self,
        genome: Any,
        *,
        ontology_term: str = "RAST",
        classifier: Optional[Any] = None,
        classifier_id: Optional[str] = None,
    ) -> str:
        """Classify a genome and return the template key to build it against.

        Uses the classifier the installed ModelSEEDpy ships (the ``.npz`` under
        ``modelseedpy/data/ml``, loaded through ``modelseedpy.helpers``), and
        applies the same class -> template-key map that
        ``build_metabolic_model`` applies inline for ``gs_template="auto"``:
        ``P`` -> ``gp``, ``N`` -> ``gn``, ``A`` -> ``ar``, the classifier's
        ``"--"`` no-call -> ``gn``.

        Any other class -- notably ``C`` (Cyanobacteria) -- has no dedicated
        template in ``self.templates`` and resolves to
        :data:`DEFAULT_TEMPLATE_KEY` (``gn``) with a warning.  Returning a
        buildable key rather than None is deliberate: the legacy inline branch
        abandoned the genome instead, and a KBase CTS job has no operator to
        pick a template for it.

        The classifier reads ``feature.ontology_terms[ontology_term]`` only; it
        never touches sequences.

        Args:
            genome: ``MSGenome`` whose features carry functions under
                ``ontology_term``.
            ontology_term: Ontology key to classify from.  KBDL folds every
                annotation source under one key, which is what makes this a
                single argument.
            classifier: Pre-loaded classifier, used as given.  Mainly for tests
                and for reusing one classifier across many genomes.
            classifier_id: Explicit classifier id, passed to
                ``modelseedpy.helpers.get_classifier``.

        Returns:
            A key of ``self.templates`` (``"gp"``, ``"gn"`` or ``"ar"``).

        Raises:
            ModelSEEDpyCapabilityError: No classifier artifact is available.
        """
        if classifier is None:
            classifier = _load_upstream_genome_classifier(classifier_id=classifier_id)

        genome_class = str(classifier.classify(genome, ontology_term=ontology_term))
        template_key = GENOME_CLASS_TO_TEMPLATE_KEY.get(genome_class)
        if template_key is None:
            self.log_warning(
                f"genome class {genome_class!r} has no dedicated template; "
                f"using {DEFAULT_TEMPLATE_KEY!r}"
            )
            template_key = DEFAULT_TEMPLATE_KEY
        self.log_info(f"genome {getattr(genome, 'id', '?')} class {genome_class} -> template {template_key}")
        return template_key

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
