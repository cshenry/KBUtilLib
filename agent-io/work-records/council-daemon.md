# Work Record — council-daemon

## task_id

council-daemon (M2.4)

## branch

`maestro/developer/background-reference-only-agent--council-daemon`

## base

KBUtilLib `main` **44fb8b4** ("docs(work-record): record the servable-seats +
query-dirs milestone") — M1 plus M2.1 (llm-call-wrapper), M2.2
(local-only-cleanup) and M2.3 (servable-seats-query-dirs).

## commit_shas

- `a745668` — feat(council): the per-machine daemon, holding the execution lock
- `c6262da` — test(council): the daemon, against real processes and a real store
- `2fa526f` — test(cli): the three kbu council daemon verbs and their envelopes
- `(this commit)` — this work record

## summary

The per-machine council daemon: one long-running `CouncilWorker` over one
machine's store, detached from any terminal, working EVERY job in that store
rather than one. It holds `council/execlock.py`'s execution lock for its whole
life (kind `daemon`), and `kbu council run` (kind `run`) and `evaluate()`
(kind `evaluate`) now hold the same lock for theirs — so each of the three
refuses while another holds it, naming the holder's pid and kind. Readiness is
per `(endpoint, served model)`, re-probed at start, every 60 s and when a halt
is cleared. At the 120 s stop deadline unfinished calls are interrupted,
recorded and returned to pending without spending a retry, before the lock is
released.

---

## 1. What landed, and where

### `src/kbutillib/domains/ai/council/daemon.py` (NEW, 1356 lines)

| Symbol | What it is |
|---|---|
| `DaemonTimings` (`daemon.py:119`) | Every clock in one frozen record: `heartbeat_s=30`, `probe_s=60`, `idle_poll_s=10`, `drain_s=120`, `stop_wait_s=130`, `start_wait_s=30`. `DEFAULT_TIMINGS` is the specified production set; `.scaled(**overrides)` is how a test shortens one. |
| `CouncilDaemon.start()` (`daemon.py:411`) | Detached by default: one `subprocess.Popen` with `start_new_session=True`, stdin `DEVNULL`, stdout+stderr **appended** to `daemon.log` in the council directory, cwd the council directory. Then it WAITS until that child is the lock's holder, or reports why not (with the log tail). `--foreground` delegates to `run_foreground()`. |
| `CouncilDaemon.stop()` (`daemon.py:524`) | Reads the holder from the lock file; `SIGTERM`s it only if its recorded kind is `daemon`; waits up to `stop_wait_s` for the lock to free; reaps it if it is this process's own child. |
| `CouncilDaemon.status()` (`daemon.py:369`) | The lock holder (pid, kind, start time, store path) plus this store's `workers` rows, each `live` (heartbeat under 2 min) or `stale`. Reads only — never creates the directory. |
| `CouncilDaemon.run_foreground()` (`daemon.py:593`) | Installs the signal handlers, takes the lock, `chdir`s to the council directory, runs the loop on a loop it owns. |
| `CouncilDaemon._serve()` (`daemon.py:625`) | The loop: resume check → readiness probe (on the probe clock, or at once if a halt was cleared) → `CouncilWorker.work(until_idle=True)` bounded by the stop deadline → heartbeat → wait `idle_poll_s` or for the stop. |
| `CouncilDaemon._interrupt_claimed()` (`daemon.py:779`) | Per still-claimed call, in ONE transaction: an `interrupted` attempt (usage kept when known, `{"unknown": true}` when not), `budget_usage_unknown` on a budgeted endpoint whose usage is unknown, and `release_call(..., increment_attempt=False)`. |
| `_DaemonWorker` (`daemon.py:216`) | `CouncilWorker` with one added filter in `_claim`: a claimed call whose own ROUTE is not runnable goes straight back (`increment_attempt=False`, `not_before` = the probe interval). The queue can only filter by endpoint; readiness is per route. |
| `_Route` (`daemon.py:162`) | The subset of the seat interface `servable.seat_state` reads, so one readiness implementation serves both call rows and roster seats. |

### Existing files changed

| File:line | Change |
|---|---|
| `council_utils.py:368` | `execution_lock()` — the one place the lock is built. `db_migrate` (`:341`) now goes through it instead of inlining it. |
| `council_utils.py:385` | `daemon(**kwargs)` — the facade's factory, so the CLI does not import the daemon module and the daemon does not import the facade. |
| `council_utils.py:398` | `endpoint_api(endpoint, kind="")` — one api type per ALIAS (`build_backends` answers the same question per roster). `None` for an endpoint this installation cannot build. |
| `council_utils.py:919` | `run_async` holds the lock, kind `run`, for the worker's whole lifetime. |
| `council_utils.py:959` | `evaluate_async` holds it, kind `evaluate`. |
| `council/execlock.py:252` | `held_message(wanted, holder=None)` — public, so `start`'s probe-before-spawn raises the SAME wording `acquire` does. |
| `council/store/sqlite.py:1287` | `next_uncharged_attempt_no(call_id)` — a free NEGATIVE attempt number. Not in the `CouncilStore` protocol (see §4). |
| `llm/__init__.py:422` | `kill_cli_children()` — published on the ONE public wrapper, so no council module reaches into `llm.apis`. |
| `llm/apis/cli_common.py:649` | `_LIVE_CHILDREN` + `live_cli_children()` + `kill_cli_children()`; `run_cli` registers its child and **kills its process group on `CancelledError`**. |
| `interfaces/cli/council.py:688` | `kbu council daemon start [--foreground] | stop | status`, plus the rendering of their shared envelope. The bare group is `status`. |

Untouched, as required: `src/kbutillib/domains/ai/argo_utils.py`,
`src/kbutillib/domains/ai/ai_curation_utils.py`.

---

## 2. The round-3 additions, point by point

**One lock, three holders, each refusing the others.** Not a second lock:
`council/execlock.py` is used as shipped. `CouncilUtils.execution_lock()` is
the one builder and all four kinds go through it.

```
$ git grep -n 'acquire("' -- src/kbutillib
src/kbutillib/domains/ai/council/daemon.py:615:            with self.lock.acquire("daemon") as record:
src/kbutillib/domains/ai/council/execlock.py:26:    with lock.acquire("run"):          # LockHeld if somebody else has it
src/kbutillib/domains/ai/council_utils.py:341:        with self.execution_lock().acquire("migrate"):
src/kbutillib/domains/ai/council_utils.py:919:            with self.execution_lock().acquire("run"):
src/kbutillib/domains/ai/council_utils.py:959:            with self.execution_lock().acquire("evaluate"):
```

(`execlock.py:26` is the module docstring's usage example, not a call site.)

**`daemon stop` signals only a daemon holder.** `daemon.py:545` refuses with a
`DaemonError` naming the holder's kind for `run`, `evaluate` and `migrate`.

**Readiness per (endpoint, served model).** `_refresh_readiness`
(`daemon.py:992`) probes each DISTINCT endpoint once (five seats on one gateway
cost one free `/models` GET) and reads one `seat_state` report per route off
it. Re-probed:

- at start (`last_probe = 0.0`, so the first pass probes);
- every `probe_s` (60 s in production);
- immediately when a halt this daemon had recorded is gone — `_halt_cleared`
  (`daemon.py:1099`). The daemon and `kbu council endpoints reset-halt` are two
  processes with no channel between them (a store is a file, not a server), so
  "immediately after reset-halt" is the daemon noticing on its next pass:
  seconds, not up to a probe interval;
- a route a NEW job introduces is admitted without waiting for a probe
  (`_admits` returns True for a route with no report yet, `daemon.py:938`) —
  the worker's own per-call gate then decides it.

Routes come from the queue (every live pending/claimed call) **plus** the
configured profiles' seats (`_routes`, `daemon.py:1165`).

**A route unavailable at start becomes eligible when its probe succeeds, and
`job_status` shows its latest probe failure as the blocker.**
`_record_route_blockers` (`daemon.py:1050`) writes the probe's own detail onto
each waiting call as a `model_unavailable` error — which
`store.queries.BLOCKER_BY_ERROR_KIND` maps to the `waiting_for_model` blocker,
the only channel the store has for "why is this not moving". It is CLEARED
again the moment a probe succeeds.

**Ready runs; unknown runs only for a job carrying `allow_unverified`.** Two
layers, deliberately:

- the daemon's own `_admits` (`daemon.py:938`) reads the
  options of each call's OWN job (`_job_options`, `daemon.py:970`), so one
  job's permission can never admit another's;
- the worker's existing `_permission_gate` decides the billing-unknown case
  per call from the same place.

Both are tested (§3, D).

**The stop deadline.** `_work_once` (`daemon.py:697`): the stop event makes the
worker stop claiming and gather what is in flight, so `work()` returns of its
own accord once the last call answers. The deadline bounds THAT: at `drain_s`
the pass is cancelled (which propagates into the api types' awaits), every CLI
child's process GROUP is killed and reaped, and then — and only then —
`_shutdown` (`daemon.py:762`) interrupts what is still claimed, removes the
`workers` row and lets the lock go. In that order, because `job_status` reports
`no_daemon` for an endpoint with pending calls and no live worker: removing the
row first would make every call this daemon held look abandoned for the length
of the interrupt pass.

**Resume from suspension.** `_tick_clock` (`daemon.py:1238`) measures the wall
clock against the monotonic clock: on Linux `CLOCK_MONOTONIC` does not run
while the machine is suspended and `CLOCK_REALTIME` does, so the difference
between the two deltas is the time spent asleep. A gap over `2 × heartbeat_s`
runs `store.reclaim_stale()` **before** the next heartbeat. Comparing wall-clock
deltas alone would read a single long busy pass — a twenty-minute CLI call — as
a resume.

---

## 3. Tests — `tests/domains/ai/council/test_daemon.py` (26) and `tests/cli/test_council_cli.py::TestDaemonVerbs` (8)

Command and result:

```
$ python -m pytest tests/domains/ai/council/test_daemon.py \
    "tests/cli/test_council_cli.py::TestDaemonVerbs" -p no:cacheprovider --durations=0 -q
34 passed in 12.11s
```

Exit code 0. Per-test wall time, from that run's `--durations=0` (slowest
first; **the slowest is 2.33 s and none exceeds 30 s**):

| s | test |
|---|---|
| 2.33 | `TestTheLoop::test_sigterm_lets_an_in_flight_call_finish_and_claims_nothing_new` |
| 2.23 | `TestTheStopDeadline::test_a_hung_call_at_the_deadline_is_interrupted_and_costs_no_retry` |
| 1.14 | `TestTheLockIsTheSingleton::test_a_new_start_succeeds_once_the_holder_is_killed` |
| 0.97 | `TestTheLoop::test_the_foreground_loop_completes_a_job_and_heartbeats` |
| 0.90 | `TestTheLockIsTheSingleton::test_stop_signals_the_pid_in_the_lock_file` |
| 0.78 | `TestPerJobPermissions::test_one_jobs_allow_unverified_admits_only_its_own_unknown_calls` |
| 0.68 | `TestTheLoop::test_the_cwd_moves_to_the_council_directory` |
| 0.54 | `TestTheLockIsTheSingleton::test_a_second_start_is_refused_naming_the_holders_pid` |
| 0.28 | `TestResumeFromSuspension::test_the_loop_reclaims_stale_claims_before_the_next_heartbeat` |
| 0.11 | `TestTheStopDeadline::test_an_interrupted_call_can_be_retried_without_an_attempt_collision` |
| 0.11 | `TestWhatIsShipped::test_a_child_that_dies_before_taking_the_lock_is_reported_with_its_log` |
| 0.09 | `TestReadiness::test_a_route_that_fails_its_first_probe_becomes_eligible_when_it_succeeds` |
| 0.07 | `TestWhatIsShipped::test_the_child_argv_is_this_interpreter_and_this_configuration` |
| 0.06 | `TestTheLockIsTheSingleton::test_run_is_refused_while_the_daemon_holds_the_lock` |
| 0.05 | `TestTheLockIsTheSingleton::test_stop_refuses_to_signal_a_holder_that_is_not_a_daemon` |
| 0.05 | `TestPerJobPermissions::test_an_unknown_route_is_admitted_for_a_permitted_job_only` |
| 0.05 | `TestReadiness::test_a_cleared_halt_forces_a_probe_rather_than_waiting_for_the_interval` |
| 0.04 | `TestTheLockIsTheSingleton::test_evaluate_is_refused_while_the_daemon_holds_the_lock` |
| 0.04 | `TestTheLockIsTheSingleton::test_the_daemon_is_refused_while_a_run_holds_the_lock` |
| 0.04 | `TestWhatIsShipped::test_a_detached_start_refuses_an_in_memory_configuration` |
| 0.04 | `TestReadiness::test_only_endpoints_the_doctor_reports_ready_are_served` |
| 0.04 | `TestReadiness::test_readiness_is_per_endpoint_and_model_not_per_endpoint` |
| 0.04 | `TestTheLockIsTheSingleton::test_stop_says_so_when_nothing_is_running` |
| 0.04 | `TestReadiness::test_a_route_with_no_report_yet_is_admitted_rather_than_stalled` |
| 0.03 | `TestResumeFromSuspension::test_a_wall_clock_gap_is_detected_as_a_resume` |
| 0.02 | `cli::TestDaemonVerbs::test_status_names_the_holder_and_its_worker_row` |
| 0.02 | `cli::TestDaemonVerbs::test_start_refuses_while_the_lock_is_held_and_names_the_holder` |
| 0.02 | `cli::TestDaemonVerbs::test_the_human_rendering_is_a_rendering_of_the_same_dict` |
| 0.02 | `cli::TestDaemonVerbs::test_the_bare_group_is_status` |
| 0.02 | `cli::TestDaemonVerbs::test_status_reports_a_free_lock_and_no_workers` |
| 0.01 | `cli::TestDaemonVerbs::test_stop_reports_that_nothing_is_running` |
| 0.01 | `cli::TestDaemonVerbs::test_stop_refuses_a_holder_that_is_not_a_daemon` |
| 0.01 | `cli::TestDaemonVerbs::test_schemaless_store_sends_the_user_to_db_init` |

### What is real, and what is substituted (the no-injection check)

- **The lock tests use the real `execlock.py` against a real file with a REAL
  subprocess holder.** `hold_the_lock` (`test_daemon.py:448`) is a module-level
  function run under `multiprocessing.get_context("spawn")`, i.e. a brand-new
  interpreter that shares nothing with pytest but the directory path. Nothing
  about exclusion is simulated, and `test_a_new_start_succeeds_once_the_holder_is_killed`
  then calls the real `start()`, which spawns the SHIPPED argv — a real
  detached `kbu council daemon start --foreground` child.
- **The stop-deadline test runs a real `CouncilWorker` with a hung
  test-registered fake api type, and reads the result back out of SQLite.**
  `serve_the_store` (`test_daemon.py:465`) runs `daemon.run_foreground()` in a
  spawned child; the child is sent a real `SIGTERM`; the `interrupted` attempt,
  the unchanged `attempt_count` and the cleared `claim_token` are read with
  `sqlite3.connect` (`_rows`, `test_daemon.py:378`), not from any object the
  test still holds.
- **The one substitute is the api type**: a plug-in `kind` registered with
  `llm.register_api_type` whose readiness shape (`models`/`preflight` present
  or absent, a gate FILE that must exist before it lists anything) and answer
  delay (`delay`, `hang`) each test chooses through the endpoint's `options`
  bag. That is the sanctioned substitution — `fake` is deliberately left
  unregistered by the wrapper so no shipped configuration can name it.
- Nothing patches the queue, the claim, the worker's permission gate or the
  attempt bookkeeping.

### The required cases, mapped

| Required | Test |
|---|---|
| a second start is refused by the lock, naming the pid | `test_a_second_start_is_refused_naming_the_holders_pid` |
| after the holder is killed a new start succeeds | `test_a_new_start_succeeds_once_the_holder_is_killed` |
| the foreground loop completes a job and heartbeats | `test_the_foreground_loop_completes_a_job_and_heartbeats` |
| SIGTERM lets an in-flight slow fake call finish and claims nothing new | `test_sigterm_lets_an_in_flight_call_finish_and_claims_nothing_new` |
| stop signals the pid in the lock file | `test_stop_signals_the_pid_in_the_lock_file` |
| run is refused while the lock is held | `test_run_is_refused_while_the_daemon_holds_the_lock` (and `..._evaluate_...`) |
| only endpoints the (faked) doctor reports ready are served | `test_only_endpoints_the_doctor_reports_ready_are_served` |
| the cwd moves to the council directory | `test_the_cwd_moves_to_the_council_directory` |
| run vs daemon mutual refusal BOTH ways | `test_run_is_refused_while_the_daemon_holds_the_lock` + `test_the_daemon_is_refused_while_a_run_holds_the_lock` |
| a route failing its first fake probe becomes eligible when it succeeds | `test_a_route_that_fails_its_first_probe_becomes_eligible_when_it_succeeds` |
| a hung fake call at the deadline ends pending, retry count unchanged, `interrupted` attempt | `test_a_hung_call_at_the_deadline_is_interrupted_and_costs_no_retry` |
| one job's allow_unverified admits only its own unknown calls | `test_one_jobs_allow_unverified_admits_only_its_own_unknown_calls` + `test_an_unknown_route_is_admitted_for_a_permitted_job_only` |

Plus: per-(endpoint,model) granularity, a cleared halt forcing a probe, the
resume detector and its ordering, `stop` refusing a non-daemon holder, `stop`
on an idle store, a child that dies before taking the lock, the shipped argv,
the shipped clocks, and the eight CLI-envelope tests.

No test makes a network call, spends a token, needs a CLI login or needs a GPU.
Every spawned child is killed in teardown (`children` and `detached` fixtures,
`test_daemon.py:499`/`:527`) — a leaked daemon would hold the lock for the rest
of the session. Every test runs under a temp `HOME` (`temp_home`, autouse) and
a temp store, so `~/.kbutillib` on the host is never touched.

---

## 4. Four problems the tests found, and what was done

1. **A `SIGTERM` in the window between taking the lock and installing the
   handlers got the default action — death with the lock held and calls
   claimed.** `test_the_cwd_moves_to_the_council_directory` hit it: the child
   exited `-15`. The handlers now go on in `run_foreground` BEFORE the lock
   (`daemon.py:593`), writing to a `threading.Event` that `_serve` adopts when
   it starts — so a signal that arrived early is honoured rather than lost.

2. **An uncharged `interrupted` attempt collided with the call's next real
   try.** `attempts` is keyed `(call_id, attempt_no)` and the engine derives
   that number from `calls.attempt_count`, which an interrupt deliberately does
   not move — so `attempt_count + 1` is still the number the next try will use,
   and the duplicate key failed that try's settle transaction (observed as a
   SQLAlchemy `IntegrityError` inserting `(…, 1, …, 'ok', …)`), losing the
   answer the call eventually gave. Uncharged attempts are now filed BELOW
   zero: `SqliteStore.next_uncharged_attempt_no` hands out −1, −2, …, the
   engine's sequence stays 1, 2, 3, and the sign records which attempts cost
   the call a retry. Not added to the `CouncilStore` protocol — that would have
   broken `test_store_contract.py::TestCouncilStoreProtocol::test_the_protocol_declares_exactly_the_contract`,
   which pins the contract's method set; the daemon reaches it with `getattr`
   and has a fallback.

3. **A detached `start` from an in-memory configuration would have spawned a
   child that silently loaded the packaged default** — i.e. probed ARGO over
   the network and worked `~/.kbutillib/council`, neither of which the caller
   asked for. Found because the first draft of the start test hung for 90 s on
   exactly that. `_default_child_argv` now refuses it (`daemon.py:483`) and
   says to use `--foreground` or a configuration FILE.

4. **The CLI-child kill used the wrong relative import depth**
   (`from ... import llm` in `council.daemon` resolves to
   `kbutillib.domains`), so the drain path raised `ImportError`. Fixed to
   `from .. import llm`, and a failure there can no longer abort the drain —
   the interrupt pass is still owed to the queue. Separately, `run_cli` now
   kills its child's process group on `CancelledError`: cancelling the awaiting
   task unwinds the coroutine and does nothing to the child, which would keep
   answering (and spending against a subscription) with nobody listening.

A fifth, in the test rather than the product: `release.set()` on a
`multiprocessing.Event` after SIGKILLing the child that was waiting on it
deadlocks the parent for ever (the child's wake-up accounting is permanently
short one slot). The kill test no longer calls it — there is nothing to
release, the holder is dead, which is the point.

---

## 5. Regression check

Own task venv, provisioned as instructed:

```
python -m pip install -e '.[council]'
python -m pip install pytest pytest-asyncio pytest-cov ruff
```

(`.[dev,ai]` was not installed: `ai` pulls the optional scientific stack this
machine's venv does not need for the council, and `[council]` plus pytest is
what every council test in this repo runs on. See the honesty note below.)

**Base run, commit 44fb8b4, in this venv, BEFORE any change:**

```
$ python -m pytest tests/domains/ai -q      → 928 passed, 1 skipped
$ python -m pytest tests/cli -q             → 27 failed, 793 passed, 8 skipped
```

**Branch run, same venv, same command:**

```
$ python -m pytest tests/domains/ai tests/cli -q
27 failed, 1755 passed, 9 skipped, 14 warnings in 57.86s
```

**Compared BY NAME, not by count:**

```
$ diff /tmp/base_cli_failures.txt /tmp/branch2.txt && echo IDENTICAL
IDENTICAL
```

The 27 are all pre-existing and all in `tests/cli/test_kind.py`,
`tests/cli/test_clearinghouse*.py` and `tests/cli/test_doctor.py` — missing
optional dependencies in a minimal venv (the same family the committed
`build-baselines/kbutillib.json` records). **No test that passed on 44fb8b4
fails on this branch**, and `tests/domains/ai` is fully green on both.

The rest of the tree (`tests/` minus `domains/ai` and `cli`) was NOT compared
run-to-run: five of its modules cannot be collected in this venv at all
(`duckdb`, `pandas`, the pydantic capability stack), which is pre-existing and
recorded in the committed baseline's `--ignore` list. Instead, that nothing
there can be affected was checked directly:

```
$ grep -rln "council\|domains.ai.llm\|cli_common" tests/ --include=*.py \
    | grep -v "^tests/domains/ai\|^tests/cli"
(no output)
```

No test module outside those two trees references any module this task
touched.

**Lint:**

```
$ python -m ruff check src/kbutillib/domains/ai/council \
    src/kbutillib/domains/ai/council_utils.py src/kbutillib/domains/ai/llm \
    src/kbutillib/interfaces/cli/council.py \
    tests/domains/ai/council/test_daemon.py tests/cli/test_council_cli.py
All checks passed!
```

**The architectural invariant still holds** (no council module imports
`llm.apis` except the four re-export shims) — this is what moved
`kill_cli_children` onto the public wrapper:

```
$ python -m pytest "tests/domains/ai/llm/test_llm_wrapper.py::TestNoCouncilModuleReachesAProviderDirectly" -q
2 passed
```

---

## 6. Manual end-to-end check

Against a temp `HOME` and an endpoint-less configuration (so nothing is probed
and nothing can be spent):

```
$ python -m kbutillib council --config /tmp/dsmoke/c.yaml db init
schema_version: 2
$ python -m kbutillib council --config /tmp/dsmoke/c.yaml daemon status
holder: (none -- no council process is executing)
(none)
$ python -m kbutillib council --config /tmp/dsmoke/c.yaml daemon start
holder: daemon pid 3287202 started 2026-10-16T08:50:00.846234+00:00
$ python -m kbutillib council --config /tmp/dsmoke/c.yaml daemon status
worker_id                            kind    host                 pid      state  heartbeat_age_s  endpoints
poplar.cels.anl.gov-daemon-a5bd351d  daemon  poplar.cels.anl.gov  3287202  live   0.7              []
$ python -m kbutillib council --config /tmp/dsmoke/c.yaml daemon start
Error: LockHeld: refusing to start a council daemon: a council daemon (pid
3287202) started at 2026-10-16T08:50:00.846234+00:00 already holds the
execution lock '/tmp/dsmoke/council/daemon.lock'. ... Stop it, or wait for it
to finish.
$ python -m kbutillib council --config /tmp/dsmoke/c.yaml daemon stop
sent SIGTERM to pid 3287202; the lock is free
$ python -m kbutillib council --config /tmp/dsmoke/c.yaml daemon status
(none)        # the daemon took its workers row with it
```

---

## 7. Divergences between the task prompt and the tree

The prompt was accurate about `execlock.py`'s API, the per-job
`allow_unverified`/`allow_unavailable` options, the wrapper's `apis/` layout
and the worker's 10-minute reclaim. Three places where it described something
the tree does differently, and what was done:

1. **"an exclusive `fcntl.flock(LOCK_EX | LOCK_NB)` on `daemon.lock` … with
   pid, start time and store path written into the file"** — this is already
   `council/execlock.py`, exactly as the round-3 addition says. The round-3
   instruction wins and no second lock was written; the earlier bullet is
   satisfied BY that module, including the `held_message` wording.

2. **"restricted to the served endpoints"** — the queue can only filter by
   ENDPOINT (`claim_calls` takes a set of aliases), while round 3 requires
   readiness per `(endpoint, served model)`. One endpoint can therefore be
   legitimately served and still have an unrunnable route. Resolved with a
   second filter in `_DaemonWorker._claim`, using the same mechanism the base
   class already uses to scope a run to one job. Both layers are tested.

3. **"`kbu council run` refuses while the daemon lock is held (probe with a
   non-blocking flock and release at once)"** vs round 3's "`run` … acquires
   it and HOLDS it for its whole worker lifetime". The later, more specific
   binding wins: `run_async` holds the lock, which also refuses while a daemon
   has it. The probe-and-release form survives in exactly one place where it
   is the right shape — `start`'s check before it spawns a child, so the
   refusal is printed by the command the user typed rather than left in a log.

