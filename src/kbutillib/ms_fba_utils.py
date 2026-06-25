"""KBase model utilities for constraint-based metabolic modeling.

Energy/redox/mass loop (EGC) detection utilities
-------------------------------------------------
The five EGC-detection helpers (``build_full_model_from_template``,
``add_probe_reaction``, ``minimize_active_reactions``,
``enumerate_alternative_reaction_sets``, and ``find_flux_loops``) rely on the
following ModelSEEDpy APIs.  No version SHA is pinned in pyproject.toml; these
are the stable public surfaces used as of modelseedpy 0.4.x.

    from modelseedpy.core.mstemplate import MSTemplate, MSTemplateReaction
    MSTemplateReaction.to_reaction(cobra_model, index)
        Builds a cobra.Reaction from the template reaction in compartment
        ``{reaction.compartment}{index}``; carries lower_bound/upper_bound
        from the template directionality.

    from modelseedpy.core.msmodelutl import MSModelUtil
    MSModelUtil.get(cobra_model)
        Wraps a cobra.Model in an MSModelUtil (singleton per model object).
    MSModelUtil.find_reaction(stoichiometry)
        Returns [reaction, direction_int] where direction_int is 1 (forward)
        or -1 (reverse), or None if no match exists.
    MSModelUtil.msid_hash()
        Returns {msid: [cobra.Metabolite, ...]} for every metabolite in the
        model whose annotation contains a ModelSEED compound id.
    MSModelUtil.assign_reliability_scores_to_reactions()
        Returns {rxn_id: {">": float, "<": float}} reliability scores.
    MSModelUtil.is_core(rxn)
        Returns True if the reaction is in the core reaction set.

    model.pkgmgr.getpkg("ReactionUsePkg").build_package(filter_dict)
        Adds fu/ru binary variables + constraints for the reactions in
        filter_dict = {rxn_id: ">"|"<"|"="}.  Variables are accessible as
        model.pkgmgr.getpkg("ReactionUsePkg").variables["fu"] and ["ru"].

    from optlang.symbolics import Zero
        The zero symbolic constant used when constructing LP objectives.
"""

import logging
import pickle
import time
from typing import Any, Dict
import pandas as pd
import re
import json

from cobra.flux_analysis import flux_variability_analysis
from cobra.flux_analysis import pfba

from .kb_model_utils import KBModelUtils

logger = logging.getLogger(__name__)

# ── EGC probe catalog ────────────────────────────────────────────────────────
# Module-level constant keyed by group.  Each entry is a list of probe specs:
#   {"name": str, "stoichiometry": {msid: coef}, "seed_annotation": str}
# stoichiometry coefficients follow the cobra convention: negative = reactant.
# Compartment suffixes are added at run-time by add_probe_reaction.

EGC_PROBE_CATALOG: Dict[str, list] = {
    "atp": [
        {
            "name": "atp_hydrolysis",
            "stoichiometry": {
                "cpd00002": -1,  # ATP
                "cpd00001": -1,  # H2O
                "cpd00008": 1,   # ADP
                "cpd00009": 1,   # Pi
                "cpd00067": 1,   # H+
            },
            "seed_annotation": "rxn00062",
        }
    ],
    # Redox probes: reduced -> oxidized + H2 [+ H+ as needed for charge balance]
    # Gated behind enable_redox_probes.  Each probe specifies:
    #   reduced msid, oxidized msid, cpd00067 (H+) coefficient (0 or 1),
    #   H2 msid = cpd11749 (dihydrogen in ModelSEED).
    # cpd00067 coefficient on product side handles charge balance:
    #   NADH (charge -1) -> NAD (charge -1) + H2 (charge 0)  → charge balanced
    #   NADPH (charge -2) -> NADP (charge -2) + H2 (charge 0) → balanced
    # Couples with unresolvable compound ids are skipped at runtime.
    "redox": [
        {
            "name": "nadh_drain",
            "stoichiometry": {
                "cpd00004": -1,   # NADH
                "cpd00003": 1,    # NAD+
                "cpd11749": 1,    # H2
            },
            "seed_annotation": "PROBE_NADH",
        },
        {
            "name": "nadph_drain",
            "stoichiometry": {
                "cpd00005": -1,   # NADPH
                "cpd00006": 1,    # NADP+
                "cpd11749": 1,    # H2
            },
            "seed_annotation": "PROBE_NADPH",
        },
        {
            "name": "fadh2_drain",
            "stoichiometry": {
                "cpd00982": -1,   # FADH2 (cpd00982 in ModelSEED)
                "cpd00015": 1,    # FAD
                "cpd11749": 1,    # H2
                "cpd00067": 1,    # H+ (charge: FADH2=-2, FAD=-2, H2=0 → need +2H but H2=2H → balanced as is)
            },
            "seed_annotation": "PROBE_FADH2",
        },
        {
            "name": "fdred_drain",
            "stoichiometry": {
                "cpd11620": -1,   # ferredoxin reduced (cpd11620)
                "cpd11621": 1,    # ferredoxin oxidized (cpd11621)
                "cpd11749": 1,    # H2
            },
            "seed_annotation": "PROBE_FDRED",
        },
        {
            "name": "ubiquinol_drain",
            "stoichiometry": {
                "cpd15561": -1,   # ubiquinol (cpd15561)
                "cpd15560": 1,    # ubiquinone (cpd15560)
                "cpd11749": 1,    # H2
                "cpd00067": 1,    # H+ for charge balance (QH2 neutral, Q neutral → +2H, H2=2H)
            },
            "seed_annotation": "PROBE_QH2",
        },
        {
            "name": "gsh_drain",
            "stoichiometry": {
                "cpd00111": -2,   # 2 GSH
                "cpd00154": 1,    # GSSG
                "cpd11749": 1,    # H2
            },
            "seed_annotation": "PROBE_GSH",
        },
        {
            "name": "trxred_drain",
            "stoichiometry": {
                "cpd12689": -1,   # thioredoxin reduced (cpd12689)
                "cpd12690": 1,    # thioredoxin oxidized (cpd12690)
                "cpd11749": 1,    # H2
            },
            "seed_annotation": "PROBE_TRXRED",
        },
    ],
    # Mass sinks: exactly these four metabolites, irreversible drain
    "mass": [
        {
            "name": "co2_sink",
            "stoichiometry": {"cpd00011": -1},  # CO2
            "seed_annotation": "PROBE_CO2",
        },
        {
            "name": "acetate_sink",
            "stoichiometry": {"cpd00029": -1},  # acetate
            "seed_annotation": "PROBE_ACETATE",
        },
        {
            "name": "formate_sink",
            "stoichiometry": {"cpd00047": -1},  # formate
            "seed_annotation": "PROBE_FORMATE",
        },
        {
            "name": "nh3_sink",
            "stoichiometry": {"cpd00013": -1},  # NH3
            "seed_annotation": "PROBE_NH3",
        },
    ],
}
# "all" is the union; built lazily to avoid duplicates if catalog is modified
EGC_PROBE_CATALOG["all"] = (
    EGC_PROBE_CATALOG["atp"]
    + EGC_PROBE_CATALOG["redox"]
    + EGC_PROBE_CATALOG["mass"]
)

_TOL = 1e-6   # active threshold: |flux| > _TOL
_ZERO = 1e-9  # zero threshold: |flux| <= _ZERO
_SUPPORT_CAP = 500  # max ReactionUse filter size


# ── Module-level standalone helpers (callable from tests without auth) ────────


def _safe_reliability_scores(mdlutl):
    """Return reliability scores, falling back to empty defaults if biochem DB unavailable."""
    try:
        return mdlutl.assign_reliability_scores_to_reactions()
    except Exception as e:
        logger.warning(f"assign_reliability_scores_to_reactions unavailable ({e}); using defaults")
        scores = {}
        for rxn in mdlutl.model.reactions:
            scores[rxn.id] = {">": 0.0, "<": 0.0}
        return scores


def _safe_is_core(mdlutl, rxn):
    """Return is_core, falling back to False if biochem DB unavailable."""
    try:
        return mdlutl.is_core(rxn)
    except Exception:
        return False


def _strip_reaction_use_pkg(model_cobra, pkgmgr):
    """Remove all fu_/ru_ variables and constraints from the model.

    Tries the clean ``ReactionUsePkg.clear()`` path first; falls back to
    removing variables/constraints by name prefix through optlang.
    """
    try:
        pkg = pkgmgr.getpkg("ReactionUsePkg")
        if hasattr(pkg, "clear"):
            pkg.clear()
            return
    except Exception:
        pass
    # Fallback: remove by name prefix
    to_remove_vars = [
        v for v in model_cobra.variables if v.name.startswith(("fu_", "ru_"))
    ]
    to_remove_cons = [
        c for c in model_cobra.constraints if c.name.startswith(("fu_", "ru_", "exclusion"))
    ]
    model_cobra.remove_cons_vars(to_remove_vars + to_remove_cons)


def build_full_model_from_template_standalone(template, index="0"):
    """Build a closed cobra.Model from an MSTemplate, wrapped as MSModelUtil.

    Every template reaction is included via MSTemplateReaction.to_reaction.
    Template directionality is preserved (the defect surface).  Any reaction
    whose id starts with EX_, DM_, SK_, or bio has lb=ub=0 (boundary guard).

    Parameters
    ----------
    template : MSTemplate
        A loaded ModelSEED template object.
    index : str, optional
        Compartment index appended by to_reaction (default "0").

    Returns
    -------
    MSModelUtil
        Wrapped closed model.
    """
    import cobra
    from modelseedpy.core.msmodelutl import MSModelUtil

    cobra_model = cobra.Model(f"template_closed_{getattr(template, 'id', 'model')}")
    for tmpl_rxn in template.reactions:
        try:
            rxn = tmpl_rxn.to_reaction(cobra_model, index)
            cobra_model.add_reactions([rxn])
        except Exception as e:
            logger.warning(f"build_full_model_from_template: skipping {tmpl_rxn.id}: {e}")
    # Zero boundary/maintenance reactions
    boundary_prefixes = ("EX_", "DM_", "SK_", "bio")
    for rxn in cobra_model.reactions:
        if any(rxn.id.startswith(p) for p in boundary_prefixes):
            rxn.lower_bound = 0
            rxn.upper_bound = 0
    return MSModelUtil.get(cobra_model)


def add_probe_reaction_standalone(mdlutl, probe, compartment="c0"):
    """Add or reuse a probe reaction on the wrapped model.

    Generalizes MSModelUtil.add_atp_hydrolysis to an arbitrary probe spec.

    Parameters
    ----------
    mdlutl : MSModelUtil
        Wrapped cobra.Model to add the probe to.
    probe : dict
        Probe spec with keys ``name``, ``stoichiometry`` (msid→coef),
        ``seed_annotation``.
    compartment : str, optional
        Target compartment (default "c0").

    Returns
    -------
    dict
        ``{"reaction": cobra.Reaction, "direction": ">", "new": bool}``
        new=False when an existing reaction was reused.
    """
    from modelseedpy.core.msmodelutl import MSModelUtil

    name = probe["name"]
    raw_stoich = probe["stoichiometry"]
    seed_ann = probe["seed_annotation"]

    # Resolve msids to cobra.Metabolite objects in the target compartment
    id_hash = mdlutl.msid_hash()
    stoichiometry = {}
    for msid, coef in raw_stoich.items():
        if msid not in id_hash:
            # metabolite not in model; add a new one
            import cobra as _cobra
            new_met = _cobra.Metabolite(
                f"{msid}_{compartment}",
                name=msid,
                compartment=compartment,
            )
            new_met.annotation = {"seed.compound": msid}
            stoichiometry[new_met] = coef
        else:
            # pick the metabolite in the right compartment
            met_in_comp = [m for m in id_hash[msid] if m.compartment == compartment]
            if met_in_comp:
                stoichiometry[met_in_comp[0]] = coef
            else:
                # use first available metabolite (different compartment)
                stoichiometry[id_hash[msid][0]] = coef

    # Check for an existing matching reaction in the FORWARD direction.
    # find_reaction returns [rxn, dir_int] where dir_int=1 means forward match
    # (query stoichiometry matches rxn's own metabolite coefficients exactly),
    # dir_int=-1 means reverse match. Only reuse on forward match so the probe
    # always reads in the expected direction.
    existing = mdlutl.find_reaction(stoichiometry)
    if existing is not None:
        rxn, dir_int = existing
        if dir_int == 1:
            return {"reaction": rxn, "direction": ">", "new": False}

    # Create the probe reaction
    probe_id = f"PROBE_{name}_{compartment}"
    if probe_id in [r.id for r in mdlutl.model.reactions]:
        # Already present (e.g. second call) — reuse
        rxn = mdlutl.model.reactions.get_by_id(probe_id)
        return {"reaction": rxn, "direction": ">", "new": False}

    import cobra as _cobra
    probe_rxn = _cobra.Reaction(
        probe_id,
        name=f"{name} probe [{compartment}]",
        lower_bound=0,
        upper_bound=1000,
    )
    probe_rxn.annotation["seed.reaction"] = seed_ann
    probe_rxn.add_metabolites(stoichiometry)
    mdlutl.model.add_reactions([probe_rxn])
    return {"reaction": probe_rxn, "direction": ">", "new": True}


