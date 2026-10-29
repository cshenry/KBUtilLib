# kbu-anthropic-api-type-v1 — the `anthropic` api type and an exactly-priced council wallet

## Research Report

### Important findings

**1. The closed `Literal` is the only hard blocker in the type system, and the store needs no migration.**
`SeatSpec.endpoint_kind` (`council/types.py:489`) is
`Literal["argo", "openai", "claude_cli", "codex_cli", "fake", "replay"]`.
A seat on a sixth kind raises a pydantic `ValidationError` at roster
construction, however correctly the api type is registered.
`ENDPOINT_KINDS` (`council/types.py:112`) is the same list in tuple form.
The store column is `sa.String(32)` with **no CHECK constraint**
(`council/store/schema.py:225`, `_ENUM = sa.String(32)` at line 125),
so no migration is owed and no task should write one.
The llm registry itself is already open: `_require_registered_kind`
(`council/config.py:1333`) asks `llm.api_types()` rather than a local list,
so registering `anthropic` as a built-in is sufficient for config load
to accept `kind: anthropic`.

**2. The pricing bucket and the rate-limit group are the SAME config key, and
on Anthropic that is lucky rather than a conflict.**
This is the finding that shapes the whole design.

- `quote.py::_gateway_of` returns `cfg.get("account") or seat.endpoint`.
- `engine/worker.py::_build_limits` groups endpoints by
  `cfg.get("account") or f"endpoint:{endpoint}"`.

So `account` simultaneously decides which dollar rate a seat's tokens are
priced at and which semaphore + rate bucket its calls are paced by. For MAG
those two coincided because MAG had one account-wide limit and one effective
price. The brief for this PRD assumed the same shape and predicted a conflict:
split the aliases for pricing and you get N independent pools against one
account-wide limit — the exact 427-of-500 failure.

**That prediction does not hold for Anthropic, and the reason is a fact about
Anthropic's limits, not about this code.** Rate limits are enforced **per
model-class bucket**, not per organisation
(`https://platform.claude.com/docs/en/api/rate-limits`, read 2026-10-29:
"Each row in the following tables has its own RPM, ITPM, and OTPM limits").
Claude Opus 5, Claude Sonnet 5 and Claude Haiku 4.5 each hold a *separate*
RPM/ITPM/OTPM allowance; Opus 4.8/4.7/4.6/4.5 *share* one bucket, as do
Sonnet 4.6/4.5 and the Fable pair.

Therefore **one endpoint alias per rate-limit bucket**, with `account` set to
that bucket's name, is correct for pacing *and* splits the pricing bucket at
exactly the granularity pricing needs. Three Claude seats on three buckets are
three genuinely independent pools because the provider enforces them that way.

**3. The 1:1 correspondence between rate-limit bucket and list price is a
coincidence of the current model table, not a guarantee.**
Every current rate-limit bucket happens to contain models at one price
(Opus 4.x: all $5/$25; Sonnet 4.x: all $3/$15; Opus 5, Sonnet 5, Haiku 4.5 each
alone in theirs). So keying `rates` by the bucket is exact **today**. Nothing
makes it stay exact — Anthropic can add a model to an existing bucket at a new
price without telling us, and `Claude Haiku 5.5` already demonstrates a worse
version of the problem (below). The design therefore prices per **canonical
model** inside each gateway bucket, which makes the quote exact by construction
rather than exact by coincidence.

**4. `Claude Haiku 5.5` cannot be exactly priced by the current `rates` schema
at all.** Its input price is **tiered by prompt size**: $0.10/MTok for prompts
up to 100,000 tokens and $0.50/MTok above
(`https://platform.claude.com/docs/en/about-claude/pricing`, read 2026-10-29).
`RateConfig` (`council/config.py:744`) carries a single `input_per_mtok`. No
keying strategy fixes this; it needs a size-tiered rate type. Declared out of
scope, and made *visible* rather than silently mispriced.

**5. Sending `temperature` to the current Claude models returns HTTP 400 — and
the repository already knows this through ARGO.**
`llm/apis/http.py` carries
`ARGO_NO_TEMPERATURE_MODELS = frozenset({"claudeopus47", "claudeopus48", "claudesonnet5"})`
with the comment "Models that reject an explicit temperature; sending one fails
the call." Sampling parameters (`temperature`, `top_p`, `top_k`) are **removed**
on Claude Sonnet 5, Opus 5, Opus 4.8 and Opus 4.7 and return 400.
`SeatSpec.temperature` defaults to `0` (not `None`), and
`HttpBackend._params` sends `temperature` whenever it is not `None` — so a
naive port would fail **every call on every current Claude model**, loudly but
with a cause nobody would read as "the field default was never chosen for this
provider". Closed by omitting sampling parameters unless a seat sets one
explicitly (see Q4).

**6. Anthropic's truncation signal is `stop_reason: "max_tokens"`, not
`finish_reason: "length"` — and truncation is LIKELY, not hypothetical.**
`HttpBackend._complete` tests `reply.finish_reason == "length"` to produce the
"a cut-off answer is not a vote" failure. Anthropic says `max_tokens`. Worse,
thinking is **on by default and adaptive** on Opus 5 and Sonnet 5, thinking
tokens are billed and counted as **output** tokens, and `SeatSpec`'s default
`max_output_tokens` is **4000**. A seat left at the default will frequently
spend its whole output budget thinking and return a truncated answer, which
without the mapping is diagnosed as "reply contained no JSON object" — the
wrong cause, on the wrong seat, repeatedly.

**7. The wallet-exhausted signal arrives as an HTTP 400 and is currently
neither halted nor retried.** Three distinct money/limit responses:

| Condition | Response | Current mapping via `HttpBackend._status_error` | Correct |
|---|---|---|---|
| Per-minute rate limit | 429 **with** `retry-after` | `TransportError`, retryable, `rate_limited=True` | correct as is |
| Organisation monthly spend cap | 429, **no** `retry-after`, `error.details.error_code = "enforced_spend_limit_reached"` | same as above — retried, then halted after 5 with the message "lower max_requests_per_minute" | `QuotaExhausted` (halts endpoint, not retryable) |
| **Operator-set spend limit** | **HTTP 400** `invalid_request_error`, message begins `You have reached your specified API usage limits` | `InvalidOutput` — **not** retryable and **does not halt** | `QuotaExhausted` |

The third row is the one that will actually happen, because setting a limit at
$200 is the obvious way to fence a $200 credit. Under the current mapping the
run does not stop: it fails every remaining item individually with
`invalid_output`, which reads like a model problem rather than an empty wallet.

**8. `input_tokens` is not the input token total.** Anthropic's own docs:
`total_input_tokens = cache_read_input_tokens + cache_creation_input_tokens + input_tokens`
— `input_tokens` counts only tokens *after the last cache breakpoint*. With no
caching all three are consistent (the two cache fields are 0), but writing the
mapping as the bare field would make the budget silently under-report the day
anyone enables caching. The mapping is written as the **sum** (Q2), and the
backend **refuses** a request carrying `cache_control` in v1 rather than
mis-billing it, because cache reads are billed at a fraction of base input and
summing them into a single `prompt_tokens` would make the dollar figure wrong
even when the token figure is right.

**9. The account's limits are machine-readable — a first for this project.**
Every response carries `anthropic-ratelimit-requests-limit`,
`-requests-remaining`, `-requests-reset`, and the `-input-tokens-*` /
`-output-tokens-*` families. Every ARGO and MAG usage, billing, pricing and
quota path 404s, which is why `quote.py` refuses to substitute list rates and
why `max_requests_per_minute` has always had to be guessed or measured by
balance arithmetic. Here the configured value can be **checked against the
provider's own answer** from a single real call. This PRD does not build a
self-tuning pacer (Q7) — it records the headers so the configured number is
verifiable, which is the cheap half and the half that catches a wrong config.

**10. A `max_concurrency` of N aliases is NOT additive against one bucket, and
the per-minute limit binds first.** `_build_limits` sizes each account's
semaphore from the **smallest** declared `max_concurrency` in the group and its
pacer from the **smallest** declared `max_requests_per_minute`. In queue mode
every capacity is overridden by the queue's own `concurrency`, and the
engine's own docstring records the reason the rate has to be apportioned
across queue **children**: they are separate processes and cannot share an
in-process bucket (`CouncilWorker.__init__`, the `requests_per_minute`
parameter). That apportionment is static and therefore drifts when a child
idles — a known and accepted limitation of the existing pacer, inherited here
rather than introduced.

**11. `VERIFIED_OUTPUT_CAPS` is empty, so every seat runs as singles — and an
`anthropic` seat is the first that could legitimately get an entry.** The table
is keyed `(endpoint_kind, model)` and gates batching; a seat with no entry is
forced to singles (`_effective_output_cap`, `_batch_n`). `max_tokens` on
`/v1/messages` is a required, hard, documented ceiling — a real verifiable
mechanism, unlike either CLI. Deliberately **not** added in v1 (Q9): batching
is a separate capability and adding an unmeasured cap entry to make an option
"look enabled" is exactly what that table's own comment forbids.

**12. `resolve_credentials` would report an `anthropic` endpoint as needing
nothing.** `domains/genome/ontology_mapping/council_run.py::resolve_credentials`
branches on `argo`, then `openai`, then falls through to
`"requires": "interactive_subscription_login" if kind in ("claude_cli", "codex_cli") else "none"`.
An `anthropic` endpoint lands on `"none"` — reporting that a seat which cannot
work without an API key needs no credential. The `argo` branch is the model to
copy: it raises `RunRefused` with an actionable message rather than letting the
seats report unavailable and the calls park.

**13. An `anthropic` endpoint would be refused at config load for a reason
nobody would guess.** `BASE_URL_KINDS = ("argo", "openai")`
(`council/config.py:119`). `EndpointConfig._check` sends any other non-CLI kind
down the branch that demands `reuse_namespace`, because
`default_reuse_namespace` would otherwise return the same string for every such
endpoint and two unrelated deployments would share one answer cache. Filling a
default `base_url` of `https://api.anthropic.com` resolves it properly — the
namespace then derives from a real identity — rather than pushing the burden
onto every operator config.

**14. THE SDK RETRIES A 429 WITHOUT LOOKING AT THE SPEND CODE, which breaks the
wrapper's one-attempt invariant and triples the spend-cap spiral.**
From the external scan: `BaseClient._should_retry`
(`anthropic-sdk-python`, `src/anthropic/_base_client.py:906`) checks
`x-should-retry`, then the HTTP status — including 429 — **without examining
`error.details.error_code`**. The SDK's default is 2 retries. So a spend-cap
429 is retried twice *inside the SDK*, before this repository's backend ever
sees it.
That is a direct violation of a stated invariant: `llm/__init__.py`'s module
docstring promises "**Exactly one attempt per call.** Retries, backoff,
budgets, halts, concurrency limits and single-flight collapsing belong to the
caller … because only the caller knows what a second attempt would cost."
The error mapping in finding 7 is therefore **necessary but not sufficient** —
the client must also be constructed with retries **disabled**. The scan's own
phrasing is the right one: give one layer ownership of retries, or you get
SDK × wrapper × task-loop amplification.

**15. The SDK requires Python ≥ 3.10 and this repository's CI tests 3.9.**
`requires-python = ">=3.9"` in `pyproject.toml`, and `.github/workflows/ci.yml`
runs a matrix of 3.9, 3.10 and 3.11. The SDK's own manifest requires
`>=3.10` (`anthropic-sdk-python`, `pyproject.toml:11`, verified by the scan).
CI installs the `all` extra, so an unmarked entry there would **break the 3.9
job**. The repository already has the precedent:
`mcp = ["mcp >=1.27,<2 ; python_version >= '3.10'"]`. The same environment
marker is required, and the lazy-import failure message must name the Python
requirement as well as the extra — otherwise a 3.9 user is told to install an
extra that resolves to nothing.

**16. The usage-name trap is confirmed empirically in other projects, and two
libraries get it wrong today.**
The external scan verified the normalisation point in six libraries. LiteLLM
(`AnthropicConfig.calculate_usage`), LangChain
(`_create_usage_metadata`) and the Vercel AI SDK (`convertAnthropicUsage`) all
**sum the three input categories**, which is the decision taken in Q2.
aisuite (`AnthropicMessageConverter._get_completion_usage`) and LlamaIndex
(`get_tokens_from_response`) do **not** — and the scan extracted and *executed*
both functions against input=18, cache-creation=2,284, cache-read=0: each
returned `prompt_tokens = 18` where the true input total is 2,302.
Two real closed defects of exactly this class: **Vercel AI SDK #9921** (closed
via #10975) reported 18 input tokens instead of 2,302 for 2,284 cache-write
tokens, and **LiteLLM #28354** read `usage.cache_read_input_tokens` off an
object whose field was `input_tokens_details.cached_tokens`, with the fallback
returning zero. This is no longer a hypothesis about what could go wrong; it is
a bug that has shipped in two serious projects.

**17. Double normalisation zeroes everything, so the normalised dict must never
be re-read as native usage.** The scan cites **Hermes #123157**: adapters
produced `prompt_tokens`/`completion_tokens`, then a downstream pricing
function selected a *native* Anthropic usage parser by provider identity, which
looked for `input_tokens`/`output_tokens` and found neither — all usage and all
cost became zero, with a deterministic reproduction. The lesson transfers
directly: once `AnthropicBackend` has normalised, nothing downstream may
re-interpret that dict by provider. In this repository nothing does today, which
is why this appears as a gotcha rather than a change.

**18. Anthropic documents its own OpenAI-compatibility surface as
evaluation-only.** It records that prompt caching is unsupported, tool `strict`
is ignored, `response_format` is ignored, thinking output is unavailable, and
system/developer messages are hoisted into one top-level system field — and
that unsupported inputs may be **silently ignored**
(`platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk`).
That is first-party confirmation of the brief's instruction not to use an
OpenAI-compatible shim, and it is a stronger argument than the shape mismatch
alone: a shim's failures are silent.

**19. Nobody has solved the per-bucket pacing problem automatically, so a
config-driven bucket table is the state of the art rather than a shortcut.**
The scan found cross-process coordination prior art (LiteLLM's Router
recommends Redis-backed per-deployment/model/minute counters;
`lowest_tpm_rpm_v2.py`) and header-driven adaptive *concurrency* (Promptfoo
reduces concurrency below 10% remaining, AIMD-style), but **no confirmed client
that discovers Anthropic sibling-model bucket membership from response
headers**. It also makes a refinement worth taking: scope the bucket to the
credential's **organisation/workspace**, not to the API key, because several
keys can share one quota. And a sharp caveat against the tempting
half-measure: lowering *concurrency* does not ensure that several large prompts
fit within ITPM.

**20. This is a new PRD, not an amendment to `kbu-ai-council-v1`.**
`kbu-ai-council-v1` is at status `ready` in the registry, not `done`: amending
a `ready` PRD edits a document an interactive conductor may claim, and would
reopen a bundle whose work has already landed. `kbu-ai-council-v1` established
the backend-agnostic provider layer; this PRD *uses* that layer exactly as
designed — through the entry-point/registry door and the `ApiType` protocol —
which is evidence the layer worked, not a reason to reopen it. The only places
this PRD must touch pre-existing code are the ones that layer did **not** make
open: a closed `Literal`, a two-element `BASE_URL_KINDS`, a gateway-keyed rate
lookup, and three kind-branching `if` chains.

### Links to full research output

