# Clearinghouse Schema -- Operator Runbook

**PRD**: `clearinghouse-lake-1-schema` (KBDLJobRunningPrototype, on `wip`; not
generally reachable from off-pod worktrees -- treat this document as the
authoritative reference for the in-pod steps).

## Why this document exists, and why it must be a human

The three clearinghouse tables (`entity`, `canonical_content`, `result`)
this PRD defines live inside the BERDL JupyterHub pod (host `kbhub`), inside
the `kbaseincubator` tenant. No automated build agent can reach that pod:

- `BerdlCapability.load()` (`src/kbutillib/domains/kbase/berdl/capability.py`)
  refuses off-pod by design (`BerdlLoadRefusedError`) -- see its `locus()`
  check, which tests importability of the pod-only `berdl_notebook_utils`
  package, not just environment variables.
- `kbhub` is not a headless Maestro/AgentForge worker host.

Everything built in the three earlier phases of this PRD --
`clearinghouse_schema.py` (table configs, `entity_hash` encode/decode,
`bootstrap()`) and `clearinghouse_derivation.py` (`current_state_sql()`) --
is code plus pod-free tests (`tests/berdl/test_clearinghouse_schema.py`,
`test_clearinghouse_bootstrap.py`, `test_clearinghouse_derivation.py`), all
of which pass off-pod against a fake capability (bootstrap tests) or a
DuckDB surrogate (derivation tests). **None of that proves the live
Iceberg-on-Spark path actually works.** This runbook is what an operator
follows, by hand, inside the pod, to finish the job -- and OP3 below is the
one step in the whole PRD that exercises the real dependency.

