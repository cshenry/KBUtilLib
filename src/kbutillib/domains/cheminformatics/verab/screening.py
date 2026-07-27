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
) -> tuple:
    """Call ``biochem.search_reactions``, distinguishing not-found from failure.

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

    Returns
    -------
    (result_dict, error_or_None) : tuple
        - On success (query ran, may be empty): ``(result_dict, None)``
        - On exception (query failed):  ``({}, "search_reactions failed: <type>: <msg>")``

    This makes a failed DB query distinguishable from a legitimate empty result
    (genuine "not found"), so callers can annotate ScreeningRecord.lookup_ok=False
    rather than silently treating an error as a confirmed absence.
    """
    try:
        kwargs: Dict[str, Any] = {}
        if query_identifiers is not None:
            kwargs["query_identifiers"] = query_identifiers
        if cpd_hits is not None:
            kwargs["cpd_hits"] = cpd_hits
        result = biochem.search_reactions(**kwargs)
        if result is None:
            return {}, None
        return result, None
    except Exception as exc:
        msg = f"search_reactions failed: {type(exc).__name__}: {exc}"
        logger.warning("biochem.%s", msg)
        return {}, msg


def _search_compounds_safe(
    biochem: Any,
    *,
    query_structures: Optional[List[str]] = None,
    query_identifiers: Optional[List[str]] = None,
) -> tuple:
    """Call ``biochem.search_compounds``, distinguishing not-found from failure.

    Signature (from ms_biochem_utils.py:510-514):
        search_compounds(
            query_identifiers=[], query_structures=[], query_formula=None
        ) -> dict

    Returns
    -------
    (result_dict, error_or_None) : tuple
        - On success (query ran, may be empty): ``(result_dict, None)``
        - On exception (query failed):  ``({}, "search_compounds failed: <type>: <msg>")``
    """
    try:
        kwargs: Dict[str, Any] = {}
        if query_structures is not None:
            kwargs["query_structures"] = query_structures
        if query_identifiers is not None:
            kwargs["query_identifiers"] = query_identifiers
        result = biochem.search_compounds(**kwargs)
        if result is None:
            return {}, None
        return result, None
    except Exception as exc:
        msg = f"search_compounds failed: {type(exc).__name__}: {exc}"
        logger.warning("biochem.%s", msg)
        return {}, msg


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
) -> tuple:
    """Answer question (a): is the predicted reaction already in the DB?

    Strategy:
    1. Direct identifier look-up via ``search_reactions(query_identifiers=[reaction_id])``.
    2. If no hit, also try the compound ids from reactants/products as
       stoichiometry evidence (lightweight: uses ``cpd_hits`` dict).

    Returns
    -------
    (msrxn_id_or_None, lookup_errors) : tuple
        *msrxn_id_or_None* is ``None`` whether genuinely absent OR a lookup failed.
        *lookup_errors* is a (possibly empty) list of error message strings.
        Callers must inspect ``lookup_errors`` to distinguish genuine absence from
        query failure — a non-empty list signals failure (false-negative risk).
    """
    lookup_errors: List[str] = []

    # 1. Direct id look-up
    hits, err = _search_reactions_safe(biochem, query_identifiers=[reaction_id])
    if err is not None:
        lookup_errors.append(err)
    elif hits:
        return _best_hit(hits), lookup_errors

    # 2. Compound-set stoichiometry probe: pass reactant and product ids as
    #    cpd_hits stubs so the stoichiometry index can find a match.
    cpd_stubs: Dict[str, Any] = {cid: {} for cid in list(reactant_ids) + list(product_ids)}
    if cpd_stubs:
        hits2, err2 = _search_reactions_safe(biochem, cpd_hits=cpd_stubs)
        if err2 is not None:
            lookup_errors.append(err2)
        elif hits2:
            return _best_hit(hits2), lookup_errors

    return None, lookup_errors


# ---------------------------------------------------------------------------
# (b) product_in_db
# ---------------------------------------------------------------------------


