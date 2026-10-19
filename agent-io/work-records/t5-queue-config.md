# t5-queue-config

**task_id:** t5-queue-config
**branch:** maestro/developer/t5-queue-config
**commit_shas:**
- `19506105560599e47d039edb9466b5e275be78ca` — feat: add queue partitioning config with account over-subscription checks

## summary

Added an additive, opt-in `queues` section to the council configuration
(`CouncilConfig.queues: Dict[str, QueueConfig] = {}`), merged per entry by
`_merge` exactly like `endpoints`/`models`/`profiles`. `QueueConfig` declares
`seats` (required, non-empty), optional `roles`, `workers` (default 1),
required `concurrency`, `batch_size` (1-50, default 1), and optional
`max_output_tokens_per_item`. A new `cli_temp_root` option was added to
`CouncilConfig` for a later task to consume (prompt-by-file delivery for the
Claude CLI).

When `queues` is non-empty, a `CouncilConfig` model validator
(`_validate_queues`) enforces the full safety contract from the task prompt
and the PRD's "Queue configuration" section / Q4:

- no seat may appear in more than one queue (named in the error);
- coverage is required — every `(seat_name, role)` pair reachable from *any*
  declared profile's default roster must be owned by exactly one queue, with
  no implicit fallback worker;
- every queue must resolve to exactly one provider account, resolved by
  walking **every declared profile** (not just `default_profile`), because
  `roster(profile, overrides)` lets a seat move accounts per job — a profile
  override is therefore caught by the same mechanism that resolves every
  profile, since two declared profiles disagreeing on a seat's account is the
  static, load-time-visible form of that same risk;
- `sum(queue.concurrency * queue.workers)` per account must not exceed that
  account's cap (the **minimum** effective `max_concurrency` across the
  endpoints sharing that account, mirroring `engine/worker.py`'s
  `_build_limits`); the error names the account, its cap, the computed sum
  and every contributing queue;
- `batch_size > 1` is refused for any seat whose endpoint kind is not one of
  `CLI_KINDS` (claude_cli/codex_cli), naming the seat and its kind.

A config with no `queues` is unaffected — the validator is a no-op and every
new field defaults to its empty/`None` value.

A `CouncilConfig.check_roster_against_queues(roster, *, job_id=None)` method
was added as the per-job rejection **predicate** called for by the PRD
(submission-time and pre-claim rejection of a roster/override that falls
outside the validated `(seat, account)` set, with no mutation of any stored
call). It is defined and tested here, but **not wired into any caller** —
see `caveats` below for where it must be invoked from.

## files_touched

- `src/kbutillib/domains/ai/council/config.py`
- `tests/domains/ai/council/test_types_config.py`

## success_criteria_check

- **A four-queue config loads and round-trips.**
  PASS — `test_four_queue_config_loads_and_round_trips` builds the
  three-chairs-plus-combined-critic/meta scenario from the PRD narrative
  (two codex chairs on one account, one ARGO chair, one combined critic/meta
  queue on its own account), asserts the loaded `QueueConfig` fields, then
  dumps `cfg.queues` back into a raw `queues` section and reloads it,
  asserting equality with the original.
- **A duplicate seat, an over-subscribed account, and batch_size>1 on a
  non-CLI seat are each refused with a message naming the specific offending
  items.** PASS — `test_a_seat_in_two_queues_is_refused_naming_both_queues`,
  `test_two_queues_over_an_accounts_cap_are_refused` (asserts account, cap,
  sum and both queue names are all present in the message), and
  `test_batch_size_above_one_on_a_non_cli_seat_is_refused` (asserts the seat
  name and its endpoint kind are both present).
- **A config summing exactly to an account cap is accepted.** PASS —
  `test_two_queues_summing_exactly_to_the_cap_are_accepted`.
- **`workers` multiplies into the account sum.** PASS —
  `test_workers_multiplies_into_the_account_sum` (concurrency 5 × workers 2
  alone exceeds an 8-cap, asserted via the "sum to concurrency 10" message).
- **A config with no `queues` section loads with behaviour unchanged from the
  base commit.** PASS — `test_a_config_with_no_queues_loads_unchanged_from_today`
  loads the pre-existing `config_dict()` fixture (no `queues` key) and asserts
  `cfg.queues == {}`, `cfg.cli_temp_root is None`, and the existing roster
  still resolves.
- **No test that passed on the base commit fails on the branch.** PASS —
  see `tests_run`: the same 4 pre-existing, machine-local failures (and no
  others) are present on the branch; everything else that passed at base
  still passes, plus 21 new queue tests.

