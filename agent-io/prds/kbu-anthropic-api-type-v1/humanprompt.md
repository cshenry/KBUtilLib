# Make the $200 of Anthropic API credit a usable council wallet

## What this is for

The MAG token expired and took the two GPT chairs with it.
The replacement wallet is $200 of Anthropic API credit,
and today no code path in KBUtilLib can reach it.

The council can reach ARGO, an OpenAI-compatible server,
the Claude CLI and the Codex CLI.
It cannot reach the Anthropic API,
because `/v1/messages` is not the OpenAI `/chat/completions` shape
and every HTTP route in the repository is hardcoded to that shape.

This PRD adds a fifth LLM api type, `anthropic`,
and makes a council run on it exactly quotable in dollars.

## What "exactly quotable" means and why it is the hard part

A council run has to be priced before it is launched,
because the wallet is fixed at $200 and there is no second one.

Pricing works today by bucketing every seat's tokens into a "gateway"
and multiplying by an operator-configured dollar rate for that gateway.
There is one input rate and one output rate per gateway.
Seat Haiku 4.5, Sonnet 5 and Opus 5 on a single `anthropic` endpoint
and all three get priced at one rate —
wrong by 5x across the input span ($1 vs $5 per million tokens).

So the models have to sit on separate endpoint aliases.
The non-obvious part, and the thing this PRD had to settle,
is that the key which splits the pricing bucket
is the *same* key that groups the rate limiter.
Choosing it for pricing used to mean choosing it wrongly for pacing.

It turns out those two needs agree here, and that is lucky rather than designed:
Anthropic enforces its rate limits **per model-class bucket**, not per account,
so one alias per rate-limit bucket is simultaneously
the right pricing split and the right pacing group.
That resolution is the centre of this design.

## What this does NOT buy, stated up front

**This PRD does not, on its own, make the planned run affordable.**

While it was being written, session `a6182005` measured the real corpus: 29,179
items needing a vote, not the 21,333 the brief assumed. At that size the
Anthropic leg of any three-chair roster costs about **$313 against $200 of
credit**. The Batch API's 50% discount brings it to **$156.52**; without batch
there is no feasible roster at all.

So what this delivers is the wallet being *reachable, accounted and exactly
priced* — which is a precondition for spending it safely, and which nothing
currently provides. Actually funding the 29,179-item run needs the Batch API
lane as well, and that is a separate build because it is a different execution
shape in the engine (submit, poll, retrieve) rather than a parameter on the
per-call worker. This PRD is its prerequisite.

The original brief called batch "worth ~$127 and not worth the build yet". That
figure was sized against the wrong corpus and the conclusion is withdrawn.

## What is already paid for, and must not be relearned

Four failures have already been measured on this project
and each one has a counterpart here:

1. **A gateway that reports no usage makes a budget unenforceable.**
   Anthropic calls its token counts `input_tokens` and `output_tokens`.
   The engine sums `prompt_tokens` and `completion_tokens`.
   A backend that passes the names through reports zero spend:
   the ceiling never trips, the halt never fires,
   and the quote reconciles against nothing.
   Closed here by mapping inside the backend,
   and by an acceptance criterion that a REAL call records a non-zero
   `prompt_tokens` — not a unit test against a stub that would pass either way.

2. **A concurrency cap is not a rate limit.**
   The first MAG pilot configured 24-parallel against an account-wide
   120 requests per minute and errored 427 of 500 items
   over 1,925 transport attempts.
   The pacer built in response to that is already in the engine
   and this PRD reuses it rather than adding a second one.

3. **A monthly cap that answers like something else burns days silently.**
   ARGO's spend breaker answered HTTP 200 with a fake assistant message,
   and roughly two days of scheduled wakes exited 0 having done nothing.
   Anthropic has the same hazard in a different shape:
   the wallet-exhausted signal arrives as an HTTP **400**, not a 429,
   and the current mapping neither halts the endpoint nor retries it —
   it fails every remaining item one at a time with a misleading reason.

4. **A list price is a floor, not a cost.**
   This matters less here than anywhere else so far,
   because the Anthropic API bills at list by construction.
   But it is worth saying plainly that this credit is a
   **second wallet at the same price, not a discount**:
   ARGO was measured billing Claude at vendor list within 1.5%
   on the 2026-10-20 bill.
   No task in this plan may justify itself on a saving that does not exist.

## What this does not do

Not the Batch API — see above; it is a separate PRD this one is a prerequisite
for, and the run needs it.
Not prompt caching.
Not a token-per-minute pacer: the two settings this system has bound requests
and parallelism, and the limit that actually binds a council's large prompts is
tokens per minute. What ships is a halt after the fact, not prevention, and the
documentation says so rather than implying otherwise.
Not a single enforced $200 ceiling. Each endpoint carries its own token budget
and nothing sums them, so the thing that really stops the spend is the
provider-side console limit — which is why recognising its error response
correctly is one of the more valuable parts of this build.
Not the corpus, roster or scoping decisions for the run itself.
Not a migration of the council store, which needs none.

## Who this is for

An operator who has an Anthropic API key and $200 of credit,
wants to seat two or three Claude models as council chairs,
and needs to know what the run will cost before starting it
and to be stopped automatically when the money runs out.
