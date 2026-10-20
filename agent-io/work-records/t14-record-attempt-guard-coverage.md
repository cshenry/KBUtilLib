# t14-record-attempt-guard-coverage

## task_id
t14-record-attempt-guard-coverage

## branch
maestro/developer/t14-record-attempt-guard-coverage

## commit_shas
- a5d2231 — fix: cover queued_at in the attempts-table prompt-text guard

## summary
`SqliteStore.record_attempt` refuses to write a dict carrying a `system`/`user`
key to the `attempts` table, via `queries.refuse_prompt_text(**arguments)`. An
earlier phase added a `queued_at: Optional[datetime] = None` parameter to
`record_attempt` but never added `queued_at` to the hand-written kwargs list
passed to `refuse_prompt_text(...)`, so that parameter path was unchecked.
Fixed by deriving the guard's argument dict from the function's own
`locals()` (captured as the very first statement of the method body, before
any other local variable exists, so it is exactly the parameter set minus
`self`) instead of listing kwargs by hand. This makes coverage structural:
any current parameter, and any future one added to the signature, is passed
to the guard automatically, with no second place to remember to update.
`refuse_prompt_text` already took `**arguments` and names the offending
argument by its dict key, so switching the caller to `locals()` preserves
the existing "argument 'x' contains a dict with key 'y'" error message
verbatim — nothing about the diagnostic is lost.

## files_touched
- `src/kbutillib/domains/ai/council/store/sqlite.py` — `record_attempt`: replaced the
  hand-maintained `refuse_prompt_text(call_id=..., attempt_no=..., ...)` kwargs
  list with `refuse_prompt_text(**{k: v for k, v in locals().items() if k != "self"})`,
  with a comment explaining why (and naming `queued_at` as the parameter that was
  missed).
- `tests/domains/ai/council/test_store_contract.py` — added `import inspect`, and five
  new tests inside `TestRecordAttempt`:
  - `test_prompt_text_in_queued_at_is_refused` (parametrized `system`/`user`) — the
    regression test.
  - `test_a_legitimate_queued_at_datetime_is_accepted_and_stored`
  - `test_queued_at_left_as_none_is_accepted`
  - `test_guard_coverage_is_structural_not_a_hand_maintained_list` — enumerates
    `record_attempt`'s live signature (via `inspect.signature`) for every optional
    parameter and asserts each refuses a prompt-text dict, rather than naming
    `usage`/`worker_id`/`queued_at` by hand.

## success_criteria_check
- "A dict carrying a `system` or `user` key passed as `queued_at` is refused by
  `record_attempt`, and that test fails against the base implementation" — PASS.
  `test_prompt_text_in_queued_at_is_refused` passes on the branch. Verified it
  fails on base: stashed only the `sqlite.py` change and re-ran the two new
  tests that exercise `queued_at` against the unmodified base `record_attempt`
  (base commit `99ee9e9`, confirmed by reading `git show 99ee9e9:...sqlite.py`
  lines 1383-1416, which still hand-lists kwargs without `queued_at`). Both
  failed: the dict flowed past the guard into the SQLAlchemy insert and raised
  `sqlalchemy.exc.StatementError` / `TypeError: expected a datetime, got dict`
  from the `UtcDateTime` column type, not the expected `ValueError`, so
  `pytest.raises(ValueError, match="prompt text")` reported the test as failed.
  Restored the fix afterward (`git stash pop`) and reran the full suite to
  confirm the fix-state result stands — see `tests_run` below.
- "Every pre-existing prompt-text refusal still fires" — PASS. The existing
  parametrized `test_prompt_text_is_refused_at_any_depth` (system/user/nested/
  in-a-list/deeply-nested, via `usage`) and
  `test_an_attempt_for_an_unknown_call_is_refused` /
  `test_the_same_attempt_number_cannot_be_recorded_twice` all still pass
  unchanged. Also exercised structurally by the new
  `test_guard_coverage_is_structural_not_a_hand_maintained_list`, which refuses
  a prompt-text dict through `usage` and `worker_id` as well as `queued_at`.
- "Legitimate datetime and None values both still pass" — PASS. New tests
  `test_a_legitimate_queued_at_datetime_is_accepted_and_stored` (and the
  pre-existing `test_migrate_adds_the_column_and_keeps_every_row`, which passes
  no `queued_at` at all) and `test_queued_at_left_as_none_is_accepted` both
  pass on the branch.
