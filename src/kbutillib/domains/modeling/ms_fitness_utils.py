"""Fitness-weighted evidence analysis — the KBase-free, GAA-free entry point.

This is the one entry point KBDL's ``KBDLFitnessModelAnalysis`` job calls.  It
replaces the job's import of ``genome_annotation_aggregator.fitness_simulation``
(``simulate_model_conditions``), which is being severed from all modelling
(Chris, 2026-11-14).

What does the modelling, and what does NOT
------------------------------------------
The method itself is ModelSEEDpy's
:class:`modelseedpy.core.msfitnessweighted.MSFitnessWeightedAnalysis` — Jose
Faria's port of the GAA method, adopted UNCHANGED (Chris, 2026-11-16: "let's
just use Jose's port of this and conform as much as possible with what he's
done.  I don't want to create another divergent system").  Nothing in this
module adds a parameter, an option or a behaviour to that class; everything
here is built on its PUBLIC surface (the constructor, ``run``,
``analyze_condition``, ``gene_fit_best``, ``reaction_penalties``, ``edges``,
``draft_reactions``, ``mass_imbalanced_ids``, ``superset``,
``atp_tests_from_correction`` and the module function ``normalize_fitness``).

The KBDL-specific behaviour lives HERE, in full:

1. **Reaction-id normalisation** (:func:`normalize_gene_reactions`).  GAA looked
   up gene edges by bare ModelSEED id (``rxn05294``) against cobra ids
   (``rxn05294_c0``), so on the old path the edges matched nothing and the
   fitness values never reached the penalty at all (KBDL issue #44).  A key that
   maps to nothing is COUNTED and reported, never silently dropped.
2. **The no-t-score significance rule** (``missing_t_policy``).  Jose's class
   treats a fitness row as significant only when ``fitness < 0`` and
   ``|t| >= t_sig``, so a row with no t-score is never significant.  KBDL's
   fitness tables routinely carry no t-score, and Price et al. (2018) treat
   ``fitness < -2`` as a strong phenotype on its own, so KBDL admits such rows
   — through the class's own gate, by supplying ``t_score = -t_sig`` for them,
   and then restoring ``t_score = None`` in the reported evidence.
3. **The discount count** (``n_reactions_discounted`` per condition).  How many
   model reactions the fitness data actually re-priced.  A zero here with
   non-empty fitness is the silent no-op this PRD exists to stop.
4. **The Complete leg** (``require_fitness=False``).  A condition with no
   fitness rows is still solved, at uniform model prices, instead of being
   reported ``no_fitness``.
5. **The ATP leak diagnostic** (``atp_safe=True`` only).  Whether the
   ATP-filtered superset can still make ATP with nothing coming in.  Measured on
   a COPY; the analysis superset is left exactly as the run left it.

Two reaction-id vocabularies, and why normalisation is not optional
-------------------------------------------------------------------
A KBDL gene mapping is keyed by whatever the annotation source produced: a bare
ModelSEED id (``rxn00001``), a model reaction id (``rxn00001_c0``), or both at
once for the same reaction.  Both resolve to the same cobra reaction here, their
genes are UNIONed and each gene's source strings are unioned and de-duplicated,
so neither key can overwrite the other and the result does not depend on the
order the keys arrived in.  Source strings are opaque and are never split.

What this module does not do
----------------------------
It constructs no ``MSFBAUtils`` and no KBUtilLib biochemistry utility — not
lazily, not as a fallback.  The only KBUtilLib call it makes is
:func:`~.ms_reconstruction_utils.initialize_reconstruction_environment` (with
``check_alias_matching=False``: this layer builds no model from a KBDL template,
so the alias behaviour it checks cannot affect a fitness result), plus
``resolve_distribution_provenance`` to record WHICH ModelSEEDpy ran.

It also does not retry.  A condition whose solve returns non-optimal keeps
whatever status the class reported (``solver_failed``, ``no_growth``) and the
run moves on to the next condition; ``time_s`` per condition comes from the
class.  A solver that RAISES propagates and ends the run — deliberately, since
the alternative is reporting a partial panel as a complete one.
"""

from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "COMPARTMENT_SUFFIX",
    "MISSING_T_POLICIES",
    "DEFAULT_FIT_ONLY_THRESHOLD",
    "normalize_gene_reactions",
    "run_fitness_weighted_analysis",
]

