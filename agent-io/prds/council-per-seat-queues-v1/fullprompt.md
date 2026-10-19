# Per-seat queues and batched CLI calls for the council engine

## Research Report

### Important findings

**1. The starvation has an exact mechanism, and it is a budget bug, not a queue-shape bug.**
`CouncilWorker._free_slots()` (`engine/worker.py:901`) sums free capacity across
*every* account the worker serves.
That total becomes the `limit` passed to `store.claim_calls()`, but
`select_claimable()` (`store/queries.py:164`) orders by
`(job priority, submitted_at, item_order, role_rank, call_id)` with **no
per-account quota** — so a saturated account's rows occupy the whole batch.
`_claim()` then increments `_AccountLimit.assigned` **unconditionally**
(`engine/worker.py:923`), so `assigned` runs far past `capacity`, which drives
`_free_slots()` to zero and stalls every other account.

Measured on the live store (`/scratch/chenry/council/council.sqlite`, job
`96a612a03a2149c5935478d519f9f522`): **92 calls claimed against ~20 slots**, of
which codex held 66.
Argo, which the store shows succeeding 52/52, held 5.

**2. `calls.role` is the wrong partition key, and partitioning on it would fix nothing.**
`ROLE_ORDER` is `("proposer", "critic", "meta")` (`store/queries.py:65`) — three
values.
The live roster has five seats, and all three council chairs carry
`role='proposer'`:

| seat | role | endpoint | account | model |
|---|---|---|---|---|
| proposer_1 | proposer | argo | argo | Gemini 3.5 Flash |
| proposer_2 | proposer | codex | codex | gpt-5.6-sol |
| proposer_3 | proposer | codex | codex | gpt-5.6-terra |
| critic | critic | claude | claude | sonnet |
| meta | meta | claude | claude | opus |

A partition on `role` leaves all three chairs sharing one queue — the exact
head-of-line blocking being removed.
The partition key is the **seat**; the key that owns a concurrency budget is the
**account** (`_build_limits`, `engine/worker.py:362`).
The two are different, and conflating them is how two codex worker processes
would open 16 concurrent children against an account declaring 8.

**3. The review stage is already gated correctly. No monitor script is needed for it.**
`decide()` returns `waiting` until every proposer has settled, and `advance()`
(`engine/worker.py:1652`) creates the critic row only on `need_critic`, **inside
the settling call's own transaction**.
Evidence: chair successes were 52 / 26 / 25, so ~25 items had all three chairs
in, and exactly **24 critic rows exist**.
Nothing was queued ahead of its conditions.
This is strictly better than a polling monitor — event-driven, no poll interval,
and atomic with the reply that triggered it.
What *is* absent is a supervisor that launches N workers per queue to a
configured concurrency; that is built here.

**4. The existing daemon structurally forbids a second worker. This is the main obstacle.**
`CouncilDaemon` holds `ExecutionLock` (kind `daemon`) for its entire life, over a
**single lock file per store directory** (`execlock.py:55`, `LOCK_KINDS` at
`:61`), explicitly so that "two workers on one store would claim the same calls
and a migration would change the schema under a running worker."
Per-seat worker processes are N workers on one store — precisely what that lock
exists to prevent.
The two stated reasons are not equally load-bearing: double-claiming is already
prevented by the claim protocol itself (`BEGIN IMMEDIATE` plus a per-row
`claim_token` plus a `status == 'pending'` guard, `store/queries.py:236`), so the
lock's irreplaceable job is **migration safety**.
That makes the fix principled rather than a weakening: workers take a *shared*
lock, migration takes an *exclusive* one.

**5. The Claude token floor is not our payload, and three obvious causes are already excluded.**
The argv already passes `--tools ""` and already *replaces* the system prompt
rather than appending (`llm/apis/cli.py:114-136`), and `run_cli` already runs the
child in a **fresh empty temp directory** specifically to stop `CLAUDE.md` and
git-repo context injection (`llm/apis/cli_common.py:713,743`).
The payload is small: the entire `go_mf@3` spec — every role's prompt template
and answer schema together — is 8,269 **characters** (~2k tokens), and a
per-item question is ~1,200 characters.
The one `ok` Claude attempt in the store reports
`{"prompt_tokens": 2, "completion_tokens": 667}` with ~7,835 tokens appearing as
`cache_creation`, so the harness is ~7,500 tokens and **96% of billed input is
boilerplate**.
It is charged as a cache *write* on every call, never a read.

**6. Batching beats caching, and makes the caching question moot.**
Per-item input is `7500/n + 300`:

| batch | per-item input | reduction | output cap needed | failure blast radius |
|---|---|---|---|---|
| 1 | 7,800 | — | 4 K (today) | 1 item |
| 6 | 1,550 | 5.0x | 4 K | 6 |
| **20** | **675** | **11.6x** | **~16 K** | **20** |
| 50 | 450 | 17.3x | ~48 K | 50 |
| 100 | 375 | 20.8x | ~90 K | 100 |

At n=20 the 38,552 Claude calls become 1,928 and input drops from ~302 M tokens
to ~26 M.
Paying the floor 1,928 times instead of 38,552 removes the motive for chasing a
cache hit, so no work is spent on prefix stability.
The curve is sharply diminishing: 20→50 buys 1.5x more while tripling the blast
radius and requiring an output ceiling nobody has verified against the CLI.

**7. Output tokens do not amortize, and become the dominant cost.**
667 completion tokens/item × 19,276 items ≈ 12.9 M tokens regardless of batch
size.
After batching, any further cost work must target output or call volume — which
is finding 8.

**8. The council never agrees, so the expensive stage runs on every item.**
`decide()` emits `decided_by="unanimous"` only when the vote is unanimous and
approved (`engine/decide.py:346-357`).
24 of ~25 fully-chaired items went to a critic, i.e. a measured ~100%
disagreement rate.
This is the largest cost term in the pipeline and it is **out of scope** here
(see Out of Scope) — it is prompt and schema design against `go_mf`, not
throughput.

**9. The engine cannot measure its own seat latency, which is why two honest measurements disagreed 65x.**
`engine/worker.py:1173` stamps `started = _utcnow()` **before**
`async with self._slot(endpoint)` on the next line.
Every `attempts` row therefore records queue-wait + service time as one number.
Store medians read codex 345–352 s and argo 127 s; the prior session measured
codex at ~5.3 s uncontended.
Both are defensible readings of different quantities, and no data in the store
can separate them.
Codex itself is healthy: **51 attempts, 51 `ok`, zero errors**.
Claude was the opposite: 23 of 24 attempts `transport`.

### Links

No external literature scan was dispatched — see the quality report.

### Quality report

What was read: the council engine (`engine/worker.py` claim loop, `advance`,
`_invoke`), the store (`store/queries.py` claim ordering, `store/schema.py`
`calls` table, confirming `role` and `seat_name` are existing non-null columns so
no schema change is needed), `engine/decide.py`, `daemon.py`, `execlock.py`,
`config.py` (`EndpointConfig.account` / `max_concurrency` at `:573-574`, default
at `:634`), both CLI backends (`llm/apis/cli.py`, `llm/apis/cli_common.py`), and
the **live production store** for the failed run — roster, per-seat call states,
the `attempts` table, and the stored `query_types` specs.
Every quantitative claim above is from that store or from a cited line, not from
the prior session's episode, and where the two disagree the discrepancy is stated
rather than resolved by preference.

What was NOT searched, and why it matters:

- **No external/literature scan ran.** Multi-queue work distribution and
  head-of-line blocking are thoroughly settled territory and the diagnosis here
  is specific to this codebase, so the expected yield was low. Confidence in the
  design is not materially limited by its absence.