- "No test that passed on the base commit fails on the branch" — PASS. Full
  targeted selection on the branch: 989 passed, 4 failed (same 4 pre-existing,
  machine-local `TestPackagedDefaultConfig` failures named in the task
  envelope, dev row 53 — unrelated to this change and not touched). Baseline
  (measured before any edit) was 984 passed, 4 failed — same 4 failures,
  identical ids. The branch adds exactly 5 new passing tests
  (`test_prompt_text_in_queued_at_is_refused` is parametrized x2, plus 3 more
  singleton tests = 5 new test items; 984 + 5 = 989).

## tests_run
Baseline, measured before any source change, from the worktree root:
```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest \
  tests/domains/ai/council -q -p no:cacheprovider --no-header
```
Result: `4 failed, 984 passed in 148.35s`. The 4 failures were all in
`test_types_config.py::TestPackagedDefaultConfig` (`test_every_packaged_profile_builds_a_roster`,
`test_the_packaged_config_declares_no_poplar_endpoint`,
`test_on_a_fresh_machine_with_no_kbutillib_config`,
`test_cli_endpoints_default_to_one_call_at_a_time`) — matches the envelope's stated
baseline and dev row 53 (this box's `~/.kbutillib/config.yaml` leaking past a
monkeypatched `HOME`).

Same command after the fix and new tests: `4 failed, 989 passed in 138.28s` — same 4
failing ids, 5 new passing tests, nothing else moved.

Targeted re-run of just the new/changed class:
```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest \
  tests/domains/ai/council/test_store_contract.py::TestRecordAttempt -q -p no:cacheprovider --no-header
```
Result: `14 passed in 2.62s`.

Base-implementation check (regression-proof): temporarily `git stash push` on
only `src/kbutillib/domains/ai/council/store/sqlite.py` (restoring the base
`record_attempt`), then ran the two `queued_at`-exercising tests:
```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest \
  "tests/domains/ai/council/test_store_contract.py::TestRecordAttempt::test_prompt_text_in_queued_at_is_refused" \
  "tests/domains/ai/council/test_store_contract.py::TestRecordAttempt::test_guard_coverage_is_structural_not_a_hand_maintained_list" \
  -q -p no:cacheprovider --no-header
```
Result: `3 failed` (both parametrized ids of the first test, plus the
structural test) — each failing with `sqlalchemy.exc.StatementError` /
`TypeError: expected a datetime, got dict`, i.e. the dict reached the INSERT
instead of being refused. `git stash pop` immediately after to restore the fix.

No full repo-wide suite was run (per the baseline instructions, only the
targeted `tests/domains/ai/council` selection was used); the two files this
task owns (`store/sqlite.py`, `tests/domains/ai/council/*`) are both inside
that selection, so no further scoping was needed.

## caveats
- **Structural-vs-explicit judgment call, as the task asked me to record.** I
  took the structural route: `refuse_prompt_text(**{k: v for k, v in
  locals().items() if k != "self"})` placed as the first statement of
  `record_attempt`, before any other local is bound. This is safe here
  specifically because (a) `refuse_prompt_text` already accepted `**arguments`
  (no signature change needed on the query-helper side), (b) `locals()` at
  that point in the function body contains exactly the bound parameters, no
  more and no less, and (c) the per-argument error message is unaffected
  because `refuse_prompt_text` iterates `arguments.items()` and names the dict
  key, which is still the real parameter name. The main risk I considered and
  rejected: `locals()`-based introspection is sometimes criticized as fragile
  because it changes if someone adds an early local variable inside the
  function before the guard call — I mitigated this by keeping the guard call
  the literal first statement (immediately after the docstring) and adding a
  comment warning against moving it. I judged this safer than hand-maintaining
  a second kwargs list, which is the exact failure mode this task exists to
  fix. I did not attempt a `inspect.signature`-based wrapper/decorator
  approach (e.g. a decorator that inspects bound arguments via
  `inspect.signature(...).bind(...)`) because it would be more invasive for a
  single-call-site fix and would not improve on `locals()` here — both derive
  the argument set from the signature, but a decorator adds a layer of
  indirection with no corresponding benefit for a function with no `**kwargs`
  passthrough and a single caller of the guard.
- The 4 pre-existing `TestPackagedDefaultConfig` failures are untouched —
  confirmed identical before and after, consistent with the envelope's note
  that they are machine-local and already tracked as dev row 53.
- Did not touch `queries.py`'s `refuse_prompt_text` itself — its `**arguments`
  signature already supported arbitrary named arguments, so no change was
  needed there; the gap was entirely in how `sqlite.py` called it.
