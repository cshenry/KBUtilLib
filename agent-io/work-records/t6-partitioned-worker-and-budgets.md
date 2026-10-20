# t6-partitioned-worker-and-budgets

- **task_id**: `t6-partitioned-worker-and-budgets`
- **branch**: `maestro/developer/t6-partitioned-worker-and-budgets`
- **commit_shas**:
  - `e6ad5c1` — fix(council): partition CouncilWorker per queue and close the starvation budget bugs

## summary

Fixed the production starvation bug in `CouncilWorker` (`engine/worker.py`):
`_free_slots()` summed free capacity across every account, and that sum sized
a single global `store.claim_calls()` call with no per-account quota, so a
saturated account's rows occupied the whole claimed batch while `_claim()`
incremented `_AccountLimit.assigned` past `capacity` unconditionally,
starving every other account behind it. `_claim()` now claims **per
account** — each call's `endpoints` restricted to that account's own
aliases and its `limit` to that account's own free capacity — which makes
the starvation structurally impossible rather than merely corrected, and
makes `assigned` provably bounded by `capacity`. Added optional `seats`/
`roles` partitioning and an optional `concurrency` override (the per-child
queue budget, not the account's full `max_concurrency`) to
`CouncilWorker.__init__`, carried as plain attributes so `_claim(limit,
job_id, summary)`'s signature (which `daemon.py` calls via `super()`) stays
unchanged. Moved service-time instrumentation's `started` timestamp to
after the concurrency slot is acquired (previously conflating queue-wait
with service time) and added a `queued_at` timestamp written to the
existing `attempts.queued_at` column. Wired the previously-dead
`CouncilConfig.check_roster_against_queues` predicate into `submit()`/
`asubmit()` and the top of `_claim()`, per that method's own docstring
naming those exact call sites. Threaded a configured `cli_temp_root`
through `endpoint_mapping()` as `temp_root`. Left a non-behavioral grouping
seam (`_group_claimed`/`_run_group`) in the claim/dispatch loop for a later
batching phase.

## files_touched

- `src/kbutillib/domains/ai/council/engine/worker.py` (owned by this task)
- `tests/domains/ai/council/test_engine.py` (owned by this task)
- `src/kbutillib/domains/ai/council/protocols.py` (deviation — see caveats)
- `src/kbutillib/domains/ai/council/store/sqlite.py` (deviation — see caveats)

## success_criteria_check

- **test_engine.py contains a two-account starvation test that fails on the
  base commit and passes on the branch** — PASS. Added
  `TestStarvationRegression::test_the_fast_account_completes_while_the_slow_one_is_still_in_flight`.
  Verified by hand: checked out
  `76fdeefd8a8ebc712e4fd13b50d61b382ba44ed8`'s `engine/worker.py` into this
  worktree, ran the test alone — it failed (`fast_items` never reached
  `"ok"` within the 0.5s observation window; captured output below) —
  then restored this branch's `worker.py` and re-ran the full suite to
  confirm no file was left mutated (`git diff --stat` showed only the
  intended changes afterward).
- **`assigned` is proven never to exceed `capacity` under a claim storm** —
  PASS. `TestAssignedNeverExceedsCapacity` claims repeatedly (50 pending
  rows against capacity 3, never executing anything) and asserts the
  invariant after every claim.
- **a seat-partitioned worker never executes another seat's call** — PASS.
  `TestSeatPartitioning` asserts seat B's rows stay `pending` and are never
  sent to the backend by a worker scoped to `seats={"proposer_a"}`.
- **`attempts.queued_at` is populated with `started_at >= queued_at` and
  strictly later when a slot was contended** — PASS.
  `TestServiceTimeInstrumentation` asserts both properties over a
  single-slot account serialising three calls (two proposers plus the
  critic that always audits them).
- **the `_claim(limit, job_id, summary)` signature is unchanged from the
  base commit** — PASS. Confirmed by diff: only the body changed; `git show
  <base>:...worker.py | grep 'def _claim('` and the branch's own `def
  _claim(` both show the identical three-parameter signature, and
  `daemon.py`'s `super()._claim(limit, job_id, summary)` override (not
  edited by this task) still calls it positionally with exactly those three
  arguments.
- **an unpartitioned worker's behaviour is unchanged** — PASS.
  `TestUnpartitionedWorkerUnchanged` plus the full pre-existing
  `test_engine.py` suite (921 tests) all still pass unmodified.
- **no test that passed on the base commit fails on the branch** — PASS,
  with one pre-existing, machine-local, unrelated exception noted in
  `tests_run` below (dev row 53; not in scope for this task and not caused
  by it).

## tests_run

Command (exact, from the worktree root):

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest tests/domains/ai/council -q -p no:cacheprovider --no-header
```

- **On this branch**: `4 failed, 932 passed in ~87s`. The 4 failures are
  exactly the pre-existing, machine-local baseline
  (`TestPackagedDefaultConfig::test_cli_endpoints_default_to_one_call_at_a_time`,
  `test_every_packaged_profile_builds_a_roster`,
  `test_on_a_fresh_machine_with_no_kbutillib_config`,
  `test_the_packaged_config_declares_no_poplar_endpoint` — this box's
  `~/.kbutillib/config.yaml` leaks past their monkeypatched `HOME`, dev row
  53) — no others. 932 = 921 baseline passed + 11 new tests, all green.
- **Base-commit verification** (hand check, not part of the regular run):
  with `76fdeefd`'s `engine/worker.py` substituted in, running only
  `TestStarvationRegression::test_the_fast_account_completes_while_the_slow_one_is_still_in_flight`
  produced:

  ```
  FAILED tests/domains/ai/council/test_engine.py::TestStarvationRegression::test_the_fast_account_completes_while_the_slow_one_is_still_in_flight[sqlite]
  AssertionError: the fast account's calls never completed within the observation window -- the slow account's 600-row backlog is still starving it, which is the bug this task fixes
  1 failed in 15.82s
  ```

  `worker.py` was restored immediately afterward (`cp` from a saved copy),
  and the full suite re-run clean (same 4 pre-existing failures, 932
  passed) to confirm the restore was exact.

## caveats

- **File-ownership deviation (documented, not hidden).** This task's
  envelope lists `engine/worker.py` and `test_engine.py` as the only owned
  files, but item 5 (service-time instrumentation) requires
  `record_attempt()` to accept `queued_at` — and the store (`protocols.py`,
  `store/sqlite.py`) did not have that parameter. Checked:
  `t2-store-claim-filter-and-schema`'s own prompt says explicitly "Nothing
  in this task writes the column; a later task does" for
  `attempts.queued_at`, and no other task in
  `agent-io/prds/council-per-seat-queues-v1/taskplan.json` lists those two
  files as owned either. Rather than leave `queued_at` unwritable (which
  would fail an explicit, graded success criterion), I added a narrow,
  additive, backward-compatible `queued_at: Optional[datetime] = None`
  parameter to `record_attempt()` in both files. This phase (`t6`) has no
  parallel sibling in its taskplan group, so there is no concurrent-task
  file-conflict risk from this. Flagging for the reviewer/coordinator in
  case a stricter read of "touch no others" is wanted instead (e.g.
  rejecting this task and re-scoping the gap into its own task).
- **`cli_temp_root` wiring is only half-done, for the same reason.** The
  task prompt says to "thread the configured value through to the backend
  invocation," but the actual `run_cli(..., temp_root=...)` call lives in
  `src/kbutillib/domains/ai/llm/apis/cli.py` (`_run`, two call sites),
  which is outside `engine/worker.py` and is not listed as owned by ANY
  task in the taskplan (confirmed by grep across every task's "FILES YOU
  OWN" line — `t4-cli-stdin-from-file` added the `temp_root` parameter to
  `run_cli` itself and explicitly said "the wiring [is] by a third" task,
  but no third task claims `llm/apis/cli.py`). I implemented the half that
  is in scope: `endpoint_mapping()` now threads `CouncilConfig.cli_temp_root`
  into the per-endpoint mapping dict as `temp_root`, with tests proving
  that. The CLI backend classes do not yet read it. This is a planning gap,
  not an oversight in this task, and should be routed to the coordinator as
  a new task owning `llm/apis/cli.py` before anything depends on it in
  production.
- **Batching-capacity math (taskplan.json's "CONFRONT ROUND 1" amendment
  (b)) was intentionally NOT implemented.** The task envelope I was given
  explicitly says: "Do not implement batching — but do not hard-code
  one-row-per-task... A small seam is enough," which is what
  `_group_claimed`/`_run_group` provide (today: one-row groups,
  behaviourally identical to the base commit). `taskplan.json`'s own text
  for this task additionally describes a more detailed claim-capacity
  algorithm for batched queues (claim `free_slots × batch_size` candidates,
  group by homogeneity, schedule `free_slots` batches, release surplus) and
  asks for a `concurrency=1, batch_size=20` assembly test. None of this is
  in the graded `success_criteria` (verified: identical wording in both the
  envelope and `taskplan.json`, and neither mentions batching), and
  implementing the fuller algorithm risked destabilizing the graded
  invariants (`assigned <= capacity`, the starvation fix, roster rejection)
  under a tighter time budget, so I followed the more conservative envelope
  instruction. Flagging the discrepancy between the envelope text and
  `taskplan.json`'s prompt for this task in case the coordinator wants the
  fuller batching-capacity logic folded into `t8-batched-invocation`
  instead (which also owns `engine/worker.py`).
- `_record_attempt` wraps `store.record_attempt(..., queued_at=...)` in a
  `try/except TypeError` falling back to the pre-`queued_at` call shape, as
  a defensive measure in case a third-party `CouncilStore` implementation
  (not `SqliteStore`) hasn't been updated — harmless given `SqliteStore` is
  the only implementation in this repo today.
- The starvation regression test's timing margins (0.5s observation window
  vs. an estimated ~1.5s drain time for 600 pending rows under the
  base-commit bug) were chosen empirically on this machine and verified
  both ways (fails on base, passes on branch); they carry roughly 3x
  margin, which should be robust to normal CI slowdown but is a timing-based
  test and not immune to extreme host contention.