- **`claude -p` was never executed.** The permission gate refused the command in
  the authoring session. Consequently the 7,500-token harness figure is derived
  from one stored `usage` record plus the argv, **not** measured across variants,
  and the claim that a 16 K output ceiling supports a 20-item batch is arithmetic
  from a 667-token-per-item observation rather than an observed batched call.
  Task `t8-batched-invocation` must measure both before the batch default is
  trusted; the derived-ceiling guard exists precisely so that being wrong here
  fails loudly as a refused config rather than silently as truncation.
- **The per-item payload size for the *critic* role is unmeasured.** A critic
  prompt carries the chairs' ballots and votes and so is larger than a
  proposer's ~1,200 characters. The 300-token per-item figure is the proposer's.
  Batch sizing is derived at runtime from measured per-item output, so this does
  not block the build, but the critic's effective batch may be smaller than 20.

Confidence: **high** on the starvation mechanism, the partition key, the review
gating, and the lock obstacle (all read directly from source and corroborated by
live-store counts). **Medium** on the batching arithmetic (one usage sample).
**Low** on the absolute token floor (one sample, no variant measurement).

## Revision Log

- **Confront round 1 — 2026-10-19.** Codex (`task-570d9569`) returned 12 binding
  stalls and **all 12 were accepted** — none were padding, and three were
  internal contradictions or factual errors in round 0. The three that changed
  the design: a seat name does not bind to one account (`roster()` overrides can
  replace the whole proposer bench), so account validation must cover every
  declared profile; a shared store lock alone does not prevent two configs
  over-committing a provider, so queue workers now run only under a single
  controller; and **no CLI argv forwards an output-token limit at all**, so the
  batch ceiling divided by a number nothing enforces and batching now requires a
  verified cap or falls back to singles. Also corrected: a per-queue lock could
  not admit `workers > 1`, detached children are not supervision, row-bounded
  claiming cannot assemble a batch, bisection contradicted `max_attempts: 3`,
  usage anchored on "the first row" double-counts under bisection, and the A/B
  measured agreement while being called accuracy.
- **Round 0 — 2026-10-19.** Initial single-pass draft. Spec taken from Chris's
  2026-10-19T14:06Z and 18:30Z messages; batch size settled at 20 in session
  after presenting the amortization curve; seat-level partition confirmed by him
  explicitly. Two of his stated requirements were found already satisfied in
  code (review gating) or mis-keyed (per-`role` partition) and are recorded as
  findings 2 and 3 rather than built as specified.

## Problem Statement

An operator submits a council job of 19,276 items and it does not finish.
Four items settled in 18 minutes of work, projecting 1,286 hours.
The operator cannot tell why from anything the system reports: the job status
reads `running`, no endpoint is halted, no call is erroring on the two seats that
matter, and the per-attempt latencies in the store say the models are slow when
they are not.

Three distinct problems produce that one symptom.

The fastest seat starves behind the slowest, because one worker drains one
shared queue for every seat and mis-accounts its concurrency budget while doing
so.
The most expensive seat spends 96% of its tokens on boilerplate, because a CLI
subscription seat pays a fixed ~7,500-token harness on every single-item call.
And the seat that fails spends its failures invisibly, because a 3-second stdin
timeout is reported as a transport error indistinguishable from a network
problem.

An operator also has no way to express what they want: there is no file in which
to say "three chairs at concurrency 8, the review stage at 4" and have the
system honour it.

## Solution

The queue is partitioned by **seat**.
A named queue in the config file maps to a set of seats and declares its own
worker count, concurrency and batch size.
The council daemon reads that file and supervises one worker process per queue;
each worker claims only its own seats' calls and owns its own budget, so no
seat can be blocked by another seat's saturation and no account can be
over-committed.

Config load **refuses** a configuration whose per-queue concurrencies sum above
the declared `max_concurrency` of any account they share.
That turns the only cross-process hazard into a startup error instead of a
runtime race, and means no inter-process coordination is needed at all.

A seat may declare `batch_size`, defaulting to 1 — bit-for-bit today's behaviour
for any seat not opted in.
Above 1, a worker sends up to that many items in one backend invocation, with a
per-item-keyed reply schema so that one reply settles many call rows, a derived
ceiling so a batch can never be sized into output truncation, and
bisect-on-failure so one poison item cannot repeatedly destroy a whole batch.

The review stage keeps its existing event-driven creation, which is already
correct: a critic call is created in the transaction that settles the last chair
for its item, never ahead of its conditions.

Three supporting corrections: the CLI prompt is written to a file and handed to
the child as its stdin, the CLI temp root comes from config instead of defaulting
to `/tmp`, and the attempt clock starts after the concurrency slot is acquired so
that queue-wait and service time are finally two different numbers.

## User Stories

1. As an operator, I want to declare four named queues in a config file — three
   chairs and one combined review queue — so that the shape of the council's
   execution is something I state rather than something I infer.
2. As an operator, I want each queue to declare its own worker count and
   concurrency, so that I can give a fast endpoint eight slots and a rate-limited
   one two.
3. As an operator, I want a config that over-subscribes an account to be
   **refused at startup** with a message naming the account, its declared
   `max_concurrency`, and the queues that oversubscribed it — so that I learn
   about it before a run rather than from a provider's 429s.
4. As an operator, I want one command to start the whole council and have it
   supervise one worker per queue, so that I do not hand-launch four processes.
5. As an operator, I want `kbu council jobs status` to report per-queue state —
   claimed, in-flight, pending — so that "nothing is moving" names which queue.
6. As an operator, I want a worker's death to return its claims to the queue
   without consuming retries, so that a restart costs throughput and not items.
7. As an operator, I want to stop one queue without stopping the others, so that
   I can take a failing seat out of service while the rest of the council runs.
8. As the engine, I want to claim only calls belonging to my own seats, so that
   I never hold a call I cannot run.
9. As the engine, I want my concurrency budget to cover exactly one account, so
   that `free slots` is a number I can compute without consulting another
   process.
10. As the engine, I want `assigned` never to exceed `capacity`, so that a
    claim can never over-commit a provider.
11. As an operator, I want a seat to be able to send many items per invocation,
    so that a fixed per-call harness is amortized rather than paid per item.
12. As an operator, I want batch size to default to 1, so that enabling batching
    is a deliberate per-seat decision and no existing seat changes behaviour.
13. As an operator, I want a batch that would exceed the seat's output budget to
    be refused or split rather than truncated, so that batching cannot silently
    corrupt results.
14. As the engine, I want one batched reply to settle each of its call rows
    independently, so that a partial reply still banks the items it answered.
15. As the engine, I want a failed batch to be retried as two halves down to
    singles, so that one unanswerable item costs one item and not twenty.
16. As the engine, I want answer reuse to stay keyed per item, so that batching
    does not destroy cross-job reuse of paid answers.
17. As an operator, I want the Claude seat's prompt delivered by file rather than
    pipe, so that a busy event loop cannot cause a 3-second stdin timeout.
18. As an operator, I want the CLI temp root to come from config, so that a
    shared host's full `/tmp` does not take down a multi-day run.
19. As an analyst, I want queue-wait and service time recorded separately, so
    that "is this seat slow" is answerable from the store.
20. As an analyst, I want a batched-vs-unbatched accuracy comparison on a sample,
    so that a 11.6x cost reduction is not bought with an unmeasured accuracy
    regression.
21. As an operator, I want the live paused job resumable under the new design
    without resubmission, so that the 104 answers already paid for are not
    re-bought.

## Implementation Decisions

### Queue configuration

`CouncilConfig` gains a `queues` mapping, sibling to `endpoints`/`models`/
`profiles` and merged by the same per-entry rule (`config.py:903`).
A `QueueConfig` entry declares:

- `seats` — the seat names this queue serves. Required, non-empty.
- `roles` — optional role restriction, for a queue that serves a seat in one
  role only.
