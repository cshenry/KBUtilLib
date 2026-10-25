# Work record: council-lakehouse-t2-93-t5-t6-t8 — PRD council-record-lakehouse-v1, tranche 2

## task_id
`council-lakehouse-t2-93-t5-t6-t8`

## prd
`council-record-lakehouse-v1` (status ready, Chris-signed), tranche 2.
Tranche 1 (t1–t4) is on KBUtilLib `main`: `5d579e3` t1, `5e4724e` t2,
`3b8d9e7` t3, `d9727de` t4, `b518979` work record.

## branch
`maestro/developer/serial-tranche-four-items-one-br-council-lakehouse-t2-93-t5-t6-t8`

## base
`a789bba` ("chore: work-record for build-attributor") — the worktree's HEAD.
No rebase was needed. The base is AHEAD of the `d2eb2cc` the task prompt's
corrections were measured against, by two commits of the concurrent
`kbdl-job-arc-indexer-v1` build. None of its named files were touched (see
"Files deliberately not touched").

## completion status

**All four items are COMPLETE.** Nothing was left part-done.

| # | item | commit | state |
|---|---|---|---|
| 1 | KBUtilLib#93 | `5be03ad` | complete |
| 2 | t5-job-disposition | `ac05af7` | complete |
| 3 | t6-generic-archiver | `09393d4` | complete |
| 4 | t8-publication-bundle | `9d01cfd` | complete |

Two bugfix commits sit between them, both for defects the later items' tests
found in the earlier items' code:

| commit | what |
|---|---|
| `307860d` | `require_complete_delivery` raised `KeyError` instead of refusing when a snapshot had no delivery binding at all (found by t8) |
| `cc0ad1f` | `BerdlCouncilSink.bootstrap` created `council_schema`'s nine and not `publication_bundle`, so a table would have existed locally and been missing in the pod (found while reviewing t8's write path) |

Two housekeeping commits follow, neither behaviour-changing:

| commit | what |
|---|---|
| `dea0093` | removed a dead conjunct from the archiver's byte batcher (`len(batch) >= 1` is implied by `batch` being non-empty); the invariant it documented moved into the docstring and a comment where it is load-bearing |
| `5261473` | dropped an unused `ARCHIVED_TABLES` import and made `ContradictoryQueryNameError` a deliberate re-export (`archive_snapshot` documents raising it) |
| `2ad37e3` | documented, at `SqliteStore._row_counts`, why it must not be used to describe a snapshot -- `09393d4` left it with no caller in `src` and looking like the obvious way to build a manifest, which is how that bug would come back |

Everything that needs the kbhub pod — the live `kbaseincubator.council`
namespace, any `BerdlCapability.load()` call, the `BerdlCouncilSink` write
path — is **awaiting operator acceptance (Albert, kbhub)**. Nothing in this
tranche has run against a live pod and no test here is an acceptance of one.
KBUtilLib#91 (the removed `delta_lake`/`use_hive`/`tenant_name`/`connector`
kwargs) is unchanged and was not touched.

---

## Divergences reconciled: what the prompt claimed vs what the tree held

Listed so the taskplan can be corrected at source.

1. **`SqliteCouncilStore` does not exist.** The corrections name
   `SqliteCouncilStore.snapshot()` and `SqliteCouncilStore.find_reusable`.
   The class is **`SqliteStore`** (`store/sqlite.py:519`). Used the real
   name throughout. Nothing else about those two claims was wrong:
   `snapshot()` is at `store/sqlite.py:2216` as stated and
   `find_reusable` at `:1741`.

2. **#93 had a SECOND half the prompt does not mention, and the prompt's fix
   alone would have changed nothing.** The prompt describes one defect:
   `CouncilUtils.store()` passing no `store_id` to `open_store`. That is
   real and is fixed. But `store_id` was **absent from
   `config._TOP_KEYS`** (`council/config.py:155`) and was **never passed to
   `CouncilConfig(...)`** in `load_council_config` — and an unknown
   top-level key is dropped with nothing but a `logger.warning`. So
   `CouncilConfig.store_id` was unreachable from any config file or mapping;
   the only way to set it was `CouncilConfig(store_id=…)` in Python, which
   is what the existing identity tests do and what production does not.
   Found by the first run of the new no-injection test, which failed with
   `ignoring unknown top-level key 'store_id'`. Both halves are fixed in
   `5be03ad`.

3. **`store/schema.py` line numbers drifted by ~2.** Correction (b) gives
   `SCHEMA_VERSION=2` at `:54` (correct), `items` at `:194` (tree: `:194`,
   correct), `calls` at `:216` (correct), `ux_calls_one_live` at `:289`
   (tree: `:290`), `endpoint_state` at `:328` (tree: `:327`). Everything was
   found by symbol; no behaviour depended on this.

4. **`test_store_identity.py:503` does NOT cover the open-write-transaction
   case.** Correction (a) asked me to check first. `:503`
   (`test_the_snapshot_is_a_real_openable_store_not_a_mid_write_copy`)
   asserts the snapshot opens and carries committed data, but takes it with
   **no transaction open**. The genuinely-missing test was added (see t6
   below) and it found a real defect.

