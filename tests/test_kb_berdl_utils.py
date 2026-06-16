"""Tests for KBBERDLUtils."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kbutillib import KBBERDLUtils


class TestKBBERDLUtilsInitialization:
    """Test suite for KBBERDLUtils initialization."""

    def test_initialization_default_config(self, temp_dir):
        """Test initialization uses default configuration."""
        # Create token file with kbase token
        token_file = Path(temp_dir) / "tokens"
        token_file.write_text("kbase=test_token\n")

        utils = KBBERDLUtils(
            token_file=token_file,
            kbase_token_file=Path(temp_dir) / "kbase_token"
        )

        assert utils.berdl_base_url == "https://hub.berdl.kbase.us"
        assert utils.berdl_api_path == "/apis/mcp/delta/tables"
        assert utils.berdl_timeout == 60
        assert utils.berdl_default_limit == 100

    def test_initialization_custom_config(self, temp_dir):
        """Test initialization with custom configuration."""
        config_file = Path(temp_dir) / "config.yaml"
        config_file.write_text("""
berdl:
  base_url: https://custom.berdl.url
  api_path: /custom/api/path
  timeout: 120
  default_limit: 500
""")
        token_file = Path(temp_dir) / "tokens"
        token_file.write_text("kbase=test_token\n")

        utils = KBBERDLUtils(
            config_file=str(config_file),
            token_file=token_file,
            kbase_token_file=Path(temp_dir) / "kbase_token"
        )

        assert utils.berdl_base_url == "https://custom.berdl.url"
        assert utils.berdl_api_path == "/custom/api/path"
        assert utils.berdl_timeout == 120
        assert utils.berdl_default_limit == 500

    def test_api_url_construction(self, temp_dir):
        """Test that API URL is correctly constructed."""
        token_file = Path(temp_dir) / "tokens"
        token_file.write_text("kbase=test_token\n")

        utils = KBBERDLUtils(
            token_file=token_file,
            kbase_token_file=Path(temp_dir) / "kbase_token"
        )

        assert utils.api_url == "https://hub.berdl.kbase.us/apis/mcp/delta/tables"


class TestGetHeaders:
    """Test suite for _get_headers method."""

    def test_get_headers_with_token(self, temp_dir):
        """Test headers are correctly generated with token."""
        token_file = Path(temp_dir) / "tokens"
        token_file.write_text("kbase=my_test_token\n")

        utils = KBBERDLUtils(
            token_file=token_file,
            kbase_token_file=Path(temp_dir) / "kbase_token"
        )

        headers = utils._get_headers()

        assert headers["accept"] == "application/json"
        assert headers["Authorization"] == "Bearer my_test_token"
        assert headers["Content-Type"] == "application/json"

    def test_get_headers_no_token_raises(self, temp_dir):
        """Test that missing token raises ValueError."""
        utils = KBBERDLUtils(
            token_file=Path(temp_dir) / "nonexistent_tokens",
            kbase_token_file=Path(temp_dir) / "nonexistent_kbase_token"
        )

        with pytest.raises(ValueError, match="No KBase token available"):
            utils._get_headers()


class TestQuery:
    """Test suite for query method."""

    @pytest.fixture
    def utils(self, temp_dir):
        """Create KBBERDLUtils instance with token."""
        token_file = Path(temp_dir) / "tokens"
        token_file.write_text("kbase=test_token\n")
        return KBBERDLUtils(
            token_file=token_file,
            kbase_token_file=Path(temp_dir) / "kbase_token"
        )

    def test_query_success(self, utils):
        """Test successful query execution."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"contig_id": "CDM:cntg-1", "gc_content": 0.36, "length": 919},
            {"contig_id": "CDM:cntg-2", "gc_content": 0.32, "length": 911},
        ]
        mock_response.raise_for_status = MagicMock()

        with patch('requests.post', return_value=mock_response):
            result = utils.query("SELECT * FROM kbase_genomes.contig LIMIT 2")

        assert result["success"] is True
        assert result["row_count"] == 2
        assert len(result["data"]) == 2
        assert result["columns"] == ["contig_id", "gc_content", "length"]
        assert result["data"][0]["contig_id"] == "CDM:cntg-1"

    def test_query_authentication_failure(self, utils):
        """Test handling of 401 authentication error."""
        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch('requests.post', return_value=mock_response):
            result = utils.query("SELECT * FROM test")

        assert result["success"] is False
        assert "Authentication failed" in result["error"]

    def test_query_access_denied(self, utils):
        """Test handling of 403 access denied error."""
        mock_response = MagicMock()
        mock_response.status_code = 403

        with patch('requests.post', return_value=mock_response):
            result = utils.query("SELECT * FROM test")

        assert result["success"] is False
        assert "Access denied" in result["error"]

    def test_query_timeout(self, utils):
        """Test handling of request timeout."""
        import requests

        with patch('requests.post', side_effect=requests.exceptions.Timeout()):
            result = utils.query("SELECT * FROM test", timeout=5)

        assert result["success"] is False
        assert "timed out" in result["error"]

    def test_query_with_limit_and_offset(self, utils):
        """Test query with custom limit and offset."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [{"id": 1}]
        mock_response.raise_for_status = MagicMock()

        captured_payload = {}

        def capture_request(*args, **kwargs):
            captured_payload.update(kwargs.get('json', {}))
            return mock_response

        with patch('requests.post', side_effect=capture_request):
            utils.query("SELECT * FROM test", limit=50, offset=100)

        assert captured_payload["limit"] == 50
        assert captured_payload["offset"] == 100

    def test_query_uses_default_limit(self, utils):
        """Test that default limit is used when not specified."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()

        captured_payload = {}

        def capture_request(*args, **kwargs):
            captured_payload.update(kwargs.get('json', {}))
            return mock_response

        with patch('requests.post', side_effect=capture_request):
            utils.query("SELECT * FROM test")

        assert captured_payload["limit"] == 100  # default


