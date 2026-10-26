# Work record: council-lakehouse-t4-t11 — PRD council-record-lakehouse-v1, tranche 4

## task_id

`council-lakehouse-t4-t11-rehearsal` (Maestro, developer role, h100)

## prd

`council-record-lakehouse-v1`, tranche 4, one item: **t11-acceptance-rehearsal**.

## branch

`maestro/developer/council-lakehouse-t4-t11-acceptance-rehearsal`

## base

`950602e` — `docs: work record for council-lakehouse tranche 3 (t10, t9, t7)`.
Tranche 3 landed; nothing in tranches 1–3 was modified by this item.

## completion status

**COMPLETE, and better than the task expected.** The deliverable is a target
plus one real run of it, and both are committed:

| commit | what |
|---|---|
| `42ee0ba` | the target: `council_rehearsal.py`, `scripts/council/acceptance_rehearsal.py`, and unit tests for the target's own logic |
| `be58982` | the run report, text and JSON, from one real run against the production snapshot |

**The run: ALL TEN STEPS ASSERTED. 131 of 131 assertions held. 0 FAILED, 0 NOT
ASSERTED. 211 s. ZERO tokens, no model call, no endpoint contacted, no live
council run, no production clearinghouse row written.** Nine findings are
carried as findings, not fixed (rule 9).

The single biggest difference from what the task anticipated: **step 0 found
the run directory.** The prompt said its location was "NOT recorded anywhere
I could find", that `/scratch` is ephemeral so it was probably gone, and that
if so steps 5, 6 and 7 should be reported NOT ASSERTED. The search found it at
`/scratch/chenry/xport.Iv9Tft`, with its `abstain.jsonl` and `fanout.jsonl`
intact — so the bundle, the two-floor publish and the per-row cross-check were
all **rehearsed for real** instead of being reported as gaps. Details below.

---

## The deliverable

**One command**, as rule 5 requires:

```bash
python scripts/council/acceptance_rehearsal.py \
    --snapshot /scratch/chenry/tmp/council-snapshot-20261023.sqlite \
    --work-root /scratch/chenry/t11-rehearsal \
    --report  agent-io/reports/2026-10-26-council-lakehouse-t11-acceptance-rehearsal.txt \
    --json    agent-io/reports/2026-10-26-council-lakehouse-t11-acceptance-rehearsal.json
```

The script is argument handling, a snapshot digest verification and output. The
logic is `src/kbutillib/domains/kbase/berdl/council_rehearsal.py`, which is
importable and unit-testable without the 896 MB snapshot.

### The report shape IS the deliverable, not decoration

Rule 5: *"A step that silently passes over something it could not check is the
failure this task exists to prevent."* So the report is not pass/fail. Every
step settles into exactly one of three states, and the rule that makes that
honest is `Step.settle` (`council_rehearsal.py:227`):

- an explicitly declared `NOT ASSERTED` or `FAILED` **stands** — a later
  assertion that happened to hold cannot talk a step out of its own refusal;
- any **false** assertion makes the step `FAILED`, and the reason **names the
  failing claims** rather than counting them;
- a step with **zero assertions is `NOT ASSERTED`, never `ASSERTED`** — a step
  that checked nothing established nothing.

That last clause is the one that matters, and it is the one most likely to rot,
so it is the first test in `tests/berdl/test_council_rehearsal.py`.

Every assertion carries `expected` **and** `measured` even when it holds,
because half the value of this run is that it is a *measurement* of the
snapshot, not only a verdict. There are **no vacuous assertions** in the
target: `grep 'ok=True,'` returns nothing. Two existed in a first draft
(`check("...", True, True, ok=True)` next to a call whose only effect was not
raising) and both were replaced with checks on the actual return value.

Exit codes: `0` when no step FAILED, `1` when one did, `2` when the rehearsal
refused its target. A `NOT ASSERTED` step does **not** fail the run by default
— a leg whose input does not exist is a reported gap, and conflating it with a
broken one is exactly what this report shape exists to prevent — but `--strict`
makes it exit non-zero for a CI caller that wants that.

### Nothing can touch production

`assign_store_identity` is **one-way** (`redeclare_store_identity` is the only
way back), so `require_scratch_target` (`council_rehearsal.py:108`) gates every
path the rehearsal writes, and it refuses twice:

