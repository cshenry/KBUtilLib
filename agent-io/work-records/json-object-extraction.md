# json-object-extraction

- task_id: json-object-extraction (PRD kbu-alcf-api-type-v1, phase 2)
- branch: maestro/developer/kbu-alcf-json-object-extraction
- commit_shas: 22347ea (code + tests; full SHA via git log), plus the trailing work-record commit

## summary
http_common.py: `extract_json_object` is now a thin wrapper over new `scan_json_objects`, which raw_decodes at the FIRST '{' only (failure => zero objects, no inner-brace salvage), then continues past each decoded object; a later decode failure after a success is tolerated as prose; 2+ objects => obj None with the count. The result is an immutable `JsonScan(obj, count, labels)` returned by value (no global/backend state); labels are bounded (40-char, printable-ASCII, max 5 objects, "<empty object>" label). `HttpBackend._complete` reports ambiguity via `describe_ambiguity` (count, first keys, "ambiguous ... nothing was guessed") with kind still invalid_output; anthropic.py uses the same scan/message. Inside the content-is-None branch, finish_reason=="length" now reports the token-ceiling truncation with truncated=True (refusal screen still first).

## files_touched
- src/kbutillib/domains/ai/llm/apis/http_common.py
- src/kbutillib/domains/ai/llm/apis/anthropic.py
- tests/domains/ai/llm/test_json_object_extraction.py (new)
- agent-io/work-records/json-object-extraction.md

## success_criteria_check
- 8+ row old-vs-new identical-dict table in test file: pass (10 rows, old impl embedded verbatim).
- Two-object via _complete/MockTransport: ok False, invalid_output, count in message: pass.
- content null + length => truncated True, ceiling named; no finish_reason => no-content message, truncated False: pass.
- Synthetic-fixture comment: pass (module docstring and fixture comment).
- Truncated '{"picked": [{"go_id":"GO:1"}]' => None: pass.
- Existing test_http_backends.py one-in-list/two-in-list cases unmodified and passing: pass.
- Count returned not stored: pass (frozen dataclass; test).
- anthropic.py diagnostic names count: implemented; its end-to-end test is SKIPPED in this environment (anthropic SDK / httpx2 absent), so uncertain-by-execution.
- Malformed-brace-between-objects returns ONE, labelled known limitation: pass.
- Bounded/sanitised key excerpts: pass.
- No base-passing test fails: pass (below).

## tests_run
- Targeted: pytest tests/domains/ai/llm tests/domains/ai/council/test_http_backends.py: 265 passed, 68 skipped.
- Full baseline command: 3 failed, 7849 passed, 576 skipped in 504 s. The 3 failures are exactly the baseline's tests/interfaces/test_mcp_server.py::TestUnavailableHandler ones (by node id).

## caveats
- Does not prevent retry waste: ambiguity stays invalid_output, so a deterministic two-object model still costs the same attempts. It buys diagnosis only.
- Widening: text like '{"a":1} stray }' (old: failure) now yields one object; all old successes are unchanged.
- Anthropic end-to-end test could not execute here (skipped).
