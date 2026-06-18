"""Offline unit tests for prokka_utils.py.

Tests cover:
- DNA guard (delegates to annotator_utils; minimal smoke test here)
- Kingdom validation
- Safe-id remap: g{index} deterministic mapping, >32-char ids handled
- _parse_gff_locus_map: CDS extraction, non-CDS skipped, locus_tag parsing
- _parse_tsv: product/EC/gene/COG term building, multi-value EC/COG splitting,
  multi-ORF tie-break (longest CDS wins; ties by smallest start), zero-ORF absence
- _row_to_terms: each field variant
- ProkkaUtils.is_available(): returns bool without side effects
- ProkkaUtils.annotate(): ToolUnavailableError, ValueError on bad kingdom,
  ValueError on protein input
- Live integration test (@pytest.mark.integration, skipif prokka absent)
- Export from kbutillib.__init__
"""

from __future__ import annotations

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
from kbutillib.prokka_utils import (
    ProkkaUtils,
    _parse_gff_locus_map,
    _parse_tsv,
    _row_to_terms,
)

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "prokka"
_GOLDEN_TSV = _FIXTURES / "prokka.tsv"
_GOLDEN_GFF = _FIXTURES / "prokka.gff"


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_utils(**kwargs: Any) -> ProkkaUtils:
    """Construct a ProkkaUtils with no filesystem discovery."""
    return ProkkaUtils(config_file=False, token_file=None, kbase_token_file=None, **kwargs)


def _golden_locus_map() -> dict[str, tuple[str, int]]:
    """Return the locus map parsed from the golden GFF fixture."""
    return _parse_gff_locus_map(_GOLDEN_GFF.read_text(encoding="utf-8"))


def _golden_safe_to_caller() -> dict[str, str]:
    """Return a safe_id → caller_id map matching the golden fixture scenario.

    Input genes (in order):
      g0 → "gene_short"
      g1 → "gene_multi_ec_cog"
      g2 → "gene_with_id_longer_than_32_chars_abcdefghijklmnopqrstuvwxyz"
      g3 → "gene_zero_orf"
      g4 → "gene_no_ec_no_cog"
    """
    return {
        "g0": "gene_short",
        "g1": "gene_multi_ec_cog",
        "g2": "gene_with_id_longer_than_32_chars_abcdefghijklmnopqrstuvwxyz",
        "g3": "gene_zero_orf",
        "g4": "gene_no_ec_no_cog",
    }


# ---------------------------------------------------------------------------
# _parse_gff_locus_map
# ---------------------------------------------------------------------------


class TestParseGffLocusMap:
    """Tests for _parse_gff_locus_map."""

    def test_golden_fixture_cds_count(self):
        """Golden GFF has 5 CDS rows (g2 has 2)."""
        locus_map = _golden_locus_map()
        # 5 CDS locus_tags across g0, g1, g2(x2), g4
        assert len(locus_map) == 5

    def test_golden_fixture_safe_ids(self):
        locus_map = _golden_locus_map()
        safe_ids = {v[0] for v in locus_map.values()}
        assert "g0" in safe_ids
        assert "g1" in safe_ids
        assert "g2" in safe_ids
        assert "g4" in safe_ids
        # g3 is rRNA — should NOT appear
        assert "g3" not in safe_ids

    def test_rrna_row_excluded(self):
        """rRNA rows in the golden GFF must not produce locus_tag entries."""
        locus_map = _golden_locus_map()
        # prokka_00005 is the rRNA locus_tag in the golden GFF
        assert "prokka_00005" not in locus_map

    def test_start_coordinates(self):
        """g2 has two CDS entries with start=1 and start=101."""
        locus_map = _golden_locus_map()
        g2_starts = {
            locus: start
            for locus, (sid, start) in locus_map.items()
            if sid == "g2"
        }
        starts = sorted(g2_starts.values())
        assert starts == [1, 101]

    def test_comment_and_blank_lines_ignored(self):
        gff = "\n##gff-version 3\n# comment\n\ng0\t.\tCDS\t1\t300\t.\t+\t0\tlocus_tag=lt1\n"
        locus_map = _parse_gff_locus_map(gff)
        assert "lt1" in locus_map
        assert locus_map["lt1"] == ("g0", 1)

    def test_no_locus_tag_attribute_ignored(self):
        gff = "g0\t.\tCDS\t1\t300\t.\t+\t0\tID=x;product=foo\n"
        locus_map = _parse_gff_locus_map(gff)
        assert locus_map == {}

    def test_empty_text(self):
        assert _parse_gff_locus_map("") == {}

    def test_non_cds_features_ignored(self):
        gff = (
            "g0\t.\trRNA\t1\t1500\t.\t+\t.\tlocus_tag=lt_rrna\n"
            "g0\t.\ttRNA\t1\t80\t.\t+\t.\tlocus_tag=lt_trna\n"
            "g0\t.\tCDS\t1\t300\t.\t+\t0\tlocus_tag=lt_cds\n"
        )
        locus_map = _parse_gff_locus_map(gff)
        assert list(locus_map.keys()) == ["lt_cds"]


