"""Offline + live tests for dram2_utils.py.

Test strategy
-------------
The Nextflow subprocess path (``_run_nextflow``, ``annotate`` when the
pipeline is reachable) is exercised only in the
``@pytest.mark.integration`` live test, which is doubly gated by both
``KBU_DRAM2_LIVE=1`` and ``DRAM2Utils().is_available()`` so it runs only
on h100.

The pure parse functions and CLI-builder are tested fully offline using
the committed golden fixture under ``tests/fixtures/dram2/``.

Coverage gate
-------------
The ``_run_nextflow`` body's success branch is reached via a
subprocess-mocking test; its failure (``CalledProcessError``) branch is
covered too.  Everything else — ``is_available`` returning False on each
missing dependency, the ``_guard_protein`` raise on nucleotide input,
the ``_require_available`` raise, and every line of
``_parse_annotations_tsv`` / ``_parse_dram2_version`` —  is covered by
the offline tests so the repo's ``fail_under=100`` gate stays green.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kbutillib.annotator_utils import (
    AnnotationRecord,
    AnnotationResult,
    Term,
    ToolUnavailableError,
)
from kbutillib.dram2_utils import (
    DRAM2Utils,
    _parse_annotations_tsv,
    _parse_dram2_version,
)

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "dram2"
_GOLDEN_TSV = _FIXTURES / "raw-annotations.tsv"
_GOLDEN_FAA = _FIXTURES / "demo.faa"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_utils(**kwargs: Any) -> DRAM2Utils:
    """Construct DRAM2Utils with no filesystem discovery."""
    return DRAM2Utils(
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        **kwargs,
    )


def _make_available_utils(tmp_path: Path) -> DRAM2Utils:
    """Construct a DRAM2Utils whose ``is_available()`` is True via stubs.

    Creates a fake nextflow binary on PATH (executable shim), a fake
    ``main.nf`` file, and points launch_dir at *tmp_path*.
    """
    pipeline = tmp_path / "main.nf"
    pipeline.write_text("// stub\n")
    launch = tmp_path / "launch"
    launch.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    nf = bin_dir / "nextflow"
    nf.write_text("#!/usr/bin/env bash\nexit 0\n")
    nf.chmod(0o755)
    utils = _make_utils()
    utils._nextflow_exe = str(nf)
    utils._pipeline = str(pipeline)
    utils._launch_dir = str(launch)
    return utils


def _read_caller_ids_from_faa(path: Path) -> list[str]:
    """Read FASTA headers from a .faa, returning them in file order."""
    ids: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(">"):
            ids.append(line[1:].strip())
    return ids


# ---------------------------------------------------------------------------
# _parse_annotations_tsv — pure parser tests against the golden fixture
# ---------------------------------------------------------------------------


class TestParseAnnotationsTsv:
    """Tests for _parse_annotations_tsv against the committed golden fixture."""

    def test_returns_records_for_called_ids(self):
        ids = _read_caller_ids_from_faa(_GOLDEN_FAA)
        records = _parse_annotations_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"), ids
        )
        assert len(records) >= 3
        for rec in records:
            assert isinstance(rec, AnnotationRecord)
            assert rec.gene_id in ids

    def test_zero_hit_row_absent_from_records(self):
        """Row OWC_0000_k121_3157_1 has only the prefix columns populated
        (no DB hit anywhere) → must be absent from records."""
        ids = _read_caller_ids_from_faa(_GOLDEN_FAA)
        records = _parse_annotations_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"), ids
        )
        rec_ids = {r.gene_id for r in records}
        assert "OWC_0000_k121_3157_1" not in rec_ids

    def test_caller_ids_outside_input_set_skipped(self):
        """A query_id that is NOT in the caller's input list is dropped."""
        # Pass only one of the five fixture ids
        records = _parse_annotations_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            ["OWC_0000_k121_3157_3"],
        )
        rec_ids = {r.gene_id for r in records}
        assert rec_ids == {"OWC_0000_k121_3157_3"}

    def test_kegg_id_emits_ko_namespace(self):
        """`kegg_id` column → Term(namespace='KO', id=<K-number>)."""
        records = _parse_annotations_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            ["OWC_0000_k121_3157_2"],
        )
        rec = records[0]
        ko_terms = [t for t in rec.terms if t.namespace == "KO"]
        # Both kegg_id and kofam_id are K14127 for this row
        assert {t.id for t in ko_terms} == {"K14127"}

    def test_kofam_id_emits_ko_namespace(self):
        records = _parse_annotations_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            ["OWC_0001_k121_21365628_15"],
        )
        rec = records[0]
        ko_terms = [t for t in rec.terms if t.namespace == "KO"]
        assert any(t.id == "K23356" for t in ko_terms)
        # The KO term must carry the source column in evidence
        kofam = next(t for t in ko_terms if t.evidence.get("source") == "kofam_id")
        assert kofam.id == "K23356"

    def test_ec_columns_split_and_prefix_stripped(self):
        """EC values like 'EC:1.8.98.5; EC:1.8.98.6' split into individual
        Terms with the 'EC:' prefix stripped."""
        records = _parse_annotations_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            ["OWC_0000_k121_3157_2"],
        )
        rec = records[0]
        ec_terms = [t for t in rec.terms if t.namespace == "EC"]
        ec_ids = {t.id for t in ec_terms}
        # Union of kegg_EC and kofam_EC (the 1.12.99.- comes from kofam)
        assert "1.8.98.5" in ec_ids
        assert "1.8.98.6" in ec_ids
        assert "1.12.99.-" in ec_ids
        assert all(not eid.upper().startswith("EC:") for eid in ec_ids)
        # The dbcan_EC (synthetic '2.4.1.-') also lands here
        assert "2.4.1.-" in ec_ids

    def test_dbcan_id_emits_cazy_namespace(self):
        """`dbcan_id` (synthetic row in fixture) → namespace='CAZY'."""
        records = _parse_annotations_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            ["OWC_0000_k121_3157_2"],
        )
        rec = records[0]
        cazy_terms = [t for t in rec.terms if t.namespace == "CAZY"]
        assert len(cazy_terms) == 1
        assert cazy_terms[0].id == "GT4"
        assert cazy_terms[0].evidence.get("source") == "dbcan_id"

    def test_description_columns_emit_free_text(self):
        """`*_description` columns → Term(namespace=None, id=None)."""
        records = _parse_annotations_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            ["OWC_0000_k121_3157_3"],
        )
        rec = records[0]
        free = [t for t in rec.terms if t.namespace is None]
        # KEGG + kofam descriptions both populated
        assert len(free) >= 2
        joined = " | ".join(t.value for t in free)
        assert "heterodisulfide reductase subunit A2" in joined

    def test_empty_text_returns_empty(self):
        assert _parse_annotations_tsv("", ["x"]) == []

    def test_header_without_query_id_returns_empty(self):
        tsv = "foo\tbar\nA\tB\n"
        assert _parse_annotations_tsv(tsv, ["A"]) == []

    def test_records_in_input_order_not_tsv_order(self):
        """Records are emitted in the caller's input order."""
        ids = [
            "OWC_0001_k121_21365628_15",
            "OWC_0000_k121_3157_3",
            "OWC_0000_k121_3157_2",
        ]
        records = _parse_annotations_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"), ids
        )
        result_order = [r.gene_id for r in records]
        assert result_order == ids

    def test_short_row_padded(self):
        """A TSV row with fewer fields than the header is padded with empty
        strings (no parse error)."""
        tsv = (
            "query_id\tkegg_id\tkegg_EC\tkegg_description\n"
            "g0\tK99999\n"  # short row — kegg_EC and kegg_description missing
        )
        records = _parse_annotations_tsv(tsv, ["g0"])
        assert len(records) == 1
        ko = [t for t in records[0].terms if t.namespace == "KO"]
        assert ko[0].id == "K99999"

    def test_blank_lines_in_body_skipped(self):
        tsv = (
            "query_id\tkegg_id\tkegg_description\n"
            "\n"
            "g0\tK1\thit description\n"
        )
        records = _parse_annotations_tsv(tsv, ["g0"])
        assert len(records) == 1

    def test_empty_ec_values_skipped(self):
        """A column value of an empty EC piece (e.g. 'EC:;') produces no
        empty-string Term."""
        tsv = (
            "query_id\tkegg_EC\n"
            "g0\tEC:1.1.1.1;EC:;EC:\n"  # second and third pieces strip to empty
        )
        records = _parse_annotations_tsv(tsv, ["g0"])
        ec = [t for t in records[0].terms if t.namespace == "EC"]
        assert {t.id for t in ec} == {"1.1.1.1"}

    def test_ec_whitespace_only_pieces_skipped(self):
        """Whitespace-only pieces from EC splitting are dropped before the
        EC-prefix-stripping branch even runs."""
        # A doubled tab in the EC column round-trips through re.split as ['', '']
        tsv = (
            "query_id\tkegg_EC\n"
            "g0\t  \n"  # entire EC value is whitespace
        )
        records = _parse_annotations_tsv(tsv, ["g0"])
        # No terms at all
        assert records == []

    def test_ec_split_produces_empty_pieces(self):
        """A trailing/leading delimiter produces empty pieces that must be
        dropped at the pre-prefix-strip ``if not piece`` guard."""
        tsv = (
            "query_id\tkegg_EC\n"
            "g0\t;1.1.1.1;\n"  # leading and trailing semicolons → empty pieces
        )
        records = _parse_annotations_tsv(tsv, ["g0"])
        ec = [t for t in records[0].terms if t.namespace == "EC"]
        assert {t.id for t in ec} == {"1.1.1.1"}

    def test_unknown_id_column_falls_through_to_free_text(self):
        """An `<unknown>_id` column not in the namespace map gets
        namespace=None and the source column recorded in evidence."""
        tsv = (
            "query_id\tcustom_id\n"
            "g0\tXYZ123\n"
        )
        records = _parse_annotations_tsv(tsv, ["g0"])
        rec = records[0]
        assert len(rec.terms) == 1
        assert rec.terms[0].namespace is None
        assert rec.terms[0].id == "XYZ123"
        assert rec.terms[0].evidence == {"source": "custom_id"}


