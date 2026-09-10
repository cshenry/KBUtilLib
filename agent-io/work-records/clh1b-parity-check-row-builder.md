# clh1b-parity-check-row-builder

## task_id
clh1b-parity-check-row-builder (jane.db task 914)

## branch
wip (direct, no Maestro lane -- follow-on fix to the merged -1b build)

## commit_shas
- a4e332d (fix + regression test)

## summary
Follow-on to the `-1b` clearinghouse build, closing the three items filed as
task 914 by the phase-2 developer and its reviewer. All three sit in
`scripts/clearinghouse_parity_check.py`, which the `-1b` taskplan did not
cover: its success criteria named the fixture rows, the module docstring of
`clearinghouse_parity_fixture.py`, and the script's module-level
`_RESULT_COLUMNS` list -- but not the `Row(field=...)` builder inside
`_build_fixture_dataframe`.

**Item 1 (the real defect).** `_build_fixture_dataframe` constructed its
Spark `Row` objects from a hand-maintained keyword list of seven fields --
`entity_hash`, `result_type`, `source`, `result_type_version`, `payload`,
`observed_at`, `ingest_batch_id` -- omitting `entity_type`. The `StructType`
built immediately above it is derived from `clearinghouse_schema`, which now
declares eight columns with `nullable=False`. An operator running OP3 would
have met that mismatch for the first time against the live `result` table.

**Item 3 (reported unverified, now moot).** The phase-2 reviewer reported
that `Row(**kwargs)` alphabetises field order, implying the seven supplied
fields might not line up positionally with the declared schema either. That
claim is version-dependent -- Spark sorted keyword fields alphabetically
before 3.0 and preserves entry order from 3.0 on -- and `pyspark` is not
installed on primary-laptop, so it could not be settled here.

Rather than patch item 1 and leave item 3 pending a pod check, the row build
was changed to source both the schema and the rows from the SAME parsed
`columns` list and to construct rows POSITIONALLY:

```python
rows = [
    Row(*(cell(name, row[name]) for name, _sql_type in columns))
    for row in ALL_PARITY_ROWS
]
```

This makes the keyword-ordering question irrelevant to this script, picks up
any future schema column automatically, and turns a missing column into a
`KeyError` raised off-pod naming that column rather than a silent
short-supply at the write step. `grep` confirms this was the ONLY `Row(`
construction in `src/` and `scripts/` combined, so item 3 is closed for the
whole repo, not merely deferred.

**Item 2.** The script's module docstring still described the slot key in its
old three-part form (`entity_hash`/`result_type`/`source`) when arguing that
re-running the script is harmless. Updated to the four-part key. This is the
same staleness class the `-1b` build fixed in `clearinghouse_parity_fixture.py`'s
docstring; it was outside that task's stated criteria.

**Regression guard.** The script itself has no test coverage and cannot get
any off-pod (it needs `pyspark`). The invariant that now keeps the positional
build correct -- every fixture row supplies exactly the declared `result`
columns -- IS testable without `pyspark`, and is asserted in
`tests/berdl/test_clearinghouse_schema.py::TestParityFixtureMatchesResultSchema`.

## files_touched
- `scripts/clearinghouse_parity_check.py`
- `tests/berdl/test_clearinghouse_schema.py`

## success_criteria_check

- **`entity_type` reaches the Spark rows** -- PASS. Verified off-pod by
  replaying the exact derivation (`columns` parsed from
  `table_configs()['result']['schema_sql']`, values coerced by the same
  `cell` logic) against `ALL_PARITY_ROWS`: 13 rows built, every one 8 fields
  wide, schema width 8, column order
  `['entity_hash', 'entity_type', 'result_type', 'source',
  'result_type_version', 'payload', 'observed_at', 'ingest_batch_id']`.
- **No fixture key is silently dropped** -- PASS. Same replay confirmed the
  set difference `{fixture keys} - {schema columns}` is empty.
- **Module docstring states the four-part slot key** -- PASS. Direct read;
  no remaining three-part spelling in the file (`grep`).
- **Item 3 closed rather than deferred** -- PASS. Rows are positional, so
  `Row` keyword ordering cannot affect this script; and `grep -rn "Row(" src
  scripts` returns this one call site only.