5. **t6's "all nine council tables" contradicts t6's own "it writes no
   dispositions".** Both are in the same prompt. Resolved in favour of the
   more specific instruction: the archiver writes **eight** tables
   (`ARCHIVER_TABLES`) and `job_disposition` is named in
   `NOT_WRITTEN_BY_THE_ARCHIVER`, whose rows come from t5. The DDL for all
   nine still belongs to `council_schema` and is created by its `bootstrap`.
   Stated in the archiver's docstring, in its reconciliation report, and in
   the delivery binding's `not_written_here` field.

6. **t8's `job_disposition` DDL is snapshot-scoped, which forces t5 into two
   records.** `council_schema.table_primary_key(JOB_DISPOSITION_TABLE)` is
   `(store_id, snapshot_id, job_id)` — one disposition per job per snapshot.
   t5 also requires that a disposition CHANGE "not lose who said what". One
   table cannot be both unique-by-job and append-only-by-declaration, so t5
   keeps an append-only JSONL **declaration ledger** (the provenance record)
   and writes its **current projection** into the table. Columns were not
   redeclared and the key was not widened.

7. **t8's line citations for `ontology_mapping_utils` were already corrected
   in the prompt and the corrections are right.** `council_export` is at
   `:979`, `job_ids` `:984`, `retry_job_ids` `:985`, `_run_job_ids` `:1520`.
   The abstain loop is `verdicts.py:315` inside `export_rows` at `:194`.
   `council_publish()` `:277`, `_read_plan` `:421`, `_check_inventory`
   `:559`. All verified. (The item text's own `:798`/`:812`/`:827`/`:272`
   are stale, as the corrections say.)

8. **t5's seed file IS reachable from this worktree.** Correction (i)
   allowed for it not being. `~/Dropbox/Jobs/function-to-go/AUTHORITATIVE-JOB-PROVENANCE.json`
   was read (7308 bytes, written 2026-10-23, 21 jobs, 12 in `LOAD_THESE`)
   and copied verbatim into `tests/berdl/fixtures/`. **No named gap.** No
   job id is transcribed into code.

9. **Correction (h) is right and it mattered twice.** `calls.reply_parsed`
   is `sa.JSON` with `none_as_null=False`, so Python `None` is stored as the
   TEXT `'null'`. The archiver therefore reads the snapshot with raw
   `sqlite3`, not through `sa.JSON`, so a JSON `'null'` and a real SQL NULL
   stay distinguishable; and `role_brief_rows` tests the PARSED spec value
   for `None` rather than the column for SQL NULL. Both are pinned by tests.
   `go_mf` version `4` was present in the fixtures I built, as stated.

10. **No `ruff` on this machine.** `/home/chenry/venvs/kbdl/bin/python -m
    ruff` → `No module named ruff`; no `ruff` on `PATH`. **No linter was
    run.** Style was matched by hand against the surrounding modules.

---

## Item 1 of 4 — KBUtilLib#93: the configured `store_id` reaches the real path

Commit `5be03ad`.

### What was built

`src/kbutillib/domains/ai/council_utils.py:209` — `CouncilUtils.store()`
now passes the configured archive identity to `open_store`:

```python
options = dict(kwargs)
if "store_id" not in options:
    options["store_id"] = getattr(self.config(), "store_id", None)
store = open_store(self.store_url(), **options)
if kwargs:            # the ORIGINAL kwargs, unmodified
    return store
```

The id goes in as its **own argument alongside `**kwargs`**, and the
one-off-open decision reads the caller's *original* `kwargs`. That is the
trap the prompt names: `kwargs["store_id"] = …` would make `kwargs` always
truthy, so the store would never be cached (a fresh `open_store` per
`.store()` call) and `_load_query_dirs` would never be reached (every spec
file in a configured `query_dirs` unregistered). Neither raises. An explicit
`store_id=` from a caller wins and is a one-off like any other kwarg-bearing
call. `db_migrate`'s `self.store(check_version=False)` goes through the same
path, so the reopen carries the id too.

`src/kbutillib/domains/ai/council/config.py:155` and `:1572` — the second
half (divergence 2): `store_id` added to `_TOP_KEYS`, and passed to
`CouncilConfig(...)` with a blank-to-`None` normalisation matching `user`'s.
The id's *shape* is still validated by the store at open time, which is the
only place it can also be checked against the record inside the database.

### Tests

`tests/domains/ai/council/test_council_utils_store_identity.py` — 18 tests.
Every one drives the facade; **none** constructs `SqliteStore(store_id=…)`,
because doing that is exactly what made the gap invisible.

- NO-INJECTION: `test_snapshot_refuses_naming_both_values` and
  `test_export_refuses_naming_both_values` — real `CouncilUtils`, **no
  explicit `store_id` kwarg from the test**, a real sqlite store whose
  `store_identity` row disagrees with `config.store_id`,
  `StoreIdentityMismatch` naming **both** values, and no snapshot file left
  behind.
