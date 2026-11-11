# Work Record — anthropic-acceptance-rehearsal

## task_id
anthropic-acceptance-rehearsal (phase 7 of 7, PRD `anthropic` api type)

## DELIVERY STATE: LIVE-ACCEPTANCE-PENDING

**No live run was performed.** No `ANTHROPIC_API_KEY` was available in the
build environment (checked 2026-11-01), and none was put there: this is a
public repository. The rehearsal script is the deliverable. It has **not**
been run against the real API, and nothing here claims it has.

The PRD is **not accepted**. All of its code is merged on the branch, but the
criteria that need the real API have not been checked by anyone:

| PRD criterion | State | What the operator must supply to close it |
|---|---|---|
| 1 | **pending, live-only** | a real API key, the seated models reachable, an isolated new council store, an explicit spending cap |
| 2 | **pending, live-only** | same |
| 3 | **pending, live-only** | same |
| 4 | **met offline, by design** | nothing; see below |
| 6 | **pending, live-only** | same |

The PRD bundle is not in this repository, so this record doesn't restate the
criteria's wording. The six checks below are what the script asserts. Mapping
each check to a criterion is the operator's call when they read the report.

Criterion 4 says an endpoint whose provider reports no usage object halts with
`budget_usage_unknown`. It can't be exercised live, because there's no
supported way to make the Anthropic API omit its usage object. Its offline
counterpart is
`tests/domains/ai/council/test_anthropic_engine_budget.py::test_AC4_no_usage_object_halts_budget_usage_unknown`.
That test runs the real `AnthropicBackend`, the real `CouncilWorker` and a real
store; only the HTTP response is injected, through `httpx2.MockTransport`. The
envelope comes from the phase-1 FIXTURE BASIS: it parses with the SDK, but it
was written by hand, not captured from the API. A second test in the same file
covers the contrast case. A usage object carrying only Anthropic's field names
lands in `calls.fresh_usage` as `{"prompt_tokens": 29, "completion_tokens": 7}`
and halts `budget_exhausted`.

## How to run it

```bash
pip install 'kbutillib[anthropic,council]'      # Python >= 3.10
export ANTHROPIC_API_KEY=...                    # never on the command line
python scripts/council/anthropic_acceptance_rehearsal.py \
    --work-root /scratch/$USER/anthropic-rehearsal-$(date +%Y%m%d) \
    --rpm anthropic-haiku-4.5=<your limit> --rpm anthropic-sonnet-4=<...> \
    --confirm-spend
```

Optional flags:

- `--proposers`, `--critic`, `--meta` choose the seats. Only the five ids
  confirmed during design are accepted; anything else is refused.
- `--rates FILE` supplies `rates` entries, so check 5 sees priced rows.
- `--max-output-tokens` sets the per-seat ceiling (default 2000, to leave room
  for thinking).
- `--strict` exits non-zero when a check is NOT ASSERTED.

Reports are written to
`agent-io/reports/<date>-anthropic-acceptance-rehearsal.{json,txt}`. The JSON
is written first, following the convention in `acceptance_rehearsal.py`.

### Refusals (exit 2), all before anything is sent

- `$ANTHROPIC_API_KEY` is unset or empty. The script never falls back to a
  fixture, stub or recorded response.
- The SDK is not installed.
- A model id outside the design-confirmed list.
- `--work-root` is not empty. The store must be isolated and new.
- No `--confirm-spend` (or `KBU_ANTHROPIC_REHEARSAL_CONFIRM=yes`). The script
  prints the estimated ceiling first, then refuses.

## What it does and what each check proves

The script generates a config with one endpoint per rate-limit bucket seated,
each with `account` = bucket and `billing_wallet` set. It adds one extra
endpoint, `rehearsal-budget`, on the first proposer's bucket with
`max_total_tokens: 1` and `max_concurrency: 1`.

It creates a new SQLite store under `--work-root` and registers the packaged
example query type (`queries/examples/custom_example.yaml`), so nothing the
user wrote is involved. It then runs one job with one item through the real
`CouncilWorker`, making one call per seat that fires. The meta seat fires only
on a split decision, and when it doesn't fire the report says so.