- **Script still imports and runs off-pod safely** -- PASS. `py_compile` OK;
  loading the module by path and reading `RESULT_TABLE_FQN` /
  `_RESULT_COLUMNS` succeeds with no `pyspark` present, preserving the
  module docstring's off-pod-import guarantee.
- **`ruff check`** -- PASS, "All checks passed!".
- **New tests fail on the defect they guard** -- PASS. Captured below.
- **No test that passed before fails now** -- see `tests_run`.

## tests_run

1. **Must-fail-first capture for the new guard.** With `entity_type`
   temporarily deleted from the `parity-dup` fixture rows:

   ```
   FAILED tests/berdl/test_clearinghouse_schema.py::TestParityFixtureMatchesResultSchema::test_every_fixture_row_supplies_exactly_the_declared_result_columns
   FAILED tests/berdl/test_clearinghouse_schema.py::TestParityFixtureMatchesResultSchema::test_entity_type_is_declared_and_supplied_by_every_fixture_row
   2 failed in 0.65s
   ```

   The fixture was then restored and `git diff --stat` confirmed a clean
   restore (no residue).

2. **berdl module, post-change**: `139 passed in 0.84s` (before the two new
   tests were added), then `23 passed` for
   `tests/berdl/test_clearinghouse_schema.py` with them.

3. **Full suite, post-change**, in the baseline interpreter (see the venv
   caveat below):

   ```
   /Users/chenry/VirtualEnvironments/KBUtilLib-py3.13/bin/python -m pytest \
     tests/ -q --continue-on-collection-errors
   ```

   Result: `1 failed, 2869 passed, 401 skipped, 266 warnings, 6 errors in
   199.90s`. That is the `-1b` baseline of `2867 passed / 401 skipped /
   1 failed / 6 errors` plus exactly the two tests added here, with the
   skipped count unchanged and the failed/errored set byte-identical:

   ```
   FAILED tests/modeling/test_ms_reconstruction_utils.py::test_default_mode_uses_db_fallback
   ERROR tests/biochem/test_escher_utils.py
   ERROR tests/notebook/helpers - ModuleNotFoundError: No module named 'cobra'
   ERROR tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_returns_correct_shape
   ERROR tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_solutions_nonempty
   ERROR tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_model_grows
   ERROR tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_reaction_count_increases
   ```

   All 7 are the pre-existing undeclared-`cobra` casualties named in the
   `-1b` envelope. No test that passed before this change fails after it.

## caveats
- **Venv -- this cost a wrong run, so it is recorded in full.** The `-1b`
  baseline was taken in `/Users/chenry/VirtualEnvironments/KBUtilLib-py3.13`
  (capital `K`), per the `clh1b-derivation-slot-key` and
  `clh1b-parity-fixture-entity-type` records. Two other interpreters are
  easy to reach for and both give non-comparable numbers:
  - the shell default (`kbu.nb-adp1notebooks-py3.11`) lacks `duckdb` and
    cannot even collect `tests/berdl/test_clearinghouse_derivation.py`;
  - `kbutillib-conductor-baseline` HAS `duckdb` and runs the whole suite
    cleanly, but carries a different set of optional deps -- it reports
    `2891 passed / 380 skipped` where the real baseline reports
    `2867 passed / 401 skipped`. The failed/errored set is identical in
    both, so the difference is entirely skip-vs-pass on optional
    dependencies, not a regression. A `*kbu*` glob is case-sensitive and
    silently misses `KBUtilLib-py3.13`, which is how the wrong one got
    picked here.

  Any comparison against the 2867/1/6 baseline must use `KBUtilLib-py3.13`.
- **`ruff format` drift is pre-existing and was deliberately NOT swept.**
  `scripts/clearinghouse_parity_check.py` fails `ruff format --check` at
  `e7acc8f`, before this change, on lines this change does not touch.
  `ruff format` is declared in `.pre-commit-config.yaml`, but the hook is
  not installed in this clone and `.github/workflows/` does not run `ruff`
  at all -- so nothing enforces it, and reformatting would have buried a
  small task-914 diff under unrelated churn. Flagged rather than fixed.
- **OP3 has still never been run.** This change makes the write step
  survivable; it does not prove the parity check passes. The operator gate
  in `agent-io/docs/clearinghouse-schema-operator-runbook.md` is unchanged
  and still owed a real pod run.
