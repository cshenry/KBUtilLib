"""Phase-2 verAB methoxy-aromatic screening: product/reaction/pathway/model
cross-referencing.

The single public entry-point is :func:`screen_products`, which:

1. Runs network expansion (via *expander*) restricted/filtered to the supplied
   *operators*.
2. For each predicted product compound, answers the four design §5b screening
   questions:

   (a) **reaction_in_db** — is the predicted reaction already present in the
       ModelSEED biochem database? Answered via
       ``biochem.search_reactions(query_identifiers=[...])`` using the reaction
       id directly, or via a stoichiometry look-up.
   (b) **product_in_db** — is the product compound already in the database?
       Answered via ``biochem.search_compounds(query_structures=[smiles/inchikey])``.
   (c) **has_downstream_pathway** — are there database reactions that consume
       the product compound (i.e. further-degradation reactions)?  Answered via
       ``biochem.search_reactions(cpd_hits={mscpd: {}})``.
   (d) **in_models** — membership of the downstream reactions over each
       supplied model's ``model.model.reactions``, normalised via
       ``reaction_id_to_msid``.

3. Aggregates all :class:`~kbutillib.cheminformatics.verab.models.ScreeningRecord`
   entries into a :class:`~kbutillib.cheminformatics.verab.models.ScreeningReport`.

RDKit-lazy contract
-------------------
No RDKit import occurs at module load time.  InChIKey computation from a SMILES
string is attempted lazily when RDKit is available; if absent the field is
left ``None`` and a warning is recorded.

No minedatabase import at module load time — *expander* is a plain duck-type
collaborator.
"""

from __future__ import annotations

import importlib.util
import logging
from typing import Any, Dict, List, Optional, Sequence

from .models import ScreeningRecord, ScreeningReport

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RDKit availability probe
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


