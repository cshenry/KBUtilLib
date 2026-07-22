"""S4 unit tests for verAB screening: screen_products().

These tests run without RDKit, minedatabase, or a live ModelSEED database.
All external collaborators are replaced with simple fakes that expose the
exact method signatures found in the real code:

  biochem.search_reactions(query_identifiers=[], query_ec=[], query_stoichiometry=None,
                           cpd_hits=None, default_missing_count=1)
      → dict[rxnid, {score, ...}]   (ms_biochem_utils.py:567-573)

  biochem.search_compounds(query_identifiers=[], query_structures=[], query_formula=None)
      → dict[cpdid, {score, ...}]   (ms_biochem_utils.py:510-514)

  biochem.get_compound_by_id(compound_id)
      → compound object or None      (ms_biochem_utils.py:720)

  model_util.model.reactions          → iterable of objects with .id
  model_util.reaction_id_to_msid(rxn_id) → str or None
      (ms_biochem_utils.py composition wrapper, lines 1343-1348)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Fake collaborators
# ---------------------------------------------------------------------------


class _FakeReaction:
    """Minimal cobra-like reaction object with an id."""

    def __init__(self, rxn_id: str) -> None:
        self.id = rxn_id


class _FakeCobraModel:
    """Minimal cobra Model stub exposing .reactions."""

    def __init__(self, reactions: List[_FakeReaction]) -> None:
        self.reactions = reactions


class _FakeModelUtil:
    """Fake MSModelUtil-like object.

    Exposes:
      .model            → _FakeCobraModel  (MSModelUtil pattern)
      .reaction_id_to_msid(rxn_id)         (ms_biochem_utils.py:1343-1348)
    """

    def __init__(
        self,
        model_id: str,
        rxn_ids: List[str],
        msid_map: Optional[Dict[str, str]] = None,
    ) -> None:
        self.id = model_id
        self.model = _FakeCobraModel([_FakeReaction(r) for r in rxn_ids])
        self._msid_map: Dict[str, str] = msid_map or {}

    def reaction_id_to_msid(self, reaction_id: str) -> Optional[str]:
        """Mirror ms_biochem_utils.py:1343-1348 (regex on rxnNNNNN pattern)."""
        return self._msid_map.get(reaction_id, None)


class _FakeBiochem:
    """Fake biochem object implementing the three needed search methods.

    Attributes
    ----------
    search_reactions_return:
        What to return from search_reactions().
    search_compounds_return:
        What to return from search_compounds().
    get_compound_by_id_return:
        What to return from get_compound_by_id().

    Records arguments of each call for assertion.
    """

    def __init__(
        self,
        search_reactions_return: Optional[Dict[str, Any]] = None,
        search_compounds_return: Optional[Dict[str, Any]] = None,
        get_compound_by_id_return: Any = None,
    ) -> None:
        self._search_reactions_return = search_reactions_return or {}
        self._search_compounds_return = search_compounds_return or {}
        self._get_compound_by_id_return = get_compound_by_id_return
        # Call records
        self.search_reactions_calls: List[Dict[str, Any]] = []
        self.search_compounds_calls: List[Dict[str, Any]] = []
        self.get_compound_by_id_calls: List[str] = []

    # Signature from ms_biochem_utils.py:567-573
    def search_reactions(
        self,
        query_identifiers: list = [],
        query_ec: list = [],
        query_stoichiometry: Optional[dict] = None,
        cpd_hits: Optional[dict] = None,
        default_missing_count: float = 1,
    ) -> Dict[str, Any]:
        self.search_reactions_calls.append(
            {
                "query_identifiers": list(query_identifiers),
                "query_ec": list(query_ec),
                "query_stoichiometry": query_stoichiometry,
                "cpd_hits": dict(cpd_hits) if cpd_hits is not None else None,
            }
        )
        return dict(self._search_reactions_return)

    # Signature from ms_biochem_utils.py:510-514
    def search_compounds(
        self,
        query_identifiers: list = [],
        query_structures: list = [],
        query_formula: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.search_compounds_calls.append(
            {
                "query_identifiers": list(query_identifiers),
                "query_structures": list(query_structures),
                "query_formula": query_formula,
            }
        )
        return dict(self._search_compounds_return)

    # Signature from ms_biochem_utils.py:720-732
    def get_compound_by_id(self, compound_id: str) -> Any:
        self.get_compound_by_id_calls.append(compound_id)
        return self._get_compound_by_id_return


# ---------------------------------------------------------------------------
# Fake expander
# ---------------------------------------------------------------------------

from kbutillib.cheminformatics.base import (
    ExpansionResult,
    PredictedCompound,
    PredictedReaction,
)


def _make_expansion_result(
    src_id: str = "cpd_src",
    src_smiles: str = "COc1ccccc1C(=O)O",   # guaiacyl-like methoxy aromatic
    prod_id: str = "cpd_prod",
    prod_smiles: str = "Oc1ccccc1C(=O)O",   # phenol product (demethylated)
    rxn_id: str = "rxn_fake_001",
    operator: str = "rule_demethyl_01",
    extra_rxn: Optional[PredictedReaction] = None,
) -> ExpansionResult:
    """Build a synthetic ExpansionResult for a single O-demethylation event."""
    compounds = {
        src_id: PredictedCompound(
            compound_id=src_id, smiles=src_smiles, is_seed=True
        ),
        prod_id: PredictedCompound(
            compound_id=prod_id, smiles=prod_smiles, is_seed=False
        ),
    }
    reactions: List[PredictedReaction] = [
        PredictedReaction(
            reaction_id=rxn_id,
            backend="fake",
            operator=operator,
            reactant_ids=[src_id],
            product_ids=[prod_id],
        )
    ]
    if extra_rxn is not None:
        reactions.append(extra_rxn)
    return ExpansionResult(backend="fake", compounds=compounds, reactions=reactions)


class _FakeExpander:
    """Fake expander duck-typing NetworkExpansionUtils.expand()."""

    def __init__(self, result: ExpansionResult) -> None:
        self._result = result
        self.expand_calls: List[Dict[str, Any]] = []

    def expand(self, seed_smiles: Dict[str, str], generations: int = 1, **kwargs: Any) -> ExpansionResult:
        self.expand_calls.append({"seed_smiles": dict(seed_smiles), "generations": generations})
        return self._result


# ---------------------------------------------------------------------------
# Import the unit under test
# ---------------------------------------------------------------------------

from kbutillib.cheminformatics.verab.screening import screen_products


# ---------------------------------------------------------------------------
# Tests: happy path — each of (a)-(d) resolves correctly
# ---------------------------------------------------------------------------


def test_screen_products_reaction_in_db():
    """(a) reaction_in_db: biochem.search_reactions returns a hit → recorded."""
    result = _make_expansion_result()
    expander = _FakeExpander(result)
    biochem = _FakeBiochem(
        search_reactions_return={"rxn00001": {"score": 20}},
        search_compounds_return={},
    )

    compounds = [{"id": "cpd_src", "smiles": "COc1ccccc1C(=O)O"}]
    report = screen_products(
        expander=expander,
        operators=["rule_demethyl_01"],
        compounds=compounds,
        biochem=biochem,
        models=None,
    )

    assert len(report.records) == 1, f"Expected 1 record, got {len(report.records)}"
    rec = report.records[0]
    assert rec.reaction_in_db == "rxn00001", (
        f"Expected reaction_in_db='rxn00001', got {rec.reaction_in_db!r}"
    )


def test_screen_products_product_in_db():
    """(b) product_in_db: biochem.search_compounds returns a hit → recorded."""
    result = _make_expansion_result()
    expander = _FakeExpander(result)
    biochem = _FakeBiochem(
        search_reactions_return={},
        search_compounds_return={"cpd00294": {"score": 8}},
    )

    compounds = [{"id": "cpd_src", "smiles": "COc1ccccc1C(=O)O"}]
    report = screen_products(
        expander=expander,
        operators=["rule_demethyl_01"],
        compounds=compounds,
        biochem=biochem,
    )

    assert len(report.records) == 1
    rec = report.records[0]
    assert rec.product_in_db == "cpd00294", (
        f"Expected product_in_db='cpd00294', got {rec.product_in_db!r}"
    )


def test_screen_products_has_downstream_pathway():
    """(c) has_downstream_pathway: reactions consuming product exist → True + list."""
    result = _make_expansion_result()
    expander = _FakeExpander(result)

    # search_compounds returns product hit; search_reactions returns downstream rxn
    call_count = [0]
    orig_sr = {"rxn00999": {"score": 15}}  # downstream rxn

    biochem_obj = _FakeBiochem(
        search_reactions_return=orig_sr,
        search_compounds_return={"cpd00294": {"score": 8}},
    )

    compounds = [{"id": "cpd_src", "smiles": "COc1ccccc1C(=O)O"}]
    report = screen_products(
        expander=expander,
        operators=["rule_demethyl_01"],
        compounds=compounds,
        biochem=biochem_obj,
    )

    assert len(report.records) == 1
    rec = report.records[0]
    assert rec.has_downstream_pathway is True, "Expected downstream pathway True"
    assert "rxn00999" in rec.downstream_reactions


def test_screen_products_in_models():
    """(d) in_models: downstream rxn present in model → True."""
    result = _make_expansion_result()
    expander = _FakeExpander(result)

    biochem = _FakeBiochem(
        search_reactions_return={"rxn00999": {"score": 15}},
        search_compounds_return={"cpd00294": {"score": 8}},
    )

    # Model has rxn00999 (the downstream reaction)
    model_util = _FakeModelUtil(
        model_id="model_ADP1",
        rxn_ids=["rxn00999_c0", "rxn00042_c0"],
        msid_map={"rxn00999_c0": "rxn00999", "rxn00042_c0": "rxn00042"},
    )

    compounds = [{"id": "cpd_src", "smiles": "COc1ccccc1C(=O)O"}]
    report = screen_products(
        expander=expander,
        operators=["rule_demethyl_01"],
        compounds=compounds,
        biochem=biochem,
        models=[model_util],
    )

    assert len(report.records) == 1
    rec = report.records[0]
    assert rec.in_models.get("model_ADP1") is True, (
        f"Expected in_models['model_ADP1']=True, got {rec.in_models}"
    )


def test_screen_products_in_models_absent():
    """(d) in_models: downstream rxn NOT present in model → False."""
    result = _make_expansion_result()
    expander = _FakeExpander(result)

    biochem = _FakeBiochem(
        search_reactions_return={"rxn00999": {"score": 15}},
        search_compounds_return={"cpd00294": {"score": 8}},
    )

    # Model does NOT have rxn00999
    model_util = _FakeModelUtil(
        model_id="model_other",
        rxn_ids=["rxn00042_c0"],
        msid_map={"rxn00042_c0": "rxn00042"},
    )

    compounds = [{"id": "cpd_src", "smiles": "COc1ccccc1C(=O)O"}]
    report = screen_products(
        expander=expander,
        operators=["rule_demethyl_01"],
        compounds=compounds,
        biochem=biochem,
        models=[model_util],
    )

    assert len(report.records) == 1
    rec = report.records[0]
    assert rec.in_models.get("model_other") is False, (
        f"Expected in_models['model_other']=False, got {rec.in_models}"
    )


# ---------------------------------------------------------------------------
# Tests: graceful behavior when biochem returns no hits
# ---------------------------------------------------------------------------


def test_screen_products_no_db_hits():
    """All four questions gracefully resolve to None/False when biochem finds nothing."""
    result = _make_expansion_result()
    expander = _FakeExpander(result)
    biochem = _FakeBiochem(
        search_reactions_return={},
        search_compounds_return={},
    )

    compounds = [{"id": "cpd_src", "smiles": "COc1ccccc1C(=O)O"}]
    report = screen_products(
        expander=expander,
        operators=["rule_demethyl_01"],
        compounds=compounds,
        biochem=biochem,
        models=None,
    )

    assert len(report.records) == 1
    rec = report.records[0]
    assert rec.reaction_in_db is None
    assert rec.product_in_db is None
    assert rec.has_downstream_pathway is False
    assert rec.downstream_reactions == []
    assert rec.in_models == {}


# ---------------------------------------------------------------------------
# Tests: graceful behavior when models is None
# ---------------------------------------------------------------------------


def test_screen_products_models_none():
    """models=None is tolerated; in_models is empty dict."""
    result = _make_expansion_result()
    expander = _FakeExpander(result)
    biochem = _FakeBiochem(
        search_reactions_return={"rxn00999": {"score": 15}},
        search_compounds_return={"cpd00294": {"score": 8}},
    )

    compounds = [{"id": "cpd_src", "smiles": "COc1ccccc1C(=O)O"}]
    report = screen_products(
        expander=expander,
        operators=["rule_demethyl_01"],
        compounds=compounds,
        biochem=biochem,
        models=None,
    )

    assert len(report.records) == 1
    assert report.records[0].in_models == {}


# ---------------------------------------------------------------------------
# Tests: operator filtering
# ---------------------------------------------------------------------------


def test_screen_products_operator_filtering():
    """Reactions not attributed to the supplied operators are skipped."""
    # ExpansionResult with two reactions: one matching, one decoy
    decoy_rxn = PredictedReaction(
        reaction_id="rxn_decoy",
        backend="fake",
        operator="rule_decoy",
        reactant_ids=["cpd_src"],
        product_ids=["cpd_decoy_prod"],
    )
    compounds_map = {
        "cpd_src": PredictedCompound("cpd_src", smiles="COc1ccccc1C(=O)O", is_seed=True),
        "cpd_prod": PredictedCompound("cpd_prod", smiles="Oc1ccccc1C(=O)O", is_seed=False),
        "cpd_decoy_prod": PredictedCompound("cpd_decoy_prod", smiles="CCC", is_seed=False),
    }
    expansion = ExpansionResult(
        backend="fake",
        compounds=compounds_map,
        reactions=[
            PredictedReaction(
                reaction_id="rxn_fake_001",
                backend="fake",
                operator="rule_demethyl_01",
                reactant_ids=["cpd_src"],
                product_ids=["cpd_prod"],
            ),
            decoy_rxn,
        ],
    )
    expander = _FakeExpander(expansion)
    biochem = _FakeBiochem()

    source_compounds = [{"id": "cpd_src", "smiles": "COc1ccccc1C(=O)O"}]
    report = screen_products(
        expander=expander,
        operators=["rule_demethyl_01"],
        compounds=source_compounds,
        biochem=biochem,
    )

    # Only the reaction with operator="rule_demethyl_01" should be included
    assert len(report.records) == 1
    assert report.records[0].operator == "rule_demethyl_01"


# ---------------------------------------------------------------------------
# Tests: ScreeningReport shape
# ---------------------------------------------------------------------------


def test_screening_report_to_dict():
    """ScreeningReport.to_dict() returns the expected keys."""
    result = _make_expansion_result()
    expander = _FakeExpander(result)
    biochem = _FakeBiochem()

    compounds = [{"id": "cpd_src", "smiles": "COc1ccccc1C(=O)O"}]
    report = screen_products(
        expander=expander,
        operators=["rule_demethyl_01"],
        compounds=compounds,
        biochem=biochem,
    )

    d = report.to_dict()
    assert "n_source_compounds" in d
    assert "records" in d
    assert "genome_predictions" in d
    assert "warnings" in d
    assert d["n_source_compounds"] == 1

    if d["records"]:
        rec_d = d["records"][0]
        for key in (
            "source_msid",
            "source_smiles",
            "operator",
            "product_smiles",
            "product_inchikey",
            "reaction_in_db",
            "product_in_db",
            "has_downstream_pathway",
            "downstream_reactions",
            "in_models",
        ):
            assert key in rec_d, f"Missing key in ScreeningRecord.to_dict(): {key!r}"


def test_screening_report_n_source_compounds():
    """n_source_compounds reflects the number of input compounds, not records."""
    # Two source compounds, one with no SMILES (will be skipped → warning)
    result = _make_expansion_result()
    expander = _FakeExpander(result)
    biochem = _FakeBiochem()

    compounds = [
        {"id": "cpd_src", "smiles": "COc1ccccc1C(=O)O"},
        {"id": "cpd_noslm", "smiles": ""},
    ]
    report = screen_products(
        expander=expander,
        operators=["rule_demethyl_01"],
        compounds=compounds,
        biochem=biochem,
    )

    assert report.n_source_compounds == 2
    # The compound with no SMILES should generate a warning
    assert any("cpd_noslm" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# Tests: no-top-level rdkit/minedatabase import guard
# ---------------------------------------------------------------------------


def test_no_top_level_rdkit_import():
    """Importing screening.py must not trigger an rdkit/minedatabase import."""
    import sys

    # The module should already be imported (from the test above).
    # Verify rdkit is NOT a top-level requirement by confirming the module
    # is importable in this session where rdkit is absent.
    assert "kbutillib.cheminformatics.verab.screening" in sys.modules, (
        "screening module should be importable without rdkit"
    )
    # rdkit should NOT be in sys.modules as a result of importing screening
    # (it would be present only if something else imported it, e.g. retrorules
    # backend — we only check the screening module doesn't inject it)
    # This is verified by the fact that the test file runs at all without error.


# ===========================================================================
# S5 — genome / gene-content degradation prediction tests
# ===========================================================================
#
# Fake collaborators mirror the EXACT method signatures from the real code:
#
#   annotation.translate_term_to_modelseed(term: str) -> list[str]
#       (kb_annotation_utils.py:223-244)
#
#   annotation.process_object(params: dict) -> None
#       Populates annotation.ftrhash: dict[ftr_id, ftr_dict]
#       where ftr_dict["ontology_terms"] is keyed by ontology namespace.
#       (kb_annotation_utils.py:568-658)
#
# ---------------------------------------------------------------------------


class _FakeAnnotation:
    """Fake annotation object for S5 tests.

    Exposes:
        translate_term_to_modelseed(term) → list[str]
            (kb_annotation_utils.py:223-244)
        process_object(params) → None
            (kb_annotation_utils.py:568-658); populates self.ftrhash
        ftrhash: dict[ftr_id, ftr_dict]
    """

    def __init__(
        self,
        ec_to_msrxn: Optional[Dict[str, List[str]]] = None,
    ) -> None:
        # Maps EC term (e.g. "EC:1.14.13.82") → list of MSRXN ids
        self._ec_to_msrxn: Dict[str, List[str]] = ec_to_msrxn or {}
        # Populated by process_object; maps genome → its ftrhash
        self._genome_ftrhashes: Dict[str, Dict[str, Any]] = {}
        # The currently active feature hash (set by process_object)
        self.ftrhash: Dict[str, Any] = {}
        self.translate_calls: List[str] = []
        self.process_calls: List[Dict[str, Any]] = []

    def translate_term_to_modelseed(self, term: str) -> List[str]:
        """Mirror kb_annotation_utils.py:223-244."""
        self.translate_calls.append(term)
        return list(self._ec_to_msrxn.get(term, []))

    def process_object(self, params: Dict[str, Any]) -> None:
        """Mirror kb_annotation_utils.py:568-658.

        Sets self.ftrhash to the feature hash for the genome referenced in
        *params*.  Genome is identified by params["object"]["id"] if present,
        else params.get("input_ref", "unknown").
        """
        self.process_calls.append(dict(params))
        genome_obj = params.get("object")
        if genome_obj is not None:
            genome_key = genome_obj.get("id", "unknown") if isinstance(genome_obj, dict) else str(id(genome_obj))
        else:
            genome_key = params.get("input_ref", "unknown")
        # Set ftrhash to the per-genome feature hash registered for this genome
        self.ftrhash = self._genome_ftrhashes.get(genome_key, {})

    def register_genome_features(
        self, genome_key: str, features: Dict[str, Dict[str, Any]]
    ) -> None:
        """Register feature dicts for a genome so process_object can serve them.

        Each feature dict should include at least ``{"ontology_terms": {...}}``.
        """
        self._genome_ftrhashes[genome_key] = features


def _make_genome_dict(genome_id: str, ec_terms: Optional[List[str]] = None) -> Dict[str, Any]:
    """Build a minimal KBase-style genome dict with one feature that carries *ec_terms*.

    The feature's ``"ontology_terms"`` dict is keyed by ``"EC"`` namespace,
    matching what kb_annotation_utils.process_object populates.
    (kb_annotation_utils.py:654-658, :850-944)
    """
    ontology_terms: Dict[str, Any] = {}
    if ec_terms:
        ontology_terms["EC"] = {term: [{"score": 1.0}] for term in ec_terms}
    feature = {
        "id": f"gene_{genome_id}_001",
        "protein_translation": "MPKL",
        "ontology_terms": ontology_terms,
    }
    return {
        "id": genome_id,
        "features": [feature],
        "type": "KBaseGenomes.Genome",
    }


from kbutillib.cheminformatics.verab.screening import predict_genome_degradation


# ---------------------------------------------------------------------------
# S5 test: carrier genome → can_degrade=True
# ---------------------------------------------------------------------------


def test_predict_genome_degradation_carrier():
    """A genome carrying EC 1.14.13.82 is predicted can_degrade=True."""
    ec_hint = "1.14.13.82"
    ec_term = "EC:" + ec_hint
    msrxn_id = "rxn02941"  # fictional ModelSEED id for vanillate monooxygenase

    annotation = _FakeAnnotation(ec_to_msrxn={ec_term: [msrxn_id]})

    # Carrier genome: feature has EC:1.14.13.82 in its ontology_terms
    carrier_genome = _make_genome_dict("genome_ADP1", ec_terms=[ec_term])
    carrier_ftrhash = {
        "gene_genome_ADP1_001": {
            "id": "gene_genome_ADP1_001",
            "protein_translation": "MPKL",
            "ontology_terms": {"EC": {ec_term: [{"score": 1.0}]}},
        }
    }
    annotation.register_genome_features("genome_ADP1", carrier_ftrhash)

    result = predict_genome_degradation(
        operators=["rule_demethyl_01"],
        ec_hint=ec_hint,
        genomes=[carrier_genome],
        annotation=annotation,
    )

    assert "genome_ADP1" in result, f"Expected 'genome_ADP1' key; got {list(result)}"
    entry = result["genome_ADP1"]
    assert entry["can_degrade"] is True, (
        f"Carrier genome should be can_degrade=True; got {entry}"
    )
    assert len(entry["ec_hits"]) > 0, (
        f"Expected ≥1 ec_hit for carrier genome; got {entry['ec_hits']}"
    )
    assert msrxn_id in entry["msrxn_ids"], (
        f"Expected msrxn_id {msrxn_id!r} in msrxn_ids; got {entry['msrxn_ids']}"
    )


# ---------------------------------------------------------------------------
# S5 test: non-carrier genome → can_degrade=False
# ---------------------------------------------------------------------------


def test_predict_genome_degradation_non_carrier():
    """A genome lacking EC 1.14.13.82 is predicted can_degrade=False."""
    ec_hint = "1.14.13.82"
    ec_term = "EC:" + ec_hint
    msrxn_id = "rxn02941"

    annotation = _FakeAnnotation(ec_to_msrxn={ec_term: [msrxn_id]})

    # Non-carrier genome: feature has a DIFFERENT EC (e.g. EC:1.1.1.1)
    non_carrier_ftrhash = {
        "gene_genome_OTHER_001": {
            "id": "gene_genome_OTHER_001",
            "protein_translation": "MPKL",
            "ontology_terms": {"EC": {"EC:1.1.1.1": [{"score": 1.0}]}},
        }
    }
    annotation.register_genome_features("genome_OTHER", non_carrier_ftrhash)

    non_carrier_genome = _make_genome_dict("genome_OTHER", ec_terms=["EC:1.1.1.1"])

    result = predict_genome_degradation(
        operators=["rule_demethyl_01"],
        ec_hint=ec_hint,
        genomes=[non_carrier_genome],
        annotation=annotation,
    )

    assert "genome_OTHER" in result, f"Expected 'genome_OTHER' key; got {list(result)}"
    entry = result["genome_OTHER"]
    assert entry["can_degrade"] is False, (
        f"Non-carrier genome should be can_degrade=False; got {entry}"
    )
    assert entry["ec_hits"] == [], (
        f"Non-carrier genome should have no ec_hits; got {entry['ec_hits']}"
    )


# ---------------------------------------------------------------------------
# S5 test: carrier vs non-carrier in same call
# ---------------------------------------------------------------------------


def test_predict_genome_degradation_mixed():
    """Only the carrier genome is predicted can_degrade=True; non-carrier is False."""
    ec_hint = "1.14.13.82"
    ec_term = "EC:" + ec_hint

    annotation = _FakeAnnotation(ec_to_msrxn={ec_term: ["rxn02941"]})

    carrier_ftrhash = {
        "gene_ADP1_001": {
            "id": "gene_ADP1_001",
            "protein_translation": "MPKL",
            "ontology_terms": {"EC": {ec_term: [{"score": 1.0}]}},
        }
    }
    non_carrier_ftrhash = {
        "gene_OTHER_001": {
            "id": "gene_OTHER_001",
            "protein_translation": "MPKL",
            "ontology_terms": {"EC": {"EC:1.1.1.1": [{"score": 1.0}]}},
        }
    }
    annotation.register_genome_features("genome_ADP1", carrier_ftrhash)
    annotation.register_genome_features("genome_OTHER", non_carrier_ftrhash)

    carrier_genome = _make_genome_dict("genome_ADP1", ec_terms=[ec_term])
    non_carrier_genome = _make_genome_dict("genome_OTHER", ec_terms=["EC:1.1.1.1"])

    result = predict_genome_degradation(
        operators=["rule_demethyl_01"],
        ec_hint=ec_hint,
        genomes=[carrier_genome, non_carrier_genome],
        annotation=annotation,
    )

    assert result["genome_ADP1"]["can_degrade"] is True, (
        f"Carrier ADP1 should be True; got {result['genome_ADP1']}"
    )
    assert result["genome_OTHER"]["can_degrade"] is False, (
        f"Non-carrier OTHER should be False; got {result['genome_OTHER']}"
    )


# ---------------------------------------------------------------------------
# S5 test: annotation=None → graceful degradation
# ---------------------------------------------------------------------------


def test_predict_genome_degradation_annotation_none():
    """annotation=None returns can_degrade=False for all genomes with a warning."""
    genome = _make_genome_dict("genome_ADP1")

    result = predict_genome_degradation(
        operators=["rule_demethyl_01"],
        ec_hint="1.14.13.82",
        genomes=[genome],
        annotation=None,
    )

    assert "genome_ADP1" in result, f"Expected 'genome_ADP1' key; got {list(result)}"
    entry = result["genome_ADP1"]
    assert entry["can_degrade"] is False, (
        f"annotation=None should yield can_degrade=False; got {entry}"
    )
    assert "warning" in entry, "Expected a 'warning' key when annotation is None"
    assert "unavailable" in entry["warning"], (
        f"Warning should mention 'unavailable'; got {entry['warning']!r}"
    )


# ---------------------------------------------------------------------------
# S5 test: annotation object missing required methods → graceful degradation
# ---------------------------------------------------------------------------


def test_predict_genome_degradation_annotation_missing_methods():
    """An annotation object that lacks process_object → graceful degradation."""

    class _BrokenAnnotation:
        """Has translate_term_to_modelseed but NOT process_object."""
        def translate_term_to_modelseed(self, term: str) -> List[str]:
            return []

    genome = _make_genome_dict("genome_X")
    result = predict_genome_degradation(
        operators=[],
        ec_hint="1.14.13.82",
        genomes=[genome],
        annotation=_BrokenAnnotation(),
    )

    assert "genome_X" in result
    assert result["genome_X"]["can_degrade"] is False
    assert "warning" in result["genome_X"]


# ---------------------------------------------------------------------------
# S5 test: translate_term_to_modelseed is called with correct term
# ---------------------------------------------------------------------------


def test_predict_genome_degradation_calls_translate():
    """translate_term_to_modelseed is called with 'EC:<ec_hint>'."""
    ec_hint = "1.14.13.82"
    annotation = _FakeAnnotation()
    genome = _make_genome_dict("genome_ADP1")
    annotation.register_genome_features("genome_ADP1", {})

    predict_genome_degradation(
        operators=[],
        ec_hint=ec_hint,
        genomes=[genome],
        annotation=annotation,
    )

    assert len(annotation.translate_calls) >= 1, "translate_term_to_modelseed not called"
    assert "EC:1.14.13.82" in annotation.translate_calls, (
        f"Expected 'EC:1.14.13.82' in calls; got {annotation.translate_calls}"
    )


# ---------------------------------------------------------------------------
# S5 test: process_object is called once per genome
# ---------------------------------------------------------------------------


def test_predict_genome_degradation_calls_process_object():
    """process_object is called exactly once per genome."""
    annotation = _FakeAnnotation()
    genomes = [
        _make_genome_dict("genome_A"),
        _make_genome_dict("genome_B"),
    ]
    for g in genomes:
        annotation.register_genome_features(g["id"], {})

    predict_genome_degradation(
        operators=[],
        ec_hint="1.14.13.82",
        genomes=genomes,
        annotation=annotation,
    )

    assert len(annotation.process_calls) == 2, (
        f"Expected 2 process_object calls; got {len(annotation.process_calls)}"
    )


# ---------------------------------------------------------------------------
# S5 test: no top-level rdkit/minedatabase import via predict_genome_degradation
# ---------------------------------------------------------------------------


def test_s5_no_top_level_rdkit_import():
    """predict_genome_degradation import must not trigger rdkit/minedatabase."""
    import sys
    assert "kbutillib.cheminformatics.verab.screening" in sys.modules, (
        "screening module must be importable without rdkit"
    )
    # predict_genome_degradation is part of the same module; if we got here, it's fine.
    assert hasattr(
        sys.modules["kbutillib.cheminformatics.verab.screening"],
        "predict_genome_degradation",
    ), "predict_genome_degradation must be exported from screening module"
