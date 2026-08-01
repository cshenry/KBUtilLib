---
name: BERDL Load
description: In-pod BERDL Lakehouse loading — preflight, DataFrame/bronze source-mode routing, write semantics, postflight verification, and namespace lifecycle (teardown, schema evolution) via BerdlCapability
scope: domain
---

# BERDL Load

## 1. What This Skill Covers

This skill covers **loading data into BERDL (BER Data Lakehouse) tenant
catalogs** — creating and appending to Iceberg tables, and administering the
namespaces that hold them — through
`kbutillib.domains.kbase.berdl.capability.BerdlCapability`, the deep module
built for this purpose. It is **in-pod only**: every write in BERDL requires
a Spark session, and Spark only exists inside the BERDL JupyterHub pod
(`kbhub` / `jupyter-chenry`). There is no off-pod write path — writes are
refused with guidance, never emulated.

Topics covered:
- The off-pod refusal — what it says, and why it must fire before anything
  else
- Preflight: read-write membership, table-existence/operation reporting
- Source-mode routing — DataFrame mode vs. bronze (file) mode
- Write semantics — `overwrite` vs. `append`, and the non-existent-table trap
- Postflight verification — row count and Iceberg snapshot history
- Namespace lifecycle — purge-ordered teardown and schema evolution

Companion skills: `/berdl-session` (locus detection, the import map, the
credential ladder — read this first if you have not already), `/berdl-query`
(reads, Trino/Spark routing, time travel), `/berdl-tenant` (membership, ACLs,
stewardship). This skill assumes you already know how to detect locus and
resolve a token; it does not repeat that material.

Entry point is always `BerdlCapability`, not a bare `berdl_notebook_utils`
call and not a hand-rolled `ingest` config:

```python
from kbutillib.domains.kbase.berdl.capability import BerdlCapability

berdl = BerdlCapability()
```

`BerdlCapability` implements the preflight/refusal/mode-selection logic
described in this document — see
`src/kbutillib/domains/kbase/berdl/capability.py` (`BerdlCapability.load`,
`build_ingest_config`, `select_write_mode`, `BerdlLoadRefusedError`) for the
authoritative implementation. This skill tells you *when* and *why* to call
it a given way; it does not reimplement its logic.

Verified read-write standing at design time on both initial target tenants —
**`aiale`** and **`kbaseincubator`** — so loads to either do not need an
access request first. Do not assume this holds for any other tenant; check
membership (Section 3) every time.

---

## 2. In-Pod Only: The Off-Pod Refusal

`BerdlCapability.load()` starts every call by resolving locus. Off-pod, it
refuses immediately and **before doing anything else** — no staging, no
generated artifacts, no dispatched work of any kind:

```python
from kbutillib.domains.kbase.berdl.capability import BerdlCapability, BerdlLoadRefusedError

berdl = BerdlCapability()
try:
    berdl.load(dataset="aiale", tables=[{"name": "my_table"}], dataframes={"my_table": df})
except BerdlLoadRefusedError as exc:
    print(exc)
```

The raised message names three things, always:
1. **The reason** — writing requires a Spark session, and Spark only exists
   inside the pod.
2. **The target machine** — `kbhub`.
3. **The exact command to run there** — a runnable `python -c "..."`
   one-liner (or the equivalent notebook cell) that constructs
   `BerdlCapability()` and calls `.load(...)` with your same arguments,
   from inside the pod.

If you are running this skill off-pod (laptop, h100, any machine other than
`kbhub`), **stop here**. Do not attempt to stage files, build an `ingest`
config, or hand the load off to another process — there is no partial
off-pod write path to fall back to. Re-run the same call inside a `kbhub`
notebook or terminal instead. Detecting locus itself is `/berdl-session`'s
job (`BerdlCapability().locus()` → `'in_pod'` / `'off_pod'`, which tests
importability of `berdl_notebook_utils`, not environment variables alone).

---

## 3. Preflight: Membership and Table State

