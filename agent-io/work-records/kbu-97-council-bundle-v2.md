# Work record: kbu-97-council-bundle-v2 — publication bundle v2

## task_id
kbu-97-council-bundle-v2 (dispatched by Jane, session 71d48f60, 2026-10-26)

## issue
cshenry/KBUtilLib#97, as DECIDED in PRD `council-record-lakehouse-v1` Q21
(Chris agreed 2026-10-26). Commit messages say **Refs**, not Fixes: the issue
closes only after the first real publish. Part D also carries #98's decision
(PRD Q20).

## branch
`maestro/developer/task-kbu-97-council-bundle-v2-kb-kbu-97-council-bundle-v2`

## base
`0bb8a88` — the worktree's HEAD, `main` as of 2026-10-26 15:25Z.

`main` MOVED under me during the build, as the task header warned it would:
the concurrent branch (annotator version typing + `KBDLServiceUtils` batch
status) landed, so `git diff main..HEAD` now shows its files as deletions.
That is an artifact of comparing across a moved base, not a change of mine.
`git diff 0bb8a88..HEAD --stat` is the real diff and it touches **7 files,
none of them shared** with that branch.

## commit_shas
- `6250b90` — `feat(council): bundle v2 carries abstention text and its plan's origin`
- `a34bb46` — `test(council): the v2 bundle's texts, its plan_origin and the three refusals`
- `e8e34ed` — `feat(council): the t11 rehearsal builds a v2 reconstructed bundle`
- `4d5c3f4` — `fix(council): step 0's run-directory search survives a moving /scratch`
- `07e9927` — `fix(council): assembled_on is 2026-10-23, the date the plan was assembled`
- plus the v2 rehearsal report and this work record.

The last two were not foreseen by the task. Both are defects the rehearsal run
exposed; see the rehearsal section.

## summary

`BUNDLE_SCHEMA_VERSION` 1 → 2. Both additions exist because of the same
property of the clearinghouse: its slot key `(entity_hash, entity_type,
result_type, source, parameter_set_hash)` deliberately excludes
`result_type_version`, so **a payload is frozen on first write** and a
corrected row in the same slot is silently dropped as already-present. Two
things the publisher could previously get wrong were therefore not recoverable
errors but permanent ones.

---

## A. Abstention text rides in the bundle; a blank one refuses

`src/kbutillib/domains/genome/ontology_mapping/council_bundle.py:420` —
`build_bundle`'s abstain loop copies each plan abstain row's `text` into the
abstention it builds. `src/kbutillib/domains/kbase/berdl/council_publish.py:852`
— `lakehouse_export_rows` reads it from there.

It is the only durable source. A retrieval abstention is declined *before*
anything is submitted, so no council item exists, no `items.evidence` row
holds its text, and nothing in the archive can reconstruct it. Previously the
publisher took it from a `texts=` kwarg and, failing that, published `''` and
**counted** it in the receipt's `abstentions_without_text`. For this campaign
that is 760 rows.

Now:
- A blank (or whitespace-only) text raises `AbstentionTextMissingError`
  (`council_publish.py:188`), **naming every blank `query_id`**, not just the
  first, and not as a count.
- A **v1 bundle that carries abstentions** raises `StaleBundleSchemaError`
  (`council_publish.py:158`) with an instruction to rebuild — the run
  directory's abstain record still holds every text, so the rebuild recovers
  them and nothing in the publisher can. A v1 bundle with *no* abstentions is
  not refused: there is nothing it could be missing.
- The schema check runs **before** the blank-text check, because "fix it in
  the plan's abstain record" is the wrong instruction for a v1 bundle.

Both live in `require_publishable_bundle` (`council_publish.py:604`), called
first by `publish_from_lakehouse` (step 0, before `select_verdicts`) and first
by `lakehouse_export_rows`. Pure, idempotent, needs nothing but the bundle.

### The `texts=` kwarg: KEPT, narrowed to fanout-only

The spec left this to me. **I kept it, as an override for fanout rows only.**

- Keeping it preserves a real capability: a caller holding the run's ranked
  file can supply a better text for a *decided* row whose `items.evidence` is
  empty. Removing it would lose that and buy nothing.
