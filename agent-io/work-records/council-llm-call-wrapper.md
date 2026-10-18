# Work Record — council-llm-call-wrapper

## task_id
council-llm-call-wrapper (M2.1)

## branch
`maestro/developer/background-reference-only-agent--council-llm-call-wrapper`

## commit_shas
- `048e54a` — feat(llm): one public wrapper for every model call, with plug-in api types
- `3d53f98` — feat(council): every model call goes through the llm wrapper
- `fe54960` — feat(council): config validates kinds against the registry, plus round 3
- (this record)

## summary

Every model call in KBUtilLib now goes through ONE public wrapper,
`kbutillib.domains.ai.llm`, which takes the API type as an argument and
accepts plug-in API types from other distributions. Chris, review round 2:
*"send ALL LLM calls through a common wrapper function that accepts the API
types as an argument, then we can implement a wide range of API types within
this function."*

New package `src/kbutillib/domains/ai/llm/`:

| File | What it holds |
|---|---|
| `types.py` | `LLMRequest`, `LLMResult`, `LLMError`, `LLMException`, `CallError` + the seven provider failures, `as_llm_error`, `as_llm_request`, `generation_params` |
| `protocol.py` | the `ApiType` protocol, `REQUIRED_MEMBERS`, `missing_members` |
| `__init__.py` | `acomplete` / `complete` / `preflight` / `preflight_api` / `models` / `models_api` / `register_api_type` / `api_types` / `get_api` / `endpoint_key` / `aclose_all`, the registry, the entry-point discovery, the four built-in lazy factories |
| `apis/` | the four shipped API types (see the move table below) |

The council stops constructing providers: `CouncilUtils._backend_for` is
deleted, `CouncilWorker` makes every model call through an injected callable
whose default IS `llm.acomplete`, and `council/config.py` validates an
endpoint's `kind` against `llm.api_types()` at load.

## files_touched

New:

- `src/kbutillib/domains/ai/llm/__init__.py` (560 lines)
- `src/kbutillib/domains/ai/llm/types.py` (340 lines)
- `src/kbutillib/domains/ai/llm/protocol.py` (82 lines)
- `src/kbutillib/domains/ai/llm/apis/__init__.py` (20 lines, docs only)
- `tests/domains/ai/llm/__init__.py`, `tests/domains/ai/llm/test_llm_wrapper.py` (1430 lines, 76 tests)
- `agent-io/work-records/council-llm-call-wrapper.md` (this file)

Moved (git-tracked renames, behaviour unchanged except the request type):

- `council/backends/http.py` → `llm/apis/http.py`
- `council/backends/http_common.py` → `llm/apis/http_common.py`
- `council/backends/cli.py` → `llm/apis/cli.py`
- `council/backends/cli_common.py` → `llm/apis/cli_common.py`

Rewritten as thin re-export shims (same paths, same `__all__`):

- `council/backends/http.py`, `http_common.py`, `cli.py`, `cli_common.py`
- `council/backends/__init__.py` (docstring only)

Edited:

- `council/types.py` — the moved records become aliases; `route_fingerprint`
  gains `options`
- `council/protocols.py` — `Backend`'s docstring records that it is the M1
  name for `ApiType` (no signature change)
- `council/backends/testing.py` — `FakeBackend` / `ReplayBackend` implement
  `ApiType`
- `council/config.py` — dynamic `kind`, `options:`, the `reuse_namespace`
  rule, the credential refusal
- `council/engine/worker.py` — the single model-call site, the injected
  callable, api-type resolution
- `council_utils.py` — `_backend_for` deleted; `build_backends`,
  `models_async`, `doctor` go through the wrapper
- `tests/domains/ai/council/test_cli_backends.py` — one test replaced (see
  **Superseded test**)

## moved classes: old and new import path

Required by success criterion 3. Every OLD path still works, through the
re-export shims; the NEW path is where the code now lives. No call site in
the repository uses either any more — they go through `llm.get_api`.

