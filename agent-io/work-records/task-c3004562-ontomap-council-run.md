# Work record: task-c3004562 — `kbu ontomap council-run` (stage 4)

## task_id
task-c3004562

## prd
function-to-go-council-pipeline-v1, stage 4 (the approved run) plus the
account-wide request pacer.

## branch
`maestro/developer/background-read-this-first-it-is-task-c3004562`

## commit_shas
- `8338bd8` — `feat(ontomap): execute an approved plan, one chunk at a time (stage 4)`
- (this work record is committed on top of it)

## base
`main` at `f93fcc39` — the worktree's HEAD. No rebase was needed.

## summary

Stage 4 of the standing, quoted, operator-approved `function_to_go` pipeline,
replacing the throwaway `run_chunks.sh`, plus the one genuinely new piece of
machinery the PRD asked for: an **account-wide request pacer**.

No new pipeline infrastructure was written. `council_run` is an ORDER OF CALLS
into things that already shipped — `validate_approval` for the gate,
`endpoint_state`/`reset_budget`/`set_halt` for the ceiling and the stop,
`CouncilUtils.submit`/`run`/`job_status` for the work, and `RUN.json`'s
existing `chunks[].job_id` / `chunks[].retry` contract for the hand-off to
stage 5 (so `council_export` needed no change at all).

### Stage 4: `council_run(run_dir, *, resume=True) -> dict`

`src/kbutillib/domains/genome/ontology_mapping/council_run.py` (new, 1,100
lines). The signature is the PRD's.

1. **The gate.** `validate_approval(run_dir, resume=...)`; its refusal names
   one of its six checks. On FIRST use the consumed `quote_hash` is written
   into `RUN.json` via the gate module's own `mark_consumed`.
2. **The ceiling, as the store's own counter.** `reset_budget` opens one
   epoch per gateway's endpoints; the supervisor then reads
   `endpoint_state.epoch_tokens` — the number the worker itself writes
   through `add_tokens` — and sums it per GATEWAY over that gateway's
   endpoints, joined through the plan's own `transport_allocation`. On
   overrun it writes the store's own `budget_exhausted` halt. There is no
   second counter anywhere in the runner.
3. **One chunk at a time**, in plan order, with `chunks/chunk-NNNN.job`
   (written before the drain) and `chunks/chunk-NNNN.done` (written only once
   the job settles). A resume after a killed chunk therefore re-executes only
   that chunk, and ADOPTS the job it already submitted rather than
   re-submitting the same 2,000 items. KBUtilLib issue 84 is cited beside the
   chunk size, in `RUN.json`'s own `chunks_in_flight_issue`, and in the CLI
   help.
4. **Credentials, RESOLVED and not merely checked.** `$ARGO_USER` (or the
   endpoint's `user_env`) must be set; an `openai` endpoint's `api_key_env`
   is read from the environment, else from `~/.config/amsc/token`, and when
   it comes from the file it is EXPORTED into the environment. That is what
   retires `kbu-env.sh`. Either failing is a `RunRefused` naming
   `argo_user` or `amsc_token`, raised BEFORE anything is submitted and
   before the approval is consumed. No credential value is ever returned or
   logged.
5. **The retry pass as a declared stage**, round cap 2, over items an
   `invalid_output` call cost a verdict. `complete` is false until it has run
   or been declared unnecessary, and either way `RUN.json` says which and
   why.
6. **Per-chunk record** appended to `RUN.json`: items, ok, error,
   `duration_s`, `tokens_by_gateway`, and a `claim_probe`.
7. **Halts** on the ceiling, on a gateway halt (including a sustained rate
   limit), on an operator stop (a `STOP` file in the run directory, or an
   in-process `stop_event`), and on a per-chunk timeout.

### Who drains

`council_run` probes the store's execution lock. Held (a daemon, a
controller, a queue child) → it submits and WATCHES, because that worker
drains every claimable job and a second worker on one queue would claim the
same calls. Free → it drains the chunk itself in a thread, supervised from
the main thread. Both paths are watched identically.

### The account-wide request pacer (the new part)

`_RateBucket` in `engine/worker.py`, hung on the existing `_AccountLimit`
beside its semaphore, acquired inside `_slot()`.

**Where it went, and why — the PRD left this choice open.** It is in the
**per-seat queue WORKER, in `_slot()`**, not in the HTTP backend:

- `_slot(endpoint)` is the one chokepoint every dispatch passes through, and
  `_invoke` and `_invoke_batch` each hold it **exactly once per backend
  invocation** (verified: `worker.py` lines 1698 and 2335 are the only two
  call sites). So one token is taken per REQUEST whether that request carries
  one item or twenty — which is what a rate limit counts.
- The api types are **per-endpoint objects cached by the wrapper**, so two
  aliases of one subscription would be two buckets. That is exactly the wrong
  granularity for a limit the provider enforces account-wide, and fixing it
  there would need a module-level account registry — a second source of truth
  about accounts, next to `_build_limits`, which already has the right one.
- `llm.acomplete` is shared with non-council callers that have no account
  model at all; an account pacer there would impose a council concept on the
  shared wrapper.

Shape: refills at `max_requests_per_minute / 60` with **burst 1** (no burst —
the gateway limit is windowed, and a burst probe overestimated sustained
throughput by ~3x for exactly that reason). The lock is held across the sleep
deliberately, so waiters are served FIFO at the configured rate rather than
all recomputing "I only need 0.1 s" from one stale token count and firing
together. The per-model semaphores are untouched and still enforce the
parallel cap.

Rate source: the smallest `max_requests_per_minute` any endpoint on the
account declares (the tightest declared limit is the real one, as for
concurrency). An account declaring none gets **no bucket** — `None` there
means "no separate per-minute limit known", not "unlimited".

**Queue children are separate processes**, so `CouncilDaemon.
_queue_requests_per_minute` apportions the account's rate between them,
weighted by each child's `concurrency` over the account's
`concurrency × workers`, and passes it as the new `CouncilWorker(...,
requests_per_minute=...)`. Handing each child the full rate would pace MAG's
account at `children × 120` req/min — worse than no pacer, because it looks
enforced. `CouncilConfig.queue_account()` was added to expose the
queue→account mapping `_validate_queues` already records, rather than
re-deriving it.

**A SUSTAINED rate limit is a HALT, not a retry.** `LLMError` gained an
additive `rate_limited: bool = False`, set by `http_common._status_error` on
a 429 (which stays a retryable `transport` failure — one 429 against a
windowed limit is normal). `CouncilWorker._note_rate_limit` counts them PER
ACCOUNT in a 60 s window and, at the 5th, halts the endpoint with
`quota_exhausted`. A flag rather than a message match, and rather than a new
error kind: counting "any transport failure" would halt on a flaky network
instead.

**The two limits recorded rather than designed around** (both in the code,
not just here): the bucket cannot see other consumers on the same account —
another session, another machine, another person on the subscription — which
is why the sustained-rate-limit halt exists at all; and discovering a ceiling
safely means a slow ramp, which burst 1 gives, never a burst probe.

## THE HALT PATH: VERIFIED, AND IT DID NOT HAVE TO BE CHANGED

The PRD required this to be established rather than assumed, and said to
report in the work record whether the existing `endpoint_state` halt path
stops a running worker as-is or had to be made to.

**It stops a running worker as shipped. Nothing was changed and no second
stopping mechanism was added.** Two mechanisms do it, and both were read out
of the tree and then pinned by test:

- `store/queries.py::select_claimable` carries a correlated `~halted`
  condition (lines 203–220), so a halted endpoint's pending calls are not
  claimable at all.
- `engine/worker.py::_scope_state` skips a halted endpoint's calls when
  deciding whether anything is workable, and `work()` then breaks out of its
  loop reporting a `halted` BLOCKER rather than spinning.

Pinned by `tests/domains/test_ontomap_council_run.py::
TestCeilingHaltsARunningWorker`, against a REAL `SqliteStore` and a REAL
`CouncilWorker` over a `FakeBackend`:

- `test_a_halted_endpoints_calls_are_never_claimed` — halt first, then
  `work(until_idle=True)`: **zero** calls made, blockers `["halted"]`.
- `test_a_halt_written_MID_RUN_stops_the_worker_and_is_reported` — a
  supervisor thread writes the halt once the run is observably spending (as
  `_Supervisor` does); the worker stops well short of a full run's 48 calls,
  comes out reporting `halted`, and the job is under 100% complete. Clearing
  the halt lets the SAME worker finish — so the halt is a stop, not a kill:
  no call lost, no retry budget spent, nothing re-submitted.

Note the one honest limit, which is the shipped design and not a defect:
calls **already in flight** when the halt lands are finished and banked. The
halt stops the next CLAIM. `council_run` therefore joins a halted chunk's
drain with `DEFAULT_DRAIN_GRACE_S` (200 s, covering the 180 s default seat
timeout) rather than abandoning claims to time out.