#: Terminal compartment suffix of a cobra reaction id built by ModelSEED
#: conventions: an underscore, one compartment letter, one or more index digits
#: (``_c0``, ``_e0``, ``_m1``).  Stripping it from ``rxn00001_c0`` gives the bare
#: ModelSEED id ``rxn00001``, which is the OTHER vocabulary KBDL gene mappings
#: arrive in.  Anchored at the end, so ``EX_glc__D_e0`` -> ``EX_glc__D`` and
#: ``PGI_c0`` -> ``PGI``, while an id with no such suffix is left alone.
COMPARTMENT_SUFFIX = re.compile(r"_[a-z][0-9]+$")

#: Accepted ``missing_t_policy`` values.  ``fitness_only``: a row with no
#: t-score is significant when its fitness is strictly below
#: ``fit_only_threshold`` (Price et al. 2018 strong phenotype) — KBDL's choice.
#: ``ignore``: keep Jose's rule, so a row with no t-score is never significant.
MISSING_T_POLICIES: Tuple[str, ...] = ("fitness_only", "ignore")

#: Fitness below which a row with NO t-score counts as a strong phenotype.
#: Price et al. (2018); the same number as the class's ``fit_important``, but a
#: separate knob because it answers a different question (admit the row at all,
#: versus weight it tier-1 once admitted).
DEFAULT_FIT_ONLY_THRESHOLD: float = -2.0

#: How many unmapped gene-mapping keys are reported verbatim alongside the
#: count.  A sample, not the list: a mapping built against the wrong
#: vocabulary produces thousands and the count is the number that matters.
UNMAPPED_SAMPLE_SIZE: int = 20


# ---------------------------------------------------------------------------
# Lazy imports — importing this module must not require modelseedpy
# ---------------------------------------------------------------------------


def _fitness_module() -> Any:
    """``modelseedpy.core.msfitnessweighted``, with the extra named when absent.

    Imported lazily, and through a function, so importing this module — and so
    reading its documentation or calling :func:`normalize_gene_reactions` —
    never requires modelseedpy.

    Returns:
        The imported module.

    Raises:
        ImportError: modelseedpy is absent, or is an install without the
            fitness port (anything before ``kbdl-target`` d55034c).
    """
    try:
        from modelseedpy.core import msfitnessweighted  # type: ignore[import]
    except ImportError as exc:  # pragma: no cover - import guard
        raise ImportError(
            "modelseedpy with MSFitnessWeightedAnalysis is required to run a "
            "fitness-weighted analysis. Install the model extra: pip install "
            "kbutillib[model] (it pins the ModelSEEDpy commit that carries "
            "modelseedpy/core/msfitnessweighted.py)"
        ) from exc
    return msfitnessweighted


def _release(model: Any) -> None:
    """Drop a throwaway model's MSModelUtil / package-manager registrations.

    ``MSModelUtil.get`` and ``MSPackageManager.get_pkg_mgr`` cache per model in
    class-level dicts, so a copy that is not released stays reachable for the
    life of the process.  The same two lines as
    ``msfitnessweighted._release``, which is private to that module.

    Args:
        model: The cobra model to forget.
    """
    try:
        from modelseedpy.core.msmodelutl import MSModelUtil  # type: ignore[import]
        from modelseedpy.fbapkg.mspackagemanager import (  # type: ignore[import]
            MSPackageManager,
        )
    except ImportError:  # pragma: no cover - import guard
        return
    MSModelUtil.mdlutls.pop(model, None)
    MSPackageManager.pkgmgrs.pop(model, None)


def _cobra_model(model: Any) -> Any:
    """The ``cobra.Model`` behind a ``cobra.Model`` or an ``MSModelUtil``.

    Args:
        model: Either shape, as the public entry point accepts.

    Returns:
        The cobra model.
    """
    inner = getattr(model, "model", None)
    if inner is not None and hasattr(inner, "reactions"):
        return inner
    return model


# ---------------------------------------------------------------------------
# (2a) Reaction-id normalisation
# ---------------------------------------------------------------------------