- [External prior-art scan](research/external-scan.md) — how six multi-provider
  abstraction layers (LiteLLM, LangChain, LlamaIndex, aisuite, OpenRouter,
  Vercel AI SDK) normalise Anthropic usage field names and cache-aware token
  accounting, with the normalisation function and line number for each; four
  real shipped defects of the exact class this PRD guards against; the official
  SDK's retry behaviour; cross-process rate-limiting prior art and the pitfalls
  of header-driven tuning. Dispatched to a cross-family model (codex) on h100
  as `task-a7ad9d14`; findings 14–19 above come from it.
  **It returned and it changed the design**: findings 14 (SDK retries a
  spend-cap 429) and 15 (Python ≥ 3.10 versus a 3.9 CI job) were not known
  before it ran, and both are build-affecting.
- [Confront round 1 stall report](research/confront-round-1-stall-report.md) —
  a cross-family (codex) adversary instructed to execute this PRD in its head,
  phase by phase, and report every point where it would be forced to stop or
  guess. It returned **seven binding stalls** and verified several of them by
  *executing the real functions* rather than reading them. Six were accepted and
  changed the design; one was accepted in substance and resolved differently.
  The two that matter most: the draft's temperature rule was **unimplementable**
  because `_seat` pops temperature out of model params (Q4), and the draft's own
  pricing fix would have published a subtotal as a complete cost because
  `Quote.to_dict` selects on truthiness (Q11). Adjudication is recorded inline
  in each Open Question, including what was **not** adopted and why.

### Quality report

**What was searched, and how well.**

The local corpus was read directly on `main` at `cb6cf1b` and is the strongest
part of this research. Read in full: `llm/__init__.py`, `llm/protocol.py`,
`llm/types.py`, `llm/apis/http_common.py`; read in the regions that matter:
`llm/apis/http.py` (the ARGO parameter tables and both `_bearer`
implementations), `council/types.py` (`SeatSpec`, `Roster._check_independence`,
`default_reuse_namespace`, `ENDPOINT_KINDS`), `council/config.py`
(`DEFAULT_CANONICAL`, `DEFAULT_CANONICAL_BY_KIND`, `BASE_URL_KINDS`,
`DEFAULT_MAX_CONCURRENCY`, `EndpointConfig` and `_check`, `RateConfig`,
`_require_registered_kind`, the credential-channel and static-header
validators), `council/quote.py` (`_gateway_of`, `_price_gateways`, `QuoteLine`,
`_balance_delta_block`), `council/engine/worker.py` (`_RateBucket`,
`_AccountLimit`, `_build_limits`, `_enforce_budget`, `_token_total`,
`VERIFIED_OUTPUT_CAPS`, `_effective_output_cap`), `council/store/schema.py`,
`council/default_config.yaml`, and
`domains/genome/ontology_mapping/council_run.py::resolve_credentials`.
Every claim above carrying a file and line was read, not recalled.

The Anthropic API surface was taken from the bundled `claude-api` skill
(authoritative, and explicitly warns that a training prior on several of these
shapes is wrong) plus two live documentation pages fetched on 2026-10-29:
the rate-limits page and the pricing page. The per-model-bucket rate-limit
structure, the three money-signal shapes, the response-header names, the
cache-aware ITPM rule and the per-model prices are all from those two pages.

Dependency state was measured, not assumed: `anthropic` is **not** installed in
the kbdl venv (`ModuleNotFoundError`) and is **not** in `pyproject.toml`. The
venv's KBUtilLib install tree was resolved by asking the interpreter for
`kbutillib.__file__` and then comparing inodes — it reports
`/scratch/fliu/hub_home/chenry/H100Projects/KBUtilLib`, which `stat` shows is
the same inode as `~/projects/KBUtilLib` (device 64517, inode 5167606134), a
path alias and not a third clone. That tree is on `main` at `b1cbc40`, one
commit behind `cb6cf1b`.

**What was NOT searched, and why.**

- **No live Anthropic API call was made.** No `ANTHROPIC_API_KEY` is present in
  this session's environment, and a design session does not spend money. Every
  claim about response *shape* therefore rests on documentation rather than on
  an observed response. This is precisely why the usage assertion is an
  acceptance criterion executed against a real call (AC1–AC4) and not a test in
  the plan: a stub built from the same documentation would confirm the
  documentation, not the API.
- **The `anthropic` SDK's own method signatures were not verified against an
  installed package**, because it is not installed. The plan's phase 1 is a
  real-dependency smoke task whose entire job is to close that gap before
  anything builds on it.
- **No 5.5-generation model ID strings.** The live pricing and rate-limit pages
  list `Claude Opus 5.5`, `Claude Sonnet 5.5` and `Claude Haiku 5.5`; the
  bundled skill's model table (cached 2026-06-24) does not, so it supplies no
  ID strings for them. See the terminal entry in Open Questions — this is the
  one fact the design assumed around rather than resolved.
- **`Claude Opus 4.5`'s price was confirmed** ($5/$25) but the older
  `Opus 4.1`/`Opus 4` rows ($15/$75) were not considered, as nothing would seat
  a retired model.
- **Batch API and prompt caching were researched only far enough to scope them
  out**, and the caching finding that survived (ITPM excludes cache reads, so
  caching is a *throughput* lever and not only a cost lever) is recorded in
  Out of Scope rather than acted on.

**The external scan ran, returned, and is the strongest part of this research
other than the local code read.** It verified six libraries' normalisation
functions by reading them at pinned commits, and went further than asked: it
extracted aisuite's and LlamaIndex's usage functions and **executed them** on a
cache-heavy input, measuring the undercount (18 against a true 2,302) rather
than asserting it. It also stated its own limits clearly, and those limits are
real: no live-provider call, no third-party test suite installed, the
OpenRouter server-side adapter not reachable, and the out-of-order/stale-header
failure mode an engineering inference rather than a measured incident. Its own
self-assessment — high confidence on the cited normalisation paths and
documented mismatches, medium overall — is the right one and is adopted here.

**One claim in it was checked rather than taken.** It reported the SDK's
manifest requiring Python ≥ 3.10; that only matters if this repository tests
below that, so `pyproject.toml`'s `requires-python` and
`.github/workflows/ci.yml`'s matrix were read directly — `>=3.9` and a 3.9 job
respectively. Finding 15 rests on that local check, not on the scan alone.

**Confidence the design should carry.** High on the local code facts — they
were read at a named commit and each is cited to a line. High on the Anthropic
limit/price/error structure, which came from live first-party documentation
today. **Medium on the exact SDK call shape**, which is documentation-derived
and is the explicit job of phase 1 to pin down. **Medium-low on anything
concerning the 5.5 generation**, which the design deliberately handles by
making model addition a configuration act rather than a code act.

## Revision Log

**Round 0 — 2026-10-29 — initial single-pass draft.**
Written against `main` at `cb6cf1b`. Three findings moved the design away from
the brief it was commissioned under: Anthropic's rate limits are per
model-class bucket rather than account-wide (which dissolves the predicted
pricing-versus-pacing conflict), sampling parameters return HTTP 400 on the
current Claude models (a guaranteed failure the brief did not name), and the
operator-set spend limit arrives as an HTTP 400 that currently neither halts
nor retries (the wallet-exhausted path).

The cross-family external scan (`task-a7ad9d14`) then added two
build-affecting facts of its own, both folded in before the first commit: the
official SDK retries a 429 **without** inspecting the spend code and defaults
to 2 retries, which breaks this repository's stated one-attempt-per-call
invariant and triples the spend-cap spiral (new Q2b); and the SDK requires
Python ≥ 3.10 while CI runs a 3.9 job against the `all` extra, so the
dependency needs an environment marker or the 3.9 build breaks (finding 15).
It also turned the central usage-mapping trap from a predicted hazard into a
documented one, citing two shipped defects and measuring the undercount in two
libraries that still have it.

**Confront round 1 (`task-4deb5df1`) then returned seven binding stalls, six of
which changed the design before the first commit.** The two largest were
defects in the draft itself rather than gaps: the temperature rule could not be
implemented as written, because `CouncilConfig._seat` pops `temperature` out of
a model's params onto the seat (Q4, re-decided); and the draft's own
`priced: "partial"` fix would have published a partial subtotal as a **complete**
dollar figure, because `Quote.to_dict` and the quote CLI both select priced
gateways by truthiness (Q11, representation changed to a strict boolean plus a
flag so unaware readers understate). Also folded: a hole in the `account`
validation (Q3), an unsatisfiable namespace criterion (AC7), a three-way
contradiction about size-tiered pricing (new Q11b), balance-delta rows
triple-attributing one wallet (new Q11c), an impossible live criterion
(new Q5b, AC4 reclassified), and a framing error about which rate limit binds
(Q8). One recommendation was accepted in substance but resolved differently —
a required `quota_scope` field, replaced by requiring `account` to *be* the
bucket name plus an explicit non-goal.

**A sibling session's measured correction landed in the same window and changed
an Out of Scope entry from a throwaway into a load-bearing dependency.** The
planned corpus is 29,179 items, not the 21,333 this PRD was briefed with, which
makes the Anthropic leg $313 against $200 of credit and the Batch API the only
thing that closes the gap. Batch stays out of scope, but as a **separate PRD
that this one is a prerequisite for**, and the claim that this PRD alone makes
the planned run affordable has been withdrawn.

## Problem Statement

An operator has $200 of Anthropic API credit and wants to seat two or three
Claude models as council chairs. Today they cannot: `kind: anthropic` is
refused at config load, and no code path in KBUtilLib reaches
`POST /v1/messages`.

The four api types that do exist are an OpenAI-compatible HTTP backend, ANL's
ARGO gateway, and two headless CLIs that bill against interactive
subscriptions. The two HTTP ones share one request path hardcoded to
`{base_url}/chat/completions` and one response parse hardcoded to
`choices[0].message.content` with `usage.prompt_tokens` /
`usage.completion_tokens`. `/v1/messages` matches none of that.

Even once the route exists, the operator still cannot answer the question that
actually gates the run: **what will it cost?** A fixed $200 wallet with no
second one means a run has to be priced before it starts and halted
automatically when the money is gone. Three things stand in the way:

1. The engine's spend accounting reads token-count field names that Anthropic
   does not use, so a naive backend reports **zero** spend — the budget
   ceiling never trips and the quote reconciles against nothing.
2. The quote prices one dollar rate per gateway, so Haiku 4.5, Sonnet 5 and
   Opus 5 on one endpoint are all priced at one rate, wrong by 5x across the
   input span.
3. The signal that the wallet is empty arrives in a shape the current error
   mapping reads as a per-item model fault, so the run does not stop — it
   fails every remaining item individually with a misleading reason.

## Solution

Add a fifth LLM api type, `anthropic`, which speaks `/v1/messages` through the
official `anthropic` Python SDK, and make the council's existing pricing and
pacing machinery exact for it by configuration rather than by new mechanism.

**The route.** A new `AnthropicBackend` in the `llm.apis` package, registered
as the built-in `anthropic`, implementing the `ApiType` protocol directly. It
does **not** subclass `HttpBackend` and does **not** go through an
OpenAI-compatible shim: the path, the body and the response parse are all
different, and a shim would make the three traps above invisible rather than
absent. It reuses the shared, provider-neutral helpers that are genuinely
shared (`redact`, `extract_json_object`, `strip_fences`, the error taxonomy)
and nothing else.

**The accounting.** The backend normalises Anthropic's usage object into the
engine's vocabulary at the one point the engine reads:
`prompt_tokens = input_tokens + cache_creation_input_tokens + cache_read_input_tokens`
and `completion_tokens = output_tokens`. Written as the sum, not the bare
field, so the identity survives anyone later enabling caching. A request
carrying `cache_control` is refused in v1 rather than mis-billed.

**The pricing.** One endpoint alias per Anthropic **rate-limit bucket**, each
declaring `account: <bucket>`. That single key does both jobs correctly
because Anthropic enforces its limits per model-class bucket: the aliases are
genuinely independent pools at the provider, so they are genuinely independent
semaphores and pacers here, and they bucket the quote at the granularity
pricing needs. On top of that, `_price_gateways` gains per-canonical-model rate
resolution inside each bucket — `rates["<gateway>/<canonical>"]` falling back
to `rates["<gateway>"]` — so the quote is exact by construction rather than
exact by today's coincidence that each bucket happens to hold one price. A
gateway whose lines span models where only some have a rate is reported
**partially priced with the unpriced models named**, never as a total.

**The stopping.** Three money/limit responses are mapped to three different
outcomes: a per-minute 429 stays a retryable flagged transport failure (the
existing pacer and sustained-halt logic are correct and are reused), while both
spend-limit shapes — the 429 carrying `enforced_spend_limit_reached` and the
HTTP 400 beginning `You have reached your specified API usage limits` — become
`QuotaExhausted`, which halts the endpoint and is not retried.

**The parameters.** Sampling parameters are omitted unless a seat sets one
explicitly, because the current Claude models reject them with HTTP 400 and
`SeatSpec.temperature`'s default of `0` was never a decision anyone made about
this provider. `stop_reason: "max_tokens"` maps onto the existing truncation
failure. Both are small, and both are the difference between a working seat and
one that fails every call.

**The visibility.** Every response's `anthropic-ratelimit-*` headers are
recorded, so the configured `max_requests_per_minute` can be checked against
the provider's own answer. This is the first gateway in this project whose
limits are machine-readable at all; the design uses that to make a wrong config
*detectable*, and stops short of a self-tuning pacer.

## User Stories

1. As an operator, I want to declare `kind: anthropic` in my council config and
   have it accepted at load, so that I can reach the Anthropic API at all.
2. As an operator, I want to declare an `anthropic` endpoint without writing a
   `base_url`, so that the common case needs no URL I would only copy from
   documentation.
3. As an operator, I want my Anthropic API key to travel through `api_key_env`
   like every other credential in this system, so that no key is ever written
   into a config file, a route fingerprint or a store snapshot.
4. As an operator, I want a council run with an `anthropic` seat to be refused
   at submit time with an actionable message when the key is absent, so that I
   do not discover it as seats reporting unavailable and calls parking.
5. As an operator, I want to seat Haiku 4.5, Sonnet 5 and Opus 5 at the same
   time and see three separate dollar figures, so that the quote is exact
   rather than wrong by 5x.
6. As an operator, I want each seated Claude model paced against its own rate
   limit, so that seating three models does not make three of them collide the
   way the first MAG pilot did.
7. As an operator, I want two models that share one Anthropic rate-limit bucket
   (Opus 4.8 and Opus 4.7) to share one semaphore and one pacer, so that I do
   not configure two independent pools against one provider allowance.
8. As an operator, I want a config that puts two differently-limited models on
   one `account` to be refused at load, so that a quiet mis-grouping cannot
   silently halve my throughput or double my rate.
9. As an operator, I want the run to stop by itself when the $200 is spent, so
   that I am not relying on noticing.
10. As an operator, I want "the wallet is empty" to read differently from "slow
    down", so that I do not spend an afternoon tuning a rate limit that was
    never the problem.
11. As an operator, I want a per-minute 429 retried and a sustained run of them
    to halt the endpoint, exactly as it already does for MAG, so that this
    provider needs no new pacing concepts.
12. As the budget enforcer, I want a completed Anthropic call to record a
    non-zero `prompt_tokens`, so that `max_total_tokens` is enforceable rather
    than silently unenforced.
13. As an operator, I want an endpoint that reports no usage at all to halt with
    `budget_usage_unknown` rather than run free, so that an unmeasurable budget
    stops the run instead of being ignored.
