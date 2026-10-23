# Work record: task-2b5e29ff — `kbu ontomap council-publish` + the pre-commit assertion set (stage 5)

## task_id
task-2b5e29ff

## prd
function-to-go-council-pipeline-v1, stage 5: the producer half of the
publish, the pre-commit join assertions, the arrival decision, and the load
receipt.

## branch
`maestro/developer/background-read-this-first-it-is-task-2b5e29ff`

## commit_shas
- `fae9fa4` — `feat(ontomap): publish a run, and make a wrong join key loud (stage 5)`
- (this work record is committed on top of it)

## base
`main` at `e106225` — the worktree's HEAD. No rebase was needed.

## summary

Four deliverables, and **no new pipeline infrastructure**. Every step is a
call into something already shipped: `resolve_manifest_path` for the preset,
`load_manifest` + `shard_plan` for the plan gate (the single plan-time
validator, which is what `kbu clearinghouse plan` resolves a manifest
through), `shard_manifest` for the bronze write, and `run.json` for the
completeness contract. **There is no second manifest format and no second
receipt schema.**

### 1. `kbu ontomap council-publish` — stage 5's producer half

`src/kbutillib/domains/genome/ontology_mapping/council_publish.py` (new, ~620
lines) plus the CLI verb in `src/kbutillib/interfaces/cli/ontomap.py:1160`,
which replaces the not-implemented stub. Re-exported from
`ontology_mapping_utils` beside the other stages, and it imports no numpy —
publishing ranks nothing, exactly as `council_run` does not.

`council_publish(run_dir, *, verdicts=None, out_dir=None, target_bytes=None)`:

1. **PLAN.json** — the run identity. Five fields are required and a missing
   one is refused BY NAME (`PublishRefused.check == "plan_fields"`): the query
   reference and the two digests ARE the partition key.
2. **The verdict parquet** — `--verdicts`, else `verdicts.parquet` in the
   scratch directory **PLAN.json's own absolute `items_file` names**. Not a
   re-resolution of `$KBU_SCRATCH_DIR`: the plan already recorded where it
   wrote, and re-deriving the root could answer differently from the stage
   that actually wrote there — which would read as "not exported yet".
3. **The render** → `<run-dir>/MANIFEST.toml`. Four markers, each anchored on
   a whole declaration LINE (the header comments mention the same values) and
   each required to appear EXACTLY ONCE, so an edited preset fails loudly here
   instead of publishing the template. `source` and `parameter_set.query` are
   both written from the same `plan["query"]`, because they are two spellings
   of one fact that `validate_council_parameter_set` cross-checks.
4. **The plan gate** — `load_manifest` + `shard_plan`, then three partition
   refusals: the target table must be exactly `function_result` (the functions
   were READ from `function_entity`/`function_content`, so a second entity is
   unrecoverable), the rendered hash must not be the documented inert
   `7bccf01b…`, and it must equal the hash `PLAN.json` recorded (publishing at
   any other hash leaves the whole plan still un-run and re-charges the next
   run for all of it).
5. **The shard run** → `<run-dir>/shard`. Runs anywhere; `run.json` carries a
   sha256, a row count, a byte count and a hash range per batch.
6. **Check the inventory back** — every batch must carry sha256/rows/hash
   range, and the PAYLOAD row count must be non-zero. The parameter-set
   registry batch is excluded from that count: it carries a row per declared
   parameter set, from the manifest rather than from the data, so counting
   batches rather than payload rows is exactly how an empty publish would slip
   through.
7. **`PUBLISH.json`** — the item accounting the receipt reconciles against,
   plus a declared `hand_off` naming the two in-pod verbs and where the
   receipt should go.

**Nothing that runs on kbhub was added.** `load` and `verify` already exist and
are already locus-gated; this task extended them, and `council_publish` is
proven never to construct `ClearinghouseCapability`
(`TestItRunsAnywhere::test_publishing_never_touches_the_berdl_capability`
makes construction fatal and the publish still succeeds).

### 2. The pre-commit assertion set

`clearinghouse_ingest.join_assertions()` — pure, four assertions, a COUNT for
each, **evaluated on the INPUTS**:

| assertion | count reported |
|---|---|
| non-empty source | `source_rows` |
| non-null join keys | `null_keys` |
| at most one source row per target slot | `ambiguous_slots` (over `SLOT_KEY_COLUMNS`) |
| coverage ≥ declared floor | `resolved_keys` / `source_keys`, `coverage`, `coverage_floor` |

