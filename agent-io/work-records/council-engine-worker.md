# Work Record — council-engine-worker

## task_id
council-engine-worker

## branch
maestro/developer/background-reference-only-agent--council-engine-worker

## base commit
d8a8573 (`docs(work-record): fill in the record's own commit sha`)

## commit_shas
- 06a6aea — feat(council): the decision rule as one pure function
- 6756569 — feat(council): the worker -- submit, claim, execute, advance
- e477948 — test(council): halts, budgets, shared-account limits and the real GO council
- e5e5fc1 — test(council): cancellation, stale claims, priority, stale digests, order
- bb02502 — test(council): cover both halves of the answer-schema check
- 401420b — docs(work-record): record the council engine milestone

## summary

The council's engine: `src/kbutillib/domains/ai/council/engine/` with
`decide.py` (the whole adjudication rule as one pure function) and `worker.py`
(`CouncilWorker` — submit, work, execute, advance — plus `evaluate`).  97 new
tests in `tests/domains/ai/council/test_engine.py` drive it against a real
`SqliteStore` and scripted fake backends, including one end-to-end run of the
**shipped** `go_mf` query type seeded into the store with
`QueryRegistry.seed_builtins()`.

No test makes a network call, spends a token, needs a CLI login or needs a GPU.

Byte-identical to the base commit (`git diff --stat d8a8573 -- <each>` is
empty for all of them): `domains/ai/argo_utils.py`,
`domains/ai/ai_curation_utils.py`, `council/types.py`, `council/config.py`,
`council/queries/` (whole tree), `council/backends/` (whole tree),
`council/store/schema.py`, `council/store/errors.py`,
`council/store/__init__.py`, `pyproject.toml`.

## files_touched

New:
- `src/kbutillib/domains/ai/council/engine/__init__.py` — re-exports
  `decide`, `tally`, `critic_approves`, `SettledCall`, `CouncilWorker`,
  `evaluate`, `aevaluate`.
- `src/kbutillib/domains/ai/council/engine/decide.py` (556 lines) — the rule,
  `SettledCall`, `tally()`, `critic_approves()`.
- `src/kbutillib/domains/ai/council/engine/worker.py` (1634 lines) —
  `CouncilWorker`, `evaluate`/`aevaluate`, `renderer_id_version`,
  `_validate_answer`.
- `tests/domains/ai/council/engine_support.py` (372 lines) — the shared
  roster/query-type/loop scaffolding, `InstrumentedBackend`, `InFlight`.
- `tests/domains/ai/council/test_engine.py` (2226 lines, 97 tests).
- `agent-io/work-records/council-engine-worker.md` (this file).