14. As a quote reader, I want a gateway with no configured rate to be reported
    `unpriced` with its token total, so that I know what is missing rather than
    being handed a confident wrong number.
15. As a quote reader, I want a gateway where only some of its models have
    rates to be reported as partially priced, naming the unpriced models, so
    that a partial sum is never presented as a total.
16. As an operator, I want the balance-delta calibration block to carry one row
    per wallet rather than one per model, so that it asks me for a balance I can
    actually read off a console.
17. As a council designer, I want ARGO's `claudesonnet5` and the Anthropic
    API's Sonnet 5 to be refused as two independent proposers, so that one
    model cannot be counted as two votes through two wallets.
18. As a council designer, I want to know which Claude models ARGO does **not**
    serve, so that I can tell which seats the new wallet actually adds rather
    than duplicates.
19. As an operator, I want a seat left at the default 4000 output tokens that
    gets truncated to be reported as truncated, so that a cut-off answer is not
    diagnosed as a model that cannot produce JSON.
20. As an operator, I want adaptive thinking's token cost named in the
    documentation, so that I size `max_output_tokens` deliberately instead of
    discovering the default is too small for this provider.
21. As an operator, I want a seat that sets `temperature` on a model which
    rejects it to fail with the provider's own explanation, so that the cause is
    readable.
22. As an operator, I want every seat NOT to send a temperature by default, so
    that the current Claude models work without my having to know which ones
    reject sampling parameters.
23. As an operator, I want `kbu council doctor` / preflight to tell me an
    `anthropic` endpoint is ready without spending anything, so that I can
    verify configuration before committing $200 to a run.
24. As an operator, I want the live rate limits the provider reports in its
    response headers recorded, so that I can check whether my configured
    `max_requests_per_minute` matches reality.
25. As a developer, I want `anthropic` to be an optional install extra that
    fails with a message naming the extra, so that `import kbutillib` and a
    core install never require it.
26. As a maintainer, I want adding a newly released Claude model to need a
    config change and not a code change, so that the shipped code does not rot
    every time Anthropic ships a model.
27. As a maintainer, I want no store migration, so that an existing council
    database keeps working untouched.
28. As a reviewer of a past run, I want `endpoint_kind: anthropic` persisted on
    every call row, so that a verdict's route is identifiable later.
29. As an operator, I want a request carrying `cache_control` refused in v1
    rather than silently billed wrong, so that the dollar figure never
    disagrees with the token figure.
30. As an operator, I want to be told that this credit is a second wallet at
    the same price as ARGO rather than a discount, so that I do not plan a run
    around a saving that does not exist.

## Implementation Decisions

### Modules to add

**`src/kbutillib/domains/ai/llm/apis/anthropic.py`** — new. One class,
`AnthropicBackend`, implementing `ApiType` directly.

- `kind = "anthropic"`; `supports_web_search = False` (a server-side web search
  tool exists on this API but is not wired here — see Out of Scope).
- `DEFAULT_BASE_URL = "https://api.anthropic.com"`.
- Constructed from one endpoint-config mapping, exactly like `HttpBackend`:
  `{}` is valid and falls back to defaults. Accepts a test-seat injection point
  for the transport so the whole request path is exercisable without a socket.
- **The SDK client is constructed with retries DISABLED** (`max_retries=0`),
  and that applies to the client used by `models()` as well as the one used by
  `complete()` — if the one-attempt policy is the house rule, a diagnostic
  enumeration does not get an exemption. This is not a tuning preference. The
  SDK's `_should_retry` retries on HTTP status including 429, **without**
  examining `error.details.error_code`, and defaults to 2 retries — so a
  spend-cap 429 would be retried twice inside the SDK before this backend saw
  it, and `llm/__init__.py` promises the opposite: "Exactly one attempt per call
  … because only the caller knows what a second attempt would cost." One layer
  owns retries, and that layer is `CouncilWorker`.
- **The major version is pinned, but WHICH major is phase 1's to determine.**
  The draft specified `>=1,<2`. The external scan then reported the SDK's
  current main at **2.3.0**, which makes a 1.x pin a deliberate choice to ship
  on a superseded major — defensible only with a stated reason, and the draft's
  reason (1.x moved to `httpx2`) is a fact about 1.x, not an argument against
  2.x. So phase 1 inspects what is currently resolvable, pins to a **single**
  major, and **records why** in its work record. What is not negotiable is that
  it is pinned to one major and that phase 1 inspects the **installed artifact**
  rather than today's main branch — the scan's own caution, and correct.
- **The SDK is imported lazily, inside the factory**, never at module scope.
  A missing package raises an error naming the extra
  (`pip install 'kbutillib[anthropic]'`) **and the Python requirement**,
  following the `pymongo`/`backfill` convention already documented in
  `pyproject.toml`. The Python requirement belongs in the message because the
  extra carries a `python_version >= '3.10'` marker (below) and on 3.9 the
  install silently resolves to nothing — "install the extra" would otherwise be
  advice that cannot work.
- **`complete()` never raises for a call failure** — the protocol's one hard
  rule. The outer guard converts any unforeseen exception into
  `LLMResult(ok=False, error=...)` with the message passed through `redact`.
- **`effective_params()`** reports exactly what is sent and nothing about
  routing, so the route fingerprint is honest about the omitted temperature.

Request construction:

- `model` ← the seat's served model name, verbatim.
- `max_tokens` ← `req.max_output_tokens`. **Required** by this API; always
  present.
- `system` ← `req.system` as the top-level `system` parameter (not a message),
  omitted when empty.
- `messages` ← a single `{"role": "user", "content": req.user}`.
- **Sampling parameters are omitted** unless the seat's `params` explicitly
  carries one. See Q4.
- `thinking` is **not** sent, leaving the model's default. See Q5.
- `output_config.format` (structured outputs) is **not** requested. See Q6.
- The endpoint's `extra_body` and the request's own `params` merge into the
  call as provider extras, minus the keys that are not generation parameters —
  the same `("extra_body", "web_search")` exclusion `HttpBackend._model_params`
  applies, for the same reason.

Response parse:

- Content is a **list of blocks**. Concatenate the `text` of every block whose
  `type == "text"`, in order. Do not index `content[0]`: with thinking on, a
  non-text block legitimately comes first, and `thinking.display` defaults to
  omitted so its text is empty.
- `resolved_model` ← the response's `model` field.
- **Usage mapping, the one the budget reads:**
  `prompt_tokens = input_tokens + cache_creation_input_tokens + cache_read_input_tokens`
  (each absent field read as 0), `completion_tokens = output_tokens`.
  `usage` stays `None` only when the provider reported no usage object at all,
  preserving the engine's distinction between "free" and "unmeasurable".
- `stop_reason == "max_tokens"` → the truncation failure, with the same
  "a cut-off answer is not a vote" semantics `HttpBackend` applies to
  `finish_reason == "length"`.
- `stop_reason == "refusal"` → `InvalidOutput`, not retryable, with
  `stop_details.category` in the message.
- Answer extraction reuses `extract_json_object` over `strip_fences`, so a
  model that fenced its JSON is not failed for the decoration. **Also strip
  `<thinking>…</thinking>`**, not only `<think>…</think>`: `strip_think`
  matches the latter, and Anthropic's documented tag-leakage failure mode uses
  the former. This is an **Anthropic-LOCAL cleaner**; the shared `strip_think`
  helper is **not** modified. Widening a helper that ARGO and every
  OpenAI-compatible endpoint also call, to fix a provider none of them use, is
  how an unrelated route changes behaviour in a PRD that never mentions it.
- **Reserved and forbidden request keys.** Provider extras are a passthrough,
  which means they can defeat the invariants stated above. So: `model`,
  `messages` and `max_tokens` are **reserved** — an extra that would overwrite
  one is refused, not silently merged, because a caller who overwrites `model`
  has changed which model the route fingerprint claims. `stream` and `tools`
  are **forbidden** in this lane: both change the response shape this parse
  does not handle, and `schema_enforced=False` stops being accurate if a caller
  smuggles in `output_config.format`. The `cache_control` refusal must be a
  **recursive** check over the MERGED request `params` and endpoint
  `extra_body`, not a top-level key test — a nested `cache_control` is the one
  that would get through.
  Stated as a distinction rather than a ban: thinking, structured outputs and
  caching are **not requested by default** (Q5, Q6, Q2); of those, only
  `cache_control` is actually **refused**, because only it makes the dollar
  figure disagree with the token figure. The others an operator may set
  knowingly, and if they set `output_config.format` then `schema_enforced`
  becomes inaccurate — which is a reason to document the distinction, not to
  pretend extras do not exist.
- `schema_enforced = False`, consistent with every other api type here, because
  no schema was requested of the provider and the engine validates the answer
  itself.

Error taxonomy mapping (all via the typed failures already in `llm.types`):

| Signal | Mapping |
|---|---|
| 401 / 403 | `AuthError` (halts endpoint) |
| 400 whose message begins `You have reached your specified API usage limits` | `QuotaExhausted` (halts endpoint) |
| 429 with `error.details.error_code == "enforced_spend_limit_reached"` | `QuotaExhausted` (halts endpoint) |
| 429 otherwise | `TransportError` with `rate_limited=True` (retryable) |
| ≥ 500 | `TransportError` (retryable) |
| other 4xx | `InvalidOutput` (the request was wrong, not the credential) |
| connect / timeout | `TransportError` / `CallTimeout` |
| request carrying `cache_control` | `InvalidOutput`, refused before anything is sent |

The two spend-limit matches are ordered **before** the generic status rules, and
the 400 match is on the documented message prefix because that response carries
no machine-readable code. Match on the prefix, case-insensitively, and record
the matched text in the halt reason so a future change in wording is visible as
a halt that stopped firing rather than as a silent regression.

- `preflight()` — free, spends nothing: reports `ready` when the credential
  named by `api_key_env` resolves and `refused` with a reason when it does not.
  Never returns the credential's value, only the name of what was resolved.
- `models()` — `GET /v1/models` through the SDK; `[]` on any failure, because
  enumeration is a diagnostic and never a precondition for a call.
- **Rate-limit headers:** read `anthropic-ratelimit-requests-limit`,
  `-requests-remaining`, `-requests-reset` and the input/output token families
  off each response and expose them on the backend as last-observed values,
  logged at debug. This is a *record*, not a control input (Q7).

### Modules to modify

**`council/types.py`** — add `"anthropic"` to `SeatSpec.endpoint_kind`'s
`Literal` and to `ENDPOINT_KINDS`. Add an `HTTP_KINDS`-adjacent note that
`anthropic` is URL-reached but not OpenAI-shaped, so nothing infers the request
shape from the kind being HTTP. Do **not** add it to `HTTP_KINDS` without
checking every reader of that tuple — `HTTP_KINDS` currently means "argo or
openai", i.e. OpenAI-shaped, and widening it silently changes whatever asks.

**`council/config.py`** —
- `DEFAULT_MAX_CONCURRENCY["anthropic"]` = 4. Deliberately conservative: the
  binding constraint is the per-minute rate, not the parallel cap (Q8), and a
  low concurrency with a correct pacer is safe while the reverse is not.
- `EndpointConfig._check` gains an `anthropic` branch, placed **before** the
  generic plug-in branch, which defaults `base_url` to
  `https://api.anthropic.com` when absent. This both removes the
  `reuse_namespace` burden and gives `default_reuse_namespace` a real identity
  to derive from.
- `billing` must be `api` for an `anthropic` endpoint — refuse `subscription`
  with a message saying why (this is an API-billed wallet; the CLI kinds are the
  subscription path).
- **`account` is required** on an `anthropic` endpoint, and config load refuses
  two `anthropic` endpoints that share an `account` while naming models in
  different rate-limit buckets, and refuses one `anthropic` endpoint whose
  models span two buckets. This is the validation that makes story 8 real; see
  Q3 for why it is a refusal rather than a convention.
- `DEFAULT_CANONICAL_BY_KIND["anthropic"]` maps the served model ids to
  canonical identities that **collide with ARGO on purpose**:
  `claude-sonnet-5 → anthropic/claude-sonnet-5`,
  `claude-opus-5 → anthropic/claude-opus-5`,
  `claude-haiku-4-5 → anthropic/claude-haiku-4.5`,
  `claude-sonnet-4-6 → anthropic/claude-sonnet-4.6`,
  `claude-opus-4-8 → anthropic/claude-opus-4.8`.
  The first two are byte-identical to `DEFAULT_CANONICAL`'s entries for ARGO's
  `claudesonnet5` and `claudeopus5`, so `Roster._check_independence` refuses
  seating both — which is correct and must not be loosened (Q1).
- The **rate-limit bucket table**: a module-level mapping from canonical model
  to bucket name, which is what the `account` validation checks against. It
  must be overridable from configuration so a new model needs no code change
  (Q10).

**`council/quote.py`** — three functions, not one (see Q11; the draft's
"confined to one function" claim was wrong).

`_price_gateways` sub-buckets each gateway's lines by `line.canonical_model`
(already a field on `QuoteLine`; no new field needed) and resolves each
sub-bucket's rate as `rates["<gateway>/<canonical>"]`, falling back to
`rates["<gateway>"]`. The gateway entry keeps every field it publishes today and
gains a `by_model` breakdown. Pricing honesty rules, which are the point:

- Every sub-bucket priced → `priced: true`, as today, with `dollars_point` and
  `dollars_upper` the sum over sub-buckets.
- No sub-bucket priced → `priced: false` with the existing `unpriced_reason`.
- **Some priced** → `priced: **false**` plus `partially_priced: true`,
  `unpriced_models` naming the models with no rate, a
  `dollars_known_subtotal`, and an `unpriced_reason` saying the subtotal covers
  only part of the tokens. **`priced` stays a strict boolean.** It must not
  become a tri-state: `Quote.to_dict` and the CLI both select on truthiness, so
  a `"partial"` string would be counted as fully priced and
  `dollars_complete` would read true over a subtotal. Keeping `priced` false
  makes every existing reader — including any not found here — treat a partial
  gateway as unpriced, which understates rather than overstates.
- A multi-rate gateway publishes `rate: null` and `rate_provenance: null`; the
  contributing rates and their provenances live under `by_model`. One legacy
  rate pair cannot represent three rates, and `RateConfig` requires a
  provenance precisely so no figure is unlabelled.
- A quote whose contributing rates are **not all one currency** is refused, not
  summed. `RateConfig.currency` is a free string, so mixed denominations are
  otherwise added together silently.
- Per-model costs are summed **unrounded** and rounded once at the gateway, so
  the `by_model` figures and the gateway total cannot disagree by rounding.
- The `is_floor` warning fires if **any** contributing rate is marked a floor,
  naming which.

`Quote.to_dict` additionally surfaces the partial state: a partially-priced
gateway appears in `unpriced_gateways` (it already will, via the boolean) and
its known subtotal is published so an operator can see it. `dollars_complete`
needs no change — it is `not unpriced`, which goes false on its own.

The **quote CLI** (`interfaces/cli/council.py`) names the unpriced canonical
models for a partially-priced gateway, rather than printing it as simply
"unpriced", so the operator learns which rate is missing.

`_balance_delta_block` is **re-keyed on `billing_wallet`**, not on gateway (see
Q11c). Keyed on gateway it would emit one row per rate-limit bucket, i.e. three
rows asking for the same console balance, and copying one wallet delta into
each would attribute the bill three times. It aggregates member gateways' token
totals and names the member gateways in the row. It must **not** derive
per-model rates from a single wallet observation — one number cannot be
decomposed into three models' four rates.

