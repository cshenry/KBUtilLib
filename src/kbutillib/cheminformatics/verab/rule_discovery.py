"""verAB O-demethylation rule discovery from Pickaxe network expansions.

This module provides two public functions:

* :func:`match_transformation` — classifies each :class:`~kbutillib.cheminformatics.base.PredictedReaction`
  in an :class:`~kbutillib.cheminformatics.base.ExpansionResult` as a verAB
  aryl methyl ether O-demethylation event (or not) and returns a list of
  :class:`~kbutillib.cheminformatics.verab.models.VerabRuleMatch`.

* :func:`discover_verab_rules` — high-level orchestrator: expand seed compounds
  with a given expander, run :func:`match_transformation`, and return a
  :class:`~kbutillib.cheminformatics.verab.models.VerabDiscoveryResult`.

RDKit-lazy contract
-------------------
Neither function imports RDKit at module load time. RDKit is imported inside
function bodies only when it is available; if it is absent the functions fall
back to keyword/text matching on the rule SMARTS / operator string and emit a
warning on the returned result.

No hard import of ``minedatabase`` here either — the expander object is treated
as a plain duck-type collaborator.
"""

from __future__ import annotations

import importlib.util
import logging
import warnings as _warnings_mod
from typing import Any, Dict, List, Optional, Sequence