- `workers` — how many worker processes the daemon launches for it. Default 1.
- `concurrency` — in-flight calls permitted for this queue. Required.
- `batch_size` — items per backend invocation. Default 1.
- `max_output_tokens_per_item` — the per-item output estimate used to derive the
  batch ceiling. Optional; when absent the ceiling check uses the measured
  running mean and refuses batching until it has one.

**A seat name does not bind to one account, and the validation must account for
that** (confront stall 1).
`CouncilConfig.roster(profile, overrides)` resolves seat names per job:
`overrides` is keyed by seat name and the special key `"proposers"` **replaces
the proposer bench entirely** (`config.py:725-760`).
So `proposer_1` can reach different accounts in two simultaneous jobs, including
a job persisted under an older config.
Validating only the default profile would make the no-over-commitment promise
false.

Validation at load, raising the existing config error type:

- **In queue mode, resolve EVERY declared profile** and charge each queue's full
  `concurrency × workers` reservation to **every account it can reach**. An
  account's cap is the **minimum** effective endpoint cap for that account, which
  is what the worker already computes (`engine/worker.py:362`).
- At submission, and before claiming an existing job, **reject any roster or
  override whose (seat, account, backend) mapping falls outside that validated
  set**, with a diagnostic naming the job and the seat. Leave existing calls
  untouched — a refusal must not mutate a stored job.
- **Coverage is required** (confront stall 2): queue mode requires exactly one
  owner for every executable `(seat_name, role)` in every accepted roster. There
  is **no implicit fallback worker**. An uncovered pair is refused at load, not
  discovered as a job that silently never finishes.
- **Every queue must resolve to exactly one account** across its accepted
  rosters; a multi-account queue is refused. This makes story 9 an enforced
  invariant rather than "the normal configuration".
- No seat may appear in two queues, the error naming the seat and both queues.
- For each account, `sum(queue.concurrency × queue.workers)` over the queues
  reaching it must be `<= account.max_concurrency`, the error naming the account,
  the cap, the sum and the contributing queues.
- **Each child gets `queue.concurrency` slots, NOT the account's full
  `max_concurrency`** (confront stall 2). The existing worker builds full account
  capacities; under queue mode the per-child budget is the queue's own number.
- `batch_size` is an integer **1 through 50, defaulting to 1**, and
  `batch_size > 1` requires the seat's backend to declare batch support (today:
  the two CLI kinds).

**`batch_size` lives on the QUEUE and there is no seat-level override in this
version** (confront stall 3); a queue's value applies independently to each of
its seats. 20 is the *recommended explicit opt-in* for a Claude queue, not a
default — see Q3, whose earlier wording contradicted this and has been corrected.

A config declaring no `queues` behaves exactly as today — one worker, all seats
— so this is additive and the migration is opt-in.

### Claim filtering by seat

`select_claimable()` gains optional `seats` and `roles` parameters.
When provided they add `calls.seat_name.in_(...)` and `calls.role.in_(...)` to
the existing WHERE clause.
Both columns are already non-null on `calls` (`store/schema.py`), so **no schema
migration is required** — this is a WHERE clause and one index.

A partial index on `(status, seat_name, not_before)` is added for the claim scan;
the existing `ix_calls_status_not_before` stays for the unpartitioned path.

`CouncilStore.claim_calls()` gains matching optional parameters, defaulting to
`None` so every existing caller is unchanged.

### Per-queue worker and budget

`CouncilWorker` gains `seats` and `roles` (optional) and passes them through
`_claim`.
Two budget corrections, both of which are bugs independent of partitioning:

- `_free_slots()` returns free capacity **per account**, and `_claim` requests at
  most that account's free slots, claiming per account rather than against a
  cross-account sum.
- `_claim` must not increment `assigned` past `capacity`. A row claimed when the
  account is already at capacity is released immediately by the existing
  `_release_quietly` path.

With exactly one account per queue — now an enforced invariant, not a
convention — `_free_slots()` becomes a single account's arithmetic and the
starvation is structurally impossible rather than merely corrected.
The per-child budget is `queue.concurrency`, not the account's
`max_concurrency`.

**For a batched queue, free capacity is measured in INVOCATIONS, not rows**
(confront stall 7).
The row-bounded claim and the "a batch is one in-flight unit" rule are otherwise
in direct conflict: at `concurrency: 1` a row-bounded claim yields one row
forever and a configured batch of 20 can never form, and at `concurrency: 8`
per-row assignment prevents a 20-row batch from ever being assembled.
So for a queue with `batch_size > 1`:

- claim up to `free_slots × effective_batch_size` **candidate** rows;
- group them by the complete homogeneity key;
- schedule **no more than `free_slots` batches**, and release surplus groups
  **without incrementing attempts** — nothing is wrong with those rows;
- `assigned` increments **once**, when a batch is reserved.

An unbatched queue keeps the row bound exactly as specified above.

### Execution locking

**The store-wide lock file keeps its existing inode and name, `daemon.lock`**
(confront stall 5).
Renaming it to `council.lock` would let an old binary and a new binary each take
"the" store lock and never see the other, which is a silent double-council.

`ExecutionLock` gains a partition-scoped lock file **per (queue_name,
worker_index)**, indices `0..workers-1` — not one per queue (confront stall 4).
A per-queue lock would make every child of a queue declaring `workers > 1`
contend for the same exclusive file, so only one could ever start.
A queue child holds the store-wide lock **shared** (`flock(LOCK_SH)`) *and* its
own `(queue, index)` lock **exclusively**.
Holder metadata lives in the worker lock files, and a store-wide refusal
enumerates every kernel-held worker lock.

`migrate` takes the store-wide lock **exclusively**, so it still cannot run while
any worker holds it, and `evaluate` is unchanged.

**Shared mode alone does NOT preserve the account guarantee, and this is the
part that needs a controller** (confront stall 5).
Static config validation counts the reservations of *one* configuration.
Two processes started from different queue configs, or an unpartitioned `run`
taking the whole account cap, could each acquire non-conflicting partition locks
and jointly over-commit a provider — something today's exclusive lock prevents
outright, and which independent claim tokens do nothing to stop.
So:

- queue workers are launched **only** under the single store **controller**,
  which holds a persistent exclusive controller lock;
- the controller's active queue configuration is **immutable while any of its
  workers is running**; changing it requires stop-all then start-all;
- a standalone `run` **in queue mode is refused**; unpartitioned `run`, an
  old-style `daemon`, and `evaluate` all remain **exclusive** as today, so they
  cannot coexist with a queue controller in either direction.

Rationale, recorded because it reads as a weakening and is not: double-claiming
is prevented by the claim protocol (`BEGIN IMMEDIATE`, per-row `claim_token`, and
the `status == 'pending'` guard at `store/queries.py:236`), not by this lock.
Migration safety is what the exclusive lock irreplaceably provides, and shared
mode preserves it exactly.
`LockHeld` must continue to name the holder's kind, pid and start time, and for a
partition lock it also names the partition.

### Daemon supervision

**Spawning detached children is not supervision, and today's `start()` does not
survive to supervise anything** (confront stall 6).
The existing detached child *is* the daemon, and the caller exits once it has
verified the child's lock (`daemon.py:411`), so N detached daemons would leave
no process able to restart a dead one — and story 6 (death recovery) would be
unmeetable.

**One detached controller stays alive and supervises the workers:**

- an **unexpected** worker exit triggers claim recovery through the existing
  stale-worker lease and a restart after 5 seconds, indefinitely, with the
  failure visible in `status()`;
- a **deliberate** per-queue stop disables restart for that queue until an
  explicit start, so an operator's stop is never undone by the supervisor;
- **initial start is all-or-nothing**: if any child fails readiness, every child
  started by that operation is drained and stopped and the start returns failure
  — leaving two of three queues consuming work after a partial start is a
  materially different and worse outcome than failing cleanly;
- **foreground mode runs this same controller** attached to the terminal, so the
  two paths do not diverge.

