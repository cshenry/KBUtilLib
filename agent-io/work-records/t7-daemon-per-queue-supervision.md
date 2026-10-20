# t7-daemon-per-queue-supervision

- **task_id:** `t7-daemon-per-queue-supervision`
- **branch:** `maestro/developer/t7-daemon-per-queue-supervision`
- **commit_shas:**
  - `847bcdd` feat: per-queue daemon supervision via a persistent controller

## summary

`CouncilDaemon` now supervises one worker process per configured queue
instead of one worker for the whole store, while preserving every
pre-queue-mode behaviour unchanged when `queues` is empty. A detached
**controller** (holding the `controller` execlock kind that t1 already
built) stays alive after `start()` returns and launches `workers`
children per queue via `multiprocessing` (spawn), each taking its own
`(queue, index)` partition lock and constructing a `_DaemonWorker`
scoped to that queue's `seats`/`roles`/`concurrency` (threading the
existing `CouncilWorker(seats=, roles=, concurrency=)` parameters from
t6 through `_worker_for`). Each child reuses `_serve`/`_shutdown`/
`_interrupt_claimed` **completely unmodified** — the only new state is
`_scope_seats`/`_scope_roles`/`_scope_concurrency`, read by
`_worker_for`, `_pending_calls` and `_profile_routes` (all `None`,
i.e. unscoped, for every caller that existed before this change).

The controller launches every configured queue **all-or-nothing**: if
any child fails to take its partition lock within `start_wait_s`,
every child already started by that operation is drained and stopped
and the controller raises. Thereafter it supervises on a
`supervise_poll_s` tick: an **unexpected** child death is restarted
after `restart_delay_s`, indefinitely, with an immediate
`reclaim_stale()` so the dead child's orphaned claims are not stuck
behind the full ten-minute lease; a **deliberate** per-queue stop
writes a marker file (`queue-<name>.stopped`) that the tick honours
and never auto-restarts behind, until an explicit per-queue `start`
clears it. `status()` now reports, per queue, each child's pid/alive
state, the stop marker, pending/claimed(=in_flight) counts over that
queue's seats, and the latest matching `workers` row's heartbeat.

`stop()`/`start()` both gained an optional `queue=` parameter: named,
they operate on one queue only (via the queue's own partition lock
files, found by duplicating the existing probe-by-flock-trial
technique locally since `execlock.py` exposes no public per-partition
probe); unnamed, they behave exactly as today (`stop()`) or launch
the whole controller (`start()`).

## files_touched

- `src/kbutillib/domains/ai/council/daemon.py`
- `tests/domains/ai/council/test_daemon.py`

## success_criteria_check

Quoting the task's own `success_criteria` string, one clause at a time:

1. **"A four-queue config starts four children each carrying its queue
   name"** — PASS. `test_a_four_queue_config_starts_four_children_each_in_its_own_queue`
   spawns a real controller over a real four-queue config and asserts
   `status()["queues"]` has exactly `q1..q4`, each with one alive child
   whose `status()` record carries `"queue"` equal to its own queue
   name, and that the four children are four *distinct* OS pids.
   `test_each_queue_childs_launch_args_carry_its_queue_name_and_index`
   additionally asserts the literal `(config_source, store_url, queue,
   index, timings)` tuple `_launch_queue_child` hands to
   `multiprocessing.Process` carries the queue name and index directly
   (see **caveat on "argv"** below for why this is a tuple of launch
   args rather than literal OS `argv`).
2. **"a queue with workers=2 starts two children holding distinct
   partition locks"** — PASS.
   `test_workers_two_on_a_queue_starts_two_children_with_distinct_partition_locks`
   spawns both workers directly, confirms their partition-lock holder
   pids differ and match the two real processes, and confirms a third
   attempt at the same `(queue, index)` is refused with `LockHeld`
   while one of them is alive.
3. **"status() reports per-queue claimed/in-flight/pending and
   distinguishes a dead child from an idle queue"** — PASS, with one
   documented judgement call: `claimed` and `in_flight` are reported as
   the **same number** (see Caveats — the store has no status distinct
   from `claimed` for "an attempt is actively running").
   `test_status_distinguishes_pending_work_behind_a_dead_child_from_an_idle_queue`
   kills a chairs-queue worker mid-flight (no graceful drain) while a
   review queue has never been touched, and asserts the two queues'
   `pending`/`claimed` counts differ in the way an operator would need.