def minimize_active_reactions_standalone(
    mdlutl,
    objective=None,
    active_filter=None,
    fraction_of_optimum=1.0,
    tol=_TOL,
    zero_tol=_ZERO,
):
    """Return the count-minimal set of active reactions supporting an objective.

    Phase 1 (if active_filter is None): LP-maximize the objective, then
    LP-minimize total flux to get the pFBA-style support.  Phase 2: introduce
    fu/ru binary variables only for the support reactions (capped at
    _SUPPORT_CAP by |flux|) and minimize Σ(fu+ru) via MILP.

    Parameters
    ----------
    mdlutl : MSModelUtil
        Wrapped model (will be mutated temporarily; caller must strip
        ReactionUsePkg afterward if they want a clean model).
    objective : str or None
        Objective string (e.g. "MAX{PROBE_atp_hydrolysis_c0}").  Ignored when
        active_filter is provided.
    active_filter : dict or None
        Pre-computed {rxn_id: direction ">" or "<"}.
    fraction_of_optimum : float
        Pin the objective at this fraction of its max value (default 1.0).
    tol : float
        Activity threshold (default 1e-6).
    zero_tol : float
        Zero threshold (default 1e-9).

    Returns
    -------
    dict
        ``{"reactions": [...], "size": int, "solution": cobra.Solution}``
        Each reaction entry: {id, direction, flux, reliability_score, is_core}.
    """
    import time as _time
    from optlang.symbolics import Zero

    t0 = _time.time()
    model = mdlutl.model

    if active_filter is None:
        if objective is None:
            raise ValueError("minimize_active_reactions: need objective or active_filter")
        # Phase 1a: LP-maximize objective
        original_obj = model.objective
        obj_str = objective
        # Set objective via ObjectivePkg if available; else set directly
        try:
            mdlutl.pkgmgr.getpkg("ObjectivePkg").build_package(obj_str)
        except Exception:
            _set_objective_simple(model, obj_str)

        sol_max = model.optimize()
        if sol_max.status != "optimal":
            logger.warning(f"minimize_active_reactions: maximization infeasible ({sol_max.status})")
            return {"reactions": [], "size": 0, "solution": sol_max}

        vmax = sol_max.objective_value
        if vmax <= tol:
            return {"reactions": [], "size": 0, "solution": sol_max}

        # Phase 1b: pin objective at fraction_of_optimum * vmax
        pin_lb = vmax * fraction_of_optimum
        # Set minimize total flux with the objective pinned
        _pin_objective(model, obj_str, pin_lb)

        # LP-minimize total flux
        pfba_terms = []
        for rxn in model.reactions:
            pfba_terms.append(rxn.forward_variable)
            pfba_terms.append(rxn.reverse_variable)
        min_flux_obj = model.problem.Objective(sum(pfba_terms), direction="min")
        orig_obj = model.objective
        model.objective = min_flux_obj
        sol_pfba = model.optimize()
        model.objective = orig_obj
        # NOTE: do NOT unpin objective here — keep pin through MILP phase so
        # the probe (or any other pinned objective reaction) still forces flux
        # through the active set during the MILP minimization.

        if sol_pfba.status != "optimal":
            _unpin_objective(model, obj_str)
            logger.warning(f"minimize_active_reactions: pFBA infeasible ({sol_pfba.status})")
            return {"reactions": [], "size": 0, "solution": sol_pfba}

        # Build active filter from pFBA solution
        active_filter = {}
        for rxn in model.reactions:
            flux = sol_pfba.fluxes.get(rxn.id, 0.0)
            if flux > tol:
                active_filter[rxn.id] = ">"
            elif flux < -tol:
                active_filter[rxn.id] = "<"
        current_solution = sol_pfba
        unpin_after_milp = True
    else:
        current_solution = None
        unpin_after_milp = False

    if not active_filter:
        if unpin_after_milp:
            _unpin_objective(model, obj_str)
        return {"reactions": [], "size": 0, "solution": current_solution}

    # Cap at _SUPPORT_CAP by |flux| if active_filter was provided without solution
    if current_solution is not None and len(active_filter) > _SUPPORT_CAP:
        sorted_rxns = sorted(
            active_filter.keys(),
            key=lambda rid: abs(current_solution.fluxes.get(rid, 0.0)),
            reverse=True,
        )
        capped = sorted_rxns[:_SUPPORT_CAP]
        logger.info(
            f"minimize_active_reactions: support ({len(active_filter)}) exceeds cap "
            f"({_SUPPORT_CAP}); using top-{_SUPPORT_CAP} by |flux|"
        )
        active_filter = {rid: active_filter[rid] for rid in capped}

    t_phase2 = _time.time()
    logger.info(f"minimize_active_reactions: phase-2 MILP on {len(active_filter)} reactions")

    # Phase 2: build fu/ru binaries and minimize reaction count.
    # Zero all reactions NOT in active_filter so the solver must route flux
    # through the active set (mirrors binary_check_gapfilling_solution's
    # knockout_gf_reactions_outside_solution pattern).
    inactive_bounds = {}  # rxn_id -> (orig_lb, orig_ub)
    for rxn in model.reactions:
        if rxn.id not in active_filter:
            # Only zero reactions that can naturally carry zero flux (lb <= 0).
            # Reactions with lb > 0 are externally pinned (e.g. the probe)
            # and must be left as-is so they keep providing driving force.
            if rxn.lower_bound > 0:
                continue
            if rxn.lower_bound != 0 or rxn.upper_bound != 0:
                inactive_bounds[rxn.id] = (rxn.lower_bound, rxn.upper_bound)
                # Set lb=0 first (always valid since lb <= 0 < ub or lb=ub=0)
                # then ub=0.
                rxn.lower_bound = 0
                rxn.upper_bound = 0

    ru_pkg = mdlutl.pkgmgr.getpkg("ReactionUsePkg")
    ru_pkg.build_package(active_filter)

    obj_coef = {}
    for rxn_id, direction in active_filter.items():
        if direction in (">", "=") and rxn_id in ru_pkg.variables["fu"]:
            obj_coef[ru_pkg.variables["fu"][rxn_id]] = 1
        if direction in ("<", "=") and rxn_id in ru_pkg.variables["ru"]:
            obj_coef[ru_pkg.variables["ru"][rxn_id]] = 1

    min_count_obj = model.problem.Objective(Zero, direction="min")
    orig_obj = model.objective
    model.objective = min_count_obj
    min_count_obj.set_linear_coefficients(obj_coef)
    sol_milp = model.optimize()
    model.objective = orig_obj

    # Unpin objective now that MILP is done
    if unpin_after_milp:
        _unpin_objective(model, obj_str)

    # Restore bounds of zeroed reactions
    for rxn_id, (lb, ub) in inactive_bounds.items():
        rxn = model.reactions.get_by_id(rxn_id)
        if lb > rxn.upper_bound:
            rxn.upper_bound = ub
            rxn.lower_bound = lb
        else:
            rxn.lower_bound = lb
            rxn.upper_bound = ub

    t_done = _time.time()
    logger.info(
        f"minimize_active_reactions: MILP done in {t_done - t_phase2:.2f}s "
        f"(total {t_done - t0:.2f}s), status={sol_milp.status}"
    )

    if sol_milp.status != "optimal":
        logger.warning(f"minimize_active_reactions: MILP infeasible ({sol_milp.status})")
        return {"reactions": [], "size": 0, "solution": sol_milp}

    # Collect reactions that are active in the MILP solution.
    # Use raw flux values from sol_milp (works for both MILP and LP solvers).
    # For solvers that support binary MILPs, the fu/ru primal > 0.5 test is
    # also applicable; for LP-relaxation solvers (GLPK), rely on flux values.
    scores = _safe_reliability_scores(mdlutl)
    result_rxns = []
    for rxn_id, direction in active_filter.items():
        flux_val = sol_milp.fluxes.get(rxn_id, 0.0)
        fu_primal = (
            ru_pkg.variables["fu"][rxn_id].primal
            if rxn_id in ru_pkg.variables["fu"]
            else 0.0
        )
        ru_primal = (
            ru_pkg.variables["ru"][rxn_id].primal
            if rxn_id in ru_pkg.variables["ru"]
            else 0.0
        )
        # Active check: prefer binary primal > 0.5 (MILP solver), fall back to flux
        fu_active = direction in (">", "=") and (fu_primal > 0.5 or flux_val > tol)
        ru_active = direction in ("<", "=") and (ru_primal > 0.5 or flux_val < -tol)
        if fu_active or ru_active:
            rxn = model.reactions.get_by_id(rxn_id)
            dir_used = ">" if (flux_val > 0 and fu_active) or (not ru_active) else "<"
            sc = scores.get(rxn_id, {}).get(dir_used, 0.0)
            result_rxns.append(
                {
                    "id": rxn_id,
                    "direction": dir_used,
                    "flux": float(flux_val),
                    "reliability_score": float(sc),
                    "is_core": _safe_is_core(mdlutl, rxn),
                }
            )

    return {"reactions": result_rxns, "size": len(result_rxns), "solution": sol_milp}


def enumerate_alternative_reaction_sets_standalone(
    mdlutl,
    solution,
    tol=_TOL,
    zero_tol=_ZERO,
):
    """For each active reaction, report alternatives, coupled reactions, and essentiality.

    Generalizes MSModelUtil.analyze_minimal_reaction_set.

    Parameters
    ----------
    mdlutl : MSModelUtil
        Wrapped model.  Objective will be temporarily replaced.
    solution : dict
        Result from minimize_active_reactions_standalone containing
        ``"reactions"`` list.
    tol : float
        Activity threshold.
    zero_tol : float
        Zero threshold.

    Returns
    -------
    dict
        Per-reaction perturbation map: {rxn_id: {alternatives, coupled, essential,
        direction, flux, reliability_score, is_core, equation, failed}}.
    """
    from optlang.symbolics import Zero

    model = mdlutl.model
    rxn_list = solution.get("reactions", [])
    if not rxn_list:
        return {}

    # Categorize all model reactions as initially-zero (for alternative detection)
    active_ids = {r["id"] for r in rxn_list}
    initial_zero = {}
    for rxn in model.reactions:
        if rxn.id in active_ids:
            continue
        initial_zero[rxn.id] = {">": True, "<": True}

    # Build minimal-deviation objective: minimize sum(fwd+rev) over zero reactions
    obj_coef = {}
    for rxn in model.reactions:
        if rxn.id not in active_ids:
            obj_coef[rxn.forward_variable] = 1
            obj_coef[rxn.reverse_variable] = 1

    original_objective = model.objective
    min_dev_obj = model.problem.Objective(Zero, direction="min")
    model.objective = min_dev_obj
    min_dev_obj.set_linear_coefficients(obj_coef)

    scores = _safe_reliability_scores(mdlutl)
    output = {}

    for item in rxn_list:
        rxn_id = item["id"]
        direction = item["direction"]
        rxn = model.reactions.get_by_id(rxn_id)
        sc = item.get("reliability_score", scores.get(rxn_id, {}).get(direction, 0.0))
        is_c = item.get("is_core", _safe_is_core(mdlutl, rxn))

        # Knock out the reaction in the direction it is used.
        # Save original bounds and zero the used direction.
        orig_lb = rxn.lower_bound
        orig_ub = rxn.upper_bound
        result = {
            "alternatives": [],
            "coupled": [],
            "essential": False,
            "failed": False,
            "direction": direction,
            "flux": item.get("flux", 0.0),
            "reliability_score": float(sc),
            "is_core": is_c,
            "equation": rxn.build_reaction_string(use_metabolite_names=True),
        }

        # Short-circuit: if the reaction is pinned (lb > 0 for forward, ub < 0
        # for reverse), knocking it out contradicts its lower bound → essential
        # without needing to solve.
        if direction == ">" and orig_lb > zero_tol:
            result["essential"] = True
            output[rxn_id] = result
            continue
        if direction == "<" and orig_ub < -zero_tol:
            result["essential"] = True
            output[rxn_id] = result
            continue

        if direction == ">":
            # Zero forward: set lb=min(orig_lb,0) first to avoid lb>ub=0 conflict
            if rxn.lower_bound > 0:
                rxn.lower_bound = 0
            rxn.upper_bound = 0
        else:
            # Zero reverse: set ub=max(orig_ub,0) first, then lb=0
            if rxn.upper_bound < 0:
                rxn.upper_bound = 0
            rxn.lower_bound = 0

        ko_sol = model.optimize()

        if ko_sol.status not in ("optimal",):
            # GLPK may return "infeasible", "undefined", or other non-optimal
            # status when the knockout makes the problem infeasible.
            result["essential"] = True
            result["failed"] = True
        else:
            # Check for coupled reactions (other active ones that dropped to zero)
            for other in rxn_list:
                if other["id"] == rxn_id:
                    continue
                other_flux = ko_sol.fluxes.get(other["id"], 0.0)
                if abs(other_flux) <= zero_tol:
                    result["coupled"].append([other["id"], other["direction"]])
            # Check for alternatives (initially-zero reactions that now carry flux)
            for zid in initial_zero:
                zflux = ko_sol.fluxes.get(zid, 0.0)
                if zflux > tol and ">" in initial_zero[zid]:
                    result["alternatives"].append([zid, ">"])
                elif zflux < -tol and "<" in initial_zero[zid]:
                    result["alternatives"].append([zid, "<"])

        # Restore original bounds in safe order.
        # If orig_lb > current ub, must expand ub first; otherwise can set lb first.
        if orig_lb > rxn.upper_bound:
            rxn.upper_bound = orig_ub
            rxn.lower_bound = orig_lb
        else:
            rxn.lower_bound = orig_lb
            rxn.upper_bound = orig_ub

        output[rxn_id] = result

    model.objective = original_objective
    return output


