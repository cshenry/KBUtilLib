# Work Record — council-facade-cli

## task_id
council-facade-cli

## branch
maestro/developer/background-reference-only-agent--council-facade-cli

## base commit
`5ad3ca2` (`docs(work-record): fill in the record's own commit sha`)

## commit_shas
- `9db04bf` — feat(council): CouncilUtils facade, toolkit property and the kbu council CLI
- `1c431fe` — docs(council): README section, plus blocked-run and parquet coverage
- `662b05a` — docs(work-record): record the council facade/CLI milestone

## summary
Wired the council (M1, local SQLite store) into KBUtilLib's three public
surfaces: the library facade, the `KBUtilLib()` toolkit, and the `kbu` CLI.
Nothing under `src/kbutillib/domains/ai/council/` was modified — this task is
entirely the layer above it — and `argo_utils.py` and `ai_curation_utils.py`
are **byte-identical to the base commit** (sha256 of each verified against
`git show 5ad3ca2:<path>`; `git diff 5ad3ca2 HEAD -- <both>` is 0 lines).

### 1. `src/kbutillib/domains/ai/council_utils.py` (1080 lines, new)
`CouncilUtils(SharedEnvUtils)` plus `CouncilUtilsImpl(env)` on the
`ArgoUtilsImpl` pattern (lazy delegate, `env` property, `__getattr__`
delegation).

**It does not inherit `ArgoUtils`**, and `test_council_utils_does_not_inherit_argo_utils`
asserts that over the MRO. `ArgoUtils.__init__` builds an `httpx` client and
can ping the gateway, so inheriting it would put a network round trip behind
`KBUtilLib().council`. The council reaches ARGO through `ArgoBackend`, built
from the one endpoint-config mapping `CouncilConfig.endpoint(alias)` returns,
so there is nothing to inherit.

| method | notes |
|---|---|
| `config(reload=False)` / `config_snapshot()` / `store_url()` | cached `load_council_config` |
| `store(**kw)` | `open_store(store_url())`, cached; **never creates a schema** |
| `require_schema()` | raises `SchemaVersionMismatch` naming `kbu council db init` |
| `db_init()` / `db_migrate()` / `db_check()` | `init_schema` + `seed_builtins`; migrate opens with `check_version=False`; check reports rather than raises |
| `user()` | KBase username via `KBaseAuth().get_user(token)` **only when a token is configured**, else `getpass.getuser()` |
| `roster(profile=None, **seat_overrides)` | `CouncilConfig.roster(profile, overrides=...)` |
| `build_backends(roster, allow_unverified=None)` | one backend per DISTINCT endpoint, via `_backend_for` |
| `submit` / `submit_async` | `-> job_id`; spends nothing (pending call rows) |
| `run` / `run_async` | foreground worker `until_idle`; returns `job_status` + `["worker"]` summary |
| `evaluate` / `evaluate_async` | one item -> `CouncilVerdict`, via `engine.aevaluate` |
| `job_status` / `results(…, as_dataframe=False)` / `cancel` / `delete` / `rerun` / `jobs` | |
| `registry()` / `queries()` / `query(ref)` / `register_query(path)` / `retire_query(name, version)` | |
| `search_calls(**filters)` | `entity=` is hashed here into `entity_hash` |
| `endpoint_status()` / `reset_halt(ep)` / `reset_budget(ep)` | every CONFIGURED endpoint is reported, halted or not |
| `doctor` / `doctor_async` | see below |
| `models` / `models_async(endpoint=None)` | |
| `service_client(**kw)` | `ConcordiaServiceClient` |

`doctor` **never calls `complete()` and never sends a prompt**. It probes each
ENDPOINT once (not each seat — five seats on one endpoint is one `/models`
GET), then derives each seat's state from that probe:

- HTTP: `models()` (a free GET) answered a non-empty listing **and** the
  served model appears in it → `ready`; empty listing → `unavailable`
  ("unreachable, refused the request, or does not implement /models");
  reachable but model absent → `unavailable`, quoting what it does list.
- CLI: `version()` (`--version`) then `preflight()` (the offline
  `billing_preflight`, which reads config files only): `ready` → `ready`,
  `refused` → `unavailable`, `unknown` → `unknown`.
- `verified: false` → `unknown` **before any probe**, because a reachable
  endpoint is not evidence that it serves the model the seat names.

