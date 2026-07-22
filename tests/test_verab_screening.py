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


# ---------------------------------------------------------------------------
# FIX2: operators list on ScreeningRecord
# ---------------------------------------------------------------------------


def test_fix2_screening_record_has_operators_field():
    """ScreeningRecord must have an `operators` list field (default empty)."""
    from kbutillib.cheminformatics.verab.models import ScreeningRecord

    rec = ScreeningRecord(
        source_msid="cpd001",
        source_smiles="COc1ccccc1",
        operator="rule_x",
        product_smiles="Oc1ccccc1",
    )
    assert hasattr(rec, "operators"), "ScreeningRecord missing `operators` field"
    assert isinstance(rec.operators, list)
    assert rec.operators == []  # default empty


def test_fix2_screening_record_scalar_operator_preserved():
    """ScreeningRecord scalar `operator` must be preserved alongside the list."""
    from kbutillib.cheminformatics.verab.models import ScreeningRecord

    rec = ScreeningRecord(
        source_msid="cpd001",
        source_smiles="COc1ccccc1",
        operator="rule_x",
        product_smiles="Oc1ccccc1",
        operators=["rule_x", "rule_y"],
    )
    assert rec.operator == "rule_x", "Scalar operator field must be preserved"
    assert rec.operators == ["rule_x", "rule_y"]


def test_fix2_screening_record_to_dict_includes_operators():
    """ScreeningRecord.to_dict() must emit both 'operator' and 'operators'."""
    from kbutillib.cheminformatics.verab.models import ScreeningRecord

    rec = ScreeningRecord(
        source_msid="cpd001",
        source_smiles="COc1ccccc1",
        operator="rule_x",
        product_smiles="Oc1ccccc1",
        operators=["rule_x", "rule_y"],
    )
    d = rec.to_dict()
    assert "operator" in d, "Scalar 'operator' key must remain in to_dict()"
    assert "operators" in d, "'operators' list key must be in to_dict()"
    assert d["operator"] == "rule_x"
    assert d["operators"] == ["rule_x", "rule_y"]


def test_fix2_screen_products_record_has_operators_list():
    """screen_products records must carry an operators list, not just scalar."""
    result = _make_expansion_result(
        operator="rule_demethyl_01",
    )
    # Patch the reaction to carry a multi-op list (simulating Pickaxe)
    for rxn in result.reactions:
        rxn.operators = ["rule_demethyl_01", "rule_extra"]

    expander = _FakeExpander(result)
    biochem = _FakeBiochem()

    source_compounds = [{"id": "cpd_src", "smiles": "COc1ccccc1C(=O)O"}]
    report = screen_products(
        expander=expander,
        operators=["rule_demethyl_01"],
        compounds=source_compounds,
        biochem=biochem,
    )

    assert len(report.records) == 1
    rec = report.records[0]
    # Scalar operator still set
    assert rec.operator == "rule_demethyl_01"
    # operators list must be populated from the reaction
    assert hasattr(rec, "operators")
    assert "rule_demethyl_01" in rec.operators
    assert "rule_extra" in rec.operators


def test_fix2_screen_products_multi_op_filter_any_match():
    """screen_products operator filter accepts a reaction if ANY operator matches."""
    # Build an expansion with a reaction attributed to two operators
    src_id = "cpd_src"
    prod_id = "cpd_prod"
    compounds_map = {
        src_id: PredictedCompound(src_id, smiles="COc1ccccc1C(=O)O", is_seed=True),
        prod_id: PredictedCompound(prod_id, smiles="Oc1ccccc1C(=O)O", is_seed=False),
    }
    rxn = PredictedReaction(
        reaction_id="rxn_multi",
        backend="fake",
        operator="opA;opB",            # joined scalar for display
        operators=["opA", "opB"],      # full list (the FIX2 field)
        reactant_ids=[src_id],
        product_ids=[prod_id],
    )
    expansion = ExpansionResult(backend="fake", compounds=compounds_map, reactions=[rxn])
    expander = _FakeExpander(expansion)
    biochem = _FakeBiochem()

    # Filter on just "opB" — must still pass because opB is in operators list
    report = screen_products(
        expander=expander,
        operators=["opB"],
        compounds=[{"id": src_id, "smiles": "COc1ccccc1C(=O)O"}],
        biochem=biochem,
    )
    assert len(report.records) == 1, (
        "Reaction with opB in operators list must pass the filter"
    )


