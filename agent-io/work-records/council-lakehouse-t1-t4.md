# Work Record — council-lakehouse-t1-t4

## task_id
council-lakehouse-t1-t4 (serial tranche: PRD `council-record-lakehouse-v1`, phases P1–P4 of 11)

## branch
`maestro/developer/serial-tranche-four-items-one-br-council-lakehouse-t1-t4`

## base_commit
`ec78ea7` — `docs(work-record): task-aa22e94b ontomap council-status, runbook, rehearsal`

## commit_shas (one per item, in order)
- `5d579e3` — t1-council-schema
- `5e4724e` — t2-store-identity
- `3b8d9e7` — t3-clearinghouse-reference
- `d9727de` — t4-stage1-unrun-bundle

## status
**All four items complete.** None left not-started, none partial.

## environment
- Host: h100 (`poplar.cels.anl.gov`), worktree
  `/home/chenry/.maestro/worktrees/council-lakehouse-t1-t4`.
- Interpreter for every test run: `~/venvs/kbdl/bin/python3` — CPython
  3.11.15, pytest 9.1.1, SQLAlchemy 2.0.52, pyarrow 25.0.1. This matches the
  interpreter/pytest pair recorded in `build-baselines/kbutillib.json`. The
  task venv at `.task-venv/` has no pytest and no SQLAlchemy, so it could not
  be used; nothing was installed into any environment.
- `PYTHONPATH=$PWD/src` and `TMPDIR=/scratch/chenry/tmp` on every run, as
  instructed.
- `ruff` is **not installed** in any available environment, so no lint pass
  was run. Import ordering and line discipline were matched to the
  surrounding modules by hand.
- The full suite was never run (45–57 min, above the 600 s tool ceiling).
  Every number below is a targeted selection.

---

# t1 — council-schema

## PRECONDITION ANSWERS (both RUN, neither UNRUN)

The snapshot named by the prompt **exists on this machine**:
`~/Dropbox/Jobs/function-to-go/_store-backups/council-snapshot-20261023.sqlite.xz`
with `MANIFEST.json` beside it. It was decompressed to
`/scratch/chenry/tmp/council-snapshot-20261023.sqlite` (896,069,632 bytes) and
**never into the worktree**. Its uncompressed sha256 is
`37535e28382fd3323af9eab2fa0a6f4612d0852856c650cf6cf42740e9bcef32`, which
matches the manifest's `uncompressed_sha256` exactly. Queried read-only
(`mode=ro`). There is **no named gap**.

### (a) Does `query_types` hold a row with `name='go_mf'` and `version='4'`?

**YES.** Verbatim row:

```
('go_mf', '4', 'active', 'user', 'chenry', '2026-10-20 17:43:46.472532')
```

(columns `name, version, status, source, created_by, created_at`). All five
`query_types` rows:

```
('go_mf', '1', 'active', 'builtin',   'builtin', '2026-10-19 04:38:39.672040')
('go_mf', '2', 'active', 'directory', 'chenry',  '2026-10-19 04:58:08.289214')
('go_mf', '3', 'active', 'directory', 'chenry',  '2026-10-19 05:01:15.986050')
('go_mf', '4', 'active', 'user',      'chenry',  '2026-10-20 17:43:46.472532')
('reaction_pickup', '1', 'active', 'builtin', 'builtin', '2026-10-19 04:38:39.686142')
```

So `role_brief` **does** have a spec to extract from for this run.

### (b) Is `calls.reply_parsed` non-NULL for `role='critic'` and `role='meta'`, or only for proposers? Counts per role.

**Critic and meta both carry a structured `reply_parsed`. It is not
reply_text-only for either.** Downstream queries can query structure rather
than parsing at read time.

One measurement caveat that changes how the raw counts must be read, and
which I checked rather than assumed: the column is `sa.JSON` with the
SQLAlchemy default `none_as_null=False`, so a Python `None` is stored as the
**JSON text `'null'`**, not as SQL NULL. `reply_parsed IS NOT NULL` is
therefore true for all 203,821 rows and says nothing. The meaningful split is
SQL NULL vs JSON-`null` vs a substantive parsed object:

**All calls (203,821):**

| role | rows | SQL NULL | JSON `'null'` | substantive object |
|---|---|---|---|---|
| critic | 20,391 | 0 | 188 | 20,203 |
| meta | 4,951 | 0 | 78 | 4,873 |
| proposer | 178,479 | 0 | 116,179 | 62,300 |

**LIVE calls only (88,465):**

| role | rows | SQL NULL | JSON `'null'` | substantive object |
|---|---|---|---|---|
| critic | 20,368 | 0 | 165 | 20,203 |
| meta | 4,951 | 0 | 78 | 4,873 |
| proposer | 63,146 | 0 | 846 | 62,300 |

`reply_text` non-NULL for comparison (all calls): critic 20,258, meta 4,949,
proposer 62,344.

Sample substantive shapes, one per role, showing that the structure is real
and queryable:

