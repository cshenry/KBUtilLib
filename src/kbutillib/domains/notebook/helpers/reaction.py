"""Reaction-related notebook helpers."""
from __future__ import annotations

import re
from typing import Any

import numpy as np

try:
    import cobra
except ImportError:
    cobra = None  # type: ignore


def get_reaction_directionality(reaction: "cobra.Reaction") -> str:
    """Classify reaction directionality based on its bounds.

    Returns one of: 'reversible', 'forward', 'reverse', 'blocked'.
    """
    if reaction.lower_bound < 0 and reaction.upper_bound > 0:
        return "reversible"
    elif reaction.lower_bound >= 0 and reaction.upper_bound > 0:
        return "forward"
    elif reaction.lower_bound < 0 and reaction.upper_bound <= 0:
        return "reverse"
    else:
        return "blocked"


_EXCHANGE_RE = re.compile(r"^EX_(.+?)(?:_e0?)?$")


def standardize_exchange_id(reaction_id: str) -> str:
    """Normalize exchange reaction IDs to the form 'EX_<met_id>_e0'.

    If *reaction_id* does not look like an exchange reaction, return it as-is.
    """
    m = _EXCHANGE_RE.match(reaction_id)
    if m:
        met_base = m.group(1)
        # Strip trailing compartment tag if already present
        met_base = re.sub(r"_e0?$", "", met_base)
        return f"EX_{met_base}_e0"
    return reaction_id


def get_exchange_map(model: "cobra.Model") -> dict[str, "cobra.Reaction"]:
    """Return a mapping of standardized exchange id -> Reaction for all exchanges."""
    result: dict[str, "cobra.Reaction"] = {}
    for rxn in model.exchanges:
        std_id = standardize_exchange_id(rxn.id)
        result[std_id] = rxn
    return result


def build_gene_reaction_map(model: "cobra.Model") -> dict[str, list[str]]:
    """Build a dict mapping gene id -> list of reaction ids that use that gene."""
    gene_rxn_map: dict[str, list[str]] = {}
    for gene in model.genes:
        gene_rxn_map[gene.id] = [rxn.id for rxn in gene.reactions]
    return gene_rxn_map


def reaction_equation_with_names(reaction: "cobra.Reaction") -> str:
    """Build a human-readable reaction equation using metabolite names."""
    def _format_met(met: "cobra.Metabolite", coeff: float) -> str:
        abs_coeff = abs(coeff)
        name = met.name if met.name else met.id
        if abs_coeff == 1.0:
            return name
        # Use integer display when possible
        if abs_coeff == int(abs_coeff):
            return f"{int(abs_coeff)} {name}"
        return f"{abs_coeff} {name}"

    reactants = []
    products = []
    for met, coeff in reaction.metabolites.items():
        if coeff < 0:
            reactants.append(_format_met(met, coeff))
        else:
            products.append(_format_met(met, coeff))

    arrow = " <=> " if reaction.reversibility else " => "
    return " + ".join(reactants) + arrow + " + ".join(products)


def is_diffusion_reaction(reaction: "cobra.Reaction") -> bool:
    """Return True if the reaction is a simple diffusion (same metabolite in two compartments)."""
    mets = list(reaction.metabolites.keys())
    if len(mets) != 2:
        return False
    # Check same base metabolite in different compartments
    id0 = re.sub(r"_[a-z]\d?$", "", mets[0].id)
    id1 = re.sub(r"_[a-z]\d?$", "", mets[1].id)
    if id0 != id1:
        return False
    # Coefficients should be +1 and -1
    coeffs = sorted(reaction.metabolites.values())
    return coeffs == [-1.0, 1.0] or coeffs == [-1, 1]


def compare_reaction_stoichiometry(
    rxn_a: "cobra.Reaction",
    rxn_b: "cobra.Reaction",
) -> dict[str, Any]:
    """Compare stoichiometry of two reactions.

    Returns a dict with keys:
        - identical: bool
        - only_in_a: list of met ids
        - only_in_b: list of met ids
        - coefficient_diffs: dict of met_id -> (coeff_a, coeff_b)
    """
    mets_a = {met.id: coeff for met, coeff in rxn_a.metabolites.items()}
    mets_b = {met.id: coeff for met, coeff in rxn_b.metabolites.items()}

    only_in_a = sorted(set(mets_a) - set(mets_b))
    only_in_b = sorted(set(mets_b) - set(mets_a))

    coefficient_diffs: dict[str, tuple[float, float]] = {}
    for met_id in set(mets_a) & set(mets_b):
        if not np.isclose(mets_a[met_id], mets_b[met_id]):
            coefficient_diffs[met_id] = (mets_a[met_id], mets_b[met_id])

    identical = not only_in_a and not only_in_b and not coefficient_diffs
    return {
        "identical": identical,
        "only_in_a": only_in_a,
        "only_in_b": only_in_b,
        "coefficient_diffs": coefficient_diffs,
    }