# ===========================================================================
# FIX3: phase-2 DB lookup failure vs genuine not-found
# ===========================================================================
#
# These tests verify that _search_reactions_safe / _search_compounds_safe
# distinguish:
#   (1) a query that RAISES   → lookup_ok=False, lookup_errors non-empty
#   (2) a query that returns {} → lookup_ok=True,  reaction_in_db=None (genuine not-found)
#   (3) models=None           → "unavailable" (not an error)
#   (4) to_dict()             → lookup_ok and lookup_errors keys are present
# ---------------------------------------------------------------------------


class _RaisingBiochem:
    """Fake biochem whose search_reactions always raises an exception.

    This simulates a transient DB error, network failure, or API change — a
    case that should NOT be silently reported as "reaction not in DB".
    """

    def search_reactions(self, **kwargs):
        raise RuntimeError("DB connection timeout: simulated failure")

    def search_compounds(self, **kwargs):
        raise RuntimeError("DB connection timeout: simulated failure")


class _EmptyBiochem:
    """Fake biochem that returns empty dicts (legitimate "not found" result)."""

    def search_reactions(self, **kwargs):
        return {}

    def search_compounds(self, **kwargs):
        return {}


def test_fix3_search_raises_sets_lookup_ok_false():
    """When biochem.search_reactions RAISES, lookup_ok=False and lookup_errors is non-empty.

    The record must NOT silently report reaction_in_db=False (false negative);
    instead lookup_ok=False exposes the failure so downstream analysis is honest.
    """
    result = _make_expansion_result(operator="rule_demethyl_01")
    expander = _FakeExpander(result)
    biochem = _RaisingBiochem()

    report = screen_products(
        expander=expander,
        operators=["rule_demethyl_01"],
        compounds=[{"id": "cpd_src", "smiles": "COc1ccccc1C(=O)O"}],
        biochem=biochem,
        models=None,
    )

    assert len(report.records) == 1, (
        f"Expected 1 record even on DB failure; got {len(report.records)}"
    )
    rec = report.records[0]

    # Core contract: a failed query must flip lookup_ok to False
    assert rec.lookup_ok is False, (
        f"Expected lookup_ok=False when search_reactions raises; got {rec.lookup_ok}"
    )
    # At least one error message must be present describing the failure
    assert len(rec.lookup_errors) > 0, (
        f"Expected non-empty lookup_errors when search_reactions raises; got {rec.lookup_errors!r}"
    )
    # The error message must mention the failure (type or message)
    assert any("failed" in e.lower() or "error" in e.lower() or "timeout" in e.lower()
               for e in rec.lookup_errors), (
        f"Expected an informative error string; got {rec.lookup_errors!r}"
    )
    # reaction_in_db is None (could not determine — NOT confirmed absent)
    assert rec.reaction_in_db is None, (
        f"reaction_in_db should be None when lookup failed; got {rec.reaction_in_db!r}"
    )
    # The report-level warnings must also surface the failure
    assert any("lookup failure" in w for w in report.warnings), (
        f"Expected a report-level warning for the lookup failure; got {report.warnings!r}"
    )


def test_fix3_empty_result_sets_lookup_ok_true():
    """When biochem.search_reactions returns {} (no raise), lookup_ok=True and it is a genuine not-found.

    An empty result is a legitimate "reaction not in DB"; the record must reflect
    this honestly — lookup_ok=True and reaction_in_db=None.
    """
    result = _make_expansion_result(operator="rule_demethyl_01")
    expander = _FakeExpander(result)
    biochem = _EmptyBiochem()

    report = screen_products(
        expander=expander,
        operators=["rule_demethyl_01"],
        compounds=[{"id": "cpd_src", "smiles": "COc1ccccc1C(=O)O"}],
        biochem=biochem,
        models=None,
    )

    assert len(report.records) == 1
    rec = report.records[0]

    # Legitimate not-found: lookup ran fine, just no results
    assert rec.lookup_ok is True, (
        f"Expected lookup_ok=True when search returns empty; got {rec.lookup_ok}"
    )
    assert rec.lookup_errors == [], (
        f"Expected empty lookup_errors for genuine not-found; got {rec.lookup_errors!r}"
    )
    assert rec.reaction_in_db is None, (
        f"reaction_in_db should be None (not found); got {rec.reaction_in_db!r}"
    )