| Class / symbol | Old import path (still resolves) | New import path |
|---|---|---|
| `ArgoBackend` | `kbutillib.domains.ai.council.backends.http` | `kbutillib.domains.ai.llm.apis.http` |
| `OpenAICompatibleBackend` | `kbutillib.domains.ai.council.backends.http` | `kbutillib.domains.ai.llm.apis.http` |
| `HttpBackend` | `kbutillib.domains.ai.council.backends.http_common` | `kbutillib.domains.ai.llm.apis.http_common` |
| `ChatReply` | `kbutillib.domains.ai.council.backends.http_common` | `kbutillib.domains.ai.llm.apis.http_common` |
| `ClaudeCliBackend` | `kbutillib.domains.ai.council.backends.cli` | `kbutillib.domains.ai.llm.apis.cli` |
| `CodexCliBackend` | `kbutillib.domains.ai.council.backends.cli` | `kbutillib.domains.ai.llm.apis.cli` |
| `CliBackend` | `kbutillib.domains.ai.council.backends.cli_common` | `kbutillib.domains.ai.llm.apis.cli_common` |

The module-level helpers moved with their modules and are re-exported from the
old path unchanged: `argo_model_kwargs`, `argo_refusal_error`,
`ARGO_BASE_URL`, `ARGO_USER_ENV`, `ARGO_REFUSAL_SIGNATURES`,
`ARGO_NO_TEMPERATURE_MODELS`, `ARGO_FIXED_TEMPERATURE_MODELS`,
`ARGO_GEMINI_TOKEN_FLOOR`, `ARGO_GEMINI_TOKEN_FLOOR_MODELS` (http);
`strip_think`, `strip_fences`, `extract_json_object`, `read_chat_envelope`,
`client_kwargs`, `normalise_base_url`, `redact`, `BODY_EXCERPT_CHARS`,
`DEFAULT_TIMEOUT_S` (http_common); `CODEX_SCHEMA_NAME`, `CODEX_OUTPUT_NAME`
(cli); `billing_preflight`, `run_cli`, `classify_failure`,
`dropped_billing_vars`, `_child_env`, `strip_fences`, `AUTH_PATTERNS`,
`QUOTA_PATTERNS`, `CHILD_ENV_ALLOWLIST`, `CLAUDE_EXTRA_ENV`,
`CODEX_EXTRA_ENV`, `DENIED_BILLING_VARS`, `KILL_GRACE_S`,
`OUTPUT_EXCERPT_CHARS` (cli_common).

`_child_env` is re-exported despite the leading underscore because
`test_cli_backends.py` imports it by name to prove the child environment is
built from an allowlist — the one test standing between a CLI seat and
metered API billing.

`FakeBackend` and `ReplayBackend` did NOT move. They stay in
`council/backends/testing.py` (test scaffolding, not a packaged way of
reaching a provider) and are deliberately not registered as api types, so no
shipped configuration can name an endpoint that answers from a script.

Records moved from `council/types.py` to `llm/types.py`, with aliases left
behind that are **the same objects**, not shims:

| Alias in `council.types` | Object in `llm.types` |
|---|---|
| `CompletionResult` | `LLMResult` |
| `CouncilError` | `LLMError` |
| `CouncilException` | `LLMException` |
| `as_council_error` | `as_llm_error` |
| `CallError`, `TransportError`, `CallTimeout`, `QuotaExhausted`, `AuthError`, `BillingGuardRefused`, `ModelUnavailable`, `InvalidOutput` | same names |

`BudgetExhausted` and `BudgetUsageUnknown` stayed in the council: a token
budget is the council's own accounting and no api type knows about one.
`ConfigError` and `RosterError` stayed too, and still subclass
`CouncilException` — which is `LLMException`, so
`isinstance(TransportError("x"), CouncilException)` is still `True` and every
`except CouncilException` in the store, the query registry and the service
client behaves exactly as before.

### every pre-existing import path resolves

