# Work record: task-aa22e94b — `kbu ontomap council-status`, the operator runbook, and the end-to-end rehearsal

## task_id
task-aa22e94b

## prd
function-to-go-council-pipeline-v1 — the status verb, the operator runbook,
and the rehearsal that is this PRD's acceptance.

## branch
`maestro/developer/background-read-this-first-it-is-task-aa22e94b`

## base
`main` at `24478c0` ("docs(work-record): task-2b5e29ff ontomap council-publish
(stage 5)") — the worktree's HEAD. No rebase was needed.

## summary

Three deliverables, and **no new pipeline infrastructure**. The status verb is
two clearinghouse reads around pure arithmetic; the rehearsal drives the
shipped stages and the shipped consumer; the runbook writes down the
constants that already travel with the plan.

### 1. `kbu ontomap council-status`

`src/kbutillib/domains/genome/ontology_mapping/council_status.py` (new, 878
lines) plus the CLI verb at `src/kbutillib/interfaces/cli/ontomap.py:1433`
(builder at `:1368`, renderer branch at `:346`), which **replaces the last
not-implemented stub**. `_PLANNED_VERBS` is now `()`
(`src/kbutillib/interfaces/cli/ontomap.py:1520`) — kept, with its stub
machinery, rather than deleted, so the next verb designed ahead of its
implementation has somewhere to go. Re-exported from `ontology_mapping_utils`
beside `council_run` and `council_publish`, and it imports no numpy: status
ranks nothing.

**Coverage is reported twice, because a corpus under a versioned transform is
always partly stale and the two ways it is stale are unrelated.**

**BY LIFECYCLE** (`lifecycle_coverage`, `council_status.py:348`) — `un-run`,
`planned-not-quoted`, `quoted-not-approved`, `approved-not-run`, `running`,
`exported-not-loaded`, `loaded`, and they are a **PARTITION**: they add up to
the corpus. `paid_for_not_landed` (= `running` + `exported-not-loaded`) is the
number the verb exists for — tokens spent, nothing in the clearinghouse,
invisible in both a "what is un-run" count (the plan took it out of the un-run
set) and a "what is loaded" count.

**BY `parameter_set_hash`** (`parameter_set_coverage`, `:440`) — one row per
hash, the current one first, plus
`covered_only_by_an_older_parameter_set`, `covered_at_all` and `uncovered`.
Partial-version coverage is **reported, not normalised away**: re-running
47,341 functions on every bump is unaffordable, so the split is the only thing
that makes "is this dataset consistent?" answerable. `parameter_set_hash` is
part of the result slot key, so the split is a group-by over ONE
`current_state` read rather than a read per hash.

**A second concurrent run** (`concurrent_runs`, `:328`) — runs with `RUN.json`
present and `complete` false. Returns the rows, not a boolean, because the
remedy needs a path (`touch <dir>/STOP`). The warning names KBUtilLib issue 84
and the 10.8x throughput collapse.

Three design decisions worth stating:

- **The seven states are made a partition by folding "ran, not published" into
  `running`.** Taken literally the PRD's seven leave a hole — a run whose
  chunks and retry pass have settled but whose verdicts are not published is in
  none of them — and a lifecycle report whose buckets do not add up is not a
  report. `running` means **`RUN.json` exists and `PUBLISH.json` does not**.
  The per-run rows carry `complete`, `halt` and `published`, so an operator
  still sees which of the three it is; what is not available is a bucket that
  quietly holds nothing.
- **The state is read off the FILES**, never off a status field. Each stage
  writes exactly one artifact, so the lifecycle is a function of which of the
  six exist; the names are imported from the writing stages
  (`stage_artifacts()`, `:150`) rather than restated. A `STATUS.json` would be
  a seventh file that could disagree with the six.
- **`loaded` comes from the clearinghouse and not from the receipts.** That is
  what catches the crash-after-commit case: the Iceberg commit and the
  `LOADED.json` write have no transaction between them, so a published run
  with no receipt whose rows are already in the table gets told to RE-EXPORT
  the receipt (`verify --receipt`), not to reload (`_receipt_note`, `:773`).

Degradation is explicit throughout: an unresolvable parameter set, a failed
`stats()` read and a failed `current_state()` read each leave the affected
figure `None` **and never 0**, with a warning, while everything still
computable is still reported. The corpus comes from `stats()` `COUNT(*)` and
not from the 47,341-row `universe()` join — status is run repeatedly while a
9-hour run drains — and when `function_entity` and `function_content` disagree
the smaller is used (the universe is their inner join) with a warning saying
so.

### 2. The operator runbook

`agent-io/docs/function-to-go-council-operator-runbook.md` (new, 550 lines),
following the convention of `agent-io/docs/clearinghouse-schema-operator-runbook.md`.
It carries, as the task required:

- the five stages with their **exact commands**, plus the locus of each;
- the measured concurrency and chunk constants **with their rationale** — MAG
  queues summing to 6 against an account-wide 120 req/min AND 20-parallel cap
  (24 parallel errored 427 of 500 pilot items over 1,925 transport attempts);
  ARGO chair 24 / critic 8 / meta 8 (saturates ~24, zero rate limiting at 40);
  ~2,000-item chunks with ONE in flight;
- the **KBUtilLib issue 84** reference that makes the chunking removable, with
  the three places the number and its reason live, so the day the issue is
  fixed all three change;
- the **`systemd-run --user --scope --collect`** requirement, and why `setsid`
  / `nohup` / `disown` do not work on this host;
- **what Albert runs on kbhub** — `load --dry-run`, then `load`, then
  `verify --receipt` — with the trigger condition stated as **"a `run.json`
  exists AND every batch it lists is present and sha256-matches"**, never as
  "a `run.json` exists", including the few lines of shell that check it;
- the **superseded scratch scripts**, in a table giving what replaces each and
  why the script is worse, with the `rm` to remove them.

It also records the credential trap (`ARGO_USER` and the AMSC token are
exported in no shell and fail silently by parking the calls), why
`bench_fingerprint` and not `roster_fingerprint` partitions the corpus, and a
symptom-to-cause table.

### 3. The rehearsal

`tests/domains/test_ontomap_pipeline_rehearsal.py` (new, 1,795 lines, 55
tests). A **test module, not a shell script**, so it runs in CI rather than
when somebody remembers — which is exactly how the `/scratch/chenry/council/`
scripts became a second divergent path.

**IT SPENDS ZERO TOKENS, AND THAT IS TESTED RATHER THAN ASSERTED.** The whole
happy path runs with `socket.socket` and `socket.create_connection` POISONED,
so it can only pass if no connection is attempted at all — deliberately not "a
stubbed HTTP client that went uncalled", because a seam every test replaces is
a seam no test covers. No live council pass happens anywhere.

**How much of it is real.** The council store is a real SQLite store with the
real schema; the query type is the **shipped `go_mf@1`**, seeded through the
real `QueryRegistry.seed_builtins()`; the configuration is a real
`CouncilConfig` carrying the measured concurrency; and stages 1, 2, 3, 4's
gates, 5 and the consumer's load/verify all run their production code. Exactly
three collaborators are stand-ins:

- the **clearinghouse read** (`_FakeClearinghouse`) — the real one needs the
  BERDL delta API and a populated lake;
- **retrieval** (`function_to_go` on `_Stage`) — scikit-learn plus the
  vendored GO artifacts, and no pipeline logic depends on which GO terms come
  back;
- the **lakehouse write boundary** (`_FakeLake`, **imported** from
  `tests/berdl/test_clearinghouse_ingest.py`, not copied) — the real one is a
  Spark session that exists only inside the pod, and `load`/`verify` refuse
  off-pod by design. `_FakeLake` stores what it is handed, which is what makes
  "the second load writes nothing" earned rather than asserted.

The **already-stored verdicts** are written straight through the store's own
`create_job` + `settle_item` — no worker, no backend, no call. That is what
makes the whole of stage 5 free, and it mirrors the production fact the PRD
rests on: the store already holds a completed 19,268-verdict run.

The happy path covers plan → quote → approve → export → publish →
`load --dry-run` → the real load → `verify` → the receipt, plus
`council-status` across the rehearsed run.

## which of the ten failure cases were handled as designed, and which needed a fix

**All ten were handled as designed. NONE needed a product fix.**

| # | Case | Verdict | Where it is handled |
|---|---|---|---|
| 1 | marker before its payload | as designed | `transfer_state` → `syncing` inside the deadline, `failed_delivery` past it, measured from `run.json`'s own mtime; and when even the registry has not arrived, `_assert_registry_committed_through` refuses outright |
| 2 | one listed file missing | as designed | same arrival check, per batch, naming the batch id and the file; the load cannot exit zero |
| 3 | a batch whose sha256 does not match | as designed | `TRANSFER_DIGEST_MISMATCH` with expected-vs-observed digest and size as evidence; nothing committed. A corrupt REGISTRY batch refuses the result batch outright |
| 4 | a 0% join | as designed | `join_assertions` "WRONG JOIN KEY", and `--coverage-floor 0` does **not** waive it |
| 5 | a partial join below the floor | as designed | refused at the default floor 1.0; admitted at a DECLARED lower floor, with the measured coverage on record either way |
| 6 | a duplicate consumer loading the run twice | as designed | a new execution id, its own ledger, zero survivors, `load()` never called a second time; a concurrent duplicate is refused by `NamespaceLock` naming the holder |
| 7 | a crash after the commit, before the receipt | as designed | the receipt is re-exportable and byte-identical; re-exporting appends nothing; `council-status` detects the shape and says RE-EXPORT, not reload |
| 8 | an expired approval | as designed | `ApprovalRefused(check="expires_at")`; stage 4 refuses before a token and before a budget epoch is opened |
| 9 | a plan changed after approval | as designed | `ApprovalRefused(check="plan_hash")` on a re-plan and on an edited `PLAN.json`; an edited quote is the neighbouring `quote_hash` refusal |
| 10 | a sustained rate-limit response | as designed | `_note_rate_limit` halts at 5 responses in 60 s per ACCOUNT; a run whose gateway carries that halt submits **nothing** — the pre-flight supervisor pass finds it before chunk 1 — and the halt's own reason survives into `RUN.json` |

**A rehearsal that found nothing is a rehearsal to be suspicious of**, so here
is what it DID find. Three places where the system's real behaviour is not the
obvious one, each of which had the rehearsal written wrongly against it first.
All three are correct as built; what changed was the test.

1. **A refused or unarrived batch is REPORTED, not raised.** `ingest_shards`
   returns a per-batch report with `action` `refused`/`syncing`/`failed`; the
   non-zero exit lives in the CLI (`clearinghouse._load_failures` +
   `_exit_on_load_failure`). Deliberate — one bad batch must not unwind the
   batches that already committed, and the ledger has to KEEP the failure so
   `verify_run` blocks on it. The rehearsal now asserts **through the CLI's own
   classifier**, so it cannot pass while `kbu clearinghouse load` exits zero.
2. **The registry-first rule outranks the arrival check.** When `run.json`
   arrives ahead of everything, the load does not patiently report every batch
   as syncing: the parameter-set registry batch has not committed, so the first
   result batch refuses outright with `ClearinghouseRegistryOrderError`.
   Louder, and right — a result row whose `parameter_set_hash` the registry
   cannot explain is an orphan. Failure 1 therefore has two tests: nothing
   arriving at all, and the registry arrived with the payload missing.
3. **Stage 3 will not WRITE an already-expired approval** (`ValueError`,
   "authorises nothing"). So the expired case can only arise through the
   passage of time, and it is exercised with the gate's own `now=` clock rather
   than by constructing an artifact the product refuses to produce. That
   refusal is itself now a test.

One small **product** change did come out of the status tests, and it is not
one of the ten: `run_lifecycle` reported `complete: False` for a run whose
`RUN.json` is present but unparseable, because `bool({}.get("complete"))` is
`False`. `False` is the definite claim "this run did not finish" about a run
whose record merely would not parse, so it now reads `None` (unknown) — as
does `halt` — and the unreadable artifact is listed in `unreadable`
(`council_status.py:283`). `_read_artifact` (`:182`) returns three outcomes and
not two for the same reason: "parsed to an empty dict" and "would not parse"
are different things, and conflating them flagged a legitimately empty
artifact as corrupt.

## tests

`tests/domains/test_ontomap_council_status.py` (new, 48 tests) — the pure
arithmetic and the verb's degradation, all without numpy, pyarrow, sqlalchemy
or a clearinghouse. Pins that the seven buckets partition the corpus, that
`paid_for_not_landed` is reported, that partial-version coverage is reported
rather than summed, that an unknown answer is `None` and never 0, and that the
module imports no retrieval stack at module level (checked by parsing its own
AST, not by trusting a comment).

`tests/cli/test_ontomap_cli.py` — `STUB_VERBS` is now `[]` and
`council-status` has joined `IMPLEMENTED_VERBS`; `TestCouncilStatus` (11
tests) covers registration, the usage error, the help's content (every
lifecycle state, `parameter_set_hash`, the issue-84 rationale, the `STOP`
remedy), both output paths against the same record, the
no-run-directories warning and the clean refusal. The existing
`test_every_registered_verb_is_either_implemented_or_a_known_stub` still
pins the registered set exactly.

