"""Offline unit tests for kofamscan_utils.py.

Test strategy
-------------
The Docker/subprocess-dependent code paths are exercised only with
``subprocess.run`` mocked out. The pure parse/bridge helpers
(``_parse_kofam_detail_tsv``, ``_bridge_ko``, ``_build_kofam_records``,
``_parse_ko_function_map_text``, ``_load_ko_function_map``) are tested fully
offline against small inline fixtures — none of them depend on any generated
artifact (the real ``ko_function_map.tsv`` / guard profile set are built by a
sibling task and are not required here).
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kbutillib.domains.genome.annotation.annotator_utils import (
    AnnotationResult,
    ToolUnavailableError,
)
from kbutillib.domains.genome.annotation.kofamscan_utils import (
    KofamscanUtils,
    _bridge_ko,
    _build_kofam_records,
    _load_ko_function_map,
    _parse_ko_function_map_text,
    _parse_kofam_detail_tsv,
)


def _make_utils(
    profiles_path: str = "/fake/profiles",
    profile_set: str = "2025-11-03",
    **kwargs: Any,
) -> KofamscanUtils:
    """Create KofamscanUtils with no filesystem discovery."""
    return KofamscanUtils(
        profiles_path=profiles_path,
        profile_set=profile_set,
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# is_available()
# ---------------------------------------------------------------------------


class TestIsAvailable:
    def test_docker_mode_true_when_image_present(self):
        ku = _make_utils()
        ku._docker_image = "kbutillib/kofamscan:latest"
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            assert ku.is_available() is True

    def test_docker_mode_false_when_image_absent(self):
        ku = _make_utils()
        ku._docker_image = "kbutillib/kofamscan:latest"
        with patch("subprocess.run", return_value=MagicMock(returncode=1)):
            assert ku.is_available() is False

    def test_native_mode_true_when_absolute_executable_exists(self, tmp_path):
        exe = tmp_path / "exec_annotation"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        ku = _make_utils()
        ku._kofamscan_exe = str(exe)
        assert ku.is_available() is True

    def test_native_mode_false_when_absolute_executable_missing(self, tmp_path):
        ku = _make_utils()
        ku._kofamscan_exe = str(tmp_path / "nope")
        assert ku.is_available() is False

    def test_native_mode_uses_which_for_bare_name(self):
        ku = _make_utils()
        ku._kofamscan_exe = "exec_annotation"
        with patch("shutil.which", return_value=None):
            assert ku.is_available() is False
        with patch("shutil.which", return_value="/usr/bin/exec_annotation"):
            assert ku.is_available() is True


# ---------------------------------------------------------------------------
# annotate() — validation before the tool is touched
# ---------------------------------------------------------------------------


class TestAnnotateValidation:
    def test_raises_tool_unavailable_when_absent(self):
        ku = _make_utils()
        with patch.object(ku, "is_available", return_value=False):
            with pytest.raises(ToolUnavailableError):
                ku.annotate(proteins={"p1": "MKTAY"})

    def test_raises_on_nucleotide_input(self):
        ku = _make_utils()
        with patch.object(ku, "is_available", return_value=True):
            nuc = "U" * 100
            with pytest.raises(ValueError, match="protein"):
                ku.annotate(proteins={"g1": nuc})


# ---------------------------------------------------------------------------
# _parse_kofam_detail_tsv — the mandatory significance filter
# ---------------------------------------------------------------------------


class TestParseKofamDetailTsv:
    _HEADER = "# gene name\tKO\tthrshld\tscore\tE-value\tKO definition"

    def test_significant_row_is_kept(self):
        text = "\n".join(
            [
                self._HEADER,
                "*\tprot1\tK00001\t329.85\t356.50\t1.4e-105\talcohol dehydrogenase",
            ]
        )
        rows = _parse_kofam_detail_tsv(text)
        assert len(rows) == 1
        assert rows[0] == {
            "gene_id": "prot1",
            "ko_id": "K00001",
            "thrshld": "329.85",
            "score": "356.50",
            "evalue": "1.4e-105",
            "definition": "alcohol dehydrogenase",
        }

    def test_below_threshold_row_is_excluded(self):
        """Rows without the '*' significance flag must never become calls."""
        text = "\n".join(
            [
                self._HEADER,
                "*\tprot1\tK00001\t329.85\t356.50\t1.4e-105\tsignificant hit",
                "\tprot1\tK00099\t250.00\t12.30\t5.0\tbelow threshold hit",
            ]
        )
        rows = _parse_kofam_detail_tsv(text)
        assert len(rows) == 1
        assert rows[0]["ko_id"] == "K00001"
        assert all(r["ko_id"] != "K00099" for r in rows)

    def test_multimap_gene_with_several_significant_hits(self):
        """A gene with N significant hits yields N rows, not just the last."""
        text = "\n".join(
            [
                self._HEADER,
                "*\tprot1\tK00001\t1\t1\t1e-10\tdef1",
                "*\tprot1\tK00002\t1\t1\t1e-20\tdef2",
                "*\tprot1\tK00003\t1\t1\t1e-30\tdef3",
            ]
        )
        rows = _parse_kofam_detail_tsv(text)
        assert [r["ko_id"] for r in rows] == ["K00001", "K00002", "K00003"]

    def test_blank_and_comment_lines_ignored(self):
        text = "\n".join(
            [
                self._HEADER,
                "",
                "# another comment",
                "*\tprot1\tK00001\t1\t1\t1\tdef",
            ]
        )
        assert len(_parse_kofam_detail_tsv(text)) == 1

    def test_short_rows_skipped(self):
        text = "\n".join([self._HEADER, "*\tprot1\tK00001"])
        assert _parse_kofam_detail_tsv(text) == []

    def test_empty_text_returns_empty(self):
        assert _parse_kofam_detail_tsv("") == []


# ---------------------------------------------------------------------------
# _bridge_ko — the pure lookup function
# ---------------------------------------------------------------------------


class TestBridgeKo:
    """Injected in-memory table — no dependency on any generated artifact."""

    _TABLE = {
        # Multi-symbol prefix (several gene-symbol aliases before the ';').
        "K00001": "e1.1.1.1, adh; alcohol dehydrogenase",
        # An [EC:...] block retained verbatim in the composed definition.
        "K00003": "hom; homoserine dehydrogenase [EC:1.1.1.3]",
        # Ordinary single-symbol entry.
        "K00002": "aldh; aldehyde dehydrogenase",
    }

    def test_multi_symbol_prefix_bridges(self):
        assert _bridge_ko("K00001", self._TABLE) == (
            "e1.1.1.1, adh; alcohol dehydrogenase"
        )

    def test_ec_block_bridges(self):
        result = _bridge_ko("K00003", self._TABLE)
        assert result == "hom; homoserine dehydrogenase [EC:1.1.1.3]"

    def test_non_bridging_ko_returns_none(self):
        assert _bridge_ko("K99999", self._TABLE) is None

    def test_ordinary_entry_bridges(self):
        assert _bridge_ko("K00002", self._TABLE) == "aldh; aldehyde dehydrogenase"

    def test_empty_table_returns_none(self):
        assert _bridge_ko("K00001", {}) is None


# ---------------------------------------------------------------------------
# _build_kofam_records — the D5 pair, the multimap, and bridged_fraction
# ---------------------------------------------------------------------------


class TestBuildKofamRecords:
    def test_bridgeable_hit_emits_id_then_bridged_text_pair(self):
        rows = [
            {
                "gene_id": "g1",
                "ko_id": "K00001",
                "thrshld": "1",
                "score": "1",
                "evalue": "1e-10",
                "definition": "d",
            }
        ]
        table = {"K00001": "adh; alcohol dehydrogenase"}
        records, fraction = _build_kofam_records(rows, table)
        assert len(records) == 1
        terms = records[0].terms
        # Compared as a multiset, not by position — ordering carries no
        # semantics (D5's confront-resolved decision).
        values = {t.value for t in terms}
        assert values == {"K00001", "adh; alcohol dehydrogenase"}
        assert fraction == 1.0

    def test_non_bridging_ko_degrades_to_id_only(self):
        rows = [
            {
                "gene_id": "g1",
                "ko_id": "K99999",
                "thrshld": "1",
                "score": "1",
                "evalue": "1",
                "definition": "d",
            }
        ]
        records, fraction = _build_kofam_records(rows, {})
        assert len(records[0].terms) == 1
        assert records[0].terms[0].value == "K99999"
        assert fraction == 0.0

    def test_multimap_gene_yields_term_per_hit(self):
        """A gene with several significant hits yields terms for every hit."""
        rows = [
            {"gene_id": "g1", "ko_id": f"K0000{i}", "thrshld": "1", "score": "1",
             "evalue": "1", "definition": "d"}
            for i in range(1, 4)
        ]
        records, _fraction = _build_kofam_records(rows, {})
        assert len(records) == 1
        ko_terms = [t for t in records[0].terms if t.namespace == "KO"]
        assert {t.id for t in ko_terms} == {"K00001", "K00002", "K00003"}

    def test_evidence_carries_score_thrshld_evalue(self):
        rows = [
            {
                "gene_id": "g1",
                "ko_id": "K00001",
                "thrshld": "329.85",
                "score": "356.50",
                "evalue": "1.4e-105",
                "definition": "d",
            }
        ]
        records, _fraction = _build_kofam_records(rows, {})
        ev = records[0].terms[0].evidence
        assert ev["thrshld"] == "329.85"
        assert ev["score"] == "356.50"
        assert ev["evalue"] == "1.4e-105"

    def test_table_none_forces_bridged_fraction_zero(self):
        rows = [
            {"gene_id": "g1", "ko_id": "K00001", "thrshld": "1", "score": "1",
             "evalue": "1", "definition": "d"}
        ]
        # Even though a table WOULD bridge this KO, table=None (absent) must
        # force 0.0, never a computed fraction.
        _records, fraction = _build_kofam_records(rows, None)
        assert fraction == 0.0

    def test_table_none_emits_id_only(self):
        rows = [
            {"gene_id": "g1", "ko_id": "K00001", "thrshld": "1", "score": "1",
             "evalue": "1", "definition": "d"}
        ]
        records, _fraction = _build_kofam_records(rows, None)
        assert len(records[0].terms) == 1
        assert records[0].terms[0].value == "K00001"

    def test_bridged_fraction_is_over_distinct_kos_not_hits(self):
        # Same KO called on two genes: 1 distinct KO, bridged.
        rows = [
            {"gene_id": "g1", "ko_id": "K00001", "thrshld": "1", "score": "1",
             "evalue": "1", "definition": "d"},
            {"gene_id": "g2", "ko_id": "K00001", "thrshld": "1", "score": "1",
             "evalue": "1", "definition": "d"},
        ]
        _records, fraction = _build_kofam_records(rows, {"K00001": "bridged"})
        assert fraction == 1.0

    def test_input_ids_filter(self):
        rows = [
            {"gene_id": "g1", "ko_id": "K00001", "thrshld": "1", "score": "1",
             "evalue": "1", "definition": "d"},
            {"gene_id": "stray", "ko_id": "K00002", "thrshld": "1", "score": "1",
             "evalue": "1", "definition": "d"},
        ]
        records, _fraction = _build_kofam_records(rows, {}, input_ids={"g1"})
        assert {r.gene_id for r in records} == {"g1"}

    def test_empty_rows_returns_empty(self):
        records, fraction = _build_kofam_records([], {})
        assert records == []
        assert fraction == 0.0


# ---------------------------------------------------------------------------
# ko_function_map loading
# ---------------------------------------------------------------------------


class TestParseKoFunctionMapText:
    def test_parses_data_rows(self):
        text = "K00001\tadh; alcohol dehydrogenase\nK00002\taldh; aldehyde dehydrogenase\n"
        table, _meta = _parse_ko_function_map_text(text)
        assert table["K00001"] == "adh; alcohol dehydrogenase"
        assert table["K00002"] == "aldh; aldehyde dehydrogenase"

    def test_parses_header_metadata(self):
        text = "\n".join(
            [
                "# kegg_release: 109.1",
                "# ko_list_version: 2025-11-03",
                "# generated: 2026-08-24",
                "K00001\tadh; alcohol dehydrogenase",
            ]
        )
        table, meta = _parse_ko_function_map_text(text)
        assert meta["kegg_release"] == "109.1"
        assert meta["ko_list_version"] == "2025-11-03"
        assert len(table) == 1

    def test_malformed_rows_skipped(self):
        text = "not_a_valid_row_no_tab\nK00001\tvalid\n"
        table, _meta = _parse_ko_function_map_text(text)
        assert table == {"K00001": "valid"}

    def test_empty_text_returns_empty(self):
        table, meta = _parse_ko_function_map_text("")
        assert table == {}
        assert meta == {}


class TestLoadKoFunctionMap:
    def test_empty_path_returns_none(self):
        assert _load_ko_function_map("") is None

    def test_missing_file_returns_none(self, tmp_path):
        assert _load_ko_function_map(str(tmp_path / "nope.tsv")) is None

    def test_valid_file_loads(self, tmp_path):
        f = tmp_path / "ko_function_map.tsv"
        f.write_text("K00001\tadh; alcohol dehydrogenase\n", encoding="utf-8")
        loaded = _load_ko_function_map(str(f))
        assert loaded is not None
        table, _meta = loaded
        assert table["K00001"] == "adh; alcohol dehydrogenase"


# ---------------------------------------------------------------------------
# _run_kofamscan — command shape (native + docker)
# ---------------------------------------------------------------------------


class TestRunKofamscanCommandShape:
    def _fake_completed(self, returncode=0, stdout="", stderr=""):
        return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_native_command_uses_detail_tsv_not_mapper(self, tmp_path):
        ku = _make_utils(profiles_path=str(tmp_path), profile_set="2025-11-03")
        fasta = tmp_path / "input.faa"
        fasta.write_text(">g1\nMKTAY\n")
        scan_tmp = tmp_path / "scan_tmp"
        scan_tmp.mkdir()
        out_path = tmp_path / "out.tsv"

        with patch("subprocess.run", return_value=self._fake_completed()) as mock_run:
            ku._run_kofamscan(
                fasta_path=fasta,
                out_path=out_path,
                scan_tmp_dir=scan_tmp,
                resolved_profiles=str(tmp_path),
                threads=2,
            )

        argv = mock_run.call_args[0][0]
        assert "-f" in argv
        assert argv[argv.index("-f") + 1] == "detail-tsv"
        assert "mapper" not in argv
        assert "--cpu" in argv and "2" in argv
        assert f"{tmp_path}/profiles/2025-11-03" in argv
        assert f"{tmp_path}/profiles/2025-11-03.txt" in argv

    def test_docker_command_has_user_network_none_and_ro_mount(self, tmp_path):
        ku = _make_utils(profiles_path=str(tmp_path), profile_set="2025-11-03")
        ku._docker_image = "kbutillib/kofamscan:latest"
        fasta = tmp_path / "input.faa"
        fasta.write_text(">g1\nMKTAY\n")
        scan_tmp = tmp_path / "scan_tmp"
        scan_tmp.mkdir()
        out_path = tmp_path / "out.tsv"

        with patch("subprocess.run", return_value=self._fake_completed()) as mock_run:
            ku._run_kofamscan(
                fasta_path=fasta,
                out_path=out_path,
                scan_tmp_dir=scan_tmp,
                resolved_profiles=str(tmp_path),
                threads=1,
            )

        argv = mock_run.call_args[0][0]
        assert argv[:2] == ["docker", "run"]
        assert "--user" in argv
        assert "--network" in argv
        assert argv[argv.index("--network") + 1] == "none"
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        assert any(m.endswith(":/profiles:ro") for m in mounts)

    def test_nonzero_exit_raises_called_process_error(self, tmp_path):
        ku = _make_utils(profiles_path=str(tmp_path), profile_set="2025-11-03")
        fasta = tmp_path / "input.faa"
        fasta.write_text(">g1\nMKTAY\n")
        scan_tmp = tmp_path / "scan_tmp"
        scan_tmp.mkdir()
        out_path = tmp_path / "out.tsv"
        with patch(
            "subprocess.run",
            return_value=self._fake_completed(returncode=1, stderr="boom"),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                ku._run_kofamscan(
                    fasta_path=fasta,
                    out_path=out_path,
                    scan_tmp_dir=scan_tmp,
                    resolved_profiles=str(tmp_path),
                    threads=1,
                )


# ---------------------------------------------------------------------------
# annotate() — full path, subprocess mocked
# ---------------------------------------------------------------------------


class TestAnnotateMocked:
    _DETAIL_TSV = "\n".join(
        [
            "# gene name\tKO\tthrshld\tscore\tE-value\tKO definition",
            "*\tgene1\tK00001\t1\t1\t1e-10\tdef1",
            "*\tgene1\tK00002\t1\t1\t1e-20\tdef2",
            "\tgene1\tK99999\t1\t0.001\t5.0\tbelow threshold",
        ]
    )

    def _fake_run_kofamscan(
        self, fasta_path, out_path, scan_tmp_dir, resolved_profiles, threads
    ):
        return self._DETAIL_TSV, "exec_annotation ..."

    def test_returns_annotation_result_with_multimap_and_filter(self):
        ku = _make_utils()
        with patch.object(ku, "is_available", return_value=True), patch.object(
            ku, "_run_kofamscan", side_effect=self._fake_run_kofamscan
        ):
            result = ku.annotate(proteins={"gene1": "MKTAY"})

        assert isinstance(result, AnnotationResult)
        assert result.tool == "kofamscan"
        rec = next(r for r in result.records if r.gene_id == "gene1")
        ko_ids = {t.id for t in rec.terms if t.namespace == "KO"}
        # Both significant hits present; the below-threshold hit excluded.
        assert ko_ids == {"K00001", "K00002"}

    def test_absent_ko_function_map_yields_ko_ids_only(self):
        ku = _make_utils()
        assert ku._ko_function_map_path == ""
        with patch.object(ku, "is_available", return_value=True), patch.object(
            ku, "_run_kofamscan", side_effect=self._fake_run_kofamscan
        ):
            result = ku.annotate(proteins={"gene1": "MKTAY"})

        rec = next(r for r in result.records if r.gene_id == "gene1")
        assert all(t.namespace == "KO" for t in rec.terms)
        assert result.parameters["bridged_fraction"] == 0.0
        assert result.parameters["ko_function_map"] == "absent"

    def test_configured_but_missing_file_also_degrades_not_fails(self, tmp_path):
        ku = _make_utils(config={"kofamscan": {"ko_function_map": str(tmp_path / "nope.tsv")}})
        with patch.object(ku, "is_available", return_value=True), patch.object(
            ku, "_run_kofamscan", side_effect=self._fake_run_kofamscan
        ):
            result = ku.annotate(proteins={"gene1": "MKTAY"})

        assert result.parameters["bridged_fraction"] == 0.0
        assert result.parameters["ko_function_map"] == "absent"

    def test_present_table_bridges_and_records_versions(self, tmp_path):
        table_path = tmp_path / "ko_function_map.tsv"
        table_path.write_text(
            "\n".join(
                [
                    "# kegg_release: 109.1",
                    "K00001\tadh; alcohol dehydrogenase",
                ]
            ),
            encoding="utf-8",
        )
        ku = _make_utils(
            profile_set="2025-11-03",
            config={"kofamscan": {"ko_function_map": str(table_path)}},
        )
        with patch.object(ku, "is_available", return_value=True), patch.object(
            ku, "_run_kofamscan", side_effect=self._fake_run_kofamscan
        ):
            result = ku.annotate(proteins={"gene1": "MKTAY"})

        assert "ko_function_map" not in result.parameters
        assert result.parameters["kegg_release"] == "109.1"
        assert result.parameters["profile_set"] == "2025-11-03"
        assert result.parameters["ko_list_version"] == "2025-11-03"
        rec = next(r for r in result.records if r.gene_id == "gene1")
        values = {t.value for t in rec.terms}
        assert "adh; alcohol dehydrogenase" in values


# ---------------------------------------------------------------------------
# Package export
# ---------------------------------------------------------------------------


class TestPackageExport:
    def test_exported_from_annotation_package(self):
        from kbutillib.domains.genome.annotation import (
            KofamscanUtils as ReExported,
        )

        assert ReExported is KofamscanUtils
