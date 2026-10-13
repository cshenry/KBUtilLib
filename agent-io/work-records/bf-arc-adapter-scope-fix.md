# bf-arc-adapter-scope-fix — checkpoint promotion scoped to the unit that earned it

Addendum to `bf-arc-adapter.md`. Branch `clearinghouse-backfill-v1`, follow-up on
the reviewer-failed `bf-arc-adapter` landing (`bb53f3a`, off `main` `384d5d8`).
One localized design defect; nothing else on the branch was restructured.

## The defect

`promote_arc_checkpoints()` took no arguments and promoted every
`*.candidate.json` it globbed in `arc_checkpoint_dir()`. That directory is ONE
directory under the clearinghouse state root — it has to be, because a
watermark must be found by the NEXT run and must survive the shard output being
regenerated — so it is shared by every arc source of every manifest on the
machine. `run_backfill` called it on any unit that came out `UNIT_DONE`.

The guard tested *this* unit; the call promoted *any* unit's candidate:

1. Arc store B's run leaves a candidate without reaching `done` (its batches
   did not verify, or the process was killed after sharding).
2. Any later run A, from any manifest, finishes any unit.
3. A promotes B's candidate.
4. B's next run starts from a watermark covering rows that never landed.

The lake is append-only and fill-only, so nothing comes back for that range.
The gap is permanent and every run reports clean. The call-site comment
asserted the opposite ("a capped, incomplete or killed unit never gets here and
its candidate is simply never promoted") and was false as written.

## The fix

- `clearinghouse_shard.py:1871` — `promote_arc_checkpoints(keys: Iterable[str])`.
  Per key: `arc_checkpoint_path(key, candidate=True)` → `arc_checkpoint_path(key)`
  by `os.replace`. A named key with no candidate moves nothing and raises
  nothing. Keys are de-duplicated (`dict.fromkeys`), order preserved. The
  glob-everything branch is gone and there is no zero-argument form.
- `clearinghouse_shard.py:407` — new `SourceAdapter.checkpoint_candidates()`
  hook, default `()`. This is how the keys travel: a candidate file on disk
  carries nothing that says which unit earned it, so the adapter has to report
  it.
- `clearinghouse_shard.py` arc adapter — `self._candidates` appended in
  `_record_outcome` at the point the candidate is actually written (so a capped
  or retry-blocked run reports nothing, unchanged); `checkpoint_candidates()`
  overridden beside `watermark()`.
- `clearinghouse_shard.py` — `SourceOutcome.checkpoint_candidates`, populated in
  `shard_manifest` beside `watermark=adapter.watermark()`, serialised onto each
  `run.json` source entry under `ARC_CHECKPOINT_CANDIDATES_FIELD`
  (`"arc_checkpoint_candidates"`) — one constant shared by writer and reader so
  the keys cannot be recorded under one spelling and promoted under another.
- `clearinghouse_shard.py` — `arc_checkpoint_candidates(run)`, a `run.json`
  reader beside `unit_ids` / `batches_for_unit`. The unit directory is what
  scopes it: the driver shards each unit into its own `<out>/units/<unit>`, so
  every source entry in that inventory belongs to that unit. The preset's four
  arc sources read one store with one identity and report the same key, which
  the dedup collapses. Empty for a non-arc manifest and for an inventory
  written before the field existed — the correct answer in both cases.
- `clearinghouse_backfill.py:1432` — the `UNIT_DONE` branch now promotes
  `cs.arc_checkpoint_candidates(cs.read_run_manifest(unit_dir))`, and the
  comment states the property the code provides.

Nothing else on the branch changed. The five existing promotion call sites in
`test_clearinghouse_arc_adapter.py` were updated to the new signature; the
capped-run test now passes the key *explicitly*, which is the stronger
assertion (even named, a key with no candidate moves nothing).

## Tests added (`tests/berdl/test_clearinghouse_arc_adapter.py`)

A new section, reusing the module's `arc_store`, `_make_tarball`,
`FakeRunContext` and autouse `state_root` fixtures, plus a compact `_FakeCap`
(the two methods `run_backfill` calls) and `_drive_preset` (register, plan,
run — the real driver).

- `test_another_manifests_completed_unit_never_promotes_a_foreign_candidate` —
  **two manifests sharing the checkpoint directory**, both through the real
  driver. The `arc-kbdl` preset runs with `verify_ok=False`: the unit comes out
  `incomplete`, and the adapter has nonetheless written a candidate (its read
  was uncapped, exhausted and clean, which is all an adapter can know). The
  `file-example` preset then completes a unit — asserted `UNIT_DONE`, so the
  promotion guard really was reached, and asserted to record no keys of its own.
  The arc candidate is still a candidate; the real checkpoint is absent and
  `read_arc_checkpoint` is `{}`.
- `test_a_completed_arc_unit_promotes_its_own_key_and_no_other` — a foreign
  candidate is planted in the directory, the arc preset completes: its own key
  is promoted and readable, the foreign candidate is untouched and unreadable.
- `test_the_four_arc_sources_of_one_unit_report_one_shared_key` — one store, one
  identity: several sources, one key, no duplicate promotion.
- `test_promotion_of_an_unknown_key_moves_nothing_and_is_idempotent`.
- `test_promotion_has_no_zero_argument_form_anywhere` — asserted twice over,
  because either half alone would pass while the defect was reachable: the
  signature raises `TypeError`, and no line in the shipped `kbutillib` package
  contains `promote_arc_checkpoints()`.

**Defect-sensitivity checked, not assumed.** With the glob behaviour
reintroduced behind the new signature and the driver calling it unscoped, three
of these tests fail (`foreign_candidate`, `own_key_and_no_other`,
`unknown_key`). The fix was then restored and the suite re-run.

## Verification

`pytest -q -p no:randomly` with 13 `--ignore`d modules that cannot be collected
in this venv (missing optional scientific deps: pandas, torch, rdkit, …;
identical on both commits and untouched by this change).

| | exit | result |
|---|---|---|
| base `384d5d8`, detached worktree `/tmp/base-384d5d8-scope` | 1 | 359 failed, 3449 passed, 424 skipped, 9 errors |
| branch | 1 | 358 failed, 3514 passed, 424 skipped, 9 errors |

Same venv for both (`.task-venv`, CPython 3.12.3, pytest 9.1.1), the base run
forced onto its own tree with `PYTHONPATH=<base>/src` (the editable install is a
plain `.pth`, so `PYTHONPATH` takes precedence — verified by printing
`kbutillib.__file__`). Compared by nodeid NAME:

- `comm -13` (failing on branch, not on base) — **empty. No regressions.**
- `comm -23` — one entry,
  `tests/cli/test_clearinghouse_operator.py::test_plan_invalid_manifest_exits_nonzero_naming_key`,
  which fails on base and passes on the branch (fixed by `bb53f3a`'s manifest
  work, not by this change).

Targeted: `tests/berdl/test_clearinghouse_arc_adapter.py` +
`tests/berdl/test_clearinghouse_backfill.py` → 165 passed, exit 0.

The cached `build-baselines/kbutillib.json` was NOT used: it was captured at
`0f84803` in a different venv, and the task required a fresh base-vs-branch
comparison. It is left unmodified.

## Reconciled against the tree

1. **The worktree was at `384d5d8`, not `bb53f3a`.** The prompt said the branch
   was at `bb53f3a`; `maestro/developer/shared-context-this-prompt-is-se-bf-arc-adapter`
   had been reset to `main`, and `bb53f3a` was reachable only from
   `maestro/reviewer/shared-context-this-prompt-is-se-bf-arc-adapter-task-2ab6783f`.
   `bb53f3a`'s parent is exactly `384d5d8`, as the prompt said, so the developer
   branch was fast-forwarded (`git merge --ff-only bb53f3a`) onto the work it was
   documented to be carrying — same branch throughout, no checkout, no new branch.
   Without this the named line ranges did not exist and the "do not redo the 59
   tests" constraint was unsatisfiable.
2. **Line numbers were off by ~20–30** (`promote_arc_checkpoints` at 1840 vs. 1871,
   `arc_checkpoint_dir` at 1755 vs. 1755, `_checkpoint_key` "~2133" vs. 2133).
   Every symbol was located by name. Not a problem; noted for the record.
3. **"An arc unit capped by `--limit` leaves a candidate" is not what the code
   does.** `_record_outcome` returns early on `self._capped`, so a capped run
   writes *no* candidate at all — which is itself an original `bf-arc-adapter`
   success criterion ("capped, incomplete and scratch runs never misuse" the
   watermark) and is already covered by
   `test_a_capped_run_writes_no_checkpoint_at_all`. The tree wins. The
   two-manifest test therefore strands its candidate the way the tree actually
   permits: a unit that wrote a candidate and did **not** reach `done`, via a
   verification discrepancy — the `incomplete` case, which is the real
   surviving-candidate scenario and exercises the identical defect (the guard
   tests this unit, the call promoted anyone's candidate). The prompt's capped
   premise is the only part of its test spec that could not be honoured as
   written; the property it was reaching for is proven.

## Inferred rather than verified

- No BERDL pod, no KBDL endpoint and no real arc store were reachable, so the
  store shapes remain the fixtures `bf-arc-adapter` built from Albert's
  2026-10-06 survey. This change does not touch the store-reading code.
- The 13 uncollectable modules and the 358 shared failures are attributed to
  missing optional dependencies in this venv on the strength of their
  collection errors; they were not individually diagnosed. They are identical
  on both commits, which is the only claim this task needs from them.