- proposer — `{"picked": [{"go_id": "GO:0030337", "confidence": "high", "why": "…"}], "overall_confidence": "high", "no_functional_content": false}`
- critic — `{"verdict": "strongly_consistent", "reason": "…"}`
- meta — `{"final_go_ids": ["GO:0030776", "GO:0030794"], "final_confidence": "high", "no_functional_content": false, "reasoning": "…"}`

### (c) — already answered in the prompt, and confirmed

All six `workers.version` values in this snapshot are NULL:

```
('poplar.cels.anl.gov-225c93d6', 'poplar.cels.anl.gov', None, '2026-10-19 04:45:07.573351')
('poplar.cels.anl.gov-0a9b3005', 'poplar.cels.anl.gov', None, '2026-10-19 04:54:10.918708')
('poplar.cels.anl.gov-02771437', 'poplar.cels.anl.gov', None, '2026-10-19 04:58:45.490284')
('poplar.cels.anl.gov-175be1ac', 'poplar.cels.anl.gov', None, '2026-10-19 05:01:25.783443')
('poplar.cels.anl.gov-c492f153', 'poplar.cels.anl.gov', None, '2026-10-19 05:25:15.708854')
('poplar.cels.anl.gov-acebb076', 'poplar.cels.anl.gov', None, '2026-10-19 05:36:25.598926')
```

The `workers` table publishes anyway and the NULLs are preserved
(`test_workers_keeps_version_even_though_the_snapshot_has_only_nulls`).

### Other measured facts confirmed against the snapshot

- `calls` 203,821 total; `live` **88,465** snapshot-wide. The prompt's 82,270
  is the production run specifically: live calls of the **done `go_mf@4`**
  jobs is exactly **82,270** (82,553 across all `go_mf@4` jobs including the
  two cancelled ones). Both numbers are in the module docstring so the
  difference is not rediscovered as a discrepancy.
- Call status breakdown: ok 87,376, superseded 115,356, error 723, pending 366.
- `items` has no `query_name` column —
  `PRAGMA table_info(items)` → `job_id, item_id, entity_hash, data_hash,
  evidence, menu, item_order, status, verdict, decided_at`.
- `items.entity_hash`, `items.data_hash`, `calls.entity_hash`,
  `calls.prompt_hash` and `jobs.roster_fingerprint` are all 64-char lowercase
  hex. **`calls.upstream_digest` is the empty string**, not a digest — so the
  hash seam explicitly excludes it, and a test asserts the seam refuses `""`.

## What was built

`src/kbutillib/domains/kbase/berdl/council_schema.py` (new, 882 lines), a
structural mirror of `clearinghouse_schema.py`: nine archived tables of a new
`kbaseincubator.council` namespace, each table's columns declared **once** as a
module-level `(name, sql_type)` tuple with `table_name`, `table_columns`,
`column_names`, `table_primary_key`, `table_configs` and `bootstrap` all
derived from those tuples. No parenthesis in any config value; no `NOT NULL`
in the DDL path; `partition_by` omitted entirely when unpartitioned.

Two deliberate reuse decisions, both documented in the module:

- `encode_entity_hash` / `decode_entity_hash` are **re-exported** from
  `clearinghouse_schema` rather than re-implemented. Both namespaces store
  the same 64-char lowercase hex form, so one normalisation rule with one
  implementation cannot drift; a second copy could, and the drift would only
  surface where neither copy can be tested.
- `bootstrap()` **delegates** to `clearinghouse_schema.bootstrap()` with
  council defaults. That function reads only `name`, `partition_by` and (via
  `load()`) `schema_sql` and never parses a name into a type and kind — its
  own docstring says so — so its control flow is not clearinghouse-specific.
  The two refusal types are re-exported rather than redefined.

Dropped on archive: `calls`' five lease/queue columns, `items.item_order`,
`jobs.priority`, `endpoint_state.halt_kind`/`halt_message`, and all of
`workers` except `worker_id`/`host`/`version`/`started_at`. `items.entity_hash`
is renamed `council_entity_hash`. `calls.live` is archived as a column and
never filtered.

Two denormalisations, each with a named resolver so the rule is testable
rather than inlined in a join: `item_query_name()` (from `jobs`) and
`attempt_query_name()` (from the owning call, `jobs` as the legacy-NULL
fallback, `ContradictoryQueryNameError` on a non-NULL disagreement — not
silently resolved, because `query_name` is the partition key and a wrong
value is baked into the physical layout).

`job_disposition` and `role_brief` are declared; `role_brief` ships a
`ROLE_BRIEF_UNEXTRACTABLE` sentinel so "a spec shape you cannot extract from
must be recorded as unextractable rather than guessed at" is a value contract
and not only prose.

## files_touched
- `src/kbutillib/domains/kbase/berdl/council_schema.py` (new)
- `tests/berdl/test_council_schema.py` (new — 84 tests)

## success_criteria_check (t1)

- **Exposes `table_name`, `table_columns`, `column_names`, `table_configs`,
  `bootstrap` for nine archived tables** — PASS.
  `test_nine_archived_tables`, `test_resolves_every_archived_table_to_itself`,
  `test_configs_are_emitted_in_archived_tables_order`.