def normalize_gene_reactions(
    *,
    gene_reactions: Mapping[str, Any],
    model_reaction_ids: Iterable[str],
) -> Tuple[Dict[str, Dict[str, List[str]]], List[str]]:
    """Resolve a gene mapping's reaction keys onto model reaction ids.

    A key is taken as a model reaction id when the model has one by that exact
    name; otherwise it is taken as a BARE id and maps to every model reaction
    whose id, with a terminal :data:`COMPARTMENT_SUFFIX` stripped, equals it.
    One model reaction can therefore be named by several keys (``rxn00001`` and
    ``rxn00001_c0``), and one bare key can name several model reactions
    (``rxn00001_c0`` and ``rxn00001_e0``).

    Args:
        gene_reactions: ``{reaction_id: {gene: [source, ...]}}`` or
            ``{reaction_id: [gene, ...]}``, keyed by model id or bare id.
        model_reaction_ids: Every reaction id in the model.

    Returns:
        ``(merged, unmapped)``.  ``merged`` is
        ``{model_reaction_id: {gene: [source, ...]}}``: genes are UNIONed across
        every key that resolved to that reaction and each gene's sources are
        unioned, de-duplicated and SORTED, so the result is independent of the
        order the input keys were iterated in.  ``unmapped`` is the sorted list
        of distinct input keys that matched no model reaction.

    Source strings are opaque — unioned and compared whole, never split or
    parsed — because a source is provenance text from an annotation pipeline and
    this layer is not entitled to interpret it.
    """
    model_ids = set(model_reaction_ids)
    by_base: Dict[str, List[str]] = defaultdict(list)
    for rid in sorted(model_ids):
        by_base[COMPARTMENT_SUFFIX.sub("", rid)].append(rid)

    # Accumulate as sets, then render sorted: a set union is order-independent
    # by construction, which is the property the caller is promised.
    staged: Dict[str, Dict[str, Set[str]]] = {}
    unmapped: Set[str] = set()
    for key in sorted(gene_reactions):
        if key in model_ids:
            targets: Sequence[str] = [key]
        else:
            targets = by_base.get(key, [])
        if not targets:
            unmapped.add(key)
            continue
        genes = gene_reactions[key]
        if isinstance(genes, Mapping):
            pairs = [(gene, sources) for gene, sources in genes.items()]
        else:
            pairs = [(gene, None) for gene in genes]
        for rid in targets:
            slot = staged.setdefault(rid, {})
            for gene, sources in pairs:
                bucket = slot.setdefault(gene, set())
                if sources is None:
                    bucket.add("model")
                elif isinstance(sources, str):
                    bucket.add(sources)
                else:
                    bucket.update(str(source) for source in sources)

    merged: Dict[str, Dict[str, List[str]]] = {}
    for rid in sorted(staged):
        merged[rid] = {gene: sorted(staged[rid][gene]) for gene in sorted(staged[rid])}
    return merged, sorted(unmapped)


# ---------------------------------------------------------------------------
# (2b) The no-t-score significance rule
# ---------------------------------------------------------------------------


def _policy_adjusted_fitness(
    *,
    normalized: Mapping[str, Tuple[Optional[float], Optional[float]]],
    missing_t_policy: str,
    fit_only_threshold: float,
    t_sig: float,
) -> Tuple[Dict[str, Tuple[Optional[float], Optional[float]]], Set[str]]:
    """Admit strong no-t-score rows through the class's OWN significance gate.

    Jose's class uses the t-score for exactly one thing — the gate
    ``fitness < 0 and |t| >= t_sig`` (``msfitnessweighted._significant_negative``,
    reached from ``normalize_fitness``, ``gene_fit_best`` and
    ``classify_impact``) — and for nothing else: no tier, no weight and no score
    is computed FROM ``t``, only gated by it.  That is what makes this
    substitution safe rather than a second scoring rule: supplying
    ``t_score = -t_sig`` for a row the policy admits changes which rows pass the
    gate and changes nothing about what happens afterwards.  The reported
    evidence gets ``t_score = None`` back, so no consumer ever sees the
    synthetic value.

    Args:
        normalized: ``{gene: (fitness, t_score)}`` from modelseedpy's
            ``normalize_fitness`` — the real data, before any substitution, so
            its own multi-row-per-gene preference is applied to real t-scores.
        missing_t_policy: ``fitness_only`` or ``ignore``.
        fit_only_threshold: Strict upper bound on the fitness of an admitted
            no-t-score row.
        t_sig: The constructed object's ``t_sig``, so the substituted value is
            exactly at the gate the object will apply.

    Returns:
        ``(adjusted, admitted)``: the fitness mapping to hand the class, and the
        set of genes whose t-score was substituted.
    """
    adjusted = dict(normalized)
    if missing_t_policy == "ignore":
        return adjusted, set()
    admitted: Set[str] = set()
    for gene, (fit, t) in normalized.items():
        if t is None and fit is not None and fit < fit_only_threshold:
            adjusted[gene] = (fit, -abs(t_sig))
            admitted.add(gene)
    return adjusted, admitted


