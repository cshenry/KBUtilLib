# t15-daemon-batch-config-wiring

## task_id
t15-daemon-batch-config-wiring

## branch
maestro/developer/t15-daemon-batch-config-wiring

## commit_shas
- 2bce870778ce1cfc4f5fa9eb8b0b86c55fdd0485 — fix: thread queue batch_size/max_output_tokens_per_item into daemon worker

## summary
`QueueConfig` (landed by t5) carries `batch_size` and `max_output_tokens_per_item`,
and `CouncilWorker` (landed by t6/t8) accepts both by those exact keyword names,
but `CouncilDaemon._run_as_queue_worker`/`_worker_for` only ever threaded
`seats`/`roles`/`concurrency` into the queue child's worker — `batch_size` and
`max_output_tokens_per_item` were silently dropped, so every daemon-run queue
worker ran with `batch_size=1` regardless of what an operator wrote in config.
This is the second of two gates holding batching shut (the first, correctly
still shut, is that `VERIFIED_OUTPUT_CAPS` is empty). Fixed by adding two new
scope attributes on `CouncilDaemon` — `_scope_batch_size` and
`_scope_max_output_tokens_per_item`, mirroring the existing
`_scope_seats`/`_scope_roles`/`_scope_concurrency` pattern exactly — populating
them from the queue's `QueueConfig` in `_run_as_queue_worker`, and forwarding
them into the `CouncilWorker`/`_DaemonWorker` construction in `_worker_for`. A
queue declaring neither keeps today's defaults bit for bit: `_scope_batch_size`
stays `None` for every pre-queue-mode/unscoped caller, and `_worker_for` maps
that `None` to the literal `1` `CouncilWorker.__init__` already defaults to
(it cannot forward `None` itself there, since the constructor does
`int(batch_size)`); `max_output_tokens_per_item` already defaults to `None` on
both sides, so it is forwarded as-is. No behaviour other than "which value the
worker's `batch_size`/`max_output_tokens_per_item` attributes hold" changes —
`VERIFIED_OUTPUT_CAPS` is untouched (still `{}`) and every refusal path inside
`CouncilWorker._batch_n` is untouched, so a queue that declares `batch_size: 20`
still runs singles today; it now does so because the worker's own refusal
logic saw the real value and said no, not because the value never arrived.

## files_touched
- `src/kbutillib/domains/ai/council/daemon.py`
  - `CouncilDaemon.__init__`: added `_scope_batch_size: Optional[int] = None`
    and `_scope_max_output_tokens_per_item: Optional[int] = None`, alongside
    the existing `_scope_seats`/`_scope_roles`/`_scope_concurrency`, with a
    docstring comment explaining the `None` default's meaning.
  - `_run_as_queue_worker`: reads `qcfg.batch_size`/`qcfg.max_output_tokens_per_item`
    into the two new scope attributes, right next to the existing
    `seats`/`roles`/`concurrency` parsing; updated the method's docstring to
    name the new attributes and to state explicitly that threading the value
    through does not enable batching by itself.
  - `_worker_for`: passes `batch_size=self._scope_batch_size if ... is not None else 1`
    and `max_output_tokens_per_item=self._scope_max_output_tokens_per_item`
    into the `_DaemonWorker(...)` construction, with a comment explaining why
    `batch_size` needs the `None`-to-`1` guard that `seats`/`roles`/`concurrency`
    don't.
- `tests/domains/ai/council/test_daemon.py`
  - Added `import logging` and `from kbutillib.domains.ai.council.config import QueueConfig`.
  - New `TestQueueBatchConfigWiring` class (4 tests), with a shared
    `_scoped_daemon` helper that monkeypatches `CouncilDaemon._queues_config`
    to hand back a directly-constructed `QueueConfig` (see Caveats for why)
    and stubs `_serve` so `_run_as_queue_worker`'s scope-setting prologue runs
    for real without entering the infinite service loop:
    - `test_batch_size_n_constructs_its_worker_with_that_value` — asserts
      `worker.batch_size == 7` for a queue declaring `batch_size=7`.
    - `test_max_output_tokens_per_item_passes_through` — asserts
      `worker.max_output_tokens_per_item == 321`.
    - `test_a_queue_declaring_neither_gets_base_commit_defaults` — asserts
      `worker.batch_size == 1` and `worker.max_output_tokens_per_item is None`.
    - `test_batch_size_20_with_empty_verified_caps_still_runs_singles_and_names_the_seat`
      — the one that matters: asserts `VERIFIED_OUTPUT_CAPS == {}`, that
      `worker.batch_size == 20` (the value arrived), that claiming three
      same-seat rows and grouping them via the real, untouched
      `CouncilWorker._claim`/`_group_claimed` still yields three singleton
      groups, and that a captured log record contains both "no VERIFIED
      effective" and the seat name `proposer_1`.

