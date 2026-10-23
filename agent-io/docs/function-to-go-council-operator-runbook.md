# function_to_go Council Pipeline -- Operator Runbook

**PRD**: `function-to-go-council-pipeline-v1`
(`agent-io/prds/function-to-go-council-pipeline-v1/fullprompt.md`, committed on
whatever branch `/ai-design` ran against; it may not be reachable from an
off-`wip` worktree -- treat this document as the authoritative operator
reference).

**What this pipeline does**: assigns GO molecular-function terms to the
clearinghouse's protein function descriptions, with an AI council, under a
standing quote and a human approval. Five stages, each a verb of
`kbu ontomap`:

| Stage | Verb | Spends | Runs where |
|---|---|---|---|
| 1 PLAN | `council-plan` | nothing | anywhere (reads the clearinghouse off-pod) |
| 2 QUOTE | `council-quote` | nothing | anywhere |
| 3 APPROVE | `council-approve` | nothing | anywhere |
| 4 RUN | `council-run` | **LLM tokens -- the only stage that does** | h100 (ARGO + MAG reachable) |
| 5 PUBLISH | `council-export` + `council-publish` | nothing | anywhere |
| 5 (consumer) | `clearinghouse load` + `verify` | nothing | **kbhub, IN-POD ONLY** |

`council-status` sits beside stage 4 and spends nothing. `kbu ontomap version`
reports the retrieval backend's identity and is free.

**Be honest about what has and has not been proven.** The whole pipeline has
been rehearsed end to end against ALREADY-STORED verdicts with zero token
spend (`tests/domains/test_ontomap_pipeline_rehearsal.py`), including ten
named failure cases -- marker before payload, a missing file, a wrong hash, a
0% join, a partial join, a duplicate consumer, a crash after the commit, an
expired approval, a plan changed after approval, and a sustained rate limit.
All ten behaved as designed. That proves the stage-to-stage file contract, the
refusals and the consumer's gates. It does **not** prove a live council pass: no run has
been executed through these verbs, and the one completed 19,276-item run
(2026-10-20) was hand-driven and bypassed them. Stage 4 against real gateways
is still a first.

---

## The constants, and why each one is what it is

**Do not change these without measuring.** Every number below was paid for.

### MAG: queues summing to 6

MAG's limits are **ACCOUNT-WIDE**, and **both** of them bind:

- `max_parallel_requests` 20
- **120 requests per minute**

A config asking **24 parallel errored 427 of 500 pilot items** across 1,925
transport attempts. The configuration measured to work is **MAG queues summing
to 6**. The per-minute ceiling is the one people forget: 20 parallel slots
cycling faster than 6 s/request exceeds 120 rpm, so the parallel cap alone
does not keep you inside the account's budget.

Declared in the council config's `queues` table; the plan records the live
table as `concurrency.declared_queues` so you can see whether the config this
run will execute under matches the shape the plan assumed.

### ARGO: chair 24, critic 8, meta 8

ARGO **saturates at ~24 concurrent** and showed **zero rate limiting at 40
concurrent across three models**. So the chair (proposer_1) gets 24 and the
two single-seat roles get 8 each -- ARGO is not the constraint, and giving the
critic and meta more would only deepen a queue in front of a gateway that is
not the bottleneck.

**The ARGO endpoint's own `max_concurrency` must therefore be at least 40, not
24.** `CouncilConfig`'s validator refuses an account whose queues
over-subscribe its cap, so `24 + 8 + 8 = 40` against `max_concurrency: 24`
will not load at all -- with a message naming the three queues and the sum.
That is the same measured fact from the other side: 40 is the declared account
cap (zero rate limiting observed there) and 24 is the chair's share of it. If
you see

```
account 'argo-anl' (max_concurrency 24) is over-subscribed: queues
['argo_chair', 'argo_critic', 'argo_meta'] sum to concurrency 40
```

raise the ENDPOINT's cap to 40; do not lower the chair.

### ~2,000-item chunks, ONE chunk in flight

