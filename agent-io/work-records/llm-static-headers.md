# Work Record — llm-static-headers

## task_id
llm-static-headers

## branch
maestro/developer/task-llm-static-headers-kbutilli-llm-static-headers

## base_commit
6de52e92 (`feat(kbdl-client): reinstate bakta_db/kofam_profiles/kofam_profile_set on submit_genome_annotation`)

## summary
An `openai`-kind council endpoint can now declare `options.headers`: a mapping
of static, non-secret request headers that `OpenAICompatibleBackend` sends on
**every** request it makes — the chat-completions POST and the `/models` GET.
This is what a Claude Opus live run through the Genesis Model Access Gateway
(MAG) needs: MAG is OpenAI-compatible at
`https://mag.genesis.americansciencecloud.org/v1` and selects the upstream
provider from an `X-AmSC-MAG-Provider` header, which nothing in the stack could
send before.

Three properties hold by construction rather than by convention:

1. **The credential keeps its own channel.** `HttpBackend._headers` now builds
   `Content-Type` → static headers → `Authorization` **last**, so a static
   header can never displace the bearer token however it is spelled. The token
   still comes only from `api_key_env`.
2. **A credential cannot be written there in the first place.** `load_council_config`
   refuses, naming the endpoint, any header whose name is (case-insensitively)
   `Authorization`, `Proxy-Authorization`, `Cookie`, `X-Api-Key` or `Api-Key`, or
   whose name contains `token`, `secret`, `password` or `key` — and additionally
   any header *value* that `looks_like_credential()` recognises, because a
   gateway's own spelling (`X-Forwarded-Auth`) is not obliged to match a name
   rule.
3. **Nothing changed for anyone who did not ask for it.** The headers enter the
   route fingerprint through the **existing** `options` channel in
   `route_fingerprint` — no second mechanism was added — and that channel is
   already conditional on `options` being non-empty, so an endpoint configured
   before this change keeps the fingerprint already stored on its call rows.
   ARGO is untouched: `_static_headers()` is empty on the base class and
   `ArgoBackend` does not override it.

## files_touched

- `src/kbutillib/domains/ai/council/config.py`
  - `validate_static_headers()` (new public function, `config.py:305`), plus
    `_REFUSED_HEADER_NAMES`, `_REFUSED_HEADER_FRAGMENTS` and `_HEADER_NAME_RE`
    (`config.py:282-302`). Exported in `__all__`.
  - `EndpointConfig._check` (`config.py:596`) calls it for every kind.
  - `EndpointConfig.options` docstring notes the one option a built-in kind
    understands.
- `src/kbutillib/domains/ai/llm/apis/http_common.py`
  - `HttpBackend._headers` (`http_common.py:507`) — merge order, documented.
  - `HttpBackend._static_headers` (`http_common.py:554`) — new hook, `{}` by
    default.
- `src/kbutillib/domains/ai/llm/apis/http.py`
  - `OpenAICompatibleBackend._static_headers` (`http.py:301`) reads
    `self.config["options"]["headers"]`. `Mapping` added to the typing import.
- `src/kbutillib/domains/ai/council/default_config.yaml`
  - A **commented** MAG example after the `my-vllm` example (lines 44-67).
- `tests/domains/ai/llm/test_static_headers.py` (new, 40 tests).

## success_criteria_check

**Criterion 1a — the header is sent on chat and /models.** PASS.
`TestTheHeaderTravels::test_it_reaches_the_chat_completions_request` and
`::test_it_reaches_the_models_request` assert
`recorder.requests[-1].headers["X-AmSC-MAG-Provider"] == "anthropic"` on a
`POST /v1/chat/completions` and a `GET /v1/models` respectively.
`::test_every_declared_header_travels_not_just_the_first` covers three at once.

**Criterion 1b — it cannot carry or override credentials.** PASS, three ways.
`TestConfigLoadRefusals::test_each_refused_name_fails_the_load` is parametrised
over all six listed names in both casings (10 cases);
`::test_a_name_containing_a_credential_word_fails_the_load` covers the four
substrings; `::test_the_error_names_the_endpoint` pins the message naming
`endpoint 'mag'`; `::test_a_credential_VALUE_under_a_header_name_is_still_refused`
covers a `sk-…` value under an unlisted name. On the wire,
`TestTheCredentialStaysItsOwnChannel::test_a_hand_built_config_cannot_displace_the_credential`
hands the backend an endpoint mapping that never went through the loader with
`options.headers["Authorization"] = "Bearer attacker-token"` and asserts the
sent `Authorization` is still the `api_key_env` value.

**Criterion 1c — it enters the route fingerprint.** PASS.
`TestRouteFingerprint::test_a_header_change_changes_the_fingerprint` and
`::test_adding_a_header_changes_the_fingerprint`. No new mechanism was added —
the existing one was located before any code was written:

```
$ grep -n "options" src/kbutillib/domains/ai/council/types.py | sed -n '1,6p'
320:    An endpoint's ``options`` (a plug-in api type's own non-secret fields)
334:    # A plug-in api type's own non-secret ``options`` change where and how a
336:    # 3).  Added only when there ARE options, so every endpoint configured
340:    options = cfg.get("options")
341:    if options:
342:        payload["options"] = _jsonable(dict(options))
```

`route_fingerprint` already hashes the whole `options` mapping, so
`options.headers` is carried by it unchanged.
`::test_headers_stay_out_of_effective_params` pins the other side: headers are
*not* in `effective_params`, which documents itself as generation parameters
only, so prompt hashes and answer reuse are unaffected.

**Criterion 1d — endpoints without it are byte-identical.** PASS.
`TestByteIdenticalWithoutOptions::test_the_header_dict_is_literally_the_pre_change_one`
asserts `backend._headers("some-token") == {"Content-Type":
"application/json", "Authorization": "Bearer some-token"}` *and* the key order.
`::test_the_wire_carries_exactly_content_type_and_authorization` asserts on both
real requests that no header beyond httpx's own plus those two is present (the
httpx-own set is computed at runtime, not hard-coded, so an httpx upgrade is not
a false regression). `::test_argo_ignores_options_headers_entirely` proves ARGO's
dict is unchanged even when an `options.headers` block is present.
`::test_an_empty_headers_mapping_changes_nothing` covers `headers: {}`.

**Criterion 1e — commented MAG example, no unverified model ids.** PASS.
`TestPackagedExample::test_the_default_config_declares_no_mag_endpoint` loads the
packaged default through the real loader and asserts `"mag" not in cfg.endpoints`.
`::test_the_example_is_present_and_names_no_model_ids` reads the shipped YAML and
asserts the base URL, `api_key_env: AMSC_TOKEN`, the literal
`X-AmSC-MAG-Provider: <provider>` placeholder and the MyAmSC attribution are all
there. No model alias is declared for MAG anywhere.

**Criterion 2 — no injection.** PASS. Every on-the-wire test registers the real
class and substitutes nothing else:

```python
llm.register_api_type(
    name, lambda endpoint: OpenAICompatibleBackend(endpoint, transport=transport)
)
```

`transport` is an `httpx.MockTransport`, which is the constructor's documented
TEST SEAT argument. Calls go through `llm.acomplete(...)` and `llm.models(...)`;
nothing is monkeypatched. Config tests go through the real
`load_council_config`, including the packaged default with no argument.

## verification

All commands run from the worktree root in `.task-venv` (cpython 3.12.3,
pytest 9.1.1).

| Command | Result | Exit |
|---|---|---|
| `python -m pytest tests/domains/ai/ -q` (**before** any edit) | 955 passed | 0 |
| `python -m pytest tests/domains/ai/llm/test_static_headers.py -q` | 40 passed | 0 |
| `python -m pytest tests/domains/ai/ -q` (**after**) | 995 passed | 0 |
| `python -m ruff check <the four changed/added files>` | All checks passed | 0 |

995 = 955 + the 40 new tests, with nothing lost. The before/after pair is the
real regression evidence for this change: everything it touches is under
`tests/domains/ai/`.

The rest of the suite was run in four slices, all under 600 s, and compared **by
nodeid name** against `build-baselines/kbutillib.json` (captured 2026-09-13 at
`0f84803`):

| Slice | Result |
|---|---|
| `tests/core tests/cli tests/interfaces tests/guard tests/harness` | 26 failed, 1253 passed, 55 skipped |
| `tests/biochem tests/annotators tests/arc_context tests/koros_arc_store tests/kb_app_runner tests/kb_job_utils` (`--ignore=tests/biochem/test_escher_utils.py`) | 871 passed, 83 skipped |
| `tests/berdl tests/kbase tests/external tests/fixtures` | 1231 passed, 210 skipped |
| `tests/modeling tests/models_and_analyses tests/verab tests/beril_worktree` + the three root test files | 8 failed, 661 passed, 110 skipped, 4 errors |

Of the 26 failures in slice 1, **16 are in the cached `known_failing` set by
name** (all of `tests/cli/test_kind.py`). The other **10 names are not in the
cache**, and the cache is five weeks stale, so each was checked directly against
the base commit rather than assumed:

```
$ git worktree add /tmp/llm-static-headers-base HEAD
$ cd /tmp/llm-static-headers-base && python -m pytest \
    tests/cli/test_clearinghouse.py tests/cli/test_clearinghouse_operator.py \
    tests/core/test_composition_smoke.py -q -p no:randomly
9 failed, 77 passed, 16 skipped in 1.17s
```