- a work root outside `("/scratch/", "/tmp/")`;
- **by exact path**, `PRODUCTION_STORE_PATHS` — the live store
  `/scratch/chenry/council/council.sqlite`, the snapshot taken from it, and
  `/scratch/chenry/tmp/council-snapshot-20261023.sqlite`. That third one is the
  shared read-only decompressed copy from tranche 1, which other work depends
  on **by digest**; it is read-only input here and is never written.

The identity declared is `rehearsal/council-20261023`. Never `poplar/council-1`
— that id is reserved, its assignment is Chris's decision, and an archived row
keyed on it cannot be corrected once a clearinghouse payload carries it.

---

## STEP 0 — the run directory EXISTS, and the prompt's NOT-FOUND branch did not apply

This is the finding that changed the shape of the whole item, so it is first.

The prompt's step 0 said the 2026-10-19 run's `PLAN.json` location "is NOT
recorded anywhere I could find", that `/scratch` is ephemeral and it may be
gone, and gave a NOT-FOUND branch: mark steps 5, 6 and 7 NOT ASSERTED.

**I nearly took that branch, and would have been wrong.** My first manual
`find` listed `/scratch/chenry/xport.Iv9Tft/PLAN.json` and I dismissed it as a
pytest tmpdir by its name. It is not. It is the campaign's real run directory:

```
/scratch/chenry/xport.Iv9Tft/
  PLAN.json          141 MB   query go_mf@4, counts {submit: 19276, abstain: 760, fanout: 19276}
  abstain.jsonl      760 rows   {query_id, text, source, abstain_reason, decline_reason}
  fanout.jsonl    19,276 rows   {query_id, item_id}
  MANIFEST.toml / PUBLISH.json / shard/   the 2026-10-23 run-directory publish
```

Writing the search into the target rather than doing it by hand is what caught
it. That is the lesson worth keeping: **the rehearsal's own step 0 was more
careful than I was.**

Measured properties, all asserted (7/7):

- fanout is **1:1** — 19,276 rows, 19,276 distinct `query_id`, 19,276 distinct
  `item_id`. No text is shared in this campaign.
- the two partitions are **disjoint** on `query_id` (overlap 0).
- **all 760 abstain rows carry their function text.** This is what makes
  header fact C assertable instead of a gap.
- 19,276 + 760 = **20,036** = every distinct `entity_hash` in
  `priority_adp1_pmi.tsv`. There is no unexplained residue.

### Three findings from step 0

**(1) The plan was NOT written by stage 1, and a reader should know it.**
`OntologyMappingUtils.council_plan` writes ~25 keys. This file carries **13**,
missing `run_dir`, `run_id`, `generated_at`, `plan_hash`, `chunks`,
`items_file`, `ranked_file`, `concurrency`, `transport_allocation`,
`mapping_version`, `unrun_bundle_file` and `counts.universe/done/unrun`. It was
hand-assembled on 2026-10-23 to drive the run-directory export and publish,
because the campaign itself was driven by `kbu submit --query go_mf@4 --input
chunkNN.jsonl` from `/scratch/chenry/council/run_chunks.sh` and **predates
stage 1 entirely**. `build_bundle_from_run` reads only `query`, `mapping_hash`,
`parameter_set`, `parameter_set_hash`, `plan_hash`, `generated_at` and the two
record files, so it works — but the bundle's `plan` block, and the `PLAN.json`
the publisher re-writes beside its output, carry a plan no stage-1 run would
produce, and its `plan_hash` is the **empty string**.

**(2) The fanout's `query_id` key is corroborated but NOT independent, and this
bounds what step 7 proves.** Measured: all 19,276 fanout and all 760 abstain
`query_id`s are `entity_hash`es in `priority_adp1_pmi.tsv` (20,036 distinct
there, and `definition -> entity_hash` is **1:1** with zero ambiguity); every
fanout `item_id` exists in the twelve published jobs; and the fanout's
`(query_id, item_id)` pairing agrees with the text join
`store item text -> TSV definition -> entity_hash` for **19,276 of 19,276 rows,
0 disagreements**. That text join is *also* how the 2026-10-22 verdict export
resolved its `clearinghouse_entity_hash`
(`/scratch/chenry/council/export_results.py`). So step 7's **key** is common to
both sides. What step 7 *does* check independently is the **verdict content**
— `go_ids` and `decided_by` — produced by two different folds: the publisher's
bundle-ordered `verdicts_by_item` versus `export_results.py`'s `item_order`
scan. Saying that precisely is the point; claiming step 7 corroborates the key
would be false.