4. **"stopping one queue leaves siblings claiming"** — PASS, folded
   into `test_stopping_one_queue_leaves_siblings_running_and_restart_policy_holds`,
   which also covers the unexpected-death-restarts /
   deliberate-stop-does-not-restart halves of judgement call (b).
5. **"a still-claimed call at the drain deadline returns to pending
   with attempt_count unchanged from before the stop"** — PASS.
   `test_the_bounded_drain_contract_holds_for_a_queue_worker` is the
   same assertion `TestTheStopDeadline` already made for the
   unpartitioned daemon, run through a queue worker instead — proving
   the inherited `_serve`/`_shutdown`/`_interrupt_claimed` path is
   untouched for a queue child.
6. **"a config with no `queues` starts one child over all seats as on
   the base commit"** — PASS by construction and by regression: every
   one of the 26 pre-existing `test_daemon.py` tests (which exercise
   exactly this path) still passes unchanged, and
   `test_a_config_with_no_queues_keeps_the_base_status_shape` adds an
   explicit check that `status()` carries no `"queues"`/`"controller"`
   key at all in this mode.
7. **"no test that passed on the base commit fails on the branch"** —
   PASS. See Tests Run below: the full `tests/domains/ai/council`
   subtree is 941 passed / 4 failed, and the 4 failures are the exact
   pre-existing, machine-local `TestPackagedDefaultConfig` failures
   named in the baseline (932 passed/4 failed at the base commit) —
   932 + 9 new = 941, zero new failures.

Additional confront-round-1 amendments, graded informally (not in the
literal `success_criteria` string, but in the prompt body):

- **(a) one detached controller stays alive** — PASS, built as
  described above (`run_foreground`'s controller branch,
  `_serve_controller`).
- **(b) restart policy, both ways** — PASS (see criterion 4's test).
- **(c) all-or-nothing initial start** — PASS.
  `test_a_partial_start_failure_drains_and_stops_every_child_from_that_operation`
  forces one queue's child to exit immediately (a test-injected
  `queue_child_target`, not a real misconfiguration — see Caveats) and
  asserts every other queue's partition lock and the controller lock
  are all free afterward.
- **(d) foreground mode runs the same controller** — PASS by
  construction: `run_foreground()` is the one entry point for all three
  shapes (legacy/controller/queue-worker); `start(foreground=True)`
  still just calls it directly, unchanged.
- **(e) controller holds an exclusive controller lock; standalone run
  in queue mode refused; config immutable while running** — PARTIAL,
  see Caveats: the "refused while the controller is holding anything"
  half is proven directly
  (`test_a_standalone_run_is_refused_while_the_controller_holds_the_lock`,
  using the shared-vs-exclusive lock semantics t1 already shipped), and
  "config immutable while running" holds by construction (there is no
  reconfigure verb on a live controller — only stop-all then start-all).
  The stronger reading — refuse a standalone `run` purely because the
  *loaded configuration* declares `queues`, even with no controller
  alive at all — is **not implemented**, because enforcing it requires
  a change inside `CouncilUtils.run()` in `council_utils.py`, a file
  outside this task's "touch no other files" scope.
- **(f) child argv carries queue name and worker index** — PASS in
  spirit, see Caveats for the "argv" framing.

## tests_run

- `TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest tests/domains/ai/council/test_daemon.py -q -p no:cacheprovider --no-header`
  → **35 passed** (26 pre-existing + 9 new), ~62s.
- `TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest tests/domains/ai/council -q -p no:cacheprovider --no-header` (the full baseline-measured targeted selection)
  → **941 passed, 4 failed**, ~99s. The 4 failures are exactly
  `test_types_config.py::TestPackagedDefaultConfig::{test_every_packaged_profile_builds_a_roster,
  test_the_packaged_config_declares_no_poplar_endpoint,
  test_on_a_fresh_machine_with_no_kbutillib_config,
  test_cli_endpoints_default_to_one_call_at_a_time}` — the pre-existing,
  machine-local failures named in the envelope's baseline
  (932 passed/4 failed at the base commit; 932 + 9 new = 941). No test
  that passed at the base commit fails on this branch.
