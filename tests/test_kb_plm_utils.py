"""Unit tests for KBPLMUtils PLM API interaction functions."""

import pytest
from unittest.mock import Mock, patch, MagicMock
from kbutillib.kb_plm_utils import KBPLMUtils


@pytest.fixture
def plm_utils():
    """Create a KBPLMUtils instance for testing."""
    with patch('kbutillib.kb_plm_utils.subprocess.run'):
        utils = KBPLMUtils()
    return utils


class TestQueryPLMAPI:
    """Test the query_plm_api function with job polling."""

    def test_successful_job_completion(self, plm_utils):
        """Test successful job submission and completion."""
        mock_job_response = {"job_id": "test-job-123"}
        mock_result_response = {
            "status": "done",
            "result": {
                "hits": [
                    {
                        "query_id": "protein1",
                        "hits": [
                            {"id": "UniProt1", "score": 0.95},
                            {"id": "UniProt2", "score": 0.85}
                        ]
                    }
                ]
            },
            "error": None
        }

        with patch('requests.post') as mock_post, \
             patch('time.sleep'):  # Mock sleep to speed up test

            # Mock job submission (first POST call)
            # Mock result polling (second POST call)
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.side_effect = [
                mock_job_response,
                mock_result_response
            ]
            mock_post.return_value.raise_for_status = Mock()

            query_sequences = [{"id": "protein1", "sequence": "MKTEST"}]
            result = plm_utils.query_plm_api(query_sequences)

            # Verify the result (should be the "result" field from the response)
            assert result == mock_result_response["result"]
            assert "hits" in result

            # Verify job submission was called
            assert mock_post.call_count == 2  # Once for submission, once for result

            # Verify first call was to search endpoint
            first_call = mock_post.call_args_list[0]
            assert first_call[0][0] == plm_utils.plm_search_endpoint

            # Verify second call was to result endpoint with job_id
            second_call = mock_post.call_args_list[1]
            assert second_call[0][0] == plm_utils.plm_result_endpoint
            assert second_call[1]["json"] == {"job_id": "test-job-123"}

    def test_job_polling_with_pending_status(self, plm_utils):
        """Test job polling when job is initially pending then completes."""
        mock_job_response = {"job_id": "test-job-456"}
        mock_pending_result = {"status": "pending", "result": None, "error": None}
        mock_running_result = {"status": "running", "result": None, "error": None}
        mock_completed_result = {
            "status": "done",
            "result": {"hits": [{"query_id": "protein1", "hits": []}]},
            "error": None
        }

        with patch('requests.post') as mock_post, \
             patch('time.sleep'):

            # Mock job submission and result polling - all use POST now
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.side_effect = [
                mock_job_response,
                mock_pending_result,
                mock_running_result,
                mock_completed_result
            ]
            mock_post.return_value.raise_for_status = Mock()

            query_sequences = [{"id": "protein1", "sequence": "MKTEST"}]
            result = plm_utils.query_plm_api(query_sequences, poll_interval=0.1)

            # Verify final result
            assert result == mock_completed_result["result"]

            # Verify polling was called multiple times (1 submit + 3 polls)
            assert mock_post.call_count == 4

    def test_job_failure(self, plm_utils):
        """Test handling of job failure."""
        mock_job_response = {"job_id": "test-job-789"}
        mock_failed_result = {
            "status": "failed",
            "result": None,
            "error": "Processing error occurred"
        }

        with patch('requests.post') as mock_post, \
             patch('time.sleep'):

            # Mock job submission and result polling
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.side_effect = [
                mock_job_response,
                mock_failed_result
            ]
            mock_post.return_value.raise_for_status = Mock()

            query_sequences = [{"id": "protein1", "sequence": "MKTEST"}]

            with pytest.raises(RuntimeError, match="PLM job failed"):
                plm_utils.query_plm_api(query_sequences)

    def test_job_timeout(self, plm_utils):
        """Test handling of job timeout."""
        mock_job_response = {"job_id": "test-job-timeout"}
        mock_pending_result = {"status": "pending", "result": None, "error": None}

        with patch('requests.post') as mock_post, \
             patch('time.sleep'):

            # Mock job submission
            mock_post_response = Mock()
            mock_post_response.status_code = 200
            mock_post_response.raise_for_status = Mock()

            # First call returns job_id, subsequent calls return pending
            def json_side_effect():
                if mock_post.call_count == 1:
                    return mock_job_response
                else:
                    return mock_pending_result

            mock_post_response.json.side_effect = json_side_effect
            mock_post.return_value = mock_post_response

            query_sequences = [{"id": "protein1", "sequence": "MKTEST"}]

            with pytest.raises(TimeoutError, match="did not complete"):
                plm_utils.query_plm_api(
                    query_sequences,
                    poll_interval=0.1,
                    max_wait_time=0.5
                )

    def test_missing_job_id_in_response(self, plm_utils):
        """Test handling when job_id is missing from submission response."""
        mock_invalid_response = {"status": "accepted"}  # Missing job_id

        with patch('requests.post') as mock_post:
            # Mock job submission with invalid response
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = mock_invalid_response
            mock_post.return_value.raise_for_status = Mock()

            query_sequences = [{"id": "protein1", "sequence": "MKTEST"}]

            with pytest.raises(ValueError, match="Expected 'job_id'"):
                plm_utils.query_plm_api(query_sequences)

    def test_http_202_status_code(self, plm_utils):
        """Test handling of HTTP 202 (Accepted) during polling."""
        mock_job_response = {"job_id": "test-job-202"}
        mock_completed_result = {
            "status": "done",
            "result": {"hits": []},
            "error": None
        }

        with patch('requests.post') as mock_post, \
             patch('time.sleep'):

            # Mock job submission - returns job_id
            post_response_submit = Mock()
            post_response_submit.status_code = 200
            post_response_submit.json.return_value = mock_job_response
            post_response_submit.raise_for_status = Mock()

            # Mock result polling - return 202 then 200
            post_response_202 = Mock()
            post_response_202.status_code = 202

            post_response_200 = Mock()
            post_response_200.status_code = 200
            post_response_200.json.return_value = mock_completed_result

            mock_post.side_effect = [post_response_submit, post_response_202, post_response_200]

            query_sequences = [{"id": "protein1", "sequence": "MKTEST"}]
            result = plm_utils.query_plm_api(query_sequences, poll_interval=0.1)

            # Verify completion
            assert result == mock_completed_result["result"]
            assert mock_post.call_count == 3  # 1 submit + 2 polls

    def test_result_with_done_status(self, plm_utils):
        """Test handling when result has 'done' status."""
        mock_job_response = {"job_id": "test-job-done"}
        mock_result = {
            "status": "done",
            "result": {
                "hits": [
                    {
                        "query_id": "protein1",
                        "hits": [{"id": "UniProt1", "score": 0.95}]
                    }
                ]
            },
            "error": None
        }

        with patch('requests.post') as mock_post, \
             patch('time.sleep'):

            # Mock job submission and result
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.side_effect = [
                mock_job_response,
                mock_result
            ]
            mock_post.return_value.raise_for_status = Mock()

            query_sequences = [{"id": "protein1", "sequence": "MKTEST"}]
            result = plm_utils.query_plm_api(query_sequences)

            # Verify the result
            assert result == mock_result["result"]
            assert "hits" in result

    def test_validation_errors(self, plm_utils):
        """Test input validation."""
        # Empty query sequences
        with pytest.raises(ValueError, match="cannot be empty"):
            plm_utils.query_plm_api([])

        # Invalid max_hits
        with pytest.raises(ValueError, match="must be between 1 and 100"):
            plm_utils.query_plm_api(
                [{"id": "p1", "sequence": "MK"}],
                max_hits=0
            )

        with pytest.raises(ValueError, match="must be between 1 and 100"):
            plm_utils.query_plm_api(
                [{"id": "p1", "sequence": "MK"}],
                max_hits=101
            )