class TestQueryContigs:
    """Test suite for query_contigs method."""

    @pytest.fixture
    def utils(self, temp_dir):
        """Create KBBERDLUtils instance with token."""
        token_file = Path(temp_dir) / "tokens"
        token_file.write_text("kbase=test_token\n")
        return KBBERDLUtils(
            token_file=token_file,
            kbase_token_file=Path(temp_dir) / "kbase_token"
        )

    def test_query_contigs_basic(self, utils):
        """Test basic contig query."""
        captured_sql = []

        def mock_query(sql, **kwargs):
            captured_sql.append(sql)
            return {"success": True, "data": [], "columns": [], "row_count": 0}

        with patch.object(utils, 'query', side_effect=mock_query):
            utils.query_contigs(limit=10)

        assert "kbase_genomes.contig" in captured_sql[0]
        assert "ORDER BY contig_id" in captured_sql[0]

    def test_query_contigs_with_filters(self, utils):
        """Test contig query with filters."""
        captured_sql = []

        def mock_query(sql, **kwargs):
            captured_sql.append(sql)
            return {"success": True, "data": [], "columns": [], "row_count": 0}

        with patch.object(utils, 'query', side_effect=mock_query):
            utils.query_contigs(filters={"length": 1000})

        assert "WHERE" in captured_sql[0]
        assert "length = 1000" in captured_sql[0]


class TestQueryOntologyStatements:
    """Test suite for query_ontology_statements method."""

    @pytest.fixture
    def utils(self, temp_dir):
        """Create KBBERDLUtils instance with token."""
        token_file = Path(temp_dir) / "tokens"
        token_file.write_text("kbase=test_token\n")
        return KBBERDLUtils(
            token_file=token_file,
            kbase_token_file=Path(temp_dir) / "kbase_token"
        )

    def test_query_ontology_with_subject_prefix(self, utils):
        """Test ontology query with subject prefix filter."""
        captured_sql = []

        def mock_query(sql, **kwargs):
            captured_sql.append(sql)
            return {"success": True, "data": [], "columns": [], "row_count": 0}

        with patch.object(utils, 'query', side_effect=mock_query):
            utils.query_ontology_statements(subject_prefix="seed.reaction:")

        assert "subject LIKE 'seed.reaction:%'" in captured_sql[0]

    def test_query_ontology_with_predicate(self, utils):
        """Test ontology query with predicate filter."""
        captured_sql = []

        def mock_query(sql, **kwargs):
            captured_sql.append(sql)
            return {"success": True, "data": [], "columns": [], "row_count": 0}

        with patch.object(utils, 'query', side_effect=mock_query):
            utils.query_ontology_statements(predicate="rdfs:label")

        assert "predicate = 'rdfs:label'" in captured_sql[0]