1. **Usage reaches the store.** Every completed call row's
   `calls.fresh_usage` has non-zero `prompt_tokens` and `completion_tokens`.
   The values are read from the store, which is what the budget and every
   later reader see, not from the in-memory result. This is the one check that
   can catch the zero-spend defect, which a fixture cannot.
2. **The budget sees it.** The real `council.engine.worker._token_total`,
   imported and called on the stored usage, is non-zero.
3. **Rate-limit header.** For each endpoint that completed a call, the script
   asserts that `anthropic-ratelimit-requests-limit` was observed and compares
   it with the configured `max_requests_per_minute`. A mismatch is reported as
   a FINDING, not a failure, because the header reports the most restrictive
   limit in effect and a workspace limit can shadow the model's.
   **The observation is process-local.** It is stored on the backend instance
   (`AnthropicBackend.last_ratelimit`). It doesn't cross a process boundary
   into a queue worker's submitter, and with debug logging off it is written
   nowhere. The script reads it from the calls it made itself, which is the
   one context where the value is guaranteed to be in scope. It is not
   generally available.
4. **Free preflight.** `llm.preflight_api` reports `ready` for every endpoint.
   This step also lists `GET /v1/models` (free) and records a FINDING when a
   served id isn't literally in the listing, which is the `doctor`/submit risk
   below.
5. **Quote.** `quote_plan` runs over two NEW items, using a token model
   fitted from this store's own calls. It must publish one gateway row per
   rate-limit bucket. Priced rows must have `by_model` dollars that sum to the
   gateway total. Rows without a rate must carry no dollar figure and be
   reported `unpriced`/`partial`.
6. **The budget bites.** A second job is worked by a worker that holds only
   the `rehearsal-budget` backend, so it can spend nowhere else. The endpoint
   must halt `budget_exhausted`, the halt message must quote `spent N` and the
   budget, and exactly one call must complete there. The halt stops the next
   call.

## Approximate cost

The script prints an upper bound before spending. With the defaults that is
at most 5 calls (2 proposers, critic, meta if it fires, 1 budget call), each
with an assumed 3000 input tokens and the 2000-token output ceiling, priced at
deliberately pessimistic assumed ceilings of $30/MTok in and $150/MTok out.
That comes to **≤ ~$1.95**.

These are assumed ceilings, not prices. The real spend should be a small
fraction of that, because prompts are about 1k tokens and answers are short.
Check the console balance before and after.

## Not covered by the script, and why

- **Criterion 4.** It can't be produced live; see above.
- **Cross-endpoint wallet ceiling.** `max_total_tokens` is per endpoint and
  nothing adds the budgets up. The design deliberately leaves this open, so
  the script doesn't test a ceiling that doesn't exist.
- **The two spend-limit halts** (HTTP 400 `You have reached your specified API
  usage limits`, HTTP 429 `enforced_spend_limit_reached`). Triggering either
  live would mean exhausting a real limit. They are covered offline by the
  backend's taxonomy tests. The message prefix and error code come from
  documentation and are **unverified against the live API**.
- **Whether the live API accepts `Authorization: Bearer <api key>`.** SDK 2.3.0
  sends that header rather than `x-api-key` (phase-1 record). The first live
  call answers the question: a 401 on every endpoint would point here.
- **`kbu council doctor` / `submit` seat readiness.** For non-CLI kinds,
  readiness means "the model listing names the `served_name`". If the live
  `/v1/models` returns only dated snapshot ids, `doctor` and `submit` would
  call Anthropic seats unavailable. The script uses `CouncilWorker.asubmit`
  directly, so the rehearsal itself isn't blocked, and check 4 reports the
  mismatch as a FINDING. **Unverified.**

## Verification done here (offline only)

The script was driven once end to end by a throwaway, uncommitted driver. The
driver patched `AnthropicBackend` to use a mock transport. All six checks
completed and the reports rendered. That run found and fixed one bug: check 3
asserted a header on an endpoint that never made a call (meta didn't fire).
That run tested the script's plumbing only; it is **not** a rehearsal, and
none of its numbers mean anything about the API.

Committed tests for the script cover only its refusals
(`tests/domains/ai/council/test_anthropic_rehearsal_script.py`). The script
has no fixture mode to test, by design.
