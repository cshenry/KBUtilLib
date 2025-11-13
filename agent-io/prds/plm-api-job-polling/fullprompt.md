# PLM API Job Polling Implementation - Complete PRD

## Overview

This PRD documents the fix for the PLM API interaction in the KBUtilLib project. The `query_plm_api` function was incorrectly treating the PLM search endpoint as synchronous when it actually returns a job ID and requires polling for results.

## Problem Statement

### Current (Broken) Behavior
The `query_plm_api` function in [src/kbutillib/kb_plm_utils.py](src/kbutillib/kb_plm_utils.py) made a single POST request to the `/search` endpoint and expected to receive the complete search results in the response.

### Actual API Behavior
The PLM API follows an asynchronous job pattern:
1. Client submits search request to `/search` endpoint
2. Server responds immediately with a job ID: `{'job_id': '<uuid>'}`
3. Client must poll `/result/<job_id>` endpoint to check job status
4. When job completes, client retrieves results from the result endpoint

### Impact
Without proper job polling, the `query_plm_api` function would fail to retrieve results, causing downstream functions like `find_best_hits_for_features` to fail.

## Solution

### Implementation Approach
Implement synchronous job polling within the `query_plm_api` function. While asynchronous batch processing would be more efficient for multiple queries, the synchronous approach is simpler and sufficient for current use cases.

### Modified Architecture

#### New Endpoint Configuration
Updated `__init__` to include result endpoint:
```python
self.plm_search_endpoint = f"{self.plm_api_url}/search"
self.plm_result_endpoint = f"{self.plm_api_url}/result"
```

#### Updated Function Signature
```python
def query_plm_api(
    self,
    query_sequences: List[Dict[str, str]],
    max_hits: int = 100,
    similarity_threshold: float = 0.0,
    return_embeddings: bool = False,
    poll_interval: float = 2.0,        # New: configurable polling interval
    max_wait_time: float = 300.0       # New: maximum wait time for results
) -> Dict[str, Any]:
```

#### Two-Phase Execution

**Phase 1: Job Submission**
- POST request to `/search` endpoint
- Extract `job_id` from response
- Validate that job_id exists in response
- Short timeout (30s) for submission
- Raise ValueError if job_id missing

**Phase 2: Result Polling**
- **IMPORTANT**: Use POST (not GET) to `/result` endpoint
- POST body: `{"job_id": "<job_id>"}`
- Poll at `poll_interval` (default 2.0s)
- Track elapsed time
- Handle multiple response scenarios:
  - **HTTP 200 with status field**: Check status value
    - "done": Return the `result` field (job completed successfully)
    - "failed": Raise RuntimeError with error message from `error` field
    - "pending" or "running": Continue polling
    - Unknown status: Log warning and continue
  - **HTTP 202**: Job still processing, continue polling
  - **Other status codes**: Raise for status
- Timeout if elapsed_time exceeds max_wait_time
- Raise TimeoutError with job_id if timeout occurs

**Response Structure** (from OpenAPI spec):
```json
{
  "status": "done|pending|running|failed",
  "result": {
    "hits": [...]  // Actual search results
  },
  "error": "error message if failed"
}
```

### Error Handling

#### Job Submission Errors
- **Timeout**: Log error and raise requests.Timeout
- **HTTP errors**: Log error and raise requests.RequestException
- **Missing job_id**: Raise ValueError with descriptive message

#### Polling Errors
- **Job failure**: Raise RuntimeError with server-provided error message
- **Timeout**: Raise TimeoutError after max_wait_time
- **Network errors**: Log warning and continue polling (transient errors)
- **HTTP errors**: Log error and raise

### Logging Strategy

- **INFO level**:
  - Query submission with sequence count and parameters
  - Job submission success with job_id
  - Polling start with URL and parameters
  - Job completion with elapsed time

- **DEBUG level**:
  - Job status during polling (pending, running)
  - HTTP 202 responses

- **WARNING level**:
  - Unknown job status values
  - Timeout during polling attempts (transient)

- **ERROR level**:
  - Job submission failures
  - Polling errors that will be raised

## Implementation Details

### Files Modified

1. **[src/kbutillib/kb_plm_utils.py](src/kbutillib/kb_plm_utils.py:74-220)**
   - Added `time` import (line 11)
   - Added `plm_result_endpoint` to `__init__` (line 44)
   - Completely rewrote `query_plm_api` function (lines 74-220)
   - **Key changes**:
     - Changed result polling from GET to POST
     - Changed POST body to include `{"job_id": job_id}`
     - Updated status checking from "completed" to "done"
     - Return `result` field from response, not entire response
   - Lines changed: ~65 lines added/modified

