# t13-claim-index-backfill

**task_id:** t13-claim-index-backfill
**branch:** maestro/developer/t13-claim-index-backfill
**commit_shas** (chronological, one commit):
- 476af2bb62ecdbddcb4be77105919da0e730533a — fix: backfill ix_calls_status_seat_not_before onto existing v2 stores

## Summary

`SqliteStore.migrate()` on an already-SCHEMA_VERSION-2 store only ran
`add_missing_columns()`, which patches missing *columns* but not missing
*indexes*. `ix_calls_status_seat_not_before` (added by an earlier phase to
support the seat-filtered claim scan) was therefore created only by
`metadata.create_all()` for brand-new stores and never reached a store that
already existed at version 2 — including the live production store holding
a paused 19,272-item job with ~57,000 pending call rows. Added
`SqliteStore.add_missing_indexes()` in `store/sqlite.py`, the index-shaped
twin of the existing `add_missing_columns()`: it iterates every table in
`store/schema.py`'s declared metadata and every index each table declares,
and for any index missing from the live database issues
`CREATE INDEX IF NOT EXISTS` (via SQLAlchemy's
`sa.schema.CreateIndex(index, if_not_exists=True)`, which also correctly
reproduces the partial unique index `ux_calls_one_live`'s `WHERE live`
predicate). It is deliberately general over every declared index rather than
hard-coded to `ix_calls_status_seat_not_before`, so the next index someone
adds does not silently reopen the same gap. `migrate()` now calls
`add_missing_indexes()` at the same point it calls `add_missing_columns()`,
in both the "already current" and the "v1→v2" branches.
`schema.py`'s `SCHEMA_VERSION` constant is untouched (still `2`); only a
short comment was added next to the index declaration pointing future
maintainers at the backfill mechanism.

## Files touched

- `src/kbutillib/domains/ai/council/store/sqlite.py` — added
  `add_missing_indexes()`; wired it into `migrate()` alongside
  `add_missing_columns()`; updated the `migrate()` docstring.
- `src/kbutillib/domains/ai/council/store/schema.py` — added a comment next
  to `ix_calls_status_seat_not_before`'s declaration documenting that
  `add_missing_indexes()` is what backfills a declared index onto an
  existing store. No index definitions, table shapes, or `SCHEMA_VERSION`
  changed.
- `tests/domains/ai/council/test_store_contract.py` — added
  `TestAdditiveIndexSeatNotBefore` (4 tests, see below).

`store_contract.py` and `test_store_migrate_v2.py` were in-scope but did not
need changes: the existing `store`, `job_row`, `item_row`, `call_row` and
`T0` fixtures/helpers in `store_contract.py` already covered everything the
new tests needed, and the v1→v2 migration tests in
`test_store_migrate_v2.py` did not need a new case because the backfill is
exercised identically through the "already v2" path, which is what the new
tests cover.

## Success criteria check

- **A store created without `ix_calls_status_seat_not_before` gains it on
  open/migrate with all pre-existing rows intact** — PASS.
  `test_migrate_adds_the_index_and_keeps_every_row` creates a job/item/call,
  claims and records an attempt on it, drops the index by hand (standing in
  for a store built before the index existed), calls `migrate()`, and
  asserts the index is present afterward and the job/call/attempt rows are
  byte-identical to before.
- **Running the backfill twice is a no-op that raises nothing** — PASS.
  `test_add_missing_indexes_is_idempotent` calls `add_missing_indexes()`
  twice after dropping the index; the first call reports the index name
  created, the second returns `[]` and does not raise. Also exercised
  implicitly in every other test, since `store` is itself the product of
  `init_schema()` followed by (in some tests) a second `migrate()`/
  `add_missing_indexes()` call.
- **A store already carrying the index is unchanged** — PASS.
  `test_a_store_already_carrying_the_index_is_untouched` asserts the
  freshly-`init_schema()`'d fixture already has the index, then calls both
  `add_missing_indexes()` (returns `[]`) and `migrate()` (returns the
  version, unchanged) and re-asserts the index and its sibling are both
  still present.
- **`ix_calls_status_not_before` remains present** — PASS. Asserted
  directly in `test_migrate_adds_the_index_and_keeps_every_row` and
  `test_a_store_already_carrying_the_index_is_untouched`; `add_missing_indexes()`
  only ever adds a missing index, never drops, rebuilds, or reorders one, so
  this holds structurally and not just in the two tests that assert it.
- **`SCHEMA_VERSION` is still 2** — PASS. Confirmed by reading
  `schema.py` (`SCHEMA_VERSION = 2`, untouched by this diff) and by
  `store.schema_version() == sch.SCHEMA_VERSION` assertions before and after
  `migrate()` in the new tests.