- Narrowing it is the part that matters. The abstention path now has exactly
  **one** source — the bundle — and a caller who forgets a kwarg is precisely
  how 760 permanently-empty cells would have been published. That failure mode
  is now unreachable rather than merely documented.
- Removing it outright would also have broken `publish_from_lakehouse`'s
  signature and the CLI for no gain.

`tests/berdl/test_council_publish_lakehouse.py` pins the narrowing: passing
`texts={ABSTAIN: "something else entirely"}` does **not** override the
bundle's text.

---

## B. The 2026-10-19 PLAN.json is accepted and recorded, not regenerated

The bundle gains `plan_origin = {kind, assembled_on, reason, generated_at,
generated_at_source}` (`council_bundle.py:152`, `PLAN_ORIGIN_FIELDS`). `kind`
is `stage1` or `reconstructed` and nothing else.

`build_bundle_from_run` takes the reconstruction fields as **explicit keyword
arguments** (`plan_origin_kind`, `assembled_on`, `plan_origin_reason`,
`generated_at`, `generated_at_source`) and infers none of them. A run
directory is the wrong place to guess from: a hand-assembled `PLAN.json` is
indistinguishable from a stage-1 one on disk, so any heuristic — does it carry
`run_id`? is `plan_hash` blank? — would silently classify a reconstruction as
stage 1. The only thing ever derived is a `stage1` plan's own `generated_at`,
read straight off `PLAN.json`, because for a stage-1 plan that field *is* the
moment retrieval ran.

`build_bundle` refuses an unknown `kind`, an unknown *field* (a misspelled
`generated_at` would otherwise be dropped silently), and a `reconstructed`
origin missing any of its four.

**The publisher refuses `plan_hash == ''` unless `kind == 'reconstructed'`**
(`UnreconstructedPlanError`, `council_publish.py:172`). A blank hash is
legitimate for a campaign older than stage 1 — but it must be *declared*,
because the publisher re-writes the bundle's plan beside its output as the
campaign's `PLAN.json`.

**`export_rows` takes `abstained_at` from `plan_origin.generated_at`**, not
from `plan['generated_at']` (which a reconstructed plan does not have).

### Why the date matters, and where I closed the hole

`verdicts.export_rows:286` reads `declined_at = _iso(abstained_at) or
_now_iso()`. The preset maps `decided_at` → `observed_at`; the slot freezes on
first write. So a publish that could not say when retrieval declined would
make **its own publish date the permanent observation time of all 760
abstentions**.

I closed this at the *bundle*, not in `verdicts.py`: `build_bundle` refuses a
blank `generated_at` **whenever the campaign has abstentions**, so the
publisher can never reach the fallback. A campaign with no abstentions may
have a blank one — there is nothing to mis-stamp.

**`verdicts.py` is untouched.** Its `now()` fallback is still correct for an
export with no abstentions, and the task says this work "removes the need for"
the fallback, not the fallback itself. Removing it would have widened the
change into the run-directory export path for no benefit.

I also added `plan_origin` to the receipt (`council_publish.py:1012`). For a
reconstruction `generated_at` is a *derived claim about a past run* rather
than something a tool stamped, and it freezes permanently — so the receipt
carries both the claim and its stated source, and an operator can audit them.

---

## C. The `--coverage-floor` help text

`src/kbutillib/interfaces/cli/ontomap.py:1760`. Was "8 of **19,276 inputs**
unresolved"; now "8 of **20,036 submitted query_ids (19,276 fanout + 760
abstentions)** unresolved". The old figure described the campaign minus its
abstentions.

---

## D. `genomes` is never published (#98)

Docstring on `lakehouse_export_rows` (`council_publish.py:700`) and a comment
on the preset's `[source.result]` payload
(`backfills/function-to-go-council.toml`). Three reasons, the third decisive:

1. The shipped preset already omits the column and neither export path has
   ever published it — there is nothing to preserve.
2. The genome set **grows** as genomes are added while the slot **freezes** on
   first write, so a published list would go stale the first time a new genome
   carried the function, and keep reading as authoritative while stale.
3. The only in-pipeline source is `priority_adp1_pmi.tsv` — a join on the
   definition **text**, which the preset refuses by name in its own section 1.