Those nine (`test_clearinghouse.py` ×6, `test_clearinghouse_operator.py` ×2,
`test_composition_smoke.py::TestKBUtilLibFacade::test_facade_community_lazy_idempotent_and_available_attrs`)
fail identically at `6de52e9` with my change absent. The tenth,
`tests/core/test_composition_smoke.py::TestNotebookSessionKbu::test_notebook_session_kbu_returns_facade`,
**is** in the cache.

Slice 4's 8 failures + 4 errors are `tests/modeling/test_ms_reconstruction_utils.py`
and `tests/modeling/test_comprehensive_gapfill_wrapper.py`, both of which the
cached baseline's own `command` field excludes via `--ignore` as uncollectable
without `cobra`. Checked at base anyway:

```
$ cd /tmp/llm-static-headers-base && python -m pytest \
    tests/modeling/test_ms_reconstruction_utils.py \
    tests/modeling/test_comprehensive_gapfill_wrapper.py -q -p no:randomly
8 failed, 14 passed, 4 errors in 1.20s
```

Identical. The temp worktree was removed afterwards (`git worktree remove … --force`).

Two collection errors are environmental and match the baseline's `--ignore`
rationale: `tests/biochem/test_escher_utils.py` and `tests/notebook/helpers`
(`ModuleNotFoundError: No module named 'cobra'`). Those two paths were excluded
from the slices rather than being fixed, which is out of this task's scope.

**No test that passed on the base commit fails on this branch.**

`build-baselines/kbutillib.json` was read and used, not regenerated: the file
already existed, so per the developer protocol the full suite was not re-run to
establish a baseline.

## reconciled divergences

The task prompt was written against a design branch; three of its claims did not
match the worktree. The tree won in each case and the work carried on.

1. **`verified: false` on the MAG endpoint example.** The prompt asked for it on
   the endpoint. In the tree, `verified` is a **model** key — `_MODEL_KEYS =
   frozenset({"endpoint", "served_name", "canonical", "params", "verified"})`
   (`config.py:140`) — and `_ENDPOINT_KEYS` (`config.py:119-139`) has no such key,
   so `_warn_unknown` would log `ignoring unknown key 'verified' in endpoint
   'mag'` and drop it. Shipping a commented example that warns when uncommented
   is worse than useless, so the example instead tells the user to put
   `verified: false` on the model aliases they declare — which is exactly how the
   packaged `gpt54-cli` / `gpt55-cli` entries already handle unconfirmed served
   names (`default_config.yaml:71-84`) — and says in the comment why the key does
   not belong on the endpoint.

2. **Line numbers.** The prompt cites `http_common.py` constructor ~264, `_headers`
   ~507, chat ~391, `/models` ~333, and `http.py` ArgoBackend ~198,
   OpenAICompatibleBackend ~258. All six were accurate in the tree; no adaptation
   was needed. Noted because the prompt invited the check.

3. **"check how options enter the fingerprint and follow it".** They enter at
   `types.py:336-338`, conditionally on `options` being truthy. Followed exactly:
   nothing in `route_fingerprint` was changed, and
   `TestRouteFingerprint::test_an_endpoint_with_no_options_keeps_the_fingerprint_it_had`
   pins the conditional so a later edit cannot silently invalidate every stored
   call row.

## inferred rather than verified

- **MAG's wire behaviour.** `https://mag.genesis.americansciencecloud.org/v1`,
  the `X-AmSC-MAG-Provider` header name and `AMSC_TOKEN` are taken from the task
  prompt. No request was made to MAG (the task forbids network calls), so the
  gateway has not been observed accepting them. The YAML example is commented out
  and the provider value is the literal placeholder `<provider>` for that reason.
- **Validating `options.headers` for every kind, not only `openai`.** The prompt
  scopes the *sending* to `openai`, which is what the code does. The *refusal*
  is applied to all kinds, because the rule it enforces ("no credential in
  `options`") is about what reaches the store's config snapshot and the route
  fingerprint, and that is true wherever the option was written. A plug-in api
  type that already used `options.headers` with non-string values would now fail
  to load; no such plug-in exists in this repo or in the entry-point group on
  this machine, so the risk is theoretical.
- **Refusing credential-shaped header VALUES** (`looks_like_credential`) was not
  in the prompt. It was added after
  `test_a_credential_VALUE_under_a_header_name_is_still_refused` failed: the
  existing `_refuse_credential_values` only screens values under
  credential-shaped *key* names (`_SECRET_EXACT` / `_SECRET_SUFFIXES`), and a
  header name like `X-Forwarded-Auth` matches neither, so `sk-…` would have
  loaded. Four lines, same heuristic, same error shape.