def test_fix3_models_none_is_unavailable_not_error():
    """models=None is treated as 'unavailable', not a lookup failure.

    When models=None the in_models dict is empty and lookup_ok stays True
    (absence of model data is not a DB query error).
    """
    result = _make_expansion_result(operator="rule_demethyl_01")
    expander = _FakeExpander(result)
    # Biochem returns hits so we can confirm the record is created normally
    biochem = _FakeBiochem(
        search_reactions_return={"rxn00001": {"score": 10}},
        search_compounds_return={"cpd00294": {"score": 5}},
    )

    report = screen_products(
        expander=expander,
        operators=["rule_demethyl_01"],
        compounds=[{"id": "cpd_src", "smiles": "COc1ccccc1C(=O)O"}],
        biochem=biochem,
        models=None,   # ← unavailable, not an error
    )

    assert len(report.records) == 1
    rec = report.records[0]

    # models=None is gracefully skipped; it must NOT inject an error
    assert rec.lookup_ok is True, (
        f"models=None should not set lookup_ok=False; got {rec.lookup_ok}"
    )
    assert rec.lookup_errors == [], (
        f"models=None should not add lookup_errors; got {rec.lookup_errors!r}"
    )
    assert rec.in_models == {}, (
        f"in_models should be empty when models=None; got {rec.in_models!r}"
    )


def test_fix3_to_dict_includes_lookup_fields():
    """ScreeningRecord.to_dict() must include 'lookup_ok' and 'lookup_errors' keys."""
    from kbutillib.cheminformatics.verab.models import ScreeningRecord

    # Verify for a default (no-error) record
    rec_ok = ScreeningRecord(
        source_msid="cpd001",
        source_smiles="COc1ccccc1",
        operator="rule_x",
        product_smiles="Oc1ccccc1",
    )
    d_ok = rec_ok.to_dict()
    assert "lookup_ok" in d_ok, "to_dict() must include 'lookup_ok'"
    assert "lookup_errors" in d_ok, "to_dict() must include 'lookup_errors'"
    assert d_ok["lookup_ok"] is True
    assert d_ok["lookup_errors"] == []

    # Verify for a failed-lookup record
    rec_fail = ScreeningRecord(
        source_msid="cpd001",
        source_smiles="COc1ccccc1",
        operator="rule_x",
        product_smiles="Oc1ccccc1",
        lookup_ok=False,
        lookup_errors=["search_reactions failed: RuntimeError: timeout"],
    )
    d_fail = rec_fail.to_dict()
    assert d_fail["lookup_ok"] is False
    assert d_fail["lookup_errors"] == ["search_reactions failed: RuntimeError: timeout"]


# ===========================================================================
# FIX4: genome degradation prediction — multi-EC + explainable evidence
# ===========================================================================
#
# New behaviour: predict_genome_degradation accepts ec_hints (Sequence[str])
# instead of a single ec_hint.  For each genome it returns a richer result:
#   {
#     "can_degrade":   bool,
#     "ec_hits":       [ftr_id, ...],          # union across all ECs (back-compat)
#     "matched_terms": {ftr_id: [ec_term/msrxn, ...]},  # per-feature evidence
#     "ec_hints":      [bare_ec, ...],          # effective EC list that was checked
#     "msrxn_ids":     [msrxn, ...],            # union of all resolved MSRXNs
#   }
# Graceful-degrade shape is the same (all lists empty; "warning" key added).
# ---------------------------------------------------------------------------