def _annotate_significance(
    *,
    rows: Iterable[Dict[str, Any]],
    admitted: Set[str],
    t_sig: float,
) -> None:
    """Add ``significance`` to every edge-evidence row and undo the substitution.

    Args:
        rows: The condition's ``edge_evidence`` rows, modified IN PLACE.
        admitted: Genes whose t-score this wrapper supplied.
        t_sig: The object's significance threshold.

    Every row gets a ``significance`` of ``"t"`` (a real t-score passed the
    gate), ``"fitness_only"`` (admitted by this wrapper's policy) or ``None``
    (the row is in the evidence for another reason — an essential gene, or a
    negative verdict).  A ``fitness_only`` row also gets ``t_score`` back to
    ``None``.
    """
    for row in rows:
        if row.get("gene") in admitted:
            row["significance"] = "fitness_only"
            row["t_score"] = None
            continue
        fit = row.get("fitness")
        t = row.get("t_score")
        significant = fit is not None and t is not None and fit < 0 and abs(t) >= t_sig
        row["significance"] = "t" if significant else None


# ---------------------------------------------------------------------------
# (2c) The discount count
# ---------------------------------------------------------------------------


def _count_discounted(
    *,
    analysis: Any,
    fitness: Mapping[str, Tuple[Optional[float], Optional[float]]],
) -> int:
    """Model reactions whose fitness-derived base price is below ``p_base``.

    Args:
        analysis: The constructed ``MSFitnessWeightedAnalysis``, after its
            superset is built (``mass_imbalanced_ids`` only exists then).
        fitness: The condition's normalised, policy-adjusted fitness.

    Returns:
        The count.

    This is exactly ``reaction_penalties``' discount branch, counted rather than
    re-derived: a model reaction (``draft_reactions``) that is not overridden by
    the mass-imbalance price (``mass_imbalanced_ids``) and has at least one gene
    with a significant negative fitness gets
    ``fitness_penalty(best) < p_base`` — strictly, since ``best > 0`` there.
    Candidates are excluded (they are not model reactions) and reversal
    replacement is irrelevant (it replaces the price of a DIRECTION that the
    superset alone opens, after this discount is computed).  Whether the
    reaction carries flux does not matter: this counts pricing, not use.
    """
    best = analysis.gene_fit_best(fitness)
    if not best:
        return 0
    discounted = 0
    for rid, genes in analysis.edges.items():
        if rid not in analysis.draft_reactions:
            continue
        if rid in analysis.mass_imbalanced_ids:
            continue
        if any(gene in best for gene in genes):
            discounted += 1
    return discounted


# ---------------------------------------------------------------------------
# (2e) The ATP leak diagnostic
# ---------------------------------------------------------------------------


def _atp_hydrolysis_hint(model: Any) -> Optional[str]:
    """The model's existing ATP hydrolysis reaction, by the class's OWN rule.

    Args:
        model: The cobra model.

    Returns:
        The reaction id, or ``None`` when the model has no such reaction.

    ``MSATPCorrection`` falls back to ``MSModelUtil.add_atp_hydrolysis``, which
    builds the reaction from ModelSEED compound ids (``cpd00001_c0``) and
    returns ``None`` on a model named in another vocabulary -- which then
    surfaces inside ``MSATPCorrection.__init__`` as
    ``TypeError: 'NoneType' object is not subscriptable``, nowhere near the
    cause.  Naming the reaction up front avoids that for any model that already
    has one, and the test applied here (``rxn00062_*`` or ``ATPM*``) is
    character-for-character the one ``MSFitnessWeightedAnalysis._empty_atp_test``
    uses, including the iteration order, so this resolves to the same reaction
    the class itself would pick.
    """
    for reaction in model.reactions:
        if reaction.id.startswith("rxn00062_") or reaction.id.startswith("ATPM"):
            return reaction.id
    return None