### Files Created

2. **[tests/test_kb_plm_utils.py](tests/test_kb_plm_utils.py)**
   - New comprehensive test suite
   - 8 test cases covering all scenarios
   - Uses mocking for API calls and time.sleep
   - Tests both success and failure paths

### Dependencies
- No new dependencies added
- Uses existing: `requests`, `time` (stdlib)

## Testing

### Test Coverage

Created comprehensive unit tests in `tests/test_kb_plm_utils.py`:

1. **test_successful_job_completion**: Basic happy path
2. **test_job_polling_with_pending_status**: State transitions
3. **test_job_failure**: Server-side failure handling
4. **test_job_timeout**: Client-side timeout handling
5. **test_missing_job_id_in_response**: Invalid server response
6. **test_http_202_status_code**: HTTP 202 during polling
7. **test_result_without_status_field**: Backward compatibility
8. **test_validation_errors**: Input validation

### Test Results
All 8 tests pass successfully:
```
tests/test_kb_plm_utils.py::TestQueryPLMAPI::test_successful_job_completion PASSED
tests/test_kb_plm_utils.py::TestQueryPLMAPI::test_job_polling_with_pending_status PASSED
tests/test_kb_plm_utils.py::TestQueryPLMAPI::test_job_failure PASSED
tests/test_kb_plm_utils.py::TestQueryPLMAPI::test_job_timeout PASSED
tests/test_kb_plm_utils.py::TestQueryPLMAPI::test_missing_job_id_in_response PASSED
tests/test_kb_plm_utils.py::TestQueryPLMAPI::test_http_202_status_code PASSED
tests/test_kb_plm_utils.py::TestQueryPLMAPI::test_result_without_status_field PASSED
tests/test_kb_plm_utils.py::TestQueryPLMAPI::test_validation_errors PASSED
```

## Configuration Parameters

### Default Values
- `poll_interval`: 2.0 seconds
  - Rationale: Balance between responsiveness and server load
  - Can be reduced for faster response or increased to reduce server requests

- `max_wait_time`: 300.0 seconds (5 minutes)
  - Rationale: Allows time for large protein sequence searches
  - Prevents indefinite hanging on stuck jobs
  - Can be adjusted based on typical job duration

### Tuning Guidelines
- For small queries (1-10 sequences): Consider `poll_interval=1.0`
- For large queries (50-100 sequences): May need `max_wait_time=600.0`
- For development/testing: Use `poll_interval=0.1` for faster tests

## Future Enhancements

### Asynchronous Batch Processing
The current implementation is synchronous (one job at a time). Future versions could implement:

1. **Batch Job Submission**
   - Submit multiple queries concurrently
   - Track all job IDs in a list/dict

2. **Concurrent Polling**
   - Poll all jobs simultaneously using threading or asyncio
   - Return results as they complete

3. **Progress Callbacks**
   - Notify caller as individual jobs complete
   - Useful for processing large batches

4. **Job Management**
   - Save job IDs for later retrieval
   - Resume polling after interruption
   - Cancel jobs

### Example Future API
```python
# Submit multiple jobs
job_ids = plm_utils.submit_plm_jobs_batch(query_sequences_list)

# Poll for results with callback
results = plm_utils.poll_jobs_async(
    job_ids,
    on_complete=lambda job_id, result: print(f"Job {job_id} done")
)
```

## Backward Compatibility

The changes are backward compatible:
- Existing function parameters unchanged (new parameters have defaults)
- Return value structure unchanged
- Existing callers will work without modification
- New parameters are optional and have sensible defaults

## Risk Assessment

### Low Risk
- Well-tested with comprehensive unit tests
- Follows existing patterns (similar to argo_utils.py polling)
- Backward compatible
- Clear error handling

### Potential Issues
- Network instability could cause spurious timeouts
  - Mitigation: Transient errors continue polling
- Server changes to response format
  - Mitigation: Handles both with/without status field
- Very long-running jobs might timeout
  - Mitigation: max_wait_time is configurable

## Success Criteria

✅ Function correctly submits jobs and retrieves job IDs
✅ Polling loop handles all status states correctly
✅ Errors are properly raised and logged
✅ All unit tests pass
✅ Backward compatible with existing code
✅ Documentation updated with new parameters
✅ Performance acceptable for typical use cases

## References

- Implementation pattern based on [src/kbutillib/argo_utils.py:345-375](src/kbutillib/argo_utils.py#L345-L375)
- PLM API documentation: https://kbase.us/services/llm_homology_api
- Related function: `find_best_hits_for_features` in [src/kbutillib/kb_plm_utils.py:355-563](src/kbutillib/kb_plm_utils.py#L355-L563)