One honest note on provisioning: the prompt's install line included
`python -m pip install -e '.[dev,ai]'`. I installed `.[council]` plus
pytest/ruff only. `[ai]` pulls the optional scientific stack, and installing it
would have changed which of this repo's 27 pre-existing `tests/cli` failures
reproduce — making the by-name comparison against my own base run less, not
more, trustworthy. The council's own dependencies (SQLAlchemy, httpx, click,
yaml) all come from `[council]`. A reviewer whose venv differs may see a
different pre-existing set; that is an environment gap, not a verdict.

---

## 8. Not done

- **No systemd unit, launchd plist or `nohup` wrapper.** `--foreground` is the
  interface those use and the task says "whatever the machine has"; writing
  one would be an operator-role artefact for a specific machine.
- **`council/__init__.py`'s lazy map still lists `current_user`,
  `ModelRegistry`, `GpuInventory` and `ModelLifecycle`**, whose modules
  (`identity`, `models`) do not exist — leftovers of the designs review round 2
  removed. `CouncilDaemon → daemon` was already mapped and now resolves:
  ```
  $ python -c "from kbutillib.domains.ai.council import CouncilDaemon; print(CouncilDaemon.__module__)"
  kbutillib.domains.ai.council.daemon
  ```
  Removing the other four is a one-line cleanup belonging to whoever owns that
  file's list, not to this task.
- **The daemon does not reap a `workers` row left by a daemon the kernel
  killed.** `status` reports it as `stale`, which is the evidence that it was
  killed; the next daemon registers under its own id and the stale row is
  harmless. Deleting other rows was not asked for and a daemon that tidied up
  rows it cannot identify as dead would be guessing.