Wired in `ClearinghouseCapability._join_assertions` →
`_ingest_one_batch` (after the five-check gate, before the retag, the
fill-only filter, the `started` line and any `load()`) **and** in
`_dry_run_reports`, because `--dry-run` is the operator's last gate and a gate
that passed exactly the load that fails would be worse than no gate.

"Resolve" = the key names a row in the table
`resolvable_source_table()` picks: `<type>_result` → `<type>_entity`. `None`
for entity/content/registry batches, which MINT identity and have nothing to
resolve against — recorded as `coverage_checked: False`, i.e. skipped rather
than silently passed. The read is one Spark query per result batch, restricted
to the batch's own `[hash_min, hash_max]` window exactly as `_existing_keys`
is.

`DEFAULT_COVERAGE_FLOOR = 1.0`, with `--coverage-floor` to lower it. **Zero
resolution of a non-empty batch is fatal whatever the floor says**, including
`--coverage-floor 0`: a floor expresses tolerance for a PARTIAL mismatch and
total non-resolution is a wrong join key. A refused batch is ledgered
`failed` with its assertion report and the ingest carries on (one producer
defect in one batch does not invalidate the batches that committed, and a
ledgered failure blocks `verify_run`); `kbu clearinghouse load` then exits
non-zero via the new `_exit_on_load_failure`.

**The trap, proven.**
`test_the_assertion_is_on_the_inputs_not_on_the_join_result` computes the
resolution join over a wrong-column batch (empty), shows that every
relationship claim over it is vacuously true, and then shows the same claims
on the inputs reporting 0 of 3 and failing.
`test_a_wrong_column_shard_run_is_refused_before_anything_is_committed` and
`test_kbu_clearinghouse_load_exits_nonzero_on_a_wrong_column_run` construct a
shard run by hashing the WRONG column and assert the non-zero exit;
`test_the_same_run_keyed_on_the_right_column_lands` is the control.

### 3. Arrival — `run.json` existing is not proof

`clearinghouse_ingest.transfer_state()` + `has_arrived` + `describe_transfer`
+ `raise_for_transfer`, and a new NON-TERMINAL ledger state `LEDGER_SYNCING`.

A batch that is absent, or whose bytes are not its published sha256, is
`syncing` while the clock allows and a `failed_delivery` after. The clock is
**`run.json`'s own mtime**, not the attempt's start: an operator re-running a
stuck load is the EXPECTED response to "still syncing", so a deadline measured
from the attempt would make the failed-delivery verdict unreachable in exactly
the case it exists for. `DEFAULT_TRANSFER_DEADLINE_SECONDS = 6 h`, with
`--transfer-deadline-hours`.

Progress is persisted: the `syncing` line carries `first_observed_at`, and the
next attempt reads it back out of the ledger
(`_ingest_inventory` passes the prior record down as `prior=`), so the retry
resumes the clock rather than restarting verification from nothing — and the
batches that already committed are skipped, not re-verified
(`test_a_retry_resumes_the_deadline_from_the_persisted_first_sighting`
asserts one `load()` call across two attempts).

The failed-delivery report names the batch, expected versus observed size and
expected versus observed digest. **Size needed a new field**: `run.json` had
no byte count, so `clearinghouse_shard._batch_entry` now records `bytes`
(additive; an older inventory reports `expected_bytes: null`, which is honest
rather than wrong) and `derive_execution_inventory` carries it through.

`transfer_state` hands its digest back in `observed_sha256` and
`verify_batch_file` gained an `observed_sha256=` parameter, so **a full-size
batch is still hashed exactly once per load** rather than twice. The check
itself is unchanged: a wrong pre-computed digest still refuses
(asserted in `test_an_arrived_batch_hands_back_the_digest_it_streamed`).

### 4. The receipt — `verify`'s own envelope, re-exportable

`clearinghouse_ingest.build_receipt()` / `read_publish_record()` /
`write_receipt()`, surfaced as `data.receipt` on every
`kbu clearinghouse verify` envelope, with `--receipt PATH` to file it as
`LOADED.json`. No new schema: it is verify's envelope, extended.

It reconciles against **items**, not rows: `items_submitted`,
`items_with_verdict`, `items_unresolved`, `result_rows_written`,
`declines_by_reason`, `snapshot_ids`, `parameter_set_hashes`, `reconciled`,
`notes`. One item can carry several GO terms, so the row count and the item
count are reported separately and neither stands in for the other; a
`PUBLISH.json` that does not add up, and a `result_rows_written` BELOW
`items_with_verdict`, each produce a note.