**`council/config.py::EndpointConfig`** also gains an optional non-secret
`billing_wallet`, defaulting to `account` (so every existing kind is unchanged)
and **required** for an `anthropic` endpoint.

**`domains/genome/ontology_mapping/council_run.py::resolve_credentials`** — add
an `anthropic` branch modelled on the `argo` one: resolve the variable named by
`api_key_env` (defaulting to `ANTHROPIC_API_KEY`), and raise `RunRefused` with
an actionable message when it is unset or empty. Report
`"requires": "api_key"` with the variable **name** and `"source": "environment"`
— never the value.

**`council/config.py::_seat`** — for an `anthropic` endpoint only, a model whose
params omit `temperature` yields `SeatSpec.temperature = None` rather than `0`,
while an explicitly configured value (including an explicit `0`) is preserved.
Every other endpoint kind keeps today's behaviour byte for byte. This is what
makes Q4's rule implementable at all: `_seat` currently does
`temperature = params.pop("temperature", 0)`, which moves an explicit
temperature out of `params` and onto the seat, so no backend reading `params`
can tell an explicit value from the default. `LLMRequest.temperature`'s own
docstring already defines `None` as "do not send a temperature at all", so this
uses the existing contract rather than adding one.

**`pyproject.toml`** — a new optional extra, pinned to one major with the major
chosen by phase 1 (above), carrying an environment marker:
`anthropic = ["anthropic >=N,<N+1 ; python_version >= '3.10'"]`.

The **pin** is because the SDK has had a major version move `httpx`→`httpx2`
and change the async raw-response surface, so an unpinned floor would let two
installs behave differently with no signal. The **environment marker** is
because the SDK requires Python ≥ 3.10 while this repository declares
`requires-python = ">=3.9"` and CI runs a 3.9 job — and CI installs the `all`
extra, so an unmarked entry there **breaks the 3.9 build**. The precedent to
copy is already in the file: `mcp = ["mcp >=1.27,<2 ; python_version >= '3.10'"]`.
Add it to both `[project.optional-dependencies]` and the `all` extra, with the
marker in both places, and respect the in-file warning that the `dev` list is
duplicated in `[dependency-groups]` and both must be edited together. Note that the SDK brings `httpx2` alongside the
project's existing `httpx 0.28` — these are separate distributions and
coexist; it is recorded because two HTTP stacks in one process is a fact worth
knowing, not a conflict.

**`council/default_config.yaml`** — a commented example block, following the
`mag` precedent of showing the shape without shipping live model ids. It must
show one alias per rate-limit bucket, each with its own `account`,
`api_key_env: ANTHROPIC_API_KEY`, `billing: api`, and
`max_requests_per_minute`, and must say in prose that the `account` names the
**rate-limit bucket** rather than the wallet, and why.

### The deep module

`AnthropicBackend` is the deep module here, and that is the design's main
structural claim. Behind the two-method `ApiType` interface it holds: the
`/v1/messages` request shape, a multi-block response parse, the usage-name
normalisation the entire budget depends on, a seven-way error taxonomy
including two differently-shaped money signals, the sampling-parameter
omission rule, two truncation vocabularies and the rate-limit header read.
None of that leaks upward — the engine, the quote and the store all keep
speaking exactly the vocabulary they speak today. That is the test the design
should be held to: **if this PRD is built correctly, nothing outside the
backend learns that Anthropic names its token counts differently.**

The four edits outside the backend are not part of that module and are not
hidden by it; each is a place the existing design was closed where it should
have been open (a `Literal`, a two-element tuple, a single-rate lookup, a
kind-branching `if` chain). They are small on purpose and should stay small.

## Testing Decisions

**What a good test is here.** The behaviour that matters is external and
observable: given a response envelope, what `LLMResult` comes out; given an
error shape, which typed failure; given a quote input, which dollar figures and
which `priced` verdict. Tests assert on `LLMResult` and on the quote's
published dicts, never on the backend's internals.

**The trap this section exists to avoid.** Every test of a provider backend
replaces the provider. A suite built entirely on a hand-written envelope
fixture proves the parse matches *the fixture*, and if the fixture was written
from the same documentation as the code, the pair can be consistently wrong
together — which is exactly how a zero-usage mapping would survive a green
suite. The mitigation is structural and has two halves:

- **Phase 1 is a real-dependency smoke task.** It installs the actual
  `anthropic` SDK, imports it, constructs the real request object and asserts
  the shape of what the SDK itself accepts and returns. Every later phase
  depends on it. Fixtures are then *derived from observed output*, recorded in
  the phase-1 work record, rather than written from prose.
- **The usage assertion is an acceptance criterion, not a test.** AC1, AC2, AC3
  and AC6 are executed against a live call with a real key. No stub can satisfy
  them. This is the direct lesson of the "tests that validate their own stub"
  failure class: the cure is a criterion no fixture can meet.

**And the build cannot close them, which the draft got wrong (Q5b).** No
autonomous build has a credential, so implementation and offline verification
complete **without** one and the PRD's delivery state is
**live-acceptance-pending** until an operator supplies a key, the models to
call, an isolated council store and an explicit spending cap. A build **records**
the pending criteria and **does not claim acceptance** — a `done` PRD whose
acceptance step nobody ran is the measured failure this is guarding against.
**AC4 is excluded from the live set entirely**: it requires a response with no
usage object, and there is no supported way to make the real provider emit one.
It becomes an **engine integration test** with an injected response whose usage
is absent, asserting `_enforce_budget`'s `budget_usage_unknown` halt — a real
test of real engine behaviour, just not of the provider.

Note also what a no-socket SDK smoke does and does not prove. Exercising the
installed SDK through a mock transport proves **client compatibility** — real
serialisation, real response-model parsing, real raw-response header access. It
does **not** prove provider truth: the envelope is still a fixture. Phase 1's
work record must label that provenance, so a later reader does not mistake
"the SDK parsed it" for "the API sent it".

**Modules under test.**

- `AnthropicBackend` — the bulk. Table-driven over response envelopes:
  text-only; thinking-block-first then text; multi-text-block; fenced JSON;
  `<thinking>` leakage; `stop_reason: "max_tokens"`; `stop_reason: "refusal"`;
  usage present with all three input fields; usage with only `input_tokens`;
  usage absent entirely. Plus one case per error-taxonomy row, with the two
  spend-limit shapes asserted to produce `QuotaExhausted` with
  `halts_endpoint=True` — these two are the highest-value tests in the suite
  because they are the ones whose absence costs money.
- **A negative test that would have caught the original bug:** feed a response
  whose usage object carries *only* `input_tokens`/`output_tokens` and assert
  the resulting `usage` dict has non-zero `prompt_tokens` under the
  `prompt_tokens` key. A pass-through implementation fails it.
- **A property-style check that `_token_total` sees non-zero spend** for any
  envelope with non-zero Anthropic usage — asserting against the *engine's own*
  summing function rather than re-implementing it, so the test cannot drift from
  what the budget actually reads.
- `effective_params` — asserts temperature is **absent** by default and present
  only when explicitly set. Determinism: same request in, same dict out.
- `config` — `kind: anthropic` loads; `base_url` defaults; `billing:
  subscription` is refused; a missing `account` is refused; two aliases sharing
  an `account` across two rate-limit buckets are refused; two aliases sharing a
  bucket and an `account` are accepted.
- `_build_limits` — three `anthropic` aliases on three accounts produce three
  `_AccountLimit`s; two on one account share one, with capacity the minimum and
  pacer rate the minimum. This is an existing-behaviour assertion over new
  config, which is the right shape: it proves the reuse claim rather than
  trusting it.
- `Roster._check_independence` — an ARGO `claudesonnet5` proposer and an
  Anthropic `claude-sonnet-5` proposer raise `RosterError`. **This test asserts
  a refusal is preserved**, which is unusual and deliberate: the independence
  rule is what stops two wallets seating one model as two votes, and a future
  change that "fixes" the collision would otherwise pass silently.
- `_price_gateways` — one gateway with three canonical models and three rates
  prices each separately and sums; with one rate missing reports
  `priced: "partial"` naming the unpriced model; with none reports `unpriced`;
  the composite key is preferred over the gateway fallback; `is_floor`
  propagates from any contributor.
- `resolve_credentials` — an `anthropic` endpoint with the variable set reports
  `requires: api_key` naming the variable; unset raises `RunRefused`; the value
  never appears in the returned rows.

**Prior art to follow.** `read_chat_envelope`'s tolerant-parse convention (a
missing field becomes `None`, the caller decides what that means) and
`HttpBackend`'s `transport=` test seat, which lets the whole suite exercise the
real request path with no socket. Mirror both; do not invent a third
convention.

**Framing.** Every criterion is regression-framed. This repository carries
pre-existing test failures, so no criterion may require a green suite — the
bar is "the tests this task adds pass, and nothing that passed on the base
commit fails".

## Open Questions and Judgement Calls

**Every entry below is already decided and reflected in the PRD body and the
taskplan. The PRD is buildable as it stands.** The section is a manifest of
choices, not a queue of holes. To change a decision, edit its `DECIDED:` line —
and then follow it through the body and the plan, because each one is applied
in both. Ordered by blast radius descending.

### Q1. Should the canonical-identity collision with ARGO be avoided, or allowed to happen? -- DECIDED: allowed to happen, deliberately, and the refusal is pinned by a test.
**Blast radius:** IRREVERSIBLE
**Why:** ARGO's `claudesonnet5` already has canonical
`anthropic/claude-sonnet-5` and `claudeopus5` has `anthropic/claude-opus-5`
(`council/config.py`, `DEFAULT_CANONICAL`). Giving the Anthropic API's own
Sonnet 5 the identical canonical id makes `Roster._check_independence` refuse
seating both as proposers. That is the correct outcome and the whole purpose of
canonical identity: it is what stops two wallets presenting one model as two
independent votes. Every verdict already decided under the current identity
table was decided under that rule; loosening it would retroactively change what
"independent" meant in the record, which is why this is irreversible rather
than merely awkward.
The practical consequence is worth stating because it is the real finding
hiding behind this decision: **the models ARGO already serves are not what the
new wallet adds.** ARGO's canonical table covers Sonnet 5, Opus 5 and
Opus 4.5. It does **not** cover **Haiku 4.5** or **Sonnet 4.6**. So the seats
the Anthropic wallet genuinely adds are those two, plus the option of moving an
existing Claude seat off ARGO onto the new wallet — not three extra Claude
chairs.
**If you disagree:** the alternative is a per-wallet canonical suffix
(`anthropic/claude-sonnet-5@api`), which would let both be seated. That changes
`DEFAULT_CANONICAL_BY_KIND["anthropic"]`, breaks answer reuse between the two
routes (the reuse key is built from `canonical_model`, `council/store/schema.py`
around line 236), and makes every historical verdict's independence claim
incomparable with future ones. Do not do this without a written decision from
Chris; it is not a code change, it is a change to what the council means.
**Confidence:** high — the collision was verified by reading both tables, and
the refusal path was read in `Roster._check_independence`. What would raise it
further is nothing; what would change it is a deliberate redefinition of
independence.

### Q2. How is Anthropic's usage object mapped onto `prompt_tokens` / `completion_tokens`? -- DECIDED: inside the backend, as the SUM of all three input fields, with a live-call acceptance criterion.
**Blast radius:** IRREVERSIBLE
**Why:** `engine/worker.py:3440` is
`int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)`.
A backend that passes `input_tokens`/`output_tokens` through reports **zero**
spend: `_enforce_budget` never trips `budget_exhausted`, the halt never fires,
the stored call rows carry no usable usage, and the quote's balance-delta
calibration reconciles against nothing. Irreversible because the damage is to
the **record**: a run completed under zero-usage accounting cannot be
retrospectively priced, and the $200 wallet would be spent before anything
noticed.
Written as the sum —
`input_tokens + cache_creation_input_tokens + cache_read_input_tokens` — rather
than the bare `input_tokens`, because `input_tokens` counts only tokens after
the last cache breakpoint. With no caching the cache fields are 0 and the two
forms agree, so the sum costs nothing now and is the difference between
surviving and silently under-reporting the day anyone enables caching.
Closed by an **acceptance criterion against a real call** (AC1) rather than a
unit test, because a stub built from the same documentation as the code would
pass under both the correct and the broken mapping — the defining property of a
test that proves nothing.
**This is not a hypothetical, and the external scan settled that.** Of six
multi-provider libraries it read, three sum the three input categories
(LiteLLM's `calculate_usage`, LangChain's `_create_usage_metadata`, Vercel's
`convertAnthropicUsage`) and two do not — and it extracted and *executed*
aisuite's and LlamaIndex's functions on input=18 / cache-creation=2,284, each
returning `prompt_tokens = 18` against a true input total of 2,302. Two shipped
defects of exactly this class are cited: Vercel AI SDK **#9921** (18 instead of
2,302, closed via #10975) and LiteLLM **#28354** (a field-name mismatch whose
fallback returned zero). Three independent implementations converged on the sum;
the two that did not were measurably wrong.
**If you disagree:** mapping in the engine instead would mean
`engine/worker.py` learning provider-specific field names, which is the
coupling `kbu-ai-council-v1`'s provider layer exists to prevent, and would need
the same change in `_record_attempt` (around line 2013) and
`_record_output_sample` (around line 1636).
**Confidence:** high on the trap and on where the engine reads; **medium on the
exact SDK attribute names**, which is phase 1's job to pin before phase 2
builds on them.

### Q2b. Should the SDK client be allowed to retry? -- DECIDED: no. Construct it with `max_retries=0`.
**Blast radius:** MEDIUM
**Why:** the SDK's `_should_retry`
(`anthropic-sdk-python`, `src/anthropic/_base_client.py:906`, read by the
external scan) retries on HTTP status **including 429**, without examining
`error.details.error_code`, and the client default is 2 retries. Two
consequences, and the second is the expensive one:
1. It breaks a **stated invariant** of this repository. `llm/__init__.py`'s
   module docstring: "Exactly one attempt per call. Retries, backoff, budgets,
   halts, concurrency limits and single-flight collapsing belong to the caller
   (for the council, to `CouncilWorker`) because only the caller knows what a
   second attempt would cost." An api type that retries internally makes
   `CouncilWorker`'s own `max_attempts` a multiplier rather than a limit.
2. It **triples the spend-cap spiral**. A spend-cap 429 can never succeed, so
   the SDK's two retries are two guaranteed-wasted calls per attempt, each
   consuming the per-minute request allowance — which is how the engine's
   sustained-rate halt then fires and reports a rate problem for an empty
   wallet. Compounded with `CouncilWorker`'s retries, one item becomes up to
   nine upstream requests. The scan cites the analogous LiteLLM **#32785**,
   where three configured retries produced four upstream calls against a
   quota-exhausted account.
**If you disagree:** it is one keyword argument, so this is cheap to reverse —
which is why the blast radius is MEDIUM and not higher. It is, however,
expensive to *omit*, and omitting it is the default: a developer who simply
constructs the client gets retries. That asymmetry is why it is written down as
a decision rather than left as an implementation detail.
**Confidence:** high on the SDK behaviour (read at a pinned commit by the scan)
and high on the invariant (quoted from this repository). What the scan could
not establish is any released client that handles
`enforced_spend_limit_reached` specially — so there is no prior art to copy
here, only the taxonomy to get right ourselves.

