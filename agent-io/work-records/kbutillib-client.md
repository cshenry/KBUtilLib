# Work record: kbutillib-client

## task_id
`kbutillib-client`

## branch
`maestro/developer/kbutillib-kbdl-client`

## commit_shas
- `160aedd` — feat(external): add KBDL job service client (kbdl_service_utils)

## summary

Added `src/kbutillib/domains/external/kbdl_service_utils.py`, the
KBUtilLib-side client for the KBDL job-running service's HTTP contract
(the final task of PRD `kbdl-job-service-v0`). It defines
`KBDLServiceUtils` (a `SharedEnvUtils`-derived class) plus the
composition-based `KBDLServiceUtilsImpl` variant, following the exact
pattern already used by `bvbrc_utils.py`, `kb_uniprot_utils.py`,
`patric_ws_utils.py` and `rcsb_pdb_utils.py` (constructor kwargs
`config_file`/`token_file`/`kbase_token_file`, `Impl.__getattr__`
delegation, token pulled from `env.get_token("kbase")`). Registered as
`KBUtilLib().kbdl_service` in `toolkit.py`, and re-exported from
`kbutillib/__init__.py` and `domains/external/__init__.py` alongside its
siblings, using the same `try/except ImportError -> None` graceful-
degradation pattern.

The client covers every service feature named in the task: submit for
all five job types (`submit_genome_annotation`,
`submit_model_reconstruction`, `submit_fitness_model_analysis`,
`submit_skani`, and `upload_object` which doubles as the
`KBDLUploadObject` submission since that job type's bytes can only ever
arrive via the multipart endpoint), `list_jobs`/`check_job`/
`get_job_result`/`clear_job`, and `list_objects`/`get_object_metadata`/
`list_archive_files`/`delete_object`. It targets the tunnelled loopback
endpoint (default `http://127.0.0.1:8790`, overridable via the
`KBDL_SERVICE_URL` env var or a `base_url=` kwarg), authenticates via
`Authorization: Bearer <token>` using the same `get_token(namespace=
"kbase")` mechanism every other KBUtilLib external client uses, and
accepts no `username` parameter anywhere (verified by a signature-
introspection test) since the service derives identity from the token
alone.

Wire-contract-only: the module never imports `kbdl_service` at any
scope (source-level AST check + a runtime `sys.modules` check, both in
the test file) and re-encodes the request/response shapes itself
(envelope `{"schema_version": "1", "job_type": ..., "params": {...}}`,
error-body shapes, job-state strings) based on reading
`KBDLJobRunningPrototype`'s `src/kbdl_service/job_api.py`,
`src/kbdl_service/schemas/*.py`, `src/kbdl_service/identity.py`,
`src/kbdl_service/object_store/errors.py` and `ops/DEPLOY.md` at commit
`e0b2dda` on that repo's `main` (its `wip`, which is what its working
tree is parked on, was two commits behind and still had the job-api
placeholder — read via `git show main:<path>`, never checked out).