- CACHING SURVIVES: `u.store() is u.store()` (with and without a configured
  id), and `u.store(check_version=False) is not u.store()`.
- QUERY DIRS STILL LOAD: a spec written into a configured `query_dirs` is
  registered on the **cached** store, which is only reached when `kwargs` is
  falsy.
- ABSENCE UNCHANGED: `store_id=None` against an identity-less store still
  raises `StoreIdentityMissing`; and configuring an id does **not** assign
  one (`store_identity()` stays `None` before and after the refusal).
- The config-loader half: a mapping and a YAML file both keep `store_id`,
  the loader no longer warns about it, a blank value means "do not compare",
  there is still no default, and `config_snapshot()` carries it.

### Commands and results

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src \
  /home/chenry/venvs/kbdl/bin/python -m pytest \
  tests/domains/ai/council/test_council_utils_store_identity.py \
  -q -p no:cacheprovider --no-header
→ 18 passed                                            exit 0
```

Regression, same selection on base and branch, compared **by node id**:

```
tests/domains/ai/council/ + tests/cli/test_council_cli.py
  base   a789bba : 6 failed, 1291 passed   (137.29s)
  branch 5be03ad : 6 failed, 1309 passed   (139.65s)
  diff of sorted FAILED node-id lists: EMPTY (identical set)
```

The six are the documented machine-local `test_types_config.py` set (the
h100 user config leaking past a monkeypatched `HOME`; see
`build-baselines/kbutillib-h100-main-6310d61.json`, whose
`CORRECTION_2026-10-25` records the count as six). Left alone. The +18
passed are this item's new tests.

---

## Item 2 of 4 — t5-job-disposition

Commit `ac05af7`.

### What was built

**`src/kbutillib/domains/kbase/berdl/council_sink.py` (new, 625 lines)** —
the row-level read/write seam over `kbaseincubator.council`, four methods
wide (`bootstrap`, `append_rows`, `select_rows`, `count_rows`). Shared by
t5, t6 and t8, which is why it exists rather than three copies.

- `SqliteCouncilSink` materialises the nine tables in one local file from
  `council_schema`'s **own** `schema_sql`. Not a test double: it is how an
  archive is staged and verified off-pod on h100, which has no Spark.
- `BerdlCouncilSink` is the pod sink — `load(dataframes=…)` with
  `mode='append'`, never `'overwrite'`, and reads through `query()`. Wrapped,
  not subclassed, the same posture `ClearinghouseCapability` takes, so the
  raw transport surface is not exposed.
- Filters bind values as **parameters** and never format them into query
  text (clearinghouse_capability Rule 4: `STRING` equality is case-sensitive,
  so a mis-encoded bind matches zero rows, reports "empty", and fails OPEN
  into redoing paid work).
- No primary key is declared, matching the namespace: `schema_sql` emits no
  constraints and Iceberg tables written through `data_lakehouse_ingest`
  declare none either. A stricter sink would pass tests the real target
  would fail.

**`src/kbutillib/domains/kbase/berdl/council_disposition.py` (new, 890
lines)**:

- `validate_disposition` — the enum at the write boundary (the DDL path
  emits no constraints at all), rejecting a sixth value and case near-misses.
- `read_provenance` — **derives** every disposition from the file's own
  structure: its `LOAD_THESE` list and each job's prose, **cross-checked
  against each other and refused on disagreement**. An excluding prose is
  classified by **first marker occurrence**, because the one real entry that
  names two (`"cancelled monolithic attempt … but superseded by the chunk
  that re-ran those items"`) leads with its reason. A prose that classifies
  to nothing raises `ProvenanceFormatError` rather than guessing.
- `DispositionLedger` + `declare` + `seed_from_provenance` — the append-only,
  fsynced JSONL provenance record, shaped after
  `clearinghouse_ingest.ExecutionLedger`. Re-seeding the same file appends
  nothing and restamps nothing (**byte-identical**); a change appends with
  the new `declared_by`/`declared_at` and records what it `supersedes`;
  the superseded line stays. `declared_by` is required, never defaulted.
- `disposition_rows` / `write_dispositions` — the current projection into
  `council.job_disposition`, one row per job per snapshot, refusing a second
  write for the same `(store_id, snapshot_id)`.
- `published_jobs` / `dispositions` / `require_published` — the resolver and
  the gate. **ABSENCE IS NOT CONSENT.** The resolver reads the **current**
  snapshot (highest `snapshot_seq`), not a union, so a job published in
  snapshot 1 and re-declared in snapshot 2 stops resolving. `query_name`
  filtering joins the archived `jobs` table for the same snapshot and
  returns **nothing** when that table is absent — `job_disposition` has no
  `query_name` column and inventing one would be a third copy of the only
  council-kind discriminator in the store.
- `require_published` distinguishes **"NO disposition row"** from
  **"declared `cancelled`"**: they need different fixes and the first is the
  dangerous default this layer removes.

