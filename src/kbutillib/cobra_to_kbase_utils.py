"""Conversion utilities between COBRApy and KBase model formats.

This module provides functions for converting between COBRApy model objects
and KBase FBA model data structures. It includes utilities for:
- Converting COBRA reactions to KBase modelreactions
- Converting COBRA metabolites to KBase modelcompounds
- Building compartment and compound references
- Parsing and building GPR (Gene-Protein-Reaction) rules
"""

import math
from typing import Any, Dict, List, Optional, Tuple


def get_compartmets_references(model: Any) -> Dict[str, str]:
    """Build compartment ID to KBase reference mapping.

    Args:
        model: COBRApy model object

    Returns:
        Dictionary mapping compartment IDs to KBase modelcompartment references
    """
    compartments_to_refs = {}
    for c in model.compartments:
        if c not in compartments_to_refs:
            compartments_to_refs[c] = f"~/modelcompartments/id/{c}0"
    return compartments_to_refs


def get_compounds_references(model: Any) -> Dict[str, str]:
    """Build metabolite ID to KBase reference mapping.

    Args:
        model: COBRApy model object

    Returns:
        Dictionary mapping metabolite IDs to KBase modelcompound references
    """
    compounds_to_refs = {}
    for m in model.metabolites:
        if m.id not in compounds_to_refs:
            compounds_to_refs[m.id] = f"~/modelcompounds/id/{m.id}"
    return compounds_to_refs


def get_bounds(reaction: Any) -> Tuple[float, float, str]:
    """Extract flux bounds and direction from a COBRA reaction.

    Args:
        reaction: COBRApy reaction object

    Returns:
        Tuple of (maxrevflux, maxforflux, direction) where direction is
        '>' for forward, '<' for reverse, or '=' for reversible
    """
    maxrevflux = math.fabs(reaction.lower_bound)
    maxforflux = math.fabs(reaction.upper_bound)

    if maxrevflux == 0 and maxforflux > 0:
        direction = ">"
    elif maxrevflux > 0 and maxforflux == 0:
        direction = "<"
    else:
        direction = "="

    return maxrevflux, maxforflux, direction


def build_model_compound(
    metabolite: Any,
    compartments_to_refs: Dict[str, str],
) -> Dict[str, Any]:
    """Convert a COBRApy metabolite to KBase modelcompound format.

    Args:
        metabolite: COBRApy metabolite object
        compartments_to_refs: Mapping of compartment IDs to references

    Returns:
        KBase modelcompound dictionary
    """
    compound_ref = None
    formula = "*"

    if metabolite.formula is not None:
        formula = metabolite.formula

    # Check if this is a ModelSEED compound
    if metabolite.id.startswith("cpd"):
        compound_ref = f"~/template/compounds/id/{metabolite.id.split('_')[0].strip()}"

    # Get compartment reference
    modelcompartment_ref = "~/modelcompartments/id/c0"
    if metabolite.compartment in compartments_to_refs:
        modelcompartment_ref = compartments_to_refs[metabolite.compartment]
    else:
        print(f"Warning: undeclared compartment: {metabolite.compartment}")

    return {
        "aliases": [],
        "charge": metabolite.charge if hasattr(metabolite, "charge") else 0,
        "compound_ref": compound_ref,
        "dblinks": {},
        "formula": formula,
        "id": metabolite.id,
        "modelcompartment_ref": modelcompartment_ref,
        "name": metabolite.name if hasattr(metabolite, "name") else metabolite.id,
        "numerical_attributes": {},
        "string_attributes": {},
    }


def build_model_compartment(
    identifier: str,
    compartment_ref: str,
    label: str,
) -> Dict[str, Any]:
    """Build a KBase modelcompartment structure.

    Args:
        identifier: Compartment ID (e.g., 'c0', 'e0')
        compartment_ref: KBase compartment reference
        label: Human-readable compartment label

    Returns:
        KBase modelcompartment dictionary
    """
    return {
        "compartmentIndex": 0,
        "compartment_ref": compartment_ref,
        "id": identifier,
        "label": label,
        "pH": 7,
        "potencial": 0,
    }


def parse_gpr_string(gpr_string: str) -> List[List[str]]:
    """Parse a GPR string into a list of protein complexes.

    This is a simplified parser that handles basic GPR formats:
    - "gene1 or gene2" -> [[gene1], [gene2]]
    - "gene1 and gene2" -> [[gene1, gene2]]
    - "(gene1 and gene2) or gene3" -> [[gene1, gene2], [gene3]]

    Args:
        gpr_string: GPR rule string

    Returns:
        List of protein complexes, where each complex is a list of gene IDs
    """
    if not gpr_string or not gpr_string.strip():
        return []

    # Split by 'or' first (lower priority)
    or_parts = gpr_string.split(" or ")

    proteins = []
    for part in or_parts:
        # Clean up parentheses
        part = part.replace(")", "").replace("(", "").strip()

        # Split by 'and' (higher priority - forms protein complex)
        genes = []
        for gene in part.split(" and "):
            gene = gene.strip()
            if gene:
                genes.append(gene)

        if genes:
            proteins.append(genes)

    return proteins


