"""Offline unit tests for transyt_utils.py.

Test strategy
-------------
The Docker-dependent code paths (_run_docker, annotate when the image is
present) are exercised only with Docker mocked out.  The pure parse functions
(_parse_transyt_xml, _parse_species, _parse_reactions, _parse_scores_method1,
_parse_reactions_references, _reaction_equation, _build_annotation_records) are
tested fully offline against the committed real-structure fixtures under
``tests/fixtures/transyt/`` (SBML Level 3 Version 2 + fbc, as the current
TranSyT actually emits).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kbutillib.annotator_utils import (
    AnnotationResult,
    ToolUnavailableError,
)
from kbutillib.transyt_utils import (
    TransytUtils,
    _build_annotation_records,
    _parse_reactions,
    _parse_reactions_references,
    _parse_scores_method1,
    _parse_species,
    _parse_transyt_xml,
    _reaction_equation,
)

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "transyt"
_XML_FIXTURE = _FIXTURES / "transyt.xml"
_REF_FIXTURE = _FIXTURES / "reactions_references.txt"
_SCORES_FIXTURE = _FIXTURES / "scoresMethod1.txt"


def _make_utils(**kwargs) -> TransytUtils:
    """Create TransytUtils with no file discovery and a sentinel image tag."""
    return TransytUtils(
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# is_available()
# ---------------------------------------------------------------------------


class TestIsAvailable:
    """TransytUtils.is_available() contract."""

    def test_returns_false_when_docker_not_found(self):
        tu = _make_utils()
        with patch("subprocess.run", side_effect=FileNotFoundError("no docker")):
            assert tu.is_available() is False

    def test_returns_false_when_image_absent(self):
        tu = _make_utils()
        mock = MagicMock(returncode=1)
        with patch("subprocess.run", return_value=mock):
            assert tu.is_available() is False

    def test_returns_true_when_image_present(self):
        tu = _make_utils()
        mock = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock):
            assert tu.is_available() is True

    def test_returns_false_on_timeout(self):
        tu = _make_utils()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 10)):
            assert tu.is_available() is False

    def test_returns_false_when_image_tag_empty(self):
        tu = _make_utils()
        tu._docker_image = ""
        assert tu.is_available() is False

    def test_returns_false_on_os_error(self):
        tu = _make_utils()
        with patch("subprocess.run", side_effect=OSError("os error")):
            assert tu.is_available() is False


# ---------------------------------------------------------------------------
# annotate() — validation before Docker is touched
# ---------------------------------------------------------------------------


class TestAnnotateValidation:
    """Validation that happens before Docker is invoked."""

    def test_raises_on_empty_tax_id(self):
        tu = _make_utils()
        with pytest.raises(ValueError, match="tax_id is required"):
            tu.annotate(proteins={"p1": "MKTAY"}, tax_id="")

    def test_raises_on_missing_tax_id(self):
        tu = _make_utils()
        with pytest.raises(ValueError, match="tax_id is required"):
            tu.annotate(proteins={"p1": "MKTAY"})

    def test_raises_on_nucleotide_input(self):
        tu = _make_utils()
        nuc = "U" * 100
        with pytest.raises(ValueError, match="protein"):
            tu.annotate(proteins={"g1": nuc}, tax_id="562")

    def test_raises_tool_unavailable_when_image_absent(self):
        tu = _make_utils()
        with patch.object(tu, "is_available", return_value=False):
            with pytest.raises(ToolUnavailableError):
                tu.annotate(proteins={"p1": "MKTAY"}, tax_id="562")


# ---------------------------------------------------------------------------
# _parse_transyt_xml — gene → reactions (L3V2/fbc, refs nested in reactions)
# ---------------------------------------------------------------------------


class TestParseTransytXml:
    """Unit tests for _parse_transyt_xml using the golden fixture."""

    def test_fixture_exists(self):
        assert _XML_FIXTURE.exists(), f"Missing fixture: {_XML_FIXTURE}"

    def test_returns_dict(self):
        assert isinstance(_parse_transyt_xml(_XML_FIXTURE), dict)

    def test_prot1_has_two_reactions(self):
        result = _parse_transyt_xml(_XML_FIXTURE)
        assert set(result["prot1"]) == {"TR0001", "TO0003"}

    def test_prot2_has_one_reaction(self):
        assert _parse_transyt_xml(_XML_FIXTURE)["prot2"] == ["TZ0002"]

    def test_prot3_has_one_reaction(self):
        # TO0003 lists both prot1 and prot3 as gene products.
        assert _parse_transyt_xml(_XML_FIXTURE)["prot3"] == ["TO0003"]

    def test_uses_fbc_label_not_prefixed_id(self):
        # Gene ids are the fbc:label ("prot1"), never the fbc:id ("G_prot1").
        result = _parse_transyt_xml(_XML_FIXTURE)
        assert "G_prot1" not in result

    def test_returns_empty_for_invalid_xml(self, tmp_path):
        bad = tmp_path / "bad.xml"
        bad.write_text("not xml <<<<<", encoding="utf-8")
        assert _parse_transyt_xml(bad) == {}

    def test_dedup_same_reaction(self, tmp_path):
        xml = """<?xml version='1.0'?>