def _atp_drain_id(
    *,
    leak_model: Any,
    atp_tests: Optional[Sequence[Mapping[str, Any]]],
    compartment: str,
) -> str:
    """The reaction to maximise in the leak test, resolved or created.

    Args:
        leak_model: The COPY of the superset the diagnostic runs on.
        atp_tests: The tests this call built, if any.
        compartment: Compartment to add ATP hydrolysis in when there is nothing
            to reuse.

    Returns:
        The reaction id.

    Raises:
        ValueError: No ATP hydrolysis reaction exists and none can be added.

    Preference order: the empty-medium test's own ATP objective (so the
    diagnostic measures the very reaction the filter was built around), then ATP
    hydrolysis in ``compartment``, resolved on the copy or added to it.
    """
    for test in atp_tests or []:
        media = test.get("media")
        objective = test.get("objective")
        is_empty = getattr(media, "id", None) == "empty" or not getattr(
            media, "mediacompounds", None
        )
        if (
            is_empty
            and isinstance(objective, str)
            and objective in leak_model.reactions
        ):
            return objective
    from modelseedpy.core.msmodelutl import MSModelUtil  # type: ignore[import]

    output = MSModelUtil.get(leak_model).add_atp_hydrolysis(compartment)
    if not output or output.get("reaction") is None:
        raise ValueError(
            "ATP leak diagnostic: no ATP hydrolysis reaction in compartment "
            f"{compartment} and none could be added"
        )
    return output["reaction"].id


def _atp_leak(
    *,
    analysis: Any,
    atp_tests: Optional[Sequence[Mapping[str, Any]]],
    compartment: str,
) -> Tuple[Optional[float], Optional[str]]:
    """Maximise ATP production with nothing coming in, on a COPY of the superset.

    Args:
        analysis: The constructed object, after ``run``.
        atp_tests: The tests this call built, if any.
        compartment: Compartment for the ATP hydrolysis fallback.

    Returns:
        ``(flux, status)``: the optimal ATP drain flux (``None`` when the solve
        is not optimal or the value is not finite) and the solver status.

    ``atp_safe`` filters the candidate database with ATP tests, but a filtered
    superset can still hold an energy-generating loop that the tests did not
    reach — and when the filter leaks, every condition's gap-fill is scored
    against a network that makes free ATP.  This measures it directly: biomass
    is released from any minimum, every boundary reaction's uptake is closed,
    the ATP drain is opened, and the drain is maximised.  A positive leak is a
    REPORTED number and never an exception; the caller decides what to do about
    it.  The analysis superset is untouched — everything happens on a copy,
    which is also what lets ``superset`` bounds be asserted unchanged
    afterwards.
    """
    leak_model = analysis.superset.copy()
    status: Optional[str] = None
    flux: Optional[float] = None
    try:
        # The gap-fill objective constraint is already gone (build_superset
        # removes ObjConstPkg's "objc"); remove anything left of it, and the
        # biomass reaction's own lower bound, so the leak test is never forced
        # to grow -- on an empty medium a forced biomass makes it infeasible.
        leftover = [
            cons
            for cons in leak_model.constraints
            if (getattr(cons, "name", "") or "").startswith("objc")
        ]
        if leftover:
            leak_model.remove_cons_vars(leftover)
        biomass = leak_model.reactions.get_by_id(analysis.biomass)
        biomass.bounds = (0.0, max(biomass.upper_bound, 0.0))
        for reaction in leak_model.reactions:
            if reaction.boundary:
                reaction.lower_bound = 0.0
        drain_id = _atp_drain_id(
            leak_model=leak_model, atp_tests=atp_tests, compartment=compartment
        )
        drain = leak_model.reactions.get_by_id(drain_id)
        drain.bounds = (0.0, max(drain.upper_bound, 1000.0))
        leak_model.objective = drain
        leak_model.objective_direction = "max"
        solution = leak_model.optimize()
        status = str(solution.status)
        value = solution.objective_value
        if status == "optimal" and value is not None and math.isfinite(value):
            flux = float(value)
        else:
            logger.warning(
                "ATP leak diagnostic did not reach an optimum (status %s, "
                "objective %s); reporting atp_leak_flux None",
                status,
                value,
            )
    finally:
        _release(leak_model)
    return flux, status


