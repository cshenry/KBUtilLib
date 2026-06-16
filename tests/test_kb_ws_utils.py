"""Unit tests for KBWSUtils workspace type management functions."""

import pytest
from unittest.mock import Mock, patch, MagicMock
from kbutillib.kb_ws_utils import KBWSUtils


@pytest.fixture
def mock_ws_client():
    """Create a mock workspace client with controlled test data."""
    client = MagicMock()

    # Mock data for list_all_types
    client.list_all_types.return_value = {
        'KBaseGenomes': {'Genome': '17.0', 'Pangenome': '4.0'},
        'KBaseFBA': {'FBAModel': '14.0', 'FBA': '7.0'},
        'KBaseGenomeAnnotations': {'Assembly': '5.0'}
    }

    # Mock data for get_type_info
    def mock_get_type_info(type_string):
        type_specs = {
            'KBaseGenomes.Genome': {
                'type_def': 'KBaseGenomes.Genome-17.0',
                'description': 'Genome object for KBase',
                'json_schema': {'type': 'object', 'properties': {}},
                'spec_def': 'module KBaseGenomes { typedef structure { ... } Genome; }',
                'module_vers': ['17.0'],
                'type_vers': ['17.0'],
            },
            'KBaseFBA.FBAModel': {
                'type_def': 'KBaseFBA.FBAModel-14.0',
                'description': 'FBA Model object',
                'json_schema': {'type': 'object', 'properties': {}},
                'spec_def': 'module KBaseFBA { typedef structure { ... } FBAModel; }',
                'module_vers': ['14.0'],
                'type_vers': ['14.0'],
            }
        }
        if type_string in type_specs:
            return type_specs[type_string]
        else:
            raise Exception(f"Type {type_string} not found")

    client.get_type_info.side_effect = mock_get_type_info

    return client


@pytest.fixture
def kb_utils(mock_ws_client, temp_dir):
    """Create KBWSUtils instance with mocked workspace client."""
    with patch('kbutillib.kb_ws_utils.Workspace', return_value=mock_ws_client):
        with patch('kbutillib.kb_ws_utils.HandleService'):
            # Use temp directory for safe token file operations
            utils = KBWSUtils(
                kb_version='prod',
                token_file=f"{temp_dir}/test_tokens",
                kbase_token_file=f"{temp_dir}/test_kbase_token"
            )
            utils._ws_client = mock_ws_client
            return utils


class TestListAllTypes:
    """Tests for the list_all_types() function."""

    def test_list_all_types_default_params(self, kb_utils, mock_ws_client):
        """Test list_all_types with default parameters."""
        result = kb_utils.list_all_types()

        # Verify API was called with correct parameters (converts bool to int)
        mock_ws_client.list_all_types.assert_called_once_with({'with_empty_modules': 0})

        # Verify result is a list
        assert isinstance(result, list)

        # Verify result contains expected types
        assert 'KBaseGenomes.Genome' in result
        assert 'KBaseGenomes.Pangenome' in result
        assert 'KBaseFBA.FBAModel' in result
        assert 'KBaseFBA.FBA' in result
        assert 'KBaseGenomeAnnotations.Assembly' in result

        # Verify count
        assert len(result) == 5

    def test_list_all_types_include_empty_modules_true(self, kb_utils, mock_ws_client):
        """Test list_all_types with include_empty_modules=True."""
        result = kb_utils.list_all_types(include_empty_modules=True)

        # Verify parameter is passed correctly to API (converts bool to int)
        mock_ws_client.list_all_types.assert_called_once_with({'with_empty_modules': 1})

        assert isinstance(result, list)
        assert len(result) == 5

    def test_list_all_types_include_empty_modules_false(self, kb_utils, mock_ws_client):
        """Test list_all_types with include_empty_modules=False."""
        result = kb_utils.list_all_types(include_empty_modules=False)

        # Verify parameter is passed correctly to API (converts bool to int)
        mock_ws_client.list_all_types.assert_called_once_with({'with_empty_modules': 0})

        assert isinstance(result, list)

    def test_list_all_types_error_handling(self, kb_utils, mock_ws_client):
        """Test list_all_types error handling when API raises exception."""
        mock_ws_client.list_all_types.side_effect = Exception("API connection failed")

        with pytest.raises(Exception) as exc_info:
            kb_utils.list_all_types()

        assert "Failed to list all types from workspace" in str(exc_info.value)
        assert "API connection failed" in str(exc_info.value)

    def test_list_all_types_empty_result(self, kb_utils, mock_ws_client):
        """Test list_all_types returns empty list when API returns empty dict."""
        mock_ws_client.list_all_types.return_value = {}

        result = kb_utils.list_all_types()

        assert isinstance(result, list)
        assert len(result) == 0

    def test_list_all_types_logging(self, kb_utils, caplog):
        """Test that list_all_types generates appropriate log messages."""
        import logging
        caplog.set_level(logging.INFO)

        kb_utils.list_all_types()

        # Check for INFO level log messages
        assert any("Retrieving all types from workspace" in record.message for record in caplog.records)
        assert any("Successfully retrieved" in record.message for record in caplog.records)