Measured, not asserted. A script imported all five modules under
`council/backends/` and `council/types.py`, checked every name in their M1
`__all__`, and resolved the eight lazy names through
`council/__init__.py`'s loader:

```
checked 66 names; unresolved: none
CompletionResult is LLMResult: True
```

## verification

All runs in this task's own venv (`.task-venv`, nothing shared or
service-owned), in the foreground, from the worktree root.

| Command | Exit | Result |
|---|---|---|
| `python -m pytest tests/domains/ai/llm/ -q -p no:cacheprovider` | 0 | **76 passed** |
| `python -m pytest tests/domains/ai/ -q -p no:cacheprovider` | 0 | 828 passed, 243 skipped |
| `python -m pytest tests/domains/ai/council/test_http_backends.py tests/domains/ai/council/test_cli_backends.py -q` | 0 | 194 passed |
| `python -m pytest tests/domains/ai/council/test_engine.py -q` | 0 | 97 passed, 53 skipped |
| `python -m ruff check --output-format=concise src/kbutillib/domains/ai/llm src/kbutillib/domains/ai/council src/kbutillib/domains/ai/council_utils.py tests/domains/ai` | 0 | All checks passed |
| full suite on the **branch** | 1 | **26 failed**, 5062 passed, 699 skipped |
| full suite at the **base** `f30ac6e` | 1 | **26 failed**, 4986 passed, 699 skipped |

Full-suite command, identical on both sides:

```
python -m pytest -q -p no:cacheprovider --tb=no -rf \
  --ignore=tests/biochem/test_escher_utils.py \
  --ignore=tests/modeling/test_comprehensive_gapfill_wrapper.py \
  --ignore=tests/modeling/test_ms_reconstruction_utils.py \
  --ignore=tests/notebook \
  --ignore=tests/external/test_prott5_utils.py
```

**Regression comparison was BY NAME against a base run in this task's own
venv.** The base side ran in a detached `git worktree` at `f30ac6e` under
`/tmp` with `PYTHONPATH=<that worktree>/src`, which takes precedence over the
editable install's plain path entry (verified: `kbutillib.__file__` resolved
to the base tree and `domains/ai/llm` was absent there). `comm` over the two
sorted failure-name sets is **empty in both directions**: the same 26 nodeids
fail on base and on branch, and the +76 passes are exactly this task's new
test module.

The 26 pre-existing failures are 17 in `tests/cli/test_kind.py`, 6 in
`tests/cli/test_clearinghouse*.py`, 2 in
`tests/cli/test_clearinghouse_operator.py` and 1 in
`tests/core/test_composition_smoke.py` — the same set the previous two
council milestones recorded (they counted 26 as well).

`build-baselines/kbutillib.json` (captured 2026-09-13 at `0f84803`) was read
and deliberately NOT used to adjudicate: it was captured in a different
minimal venv whose 99 `known_failing` names are all missing-optional-dep
failures that do not occur here, and four of its `--ignore` targets collect
fine in this venv while `tests/external/test_prott5_utils.py` does not. I did
not rewrite it, for the reason the two previous milestones gave: one exists,
and the instruction to write one applies when none does.

Task venv provisioning (as the prompt specified):
`pip install -e '.[council]'`, `pip install -e '.[dev,ai]'`,
`pip install pytest pytest-asyncio pytest-cov ruff`. `pytest-asyncio` was
installed but is not used — every suite here drives coroutines through a local
`run()` helper that owns its loop (see **design decisions**).

No test makes a network call beyond a refused connection to a closed
`127.0.0.1` port, spends a token, needs a CLI login or needs a GPU.
`src/kbutillib/domains/ai/argo_utils.py` and `ai_curation_utils.py` are
byte-identical to the base commit (`git diff --stat f30ac6e -- <both>` is
empty).

## success_criteria_check

