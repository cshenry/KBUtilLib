# Work record: manifest-alias-block

- task_id: manifest-alias-block
- branch: maestro/developer/manifest-alias-block
- commit_shas: d8c8eee14c6a24e92a07d15ad224d3ff4eb1f872 (feature + tests); work-record commit follows
- base: 4435c0aef083d240ffb36d4e476baef47e262f73 (main did not move)

## Summary
Added the fourth manifest kind `alias`. `KINDS` now MIRRORS the schema module
(`tuple(schema._KINDS)`) instead of re-typing the tuple, so the two cannot drift.
`Source` gains `alias` (list of tables; `[[source.alias]]`), `TablePlan` gains
`aliases` ((alias_source, alias_id Derivation) pairs). `_plan_alias` validates at plan
time against the standard `schema.column_names(entity_type, "alias")`, no bespoke
accessor, and yields a `<entity_type>_alias` TablePlan (the entity_alias wording in the
task was resolved per the dispatch note). Refusals: unknown alias_source (message lists
ALIAS_SOURCES), alias_source not legal for the entity_type (extra, from
ALIAS_SOURCES_BY_ENTITY_TYPE), unparseable alias_id (parse_derivation), reserved
columns entity_type/entity_hash/observation/observed_at/ingest_batch_id (observation's
message names observe_alias as the withdrawal path), unknown keys, 'alias' in kinds with
no block, a block without 'alias' in kinds, and a single-table (non-array) block.
Sharder: `_build_alias_rows` emits one row per entry with observation="present"
unconditionally (constant, never read from the preset); a null alias_id drops the ROW
(counted as skip reason alias-id-null); rows validate_alias refuses are dropped
(alias-invalid). `_key_spec` gained an alias spec (dedup on
entity_hash, alias_source, alias_id, observation); shard_table's row loop now handles a
list of rows per record.

## files_touched
- src/kbutillib/domains/kbase/berdl/clearinghouse_manifest.py
- src/kbutillib/domains/kbase/berdl/clearinghouse_shard.py
- tests/berdl/test_clearinghouse_manifest_alias.py (new)

## success_criteria_check
- KINDS includes alias: pass (mirrors schema._KINDS; tested)
- list-shaped block plans, yields <type>_alias TablePlan: pass (genome_alias)
- refusals (unknown source naming legal values, unparseable derivation, sharder-filled
  columns incl. observation specifically, alias kind with no block): pass
- null @concat drops the row, no "None" in any alias_id: pass (sharder-level test)
- observation="present" on every row; preset cannot declare it: pass
- no previously-passing test fails: pass for tests/berdl (full suite left to coordinator)

## tests_run
- Interpreter: /Users/chenry/VirtualEnvironments/KBUtilLib-py3.13/bin/python3 -m pytest -o pythonpath=src tests/berdl -q
- Result: 1704 passed, 6 skipped, 0 failed (new file: 15 tests, all pass)
- kbutillib.__file__ verified: /Users/chenry/.maestro/worktrees/manifest-alias-block/src/kbutillib/__init__.py
- Full suite not run (coordinator runs it).

## caveats
- Scope additions beyond the prompt: per-entity-type alias_source legality check, and refusing a [[source.alias]] block when 'alias' is not in kinds (it would otherwise be silently ignored).
- No live table is required; the five <type>_alias tables are OP-C5 (P7).
- Module docstring of clearinghouse_manifest.py was not extended with an alias example.
- A concat test needs 4 args (a 2-arg @concat is rejected as @prefix); the test uses a trailing empty-valued column.
