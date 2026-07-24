"""Reusable notebook helpers — compartment, reaction, and FVA utilities.

These were promoted from project-level util.py files so that every
notebook project gets them for free via ``kbutillib.notebook.helpers``.
"""

from .compartment import COMPARTMENT_MAP, normalize_compartment
from .fva import classify_fva_flux, find_significant_differences
from .reaction import (
    build_gene_reaction_map,
    compare_reaction_stoichiometry,
    get_exchange_map,
    get_reaction_directionality,
    is_diffusion_reaction,
    reaction_equation_with_names,
    standardize_exchange_id,
)

__all__ = [
    "COMPARTMENT_MAP",
    "normalize_compartment",
    "get_reaction_directionality",
    "standardize_exchange_id",
    "get_exchange_map",
    "build_gene_reaction_map",
    "reaction_equation_with_names",
    "is_diffusion_reaction",
    "compare_reaction_stoichiometry",
    "find_significant_differences",
    "classify_fva_flux",
]
