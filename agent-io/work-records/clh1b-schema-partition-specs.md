# clh1b-schema-partition-specs

- **task_id**: clh1b-schema-partition-specs
- **branch**: maestro/developer/clh1b-schema-partition-specs
- **commit_shas**:
  - 9af9f5228abaef5955296222666ba467a47395ba

## summary

The clearinghouse `entity` and `result` Iceberg tables were built without a
partition key before the operator step that creates them ever ran. Two
findings (the dedup probe is typed by construction on `entity_type`, and
`protein`/`gene_dna` share a standardizer and can collide on hash) mean
identity in this schema is the pair `(entity_hash, entity_type)`, and
`entity_type` is the right partition key. This change adds `entity_type` to
`_RESULT_COLUMNS` (immediately after `entity_hash`), gives `entity` a
`partition_by` of `"entity_type"`, changes `result`'s `partition_by` to the
order-significant two-element list `["source", "entity_type"]`, leaves
`canonical_content` untouched and unpartitioned (content-addressed, deferred
pending downstream query shapes rather than settled), and rewrites the
module's "Partitioning" docstring paragraph accordingly. `test_clearinghouse_schema.py`'s
`test_entity_and_canonical_content_declare_no_partition_by` is split into
per-table assertions, with `result`'s spec asserted as an ordered list.