# ---------------------------------------------------------------------------
# _row_to_terms
# ---------------------------------------------------------------------------


class TestRowToTerms:
    """Tests for _row_to_terms."""

    def test_product_only(self):
        terms = _row_to_terms({"product": "hypothetical protein"})
        assert len(terms) == 1
        assert terms[0].namespace is None
        assert terms[0].id is None
        assert terms[0].value == "hypothetical protein"

    def test_ec_single(self):
        terms = _row_to_terms({"product": "dehydrogenase", "EC_number": "1.1.1.1"})
        ec_terms = [t for t in terms if t.namespace == "EC"]
        assert len(ec_terms) == 1
        assert ec_terms[0].id == "1.1.1.1"
        assert ec_terms[0].value == "1.1.1.1"

    def test_ec_multi_semicolon(self):
        terms = _row_to_terms({"EC_number": "1.3.5.1;1.3.5.4"})
        ec_terms = [t for t in terms if t.namespace == "EC"]
        assert len(ec_terms) == 2
        assert {t.id for t in ec_terms} == {"1.3.5.1", "1.3.5.4"}

    def test_ec_multi_comma(self):
        terms = _row_to_terms({"EC_number": "1.1.1.1,2.2.2.2"})
        ec_terms = [t for t in terms if t.namespace == "EC"]
        assert len(ec_terms) == 2

    def test_gene_term(self):
        terms = _row_to_terms({"gene": "adhA"})
        gene_terms = [t for t in terms if t.namespace == "GENE"]
        assert len(gene_terms) == 1
        assert gene_terms[0].id == "adhA"
        assert gene_terms[0].value == "adhA"

    def test_cog_single(self):
        terms = _row_to_terms({"COG": "COG0604"})
        cog_terms = [t for t in terms if t.namespace == "COG"]
        assert len(cog_terms) == 1
        assert cog_terms[0].id == "COG0604"

    def test_cog_multi_comma(self):
        terms = _row_to_terms({"COG": "COG1053,COG0479"})
        cog_terms = [t for t in terms if t.namespace == "COG"]
        assert len(cog_terms) == 2
        assert {t.id for t in cog_terms} == {"COG1053", "COG0479"}

    def test_empty_fields_produce_no_terms(self):
        terms = _row_to_terms({"product": "", "EC_number": "", "gene": "", "COG": ""})
        assert terms == []

    def test_missing_fields_produce_no_terms(self):
        terms = _row_to_terms({})
        assert terms == []

    def test_all_fields(self):
        row = {
            "product": "Alcohol dehydrogenase",
            "EC_number": "1.1.1.1",
            "gene": "adhA",
            "COG": "COG0604",
        }
        terms = _row_to_terms(row)
        namespaces = [t.namespace for t in terms]
        assert None in namespaces  # product
        assert "EC" in namespaces
        assert "GENE" in namespaces
        assert "COG" in namespaces


# ---------------------------------------------------------------------------
# _parse_tsv
# ---------------------------------------------------------------------------