- Did not run the full repo suite (measured 3416s at base, 5.7x the
  single-call ceiling); the conductor owns that per the envelope.

## caveats

- **"Argv" is a `multiprocessing.Process(args=...)` tuple, not literal
  OS argv.** The task prompt says the controller "extends
  `_default_child_argv` with the queue name and the worker index", which
  reads as spawning each queue child through another
  `subprocess.Popen` of the shipped `kbu council daemon start
  --foreground` CLI. I did not build it that way, for a concrete
  reason: that CLI's `daemon_start_cmd` (in
  `src/kbutillib/interfaces/cli/council.py`, a file outside this
  task's ownership) takes no queue-selector arguments and uses a plain
  `click` parser with no extra-args passthrough configured, so a real
  invocation with two trailing positional tokens would be rejected by
  `click` before any of this task's code ever ran. No task in this
  PRD's taskplan (`t1`..`t11`) wires that CLI surface. Rather than ship
  code that only works if a future, unspecified change lands elsewhere,
  the controller spawns each queue worker via `multiprocessing` (spawn
  context) directly, targeting the new module-level
  `_serve_queue_child`, which rebuilds the council facade from the SAME
  `council_config_source`/`store_url_override` the top-level detached
  spawn already carries and then calls `CouncilDaemon.run_foreground()`
  on an object constructed with `queue=`/`worker_index=` set. This
  still gives full OS-process isolation (the architectural property
  Q1 and the module docstring both call load-bearing — a separate
  *process*, not a thread or a task group, so one busy event loop can
  never block another queue's), it is fully exercised by real
  `multiprocessing` children in the tests (nothing here is mocked),
  and it requires no change to any file this task does not own. The
  `(config_source, store_url_override, queue, index, timings)` tuple
  plays the role "argv" would have: it is what each child is launched
  with, it is inspectable (`_QueueChild.args`), and it is asserted on
  directly in `test_each_queue_childs_launch_args_carry_its_queue_name_and_index`.
  If a future task wires `kbu council daemon start --queue <name> <index>`
  through the CLI, `_default_child_argv`/`_launch_queue_child` would
  need a literal-argv variant added at that point — flagged here rather
  than guessed at.
- **"Standalone run in queue mode is refused" is proven only for "while
  a controller is up".** `council_utils.py`'s `run()` calls
  `execution_lock().acquire("run")` unconditionally and un-partitioned,
  with no awareness of `config().queues` at all; that is the one place
  an unconditional refusal (purely from the *configuration* declaring
  queues, independent of whether a controller happens to be alive)
  would have to be added, and it is outside `daemon.py`/`test_daemon.py`.
  What I *can* and did prove: the controller's SHARED store-wide hold
  already conflicts with `run()`'s EXCLUSIVE one via the shared/exclusive
  lock semantics t1 shipped, so a standalone run is refused in exactly
  the same shape a lone `daemon` already refuses it today. The
  narrower gap this leaves (no controller running yet, but the config
  already declares `queues`, and someone runs `kbu council run`
  un-partitioned against the full account cap) is a real one Q1 names;
  closing it needs a one-line guard in `council_utils.run()`, which I
  am flagging rather than silently leaving unflagged.
- **`claimed` and `in_flight` are the same number in `status()`.** The
  call store has no status distinct from `claimed` for "an attempt is
  actively in flight" versus "claimed but momentarily idle between
  probe passes" — only `attempts` rows, which are per *try*, not per
  outstanding call. Both keys are populated with the one number the
  store can actually support rather than inventing a second one.
- **Reclaim-on-death (`_safely_reclaim`) is exercised indirectly, not
  asserted on directly.** The restart test confirms a dead queue
  worker's slot is restarted and the siblings are unaffected; it does
  not independently assert that `reclaim_stale()` ran before the
  restart (that would require a stuck claim held long enough to
  observe the reclaim window, adding real wall-clock time to an
  already-heavy test file).
- **Per-partition and controller lock probing duplicates a few lines of
  `fcntl`-trial logic that already exists in `execlock.py`** (`probe()`'s
  technique), because `execlock.py` exposes a public probe only for the
  store-wide lock, not for an individual partition or controller file,
  and that module is outside this task's ownership. Documented in the
  new `_probe_lock_file` docstring rather than silently duplicated.
