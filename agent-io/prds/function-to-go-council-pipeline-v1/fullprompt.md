# function-to-go-council-pipeline-v1

## Research Report

### Important findings

**1. Four of the five stages already exist in KBUtilLib, and the completed run
bypassed all of them.** This is the finding that sets the shape of the whole PRD.
Every defect the 2026-10-20/21 run hit is a consequence of the bypass, not of a
missing capability:


| what went wrong on 2026-10-20                                                                                 | what the bypassed API already does                                                                                                               |
| ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| council `items.entity_hash` matched 0 of 19,268 clearinghouse hashes; recovered by joining on definition text | `function_to_go_council` carries `query_id` — the clearinghouse hash — on every output row (`ontology_mapping_utils.py:231`)                     |
| 760 priority definitions "never submitted", cause unexplained, flagged to Albert as an open gap               | they are the retrieval stage's `abstained` rows. `function_to_go_council` emits them with `decline_reason="abstained"` rather than dropping them |
| a hand-written SQL export on `/scratch`, the only record of how verdicts became a file                        | `write_council()` already writes council output as gzipped JSONL with an atomic `.partial` → `replace`                                           |
| three output files + a hand-written `MANIFEST.json`, one of which did not reach the consumer                  | `kbu clearinghouse shard` publishes bronze parquet plus a `run.json` carrying per-batch sha256, row count and hash range                         |


**2. The 760-definition coverage gap is SOLVED, by measurement, in this session.**
`agent-io/.../2026-10-20-...md` round 9 flagged 760 definitions in the priority
list that were never submitted, said the cause predated the session, and routed
it to Albert as an unexplained gap. It is not unexplained. Measured here against
`ranked.jsonl.gz` and `priority_adp1_pmi.tsv`:

```
priority rows 22,255 | distinct entity_hash 20,036 | distinct definition 20,036
rows in priority present in ranked : 20,036   (0 missing)
abstained                          :    760
distinct texts, non-abstained      : 19,276   <- exactly what was submitted
distinct texts, abstained          :    760
abstained-only texts               :    760   (disjoint from the submitted set)
```

20,036 = 19,276 + 760, exactly. `function_to_go` abstains on placeholder
functions and on any description where no candidate survives the GO height
filter, and `function_to_go_council` does not send an abstained row to the
council. So the 760 were correctly never spent on — and would have appeared in
the output as declines had the API been used. **Nothing is missing and nobody
needs to investigate it.**

**3. The clearinghouse is READABLE from h100. Stage 1 does not need kbhub.**
Measured today, 2026-10-22 12:03 CDT, from the `~/venvs/kbdl` interpreter:
`kbu clearinghouse tables` returned the full table list over
`https://hub.berdl.kbase.us/apis/mcp/delta`, with sixteen successful queries.
Only `load` and `verify` are in-pod — `_require_in_pod_or_refuse` guards exactly
those, and the `shard` docstring says sharding "runs anywhere — sharding needs
CPU and disk, no pod and no credentials."

Current table state, same read:

```
function_entity   47,341 rows
function_content  47,341 rows
function_result        0 rows      <- partitioned by `source`
```

`function_result` is empty: the 19,268 verdicts have not been loaded. And
`function_entity` at 47,341 rows is exactly the row count of `ranked.jsonl.gz`,
so the ranked file already covers the whole corpus and the priority list was a
20,036-row subset of it.

**4.** `<T>_result` **is keyed on** `parameter_set_hash`**, which is the versioning
answer, for free.** `current_state()` windows per parameter set, and
`clearinghouse results --parameter-set-hash` pre-filters on it. So "un-run at
THIS version" is a native query, not something this design has to invent. The
parameter set is where `query_version`, the BENCH and `mapping_hash` belong —
but **not** `roster_fingerprint`, which covers `endpoint_kind` and the route
spelling too and so would make a transport change re-partition the corpus. See
Q2.

**5.** `prompt_hash` **is a pure function, so the quote can be EXACT for the
deterministic seats.** `types.py:264` takes only (seat spec, reuse namespace,
effective params, json schema, query name, query version, renderer id version,
role, system, user) — no network, no backend. `seat_generation_params()` exists
specifically so a fingerprint can be computed "without instantiating a backend".
Two consequences:

- Every proposer and critic prompt for a planned run can be rendered and counted
locally, at zero API cost.
- Reuse is exactly countable: `ix_calls_prompt_hash_status` indexes
`(prompt_hash, status)`, so "how many of these prompts already have a paid
`ok` answer" is one indexed lookup per prompt.

Only the **meta** seat must be projected, because its prompt is a function of
ballots that do not exist until the chairs have voted.

**6. The token model can be fitted from our own measured traffic, so no
tokenizer dependency is needed.** `attempts.usage` holds per-attempt token counts
for the entire 199.2M-token run across all five seats, and `calls.fresh_usage`
holds the per-call figure. A per-(seat, role) fit of input-tokens-per-character
over ~58,000 real calls per chair is better calibrated than any single
third-party tokenizer would be across a roster spanning Google, OpenAI and
Anthropic — and it removes a dependency we would otherwise have to carry for
three families.

The prior run is the evidence that such a model is credible: **10,334 tokens per
item actual against 10,482 projected — 1.4% error** — and that projection was
built from a 475-item pilot with a far cruder model than this one.

**7. Neither gateway publishes a rate card, and this is a measured dead end, not
an unexplored one.** Every `usage` / `billing` / `quota` / `pricing` path 404s on
both `/argoapi` and `/argoapi/v1`; MAG's `/usage`, `/pricing`, `/limits`,
`/spend` all 404 for a non-admin key and `/models/<id>` returns 401 "Only proxy
admin can be used". Vendor list rates were solved exactly from the Claude Code
CLI's `costBasis: "list"` figures (`claude-sonnet-5` $2.00/$10.00 per M;
`claude-opus-5` $4.99/$31.02, four back-checks reproducing the billed figure to
0.00%) — but a $350/month ARGO cap was exhausted in the first week of October by
ordinary wake traffic, which could not happen at pass-through, so ARGO's
effective multiple over list is unknown and above 1. **A dollar figure derived
from list rates is a FLOOR and presenting it as a cost is the specific error
already made once.**

**8. The one hard prerequisite: the council on** `main` **cannot run.** `0825c5a`
("readiness must accept every id a gateway advertises") exists in no shared
branch — `git cat-file` in the Dropbox repo cannot even resolve the object. It
lives only in h100's editable install (`~/projects/KBUtilLib` and
`/scratch/fliu/hub_home/chenry/H100Projects/KBUtilLib` are the same inode,
`64517:5167606134`, one tree). Its own commit message records the measurement:
ARGO `/models` returns `{"id": "Gemini 3.5 Flash", "internal_id": "gemini35flash"}` and `/chat/completions` now accepts only `internal_id` — the
display spelling returns HTTP 400 for Claude and HTTP 500 for Gemini.
`servable.py` read only `entry["id"]`, making the readiness test the exact
inverse of the call, so **every ARGO seat is refused at submit and** `kbu council doctor` **reports the whole ARGO bench unavailable.** Three of five seats in the
measured roster are ARGO.

**9. Operational constraints that are measurement, not lore**, and that the
design must carry as hard constraints:

- **MAG limits are ACCOUNT-WIDE**: `max_parallel_requests` 20 and `requests` 120
per minute, read verbatim from stored errors. A config asking 24 parallel
errored 427 of 500 pilot items. MAG queues must sum to **6** (≈100 req/min with
headroom). Burst-concurrency probes overestimated sustained throughput by ~3x;
the limit is windowed and account-wide, so only sustained multi-seat load
reveals it.
- **ARGO saturates at ~24** and showed zero rate limiting at 40 concurrent across
three models. Round-1's "ARGO is the slow gateway" was an artifact of testing
it at 8.
- **~2,000-item chunks, one chunk in flight.** `claim_calls` is synchronous on
the worker's event loop and its cost is linear in the pending set: 217 ms at
19k pending rows vs **10.4 ms** at 2k, a 21x difference that inflated service
times 6–9x on both gateways at once and cost a 10.8x throughput collapse. The
daemon drains every claimable job, so submitting all chunks up front rebuilds
the pending set being avoided. KBUtilLib issue 84 is the real fix; chunking is
a declared workaround.
- **A retry pass is a stage, not an afterthought.** `invalid_output` is not in
`RETRYABLE_KINDS` (only `transport` and `timeout`) and ARGO truncates silently
with no length finish-reason, so a cut-off reply is abandoned on its first
attempt. 113 items failed that way; **105 came back on a plain retry for 0.30%
of run cost.**
- **The meta fire rate is 24.1%** over the full 19,268-item set. That figure
supersedes 16.7% (n=6) and 38.7% (n=475); each earlier one was honestly
caveated and each was wrong.
- **Input is 86% of all tokens.**



**10. The** `file` **adapter reads parquet, jsonl and tsv — not gzip.**
`FileSourceAdapter._format()` infers from the suffix among exactly those three
and `_iter_jsonl` uses a plain `path.open("r")`. So Albert's reasonable
suggestion — "for the automated loop, ship the `.gz` only" — is incompatible with
the manifest path as it stands. Parquet is the better answer on three counts: it
is compressed, `estimate_rows()` is exact from the footer where jsonl returns
`None` on purpose, and `read_batch_table` refuses anything but parquet anyway.

### Links to full research output