**This is a WORKAROUND for [KBUtilLib issue 84](https://github.com/cshenry/KBUtilLib/issues/84), not a design.**

The council store's `claim_calls` is **synchronous on the worker event loop**
and its cost is **linear in the pending set**: measured **217 ms at 19k
pending rows against 10.4 ms at 2k**. That inflated service times **6-9x on
BOTH gateways at once** -- with queue wait at **0.00**, which is how it was
diagnosed as claim cost and not as a gateway problem -- and **collapsed
throughput 10.8x**.

~2,000-item chunks keep the pending set small. **ONE chunk in flight is
load-bearing, not caution**: the daemon drains every claimable job, so
submitting all ten chunks up front would rebuild exactly the pending set the
chunking exists to avoid.

The rationale travels with the number, in three places, so that this becomes
removable rather than permanent: `PLAN_CHUNK_SIZE` /
`PLAN_CHUNKS_IN_FLIGHT` in
`src/kbutillib/domains/genome/ontology_mapping/ontology_mapping_utils.py`, the
plan's own `concurrency` block (`_plan_concurrency`, which names issue 84 and
quotes the measurement), and `RUN.json`'s `chunks_in_flight_issue`.

**When issue 84 is fixed, all three change.** Until then, do not raise
`--chunk-size` and do not run two chunks at once.

### The other measured facts this pipeline rests on

- A completed run: **19,276 items submitted, 19,268 verdicts (99.96%),
  199.2M tokens (ARGO 106.97M + MAG 92.21M), 10,334 tokens/item, 9.46 h wall
  clock, 0.59% error rate.**
- **Meta fires on 24.1% of items** (n=19,268). The earlier 16.7% (n=6),
  19.2% (n=73) and 38.7% (n=475) figures are **WITHDRAWN** -- do not quote
  them.
- **Input is 86% of all tokens.** Prompt size, not completion length, is what
  this costs.
- Per seat per item, measured input/output: proposer_1 ARGO gemini35flash
  2,226/113; proposer_2 MAG gpt-5.6-sol 2,129/219; proposer_3 MAG
  gpt-5.6-luna 2,129/292; critic ARGO claudesonnet5 1,416/629; meta ARGO
  claudeopus5 2,932/493.
- The retry pass: `invalid_output` is **NOT** in the worker's
  `RETRYABLE_KINDS` (only `transport` and `timeout`) and **ARGO truncates
  silently with no length finish-reason**, so a cut-off reply is abandoned on
  its first attempt. **113 items failed that way; 105 came back on a plain
  retry, for 0.30% of run cost.** This is why the retry pass is a first-class
  stage and not an optional extra.
- The join key into the clearinghouse is **`query_id`** -- the clearinghouse
  `entity_hash`, carried through from the ranked file. The council's own
  `items.entity_hash` matched **0 of 19,268** clearinghouse hashes; the
  definition **TEXT** matched **19,268 of 19,268**. **Never join on text, and
  never use the council's own `entity_hash` as the key.** The one join that
  would appear to work is the one join this pipeline must never make.
- The clearinghouse **READS work from h100 off-pod** over the BERDL delta API
  (measured 2026-10-22). Only `load` and `verify` are in-pod;
  `_require_in_pod_or_refuse` guards exactly those two. `shard` runs anywhere.
- Current clearinghouse state: `function_entity` 47,341 rows,
  `function_content` 47,341 rows, `function_result` **0** rows (partitioned by
  `source`).

### `bench_fingerprint`, NOT `roster_fingerprint`

The clearinghouse parameter set is
`{query: "<name>@<version>", bench_fingerprint, mapping_hash}`.

**`bench_fingerprint` and not `roster_fingerprint`**: the bench (the MODELS)
partitions the corpus; the transport does not. `Roster.fingerprint()` covers
the endpoint kind, the route spelling, the token ceiling, the temperature and
the extras, so **moving one seat from one gateway to another would move it** --
which would re-partition all 47,341 functions and re-charge ~199M tokens for
a pure cost optimisation. `roster_fingerprint` and the per-seat
`route_fingerprint` are still RECORDED in the plan, as provenance. They are
not keys.

