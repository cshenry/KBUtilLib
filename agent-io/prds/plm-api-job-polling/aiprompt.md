# PLM API Job Polling Implementation

## Problem Statement

The `query_plm_api` function in `kb_plm_utils.py` incorrectly assumes that the PLM search endpoint returns results synchronously. In reality, the endpoint follows an asynchronous job pattern:

1. Submit a search request to `/search` endpoint
2. Receive a job ID response: `{'job_id': '<uuid>'}`
3. Poll the `/result/<job_id>` endpoint until the job completes
4. Retrieve the final results

## Solution Approach

Implement synchronous job polling within the `query_plm_api` function:

1. **Job Submission**: POST to `/search` endpoint with query parameters
2. **Extract Job ID**: Parse the job_id from the response
3. **Poll for Results**:
   - GET `/result/<job_id>` at regular intervals
   - Check response status (200, 202, etc.)
   - Handle job status field if present (pending, running, completed, failed)
   - Continue polling until completion or timeout
4. **Error Handling**:
   - Handle job failures
   - Timeout if job doesn't complete within max_wait_time
   - Handle network errors during polling

## Implementation Details

### Modified Function Signature
```python
def query_plm_api(
    self,
    query_sequences: List[Dict[str, str]],
    max_hits: int = 100,
    similarity_threshold: float = 0.0,
    return_embeddings: bool = False,
    poll_interval: float = 2.0,        # New parameter
    max_wait_time: float = 300.0       # New parameter
) -> Dict[str, Any]:
```

### Polling Logic
- Poll every `poll_interval` seconds (default: 2.0s)
- Maximum wait time: `max_wait_time` seconds (default: 300s)
- Handle multiple status indicators:
  - HTTP 200 with status field: check for "completed", "failed", "pending", "running"
  - HTTP 202: job still processing
  - HTTP 404: job not found
  - HTTP 200 without status field: assume completed

### Future Enhancement Considerations
While this implementation is synchronous, the architecture allows for future async improvements:
- Submit multiple jobs concurrently
- Track job IDs in a queue
- Poll multiple jobs simultaneously
- Return results as they become available

## Testing Strategy

Comprehensive unit tests covering:
1. Successful job completion
2. Job polling with state transitions (pending → running → completed)
3. Job failure handling
4. Timeout scenarios
5. Missing job_id in response
6. HTTP 202 status code handling
7. Results without status field
8. Input validation
