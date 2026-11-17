# Work Record — gomf-gt4-conflicts-sample

## task_id
task-b0ffefd3 (PRD go-mf-council-autoresearch-v1, task 4 of 9)

## branch
maestro/developer/gomf-gt4-conflicts-sample

## base_commit
78a9b19 — `docs(council): live smoke of go_mf_adjudicate@1 on h100's real seats`

## precondition
MET. `src/kbutillib/domains/ai/llm/apis/schema_compat.py` is present on the
base commit (11,253 bytes), so the `council-seat-schemas-and-capture-v1` build
has landed.

## summary
Added conflict extraction and the seeded sampler to
`kbutillib.domains.ai.council.go_tuning`, plus a two-subcommand CLI.

**Two matchers now live side by side, and the distinction is the heart of this
task.** `match_items` — the bench report's `item_id` matcher, which REFUSES on
any ambiguity — moved out of `scripts/council/alcf_bench_report.py` into
`go_tuning/conflicts.py` unchanged, and the script imports it. Beside it,
`match()` is the tuning extractor: it joins on `(entity_hash, data_hash)` and
ignores `item_id` entirely, because on the real stores `item_id` does not
identify an entity across stores (Bench A's 12-hex ids are prefixes of the
64-hex `entity_hash`, and one entity's `go_mf@4` baseline spans ~29 rows).
Refusing on that shape, as `match_items` rightly does, would refuse the whole
corpus; so `match()` coalesces agreeing answers per side and marks only the
keys whose own side disagrees with itself `unstable_alcf`/`unstable_gomf`,
excluding and counting them rather than failing the run.

`sample.build_manifest()` is a pure function of `(matched, seed, plan)` — no
clock, no uuid, no unordered iteration — so the same three inputs produce a
byte-identical file. Pool assignment is made on `entity_hash`, and every row
sharing a pilot entity is withheld from tuning and heldout (reported as
`excluded_pilot_entity`), so tuning on one menu of a protein cannot leak into
the pilot's other menu for the same protein.

## files_touched

New:
- `src/kbutillib/domains/ai/council/go_tuning/conflicts.py` — `match_items` (legacy, moved), `match()`, `classify()`, `sub_kind()`, `MatchedItem`, `MatchResult`, `Refused`, `selected_set`, `verdict_ok`.
- `src/kbutillib/domains/ai/council/go_tuning/sample.py` — `Plan`, `Manifest`, `build_manifest()`, `execution_order()`, `largest_remainder()`, `POOLS`.
- `scripts/council/go_tuning.py` — `extract` and `sample` subcommands, one subparser each.
- `tests/domains/ai/council/go_tuning_support.py` — the synthetic two-store `SqliteStore` fixture (`make_store`, `two_stores`, `row`, `hash_of`).
- `tests/domains/ai/council/test_go_tuning_conflicts.py` — 31 tests (parametrisation included).
- `tests/domains/ai/council/test_go_tuning_sample.py` — 41 tests.
- `tests/domains/ai/council/test_go_tuning_script.py` — 7 tests.

Modified:
- `scripts/council/alcf_bench_report.py` — deleted `BASELINE_QUERY`, `class Refused`, `selected_set`, `_verdict_ok` and `match_items` (65 lines); imports all five from `go_tuning.conflicts`, aliasing `verdict_ok as _verdict_ok` so the rest of the file is untouched. `defaultdict` dropped from the `collections` import (it was used only by `match_items`). No other behaviour changed.
- `src/kbutillib/domains/ai/council/go_tuning/__init__.py` — docstring only; lists the two new modules and states that they are deliberately NOT re-exported, so importing the package still pulls in nothing but `policy`.

## verification

All runs: `TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest <sel> -q -p no:cacheprovider --no-header`

| selection | before (base 78a9b19) | after (branch) |
|---|---|---|
| the two pre-existing `test_alcf_bench_report_*` files | 18 passed (exit 0) | 18 passed (exit 0) |
| the three new `test_go_tuning_*` files | — | 79 passed (exit 0) |
| `tests/domains/ai/council/` (whole dir) | 8 failed, 1605 passed, 7 skipped in 356s (exit 1) | 6 failed, 1684 passed, 7 skipped in 294s (exit 1) |

`build-baselines/kbutillib-h100-main-6310d61.json` already exists, so no new
baseline file was written and the full suite was NOT re-run (it takes ~47-57
min on this box, 5-6x the sub-agent Bash ceiling). The council directory was
run instead, at BOTH the base commit and the branch tip, and compared by
nodeid.

The base-commit run was taken in a detached `git worktree` under
`$MAESTRO_TASK_SCRATCH/base-wt` so both runs measure the same tests from the
same interpreter; only nodeids are compared, not counts or locations.

**NO REGRESSIONS, compared by nodeid.** The set failing on the branch and not
on base is EMPTY. The branch's 6 failures are exactly the six
`test_types_config.py::TestPackagedDefaultConfig` nodeids that
`build-baselines/kbutillib-h100-main-6310d61.json` records as machine-local:
h100's `~/.kbutillib/config.yaml` leaks past those tests' monkeypatched
`HOME`. They are untouched by this task and fail identically on base.

Base failed TWO MORE than the branch, both timing-sensitive and both in the
other direction (they fail on base, pass on the branch), so neither can be a
regression:

- `test_daemon.py::TestQueueMode::test_stopping_one_queue_leaves_siblings_running_and_restart_policy_holds`
- `test_four_queue_integration.py::TestConcurrentProgressAndAccountSafety::test_fast_seat_progresses_while_the_shared_account_is_saturated[sqlite]`

Both were re-run in isolation at the base commit and PASSED (2 passed in
25.70s), so they are load flakes from a contended run on this shared box, not
a property of either commit. Nothing in this task touches the daemon, the
queue or the claim path.

Lint: `ruff check` (from `/home/chenry/venvs/kbdl-recon-scratch-layer/bin/ruff`,
since neither `ruff` nor `pytest` is installed in this worktree's `.task-venv`
and `kbdl` has no `ruff`) passes on every file this task added or modified.
The 4 pre-existing `ruff` errors under `scripts/council/` are in
`anthropic_acceptance_rehearsal.py` and `batching_accuracy_ab.py`, neither of
which this task touches; they were left alone.

A live CLI smoke was run on a 421-key synthetic two-store corpus (including
the h100 messy-entity shape): `extract` printed all six class counts, 261
conflicts and `baseline:cancelled 1`, and `sample` produced
pilot 150 rows/180 entries, tuning 66, heldout 45, regression 40,
decline_guard 120 — i.e. 66/111 = 59.5% of the post-pilot entity groups to
tuning, and `min(400, 120)` on the guard.

## success_criteria_check

- **`go_tuning.conflicts` and `go_tuning.sample` exist with the specified
  functions** — PASS. `conflicts.match(candidate_store, candidate_jobs,
  baseline_store, baseline_jobs)` takes SEVERAL candidate jobs and accepts
  either an open store or a URL on each side; `conflicts.classify(matched)`
  returns exactly one of the six classes and `conflicts.sub_kind(matched)` the
  partial sub-kind; `sample.build_manifest(matched, seed, plan)` returns a
  `Manifest`.
- **`alcf_bench_report.py` delegates matching to `go_tuning.conflicts` and its
  pre-existing tests pass unchanged** — PASS. Both test files, 18 tests, byte
  unchanged; `grep` confirms no other module in the repo imported `match_items`.
- **`scripts/council/go_tuning.py` has `extract` and `sample` subcommands** —
  PASS, one subparser each with its own `--out`
  (`test_both_subcommands_exist_and_a_command_is_required`).
- **Six classes** — PASS. `test_all_six_classes_are_found_and_counted` asserts
  the full counts dict over two real stores;
  `test_classify_returns_exactly_one_class_and_the_right_sub_kind` is
  parametrised over all six classes and all three partial sub-kinds.
- **De-dup** — PASS.
  `test_one_entity_across_several_baseline_jobs_coalesces_to_one_answer`
  (3 baseline rows across 2 jobs -> 1 item, `baseline_answers == 3`,
  `duplicate_count == 2`, provenance `("j-base-1", "b-aa")` = the
  lexicographically lowest `(job_id, item_id)`) and
  `test_a_duplicated_candidate_entity_also_coalesces`.
- **Determinism** — PASS.
  `test_the_same_seed_gives_a_byte_identical_manifest` builds from the item
  list AND from its reverse and compares `to_json()`;
  `test_the_manifest_round_trips_to_a_file_byte_identically` compares bytes on
  disk; `test_sample_is_byte_identical_across_two_invocations` does it through
  the CLI.
- **No cross-pool entity** — PASS.
  `test_no_entity_hash_appears_in_two_of_pilot_tuning_heldout` over a
  100-entity x 3-menu corpus asserts all three pairwise intersections empty.
- **Refusal below 100** — PASS.
  `test_fewer_than_100_conflicts_refuses_and_quotes_the_extra_items_needed`
  asserts the message carries `99`, the floor, the observed rate `49.5%`
  (`99/200`) and the extra ALCF items needed for both the floor and the full
  150-item pilot; `test_exactly_100_conflicts_is_accepted` pins the boundary.
- **Pilot strata match the plan** — PASS. `test_pilot_strata_match_the_plan`
  pins the exact largest-remainder answer on a known population:
  `disjoint|tool_a 56, disjoint|tool_b 19, partial|tool_a 38, partial|tool_b 37`.
- **No test that passed on the base commit fails** — PASS, by nodeid. See the
  verification table.

## the binding contracts, one by one

- **C3** — admission is item status AND verdict status `ok`; everything else is
  counted by status in `MatchResult.excluded`
  (`test_error_pending_and_no_verdict_rows_are_excluded_and_reported` asserts
  the exact dict). `match_items`' legacy behaviour is kept verbatim.
- **C4** — the tuning extractor keys on `(entity_hash, data_hash)` and ignores
  `item_id` (`test_item_ids_differing_between_stores_still_match_by_key`);
  `baseline_jobs=None` means every `go_mf@4` job in the store; agreeing
  answers coalesce with the lowest `(job_id, item_id)` as provenance and an
  answer count; disagreeing ones become `unstable_alcf`/`unstable_gomf`,
  excluded and counted.
- **C5** — sampling is without replacement
  (`test_pools_are_sampled_without_replacement`); pilot is
  `min(150, available)` and refuses only below 100; a short class contributes
  all its rows and the other fills the rest
  (`test_a_short_class_contributes_all_and_the_other_fills_the_rest`:
  130 + 20); source allocation within class is deterministic largest-remainder;
  each guard slice is `min(requested, eligible)` with the denominator and
  shortfall recorded.
- **C6** — every row sharing a pilot entity is excluded from tuning and
  heldout and recorded as `excluded_pilot_entity`; the 60/40 split is over
  ENTITY GROUPS by entity count with a seeded shuffle, all of an entity's menus
  travelling together (`test_the_split_is_60_40_over_entity_groups` checks the
  per-entity row completeness); realised row and stratum counts are reported;
  execution order per tranche is a seeded weighted interleaving of
  `class x source` strata, frozen in the manifest.
- **C9** — manifest entries are `(key, orientation)`: a swapped row emits a
  SECOND entry over the same key with the opposite `alcf_is_A` and the id
  `<item_id>:swap` (`test_the_swap_slice_adds_an_opposite_orientation_twin`).

## reconciliation against the tree

The prompt was accurate about this tree; four divergences, all small, all
fixed rather than escalated:

1. **The prompt says items come from "`get_items`/results".** There is no
   `results` method on `SqliteStore`; the reader is `get_items(job_id)` only
   (`store/sqlite.py:1329`). Used `get_items`, plus `list_jobs()` and
   `get_job()` for baseline discovery and the query-type check.
2. **The prompt says "Stores are opened with `SqliteStore(url)`" while the
   signature it specifies takes stores.** Resolved by accepting either: a URL
   string is opened with `SqliteStore(url, create_parents=False)` and closed
   again, an already-open store is used and NOT closed
   (`test_match_accepts_an_already_open_store`). The CLI passes URLs.
3. **`classify(matched)` "returns exactly one of" six classes, but partial
   "also records sub-kind".** One function cannot return one class and also a
   sub-kind, so `classify()` returns the class string exactly as specified and
   `sub_kind()` is a second function returning the partial sub-kind or `None`.
   `match()` calls both and records them on the item. This is a naming
   choice, not a behaviour change.
4. **The prompt's `~29 jobs` figure and the `009948a3814c` entity are h100
   production facts from task 3, not things in this tree.** The test models the
   SHAPE (`test_the_measured_h100_shape_drops_cancelled_rows_and_go_mf3`: a
   cancelled `go_mf@3` row, a cancelled `go_mf@4` row and one good `go_mf@4`
   answer for the same entity) rather than asserting the production numbers,
   which no test in this repo can see.

One correction to the executor notes: they say "7 machine-local failures listed
in `build-baselines/kbutillib-h100-main-6310d61.json`". That file records
**six** (its own `CORRECTION_2026-10-25` block raised the figure from four to
six and lists them), and six is what both runs observed. The seventh failure
the gt2 work record mentions is in `tests/domains/ai/llm/test_static_headers.py`,
outside the council directory and outside both of this task's runs.

## inferred rather than verified

- **The real stores were never opened.** Every number in this record comes from
  synthetic fixtures. The claims that Bench A's `item_id` is a 12-hex prefix of
  a 64-hex `entity_hash`, that one entity's baseline spans ~29 rows, and that
  entity `009948a3814c` carries cancelled `go_mf@3` and `go_mf@4` rows are
  taken from the task prompt's report of task 3's h100 measurements. They are
  the REASON C4 exists and they shaped the tests, but this task did not
  re-measure them.
- **`mypy` was not run** — it is not installed in `/home/chenry/venvs/kbdl`.
  `ruff` was run from another venv's binary, read-only.
- **Two policy choices the contracts left open**, both recorded in the manifest
  rather than hidden:
  - When BOTH sides of a key are unstable, the key is labelled
    `unstable_alcf` — the candidate is the side under test.
    (`test_both_sides_unstable_is_labelled_for_the_side_under_test`.)
  - A row withheld for sharing a pilot entity stays ELIGIBLE for the two guard
    slices. C6 withholds it from tuning and heldout only, and excluding it from
    the population guard as well would bias that guard towards the entities
    nothing else wanted. Where a guard does share an entity with a signal pool,
    the manifest reports the count as `guard_entity_overlap` rather than
    preventing it.
- **An item whose status is `ok` but which carries NO verdict is excluded as
  `no_verdict`** by the tuning extractor, not read as an empty selection. The
  legacy `verdict_ok` (and therefore the bench report) is lenient here and was
  left exactly as it was; the divergence is documented at both functions.
- **`sample` cannot recover the extractor's diagnostics from a JSONL.** Rather
  than omit them — which reads as "zero unstable keys" — the manifest always
  carries an `extract_diagnostics` block that says `carried: false` and where
  to read them instead.

## no-touch confirmations

The council engine (`C/engine/`), the store schema
(`C/store/schema.py`), and every existing query type are unmodified. No new
JSON schema of any kind was added, so the strict-clean rule (no `$schema`,
every property in `required`, `additionalProperties: false`, a required
`notes` string on every role) had nothing to apply to in this task. The only
pre-existing file whose behaviour could have moved is
`scripts/council/alcf_bench_report.py`, and its 18 tests pass unchanged.
