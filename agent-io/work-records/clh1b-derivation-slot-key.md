# clh1b-derivation-slot-key

## task_id
clh1b-derivation-slot-key

## branch
maestro/developer/clh1b-derivation-slot-key

## commit_shas
- b972442f4c3d99941e2f5b66bfeefb54d1794d63

## summary
`clearinghouse_derivation.py`'s slot key was the 3-tuple
`(entity_hash, result_type, source)`. Because `_standardize_protein` and
`_standardize_gene_dna` are the same standardizer, a sequence over the
alphabet {A,C,G,T,N} produces a byte-identical `entity_hash` whether
submitted as `protein` or `gene_dna`, so a protein row and a gene_dna row
with the same hash/result_type/source collapsed into a single slot and the
`ROW_NUMBER()` window function silently returned only one of them -- a wrong
answer, not a stale one. This change widens `_SLOT_KEY_COLUMNS` to
`("entity_hash", "entity_type", "result_type", "source")` (leaving
`result_type_version` out, unchanged, for the existing documented reason),
updates the module/function docstrings to state the 4-tuple and the reason,
and adds a keyword-only `entity_types: list[str] | None = None` parameter to
`current_state_sql` that mirrors `sources` exactly (`None` = no filter,
`[]` = match nothing via `WHERE 1 = 0`, and when both `sources` and
`entity_types` are given they compose with `AND`, with an empty list on
either one still producing a single `WHERE 1 = 0` for the whole query rather
than a redundant `1 = 0 AND ...`). Tests were extended with a regression
test that proves the defect, a string-shape test on the `PARTITION BY`
clause, and coverage for the new filter (alone, combined with `sources`,
and the empty-list case on each).

## files_touched
- `src/kbutillib/domains/kbase/berdl/clearinghouse_derivation.py`
- `tests/berdl/test_clearinghouse_derivation.py`

## success_criteria_check

- **`_SLOT_KEY_COLUMNS == ("entity_hash", "entity_type", "result_type", "source")`, `result_type_version` still absent** -- PASS. Verified by direct read of the source (`_SLOT_KEY_COLUMNS` literal) and by `test_partition_by_names_all_four_slot_key_columns_in_order`.
- **`current_state_sql` accepts keyword-only `entity_types` defaulting to `None`; `None` = no filter, `[]` = `WHERE 1 = 0`** -- PASS. Verified by `test_no_entity_types_filter_when_none`, `test_entity_types_filter_renders_as_where_in`, `test_empty_entity_types_list_filters_out_everything`.
- **`sources` + `entity_types` given together combine with AND, never OR** -- PASS. Verified by `test_sources_and_entity_types_combine_with_and` (string-shape) and `test_entity_types_and_sources_filters_combine_with_and` (execution-level, DuckDB).
- **Emitted SQL contains no `NULLS FIRST`/`NULLS LAST`; function performs no I/O** -- PASS. `test_no_nulls_first_or_last` and `test_function_returns_plain_text_with_no_i_o` both still pass; no I/O or session objects were introduced anywhere in the diff (grep confirms no `open(`, no network/session imports).
- **New test proves a protein row and a gene_dna row sharing entity_hash/result_type/source yield two rows at rn=1; fails on base, passes on branch** -- PASS. `TestEntityTypeInSlotKey::test_protein_and_gene_dna_sharing_hash_both_survive`. Captured pre-change failure below.
- **Separate test asserts `PARTITION BY` names all four slot-key columns in order** -- PASS. `test_partition_by_names_all_four_slot_key_columns_in_order` asserts the literal substring `"PARTITION BY entity_hash, entity_type, result_type, source"` is present in the emitted SQL.
- **No test that passed on the base commit fails on the branch** -- PASS. Full suite run on the branch produced the identical pre-existing failure/error set (1 failed + 6 errors, all missing-`cobra` casualties named in the envelope) plus 2867 passed (2857 base + 10 new tests), 401 skipped -- an exact match modulo the new tests added.

## tests_run

1. **Isolated pre-change capture** (evidence for the must-fail-first requirement): with the derivation source still unmodified and the test harness's `current_state()` helper temporarily reverted to not pass `entity_types` (so the call shape matched the unmodified `current_state_sql` signature), ran:

   ```
   /Users/chenry/VirtualEnvironments/KBUtilLib-py3.13/bin/python -m pytest \
     tests/berdl/test_clearinghouse_derivation.py::TestEntityTypeInSlotKey::test_protein_and_gene_dna_sharing_hash_both_survive -q
   ```

   Result: **1 failed**. Captured output:

   ```
   F                                                                        [100%]
   =================================== FAILURES ===================================
   _ TestEntityTypeInSlotKey.test_protein_and_gene_dna_sharing_hash_both_survive __

       fixture.insert(entity_type="protein", payload={"kind": "protein"}, **shared_kwargs)
       fixture.insert(entity_type="gene_dna", payload={"kind": "gene_dna"}, **shared_kwargs)
       rows = {
           row["entity_type"]: row
           for row in fixture.current_state()
           if row["entity_hash"] == entity_hash
       }
   >   assert set(rows) == {"protein", "gene_dna"}
   E   AssertionError: assert {'protein'} == {'gene_dna', 'protein'}
   E
   E     Extra items in the right set:
   E     'gene_dna'
   E     Use -v to get more diff

   tests/berdl/test_clearinghouse_derivation.py:291: AssertionError
   =========================== short test summary info ============================
   FAILED tests/berdl/test_clearinghouse_derivation.py::TestEntityTypeInSlotKey::test_protein_and_gene_dna_sharing_hash_both_survive
   1 failed in 0.67s
   ```

   This confirms the defect directly: the gene_dna row was silently dropped
   (shadowed by the protein row occupying the same 3-column slot), not a
   harness/signature error. The test harness was then restored to its full
   form (with `entity_types` support) before implementing the source fix.

2. **Full derivation test module, post-change**:
   ```
   /Users/chenry/VirtualEnvironments/KBUtilLib-py3.13/bin/python -m pytest \
     tests/berdl/test_clearinghouse_derivation.py -q
   ```
   Result: **30 passed** (20 pre-existing + 10 new).

3. **Full repo baseline command, post-change, exactly as specified**:
   ```
   /Users/chenry/VirtualEnvironments/KBUtilLib-py3.13/bin/python -m pytest \
     tests/ -q --continue-on-collection-errors
   ```
   (run with a 600000 ms Bash timeout; actual wall time ~199s)
   Result: **1 failed, 2867 passed, 401 skipped, 6 errors** -- the failed test
   and all 6 errors are exactly the pre-existing, missing-`cobra` casualties
   named in the envelope's `failed_node_ids`/error list (identical set,
   confirmed by name). Passed count is base (2857) + 10 new tests = 2867,
   confirming nothing regressed and nothing else newly failed.

## caveats
- `scripts/clearinghouse_parity_check.py` (the in-pod OP3 parity check) calls
  `current_state_sql(RESULT_TABLE_FQN, sources=list(sources))` and was left
  untouched -- it still works unchanged because `entity_types` defaults to
  `None`, but it does not yet exercise the new `entity_types` filter. Adding
  that is out of scope for this task (which was scoped to the derivation
  module and its own test file) and is a reasonable follow-up if the parity
  check should also assert entity_type isolation in-pod.
- No changes were made to `clearinghouse_schema.py` or
  `clearinghouse_parity_fixture.py` -- both already carried the `entity_type`
  column/values from the prior two tasks in this taskplan, confirmed by
  reading them before starting.