HTTP 400/401/404/409/413/503 are raised as distinguishable typed
exceptions (`KBDLUnsupportedSchemaVersionError`/`KBDLInvalidInputError`
for the two 400 shapes, `KBDLAuthenticationError` for 401,
`KBDLUpstreamAuthUnavailableError` for 503 — deliberately a different
type than 401, per the service's identity module — `KBDLNotFoundError`,
`KBDLConflictError`, `KBDLPayloadTooLargeError`). `poll_until_terminal`
and `submit_and_wait` give an injectable `sleep_fn`/`time_fn` (mirroring
`kbdl_service.identity.IdentityClient`'s own clock-injection pattern) so
tests never really sleep while covering the asynchronous job model.

No ACL/grant client method was added — the service exposes none
(confirmed by reading `job_api.py`'s module docstring, which explains
`ObjectStore.grant()` is not owner-gated and adding a route for it would
be a privilege-escalation hole).

No dependency was added to `pyproject.toml`; the module uses `requests`,
already a KBUtilLib dependency and the dominant HTTP library among its
external-client siblings (as opposed to `httpx`, used only by the
`domains.ai.*` Argo/curation clients). No mocking library exists in the
repo's dependencies, so the test file hand-rolls a `FakeResponse`/
`FakeSession` pair (a `requests.Session`-shaped stand-in injected via a
new `session=` constructor kwarg) rather than adding one.

## files_touched

- `src/kbutillib/domains/external/kbdl_service_utils.py` (new) — the
  client, its five typed HTTP error classes plus `KBDLJobFailedError`
  and `KBDLServiceError` base, and the `Impl` composition wrapper.
- `src/kbutillib/toolkit.py` — `TYPE_CHECKING` import,
  `self._kbdl_service = None` backing field, and the `kbdl_service`
  lazy property, placed next to the other `domains.external.*`
  properties (`patric`/`uniprot`/`pdb`).
- `src/kbutillib/__init__.py` — `try/except ImportError` re-export
  blocks for `KBDLServiceUtils` and `KBDLServiceUtilsImpl` (matching the
  sibling blocks exactly), plus both names added to `__all__` in their
  alphabetized sections.
- `src/kbutillib/domains/external/__init__.py` — `KBDLServiceUtils`/
  `KBDLServiceUtilsImpl` added to the lazy-loader `__all__` and
  `_MODULE_MAP`.
- `tests/external/test_kbdl_service_utils.py` (new) — 33 tests, all
  offline (see `tests_run` below for what they cover).
- `agent-io/work-records/kbutillib-client.md` (this file).

## success_criteria_check

- **`kbdl_service_utils.py` defines a `SharedEnvUtils`-derived class with
  an `Impl` variant, registered in `toolkit.py` following existing
  conventions** — pass. `KBDLServiceUtils(SharedEnvUtils)` +
  `KBDLServiceUtilsImpl` (composition, `env` property,
  `__getattr__` delegation), registered as `kbu.kbdl_service`; structure
  verified line-by-line against `patric_ws_utils.py` (the closest
  sibling, itself `SharedEnvUtils`-derived) while writing it.
- **Covers submit for all five job types, list/check/result/clear jobs,
  upload/list/metadata/archive-listing/delete objects** — pass. All
  eleven operations implemented; every one has a dedicated passing
  test (see `tests_run`).
- **Targets configurable loopback endpoint, default
  `http://127.0.0.1:8790`** — pass, `KBDL_SERVICE_URL` env var or
  `base_url=` kwarg, both tested.
- **Sends KBase token as Bearer header, no `username` parameter** —
  pass. `Authorization: Bearer <token>` verified by a request-recording
  test; absence of a `username` constructor parameter verified by
  `inspect.signature`.
- **Module does not import `kbdl_service`; a delivered test asserts
  this** — pass. AST-based static test (`test_module_source_never_
  imports_kbdl_service`) plus a `sys.modules` runtime check
  (`test_importing_module_does_not_import_kbdl_service`).
- **Stubbed-transport tests, no real network calls, no running service
  required, covering every listed call, poll-until-terminal at both
  completed and failed, and 400/401/404/409/413 as distinguishable
  typed errors** — pass. 33 tests, all against `FakeSession`/
  `FakeResponse`; no `requests` calls escape the fake; 503 additionally
  covered (task explicitly asked for 401-vs-503 distinguishability, which
  is a superset of the 400/401/404/409/413 list).
- **`git status` shows no file modified under
  `~/Dropbox/Projects/KBDLJobRunningPrototype` or
  `~/Dropbox/Projects/GenomeAnnotationAggregator`** — pass. Verified
  both repos' `git status --porcelain` are empty after this task's work
  (KBDLJobRunningPrototype was only read via `git show main:<path>`,
  never checked out or edited; GenomeAnnotationAggregator was never
  touched at all).

## tests_run

Full baseline (`python -m pytest tests/ -q`, 701s measured duration)
was **deliberately NOT run** — it exceeds the 600s single-Bash-call
ceiling this task operates under. This is a budget decision, not an
oversight; the task explicitly calls this out.

Scoped subset actually run (all via
`PYTHONPATH=<worktree>/src ~/VirtualEnvironments/kbu.nb-genomeannotationaggregator-py3.11/bin/python -m pytest <paths> -q`,
each well inside the 120s per-call timeout used):

1. `tests/external/test_kbdl_service_utils.py` (the new test file, 33
   tests) — **33 passed**.
2. `tests/external/ tests/guard/ tests/core/test_deprecation_shims.py tests/domains/test_external_smoke.py`
   (primary/secondary scoped selection: mirrors the changed module's
   package, plus the guard test that directly exercises the new
   `toolkit.py`/`__init__.py` entries, plus the deprecation-shim suite
   `toolkit.py` edits are adjacent to) — **178 passed, 3 failed**.
   All 3 failures are `rcsb_pdb_utils`-related
   (`ModuleNotFoundError: No module named 'aiohttp'`), matching 3 of the
   5 pre-existing-failure node ids the conductor flagged as out of
   scope (`test_all_class_entries_import_non_none`,
   `test_legacy_import_succeeds_and_warns[rcsb_pdb_utils-...]`,
   `test_legacy_reexports_are_identical_objects[rcsb_pdb_utils-...]`).
   None involve `kbdl_service_utils`.
3. `tests/core/test_cli_cap.py::TestCapRun::test_unavailable_cap_exits_nonzero`
   and
   `tests/core/test_composition_smoke.py::TestMSBiochemUtils::test_search_compounds_glucose`
   run individually (the other 2 of the 5 flagged pre-existing node
   ids) — the cli_cap one **passed** in isolation (order-dependent
   flakiness against the baseline's "failing" designation, not
   something this task changed); the composition_smoke one **errored**
   with `FileNotFoundError` for a `ModelSEEDDatabase/Biochemistry/`
   path resolved relative to this worktree's location — an
   environment/path-coupling issue pre-existing and unrelated to
   `kbdl_service_utils` (confirmed: neither test imports or touches the
   new module).
4. Also ran `tests/core/test_cli_cap.py` and
   `tests/core/test_composition_smoke.py` in full once, for context:
   4 failed / 8 errored beyond the two flagged node ids above, all
   pre-existing environment-dependent failures (missing
   `ModelSEEDDatabase` checkout adjacent to this worktree, `cli_cap`
   JSON-output tests) with no connection to this task's diff.

**`test_all_class_entries_import_non_none` investigation (the guard
test explicitly called out)**: at base it already fails, listing
`RCSBPDBUtils`/`RCSBPDBUtilsImpl` as `None` because `aiohttp` is not
importable in this environment. After this task's change, it **still
fails for exactly that same reason** — `KBDLServiceUtils` and
`KBDLServiceUtilsImpl` are absent from the failure list, i.e. they
import to real (non-`None`) objects. This is a **pre-existing cause,
unchanged by this task**, not a new one: `kbdl_service_utils.py` only
imports `requests` (already installed) plus KBUtilLib's own
`SharedEnvUtils`, so its `try/except ImportError` re-export in
`kbutillib/__init__.py` never trips.

`ruff check` and `ruff format` were run against every touched/added
file; `ruff check` reported no issues, and `ruff format` was applied to
the two new files (`kbdl_service_utils.py`,
`test_kbdl_service_utils.py`) only — the three pre-existing files
(`toolkit.py`, `__init__.py` x2) were left as-is since they are not
themselves `ruff format`-clean throughout, and reformatting them
wholesale would have violated "don't reformat unrelated code"; the few
lines added there match the surrounding (unformatted) style of their
sibling property blocks.

## caveats

- **`session=` constructor kwarg is new relative to sibling clients.**
  None of `bvbrc_utils`/`kb_uniprot_utils`/`patric_ws_utils`/
  `rcsb_pdb_utils` accept an injectable transport; this repo has no
  HTTP-mocking dependency (no `requests-mock`/`responses`/`respx`), so
  making the session injectable was the least-invasive way to satisfy
  "stubbed HTTP transport, no real network calls" without adding a new
  dependency. Default behavior (no `session=` passed) is unchanged: a
  plain `requests.Session()`, exactly like `bvbrc_utils.BVBRCUtils`.
- **`upload_object` is the only path to submitting a `KBDLUploadObject`
  job.** The task's SURFACE list mentions "submit ... KBDLUploadObject"
  and "upload object (multipart)" as two separate bullets, but per the
  service's own `job_api.py`, `KBDLUploadObject`'s params
  (`object_type`/`name`/`visibility`) carry no bytes — the actual
  content only ever arrives via the multipart endpoint, and the service
  itself has no other path to submitting that job type with real
  content. Implementing a second, JSON-only "submit_upload_object"
  method would have been dead code that could never succeed against the
  real service, so this was resolved by judgment: one method,
  documented as serving both roles.
- **Top-level `kbutillib/__init__.py` / `domains/external/__init__.py`
  registration** was not explicitly named as a success criterion (only
  `toolkit.py` was), but every existing external-domain sibling is
  registered in all three places, so this was added for convention
  consistency. If the reviewer considers that out of scope, it is
  additive and easily dropped without touching the `toolkit.py`
  registration the criteria do require.
- The five job-type submit methods and `upload_object` were built
  against the schemas at `KBDLJobRunningPrototype` commit `e0b2dda`
  (its `main`, not its parked `wip`). If that repo's contract changes
  after this commit, this client's request/response shapes will need a
  manual diff against the new schemas — there is no automated contract
  check between the two repos by design (independent deployability).