Which genomes carry a function is a read-time join through `protein_result`.
`verdicts.EXPORT_COLUMNS` **keeps** the column so the run-directory parquet
stays byte-compatible: it is published empty, not dropped.

---

## Tests added

All in two files; nothing was deleted.

### `tests/domains/test_council_bundle.py` (+331 lines, 3 classes)

- **`TestTheAbstentionTexts`** — `BUNDLE_SCHEMA_VERSION == 2`; every
  abstention carries the plan row's text verbatim; a plan row with no text
  yields an **empty** text rather than a build-time refusal; the text survives
  a disk round trip; the text is covered by the digest.

  The "no build-time refusal" case is a deliberate design assertion: the
  refusal belongs at publish, where it can name the rows that would land in a
  frozen slot, and a bundle has to stay *buildable for inspection* in order to
  be fixable.

- **`TestThePlanOrigin`** — `plan_origin` is a declared content field; a
  stage-1 plan needs no declaration and takes the plan's own date; the two
  kinds and only those; an unknown kind and a **misspelled field** are both
  refused rather than dropped; a `reconstructed` origin must declare all four
  (parametrized over each); a declared origin is carried whole and the plan's
  own date does not override it; a campaign **with** abstentions may not have
  a blank `generated_at` and one **without** may; the origin is covered by the
  digest.

- **`TestBuildFromARunDirectoryInfersNoOrigin`** — a hand-assembled run
  directory (no `generated_at`, no `plan_hash`, no `run_id` — the real
  2026-10-19 shape) is refused when passed off as stage 1; refused again when
  declared reconstructed without the four; builds when all four are explicit,
  **with `plan_hash` still `''` and no date invented for the plan**. A stage-1
  run directory still needs no declaration.

### `tests/berdl/test_council_publish_lakehouse.py` (+383 lines, 5 classes)

- **`TestTheAbstentionTextRefusal`** — a blanked and a whitespace-only text
  each refuse naming the `query_id`; the whole publish refuses and writes
  neither parquet nor shard; **every** blank one is named, not just the first;
  a good bundle reports `abstentions_without_text == []`.
- **`TestTheV1BundleRefusal`** — a v1 bundle with abstentions is refused and
  told to rebuild; refused at publish before anything is written; the v1
  refusal **wins** over the blank-text one; a v1 bundle with no abstentions is
  not refused.
- **`TestTheEmptyPlanHashRefusal`** — `plan_hash == ''` with a stage-1 origin
  is refused; declared reconstructed it is accepted (the 2026-10-19 campaign's
  own case); a non-blank hash needs no declaration; refused at publish before
  anything is written.
- **`TestTheAbstentionDecidedAt`** — `decided_at` comes from
  `plan_origin.generated_at` and **not** from the plan's own date (the fixture
  sets them to different days so the assertion can distinguish them); every
  abstention carries exactly that value; the receipt carries the origin.
- **`TestTheCoverageFloorHelpText`** — the real `--help` output contains "8 of
  20,036 submitted query_ids" and "19,276 fanout + 760 abstentions", and no
  longer "of 19,276 inputs".

The v1 fixture (`_v1`) is built by **degrading a real v2 bundle**, not written
by hand: a hand-written dict would prove the publisher reads a field, not that
the field means what v1 wrote.

## Existing fixtures updated, each explained

Two, both in `tests/berdl/test_council_publish_lakehouse.py`. Nothing deleted.

1. **The shared `_bundle` helper's default abstain row gained `text`**
   (`ABSTAIN_TEXT = "DUF1234 family protein"`). It always carried one in a
   real plan — the fixture simply omitted the field, which v1 tolerated
   because the publisher took abstention texts from a kwarg. With v2 the
   bundle is the source and a blank refuses, so a fixture without it now
   (correctly) refuses. **This one fixture accounted for all 22 initial
   failures** — 14 failed and 8 errored at setup — across `TestTheThreeGates`,
   `TestPinFanoutAndWinners`, `TestThePayload`, `TestThePublishAndTheReceipt`,
   `TestIdempotency` and `TestTheCli`. Updating it fixed all 22.