- **Seven dropped queue columns absent** — PASS.
  `test_the_five_lease_columns_are_absent_from_calls` (and from *every*
  table), `test_items_item_order_is_absent`, `test_jobs_priority_is_absent`.
  Each asserts the column **exists in the live store schema** and is absent
  here, so the test cannot pass by the column having been renamed away.
- **`council_entity_hash` rename, no bare `entity_hash` on `items`** — PASS.
  `TestCouncilEntityHashRename`.
- **`query_name` on both `items` and `attempts`** — PASS, each asserted
  against the store's own absence of the column.
- **`live` on `calls`** — PASS, plus the five `ux_calls_one_live` key columns
  and the reuse/cost columns.
- **Primary keys lead with `(store_id, snapshot_id)`** — PASS for all nine,
  with the tail checked against the store table's own primary key.
- **`partition_by` on exactly `calls`/`items`/`attempts`** — PASS,
  `test_partition_by_is_set_on_exactly_calls_items_and_attempts` plus
  `test_the_other_six_omit_the_key_entirely_rather_than_emitting_a_falsy_value`.
- **`bootstrap()` idempotency** — PASS, `test_bootstrap_is_idempotent` (three
  runs; first `create`, then `append`, columns re-read from the catalog each
  time).
- **NO-INJECTION** — PASS, **and the work record owes a statement here.**
  `clearinghouse_schema`'s own bootstrap tests
  (`tests/berdl/test_clearinghouse_bootstrap.py`) drive it against a
  hand-built `_FakeCapability` whose `load()` records its arguments and writes
  nothing: **there is no local engine or SQL sink in that module to mirror.**
  Rather than inventing a live Iceberg sink, `_SqliteSinkCapability` in the
  new test module **executes each config's `schema_sql` as real DDL against a
  real SQLAlchemy/SQLite database** and answers
  `table_exists`/`table_partition_spec` by reading that database back.
  `bootstrap()` runs through its real default code path (`tables` defaulted to
  `table_configs()`) and the nine tables plus their columns are asserted
  **from the catalog**, not from the config dict. What that does not prove:
  SQLite is not Iceberg — no real partition specs (the sink records
  `partition_by` in a side table by its own convention), no Iceberg table
  properties, no `data_lakehouse_ingest` type map. Stated in the test
  module's docstring too.

## targeted tests (t1)

```
PYTHONPATH=$PWD/src TMPDIR=/scratch/chenry/tmp \
  ~/venvs/kbdl/bin/python3 -m pytest tests/berdl/test_council_schema.py -q
→ 84 passed      exit 0
```

Modules t1 mirrors but does not edit, before and after (node-id comparison):

```
tests/berdl/test_clearinghouse_schema.py
tests/berdl/test_clearinghouse_bootstrap.py
tests/berdl/test_clearinghouse_bootstrap_adapter.py
  base ec78ea7 : 140 passed, 0 failed   exit 0
  branch        : 140 passed, 0 failed   exit 0   (226 with package-exports added)
```

---

# t2 — store-identity

## What was built

`store/sqlite.py` (+617 lines) and `config.py` (+24).

- A **versioned identity record persisted inside the store database**, in its
  own append-only `store_identity` table: `declared_seq`, `record_version`,
  `store_id`, `declared_at`, `declared_by`, `supersedes`. Inside the file so a
  copy carries it — config is per-machine and a sidecar is dropped by every
  `cp` that does not know about it.
- **Deliberately outside `schema.py`'s versioned `MetaData`**, with its own
  `STORE_IDENTITY_RECORD_VERSION`. Two reasons, both recorded in the module:
  every store that already exists — including the one this is for — was
  created at `SCHEMA_VERSION = 2` without it and `migrate()` adds columns and
  indexes, not tables, so an identity reachable only by migration is an
  identity the existing store cannot have; and it must be readable with
  `check_version=False`, because "which store did these bytes come from" is
  exactly the question asked about an old file. (`schema.py` is also outside
  this item's allowed file set, which the design agrees with rather than
  works around.)
- `store_identity()` **reads without writing**, so it works on a read-only
  snapshot; an absent table reads as "no identity", not an error.
- `assign_store_identity()` — idempotent for the same id (one row after three
  calls), `StoreIdentityAlreadyDeclared` for a different one.
  `redeclare_store_identity()` is the **explicit act** an independently
  writable fork needs; it records `supersedes` and leaves the old row in
  place, because a later reader has no other way to tell a fork from a store
  that always had its current id. A move or a read-only snapshot simply
  retains the identity, by construction.
- `require_store_identity()` is the gate: `StoreIdentityMissing` when nothing
  is declared, `StoreIdentityMismatch` when configured and stored disagree —
  **naming both values, never auto-healing**, because a mismatch means
  somebody copied a store and kept writing to it.
- `snapshot()` uses SQLite's own **backup API**, not a `cp` of the db+WAL
  pair, and returns the **verified sha256 of the file it wrote** — which is
  exactly what `council_schema`'s `snapshot_id` column holds. `export()`
  writes one JSONL per store table plus a `MANIFEST.json` carrying the
  identity, a per-file digest and row counts. Both refuse before writing
  anything.
