# Work record: clh1-table-configs-and-hash

## task_id
clh1-table-configs-and-hash

## branch
maestro/developer/clh1-table-configs-and-hash

## commit_shas
- 3550498e135e00bb7b602c85aab8a4ff00dcffc3 -- feat(berdl): add clearinghouse schema table configs and hash encoding

## summary

Added `src/kbutillib/domains/kbase/berdl/clearinghouse_schema.py`, defining the
three Apache Iceberg tables of the `kbaseincubator.clearinghouse` namespace
(`entity`, `canonical_content`, `result`) as pure, pod-free config dicts shaped
for `BerdlCapability.load(tables=...)`, plus `encode_entity_hash()` /
`decode_entity_hash()`, which bridge the 64-char hex digests produced by
`kbutillib.domains.identity.standardizers` to this schema's raw 32-byte binary
`entity_hash` column. The module docstring documents that bridge explicitly
(the standardizers as the hex source, and the requirement that probe hashes be
bound as query parameters carrying bytes, never interpolated into SQL as a hex
literal) because a caller who skips the encode step gets a silent, total
mismatch: every dedup probe returns "unknown" and the corpus is recomputed
from scratch. `result` declares `partition_by: "source"` (a bare, stored
column); `entity` and `canonical_content` carry no `partition_by` at all,
since bucketing a hash column gives zero read pruning by design. Added
`tests/berdl/test_clearinghouse_schema.py` (20 tests) and added `duckdb` to
both dev dependency lists in `pyproject.toml` per its "edit one list, edit the
other" convention comment.

## files_touched

- `src/kbutillib/domains/kbase/berdl/clearinghouse_schema.py` (new)
- `tests/berdl/test_clearinghouse_schema.py` (new)
- `src/kbutillib/domains/kbase/berdl/__init__.py` (modified -- exports the new
  module's public symbols, matching the package's existing re-export
  convention, and adds a bullet to the package docstring)
- `pyproject.toml` (modified -- `duckdb >=0.10` added to both
  `[project.optional-dependencies].dev` and `[dependency-groups].dev`)

## success_criteria_check

- **Module exports `table_configs()`, `encode_entity_hash()`,
  `decode_entity_hash()`** -- pass. All three are top-level functions in
  `clearinghouse_schema.py` and are also re-exported from the package
  `__init__.py`.
- **`table_configs()` returns exactly three tables named `entity`,
  `canonical_content`, `result`** -- pass. Asserted directly in
  `TestTableConfigs::test_returns_exactly_three_tables_with_expected_names`.
- **`result` declares `partition_by` of exactly the bare column `'source'`;
  the other two declare none** -- pass. `result`'s config has
  `"partition_by": "source"` (bare string, per the task prompt's literal
  spec); `entity`/`canonical_content` omit the key entirely. Covered by
  `test_result_declares_partition_by_source_and_only_source` and
  `test_entity_and_canonical_content_declare_no_partition_by`.
- **No config value anywhere contains a partition-transform expression (no
  parentheses)** -- pass, with one resolved ambiguity: the task prompt's
  column spec literally read `entity_hash BINARY(32)`. Taken as a literal SQL
  type spelling, that string itself contains a parenthesis and would fail
  this exact criterion. I resolved this by declaring the column as bare
  `BINARY` (Spark SQL's `BinaryType` has no length parameter in the first
  place, so `BINARY(32)` was never valid DDL for this write path regardless)
  and enforcing the 32-byte width in Python, at the `encode_entity_hash`/
  `decode_entity_hash` boundary, instead of in the DDL. Documented in the
  module docstring. Covered by
  `test_no_config_value_anywhere_contains_a_parenthesis`, which walks every
  string value in every table config recursively.
- **`duckdb` appears in both dev dependency lists in `pyproject.toml` and in
  neither the runtime `dependencies` list nor as `pyspark` anywhere** --
  pass. Added to `[project.optional-dependencies].dev` and
  `[dependency-groups].dev`; grepped the diff to confirm no `pyspark` or
  runtime-list addition.
- **New tests pass** -- pass. 20/20 new tests green (see Tests run).
- **No test that passed on the base commit fails on the branch** -- pass.
  Full baseline command run on the branch: 2834 passed / 1 failed / 4 errors
  / 380 skipped, vs. base 2814 passed / 1 failed / 4 errors / 380 skipped.
  The delta is exactly the 20 new tests added; the 1 failure and 4 errors are
  byte-for-byte the same pre-existing node IDs listed in the task envelope
  (none in `tests/berdl/` or `tests/domains/test_identity_standardizers.py`).
- **Module docstring names the standardizers as the hex source and states
  probes bind bytes as parameters, not hex literals** -- pass. See the
  module's second and third paragraphs.
