# Work Record — council-http-backends

## task_id
council-http-backends

## branch
maestro/developer/background-reference-only-agent--council-http-backends

## commit_shas
- 7f07fbe — feat(council): ARGO and OpenAI-compatible HTTP backends
- 5141c0a — docs(work-record): record the council HTTP backend milestone

## summary
Implemented the council's two HTTP backends on top of a shared helper module,
as the fourth milestone of `src/kbutillib/domains/ai/council/`:

- **`backends/http_common.py`** — `HttpBackend`, the whole common
  chat-completions path, plus the pure helpers (`strip_think`,
  `strip_fences`, `extract_json_object`, `read_chat_envelope`,
  `client_kwargs`, `redact`). One `httpx.AsyncClient` per backend instance,
  proxy from the endpoint config, per-call timeout from `seat.timeout_s`,
  `POST {base_url}/chat/completions` with `system` then `user` and no
  `response_format`, and the status/transport mapping onto the council's
  failure taxonomy. `complete()` never raises: an outer `except Exception`
  converts anything unforeseen into a `transport` error, because the
  `Backend` protocol's one hard rule is that one bad seat cannot abort an
  item.
- **`backends/http.py`** — `ArgoBackend` and `OpenAICompatibleBackend`, the
  ARGO parameter mapping (`argo_model_kwargs`), the budget-breaker detector
  (`ARGO_REFUSAL_SIGNATURES` / `argo_refusal_error`).

Nothing else was edited. `council/__init__.py`, `types.py`, `protocols.py`
and `config.py` are untouched (the lazy loader already mapped
`ArgoBackend`/`OpenAICompatibleBackend` to `backends.http`), and
`src/kbutillib/domains/ai/argo_utils.py` and `ai_curation_utils.py` are
byte-identical to the base commit (`git diff --stat HEAD -- <both>` is
empty). No test makes a network call beyond a refused connection to a closed
`127.0.0.1` port, spends a token, needs a CLI login or needs a GPU.

## files_touched
- `src/kbutillib/domains/ai/council/backends/http_common.py` (new, 606 lines:
  `HttpBackend` + helpers)
- `src/kbutillib/domains/ai/council/backends/http.py` (new, 344 lines:
  `ArgoBackend`, `OpenAICompatibleBackend`, `argo_model_kwargs`,
  `argo_refusal_error`, `ARGO_REFUSAL_SIGNATURES`)
- `tests/domains/ai/council/test_http_backends.py` (new — 89 tests)

## verification

| Command | Exit | Result |
|---|---|---|
| `python -m pytest tests/domains/ai/council/test_http_backends.py -q -p no:cacheprovider` | 0 | 89 passed |
| `python -m pytest tests/domains/ai/council/ -q -p no:cacheprovider` | 0 | 529 passed, 243 skipped |
| `python -m ruff check --output-format=concise src/kbutillib/domains/ai/council tests/domains/ai` | 0 | All checks passed |
| full suite at base `840a8bf` (same venv, before any file was written) | 1 | **26 failed**, 4641 passed, 699 skipped |
| full suite on the branch (same venv, same command) | 1 | **26 failed**, 4730 passed, 699 skipped |

Full-suite command (both sides, identical):

```
python -m pytest -q -p no:cacheprovider --tb=no -rf \
  --ignore=tests/biochem/test_escher_utils.py \
  --ignore=tests/modeling/test_comprehensive_gapfill_wrapper.py \
  --ignore=tests/modeling/test_ms_reconstruction_utils.py \
  --ignore=tests/notebook \
  --ignore=tests/external/test_prott5_utils.py
```

**Regression comparison was by NAME against a base run in this task's own
venv, not against the cached baseline.** `comm` over the two sorted failure
name sets is **empty in both directions**: the same 26 nodeids fail on base
and on branch, and the +89 passes are exactly this task's new tests. The 26
pre-existing failures are 17 in `tests/cli/test_kind.py`, 8 in
`tests/cli/test_clearinghouse*.py` and 1 in
`tests/core/test_composition_smoke.py` — the same count the previous council
milestone recorded.

`build-baselines/kbutillib.json` (captured 2026-09-13 at `0f84803`) was read
and **not** used to adjudicate: it was captured in a different minimal venv,
and five of its `--ignore` targets now collect fine here while
`tests/external/test_prott5_utils.py`, `tests/biochem/test_escher_utils.py`,
`tests/modeling/test_comprehensive_gapfill_wrapper.py` and
`tests/modeling/test_ms_reconstruction_utils.py` do not (no `torch`, no
`rdkit`, no `cobra`/`modelseedpy` in a fresh task venv). I did not rewrite
it, for the same reason the previous milestone gave: one exists, and the
instruction to write one applies when none does.