- `validate_store_id()` refuses malformed values **and values that look
  automatically derived** — a path, a `~`/`.`-relative path, a `.sqlite`/`.db`
  filename, a bare or dashed uuid, whitespace, over 128 chars. Passing an
  automatic-looking value in by hand is the cheapest way to get an automatic
  default back.
- `declare_identity_once(url, store_id, declared_by=)` is the documented,
  runnable, idempotent one-off. `POPLAR_COUNCIL_STORE_ID = "poplar/council-1"`
  and its docstring carries the exact command. **The one-off procedure, for
  the record:**

  ```
  python -c "from kbutillib.domains.ai.council.store.sqlite import \
      declare_identity_once, POPLAR_COUNCIL_STORE_ID; \
      print(declare_identity_once(
          '/scratch/chenry/council/council.sqlite',
          POPLAR_COUNCIL_STORE_ID, declared_by='chenry'))"
  ```

  Running it twice is a no-op; against a store already carrying a different
  id it raises and the process exits non-zero. It opens with
  `check_version=False` so a version-mismatched store can still be stamped.
- `config.py`: `CouncilConfig.store_id: Optional[str] = None`, **no default
  and a comment recording that none may ever be added.** It is the
  *configured* value compared against the store's own record, not the
  authority.
- `SqliteStore.__init__` gains `store_id=`, validated at open time and
  reachable through `open_store(url, store_id=...)` since that function
  already forwards `**kwargs` — so **`store/__init__.py` was not touched.**

## files_touched
- `src/kbutillib/domains/ai/council/store/sqlite.py`
- `src/kbutillib/domains/ai/council/config.py`
- `tests/domains/ai/council/test_store_identity.py` (new — 53 tests)

## success_criteria_check (t2)

- **Versioned record inside the database, carried by a file copy** — PASS.
  `test_a_real_shutil_copy_of_the_file_carries_the_identity`,
  `test_a_read_only_snapshot_carries_the_identity`,
  `test_the_record_persists_across_a_reopen`,
  `test_the_identity_table_is_outside_the_versioned_schema`.
- **snapshot and export refuse non-zero when absent** — PASS.
  `test_snapshot_refuses_and_names_the_absence`,
  `test_export_refuses_and_names_the_absence` (both also assert nothing was
  written), and `test_the_refusal_exits_non_zero_in_a_real_subprocess`, which
  runs a real `subprocess` and asserts `returncode != 0` with
  `StoreIdentityMissing` on stderr — so "refuse non-zero" is literal and not
  only an exception.
- **…and when configured and stored disagree, naming both and not
  auto-healing** — PASS. `TestConfiguredVersusStoredMismatch`: both values in
  both messages, nothing written, and the stored record untouched afterwards.
- **No automatic default from hostname, path or uuid** — PASS.
  `TestNoAutomaticDefault`, including
  `test_a_store_never_assigns_itself_an_identity` (which runs `init_schema`,
  `migrate`, `add_missing_columns` and `add_missing_indexes` and asserts the
  identity is still absent) and a 13-case parametrised refusal set.
- **Poplar store assignable to `poplar/council-1` idempotently** — PASS.
  `TestThePoplarOneOff`, including on a version-mismatched store.
- **Explicit act required to replace a declared identity** — PASS.
  `test_an_independently_writable_fork_needs_the_explicit_act`,
  `test_redeclaring_keeps_the_superseded_row_visible`.
- **NO-INJECTION** — PASS. Every store in the module is a real SQLite file
  created by the real `SqliteStore` constructor and the real
  `init_schema()`/`migrate()` path; every refusal comes from the real
  `snapshot()`/`export()` entry points; the copy tests use `shutil.copy2` on
  disk. No fake store object, no monkeypatched identity reader.

## targeted tests (t2)

```
tests/domains/ai/council/test_store_identity.py
  → 53 passed      exit 0

tests/domains/ai/council/ + tests/cli/test_council_cli.py
  base ec78ea7 : 1238 passed, 6 failed    exit 1
  after t2      : 1291 passed, 6 failed    exit 1   (+53 = the new tests)
  after all four: 1291 passed, 6 failed    exit 1
  node-id diff (base vs branch): EMPTY — identical failing set
```

The six failures are the known machine-local ones, every one in
`tests/domains/ai/council/test_types_config.py::TestPackagedDefaultConfig`
(the h100 user config leaking past a monkeypatched `HOME`). The task prompt
said four; the measured set is six, all in that one class. Node ids:

```
TestPackagedDefaultConfig::test_every_packaged_profile_builds_a_roster
TestPackagedDefaultConfig::test_the_packaged_config_declares_no_poplar_endpoint
TestPackagedDefaultConfig::test_default_profile_is_mixed_subscription
TestPackagedDefaultConfig::test_on_a_fresh_machine_with_no_kbutillib_config
TestPackagedDefaultConfig::test_codex_seats_are_marked_unverified
TestPackagedDefaultConfig::test_cli_endpoints_default_to_one_call_at_a_time
```