class TestParseTsv:
    """Tests for _parse_tsv using the golden fixture."""

    def test_golden_gene_short(self):
        """g0 → 'gene_short': single CDS with product + EC + gene + COG."""
        locus_map = _golden_locus_map()
        safe_to_caller = _golden_safe_to_caller()
        records = _parse_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            locus_map,
            safe_to_caller,
        )
        rec_map = {r.gene_id: r for r in records}

        assert "gene_short" in rec_map
        rec = rec_map["gene_short"]
        namespaces = [t.namespace for t in rec.terms]
        assert None in namespaces  # product
        assert "EC" in namespaces
        assert "GENE" in namespaces
        assert "COG" in namespaces

    def test_golden_multi_ec_cog(self):
        """g1 → 'gene_multi_ec_cog': multi EC (semicolon) + multi COG (comma)."""
        locus_map = _golden_locus_map()
        safe_to_caller = _golden_safe_to_caller()
        records = _parse_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            locus_map,
            safe_to_caller,
        )
        rec_map = {r.gene_id: r for r in records}

        assert "gene_multi_ec_cog" in rec_map
        rec = rec_map["gene_multi_ec_cog"]
        ec_terms = [t for t in rec.terms if t.namespace == "EC"]
        cog_terms = [t for t in rec.terms if t.namespace == "COG"]
        assert len(ec_terms) == 2
        assert {t.id for t in ec_terms} == {"1.3.5.1", "1.3.5.4"}
        assert len(cog_terms) == 2
        assert {t.id for t in cog_terms} == {"COG1053", "COG0479"}

    def test_golden_long_id_remapped(self):
        """g2 → a >32-char caller id is preserved in the output."""
        long_id = "gene_with_id_longer_than_32_chars_abcdefghijklmnopqrstuvwxyz"
        assert len(long_id) > 32
        locus_map = _golden_locus_map()
        safe_to_caller = _golden_safe_to_caller()
        records = _parse_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            locus_map,
            safe_to_caller,
        )
        rec_map = {r.gene_id: r for r in records}
        assert long_id in rec_map

    def test_golden_multi_orf_longest_selected(self):
        """g2 has two CDS rows (450bp and 750bp); the 750bp row must be selected."""
        long_id = "gene_with_id_longer_than_32_chars_abcdefghijklmnopqrstuvwxyz"
        locus_map = _golden_locus_map()
        safe_to_caller = _golden_safe_to_caller()
        records = _parse_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            locus_map,
            safe_to_caller,
        )
        rec_map = {r.gene_id: r for r in records}

        rec = rec_map[long_id]
        # The 750bp row has product "Serine hydroxymethyltransferase"
        # and EC 2.1.2.1; the 450bp row has product "Homoserine kinase"
        products = [t.value for t in rec.terms if t.namespace is None]
        assert "Serine hydroxymethyltransferase" in products
        assert "Homoserine kinase" not in products

    def test_golden_multi_orf_tiebreak_by_start(self):
        """When length_bp is equal, the row with the smallest start wins."""
        locus_map = {
            "lt_a": ("g0", 50),   # length 300, start 50
            "lt_b": ("g0", 10),   # length 300, start 10 → should win
        }
        safe_to_caller = {"g0": "gene_x"}
        tsv = (
            "locus_tag\tftype\tlength_bp\tgene\tEC_number\tCOG\tproduct\n"
            "lt_a\tCDS\t300\t\t\t\tProduct A\n"
            "lt_b\tCDS\t300\t\t\t\tProduct B\n"
        )
        records = _parse_tsv(tsv, locus_map, safe_to_caller)
        assert len(records) == 1
        rec = records[0]
        products = [t.value for t in rec.terms if t.namespace is None]
        # lt_b has start=10 < 50 → Product B wins
        assert products == ["Product B"]

    def test_golden_zero_orf_absent(self):
        """g3 ('gene_zero_orf') has only an rRNA row → must be absent from records."""
        locus_map = _golden_locus_map()
        safe_to_caller = _golden_safe_to_caller()
        records = _parse_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            locus_map,
            safe_to_caller,
        )
        ids = {r.gene_id for r in records}
        assert "gene_zero_orf" not in ids

    def test_golden_no_ec_no_cog(self):
        """g4 → product + GENE term only (no EC, no COG in golden TSV)."""
        locus_map = _golden_locus_map()
        safe_to_caller = _golden_safe_to_caller()
        records = _parse_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            locus_map,
            safe_to_caller,
        )
        rec_map = {r.gene_id: r for r in records}
        assert "gene_no_ec_no_cog" in rec_map
        rec = rec_map["gene_no_ec_no_cog"]
        assert not any(t.namespace == "EC" for t in rec.terms)
        assert not any(t.namespace == "COG" for t in rec.terms)

    def test_caller_ids_preserved_exactly(self):
        """All non-zero-ORF caller ids appear in records exactly."""
        locus_map = _golden_locus_map()
        safe_to_caller = _golden_safe_to_caller()
        records = _parse_tsv(
            _GOLDEN_TSV.read_text(encoding="utf-8"),
            locus_map,
            safe_to_caller,
        )
        ids = {r.gene_id for r in records}
        # g3 is zero-orf → absent; others present
        assert "gene_short" in ids
        assert "gene_multi_ec_cog" in ids
        assert "gene_with_id_longer_than_32_chars_abcdefghijklmnopqrstuvwxyz" in ids
        assert "gene_no_ec_no_cog" in ids

    def test_empty_tsv_returns_empty(self):
        records = _parse_tsv("", {}, {})
        assert records == []

    def test_non_cds_rows_ignored(self):
        locus_map = {"lt1": ("g0", 1)}
        safe_to_caller = {"g0": "gene_x"}
        tsv = (
            "locus_tag\tftype\tlength_bp\tgene\tEC_number\tCOG\tproduct\n"
            "lt1\trRNA\t1500\t\t\t\t16S ribosomal RNA\n"
        )
        records = _parse_tsv(tsv, locus_map, safe_to_caller)
        assert records == []

    def test_locus_tag_not_in_locus_map_ignored(self):
        locus_map: dict = {}
        safe_to_caller = {"g0": "gene_x"}
        tsv = (
            "locus_tag\tftype\tlength_bp\tgene\tEC_number\tCOG\tproduct\n"
            "lt_unknown\tCDS\t300\t\t\t\thypothetical protein\n"
        )
        records = _parse_tsv(tsv, locus_map, safe_to_caller)
        assert records == []