**Criterion 1 — the package exposes the named surface with `LLMRequest` and
`LLMResult`, registers the four kinds, discovers the entry-point group
lazily; `council.types.CompletionResult` is an alias of `LLMResult`;
`CouncilUtils._backend_for` no longer exists and the worker makes every model
call through the wrapper; `test_llm_wrapper.py` covers the listed cases and
passes; no test that passed on base fails on the branch.** — PASS.

| Required | Where | Test |
|---|---|---|
| `acomplete`, `complete`, `preflight`, `models`, `register_api_type`, `api_types`, `get_api` | `llm/__init__.py` `__all__` | `TestCalling`, `TestProbes`, `TestRegistration`, `TestClientCaching` |
| `LLMRequest` / `LLMResult` | `llm/types.py` | used throughout; `test_a_legacy_completion_request_still_works` pins the adapter field by field |
| registers argo / openai / claude_cli / codex_cli | `_register_builtin` at import | `test_each_builtin_resolves_by_name_to_a_conforming_api_type` (4 params), `test_the_builtin_set_is_exactly_the_four_real_kinds` |
| lazy entry-point discovery | `_load_entry_points` | `test_an_entry_point_is_discovered_from_a_dist_info_on_sys_path`, `test_discovery_is_lazy_and_happens_once` |
| `CompletionResult is LLMResult` | `council/types.py` | quoted above; also `test_a_legacy_completion_request_still_works` |
| `_backend_for` gone | `council_utils.py` | `test_council_utils_no_longer_has_a_backend_dispatch` |
| worker calls through the wrapper | `worker._invoke` | `test_the_default_callable_IS_llm_acomplete`, `test_a_job_runs_to_a_verdict_through_llm_acomplete`, `test_patching_the_injected_callable_counts_every_model_call` |
| required `effective_params` | `protocol.REQUIRED_MEMBERS` | `test_a_class_that_is_not_an_api_type_is_refused_at_registration` (3 params), `test_a_malformed_factory_FUNCTION_is_caught_on_first_use_instead`, `test_the_route_fingerprint_uses_the_api_types_effective_params` |
| explicit `reuse_namespace` for base-URL-less kinds | `EndpointConfig._check` | `TestReuseNamespaceForBaseUrllessKinds` (5 tests) |
| `options` passthrough | `config.py`, `types.route_fingerprint` | `TestPluginOptions` (5 tests) |
| per-event-loop client caching | `get_api`, `_loop_id` | `TestClientCaching` (8 tests), notably `test_a_client_is_never_reused_across_event_loops` |
| mtime-triggered billing re-preflight | `apis/cli_common.py` | `TestTheCliBillingGateFollowsItsFiles` (4 tests), plus `test_the_gate_is_cached_until_a_config_file_it_read_changes` in the CLI suite |
| a config naming an unregistered kind fails at load | `_require_registered_kind` | `test_an_unregistered_kind_fails_at_load` |
| duplicate register refused, `replace=True` allowed | `register_api_type` | `test_a_duplicate_registration_is_refused`, `test_replace_is_allowed_and_drops_the_cached_client` |
| `complete()` returns `ok=False` and never raises | `acomplete` | `test_complete_is_the_sync_twin_and_returns_ok_false_for_a_failure`, `test_an_api_type_that_raises_is_converted_not_propagated` |
| M1 backend suites still pass | re-export shims | 194 passed |
| no base-passing test fails on branch | — | `comm` empty both ways, above |

**Criterion 2 (no-injection check) — at least one test runs a council job end
to end through the DEFAULT injected callable (`llm.acomplete` itself) with a
test-registered fake api type and a real temp SQLite store, and a grep-style
test asserts no module under `council/` (other than the shims) imports from
the api-type package directly.** — PASS.

`TestACouncilJobEndToEndThroughTheWrapper` builds a `CouncilWorker` with
`backends=["fake1"]` — endpoint **aliases only**, so there is no instance to
dispatch to and nothing but the wrapper can serve the call — over a real
`SqliteStore` in `tmp_path`, with `"fake"` registered by the test. It asserts
`worker._acomplete is llm.acomplete` by identity, then submits and works a
job to a verdict (`selected_ids == ["A:1"]`, `decided_by == "unanimous"`),
and checks that the four requests the api type actually received were
`LLMRequest`s carrying the rendered prompts, the menu, the token ceiling and
the trace ids.

