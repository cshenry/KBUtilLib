# Work record: lw-1-schema-and-write-path

- task_id: lw-1-schema-and-write-path
- branch: maestro/developer/clearinghouse-link-waves-lw-1-schema-and-write-path
- base: be432e78d4a3efa89d6dbcb7931d65273bb40738
- commit_shas: 1e73c36 (feat: schema, links, verbs, entity_ids), 9a8cd38 (feat: delivery manifest + golden fixture + lock + tests), plus the work-record commit (see `git log`).

## summary
Added the `method` table and seven joining tables (appended after the original 21 `table_configs()` entries, which are byte-identical to the base), a pure rule module (`clearinghouse_links.py`), the verbs `register_methods` / `register_links` (under the namespace writer lock, fill-only), `entity_ids`, the nine new alias sources and the display-prefix map, the stop-list constant, ingest key/range/resolve support for method and join tables (subject, object and method all resolved at floor 1.0), a lake-5 preflight that requires `method` and each targeted join table, `parameter_set_orphans` coverage of `method.parameter_set_hash`, and the versioned prehashed delivery manifest (`clearinghouse_delivery.py`, `delivery_schema_version` 1) with a checked-in golden fixture. Shard inventory entries gain `method_names` and load order is registry -> method -> entities -> aliases -> links.

## files_touched
- src/kbutillib/domains/kbase/berdl/clearinghouse_schema.py (METHOD_TABLE, JOIN_TABLES, columns, vocabularies, SYMMETRIC_RELATIONS, stop-list, alias sources, ALIAS_DISPLAY_PREFIXES, gene_content note)
- src/kbutillib/domains/kbase/berdl/clearinghouse_links.py (new; pure validation)
- src/kbutillib/domains/kbase/berdl/clearinghouse_ingest.py (key modes, range column, resolvable_targets, join_assertions multi-column coverage, load_rank, method_names in derived inventory, alias key mode)
- src/kbutillib/domains/kbase/berdl/clearinghouse_capability.py (register_methods, register_links, entity_ids, lake5 preflight, orphans, link resolve reads)
- src/kbutillib/domains/kbase/berdl/clearinghouse_delivery.py (new)
- tests/berdl/test_clearinghouse_links.py (new, 152 tests), tests/berdl/delivery_fixture.py (new), tests/berdl/fixtures/delivery_v1/ (new, 16 files)
- pre-existing tests edited (see below)

## success_criteria_check
- table_configs() emits original 21 unchanged: PASS (sha256 of the first 21 configs equals the base value; test_table_configs_original_21_unchanged_then_method_then_seven_joins).
- followed by `method` and seven join tables with specified columns and partitioning: PASS (column-order tests; join tables partition_by method_name, method unpartitioned).
- register_methods and register_links exist: PASS.
- each listed refusal rule has a test that submits a violating row and asserts the refusal: PASS (map below).
- fill-only, symmetric-ordering, orphan, stop-list tests: PASS.
- entity_ids reads <type>_alias without creating any view: PASS (test asserts only `genome_alias` appears in emitted SQL; no all_alias, no CREATE).
- regression-framed: PASS. Baseline selection on the branch: 2152 passed, 6 skipped, 0 failed (base: 2000 passed, 6 skipped, 0 failed; +152 new).

## tests_run
- `env PYTHONPATH=src <venv>/bin/python -m pytest tests/domains/test_identity_standardizers.py tests/domains/test_identity_parameter_sets.py tests/berdl/ tests/cli/test_clearinghouse.py tests/cli/test_clearinghouse_aliases.py tests/cli/test_clearinghouse_backfill_cli.py -q -p no:cacheprovider --no-header -m "not network"` -> 2152 passed, 6 skipped, 0 failed (19 s). Full tests/ tree not run (per instructions).
- Mutation check: disabling the symmetric byte-order rule made 6 tests fail; restored.