Widening `_RESULT_COLUMNS` has a blast radius beyond the two files this
task's prompt named: `clearinghouse_derivation._result_columns()` derives
its SELECT list from `table_configs()`, so two other test files that
hardcode the old 7-column `result` shape or the old single-column
`partition_by` broke. `tests/berdl/test_clearinghouse_bootstrap.py` isn't
owned by any later task in this PRD's taskplan, so I updated its hardcoded
partition-spec literals (`result`'s live/expected spec, `entity`'s
"unpartitioned" example table swapped to `canonical_content` since `entity`
no longer is unpartitioned) to match the new config. `tests/berdl/test_clearinghouse_derivation.py`
*is* owned by a later, dependent task (`clh1b-derivation-slot-key`, which
widens `_SLOT_KEY_COLUMNS` and this file's DDL/fixtures for real); rather
than pre-empt that task's design, I only widened this file's own local
DuckDB harness (`_CREATE_RESULT_TABLE`, `_INSERT_ROW`, `ResultFixture.insert`,
`ResultFixture.current_state`'s `columns` list) to add an `entity_type`
column defaulting to `"protein"`, so existing rows (which don't yet carry
`entity_type` -- that's `clh1b-parity-fixture-entity-type`'s job) keep
inserting and the SELECT's wider column list keeps zipping correctly.
Neither `clearinghouse_derivation.py`'s `_SLOT_KEY_COLUMNS` nor
`clearinghouse_parity_fixture.py` was touched.

## files_touched

- `src/kbutillib/domains/kbase/berdl/clearinghouse_schema.py` (in scope)
- `tests/berdl/test_clearinghouse_schema.py` (in scope)
- `tests/berdl/test_clearinghouse_bootstrap.py` (out of the prompt's explicit
  file list, but not owned by any later task in the taskplan; fixed because
  it hardcoded the pre-change partition specs this task changed)
- `tests/berdl/test_clearinghouse_derivation.py` (out of the prompt's
  explicit file list; only its local DuckDB test-harness plumbing was
  widened for column-count parity -- the derivation source, its slot key,
  and the shared parity fixture module were left alone for the dependent
  `clh1b-derivation-slot-key` / `clh1b-parity-fixture-entity-type` tasks)

## success_criteria_check

- entity declares `partition_by` equal to `["entity_type"]` (bare string
  form): **pass** -- `table_configs()["entity"]["partition_by"] == "entity_type"`.
- result declares exactly `["source", "entity_type"]` in that order:
  **pass** -- verified by `test_result_declares_partition_by_source_then_entity_type_in_order`.
- canonical_content declares no `partition_by` key at all: **pass** --
  verified by `test_canonical_content_declares_no_partition_by`.
- `_RESULT_COLUMNS` lists `entity_type` immediately after `entity_hash`:
  **pass**.
- `_CANONICAL_CONTENT_COLUMNS` unchanged from base commit: **pass** --
  `git diff ccb248b -- src/.../clearinghouse_schema.py` touches nothing in
  that tuple.
- No config value contains a parenthesis: **pass** -- `test_no_config_value_anywhere_contains_a_parenthesis`
  passes unmodified, and the new `partition_by` values (`"entity_type"`,
  `["source", "entity_type"]`) contain none.
- The module docstring no longer asserts entity is unpartitioned: **pass**
  -- both the top-level "Partitioning" paragraph and `table_configs()`'s own
  docstring were rewritten.
- The no-partition-by-for-entity test is replaced by per-table assertions,
  with result's spec asserted in order: **pass**.
- No test that passed on the base commit fails on the branch: **pass**,
  with the caveat below about touching two files outside the prompt's
  explicit list to make this true (see `caveats`).

## tests_run

- `/Users/chenry/VirtualEnvironments/KBUtilLib-py3.13/bin/python -m pytest tests/berdl/ -q`
  -- 129 passed (baseline was 128 passed on this subset; net +1 from
  replacing 2 partition-by tests with 3).
- `/Users/chenry/VirtualEnvironments/KBUtilLib-py3.13/bin/python -m pytest tests/ -q --continue-on-collection-errors`
  (the exact baseline command) -- 2857 passed, 1 failed, 6 errors, 401
  skipped, 196.96s. The 1 failure and 6 errors are exactly the 7
  pre-existing cobra-related `failed_node_ids` from the conductor's
  baseline (unchanged names); 2857 = baseline's 2856 + the same net +1 new
  test. No regression.

## caveats

- This task's prompt named exactly two files
  (`clearinghouse_schema.py` and its own test file). Widening
  `_RESULT_COLUMNS` per the prompt's explicit instruction breaks two other
  test files that structurally depend on the `result` table's column count
  and the old partition specs (`test_clearinghouse_bootstrap.py`,
  `test_clearinghouse_derivation.py`) because
  `clearinghouse_derivation._result_columns()` derives its SELECT list from
  `table_configs()`. I read the PRD's taskplan (reachable at
  `~/Dropbox/Projects/KBDLJobRunningPrototype/agent-io/prds/clearinghouse-lake-1b-partitioned-scheme/taskplan.json`,
  despite the prompt's "may be UNREACHABLE" caveat) to confirm which of the
  two broken files is owned by a later, dependent task
  (`test_clearinghouse_derivation.py`, owned by `clh1b-derivation-slot-key`)
  versus unowned by anything downstream (`test_clearinghouse_bootstrap.py`).
  I fixed the unowned one fully and made only the minimal, source-code-free
  fix to the owned one (default `entity_type="protein"` in its local DuckDB
  harness) so this task's explicit "no previously-passing test fails"
  success criterion holds without duplicating or conflicting with the
  later task's planned edits to `clearinghouse_derivation.py`,
  `clearinghouse_parity_fixture.py`, or that test file's slot-key/DDL
  design.
- `clearinghouse_derivation.py`'s `_SLOT_KEY_COLUMNS` still reads
  `("entity_hash", "result_type", "source")` -- unchanged, and still
  vulnerable to the protein/gene_dna hash-collision defect the PRD
  describes (D1b.4). That fix is explicitly out of this task's scope and
  belongs to `clh1b-derivation-slot-key`.
- `clearinghouse_parity_fixture.py` rows still carry no `entity_type` key;
  that is `clh1b-parity-fixture-entity-type`'s scope.
- Whether Spark/Iceberg genuinely accepts these bare-string/list
  `partition_by` values through the real write path is unverified off-pod,
  per the module's own pre-existing docstring caveat (OP2, in-pod).