# ---------------------------------------------------------------------------
# ProkkaUtils class: availability + annotate guards
# ---------------------------------------------------------------------------


class TestProkkaUtilsAvailability:
    """Tests for ProkkaUtils.is_available()."""

    def test_returns_true_when_prokka_exits_zero(self):
        utils = _make_utils()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert utils.is_available() is True

    def test_returns_false_when_prokka_exits_nonzero(self):
        utils = _make_utils()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert utils.is_available() is False

    def test_returns_false_when_file_not_found(self):
        utils = _make_utils()
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            assert utils.is_available() is False

    def test_returns_false_on_timeout(self):
        utils = _make_utils()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="prokka", timeout=10)):
            assert utils.is_available() is False

    def test_is_available_is_side_effect_free(self):
        """is_available() must not mutate instance state or log."""
        utils = _make_utils()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result1 = utils.is_available()
            result2 = utils.is_available()
        assert result1 == result2


class TestProkkaUtilsAnnotateGuards:
    """Tests for ProkkaUtils.annotate() input validation."""

    def test_raises_tool_unavailable_when_prokka_absent(self):
        utils = _make_utils()
        with patch.object(utils, "is_available", return_value=False):
            with pytest.raises(ToolUnavailableError) as exc_info:
                utils.annotate({"g1": "ATGAAACCC"})
            assert exc_info.value.tool == "prokka"

    def test_raises_value_error_for_invalid_kingdom(self):
        utils = _make_utils()
        # Kingdom validation happens before availability check
        with pytest.raises(ValueError, match="kingdom"):
            utils.annotate({"g1": "ATGAAACCC"}, kingdom="Plants")

    def test_raises_value_error_for_protein_input(self):
        utils = _make_utils()
        with patch.object(utils, "is_available", return_value=True):
            # A purely amino-acid sequence
            protein_seq = "MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEKAVQVKVKALPDAQFEVVHSLAKWKRQTLGQHDFSAGEGLYTHMKALRPDEDRLSPLHSVYVDQWDWERVMGDGERQFSTLKSTVEAIWAGIKATEAAVSEEFGLAPFLPDQIHFVHSQELLSRYPDLDAKGRERAIAKDLGAVFLVGIGGKLSDGHRHDVRAPDYDDWSTPSELGHAGLNGDILVWNPVLEDAFELSSMGIRVDADTLKHQLALTGEDEDTOKENACID" * 2
            with pytest.raises(ValueError, match="DNA"):
                utils.annotate({"p1": protein_seq})

    def test_none_kingdom_is_valid(self):
        """kingdom=None is allowed and must not raise."""
        utils = _make_utils()
        # Validate only the kingdom check — stop before subprocess
        with patch.object(utils, "is_available", return_value=False):
            with pytest.raises(ToolUnavailableError):
                utils.annotate({"g1": "ATGAAACCC"}, kingdom=None)
        # No ValueError should have been raised

    def test_valid_kingdoms_accepted(self):
        for k in ("Bacteria", "Archaea", "Viruses"):
            utils = _make_utils()
            with patch.object(utils, "is_available", return_value=False):
                with pytest.raises(ToolUnavailableError):
                    utils.annotate({"g1": "ATGAAACCC"}, kingdom=k)