Consequence for stage 3: a **MODEL** swap after approval is refused by stage
4's gate; **RE-ROUTING** the same models to another gateway is permitted and
only makes the quoted PRICE stale (reported as a warning).

---

## Before you start: the credentials nothing fails loudly about

**`ARGO_USER` and the AMSC token (`~/.config/amsc/token`) are exported in NO
shell on this host.** A council started without them **does not fail loudly**:
the seats report unavailable and the calls PARK. A 9-hour run that spent
nothing and produced nothing looks exactly like a slow one.

`council-run` resolves both before it spends the first token and refuses by
name (`argo_user`, `amsc_token`) if either is missing. **That refusal is the
whole reason the old `kbu-env.sh` wrapper is retired** -- do not reintroduce a
wrapper that exports them, because then a missing credential is silent again.

```bash
# Check, do not assume:
echo "ARGO_USER=${ARGO_USER:-<UNSET>}"
test -s ~/.config/amsc/token && echo "AMSC token present" || echo "AMSC token MISSING"
```

---

## Stage 1 -- PLAN: detect the un-run functions

```bash
RUN_DIR=~/Dropbox/Projects/KBUtilLib/runs/function-to-go/$(date +%Y-%m-%d)-f2go
kbu ontomap council-plan --run-dir "$RUN_DIR" --query go_mf@1 --json
```

Writes **`PLAN.json`, and only `PLAN.json`, into the run directory.** The
ranked file, the items file and the abstain/fanout records go to local
scratch (`$KBU_SCRATCH_DIR`, else `/scratch/<user>`): the items of a 19k-item
plan are ~130 MB of evidence and menus (6.9 kB each, measured), nothing
off-machine reads them, and every byte on the Dropbox surface needs an
integrity gate.

**"Un-run" means no current-state `function_result` row at this run's
`parameter_set_hash`.** A row at a DIFFERENT parameter set is a different
experiment and leaves the function un-run.

Read the `counts` block before going on:

- `universe` -- every function the clearinghouse knows (expect ~47,341).
- `done` -- current-state rows at THIS parameter set.
- `unrun` = `universe - done`.
- `submit` -- distinct definition TEXTS retrieval did not decline. This is
  what you pay for.
- `abstain` -- un-run functions retrieval declined (placeholders, or nothing
  survived the GO height filter). **They are published as
  `decline_reason=abstained:*`, never dropped.** Dropping them is what made
  760 definitions look like an unexplained coverage gap after 2026-10-20.
- `fanout` -- the `query_id -> item_id` map. A text shared by three functions
  is ONE paid answer and THREE published rows.

A plan with `submit = 0` is warned about and stage 2 refuses to quote it.

## Stage 2 -- QUOTE: price it before anything is spent

```bash
kbu ontomap council-quote --plan "$RUN_DIR" --json
```

Writes `QUOTE.json` into the run directory, carrying the `plan_hash` stage 3
binds to. Proposer volume and reuse are **counted, not estimated** (every
prompt is rendered through the worker's own renderer and looked up in the
store by `prompt_hash`); critic volume is exact and its reuse derived; meta is
projected at the measured **24.1%** fire rate; the retry pass is its own line
at the measured **0.30%**.

Every projected seat and every unpriced gateway comes back as a warning. A
quote with warnings is not wrong -- it is a quote telling you which of its
numbers are not measurements.

## Stage 3 -- APPROVE: a human says yes to one quote

```bash
kbu ontomap council-approve "$RUN_DIR" --note "why this run" --json
```

**`APPROVED.json` IS AN AUDIT TRAIL AND A BINDING CEILING. IT IS NOT AN
AUTHENTICATION BOUNDARY.** It is unsigned and cannot be signed: there is no
key material on these machines, and an agent can write the file as easily as
you can. `--approver` is a LABEL.