`test_patching_the_injected_callable_counts_every_model_call` runs a 3-item
job through a substitute that **delegates to the real `llm.acomplete`** (a
stub that answered by itself would prove only that the stub was called) and
asserts exactly 12 counted calls and 12 calls arriving at the api type — if
any council module reached an api type directly, its calls would be missing
from that count.

`TestNoCouncilModuleReachesAProviderDirectly` walks every `*.py` under
`council/`, skipping only the four shims, and asserts that no `import`
statement names `llm.apis` and that no module imports or constructs
`ArgoBackend`, `OpenAICompatibleBackend`, `ClaudeCliBackend`,
`CodexCliBackend`, `HttpBackend` or `CliBackend`.

**Criterion 3 — the work record lists each moved class with old and new
import path, and confirms every pre-existing import path under
`council/backends` resolves.** — PASS. See **moved classes** above; the
66-name check is quoted.

## reconciliation (prompt vs tree)

The prompt is a claim about the code; the code is the fact. Divergences found,
and what I did about each.

1. **`llm/api_types/` cannot coexist with an `api_types()` function on the
   same package — the directory is `llm/apis/`.** The prompt names the
   directory `llm/api_types/` and criterion 1 requires the package to expose
   `api_types`. In Python those collide: importing a submodule BINDS its name
   on the parent package, so `llm.api_types` becomes the module object the
   moment the first provider is constructed, and every later
   `llm.api_types()` raises `TypeError: 'module' object is not callable`. I
   hit exactly that (10 test failures) before renaming. The function wins
   because it is in the gated list and because `council/config.py` and the
   CLI both call it; the directory is `llm/apis/`. Criterion 2's grep test
   checks the real directory name, so its intent is unaffected.

2. **`get_api` keys on an event-loop TOKEN, not `id(loop)`.** The prompt says
   "id of the running event loop". CPython collects a closed loop and hands
   its address straight to the next one, so two successive synchronous calls
   really do see the same `id` — measured: `test_a_client_is_never_reused_
   across_event_loops` failed with `first is second` under `id(loop)`. The
   cache now uses a weak-keyed counter, with a `weakref.finalize` that drops
   a dead loop's entries. Same property the prompt asked for, actually
   delivered.

