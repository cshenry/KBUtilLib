# t2-store-claim-filter-and-schema

## task_id
t2-store-claim-filter-and-schema

## branch
maestro/developer/t2-store-claim-filter-and-schema

## commit_shas
- 77501f23c9d8c6e6cd7c3fda26e3d5cb6db7275c -- feat: filter claims by seat/role and add attempts.queued_at

(single commit; run `git log main..HEAD` on the branch to confirm -- base
was `35f26870d678e4a9f5b31e94db3464865ef5462d`)

## summary

Part 1 (claim filter): added optional `seats` and `roles` keyword-only
parameters to `select_claimable()` in `store/queries.py`, threaded through
to `SqliteStore.claim_calls()` (`store/sqlite.py`) and the `CouncilStore`
protocol declaration (`protocols.py`, the one `claim_calls` method around
line 240). When given (not `None`) they add
`calls.seat_name.in_(sorted(seats))` / `calls.role.in_(sorted(roles))` to
the existing WHERE clause; both default to `None`, which is unchanged
behaviour for every existing caller. The four-tier claim order (job
priority desc, submitted_at asc, item_order asc, role_rank asc, call_id
asc) was left untouched -- the new conditions are appended to the WHERE
list, not inserted into the ORDER BY. Added a new
`ix_calls_status_seat_not_before` index on `(status, seat_name,
not_before)` in `store/schema.py`, alongside (not replacing) the existing
`ix_calls_status_not_before`. No `SCHEMA_VERSION` bump: both `seat_name`
and `role` are already non-null columns on `calls`, so this is a WHERE
clause and an index, not a migration.

Part 2 (additive column): added a nullable `queued_at` column
(`UtcDateTime`, same type as the other timestamp columns) to the
`attempts` table in `store/schema.py`, and appended `("attempts",
"queued_at")` to `ADDITIVE_COLUMNS`. `SqliteStore.migrate()` /
`add_missing_columns()` already walk `ADDITIVE_COLUMNS` generically, so no
code change was needed there -- a store missing the column gets it added
on the next `migrate()`. Nothing writes the column in this task (that is
a later task in the PRD, per the task prompt). `SCHEMA_VERSION` stays at
2.

Tests were added to `tests/domains/ai/council/test_store_contract.py`:
a new `TestClaimCallsSeatsAndRoles` class (seat filter excludes another
seat's row with that row at the unfiltered FIFO head; role filter
excludes other roles; seats+roles combine; `seats=None, roles=None`
reproduces today's exact row set and order; claim order is unchanged
under a filter that excludes nothing), and a new
`TestAdditiveColumnQueuedAt` class (a store with the column dropped by
hand gets it back via `migrate()`, idempotently, with every pre-existing
job/item/call/attempt row intact and the new column read back as `NULL`).

## files_touched
- `src/kbutillib/domains/ai/council/store/queries.py` -- `select_claimable()` gains optional `seats`/`roles`
- `src/kbutillib/domains/ai/council/store/sqlite.py` -- `SqliteStore.claim_calls()` gains matching optional `seats`/`roles`, passed through to `select_claimable()`
- `src/kbutillib/domains/ai/council/store/schema.py` -- `attempts.queued_at` column, `ADDITIVE_COLUMNS` entry, new `ix_calls_status_seat_not_before` index
- `src/kbutillib/domains/ai/council/protocols.py` -- `CouncilStore.claim_calls()` protocol declaration gains matching optional `seats`/`roles` (the one declaration at ~line 240)
- `tests/domains/ai/council/test_store_contract.py` -- `TestClaimCallsSeatsAndRoles`, `TestAdditiveColumnQueuedAt`

No other files were touched (`store/sqlite.py`'s only other reference to
these changes is the one `claim_calls` method; `protocols.py`'s only
`claim_calls` declaration is the one edited).

## success_criteria_check

- "Store-contract tests prove a seat-filtered claim never returns another
  seat's row while an unfiltered claim is unchanged in rows and order":
  **pass**. `test_seats_a_never_returns_seat_b_with_b_at_the_fifo_head`
  puts seat B at the FIFO head unfiltered (asserted directly) and then
  shows `seats={"A"}` returns only A; `test_roles_critic_never_returns_a_proposer_row`
  and `test_seats_and_roles_combine` cover the role and combined cases;
  `test_seats_none_and_roles_none_is_byte_identical_to_today` and
  `test_claim_order_is_unchanged_under_filtering` cover the unfiltered/
  non-excluding cases against the same mis-inserted-order fixture the
  existing unfiltered order test uses.
