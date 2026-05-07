"""Tests for kbutillib.notebook.helpers.reaction."""
from __future__ import annotations

from kbutillib.notebook.helpers.reaction import (
    build_gene_reaction_map,
    compare_reaction_stoichiometry,
    get_exchange_map,
    get_reaction_directionality,
    is_diffusion_reaction,
    reaction_equation_with_names,
    standardize_exchange_id,
)


class TestDirectionality:
    def test_reversible(self, mini_model):
        rxn = mini_model.reactions.get_by_id("R1")
        assert get_reaction_directionality(rxn) == "reversible"

    def test_forward(self, mini_model):
        rxn = mini_model.reactions.get_by_id("R2")
        assert get_reaction_directionality(rxn) == "forward"

    def test_reverse(self, mini_model):
        rxn = mini_model.reactions.get_by_id("R_reverse")
        assert get_reaction_directionality(rxn) == "reverse"

    def test_blocked(self, mini_model):
        rxn = mini_model.reactions.get_by_id("R_blocked")
        assert get_reaction_directionality(rxn) == "blocked"


class TestExchangeId:
    def test_already_standard(self):
        assert standardize_exchange_id("EX_glc__D_e0") == "EX_glc__D_e0"

    def test_without_compartment_suffix(self):
        assert standardize_exchange_id("EX_glc__D") == "EX_glc__D_e0"

    def test_with_e_suffix(self):
        assert standardize_exchange_id("EX_glc__D_e") == "EX_glc__D_e0"

    def test_non_exchange(self):
        assert standardize_exchange_id("R_PFK") == "R_PFK"


class TestExchangeMap:
    def test_builds_map(self, mini_model):
        emap = get_exchange_map(mini_model)
        assert "EX_a_e0" in emap
        assert emap["EX_a_e0"].id == "EX_a_e0"


class TestGeneReactionMap:
    def test_map_contents(self, mini_model):
        grm = build_gene_reaction_map(mini_model)
        assert "gene1" in grm
        assert "gene2" in grm
        assert "R1" in grm["gene1"]
        assert "R2" in grm["gene1"]
        assert "R1" in grm["gene2"]
        assert "R2" not in grm["gene2"]


class TestReactionEquation:
    def test_reversible(self, mini_model):
        rxn = mini_model.reactions.get_by_id("R1")
        eq = reaction_equation_with_names(rxn)
        assert "<=>" in eq
        assert "A" in eq
        assert "B" in eq
        assert "2 C" in eq

    def test_forward_only(self, mini_model):
        rxn = mini_model.reactions.get_by_id("R2")
        eq = reaction_equation_with_names(rxn)
        assert "=>" in eq


class TestDiffusion:
    def test_is_diffusion(self, mini_model):
        rxn = mini_model.reactions.get_by_id("diffusion_a")
        assert is_diffusion_reaction(rxn) is True

    def test_not_diffusion_multiple_mets(self, mini_model):
        rxn = mini_model.reactions.get_by_id("R1")
        assert is_diffusion_reaction(rxn) is False

    def test_not_diffusion_exchange(self, mini_model):
        rxn = mini_model.reactions.get_by_id("EX_a_e0")
        assert is_diffusion_reaction(rxn) is False


class TestStoichiometryCompare:
    def test_identical(self, mini_model):
        rxn = mini_model.reactions.get_by_id("R2")
        result = compare_reaction_stoichiometry(rxn, rxn)
        assert result["identical"] is True
        assert result["only_in_a"] == []
        assert result["only_in_b"] == []
        assert result["coefficient_diffs"] == {}

    def test_different(self, mini_model):
        rxn_a = mini_model.reactions.get_by_id("R1")
        rxn_b = mini_model.reactions.get_by_id("R2")
        result = compare_reaction_stoichiometry(rxn_a, rxn_b)
        assert result["identical"] is False