What it does buy: an explicit per-gateway token ceiling for stage 4 to halt
at, a checkable binding to this plan and this quote, and a refusal naming the
check when the job has changed underneath it.

**The ceiling defaults to the quote's UPPER band x 1.25, not to its point
estimate.** A ceiling at the mean halts about half of all runs partway, and a
run halted at 60% has spent 60% of the tokens for none of the result. The
headroom also covers the retry pass, which is charged after this file is
written.

**It expires in 7 days by default.** An approval with no expiry would be a
standing authorisation to spend.

Stage 4's gate makes six checks and always names the one that failed:
`approval_present`, `quote_hash`, `plan_hash`, `bench_fingerprint`,
`expires_at`, `consumed`.

## Stage 4 -- RUN: the only stage that spends

### Launch it so it outlives your session

**`council-run` RUNS IN THE FOREGROUND and detaching it is yours to do.** On
this host:

```bash
systemd-run --user --scope --collect \
    kbu ontomap council-run --run-dir "$RUN_DIR" --json \
    > "$RUN_DIR/run.log" 2>&1
```

**`setsid`, `nohup` and `disown` DO NOT WORK here.** None of them escapes the
launching unit's cgroup, and systemd kills everything in that cgroup when the
unit goes away -- **without a trace in the run's own log**. A 9-hour run lost
that way looks identical to a crash. This is not a preference; it is the only
form that survives.

### What it does, in order

1. The approval gate (six checks), then the credentials (`argo_user`,
   `amsc_token`). It refuses before the first token, naming the check.
2. A fresh budget epoch per gateway endpoint -- on a FIRST run only, never on
   a resume: the approved ceiling covers the whole run, so what a resumed run
   already spent counts against it.
3. **The chunks, ONE at a time, in plan order**, behind `.job`/`.done`
   markers. The `.job` marker is written BEFORE the chunk is drained and names
   the council job; the `.done` marker only once that job has settled. So a
   resume after a killed chunk re-executes **only that chunk, against the job
   it already submitted** -- not a duplicate submission of the same 2,000
   items.
4. **The retry pass, as a DECLARED stage.** The run is NOT reported complete
   until it has run or been declared unnecessary. Capped at 2 rounds.
5. `RUN.json`, carrying the ceiling, the spend per gateway, every chunk row
   and the halt if there was one.

### Watching it

```bash
kbu ontomap council-status --runs-root "$(dirname "$RUN_DIR")" --json
```

### Stopping it

```bash
touch "$RUN_DIR/STOP"        # stops at the next supervisor pass (~5 s)
```

A file and not a signal, because the operator who needs it is usually on
another terminal -- or another machine, through the synced run directory --
from the one holding the foreground run.

### When it halts

It halts on the approved per-gateway **ceiling**, on a **gateway halt**
(including a **sustained rate limit**, which the worker records as a halt on
the endpoint), on an **operator stop**, and on a **chunk timeout** (6 h per
chunk, so one wedged chunk cannot hold the rest of the run). Each halt is
recorded on every endpoint of the run -- not only the offending gateway's,
because leaving the other gateway claiming would spend its own ceiling
finishing items whose other seats can no longer answer.

```bash
kbu council endpoints reset-halt <endpoint>     # clear the halt
kbu council endpoints reset-budget <endpoint>   # also needed for a CEILING halt
kbu ontomap council-run --run-dir "$RUN_DIR"    # resume; .done markers are honoured
```

**A halted run is work that has been PAID FOR and has not LANDED.**
`council-status` counts it in `running` and in `paid_for_not_landed`, and
warns with the halt's reason. Do not leave it there: resume it, or publish
what it got.

## Stage 5 -- PUBLISH (producer half, runs here)

```bash
kbu ontomap council-export  --run-dir "$RUN_DIR" --json
kbu ontomap council-publish --run-dir "$RUN_DIR" --json
```