2. **`test_an_abstentions_text_is_absent_and_said_to_be_absent` → renamed
   `test_an_abstentions_text_COMES_FROM_THE_BUNDLE`.** It pinned the exact v1
   behaviour this change replaces: that the lake has no text for an abstention
   so the publisher reports `abstentions_without_text`, and that `texts=` can
   fill it. Both halves are now wrong by design. Rewritten to assert the text
   arrives from the bundle and that `texts=` **cannot** override an
   abstention. Kept and re-pointed rather than deleted, because the question it
   asks — where does an abstention's text come from — is still the right
   question and now has a different answer; its docstring records what it used
   to assert.

No fixture anywhere pinned `BUNDLE_SCHEMA_VERSION == 1` as a literal, so none
needed changing for the bump. `tests/domains/test_council_bundle.py`'s
`test_the_bundle_carries_every_declared_field` compares against the constant
rather than against `1`, and passed unchanged.

## Verification

Baseline: **`build-baselines/kbutillib-h100-main-6310d61.json`** (captured
2026-10-19 at `6310d61`, h100, `~/venvs/kbdl/bin/python`). Used as a **name
set**, per its own `compare_by: "nodeid, never count"`.

I did **not** re-run the full suite. That file records `duration_s: 3416`
(57 min) with an explicit warning that it is 5.7× the 600 s sub-agent Bash
ceiling and that only a conductor may run it. It also records a
`WORKTREE_MEASUREMENT_TRAP`: a full run inside `.maestro/worktrees/` yields
~5 spurious setup errors in `tests/core/test_composition_smoke.py` from a
fixture resolving a data file into a sibling worktree, so a worktree run
compared against that main-clone baseline shows phantom regressions. A
targeted selection is the correct instrument here.

Targeted run, every test file that touches a changed module (found by
`grep -rln "council_bundle\|council_publish\|EXPORT_COLUMNS\|function-to-go-council\|coverage_floor" tests/`):

```
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python \
  -m pytest tests/domains/test_council_bundle.py \
            tests/berdl/test_council_publish_lakehouse.py \
            tests/domains/test_ontomap_council_publish.py \
            tests/berdl/test_council_rehearsal.py \
            tests/cli/test_ontomap_cli.py \
            tests/berdl/test_clearinghouse_function_to_go_preset.py \
            tests/berdl/test_clearinghouse_ingest.py \
            tests/berdl/test_clearinghouse_manifest_and_shard.py \
            tests/berdl/test_council_delivery.py \
            tests/berdl/test_council_sink.py \
            tests/cli/test_clearinghouse_operator.py \
            tests/domains/test_ontomap_council_status.py \
            tests/domains/test_ontomap_pipeline_rehearsal.py \
            tests/domains/test_ontomap_unrun_bundle.py \
            tests/domains/test_verdict_council_reference.py \
            tests/domains/test_verdict_export.py \
  -q -p no:cacheprovider --no-header
```

**`752 passed, 3 skipped in 18.01s`, exit code 0.** The 3 skipped are
pre-existing skips in `tests/berdl/test_clearinghouse_ingest.py`.

Before my change the same selection had 574 passed / 3 skipped (measured on
the base tree, excluding the four files I went on to edit). The four edited
files went 139 passed → 309 passed. The arithmetic: +170 is the 157 assertions
I added plus the 13 that moved from the rewritten test.

Intermediate measurement worth recording: the FIRST run of this selection
after the implementation commit gave **14 failed, 8 errors, 117 passed**. All
22 traced to ONE stale fixture (see below), not 22 defects. Fixing that
fixture took the file to all-passing.

**Regression framing, not green framing.** None of these files appears in the
baseline's `failures_by_area` (`biochem` 27, `cli` 19, `modeling` 16,
`domains/ai` 5, `core` 2), so their base state is all-passing and the
comparison is meaningful. I did not establish the base state of the suite as a
whole and do not claim the suite is green — the baseline records 69 known
failures at `6310d61`, concentrated in areas this change does not touch.

## The acceptance rehearsal, step by step

Command, run from this worktree with a **fresh** work root (the v1 evidence at
`/scratch/chenry/t11-rehearsal` was neither reused nor touched, and the v1
report is untracked-unmodified):

```
TMPDIR=/scratch/chenry/tmp /home/chenry/venvs/kbdl/bin/python \
  scripts/council/acceptance_rehearsal.py \
  --snapshot  /scratch/chenry/tmp/council-snapshot-20261023.sqlite \
  --work-root /scratch/chenry/t11-rehearsal-v2 \
  --report    agent-io/reports/2026-10-26-council-lakehouse-t11-acceptance-rehearsal-v2.txt \
  --json      /scratch/chenry/t11-rehearsal-v2-report.json
```