class TestGetReactionNames:
    """Test suite for get_reaction_names method."""

    @pytest.fixture
    def utils(self, temp_dir):
        """Create KBBERDLUtils instance with token."""
        token_file = Path(temp_dir) / "tokens"
        token_file.write_text("kbase=test_token\n")
        return KBBERDLUtils(
            token_file=token_file,
            kbase_token_file=Path(temp_dir) / "kbase_token"
        )

    def test_get_reaction_names_all(self, utils):
        """Test fetching all reaction names."""
        captured_sql = []

        def mock_query(sql, **kwargs):
            captured_sql.append(sql)
            return {"success": True, "data": [], "columns": [], "row_count": 0}

        with patch.object(utils, 'query', side_effect=mock_query):
            utils.get_reaction_names()

        assert "seed.reaction:" in captured_sql[0]
        assert "rdfs:label" in captured_sql[0]

    def test_get_reaction_names_specific_ids(self, utils):
        """Test fetching specific reaction names."""
        captured_sql = []

        def mock_query(sql, **kwargs):
            captured_sql.append(sql)
            return {"success": True, "data": [], "columns": [], "row_count": 0}

        with patch.object(utils, 'query', side_effect=mock_query):
            utils.get_reaction_names(reaction_ids=["rxn00001", "rxn00002"])

        assert "subject IN" in captured_sql[0]
        assert "seed.reaction:rxn00001" in captured_sql[0]
        assert "seed.reaction:rxn00002" in captured_sql[0]


class TestGetCompoundNames:
    """Test suite for get_compound_names method."""

    @pytest.fixture
    def utils(self, temp_dir):
        """Create KBBERDLUtils instance with token."""
        token_file = Path(temp_dir) / "tokens"
        token_file.write_text("kbase=test_token\n")
        return KBBERDLUtils(
            token_file=token_file,
            kbase_token_file=Path(temp_dir) / "kbase_token"
        )

    def test_get_compound_names_all(self, utils):
        """Test fetching all compound names."""
        captured_sql = []

        def mock_query(sql, **kwargs):
            captured_sql.append(sql)
            return {"success": True, "data": [], "columns": [], "row_count": 0}

        with patch.object(utils, 'query', side_effect=mock_query):
            utils.get_compound_names()

        assert "seed.compound:" in captured_sql[0]
        assert "rdfs:label" in captured_sql[0]


