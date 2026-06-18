"""Offline unit tests for transyt_utils.py.

Test strategy
-------------
The Docker-dependent code paths (_run_docker, annotate when image present)
are exercised only in the ``@pytest.mark.integration`` tests which are
skipped unless the configured Docker image is locally present.

The pure parse functions (_parse_transyt_xml, _parse_reaction_tc,
_parse_reactions_references, _build_annotation_records) are tested
fully offline using the committed golden fixtures under
``tests/fixtures/transyt/``.

Coverage gate
-------------
The ``_run_docker`` and live ``annotate`` paths are excluded from the
offline coverage requirement via the ``@pytest.mark.integration`` skip
mechanism (the lines are only reached when the image is present).  All
other lines — including is_available() returning False, the ValueError
raises, and every parse function — are covered by the offline tests.
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
    _parse_reaction_tc,
    _parse_reactions_references,
    _parse_transyt_xml,
)

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "transyt"
_XML_FIXTURE = _FIXTURES / "transyt.xml"
_REF_FIXTURE = _FIXTURES / "reactions_references.txt"


# ---------------------------------------------------------------------------
# Test doubles / helpers
# ---------------------------------------------------------------------------


def _make_utils(**kwargs) -> TransytUtils:
    """Create TransytUtils with no file discovery and a sentinel image tag."""
    return TransytUtils(
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# is_available() — returns False without raising when image absent
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
        # Nucleotide sequence containing U (not in protein alphabet) — guard rejects.
        # Need more than 10% U in a sequence to trigger the threshold.
        nuc = "U" * 100  # 100% U, clearly not protein
        with pytest.raises(ValueError, match="protein"):
            tu.annotate(proteins={"g1": nuc}, tax_id="562")

    def test_raises_tool_unavailable_when_image_absent(self):
        tu = _make_utils()
        with patch.object(tu, "is_available", return_value=False):
            with pytest.raises(ToolUnavailableError):
                tu.annotate(proteins={"p1": "MKTAY"}, tax_id="562")


# ---------------------------------------------------------------------------
# _parse_transyt_xml — golden fixture
# ---------------------------------------------------------------------------


class TestParseTransytXml:
    """Unit tests for _parse_transyt_xml using the golden fixture."""

    def test_fixture_exists(self):
        assert _XML_FIXTURE.exists(), f"Missing fixture: {_XML_FIXTURE}"

    def test_returns_dict(self):
        result = _parse_transyt_xml(_XML_FIXTURE)
        assert isinstance(result, dict)

    def test_gene_prot1_has_two_reactions(self):
        result = _parse_transyt_xml(_XML_FIXTURE)
        assert "prot1" in result
        rxns = result["prot1"]
        assert len(rxns) == 2
        assert "R_T0001" in rxns
        assert "R_T0003" in rxns

    def test_gene_prot2_has_one_reaction(self):
        result = _parse_transyt_xml(_XML_FIXTURE)
        assert "prot2" in result
        assert result["prot2"] == ["R_T0002"]

    def test_gene_prot3_has_one_reaction(self):
        result = _parse_transyt_xml(_XML_FIXTURE)
        assert "prot3" in result
        assert result["prot3"] == ["R_T0003"]

    def test_returns_empty_for_invalid_xml(self, tmp_path):
        bad = tmp_path / "bad.xml"
        bad.write_text("not xml <<<<<", encoding="utf-8")
        assert _parse_transyt_xml(bad) == {}


# ---------------------------------------------------------------------------
# _parse_reaction_tc — golden fixture
# ---------------------------------------------------------------------------


class TestParseReactionTc:
    """Unit tests for _parse_reaction_tc using the golden fixture."""

    def test_returns_dict(self):
        result = _parse_reaction_tc(_XML_FIXTURE)
        assert isinstance(result, dict)

    def test_r_t0001_has_tc(self):
        result = _parse_reaction_tc(_XML_FIXTURE)
        assert "R_T0001" in result
        assert result["R_T0001"] == "3.A.1.1.1"

    def test_r_t0002_has_tc(self):
        result = _parse_reaction_tc(_XML_FIXTURE)
        assert "R_T0002" in result
        assert result["R_T0002"] == "4.A.1.1.1"

    def test_r_t0003_has_tc(self):
        result = _parse_reaction_tc(_XML_FIXTURE)
        assert "R_T0003" in result
        assert result["R_T0003"] == "2.A.1.5.1"

    def test_returns_empty_for_invalid_xml(self, tmp_path):
        bad = tmp_path / "bad.xml"
        bad.write_text("not xml <<<<<", encoding="utf-8")
        assert _parse_reaction_tc(bad) == {}


# ---------------------------------------------------------------------------
# _parse_reactions_references — golden fixture
# ---------------------------------------------------------------------------


class TestParseReactionsReferences:
    """Unit tests for _parse_reactions_references using the golden fixture."""

    def test_fixture_exists(self):
        assert _REF_FIXTURE.exists(), f"Missing fixture: {_REF_FIXTURE}"

    def test_returns_dict(self):
        result = _parse_reactions_references(_REF_FIXTURE)
        assert isinstance(result, dict)

    def test_r_t0001_mapping(self):
        result = _parse_reactions_references(_REF_FIXTURE)
        assert "R_T0001" in result
        msrxn, cpds = result["R_T0001"]
        assert msrxn == "rxn05145"
        assert "cpd00027" in cpds
        assert "cpd00002" in cpds

    def test_r_t0002_mapping(self):
        result = _parse_reactions_references(_REF_FIXTURE)
        msrxn, cpds = result["R_T0002"]
        assert msrxn == "rxn00549"
        assert "cpd00027" in cpds

    def test_r_t0003_mapping(self):
        result = _parse_reactions_references(_REF_FIXTURE)
        msrxn, cpds = result["R_T0003"]
        assert msrxn == "rxn05116"
        assert "cpd00208" in cpds

    def test_comments_skipped(self, tmp_path):
        f = tmp_path / "refs.txt"
        f.write_text("# comment\nR_T9999\trxn99999\tcpd11111\n", encoding="utf-8")
        result = _parse_reactions_references(f)
        assert "R_T9999" in result
        assert len(result) == 1

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        assert _parse_reactions_references(f) == {}

    def test_missing_file(self, tmp_path):
        result = _parse_reactions_references(tmp_path / "nonexistent.txt")
        assert result == {}

    def test_missing_compound_column(self, tmp_path):
        f = tmp_path / "refs.txt"
        f.write_text("R_T1\trxn00001\n", encoding="utf-8")
        result = _parse_reactions_references(f)
        msrxn, cpds = result["R_T1"]
        assert msrxn == "rxn00001"
        assert cpds == []

    def test_empty_compound_field(self, tmp_path):
        f = tmp_path / "refs.txt"
        f.write_text("R_T1\trxn00001\t\n", encoding="utf-8")
        result = _parse_reactions_references(f)
        msrxn, cpds = result["R_T1"]
        assert cpds == []


# ---------------------------------------------------------------------------
# _build_annotation_records — logic tests
# ---------------------------------------------------------------------------


class TestBuildAnnotationRecords:
    """Unit tests for _build_annotation_records."""

    def _run_with_fixtures(self, input_ids: list[str]):
        gene_to_rxns = _parse_transyt_xml(_XML_FIXTURE)
        rxn_to_tc = _parse_reaction_tc(_XML_FIXTURE)
        rxn_to_msrxn_cpds = _parse_reactions_references(_REF_FIXTURE)
        return _build_annotation_records(
            gene_to_rxns, rxn_to_tc, rxn_to_msrxn_cpds, input_ids
        )

    def test_prot1_has_tc_terms(self):
        records = self._run_with_fixtures(["prot1", "prot2", "prot3"])
        prot1 = next(r for r in records if r.gene_id == "prot1")
        tc_terms = [t for t in prot1.terms if t.namespace == "TC"]
        assert len(tc_terms) >= 1
        tc_vals = {t.id for t in tc_terms}
        assert "3.A.1.1.1" in tc_vals or "2.A.1.5.1" in tc_vals

    def test_prot1_has_msrxn_terms(self):
        records = self._run_with_fixtures(["prot1", "prot2", "prot3"])
        prot1 = next(r for r in records if r.gene_id == "prot1")
        msrxn_terms = [t for t in prot1.terms if t.namespace == "MSRXN"]
        assert len(msrxn_terms) >= 1

    def test_prot1_has_mscpd_terms(self):
        records = self._run_with_fixtures(["prot1", "prot2", "prot3"])
        prot1 = next(r for r in records if r.gene_id == "prot1")
        mscpd_terms = [t for t in prot1.terms if t.namespace == "MSCPD"]
        assert len(mscpd_terms) >= 1

    def test_terms_keyed_to_caller_ids(self):
        records = self._run_with_fixtures(["prot1", "prot2", "prot3"])
        ids = {r.gene_id for r in records}
        assert ids.issubset({"prot1", "prot2", "prot3"})

    def test_genes_not_in_input_excluded(self):
        # Only ask for prot2; prot1 and prot3 should be excluded
        records = self._run_with_fixtures(["prot2"])
        assert all(r.gene_id == "prot2" for r in records)

    def test_empty_input_returns_empty(self):
        records = self._run_with_fixtures([])
        assert records == []

    def test_gene_with_no_tc_or_mapping_absent(self):
        # Gene with reactions that have no TC and no ref mapping
        gene_to_rxns = {"ghost_gene": ["R_T9999"]}
        rxn_to_tc: dict = {}
        rxn_to_msrxn_cpds: dict = {}
        records = _build_annotation_records(
            gene_to_rxns, rxn_to_tc, rxn_to_msrxn_cpds, ["ghost_gene"]
        )
        # No terms → no record
        assert records == []

    def test_dedup_tc_same_gene_same_tc(self):
        # Two reactions with the same TC — should emit TC Term only once
        gene_to_rxns = {"g1": ["R_A", "R_B"]}
        rxn_to_tc = {"R_A": "3.A.1.1.1", "R_B": "3.A.1.1.1"}
        rxn_to_msrxn_cpds: dict = {}
        records = _build_annotation_records(
            gene_to_rxns, rxn_to_tc, rxn_to_msrxn_cpds, ["g1"]
        )
        tc_terms = [t for t in records[0].terms if t.namespace == "TC"]
        assert len(tc_terms) == 1

    def test_term_namespaces_correct(self):
        records = self._run_with_fixtures(["prot1", "prot2", "prot3"])
        for rec in records:
            for term in rec.terms:
                assert term.namespace in {"TC", "MSRXN", "MSCPD"}

    def test_term_evidence_contains_transyt_rxn_id(self):
        records = self._run_with_fixtures(["prot1"])
        for rec in records:
            for term in rec.terms:
                assert "transyt_rxn_id" in term.evidence

    def test_msrxn_none_mapping_emits_only_cpds(self):
        """When msrxn is None in a mapping, only MSCPD terms are emitted."""
        gene_to_rxns = {"g1": ["R_A"]}
        rxn_to_tc: dict[str, str] = {}
        rxn_to_msrxn_cpds = {"R_A": (None, ["cpd00001"])}
        records = _build_annotation_records(
            gene_to_rxns, rxn_to_tc, rxn_to_msrxn_cpds, ["g1"]
        )
        assert len(records) == 1
        assert all(t.namespace == "MSCPD" for t in records[0].terms)

    def test_dedup_cpd_same_gene_two_reactions(self):
        """Same cpd from two reactions appears only once in terms."""
        gene_to_rxns = {"g1": ["R_A", "R_B"]}
        rxn_to_tc: dict[str, str] = {}
        rxn_to_msrxn_cpds = {
            "R_A": ("rxn00001", ["cpd00001"]),
            "R_B": ("rxn00002", ["cpd00001"]),  # same cpd
        }
        records = _build_annotation_records(
            gene_to_rxns, rxn_to_tc, rxn_to_msrxn_cpds, ["g1"]
        )
        cpd_terms = [t for t in records[0].terms if t.namespace == "MSCPD"]
        assert len(cpd_terms) == 1

    def test_dedup_msrxn_same_gene_two_reactions(self):
        """Same msrxn from two reactions appears only once."""
        gene_to_rxns = {"g1": ["R_A", "R_B"]}
        rxn_to_tc: dict[str, str] = {}
        rxn_to_msrxn_cpds = {
            "R_A": ("rxn00001", []),
            "R_B": ("rxn00001", []),  # same rxn
        }
        records = _build_annotation_records(
            gene_to_rxns, rxn_to_tc, rxn_to_msrxn_cpds, ["g1"]
        )
        msrxn_terms = [t for t in records[0].terms if t.namespace == "MSRXN"]
        assert len(msrxn_terms) == 1


# ---------------------------------------------------------------------------
# TransytUtils._stage_inputs — input file staging
# ---------------------------------------------------------------------------


class TestStageInputs:
    """Unit tests for the input staging logic."""

    def test_protein_faa_written(self, tmp_path):
        tu = _make_utils()
        indir = tmp_path / "proc"
        indir.mkdir()
        tu._stage_inputs(indir, {"gene1": "MKTAY", "gene2": "MNFST"}, "562", "ModelSEED", None)
        faa = (indir / "protein.faa").read_text()
        assert ">gene1" in faa
        assert "MKTAY" in faa
        assert ">gene2" in faa

    def test_params_txt_contains_tax_id(self, tmp_path):
        tu = _make_utils()
        indir = tmp_path / "proc"
        indir.mkdir()
        tu._stage_inputs(indir, {"p1": "MKTAY"}, "562", "ModelSEED", None)
        params = (indir / "params.txt").read_text()
        assert "taxID=562" in params

    def test_params_txt_contains_reference_database(self, tmp_path):
        tu = _make_utils()
        indir = tmp_path / "proc"
        indir.mkdir()
        tu._stage_inputs(indir, {"p1": "MKTAY"}, "562", "ModelSEED", None)
        params = (indir / "params.txt").read_text()
        assert "reference_database=ModelSEED" in params

    def test_metabolites_txt_written_when_provided(self, tmp_path):
        tu = _make_utils()
        indir = tmp_path / "proc"
        indir.mkdir()
        tu._stage_inputs(indir, {"p1": "MKTAY"}, "562", "ModelSEED", ["cpd00001", "cpd00002"])
        met = (indir / "metabolites.txt").read_text()
        assert "cpd00001" in met
        assert "cpd00002" in met

    def test_no_metabolites_txt_when_none(self, tmp_path):
        tu = _make_utils()
        indir = tmp_path / "proc"
        indir.mkdir()
        tu._stage_inputs(indir, {"p1": "MKTAY"}, "562", "ModelSEED", None)
        assert not (indir / "metabolites.txt").exists()


# ---------------------------------------------------------------------------
# _build_docker_command — command shape
# ---------------------------------------------------------------------------


class TestBuildDockerCommand:
    """Unit tests for the Docker command builder."""

    def test_returns_list(self):
        tu = _make_utils()
        tu._docker_image = "test_image:v1"
        indir = Path("/tmp/fakedir")
        cmd = tu._build_docker_command(indir)
        assert isinstance(cmd, list)

    def test_starts_with_docker_run(self):
        tu = _make_utils()
        tu._docker_image = "test_image:v1"
        indir = Path("/tmp/fakedir")
        cmd = tu._build_docker_command(indir)
        assert cmd[:2] == ["docker", "run"]

    def test_contains_image(self):
        tu = _make_utils()
        tu._docker_image = "my_image:tag"
        indir = Path("/tmp/fakedir")
        cmd = tu._build_docker_command(indir)
        assert "my_image:tag" in cmd

    def test_contains_entrypoint_bash(self):
        tu = _make_utils()
        tu._docker_image = "img:latest"
        indir = Path("/tmp/fakedir")
        cmd = tu._build_docker_command(indir)
        assert "--entrypoint" in cmd
        ep_idx = cmd.index("--entrypoint")
        assert cmd[ep_idx + 1] == "bash"

    def test_contains_bind_mount(self):
        tu = _make_utils()
        tu._docker_image = "img:latest"
        indir = Path("/tmp/fakedir")
        cmd = tu._build_docker_command(indir)
        v_idx = cmd.index("-v")
        mount_spec = cmd[v_idx + 1]
        assert str(indir) in mount_spec
        assert "/workdir/processingDir" in mount_spec

    def test_inner_script_has_no_fixed_sleep(self):
        tu = _make_utils()
        tu._docker_image = "img:latest"
        indir = Path("/tmp/fakedir")
        cmd = tu._build_docker_command(indir)
        # The last element is the inner bash -lc script
        inner = cmd[-1]
        # Must not use a bare fixed sleep (sleep N without a loop) for Neo4j
        # The poll loop uses `sleep 1` inside a while loop — that's fine.
        # Ensure there is a loop construct (while) rather than just `sleep <big_number>`.
        assert "while" in inner, "Neo4j readiness must use a loop, not a fixed sleep"

    def test_inner_script_has_neo4j_start(self):
        tu = _make_utils()
        tu._docker_image = "img:latest"
        indir = Path("/tmp/fakedir")
        cmd = tu._build_docker_command(indir)
        inner = cmd[-1]
        assert "neo4j start" in inner

    def test_inner_script_has_jar_invocation(self):
        tu = _make_utils()
        tu._docker_image = "img:latest"
        indir = Path("/tmp/fakedir")
        cmd = tu._build_docker_command(indir)
        inner = cmd[-1]
        assert "transyt.jar" in inner

    def test_neo4j_timeout_in_poll(self):
        tu = _make_utils()
        tu._docker_image = "img:latest"
        tu._neo4j_timeout = 90
        indir = Path("/tmp/fakedir")
        cmd = tu._build_docker_command(indir)
        inner = cmd[-1]
        assert "90" in inner


# ---------------------------------------------------------------------------
# annotate() — mocked Docker path (happy path + exit-8 empty result)
# ---------------------------------------------------------------------------


class TestAnnotateMocked:
    """Tests for annotate() with Docker mocked out."""

    def _make_with_available(self) -> TransytUtils:
        tu = _make_utils()
        tu._docker_image = "mock_image:latest"
        return tu

    def test_annotate_returns_annotation_result(self, tmp_path):
        tu = self._make_with_available()
        # Patch is_available to True; _run_docker to exit 0 with fixture results
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        import shutil

        shutil.copy(_XML_FIXTURE, results_dir / "transyt.xml")
        shutil.copy(_REF_FIXTURE, results_dir / "reactions_references.txt")

        def _fake_run_docker(indir):
            # Copy results into indir/results
            dest = indir / "results"
            dest.mkdir(exist_ok=True)
            shutil.copy(_XML_FIXTURE, dest / "transyt.xml")
            shutil.copy(_REF_FIXTURE, dest / "reactions_references.txt")
            return ("docker run ...", 0)

        with patch.object(tu, "is_available", return_value=True):
            with patch.object(tu, "_run_docker", side_effect=_fake_run_docker):
                with patch.object(tu, "_get_image_digest", return_value="sha256:abc"):
                    result = tu.annotate(
                        proteins={"prot1": "MKTAYIAKQ", "prot2": "MNFSTPDQ"},
                        tax_id="562",
                    )

        assert isinstance(result, AnnotationResult)
        assert result.tool == "transyt"
        assert result.run_id != ""
        assert result.parameters["tax_id"] == "562"
        assert result.parameters["reference_database"] == "ModelSEED"

    def test_annotate_exit8_returns_empty_records(self):
        tu = self._make_with_available()

        def _fake_run_docker(indir):
            return ("docker run ...", 8)

        with patch.object(tu, "is_available", return_value=True):
            with patch.object(tu, "_run_docker", side_effect=_fake_run_docker):
                with patch.object(tu, "_get_image_digest", return_value=""):
                    result = tu.annotate(
                        proteins={"p1": "MKTAY"},
                        tax_id="562",
                    )

        assert result.records == []
        assert result.tool == "transyt"

    def test_annotate_stores_command_string(self, tmp_path):
        tu = self._make_with_available()
        import shutil

        def _fake_run_docker(indir):
            dest = indir / "results"
            dest.mkdir(exist_ok=True)
            shutil.copy(_XML_FIXTURE, dest / "transyt.xml")
            shutil.copy(_REF_FIXTURE, dest / "reactions_references.txt")
            return ("docker run --rm -v /tmp/x:/workdir/processingDir ...", 0)

        with patch.object(tu, "is_available", return_value=True):
            with patch.object(tu, "_run_docker", side_effect=_fake_run_docker):
                with patch.object(tu, "_get_image_digest", return_value=""):
                    result = tu.annotate(
                        proteins={"prot1": "MKTAY"},
                        tax_id="562",
                    )

        assert "docker run" in result.command

    def test_annotate_records_keyed_to_caller_ids(self):
        tu = self._make_with_available()
        import shutil

        def _fake_run_docker(indir):
            dest = indir / "results"
            dest.mkdir(exist_ok=True)
            shutil.copy(_XML_FIXTURE, dest / "transyt.xml")
            shutil.copy(_REF_FIXTURE, dest / "reactions_references.txt")
            return ("docker run ...", 0)

        proteins = {"prot1": "MKTAY", "prot2": "MNFST", "prot3": "MAABCD"}
        with patch.object(tu, "is_available", return_value=True):
            with patch.object(tu, "_run_docker", side_effect=_fake_run_docker):
                with patch.object(tu, "_get_image_digest", return_value=""):
                    result = tu.annotate(proteins=proteins, tax_id="562")

        record_ids = {r.gene_id for r in result.records}
        assert record_ids.issubset(set(proteins.keys()))

    def test_annotate_metabolites_parameter_stored(self):
        tu = self._make_with_available()

        def _fake_run_docker(indir):
            return ("docker run ...", 8)

        with patch.object(tu, "is_available", return_value=True):
            with patch.object(tu, "_run_docker", side_effect=_fake_run_docker):
                with patch.object(tu, "_get_image_digest", return_value=""):
                    result = tu.annotate(
                        proteins={"p1": "MKTAY"},
                        tax_id="562",
                        metabolites=["cpd00027"],
                    )

        assert result.parameters["metabolites"] == ["cpd00027"]


# ---------------------------------------------------------------------------
# _parse_transyt_xml — edge cases for coverage
# ---------------------------------------------------------------------------


class TestParseTransytXmlEdgeCases:
    """Edge-case coverage for _parse_transyt_xml branches."""

    def test_xml_without_sbml_namespace(self, tmp_path):
        """XML with plain element names (no SBML namespace) is parsed."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<sbml>
  <model id="m">
    <listOfGeneProducts>
      <geneProduct id="gp1" name="mygene"/>
    </listOfGeneProducts>
    <listOfGeneProductAssociations>
      <geneProductAssociation reaction="R_T1">
        <geneProductRef geneProduct="gp1"/>
      </geneProductAssociation>
    </listOfGeneProductAssociations>
  </model>