It also reports store reachability + schema version, the submitting user, and
`dropped_billing_vars()` — **NAMES only**, with a test that puts a secret value
in the environment and asserts it does not appear anywhere in the report JSON.
`exit_code` is computed in the facade (1 any unavailable, else 2 any unknown,
else 0) so the CLI does not re-derive it.

**Sync wrappers** (`_run_sync`): no running loop → a loop owned by that call;
a loop already running (Jupyter) → the coroutine goes to a worker thread with
its own loop and the caller blocks on the join. Two tests pin this: the facade
works from inside `loop.run_until_complete(...)`, and `CouncilWorker`'s own
`_run_coroutine` still refuses to nest — the division of labour is deliberate.

### 2. `src/kbutillib/toolkit.py` (+9 lines)
A lazy `council` property built exactly like `argo` and `curation`
(`TYPE_CHECKING` import, `self._council = None` initialiser, local import in
the getter). The property itself reads no config and opens no store, because
`CouncilUtilsImpl`'s delegate is lazy too — asserted by
`test_toolkit_property_is_lazy_and_cached`.

### 3. `src/kbutillib/domains/ai/__init__.py` (+5 lines)
`CouncilUtils` / `CouncilUtilsImpl` mapped to `"council_utils"` in the lazy
loader and added to `__all__`.

### 4. `src/kbutillib/interfaces/cli/council.py` (1144 lines, new)
`council_cmd`, registered as `main.add_command(council_cmd, name="council")`
beside `clearinghouse_cmd`. Verbs:

```
doctor [--profile]
submit --query NAME[@VER] --input FILE.jsonl [--label] [--priority] [--profile]
       [--seat SEAT=ALIAS ...] [--allow-unverified]
run (--job ID | --query ... --input ... [+ the submit options])
jobs [--all-users] [--status] [--include-deleted]
jobs status ID | results ID [--out F] [--format jsonl|parquet] [--include-pending]
     | cancel ID | delete ID | rerun ID [--profile] [--seat ...] [--query-version]
query list [--include-retired] | show NAME[@VER] | register FILE | retire NAME@VER
calls search [--query] [--model] [--entity TEXT | --entity-hash H] [--data-hash H]
             [--status] [--limit]
endpoints status | reset-halt ENDPOINT | reset-budget ENDPOINT
db init | migrate | check
models list [--endpoint]
```

Every verb takes `--json` and emits exactly one envelope,
`{"schema_version": 1, "verb", "generated_at", "data", "warnings"}`, with the
human path handing that same dict to `_render` — pinned by a test that patches
`_render` and asserts it received what the serialiser would have emitted.
Warnings go to stderr as well as into the envelope; under `--json`, stdout
carries the object and nothing else (pinned with a stream-separated
`CliRunner`).

Exit codes: `doctor` exits 1 / 2 from the facade's `exit_code`; `run` exits 1
when `job_status`'s `blockers` is non-empty.

**The schema guard** lives in one place, `_ready(ctx)`: every verb except
`db *` and `doctor` goes through it, so a store with no tables fails with the
`kbu council db init` instruction and nothing creates the schema implicitly.

## files_touched
New:
- `src/kbutillib/domains/ai/council_utils.py` (1080 lines)
- `src/kbutillib/interfaces/cli/council.py` (1144 lines)
- `tests/domains/ai/council/test_council_utils.py` (708 lines, 33 tests)
- `tests/cli/test_council_cli.py` (786 lines, 24 tests)
- `agent-io/work-records/council-facade-cli.md` (this file)

Modified:
- `src/kbutillib/toolkit.py` (+9: the `council` property, its initialiser, its
  `TYPE_CHECKING` import)
- `src/kbutillib/domains/ai/__init__.py` (+5: the two exports and a docstring
  import line)
- `src/kbutillib/interfaces/cli/__init__.py` (+2: the import and the
  `add_command`)
- `src/kbutillib/domains/ai/README.md` (+171/-7: the council section)

Untouched, deliberately: everything under
`src/kbutillib/domains/ai/council/`, `argo_utils.py`, `ai_curation_utils.py`,
`pyproject.toml` (see reconciliation 1).

## reconciliation (prompt vs tree)
1. **"declared in a new optional extra `council` in pyproject.toml" — it is
   already there.** `pyproject.toml:97-100` declares
   `council = ["SQLAlchemy >=2.0", "psycopg[binary] >=3.1"]` with the comment
   block the prompt asks for, landed by the store task. `pyproject.toml` was
   therefore not modified. The package-data globs for
   `domains/ai/council/*.yaml` and the query-type JSON/YAML are likewise
   already present (`pyproject.toml:253-270`).