`council-export` writes **one parquet row per input `query_id`**, on LOCAL
SCRATCH (a path inside the synced run directory is refused -- the published
artifact is the shard run, and this file is its input). All three decline
classes get a row with `go_ids = []`: `abstained:<reason>`,
`no_candidate_matched`, `contentless`. **Recording them is what stops stage 1
re-selecting them forever**, because un-run means "no current-state result
row" and an ABSENT row is indistinguishable from "not yet run". An item whose
every call failed is WITHHELD and warned about: it is genuinely un-run and the
next plan must re-select it.

`council-publish` renders the shipped `function-to-go-council` manifest preset
into `<run-dir>/MANIFEST.toml` with this run's query reference, bench
fingerprint and mapping hash substituted; gates it through
`kbu clearinghouse plan`'s own validator; compares the rendered
`parameter_set_hash` against the one `PLAN.json` recorded (a mismatch, or the
documented inert un-rendered hash, refuses **before a single row is
written**); shards to `<run-dir>/shard`; and checks the inventory back. Then it
writes `PUBLISH.json` -- the ITEM accounting the load receipt reconciles
against.

**`<run-dir>/shard/run.json` is the completeness contract**, carrying a
sha256, a row count, a byte count and a `[hash_min, hash_max]` range **per
batch**. There is no second manifest format and there must not be: two
descriptions of one directory could disagree, and the one a load trusted
would be whichever it happened to read.

Dropbox then syncs `<run-dir>/` to kbhub.

## Stage 5 -- the CONSUMER half: what Albert runs on kbhub

**IN-POD ONLY.** `load` and `verify` write through (or read back) a Spark
session that exists only inside the BERDL JupyterHub pod.
`_require_in_pod_or_refuse` guards exactly these two verbs and names the locus
as the reason.

### THE TRIGGER CONDITION

> **A `run.json` exists AND every batch it lists is present and
> sha256-matches.**

**Never "a `run.json` exists".** A sync client does not guarantee the
producer's write order, so the marker can arrive before its payload -- and a
load triggered on the marker alone reads a directory whose batches are
missing or half-written. `load` itself defends this (a missing or mismatched
batch is treated as still-SYNCING until `--transfer-deadline-hours`, 6 by
default, measured from `run.json`'s own mtime, after which it is reported as a
**FAILED DELIVERY** naming the batch and the expected versus observed digest),
but **the trigger must not depend on that defence**: a load fired on an empty
directory burns a pod session and tells you nothing you could not have checked
in a second.

```bash
SHARD=<run-dir>/shard

# 1. The gate. Both halves, every time.
test -f "$SHARD/run.json" || { echo "no marker yet"; exit 1; }
python3 - "$SHARD" <<'EOF'
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
run = json.loads((root / "run.json").read_text())
bad = []
for entry in run["batches"]:
    path = root / entry["file"]
    if not path.is_file():
        bad.append(f"{entry['batch_id']}: ABSENT ({entry['file']})"); continue
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != entry["sha256"]:
        bad.append(f"{entry['batch_id']}: sha256 {digest} != {entry['sha256']}")
print("\n".join(bad) if bad else f"{len(run['batches'])} batch(es) verified")
sys.exit(1 if bad else 0)
EOF
```

A failure here is **not** a reason to load anyway. It is either sync lag (wait,
then re-check) or a lost file (re-sync the shard directory from the producer,
or re-shard). Either way, **do not load.**

### Then, and only then, the three commands in order

```bash
# 2. DRY RUN -- the last gate. Genuinely read-only: no rows, no ledger line,
#    no execution. Reports the action each shard would take and runs the
#    pre-write assertions.
kbu clearinghouse load "$SHARD" --dry-run --json

# 3. THE REAL LOAD. Fill-only, idempotent, ledger-resumable. Note the
#    EXECUTION id it prints -- verify needs it, and so does a resume.
kbu clearinghouse load "$SHARD" --json

# 4. VERIFY, and write the receipt back onto the synced surface.
kbu clearinghouse verify "$SHARD" \
    --run-id <execution id from step 3> \
    --receipt <run-dir>/LOADED.json --json
```