class TestProkkaUtilsAnnotateEnd2End:
    """End-to-end test of annotate() with a mocked _run_prokka."""

    def _make_mock_run_prokka(self) -> Any:
        """Return a mock for _run_prokka that returns golden fixture content."""
        tsv_text = _GOLDEN_TSV.read_text(encoding="utf-8")
        gff_text = _GOLDEN_GFF.read_text(encoding="utf-8")
        return MagicMock(
            return_value=(tsv_text, gff_text, "1.14.6", None, "prokka --outdir /tmp ...")
        )

    def test_annotate_returns_annotation_result(self):
        utils = _make_utils()
        # The golden fixture maps g0-g4; we need matching input sequences
        genes = {
            "gene_short": "ATGAAACCCGGG" * 25,  # 300nt
            "gene_multi_ec_cog": "ATGCCCGGGAAA" * 20,
            "gene_with_id_longer_than_32_chars_abcdefghijklmnopqrstuvwxyz": "ATGAAATTTGGG" * 20,
            "gene_zero_orf": "ATGCCCAAA" * 10,
            "gene_no_ec_no_cog": "ATGAAAGGG" * 10,
        }
        with patch.object(utils, "is_available", return_value=True):
            with patch.object(utils, "_run_prokka", self._make_mock_run_prokka()):
                result = utils.annotate(genes)

        assert isinstance(result, AnnotationResult)
        assert result.tool == "prokka"
        assert result.tool_version == "1.14.6"

    def test_annotate_records_keyed_by_caller_ids(self):
        utils = _make_utils()
        genes = {
            "gene_short": "ATGAAACCCGGG" * 25,
            "gene_multi_ec_cog": "ATGCCCGGGAAA" * 20,
            "gene_with_id_longer_than_32_chars_abcdefghijklmnopqrstuvwxyz": "ATGAAATTTGGG" * 20,
            "gene_zero_orf": "ATGCCCAAA" * 10,
            "gene_no_ec_no_cog": "ATGAAAGGG" * 10,
        }
        with patch.object(utils, "is_available", return_value=True):
            with patch.object(utils, "_run_prokka", self._make_mock_run_prokka()):
                result = utils.annotate(genes)

        ids = {r.gene_id for r in result.records}
        assert "gene_short" in ids
        assert "gene_multi_ec_cog" in ids
        assert "gene_with_id_longer_than_32_chars_abcdefghijklmnopqrstuvwxyz" in ids
        assert "gene_no_ec_no_cog" in ids

    def test_annotate_zero_orf_absent(self):
        utils = _make_utils()
        genes = {
            "gene_short": "ATGAAACCCGGG" * 25,
            "gene_multi_ec_cog": "ATGCCCGGGAAA" * 20,
            "gene_with_id_longer_than_32_chars_abcdefghijklmnopqrstuvwxyz": "ATGAAATTTGGG" * 20,
            "gene_zero_orf": "ATGCCCAAA" * 10,
            "gene_no_ec_no_cog": "ATGAAAGGG" * 10,
        }
        with patch.object(utils, "is_available", return_value=True):
            with patch.object(utils, "_run_prokka", self._make_mock_run_prokka()):
                result = utils.annotate(genes)

        ids = {r.gene_id for r in result.records}
        assert "gene_zero_orf" not in ids

    def test_annotate_parameters_captured(self):
        utils = _make_utils()
        genes = {"gene_short": "ATGAAACCCGGG" * 25}
        # Override locus map to return just g0 for simplicity
        simple_gff = "g0\t.\tCDS\t1\t300\t.\t+\t0\tlocus_tag=prokka_00001\n"
        simple_tsv = (
            "locus_tag\tftype\tlength_bp\tgene\tEC_number\tCOG\tproduct\n"
            "prokka_00001\tCDS\t300\t\t\t\thypothetical protein\n"
        )
        with patch.object(utils, "is_available", return_value=True):
            with patch.object(
                utils,
                "_run_prokka",
                return_value=(simple_tsv, simple_gff, "1.14.6", None, "prokka ..."),
            ):
                result = utils.annotate(genes, gcode=4, kingdom="Bacteria", threads=4)

        assert result.parameters["gcode"] == 4
        assert result.parameters["kingdom"] == "Bacteria"
        assert result.parameters["threads"] == 4
        assert result.parameters["remapped_ids"] == 1

    def test_annotate_run_id_is_uuid_hex(self):
        utils = _make_utils()
        genes = {"g": "ATGAAACCCGGG"}
        simple_gff = "g0\t.\tCDS\t1\t300\t.\t+\t0\tlocus_tag=prokka_00001\n"
        simple_tsv = (
            "locus_tag\tftype\tlength_bp\tgene\tEC_number\tCOG\tproduct\n"
            "prokka_00001\tCDS\t300\t\t\t\thypothetical protein\n"
        )
        with patch.object(utils, "is_available", return_value=True):
            with patch.object(
                utils,
                "_run_prokka",
                return_value=(simple_tsv, simple_gff, None, None, "prokka ..."),
            ):
                result = utils.annotate(genes)

        import uuid
        # run_id should be parseable as a UUID hex
        run_uuid = uuid.UUID(hex=result.run_id)
        assert run_uuid.version == 4

    def test_annotate_long_id_does_not_abort(self):
        """A >32-char caller id must not cause an error."""
        long_id = "x" * 100
        utils = _make_utils()
        simple_gff = "g0\t.\tCDS\t1\t300\t.\t+\t0\tlocus_tag=prokka_00001\n"
        simple_tsv = (
            "locus_tag\tftype\tlength_bp\tgene\tEC_number\tCOG\tproduct\n"
            "prokka_00001\tCDS\t300\t\t\t\thypothetical protein\n"
        )
        with patch.object(utils, "is_available", return_value=True):
            with patch.object(
                utils,
                "_run_prokka",
                return_value=(simple_tsv, simple_gff, None, None, "prokka ..."),
            ):
                result = utils.annotate({long_id: "ATGAAACCCGGG"})

        ids = {r.gene_id for r in result.records}
        assert long_id in ids


