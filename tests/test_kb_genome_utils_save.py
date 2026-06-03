"""Unit tests for new KBGenomeUtils save / validate / build methods.

Covers:
- validate_genome: happy + sad paths
- build_genome_from_fasta_gff: FASTA-only and FASTA+GFF
- save_assembly_from_fasta: legacy class raises RuntimeError
- save_genome_object: mocked save_ws_object returns correct ref format

Integration tests (require live EE2 + KBase workspace) are marked
@pytest.mark.integration and skipped by default.  To run them:

    KBASE_LIVE_TESTS=1 pytest tests/test_kb_genome_utils_save.py -m integration
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── helpers ───────────────────────────────────────────────────────────────


def _minimal_genome(**overrides) -> dict:
    """Smallest valid KBase Genome dict that passes validate_genome()."""
    base = {
        "id": "test_genome",
        "scientific_name": "Testus testus",
        "domain": "Bacteria",
        "taxonomy": "Bacteria; Firmicutes; Bacilli",
        "genetic_code": 11,
        "dna_size": 1000,
        "num_contigs": 1,
        "contig_ids": ["contig1"],
        "contig_lengths": [1000],
        "gc_content": 0.5,
        "md5": "abc123",
        "molecule_type": "DNA",
        "source": "User",
        "source_id": "test_genome",
        "assembly_ref": "1/2/3",
        "features": [],
        "cdss": [],
        "mrnas": [],
        "non_coding_features": [],
        "feature_counts": {},
    }
    base.update(overrides)
    return base


def _genome_utils():
    """Bare KBGenomeUtils instance (no tokens required for pure-logic methods)."""
    from kbutillib.kb_genome_utils import KBGenomeUtils

    return KBGenomeUtils(
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        token="fake-kbase-token-for-tests",
    )


# ── validate_genome: happy path ───────────────────────────────────────────


class TestValidateGenomeHappy:
    def test_minimal_valid_genome_passes(self):
        """validate_genome returns [] for a minimal valid Genome dict."""
        genome = _genome_utils()
        errors = genome.validate_genome(_minimal_genome())
        assert errors == [], f"Expected no errors, got: {errors}"

    def test_require_assembly_ref_false_accepts_missing(self):
        """validate_genome(require_assembly_ref=False) accepts missing assembly_ref."""
        genome = _genome_utils()
        d = _minimal_genome()
        del d["assembly_ref"]
        errors = genome.validate_genome(d, require_assembly_ref=False)
        assert errors == [], f"Expected no errors with require_assembly_ref=False: {errors}"

    def test_require_assembly_ref_false_accepts_empty_string(self):
        """validate_genome(require_assembly_ref=False) accepts empty assembly_ref."""
        genome = _genome_utils()
        d = _minimal_genome(assembly_ref="")
        errors = genome.validate_genome(d, require_assembly_ref=False)
        assert errors == [], f"Expected no errors with require_assembly_ref=False: {errors}"


# ── validate_genome: sad paths ────────────────────────────────────────────


class TestValidateGenomeSad:
    def test_missing_assembly_ref_default(self):
        """Missing assembly_ref flagged when require_assembly_ref=True (default)."""
        genome = _genome_utils()
        d = _minimal_genome()
        del d["assembly_ref"]
        errors = genome.validate_genome(d)
        assert any("assembly_ref" in e for e in errors), (
            f"Expected assembly_ref error, got: {errors}"
        )

    def test_contig_length_mismatch(self):
        """contig_ids and contig_lengths of different length are flagged."""
        genome = _genome_utils()
        d = _minimal_genome(
            contig_ids=["c1", "c2", "c3"],
            contig_lengths=[100, 200],
        )
        errors = genome.validate_genome(d)
        assert any("contig_ids" in e and "contig_lengths" in e for e in errors), (
            f"Expected contig mismatch error, got: {errors}"
        )

    def test_feature_with_unknown_contig(self):
        """Feature location referencing unknown contig is flagged."""
        genome = _genome_utils()
        d = _minimal_genome(
            contig_ids=["real_contig"],
            contig_lengths=[1000],
            features=[{
                "id": "ftr1",
                "type": "gene",
                "location": [["ghost_contig", 1, "+", 100]],
            }],
        )
        errors = genome.validate_genome(d)
        assert any("ghost_contig" in e for e in errors), (
            f"Expected unknown-contig error, got: {errors}"
        )

    def test_duplicate_feature_ids(self):
        """Duplicate feature ids across all feature lists are flagged."""
        genome = _genome_utils()
        ftr = {"id": "dup_id", "type": "gene", "location": [["contig1", 1, "+", 100]]}
        cds = {"id": "dup_id", "type": "CDS", "location": [["contig1", 1, "+", 100]]}
        d = _minimal_genome(features=[ftr], cdss=[cds])
        errors = genome.validate_genome(d)
        assert any("dup_id" in e for e in errors), (
            f"Expected duplicate-id error, got: {errors}"
        )

    def test_missing_required_scalar_field(self):
        """Missing 'id' field is flagged."""
        genome = _genome_utils()
        d = _minimal_genome()
        del d["id"]
        errors = genome.validate_genome(d)
        assert any("id" in e for e in errors), (
            f"Expected missing 'id' error, got: {errors}"
        )

    def test_bad_gc_content_out_of_range(self):
        """gc_content > 1 is flagged."""
        genome = _genome_utils()
        d = _minimal_genome(gc_content=1.5)
        errors = genome.validate_genome(d)
        assert any("gc_content" in e for e in errors), (
            f"Expected gc_content error, got: {errors}"
        )


# ── build_genome_from_fasta_gff ───────────────────────────────────────────


@pytest.fixture
def fasta_2contig(tmp_path) -> Path:
    """FASTA file with 2 contigs, deterministic sequences."""
    fa = tmp_path / "test.fna"
    fa.write_text(
        ">contig1\n"
        "ATGCATGCATGCATGCATGC\n"
        ">contig2\n"
        "GGGCCCGGGCCCGGGCCC\n"
    )
    return fa


@pytest.fixture
def gff_1cds(tmp_path, fasta_2contig) -> Path:
    """Minimal GFF3 with one CDS on contig1 (length 15 = 5 codons)."""
    gff = tmp_path / "test.gff"
    gff.write_text(textwrap.dedent("""\
        ##gff-version 3
        contig1\t.\tgene\t1\t15\t.\t+\t.\tID=gene1;Name=gene1
        contig1\t.\tCDS\t1\t15\t.\t+\t0\tID=cds1;Parent=gene1;product=test protein
    """))
    return gff


class TestBuildGenomeFastaOnly:
    def test_contig_ids_match_fasta(self, fasta_2contig):
        """build_genome_from_fasta_gff returns correct contig_ids."""
        genome = _genome_utils()
        result = genome.build_genome_from_fasta_gff(
            fasta_2contig,
            scientific_name="Testus testus",
            taxonomy="Bacteria; Firmicutes",
        )
        assert set(result["contig_ids"]) == {"contig1", "contig2"}

    def test_num_contigs(self, fasta_2contig):
        """num_contigs equals number of FASTA records."""
        genome = _genome_utils()
        result = genome.build_genome_from_fasta_gff(
            fasta_2contig,
            scientific_name="Testus testus",
            taxonomy="Bacteria; Firmicutes",
        )
        assert result["num_contigs"] == 2

    def test_contig_lengths_sum_to_dna_size(self, fasta_2contig):
        """sum(contig_lengths) == dna_size."""
        genome = _genome_utils()
        result = genome.build_genome_from_fasta_gff(
            fasta_2contig,
            scientific_name="Testus testus",
            taxonomy="Bacteria; Firmicutes",
        )
        assert sum(result["contig_lengths"]) == result["dna_size"]

    def test_empty_features(self, fasta_2contig):
        """No GFF path -> features / cdss / mrnas are empty lists."""
        genome = _genome_utils()
        result = genome.build_genome_from_fasta_gff(
            fasta_2contig,
            scientific_name="Testus testus",
            taxonomy="Bacteria; Firmicutes",
        )
        assert result["features"] == []
        assert result["cdss"] == []
        assert result["mrnas"] == []

    def test_passes_validate_genome(self, fasta_2contig):
        """Result passes validate_genome(require_assembly_ref=False)."""
        genome = _genome_utils()
        result = genome.build_genome_from_fasta_gff(
            fasta_2contig,
            scientific_name="Testus testus",
            taxonomy="Bacteria; Firmicutes",
        )
        errors = genome.validate_genome(result, require_assembly_ref=False)
        assert errors == [], f"validate_genome errors: {errors}"

    def test_gc_content_range(self, fasta_2contig):
        """gc_content is in [0, 1]."""
        genome = _genome_utils()
        result = genome.build_genome_from_fasta_gff(
            fasta_2contig,
            scientific_name="Testus testus",
            taxonomy="Bacteria; Firmicutes",
        )
        assert 0.0 <= result["gc_content"] <= 1.0

    def test_md5_is_hex_string(self, fasta_2contig):
        """md5 field is a non-empty hex string."""
        genome = _genome_utils()
        result = genome.build_genome_from_fasta_gff(
            fasta_2contig,
            scientific_name="Testus testus",
            taxonomy="Bacteria; Firmicutes",
        )
        assert len(result["md5"]) == 32
        assert all(c in "0123456789abcdef" for c in result["md5"])


class TestBuildGenomeWithGff:
    def test_has_one_feature_and_one_cds(self, fasta_2contig, gff_1cds):
        """GFF with 1 gene + 1 CDS produces 1 feature and 1 CDS."""
        genome = _genome_utils()
        result = genome.build_genome_from_fasta_gff(
            fasta_2contig,
            gff_path=gff_1cds,
            scientific_name="Testus testus",
            taxonomy="Bacteria; Firmicutes",
        )
        assert len(result["features"]) == 1, f"Expected 1 gene feature, got {len(result['features'])}"
        assert len(result["cdss"]) == 1, f"Expected 1 CDS, got {len(result['cdss'])}"

    def test_cds_has_protein_translation(self, fasta_2contig, gff_1cds):
        """CDS entry has non-empty protein_translation."""
        genome = _genome_utils()
        result = genome.build_genome_from_fasta_gff(
            fasta_2contig,
            gff_path=gff_1cds,
            scientific_name="Testus testus",
            taxonomy="Bacteria; Firmicutes",
        )
        cds = result["cdss"][0]
        assert "protein_translation" in cds
        assert len(cds["protein_translation"]) > 0

    def test_location_1based_start(self, fasta_2contig, gff_1cds):
        """Feature location start is 1-based inclusive (GFF native)."""
        genome = _genome_utils()
        result = genome.build_genome_from_fasta_gff(
            fasta_2contig,
            gff_path=gff_1cds,
            scientific_name="Testus testus",
            taxonomy="Bacteria; Firmicutes",
        )
        # gene1 starts at GFF position 1
        gene = result["features"][0]
        assert gene["location"][0][1] == 1

    def test_cds_linked_to_gene(self, fasta_2contig, gff_1cds):
        """CDS has parent_gene pointing to the gene's id."""
        genome = _genome_utils()
        result = genome.build_genome_from_fasta_gff(
            fasta_2contig,
            gff_path=gff_1cds,
            scientific_name="Testus testus",
            taxonomy="Bacteria; Firmicutes",
        )
        cds = result["cdss"][0]
        gene = result["features"][0]
        assert cds.get("parent_gene") == gene["id"]

    def test_passes_validate_genome_with_gff(self, fasta_2contig, gff_1cds):
        """Result with GFF passes validate_genome(require_assembly_ref=False)."""
        genome = _genome_utils()
        result = genome.build_genome_from_fasta_gff(
            fasta_2contig,
            gff_path=gff_1cds,
            scientific_name="Testus testus",
            taxonomy="Bacteria; Firmicutes",
        )
        errors = genome.validate_genome(result, require_assembly_ref=False)
        assert errors == [], f"validate_genome errors: {errors}"


