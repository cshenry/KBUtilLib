# clh1-idempotent-bootstrap

## task_id
clh1-idempotent-bootstrap (PRD `clearinghouse-lake-1-schema`)

## branch
maestro/developer/clh1-idempotent-bootstrap

## commit_shas
- 20f7ac4be0dd1323b2fdd293c22dc807d4369ab9 — feat: add idempotent
  bootstrap for clearinghouse Iceberg tables

This branch has exactly one commit.

## summary

Added `bootstrap()` to `src/kbutillib/domains/kbase/berdl/clearinghouse_schema.py`,
an idempotent create-or-verify entry point for the three clearinghouse
Iceberg tables (`entity`, `canonical_content`, `result`). It takes an
injected `capability` object (duck-typed, not the concrete
`BerdlCapability` class) plus a required, explicit `namespace` parameter,
and:

- Never requests `'overwrite'` itself. It always requests `'append'` in
  the table dicts it hands to `capability.load()`, relying on `load()`'s
  own `select_write_mode()` to promote `'append'` to `'overwrite'` only
  when it independently confirms a table does not yet exist (first
  creation).
- Before writing to a table it has independently confirmed already
  exists, compares this module's `partition_by` config against the live
  table's actual partition spec (via a new required capability method,
  `table_partition_spec`) and refuses with `BootstrapPartitionSpecMismatchError`
  if they disagree, naming both values and directing the operator to
  rebuild by replay rather than attempting live partition-spec evolution.
  A mismatch on any one table blocks the whole batch -- `load()` is never
  called for any table in that `bootstrap()` invocation.
- Treats any exception raised by the existence check (`table_exists`) or
  the partition-spec lookup (`table_partition_spec`) as "state could not
  be positively determined" and refuses (`BootstrapIndeterminateStateError`)
  rather than falling through to a default-absent assumption. In `dry_run`
  mode this is reported (`exists: None`, `action: 'refuse'`) instead of
  raised, so a dry-run preview covers every table rather than stopping at
  the first indeterminate one.
- Lets `BerdlLoadRefusedError` from an off-pod `capability.load()` call
  propagate completely untouched -- no catch, no re-wrap, no fallback
  write path, and the module imports nothing from `pyiceberg` anywhere.
- Supports `dry_run=True`, which runs every read-only check but never
  calls `capability.load()`; each table's report carries `action` in
  `{'create', 'append', 'refuse'}`, and every `'create'` entry (table not
  found to exist under the given namespace) carries a
  `namespace_warning` string flagging that this may indicate a
  namespace-resolution mistake rather than genuine first-creation.
- Never defaults `namespace` to `BerdlCapability.load()`'s `"default"` --
  `namespace` is a required keyword argument with no default, and is
  passed through verbatim and unchanged to every call (`table_exists`,
  `table_partition_spec`, `load`). No tenant-qualified/`my.`-prefix
  namespace-resolution rule is hardcoded; the caller decides.

**Why this required a new, currently-unimplemented capability contract.**
`BerdlCapability.load()` (existing, unmodified) only reports whether a
table existed *after* it has already written -- calling it is itself the
write, so it cannot serve as a read-only pre-write probe. The
partition-spec guard therefore needs its own read-only lookups *before*
`load()` is ever called. `capability.py`'s public surface today
(`load()`, `query()`, `databases()`, `memberships()`, `locus()`) has no
such lookup, so `bootstrap()` requires its injected `capability` to also
implement `table_exists(name, namespace=...)` and
`table_partition_spec(name, namespace=...)` -- methods that do **not**
exist on the real `BerdlCapability` class as of this commit. This is
documented at the top of the "Idempotent bootstrap" section in
`clearinghouse_schema.py` and restated below under caveats.

## files_touched

- `src/kbutillib/domains/kbase/berdl/clearinghouse_schema.py` — added
  `bootstrap()`, `BootstrapIndeterminateStateError`,
  `BootstrapPartitionSpecMismatchError`, `_normalize_partition_columns()`,
  and the `_NAMESPACE_RESOLUTION_WARNING` constant. No existing code in
  this file was modified beyond adding a `Mapping, Sequence` import.