class TestPaginateQuery:
    """Test suite for paginate_query method."""

    @pytest.fixture
    def utils(self, temp_dir):
        """Create KBBERDLUtils instance with token."""
        token_file = Path(temp_dir) / "tokens"
        token_file.write_text("kbase=test_token\n")
        return KBBERDLUtils(
            token_file=token_file,
            kbase_token_file=Path(temp_dir) / "kbase_token"
        )

    def test_paginate_query_single_page(self, utils):
        """Test pagination with results fitting in one page."""
        call_count = [0]

        def mock_query(sql, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {
                    "success": True,
                    "data": [{"id": 1}, {"id": 2}],
                    "columns": ["id"],
                    "row_count": 2
                }
            return {
                "success": True,
                "data": [],
                "columns": ["id"],
                "row_count": 0
            }

        with patch.object(utils, 'query', side_effect=mock_query):
            result = utils.paginate_query("SELECT * FROM test", page_size=10)

        assert result["success"] is True
        assert result["row_count"] == 2
        assert result["pages_fetched"] == 1

    def test_paginate_query_multiple_pages(self, utils):
        """Test pagination across multiple pages."""
        call_count = [0]

        def mock_query(sql, **kwargs):
            call_count[0] += 1
            offset = kwargs.get("offset", 0)
            if offset == 0:
                return {
                    "success": True,
                    "data": [{"id": i} for i in range(10)],
                    "columns": ["id"],
                    "row_count": 10
                }
            elif offset == 10:
                return {
                    "success": True,
                    "data": [{"id": i} for i in range(10, 15)],
                    "columns": ["id"],
                    "row_count": 5
                }
            return {
                "success": True,
                "data": [],
                "columns": ["id"],
                "row_count": 0
            }

        with patch.object(utils, 'query', side_effect=mock_query):
            result = utils.paginate_query("SELECT * FROM test", page_size=10)

        assert result["success"] is True
        assert result["row_count"] == 15
        assert result["pages_fetched"] == 2

    def test_paginate_query_max_pages(self, utils):
        """Test pagination with max_pages limit."""
        def mock_query(sql, **kwargs):
            return {
                "success": True,
                "data": [{"id": 1}] * 10,
                "columns": ["id"],
                "row_count": 10
            }

        with patch.object(utils, 'query', side_effect=mock_query):
            result = utils.paginate_query(
                "SELECT * FROM test",
                page_size=10,
                max_pages=3
            )

        assert result["success"] is True
        assert result["pages_fetched"] == 3
        assert result["row_count"] == 30

    def test_paginate_query_error_handling(self, utils):
        """Test pagination error handling."""
        call_count = [0]

        def mock_query(sql, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {
                    "success": True,
                    "data": [{"id": 1}],
                    "columns": ["id"],
                    "row_count": 1
                }
            return {
                "success": False,
                "error": "Query failed",
                "data": [],
                "columns": [],
                "row_count": 0
            }

        with patch.object(utils, 'query', side_effect=mock_query):
            result = utils.paginate_query("SELECT * FROM test", page_size=1)

        assert result["success"] is False
        assert result["data"] == [{"id": 1}]  # Data from successful page


class TestTestConnection:
    """Test suite for test_connection method."""

    @pytest.fixture
    def utils(self, temp_dir):
        """Create KBBERDLUtils instance with token."""
        token_file = Path(temp_dir) / "tokens"
        token_file.write_text("kbase=test_token\n")
        return KBBERDLUtils(
            token_file=token_file,
            kbase_token_file=Path(temp_dir) / "kbase_token"
        )

    def test_test_connection_success(self, utils):
        """Test successful connection test."""
        def mock_query(sql, **kwargs):
            return {
                "success": True,
                "data": [{"test": 1}],
                "columns": ["test"],
                "row_count": 1
            }

        with patch.object(utils, 'query', side_effect=mock_query):
            result = utils.test_connection()

        assert result["success"] is True
        assert "Successfully connected" in result["message"]

    def test_test_connection_failure(self, utils):
        """Test failed connection test."""
        def mock_query(sql, **kwargs):
            return {
                "success": False,
                "error": "Connection refused",
                "data": [],
                "columns": [],
                "row_count": 0
            }

        with patch.object(utils, 'query', side_effect=mock_query):
            result = utils.test_connection()

        assert result["success"] is False
        assert "Connection failed" in result["message"]


@pytest.mark.integration
class TestKBBERDLIntegration:
    """Integration tests that require actual BERDL API access."""

    def test_real_query(self, temp_dir):
        """Test actual query against BERDL API."""
        # This test requires a valid KBase token with BERDL access
        kbase_token_file = Path.home() / ".kbase" / "token"
        if not kbase_token_file.exists():
            pytest.skip("No KBase token available")

        utils = KBBERDLUtils(
            token_file=Path(temp_dir) / "tokens",
            kbase_token_file=kbase_token_file
        )

        result = utils.test_connection()
        if not result["success"]:
            pytest.skip(f"Cannot connect to BERDL: {result['message']}")

        # Try a simple query
        result = utils.query(
            "SELECT * FROM kbase_genomes.contig LIMIT 3",
            limit=3
        )

        assert result["success"] is True
        assert result["row_count"] <= 3