In-pod, before any staging or writing, `load()` runs two more preflight
checks — both must pass, or nothing is written.

### 3a. Read-write membership (not read-only)

Read-only and read-write are **different groups** on the target tenant, not
two levels of one group (see `/berdl-tenant` for the full membership-decode
story). `load()` calls `memberships()` and refuses with `PermissionError` if
the target tenant is not `'rw'`:

```python
memberships = berdl.memberships()          # {tenant: 'rw' | 'ro'}
memberships.get("aiale")                   # 'rw' — confirmed, safe to load
```

Discovering read-only access **mid-load** is not a quick retry — access
requests are an asynchronous human approval step via Slack, typically
same-day during business hours but not instant, and the skill does not poll
for it. Checking membership *before* staging anything is the entire point:
an approval cycle measured in hours is a very different failure than a
five-second retry, and finding out after a bronze read has already staged
gigabytes to MinIO is strictly worse than finding out before. If membership
comes back `'ro'` or absent, stop and go through `/berdl-tenant`'s access-
request flow before touching this skill again — do not attempt the load
"just to see."

### 3b. Table existence and resulting operation

For each table in the call, `load()` resolves whether it already exists
(`table_exists(spark, name, namespace=...)`) and reports, per table, what
the operation will actually be — **before** the write happens:

| Table exists? | Requested mode | Operation |
|---|---|---|
| No | `append` or `overwrite` | `create` (see Section 5 — `append` is silently upgraded) |
| Yes | `overwrite` | `replace` (destructive — see Section 5) |
| Yes | `append` | `append` |

Read this report before trusting the call succeeded quietly — a load you
expected to `append` to an existing table that instead reports `create`
means the table name or namespace does not match what you think it does.

---

## 4. Source-Mode Routing

Both source modes are first-class, selected purely by whether you pass
`dataframes`. There is no separate flag.

### DataFrame mode (default when you already have a DataFrame)

Pass Spark DataFrames keyed by table name via `dataframes=`. This
short-circuits the bronze read entirely: **no MinIO staging occurs**, and
the `'paths'` config section is **omitted** — even if you also pass `paths`,
it is dropped, because nothing will ever read from it.

```python
report = berdl.load(
    dataset="aiale",
    tables=[{"name": "my_table", "mode": "append"}],
    tenant="aiale",
    dataframes={"my_table": my_spark_df},
    namespace="default",
)
```

### Bronze mode (CSV, TSV, JSON, XML, Parquet files)

Omit `dataframes`. This reads files from S3/MinIO or local storage and
**requires** a `paths` dict with `bronze_base`, plus per-table
`bronze_path`/`format` on each table entry:

```python
report = berdl.load(
    dataset="aiale",
    tables=[{
        "name": "my_table",
        "mode": "append",
        "bronze_path": "raw/my_table.csv",
        "format": "csv",
    }],
    tenant="aiale",
    paths={"bronze_base": "s3a://aiale-bucket/bronze"},   # 'silver_base' optional
    namespace="default",
)
```

Both modes route through `data_lakehouse_ingest.ingest` — **never** a raw
`writeTo` — because `ingest` is what applies schema enforcement in either
mode. `BerdlCapability.load()` is the only place this skill calls `ingest`;
do not call `data_lakehouse_ingest.ingest` directly, and do not construct a
DataFrame-mode call that also carries a `paths` section expecting it to be
merged — it will be dropped, not merged.

The full `ingest` config schema (required `dataset`/`tables`; optional
`tenant`, `is_tenant`, `pipeline_name`, `defaults`; per-table `name` required,
`comment`/`schema`/`schema_sql`/`enabled` optional) is built for you by
`build_ingest_config()` inside `load()` — you do not need to hand-assemble
it, only supply the arguments above.

---

## 5. Write Semantics

Mode is only ever one of two values: `overwrite` or `append`. There is no
third mode, and no upsert/merge mode.