</sbml>"""
        f = tmp_path / "plain.xml"
        f.write_text(xml_content, encoding="utf-8")
        result = _parse_transyt_xml(f)
        assert "mygene" in result or "gp1" in result  # fallback name or id

    def test_association_with_empty_reaction_id(self, tmp_path):
        """Associations with empty reaction attribute are skipped."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<sbml xmlns="http://www.sbml.org/sbml/level2/version4">
  <model id="m">
    <listOfGeneProducts>
      <geneProduct id="gp1" name="g1"/>
    </listOfGeneProducts>
    <listOfGeneProductAssociations>
      <geneProductAssociation reaction="">
        <geneProductRef geneProduct="gp1"/>
      </geneProductAssociation>
    </listOfGeneProductAssociations>
  </model>
</sbml>"""
        f = tmp_path / "empty_rxn.xml"
        f.write_text(xml_content, encoding="utf-8")
        result = _parse_transyt_xml(f)
        # Empty reaction id is skipped; gene absent
        assert result == {}

    def test_no_model_element(self, tmp_path):
        """SBML with no model element returns empty dict."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<sbml xmlns="http://www.sbml.org/sbml/level2/version4">
</sbml>"""
        f = tmp_path / "nomodel.xml"
        f.write_text(xml_content, encoding="utf-8")
        result = _parse_transyt_xml(f)
        assert result == {}

    def test_rxn_id_dedup_same_reaction_twice(self, tmp_path):
        """The same reaction id is not added twice for the same gene."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<sbml xmlns="http://www.sbml.org/sbml/level2/version4">
  <model id="m">
    <listOfGeneProducts>
      <geneProduct id="gp1" name="g1"/>
    </listOfGeneProducts>
    <listOfGeneProductAssociations>
      <geneProductAssociation reaction="R_T1">
        <geneProductRef geneProduct="gp1"/>
      </geneProductAssociation>
      <geneProductAssociation reaction="R_T1">
        <geneProductRef geneProduct="gp1"/>
      </geneProductAssociation>
    </listOfGeneProductAssociations>
  </model>