def _check_product_in_db(
    biochem: Any,
    product_smiles: str,
    product_inchikey: Optional[str],
    warnings: List[str],
) -> tuple:
    """Answer question (b): is the product compound already in the DB?

    Tries SMILES first, then the InChIKey first block as an identifier look-up.

    Returns
    -------
    (mscpd_id_or_None, lookup_errors) : tuple
        *mscpd_id_or_None* is ``None`` whether genuinely absent OR lookup failed.
        *lookup_errors* is a (possibly empty) list of error message strings.
    """
    lookup_errors: List[str] = []

    structures: List[str] = []
    if product_smiles:
        structures.append(product_smiles)
    if product_inchikey:
        structures.append(product_inchikey)

    if structures:
        hits, err = _search_compounds_safe(biochem, query_structures=structures)
        if err is not None:
            lookup_errors.append(err)
        elif hits:
            return _best_hit(hits), lookup_errors

    # InChIKey first-block as an identifier (e.g. "AAAA-BBBB-C" → "AAAA")
    if product_inchikey and "-" in product_inchikey:
        first_block = product_inchikey.split("-")[0]
        hits2, err2 = _search_compounds_safe(biochem, query_identifiers=[first_block])
        if err2 is not None:
            lookup_errors.append(err2)
        elif hits2:
            return _best_hit(hits2), lookup_errors

    return None, lookup_errors


# ---------------------------------------------------------------------------
# (c) downstream pathway
# ---------------------------------------------------------------------------