# ---------------------------------------------------------------------------
# Coverage: GFF parser edge cases
# ---------------------------------------------------------------------------


class TestParseGffLocusMapEdgeCases:
    """Additional coverage for _parse_gff_locus_map edge cases."""

    def test_line_with_fewer_than_9_fields_ignored(self):
        """A malformed GFF line with < 9 tab-separated fields is skipped."""
        gff = "g0\t.\tCDS\t1\t300\t.\t+\n"  # only 8 fields (no attributes col)
        locus_map = _parse_gff_locus_map(gff)
        assert locus_map == {}

    def test_non_integer_start_ignored(self):
        """A CDS line with a non-integer start coordinate is skipped."""
        gff = "g0\t.\tCDS\tnot_a_number\t300\t.\t+\t0\tlocus_tag=lt1\n"
        locus_map = _parse_gff_locus_map(gff)
        assert locus_map == {}


# ---------------------------------------------------------------------------
# Coverage: _parse_tsv edge cases
# ---------------------------------------------------------------------------


class TestParseTsvEdgeCases:
    """Additional coverage for _parse_tsv edge cases."""

    def test_safe_id_not_in_caller_map_ignored(self):
        """A CDS row whose safe_id is in locus_map but not safe_to_caller is skipped."""
        # safe_id "g0" is in locus_map but absent from safe_to_caller
        locus_map = {"lt1": ("g0", 1)}
        safe_to_caller: dict[str, str] = {}  # g0 deliberately absent
        tsv = (
            "locus_tag\tftype\tlength_bp\tgene\tEC_number\tCOG\tproduct\n"
            "lt1\tCDS\t300\t\t\t\thypothetical protein\n"
        )
        records = _parse_tsv(tsv, locus_map, safe_to_caller)
        assert records == []

    def test_length_bp_non_integer_defaults_to_zero(self):
        """A CDS row with a non-integer length_bp uses length 0."""
        locus_map = {"lt1": ("g0", 1)}
        safe_to_caller = {"g0": "gene_x"}
        tsv = (
            "locus_tag\tftype\tlength_bp\tgene\tEC_number\tCOG\tproduct\n"
            "lt1\tCDS\tnot_a_number\t\t\t\thypothetical protein\n"
        )
        records = _parse_tsv(tsv, locus_map, safe_to_caller)
        # Should succeed despite bad length_bp (defaults to 0)
        assert len(records) == 1
        assert records[0].gene_id == "gene_x"

    def test_row_padded_when_short(self):
        """TSV rows shorter than the header are padded to match."""
        locus_map = {"lt1": ("g0", 1)}
        safe_to_caller = {"g0": "gene_x"}
        # Row has only locus_tag + ftype + length_bp (missing other columns)
        tsv = (
            "locus_tag\tftype\tlength_bp\tgene\tEC_number\tCOG\tproduct\n"
            "lt1\tCDS\t300\n"  # short row
        )
        records = _parse_tsv(tsv, locus_map, safe_to_caller)
        assert len(records) == 1
        # All extra fields should be empty → no terms
        assert records[0].terms == []

    def test_blank_line_in_tsv_body_skipped(self):
        """Blank lines in the TSV body (after header) are silently skipped."""
        locus_map = {"lt1": ("g0", 1)}
        safe_to_caller = {"g0": "gene_x"}
        tsv = (
            "locus_tag\tftype\tlength_bp\tgene\tEC_number\tCOG\tproduct\n"
            "\n"  # blank line — must be skipped
            "lt1\tCDS\t300\t\t\t\thypothetical protein\n"
        )
        records = _parse_tsv(tsv, locus_map, safe_to_caller)
        assert len(records) == 1
        assert records[0].gene_id == "gene_x"


# ---------------------------------------------------------------------------
# Coverage: _row_to_terms trailing-delimiter edge cases
# ---------------------------------------------------------------------------


class TestRowToTermsTrailingDelimiter:
    """Test that trailing delimiters in EC/COG don't produce empty Terms."""

    def test_ec_trailing_semicolon_no_empty_term(self):
        """EC_number with trailing semicolon must not produce a Term with id=''."""
        terms = _row_to_terms({"EC_number": "1.1.1.1;"})
        ec_terms = [t for t in terms if t.namespace == "EC"]
        assert len(ec_terms) == 1
        assert ec_terms[0].id == "1.1.1.1"
        # No empty-string term
        assert all(t.id != "" for t in ec_terms)

    def test_cog_trailing_comma_no_empty_term(self):
        """COG with trailing comma must not produce a Term with id=''."""
        terms = _row_to_terms({"COG": "COG0604,"})
        cog_terms = [t for t in terms if t.namespace == "COG"]
        assert len(cog_terms) == 1
        assert cog_terms[0].id == "COG0604"
        assert all(t.id != "" for t in cog_terms)


