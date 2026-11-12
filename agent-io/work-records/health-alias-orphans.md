# Work record: health-alias-orphans

- task_id: health-alias-orphans
- branch: maestro/developer/health-alias-orphans
- commit_shas: e8e72f42a5aecfc416ba100cb248ebf023539e50 (feature + tests); work-record commit follows

## Summary
Added `ClearinghouseCapability.alias_orphans()`: per entity type, LEFT JOINs `<type>_alias` to its own `<type>_entity`, reduces rows with the existing `latest_alias_observations` reducer (reused unchanged), and counts only latest==present rows with no entity row, grouped by (entity_type, alias_source). Report keys: `orphans_by_source`, `orphan_total`, `reduced_rows`, `reduced_rows_by_type`; deliberately NO `failures` key (reason documented in docstring). `_build_health` carries it as `data.alias_integrity`; `_exit_on_integrity_failure` and `warnings` untouched. `_assert_lake5_schema` untouched.

## Files touched
- src/kbutillib/domains/kbase/berdl/clearinghouse_capability.py
- src/kbutillib/interfaces/cli/clearinghouse.py
- tests/cli/test_clearinghouse.py (added alias_orphans to _FakeCH so existing health tests still run)
- tests/berdl/test_clearinghouse_alias_health.py (new, 7 tests)

## success_criteria_check
- No 'failures' key, orphans_by_source/orphan_total, latest==present only, withdrawn 0 / re-present counted: pass (tests)
- data.alias_integrity, not in warnings: pass
- Alias orphans -> health exit 0; parameter-set finding still exit 1 with alias orphans present: pass
- Lake5 preflight passes on 16 old tables, no alias tables: pass (and the guard was not modified)
- Five-pair join to own entity table: pass (test asserts each SQL names only its own entity table)
- New tests pass; no base-passing test fails in selection: pass

## tests_run
- kbutillib.__file__ verified: /Users/chenry/.maestro/worktrees/health-alias-orphans/src/kbutillib/__init__.py (not committed as a test)
- `/Users/chenry/VirtualEnvironments/KBUtilLib-py3.13/bin/python3 -m pytest -o pythonpath=src tests/berdl tests/cli -q` from the worktree: 2625 passed, 17 skipped, 0 failed (101.65 s). New file alone: 7 passed. Full suite not run (lead runs it).

## caveats
- Reducer reused, not extended. It is applied in Python over all alias rows (no SQL windowing), so very large alias tables load fully into memory per type; acceptable pre-OP-C5, revisit if tables grow.
- Rows carry an `entity_missing` flag computed in SQL; tests stub the capability and do not touch a live table (none exist before OP-C5).