3. **`complete()` owns a loop instead of calling `asyncio.run`.** The prompt
   says "asyncio.run when no loop is running". `asyncio.run` sets the thread's
   current event loop to `None` on the way out, and this repository has a
   recorded regression from exactly that — see the comments on
   `worker._run_coroutine`, `council_utils._run_sync` and
   `engine_support.run`, all three of which avoid it because
   `tests/interfaces/test_mcp_server.py` breaks from a distance. `complete()`
   creates, uses and closes its own loop: the same "fresh loop per sync call"
   semantics (which is what round 3's client-caching rule depends on) without
   the process-global side effect. It still raises a clear `RuntimeError`
   inside a running loop.

4. **`register_api_type` refuses a malformed CLASS at registration and a
   malformed FUNCTION at first use.** The prompt says registration refuses a
   factory "whose product lacks either" required member — but a function
   factory's product does not exist until it is called, and constructing a
   throwaway probe at registration would mean every registration opened a
   client nobody asked for (for the four built-ins, importing `httpx` at
   import time, which the lazy factories exist to prevent). A class factory
   is inspected without constructing anything, which covers the realistic
   `register_api_type("x", SomeClass)` shape; a function is checked in
   `get_api`, at the first moment a product exists. Both are tested.

5. **The `reuse_namespace` rule is scoped to PLUG-IN kinds.** The prompt says
   "an endpoint of a kind whose endpoints have no base_url must declare
   reuse_namespace". Read literally that includes `claude_cli` and
   `codex_cli`, which would have failed four existing config tests and the
   packaged `default_config.yaml`. It is also not what the rule is for:
   `default_reuse_namespace` returns a real identity for a CLI
   (`"<kind>:<served model>"`, pinned by an existing test) and `"#"` — the
   same string for everything — for an unknown kind. The rule applies to any
   kind that is neither a built-in HTTP kind (which must have a `base_url`,
   unchanged) nor a built-in CLI kind. `test_the_two_builtin_cli_kinds_are_
   exempt` states and pins the scope.

6. **The credential rule is a heuristic on the VALUE, not a ban on the key.**
   The prompt says "a config value that *looks like* a credential under a
   non-`_env` key of a known secret name ... is a ConfigError". A ban on the
   key would have failed two existing tests —
   `test_snapshot_drops_credentials_but_keeps_env_var_names` loads
   `api_key: sekrit`, and the Filipe legacy fixture carries
   `api_key: REDACTED` on nine endpoints — and would also refuse
   `api_key: EMPTY`, vLLM's own documented placeholder. `looks_like_credential`
   therefore matches a vendor prefix (`sk-`, `ghp_`, `AIza`, `Bearer `, a JWT
   header, ...) or a 32+ character opaque token. Six real shapes are refused
   and five placeholders accepted, by test.

7. **`options` enters the route fingerprint only when non-empty.** The prompt
   says options "enter the route fingerprint". Adding the key unconditionally
   changes the fingerprint of EVERY endpoint, and a route fingerprint is
   stored on each queued call row and compared when the call runs — so an
   upgrade would make every queued call report `configuration_mismatch` and
   park itself. Included when present, which is the behaviour the rule wants;
   `test_an_empty_options_block_does_not_change_an_existing_fingerprint`
   pins the other half.

8. **`CouncilWorker` keeps accepting api type INSTANCES in `backends`, and
   hands them the legacy `CompletionRequest`.** The prompt has the worker call
   the wrapper through an injected callable. It also requires (round 3) that
   "old CompletionRequest construction keeps working ... council code and
   tests build it with seat, role, deadline_s", and the tree makes that
   concrete: `test_engine.py`, `test_council_utils.py` and
   `tests/cli/test_council_cli.py` all hand `CouncilWorker` a `FakeBackend`
   whose script keys on `req.role` and `req.seat.seat_name`, which an
   `LLMRequest` deliberately does not carry. So `backends` accepts aliases
   (the M2 form — the default callable is then `llm.acomplete` itself),
   `{alias: None}`, or `{alias: instance}`; an endpoint with an injected
   instance is handed the `CompletionRequest` and everything else the
   `LLMRequest`. One call site either way, and `llm.types.as_llm_request`
   makes the two records interchangeable to anything that only reads them.

9. **`doctor` probes the instances `build_backends` handed it, through
   `llm.preflight_api` / `llm.models_api`.** The prompt says doctor uses
   `llm.preflight` / `llm.models`. Those resolve an api type from a name,
   which would bypass the instances doctor already holds — and four existing
   tests monkeypatch `build_backends` and then assert on
   `backend.probes.count("preflight") == 1`, i.e. that each endpoint is
   probed exactly once. The instance forms are the same functions with the
   resolution step already done (`preflight(name, endpoint)` is literally
   `preflight_api(get_api(name, endpoint))`), so doctor goes through the
   wrapper and still probes once per endpoint.

10. **`council/backends/` is a package directory of shims, not four deleted
    files.** Worth recording because a reviewer checking "the backends moved"
    will find files at the old paths. They are 20–50 line re-export modules;
    `git log --follow` on `llm/apis/http.py` shows the rename.

## superseded test

`tests/domains/ai/council/test_cli_backends.py::TestTheBillingGate::
test_the_gate_is_evaluated_once_per_backend` **no longer exists.** It pinned
the M1 behaviour the round-3 addition explicitly retracts — the prompt says
"The CLI billing preflight is re-run whenever the mtime of any settings/auth
file it inspected changes (M1 caches it once ...), not once per process" —
and its body made the settings hostile after the gate had cleared and
asserted the next call still succeeded.

It is replaced by `test_the_gate_is_cached_until_a_config_file_it_read_
changes`, which asserts BOTH halves of the new contract: an unchanged
filesystem must not re-read anything (counted through a wrapped
`preflight`), and a changed one must re-run and refuse. Four more tests in
`test_llm_wrapper.py::TestTheCliBillingGateFollowsItsFiles` cover it from the
caller's side, including a file APPEARING (recorded as `None`, so
`settings.local.json` arriving invalidates the verdict just as a rewrite
does).