**(3) The results MANIFEST misdescribes the 760.**
`2026-10-19-clearinghouse/council-results/MANIFEST.json` records
`in_priority_list_but_NEVER_submitted: 760` with the note "definitions that
were not in the submitted set". They are in fact the plan's **retrieval
abstentions** — declined before submission with a recorded `abstain_reason`
("placeholder function: 'uncharacterized protein ylbe'" and the like). That is
a materially different statement: an abstention is a *published verdict* with
`council = null` and a decline reason, not a gap.

---

## Steps 1–4 and 8–9 — the archive rail, by direct query

### Step 1 — bootstrap (5/5)

Bootstrap into a scratch `SqliteCouncilSink`; assert every table in
`table_configs()` exists, that each created table's columns **read back out of
the database** equal its declaration, that a second bootstrap is idempotent,
and that the file is a scratch file and not the production store.

`publication_bundle` is in that set, and getting it there was a real step: it
registers itself with `council_sink.register_table` **at import** of
`council_bundle`, so a sink built before that module was imported simply does
not carry it. The first draft asserted nine tables because it had not imported
the module. Fixed with a deliberate side-effect import (`# noqa: F401`) and a
comment saying why.

### Step 2 — identity, manifest v1, and the SCANNER (18/18)

A **fresh copy** of the snapshot, then `assign_store_identity`. Asserted: the
production snapshot carries **no** declared identity before this; the id reads
back out of the store file; re-declaring the **same** id writes nothing (same
`declared_seq`); and declaring a **different** id raises
`StoreIdentityAlreadyDeclared` rather than replacing it.