**CLI**: `kbu council disposition {seed, declare, list, publish, check}`
in `src/kbutillib/interfaces/cli/council.py`. `check` is the gate — non-zero,
naming every job that is not `published`.

**Fixture**: `tests/berdl/fixtures/AUTHORITATIVE-JOB-PROVENANCE.json`, a
verbatim copy of the Dropbox file, with a `README.md` saying why it is copied
rather than read at test time.

Documented explicitly as **necessary but not sufficient**: the publisher also
requires bundle listing (closed in item 4 by `require_publishable`).

### Tests

`tests/berdl/test_council_disposition.py` (56) and
`tests/berdl/test_council_sink.py` (34 at final HEAD; 28 when this item was
committed, +6 added with the `cc0ad1f` fix). Every disposition test seeds from
the checked-in fixture through the real `seed_from_provenance`, writes real
rows into a real SQL sink built from the real DDL, and resolves through the
real `published_jobs` / `require_published`.

The gate, both sides:
- `published` resolves (12 of 21); the SAME job re-declared `pilot` through
  the real `declare` drops out (11);
- the cancelled 19,276-item look-alike never resolves;
- an undeclared job does not resolve, and **through the real CLI** exits
  non-zero naming the job id and saying "NO disposition row";
- an empty table resolves NOTHING, not everything;
- re-seeding is a byte-identical no-op, including by a different person;
- an explicit change records the new author and keeps the superseded line;
- the enum rejects a sixth value, in Python and through the CLI.

NO-INJECTION: `TestTheGateThroughTheRealCli` seeds the fixture and publishes
the rows **through `kbu council disposition seed` / `publish`**, then asks
`kbu council disposition check`. No list of published ids is handed to
anything and the resolver is not monkeypatched.

### Commands and results

```
pytest tests/berdl/test_council_disposition.py → 56 passed            exit 0
pytest tests/berdl/test_council_sink.py        → 28 passed            exit 0
  (34 passed at final HEAD, after cc0ad1f added six)

tests/berdl/ + tests/cli/test_council_cli.py
  base   a789bba : 1286 passed, 6 skipped, 0 failed  (17.96s)
  branch ac05af7 : 1370 passed, 6 skipped, 0 failed  (21.02s)
  → +84 new tests, no failures on either side
```

---

## Item 3 of 4 — t6-generic-archiver

Commit `09393d4`.

### What was built

**`src/kbutillib/domains/kbase/berdl/council_archive.py` (new, 1462
lines)**. Knows nothing about GO, functions or the clearinghouse.