Task venv provisioning (`.task-venv`, nothing shared or service-owned):
`python -m pip install 'httpx[socks]>=0.28' 'SQLAlchemy>=2.0' pytest
pytest-cov ruff tomli-w pandas duckdb`. `pandas` and `duckdb` are declared
repo dependencies and were needed for the suite to *collect* (five modules
error at import without `pandas`); `pytest-asyncio` was NOT needed — this
suite drives coroutines through a local `run()` helper, see below.

## success_criteria_check

**Criterion 1 — `http.py` defines both backends implementing `Backend`
(`complete()` returns `ok=False` with a typed error instead of raising), with
the ARGO parameter mapping, `ARGO_REFUSAL_SIGNATURES` mapping usage notices
to `quota_exhausted` and authentication notices to `auth`, `<think>`
stripping and 503 → `model_unavailable`; the test module uses
`httpx.MockTransport` only, asserts every listed case including that no POST
is ever sent to a `/service/` path, and passes; no test that passed on base
fails on the branch.** — PASS.

Case-by-case, with the test that pins it:

| Required case | Test |
|---|---|
| mapping table, one model per branch | `TestArgoParameterMapping::test_mapping_table` (23 rows), plus `test_gpt55_is_matched_before_the_gpt5_prefix` and `test_the_mapping_is_what_reaches_the_wire` |
| Bearer header | `TestArgoAuthentication::test_bearer_header_carries_the_username`, `test_a_custom_user_env_is_honoured` |
| each refusal signature → correct kind | `TestArgoRefusalSignatures::test_each_signature_maps_to_its_error_kind` (5 params) + `test_every_declared_signature_is_covered_by_the_table_above` (the parametrised list IS the module constant, not a subset) |
| 429 / 500 → transport | `TestFailureMapping::test_429_and_5xx_are_retryable_transport_failures` (429, 500, 502, 504) |
| 401 → auth | `TestFailureMapping::test_401_and_403_halt_the_endpoint_as_auth` |
| `finish_reason == "length"` → truncated | `TestFailureMapping::test_a_truncated_reply_is_not_a_vote` |
| fenced JSON parsed | `TestReplyParsing::test_fenced_json_is_parsed` |
| `<think>` stripped | `TestReplyParsing::test_think_blocks_are_stripped_before_parsing` (the scratchpad contains *draft JSON*, so a failure to strip picks the wrong object, not merely a parse error) |
| 503 → `model_unavailable`, and no POST to any `/service/` path | `TestOpenAICompatible::test_503_is_model_unavailable_and_names_where_to_look`, `test_503_sends_no_request_to_any_service_path` (asserts the full request log is exactly `["POST /v1/chat/completions"]`, then separately that no request and no POST touches `/service/`) |
| a valid answer quoting "ACCESS REVOKED" is NOT a refusal | `TestArgoRefusalSignatures::test_a_valid_answer_quoting_a_signature_is_not_a_refusal` (quotes all three phrases inside a schema-shaped answer) |
| `models()` parses a body / returns `[]` on error | `TestModelListing` (6 tests) |
| proxy kwarg wiring, no SOCKS connection | `TestProxyWiring::test_socks_proxy_reaches_the_client` |

**Criterion 2 (no-injection check) — two tests construct the backends WITHOUT
the test-only transport kwarg.** — PASS.
`TestNoInjection::test_argo_without_a_username_fails_auth_on_the_real_client`
builds `ArgoBackend` directly with a real `httpx.AsyncClient`
(`assert "transport" not in backend.client_kwargs`), `monkeypatch.delenv`s
`ARGO_USER`, patches nothing in the backend, and asserts `ok is False`,
`kind == "auth"`, `halts_endpoint is True` with no raise.
`TestNoInjection::test_openai_against_a_closed_local_port_waits_for_the_model`
binds a `127.0.0.1` socket, reads its port, closes it, points `base_url` at
`http://127.0.0.1:<port>/v1`, and asserts `ok is False` with
`kind == "model_unavailable"` and the port named in the message.
`test_both_backends_satisfy_the_backend_protocol` additionally
`isinstance`-checks both against the `runtime_checkable` `Backend`.