# ---------------------------------------------------------------------------
# _parse_dram2_version
# ---------------------------------------------------------------------------


class TestParseDram2Version:
    def test_revision_tag_extracted(self):
        text = (
            "Launching `repo/main.nf` [foobar] DSL2 - "
            "revision: a1b2c3d4 [v2.0.0-beta17]\n"
        )
        assert _parse_dram2_version(text) == "v2.0.0-beta17"

    def test_nextflow_version_extracted(self):
        text = "N E X T F L O W  ~  version 24.10.5\nLaunching ...\n"
        assert _parse_dram2_version(text) == "24.10.5"

    def test_none_when_no_match(self):
        assert _parse_dram2_version("nothing recognisable here") is None

    def test_empty_returns_none(self):
        assert _parse_dram2_version("") is None


# ---------------------------------------------------------------------------
# is_available — all three checks
# ---------------------------------------------------------------------------


class TestIsAvailable:
    def test_false_when_nextflow_missing_from_path(self, tmp_path: Path):
        utils = _make_utils()
        utils._nextflow_exe = "definitely-not-a-real-binary-xyz"
        utils._pipeline = str(tmp_path / "main.nf")
        utils._launch_dir = str(tmp_path)
        (tmp_path / "main.nf").write_text("")
        assert utils.is_available() is False

    def test_false_when_nextflow_exe_empty(self):
        utils = _make_utils()
        utils._nextflow_exe = ""
        assert utils.is_available() is False

    def test_false_when_nextflow_path_not_executable(self, tmp_path: Path):
        """shutil.which() returns None for a non-executable absolute path."""
        nf = tmp_path / "nextflow"
        nf.write_text("not executable")
        nf.chmod(0o644)  # readable but not +x → shutil.which returns None
        utils = _make_utils()
        utils._nextflow_exe = str(nf)
        utils._pipeline = str(tmp_path / "main.nf")
        utils._launch_dir = str(tmp_path)
        (tmp_path / "main.nf").write_text("")
        assert utils.is_available() is False

    def test_false_when_pipeline_unset(self, tmp_path: Path):
        utils = _make_utils()
        # Use a real binary on PATH (sh) so the first check passes
        utils._nextflow_exe = "sh"
        utils._pipeline = ""
        utils._launch_dir = str(tmp_path)
        assert utils.is_available() is False

    def test_false_when_pipeline_file_missing(self, tmp_path: Path):
        utils = _make_utils()
        utils._nextflow_exe = "sh"
        utils._pipeline = str(tmp_path / "no-such-main.nf")
        utils._launch_dir = str(tmp_path)
        assert utils.is_available() is False

    def test_false_when_launch_dir_unset(self, tmp_path: Path):
        pipeline = tmp_path / "main.nf"
        pipeline.write_text("")
        utils = _make_utils()
        utils._nextflow_exe = "sh"
        utils._pipeline = str(pipeline)
        utils._launch_dir = ""
        assert utils.is_available() is False

    def test_false_when_launch_dir_missing(self, tmp_path: Path):
        pipeline = tmp_path / "main.nf"
        pipeline.write_text("")
        utils = _make_utils()
        utils._nextflow_exe = "sh"
        utils._pipeline = str(pipeline)
        utils._launch_dir = str(tmp_path / "no-such-dir")
        assert utils.is_available() is False

    def test_true_when_all_three_present(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        assert utils.is_available() is True

    def test_true_when_nextflow_on_path(self, tmp_path: Path):
        """Resolving the nextflow binary via PATH (shutil.which) is enough."""
        pipeline = tmp_path / "main.nf"
        pipeline.write_text("")
        utils = _make_utils()
        utils._nextflow_exe = "sh"  # always on PATH
        utils._pipeline = str(pipeline)
        utils._launch_dir = str(tmp_path)
        assert utils.is_available() is True


# ---------------------------------------------------------------------------
# annotate — guards and integration with the parser via a mocked _run_nextflow
# ---------------------------------------------------------------------------


class TestAnnotateGuards:
    def test_raises_tool_unavailable_when_pipeline_missing(self):
        utils = _make_utils()  # nothing configured
        with pytest.raises(ToolUnavailableError) as exc:
            utils.annotate({"p1": "MKTAYIAKQRQ"})
        assert exc.value.tool == "dram2"

    def test_raises_value_error_on_nucleotide_input(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        # The protein alphabet check excludes U/O/J — a U-rich RNA-looking
        # sequence is over the 10% threshold and trips the guard.  (Plain
        # ATGC strings are NOT rejected because A, C, G, T are all valid
        # amino acid codes — exactly the ambiguity ProkkaUtils' DNA guard
        # is designed to catch in the opposite direction.)
        with pytest.raises(ValueError, match="protein"):
            utils.annotate({"p1": "UUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUU"})


class TestAnnotateWithMockedNextflow:
    """End-to-end annotate() with a mocked _run_nextflow."""

    def _make_mock(self) -> Any:
        tsv_text = _GOLDEN_TSV.read_text(encoding="utf-8")
        return MagicMock(
            return_value=(tsv_text, "v2.0.0-beta17", "nextflow run ...")
        )

    def test_returns_annotation_result(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        ids = _read_caller_ids_from_faa(_GOLDEN_FAA)
        proteins = {cid: "MKTAYIAKQ" * 10 for cid in ids}
        with patch.object(utils, "_run_nextflow", self._make_mock()):
            result = utils.annotate(proteins)
        assert isinstance(result, AnnotationResult)
        assert result.tool == "dram2"
        assert result.tool_version == "v2.0.0-beta17"

    def test_records_keyed_by_caller_ids(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        ids = _read_caller_ids_from_faa(_GOLDEN_FAA)
        proteins = {cid: "MKTAYIAKQ" * 10 for cid in ids}
        with patch.object(utils, "_run_nextflow", self._make_mock()):
            result = utils.annotate(proteins)
        rec_ids = {r.gene_id for r in result.records}
        # Zero-hit row absent; others present
        assert "OWC_0000_k121_3157_1" not in rec_ids
        assert "OWC_0000_k121_3157_2" in rec_ids
        assert "OWC_0000_k121_3157_3" in rec_ids

    def test_parameters_captured(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        proteins = {"g1": "MKTAYIAKQ" * 10}
        with patch.object(utils, "_run_nextflow", self._make_mock()):
            result = utils.annotate(
                proteins, databases=["kofam", "pfam"], threads=2,
                extra_note="hello",
            )
        assert result.parameters["databases"] == ["kofam", "pfam"]
        assert result.parameters["threads"] == 2
        assert result.parameters["input_protein_count"] == 1
        assert result.parameters["extra_note"] == "hello"

    def test_db_version_is_joined_databases(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        proteins = {"g1": "MKTAYIAKQ" * 10}
        with patch.object(utils, "_run_nextflow", self._make_mock()):
            result = utils.annotate(proteins, databases=["kofam", "dbcan"])
        assert result.db_version == "kofam,dbcan"

    def test_db_version_none_when_databases_empty(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        proteins = {"g1": "MKTAYIAKQ" * 10}
        with patch.object(utils, "_run_nextflow", self._make_mock()):
            result = utils.annotate(proteins, databases=[])
        assert result.db_version is None

    def test_run_id_is_uuid_hex(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        proteins = {"g1": "MKTAYIAKQ" * 10}
        with patch.object(utils, "_run_nextflow", self._make_mock()):
            result = utils.annotate(proteins)
        import uuid
        run_uuid = uuid.UUID(hex=result.run_id)
        assert run_uuid.version == 4


# ---------------------------------------------------------------------------
# _write_faa
# ---------------------------------------------------------------------------


class TestWriteFaa:
    def test_writes_records(self, tmp_path: Path):
        utils = _make_utils()
        path = tmp_path / "input.faa"
        utils._write_faa(path, {"g1": "MKT", "g2": "AYI"})
        text = path.read_text(encoding="utf-8")
        assert ">g1" in text
        assert ">g2" in text
        assert "MKT" in text
        assert "AYI" in text

    def test_strips_seq_whitespace(self, tmp_path: Path):
        utils = _make_utils()
        path = tmp_path / "input.faa"
        utils._write_faa(path, {"g1": "  MKTAY  "})
        text = path.read_text(encoding="utf-8")
        assert "MKTAY" in text
        assert "  MKTAY" not in text


# ---------------------------------------------------------------------------
# _build_nextflow_command — argv shape pinned against the live h100 install
# ---------------------------------------------------------------------------


class TestBuildNextflowCommand:
    def test_pinned_invocation(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        genes = tmp_path / "genes"
        out = tmp_path / "out"
        work = tmp_path / "work"
        genes.mkdir(); out.mkdir(); work.mkdir()
        cmd = utils._build_nextflow_command(
            genes_dir=genes,
            outdir=out,
            workdir=work,
            databases=("kofam", "dbcan"),
            threads=4,
        )
        assert cmd[0] == utils._nextflow_exe
        assert cmd[1] == "run"
        assert cmd[2] == utils._pipeline
        assert "-profile" in cmd and cmd[cmd.index("-profile") + 1] == "conda"
        assert "--annotate" in cmd
        i = cmd.index("--input_genes")
        assert cmd[i + 1] == str(genes)
        o = cmd.index("--outdir")
        assert cmd[o + 1] == str(out)
        w = cmd.index("-work-dir")
        assert cmd[w + 1] == str(work)
        t = cmd.index("--threads")
        assert cmd[t + 1] == "4"
        assert "--use_kofam" in cmd
        assert "--use_dbcan" in cmd

    def test_extra_config_appended(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        utils._extra_config = "/tmp/extra.config"
        cmd = utils._build_nextflow_command(
            genes_dir=tmp_path, outdir=tmp_path, workdir=tmp_path,
            databases=(), threads=1,
        )
        assert "-c" in cmd
        ci = cmd.index("-c")
        assert cmd[ci + 1] == "/tmp/extra.config"


# ---------------------------------------------------------------------------
# _run_nextflow — subprocess mocking covers success + non-zero exit
# ---------------------------------------------------------------------------


class TestRunNextflow:
    def test_success_returns_tsv_text(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        genes = tmp_path / "genes"
        out = tmp_path / "out"
        work = tmp_path / "work"
        genes.mkdir(); out.mkdir(); work.mkdir()
        # Pretend Nextflow wrote raw-annotations.tsv
        raw_dir = out / "RAW"
        raw_dir.mkdir()
        (raw_dir / "raw-annotations.tsv").write_text("query_id\nfoo\n")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="N E X T F L O W  ~  version 24.10.5\n",
                stderr="",
            )
            tsv, ver, cmd = utils._run_nextflow(
                genes_dir=genes, outdir=out, workdir=work,
                databases=("kofam",), threads=1,
            )
        assert "query_id" in tsv
        assert ver == "24.10.5"
        assert "nextflow" in cmd or utils._nextflow_exe in cmd

    def test_missing_output_file_yields_empty_tsv(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        genes = tmp_path / "genes"
        out = tmp_path / "out"
        work = tmp_path / "work"
        genes.mkdir(); out.mkdir(); work.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            tsv, ver, _ = utils._run_nextflow(
                genes_dir=genes, outdir=out, workdir=work,
                databases=(), threads=1,
            )
        assert tsv == ""
        assert ver is None

    def test_nonzero_exit_raises(self, tmp_path: Path):
        utils = _make_available_utils(tmp_path)
        genes = tmp_path / "genes"
        out = tmp_path / "out"
        work = tmp_path / "work"
        genes.mkdir(); out.mkdir(); work.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="boom"
            )
            with pytest.raises(subprocess.CalledProcessError):
                utils._run_nextflow(
                    genes_dir=genes, outdir=out, workdir=work,
                    databases=(), threads=1,
                )


# ---------------------------------------------------------------------------
# Export verification
# ---------------------------------------------------------------------------


class TestDram2Exports:
    def test_dram2_utils_exported(self):
        import kbutillib
        assert hasattr(kbutillib, "DRAM2Utils")
        assert kbutillib.DRAM2Utils is not None

    def test_dram2_utils_is_correct_class(self):
        import kbutillib
        from kbutillib.dram2_utils import DRAM2Utils as DU
        assert kbutillib.DRAM2Utils is DU


# ---------------------------------------------------------------------------
# Live integration test (h100-only)
# ---------------------------------------------------------------------------


def _dram2_live_available() -> bool:
    if os.environ.get("KBU_DRAM2_LIVE") != "1":
        return False
    try:
        utils = DRAM2Utils(
            config_file=False, token_file=None, kbase_token_file=None,
        )
        return utils.is_available()
    except Exception:
        return False


@pytest.mark.integration
@pytest.mark.skipif(
    not _dram2_live_available(),
    reason="DRAM2 live integration requires KBU_DRAM2_LIVE=1 and a reachable install",
)
class TestDram2LiveIntegration:
    """Live integration — runs only on h100 with KBU_DRAM2_LIVE=1."""

    def test_annotate_real_proteins(self):
        # A small real proteome — the FASTA from the golden fixture
        proteins: dict[str, str] = {}
        for record in _GOLDEN_FAA.read_text(encoding="utf-8").split(">"):
            record = record.strip()
            if not record:
                continue
            header, *seq_lines = record.splitlines()
            proteins[header.strip()] = "".join(seq_lines).strip()

        utils = DRAM2Utils(
            config_file=False, token_file=None, kbase_token_file=None,
        )
        result = utils.annotate(proteins, databases=["kofam"], threads=1)
        assert isinstance(result, AnnotationResult)
        assert result.tool == "dram2"
        # Even with zero hits, the structure must be intact
        for rec in result.records:
            assert rec.gene_id in proteins
