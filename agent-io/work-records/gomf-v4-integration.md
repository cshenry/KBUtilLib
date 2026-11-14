# gomf-v4-integration

## Failure
On the combined tree (main ed7b839 + go-mf-v4 cherry-picks), 5 tests in
tests/domains/ai/llm/test_alcf_backend.py failed with
`UnknownRenderer: builtin query type 'go_mf' ships 3 versions (1, 3, 4); name the one you want`:
test_response_format_present_with_schema,
test_response_format_absent_when_flag_false_or_no_schema,
test_meta_v4_schema_has_no_strict_and_proposer_has,
test_strict_gate_rejects_nested_open_object_and_forbidden_constructs,
test_route_fingerprint_moves_with_flag_not_with_schema.

## Cause
go-mf-v4 made `load_builtin_spec(name, version=None)` refuse unversioned lookups
when several versions ship. The alcf tests (written before go_mf@4 was packaged)
called `load_builtin_spec("go_mf")` unversioned in `proposer_schema()` and
`meta_v4_schema()`, the latter hand-building a stand-in v4 META schema from @1.

## Change (tests/domains/ai/llm/test_alcf_backend.py only)
- `proposer_schema()` and `meta_v4_schema()` now use `load_builtin_spec("go_mf", "4")`.
- Deleted the hand-built stand-in (deepcopy + additionalProperties flip).
- Module docstring updated: go_mf@4 is packaged and is what the ALCF council runs.
- No src/ change, loader refusal intact, seat() fixture untouched.

## Verification of premises
Real go_mf@4 META schema has top-level additionalProperties == True (checked).
## (a)/(b) decision
None needed: all 29 tests in the file pass against the real @4 proposer and meta
schemas with no assertion changed (strict-gate verdicts are unchanged by @4).