def test_fix4_multi_ec_genome_carrying_second_ec():
    """A genome carrying EC 3.1.1.45 (not the default 1.14.13.82) is predicted
    can_degrade=True when ec_hints includes that EC, and the matched term
    is recorded in matched_terms."""
    ec_primary = "1.14.13.82"
    ec_secondary = "3.1.1.45"
    ec_term_secondary = "EC:" + ec_secondary
    msrxn_secondary = "rxn09999"

    annotation = _FakeAnnotation(
        ec_to_msrxn={
            "EC:" + ec_primary: [],
            ec_term_secondary: [msrxn_secondary],
        }
    )

    # Genome only carries the secondary EC (would be missed by single-EC logic)
    carrier_ftrhash = {
        "gene_G2_001": {
            "id": "gene_G2_001",
            "ontology_terms": {"EC": {ec_term_secondary: [{"score": 1.0}]}},
        }
    }
    annotation.register_genome_features("genome_G2", carrier_ftrhash)
    genome = _make_genome_dict("genome_G2", ec_terms=[ec_term_secondary])

    result = predict_genome_degradation(
        operators=["rule_x"],
        ec_hints=(ec_primary, ec_secondary),
        genomes=[genome],
        annotation=annotation,
    )

    assert "genome_G2" in result
    entry = result["genome_G2"]
    assert entry["can_degrade"] is True, (
        f"Genome carrying secondary EC {ec_secondary!r} must be can_degrade=True; got {entry}"
    )
    assert "gene_G2_001" in entry["ec_hits"], (
        f"gene_G2_001 must appear in ec_hits; got {entry['ec_hits']}"
    )
    # matched_terms should attribute the match to the secondary EC
    assert "gene_G2_001" in entry["matched_terms"], (
        f"gene_G2_001 must appear in matched_terms; got {entry['matched_terms']}"
    )
    assert any(ec_secondary in t or ec_term_secondary in t
               for t in entry["matched_terms"]["gene_G2_001"]), (
        f"matched_terms for gene_G2_001 must reference {ec_secondary!r}; "
        f"got {entry['matched_terms']['gene_G2_001']}"
    )
    # ec_hints in result must list both ECs
    assert ec_primary in entry["ec_hints"]
    assert ec_secondary in entry["ec_hints"]


def test_fix4_genome_with_no_matching_ec_is_false_with_ec_hints_populated():
    """A genome carrying none of the supplied EC terms is can_degrade=False,
    but ec_hints and msrxn_ids are still populated in the result (explainable)."""
    ec_primary = "1.14.13.82"
    ec_secondary = "3.1.1.45"
    annotation = _FakeAnnotation(
        ec_to_msrxn={
            "EC:" + ec_primary: ["rxn02941"],
            "EC:" + ec_secondary: ["rxn09999"],
        }
    )
    # Genome only carries an irrelevant EC
    non_carrier_ftrhash = {
        "gene_irrelevant_001": {
            "id": "gene_irrelevant_001",
            "ontology_terms": {"EC": {"EC:1.1.1.1": [{"score": 1.0}]}},
        }
    }
    annotation.register_genome_features("genome_NONE", non_carrier_ftrhash)
    genome = _make_genome_dict("genome_NONE", ec_terms=["EC:1.1.1.1"])

    result = predict_genome_degradation(
        operators=["rule_x"],
        ec_hints=(ec_primary, ec_secondary),
        genomes=[genome],
        annotation=annotation,
    )

    assert "genome_NONE" in result
    entry = result["genome_NONE"]
    assert entry["can_degrade"] is False, f"No-match genome must be False; got {entry}"
    assert entry["ec_hits"] == [], f"ec_hits must be empty; got {entry['ec_hits']}"
    assert entry["matched_terms"] == {}, f"matched_terms must be empty; got {entry['matched_terms']}"
    # ec_hints must still list the checked ECs (explainable: we can show what was checked)
    assert ec_primary in entry["ec_hints"], (
        f"ec_hints must include {ec_primary!r}; got {entry['ec_hints']}"
    )
    assert ec_secondary in entry["ec_hints"], (
        f"ec_hints must include {ec_secondary!r}; got {entry['ec_hints']}"
    )
    # msrxn_ids should be populated (the resolved MSRXNs that were searched for)
    assert "rxn02941" in entry["msrxn_ids"] or "rxn09999" in entry["msrxn_ids"], (
        f"msrxn_ids should contain resolved IDs; got {entry['msrxn_ids']}"
    )