def _inchikey_from_smiles(smiles: str) -> Optional[str]:
    """Attempt to compute an InChIKey from *smiles* using RDKit.

    Returns ``None`` (silently) if RDKit is unavailable or the SMILES is
    invalid.
    """
    if not _rdkit_available() or not smiles:
        return None
    try:
        from rdkit import Chem  # lazy import
        from rdkit.Chem.inchi import MolToInchiKey  # lazy import

        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        return MolToInchiKey(mol)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _search_reactions_safe(
    biochem: Any,
    *,
    query_identifiers: Optional[List[str]] = None,
    cpd_hits: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Call ``biochem.search_reactions`` with graceful fallback to empty dict.

    Signature (from ms_biochem_utils.py:567-573):
        search_reactions(
            query_identifiers=[], query_ec=[], query_stoichiometry=None,
            cpd_hits=None, default_missing_count=1
        ) -> dict

    Parameters
    ----------
    biochem:
        Any object exposing ``search_reactions``.
    query_identifiers:
        List of reaction id strings to look up.
    cpd_hits:
        Dict mapping compound id -> existing match dict, used to find
        reactions consuming that compound (pass ``{mscpd_id: {}}``).
    """
    try:
        kwargs: Dict[str, Any] = {}
        if query_identifiers is not None:
            kwargs["query_identifiers"] = query_identifiers
        if cpd_hits is not None:
            kwargs["cpd_hits"] = cpd_hits
        result = biochem.search_reactions(**kwargs)
        if result is None:
            return {}
        return result
    except Exception as exc:
        logger.debug("biochem.search_reactions failed: %s", exc)
        return {}


def _search_compounds_safe(
    biochem: Any,
    *,
    query_structures: Optional[List[str]] = None,
    query_identifiers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Call ``biochem.search_compounds`` with graceful fallback to empty dict.

    Signature (from ms_biochem_utils.py:510-514):
        search_compounds(
            query_identifiers=[], query_structures=[], query_formula=None
        ) -> dict
    """
    try:
        kwargs: Dict[str, Any] = {}
        if query_structures is not None:
            kwargs["query_structures"] = query_structures
        if query_identifiers is not None:
            kwargs["query_identifiers"] = query_identifiers
        result = biochem.search_compounds(**kwargs)
        if result is None:
            return {}
        return result
    except Exception as exc:
        logger.debug("biochem.search_compounds failed: %s", exc)
        return {}


def _best_hit(matches: Dict[str, Any]) -> Optional[str]:
    """Return the id of the highest-scoring hit in a search result dict, or None."""
    if not matches:
        return None
    return max(matches, key=lambda k: matches[k].get("score", 0))


def _reaction_id_to_msid_safe(
    model_util: Any,
    reaction_id: str,
) -> Optional[str]:
    """Call ``reaction_id_to_msid`` on *model_util* (or fall back to regex).

    The method ``reaction_id_to_msid`` is defined on ``MSBiochemUtils`` and its
    composition wrapper (ms_biochem_utils.py:1343-1348).  It extracts the base
    ModelSEED id (e.g. ``"rxn00001"``) from a reaction id string.
    """
    try:
        return model_util.reaction_id_to_msid(reaction_id)
    except Exception:
        pass
    # Minimal regex fallback when the method is not exposed
    import re
    m = re.search(r"rxn\d+", reaction_id)
    return m.group() if m else None


# ---------------------------------------------------------------------------
# (a) reaction_in_db
# ---------------------------------------------------------------------------


def _check_reaction_in_db(
    biochem: Any,
    reaction_id: str,
    reactant_ids: Sequence[str],
    product_ids: Sequence[str],
    warnings: List[str],
) -> Optional[str]:
    """Answer question (a): is the predicted reaction already in the DB?

    Strategy:
    1. Direct identifier look-up via ``search_reactions(query_identifiers=[reaction_id])``.
    2. If no hit, also try the compound ids from reactants/products as
       stoichiometry evidence (lightweight: uses ``cpd_hits`` dict).

    Returns the best-matching MSRXN id, or ``None``.
    """
    # 1. Direct id look-up
    hits = _search_reactions_safe(biochem, query_identifiers=[reaction_id])
    if hits:
        return _best_hit(hits)

    # 2. Compound-set stoichiometry probe: pass reactant and product ids as
    #    cpd_hits stubs so the stoichiometry index can find a match.
    cpd_stubs: Dict[str, Any] = {cid: {} for cid in list(reactant_ids) + list(product_ids)}
    if cpd_stubs:
        hits = _search_reactions_safe(biochem, cpd_hits=cpd_stubs)
        if hits:
            return _best_hit(hits)

    return None


# ---------------------------------------------------------------------------
# (b) product_in_db
# ---------------------------------------------------------------------------


def _check_product_in_db(
    biochem: Any,
    product_smiles: str,
    product_inchikey: Optional[str],
    warnings: List[str],
) -> Optional[str]:
    """Answer question (b): is the product compound already in the DB?

    Tries SMILES first, then the InChIKey first block as an identifier look-up.
    Returns the best-matching MSCPD id, or ``None``.
    """
    structures: List[str] = []
    if product_smiles:
        structures.append(product_smiles)
    if product_inchikey:
        structures.append(product_inchikey)

    if structures:
        hits = _search_compounds_safe(biochem, query_structures=structures)
        if hits:
            return _best_hit(hits)

    # InChIKey first-block as an identifier (e.g. "AAAA-BBBB-C" → "AAAA")
    if product_inchikey and "-" in product_inchikey:
        first_block = product_inchikey.split("-")[0]
        hits = _search_compounds_safe(biochem, query_identifiers=[first_block])
        if hits:
            return _best_hit(hits)

    return None


# ---------------------------------------------------------------------------
# (c) downstream pathway
# ---------------------------------------------------------------------------


def _check_downstream_pathway(
    biochem: Any,
    product_in_db: Optional[str],
    warnings: List[str],
) -> tuple[bool, List[str]]:
    """Answer question (c): are there DB reactions consuming the product?

    Uses ``search_reactions(cpd_hits={mscpd: {}})`` to look up reactions where
    the product appears as a reactant.  Returns ``(has_pathway, [rxn_ids])``.
    """
    if product_in_db is None:
        return False, []

    # Pass the product MSCPD as a cpd_hit stub; search_reactions will use its
    # stoichiometry index to find reactions consuming it.
    hits = _search_reactions_safe(biochem, cpd_hits={product_in_db: {}})
    rxn_ids = list(hits.keys())
    return len(rxn_ids) > 0, rxn_ids


# ---------------------------------------------------------------------------
# (d) in_models
# ---------------------------------------------------------------------------


def _check_in_models(
    models: Optional[Sequence[Any]],
    downstream_reactions: List[str],
    warnings: List[str],
) -> Dict[str, bool]:
    """Answer question (d): are any downstream reactions present in supplied models?

    For each model utility in *models*, iterates ``model.model.reactions``
    (cobra reaction objects with ``.id``), normalises each reaction id to a
    ModelSEED base id via ``reaction_id_to_msid``, and tests membership in
    *downstream_reactions*.

    Evidence (evidence.md, kb_model_utils.py:792, ms_biochem_utils.py:1343):
        ``for rxn in model.model.reactions:``  → each ``rxn.id``
        ``model.reaction_id_to_msid(rxn.id)``  → normalised MSRXN base id

    Tolerates:
    - ``models=None`` or empty list → returns ``{}``.
    - A model whose ``.model`` or ``.model.reactions`` is unavailable → records
      warning, skips.
    - Non-ModelSEED ids → recorded as ``False`` (not crashed).
    """
    if not models:
        return {}

    downstream_set = set(downstream_reactions)
    result: Dict[str, bool] = {}

    for model_util in models:
        # Determine a human-readable reference for this model
        model_ref = getattr(model_util, "id", None) or getattr(model_util, "name", None)
        if model_ref is None:
            try:
                model_ref = str(id(model_util))
            except Exception:
                model_ref = "unknown"

        try:
            cobra_model = model_util.model  # MSModelUtil.model → cobra Model
            rxn_objects = cobra_model.reactions  # list[cobra.core.Reaction]
        except Exception as exc:
            warnings.append(
                f"model '{model_ref}': could not access .model.reactions — {exc}"
            )
            result[model_ref] = False
            continue

        found = False
        for rxn in rxn_objects:
            rxn_id = getattr(rxn, "id", None)
            if rxn_id is None:
                continue
            # Normalise to a ModelSEED base id
            msid = _reaction_id_to_msid_safe(model_util, rxn_id)
            check_ids = {rxn_id, msid} if msid else {rxn_id}
            if check_ids & downstream_set:
                found = True
                break

        result[model_ref] = found

    return result


# ---------------------------------------------------------------------------
# screen_products — main public function
# ---------------------------------------------------------------------------


def screen_products(
    *,
    expander: Any,
    operators: Sequence[str],
    compounds: Sequence[Dict[str, Any]],
    biochem: Any,
    models: Optional[Sequence[Any]] = None,
    generations: int = 1,
) -> ScreeningReport:
    """Screen methoxy-aromatic compounds and cross-reference predicted products.

    Parameters
    ----------
    expander:
        Any object with ``.expand(seed_smiles, generations, **kwargs)`` that
        returns an :class:`~kbutillib.cheminformatics.base.ExpansionResult`.
        In production this is the ``NetworkExpansionUtils`` facade; in tests a
        fake is acceptable.
    operators:
        Discovered verAB operator/rule ids (from
        :func:`~kbutillib.cheminformatics.verab.rule_discovery.discover_verab_rules`
        or a subset).  Used to filter expansion results to only reactions
        attributed to those operators.
    compounds:
        Source methoxy-aromatic compound dicts, each with at least ``"id"``
        (MSID) and ``"smiles"`` keys.
    biochem:
        Any object exposing ``search_reactions`` and ``search_compounds``
        (an ``MSBiochemUtils`` or its composition wrapper).
    models:
        Optional sequence of model utility objects exposing ``.model.reactions``
        (MSModelUtil-like) and ``reaction_id_to_msid``.  Pass ``None`` or an
        empty list to skip model-membership checks.
    generations:
        Number of expansion generations to run.

    Returns
    -------
    ScreeningReport
        Aggregated result with one :class:`~kbutillib.cheminformatics.verab.models.ScreeningRecord`
        per (source compound × operator × predicted product) triple.
    """
    warnings: List[str] = []
    records: List[ScreeningRecord] = []
    operators_set = set(operators)

    if not _rdkit_available():
        warnings.append(
            "RDKit is not available; product InChIKey computation is disabled."
        )

    for cpd in compounds:
        src_id: str = cpd.get("id", "")
        src_smiles: str = cpd.get("smiles", "")

        if not src_smiles:
            warnings.append(
                f"Compound '{src_id}' has no SMILES; skipping expansion."
            )
            continue

        # Run expansion for this source compound
        try:
            expansion = expander.expand(
                {src_id: src_smiles},
                generations=generations,
            )
        except Exception as exc:
            warnings.append(
                f"Expansion failed for compound '{src_id}': {exc}"
            )
            continue

        if not expansion.reactions:
            continue

        for rxn in expansion.reactions:
            # Filter to only reactions attributed to discovered operators
            rxn_operator: Optional[str] = getattr(rxn, "operator", None)
            if operators_set and rxn_operator not in operators_set:
                continue

            for prod_id in rxn.product_ids:
                prod_cpd = expansion.compounds.get(prod_id)
                if prod_cpd is None:
                    continue

                product_smiles: str = prod_cpd.smiles or ""
                product_inchikey: Optional[str] = _inchikey_from_smiles(product_smiles)

                # (a) reaction in DB?
                reaction_in_db = _check_reaction_in_db(
                    biochem,
                    rxn.reaction_id,
                    rxn.reactant_ids,
                    rxn.product_ids,
                    warnings,
                )

                # (b) product in DB?
                product_in_db = _check_product_in_db(
                    biochem,
                    product_smiles,
                    product_inchikey,
                    warnings,
                )

                # (c) downstream pathway?
                has_downstream, downstream_rxns = _check_downstream_pathway(
                    biochem,
                    product_in_db,
                    warnings,
                )

                # (d) in models?
                in_models = _check_in_models(
                    models,
                    downstream_rxns,
                    warnings,
                )

                records.append(
                    ScreeningRecord(
                        source_msid=src_id,
                        source_smiles=src_smiles,
                        operator=rxn_operator or "",
                        product_smiles=product_smiles,
                        product_inchikey=product_inchikey,
                        reaction_in_db=reaction_in_db,
                        product_in_db=product_in_db,
                        has_downstream_pathway=has_downstream,
                        downstream_reactions=downstream_rxns,
                        in_models=in_models,
                    )
                )

    return ScreeningReport(
        n_source_compounds=len(compounds),
        records=records,
        genome_predictions={},
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# S5 — genome / gene-content degradation prediction
# ---------------------------------------------------------------------------


def predict_genome_degradation(
    *,
    operators: Sequence[str],
    ec_hint: str = "1.14.13.82",
    genomes: Sequence[Any],
    annotation: Any,
) -> Dict[str, Any]:
    """Predict which genomes carry the enzymatic capacity to degrade methoxy-aromatics.

    For each genome, tests whether the genome's feature/ontology annotations
    include the EC number associated with the verAB O-demethylation operator
    (default EC 1.14.13.82 — vanillate monooxygenase).

    Workflow
    --------
    1. Resolve the EC term to ModelSEED reaction id(s) via
       ``annotation.translate_term_to_modelseed("EC:" + ec_hint)``.
       Signature (kb_annotation_utils.py:223-244):
           translate_term_to_modelseed(term: str) -> list[str]
    2. For each genome, call
       ``annotation.process_object({"object": genome, "type": genome_type})``
       (kb_annotation_utils.py:568-658) to populate
       ``annotation.ftrhash`` (feature id → feature dict) where each feature
       carries an ``"ontology_terms"`` dict keyed by ontology namespace.
    3. Scan ``annotation.ftrhash.values()`` for any feature that carries the
       EC term or a matching MSRXN id in its ``"ontology_terms"``.

    Parameters
    ----------
    operators:
        Discovered verAB operator ids (informational; used for provenance only).
    ec_hint:
        EC number for the verAB enzyme (default ``"1.14.13.82"``). Do NOT
        include the ``"EC:"`` prefix — it is added internally when calling
        ``translate_term_to_modelseed``.
    genomes:
        Sequence of genome objects or dicts.  Each entry may be:

        * A plain ``dict`` with KBase genome structure (has ``"features"``).
          Passed directly to ``process_object`` as
          ``{"object": genome, "type": "KBaseGenomes.Genome"}``.
        * Any object exposing a ``ref`` attribute (string) and a ``data``
          attribute (dict); the dict is used as the object.
        * A string — treated as a genome reference that ``process_object``
          should resolve via the workspace client (``{"input_ref": genome}``).
    annotation:
        Any object implementing:
            ``translate_term_to_modelseed(term: str) -> list[str]``
                (kb_annotation_utils.py:223-244)
            ``process_object(params: dict) -> None``
                (kb_annotation_utils.py:568-658)
            ``ftrhash: dict[str, dict]``   (populated by process_object)
        Pass ``None`` to get graceful degradation (all genomes → not evaluated).

    Returns
    -------
    dict[genome_ref, {"can_degrade": bool, "ec_hits": list[str], "msrxn_ids": list[str]}]
        *genome_ref* is a string key derived from each genome entry.
        *can_degrade* is ``True`` iff at least one genome feature carries the
        EC term.
        *ec_hits* lists the feature ids where the EC term was found.
        *msrxn_ids* lists the ModelSEED reaction ids resolved from the EC term.

    Graceful degradation
    --------------------
    - If *annotation* is ``None`` or does not expose the required methods,
      every genome is returned with ``{"can_degrade": False, "ec_hits": [],
      "msrxn_ids": [], "warning": "annotation layer unavailable"}``.
    - Exceptions raised by ``process_object`` for an individual genome are
      caught; that genome entry records the exception message and is marked
      ``can_degrade=False``.
    """
    ec_term = "EC:" + ec_hint

    # ------------------------------------------------------------------
    # Graceful degradation: annotation layer unavailable
    # ------------------------------------------------------------------
    _annotation_ok = False
    if annotation is not None:
        _annotation_ok = (
            hasattr(annotation, "translate_term_to_modelseed")
            and hasattr(annotation, "process_object")
        )

    if not _annotation_ok:
        result: Dict[str, Any] = {}
        for genome in genomes:
            genome_ref = _genome_ref(genome)
            result[genome_ref] = {
                "can_degrade": False,
                "ec_hits": [],
                "msrxn_ids": [],
                "warning": "annotation layer unavailable",
            }
        return result

    # ------------------------------------------------------------------
    # Step 1: EC → ModelSEED reaction ids
    # ------------------------------------------------------------------
    msrxn_ids: List[str] = []
    try:
        msrxn_ids = annotation.translate_term_to_modelseed(ec_term) or []
    except Exception as exc:
        logger.debug("translate_term_to_modelseed(%r) failed: %s", ec_term, exc)

    # ------------------------------------------------------------------
    # Step 2 & 3: per-genome feature scan
    # ------------------------------------------------------------------
    result = {}
    for genome in genomes:
        genome_ref = _genome_ref(genome)
        try:
            params = _genome_process_params(genome)
            annotation.process_object(params)  # populates annotation.ftrhash
        except Exception as exc:
            logger.debug(
                "annotation.process_object failed for genome %r: %s", genome_ref, exc
            )
            result[genome_ref] = {
                "can_degrade": False,
                "ec_hits": [],
                "msrxn_ids": list(msrxn_ids),
                "warning": str(exc),
            }
            continue

        # Scan features for the EC term in ontology_terms
        ec_hits: List[str] = []
        ftrhash: Dict[str, Any] = getattr(annotation, "ftrhash", {}) or {}
        for ftr_id, ftr in ftrhash.items():
            onto_terms: Dict[str, Any] = ftr.get("ontology_terms", {})
            # ontology_terms is keyed by ontology namespace (e.g. "EC", "MSRXN", ...)
            # Check the "EC" namespace for the exact EC term
            ec_namespace = onto_terms.get("EC", {})
            if ec_term in ec_namespace:
                ec_hits.append(ftr_id)
                continue
            # Also accept the bare number form (e.g. "1.14.13.82") in the EC namespace
            if ec_hint in ec_namespace:
                ec_hits.append(ftr_id)
                continue
            # Check MSRXN namespace for any of the resolved ModelSEED reaction ids
            if msrxn_ids:
                msrxn_namespace = onto_terms.get("MSRXN", {})
                for msrxn in msrxn_ids:
                    if msrxn in msrxn_namespace:
                        ec_hits.append(ftr_id)
                        break

        result[genome_ref] = {
            "can_degrade": len(ec_hits) > 0,
            "ec_hits": ec_hits,
            "msrxn_ids": list(msrxn_ids),
        }

    return result


# ---------------------------------------------------------------------------
# predict_genome_degradation helpers
# ---------------------------------------------------------------------------


def _genome_ref(genome: Any) -> str:
    """Derive a stable string key for a genome entry."""
    if isinstance(genome, str):
        return genome
    if hasattr(genome, "ref"):
        return str(genome.ref)
    if isinstance(genome, dict):
        # Try KBase workspace ref keys
        for key in ("ref", "id", "name", "object_name"):
            if key in genome:
                return str(genome[key])
        return f"genome@{id(genome)}"
    return f"genome@{id(genome)}"


def _genome_process_params(genome: Any) -> Dict[str, Any]:
    """Build the params dict for ``annotation.process_object``.

    Handles:
    - dict (KBase genome object)  → ``{"object": genome, "type": "KBaseGenomes.Genome"}``
    - object with ``.data`` attr → ``{"object": genome.data, "type": genome.type}``
    - string (workspace ref)     → ``{"input_ref": genome}``
    """
    if isinstance(genome, str):
        return {"input_ref": genome}
    if isinstance(genome, dict):
        genome_type = genome.get("type", "KBaseGenomes.Genome")
        return {"object": genome, "type": genome_type}
    if hasattr(genome, "data") and hasattr(genome, "type"):
        return {"object": genome.data, "type": genome.type}
    if hasattr(genome, "data"):
        return {"object": genome.data, "type": "KBaseGenomes.Genome"}
    # Fallback: assume genome itself is the object dict
    return {"object": genome, "type": "KBaseGenomes.Genome"}