**No test was weakened, skipped or deleted.** `TestStubsExitNonZero` is
parametrised over `STUB_VERBS` and so now collects zero cases — it is retained
because the next stubbed verb re-arms it.

## verification

The KBUtilLib suite runs **2,665–3,416 s** (measured, `build-baselines/kbutillib-h100-main-6310d61.json`),
which is 4.4–5.7x the 600 s sub-agent ceiling, so a TARGETED selection was run
as that baseline's `WHY_DURATION_MATTERS` field instructs. All commands were
run from the worktree with `TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src`
and `/home/chenry/venvs/kbdl/bin/python`.

```
python -m pytest tests/domains/test_ontomap_council_plan.py \
  tests/domains/test_ontomap_council_export.py \
  tests/domains/test_ontomap_council_publish.py \
  tests/domains/test_ontomap_council_run.py \
  tests/domains/test_ontomap_council_status.py \
  tests/domains/test_ontomap_pipeline_rehearsal.py \
  tests/domains/test_ontology_mapping.py \
  tests/cli/test_ontomap_cli.py -q -p no:cacheprovider --no-header
→ 349 passed, 3 skipped          exit 0
```

```
python -m pytest tests/berdl/test_clearinghouse_ingest.py \
  tests/berdl/test_clearinghouse_manifest_and_shard.py \
  tests/berdl/test_clearinghouse_function_to_go_preset.py \
  tests/cli/test_clearinghouse.py tests/cli/test_clearinghouse_operator.py \
  tests/domains/ai/council/test_approval.py \
  tests/domains/ai/council/test_quote_plan.py \
  tests/cli/test_council_cli.py -q -p no:cacheprovider --no-header
→ 395 passed                     exit 0
```

