# Work record: clh1-current-state-sql

## task_id
clh1-current-state-sql

## branch
maestro/developer/clh1-current-state-sql

## commit_shas
- 5049fa26c3c81ee5eeb31e88b32c05c14b23fda -- feat(berdl): add clearinghouse current-state SQL derivation

(Verify with `git log main..HEAD --format=%H` in the worktree if this needs
re-confirming; the SHA above is the single commit made for this task.)

## summary

Added `src/kbutillib/domains/kbase/berdl/clearinghouse_derivation.py`, a
pure module exporting `current_state_sql(result_table_fqn, *,
sources=None) -> str`. It builds Spark SQL that derives "current state"
from the append-only `result` table: a `ROW_NUMBER() OVER (PARTITION BY
entity_hash, result_type, source ORDER BY observed_at DESC,
ingest_batch_id DESC)` window, keeping rows where the number is 1.
`result_type_version` is recorded but deliberately excluded from the
partition key. Table identifiers are backtick-quoted per dot-separated
segment (matching `BerdlCapability.load()`'s existing
`` `namespace`.`table` `` convention); an optional `sources` list
pre-filters the underlying rows with `WHERE source IN (...)` before
windowing (or `WHERE 1 = 0` for an explicitly empty list), pruning on
`result`'s partition column. Column names are derived by parsing
`clearinghouse_schema.table_configs()["result"]["schema_sql"]` rather
than re-typed, so the two modules cannot drift apart. The function is
pure: no session, no I/O, no pod-only imports.

Added `tests/berdl/test_clearinghouse_derivation.py`, which builds a
small in-memory DuckDB `result` table (BLOB `entity_hash`, values bound
as query parameters, never as literals) and *executes* the SQL
`current_state_sql()` returns, asserting on the query's actual output
rather than the SQL string, for each of the six named properties.
Because DuckDB has no backtick-quoted-identifier syntax at all (it uses
ANSI double quotes; confirmed by testing directly against DuckDB 1.5.5
-- `` `x` `` raises `ParserException`), the test harness applies a single
syntax-only translation (backtick -> double-quote) before executing
against the DuckDB surrogate; the window function, WHERE clause, and
column list are otherwise untouched. This translation and its rationale
are documented in both the module docstring and the test file's module
docstring. A separate `TestDialectConformance` class asserts on the raw
SQL string for the properties DuckDB execution alone cannot prove
(backtick quoting per segment, no BLOB-literal syntax, no `NULLS
FIRST`/`NULLS LAST`, `sources` filter shape, single-quote escaping),
since a green DuckDB run does not by itself prove the SQL stays inside
the DuckDB/Spark syntax intersection (DuckDB accepts strictly more than
Spark does).

Also updated `src/kbutillib/domains/kbase/berdl/__init__.py` (explicitly
scoped to this task per the envelope) to import and re-export
`current_state_sql`, and added it to `__all__`, alongside a short
description in the package docstring's module list.

Did not touch `clearinghouse_schema.py` (a sibling task,
`clh1-idempotent-bootstrap`, is editing it in parallel) -- only imported
`table_configs` from it, and reused its `result`-table column names by
parsing `schema_sql` rather than re-declaring the column list.

## files_touched
- `src/kbutillib/domains/kbase/berdl/clearinghouse_derivation.py` (new)
- `tests/berdl/test_clearinghouse_derivation.py` (new)
- `src/kbutillib/domains/kbase/berdl/__init__.py` (re-export only)

## success_criteria_check

- **New module exports a pure `current_state_sql()` returning SQL text, no I/O** -- PASS. No session object, no network call, no pod-only import; verified by inspection and by the `test_function_returns_plain_text_with_no_i_o` determinism check.
- **Tests EXECUTE the SQL against DuckDB, not string-only assertions** -- PASS. All six property tests run `con.execute(...)` against a real DuckDB connection and assert on fetched rows; only the separate `TestDialectConformance` class asserts on the string, and it targets dialect-shape properties that execution cannot prove either way.
- **Six named properties each have a passing test** -- PASS. `TestDuplicateAppendsCollapse`, `TestNewestWins`, `TestIngestBatchIdTieBreak`, `TestRemovalProperty`, `TestSourceIsolation`, `TestResultTypeVersionOutsideSlotKey` -- one class per property, all green.
- **Tie-break test is order-independent** -- PASS. `test_greater_ingest_batch_id_wins_regardless_of_insertion_order` is parametrized over 5 random shuffle seeds of the two-row insertion order and asserts the same winner every time.
- **No test that passed on base fails on branch** -- PASS. Full suite before/after counts match exactly on the non-new tests; see Tests below.
- **Signature is exactly `current_state_sql(result_table_fqn, *, sources=None)`** -- PASS, verified by direct inspection of the `def` line and by the calls made throughout the test file.
- **`sources` pre-filters on the `source` partition column** -- PASS. `test_sources_filter_prunes_to_requested_sources_only` (execution-based) and `test_sources_filter_renders_as_where_in` (string-based) both cover it; filtering happens inside the CTE's `SELECT ... FROM ... WHERE source IN (...)`, i.e. before the window function runs, not after.
- **No DuckDB-specific BLOB literal syntax, no DuckDB-only timestamp helper, no NULLS FIRST/LAST** -- PASS, checked by `test_no_duckdb_blob_literal_syntax` and `test_no_nulls_first_or_last`; also true by construction -- the function never emits a BLOB/timestamp literal or an explicit NULLS ordering clause of any kind.
- **Binary values bound as parameters** -- PASS for the test fixture, which is the only place binary (`entity_hash`) values are ever materialized in this task's code; `ResultFixture.insert` binds `entity_hash` through `duckdb`'s `?` parameter placeholder, never interpolated into SQL text. `current_state_sql()` itself never handles raw entity_hash values at all (its only literal-embedded values are `source` strings, which are internal `<tool>/<version>` identifiers, not user input, and are still single-quote-escaped).