### Q3. One alias per model, or one endpoint for all Claude models? -- DECIDED: one alias per Anthropic RATE-LIMIT BUCKET, with `account` set to the bucket and required by config validation.
**Blast radius:** MEDIUM
**Why:** `account` is read by both `quote.py::_gateway_of` and
`engine/worker.py::_build_limits`, so it decides the pricing bucket and the
pacing group at once. The brief predicted that splitting for pricing would
create N independent pools against one account-wide limit — the first MAG
pilot's 427-of-500 failure. **It does not, because Anthropic's limits are
per model-class bucket, not per account.** One alias per bucket is therefore
correct on both axes simultaneously: the pools really are independent at the
provider, and the quote buckets at the granularity pricing needs.
The bucket is the unit, not the model, because Opus 4.8/4.7/4.6/4.5 share one
allowance (and Sonnet 4.6/4.5 another). Keying per *model* would configure two
independent pools against one shared bucket and reproduce the MAG failure in
miniature. Keying per *account/wallet* would collapse three genuinely separate
allowances into one and throttle the run to a third of its capacity.
Made a **refusal at config load** rather than documented convention because the
failure is quiet in both directions — a wrong grouping either halves throughput
or doubles the effective rate, and neither announces itself. A convention that
is only in a comment is one an operator discovers by being billed.