(The second selection is the neighbours this work reaches into: the ingest
module the rehearsal imports `_FakeLake` from, the manifest/shard chain, the
preset, the two clearinghouse CLIs whose `_load_failures` the rehearsal
borrows, and the approval and quote stages the rehearsal drives.)

The three skips are pre-existing in `tests/cli/test_ontomap_cli.py` (they
`importorskip` the retrieval backend's numpy/sklearn/joblib).

### The regression comparison, measured BOTH SIDES in a worktree

`build-baselines/kbutillib-h100-main-6310d61.json` records no `known_failing`
nodeid list (only a `failures_by_area` tally and a path to a file on
`/scratch` that is not in this tree), and
`build-baselines/kbutillib.json` is from 2026-09-13 at a different commit
under a different command in a minimal venv. **Neither gives a comparable
nodeid set**, so the base state was measured directly — and in a WORKTREE on
both sides, because that baseline's own `WORKTREE_MEASUREMENT_TRAP` field
records that comparing a worktree run against a main-clone run yields phantom
regressions:

```
git worktree add /scratch/chenry/base-aa22e94b 24478c0 --detach

# both sides, same command, same interpreter, same TMPDIR:
python -m pytest tests/cli tests/domains -q -p no:cacheprovider --no-header --tb=no
  base   (24478c0):  27 failed, 2750 passed, 2 skipped   in 256 s   exit 0
  branch           :  27 failed, 2857 passed, 5 skipped   in 259 s   exit 0
```

**ZERO REGRESSIONS. The failing set is node-ID-IDENTICAL, 27 → 27**: `comm`
over the two sorted nodeid lists reports no new failure and no accidentally
fixed one. The 27 are pre-existing and machine-local — 18 in
`tests/cli/test_kind.py` plus `test_kind_install_containment.py` (KIND local
state), 2 in `tests/cli/test_model_recording.py`, 6 in
`test_types_config.py::TestPackagedDefaultConfig` (the h100 user config leaks
in despite a monkeypatched `HOME`; recorded as machine-local in the h100
baseline) and `test_static_headers.py::TestPackagedExample::test_the_default_config_declares_no_mag_endpoint`
(KBUtilLib issue 83). None is touched by this work.

The collected set was diffed too, so the `+107 passed` is fully accounted for
rather than inferred — **2,779 → 2,891 nodeids, with exactly three removed**:

```
--- removed (3)
tests/cli/test_ontomap_cli.py::TestStubsExitNonZero::test_stub_exits_non_zero_and_says_not_implemented[council-status]
tests/cli/test_ontomap_cli.py::TestStubsExitNonZero::test_stub_exits_non_zero_under_json_too[council-status]
tests/cli/test_ontomap_cli.py::TestStubsExitNonZero::test_stub_never_emits_a_success_envelope[council-status]
--- added (115)
  12  tests/cli/test_ontomap_cli.py
  48  tests/domains/test_ontomap_council_status.py
  55  tests/domains/test_ontomap_pipeline_rehearsal.py
```

The three that disappeared are the `council-status` parametrisations of
`TestStubsExitNonZero`, and each disappeared because the verb it asserted is
"not implemented yet" **is now implemented** — which is the deliverable. No
other nodeid left the suite.

**Lint was NOT run**: `ruff` is not installed in `~/venvs/kbdl` and is not on
this host's `PATH`, so the repo's own `nox -s lint` could not be executed.
Unused imports were checked by parsing each new file's AST instead (one was
found and removed: an unused `import pyarrow as pa` in the rehearsal), but
style, line length and the rest of the configured rule set are unverified.