- **`append` to a table that does not yet exist raises `ValueError`** inside
  `ingest` itself. `load()` protects you from this: it checks table
  existence in preflight and silently substitutes `overwrite` when you
  requested `append` on a table that does not exist yet, so a re-runnable
  loader (the common case — the same load script run repeatedly as new data
  arrives) does not fail on its first execution. The per-table report
  (Section 3b) tells you when this substitution happened — check
  `report["tables"][i]["operation"] == "create"` rather than assuming your
  requested mode was honored verbatim.
- **`overwrite` maps to `createOrReplace()`** — a full, destructive replace
  of the table's contents. It is **not** a merge and **not** an upsert.
  Recovery after an unwanted `overwrite` is via Iceberg snapshot time travel
  only (see `/berdl-query` for reading a prior snapshot) — there is no
  separate undo. Treat every `overwrite` call, and every `append` that gets
  silently upgraded to `overwrite` by the first-creation rule above, as
  irreversible in the ordinary sense.
- **`partition_by` accepts a string or a list** — `partition_by="date"` and
  `partition_by=["date", "region"]` are both valid; you do not need to wrap
  a single column in a list yourself.

---

## 6. Postflight Verification

`load()` does not report success just because `ingest()` returned without
raising. After the write, it verifies **each table** two ways and includes
both in the returned report:

1. **Row count** — `SELECT COUNT(*) FROM `<namespace>`.`<table>``.
2. **Snapshot history** — the most recent row of
   `SELECT * FROM `<namespace>`.`<table>`.history ORDER BY made_current_at
   DESC LIMIT 1`, reporting the new `snapshot_id`.

```python
report = berdl.load(dataset="aiale", tables=[...], tenant="aiale", dataframes={...})
for table_report in report["tables"]:
    print(table_report["name"], table_report["operation"],
          table_report["row_count"], table_report["new_snapshot_id"])
```

Treat a load as unverified — regardless of what `ingest()` returned — until
you have looked at both `row_count` and `new_snapshot_id` for every table in
the call. If either key comes back `None` with a `*_error` sibling key set,
the write itself may have succeeded while verification failed (e.g. a
transient read issue): re-run the row-count/snapshot query yourself before
declaring the load done. Success here is verified, not assumed.

---

## 7. Namespace Lifecycle

`BerdlCapability` covers the write path but does not wrap namespace
creation, teardown, or schema evolution — those are handled directly through
the import map (`/berdl-session` has the authoritative table) and Iceberg
Spark SQL, since there is no higher-level wrapper for them yet.

### 7a. Creating a namespace

```python
from kbutillib.domains.kbase.berdl.transports import InPodTransport

transport = InPodTransport()
spark = transport.spark_session()
transport.create_namespace_if_not_exists(spark, namespace="default", tenant_name="aiale")
```

`iceberg=True` is already the default — you do not need to pass it to get
the Iceberg (not legacy Delta) path.

### 7b. Teardown — purge-ordered, irreversible

**Iceberg via Polaris REST does not support `CASCADE`.** You cannot drop a
namespace that still has tables in it, and there is no single command that
does both steps for you. Teardown is exactly three steps, in this order,
every time:

1. **Enumerate every table in the namespace**, requesting a Python **list**,
   not the platform's default JSON **string** — the same `return_json=False`
   trap that applies to `get_databases`/`get_tables`/`get_table_schema`
   applies here too; do not iterate the result without checking its type
   first, or you will iterate characters of a string instead of table names.
2. **`DROP TABLE ... PURGE` each table individually.**
   `remove_table(spark, table_name, namespace=...)` (wrapped on
   `InPodTransport`) or the equivalent `spark.sql("DROP TABLE
   <catalog>.<namespace>.<table> PURGE")` — the `PURGE` is not optional.
   **Omitting `PURGE` silently orphans the table's S3 data files**: the
   catalog entry is gone, but the underlying Parquet/Avro/manifest files in
   MinIO/S3 are left behind, unreferenced and un-billed-for-by-name, with no
   catalog trail left to find them by later.