- "`attempts.queued_at` is present after `migrate()` on a store created
  without it with all pre-existing rows intact": **pass**.
  `TestAdditiveColumnQueuedAt.test_migrate_adds_the_column_and_keeps_every_row`
  creates a job/item/call/attempt, drops the column by hand (standing in
  for a store built before this column existed), runs `migrate()`, and
  asserts the column is back, `add_missing_columns()` is idempotent, and
  the job/item/call/attempt rows are all still there with the new column
  reading `NULL`.
- "`SCHEMA_VERSION` is unchanged from the base commit": **pass**. Verified
  by inspection (`schema.py` still reads `SCHEMA_VERSION = 2`, the same
  value as on the base commit) and by the test asserting
  `store.schema_version() == sch.SCHEMA_VERSION` both before and after the
  column drop and the `migrate()` call.
- "no test that passed on the base commit fails on the branch": **pass**
  for the four-file targeted selection (see `tests_run` below): 295 passed
  (289 baseline + 6 new), 0 failed. I additionally ran
  `tests/domains/ai/council/test_normalise.py` (not in the targeted
  selection, but the test file most likely to be sensitive to the new
  `attempts` column) as an out-of-scope sanity check: 25 passed, 0 failed.
  I did not run the full suite -- see `tests_run` for why that is out of
  scope for this task.

## tests_run

Targeted selection (exact command from the envelope):

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest \
  tests/domains/ai/council/test_store_contract.py tests/domains/ai/council/test_queries.py \
  tests/domains/ai/council/test_store_migrate_v2.py tests/domains/ai/council/test_store_path_refusals.py \
  -q -p no:cacheprovider --no-header
```

Result: **295 passed in 12.91s** (baseline was 289 passed, 0 failed,
17.88s; the 6 extra passes are the new tests added in this task). 0
failures, 0 skips.

Additional out-of-scope sanity check (not part of the required
selection, run because it is the test file most likely to interact with
the new `attempts.queued_at` column):

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest \
  tests/domains/ai/council/test_normalise.py -q -p no:cacheprovider --no-header
```

Result: 25 passed, 0 failed.

The full suite (~60 minutes, 27+ pre-existing failures at base) was
**not** run -- it does not fit the 600s Bash ceiling and is explicitly the
conductor's responsibility per the envelope, not this task's.

## caveats

- The new `ix_calls_status_seat_not_before` index is created by
  `metadata.create_all()` for a brand-new store (`init_schema()`), but
  SQLAlchemy's `create_all()` does not add new indexes to a table that
  already exists -- so a store created at `SCHEMA_VERSION == 2` by an
  earlier build will not pick up this index automatically via
  `migrate()`/`add_missing_columns()`, since that mechanism only handles
  columns (`ADDITIVE_COLUMNS`), not indexes. This is a performance
  consideration, not a correctness one -- the WHERE clause is correct
  with or without the index -- and the task prompt only asked for the
  index to be "added", with no existing index-migration mechanism in the
  codebase to extend. Flagging it in case a later task in this PRD wants
  an explicit `ensure_index`-style step in `migrate()`.
- `test_seats_none_and_roles_none_is_byte_identical_to_today` and
  `test_claim_order_is_unchanged_under_filtering` both duplicate the
  fixture shape of the existing (unmodified)
  `test_claim_order_is_priority_then_submission_then_item_then_role` in
  `TestClaimCalls`, rather than comparing two live `claim_calls()` calls
  against each other. I chose this over a literal two-call comparison
  because `claim_token` is a fresh random UUID on every claim, so a
  dict-equality check between two separate claims of the same rows can
  never pass regardless of filter correctness; asserting against the
  known-correct expected order (already established by the sibling
  unfiltered test) tests the same "byte-identical" property without that
  false-failure mode.
- I did not add a test asserting the literal compiled SQL of
  `select_claimable(..., seats=None, roles=None)` equals
  `select_claimable(...)` with the keywords omitted entirely (i.e. a
  non-behavioural, SQL-string-level check). The behavioural tests above
  cover the success criterion as stated; a SQL-compile-level test would
  be redundant with them.