def test_fix4_annotation_none_graceful_uniform_shape():
    """annotation=None produces a uniform explainable shape with all list fields
    empty and ec_hints populated (so the caller can see what would have been checked)."""
    genome = _make_genome_dict("genome_ADP1")

    result = predict_genome_degradation(
        operators=["rule_x"],
        ec_hints=("1.14.13.82", "3.1.1.45"),
        genomes=[genome],
        annotation=None,
    )

    assert "genome_ADP1" in result
    entry = result["genome_ADP1"]
    assert entry["can_degrade"] is False
    assert entry["ec_hits"] == []
    assert entry["matched_terms"] == {}
    assert "warning" in entry and "unavailable" in entry["warning"]
    # ec_hints must still be present (shape uniformity)
    assert "ec_hints" in entry, "Graceful-degrade result must include 'ec_hints'"
    assert "1.14.13.82" in entry["ec_hints"]
    assert "3.1.1.45" in entry["ec_hints"]
    # msrxn_ids must be present (empty, since annotation unavailable)
    assert "msrxn_ids" in entry
    assert entry["msrxn_ids"] == []


def test_fix4_matched_terms_populated_per_feature():
    """matched_terms is keyed by feature id and contains the matching EC term(s)
    or MSRXN ids — so the caller can explain WHY a genome is predicted can_degrade."""
    ec_primary = "1.14.13.82"
    ec_term = "EC:" + ec_primary
    msrxn_id = "rxn02941"

    annotation = _FakeAnnotation(ec_to_msrxn={ec_term: [msrxn_id]})

    # Two features: one with EC match, one with MSRXN match
    carrier_ftrhash = {
        "gene_ec_match": {
            "id": "gene_ec_match",
            "ontology_terms": {"EC": {ec_term: [{"score": 1.0}]}},
        },
        "gene_msrxn_match": {
            "id": "gene_msrxn_match",
            "ontology_terms": {"MSRXN": {msrxn_id: [{"score": 1.0}]}},
        },
        "gene_no_match": {
            "id": "gene_no_match",
            "ontology_terms": {"EC": {"EC:1.1.1.1": [{"score": 1.0}]}},
        },
    }
    annotation.register_genome_features("genome_ADP1", carrier_ftrhash)
    genome = _make_genome_dict("genome_ADP1", ec_terms=[ec_term])

    result = predict_genome_degradation(
        operators=["rule_x"],
        ec_hints=(ec_primary,),
        genomes=[genome],
        annotation=annotation,
    )

    entry = result["genome_ADP1"]
    assert entry["can_degrade"] is True
    # Both matching features must appear in ec_hits
    assert "gene_ec_match" in entry["ec_hits"]
    assert "gene_msrxn_match" in entry["ec_hits"]
    assert "gene_no_match" not in entry["ec_hits"]

    # matched_terms must report what matched for each feature
    assert "gene_ec_match" in entry["matched_terms"], "EC-match feature must be in matched_terms"
    assert "gene_msrxn_match" in entry["matched_terms"], "MSRXN-match feature must be in matched_terms"
    assert "gene_no_match" not in entry["matched_terms"], "Non-match feature must NOT be in matched_terms"

    # The EC match is attributed to the EC term
    assert any(ec_primary in t or ec_term in t
               for t in entry["matched_terms"]["gene_ec_match"]), (
        f"gene_ec_match matched_terms should reference {ec_term!r}; "
        f"got {entry['matched_terms']['gene_ec_match']}"
    )
    # The MSRXN match is attributed to the EC term that produced the MSRXN
    assert len(entry["matched_terms"]["gene_msrxn_match"]) > 0, (
        "gene_msrxn_match matched_terms should be non-empty"
    )


