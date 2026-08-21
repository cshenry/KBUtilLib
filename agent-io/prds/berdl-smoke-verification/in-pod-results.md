# In-pod smoke verification — results

**Machine:** kbhub (`jupyter-chenry`, BERDL JupyterHub pod) · **Date:** 2026-08-03
**Session:** `/ai-cowork` (inbox task `2026-08-03_1308_in-pod-berdl-smoke-verification-kbu-dl-skills.md`)
**Spec:** [`humanprompt.md`](humanprompt.md) · **Off-pod half:** [`off-pod-results.md`](off-pod-results.md)

**Interpreter:** `/opt/conda/bin/python3.13` (the pod interpreter — the only one
with `berdl_notebook_utils`) with `PYTHONPATH=~/Dropbox/Projects/KBUtilLib/src`
for the editable `kbutillib` checkout, branch `wip` @ `98c6f44`. Nothing was
installed; `PIP_USER` was never exercised.

**Scratch namespace:** `kbaseincubator.smoke_scratch_20260803`, torn down at the
end of the session (item 8). Tenant `kbaseincubator` per the Q2 decision.

---

## Summary

| # | In-pod item | Result |
|---|---|---|
| 1 | Trino connector is explicit, never legacy `delta_lake` | **PASS** (clean) |
| 2 | JSON-string returns not iterated as lists | **PASS** (clean) |
| 3 | First-load re-runnability (run the same load twice) | **FAIL** — silent data loss |
| 4 | Alias translation (`my` Spark-only → `{username}` Trino) | **PASS** (clean) |
| 5 | Both source modes; DataFrame mode does no MinIO staging | **PASS** (clean) |
| 6 | Preflight gate ordering + read-only refusal (`refdata`) | **PASS** (message wording caveat) |
| 7 | Postflight verification (row count, snapshot history) | **FAIL** — always `null` |
| 8 | Purge-ordered teardown, verified in object storage | **PASS** (clean) |

Five clean passes, two failures, one pass with a caveat. Both failures are in
`BerdlCapability.load()` and both are namespace-resolution bugs. Three defects
filed (D1–D3 below); **D1 is data-destroying and should block any real use of
`load()`.**

A ninth finding, outside the numbered items: **`load()` cannot write to the
personal catalog at all** (D3). The spec's own instruction — "personal catalog
first, then one tenant write" — is not currently executable.

---

## Step 0 — Spark Connect repair (the session's highest-value step)

The addendum was right that this mattered most. State on arrival:

- `get_spark_connect_status()` → `{"status": "running", "pid": 374, "port": 15002}`.
- The driver log `~/.spark/connect-server-logs/spark-connect-server-chenry.log`
  (148 MB) **had not been written since `26/08/01 23:45`** — nearly two days.
- JVM (PID 381, child of 374) alive: RSS 3.3 GB, 10.8% CPU, uptime 3d22h.
- A raw socket connect to `127.0.0.1:15002` **succeeded** — so the port was
  genuinely listening.

That is the documented signature exactly: listening socket, live JVM, dead
driver. `status: "running"` is derived from the PID file and is **not** evidence
the server serves sessions.

**Non-rotating repair only** (ladder step 1; `refresh_spark_environment()` was
never called, so no credentials were rotated and no other kernel was disturbed):

1. `InPodTransport().credentials()` → OK (`CredentialsResponse`).
2. `start_spark_connect_server(force_restart=True)` → logged
   `Timeout waiting for port 15002 to be released` then
   `Server survived SIGTERM; escalating to SIGKILL`. New driver PID **688937**.

**Verification that it actually serves** (the check `status` cannot make):
`get_spark_session()` returned in **12.1 s** and `SELECT 1` → `[Row(ok=1)]`.

Chris was pinged at this point, per the addendum, so the laptop's `offpod_probe2.py`
could be re-run against a healthy pod without waiting for the rest of this session.

> **Operational note:** the zombie needed **SIGKILL** — a plain restart that only
> sends SIGTERM will appear to succeed and leave the zombie in place. Worth
> encoding wherever this repair gets documented.