The controller launches one child per configured queue (`workers` children for a
queue declaring more than one), extending the existing `_default_child_argv`
(`daemon.py:483`) with the queue name and the worker index.
`status()` aggregates per-queue: child pid, liveness, claimed, in-flight,
pending for its seats, and last heartbeat.
`stop()` drains and stops all children, preserving the existing bounded-drain
contract — in-flight calls get up to `drain_s`, then each still-claimed call
receives an `interrupted` attempt and returns to pending **without** its retry
count moving.
A per-queue stop/start takes a queue name and leaves siblings running.

The readiness machinery (`_admits`, `_routes`, the probe clock) stays per child
and is unchanged in behaviour; each child probes only the routes its own seats
need.

### Batched invocation

A batch is a set of call rows sharing `(job, role, seat, query, query_version)`.
Homogeneity is free given the seat partition — a partitioned worker's claim is
already single-seat — and must be asserted rather than assumed.

Batch assembly:

- `n = min(batch_size, len(claimed), derived_ceiling)` where
  `derived_ceiling = floor(effective_output_cap × 0.75 / per_item_output)`.
  With fewer than `batch_size` rows available the batch is simply the rows in
  hand, which is the required "if there are fewer, take the full list".
- `per_item_output` comes from `max_output_tokens_per_item` when configured, else
  the running mean defined under "Usage attribution" below. Absent both,
  `batch_size` is forced to 1 — batching never runs on an unknown.
- A `derived_ceiling < batch_size` is logged once per seat per run, naming both
  numbers, so a silently-reduced batch is visible.
- **`derived_ceiling < 1` refuses batching for that seat** with a diagnostic
  (confront stall 8). Assembling an empty batch would leave work spinning
  forever.

**`seat.max_output_tokens` is NOT an output cap for a CLI seat, and the original
ceiling divided by a number nothing enforces** (confront stall 8).
**Neither** CLI argv forwards any output-token limit: `ClaudeCliBackend.argv`
(`llm/apis/cli.py:114-136`) passes `-p`, `--output-format`, `--json-schema`,
`--model`, `--system-prompt`, `--no-session-persistence`, `--tools ""` and
nothing else, and the codex argv (`:344`) passes `exec`, `--model`,
`--output-schema`, `-o`, `--ephemeral`, `--ignore-user-config`, `--sandbox`,
`--skip-git-repo-check` and `-`.
So the Claude question is not whether an existing argument is honoured — there is
no such argument.
Therefore:

- **Batching is enabled only for a CLI/model combination with a VERIFIED
  effective output cap and a documented mechanism for setting it.** Until that is
  verified, the combination is forced to singles and the reason is reported.
  A backend declaring batch support does **not** establish an output cap.
- `effective_output_cap` is the **lesser** of the verified cap and
  `seat.max_output_tokens`.
- A running mean is **not** a worst-case bound even with a real cap, so the
  heuristic *reduces* truncation risk rather than eliminating it; detected
  truncation still falls through to bisection. The earlier claim that a batch
  "can never be sized into output truncation" was too strong and is withdrawn.

Request shape: the rendered user content becomes a numbered list of per-item
blocks, each carrying an opaque `item_key`.
The answer schema becomes an object with an `answers` array whose elements each
carry their `item_key` plus the existing per-role answer shape.
The system prompt is unchanged apart from stating that each answer must carry
back its `item_key` and that items are independent.

Settlement goes 1:N.
`execute()` resolves each returned element to its call row by `item_key`,
validates it against the **existing per-role answer schema** (so
`_validate_answer` and `engine/normalise.py` are reused unchanged per element),
and settles that row through the existing `_settle_ok` path.
Consequences that must be handled explicitly:

**Validate the wrapper structure and the keys SEPARATELY from the role
payloads**, and strip `item_key` before the existing role-schema validation runs
(confront stall 9).
Validating the whole wrapper as one schema would reject 20 answers because one
was malformed.

- **Missing elements.** A row whose `item_key` does not appear in the reply is
  *not* an error for the batch: it is released for retry with its attempt count
  incremented, so a model that answers 18 of 20 banks 18. **Subject to a finite
  `max_attempts`** — without one, an always-omitted item consumes calls
  indefinitely. On exhaustion it settles as `error` and its item advances.
- **Keyed but invalid payload.** A valid, known `item_key` carrying a payload
  that fails the role schema is treated **like a missing answer**, not like
  today's single-reply case (which is terminal, `engine/worker.py:1198`): record
  `invalid_output` and release **only that row** with `increment_attempt=True`
  while below `max_attempts`; on exhaustion settle it as `error` and advance its
  item. Every valid, unique, known answer in the same reply is still banked.
- **Unknown elements.** An `item_key` not in the batch is dropped with a warning
  and never written.
- **Duplicate elements.** First wins; the duplicate is dropped with a warning.
- **Usage attribution — by `invocation_id`, not by "the first call row"**
  (confront stall 11). The first row may itself be missing, invalid or stale, and
  a bisected child batch can share its parent's first row, so anchoring on it
  double-counts or references an attempt that was never written. Instead: every
  backend invocation gets a unique `invocation_id`, and its reported usage,
  ordered member ids, batch size and anchor `(call_id, attempt_no)` are recorded
  **atomically, exactly once**, with endpoint spend updated once — **including
  for FAILED invocations**, whose spend today's retry path discards
  (`engine/worker.py:1394,1491,1520`). Each member attempt references the
  `invocation_id`. Usage is never divided N ways: that would invent per-item
  numbers the provider never reported, and those invented numbers feed the next
  batch's ceiling.
- **The running mean must exclude zero-cost and partial samples.** Including
  zero-cost member attempts drags the per-item estimate **down**, which
  *enlarges* the next batch — a feedback loop toward truncation. Define it as
  `total completion_tokens / total requested members` over **successful, complete**
  invocations for the same `(seat, role, served_model)`, excluding partial,
  truncated, failed, reused and unknown-usage samples. With no eligible history,
  use the configured estimate or fresh singleton observations.
- **Budget accounting.** A batch is **one** in-flight unit against
  `_AccountLimit`, not N. `assigned` moves by one per batch.

Failure handling: a batch failure that is not per-item (transport, timeout,
truncation, wrapper schema-invalid) is retried by **bisection** — split into two
halves and re-run each, recursing to single items.
A single item that fails alone fails as it does today.

**Bisection as first specified could not coexist with the attempt budget**
(confront stall 10).
`max_attempts` defaults to 3 (`engine/worker.py:282`) while a path
20→10→5→3→2→1 needs five failed ancestor invocations before a poison item is ever
invoked alone — so honouring the budget would fail healthy neighbours before
isolation, and ignoring it would abandon the budget.
`attempt_no` is also derived from `attempt_count` and is part of a primary key
(`engine/worker.py:1621`, `store/schema.py:290`), so recursion needs real counter
updates rather than bookkeeping in memory.
Therefore:

- record and increment `attempt_count` for each failed **ancestor** invocation,
  but **do not enforce the single-item `max_attempts` budget until a row is
  invoked alone**; count singleton failures separately from batch-isolation
  failures;
- within a live worker, run split halves **directly under the existing claims**,
  sequentially per reserved slot, **without returning them to the queue** — a
  re-queued half can be recombined with the poison item and loop;
- on worker death, abandon the split tree and recover claims **without an
  additional failure charge**;
- persist enough attempt metadata that singleton failures are reconstructible
  across a restart.

**Reuse stays per item.** `prompt_hash` continues to be computed from the
*single-item* rendered prompt for that call row, never from the batch.
Batching changes only the transport, so `_reusable_self` and `find_reusable` keep
working and a roster change still reuses paid answers.
This is the one place where the obvious implementation — hash what you sent — is
wrong, and it would silently destroy cross-job reuse.

### CLI transport corrections

