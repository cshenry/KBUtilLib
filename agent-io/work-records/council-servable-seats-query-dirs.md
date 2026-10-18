# Work Record — council-servable-seats-query-dirs

## task_id
council-servable-seats-query-dirs (M2.3)

## branch
`maestro/developer/background-reference-only-agent--council-servable-seats-query-dirs`

## base
KBUtilLib `main` @ `8cc7d4b` (M1 + answer-normalisation + llm-call-wrapper +
local-only-cleanup).

## summary

Two features that make a PER-MACHINE council queue safe and portable, plus the
round-3 rule that a job's permissions belong to that job alone.

**A. Servable seats at submit.** `CouncilUtils.submit` / `rerun` (and therefore
`kbu council submit` and `kbu council run --query`) now run the SAME offline
per-endpoint check `kbu council doctor` runs — once per distinct endpoint in
the roster, spending nothing — and refuse a job whose seats this machine
cannot serve. The reason is in the docstrings: the store is one machine's
single-user SQLite file and nothing coordinates across hosts, so no other
machine will ever work a seat this one cannot reach; a call row for an
unreachable endpoint is not work waiting for somebody, it is a job that never
finishes.

* `unavailable` → `SeatUnavailable`, naming each affected seat, its endpoint
  and the doctor's own detail. With `allow_unavailable=True` /
  `--allow-unavailable` the job is created and `job_status` reports the
  endpoint as a blocker of kind `unservable`; its calls then **wait** (parked
  `model_unavailable`) rather than spend.
* `unknown` follows the existing `allow_unverified` rule.

**B. Query directories.** New optional `ai_council.query_dirs` (list of paths,
`~` and `${VAR}` expanded). Every `*.yaml`/`*.yml` directly in each directory
(non-recursive) is registered through the existing registry path whenever
`CouncilUtils` opens the store and in `kbu council db init`: identical is a
no-op, a changed file at the same `name@version` raises `QueryTypeExists`
naming the file, a missing directory warns, an invalid file is an error naming
the file. Nothing is ever written into these directories (they are expected to
be in Dropbox). `kbu council query list` shows `source` (`directory`) and a new
`source_file` column.

**Round 3.** `allow_unverified` and `allow_unavailable` are persisted in the
job's `options` at submit and apply to that job's calls only: `CouncilWorker`
decides per call from `jobs.options` (`CouncilWorker.job_permissions` +
`_permission_gate`), never from a flag on the worker object or on a cached
client. A `refused` billing check is never bypassed. Readiness of an api type
with no `preflight()` and no `models()` is `unknown` (never an
`AttributeError`); an empty listing from an api type that HAS `models()` stays
`unavailable`.

## files_touched

New:

| File | What it holds |
|---|---|
| `src/kbutillib/domains/ai/council/servable.py` (260 lines) | `SEAT_STATES`, `SeatUnavailable`, `probe_endpoint`, `seat_state`, `probe_roster`, `unservable_message` — the ONE readiness implementation, now shared by doctor, submit and the worker |
| `src/kbutillib/domains/ai/council/queries/directories.py` (175 lines) | `expand_query_dirs`, `spec_files`, `register_query_dirs`, `annotate_sources`, `SOURCE_DIRECTORY` |
| `tests/domains/ai/council/test_servable_and_query_dirs.py` (24 tests) | the whole feature, through the public submit path into a real temp SQLite store |

Changed:

* `src/kbutillib/domains/ai/council_utils.py`
  * `_probe_endpoint` / `_seat_report` now delegate to `council.servable`
    (the doctor's behaviour is unchanged; the logic moved so submit and the
    worker cannot disagree with it).
  * new `_servable_seats()` (the refusal) and `_job_options()` (the persisted
    permissions).
  * `submit_async` / `submit` / `rerun` take `allow_unavailable`, run the
    check, and write `{"allow_unverified", "allow_unavailable",
    "unservable_endpoints"}` into the job's options.
  * `run_async` reads the job's `options` for the permission it builds its
    clients with (it works exactly one job).
  * `store()` loads the configured `query_dirs` on open (`_load_query_dirs`);
    `db_init()` loads them after creating the schema and reports
    `query_dirs` / `query_dir_types`; `queries()` annotates each row with
    `source_file`; new `query_dir_sources` attribute.
