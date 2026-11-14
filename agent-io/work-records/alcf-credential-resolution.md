# alcf-credential-resolution

- task_id: alcf-credential-resolution
- branch: maestro/developer/kbu-alcf-alcf-credential-resolution
- commit_shas: 430c7fc (feature + tests; see git log for the work-record commit)

## summary
`resolve_credentials` in council_run.py now handles `kind == "alcf"`: it runs the endpoint's token source once (via a new module-level `resolve_alcf_token(options, name=)` in llm/apis/alcf.py, which `AlcfBackend._bearer` now delegates to, behaviour unchanged) and appends a `globus_token` row naming the source, or raises `RunRefused("alcf_token", ...)`. `CREDENTIAL_CHECKS` gains `alcf_token` (appended), and a docstring bullet was added.

## files_touched
- src/kbutillib/domains/ai/llm/apis/alcf.py
- src/kbutillib/domains/genome/ontology_mapping/council_run.py
- tests/domains/test_ontomap_alcf_credentials.py (new)
- tests/domains/ai/llm/test_alcf_backend.py NOT touched

## success_criteria_check
- alcf_token in CREDENTIAL_CHECKS: pass
- resolvable source -> row requires=globus_token, name=source, no token: pass (tests)
- unresolvable -> RunRefused check alcf_token, message has endpoint and 'alcf-tokens login': pass
- no base-passing test fails: pass (full suite)

## tests_run
- full suite baseline command: 4 failed (exactly the 4 known), 7972 passed, 576 skipped, 451 s.
- targeted: new file 6 pass; test_alcf_backend.py and anthropic credentials tests pass.

## caveats
- The refusal message quotes the source name (argv joined / import path) and the AuthError text; argv must carry no credential (already a documented rule).
- Resolution goes through the backend's in-process token cache (TTL 300 s), so the preflight run also warms the cache for the backend; only successes are cached.
- Default command (`alcf-tokens get-token inference`) is used and reported when neither option is set, source `token_command`.
- Kept `AlcfBackend._auth_error` as a thin method for compatibility.