class TestGetTypeSpecs:
    """Tests for the get_type_specs() function."""

    def test_get_type_specs_single_type(self, kb_utils, mock_ws_client):
        """Test get_type_specs with a single valid type."""
        result = kb_utils.get_type_specs(['KBaseGenomes.Genome'])

        # Verify it's a dict
        assert isinstance(result, dict)

        # Verify the type is in the result
        assert 'KBaseGenomes.Genome' in result

        # Verify structure
        spec = result['KBaseGenomes.Genome']
        assert 'type_def' in spec
        assert 'description' in spec
        assert 'json_schema' in spec
        assert 'spec_def' in spec
        assert spec['type_def'] == 'KBaseGenomes.Genome-17.0'

    def test_get_type_specs_multiple_types(self, kb_utils, mock_ws_client):
        """Test get_type_specs with multiple valid types."""
        result = kb_utils.get_type_specs(['KBaseGenomes.Genome', 'KBaseFBA.FBAModel'])

        # Verify both types are retrieved
        assert len(result) == 2
        assert 'KBaseGenomes.Genome' in result
        assert 'KBaseFBA.FBAModel' in result

        # Verify each has correct structure
        for type_string in ['KBaseGenomes.Genome', 'KBaseFBA.FBAModel']:
            assert 'type_def' in result[type_string]
            assert 'description' in result[type_string]
            assert 'json_schema' in result[type_string]

    def test_get_type_specs_empty_list(self, kb_utils):
        """Test get_type_specs with empty list input raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            kb_utils.get_type_specs([])

        assert "type_list cannot be empty" in str(exc_info.value)

    def test_get_type_specs_non_list_string(self, kb_utils):
        """Test get_type_specs with string input raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            kb_utils.get_type_specs("KBaseGenomes.Genome")

        assert "type_list must be a list" in str(exc_info.value)

    def test_get_type_specs_non_list_none(self, kb_utils):
        """Test get_type_specs with None input raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            kb_utils.get_type_specs(None)

        assert "type_list must be a list" in str(exc_info.value)

    def test_get_type_specs_nonexistent_type(self, kb_utils, mock_ws_client):
        """Test get_type_specs with non-existent type raises clear error."""
        with pytest.raises(Exception) as exc_info:
            kb_utils.get_type_specs(['NonExistent.Type'])

        assert "Failed to retrieve spec for type 'NonExistent.Type'" in str(exc_info.value)
        assert "Type NonExistent.Type not found" in str(exc_info.value)

    def test_get_type_specs_partial_failure(self, kb_utils, mock_ws_client):
        """Test get_type_specs partial failure scenario indicates which type failed."""
        with pytest.raises(Exception) as exc_info:
            kb_utils.get_type_specs(['KBaseGenomes.Genome', 'Invalid.Type'])

        # Error should indicate which type failed
        assert "Failed to retrieve spec for type 'Invalid.Type'" in str(exc_info.value)

    def test_get_type_specs_logging(self, kb_utils, caplog):
        """Test get_type_specs generates appropriate log messages."""
        import logging
        caplog.set_level(logging.INFO)

        kb_utils.get_type_specs(['KBaseGenomes.Genome'])

        # Check for INFO level log messages
        assert any("Retrieving type specifications for" in record.message for record in caplog.records)
        assert any("Successfully retrieved" in record.message for record in caplog.records)

    def test_get_type_specs_provenance_tracking(self, kb_utils):
        """Test get_type_specs with track_provenance=True calls initialize_call."""
        with patch.object(kb_utils, 'initialize_call') as mock_init_call:
            kb_utils.get_type_specs(['KBaseGenomes.Genome'], track_provenance=True)

            # Verify initialize_call was called
            mock_init_call.assert_called_once_with(
                "get_type_specs",
                {"type_list": ['KBaseGenomes.Genome']}
            )

    def test_list_all_types_provenance_tracking(self, kb_utils):
        """Test list_all_types with track_provenance=True calls initialize_call."""
        with patch.object(kb_utils, 'initialize_call') as mock_init_call:
            kb_utils.list_all_types(track_provenance=True)

            # Verify initialize_call was called
            mock_init_call.assert_called_once_with(
                "list_all_types",
                {"include_empty_modules": False}
            )


@pytest.mark.integration
class TestIntegration:
    """Integration tests that could use real workspace (skip by default)."""

    @pytest.mark.skip(reason="Requires real KBase workspace connection")
    def test_list_all_types_real_workspace(self):
        """Integration test with real workspace (manual testing only)."""
        utils = KBWSUtils(kb_version='appdev')
        result = utils.list_all_types()

        assert isinstance(result, list)
        assert len(result) > 0
        assert all('.' in type_str for type_str in result)

    @pytest.mark.skip(reason="Requires real KBase workspace connection")
    def test_get_type_specs_real_workspace(self):
        """Integration test with real workspace (manual testing only)."""
        utils = KBWSUtils(kb_version='appdev')
        result = utils.get_type_specs(['KBaseGenomes.Genome'])

        assert isinstance(result, dict)
        assert 'KBaseGenomes.Genome' in result
        assert 'json_schema' in result['KBaseGenomes.Genome']