**Round 1 of confront found the first draft's validation had a hole, and it was
right.** Refusing only "two endpoints sharing an `account` across different
buckets" leaves the opposite case open: two aliases for the **same** bucket with
two different arbitrary `account` strings pass every named check and produce two
independent pacers — reproducing the exact over-subscription the validation
promises to prevent. Closed by requiring, for an `anthropic` endpoint, that
`account` be **exactly the resolved rate-limit bucket name**. Two aliases on one
bucket then necessarily share an `account` string, and the hole shuts without a
new config key. Also added, because the draft left it unspecified: a seated
canonical model with **no entry in the bucket table** is refused at load, naming
the canonical identity and the override path — the alternative is an unknown
model silently becoming its own bucket, which is the same hole again.
**What was deliberately NOT adopted** from the confront's recommendation here: a
required `options.quota_scope` naming the operator's organisation/workspace. The
objection behind it is real — several API keys can share one quota, and two
organisations legitimately hold independent pools for one model — but the
proposed field is **operator-asserted and unverifiable** ("validation cannot
discover them from API keys", as the report itself says), so it buys a key that
can be wrong rather than a check that can fail. There is one Anthropic wallet
and one organisation here. Multi-organisation use is named as an explicit
non-goal in Out of Scope instead, which is honest about the limit rather than
pretending a field closes it.
**If you disagree:** the fallback is `account` = the wallet with per-model
sub-bucketing carrying all the pricing exactness (Q11's machinery is already
doing that work, so the quote stays exact). What you lose is pacing: one
semaphore and one pacer sized to the smallest declared limit across all seated
Claude models, i.e. the fastest model paced at the slowest model's rate. That
moves `DEFAULT_MAX_CONCURRENCY["anthropic"]`, the `account` validation in
`EndpointConfig._check`, the rate-limit bucket table, the
`default_config.yaml` example, and the `_build_limits` tests.
**Confidence:** high — the per-bucket structure was read today off the live
rate-limits page, including the footnotes naming which models share a bucket.

### Q4. Should the backend send `temperature`, and how does it tell "explicitly set" from "field default"? -- DECIDED: omit every sampling parameter by default, and make `_seat` carry the distinction by setting `temperature=None` for an `anthropic` seat whose model params omit it.
**Blast radius:** MEDIUM
**Why:** `temperature`, `top_p` and `top_k` are **removed** on Claude Sonnet 5,
Opus 5, Opus 4.8 and Opus 4.7 and return HTTP 400. `SeatSpec.temperature`
defaults to `0` — a field default, not a decision anyone made about this
provider — and `HttpBackend._params` sends it whenever it is not `None`. A
naive port therefore fails **every call on every current Claude model**. The
repository already knows this through ARGO
(`ARGO_NO_TEMPERATURE_MODELS = {"claudeopus47", "claudeopus48", "claudesonnet5"}`),
which is strong corroboration from an independent route.

**Round 1 of confront found that the obvious form of this rule cannot be
implemented, and it was right.** The first draft said "omit unless the seat's
`params` explicitly carries one". It cannot work:
`CouncilConfig._seat` does `temperature = params.pop("temperature", 0)`
(`council/config.py:982`), so an explicitly configured temperature is **moved
out of** the model's params and onto `SeatSpec.temperature`, and
`as_llm_request` then copies that field separately from `extra`. The confront
executed `_seat` with `params={"temperature": 0.3}` and measured the result:
`seat.temperature == 0.3`, `seat.extra == {}`. So a backend reading only
`req.params` **silently drops an explicitly configured temperature**, and one
reading `req.temperature` **sends the default 0 on every call**. Two observably
different wrong implementations, which is exactly the kind of thing an
autonomous build picks one of at 2am.

The fix leans on a contract that **already exists** rather than inventing one.
`LLMRequest.temperature`'s own docstring says: "`None` means 'do not send a
temperature at all', which is not the same as sending zero -- several models
reject an explicit temperature outright." That is precisely this situation. So:
- `_seat` is changed so that for an `anthropic` endpoint, a model whose params
  **omit** `temperature` yields `SeatSpec.temperature = None`, while an
  explicitly configured one is preserved verbatim (including an explicit `0`).
  Every other endpoint kind keeps today's `0` default, byte for byte.
- The backend sends a sampling parameter **iff `req.temperature is not None`**,
  and likewise for any `top_p`/`top_k` arriving through `params`.
- A direct (non-council) `LLMRequest` caller opts in the same way the field
  documents: by passing a non-`None` temperature. The field's own default is
  `0`, so a direct caller who never thought about it does get `0` sent — which
  is the pre-existing behaviour of every other backend and not this PRD's to
  change. Tests cover omitted, explicit-zero and explicit-non-zero separately,
  because those are three different cases that one assertion would blur.

The honest cost, unchanged: Haiku 4.5 and Sonnet 4.6 *do* accept `temperature`,
and their provider default is 1.0, not 0. So omitting it makes those two seats
more variable than a `temperature: 0` seat on another route. For the current
generation that variability is governed by adaptive thinking and `effort`
rather than by temperature anyway, and a seat that wants determinism on a model
which supports it declares it and gets it.
**If you disagree:** the alternative is a per-model reject-list — a module-level
frozenset keyed on served model id, plus a config override, plus a decision for
unknown models where "send" reintroduces the failure and "omit" is this
decision with extra steps. It also **rots**: a model released next month that
rejects sampling parameters fails every call until someone ships code. Changing
this moves `_seat` in `council/config.py`, the request builder in
`llm/apis/anthropic.py`, AC12, and the three temperature tests.
**Confidence:** high — the mechanism was read and then independently executed
by the confront adversary, which is why the decision changed shape between the
draft and this round.

### Q5. Should thinking be disabled to protect the output budget? -- DECIDED: no. Leave the provider default on, map the truncation signal, and raise the documented seat ceiling.
**Blast radius:** MEDIUM
**Why:** thinking is on by default and adaptive on Opus 5 and Sonnet 5, its
tokens are billed and counted as **output**, and `SeatSpec.max_output_tokens`
defaults to **4000**. So truncation is the expected case, not an edge case.
Disabling thinking looks like the fix and is the wrong one: `{"type":
"disabled"}` has a documented failure mode of leaking `<thinking>` tags into
the visible response, and this backend's output is parsed as a JSON object. A
leaked tag turns a working seat into an unparseable one, trading a diagnosable
failure for a confusing one. On Opus 5 it is additionally rejected outright
above effort `high`.
So: leave the default, map `stop_reason: "max_tokens"` onto the existing
"a cut-off answer is not a vote" truncation failure (without which a truncated
answer is reported as "reply contained no JSON object" — wrong cause, wrong
seat, every time), handle `<thinking>` as well as `<think>` in the cleaner, and
document that an `anthropic` seat's `max_output_tokens` must cover thinking
plus answer. `effort` is exposed as a passthrough parameter and **not chosen
here**: it is a quality decision about a specific council run, which this PRD
scopes out.
**If you disagree:** disabling thinking per-seat is a `params` passthrough and
needs no code change, so this is reversible per seat without touching the
build — which is why it is MEDIUM and not higher.
**Confidence:** high on the mechanism and the default; **medium on how much
output budget thinking actually consumes on a council-shaped prompt**, which is
unmeasured and is why the decision is "document and map" rather than "pick a
number".

### Q5b. Can the build declare acceptance without a credential, and is AC4 even achievable live? -- DECIDED: no, and no. The build completes offline; live acceptance is operator-gated, and AC4 becomes an injected-response test.
**Blast radius:** MEDIUM
**Why:** round 1 of confront refused to treat the draft's testing contract as
achievable, and it was right on both counts. The draft said AC1–AC4 "are
executed against a live call with a real key" and that "no stub can satisfy
them". Two problems:
1. **No autonomous build has a credential.** The confront process had no
   non-empty `ANTHROPIC_API_KEY` and no installed SDK. Installing the SDK closes
   the dependency gap; it cannot create a key. So every phase could complete
   correctly and the PRD would still be unable to declare acceptance — and the
   failure mode that matters is a build that *claims* it anyway, which is the
   measured pattern on this fleet (a PRD at `done` whose acceptance step nobody
   ran).
2. **AC4 is impossible live, by construction.** It requires "an endpoint whose
   provider reports no usage object". There is no safe, supported way to make
   the real Anthropic API omit its usage object. A criterion that can only be
   met by a response the provider will not produce is not a criterion.
So the contract splits honestly:
- **AC4 is an engine integration test** using an injected provider response with
  the usage object omitted, and is explicitly **excluded** from the live-only
  set. It is still a real test of real engine behaviour — `_enforce_budget`'s
  `budget_usage_unknown` halt — just not of the provider.
- **AC1, AC2, AC3 and AC6 stay live-only** and the PRD's delivery state is
  **"live-acceptance-pending"** until an operator supplies four things: the
  credential, the model(s) to call, an isolated council store, and an explicit
  spending cap. Implementation and offline verification may complete without
  them.
- A build **records** pending live criteria and **does not claim acceptance**.
  The rehearsal script enforces this by refusing to run without a key rather
  than falling back to a fixture.
This is the one place where the right answer was to make the PRD's own success
condition weaker and more honest, rather than to specify harder.
**If you disagree:** provisioning a key, a model, an isolated store and a cap
before dispatch would let a build close AC1–AC3 and AC6 itself. That is a
better outcome and this decision does not prevent it — it only refuses to
*assume* it. Changing it means provisioning those four inputs, not editing the
document.
**Confidence:** high. The credential's absence was measured, and AC4's
impossibility follows from the provider's own response contract.

### Q6. Should structured outputs (`output_config.format`) be requested? -- DECIDED: no, not in v1.
**Blast radius:** LOW
**Why:** this API genuinely supports server-enforced JSON, unlike the
OpenAI-compatible `response_format` that `http_common`'s module docstring
refuses because "servers disagree about it and several silently reject the
whole request". So the existing rationale does not transfer and a new one is
owed. It is this: enabling schema enforcement on **one seat of a council**
makes that seat's failure modes structurally different from its peers'. The
council exists to compare independent answers; a chair that can never return
malformed JSON while its colleagues can is not a fairer chair, it is a chair
whose `invalid_output` rate is incomparable. The engine validates every answer
against the query schema anyway, so enforcement buys no correctness — only an
asymmetry.
**If you disagree:** it is a per-request parameter, so it can be turned on later
without re-architecting; it would change `schema_enforced` from `False` to
`True` on those results, which readers of historical call rows should know is
not comparable across the change. Also incompatible with document citations,
which nothing here uses.
**Confidence:** medium-high. The asymmetry argument is a judgement, not a
measurement; what would settle it is a run comparing `invalid_output` rates
across chairs.

### Q7. Should the pacer self-tune from the `anthropic-ratelimit-*` response headers? -- DECIDED: no. Record them; do not feed them back.
**Blast radius:** LOW
**Why:** this is the first gateway in the project whose limits are
machine-readable at all, so the temptation is real and worth answering
explicitly rather than leaving as an omission. Against feeding them back: the
pacer is in-process and a queue-mode run has several worker **processes**, so a
header-derived limit would be applied N times over — the same arithmetic error
the existing static apportionment at least makes *visibly*. The `remaining`
counts are rounded to the nearest thousand, the limits reported are the "most
restrictive currently in effect" (which can be a workspace limit rather than
the model's), and the figure is already stale when it arrives. A self-tuner
built on that is a control loop over a noisy, delayed, multi-consumer signal —
the shape that spirals.
The external scan sharpened this and did not soften it. There is header-driven
prior art — Promptfoo lowers concurrency below 10% remaining and raises it after
sustained success, AIMD-style — but also a caveat that undercuts the whole
approach for this use: **lowering concurrency does not ensure that several large
prompts fit within ITPM**, and a council's prompts are large. Add that a
response is a snapshot, that snapshots can arrive out of order, and that a
later-arriving *older, larger* `remaining` value would raise the allowance
exactly when it should fall. The scan also found **no confirmed client that
discovers sibling-model bucket membership from headers at all**, so there is no
implementation to copy even if we wanted one.
Recording them is the cheap half and catches the error that actually matters: a
configured `max_requests_per_minute` that does not match what the provider says
it allows. AC6 makes that check an acceptance step.
One refinement taken from the scan: the bucket should be scoped to the
credential's **organisation/workspace**, not to the API key, because several
keys can share one quota. Reflected in the bucket-table and `account`
conventions, and in the documentation task.
**If you disagree:** a self-tuner would be a new control path in
`_RateBucket`, needs a cross-process story (the existing
`requests_per_minute` apportionment parameter is where that would land), and
should be its own PRD with a measured run behind it.
**Confidence:** high on the header semantics, which were read off the live docs
including the rounding and most-restrictive caveats.

### Q8. What should `DEFAULT_MAX_CONCURRENCY["anthropic"]` be, and what actually binds? -- DECIDED: 4 — and state plainly that NEITHER lever can express this provider's binding limit.
**Blast radius:** LOW
**Why:** 4 is deliberately below the `openai` default of 16: an unpaced endpoint
at high concurrency is how the MAG pilot errored 427 of 500 items, and a low cap
with a correct pacer is safe while the reverse is not. Operators raise it per
endpoint.

**The draft's framing was wrong and round 1 of confront corrected it.** The
draft said "the per-minute rate binds first", which conflates two different
limits. This provider enforces **three** axes — RPM, ITPM (input tokens per
minute) and OTPM (output tokens per minute) — and for a council's workload the
**token** axes are what bind: even the Start tier allows 1,000 RPM for most
models, which no plausible concurrency setting approaches, while a council
prompt carrying a per-item GO menu is large. `max_requests_per_minute` paces
**requests**, so it cannot express a token-per-minute ceiling at all, and
`max_concurrency` cannot either. The confront's phrasing is the accurate one: a
correct RPM value plus concurrency 4 **does not guarantee** compliance with
either token allowance.
So the honest statement of this decision is that the two available levers bound
the request rate and the parallelism, the provider's binding constraint is
elsewhere, and what covers the gap is the **halt**: a sustained run of flagged
429s stops the endpoint (`RATE_LIMIT_HALT_AFTER`, 5 inside a 60-second window).
That is detection after the fact, not prevention — and it is the correct scope
choice here only because the alternative is a token-aware pacer, which is new
mechanism and belongs with a measurement of the real prompt and output
distributions. Named as such in Out of Scope rather than implied to be covered.
**If you disagree:** the concurrency number is one entry in one dict with a
per-endpoint override, so it is the cheapest value here to change. The *framing*
is not a value — a document that claims the per-minute rate binds first would
have the next operator tune the one knob that cannot help.
**Confidence:** high on the three axes and on the Start-tier figures, both read
off the live rate-limits page; high on the correction, which the confront
reached independently from the same source.

### Q9. Should `anthropic` get a `VERIFIED_OUTPUT_CAPS` entry to enable batching? -- DECIDED: no.
**Blast radius:** LOW
**Why:** `max_tokens` on `/v1/messages` is a required, hard, documented
ceiling, so unlike either CLI an `anthropic` seat *could* legitimately earn an
entry — this is the first kind that could. But the table's own comment is
explicit that an entry goes in only once a mechanism is "independently measured
end to end against the live" provider, "never to make a config option *look*
enabled". Nothing here has been measured against a live call. With no entry the
seat runs as singles, which is what every other seat does today, so this
changes nothing relative to current behaviour.
It is named rather than omitted because the cost is real and belongs in the
quote's assumptions: singles means the per-item GO menu is re-sent per item
with no batch amortisation.
**If you disagree:** an entry plus a measured batch run is a separate, small
PRD and should carry the measurement.
**Confidence:** high — read directly from the table's comment and
`_effective_output_cap`.

### Q10. Should the shipped code carry a list of Claude model ids? -- DECIDED: only as overridable defaults; adding a model must be a config act.
**Blast radius:** LOW
**Why:** two code-side tables are unavoidable — canonical identities
(`DEFAULT_CANONICAL_BY_KIND["anthropic"]`) and the rate-limit bucket mapping
the `account` validation checks against. Both must be **overridable from
configuration**, because the alternative is that every Anthropic model release
rots the shipped code. `ModelConfig.canonical` already overrides the canonical
default, so only the bucket table needs a new override path.
This decision is load-bearing for a specific reason: the live docs list
`Claude Opus 5.5`, `Claude Sonnet 5.5` and `Claude Haiku 5.5`, and this design
session could not obtain their model **ID strings** from any source it could
read. A design that hardcoded a closed model list would ship unable to seat
models that already exist.
**If you disagree:** a closed list is simpler to validate and refuses typos
earlier; the price is a code change per model release, which is the kind of
cost that gets paid by whoever is unlucky rather than by whoever chose it.
**Confidence:** high on the need; medium on the override mechanism's exact
shape, which the implementing task may improve on provided it stays
config-overridable.

### Q11. Should the quote price per model inside a gateway, or rely on one rate per gateway? -- DECIDED: price per canonical model inside each gateway.
**Blast radius:** LOW
**Why:** with Q3's bucket-per-alias keying, every current rate-limit bucket
happens to contain models at a single list price, so one rate per gateway is
exact **today**. Nothing guarantees it stays exact: Anthropic can add a model
to an existing bucket at a different price, and a quote that is right by
coincidence is indistinguishable from one that is right by construction —
until it silently is not. The change is confined to one function, needs no new
`QuoteLine` field (`canonical_model` is already there) and no `rates` schema
change (`rates` is `Dict[str, RateConfig]` keyed by a free string, so a
composite `"<gateway>/<canonical>"` key just works).
It also pays for itself immediately in one real case: seating Opus 4.8 and
Opus 4.7 together — correctly one `account` under Q3 — is two models in one
gateway, which without this is two models at one rate.
The `priced: "partial"` verdict is the part that matters most. A gateway where
some models have rates and some do not must not report a dollar total; that is
the same error as substituting a list rate for an unknown one, which this
module already refuses on measured grounds.
**Round 1 of confront found that the first draft's own fix introduced the
silent mispricing this module exists to prevent. It was right, and this is the
most consequential correction in the round.** The draft published a tri-state
`priced: "partial"`. But `Quote.to_dict` selects priced gateways **by
truthiness**:

```python
priced = [g for g in self.gateways if g.get("priced")]
unpriced = [str(g["gateway"]) for g in self.gateways if not g.get("priced")]
...
"dollars_complete": not unpriced,
```

`"partial"` is a non-empty string, so a partially-priced gateway would land in
`priced`, stay out of `unpriced`, and leave `dollars_complete` reading **true** —
publishing a subtotal as a complete cost. The CLI has the same boolean
assumption (`interfaces/cli/council.py:337`, `if not gateway.get("priced")`),
so it would print a partial gateway as fully priced. A quote that understates a
fixed $200 wallet is exactly how the wallet gets overspent.

**So the representation changed, and not in the way the confront suggested.**
Its recommendation was to fix every reader to test `priced is True`. The
adopted design is safer: keep `priced` a **strict boolean** and add a separate
`partially_priced: true` flag with a `dollars_known_subtotal`. Every existing
truthiness reader — including any this PRD has not found — then treats a
partial gateway as **unpriced**, which **understates** rather than overstates,
and `dollars_complete` goes false on its own with no change at all. Fixing N
readers makes the Nth one I missed overstate; failing safe makes it understate.
That asymmetry is the whole argument.
`Quote.to_dict` and the quote CLI are still updated — to *surface* the subtotal
and name the unpriced canonical models, so an operator sees what is missing —
but those edits are now additive presentation rather than load-bearing
correctness.

Three further holes the confront named in the aggregate shape, all adopted:
- a multi-rate gateway publishes `rate: null` and `rate_provenance: null`, with
  the contributing rate and provenance records under `by_model`. A single
  legacy rate pair cannot represent three rates, and picking one would
  misattribute provenance — which `RateConfig` makes **required** precisely so
  that no dollar figure is unlabelled.
- `RateConfig.currency` is a free string, so summing differently denominated
  rates is undefined. A quote whose contributing rates are not all one currency
  is **refused**, not summed.
- sum unrounded per-model costs and round once at the gateway, so the
  per-model figures and the gateway total cannot disagree by rounding.

**The claim that this change is "confined to one function" was therefore false
and has been removed.** It touches `_price_gateways`, `Quote.to_dict` and the
quote CLI, and the tests must assert on the **published quote and the CLI
output**, not only on `_price_gateways` — because the defect lived entirely in
the gap between them.
**If you disagree:** skipping per-model pricing leaves the quote exact for the
shipped configuration and makes a future mixed-price bucket silently wrong.
That is a defensible scope cut; it changes the pricing task and its tests, and
nothing else in the plan. What is **not** optional either way is the boolean
`priced`: a tri-state in that field is a live defect regardless of whether
per-model pricing ships.
**Confidence:** high on the mechanism; high on the 1:1 coincidence holding
today, having checked every bucket against the live pricing table; and high on
the truthiness defect, which was read in the source and confirmed by the
confront independently.

### Q11b. What happens to a model whose published price is not a flat per-token rate? -- DECIDED: v1 prices flat rates only, and says so; the half-specified "refuse or warn" guard is dropped.
**Blast radius:** LOW
**Why:** round 1 of confront found the draft held **three incompatible
contracts** for this at once — Q13.2 said rates "should refuse — or at minimum
loudly warn on — a rate declared for a model known to be size-tiered", Out of
Scope excluded Haiku 5.5 "as a priced seat", and Q10 permitted new models purely
through configuration. Refusal, warn-and-price, and stay-unpriced are three
different behaviours and a build would have had to pick one.
Resolved by choosing the simplest contract that is actually true: **v1 supports
flat input/output rates only**, and the "exactly quotable" claim is explicitly
scoped to models priced that way. The guard is **dropped** rather than
specified, for a reason that is the same as Q10's: implementing "known to be
size-tiered" requires a shipped list of which models are size-tiered, which is
precisely the code table that rots on every model release. And it buys little —
a model is only priced at all if an operator declares a rate for it, and
`RateConfig` already **requires** a `provenance` string for every rate, so the
operator has already asserted where the number came from.
What replaces the guard is documentation: the packaged config and the operator
docs name Haiku 5.5 as a model whose published input price is tiered by prompt
size ($0.10/MTok up to 100,000 tokens, $0.50/MTok above) and therefore cannot be
exactly priced by a single `input_per_mtok`. An operator who declares a flat
rate for it has been told what they are approximating.
**If you disagree:** the confront's alternative was a `pricing_mode:
flat|unsupported` field on model config with unknown models defaulting to
`unsupported`. That is a real option and it fails safe, but it makes every newly
configured model unpriced until someone sets a field — and since an unknown
model with no declared rate is *already* unpriced, the field only bites in the
case where an operator has gone to the trouble of declaring a rate, which is
the case where they have least need of being overruled. Adopting it changes
`ModelConfig`, the rates loader, and the pricing tests.
**Confidence:** medium-high. The contradiction was real and had to be resolved;
which resolution is best is a judgement, and the confront preferred the other
one.

### Q11c. Should the balance-delta calibration block be keyed on the rate-limit bucket? -- DECIDED: no. It is keyed on a new `billing_wallet`, because Q3 made `gateway` mean "rate-limit bucket" and there is one console balance per wallet.
**Blast radius:** LOW
**Why:** round 1 of confront found the draft **internally inconsistent** here,
and it was right. User story 16 asks for "one row per wallet rather than one per
model". The draft's Implementation Decisions kept `_balance_delta_block` keyed on
`gateway` and justified it with "there is one balance per wallet" — a sentence
that was true before Q3 and false after it. Once `gateway` means the rate-limit
bucket, three Claude buckets on one API wallet produce **three balance rows
asking for the same console balance**, and an operator who dutifully copies the
wallet delta into each row attributes the whole bill three times and corrupts
every per-bucket calibration derived from it.
So: an endpoint gains an optional non-secret `billing_wallet`, defaulting to its
`account` — which leaves every existing kind byte-for-byte unchanged — and
**required** on an `anthropic` endpoint. Pricing and pacing stay grouped by
`account`; balance-delta rows group by `billing_wallet`, aggregate the member
gateways' token totals, and name the member gateways in the row.
One further point adopted verbatim because it is a trap in the arithmetic: **a
single wallet balance delta does not identify each member model's effective
input and output rate.** One observation cannot be decomposed into three
models' four rates. The block must not generate per-model calibration rates
from it, and the documentation must say so — otherwise the next operator does
the division and writes three confident wrong rates into `ai_council.rates`.
**If you disagree:** the alternative is to drop story 16 and disable
balance-delta calibration entirely for shared-wallet buckets, which is honest
and cheaper. Leaving the function unchanged is the one option that is not
available, because it cannot satisfy the story it claims to.
**Confidence:** high on the inconsistency and on the triple-attribution; medium
on `billing_wallet` being the best shape, versus simply dropping the story.

### Q12. New PRD, or an amendment to `kbu-ai-council-v1`? -- DECIDED: a new PRD.
**Blast radius:** NONE
**Why:** `kbu-ai-council-v1` is at status `ready` in the registry, not `done`.
Amending a `ready` PRD edits a document an interactive conductor may claim, and
reopens a bundle whose work has already landed. More to the point, this PRD
*uses* the provider layer that one built, through the registry door and the
`ApiType` protocol, exactly as designed — which is evidence the layer worked
rather than a reason to reopen it. The places this PRD must touch pre-existing
code are the ones that layer did not make open, and each is named in
Implementation Decisions.
**If you disagree:** nothing in the build changes; only where the document
lives.
**Confidence:** high — the registry status was read directly.

### Q13. What I could not decide and did not guess.

Three external facts are left unreconciled. None blocks the build; each is
stated so it is not mistaken for something that was settled.

1. **The model ID strings for the 5.5 generation.** The live pricing and
   rate-limit pages list `Claude Opus 5.5`, `Claude Sonnet 5.5` and
   `Claude Haiku 5.5`; the bundled `claude-api` skill's model table (cached
   2026-06-24) does not, and supplies no ID strings for them. **Assumed
   instead:** the shipped canonical and bucket defaults cover only models whose
   ID strings are documented (`claude-haiku-4-5`, `claude-sonnet-5`,
   `claude-sonnet-4-6`, `claude-opus-5`, `claude-opus-4-8`), and Q10 makes
   adding the rest a configuration act. The implementing task must **not**
   invent an ID string by pattern — `GET /v1/models` returns the live list and
   is free.

2. **Whether `Claude Haiku 5.5` can be priced at all by the current schema.**
   Its input price is tiered by prompt size ($0.10/MTok up to 100,000 tokens,
   $0.50/MTok above) and `RateConfig` carries a single `input_per_mtok`. No
   keying strategy fixes this. **Assumed instead:** it is out of scope, and the
   `rates` config should refuse — or at minimum loudly warn on — a rate
   declared for a model known to be size-tiered, rather than pricing it wrong
   with a straight face. Which of refuse-or-warn is correct depends on whether
   anyone wants to seat it, which is a roster decision this PRD scopes out.

3. **Whether ARGO's `claudeopus5` also rejects `temperature`.**
   `ARGO_NO_TEMPERATURE_MODELS` contains `claudeopus47`, `claudeopus48` and
   `claudesonnet5` but **not** `claudeopus5`, while the direct API rejects
   sampling parameters on Opus 5. Either ARGO normalises the parameter away or
   this is a latent gap in the ARGO table. **I did not determine which, and did
   not change the ARGO table** — doing so on inference, in a PRD about a
   different api type, is exactly how an unrelated route breaks. It is recorded
   as an observation for a separate investigation and is explicitly out of
   scope here.

## Gotchas and Unintuitive Consequences

A careful reader of the Solution section would not predict these.

1. **The new wallet adds fewer chairs than it appears to.** ARGO's canonical
   table already covers Sonnet 5, Opus 5 and Opus 4.5, and Q1 keeps the
   independence refusal. So the models this wallet *newly* makes seatable are
   **Haiku 4.5** and **Sonnet 4.6** — everything else is a route change for a
   seat the council could already fill. Anyone planning a roster on "three new
   Claude chairs" is planning on a council that will be refused at
   construction.

2. **Fixing the usage mapping can make a previously "free" run start halting.**
   `_enforce_budget` halts an endpoint with `budget_usage_unknown` when a
   completed call reports no usage, and with `budget_exhausted` once
   `epoch_tokens` passes `max_total_tokens`. An endpoint that was reporting
   zero spend was silently exempt from both. Correct accounting is what makes
   the ceiling bite — so the first correct run may halt where a broken one ran
   on. That is the feature working, and it will not look like it at 2am.

3. **Three aliases share nothing but the bill.** Under Q3 each rate-limit
   bucket is its own `account`, so each has its own semaphore, its own pacer,
   its own budget state, its own halt state and its own gateway row in the
   quote. `max_total_tokens` is **per endpoint**, so a single $200 ceiling
   cannot be expressed as one number — three endpoints need three budgets whose
   sum is the wallet, and nothing in the system adds them up or stops the third
   when the first two have spent everything. **The wallet is not a single
   enforced ceiling.** That is the largest gap this PRD leaves open, and it is
   left open deliberately: a cross-endpoint budget is new mechanism, not
   configuration.

4. **Halting one alias does not stop the run.** A halt is per endpoint. If
   Haiku's budget trips, the Sonnet and Opus seats keep working — which is
   usually what you want, and is not what "the wallet is empty" should mean. In
   the spend-cap case all three *will* halt, but only as each one next makes a
   call and gets its own 400/429; the run drains rather than stops.

5. **The operator-set spend limit is the likely path, and it is a 400.**
   Fencing a $200 credit with a $200 console limit is the obvious thing to do,
   and it produces an HTTP 400, not a 429. Without the Q2/taxonomy mapping that
   is `InvalidOutput`: not retryable, **not halting**, so every remaining item
   fails individually with a reason that reads like a model fault. The run looks
   like it is working through a backlog of bad answers.

6. **Omitting `temperature` changes Haiku's and Sonnet 4.6's behaviour, not
   just Opus's and Sonnet 5's.** Those two models accept `temperature` and
   default to 1.0. A seat that previously would have been sent `0` now gets the
   provider default, so answers are more variable. This is a *behaviour change
   on exactly the two models Q1 identifies as the new wallet's real additions* —
   the overlap is unlucky and worth knowing before reading variance in a run as
   a model property.

7. **Prompt caching is a throughput lever, not only a cost lever.** Cache reads
   do **not** count toward ITPM on current models. So caching raises the
   effective token rate limit as well as cutting cost — a different and
   stronger argument than the cost one that got caching scoped out. It remains
   out of scope, but the reason for revisiting it is now better than the reason
   it was dropped.

8. **The budget token total and the billed dollar total diverge the moment
   caching is on.** Q2 sums all three input fields into `prompt_tokens`, which
   is the right *token* count. But cache reads are billed at a fraction of base
   input, so the quote would then overstate dollars. That is why the backend
   refuses `cache_control` in v1 rather than passing it through: the refusal is
   not conservatism, it is the only state in which the token figure and the
   dollar figure agree.

9. **`strip_think` is the wrong tag.** The shared helper matches
   `<think>…</think>` (the open-weight thinking models on poplar). Anthropic's
   documented leakage uses `<thinking>`. A cleaner that reuses `strip_think`
   unchanged will leave `<thinking>` blocks in the text handed to
   `extract_json_object`, which usually still finds the object — so this fails
   *intermittently*, on the subset of leaks that contain a brace.

10. **An `anthropic` endpoint will be refused at config load for a reason about
    answer caching.** With no `base_url` default it falls down
    `EndpointConfig._check`'s plug-in branch and is refused for not declaring
    `reuse_namespace`. The message is correct and the cause is unguessable from
    it. Filling the `base_url` default is what prevents a support question
    about reuse namespaces from being the first thing an operator meets.

11. **`HTTP_KINDS` means OpenAI-shaped, not URL-reached.** It currently reads
    `("argo", "openai")`. Adding `anthropic` to it because the endpoint has a
    URL would quietly tell every reader of that tuple that the request shape is
    OpenAI's. The tuple's name is the trap; leave it alone.

12. **Preflight reporting `ready` proves nothing about the model.** For an api
    type with no `preflight`, `llm.preflight_api` reports `ready` because
    "there is nothing about it that could refuse". A credential-only preflight
    is a real improvement on that, but it still says nothing about whether the
    configured model id exists — and an unknown model id is a 404 mapped to
    `InvalidOutput`, which fails per call rather than halting. `GET /v1/models`
    is free; the gap between "key works" and "model exists" is the one an
    operator will fall into.

13. **This credit buys no discount.** ARGO was measured billing Claude at
    vendor list within 1.5% on the 2026-10-20 bill. The $200 is a **second
    wallet at the same price** — it buys independence from the ARGO cap and
    access to two models ARGO does not serve, and nothing else. No task in the
    plan may justify itself on a saving, and a quote comparing the two routes
    should come out roughly equal.

14. **The SDK brings a second HTTP stack.** `anthropic` 1.x is built on
    `httpx2`; the project depends on `httpx 0.28`. They are separate
    distributions and coexist without conflict, but a process using both an
    `anthropic` and an `openai` endpoint holds two independent connection
    pools, timeout regimes and proxy configurations. The endpoint's `proxy` key
    is honoured by `HttpBackend`'s stack and must be wired explicitly for the
    SDK's — it will not be inherited.

15. **Once normalised, the usage dict must never be read as native Anthropic
    usage again.** The scan cites Hermes **#123157**: adapters correctly emitted
    `prompt_tokens`/`completion_tokens`, and then a downstream pricing function
    selected a *native* Anthropic usage parser **by provider identity**, looked
    for `input_tokens`/`output_tokens`, found neither, and reported all usage
    and all cost as **zero** — with a deterministic reproduction. Nothing in
    this repository does that today, which is exactly why it is worth writing
    down: the natural future change is a cost path that branches on
    `endpoint_kind` to "handle Anthropic properly", and that change
    reintroduces the bug this PRD exists to prevent. **After the backend, there
    is no Anthropic-shaped usage anywhere in the system.**

16. **The `all` extra is installed by CI, so an unmarked dependency breaks the
    3.9 job rather than the feature.** The SDK needs Python ≥ 3.10; CI tests
    3.9, 3.10 and 3.11. The failure would not look like "the anthropic backend
    is broken" — it would look like the whole test suite failing to install on
    one matrix leg, in a PR that appeared to be about a council backend.

17. **Turning retries off in the SDK makes the council's own retry budget mean
    what it says — and look smaller.** With `max_retries=0` an operator
    comparing upstream request counts before and after will see fewer attempts
    per item and may read it as calls being dropped. They are not: the
    attempts were previously happening invisibly inside the SDK, outside
    `CouncilWorker`'s accounting and outside the pacer.

18. **Anthropic's own OpenAI-compatibility layer would have hidden all of
    this.** It silently ignores `response_format` and tool `strict`, does not
    support prompt caching, and makes thinking output unavailable
    (first-party documentation). Had this been built as a shim, the symptoms
    would have been a council whose Claude chairs quietly behaved differently
    from its others, with nothing in any error message to say why. The reason
    not to use a shim is not that it would fail — it is that it would work
    badly and silently.

19. **A tri-state in a boolean field is read as "true" by every caller that
    never heard about the third state.** This is the generalisation of the
    defect confront found in the draft's own pricing fix: `Quote.to_dict` and
    the quote CLI both test `if gateway.get("priced")`, so `"partial"` would
    have published a subtotal as a complete cost. It is worth stating as a
    gotcha and not only as a decision, because the same temptation recurs
    wherever a two-valued field needs a third case, and the safe direction is
    always the one where an unaware reader **understates**.

20. **Neither configurable limit can express what this provider actually
    enforces.** `max_requests_per_minute` paces requests; `max_concurrency`
    bounds parallelism; the binding constraint on a council's large prompts is
    **ITPM/OTPM**, which neither expresses. A correct RPM value and a
    conservative concurrency therefore do not guarantee compliance, and what
    covers the gap is detection after the fact — the sustained-429 halt — not
    prevention. An operator who reads "set `max_requests_per_minute`" as "now I
    am within my limits" has been misled.

21. **Tokens are not dollars, and the budget is denominated in tokens.**
    `max_total_tokens` is a token threshold checked **after** a call, so it
    cannot be set to "$200" and cannot be exact: the dollar value of a token
    varies by model and by whether it is input or output, which is the whole
    reason Q11 prices per model. Concurrent in-flight calls can also overshoot
    a soft ceiling before it trips. Combined with gotcha 3 — three endpoints,
    three budgets, nothing summing them — the honest summary is that the token
    budget is a backstop against runaway, not a wallet guarantee. The thing
    that actually enforces $200 is the **provider-side console spend limit**,
    which is why its 400 response (gotcha 5) is the signal that matters most.

22. **The rate-limit header observation is process-local and may never reach
    whoever needs it.** The backend exposes last-observed values on the
    instance. In queue mode the worker is a separate **process** from the
    submitter, so nothing carries that attribute back; and if debug logging is
    off, it is written nowhere. AC6 therefore has to define how the observation
    is obtained and reported, rather than assuming an attribute is visible —
    the rehearsal script reads it from the call it makes itself, which is the
    one context where it is guaranteed to be in scope.

## Sources Consulted

Read on `main` at `cb6cf1b` unless noted. Files are relative to
`src/kbutillib/domains/ai/`.

**Read in full:**
- `llm/__init__.py` — `BUILTIN_API_TYPES` (line 136), `ENTRY_POINT_GROUP`,
  `register_api_type`, `api_types`, `get_api`, `acomplete`, `preflight_api`,
  `supports_web_search`, `_load_entry_points`, `_register_builtin` and the four
  built-in factories (lines 662–699).
- `llm/protocol.py` — `REQUIRED_MEMBERS = ("complete", "effective_params")`
  (line 65), the `ApiType` protocol, the four optional members.
- `llm/types.py` — `LLMRequest`, `LLMResult`, `LLM_ERROR_KINDS`, the seven
  typed failures and their `retryable`/`halts_endpoint` class vars,
  `LLMError.rate_limited`, `as_llm_request`, `generation_params`,
  `WEB_SEARCH_PARAM`.
- `llm/apis/http_common.py` — `HttpBackend` and every hook;
  `read_chat_envelope` (lines 205–239, the `choices[0]` and
  `prompt_tokens`/`completion_tokens` read); the hardcoded
  `url = f"{self.base_url}/chat/completions"` (line 413); `_status_error`
  (429/401/5xx/4xx mapping, lines 645–680); the `finish_reason == "length"`
  truncation branch (line 480); `redact`, `strip_think`, `strip_fences`,
  `extract_json_object`, `client_kwargs`, `_model_params`, `_extra_body`,
  `_headers`.
- `council/backends/http.py`, `council/backends/http_common.py` — confirmed
  both are pure re-export shims onto `llm.apis.*`; nothing to change in them.
- `council/default_config.yaml` — the endpoint shape, and the `mag` commented
  example used as the precedent for the `anthropic` one.

**Read in the regions that matter:**
- `llm/apis/http.py` — `ARGO_NO_TEMPERATURE_MODELS`,
  `ARGO_FIXED_TEMPERATURE_MODELS`, `ARGO_GEMINI_TOKEN_FLOOR_MODELS`,
  `ARGO_BASE_URL`, `ARGO_USER_ENV`, `OpenAICompatibleBackend._bearer` (the
  `api_key` → `api_key_env` → `"EMPTY"` chain), `_static_headers`.
- `council/types.py` — `SeatSpec` and its `endpoint_kind` `Literal` (line 489),
  `ENDPOINT_KINDS`/`HTTP_KINDS`/`CLI_KINDS` (lines 112–121),
  `Roster._check_independence`, `default_reuse_namespace` (lines 210–240),
  `seat_generation_params`.
- `council/config.py` — `DEFAULT_CANONICAL` and `DEFAULT_CANONICAL_VERSION`
  (lines 77–90), `DEFAULT_CANONICAL_BY_KIND` (line 96),
  `DEFAULT_MAX_CONCURRENCY` (line 105), `BASE_URL_KINDS` (line 119),
  `_ENDPOINT_KEYS` (including the pre-existing `account`, `max_concurrency`,
  `max_requests_per_minute`, `max_total_tokens`, `reuse_namespace`, `options`),
  `EndpointConfig` (line 600) and `_check` (line 637), `ModelConfig`,
  `RateConfig` (line 744) and the required `provenance`,
  `validate_static_headers` and `_REFUSED_HEADER_NAMES`/`_FRAGMENTS`,
  `_require_registered_kind` (line 1333), the `rates` load block (line 1553).
- `council/quote.py` — `_gateway_of` (line 1556), `_price_gateways` (line 1569)
  including the "never substitutes a vendor list rate" rationale and the
  `unpriced` shape, `QuoteLine` (line 995, confirming `canonical_model` and
  `gateway` are already fields), `_balance_delta_block`, `_reuse_namespace`,
  the `max_requests_per_minute` read at line 1428.
- `council/engine/worker.py` — the account-wide pacer design block (lines
  144–190, carrying the measured MAG numbers), `DEFAULT_RATE_BURST`,
  `RATE_LIMIT_WINDOW_S`, `RATE_LIMIT_HALT_AFTER`, `_RateBucket`,
  `_AccountLimit` (line 402), `_build_limits` (line 673, the
  `cfg.get("account")` grouping and the min-capacity/min-rate arithmetic),
  `CouncilWorker.__init__`'s `concurrency`/`requests_per_minute`/`batch_size`
  docstrings (the cross-process apportionment note), `_enforce_budget` (line
  3084), `_token_total` (line 3438), `VERIFIED_OUTPUT_CAPS` (line 322) and its
  comment, `_effective_output_cap` (line 1602), `_note_rate_limit`,
  `endpoint_mapping`, `_reuse_namespace`, `ZERO_USAGE`,
  `CONFIGURATION_MISMATCH`.
- `council/store/schema.py` — `_ENUM = sa.String(32)` (line 125) and the
  `endpoint_kind` column (line 225); the reuse-key comment confirming the key
  is built from the reuse namespace and canonical model.
- `domains/genome/ontology_mapping/council_run.py` — `resolve_credentials`
  (line 272) and its per-kind branches (`argo` at 354, `openai` at 377, the
  CLI/`none` fallthrough at 456); `gateway_endpoints`; `_Supervisor`'s
  "HOLDS NO COUNTER OF ITS OWN" note.
- `pyproject.toml` — the core `dependencies`, the extras pattern and the
  `pymongo`/`backfill` lazy-import convention (lines 49–62), the `all` extra
  (line 164), and the in-file warning that the `dev` list is duplicated in
  `[dependency-groups]`.

**External, fetched 2026-10-29:**
- `https://platform.claude.com/docs/en/api/rate-limits` — the per-model-bucket
  RPM/ITPM/OTPM structure and the footnotes naming which models share a bucket;
  the full `anthropic-ratelimit-*` response-header table and its rounding /
  most-restrictive caveats; cache-aware ITPM and the
  `total_input = cache_read + cache_creation + input_tokens` identity; the
  spend-cap 429 with `enforced_spend_limit_reached` and no `retry-after`; the
  operator-set-limit HTTP 400 and its message prefix; the note that `max_tokens`
  does not count toward OTPM.
- `https://platform.claude.com/docs/en/about-claude/pricing` — per-model input,
  output, cache-write and cache-read prices; the size-tiered `Claude Haiku 5.5`
  rows; confirmation that `Claude Opus 4.5`–`4.8` are all $5/$25 and
  `Sonnet 4.5`/`4.6` both $3/$15.
- The bundled `claude-api` skill, including its API-drift table and
  `python/claude-api/README.md` — `AsyncAnthropic`, lazy/explicit credential
  resolution, the removal of `budget_tokens` and of sampling parameters on
  current models, adaptive thinking and `display` defaults, `stop_reason` /
  `stop_details`, `output_config.format` replacing `output_format`, the
  `<thinking>` leakage failure mode of disabled thinking, the typed exception
  classes, and the `anthropic` 1.x / `httpx2` dependency fact.

**External prior art, via the cross-family scan (`task-a7ad9d14`, codex on
h100) — read at the pinned commits the scan cites:**
- `anthropic-sdk-python` — `src/anthropic/_base_client.py:906`
  (`BaseClient._should_retry`, which retries 429 without inspecting
  `error.details.error_code`) and `pyproject.toml:11` (Python ≥ 3.10, and the
  `httpx2` dependency).
- LiteLLM — `litellm/llms/anthropic/chat/transformation.py:2747`
  (`AnthropicConfig.calculate_usage`, which sums the three input categories);
  `litellm/router_strategy/lowest_tpm_rpm_v2.py` (per-deployment/model/minute
  counters); issues **#28354** (field-name mismatch returning zero) and
  **#32785** (billing exhaustion conflated with throttling).
- LangChain — `libs/partners/anthropic/langchain_anthropic/chat_models.py:3316`
  (`_create_usage_metadata`), and the Google integration's `chat_models.py:2055`
  for contrast.
- Vercel AI SDK — `packages/anthropic/src/convert-anthropic-usage.ts:44` and
  `packages/google/src/convert-google-usage.ts:25`; issue **#9921** (18 tokens
  reported instead of 2,302), closed via **#10975**.
