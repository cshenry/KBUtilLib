# Work Record — council-store-queue-ops

## task_id
council-store-queue-ops

## branch
maestro/developer/background-reference-only-agent--council-store-queue-ops

## base commit
4b56acd (`docs(work-record): fill in the record's own commit sha`)

## commit_shas
- c94d2bd — feat(council): the store's queue and job control
- (this record's own commit — see the end of this file)

## summary
Part 2 of 2 of the council store. Fills in the ten methods `SqliteStore` left
as `NotImplementedError("store-queue-ops")` — `claim_calls`, `complete_call`,
`release_call`, `record_attempt`, `find_reusable`, `reclaim_stale`,
`job_status`, `results`, `cancel_job`, `delete_job` — so that call rows become
a working queue.

Every statement they run is built in the new
`src/kbutillib/domains/ai/council/store/queries.py`, which is dialect-neutral
SQLAlchemy Core. The only dialect-specific part is the lock: `SqliteStore`
wraps select-then-stamp in `BEGIN IMMEDIATE` (the re-entrant `transaction()`
the previous milestone landed), and `queries.select_claimable()` takes a
`for_update` flag that `PostgresStore` will set to get `FOR UPDATE OF calls
SKIP LOCKED`.

`src/kbutillib/domains/ai/council/protocols.py`, `types.py`, `argo_utils.py`,
`ai_curation_utils.py`, `store/schema.py`, `store/errors.py`,
`store/__init__.py` and `pyproject.toml` are all byte-identical to the base
commit (`git diff --stat 4b56acd -- <each>` is empty). No new error type was
needed: `store/errors.py` already carried `StaleClaim` and `NotOwner`.

## files_touched
- `src/kbutillib/domains/ai/council/store/queries.py` (new, 537 lines) — the
  dialect-neutral statement builders, plus two dialect-neutral queue *rules*
  (`refuse_prompt_text`, `BLOCKER_BY_ERROR_KIND`) that both stores need and
  that `PostgresStore` would otherwise have to import from the SQLite module.
- `src/kbutillib/domains/ai/council/store/sqlite.py` (modified, +475/-33) —
  the ten methods, their helpers (`_claim_generation`, `_claimed_call`,
  `_assert_lease_current`, `_blockers`, `_owned_job`, `_stale_claim`,
  `_serves`) and an updated module docstring.
- `tests/domains/ai/council/store_contract.py` (modified, +59/-4) — added
  `claim_in_subprocess`, the no-double-claim test's child entry point;
  removed the now-meaningless `QUEUE_OPS_MARKER`.
- `tests/domains/ai/council/test_store_contract.py` (modified, +1286/-26) —
  eleven new test classes (107 cases), replacing the pin test that asserted the
  ten methods raise `NotImplementedError`.
- `agent-io/work-records/council-store-queue-ops.md` (this file).

`tests/domains/ai/council/conftest.py` needed no edit: it re-exports the four
fixtures by name and the new tests use the same ones.

## environment

SQLAlchemy, psycopg and pytest are not in a fresh task venv. The supervisor
had provisioned an EMPTY `.task-venv/` in the worktree (created 2026-10-15
10:44, carrying its own `.gitignore` of `*`, so git never sees it). Installed
into THAT venv only — no shared or service venv was touched, and no `ssh`,
host, systemd unit or database was involved:

```
python -m pip install --upgrade pip
python -m pip install -e '.[council]'                    # SQLAlchemy 2.1.4, psycopg 3.3.6
python -m pip install pytest pytest-asyncio pytest-cov ruff   # pytest 9.1.1
python -m pip install -e '.[dev,ai]'                     # pandas, duckdb, httpx, ipykernel
```

Interpreter: CPython 3.12.3. A reviewer must do the same in its own venv; a
test that fails on `ImportError` there is an environment gap, not a result.

## verification

| Command | Exit | Result |
|---|---|---|
| `python -m pytest tests/domains/ai/council/ -q -p no:cacheprovider` (base `4b56acd`, clean tree) | 0 | 195 passed, 93 skipped |
| `python -m pytest tests/domains/ai/council/ -q -p no:cacheprovider` (branch) | 0 | **292 passed, 190 skipped** |
| `python -m ruff check --output-format=concise src/kbutillib/domains/ai/council tests/domains/ai` | 0 | All checks passed |
| full suite at base `4b56acd`, clean tree, slice A | 1 | 26 failed, 3332 passed, 193 skipped |
| full suite at base `4b56acd`, clean tree, slice B | 0 | 1018 passed, 329 skipped |
| full suite on branch, slice A | 1 | 26 failed, 3429 passed, 290 skipped |
| full suite on branch, slice B | 0 | 1018 passed, 329 skipped |
| no-double-claim test, 4 further repeat runs | 0 | passed every time |

The suite was run in two slices so each `Bash` call stayed well under 600 s
(each slice takes ~60-72 s). Slice A is `tests/annotators arc_context berdl
beril_worktree biochem cli core domains`, slice B is everything else except
`tests/notebook`; the per-file ignores are copied from
`build-baselines/kbutillib.json`'s `command`
(`test_clearinghouse_derivation.py`, `test_escher_utils.py`,
`test_ms_biochem_deltag.py`, `test_kb_annotation_utils_parquet.py`,
`test_comprehensive_gapfill_wrapper.py`, `test_ms_reconstruction_utils.py`,
`tests/notebook`). Every run was in the foreground; nothing was backgrounded.

**No regressions. The comparison was BY NAME against a base run taken in this
venv on a clean tree BEFORE any file was written** — per the dispatcher's note
that `build-baselines/kbutillib.json` is stale (captured at `0f84803` on
2026-09-13 in a differently-provisioned venv). The two `FAILED` name sets are
**the same 26 nodeids and both diffs are empty**: `comm -13` (new on branch)
and `comm -23` (gone on branch) over the sorted lists each produced nothing.
All 26 are pre-existing failures in `tests/cli/test_kind.py` (17),
`tests/cli/test_clearinghouse*.py` (8) and
`tests/core/test_composition_smoke.py` (1) — none in `tests/domains/`, and
none in a file this task touched.

The +97 passes and +97 skips are this task's own: 107 new cases minus the 10
removed pin-test cases, on each of the two parametrisations (SQLite runs,
PostgreSQL skips).

I did **not** rewrite `build-baselines/kbutillib.json`. One exists, so the
task instruction's "if it does not exist" branch does not apply, and
overwriting a shared cache with numbers from a third differently-provisioned
venv would not help the next task any more than it helped this one.

## success_criteria_check

**Criterion 1.** PASS.
- All ten methods are implemented in `store/sqlite.py`
  (`claim_calls`:1108, `complete_call`:1185, `release_call`:1235,
  `record_attempt`:1312, `find_reusable`:1356, `reclaim_stale`:1371,
  `job_status`:1389, `results`:1526, `cancel_job`:1538, `delete_job`:1561).
- Their SQL is dialect-neutral and lives in `store/queries.py`; the only
  dialect-specific thing in the queue path is the lock
  (`transaction()`'s `BEGIN IMMEDIATE`, and the `for_update` flag
  `select_claimable` offers PostgreSQL). `sqlite.py` builds no SQL of its own
  for these methods beyond three one-line `sa.select(jobs/calls)` lookups for
  ownership and existence checks.
- The extended contract suite passes on SQLite: 292 passed, including the
  two-process concurrent-claim test over 200 calls, the ownership refusals
  (`TestCancelJob::test_another_user_may_not_cancel`,
  `TestDeleteJob::test_another_user_may_not_delete`), cancel semantics
  (`TestCancelJob`, 10 cases) and delete keeping reused calls
  (`TestDeleteJob::test_delete_keeps_the_calls_a_later_job_reused`).
- No test that passed at `4b56acd` fails on this branch (see above).

**Criterion 2 (no-injection check).** PASS.
`TestNoDoubleClaim::test_two_processes_never_claim_the_same_call`:
- Real OS processes via `multiprocessing.get_context("spawn")` — two
  `ctx.Process` children.
- Each child runs `store_contract.claim_in_subprocess`, which calls
  `open_store(url)` **itself** and opens its own `SqliteStore` on the same
  on-disk file. No store object crosses the process boundary — the child
  receives only the URL string, the user, its worker id, the endpoint list,
  the limit, a `Barrier` and a result `Queue`.
- Nothing is monkeypatched in the children; `spawn` gives them a fresh
  interpreter that shares nothing with pytest's.
- The parent closes its own seeding store (`seeder.close()`) before the
  children start, so no parent handle is involved in the contention.
- The assertion is on the union of returned `call_id`s:
  `len(handed_out) == len(set(handed_out))` (no duplicate),
  `set(handed_out) == expected` (no gap) and `len(handed_out) == 200`. It then
  re-reads the database and asserts all 200 rows are `claimed`, by one of the
  two workers, under 200 distinct tokens.
- The harness holds **no lock of its own**. The only synchronisation is a
  `Barrier(2)` released before the first claim, whose purpose is to make the
  children contend rather than to serialise them, and a 2 ms `time.sleep`
  between batches that exists for the same reason (see below).

I verified the test has teeth rather than trusting a green tick
(`/tmp/csq/contention_check.py`, a throwaway, not committed):
- **Contention is real.** Instrumenting the split showed, across four runs,
  `119/81`, `112/88`, `168/32` and `193/7` — both children claim on most
  batches. Without the 2 ms yield the split was `193/7`: one child won the
  writer lock back so consistently that it drained almost the whole queue
  alone and the select-then-stamp window — the only place a double claim can
  happen — was contended **once** instead of on every batch. The sleep makes
  the test strictly stronger; it does not serialise anything.
- **A broken lock is caught.** Re-running the same race with the claim under
  a DEFERRED transaction (`transaction(immediate=False)`, the bug `BEGIN
  IMMEDIATE` exists to prevent) fails loudly:
  `OperationalError: database is locked` on the claim UPDATE. That is
  SQLite's deferred-transaction upgrade conflict, so on this engine the wrong
  strategy surfaces as a hard error rather than as a silent duplicate — but
  either way the test distinguishes the two.

**Criterion 3.** PASS.
`TestCompleteCall::test_a_token_invalidated_by_reclaim_stale_is_refused`,
end to end and using only contract methods:
1. `register_worker` a daemon, then `claim_calls(..., now=utcnow() - 30min)`.
2. `heartbeat_worker("w1", long_ago)` back-dates the heartbeat past the
   staleness window.
3. `reclaim_stale()` returns `1`.
4. `complete_call(call_id, old_token, "ok", reply_text="too late")` raises
   `StaleClaim`.
5. The row is asserted unchanged: `status == "pending"`, `reply_text is
   None`, `reply_parsed is None`, `claim_token is None`.

## reconciliation (prompt vs tree)

The prompt's CONTEXT section is a design-branch snapshot and disagreed with
`main` in five places. In each case the tree won.

1. **`store/errors.py` already has `StaleClaim` and `NotOwner`.** The
   dispatcher's note said to add them there "if missing". Both are present
   (`errors.py:60`, `errors.py:70`) with docstrings that match the contract,
   so I added nothing and `errors.py` is byte-identical to the base commit.
2. **`transaction()` is already `BEGIN IMMEDIATE` by default and re-entrant.**
   The prompt names `BEGIN IMMEDIATE` as something `claim_calls` must do. It
   is already the default for every `transaction()` on this class, and nested
   calls join the outer transaction. I used it as-is and opened no second
   locking scheme — which is also what lets `complete_call` be called inside
   a caller's larger transaction (the worker's "complete + record attempt +
   add tokens + advance" unit) without deadlocking against itself.
3. **The partial unique index is five columns, with `live` as the predicate.**
   Already settled by the previous milestone; `cancel_job` sets `live=False`
   on superseded calls, which is exactly what releases them from that index
   so a replacement call can be added later. Nothing needed changing.
4. **`configuration_mismatch` is not a declared error kind.** The prompt's
   `job_status` blocker list requires it, but `types.ERROR_KINDS`
   (`types.py:101`) does not contain it and the prompt's own kind-strings list
   omits it too. The store does not validate the kinds it stores, so I wired
   the blocker up anyway via `queries.BLOCKER_BY_ERROR_KIND` — it will start
   reporting the moment anything writes that kind — and tested it by writing
   that kind into a pending call's `error` column. **`types.py` was not
   edited** (the prompt forbids it, and adding the kind is the engine
   milestone's call, not the store's).
5. **The pin test had to go, not be kept.** `test_store_contract.py` carried
   `test_queue_methods_are_declared_but_not_yet_implemented`, which asserted
   all ten methods raise `NotImplementedError("store-queue-ops")`. Implementing
   them necessarily falsifies it, so it was deleted along with the
   `QUEUE_OPS_MARKER` constant it used. Its own docstring named this milestone
   as the one that would replace it.

## design decisions not dictated by the prompt

- **One claim token per CALL, not per claim batch.** The prompt says "a fresh
  claim_token (uuid4 hex)" for the batch. A shared token would let a worker
  settle a call it no longer holds by quoting a sibling's, and
  `complete_call`'s whole job is to tell those apart. One token per row.
- **`complete_call` clears the token.** Not stated. It is what makes a second
  completion a `StaleClaim` instead of a silent double write, and it is why
  `_claimed_call` treats a *cleared* token as stale just as much as a
  different one.
- **The lease generation on a claim is derived, not passed.** `claim_calls`'s
  contract signature has no generation argument, yet the contract says the
  claim carries "the caller's lease_generation (None for foreground)". So
  `_claim_generation` looks up `daemon_lease` for `(user, holder=worker_id)`:
  a worker holding the lease claims under its generation, anyone else under
  `None`. A foreground claim is therefore never refused on lease grounds —
  there is no lease behind it to supersede.
- **`reclaim_stale` does not charge an attempt.** Not specified. Nothing was
  learned about the call — its worker died — so incrementing `attempt_count`
  would exhaust the retry budget of a call that was never actually tried.
  `release_call` does charge one by default, because that release followed a
  real failure.
- **`release_call` writes `not_before` unconditionally, including `None`.** A
  release with no backoff must CLEAR an earlier deadline rather than leave the
  call parked behind one that has already been served.
- **`job_status` counts only `pending` calls as outstanding, not `claimed`.**
  A claimed call is work in flight; counting it as waiting would make a busy
  job look stalled and would fire a `no_daemon` blocker at a job whose daemon
  is mid-call.
- **`percent_complete` is `100.0` for a job with no items.** The contract's
  formula divides by `n_items`, which is undefined at zero. There is nothing
  outstanding, so `100.0`; `0.0` would make such a job look stuck forever.
  `n_items` is the stored `jobs.n_items` column, which `create_job` defaults
  to `len(items)`.
- **Blockers are reported independently, not deduplicated.** An endpoint can
  be both halted and unserved; suppressing either would send the user to fix
  the wrong thing. `test_halted_and_unserved_are_reported_independently`
  pins it.
- **A worker that declared no endpoints is treated as serving all of them.**
  `workers.endpoints` is nullable. The worker IS running, so reporting
  `no_daemon` against it would tell the user to start a second one for no
  reason.
- **`pending_calls_by_role_endpoint` is nested `{role: {endpoint: n}}`.** The
  contract names the key but not the shape. A dict keyed by a `(role,
  endpoint)` tuple would not survive JSON, and this value goes to a CLI and
  very likely to `--json`.
- **`throughput_last_10min` is an integer count of calls finished in the
  window**, not a rate. The name fixes the window, so a per-minute figure
  would just be the same number divided by ten, and a count is what a
  progress line wants.
- **`job_status` adds `job_id` and `status` to the contract's key list.** A
  status report that does not say whether the job is running, paused or
  cancelled forces every caller into a second `get_job`.
- **`cancel_job` and `delete_job` are idempotent.** Cancellation is terminal,
  so a second cancel returns early rather than re-stamping; `tombstone_job_update`
  carries `deleted_at IS NULL` in its WHERE so a second delete does not move
  the tombstone's date and "when was this deleted?" stays answerable.
- **`cancel_job` sets `decided_at` on the items it cancels.** Not stated, but
  it is the column that records when an item reached its final state, and
  `settle_item` sets it for every other terminal status.
- **`delete_job` raises `ValueError` for a job that is not finished.** The
  contract says "only cancelled or done jobs" without naming an error. A
  dedicated error type would have meant editing `errors.py` (and so the
  protocol's install base) for a caller-argument mistake, which is what
  `ValueError` is for. The message says "cancel it first".
- **`find_reusable` orders newest-first and includes tombstoned jobs.** The
  contract says "any job, any user"; it does not say which of several
  candidates wins. The most recent answer is the one a human would expect,
  and `NULLS LAST` on `finished_at` keeps a row answered before that column
  was recorded as the least recent candidate rather than the first.
- **`refuse_prompt_text` walks nested structures, not just top-level dicts.**
  The contract says "any argument value is a dict containing a 'system' or
  'user' key". A `CompletionRequest` nested one level inside a usage dict is
  the same leak, so the scan is recursive (bounded at depth 8).
- **`refuse_prompt_text` and `BLOCKER_BY_ERROR_KIND` live in `queries.py`**
  despite not being SQL. They are dialect-neutral queue RULES that both
  stores must implement identically, and the alternative — `PostgresStore`
  importing them from `sqlite.py` — is how two implementations drift.
- **`_blockers` calls the contract's own `get_endpoint_state` and
  `live_workers`** rather than adding query builders for them. One lookup per
  endpoint is a handful of rows (the endpoints of a job are its roster's), and
  reusing the contract methods keeps the halt and liveness semantics in one
  place instead of two that can disagree.
- **The no-double-claim children sleep 2 ms between batches.** Measured, not
  guessed: without it the split was 193/7 and the claim window was contended
  once. See the Criterion 2 notes.
- **Contention is arranged but not asserted.** How the scheduler splits 200
  calls between two processes is not a property of the store, and asserting a
  particular split (or even "both got some") would make a correct store fail
  intermittently in CI.

## inferred rather than verified

- **The PostgreSQL half of the contract suite still has never run.**
  `PostgresStore` remains a later milestone, so all 190 PostgreSQL
  parametrisations skip with the stated reason `KBU_COUNCIL_TEST_PG_DSN is not
  set` and will turn themselves on when that module lands. Every statement in
  `queries.py` is written to be portable — `sa.JSON`, the CASE in the ORDER
  BY, correlated `EXISTS`, `NULLS LAST`, the quoted `"user"` column — and the
  `for_update=True` path (`FOR UPDATE OF calls SKIP LOCKED`) is **reasoned,
  never executed**. `with_for_update(skip_locked=True, of=calls)` on a
  three-table join compiles on PostgreSQL by SQLAlchemy's documented
  behaviour; nobody has run it. The prompt forbids a test that needs a
  database, and none was reachable from this task.
- **A pending call's "last error" has no contract writer.** `job_status`'s
  `waiting_for_model` and `configuration_mismatch` blockers read
  `calls.error` on a PENDING row, but `release_call`'s contract signature
  takes no `error` argument and `complete_call` moves the row out of
  `pending`. So on this commit nothing in the store can put an error on a
  pending call. I did **not** widen `release_call` — `protocols.py` declares
  that exact signature and the prompt forbids changing it — so the engine
  milestone will need to write `calls.error` itself, inside the same
  transaction as the release (`transaction()` is re-entrant precisely to
  allow that). The two blocker tests set the column directly and say so in
  their docstrings. **This is the one place the store's queue contract is
  not self-sufficient, and it is the engine milestone's to close.**
- **`claim_update()`'s `status == 'pending'` guard is never exercised.** It
  cannot fail under `BEGIN IMMEDIATE`, and `claim_calls` builds its return
  value from the selected rows rather than from the update's rowcount
  (`executemany` reports one total, not per-row). If a future dialect's
  locking lets a row change between the select and the stamp, that guard
  would silently drop the write while `claim_calls` still reported the call
  as claimed. Marked `pragma: no cover` and called out here rather than
  defended against speculatively.
- **`'done'` is a job status nothing sets yet.** `delete_job` accepts it per
  the contract, and the tests reach it by creating a job with
  `status="done"`. Whatever marks a job done is a later milestone's.
- **`reclaim_stale` compares the heartbeat with `>` and `live_workers` with
  `>=`.** That follows the contract's wording for each ("newer than" vs
  "within"), so a worker whose heartbeat is exactly on the cutoff is live for
  `live_workers` and dead for `reclaim_stale`. A one-microsecond edge nobody
  will hit, recorded so it is a decision rather than a discovery.