`run_cli` takes the prompt as a **file**: the text is written to a file inside
the per-call temp directory and handed to the child as `stdin=open(path, 'rb')`,
replacing `proc.communicate(stdin_text)` (`llm/apis/cli_common.py:762`).
The child's view is unchanged — it still reads its prompt on stdin — but the
parent no longer pumps a pipe from a loop that may be busy, which is the measured
cause of "Input must be provided through stdin" in 23 of 24 attempts.
The file lives inside the existing per-call `TemporaryDirectory`, so it is
removed with it and the prompt never persists.

The temp root becomes a config value threaded to
`tempfile.TemporaryDirectory(dir=...)`.
`TMPDIR` is already in the child env allowlist (`llm/apis/cli_common.py:180`), so
the child inherits it; the change is that the *parent* stops defaulting to
`/tmp`.

### Service-time instrumentation

`started` moves to after the slot acquire (`engine/worker.py:1173-1174`), and a
second timestamp records when the call entered the slot queue.
`attempts` gains nullable `queued_at`, written by the same code that writes
`started_at`, so an existing row's meaning does not change and no version bump is
needed — the same additive-column discipline `ADDITIVE_COLUMNS` already
documents for `calls.normalised`.
Service time is `finished_at - started_at`; queue wait is
`started_at - queued_at`.

## Testing Decisions

Test external behaviour, not internals. The existing suites
(`tests/domains/ai/council/`) already establish the shape to follow:
`store_contract.py` / `test_store_contract.py` for store behaviour against both
implementations, `engine_support.py` for a scripted fake backend, and
`test_engine.py` for claim-loop behaviour.

What gets tested, and the criterion in each case:

- **Starvation, as a regression test with a number.** Two accounts, one slow and
  saturated, one fast and free; assert the fast account's calls complete while
  the slow account is still in flight. This test must *fail* on the current code
  — a test that passes before the fix is testing nothing.
- **`assigned` never exceeds `capacity`**, asserted by observing the limit object
  across a claim storm with more pending rows than slots.
- **Claim filtering**, through the store contract so both store implementations
  are covered: a worker declaring seats `{A}` never receives a `B` row.
- **Config refusal**, asserted on the message: an over-subscribed account
  produces an error naming the account, the cap and the contributing queues.
- **Shared/exclusive locking**: two partition workers coexist; `migrate` is
  refused while either holds its shared lock; a killed worker's lock is
  immediately re-acquirable (kernel release, no TTL).
- **Batched settlement**, against the scripted fake so no subscription is spent:
  N rows settle from one reply; a reply missing two `item_key`s settles N-2 and
  releases 2 with attempts incremented; an unknown key is dropped; a duplicate
  key takes the first; usage is recorded once.
- **Bisection**, with a fake that fails any batch containing a designated poison
  item: assert the other items settle and the poison item consumes its own
  attempts rather than the batch's.
- **Derived ceiling**, asserted as a refusal: a seat whose per-item output
  estimate is unknown runs at `batch_size` 1, and a configured ceiling below
  `batch_size` reduces the batch and logs both numbers.
- **Reuse under batching**: an item answered inside a batch is afterwards
  reusable by `prompt_hash` from a *different* job, which is the property that
  would silently break.
- **stdin from file**: assert the child receives the prompt and that the prompt
  never appears in `argv` — extending the existing argv assertions in
  `tests/domains/ai/council/test_cli_backends.py`, which exist to keep a CLI seat
  off metered billing and must not regress.

Pre-existing failures: the suite carries failures unrelated to this work.
Every success criterion is therefore framed as "no test that passed on the base
commit fails on the branch", never as a green suite.

## Open Questions and Judgement Calls

Every entry below is **already decided and reflected in the PRD body and
taskplan**. The PRD is buildable as it stands. To change a decision, edit its
`DECIDED:` line and the body text it governs.

### Q1. Does weakening the store-wide execution lock from exclusive to shared risk store corruption? -- DECIDED: shared for workers, exclusive for migrate; the claim protocol, not the lock, prevents double-claiming
**Blast radius:** IRREVERSIBLE
**Why:** The lock's docstring gives two reasons, and they are not equally
load-bearing. Double-claiming is prevented by `BEGIN IMMEDIATE` plus a per-row
`claim_token` plus a `status == 'pending'` guard (`store/queries.py:236`), which
is a correctness property of the claim itself and holds for any number of
workers. Migration safety has no such backstop, which is why `migrate` keeps an
exclusive lock. `flock` expresses exactly this with `LOCK_SH`/`LOCK_EX`.
**Amended after confront (stall 5), and this amendment is the load-bearing
half:** shared mode alone does **not** preserve the no-over-commitment promise.
Static config validation counts one configuration's reservations, so two
processes from different queue configs — or an unpartitioned `run` taking a whole
account cap — could hold non-conflicting partition locks and jointly
over-commit a provider, which the exclusive lock prevents outright and which
claim tokens do nothing to stop. The shared lock is therefore permitted **only**
under a single store controller holding an exclusive controller lock, with its
queue configuration immutable while any worker runs, and standalone `run` in
queue mode refused. The store-wide lock also **keeps its existing `daemon.lock`
name**: renaming it would let an old and a new binary each take "the" store lock
and never see each other.
**If you disagree:** the whole design collapses to one process. The alternative
is in-process per-seat task groups instead of processes, which keeps one lock but
abandons the event-loop isolation that motivated separate processes — the Claude
stdin stall was caused by store I/O blocking the loop that fed the subprocess, so
a single-process design keeps that failure mode alive. Tasks `t1` and `t7` would
be replaced.
**Confidence:** high on the claim-protocol reasoning (readable, and already
tested by `test_store_contract.py`); the over-commitment hole was **missed in the
first draft** and is only closed by the controller constraint above — which is
the single best argument in this document for having run the confront.

### Q2. Is batching safe for a closed-menu classification task, or does cross-item contamination degrade accuracy? -- DECIDED: ship batching, and gate the full-corpus run on a measured sample A/B
**Blast radius:** IRREVERSIBLE (for corpus results, if wrong and unmeasured)
**Why:** Twenty independent classifications in one context can influence each
other, and there is no way to reason this out — it is an empirical question about
a specific model and prompt. Making the pipeline 11.6x cheaper and quietly less
accurate is the worst outcome available, and it would not announce itself.
Gating the *operator run* rather than the *code* costs no engineering throughput.
**Specified after confront (stall 12), because "run an A/B" is not a buildable
instruction and agreement is not accuracy:** the deliverable is a reproducible
report over **100 fixed-seed sampled `go_mf` items**, covering the critic and
meta roles against **identical frozen upstream context**, comparing normalised
per-role decisions between a singleton run and a batch-20 run, and reporting
disagreement, validity, latency and measured usage. This measures **agreement
with the unbatched pipeline, NOT accuracy against truth** — calling it accuracy
would be claiming a scientific gate nobody has labelled data for. **Code may land
with batching opt-in disabled** when live measurement is unavailable, so an
unverifiable CLI cap never blocks the build. **An operator approves the report
and the measured CLI cap before batching is enabled for the corpus run** — it is
never authorised automatically.
**If you disagree:** drop `t10-batching-accuracy-ab` from the taskplan and the
corpus run proceeds on arithmetic alone. Chris was offered the either/or
explicitly and did not take it, so this is my call and the cheap one. If you want
a genuine *accuracy* gate rather than an agreement gate, that needs labelled
`go_mf` items and a numerical pass threshold, and neither exists in this repo
today.
**Confidence:** medium -- that contamination is *possible* is near-certain; its
*magnitude* on `go_mf` is unknown and only the comparison resolves it.