**Criterion 3 — `effective_params()` for both backends contains no key whose
name contains "auth", "key", "token" (other than the three token-ceiling
names) or "proxy".** — PASS.
`TestEffectiveParams::test_no_credential_or_routing_key_appears_at_any_depth`
is parametrised over both backends, builds each with BOTH a
`socks5://127.0.0.1:1080` proxy and an `api_key`, and walks the returned
structure **recursively** (`_offending_keys`, which descends dicts and lists)
rather than checking only the top level — `extra_body` is nested, so a
top-level-only check would not have covered it. It also asserts neither the
key value nor `socks5` appears anywhere in the JSON dump.

## design decisions not dictated by the prompt

- **The refusal screen runs on three "no answer" paths, not one.** The prompt
  says to check signatures only when the *content* does not parse as a JSON
  object. I kept that exactly, and additionally screen when the envelope
  itself is not JSON and when a well-formed envelope carries no
  `choices[0].message.content`. Both are bodies that are not an answer, so
  the property the rule protects ("a quoted phrase inside a legitimate answer
  never trips it") is untouched — and the breaker is reported as the spend
  halt it is rather than as a retryable blip in two more shapes. Pinned by
  `test_a_banner_in_a_non_json_envelope_is_still_detected`.
- **Status wins over content.** A 429 whose body says "Monthly limit
  exceeded" is rate limiting, and stays a retryable `transport` failure; the
  signature table only applies to a reply the gateway called successful.
  Pinned by `test_a_refusal_in_a_429_body_is_still_a_transport_failure`.
- **A non-429 4xx other than 401/403 is `invalid_output`.** The prompt maps
  429, 5xx, connect/read errors and 401/403; it does not say what a 400 is. A
  400 is the *request* being wrong (bad model name, unsupported parameter):
  retrying cannot fix it and halting the endpoint would punish every other
  seat on it, so it fails just that call and surfaces the configuration
  mistake. `test_a_400_is_the_request_being_wrong_not_the_credential`.
- **`effective_params()` includes `extra_body`, nested.** It travels in the
  request body and changes the answer, so leaving it out would let a
  thinking-enabled and a thinking-disabled call share one cache entry. It is
  still generation-only: no header, no credential, no proxy.
  `test_extra_body_is_included_because_it_changes_the_answer`.
- **A model's configured `params` are merged on top of the mapping.**
  `CouncilConfig._seat` puts a model's `params:` block into `seat.extra`, so
  ignoring it would silently discard a configured `top_p`. The ARGO mapping
  remains the base, which is what the criterion's table asserts.
- **Credential redaction at a single chokepoint.** `redact()` scrubs
  `Authorization:`/`Bearer `/`api_key=`-shaped text out of every message and
  log line this module emits, because an `httpx` exception can quote the
  request headers in its own string form.
  `test_an_error_message_never_carries_the_bearer_token`.
- **The ANL username is resolved per call, not cached at construction**, so a
  user who exports `ARGO_USER` after a daemon started is picked up on the
  next sweep instead of after a restart.
- **`client_kwargs` is a public attribute holding the exact kwargs
  `httpx.AsyncClient(**kwargs)` was given.** That is what makes the proxy
  assertion meaningful without a monkeypatch and without dialling the proxy;
  it holds no credential (the `Authorization` header is built per call).
- **`models()` on a supervised port may GET `/service/status` once, after
  `/models` has already failed**, purely so the log says *why* (starting,
  stopped, failed). That is the single `/service/` request anywhere in the
  backend and it is read-only;
  `test_a_supervised_port_may_GET_service_status_but_never_POST_it` asserts
  the full request log is `["GET /v1/models", "GET /service/status"]` and
  that no non-GET request was made at all, and
  `test_an_unsupervised_port_never_touches_service_status` asserts the
  unsupervised port does not even do that.
- **`schema_enforced` is always `False`.** No `response_format` is requested
  (servers differ), so the engine's `_validate_answer` is the only enforcer
  and claiming otherwise on a result would be a lie to the worker.
- **`usage` stays `None` when the server reported no `usage` block**, and is
  a dict with possibly-`None` counts when it did. That is exactly the
  distinction `CouncilWorker._enforce_budget` needs to tell "free" from
  "unmeasurable" (`budget_usage_unknown`), verified against
  `worker.py:1166-1186` and `_token_total` at `worker.py:1496`.
- **Tests drive coroutines through a local `run()` helper that owns its
  loop**, copying `engine_support.run` verbatim in reasoning: `asyncio.run`
  leaves the thread with no current event loop and breaks
  `tests/interfaces/test_mcp_server.py` from a distance (the regression the
  first council milestone found and recorded). No `pytest-asyncio` dependency
  was added.

## reconciliation (prompt vs tree)

The prompt is a claim about the code; the code is the fact. Divergences found
and what I did:

1. **`council/__init__.py` already maps both new names.** The prompt forbids
   editing it; it was written in the first milestone with
   `"ArgoBackend": "backends.http"` and `"OpenAICompatibleBackend":
   "backends.http"` already in `_MODULE_MAP`, so the module landed with no
   edit needed anywhere. Nothing to change — recording it so a reviewer does
   not look for a missing wiring commit.
2. **The `council` extra is irrelevant to this task.** The prompt's notes say
   to install `.[council]` (SQLAlchemy + psycopg) before running tests.
   `SQLAlchemy` is needed only because `tests/domains/ai/council/` also
   contains the *store* contract tests; the HTTP backends themselves need
   only `httpx`, which is the already-declared `ai` extra. `psycopg` was
   never installed and 243 PostgreSQL contract tests skip, as they do on base.
3. **`pandas`/`duckdb` are load-bearing for *collection*, not for this
   change.** The prompt's install list does not mention them; without them
   five test modules fail at import and `pytest` aborts the whole run with
   "Interrupted: 5 errors during collection", so no base/branch comparison is
   possible at all. Both are declared repo dependencies, so I installed them
   in the task venv rather than extending the `--ignore` list.
4. **The prompt's `--ignore` list (via the cached baseline) is stale in both
   directions.** `tests/berdl/test_clearinghouse_derivation.py`,
   `tests/biochem/test_ms_biochem_deltag.py` and
   `tests/kbase/test_kb_annotation_utils_parquet.py` now collect fine and
   were NOT ignored; `tests/external/test_prott5_utils.py` had to be added
   (no `torch`). The command actually used is quoted above and was identical
   on both sides.
5. **`httpx`'s `proxy=` does not land on `client._transport`.** The criterion
   says to assert the client was built with the proxy. In httpx 0.28 a proxy
   becomes a *mount* (`client._mounts`), and `client._transport` stays the
   plain pool — so the obvious assertion would have passed vacuously on a
   broken implementation. The test asserts the recorded constructor kwargs
   AND that a mounted transport's pool is an `AsyncSOCKSProxy`. Related
   consequence, documented in the test: a proxy and a `MockTransport` must
   not be combined, because the mount takes precedence over the explicit
   transport; the proxy test therefore sends no request, which is also what
   "without opening a SOCKS connection" requires.
6. **`extract_json_object('[{"id": "GO:1"}]')` returns the contained
   object.** "The outermost `{...}` object" is literally the brace span, and
   for a single-element list that span is the element. I pinned the real
   behaviour in `test_extract_json_object_needs_a_parseable_brace_span`
   rather than adding a list guard: the engine's schema validation is what
   rejects a wrong shape, and a helper that silently dropped a recoverable
   answer would be the worse failure. A multi-element list still yields
   `None`, because that span is not valid JSON.

## inferred rather than verified

- **The ARGO parameter mapping is transcribed from the prompt, not confirmed
  against the live gateway.** No ARGO call was made (no `ARGO_USER` on this
  machine, and the task forbids spending). If a family's requirement has
  changed since Jose Faria's `model_kwargs`, the table in
  `argo_model_kwargs` is where it changes, and
  `TestArgoParameterMapping::test_mapping_table` is the row to edit with it.
- **The exact wire form of ARGO's budget-breaker reply is inferred.** The
  signatures and the HTTP-200 behaviour come from the prompt's account of the
  2026-10-05 incident; I have not seen a captured body. The detector
  therefore screens prose content, a non-JSON envelope and a
  choices-less envelope, which covers the shapes such a reply can take
  without my having to guess which one it is.
- **ARGO's `/v1/models` support is unverified.** If ARGO does not serve it,
  `models()` logs and returns `[]`, which is the documented "cannot
  enumerate" answer rather than an error.
- **`EMPTY` as the fallback bearer for a vLLM port** is the OpenAI-client
  convention, not something checked against the poplar servers; they are not
  reachable from a test and were not contacted.
- **The poplar control plane's `/service/status` shape is not asserted.**
  The backend only logs its body; nothing parses it, so no assumption about
  its schema is embedded anywhere.