- aisuite — `aisuite/providers/anthropic_provider.py:281`
  (`_get_completion_usage`, which does **not** add the cache fields).
- LlamaIndex — `llama-index-core/.../callbacks/token_counting.py:39`
  (`get_tokens_from_response`, first-matching-field-wins, cache fields not
  added).
- Hermes — issue **#123157** (double normalisation zeroing all usage and cost).
- Promptfoo rate-limit documentation (header-driven AIMD concurrency) and the
  `agentpause` README (header staleness).
- `platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk` —
  Anthropic's own statement that its OpenAI-compatibility surface is for
  evaluation and silently ignores unsupported inputs.
- `platform.claude.com/docs/en/build-with-claude/prompt-caching` — cache reads
  at 0.1x base input, 5-minute writes 1.25x, 1-hour writes 2x; the scan's point
  that these are **prices, not token-count weights**.

**Platform state, measured today:**
- `anthropic` is not importable in `/home/chenry/venvs/kbdl`
  (`ModuleNotFoundError`) and appears nowhere in `pyproject.toml`.
- The kbdl venv's KBUtilLib install tree, obtained by asking the interpreter for
  `kbutillib.__file__`, is
  `/scratch/fliu/hub_home/chenry/H100Projects/KBUtilLib`, which `stat` shows
  shares device 64517 / inode 5167606134 with `~/projects/KBUtilLib` — a path
  alias, not a second clone. It is on `main` at `b1cbc40`, one commit behind
  `cb6cf1b`. `httpx` there is 0.28.1.