### Q3. Batch size: 50 as originally proposed, or 20? -- DECIDED: `QueueConfig.batch_size` defaults to 1 and accepts 1-50; 20 is the recommended explicit opt-in for a Claude queue
**Blast radius:** LOW
**Why:** Per-item input is `7500/n + 300`, so n=20 captures 11.6x of a maximum
21x while 20→50 buys only 1.5x more. The costs of 50 are not proportional: a
3x larger failure blast radius against the seat measured at 23/24 failures, and
an output ceiling of ~48 K that nobody has verified the CLI honours. Chris
proposed ~50, then observed that 20 would already reduce overheads
significantly; the curve agrees with him.
**Corrected after confront (stall 3):** the earlier wording of this line said
"20 by default", which directly contradicted `QueueConfig.batch_size`'s declared
default of 1 — an implementer could not satisfy both, and a default of 20 would
have changed existing invocation behaviour and spend for any seat merely by
upgrading. The default is **1**; 20 is what an operator writes for a Claude
queue. There is also **no seat-level `batch_size`** in this version — the field
is on the queue and applies to each of its seats independently, which matters for
the combined critic/meta queue.
**If you disagree:** change one config value in the deployed config, not a code
default. The derived ceiling means a larger value is reduced or refused rather
than truncated, so a wrong choice here is safe.
**Confidence:** high -- the arithmetic is simple and the diminishing return is
steep.

### Q4. Is the partition key the seat, the endpoint, or the account? -- DECIDED: seat for the queue, account for the budget
**Blast radius:** MEDIUM
**Why:** The three differ in this roster — five seats, three endpoints, three
accounts — and the thing with "wildly different properties" is the seat's model
and call mechanism, which is what Chris asked to isolate and what he confirmed
explicitly. But concurrency is owned by the provider account, so the budget must
be per account or two codex queues would open 16 children against a cap of 8.
Keeping them separate, with config-load validation bridging the two, is what
makes cross-process coordination unnecessary.
**If you disagree:** partitioning on account instead gives three queues and makes
the budget trivially correct with no validation rule, at the cost of putting both
codex chairs in one queue. The validation rule in `t5` and the `seats` field in
`QueueConfig` would both go away.
**Confidence:** high -- confirmed by Chris in session, and the roster table makes
the three-way distinction concrete.

### Q5. How is one provider usage record attributed across N batched items? -- DECIDED: once per `invocation_id`, including for failed invocations; never divided N ways
**Blast radius:** MEDIUM
**Why:** The provider reports one number for the invocation. Dividing it N ways
would write per-item token counts that were never measured, and those numbers
would then be used to derive the next batch's ceiling — a fabricated input to a
real decision. Recording it once, attributed, keeps every stored number true.
**Corrected after confront (stall 11):** the first draft anchored the record on
"the batch's first call row", which breaks three ways — that row may itself be
missing, invalid or stale; a bisected child batch can share its parent's first
row and double-count; and the referenced attempt may never have been written.
The anchor is now a unique `invocation_id` carrying the ordered member ids, the
batch size and an explicit `(call_id, attempt_no)`, recorded atomically exactly
once and **also for failed invocations**, whose spend today's retry path simply
discards. Separately, the running mean must **exclude** zero-cost member attempts
and partial/truncated/failed/reused samples: including them drags the per-item
estimate down, which *enlarges* the next batch — a feedback loop pointed straight
at the truncation this heuristic exists to avoid.
**If you disagree:** per-item division makes `select_pending_call_facets` and any
per-item cost report read naturally, at the cost of inventing data. The batch
ceiling derivation in `t8` would need a separate honest source for per-item
output.
**Confidence:** medium -- correct in principle; it does make "what did this item
cost" unanswerable without a join, and a cost report may later want a documented
convention.

### Q6. Should the review stage get the monitoring script Chris asked for? -- DECIDED: no; `advance()` already gates it correctly and event-driven beats polled
**Blast radius:** LOW
**Why:** `decide()` returns `waiting` until all proposers settle and `advance()`
creates the critic row inside the settling transaction. The live store confirms
it: 24 critic rows for the ~24 items whose three chairs had both settled. A
polling monitor would be strictly worse — a poll interval of added latency and a
non-atomic read of state that is currently transactional. The supervision Chris
actually lacks is per-queue worker launching, which `t7` builds.
**If you disagree:** nothing in the build changes; this is a decision not to add
a component. If a monitor is wanted for observability rather than correctness, it
is a reporting feature over `status()`.
**Confidence:** high -- directly read from source and corroborated by the
24-row count.

### Q7. Where does the ~100% critic rate get addressed? -- DECIDED: out of scope here; its own design session against `go_mf`
**Blast radius:** LOW (for this PRD; the cost itself is large)
**Why:** It is the biggest cost term in the pipeline — the expensive Claude stage
firing on every item instead of only on disagreement — but the levers are prompt
and schema design (`required` lists, menu construction, whether a 2-of-3 rule
should settle), not queueing. Bundling it would make both harder to review and
would put a query-semantics change in a throughput PRD.
**If you disagree:** the taskplan gains a query-design phase and the PRD's scope
roughly doubles. Nothing already specified changes.
**Confidence:** high -- the two problems share no code.

### Q8. Does `attempts` need a schema version bump for `queued_at`? -- DECIDED: no; additive nullable column, same discipline as `calls.normalised`
**Blast radius:** LOW
**Why:** The column is nullable with no default and is written by code that reads
its absence as "nothing to say", so adding it cannot change the meaning of an
existing row. `ADDITIVE_COLUMNS` already documents exactly this case and
`migrate()` adds missing ones. A version bump would refuse to open every existing
store, including the live one holding 104 paid answers.
**If you disagree:** a bump means a migration path for the live store before the
paused job can resume, which conflicts directly with story 21.
**Confidence:** high -- the precedent is in the schema module and the rule is
written there.

### Q9. Should batching be enabled for codex and argo too? -- DECIDED: per-seat config knob, default 1; opt in codex later on evidence, leave argo alone
**Blast radius:** NONE
**Why:** Codex is also a CLI with its own harness and would benefit similarly,
but it is succeeding 51/51 today and is not the bottleneck; changing it now adds
risk for no measured need. Argo is HTTP with no harness overhead, so batching
buys it nothing and costs it blast radius. A default of 1 means no seat changes
behaviour unless named.
**If you disagree:** set `batch_size` on those seats in config. No code changes.
**Confidence:** high -- the default makes this reversible per seat.

### Q10. What I could not decide and did not guess
Three external facts are unresolved, and each is handled by making the code
fail loudly rather than by assuming an answer:

- **Whether a CLI seat can be given an output cap AT ALL, and what the harness
  floor actually is across argv variants.** `claude` could not be executed in the
  authoring session (permission gate). The confront then established something
  worse than uncertainty: **neither** CLI argv forwards any output-token limit
  today (`llm/apis/cli.py:114` and `:344`), so this is not a question about
  whether an existing flag is honoured — there is no flag, and `seat.max_output_tokens`
  reaches no CLI child. `t8` must establish a verified effective cap and a
  documented mechanism for setting it, and **force singles until it has one**.
- **The per-item payload and output size for the *critic* role**, whose prompt
  carries ballots and votes and is therefore larger than the proposer's measured
  ~1,200 characters. Batch size is derived at runtime from measured per-item
  output, so the critic's effective batch may legitimately settle below 20.
- **Whether 24/25 non-unanimous is the true disagreement rate or an artifact of
  the first 25 items.** The sample is small and unrepresentative by construction
  (it is whatever the FIFO reached first). The number is reported as measured and
  is not used to size anything in this build.

## Gotchas and Unintuitive Consequences

- **Fixing the starvation makes the Claude failure rate matter more, not less.**
  Today Claude's 96% transport failure is partly hidden because the queue barely
  reaches it. With four queues running concurrently the review queue will be fed
  at full rate from the start, so if the stdin fix is incomplete the run fails
  faster and louder than before. `t4` is therefore not a side-fix — it is a
  precondition for the partition being an improvement.
