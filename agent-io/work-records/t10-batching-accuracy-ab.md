# t10-batching-accuracy-ab

- **task_id**: t10-batching-accuracy-ab
- **branch**: maestro/developer/t10-batching-accuracy-ab
- **commit_shas**:
  - `5345caa` — feat(council): add batched-vs-unbatched accuracy A/B harness (t10)

## Summary

Built `scripts/council/batching_accuracy_ab.py`, a script that measures
**agreement** (explicitly never "accuracy" — no labelled `go_mf` data exists
in this repo) between a council's unbatched and batched critic/meta seats on
a fixed-seed sample of an existing job's items.

Design, summarized (full reasoning is in the script's module docstring):

1. Every proposer seat answers each sampled item twice — alone and batched —
   and those two answers are compared directly (no upstream to freeze for a
   proposer).
2. The **unbatched** proposer answers only are tallied (via the real
   `engine.decide.tally()`) into one frozen reference vote table per item.
   This is the "same chair ballots and votes feed both arms" the task's
   binding amendment (b) requires, so a critic or meta disagreement can be
   attributed to that seat's own batching rather than to proposer-batching
   noise riding along underneath it.
3. The critic seat answers each item twice against that one frozen vote
   table — alone and batched — and the two answers are compared.
4. The meta seat answers each item twice against the same frozen vote table,
   but each arm sees **its own arm's** critic answer (unbatched meta sees
   unbatched critic; batched meta sees batched critic) — votes/ballots are
   frozen, the critic's own verdict is not, so meta's own batching effect is
   measured against an input that corresponds to something that can actually
   happen in that arm.
5. The real `engine.decide.decide()` rule runs twice per item — once with
   the unbatched critic/meta calls, once with the batched ones, always
   against the same frozen proposer calls — producing the headline: the
   fraction of items whose **final verdict** differs between arms. This is
   deliberately a different (smaller-or-equal) number than any single
   seat's own disagreement count, since an intermediate disagreement the
   council still resolves the same way is not a regression (task point 4) —
   the test module proves this distinction with a rigged critic
   disagreement that does not propagate to the headline.
6. Exits `0` when the verdict-difference fraction is at/below `--threshold`,
   `1` when it exceeds it, `2` on a usage error (bad args, missing job,
   declined cost confirmation), `3` when no seat's backend is resolvable at
   all (per binding amendment (d): the harness still "completes" by saying
   so rather than inventing a number).

Safety / honesty properties, all stated in the script's own `--help` and
module docstring, not only here:

- The only store interaction is `init_schema()` (idempotent) plus the
  read-only `get_job` / `get_items` / `QueryRegistry(store).get` — no
  `claim_calls`, `add_calls`, `complete_call`, or `settle_item` is ever
  called, so no production call row can be claimed, settled or superseded.
- Before spending anything, it prints a rough projected token-spend estimate
  and requires `--yes` or an interactive confirmation; after each arm it
  prints the real measured usage from the backend.
- It never enables batching for anything (no config write, no
  `VERIFIED_OUTPUT_CAPS` mutation) and says so explicitly in its JSON output
  (`authorisation` field) and in `--help`.
- `--help` and the module docstring both state plainly that a genuine
  accuracy gate would need labelled items and a numerical pass threshold,
  neither of which exists in this repo, and that nothing this script prints
  should be read as "the accuracy gate passed."
- A documented, deliberate scope limitation: meta is invoked for every
  sampled item regardless of whether `decide()` would actually reach it in
  production (which only happens when the critic disapproves or the vote is
  not unanimous) — this spends more tokens than production would in
  exchange for a meta-seat report that never depends on which items
  happened to need meta. The headline is unaffected by this, since
  `decide()` itself ignores a supplied meta call when it is not needed.

## Files touched

- `scripts/council/batching_accuracy_ab.py` (new)
- `tests/domains/ai/council/test_batching_accuracy_ab.py` (new)

No engine, store, config or daemon file was touched, per the task's "files
you own" constraint.

## Success criteria self-assessment

> The harness runs end to end against the scripted fake and reports
> per-seat identical/differing counts, per-item disagreements, per-arm
> token cost, and a verdict-difference fraction

**PASS.** `TestRunAB.test_per_seat_and_headline_counts` asserts exact
per-seat `(n_items, n_identical, n_differ, n_invalid)` tuples and disagreement
item lists for both proposer seats, the critic seat and the meta seat, plus
the headline `n_verdict_differs` / `verdict_difference_fraction`, and asserts
usage/invocation counts are present (zero, since `FakeBackend` reports zero
usage, but present as a real key rather than missing).

> it exits non-zero above a threshold passed on the command line and zero
> below it, proven by tests on both sides