def build_model_reaction_proteins(
    gene_sets: List[List[str]],
) -> List[Dict[str, Any]]:
    """Build KBase modelReactionProteins structure from gene sets.

    Args:
        gene_sets: List of protein complexes, each containing a list of gene IDs

    Returns:
        List of modelReactionProtein structures
    """
    model_reaction_proteins = []

    for gene_set in gene_sets:
        model_reaction_protein_subunits = []

        for gene in gene_set:
            subunit = {
                "feature_refs": [f"~/genome/features/id/{gene}"],
                "note": "",
                "optionalSubunit": 0,
                "role": "",
                "triggering": 1,
            }
            model_reaction_protein_subunits.append(subunit)

        model_reaction_protein = {
            "complex_ref": "~/template/complexes/name/cpx00000",
            "note": "",
            "source": "",
            "modelReactionProteinSubunits": model_reaction_protein_subunits,
        }
        model_reaction_proteins.append(model_reaction_protein)

    return model_reaction_proteins


def convert_to_kbase_reaction(
    reaction: Any,
    compounds_to_refs: Dict[str, str],
) -> Dict[str, Any]:
    """Convert a COBRApy reaction to KBase modelreaction format.

    Args:
        reaction: COBRApy reaction object
        compounds_to_refs: Mapping of metabolite IDs to references

    Returns:
        KBase modelreaction dictionary
    """
    # Build reagent list
    model_reaction_reagents = []
    for metabolite in reaction.metabolites:
        if metabolite.id in compounds_to_refs:
            model_reaction_reagent = {
                "coefficient": reaction.metabolites[metabolite],
                "modelcompound_ref": compounds_to_refs[metabolite.id],
            }
            model_reaction_reagents.append(model_reaction_reagent)
        else:
            print(f"Warning: discarded undeclared compound: {metabolite.id}")

    # Get bounds and direction
    maxrevflux, maxforflux, direction = get_bounds(reaction)

    # Build GPR
    model_reaction_proteins = []
    if hasattr(reaction, "gene_reaction_rule") and reaction.gene_reaction_rule:
        gene_sets = parse_gpr_string(reaction.gene_reaction_rule)
        model_reaction_proteins = build_model_reaction_proteins(gene_sets)

    # Build reaction ID (append compartment suffix if not present)
    reaction_id = reaction.id
    if not reaction_id.endswith("_c0") and not reaction_id.endswith("_e0"):
        reaction_id = f"{reaction_id}_c0"

    return {
        "aliases": [],
        "dblinks": {},
        "direction": direction,
        "edits": {},
        "gapfill_data": {},
        "id": reaction_id,
        "maxforflux": maxforflux,
        "maxrevflux": maxrevflux,
        "modelReactionProteins": model_reaction_proteins,
        "modelReactionReagents": model_reaction_reagents,
        "modelcompartment_ref": "~/modelcompartments/id/c0",
        "name": reaction.name if hasattr(reaction, "name") else reaction.id,
        "numerical_attributes": {},
        "probability": 0,
        "protons": 0,
        "reaction_ref": f"~/template/reactions/id/{reaction.id.split('_')[0]}",
        "string_attributes": {},
    }


def convert_cobra_model_to_kbase(
    model: Any,
    model_id: str,
    genome_ref: str = "38412/14/1",
    template_ref: str = "12998/1/2",
) -> Dict[str, Any]:
    """Convert a complete COBRApy model to KBase FBAModel format.

    Args:
        model: COBRApy model object
        model_id: ID for the KBase model
        genome_ref: Reference to the associated genome
        template_ref: Reference to the template used

    Returns:
        KBase FBAModel dictionary
    """
    modelcompartments = []
    modelcompounds = []
    modelreactions = []
    biomasses = []

    compartments_to_refs = get_compartmets_references(model)
    compounds_to_refs = get_compounds_references(model)

    # Convert compartments
    for c in model.compartments:
        modelcompartment = build_model_compartment(
            c + "0",
            compartments_to_refs[c],
            model.compartments[c] + "_0",
        )
        modelcompartments.append(modelcompartment)

    # Convert metabolites
    for m in model.metabolites:
        modelcompound = build_model_compound(m, compartments_to_refs)
        modelcompounds.append(modelcompound)

    # Convert reactions
    for r in model.reactions:
        modelreaction = convert_to_kbase_reaction(r, compounds_to_refs)
        if modelreaction is not None:
            modelreactions.append(modelreaction)

    return {
        "gapfilledcandidates": [],
        "gapgens": [],
        "gapfillings": [],
        "id": model_id,
        "genome_ref": genome_ref,
        "template_ref": template_ref,
        "template_refs": [template_ref],
        "name": model.name if hasattr(model, "name") else model_id,
        "type": "GenomeScale",
        "source": "cobrapy",
        "source_id": model.id if hasattr(model, "id") else model_id,
        "biomasses": biomasses,
        "modelcompartments": modelcompartments,
        "modelcompounds": modelcompounds,
        "modelreactions": modelreactions,
    }