</sbml>"""
        f = tmp_path / "dup.xml"
        f.write_text(xml_content, encoding="utf-8")
        result = _parse_transyt_xml(f)
        assert result.get("g1", []).count("R_T1") == 1

    def test_gene_product_without_id_attr_skipped(self, tmp_path):
        """geneProduct elements with missing id attribute are skipped in id map."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<sbml xmlns="http://www.sbml.org/sbml/level2/version4">
  <model id="m">
    <listOfGeneProducts>
      <geneProduct name="orphan_gene"/>
    </listOfGeneProducts>
    <listOfGeneProductAssociations>
    </listOfGeneProductAssociations>
  </model>
</sbml>"""
        f = tmp_path / "noid.xml"
        f.write_text(xml_content, encoding="utf-8")
        result = _parse_transyt_xml(f)
        # No id → not in id_to_name, no refs → empty
        assert result == {}

    def test_model_no_gene_product_list(self, tmp_path):
        """Model with no listOfGeneProducts still processes associations."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<sbml xmlns="http://www.sbml.org/sbml/level2/version4">
  <model id="m">
    <listOfGeneProductAssociations>
      <geneProductAssociation reaction="R_T1">
        <geneProductRef geneProduct="unknown_gp"/>
      </geneProductAssociation>
    </listOfGeneProductAssociations>
  </model>