Then `SqliteStore.snapshot()`, `build_manifest`, `write_manifest`, and the
delivery is driven through **`CouncilDeliveryScanner.tick()`** — not by calling
`archive_snapshot` directly, because the rail is what is being rehearsed.
Asserted: manifest `schema_version == 1`; it round-trips through
`read_manifest` with the same `manifest_digest`; it carries `store_host` (which
tranche 3's `9d73ff6` added to COUNCIL-DELIVERY v1); its inventory is
**measured** — the snapshot entry's sha256 equals the `snapshot_id`; the
scanner enumerates the drop and reaches `state: complete`; the archive stage is
`ok`; the publish stage is **`not_applicable`, never `ok`**, because this
delivery declares no bundle; `require_complete_delivery` says complete; every
archiver table reconciled; and `job_disposition` is declared
`not_written_here`.

The rehearsal's `snapshot_id` is deliberately **not** the production
`37535e28...`: assigning the identity added a `store_identity` row, so these
are different bytes, and keying archived rows on the verified digest of the
file actually delivered is the whole point.

### Step 3 — the archive BY DIRECT QUERY (21/21)

Not by exit code and not by trusting a verify command. The store side is
measured **independently**, straight out of the snapshot file with `sqlite3`,
so the comparison is not the archiver agreeing with itself.

| asserted | value |
|---|---|
| jobs | **21** |
| calls | **203,821** — the store's full count, and `!=` the live subset |
| live / not-live split, per row | 88,465 / 115,356, and they partition the table |
| live split per role | critic 20,368/23, meta 4,951/0, proposer 63,146/115,333 |
| items by status | ok 20,125, error 627, pending 242, cancelled 38,499 |
| workers | 6 rows, and **every `version` IS NULL** (non-null count 0) |
| role_brief | **15** rows = 5 query versions × 3 roles, **0** `<unextractable>`, 0 empty `answer_schema` |
| query_types / attempts / endpoint_state | equal to the store's, measured independently |

#### Finding: the 82,270 vs 88,465 "disagreement" is RESOLVED — both numbers are right

The task header states, as measured fact A, *"LIVE CALLS ARE 88,465, NOT
82,270"*, that the PRD's figure "does not match this snapshot", and instructs
step 3 to "name the 82,270 vs 88,465 disagreement as a finding".

Measured on this snapshot:

```sql
SELECT COUNT(*) FROM calls WHERE live=1;                        -- 88,465
SELECT COUNT(*) FROM calls WHERE live=1
  AND job_id IN (<the 12 published jobs>);                      -- 82,270  EXACTLY
```

**There is no disagreement.** 88,465 is the whole store's live subset across
all 21 jobs; 82,270 is the *published campaign's*. The difference, 6,195, is
the trials, the pilots and the two cancelled look-alikes. The PRD and the
header are counting different populations and both are correct. The thing to
correct at source is the header's instruction to report the PRD's figure as a
mismatch. The rehearsal asserts 82,270 by direct query so this cannot drift
again.

#### Finding: `calls.reply_parsed` makes `IS NOT NULL` vacuous

Header fact A flagged this and the rehearsal pins it: `IS NOT NULL` = **203,821**
(every row), `= 'null'` = **116,445**, `IS NULL` = **0**. So a consumer counting
parsed replies with `IS NOT NULL` overcounts by 116,445; the real figure is
87,376. `council_archive.role_brief_rows` already documents the same trap for
`query_types.spec`; nothing warns a consumer of `calls`.

#### Finding: the snapshot manifest's `reply_text` prose count is off by 44

`_store-backups/MANIFEST.json` says "the raw reply text (87,507 rows)"; measured
on the snapshot it names, `reply_text IS NOT NULL` is **87,551**. Cosmetic — the
manifest's own `contents` block (203,821 / 59,493 / 21) all reconcile exactly —
but the `WHY_THIS_MATTERS` prose figure does not.

### Step 4 — dispositions (9/9)

The **live** provenance JSON was used, and it is **byte-identical** to the
committed test fixture — asserted by sha256
(`fdcbb9f72e4467b62f438c5df6747c81af2305055c21d55515f9b00e907c582b`), so they
agree. Asserted: 21 jobs declared, **12 published / 9 excluded**, the 12 are
exactly the ten chunk jobs plus the two retry passes, re-seeding changes nothing
(`added 0 / changed 0 / unchanged 21`), and then **by direct query** on
`job_disposition`: 12 `published`, 9 not, and one row per job — which is all the
table's declared primary key `(store_id, snapshot_id, job_id)` permits.

### Step 8 — the reuse seed (8/8)

`read_seed_rows` scoped to `go_mf@4`, into a fresh empty store via the real
`seed_from_rows`. Asserted: the snapshot is complete so nothing is refused;
the seed carries **82,416** live-ok-in-scope calls and `seed_from_rows` imported
all of them; `SEED_TABLES` is `(query_types, jobs, items, calls)` — `attempts`,
`endpoint_state` and `workers` are deliberately **not** read; `find_reusable`
hits for **25 of 25** sampled `prompt_hash`es spread across the seed and misses
a hash that was never seeded; **`claim_calls` returns `[]`** through the real
store API; and the store records exactly **one** seed origin.

Tranche 3's stated limits are noted, not tested away: a seeded job is hidden
from the *queue* (tombstoned, and `select_claimable` excludes it), **not** from
a caller who names it; and `kbu council db export` includes seeded rows.

### Step 9 — the second, mutated snapshot (11/11)

The rehearsal's **own** store copy is mutated — one appended `calls` row
(`rehearsal-appended-call-0001`) and +1,000 tokens on the `argo` counter — then
snapshotted at `snapshot_seq 2` and delivered through the scanner.

| asserted | value |
|---|---|
| the two snapshots have different verified digests, same declared store id | `8072acd7…` / `afcf4b58…` |
| the scanner enumerates **both** deliveries every tick | 2 |
| both reconcile `complete`; seq 1 from its **cached receipt re-checked against the lakehouse** | ✓ (tranche 3's `ba22087`) |
| both independently queryable, seq 1 **unchanged** by seq 2's arrival | calls 203,821 / 203,822 |
| nothing overwritten — the store's archived total is the **sum** of two snapshots | 407,643 |
| `current_projection` resolves by `snapshot_seq` to seq **2** | ✓ |
| `endpoint_state` is **NOT additive**: seq 2 is the running total | 106,972,808, where the additive reading would be 213,944,616 — about **2×** the tokens ARGO was ever charged |

---

## Step 6 — BOTH halves of the publish, which is a deliberate two-step (26/26)

Header fact B is correct that the default coverage floor refuses this campaign
by design, and both halves were rehearsed.

**6a — the default floor REFUSES.** `DEFAULT_COVERAGE_FLOOR` is `1.0`; publishing
at it raises `ClearinghouseJoinAssertionError`, and asserted: **no shard
directory** and **no verdict parquet** were written, because `guard_join` runs
on the source side *before* the join and before anything is written.

**6b — the operator's explicit floor.** `--coverage-floor 0.999`, below the
measured coverage, publishes. Asserted:

- **20,028 assertion rows** on 20,028 distinct `query_id`s = 19,268
  council-decided + 760 abstentions;
- **zero** rows attributable to the cancelled or pilot jobs — every decided row
  names one of the twelve;
- every council-decided payload carries `council` with **all three fields
  non-null**, and every `council.store_id` is this store's declared identity;
- every abstention carries `council = null` **with** its decline reason
  (`abstained:…`), and `status` NULL — retrieval declined before any council
  item existed;
- **no** payload carries `council_entity_hash`;
- the publisher writes `go_molecular_function` and never the frozen
  `go_council_mapping`;
- **receipt**, reconciled on distinct `query_id`s: **20,036 submitted / 20,028
  with a verdict / 8 unresolved, `reconciled: true`**, and the eight are
  **NAMED**;
- the eight the default floor refused for are **exactly** the eight the receipt
  names;
- **`abstentions_without_text` is `[]`** — texts supplied from the plan's own
  abstain record, which is the only place they exist;
- items / inputs / rows reported as three different numbers (19,276 / 20,036 /
  20,028);
- the load is `awaiting operator acceptance` and `target_table_snapshot_id` is
  `None` with its reason.

### Finding: the default-floor refusal COUNTS the eight, it does not NAME them

Header fact B says the default-floor publish "REFUSES and names the eight".
Measured: it refuses and **counts** them. `clearinghouse_ingest.join_assertions`
builds its coverage failure from counts (`resolved_keys`, `source_keys`,
`unresolved_keys`) and its report carries no list of unresolved keys, so an
operator who hits the refusal is told *"8 key(s) name no entity the
clearinghouse holds"* and cannot see **which** eight without lowering the floor
and reading the 6b receipt's `unresolved_query_ids`. Adding an
`unresolved_sample` to the join report would close it. **Not done here** —
`clearinghouse_ingest.py` is tranche 1–3 code and rule 9 makes this a finding.

### Finding: the spec's "19,268 assertion rows" is short by the 760 abstentions

The taskplan says "19,268 assertions"; header fact B says "19,276 submitted"
and coverage "0.999585"; header fact C requires abstentions published **with
text** and `abstentions_without_text == 0`. Those cannot all be true at once.

Measured against the real plan:

```
submitted              19,276 fanout + 760 abstentions = 20,036 distinct query_ids
council items settled      19,268
published rows             20,028  = 19,268 decided + 760 abstentions (council=null)
unresolved                      8
coverage                   20,028 / 20,036 = 0.999601
```

All three of 19,268, 19,276 and 0.999585 (= 19,268/19,276) describe the campaign
**minus its abstentions** — they were computed before the abstain record was
located, which is consistent with the prompt saying its location was unknown.
**8 unresolved is right in every reading.** The rehearsal asserts the measured
values and reports the discrepancy rather than asserting the spec's.

### Finding: `publish_from_lakehouse` takes `texts=` but no CLI flag supplies it

`kbu ontomap council-publish-lakehouse` has `--bundle`, `--lakehouse`,
`--publish-dir`, `--snapshot-id`, `--state-root`, `--coverage-floor`,
`--roster-fingerprint` and `--target-bytes` — and **nothing** for `texts=`. An
abstention has no council item, so its `function_text` is not recoverable from
the lakehouse, and the clearinghouse slot is **frozen on first write**: an empty
text published once stays empty forever. An operator driving the publish from
the command line cannot supply abstention texts at all. For *this* campaign the
exposure happens to be nil because the rehearsal supplies them programmatically
from the abstain file; the defect bites the next campaign. Recorded, not fixed
(rule 9, and the prompt said explicitly not to add the flag here).

---

## Step 7 — the per-row cross-check against the independent export (11/11)

`function_to_go_verdicts.jsonl`: **19,268** rows on 19,268 distinct
`clearinghouse_entity_hash`es, `decided_by` {unanimous 14,619, meta 4,649}.

Asserted: every export row has a published counterpart on
`clearinghouse_entity_hash == query_id`; the **760** published-only rows are
exactly the abstentions, which are absent from the export **by construction**
and are therefore **not** disagreements (the export was built from
`items WHERE status='ok'`); the 19,268 council-decided rows are exactly the
export's key set.

Then, **per row across all 19,268**:

- `go_ids` agree — **0** disagreeing rows;
- `decided_by` agrees — **0** disagreeing rows, and the published rows reproduce
  {unanimous 14,619, meta 4,649} exactly;
- the published `council.item_id` equals the export's `council_item_id` — 0
  disagreements;
- the publisher's **bundle-ordered** winner fold picks the same job as the
  export's independent fold for every row including the 105 retried ones — 0
  disagreements;
- no published row carries an empty `function_text`.

The target reports any disagreement **per row with both values** and raises a
finding if there is one. There was none, so nothing was reported — the code path
is exercised by the empty-list assertions.

### Finding: the lakehouse publisher cannot recover `genomes` and publishes it empty

**20,028 of 20,028** published rows carry an empty `genomes` list, where the
2026-10-22 export carried the ADP1/PMI genome lists resolved from
`priority_adp1_pmi.tsv`. `verdicts.export_rows` takes an optional `genomes=` map
and `council_publish.lakehouse_export_rows` never passes one — **correctly**,
because which genomes carry a function is a *clearinghouse* fact and not a
council one, and inventing it from a TSV inside the publisher would be a text
join by another name. Recorded because the two artifacts are **not
interchangeable on that column** and anyone diffing them will see it.

### What step 7 does NOT cover, and says so

The export **predates** the lakehouse publisher and carries **no `council`
column at all** (asserted). So the three-non-null-fields property of a
council-decided payload has no independent artifact to be checked against. Its
`council_item_id` and `job_id` are cross-checked above, covering two of the
three fields; `store_id` has no counterpart, because the export was written
before the store declared an identity.

---

## The live in-pod leg — AWAITING OPERATOR ACCEPTANCE

Reported, never claimed, and a mocked load is never an acceptance. Three
**independent** reasons it is unreachable from here, each sufficient alone:

1. **KBUtilLib#91** — the BERDL transport has been broken on kbhub since the
   2026-10-19 image (removed kwargs `delta_lake` / `use_hive` / `tenant_name` /
   `connector` make Spark/Trino calls raise `TypeError`); worked around
   in-process only, by `albert-load-artifacts/kbu_compat.py`.
2. `kbaseincubator.council` is **not bootstrapped** on kbhub — there is nothing
   to append to.
3. kbhub is **not a Maestro or AgentForge worker**, so no worker can reach it.
   By design, not oversight.

It closes on a **recorded** kbhub smoke test that reads and appends to a
*disposable* council namespace with the **deployed** package version, plus the
scanner's Jupyter startup registration. Albert owns both. This rehearsal's job
was to make that operator step boring, and the 387-line report is what it
hands over.

**Production clearinghouse rows written: 0.**

---

## Verification

### The targeted selection, base vs branch

Rule 7: never the full suite (the h100 baseline measures it at 45–57 min,
5.7× the 600 s sub-agent Bash ceiling). The base was checked out into a
**detached** worktree at `/scratch/chenry/t11-base-950602e`
(`git worktree add --detach`, no branch created, non-Dropbox path) so both sides
used the same interpreter and the same selection. Tranche 3's 20-file selection
plus the three files tranche 3 added, 23 files; interpreter
`/home/chenry/venvs/kbdl/bin/python` (cpython 3.11.15, pytest 9.1.1).

| side | sha | result | exit |
|---|---|---|---|
| base | `950602e` (detached worktree) | **1 failed, 1287 passed, 3 skipped** | 1 |
| branch | `be58982` + `test_council_rehearsal.py` | **1 failed, 1309 passed, 3 skipped** | 1 |

```
$ python -m pytest $(cat selection) -q -p no:cacheprovider --no-header
```

**Sorted FAILED node-id diff: EMPTY.** The same single nodeid on both sides:

```
tests/domains/ai/council/test_store_identity.py::TestFreshStoreHasNoIdentity::test_the_refusal_exits_non_zero_in_a_real_subprocess
```

That is a **pre-existing** failure present on the base commit, which this item
neither caused nor touched. The +22 passed is exactly the new test file. Nothing
that passed on the base fails on the branch.

The new file alone: `pytest tests/berdl/test_council_rehearsal.py -q` →
**22 passed in 2.19 s**, exit 0.

Also run clean, all exit 0: `test_council_delivery.py` +
`test_council_archive.py` + `test_council_bundle.py` → 175 passed;
`test_council_publish_lakehouse.py` + `test_council_seed.py` +
`test_council_disposition.py` + `test_ontomap_cli.py` → 196 passed, 3 skipped.

The six known machine-local
`tests/domains/ai/council/test_types_config.py::TestPackagedDefaultConfig`
failures are not in this selection and were not touched.

### The rehearsal run itself

```
$ python scripts/council/acceptance_rehearsal.py --work-root /scratch/chenry/t11-rehearsal ...
snapshot sha256: 37535e28382fd3323af9eab2fa0a6f4612d0852856c650cf6cf42740e9bcef32
VERDICT: ASSERTED  (10 asserted, 0 failed, 0 not asserted; 131/131 assertions held)
exit 0
```

Per step: 0 → 7/7, 1 → 5/5, 2 → 18/18, 3 → 21/21, 4 → 9/9, 5 → 15/15,
6 → 26/26, 7 → 11/11, 8 → 8/8, 9 → 11/11. 211.31 s wall.

The snapshot digest is **verified before use**, and the script refuses with
exit 2 if it does not match — `/scratch` is ephemeral and not backed up, so a
re-decompressed-from-elsewhere or truncated file would archive 203,821 rows
keyed on a digest that does not describe them.

### A real failure this run had, and what was changed because of it

The **first** complete run died in `render_report` with
`TypeError: keys must be str … not tuple` — step 3 built its per-role live split
as a dict keyed on `(role, live)`. Every step had already passed; the whole
measurement was lost because the renderer crashed after it. Two changes:

1. the per-role keys are now strings (`"critic/live=1"`), and `_fmt` falls back
   to `repr` on a `TypeError` so no formatting failure can ever lose a run
   again;
2. **the JSON report is written BEFORE the text is rendered**
   (`scripts/council/acceptance_rehearsal.py:154`). The measurement is the
   valuable artifact; the text is a view of it. The comment there says why.

Reported because it is exactly the kind of thing that gets quietly tidied away:
the first run of the deliverable failed, and the ordering fix is a direct
consequence.

---

## Divergences reconciled: what the prompt claimed vs what the tree held

| the prompt said | the tree holds | what I did |
|---|---|---|
| `council_bundle.py` at `genome/ontology_mapping/council_bundle.py:464` | `src/kbutillib/domains/genome/ontology_mapping/council_bundle.py` — a `domains/` segment the prompt omits throughout | used the real path; same for every `berdl/*` path, which is `domains/kbase/berdl/*` |
| `SqliteCouncilStore.assign_store_identity` (store/sqlite.py:2741) | the class is **`SqliteStore`**; the method is at that line | used `SqliteStore`. `SqliteCouncilStore` does not exist |
| `SqliteCouncilStore.snapshot()` (sqlite.py:2936) | `SqliteStore.snapshot`, line correct | used it |
| step 0: the run directory's location "is NOT recorded anywhere I could find", `/scratch` is ephemeral so it may be gone → mark 5, 6, 7 NOT ASSERTED | it is at **`/scratch/chenry/xport.Iv9Tft`**, complete, with both record files and all 760 abstention texts | took the FOUND branch. Steps 5, 6 and 7 were fully rehearsed. The NOT-FOUND branch is still implemented (`_needs_run_dir`) for the day the directory is gone |
| fact A: "LIVE CALLS ARE 88,465, **NOT** 82,270 … the PRD's 82,270 does not match this snapshot" → name it as a disagreement | 88,465 is `live=1` over all 21 jobs; 82,270 is `live=1` over the 12 published jobs, **exactly**. Both correct | asserted **both** by direct query and reported the finding as a *resolution* rather than a disagreement |
| fact B: at the default floor it "REFUSES **and names** the eight" | it refuses and **counts** them; `join_assertions`' report carries no key list | asserted the refusal and the count; the rehearsal computes the names itself; raised the naming gap as a finding |
| fact B: "19,276 submitted … coverage 0.999585" | 20,036 submitted (19,276 fanout + 760 abstentions), coverage 20,028/20,036 = **0.999601** | asserted the measured values; finding explains that the header's figures omit the abstentions on both sides. **8 unresolved is unchanged** |
| taskplan: "19,268 assertions with zero rows from cancelled or pilot jobs" | **20,028** rows — 19,268 decided + 760 abstentions, which header fact C itself requires | asserted 20,028 broken down as 19,268 + 760, and asserted the zero-cancelled-or-pilot property on the decided rows |
| fact C: "If you cannot source a text for some abstention, report those query_ids" | all 760 abstain rows carry `text` | asserted `abstentions_without_text == []`; nothing to report |
| `council_archive.require_complete_delivery :1277, reconcile_delivery :1211, current_projection :1356` | all three at exactly those lines | used as given |
| `publish_bundle` / `read_lakehouse_bundle` assumed `state_root=` / `store_id=` | `publish_bundle(..., archive_state_root=)`, `read_lakehouse_bundle(sink, campaign_id)` positional | used the real signatures |

### A clause I did not need, and one I did not honour

The prompt's step-0 NOT-FOUND branch was a conditional stop-and-report. The
condition did not hold, so it did not fire. The branch is implemented anyway.

Rule 2 told me to verify the decompressed snapshot's digest before using it. I
did (`37535e28…`, matching), **and** I built that verification into the script
so every future run does it too — the prompt only asked for it once.

---

## Files touched

| file | lines | what |
|---|---|---|
| `src/kbutillib/domains/kbase/berdl/council_rehearsal.py` | +2,492 (new) | the ten steps, the three-state report model, the scratch refusals, the in-pod leg |
| `scripts/council/acceptance_rehearsal.py` | +184 (new) | the one command: arguments, snapshot digest verification, output, exit codes |
| `tests/berdl/test_council_rehearsal.py` | +206 (new) | 22 tests for the target's own logic. **No snapshot needed**; in the default suite |
| `agent-io/reports/2026-10-26-…-rehearsal.txt` | 387 lines | the operator's view of the real run |
| `agent-io/reports/2026-10-26-…-rehearsal.json` | 108 KB | the measurement |

**Nothing in tranches 1–3 was modified.** `git diff --stat 950602e..HEAD` is
additions only. None of rule 9's forbidden files — `interfaces/cli/kbdl_index.py`,
`koros_arc_store/`, `clearinghouse_manifest.py`, `clearinghouse_shard.py`,
`backfills/*.toml`, `transports.py` — was touched.

The 896 MB snapshot and every derived copy stay under `/scratch/chenry/` and are
not committed.

---

## What the next operator should do with this

1. **Correct the taskplan at source** on the four reconciled figures: 82,270 is
   the published campaign's live calls and is *correct*; the submitted inventory
   is 20,036 not 19,276; the published row count is 20,028 not 19,268; coverage
   is 0.999601 not 0.999585. The eight unresolved are unchanged.
2. **Record `/scratch/chenry/xport.Iv9Tft` somewhere durable.** `/scratch` is
   ephemeral and not backed up, and that directory is the only copy of the
   campaign's `abstain.jsonl` — 760 function texts that, once published empty
   into a frozen clearinghouse slot, cannot be corrected. Its `fanout.jsonl` is
   likewise the only `query_id -> item_id` map. Both are small (2.2 MB together)
   and belong next to the snapshot in `~/Dropbox/Jobs/function-to-go/`.
3. **Decide on the two tranche-1–3 findings**: an `unresolved_sample` in
   `join_assertions`' report so the default-floor refusal names the eight, and a
   `--abstain-texts` flag on `kbu ontomap council-publish-lakehouse` so an
   operator can supply what `publish_from_lakehouse` already accepts.
4. **The live leg is Albert's.** The rehearsal has made the operator step
   boring: the report says exactly what the producer half establishes, and the
   in-pod load is the only thing left that nothing here can claim.