2. **`doctor`'s "HTTP: models() reachable" cannot distinguish unreachable from
   unenumerable.** `HttpBackend.models()` is documented to return `[]` on ANY
   failure and never to raise (`http_common.py:309-345`) — listing is a
   diagnostic, not a precondition. So an empty listing is reported
   `unavailable` with a detail naming all three possibilities rather than
   being split into two states the backend cannot tell apart. This is what
   makes the no-injection criterion's closed port report `unavailable`.
3. **"the sync wrapper uses `asyncio.run`" — it does not, and deliberately.**
   `CouncilWorker._run_coroutine` (`engine/worker.py:1549-1578`) avoids
   `asyncio.run` because it sets the thread's current event loop to `None` on
   the way out, which breaks other modules in this repository that still call
   `asyncio.get_event_loop()`; the engine tests' own `run()` helper records the
   same reasoning. `_run_sync` therefore creates, uses and closes a loop
   locally, leaving process-global asyncio state as it found it. The worker
   thread branch for "a loop is already running" is exactly as the prompt
   specifies.
4. **`*_async` twins exist for the methods that actually await something** —
   `submit`, `run`, `evaluate`, `doctor`, `models` — and not for the
   store-only/config-only methods (`store`, `user`, `jobs`, `results`,
   `job_status`, `cancel`, `delete`, `rerun`, `queries`, `search_calls`,
   `endpoint_status`, `db_*`). Those never await: SQLite reads are blocking
   calls, and an `async def` wrapper around one would be a false promise about
   where the thread goes. Documented in the module docstring so the asymmetry
   is a stated decision rather than an omission.
5. **`models(endpoint=None)` lists the CONFIGURED aliases, not every
   endpoint's live list.** The packaged default declares eleven endpoints,
   nine of them vLLM ports on poplar; a no-argument `models list` that probed
   all of them would be eleven network calls (and would make the CLI test
   suite reach the network). With `--endpoint NAME` it is that one endpoint's
   live `models()`. `doctor` remains the verb that probes.
6. **`rerun` records the parent in `options["rerun_of"]`, and
   `jobs.parent_job_id` stays null.** The column exists
   (`store/schema.py:173`) but `CouncilWorker.asubmit` builds the job row
   itself (`engine/worker.py:418-436`) and the store contract exposes no way
   to set that column from a caller. Writing it would mean the facade issuing
   its own UPDATE against `store.transaction()`, i.e. SQL in the facade. A
   later task that adds a `parent_job_id` parameter to `asubmit` should move
   this.
