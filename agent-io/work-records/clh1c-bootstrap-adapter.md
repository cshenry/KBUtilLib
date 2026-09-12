# Work record: clh1c-bootstrap-adapter

## task_id
clh1c-bootstrap-adapter (PRD: clearinghouse-lake-1c-pod-operator)

## branch
maestro/developer/clh1c-bootstrap-adapter

## commit_shas
- 9c4f50d440728bc0cd8b73495219e1c8bbe1f2f4 — feat: add ClearinghouseBootstrapCapability, the real bootstrap() adapter

## summary
`clearinghouse_schema.bootstrap()` is the sanctioned, idempotent table-creation
entry point for the three clearinghouse Iceberg tables, but it requires its
injected `capability` to expose `table_exists()` and `table_partition_spec()`
in addition to `load()`. The real `BerdlCapability` implements only `load()`,
so `bootstrap()` has never been runnable against anything but a test fake.
This change adds `ClearinghouseBootstrapCapability` in a new module,
`src/kbutillib/domains/kbase/berdl/clearinghouse_bootstrap_adapter.py`, which
wraps (does not subclass) `BerdlCapability` and supplies the missing two
read-only probes without touching `capability.py`, `transports.py`, or
`clearinghouse_schema.py`. `table_exists` delegates to
`transport.table_exists(spark, name, namespace=namespace)` — the identical
probe `BerdlCapability.load()` itself uses — and the adapter resolves exactly
one Spark session, shared between that probe and the session forwarded to the
wrapped `load()` call as `spark=`, so the guard and the write can never
disagree about which session's view of the catalog they're using.
`table_partition_spec` makes exactly one
`capability.query(sql, engine="spark")` call (DESCRIBE TABLE EXTENDED) and
hands the result to a pure dispatch that tries two recognised catalog output
shapes (a DESCRIBE TABLE EXTENDED partitioning block, or — as a fallback
shape the dispatch also recognises — a SHOW CREATE TABLE PARTITIONED BY
clause) before raising a new module-level `PartitionSpecUnparseableError` on
anything else, including a recognised-but-transform-bearing spec (e.g.
`bucket(256, entity_hash)`). It never returns `[]`/`None` as a fallback for
unparseable input, since `bootstrap()` treats both as "unpartitioned" and a
silent `[]` would defeat the one safety check `bootstrap()` exists to
provide (the `canonical_content` trap called out in the task prompt).
Everything besides the single `query()` call is a pure, module-level
function (SQL emitters, parsers, a paren-depth-aware top-level comma
splitter for `PARTITIONED BY (...)`, and the dispatch), so the module
imports and is fully testable off-pod.

## files_touched
- `src/kbutillib/domains/kbase/berdl/clearinghouse_bootstrap_adapter.py` (new)
- `tests/berdl/test_clearinghouse_bootstrap_adapter.py` (new)
- `agent-io/work-records/clh1c-bootstrap-adapter.md` (new, this file)