def _neutral_gene(*, analysis: Any, model: Any) -> str:
    """A gene id to hang the Complete leg's neutral fitness row on.

    Args:
        analysis: The constructed object, for its normalised ``edges``.
        model: The cobra model, consulted only when the edges have no genes.

    Returns:
        The lexically first gene in the edges, else the lexically first gene in
        the model.

    Raises:
        ValueError: The model has no genes, so there is nothing to name.

    The row carries fitness 0.0 and no t-score, so it is not significant under
    any policy and cannot change a price; WHICH gene it names is therefore
    immaterial, and lexically first makes it reproducible.
    """
    genes = sorted({gene for edge in analysis.edges.values() for gene in edge})
    if genes:
        return genes[0]
    model_genes = sorted(gene.id for gene in model.genes)
    if model_genes:
        return model_genes[0]
    raise ValueError(
        "require_fitness=False needs one gene to carry the neutral fitness row "
        "that gets the condition past the class's `if not fit and not "
        "essential_genes` check, and this model has no genes at all"
    )


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def run_fitness_weighted_analysis(
    *,
    model: Any,
    template: Any,
    conditions: Mapping[str, Mapping[str, Any]],
    gene_reactions: Optional[Mapping[str, Any]] = None,
    biomass: str = "bio1",
    atp_safe: bool = False,
    core_template: Any = None,
    blacklist: Iterable[str] = (),
    biomass_reference: str = "model",
    missing_t_policy: str = "fitness_only",
    fit_only_threshold: float = DEFAULT_FIT_ONLY_THRESHOLD,
    nonpositive_penalty: str = "one",
    price_reversals: bool = True,
    frac: float = 1.0,
    loopless: str = "auto",
    **method_params: Any,
) -> Dict[str, Any]:
    """Run the fitness-weighted per-condition analysis the KBDL way.

    Args:
        model: ``cobra.Model`` or ``MSModelUtil``.  Not modified.
        template: ``MSTemplate`` whose reactions are the gap-fill candidates.
        conditions: ``{condition_id: spec}``, where ``spec`` carries ``media``
            (``MSMedia``, ``{cpd: (lb, ub)}`` or ``None``), ``fitness`` (any
            shape modelseedpy's ``normalize_fitness`` accepts),
            ``essential_genes`` (optional) and ``require_fitness`` (optional,
            default ``True``; ``False`` selects the Complete leg below).
        gene_reactions: ``{reaction_id: {gene: [source, ...]}}`` keyed by model
            reaction id OR bare ModelSEED id, in any mixture.  ``None`` takes
            the edges from the model's own gene rules.
        biomass: Biomass reaction id.  ``bio1`` is the ModelSEED convention;
            pass the real id for a model that does not follow it (ModelSEEDpy's
            own ``e_coli_core`` fixture names it
            ``BIOMASS_Ecoli_core_w_GAM_c0``).
        atp_safe: Filter gap-fill candidates with the FULL ATP test set before
            any solve.  Requires ``core_template``.
        core_template: Core ``MSTemplate`` for the ATP tests.  Required when
            ``atp_safe=True``.
        blacklist: Template reaction ids never offered as candidates.
        biomass_reference: ``model`` (KBDL) or ``superset``.
        missing_t_policy: One of :data:`MISSING_T_POLICIES`.
        fit_only_threshold: Strict fitness bound for ``fitness_only``.
        nonpositive_penalty: ``one`` (KBDL) or ``floor``.
        price_reversals: Price a direction only the superset opens as a
            candidate.
        frac: Fraction of the reference optimum biomass is fixed at.
        loopless: ``auto``, ``True`` or ``False``.
        **method_params: Passed straight to ``MSFitnessWeightedAnalysis``
            (``p_base``, ``p_min``, ``k``, ``t_sig``, ``candidate_factor``,
            ``mass_imbalanced``, ...).  Nothing here renames or reinterprets
            them.

    Returns:
        The class's ``run`` result — ``conditions``, ``annotation_evidence``,
        ``superset`` — plus:

        * per condition, ``n_reactions_discounted``;
        * per edge-evidence row, ``significance`` (``"t"``, ``"fitness_only"``
          or ``None``);
        * ``edges``: the normalised, merged gene mapping actually in force;
        * ``superset.n_unmapped_edge_reactions`` and
          ``superset.unmapped_edge_reactions_sample``;
        * ``superset.atp_leak_flux`` and ``superset.atp_leak_status``, when
          ``atp_safe=True``;
        * ``solver``: the environment report;
        * ``modelseedpy_sha``: the commit the installed modelseedpy was built
          from, or ``"unknown"`` when the install does not record one.

        ``annotation_evidence`` is recomputed from the post-processed conditions,
        so the rollup and the per-condition rows can never disagree.

    Raises:
        ValueError: ``missing_t_policy`` is not one of
            :data:`MISSING_T_POLICIES`; ``atp_safe=True`` without
            ``core_template``; a Complete-leg condition in a model with no genes
            at all.
        ImportError: modelseedpy, or its fitness port, is not installed.

    **The defaults here are KBDL's decided defaults and deliberately differ from
    the library class's.**  ``atp_safe=False`` (class: ``True``),
    ``biomass_reference="model"`` (class: ``"superset"``),
    ``nonpositive_penalty="one"`` (class: ``"floor"``), plus
    ``missing_t_policy="fitness_only"``, which the class has no equivalent of at
    all.  They were decided for KBDL (PRD kbdl-fitness-off-gaa-v1) against the
    defects KBDL issue #44 records — a superset biomass reference makes gap-fill
    appear where the model already grows, and the ATP filter closes its own test
    reaction and changes the output model.  Changing one of these changes KBDL's
    numbers; pass it explicitly rather than editing a default.

    One class default this wrapper deliberately does NOT override is
    ``candidate_factor`` (2.0).  GAA priced candidates at
    ``candidate_scale * 1.0 * gap-fill penalty``, so candidate prices here are
    2x GAA's, and a measured objective comparison against GAA needs
    ``candidate_factor=1.0`` passed through ``method_params``.  That is left to
    the caller because conforming to Jose's port means taking his default, not
    quietly substituting another one; see
    ``agent-io/work-records/kbu-fitness-layer.md``.
    """
    if missing_t_policy not in MISSING_T_POLICIES:
        raise ValueError(
            f"missing_t_policy must be one of {MISSING_T_POLICIES}; "
            f"got {missing_t_policy!r}"
        )
    if atp_safe and core_template is None:
        raise ValueError(
            "atp_safe=True needs core_template: the full ATP test set comes "
            "from MSATPCorrection against a CORE template, and there is no "
            "unfiltered fallback (an unfiltered superset scores every "
            "condition against a network that can make free ATP)"
        )

    # The environment first, and before any cobra.Model is touched: the solver
    # choice must be cobra's default before a model binds an interface, and the
    # dependency forwarding is what makes the ModelSEED biochem path resolvable.
    # check_alias_matching=False on purpose -- fitness builds no model from a
    # KBDL template, so role-alias behaviour cannot reach a fitness number.
    from .ms_reconstruction_utils import (  # imported here: heavy, and optional
        initialize_reconstruction_environment,
        resolve_distribution_provenance,
    )

    environment = initialize_reconstruction_environment(check_alias_matching=False)

    fitness_module = _fitness_module()
    analysis_class = fitness_module.MSFitnessWeightedAnalysis
    normalize_fitness = fitness_module.normalize_fitness

    cobra_model = _cobra_model(model)

    # (2a) normalise the gene mapping BEFORE construction: the class restricts
    # gene_reactions to ids the model already has, so a bare id handed to it
    # straight is dropped in silence -- which is exactly how the fitness values
    # stopped reaching the penalty on the GAA path (KBDL issue #44).
    edges: Optional[Dict[str, Dict[str, List[str]]]] = None
    unmapped: List[str] = []
    if gene_reactions is not None:
        edges, unmapped = normalize_gene_reactions(
            gene_reactions=gene_reactions,
            model_reaction_ids=[rxn.id for rxn in cobra_model.reactions],
        )
        if unmapped:
            logger.warning(
                "%d of %d gene-mapping reaction keys matched no model reaction "
                "and were dropped (first %d: %s)",
                len(unmapped),
                len(gene_reactions),
                min(len(unmapped), UNMAPPED_SAMPLE_SIZE),
                unmapped[:UNMAPPED_SAMPLE_SIZE],
            )

    atp_tests: Optional[List[Dict[str, Any]]] = None
    if atp_safe:
        # Any exception propagates: a fitness run that silently fell back to an
        # unfiltered superset would report gap-fill solutions priced against
        # free ATP as if they were ATP-safe.
        atp_tests = analysis_class.atp_tests_from_correction(
            cobra_model.copy(),
            core_template,
            atp_hydrolysis_id=_atp_hydrolysis_hint(cobra_model),
        )

    analysis = analysis_class(
        model,
        template,
        biomass=biomass,
        atp_safe=atp_safe,
        atp_tests=atp_tests,
        blacklist=list(blacklist),
        gene_reactions=edges,
        biomass_reference=biomass_reference,
        nonpositive_penalty=nonpositive_penalty,
        price_reversals=price_reversals,
        frac=frac,
        loopless=loopless,
        **method_params,
    )
    t_sig = analysis.params["t_sig"]

    # (2b) and (2d): build the fitness each condition is actually solved with,
    # and remember what was done to it so the result can be told the truth.
    specs: Dict[str, Dict[str, Any]] = {}
    adjusted_fitness: Dict[str, Dict[str, Tuple[Any, Any]]] = {}
    admitted_genes: Dict[str, Set[str]] = {}
    complete_legs: Set[str] = set()
    for condition_id, spec in conditions.items():
        normalized = normalize_fitness(spec.get("fitness"))
        adjusted, admitted = _policy_adjusted_fitness(
            normalized=normalized,
            missing_t_policy=missing_t_policy,
            fit_only_threshold=fit_only_threshold,
            t_sig=t_sig,
        )
        if not adjusted and not spec.get("require_fitness", True):
            # The Complete leg. The class returns "no_fitness" on
            # `if not fit and not essential_genes`, so a NEUTRAL row is enough
            # to get past that check -- and a neutral row prices nothing: 0.0 is
            # not a significant negative fitness, so gene_fit_best() is empty
            # and every model reaction keeps p_base. The condition is solved at
            # uniform model prices, which is what "Complete" means.
            adjusted = {
                _neutral_gene(analysis=analysis, model=cobra_model): (0.0, None)
            }
            admitted = set()
            complete_legs.add(condition_id)
        adjusted_fitness[condition_id] = adjusted
        admitted_genes[condition_id] = admitted
        specs[condition_id] = {
            "media": spec.get("media"),
            "fitness": adjusted,
            "essential_genes": spec.get("essential_genes"),
        }

    result = analysis.run(specs)

    # Post-processing: counts and labels only. Nothing here re-solves anything.
    for condition_id, condition in result["conditions"].items():
        if condition_id in complete_legs:
            condition["n_reactions_discounted"] = 0
            condition["edge_evidence"] = []
            continue
        if condition.get("status") in ("no_growth", "no_fitness"):
            # Skipped before pricing: no reaction was ever re-priced.
            condition["n_reactions_discounted"] = 0
        else:
            condition["n_reactions_discounted"] = _count_discounted(
                analysis=analysis, fitness=adjusted_fitness[condition_id]
            )
        _annotate_significance(
            rows=condition.get("edge_evidence", []),
            admitted=admitted_genes[condition_id],
            t_sig=t_sig,
        )
    result["annotation_evidence"] = analysis_class.aggregate_evidence(
        result["conditions"]
    )

    summary = result.setdefault("superset", {})
    summary["n_unmapped_edge_reactions"] = len(unmapped)
    summary["unmapped_edge_reactions_sample"] = unmapped[:UNMAPPED_SAMPLE_SIZE]
    if atp_safe:
        flux, status = _atp_leak(
            analysis=analysis, atp_tests=atp_tests, compartment=analysis.compartment
        )
        summary["atp_leak_flux"] = flux
        summary["atp_leak_status"] = status

    # The mapping actually in force: what was passed when a mapping was given,
    # and the model's own gene rules (as the class read them) when it was not.
    result["edges"] = (
        edges
        if edges is not None
        else {
            rid: {gene: list(sources) for gene, sources in genes.items()}
            for rid, genes in analysis.edges.items()
        }
    )
    result["solver"] = environment
    result["modelseedpy_sha"] = resolve_distribution_provenance("modelseedpy")[1]
    return result