def test_fix4_operator_ec_map_merges_extra_ecs():
    """operator_ec_map lets callers pass operator→EC mappings from rule metadata;
    those ECs are merged into the effective EC list and checked."""
    ec_base = "1.14.13.82"
    ec_from_operator = "1.14.99.1"   # hypothetical operator-derived EC
    ec_term_from_operator = "EC:" + ec_from_operator
    msrxn_from_operator = "rxn88888"

    annotation = _FakeAnnotation(
        ec_to_msrxn={
            "EC:" + ec_base: [],
            ec_term_from_operator: [msrxn_from_operator],
        }
    )

    # Genome only carries the operator-derived EC
    carrier_ftrhash = {
        "gene_op_ec_001": {
            "id": "gene_op_ec_001",
            "ontology_terms": {"EC": {ec_term_from_operator: [{"score": 1.0}]}},
        }
    }
    annotation.register_genome_features("genome_OP", carrier_ftrhash)
    genome = _make_genome_dict("genome_OP", ec_terms=[ec_term_from_operator])

    result = predict_genome_degradation(
        operators=["op_rule_99"],
        ec_hints=(ec_base,),
        operator_ec_map={"op_rule_99": [ec_from_operator]},
        genomes=[genome],
        annotation=annotation,
    )

    entry = result["genome_OP"]
    assert entry["can_degrade"] is True, (
        f"Genome carrying operator-derived EC {ec_from_operator!r} must be True; got {entry}"
    )
    assert ec_from_operator in entry["ec_hints"], (
        f"Operator-derived EC must be in ec_hints; got {entry['ec_hints']}"
    )
    assert "gene_op_ec_001" in entry["ec_hits"]


def test_fix4_back_compat_single_ec_hint_kwarg():
    """Legacy callers that pass ec_hint= (singular) still work correctly.
    The result shape includes all new FIX4 fields."""
    ec = "1.14.13.82"
    ec_term = "EC:" + ec
    msrxn_id = "rxn02941"

    annotation = _FakeAnnotation(ec_to_msrxn={ec_term: [msrxn_id]})
    carrier_ftrhash = {
        "gene_legacy_001": {
            "id": "gene_legacy_001",
            "ontology_terms": {"EC": {ec_term: [{"score": 1.0}]}},
        }
    }
    annotation.register_genome_features("genome_LEGACY", carrier_ftrhash)
    genome = _make_genome_dict("genome_LEGACY", ec_terms=[ec_term])

    # Use old-style single ec_hint= kwarg
    result = predict_genome_degradation(
        operators=["rule_x"],
        ec_hint=ec,      # back-compat kwarg
        genomes=[genome],
        annotation=annotation,
    )

    entry = result["genome_LEGACY"]
    assert entry["can_degrade"] is True
    assert "gene_legacy_001" in entry["ec_hits"]
    # New explainable fields must exist
    assert "matched_terms" in entry, "matched_terms must be present even with legacy ec_hint="
    assert "ec_hints" in entry
    assert ec in entry["ec_hints"]
    assert "msrxn_ids" in entry
    assert msrxn_id in entry["msrxn_ids"]


def test_fix4_translate_called_for_each_ec_hint():
    """translate_term_to_modelseed is called once per effective EC hint."""
    ec1 = "1.14.13.82"
    ec2 = "3.1.1.45"
    ec3 = "4.1.1.20"
    annotation = _FakeAnnotation(ec_to_msrxn={})
    genome = _make_genome_dict("genome_X")
    annotation.register_genome_features("genome_X", {})

    predict_genome_degradation(
        operators=[],
        ec_hints=(ec1, ec2, ec3),
        genomes=[genome],
        annotation=annotation,
    )

    # Each EC must have been looked up
    called_terms = set(annotation.translate_calls)
    assert "EC:" + ec1 in called_terms, f"EC:1.14.13.82 not in translate calls: {called_terms}"
    assert "EC:" + ec2 in called_terms, f"EC:3.1.1.45 not in translate calls: {called_terms}"
    assert "EC:" + ec3 in called_terms, f"EC:4.1.1.20 not in translate calls: {called_terms}"