## Reconciliation: where the prompt and the tree disagreed

| the prompt said | the tree held | what I did |
|---|---|---|
| "replacing the throwaway `run_chunks.sh`" and "retires the `kbu-env.sh` wrapper" | Neither file exists anywhere in the repo (`find`). They were throwaway and lived outside it. | Nothing to delete. The CLI help and this record state that `council-run` is what replaces them. |
| implied stage 4 belongs with stages 1 and 5 in `ontology_mapping_utils.py` | That module imports `go_text`, which imports **numpy at module scope**, so it is unimportable without the retrieval stack. Stage 4 ranks nothing. | New `council_run.py` — the same split `unrun.py` and `verdicts.py` already use for the same stated reason — re-exported from `ontology_mapping_utils` so callers find it beside the other stages. The CLI calls it directly rather than through `_ontomap()`, so `council-run` does not pay for numpy. |
| "add ONE TOKEN BUCKET PER ENDPOINT ACCOUNT" (per-process implied) | Queue children are separate `multiprocessing` processes (`daemon.py::_serve_queue_child`), so a per-process bucket at the full account rate paces the account at `children × rate`. | Apportioned the rate across the account's children (`_queue_requests_per_minute`) and recorded why. The bucket is still one per account **per process**, which is the most any in-process mechanism can be. |
| `max_requests_per_minute` treated as not yet existing | It already exists on `EndpointConfig` (config.py:615) — but is read **only** by the quote's wall-clock estimate (`quote.py:1428`) and enforced nowhere. | Reused the existing field as the bucket's rate rather than adding a second knob. |
| "`invalid_output` items" for the retry pass | An item can lose ONE seat to a truncation and still reach a verdict from the other two. Re-asking it would re-spend for an answer the run already has. | Retry only items with an `invalid_output` call **and** a non-`ok` item status — which is the 113-item population the completed run actually lost. Stated in `invalid_output_items`' docstring and pinned by test. |
| nothing said about the store's single epoch counter | `endpoint_state` holds ONE `epoch_tokens` per endpoint, shared with any other work in the store. | Kept using it (a second counter is what the PRD forbids) and emit a WARNING saying what opening an epoch means. Also: a resume does **not** reopen the epoch, because the approved ceiling covers the whole run — which is what `validate_approval(resume=True)` itself warns. |

## files_touched

Added:
- `src/kbutillib/domains/genome/ontology_mapping/council_run.py` — stage 4.
- `tests/domains/test_ontomap_council_run.py` — 68 tests.
- `tests/domains/ai/council/test_rate_pacer.py` — 33 tests.

Changed:
- `src/kbutillib/domains/ai/council/engine/worker.py` — `_RateBucket`,
  `_AccountLimit.pacer`/`.rate_limited_at`, `requests_per_minute` constructor
  parameter, the rate derivation in `_build_limits`, the bucket acquire in
  `_slot`, `pacer_report()`, `_note_rate_limit()`, and the sustained-halt
  branch in `_invoke` and `_invoke_batch`.
- `src/kbutillib/domains/ai/council/daemon.py` —
  `_queue_requests_per_minute`, `_scope_requests_per_minute`, and the
  forward in `_worker_for`.
- `src/kbutillib/domains/ai/council/config.py` — `queue_account()`.
- `src/kbutillib/domains/ai/llm/types.py` — `LLMError.rate_limited`.
- `src/kbutillib/domains/ai/llm/apis/http_common.py` — set it on a 429.
- `src/kbutillib/domains/genome/ontology_mapping/ontology_mapping_utils.py` —
  re-export `council_run`, document stage 4.
- `src/kbutillib/interfaces/cli/ontomap.py` — the `council-run` verb, its
  renderer, its help (including the `systemd-run` requirement), and
  `council-run` out of `_PLANNED_VERBS`.
- `tests/cli/test_ontomap_cli.py` — `TestCouncilRun` (14 tests); `council-run`
  moved from `STUB_VERBS` to `IMPLEMENTED_VERBS`.

## verification

Interpreter `/home/chenry/venvs/kbdl/bin/python` (CPython 3.11.15), pytest
9.1.1. A TARGETED selection, per the task's own test framing: the full suite
runs 900–3400 s against a 600 s sub-agent ceiling.

Baselines were measured on this branch BEFORE any change, in this worktree,
so they are comparable by location as well as by name:

| selection | before | after |
|---|---|---|
| `test_approval.py` + `test_ontomap_cli.py` | 101 passed, 0 failed | — (grew; see below) |
| `test_engine.py` + `test_http_backends.py` + `tests/domains/ai/llm` | 337 passed, **1 failed** | 359 passed, **1 failed** |
| `test_daemon.py` + `test_types_config.py` + `test_four_queue_integration.py` + `test_council_utils.py` | 208 passed, **6 failed** | — (folded into the run below) |

Final run, over every area this change touches:

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src python -m pytest \
  tests/domains/ai/council tests/domains/ai/llm \
  tests/domains/test_ontomap_council_run.py \
  tests/domains/test_ontomap_council_plan.py \
  tests/domains/test_ontomap_council_export.py \
  tests/domains/test_ontology_mapping.py \
  tests/cli/test_ontomap_cli.py tests/cli/test_council_cli.py \
  -q -p no:cacheprovider --no-header

7 failed, 1583 passed in 137.73s          (shell exit code 0)
```

All seven are **pre-existing and node-ID-identical to the baseline taken on
this branch before any change**:

- `test_types_config.py::TestPackagedDefaultConfig` ×6 — the h100 user config
  `~/.kbutillib/config.yaml` leaks past these tests' monkeypatched `HOME`.
  Documented in `build-baselines/kbutillib-h100-main-6310d61.json` as
  machine-local, not a product defect.
- `tests/domains/ai/llm/test_static_headers.py::TestPackagedExample::
  test_the_default_config_declares_no_mag_endpoint` — KBUtilLib issue 83,
  named as pre-existing in the task prompt itself.

The stage-4, ontomap and CLI selections:

```
tests/domains/test_ontomap_council_run.py tests/domains/test_ontomap_council_plan.py
tests/domains/test_ontomap_council_export.py tests/domains/test_ontology_mapping.py
tests/domains/test_unrun_set.py tests/domains/test_verdict_export.py
tests/cli/test_ontomap_cli.py tests/cli/test_council_cli.py
-> 330 passed in 7.94s                                        (exit 0)

tests/domains/test_ontomap_council_run.py tests/domains/ai/council/test_rate_pacer.py
tests/cli/test_ontomap_cli.py
-> 166 passed in 3.41s                                        (exit 0)
```

The threaded and timing-sensitive tests were run five times and three times
respectively; stable each time.

A wider `tests/cli` + smoke run reported 20 failures, every one of them in
files this task does not touch and traceable to this host rather than to the
change:

- `tests/cli/test_kind.py` ×8 and `tests/cli/test_kind_install_containment.py`
  ×1 — no KING checkout at `~/king-stack` on h100
  (`FileNotFoundError: KING launcher not found`).
- `tests/cli/test_model_recording.py::TestVerbStamping` ×2 —
  `CliRunner(mix_stderr=False)` with no fallback; click ≥ 8.2 removed that
  argument. (This task's own CLI test used the repo's existing
  `_separated_runner()` helper, which probes for it.)

`build-baselines/kbutillib.json` already exists, so no baseline was captured
and the full suite was deliberately not re-run.

## inferred rather than verified

- **The pacer's effect on a live gateway.** Every pacer test uses a
  zero-service-time fake. That the configured rate is the dispatched rate is
  measured; that 120 req/min is the right number for MAG is the PRD's
  measurement, carried through, not re-measured here.
- **The chunk timeout (6 h) and the drain grace (200 s)** are judgements
  scaled from the measured 9.46 h / 19,276-item run and the 180 s default
  seat timeout. No wedged chunk has been observed to calibrate against.
- **`RATE_LIMIT_HALT_AFTER = 5` in a 60 s window** is a judgement, not a
  measurement. What is measured is the failure it prevents (1,925 transport
  attempts for 500 items, 427 errored).
- **Cross-thread store use.** `council_run` polls `job_status` on the main
  thread while a foreground drain runs on another, sharing one
  `SqliteStore`. Checked empirically on this host (a thread wrote
  `add_tokens` and the main thread read it back) and by reading the store's
  `threading.local()` per-thread connection; not proven across every
  SQLAlchemy version the project might install.
- **`claim_probe` is a PROXY and is labelled as one in the record itself.**
  The cost that matters is `claim_calls`', which only a worker may pay —
  timing it from the runner would steal claims. What is recorded is the
  quantity it is linear in (the run's pending calls) and the wall time of one
  public read over them.
- **The watch path's behaviour against a real daemon** is exercised only
  against a fake that settles jobs as a draining daemon would. Nothing was
  deployed and no daemon was started: that is the operator role's job.