</sbml>"""
        f = tmp_path / "nogplist.xml"
        f.write_text(xml_content, encoding="utf-8")
        result = _parse_transyt_xml(f)
        # gp falls back to its id string
        assert "unknown_gp" in result

    def test_model_no_association_list(self, tmp_path):
        """Model with no listOfGeneProductAssociations returns empty dict."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<sbml xmlns="http://www.sbml.org/sbml/level2/version4">
  <model id="m">
    <listOfGeneProducts>
      <geneProduct id="gp1" name="g1"/>
    </listOfGeneProducts>
  </model>
</sbml>"""
        f = tmp_path / "noassoc.xml"
        f.write_text(xml_content, encoding="utf-8")
        result = _parse_transyt_xml(f)
        assert result == {}

    def test_gpr_ref_with_empty_geneproduct_attr(self, tmp_path):
        """geneProductRef with empty geneProduct attr → empty fallback → skipped."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<sbml xmlns="http://www.sbml.org/sbml/level2/version4">
  <model id="m">
    <listOfGeneProducts>
    </listOfGeneProducts>
    <listOfGeneProductAssociations>
      <geneProductAssociation reaction="R_T1">
        <geneProductRef geneProduct=""/>
      </geneProductAssociation>
    </listOfGeneProductAssociations>
  </model>