---

## Item 1 — Trino connector default: PASS

`get_trino_connection` does default to `connector='delta_lake'`, but nothing in
this stack takes that default:

- `capability.py:541` is the **only** call site and passes
  `connector="iceberg"` explicitly.
- `transports.py:279` forwards the caller's `connector` through; its docstring
  (`transports.py:272`) records the legacy default as a hazard.
- The `kbu-dlquery` skill documents the rule at lines 57–60 ("do not add a new
  call site that omits `connector=`").

Live: `trino_connection(connector="iceberg")` → `Connection`; `SELECT 1` → `[[1]]`.

## Item 2 — JSON-string returns: PASS

The trap is real at the helper level but handled at the transport boundary.
`BerdlCapability.databases()` returned a genuine `list` of **97**
`NormalizedDatabase` objects — not a string, and therefore not iterable as
characters. Verified `type(...).__name__ == 'list'` and inspected element
structure (`name`, `is_iceberg`, `legacy_alias`), rather than trusting `len()`,
which a string would also answer.

Two incidental observations from the same call:

- **No entry had `legacy_alias` set, and none had `is_iceberg=False`.** The
  in-pod `get_databases()` surface does not return underscored legacy Delta
  twins at all, so in-pod gives **no evidence either way** on off-pod item 5
  (dual-name disambiguation). That check remains genuinely off-pod-only.
- **The personal catalog is absent in-pod too** — see the Q3 note below.

## Item 3 — First-load re-runnability: **FAIL (silent data loss)**

This is defect **D1** and it is the serious one.

`BerdlCapability.load()` takes a `namespace` parameter defaulting to
`"default"`, and uses it for the pre-write existence check. But
`data_lakehouse_ingest` **derives its own target namespace** and ignores that
parameter entirely — `orchestrator/init_utils.py:102–117`: `{tenant}.{dataset}`
when a tenant is given, `my.{dataset}` otherwise.

So the existence check looks in a namespace the write never touches, always
finds nothing, and `select_write_mode('append', table_exists=False)` dutifully
returns `'overwrite'`.

Observed live, same load run three times against `kbaseincubator.smoke_scratch_20260803.t1`:

| Run | `namespace=` passed | `existed_before` | `effective_mode` | Row count after |
|---|---|---|---|---|
| 1 | `"default"` (the default) | `false` | `overwrite` | 3 (created) |
| 2 | `"default"` (the default) | `false` | `overwrite` | **6 → 3** |
| 3 | `kbaseincubator.smoke_scratch_20260803` | `true` | `append` | 3 → 6 |

Run 2 is the failure: **`mode="append"` was requested against an existing table
and the table was overwritten instead — three rows destroyed, no error, no
warning, `success: true`.** `ingest`'s own log for that run makes the
contradiction explicit:

```
Table default.t1 does not exist.
... Writing table: kbaseincubator.smoke_scratch_20260803.t1 (mode=overwrite, exists=True)
```

`ingest` knew the table existed. The mode had already been forced by the wrong
existence check upstream.

Run 3 is the control and it matters: passing the namespace that `ingest`
actually derives makes everything behave correctly. **`select_write_mode` is not
the bug** — its logic is right. The bug is the `namespace` default, and the fact
that a caller must guess a value that duplicates derivation logic living in
another package.

Direct probe of the underlying helper:

```
table_exists(spark,'t1',namespace='default')                            -> False
table_exists(spark,'t1',namespace='smoke_scratch_20260803')             -> True
table_exists(spark,'t1',namespace='kbaseincubator.smoke_scratch_20260803') -> True
```

## Item 4 — Alias translation: PASS

Pure helpers round-trip correctly (`naming.SPARK_PERSONAL_ALIAS == 'my'`):

```
to_trino_alias('my.foo.bar', 'chenry')             -> 'chenry.foo.bar'
to_trino_alias('kbaseincubator.foo.bar', 'chenry') -> 'kbaseincubator.foo.bar'   (unchanged)
to_spark_alias('chenry.foo.bar', 'chenry')         -> 'my.foo.bar'
to_spark_alias('kbaseincubator.foo.bar', 'chenry') -> 'kbaseincubator.foo.bar'   (unchanged)
```

End-to-end: the table was **written via Spark** and then **read back through
Trino** with `capability.query()`, returning all six rows correctly. Tenant
catalog names pass through untranslated, which is correct — only the `my` alias
needs rewriting.

Caveat worth stating plainly: because `load()` cannot write to the personal
catalog (D3), the `my` → `{username}` translation could only be verified on the
**pure helpers**, not on a live personal-catalog write-then-Trino-read. That
specific end-to-end path remains unexercised, and will stay unexercised until
D3 is fixed.

## Item 5 — Both source modes: PASS

**DataFrame mode** — `build_ingest_config` drops `paths` unconditionally when
`dataframes` is truthy, even when the caller also passed `paths`:

```python
build_ingest_config(dataset, [{"name":"t1"}], dataframes={"t1": ...},
                    paths={"bronze_base": "s3a://should/be/dropped"})
# -> {'dataset': ..., 'tables': [{'name': 't1'}]}     ('paths' present? False)
```

Confirmed in the live run logs: `No 'paths' section found in config — skipping
base path validation` and `Bronze: <dataframe override>`. **No MinIO staging
occurred.**

**Bronze mode** — staged a CSV to
`s3a://cdm-lake/users-general-warehouse/chenry/smoke_bronze_20260803/t2/` and
loaded with `paths.bronze_base`. `ingest` reported `input_source: BRONZE`,
`bronze_path: s3a://.../t2`, `rows_in: 3`, `rows_written: 3`, `status: SUCCESS`.

One caveat, **not** a kbutillib defect: `ingest` logged `No defaults found for
format 'csv', using safe fallback` and read the **header row as data** — the
resulting columns were `_c0, _c1, _c2` and a 2-row CSV became 3 rows. Any real
bronze CSV load needs explicit `defaults`/read options. Worth a line in
`kbu-dlload`; it will silently corrupt a first load otherwise.

Minor observation: DataFrame mode still auto-initializes a MinIO client
(`No MinIO client provided — attempting auto-initialization via get_s3_client()`)
even though nothing stages. Harmless, but it means a MinIO credential failure
could surface on a path that never needs MinIO.

## Item 6 — Preflight gate ordering + read-only refusal: PASS

**Ordering, confirmed by code and by behavior.** In `capability.py`, the
membership check (lines 421–431) precedes `transport.spark_session()` (line 433),
the per-table existence checks (438–442), and the `ingest` call (476). The Q4
premise held: the check is a client-side lookup in the membership dict.

**The `refdata` refusal test ran for real,** per the Q4 decision:

```
PermissionError: BerdlCapability.load() refused: no read-write membership on
tenant 'refdata' (current permission: 'ro'). Request read-write access before
retrying; this is an asynchronous human approval step.
```

It refused early, named the tenant, and named the actual permission held
(`'ro'`). Nothing was staged and nothing was attempted against `refdata`.
Membership decoding itself was also correct across the heterogeneous-shape trap
— `get_my_groups()` → `UserGroupsResponse` (list on `.groups`) vs
`list_available_groups()` → plain `list` of 31 — yielding:

```
aiale rw · enigma ro · globalusers rw · ideas rw · kbase rw
kbaseincubator rw · kescience rw · microbialdiscoveryforge ro
planetmicrobe ro · refdata ro
```

**Caveat (folded into D3):** the refusal message is correct for `refdata` but is
emitted verbatim for the personal-catalog case too, where "request read-write
access… asynchronous human approval step" is actively misleading — there is no
one to ask and no approval to wait for.

## Item 7 — Postflight verification: **FAIL**

Defect **D2**. `row_count` and `new_snapshot_id` came back `null` on **every**
load in this session — including run 3, which passed the correct namespace and
wrote successfully. Two independent causes:

1. **Wrong namespace** (same root as D1) when `namespace` is left at `"default"`.
2. **Broken quoting**, which bites even when the namespace is correct.
   `capability.py:486` builds `` f"`{namespace}`.`{report['name']}`" ``, so a
   dotted namespace becomes a *single* backticked identifier:

```
`kbaseincubator.smoke_scratch_20260803`.`t1`   -> TABLE_OR_VIEW_NOT_FOUND
kbaseincubator.smoke_scratch_20260803.t1       -> 6 rows
`kbaseincubator`.`smoke_scratch_20260803`.`t1` -> 6 rows
```

The underlying capability is fine — queried correctly, snapshot history returns
cleanly (three snapshots, matching the three runs):

```
6090223318847613722  2026-08-03 18:32:34.425
3030600346369501408  2026-08-03 18:32:30.248
1116709942058101386  2026-08-03 18:32:09.223
```

**The severity multiplier:** `load()` catches both failures and stores them in
`row_count_error` / `snapshot_history_error` while still returning
`success: true` at the top level. So postflight verification is not merely
absent — it is **silently** absent, and it is exactly the check that would have
caught D1. A caller reading `ingest_result.success` sees a green result while
verification never ran.

## Item 8 — Purge-ordered teardown: PASS

Pre-teardown inventory: `t1` (6 data files), `t2` (2 data files), all under
`s3a://cdm-lake/tenant-sql-warehouse/kbaseincubator/iceberg/smoke_scratch_20260803/`.

Order followed exactly — every table dropped `PURGE` first, namespace second:

```
DROP TABLE kbaseincubator.smoke_scratch_20260803.t1 PURGE -> ok
DROP TABLE kbaseincubator.smoke_scratch_20260803.t2 PURGE -> ok
DROP NAMESPACE kbaseincubator.smoke_scratch_20260803     -> ok
```

Post-teardown: `SHOW TABLES` → `NoSuchNamespaceException`; `SHOW NAMESPACES IN
kbaseincubator` no longer lists it.

**Object-storage confirmation** — the part the catalog cannot tell you:

```
tenant-sql-warehouse/kbaseincubator/iceberg/smoke_scratch_20260803/  -> 0 objects
```

`PURGE` genuinely removed the data files. Verified with the authenticated MinIO
client (`get_s3_client()`) rather than `aws s3 ls` — the `aws` CLI is present at
`/usr/local/bin/aws` but is not credentialed for this endpoint, so the MinIO
client is the equivalent authenticated check. The bronze staging CSV was also
removed; **no scratch state remains** in either the catalog or object storage.

---

## Defects filed (project `kbutillib`)

### D1 — `load()` silently converts `append` into `overwrite` (data loss)

**Exact call:**
```python
BerdlCapability().load(dataset="smoke_scratch_20260803",
                       tables=[{"name": "t1", "mode": "append"}],
                       tenant="kbaseincubator", dataframes={"t1": df})
```
**Expected:** existing table appended (3 → 6 rows).
**Actual:** `existed_before=false`, `effective_mode='overwrite'`, table replaced
(6 → 3 rows), `success: true`, no warning.

**Cause:** `capability.py:442` calls
`transport.table_exists(load_spark, name, namespace=namespace)` with
`namespace` defaulting to `"default"` (line 343), but `ingest` derives its
target as `{tenant}.{dataset}` / `my.{dataset}`
(`data_lakehouse_ingest/orchestrator/init_utils.py:102–117`) and ignores the
parameter.

**Fix:** derive the namespace inside `load()` the same way `ingest` does instead
of defaulting to `"default"` — `create_namespace_if_not_exists` already
*returns* the resolved namespace, so the honest fix is to resolve it once and
use it for both the existence check and postflight. Failing that, `namespace`
should have no default and be validated against `tenant`/`dataset`. A silent
`append` → `overwrite` demotion should never be reachable; if existence cannot
be determined, `load()` should raise rather than pick the destructive mode.

### D2 — Postflight verification never runs, and fails silently

**Exact call:** any successful `load()`.
**Expected:** `row_count` and `new_snapshot_id` populated.
**Actual:** both `null` on every run, with the real errors buried in
`row_count_error` / `snapshot_history_error` while the call returns
`success: true`.

**Cause:** two, independently sufficient — (a) the D1 namespace mismatch;
(b) `capability.py:486` builds `` f"`{namespace}`.`{name}`" ``, backticking a
dotted namespace into one identifier. Proven: `` `ns`.`t1` `` →
`TABLE_OR_VIEW_NOT_FOUND` while `ns.t1` and `` `c`.`n`.`t1` `` both return 6 rows.

**Fix:** quote per segment (`` `cat`.`ns`.`tbl` ``) or leave the dotted name
unquoted, and fix the namespace per D1. Do not keep swallowing both exceptions
into report fields while reporting overall success — a postflight check that
cannot run is a failed load, not a successful one with a note.

### D3 — `load()` cannot write to the personal catalog at all

**Exact call:**
```python
BerdlCapability().load(dataset="smoke_scratch_20260803",
                       tables=[{"name": "t1", "mode": "append"}],
                       dataframes={"t1": df})   # no tenant -> personal catalog
```
**Expected:** write to `my.smoke_scratch_20260803`.
**Actual:**
```
PermissionError: BerdlCapability.load() refused: no read-write membership on
tenant 'smoke_scratch_20260803' (current permission: 'none'). Request read-write
access before retrying; this is an asynchronous human approval step.
```

**Cause:** `capability.py:422` — `target_tenant = tenant or dataset`. With no
tenant, the **dataset name** is looked up in the tenant membership dict, misses,
and refuses. A personal dataset name can never appear there, so **every**
personal-catalog load is refused.

**Fix:** when `tenant is None` the write targets `my.{dataset}`, which the user
owns by construction — skip the tenant membership gate entirely rather than
looking up a dataset name as if it were a tenant. The guidance text must also
not be emitted for this case; there is no access request to make.

**Impact beyond the refusal:** this is why the spec's "personal catalog first,
then one tenant write" could not be run as written, and why item 4's `my` →
`{username}` translation could only be verified on the pure helpers.

---

## Open questions Q1–Q4 — none reopened, one strengthened

- **Q1 (stay a manual procedure)** — **holds, and this session is evidence for
  it.** A `kbu dl smoke` script written before this run would have encoded the
  broken `namespace="default"` call shape as the reference example and made D1
  harder to see, not easier.
- **Q2 (`kbaseincubator`)** — used; no issue. Namespace created and fully
  purged; nothing left behind in a shared tenant.
- **Q3 (off-pod personal-catalog gap → warn and continue naming kbhub)** —
  **not contradicted; strengthened.** In-pod `databases()` returned 97 entries
  across 9 tenant catalogs (`aiale`, `enigma`, `globalusers`, `ideas`, `kbase`,
  `kbaseincubator`, `kescience`, `planetmicrobe`, `refdata`) and **zero** `my.*`
  or `chenry.*` entries. The personal catalog is invisible to the `get_databases`
  surface **in both loci** — so this is not an off-pod REST limitation as the
  question assumed, and "route the user to the pod" would not have helped.
  Warn-and-continue remains right, but the warning should not imply the pod
  would show personal datasets in a `databases()` listing.
- **Q4 (run the refusal test against `refdata`)** — run, refused early and
  cleanly, nothing attempted against the tenant. The evidence-based safety
  argument held exactly as stated.

## Scope guard

Respected. No `kbu dl smoke` operator script was written, no dispatch or
notebook-generation path was added, no tests were changed, and nothing was
installed. The only rotating repair (`refresh_spark_environment()`) was **not**
called — step 1 of the ladder sufficed, so no other kernel's credentials were
invalidated.

## Reproduction scripts

Working scripts are in the session scratchpad (not committed — they hardcode the
scratch namespace and are superseded by this record):
`smoke_personal.py`, `smoke_tenant.py`, `smoke_run2.py`, `smoke_rest.py`,
`smoke_4_5.py`, `smoke_bronze.py`, `smoke_teardown.py`.
