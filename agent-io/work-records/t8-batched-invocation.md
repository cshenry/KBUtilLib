# t8-batched-invocation — work record

- **task_id:** `t8-batched-invocation`
- **branch:** `maestro/developer/t8-batched-invocation`
- **commit_shas:**
  - `385657f14a358cbc3c61b416270759d503f72ac8` — feat(council): batched invocation for CLI seats (t8)
- **base commit:** `edb8627` (PRD `council-per-seat-queues-v1`, phase 7 of 12)

## Summary

Added batched invocation to `CouncilWorker`: a worker opted into
`batch_size > 1` can now send several claimed call rows sharing
`(job_id, role, seat_name, query_name, query_version)` to a backend in one
invocation, settle each row's answer independently by an opaque `item_key`,
and bisect a whole-invocation failure down to singles rather than losing an
entire batch to one poison item. `batch_size` and `max_output_tokens_per_item`
are threaded through `CouncilWorker.__init__` the same way `concurrency`
already is (queue-level settings, not re-derived from config inside this
class). `_group_claimed`/`_run_group` are the seam the previous task
(`t6`/`t7`) left for this; `_claim` now claims up to `free * batch_size`
candidate rows per account and charges the account's `_AccountLimit.assigned`
once per **kept group**, not once per row, consuming `_group_claimed` for
that rather than re-deriving a row bound (confront round 1, point i).

Per confront round 1, point (a), I verified that **neither** CLI backend's
argv forwards an output-token ceiling (`ClaudeCliBackend.argv` /
`CodexCliBackend.argv` in `llm/apis/cli.py`), so there is no mechanism to
verify a cap against today. `VERIFIED_OUTPUT_CAPS` (`engine/worker.py`) is
therefore an **empty, documented table** keyed by `(endpoint_kind, model)`:
`_effective_output_cap` returns `None` for every real CLI seat, which forces
`_batch_n` to size the next group as 1 (singles) and `_group_claimed` to
dispatch it through the pre-existing, unmodified `execute()` path — bit for
bit today's behaviour, not the new wrapped-batch-of-one mechanics. **This
means batching lands disabled for every real seat on this commit. That is the
documented, correct outcome, not a shortfall** — the task explicitly frames it
as success. A test monkeypatches `VERIFIED_OUTPUT_CAPS` to exercise the
ceiling arithmetic and the full batching path without waiting on that
verification; production code never writes to the table.

Settlement is 1:N: the wrapper (`{"answers": [...]}`) is validated separately
from each element's role payload, and `item_key` is stripped before the
EXISTING `_validate_answer`/`normalise()` run per element — one malformed
element never fails its batch-mates. Missing and keyed-but-invalid elements
are both released for retry with `attempt_count` incremented, subject to the
worker's existing `max_attempts`, and settle `error` on exhaustion. Unknown
`item_key`s are dropped with a warning; a duplicate keeps the first.