**Read the dry run before step 3.** The four pre-commit assertions, each
reporting a count, are evaluated on the INPUTS: the expected input is
non-empty; every join key is non-null; each target slot is claimed by at most
one source row; and the share of source keys resolving against
`function_entity` is at or above `--coverage-floor` (default **1.0**).

- **A non-empty batch resolving ZERO keys exits non-zero whatever the floor
  says.** That is the 0-of-19,268 wrong-join-key case, and `--coverage-floor 0`
  is not a way to wave it through: a floor expresses tolerance for a PARTIAL
  mismatch, and total non-resolution is not partial.
- Lower the floor **only** for a deliberately partial load, and say so in the
  reply. "No surviving rows" is right for a re-run and wrong for a wrong join
  key, and without these assertions the two are the same envelope and the same
  exit status.

**`--run-id` names the EXECUTION, not the shard run.** One shard run can be
loaded more than once (scratch, then production) and each load is proven by
its own per-batch counts. A **duplicate consumer** loading the same shard run
twice is therefore safe and cheap, not corrupting: a per-namespace lock
excludes a concurrent ingest, every batch is left-anti-joined against the keys
the target already holds, and a batch with zero survivors is committed
**without calling `load()` at all**.

**If the pod dies between the commit and the receipt**, the rows are landed
and `LOADED.json` is missing. **Re-export the receipt; do not reload.** The
receipt is derived entirely from the ledger, the execution's inventory, the
target's snapshot history and the producer's `PUBLISH.json`, with no timestamp
and no nonce in its body, so re-running `verify --receipt` reproduces the
SAME receipt. `kbu ontomap council-status` detects this shape directly -- a
run in `exported-not-loaded` whose parameter set already has rows in the
clearinghouse -- and says so.

**Resuming a killed load**: `kbu clearinghouse load "$SHARD" --run-id <id>`,
adding `--reconcile` to decide a batch the kill left mid-append. Resumability
comes from the ledger under `$KBU_CLEARINGHOUSE_STATE_DIR`, which lives with
the EXECUTION and not with the shards -- the shard directory is sharder output
and may be regenerated or deleted; the ledger is the only record of what
reached the lake.

### Reply with

The dry-run envelope, the load envelope (including the execution id), the
verify envelope, and the `LOADED.json` the verify wrote. Write them to
`~/Dropbox/Projects/AIAssistant/trigger-inbox/replies/`.

---

## `council-status`: the verb that makes stale work visible

```bash
kbu ontomap council-status --runs-root ~/Dropbox/Projects/KBUtilLib/runs/function-to-go --json
```

**A corpus under a versioned transform is ALWAYS partly stale, so one coverage
number is the wrong answer.** This reports two.

**BY LIFECYCLE** -- `un-run`, `planned-not-quoted`, `quoted-not-approved`,
`approved-not-run`, `running`, `exported-not-loaded`, `loaded`. The buckets
are a PARTITION of the corpus and they add up.

The **middle-late states are the ones that go missing.** Work that has been
PAID FOR and has not LANDED is invisible in both a "what is un-run" count (the
plan took it out of the un-run set) and a "what is loaded" count (nothing
landed) -- and that is exactly what a crashed run leaves behind.
`paid_for_not_landed` is that number: `running` plus `exported-not-loaded`.
**If it is non-zero, something is owed.**

**BY `parameter_set_hash`** -- how much of the corpus is current, and how much
is covered ONLY by an older parameter set. **Partial-version coverage is
TOLERATED, NOT NORMALISED AWAY**: re-running all 47,341 functions on every
bench, query-version or `mapping_hash` bump is unaffordable. So it is
REPORTED, because "is this dataset consistent?" is unanswerable otherwise.
`covered_only_by_an_older_parameter_set` is the number stage 1 will re-select.

**IT SURFACES A SECOND CONCURRENT RUN**, and you must act on that one. Two run
directories both in stage 4 interleave their chunks through the one daemon and
**defeat the chunking entirely** -- see issue 84 above. Neither run can see the
other; this verb can. Remedy: `touch <the other run dir>/STOP`.