- **Per-queue workers multiply the CLI process count on a shared host.** Three
  chairs at concurrency 8 plus review at 4 is up to 28 concurrent CLI children,
  each a node process. Poplar is shared with other users and was observed at load
  average 329–376 during unrelated work. The config validation caps per *account*,
  which is not the same as capping total process count on the box — an operator
  can satisfy every account cap and still oversubscribe the machine.
- **Batching makes a single bad item expensive before bisection finds it.** A
  poison item in a 20-batch costs the whole batch's tokens on the first attempt,
  then half, then a quarter. Worst case is ~2x the tokens of the batch to isolate
  one item. Cheaper than 20 individual calls, but not free, and the cost appears
  as mysterious usage against items that were answered fine.
- **Batched usage attribution makes per-item cost unanswerable without a join.**
  Q5's decision is correct but has this consequence: any existing report reading
  per-call `fresh_usage` will see one row carrying a batch's entire cost and N-1
  rows carrying zero. A naive per-item cost chart will look wrong rather than
  fail.
- **The service-time fix changes what every historical latency number means.**
  After `t3`, `finished_at - started_at` is service time; before it, the same
  expression over the same column was wait+service. Any trend line spanning the
  change will show an apparent 65x speedup that is purely instrumental. The new
  `queued_at` being NULL on old rows is the only marker distinguishing them.
- **A seat appearing in two queues is a double-claim, which is why config refuses
  it.** This is not obvious: the claim protocol prevents two workers taking the
  same *call*, so two queues on one seat would not corrupt anything — they would
  simply race, each getting an arbitrary half, with both queues' concurrency
  counted against the account twice. It fails as budget over-commitment, not as
  corruption, which is much harder to diagnose.
- **The shared lock means a migration can now be blocked by a queue nobody is
  watching.** With one holder, `LockHeld` named one pid. With N shared holders,
  `migrate` must report all of them or an operator will kill one worker, retry,
  and be refused again. `t1` must surface every shared holder, not the first.
- **Resuming the live paused job exercises the batched path against rows created
  by the unbatched one.** Story 21 and the 92 stale claims interact: those claims
  recover by themselves via `reclaim_stale()` on a 10-minute lease
  (`engine/worker.py:799`), but they will be re-claimed by the *new* partitioned
  workers, which is the first real test of the claim filter against
  production-shaped data. Expect the resume itself to be the acceptance test.
- **`decided_by="unanimous"` never firing means the cheap path is untested in
  production.** Everything measured so far went through critic and meta. If a
  query change later makes unanimity common, that path will be exercised for the
  first time at scale.

## Sources Consulted

Source files, with the symbols that mattered:

- `src/kbutillib/domains/ai/council/engine/worker.py` — `work()` claim loop
  (`:757`), `_free_slots` (`:901`), `_claim` with the unconditional `assigned`
  increment (`:923`), `_run_one` (`:937`), `_release_quietly` (`:929`),
  `_AccountLimit` (`:187`), `_build_limits` (`:362`), `_invoke` with `started`
  outside the slot (`:1173`), truncation routing to `_fail_call` (`:1181`),
  `advance()` (`:1652`), `_finish_job_if_done` (`:1766`), `reclaim_stale` call
  site (`:799`).
- `src/kbutillib/domains/ai/council/store/queries.py` — `ROLE_ORDER` (`:65`),
  `role_rank` (`:87`), `select_claimable` and its ordering and absent quota
  (`:164`), `claim_update` and the `status == 'pending'` guard (`:236`),
  `release_call_update`, `reclaim_stale_update` (`:381`).
- `src/kbutillib/domains/ai/council/store/schema.py` — `SCHEMA_VERSION`,
  `ADDITIVE_COLUMNS` and its stated rule, the `calls` table confirming `role` and
  `seat_name` are existing non-null columns, `ix_calls_status_not_before`, the
  partial unique index on live calls, the `attempts` table columns.
- `src/kbutillib/domains/ai/council/engine/decide.py` — `need_critic` (`:326`),
  the unanimity test and `decided_by="unanimous"` (`:346-357`), `need_meta`
  (`:374`).
- `src/kbutillib/domains/ai/council/daemon.py` — the five load-bearing properties
  in the module docstring, `_DaemonWorker._claim` override and `_return_unrun`
  (`:216-292`), `start()` (`:411`), `_default_child_argv` (`:483`), `stop()` and
  the bounded drain (`:524`), `_interrupt_claimed` (`:779`), `_admits` (`:938`),
  `_routes` (`:1165`).
- `src/kbutillib/domains/ai/council/execlock.py` — `LOCK_FILENAME` (`:55`),
  `LOCK_KINDS` (`:61`), `acquire()` and the `LockHeld` holder record (`:114`).
- `src/kbutillib/domains/ai/council/config.py` — `EndpointConfig` (`:549`),
  `account` and `max_concurrency` (`:573-574`), the default concurrency fallback
  (`:634`), `CouncilConfig.endpoints` (`:679`), the per-entry merge rule for
  `endpoints`/`models`/`profiles` (`:903`).
- `src/kbutillib/domains/ai/llm/apis/cli.py` — `ClaudeCliBackend.argv` with
  `--tools ""`, `--system-prompt` and the deliberately-absent `--bare` /
  `--dangerously-skip-permissions` (`:76-136`), `_run` (`:138`).
- `src/kbutillib/domains/ai/llm/apis/cli_common.py` — the env allowlist including
  `TMPDIR` (`:180`), `run_cli` signature and its cwd rationale (`:698,713`), the
  per-call `TemporaryDirectory` (`:743`), `proc.communicate(stdin_text)` (`:762`).
- `src/kbutillib/domains/ai/council/backends/cli_common.py` — the re-export shim
  and why `_child_env` is re-exported (billing proof).

Live production data (`/scratch/chenry/council/council.sqlite`, read-only):

- `jobs` — the five jobs and the roster of `96a612a03a2149c5935478d519f9f522`
  (three chairs on argo/codex/codex, critic and meta both on claude), options
  `allow_unverified: true`.
- `calls` grouped by endpoint/seat/status — 92 claimed against ~20 slots, codex
  holding 66; 52/26/25 chair successes; 24 critic rows; 3 critic errors, 1 ok.
- `items` — 1 ok, 3 error, 19,272 pending.
- `attempts` joined to `calls` — per-seat elapsed distributions (codex median
  345–352 s, argo 127 s, claude 112 s), outcome kinds (codex 51/51 ok, claude
  23/24 transport), and the one Claude usage record
  `{"prompt_tokens": 2, "completion_tokens": 667}`.
- `query_types` — `go_mf` at versions 1/2/3 and `reaction_pickup`, with spec
  lengths 7,844 / 8,104 / 8,269 / 15,232 characters, establishing that the schema
  is not the token floor.

Also read and found uninformative or merely confirmatory:

- `backends/http.py`, `backends/http_common.py`, `backends/cli.py` — thin
  re-export shims; the real implementations are under `llm/apis/`.
- `tests/domains/ai/council/` listing — used to identify the suites that
  establish test shape (`store_contract.py`, `engine_support.py`,
  `test_engine.py`, `test_cli_backends.py`, `test_execlock.py`); individual test
  bodies were not read.
- `maestro worker list` — all workers report `runner: max`, which initially
  looked like no codex pool was available; eight recent `backend: codex`
  researcher tasks (2026-10-18, including two confront runs that produced
  `## STALL REPORT` artifacts) establish that codex dispatch does work and that
  the `runner` column describes the supervisor default, not the accepted
  backends.

