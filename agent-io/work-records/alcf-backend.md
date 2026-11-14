# Work record: alcf-backend

- task_id: alcf-backend (PRD kbu-alcf-api-type-v1, phase 4)
- branch: maestro/developer/kbu-alcf-alcf-backend
- commit_shas: see `git log e611775..HEAD` (feature commit, fix commit, this record)

## summary
Adds the `alcf` api type: `AlcfBackend(OpenAICompatibleBackend)` in `src/kbutillib/domains/ai/llm/apis/alcf.py`, registered via `_make_alcf` and added (sorted) to `BUILTIN_API_TYPES`. URL derived from `options.cluster/framework/service_root`; `models_report()` (not only `models`) reads `{service_root}/list-endpoints`; `_bearer()` runs a per-call token source (command or import) in a child process, cached process-wide; `_body()` adds `response_format` (strict only when schema is strict-compatible). No change to route_fingerprint, prompt_hash, _seat_request, _effective_params, or `_status_error`.

## files_touched
- src/kbutillib/domains/ai/llm/apis/alcf.py (new)
- src/kbutillib/domains/ai/llm/__init__.py (factory, registration, tuple)
- tests/domains/ai/llm/test_alcf_backend.py (new, 29 tests)
- tests/domains/ai/llm/test_llm_wrapper.py (two hardcoded built-in-name lists gained "alcf")

## success_criteria_check
All criteria in the prompt: pass (each has a test in test_alcf_backend.py). Caveat on the real go_mf@4 META schema below.

## tests_run
See the final message for counts; baseline command from the worktree root.

## caveats
- go_mf@4 is not packaged in this repo (builtin is go_mf@1, whose META has additionalProperties false). The "go_mf@4 META" test uses the builtin META schema with additionalProperties set true (the one documented v4 change). The proposer test uses the real builtin proposer schema.
- Strict-compatibility = additionalProperties false at every object level + none of a forbidden-construct set (oneOf/allOf/not/if/$ref/patternProperties/...). It does NOT require all properties to be `required` (the real proposer has optional `notes` and must come out strict).
- test_llm_wrapper.py::TestBuiltins::test_importing_the_wrapper_does_not_import_a_provider_module runs a subprocess that imports the EDITABLE INSTALL (Dropbox checkout on wip), not the worktree; until main's change reaches that checkout it sees five api types and fails under the baseline command. It passes with PYTHONPATH=src.
- `SeatSpec.endpoint_kind` is a Literal that excludes "alcf" (and `ENDPOINT_KINDS`/`HTTP_KINDS` in council/types.py). Not changed (out of the prompt's scope); a council roster seat on an alcf endpoint will fail SeatSpec validation until a later change widens it. The fingerprint test uses SeatSpec.model_construct for this reason.
- Token-source failure message is from fixed text + source identity (basename of argv[0], or module name) + exit status/failure class; child stderr and exception text are never read into the message.
- The cache lock is held while the child runs, so concurrent refreshes invoke the source once, at the cost of other threads waiting up to token_timeout_s.
- Worktree registration was pruned mid-run; re-attached at ...kbu-alcf-alcf-backend-r with `worktree add --force`.