**PASS.** `TestGateExitCode.test_exit_zero_when_within_threshold` (threshold
0.35 against a measured 0.30 fraction → exit 0) and
`test_exit_one_when_exceeding_threshold` (threshold 0.25 → exit 1), both
driven through the real CLI `main()` entry point, not only the internal
`run_ab` facade.

> tests spend no real tokens

**PASS.** Every test passes an explicit `backend_for` / `backend_for_override`
built from `kbutillib...backends.testing.FakeBackend`; nothing in the test
module calls `--config` or omits the override, so `live_backend_for` (the
only function in the script that can reach `kbutillib.domains.ai.llm`, which
is what can reach a real network) is never invoked. Every call to the
script is additionally routed through a bounded (30s) `ThreadPoolExecutor`
timeout in `_bounded()`, so a hang would fail loudly rather than wedge CI.

> the harness claims, settles or supersedes no production call row

**PASS.** `run_ab`/`arun_ab` call only `store.get_job`, `store.get_items`
and `QueryRegistry(store).get` on the caller's store; no write path
(`claim_calls`, `add_calls`, `complete_call`, `settle_item`,
`cancel_job`, etc.) is referenced anywhere in the script. The CLI also
refuses to write `--out` over the store's own file (tested in
`test_main_refuses_to_write_report_over_the_store_file`).

> no test that passed on the base commit fails on the branch

**PASS**, see Tests below — the baseline's 4 pre-existing, machine-local
failures are the only ones present, unchanged, and no other test in the
targeted selection regressed.

## Tests run

Exact baseline command, from the worktree root:

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python \
  -m pytest tests/domains/ai/council -q -p no:cacheprovider --no-header
```

Result: `4 failed, 970 passed in 134.66s`.

The 4 failures are exactly the documented pre-existing/machine-local set,
all in `tests/domains/ai/council/test_types_config.py::TestPackagedDefaultConfig`
(`test_every_packaged_profile_builds_a_roster`,
`test_the_packaged_config_declares_no_poplar_endpoint`,
`test_on_a_fresh_machine_with_no_kbutillib_config`,
`test_cli_endpoints_default_to_one_call_at_a_time`) — this box's
`~/.kbutillib/config.yaml` leaking past their monkeypatched `HOME`, per dev
row 53 cited in the task envelope. None of these touch this task's files.

970 passed = the measured baseline's 963 passed + the 7 new tests this task
adds in `tests/domains/ai/council/test_batching_accuracy_ab.py`, all
passing. Also ran that new module alone first, in isolation, to confirm it
is self-sufficient: `7 passed in 4.19s`.

I did not run the full repository suite — the task explicitly scopes that
to the conductor and warns it is not mine to run from this worktree (and
would additionally hit the documented `test_composition_smoke.py` worktree
artifact, which is unrelated to this change).

## Caveats

- **Scope decision, stated prominently in the script itself**: this harness
  isolates each role's OWN sensitivity to batching by holding every role's
  input fixed across both arms (proposer votes/ballots always; meta's
  critic input only within its own arm). It is deliberately NOT a comparison
  of two fully self-consistent end-to-end pipelines in which one role's
  batching-induced drift propagates into the next role's prompt. A
  correlated effect across roles in a real, unfrozen batched run could be
  larger than what this harness reports. I judged this the right trade for
  a number that is attributable to one seat at a time, per the binding
  amendment's explicit "identical frozen upstream context" requirement, but
  it is a real limitation and I want the reviewer to see it stated rather
  than discovered.
- **Live mode is built but unexercised here.** `live_backend_for()` resolves
  a seat's endpoint through `kbutillib.domains.ai.llm.acomplete` using
  `--config` (default: the packaged+user config). I did not have a real
  council store with a submitted job, a reachable subscription, or
  credentials in this environment to exercise it end to end, which is
  explicitly permitted by binding amendment (d) ("code may land with
  batching disabled... do not block on a subscription being reachable").
  The live path is exercised only by code review and by the fact that it
  reuses the same public `CompletionRequest` / `llm.acomplete` shapes the
  tests exercise against `FakeBackend` — I did not fabricate a live run or
  its numbers.
- **The batched arm does not bisect a whole-invocation failure.** Production
  retries a failed batch by splitting it in half (`_run_batch_or_split`);
  this harness is a one-shot measurement tool, not a queue, so a whole-batch
  failure in the batched arm is recorded as a failure for every member of
  that group once, with no retry. This is stated in `invoke_batched`'s
  docstring.
- I did not add a `--roles` filter to let an operator skip, e.g., the
  proposer-level comparison to save tokens. All three roles are always
  measured. This was a deliberate simplification to keep the CLI surface
  (and its interaction with the frozen-reference construction, which needs
  proposer answers regardless) smaller; an operator who wants to skip
  proposer *reporting* still pays for the proposer calls today, since they
  are required to build the frozen reference tally.