**Be honest about what has and hasn't been proven before you start**: the
DuckDB tests in `test_clearinghouse_derivation.py` prove `current_state_sql()`'s
*logic* -- which row wins a slot, which scope excludes which -- against
DuckDB as a stand-in engine, never against Spark-on-Iceberg. The bootstrap
tests in `test_clearinghouse_bootstrap.py` prove `bootstrap()`'s control
flow (create-vs-append selection, the partition-spec guard, off-pod
refusal, dry-run's no-write guarantee) against a hand-built fake capability
that stands in for the whole lakehouse boundary -- they do not prove
anything about the real tenant, the real catalog, or the real column
types. OP3, and only OP3, runs against the real thing.

Do these three steps **in order**. OP1 is blocking: do not attempt OP2 or
OP3 until it succeeds.

---

## OP1 (BLOCKING) -- verify the three-package import

### The problem

No single environment on `kbhub` currently imports `kbutillib`,
`berdl_notebook_utils`, and `data_lakehouse_ingest` together:

- `kbutillib` is importable only from `~/venvs/kbu-modeling` -- which lacks
  `berdl_notebook_utils`.
- The main conda environment at `/opt/conda` lacks `kbutillib`.

Prior reconnaissance had to bridge the two with a `sys.path` append to get
all three importable in one interpreter. **This runbook does not tell you
which of those two environments to fix, or how** -- neither is inspectable
from off-pod, and a confidently wrong `pip install` command for an
environment nobody here can see is worse than no command at all. That
choice (extend `~/venvs/kbu-modeling` with `berdl_notebook_utils` and
`data_lakehouse_ingest`, extend `/opt/conda` with `kbutillib`, or bridge
both with a `sys.path` append as recon did) is yours to make once you can
see the pod's actual environments.

### The verification step

Run this in whichever environment you believe now has all three, from a
`kbhub` notebook cell or terminal:

```python
import kbutillib
import berdl_notebook_utils
import data_lakehouse_ingest

print("kbutillib:", getattr(kbutillib, "__version__", "unknown"), kbutillib.__file__)
print("berdl_notebook_utils:", getattr(berdl_notebook_utils, "__version__", "unknown"), berdl_notebook_utils.__file__)
print("data_lakehouse_ingest:", getattr(data_lakehouse_ingest, "__version__", "unknown"), data_lakehouse_ingest.__file__)
```

**All three imports must succeed in the same interpreter, in the same
cell.** If any one of them raises `ModuleNotFoundError`, OP2 and OP3 cannot
run -- do not skip ahead and improvise a partial workaround (e.g. calling
`bootstrap()` against a hand-rolled adapter that never actually reaches
Spark). Fix the environment gap first, re-run this cell, and only proceed
once all three lines print.

---

## OP2 -- create the tables

### 2.0 -- the capability seam you will hit here (read this first)

`bootstrap()` (`clearinghouse_schema.bootstrap()`) requires its injected
`capability` argument to expose three things:

- `capability.load(*, dataset, tables, namespace, tenant=None, pipeline_name=None)`
  -- same shape as `BerdlCapability.load()`.
- `capability.table_exists(name, namespace=namespace) -> bool` -- read-only.
- `capability.table_partition_spec(name, namespace=namespace) -> list[str] | None`
  -- read-only.

**The stock `BerdlCapability` does not implement the last two.** Its only
existence check anywhere in the write path is a private, in-pod-only call
inside `load()` itself -- `transport.table_exists(load_spark, name,
namespace=namespace)` on `InPodTransport` (`transports.py`), which takes a
`spark` session as its first argument and is not exposed as a public,
capability-level, read-only method. There is no `table_partition_spec`
reader anywhere in this repo, on `BerdlCapability`, `InPodTransport`, or
otherwise.

This is a known, accepted gap in what shipped from the earlier phases, not
an oversight you're the first to discover: the tests in
`test_clearinghouse_bootstrap.py` exercise `bootstrap()` entirely against a
hand-built fake capability (`_FakeCapability`) that implements this
three-method contract, specifically because wiring a real adapter around
`BerdlCapability` -- most plausibly via `capability.query()` against
Iceberg/Spark catalog metadata -- is exactly the kind of thing that cannot
be verified off-pod. **You are the first person to run this against a real
capability, and you have to build or adapt one first.**

Concretely, before you can call `bootstrap()` for real, write a small
in-pod adapter (or a subclass, or a wrapper object -- whatever mechanism
fits how you're already interacting with `BerdlCapability` in the pod) that:

- Delegates `load(...)` straight to a real `BerdlCapability().load(...)`.
- Implements `table_exists(name, namespace=namespace)` -- most plausibly by
  querying Iceberg catalog metadata via `capability.query(sql,
  engine="spark")` (e.g. `SHOW TABLES IN <namespace>` or a catalog
  information-schema query -- the exact SQL is a pod-only detail; use
  whichever this cluster's Iceberg catalog actually exposes), or via
  `InPodTransport.table_exists(spark, name, namespace=namespace)` directly
  if you're comfortable reaching past `BerdlCapability`'s public surface
  for this one read.
- Implements `table_partition_spec(name, namespace=namespace)` -- there is
  no existing reader for this anywhere in the codebase; you will need a
  catalog-metadata query for the live partition columns (e.g. inspecting
  the table's `DESCRIBE`/`SHOW CREATE TABLE` output or the Iceberg
  `partitions`/`snapshots` metadata tables) and translate it to the
  `list[str]` shape `bootstrap()` expects (`[]` for unpartitioned).

An `AttributeError` at this step (`'BerdlCapability' object has no
attribute 'table_exists'`) means you skipped this -- it is not a bug in
`bootstrap()`, and it is the expected first failure mode for anyone who
tries to pass a bare `BerdlCapability()` straight in.

### 2.1 -- dry run first

Once you have a capability satisfying the contract above, run `bootstrap()`
with `dry_run=True` **before** the real run:

```python
from kbutillib.domains.kbase.berdl.clearinghouse_schema import bootstrap

report = bootstrap(
    my_capability,               # your OP2.0 adapter, not a bare BerdlCapability()
    namespace="clearinghouse",   # confirm this is the namespace you intend
    dry_run=True,
)
for table in report["tables"]:
    print(table)
```

Each table's report entry shows `'action'`: `'create'`, `'append'`, or
`'refuse'` (with a `'reason'`), plus (for `'create'` entries)
`'namespace_warning'`.

**If a dry run reports `'action': 'create'` for a table you believe
already exists, stop.** That is a namespace-resolution problem -- most
likely a wrong or misresolved `namespace` argument (tenant-qualified vs. a
personal `my.` prefix, or similar) -- not evidence that the table is
genuinely new. Do **not** proceed past this into the real run: `load()`'s
own `select_write_mode()` promotes a requested `'append'` to `'overwrite'`
for any table it independently determines does not yet exist, which means
proceeding here risks **silently overwriting a live table** that the
dry-run simply looked up under the wrong namespace. Resolve which
namespace resolves to the table you actually mean before re-running the
dry run and confirming it now reports `'append'`.

Only once the dry run's report matches what you expect -- `'create'` for
tables that are genuinely new (first bootstrap ever), `'append'` for
tables that already exist with a matching partition spec -- move to the
real run.

### 2.2 -- the real run

```python
report = bootstrap(
    my_capability,
    namespace="clearinghouse",
    dry_run=False,
)
print(report)
```

**Expected output, first (creating) run** -- every table's report entry has
`'exists': False`, `'action': 'create'`, and `report['load_result']` is the
`BerdlCapability.load()` report dict (`{'ingest_result': ..., 'tables':
[...]}`) with one entry per table, `'operation': 'create'`.

**Expected output, a re-run against the same namespace (idempotent)** --
every table's report entry has `'exists': True`, `'actual_partition_by'`
matching `'expected_partition_by'`, and `'action': 'append'`.
`report['load_result']`'s per-table entries show `'operation': 'append'`.
No table is overwritten and no table's partitioning is re-specced.

**How to read a partition-spec refusal.** If a table already exists with a
live partition spec that disagrees with this module's config (in
particular, `result` should be partitioned on `source`; `entity` and
`canonical_content` should be unpartitioned), `bootstrap()` raises
`BootstrapPartitionSpecMismatchError` naming both the expected and actual
spec, and **writes nothing for any table in the batch** -- not just the
mismatched one. **Do not append anyway.** Changing a live Iceberg table's
partition spec through this write path is unsupported and unmeasured;
`bootstrap()` refuses specifically so nobody discovers that the hard way
mid-write. The tenant's tables are append-only by design, so the correct
fix is to rebuild by replay: drop the mismatched table, recreate it under
the corrected `partition_by` (a fresh `bootstrap()` call against the empty
namespace), and re-ingest whatever was already written into it. Do not try
to patch the partitioning of the live table in place.

### 2.3 -- acceptance step: confirm `entity_hash` is genuinely BINARY

**Do this immediately after table creation succeeds, before any real
corpus is loaded.** `clearinghouse_schema.py`'s own module docstring flags
this explicitly: `entity_hash` is declared as bare `BINARY` in this
module's `schema_sql`, but whether Spark SQL's `data_lakehouse_ingest`
write path actually honors that declaration -- as opposed to silently
creating a `STRING` column -- is **not verifiable off-pod** and is not
asserted as fact anywhere in the earlier phases' code or tests. This is
the one step where a silently-wrong type gets caught, and it must be
caught here: discovering *after* the real annotation corpus has been
loaded that `entity_hash` is actually `STRING` means every already-written
row's hash has to be re-encoded (or the table rebuilt from source) at full
corpus scale, instead of a five-minute fix against three empty tables.

Run, against each of the three tables (using whichever introspection this
cluster's Iceberg catalog exposes -- `DESCRIBE`, `SHOW CREATE TABLE`, or
Spark's catalog API all work):

```python
for table in ("entity", "canonical_content", "result"):
    print(table, spark.sql(f"DESCRIBE `clearinghouse`.`{table}`").collect())
```

Confirm `entity_hash`'s reported type is `binary`, not `string`, in every
table that has the column (`entity`, `canonical_content`, `result`). If it
is `string`, **stop before loading any data**: drop the affected table(s),
adjust however this write path needs to be told to honor `BINARY` (a
pod-only detail this module deliberately does not guess at), recreate via
`bootstrap()`, and re-verify with this same check before proceeding.

---

## OP3 -- run the parity check

`scripts/clearinghouse_parity_check.py` proves that `current_state_sql()`
behaves identically on the real Spark/Iceberg engine as it does on the
DuckDB surrogate the CI suite uses. Run it from inside the pod, after OP1
and OP2 have both succeeded:

```bash
python scripts/clearinghouse_parity_check.py
```

What it does:

1. Builds a small, explicitly-typed Spark DataFrame from
   `kbutillib.domains.kbase.berdl.clearinghouse_parity_fixture` --
   **the exact same fixture rows** `tests/berdl/test_clearinghouse_derivation.py`
   imports and inserts into its DuckDB surrogate (see that module's
   docstring). Sharing one module, rather than hand-retyping a lookalike
   fixture in this script, is what keeps the two from silently drifting
   apart -- a parity check run against a fixture that no longer matches
   what the DuckDB tests exercise would prove nothing.
2. Appends those rows to the real, live `result` table through
   `BerdlCapability.load()` -- the same sanctioned write path OP2 uses --
   never a raw `pyiceberg` write (see "if something goes wrong" below).
3. Runs the real `current_state_sql()` SQL text against the live table via
   Spark, once per property, and asserts the same six properties the
   DuckDB tests assert:
   - duplicate appends collapse to exactly one current row,
   - the newest row (by `observed_at`) wins per slot,
   - an `observed_at` tie is broken by the greater `ingest_batch_id`,
   - a term dropped from a newer whole-call payload is absent from
     current state,
   - two sources annotating the same entity both remain current, and a
     `sources` filter prunes to exactly the requested sources,
   - `result_type_version` differences alone never fork a slot.
4. Prints one `PASS`/`FAIL` line per property plus a final summary, and
   exits non-zero if any property fails.

**Every fixture row's `source` carries the `parity-check/` prefix**
(`PARITY_SOURCE_PREFIX` in the fixture module) so these rows can never be
mistaken for real tool output, and can be found again later with
`WHERE source LIKE 'parity-check/%'`. **These rows are expected to remain
in the append-only `result` table permanently** -- this is expected and
harmless: they occupy their own `(entity_hash, result_type, source)`
slots, distinct from any real corpus's slots, and re-running this script
appends more rows to those same slots without changing any real
annotation's current-state answer.

**Record the result.** After running OP3, note in this table (or your own
operational log) the date, who ran it, and whether all six properties
passed:

| Date | Operator | Result |
|---|---|---|
| _(fill in)_ | _(fill in)_ | _(fill in: ALL PASS / n FAILED, which)_ |

If `RESULT_TABLE_FQN` in the script (`kbaseincubator.clearinghouse.result`)
does not resolve, see the comment above that constant in
`scripts/clearinghouse_parity_check.py` -- `BerdlCapability.load()`'s own
postflight queries use a different, two-part form
(`clearinghouse.result`, no tenant segment), and which form actually
resolves against the live catalog is not verifiable off-pod. Try the
two-part form next, and record in this log which one worked.

---

## If something goes wrong

**A partition-spec refusal is not a green light to append anyway.**
`bootstrap()`'s `BootstrapPartitionSpecMismatchError` (OP2.2) exists
specifically because there is no check anywhere in `BerdlCapability.load()`'s
own write path comparing a config's `partition_by` against a live table's
actual partition spec before appending. If you bypass the refusal and
append regardless -- by calling `BerdlCapability.load()` directly instead
of through `bootstrap()`, for instance -- you are writing into a table
whose live partitioning may disagree with what every reader (including
`current_state_sql()`'s own pruning-by-source logic) assumes, with no
supported way to undo it. Rebuild by replay instead (see OP2.2).

**A direct `pyiceberg` write is available in the pod, and must not be used
to route around a failure.** `bootstrap()` and `BerdlCapability.load()`
both route every write through `data_lakehouse_ingest.ingest`, never a raw
`pyiceberg` call, specifically because `ingest` is what applies schema
enforcement in both of its write modes (dataframe mode and bronze mode).
A raw `pyiceberg` write bypasses that enforcement entirely -- it is not a
faster path to the same sanctioned result, it is a different, unsanctioned
result that happens to land in the same table. If `bootstrap()` or
`BerdlCapability.load()` is refusing a write you believe should succeed
(a partition-spec mismatch, a membership check, an OP2.3 type mismatch),
the fix is to resolve *why* it's refusing -- rebuild by replay, request
read-write membership, correct the column type -- never to reach for
`pyiceberg` as a workaround. This applies equally to OP2 (table creation)
and OP3 (the parity check's fixture append): both go through
`BerdlCapability.load()` and neither should ever be "fixed" by dropping
to a raw Iceberg write.

**An `AttributeError` calling `bootstrap()` against a bare
`BerdlCapability()`** means you skipped OP2.0 -- see that section for what
you need to build first.

**A namespace-resolution warning on a dry run** (OP2.1) means stop and
verify the `namespace` argument before proceeding -- see that section for
why proceeding risks an overwrite.