def find_flux_loops_standalone(
    mdlutl,
    objective="all",
    compartment="c0",
    max_loops_per_probe=5,
    tol=_TOL,
    zero_tol=_ZERO,
    fraction_of_optimum=1.0,
    flux_min_threshold=_TOL,
    enable_redox_probes=True,
    log_fn=None,
):
    """Find energy/redox/mass loops (EGCs) in a closed metabolic model.

    Operates on an already-built closed model (see build_full_model_from_template
    or pass a cobra.Model stand-in wrapped via MSModelUtil.get).

    Parameters
    ----------
    mdlutl : MSModelUtil
        Closed model (no open boundary fluxes).
    objective : str, cobra.Reaction, or None
        "all", "atp", "redox", "mass" (group names), or a single cobra.Reaction.
        None is equivalent to "all".
    compartment : str
        Target compartment for probes (default "c0").
    max_loops_per_probe : int
        Maximum distinct loops to enumerate per probe (default 5).
    tol : float
        Activity threshold (default 1e-6).
    zero_tol : float
        Zero threshold (default 1e-9).
    fraction_of_optimum : float
        Fraction at which to pin the probe flux (default 1.0).
    flux_min_threshold : float
        Minimum probe flux to consider non-trivial (default 1e-6).
    enable_redox_probes : bool
        Include redox probes (default True).
    log_fn : callable or None
        Logging function (default: logger.info).

    Returns
    -------
    dict
        {probe_name: [loop_record, ...], ...}  An empty list means no loop.
        Each loop_record has exactly: target_flux, size, reactions.
        Each reaction entry has: id, direction_used, flux, equation,
        reliability_score, is_core, alternatives, coupled, essential.
    """
    import cobra as _cobra
    from optlang.symbolics import Zero

    if log_fn is None:
        log_fn = logger.info

    model = mdlutl.model

    # ── Snapshot all reaction bounds before any modification ──────────────
    bounds_snapshot = {r.id: (r.lower_bound, r.upper_bound) for r in model.reactions}

    # ── Resolve probe list ────────────────────────────────────────────────
    if isinstance(objective, _cobra.Reaction):
        probes_to_run = [{"_custom": objective}]
    else:
        group = objective if objective is not None else "all"
        if group == "all":
            catalog_groups = ["atp", "redox", "mass"]
        elif group in EGC_PROBE_CATALOG:
            catalog_groups = [group]
        else:
            raise ValueError(f"find_flux_loops: unknown objective group '{group}'")
        probes_to_run = []
        for g in catalog_groups:
            if g == "redox" and not enable_redox_probes:
                continue
            probes_to_run.extend(EGC_PROBE_CATALOG[g])

    results = {}

    for probe_spec in probes_to_run:
        # ── Add probe ─────────────────────────────────────────────────────
        if "_custom" in probe_spec:
            custom_rxn = probe_spec["_custom"]
            # If the id already exists in the model, check stoichiometry match
            existing_ids = [r.id for r in model.reactions]
            if custom_rxn.id in existing_ids:
                existing_rxn = model.reactions.get_by_id(custom_rxn.id)
                if existing_rxn.metabolites != custom_rxn.metabolites:
                    raise ValueError(
                        f"find_flux_loops: custom probe id '{custom_rxn.id}' collides "
                        f"with existing reaction with different stoichiometry"
                    )
                probe_info = {"reaction": existing_rxn, "direction": ">", "new": False}
            else:
                model.add_reactions([custom_rxn])
                probe_info = {"reaction": custom_rxn, "direction": ">", "new": True}
            probe_name = custom_rxn.id
        else:
            probe_info = add_probe_reaction_standalone(mdlutl, probe_spec, compartment)
            probe_name = probe_spec["name"]

        probe_rxn = probe_info["reaction"]
        probe_id = probe_rxn.id
        probe_direction = probe_info["direction"]
        probe_is_new = probe_info["new"]
        results[probe_name] = []

        # ── Per-probe blocking state (for integer-cut restore) ─────────────
        blocked_bounds = {}  # rxn_id -> list of (attr, original_value)

        # ── LP-maximize probe ─────────────────────────────────────────────
        _set_objective_simple(model, f"MAX{{{probe_id}}}")
        sol_max = model.optimize()

        if sol_max.status != "optimal" or sol_max.objective_value <= tol:
            log_fn(f"find_flux_loops [{probe_name}]: vmax={getattr(sol_max, 'objective_value', 0):.2e} <= tol — no loop")
            _cleanup_probe(model, mdlutl, probe_rxn, probe_is_new, blocked_bounds, bounds_snapshot, probe_id)
            continue

        vmax = sol_max.objective_value
        log_fn(f"find_flux_loops [{probe_name}]: vmax={vmax:.4f}, enumerating loops")

        loop_count = 0
        loop_hashes_seen = set()

        while loop_count < max_loops_per_probe:
            # Re-maximize to get current best vmax (after blocking)
            _set_objective_simple(model, f"MAX{{{probe_id}}}")
            sol_max2 = model.optimize()
            if sol_max2.status != "optimal" or sol_max2.objective_value <= tol:
                break
            vmax = sol_max2.objective_value

            # Pin probe at fraction_of_optimum * vmax
            pin_lb = max(vmax * fraction_of_optimum, flux_min_threshold)
            probe_rxn.lower_bound = pin_lb

            # LP-minimize total flux (pFBA-style support reduction)
            pfba_terms = []
            for rxn in model.reactions:
                pfba_terms.append(rxn.forward_variable)
                pfba_terms.append(rxn.reverse_variable)
            min_flux_obj = model.problem.Objective(sum(pfba_terms), direction="min")
            model.objective = min_flux_obj
            sol_pfba = model.optimize()
            # Keep probe pinned for MILP phase below (unpin after minimize)

            if sol_pfba.status != "optimal":
                probe_rxn.lower_bound = 0  # unpin
                log_fn(f"find_flux_loops [{probe_name}]: pFBA infeasible after pinning — stopping")
                break

            # Build active filter from pFBA support (excluding the probe itself)
            active_filter = {}
            for rxn in model.reactions:
                if rxn.id == probe_id:
                    continue
                flux = sol_pfba.fluxes.get(rxn.id, 0.0)
                if flux > tol:
                    active_filter[rxn.id] = ">"
                elif flux < -tol:
                    active_filter[rxn.id] = "<"

            if not active_filter:
                probe_rxn.lower_bound = 0  # unpin
                break

            # Cap if necessary
            if len(active_filter) > _SUPPORT_CAP:
                sorted_ids = sorted(
                    active_filter,
                    key=lambda rid: abs(sol_pfba.fluxes.get(rid, 0.0)),
                    reverse=True,
                )
                log_fn(
                    f"find_flux_loops [{probe_name}]: support cap applied "
                    f"({len(active_filter)} -> {_SUPPORT_CAP})"
                )
                active_filter = {rid: active_filter[rid] for rid in sorted_ids[:_SUPPORT_CAP]}

            # Phase 2: minimize reaction count (probe stays pinned so binaries are valid)
            t0 = time.time()
            min_result = minimize_active_reactions_standalone(
                mdlutl,
                active_filter=active_filter,
                fraction_of_optimum=fraction_of_optimum,
                tol=tol,
                zero_tol=zero_tol,
            )
            log_fn(
                f"find_flux_loops [{probe_name}]: minimize_active_reactions "
                f"size={min_result['size']} in {time.time()-t0:.2f}s"
            )

            if not min_result["reactions"]:
                _strip_reaction_use_pkg(model, mdlutl.pkgmgr)
                probe_rxn.lower_bound = 0  # unpin
                break

            # Dedup by direction-set hash
            loop_hash = frozenset(
                (r["id"], r["direction"]) for r in min_result["reactions"]
            )
            if loop_hash in loop_hashes_seen:
                _strip_reaction_use_pkg(model, mdlutl.pkgmgr)
                probe_rxn.lower_bound = 0  # unpin
                # Block this set anyway and try again
                _apply_integer_cut(model, min_result["reactions"], blocked_bounds)
                loop_count += 1
                continue
            loop_hashes_seen.add(loop_hash)

            # Perturbation scan (probe still pinned so knockout tests are meaningful)
            t1 = time.time()
            perturb = enumerate_alternative_reaction_sets_standalone(
                mdlutl, min_result, tol=tol, zero_tol=zero_tol
            )
            log_fn(
                f"find_flux_loops [{probe_name}]: enumerate_alternatives "
                f"in {time.time()-t1:.2f}s"
            )

            # Now unpin the probe before stripping and integer-cut
            probe_rxn.lower_bound = 0

            # Strip ReactionUse vars between loops
            _strip_reaction_use_pkg(model, mdlutl.pkgmgr)

            # Assemble reaction records
            rxn_records = []
            for r in min_result["reactions"]:
                rid = r["id"]
                rxn_obj = model.reactions.get_by_id(rid)
                p = perturb.get(rid, {})
                rxn_records.append(
                    {
                        "id": rid,
                        "direction_used": r["direction"],
                        "flux": r["flux"],
                        "equation": rxn_obj.build_reaction_string(use_metabolite_names=True),
                        "reliability_score": r["reliability_score"],
                        "is_core": r["is_core"],
                        "alternatives": p.get("alternatives", []),
                        "coupled": p.get("coupled", []),
                        "essential": p.get("essential", False),
                    }
                )

            loop_record = {
                "target_flux": float(vmax),
                "size": min_result["size"],
                "reactions": rxn_records,
            }
            results[probe_name].append(loop_record)
            loop_count += 1

            # Integer-cut: block the minimal set
            _apply_integer_cut(model, min_result["reactions"], blocked_bounds)

        # ── Clean up this probe ────────────────────────────────────────────
        _strip_reaction_use_pkg(model, mdlutl.pkgmgr)
        _cleanup_probe(model, mdlutl, probe_rxn, probe_is_new, blocked_bounds, bounds_snapshot, probe_id)

    # Verify model state matches snapshot
    for rxn in model.reactions:
        if rxn.id in bounds_snapshot:
            orig_lb, orig_ub = bounds_snapshot[rxn.id]
            if rxn.lower_bound != orig_lb or rxn.upper_bound != orig_ub:
                logger.warning(
                    f"find_flux_loops: bound mismatch on {rxn.id} after cleanup "
                    f"({rxn.lower_bound},{rxn.upper_bound}) vs ({orig_lb},{orig_ub})"
                )
                rxn.lower_bound = orig_lb
                rxn.upper_bound = orig_ub

    return results


# ── Private helpers ───────────────────────────────────────────────────────────

def _set_objective_simple(model, obj_str):
    """Set model objective from 'MAX{rxn_id}' or 'MIN{rxn_id}' strings."""
    m = re.match(r"(MAX|MIN)\{(.+)\}", obj_str)
    if m:
        direction = "max" if m.group(1) == "MAX" else "min"
        rxn_id = m.group(2)
        rxn = model.reactions.get_by_id(rxn_id)
        model.objective = rxn
        model.objective.direction = direction
    else:
        raise ValueError(f"_set_objective_simple: cannot parse '{obj_str}'")