## reconciled against the tree

The task prompt is a claim about the code; the code is the fact. Five
divergences, all resolved in favour of the tree, all adapted to without
stopping:

1. **The parameter set.** The prompt says the parameter set is
   `{query, roster_fingerprint, mapping_hash}`. The tree uses
   **`bench_fingerprint`**, not `roster_fingerprint`
   (`ontology_mapping_utils.council_plan`, and the preset's own
   `parameter_set` line), because the bench partitions the corpus and the
   transport does not — re-routing one seat would otherwise re-partition all
   47,341 functions and re-charge ~199M tokens for a pure cost optimisation.
   `roster_fingerprint` is recorded as provenance. **Everything built here
   uses `bench_fingerprint`**, and the runbook explains why.
2. **The seven lifecycle states are not a partition as listed.** "Ran, but
   not published yet" is in none of them. Folded into `running`, with the
   distinction kept on the per-run rows; documented in the module docstring
   and in the runbook.
3. **The task body lists nine failure cases; the success criteria enumerate
   ten** (the body's "a 0% join, and a partial join below the coverage floor"
   is one bullet covering two cases). **All ten are exercised**, each in its
   own class, and the table above reports all ten.
4. **`council-status` was described as "report progress, spend and blockers
   for a running plan"** by the stub it replaces. What the task actually
   specifies — and what landed — is a report ACROSS runs about CORPUS
   coverage. The verb takes a runs **root**, not a run directory, and the
   stub's one-line summary is gone with the stub.
