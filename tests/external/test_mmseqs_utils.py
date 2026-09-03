"""Tests for MMSeqsUtils."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kbutillib import MMSeqsUtils


class TestMMSeqsUtilsInitialization:
    """Test suite for MMSeqsUtils initialization."""

    def test_initialization_default_executable(self, temp_dir):
        """Test initialization uses default executable path."""
        with patch.object(MMSeqsUtils, '_check_mmseqs_availability'):
            utils = MMSeqsUtils(
                token_file=Path(temp_dir) / "tokens",
                kbase_token_file=Path(temp_dir) / "kbase_token"
            )
            assert utils.mmseqs_executable == "mmseqs"

    def test_initialization_custom_executable(self, temp_dir):
        """Test initialization with custom executable from config."""
        # Create a config file with custom executable path
        config_file = Path(temp_dir) / "config.yaml"
        config_file.write_text("mmseqs:\n  executable: /custom/path/mmseqs\n")

        with patch.object(MMSeqsUtils, '_check_mmseqs_availability'):
            utils = MMSeqsUtils(
                config_file=str(config_file),
                token_file=Path(temp_dir) / "tokens",
                kbase_token_file=Path(temp_dir) / "kbase_token"
            )
            assert utils.mmseqs_executable == "/custom/path/mmseqs"

    def test_mmseqs_available_check_success(self, temp_dir):
        """Test that mmseqs availability is detected when installed."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "MMseqs2 Version: 15.6f452"

        with patch('subprocess.run', return_value=mock_result):
            utils = MMSeqsUtils(
                token_file=Path(temp_dir) / "tokens",
                kbase_token_file=Path(temp_dir) / "kbase_token"
            )
            assert utils.mmseqs_available is True

    def test_mmseqs_available_check_not_found(self, temp_dir):
        """Test that mmseqs unavailability is handled gracefully."""
        with patch('subprocess.run', side_effect=FileNotFoundError()):
            utils = MMSeqsUtils(
                token_file=Path(temp_dir) / "tokens",
                kbase_token_file=Path(temp_dir) / "kbase_token"
            )
            assert utils.mmseqs_available is False