## success_criteria_check
- **New module defines `ClearinghouseBootstrapCapability`, wraps (not subclasses) `BerdlCapability`, exposes exactly `load()`/`table_exists(name, namespace=...)`/`table_partition_spec(name, namespace=...)` matching `_FakeCapability`'s signatures** — pass. Verified by `TestAdapterSatisfiesBootstrapContract::test_bootstrap_accepts_adapter_without_attributeerror`, which drives the adapter through the real `bootstrap()`. The class has no other public methods; `_get_transport`/`_get_spark` are underscore-private helpers.
- **`clearinghouse_schema.py`, `capability.py`, `transports.py` unmodified** — pass. `git diff --stat` against `main` shows only the two new files plus this work-record; `git status --porcelain` at the end of the task shows no other modified files.
- **`table_exists` delegates to the transport's `table_exists` rather than a hand-rolled query; a test asserts the Spark session used for the probe is the identical object forwarded to `load()` as `spark=`** — pass. `TestSessionIdentity::test_table_exists_and_load_share_the_same_spark_session` asserts `transport.table_exists_calls[0][0] is session` and `underlying.load_calls[0]["spark"] is session`, and that `spark_session()` was resolved exactly once.
- **Exceptions from `table_exists` propagate rather than converting to `False`; a test shows `bootstrap()` turning that into `BootstrapIndeterminateStateError` with no table written** — pass. `TestTableExistsExceptionPropagation` covers both the adapter-level propagation and the `bootstrap()`-level conversion, asserting `underlying.load_calls == []`.
- **`table_partition_spec` raises on unrecognised/unparseable output; a test asserts it does not return `[]`/`None` in that case; a separate test asserts `[]` only for a positively-unpartitioned table** — pass. `TestDispatchNeverFallsBackToEmpty` and `TestPositivelyUnpartitioned` cover this at the dispatch level; `TestDescribeExtendedParser`/`TestShowCreateTableParser` cover it per-parser.
- **Parsers exist for both DESCRIBE TABLE EXTENDED and SHOW CREATE TABLE, each pure, each covered by a test asserting a two-column spec in order** — pass. `test_two_column_spec_is_returned_in_declared_order` in both parser test classes asserts `== ["source", "entity_type"]` and explicitly `!= ["entity_type", "source"]`.
- **A transform expression (e.g. `bucket(256, entity_hash)`) in a parsed spec raises** — pass. Covered for both shapes (`test_transform_expression_raises` in each parser test class) and at the dispatch level (`TestTransformRaisesThroughDispatch`).
- **Every `capability.query()` call passes `engine="spark"`, asserted by a test** — pass. `TestEngineSparkOnEveryQuery::test_table_partition_spec_passes_engine_spark` asserts `kwargs.get("engine") == "spark"`; there is exactly one `query()` call site in the whole module.
- **SQL emitters and parsers are pure module-level functions performing no I/O; the module imports cleanly off-pod** — pass. Verified directly: `python -c "import kbutillib.domains.kbase.berdl.clearinghouse_bootstrap_adapter"` succeeds off-pod (no pyspark installed in this venv, no pod env vars set). `TestSqlEmitters` covers the emitter string shapes.
- **No live cluster contacted, no table created** — pass. Every test uses in-process fakes (`_FakeTransport`, `_FakeUnderlyingCapability`); no network calls, no `berdl_notebook_utils`, no `pyiceberg` import anywhere in the new module.
- **No test that passed on the base commit fails on the branch** — pass, see `tests_run` below: full-suite counts match the baseline exactly aside from the 30 new adapter tests, all passing.

## tests_run
- `/Users/chenry/VirtualEnvironments/KBUtilLib-py3.13/bin/python -m pytest tests/berdl/test_clearinghouse_bootstrap_adapter.py -q` — **30 passed**.
- `/Users/chenry/VirtualEnvironments/KBUtilLib-py3.13/bin/python -m pytest tests/berdl/ -q` — **171 passed** (fast scoped run requested by the task envelope).
- `/Users/chenry/VirtualEnvironments/KBUtilLib-py3.13/bin/python -m pytest tests/ -q --continue-on-collection-errors` (the repo's documented baseline command, timeout 600000ms) — **1 failed, 2899 passed, 401 skipped, 6 errors in 206.80s**. Compared against the conductor-supplied baseline (2869 passed, 1 failed, 6 errors, 401 skipped): the delta is exactly +30 passed (this task's new tests), with the same single failure (`tests/modeling/test_ms_reconstruction_utils.py::test_default_mode_uses_db_fallback`) and the same six collection/errors (`tests/biochem/test_escher_utils.py`, `tests/notebook/helpers`, and the four `tests/modeling/test_comprehensive_gapfill_wrapper.py` cases) — all pre-existing per the task envelope's `failed_node_ids` list, none touched by this change.

## caveats
- Neither catalog output shape (DESCRIBE TABLE EXTENDED's partitioning block,
  or SHOW CREATE TABLE's PARTITIONED BY clause) has been verified against a
  live BERDL/Iceberg-on-Polaris cluster — this is explicitly out of scope
  per the task prompt ("DO NOT ... run anything against a live cluster") and
  matches the same stated limitation already present in
  `clearinghouse_schema.py`'s module-level docstring for `bootstrap()`
  itself. The adapter's dispatch tries both shapes on the single query
  result so it degrades gracefully if the live shape differs from either
  expectation (raising `PartitionSpecUnparseableError` rather than silently
  misreporting), but which shape (if either, verbatim) the live catalog
  actually returns is an in-pod fact this task cannot establish.
- `table_partition_spec` issues exactly one query using the
  `DESCRIBE TABLE EXTENDED` SQL text (never `SHOW CREATE TABLE`) — the
  dispatch's SHOW CREATE TABLE parser is exercised by unit tests directly
  and reachable in principle should a future caller route different query
  text through the same dispatch, but under the adapter's current
  `table_partition_spec` implementation it is dead code on the one path
  actually wired up today. This was a judgment call: the task explicitly
  asked for both emitters and both parsers to exist and be tested, but
  specified "one `capability.query(...)` call" per `table_partition_spec`
  invocation, which rules out trying both SQL texts in sequence.
- Ruff/mypy were not run — this venv (`KBUtilLib-py3.13`) has no `ruff`
  module installed, and the task's baseline command is the pytest
  invocation only, so this was not treated as a verification gap.