## refusal rule -> test (tests/berdl/test_clearinghouse_links.py)
- unknown table: test_refusal_unknown_table
- method_name with no method row: test_refusal_method_name_with_no_method_row (and the accepting twin test_a_method_in_the_same_call_or_already_committed_satisfies_the_rule)
- relation outside vocabulary: test_refusal_relation_outside_the_vocabulary
- NULL link_key / any NULL key field: test_refusal_null_in_any_key_field[5 columns]
- missing required metadata keys: test_refusal_missing_required_metadata_key[19 cases], test_refusal_inferred_function_ontology_link_needs_non_empty_evidence
- relation/basis mismatch: test_refusal_relation_basis_mismatch
- non-canonical metadata JSON / float: test_refusal_non_canonical_metadata_json, test_refusal_metadata_with_a_float
- symmetric row not in byte order: test_refusal_symmetric_row_not_in_byte_order[5 relations]; accepted in order + xref directional: test_symmetric_row_in_order_is_accepted_and_xref_is_directional; self-pair skipped and counted: test_symmetric_self_pair_is_skipped_and_counted
- gene_x_genome coordinate rules: test_refusal_gene_x_genome_coordinate_rules[12 cases], test_refusal_gene_id_not_namespaced
- same key different metadata within batch: test_refusal_same_key_different_metadata_within_a_batch; against committed: test_refusal_same_key_different_metadata_against_committed_rows; identical rows collapse: test_byte_identical_rows_collapse_within_a_batch
- conflicting method redefinition: test_register_methods_refuses_a_conflicting_redefinition_within_the_call / _against_committed[4 columns]
- parameter_set_hash not matching its JSON: test_register_methods_refuses_a_parameter_set_hash_that_does_not_match_its_json
- NULL in a method identity column: test_register_methods_refuses_a_null_in_an_identity_column[5]; bad names/over 128: test_register_methods_refuses_a_name_outside_the_pattern, ..._over_128_characters
- caller-supplied clock: test_register_methods_refuses_a_caller_supplied_clock, test_link_rows_with_a_caller_supplied_clock_are_refused
- orphan (endpoint absent): write boundary test_orphan_endpoint_is_refused_at_the_write_boundary; ingest boundary test_ingest_refuses_a_link_batch_whose_endpoint_entity_is_absent[object+subject type], test_ingest_refuses_a_link_batch_whose_method_is_absent
- lake5 preflight: test_lake5_preflight_requires_method_and_the_join_tables_a_batch_targets, test_lake5_preflight_does_not_demand_link_tables_of_a_run_without_links
- fill-only: test_fill_only_the_same_batch_twice_writes_zero_rows_the_second_time, test_one_pair_under_two_methods_writes_two_rows, test_register_methods_writes_the_registry_row_when_new_and_is_fill_only
- seven-table round trip: test_a_valid_row_in_each_of_the_seven_tables_round_trips[7]
- stop-list: test_every_stoplist_string_is_stoplisted[13], test_family_names_are_loaded_not_stoplisted (DUF1234 kept)
- parameter_set_orphans covers method: test_parameter_set_orphans_covers_method_parameter_set_hash

## pre-existing tests changed (all are count/shape assertions made stale by appending 8 tables or 9 alias sources; none weakened)
- tests/berdl/test_clearinghouse_alias_health.py::test_lake5_preflight_passes_without_any_alias_table: fake namespace has 24 tables (was 16).
- tests/berdl/test_clearinghouse_backfill.py::test_status_merges_the_state_file_run_json_and_the_ledger and tests/cli/test_clearinghouse_backfill_cli.py::test_status_emits_one_envelope_merging_all_three_records: status reports 29 tables (was 21).
- tests/berdl/test_clearinghouse_bootstrap_adapter.py::TestSchemaOnlyCreatePath::test_entity_hash_is_declared_string_on_every_generated_statement: the 8 new tables carry no entity_hash, so expected count is len-1-8.
- tests/berdl/test_clearinghouse_schema.py: test_returns_exactly_twenty_one_configs... (first 21 asserted unchanged, then method + joins, total 29), test_every_per_type_name_equals_table_name_resolver, test_alias_tables_are_last... (alias tables are configs[16:21]; no longer last), test_the_unpartitioned_tables_omit_the_key_entirely (+method), test_registry_is_the_only_non_entity_typed_table (+method/joins), test_alias_sources_is_the_ten_in_order (first 10 unchanged, 9 appended), test_sources_by_entity_type (genome 14, protein gets 5 protein-only sources).

## delivery manifest fixture
- path: tests/berdl/fixtures/delivery_v1/ (delivery.json + 15 parquet batches); regenerate with `PYTHONPATH=src:. python -m tests.berdl.delivery_fixture <dir>`.
- delivery_schema_version: 1. Module: kbutillib.domains.kbase.berdl.clearinghouse_delivery (build_delivery, validate_delivery, write_run_inventory, batch_id_for, content_sha256).
- Order: parameter_set + method -> entities -> aliases -> links. Batch id = Crockford ULID shape of sha256(table NUL content_sha256); content_sha256 excludes ingest_batch_id. Rebuild is compared on content_sha256/batch ids, not parquet file bytes (pyarrow-version dependent).

## caveats
- The delivery manifest layout/field names are my design from the one-paragraph contract; KBDLJobRunningPrototype's producer should be pointed at `build_delivery` and the fixture rather than re-deriving them.
- Scope extension: `key_columns_for_table` previously raised for `<type>_alias`, so an alias batch in a run could not be ingested at all. I added the alias key mode (entity_type, entity_hash, alias_source, alias_id, observation), identical to what register_aliases is fill-only on, because the delivery's alias stage needs it. Load order now ranks alias after entity/content/result and link batches last (stable otherwise).
- register_links additionally refuses orphans (endpoint entity absent) at the write boundary, beyond the listed rules; it takes an optional `methods=` argument so a method and its links can land in one call ("in the batch").
- Namespace writer lock: register_* take ci.NamespaceLock; on a pod without the lock dir configured they refuse like ingest does (tests set KBU_CLEARINGHOUSE_LOCK_DIR).
- Join-table link_key checks are shape checks (64-hex for the sha256 keys, link_key == metadata key where the contract says so); the sha256 itself is not recomputed because the source payload is not in the row. genome_x_genome link_key and original_orientation values are not constrained beyond presence/strings (contract does not enumerate them); translation_table accepts a positive int or digit string.
- `method.description` identity: not part of method identity, so a differing description on an existing name is fill-only-ignored, not refused.
- parameter_set_orphans tolerates an absent `method` table (reported as `method_table_readable: false`) so old namespaces still audit.
- gene_content.protein_entity_hash stays; comment in the schema marks it deprecated for new writers (leave NULL; gene_x_protein authoritative). No writer was changed to stop writing it because none writes it in this repo's paths I touched.
- Dropbox wip working tree untouched; nothing pushed or merged.
