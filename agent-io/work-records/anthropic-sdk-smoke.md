# Work Record — anthropic-sdk-smoke

## task_id
anthropic-sdk-smoke (phase 1 of 7, PRD `anthropic` api type)

## base commit
`0bb8a88` (`fix(clearinghouse): name the unresolved keys in the coverage-floor refusal (refs #96)`)

## summary

Adds the official Anthropic Python SDK as an OPTIONAL extra and records the
SDK's real shape, obtained by inspecting the INSTALLED artifact (not the
project's main branch, not documentation, not memory). Later phases treat this
file as the authority on SDK names. No backend code is written here; `llm/apis/`,
`council/` and `default_config.yaml` are untouched by this phase.

## LIVE CALL: SKIPPED

**No `ANTHROPIC_API_KEY` was present in this environment (checked
2026-11-01), so the one real call (model `claude-haiku-4-5`, 64 output tokens)
was NOT made.** Nothing below is a live observation of the Anthropic API.
No fixture was substituted for it and this step is NOT marked done: the live
assertion is a PRD acceptance criterion and belongs to an operator run
(`scripts/council/anthropic_acceptance_rehearsal.py`, phase 7).

## pin chosen: `anthropic >=2.3,<3 ; python_version >= '3.10'`

> **2026-11-03: `2.4.0` now resolves under this pin, and was spot-checked.**
> The record below describes 2.3.0, the version it was written against. It is
> not the only version the pin admits. Against an installed 2.4.0 I checked
> that these still hold: `AsyncAnthropic` and `max_retries` (default 2);
> `_should_retry` still retries 429 without reading the error body; the auth
> header is still `Authorization: Bearer`; `messages.create` still takes
> `model`/`max_tokens`/`system`/`messages` and still has no named
> `temperature`; the four `Usage` token fields and the `Message`
> content/model/stop_reason/stop_details/usage fields are unchanged; and the
> exception classes and `DefaultAsyncHttpxClient` are all present. The
> backend, config, engine-budget, quote, credential and rehearsal-script tests
> pass against 2.4.0 (246 passed, Python 3.11). Re-run this check whenever a
> newer 2.x resolves.

Inspection of what is resolvable (`pip index versions anthropic`, 2026-11-01):
current release **2.3.0**; the 2.x line is 2.0.0–2.3.0, the 1.x line ends at
1.17.0.

Reason for **2.x**:
- 2.3.0 is the artifact actually installed and inspected below; every name in
  this record is from it. Pinning 1.x would be a deliberate choice to ship on a
  superseded major with no compensating reason found.