def _check_downstream_pathway(
    biochem: Any,
    product_in_db: Optional[str],
    warnings: List[str],
) -> tuple:
    """Answer question (c): are there DB reactions consuming the product?

    Uses ``search_reactions(cpd_hits={mscpd: {}})`` to look up reactions where
    the product appears as a reactant.

    Returns
    -------
    (has_pathway, rxn_ids, lookup_errors) : tuple
        *has_pathway* is ``True`` iff at least one downstream reaction was found.
        *rxn_ids* is the list of MSRXN ids found.
        *lookup_errors* is a (possibly empty) list of error message strings;
        non-empty means the query raised an exception (false-negative risk).
    """
    if product_in_db is None:
        return False, [], []

    # Pass the product MSCPD as a cpd_hit stub; search_reactions will use its
    # stoichiometry index to find reactions consuming it.
    hits, err = _search_reactions_safe(biochem, cpd_hits={product_in_db: {}})
    if err is not None:
        return False, [], [err]
    rxn_ids = list(hits.keys())
    return len(rxn_ids) > 0, rxn_ids, []


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
            # Filter to only reactions attributed to discovered operators.
            # Use the full operators list when available (multi-op reactions);
            # fall back to the scalar operator for backward compatibility.
            rxn_operator: Optional[str] = getattr(rxn, "operator", None)
            rxn_operators_list: list = list(getattr(rxn, "operators", None) or [])
            if not rxn_operators_list and rxn_operator is not None:
                rxn_operators_list = [rxn_operator]
            if operators_set and not (operators_set & set(rxn_operators_list)):
                continue

            for prod_id in rxn.product_ids:
                prod_cpd = expansion.compounds.get(prod_id)
                if prod_cpd is None:
                    continue

                product_smiles: str = prod_cpd.smiles or ""
                product_inchikey: Optional[str] = _inchikey_from_smiles(product_smiles)

                # Accumulate lookup errors for this specific product record.
                # A non-empty list signals a query failure (false-negative risk)
                # rather than a confirmed absence.
                rec_lookup_errors: List[str] = []

                # (a) reaction in DB?
                reaction_in_db, errs_a = _check_reaction_in_db(
                    biochem,
                    rxn.reaction_id,
                    rxn.reactant_ids,
                    rxn.product_ids,
                    warnings,
                )
                rec_lookup_errors.extend(errs_a)

                # (b) product in DB?
                product_in_db, errs_b = _check_product_in_db(
                    biochem,
                    product_smiles,
                    product_inchikey,
                    warnings,
                )
                rec_lookup_errors.extend(errs_b)

                # (c) downstream pathway?
                has_downstream, downstream_rxns, errs_c = _check_downstream_pathway(
                    biochem,
                    product_in_db,
                    warnings,
                )
                rec_lookup_errors.extend(errs_c)

                # (d) in models?
                in_models = _check_in_models(
                    models,
                    downstream_rxns,
                    warnings,
                )

                # Determine overall lookup health for this record.
                # Any error from (a)-(c) means a query failed; this is surfaced
                # via lookup_ok=False so downstream analysis is not silently
                # misled into treating a query failure as a confirmed "not in DB".
                rec_lookup_ok = len(rec_lookup_errors) == 0
                if not rec_lookup_ok:
                    for err_msg in rec_lookup_errors:
                        warnings.append(
                            f"[screening] lookup failure for product "
                            f"'{product_smiles[:40]}' of '{src_id}': {err_msg}"
                        )

                records.append(
                    ScreeningRecord(
                        source_msid=src_id,
                        source_smiles=src_smiles,
                        operator=rxn_operator or "",
                        operators=list(rxn_operators_list),
                        product_smiles=product_smiles,
                        product_inchikey=product_inchikey,
                        reaction_in_db=reaction_in_db,
                        product_in_db=product_in_db,
                        has_downstream_pathway=has_downstream,
                        downstream_reactions=downstream_rxns,
                        in_models=in_models,
                        lookup_ok=rec_lookup_ok,
                        lookup_errors=rec_lookup_errors,
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
    ec_hints: Sequence[str] = ("1.14.13.82",),
    ec_hint: Optional[str] = None,
    operator_ec_map: Optional[Dict[str, List[str]]] = None,
    genomes: Sequence[Any],
    annotation: Any,
) -> Dict[str, Any]:
    """Predict which genomes carry the enzymatic capacity to degrade methoxy-aromatics.

    For each genome, tests whether the genome's feature/ontology annotations
    include EC numbers associated with the verAB O-demethylation operators.
    Supports multiple EC terms (``ec_hints``) so that predictions are not
    limited to a single hard-coded EC.  For each matched feature the result
    records WHICH EC term (or MSRXN id) was the evidence, making the
    prediction fully explainable.

    Workflow
    --------
    1. Build the effective EC list from ``ec_hints``, the legacy ``ec_hint``
       kwarg (if provided), and optional ``operator_ec_map`` (operator id →
       list of associated EC numbers derived from rule metadata).
    2. Resolve EACH EC term to ModelSEED reaction id(s) via
       ``annotation.translate_term_to_modelseed("EC:" + ec)``.
       Signature (kb_annotation_utils.py:223-244):
           translate_term_to_modelseed(term: str) -> list[str]
    3. For each genome, call
       ``annotation.process_object({"object": genome, "type": genome_type})``
       (kb_annotation_utils.py:568-658) to populate
       ``annotation.ftrhash`` (feature id → feature dict) where each feature
       carries an ``"ontology_terms"`` dict keyed by ontology namespace.
    4. Scan ``annotation.ftrhash.values()`` for any feature that carries ANY
       of the EC terms or a matching MSRXN id.  Record per-feature evidence
       (which EC/MSRXN matched).

    Parameters
    ----------
    operators:
        Discovered verAB operator ids (informational; used for provenance).
    ec_hints:
        Sequence of EC numbers to check (do NOT include the ``"EC:"`` prefix).
        Default ``("1.14.13.82",)`` — vanillate monooxygenase.  Additional
        ECs derived from ``operator_ec_map`` are merged into this set.
    ec_hint:
        Legacy single-EC kwarg.  If provided it is prepended to ``ec_hints``
        (back-compat: callers supplying only ``ec_hint`` still work).
    operator_ec_map:
        Optional dict mapping operator id → list of associated EC numbers.
        When provided, ECs for any operator in *operators* are merged into the
        effective EC list.  This lets rule-discovery metadata drive the EC
        evidence without hard-coding.
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
    dict[genome_ref, explainable_entry]
        *genome_ref* is a string key derived from each genome entry.

        Each *explainable_entry* is a dict with keys:

        ``can_degrade`` (bool)
            True iff at least one genome feature matched any EC term or MSRXN.
        ``ec_hits`` (list[str])
            Feature ids where ANY EC term or MSRXN matched (union across all
            terms; backward-compatible with the old single-EC shape).
        ``matched_terms`` (dict[str, list[str]])
            Per-feature evidence: maps feature id → list of matched EC terms
            (prefixed ``"EC:…"``) or MSRXN ids that were found in that feature.
        ``ec_hints`` (list[str])
            The effective EC numbers that were checked (bare, without prefix).
        ``msrxn_ids`` (list[str])
            Union of all ModelSEED reaction ids resolved from all EC terms.

    Graceful degradation
    --------------------
    - If *annotation* is ``None`` or does not expose the required methods,
      every genome is returned with ``{"can_degrade": False, "ec_hits": [],
      "matched_terms": {}, "ec_hints": [...], "msrxn_ids": [],
      "warning": "annotation layer unavailable"}``.
    - Exceptions raised by ``process_object`` for an individual genome are
      caught; that genome entry records the exception message and is marked
      ``can_degrade=False`` with the explainable fields populated as far as
      possible (msrxn_ids and ec_hints are still reported).
    """
    # ------------------------------------------------------------------
    # Step 0: Build the effective EC list
    # ------------------------------------------------------------------
    # Start from the sequence param (may be empty tuple if caller overrides)
    _effective_ec_bare: List[str] = list(ec_hints)

    # Legacy back-compat: single ec_hint kwarg → prepend if not already present
    if ec_hint is not None and ec_hint not in _effective_ec_bare:
        _effective_ec_bare.insert(0, ec_hint)

    # Operator-derived ECs via operator_ec_map
    if operator_ec_map:
        for op in operators:
            for derived_ec in operator_ec_map.get(op, []):
                if derived_ec not in _effective_ec_bare:
                    _effective_ec_bare.append(derived_ec)

    # Ensure the fallback hint is always present so the function stays
    # meaningful even when called with an empty ec_hints sequence.
    if not _effective_ec_bare:
        _effective_ec_bare = ["1.14.13.82"]

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
                "matched_terms": {},
                "ec_hints": list(_effective_ec_bare),
                "msrxn_ids": [],
                "warning": "annotation layer unavailable",
            }
        return result

    # ------------------------------------------------------------------
    # Step 1: Resolve ALL EC terms → ModelSEED reaction ids
    #
    # Build two structures:
    #   all_msrxn_ids   : union of all MSRXNs across all ECs (for back-compat)
    #   ec_term_to_msrxn: "EC:X.Y.Z.W" → [msrxn, ...] (for per-hit attribution)
    # ------------------------------------------------------------------
    all_msrxn_ids: List[str] = []
    ec_term_to_msrxn: Dict[str, List[str]] = {}

    for bare_ec in _effective_ec_bare:
        ec_term = "EC:" + bare_ec
        try:
            ids = annotation.translate_term_to_modelseed(ec_term) or []
        except Exception as exc:
            logger.debug("translate_term_to_modelseed(%r) failed: %s", ec_term, exc)
            ids = []
        ec_term_to_msrxn[ec_term] = list(ids)
        for msrxn in ids:
            if msrxn not in all_msrxn_ids:
                all_msrxn_ids.append(msrxn)

    # Also build a reverse map: msrxn_id → list of EC terms that produced it
    # (used for per-feature attribution of MSRXN hits)
    msrxn_to_ec_terms: Dict[str, List[str]] = {}
    for ec_term, ids in ec_term_to_msrxn.items():
        for msrxn in ids:
            msrxn_to_ec_terms.setdefault(msrxn, []).append(ec_term)

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
                "matched_terms": {},
                "ec_hints": list(_effective_ec_bare),
                "msrxn_ids": list(all_msrxn_ids),
                "warning": str(exc),
            }
            continue

        # Scan features; accumulate matched feature ids and per-feature evidence
        ec_hits: List[str] = []
        matched_terms: Dict[str, List[str]] = {}  # ftr_id → [matched EC/MSRXN terms]

        ftrhash: Dict[str, Any] = getattr(annotation, "ftrhash", {}) or {}
        for ftr_id, ftr in ftrhash.items():
            onto_terms: Dict[str, Any] = ftr.get("ontology_terms", {})
            # ontology_terms is keyed by ontology namespace (e.g. "EC", "MSRXN", ...)
            ec_namespace = onto_terms.get("EC", {})
            msrxn_namespace = onto_terms.get("MSRXN", {})

            ftr_matched: List[str] = []

            # Check EACH effective EC term (full "EC:X.Y.Z.W" and bare form)
            for bare_ec in _effective_ec_bare:
                ec_term = "EC:" + bare_ec
                if ec_term in ec_namespace or bare_ec in ec_namespace:
                    ftr_matched.append(ec_term)

            # Check MSRXN namespace for any resolved ModelSEED reaction ids
            for msrxn in all_msrxn_ids:
                if msrxn in msrxn_namespace:
                    # Attribute this hit to the EC terms that produced the MSRXN
                    for src_ec in msrxn_to_ec_terms.get(msrxn, [msrxn]):
                        if src_ec not in ftr_matched:
                            ftr_matched.append(src_ec)

            if ftr_matched:
                ec_hits.append(ftr_id)
                matched_terms[ftr_id] = ftr_matched

        result[genome_ref] = {
            "can_degrade": len(ec_hits) > 0,
            "ec_hits": ec_hits,
            "matched_terms": matched_terms,
            "ec_hints": list(_effective_ec_bare),
            "msrxn_ids": list(all_msrxn_ids),
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
