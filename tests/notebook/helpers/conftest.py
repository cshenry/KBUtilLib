"""Shared pytest fixtures for notebook helpers tests."""
from __future__ import annotations

import pytest
import cobra


@pytest.fixture
def mini_model() -> cobra.Model:
    """A minimal synthetic cobra.Model for testing util helpers."""
    model = cobra.Model("test_model")

    # Metabolites
    a_c = cobra.Metabolite("a_c", name="A", compartment="c")
    a_e = cobra.Metabolite("a_e", name="A", compartment="e")
    b_c = cobra.Metabolite("b_c", name="B", compartment="c")
    c_c = cobra.Metabolite("c_c", name="C", compartment="c")

    # Reversible reaction: A_c + B_c <=> 2 C_c
    rxn1 = cobra.Reaction("R1")
    rxn1.name = "Reaction 1"
    rxn1.lower_bound = -1000
    rxn1.upper_bound = 1000
    rxn1.add_metabolites({a_c: -1, b_c: -1, c_c: 2})

    # Forward-only reaction: A_c => B_c
    rxn2 = cobra.Reaction("R2")
    rxn2.name = "Reaction 2"
    rxn2.lower_bound = 0
    rxn2.upper_bound = 1000
    rxn2.add_metabolites({a_c: -1, b_c: 1})

    # Blocked reaction
    rxn_blocked = cobra.Reaction("R_blocked")
    rxn_blocked.lower_bound = 0
    rxn_blocked.upper_bound = 0
    rxn_blocked.add_metabolites({a_c: -1, b_c: 1})

    # Reverse-only reaction
    rxn_rev = cobra.Reaction("R_reverse")
    rxn_rev.lower_bound = -1000
    rxn_rev.upper_bound = 0
    rxn_rev.add_metabolites({a_c: -1, b_c: 1})

    # Diffusion: A_c <=> A_e
    rxn_diff = cobra.Reaction("diffusion_a")
    rxn_diff.lower_bound = -1000
    rxn_diff.upper_bound = 1000
    rxn_diff.add_metabolites({a_c: -1, a_e: 1})

    # Exchange: EX_a_e0
    rxn_ex = cobra.Reaction("EX_a_e0")
    rxn_ex.lower_bound = -1000
    rxn_ex.upper_bound = 1000
    rxn_ex.add_metabolites({a_e: -1})

    model.add_reactions([rxn1, rxn2, rxn_blocked, rxn_rev, rxn_diff, rxn_ex])

    # Genes
    rxn1.gene_reaction_rule = "gene1 and gene2"
    rxn2.gene_reaction_rule = "gene1"

    return model
