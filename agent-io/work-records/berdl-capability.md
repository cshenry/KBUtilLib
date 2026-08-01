# Work Record: berdl-capability

## task_id
berdl-capability

## branch
conductor/berdl-lakehouse-skills/berdl-capability

## commit_shas
- fd618b0cf78a57daf1cf19400b0e9b9d12dd6275 — feat(berdl): BerdlCapability deep module (locus, databases, memberships, load, query)

## summary
Adds `capability.py` to the `berdl` subpackage, implementing `BerdlCapability`
— the deep module described in `agent-io/prds/berdl-lakehouse-skills/fullprompt.md`
— on top of the already-existing `naming.py`, `membership.py`, `tokens.py`,
and `transports.py`. `locus()` detects execution locus by testing
**importability** of `berdl_notebook_utils` via `importlib.util.find_spec`
(not by inspecting the pod's environment variables, which can be present
without the package actually being installed). `databases()` and
`memberships()` delegate to whichever transport `locus()` selects;
`memberships()` checks locus *before* constructing a transport so it raises
a clear `BerdlMembershipUnavailableError` off-pod without depending on
off-pod token availability. `load()` is in-pod only: it runs a membership
and per-table existence preflight, resolves write mode via the new
`select_write_mode()` helper (append-to-nonexistent becomes overwrite, so a
re-runnable load does not fail on first execution), builds the ingest
config via the new `build_ingest_config()` helper (DataFrame mode omits
`paths` entirely; bronze mode requires and includes `paths.bronze_base`),
routes every write through `data_lakehouse_ingest.ingest` (never a raw
`writeTo`), and runs a row-count/snapshot-history postflight. Off-pod,
`load()` raises `BerdlLoadRefusedError` before touching any transport,
staging any data, or importing `data_lakehouse_ingest` — the message names
the reason (a Spark session, which only exists in the pod), the target
machine (`kbhub`), and a concrete runnable command. `query()` routes to
Trino (with the connector explicit — `'iceberg'`, never relying on
`get_trino_connection`'s legacy `'delta_lake'` default) or Spark in-pod,
and to the REST transport off-pod.

The two pure helper functions (`build_ingest_config`, `select_write_mode`)
are the load-bearing logic and are unit-tested directly, independent of any
transport, per the PRD's "Testing Decisions" section.

## files_touched
- `src/kbutillib/domains/kbase/berdl/capability.py` — new: `BerdlCapability`, `BerdlLoadRefusedError`, `BerdlMembershipUnavailableError`, `berdl_notebook_utils_importable()`, `build_ingest_config()`, `select_write_mode()`
- `src/kbutillib/domains/kbase/berdl/__init__.py` — re-export the new capability-layer symbols; docstring updated to describe `capability.py`
- `tests/berdl/test_capability.py` — new: pure-logic unit tests (see below)

## success_criteria_check

- **`capability.py` defines `BerdlCapability` exposing `locus`, `databases`, `memberships`, `load`, `query`** — PASS. All five are public methods on the class.
- **Unit tests cover ingest-config construction in DataFrame mode omitting `paths`, and bronze mode including `paths.bronze_base`** — PASS. `TestBuildIngestConfig` has `test_dataframe_mode_omits_paths`, `test_dataframe_mode_drops_paths_even_if_caller_passed_one` (also covers the case where a caller mistakenly supplies `paths` in DataFrame mode — it is dropped, not merged), `test_bronze_mode_includes_paths_bronze_base`, plus negative tests for missing `paths`/`bronze_base` in bronze mode.
- **Unit tests cover selection of `overwrite` rather than `append` when the target table does not exist** — PASS. `TestSelectWriteMode.test_append_to_nonexistent_table_becomes_overwrite`, plus companion tests confirming `append` stays `append` when the table exists and `overwrite` is unaffected by existence either way.
- **Unit tests cover off-pod `load()` raising an error whose message names the pod requirement, the `kbhub` machine, and a concrete command** — PASS. `TestLoadOffPod` asserts `BerdlLoadRefusedError` is raised, that the message contains `"spark"` and `"pod"` (case-insensitive), that it contains the literal string `"kbhub"` (via the `POD_MACHINE` constant, asserted both ways), and that it contains a concrete runnable command (`"BerdlCapability"` and `"load("` present in the message body, which is a real `python -c "..."` one-liner).
- **`locus()` is shown to test importability of `berdl_notebook_utils` rather than environment variables alone** — PASS. `test_locus_reports_off_pod_when_package_not_importable` sets all three pod environment variables (`KBASE_AUTH_TOKEN`, `SPARK_CONNECT_URL`, `S3_ACCESS_KEY`) to fake values and asserts `locus()` still reports `'off_pod'`, because the package genuinely is not importable in this environment.
- **No write path bypasses `data_lakehouse_ingest.ingest`** — PASS by inspection: `load()` is the only method that writes, and its only call into the pod package is `from data_lakehouse_ingest import ingest` followed by a single `ingest(...)` call; no `writeTo` call appears anywhere in the module.
- **Tests make no network calls and the full suite passes** — PASS for `tests/berdl/` (61 passed, 1.05s, no network — see `tests_run` below). Per the mandatory process rules for this task, the full repository suite was intentionally **not** run (it is 15 minutes and separately characterized); the task bar is `tests/berdl/` passing with no new failures, which is met.

## tests_run

```
python3 -m pytest tests/berdl/ -q
61 passed in 1.05s
```

Also ran, narrowly, before and after implementation to confirm no regression
in the modules this one imports from:
```
ruff check src/kbutillib/domains/kbase/berdl/capability.py src/kbutillib/domains/kbase/berdl/__init__.py tests/berdl/test_capability.py
All checks passed!
ruff format src/kbutillib/domains/kbase/berdl/capability.py src/kbutillib/domains/kbase/berdl/__init__.py tests/berdl/test_capability.py
2 files reformatted, 1 file left unchanged
```

The full `pytest` suite was deliberately **not** run, per this task's
explicit process rules (measured baseline on `main`: 77 failed / 2773 passed
/ 2 errors, none in `berdl`; re-running it risks losing the worktree to a
background-timeout race that has already bitten two prior developers on this
PRD).

## caveats
- `load()`'s postflight (row-count + snapshot-history verification) and its
  in-pod preflight (`transport.table_exists`, `transport.spark_session`,
  `self.memberships()`) are written against the PRD and the harvested
  `api-reference.md` signatures, but — like `transports.py` before it — are
  **not exercised by any test**, because they require a live Spark session
  inside the pod. This mirrors the PRD's own scope note ("Not tested
  automatically: the two transports... require live BERDL") extended to the
  in-pod branches of `capability.py` that call into those transports.
- `query()`'s in-pod routing (Trino with an explicit `'iceberg'` connector,
  or Spark via an `engine='spark'` kwarg) is implemented per the PRD's "Read
  path" section but is likewise untested here for the same reason — no
  Spark/Trino connection is available off-pod, and the PRD's testing
  decisions do not call for a mocked-transport layer.
- `load()`'s public signature accepts a list of table dicts (matching the
  `ingest` config's own `'tables'` list) rather than being restricted to
  exactly one table per call, so a single call can create/append/replace
  several tables of one dataset in one `ingest` invocation. This was a
  judgment call: the PRD's interface line for `load()` is `load(...)` with
  no signature given, and the config schema itself is inherently
  multi-table (`'tables': [...]`), so a single-table-only method would have
  forced an artificial one-call-per-table restriction not implied anywhere
  else in the PRD.
- `memberships()` deliberately checks `self.locus()` before constructing any
  transport (rather than constructing the transport and checking its type),
  specifically so the off-pod refusal never depends on whether an off-pod
  BERDL token happens to be configured on the calling machine. An earlier
  draft did it the other way and turned out to be non-deterministic
  depending on local `~/.kbase/token` state; the fix (and the reasoning) is
  captured in the method's docstring.
- The existing `KBBERDLUtils` class was not touched, per the PRD's "Out of
  Scope" list.
