"""Offline unit tests for bakta_utils.py.

Test strategy
-------------
The Docker/subprocess-dependent code paths are exercised only with
``subprocess.run`` mocked out. The pure parse helpers (``_parse_bakta_features``,
``_parse_bakta_db_version``) and the FASTA writer are tested fully offline
against small inline payloads shaped per the module docstring's documented
Bakta ``input.json`` schema.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kbutillib.domains.genome.annotation.annotator_utils import (
    AnnotationResult,
    ToolUnavailableError,
)
from kbutillib.domains.genome.annotation.bakta_utils import (
    BaktaUtils,
    _parse_bakta_db_version,
    _parse_bakta_features,
    _write_one_line_faa,
)
from kbutillib.domains.genome.annotation.ontology_dictionary import OntologyDictionary
from kbutillib.domains.modeling.ec_role_resolver import EcRoleResolver


def _make_utils(db_path: str = "/fake/db", **kwargs: Any) -> BaktaUtils:
    """Create BaktaUtils with no filesystem discovery."""
    return BaktaUtils(
        db_path=db_path,
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# is_available()
# ---------------------------------------------------------------------------


class TestIsAvailable:
    """BaktaUtils.is_available() contract."""

    def test_docker_mode_true_when_image_present(self):
        bu = _make_utils()
        bu._docker_image = "kbutillib/bakta:latest"
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            assert bu.is_available() is True

    def test_docker_mode_false_when_image_absent(self):
        bu = _make_utils()
        bu._docker_image = "kbutillib/bakta:latest"
        with patch("subprocess.run", return_value=MagicMock(returncode=1)):
            assert bu.is_available() is False

    def test_docker_mode_false_on_docker_not_found(self):
        bu = _make_utils()
        bu._docker_image = "kbutillib/bakta:latest"
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            assert bu.is_available() is False

    def test_docker_mode_false_on_timeout(self):
        bu = _make_utils()
        bu._docker_image = "kbutillib/bakta:latest"
        with patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 15)
        ):
            assert bu.is_available() is False

    def test_native_mode_true_when_absolute_executable_exists(self, tmp_path):
        exe = tmp_path / "bakta_proteins"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        bu = _make_utils()
        bu._bakta_exe = str(exe)
        assert bu.is_available() is True

    def test_native_mode_false_when_absolute_executable_missing(self, tmp_path):
        bu = _make_utils()
        bu._bakta_exe = str(tmp_path / "nope")
        assert bu.is_available() is False

    def test_native_mode_uses_which_for_bare_name(self):
        bu = _make_utils()
        bu._bakta_exe = "bakta_proteins"
        with patch("shutil.which", return_value=None):
            assert bu.is_available() is False
        with patch("shutil.which", return_value="/usr/bin/bakta_proteins"):
            assert bu.is_available() is True


# ---------------------------------------------------------------------------
# annotate() — validation before the tool is touched
# ---------------------------------------------------------------------------


class TestAnnotateValidation:
    def test_raises_tool_unavailable_when_absent(self):
        bu = _make_utils()
        with patch.object(bu, "is_available", return_value=False):
            with pytest.raises(ToolUnavailableError):
                bu.annotate(proteins={"p1": "MKTAY"})

    def test_raises_on_nucleotide_input(self):
        bu = _make_utils()
        with patch.object(bu, "is_available", return_value=True):
            nuc = "U" * 100
            with pytest.raises(ValueError, match="protein"):
                bu.annotate(proteins={"g1": nuc})


# ---------------------------------------------------------------------------
# _read_db_version — the fixed-wording ToolUnavailableError
# ---------------------------------------------------------------------------


class TestReadDbVersion:
    def test_missing_version_json_raises_fixed_message(self, tmp_path):
        bu = _make_utils(db_path=str(tmp_path))
        with pytest.raises(ToolUnavailableError) as excinfo:
            bu._read_db_version(str(tmp_path))
        msg = str(excinfo.value)
        assert f"Bakta DB at {tmp_path}" in msg
        assert "missing db/version.json or it is not parseable" in msg

    def test_unparseable_version_json_raises_fixed_message(self, tmp_path):
        (tmp_path / "version.json").write_text("not json{{{", encoding="utf-8")
        bu = _make_utils(db_path=str(tmp_path))
        with pytest.raises(ToolUnavailableError) as excinfo:
            bu._read_db_version(str(tmp_path))
        assert "missing db/version.json or it is not parseable" in str(excinfo.value)

    def test_version_json_missing_major_key_raises(self, tmp_path):
        (tmp_path / "version.json").write_text(
            json.dumps({"date": "2026-01-01"}), encoding="utf-8"
        )
        bu = _make_utils(db_path=str(tmp_path))
        with pytest.raises(ToolUnavailableError):
            bu._read_db_version(str(tmp_path))

    def test_valid_version_json_returns_payload(self, tmp_path):
        payload = {"date": "2026-01-01", "major": 6, "minor": 0, "type": "full"}
        (tmp_path / "version.json").write_text(json.dumps(payload), encoding="utf-8")
        bu = _make_utils(db_path=str(tmp_path))
        result = bu._read_db_version(str(tmp_path))
        assert result["major"] == 6
        assert result["type"] == "full"


class TestParseBaktaDbVersion:
    def test_bad_json_raises_value_error(self):
        with pytest.raises(ValueError):
            _parse_bakta_db_version("{not json")

    def test_missing_major_raises_value_error(self):
        with pytest.raises(ValueError):
            _parse_bakta_db_version(json.dumps({"date": "x"}))

    def test_valid_returns_dict(self):
        payload = _parse_bakta_db_version(json.dumps({"major": 6}))
        assert payload["major"] == 6


# ---------------------------------------------------------------------------
# _parse_bakta_features — D4: product only, everything else in evidence
# ---------------------------------------------------------------------------


class TestParseBaktaFeatures:
    def test_emits_product_as_function_term(self):
        features = [
            {"id": "gene1", "product": "hypothetical protein", "gene": "yfoo"}
        ]
        records = _parse_bakta_features(features)
        assert len(records) == 1
        rec = records[0]
        assert rec.gene_id == "gene1"
        assert len(rec.terms) == 1
        term = rec.terms[0]
        assert term.namespace == "FUNCTION"
        assert term.id is None
        assert term.value == "hypothetical protein"

    def test_ec_kegg_cog_go_now_emitted_as_additive_namespaced_terms(self):
        """PRD kbdl-ontology-descriptions-v1 knowingly reverses D4's original
        "product string only" rule: psc.* accessions are now ADDITIONALLY
        surfaced as their own namespaced terms, alongside (never instead of)
        the unchanged FUNCTION term and its evidence dict.
        """
        features = [
            {
                "id": "gene1",
                "product": "alcohol dehydrogenase",
                "psc": {
                    "ec_ids": ["1.1.1.1"],
                    "kegg_orthology_id": ["K00001"],
                    "cog_id": "COG1064",
                    "go_ids": ["GO:0004022"],
                },
            }
        ]
        records = _parse_bakta_features(features)
        terms = records[0].terms

        function_terms = [t for t in terms if t.namespace == "FUNCTION"]
        assert len(function_terms) == 1
        assert function_terms[0].id is None
        assert function_terms[0].value == "alcohol dehydrogenase"
        # Nothing is silently dropped — evidence still carries the raw psc
        # fields exactly as before (D4's original rule, unchanged).
        assert function_terms[0].evidence["ec_ids"] == ["1.1.1.1"]
        assert function_terms[0].evidence["kegg_orthology_id"] == ["K00001"]
        assert function_terms[0].evidence["cog_id"] == "COG1064"
        assert function_terms[0].evidence["go_ids"] == ["GO:0004022"]

        # Additive namespaced terms, one per psc accession. No
        # OntologyDictionary was injected, so each degrades to the bare
        # accession (never raises, never skips).
        by_namespace = {(t.namespace, t.id): t.value for t in terms if t.namespace != "FUNCTION"}
        assert by_namespace == {
            ("EC", "1.1.1.1"): "1.1.1.1",
            ("KO", "K00001"): "K00001",
            ("COG", "COG1064"): "COG1064",
            ("GO", "GO:0004022"): "GO:0004022",
        }

    def test_psc_accessions_described_when_ontology_dictionary_injected(self):
        od = OntologyDictionary(
            ko_path=None,
            ec_path=None,
            go_path=None,
            cog_path=None,
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        od._tables = {
            "EC": {"1.1.1.1": "alcohol dehydrogenase"},
            "KO": {"K00001": "alcohol dehydrogenase"},
            "COG": {"COG1064": "alcohol dehydrogenase-like protein"},
            "GO": {"GO:0006260": "DNA replication"},
        }
        features = [
            {
                "id": "gene1",
                "product": "alcohol dehydrogenase",
                "psc": {
                    "ec_ids": ["1.1.1.1"],
                    "kegg_orthology_id": ["K00001"],
                    "cog_id": "COG1064",
                    "go_ids": ["GO:0006260"],
                },
            }
        ]
        records = _parse_bakta_features(features, ontology_dictionary=od)
        by_namespace = {
            (t.namespace, t.id): t.value for t in records[0].terms if t.namespace != "FUNCTION"
        }
        assert by_namespace == {
            ("EC", "1.1.1.1"): "1.1.1.1: alcohol dehydrogenase",
            ("KO", "K00001"): "K00001: alcohol dehydrogenase",
            ("COG", "COG1064"): "COG1064: alcohol dehydrogenase-like protein",
            # GO round-trips correctly under a first-": " split even though
            # the accession itself embeds a colon.
            ("GO", "GO:0006260"): "GO:0006260: DNA replication",
        }
        accession, _, description = by_namespace[("GO", "GO:0006260")].partition(": ")
        assert accession == "GO:0006260"
        assert description == "DNA replication"

    def test_psc_missing_dictionary_degrades_to_accession_only(self):
        """A namespace with no staged dictionary file degrades to the bare
        accession (never raises) and is recorded in degraded_namespaces."""
        od = OntologyDictionary(
            config_file=False, token_file=None, kbase_token_file=None
        )
        features = [
            {
                "id": "gene1",
                "product": "alcohol dehydrogenase",
                "psc": {"ec_ids": ["1.1.1.1"]},
            }
        ]
        records = _parse_bakta_features(features, ontology_dictionary=od)
        ec_term = next(t for t in records[0].terms if t.namespace == "EC")
        assert ec_term.value == "1.1.1.1"
        assert "EC" in od.degraded_namespaces

    def test_ec_role_resolver_yields_role_namespace_terms(self):
        resolver = EcRoleResolver("/unused/roles.tsv")
        resolver._index = {
            "1.1.1.1": [
                "Alcohol dehydrogenase (EC 1.1.1.1)",
                "Bifunctional dehydrogenase (EC 1.1.1.1)",
            ]
        }
        features = [
            {
                "id": "gene1",
                "product": "alcohol dehydrogenase",
                "psc": {"ec_ids": ["1.1.1.1"]},
            }
        ]
        records = _parse_bakta_features(features, ec_role_resolver=resolver)
        role_values = {t.value for t in records[0].terms if t.namespace == "role"}
        assert role_values == {
            "Alcohol dehydrogenase (EC 1.1.1.1)",
            "Bifunctional dehydrogenase (EC 1.1.1.1)",
        }

    def test_no_ec_role_resolver_yields_no_role_terms(self):
        features = [
            {
                "id": "gene1",
                "product": "alcohol dehydrogenase",
                "psc": {"ec_ids": ["1.1.1.1"]},
            }
        ]
        records = _parse_bakta_features(features, ec_role_resolver=None)
        assert all(t.namespace != "role" for t in records[0].terms)

    def test_aa_hexdigest_carried_in_evidence(self):
        features = [
            {"id": "gene1", "product": "product X", "aa_hexdigest": "abc123"}
        ]
        term = _parse_bakta_features(features)[0].terms[0]
        assert term.evidence["aa_hexdigest"] == "abc123"

    def test_gene_absent_when_no_product(self):
        features = [{"id": "gene1", "product": ""}, {"id": "gene2"}]
        assert _parse_bakta_features(features) == []

    def test_filters_to_input_ids(self):
        features = [
            {"id": "gene1", "product": "product 1"},
            {"id": "stray", "product": "product 2"},
        ]
        records = _parse_bakta_features(features, input_ids={"gene1"})
        assert {r.gene_id for r in records} == {"gene1"}

    def test_no_input_ids_filter_keeps_everything(self):
        features = [
            {"id": "gene1", "product": "product 1"},
            {"id": "gene2", "product": "product 2"},
        ]
        records = _parse_bakta_features(features)
        assert {r.gene_id for r in records} == {"gene1", "gene2"}

    def test_non_dict_features_skipped(self):
        assert _parse_bakta_features([None, "junk", 42]) == []

    def test_missing_id_skipped(self):
        assert _parse_bakta_features([{"product": "x"}]) == []


# ---------------------------------------------------------------------------
# _write_one_line_faa
# ---------------------------------------------------------------------------


class TestWriteOneLineFaa:
    def test_writes_header_and_sequence(self, tmp_path):
        path = tmp_path / "input.faa"
        _write_one_line_faa(path, {"gene1": "MKTAY", "gene2": "MNFST"})
        text = path.read_text()
        lines = text.strip("\n").split("\n")
        assert lines == [">gene1", "MKTAY", ">gene2", "MNFST"]

    def test_strips_whitespace_from_sequence(self, tmp_path):
        path = tmp_path / "input.faa"
        _write_one_line_faa(path, {"g1": "  MKTAY \n"})
        assert "MKTAY" in path.read_text()
        assert "  MKTAY" not in path.read_text()


# ---------------------------------------------------------------------------
# _run_bakta — command shape (native + docker)
# ---------------------------------------------------------------------------


class TestRunBaktaCommandShape:
    def _fake_completed(self, returncode=0, stdout="", stderr=""):
        return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_native_command_shape(self, tmp_path):
        bu = _make_utils(db_path=str(tmp_path))
        fasta = tmp_path / "input.faa"
        fasta.write_text(">g1\nMKTAY\n")
        outdir = tmp_path / "out"

        with patch("subprocess.run", return_value=self._fake_completed()) as mock_run:
            payload, cmd = bu._run_bakta(
                fasta_path=fasta, outdir=outdir, resolved_db=str(tmp_path), threads=2
            )

        argv = mock_run.call_args[0][0]
        assert argv[0] == "bakta_proteins"
        assert "--threads" in argv and "2" in argv
        assert "--db" in argv and str(tmp_path) in argv
        assert payload == {}  # no input.json written by the fake run

    def test_docker_command_has_user_network_none_and_ro_mount(self, tmp_path):
        bu = _make_utils(db_path=str(tmp_path))
        bu._docker_image = "kbutillib/bakta:latest"
        fasta = tmp_path / "input.faa"
        fasta.write_text(">g1\nMKTAY\n")
        outdir = tmp_path / "out"

        with patch("subprocess.run", return_value=self._fake_completed()) as mock_run:
            bu._run_bakta(
                fasta_path=fasta, outdir=outdir, resolved_db=str(tmp_path), threads=1
            )

        argv = mock_run.call_args[0][0]
        assert argv[:2] == ["docker", "run"]
        assert "--user" in argv
        user_val = argv[argv.index("--user") + 1]
        assert ":" in user_val
        assert "--network" in argv
        assert argv[argv.index("--network") + 1] == "none"
        # Database mounted read-only.
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        assert any(m.endswith(":/db:ro") for m in mounts)
        # Entrypoint overridden to bash so PATH can be prefixed.
        assert "--entrypoint" in argv
        assert argv[argv.index("--entrypoint") + 1] == "bash"
        inner = argv[-1]
        assert "/opt/conda/bin" in inner
        assert "bakta_proteins" in inner

    def test_nonzero_exit_raises_called_process_error(self, tmp_path):
        bu = _make_utils(db_path=str(tmp_path))
        fasta = tmp_path / "input.faa"
        fasta.write_text(">g1\nMKTAY\n")
        outdir = tmp_path / "out"
        with patch(
            "subprocess.run",
            return_value=self._fake_completed(returncode=1, stderr="boom"),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                bu._run_bakta(
                    fasta_path=fasta,
                    outdir=outdir,
                    resolved_db=str(tmp_path),
                    threads=1,
                )

    def test_parses_input_json_when_present(self, tmp_path):
        bu = _make_utils(db_path=str(tmp_path))
        fasta = tmp_path / "input.faa"
        fasta.write_text(">g1\nMKTAY\n")
        outdir = tmp_path / "out"
        outdir.mkdir()
        payload = {
            "version": {"bakta": "1.11.4", "db": {"version": "6.0", "type": "full"}},
            "features": [{"id": "g1", "product": "test product"}],
        }
        (outdir / "input.json").write_text(json.dumps(payload), encoding="utf-8")

        with patch("subprocess.run", return_value=self._fake_completed()):
            result_payload, _cmd = bu._run_bakta(
                fasta_path=fasta, outdir=outdir, resolved_db=str(tmp_path), threads=1
            )

        assert result_payload["version"]["bakta"] == "1.11.4"
        assert result_payload["features"][0]["id"] == "g1"

    def test_native_outdir_not_pre_created_only_parent_exists(self, tmp_path):
        """bakta_proteins refuses a pre-existing --output dir (native path).

        Assert the *observable state at subprocess-invocation time*: outdir
        must not exist yet, while its parent must. Asserting only that
        ``Path.mkdir`` was not called would be a weaker claim than asserting
        what the tool actually sees when the command runs.
        """
        bu = _make_utils(db_path=str(tmp_path))
        fasta = tmp_path / "input.faa"
        fasta.write_text(">g1\nMKTAY\n")
        outdir = tmp_path / "out"

        seen: dict[str, bool] = {}

        def _fake_run(cmd, **kwargs):
            seen["outdir_exists"] = outdir.exists()
            seen["parent_exists"] = outdir.parent.exists()
            return self._fake_completed()

        with patch("subprocess.run", side_effect=_fake_run):
            bu._run_bakta(
                fasta_path=fasta, outdir=outdir, resolved_db=str(tmp_path), threads=1
            )

        assert seen["outdir_exists"] is False
        assert seen["parent_exists"] is True

    def test_docker_outdir_not_pre_created_only_parent_exists(self, tmp_path):
        """bakta_proteins refuses a pre-existing --output dir (docker path).

        Same observable-state assertion as the native-path test above,
        exercised through the docker command-construction branch.
        """
        bu = _make_utils(db_path=str(tmp_path))
        bu._docker_image = "kbutillib/bakta:latest"
        fasta = tmp_path / "input.faa"
        fasta.write_text(">g1\nMKTAY\n")
        outdir = tmp_path / "out"

        seen: dict[str, bool] = {}

        def _fake_run(cmd, **kwargs):
            seen["outdir_exists"] = outdir.exists()
            seen["parent_exists"] = outdir.parent.exists()
            return self._fake_completed()

        with patch("subprocess.run", side_effect=_fake_run):
            bu._run_bakta(
                fasta_path=fasta, outdir=outdir, resolved_db=str(tmp_path), threads=1
            )

        assert seen["outdir_exists"] is False
        assert seen["parent_exists"] is True


# ---------------------------------------------------------------------------
# annotate() — full path, subprocess + db version mocked
# ---------------------------------------------------------------------------


class TestAnnotateMocked:
    def _fake_run_bakta(self, fasta_path, outdir, resolved_db, threads):
        payload = {
            "version": {"bakta": "1.11.4", "db": {"version": "6.0", "type": "full"}},
            "features": [
                {"id": "gene1", "product": "alcohol dehydrogenase"},
                {"id": "gene2", "product": "catalase"},
            ],
        }
        return payload, "bakta_proteins --db /fake/db ..."

    def test_returns_annotation_result_with_versions_and_records(self, tmp_path):
        db_payload = {"date": "2026-01-01", "major": 6, "minor": 0, "type": "full"}
        (tmp_path / "version.json").write_text(json.dumps(db_payload), encoding="utf-8")
        bu = _make_utils(db_path=str(tmp_path))

        with patch.object(bu, "is_available", return_value=True), patch.object(
            bu, "_run_bakta", side_effect=self._fake_run_bakta
        ):
            result = bu.annotate(proteins={"gene1": "MKTAY", "gene2": "MNFST"})

        assert isinstance(result, AnnotationResult)
        assert result.tool == "bakta"
        assert result.tool_version == "1.11.4"
        assert result.db_version == "6.0"
        assert result.parameters["db_major"] == 6
        gene_ids = {r.gene_id for r in result.records}
        assert gene_ids == {"gene1", "gene2"}

    def test_raises_when_db_version_json_missing(self, tmp_path):
        bu = _make_utils(db_path=str(tmp_path))
        with patch.object(bu, "is_available", return_value=True):
            with pytest.raises(ToolUnavailableError, match="db/version.json"):
                bu.annotate(proteins={"gene1": "MKTAY"})

    def test_ontology_and_ec_role_degradation_recorded_in_parameters(self, tmp_path):
        """No ontology_dictionary/ec_role_resolver injected -> parameters
        records the degradation (never raises), mirroring KofamscanUtils's
        existing 'ko_function_map: absent' behaviour."""
        db_payload = {"date": "2026-01-01", "major": 6, "minor": 0, "type": "full"}
        (tmp_path / "version.json").write_text(json.dumps(db_payload), encoding="utf-8")
        bu = _make_utils(db_path=str(tmp_path))

        def _fake_run_bakta_with_psc(fasta_path, outdir, resolved_db, threads):
            payload = {
                "version": {"bakta": "1.11.4", "db": {"version": "6.0", "type": "full"}},
                "features": [
                    {
                        "id": "gene1",
                        "product": "alcohol dehydrogenase",
                        "psc": {"ec_ids": ["1.1.1.1"], "kegg_orthology_id": ["K00001"]},
                    }
                ],
            }
            return payload, "bakta_proteins --db /fake/db ..."

        with patch.object(bu, "is_available", return_value=True), patch.object(
            bu, "_run_bakta", side_effect=_fake_run_bakta_with_psc
        ):
            result = bu.annotate(proteins={"gene1": "MKTAY"})

        assert result.parameters["ontology_ec"] == "absent"
        assert result.parameters["ontology_ko"] == "absent"
        assert result.parameters["ec_role_resolver"] == "absent"

    def test_function_term_byte_identical_to_base_commit_capture(self, tmp_path):
        """Captured from base commit 222490d (unmodified _parse_bakta_features)
        via the exact feature dict below, BEFORE this task's changes:

            _parse_bakta_features([{
                "id": "gene1",
                "product": "alcohol dehydrogenase, zinc-containing",
                "gene": "adhA",
                "aa_hexdigest": "abc123",
                "psc": {
                    "ec_ids": ["1.1.1.1"],
                    "kegg_orthology_id": ["K00001"],
                    "cog_id": "COG1064",
                    "go_ids": ["GO:0006260"],
                },
            }])[0].terms[0].value

        captured literal: 'alcohol dehydrogenase, zinc-containing'

        This asserts the branch's FUNCTION-destined term is still exactly
        that literal, unaffected by any ontology-description machinery
        added by this task -- reactions are reached downstream by joining
        this exact text against mapping tables keyed on it.
        """
        base_commit_captured_value = "alcohol dehydrogenase, zinc-containing"

        db_payload = {"date": "2026-01-01", "major": 6, "minor": 0, "type": "full"}
        (tmp_path / "version.json").write_text(json.dumps(db_payload), encoding="utf-8")

        # An OntologyDictionary WITH descriptions staged for every accession
        # this feature carries -- the maximally adversarial case for byte
        # identity, since if the FUNCTION term were vulnerable to being
        # folded together with accession data, this is where it would show.
        od = OntologyDictionary(
            config_file=False, token_file=None, kbase_token_file=None
        )
        od._tables = {
            "EC": {"1.1.1.1": "alcohol dehydrogenase"},
            "KO": {"K00001": "alcohol dehydrogenase"},
            "COG": {"COG1064": "alcohol dehydrogenase-like"},
            "GO": {"GO:0006260": "DNA replication"},
        }
        resolver = EcRoleResolver("/unused/roles.tsv")
        resolver._index = {"1.1.1.1": ["Alcohol dehydrogenase (EC 1.1.1.1)"]}
        bu = _make_utils(
            db_path=str(tmp_path),
            ontology_dictionary=od,
            ec_role_resolver=resolver,
        )

        def _fake_run_bakta_with_psc(fasta_path, outdir, resolved_db, threads):
            payload = {
                "version": {"bakta": "1.11.4", "db": {"version": "6.0", "type": "full"}},
                "features": [
                    {
                        "id": "gene1",
                        "product": "alcohol dehydrogenase, zinc-containing",
                        "gene": "adhA",
                        "aa_hexdigest": "abc123",
                        "psc": {
                            "ec_ids": ["1.1.1.1"],
                            "kegg_orthology_id": ["K00001"],
                            "cog_id": "COG1064",
                            "go_ids": ["GO:0006260"],
                        },
                    }
                ],
            }
            return payload, "bakta_proteins --db /fake/db ..."

        with patch.object(bu, "is_available", return_value=True), patch.object(
            bu, "_run_bakta", side_effect=_fake_run_bakta_with_psc
        ):
            result = bu.annotate(proteins={"gene1": "MKTAY"})

        rec = next(r for r in result.records if r.gene_id == "gene1")
        function_term = next(t for t in rec.terms if t.namespace == "FUNCTION")
        assert function_term.value == base_commit_captured_value
        assert function_term.id is None
        # And the new namespaced terms are additive, not a replacement.
        assert any(t.namespace == "EC" for t in rec.terms)
        assert any(t.namespace == "role" for t in rec.terms)


# ---------------------------------------------------------------------------
# Package export
# ---------------------------------------------------------------------------


class TestPackageExport:
    def test_exported_from_annotation_package(self):
        from kbutillib.domains.genome.annotation import BaktaUtils as ReExported

        assert ReExported is BaktaUtils
