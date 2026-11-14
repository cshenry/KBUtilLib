# Work Record: alcf-packaged-config-and-docs

## task_id
alcf-packaged-config-and-docs (PRD kbu-alcf-api-type-v1, phase 7)

## branch
maestro/developer/kbu-alcf-alcf-packaged-config-and-docs (base 64f997f7bc50538cb2d2bec59ff4003153a55c50)

## commit_shas
- 6ffe7d539118181634811ba8350aba6b45580217
- a2248e096e57ff2c0a5294a4942d6650e728c818
- 26b43463330400d863efff575708a47c7b3ea516
(plus the trailing work-record commit)

## summary
Added one fully commented ALCF block, delimited by BEGIN/END marker comments, to default_config.yaml (inserted between the anthropic comments and `models:`). It carries three alcf endpoints, five model aliases, two metis failover aliases, one profile (alcf-open), a rates example, and all the requested notes. The file is a pure insertion: outside the markers it is byte-identical to base. New tests pin that by sha256, check the block is all comments, and uncomment the block's YAML in-test to prove it loads and resolves.

## files_touched
- src/kbutillib/domains/ai/council/default_config.yaml
- tests/domains/ai/council/test_default_config_alcf_block.py
- agent-io/work-records/alcf-packaged-config-and-docs.md

## success_criteria_check
- Marker-delimited commented block, 3 alcf endpoints, no Sophia endpoint/model: pass (Sophia appears only in explanatory comments, test asserts no uncommented mention)
- Shared account + reason + one-worker caveat; rpm 1200 and timeout 300 on all: pass
- Five aliases @tara with max_output_tokens 32000 under params: pass (plus 2 metis failovers, also 32000)
- One profile, 3 proposers + critic + meta, no placeholder: pass
- Rationale, failover, latency, concurrency, full-corpus, Mistral do-not-seat, deployment notes, rates 0 with provenance: pass
- Packaged config loads test + byte-identical test: pass
- No base-passing test fails: pass; pinned fingerprints test (test_alcf_config.py) passes

## tests_run
- tests/domains/ai/council: 1418 passed, 7 skipped (before final test fix; new file 5 passed after)
- Full suite baseline command: 4 failed, 7993 passed, 576 skipped; the 4 failures are exactly the known base failures.

## caveats
- framework is `api` (live roster key), not vllm.
- No explicit canonical: all spellings are already in the built-in alcf table.
- Block is placed before `models:` yet documents models/profiles/rates parts; each says which top-level key it belongs under.
- Base byte-identity is pinned by sha256 of the base file, so any later edit outside the block must update BASE_SHA256 in the test.
- Token command used verbatim: alcf-tokens get-token inference.