<sbml xmlns="http://www.sbml.org/sbml/level3/version2/core"
      xmlns:fbc="http://www.sbml.org/sbml/level3/version1/fbc/version2">
  <model id="m">
    <fbc:listOfGeneProducts>
      <fbc:geneProduct fbc:id="G_g1" fbc:label="g1"/>
    </fbc:listOfGeneProducts>
    <listOfReactions>
      <reaction id="RX1" reversible="false">
        <fbc:geneProductAssociation>
          <fbc:geneProductRef fbc:geneProduct="G_g1"/>
          <fbc:geneProductRef fbc:geneProduct="G_g1"/>
        </fbc:geneProductAssociation>
      </reaction>
    </listOfReactions>
  </model>
</sbml>"""
        f = tmp_path / "dup.xml"
        f.write_text(xml, encoding="utf-8")
        assert _parse_transyt_xml(f)["g1"].count("RX1") == 1

    def test_geneproduct_without_label_falls_back_to_id(self, tmp_path):
        xml = """<?xml version='1.0'?>
<sbml xmlns="http://www.sbml.org/sbml/level3/version2/core"
      xmlns:fbc="http://www.sbml.org/sbml/level3/version1/fbc/version2">
  <model id="m">
    <fbc:listOfGeneProducts>
      <fbc:geneProduct fbc:id="G_only"/>
    </fbc:listOfGeneProducts>
    <listOfReactions>
      <reaction id="RX1" reversible="false">
        <fbc:geneProductAssociation>
          <fbc:geneProductRef fbc:geneProduct="G_only"/>
        </fbc:geneProductAssociation>
      </reaction>
    </listOfReactions>
  </model>