This is the only test nodeid that disappeared. Every other previously-passing
nodeid still passes; see the `comm` comparison above.

## design decisions not dictated by the prompt

- **`_stamp_paths` records `(st_mtime_ns, st_size)`, not just the mtime.** A
  filesystem's mtime granularity is not guaranteed finer than a second, and
  `/mnt/homes` on this fleet is networked. A `claude login` that rewrote
  `settings.json` inside one mtime tick would otherwise keep the stale
  billing verdict — the exact failure the invalidation exists to prevent.
- **An absent file is stamped `None` rather than skipped.** "There is no
  `auth.json`" is a fact the verdict rests on, so the file later appearing
  has to invalidate it. Pinned by
  `test_a_file_appearing_also_invalidates_it`.
- **`CouncilException` is `LLMException`, i.e. the moved base class, not a new
  one.** `CallError` has to be catchable as a `CouncilException` — the store,
  query and service-client error hierarchies all document "a caller can catch
  `CouncilException`" — and the council's own `ConfigError` / `RosterError`
  still derive from it. Aliasing the one class preserves every `isinstance`
  and `except` relation exactly; defining a second base would have silently
  broken `except CouncilException` around a transport failure.
- **`llm` never imports the council, and `llm/types.py` recognises council
  records by DUCK TYPING.** `as_llm_request` reads a `.seat` attribute rather
  than importing `CompletionRequest`, which is what keeps the wrapper
  domain-neutral and import-cycle-free (`council.types` imports `llm.types`,
  never the reverse).
- **`endpoint_key` hashes with a local canonical-JSON helper rather than
  `council.types.canonical_sha256`.** Same reason.
- **The endpoint mapping handed to the wrapper carries `name`.** It is part of
  the client cache key, so without it two aliases whose configs happened to
  be identical — two `fake` endpoints in a test, two ports behind one URL —
  would share ONE client, and the engine's shared-account concurrency tests
  would silently be measuring one endpoint twice.
  `test_two_endpoints_of_one_kind_are_two_clients` pins the nine-port case.
- **`allow_unverified` is part of the endpoint mapping, and so of the cache
  key.** "Spend against an endpoint whose billing mode could not be
  confirmed" is a different client from one that would refuse, and the CLI api
  types read it.
- **An entry point never overrides a built-in.** A name collision loses to the
  shipped kind rather than winning by install order: "which gateway did that
  call go to?" has to stay answerable from this repository. Pinned by
  `test_an_entry_point_never_overrides_a_builtin` with a real `*.dist-info`
  declaring `argo`.
- **A broken entry point is logged and skipped, not fatal.** One bad
  third-party distribution must not stop the council from reaching ARGO.
- **`preflight` on an api type with no `preflight` is `ready`, not
  `unknown`.** `CouncilWorker._preflight` refuses a job on anything but
  `ready`, so reporting "nothing to check" as "could not check" would make
  every HTTP endpoint unsubmittable.
- **`acomplete` converts a raising api type into `ok=False`, but lets an
  unknown `api_type` raise `KeyError`.** A plug-in that has not read the
  never-raise contract must not be able to abort a caller's batch; a config
  typo is a different thing, and reporting it as a model failure would have a
  caller retry a name that can never work.
- **`get_api` resolves a construction race by keeping one instance per key and
  orphaning the loser**, rather than holding the lock across a factory call
  that may open a socket.