Additional PRD-amendment requirements, self-assessed:
- Profile-override-moves-account case (confront amendment a): PASS —
  `test_a_profile_override_moving_a_seat_to_another_account_is_caught`
  (static, load-time form: two declared profiles disagree on `critic`'s
  account) and `test_check_roster_against_queues_refuses_an_override_moving_the_account`
  (dynamic, per-job form via the new predicate).
- Coverage requirement (confront amendment b): PASS —
  `test_a_config_omitting_meta_coverage_is_refused`.
- One-account-per-queue invariant (confront amendment c): PASS —
  `test_a_multi_account_queue_is_refused`.
- `batch_size` 1-50, default 1, no seat-level override (confront amendment d):
  PASS — `test_batch_size_defaults_to_one_not_twenty`,
  `test_batch_size_out_of_range_is_refused` (0 and 51 both refused),
  `test_batch_size_above_one_on_a_cli_seat_is_allowed` (20 accepted on a CLI
  seat). There is no seat-level `batch_size` field anywhere in `QueueConfig`
  or the loader.
- Rejection-hook predicate (confront amendment e): PARTIAL — the predicate
  (`check_roster_against_queues`) is defined and tested in `config.py`, but
  its natural call site is outside this task's owned files; see `caveats`.
- Per-entry merge rule for `queues` (prompt item 2): PASS —
  `test_the_per_entry_merge_rule_applies_to_queues` exercises `_merge`
  directly and asserts overriding one queue does not drop its sibling.

## tests_run

Exact command (as specified for this task):

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest tests/domains/ai/council/test_types_config.py -q -p no:cacheprovider --no-header
```

Result: **4 failed, 114 passed** (93 baseline passed + 21 new queue tests;
0 of my new tests fail). The 4 failures are, by name, exactly the 4
pre-existing/machine-local failures named in the task's test baseline:
`TestPackagedDefaultConfig::test_cli_endpoints_default_to_one_call_at_a_time`,
`TestPackagedDefaultConfig::test_every_packaged_profile_builds_a_roster`,
`TestPackagedDefaultConfig::test_on_a_fresh_machine_with_no_kbutillib_config`,
`TestPackagedDefaultConfig::test_the_packaged_config_declares_no_poplar_endpoint`.
**No other test fails and no new test fails** — confirmed by diffing the
`FAILED` lines against the task's documented baseline list (identical set,
identical count).

Additionally ran the full `tests/domains/ai/council/` subtree (921 tests,
86s) as a broader regression check on dependent modules (worker.py, daemon.py,
store, CLI backends, etc., all of which import `config.py`) given this
module is shared: same 4 pre-existing failures, 921 passed, no new failures.
This was within the single-call budget and not the full suite (which remains
the conductor's to run, per the baseline notice).

## caveats

- **`check_roster_against_queues` is not wired into any caller.** Per the
  task's instruction ("if its natural call site is outside config.py, say so
  ... rather than editing another module"), I left `engine/worker.py` and
  `daemon.py` untouched. The natural call sites are:
  - `engine/worker.py`'s `submit()` (around line 488) — call it with the
    roster being submitted, before the job/items/calls are written in the
    same transaction, so a refusal never creates a job.
  - `engine/worker.py`'s `_claim()` (around line 905) — call it with the
    roster the claiming worker is configured for, before claiming an
    existing job's pending calls, so an old job built under a
    since-changed config is refused rather than claimed.
  A later task in this PRD (not `t5`) should wire these in.
- **Account cap resolution assumes `EndpointConfig.max_concurrency` is
  always populated by the time validation runs.** This holds today because
  `EndpointConfig._check` (a `model_validator(mode="after")` on
  `EndpointConfig` itself) fills the per-kind default before
  `CouncilConfig`'s own validator ever runs (pydantic validates nested models
  before the parent's `model_validator(mode="after")`), so `None` never
  reaches `_validate_queues`. Guarded defensively anyway
  (`int(endpoint.max_concurrency or 0)`).
- **A queue whose seats never appear in any declared profile's roster is
  silently inert** (contributes nothing to any account's sum, never
  triggers the multi-account check). This is an unavoidable consequence of
  "account resolution requires walking declared profiles" and isn't
  mentioned as a requirement anywhere in the task description; I did not add
  a dedicated error for it to avoid inventing a rule the task didn't ask for.
- Did not touch `default_config.yaml` — it declares no `queues`, so the
  packaged default's behaviour is provably unchanged (see
  `test_a_config_with_no_queues_loads_unchanged_from_today`, and the full
  council subtree run above touches `load_council_config()` with no args in
  several other test files with no regressions).
- `cli_temp_root` is declared and expanded (`~` expansion, matching the
  pattern used for `store` and `query_dirs`) but entirely unconsumed by this
  task, as instructed ("another task consumes it; you only declare it").
