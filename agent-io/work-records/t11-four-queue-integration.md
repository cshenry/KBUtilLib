# t11-four-queue-integration

- **task_id**: t11-four-queue-integration
- **branch**: maestro/developer/t11-four-queue-integration
- **commit_shas**:
  - `2b9503b` — test: four-queue acceptance test for council-per-seat-queues-v1 (t11)

## Summary

Added `tests/domains/ai/council/test_four_queue_integration.py`, a new
integration test module that proves the per-seat-queue design (PRD
`council-per-seat-queues-v1`) end to end over the roster shape that actually
failed in production: three chair seats — one fast seat on its own account,
two seats sharing a second account — plus a fourth queue serving the critic
and meta roles on a third account. No production code was changed.

Everything runs through the real, shipped `CouncilWorker` and `SqliteStore`,
with each queue partitioned exactly as `daemon.py`'s per-queue children are
(`seats=`/`roles=`/`concurrency=` on the worker constructor), driven as its
own worker object concurrently over one shared store via `asyncio.gather`.
This is the same claiming/concurrency/review-gating/batching machinery a
real multi-process deployment uses, minus the OS-process boundary itself —
which is already covered end to end, with real spawned children, by
`test_daemon.py::TestQueueMode`. Every backend is the scripted
`InstrumentedBackend`/`FakeBackend` from `engine_support.py`; no network
call, token spend, CLI login or GPU is involved anywhere in the module.

Five test classes:

1. `TestConcurrentProgressAndAccountSafety` — the headline. With the
   shared account's 12-item backlog at the FIFO head and a real 0.1s
   per-call delay, the fast seat's queue finishes all 12 of its calls while
   the shared account's two queues (each capped at its own declared
   `concurrency=1`, summing to the account's real cap of 2) still have
   calls outstanding. A continuous watcher asserts, for every poll during
   the run, that a critic call never exists for an item until all three of
   its chairs have settled (the review-gating regression guard). A shared
   `InFlight` monitor proves the two shared-account queues never together
   exceed 2 calls in flight. Attempts instrumentation (`queued_at`/
   `started_at`) is checked on the shared-account calls that had to wait
   for the single per-queue slot.
2. `TestNonUnanimousVerdictAttribution` — a 3-2 split vote runs the meta
   round to a verdict, with every one of the five resulting calls'
   `endpoint`/`claimed_by` columns checked against the queue that should
   have run it.
3. `TestBatchedReviewQueueProgressesAlongsideChairs` — batching exercised
   at the `CouncilWorker` level only (per the task's explicit instruction,
   since batching is end-to-end disabled in production today). Phase 1
   settles job 1's three chairs with no review worker running yet, so all
   five critic rows exist before the review queue ever claims (avoiding a
   race where the review queue could grab item 0's row alone before items
   1-4 exist). Phase 2 submits a second job and runs the review queue's one
   batched, deliberately slow (0.2s) invocation concurrently with the chair
   queues working job 2; a watcher polling the review backend's in-flight
   counter confirms at least one job-2 chair call reaches `ok` WHILE the
   batched invocation is still in flight. Asserts exactly one backend
   request settled all five critic rows.
4. `TestRestartSafety` — a queue "dies" by calling the worker's own
   `_claim` directly (the same sanctioned technique
   `TestAssignedNeverExceedsCapacity` in `test_engine.py` uses) and then
   simply being abandoned, with nothing ever attempted against the claim —
   deliberately NOT by cancelling a running asyncio task, which would risk
   the worker's own exception handling running where a real `SIGKILL` would
   have allowed none. The other two chair queues are shown to keep making
   progress regardless. `store.reclaim_stale(max_age=timedelta(seconds=0))`
   is called directly to stand in for the real 10-minute wall-clock wait
   every call site in `daemon.py`/`worker.py` would otherwise require (no
   test can afford to wait that out for real). The reclaimed call is shown
   to return to `pending` with `claim_token` cleared and `attempt_count`
   unchanged, and a brand-new replacement worker object then claims and
   completes it (and its never-claimed siblings) to a finished job.