Memory nodes: the prior session's episode
`mem_01M5ADW4FMTC80AJEJ3160C8J3` (its per-endpoint worker proposal is superseded
here by per-seat, and its 5.3 s codex figure is reconciled against the store in
finding 9); `poplar-gpus-held-by-fliu-vllm-at-0point9` (nine on-demand vLLM
models, both H100s measured near-empty — the basis for the local-model option in
Out of Scope); `argo-allowance-cut-to-350-month`; `codex-on-h100-is-a-standalone-musl-binary`.

Session instruction: Chris's messages of 2026-10-19T14:06:27Z (the four-queue
architecture, the three questions), 18:30Z (seat-level partition confirmed,
batching proposed at ~50, review-gating requirement, daemon-from-config
requirement) and 18:41Z (20 is sufficient).

## Out of Scope

- **The ~100% critic rate and the `go_mf` query design.** The largest cost term,
  deliberately separated — see Q7. Its own `d` session.
- **Moving the critic/meta seats off the Claude CLI.** Three alternatives were
  costed and all are deferred: a local vLLM model on poplar (both H100s measured
  near-empty, nine models on demand, zero marginal token cost, but ~84 GiB per
  card and a ~17-minute cold load on another user's box); the metered Anthropic
  API with prompt caching (~$300–600 of real money versus ~$1,270 of Max
  allowance, and the CLI seat exists precisely to avoid metered billing); and
  ARGO (capped at $350/month and already carrying a chair). Batching reduces the
  pressure that motivated all three, which is why none is needed now.
- **Chasing a prompt-cache hit on the CLI harness.** Finding 6: paying the floor
  1,928 times instead of 38,552 removes the motive. The suspected invalidator —
  a fresh temp directory per call appearing in the CLI's environment preamble —
  is recorded here in case batching under-delivers.
- **The four pre-existing defects, dev 49/50/51/52.** Independent of this design
  and tranche-ready now; the caller half of 51 already landed on `main` at
  `e97acd5`. They go to an `e` session.
- **Relaunching job `96a612a03a2149c5935478d519f9f522`.** The job is alive and
  resumable by `job_id` and must not be resubmitted; relaunching it before this
  work lands reproduces the 1,286-hour projection. The resume is an operator step
  after deploy, not a task here.
- **Cross-machine council execution.** A store is one machine's local SQLite file
  and this design does not change that; nothing here coordinates across hosts.
- **Capping total CLI child processes per machine.** Named as a gotcha; the
  validation added here caps per account, not per box.

## Further Notes

The `queues` config is additive and a config without it behaves exactly as today,
so the deploy is not a cutover: the new code can land and run unpartitioned,
and the four-queue configuration becomes a separate, reversible config change.
That matters because the live store holds 104 paid answers and a paused 19,272-item
job, and story 21 requires resuming it rather than re-buying them.

Two numbers worth carrying into the resume: the 92 stale claims recover by
themselves on a 10-minute lease via `reclaim_stale()`, and the answers are
reusable by `prompt_hash` if the roster changes — which is exactly the property
Q5's per-item hashing decision protects.

## Acceptance Criteria

1. Queue-mode config validation resolves every declared profile and charges each queue's `concurrency × workers` to every account it can reach, using the minimum effective endpoint cap for that account.
2. A roster or per-job override whose (seat, account, backend) mapping falls outside the validated set is rejected at submission and before claiming an existing job, with a diagnostic naming the job and the seat, and no stored call is mutated by the refusal.
3. Queue mode requires exactly one owner for every executable (seat_name, role) in every accepted roster; an uncovered pair is refused at config load and there is no implicit fallback worker.
4. A queue resolving to more than one account is refused at config load.
5. Each queue child is given `queue.concurrency` slots, not the account's `max_concurrency`.
6. `QueueConfig.batch_size` defaults to 1, accepts 1 through 50, lives on the queue with no seat-level override, and applies independently to each of the queue's seats.
7. The store-wide lock retains the existing `daemon.lock` name and inode.
8. Worker locks are exclusive per (queue_name, worker_index) for indices 0..workers-1, held alongside a shared store-wide lock, so a queue declaring `workers: 2` starts two children.
9. A store-wide lock refusal enumerates every kernel-held worker lock, not the first.
10. Queue workers are launched only under a single store controller holding an exclusive controller lock; standalone `run` in queue mode is refused; unpartitioned `run`, old-style `daemon` and `evaluate` remain exclusive.
11. The controller's queue configuration is immutable while any of its workers is running; changing it requires stop-all then start-all.
12. One detached controller remains alive and supervises workers: an unexpected worker exit triggers claim recovery under the existing stale-worker lease and a restart after 5 seconds, indefinitely, visible in `status()`.
13. A deliberate per-queue stop disables restart for that queue until an explicit start.
14. Initial start is all-or-nothing: if any child fails readiness, every child started by that operation is drained and stopped and the start returns failure.
15. Foreground mode runs the same controller attached to the terminal.
16. For a batched queue, free capacity is measured in invocations: up to `free_slots × effective_batch_size` candidate rows are claimed, grouped by the complete homogeneity key, and no more than `free_slots` batches are scheduled; surplus groups are released without incrementing attempts; `assigned` increments once per reserved batch.
17. An unbatched queue retains the per-row `free_slots` claim bound.
18. Batching is enabled only for a CLI/model combination with a verified effective output cap and a documented mechanism for setting it; until verified, that combination runs singles and reports why.
19. `effective_output_cap` is the lesser of the verified cap and `seat.max_output_tokens`.
20. A `derived_ceiling` below 1 refuses batching for that seat with a diagnostic rather than assembling an empty batch.
21. Wrapper structure and item keys are validated separately from role payloads, and `item_key` is stripped before the existing role-schema validation runs.
22. Every valid, unique, known answer in a reply is banked even when sibling elements are missing or malformed.
23. A keyed-but-invalid payload records `invalid_output` and releases only that row with `increment_attempt=True` while below `max_attempts`, settling as `error` and advancing its item on exhaustion.
24. Missing answers follow the same finite `max_attempts` rule, so an always-omitted item cannot consume calls indefinitely.
25. Each backend invocation has a unique `invocation_id` whose reported usage, ordered member ids, batch size and `(call_id, attempt_no)` anchor are recorded atomically exactly once, with endpoint spend updated once, including for failed invocations.
26. Every member attempt references its `invocation_id`, and zero-cost references are excluded from output estimation.
27. The per-item output mean is `total completion_tokens / total requested members` over successful complete invocations for the same (seat, role, served_model), excluding partial, truncated, failed, reused and unknown-usage samples.
28. Bisection records and increments `attempt_count` for each failed ancestor invocation but does not enforce the single-item `max_attempts` budget until a row is invoked alone, counting singleton failures separately.
29. Split halves run directly under the existing claims, sequentially per reserved slot, and are never returned to the queue where they could recombine with the poison item.
30. On worker death the split tree is abandoned and claims recover without an additional failure charge, with enough attempt metadata persisted to reconstruct singleton failures across a restart.
31. `prompt_hash` is computed from the single-item rendered prompt, and an item answered inside a batch is reusable by `prompt_hash` from a different job.
32. The CLI prompt is delivered by file as the child's stdin, never appears in `argv`, and its file does not exist after `run_cli` returns.
33. `attempts.queued_at` is added as a nullable additive column with no `SCHEMA_VERSION` bump, and `started_at` is stamped after the concurrency slot is acquired.
34. Code may land with batching opt-in disabled when live CLI measurement is unavailable.
35. The A/B deliverable is a reproducible report over 100 fixed-seed sampled `go_mf` items covering critic and meta against identical frozen upstream context, comparing normalised per-role decisions between singleton and batch-20 runs and reporting disagreement, validity, latency and measured usage; it is reported as agreement with the unbatched pipeline, not as accuracy against truth.
36. Enabling batching for the corpus run requires an operator to approve that report and the measured CLI cap; it is never authorised automatically.
37. A config declaring no `queues` behaves exactly as on the base commit.
38. No test that passed on the base commit fails on the branch.