* `src/kbutillib/domains/ai/council/engine/worker.py` — `job_permissions()`,
  `_permission_gate()`, `_billing_state()`, `_endpoint_state()`; `_invoke`
  takes the job row and runs the gate before rendering a prompt.
* `src/kbutillib/domains/ai/council/store/sqlite.py` — `_blockers` emits the
  `unservable` kind from the job's own options.
* `src/kbutillib/domains/ai/council/config.py` — `query_dirs` top-level key,
  `CouncilConfig.query_dirs`, `_resolve_query_dirs`.
* `src/kbutillib/domains/ai/council/default_config.yaml` — commented
  `query_dirs` example.
* `src/kbutillib/domains/ai/council/queries/__init__.py`,
  `src/kbutillib/domains/ai/council/__init__.py` — exports.
* `src/kbutillib/interfaces/cli/council.py` — `--allow-unavailable` on
  `submit` and `run`; submit's envelope carries `blockers` and warns about
  them; `query list` shows `source_file`.
* `tests/domains/ai/council/test_council_utils.py`, `tests/cli/test_council_cli.py`
  — the fake backend those fixtures inject now LISTS the five models the
  config seats (`ServingFake` / `_ServingFake`). `FakeBackend.models()`
  returns `[]` because a fake enumerates nothing, which is correct for a
  backend and is now "unavailable" to the submit check; a stand-in for a
  reachable endpoint has to answer `/models` the way a reachable one would.
  No assertion was weakened or skipped.

## schema

**No migration.** `query_types` already has a `source` column at schema v2
(`String(32)`, default `"user"`), so a directory-loaded spec is recorded with
`source="directory"` and the schema stays at v2. The FILE a spec came from is
deliberately NOT stored: `put_query_type` compares specs byte for byte, and an
absolute path inside the row would make the same shared spec compare unequal
between two machines and turn a shared Dropbox directory into a
`QueryTypeExists` on arrival. The path is reported from the directory scan the
current process did when it opened the store, which is also why it reports
where a spec IS rather than where it once was.

## reconciliation (prompt vs tree)

| The prompt said | The tree held | What I did |
|---|---|---|
| "`CouncilWorker.submit` … run the same offline per-endpoint check" | `CouncilWorker.asubmit` has no backends it can probe for a `fake` endpoint, and every engine/normalise/llm-wrapper test submits through it with a `FakeBackend` whose `models()` is `[]` — i.e. "unavailable". Putting the check there would have refused several hundred existing tests' submits. | Put the check in `CouncilUtils.submit_async` / `rerun`, which is the path `kbu council submit` and `run --query` take (and what dispatcher criterion 2 requires: the PUBLIC submit path). `CouncilWorker` keeps the per-call half of the job (the round-3 gate). Noted as a deliberate divergence. |
| "the API type … goes through llm.preflight / llm.models after task llm-call-wrapper" | `llm.preflight_api` / `llm.models_api` are the instance forms, and `CouncilUtils` holds instances already | Used the `*_api` forms, so a job does not re-resolve an endpoint (and re-read a CLI's config files) per probe. |
| "query_types `source` column … check the schema before adding one" | v2 already has it | No column, no v3 migration. See **schema** above. |
| `src/kbutillib/domains/ai/llm/api_types/` | the shipped api types are in `llm/apis/` | Used `llm/apis/`. |
| (implied) a plug-in `kind` can be seated | `SeatSpec.endpoint_kind` is still a `Literal` over the six known kinds, so a plug-in kind cannot be put in a roster at all (M2.1 relaxed `EndpointConfig.kind` but not this) | Did NOT widen it — that is a design decision and out of this task's scope. The tests register their api type under `fake`, which the wrapper deliberately leaves unregistered for exactly this purpose and which `SeatSpec` accepts. **Flagged for the next council task:** an endpoint of a genuinely third-party kind cannot currently be seated. |

## decisions worth knowing

* **The split between the two checks.** For a non-CLI endpoint, readiness is
  the `/models` listing; a `preflight()` is only asked for when there is no
  listing to read. That keeps the doctor's existing behaviour exactly (a test
  pins that an HTTP endpoint costs one probe) and puts the per-job billing
  decision in the worker's gate, where round 3 asks for it. An api type with
  BOTH `models()` and `preflight()` is therefore admitted at submit on its
  listing and judged on its preflight at the call.