Usage is anchored on a unique `invocation_id` (not "the batch's first row"):
every member's attempt row carries `invocation_id`, `batch_size`,
`member_call_ids` and an explicit anchor `(call_id, attempt_no)`; only the
anchor's own attempt also carries the real `prompt_tokens`/`completion_tokens`
(everyone else's usage dict omits them, so nothing can be summed into a
double count). This is recorded for a **failed** invocation too, via a
negative/uncharged `attempt_no` (the same `next_uncharged_attempt_no`
convention `daemon.py`'s `interrupted` recording already uses), so a
transport/timeout/truncation/wrapper failure's spend is no longer discarded
the way the single-call retry path discards it today. Bisection runs the two
halves sequentially under the SAME claims (never released mid-split), and
bottoms out by handing a lone row to the ordinary, unmodified single-call
path — which is where its own `max_attempts` budget is spent, so a poison
item's ancestor (batch-level) failures never charge anything and its
batch-mates are never charged for its cost.

The per-item output running mean (`(seat_name, role, served_model)` ->
`(total_completion_tokens, total_members)`) is fed by successful, complete,
measured batch invocations AND by ordinary singleton successes (the
pre-existing `_settle_ok` path now also calls `_record_output_sample` with
`members=1`) — "fresh singleton observations" per the PRD, so a freshly
configured batched queue can bootstrap its own estimate from ordinary
single-call traffic rather than being permanently stuck with no estimate.
Zero-cost samples (`completion_tokens <= 0`) are excluded at the one choke
point (`_record_output_sample` itself), so a reused answer or an unmeasured
usage dict coerced to zero can never drag the mean down and enlarge the next
batch.

`prompt_hash` is unaffected by any of this: `_prepare_member` renders and
hashes each row's prompt exactly as `execute()` always has, before any
wrapping happens, so cross-job reuse (`find_reusable`) keeps working
unchanged — proven by a dedicated test.

## Files touched

- `src/kbutillib/domains/ai/council/engine/worker.py` — batching
  constructor params, `VERIFIED_OUTPUT_CAPS`/`BATCH_WRAPPER_SCHEMA`/
  `BATCH_ITEM_KEY_FIELD`, `_BatchMember`, `_group_claimed`/`_split_bucket`/
  `_batch_n`/`_effective_output_cap`/`_per_item_output`/
  `_record_output_sample`/`_log_once`, the rewritten `_claim` (per-group
  `assigned` accounting), `_run_group`/`_run_one_within_group`/
  `_run_batch_or_split`/`_prepare_member`/`_invoke_batch`/
  `_invocation_usage`/`_record_failed_invocation`/`_uncharged_attempt_no`/
  `_write_attempt`/`_settle_batch_answers`/`_settle_batch_ok`/
  `_settle_batch_released`, the module-level `_bisect_rows` helper, and one
  additive hook in `_settle_ok` (feeds `_record_output_sample` for a
  singleton success; a no-op read by nothing when `batch_size <= 1`).
- `src/kbutillib/domains/ai/council/queries/spec.py` — `BATCH_ITEM_KEY_FIELD`,
  `BATCH_SYSTEM_SUFFIX`, `wrap_batch_answer_schema`, `render_batch_user`
  (imported lazily from `worker.py`, inside `_invoke_batch`, to keep that
  module's "nothing here imports a query type" property at module scope).
- `tests/domains/ai/council/test_batched_invocation.py` (new) — see Tests
  below.

## Success criteria self-assessment

| Criterion | Verdict | Note |
|---|---|---|
| One batched reply settles N call rows | PASS | `TestBatchedSettlement::test_n_rows_settle_from_one_reply` |
| A reply missing k item_keys settles N-k, releases k with attempt_count incremented | PASS | `test_missing_k_item_keys_settles_n_minus_k_and_releases_k` |
| Unknown and duplicate item_keys dropped per spec | PASS | `test_unknown_item_key_is_dropped_and_nothing_is_written_for_it`, `test_duplicate_item_key_takes_the_first` |
| Usage recorded once with batch size and member call_ids, zero-cost attempts elsewhere | PASS | `TestUsageAttribution::test_usage_is_recorded_once_with_batch_size_and_member_ids` |
| `assigned` moves by one per batch | PASS | `test_assigned_moves_by_one_per_batch_not_n` |
| A poison item is isolated by bisection while its batch-mates settle | PASS | `TestBisection::test_a_poison_item_is_isolated_while_its_batch_mates_settle` |
| Unknown per-item output forces batch_size 1; a derived ceiling below batch_size reduces the batch with both numbers logged | PASS | `TestDerivedCeiling` (five tests, including the "logged once" guard) |
| An item answered inside a batch is reusable by prompt_hash from a different job | PASS | `TestReuseAcrossBatching::test_an_item_answered_inside_a_batch_is_reusable_from_a_different_job` |
| `batch_size` 1 reproduces base-commit behaviour | PASS | `TestBatchSizeOneUnchanged` + the full `test_engine.py` suite (108/108 unchanged, see Tests) |
| Measured per-item output and harness floor recorded in the work record | PARTIAL | see "Measurement" below — the sandbox denies running `claude` directly, so neither figure could be independently measured from this session; documented honestly rather than guessed |
| Whether a CLI output cap could be verified, stated plainly | PASS | no — stated above and in-code (`VERIFIED_OUTPUT_CAPS` is empty, documented why); batching lands disabled for every real CLI seat, which the task states is success |
| No test that passed on the base commit fails on the branch | PASS | see Tests |

## Tests run

Exact command (per the envelope's baseline):

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest tests/domains/ai/council -q -p no:cacheprovider --no-header
```

Result: **4 failed, 963 passed** (164 s). The 4 failures are exactly
`tests/domains/ai/council/test_types_config.py::TestPackagedDefaultConfig::*`
(all four of them: `test_every_packaged_profile_builds_a_roster`,
`test_the_packaged_config_declares_no_poplar_endpoint`,
`test_on_a_fresh_machine_with_no_kbutillib_config`,
`test_cli_endpoints_default_to_one_call_at_a_time`) — the PRE-EXISTING,
machine-local failures named in the envelope's baseline (dev row 53,
`/home/chenry/.kbutillib/config.yaml` leaking past the monkeypatched `HOME`).
963 = 941 (baseline passed) + 22 (this task's new test module). No test that
passed on the base commit fails on this branch, and no new failures appeared.

Also ran, earlier and narrower, while iterating:

- `tests/domains/ai/council/test_engine.py` alone: **108 passed** both before
  and after every edit to `worker.py` (including the `_claim` rewrite),
  confirming `batch_size <= 1` stays bit for bit.
- `tests/domains/ai/council/test_batched_invocation.py` alone: **22 passed**
  (the new module), ~36 s.

## Measurement (the PRD's "measure first")

Two numbers in the PRD came from a single stored usage record because
`claude -p` could not be run while authoring it: the ~7,500-token CLI harness
floor, and that a ~16K output budget supports a 20-item batch at ~667
output-tokens/item. I attempted to independently measure both from this
session and could not: invoking `claude` directly is denied by this
sandbox's permission policy (`Permission to use Bash with command ... claude
... has been denied`), consistent with the documented `WAKE_DENY` posture for
headless h100 sessions (claude/curl/ssh/rm/git-push are held back). I did not
work around this with a different invocation path, since a sandboxed-away
subprocess call is a policy boundary, not a bug to route past.

Consequently I cannot add a measured number here, and I am saying so plainly
rather than restating the PRD's single stored sample as if I had confirmed
it. This has **no effect on correctness**: per confront round 1 point (a),
batching is correctly forced to singles for every real CLI seat regardless of
either number, because the blocking fact is structural (no CLI argv forwards
an output-token ceiling at all), not a question of what the harness floor or
per-item output happens to measure as. Whoever next attempts to verify a cap
and enable batching for a real seat should run the measurement this task
could not, and add the result to `VERIFIED_OUTPUT_CAPS` only once it is
independently confirmed end to end — not before.

## Caveats

- **Ancestor (batch-level) bisection failures do not increment
  `attempt_count`.** The PRD's confront round 1 point (h) literally says
  "record and increment `attempt_count` for each failed ancestor invocation,
  but do not enforce the single-item `max_attempts` budget until a row is
  invoked alone" — but `attempt_count` is the ONLY column the single-item
  `max_attempts` gate reads (`_retry_later`/`_fail_call`, unchanged), and
  there is no second column to split the two meanings. Incrementing it on
  every ancestor failure would make the gate fire early for a poison item's
  *neighbours* too, re-creating exactly the problem point (h) exists to
  solve (a 20->10->5->3->2->1 path needs five ancestor failures before a
  poison item is ever tried alone). I resolved this by **not** incrementing
  `attempt_count` for ancestor failures at all, auditing them instead under
  a negative, UNCHARGED `attempt_no` (the same convention `daemon.py`'s
  `interrupted` recording already uses for "this try is not the call's
  fault"), which satisfies the actually-load-bearing half of point (h) (the
  budget is never spent until a row is invoked alone) without resurrecting
  the contradiction. `TestBisection` asserts both halves of this: the poison
  item's own `attempt_count` ends at exactly what the ordinary singleton
  `max_attempts` gate would charge, and its batch-mates' `attempt_count`
  never moves.
- **Single-flight collapsing is not applied to batch members.** `execute()`'s
  single-flight dedup (two identical in-flight prompts collapsing into one
  call) is deliberately not ported into `_prepare_member`/`_invoke_batch` —
  only the route-fingerprint check, reuse check and permission gate are.
  Two concurrently-batched invocations that happen to carry an identical
  prompt will both be sent rather than one following the other; the
  cross-JOB reuse path (`find_reusable`, checked per member before a batch
  is sent) still prevents a *second* invocation from re-asking something
  already settled. Scoped out for time; not covered by a test.
- **daemon.py is not wired to pass `batch_size`/`max_output_tokens_per_item`
  from `QueueConfig`** — those two fields already exist on `QueueConfig`
  (landed by an earlier task) but nothing yet threads them into the
  `CouncilWorker(...)` construction in `daemon.py`, which is outside this
  task's owned files (`engine/worker.py`, `queries/spec.py`, tests only).
  Until that wiring lands, `batch_size` defaults to 1 for every daemon-run
  worker regardless of a queue's configured value — a second, smaller gap
  on top of "no verified cap", worth flagging to whichever task (`t11`
  four-queue integration, most likely) wires queue config into daemon
  worker construction.
- **Cross-job batch assembly is real, not merely cross-item.** `_group_claimed`
  buckets by `(job_id, role, seat_name, query_name, query_version)`, so two
  DIFFERENT jobs of the same query type sharing a seat name are never batched
  together, even though the PRD's grouping key nominally allows it (job_id is
  part of my key; the PRD's own key list also includes job, so this matches
  the letter of the spec). Batching this task builds is therefore
  within-job, across-items only; cross-job batching of the same
  (role, seat, query) across concurrently pending jobs was considered but not
  implemented, since asserting homogeneity across two jobs' independently
  resolved rosters (which could route the same seat name differently) adds a
  real correctness risk for no evidence of operational need yet.