def _pin_objective(model, obj_str, lb):
    """Add a constraint that pins the objective >= lb."""
    m = re.match(r"(MAX|MIN)\{(.+)\}", obj_str)
    if m:
        rxn_id = m.group(2)
        rxn = model.reactions.get_by_id(rxn_id)
        # Store original lower bound and pin
        rxn._pin_orig_lb = rxn.lower_bound
        rxn.lower_bound = lb


def _unpin_objective(model, obj_str):
    """Restore original lower bound set by _pin_objective."""
    m = re.match(r"(MAX|MIN)\{(.+)\}", obj_str)
    if m:
        rxn_id = m.group(2)
        rxn = model.reactions.get_by_id(rxn_id)
        if hasattr(rxn, "_pin_orig_lb"):
            rxn.lower_bound = rxn._pin_orig_lb
            del rxn._pin_orig_lb


def _apply_integer_cut(model, rxn_list, blocked_bounds):
    """Block each reaction in rxn_list in its used direction (integer-cut).

    Records each original bound in blocked_bounds for later restoration.
    """
    for r in rxn_list:
        rid = r["id"]
        direction = r["direction"]
        rxn = model.reactions.get_by_id(rid)
        if rid not in blocked_bounds:
            blocked_bounds[rid] = {}
        if direction == ">" and "ub" not in blocked_bounds[rid]:
            blocked_bounds[rid]["ub"] = rxn.upper_bound
            rxn.upper_bound = 0
        elif direction == "<" and "lb" not in blocked_bounds[rid]:
            blocked_bounds[rid]["lb"] = rxn.lower_bound
            rxn.lower_bound = 0


def _cleanup_probe(model, mdlutl, probe_rxn, probe_is_new, blocked_bounds, bounds_snapshot, probe_id):
    """Remove the probe reaction and restore all blocked bounds."""
    # Restore blocked bounds
    for rid, bound_dict in blocked_bounds.items():
        try:
            rxn = model.reactions.get_by_id(rid)
            if "ub" in bound_dict:
                rxn.upper_bound = bound_dict["ub"]
            if "lb" in bound_dict:
                rxn.lower_bound = bound_dict["lb"]
        except Exception:
            pass

    # Remove probe reaction if we added it
    if probe_is_new:
        try:
            rxn = model.reactions.get_by_id(probe_id)
            model.remove_reactions([rxn], remove_orphans=False)
        except Exception:
            pass
    else:
        # Restore probe bounds from snapshot
        if probe_id in bounds_snapshot:
            try:
                rxn = model.reactions.get_by_id(probe_id)
                lb, ub = bounds_snapshot[probe_id]
                rxn.lower_bound = lb
                rxn.upper_bound = ub
            except Exception:
                pass