The item half can only come from the producer — an item whose every call
failed produced no row, so it is invisible to the shard run — so it is read
from `PUBLISH.json` beside the shard directory. **Beside and not inside:** the
shard directory's inventory is `run.json`. With no `PUBLISH.json` the item
fields are `null` with a note, never a row count in disguise.

`verify_run` grew two fields the receipt needs: `parameter_set_hashes` (from
the batch files it already reads for the unit registry check) and
`snapshot_ids` per table (from the ledger it already reads).

**Re-exportable, tested as a property.**
`test_the_receipt_is_re_exportable_and_reproduces_byte_for_byte` writes the
receipt, DELETES it (the crash between the Iceberg commit and the
acknowledgement), re-runs `verify_run`, and asserts the re-exported body is
byte-identical. The body deliberately carries no timestamp and no nonce —
the envelope has `generated_at` — because a receipt that changed on every
export could not be compared to the one that was lost.

Writing the file is OPT-IN because `verify` otherwise WRITES NOTHING, and that
guarantee is worth more than a default; `PUBLISH.json`'s `hand_off` declares
the path to pass.

## reconciled against the tree

The prompt was accurate about the tree in every structural claim. Four
divergences, all adaptations rather than contradictions:

1. **`roster_fingerprint` vs `bench_fingerprint` in the parameter set.** The
   prompt's MEASURED FACTS say the parameter set is
   `{query, roster_fingerprint, mapping_hash}`. The tree says
   `bench_fingerprint`, in three places that agree with each other:
   `clearinghouse_parameters.COUNCIL_PARAMETER_KEYS`,
   `ontology_mapping_utils.council_plan`, and the shipped preset's header
   (which records Chris's 2026-10-22 decision and the reason: the bench
   partitions the corpus, the transport does not, and
   `COUNCIL_REFUSED_KEYS` refuses `roster_fingerprint` as a key). **The tree
   wins**; the publish step renders `bench_fingerprint`.
2. **`run.json` had no byte count**, so "expected versus observed size" was
   not reportable. Added as `bytes` on the sharder's batch entry — the one
   genuinely new field in this change, and additive.
3. **"A load that joins nothing SUCCEEDS … reported as a clean idempotent
   no-op."** With fill-only's anti-join semantics a *wrong key* actually means
   nothing matches, so every row SURVIVES and is written — the no-op reading
   describes the resolution join, not the fill-only one. Both readings make
   the same defect fatal, and the assertion set is written against what the
   code does: coverage is measured against the target's identity table,
   independently of either join.
4. **`verify`'s `--unit`** existed on `verify_run` but was not exposed on the
   CLI; exposed while extending the command, since the receipt is per-unit
   aware (`unit` rides in the body).

## verification

Baseline and branch were run over the SAME selection with the interpreter and
`TMPDIR` the h100 baseline file names, and compared **by node ID**. The full
suite was deliberately not run: `build-baselines/kbutillib.json` and
`build-baselines/kbutillib-h100-main-6310d61.json` both already exist, and the
latter records 3,416 s for it.

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src \
  /home/chenry/venvs/kbdl/bin/python -m pytest \
  tests/berdl/ tests/domains/ tests/cli/ tests/interfaces/ \
  -q -p no:cacheprovider --no-header