</sbml>"""
        f = tmp_path / "emptyref.xml"
        f.write_text(xml_content, encoding="utf-8")
        result = _parse_transyt_xml(f)
        # geneProduct="" → id_to_name.get("", "") = "" → gene_name = "" → skipped
        assert result == {}


class TestParseReactionTcEdgeCases:
    """Edge-case coverage for _parse_reaction_tc and _extract_tc_from_reaction."""

    def test_reaction_without_notes_not_in_output(self, tmp_path):
        """Reactions with no notes element yield no TC entry."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<sbml xmlns="http://www.sbml.org/sbml/level2/version4">
  <model id="m">
    <listOfReactions>
      <reaction id="R_T1" reversible="false"/>
    </listOfReactions>
  </model>
</sbml>"""
        f = tmp_path / "nonotes.xml"
        f.write_text(xml_content, encoding="utf-8")
        result = _parse_reaction_tc(f)
        assert "R_T1" not in result

    def test_reaction_notes_without_tc_pattern(self, tmp_path):
        """Reactions with notes but no TC: line yield no TC entry."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<sbml xmlns="http://www.sbml.org/sbml/level2/version4">
  <model id="m">
    <listOfReactions>
      <reaction id="R_T1" reversible="false">
        <notes>
          <html:p xmlns:html="http://www.w3.org/1999/xhtml">No TC here</html:p>
        </notes>
      </reaction>
    </listOfReactions>
  </model>
