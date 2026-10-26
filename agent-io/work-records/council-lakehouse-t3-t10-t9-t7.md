# Work record: council-lakehouse-t3-t10-t9-t7 — PRD council-record-lakehouse-v1, tranche 3

## task_id
`council-lakehouse-t3-t10-t9-t7`

## prd
`council-record-lakehouse-v1`, tranche 3. The task name carries a `t3` that
the prompt's body does not: the prompt declares **three items, in the order
t10 → t9 → t7**, and names no t3 work anywhere. t3 was not implemented and no
t3 success criteria were given to implement against. See "Divergences" below.

## branch
`maestro/developer/serial-tranche-three-items-one-br-council-lakehouse-t3-t10-t9-t7`

## base
`ed40f28` ("fix(berdl,arc-store): one spelling for KBDL object references,
obj://<id>[#<member>]") — exactly the commit the prompt's re-measured
citations were taken at. No rebase was needed.

## completion status

**All three items are COMPLETE.** Nothing was left part-done and nothing was
stashed or discarded.

| # | item | commit | state |
|---|---|---|---|
| 1 | t10-seed-store | `dbb6edd` | complete |
| 2 | t9-clearinghouse-publisher | `591bc92` | complete |
| 3 | t7-delivery-and-scanner | `773eae0` | complete |

Four follow-up commits sit after them. Two are defects my own review of the
landed code found, and two are performance fixes at production scale; none
widens the task's scope:

| commit | what |
|---|---|
| `322de65` | perf: `guard_join` rebuilt `set(submitted)` and `set(malformed)` once per element, which is quadratic over the campaign's 19,276 keys |
| `971dde9` | fix: COUNCIL-DELIVERY v1 carried no `store_host`, so every row archived through the scanner would have said the host was `''`. The field is in the archive's own identity block and the delivery is the only thing in the rail that knows it |
| `639777c` | perf: `select_verdicts` read every gated job's `items` table TWICE -- once to fold the verdicts and again for the `function_text` out of `evidence`. For the measured campaign that is 12 jobs x up to 19,276 rows read a second time for one column |
| `71221da` | fix: the scanner trusted a cached archive receipt on its identity match alone, leaving one state invisible -- a receipt that matches its manifest while the rows are GONE from the namespace. A cached `ok` now re-runs `reconcile_delivery` every tick, and a disagreement is reported rather than papered over by re-appending (the ledger says every batch committed and there is no rollback) |

Everything that needs the kbhub pod — the live `kbaseincubator.council`
namespace, any `BerdlCapability.load()`, `kbu clearinghouse load`/`verify`,
and the registration of the scanner as a restartable service — is **awaiting
operator acceptance (Albert, kbhub)**. Nothing in this tranche ran against a
live pod, no test here is an acceptance of one, and no mocked load is called
an acceptance. KBUtilLib#91 was not touched.

---

## Item 1 — t10-seed-store (`dbb6edd`)

### What was built

`kbu council seed-store --from-lakehouse` plus `SqliteStore.seed_from_rows()`:
rebuild a REUSE-ONLY council store from one store's archived rows.

The work splits across two layers on purpose, because the store layer must not
depend on `kbase.berdl` (SQLAlchemy and the lakehouse stack are different
optional extras, and a seed must be possible from any source that can produce
rows of the archived shape):

| layer | file | what |
|---|---|---|
| store | `src/kbutillib/domains/ai/council/store/sqlite.py` | `seed_from_rows()`, `seed_records()`, the seven rules, the type conversions |
| lakehouse | `src/kbutillib/domains/kbase/berdl/council_seed.py` (NEW) | `read_seed_rows()` — the completeness gate and the four-table read |
| CLI | `src/kbutillib/interfaces/cli/council.py` | the verb |
| errors | `src/kbutillib/domains/ai/council/store/errors.py`, `.../store/__init__.py` | `SeedError`, `SeedMultiOrigin`, `SeedDestinationNotEmpty` |

Four tables are imported: `calls` (the cache), plus `jobs`, `items` and
`query_types` because the imported calls carry foreign keys to them.
`attempts`, `endpoint_state` and `workers` are deliberately NOT imported —
an attempt records what the ORIGINAL run's try cost, the endpoint counters are
mutable cumulative totals that would charge this machine for another machine's
spend, and a worker row describes a process that no longer exists. All three
stay queryable in the lakehouse.

### EXACTLY WHAT THE NON-CLAIMABLE GUARANTEE ENFORCES

This is the statement the spec asks for, and it is deliberately precise about
what is enforced by construction, what is enforced by an existing query, and
what is NOT enforced at all.

**Enforced, layer 1 — there is no row of a claimable STATUS.**
`seed_from_rows` imports only `calls` rows whose `status == 'ok'`.
`queries.select_claimable` (the single source of the claim scan, used by both
`SqliteStore.claim_calls` and `Worker._claim`) filters
`calls.status == 'pending'`. A seeded store therefore contains no row the
claim query can match. Measured in the test suite: after a seed the distinct
set of `calls.status` values in the destination is exactly `{'ok'}`.

**Enforced, layer 2 — every imported job is TOMBSTONED.**
Each imported `jobs` row is written with `deleted_at` set to the seed
timestamp and `status = 'seeded'`. `select_claimable` additionally requires
`jobs.deleted_at IS NULL`, so even a `pending` call attached to a seeded job
AFTER the seed cannot be claimed. That case is tested directly: a pending call
is added to a seeded job through the real `add_calls`, and the real
`claim_calls` still returns `[]`.

Tombstoning is the right EXISTING mechanism rather than a new one, and that is
the load-bearing part: `queries.select_reusable`'s docstring says tombstoned
jobs are deliberately INCLUDED ("deleting a job must not throw away an answer
somebody paid for"), while `select_claimable` deliberately excludes them. Those
two queries disagreeing on purpose is what makes "reusable but never
executable" expressible at all, with no new column and no schema version bump.

**Enforced — progress/listing visibility.** `list_jobs()` excludes tombstoned
jobs by default, so a seeded job does not appear in the job listing or in
anything built on it. Tested.

**NOT enforced, stated plainly:**

- `SqliteStore.export()` is a whole-store dump of every table and does NOT
  filter tombstoned jobs. A `kbu council db export` of a seeded store will
  therefore include the imported rows. That is correct for an export whose
  purpose is "every row in this file", but it means the phrase "excluded from
  run export" in the spec holds for the RUN export (the per-job
  `results(job_id)` path, which is only reached by naming a job) and not for
  the store export.
- `get_job()`, `get_items()`, `get_calls()` and `job_status()` all answer for a
  seeded job when it is named explicitly. Nothing hides a seeded job from a
  caller who asks for it by id; the guarantee is about the QUEUE, not about
  visibility.
- Provenance is recorded in `jobs.options[SEED_PROVENANCE_KEY]` (`kbu_seed`),
  not in a dedicated column or table. Adding either would need a
  `SCHEMA_VERSION` bump, and `ADDITIVE_COLUMNS` is for nullable columns whose
  absence means "nothing to say" — which a provenance marker is not.

### The idempotency / empty-destination interaction

The prompt flags that rule 1 ("EMPTY destination") and rule 7 ("seeding twice
is idempotent") interact. **The decision: a second seed of the SAME
`(store_id, snapshot_id, query_name, query_version)` into a store that holds
exactly that seed and nothing else is a NO-OP** — it returns
`{"imported": False, "idempotent": True}` and writes nothing. A refusal there
would make idempotency untestable, because the only store a second identical
seed can ever run against is one that already holds that seed.

The three other cases are refusals:

- a store holding a DIFFERENT seed → `SeedMultiOrigin`;
- a store holding jobs/items/calls/attempts that are not part of any seed →
  `SeedDestinationNotEmpty`;
- rows offered whose own `store_id`/`snapshot_id` is not the declared one →
  `SeedMultiOrigin`, raised before anything is written.

`query_types` is deliberately excluded from the emptiness test: it is an
immutable registry every initialised store legitimately carries (and a store
opened through `CouncilUtils` is seeded with the builtins and the user's
configured `query_dirs`). A conflicting incoming spec under the same
`(name, version)` raises `QueryTypeExists`, which is `put_query_type`'s own
rule.

### A measured defect this item found and fixed in both layers

The archived tables are declared `STRING`, and **SQLite's type-affinity rules
give a column whose declared type is literally `STRING` NUMERIC affinity** (the
name contains none of `INT`, `CHAR`, `CLOB`, `TEXT`, `BLOB`, `REAL`, `FLOA`,
`DOUB`). So the local staging sink hands a digit-only value back as a NUMBER:
the production run's `query_version` is `"4"` and reads back as the integer
`4`, and `4 != "4"` would have scoped a version-scoped seed to ZERO rows while
reporting success. Both `seed_from_rows` and `read_seed_rows` now coerce
`query_name`/`query_version` to text before comparing (`_seed_text` /
`_as_text`), and `_seed_row` coerces every value destined for a `String`
column. The same affinity also turned an all-digit test hash into a float,
which is how it was caught — real sha256 hex has letters, so production data
does not hit the hash case, but `query_version` hits it every time.

### Tests

`tests/berdl/test_council_seed.py` — **28 tests, all passing.**

The no-injection criterion is met: the round-trip test archives a real store
fixture with the REAL `council_archive.archive_snapshot` into a REAL
`SqliteCouncilSink`, reads it back with the REAL `read_seed_rows`, seeds a
fresh REAL `SqliteStore` through `seed_from_rows`, and calls the REAL
`store.find_reusable(prompt_hash)`. The non-claimable tests call the REAL
`store.claim_calls`. No stub store, no patched `find_reusable`, no patched
claim.

Covered: `find_reusable` returns the expected row for a known `prompt_hash`;
`normalised` round-trips intact; a JSON-null `normalised` stays JSON null (and
the raw column really holds the text `'null'`, not SQL NULL); a `live=0`
status-`ok` call is NOT served as reusable while the archive still holds it; no
imported row is claimable (both layers); a seeded job is invisible to
`list_jobs`; version-scoped seeding excludes another version's rows;
`disposition='pilot'` answers ARE imported; multi-origin refusal (store and
snapshot); incomplete-snapshot refusal through `require_complete_delivery`;
a snapshot nobody archived; non-empty destination; a destination holding a
different seed; idempotency three ways; and the verb through the real CLI with
its exit codes.

The `live`/`status` filtering tests hand `seed_from_rows` the UNFILTERED rows
read straight from the sink, so what they exercise is the store's own
authoritative filter rather than the reader's pre-filter. A separate test
asserts the two filters agree.

### A note on a machine-local hazard this item had to dodge

The CLI test's destination store is opened through `CouncilUtils`, which
registers the builtin query types AND every spec file in the user's configured
`query_dirs`. The first draft of the fixture used `go_mf` and collided with a
machine-local `go_mf@3` spec on this host. The fixture now uses a query name
(`seedtest_mf`) that no shipped builtin and no configured directory can
collide with — the same class of machine-local leak that already accounts for
the six known failures in `tests/domains/ai/council/test_types_config.py`.

---

## Item 2 — t9-clearinghouse-publisher (`591bc92`)

### What was built

`src/kbutillib/domains/kbase/berdl/council_publish.py` (NEW) — the
LAKEHOUSE-SOURCED publisher. Its module docstring's first paragraph says, in
so many words, how it differs from the same-named
`genome/ontology_mapping/council_publish.py`: that one is
RUN-DIRECTORY-SOURCED (it reads the verdict parquet a finished run's export
wrote beside its `PLAN.json`); this one reads the archived deliberation out of
`kbaseincubator.council` plus the campaign's publication bundle and
reconstructs that same parquet from durable data.

Plus `kbu ontomap council-publish-lakehouse` in
`src/kbutillib/interfaces/cli/ontomap.py`, registered beside
`council-publish`.

### The three gates, and what is reuse

| gate | implemented by | notes |
|---|---|---|
| snapshot pinned AND complete | `council_archive.require_complete_delivery` | the ONE completeness test; not re-implemented |
| 1. `query_name == 'go_mf'` | `council_publish.require_query_kind` (NEW) | the only gate this module adds |
| 2. bundle-listed | `council_bundle.require_publishable` | the ONE two-gate test |
| 3. disposition `published` | `council_disposition.require_published`, via gate 2's function | a job with NO disposition row is refused naming it |

Winner order is `council_bundle.ordered_job_ids`, folded with
`verdicts.verdicts_by_item` — the same fold the run-directory exporter uses,
so "a later job supersedes an earlier one for the same `item_id`" means one
thing in both paths. Fanout is `fanout_map`'s data (`bundle["fanout"]`), the
submitted inventory is `bundle_query_ids`, and the abstentions are
`abstentions`.

Rows are built by `verdicts.export_rows`, which builds every `council` cell
through `verdicts.council_reference` and writes `council = null` for every
retrieval abstention — so the lakehouse-sourced export is byte-compatible with
the existing exporter by construction rather than by agreement. The hand-off
is `genome.ontology_mapping.council_publish.council_publish`, i.e. the
existing `render_manifest` + `load_manifest` + `shard_plan` + `shard_manifest`
path and the shipped `function-to-go-council` preset. **No new kbhub-side
loader and no second manifest format.**

`PLAN.json` is written into the publish directory FROM THE BUNDLE's own
carried plan, which is what lets a re-publish need no original run directory.
A publish directory holding a DIFFERENT plan is refused rather than
overwritten.

### THE RECEIPT FIELDS, AND WHETHER THE GUARD RUNS BEFORE THE JOIN

**The guard runs BEFORE the join. Yes, verified, and tested as such.**
`publish_from_lakehouse` calls, in this order: `select_verdicts` (gates) →
`guard_join` → `lakehouse_export_rows` (the join) → `write_verdict_parquet`
→ the existing shard path. `guard_join` is evaluated entirely on the SOURCE
side: the bundle's submitted inventory on one side, and the council-side key
space (`resolvable_query_ids`, which `item_id`s actually carry an `ok`
verdict, plus the abstentions) read independently on the other. It never looks
at the rows a join returned.

It does not re-implement the four assertions: it builds the batch the campaign
WOULD write — one row per submitted `query_id` carrying the REAL slot tuple
`(entity_hash, entity_type, result_type, source, parameter_set_hash)` — and
calls `clearinghouse_ingest.join_assertions` + `raise_for_join_report`. So
"at most one source row per target slot" is asserted against the slot key the
loader will actually use, each of the four reports a COUNT, and the exception
type a caller catches is the same `ClearinghouseJoinAssertionError` the load
side raises. The floor is the EXISTING `DEFAULT_COVERAGE_FLOOR` of 1.0; no new
tolerance policy was invented. Zero resolution is fatal whatever the floor.

Every `query_id` goes through `clearinghouse_schema.encode_entity_hash`, so a
truncated or uppercased key becomes a NULL key and a count rather than a
plausible wrong hash. `council_entity_hash` is never read and never published.

**The receipt carries:**

| field | meaning |
|---|---|
| `query_ids_submitted` | distinct `query_id`s in the bundle's submitted inventory (fanout + abstentions) |
| `query_ids_with_verdict` | distinct `query_id`s that produced a published row |
| `query_ids_unresolved` + `unresolved_query_ids` | the residual count AND the ids (up to 50) — the eight residual items of the measured run are the interesting number |
| `query_ids_resolvable` | the council-side key space the guard measured |
| `reconciled`, `reconciled_on` | `with_verdict + unresolved == submitted`, on **distinct `query_id`** |
| `export_rows`, `result_rows_written` | ROW counts, which are neither items nor `query_id`s |
| `items_submitted`, `inputs_submitted` | the bundle's own item and input counts |
| `abstentions`, `abstentions_without_text` | including the retrieval abstentions |
| `declines_by_reason` | every decline class, including `abstained:<reason>` |
| `parameter_set_hash` | the hash the rows landed under |
| `target_table_snapshot_id` | **`None`**, with `load.why` saying why |
| `load` | `state: "awaiting operator acceptance"` plus the two in-pod commands |
| `guard`, `publish` | the guard report and the existing publish record, whole |
| `frozen_result_type_untouched` | `go_council_mapping`, named so a reader can see it is a DIFFERENT value from what this writes |

`target_table_snapshot_id` is `None` on purpose: the Iceberg commit that
assigns it is made by `kbu clearinghouse load` IN THE POD, this half runs
off-pod, and claiming a value would be inventing one.

### What is not touched

`RESULT_TYPE` is `go_molecular_function` — the shipped preset's own
`@const` — and a test reads the preset TOML and asserts the two agree. The
19,268 pre-existing rows loaded as `result_type='go_council_mapping'` are a
DIFFERENT result type; nothing here writes it and nothing here issues an
Iceberg delete. A test asserts the sharded rows' `result_type` set is exactly
`{go_molecular_function}`.

### A measured consequence worth flagging to the PRD

A retrieval abstention's `function_text` is NOT RECOVERABLE FROM THE LAKEHOUSE.
An abstention has no council item, so no `items.evidence` holds its text, and
the bundle carries only its `query_id` and decline reason. Decided rows get
their text from `items.evidence`; an abstention's is `''` unless the caller
passes `texts={query_id: ...}`. This is reported in the receipt as
`abstentions_without_text` rather than smoothed over, **because the
clearinghouse slot is frozen on first write and an empty `function_text`
published once stays empty.** An operator republishing a campaign's
abstentions from the lakehouse should pass the run's abstain file through
`--texts`-equivalent (the `texts=` argument; no CLI flag for it yet).

Also: at the default coverage floor of 1.0 the measured production campaign
(19,276 submitted, 8 never settled, coverage 0.999585) **REFUSES**, naming the
eight. Lowering it is the operator's explicit act via `--coverage-floor`,
exactly as on the load side. That is the instructed behaviour ("adopt the
EXISTING default... do not invent a new tolerance policy") and it is called out
here because it means the real republish is a two-step with a deliberate flag.

### Tests

`tests/berdl/test_council_publish_lakehouse.py` — **41 tests, all passing.**

No-injection criterion met: every test runs against a REAL `SqliteCouncilSink`
populated by the REAL `archive_snapshot`, with real disposition rows written
through `council_disposition.declare` + `write_dispositions` and a real bundle
built and published through `council_bundle.build_bundle` +
`publish_bundle`. `require_publishable`, `require_published` and
`require_complete_delivery` are never patched.

The zero-join fixture is built so a POST-join assertion passes VACUOUSLY: four
real, well-formed, non-null `query_id`s are submitted and none of their item
ids exists in the pinned snapshot, so the join returns zero rows. The test
asserts BOTH that the naive post-join checks are trivially true AND that the
source-side guard refuses, and the CLI test asserts it exits NON-ZERO and that
no shard directory was written.

Each gate is tested alone, including the pair that proves gate 1 was isolated
(gates 2 and 3 pass for the other-kind job). The declared-order test runs the
bundle REVERSED, because `job-retry`'s `parent_job_id` points at the chunk and
it was inserted second — so `parent_job_id`, insertion order and latest
timestamp would ALL pick the retry, and only declared order can pick the
chunk.

Idempotency is tested LOCALLY through the producer path: two runs produce the
same slot keys with identical payloads, no slot is claimed twice, and
`publish_bundle` a second time is a no-op. No live load was built to test it.

---

## Item 3 — t7-delivery-and-scanner (`773eae0`)

### What was built

`src/kbutillib/domains/kbase/berdl/council_delivery.py` (NEW), deliberately
separate from `council_archive.py`: it is the piece with no database in it and
the piece most worth testing alone. It DRIVES the archiver — `archive_snapshot`,
`ArchiveLedger`, `delivery_state_dir`, `reconcile_delivery`, `receipt`,
`prepare_snapshot`, `resolve_state_root` — and re-implements no batching, no
ledgering and no reconciliation. Its publish stage calls t9's
`publish_from_lakehouse`.

Plus `kbu council delivery-scan` in `src/kbutillib/interfaces/cli/council.py`,
one tick by default and `--ticks 0` for the forever form.

### COUNCIL-DELIVERY manifest v1

`COUNCIL-DELIVERY.json` — deliberately NOT `MANIFEST.json`, because the
snapshot's own single-file checksum record travels in the same directory and
two documents under one name is how a consumer validates the wrong one.

Fields: `schema_version`, `delivery_id`, `store_id`, **`snapshot_id`**,
**`snapshot_file`**, `snapshot_seq`, `snapshot_schema_version`, `created_at`,
`deadline_at` (optional — absent is the legacy form), `files` (relative
`(path, bytes, sha256)`), `bundle` (optional identity:
`campaign_id`, `bundle_digest`, `path`).

`snapshot_id` and `snapshot_file` are ADDITIONS to the field list the design
named, and they are forced rather than chosen: the archiver keys every
archived row AND its whole delivery-state directory on
`(store_id, snapshot_id)`, and `prepare_snapshot` verifies the uncompressed
digest against a declared `snapshot_id` before anything is archived. A
delivery that could not say which of its files is the snapshot, or what that
snapshot's verified digest is, could not be archived at all.

The identity of a delivery is `manifest_digest()` — sha256 over the
canonicalised manifest. A persisted stage outcome records the identity it was
established for, so an edited manifest invalidates every cached outcome.

### The four contract rules, as implemented

1. **Presence is not arrival.** `verify_arrival` requires every listed file to
   be present, the declared SIZE, and sha256-matching. Size is checked before
   the digest, because a partially-synced 900 MB file is the common case and
   digesting it to learn that is pure waste.
2. **A first mismatch is `retry`, with progress persisted.**
   `DeliveryProgress` records each file verified by `(path, bytes, sha256)`, so
   a retry does not re-digest what it has already established. Tested: the
   second tick reports the snapshot under `reused` rather than re-reading it.
3. **Past the deadline it is a failed delivery**, raised as
   `DeliveryNotArrivedError` carrying per-file `expected_bytes` /
   `observed_bytes` / `expected_sha256` / `observed_sha256` and a `problem` of
   `absent` / `size` / `digest`. The deadline comes from the manifest's
   `deadline_at`; for a legacy manifest it is `mtime(manifest) + 6h` computed
   ONCE on first observation and PERSISTED, because consumer-side mtime can
   reset on sync and a deadline recomputed each tick never expires.
4. **Archival and publication are persisted separately** in
   `delivery_outcomes.json`, each with its own state, receipt and delivery
   identity, each retried independently. Publish records
   `not_applicable` — never `ok` — when the delivery declares no bundle. A
   receipt whose `delivery_digest` disagrees with the manifest's is DISCARDED
   and its stage re-run; re-running is safe because the archiver resolves an
   already-landed batch from its `load_batch_id` rather than appending it
   twice, which the row-count assertions confirm.

### The scanner

Every tick ENUMERATES the drop directory (by the presence of a manifest),
resolves each delivery against the archiver's load ledger and the persisted
outcomes, and completes whatever is incomplete. A directory with no manifest is
not a delivery. One bad delivery does not stop the good ones — a per-delivery
failure is a report, never an exception out of `tick()`.

Four EXPLICIT settings and no environment guessing beyond the archiver's own
`KBU_COUNCIL_ARCHIVE_DIR`: `drop_dir`, `state_root`, `scratch_dir`,
`poll_seconds` (default 300). The class docstring states the real latency: a
tick interval plus a Dropbox sync, tens of minutes at best, not seconds.

State COMPOSES with the archiver's rather than shadowing it: both live in
`council_archive.delivery_state_dir(store_id=..., snapshot_id=...,
root=state_root)`, so one delivery has ONE directory holding the archiver's
`delivery.json` + `archive_ledger.jsonl` beside this module's
`delivery_progress.json` + `delivery_outcomes.json` + `scanner.lock`. A test
asserts all four filenames are in the one directory.

Concurrency is an `O_EXCL` claim per delivery (`DeliveryLock`), reclaimed by
AGE after two hours and never by checking a pid — a pid on a restarted pod can
be alive and be somebody else entirely. The loser skips that delivery for one
tick and picks it up on the next.

### THE JUPYTER STARTUP REGISTRATION STEP — A NAMED GAP

**The concrete registration command for kbhub's Jupyter startup facility is
NOT determinable from this repository, and it was not invented.**

What I checked, and where: a tree-wide grep over `src/`, `docs/` and
`agent-io/` for `jupyter.*startup`, `startup.*jupyter`, `.ipython/profile`,
`jupyter_notebook_config` and `JUPYTER_STARTUP` returns NOTHING. The only
references to kbhub anywhere in `src/` are `capability.POD_MACHINE = "kbhub"`,
the BERDL transport's in-pod refusal message, and the KBUtilLib#91 caveats in
the council modules. There is no startup directory, no IPython profile path
and no pod init hook named anywhere in the tree.

So `council_delivery.OPERATOR_INSTALL` holds, as data rather than prose:

- `known` — kbhub has NO user systemd (so no systemd unit is proposed, and a
  test asserts the module contains neither `[Unit]` nor `systemctl`); a
  restartable daemon there is booked to a Jupyter startup routine and is
  restarted at arbitrary points; the loop is
  `CouncilDeliveryScanner(...).run()` or `kbu council delivery-scan --ticks 0`;
  a second instance is safe.
- `evidence_of_restart_recovery` — three concrete checks: a FRESH instance must
  complete exactly the incomplete one of two deliveries; deleting the archive
  receipt and running one tick must reproduce it identically (modulo
  `issued_at`) with the sink's row count UNCHANGED; and
  `reconcile_delivery` must report `complete: true` for every delivery the
  loop reports as archived. All three are also pinned as tests.
- `GAP` — the statement above, naming Albert as the owner, and saying why it is
  not guessed at: a wrong registration command reads exactly like a right one
  until the pod restarts and nothing comes back, which is the failure this
  whole scanner exists to make impossible.

**Until that step is recorded, the loop is runnable by hand and is NOT an
installed service.**

### Tests

`tests/berdl/test_council_delivery.py` — **53 tests, all passing**, mostly
unhappy paths.

No-injection criterion met: `archive_snapshot` is REAL in every test and every
archive claim is a ROW COUNT in a real `SqliteCouncilSink` — not a call count
on a fake archiver. The concurrency test runs TWO REAL scanner instances in
two threads (each with its own connection to one sink FILE) against one drop
directory, synchronised on a barrier, and then counts rows in `calls`,
`items`, `jobs` and `attempts`.

The publisher is stubbed in exactly the two tests whose SUBJECT is what the
scanner does when publication FAILS, and there is a separate test that runs the
REAL `publish_from_lakehouse` end to end (it refuses, correctly, because the
delivered jobs carry no disposition — which proves the scanner is wired to the
real gates rather than to something that always says yes).

Covered: manifest v1 round-trips; eleven malformed-manifest cases rejected
(including a naive timestamp, an absolute inventory path and an escaping one);
the inventory is measured and relative; a complete delivery archives and the
archiver's own `reconcile_delivery` agrees; a missing file inside the deadline
is `retry` not failure; progress is persisted and reused; the same state past
the deadline fails with per-file evidence; a same-SIZE digest mismatch has the
same deadline semantics; a truncated file is caught on size; the legacy
mtime-reset case keeps its frozen deadline and fails at the ORIGINAL deadline
(the test also asserts that a recomputed deadline would still be waiting); the
frozen record survives a fresh process; publish-only retry with the archive
cached and the row count unchanged; a cached publish is not redone; a delivered
bundle whose digest disagrees is refused; receipt re-export without
re-archiving; a receipt under another identity rejected and the stage re-run
with rows landing once; an edited manifest invalidating both cached stages; a
fresh scanner completing exactly the incomplete delivery; a directory with no
manifest; one bad delivery not stopping the good ones; the bounded run sleeping
once between two ticks and not after the last; the lock's exclusivity, its
age-based reclaim, and a locked delivery picked up next tick; and the verb
through the real CLI.

---

## Divergences reconciled: what the prompt claimed vs what the tree held

Listed so the taskplan can be corrected at source.

| # | the prompt said | the tree held | what I did |
|---|---|---|---|
| 1 | the task is named `council-lakehouse-t3-t10-t9-t7`, implying four items | the prompt's body declares **three** items (`t10`, `t9`, `t7`), gives three sets of success criteria, and names no t3 work anywhere | implemented the three items the prompt specifies. **t3 was not implemented**: there are no t3 success criteria, no t3 module path and no t3 description in the prompt to implement against, and the tranche-2 record shows t1–t4 already landed on `main` (`3b8d9e7` is t3). I read the `t3` in the task id as a naming artefact of the branch name, not a fourth item. |
| 2 | "a fresh REAL `SqliteCouncilStore`"; `council_schema`'s docstring also says `SqliteCouncilStore.find_reusable` | the class is `SqliteStore` (`ai/council/store/sqlite.py:542`). There is no `SqliteCouncilStore` | used `SqliteStore`. The stale name in `council_schema.py`'s docstring was left alone — it is a comment in a DO-NOT-TOUCH-adjacent file and fixing it is not this task. |
| 3 | `schema.py:145` `query_types`, `:165` `jobs`, `:194` `items`, `:216` `calls`, `:300` `attempts`, `:327` `endpoint_state`, `:351` `workers` | the symbols are all present; the line numbers are off by a few (e.g. `query_types` is at `:145` as claimed, `calls` at `:216`). Close enough to be usable | used the symbols. |
| 4 | the taskplan: "Add `seed_from_rows()` to `store/sqlite.py` and wire the CLI" — naming no home for the lakehouse READ | the store layer imports nothing from `kbase.berdl`, and `ai/council/store/__init__.py` documents SQLAlchemy as an optional extra | split the item: the rules and the write live in `sqlite.py` as instructed, and the lakehouse read lives in a NEW `berdl/council_seed.py`. Putting the read in `sqlite.py` would make the store layer depend on the lakehouse layer. |
| 5 | t9's taskplan said to emit "through the existing plan/shard/load/verify path" and named no producer entry point | `genome/ontology_mapping/council_publish.council_publish(run_dir, verdicts=...)` already IS that entry point, and it needs a `PLAN.json` | t9 writes `PLAN.json` from the bundle's own carried plan and calls that function. No new render, no new shard driver. |
| 6 | the t7 field list for manifest v1 omits any snapshot identity and any host | the archiver keys rows and its state directory on `(store_id, snapshot_id)`, `prepare_snapshot` verifies against a declared `snapshot_id`, and `store_host` is in the archive's identity block on every row | added `snapshot_id` and `snapshot_file` as REQUIRED v1 fields and `store_host` as an optional-valued one, all documented as forced additions. Without the first two a delivery is not archivable; without the third every row archived through the rail silently carries an empty host (`971dde9`). |
| 7 | t7: "a receipt whose identity does not match the manifest digest is rejected rather than trusted" — without saying what happens next | — | the mismatched record is discarded and its stage RE-RUN, which is safe because of the archiver's `load_batch_id` resolution. The tests assert the rows land exactly once. Stated here because "rejected" alone leaves the next step open. |
| 8 | the header's claim that `calls.reply_parsed` stores Python `None` as the TEXT `'null'` | confirmed — `_filled` widens every absent JSON column to Python `None` and `sa.JSON` has `none_as_null=False`. Verified directly in the seed round-trip test by reading the raw column | the seed carries it through as JSON null, and the test asserts the raw value is the string `'null'`. |
| 9 | t9's "running twice does not duplicate rows" | the shard path writes a new shard run per invocation; it does not mutate anything | tested as "two runs produce the same slot keys with identical payloads, and no slot is claimed twice", which is the local, honest form. No live load was built. |

### A stop-and-report clause I did NOT honour

None of the three items carried one. The `WrongQueryKindError`-style refusals
in the code are product behaviour, not task-prompt stop clauses.

---

## Files touched

New source:
- `src/kbutillib/domains/kbase/berdl/council_seed.py` (t10, the lakehouse read)
- `src/kbutillib/domains/kbase/berdl/council_publish.py` (t9)
- `src/kbutillib/domains/kbase/berdl/council_delivery.py` (t7)

Modified source:
- `src/kbutillib/domains/ai/council/store/sqlite.py` (t10: `seed_from_rows`,
  `seed_records`, the seed helpers and constants)
- `src/kbutillib/domains/ai/council/store/errors.py`,
  `src/kbutillib/domains/ai/council/store/__init__.py` (t10: three errors)
- `src/kbutillib/interfaces/cli/council.py` (t10 `seed-store`, t7
  `delivery-scan`)
- `src/kbutillib/interfaces/cli/ontomap.py` (t9
  `council-publish-lakehouse`)

New tests:
- `tests/berdl/test_council_seed.py` (28)
- `tests/berdl/test_council_publish_lakehouse.py` (41)
- `tests/berdl/test_council_delivery.py` (51)

Modified tests:
- `tests/cli/test_ontomap_cli.py` — added `council-publish-lakehouse` to
  `IMPLEMENTED_VERBS`. That list is asserted to equal the registered verb set
  exactly, so a new implemented verb has to be declared there; it is NOT a
  stub and must not be in `STUB_VERBS`.

### Files deliberately NOT touched (the prompt's do-not-touch list)

`src/kbutillib/interfaces/cli/kbdl_index.py`, everything under
`src/kbutillib/koros_arc_store/`,
`berdl/clearinghouse_manifest.py`, `berdl/clearinghouse_shard.py`,
`berdl/backfills/*.toml`, `berdl/transports.py`. t9 CALLS the plan/shard path
and the shipped preset; it edits neither. No item needed an edit to any of
them.

---

## Verification

### Environment

The task venv at `.task-venv/` has **no pytest and no pandas**, so it cannot
run this suite at all. Tests were run with the repo's provisioned venv,
`/home/chenry/venvs/kbdl/bin/python` (cpython 3.11.15, pytest 9.1.1) — the
same interpreter `build-baselines/kbutillib-h100-main-6310d61.json` records
for this machine — with `PYTHONPATH=$PWD/src` and `TMPDIR=/scratch/chenry/tmp`.
**Nothing was installed into it.**

### Baseline

`build-baselines/kbutillib.json` exists but is stale and was captured in a
minimal venv at `0f84803` (99 known failures, all missing optional deps);
`build-baselines/kbutillib-h100-main-6310d61.json` is the h100 full-suite
capture (56m56s, 69 failures, 6 of them the machine-local
`test_types_config.py::TestPackagedDefaultConfig` config leaks). Per the task
prompt's rule 4 the full suite was NOT run. Instead the TARGETED selection was
run on the base commit and on the branch tip and the sorted FAILED node-id
lists compared.

### The targeted selection

Base commit `ed40f28` was checked out into a detached worktree at
`/scratch/chenry/base-ed40f28` (`git worktree add --detach`) so the two sides
could be measured with the same interpreter and the same selection. No branch
was created.

Selection (20 files — the council store, the council archive/sink/schema/
disposition stack, the clearinghouse ingest/manifest/shard/preset tests, the
publication bundle, the run-directory publisher, and the council/ontomap/
clearinghouse CLIs):

```
tests/berdl/test_council_archive.py
tests/berdl/test_council_sink.py
tests/berdl/test_council_schema.py
tests/berdl/test_council_disposition.py
tests/berdl/test_clearinghouse_ingest.py
tests/berdl/test_clearinghouse_manifest_and_shard.py
tests/berdl/test_clearinghouse_function_to_go_preset.py
tests/berdl/test_berdl_package_exports.py
tests/domains/test_council_bundle.py
tests/domains/test_ontomap_council_publish.py
tests/domains/ai/council/test_store_contract.py
tests/domains/ai/council/test_store_identity.py
tests/domains/ai/council/test_store_migrate_v2.py
tests/domains/ai/council/test_store_path_refusals.py
tests/domains/ai/council/test_queries.py
tests/domains/ai/council/test_council_utils.py
tests/domains/ai/council/test_council_utils_store_identity.py
tests/cli/test_council_cli.py
tests/cli/test_ontomap_cli.py
tests/cli/test_clearinghouse.py
```

| side | sha | command | result | exit |
|---|---|---|---|---|
| base | `ed40f28` | `python -m pytest <selection> -q -p no:cacheprovider --no-header` | **1166 passed, 3 skipped, 0 failed** | 0 |
| branch | `71221da` (HEAD) | same selection **plus** the three new test files | **1288 passed, 3 skipped, 0 failed** | 0 |

**Sorted FAILED node-id diff: EMPTY.** Zero failures on either side, so
nothing that passed on the base fails on the branch. The 122-test delta is
exactly the three new files (28 + 41 + 53).

Per-item runs, all exit 0:

| item | command | result |
|---|---|---|
| t10 | `pytest tests/berdl/test_council_seed.py -q` | 28 passed |
| t9 | `pytest tests/berdl/test_council_publish_lakehouse.py -q` | 41 passed |
| t7 | `pytest tests/berdl/test_council_delivery.py -q` | 53 passed |

### The wider sweep, also base vs branch

A targeted selection can only prove what it selects, and three of the files I
edited are shared surfaces (`store/sqlite.py`, `cli/council.py`,
`cli/ontomap.py`). So `pytest tests/berdl tests/domains tests/cli -q` — about
4,600 tests, ~290 s, well inside the tool ceiling — was run on BOTH sides and
the failed node-ids diffed:

| side | sha | result | duration |
|---|---|---|---|
| base | `ed40f28` | **4459 passed, 27 failed, 11 skipped** | 290 s |
| branch | `773eae0` (t7's commit; the four follow-ups came after) | **4579 passed, 27 failed, 11 skipped** | 289 s |

```
$ diff <(sorted base FAILED nodeids) <(sorted branch FAILED nodeids)
$ echo $?
0
```

**The two 27-name sets are BYTE-IDENTICAL.** Not "the same number" — the same
names. The +120 passed is the three new test files as they stood at that
commit.

All 27 are PRE-EXISTING and machine-local, none in a file this branch touches:

| area | n | what they are |
|---|---|---|
| `tests/cli/test_kind.py` + `test_kind_install_containment.py` | 18 | the KIND/KING install surface; on the h100 full-suite baseline these are already in the 69 known failures |
| `tests/domains/ai/council/test_types_config.py::TestPackagedDefaultConfig` | 6 | the six config leaks the task prompt names as KNOWN on the base — this host's `~/.kbutillib/config.yaml` |
| `tests/cli/test_model_recording.py` | 2 | pre-existing |
| `tests/domains/ai/llm/test_static_headers.py::TestPackagedExample` | 1 | the same class of machine-local config leak as the six (an endpoint the local config declares) |

The four follow-up commits (`322de65`, `971dde9`, `639777c`, `71221da`) landed
after that sweep. The TARGETED selection was re-run at HEAD `71221da` and is
**1288 passed, 3 skipped, 0 failed** — the +2 over the earlier 1286 being the
two tests those fixes brought with them — and each follow-up's own test file
was run green before its commit. None of the four touches a file outside
`berdl/council_publish.py`, `berdl/council_delivery.py` and their two test
files.

### Not run, and why

The full suite (`pytest tests/`) was NOT run: the h100 baseline measures it at
**56m56s**, 5.7× the 600 s tool ceiling, and the task prompt forbids it
explicitly. The six known machine-local
`test_types_config.py::TestPackagedDefaultConfig` failures are in neither
selection above; they are config-file leaks on this host and are unrelated to
every file touched here.

No linter was run: `ruff` is declared in `pyproject.toml`'s dev extras but is
not installed in any venv on this machine (`python -m ruff` fails in both the
task venv and the kbdl venv). Unused imports and undefined names were checked
by reading; every new module imports cleanly.

---

## Inferred rather than verified

- **Iceberg behaviour.** Every sink in every test is `SqliteCouncilSink`.
  SQLite is not Iceberg: no partition specs, no snapshot history, no
  `data_lakehouse_ingest` type map, and permissive type affinities where
  Iceberg's are strict. Agreement between the two is established by NO test
  here and may not be claimed. (The `STRING`-affinity defect t10 found is
  itself a SQLite-only artefact — but the coercion it forced is harmless on
  the pod side and necessary on the staging side.)
- **The in-pod load.** `kbu clearinghouse load` and `kbu clearinghouse verify`
  were never run. t9's receipt reports the load as awaiting operator
  acceptance and leaves `target_table_snapshot_id` null.
- **`BerdlCouncilSink`.** Unexercised end to end, as before: KBUtilLib#91.
- **The Dropbox rail.** t7's drop directory is a `tmp_path` in every test. The
  behaviour of a real Dropbox sync — in particular whether it rewrites mtimes
  in the way the frozen-deadline logic is designed for — is INFERRED from the
  design's own measured claim, not re-measured here.
- **The scanner as a service.** It has never been registered with anything.
  See the named gap above.
