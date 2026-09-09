# Work record: clh1-operator-runbook

## task_id
clh1-operator-runbook

## branch
maestro/developer/clh1-operator-runbook

## commit_shas
- 86a7ab0da0ba838130f5c36922b52788a7cc5ee6 — docs: add clearinghouse operator runbook and OP3 parity check

## summary

Added the human-operator runbook and the pod-only parity check for the
`clearinghouse-lake-1-schema` PRD's three Iceberg tables, which live inside
the BERDL JupyterHub pod (`kbhub`) and cannot be reached by any automated
build agent (`BerdlCapability.load()` refuses off-pod by design, and
`kbhub` is not a headless worker host).

`agent-io/docs/clearinghouse-schema-operator-runbook.md` documents OP1
(the blocking three-package import verification, naming the known
`~/venvs/kbu-modeling` / `/opt/conda` gap and inventing no version pins or
install commands), OP2 (dry-run-first `bootstrap()`, the capability seam
`bootstrap()` requires that the stock `BerdlCapability` does not
implement — `table_exists`/`table_partition_spec` — and what the operator
has to build to supply it, the namespace-resolution warning tied to
append-to-overwrite promotion, and a new acceptance step verifying the
live `entity_hash` column is genuinely `BINARY` and not `STRING` before
any corpus load), and OP3 (running the parity check and recording its
result), plus an "if something goes wrong" section covering the
partition-spec-refusal trap and why a direct `pyiceberg` write must never
be used to route around a failure.

`src/kbutillib/domains/kbase/berdl/clearinghouse_parity_fixture.py` is a
new, pure, off-pod-safe module holding the six-property fixture (row data
only — one `ParityCase` per property: duplicate collapse, newest-wins,
ingest_batch_id tie-break, term removal, source isolation, and
result_type_version outside the slot key). Every fixture row's `source`
carries the `parity-check/` prefix so real writes into the live table can
never be mistaken for real annotations. `tests/berdl/test_clearinghouse_derivation.py`
was refactored (row-data sourcing only — test names, count, and assertions
are otherwise unchanged) to import its six properties' rows from this
shared module instead of re-typing them inline, so the DuckDB surrogate
tests and the in-pod parity check draw from one fixture rather than two
that can silently drift apart.

`scripts/clearinghouse_parity_check.py` is the OP3 parity script: it
imports the same shared fixture, builds an explicitly-typed Spark
DataFrame from it, appends the rows to the real `result` table through
the sanctioned `BerdlCapability.load()` write path (never a raw
`pyiceberg` write), runs the real `current_state_sql()` SQL through Spark,
and asserts the same six properties the DuckDB tests assert, printing one
PASS/FAIL line per property plus a final summary. Every pod-only import
(`pyspark`, `InPodTransport`) is deferred inside functions, so importing
the module and calling `main()` are both safe off-pod: `main()` detects
`locus() != "in_pod"` and exits 2 with a clear message rather than raising
a raw `ModuleNotFoundError` deep in a stack.

## files_touched

- `agent-io/docs/clearinghouse-schema-operator-runbook.md` (new)
- `scripts/clearinghouse_parity_check.py` (new)
- `src/kbutillib/domains/kbase/berdl/clearinghouse_parity_fixture.py` (new)
- `tests/berdl/test_clearinghouse_derivation.py` (modified — row data now
  sourced from the shared fixture module; `_hash()` helper removed as
  dead code once its only callers were replaced)

## success_criteria_check

- **Runbook exists at the required path, documents OP1/OP2/OP3 in order,
  marks OP1 as blocking, names the exact `~/venvs/kbu-modeling`/`/opt/conda`
  gap** — pass. See the "OP1" section; no version pins or install commands
  are invented, per the CONFRONT-ROUND-1 instruction.
- **Parity-check script exists, exercises `current_state_sql()` against the
  real engine, asserts the same six properties as the DuckDB tests, shares
  the fixture rather than duplicating it** — pass. `scripts/clearinghouse_parity_check.py`
  imports `PARITY_CASES`/`ALL_PARITY_ROWS` from `clearinghouse_parity_fixture.py`,
  the same module `test_clearinghouse_derivation.py` now imports from.
- **Parity fixture confined to an obviously-synthetic source prefix** —
  pass. Every row's `source` is built via `fixture_source(...)`, which
  prepends `PARITY_SOURCE_PREFIX = "parity-check/"`.
- **Runbook states DuckDB tests prove logic only, bootstrap tests fake the
  boundary** — pass. Stated explicitly in the runbook's opening section
  ("Be honest about what has and hasn't been proven before you start").
- **Deliverables importable/parseable off-pod; nothing attempted a live pod
  call** — pass. `py_compile` and a live off-pod import of both new
  modules succeeded (see tests_run below); `clearinghouse_parity_check.py`'s
  `main()` was invoked off-pod and correctly short-circuited with exit code
  2 (`locus() != "in_pod"`) rather than attempting any pod call — this was
  a verification of the fail-fast path, not an attempt to reach the pod.