- **No test that passed on the base commit fails on the branch** — PASS,
  see `tests_run` below: the identical 4 pre-existing failures (by name) are
  the only failures, and the passed count rose by exactly the 4 new tests
  added.

## Tests run

Baseline, measured on this worktree before any change (confirmed to match
the envelope's stated baseline):

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python \
  -m pytest tests/domains/ai/council -q -p no:cacheprovider --no-header
```
→ `4 failed, 980 passed`, all 4 failures in
`tests/domains/ai/council/test_types_config.py::TestPackagedDefaultConfig`
(machine-local `$HOME`/config.yaml leakage past a monkeypatched `HOME`, per
dev row 53 — not touched by this task, not mine).

Same command on the branch, after the commit:

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python \
  -m pytest tests/domains/ai/council -q -p no:cacheprovider --no-header
```
→ `4 failed, 984 passed` (159s). The 4 failures are the exact same 4 tests,
by name, as the baseline:
- `TestPackagedDefaultConfig::test_every_packaged_profile_builds_a_roster`
- `TestPackagedDefaultConfig::test_the_packaged_config_declares_no_poplar_endpoint`
- `TestPackagedDefaultConfig::test_on_a_fresh_machine_with_no_kbutillib_config`
- `TestPackagedDefaultConfig::test_cli_endpoints_default_to_one_call_at_a_time`

980 + 4 (the new tests in `TestAdditiveIndexSeatNotBefore`) = 984. No
regression; no new failure.

I did not run the full `tests/` tree per the machine-specific instruction
(spurious `tests/core/test_composition_smoke.py` setup errors inside a
`.maestro` worktree are environmental and out of scope for this task).

## SCHEMA_VERSION confirmation

`src/kbutillib/domains/ai/council/store/schema.py` line 54:
`SCHEMA_VERSION = 2` — unchanged by this diff (the only edit to that file is
a comment, confirmed by `git diff`). Every test in
`TestAdditiveIndexSeatNotBefore` additionally asserts
`store.schema_version() == sch.SCHEMA_VERSION` both before and after
`migrate()`/`add_missing_indexes()`.

## Index-build timing / concurrency safety

Measured directly (not estimated) against a synthetic store built in this
worktree: one job, one item, 57,000 `calls` rows (the production store's
approximate pending-row count) spread across 20 distinct `seat_name` values,
each with a `status='pending'` and a unique `upstream_digest` (to satisfy
the `ux_calls_one_live` partial unique index). After dropping
`ix_calls_status_seat_not_before` to simulate a pre-backfill store:

- `add_missing_indexes()` building the index from scratch over the 57,000
  rows: **~49 ms** (0.049 s).
- A second call, with the index already present (the no-op idempotent
  case): **~2 ms** (0.002 s).

`SqliteStore` sets `busy_timeout=30000` (30s) on every connection (see
`_install_sqlite_pragmas` in `sqlite.py`), so a worker's claim attempt that
happens to collide with this backfill simply waits up to 30s for the write
lock rather than failing — and at ~49ms the real wait, if any, is far under
that budget. `CREATE INDEX` in SQLite does take the database's write lock
for its duration (it's a schema change), so a worker's write is blocked
*during* that ~49ms window, not merely slowed, but the window closes two to
three orders of magnitude before `busy_timeout` would ever surface an
error. `add_missing_indexes()` is also only reached from `migrate()`, which
is run once at store-open time (`kbu council db migrate` / the daemon's
startup path), not on every claim — so this ~49ms cost is paid once per
store-open, not per worker or per claim cycle. I judge this safe for the
realistic 57,000-row case; I did not attempt to measure concurrent
contention under a live `flock`-held worker process, since the SQLite-level
`busy_timeout` and the one-shot-at-open-time call pattern already bound the
risk without it.

## Caveats

- The task's background-reference paragraph ("Claim filtering by seat" /
  Q8) in `fullprompt.md` was reachable from this worktree (both sections
  read successfully), so no judgement call was needed to work around an
  unreachable reference.
- `store_contract.py` and `test_store_migrate_v2.py` were named as owned
  files but ended up untouched — see "Files touched" above for why; this is
  a deliberate minimal diff, not an oversight.
- I did not add a CLI-surfaced report of which indexes `kbu council db
  migrate` added versus which columns, beyond returning the list from
  `add_missing_indexes()` (mirroring `add_missing_columns()`'s existing
  return-value convention) — wiring that into the CLI's printed migration
  report, if there is one, was out of scope for the files this task owns.