7. **`--config` / `--store` on the `council` group.** The prompt's option
   lists are per-verb and name neither, but the dispatcher's no-injection
   criterion requires a config route ("passed by the CLI's normal config
   route, or via a temp HOME"). Both are group-level options with env-var
   fallbacks (`KBU_COUNCIL_CONFIG`, `KBU_COUNCIL_STORE`), which is also what
   makes the test suite able to point at a throwaway SQLite file.
8. **Verbs the prompt's method list did not name but its CLI spec needs** —
   `db_migrate()`, `db_check()`, `reset_budget()`, `query(ref)` and
   `registry()` — were added to the facade so `db migrate`, `db check`,
   `endpoints reset-budget` and `query show` are facade calls rather than CLI
   logic.
9. **Envelope has no `locus` key.** `clearinghouse.py`'s envelope carries one;
   the prompt's shape for this CLI does not, and a council verb has no
   in-pod/off-pod distinction to report. The other four keys and all three
   structural rules are identical.
10. **Work records read first, as instructed**, and they were right about
    every divergence they flagged: `transaction()` is re-entrant
    (`store/sqlite.py:262`), store errors live in `store/errors.py`,
    CLI backends resolve env and HOME at call time (which is what lets the
    no-injection test set a temp `HOME`), and `row_to_comparison` is the
    service client's method name. None of those needed a workaround; the
    facade is written against them.
11. Nothing in the prompt proved unbuildable, and no stop condition fired.

## PostgreSQL extension point (task 11's seam)
`db init | migrate | check` contain **no SQLite-specific code and no
PostgreSQL stub**. They call `open_store(url)`, which already dispatches on
URL scheme (`store/__init__.py:48-103`) and already raises a clean
`ImportError` naming the `council` extra when psycopg is absent, plus
`init_schema()` / `migrate()` / `schema_version()`, all three of which are
`CouncilStore` protocol members. So the three verbs reach a `PostgresStore`
the moment that module lands, with no edit to the CLI or the facade. The only
SQLite-specific thing in this task is in the TESTS (they build
`sqlite:///tmp_path/...` URLs), and `tests/domains/ai/council/store_contract.py`
already has the parametrised PostgreSQL branch that turns itself on when
`KBU_COUNCIL_TEST_PG_DSN` is set and `store.postgres` exists.

## tests
No test makes a network call except to `127.0.0.1`, spends a token, needs a
CLI login or needs a GPU.

`tests/domains/ai/council/test_council_utils.py` (33 tests) — a throwaway
SQLite store in `tmp_path`, `CouncilUtils.build_backends` monkeypatched to one
`FakeBackend`, and the shipped `go_mf` builtin (deliberately, not an in-test
query type: `go_mf`'s `id_field` is `go_id`, so a facade that skipped
`postprocess` would pass against a convenient spec and fail against the
product). Covers: submit -> run -> status -> results end to end; the meta path
on a split vote; an empty menu settling with zero calls; `as_dataframe`;
jobs/cancel/delete/rerun (including that a rerun's items carry the parent's
`data_hash`); the no-implicit-schema rule; `db_init`/`db_migrate`/`db_check`;
the query registry; `search_calls(entity=...)`; endpoint halt/budget;
`evaluate`; the sync wrapper inside a running loop; `doctor` (never calls
`complete()`, one probe per endpoint, dropped NAMES not values, unknown ≠
unavailable, CLI readiness = `version` + `preflight`); `models`; and the
toolkit property.

`tests/cli/test_council_cli.py` (24 tests, `click.testing.CliRunner`,
following `tests/cli` conventions). Covers the `--json` envelope for every
verb exercised, submit + run + `jobs status` + `jobs results` on a fixture
JSONL, results to `--out` as jsonl and parquet, `query list` showing both
builtins, `endpoints reset-halt`, `run` exiting 1 on a blocked job, the
one-rendering rule, the stdout discipline, and the two dispatcher criteria:

- **No-injection doctor** (`TestDoctorWithNothingPatched`) — the REAL
  `build_backends`, **nothing in the council package monkeypatched**. Its
  config declares an `openai` endpoint whose `base_url` is a 127.0.0.1 port
  that was bound and then closed, and a `claude_cli` endpoint whose
  `executable` is a shell script appending its argv to a file. Asserts: both
  HTTP seats report `unavailable` with the port number in the detail; exit code
  1; envelope `schema_version` 1 and `verb` `"doctor"`; and the fake CLI's argv
  log is exactly `["--version"]` — never `-p`, the flag that would have sent a
  prompt. (`HOME` is a temp directory so the preflight reads nothing of the
  real user's Claude configuration.)
- **No implicit schema** (`test_jobs_on_a_schemaless_store_says_run_db_init_and_creates_nothing`)
  — `jobs` against a SQLite path with no schema fails, the message contains
  `kbu council db init`, and a direct `sqlite3` read of the file afterwards
  shows **no tables at all**; a following `db init` then creates them.

## tests_run
All runs foreground, in this task's own venv only
(`.task-venv`, CPython 3.12.3, pytest 9.1.1, SQLAlchemy 2.1.4, httpx 0.28.1,
click 8.1.6, pandas 3.0.6, pyarrow 26.0.0), installed per the dispatcher's
note: `python -m pip install -e '.[council]'`, `-e '.[dev,ai]'`,
`pip install pytest pytest-asyncio pytest-cov ruff`. No shared or service venv
was touched.

```
python -m ruff check src/kbutillib/domains/ai/council_utils.py \
    src/kbutillib/interfaces/cli/council.py \
    tests/domains/ai/council/test_council_utils.py tests/cli/test_council_cli.py
# All checks passed!                                             exit 0

python -m pytest -q tests/domains/ai/council/test_council_utils.py tests/cli/test_council_cli.py
# 57 passed in 3.78s                                             exit 0

python -m pytest -q tests/domains/ai/council/
# 705 passed, 243 skipped in 19.26s                              exit 0

python -m pytest -q tests/domains/ai tests/cli
# 25 failed, 1501 passed, 250 skipped                            exit 1
#   BASELINE at 5ad3ca2 in this same venv: 25 failed, 1444 passed, 250 skipped
#   Compared BY NAME: the two failure sets are IDENTICAL (comm(1) reports no
#   line on either side). All 25 are pre-existing: tests/cli/test_kind.py (17),
#   tests/cli/test_clearinghouse*.py (8).

python -m pytest -q tests/core tests/interfaces tests/guard tests/harness \
    tests/test_argo_utils_construct.py tests/test_python_floor_syntax.py
# 1 failed, 454 passed, 48 skipped                                exit 1
#   tests/core/test_composition_smoke.py::TestKBUtilLibFacade::
#   test_facade_community_lazy_idempotent_and_available_attrs
#   PRE-EXISTING and proved so: re-ran that single test with the BASE commit's
#   toolkit.py checked out in place (git checkout 5ad3ca2 -- toolkit.py) and it
#   fails identically. The cause is `ImportError: KBUtilLib.fba requires the
#   optional dependency cobra` -- an environment gap in the task venv, reached
#   through the `fba` property, which this task does not touch.

python -m pytest -q tests/domains tests/berdl tests/biochem tests/annotators \
    --ignore=tests/biochem/test_escher_utils.py \
    --ignore=tests/biochem/test_ms_biochem_deltag.py
# 2549 passed, 313 skipped in 41.88s                              exit 0
#   The two --ignore'd modules cannot be COLLECTED without cobra; both are in
#   the cached baseline's own --ignore set.

python -m pytest -q tests/external tests/kbase tests/kb_job_utils \
    tests/kb_app_runner tests/models_and_analyses tests/verab tests/arc_context \
    tests/beril_worktree tests/koros_arc_store
# 1010 passed, 257 skipped in 16.31s                              exit 0

python -m pytest -q tests/modeling tests/fixtures tests/test_lp_solver_service.py
# 8 failed, 157 passed, 59 skipped, 4 errors                      exit 1
#   Every failure and error is in tests/modeling/test_ms_reconstruction_utils.py
#   or tests/modeling/test_comprehensive_gapfill_wrapper.py -- the two modules
#   the cached baseline ALSO excludes via --ignore. All are
#   ModuleNotFoundError: cobra / modelseedpy. Nothing in tests/modeling is
#   touched by this task.

python -m pytest -q tests/notebook --ignore=tests/notebook/helpers
# 163 passed in 2.66s                                             exit 0
#   tests/notebook/helpers/conftest.py imports cobra at module scope, so that
#   directory cannot be collected in this venv.
```

**Baseline handling.** `build-baselines/kbutillib.json` exists but is stale
(base commit `0f84803`, captured 2026-09-13 in a different venv), exactly as
the dispatcher said. Per the role prompt I read it rather than re-running the
whole suite to establish a baseline, and because it is stale I additionally
captured a same-venv base run of the two areas this task touches
(`tests/domains/ai` + `tests/cli`) at `5ad3ca2` and compared by NAME. I did
not rewrite the cached file: it would then record this task venv's optional-dep
gaps as if they were the repo's, which is the opposite of useful for the next
task.

**No test that passed on the base commit fails on this branch.**

## verified vs inferred
Verified by running it:
- Every claim in `tests_run` above, including the two dispatcher criteria and
  the byte-identity of `argo_utils.py` / `ai_curation_utils.py`.
- `kbu council --help` and each subgroup's `--help` list the full verb set.
- The no-injection doctor against a real closed loopback port and a real fake
  executable, with the argv log read back from disk.

Inferred, NOT verified:
- **No real backend was ever asked to complete anything.** ARGO was not
  contacted, no vLLM port on poplar was contacted, the real `claude` and
  `codex` binaries were never run (not even `--version`: every CLI invocation
  in this task went to a script written into `tmp_path`), and no API key was
  read. So the ARGO and self-hosted branches of `_backend_for` are exercised
  only through construction, and `doctor`'s HTTP `ready` branch is exercised
  only against injected listings and a closed port. What a live ARGO
  `/models` payload spells its model ids as is therefore inferred from
  `HttpBackend.models()`'s documented `{"data": [...]}` handling, not measured.
- `--format parquet` is covered with pandas 3.0.6 + pyarrow 26.0.0 present; the
  ImportError branch that names the install is not exercised.
- PostgreSQL: not run at all (`KBU_COUNCIL_TEST_PG_DSN` unset and
  `store.postgres` does not exist yet). The claim that `db *` reaches it
  unchanged rests on the `open_store` dispatch and the `CouncilStore`
  protocol, not on a run.