Snapshot-versioned archival, keyed `(store_id, snapshot_id, source-PK)` with
`snapshot_id` the verified uncompressed digest. `current_projection()`
resolves by **explicit `snapshot_seq`**, never `loaded_at` (replaying an
older snapshot after a newer one is legitimate). A conflicting seq (same
sequence, different digest) or a conflicting digest (same bytes at a second
point in the store's history) is refused by `SnapshotConflictError`. The
conflict probe reads the four SMALL archived tables, cheapest first, and
never scans `calls`.

The load ledger (`ArchiveLedger`, `build_inventory`, `reconcile_delivery`,
`require_complete_delivery`, `receipt`), shaped after
`clearinghouse_ingest`'s: a delivery is bound to the digest, the namespace
and `ARCHIVE_SCHEMA_VERSION`, with **deterministic** batch ids carrying
expected row counts and digests. On resume a `committed` batch is skipped; a
batch left `started` is **resolved from its batch identity** against the
table (every archived row carries its batch's `load_batch_id`) *before*
being retried; a **partially** landed batch raises `AmbiguousBatchError`
rather than duplicating, because there is no rollback. A delivery is complete
only when **every** table reconciles, and only a complete delivery is
publishable. Receipts are re-exported from that record, not recomputed.

`ARCHIVER_TABLES` puts **`endpoint_state` first**, with the reason at the
declaration: it is the only per-gateway token record that exists anywhere, so
a delivery that dies early still carries the one table that cannot be
reconstructed. `role_brief` is last, after `query_types.spec` is archived in
full. `job_disposition` is in `NOT_WRITTEN_BY_THE_ARCHIVER` (divergence 5).

Faithfulness: every job archived including cancelled/pilot/trial;
`calls.live` and `jobs.deleted_at` carried and never acted on; endpoint_state
documented at its write site as point-in-time and **never additive**; exactly
`council_schema`'s columns including both `query_name` denormalisations, with
a contradictory non-NULL name **rejected**; `role_brief` derived from
`query_types.spec` with an unreadable shape recorded as `<unextractable>`;
JSON columns read as **raw text** (divergence 9).

Batches sized **by bytes** (`MAX_BATCH_BYTES` = half of Spark Connect's
1 MiB `localRelationCacheThreshold`, which fails deterministically with "Not
enough bytes in channel"), because `calls` rows carry `reply_text` and vary
by orders of magnitude. `prepare_snapshot` decompresses `.xz` to a
**declared** scratch path, verifies the uncompressed digest against the
manifest, and **removes its partial output on any failure** — a
half-decompressed SQLite file opens fine and is wrong.

`reconciliation_report` gives rows read/written per table, the `calls`
live/not-live split, items by status, and **distinct-entity** counts rather
than row sums, with retry rounds identified by `jobs.parent_job_id`.

No docstring promises rollback; `ROLLBACK IS NOT IMPLEMENTED` is stated and
pinned by a test over the module source.

**CLI**: `kbu council snapshot` — the verb that was missing. The store method
was **not** re-implemented and no second snapshot method was added; the verb
calls `SqliteStore.snapshot()` and offers **no copy-based path**.

**A defect found and fixed in the shipped `snapshot()`.**
`store/sqlite.py:2299` built its manifest's `row_counts` from
`self._row_counts()`, which reads through the store's own connection — and
`transaction()` is re-entrant, so it **joins a write transaction the caller
already has open** and counts uncommitted rows. Measured by the new test:
under an open transaction the manifest said 2 workers while the snapshot file
contained 1. Counts are now read from the snapshot **file**
(`_snapshot_row_counts`). A manifest whose counts describe a state the
snapshot does not contain is the same class of silent wrongness as the torn
file copy the backup API exists to prevent, and worse — an archiver
reconciling against it would conclude the snapshot was short.

`council_sink.register_table()` was added so item 4 can declare its own table
in the same namespace without making `council_schema`'s nine mean two things.

### Which snapshot tests already existed (cited, not duplicated)

Per correction (a), these were checked first and are **not** re-written:

- `tests/domains/ai/council/test_store_identity.py::TestFreshStoreHasNoIdentity::test_snapshot_refuses_and_names_the_absence`
  (`:104`) — snapshot refuses without a stored identity. **Covered.**
- `…::TestFreshStoreHasNoIdentity::test_the_refusal_exits_non_zero_in_a_real_subprocess`
  (`:121`) — that refusal is a non-zero exit.
- `…::TestConfiguredVersusStoredMismatch::test_snapshot_refuses_naming_both_values`
  (`:365`) and `…::test_a_mismatch_is_never_auto_healed` (`:391`).
- `…::TestACopiedStoreFileCarriesTheIdentity::test_a_read_only_snapshot_carries_the_identity`
  (`:289`).
- `…::TestSnapshotAndExportOutput::test_the_snapshot_is_a_real_openable_store_not_a_mid_write_copy`
  (`:503`) and `…::test_snapshot_refuses_to_overwrite_by_default` (`:518`).

**What I added, because nothing covered it:** `TestSnapshotUnderAnOpenWriteTransaction`
— a snapshot taken while a **write transaction is open**, asserting the file
opens as a real store, its row counts match **committed** state, it does not
carry the uncommitted row, the committed row IS there afterwards, and the
snapshot archives and reconciles. This is the test that found the manifest
defect above. Plus `TestTheSnapshotCli` for the new verb (its *refusals* are
the method's and are cited above, not duplicated).

### Tests

`tests/berdl/test_council_archive.py` — 70 tests.

NO-INJECTION: every archive test runs against a REAL SQLite council store
built through `SqliteStore` + `init_schema()` and written through the store's
own `create_job` / `add_calls` / `record_attempt` / `add_tokens` /
`register_worker` / `settle_item` / `cancel_job`, with supersession applied
through the engine's real `status='superseded', live=False` update;
snapshotted with the **existing** `SqliteStore.snapshot()`; archived from
that manifest. The crash-recovery tests interrupt the **real** archiver with
a table writer that raises on `attempts` — no ledger is written by hand.

Covered, each as its own test: every archived column present; every row count
equal to the **store's** count (not live-only, and the fixture really has
non-live calls); `workers.version` NULL preserved; `live` readable per row
and both the superseded call and its replacement kept; the reused call's full
`reply_text` with zeroed `fresh_usage` and a `reused` attempt; `deleted_at`
carried and the tombstoned job still archived; the dropped columns absent
(the five lease columns, `priority`, `item_order`, `halt_*`, the five worker
process columns); `council_entity_hash` present with no bare `entity_hash` on
`items`; a JSON `'null'` distinguishable from a SQL NULL; both `query_name`
denormalisations with the legacy-NULL fallback and a contradictory non-NULL
name **rejected**; `role_brief` mission text, the `<unextractable>` shapes
(no `roles`, a spec that is JSON `'null'`, a role with no `system`); identity
from the **manifest** and not from the path (including a deliberately
misleading `…/laptop/someone-elses-council/copy.sqlite`).

Snapshot versioning: archive s1, **mutate** the store (a new call under an
old job, a changed job status, advanced endpoint_state counters), archive s2
— both independently queryable, no row overwritten, the new call present only
in s2, the current projection resolving by `snapshot_seq`, and
`endpoint_state` **not additive** (1200 → 2000 across two snapshots; the
plausible-and-wrong sum of 3200 is asserted explicitly so the test says what
it guards). A conflicting seq and a conflicting digest are both refused, and
the refusal exits non-zero **in a real subprocess**.

Crash recovery: `calls` committed and `attempts` did not; the delivery
reports INCOMPLETE naming `attempts` **while `calls` and `jobs` reconcile**
(which is why checking one table is not a completeness test); it is not
publishable; the failed batch is recorded `failed`; it **resumes without
duplicating** the `calls` rows (`written == 0`, `skipped == calls_before`);
the resumed delivery is publishable. Plus the ambiguous append both ways: a
`started` batch whose rows all landed is **resolved, not retried**, and a
**partially** landed one raises.

Report: distinct-entity count (3) differs from the item row sum (5) with a
retry job present, and `retry_jobs` is found via `parent_job_id`.

### Commands and results

```
pytest tests/berdl/test_council_archive.py → 70 passed                exit 0

tests/berdl/ + tests/cli/test_council_cli.py
  + tests/domains/ai/council/test_store_identity.py
  + tests/domains/ai/council/test_store_contract.py
  branch : 1688 passed, 6 skipped, 0 failed  (43.52s)

tests/domains/ai/council/   (the snapshot() change touches a widely-used
                             method, so the whole council suite was re-run)
  branch 09393d4 : 6 failed, 1266 passed  (143.87s)
  → the SAME six machine-local test_types_config.py node ids. No regressions.
```

---

## Item 4 of 4 — t8-publication-bundle

Committed with this record.

### What was built

**`src/kbutillib/domains/genome/ontology_mapping/council_bundle.py` (new,
856 lines)**, beside `verdicts.py` and `council_publish.py`.

The module docstring leads with the three reasons `council.*` alone is not
enough, and every field exists because of one of them:

1. **one item fans out to several `query_id`s** → the bundle carries the full
   `query_id → item_id` map (`fanout_map`), because the clearinghouse key set
   is not derivable from `council.items`;
2. **retrieval abstentions have no council item** (`verdicts.py:315`) → the
   abstentions are carried **with their decline reasons and `item_id: None`**
   (key present and null, not absent), plus a submitted inventory, because
   the submitted set is not derivable from the council projection either;
3. **later verdicts win by declared order** → the base and retry job ids are
   carried **in the order that resolves winners**, composed the way
   `council_export` composes them (retries LAST) and preserved **exactly as
   declared** — not sorted, not de-duplicated.

Also carried: `PLAN.json` content whole, the canonical `parameter_set`
**including `mapping_hash`**, `parameter_set_hash`, `plan_hash`, and a
`bundle_digest` over the canonicalised whole. `canonical_json` uses
`sort_keys=True`, which sorts KEYS and never list elements — which is what
makes the declared winner order survive canonicalisation, and is commented
as such.

Bound to `store_id` and a **completed** `snapshot_id`. `campaign_id` is
**declared, never derived**: a parameter set does not separate independent
campaigns, so a derived id would silently merge them.

Immutability: a second write for a campaign with **different** content is a
`BundleConflictError`, on disk and in the lakehouse; an identical rewrite is
a no-op. The reason given is the clearinghouse slot key's exclusion of
`result_type_version`, which freezes a payload shape on first write.

Persisted **both** on disk (`write_bundle`, atomic, canonical bytes, fsynced)
and in the lakehouse (`publish_bundle`). Where it lands:
`PUBLICATION_BUNDLE_TABLE = "publication_bundle"` in the council namespace,
registered via `council_sink.register_table` at import — deliberately **not**
a tenth entry in `council_schema`, which declares the *archive* of a council
store. `publish_bundle` calls `sink.bootstrap()` itself, so there is no
import-order hazard.

**No second completeness check** (correction (g)): the snapshot gate
delegates to `council_archive.require_complete_delivery`, which reads t6's
load ledger, and re-raises its message as
`BundleIncompleteSnapshotError`. The bundle module never counts archived rows
itself, and a test asserts `count_rows` does not appear in its source.
`require_complete=False` exists for a snapshot archived on another machine
and is a **named, tested exception** — never a default.

**Both gates in one place**: `require_publishable` = `require_bundle_listed`
**and** `council_disposition.require_published`. Neither implies the other,
and this closes the loop t5 deliberately left open.

### Tests

`tests/domains/test_council_bundle.py` — 52 tests.

NO-INJECTION, both clauses:

- The incomplete-snapshot refusal reads a ledger left by an **interrupted run
  of t6's real archiver** — `archive_snapshot` on a real snapshot of a real
  store, with `attempts` failed by a table writer. The test constructs no
  ledger and no completeness flag. It additionally asserts the SAME bundle
  publishes once the delivery is **resumed**, and that a snapshot with **no
  delivery at all** is refused ("no ledger" is not "complete by default").
- The fanout and abstention inputs are the plan's own record shapes and are
  fed to **both** `build_bundle` and the real `verdicts.export_rows`. The
  key assertion is that the two agree on the submitted set:
  `exported == set(bundle_query_ids(bundle))` and
  `export_rows counts["inputs"] == bundle["submitted"]["counts"]["inputs"]`.
  A separate test shows the exporter publishing **three** rows from the one
  shared item while `council.items` holds one — which is why the map is
  needed at all — and another shows the exporter publishing abstentions with
  `council = None`.

Also covered: the bundle round-trips and its digest verifies; the digest is
order-independent and covers the whole content; an **edited** file fails its
digest on read; a wrong declared digest is refused before anything is
written; the ordered job list is preserved exactly (including a repeat) and
survives a disk round trip; abstentions carry their decline reasons and no
item ids; the on-disk and lakehouse copies **agree** by value and by
canonical bytes; a conflicting rewrite is refused on both copies with both
digests named and the first bundle untouched; a tampered lakehouse row fails
its digest; an unpublished campaign reads as `None`; both gates refuse in
both directions (bundle-listed-but-pilot, bundle-listed-but-undeclared,
declared-published-but-not-bundle-listed); and `build_bundle_from_run` reads
the **same two files the exporter reads**, refusing a missing plan (naming
`council-plan`), a record file that is gone, a plan with no job ids, and a
plan naming no record file.

**A defect found and fixed in item 3 by these tests.**
`council_archive.require_complete_delivery` raised `KeyError: 'endpoint_state'`
when there was **no delivery binding at all** — `reconcile_delivery` returns
`tables={}` with `incomplete=list(ARCHIVER_TABLES)`, and the message builder
indexed `tables` unconditionally. So the commonest incomplete case (a
snapshot nobody archived here) crashed instead of refusing. Fixed, and pinned
by a new test in both files.

### Commands and results

```
pytest tests/domains/test_council_bundle.py → 52 passed               exit 0
pytest tests/domains/test_council_bundle.py tests/berdl/test_council_archive.py
                                            → 122 passed              exit 0
```

Regression, same selection on base and branch, compared by node id:
see "Final regression check" below.

---

## Final regression check

Comparison is by **node id, never by count**, and both runs were taken in the
**same kind of location** (the base in a detached worktree at `a789bba` under
`/scratch/chenry/base-council-tranche2`, the branch in this worktree) — the
`WORKTREE_MEASUREMENT_TRAP` in `build-baselines/kbutillib-h100-main-6310d61.json`
records that a worktree run compared against a main-clone baseline shows five
phantom regressions in `tests/core/test_composition_smoke.py`. That file is
not in any selection below.

**The full suite was NOT run.** It takes 45–57 minutes on h100 (measured:
3416 s and 2865 s in the cached baseline), which is 5–6× the 600 s Bash-tool
ceiling; running it would kill the shell and read as a failure. Targeted
selections were used, covering every module touched.

```
A. tests/domains/ai/council/ + tests/cli/test_council_cli.py
     base   a789bba : 6 failed, 1291 passed   (137.29s)
     branch (item 1) : 6 failed, 1309 passed  (139.65s)
     FAILED node-id diff: EMPTY

B. tests/domains/ai/council/            (after the snapshot() change)
     branch (item 3) : 6 failed, 1266 passed (143.87s)
     same six node ids

C. tests/berdl/ + tests/cli/test_council_cli.py
     base   a789bba : 1286 passed, 6 skipped, 0 failed (17.96s)
     branch (item 2) : 1370 passed, 6 skipped, 0 failed (21.02s)

D. tests/berdl/ + tests/domains/ + tests/cli/test_council_cli.py   (-rf)
     base   a789bba : 7 failed, 3341 passed, 6 skipped  (158.94s)
     branch cc0ad1f : 7 failed, 3571 passed, 6 skipped  (164.50s)
     FAILED node-id diff: EMPTY  -- 7 unique ids on each side, identical
```

`cc0ad1f` is the last commit that changes behaviour. The three commits after
it (`dea0093`, `5261473`, `2ad37e3`) are a dead conjunct, two imports and a
docstring; each was verified against the targeted suites of everything it
touches (`tests/berdl/test_council_archive.py`,
`tests/domains/test_council_bundle.py`,
`tests/domains/ai/council/test_store_identity.py` — 123–224 tests depending
on the commit, all passing) and selection D was **not** re-run for them. Say
so rather than quote a number I did not measure at the final SHA.

Commands, verbatim:

```
# base, in a detached worktree at a789bba
cd /scratch/chenry/base-council-tranche2 && TMPDIR=/scratch/chenry/tmp \
  PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest \
  tests/berdl/ tests/domains/ tests/cli/test_council_cli.py \
  -q -p no:cacheprovider --no-header -rf
→ 7 failed, 3341 passed, 6 skipped                      exit 0

# branch, in this worktree at cc0ad1f
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src \
  /home/chenry/venvs/kbdl/bin/python -m pytest \
  tests/berdl/ tests/domains/ tests/cli/test_council_cli.py \
  -q -p no:cacheprovider --no-header -rf
→ 7 failed, 3571 passed, 6 skipped                      exit 0
```

The +230 passed are this tranche's new tests: 18 + 34 + 56 + 70 + 52.

### The seven pre-existing failures, all machine-local, all identical on base

```
tests/domains/ai/council/test_types_config.py::TestPackagedDefaultConfig::test_cli_endpoints_default_to_one_call_at_a_time
tests/domains/ai/council/test_types_config.py::TestPackagedDefaultConfig::test_codex_seats_are_marked_unverified
tests/domains/ai/council/test_types_config.py::TestPackagedDefaultConfig::test_default_profile_is_mixed_subscription
tests/domains/ai/council/test_types_config.py::TestPackagedDefaultConfig::test_every_packaged_profile_builds_a_roster
tests/domains/ai/council/test_types_config.py::TestPackagedDefaultConfig::test_on_a_fresh_machine_with_no_kbutillib_config
tests/domains/ai/council/test_types_config.py::TestPackagedDefaultConfig::test_the_packaged_config_declares_no_poplar_endpoint
tests/domains/ai/llm/test_static_headers.py::TestPackagedExample::test_the_default_config_declares_no_mag_endpoint
```

The first **six** are the documented machine-local set
(`build-baselines/kbutillib-h100-main-6310d61.json`
`MACHINE_LOCAL_FAILURES_NOT_PRODUCT_DEFECTS`, whose
`CORRECTION_2026-10-25` records the count as six, not four, and proves all
six pass under an empty `HOME`). Left alone, as instructed.

**The seventh is NOT in that baseline entry and is worth recording.**
`test_static_headers.py::TestPackagedExample::test_the_default_config_declares_no_mag_endpoint`
fails on the base commit from the **same cause**: `load_council_config()`
picks up `/home/chenry/.kbutillib/config.yaml` (the run logs
`Loaded configuration from: /home/chenry/.kbutillib/config.yaml`) and the
resolved config reports
`source='packaged default + user ai_council section'` with a `mag` endpoint
present, while `assert "mag" not in cfg.endpoints` expects the packaged
default, where `mag` is commented out. Measured on the base commit in the
detached worktree, not inferred. It is the same non-hermetic-config defect
the baseline already files separately, and it is **not this PRD's to fix** —
but the baseline entry should be extended from six node ids to seven so a
later reviewer does not read it as a phantom regression.

**Zero regressions: the non-passing node-id set is identical on base and
branch, by node id.**

---

## Files deliberately not touched

The concurrent `kbdl-job-arc-indexer-v1` build owns these and none was read
for modification or written:

- `src/kbutillib/kbdl_index/`
- `src/kbutillib/arc_context.py`
- `src/kbutillib/domains/external/kbdl_service_utils.py`
- `src/kbutillib/koros_arc_store/`
- `scripts/kbdl_sweep_launcher.py`
- `tests/kbdl_index/`

`src/kbutillib/domains/kbase/berdl/transports.py` (KBUtilLib#91's
compatibility kwargs at `:146`–`:164`) was **read only** and not modified.

---

## What needs the pod — awaiting operator acceptance (Albert, kbhub)

Nothing below has been run, and no test in this tranche is an acceptance of
any of it:

1. `council_schema.bootstrap()` creating the nine tables (plus
   `publication_bundle`) in a real `kbaseincubator.council` namespace.
2. `BerdlCouncilSink.append_rows` → `BerdlCapability.load(dataframes=…)`
   against a live Spark session, including whether the byte-sized batches
   really stay under `localRelationCacheThreshold` as Arrow payloads (the
   ceiling is respected by construction; the **margin** is an estimate, and
   `MAX_BATCH_BYTES` says so).
3. `BerdlCouncilSink.select_rows` / `count_rows` through `query()` on Trino
   and on Spark, including the `to_dict("records")` vs row-mapping
   normalisation.
4. Whether SQLite's permissive type affinities and Iceberg's types agree for
   `TIMESTAMP` columns written as ISO-8601 strings and `BOOLEAN` `live`.
   **Agreement between the two sinks is asserted nowhere in this repo** and
   may not be claimed.
5. An end-to-end run on the real ~896 MB snapshot: `prepare_snapshot` on the
   real `.xz`, the real 203,821-row `calls` table (`build_inventory`
   materialises each table, which that snapshot fits but a larger one would
   not — a streaming variant is named in its docstring), and the real
   reconciliation numbers against the measured 82,270 live / 19,276
   submitted / 19,268 decided.

Live acceptance remains blocked on KBUtilLib#91 until a recorded kbhub smoke
test reads and appends to a disposable council namespace with the deployed
package version. Albert owns that test.

## Unverified / inferred rather than measured

- The `calls` live/not-live and distinct-entity numbers in the tests are
  **this fixture's**, not the production store's. The production figures
  (203,821 / 82,270 / ~115,000 null-reply_text / 19,276 / 19,268 / 19,405)
  are quoted from the task prompt as the reasons for the design and are
  carried in comments; none was re-measured here.
- `role_brief` extraction was tested against the spec shape the shipped
  `go_mf.yaml` builtin uses (`roles.<role>.system` / `.schema`) and against
  three unextractable shapes. Whether every spec in the production store's
  `query_types` is extractable is **not** established; the unextractable
  path is what covers the rest.
- No linter was run (no `ruff` on this machine — divergence 10).