The lifecycle state is read off the FILES -- `PLAN.json`, `QUOTE.json`,
`APPROVED.json`, `RUN.json`, `PUBLISH.json`, `LOADED.json` -- so there is no
seventh status file that could disagree with the six. `loaded` comes from the
CLEARINGHOUSE and not from the receipts, which is what catches the
crash-after-commit case above.

---

## THIS PRD SUPERSEDES THE SCRATCH SCRIPTS -- remove them

The 2026-10-20 run was hand-driven through throwaway scripts under
`/scratch/chenry/council/`. **Every one of them is superseded, and they should
be REMOVED rather than left as a second divergent path.** A second path is a
second thing that can disagree with the library, and the one an operator
trusted would be whichever they happened to find.

| Script | Superseded by | Why the script is worse |
|---|---|---|
| `export_results.py` | `kbu ontomap council-export` | Dropped the abstained set, which is how 760 definitions became an unexplained coverage gap. The verb publishes all three decline classes. |
| `_adp1_prep.py` | `kbu ontomap council-plan` | Hand-built an item set with no `parameter_set_hash`, no plan hash and no chunk boundaries, so nothing downstream could be bound to it. |
| `run_chunks.sh` | `kbu ontomap council-run` | Chunked by shell loop with no `.job`/`.done` markers, so a killed chunk re-submitted its items; no approval gate, no ceiling, no retry pass. |
| `chunk_progress.sh` | `kbu ontomap council-status` | Counted one number. It could not see paid-for-not-landed work, the parameter-set split, or a second concurrent run. |
| `kbu-env.sh` | `council-run`'s own credential resolution | **Exporting the credentials is the problem, not the fix.** A wrapper makes a missing `ARGO_USER` or AMSC token silent again; the verb resolves what it needs and refuses by name. |

```bash
# After this runbook's stages have been exercised once for real:
rm -rf /scratch/chenry/council/export_results.py \
       /scratch/chenry/council/_adp1_prep.py \
       /scratch/chenry/council/run_chunks.sh \
       /scratch/chenry/council/chunk_progress.sh \
       /scratch/chenry/council/kbu-env.sh
```

(`/scratch` is ephemeral on h100 in any case -- but "it will be cleaned up
eventually" is how a divergent path survives a year.)

---

## If something goes wrong

| Symptom | Where to look |
|---|---|
| `council-run` refuses naming a check | The six approval checks and the two credential checks all name themselves. `expires_at` -> re-approve. `plan_hash` -> the plan was re-derived after quoting; re-quote and re-approve. `bench_fingerprint` -> a MODEL changed; re-plan. `consumed` -> a different run spent this approval; use a fresh run directory. |
| Seats "unavailable", calls parked, nothing spent | `ARGO_USER` or the AMSC token. See the credentials section -- this is the failure that is not loud. |
| Throughput collapsed, queue wait 0.00, both gateways slow | Issue 84's claim cost. Check `council-status` for a SECOND RUN in stage 4, and check that `--chunk-size` was not raised. |
| MAG erroring most items | The account-wide limits. Queues must sum to 6. 24 parallel errored 427 of 500. |
| Items with no verdict after a complete run | The retry pass over `invalid_output`. Check `RUN.json`'s `retry` block: `ran`, `rounds`, `declared_unnecessary`. |
| `load` reports zero surviving rows | Read the pre-commit assertions, not the row count. Zero resolution = wrong join key (`query_id`, never text, never the council's `entity_hash`). Zero survivors with full resolution = a re-run of a landed run, which is correct and cheap. |
| `load` says FAILED DELIVERY | A lost file, not sync lag: the deadline from `run.json`'s mtime has passed. Re-sync the shard directory from the producer, or re-shard. |
| Rows landed, no `LOADED.json` | Crash between the commit and the receipt. `verify --receipt` re-exports it. **Do not reload.** |
| `council-status` shows `paid_for_not_landed` > 0 | A run is halted, draining, or published-but-unloaded. The per-run rows and warnings say which. |