class TestClusterProteins:
    """Test suite for cluster_proteins method."""

    @pytest.fixture
    def mock_utils(self, temp_dir):
        """Create MMSeqsUtils instance with mocked availability."""
        with patch.object(MMSeqsUtils, '_check_mmseqs_availability'):
            utils = MMSeqsUtils(
                token_file=Path(temp_dir) / "tokens",
                kbase_token_file=Path(temp_dir) / "kbase_token"
            )
            utils.mmseqs_available = True
            return utils

    @pytest.fixture
    def sample_proteins(self):
        """Create sample protein data for testing."""
        return [
            {"id": "prot1", "protein_translation": "MKTAYIAKQRQISFVK"},
            {"id": "prot2", "protein_translation": "MKTAYIAKQRQISFVK"},
            {"id": "prot3", "protein_translation": "MVLSPADKTNVKAAWGK"},
        ]

    def test_cluster_proteins_mmseqs_not_available(self, temp_dir):
        """Test that clustering raises error when mmseqs not available."""
        with patch.object(MMSeqsUtils, '_check_mmseqs_availability'):
            utils = MMSeqsUtils(
                token_file=Path(temp_dir) / "tokens",
                kbase_token_file=Path(temp_dir) / "kbase_token"
            )
            utils.mmseqs_available = False

            proteins = [{"id": "p1", "protein_translation": "MKTAY"}]
            with pytest.raises(RuntimeError, match="MMseqs2 is not available"):
                utils.cluster_proteins(proteins)

    def test_cluster_proteins_empty_list(self, mock_utils):
        """Test that empty protein list raises ValueError."""
        with pytest.raises(ValueError, match="proteins list cannot be empty"):
            mock_utils.cluster_proteins([])

    def test_cluster_proteins_missing_id(self, mock_utils):
        """Test that protein without id raises ValueError."""
        proteins = [{"protein_translation": "MKTAY"}]
        with pytest.raises(ValueError, match="missing 'id' field"):
            mock_utils.cluster_proteins(proteins)

    def test_cluster_proteins_missing_sequence(self, mock_utils):
        """Test that protein without sequence raises ValueError."""
        proteins = [{"id": "prot1"}]
        with pytest.raises(ValueError, match="missing 'protein_translation' field"):
            mock_utils.cluster_proteins(proteins)

    def test_cluster_proteins_success(self, mock_utils, sample_proteins, temp_dir):
        """Test successful clustering with mocked subprocess calls."""
        # Mock the subprocess calls to simulate successful mmseqs execution
        mock_results = []

        # Mock for createdb
        createdb_result = MagicMock()
        createdb_result.returncode = 0
        mock_results.append(createdb_result)

        # Mock for cluster
        cluster_result = MagicMock()
        cluster_result.returncode = 0
        mock_results.append(cluster_result)

        # Mock for createtsv
        createtsv_result = MagicMock()
        createtsv_result.returncode = 0
        mock_results.append(createtsv_result)

        def mock_run(cmd, **kwargs):
            result = mock_results.pop(0) if mock_results else MagicMock(returncode=0)
            # Create the TSV output file if this is createtsv
            if 'createtsv' in cmd:
                output_path = cmd[-1]  # Last arg is output file
                with open(output_path, 'w') as f:
                    f.write("prot1\tprot1\n")
                    f.write("prot1\tprot2\n")
                    f.write("prot3\tprot3\n")
            return result

        with patch('subprocess.run', side_effect=mock_run):
            result = mock_utils.cluster_proteins(sample_proteins)

        assert result["success"] is True
        assert result["num_proteins"] == 3
        assert result["num_clusters"] == 2
        assert len(result["clusters"]) == 2

    def test_cluster_proteins_createdb_failure(self, mock_utils, sample_proteins):
        """Test handling of createdb failure."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "createdb error"

        with patch('subprocess.run', return_value=mock_result):
            result = mock_utils.cluster_proteins(sample_proteins)

        assert result["success"] is False
        assert "createdb failed" in result["error"]

    def test_cluster_proteins_with_extra_args(self, mock_utils, sample_proteins):
        """Test that extra_args are passed to mmseqs cluster command."""
        captured_cmd = []

        def capture_cmd(cmd, **kwargs):
            captured_cmd.append(cmd)
            mock_result = MagicMock()
            mock_result.returncode = 0
            if 'createtsv' in cmd:
                output_path = cmd[-1]
                with open(output_path, 'w') as f:
                    f.write("prot1\tprot1\n")
            return mock_result

        with patch('subprocess.run', side_effect=capture_cmd):
            mock_utils.cluster_proteins(
                sample_proteins,
                extra_args=["--custom-arg", "value"]
            )

        # Find the cluster command
        cluster_cmd = None
        for cmd in captured_cmd:
            if 'cluster' in cmd and 'createtsv' not in cmd:
                cluster_cmd = cmd
                break

        assert cluster_cmd is not None
        assert "--custom-arg" in cluster_cmd
        assert "value" in cluster_cmd


class TestEasyCluster:
    """Test suite for easy_cluster convenience method."""

    def test_easy_cluster_calls_cluster_proteins(self, temp_dir):
        """Test that easy_cluster calls cluster_proteins with correct defaults."""
        with patch.object(MMSeqsUtils, '_check_mmseqs_availability'):
            utils = MMSeqsUtils(
                token_file=Path(temp_dir) / "tokens",
                kbase_token_file=Path(temp_dir) / "kbase_token"
            )
            utils.mmseqs_available = True

            proteins = [{"id": "p1", "protein_translation": "MKTAY"}]

            with patch.object(utils, 'cluster_proteins', return_value={"success": True}) as mock:
                utils.easy_cluster(proteins, min_seq_id=0.9)

                mock.assert_called_once()
                call_kwargs = mock.call_args[1]
                assert call_kwargs["min_seq_id"] == 0.9
                assert call_kwargs["coverage"] == 0.8
                assert call_kwargs["coverage_mode"] == 0
                assert call_kwargs["cluster_mode"] == 0


class TestHelperMethods:
    """Test suite for helper methods."""

    @pytest.fixture
    def mock_utils(self, temp_dir):
        """Create MMSeqsUtils instance with mocked availability."""
        with patch.object(MMSeqsUtils, '_check_mmseqs_availability'):
            utils = MMSeqsUtils(
                token_file=Path(temp_dir) / "tokens",
                kbase_token_file=Path(temp_dir) / "kbase_token"
            )
            utils.mmseqs_available = True
            return utils

    def test_get_cluster_representatives(self, mock_utils):
        """Test extracting cluster representatives."""
        cluster_result = {
            "success": True,
            "clusters": [
                {"representative": "prot1", "members": ["prot1", "prot2"], "size": 2},
                {"representative": "prot3", "members": ["prot3"], "size": 1},
            ]
        }

        representatives = mock_utils.get_cluster_representatives(cluster_result)
        assert len(representatives) == 2
        assert representatives[0]["id"] == "prot1"
        assert representatives[1]["id"] == "prot3"

    def test_get_cluster_representatives_with_proteins(self, mock_utils):
        """Test extracting full protein data for representatives."""
        cluster_result = {
            "success": True,
            "clusters": [
                {"representative": "prot1", "members": ["prot1", "prot2"], "size": 2},
            ]
        }
        proteins = [
            {"id": "prot1", "protein_translation": "MKTAY", "extra_field": "value1"},
            {"id": "prot2", "protein_translation": "MKTAY", "extra_field": "value2"},
        ]

        representatives = mock_utils.get_cluster_representatives(cluster_result, proteins)
        assert len(representatives) == 1
        assert representatives[0]["id"] == "prot1"
        assert representatives[0]["extra_field"] == "value1"

    def test_get_cluster_representatives_failed_result(self, mock_utils):
        """Test handling of failed clustering result."""
        cluster_result = {"success": False, "clusters": []}
        representatives = mock_utils.get_cluster_representatives(cluster_result)
        assert representatives == []

    def test_get_cluster_membership(self, mock_utils):
        """Test creating cluster membership mapping."""
        cluster_result = {
            "success": True,
            "clusters": [
                {"representative": "prot1", "members": ["prot1", "prot2", "prot4"], "size": 3},
                {"representative": "prot3", "members": ["prot3"], "size": 1},
            ]
        }

        membership = mock_utils.get_cluster_membership(cluster_result)
        assert membership["prot1"] == "prot1"
        assert membership["prot2"] == "prot1"
        assert membership["prot3"] == "prot3"
        assert membership["prot4"] == "prot1"

    def test_get_cluster_membership_failed_result(self, mock_utils):
        """Test handling of failed clustering result."""
        cluster_result = {"success": False}
        membership = mock_utils.get_cluster_membership(cluster_result)
        assert membership == {}


class TestWriteFasta:
    """Test suite for FASTA writing functionality."""

    @pytest.fixture
    def mock_utils(self, temp_dir):
        """Create MMSeqsUtils instance with mocked availability."""
        with patch.object(MMSeqsUtils, '_check_mmseqs_availability'):
            utils = MMSeqsUtils(
                token_file=Path(temp_dir) / "tokens",
                kbase_token_file=Path(temp_dir) / "kbase_token"
            )
            return utils

    def test_write_fasta_basic(self, mock_utils, temp_dir):
        """Test basic FASTA file writing."""
        proteins = [
            {"id": "prot1", "protein_translation": "MKTAY"},
            {"id": "prot2", "protein_translation": "MVLSP"},
        ]
        output_path = Path(temp_dir) / "test.fasta"

        mock_utils._write_fasta(proteins, output_path)

        content = output_path.read_text()
        assert ">prot1\n" in content
        assert "MKTAY\n" in content
        assert ">prot2\n" in content
        assert "MVLSP\n" in content

    def test_write_fasta_long_sequence(self, mock_utils, temp_dir):
        """Test that long sequences are wrapped at 80 characters."""
        long_seq = "M" * 100  # 100 character sequence
        proteins = [{"id": "prot1", "protein_translation": long_seq}]
        output_path = Path(temp_dir) / "test.fasta"

        mock_utils._write_fasta(proteins, output_path)

        content = output_path.read_text()
        lines = content.strip().split('\n')
        # Should have header + 2 sequence lines (80 + 20)
        assert len(lines) == 3
        assert lines[0] == ">prot1"
        assert len(lines[1]) == 80
        assert len(lines[2]) == 20


class TestParseClusterTsv:
    """Test suite for TSV parsing functionality."""

    @pytest.fixture
    def mock_utils(self, temp_dir):
        """Create MMSeqsUtils instance with mocked availability."""
        with patch.object(MMSeqsUtils, '_check_mmseqs_availability'):
            utils = MMSeqsUtils(
                token_file=Path(temp_dir) / "tokens",
                kbase_token_file=Path(temp_dir) / "kbase_token"
            )
            return utils

    def test_parse_cluster_tsv_basic(self, mock_utils, temp_dir):
        """Test basic TSV parsing."""
        tsv_file = Path(temp_dir) / "clusters.tsv"
        tsv_file.write_text("prot1\tprot1\nprot1\tprot2\nprot3\tprot3\n")

        clusters = mock_utils._parse_cluster_tsv(tsv_file)

        assert len(clusters) == 2
        # Clusters should be sorted by size (largest first)
        assert clusters[0]["representative"] == "prot1"
        assert clusters[0]["size"] == 2
        assert set(clusters[0]["members"]) == {"prot1", "prot2"}
        assert clusters[1]["representative"] == "prot3"
        assert clusters[1]["size"] == 1

    def test_parse_cluster_tsv_empty(self, mock_utils, temp_dir):
        """Test parsing empty TSV file."""
        tsv_file = Path(temp_dir) / "empty.tsv"
        tsv_file.write_text("")

        clusters = mock_utils._parse_cluster_tsv(tsv_file)
        assert clusters == []

    def test_parse_cluster_tsv_with_empty_lines(self, mock_utils, temp_dir):
        """Test parsing TSV with empty lines."""
        tsv_file = Path(temp_dir) / "clusters.tsv"
        tsv_file.write_text("prot1\tprot1\n\nprot1\tprot2\n\n")

        clusters = mock_utils._parse_cluster_tsv(tsv_file)
        assert len(clusters) == 1
        assert clusters[0]["size"] == 2


@pytest.mark.integration
class TestMMSeqsIntegration:
    """Integration tests that require MMseqs2 to be installed."""

    def test_cluster_real_proteins(self, temp_dir):
        """Test actual clustering with MMseqs2 (requires installation)."""
        try:
            utils = MMSeqsUtils(
                token_file=Path(temp_dir) / "tokens",
                kbase_token_file=Path(temp_dir) / "kbase_token"
            )
        except Exception:
            pytest.skip("MMseqs2 not available")

        if not utils.mmseqs_available:
            pytest.skip("MMseqs2 not installed")

        # Create test proteins with identical sequences (should cluster together)
        proteins = [
            {"id": "identical1", "protein_translation": "MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEKAVQV"},
            {"id": "identical2", "protein_translation": "MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEKAVQV"},
            {"id": "different", "protein_translation": "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHFDLSHGSAQVK"},
        ]

        result = utils.cluster_proteins(proteins, min_seq_id=0.9)

        assert result["success"] is True
        assert result["num_proteins"] == 3
        # With 90% identity, identical1 and identical2 should cluster
        assert result["num_clusters"] == 2


class TestBuildSearchDb:
    """Test suite for build_search_db method."""

    @pytest.fixture
    def mock_utils(self, temp_dir):
        """Create MMSeqsUtils instance with mocked availability."""
        with patch.object(MMSeqsUtils, '_check_mmseqs_availability'):
            utils = MMSeqsUtils(
                token_file=Path(temp_dir) / "tokens",
                kbase_token_file=Path(temp_dir) / "kbase_token"
            )
            utils.mmseqs_available = True
            return utils

    @pytest.fixture
    def sample_proteins(self):
        """Create sample protein data for testing."""
        return [
            {"id": "rep1", "protein_translation": "MKTAYIAKQRQISFVK"},
            {"id": "rep2", "protein_translation": "MVLSPADKTNVKAAWGK"},
        ]

    def test_build_search_db_mmseqs_not_available(self, temp_dir):
        """Test that build_search_db raises when mmseqs not available."""
        with patch.object(MMSeqsUtils, '_check_mmseqs_availability'):
            utils = MMSeqsUtils(
                token_file=Path(temp_dir) / "tokens",
                kbase_token_file=Path(temp_dir) / "kbase_token"
            )
            utils.mmseqs_available = False

            proteins = [{"id": "p1", "protein_translation": "MKTAY"}]
            with pytest.raises(RuntimeError, match="MMseqs2 is not available"):
                utils.build_search_db(proteins, Path(temp_dir) / "db")

    def test_build_search_db_empty_list(self, mock_utils, temp_dir):
        """Test that empty protein list raises ValueError."""
        with pytest.raises(ValueError, match="proteins list cannot be empty"):
            mock_utils.build_search_db([], Path(temp_dir) / "db")

    def test_build_search_db_missing_id(self, mock_utils, temp_dir):
        """Test that protein without id raises ValueError."""
        proteins = [{"protein_translation": "MKTAY"}]
        with pytest.raises(ValueError, match="missing 'id' field"):
            mock_utils.build_search_db(proteins, Path(temp_dir) / "db")

    def test_build_search_db_missing_sequence(self, mock_utils, temp_dir):
        """Test that protein without sequence raises ValueError."""
        proteins = [{"id": "prot1"}]
        with pytest.raises(ValueError, match="missing 'protein_translation' field"):
            mock_utils.build_search_db(proteins, Path(temp_dir) / "db")

    def test_build_search_db_success(self, mock_utils, sample_proteins, temp_dir):
        """Test successful database build with mocked subprocess calls."""
        captured_cmds = []

        def mock_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            result = MagicMock()
            result.returncode = 0
            if "createdb" in cmd:
                # Simulate the primary db file being created.
                Path(cmd[-1]).write_text("fake db")
            return result

        out_dir = Path(temp_dir) / "refdb"
        with patch('subprocess.run', side_effect=mock_run):
            db_path = mock_utils.build_search_db(sample_proteins, out_dir, threads=4)

        assert db_path == out_dir / "searchDB"

        createdb_cmd = next(c for c in captured_cmds if "createdb" in c)
        assert mock_utils.mmseqs_executable in createdb_cmd

        createindex_cmd = next(c for c in captured_cmds if "createindex" in c)
        assert "--threads" in createindex_cmd
        assert createindex_cmd[createindex_cmd.index("--threads") + 1] == "4"

    def test_build_search_db_createdb_failure(self, mock_utils, sample_proteins, temp_dir):
        """Test handling of createdb failure."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "createdb error"

        with patch('subprocess.run', return_value=mock_result):
            with pytest.raises(RuntimeError, match="mmseqs createdb failed"):
                mock_utils.build_search_db(sample_proteins, Path(temp_dir) / "db")

    def test_build_search_db_createindex_failure(self, mock_utils, sample_proteins, temp_dir):
        """Test handling of createindex failure."""
        def mock_run(cmd, **kwargs):
            result = MagicMock()
            if "createdb" in cmd:
                result.returncode = 0
                Path(cmd[-1]).write_text("fake db")
            else:
                result.returncode = 1
                result.stderr = "createindex error"
            return result

        with patch('subprocess.run', side_effect=mock_run):
            with pytest.raises(RuntimeError, match="mmseqs createindex failed"):
                mock_utils.build_search_db(sample_proteins, Path(temp_dir) / "db")