* **A per-job refusal does not halt the endpoint.** `billing_guard` is in
  `HALTING_KINDS`, so letting the gate's refusal travel the normal route
  would have halted the endpoint store-wide — stopping a *different* job that
  legitimately carries `allow_unverified`. The gate fails that one call with
  `halts_endpoint=False` instead. A `refused` preflight still halts, because
  that one IS a fact about the endpoint.
* **The worker caches readiness, not permission.** `_billing_state` and
  `_endpoint_state` are cached per endpoint for the worker's life (they are
  facts about this machine; re-probing per call in a 20k batch is pure
  syscall/HTTP overhead). The permission is re-read from the call's job every
  single call. A consequence: an endpoint that recovers mid-`run` is picked up
  by the next `run`, not mid-sweep.
* **`evaluate()` was left alone.** It is not one of the submit paths the task
  names and has no `allow_unavailable`; adding one would widen the surface.
  Its calls still pass the worker's per-call gate.

## verification

Commands run in this task's own venv
(`.task-venv`, cpython 3.12.3, pytest 9.1.1; `pip install -e '.[council]'`
plus `pytest pytest-asyncio pytest-cov ruff`).

| Command | Exit | Result |
|---|---|---|
| `python -m pytest -q tests/domains/ai tests/cli/test_council_cli.py` | 0 | **951 passed, 2 skipped** (927 + the 24 new tests) |
| `python -m pytest -q tests/domains/ai/council/test_servable_and_query_dirs.py` | 0 | 24 passed |
| `python -m ruff check` (every file touched) | 0 | All checks passed |
| full suite, branch (`tests`, 12 uncollectable optional-dep modules ignored) | 1 | 110 failed, 4921 passed, 417 skipped, 1 error |
| full suite, base `8cc7d4b` in the same venv (`git worktree` + `PYTHONPATH`) | 1 | 110 failed, **4897** passed, 417 skipped, 1 error |

**Regression comparison is BY NAME and the two sets are identical**: `diff` of
the sorted `FAILED` lines from the two runs is empty. All 110 are pre-existing
environment gaps in this minimal venv (`tests/cli/test_kind.py` wants a KING
stack on disk, `tests/cli/test_clearinghouse*.py` wants a BERDL pod, the rest
are missing optional scientific deps — pandas/torch/rdkit-backed registry
tests). The branch's +24 passed are exactly the new module.

The committed `build-baselines/kbutillib.json` was NOT used as the baseline and
was NOT updated: it was captured at `0f84803` in a different venv (99 failures,
a different `--ignore` set), so it is stale for this comparison. The base run
above replaces it for this task.

## not done / inferred rather than verified

* No test exercises a REAL endpoint, CLI or GPU; every probe answer comes from
  a test-registered api type. That the shipped `argo`/`openai` backends have no
  `preflight()` (so their readiness is the `/models` listing) is read from
  `llm/apis/http*.py`, not exercised against a live gateway.
* The daemon is out of scope (last task), so "one worker, two jobs with
  different permissions" is driven by `worker.work(until_idle=True)` over the
  whole queue rather than by the daemon.
* `kbu council doctor`'s own output is unchanged by design; its existing tests
  pin that and still pass, which is the evidence.