- **No test that passed on base fails on branch** — pass. Full suite:
  2878 passed / 1 failed / 4 errors / 380 skipped, identical to the stated
  baseline; the 1 failure and 4 errors are exactly the five pre-existing,
  named-as-not-mine node IDs.
- **OP2 documents dry-run-first, an `entity_hash` BINARY-vs-STRING
  acceptance step, and the namespace-resolution/append-to-overwrite
  warning** — pass. See OP2.1 (dry run first, namespace-resolution
  warning), OP2.3 (the acceptance step).
- **OP1 names the known environment gap with a verification one-liner,
  invents no version pins or install commands** — pass. The one-liner
  imports all three packages and prints `__version__`/`__file__`; the
  choice of remedy is explicitly left to the operator.

## tests_run

- `ruff check scripts/ src/kbutillib/domains/kbase/berdl/ tests/berdl/` —
  pass, no findings.
- `python -m py_compile scripts/clearinghouse_parity_check.py
  src/kbutillib/domains/kbase/berdl/clearinghouse_parity_fixture.py` —
  pass.
- Off-pod import smoke test: imported `clearinghouse_parity_fixture`
  (confirmed 6 `PARITY_CASES`, 13 `ALL_PARITY_ROWS`) and
  `clearinghouse_parity_check` as a module, then called `main()` off-pod —
  it printed the expected "must run inside the BERDL JupyterHub pod"
  message and returned 2, confirming the fail-fast path without any
  attempt to reach the pod.
- `pytest tests/berdl/ -q` — 128 passed (targeted scoped run, before the
  full-suite run, to validate the refactored derivation tests in
  isolation).
- Full baseline command, exactly as specified:
  `/Users/chenry/VirtualEnvironments/kbutillib-conductor-baseline/bin/python
  -m pytest tests/ -q --ignore=tests/biochem/test_escher_utils.py
  --ignore=tests/notebook/helpers` — **before** (stated baseline on
  `a0e7245`): 2878 passed, 1 failed, 4 errors, 380 skipped. **After** (this
  branch, commit 86a7ab0): 2878 passed, 1 failed, 4 errors, 380 skipped —
  identical counts, and the failing/erroring node IDs match the
  pre-existing list exactly (`test_ms_reconstruction_utils.py::test_default_mode_uses_db_fallback`,
  and the four `test_comprehensive_gapfill_wrapper.py` collection errors).
  Duration: 250.67s, within the stated ~249.9s baseline and the 600000ms
  timeout budget.

## caveats

- **`tests/berdl/test_clearinghouse_derivation.py` was refactored, not
  left untouched.** The task's own spec required the parity script to
  share "the same fixture the DuckDB tests use," which by construction
  means the DuckDB test file had to start importing from a shared module
  rather than the reviewer independently trusting that two hand-typed
  fixtures matched. The refactor changed only how each test method sources
  its row data (`case.rows` from the shared module instead of inline
  dict literals) and, in `TestIngestBatchIdTieBreak`, how the "expected
  winner" is expressed (`max(case.rows, key=...)` instead of a hardcoded
  literal) — test names, test count (10, unchanged), and every assertion's
  meaning are preserved. Full-suite results before/after are identical,
  and `tests/berdl/` alone was also run in isolation (128 passed) to
  double-check this specific file.
- **OP2.0's capability adapter is left as an operator responsibility, by
  design, not an oversight.** The task's own "additional context" section
  confirmed this is accepted scope: `bootstrap()` needs `table_exists`/
  `table_partition_spec` on its injected capability, neither exists on the
  real `BerdlCapability`, and building a real adapter (most plausibly via
  Iceberg/Spark catalog metadata queries) is exactly the kind of thing
  that cannot be verified off-pod. The runbook names this gap plainly
  (OP2.0) and gives the operator concrete starting points
  (`InPodTransport.table_exists`, `DESCRIBE`/`SHOW CREATE TABLE`-style
  catalog introspection for the partition-spec reader) without inventing
  a fabricated implementation that has never run against a real pod.
- **`RESULT_TABLE_FQN` in the parity script has a documented, unresolved
  ambiguity.** `clearinghouse_derivation.py`'s own docstring example uses
  a three-part, tenant-qualified FQN
  (`"kbaseincubator.clearinghouse.result"`), but `BerdlCapability.load()`'s
  own postflight row-count/history queries use a two-part form
  (`namespace.table`, no tenant segment). Which one actually resolves
  against the live Iceberg catalog is not verifiable off-pod. The script
  defaults to the three-part form (matching the derivation module's own
  worked example) with an inline comment and a matching runbook note
  telling the operator to try the two-part form next if the first doesn't
  resolve, and to record which one worked. This is disclosed rather than
  guessed at as settled fact.
- **Not run against the pod, as instructed.** No step in this task
  attempted a live pod call; the only in-pod-path exercise performed was
  confirming the script's off-pod fail-fast behavior (exit 2, clear
  message) rather than a raw import crash.
- The worktree's shared conductor-baseline venv
  (`/Users/chenry/VirtualEnvironments/kbutillib-conductor-baseline`) was
  repointed to this worktree via `pip install -e . --no-deps` before any
  test run, per the task's instructions; no other worktree's editable
  install was touched.
