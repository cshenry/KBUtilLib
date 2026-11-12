# Work record: read-aliases-and-resolve

- task_id: read-aliases-and-resolve
- branch: maestro/developer/read-aliases-and-resolve (base e5ff33d)
- commit_shas: 0171fcad52beb95dff32e69d696bbf4570d32816 (feature + tests); work-record commit follows

## Summary
Added `aliases()` and `resolve()` to ClearinghouseCapability, plus `AliasAmbiguousError` and `AliasNotInvertibleError` (both RuntimeError subclasses, unrelated to each other and not ValueError). One SQL builder (`_alias_rows`) emits `entity_hash = ?` with canonicalized params (never interpolates a hash), `_in_literal` for list filters (None = none, [] = `1 = 0`), `ROW_NUMBER` ordered by the existing `ALIAS_LATEST_ORDER_BY`. `entity_type` selects the `<t>_alias` table; None reads `all_alias`. The Python side also reduces (defensive against duplicates) via the existing `latest_alias_observations`. resolve(): ValueError for unknown source, then AliasNotInvertibleError for sources outside `INVERTIBLE_ALIAS_SOURCES` (before any query, so it fires with one row), then per-four-tuple reduce -> drop withdrawn -> count distinct (entity_type, entity_hash) pairs.

## Reuse
`ALIAS_LATEST_ORDER_BY` and `latest_alias_observations` imported/used as-is; nothing redefined or extended.

## files_touched
- src/kbutillib/domains/kbase/berdl/clearinghouse_capability.py
- tests/berdl/test_clearinghouse_alias_reads.py (new, 18 tests)

## success_criteria_check
All criteria: pass (each covered by a named test: None/[] SQL, AND composition, bound hash placeholder, dedupe, history = two rows, current_only incl. finally-withdrawn empty, hA/hB/hA-withdrawn -> hB, issubclass not ValueError, non-invertible with one row, cross-type pair collision, table vs all_alias). Uncertain only: real Trino/Spark execution of the window SQL (no live table; OP-C5 not done).

## tests_run
- kbutillib.__file__ verified (with `-o pythonpath=src`): /Users/chenry/.maestro/worktrees/read-aliases-and-resolve/src/kbutillib/__init__.py (bare import resolves to the Dropbox wip tree, as warned).
- `/Users/chenry/VirtualEnvironments/KBUtilLib-py3.13/bin/python3 -m pytest -o pythonpath=src tests/berdl/ -q`: 1682 passed, 6 skipped, 0 failed. Full suite not run (coordinator runs it).

## caveats
- Test stub evaluates filters by regexing emitted SQL; the window SQL itself is unexecuted against a real engine.
- Non-invertible error is a new type (AliasNotInvertibleError) since the prompt required a distinct one but did not name it.
- Rows are sorted deterministically in Python after reduction.
