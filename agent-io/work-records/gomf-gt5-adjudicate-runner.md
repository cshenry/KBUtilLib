# go_mf tuning task 5 — the adjudication runner, the ledger, and the gates

Maestro task `task-9ec9e137`, branch `maestro/developer/gomf-gt5-adjudicate-runner`,
base `5af606d` (KBUtilLib `main`, go_mf task 4's conflicts + sampler).
Written 2026-11-16 on h100.

## precondition

`src/kbutillib/domains/ai/llm/apis/schema_compat.py` is PRESENT (11,253 bytes,
exporting `strict_problems`, `strip_meta_keywords`, `walk_schema`). The
`council-seat-schemas-and-capture-v1` build has landed, so the task proceeded.

## what was added

### `src/kbutillib/domains/ai/council/go_tuning/ledger.py` (new, 366 lines)

`Ledger(run_dir, invocation_id=None, code=None)` appending one JSON object per
line to `run_dir/ledger.jsonl`.

- `append(kind, **fields)` — stamps `kind`, `ts` (UTC ISO with `Z`), `code`
  (`code_version()`, so the commit is on every line), `invocation_id` and
  `seq`; refuses a `kind` outside the closed `LEDGER_KINDS` tuple and refuses
  `**fields` that would shadow those five. Flushes and `os.fsync`s before
  returning; a failed write raises `LedgerError` and is never swallowed.
- `records(kind=None)` / `last(kind=None)` — file order, filtered by kind.
- Writers serialise on `run_dir/ledger.lock` with a blocking `fcntl.flock`
  (`_FileLock`), which is the same mechanism `council/execlock.py` uses.
- Per-kind field conventions are tabulated in the module docstring, including
  the two kinds this task does not write (`candidate` carries a candidate
  prompt's `name@version` and its PARENT version; `score` carries `metrics`).

**Two defects in my own first draft, found by the tests and fixed:**

1. `append` concatenated onto an unterminated final line. A process killed
   mid-write leaves no trailing newline, so the next record was spliced into
   the damaged one and BOTH were lost — turning the reported, ignorable
   truncation C22 asks for into a silent gap. `append` now writes a newline
   first when `_unterminated()`.
2. The first draft raised on any unparsable line that was not the last. That
   makes a ledger permanently unreadable from the second append after a
   truncation onwards (the damaged line stops being last), i.e. it bricks the
   exact restart the ledger exists to serve. Replaced: an unparsable line is
   reported on `Ledger.damaged` (and on `Ledger.truncated` when it is last)
   and skipped, while a record whose `seq` does not match its position raises
   — that is a MISSING record, which is the case worth refusing, and `seq`
   is what distinguishes the two.

### `src/kbutillib/domains/ai/council/go_tuning/adjudicate.py` (new, 1,755 lines)

- `render_answer(menu, ids)` / `parse_answer(text)` — `GO:id | label` per line
  in MENU order, `(no terms)` when empty, `(not on the menu)` for an id the
  menu does not hold. `parse_answer` is the inverse and is how `read_results`
  recovers the two sets from the item's own stored evidence.
- `build_items(manifest, matched, pools=None)` → `CouncilTask`s with evidence
  `{text, source, answer_a, answer_b}`, A/B assigned by the entry's
  `alcf_is_A`, the menu copied unchanged, ordered by the manifest's frozen
  `execution_order` per pool.
- `blinding_problems(items)` — every place an item's evidence or id contains
  `alcf`, `go_mf` or `gomf`, case-insensitive. `run_tranches` refuses on a
  non-empty result before anything is submitted.
- `adjudicate_spec_hash(query)` — a digest of the query spec **plus**
  `MAPPING_POLICY`, because the policy reaches the models through the renderer
  and so changes the question without changing the spec, the version or any
  `prompt_hash` (`policy.py` says so itself). A calibration record matching on
  the spec alone would keep admitting tranches after the standard moved.
- `submission_token(...)` — content-keyed over run, manifest, tranche index,
  `name@version`, roster fingerprint and every planned item's evidence/menu
  hash. Deterministic, so a later process recomputes it.
- `item_key(query, task)` / `_already_ok(...)` — C9's reuse key.
- `usage_report(store, job_ids)` — `argo_tokens`, `unmetered_tokens`,
  `other_tokens`, `by_endpoint_kind`, `attempts`, `missing_usage`.
- `calibration_gate(ledger, ...)` and `spot_check_ids(token, ids, size)`.
- `run_tranches(store_url, profile, items, tranche_size=100,
  token_ceiling=3_000_000, *, ledger, manifest_id="", council=None,
  query_ref=..., work=True, retry_failed=False)`.
- `read_results(store_url, job_ids, manifest) -> List[ItemResult]`,
  `derived_verdict(final_ids, set_a, set_b, deciding_prefers=None)`,
  `verdict_side(derived, alcf_is_a)`.

### `scripts/council/go_tuning.py` (modified, +137 lines)

New `adjudicate` subparser with `--manifest --matched --pools --store
--profile --tranche-size --token-ceiling --run-dir`, plus `--retry-failed`
(C11 names it) and `--no-work`. Exit codes documented: 0 done, 2 refused,
**3 stopped at a gate with work still planned** — a partial corpus reported
as success is how a hole gets carried into the scoring.

### tests

- `tests/domains/ai/council/test_go_tuning_ledger.py` (new, 17 tests)
- `tests/domains/ai/council/test_go_tuning_adjudicate.py` (new, 43 tests)
- `tests/domains/ai/council/test_go_tuning_script.py` (+4 tests)

The adjudicate tests run the SHIPPED `go_mf_adjudicate@1` builtin through the
real `CouncilWorker` over `FakeBackend`s, against a `SqliteStore` under
`tmp_path`. The roster seats two proposers plus critic and meta on an `argo`
endpoint and one proposer on a `claude_cli` one, both reporting **real**
usage — 1,000 tokens per ARGO call and 20,000 per CLI call — which is what
makes the C10 metering tests discriminating rather than decorative.

## how each binding contract is met, and where it is tested

| contract | how | test |
|---|---|---|
| blinding | `blinding_problems` over evidence + id; `run_tranches` refuses | `TestBlinding` (5) |
| swap | one task per manifest entry; `:swap` id, answers reversed | `TestSwapItems` (5) |
| C7 | meta's metadata when `decided_by == "meta"`, else proposer unique-mode / sorted union / lowest confidence; `proposer_consensus` reported either way; critic separate | `TestReadResults` (6) |
| C8 | `_validated_reply` reads `calls.reply_parsed` only, follows `reused_from` (across jobs), validates against the role schema, raises otherwise | `test_a_settled_ok_call_with_no_validated_metadata_fails_the_read`, `test_a_reply_that_does_not_satisfy_its_role_schema_fails_the_read` |
| C9 | `submission_token` + `item_key` cover query version, evidence/menu hashes, roster fingerprint and orientation | `test_the_token_is_the_same_in_a_later_process_and_covers_orientation`, `test_the_same_id_in_the_other_orientation_is_not_an_answer` |
| C10 | soft per-invocation threshold on `argo_tokens`; CLI kinds reported, never metered; missing usage on an ARGO ok call raises | `TestTokenCeiling` (4) |
| C11 | token fsync-ed `planned` before submit, carried in the label, reconciled from the store before any job is created; non-terminal jobs resumed; error/cancelled items HALT | `TestResume` (4), `TestHaltAndGates` (2) |
| C13 | `calibration_gate` + `spot_check_ids` (5 trusted / 20 conditional, capped) | `TestHaltAndGates` (6) |
| C22 | flush+fsync, lock file, truncated last line reported and skipped, invocation + submission ids on every record | `test_go_tuning_ledger.py` (17) |

## reconciliations — what the prompt claimed, what the tree held

1. **"For items with swap=true, also emit a second item with the order
   reversed and item_id suffixed ':swap'."** The tree already does this. Task
   4's `sample.py` emits TWO manifest entries for a swapped row
   (`sample.py:487-492`): the original with `id == item_id`, and a twin with
   `id == f"{item_id}:swap"` and `alcf_is_A` **already flipped**. A manifest
   entry is therefore a `(key, orientation)` pair, not a row. `build_items`
   emits exactly one task per entry; emitting a second for `swap=True` would
   have produced `c-pa03:swap:swap`. The observable result is what the prompt
   and C9 describe — a task with id `<item_id>:swap` whose answers are
   reversed — and it is asserted directly.

2. **The per-item token estimate is withdrawn by the prompt's own later
   text.** The task body asks for "the mean of completed tranches, or
   1,500,000/100 per item if none"; C10 says it "replaces the per-item
   estimate rule above". The later binding wins, so **nothing is estimated**:
   the gate reads completed usage out of the store and admits a tranche only
   while that total is below the ceiling. One tranche may overshoot; no second
   one can, and the overshoot is recorded (`stop` reason
   `token_ceiling_overshoot`).

3. **C10's premise about null usage is out of date, as the executor note
   says.** "ARGO-billed" is decided by endpoint kind — `ARGO_BILLED_KINDS =
   ("argo",)`, `UNMETERED_KINDS = CLI_KINDS` — read off `calls.endpoint_kind`,
   never off whether a usage figure is null. Note that `calls.endpoint_kind`
   IS the seat's kind, recorded per call by `worker._call_row` (worker.py:1145);
   reading it there rather than off `jobs.roster` additionally survives a
   per-job roster override.

4. **`run_tranches(... tranche_size=100, token_ceiling=3_000_000, ledger)` is
   not valid Python** — a positional parameter cannot follow defaults.
   `ledger` is keyword-only and REQUIRED: C11 needs the token durable before
   the submit, so there is no ledger-free mode that is safe.

5. **The store contract publishes no per-attempt usage read.** C10 meters
   "across all seats and attempts", and `calls.fresh_usage` cannot answer that
   — a retried call paid for every attempt, and only `attempts.usage` holds
   them all. `usage_report` therefore reads the `attempts`×`calls` join
   through `store.engine` and raises `TypeError` for a store without one,
   which is exactly the precedent `quote.fit_token_model` set (quote.py:336)
   for the same table and the same reason. No store method, schema or query
   type was modified.

6. **The executor note's "7 machine-local failures" is six.** The baseline
   file's own `MACHINE_LOCAL_FAILURES_NOT_PRODUCT_DEFECTS` block records SIX
   and carries a `CORRECTION_2026-10-25` that had already raised the figure
   from four; task 4's work record corrected the same claim and placed the
   seventh in `tests/domains/ai/llm/test_static_headers.py`, outside this
   task's selection. Six is what both runs below observed.

7. **`_opened` is imported from `.conflicts`.** A private of a sibling module
   in the same package, taken rather than duplicated; it is the one helper
   that decides whether `read_results` closes a store it opened.

## one deliberate strengthening beyond the literal prompt

The task body says `run_tranches` must "never re-submit an item_id that
already has an ok result in the target store". Keyed on the id alone, that is
wrong in a way that costs correctness rather than money: C9 says the
resume/reuse check keys on "query version, evidence/menu hashes, roster
identity and orientation", and C9 is binding. Two manifests can name the same
row, and the same row in the opposite orientation is a different question
about the same protein. So `_already_ok` keys on
`(item_id, entity_hash, data_hash)` — computed by the ENGINE's own
`entity_hash`/`data_hash` over the engine's own split of the evidence, so they
are the values the item row actually carries — and only counts jobs whose
`roster_fingerprint` matches. The plain-id rule is a strict subset of this
one. `test_the_same_id_in_the_other_orientation_is_not_an_answer` pins it.

## verification

All runs: `TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src
/home/chenry/venvs/kbdl/bin/python -m pytest <sel> -q -p no:cacheprovider
--no-header`.

| selection | base (`5af606d`) | branch (`c8b0e5c`) |
|---|---|---|
| `test_go_tuning_adjudicate.py` | — | **43 passed**, exit 0 |
| `test_go_tuning_ledger.py` | — | **17 passed**, exit 0 |
| `test_go_tuning_script.py` | 7 passed | **11 passed**, exit 0 |
| `tests/domains/ai/council/` (whole dir) | 6 failed, 1686 passed, 7 skipped in 296s, exit 1 | 6 failed, **1750 passed**, 7 skipped in 287s, exit 1 |

**NO REGRESSIONS, compared by nodeid.** The set failing on the branch and not
on base is EMPTY, and so is the set failing on base and not on the branch —
the two failing sets are identical, 6 and 6. They are exactly the six
`test_types_config.py::TestPackagedDefaultConfig` nodeids that
`build-baselines/kbutillib-h100-main-6310d61.json` records as machine-local:
h100's `~/.kbutillib/config.yaml` leaks past those tests' monkeypatched
`HOME`. Nothing in this task touches the config loader, and they fail
identically on base. The +64 passed are this task's own new tests.

`build-baselines/kbutillib-h100-main-6310d61.json` already exists, so no new
baseline file was written and the full suite was NOT re-run: that file records
47–57 min on this box, 5–6× the 600 s sub-agent Bash ceiling, and its own
`WHY_DURATION_MATTERS` note says only a conductor may run it. The council
directory was run at BOTH the base commit and the branch tip instead, and
compared by nodeid. The base-commit run was taken in a detached
`git worktree` under `$MAESTRO_TASK_SCRATCH/base-wt`, so both runs measure the
same tests from the same interpreter.

Lint: `ruff check` (from `/home/chenry/venvs/kbdl-recon-scratch-layer/bin/ruff`,
since neither `ruff` nor `pytest` is in this worktree's `.task-venv` and
`kbdl` has no `ruff`) passes on every file this task added or modified.

## what was inferred rather than verified

- **No real store was opened and no real seat was called.** Every number here
  comes from `FakeBackend`s. The per-call token figures the tests use (1,000
  ARGO, 20,000 CLI) are shaped after the measurements the task prompt reports
  from task 3's live h100 run (codex ~17K prompt tokens, claude 20–50K); they
  are a plausible SHAPE, not a measurement taken here.
- **The 21-calls-for-5-items shape is modelled, not re-measured.** The fixtures
  rely on the engine skipping meta on a unanimous item, which this suite does
  observe (`verdict.meta_skipped is True`, and no meta call row), but the ratio
  of meta runs to items on real traffic is the task prompt's report of task 3.
- **`allow_meta_overlap` was not exercised.** The executor note says h100's
  adjudicate roster needs it because `argo-opus5` and `opus-cli` share a
  canonical model. Nothing here hardcodes a roster — `run_tranches` resolves it
  from `profile` through `council.roster(profile)` and records only its
  fingerprint — so the flag is the operator's config and this code never reads
  it. The test roster seats five distinct canonical models and so does not need
  it, which means the overlap path itself is untested here.
- **`mypy` was not run**; it is not installed in `/home/chenry/venvs/kbdl`.
- **`--no-work` is untested end to end.** It submits and leaves the calls for a
  daemon, at which point the caller owns the no-overlap rule; the option is
  wired and asserted at the CLI boundary (`seen["work"] is True` for the
  default) but no test drives a daemon.