**Preconditions, both checked, not assumed.** The snapshot was present and
hashed `37535e28382fd3323af9eab2fa0a6f4612d0852856c650cf6cf42740e9bcef32` —
the expected value, byte for byte, so no re-decompression from the Dropbox
`.xz` was needed. The run directory `/scratch/chenry/xport.Iv9Tft` had
survived, with `abstain.jsonl` (760 rows), `fanout.jsonl` (19,276 rows) and
the 141 MB `PLAN.json`. Nothing was reconstructed.

### It took three runs. The first two found real defects.

**Run 1 — FAILED.** Step 0 died with
`FileNotFoundError: /scratch/chenry/tmp/pip-build-tracker-owajbjkv` inside
`Path.rglob`, taking steps 5, 6 and 7 down with it as NOT ASSERTED (6
asserted, 1 failed, 3 not asserted). `rglob` raises when a directory it has
decided to descend into vanishes first, and `/scratch/chenry` is both a search
root and the parent of `TMPDIR` on a shared host. Fixed in `4d5c3f4` with
`find_plan_files`, an `os.walk(onerror=...)` walk that skips the vanished
subtree and keeps going.

**Run 2 — ASSERTED, but on a wrong `assembled_on`.** Fixing the walk exposed a
second step-0 defect that run 1 had masked: `write_publish_plan` re-writes the
bundle's plan into every publish directory, so the **v1 evidence** at
`/scratch/chenry/t11-rehearsal/publish-explicit-floor/PLAN.json` matched the
campaign on every count step 0 checked — same `go_mf@4`, same
`counts.submit = 19276`, and both record files still resolved because the plan
names them by absolute path into the real run directory. It also sorts
*before* `xport.Iv9Tft`, so the rehearsal would have taken its own prior
output as its input. Also fixed in `4d5c3f4`: a candidate now carries
`self_contained` — does its plan name records that are its own siblings? — and
matches are stable-sorted to put the owning directory first, with a new
assertion pinning that the chosen one owns its records. Neither defect was
reachable on 2026-10-26 07:50Z, because that run's work root was fresh and its
own publish output did not yet exist when its step 0 ran.

**Run 3 — ASSERTED**, with `assembled_on` corrected to the measured
`2026-10-23`. See the reconciliation section: the prompt's `2026-10-19` is the
campaign's *name*, not the date the plan was assembled.

### Final result

```
VERDICT: ASSERTED  (10 asserted, 0 failed, 0 not asserted; 144/144 assertions held)
seconds          226.42
snapshot_id      d117cd6330e0d14e7b933c5f2123c8c99fc364fff838bacd7a5ccb254fdcfe42
```

Exit code **0**, zero `[FAIL]` lines. Against v1's `ASSERTED (10 asserted, 0
failed, 0 not asserted; 131/131)` in 211.31 s: **+13 assertions, same ten
steps, still zero failures.** Report committed verbatim at
`agent-io/reports/2026-10-26-council-lakehouse-t11-acceptance-rehearsal-v2.txt`
(byte-identical to the run's stdout). The v1 report at
`…-t11-acceptance-rehearsal.txt` was not overwritten.

All ten steps ASSERTED, including the one that records the corrected date:

```
[ok] the plan is RECORDED AS A RECONSTRUCTION, with when it was assembled
     and why -- not regenerated and not passed off as stage 1's
     expected=["reconstructed", "2026-10-23", true, true]
     measured=["reconstructed", "2026-10-23", true, true]
```

### The three acceptance bullets

**1. "It must produce the same 20,028 rows."** ASSERTED, from a v2 bundle
built from the run directory:

```
[ok] 6b: 20,028 assertion rows -- 19,268 council-decided plus 760
     abstentions, on distinct query_ids
     expected=[20028, 20028, 19268, 760]  measured=[20028, 20028, 19268, 760]
[ok] 6b RECEIPT, reconciled on DISTINCT query_ids: 20,036 submitted
     against 20,028 with a verdict, 8 unresolved
     expected=[20036, 20028, 8, true]  measured=[20036, 20028, 8, true]