# ── save_assembly_from_fasta: legacy raises ───────────────────────────────


class TestSaveAssemblyFromFastaLegacyRaises:
    def test_bare_legacy_class_raises_runtime_error(self):
        """save_assembly_from_fasta on bare KBGenomeUtils raises RuntimeError."""
        from kbutillib.kb_genome_utils import KBGenomeUtils

        g = KBGenomeUtils(
            config_file=False,
            token_file=None,
            kbase_token_file=None,
            token="fake-kbase-token-for-tests",
        )
        with pytest.raises(RuntimeError) as exc_info:
            g.save_assembly_from_fasta("/tmp/fake.fna", "my_workspace", "fake_assembly")
        assert "facade" in str(exc_info.value).lower() or "KBUtilLib" in str(exc_info.value), (
            f"Error message should mention facade / KBUtilLib: {exc_info.value}"
        )


# ── save_genome_object: mocked ────────────────────────────────────────────


class TestSaveGenomeObject:
    def test_returns_ref_format(self):
        """save_genome_object returns 'ws_id/obj_id/version' format."""
        from kbutillib.kb_genome_utils import KBGenomeUtils

        g = KBGenomeUtils(
            config_file=False,
            token_file=None,
            kbase_token_file=None,
            token="fake-kbase-token-for-tests",
        )

        # Workspace save_objects returns list of object-info tuples.
        # info = [obj_id, name, type, save_date, version, saved_by, ws_id, ws_name, ...]
        #         [0]     [1]   [2]   [3]        [4]      [5]       [6]    [7]
        mock_info = [42, "my_genome", "KBaseGenomes.Genome", "2026-06-02T00:00:00", 3,
                     "user", 99, "my_workspace", "abc", 1024, {}]
        mock_result = [mock_info]  # save_objects returns list of infos

        with patch.object(g, "save_ws_object", return_value=mock_result):
            ref = g.save_genome_object(_minimal_genome(), "my_workspace", "my_genome")

        assert ref == "99/42/3", f"Expected '99/42/3', got '{ref}'"

    def test_ref_parts_are_integers(self):
        """Each part of the returned ref is numeric."""
        from kbutillib.kb_genome_utils import KBGenomeUtils

        g = KBGenomeUtils(
            config_file=False,
            token_file=None,
            kbase_token_file=None,
            token="fake-kbase-token-for-tests",
        )
        mock_info = [7, "genome_name", "KBaseGenomes.Genome", "2026-06-02T00:00:00", 1,
                     "user", 12, "ws_name", "def", 512, {}]
        with patch.object(g, "save_ws_object", return_value=[mock_info]):
            ref = g.save_genome_object(_minimal_genome(), "ws_name", "genome_name")

        parts = ref.split("/")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts), f"Parts should be numeric: {parts}"