- `tests/berdl/test_clearinghouse_bootstrap.py` — new file, 24 tests
  against a hand-built fake capability (`_FakeCapability`).

Not touched (per the parallel-lane instructions):
`clearinghouse_derivation.py`, `berdl/__init__.py`, `pyproject.toml`,
`tests/berdl/test_clearinghouse_schema.py`, `capability.py`,
`transports.py`.

## success_criteria_check

- **bootstrap() accepts an injected capability** — pass. `bootstrap(capability, *, namespace, ...)`; every test in the new file passes a hand-built fake, never a real `BerdlCapability`.
- **Second run against existing tables requests 'overwrite' for none of them** — pass. `TestSecondRunNeverRequestsOverwrite` asserts every table's requested mode in the `load()` call is `'append'`, both against a pre-populated fake and across two back-to-back `bootstrap()` calls.
- **Partition-spec mismatch causes a refusal naming both expected and actual spec** — pass. `TestPartitionSpecGuard::test_mismatch_against_live_table_refuses_and_names_both_specs` asserts both `['source']` (expected) and `['standardizer_version']` (actual) appear in the raised message.
- **BerdlLoadRefusedError from an off-pod capability propagates, no fallback write attempted, no pyiceberg import anywhere** — pass. `TestOffPodRefusalPropagates` asserts the exact fake-raised error (with its message) propagates unmodified; `grep -n pyiceberg clearinghouse_schema.py` returns nothing (verified).
- **Dry-run performs no write** — pass. Every `TestDryRun` test asserts `cap.load_calls == []`; `test_dry_run_performs_no_write_of_any_kind` checks this across a mixed batch (one creatable, one matching-existing, one mismatched table).
- **Each of those five behaviors has a passing test** — pass, see above.
- **Work record states the faked boundary leaves the live path unverified** — pass, see the "Limitation" section below and this file's own docstring note in `test_clearinghouse_bootstrap.py`.
- **No test that passed on base fails on branch** — pass, see Tests below (2858 passed vs. baseline's 2834; same 1 failed / 4 errors, all in the documented pre-existing set).
- **Explicit namespace parameter, passed on every call, never relies on load()'s "default"** — pass. `namespace` is a required keyword-only argument (`ValueError` if empty); `TestNoHardcodedNamespaceRule` and `TestFirstRunCreatesAllThree::test_explicit_namespace_is_passed_to_every_call` confirm it reaches `table_exists`, `table_partition_spec`, and `load()` verbatim.
- **Existence check that raises or is ambiguous causes a refusal rather than an overwrite, with its own test** — pass. `TestIndeterminateExistenceRefuses` (three tests: existence-raises, blocks-whole-batch, partition-spec-lookup-raises).
- **No namespace-resolution rule is hardcoded** — pass. `bootstrap()` never inspects or transforms the `namespace` string; `test_namespace_is_passed_through_verbatim_never_transformed` uses a deliberately unusual value (`"my.kbaseincubator.clearinghouse"`) and confirms it reaches every call unchanged.
- **Dry-run flags an unexpected 'would create' as a namespace-resolution warning** — pass. `test_dry_run_would_create_carries_a_namespace_resolution_warning` asserts every `'create'`-action dry-run entry carries a `namespace_warning` string mentioning "namespace"; `test_namespace_warning_absent_on_a_real_run` confirms the field is absent (not just falsy) outside dry-run.

## tests_run

Baseline command (exact, as specified):
```
/Users/chenry/VirtualEnvironments/kbutillib-conductor-baseline/bin/python -m pytest tests/ -q --ignore=tests/biochem/test_escher_utils.py --ignore=tests/notebook/helpers
```

- Before (measured baseline on base commit `b0f145a81d65ae20d96748e10e7a0be6f8e83e5c`, per task envelope, not re-measured by me): 2834 passed, 1 failed, 4 errors, 380 skipped.
- After (measured on this branch, full run, 254.92s): **2858 passed, 1 failed, 4 errors, 380 skipped**.
- Delta: +24 passed (exactly the 24 new tests in `tests/berdl/test_clearinghouse_bootstrap.py`), 0 change in failed/errored/skipped counts.
- The 1 failed and 4 errors are the exact pre-existing, documented set (`test_default_mode_uses_db_fallback`, the four `test_comprehensive_gapfill_wrapper.py` cases) — confirmed by name in this run's output; none of these touch `berdl`/`clearinghouse` code.
- Also separately ran, for faster local iteration: `pytest tests/berdl/test_clearinghouse_bootstrap.py tests/berdl/test_clearinghouse_schema.py tests/berdl/test_capability.py -q` → 69 passed, 0 failed.

Venv confirmation: `python -c "import kbutillib; print(kbutillib.__file__)"` against the baseline venv initially resolved to a sibling worktree's copy (`clh1-current-state-sql`); ran `pip install -e . --no-deps` from this worktree before testing, after which the import resolved to this worktree's `src/kbutillib/__init__.py`.

## caveats

- **The faked lakehouse boundary means the live Iceberg-on-Polaris path is unverified by this change.** Every test in `tests/berdl/test_clearinghouse_bootstrap.py` drives `bootstrap()` against a hand-built fake capability (`_FakeCapability`) with in-memory `table_exists`/`table_partition_spec`/`load` methods -- no pod, no Spark, no network, no real Iceberg table. These tests prove `bootstrap()`'s control flow (mode selection never requesting `'overwrite'` on an existing table, the partition-spec comparison and refusal, off-pod-refusal propagation, indeterminate-state handling, dry-run's no-write guarantee) but do **not** prove any of it against real Iceberg-on-Polaris. Per the task prompt, a companion operator runbook in the next phase is expected to carry that real-dependency check; this task does not claim to have performed it.
- **`table_exists` and `table_partition_spec` are not implemented on the real `BerdlCapability` today.** `bootstrap()` requires its injected `capability` to expose these two read-only methods in addition to `load()` (see the "Idempotent bootstrap" comment block above `bootstrap()` in `clearinghouse_schema.py` for the full rationale: `load()` cannot serve as a read-only probe because calling it is itself the write). Wiring a real adapter around `BerdlCapability` -- most plausibly via `capability.query()` against Iceberg/Spark catalog metadata -- is deliberately **not done** in this task, both because it is out of scope for "add bootstrap() to clearinghouse_schema.py" and because such wiring is exactly the kind of thing this task's own instructions say is not verifiable off-pod. An in-pod operator (or a follow-up task) will need to supply a `capability` argument satisfying this contract before `bootstrap()` can run for real. I judged this the most honest option given the constraint that only `clearinghouse_schema.py` (and a new test file) were mine to touch, and flagging the gap explicitly seemed better than inventing unverified SQL-parsing logic against `capability.query()`'s three different underlying result shapes (Trino tuples, Spark Rows, off-pod REST dict) to fake a "real" implementation that would itself be unverified.
- `bootstrap()`'s `dataset`/`tenant` parameters default to this module's `NAMESPACE`/`TENANT` constants (`"clearinghouse"`/`"kbaseincubator"`) and are passed straight through to `capability.load()`; the task prompt did not specify these explicitly, so I matched the existing module's constants rather than inventing new ones.
- The partition-spec comparison is list-equality with order significant (`_normalize_partition_columns` normalizes a bare string, a sequence, or `None`/absent all to a canonical `list[str]`, `[]` meaning unpartitioned) -- I did not attempt to normalize or tolerate reordering, since Iceberg partition-spec column order is itself meaningful.
- I did not touch `pyproject.toml`, `berdl/__init__.py`, or `clearinghouse_derivation.py`, per the parallel-lane instructions; `bootstrap()` is therefore only reachable as `from kbutillib.domains.kbase.berdl.clearinghouse_schema import bootstrap`, not via the `berdl` package's `__all__`. If a top-level re-export is wanted, that is left for the conductor to wire after both branches merge, as instructed.