class MSFBAUtils(KBModelUtils):
    """Tools for running a wide range of FBA analysis on metabolic models in KBase
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialize MS model utilities

        Args:
            **kwargs: Additional keyword arguments passed to SharedEnvironment
        """
        super().__init__(**kwargs)

    # ── EGC detection methods ─────────────────────────────────────────────

    def build_full_model_from_template(self, template, index="0"):
        """Build a fully-closed cobra.Model from an MSTemplate, wrapped as MSModelUtil.

        Every template reaction is included via MSTemplateReaction.to_reaction.
        Template directionality is preserved (the defect surface).  Any reaction
        whose id starts with EX_, DM_, SK_, or bio has lb=ub=0 (boundary guard).

        Args:
            template: MSTemplate object or template id resolvable via get_template().
            index: Compartment index (default "0").

        Returns:
            MSModelUtil wrapping the closed model.
        """
        if isinstance(template, str):
            template = self.get_template(template)
        return build_full_model_from_template_standalone(template, index=index)

    def add_probe_reaction(self, model, probe, compartment="c0"):
        """Add or reuse a probe reaction on the model.

        Generalizes MSModelUtil.add_atp_hydrolysis to an arbitrary probe spec.

        Args:
            model: cobra.Model or MSModelUtil.
            probe: Probe spec dict with keys name, stoichiometry (msid->coef),
                seed_annotation.
            compartment: Target compartment (default "c0").

        Returns:
            dict with keys reaction (cobra.Reaction), direction (">"), new (bool).
        """
        mdlutl = self._check_and_convert_model(model)
        return add_probe_reaction_standalone(mdlutl, probe, compartment=compartment)

    def minimize_active_reactions(
        self,
        model,
        objective=None,
        active_filter=None,
        fraction_of_optimum=1.0,
        tol=_TOL,
        zero_tol=_ZERO,
    ):
        """Return the count-minimal set of active reactions supporting an objective.

        Args:
            model: cobra.Model or MSModelUtil.
            objective: Objective string like "MAX{rxn_id}" (ignored if active_filter given).
            active_filter: Pre-computed {rxn_id: ">" or "<"} (optional).
            fraction_of_optimum: Pin fraction (default 1.0).
            tol: Activity threshold (default 1e-6).
            zero_tol: Zero threshold (default 1e-9).

        Returns:
            dict with keys reactions (list), size (int), solution.
        """
        mdlutl = self._check_and_convert_model(model)
        return minimize_active_reactions_standalone(
            mdlutl,
            objective=objective,
            active_filter=active_filter,
            fraction_of_optimum=fraction_of_optimum,
            tol=tol,
            zero_tol=zero_tol,
        )

    def enumerate_alternative_reaction_sets(
        self,
        model,
        solution,
        tol=_TOL,
        zero_tol=_ZERO,
    ):
        """For each active reaction, report alternatives, coupled reactions, and essentiality.

        Args:
            model: cobra.Model or MSModelUtil.
            solution: Result from minimize_active_reactions with "reactions" key.
            tol: Activity threshold (default 1e-6).
            zero_tol: Zero threshold (default 1e-9).

        Returns:
            dict mapping rxn_id to {alternatives, coupled, essential, direction,
            flux, reliability_score, is_core, equation, failed}.
        """
        mdlutl = self._check_and_convert_model(model)
        return enumerate_alternative_reaction_sets_standalone(
            mdlutl, solution, tol=tol, zero_tol=zero_tol
        )

    def find_flux_loops(
        self,
        template,
        objective="all",
        compartment="c0",
        max_loops_per_probe=5,
        tol=_TOL,
        zero_tol=_ZERO,
        fraction_of_optimum=1.0,
        flux_min_threshold=_TOL,
        enable_redox_probes=True,
    ):
        """Find energy/redox/mass loops (EGCs) in a template or pre-built model.

        Builds a closed model from the template, then for each probe in the
        target group: LP-maximizes probe flux, pins it, LP-minimizes total flux,
        finds the count-minimal reaction set, enumerates alternatives/coupled/
        essential reactions, and iteratively blocks found loops up to
        max_loops_per_probe.

        Args:
            template: MSTemplate object, template id string, or an MSModelUtil of
                a pre-built closed model (when template is already an MSModelUtil,
                it is used directly without rebuilding).
            objective: "all", "atp", "redox", "mass", None, or a cobra.Reaction.
            compartment: Target compartment for probes (default "c0").
            max_loops_per_probe: Max loops to enumerate per probe (default 5).
            tol: Activity threshold (default 1e-6).
            zero_tol: Zero threshold (default 1e-9).
            fraction_of_optimum: Pin fraction (default 1.0).
            flux_min_threshold: Min probe flux to be non-trivial (default 1e-6).
            enable_redox_probes: Include redox probes (default True).

        Returns:
            dict keyed by probe name; each value is a list of loop records
            (empty list = no loop for that probe).
        """
        import cobra as _cobra
        from modelseedpy.core.msmodelutl import MSModelUtil as _MSModelUtil

        if isinstance(template, _MSModelUtil):
            mdlutl = template
        elif isinstance(template, _cobra.Model):
            mdlutl = _MSModelUtil.get(template)
        elif isinstance(template, str):
            tmpl = self.get_template(template)
            mdlutl = build_full_model_from_template_standalone(tmpl)
        else:
            mdlutl = build_full_model_from_template_standalone(template)

        return find_flux_loops_standalone(
            mdlutl,
            objective=objective,
            compartment=compartment,
            max_loops_per_probe=max_loops_per_probe,
            tol=tol,
            zero_tol=zero_tol,
            fraction_of_optimum=fraction_of_optimum,
            flux_min_threshold=flux_min_threshold,
            enable_redox_probes=enable_redox_probes,
            log_fn=self.log_info,
        )

    def set_media(self, model, media):
        """Sets the media for the model"""
        if media is None:
            return
        model = self._check_and_convert_model(model)
        if isinstance(media,str):
            media = self.get_media(media,None)
        if isinstance(media,dict):
            media = self.MSMediaUtil(media)
        model.pkgmgr.getpkg("KBaseMediaPkg").build_package(media)
        return media
    
    def set_objective_from_string(self, model, objective: str):
        """Sets the objective for the model from a string"""
        if objective is None:
            return
        model = self._check_and_convert_model(model)
        model.pkgmgr.getpkg("ObjectivePkg").build_package(objective)
    
    def constrain_objective(self, model, objective=None, lower_bound=None, upper_bound=None):
        """Constrains the current objective to set upper/lower bounds"""
        self.set_objective_from_string(model,objective)
        model = self._check_and_convert_model(model)
        model.pkgmgr.getpkg("ObjConstPkg").build_package(lower_bound, upper_bound)

    def constrain_objective_to_fraction_of_optimum(self, model, fraction=0.9, media=None, objective=None):
        """Constrains the current objective to a fraction of the optimum"""
        model = self._check_and_convert_model(model)
        self.set_media(model,media)
        self.set_objective_from_string(model,objective)
        objective_value = model.model.slim_optimize()
        lower_bound = objective_value*fraction
        upper_bound = None
        if model.model.objective_direction == "min":
            lower_bound = None
            upper_bound = objective_value/fraction
        self.constrain_objective(model,lower_bound=lower_bound,upper_bound=upper_bound)
        return objective_value

    def configure_fba_formulation(self,model,media=None,objective=None,fraction_of_optimum=None):
        model = self._check_and_convert_model(model)
        if media is not None:
            self.set_media(model,media)
        if objective is not None:
            self.set_objective_from_string(model,objective)
        if fraction_of_optimum is not None:
            self.constrain_objective_to_fraction_of_optimum(model, fraction=fraction_of_optimum)
        return model

    def run_fba(self,model,media=None,objective=None,run_pfba=True):
        """Run FBA on a model with a specified media and objective"""
        model =self.configure_fba_formulation(model,media=media,objective=objective)
        #Optimizing the model
        solution = model.model.optimize()
        if run_pfba:
            pfb_solution = pfba(model.model)
            pfb_solution.objective_value = solution.objective_value
            return pfb_solution
        return solution
    
    def run_fva(self,model,media=None,objective=None,fraction_of_optimum=0.9):
        model = self.configure_fba_formulation(model,media=media,objective=objective,fraction_of_optimum=fraction_of_optimum)
        original_objective = model.model.objective
        results = {}
        for rxn in model.model.reactions:
            self.set_objective_from_string(model, objective="MAX{" + rxn.id + "}")
            results[rxn.id] = {}
            results[rxn.id]["MAX"] = model.model.slim_optimize()
            self.set_objective_from_string(model, objective="MIN{" + rxn.id + "}")
            results[rxn.id]["MIN"] = model.model.slim_optimize()
        model.model.objective = original_objective
        return results

    def analyzed_reaction_objective_coupling(self,model,solution,media=None,objective=None,fraction_of_optimum=None,biomass_objective_coupling=False,biomass_id=None):
        model = self.configure_fba_formulation(model,media=media,objective=objective,fraction_of_optimum=fraction_of_optimum)
        original_objective = model.model.objective
        print(original_objective.expression)
        # Categorize reactions by flux
        output = {}
        zero_flux_rxns = []
        active_rxns = []
        
        for rxn_id, flux in solution.fluxes.items():
            if rxn_id not in [r.id for r in model.model.reactions]:
                continue
            if abs(flux) <= 1e-9:
                zero_flux_rxns.append(rxn_id)
            else:
                active_rxns.append((rxn_id, flux))
        
        self.log_info(f"Zero-flux reactions: {len(zero_flux_rxns)}")
        self.log_info(f"Active reactions: {len(active_rxns)}")
        
        #Building this outside of with model statement because model is failing to clear the additional constraints consistently
        if biomass_objective_coupling:
            self.log_info(f"Building flexible biomass package")
            model.pkgmgr.getpkg("FlexibleBiomassPkg").build_package({"bio_rxn_id":biomass_id,"set_min_flex_biomass_objective":False})
            for rxn in model.model.reactions:
                if rxn.id.startswith("FLEX_"):
                    rxn.lower_bound = 0
                    rxn.upper_bound = 0

        # Get baseline growth with constrained model
        print(f"Baseline objective: {model.model.objective.expression}")
        output["baseline_objective_value"] = model.model.optimize().objective_value
        # Test each active reaction knockout
        output["essential_count"] = 0
        output["reduced_count"] = 0
        output["reaction_objective_coupling"] = {}
        with model.model:
            #model.model.objective = original_objective
            # Set zero-flux reactions to have zero bounds
            for rxn_id in zero_flux_rxns:
                rxn = model.model.reactions.get_by_id(rxn_id)
                rxn.lower_bound = 0
                rxn.upper_bound = 0
            
            #Consider setting max flux to current flux of every reaction as an optional procedure
            
            for rxn_id, original_flux in active_rxns:
                output["reaction_objective_coupling"][rxn_id] = {"original_flux":original_flux}
                rxn = model.model.reactions.get_by_id(rxn_id)
                
                # Save original bounds
                orig_lb = rxn.lower_bound
                orig_ub = rxn.upper_bound
                
                # Knock out the reaction
                rxn.lower_bound = 0
                rxn.upper_bound = 0
                
                # Optimize
                ko_solution = model.model.optimize()
                if ko_solution.status == 'optimal':
                    output["reaction_objective_coupling"][rxn_id]["ko_objective_value"] = ko_solution.objective_value
                    output["reaction_objective_coupling"][rxn_id]["objective_ratio"] = ko_solution.objective_value / output["baseline_objective_value"]  if output["baseline_objective_value"] > 0 else 0
                else:
                    output["reaction_objective_coupling"][rxn_id]["objective_ratio"] = 0
                    output["reaction_objective_coupling"][rxn_id]["ko_objective_value"] = None
                
                # Categorize impact
                if output["reaction_objective_coupling"][rxn_id]["objective_ratio"] < 0.01:
                    output["reaction_objective_coupling"][rxn_id]["impact"] = "essential"
                    output["essential_count"] += 1
                elif output["reaction_objective_coupling"][rxn_id]["objective_ratio"] < 0.95:
                    output["reaction_objective_coupling"][rxn_id]["impact"] = "reduced"
                    output["reduced_count"] += 1
                else:
                    output["reaction_objective_coupling"][rxn_id]["impact"] = "dispensable"
                
                if biomass_objective_coupling and output["reaction_objective_coupling"][rxn_id]["impact"] in ["essential","reduced"] and rxn_id != biomass_id:
                    output["reaction_objective_coupling"][rxn_id]["biomass_coupling"] = self.determine_biomass_objective_coupling(model,biomass_id,output["baseline_objective_value"],media=media,objective=objective,fraction_of_optimum=fraction_of_optimum)
                
                # Restore original bounds
                rxn.upper_bound = orig_ub
                rxn.lower_bound = orig_lb

        #Now let's repeat the analysis while allowing flux through the zero flux reactions
        # Get baseline growth with constrained model
        print(f"Unconstrained baseline objective: {model.model.objective.expression}")
        output["unconstrained_baseline_objective_value"] = model.model.optimize().objective_value
        # Test each active reaction knockout
        output["unconstrained_essential_count"] = 0
        output["unconstrained_reduced_count"] = 0
        with model.model:
            for rxn_id, original_flux in active_rxns:
                rxn = model.model.reactions.get_by_id(rxn_id)
                
                # Save original bounds
                orig_lb = rxn.lower_bound
                orig_ub = rxn.upper_bound
                
                # Knock out the reaction
                rxn.lower_bound = 0
                rxn.upper_bound = 0
                
                # Optimize
                ko_solution = model.model.optimize()
                if ko_solution.status == 'optimal':
                    output["reaction_objective_coupling"][rxn_id]["unconstrained_ko_objective_value"] = ko_solution.objective_value
                    output["reaction_objective_coupling"][rxn_id]["unconstrained_objective_ratio"] = ko_solution.objective_value / output["unconstrained_baseline_objective_value"]  if output["unconstrained_baseline_objective_value"] > 0 else 0
                else:
                    output["reaction_objective_coupling"][rxn_id]["unconstrained_objective_ratio"] = 0
                    output["reaction_objective_coupling"][rxn_id]["unconstrained_ko_objective_value"] = None
                
                # Categorize impact
                if output["reaction_objective_coupling"][rxn_id]["unconstrained_objective_ratio"] < 0.01:
                    output["reaction_objective_coupling"][rxn_id]["unconstrained_impact"] = "essential"
                    output["unconstrained_essential_count"] += 1
                elif output["reaction_objective_coupling"][rxn_id]["unconstrained_objective_ratio"] < 0.95:
                    output["reaction_objective_coupling"][rxn_id]["unconstrained_impact"] = "reduced"
                    output["unconstrained_reduced_count"] += 1
                else:
                    output["reaction_objective_coupling"][rxn_id]["unconstrained_impact"] = "dispensable"
                
                if biomass_objective_coupling and output["reaction_objective_coupling"][rxn_id]["unconstrained_impact"] in ["essential","reduced"] and rxn_id != biomass_id:
                    output["reaction_objective_coupling"][rxn_id]["unconstrained_biomass_coupling"] = self.determine_biomass_objective_coupling(model,biomass_id,output["unconstrained_baseline_objective_value"],media=media,objective=objective,fraction_of_optimum=fraction_of_optimum)
                
                # Restore original bounds
                rxn.upper_bound = orig_ub
                rxn.lower_bound = orig_lb
        return output

    def determine_biomass_objective_coupling(self,model,biomass_id,biomass_flux,media=None,objective=None,fraction_of_optimum=None):
        model = self.configure_fba_formulation(model,media=media,objective=objective,fraction_of_optimum=fraction_of_optimum)
        
        #Checking if flexible biomass package is already built
        original_objective = model.model.objective
        flex_found = False
        for rxn in model.model.reactions:
            if rxn.id.startswith("FLEX_"):
                flex_found = True
                rxn.lower_bound = -1000
                rxn.upper_bound = 1000
        if not flex_found:
            model.pkgmgr.getpkg("FlexibleBiomassPkg").build_package({"bio_rxn_id":biomass_id,"set_min_flex_biomass_objective":True})
        else:
            model.pkgmgr.getpkg("FlexibleBiomassPkg").set_min_flex_biomass_objective()
        #Forcing biomass to stay at a fixed value
        biorxn = model.model.reactions.get_by_id(biomass_id)
        original_lower_bound = biorxn.lower_bound
        original_upper_bound = biorxn.upper_bound
        biorxn.lower_bound = biomass_flux
        biorxn.upper_bound = biomass_flux

        #Optimizing the model
        solution = model.model.optimize()
        #Getting the impacted biomass components
        impacted_biomass_components = {}
        for rxn in model.model.reactions:
            if rxn.id.startswith("FLEX_") and solution.fluxes[rxn.id] < 0:
                impacted_component = rxn.id[6+len(biomass_id):]
                impacted_biomass_components[impacted_component] = solution.fluxes[rxn.id]

        #Disabling FLEX variables
        for rxn in model.model.reactions:
            if rxn.id.startswith("FLEX_"):
                rxn.lower_bound = 0
                rxn.upper_bound = 0

        model.model.objective = original_objective
        biorxn.lower_bound = original_lower_bound
        biorxn.upper_bound = original_upper_bound
        return impacted_biomass_components

    def unblock_objective_with_exchanges(self, model, media=None, objective=None, min_threshold=0.1, solution_count=10,exclude_metabolites=[]):
        """Find minimal sets of exchanges needed to unblock an objective.

        This function helps debug why a model cannot achieve flux through an objective.
        It adds temporary exchanges for all non-extracellular metabolites that don't
        already have exchanges, constrains the objective to exceed a threshold, then
        minimizes the total exchange flux to find what metabolites need to be supplied
        or removed.

        Args:
            model: COBRApy model or MSModelUtil instance
            media: Media to apply (optional)
            objective: Objective string (e.g., "MAX{ANME_2}")
            min_threshold: Minimum flux required through objective
            solution_count: Maximum number of alternative solutions to find

        Returns:
            List of solution dictionaries, each containing:
            - 'active_exchanges': dict of {rxn_id: flux} for active temporary exchanges
            - 'objective_value': the objective value achieved
            - 'total_exchange_flux': sum of absolute flux through temporary exchanges
        """
        # Configure model with media and objective (returns MSModelUtil instance)
        mdlutl = self.configure_fba_formulation(model, media=media, objective=objective)
        cobra_model = mdlutl.model

        # Use MSModelUtil's exchange_hash to find metabolites that already have exchanges
        existing_exchange_hash = mdlutl.exchange_hash()
        existing_exchange_mets = set(met for met in existing_exchange_hash.keys())

        # Extracellular compartments - metabolites here don't need temporary exchanges
        extracellular_compartments = ['e', 'e0', 'env']

        # Find metabolites that need temporary exchanges:
        # - Not in extracellular compartment
        # - Don't already have an exchange
        mets_needing_exchanges = []
        for met in cobra_model.metabolites:
            if met.compartment not in extracellular_compartments and met not in existing_exchange_mets and met.id not in exclude_metabolites:
                mets_needing_exchanges.append(met)

        # Use MSModelUtil's add_exchanges_for_metabolites to add temporary exchanges
        if mets_needing_exchanges:
            mdlutl.add_exchanges_for_metabolites(
                mets_needing_exchanges,
                uptake=1000,
                excretion=1000,
                prefix="EX_temp_"
            )

        # Get list of added exchange reaction objects
        added_exchanges = []
        for rxn in cobra_model.reactions:
            if rxn.id.startswith("EX_temp_"):
                added_exchanges.append(rxn)

        self.log_info(f"Added {len(added_exchanges)} temporary exchanges for non-extracellular metabolites")

        # Constrain the objective to exceed the minimum threshold
        self.constrain_objective(mdlutl, objective=objective, lower_bound=min_threshold)

        # Set objective to minimize total absolute exchange flux
        objective_terms = []
        for ex_rxn in added_exchanges:
            objective_terms.append(ex_rxn.forward_variable)
            objective_terms.append(ex_rxn.reverse_variable)
        min_objective = cobra_model.problem.Objective(sum(objective_terms), direction='min')
        original_objective = cobra_model.objective
        cobra_model.objective = min_objective

        # Find multiple solutions by iteratively blocking active exchanges
        solutions = []
        blocked_exchanges = set()

        for i in range(solution_count):
            # Try to optimize
            mdlutl.printlp(print=True,path="models/lpfiles/",filename="unblock_objective_with_exchanges_"+str(i))
            solution = cobra_model.optimize()

            if solution.status != 'optimal':
                self.log_info(f"Solution {i+1}: No feasible solution found")
                break

            # Find active temporary exchanges
            active_exchanges = {}
            for ex_rxn in added_exchanges:
                if ex_rxn.id not in blocked_exchanges:
                    flux = solution.fluxes.get(ex_rxn.id, 0)
                    if abs(flux) > 1e-6:
                        # Get the metabolite name for better readability
                        met_id = ex_rxn.id.replace("EX_temp_", "")
                        active_exchanges[ex_rxn.id] = {
                            'flux': flux,
                            'metabolite': met_id,
                            'direction': 'uptake' if flux < 0 else 'excretion'
                        }

            if not active_exchanges:
                self.log_info(f"Solution {i+1}: No active temporary exchanges (objective achievable without them)")
                solutions.append({
                    'solution_number': i + 1,
                    'active_exchanges': {},
                    'objective_value': solution.objective_value,
                    'message': 'Objective achievable without temporary exchanges'
                })
                break

            # Record solution
            sol_record = {
                'solution_number': i + 1,
                'active_exchanges': active_exchanges,
                'objective_value': solution.objective_value,
                'total_exchange_flux': sum(abs(v['flux']) for v in active_exchanges.values()),
                'fluxes': dict(solution.fluxes)
            }
            solutions.append(sol_record)

            self.log_info(f"Solution {i+1}: {len(active_exchanges)} active exchanges, total flux: {sol_record['total_exchange_flux']:.4f}")
            for rxn_id, data in active_exchanges.items():
                self.log_info(f"  {data['metabolite']}: {data['flux']:.4f} ({data['direction']})")

            # Block the active exchanges for next iteration
            for rxn_id in active_exchanges.keys():
                ex_rxn = cobra_model.reactions.get_by_id(rxn_id)
                ex_rxn.lower_bound = 0
                ex_rxn.upper_bound = 0
                blocked_exchanges.add(rxn_id)

        # Clean up: remove temporary exchanges
        cobra_model.remove_reactions(added_exchanges, remove_orphans=False)
        cobra_model.objective = original_objective

        return solutions

    def fit_flux_to_mutant_growth_rate_data(
        self,
        model,
        genome,
        data_source,
        media_dict,
        conditions=None,
        excluded_conditions=None,
        default_coef=0.01,
        activation_threshold=0.90,
        deactivation_threshold=0.95,
        biomass_reaction_id="bio1",
        growth_fraction=0.5,
        use_activation_constraints=False,
        run_reaction_coupling_analysis=True,
        verbose=True
    ):
        """Fit metabolic model fluxes to mutant growth rate data across multiple conditions.

        This function takes mutant growth rate phenotype data (normalized ratios), creates
        MSExpression constraints, and analyzes reaction essentiality for each condition.

        Args:
            model: COBRApy model, MSModelUtil instance, or path to model JSON file
            genome: MSGenome object or dict containing genome data (for gene lookups)
            data_source: One of:
                - str: Path to spreadsheet (.xls, .xlsx), JSON, or TSV file
                - dict: Dictionary of {condition: {gene_id: value}} data
                - pd.DataFrame: DataFrame with genes as rows, conditions as columns
            media_dict: Dictionary mapping condition names to media objects/dicts
            conditions: List of condition names to analyze. If None, uses all conditions
                from media_dict keys
            excluded_conditions: List of condition names to skip (default: None)
            default_coef: Default coefficient for expression fitting (default: 0.01)
            activation_threshold: Threshold below which genes are considered "on" (default: 0.90)
            deactivation_threshold: Threshold above which genes are considered "off" (default: 0.95)
            biomass_reaction_id: ID of the biomass/growth reaction (default: "bio1")
            growth_fraction: Fraction of optimal growth to constrain to (default: 0.5)
            use_activation_constraints: Whether to use hard activation constraints (default: False)
            run_reaction_coupling_analysis: Whether to run reaction KO analysis (default: True)
            verbose: Print progress messages (default: True)

        Returns:
            dict: Results dictionary with structure:
                {
                    "condition_name": {
                        "fluxes": {rxn_id: flux_value, ...},
                        "growth_rate": float,
                        "fraction": float,
                        "status": str,
                        "on_on": [rxn_ids...],  # Expression "on" AND flux active
                        "on_off": [rxn_ids...], # Expression "on" BUT flux inactive
                        "off_on": [rxn_ids...], # Expression "off" BUT flux active
                        "off_off": [rxn_ids...], # Expression "off" AND flux inactive
                        "none_on": [rxn_ids...], # No data, flux active
                        "none_off": [rxn_ids...], # No data, flux inactive
                        "on_genes": [gene_ids...], # Genes marked as "on"
                        "off_genes": [gene_ids...], # Genes marked as "off"
                        "on_rxn_genes": {rxn_id: [gene_ids]}, # Genes inducing "on" reactions
                        "baseline_growth": float,
                        "essential_count": int,
                        "reduced_count": int,
                        "reaction_objective_coupling": {...},
                        "on_on_reduced": [rxn_ids...],
                        "off_on_reduced": [rxn_ids...],
                        "none_on_reduced": [rxn_ids...],
                        "unconstrained_baseline_growth": float,
                        "unconstrained_essential_count": int,
                        "unconstrained_reduced_count": int
                    },
                    ...
                }
        """
        import cobra.io
        from modelseedpy import MSExpression, MSMedia
        from modelseedpy.core.msmodelutl import MSModelUtil

        # Load/convert model
        if isinstance(model, str):
            model = MSModelUtil.from_cobrapy(model)
        elif not isinstance(model, MSModelUtil):
            model = MSModelUtil.get(model)

        # Load/convert genome
        if isinstance(genome, dict):
            genome = self.get_msgenome_from_dict(genome.get("data", genome))

        # Load expression data from various sources
        if isinstance(data_source, str):
            # File path - determine type from extension
            if data_source.endswith(('.xls', '.xlsx')):
                expression = MSExpression.from_spreadsheet(
                    filename=data_source,
                    type="NormalizedRatios"
                )
            elif data_source.endswith('.json'):
                with open(data_source, 'r') as f:
                    data_dict = json.load(f)
                expression = MSExpression.load_from_dict(
                    genome_or_model=genome,
                    data_dict=data_dict,
                    value_type="NormalizedRatios"
                )
            elif data_source.endswith('.tsv'):
                df = pd.read_csv(data_source, sep='\t', index_col=0)
                expression = MSExpression.from_dataframe(
                    genome_or_model=genome,
                    df=df,
                    type="NormalizedRatios"
                )
            else:
                raise ValueError(f"Unsupported file type: {data_source}")
        elif isinstance(data_source, dict):
            expression = MSExpression.load_from_dict(
                genome_or_model=genome,
                data_dict=data_source,
                value_type="NormalizedRatios"
            )
        elif isinstance(data_source, pd.DataFrame):
            expression = MSExpression.from_dataframe(
                genome_or_model=genome,
                df=data_source,
                type="NormalizedRatios"
            )
        else:
            raise ValueError(f"Unsupported data_source type: {type(data_source)}")

        # Determine conditions to process
        if conditions is None:
            conditions = list(media_dict.keys())

        if excluded_conditions is None:
            excluded_conditions = []

        # Store results
        results = {}
        optimal_growth_rates = {}

        if verbose:
            self.log_info(f"Fitting flux to mutant growth rate data for {len(conditions)} conditions")
            self.log_info(f"Parameters: default_coef={default_coef}, activation_threshold={activation_threshold}, deactivation_threshold={deactivation_threshold}")

        for condition in conditions:
            if condition in excluded_conditions:
                if verbose:
                    self.log_info(f"Skipping excluded condition: {condition}")
                continue

            if condition not in media_dict:
                self.log_warning(f"No media found for condition: {condition}, skipping")
                continue

            if verbose:
                self.log_info(f"\nProcessing condition: {condition}")

            try:
                # Create a deep copy of the model for this condition so per-condition
                # mutations (media bounds, objective constraints, etc.) don't leak
                # back into the shared parent model. `MSModelUtil.from_cobrapy`
                # expects a *file path*, not a JSON string, so the previous
                # `from_cobrapy(cobra.io.json.to_json(...))` call was relying on
                # an undocumented fallback. Use `cobra.Model.copy()` directly
                # and wrap the result in a fresh `MSModelUtil`.
                model_copy = MSModelUtil(model.model.copy())

                # Get media for this condition
                media = media_dict[condition]
                if isinstance(media, dict):
                    media = MSMedia.from_dict(media)

                # Set growth constraint to fraction of optimum
                optimal_growth = self.constrain_objective_to_fraction_of_optimum(
                    model_copy,
                    media=media,
                    objective=f"MAX{{{biomass_reaction_id}}}",
                    fraction=growth_fraction
                )
                optimal_growth_rates[condition] = optimal_growth

                if verbose:
                    self.log_info(f"  Optimal growth: {optimal_growth:.4f}, constrained to {growth_fraction*100}%")

                # Fit flux to mutant growth rate data
                model_copy.util = self
                fit_result = expression.fit_flux_to_mutant_growth_rate_data(
                    model=model_copy,
                    condition=condition,
                    default_coef=default_coef,
                    activation_threshold=activation_threshold,
                    deactivation_threshold=deactivation_threshold,
                    use_activation_constraints=use_activation_constraints
                )

                solution = fit_result.get('solution')
                if solution is None or solution.status != 'optimal':
                    status = solution.status if solution else 'no_solution'
                    self.log_warning(f"  Optimization failed for {condition}: {status}")
                    results[condition] = {'status': status}
                    continue

                if verbose:
                    self.log_info(f"  FBA successful, growth rate: {solution.objective_value:.4f}")

                # Run reaction objective coupling analysis if requested
                coupling_output = {}
                if run_reaction_coupling_analysis:
                    # Remove growth constraint for coupling analysis
                    self.constrain_objective_to_fraction_of_optimum(
                        model_copy, media=media,
                        objective=f"MAX{{{biomass_reaction_id}}}",
                        fraction=0
                    )

                    coupling_output = self.analyzed_reaction_objective_coupling(
                        model_copy,
                        solution,
                        biomass_objective_coupling=True,
                        biomass_id=biomass_reaction_id
                    )

                    # Initialize reduced lists
                    coupling_output["on_on_reduced"] = []
                    coupling_output["off_on_reduced"] = []
                    coupling_output["none_on_reduced"] = []

                    # Categorize reactions by expression status
                    for rxn in model_copy.model.reactions:
                        rxn_id = rxn.id
                        if rxn_id not in coupling_output.get("reaction_objective_coupling", {}):
                            continue
                        rxn_coupling = coupling_output["reaction_objective_coupling"][rxn_id]
                        if "objective_ratio" not in rxn_coupling:
                            continue

                        # Determine expression status
                        if rxn_id in fit_result.get("on_on", []) or rxn_id in fit_result.get("on_off", []):
                            rxn_coupling["expression_data_status"] = "on"
                            if rxn_coupling["objective_ratio"] < 0.95:
                                coupling_output["on_on_reduced"].append(rxn_id)
                        elif rxn_id in fit_result.get("off_on", []) or rxn_id in fit_result.get("off_off", []):
                            rxn_coupling["expression_data_status"] = "off"
                            if rxn_coupling["objective_ratio"] < 0.95:
                                coupling_output["off_on_reduced"].append(rxn_id)
                        else:
                            rxn_coupling["expression_data_status"] = "none"
                            if rxn_coupling["objective_ratio"] < 0.95:
                                coupling_output["none_on_reduced"].append(rxn_id)

                # Store results for this condition
                results[condition] = {
                    "fluxes": solution.fluxes.to_dict(),
                    "growth_rate": solution.fluxes.get(biomass_reaction_id, solution.objective_value),
                    "fraction": solution.fluxes.get(biomass_reaction_id, solution.objective_value) / optimal_growth if optimal_growth > 0 else 0,
                    "status": solution.status,
                    "on_on": fit_result.get("on_on", []),
                    "on_off": fit_result.get("on_off", []),
                    "off_on": fit_result.get("off_on", []),
                    "off_off": fit_result.get("off_off", []),
                    "none_on": fit_result.get("none_on", []),
                    "none_off": fit_result.get("none_off", []),
                    "on_genes": fit_result.get("on_genes", []),
                    "off_genes": fit_result.get("off_genes", []),
                    "on_rxn_genes": fit_result.get("on_rxn_genes", {}),
                    "baseline_growth": coupling_output.get("baseline_objective_value", 0),
                    "essential_count": coupling_output.get("essential_count", 0),
                    "reduced_count": coupling_output.get("reduced_count", 0),
                    "reaction_objective_coupling": coupling_output.get("reaction_objective_coupling", {}),
                    "on_on_reduced": coupling_output.get("on_on_reduced", []),
                    "off_on_reduced": coupling_output.get("off_on_reduced", []),
                    "none_on_reduced": coupling_output.get("none_on_reduced", []),
                    "unconstrained_baseline_growth": coupling_output.get("unconstrained_baseline_objective_value", 0),
                    "unconstrained_essential_count": coupling_output.get("unconstrained_essential_count", 0),
                    "unconstrained_reduced_count": coupling_output.get("unconstrained_reduced_count", 0)
                }

                if verbose:
                    self.log_info(f"  Essential reactions: {results[condition]['essential_count']}")
                    self.log_info(f"  On/On reactions: {len(results[condition]['on_on'])}")
                    self.log_info(f"  On genes: {len(results[condition]['on_genes'])}")

            except Exception as e:
                self.log_error(f"Error processing condition {condition}: {str(e)}")
                results[condition] = {'status': 'error', 'error': str(e)}

        if verbose:
            self.log_info(f"\nCompleted analysis for {len([r for r in results.values() if r.get('status') == 'optimal'])} conditions")

        return results

    # ── Template Evaluation Suite — per-model test functions ─────────────

    def classify_reactions_by_fva(self, model, media=None, essential_fraction=0.2):
        """Classify every reaction by its FVA flux range.

        Two FVA passes via ``run_fva`` (never cobra.flux_variability_analysis):

        * **Unconstrained pass** (fraction_of_optimum=0): classifies reactions as
          dead, forward_only, reverse_only, or reversible.
        * **Growth-forced pass** (fraction_of_optimum=essential_fraction, one pass
          per biomass): classifies reactions as essential (0 not in [MIN, MAX]).

        When both bio1 and bio2 exist in the model, the growth-forced pass runs
        separately for each biomass and essential sets are reported per-biomass
        plus a union.

        Args:
            model: cobra.Model or MSModelUtil.
            media: Media to apply (optional).
            essential_fraction: Fraction of optimum for growth-forced pass (default 0.2).

        Returns:
            dict with keys:
              dead, forward_only, reverse_only, reversible — lists of reaction ids.
              essential — dict keyed by biomass id and "union".
        """
        tol = 1e-7
        mdlutl = self._check_and_convert_model(model)

        # ── unconstrained pass (fraction_of_optimum=0) ──────────────────
        self.log_info("classify_reactions_by_fva: running unconstrained pass")
        fva_unconstrained = self.run_fva(mdlutl, media=media, fraction_of_optimum=0)

        dead = []
        forward_only = []
        reverse_only = []
        reversible = []
        for rxn_id, bounds in fva_unconstrained.items():
            mn = bounds["MIN"]
            mx = bounds["MAX"]
            if abs(mn) <= tol and abs(mx) <= tol:
                dead.append(rxn_id)
            elif mn >= -tol and mx > tol:
                forward_only.append(rxn_id)
            elif mx <= tol and mn < -tol:
                reverse_only.append(rxn_id)
            else:
                reversible.append(rxn_id)

        # ── growth-forced pass — one per biomass ──────────────────────────
        cobra_model = mdlutl.model
        biomass_ids = [
            r.id for r in cobra_model.reactions
            if r.id.startswith("bio") or "biomass" in r.id.lower()
        ]
        # Limit to bio1 / bio2 convention
        bio_rxns = [rid for rid in biomass_ids if rid in ("bio1", "bio2")]
        if not bio_rxns:
            bio_rxns = biomass_ids[:2]  # fall back to first two

        essential_per_bio = {}
        essential_union = set()

        for bio_id in bio_rxns:
            self.log_info(
                f"classify_reactions_by_fva: growth-forced pass for {bio_id}"
                f" (fraction={essential_fraction})"
            )
            try:
                fva_forced = self.run_fva(
                    mdlutl,
                    media=media,
                    objective=f"MAX{{{bio_id}}}",
                    fraction_of_optimum=essential_fraction,
                )
            except Exception as e:
                self.log_info(
                    f"classify_reactions_by_fva: growth-forced pass for {bio_id} failed: {e}"
                )
                essential_per_bio[bio_id] = []
                continue
            essential_bio = []
            for rxn_id, bounds in fva_forced.items():
                mn = bounds["MIN"]
                mx = bounds["MAX"]
                # essential when 0 is NOT in [mn, mx]
                if not (mn <= 0 <= mx):
                    essential_bio.append(rxn_id)
            essential_per_bio[bio_id] = essential_bio
            essential_union.update(essential_bio)

        essential_per_bio["union"] = sorted(essential_union)

        return {
            "dead": dead,
            "forward_only": forward_only,
            "reverse_only": reverse_only,
            "reversible": reversible,
            "essential": essential_per_bio,
        }

    def find_closed_mode_reactions(self, model, media=None):
        """Find reactions that carry flux in a closed (no-exchange) system.

        Zeroes all EX_/DM_/SK_ exchange and drain reactions (plus any identified
        by MSModelUtil.exchange_hash()), leaves ATPM and biomass unconstrained,
        runs an unconstrained FVA, returns reactions that can still carry flux.
        Restores all bounds before returning.

        Args:
            model: cobra.Model or MSModelUtil.
            media: Media to apply (optional).

        Returns:
            list of reaction ids that can carry flux in a closed system.
        """
        tol = 1e-7
        mdlutl = self._check_and_convert_model(model)
        cobra_model = mdlutl.model

        # Collect exchange/drain/SK reactions to zero
        drain_prefixes = ("EX_", "DM_", "SK_")
        skip_ids = {"ATPM"}
        # also add biomass reaction ids to the skip set
        for rxn in cobra_model.reactions:
            if rxn.id.startswith("bio") or "biomass" in rxn.id.lower():
                skip_ids.add(rxn.id)

        # Reactions from exchange_hash
        try:
            exchange_hash_rxns = set()
            for met, rxn_obj in mdlutl.exchange_hash().items():
                exchange_hash_rxns.add(rxn_obj.id)
        except Exception:
            exchange_hash_rxns = set()

        saved_bounds = {}
        for rxn in cobra_model.reactions:
            if rxn.id in skip_ids:
                continue
            if (
                any(rxn.id.startswith(p) for p in drain_prefixes)
                or rxn.id in exchange_hash_rxns
            ):
                saved_bounds[rxn.id] = (rxn.lower_bound, rxn.upper_bound)
                rxn.lower_bound = 0
                rxn.upper_bound = 0

        self.log_info(
            f"find_closed_mode_reactions: zeroed {len(saved_bounds)} exchange/drain reactions"
        )

        try:
            fva_closed = self.run_fva(mdlutl, media=None, fraction_of_optimum=0)
            closed_rxns = [
                rxn_id
                for rxn_id, bounds in fva_closed.items()
                if abs(bounds["MIN"]) > tol or abs(bounds["MAX"]) > tol
            ]
        finally:
            # Restore all zeroed bounds
            for rxn_id, (lb, ub) in saved_bounds.items():
                rxn = cobra_model.reactions.get_by_id(rxn_id)
                if lb > rxn.upper_bound:
                    rxn.upper_bound = ub
                    rxn.lower_bound = lb
                else:
                    rxn.lower_bound = lb
                    rxn.upper_bound = ub

        self.log_info(
            f"find_closed_mode_reactions: {len(closed_rxns)} reactions carry flux in closed mode"
        )
        return closed_rxns

    def get_biolog_phenotypes(self, element=None):
        """Load the committed Biolog phenotype stash from package data.

        Loads all four C/N/S/P MSGrowthPhenotypes sets, or a single set by
        element letter.  Requires no KBase authentication.

        Args:
            element: One of 'C', 'N', 'S', 'P', or None for all four.

        Returns:
            dict mapping element letter to MSGrowthPhenotypes, or a single
            MSGrowthPhenotypes when element is specified.

        Raises:
            KeyError: If element is not one of C/N/S/P.
            FileNotFoundError: If the stash file has not been committed yet.
        """
        import importlib.resources as pkg_resources
        from modelseedpy.core.msgrowthphenotypes import MSGrowthPhenotypes

        valid_elements = {"C", "N", "S", "P"}
        if element is not None and element not in valid_elements:
            raise KeyError(f"get_biolog_phenotypes: unknown element '{element}'. Use one of {valid_elements}")

        # Load via importlib.resources (works from installed package and editable install)
        try:
            # Python 3.9+ path
            ref = pkg_resources.files("kbutillib.data").joinpath("biolog_phenotypes.json")
            data = json.loads(ref.read_text(encoding="utf-8"))
        except (AttributeError, TypeError):
            # Python 3.8 fallback
            with pkg_resources.open_text("kbutillib.data", "biolog_phenotypes.json") as f:
                data = json.load(f)

        result = {}
        for elem, pheno_dict in data.items():
            result[elem] = MSGrowthPhenotypes.from_dict(pheno_dict)

        if element is not None:
            if element not in result:
                raise KeyError(f"get_biolog_phenotypes: element '{element}' not found in stash")
            return result[element]
        return result

    def refresh_biolog_phenotypes(self, workspace="KBaseMedia"):
        """Enumerate Biolog media from KBase and write the committed stash.

        Enumerates Carbon-*/Nitrogen-*/Sulfate-*/Phosphate-* media from the
        given workspace, extracts the differentiating (primary) compound per
        media, builds four MSGrowthPhenotypes sets, and writes them to
        src/kbutillib/data/biolog_phenotypes.json.

        Requires KBase authentication.

        Args:
            workspace: KBase workspace name containing Biolog media (default "KBaseMedia").

        Returns:
            dict mapping element letter to count of phenotypes built.
        """
        from modelseedpy.core.msgrowthphenotypes import MSGrowthPhenotypes, MSGrowthPhenotype
        from modelseedpy.core.msmedia import MSMedia

        ELEMENT_PREFIXES = {
            "C": ("Carbon-", "C"),
            "N": ("Nitrogen-", "N"),
            "S": ("Sulfate-", "S"),
            "P": ("Phosphate-", "P"),
        }
        ELEMENT_LIMITS = {
            "C": 10,
            "N": 10,
            "S": 10,
            "P": 10,
        }

        self.log_info(f"refresh_biolog_phenotypes: enumerating media from workspace '{workspace}'")
        raw_list = self.kbase_api.list_objects(workspace, object_type="KBaseBiochem.Media")
        # raw_list is a list of workspace object info arrays; item[1] is the name
        all_object_names = {item[1]: item for item in raw_list}

        stash = {}
        counts = {}

        for elem, (prefix, target_element) in ELEMENT_PREFIXES.items():
            self.log_info(f"refresh_biolog_phenotypes: processing {elem} ({prefix}*)")
            media_names = sorted([k for k in all_object_names.keys() if k.startswith(prefix)])
            self.log_info(f"  found {len(media_names)} media")

            # Collect all compound sets to find the shared base (intersection)
            all_cpd_sets = []
            media_cpd_map = {}  # media_name -> {cpd_id: (lower_bound, upper_bound)}

            for name in media_names:
                try:
                    # Use raw get_object to get dict directly
                    data = self.kbase_api.get_object(name, workspace)
                    if data is None:
                        continue
                    cpd_map = {}
                    for cpd in data.get("mediacompounds", []):
                        cpd_id = cpd["compound_ref"].split("/")[-1]
                        cpd_map[cpd_id] = (
                            float(-cpd.get("maxFlux", 100)),
                            float(-cpd.get("minFlux", -100)),
                        )
                    media_cpd_map[name] = cpd_map
                    all_cpd_sets.append(set(cpd_map.keys()))
                except Exception as e:
                    self.log_info(f"  skipping {name}: {e}")

            if not all_cpd_sets:
                self.log_info(f"  no accessible media for {elem}, skipping")
                continue

            # Shared base = intersection of all compound sets
            base_cpd_set = set.intersection(*all_cpd_sets)

            # Build base media from the intersection of the first media's bounds
            # (all shared cpds have same bounds across media, use first)
            first_name = media_names[0]
            first_map = media_cpd_map.get(first_name, {})
            base_media_dict = {}
            for cpd_id in base_cpd_set:
                lb, ub = first_map.get(cpd_id, (-100, 100))
                base_media_dict[cpd_id] = {"id": cpd_id, "lower_bound": lb, "upper_bound": ub}
            base_media = MSMedia.from_dict(base_media_dict)
            base_media.id = f"base_{prefix.rstrip('-').lower()}"
            base_media.name = f"Base {prefix.rstrip('-')} Media"

            # Build one phenotype per media
            phenotypes = MSGrowthPhenotypes(
                id=f"biolog_{elem}",
                name=f"Biolog {elem} panel",
                base_media=None,
                base_uptake=0,
                base_excretion=1000,
            )
            pheno_list = []
            for name in media_names:
                cpd_map = media_cpd_map.get(name)
                if cpd_map is None:
                    continue
                unique_cpds = set(cpd_map.keys()) - base_cpd_set
                if not unique_cpds:
                    # No differentiating compound (unusual); skip
                    self.log_info(f"  {name}: no unique compound vs base, skipping")
                    continue
                primary_compound_id = sorted(unique_cpds)[0]  # take first if multiple

                # Build base media for this phenotype (base + primary compound)
                pheno_base_dict = dict(base_media_dict)
                lb, ub = cpd_map.get(primary_compound_id, (-100, 100))
                pheno_base_dict[primary_compound_id] = {
                    "id": primary_compound_id, "lower_bound": lb, "upper_bound": ub
                }

                pheno = MSGrowthPhenotype(
                    id=name,
                    name=name,
                    base_media=base_media,
                    primary_compounds=[primary_compound_id],
                    target_element=target_element,
                    target_element_limit=ELEMENT_LIMITS[elem],
                )
                pheno_list.append(pheno)

            phenotypes.add_phenotypes(pheno_list)
            stash[elem] = phenotypes.to_dict()
            counts[elem] = len(pheno_list)
            self.log_info(f"  built {len(pheno_list)} phenotypes for {elem}")

        # Write the stash to the data directory
        import importlib.resources as pkg_resources
        import pathlib

        try:
            data_ref = pkg_resources.files("kbutillib.data")
            stash_path = pathlib.Path(str(data_ref)) / "biolog_phenotypes.json"
        except (AttributeError, TypeError):
            # Fallback: locate relative to this module file
            stash_path = pathlib.Path(__file__).parent / "data" / "biolog_phenotypes.json"

        stash_path.parent.mkdir(parents=True, exist_ok=True)
        with open(stash_path, "w", encoding="utf-8") as f:
            json.dump(stash, f, indent=2)
        self.log_info(f"refresh_biolog_phenotypes: wrote stash to {stash_path}")

        return counts

    def simulate_biolog(self, model, elements=("C", "N", "S", "P"), growth_threshold=0.01):
        """Simulate Biolog phenotype panels on a model.

        Runs MSGrowthPhenotypes.simulate_phenotypes for the requested element
        panels and returns the functional (growing) media per element.  When
        both bio1 and bio2 exist, runs per-biomass (MAX{bio1}, MAX{bio2}) and
        reports per-biomass plus union.

        Args:
            model: cobra.Model or MSModelUtil.
            elements: Tuple of element letters to simulate (default all four).
            growth_threshold: Minimum biomass flux to call growth (1/h, default 0.01).

        Returns:
            dict keyed by element, each value a dict with biomass_id keys and
            a "union" key, each containing a list of growing media names.
        """
        mdlutl = self._check_and_convert_model(model)
        cobra_model = mdlutl.model

        # Determine biomass reactions present
        bio_rxns = [r.id for r in cobra_model.reactions if r.id in ("bio1", "bio2")]
        if not bio_rxns:
            bio_rxns = [
                r.id for r in cobra_model.reactions
                if r.id.startswith("bio") or "biomass" in r.id.lower()
            ][:2]

        phenotypes_by_elem = self.get_biolog_phenotypes()
        results = {}

        for elem in elements:
            elem = elem.upper()
            if elem not in phenotypes_by_elem:
                self.log_info(f"simulate_biolog: element '{elem}' not in stash, skipping")
                continue

            phenoset = phenotypes_by_elem[elem]
            elem_result = {}
            union_growing = set()

            for bio_id in bio_rxns:
                self.log_info(f"simulate_biolog: simulating {elem} panel for {bio_id}")
                growing = []
                for pheno in phenoset.phenotypes:
                    try:
                        media = pheno.build_media()
                        self.set_media(mdlutl, media)
                        self.set_objective_from_string(mdlutl, f"MAX{{{bio_id}}}")
                        flux = cobra_model.slim_optimize()
                        if flux is not None and flux > growth_threshold:
                            growing.append(pheno.id)
                    except Exception as e:
                        self.log_info(
                            f"simulate_biolog: error simulating {pheno.id} for {bio_id}: {e}"
                        )
                elem_result[bio_id] = growing
                union_growing.update(growing)

            elem_result["union"] = sorted(union_growing)
            results[elem] = elem_result

        return results

    def simulate_growth_phenotypes(
        self,
        model,
        media,
        observed=None,
        growth_threshold=0.01,
        observed_threshold=1e-6,
        add_missing_exchanges=False,
        biomass="bio1",
    ):
        """Simulate a model against a list of full media phenotypes.

        Runs one FBA per media: sets the media on the model, optionally adds
        missing exchange/transport reactions (the "free transport" toggle),
        maximizes biomass, and calls growth when the biomass flux exceeds
        ``growth_threshold``.  When ``observed`` values are supplied, each
        media is binarized (observed > ``observed_threshold`` == growth) and
        classified CP/CN/FP/FN against the simulation, yielding an accuracy.

        This runs a direct FBA loop rather than
        ``MSGrowthPhenotypes.simulate_phenotypes`` deliberately: that path
        (a) KeyErrors on ``missing_transports`` when add_missing_exchanges is
        False, and (b) pulls in gapfilling machinery not wanted here.  Media
        objects are still built with ModelSEEDpy's MSMedia, and bound changes
        are isolated per-media via the cobra model context manager so the
        media are mutually independent.

        Args:
            model: cobra.Model or MSModelUtil.
            media: dict {media_id: MSMedia | KBase media dict/object}.  Each
                value is coerced to MSMedia.
            observed: optional dict {media_id: float} of observed growth values
                (e.g. a spreadsheet column).  Media absent here are simulated
                but not scored.
            growth_threshold: minimum biomass flux to call simulated growth.
            observed_threshold: observed value above which the reference is
                called growth (binarization cutoff).
            add_missing_exchanges: if True, add missing exchanges for media
                compounds before solving ("free transport").
            biomass: biomass reaction id to maximize (default "bio1").

        Returns:
            dict with keys:
              "details": list of per-media row dicts (media_id, simulated_flux,
                  simulated_growth, observed_value, observed_growth, class).
              "summary": dict with accuracy, CP, CN, FP, FN, P, N counts.
        """
        from modelseedpy.core.msmedia import MSMedia

        def _coerce_media(m):
            if isinstance(m, MSMedia):
                return m
            if isinstance(m, dict):
                # Unwrap a workspace object envelope (get_object returns
                # {info, data, provenance, ...}) to its media data payload.
                if "mediacompounds" not in m and isinstance(m.get("data"), dict) \
                        and "mediacompounds" in m["data"]:
                    m = m["data"]
                if "mediacompounds" in m:
                    # Plain KBase media data dict -> bounds hash, mirroring
                    # MSMedia.from_kbase_object's sign convention
                    # (lower_bound = -maxFlux, upper_bound = -minFlux).
                    bounds = {}
                    for c in m["mediacompounds"]:
                        cid = str(c.get("compound_ref", c.get("id", ""))).split("/")[-1]
                        if not cid:
                            continue
                        bounds[cid] = (
                            -1 * c.get("maxFlux", 100),
                            -1 * c.get("minFlux", -100),
                        )
                    return MSMedia.from_dict(bounds)
                return MSMedia.from_dict(m)
            # Assume an attribute-style KBase media object instance
            return MSMedia.from_kbase_object(m)

        mdlutl = self._check_and_convert_model(model)
        cobra_model = mdlutl.model
        objstr = f"MAX{{{biomass}}}"

        summary = {"accuracy": None, "CP": 0, "CN": 0, "FP": 0, "FN": 0, "P": 0, "N": 0}
        rows = []

        for mid, raw in media.items():
            try:
                msmedia = _coerce_media(raw)
            except Exception as e:
                self.log_info(f"simulate_growth_phenotypes: skipping media '{mid}': {e}")
                continue
            # Isolate all bound / reaction changes to this media only.
            with cobra_model:
                if add_missing_exchanges:
                    mdlutl.add_missing_exchanges(msmedia)
                self.set_media(mdlutl, msmedia)
                self.set_objective_from_string(mdlutl, objstr)
                flux = cobra_model.slim_optimize()
            # slim_optimize returns nan on infeasible; nan-safe growth call.
            sim_growth = bool(flux is not None and flux == flux and flux > growth_threshold)

            obs_val = None if observed is None else observed.get(mid)
            obs_growth = None if obs_val is None else bool(float(obs_val) > observed_threshold)

            if obs_growth is None:
                cls = "P" if sim_growth else "N"
            elif obs_growth and sim_growth:
                cls = "CP"
            elif (not obs_growth) and (not sim_growth):
                cls = "CN"
            elif (not obs_growth) and sim_growth:
                cls = "FP"
            else:
                cls = "FN"
            summary[cls] += 1

            rows.append({
                "media_id": mid,
                "simulated_flux": None if flux is None or flux != flux else float(flux),
                "simulated_growth": sim_growth,
                "observed_value": None if obs_val is None else float(obs_val),
                "observed_growth": obs_growth,
                "class": cls,
            })

        scored = summary["CP"] + summary["CN"] + summary["FP"] + summary["FN"]
        if scored > 0:
            summary["accuracy"] = (summary["CP"] + summary["CN"]) / scored
        return {"details": rows, "summary": summary}

    def test_production_potential(self, model, media=None, threshold=1e-6):
        """Test whether each cytosolic metabolite can be produced.

        For each cytosolic (_c or _c0) metabolite, adds a temporary drain
        reaction (DM_tmp_<met_id>), maximizes its flux, records the metabolite
        as producible if flux > threshold, then removes the reaction.  Uses a
        cobra context manager to guarantee no leaked reactions.

        Growth is NOT required during this test.

        Args:
            model: cobra.Model or MSModelUtil.
            media: Media to apply (optional).
            threshold: Minimum flux to call producible (default 1e-6).

        Returns:
            list of metabolite ids that can be produced.
        """
        import cobra

        mdlutl = self._check_and_convert_model(model)
        cobra_model = mdlutl.model

        if media is not None:
            self.set_media(mdlutl, media)

        # Find cytosolic metabolites
        cytosolic_mets = []
        for met in cobra_model.metabolites:
            (base_id, compartment, index) = self._parse_id(met.id)
            if compartment == "c":
                cytosolic_mets.append(met)

        self.log_info(
            f"test_production_potential: testing {len(cytosolic_mets)} cytosolic metabolites"
        )

        original_objective = cobra_model.objective
        producible = []

        for met in cytosolic_mets:
            dm_id = f"DM_tmp_{met.id}"
            with cobra_model:
                # Add temporary drain: metabolite -> (empty)
                dm_rxn = cobra.Reaction(dm_id)
                dm_rxn.lower_bound = 0
                dm_rxn.upper_bound = 1000
                dm_rxn.add_metabolites({met: -1})
                cobra_model.add_reactions([dm_rxn])
                # Maximize drain flux
                cobra_model.objective = dm_rxn
                cobra_model.objective.direction = "max"
                flux = cobra_model.slim_optimize()
                if flux is not None and not (flux != flux) and flux > threshold:
                    producible.append(met.id)
            # Context manager guarantees reaction is removed here

        cobra_model.objective = original_objective
        self.log_info(
            f"test_production_potential: {len(producible)} producible metabolites"
        )
        return producible

    def test_degradation_potential(self, model, media=None, threshold=1e-6):
        """Test whether each cytosolic metabolite can be degraded (consumed).

        For each cytosolic (_c or _c0) metabolite, adds a temporary source
        reaction (SK_tmp_<met_id>), maximizes its flux, records the metabolite
        as consumable if flux > threshold, then removes the reaction.  Uses a
        cobra context manager to guarantee no leaked reactions.

        Growth is NOT required during this test.

        Args:
            model: cobra.Model or MSModelUtil.
            media: Media to apply (optional).
            threshold: Minimum flux to call consumable (default 1e-6).

        Returns:
            list of metabolite ids that can be degraded.
        """
        import cobra

        mdlutl = self._check_and_convert_model(model)
        cobra_model = mdlutl.model

        if media is not None:
            self.set_media(mdlutl, media)

        # Find cytosolic metabolites
        cytosolic_mets = []
        for met in cobra_model.metabolites:
            (base_id, compartment, index) = self._parse_id(met.id)
            if compartment == "c":
                cytosolic_mets.append(met)

        self.log_info(
            f"test_degradation_potential: testing {len(cytosolic_mets)} cytosolic metabolites"
        )

        original_objective = cobra_model.objective
        consumable = []

        for met in cytosolic_mets:
            sk_id = f"SK_tmp_{met.id}"
            with cobra_model:
                # Add temporary source: (empty) -> metabolite
                sk_rxn = cobra.Reaction(sk_id)
                sk_rxn.lower_bound = 0
                sk_rxn.upper_bound = 1000
                sk_rxn.add_metabolites({met: 1})
                cobra_model.add_reactions([sk_rxn])
                # Maximize source flux = maximize how much the network can consume
                cobra_model.objective = sk_rxn
                cobra_model.objective.direction = "max"
                flux = cobra_model.slim_optimize()
                if flux is not None and not (flux != flux) and flux > threshold:
                    consumable.append(met.id)
            # Context manager guarantees reaction is removed here

        cobra_model.objective = original_objective
        self.log_info(
            f"test_degradation_potential: {len(consumable)} consumable metabolites"
        )
        return consumable

# ── Composition-based implementation ─────────────────────────────────────

class MSFBAUtilsImpl:
    """Composition-based FBA utilities.

    Holds ``env`` and ``model`` instead of inheriting from ``KBModelUtils``.
    Delegates all method calls to an internal legacy instance.

    **AP3 carve-outs preserved**:
    - ``run_fva`` — working FVA implementation (cobra.flux_variability_analysis is broken)
    - ``analyzed_reaction_objective_coupling`` — KO-impact-on-biomass analysis
    - ``fit_flux_to_mutant_growth_rate_data`` — specific science code
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
            self._delegate = MSFBAUtils(**_kwargs)
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
            raise RuntimeError("MSFBAUtilsImpl: delegate not initialized (missing cobrakbase/modelseedpy)")
        return getattr(self._delegate, name)
