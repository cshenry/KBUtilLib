# go_mf tuning task 7 — the isolated-run helper, the held-out scorer, the stop rule

Maestro task `task-c8de6fcb`, branch `maestro/developer/gomf-gt7-score`,
base `4528fe9` (KBUtilLib `main`, go_mf task 6's pilot table + calibration).
Written 2026-11-16 on h100.

## precondition

`src/kbutillib/domains/ai/llm/apis/schema_compat.py` is PRESENT (11,253 bytes),
so the `council-seat-schemas-and-capture-v1` build has landed and the task
proceeded.

The council engine (`C/engine/`), the store schema and every existing query
type are untouched. One existing module grew: `go_tuning/ledger.py` gained the
record kind `run` (see "the one existing file that changed" below). The
`go-mf-council-autoresearch-v1` PRD was not readable from this worktree (it is
committed on `wip`); everything was built from the task prompt.

## what was added

### `src/kbutillib/domains/ai/council/go_tuning/run.py` (new, 441 lines) — C14

One isolated sqlite store per logically independent run, and PROOF the run was
fresh.

- `fresh_run(spec_source_store, query, version, items, profile, run_dir, *,
  ledger, label=None, council_factory=None, work=True, submitted_label=None)`
  → `FreshRun` (a dict with `label`, `store`, `job_id`, `query`, `profile`,
  `n_items`, `status`, `fresh`, `reused`, `reused_call_ids`, `other_jobs`).
  Creates the store at `run_dir/stores/<label>.sqlite`, copies the spec in from
  the loop store, submits, works the job, then counts `calls.reused_from` and
  **refuses** the run if any call reused an answer.
- `isolated_store(path)` — creates and opens an EMPTY store, refusing a path
  that already exists and turning the store's own `StorePathRefused` into a
  `Refused` that names the directory.
- `reused_calls(store, job_id)` — the sorted call ids whose `reused_from` is
  set. Read from the store, not from a log line.
- `store_path_for(run_dir, label)` — where a run WILL live, without creating it.
- `go_mf_tasks(manifest, matched, pools=None)` — builds the `go_mf`
  `CouncilTask`s a manifest's pools call for, from the extract's frozen
  `evidence_text` / `source` / `menu`, de-duplicated by `item_id`. Needed
  because nothing in the package built `go_mf` items from a manifest and the
  `fresh-run` subcommand would otherwise have had no items to run.

Three refusals, each with its own failure mode:

1. **An existing store file.** Opening it is indistinguishable from the thing
   C14 exists to prevent — it may already hold answers this run would reuse.
2. **A refused store location** (a `Dropbox` path component, or a network
   filesystem). Reported with the directory named and `/scratch` suggested;
   **never relocated**, per the executor note. An operator who cannot see
   where their measurement went cannot fix the one thing that is wrong.
3. **Any reused call, or any second job in the store.** Both are recorded in
   the ledger as `phase: "not_fresh"` with the offending ids BEFORE the raise,
   so the evidence survives the refusal.

`council_factory` is a FACTORY, not a facade (unlike `adjudicate.run_tranches`,
which takes `council=`). The store does not exist until `fresh_run` creates it,
so a facade handed in ready-made would have had to open — and therefore create
— the path `isolated_store` must find absent.

### `src/kbutillib/domains/ai/council/go_tuning/score.py` (new, 1,687 lines)

- `score(candidate_store, candidate_job, baseline_store, fresh_baseline_job,
  gold, manifest, noise_floor, *, band, pools=SCORE_POOLS, agreed=None,
  incumbent=None, incumbent_label=None, ledger=None, seed, resamples)`
  → `Score`. Reads `heldout` + `regression` + `decline_guard`.
- `audit(candidate_job, fresh_baseline_job, chris_gold, manifest, *,
  noise_floor=None, band="", ledger=None, seed, resamples)` → `Score`. Reads
  `pilot` and nothing else; recorded as a `score` record with `audit=True`.
- `measure_noise(job1, job2, manifest, *, slices=GUARD_POOLS)` → the floor over
  the UNION of the guard slices' unique keys, plus `per_slice`.
- `paired_bootstrap(candidate, baseline, groups, *, resamples, seed, level)`.
- `stop_rule(ledger, *, record=True)`.
- `RunRef(store, job_id)` with `RunRef.of()`, coercing a `RunRef`, a
  `(store, job_id)` pair or a `FreshRun` report. A job id alone cannot name a
  measurement once C14 puts every run in its own file, which is why
  `measure_noise` and `audit` take runs rather than bare job ids.

Metrics, exactly as specified:

| metric | definition |
|---|---|
| `primary` | share of scored heldout items (gold present, gold not off-menu) whose final id set equals the gold set EXACTLY, per side |
| `gain` | `primary(candidate) - primary(baseline)` |
| `decline` | share of `decline_guard` items that are `ok` with an EMPTY selected set, per side |
| `regression` (candidate, gated) | share of `regression` items whose set differs from **the same iteration's fresh baseline** (C15) |
| `regression` (baseline, reported) | share differing from the **agreed set** out of the extract — needs `agreed=`, and says so when it is absent |
| secondary | `conflict_rate` (overall and per pool), `tokens` (via `adjudicate.usage_report`), `latency` (per-attempt service time and the job's wall span) |

The gain rule is four tests, all required, each with the reason it failed:
`gain > 2 * noise_floor`; paired-bootstrap 95% lower bound `> 0`; candidate
decline rate `<= baseline + 0.02`; candidate regression flip rate
`<= noise_floor + 0.02`.

An EMPTY guard pool FAILS its test rather than passing by default. A run with
no decline guard has not shown the decline rate held, and calling that a gain
is the failure the guard exists to catch. (Tested.)

The bootstrap: 1,000 resamples, fixed seed `20261117`, grouped by
`entity_hash`, drawn from a local `random.Random(seed)` over sorted group keys
— so the interval does not move when other code uses the global RNG (tested).
Bounds are nearest-rank empirical quantiles, no interpolation: the resample
distribution is discrete and an interpolated bound would claim precision the
1,000 draws do not carry.

### `scripts/council/go_tuning.py` — three subcommands

`fresh-run`, `score`, `audit`, with the module docstring's worked example
extended through the loop. `score`/`audit` share their option block.

- `--gold` is repeatable and LATER WINS, so the merge the PRD asks for
  (adjudicated first, Chris's marks second) is a command-line ordering. Each
  file is what `read-marks --out` writes, so `GoldRecord.provenance` already
  distinguishes `human` from `judge`.
- `--band` is READ off the run's newest `calibration` record when not given.
  A band typed by hand is a band that can be typed wrong in the permissive
  direction, and the band is what decides whether judge-derived gold may be
  scored against at all.
- `--noise-floor F` or `--noise-run STORE#JOB` twice (mutually exclusive); the
  second form measures the floor in place via `measure_noise`. `#` rather than
  `:` because a sqlite URL is full of colons. This is how `measure_noise`
  became CLI-reachable without inventing a fourth subcommand.
- `--matched` on `score` supplies the agreed sets (regression rows are
  `agree_assigned`, so either side's selected set IS the agreed set).
- `--incumbent STORE#JOB` / `--incumbent-label` for C19 once the incumbent is
  no longer `go_mf@4`.
- `score` evaluates the stop rule after recording and prints `STOP (<reason>)`
  to stderr, or `continue:` with the counters, to stdout.

Exit codes are the file's existing three: 0 done, 2 refused, 3 unfinished
(`fresh-run --no-work`, which has submitted but proven nothing). A stop firing
is exit 0 — the run is over, not unfinished — and is reported on stderr.

### the one existing file that changed

`go_tuning/ledger.py`: `LEDGER_KINDS` gained `"run"`, between `calibration`
and `candidate`, with its row in the docstring's field table. C14 requires the
store and job identities of every independent run to be durable, the kind set
is CLOSED by design (`append` refuses anything else), and none of the seven
existing kinds describes a `go_mf` run. Nothing else in the module changed.
`test_go_tuning_ledger.py::test_the_kind_vocabulary_is_the_run_s_steps` pins
that tuple and was updated with the reason — that assertion exists to force
exactly this deliberate act, and growing it is not the same as renaming an
entry.

## how each contract is met, and where it is tested

### the pilot is locked

`score` refuses it three ways — by pool name, under an unknown alias, and when
a manifest entry puts a pilot item in a scored pool — and all three are tested
(`TestScoreRefusesThePilot`), as is "the scored set never contains a pilot item
even when the merged gold covers them". `audit` is the only reader of `pilot`
and refuses any gold set whose provenance is not `human`
(`TestAuditReadsOnlyThePilot`).

### C14

`fresh_run` + `reused_calls`. The central test
(`TestAReusedCallIsNotAFreshRun::test_a_real_cache_reuse_is_rejected`) produces
a GENUINE reuse: two items carrying the same annotation string and the same
menu render the same prompt, so `prompt_hash` collides and the engine's own
`_settle_reused` fires. Measured: 6 calls, 3 of them with `reused_from` set,
rejected. The test then re-reads `calls.reused_from` out of the store and
checks each offender's source call exists and is `ok`. A stubbed `reused_from`
would have passed against an engine that had stopped setting it.
`test_distinct_items_do_not_reuse_each_other` is the control.

### C15

`_require_comparable` refuses, with the offending ids: a required item missing
from a run; an item that did not settle `ok` (including `ok`-with-no-verdict,
which is NOT read as a decline); and an item whose frozen `evidence`, `menu`
or `data_hash` differs between the runs. Zero eligible gold is `UNSCORABLE`
and raises rather than scoring zero. A flip is selected-set inequality — tested
with a SUPERSET of the gold set, which scores 0, not partial credit. A decline
is `ok` plus an empty selected set. The candidate's regression flips compare to
the same iteration's fresh baseline — tested with both runs flipping the SAME
three items, where the candidate's gated rate is 0 and the baseline's drift
from the extract is 0.5. The noise floor is the fraction of unique keys in the
union of the two guard slices that differ between the two iteration-0
baselines, plus `per_slice`.

### C19

The incumbent starts as `go_mf@4`, which is the fresh baseline in the
comparison, so its primary on this subset is already measured. A non-`@4`
incumbent is passed as a RUN and **re-measured here** over this score's own
labelled subset, because gold grows between iterations and a primary recorded
in an earlier iteration was computed over fewer items. Replacement needs the
gain rule AND a strictly higher primary; ties keep the older version; the
no-improvement counter resets only on replacement. The off-menu stop is
`off_menu / (off_menu + incumbent on-menu misses) > 0.5`, with a zero
denominator reporting `None` rather than stopping.

`stop_rule` reads the ledger alone (the fields it matches on are promoted to
the top of each `score` record for that reason), skips `audit` records, and
each of the three branches has its own test that fires it in isolation:

| branch | how the test fires it |
|---|---|
| `no_improvement_streak` | three candidates that do not pass the gain rule |
| `off_menu_ceiling` | one candidate, six off-menu gold rows against zero on-menu misses under the incumbent → share 1.0 |
| `candidate_budget` | eight candidates that ALL replace the incumbent, so the streak is 0 and only the budget can fire |

Also tested: an off-menu share of exactly 0.5 does NOT stop (the rule is
strictly greater); the streak resets on a replacement; every reason that fired
is reported, not only the first; the `stop` record is written once and
`record=False` decides without writing.

## reconciliations between the task prompt and the tree

Seven, four of which changed what was built.

1. **`measure_noise(job1, job2)` and `audit(candidate_job, fresh_baseline_job,
   ...)` name no stores**, but C14 in the same prompt puts every run in its own
   store, so a bare job id does not identify a run. Built `RunRef(store,
   job_id)` with coercion from a `(store, job_id)` pair or a `FreshRun`
   report, and kept the prompt's parameter NAMES. `score` keeps its seven
   explicit positional arguments as written.
2. **"regression flip rate … differs from the agreed set, for both" vs C15's
   "candidate regression flips compare to the same iteration's fresh
   baseline".** C15 is the later and more specific binding and it governs the
   GATED number; the earlier phrasing would make the baseline's own rate
   identically zero and therefore useless. Built: candidate → fresh baseline
   (gated), baseline → agreed set (reported), plus
   `candidate_vs_agreed` for completeness. The agreed sets are not derivable
   from `score`'s named arguments, so they arrive as an optional `agreed=`
   kwarg and their absence is reported rather than guessed at.
3. **"three consecutive candidates without gain" vs C19's "3 consecutive
   non-improvements".** Implemented C19's (the later binding), which strictly
   contains the earlier one — a candidate without a gain can never replace the
   incumbent. Both counters are reported, and the three-no-gain case is tested
   to fire the stop.
4. **The prompt gives `score` no `band` parameter** but requires it to "state
   the calibration band" and refuse `not_trusted`. Added `band` as a required
   keyword-only argument; the CLI reads it off the run's newest `calibration`
   record when it is not typed.
5. **Nothing in the package built `go_mf` items from a manifest**, so a
   `fresh-run` subcommand with no `--items` source would have been unusable.
   Added `go_mf_tasks(manifest, matched, pools)`, taking the same
   `--manifest` + `--matched` pair every other subcommand takes.
6. **`run.py` did not exist** and `LEDGER_KINDS` had no kind for a run; see
   "the one existing file that changed".
7. **`council_factory` instead of `council`**, diverging from
   `run_tranches(council=...)`. Explained in the docstring: the store is
   created by the function being called.

## verification

h100, `/home/chenry/venvs/kbdl/bin/python` (cpython 3.11.15, pytest 9.1.1),
`PYTHONPATH=$PWD/src`, `TMPDIR` under `$MAESTRO_TASK_SCRATCH`, every run with
`-q -p no:cacheprovider --no-header`.

`build-baselines/kbutillib.json` exists, so per the developer role's rule no
new baseline file was written and the full repo suite was NOT re-run (its h100
measurement is 3,416 s, 5.7x the tool ceiling). The targeted baseline below was
measured instead, on the base commit, in this worktree, before any change.

**Baseline** — `pytest tests/domains/ai/council` at `4528fe9`, clean tree:

```
7 failed, 1841 passed, 7 skipped in 346.17s
```

The seven, by name:

- `test_types_config.py::TestPackagedDefaultConfig::` —
  `test_cli_endpoints_default_to_one_call_at_a_time`,
  `test_codex_seats_are_marked_unverified`,
  `test_default_profile_is_mixed_subscription`,
  `test_every_packaged_profile_builds_a_roster`,
  `test_on_a_fresh_machine_with_no_kbutillib_config`,
  `test_the_packaged_config_declares_no_poplar_endpoint`
  — the machine-local six the executor note names:
  `/home/chenry/.kbutillib/config.yaml` leaks past the monkeypatched `HOME`.
- `test_four_queue_integration.py::TestConcurrentProgressAndAccountSafety::test_fast_seat_progresses_while_the_shared_account_is_saturated[sqlite]`

**Branch** — the same command on the final tree (file checksums confirmed
unchanged between launching the run and reading it):

```
6 failed, 1936 passed, 7 skipped in 314.37s      exit 1
```

The six are the `test_types_config` set, unchanged. **The branch's failure set
is a strict subset of the baseline's: no test that passed on `4528fe9` fails
here.** The seventh baseline failure, the `test_four_queue_integration`
concurrency test, PASSED on the branch — a timing-sensitive test that is flaky
on this shared host, and a baseline failure that is absent on the branch is not
this task's concern.

The task's own suites, on the final tree:

```
pytest tests/domains/ai/council/test_go_tuning_score.py \
       tests/domains/ai/council/test_go_tuning_run.py \
       tests/domains/ai/council/test_go_tuning_script.py \
       tests/domains/ai/council/test_go_tuning_ledger.py
  → 133 passed in 38.90s        exit 0
```

Per file: `test_go_tuning_score.py` 65 (new), `test_go_tuning_run.py` 17 (new),
`test_go_tuning_script.py` 34 (20 at base, 14 added), `test_go_tuning_ledger.py`
17 (unchanged in count; one test's pinned tuple updated).

Lint. `ruff check` over everything this task touched passes clean. Across the
whole of `tests/domains/ai/council/` ruff reports 21 findings on the branch and
21 on the base tree (measured by stashing the change and re-running), so this
task introduces none.

`python scripts/council/go_tuning.py --help` and `<subcommand> --help` for all
ten subcommands exit 0.

One accounting discrepancy I did not resolve: `pytest --collect-only` reports
1,951 tests for `tests/domains/ai/council` while the run's summary accounts for
1,949 (6 + 1936 + 7). There are no `xfail` markers anywhere in that directory
and no errors or deselections in the run, so I do not have an explanation. It
is a two-item gap in a 1,951-test suite and it does not bear on the regression
verdict, which is made by NAME above and not by count — but it is stated rather
than papered over.

## what I inferred rather than verified

- **No live store, no live model, no token spent.** Every test uses synthetic
  sqlite stores built through the store's own write APIs, and the one test that
  runs a real council uses `FakeBackend` over a `fake` endpoint. The scorer has
  never been pointed at the production `go_mf@4` corpus, so its behaviour on
  the real shapes (29 job rows per entity, cancelled chunks) is inferred from
  `conflicts.match`'s handling of them rather than observed.
- **`fresh_run`'s default path builds `CouncilUtils(store_url=...)`**, which
  needs a resolvable council config and a seated roster. That path is NOT
  exercised: the library tests inject a factory and the CLI test records the
  call. A config error in the real path would surface on first operator use.
- **The network-filesystem refusal is inferred, not provoked.** The Dropbox
  component refusal IS tested (a `Dropbox/` path under `tmp_path`); the NFS
  branch is the store's own `refuse_network_filesystem` and `/scratch` on h100
  is xfs, so nothing here could produce it. The message `fresh_run` wraps it in
  is the same either way.
- **Latency is reported, never gated on,** and is read from `attempts.started_at`
  / `finished_at`. On the synthetic stores no attempts exist, so the tests
  assert the SHAPE (`{"attempts": 0, ...}`) rather than any figure.
- **`insufficient` is not a refusing band.** The prompt names `not_trusted`
  only, so a calibration with too few marks to band at all passes the gold
  check. That is what was asked for and is what the docstring says; if the
  intent was "anything that is not `trusted`/`conditional`", it is a one-line
  change to `_check_band`.
- **The incumbent chain in `stop_rule` reads `candidate.query`** to name the
  new incumbent after a replacement. On this task's fixtures every run is
  `go_mf@4`, so the chain is only exercised with the label unchanged; a real
  loop's `go_mf@N` candidate would be named from its own query type.
