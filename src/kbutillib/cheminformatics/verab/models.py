"""Data models for the verAB methoxy-aromatic Pickaxe rule discovery workflow.

All dataclasses here are pure Python (stdlib only) with ``to_dict()`` methods
returning plain JSON-serialisable dicts.  No RDKit, no minedatabase, no heavy
dependency is imported at module load time — or anywhere in this file.

Style mirrors ``kbutillib.cheminformatics.base`` (PredictedCompound,
PredictedReaction, ExpansionResult) so outputs compose naturally with the
broader cheminformatics result types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# VerabRuleMatch
# ---------------------------------------------------------------------------


@dataclass
class VerabRuleMatch:
    """A single predicted reaction classified as a verAB O-demethylation event.

    Attributes
    ----------
    operator:
        The firing rule/operator id from Pickaxe (``PredictedReaction.operator``).
    reaction_id:
        The predicted reaction id from the expansion.
    backend:
        Name of the expansion backend that produced this reaction (e.g.
        ``"pickaxe"``).
    reactant_ids:
        Compound ids of the reactants as returned by the expansion.
    product_ids:
        Compound ids of the products as returned by the expansion.
    method:
        How the classification was made: ``"rdkit_transform"`` for an
        RDKit-confirmed structural match, ``"smarts_text"`` for a text/keyword
        fallback when RDKit is absent.
    confidence:
        Confidence score: ``1.0`` for RDKit-confirmed, ``< 1.0`` for the text
        fallback path.
    ec_hint:
        EC number hint (e.g. ``"1.14.13.82"`` for vanillate monooxygenase), or
        ``None`` if not resolvable from the rule's metadata.
    raw:
        The raw ``PredictedReaction.to_dict()`` dict for debugging/provenance.
    """

    operator: str
    reaction_id: str
    backend: str
    reactant_ids: List[str] = field(default_factory=list)
    product_ids: List[str] = field(default_factory=list)
    method: str = "rdkit_transform"
    confidence: float = 1.0
    ec_hint: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain JSON-serialisable dict."""
        return {
            "operator": self.operator,
            "reaction_id": self.reaction_id,
            "backend": self.backend,
            "reactant_ids": list(self.reactant_ids),
            "product_ids": list(self.product_ids),
            "method": self.method,
            "confidence": self.confidence,
            "ec_hint": self.ec_hint,
        }


# ---------------------------------------------------------------------------
# VerabDiscoveryResult
# ---------------------------------------------------------------------------


@dataclass
class VerabDiscoveryResult:
    """Aggregated result of a verAB rule-discovery run.

    Attributes
    ----------
    rule_set:
        The Pickaxe rule set used (e.g. ``"metacyc_generalized"``).
    generations:
        Number of expansion generations run.
    seeds:
        The seed compound rows that were expanded (id/name/smiles/inchikey/kegg).
    matches:
        All :class:`VerabRuleMatch` instances found.
    operators:
        De-duplicated list of firing operator ids.
    expansion_summary:
        Compact dict with ``n_compounds``, ``n_reactions``, and ``warnings``
        from the underlying :class:`~kbutillib.cheminformatics.base.ExpansionResult`.
    warnings:
        Accumulated warnings from matching and expansion.
    """

    rule_set: str
    generations: int
    seeds: List[Dict[str, Any]] = field(default_factory=list)
    matches: List[VerabRuleMatch] = field(default_factory=list)
    operators: List[str] = field(default_factory=list)
    expansion_summary: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain JSON-serialisable dict."""
        return {
            "rule_set": self.rule_set,
            "generations": self.generations,
            "seeds": list(self.seeds),
            "matches": [m.to_dict() for m in self.matches],
            "operators": list(self.operators),
            "expansion_summary": dict(self.expansion_summary),
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# ScreeningRecord
# ---------------------------------------------------------------------------


@dataclass
class ScreeningRecord:
    """Cross-referencing result for a single predicted product compound.

    One record is created per (source compound × operator × predicted product)
    triple during Phase-2 screening.

    Attributes
    ----------
    source_msid:
        ModelSEED compound id of the source methoxy-aromatic.
    source_smiles:
        SMILES of the source compound.
    operator:
        The firing operator/rule id used to generate ``product_smiles``.
    product_smiles:
        SMILES of the predicted product.
    product_inchikey:
        InChIKey of the product, or ``None`` if not computable.
    reaction_in_db:
        ModelSEED reaction id (MSRXN) if the predicted reaction is already in
        the biochem DB, else ``None``.
    product_in_db:
        ModelSEED compound id (MSCPD) if the product is already in the biochem
        DB, else ``None``.
    has_downstream_pathway:
        ``True`` if at least one further-degradation reaction consuming the
        product exists in the biochem DB.
    downstream_reactions:
        List of MSRXN ids of reactions that consume the product (phase c).
    in_models:
        Mapping of model/genome reference -> ``True``/``False`` indicating
        whether the downstream pathway is present in that model (phase d).
    """

    source_msid: str
    source_smiles: str
    operator: str
    product_smiles: str
    product_inchikey: Optional[str] = None
    reaction_in_db: Optional[str] = None
    product_in_db: Optional[str] = None
    has_downstream_pathway: bool = False
    downstream_reactions: List[str] = field(default_factory=list)
    in_models: Dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain JSON-serialisable dict."""
        return {
            "source_msid": self.source_msid,
            "source_smiles": self.source_smiles,
            "operator": self.operator,
            "product_smiles": self.product_smiles,
            "product_inchikey": self.product_inchikey,
            "reaction_in_db": self.reaction_in_db,
            "product_in_db": self.product_in_db,
            "has_downstream_pathway": self.has_downstream_pathway,
            "downstream_reactions": list(self.downstream_reactions),
            "in_models": dict(self.in_models),
        }


# ---------------------------------------------------------------------------
# ScreeningReport
# ---------------------------------------------------------------------------


@dataclass
class ScreeningReport:
    """Aggregated result of a Phase-2 verAB methoxy-aromatic screening run.

    Attributes
    ----------
    n_source_compounds:
        Number of source methoxy-aromatic compounds that were screened.
    records:
        Per-product :class:`ScreeningRecord` entries.
    genome_predictions:
        Mapping of genome/model reference to a dict with keys ``can_degrade``
        (list of source MSIDs predicted degradable) and ``ec_hits`` (list of EC
        terms found in the genome's annotation).
    warnings:
        Accumulated warnings from screening.
    """

    n_source_compounds: int = 0
    records: List[ScreeningRecord] = field(default_factory=list)
    genome_predictions: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain JSON-serialisable dict."""
        return {
            "n_source_compounds": self.n_source_compounds,
            "records": [r.to_dict() for r in self.records],
            "genome_predictions": dict(self.genome_predictions),
            "warnings": list(self.warnings),
        }