## tests_run

- `<venv>/bin/python -m pytest tests/berdl/test_clearinghouse_derivation.py tests/berdl/test_clearinghouse_schema.py -q` -- 40 passed (20 new + 20 pre-existing schema tests), 0 failed.
- Full baseline command, exact string from the envelope:
  `/Users/chenry/VirtualEnvironments/kbutillib-conductor-baseline/bin/python -m pytest tests/ -q --ignore=tests/biochem/test_escher_utils.py --ignore=tests/notebook/helpers`
  - Before (measured baseline, provided): 2834 passed, 1 failed, 4 errors, 380 skipped.
  - After (this branch): **2854 passed, 1 failed, 4 errors, 380 skipped** (255.91s).
  - Delta: +20 passed (exactly the new test file's count), 0 change in failed/errors/skipped.
  - The 1 failed + 4 errors are the exact five pre-existing entries listed in the envelope's `failed_node_ids` (`test_default_mode_uses_db_fallback` and the four `test_comprehensive_gapfill_wrapper` collection errors) -- confirmed by name in the run's short summary. None are new, none were touched.

Note: `duckdb` was declared as a `dev` optional-dependency in `pyproject.toml`
from phase 1 but was not actually installed in the shared baseline venv
(`kbutillib-conductor-baseline`); it was missing entirely
(`ModuleNotFoundError`). Installed it there with
`pip install "duckdb>=0.10"` (resolved to 1.5.5) so the new tests -- and
this repo's declared dev dependency set -- are satisfiable. This is an
environment fix, not a code or pyproject.toml change (the task
explicitly said not to touch pyproject.toml, and duckdb's version pin
there was already correct).

## caveats

- **DuckDB has no backtick-quoted-identifier syntax.** Confirmed directly
  (`` `x` `` raises `duckdb.ParserException`), which the earlier
  `clh1-table-configs-and-hash` phase's tests never hit because that
  module never builds a query string. Since this task's spec pins Spark
  SQL (backticks) as the dialect authority and simultaneously requires
  DuckDB execution, the test file applies a documented, syntax-only
  backtick -> double-quote translation immediately before executing
  against DuckDB (see `_for_duckdb` in the test file and the "one
  accommodation" paragraph in its module docstring). `current_state_sql()`
  itself always emits backticks; nothing in the production code path is
  affected. If a reviewer wants a stronger guarantee than "these are
  read-only syntax stand-ins for the same concept," the honest next step
  would be an in-pod Spark parity check (as the module docstring already
  calls for), which is out of scope here by design.
- **`sources` embeds string literals in SQL text, not query parameters.**
  `current_state_sql()`'s signature returns SQL text only -- there is no
  side channel for a caller-facing parameter list -- so `source` values
  are single-quote-escaped and embedded directly. This is consistent with
  `clearinghouse_schema.py`'s own framing of `source` as an internal,
  operator-controlled `<tool>/<version>` string, not untrusted input.
  Flagging this as a design choice rather than an oversight, since it
  differs from how `entity_hash` (bound as a parameter) is handled
  elsewhere in the codebase.
- **`sources=[]` (explicit empty list) filters out everything**
  (`WHERE 1 = 0`), distinct from `sources=None` (no filter). This wasn't
  spelled out in the task prompt; I judged an explicit empty list to mean
  "match zero sources" rather than "no filter," since treating it as "no
  filter" would silently ignore the caller's own input. Documented in the
  function's docstring and covered by
  `test_empty_sources_list_filters_out_everything`.
- Did not modify `clearinghouse_schema.py`, `pyproject.toml`, or any file
  outside the three listed above, per the parallel-lane file discipline
  in the envelope.
