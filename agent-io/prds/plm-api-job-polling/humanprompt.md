# PLM API Job Polling Fix

## Original Issue

I found through testing that there is an error in how the kb_plm_utils module handles interactions with the server. Specifically, the `query_plm_api` function is assuming that the `plm_search_endpoint` function returns the results themselves, but it does not. Actually, this function only returns a job ID (e.g. `{'job_id': '8a4b996c-3eee-441b-a437-2991a6d0e1e2'}`).

A separate API function must be called providing the job ID as input, probably with some kind of polling, to get the actual results as output. This function is accessed through the result rest endpoint.

## Request

Can you modify the query_plm API function to retrieve the job ID and then poll for results in the results endpoint? We may want to consider adjusting the API to operate asynchronously (starting multiple queries at once, tracking job IDs, and then monitoring for job completion), but for now, let's go with a synchronous implementation (submitting one job and polling for completion within a single function call).