</sbml>"""
        f = tmp_path / "notcinnotes.xml"
        f.write_text(xml_content, encoding="utf-8")
        result = _parse_reaction_tc(f)
        assert "R_T1" not in result

    def test_reaction_with_non_notes_children_only(self, tmp_path):
        """Reactions whose children are listOfReactants etc. (no notes) yield no TC."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<sbml xmlns="http://www.sbml.org/sbml/level2/version4">
  <model id="m">
    <listOfReactions>
      <reaction id="R_T1" reversible="false">
        <listOfReactants/>
        <listOfProducts/>
      </reaction>
    </listOfReactions>
  </model>
</sbml>"""
        f = tmp_path / "nonnotes.xml"
        f.write_text(xml_content, encoding="utf-8")
        result = _parse_reaction_tc(f)
        assert "R_T1" not in result

    def test_reaction_without_sbml_namespace(self, tmp_path):
        """Reactions without SBML namespace are parsed via fallback."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<sbml>
  <model id="m">
    <listOfReactions>
      <reaction id="R_TX" reversible="false">
        <notes>
          <p xmlns="http://www.w3.org/1999/xhtml">TC: 1.A.1.1.1</p>
        </notes>
      </reaction>
    </listOfReactions>
  </model>
</sbml>"""
        f = tmp_path / "nons.xml"
        f.write_text(xml_content, encoding="utf-8")
        result = _parse_reaction_tc(f)
        assert result.get("R_TX") == "1.A.1.1.1"

    def test_reaction_with_empty_id_skipped(self, tmp_path):
        """Reactions with empty id attribute are skipped."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<sbml xmlns="http://www.sbml.org/sbml/level2/version4">
  <model id="m">
    <listOfReactions>
      <reaction id="" reversible="false">
        <notes><html:p xmlns:html="http://www.w3.org/1999/xhtml">TC: 1.A.1.1.1</html:p></notes>
      </reaction>
    </listOfReactions>
  </model>