</sbml>"""
        f = tmp_path / "nolabel.xml"
        f.write_text(xml, encoding="utf-8")
        # No label/name → falls back to the fbc:id "G_only".
        assert "G_only" in _parse_transyt_xml(f)


# ---------------------------------------------------------------------------
# _parse_species
# ---------------------------------------------------------------------------


class TestParseSpecies:
    def test_modelseed_species(self):
        spec = _parse_species(_XML_FIXTURE)
        info = spec["M_cpd00027_e0"]
        assert info["modelseed"] is True
        assert info["cpd"] == "cpd00027"
        assert info["compartment"] == "e0"
        assert info["name"] == "D-Glucose"

    def test_non_modelseed_species(self):
        info = _parse_species(_XML_FIXTURE)["M_mystery_c0"]
        assert info["modelseed"] is False
        assert info["cpd"] is None
        assert info["name"] == "Mystery compound"

    def test_invalid_xml_returns_empty(self, tmp_path):
        bad = tmp_path / "bad.xml"
        bad.write_text("<<<", encoding="utf-8")
        assert _parse_species(bad) == {}


# ---------------------------------------------------------------------------
# _parse_reactions
# ---------------------------------------------------------------------------


class TestParseReactions:
    def test_reversible_flag(self):
        rxns = _parse_reactions(_XML_FIXTURE)
        assert rxns["TR0001"]["reversible"] is True
        assert rxns["TZ0002"]["reversible"] is False

    def test_reactants_and_products(self):
        tr = _parse_reactions(_XML_FIXTURE)["TR0001"]
        assert ("M_cpd00027_e0", "1") in tr["reactants"]
        assert ("M_cpd00002_c0", "1") in tr["reactants"]
        assert ("M_cpd00027_c0", "1") in tr["products"]

    def test_stoichiometry_preserved(self):
        to = _parse_reactions(_XML_FIXTURE)["TO0003"]
        assert to["reactants"] == [("M_mystery_c0", "2")]

    def test_invalid_xml_returns_empty(self, tmp_path):
        bad = tmp_path / "bad.xml"
        bad.write_text("<<<", encoding="utf-8")
        assert _parse_reactions(bad) == {}


# ---------------------------------------------------------------------------
# _parse_scores_method1 — the only source of TC numbers
# ---------------------------------------------------------------------------


class TestParseScoresMethod1:
    def test_fixture_exists(self):
        assert _SCORES_FIXTURE.exists()

    def test_prot1_tc_families(self):
        scores = _parse_scores_method1(_SCORES_FIXTURE)
        assert scores["prot1"] == [("3.A.1.1.1", "0.0"), ("2.A.1.5.1", "1.5E-50")]

    def test_prot2_tc_family(self):
        assert _parse_scores_method1(_SCORES_FIXTURE)["prot2"] == [
            ("4.A.1.1.1", "2.0E-30")
        ]

    def test_missing_file_returns_empty(self, tmp_path):
        assert _parse_scores_method1(tmp_path / "nope.txt") == {}

    def test_blank_lines_ignored(self, tmp_path):
        f = tmp_path / "s.txt"
        f.write_text(">g\n\n1.A.1.1.1 - Evalue: 0.0\t[RX1]\n", encoding="utf-8")
        assert _parse_scores_method1(f)["g"] == [("1.A.1.1.1", "0.0")]


# ---------------------------------------------------------------------------
# _reaction_equation
# ---------------------------------------------------------------------------


class TestReactionEquation:
    def test_reversible_modelseed_equation(self):
        rxns = _parse_reactions(_XML_FIXTURE)
        spec = _parse_species(_XML_FIXTURE)
        eq = _reaction_equation(rxns["TR0001"], spec)
        assert eq == "cpd00027[e0] + cpd00002[c0] <=> cpd00027[c0] + cpd00008[c0]"

    def test_irreversible_arrow(self):
        rxns = _parse_reactions(_XML_FIXTURE)
        spec = _parse_species(_XML_FIXTURE)
        eq = _reaction_equation(rxns["TZ0002"], spec)
        assert eq == "cpd00208[e0] => cpd00208[c0]"

    def test_non_modelseed_compound_and_stoichiometry(self):
        rxns = _parse_reactions(_XML_FIXTURE)
        spec = _parse_species(_XML_FIXTURE)
        eq = _reaction_equation(rxns["TO0003"], spec)
        # M_mystery_c0 → name fallback "Mystery compound[c]"; stoich 2 shown.
        assert eq == "(2) Mystery compound[c] => cpd00027[c0]"


# ---------------------------------------------------------------------------
# _parse_reactions_references
# ---------------------------------------------------------------------------


class TestParseReactionsReferences:
    def test_fixture_exists(self):
        assert _REF_FIXTURE.exists()

    def test_tr0001_maps_to_modelseed_rxn(self):
        # Bracketed "[rxn05145]" is stripped to the bare id.
        msrxn, cpds = _parse_reactions_references(_REF_FIXTURE)["TR0001"]
        assert msrxn == "rxn05145"
        assert cpds == []

    def test_comments_skipped(self, tmp_path):
        f = tmp_path / "refs.txt"
        f.write_text("# comment\nR_T9999\trxn99999\n", encoding="utf-8")
        result = _parse_reactions_references(f)
        assert list(result) == ["R_T9999"]

    def test_brackets_stripped(self, tmp_path):
        f = tmp_path / "refs.txt"
        f.write_text("R_T1\t[rxn00001]\n", encoding="utf-8")
        assert _parse_reactions_references(f)["R_T1"][0] == "rxn00001"

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        assert _parse_reactions_references(f) == {}

    def test_missing_file(self, tmp_path):
        assert _parse_reactions_references(tmp_path / "nope.txt") == {}

    def test_optional_compound_column(self, tmp_path):
        f = tmp_path / "refs.txt"
        f.write_text("R_T1\trxn00001\tcpd00001;cpd00002\n", encoding="utf-8")
        msrxn, cpds = _parse_reactions_references(f)["R_T1"]
        assert msrxn == "rxn00001"
        assert cpds == ["cpd00001", "cpd00002"]


# ---------------------------------------------------------------------------
# _build_annotation_records — rich schema
# ---------------------------------------------------------------------------


class TestBuildAnnotationRecords:
    """Unit tests for _build_annotation_records with the rich schema."""

    def _run(self, input_ids: list[str]):
        return _build_annotation_records(
            _parse_transyt_xml(_XML_FIXTURE),
            _parse_scores_method1(_SCORES_FIXTURE),
            _parse_reactions(_XML_FIXTURE),
            _parse_species(_XML_FIXTURE),
            _parse_reactions_references(_REF_FIXTURE),
            input_ids,
        )

    def _terms(self, records, gene, namespace):
        rec = next(r for r in records if r.gene_id == gene)
        return [t for t in rec.terms if t.namespace == namespace]

    def test_prot1_tc_terms(self):
        records = self._run(["prot1", "prot2", "prot3"])
        tc = {t.id for t in self._terms(records, "prot1", "TC")}
        assert tc == {"3.A.1.1.1", "2.A.1.5.1"}

    def test_tc_term_carries_evalue(self):
        records = self._run(["prot1"])
        tc = self._terms(records, "prot1", "TC")[0]
        assert "evalue" in tc.evidence

    def test_mapped_reaction_is_msrxn_with_equation(self):
        records = self._run(["prot1"])
        msrxn = self._terms(records, "prot1", "MSRXN")
        assert len(msrxn) == 1
        assert msrxn[0].id == "rxn05145"
        assert "<=>" in msrxn[0].value  # the equation is the term value
        assert msrxn[0].evidence["transyt_rxn_id"] == "TR0001"

    def test_unmapped_reaction_is_transyt_rxn_with_equation(self):
        records = self._run(["prot1"])
        tr = self._terms(records, "prot1", "TRANSYT_RXN")
        # TO0003 is not in reactions_references → kept as a TRANSYT_RXN.
        assert any(t.id == "TO0003" for t in tr)
        assert all("=>" in t.value or "<=>" in t.value for t in tr)

    def test_modelseed_compound_terms(self):
        records = self._run(["prot1"])
        cpds = {t.id for t in self._terms(records, "prot1", "MSCPD")}
        assert {"cpd00027", "cpd00002", "cpd00008"}.issubset(cpds)

    def test_non_modelseed_compound_uses_cpd_namespace(self):
        records = self._run(["prot1"])
        cpd = self._terms(records, "prot1", "CPD")
        assert any(t.id == "Mystery compound" for t in cpd)

    def test_only_requested_genes_included(self):
        records = self._run(["prot2"])
        assert {r.gene_id for r in records} == {"prot2"}

    def test_empty_input_returns_empty(self):
        assert self._run([]) == []

    def test_gene_with_only_tc_still_emitted(self):
        # A gene with a TC prediction but no reactions still yields a record.
        records = _build_annotation_records(
            {}, {"g1": [("9.A.1.1.1", "1e-9")]}, {}, {}, {}, ["g1"]
        )
        assert len(records) == 1
        assert records[0].terms[0].namespace == "TC"

    def test_gene_absent_when_no_terms(self):
        records = _build_annotation_records(
            {"g1": ["RXmissing"]}, {}, {}, {}, {}, ["g1"]
        )
        assert records == []

    def test_namespaces_are_expected_set(self):
        records = self._run(["prot1", "prot2", "prot3"])
        ns = {t.namespace for r in records for t in r.terms}
        assert ns <= {"TC", "MSRXN", "TRANSYT_RXN", "MSCPD", "CPD"}

    def test_dedup_tc(self):
        records = _build_annotation_records(
            {}, {"g1": [("1.A.1.1.1", "0"), ("1.A.1.1.1", "0")]}, {}, {}, {}, ["g1"]
        )
        assert len(self._terms(records, "g1", "TC")) == 1


# ---------------------------------------------------------------------------
# _stage_inputs — input file staging (params.txt is TAB-delimited)
# ---------------------------------------------------------------------------


class TestStageInputs:
    def test_protein_faa_written(self, tmp_path):
        tu = _make_utils()
        indir = tmp_path / "proc"
        indir.mkdir()
        tu._stage_inputs(indir, {"gene1": "MKTAY", "gene2": "MNFST"}, "562", "ModelSEED", None)
        faa = (indir / "protein.faa").read_text()
        assert ">gene1" in faa and "MKTAY" in faa and ">gene2" in faa

    def test_params_txt_tax_id_is_tab_delimited(self, tmp_path):
        tu = _make_utils()
        indir = tmp_path / "proc"
        indir.mkdir()
        tu._stage_inputs(indir, {"p1": "MKTAY"}, "562", "ModelSEED", None)
        params = (indir / "params.txt").read_text()
        assert "taxID\t562" in params
        assert "taxID=562" not in params  # NOT the '=' form (TranSyT can't parse it)

    def test_params_txt_reference_database_is_tab_delimited(self, tmp_path):
        tu = _make_utils()
        indir = tmp_path / "proc"
        indir.mkdir()
        tu._stage_inputs(indir, {"p1": "MKTAY"}, "562", "ModelSEED", None)
        assert "reference_database\tModelSEED" in (indir / "params.txt").read_text()

    def test_metabolites_txt_written_when_provided(self, tmp_path):
        tu = _make_utils()
        indir = tmp_path / "proc"
        indir.mkdir()
        tu._stage_inputs(indir, {"p1": "MKTAY"}, "562", "ModelSEED", ["cpd00001", "cpd00002"])
        met = (indir / "metabolites.txt").read_text()
        assert "cpd00001" in met and "cpd00002" in met

    def test_no_metabolites_txt_when_none(self, tmp_path):
        tu = _make_utils()
        indir = tmp_path / "proc"
        indir.mkdir()
        tu._stage_inputs(indir, {"p1": "MKTAY"}, "562", "ModelSEED", None)
        assert not (indir / "metabolites.txt").exists()


# ---------------------------------------------------------------------------
# _build_docker_command — command shape + memory config
# ---------------------------------------------------------------------------


class TestBuildDockerCommand:
    def _cmd(self, **attrs):
        tu = _make_utils()
        tu._docker_image = "test_image:v1"
        for k, v in attrs.items():
            setattr(tu, k, v)
        return tu._build_docker_command(Path("/tmp/fakedir"))

    def test_starts_with_docker_run(self):
        assert self._cmd()[:2] == ["docker", "run"]

    def test_contains_image(self):
        assert "test_image:v1" in self._cmd()

    def test_entrypoint_bash(self):
        cmd = self._cmd()
        assert cmd[cmd.index("--entrypoint") + 1] == "bash"

    def test_bind_mount(self):
        cmd = self._cmd()
        spec = cmd[cmd.index("-v") + 1]
        assert "/tmp/fakedir" in spec and "/workdir/processingDir" in spec

    def test_neo4j_readiness_uses_loop(self):
        assert "while" in self._cmd()[-1]

    def test_inner_has_neo4j_and_jar(self):
        inner = self._cmd()[-1]
        assert "neo4j start" in inner and "transyt.jar" in inner

    def test_neo4j_timeout_in_poll(self):
        assert "90" in self._cmd(_neo4j_timeout=90)[-1]

    def test_jvm_xmx_configurable(self):
        assert "-Xmx1500m" in self._cmd(_jvm_xmx="1500m")[-1]

    def test_neo4j_heap_configurable(self):
        inner = self._cmd(_neo4j_heap="512m", _neo4j_pagecache="256m")[-1]
        assert "NEO4J_dbms_memory_heap_max__size=512m" in inner
        assert "NEO4J_dbms_memory_pagecache_size=256m" in inner


# ---------------------------------------------------------------------------
# annotate() — Docker mocked out
# ---------------------------------------------------------------------------


class TestAnnotateMocked:
    """Tests for annotate() with Docker mocked out."""

    def _fake_run_docker(self, indir):
        import shutil
        dest = indir / "results"
        dest.mkdir(exist_ok=True)
        shutil.copy(_XML_FIXTURE, dest / "transyt.xml")
        shutil.copy(_REF_FIXTURE, dest / "reactions_references.txt")
        shutil.copy(_SCORES_FIXTURE, dest / "scoresMethod1.txt")
        return ("docker run --rm -v x:/workdir/processingDir ...", 0)

    def _annotate(self, tu, **kwargs):
        with patch.object(tu, "is_available", return_value=True):
            with patch.object(tu, "_run_docker", side_effect=self._fake_run_docker):
                with patch.object(tu, "_get_image_digest", return_value="sha256:abc"):
                    return tu.annotate(**kwargs)

    def test_returns_annotation_result(self):
        tu = _make_utils()
        tu._docker_image = "mock:latest"
        result = self._annotate(
            tu, proteins={"prot1": "MKTAY", "prot2": "MNFST"}, tax_id="562"
        )
        assert isinstance(result, AnnotationResult)
        assert result.tool == "transyt"
        assert result.parameters["tax_id"] == "562"
        assert result.parameters["reference_database"] == "ModelSEED"

    def test_records_have_tc_and_reaction_terms(self):
        tu = _make_utils()
        tu._docker_image = "mock:latest"
        result = self._annotate(
            tu, proteins={"prot1": "MKTAY", "prot2": "MNFST", "prot3": "MAAA"},
            tax_id="562",
        )
        prot1 = next(r for r in result.records if r.gene_id == "prot1")
        namespaces = {t.namespace for t in prot1.terms}
        assert "TC" in namespaces
        assert "MSRXN" in namespaces or "TRANSYT_RXN" in namespaces

    def test_records_keyed_to_caller_ids(self):
        tu = _make_utils()
        tu._docker_image = "mock:latest"
        proteins = {"prot1": "MKTAY", "prot2": "MNFST", "prot3": "MAAA"}
        result = self._annotate(tu, proteins=proteins, tax_id="562")
        assert {r.gene_id for r in result.records}.issubset(set(proteins))

    def test_exit8_returns_empty_records(self):
        tu = _make_utils()
        tu._docker_image = "mock:latest"
        with patch.object(tu, "is_available", return_value=True):
            with patch.object(tu, "_run_docker", side_effect=lambda d: ("cmd", 8)):
                with patch.object(tu, "_get_image_digest", return_value=""):
                    result = tu.annotate(proteins={"p1": "MKTAY"}, tax_id="562")
        assert result.records == []
        assert result.tool == "transyt"

    def test_metabolites_parameter_stored(self):
        tu = _make_utils()
        tu._docker_image = "mock:latest"
        with patch.object(tu, "is_available", return_value=True):
            with patch.object(tu, "_run_docker", side_effect=lambda d: ("cmd", 8)):
                with patch.object(tu, "_get_image_digest", return_value=""):
                    result = tu.annotate(
                        proteins={"p1": "MKTAY"}, tax_id="562",
                        metabolites=["cpd00027"],
                    )
        assert result.parameters["metabolites"] == ["cpd00027"]
