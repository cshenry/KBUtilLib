# kbu-dataset-publisher-core (PRD kbdl-dataset-mirror-v1, phase 1 of 4)

- Task: `task-8e38cc52`, dev row 2071, role developer, machine h100
- Repo: KBUtilLib, base `main` `72bf81e9ed202ca4a9b3957ead7543fb40094bff`
- Branch: `maestro/developer/dispatch-task-kbu-dataset-publis-task-8e38cc52`
- Date: 2026-11-23

## What landed

| File | Lines | What it is |
|---|---|---|
| `src/kbutillib/domains/kbase/berdl/dataset_publish.py` | 2199 (new) | The pod-side publisher: three `typing.Protocol` ports, two thin in-pod adapters, the catalog loader, the schema-1 manifest self-check, `DatasetPublisher.status` and `DatasetPublisher.publish` |
| `src/kbutillib/domains/kbase/berdl/datasets.toml` | 37 (new) | The v1 catalog: both `genome_clearhouse` datasets, tenant `kbaseincubator`, BOTH README approval fields shipped EMPTY |
| `src/kbutillib/domains/kbase/berdl/genome_quality.md` | 20 (new) | Partner-facing README, byte-for-byte as specified |
| `src/kbutillib/domains/kbase/berdl/skani_distances.md` | 22 (new) | Partner-facing README, byte-for-byte as specified |
| `tests/berdl/test_dataset_publish.py` | 1614 (new) | 97 tests: fake ports, real pyarrow zstd parquet |
| `pyproject.toml` | +10 | `[tool.setuptools.package-data]`: the catalog and the two READMEs, following the existing berdl entries. Nothing else in the file changed. |

`src/kbutillib/domains/kbase/berdl/__init__.py` was deliberately NOT touched:
the sibling `council_*` and `clearinghouse_*` pure-logic modules that are
reached by path (not re-exported) set the precedent, and the success criteria
ask for the module, not a package export.

### Module structure (`dataset_publish.py`)

- **Ports** (`LakehousePort:261`, `StagingPort:290`, `KBDLPort:311`) — all
  `typing.Protocol`. `KBDLPort` declares `upload_object`,
  `poll_jobs_until_terminal`, `get_dataset_version` and
  `publish_dataset_version`; the last two do not exist on
  `KBDLServiceUtils` on this base (the next task adds them) and
  `kbdl_service_utils.py` was NOT edited, per the dispatch rules.
- **Pure helpers** — `validate_staging_root:354` (no default; refuses
  `tenant-general-warehouse/`; requires the `/_exports_staging` suffix),
  `require_prefix_under_root:410` (refuses the root itself as well as
  anything outside it), `_iso_utc`, `canonical_manifest_bytes`,
  `validate_manifest` (the full schema-1 rule set, ~180 lines).
- **Catalog** — `DatasetEntry`, `load_catalog`, `get_dataset`, `read_readme`
  (resolved relative to the catalog dir and refused if it escapes it).
- **Real adapters** — `InPodLakehouse` (Spark SQL over
  `InPodTransport.spark_session()`; `current_snapshot_id` reads
  `<fqn>.history` filtered on `is_current_ancestor`, NOT `<fqn>.snapshots`)
  and `InPodStaging` (MinIO over `InPodTransport.minio_client()`, bucket
  `cdm-lake`). Thin, not unit-tested: they need a pod. The rule that makes
  them safe is the pure prefix guard every method calls.
- **`DatasetPublisher.status`** — five outcomes over a `parent_id` walk
  (`no_versions`, `unchanged`, `compaction_only`, `changed`, `unknown`),
  plus leftover-staging warnings that LIST and never delete.
  `committed_at` is carried in the report and never used to decide anything.
- **`DatasetPublisher.publish`** — one pinned target read once; always-reporting
  metadata-only dry run; four refusals before any export; atomic `state.json`
  with exactly the eight listed keys; manifest reuse across retries including
  `--force`; private uploads; job confirmation plus object-id checks;
  registration with tombstone handling.