3. **Only then `DROP NAMESPACE`**, once every table it contained is gone.

```python
namespace = "some_namespace"

# Step 1 — enumerate as a real list, not the default JSON string.
tables = spark.sql(f"SHOW TABLES IN {namespace}").collect()
table_names = [row["tableName"] for row in tables]

# --- STOP: display exactly what will be deleted and get explicit
# --- confirmation before proceeding past this point. ---
print(f"About to PURGE {len(table_names)} table(s) in namespace {namespace!r}:")
for name in table_names:
    print(f"  - {namespace}.{name}")
print(f"Then DROP NAMESPACE {namespace!r}. This is irreversible.")
# require an explicit, affirmative confirmation from the caller here —
# do not proceed on an assumed or default "yes".

# Step 2 — PURGE each table.
for name in table_names:
    spark.sql(f"DROP TABLE {namespace}.{name} PURGE")

# Step 3 — only after every table is gone.
spark.sql(f"DROP NAMESPACE {namespace}")
```

Teardown is **irreversible** — there is no snapshot time-travel recovery
for a purged table the way there is for an `overwrite`, because `PURGE`
deletes the underlying data files, not just the catalog pointer to them.
Never run a teardown without first displaying the exact table list that
will be purged and the namespace that will be dropped, and never proceed
without an explicit confirmation from whoever asked for the teardown. A
teardown accidentally run against the wrong namespace, or run without
`PURGE` and repeated to "fix" the orphaned files, is exactly the kind of
half-right cleanup this section exists to prevent.

### 7c. Schema evolution (no data rewrite)

Iceberg supports adding, renaming, and dropping columns as pure metadata
operations — none of them rewrite the underlying data files:

```python
fqn = f"{namespace}.{table_name}"

# Add a column
spark.sql(f"ALTER TABLE {fqn} ADD COLUMN new_col STRING")

# Rename a column
spark.sql(f"ALTER TABLE {fqn} RENAME COLUMN old_col TO new_col")

# Drop a column
spark.sql(f"ALTER TABLE {fqn} DROP COLUMN unwanted_col")
```

These are metadata-only changes — fast, and they do not require re-loading
the table's data. They are still schema changes with consumer-visible
effects (a dropped column disappears from every downstream query
immediately), so treat them with the same "confirm before mutating shared
state" caution as any other namespace-lifecycle operation, even though they
are not destructive to data the way `overwrite` or `PURGE` are.

---

## 8. Quick Reference: End-to-End Load

```python
from kbutillib.domains.kbase.berdl.capability import BerdlCapability, BerdlLoadRefusedError

berdl = BerdlCapability()

# 1. Locus gate — refuses cleanly off-pod, no staging attempted.
try:
    report = berdl.load(
        dataset="aiale",
        tenant="aiale",
        tables=[{"name": "experiment_results", "mode": "append", "partition_by": "run_date"}],
        dataframes={"experiment_results": results_df},   # DataFrame mode: no 'paths'
        namespace="default",
    )
except BerdlLoadRefusedError as exc:
    print(exc)                     # off-pod: names kbhub + the exact command to re-run there
    raise
except PermissionError as exc:
    print(exc)                     # ro membership: stop, go request rw access, don't retry
    raise

# 2. Inspect the per-table report — operation, then verification.
for t in report["tables"]:
    print(t["name"], t["operation"], "rows:", t["row_count"], "snapshot:", t["new_snapshot_id"])
```

---

## 9. Related Skills

- `/berdl-session` — locus detection, the import map (why every call in
  this document is fully qualified rather than bare), the credential
  escalation ladder, and the access-denial taxonomy. Read this first.
- `/berdl-query` — reads, Trino/Spark routing, `my`/`{username}` alias
  translation, cross-catalog joins, and time-travel reads of a table's
  snapshot history (the recovery path referenced in Section 5).
- `/berdl-tenant` — membership decoding in full, access requests, namespace
  ACLs, stewardship, and the deprecated sharing-function doctrine.