</sbml>"""
        f = tmp_path / "emptyid.xml"
        f.write_text(xml_content, encoding="utf-8")
        result = _parse_reaction_tc(f)
        assert result == {}

    def test_model_no_reactions_list(self, tmp_path):
        """Model with no listOfReactions returns empty dict."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<sbml xmlns="http://www.sbml.org/sbml/level2/version4">
  <model id="m">
  </model>
</sbml>"""
        f = tmp_path / "noreactions.xml"
        f.write_text(xml_content, encoding="utf-8")
        assert _parse_reaction_tc(f) == {}

    def test_no_model_element(self, tmp_path):
        """No model element returns empty dict."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<sbml xmlns="http://www.sbml.org/sbml/level2/version4"/>"""
        f = tmp_path / "nomodel.xml"
        f.write_text(xml_content, encoding="utf-8")
        assert _parse_reaction_tc(f) == {}


class TestParseReactionsReferencesEdgeCases:
    """Edge-case coverage for _parse_reactions_references."""

    def test_line_with_only_rxn_id(self, tmp_path):
        """Lines with only the rxn_id column produce None msrxn and empty cpds."""
        f = tmp_path / "refs.txt"
        f.write_text("R_T9\n", encoding="utf-8")
        result = _parse_reactions_references(f)
        assert "R_T9" in result
        msrxn, cpds = result["R_T9"]
        assert msrxn is None
        assert cpds == []


# ---------------------------------------------------------------------------
# _run_docker and _get_image_digest — subprocess mock tests
# ---------------------------------------------------------------------------