## Divergences reconciled between the task prompt and the tree

1. **`KBDLServiceUtils.upload_object` can answer 201, not only 202.** The
   prompt describes upload as "202 + a KBDLUploadObject job". The shipped
   method (`src/kbutillib/domains/external/kbdl_service_utils.py:1145`)
   returns `{"object_id": ...}` on a server-side dedup 201 and queues NO
   job. `_upload:1748` handles both shapes and `_confirm_uploads` polls only
   the ids that exist; `test_a_201_dedup_upload_queues_no_job_and_still_publishes`
   covers it. Implementing only the 202 path would have raised
   `KeyError`/polled `None` on the first deduplicated part file.
2. **Upload job status uses `state`, not `status`.**
   `poll_jobs_until_terminal` returns `{job_id: {"state": ...}}`
   (`_TERMINAL_STATES = {"completed", "failed"}`, `kbdl_service_utils.py:224`).
   `_confirm_uploads` reads `state`.
3. **No `logging` in the berdl pure modules.** Nothing under
   `domains/kbase/berdl/` logs; warnings are returned in the report instead
   of being logged, matching the sibling modules.
4. **Nothing in the prompt's STOP-shaped clauses fired.** Every file,
   symbol and transport method the prompt named exists as described
   (`InPodTransport.spark_session():239`, `.minio_client():464`).

## Judgement calls (inferred, not specified)

- **Free-space guard placement.** Step 5 says "then refuse if the workdir
  filesystem has less free space than 2 x total-files-size". It is applied
  on the path that is about to export/download, not on a resume at phase
  `exported`/`manifest_built` where the files are already on disk and the
  comparison would be against space the run itself consumed.
- **A `registered` `state.json`** is not in the prompt's resume table. It is
  reported and stops (exit-0 style, carrying the recorded version); with
  `force` it is discarded and the export restarts. `manifest_built` is reused
  even under force, exactly as specified.
- **Upload display names** are `"<dataset>/<target>/<filename>"` so an
  operator listing KBDL objects can see which dataset and snapshot a part
  belongs to. The prompt does not specify a name; object ids are the
  content hash either way.
- **Dry-run `staging_prefix`** is the template `"<root>/<dataset>/<target>-<uuid4>/"`
  (literal `<uuid4>`), because the real prefix's uuid is generated by the run
  that uses it. A real publish overwrites the key with the actual prefix.
- **`publish` does not list staging.** The leftover-staging warning belongs
  to `status`; `publish` calls the lineage computation directly. That is what
  lets a dry run honour "call NO export, staging, download or upload method
  of any port" while still reporting the status.

## README byte-for-byte check

The expected text is embedded in the test file as
`EXPECTED_GENOME_QUALITY_README` and `EXPECTED_SKANI_DISTANCES_README`,
retyped from the task prompt, and compared against
`path.read_bytes() == expected.encode("utf-8")` — bytes, not a normalised
string, so trailing whitespace and line endings are covered:

```
tests/berdl/test_dataset_publish.py::test_the_shipped_readmes_are_byte_for_byte_as_specified[genome_quality.md] PASSED
tests/berdl/test_dataset_publish.py::test_the_shipped_readmes_are_byte_for_byte_as_specified[skani_distances.md] PASSED
```

| File | bytes | lines | sha256 |
|---|---|---|---|
| `genome_quality.md` | 1026 | 20 | `6ac2337e6bb258d3d96d46394597a9426ba4b66788a6abed38eb6ea05cde18a3` |
| `skani_distances.md` | 967 | 22 | `ea2cf7c98af4cba22d1eb5072f079765e14da917e575f20d1b76f88e0ba0508a` |

Both begin with `DRAFT -- not approved for publication` and end with a single
trailing newline after "...checked three ways at export time."; nothing was
added. `ruff format` was run on both new `.py` files and left the `.md` files
untouched (identical sha256 before and after).

