"""Reusable notebook helpers — compartment, reaction, and FVA utilities.

These were promoted from project-level util.py files so that every
notebook project gets them for free via ``kbutillib.notebook.helpers``.
"""

from .compartment import COMPARTMENT_MAP, normalize_compartment
from .reaction import (
    get_reaction_directionality,
    standardize_exchange_id,
    get_exchange_map,
    build_gene_reaction_map,
    reaction_equation_with_names,
    is_diffusion_reaction,
    compare_reaction_stoichiometry,
)
from .fva import find_significant_differences, classify_fva_flux

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
