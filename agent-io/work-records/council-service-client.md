# Work Record — council-service-client

## task_id
council-service-client

## branch
maestro/developer/background-reference-only-agent--council-service-client

## commit_shas
- `7ae94aa` — feat(council): client for Filipe's deployed GO council on poplar
- `b9767a2` — docs(work-record): record the council service-client milestone

## summary
Added `ConcordiaServiceClient`, the optional comparison-only client for Filipe
Liu's deployed GO council service on poplar (`mfgopredict-service` 0.1.0,
FastAPI, `http://poplar.cels.anl.gov:8300`), whose decision logic is Jose
Faria's CONCORDIA runner (`scripts/go_council_pilot.py`, commit `8e8b05f` per
the live status payload).

`src/kbutillib/domains/ai/council/service_client.py` (364 lines) exposes the
five endpoints a comparison needs, one request each and no polling:

| method | request | auth |
|---|---|---|
| `health()` | `GET /health` | none |
| `models()` | `GET /models` | none |
| `status()` | `GET /concordia_go_council` | none |
| `menu(items, **params)` | `POST /concordia/go/menu` | **none** (open endpoint) |
| `council(items, profile=None, roles=None, force_council=False, use_layer1=False, **extra)` | `POST /concordia_go_council` | `X-API-Key` |

Plus `ConcordiaServiceClient.row_to_comparison(row)` (staticmethod): maps the
service's `final_go_ids` list of `{go_id, name}` objects to `selected_ids`, and
carries `status`, `decline_reason`, `final_confidence` and the untouched row
under `service_row`. It builds no `CouncilVerdict` and nothing from the service
is written to a store.

Refusals before the wire: more than 20 items → `ValueError`
(`service_client.py:305`); no API key → `CouncilServiceAuthError` naming
`$COUNCIL_API_KEY`, raised at `service_client.py:171` before the body is built
or any request made. Status mapping (`service_client.py:_result`, line 182):
401 → `CouncilServiceAuthError`, 503 → `CouncilServiceUnavailable` quoting the
body, 422 → `ValueError` quoting the body, anything else →
`httpx.Response.raise_for_status`. No credential is logged or placed in a
message; response bodies pass through `backends.http_common.redact`, which is
covered by a test that puts the key value in the 503 body.

`council/__init__.py` already mapped `ConcordiaServiceClient -> service_client`
in its lazy loader, so the new module is reachable as
`from kbutillib.domains.ai.council import ConcordiaServiceClient` with no edit
to `__init__.py`, `types.py`, `protocols.py` or `config.py` (none were
modified).

## files_touched
- `src/kbutillib/domains/ai/council/service_client.py` (new, 364 lines) —
  `ConcordiaServiceClient`, `CouncilServiceError` /
  `CouncilServiceAuthError` / `CouncilServiceUnavailable`, and the module
  constants `DEFAULT_BASE_URL`, `DEFAULT_API_KEY_ENV`, `DEFAULT_TIMEOUT_S`
  (900.0), `COUNCIL_MAX_ITEMS` (20).
- `tests/domains/ai/council/test_service_client.py` (new, 38 tests) —
  `httpx.MockTransport` throughout except `TestNoInjection`.
- `tests/domains/ai/council/fixtures/concordia_openapi.json` (new, 54 617 B) —
  the live OpenAPI document, byte-for-byte.
- `tests/domains/ai/council/fixtures/concordia_openapi.meta.json` (new) —
  capture time, URL, byte count, sha256, `"redacted": false`.
- `tests/domains/ai/council/fixtures/concordia_status.json` (new, 2 851 B) —
  the live `GET /concordia_go_council` payload, byte-for-byte.
- `tests/domains/ai/council/fixtures/concordia_status.meta.json` (new) — same
  sidecar fields.

No existing file was modified. `pyproject.toml` was not touched: the client
needs only `httpx` (the existing `ai` extra) and the stdlib — the `council`
extra (SQLAlchemy, psycopg) is the store's, and this client has no store.

## live capture (the only network calls made)
Two free `GET`s, both HTTP 200, both from this worktree on h100/poplar. No
`POST` to any endpoint, nothing on ports 8301–8309, no API key needed or sent.