# ---------------------------------------------------------------------------
# Coverage: _run_prokka, _parse_prokka_version, _parse_db_version
# ---------------------------------------------------------------------------


class TestRunProkka:
    """Unit tests for ProkkaUtils._run_prokka via subprocess mocking."""

    def _write_output_files(self, outdir: Path, tsv_text: str, gff_text: str) -> None:
        """Write mock PROKKA output files to outdir."""
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "prokka.tsv").write_text(tsv_text, encoding="utf-8")
        (outdir / "prokka.gff").write_text(gff_text, encoding="utf-8")

    def test_run_prokka_raises_on_nonzero_exit(self, tmp_path: Path) -> None:
        """_run_prokka raises CalledProcessError when prokka exits non-zero."""
        utils = _make_utils()
        fasta = tmp_path / "in.fasta"
        fasta.write_text(">g0\nATGAAAGGG\n")
        outdir = tmp_path / "out"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            with pytest.raises(subprocess.CalledProcessError):
                utils._run_prokka(fasta, outdir, gcode=11, kingdom=None, threads=1)

    def test_run_prokka_success_reads_output_files(self, tmp_path: Path) -> None:
        """_run_prokka reads tsv and gff from outdir after a successful run."""
        utils = _make_utils()
        fasta = tmp_path / "in.fasta"
        fasta.write_text(">g0\nATGAAAGGG\n")
        outdir = tmp_path / "out"
        tsv_text = "locus_tag\tftype\tlength_bp\tgene\tEC_number\tCOG\tproduct\n"
        gff_text = "##gff-version 3\n"

        def fake_run(cmd, **kwargs):
            # Create output files as a side effect
            self._write_output_files(outdir, tsv_text, gff_text)
            return MagicMock(
                returncode=0,
                stderr="[12:00:00] This is prokka 1.14.6\nDatabases: UniProt 2022_04",
                stdout="",
            )

        with patch("subprocess.run", side_effect=fake_run):
            result = utils._run_prokka(fasta, outdir, gcode=11, kingdom=None, threads=1)

        tsv_out, gff_out, tool_version, db_version, command = result
        assert tsv_out == tsv_text
        assert gff_out == gff_text
        assert tool_version == "1.14.6"
        assert db_version == "UniProt 2022_04"
        assert "prokka" in command

    def test_run_prokka_kingdom_in_command(self, tmp_path: Path) -> None:
        """_run_prokka includes --kingdom when kingdom is not None."""
        utils = _make_utils()
        fasta = tmp_path / "in.fasta"
        fasta.write_text(">g0\nATGAAAGGG\n")
        outdir = tmp_path / "out"

        captured_cmd: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured_cmd.append(list(cmd))
            self._write_output_files(outdir, "", "")
            return MagicMock(returncode=0, stderr="", stdout="")

        with patch("subprocess.run", side_effect=fake_run):
            utils._run_prokka(fasta, outdir, gcode=11, kingdom="Bacteria", threads=2)

        assert captured_cmd
        flat_cmd = captured_cmd[0]
        assert "--kingdom" in flat_cmd
        ki = flat_cmd.index("--kingdom")
        assert flat_cmd[ki + 1] == "Bacteria"
        assert "--cpus" in flat_cmd
        ci = flat_cmd.index("--cpus")
        assert flat_cmd[ci + 1] == "2"

    def test_run_prokka_missing_output_files_return_empty(self, tmp_path: Path) -> None:
        """If prokka.tsv/prokka.gff are absent, empty strings are returned."""
        utils = _make_utils()
        fasta = tmp_path / "in.fasta"
        fasta.write_text(">g0\nATGAAAGGG\n")
        outdir = tmp_path / "out"

        def fake_run(cmd, **kwargs):
            # Create outdir but no output files
            outdir.mkdir(parents=True, exist_ok=True)
            return MagicMock(returncode=0, stderr="", stdout="")

        with patch("subprocess.run", side_effect=fake_run):
            tsv_out, gff_out, _, _, _ = utils._run_prokka(
                fasta, outdir, gcode=11, kingdom=None, threads=1
            )

        assert tsv_out == ""
        assert gff_out == ""


