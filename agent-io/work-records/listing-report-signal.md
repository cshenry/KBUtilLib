# listing-report-signal

- task_id: listing-report-signal (PRD kbu-alcf-api-type-v1, phase 3)
- branch: maestro/developer/kbu-alcf-listing-report-signal
- commit_shas: fbdfa4b (feature + tests; see `git log f3dba95..HEAD` for the work-record commit)

## summary
`HttpBackend.models_report()` and `AnthropicBackend.models_report()` return `{models, outcome, detail}` with outcome in answered / not_implemented / credential_rejected / http_error / unreachable / malformed; `models()` on both is now the models-only wrapper. `probe_endpoint` reads the report through getattr (absent -> `answered`) and carries `listing_outcome` / `listing_detail`; `seat_state` gives ready/unavailable only for answered and unreachable and `unknown` otherwise, and matches ids across a trailing `-YYYYMMDD` stamp (`served_name_matches`, no prefix/fuzzy). The rehearsal script's check 4 moved into `check_listing()`, which calls `models_report()` and makes a naming claim only on `answered`.

## files_touched
- src/kbutillib/domains/ai/llm/apis/http_common.py (models_report, LISTING_OUTCOMES)
- src/kbutillib/domains/ai/llm/apis/anthropic.py (models_report)
- src/kbutillib/domains/ai/council/servable.py (probe fields, outcome verdicts, served_name_matches)
- scripts/council/anthropic_acceptance_rehearsal.py (check_listing)
- tests/domains/ai/llm/test_models_report.py (new)
- tests/domains/ai/council/test_servable_listing_outcome.py (new)

## success_criteria_check
- HttpBackend outcomes per status/exception, models() unchanged: pass (20 MockTransport cases + token failure).
- AnthropicBackend mapping, models() wrapper: pass, tested over a stand-in SDK (see caveats).
- Rehearsal check 4 calls models_report, no naming finding on credential_rejected, one on answered: pass (check_listing tests).
- seat_state over all six outcomes: pass. Alias cases incl. opus-4-5 vs opus-4-5-1: pass.
- models()-only api type treated as answered; 'Lists NOTHING' test unmodified and passing: pass.
- No servable test needed updating: none in test_servable_and_query_dirs.py uses a real HttpBackend; file untouched, nothing deleted.

## tests_run
- Targeted: llm/, council/test_servable*, test_http_backends, test_anthropic_rehearsal_script: 358 passed (before extra tests), new files 42 + 26 pass.
- Full baseline command, 600000 ms: 3 failed, 7917 passed, 576 skipped in 427 s. The 3 failures are exactly the baseline test_mcp_server TestUnavailableHandler ids.

## caveats
- The `anthropic` SDK is not installed in the KBUtilLib-py3.13 venv, so real-SDK tests are skipped; AnthropicBackend.models_report was tested against a stand-in SDK namespace (exception classes + client.models.list) and not against the real SDK's exceptions.
- Judgment calls: a non-connect/non-timeout httpx exception (e.g. RemoteProtocolError) and any unexpected failure are `malformed` (consistent with the Anthropic "other client failure" rule); a non-2xx status below 400 (3xx) is `http_error`; a mapping entry whose id/name/internal_id is non-string, or any non-mapping entry, makes the outcome `malformed` while `models` still holds the mapping entries exactly as before.
- Existing servable tests did not need changes, so no in-file "deliberate change" comment was added.