- **Hex-vs-binary non-match test is present** -- pass.
  `TestHexBridgeToStandardizers::test_hex_digest_is_not_equal_to_its_binary_encoding`,
  explicitly marked in its docstring as a regression test that must not be
  removed as redundant.
- **Column-type declaration defined in one place, with a comment recording
  that OP2 confirms it off-pod-unverifiable** -- pass. Column types live only
  in `_ENTITY_COLUMNS` / `_CANONICAL_CONTENT_COLUMNS` / `_RESULT_COLUMNS`;
  `table_configs()` derives its `schema_sql` strings from those via
  `_schema_sql()`, nothing duplicates the declaration. The module docstring's
  fourth paragraph states plainly that whether Spark/Iceberg accepts bare
  `BINARY` through this write path, and whether the resulting column is
  genuinely binary rather than STRING, is not verifiable off-pod, and names
  OP2 (create the tables in a `kbhub` notebook, then inspect the created
  columns' physical types) as the confirming step.

## tests_run

- `.../kbutillib-conductor-baseline/bin/python -m pytest tests/berdl/ -q`
  -- 84 passed (64 pre-existing `test_capability.py` + 20 new).
- `.../kbutillib-conductor-baseline/bin/python -m pytest
  tests/domains/test_identity_standardizers.py -q` -- 27 passed (sanity check;
  unmodified by this change).
- Full baseline command, exactly as specified:
  `.../kbutillib-conductor-baseline/bin/python -m pytest tests/ -q
  --ignore=tests/biochem/test_escher_utils.py --ignore=tests/notebook/helpers`
  -- **2834 passed, 1 failed, 4 errors, 380 skipped** (260.81s), vs. measured
  base of **2814 passed, 1 failed, 4 errors, 380 skipped**. The 1 failure and
  4 errors are exactly the five pre-existing node IDs from the task envelope
  (`test_ms_reconstruction_utils.py::test_default_mode_uses_db_fallback` and
  the four `test_comprehensive_gapfill_wrapper.py` tests) -- confirmed by
  reading the failure/error summary lines, not just the counts. Not caused by
  this change, not modified by this change.
- Before trusting any run, confirmed the venv imports this worktree's
  `kbutillib` (`python -c "import kbutillib; print(kbutillib.__file__)"`
  pointed at a *different* worktree initially -- `clh1-baseline-KBUtilLib` --
  so I ran `pip install -e . --no-deps` from this worktree first, then
  re-verified the import path pointed here before running any test.

## caveats

- **Resolved ambiguity: `BINARY(32)` vs bare `BINARY`.** The task prompt's
  column spec literally reads `entity_hash BINARY(32)` for all three tables,
  but the success criteria also requires that no config value contain a
  parenthesis (explicitly framed as the guard against a leaked partition
  transform). Those two requirements are in direct tension if `BINARY(32)` is
  taken as literal DDL. I resolved it in favor of the explicit, testable
  no-parenthesis criterion: the DDL declares bare `BINARY`, and the 32-byte
  length constraint is enforced entirely in Python by
  `encode_entity_hash`/`decode_entity_hash` (which reject anything not
  exactly 32 raw bytes or 64 hex characters). This is consistent with the
  underlying platform fact that Spark SQL's `BinaryType` has no length
  parameter in its DDL syntax in the first place, so `BINARY(32)` was never
  going to be valid regardless of which way this ambiguity was resolved. This
  is exactly the class of off-pod-unverifiable spelling question the task
  prompt calls out for OP2 to confirm in-pod; the module docstring says so.
- **`duckdb` is added to the dev dependency lists but not imported anywhere**
  in the new code or tests. The task prompt says only to add it as a
  dev-only dependency; it does not ask for it to be exercised by these
  tests, and the shared baseline venv does not currently have it installed
  (confirmed: `ModuleNotFoundError` before this change), so writing a test
  that imports it would have broken the baseline-venv test run. If a later
  task wants duckdb-backed schema validation (e.g. running the `schema_sql`
  DDL fragments through an actual `CREATE TABLE` to catch syntax errors
  off-pod), that is a natural next step but is out of scope here.
- Updated `src/kbutillib/domains/kbase/berdl/__init__.py` to re-export the
  new module's public symbols (aliased as `CLEARINGHOUSE_TENANT`,
  `CLEARINGHOUSE_NAMESPACE`, `clearinghouse_table_configs`,
  `encode_entity_hash`, `decode_entity_hash`) and added one docstring bullet.
  This was not explicitly requested by the task prompt, but it matches the
  package's existing pattern of re-exporting every submodule's public surface
  at the package level (see `capability`, `membership`, `naming`, `tokens`,
  `transports`, all re-exported the same way); flagging it here in case the
  reviewer considers it out of the task's stated scope.
- Did not touch `src/kbutillib/domains/kbase/berdl/capability.py` or any
  other existing file beyond the `__init__.py` re-export addition.