Left alone, as instructed.

---

# t3 — clearinghouse-reference

## The reference genuinely did not exist — verified, not assumed

On `main` `ec78ea7`: `function-to-go-council.toml`'s payload was
`@json(function_text, go_ids, go_confidences, final_confidence, decided_by,
no_functional_content, decline_reason, n_valid_proposers, votes,
roster_fingerprint, route_fingerprints)` — no council block, no `job_id`, no
`item_id`. `verdicts.py` carried a list `job_ids` and no winning item
reference. Read both before changing either.

## What was built

- `EXPORT_COLUMNS` gains `status` and `council`, **appended** so every
  pre-existing column keeps its name and position — the preset names columns
  out of this list and a reader of an older parquet must still find them where
  they were (`test_every_pre_existing_column_keeps_its_name_and_position`).
- `council` is a **declared pyarrow struct** of `(store_id, job_id, item_id)`,
  nullable as a whole. **One struct rather than three flat columns, and the
  write path forces that choice:** the preset emits its payload with
  `@json(col, …)`, which `clearinghouse_shard._derive` evaluates as
  `{col: row.get(col)}` — it cannot assemble a *nested* object out of three
  flat columns. And three nulls side by side is not the statement
  `council = null` makes: the first says "three unknown fields", the second
  says "there is no council item". Declared rather than inferred for the same
  reason `route_fingerprints` is: an export in which every row abstained would
  otherwise infer a null-typed column and two files of one source would
  disagree.
- `council = null` for retrieval abstentions, decline fields retained.
- The **winning** item, not a list: `verdicts_by_item` lets a later job
  overwrite an earlier one's entry, so `settled["job_id"]` *is* the winning
  job — which is how a retried item names its retry job rather than the chunk
  job that failed. `job_ids` stays the list of every job read.