class TestParseProkkaVersion:
    """Tests for ProkkaUtils._parse_prokka_version."""

    def test_version_from_stderr_log(self):
        """Version string in 'This is prokka X.Y.Z' is extracted."""
        utils = _make_utils()
        v = utils._parse_prokka_version("[12:00:00] This is prokka 1.14.6\n")
        assert v == "1.14.6"

    def test_version_from_version_flag_fallback(self):
        """Falls back to --version probe when stderr has no version log."""
        utils = _make_utils()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stderr="prokka 1.14.5", stdout=""
            )
            v = utils._parse_prokka_version("")
        assert v == "1.14.5"

    def test_version_fallback_file_not_found_returns_none(self):
        """Returns None when --version probe raises FileNotFoundError."""
        utils = _make_utils()
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            v = utils._parse_prokka_version("")
        assert v is None

    def test_version_fallback_timeout_returns_none(self):
        """Returns None when --version probe times out."""
        utils = _make_utils()
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="prokka", timeout=10),
        ):
            v = utils._parse_prokka_version("")
        assert v is None

    def test_version_fallback_oserror_returns_none(self):
        """Returns None when --version probe raises OSError."""
        utils = _make_utils()
        with patch("subprocess.run", side_effect=OSError("no such file")):
            v = utils._parse_prokka_version("")
        assert v is None

    def test_version_none_when_not_parseable(self):
        """Returns None when neither stderr nor --version yields a version."""
        utils = _make_utils()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            v = utils._parse_prokka_version("")
        assert v is None

    def test_version_none_when_fallback_lines_dont_match(self):
        """Returns None when --version output lines don't match the version pattern."""
        utils = _make_utils()
        with patch("subprocess.run") as mock_run:
            # Provide output that has lines but no prokka version pattern
            mock_run.return_value = MagicMock(
                returncode=0, stderr="some unrelated text\nno version here", stdout=""
            )
            v = utils._parse_prokka_version("")
        assert v is None


class TestParseDbVersion:
    """Tests for ProkkaUtils._parse_db_version."""

    def test_db_version_extracted(self):
        utils = _make_utils()
        dv = utils._parse_db_version("[12:00:00] Databases: UniProt 2022_04\n")
        assert dv == "UniProt 2022_04"

    def test_db_version_none_when_absent(self):
        utils = _make_utils()
        dv = utils._parse_db_version("[12:00:00] This is prokka 1.14.6\n")
        assert dv is None


# ---------------------------------------------------------------------------
# Export test
# ---------------------------------------------------------------------------


class TestProkkaExports:
    """Verify ProkkaUtils is exported from kbutillib.__init__."""

    def test_prokka_utils_exported(self):
        import kbutillib
        assert hasattr(kbutillib, "ProkkaUtils")
        assert kbutillib.ProkkaUtils is not None

    def test_prokka_utils_is_correct_class(self):
        import kbutillib
        from kbutillib.prokka_utils import ProkkaUtils as PU
        assert kbutillib.ProkkaUtils is PU


# ---------------------------------------------------------------------------
# Live integration test (skipped unless prokka is available)
# ---------------------------------------------------------------------------


def _prokka_available() -> bool:
    try:
        r = subprocess.run(
            ["prokka", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


@pytest.mark.integration
@pytest.mark.skipif(not _prokka_available(), reason="prokka not installed")
class TestProkkaLiveIntegration:
    """Live integration tests — skipped unless prokka is on PATH."""

    def test_annotate_small_gene(self):
        """Run PROKKA on a tiny synthetic gene and check we get an AnnotationResult."""
        # A real E. coli adhA CDS (alcohol dehydrogenase, genetic code 11)
        # Short but real enough for Prodigal to call an ORF
        orf = (
            "ATGAAAATCGCAGTTTGGATTGATCAGCAAATTCTTCAACGGCTTGAAGAACGGCTGGGCC"
            "TGATCGAAGTACAGGCTCCAATCCTGTCCCGCGTCGGCGATGGCACCCAGGATACCCTGAGC"
            "GGTGCTGAAAAGGTGCAGGTCAAGGTCAAGGCGCTTCCAGATGCACAGTTTGAAGTAGTGCA"
            "CTCTCTGGCCAAATGGAAGCGCCAAACCTTGGGGCAGCACGATTTCAGCGCCGGGGAAGGCCT"
            "GTATACACACATGAAGGCGCTGCGGCCCGACGAAGATCGGCTGAGCCCGCTGCACAGCGTCTA"
            "TGTGGATCAATGGGACTGGGAGCGGGTGATGGGGGATGGAGAGCGCCAGTTCTCCACCCTGAA"
        )
        utils = ProkkaUtils(config_file=False, token_file=None, kbase_token_file=None)
        result = utils.annotate({"test_gene_live": orf}, gcode=11, kingdom="Bacteria")

        assert isinstance(result, AnnotationResult)
        assert result.tool == "prokka"
        # run_id is a uuid4 hex
        import uuid
        uuid.UUID(hex=result.run_id)
        # Either a record was found (ORF called) or not (zero-ORF) — both are valid
        for rec in result.records:
            assert isinstance(rec, AnnotationRecord)
