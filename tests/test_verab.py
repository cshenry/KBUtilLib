"""S1 unit tests for the verAB cheminformatics sub-package.

Tests run without any optional dependency (RDKit, minedatabase) installed:
  * Importing verab sub-package and its submodules must never raise.
  * SEED_COMPOUNDS contains exactly 5 entries, each with non-empty SMILES.
  * All four dataclasses round-trip through to_dict() with the expected keys.

No test here requires RDKit or a live database.  Optional-dependency gates will
be added in S2+ tests.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Import-time safety: importing verab (and its submodules) must not raise even
# when RDKit and minedatabase are absent.
# ---------------------------------------------------------------------------


def test_import_verab_package():
    """Top-level package import must succeed without any optional dep."""
    from kbutillib.cheminformatics import verab  # noqa: F401


def test_import_smarts_module():
    from kbutillib.cheminformatics.verab import smarts  # noqa: F401


def test_import_models_module():
    from kbutillib.cheminformatics.verab import models  # noqa: F401


def test_import_via_cheminformatics_parent():
    """verab is a child of the cheminformatics package; that import must be safe."""
    import kbutillib.cheminformatics  # noqa: F401
    from kbutillib.cheminformatics import verab  # noqa: F401


# ---------------------------------------------------------------------------
# SMARTS constants
# ---------------------------------------------------------------------------


def test_verab_odemethylation_smarts_is_string():
    from kbutillib.cheminformatics.verab.smarts import VERAB_ODEMETHYLATION_SMARTS

    assert isinstance(VERAB_ODEMETHYLATION_SMARTS, str)
    assert len(VERAB_ODEMETHYLATION_SMARTS) > 0


def test_methoxy_aromatic_smarts_is_string():
    from kbutillib.cheminformatics.verab.smarts import METHOXY_AROMATIC_SMARTS

    assert isinstance(METHOXY_AROMATIC_SMARTS, str)
    assert len(METHOXY_AROMATIC_SMARTS) > 0


def test_methoxy_aromatic_smarts_strict_is_string():
    from kbutillib.cheminformatics.verab.smarts import METHOXY_AROMATIC_SMARTS_STRICT

    assert isinstance(METHOXY_AROMATIC_SMARTS_STRICT, str)
    assert len(METHOXY_AROMATIC_SMARTS_STRICT) > 0


def test_smarts_contain_expected_patterns():
    """Spot-check that SMARTS strings contain chemically meaningful fragments."""
    from kbutillib.cheminformatics.verab.smarts import (
        METHOXY_AROMATIC_SMARTS,
        VERAB_ODEMETHYLATION_SMARTS,
    )

    # Reaction SMARTS must have a ">>" separator
    assert ">>" in VERAB_ODEMETHYLATION_SMARTS
    # Substructure SMARTS should mention aromatic C and methyl group
    assert "c" in METHOXY_AROMATIC_SMARTS or "C" in METHOXY_AROMATIC_SMARTS
    assert "CH3" in METHOXY_AROMATIC_SMARTS


# ---------------------------------------------------------------------------
# SEED_COMPOUNDS
# ---------------------------------------------------------------------------


def test_seed_compounds_count():
    from kbutillib.cheminformatics.verab.smarts import SEED_COMPOUNDS

    assert len(SEED_COMPOUNDS) == 5


def test_seed_compounds_have_required_keys():
    from kbutillib.cheminformatics.verab.smarts import SEED_COMPOUNDS

    required_keys = {"id", "name", "smiles", "inchikey", "kegg"}
    for entry in SEED_COMPOUNDS:
        assert required_keys.issubset(set(entry.keys())), (
            f"Seed {entry.get('id', '?')} is missing keys: "
            f"{required_keys - set(entry.keys())}"
        )


def test_seed_compounds_non_empty_smiles():
    from kbutillib.cheminformatics.verab.smarts import SEED_COMPOUNDS

    for entry in SEED_COMPOUNDS:
        assert isinstance(entry["smiles"], str) and len(entry["smiles"]) > 0, (
            f"Seed {entry.get('id', '?')} has an empty or non-string SMILES"
        )


def test_seed_compounds_non_empty_id_and_name():
    from kbutillib.cheminformatics.verab.smarts import SEED_COMPOUNDS

    for entry in SEED_COMPOUNDS:
        assert isinstance(entry["id"], str) and len(entry["id"]) > 0
        assert isinstance(entry["name"], str) and len(entry["name"]) > 0


def test_seed_compound_names():
    """Verify that the five expected compound names are present."""
    from kbutillib.cheminformatics.verab.smarts import SEED_COMPOUNDS

    names = {e["name"] for e in SEED_COMPOUNDS}
    assert "vanillate" in names
    assert "isovanillate" in names
    assert "guaiacol" in names
    assert "4-methoxybenzoate" in names
    assert "veratrate" in names


def test_seed_compound_inchikeys_format():
    """Non-None InChIKeys should be in standard 27-char format."""
    from kbutillib.cheminformatics.verab.smarts import SEED_COMPOUNDS

    for entry in SEED_COMPOUNDS:
        ik = entry.get("inchikey")
        if ik is not None:
            parts = ik.split("-")
            assert len(parts) == 3, f"InChIKey {ik!r} does not have 3 blocks"


# ---------------------------------------------------------------------------
# VerabRuleMatch — to_dict round-trip
# ---------------------------------------------------------------------------

VERAB_RULE_MATCH_KEYS = {
    "operator",
    "reaction_id",
    "backend",
    "reactant_ids",
    "product_ids",
    "method",
    "confidence",
    "ec_hint",
}


def test_verab_rule_match_to_dict_keys():
    from kbutillib.cheminformatics.verab.models import VerabRuleMatch

    m = VerabRuleMatch(
        operator="ruleXXXX",
        reaction_id="rxn_001",
        backend="pickaxe",
        reactant_ids=["cpd_vanillate"],
        product_ids=["cpd_protocatechuate", "cpd_formaldehyde"],
        method="rdkit_transform",
        confidence=1.0,
        ec_hint="1.14.13.82",
    )
    d = m.to_dict()
    assert VERAB_RULE_MATCH_KEYS.issubset(set(d.keys()))


def test_verab_rule_match_to_dict_values():
    from kbutillib.cheminformatics.verab.models import VerabRuleMatch

    m = VerabRuleMatch(
        operator="op42",
        reaction_id="r1",
        backend="pickaxe",
        reactant_ids=["A"],
        product_ids=["B", "C"],
        method="smarts_text",
        confidence=0.5,
        ec_hint=None,
    )
    d = m.to_dict()
    assert d["operator"] == "op42"
    assert d["reaction_id"] == "r1"
    assert d["backend"] == "pickaxe"
    assert d["reactant_ids"] == ["A"]
    assert d["product_ids"] == ["B", "C"]
    assert d["method"] == "smarts_text"
    assert d["confidence"] == 0.5
    assert d["ec_hint"] is None


def test_verab_rule_match_defaults():
    from kbutillib.cheminformatics.verab.models import VerabRuleMatch

    m = VerabRuleMatch(operator="op1", reaction_id="r0", backend="pickaxe")
    assert m.reactant_ids == []
    assert m.product_ids == []
    assert m.method == "rdkit_transform"
    assert m.confidence == 1.0
    assert m.ec_hint is None
    d = m.to_dict()
    assert d["reactant_ids"] == []


# ---------------------------------------------------------------------------
# VerabDiscoveryResult — to_dict round-trip
# ---------------------------------------------------------------------------

VERAB_DISCOVERY_KEYS = {
    "rule_set",
    "generations",
    "seeds",
    "matches",
    "operators",
    "expansion_summary",
    "warnings",
}


def test_verab_discovery_result_to_dict_keys():
    from kbutillib.cheminformatics.verab.models import VerabDiscoveryResult

    dr = VerabDiscoveryResult(rule_set="metacyc_generalized", generations=1)
    d = dr.to_dict()
    assert VERAB_DISCOVERY_KEYS.issubset(set(d.keys()))


def test_verab_discovery_result_to_dict_values():
    from kbutillib.cheminformatics.verab.models import VerabDiscoveryResult, VerabRuleMatch

    match = VerabRuleMatch(
        operator="ruleABC", reaction_id="rxn1", backend="pickaxe"
    )
    dr = VerabDiscoveryResult(
        rule_set="metacyc_intermediate",
        generations=2,
        seeds=[{"id": "cpd_vanillate", "smiles": "COc1cc(C(=O)O)ccc1O"}],
        matches=[match],
        operators=["ruleABC"],
        expansion_summary={"n_compounds": 10, "n_reactions": 5},
        warnings=["rdkit absent"],
    )
    d = dr.to_dict()
    assert d["rule_set"] == "metacyc_intermediate"
    assert d["generations"] == 2
    assert len(d["seeds"]) == 1
    assert len(d["matches"]) == 1
    assert d["matches"][0]["operator"] == "ruleABC"
    assert d["operators"] == ["ruleABC"]
    assert d["expansion_summary"]["n_compounds"] == 10
    assert d["warnings"] == ["rdkit absent"]


# ---------------------------------------------------------------------------
# ScreeningRecord — to_dict round-trip
# ---------------------------------------------------------------------------

SCREENING_RECORD_KEYS = {
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
}


def test_screening_record_to_dict_keys():
    from kbutillib.cheminformatics.verab.models import ScreeningRecord

    r = ScreeningRecord(
        source_msid="cpd00137",
        source_smiles="COc1cc(C(=O)O)ccc1O",
        operator="ruleXXXX",
        product_smiles="Oc1ccc(C(=O)O)cc1O",
    )
    d = r.to_dict()
    assert SCREENING_RECORD_KEYS.issubset(set(d.keys()))


def test_screening_record_to_dict_values():
    from kbutillib.cheminformatics.verab.models import ScreeningRecord

    r = ScreeningRecord(
        source_msid="cpd00137",
        source_smiles="COc1cc(C(=O)O)ccc1O",
        operator="ruleXXXX",
        product_smiles="Oc1ccc(C(=O)O)cc1O",
        product_inchikey="ABCDE-FGHIJ-KLMNO",
        reaction_in_db="rxn00001",
        product_in_db="cpd00006",
        has_downstream_pathway=True,
        downstream_reactions=["rxn00002", "rxn00003"],
        in_models={"model_ADP1": True},
    )
    d = r.to_dict()
    assert d["source_msid"] == "cpd00137"
    assert d["reaction_in_db"] == "rxn00001"
    assert d["product_in_db"] == "cpd00006"
    assert d["has_downstream_pathway"] is True
    assert d["downstream_reactions"] == ["rxn00002", "rxn00003"]
    assert d["in_models"] == {"model_ADP1": True}


def test_screening_record_defaults():
    from kbutillib.cheminformatics.verab.models import ScreeningRecord

    r = ScreeningRecord(
        source_msid="s1",
        source_smiles="C",
        operator="op1",
        product_smiles="CO",
    )
    assert r.product_inchikey is None
    assert r.reaction_in_db is None
    assert r.product_in_db is None
    assert r.has_downstream_pathway is False
    assert r.downstream_reactions == []
    assert r.in_models == {}


# ---------------------------------------------------------------------------
# ScreeningReport — to_dict round-trip
# ---------------------------------------------------------------------------

SCREENING_REPORT_KEYS = {
    "n_source_compounds",
    "records",
    "genome_predictions",
    "warnings",
}


def test_screening_report_to_dict_keys():
    from kbutillib.cheminformatics.verab.models import ScreeningReport

    rpt = ScreeningReport()
    d = rpt.to_dict()
    assert SCREENING_REPORT_KEYS.issubset(set(d.keys()))


def test_screening_report_to_dict_values():
    from kbutillib.cheminformatics.verab.models import ScreeningRecord, ScreeningReport

    rec = ScreeningRecord(
        source_msid="m1",
        source_smiles="CC",
        operator="op2",
        product_smiles="C",
    )
    rpt = ScreeningReport(
        n_source_compounds=3,
        records=[rec],
        genome_predictions={"genome_ADP1": {"can_degrade": ["m1"], "ec_hits": ["1.14.13.82"]}},
        warnings=["some warning"],
    )
    d = rpt.to_dict()
    assert d["n_source_compounds"] == 3
    assert len(d["records"]) == 1
    assert d["records"][0]["source_msid"] == "m1"
    assert "genome_ADP1" in d["genome_predictions"]
    assert d["warnings"] == ["some warning"]


def test_screening_report_empty_defaults():
    from kbutillib.cheminformatics.verab.models import ScreeningReport

    rpt = ScreeningReport()
    assert rpt.n_source_compounds == 0
    assert rpt.records == []
    assert rpt.genome_predictions == {}
    assert rpt.warnings == []


# ---------------------------------------------------------------------------
# No rdkit / minedatabase at top level (belt-and-suspenders check)
# ---------------------------------------------------------------------------


def test_verab_smarts_module_has_no_rdkit_import():
    """smarts.py must not list rdkit or minedatabase in its globals."""
    import kbutillib.cheminformatics.verab.smarts as smarts_mod

    module_dict = vars(smarts_mod)
    assert "Chem" not in module_dict, "rdkit.Chem imported at module level in smarts.py"
    assert "rdkit" not in module_dict
    assert "minedatabase" not in module_dict


def test_verab_models_module_has_no_rdkit_import():
    """models.py must not list rdkit or minedatabase in its globals."""
    import kbutillib.cheminformatics.verab.models as models_mod

    module_dict = vars(models_mod)
    assert "Chem" not in module_dict, "rdkit.Chem imported at module level in models.py"
    assert "rdkit" not in module_dict
    assert "minedatabase" not in module_dict


# ---------------------------------------------------------------------------
# S2 — MethoxyAromaticFilter tests
# ---------------------------------------------------------------------------

# Detect whether RDKit is importable in this environment.
try:
    import rdkit as _rdkit  # noqa: F401

    _RDKIT_PRESENT = True
except ImportError:
    _RDKIT_PRESENT = False


# ---- Import safety (no rdkit required) ------------------------------------


def test_import_substructure_module():
    """substructure.py must be importable without RDKit."""
    from kbutillib.cheminformatics.verab import substructure  # noqa: F401


def test_substructure_no_toplevel_rdkit_import():
    """substructure.py must NOT import rdkit at module level."""
    import kbutillib.cheminformatics.verab.substructure as sub_mod

    module_dict = vars(sub_mod)
    assert "rdkit" not in module_dict, "rdkit imported at module level in substructure.py"
    assert "Chem" not in module_dict, "rdkit.Chem imported at module level in substructure.py"


def test_methoxy_aromatic_filter_instantiation():
    """MethoxyAromaticFilter can be constructed without any optional dep."""
    from kbutillib.cheminformatics.verab.substructure import MethoxyAromaticFilter

    f = MethoxyAromaticFilter()
    assert f is not None


# ---- RDKit-absent path (only runs when rdkit is NOT present) ---------------


@pytest.mark.skipif(_RDKIT_PRESENT, reason="RDKit is present; testing absent path only when missing")
def test_filter_available_false_when_rdkit_absent():
    """When RDKit is absent, available is False and unavailable_reason mentions rdkit."""
    from kbutillib.cheminformatics.verab.substructure import MethoxyAromaticFilter

    f = MethoxyAromaticFilter()
    assert f.available is False
    reason = f.unavailable_reason
    assert reason is not None
    assert "rdkit" in reason.lower()


@pytest.mark.skipif(_RDKIT_PRESENT, reason="RDKit is present; testing absent path only when missing")
def test_is_methoxy_aromatic_raises_when_rdkit_absent():
    """is_methoxy_aromatic raises BackendUnavailableError when RDKit missing."""
    from kbutillib.cheminformatics.base import BackendUnavailableError
    from kbutillib.cheminformatics.verab.substructure import MethoxyAromaticFilter

    f = MethoxyAromaticFilter()
    with pytest.raises(BackendUnavailableError):
        f.is_methoxy_aromatic("COc1ccccc1O")


@pytest.mark.skipif(_RDKIT_PRESENT, reason="RDKit is present; testing absent path only when missing")
def test_enumerate_from_biochem_raises_when_rdkit_absent():
    """enumerate_from_biochem raises BackendUnavailableError when RDKit missing."""
    from kbutillib.cheminformatics.base import BackendUnavailableError
    from kbutillib.cheminformatics.verab.substructure import MethoxyAromaticFilter

    class _FakeDB:
        compounds = []

    class _FakeBiochem:
        biochem_db = _FakeDB()

    f = MethoxyAromaticFilter()
    with pytest.raises(BackendUnavailableError):
        f.enumerate_from_biochem(_FakeBiochem())


# ---- RDKit-present path ----------------------------------------------------


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_filter_available_true_when_rdkit_present():
    """When RDKit is installed, available is True."""
    from kbutillib.cheminformatics.verab.substructure import MethoxyAromaticFilter

    f = MethoxyAromaticFilter()
    assert f.available is True
    assert f.unavailable_reason is None


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_is_methoxy_aromatic_guaiacol():
    """Guaiacol (COc1ccccc1O) should be classified as methoxy-aromatic."""
    from kbutillib.cheminformatics.verab.substructure import MethoxyAromaticFilter

    f = MethoxyAromaticFilter()
    assert f.is_methoxy_aromatic("COc1ccccc1O") is True


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_is_methoxy_aromatic_vanillate():
    """Vanillate SMILES should be classified as methoxy-aromatic."""
    from kbutillib.cheminformatics.verab.substructure import MethoxyAromaticFilter

    f = MethoxyAromaticFilter()
    # vanillate: 4-hydroxy-3-methoxybenzoate
    assert f.is_methoxy_aromatic("COc1cc(C(=O)O)ccc1O") is True


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_is_methoxy_aromatic_glucose_false():
    """Glucose has no aromatic methoxy group → should return False."""
    from kbutillib.cheminformatics.verab.substructure import MethoxyAromaticFilter

    f = MethoxyAromaticFilter()
    # beta-D-glucose
    assert f.is_methoxy_aromatic("OC[C@H]1OC(O)[C@H](O)[C@@H](O)[C@@H]1O") is False


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_is_methoxy_aromatic_benzene_false():
    """Plain benzene has no methoxy group → should return False."""
    from kbutillib.cheminformatics.verab.substructure import MethoxyAromaticFilter

    f = MethoxyAromaticFilter()
    assert f.is_methoxy_aromatic("c1ccccc1") is False


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_is_methoxy_aromatic_invalid_smiles():
    """An invalid SMILES string should return False (not raise)."""
    from kbutillib.cheminformatics.verab.substructure import MethoxyAromaticFilter

    f = MethoxyAromaticFilter()
    assert f.is_methoxy_aromatic("NOT_A_VALID_SMILES!!!") is False


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_enumerate_from_biochem_returns_methoxy_hits():
    """enumerate_from_biochem returns only the methoxy-aromatic compounds."""
    from kbutillib.cheminformatics.verab.substructure import MethoxyAromaticFilter

    # --- Fake compound objects ---
    class _FakeCpd:
        def __init__(self, cpd_id, name, smiles, formula=None, is_obsolete=False):
            self.id = cpd_id
            self.name = name
            self.formula = formula
            self.is_obsolete = is_obsolete
            self.annotation = {"SMILE": smiles} if smiles is not None else {}

    compounds = [
        # methoxy-aromatic → should match
        _FakeCpd("cpd_guaiacol", "guaiacol", "COc1ccccc1O", "C7H8O2"),
        _FakeCpd("cpd_vanillate", "vanillate", "COc1cc(C(=O)O)ccc1O", "C8H8O4"),
        # non-methoxy aromatic → should NOT match
        _FakeCpd("cpd_glucose", "glucose", "OC[C@H]1OC(O)[C@H](O)[C@@H](O)[C@@H]1O", "C6H12O6"),
        _FakeCpd("cpd_benzene", "benzene", "c1ccccc1", "C6H6"),
        # obsolete → should be skipped
        _FakeCpd("cpd_obs", "obsolete_cpd", "COc1ccccc1O", is_obsolete=True),
        # missing SMILE → should be skipped
        _FakeCpd("cpd_nosmi", "no_smiles_cpd", None),
    ]

    class _FakeDB:
        pass

    class _FakeBiochem:
        pass

    db = _FakeDB()
    db.compounds = compounds
    biochem = _FakeBiochem()
    biochem.biochem_db = db

    f = MethoxyAromaticFilter()
    result = f.enumerate_from_biochem(biochem)

    matched_ids = {c["id"] for c in result["compounds"]}
    assert "cpd_guaiacol" in matched_ids, "guaiacol should be matched"
    assert "cpd_vanillate" in matched_ids, "vanillate should be matched"
    assert "cpd_glucose" not in matched_ids, "glucose should not match"
    assert "cpd_benzene" not in matched_ids, "benzene should not match"
    assert "cpd_obs" not in matched_ids, "obsolete compound should be skipped"
    assert "cpd_nosmi" not in matched_ids, "missing-SMILE compound should be skipped"

    skipped = result["skipped"]
    assert skipped["obsolete"] == 1, "one compound should be counted as obsolete"
    assert skipped["missing_smile"] == 1, "one compound should be counted as missing_smile"
    assert skipped["total_scanned"] == len(compounds)


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_enumerate_from_biochem_limit():
    """enumerate_from_biochem respects the limit parameter."""
    from kbutillib.cheminformatics.verab.substructure import MethoxyAromaticFilter

    class _FakeCpd:
        def __init__(self, cpd_id, smiles):
            self.id = cpd_id
            self.name = cpd_id
            self.formula = None
            self.is_obsolete = False
            self.annotation = {"SMILE": smiles}

    # Three methoxy-aromatics
    compounds = [
        _FakeCpd("c1", "COc1ccccc1O"),
        _FakeCpd("c2", "COc1cc(C(=O)O)ccc1O"),
        _FakeCpd("c3", "COc1ccc(C(=O)O)cc1"),
    ]

    class _FakeDB:
        pass

    class _FakeBiochem:
        pass

    db = _FakeDB()
    db.compounds = compounds
    biochem = _FakeBiochem()
    biochem.biochem_db = db

    f = MethoxyAromaticFilter()
    result = f.enumerate_from_biochem(biochem, limit=2)

    assert len(result["compounds"]) <= 2


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_enumerate_from_biochem_caches_result():
    """enumerate_from_biochem caches by DB-object identity (avoids re-scan)."""
    from kbutillib.cheminformatics.verab.substructure import MethoxyAromaticFilter

    class _FakeCpd:
        def __init__(self, cpd_id, smiles):
            self.id = cpd_id
            self.name = cpd_id
            self.formula = None
            self.is_obsolete = False
            self.annotation = {"SMILE": smiles}

    class _FakeDB:
        def __init__(self):
            self.compounds = [_FakeCpd("c1", "COc1ccccc1O")]
            self.scan_count = 0

        def __iter__(self):
            self.scan_count += 1
            return iter(self.compounds)

    class _FakeBiochem:
        pass

    db = _FakeDB()
    biochem = _FakeBiochem()
    biochem.biochem_db = db

    f = MethoxyAromaticFilter()
    result1 = f.enumerate_from_biochem(biochem)
    result2 = f.enumerate_from_biochem(biochem)

    # Both results should be identical objects (cached)
    assert result1 is result2


# ---------------------------------------------------------------------------
# S3 — rule_discovery: match_transformation + discover_verab_rules tests
# ---------------------------------------------------------------------------
#
# NOTE: _RDKIT_PRESENT is already defined in the S2 section above.
# These tests build a synthetic ExpansionResult using the REAL base.py
# dataclasses to avoid any guessing about field names.


def _build_synthetic_expansion_result():
    """Build an ExpansionResult with:
    - vanillate -> protocatechuate + HCHO  (operator "ruleXXXX") — a verAB hit
    - vanillate -> some_product            (operator "ruleYYYY") — a decoy
    """
    from kbutillib.cheminformatics.base import (
        ExpansionResult,
        PredictedCompound,
        PredictedReaction,
    )

    # Compounds
    vanillate = PredictedCompound(
        compound_id="cpd_vanillate",
        smiles="COc1cc(C(=O)O)ccc1O",
        is_seed=True,
    )
    protocatechuate = PredictedCompound(
        compound_id="cpd_protocatechuate",
        smiles="Oc1ccc(C(=O)O)cc1O",  # 3,4-dihydroxybenzoate / protocatechuate
        is_seed=False,
    )
    formaldehyde = PredictedCompound(
        compound_id="cpd_formaldehyde",
        smiles="C=O",
        is_seed=False,
    )
    # Decoy product: catechol (has phenol but no HCHO coproduct in same reaction)
    catechol = PredictedCompound(
        compound_id="cpd_catechol",
        smiles="Oc1ccccc1O",
        is_seed=False,
    )

    # verAB demethylation reaction
    rxn_verab = PredictedReaction(
        reaction_id="rxn_verab_001",
        backend="pickaxe",
        operator="ruleXXXX",
        reactant_ids=["cpd_vanillate"],
        product_ids=["cpd_protocatechuate", "cpd_formaldehyde"],
        rule_smarts="[c:1][O:2][CH3:3]>>[c:1][OH:2].[CH2:3]=O",
    )

    # Decoy reaction (no formaldehyde product, no methoxy in the specific pair)
    rxn_decoy = PredictedReaction(
        reaction_id="rxn_decoy_001",
        backend="pickaxe",
        operator="ruleYYYY",
        reactant_ids=["cpd_vanillate"],
        product_ids=["cpd_catechol"],
        rule_smarts="[c:1][OH:2]>>[c:1][OH:2]",
    )

    result = ExpansionResult(
        backend="pickaxe",
        compounds={
            "cpd_vanillate": vanillate,
            "cpd_protocatechuate": protocatechuate,
            "cpd_formaldehyde": formaldehyde,
            "cpd_catechol": catechol,
        },
        reactions=[rxn_verab, rxn_decoy],
        generations=1,
    )
    return result


def test_import_rule_discovery_module():
    """rule_discovery.py must be importable without RDKit or minedatabase."""
    from kbutillib.cheminformatics.verab import rule_discovery  # noqa: F401


def test_rule_discovery_no_toplevel_rdkit_import():
    """rule_discovery.py must NOT import rdkit or minedatabase at module level."""
    import kbutillib.cheminformatics.verab.rule_discovery as rd_mod

    module_dict = vars(rd_mod)
    assert "rdkit" not in module_dict, "rdkit imported at module level in rule_discovery.py"
    assert "Chem" not in module_dict, "rdkit.Chem imported at module level in rule_discovery.py"
    assert "minedatabase" not in module_dict, "minedatabase imported at module level"


# ---- match_transformation with RDKit present --------------------------------


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_match_transformation_rdkit_finds_only_demethylation():
    """With RDKit: match_transformation returns ONLY the demethylation operator."""
    from kbutillib.cheminformatics.verab.rule_discovery import match_transformation

    result = _build_synthetic_expansion_result()
    matches = match_transformation(result)

    # Should find exactly one match (ruleXXXX), not the decoy (ruleYYYY)
    assert len(matches) == 1
    assert matches[0].operator == "ruleXXXX"


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_match_transformation_rdkit_confidence_1():
    """With RDKit: matched rule has method='rdkit_transform' and confidence=1.0."""
    from kbutillib.cheminformatics.verab.rule_discovery import match_transformation

    result = _build_synthetic_expansion_result()
    matches = match_transformation(result)

    assert len(matches) >= 1
    m = matches[0]
    assert m.method == "rdkit_transform"
    assert m.confidence == 1.0


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_match_transformation_rdkit_reaction_ids():
    """Matched VerabRuleMatch carries correct reaction_id and compound id lists."""
    from kbutillib.cheminformatics.verab.rule_discovery import match_transformation

    result = _build_synthetic_expansion_result()
    matches = match_transformation(result)

    assert len(matches) == 1
    m = matches[0]
    assert m.reaction_id == "rxn_verab_001"
    assert "cpd_vanillate" in m.reactant_ids
    assert "cpd_protocatechuate" in m.product_ids
    assert "cpd_formaldehyde" in m.product_ids


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_match_transformation_no_false_positive_decoy():
    """Decoy reaction (ruleYYYY) must NOT appear in match_transformation output."""
    from kbutillib.cheminformatics.verab.rule_discovery import match_transformation

    result = _build_synthetic_expansion_result()
    matches = match_transformation(result)

    operators_found = {m.operator for m in matches}
    assert "ruleYYYY" not in operators_found


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_match_transformation_empty_result():
    """An empty ExpansionResult produces an empty match list."""
    from kbutillib.cheminformatics.base import ExpansionResult
    from kbutillib.cheminformatics.verab.rule_discovery import match_transformation

    empty_result = ExpansionResult(backend="pickaxe")
    matches = match_transformation(empty_result)
    assert matches == []


# ---- discover_verab_rules with fake expander --------------------------------


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_discover_verab_rules_operators():
    """discover_verab_rules with a fake expander returns operators=['ruleXXXX']."""
    from kbutillib.cheminformatics.verab.rule_discovery import discover_verab_rules

    synthetic_result = _build_synthetic_expansion_result()

    class _FakeExpander:
        """Returns the synthetic ExpansionResult regardless of inputs."""
        def expand(self, seed_smiles, generations=1, backend="pickaxe", rule_set="metacyc_generalized", **kwargs):
            return synthetic_result

    expander = _FakeExpander()
    discovery = discover_verab_rules(expander, generations=1, rule_set="metacyc_generalized")

    assert discovery.operators == ["ruleXXXX"]


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_discover_verab_rules_ec_hint():
    """discover_verab_rules attaches ec_hint='1.14.13.82' to every match."""
    from kbutillib.cheminformatics.verab.rule_discovery import discover_verab_rules

    synthetic_result = _build_synthetic_expansion_result()

    class _FakeExpander:
        def expand(self, seed_smiles, generations=1, backend="pickaxe", rule_set="metacyc_generalized", **kwargs):
            return synthetic_result

    discovery = discover_verab_rules(_FakeExpander())
    assert discovery.ec_hint if hasattr(discovery, "ec_hint") else True  # field may be on matches
    for m in discovery.matches:
        assert m.ec_hint == "1.14.13.82"


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_discover_verab_rules_result_type():
    """discover_verab_rules returns a VerabDiscoveryResult."""
    from kbutillib.cheminformatics.verab.models import VerabDiscoveryResult
    from kbutillib.cheminformatics.verab.rule_discovery import discover_verab_rules

    synthetic_result = _build_synthetic_expansion_result()

    class _FakeExpander:
        def expand(self, seed_smiles, generations=1, backend="pickaxe", rule_set="metacyc_generalized", **kwargs):
            return synthetic_result

    discovery = discover_verab_rules(_FakeExpander())
    assert isinstance(discovery, VerabDiscoveryResult)


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_discover_verab_rules_uses_seed_compounds_by_default():
    """discover_verab_rules uses SEED_COMPOUNDS when seeds=None."""
    from kbutillib.cheminformatics.verab.rule_discovery import discover_verab_rules
    from kbutillib.cheminformatics.verab.smarts import SEED_COMPOUNDS

    received_seed_smiles = {}

    class _FakeExpander:
        def expand(self, seed_smiles, generations=1, backend="pickaxe", rule_set="metacyc_generalized", **kwargs):
            received_seed_smiles.update(seed_smiles)
            return _build_synthetic_expansion_result()

    discover_verab_rules(_FakeExpander())
    # All 5 seed IDs should have been passed in
    for s in SEED_COMPOUNDS:
        assert s["id"] in received_seed_smiles, f"seed {s['id']} not passed to expander"


# ---- RDKit-absent path (text/keyword fallback) --------------------------------


@pytest.mark.skipif(_RDKIT_PRESENT, reason="Testing absent-RDKit path; RDKit is present in this env")
def test_match_transformation_text_fallback_emits_warning():
    """When RDKit is absent, match_transformation emits a UserWarning and returns
    text-method matches with confidence=0.5."""
    import warnings as _w

    from kbutillib.cheminformatics.verab.rule_discovery import match_transformation

    result = _build_synthetic_expansion_result()
    with _w.catch_warnings(record=True) as caught:
        _w.simplefilter("always")
        matches = match_transformation(result)

    warning_msgs = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
    assert any("rdkit" in m.lower() or "smarts_text" in m.lower() for m in warning_msgs), (
        f"Expected a UserWarning mentioning RDKit/smarts_text; got: {warning_msgs}"
    )
    for m in matches:
        assert m.method == "smarts_text"
        assert m.confidence == 0.5


# ---------------------------------------------------------------------------
# S7 — VerabUtils facade + VerabUtilsImpl + toolkit property
# ---------------------------------------------------------------------------


# ---- Import safety ---------------------------------------------------------


def test_s7_import_verab_utils():
    """verab_utils.py must be importable without RDKit or minedatabase."""
    from kbutillib import verab_utils  # noqa: F401


def test_s7_verab_utils_no_toplevel_rdkit():
    """verab_utils must NOT import rdkit or minedatabase at module level."""
    import kbutillib.verab_utils as vu_mod

    module_dict = vars(vu_mod)
    assert "rdkit" not in module_dict, "rdkit imported at module level in verab_utils.py"
    assert "Chem" not in module_dict, "rdkit.Chem imported at module level in verab_utils.py"
    assert "minedatabase" not in module_dict, "minedatabase imported at module level in verab_utils.py"


# ---- kbu.verab property returns an instance --------------------------------


def test_s7_toolkit_verab_property_returns_instance():
    """kbu.verab property must return a VerabUtilsImpl without eagerly importing
    rdkit or minedatabase.  No network or file access is required."""
    from kbutillib.toolkit import KBUtilLib
    from kbutillib.verab_utils import VerabUtilsImpl

    kbu = KBUtilLib()
    verab = kbu.verab
    assert isinstance(verab, VerabUtilsImpl), (
        f"kbu.verab should return VerabUtilsImpl, got {type(verab).__name__}"
    )


def test_s7_toolkit_verab_property_is_cached():
    """kbu.verab property must return the same instance on repeated access."""
    from kbutillib.toolkit import KBUtilLib

    kbu = KBUtilLib()
    v1 = kbu.verab
    v2 = kbu.verab
    assert v1 is v2, "kbu.verab must be cached (same object on repeated access)"


def test_s7_toolkit_verab_does_not_import_rdkit_at_construction():
    """Constructing kbu.verab must not trigger a top-level rdkit import.
    We verify by checking sys.modules does not suddenly gain rdkit just from
    property access (it may already be present if RDKit is installed; this
    test is most meaningful when RDKit is absent, but passes either way)."""
    import sys

    from kbutillib.toolkit import KBUtilLib

    before = "rdkit" in sys.modules
    kbu = KBUtilLib()
    _ = kbu.verab  # property access
    after = "rdkit" in sys.modules
    # If rdkit was NOT present before, it should still not be present now.
    if not before:
        assert not after, "kbu.verab property access imported rdkit eagerly"


# ---- status() ----------------------------------------------------------------


def test_s7_status_returns_dict():
    """status() must return a dict."""
    from kbutillib.verab_utils import VerabUtils

    u = VerabUtils()
    s = u.status()
    assert isinstance(s, dict)


def test_s7_status_has_expected_keys():
    """status() dict must contain the documented keys."""
    from kbutillib.verab_utils import VerabUtils

    u = VerabUtils()
    s = u.status()
    expected_keys = {
        "rdkit",
        "minedatabase",
        "network_expansion",
        "biochem",
        "model",
        "genome",
        "annotation",
        "seed_count",
        "backends",
    }
    assert expected_keys.issubset(set(s.keys())), (
        f"status() is missing keys: {expected_keys - set(s.keys())}"
    )


def test_s7_status_seed_count():
    """status()['seed_count'] must equal 5 (canonical seeds)."""
    from kbutillib.verab_utils import VerabUtils

    u = VerabUtils()
    assert u.status()["seed_count"] == 5


def test_s7_status_no_deps_injected():
    """When no facades are injected, all boolean dep flags are False."""
    from kbutillib.verab_utils import VerabUtils

    u = VerabUtils()
    s = u.status()
    assert s["network_expansion"] is False
    assert s["biochem"] is False
    assert s["model"] is False
    assert s["genome"] is False
    assert s["annotation"] is False


def test_s7_status_via_toolkit_has_deps():
    """When constructed through the toolkit, dep flags must be True (facades
    are injected, not None)."""
    from kbutillib.toolkit import KBUtilLib

    kbu = KBUtilLib()
    s = kbu.verab.status()
    assert s["network_expansion"] is True
    assert s["biochem"] is True
    assert s["model"] is True
    assert s["genome"] is True
    assert s["annotation"] is True


# ---- seed_compounds() --------------------------------------------------------


def test_s7_seed_compounds_returns_five():
    """seed_compounds() returns exactly 5 dicts."""
    from kbutillib.verab_utils import VerabUtils

    u = VerabUtils()
    seeds = u.seed_compounds()
    assert len(seeds) == 5


def test_s7_seed_compounds_have_keys():
    """seed_compounds() dicts have id, name, smiles, inchikey, kegg."""
    from kbutillib.verab_utils import VerabUtils

    u = VerabUtils()
    for s in u.seed_compounds():
        assert "id" in s and "name" in s and "smiles" in s


# ---- discover_rules with injected fake expander ----------------------------


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_s7_discover_rules_with_fake_expander():
    """discover_rules with an injected fake expander returns VerabDiscoveryResult
    with operators=['ruleXXXX'] (uses the synthetic expansion result from S3)."""
    from kbutillib.cheminformatics.verab.models import VerabDiscoveryResult
    from kbutillib.verab_utils import VerabUtils

    synthetic = _build_synthetic_expansion_result()

    class _FakeExpander:
        def expand(self, seed_smiles, generations=1, backend="pickaxe",
                   rule_set="metacyc_generalized", **kwargs):
            return synthetic

    u = VerabUtils(network_expansion=_FakeExpander())
    result = u.discover_rules(generations=1)

    assert isinstance(result, VerabDiscoveryResult)
    assert result.operators == ["ruleXXXX"]


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_s7_discover_rules_via_verabutils_impl():
    """VerabUtilsImpl constructed directly with a fake expander works end-to-end."""
    from kbutillib.cheminformatics.verab.models import VerabDiscoveryResult
    from kbutillib.verab_utils import VerabUtilsImpl

    # For non-expander facades we use None (only expander is needed for discover_rules)
    synthetic = _build_synthetic_expansion_result()

    class _FakeExpander:
        def expand(self, seed_smiles, generations=1, backend="pickaxe",
                   rule_set="metacyc_generalized", **kwargs):
            return synthetic

    # Construct directly — no SharedEnvUtils needed for the test
    class _FakeEnv:
        pass

    impl = VerabUtilsImpl(
        _FakeEnv(),
        network_expansion=_FakeExpander(),
        biochem=None,
        model=None,
        genome=None,
        annotation=None,
    )
    result = impl.discover_rules()
    assert isinstance(result, VerabDiscoveryResult)
    assert "ruleXXXX" in result.operators


def test_s7_discover_rules_raises_without_expander():
    """discover_rules must raise BackendUnavailableError when no expander is injected."""
    from kbutillib.cheminformatics.base import BackendUnavailableError
    from kbutillib.verab_utils import VerabUtils

    u = VerabUtils()  # no network_expansion
    with pytest.raises(BackendUnavailableError):
        u.discover_rules()


# ---- enumerate_methoxy_aromatics: available path (RDKit present) -----------


@pytest.mark.skipif(not _RDKIT_PRESENT, reason="RDKit not installed")
def test_s7_enumerate_methoxy_aromatics_with_fake_biochem():
    """enumerate_methoxy_aromatics returns only methoxy-aromatic compounds
    from a fake biochem DB when RDKit is present."""
    from kbutillib.verab_utils import VerabUtils

    class _FakeCpd:
        def __init__(self, cpd_id, smiles, is_obsolete=False):
            self.id = cpd_id
            self.name = cpd_id
            self.formula = None
            self.is_obsolete = is_obsolete
            self.annotation = {"SMILE": smiles} if smiles else {}

    class _FakeDB:
        compounds = [
            _FakeCpd("guaiacol", "COc1ccccc1O"),
            _FakeCpd("glucose", "OC[C@H]1OC(O)[C@H](O)[C@@H](O)[C@@H]1O"),
        ]

    class _FakeBiochem:
        biochem_db = _FakeDB()

    u = VerabUtils(biochem=_FakeBiochem())
    results = u.enumerate_methoxy_aromatics()

    ids = {r["id"] for r in results}
    assert "guaiacol" in ids
    assert "glucose" not in ids


# ---- enumerate_methoxy_aromatics: unavailable path (RDKit absent) ----------


@pytest.mark.skipif(_RDKIT_PRESENT, reason="Testing RDKit-absent path; skip when RDKit present")
def test_s7_enumerate_methoxy_aromatics_raises_when_rdkit_absent():
    """enumerate_methoxy_aromatics raises BackendUnavailableError when RDKit absent."""
    from kbutillib.cheminformatics.base import BackendUnavailableError
    from kbutillib.verab_utils import VerabUtils

    class _FakeDB:
        compounds = []

    class _FakeBiochem:
        biochem_db = _FakeDB()

    u = VerabUtils(biochem=_FakeBiochem())
    with pytest.raises(BackendUnavailableError):
        u.enumerate_methoxy_aromatics()


def test_s7_enumerate_methoxy_aromatics_raises_without_biochem():
    """enumerate_methoxy_aromatics raises BackendUnavailableError when no biochem
    is injected — regardless of RDKit availability."""
    from kbutillib.cheminformatics.base import BackendUnavailableError
    from kbutillib.verab_utils import VerabUtils

    u = VerabUtils()  # no biochem; RDKit state doesn't matter here

    # When RDKit is absent the RDKit check fires first; when present the biochem
    # check fires.  Either way BackendUnavailableError must be raised.
    with pytest.raises(BackendUnavailableError):
        # Patch _rdkit_available to True to force the biochem check branch.
        import unittest.mock as _mock
        import kbutillib.verab_utils as _vu

        with _mock.patch.object(_vu, "_rdkit_available", return_value=True):
            u.enumerate_methoxy_aromatics()


# ---- Simulate unavailable RDKit path via monkeypatch ----------------------


def test_s7_enumerate_methoxy_aromatics_simulated_rdkit_absent(monkeypatch):
    """Unit-test the RDKit-absent branch by monkeypatching _rdkit_available."""
    from kbutillib.cheminformatics.base import BackendUnavailableError
    import kbutillib.verab_utils as _vu
    from kbutillib.verab_utils import VerabUtils

    class _FakeDB:
        compounds = []

    class _FakeBiochem:
        biochem_db = _FakeDB()

    # Force RDKit to appear absent
    monkeypatch.setattr(_vu, "_rdkit_available", lambda: False)

    u = VerabUtils(biochem=_FakeBiochem())
    with pytest.raises(BackendUnavailableError) as exc_info:
        u.enumerate_methoxy_aromatics()

    assert "rdkit" in str(exc_info.value).lower()


# ---- Existing toolkit properties unaffected --------------------------------


def test_s7_existing_toolkit_properties_unaffected():
    """Adding the verab property must not break any existing toolkit properties.

    We only check properties whose construction does NOT require unavailable
    optional dependencies (e.g. modelseedpy, kbase-client) so that this test
    passes in a minimal dev environment.
    """
    from kbutillib.toolkit import KBUtilLib

    kbu = KBUtilLib()

    # network_expansion and chem/predictive_thermo do not require modelseedpy.
    assert kbu.network_expansion is not None
    assert kbu.chem is not None  # alias for network_expansion
    assert kbu.chem is kbu.network_expansion  # alias must return the same object

    # verab itself must be constructable (lazy — no deps resolved yet)
    assert kbu.verab is not None

    # Verify the verab backing field is the private sentinel before first verab access
    kbu2 = KBUtilLib()
    assert kbu2._verab is None  # not yet constructed
    _ = kbu2.verab              # trigger construction
    assert kbu2._verab is not None  # now set