5. **The ARGO account cap.** The prompt gives ARGO chair 24 / critic 8 /
   meta 8, which sum to 40 — and `CouncilConfig`'s own validator REFUSES an
   account whose queues over-subscribe its `max_concurrency`. So the
   rehearsal's config declares ARGO `max_concurrency: 40`, which is the same
   measured fact from the other side ("zero rate limiting at 40 concurrent
   across three models"), with 24 as the chair's share. The prompt is not
   wrong; it is incomplete, and the config would not load as literally
   stated.

## inferred rather than verified

- **No live council pass, no token spent, no pod touched, nothing deployed.**
  Stage 4 against real gateways remains a first: the one completed
  19,276-item run was hand-driven and bypassed these verbs. Every number in
  the PRD's measured facts is carried through as a reason, not re-measured.
- **The clearinghouse reads were not made.** `stats()` and `current_state()`
  are called against `_FakeClearinghouse` throughout. The claim that they
  work from h100 off-pod over the BERDL delta API is the PRD's measurement of
  2026-10-22, inherited from `council_plan`, which already reads the universe
  the same way.
- **The consumer's half ran against `_FakeLake`.** `load` and `verify` are
  in-pod and this is h100. What is proven is the arrival check, the four
  pre-commit assertions, the fill-only anti-join as the pure function states
  it, the ledger, the lock and the receipt — all production code — against an
  in-memory lake that stores what it is handed. Not proven: Iceberg, Spark,
  the real tenant or the real catalog.
- **The corpus read is `stats()` `COUNT(*)`, not the exact `universe()`
  join.** When `function_entity` and `function_content` disagree, the smaller
  is reported with a warning and the shares are taken against it. The
  clearinghouse currently holds 47,341 of each, so the two agree today; the
  degradation path is tested but has never been exercised against a real
  content gap.
- **`run.json`'s mtime as the hand-off date.** The arrival deadline rests on
  Dropbox preserving a useful mtime on the synced copy. That is the ingest
  module's existing assumption, not something this task measured, and no
  cross-machine sync of a shard directory was timed here.
- **The scratch scripts were NOT removed.** `/scratch/chenry/council/` is
  outside the worktree and removing files there is not a developer-role
  action. The runbook records all five as superseded, says what replaces each
  and why the script is worse, and carries the `rm` for an operator to run
  after the stages have been exercised once for real.
- **`_STATUS_RECORD` in the CLI test** is shaped like a real status record
  rather than captured from one. The real shape is pinned by
  `test_ontomap_council_status.py` against the live function; the CLI test's
  job is only the renderer and the envelope.