from .models import VerabDiscoveryResult, VerabRuleMatch
from .smarts import (
    METHOXY_AROMATIC_SMARTS,
    SEED_COMPOUNDS,
    VERAB_ODEMETHYLATION_SMARTS,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RDKit availability probe (at call-time, not at import-time)
# ---------------------------------------------------------------------------

_RDKIT_CHECKED: bool = False
_RDKIT_AVAILABLE: bool = False


def _rdkit_available() -> bool:
    """Return True iff ``rdkit`` can be imported (result cached after first call)."""
    global _RDKIT_CHECKED, _RDKIT_AVAILABLE
    if not _RDKIT_CHECKED:
        _RDKIT_AVAILABLE = importlib.util.find_spec("rdkit") is not None
        _RDKIT_CHECKED = True
    return _RDKIT_AVAILABLE


# ---------------------------------------------------------------------------
# Internal chemistry helpers (all RDKit calls gated behind _rdkit_available())
# ---------------------------------------------------------------------------

# SMARTS patterns used for the three-criteria structural test.
#   (a) aromatic methoxy on reactant
_SMARTS_METHOXY_AROM = METHOXY_AROMATIC_SMARTS          # "[c]-[OX2]-[CH3]"
#   (b) aromatic hydroxyl (phenol) on product
_SMARTS_PHENOL       = "[c]-[OH1]"
#   (c) formaldehyde / methanal on product  (matches C=O with exactly 1 C)
_SMARTS_FORMALDEHYDE = "[CH2]=O"

#: Keywords used for the text-only fallback classifier.
_TEXT_KEYWORDS = (
    "demethyl",
    "o-demethyl",
    "methox",
    "monooxygenase",
    "vanillate",
    "1.14.13",
    "odemethyl",
    "ether cleavage",
)


def _mol_from_smiles(smiles: str) -> Any:
    """Return an RDKit Mol from *smiles*, or None on failure. RDKit must be available."""
    from rdkit import Chem  # lazy import
    return Chem.MolFromSmiles(smiles)


def _has_match(mol: Any, smarts_str: str) -> bool:
    """Return True iff *mol* matches *smarts_str* substructure. RDKit must be available."""
    from rdkit import Chem  # lazy import
    pattern = Chem.MolFromSmarts(smarts_str)
    if pattern is None:
        return False
    return mol.HasSubstructMatch(pattern)


def _is_verab_rdkit(
    reactant_mols: List[Any],
    product_mols: List[Any],
) -> bool:
    """Return True iff the (reactants, products) set realises the verAB O-demethylation.

    Criteria (design §4 step 3):
    (a) At least one reactant has an aromatic methoxy group (``[c]-[OX2]-[CH3]``).
    (b) At least one product has a phenol group (``[c]-[OH1]``).
    (c) At least one product is formaldehyde / methanal (``[CH2]=O``).

    All three criteria must hold simultaneously.
    """
    # (a) aromatic methoxy on a reactant
    if not any(_has_match(mol, _SMARTS_METHOXY_AROM) for mol in reactant_mols if mol is not None):
        return False
    # (b) phenol on a product
    if not any(_has_match(mol, _SMARTS_PHENOL) for mol in product_mols if mol is not None):
        return False
    # (c) formaldehyde in the product set
    if not any(_has_match(mol, _SMARTS_FORMALDEHYDE) for mol in product_mols if mol is not None):
        return False
    return True


def _is_verab_text(rule_smarts: Optional[str], operator: Optional[str]) -> bool:
    """Fallback classifier: keyword match on the rule SMARTS / operator string."""
    haystack = " ".join(
        s.lower()
        for s in [rule_smarts or "", operator or ""]
    )
    return any(kw in haystack for kw in _TEXT_KEYWORDS)


# ---------------------------------------------------------------------------
# match_transformation
# ---------------------------------------------------------------------------


def match_transformation(
    result: Any,
    target_smarts: str = VERAB_ODEMETHYLATION_SMARTS,
    *,
    mode: str = "product",
) -> List[VerabRuleMatch]:
    """Classify each :class:`~kbutillib.cheminformatics.base.PredictedReaction`
    in *result* as a verAB O-demethylation event.

    For each :class:`~kbutillib.cheminformatics.base.PredictedReaction` the
    function reconstructs the reactant and product molecules from
    ``result.compounds[id].smiles`` and applies the three-criteria structural
    test (aromatic methoxy reactant, phenol product, formaldehyde product) when
    RDKit is available.  When RDKit is absent it falls back to keyword matching
    on ``reaction.rule_smarts`` / ``reaction.operator`` and emits a
    :func:`warnings.warn` with a ``UserWarning``.

    Parameters
    ----------
    result:
        An :class:`~kbutillib.cheminformatics.base.ExpansionResult`.
    target_smarts:
        The reaction SMARTS string describing the target transformation (unused
        by the text fallback, kept for API symmetry).
    mode:
        Reserved for future multi-mode matching; currently only ``"product"``
        is recognised.

    Returns
    -------
    list[VerabRuleMatch]
        One :class:`~kbutillib.cheminformatics.verab.models.VerabRuleMatch` per
        *firing* reaction (i.e. reactions that pass the verAB classification).
    """
    matches: List[VerabRuleMatch] = []
    use_rdkit = _rdkit_available()

    if not use_rdkit:
        msg = (
            "RDKit is not available; match_transformation is using text/keyword "
            "matching (method='smarts_text', confidence=0.5). Install RDKit for "
            "structural confirmation."
        )
        _warnings_mod.warn(msg, UserWarning, stacklevel=2)

    compounds: Dict[str, Any] = result.compounds  # id -> PredictedCompound

    for rxn in result.reactions:
        # ---- collect compound SMILES -------------------------------------------
        reactant_ids: List[str] = list(rxn.reactant_ids)
        product_ids: List[str] = list(rxn.product_ids)

        operator: Optional[str] = rxn.operator
        # Skip reactions with no operator (can't be attributed to a rule)
        if operator is None:
            continue

        # ---- RDKit structural match --------------------------------------------
        if use_rdkit:
            reactant_mols = []
            for cid in reactant_ids:
                cpd = compounds.get(cid)
                smi = cpd.smiles if cpd is not None else None
                mol = _mol_from_smiles(smi) if smi else None
                reactant_mols.append(mol)

            product_mols = []
            for cid in product_ids:
                cpd = compounds.get(cid)
                smi = cpd.smiles if cpd is not None else None
                mol = _mol_from_smiles(smi) if smi else None
                product_mols.append(mol)

            is_verab = _is_verab_rdkit(reactant_mols, product_mols)
            method = "rdkit_transform"
            confidence = 1.0
        else:
            # ---- text / keyword fallback ---------------------------------------
            is_verab = _is_verab_text(
                getattr(rxn, "rule_smarts", None),
                operator,
            )
            method = "smarts_text"
            confidence = 0.5

        if is_verab:
            matches.append(
                VerabRuleMatch(
                    operator=operator,
                    reaction_id=rxn.reaction_id,
                    backend=rxn.backend,
                    reactant_ids=reactant_ids,
                    product_ids=product_ids,
                    method=method,
                    confidence=confidence,
                    ec_hint=None,   # resolved by caller / discover_verab_rules
                    raw=rxn.to_dict(),
                )
            )

    return matches


# ---------------------------------------------------------------------------
# discover_verab_rules
# ---------------------------------------------------------------------------


def discover_verab_rules(
    expander: Any,
    *,
    generations: int = 1,
    rule_set: str = "metacyc_generalized",
    seeds: Optional[Sequence[Dict[str, Any]]] = None,
    backend: str = "pickaxe",
    ec_hint: str = "1.14.13.82",
) -> VerabDiscoveryResult:
    """Expand seed compounds and discover verAB O-demethylation operators.

    Parameters
    ----------
    expander:
        Any object with an ``.expand(seed_smiles, generations, backend=...,
        rule_set=...) -> ExpansionResult`` method.  In production this is the
        :class:`~kbutillib.network_expansion_utils.NetworkExpansionUtils`
        facade; in tests it is a fake/mock.
    generations:
        Number of expansion generations to run.
    rule_set:
        Rule set name passed through to *expander* (e.g.
        ``"metacyc_generalized"`` or ``"metacyc_intermediate"``).
    seeds:
        Iterable of seed dicts (each with ``"id"`` and ``"smiles"`` keys).
        Defaults to :data:`~kbutillib.cheminformatics.verab.smarts.SEED_COMPOUNDS`.
    backend:
        Backend name to request from *expander* (default ``"pickaxe"``).
    ec_hint:
        EC number to attach to every :class:`~kbutillib.cheminformatics.verab.models.VerabRuleMatch`
        produced by this run (default ``"1.14.13.82"`` for vanillate
        monooxygenase).

    Returns
    -------
    VerabDiscoveryResult
    """
    if seeds is None:
        seeds = SEED_COMPOUNDS

    seed_list: List[Dict[str, Any]] = list(seeds)
    seed_smiles: Dict[str, str] = {
        s["id"]: s["smiles"] for s in seed_list if s.get("smiles")
    }

    # Run expansion
    result = expander.expand(
        seed_smiles,
        generations=generations,
        backend=backend,
        rule_set=rule_set,
    )

    # Classify reactions
    matches = match_transformation(result)

    # Attach ec_hint to every match and de-duplicate operators
    unique_operators: List[str] = []
    seen_ops: set = set()
    for m in matches:
        m.ec_hint = ec_hint
        if m.operator not in seen_ops:
            seen_ops.add(m.operator)
            unique_operators.append(m.operator)

    # Build expansion summary
    expansion_summary: Dict[str, Any] = {
        "n_compounds": result.n_compounds,
        "n_reactions": result.n_reactions,
        "warnings": list(result.warnings),
    }

    # Aggregate warnings
    accumulated_warnings: List[str] = list(result.warnings)
    if not _rdkit_available():
        accumulated_warnings.append(
            "RDKit absent: operator matching used text/keyword fallback "
            "(method='smarts_text', confidence=0.5)."
        )

    return VerabDiscoveryResult(
        rule_set=rule_set,
        generations=generations,
        seeds=seed_list,
        matches=matches,
        operators=unique_operators,
        expansion_summary=expansion_summary,
        warnings=accumulated_warnings,
    )