class TestRunDockerMocked:
    """Tests for _run_docker using subprocess mocking."""

    def test_run_docker_returns_cmd_str_and_exit_code(self):
        tu = _make_utils()
        tu._docker_image = "test_img:latest"

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            with tempfile.TemporaryDirectory() as tmpdir:
                indir = Path(tmpdir) / "proc"
                indir.mkdir()
                cmd_str, code = tu._run_docker(indir)
        assert isinstance(cmd_str, str)
        assert "docker" in cmd_str
        assert code == 0
        mock_run.assert_called_once()

    def test_run_docker_returns_exit_8(self):
        tu = _make_utils()
        tu._docker_image = "test_img:latest"

        mock_proc = MagicMock()
        mock_proc.returncode = 8
        with patch("subprocess.run", return_value=mock_proc):
            with tempfile.TemporaryDirectory() as tmpdir:
                indir = Path(tmpdir) / "proc"
                indir.mkdir()
                _, code = tu._run_docker(indir)
        assert code == 8


class TestGetImageDigestMocked:
    """Tests for _get_image_digest using subprocess mocking."""

    def test_returns_digest_on_success(self):
        tu = _make_utils()
        tu._docker_image = "img:latest"
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "sha256:abc123\n"
        with patch("subprocess.run", return_value=mock_proc):
            digest = tu._get_image_digest()
        assert digest == "sha256:abc123"

    def test_returns_empty_on_failure(self):
        tu = _make_utils()
        tu._docker_image = "img:latest"
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        with patch("subprocess.run", return_value=mock_proc):
            digest = tu._get_image_digest()
        assert digest == ""

    def test_returns_empty_on_file_not_found(self):
        tu = _make_utils()
        tu._docker_image = "img:latest"
        with patch("subprocess.run", side_effect=FileNotFoundError("no docker")):
            assert tu._get_image_digest() == ""

    def test_returns_empty_on_timeout(self):
        tu = _make_utils()
        tu._docker_image = "img:latest"
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 10)):
            assert tu._get_image_digest() == ""

    def test_returns_empty_on_os_error(self):
        tu = _make_utils()
        tu._docker_image = "img:latest"
        with patch("subprocess.run", side_effect=OSError("os error")):
            assert tu._get_image_digest() == ""


# ---------------------------------------------------------------------------
# annotate() — missing results files after non-8 exit (coverage branch)
# ---------------------------------------------------------------------------


class TestAnnotateMissingResults:
    """Test the branch where exit != 8 but results files are absent."""

    def test_empty_records_when_results_missing_and_exit0(self):
        import tempfile

        tu = _make_utils()
        tu._docker_image = "mock_image:latest"

        def _fake_run_docker(indir):
            # Exit 0 but write NO results files
            return ("docker run ...", 0)

        with patch.object(tu, "is_available", return_value=True):
            with patch.object(tu, "_run_docker", side_effect=_fake_run_docker):
                with patch.object(tu, "_get_image_digest", return_value=""):
                    result = tu.annotate(
                        proteins={"p1": "MKTAY"},
                        tax_id="562",
                    )

        assert result.records == []


# ---------------------------------------------------------------------------
# Import needed for tempfile in new test classes
# ---------------------------------------------------------------------------
import tempfile  # noqa: E402 (intentional late import for clarity)


# ---------------------------------------------------------------------------
# Export tests
# ---------------------------------------------------------------------------


class TestExports:
    """Verify TransytUtils is exported from kbutillib.__init__."""

    def test_transyt_utils_exported(self):
        import kbutillib

        assert hasattr(kbutillib, "TransytUtils")
        assert kbutillib.TransytUtils is not None

    def test_transyt_utils_is_correct_class(self):
        import kbutillib

        assert kbutillib.TransytUtils is TransytUtils


# ---------------------------------------------------------------------------
# Live integration tests (skipped unless Docker image is present)
# ---------------------------------------------------------------------------

_TRANSYT_UTILS_FOR_SKIP = TransytUtils(
    config_file=False, token_file=None, kbase_token_file=None
)


@pytest.mark.integration
@pytest.mark.skipif(
    not _TRANSYT_UTILS_FOR_SKIP.is_available(),
    reason="Transyt Docker image not locally present",
)
class TestTransytLiveIntegration:
    """Live integration tests — only run when the Docker image is present."""

    def test_annotate_returns_result(self):
        tu = TransytUtils(config_file=False, token_file=None, kbase_token_file=None)
        result = tu.annotate(
            proteins={"test_prot": "MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEKAVQVKVKALPDAQFEVVHSLAKWKRQTLGQHDFSAGEGLYTHMKALRPDEDRLSPLHSVYVDQWDWERVMGDGERQFSTSKSVTLKSTLEAIPHESIELPEDGIEYCCRTNAITDEFLETIADKFYINAEKELREHPIFEEAKEIFNSGKDLFEQYREELEKEYGINK"},
            tax_id="562",
        )
        assert isinstance(result, AnnotationResult)
        assert result.tool == "transyt"
        assert result.run_id != ""