Modified (the two store additions the previous milestone flagged as this
one's to close, and the consequence of adding them):
- `src/kbutillib/domains/ai/council/protocols.py` (+21) — declares
  `CouncilStore.note_call_error` and `CouncilStore.set_job_status`.
- `src/kbutillib/domains/ai/council/store/queries.py` (+36) —
  `note_call_error_update`, `set_job_status_update` (dialect-neutral, so
  `PostgresStore` inherits them).
- `src/kbutillib/domains/ai/council/store/sqlite.py` (+53) — the two methods.
- `tests/domains/ai/council/test_store_contract.py` (+5) —
  `CONTRACT_METHODS` grew by exactly those two names; that test asserts
  `dir(CouncilStore) == CONTRACT_METHODS`, so adding to the protocol
  necessarily edits it.

`tests/domains/ai/council/conftest.py` needed no edit: the engine tests use
its existing `store` / `make_store` fixtures, so they are parametrised over
both store implementations and will run against PostgreSQL the moment
`PostgresStore` lands.

## the two store additions (and why they are not scope creep)

`council-store-queue-ops.md` ("inferred rather than verified") left exactly
two things to the engine, and the dispatcher's note repeated them:

1. **Nothing in the store writes `calls.error` on a PENDING call.**
   `release_call`'s signature is fixed in `protocols.py` and takes no error;
   `complete_call` moves the row out of `pending`.  But `job_status` reads
   that column on pending rows to report `waiting_for_model` and
   `configuration_mismatch`.  Added `note_call_error(call_id, error)` — one
   column, cannot settle a call, cannot change whose claim it is under.  The
   worker calls it inside the SAME `store.transaction()` as the release,
   which `transaction()`'s re-entrancy exists to permit.
2. **Nothing sets job status `running` or `done`.**  Added
   `set_job_status(job_id, status, only_if=None)`.  The guard is what keeps
   the transition off a cancelled job: `queued -> running` on the first claim
   passes `only_if=("queued",)` and `-> done` passes
   `only_if=("queued", "running", "paused")`.

Both are declared on the protocol and built in `store/queries.py` rather than
inline in `sqlite.py`, following that module's own rule — a queue statement
both stores must agree on lives there, or the two implementations drift.

## environment

The supervisor had provisioned an EMPTY `.task-venv/` in the worktree, and
`python` resolves to it.  Installed into THAT venv only; no shared or service
venv was touched, and no `ssh`, live host, systemd unit or database was
involved:

```
python -m pip install --upgrade pip
python -m pip install -e '.[council]'                          # SQLAlchemy 2.1.4, psycopg 3.3.6
python -m pip install pytest pytest-asyncio pytest-cov ruff    # pytest 9.1.1
python -m pip install -e '.[dev,ai]'                           # pandas, duckdb, httpx, ipykernel
```

CPython 3.12.3, pydantic 2.14.1, jsonschema 4.10.3 (pulled in transitively;
it is NOT a dependency of this package, which is why `_validate_answer` has a
fallback and why that fallback has its own test).  A reviewer must install the
same way in its own venv; an `ImportError` there is an environment gap, not a
result.

## verification

| Command | Exit | Result |
|---|---|---|
| `python -m pytest tests/domains/ai/council/ -q -p no:cacheprovider` (base `d8a8573`, clean tree) | 0 | 343 passed, 190 skipped |
| `python -m pytest tests/domains/ai/council/ -q -p no:cacheprovider` (branch) | 0 | **440 passed, 243 skipped** |
| `python -m pytest tests/domains/ai/council/test_engine.py -q` (branch) | 0 | **97 passed, 53 skipped** |
| `python -m pytest tests/domains/ai/council/test_engine.py -q`, 4 further repeat runs | 0 | 97 passed every time |
| `python -m pytest tests/domains -q -p no:cacheprovider` (branch) | 0 | 696 passed, 243 skipped |
| `python -m ruff check --output-format=concise src/kbutillib/domains/ai/council tests/domains/ai` | 0 | All checks passed |
| full suite, slice A, base `d8a8573`, clean tree | 1 | 26 FAILED nodeids captured (the run's output was piped to `grep`, so only the names were kept) |
| full suite, slice A, branch | 1 | 26 failed, 3572 passed, 343 skipped — **the same 26 nodeids** |
| full suite, slice B, base `d8a8573`, clean tree | 0 | 1018 passed, 329 skipped |
| full suite, slice B, branch | 0 | 1018 passed, 329 skipped |

**No regressions, compared BY NAME against a base run taken in this venv on a
clean tree BEFORE any file was written** (per the dispatcher's note that
`build-baselines/kbutillib.json` is stale — captured 2026-09-13 at `0f84803`
in a differently-provisioned venv).  `comm -13` (new on branch) and `comm -23`
(gone on branch) over the two sorted lists are both EMPTY.  All 26 are
pre-existing failures in `tests/cli/test_kind.py` (17),
`tests/cli/test_clearinghouse.py` (6),
`tests/cli/test_clearinghouse_operator.py` (2) and
`tests/core/test_composition_smoke.py` (1) — none in `tests/domains/`, none in
a file this task touched.  (My `grep -E "^(FAILED|ERROR)"` also caught three
`ERROR` LOG lines about a missing `modelseedpy`, identically in both runs;
they are log output, not test errors, which is why the two files hold 29 lines
for 26 failures.)

The suite was run in two slices so each `Bash` call stayed under 600 s
(~92 s and ~63 s).  Slice A is `tests/annotators arc_context berdl
beril_worktree biochem cli core domains`, slice B is everything else except
`tests/notebook`; the per-file ignores are copied from
`build-baselines/kbutillib.json`'s `command`.  Every run was in the
foreground; nothing was backgrounded.

I did **not** rewrite `build-baselines/kbutillib.json`.  One exists, so the
task instruction's "if it does not exist" branch does not apply, and
overwriting a shared cache with numbers from a fourth differently-provisioned
venv would not help the next task any more than it helped this one.

## success_criteria_check

**Criterion 1.** PASS.

`engine/decide.py` implements `decide()` per the rule (`decide.py:236`), and
`engine/worker.py` implements `CouncilWorker` with `submit`/`asubmit`
(`worker.py:332`/`356`), `work` (`worker.py:576`), `execute`
(`worker.py:821`) and `advance` (`worker.py:1228`) plus
`aevaluate`/`evaluate` (`worker.py:1579`/`1625`) over the `CouncilStore`
protocol.

Every listed decision-rule case has a test (`TestDecisionRule` unless noted):

| Case | Test |
|---|---|
| {A:3} + approve -> unanimous, no meta | `test_unanimous_and_approved_skips_meta` |
| {A:3,B:2} -> need_meta | `test_a_partial_majority_goes_to_meta` |
| empty votes + approve -> verdict, contentless by majority | `test_empty_votes_with_an_approving_critic_is_decided`, `test_empty_votes_are_contentless_when_a_majority_says_so` |
| critic ambiguous -> need_meta | `test_an_ambiguous_critic_sends_the_item_to_meta` |
| critic missed=[X] -> need_meta | `test_a_missed_id_sends_the_item_to_meta` |
| proposer schema failure -> error verdict, NO critic call (store rows) | `TestFailuresAndRetries::test_an_answer_that_fails_the_schema_fails_its_seat` (asserts `calls_of(store, job_id, "critic") == []`) |
| critic error -> error, no meta | `test_a_failed_critic_fails_the_item_and_skips_meta` |
| duplicate pick counted once | `test_a_duplicate_pick_is_counted_once` |
| off-menu id dropped + recorded | `test_an_off_menu_pick_is_dropped_and_recorded` |
| meta off-menu dropped | `test_an_off_menu_meta_id_is_dropped_and_recorded`, and on the real builtin in `TestTheBuiltinGoCouncil::test_the_builtin_meta_round_reads_final_go_ids` |
| empty menu -> no_candidates, zero calls | `test_an_empty_menu_is_decided_with_zero_calls` and `TestSubmit::test_an_empty_menu_is_settled_at_submit_with_no_calls` |
| contentless tie broken by critic | `test_a_contentless_tie_is_broken_by_the_critic` |
| [True, False, False] counterexample | `test_the_minority_content_flag_counterexample` |
| selected_ids in menu order | `test_selected_ids_are_in_menu_order_on_the_skip_path`, `..._on_the_meta_path` |
| error verdict: selected_ids [] and decided_by None | `test_a_failed_proposer_fails_the_item_and_stops_the_council` |

Every listed worker case has a test:

| Case | Test |
|---|---|
| reuse across two jobs, zero backend calls, reused_from, zero usage | `TestReuseAndSingleFlight::test_a_second_job_asking_the_same_question_makes_no_calls` |
| single-flight | `..::test_two_items_with_identical_prompts_make_one_call_per_seat` |
| transport error then success -> ok after 2 attempts | `TestFailuresAndRetries::test_a_transport_error_is_retried_and_then_succeeds` (+ `test_three_transport_failures_fail_the_call_and_the_item`, + `TestBackoff` for the shipped 2 s/8 s values) |
| quota_exhausted halts, call back to pending, blocker, no item error, reset_halt + work finishes | `TestHaltsPauseRatherThanFail::test_a_halting_failure_stops_the_endpoint_and_keeps_the_call` (parametrised over quota_exhausted, auth, billing_guard) |
| soft max_total_tokens -> budget_exhausted + overshoot | `TestBudgets::test_passing_the_budget_halts_the_endpoint_and_reports_the_overshoot` |
| usage None on a budgeted endpoint -> budget_usage_unknown | `TestBudgets::test_an_unmeasurable_budget_halts_the_endpoint` |
| reset_budget resumes, keeps lifetime | `TestBudgets::test_reset_budget_resumes_the_job_and_keeps_the_lifetime_total` |
| two endpoints sharing an account never exceed the shared limit | `TestConcurrency::test_endpoints_sharing_an_account_share_one_limit` (in-flight counter shared by BOTH backends: peak <= 2 over 15 calls) |
| unverified seat refused at submit | `TestSubmit::test_an_unverified_seat_is_refused` |
| model_unavailable -> pending, waiting_for_model blocker | `TestBlockersTheWorkerWrites::test_model_unavailable_leaves_the_call_pending_and_waiting` |
| route_fingerprint mismatch -> released, configuration_mismatch blocker | `TestBlockersTheWorkerWrites::test_a_route_that_no_longer_matches_parks_the_call` |
| proposer failure + slow sibling: error verdict only after the sibling is terminal, both recorded | `TestFailuresAndRetries::test_a_failed_seat_waits_for_its_slow_sibling` |
| reply after cancel is stored (reusable), no critic call, settles nothing | `TestCancellation::test_a_reply_arriving_after_a_cancel_is_stored_and_changes_nothing` |
| stale claim_token raises StaleClaim and is discarded | `TestStaleClaims` (both halves: the store raises, the worker discards) |
| second live call for the same (item, role, seat, digest) refused | `TestOneLiveCallPerSeatAndUpstream` (advance idempotent; the store's partial unique index raises) |
| duplicate menu ids rejected at submit | `TestSubmit::test_a_duplicate_menu_id_is_refused` |
| foreground worker registers and heartbeats a workers row | `TestWorkToVerdict::test_a_foreground_worker_registers_and_heartbeats` |
| priority: later high-priority job claimed first | `TestPriority::test_a_high_priority_job_is_claimed_before_an_earlier_one` |
| stale upstream_digest -> critic treated as missing | `TestStaleUpstreamDigest` |
| order independence, >= 6 items, three orders | `TestOrderIndependence` (4 tests, 7 items) |

ORDER INDEPENDENCE in detail: seven items covering the skip path, the meta
path (both "split vote" and "critic objects"), a failed seat, the
deterministic empty menu and an off-menu pick, under one script, worked three
ways — (a) one call at a time in queue order through `work()` with a shared
account of capacity 1 (asserted `peak == 1`), (b) one endpoint at a time, with
a SEPARATE worker per endpoint claiming only its own endpoint, seat by seat,
(c) every claimable call at once, shuffled with a seeded RNG and executed
concurrently.  All three are asserted against the same `EXPECTED_VERDICTS`
table on status, selected_ids, decline_reason, decided_by, votes, meta_skipped
and per-seat error kinds, and a fourth test asserts two runs equal to each
other directly.  Asserting against a fixed table as well as across runs is
deliberate: three runs agreeing on a wrong answer is not order independence.

No test that passed at `d8a8573` fails on this branch (see **verification**).

**Criterion 2 (no-injection check).** PASS.
`TestTheBuiltinGoCouncil::test_a_go_item_is_decided_with_the_builtin_prompts`:
- No in-test query type, no monkeypatch, no stub, no injected renderer.  It
  calls `QueryRegistry(store).seed_builtins()` on the real `SqliteStore` from
  the suite's own fixture, then `registry.get("go_mf")` — which reads the spec
  back out of the database and re-validates it — and asserts
  `renderer == "go_mf_v3@1"`, `id_field == "go_id"`.
- One protein item (`beta-galactosidase (EC 3.2.1.23)`, three GO candidates)
  is driven proposers -> critic -> verdict with a `FakeBackend` whose
  proposer replies are in the BUILTIN's vocabulary (`go_id`, not `id`).
- The prompts the backend received are asserted to be the builtin's:
  `request.system == go.roles["proposer"].system` (byte identity against the
  spec that came out of the store) and it starts with "You assign Gene
  Ontology molecular-function terms"; the user prompt carries
  `Annotation string: "beta-galactosidase (EC 3.2.1.23)"`, `Produced by:
  prokka` and the builtin renderer's candidate line
  `   1 | GO:0004565 | 0.910 | beta-galactosidase activity`; the critic's
  system prompt likewise, with the score-less, definition-less candidate
  block `   1 | GO:0004565 | beta-galactosidase activity` and the vote line
  `GO:0004565: 3`.  `request.json_schema == go.answer_schema("proposer")` and
  contains `go_id`.
- `selected_ids == ["GO:0004565"]`, and the menu is deliberately not in id
  order; the sibling test `test_the_builtin_meta_round_reads_final_go_ids`
  has meta name `["GO:0030246", "GO:0004565", "GO:9999999"]` and asserts the
  verdict comes back `["GO:0004565", "GO:0030246"]` — MENU order, with the
  off-menu id dropped and recorded.
- The domain/generic boundary is asserted both ways: the stored
  `reply_parsed` still says `go_id`, while the ballot says `id`.

**Criterion 3 (the worker writes the blocker's error itself).** PASS.
`TestBlockersTheWorkerWrites` is two tests, and NEITHER writes `calls.error`:
- `test_model_unavailable_leaves_the_call_pending_and_waiting` — a scripted
  `ModelUnavailable` leaves the call `pending` with
  `error["kind"] == "model_unavailable"`, `attempt_count == 0` and
  `not_before` in the future, and `store.job_status(job)["blockers"]` contains
  `{"kind": "waiting_for_model", "endpoint": "fake1", ...}`.
- `test_a_route_that_no_longer_matches_parks_the_call` — a SECOND worker whose
  config puts endpoint `fake1` on a different account computes a different
  `route_fingerprint`, so every call is parked with
  `error["kind"] == "configuration_mismatch"`, `not_before` ~5 minutes out,
  `attempt_count == 0`, nothing asked of any model, and `job_status` reports
  the `configuration_mismatch` blocker.
The only writes to that column in either test come from the worker, inside
the same `store.transaction()` as the `release_call` (`worker.py:_park`).

## reconciliation (prompt vs tree)

The prompt is a design-branch snapshot; `main` and the prompt's own internals
disagreed in eleven places.  In each case I adapted and carried on.

1. **Where dedup and menu filtering live.**  The prompt says the WORKER
   applies "postprocess + per-proposer dedup + menu filtering" in `advance`
   (WORKER §4), while the DECISION RULE section puts both in `decide()`,
   "implemented ONCE as the pure function decide()".  Both cannot be the
   single implementation.  Dedup, menu filtering and the `off_menu` note are
   in `decide()` (they change the decision, and `off_menu` has to end up in
   `verdict.errors`); the worker applies only `postprocess` — in exactly one
   place, `SettledCall.from_call_row` — and the JSON-Schema check.  To keep
   the critic/meta PROMPTS and the verdict counting the same table, the
   ballot/vote computation is exported as `tally()` and both call it.
2. **`calls.error` on a pending call, and job `done`.**  As the previous
   milestone's record predicted; see "the two store additions" above.
3. **`CONTRACT_METHODS` had to grow.**
   `test_store_contract.py::TestCouncilStoreProtocol::test_the_protocol_declares_exactly_the_contract`
   asserts `dir(CouncilStore) == CONTRACT_METHODS`, so adding two sanctioned
   protocol methods necessarily edits that set.  Added the two names with a
   comment saying which milestone added them and why.  That is the only
   change to a pre-existing test file.
4. **`configuration_mismatch` is not a declared error kind.**  The prompt
   requires the blocker; `types.ERROR_KINDS` does not list the kind and
   `types.py` is off-limits.  `store/queries.BLOCKER_BY_ERROR_KIND` already
   maps it (the store milestone wired it up in advance), nothing validates
   the kinds stored, so the worker writes the string as
   `worker.CONFIGURATION_MISMATCH` with a comment saying it is deliberately
   outside the taxonomy.  Whoever next edits `types.py` should add it.
5. **A `CouncilConfig` cannot describe a `fake` endpoint.**
   `EndpointConfig.kind` is a `Literal` over the four real kinds, and every
   engine test runs on `fake` endpoints.  `CouncilWorker` therefore accepts
   either a `CouncilConfig` (it uses the `endpoint(name)` accessor) or the
   mapping form `{"endpoints": {name: {...}}}`, via one documented accessor
   (`CouncilWorker.endpoint_config`).  `config=None` is also accepted and
   means "no endpoint facts", which gives up budgets and shared-account
   concurrency but still runs.
6. **`submit` is synchronous in the prompt but must run async CLI
   preflights.**  Shipped as a pair: `asubmit` (async, does the work) and
   `submit` (sync wrapper), same for `aevaluate`/`evaluate`.  The wrapper
   owns a fresh loop and REFUSES to nest (a clear `RuntimeError` naming the
   async form) rather than calling `asyncio.run`, which sets the thread's
   current loop to `None` and — as `test_types_config.run`'s docstring
   records — breaks other tests in this repository from a distance.
7. **`claim_calls` has no job filter.**  The prompt says `work(job_id=...)`
   restricts claims to one job; the store's claim contract takes only
   `endpoints`.  `work` scopes in Python: an out-of-scope claim is handed
   straight back with `release_call(..., increment_attempt=False)` and no
   mark on it.  Charging it an attempt would charge the call for this
   worker's scope.
8. **`record_attempt` is not written for the three unspent releases.**
   `attempts` is keyed `(call_id, attempt_no)` and the only number available
   is the call's `attempt_count` + 1 — but a halt, a `model_unavailable` and
   a `configuration_mismatch` release deliberately do NOT increment it, so
   two such releases would collide on the primary key.  Those outcomes are
   recorded on `calls.error` instead, which is what `job_status` and an
   operator read.  Attempts are written for ok, reused, terminal failures and
   transport/timeout retries — every outcome that does move `attempt_count`.
9. **`renderer_id_version` is named by the prompt but nowhere defined.**
   Implemented as `"<renderer>|<adapter>|<id_field>"` (defaulting to
   `template|generic|id`), i.e. the three things that change what was asked
   or how it was read without changing the query type's `(name, version)`.
10. **"Everything left is blocked" needed a definition.**  `work(until_idle)`
    stops when `job_status` reports a blocker AND nothing becomes workable
    within `idle_grace_s` (default 1 s).  That is what distinguishes a
    `model_unavailable` call parked for a minute (report it) from a transport
    retry parked for two seconds (wait for it — nothing reports that one).
11. **The decision rule's own gaps.**  The rule does not say what
    `n_valid`, `final_confidence` or `meta_skipped` are on the empty-menu
    path, nor `meta_skipped` on an error verdict.  Chosen: empty menu ->
    `n_valid` 0, `final_confidence` None ("high" would be a confidence nobody
    expressed), `meta_skipped` True; error verdict -> `meta_skipped` True for
    a proposer or critic failure and **False** for a meta failure, because
    meta was called and failed.  All four are asserted.

## design decisions not dictated by the prompt

- **`decide()` returns `("waiting", None)` while ANY proposer is unsettled**,
  including when another has already failed.  That is the whole of WORKER §4's
  "do nothing until every sibling proposer call is terminal", and putting it
  in the rule rather than the worker means a second worker driving the same
  item cannot write a different verdict.
- **`selected_ids` is in MENU order on the meta path too.**  The rule only
  says so for the skip path, but a caller joining `selected_ids` back against
  its own candidate table wants one order, not two.
- **`votes` keeps FIRST-VOTE order.**  `council-query-types.md` flagged that
  the renderers' tie order reproduces `Counter.most_common` only if the
  engine inserts ids in first-vote order.  It does (roster order, then each
  ballot's pick order), and `test_selected_ids_are_in_menu_order_on_the_skip_path`
  pins it.
- **`usage` names the seats that were asked.**  The critic appears on the skip
  path (it ran); meta does not (it did not).  A zero-usage entry for a seat
  that never ran would read as a free call rather than no call.
- **Prompt text is never stored.**  `_call_row` renders the prompt to HASH it
  and throws it away; `execute` re-renders from the item and the settled
  upstream calls.  That is what `store.queries.refuse_prompt_text` is
  protecting, one layer down.
- **A single-flight follower whose leader FAILED makes its own call** rather
  than inheriting the failure.  The failure may be the leader's seat, not the
  question.
- **The route-fingerprint check happens before the query type is loaded.**  A
  machine whose config no longer matches hands the call back without needing
  a query type it was never going to render.  (`_query_for` reads the spec
  back through `QueryRegistry`, which re-validates it, so a daemon lacking a
  renderer a shared job names refuses the job — tested by
  `TestASecondWorkerReadsTheQueryTypeBack`.)
- **Concurrency is counted twice, on purpose.**  `_AccountLimit.assigned`
  counts calls this worker has taken responsibility for and is what sizes the
  next claim batch; the semaphore counts calls in flight at the backend.
  Conflating them would have the loop claim the whole queue at once.
- **The budget is measured against `epoch_tokens`, not `lifetime_tokens`**,
  because `reset_budget` resets the epoch and keeps the lifetime — so the
  lifetime total could never be brought back under a budget.
- **A reply with unmeasurable usage is still stored.**  The endpoint halts
  (`budget_usage_unknown`) but the answer was paid for, so it is kept and
  stays reusable.
- **An exception out of `execute` is re-raised by `work`.**  A backend never
  raises for a call failure, so anything that escapes is a bug in the worker,
  and on a headless machine with no human watching, failing loudly beats a
  job that quietly stops progressing.  The claim is left to `reclaim_stale`.
- **A `stalled` blocker after `max_idle_s`.**  If the scope has outstanding
  work, nothing is workable and nothing explains why (every remaining call is
  claimed by another worker that may be wedged), `work(until_idle=True)`
  returns after `max_idle_s` with a `stalled` blocker rather than looping for
  ever.  Not in the contract; a foreground command that never returns is
  worse than one that says why it gave up.
- **The workers row is left behind at the end of `work()`.**  Its heartbeat
  simply goes stale, which is what `live_workers` and `reclaim_stale` are
  written against.  Deleting it would make `job_status` report `no_daemon`
  against a job a foreground run had just finished.
- **`work()` returns a summary dict** (`claimed`, `finished`, `released`,
  `blockers`, `idle`).  The contract does not say, and "report blockers"
  needs somewhere to report them to.

## inferred rather than verified

- **The PostgreSQL half still has never run.**  The engine tests use the
  suite's existing parametrised fixtures, so all 53 PostgreSQL
  parametrisations skip with the stated reason
  (`KBU_COUNCIL_TEST_PG_DSN is not set` / `store.postgres is not implemented
  yet`) and will turn themselves on when that module lands.  The two SQL
  statements this task added are dialect-neutral SQLAlchemy Core, but they
  have only been executed on SQLite.
- **`prompt_hash` depends on `backend.effective_params`.**  A worker that
  does NOT hold the backend for a seat falls back to
  `types.seat_generation_params`, so in principle a submitter without the
  backend could write a prompt hash the running worker would not have
  computed — the call would then simply not reuse another's answer.  Every
  path in these tests submits from a worker that holds its own backends; the
  mixed case is reasoned, not executed.
- **The daemon path is not exercised.**  `worker_kind="daemon"`, the
  `daemon_lease` generation on a claim and `request_stop`/`stop_requested`
  are all read by the worker (`_should_stop` polls `stop_requested` every
  heartbeat), but `CouncilDaemon` is a later milestone and no test here
  acquires a lease.  Claiming under a lease generation is the store's own
  tested behaviour; what is untested is a worker that holds one.
- **No test runs two engine workers in separate PROCESSES.**  The store
  milestone proved the claim lock that way; here, two workers always share an
  interpreter (`TestOrderIndependence::test_one_endpoint_at_a_time` and the
  route-mismatch test use two worker objects on one loop).  Cross-process
  advance/settle contention is therefore reasoned from the claim contract
  rather than measured.
- **`asyncio.Semaphore` is rebuilt when the running loop changes.**  A worker
  object reused across two `work()` calls on two different loops gets a fresh
  semaphore, which drops any waiters the old one held.  There can be none —
  a `work()` call awaits its in-flight tasks in its `finally` — but that is a
  reasoned invariant, not an asserted one.
- **Retry jitter is `random.Random` by default.**  The shipped 2 s/8 s
  +-25% values are asserted as a RANGE (`TestBackoff`); the integration tests
  run with `retry_jitter=0.0` and 20 ms/50 ms so they do not wait for the
  real backoff.  The real timings are therefore pinned but never waited on.