```

Coverage 20,028 / 20,036 = 0.999601. The 6a default-floor refusal and the 6b
explicit-floor publish both still behave as before, and the eight the default
floor refuses for are still exactly the eight the 6b receipt names.

**2. "When a fixture blanks one abstention's text, the publish must refuse,
not just count it."** ASSERTED, as new step 6c. The fixture blanks one
abstention in a **copy** of the bundle:

```
[ok] 6c: blanking ONE abstention's text makes the publish REFUSE -- it is
     no longer merely counted in the receipt
     expected=AbstentionTextMissingError  measured=AbstentionTextMissingError
     evidence: 1 of 760 abstention(s) in the bundle for campaign
     'function-to-go/2026-10-19-adp1-pmi' carry no function text, and the
     clearinghouse slot freezes on FIRST WRITE ...
[ok] 6c: the refusal NAMES the query_id ...
     evidence: query_id=0098f9eb2883e7e43bf11011b243a70d71724267560729fa9740fe8674d7cc7c
[ok] 6c: it refused BEFORE writing -- no parquet and no shard
     expected=[false, false]  measured=[false, false]
[ok] 6c: the real bundle is UNTOUCHED by the fixture -- 760 texts still
     present and its digest unchanged
```

**3. "Every abstention's `decided_at` must equal
`plan_origin.generated_at`."** ASSERTED:

```
[ok] 6b: EVERY abstention's decided_at is plan_origin.generated_at -- ...
     expected=[760, []]  measured=[760, []]
     evidence: plan_origin.generated_at=2026-10-20 18:25:44.023091
               -> decided_at=2026-10-20T18:25:44.023091Z
[ok] 6b: that date is NOT today -- the publish date would be the wrong
     answer and is what the now() fallback would have given
     expected=False  measured=False
```

The second assertion is the one that would have caught the bug this task
exists to prevent: it fails if `decided_at` is ever the publish date.

### The derived `generated_at`, measured

```
[ok] all ten BASE jobs are in the archive and carry a submitted_at, so
     min() is over the whole campaign and not a subset
     expected=[10, 10]  measured=[10, 10]
     evidence: earliest=2026-10-20 18:25:44.023091
[ok] plan_origin.generated_at IS min(council.jobs.submitted_at) over the
     ten base jobs -- the date all 760 abstentions freeze at
[ok] the hand-assembled plan is carried WHOLE and UNEDITED: its plan_hash
     is still the empty string and no generated_at was invented for it
     expected=["", false]  measured=["", false]
[ok] EVERY abstention carries its function text IN THE BUNDLE
     expected=[760, 760]  measured=[760, 760]