## success_criteria_check
- "A queue declaring batch_size and max_output_tokens_per_item constructs its
  worker with those values (asserted on the worker)" — PASS.
  `test_batch_size_n_constructs_its_worker_with_that_value` and
  `test_max_output_tokens_per_item_passes_through` assert directly on
  `worker.batch_size`/`worker.max_output_tokens_per_item`, not on the config.
- "A queue declaring neither gets base-commit defaults" — PASS.
  `test_a_queue_declaring_neither_gets_base_commit_defaults` asserts
  `worker.batch_size == 1` (the literal default `CouncilWorker.__init__`
  already uses) and `worker.max_output_tokens_per_item is None`.
- "With VERIFIED_OUTPUT_CAPS empty a queue declaring batch_size 20 still runs
  singles and logs the no-verified-cap refusal naming the seat" — PASS.
  `test_batch_size_20_with_empty_verified_caps_still_runs_singles_and_names_the_seat`
  confirms `worker_module.VERIFIED_OUTPUT_CAPS == {}` first (guarding against a
  future change silently invalidating the test's premise), then exercises the
  real claim/group path on three actually-pending same-seat call rows and
  shows all three come back as one-row groups, with a `logging.WARNING`
  record naming `proposer_1` and reading "no VERIFIED effective output
  cap ... batching forced to singles".
- "The bounded-drain contract and the positional super()._claim call are
  preserved" — PASS, by construction: neither `_interrupt_claimed` nor
  `_DaemonWorker._claim`'s `super()._claim(limit, job_id, summary)` call was
  touched by this diff (confirmed by `git diff`, see `files_touched`); the
  pre-existing `TestTheStopDeadline`/`TestQueueMode` drain tests
  (`test_a_hung_call_at_the_deadline_is_interrupted_and_costs_no_retry`,
  `test_the_bounded_drain_contract_holds_for_a_queue_worker`) still pass
  unchanged on the branch (see `tests_run`).
- "No test that passed on the base commit fails on the branch" — PASS. Full
  targeted selection on the branch: `4 failed, 993 passed`; measured baseline
  at base commit `9da076b` (see `tests_run`): `4 failed, 989 passed`, same 4
  failing ids. `993 - 989 = 4`, exactly the 4 new tests added; nothing else
  moved.

## tests_run
Baseline, measured in a disposable `git worktree` checked out at base commit
`9da076b` (removed afterward), from that worktree's root:
```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest \
  tests/domains/ai/council -q -p no:cacheprovider --no-header
```
Result: `4 failed, 989 passed in 192.81s`. The 4 failures are all in
`test_types_config.py::TestPackagedDefaultConfig`
(`test_every_packaged_profile_builds_a_roster`,
`test_the_packaged_config_declares_no_poplar_endpoint`,
`test_on_a_fresh_machine_with_no_kbutillib_config`,
`test_cli_endpoints_default_to_one_call_at_a_time`) — matches the envelope's
stated baseline and dev row 53 (this box's `~/.kbutillib/config.yaml` leaking
past a monkeypatched `HOME`). Matches the envelope's "989 passed" exactly
(the envelope said "4 failed / 989 passed" as the headline baseline for this
selection — t14, merged into this branch's base, already carries the prior
5-test increment from 984).

Same command on the branch (worktree root, after both commits):
```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest \
  tests/domains/ai/council -q -p no:cacheprovider --no-header
```
Result: `4 failed, 993 passed in 157.48s` — same 4 failing ids, 4 new passing
tests, nothing else moved.

Targeted re-run of just the new class:
```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest \
  tests/domains/ai/council/test_daemon.py::TestQueueBatchConfigWiring -q -p no:cacheprovider --no-header
```
Result: `4 passed in 2.65s`.

Did not run the full `tests/` tree per the machine-specific instructions (a
`.maestro` worktree spuriously fails `tests/core/test_composition_smoke.py`
via a fixture that resolves a path onto a nonexistent sibling worktree — out
of scope for this task and the conductor's to own).

## key-name trace: QueueConfig -> CouncilDaemon -> CouncilWorker
- `QueueConfig.batch_size: int = 1` (`src/kbutillib/domains/ai/council/config.py:711`)
  -> `CouncilDaemon._run_as_queue_worker`: `self._scope_batch_size = int(qcfg.batch_size)`
  -> `CouncilDaemon._worker_for`: `batch_size=self._scope_batch_size if self._scope_batch_size is not None else 1`
  -> `_DaemonWorker.__init__(*args, **kwargs)` (no `batch_size`-specific handling,
     passes through via `**kwargs`) -> `CouncilWorker.__init__(..., batch_size: int = 1, ...)`
     (`src/kbutillib/domains/ai/council/engine/worker.py:407`) -> stored verbatim as
     `self.batch_size: int = max(1, int(batch_size))` (`worker.py:427`).
  Every hop uses the identical keyword `batch_size`; confirmed by reading
  `CouncilWorker.__init__`'s signature directly (`worker.py:383-409`) rather
  than assuming it from `QueueConfig`'s field name, per the task's explicit
  instruction. The one place the name does NOT travel unchanged is the
  `None`-vs-`1` boundary at `_worker_for`, documented above and in the code
  comment at the call site.
- `QueueConfig.max_output_tokens_per_item: Optional[int] = None` (`config.py:712`)
  -> `CouncilDaemon._run_as_queue_worker`: `self._scope_max_output_tokens_per_item = int(...) if ... is not None else None`
  -> `CouncilDaemon._worker_for`: `max_output_tokens_per_item=self._scope_max_output_tokens_per_item`
  -> `_DaemonWorker.__init__` (`**kwargs` passthrough) -> `CouncilWorker.__init__(..., max_output_tokens_per_item: Optional[int] = None, ...)`
     (`worker.py:408`) -> stored as `self.max_output_tokens_per_item` (`worker.py:428-432`).
  Identical keyword at every hop, `None` both ends when unset — no translation
  needed here, confirmed the same way.

## confirmation that batching remains disabled
- `VERIFIED_OUTPUT_CAPS` in `src/kbutillib/domains/ai/council/engine/worker.py`
  is untouched by this diff (`git diff` touches only `daemon.py` and
  `tests/domains/ai/council/test_daemon.py`) and remains the literal empty
  dict `{}` declared at `worker.py:174`.
- None of the three refusal paths inside `CouncilWorker._batch_n`
  (no verified cap / no per-item estimate / derived ceiling below 1,
  `worker.py:1342-1417`) were modified.
- `test_batch_size_20_with_empty_verified_caps_still_runs_singles_and_names_the_seat`
  asserts `worker_module.VERIFIED_OUTPUT_CAPS == {}` as its first line (so a
  future change that populated the table would fail this test loudly rather
  than silently invalidating its premise), then proves — against the real,
  unmodified engine code — that a queue configured for `batch_size=20` still
  settles every claimed row as its own singleton group and still logs the
  no-verified-cap refusal naming `proposer_1`.

## caveats
- **Why the tests monkeypatch `_queues_config` instead of declaring
  `batch_size` in a YAML `queues:` section.** `CouncilConfig._validate_queues`
  (owned by t5, in `config.py`, not touched by this task) refuses
  `batch_size > 1` for any seat whose endpoint kind is not a CLI kind
  (`claude_cli`/`codex_cli`). Every seat in `test_daemon.py`'s fixtures uses
  the module's test-registered `fake` kind (by design — see the module
  docstring: no real network call, no token, no CLI login), so any queue
  config loaded through the normal YAML/dict path with `batch_size > 1` would
  be refused at config-load time before daemon.py ever saw it, which would
  make it impossible to exercise this task's wiring at all through that path.
  Rather than touch `config.py` (out of scope) or add a CLI-kind test fixture
  (a much larger, unrelated change to this module's test harness), I
  monkeypatch `CouncilDaemon._queues_config` to hand back a directly
  constructed `QueueConfig` instance, which is a pydantic model with no
  cross-seat validation of its own (that validation lives in
  `CouncilConfig._validate_queues`, a separate validator over the whole
  config). This tests exactly what this task owns — whether a `QueueConfig`
  value `daemon.py` is handed reaches the worker — independent of how that
  value was validated upstream, and does not touch or weaken t5's validation
  gate in any way.
- **`_run_as_queue_worker`'s `_serve` is stubbed in the new tests.** The real
  method acquires the queue's partition lock, `os.chdir`s into the council
  directory, and then runs `_serve` forever until stopped. Running it
  unstubbed in-process (versus the existing pattern of spawning it in a
  separate OS process) would block the test. I stub `_serve` to return `{}`
  immediately so only the scope-parsing prologue — the code this task
  actually adds — executes for real; the lock-acquire and chdir still run
  unstubbed. Because `os.chdir` runs in the test's own process (not a spawned
  child, unlike every other `_run_as_queue_worker`-exercising test in this
  file), each of the four new tests calls `monkeypatch.chdir(Path.cwd())` as
  its first line purely as a safety net to restore this process's cwd at
  teardown, regardless of what directory the daemon's own `os.chdir` lands
  in.
- **The "singles" test exercises the real, unmodified claim/group code
  (`CouncilWorker._claim` -> `_group_claimed` -> `_batch_n` ->
  `_effective_output_cap`), not a mock of it.** I considered asserting only
  `worker.batch_size == 20` plus a direct call to `_batch_n`/`_group_claimed`
  in isolation, but chose to also drive three real pending call rows through
  `worker._claim(...)` (which internally calls `_group_claimed` as part of
  its own per-account accounting) so the test demonstrates the refusal
  against genuinely claimed rows, not synthetic ones — closer to what an
  operator would actually observe.
- Did not touch `config.py`, `engine/worker.py`, or any file outside
  `daemon.py`/`test_daemon.py`, per the task's "files you own" constraint.