- Both 1.17.0 and 2.3.0 already depend on `httpx2<3,>=2.0.0` and require
  Python >= 3.10 (read from each wheel's METADATA), so 1.x buys neither the old
  `httpx` stack nor Python 3.9 support.
- The floor is 2.3 (not 2.0) because 2.3.0 is the only 2.x release inspected;
  a floor below what was looked at would be an untested claim.

The `python_version >= '3.10'` marker is on the entry in both the new
`anthropic` extra and the `all` extra (precedent: `mcp`).

### dependency resolution check

`uv pip compile --python-version <v> pyproject.toml --extra all --extra anthropic`:

| target | exit | anthropic resolved |
|---|---|---|
| 3.9  | 0 | **omitted** (resolution succeeds without it) |
| 3.10 | 0 | anthropic==2.3.0 (httpx2==2.14.0) |
| 3.11 | 0 | anthropic==2.3.0 (httpx2==2.16.0) |

Note for the record: `.github/workflows/ci.yml` on the base commit installs
`pip install -e ".[dev]"`, not the `all` extra (the build brief said `all`).
The marker is on the `all` entry regardless. Because CI installs `dev`, the
same MARKED entry was also added to the `dev` extra AND to
`[dependency-groups].dev` (the file's comment says those two lists are edited
together). Without it every SDK-dependent test would silently skip in CI and
the 3.9 leg would not exercise the marker at all. With it: 3.10/3.11 run the
backend tests against the real SDK; 3.9 must resolve `.[dev]` WITHOUT the SDK
(`uv pip compile --python-version 3.9 --extra dev` exits 0, no anthropic) and
skip them.

## Observed SDK shape (anthropic 2.3.0, Python 3.11)

Legend: **[inspected]** = read off the installed package via
`inspect.signature`, `model_fields`, `dir()` or its source;
**[mock-observed]** = observed by driving the installed SDK through an
`httpx2.MockTransport` (real serialisation and parsing, but an envelope I
wrote); **[unverified]** = not established.

### client
- Async client: `from anthropic import AsyncAnthropic` **[inspected]**.
- Constructor keyword-only params **[inspected]**: `api_key`, `auth_token`,
  `credentials`, `config`, `profile`, `webhook_key`, `base_url`, `timeout`,
  `max_retries: int = 2`, `default_headers`, `default_query`, `open_telemetry`,
  `http_client: httpx2.AsyncClient | None`, `middleware`, ... There is **no
  `proxy` parameter on the client**: a proxy goes on the `http_client`
  (`anthropic.DefaultAsyncHttpxClient(proxy=...)`, a subclass of
  `httpx2.AsyncClient`, whose `__init__` accepts `proxy`) **[inspected]**.
- Credential resolution when NO explicit credential is passed **[inspected,
  docstring + source of `AsyncAnthropic.__init__`]**: `ANTHROPIC_API_KEY`, then
  `ANTHROPIC_AUTH_TOKEN`, then `ANTHROPIC_PROFILE`, workload-identity env vars,
  then an on-disk active profile. Passing `api_key=` explicitly disables that
  chain. `base_url` falls back to `ANTHROPIC_BASE_URL`, then a profile, then
  `https://api.anthropic.com` -- so the backend must pass `base_url` explicitly
  too. `ANTHROPIC_CUSTOM_HEADERS` is read from the environment unconditionally.
- An `httpx` (not `httpx2`) object passed as transport/timeout raises
  `TypeError` (`_reject_httpx_object`) **[inspected]**: test transports must be
  `httpx2.MockTransport`.
- Auth header **[inspected `AsyncAnthropic._api_key_auth` + mock-observed]**:
  2.3.0 sends an explicit `api_key` as `Authorization: Bearer <key>` (NOT
  `x-api-key`), alongside `anthropic-version: 2026-10-21` and
  `x-stainless-retry-count`. Whether the live API accepts the bearer form for a
  console API key is **[unverified]** here (no key) -- the operator rehearsal
  answers it.
- Test transport seat: `AsyncAnthropic(..., http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))` **[mock-observed]**.

### message create
- Method path: `client.messages.create(...)` (`anthropic.resources.messages.messages.AsyncMessages.create`) **[inspected]**.
- Raw/headers path: `await client.messages.with_raw_response.create(...)` returns
  `anthropic._response.AsyncAPIResponse`; `.headers` is the HTTP response
  headers; `.parse()` is a **coroutine** (`await raw.parse()`) returning the
  `Message` **[mock-observed]**.
- Parameter names **[inspected]**: model -> `model`; output-token ceiling ->
  `max_tokens` (required, no default); system prompt -> `system`; message list
  -> `messages`. Other named params: `cache_control`, `container`,
  `diagnostics`, `inference_geo`, `metadata`, `output_config`, `service_tier`,
  `speed`, `stop_sequences`, `stream`, `thinking`, `tool_choice`, `tools`,
  `user_profile_id`, `workspace_id`, plus `extra_headers`, `extra_query`,
  `extra_body`, `timeout`.
- **`temperature`, `top_p` and `top_k` are NOT named parameters of
  `messages.create` in 2.3.0** **[inspected]**. They can only be sent through
  `extra_body`; doing so puts them in the JSON body verbatim
  (`extra_body={"temperature": 0.3}` -> body contains `"temperature": 0.3`)
  **[mock-observed]**.

### response
- Type `anthropic.types.Message`; fields **[inspected]**: `id`, `container`,
  `content`, `diagnostics`, `model`, `role`, `stop_details`, `stop_reason`,
  `stop_sequence`, `type`, `usage`.
- Content block list: `message.content`; discriminator: `block.type`
  (`"text"`, `"thinking"`, ...); text: `TextBlock.text` (fields `citations`,
  `text`, `type`); thinking block fields `signature`, `thinking`, `type`
  **[inspected + mock-observed]**.
- Resolved model: `message.model`. Stop reason: `message.stop_reason`, typed
  `Optional[Literal['end_turn','max_tokens','stop_sequence','tool_use','pause_turn','refusal','model_context_window_exceeded']]` **[inspected]**.
- Stop details: `message.stop_details`; on a refusal it parsed as
  `RefusalStopDetails(category=..., explanation=..., type='refusal')`
  **[mock-observed]**; `None` when the envelope omits it **[mock-observed]**.
- Usage: `message.usage` (`anthropic.types.Usage`), fields **[inspected]**:
  `input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
  `cache_read_input_tokens`, `cache_creation`, `inference_geo`,
  `output_tokens_details`, `server_tool_use`, `service_tier`, `speed`.
  `usage` is declared REQUIRED on `Message`, but with the default
  (non-strict) response validation an envelope with no `usage` key still
  parses and `message.usage` is `None` **[mock-observed]** -- so "no usage
  object" is representable and the backend must handle `None`.

### errors **[inspected `_exceptions.py` + `_make_status_error`; mock-observed]**
- Base: `anthropic.APIStatusError` (subclass of `APIError`) for HTTP statuses;
  attributes `status_code`, `response`, `body` (parsed JSON, a `dict`),
  `message`, `type` (= `body["error"]["type"]`), `request_id`.
- 400 `BadRequestError`, 401 `AuthenticationError`, 403
  `PermissionDeniedError`, 404 `NotFoundError`, 409 `ConflictError`, 413
  `RequestTooLargeError`, 422 `UnprocessableEntityError`, 429
  `RateLimitError`, 529 `OverloadedError`, other >= 500
  `InternalServerError`, any other status plain `APIStatusError`.
- Connection failure: `APIConnectionError`; timeout: `APITimeoutError`
  (subclass of `APIConnectionError` -- **test for timeout first**).
- `error.details.error_code` is **NOT an attribute** on the exception
  (`hasattr(e, "details") is False`); it is reachable only through the dict:
  `e.body["error"]["details"]["error_code"]` **[mock-observed]**.
- `e.message` is SDK-formatted (`"Error code: 400 - {...}"`); the provider's
  own message text is `e.body["error"]["message"]` **[mock-observed]**.

### response headers
`(await client.messages.with_raw_response.create(...)).headers.get("anthropic-ratelimit-requests-limit")` **[mock-observed]**. The plain `create()` does not expose headers.

### retry surface **[inspected source + mock-observed]**
- Constructor parameter: `max_retries`, **default 2**
  (`anthropic.DEFAULT_MAX_RETRIES == 2`); the instance attribute
  `client.max_retries` reflects it.
- `BaseClient._should_retry(response)` in 2.3.0 consults only the
  `x-should-retry` header and the HTTP status: retries 408, 409, **429** and
  every >= 500. **It does NOT parse the body and does NOT inspect
  `error.details.error_code`.** CONFIRMED.
- Mock-observed: a 429 carrying `enforced_spend_limit_reached` produced **3
  upstream requests** with the default client and **1** with `max_retries=0`.
- `client.models.list()` goes through the same client and the same retry
  policy.

## FIXTURE BASIS

Provenance: the envelope SHAPE below was parsed by the installed SDK 2.3.0
(client compatibility: serialisation, `Message` parsing, raw-response header
access are **observed SDK behaviour**). The VALUES, the presence of each field
in a real response, the `anthropic-ratelimit-*` header names, the
`enforced_spend_limit_reached` error code and the
`You have reached your specified API usage limits` message prefix are
**documentation-derived** (the PRD's research) -- the SDK parsing them proves
nothing about what the API actually sends. No envelope here came from the API.

Success (HTTP 200):
```json
{
  "id": "msg_01", "type": "message", "role": "assistant",
  "model": "claude-haiku-4-5-20251001",
  "content": [
    {"type": "thinking", "thinking": "", "signature": "sig"},
    {"type": "text", "text": "{\"answer\": 1}"}
  ],
  "stop_reason": "end_turn", "stop_sequence": null,
  "usage": {"input_tokens": 3, "output_tokens": 5,
            "cache_creation_input_tokens": 7, "cache_read_input_tokens": 11}
}
```
Response headers (documentation-derived names):
`anthropic-ratelimit-requests-limit`, `-requests-remaining`, `-requests-reset`,
`anthropic-ratelimit-input-tokens-{limit,remaining,reset}`,
`anthropic-ratelimit-output-tokens-{limit,remaining,reset}`.

Refusal: `"stop_reason": "refusal", "stop_details": {"type": "refusal", "category": "cyber", "explanation": "..."}`.
Truncation: `"stop_reason": "max_tokens"`.
No usage: omit the `usage` key (parses to `message.usage is None`).

Errors (body shape `{"type":"error","error":{"type":..., "message":..., "details": {...}}}`):
- org spend cap: HTTP 429, `error.type = "rate_limit_error"`, `error.details.error_code = "enforced_spend_limit_reached"`;
- operator spend limit: HTTP 400, `error.type = "invalid_request_error"`, `error.message` beginning `You have reached your specified API usage limits`;
- auth: 401 `authentication_error`; plain rate limit: 429 `rate_limit_error` without that code; overload: 529 `overloaded_error`.

## files_touched
- `pyproject.toml` — new `anthropic` extra; marked entry added to `all`.
- `agent-io/work-records/anthropic-sdk-smoke.md` — this record.

The probe script lived in `/tmp` and is not committed.