```

The ten base jobs ran `2026-10-20T18:25:44Z` → `2026-10-21T03:27:52Z`, with
the two retries on `2026-10-22`. The retries are excluded from the `min` on
purpose: they were submitted a day later and the abstentions were declined
before any of it. (Including them would not change the minimum here, but the
rule should not depend on that.)

The bundle itself: schema v2, `137,896,135` bytes on disk, digest
`1f78fe7791124e8c2fe66db329ade0f2eb424a1c67d13772c59686634f0c3de6`, and the
lakehouse copy agrees with the on-disk one by content digest.

### snapshot_id: MISMATCH, and it is expected

The task asked me to compare against v1's
`8072acd71ce4bb6a3c39c163724fc887115028484a1b934ff8841effe50cbcc7`.

| run | snapshot_id |
|---|---|
| v1 (2026-10-26 07:50Z) | `8072acd71ce4bb6a3c39c163724fc887115028484a1b934ff8841effe50cbcc7` |
| v2 run 1 | `8fc2d3cf1a974710101e9fe8b3c25ad384a4a5599878fd0ecc4c2e37147b6e9f` |
| v2 run 2 | `c6367b15be81b75a5ed84ee408e5e1684dba2aee0cb8e183758d8d2000a94463` |
| v2 run 3 (final) | `d117cd6330e0d14e7b933c5f2123c8c99fc364fff838bacd7a5ccb254fdcfe42` |

**Four runs, four values — and this is a property of the rehearsal, not of
my change.** The rehearsal's `snapshot_id` is not the input snapshot's digest
(`37535e28…`, which was identical across all three). It is the sha256 of a
**fresh snapshot file the rehearsal itself writes** in step 2, after copying
the store and calling `assign_store_identity`. That records a `declared_at`
which **defaults to `now()`** (`store/sqlite.py:2746`), so two runs at
different wall-clock times snapshot different bytes and necessarily get
different digests.

The v1 report already said half of this in its own step-2 note: *"the
rehearsal's snapshot_id is NOT the production snapshot's 37535e28…: assigning
the identity added a store_identity row, so these are deliberately different
bytes."* It is non-reproducible **across runs** for the same reason it differs
from the input.

So: a mismatch, explained, and **a finding rather than a failure** — but the
finding is about the task's expectation, not the code. `snapshot_id` is not a
usable cross-run fixture for this rehearsal, and asking a future run to
reproduce `8072acd7…` would require pinning `assign_store_identity(now=…)`.
I did not add that pin: it is outside this task, and a rehearsal that froze
its own identity timestamp would be asserting less than it does now. What *is*
stable across runs and *did* match is everything that matters — 20,036
submitted, 20,028 rows, 19,268 decided, 760 abstentions, 8 unresolved, and
the same eight `query_id`s.

### What the rehearsal does NOT establish

Unchanged from v1 and worth repeating: **the in-pod load was not performed and
is not claimed.** The receipt reports it as `awaiting operator acceptance` and
`target_table_snapshot_id` as `None` with its reason — the Iceberg commit that
assigns it is made by `kbu clearinghouse load` in the pod, and nothing here
has been verified against a live BERDL pod (KBUtilLib#91). The rehearsal wrote
only into `/scratch/chenry/t11-rehearsal-v2` under store id
`rehearsal/council-20261023`; `/scratch/chenry/council/council.sqlite` still
has its 2026-10-22 mtime and nothing named `poplar/council-1` was written.

## Things in the task prompt I found to be false, or that diverged

**Every line-number anchor in the prompt was exactly right**, verified
against `0bb8a88` rather than assumed:

| Prompt claim | Measured at `0bb8a88` |
|---|---|
| `council_bundle.py` `BUNDLE_SCHEMA_VERSION = 1` at `:115` | `:115` ✓ |
| `build_bundle` at `:263` | `:263` ✓ |
| `build_bundle_from_run` at `:464` | `:464` ✓ |
| `council_publish.py` `lakehouse_export_rows` at `:602` | `:602` ✓ |
| `missing_text` collected at `:643-648` | `:643` and `:648` ✓ |
| reported as `abstentions_without_text` at `:680` | `:680` ✓ |
| `publish_from_lakehouse` at `:732` | `:732` ✓ |
| `verdicts.py` `export_rows` at `:194` | `:194` ✓ |
| `declined_at = _iso(abstained_at) or _now_iso()` at `:286` | `:286` ✓ |
| `ontomap.py` `--coverage-floor` at `~:1753` | `:1753` ✓ |
| help text `"of 19,276 inputs unresolved"` at `:1760` | `:1760` ✓ |
| the preset's `[source.result]` payload omits `genomes` | ✓ |
| `council.jobs` has `submitted_at`, not `created_at` | ✓ |
| the v1 t11 report is `VERDICT ASSERTED 131/131` | ✓, 10 steps, 211.31 s |
| `/scratch/chenry/tmp/council-snapshot-20261023.sqlite` sha256 `37535e28…` | ✓, byte-for-byte |
| the run directory `/scratch/chenry/xport.Iv9Tft` with its two records | ✓, 760 + 19,276 rows |

Five things diverged. **One of them was a substantive factual error** and the
rest were cosmetic:

0. **`assembled_on 2026-10-19` IS WRONG, and I recorded `2026-10-23`.** This
   is the one divergence that would have shipped a false statement. The
   prompt's step 1 said to pass "assembled_on 2026-10-19"; `2026-10-19` is the
   **campaign's name** (`CAMPAIGN_ID =
   'function-to-go/2026-10-19-adp1-pmi'`), and nothing in the record happened
   on that day. The tree is unambiguous on all three dates:

   | date | what it actually is | evidence |
   |---|---|---|
   | `2026-10-19` | the campaign's NAME, only | `CAMPAIGN_ID` |
   | `2026-10-20T18:25:44Z` | first base job submitted (ten ran to `2026-10-21T03:27:52Z`; two retries `2026-10-22`) | `jobs.submitted_at` in the snapshot |
   | `2026-10-23` | when the PLAN was assembled | `PLAN.json` mtime `2026-10-23 04:26`, records `2026-10-23 01:18` |

   And step 0's **own finding, on `main` before this task**, has always read
   "hand-assembled on **2026-10-23**". So the prompt contradicted both the
   file mtimes and the repo's own recorded finding.

   `assembled_on` means *when the plan was assembled* — the third row. Writing
   the campaign's name there would have put a false provenance claim in the
   single field whose entire purpose is to be honest about where the plan came
   from, which would have been self-defeating. Corrected in `07e9927`, with
   `PLAN_RECONSTRUCTION_REASON` now stating all three dates and step 5
   recording the distinction as a finding so the next reader need not
   re-derive it.

   This does not affect `generated_at`, which the rehearsal **measures** from
   the archive rather than taking from the prompt, and which came out
   `2026-10-20T18:25:44.023091Z` as expected.

The cosmetic four:

1. **`council schema.py ~:178` is not a path.** The file is
   `src/kbutillib/domains/ai/council/store/schema.py`, and `submitted_at` is
   indeed at `:178` there. Resolved by name. (The *archive* side's
   `council_schema._JOBS_COLUMNS` also carries `submitted_at` and no
   `created_at`, so the claim holds on both sides of the archive boundary —
   which matters, because the rehearsal reads it from the archived snapshot,
   not from the store.)

2. **The prompt's test-file list omitted the file where most of the work
   landed.** It named `tests/berdl/test_council_rehearsal.py`,
   `tests/domains/test_council_bundle.py` and
   `tests/domains/test_ontomap_council_publish.py`, but **not**
   `tests/berdl/test_council_publish_lakehouse.py` — which is where
   `lakehouse_export_rows`, `publish_from_lakehouse`, the receipt and the CLI
   verb are all tested, and where all 22 initial failures and 5 of the 8 new
   test classes are. A developer who took that list as the scope of affected
   tests would have missed every one of them.

3. **The abstain loop is at `~:348-371`, not `~:351-367`.** The prompt said
   "~", so this is within tolerance; noting it only because the loop is two
   lines longer at both ends than stated.

4. **The rehearsal could not actually run as instructed, for two reasons
   neither of which the prompt anticipated.** Both were real defects in step 0
   and both are fixed in `4d5c3f4`; see the rehearsal section. The prompt's
   step 3 said the run "spends zero tokens and took 211 s last time", which
   was accurate, and its warning that `/scratch` is ephemeral was the right
   warning — but the hazard that actually bit was the opposite one: `/scratch`
   had *too much* in it, including the v1 evidence the prompt told me to
   preserve, and that evidence shadowed the real run directory.

One instruction I could not satisfy as literally worded: **the prompt asked me
to "Print `kbutillib.__file__` in the report or work record and confirm it
resolves inside YOUR worktree."** The rehearsal script prepends its own
checkout's `src/` to `sys.path` but does not print the resolved path, and I
chose not to add a print to it for this. Verified directly instead:

```
$ TMPDIR=/scratch/chenry/tmp /home/chenry/venvs/kbdl/bin/python -c \
    "import sys; sys.path.insert(0,'$PWD/src'); import kbutillib; print(kbutillib.__file__)"
/mnt/homes/chenry/.maestro/worktrees/kbu-97-council-bundle-v2/src/kbutillib/__init__.py
```

and every rehearsal traceback in the first run's report names
`/mnt/homes/chenry/.maestro/worktrees/kbu-97-council-bundle-v2/src/kbutillib/...`,
which is independent confirmation from the run itself. The kbdl venv's
installed KBUtilLib at `~/projects/KBUtilLib` (pinned at `ec78ea7` by Chris)
was **not** pulled, checked out, edited or reinstalled. Nothing was written to
`/scratch/chenry/council/council.sqlite` or to anything named
`poplar/council-1`: the rehearsal's store id is `rehearsal/council-20261023`
and everything it wrote is under `/scratch/chenry/t11-rehearsal-v2`.
