# Work record: schema-entity-alias-table

- task_id: schema-entity-alias-table (PRD clearinghouse-entity-alias-v1, P1)
- branch: maestro/developer/schema-entity-alias-table
- base: main 9eb1f609c735d612bfc31dc727367a9cde280f96 (re-read before cutting; had not moved)
- commit_shas: see `git log 9eb1f60..HEAD` (feat, registry-order test fix, fallout test fixes, this record)

## Summary
Built the ROUND-1 AMENDMENT shape (five per-type `<type>_alias` tables; `alias` is the fourth kind), not the round-0 single table. In `clearinghouse_schema.py`: `_KINDS`/`_VIEW_KINDS` extended; `_ALIAS_COLUMNS` (7 cols, entity_hash first, entity_type second); `table_columns(t,"alias")` generic branch; `_ALIAS_PARTITION_BY="alias_source"`; `table_configs()` now 21 (five alias last, after parameter_set); `union_view_sql("all_alias")` (explicit column list so entity_type is visibly selected); `ALIAS_OBSERVATIONS`, `ALIAS_SOURCES` (10, no kbdl_genome_id), `ALIAS_SOURCE_PATTERNS` (4), `INVERTIBLE_ALIAS_SOURCES` (8), `ALIAS_SOURCES_BY_ENTITY_TYPE`, and `validate_alias` (alias_id stored verbatim; None refused; per-type source legality). No ENTITY_ALIAS_TABLE / entity_alias_columns symbols. ENTITY_TYPES untouched.

## Files touched
- src/kbutillib/domains/kbase/berdl/clearinghouse_schema.py
- tests/berdl/test_clearinghouse_schema.py (new alias/validator tests; inverted/updated count and order tests)
- tests/berdl/test_clearinghouse_capability.py (stats/tables counts 15 -> 20, now include alias kind)
- tests/berdl/test_clearinghouse_md5_fence.py (see caveats)
- tests/berdl/test_clearinghouse_backfill.py, tests/cli/test_clearinghouse_backfill_cli.py (table-count 16 -> 21)
- agent-io/work-records/schema-entity-alias-table.md

## Import-path evidence
Test `TestImportPath` prints and asserts `kbutillib.__file__`:
`/Users/chenry/.maestro/worktrees/schema-entity-alias-table/src/kbutillib/__init__.py` (all runs used `-o pythonpath=src`).

## Tests run (interpreter /Users/chenry/VirtualEnvironments/KBUtilLib-py3.13/bin/python3, all with -o pythonpath=src)
- `-m pytest -o pythonpath=src tests/berdl/test_clearinghouse_schema.py -q`: 117 passed.
- FULL `-m pytest -o pythonpath=src tests/ -q -p no:cacheprovider` (405 s, before the capability/md5-fence fixes): 8 failed, 7697 passed, 575 skipped. 5 failures are exactly the stated baseline set; 3 were mine (below) and were then fixed.
- After fixes: `tests/berdl tests/cli/test_clearinghouse.py tests/cli/test_clearinghouse_backfill_cli.py`: 1751 passed, 6 skipped. The full suite was NOT re-run after those last test-only fixes.

## Success criteria
All listed criteria PASS by test (kinds/view kinds, 7 columns and order, generic table_columns/table_name, removed symbols, all_alias view with entity_type, PARAMETER_SET_TABLE still refused/unioned nowhere, 21 configs with alias last partitioned on alias_source, vocab tuples, invertible exclusion, per-type sources, ncbi_assembly refused/arc_record accepted on protein). "No previously-passing test fails": PASS after fixes, with the caveat that several existing tests had to be edited (below) because they asserted the old counts.

## Caveats
1. `test_clearinghouse_md5_fence.py::test_renamed_md5_symbols_absent_under_berdl` bans the literal strings `kbase_assembly_md5`/`bvbrc_genome_md5` anywhere under berdl/. The mandated ALIAS_SOURCES must contain them. I exempted only `clearinghouse_schema.py` and added a test that the names appear there only as alias-source vocabulary and that md5s are still refused as entity_hash. This weakens a guard; reviewer should confirm.
2. `ClearinghouseCapability.stats()/tables()` iterate `_KINDS`, so they now report 20 tables (alias included); I updated those two tests. Whether the capability should expose alias tables is a decision for a later task.
3. The registry's position moved from last to index 15 (inverted test, as the amendment dictates).
4. all_alias lists columns explicitly instead of `SELECT *`, so the entity_type discriminator is textually visible.
5. 5 baseline failures untouched. No sibling-repo FileNotFoundError seen.