- [External prior-art scan (literature, web, reusable tools)](research/external-scan.md)
— run by a cross-family adversary (codex) on the Maestro rail as
`task-d9e8311c`, verified by git rather than by status (blob
`2e20c507`, absent from `main`, so the artifact is this run's). Covers:
pre-flight cost quoting for batch LLM workloads;
human-in-the-loop approval-gate artifacts; atomic directory publishing over an
eventually-consistent file sync; idempotent cross-machine loaders and receipts;
deriving the un-processed work set under a versioned transform; join-key
mismatch as a silent failure class; and rate-limit-aware work shaping.
- [Council composition and schedule, rounds 1–9](../../research/2026-10-20-function-to-go-council-composition-and-schedule.md)
— in the `miles` steward repo, not this one. The measured record of the
completed run: every service time, the four retracted projections, the token
economics, and the three cost levers with numbers.



### Quality report

**Searched and read in full:** `ontology_mapping_utils.py` (the whole module);
`interfaces/cli/council.py` (full command inventory); `interfaces/cli/clearinghouse.py`
(`plan`/`shard`/`load`/`verify`/`known`/`results` signatures and the pod guard);
`clearinghouse_manifest.py` (the manifest contract and its validation rules);
`clearinghouse_shard.py` (`FileSourceAdapter` in full);
`clearinghouse_ingest.py` (the five-check integrity gate, the `run.json`
requirement, registry-before-results); `council/store/schema.py` (`calls`,
`attempts`, `endpoint_state` and every index); `council/types.py` (`prompt_hash`,
`seat_generation_params`); and the four throwaway scripts that constitute the
current pipeline (`export_results.py`, `_adp1_prep.py`, `run_chunks.sh`,
`kbu-env.sh`).

**Measured live, not read:** the clearinghouse table inventory and row counts
from h100; the 760-definition gap, resolved against the actual `ranked.jsonl.gz`
and priority TSV; the inode identity of the two KBUtilLib paths; the absence of
`0825c5a` from the Dropbox repo; the Maestro queue state before dispatch.

**NOT searched, and why:**

- **The kbhub side of the load.** Deliberately. `load` is in-pod and kbhub is
Albert's machine; this PRD specifies the contract and the two commands he runs
and does not write his script. His 2026-10-22 reply (trigger
`5231042e`) confirms `~/Dropbox/Jobs/` reaches kbhub and that he verified the
`.gz` and `.tsv` by sha256 against the manifest.
- `engine/decide.py` **beyond the unanimity/critic/meta control flow.** The
council's decision logic is continuation [145]'s subject, not this PRD's.
- **The retrieval backend (**`go_text`**).** Its abstain behaviour was measured
through its output rather than read in source, which was sufficient to resolve
the 760; the ranking quality question (25.4% `no_candidate_matched`) is
explicitly out of scope.
- **Whether a second** `kbu clearinghouse` **concurrent load is safe.** The ingest
module has a lock (`ClearinghouseLockUnavailableError`) and I did not establish
its granularity. Recorded as a terminal open item.

**Confidence the design should carry:** high on stages 1, 4 and 5, because they
are assembly of code that exists and whose contracts were read directly. Medium
on stage 2 — the token-model fit is sound in principle and the 1.4% prior-run
accuracy supports it, but the fit itself has not been computed, so the quote's
real error bar is unmeasured. Medium-low on the *dollar* half of stage 2, which
rests on a rate table no gateway will supply.

## Revision Log

**Round 1 — 2026-10-22 (draft, single pass).** First complete bundle. Written
after a research pass that reversed the expected shape of the work: the design is
assembly of existing KBUtilLib machinery plus two new things (the quote and the
approval gate), not a new pipeline. Resolved the 760-definition coverage gap by
measurement and closed it. Established that the clearinghouse reads from h100, so
stage 1 does not need kbhub. Established that `0825c5a` is landed nowhere and
made it task one.

Then attacked by a cross-family confront round (`task-d9e8311c`), which returned
twelve binding stalls. Nine changed the design and none were dropped. The three
that mattered most: **concurrency caps do not enforce a rate limit** — the
working "MAG queues sum to 6" is concurrency standing in for MAG's account-wide
120/min and breaks if the gateway gets faster, so an account-wide pacer is now
Q5; **a sync client need not deliver** `run.json` **last**, so manifest-presence is
not an arrival proof and the trigger must verify bytes with a transfer deadline
behind it; and **a ceiling set at the quote's mean halts half of all runs**
partway, having spent most of the tokens for none of the result, so the quote now
emits an upper band and the ceiling derives from it. Also folded: approval expiry
and one-shot consumption, the call graph as a quote line-item, the pre-join
assertion set, mixed-version coverage in `council-status`, and a receipt that
reconciles against items *submitted* rather than rows written.

**Round 2 — 2026-10-22 (review, Chris's edits folded).** One substantive
annotation, and it landed on an IRREVERSIBLE decision: the bench must be fixed
across runs and go in the hash, while HOW a model is run may vary. That caught a
real defect. Round 1 put `roster_fingerprint` in the clearinghouse
`parameter_set`, and `Roster.fingerprint()` covers `endpoint_kind` and the route
spelling — so a transport change would have re-partitioned all 47,341 functions
and re-charged ~199M tokens for a pure cost optimisation. Q2 is rewritten around
a new model-only `bench_fingerprint`; `roster_fingerprint` and
`route_fingerprint` stay as provenance. New Q8 records the deliberate decision
NOT to apply the same rule to `prompt_hash`, whose `endpoint_kind` key is
justified by a direct measurement (the same question cost ~63,000 input tokens
over `claude -p` against 1,403 over the API). `PLAN.json` now carries the
per-seat transport allocation as an explicitly run-varying field; the quote
attributes reuse misses to new-item / bench-change / transport-change; the
approval binds to the bench rather than the roster. New gotchas G16 and G17.
Chris's editor reformatting was adopted as the new base rather than reverted, so
it stops swamping every future review diff.

## Problem Statement

Chris has one completed council run — 19,276 function descriptions, 19,268
verdicts, 199.2M tokens, 9.46 hours — and a corpus of 47,341 functions in the
clearinghouse, growing. He wants the next run, and every run after it, to happen
without a session of hand-driven scripting, and he wants to see the cost before
he pays it.

What stands in the way:

1. **Nothing can tell him what has not been run.** The one run was driven off a
  hand-exported priority TSV for two genomes. There is no verb that answers
   "which functions have no verdict at the current version".
2. **Nothing can quote a run.** He is asked to authorise a 200M-token spend on a
  gateway whose rate card does not exist, with no figure in front of him except
   what the last run happened to cost.
3. **The approval is a conversation.** It binds to nothing. A run started an hour
  later against a changed item set or a changed roster is indistinguishable from
   the one he approved, and there is no ceiling that stops a run that goes wrong
   from spending the rest of the balance.
4. **The pipeline is eight scripts on an ephemeral shared filesystem.**
  `/scratch` on this host is shared with other users, has filled to ENOSPC
   before, and is not backed up. `export_results.py` is the only description of
   how verdicts became a loadable file, and it is 88 lines of `sqlite3` and `csv`
   that joins on definition text because the hash it needed was thrown away two
   stages earlier.
5. **The hand-off to kbhub is undefined.** The last one was three files and a
  hand-written manifest; one of the three had not arrived on the consumer
   machine even though the sync client reported the directory up to date. Nothing
   reported back that a load had happened.
6. **And on** `main`**, the council does not run at all** — every ARGO seat is
  refused at submit, and three of five seats are ARGO.

The cost of leaving this alone is not only the hand-driving. It is that each of
the three cost levers already identified — dropping the third chair (~22% of
tokens), letting a 2-of-3 majority settle without a critic (~19%), trimming the
candidate menu (~30%, but now a joint yield/cost question because 25.4% of items
declined because the right term was never on the menu) — is a decision nobody can
price at the moment of making it.

## Solution

A standing five-stage pipeline, each stage a `kbu` verb, with one Dropbox
directory per run holding the small artifacts that cross machines or that Chris
reads. Large intermediates and the council store stay on h100-local `/scratch`,
because every byte on the sync surface needs an integrity gate and only what
another machine reads belongs there.

**The bench is fixed; the transport is not.** Chris, 2026-10-22: "We should try
and use the same models for the same seats in ALL our council runs we do for a
specific task, and this should go into the hash. The thing I think would be OK to
vary is HOW the model is run (e.g. codex, max, MAG, ARGO, direct GPU). The plan
should be iterative as I iron out for each particular run where we are best able
to allocate token cost."

That requirement separates two things this design had conflated, and the
separation runs through every stage below:

- **The BENCH** — which model sits in which seat — is the scientific identity of
  a verdict. It goes in the hash that partitions the corpus, and it is meant to
  stay constant across every run for a given task.
- **The TRANSPORT** — codex CLI, the Max subscription, MAG, ARGO, a direct GPU —
  is a cost-allocation decision, expected to change run to run as Chris works out
  where tokens are best spent. It is recorded as provenance on every row and
  **must not re-partition the corpus.**

See Q2 for what this replaced and why the obvious implementation was wrong.

### The run directory

`~/Dropbox/Jobs/function-to-go/<run-id>/`, where `<run-id>` is
`YYYY-MM-DD-<slug>`:

```
PLAN.json          stage 1  what is un-run, the chunk plan, the identity
QUOTE.json         stage 2  tokens per seat, exact vs projected, wall clock
APPROVED.json      stage 3  Chris's yes, bound to quote_hash, with a ceiling
RUN.json           stage 4  per-chunk progress and outcome, appended as it goes
shard/             stage 5  the published bronze shard run (run.json + parquet)
LOADED.json        stage 5  the receipt: kbu clearinghouse verify's envelope
```

Four of the six are small JSON. `shard/` is parquet. Nothing else crosses
Dropbox: the ranked candidate file, the council sqlite store and the verdict
intermediates stay on `/scratch`.

### Stage 1 — `kbu ontomap council-plan`

Derives the un-run set and writes `PLAN.json`. Runs on h100, unattended,
schedulable.

1. **Universe.** `function_entity` hashes joined to `function_content` text, read
  from the clearinghouse over the delta API. Measured working from h100.
2. **Already done.** `current_state("function")` filtered to
  `source = council/<query_name>@<version>` and
   `parameter_set_hash = <this run's parameter set>`.
3. **Un-run** = universe − done.
4. **Rank.** `function_to_go` over the un-run set; `write_ranked` to
  `/scratch/.../ranked.jsonl.gz` (already atomic via `.partial` → `replace`).
5. **Partition.** The un-run set splits three ways and all three are recorded:
  - **submit** — not abstained, has candidates, deduplicated by text
  - **abstain** — the retrieval stage declined (placeholder function, or no
  candidate survived the height filter); carried forward as result rows with
  `decline_reason = abstained:<reason>`, never dropped
  - **fan-out** — distinct `query_id`s sharing one text, which are answered once
  and fanned back out
6. **Chunk.** ~2,000 submit items per chunk, ordered, one in flight.
7. **Write** `PLAN.json` with the counts, the chunk list, the query name and
   version, the `mapping_hash`, the **`bench_fingerprint`** (model-only, Q2),
   the resolved `parameter_set`, the **transport allocation per seat** — which
   gateway each seat is routed over for THIS run, expected to differ between
   runs — the `roster_fingerprint` and per-seat `route_fingerprint` as
   provenance, the concurrency block, and a `plan_hash` over the canonicalised
   whole.



### Stage 2 — `kbu council quote`

Reads `PLAN.json`, writes `QUOTE.json`. **Spends nothing: zero API calls.**

1. **Fit the token model** from the store's own history — per `(seat, role)`,
  input tokens per prompt character and mean output tokens, over every
   `attempts.usage` row for that seat. Reported with its sample size.
2. **Render every deterministic prompt locally** — proposer and critic — through
  the same `queries/spec.py` renderer the worker uses, and compute each
   `prompt_hash` through `types.prompt_hash`.
3. **Attribute every reuse MISS, because a transport change makes a cheap run
   look like a full one.** `prompt_hash` includes `endpoint_kind` (Q8), so
   re-routing a seat invalidates that seat's stored answers even though the
   bench is unchanged. The quote must therefore split its misses three ways —
   **new item** (never asked), **bench change** (this seat's model moved), and
   **transport change** (same model, different route) — and say so in a line of
   its own. Without the split, a pure cost optimisation quotes like a corpus
   re-run and reads as a bug.
4. **Count reuse exactly.** For each `prompt_hash`, one indexed lookup for an
  existing `status='ok'` call. A hit costs zero tokens. Reported **per seat**,
   because that is what distinguishes a cheap roster change (one seat re-pays)
   from an expensive query-version bump (`query_version` is inside `prompt_hash`,
   so everything re-pays).
5. **Quote the CALL GRAPH, not just prompt lengths.** Five seats is not five
  identical calls per item, and an arithmetic that assumes it is will be wrong
   in the same direction every time. `QUOTE.json` enumerates the graph:
  - 3 proposer calls per item, **unconditional**;
  - 1 critic call per item, **also unconditional** — `decide.py` step 5 returns
  `need_critic` with no guard, which is why one critic per item carries no
  information about disagreement;
  - 1 meta call on a **fraction** of items;
  - a **paid-repair line** for the retry pass.
6. **Project meta only.** Volume = `submit_count × meta_fire_rate`, with the rate
  and its sample size named in the output. Default 0.241 (n=19,268).
7. **Emit a point estimate AND an upper band.** The band is what the approval's
  ceiling is derived from (stage 3). A mean presented as a cap understates the
   risk of the one outcome worth avoiding: a run halted partway, having spent
   most of the tokens for none of the result.
8. **Wall clock** from the measured per-seat throughput and the binding
  rate-limit ceiling, reported as the slowest queue, not the sum.
9. **Dollars, conditionally.** If `ai_council.rates` carries a rate for a
  gateway, report a dollar figure labelled with the rate's provenance and
   whether it is a floor. If it does not, report `unpriced` for that gateway.
   **Never substitute vendor list rates for a gateway's own rate.**
10. **A balance-delta block** — fields for the operator-read gateway balance
  before and after. Each completed run therefore calibrates the next run's
   dollar figure, which is the only route from "unpriced" to a real number.

Every line in `QUOTE.json` is tagged `exact` or `projected`. A quote whose
projected fraction exceeds a configured share of the total says so at the top.

### Stage 3 — `kbu ontomap council-approve`

Writes `APPROVED.json`, carrying: `plan_hash`, `quote_hash` (sha256 over the
canonicalised `QUOTE.json`), a `ceiling_tokens` per gateway, an approver label,
and a timestamp.

The runner refuses to start unless all of:

- `APPROVED.json` exists and parses;
- its `quote_hash` equals the recomputed hash of the `QUOTE.json` on disk;
- the `QUOTE.json`'s `plan_hash` equals the recomputed hash of `PLAN.json`;
- the **`bench_fingerprint`** and query version in `PLAN.json` still match what
the live config resolves to. Deliberately the BENCH and not the
`roster_fingerprint`: a transport change between quote and run is permitted and
must not invalidate an approval, whereas a model swap under the approval would
be approving one experiment and running another. A transport change does
invalidate the quote's *price*, so the runner warns and names the changed seats;
- `expires_at` is in the future;
- the approval has not already been consumed by a different run.

Any mismatch is a refusal naming which check failed. This is how a stale approval
cannot be replayed against a changed job.

**The ceiling is enforced by machinery that already exists.** `endpoint_state`
carries `budget_epoch`, `epoch_tokens`, `halt_kind` and `halt_message` per
endpoint. Approval opens a new budget epoch with the approved ceiling; the
existing halt path stops the run at it and records why.

**The ceiling is a CAP, not the quote's mean, and the difference matters.**
`council-approve` defaults `ceiling_tokens` to the quote's **upper band** times a
headroom factor (default 1.25), not to its point estimate. A ceiling set at the
mean halts roughly half of all runs partway through, and a run halted at 60%
has spent 60% of the tokens for none of the result — the most expensive possible
outcome. The quote therefore emits an upper band alongside the point estimate
(see stage 2) precisely so the ceiling has something honest to be derived from.

### Stage 4 — `kbu ontomap council-run`

Executes the approved plan. Replaces `run_chunks.sh`.

- One chunk submitted and drained at a time, in plan order, `.done` markers for
resumability, a per-chunk timeout so one bad chunk cannot wedge the run.
- Credentials: `ARGO_USER` and `AMSC_TOKEN` are exported in no shell on this
host, and a council started without them does not fail loudly — the seats
report unavailable and the calls park. The runner resolves both itself and
refuses to start if either is missing, which retires `kbu-env.sh`.
- To outlive the launching session it must be started under
`systemd-run --user --scope --collect`. `setsid`, `nohup` and `disown` do not
escape the unit's cgroup and systemd kills everything in it without a trace.
- **A retry pass is part of the run.** After the last chunk, items whose calls
failed `invalid_output` are retried to a round cap of 2. The run is not
complete until the retry pass has run or been declared unnecessary.
- `RUN.json` is appended per chunk: items, ok, error, duration, tokens by
gateway, and the live claim cost.
- Halts on the approved ceiling, on a gateway halt, and on an operator stop.



### Stage 5 — export, publish, load, receipt

1. `kbu ontomap council-export` writes **parquet** on `/scratch`, one row per
  *input* `query_id` — including abstentions and council declines — carrying
   `query_id`, `function_text`, `go_ids`, `go_confidences`, `final_confidence`,
   `decided_by`, `decline_reason`, `n_valid_proposers`, `votes`, `decided_at`,
   and the run identity. `query_id` **comes from the ranked file, not from a text
   join.**
2. **A shipped manifest preset**, `function-to-go-council`, as package data
  alongside the existing presets: `adapter = "file"`, `format = "parquet"`,
   `entity_type = "function"`, `kinds = ["result"]`,
   `[source.hash] precomputed = "query_id"` (re-canonicalised through
   `encode_entity_hash`, never trusted raw), `observed_at_column = "decided_at"`,
   and a `[source.result]` block whose `parameter_set` carries the query version,
   the `bench_fingerprint` and the `mapping_hash` — NOT the
   `roster_fingerprint`, which would re-partition the corpus on a routing change
   (Q2). The `roster_fingerprint` and per-seat `route_fingerprint` ride along in
   the result `payload` as provenance instead.
3. `kbu clearinghouse plan` then **`kbu clearinghouse shard --out
  /shard`**, on h100. Sharding runs anywhere. The sharder publishes`  run.json` last, carrying a sha256, a row count and a hash range per batch —
   which is the completeness contract this hand-off needs, already implemented.
4. **Dropbox syncs** `shard/` **to kbhub.**
5. **On kbhub, Albert runs `kbu clearinghouse load <shard_dir> --dry-run`, then
  without it.** Fill-only, idempotent, ledger-resumable; the ingest path runs
   five integrity checks per batch before any byte is used, and refuses a
   directory with no `run.json`.
6. `kbu clearinghouse verify <shard_dir> --run-id <id>` **is the receipt.** Its
  envelope is written back into the run directory as `LOADED.json`, which is
   what lets h100 see the pickup happened without asking.
   **The receipt must reconcile against what was SUBMITTED, not against what was
   exported, and must distinguish items from association rows.** "Loaded 19,268"
   is not a complete reconciliation when the plan submitted 19,276: the eight
   residual items are the interesting number and a receipt that omits them hides
   them. And one item can carry several GO terms, so a `function_result` row
   count is not an item count. `LOADED.json` therefore carries: items submitted,
   items with a verdict, items unresolved, result rows written, declines by
   reason, the target table's snapshot id, and the parameter_set_hash the rows
   landed under.

**The zero-join guard.** `--dry-run` reports the action each shard would take.
Before committing, the load must assert all of, and report the counts for each:

- the expected input is **non-empty** — a shard run with zero rows is a producer
failure, not a clean load;
- every join key is **non-null**;
- each source key maps to **at most one** target slot (no ambiguity);
- the **coverage** of source keys that resolve is at or above a declared floor.

A shard run with non-empty batches whose dry-run resolves **zero** rows is a
failure and must exit non-zero. This is the exact defect that nearly shipped: a
loader joining on the wrong hash would have loaded nothing and looked like it
worked. The trap is that the assertion must run **before** the join, not on its
result — a relationship test evaluated over an empty join passes vacuously, which
is how this class of defect survives having tests.

`run.json`**'s presence is NOT proof of arrival, and the trigger must not treat
it as one.** The sync client reported the last hand-off's directory up to date
while one of its three files was absent on the consumer, and nothing guarantees a
sync client preserves the producer's write order — so the marker can arrive
before the bytes it describes. The trigger condition is therefore not "a
`run.json` exists" but **"a** `run.json` **exists and every batch it lists is present
and sha256-matches"**, which the existing five-check ingest gate already
computes. What the design adds is what to do when it does not:

- **first failures mean "still syncing" — retry**, with progress persisted so a
retry does not restart the verification from nothing;
- **after a declared transfer deadline** (default 6 h from the `run.json`'s
mtime) **it is a failed delivery** and must be raised with the evidence: which
batch, expected vs observed size, expected vs observed digest.

Neither extreme is safe on its own. Treating the first mismatch as corruption
alarms on every normal sync lag; treating it as transient forever means a genuinely
lost file waits silently. A first observation cannot tell the two apart — only
elapsed time can.

**One writer per file.** h100 writes `PLAN.json`, `QUOTE.json` and `RUN.json`;
Chris writes `APPROVED.json`; kbhub writes `LOADED.json`. No file is written by
two machines, and there is no shared mutable `status.json` — run state is derived
from the immutable artifacts plus each machine's own ledger. The only reason to
state this is that a shared status file is the obvious thing to build and it
produces a lost-update race across a sync surface with no locking.

**The receipt must be re-exportable, not a one-shot.** The Iceberg commit and the
`LOADED.json` write are two unrelated writes, so a crash between them loses the
acknowledgement while the data is safely landed — and a producer that reads "no
receipt" as "not loaded" would ask for a reload. `verify` is derivable from the
ingest ledger and the target's own snapshot history, so re-running it after a pod
restart reproduces the same receipt. The trigger must therefore re-export a
missing receipt rather than re-loading.

### What is automated and what is not


| stage            | runs                             | automatable                                                               |
| ---------------- | -------------------------------- | ------------------------------------------------------------------------- |
| 1 plan           | h100, unattended                 | yes — schedulable                                                         |
| 2 quote          | h100, unattended, spends nothing | yes — runs with the plan                                                  |
| 3 approve        | Chris                            | no, by design                                                             |
| 4 run            | h100, launched by the approval   | yes, once approved                                                        |
| 5 export + shard | h100, unattended                 | yes                                                                       |
| 5 load + verify  | kbhub, in-pod                    | Albert's, triggered by a shard dir with a `run.json` and no `LOADED.json` |


Stages 1 and 2 together are the "automatically placed in a job folder" half of
the ask, and they are safe to schedule because neither spends anything.

## User Stories

1. As Chris, I want a verb that tells me how many functions have no council
  verdict at the current version, so that I know whether a run is worth
   starting at all.
2. As Chris, I want the un-run set materialised in a Dropbox folder I can open,
  so that the pending work is visible without my asking an agent.
3. As Chris, I want a token quote before I approve, so that I am authorising a
  number rather than a hope.
4. As Chris, I want the quote to tell me which of its lines are measured and
  which are projected, so that I know where its error lives.
5. As Chris, I want the quote expressed in tokens, with dollars only where a rate
  is actually known, so that I am never shown a list-derived figure as a cost.
6. As Chris, I want the quote to tell me how much of the run is already paid for
  by reuse, so that re-running after a small change is visibly cheap.
7. As Chris, I want to see, per seat, what a roster change costs versus what a
  query-version bump costs, so that I can choose the cheap change knowingly.
8. As Chris, I want my approval to carry a token ceiling the run halts at, so
  that a run that goes wrong cannot spend the balance.
9. As Chris, I want my approval bound to the exact plan and quote I read, so that
  nothing can be substituted between my yes and the run.
10. As Chris, I want a one-line verb to approve, so that the gate costs me a
  command and not a session.
11. As Chris, I want to decline a quote and have that recorded, so that a
  declined plan does not sit looking pending.
12. As an operator, I want the run to survive the session that starts it, so that
  a 9-hour job does not die when a wake ends.
13. As an operator, I want per-chunk progress on disk, so that I can see where a
  run is without attaching to it.
14. As an operator, I want a resumable run, so that a killed chunk costs one
  chunk and not the whole job.
15. As an operator, I want the runner to resolve its own credentials and refuse
  without them, so that a run cannot silently lose seats.
16. As an operator, I want a retry pass to be part of the run, so that the ~0.6%
  of items that fail on a silent truncation are not abandoned.
17. As an operator, I want the chunk size and the per-gateway concurrency to come
  from the plan, so that the measured rate limits are configuration and not
    folklore.
18. As an operator, I want the run to halt on a gateway halt rather than retry
  into a rate limit, so that a 429 does not amplify into a storm.
19. As Albert, I want the published output to be a standard shard run with a
  `run.json`, so that I load it with the command I already have and not a
    bespoke script.
20. As Albert, I want a dry-run that tells me what a load would do before it does
  it, so that I have a last gate.
21. As Albert, I want the load to be idempotent, so that re-running it after a
  pod restart is free and safe.
22. As Albert, I want declines in the data as rows with a reason, so that I do not
  mistake a legitimate "no GO term" for missing data.
23. As Albert, I want the join key to be a clearinghouse entity hash carried
  through from the source, so that I am not matching on free text.
24. As Albert, I want a load that joins nothing to fail loudly, so that a wrong
  key is an alarm and not a silent success.
25. As Chris, I want a receipt written back into the run folder, so that h100 can
  see the load happened without waking anyone on kbhub.
26. As Chris, I want the receipt to carry row counts, so that "loaded" and
  "loaded nothing" are different answers.
27. As Chris, I want each run's gateway balance before and after recorded, so
  that the next run's dollar quote is better than this one's.
28. As Chris, I want abstained functions recorded as results, so that they are
  not re-selected as un-run forever.
29. As Chris, I want a version bump to re-select the corpus and to say so in the
  quote, so that the cost of a bump is visible before I pay it.
30. As a future maintainer, I want the whole pipeline to be `kbu` verbs in the
  repo rather than scripts on `/scratch`, so that the next run does not depend
    on a filesystem that is shared, unbacked-up and declared ephemeral.
31. As a future maintainer, I want the chunking workaround to name KBUtilLib
  issue 84, so that the workaround is removed when the defect is fixed rather
    than becoming permanent.
32. As a future maintainer, I want the council to run from a clean clone of
  `main`, so that the system is not dependent on one machine's local commit.



## Implementation Decisions



### Modules and their interfaces

`kbutillib/domains/genome/ontology_mapping/ontology_mapping_utils.py` gains
the pipeline stages, because it already owns `function_to_go`,
`function_to_go_council`, `write_ranked`/`read_ranked` and `write_council`, and
the stages are the same two capabilities plus bookkeeping:

- `council_plan(run_dir, *, query, parameter_set=None, chunk_size=2000) -> dict`
— derives the un-run set, ranks it, partitions it, chunks it, writes
`PLAN.json`, returns the plan.
- `council_run(run_dir, *, resume=True) -> dict` — validates the approval, drains
the chunks, runs the retry pass, appends `RUN.json`.
- `council_export(run_dir, out_path) -> dict` — writes the verdict parquet, one
row per input `query_id`.

The un-run derivation is a separate deep module so it is testable without the
clearinghouse: `unrun_set(universe, done, ranked) -> {submit, abstain, fanout}`
is pure, and the clearinghouse reads are two thin calls around it.

`kbutillib/domains/ai/council/quote.py` — new, and the deep module of this
PRD. A lot of behaviour behind a small interface:

- `fit_token_model(store, *, seats) -> TokenModel` — per `(seat, role)`, input
tokens per prompt character and mean output tokens, from `attempts.usage`, with
sample sizes.
- `quote_plan(plan, *, store, config, token_model, meta_fire_rate) -> Quote` — the
whole quote. Renders deterministic prompts, computes `prompt_hash` per
`(item, seat)`, counts reuse, applies the model, projects meta, computes wall
clock, prices against a rate table if one exists.

`Quote` is a dataclass with per-line `basis: "exact" | "projected"` and serialises
to `QUOTE.json`. Nothing in this module opens a socket; that is the testable
property that makes "the quote spends nothing" checkable rather than asserted.

`kbutillib/interfaces/cli/ontomap.py` — new CLI group `kbu ontomap`, since
none exists. It follows the envelope conventions already used by `council.py` and
`clearinghouse.py` verbatim: `_envelope`/`_emit`/`_render`, a `--json` flag on
every command, `locus` where relevant, and `warnings` as a first-class field.
Commands: `version`, `council-plan`, `council-quote` (a thin alias onto
`kbu council quote`), `council-approve`, `council-run`, `council-export`,
`council-publish`, `council-status`.

`kbu council quote` is added to the existing council group, because the quote
needs the renderer, `prompt_hash`, the seat specs and the usage history — all
council-internal. `kbu ontomap council-quote` delegates to it.

`kbutillib/domains/kbase/berdl/presets/function-to-go-council.toml` — the
shipped manifest preset, as package data beside the existing presets, so
`backfill resolve` finds it by name.

### The approval artifact

`APPROVED.json`:

```json
{
  "approved_at": "<iso8601 utc>",
  "expires_at": "<iso8601 utc; default approved_at + 7 days>",
  "approver": "<label>",
  "plan_hash": "<sha256>",
  "quote_hash": "<sha256 over canonicalised QUOTE.json>",
  "ceiling_tokens": {"argo": 0, "mag": 0},
  "note": "<free text>"
}
```

**It expires, and it is consumed once.** An approval with no expiry is a standing
authorisation to spend, which is not what "approve this run" means; an approval
with no consumption record can be replayed against a second run of the same plan.
So the runner refuses an expired approval, and records the approval's
`quote_hash` into `RUN.json` on first use — a second run against a `RUN.json`
that already names that `quote_hash` is refused unless it is a declared resume of
the same run.

`canonical_sha256` from `council/types.py` is reused for both hashes, so the
canonicalisation rule is the one the store already uses.

**This is an audit trail and a ceiling, not an authentication boundary**, and the
PRD says so rather than implying otherwise. There is no key material on these
machines that would make an approval cryptographically Chris's, and a signature
scheme that nothing can verify would be security theatre. What the gate buys is
that the ceiling is explicit, the binding is checkable, and a stale approval
against a changed job is refused.

### Declines are results

Three decline classes are written to `function_result`, all with `go_ids = []`:


| `decline_reason`       | origin                                                                     | cost to produce   |
| ---------------------- | -------------------------------------------------------------------------- | ----------------- |
| `abstained:<reason>`   | retrieval; placeholder function or no candidate survived the height filter | local, cheap      |
| `no_candidate_matched` | the council; the right term was not on the menu                            | full council cost |
| `contentless`          | the council; the description carries no functional content                 | full council cost |


Recording all three is what stops stage 1 re-selecting them forever. The
expensive two are the load-bearing case: 9,356 declined rows in the completed
run, 4,893 of them `no_candidate_matched`, every one of which cost a full
five-seat council pass. An absent row is indistinguishable from "not yet run".

A `council_error:*` item — all calls failed, so there is no verdict — is **not** a
result and is not written. It is genuinely un-run and must be re-selected.

### Versioning — the bench partitions, the transport does not

    parameter_set = {query:            "<name>@<version>",
                     bench_fingerprint: "<sha>",
                     mapping_hash:      "<sha>"}

hashed by the clearinghouse's own `parameter_set_hash`. Un-run means "no
current-state `function_result` row at this `parameter_set_hash`".

`bench_fingerprint` is NEW and is `canonical_sha256([{role, canonical_model} for
role, seat in roster.seats()])` — the bench and nothing else. It is deliberately
NOT `Roster.fingerprint()`, which also covers `endpoint_kind`, the route
spelling, `max_output_tokens`, `temperature` and `extra`, and would therefore
let a routing change re-partition 47,341 rows. Q2 carries the measurement and
the reasoning.

**What is still recorded, just not as a key.** Every exported row keeps
`roster_fingerprint` and the per-seat `route_fingerprint` as provenance, so
"which transport produced this answer" stays answerable — that is what
`route_fingerprint` is for, per its own docstring. And `PLAN.json` records the
run's **transport allocation** per seat explicitly, as a field expected to differ
between runs: Chris, 2026-10-22, "the plan should be iterative as I iron out for
each particular run where we are best able to allocate token cost." A field that
is expected to vary should be written down per run, not inferred from config
later.

The consequence, stated so it is a decision rather than a surprise: a bump to the
query version, the bench, or the `mapping_hash` still re-selects the whole
corpus. That is correct — a different question, a different bench or a different
candidate generator is a different experiment — and it is expensive, which is
exactly why the quote exists. A transport change re-selects nothing.

The quote's per-seat reuse count is what distinguishes the cheap case (one seat's
model changed; the others reuse) from the expensive one (`query_version` is
inside `prompt_hash`, so everything re-pays).

### Rate-limit shaping — an ACCOUNT-WIDE pacer, not per-queue semaphores

Concurrencies come from the plan, not from code constants: MAG queues summing to
6; ARGO chair at 24, critic and meta at 8. A gateway halt stops the run rather
than being retried into, because the council already retries a 429 and retries
spend the same per-minute budget, which is how the first pilot spiralled to
**1,925 transport attempts for 500 items**.

But **concurrency caps do not enforce a rate limit, and MAG's binding limit is a
rate** — 120 requests per minute, account-wide. Six concurrent queues at the
measured 3–4 s service time happen to land near 100 req/min, which is how the
working configuration was found; that is a coincidence of service time, and it
breaks the moment the gateway gets faster. So the design adds an **account-wide
request pacer** — one token bucket per *endpoint account*, refilling at the
configured requests-per-minute, which every seat sharing that account must
acquire from before dispatch. Per-model semaphores stay, for the parallel cap;
the bucket is what enforces the rate.

Two honest limits on it, both recorded rather than designed around:

- The bucket cannot see **other consumers on the same account**. If another
process on this host uses MAG, the ceiling is shared and the pacer will
overshoot. The run must therefore treat a sustained 429 as a halt condition
and not only as a retry.
- Discovering a ceiling safely means **ramping**, not storming. Burst probes
overestimated sustained throughput by ~3x because the limit is windowed; the
only safe discovery is a slow ramp that backs off on the first 429 and records
the rate it reached.



### `kbu ontomap council-status` — the mixed-version view

A corpus under a versioned transform is **always** partly stale, and a status
verb that reports one number hides that. `council-status` reports corpus coverage
split two ways:

- **by lifecycle**: un-run · planned-not-quoted · quoted-not-approved ·
approved-not-run · running · exported-not-loaded · loaded. The two middle-late
states are the ones that go missing: work that has been paid for but has not
landed is invisible in both a "what is un-run" count and a "what is loaded"
count, and that is precisely the state a crashed run leaves behind.
- **by** `parameter_set_hash`: how much of the corpus is current, and how much is
covered only by an older parameter set. Partial-version coverage is **tolerated,
not normalised away** — re-running the whole corpus on every bump would be
unaffordable — so it must be reported rather than hidden. Anything else makes
"is this dataset consistent?" unanswerable.



### What is deliberately NOT built

No new loader, no new manifest format, no new integrity protocol, no
`LOADED.json` schema of our own invention — `verify`'s envelope is the receipt.
No bespoke kbhub script. The temptation to write all four is the one this
research pass removed.

## Testing Decisions

The tests that matter here test **external behaviour at the seams**, because
every one of this pipeline's real defects lived at a seam between two components
that each worked.

**The quote spends nothing.** The strongest available test, and it is a property,
not a mock: `quote_plan` is exercised against a seeded store with no network
available and must complete. A test that stubs the HTTP layer and asserts it was
not called is testing its own stub; a test that runs with the socket module
poisoned is testing the claim.

**The quote is exact where it says it is.** Replay the completed run: fit the
token model from the store, quote the 19,276-item plan, and compare against the
recorded 199.2M actual. The assertion is on the *exact* lines (proposer and
critic input tokens, and reuse counts), which should agree closely, and
separately on the projected lines with a stated tolerance. This is a regression
test with a real answer in the store, which is rare and worth using.

**Reuse counting is exact.** Seed a store with known `ok` calls at known
`prompt_hash` values, plan over the same items, and assert the reuse count is the
seeded count. Then change one seat's model and assert only that seat's reuse
drops. Then bump the query version and assert all reuse drops. Those three
assertions are the per-seat-versus-query-bump distinction the design rests on.

**The approval binding refuses every way it can be stale.** Four separate tests,
one per check: a missing `APPROVED.json`; a `quote_hash` that no longer matches
an edited `QUOTE.json`; a `plan_hash` that no longer matches an edited
`PLAN.json`; a `bench_fingerprint` that no longer matches the live config. Each
must refuse and name which check failed. A single "it refuses a bad approval"
test would pass while three of the four checks were absent.

**The un-run derivation is pure and is tested as a function.** `unrun_set` over
synthetic universe/done/ranked inputs: the abstained set appears in `abstain` and
not `submit`; a text shared by three `query_id`s appears once in `submit` and
three times in `fanout`; an item with a current-state result at this
`parameter_set_hash` is excluded, and the same item at a *different* hash is
included. **The 760/19,276/20,036 split from the completed run is a fixture**,
because it is a real partition with a known answer.

**The zero-join guard fires.** A shard run with non-empty batches whose keys
match nothing in the target must exit non-zero. Constructed deliberately by
hashing the wrong column, which is the actual defect it guards.

**Declines round-trip.** Each of the three decline classes survives
export → shard → the manifest mapping with its reason intact, and a
`council_error:*` item does not appear in the output at all.

**Prior art in the codebase to follow:** `tests/domains/ai/council/store_contract.py`
and `test_store_contract.py` for store-backed tests;
`clearinghouse_parity_fixture.py` for clearinghouse-shaped fixtures;
`test_four_queue_integration.py` for a multi-seat run without live gateways; and
`test_go_replay.py`, which already replays a stored council run and is the
closest existing analogue to the quote-replay test above.

**Framing, because the suite is not green and will not be.** The KBUtilLib suite
runs 900–1,130 s against a 600 s sub-agent ceiling, and
`test_static_headers.py::TestPackagedExample::test_the_default_config_declares_no_mag_endpoint`
fails on any machine with a user MAG endpoint configured (KBUtilLib issue 83,
pre-existing). So every task's criterion is **regression-framed**: the tests the
task adds pass, and no test that passed on the base commit fails on the branch.
Sub-agents run targeted selections; the conductor owns the full suite.

## Open Questions and Judgement Calls

Every entry below is **already decided and reflected in the PRD body and the
taskplan**. The PRD is buildable as it stands. To change a decision, edit its
`DECIDED:` line and Review Mode will propagate it.

Ordered by blast radius descending.

### Q1. What is the join key from the council into the clearinghouse? — DECIDED: `query_id`, carried through from the ranked file, re-canonicalised by the manifest's `precomputed` hash mode. Never a text join, and never the council's own `entity_hash`.

**Blast radius:** IRREVERSIBLE
**Why:** The council computes its own `items.entity_hash` and it matched **0 of
19,268** clearinghouse hashes; the definition text matched 19,268 of 19,268. But
the right fix is not to join on text — it is to stop discarding the key. The
ranked file carries `q` (the clearinghouse `entity_hash`) beside `t` (the text) on
every row, and `function_to_go_council` already propagates `query_id` into its
output. The hand-rolled run dropped that column and then recovered it by text.
`[source.hash] precomputed = "query_id"` re-canonicalises through
`encode_entity_hash` and never trusts the raw value, which is the manifest's own
documented stance.
**If you disagree:** a text join makes `function_text` load-bearing, so any
whitespace or unicode normalisation anywhere in the chain silently drops rows;
and it forces `clearinghouse_entity_hash` to be resolved by a lookup table that
must be shipped with every export. Changing this moves `council-export`
(t6), the manifest preset (t8), and the zero-join guard's fixture (t10).
**Confidence:** high — both hash behaviours were measured, and the `precomputed`
mode exists for exactly this case.

### Q2. What partitions the corpus — i.e. what makes an item "un-run"? — DECIDED: the clearinghouse `parameter_set_hash` over `{query@version, bench_fingerprint, mapping_hash}`, where `bench_fingerprint` is a NEW model-only hash. NOT `roster_fingerprint`, which was round 1's answer and was wrong.

**Blast radius:** IRREVERSIBLE
**Why:** Round 1 used `roster_fingerprint`, and Chris's review caught that it
violates his requirement. Measured: `Roster.fingerprint()` (`types.py:507`)
covers, per seat, `role`, **`endpoint_kind`**, `model` (the route spelling),
`canonical_model`, `max_output_tokens`, `temperature` and `extra`. So it
**includes the transport.** Moving `gpt-5.6-sol` from the codex CLI to MAG
changes `endpoint_kind` and the route spelling, so the hash changes, so stage 1
re-selects all 47,341 functions as un-run and the whole corpus is re-paid — about
199M tokens — for a pure cost optimisation. That is the precise opposite of
"the thing I think would be OK to vary is HOW the model is run."

`bench_fingerprint` is therefore `canonical_sha256([{role, canonical_model} for
role, seat in roster.seats()])` — the bench and nothing else. Model-only is the
minimal scientifically-meaningful identity: the query fixes the question, the
`mapping_hash` fixes the candidate menu, and the bench fixes who answered. The
transport does not change what the experiment IS.

`roster_fingerprint` and the per-seat `route_fingerprint` are still recorded as
**provenance** on every exported row — `route_fingerprint`'s own docstring says
it "answers 'where did this come from', not 'is this the same question'" — so
nothing is lost. They simply stop being partition keys.

The clearinghouse side is unchanged and still native: `current_state()` windows
per parameter set and `results --parameter-set-hash` pre-filters on it, so this
is the shape the store already wants.

**Deliberately EXCLUDED from `bench_fingerprint`, and this is the judgement
call inside the judgement call:** `temperature`, `max_output_tokens` and `extra`.
They can change an answer, so there is a real argument for including them. They
are excluded because they are substantially transport-COUPLED — different
gateways impose different ceilings and defaults — so including them would let a
transport change re-partition the corpus through the back door, which is the
failure this question exists to prevent. On a near-deterministic JSON-extraction
task that is the right trade. If Chris wants sampling parameters to be
scientifically load-bearing, they belong in the **query version**, where a change
is explicit and intentional, not in the bench hash where it would be a side
effect of a routing decision.

**If you disagree:** reverting to `roster_fingerprint` means every transport
change is a full-corpus re-run, and it will not be obvious that is what happened
— stage 1 will simply report 47,341 items un-run and the quote will price them.
Changing this moves `bench_fingerprint` itself (t3), `unrun_set` and `PLAN.json`
(t5), the manifest's `parameter_set` block (t8), and the reuse tests (t4).
**Confidence:** high on the diagnosis, which is read directly off
`types.py:507`; high on model-only for the bench; **medium on excluding
`temperature`**, which is the one line here a reasonable person could reverse.
**Consequence, named rather than hidden:** a bump to the query version, the
bench, or the `mapping_hash` still re-selects all 47,341 functions. That is
correct and it is expensive, which is why the quote exists. See G3 and G16.

### Q3. How is a run's cost quoted when no gateway publishes a rate card? — DECIDED: quote in TOKENS as the primary unit; report dollars only per-gateway where `ai_council.rates` carries a rate, labelled with its provenance and whether it is a floor; `unpriced` otherwise. Record a balance-delta per run so each run calibrates the next.

**Blast radius:** MEDIUM
**Why:** Measured: every ARGO and MAG `usage`/`billing`/`pricing`/`quota` path
404s, and MAG's `/models/<id>` returns 401 "Only proxy admin". Vendor list rates
were solved exactly from the Claude CLI's `costBasis: "list"` figures — and a
$350/month ARGO cap was exhausted in a week by ordinary wake traffic, which
cannot happen at pass-through. So ARGO's effective multiple over list is unknown
and greater than one, and a list-derived dollar figure is a floor. Presenting a
floor as a cost is the error that already happened once.
**If you disagree:** quoting dollars at list makes the quote comfortable and
wrong, and the error is one-sided — always low, by an unknown factor. If you want
dollars badly enough to accept that, the change is one default in the rate table,
but the floor label must stay.
**Confidence:** high on the mechanism; **medium on whether token counts alone are
decision-useful to Chris.** What would raise it: two or three completed runs with
balance deltas recorded, after which the multiple is measured and dollars become
real.

### Q4. Is the quote exact or projected? — DECIDED: exact for proposer and critic (rendered locally, `prompt_hash`-counted, reuse looked up); projected for meta only; every line tagged with its basis.

**Blast radius:** MEDIUM
**Why:** `prompt_hash` is a pure function of the seat spec, params, schema, query
name and version, renderer version, role and prompt text —
`seat_generation_params()` exists so a fingerprint can be computed without a
backend. So the deterministic prompts can be rendered and counted at zero cost,
and reuse is one indexed lookup per prompt against
`ix_calls_prompt_hash_status`. Meta cannot be: its prompt is a function of
ballots that do not exist yet. Input is 86% of all tokens and almost all of it is
exact, which is why this split is worth the complexity of a per-line basis tag.
**If you disagree:** projecting everything from per-item averages is simpler and
was accurate to 1.4% on the last run — but it cannot see reuse, which is the
whole point of quoting a *re*-run, and it would have reported a full price for a
run that was already 70% paid for.
**Confidence:** high on the mechanism; medium on the token model's error bar,
which is unmeasured until t3 fits it.

### Q5. What enforces MAG's account-wide 120 requests/minute? — DECIDED: an account-wide token-bucket pacer that every seat on that account acquires from, in addition to the per-model concurrency caps. NOT per-queue semaphores alone.

**Blast radius:** MEDIUM
**Why:** The working configuration found by measurement is "MAG queues summing to
6", which is a *concurrency* control standing in for a *rate* limit. It works
only because the measured service time is 3–4 s, so six in flight happens to
land near 100 req/min. If the gateway gets faster — the single most likely change
to it — the same configuration overshoots 120/min and 429s, and the council
retries a 429, so retries spend the same budget and it spirals. That is exactly
how the first pilot reached **1,925 transport attempts for 500 items**. A rate
limit needs a rate control.
**If you disagree:** keeping concurrency-only is less code and is empirically
working today. The cost of being wrong is a repeat of the 85%-failure pilot, and
it would present as "MAG is broken" rather than as a config error — which is how
it presented the first time, and the wrong conclusion drawn from it (re-rostering
off MAG entirely, without pricing the alternative) cost a round. Changing this
moves t9 and the `concurrency` block in `PLAN.json` (t5).
**Confidence:** medium-high on the need; **medium on the implementation seam** —
whether the pacer belongs in the per-seat queue worker or in the HTTP backend was
not settled by reading, and t9 must choose it against the code rather than from
this PRD.
**Known limit, recorded not solved:** the bucket cannot see other consumers on
the same account, so a sustained 429 must remain a halt condition and not only a
retry.

### Q6. What enforces the ceiling the approval carries? — DECIDED: the existing `endpoint_state` budget machinery — approval opens a `budget_epoch` with the ceiling and the existing `halt_kind`/`halt_message` path stops the run.

**Blast radius:** MEDIUM
**Why:** `endpoint_state` already carries `budget_epoch`, `epoch_tokens`,
`lifetime_tokens`, `halt_kind` and `halt_message` per endpoint, keyed on the
store-local identity. A second ceiling mechanism in the runner would be a parallel
truth that disagrees with the store's own counter under exactly the conditions
that matter.
**If you disagree:** a runner-side counter is simpler to write but is lost on a
restart, and the run is designed to survive the session that starts it. Changing
this moves t7 and t9.
**Confidence:** medium — the fields and the halt enum exist and `config.py:627`
documents the enforcement stance ("an unmeasurable budget is unenforceable"), but
I did not trace the halt path end to end from a budget overrun to a stopped
worker. t9 must verify it rather than assume it, and the task says so.

### Q7. Where does the pipeline's code live? — DECIDED: `OntologyMappingUtils` for stages 1/4/5-export; a new `council/quote.py` for stage 2; a new `kbu ontomap` CLI group; a shipped manifest preset for stage 5.

**Blast radius:** MEDIUM
**Why:** `OntologyMappingUtils` already owns both council capabilities and the
hand-off file formats, and its module docstring already describes the two-machine
split this pipeline operationalises. The quote belongs with the council because it
needs the renderer, the seat specs, `prompt_hash` and the usage history. There is
no `ontomap` CLI group today, so the operator surface is genuinely new.
**If you disagree:** a standalone pipeline package would be independently
releasable but would re-export four existing formats and duplicate the two
capabilities. Changing this moves every task's file layout but no behaviour.
**Confidence:** high

### Q8. Should `prompt_hash` also stop keying on the transport, so a paid answer survives a routing change? — DECIDED: NO. The reuse key legitimately keeps `endpoint_kind`; only the PARTITION key (Q2) drops it.
**Blast radius:** MEDIUM
**Why:** This is the other half of Chris's bench-vs-transport requirement, and it
is the half where the requirement should NOT be applied — so it is recorded as a
decision rather than silently skipped. `prompt_hash` (`types.py:264`) includes
`endpoint_kind`, and its docstring gives the reason: "the same canonical model
reached over a CLI and over HTTP is a different deployment with different
defaults." That reason is not theoretical here. Measured in round 3: the same
~1,400-token critic question cost **~63,000 input tokens** over `claude -p`
against **1,403** over the API, because the CLI path wraps an agent harness
around it. The two paths demonstrably do not send the same thing, so treating
their answers as interchangeable would let a harness-contaminated answer be
served as an API answer. The partition key asks "is this the same experiment";
the reuse key asks "is this literally the same question, already paid for".
Different questions, correctly different keys.
**Why keeping them different costs almost nothing:** the two interact benignly.
Under Q2's `bench_fingerprint` a transport change does not re-select completed
items, so the reuse `prompt_hash` would have lost is never needed. The loss bites
only on items that are genuinely pending, which are unpaid anyway.
**If you disagree:** dropping `endpoint_kind` from `prompt_hash` is a KBUtilLib
engine change against a documented rationale, and it would make every `claude -p`
answer already in the store reusable as though it had come from the API. There
are 104 such answers from the pre-rebuild runs.
**Confidence:** high — this rests on a direct measurement and on the code's own
stated reason.

### Q9. What does Albert run, and who writes it? — DECIDED: `kbu clearinghouse load` then `verify`, both existing; this PRD ships the manifest preset and the published shard run, and does NOT write any kbhub-side script.

**Blast radius:** LOW
**Why:** `load` is in-pod-only by design and kbhub is Albert's machine; acting
outside this machine's scope is not mine to do. The load is already idempotent,
fill-only, ledger-resumable and gated by five per-batch integrity checks, with a
`--dry-run` the code itself calls "the operator's last gate". There is nothing
left to write.
**If you disagree:** nothing in the design changes — only who writes the trigger.
The trigger itself is three lines: find a `shard/` with a `run.json` and no
`LOADED.json`, run `load --dry-run`, then `load`, then `verify`.
**Confidence:** high — Albert's 2026-10-22 reply already states he will load from
the published artifact and has opened it as work on his side.

### Q10. What is published across Dropbox? — DECIDED: four small JSON files, one parquet shard run, and nothing else. The ranked file, the council store and the verdict intermediates stay on `/scratch`.

**Blast radius:** LOW
**Why:** Every byte on the sync surface needs an integrity gate, and the gate for
the shard run already exists. The last hand-off put a 10.2 MB `ranked.jsonl.gz`
and a 14.2 MB uncompressed JSONL into Dropbox with no consumer for either, and
the uncompressed JSONL is precisely the file that had not arrived on kbhub when
the sync client reported the directory up to date.
**If you disagree:** publishing the ranked file would let kbhub re-rank
independently, which the module docstring contemplates (kbhub ranks, poplar runs
the council). But both stages run on h100 in this pipeline because the retrieval
models are vendored package data, so there is no second ranker to feed.
**Confidence:** high

### Q11. Parquet, JSONL or gzipped JSONL for the published verdicts? — DECIDED: parquet, via the sharder, with no intermediate published at all.

**Blast radius:** LOW
**Why:** `FileSourceAdapter._format()` accepts exactly `parquet | jsonl | tsv` and
`_iter_jsonl` uses a plain `path.open("r")`, so **gzipped JSONL is not readable by
the manifest path** — which retires Albert's reasonable "ship the `.gz` only"
suggestion with a better answer rather than by overruling it. Parquet is
compressed, `estimate_rows()` is exact from its footer where jsonl returns `None`
deliberately, and `read_batch_table` refuses anything but parquet anyway. Since
h100 runs `shard` itself, the verdicts never need to cross Dropbox in any text
form.
**If you disagree:** adding gzip support to `_iter_jsonl` is four lines and would
let the `.gz` be loaded directly, which is attractive if a human ever wants to
`zcat` the published file. That is a separate, small, independently useful change
and it is named in Out of Scope.
**Confidence:** high

### Q12. How is the approval authenticated? — DECIDED: it is not. `APPROVED.json` is an audit trail and a binding ceiling, not an authentication boundary, and the PRD says so plainly.

**Blast radius:** LOW
**Why:** There is no key material on these machines that would make an approval
cryptographically Chris's, and an agent can write the file. A signature scheme
nothing can verify is theatre, and claiming the gate is stronger than it is would
be worse than the gate's actual weakness. What it does buy is real: the ceiling
is explicit, the binding to plan and quote is checkable, and a stale approval is
refused.
**If you disagree:** the honest upgrade is an out-of-band confirmation —
Chris pastes a token the plan prints — and that is one field in `APPROVED.json`
plus one check in t7. It is not built because nobody asked for it and the ceiling
is the part that actually limits damage.
**Confidence:** high

### Q13. Chunk size and one-chunk-in-flight — configuration or constant? — DECIDED: written into `PLAN.json` at plan time, defaulting to 2,000, with the issue-84 rationale recorded in the plan itself.

**Blast radius:** LOW
**Why:** It is a workaround for a known defect with a measured shape (claim cost
217 ms at 19k pending rows vs 10.4 ms at 2k), and a workaround baked in as a
constant becomes permanent silently. Putting it in the plan with its rationale
means the day issue 84 is fixed, the plan's default changes and the reason is
right there.
**If you disagree:** a constant is less to carry, and the chunk size genuinely
should not vary run to run. But it must vary once, when the defect is fixed, and
that is the case this guards.
**Confidence:** high

### Q14. Is the full standing run part of acceptance? — DECIDED: no. Acceptance is a complete dry-run rehearsal of all five stages that spends zero tokens, using the 19,268 verdicts already in the store. The real run is an operator action behind Chris's approval.

**Blast radius:** NONE
**Why:** The conductor must not spend real money, and it does not need to: the
store already holds a completed run, so plan → quote → approval-refusal checks →
export → shard → `load --dry-run` can all be exercised end to end for free. That
rehearses everything except the token spend, which is the one thing the approval
gate exists to put in front of a human.
**If you disagree:** a small paid pilot (25 items, ~0.26M tokens) would also
exercise the live gateways, which the dry-run cannot. That is a reasonable
addition if Chris wants it, and it is a flag on t11, not a redesign.
**Confidence:** high

### Q15. What I could not decide and did not guess

Four external facts left unreconciled:

1. **ARGO's and MAG's effective rates.** Not derivable from any artifact here;
  every pricing path 404s. The design assumes a human-supplied rate table and a
   per-run balance delta, and reports `unpriced` otherwise. **This is the one
   permitted flag-not-question: the PRD's dollar-quote decision rests on a fact
   no artifact I can read contains, and I assumed "no rate unless configured"
   rather than substituting list prices.**
2. **The granularity of the clearinghouse ingest lock.**
  `ClearinghouseLockUnavailableError` exists; whether it is per-table, per-run or
   global, and therefore whether two shard runs can load concurrently, I did not
   establish. It affects only whether two pending runs must be loaded in series.
3. **Whether** `endpoint_state`**'s halt path actually stops a running worker** on a
  budget overrun, as opposed to merely recording the halt. The fields and the
   enum exist; the end-to-end behaviour is unverified. t9 is instructed to verify
   it and to report rather than assume. If it does not, the ceiling needs a second
   mechanism and Q6 changes.
4. **Why the uncompressed JSONL had not reached kbhub** while the sync client
  reported the directory up to date. Albert's read and mine disagree about the
   file's presence at the same moment. The design routes around it — the published
   artifact is gated on `run.json` and per-batch sha256 — but the cause is
   unknown, and "the sync client said up to date" is now established as not a
   completeness signal.



## Gotchas and Unintuitive Consequences

**G1. Running** `kbu council doctor` **from a clean clone of** `main` **will report the
entire ARGO bench unavailable, and the gateway is fine.** The readiness test
reads the gateway's display spelling while the call accepts only `internal_id`,
so readiness is the exact inverse of the call. Anyone debugging this will suspect
credentials, the gateway, or the network, and will be wrong on all three. Until
t1 lands, the council runs **only** on h100's editable install, from a commit
that exists in no shared branch.

**G2. The dry-run rehearsal in acceptance will find almost nothing wrong with the
quote.** It exercises the plumbing, not the arithmetic — the only check on the
quote's accuracy is the replay against the completed run's recorded 199.2M
actual, which is a different test. A green acceptance does not mean the quote is
right.

**G3. A** `mapping_hash` **bump re-selects all 47,341 functions, and the retrieval
backend is expected to change.** The module docstring says so in terms: "the
backend underneath is expected to change (e.g. to Vibhav's Type 4 fusion)". So a
retrieval upgrade is a full re-run of the corpus at current prices, and the
quote is what makes that visible. This is the correct behaviour and it will still
be a surprise the first time.

**G4. Recording** `no_candidate_matched` **as a result means a retrieval improvement
cannot fix those items until the** `mapping_hash` **changes.** 4,893 items declined
because the right GO term was never on the menu. Once recorded at this
`parameter_set_hash` they are "done" and stage 1 will not re-select them — which
is right, because re-running them against the same menu would decline again. But
it couples the fix to a version bump, and the bump costs the whole corpus (G3).
There is no cheap path to "re-run only the 4,893 whose menu got better".

**G5. Reuse makes the second run of anything look suspiciously cheap, and the
quote will be correct.** The completed run's chunk durations fell from 4,483 s to
~3,400 s purely as the store's completed-call set grew. A re-plan after a small
roster change may quote a tenth of the first run's tokens. That is real —
`prompt_hash` keys on the canonical model and not the endpoint alias — but it
reads like a bug.

**G6. Cancelling a job does not stop it costing.** `select_claimable` filters
paused and cancelled jobs *after* the index has found the rows, so a paused job's
57,746 pending calls were still scanned and sorted on every claim — 50% pure
waste. A run directory left with a paused job in the store taxes every later run
until the job is cancelled, and `jobs cancel` is terminal.

**G7.** `estimate_rows()` **returns** `None` **for jsonl and tsv on purpose**, so a
manifest over a text source cannot report a row count at plan time. This is why
Q11's parquet decision also improves `kbu clearinghouse plan`'s usefulness — an
effect of a format choice on an unrelated command.

**G8. A load that joins nothing currently succeeds.** Fill-only means a batch
with no surviving rows "is committed without a write at all" — which is exactly
right for a re-run and exactly wrong for a wrong key. The two are
indistinguishable to the existing code, and distinguishing them is t10's zero-join
guard. Without it, the 0-of-19,268 hash mismatch would have been reported as a
clean idempotent no-op.

**G9. The retry pass changes the item set the quote was written against.** A
retry re-pays only the failed seats (the completed run's retry round 1 reused 347
attempts and MAG's token counter did not move at all), so the retry is ~0.3% of
run cost — but it happens *after* the approval, and a ceiling set tightly against
the quote could halt the retry pass rather than the run. The ceiling must carry
headroom, and the plan should say how much.

**G10.** `/scratch` **is shared and has filled before.** The council store, the ranked
file and the verdict intermediates all live there by this design. A co-tenant
filling the filesystem produces a harness ENOSPC that can present as a clean exit
with no commit, which has already happened once on this host. The run directory in
Dropbox is what survives; `/scratch` is not.

**G11. The sync client may deliver** `run.json` **BEFORE the bytes it describes.**
Nothing guarantees a sync client preserves the producer's write order, so
"manifest written last" is a producer-side discipline that buys the consumer
nothing on its own. It is the per-batch sha256 verification, not the manifest's
presence, that proves arrival — which means the hand-off is safe only because the
existing ingest gate already checks it, and a trigger that short-circuits to
"`run.json` exists, load it" would reintroduce the hole.

**G12. The Iceberg commit and the receipt are two unrelated writes, so a crash
between them loses the acknowledgement while the data is safely landed.** A
producer reading "no receipt" as "not loaded" would then ask for a reload. The
load is fill-only so a reload is harmless, but it is also slow and would look
like a loop. The mitigation is that `verify` is re-derivable from the ledger and
the snapshot history, so the right response to a missing receipt is to re-export
it, never to re-load.

**G13. A relationship assertion evaluated on a join's RESULT passes vacuously
when the join is empty.** That is why the zero-join guard checks its *inputs* —
non-empty source, non-null keys, a coverage floor — rather than its output. A
"referential integrity" test written the obvious way would have passed on the
0-of-19,268 mismatch, which is why this defect class survives having tests.

**G14. The quote gets WORSE as the roster changes and better as it stabilises.**
The token model is fitted from each seat's own measured attempts, so a brand-new
seat has no history and its line falls back to a projection — the quote's exact
fraction drops precisely when a quote is most wanted, because a roster change is
the thing someone is trying to price. The quote must therefore report which seats
it has history for and how much.

**G15. Two concurrent runs share one council store and one daemon.** The daemon
drains every claimable job, which is why one chunk at a time is load-bearing. Two
run directories both in stage 4 would interleave their chunks and defeat the
chunking entirely. Nothing in this design prevents it; the status verb should
make it visible, and the runner should refuse if another run's job is claimable.

**G16. THREE fingerprints now exist and they are not interchangeable — this is
the likeliest thing for a later reader to get wrong.** `bench_fingerprint`
(new, model-only) PARTITIONS the corpus. `roster_fingerprint`
(`Roster.fingerprint()`) additionally covers `endpoint_kind`, the route
spelling, `max_output_tokens`, `temperature` and `extra`, and is PROVENANCE
only. `route_fingerprint` covers the endpoint alias, base URL and account and is
also provenance. Substituting `roster_fingerprint` for `bench_fingerprint`
anywhere in the partition path silently re-partitions 47,341 rows on the next
routing change, and the symptom is not an error — it is stage 1 reporting the
whole corpus un-run and the quote dutifully pricing it. Round 1 of this PRD made
exactly that mistake.

**G17. A transport change makes a cheap run quote like an expensive one, and the
quote is not wrong.** Because `prompt_hash` keys on `endpoint_kind` (Q8),
re-routing a seat invalidates that seat's stored answers. The corpus is not
re-selected (the bench did not change), so nothing already landed is re-paid —
but any still-pending item loses its partial credit. The quote's reuse-miss
attribution line exists so this reads as "transport change" rather than as a
corpus re-run. Expect it the first time a seat moves off codex.

## Sources Consulted

**Read in full, with the symbols that mattered:**

- `KBUtilLib/src/kbutillib/domains/genome/ontology_mapping/ontology_mapping_utils.py`
— `mapping_version`, `function_to_go`, `function_to_go_council` (the
`by_text`/`item_of_text` dedup at :196-212, the `query_id` propagation at :231,
the `council_error` handling at :234-241), `write_ranked`/`read_ranked`
(atomic `.partial`, the `mapping_hash` refusal at :309), `write_council`.
- `KBUtilLib/src/kbutillib/interfaces/cli/council.py` — full command inventory
(`doctor`, `submit`, `run`, `daemon{start,stop,status}`,
`jobs{list,status,results,cancel,delete,rerun}`, `query{list,show,register,retire}`,
`calls`, `endpoints`, `db`, `models`); `_submit_options` at :520 for the input
contract. **No quote, estimate or pause verb exists.**
- `KBUtilLib/src/kbutillib/interfaces/cli/clearinghouse.py` —
`_require_in_pod_or_refuse` at :293 (guards load/verify only), `_build_known`,
`_build_results`/`results_cmd` (`--parameter-set-hash`, `--default-parameters`),
`plan_cmd`, `shard_cmd`, `load_cmd` (`--run-id`, `--dry-run`, `--reconcile`),
`verify_cmd`.
- `KBUtilLib/src/kbutillib/domains/kbase/berdl/clearinghouse_manifest.py` — the
TOML contract, `DERIVATIONS`, `[source.hash]` modes (`raw_column` /
`precomputed` / `contig_set`), `[source.result]` with `parameter_set` and
`parameter_set_column`, `observed_at_column`, plan-time validation.
- `KBUtilLib/src/kbutillib/domains/kbase/berdl/clearinghouse_shard.py` —
`FileSourceAdapter` in full (`_format` at :712, `_iter_jsonl` at :785,
`estimate_rows` returning `None` for text at :736), and the "runs anywhere"
statement in its docstring.
- `KBUtilLib/src/kbutillib/domains/kbase/berdl/clearinghouse_ingest.py` —
`verify_batch_file`'s five checks at :377, `file_sha256`, `read_batch_table`'s
parquet-only refusal, the `run.json` requirement at :517, `run_manifest_version`
gating, `_registry_first` (registry before results).
- `KBUtilLib/src/kbutillib/domains/ai/council/store/schema.py` — `calls`
(`prompt_hash` as the reuse key, `fresh_usage`, `ix_calls_prompt_hash_status`,
`ux_calls_one_live`), `attempts` (`usage`, `queued_at` and the service-time
comment), `endpoint_state` (`budget_epoch`, `epoch_tokens`, `halt_kind`).
- `KBUtilLib/src/kbutillib/domains/ai/council/types.py` — `prompt_hash` at :264
(including `endpoint_kind`, with its stated rationale),
`seat_generation_params` at :250 ("so the fingerprint can be computed without
instantiating a backend"), **`Roster.fingerprint()` at :507** (round 2: covers
`role`, `endpoint_kind`, `model`, `canonical_model`, `max_output_tokens`,
`temperature`, `extra` — i.e. it includes the transport, which is what made
round 1's Q2 wrong), and `route_fingerprint` at :304 ("answers 'where did this
come from', not 'is this the same question'").
- `KBUtilLib/src/kbutillib/domains/kbase/berdl/examples/clearinghouse_example_seed.toml`
— a worked three-source manifest, the `parameter_set = {}` default-run
declaration and `DEFAULT_PARAMETER_SET_HASH`, and the "source label is a
provenance label, not a tool version" convention.
- `/scratch/chenry/council/export_results.py`, `_adp1_prep.py`, `run_chunks.sh`,
`kbu-env.sh` — the current pipeline, in full. `_adp1_prep.py:18` is where the
`abstained` filter that explains the 760 lives.
- `miles/agent-io/research/2026-10-20-function-to-go-council-composition-and-schedule.md`
— rounds 1–9 in full.

**Measured live, 2026-10-22:**

- `kbu clearinghouse tables` from h100 → `function_entity` 47,341,
`function_content` 47,341, `function_result` **0**, partitioned by `source`.
Sixteen successful delta-API queries; `health` then 422'd on one table.
- The 760-definition partition, computed against `ranked.jsonl.gz` (47,341 result
rows, short keys `q`/`t`/`s`/`a`/`c`) and `priority_adp1_pmi.tsv` (22,255 rows,
20,036 distinct `entity_hash` = 20,036 distinct `definition`).
- `git cat-file` in `~/Dropbox/Projects/KBUtilLib`: `0825c5a` is **not a valid
commit name**. Dropbox `main` is `b9a6bb6`; `~/projects/KBUtilLib` HEAD is
`0825c5a` on `main`.
- `stat` on `~/projects/KBUtilLib` and
`/scratch/fliu/hub_home/chenry/H100Projects/KBUtilLib` → `64517:5167606134`,
the same inode: one tree, the usual h100 path alias.
- `git show 0825c5a` — the readiness fix and its measured ARGO evidence.
- The published hand-off: `MANIFEST.json`, a verdict row from
`function_to_go_verdicts.jsonl`, and the three files' sizes.
- `maestro list --status running/submitted` before dispatching the external scan.

**Read and found uninformative, recorded so the absence is visible:**

- `council/default_config.yaml` — searched for a budget block; it carries only the
`user` provenance-label comment. The budget lives in `config.py`'s validation
(`:627`, "an unmeasurable budget is unenforceable") and in `endpoint_state`, not
in the shipped config.
- `clearinghouse_backfill.py` — `available_presets`/`resolve` confirm presets are
package data resolved by name with shipped names winning over same-named local
files. Nothing about how a preset is registered beyond file placement, which is
all t8 needs.
- `interfaces/cli/` directory listing — confirmed there is **no** `ontomap` CLI
module today, so Q7's new group is genuinely new surface rather than an
extension.

**Routed, not read:** Albert's trigger reply
`Dropbox/AIPlatform/channels/trigger-inbox/replies/fngo-clearinghouse-load-20261022.json`
— kbhub sees `~/Dropbox/Jobs/`, `.gz` and `.tsv` verified by sha256, the
uncompressed `.jsonl` absent, and his drop-point/shape recommendations. Read in
full and folded into Q9, Q10, Q11 and Q15.4.

## Out of Scope

- **Council composition.** Dropping the third chair (~22% of tokens, 85% pairwise
agreement), letting a 2-of-3 majority settle without a critic (~19%), and the
candidate-menu size (~30% of tokens, but now a joint yield/cost question
because 25.4% of items declined for `no_candidate_matched`) all belong to
continuation [145]. This PRD makes each choice priceable; it does not make any
of them.
- **The retrieval-recall limit.** 25.4% of items declined because the right GO
term was never on the closed menu. That is upstream of the council and is the
largest single lever on *yield*. Not touched here.
- **KBUtilLib issue 84** — getting `claim_calls` off the worker's event loop, or
denormalising the ordering columns onto `calls` so the temp B-tree disappears.
This PRD implements the chunking workaround and names the issue so the
workaround is removable.
- **KBUtilLib issue 83** — `test_static_headers.py`'s MAG-endpoint assertion
failing on any machine with a user MAG endpoint configured. Pre-existing; every
criterion here is regression-framed because of it.
- **Making** `invalid_output` **retryable in the engine**, or detecting ARGO's silent
truncation. The retry pass is a pipeline stage here; fixing `RETRYABLE_KINDS`
or adding truncation detection is an engine change and a separate task.
- **gzip support in** `FileSourceAdapter._iter_jsonl` — four lines, independently
useful, not needed because the published artifact is parquet (Q11).
- **The kbhub-side trigger.** Albert's, by scope. This PRD ships the contract.
- **Authenticating the approval.** Q12; no key material exists to do it with.
- **Loading the existing 19,268 verdicts.** Albert has opened that as work on his
side and will load from the already-published `.gz`. This PRD's acceptance
rehearses the *new* path against the same store without re-publishing.



## Further Notes

**The standing cadence.** Stages 1 and 2 spend nothing, so they are safe to
schedule. A weekly `council-plan` + `council-quote` on h100, writing a run
directory only when the un-run count exceeds a threshold, is the natural shape and
would make the pending work visible without anyone asking. That is a scheduling
decision, not a build one, and it is deliberately not in the taskplan — but it is
what "automatically placed in a job folder" means once the verbs exist.

**On the two cost figures to watch.** 10,334 tokens per item and a 24.1% meta
rate are the two numbers the quote is built on, and both are now measured over
19,268 items rather than projected. Three earlier meta-rate estimates (16.7% at
n=6, 19.2% at n=73, 38.7% at n=475) were each honestly caveated and each wrong;
only the full set settled it. The quote should record the sample size behind its
meta rate for exactly that reason, and a future roster change invalidates it.

**What this PRD deletes.** On landing, `/scratch/chenry/council/export_results.py`,
`_adp1_prep.py`, `run_chunks.sh`, `chunk_progress.sh` and `kbu-env.sh` are all
superseded. They should be removed rather than left as a second, divergent path —
and `0825c5a` in h100's local clone should be discarded once t1 lands an
equivalent fix on `main`, so that two fixes for one defect do not coexist.

**The build is necessarily sequential, and that is a scheduling fact worth
stating.** Every task is in KBUtilLib, and the taskplan validator refuses two
tasks in one parallel group that target the same repo — concurrent branches in
one repo collide (dev 1032). So this is eleven phases of one task each rather
than five phases of two or three, and the wall-clock expectation should be set
accordingly. `depends_on` still names each task's *real* predecessor rather than
merely the prior phase, so the dependency graph remains honest even though
execution is serial: if the collision constraint is ever lifted, the plan
re-parallelises without re-authoring.

## Acceptance Criteria

1. `kbu council doctor` reports all five seats ready from a clean checkout of `main`, with no local-only commit applied.
2. `kbu ontomap council-plan` writes a `PLAN.json` whose submit, abstain and fan-out counts reproduce the 19,276 / 760 / 20,036 partition when run against the recorded ADP1+PMI priority set.
3. `kbu ontomap council-plan` excludes any function with a current-state `function_result` row at the run's `parameter_set_hash`, and includes the same function when the parameter set differs.
4. `kbu council quote` completes against a seeded store with network access unavailable, proving it makes no API call.
5. `kbu council quote` reports every line tagged `exact` or `projected`, and names the sample size behind its token model and its meta fire rate.
6. `kbu council quote` enumerates the per-item call graph: three unconditional proposer calls, one unconditional critic call, a fractional meta call, and a paid-repair line for the retry pass.
7. `kbu council quote` emits a point estimate and an upper band, and reports the exact fraction of the total it is projecting.
8. `kbu council quote` reports reuse per seat, and a replay against the completed 19,276-item run agrees with the recorded actual on its exact lines.
9. Changing one seat's model reduces reuse for that seat only; bumping the query version reduces reuse for every seat.
10. `kbu council quote` reports `unpriced` for a gateway with no configured rate and never substitutes vendor list rates for a gateway's own rate.
11. `kbu ontomap council-run` refuses, naming the failed check, for each of: a missing approval, a `quote_hash` mismatch, a `plan_hash` mismatch, a roster-fingerprint mismatch, an expired approval, and an approval already consumed by another run.
12. The approved token ceiling halts a run in progress, demonstrated end to end against a deliberately low ceiling, and the halt is recorded with its reason.
13. `kbu ontomap council-run` refuses to start when either `ARGO_USER` or the AMSC token is unresolvable, rather than starting with parked seats.
14. A run resumed after a killed chunk re-executes only that chunk, and the retry pass runs to its round cap before the run is reported complete.
15. An account-wide request pacer limits dispatch to the configured requests-per-minute across all seats sharing one endpoint account, verified under a service time short enough that the concurrency caps alone would exceed it.
16. A sustained rate-limit response halts the run rather than being retried into.
17. `kbu ontomap council-export` emits one parquet row per input `query_id`, with `query_id` taken from the ranked file and never resolved through a text join.
18. All three decline classes — `abstained:*`, `no_candidate_matched`, `contentless` — appear in the export with their reason, and a `council_error:*` item does not appear at all.
19. The shipped `function-to-go-council` manifest preset resolves by name and passes `kbu clearinghouse plan` without error.
20. `kbu clearinghouse shard` publishes the run under the run directory with a `run.json` carrying a sha256, a row count and a hash range per batch.
21. A load whose source keys resolve to zero target rows exits non-zero, with the assertion evaluated on the inputs rather than on the join result; a shard run with zero source rows is likewise refused as a producer failure.
22. The load reports non-null key coverage, ambiguous key mappings, and unmatched keys, before committing.
23. A batch whose bytes do not match its published sha256 is treated as still-syncing and retried; after the configured transfer deadline it is reported as a failed delivery naming the batch and the expected versus observed digest.
24. `LOADED.json` reconciles against items submitted, not rows written, and reports items submitted, items with a verdict, items unresolved, result rows written, declines by reason, the target snapshot id, and the `parameter_set_hash`.
25. A missing receipt is re-exported from the ingest ledger rather than triggering a re-load, and re-exporting reproduces the same receipt.
26. `kbu ontomap council-status` reports corpus coverage by lifecycle state and by `parameter_set_hash`, so that work paid for but not landed, and partial-version coverage, are both visible.
27. No file in the run directory is written by more than one machine, and no shared mutable status file exists.
28. The full five-stage rehearsal runs end to end against the already-stored verdicts and spends zero tokens.
29. The failure-oriented rehearsal passes: marker arriving before payload, one missing file, a wrong hash, a 0% join, a partial join, a duplicate consumer, a crash immediately after the commit, an expired approval, a job changed after approval, and a sustained rate-limit response.
30. An operator runbook in the repo carries the five stages, the measured concurrency and chunk constants with their rationale, and the KBUtilLib issue 84 reference that makes the chunking workaround removable.
31. `bench_fingerprint` is computed from `{role, canonical_model}` per seat only: changing a seat's transport (endpoint kind, alias, base URL or account) leaves it unchanged, while changing a seat's model changes it.
32. `kbu ontomap council-plan` re-selects nothing after a transport-only change, and re-selects the whole corpus after a bench, query-version or `mapping_hash` change.
33. The clearinghouse `parameter_set` carries `bench_fingerprint` and not `roster_fingerprint`; `roster_fingerprint` and the per-seat `route_fingerprint` appear in the result payload as provenance.
34. `PLAN.json` records the per-seat transport allocation for the run as an explicit field.
35. `kbu council quote` splits reuse misses into new-item, bench-change and transport-change, and reports the three counts separately.
36. `kbu ontomap council-run` accepts an approval across a transport-only change while warning and naming the re-routed seats, and refuses it across a bench change.
37. No test that passed on the base commit fails on the branch.