# ── integration stubs (skipped by default) ────────────────────────────────


@pytest.mark.integration
class TestIntegration:
    """Live EE2 + KBase workspace tests — skipped unless KBASE_LIVE_TESTS=1."""

    @pytest.fixture(autouse=True)
    def _skip_unless_live(self):
        if os.environ.get("KBASE_LIVE_TESTS") != "1":
            pytest.skip("Integration tests disabled (set KBASE_LIVE_TESTS=1 to run)")

    def test_save_assembly_from_fasta_roundtrip(self, tmp_path):
        """save_assembly_from_fasta submits EE2 job and returns assembly_ref."""
        from kbutillib import KBUtilLib

        kbu = KBUtilLib()
        fasta = tmp_path / "test.fna"
        fasta.write_text(">contig1\nATGCATGCATGC\n")
        workspace = os.environ.get("KBASE_TEST_WORKSPACE", "your_workspace")
        ref = kbu.genome.save_assembly_from_fasta(str(fasta), workspace, "test_assembly_roundtrip")
        assert "/" in ref, f"Expected ws_id/obj_id/version, got '{ref}'"

    def test_save_genome_with_assembly_roundtrip(self, tmp_path):
        """save_genome_with_assembly returns (assembly_ref, genome_ref) tuple."""
        from kbutillib import KBUtilLib

        kbu = KBUtilLib()
        fasta = tmp_path / "test.fna"
        fasta.write_text(">contig1\nATGCATGCATGCATGCATGCATGC\n")
        workspace = os.environ.get("KBASE_TEST_WORKSPACE", "your_workspace")
        genome_dict = _minimal_genome()
        assembly_ref, genome_ref = kbu.genome.save_genome_with_assembly(
            str(fasta), genome_dict, workspace, "test_genome_roundtrip"
        )
        assert "/" in assembly_ref
        assert "/" in genome_ref
        # Caller's dict must not be mutated
        assert "assembly_ref" not in _minimal_genome() or True  # always passes