- `status` (the council **item's** status) and `retried` published explicitly.
- `council_reference()` refuses any empty part, so a decided row cannot
  publish a partial triple that looks joinable. An abstain-only export needs
  no `store_id` at all.
- A comment at the payload construction site **and** in the preset recording
  that the 19,268 rows already loaded can never gain the reference, because
  the slot key excludes `result_type_version` so a corrected payload in the
  same slot is silently dropped — and that they stay joinable only by the
  documented convention that exactly one store produced them.
- The shipped example parquet was **regenerated through the export's own
  writer** so the fixture carries both new columns: four decided rows with a
  full triple under the inert `example/council-0`, and the abstained row with
  `council = null`, making the nullable case visible in a smoke run of the
  preset as shipped.

## success_criteria_check (t3)

- **Export and parquet schema carry the winning store_id/job_id/item_id** —
  PASS. `TestCouncilDecidedRows`, `TestParquetRoundTrip`.
- **`council={store_id,job_id,item_id}` for decided rows, `null` for
  retrieval abstentions with decline fields retained** — PASS.
  `TestRetrievalAbstentionsCarryNull` (six assertions over the retained
  fields, including `no_functional_content is None` and the real
  `decided_at`).
- **`status` and `retried` explicit** — PASS.
  `TestStatusAndRetriedAreAlwaysPresent`.
- **Never the council's own entity_hash** — PASS.
  `TestNeverTheCouncilsOwnEntityHash`, three ways (no row key, no declared
  column name, no struct field).
- **Preset updated; `parameter_set` contents unchanged from the base commit**
  — PASS. Hash mode (`precomputed = "query_id"`), `observed_at_column`,
  `entity_type`, `kinds` and the `parameter_set` line are untouched, and the
  three lines `council_publish.py` pins by **exact text** (`_MARKER_PATH`,
  `_MARKER_SOURCE`, `_MARKER_PARAMETER_SET`) are byte-identical — confirmed
  by `tests/domains/test_ontomap_council_publish.py` passing unchanged,
  including `test_the_four_markers_are_substituted_from_the_plan` and
  `test_a_marker_that_moved_refuses_rather_than_publishing_the_template`.
  `EXPORT_IDENTITY_FIELDS` is asserted unchanged in
  `test_the_parameter_set_fields_are_unchanged`.
- **Comment at the payload construction site** — PASS, present in
  `verdicts.py` and mirrored in the preset.
- **NO-INJECTION** — PASS. `TestEndToEndThroughARealStore` builds a real
  SQLite council store with the real `open_store`/`init_schema`, declares its
  identity with the real `assign_store_identity`, creates real jobs and items
  through the real `create_job`, settles them with the real `settle_item`,
  reads them back with the real `results()`, drives the real `export_rows`
  and the real `write_verdict_parquet`, and asserts the three columns and the
  null-abstention row **by reading the file back with pyarrow**. A second test
  takes the triple off the file and joins it back to the real store's item
  row.

## targeted tests (t3)

```
tests/domains/test_verdict_council_reference.py
  → 31 passed      exit 0
```

Combined selection of the 15 existing modules t3/t4 can reach
(`/scratch/chenry/tmp/t1t4-baseline/SELECTION.txt`):

```
  base ec78ea7 : 645 passed, 3 skipped, 0 failed   exit 0
  after t3      : 676 passed, 3 skipped, 0 failed   exit 0
  node-id diff  : EMPTY
```

---

# t4 — stage1-unrun-bundle

## What was built

- `kbu ontomap council-unrun-bundle` →
  `OntologyMappingUtils.council_unrun_bundle(bundle_dir, query=,
  parameter_set=)`. Performs **only** the two clearinghouse reads and the set
  difference, and writes a versioned immutable bundle: `BUNDLE.json`
  (`schema_version`, `query`, `source`, the `parameter_set` with its canonical
  pre-image and hash, `generated_at`, universe/done/unrun counts, and a
  **sha256 inventory over its own files**) plus `unrun.jsonl` (`query_id` and
  the **exact, unnormalised** definition text per un-run function —
  `query_id` is the clearinghouse's digest *of that text*, so a text the
  bundle "tidied" would no longer be the function the key names). It does not
  rank, partition, chunk or prepare a quote, and it touches neither the
  council store nor the council namespace.
- The **parameter set is an input, not derived in the pod**: deriving it needs
  the council roster and the retrieval mapping version, which are exactly the
  requirements this split keeps out of there. It is canonicalised and hashed
  through the clearinghouse's own `result_parameter_set_identity` — a pure
  function — so the bundle's hash is the hash the planner recomputes.
  `--parameter-set` accepts inline JSON or `@path` to a file (it is produced
  on h100 and consumed in the pod, so it travels as a file rather than as a
  shell argument a quoting slip could truncate).
- `council_plan` gains `bundle=` / `--bundle`. With it the planner reads no
  clearinghouse and **constructs no BERDL client**; everything downstream is
  unchanged. `PLAN.json` gains one key, `unrun_bundle_file` (null on the
  direct path); `plan_hash` is unaffected because it is derived from the
  query, items, `mapping_hash` and profile rather than from the plan dict.
- `read_unrun_bundle()` is the verifier, four refusals, **all before any
  retrieval**: unknown `schema_version`; digest mismatch named with expected
  vs observed; parameter-set or source mismatch naming both sides (mandatory,
  not advisory); empty un-run set as a producer failure. Plus a declared
  row-count check for a manifest the producer wrote wrong.
- The producer **emits** an empty bundle with a loud warning rather than
  refusing, so the consumer's refusal is the one that counts and is testable.
- Both paths take **explicit** bundle paths. Nothing globs, sorts by mtime, or
  picks a "latest" bundle.

## success_criteria_check (t4)

- **Two commands exist; in-pod reader emits a versioned bundle with a sha256
  inventory, canonical parameter_set and hash** — PASS.
  `TestTheBundleTheInPodReaderWrites` (11 tests), and the CLI registry test
  `test_every_registered_verb_is_either_implemented_or_a_known_stub` now
  includes `council-unrun-bundle` as implemented.
- **Plan command consumes it with mandatory parameter-set validation and no
  BERDL client** — PASS. `TestPlanningFromABundle`,
  `TestNoBerdlClientOnTheH100Path`.
- **Explicit bundle paths, no implicit latest-file selection** — PASS.
  `test_there_is_no_implicit_latest_bundle_selection` (two bundles for
  different parameter sets, the newer one refused when it is not the one
  asked for) and `test_the_bundle_argument_has_no_default`.
- **Inventory match** — PASS, `test_the_inventory_matches_its_content`,
  recomputing the digest in the test.
- **Corrupted digest refused with expected vs observed** — PASS,
  `test_a_corrupted_digest_is_refused_with_expected_and_observed` (both
  digests asserted present in the message), plus
  `test_a_corrupted_bundle_is_refused_before_any_retrieval_runs` which also
  asserts the retrieval backend was never called and no plan was written.
- **Parameter-set mismatch refused** — PASS, three ways: hash mismatch,
  source mismatch, and a bundle built for another query version.
- **Empty un-run set refused** — PASS, in the verifier and through the plan
  command.
- **PLAN.json equivalence against the single-command path** — PASS,
  `test_the_plan_is_equivalent_to_the_single_command_path`: two real plans
  from the same inputs, compared key by key, with `counts`, `chunks`,
  `plan_hash` and `parameter_set_hash` additionally named explicitly. The
  only keys expected to differ are `generated_at`, the run paths, the scratch
  file paths and `unrun_bundle_file`.
- **Abstentions carried as `decline_reason='abstained:*'`** — PASS,
  `test_abstentions_are_carried_forward_not_dropped` and
  `test_an_abstention_is_in_neither_the_submit_nor_the_fanout_set`.
- **A `council_error:*` item gets no result row** — PASS,
  `test_a_council_error_item_is_in_neither_the_done_set_nor_a_result_row`:
  drives the real `export_rows` with an item whose status is `error`, asserts
  it produces no row and is counted `withheld`, then shows the next bundle
  re-selects it because it is absent from `done`.
- **NO-INJECTION** — PASS, and deliberately not vacuous.
  `TestNoBerdlClientOnTheH100Path` patches **`ClearinghouseCapability` and
  `BerdlCapability` to raise** and then runs the **real** `council_plan` with
  its **default** `_clearinghouse()` over a fixture bundle, reaching
  `PLAN.json`. A control test,
  `test_the_poison_really_bites_on_the_direct_read_path`, shows the same
  patch *does* fire when no bundle is passed — without it, the first test
  would pass even if the patch were ineffective. Nothing injects a fake
  clearinghouse into the plan command.

## targeted tests (t4)

```
tests/domains/test_ontomap_unrun_bundle.py
  → 43 passed      exit 0

tests/cli/test_ontomap_cli.py
  → 71 passed, 3 skipped      exit 0
```

Full combined selection (15 base modules + the 4 new ones + council CLI):

```
  base ec78ea7 (15 modules) : 645 passed, 3 skipped, 0 failed   exit 0
  branch (15 + 4 + cli)      : 899 passed, 3 skipped, 0 failed   exit 0
  node-id diff               : EMPTY
```

## IN-POD STATUS: awaiting operator acceptance

Nothing here claims live in-pod acceptance. The in-pod command
(`kbu ontomap council-unrun-bundle`) is **awaiting operator acceptance —
Albert, kbhub.** KBUtilLib#91 is open: the BERDL transport has been broken on
kbhub since the 2026-10-19 image (the removed kwargs
`delta_lake`/`use_hive`/`tenant_name`/`connector` make Spark/Trino calls raise
`TypeError`), worked around in-process by
`albert-load-artifacts/kbu_compat.py`. The named compatibility arguments are
still present in `berdl/transports.py` (see the reconciliation note below).
Everything in all four items was implemented and tested **locally**; no
mocked load was treated as acceptance, and `council_schema.bootstrap()` has
never been run against real Iceberg-on-Polaris.

---

# Reconciliation: what the prompt claimed vs what the tree held

Every citation in the prompt's "CITATION CORRECTIONS" block was checked. Most
were right. These diverged:

1. **`find_reusable` is on `SqliteStore`, not `SqliteCouncilStore`.** The
   prompt cited "`SqliteCouncilStore.find_reusable` at `store/sqlite.py:1436`".
   The line number is correct; the class is `SqliteStore` — there is no
   `SqliteCouncilStore` in the tree. The *fact* stated about it is correct and
   was verified in `store/queries.py:436` `select_reusable`: it filters
   `status == "ok"` across all jobs and users (including tombstoned jobs) and
   does **not** filter `live`. Recorded in `council_schema.py`'s docstring.
2. **`select_reusable` is at `queries.py:436`** — correct, confirmed.
3. **`CouncilConfig.store` at `config.py:809`, class at `:789`** — correct.
   `store_id` was added immediately after it.
4. **`STORE_IDENTITY = "local"` at `queries.py:33`, re-exported at
   `sqlite.py:86`** — correct.
5. **`store/schema.py` line numbers** (`SCHEMA_VERSION=2` at `:54`, `items` at
   `:194`, `calls` at `:216`, `ux_calls_one_live` at `:289`, `endpoint_state`
   at `:327`) — all correct.
6. **`verdicts.py` line numbers** (abstain loop `:271`, `job_ids` parameter
   `:163` / `all_job_ids` `:239` / row column `:116`, per-row `job_id` +
   `retried` `:334-336`) — all correct.
7. **`function-to-go-council.toml` payload at `:252`, `parameter_set` at
   `:255`** — correct.
8. **`ontology_mapping_utils.py`: `council_plan` at `:444`, the two
   clearinghouse reads at `:600-606`, `_clearinghouse()` at `:895`** — correct.
9. **`berdl/transports.py` compat kwargs around `:143-152`** — correct; found
   in `spark_session` at `:146`.
10. **`result_type` is `go_molecular_function`, not `go_council_mapping`.**
    The prompt refers to "the 19,268 rows already loaded as
    `result_type='go_council_mapping'`". The shipped preset declares
    `result_type = "@const(go_molecular_function)"`, and the string
    `go_council_mapping` appears **nowhere** in the repo. The comment I added
    is written in terms of the shipped value ("already loaded under this
    result_type"), so it is correct whichever label the operator remembers.
11. **Snapshot-wide live calls are 88,465, not 82,270.** The prompt's figure
    is correct but scoped: live calls of the *done* `go_mf@4` jobs is exactly
    82,270. Both numbers are now in `council_schema.py`'s docstring.
12. **`calls.reply_parsed` is never SQL NULL** — `sa.JSON` with
    `none_as_null=False` stores Python `None` as the text `'null'`. The
    precondition answer above reports the three-way split rather than a
    misleading `IS NOT NULL` count.
13. **There is no existing `snapshot` or `export` entry point on the council
    store.** `CouncilConfig.snapshot()` is config redaction, and
    `jobs results --out` is an interactive read, not an archive export. t2
    therefore *creates* `SqliteStore.snapshot()` and `SqliteStore.export()`
    rather than gating existing ones. Gating `results()` would have been
    wrong: it is an interactive read every existing test and CLI verb uses,
    and a fresh store legitimately has no identity.
14. **Known machine-local failures: six, not four.** All six are in
    `test_types_config.py::TestPackagedDefaultConfig`, so the class the
    prompt identified is right and only the count was off. Node ids listed
    under t2. The five `tests/core/test_composition_smoke.py` worktree
    artifacts the prompt warned about did not appear in any targeted
    selection, so nothing was chased.

Nothing in the prompt was unbuildable, and no stop-and-report clause fired.

# Deviations from the item boundaries, and why

- **t3 edits `ontology_mapping_utils.py`**, which is nominally t4's module.
  It is inside the tranche's permitted file set. It was necessary: `council`
  cannot carry a real `store_id` unless `council_export` resolves the store's
  declared identity and passes it to `export_rows`, and the alternative —
  publishing `store_id = ""` — is the fabrication criterion 3 exists to
  prevent. The change is 12 lines plus a resolver helper.
- **t3 also updates three existing test fixtures** so they declare a store
  identity (`test_verdict_export.py`'s `_build`,
  `test_ontomap_council_export.py`'s `_FakeCouncil`, and the pipeline
  rehearsal's real `CouncilUtils` store via the real
  `assign_store_identity`). The exporter now requires one exactly as
  production does; a rehearsal that did not declare one would be rehearsing
  something production cannot do. No assertion was weakened and no test was
  skipped — the node-id sets are identical before and after.
- **t4 keeps the original single-command `council_plan` path** as the default
  (`bundle=None`) rather than removing it. The bundle path is what the
  scheduled unattended stage must use and the docstrings and CLI help say so
  plainly; the direct path remains for local use with a live credential and a
  human watching, and it is what the bundle path's PLAN.json equivalence is
  tested *against*. Removing it would have deleted that comparison and broken
  the rehearsal for no gain in the property the task actually asks for.
- **`store_identity` lives outside `store/schema.py`** — a design decision
  argued in t2 above, not a workaround for the file-set restriction.

# Files changed (whole tranche)

```
 src/kbutillib/domains/ai/council/config.py                          |  24 +
 src/kbutillib/domains/ai/council/store/sqlite.py                    | 747 +
 src/kbutillib/domains/genome/ontology_mapping/ontology_mapping_utils.py | 504 +
 src/kbutillib/domains/genome/ontology_mapping/verdicts.py           | 160 +
 src/kbutillib/domains/kbase/berdl/backfills/function-to-go-council-example.parquet | Bin
 src/kbutillib/domains/kbase/berdl/backfills/function-to-go-council.toml |  41 +-
 src/kbutillib/domains/kbase/berdl/council_schema.py                 | 882 +
 src/kbutillib/interfaces/cli/ontomap.py                             | 157 +-
 tests/berdl/test_council_schema.py                                  | 822 +
 tests/cli/test_ontomap_cli.py                                       |   9 +
 tests/domains/ai/council/test_store_identity.py                     | 565 +
 tests/domains/test_ontomap_council_export.py                        |  49 +-
 tests/domains/test_ontomap_pipeline_rehearsal.py                    |  18 +
 tests/domains/test_ontomap_unrun_bundle.py                          | 822 +
 tests/domains/test_verdict_council_reference.py                     | 614 +
 tests/domains/test_verdict_export.py                                |   8 +
 16 files changed, 5397 insertions(+), 25 deletions(-)
```

Every file is one of the four items' modules, their tests, or t4's CLI
registration. `store/schema.py`, `store/__init__.py`, `store/queries.py`,
`council_utils.py`, `council_publish.py`, `unrun.py` and `transports.py` were
all read and **none** was modified.

# Inferred rather than verified

- Nothing in `council_schema.py` has been exercised against real
  Iceberg-on-Polaris. The SQLite sink proves the DDL parses and materialises
  the declared columns; it does not prove `data_lakehouse_ingest` accepts
  every declared type, that the partition specs land as written, or that a
  write succeeds. The `BIGINT`/`BOOLEAN`/`TIMESTAMP` declarations in
  particular are inferred from `clearinghouse_schema`'s surviving types
  (`STRING`, `INT`, `BIGINT`, `TIMESTAMP`, `BOOLEAN`) and from the retired
  `BINARY` lesson, not measured on-pod.
- The partition-count arithmetic in the module docstring (43M calls at 10M
  functions) is the prompt's figure carried forward from the measured
  82,270 calls / 19,276 items ratio, not independently re-derived.
- `CouncilConfig.store_id` is declared and validated, and the store honours a
  configured id passed to `open_store(..., store_id=...)`. **The wiring that
  passes `config.store_id` into `open_store` at the production call site is
  not done here** — that site is `CouncilUtils.store()` in
  `council_utils.py`, outside this tranche's file set. Until it is wired, a
  configured/stored mismatch is caught only when a caller passes `store_id=`
  explicitly. This is the one loose end of t2 and it is a one-line change in
  a module a later phase owns.
- `tests/core/test_composition_smoke.py` was not run, so the prompt's claim
  that its five setup errors are a `.maestro` worktree artifact is neither
  confirmed nor contradicted here.