```

| | failed | passed | skipped |
|---|---|---|---|
| base `e106225` (separate worktree at `/scratch/chenry/base-2b5e29ff`) | 30 | 3899 | 12 |
| this branch | 30 | 3957 | 12 |

Exit code 1 both sides, from the same 30 pre-existing node IDs. `comm` over
the two sorted node-ID lists reports **no node failing on the branch that
passed on the base, and none fixed**. The 30 are the documented
machine-local/pre-existing set: `tests/cli/test_kind.py` ×17 and
`test_kind_install_containment.py` ×1 (no KING checkout on h100),
`test_model_recording.py::TestVerbStamping` ×2 (`CliRunner(mix_stderr=False)`,
removed in click ≥ 8.2), `test_types_config.py::TestPackagedDefaultConfig` ×6
(the h100 user config leaks past the monkeypatched `HOME` — named as
machine-local in `kbutillib-h100-main-6310d61.json`),
`test_static_headers.py::TestPackagedExample::test_the_default_config_declares_no_mag_endpoint`
(KBUtilLib issue 83, named as pre-existing in the task prompt), and
`test_mcp_server.py::TestUnavailableHandler` ×3.

The two files this task's tests live in, run on their own:

```
tests/berdl/test_clearinghouse_ingest.py tests/domains/test_ontomap_council_publish.py
tests/cli/test_ontomap_cli.py tests/cli/test_clearinghouse_operator.py
-> 197 passed in 5.58s                                           (exit 0)
```

### tests that were CHANGED, and why each is a strengthening

- `tests/cli/test_ontomap_cli.py` — `council-publish` moved from `STUB_VERBS`
  to `IMPLEMENTED_VERBS` and gained a `TestCouncilPublishIsImplemented` class
  mirroring the existing `TestCouncilQuoteIsImplemented` /
  `TestCouncilPlanIsImplemented`. The three failing assertions were asserting
  the verb is a stub, which is what this task removed.
- `tests/berdl/test_clearinghouse_ingest.py` — three result-batch fixtures now
  seed `gene_entity` through the new `_preloaded_entities()` helper. Before the
  coverage assertion existed they loaded result rows into a target with NO
  entities at all, which is not a state a real load is ever in (47,341 entity
  and content rows are loaded first, and stage 1 reads the universe out of
  them). `test_a_re_run_ingests_zero_registry_rows` gained an assertion that
  the result batch actually landed — without the seed it would have been
  asserting registry counts over a batch that never ingested.
- `test_a_sha256_mismatch_refuses_that_batch_ledgers_it_and_carries_on` was
  replaced by four tests covering both sides of the arrival decision; every
  original assertion survives in
  `test_a_sha256_mismatch_past_the_deadline_is_a_failed_delivery`.
- `_write_batch` now records `bytes`, mirroring `publish_unit`.
- `tests/cli/test_clearinghouse_operator.py` — the fake's `ingest_shards` and
  `verify_run` accept and RECORD the new keyword arguments. Recording rather
  than merely tolerating them: the CLI's whole job for the two declared
  thresholds is to hand the operator's declaration down, so a signature that
  swallowed them would let the option become a no-op silently.

No test was weakened, skipped or deleted.

## inferred rather than verified

- **The real 19,276-item run.** Nothing was published to the lake and no pod
  was touched. Every number in the PRD's MEASURED FACTS is carried through as
  a reason, not re-measured. The largest shard run exercised here is the
  six-function fixture.
- **`DEFAULT_COVERAGE_FLOOR = 1.0` against the existing presets.** It is the
  floor the schema's own orphan logic implies (a result row whose entity
  nothing can find is an orphan in the same sense a parameter-set orphan is,
  which the repo already treats as a hard failure). It is **not** verified
  against a live `arc-kbdl` or `mongo-protein-bakta` load; if one of those
  carries a stray result row today, it will now refuse that batch rather than
  land it. `--coverage-floor` is the escape hatch and the report says the
  measured coverage either way.
- **`DEFAULT_TRANSFER_DEADLINE_SECONDS = 6 h`** is a judgement scaled from the
  PRD's statement that the previous hand-off's directory is ~19k rows of
  result parquet. No cross-machine sync of that directory was timed here.
- **The entity batch landing before the result batch within a unit.** The
  coverage assertion assumes a source declaring
  `kinds = ["entity", "content", "result"]` publishes them in that order, so
  the entities exist by the time the result batch is asserted. That is the
  order `run.json` records for the shipped presets and the order
  `derive_execution_inventory` preserves, but it is read off the manifest's
  `kinds` list rather than enforced. `function-to-go-council` declares
  `kinds = ["result"]` only, so this does not affect the pipeline this task
  serves.
- **A plain integrity failure still exits ZERO.** A batch refused for a bad
  row count or a stray `ingest_batch_id` is ledgered `failed` and reported,
  and `verify` blocks on it, but `kbu clearinghouse load`'s exit status is
  unchanged for that case — the new non-zero exit is scoped to the two classes
  this task added (join-assertion refusals and unarrived batches), so no
  existing exit contract moved. Widening it is a reasonable follow-up and was
  deliberately left out of scope.
- **The consumer's half was not run.** `load` and `verify` are in-pod and this
  is h100; both were exercised against `_FakeLake`, the in-memory stand-in
  that stores what it is handed. Nothing was deployed and no pod was reached.