- **`CouncilUtils._aclose_backends` closes what it was handed AND clears the
  wrapper's cache.** A test that monkeypatched `build_backends` hands over an
  object the cache has never seen, so both halves are needed; leaving a
  closed client cached would be the worse failure, and a double close is
  harmless and swallowed.
- **`_as_endpoint_table` rejects a non-api-type value in the constructor.**
  Handing the worker the wrong thing is a programming mistake, and discovering
  it mid-batch would cost a claimed call and produce a confusing typed
  failure.
- **`_seat_request` builds a prompt-less `LLMRequest` for
  `effective_params`.** The route fingerprint is computed per seat at submit
  time, before any role's prompts exist, and `effective_params` is a function
  of the generation parameters only. `test_the_route_fingerprint_uses_the_api_
  types_effective_params` asserts both that the api type's opinion wins over
  the seat's request and that it is asked with an `LLMRequest` whose `system`
  is empty.
- **Tests drive coroutines through a local `run()` helper that owns its
  loop.** Copying `engine_support.run` verbatim in reasoning, for the recorded
  reason: `asyncio.run` leaves the thread with no current event loop and
  breaks `tests/interfaces/test_mcp_server.py` from a distance. No
  `pytest-asyncio` dependency is relied on.
- **`test_llm_wrapper.py` has an autouse fixture that restores the registry,
  the client cache and the discovery flag.** The registry is process-global by
  design (a plug-in registers once), so a test that added to it would
  otherwise be visible to every later test in the session — including the
  "unknown name names the registered types" assertion, which reads the whole
  set.
- **The entry-point tests write a real `*.dist-info` directory onto
  `sys.path`.** Monkeypatching `importlib.metadata.entry_points` would have
  proved only that the code calls the function; a real distribution proves a
  third party can ship an api type without this repository knowing anything
  about it.
- **The council end-to-end tests import the engine suite's own
  `engine_support`** rather than re-building a roster and a query type. A
  council assembled slightly differently would not prove that THE council
  runs through the wrapper.

## inferred rather than verified

- **No provider was contacted.** No ARGO call, no CLI prompt, no vLLM port.
  The parameter mapping, the refusal signatures, the CLI argv and the billing
  preflight are unchanged code, moved; their behaviour is pinned by the 194
  tests of the two M1 suites, which still pass through the new location.
- **The entry-point group name `kbutillib.llm_api_types` is taken from the
  prompt.** Nothing declares it in this repository's `pyproject.toml` (the
  group is for OTHER distributions to declare), so it is exercised only
  through the fake `*.dist-info` directories the tests build. If a real
  plug-in is ever shipped, that is the string it must use.
- **`weakref.finalize` on an event loop is assumed to fire before the process
  exits in the common case, and the leak if it does not is bounded** by the
  number of loops a process creates. The tests exercise the eviction path
  only indirectly (three loops, three distinct clients); I did not force a
  collection to assert the finalizer ran.
- **A double `aclose()` on an `httpx.AsyncClient` is assumed harmless.**
  `aclose_all` swallows and logs any exception from a close, so the
  consequence of being wrong is a DEBUG line, not a failure.
- **The 26 pre-existing failures were not diagnosed.** They are in
  `tests/cli/test_kind.py`, `tests/cli/test_clearinghouse*.py` and
  `tests/core/test_composition_smoke.py`, none of which this task touches,
  and they are identical by name on base and branch.

## out of scope (later tasks, untouched)

Removing PostgreSQL, schema v2, the execution lock, query directories, the
daemon, the macOS `/var` path test, and the model registry / GPU inventory /
lifecycle names still mapped (unwritten) in `council/__init__.py`. The
`poplar_registry` kind is still in `types.ENDPOINT_KINDS` and `HTTP_KINDS`
and in `SeatSpec.endpoint_kind`'s Literal; removing it belongs to the
self-hosted-models task, and it is no longer a registered api type, so a
config naming it is now refused at load.