The shipped catalog is asserted separately: every entry's key set is exactly
`{tenant, namespace, table, target_file_bytes, readme_file,
readme_approved_by, readme_approved_on}`, both approval fields are `""` for
both datasets, and no other key matching `approv|release|steward|authoris|authoriz`
exists (`test_the_shipped_catalog_ships_both_approval_fields_empty`).

## Verification

Interpreter: `/home/chenry/venvs/kbdl/bin/python` (CPython 3.11.15), pytest
9.1.1, pyarrow 25.0.1. All runs in the FOREGROUND, `TMPDIR=/scratch/chenry/tmp`,
`PYTHONPATH=<worktree>/src`.

### The new tests

```
python -m pytest tests/berdl/test_dataset_publish.py -q -p no:cacheprovider --no-header
97 passed in 3.9s                                                   exit 0
```

```
ruff check  src/.../dataset_publish.py tests/berdl/test_dataset_publish.py   -> All checks passed!  exit 0
ruff format src/.../dataset_publish.py tests/berdl/test_dataset_publish.py   -> applied, then clean exit 0
```
(`ruff` is the repo's own pre-commit hook; it is absent from the task venv, so
the binary at `/home/chenry/venvs/kbdl-recon-scratch-layer/bin/ruff` was used
against this repo's `[tool.ruff]` config.)

### Base vs branch, by NODEID

The full suite is ~57 minutes on this machine (recorded in
`build-baselines/kbutillib-h100-main-6310d61.json`), so running it twice does
not fit a 5400 s task and no single Bash call may exceed 600 s. Instead a
base worktree was cut at `72bf81e` in `$MAESTRO_TASK_SCRATCH` and the areas
that could possibly be affected were run on BOTH, comparing non-passing
nodeids:

| Selection | base `72bf81e` | branch | non-passing nodeid diff |
|---|---|---|---|
| `tests/berdl tests/core tests/annotators/test_bakta_call_features.py tests/test_python_floor_syntax.py tests/cli/test_clearinghouse_backfill_cli.py` | 2251 passed, 24 skipped, 1 xfailed, 8 errors (274 s) | 2347 passed, 24 skipped, 1 xfailed, 8 errors (304 s) | **none** — identical 8 ERROR nodeids |
| `tests/cli tests/interfaces tests/kbdl_index tests/guard tests/harness` | 22 failed, 1112 passed, 12 skipped, 2 errors | 22 failed, 1112 passed, 12 skipped, 2 errors (176 s) | **none** — `comm -13`/`comm -23` over the sorted 24 nodeids are both empty |
| `tests/external tests/kbase tests/kb_job_utils tests/kb_app_runner` | 375 passed, 203 skipped | 375 passed, 203 skipped | **none** |
| `tests/` `--collect-only` | 9626 collected, 0 errors (35 s) | 9722 collected, 0 errors (34 s) | +96 = exactly the new tests |

The branch/base passed delta on the first selection is 2347 − 2251 = 96,
which is exactly the new tests in that run (the 97th,
`test_a_non_decimal_current_snapshot_id_is_refused`, was added afterwards;
`tests/berdl` alone then ran **1803 passed, 6 skipped, 0 failed in 224 s**).

Pre-existing failures, present on BOTH base and branch and NOT touched:

- 8 errors, `tests/core/test_composition_smoke.py` (`TestMSBiochemUtils` x3,
  `TestMSFBAUtils` x3, `TestKBModelUtils` x1, `TestEscherUtils` x1) —
  optional scientific deps missing from this venv.
- 22 failed + 2 errors under `tests/cli`: `test_kind.py` (17),
  `test_kind_install_containment.py` (1), `test_model.py` (2 failed +
  2 errored), `test_model_recording.py` (2) — environment/KIND-install and
  modelseed-dependent, unrelated to this change.

Full sorted nodeid lists for the second selection are at
`$MAESTRO_TASK_SCRATCH/{base,branch}-cli.txt` (ephemeral scratch).

### What was NOT run

`tests/biochem`, `tests/modeling`, `tests/domains`, `tests/literature`,
`tests/models_and_analyses`, `tests/notebook`, `tests/verab`,
`tests/arc_context`, `tests/beril_worktree`, `tests/koros_arc_store` and a
few root-level modules (~6,200 tests, ~40 min per run, so ~80 min for a
base/branch pair) were not executed. Stated plainly rather than implied: the
change is purely additive, nothing in the tree imports `dataset_publish`
(the berdl `__init__` was not touched), repo-wide collection is clean on both
commits, and the two global sweeps that DO read every source file or
`pyproject.toml`
(`tests/test_python_floor_syntax.py`,
`tests/annotators/test_bakta_call_features.py::TestDriverPackaging`) both
pass. The h100 baseline at `6310d61` records 69 failures with none in the
berdl area; it is five weeks old, so it was used for orientation only and
every comparison above is against a fresh run of the actual base commit.

## New tests, by nodeid (97)

- tests/berdl/test_dataset_publish.py::test_an_unset_staging_root_is_refused
- tests/berdl/test_dataset_publish.py::test_a_tenant_warehouse_staging_root_is_refused
- tests/berdl/test_dataset_publish.py::test_a_root_without_the_exports_staging_suffix_is_refused
- tests/berdl/test_dataset_publish.py::test_a_valid_root_is_normalised_without_its_trailing_slash
- tests/berdl/test_dataset_publish.py::test_require_staging_root_reads_the_environment
- tests/berdl/test_dataset_publish.py::test_the_staging_port_refuses_a_prefix_outside_the_root
- tests/berdl/test_dataset_publish.py::test_every_staging_method_goes_through_the_guard
- tests/berdl/test_dataset_publish.py::test_the_spark_uri_for_a_prefix
- tests/berdl/test_dataset_publish.py::test_status_no_versions
- tests/berdl/test_dataset_publish.py::test_status_unchanged
- tests/berdl/test_dataset_publish.py::test_status_compaction_only
- tests/berdl/test_dataset_publish.py::test_status_changed_lists_the_operations
- tests/berdl/test_dataset_publish.py::test_status_unknown_for_a_broken_parent_chain
- tests/berdl/test_dataset_publish.py::test_status_unknown_for_a_rollback_older_by_committed_at
- tests/berdl/test_dataset_publish.py::test_status_target_may_be_pinned_by_the_caller
- tests/berdl/test_dataset_publish.py::test_status_accepts_a_nested_source_snapshot_id
- tests/berdl/test_dataset_publish.py::test_a_404_by_attribute_also_reads_as_no_versions
- tests/berdl/test_dataset_publish.py::test_a_non_404_version_read_error_is_not_swallowed
- tests/berdl/test_dataset_publish.py::test_status_lists_leftover_staging_without_deleting_it
- tests/berdl/test_dataset_publish.py::test_status_says_so_when_the_staging_root_is_unset
- tests/berdl/test_dataset_publish.py::test_publish_exports_verifies_uploads_and_registers
- tests/berdl/test_dataset_publish.py::test_publish_keeps_the_workdir_when_asked
- tests/berdl/test_dataset_publish.py::test_publish_pins_the_target_even_when_the_head_advances
- tests/berdl/test_dataset_publish.py::test_publish_proceeds_on_unknown
- tests/berdl/test_dataset_publish.py::test_unchanged_and_compaction_only_stop_unless_forced[snapshots0-unchanged]
- tests/berdl/test_dataset_publish.py::test_unchanged_and_compaction_only_stop_unless_forced[snapshots1-compaction_only]
- tests/berdl/test_dataset_publish.py::test_dry_run_touches_no_port_and_still_reports_everything
- tests/berdl/test_dataset_publish.py::test_dry_run_reports_the_workdir_phase_without_touching_it
- tests/berdl/test_dataset_publish.py::test_an_unapproved_readme_refuses_a_publish_but_not_a_dry_run
- tests/berdl/test_dataset_publish.py::test_a_dry_run_reports_an_unset_staging_root_instead_of_raising
- tests/berdl/test_dataset_publish.py::test_a_real_publish_refuses_an_unset_or_tenant_warehouse_root[None]
- tests/berdl/test_dataset_publish.py::test_a_real_publish_refuses_an_unset_or_tenant_warehouse_root[tenant-general-warehouse/kbaseincubator/_exports_staging]
- tests/berdl/test_dataset_publish.py::test_a_three_way_count_disagreement_aborts_before_any_upload
- tests/berdl/test_dataset_publish.py::test_a_summary_without_total_records_aborts
- tests/berdl/test_dataset_publish.py::test_a_summary_without_total_files_size_aborts
- tests/berdl/test_dataset_publish.py::test_zero_total_records_is_refused
- tests/berdl/test_dataset_publish.py::test_an_export_with_zero_parquet_files_aborts
- tests/berdl/test_dataset_publish.py::test_staging_is_deleted_when_the_export_itself_raises
- tests/berdl/test_dataset_publish.py::test_a_failed_delete_is_a_warning_and_does_not_mask_the_export_error
- tests/berdl/test_dataset_publish.py::test_staging_is_deleted_when_an_upload_fails
- tests/berdl/test_dataset_publish.py::test_an_object_id_that_is_not_the_sha256_aborts
- tests/berdl/test_dataset_publish.py::test_a_confirmed_upload_with_no_object_id_aborts
- tests/berdl/test_dataset_publish.py::test_every_upload_is_private_and_correctly_typed
- tests/berdl/test_dataset_publish.py::test_a_201_dedup_upload_queues_no_job_and_still_publishes
- tests/berdl/test_dataset_publish.py::test_two_part_files_are_renamed_in_sorted_order_and_success_is_ignored
- tests/berdl/test_dataset_publish.py::test_files_with_different_arrow_schemas_abort
- tests/berdl/test_dataset_publish.py::test_state_json_is_written_atomically_with_exactly_the_listed_keys
- tests/berdl/test_dataset_publish.py::test_a_registration_timeout_leaves_phase_manifest_built
- tests/berdl/test_dataset_publish.py::test_a_rerun_at_phase_exported_calls_export_zero_times
- tests/berdl/test_dataset_publish.py::test_a_rerun_at_phase_manifest_built_reuses_the_exact_manifest[False]
- tests/berdl/test_dataset_publish.py::test_a_rerun_at_phase_manifest_built_reuses_the_exact_manifest[True]
- tests/berdl/test_dataset_publish.py::test_an_unusable_state_json_restarts_from_a_clean_workdir[kwargs0-unparseable]
- tests/berdl/test_dataset_publish.py::test_an_unusable_state_json_restarts_from_a_clean_workdir[kwargs1-a schema this publisher does not know]
- tests/berdl/test_dataset_publish.py::test_an_unusable_state_json_restarts_from_a_clean_workdir[kwargs2-a different target]
- tests/berdl/test_dataset_publish.py::test_a_state_json_whose_files_do_not_verify_restarts_clean
- tests/berdl/test_dataset_publish.py::test_a_manifest_built_state_with_a_missing_manifest_restarts_clean
- tests/berdl/test_dataset_publish.py::test_a_registered_state_is_reported_and_republished_only_under_force
- tests/berdl/test_dataset_publish.py::test_a_409_carrying_a_tombstone_is_reported_as_an_error
- tests/berdl/test_dataset_publish.py::test_a_409_without_a_tombstone_propagates_unchanged
- tests/berdl/test_dataset_publish.py::test_a_200_body_carrying_a_tombstone_is_reported_as_an_error
- tests/berdl/test_dataset_publish.py::test_the_shipped_catalog_ships_both_v1_datasets
- tests/berdl/test_dataset_publish.py::test_the_shipped_catalog_ships_both_approval_fields_empty
- tests/berdl/test_dataset_publish.py::test_the_shipped_readmes_are_byte_for_byte_as_specified[genome_quality.md-DRAFT -- not approved for publication\n\n# genome_clearhouse.genome_quality\n\nOne row per genome in the KBase genome clearinghouse, with its genome quality metrics.\nThis file set is a full snapshot of the lakehouse table `kbaseincubator.genome_clearhouse.genome_quality`,\npinned to the Iceberg snapshot id recorded in `MANIFEST.json` (`source.snapshot_id`).\n\n## Columns to read first\n\n- `released` (boolean). `true` means the genome is part of the released genome inventory.\n  1,634,825 SPIRE metagenome bins that SPIRE itself did not release are kept in the table with `released = false`;\n  they were flagged, not deleted, so nothing is lost. Count genomes with `released = true`.\n- `gtdb_release` (string). For a genome drawn from GTDB, the GTDB release it came from (`r220` or `r232`).\n  It is not set for genomes from other sources.\n\n## Provenance and verification\n\nThe manifest lists every file with its SHA-256 (equal to its KBDL object id), size and row count,\nand records the row count checked three ways at export time.\n]
- tests/berdl/test_dataset_publish.py::test_the_shipped_readmes_are_byte_for_byte_as_specified[skani_distances.md-DRAFT -- not approved for publication\n\n# genome_clearhouse.skani_distances\n\nskani average-nucleotide-identity results for the genomes in `genome_clearhouse.genome_quality`.\nThis file set is a full snapshot of the lakehouse table `kbaseincubator.genome_clearhouse.skani_distances`,\npinned to the Iceberg snapshot id recorded in `MANIFEST.json` (`source.snapshot_id`).\n\n## This is NOT a pairwise distance table\n\nThere is one row per genome per reference database (`db_name`).\nEach row carries `n_hits`, `top_hit` and `top_ani`, and a `distances` string column that holds the serialised list of hits.\nTo get pairs, parse `distances`.\n\n## Relationship to genome_quality\n\nJoin on the genome identifier. SPIRE bins with `released = false` in `genome_quality` may have no row here.\n\n## Provenance and verification\n\nThe manifest lists every file with its SHA-256 (equal to its KBDL object id), size and row count,\nand records the row count checked three ways at export time.\n]
- tests/berdl/test_dataset_publish.py::test_every_shipped_readme_is_readable_through_the_catalog
- tests/berdl/test_dataset_publish.py::test_a_readme_file_outside_the_catalog_directory_is_refused
- tests/berdl/test_dataset_publish.py::test_an_unknown_dataset_name_is_refused
- tests/berdl/test_dataset_publish.py::test_a_catalog_name_that_is_not_namespace_dot_table_is_refused
- tests/berdl/test_dataset_publish.py::test_the_published_manifest_passes_the_schema_1_rules
- tests/berdl/test_dataset_publish.py::test_the_reference_manifest_is_valid
- tests/berdl/test_dataset_publish.py::test_an_unknown_top_level_key_is_ignored
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[schema-2]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[schema-boolean]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[snapshot-id-number]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[unknown-operation]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[not-a-z-timestamp]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[source-missing-key]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[source-extra-key]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[name-mismatch]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[row-count-boolean]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[row-count-inconsistent]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[empty-schema]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[no-files]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[bad-part-name]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[uppercase-object-id]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[over-4-gib]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[under-12-bytes]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[duplicate-file]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[verification-mismatch]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[empty-readme]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[oversized-readme]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[empty-tool]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[short-format]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[null-value]
- tests/berdl/test_dataset_publish.py::test_the_schema_1_rules_reject[missing-required-key]
- tests/berdl/test_dataset_publish.py::test_canonical_serialisation_is_sorted_and_compact
- tests/berdl/test_dataset_publish.py::test_a_non_decimal_current_snapshot_id_is_refused