- The PRD registry (`AIAssistant/state/prd_registry.json`) —
  `kbu-ai-council-v1` is `ready`; `council-record-lakehouse-v1` is `ready`;
  `function-to-go-council-pipeline-v1` is `draft`.

**Read and found uninformative (recorded so the absence is visible):**
- `council/backends/*` — re-export shims only; no behaviour.
- `council/protocols.py` — the `Backend` protocol is satisfied through
  `llm.protocol.ApiType`; nothing kind-specific.
- A repository-wide `git grep` for `codex_cli` to find every closed enumeration
  of endpoint kinds. It returned exactly the sites named above, which is the
  basis for the claim that `SeatSpec`'s `Literal` is the **only** hard blocker.

## Out of Scope

1. **The Batch API lane — out of scope HERE, but it is a SEPARATE PRD that
   THIS ONE IS A PREREQUISITE FOR, and the planned run is not fundable without
   it.** This entry was corrected while this PRD was being written and the
   correction is material, so the superseded version is stated plainly: the
   draft called batch "worth roughly $127 on the planned run and not worth the
   build yet". **That number was wrong and the conclusion with it.**

   Session `a6182005` measured the real corpus from
   `~/Dropbox/Jobs/function-to-go/pmi_functions_for_council.tsv` (sha256
   `b2857214…`) on 2026-10-29: 45,708 distinct PMI functions, of which
   **29,179** have no council vote. The draft's figure was sized against
   21,333. At 29,179 items the Anthropic leg of any roster keeping three chairs
   costs **$313 against $200 of credit**; the Batch API's 50% discount brings
   it to **$156.52**, a 22% margin. Searching the roster space under per-wallet
   caps (ARGO ≤ $1000, Anthropic ≤ $200, an Opus-tier meta, a Sonnet-5 critic),
   there are feasible rosters at ~$992 total **with** batch and **none at all
   without it**.

   So the honest statement of this PRD's own value changes: it makes the credit
   **reachable, accounted and exactly priced**, which is a precondition for
   spending it at all — but on its own it does **not** make the planned
   29,179-item run affordable. Saying otherwise would be the "saving that does
   not exist" error in reverse.

   It stays out of scope rather than becoming a phase here because it is a
   different *execution shape* in the engine — submit, poll, retrieve, with
   results arriving out of order and keyed by `custom_id`, plus its own rate
   limits and its own queue-depth ceiling — not a parameter on the per-call
   worker. The dependency runs one way and cleanly: a batch lane needs the
   `anthropic` api type, the usage normalisation and the per-model pricing that
   this PRD builds. In particular the batch lane must price at **50% of the
   per-model rates**, which is only expressible once rates are keyed per model
   (Q11). **Recommended: a second PRD, `kbu-anthropic-batch-lane-v1`, naming
   this one as its prerequisite.** That is a recommendation, not a decision
   this session is entitled to make — the roster and run scoping belong to
   `a6182005` and to Chris.
2. **Prompt caching.** Out, but for a better reason than when it was dropped:
   the per-item GO menu dominates chair input so the cacheable prefix is
   unmeasured, *and* cache reads do not count toward ITPM, which makes caching
   a throughput lever as well as a cost one. A measurement task at most, and
   this PRD actively **refuses** `cache_control` so that the token and dollar
   figures cannot disagree in the meantime (Q2, gotcha 8).
3. **A cross-endpoint wallet ceiling.** `max_total_tokens` is per endpoint, so
   three aliases need three budgets and nothing sums them. Named in gotcha 3 as
   the largest gap this PRD leaves; closing it is new mechanism.
4. **A self-tuning pacer driven by the rate-limit headers** (Q7). The headers
   are recorded; nothing is fed back.
5. **Batching for `anthropic` seats** via a `VERIFIED_OUTPUT_CAPS` entry (Q9).
   Needs a measured end-to-end run.
6. **Server-side tools** — web search, web fetch, code execution.
   `supports_web_search` stays `False`. A council chair with live web access is
   a different kind of chair and changes what a vote means.
7. **Structured outputs / server-enforced JSON** (Q6).
8. **Streaming.** Not needed: the council's answers are small JSON objects well
   inside the non-streaming timeout. Required only for very large `max_tokens`,
   which no seat uses.
9. **Size-tiered pricing**, and therefore `Claude Haiku 5.5` as a priced seat
   (Q13.2). `RateConfig` cannot express it.
10. **Any change to the ARGO tables**, including the `claudeopus5` /
    `ARGO_NO_TEMPERATURE_MODELS` observation in Q13.3. Recorded for a separate
    investigation.
11. **The corpus, roster and scoping decisions for the planned council run.**
    This PRD is the code half only. In particular it does not choose `effort`,
    does not choose which models to seat, and does not size the run — though
    Q1's finding that the wallet adds Haiku 4.5 and Sonnet 4.6 rather than three
    new chairs is input that roster decision needs.
12. **Any council store migration.** None is needed; `endpoint_kind` is
    `String(32)` with no constraint. Stated so a task does not invent one.
13. **Managed Agents, the Admin API, Workload Identity Federation, fast mode,
    task budgets and the Files API.** Adjacent surfaces of the same provider,
    none of which a council seat touches.
14. **A token-aware pacer.** `max_requests_per_minute` and `max_concurrency`
    cannot express ITPM/OTPM, which is what actually binds this provider for a
    council's prompts (Q8, gotcha 20). Building one needs a measurement of the
    real prompt and output distributions first, and is its own PRD. What ships
    here is the sustained-429 halt, which is detection rather than prevention,
    and the documentation says so.
15. **Multi-organisation / multi-workspace quota scoping.** Several API keys can
    share one quota, and two organisations hold independent pools for the same
    model, so `account` = bucket name is only correct within one organisation.
    There is one Anthropic wallet and one organisation here. An
    operator-asserted `quota_scope` field was considered and rejected (Q3): it
    cannot be validated against anything, so it would record a claim rather
    than enforce a constraint. Named as a non-goal instead of papered over.

## Further Notes

**On the brief this PRD was written against.** Three of its five named "things
the design must get right" survive as stated: the usage-name mapping (Q2), the
refusal to subclass `HttpBackend` or use an OpenAI-compatible shim, and the
deliberate canonical collision with ARGO (Q1). One was correct about the
blocker and the absence of a migration (the closed `Literal`). One —
"N aliases are N independent pools against ONE account-wide limit" — turned out
to rest on a premise that holds for MAG and not for Anthropic, whose limits are
per model-class bucket. That changed the central decision from a trade-off into
a clean choice (Q3). The brief's two unprompted additions are also worth
naming: the guaranteed `temperature` 400 (Q4) and the HTTP-400 wallet-exhausted
path (gotcha 5) are both more likely to bite than anything the brief predicted.

**On what this PRD does not fix.** The wallet is still not a single enforced
ceiling (gotcha 3). Three endpoints carry three budgets and nothing adds them
up. An operator can still spend $200 by watching three numbers instead of one.
That is the honest limit of a configuration-shaped solution, and it should be
the first follow-on considered.

**On delivery.** This bundle was authored on h100, where the Dropbox working
tree is parked on `wip` and cannot be written. The commit lands on `main`
off-tree, which means the files exist on no machine's disk until `main` is
merged into `wip` on primary-laptop. A readable copy is placed under
`~/Dropbox/AIPlatform/artifacts/` and sha256-verified against the committed
blobs; the `main` → `wip` merge is filed as a blocked continuation because it
sweeps everything else on `main` with it and is Chris's call.

## Acceptance Criteria

**Four of these are LIVE-ONLY and a build cannot close them (Q5b).** AC1, AC2,
AC3 and AC6 require a real credential, the models to call, an isolated council
store and an explicit spending cap, all supplied by an operator. Until then the
PRD's delivery state is **live-acceptance-pending**: a build records them as
pending and **does not claim acceptance**. Every other criterion is closable
offline.

1. **(LIVE)** A real call to a live `anthropic` endpoint completes, and the
   stored call row for it carries a **non-zero `prompt_tokens`** and a non-zero
   `completion_tokens`. Verified by reading the row out of the council store,
   not by reading a log line, and not against any stub or fixture.
2. **(LIVE)** For that same call, `engine.worker._token_total(usage)` — the
   engine's own summing function, called directly — returns a non-zero value.
3. **(LIVE)** An `anthropic` endpoint configured with `max_total_tokens` set
   below the cost of two calls halts with `budget_exhausted` after the budget
   is passed, and the halt reason quotes the spend and the budget.
4. An `anthropic` endpoint that receives a response with **no usage object**
   halts with `budget_usage_unknown` rather than running on. This is an
   **engine integration test using an injected response**, explicitly excluded
   from the live set: there is no supported way to make the real provider omit
   its usage object, so a live-only form of this criterion could never be met.
5. A quote for a roster seating two Claude models in two different rate-limit
   buckets, with a rate configured for each, reports **two gateway rows with
   two different dollar figures**, and the per-model `by_model` figures sum to
   each gateway's reported total.
5b. A gateway carrying two canonical models with a rate for only one reports
   `priced: false` with `partially_priced: true`, names the unpriced model, and
   publishes its subtotal as `dollars_known_subtotal`. The **published quote**
   lists it under `unpriced_gateways` and leaves `dollars_complete` false, and
   the **quote CLI** names the unpriced canonical model. Asserted on the
   published `to_dict()` output and the CLI output, not only on
   `_price_gateways`.
5c. A quote whose contributing rates are not all one currency is refused rather
   than summed; and a multi-rate gateway publishes `rate: null` and
   `rate_provenance: null` with the contributing records under `by_model`.
5d. Balance-delta rows are keyed on `billing_wallet`: three `anthropic`
   endpoints on three rate-limit buckets but one wallet produce **one** row,
   aggregating their token totals and naming the three member gateways.
6. **(LIVE)** The `anthropic-ratelimit-requests-limit` value observed on a real
   response is recorded **by the component that made the call**, and is
   compared against that endpoint's configured `max_requests_per_minute`; a
   mismatch is reported as a finding. The criterion names where the observation
   is read, because a backend attribute does not cross a process boundary.
7. A council config declaring `kind: anthropic` loads with no `base_url` and no
   `reuse_namespace`, and the resolved reuse namespace is **non-empty and
   derived from the normalised `base_url` and `deployment_revision`**. Aliases
   of the same deployment **may share** it — this PRD does **not** require a
   distinct namespace per endpoint alias, and distinct canonical models stay
   separate in the existing reuse key regardless. (The draft demanded
   per-alias uniqueness; confront showed that is unsatisfiable without
   inventing alias separation that breaks deliberate reuse semantics, having
   measured both Haiku and Opus aliases resolving to
   `https://api.anthropic.com#`.)
8. A config placing two models from **different** Anthropic rate-limit buckets
   on one `account` is refused at load with a message naming both buckets; and
   an `anthropic` endpoint whose `account` is **not** its resolved bucket name
   is refused, so two aliases on one bucket cannot become two independent
   pacers under two arbitrary account strings.
8b. A seated canonical model with **no entry** in the rate-limit bucket table
   is refused at load, naming the canonical identity and the configuration
   override that would supply it.
9. A roster seating ARGO's `claudesonnet5` and an Anthropic `claude-sonnet-5`
   as two proposers raises `RosterError`.
10. `resolve_credentials` for an `anthropic` endpoint with its
    `api_key_env` variable unset raises `RunRefused`; with it set, it reports
    `requires: api_key` naming the **variable** and never its value.
11. A preflight against a configured `anthropic` endpoint reports `ready`
    without making a billable call, and reports `refused` with a reason when
    the credential is absent.
12. No seat sends a sampling parameter unless its config sets one explicitly.
    Verified through `effective_params` **and** through the request the backend
    builds, across three separately asserted cases: temperature **omitted** from
    the model's params (nothing sent); temperature set **explicitly to 0** (zero
    sent); temperature set to a **non-zero** value (that value sent). The three
    cases exist because `_seat` pops temperature out of params with a default of
    `0`, so "omitted" and "explicitly 0" are indistinguishable unless `_seat`
    carries the difference — which is what the Q4 change makes it do.
12b. An `anthropic` request whose extras would overwrite `model`, `messages` or
    `max_tokens` is refused rather than merged; `stream` and `tools` are
    refused in this lane; and a `cache_control` nested anywhere inside the
    merged request `params` and endpoint `extra_body` is caught, not just a
    top-level key.
12c. The shared `strip_think` helper is **unchanged**, and an ARGO or
    OpenAI-compatible call's cleaning behaviour is byte-identical to the base
    commit. The `<thinking>` handling is local to the Anthropic backend.
13. A response with `stop_reason: "max_tokens"` produces `ok=False` with
    `truncated=True`, not an "no JSON object" failure.
14. A 400 whose message begins `You have reached your specified API usage
    limits`, and a 429 carrying `enforced_spend_limit_reached`, each produce
    `QuotaExhausted` with `halts_endpoint=True`.
15. A request carrying `cache_control` is refused before anything is sent.
16. `import kbutillib` succeeds in an environment where the `anthropic`
    package is **not** installed, and constructing an `anthropic` endpoint in
    that environment fails with an error naming `kbutillib[anthropic]` **and
    the Python ≥ 3.10 requirement**.
17. The `anthropic` extra carries a `python_version >= '3.10'` marker in both
    `[project.optional-dependencies]` and the `all` extra, and
    `pip install -e ".[all]"` still succeeds on **Python 3.9**.
18. The SDK client is constructed with retries disabled, verified by
    inspecting the constructed client's retry setting — not by reading the
    source that sets it. One upstream request per `complete()` call.
19. No test that passed on the base commit fails on the merged result.