| fixture | URL | captured (UTC) | bytes | sha256 (of the file) |
|---|---|---|---|---|
| `concordia_openapi.json` | `http://poplar.cels.anl.gov:8300/openapi.json` | **2026-10-15T20:57:05Z** | 54 617 | `37a62cffae5420ecfcdd00da049fe8fbb7938cdb4d00c3b3553f66d9e790d8ca` |
| `concordia_status.json` | `http://poplar.cels.anl.gov:8300/concordia_go_council` | **2026-10-15T20:57:07Z** | 2 851 | `2d50cd75f55063704f879b13995b8daa1d262c830b744d06cbda90f4a639e3a7` |

**Secrets check: nothing was redacted, because neither payload carries a
credential value.** The status payload carries only booleans —
`endpoints.<alias>.has_key` and `auth.key_configured` — alongside non-secret
endpoint base URLs (`https://apps.inside.anl.gov/argoapi/v1`, the
`host.docker.internal:830x` vLLM ports, `mag.genesis.americansciencecloud.org`),
roster/profile model aliases, `missing_credentials: []` and the CONCORDIA
commit `8e8b05f`. Both files are therefore byte-for-byte the response bodies,
and the sha256 above verifies against the checked-in file.

## capture vs Filipe's readable source (success criterion 3)
Comparison target: the readable checkout on this machine,
`/scratch/fliu/hub_home/fliu/python3/mfgopredict_service`
(`src/mfgopredict_service/app.py`, mtime 2026-10-15 09:37, an uncommitted
working tree — `git log` reports "branch 'master' does not have any commits
yet", so there is no source SHA to pin). Read-only inspection; nothing in that
tree was touched.

**For every field of the request contract, the capture and the readable source
agree today — no differences found.** Checked field by field:

- `MenuSettings` (`app.py:518`, the base of both request models) → captured
  `CouncilRequest`/`MenuRequest` properties: `retrieval_k` (60, 1–200), `cap`
  (40, 1–100), `max_height` (1, ≥0, nullable), `pool_by_gene` (true),
  `use_layer1` (true), `use_gpu` (false), `gpu_index` (null, ≥0), `model`
  (`"minilm"`) — identical names, types, defaults and bounds.
- `CouncilRequest` (`app.py:547`) → `items` (minItems 1), `profile`, `roles`
  (`RoleOverride`), `force_council` (false); identical.
- `MenuRequest` (`app.py:973`) → `items` of `MenuItem`, minItems 1; identical.
- `MenuItem` (`app.py:418`) / `CouncilItem` (`app.py:429`) → `text` (required,
  minLength 1), `id`, `source` (`"unknown"`), `gene_id`, and for `CouncilItem`
  also `candidates`; identical, including `CouncilCandidate.definition`'s
  `alias="def"` appearing as `"def"` in the capture.
- Item caps: source `config.py:58-60` sets `council_max_items = 20` and
  `council_stage_max_items = 1000`; the captured descriptions say "at most 20"
  and "at most 1000". Both caps are `model_validator`s, so neither is in the
  JSON Schema — which is exactly why the client refuses >20 itself.
- The seven named `COUNCIL_EXAMPLES` (`app.py:438`) all appear in the captured
  `requestBody.content.application/json.examples` (`mixed`, `minimal`,
  `one_gene_many_tools`, `force_council`, `own_menu`, `swap_a_seat`,
  `cheap_menu`).

**The drift the PRD warned about is real but historical.** The PRD's research
note (`agent-io/prds/kbu-ai-council-v1/research/filipe-concordia-service.md:14`
on `wip`) recorded a live `MenuRequest`/`CouncilRequest` carrying
`menu_source`, `topk` and `tau`, which the readable source lacked. In this
capture **those three properties are gone from both request schemas**
(`removed_since_research: ['menu_source', 'tau', 'topk']`, nothing added, no
default changed, `CouncilItem`/`MenuItem` unchanged). So the deployment and the
checkout have converged since that research pass, and a client written against
the research-era capture would now be sending three fields the service no
longer declares. The capture, not the source and not the research doc, is what
the tests validate against.

## reconciliation (prompt vs tree)
1. **`row_to_verdict` vs `row_to_comparison`** — success criterion 1 names
   `row_to_verdict`; the prompt body, the PRD (`fullprompt.md:483`) and the
   dispatcher's note all name `row_to_comparison`. Implemented
   `row_to_comparison` only; **no `row_to_verdict` alias exists** (asserted
   indirectly by `test_it_returns_a_plain_dict_and_never_a_verdict`, and
   checked by hand: `hasattr(ConcordiaServiceClient, "row_to_verdict")` is
   `False`).
2. **`council/__init__.py` already maps the class** — the prompt says not to
   edit it, and it needed no edit: `_MODULE_MAP["ConcordiaServiceClient"] =
   "service_client"` was already there, which fixed the module filename and the
   class name for me. The two error classes are intentionally *not* in that map
   (it is not mine to edit), so they import from
   `kbutillib.domains.ai.council.service_client`.
3. **Prompt's item shape `[{"id","text","source"}]` is a subset of the live
   schema** — the capture requires only `text` and also accepts `gene_id`
   (both models) and `candidates` (`CouncilItem`). The client passes item dicts
   through verbatim rather than projecting them to three keys, so the richer
   shapes work; a test validates a `gene_id` + `candidates` item against the
   captured `CouncilRequest`.
4. **`use_layer1=False` default contradicts the service's own default of
   `True`** — kept the prompt's `False` (it is the cheap, GPU-free menu, which
   suits a comparison run on someone else's GPUs) and sent it explicitly so the
   behaviour does not depend on the deployment's default. Documented on the
   method and asserted by
   `test_the_minimal_council_body_validates_and_omits_the_unset_seats`.
5. **"the service is unreachable → STOP"** did not trigger: both GETs returned
   200 and `available: true`.
6. **503 is listed in the prompt's mapping but is not in the captured
   `responses` for either POST** (only 200 and 422 are declared, while the
   endpoint's own description documents the 503 and the 401). Kept the full
   mapping from the prompt and the description — the capture's `responses`
   block is simply incomplete, and FastAPI does not declare `HTTPException`
   statuses it raises by hand.

Deliberate small additions beyond the literal prompt, all in service of it: a
`CouncilServiceError` base class so one `except` covers both refusals; `close()`
/ context-manager support on the client; and a `client_kwargs` attribute (the
same convention `backends/http_common.client_kwargs` already establishes) so the
no-injection test can assert no transport was injected.

## tests_run
All runs foreground, in this task's own venv
(`.task-venv`, CPython 3.12.3, pytest 9.1.1, httpx 0.28.1, SQLAlchemy 2.1.4,
jsonschema 4.10.3), installed per the dispatcher's note
(`pip install -e '.[council]'`, `-e '.[dev,ai]'`, `pytest pytest-asyncio
pytest-cov ruff`). No shared or service venv was touched.

```
# the new module's tests
python -m pytest tests/domains/ai/council/test_service_client.py -q
# 38 passed in 0.92s                                    exit code 0

# the council package (baseline before the change: 634 passed, 243 skipped)
python -m pytest tests/domains/ai/council/ -q
# baseline at 4ff1a4e: 634 passed, 243 skipped          exit code 0
# on this branch:      672 passed, 243 skipped          exit code 0

# everything under tests/domains
python -m pytest tests/domains/ -q
# 928 passed, 243 skipped in 21.92s                     exit code 0

# the rest of the suite (baseline's --ignore set, which excludes 7 modules
# that cannot be collected without optional scientific deps)
python -m pytest -q --ignore=tests/domains --ignore=... (baseline ignore set)
# 26 failed, 3915 passed, 429 skipped in 105.66s        exit code 1

# base state for those 26, established by name in the same venv: the two new
# files moved aside, the same 26 nodeids re-run
python -m pytest -q <the 26 nodeids>
# 26 failed in 11.86s                                   exit code 1
# diff of the two failure name-sets: EMPTY -- identical

python -m ruff check src/kbutillib/domains/ai/council/service_client.py \
    tests/domains/ai/council/test_service_client.py
# All checks passed!                                    exit code 0
```

**Regression verdict: no test that passed before this change fails after it.**
The 26 failures (`tests/cli/test_kind.py` ×17, `tests/cli/test_clearinghouse*.py`
×8, `tests/core/test_composition_smoke.py` ×1) reproduce byte-identically with
`service_client.py` and `test_service_client.py` removed from the tree, so they
are pre-existing and unrelated. `build-baselines/kbutillib.json` was **not**
updated: the dispatcher flagged it stale, the comparison above was made against
a base run in this venv instead, and rewriting the cache file is not part of
this task's diff.

## test coverage of the success criteria
`tests/domains/ai/council/test_service_client.py`, 38 tests in 7 classes:

- `TestEndpointRouting` (7) — each method makes exactly one request with the
  right verb and path; the default base URL is the poplar deployment; a
  trailing slash does not double up.
- `TestAuthHeader` (5) — `council` sends `X-API-Key` (explicit key, env key,
  explicit beats env); `menu` sends **no** key header even when one is
  available; the three GETs send none.
- `TestRefusalsBeforeTheWire` (5) — 21 items → `ValueError` with **zero**
  requests recorded; exactly 20 is sent; `menu` has no 20-item cap; a missing
  key → `CouncilServiceAuthError` with **zero** requests; a missing key does
  not block the open endpoints.
- `TestStatusMapping` (7) — 401/503/422 mapping (503 and 422 quote the body),
  422 from the menu endpoint too, 500 → `httpx.HTTPStatusError`, and two
  tests that the key value never appears in a message (including when the
  response body echoes it, which `redact` strips).
- `TestRequestBodies` (6) — the council and menu bodies the client builds
  validate against the captured `CouncilRequest` / `MenuRequest`; unset
  `profile`/`roles` are omitted; a `gene_id` + `candidates` item validates; a
  non-vacuousness test shows the check rejects a missing `text`, a missing
  `items` and a misspelled `use_layer_1`; and one test pins the captured
  document itself (title/version, both `$ref`s, `APIKeyHeader` =
  `X-API-Key`, "at most 20", no `security` on the menu POST) so a later
  re-capture cannot silently move the contract.
- `TestRowToComparison` (6) — `{go_id, name}` objects → ids in order; the row
  is kept whole; a declined row (`guard`, `contentless`) and an error row; the
  return is a plain `dict` with exactly five keys and no verdict; a whole
  response maps row by row.
- `TestNoInjection` (2) — **criterion 2**: `ConcordiaServiceClient` built with
  **no** `transport` kwarg, `COUNCIL_API_KEY` deleted with
  `monkeypatch.delenv`, `base_url` pointing at a 127.0.0.1 port that was bound
  and then closed; `council([item])` raises `CouncilServiceAuthError` (any
  request actually sent would have raised `httpx.ConnectError` instead), and
  `"transport" not in client.client_kwargs`. The second test pins the 900 s
  timeout on the real client.

Schema validation uses `jsonschema.Draft202012Validator` when installed and a
required-field check when it is not; both paths also reject a key the request
schema does not declare (FastAPI emits no `additionalProperties: false`, so the
schema alone would accept a typo). The fallback path was exercised by running
the module's `validation_error()` with `sys.modules["jsonschema"] = None`: it
returns `None` for both good bodies and a message for all three bad ones, the
same verdicts as the jsonschema path.

## caveats
1. **No test or code path ever calls the live service.** The only requests this
   task made are the two documented `GET`s. The client has never been exercised
   against the real deployment, so the contract evidence is the captured
   OpenAPI document, not an observed round trip. In particular the 401/503
   mappings are derived from the endpoint's own documentation (the capture's
   `responses` block declares only 200 and 422).
2. **`row_to_comparison` assumes the documented row shape.** It reads
   `final_go_ids` entries as mappings with a `"go_id"` key — the capture's
   `GOTerm` requires exactly that — and uses `.get()` for the other three
   fields, so a truncated row yields `None` rather than raising. A
   `final_go_ids` entry that is a bare string (the notebook pipeline's parquet
   encoding, per the research doc) would raise `TypeError`; the REST contract
   never returns that shape, and converting it was not in scope.
3. **`menu()` does not enforce the service's 1000-item stage cap.** Only the
   council's 20 was specified and only it costs money to get wrong; a longer
   menu batch comes back as the mapped 422.
4. **`jsonschema` is not declared in any extra.** It happened to be present in
   this venv, so the strict path ran here; in a venv without it the tests still
   pass on the required-field path (verified as described above), which is the
   behaviour the task asked for.
5. **The readable-source comparison is against a working tree, not a commit.**
   `/scratch/fliu/hub_home/.../mfgopredict_service` has no commits on its
   branch, so the "no differences" finding is pinned to the file mtime
   (2026-10-15 09:37) and nothing stronger. Whether that tree is the code in
   the container is still unverifiable from outside it.