class TestSearchProteins:
    """Test suite for search_proteins method."""

    @pytest.fixture
    def mock_utils(self, temp_dir):
        """Create MMSeqsUtils instance with mocked availability."""
        with patch.object(MMSeqsUtils, '_check_mmseqs_availability'):
            utils = MMSeqsUtils(
                token_file=Path(temp_dir) / "tokens",
                kbase_token_file=Path(temp_dir) / "kbase_token"
            )
            utils.mmseqs_available = True
            return utils

    @pytest.fixture
    def sample_proteins(self):
        """Create sample query protein data for testing."""
        return [
            {"id": "query1", "protein_translation": "MKTAYIAKQRQISFVK"},
            {"id": "query2", "protein_translation": "MVLSPADKTNVKAAWGK"},
        ]

    @pytest.fixture
    def fake_db_path(self, temp_dir):
        """A stand-in path for a prebuilt search database."""
        db_path = Path(temp_dir) / "searchDB"
        db_path.write_text("fake db")
        return db_path

    def _mock_run_factory(self, tsv_rows):
        """Build a subprocess.run side_effect that writes *tsv_rows* on convertalis."""
        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if "convertalis" in cmd:
                output_path = cmd[-3]  # ... <tsv_output> --format-output <cols>
                with open(output_path, 'w') as f:
                    for row in tsv_rows:
                        f.write("\t".join(str(v) for v in row) + "\n")
            return result
        return mock_run

    def test_search_proteins_mmseqs_not_available(self, temp_dir, fake_db_path):
        """Test that search_proteins raises when mmseqs not available."""
        with patch.object(MMSeqsUtils, '_check_mmseqs_availability'):
            utils = MMSeqsUtils(
                token_file=Path(temp_dir) / "tokens",
                kbase_token_file=Path(temp_dir) / "kbase_token"
            )
            utils.mmseqs_available = False

            proteins = [{"id": "p1", "protein_translation": "MKTAY"}]
            with pytest.raises(RuntimeError, match="MMseqs2 is not available"):
                utils.search_proteins(proteins, fake_db_path, min_seq_id=0.5, coverage=0.5)

    def test_search_proteins_empty_list(self, mock_utils, fake_db_path):
        """Test that empty protein list raises ValueError."""
        with pytest.raises(ValueError, match="proteins list cannot be empty"):
            mock_utils.search_proteins([], fake_db_path, min_seq_id=0.5, coverage=0.5)

    def test_search_proteins_missing_id(self, mock_utils, fake_db_path):
        """Test that protein without id raises ValueError."""
        proteins = [{"protein_translation": "MKTAY"}]
        with pytest.raises(ValueError, match="missing 'id' field"):
            mock_utils.search_proteins(proteins, fake_db_path, min_seq_id=0.5, coverage=0.5)

    def test_search_proteins_missing_sequence(self, mock_utils, fake_db_path):
        """Test that protein without sequence raises ValueError."""
        proteins = [{"id": "prot1"}]
        with pytest.raises(ValueError, match="missing 'protein_translation' field"):
            mock_utils.search_proteins(proteins, fake_db_path, min_seq_id=0.5, coverage=0.5)

    def test_search_proteins_db_not_found(self, mock_utils, sample_proteins, temp_dir):
        """Test that a missing database path raises ValueError."""
        missing_db = Path(temp_dir) / "does_not_exist_db"
        with pytest.raises(ValueError, match="MMseqs2 database not found"):
            mock_utils.search_proteins(sample_proteins, missing_db, min_seq_id=0.5, coverage=0.5)

    def test_search_proteins_success_returns_exact_keys_and_fraction_identity(
        self, mock_utils, sample_proteins, fake_db_path
    ):
        """Test that hits carry exactly the documented keys with identity as a fraction."""
        # pident is a PERCENTAGE (mmseqs convention); qcov is already a fraction.
        tsv_rows = [
            ("query1", "target1", "97.300", "1.000", "1.5e-100", "350.0"),
        ]
        with patch('subprocess.run', side_effect=self._mock_run_factory(tsv_rows)):
            hits = mock_utils.search_proteins(
                sample_proteins, fake_db_path, min_seq_id=0.5, coverage=0.5
            )

        assert len(hits) == 1
        hit = hits[0]
        assert set(hit.keys()) == {
            "query_id", "target_id", "identity", "coverage", "evalue", "bits"
        }
        assert hit["query_id"] == "query1"
        assert hit["target_id"] == "target1"
        # 97.300 (percent) must be normalised to a 0.0-1.0 fraction.
        assert hit["identity"] == pytest.approx(0.973)
        assert 0.0 < hit["identity"] <= 1.0
        assert hit["coverage"] == pytest.approx(1.0)
        assert hit["evalue"] == pytest.approx(1.5e-100)
        assert hit["bits"] == pytest.approx(350.0)

    def test_search_proteins_filters_below_thresholds(
        self, mock_utils, sample_proteins, fake_db_path
    ):
        """Test that hits below min_seq_id or coverage are excluded."""
        tsv_rows = [
            ("query1", "target_good", "90.000", "0.900", "1e-50", "200.0"),
            ("query1", "target_low_identity", "40.000", "0.900", "1e-10", "80.0"),
            ("query2", "target_low_coverage", "90.000", "0.300", "1e-10", "80.0"),
        ]
        with patch('subprocess.run', side_effect=self._mock_run_factory(tsv_rows)):
            hits = mock_utils.search_proteins(
                sample_proteins, fake_db_path, min_seq_id=0.5, coverage=0.5
            )

        assert len(hits) == 1
        assert hits[0]["target_id"] == "target_good"

    def test_search_proteins_createdb_failure(self, mock_utils, sample_proteins, fake_db_path):
        """Test handling of createdb failure."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "createdb error"

        with patch('subprocess.run', return_value=mock_result):
            with pytest.raises(RuntimeError, match="mmseqs createdb failed"):
                mock_utils.search_proteins(
                    sample_proteins, fake_db_path, min_seq_id=0.5, coverage=0.5
                )

    def test_search_proteins_search_failure(self, mock_utils, sample_proteins, fake_db_path):
        """Test handling of search step failure."""
        def mock_run(cmd, **kwargs):
            result = MagicMock()
            if "createdb" in cmd:
                result.returncode = 0
            elif cmd[1] == "search":
                result.returncode = 1
                result.stderr = "search error"
            return result

        with patch('subprocess.run', side_effect=mock_run):
            with pytest.raises(RuntimeError, match="mmseqs search failed"):
                mock_utils.search_proteins(
                    sample_proteins, fake_db_path, min_seq_id=0.5, coverage=0.5
                )

    def test_search_proteins_convertalis_failure(self, mock_utils, sample_proteins, fake_db_path):
        """Test handling of convertalis step failure."""
        def mock_run(cmd, **kwargs):
            result = MagicMock()
            if "convertalis" in cmd:
                result.returncode = 1
                result.stderr = "convertalis error"
            else:
                result.returncode = 0
            return result

        with patch('subprocess.run', side_effect=mock_run):
            with pytest.raises(RuntimeError, match="mmseqs convertalis failed"):
                mock_utils.search_proteins(
                    sample_proteins, fake_db_path, min_seq_id=0.5, coverage=0.5
                )

    def test_search_proteins_sets_parameters_explicitly(
        self, mock_utils, sample_proteins, fake_db_path
    ):
        """Test that min_seq_id, coverage, cov-mode and threads are passed explicitly."""
        captured_cmds = []

        def mock_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            return self._mock_run_factory([])(cmd, **kwargs)

        with patch('subprocess.run', side_effect=mock_run):
            mock_utils.search_proteins(
                sample_proteins, fake_db_path,
                min_seq_id=0.6, coverage=0.7, threads=3, sensitivity=6.5
            )

        search_cmd = next(c for c in captured_cmds if len(c) > 1 and c[1] == "search")
        assert "--min-seq-id" in search_cmd
        assert search_cmd[search_cmd.index("--min-seq-id") + 1] == "0.6"
        assert "-c" in search_cmd
        assert search_cmd[search_cmd.index("-c") + 1] == "0.7"
        assert "--cov-mode" in search_cmd
        assert "--threads" in search_cmd
        assert search_cmd[search_cmd.index("--threads") + 1] == "3"
        assert "-s" in search_cmd
        assert search_cmd[search_cmd.index("-s") + 1] == "6.5"


class TestParseSearchTsv:
    """Test suite for the _parse_search_tsv helper."""

    @pytest.fixture
    def mock_utils(self, temp_dir):
        """Create MMSeqsUtils instance with mocked availability."""
        with patch.object(MMSeqsUtils, '_check_mmseqs_availability'):
            utils = MMSeqsUtils(
                token_file=Path(temp_dir) / "tokens",
                kbase_token_file=Path(temp_dir) / "kbase_token"
            )
            return utils

    def test_parse_search_tsv_converts_pident_percentage_to_fraction(self, mock_utils, temp_dir):
        """Test that the pident percentage column is normalised to a fraction."""
        tsv_file = Path(temp_dir) / "results.tsv"
        tsv_file.write_text("q1\tt1\t85.0\t1.0\t1e-20\t100.0\n")

        hits = mock_utils._parse_search_tsv(tsv_file, min_seq_id=0.0, coverage=0.0)

        assert len(hits) == 1
        assert hits[0]["identity"] == pytest.approx(0.85)

    def test_parse_search_tsv_filters_below_min_seq_id(self, mock_utils, temp_dir):
        """Test that rows below min_seq_id are dropped."""
        tsv_file = Path(temp_dir) / "results.tsv"
        tsv_file.write_text("q1\tt1\t40.0\t1.0\t1e-20\t100.0\n")

        hits = mock_utils._parse_search_tsv(tsv_file, min_seq_id=0.5, coverage=0.0)
        assert hits == []

    def test_parse_search_tsv_filters_below_coverage(self, mock_utils, temp_dir):
        """Test that rows below coverage are dropped."""
        tsv_file = Path(temp_dir) / "results.tsv"
        tsv_file.write_text("q1\tt1\t90.0\t0.2\t1e-20\t100.0\n")

        hits = mock_utils._parse_search_tsv(tsv_file, min_seq_id=0.0, coverage=0.5)
        assert hits == []

    def test_parse_search_tsv_empty_file(self, mock_utils, temp_dir):
        """Test parsing an empty TSV file."""
        tsv_file = Path(temp_dir) / "empty.tsv"
        tsv_file.write_text("")

        hits = mock_utils._parse_search_tsv(tsv_file, min_seq_id=0.0, coverage=0.0)
        assert hits == []

    def test_parse_search_tsv_exact_keys(self, mock_utils, temp_dir):
        """Test that parsed hits carry exactly the six documented keys."""
        tsv_file = Path(temp_dir) / "results.tsv"
        tsv_file.write_text("q1\tt1\t100.0\t1.0\t0.0\t500.0\n")

        hits = mock_utils._parse_search_tsv(tsv_file, min_seq_id=0.0, coverage=0.0)
        assert len(hits) == 1
        assert set(hits[0].keys()) == {
            "query_id", "target_id", "identity", "coverage", "evalue", "bits"
        }


@pytest.mark.integration
class TestMMSeqsSearchIntegration:
    """Integration tests for build_search_db/search_proteins (require MMseqs2)."""

    def test_build_and_search_real_proteins(self, temp_dir):
        """Test building a real search DB and searching real proteins against it."""
        try:
            utils = MMSeqsUtils(
                token_file=Path(temp_dir) / "tokens",
                kbase_token_file=Path(temp_dir) / "kbase_token"
            )
        except Exception:
            pytest.skip("MMseqs2 not available")

        if not utils.mmseqs_available:
            pytest.skip("MMseqs2 not installed")

        rep_seq = (
            "MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEKAVQVKVK"
            "ALPDAQFEVVHSLAKWKRQTLGQHDFSAGEGLYTHMKALRPDEDRLSPLHSVYVDQWDWE"
        )
        other_seq = (
            "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHFDLSHGSAQVKGHG"
            "KKVADALTNAVAHVDDMPNALSALSDLHAHKLRVDPVNFKLLSHCLLVTLAAHLPAEFTP"
        )

        references = [
            {"id": "rep1", "protein_translation": rep_seq},
            {"id": "rep2", "protein_translation": other_seq},
        ]
        db_path = utils.build_search_db(references, Path(temp_dir) / "refdb")
        assert db_path.exists()

        queries = [
            {"id": "query1", "protein_translation": rep_seq},
        ]
        hits = utils.search_proteins(queries, db_path, min_seq_id=0.5, coverage=0.5)

        assert len(hits) >= 1
        best_hit = hits[0]
        assert best_hit["query_id"] == "query1"
        assert best_hit["target_id"] == "rep1"
        # Identical sequence against itself should be a near-perfect match,
        # and MUST be a fraction, not a percentage.
        assert 0.9 <= best_hit["identity"] <= 1.0
        assert 0.0 < best_hit["coverage"] <= 1.0

        # The same database can be searched again without rebuilding.
        hits_again = utils.search_proteins(queries, db_path, min_seq_id=0.5, coverage=0.5)
        assert len(hits_again) == len(hits)