5. `TestSharedAccountConcurrencyIsValidatedStatically` — the static half
   of criterion 2: a real `CouncilConfig` (via `load_council_config`) over
   this module's exact roster is accepted when the two shared-account
   queues' declared concurrencies sum to the account's cap, and refused by
   account name when they don't. Registers `fake` as the APITYPE kind for
   the duration of these two tests only (save/restore, mirroring
   `test_daemon.py`'s own pattern), since `load_council_config` refuses an
   endpoint kind no installed API type answers to.

## Files touched

- `tests/domains/ai/council/test_four_queue_integration.py` (new, 808 lines)

## Success criteria check

Restating the task's six "what to assert" items plus the success criteria:

1. **CONCURRENT PROGRESS (headline)** — PASS. The fast seat's 12 calls all
   reach `ok` while the shared account still has outstanding `pending`/
   `claimed` calls, asserted after a bounded 0.5s window.
   `shared_monitor.peak >= 1` guards against the test passing by vacuous
   non-overlap; `peak <= 2` is the safety side (see item 2).
2. **NO ACCOUNT OVER-COMMITMENT, including the shared one** — PASS
   (runtime) via the `InFlight` peak check in test 1; PASS (static) via
   the config-validation class. The runtime check is capable of failing:
   if either shared-account queue claimed against the account's full
   `max_concurrency` (2) instead of its own declared `concurrency` (1) —
   exactly the pre-fix behaviour `_build_limits`'s docstring describes —
   the combined peak could reach 4, not 2.
3. **REVIEW STAGE GATING** — PASS, checked continuously (every ~10ms)
   during the headline test's run, not just at one end-of-run snapshot.
4. **ITEMS REACH A VERDICT, attributed per queue** — PASS, in
   `TestNonUnanimousVerdictAttribution`.
5. **BATCHING** — PASS, at the `CouncilWorker` level only, exactly as
   instructed. **Daemon-level batching (threading `batch_size`/
   `max_output_tokens_per_item` from `QueueConfig` into the worker) is
   still owed to t13** — `daemon.py` does not wire it yet, so no test
   anywhere can exercise it through the real daemon/queue-config path
   today. This module's docstring and the batching test's class docstring
   both say so explicitly.
6. **RESTART SAFETY** — PASS: claim returns to pending with
   `claim_token` cleared and `attempt_count` unchanged (0 both before and
   after the crash — `attempt_count` only counts retries-after-failure, so
   a call settled on its one real attempt also reads 0; verified this is
   the real semantics, not a bug, by reading `release_call_update`/
   `reclaim_stale_update` in `store/queries.py`), and a replacement worker
   object completes the job.
7. **Instrumentation** (`queued_at`/`started_at`, wait ordering) — PASS,
   checked in test 1 against the shared-account calls.
8. **No test that passed on the base commit fails on the branch** — PASS,
   confirmed by running the full targeted selection (see below): identical
   4 pre-existing failures, 976 passed (970 baseline + 6 new).

## Tests run

Exact command (as specified for this task):

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python \
  -m pytest tests/domains/ai/council -q -p no:cacheprovider --no-header
```

Result: `4 failed, 976 passed in 159.12s`. The 4 failures are exactly the
documented pre-existing, machine-local ones in
`test_types_config.py::TestPackagedDefaultConfig` (dev row 53) — same test
names, same count as the measured baseline (970 passed). No regressions; 6
new tests, all passing.

Also ran the new module alone during development:

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python \
  -m pytest tests/domains/ai/council/test_four_queue_integration.py -q -p no:cacheprovider --no-header
```

Result: `6 passed`.

## Caveats

- **Daemon-level batching is not exercised and cannot be**, per the task
  brief: `daemon.py` does not yet thread `QueueConfig.batch_size`/
  `max_output_tokens_per_item` into the worker it spawns per queue (that is
  phase 13's job). The batching test here monkeypatches
  `VERIFIED_OUTPUT_CAPS` and passes `batch_size`/`max_output_tokens_per_item`
  directly to a `CouncilWorker` constructor, bypassing the daemon entirely,
  exactly as instructed. When t13 lands the daemon wiring, an additional
  daemon-level (real queue-config, real spawned child) batching assertion
  becomes possible and is still owed.
- The module does not spin up real OS processes (no `multiprocessing`,
  no real `SIGKILL`). "Restart safety" is proven at the
  claim/reclaim-contract level (the same contract `daemon.py`'s controller
  relies on for its own post-death `reclaim_stale` call), with the crash
  itself simulated by calling `CouncilWorker._claim` directly and then
  abandoning the object — deliberately chosen over cancelling a running
  asyncio task, which risked the worker's own exception handling running
  where a real kill allows none. Real-process-level restart/supervision
  mechanics (spawn, SIGKILL, controller restart policy) are already covered
  end to end by `test_daemon.py::TestQueueMode`, which this module
  deliberately does not duplicate.
- `store.reclaim_stale(max_age=timedelta(seconds=0))` is called directly
  with an explicit short override rather than through the 10-minute
  default every production call site uses — the only way to exercise the
  real reclaim contract without a test waiting out 10 real minutes. This
  mirrors how `DaemonTimings.scaled(...)` compresses other production
  timing constants elsewhere in the suite.
- One bug was found and fixed during development of this test (in the
  test file itself, not production code): `asyncio.gather(...)` must be
  constructed *inside* a coroutine actually driven by `run()`'s event loop,
  not as a bare argument expression evaluated before `run()` creates its
  loop — the latter raises "the future belongs to a different loop". Fixed
  by adding a small `_run_all(*coros)` helper used throughout the module.
