---
name: KBU Lakehouse Query
description: Discovery, Trino/Spark routing, alias translation, cross-catalog joins, and Iceberg time travel against the BER Data Lakehouse — works in-pod and off-pod
scope: domain
---

# KBU Lakehouse Query

You are querying the BER Data Lakehouse (BERDL) — discovering databases and
tables, running interactive reads, joining across the personal catalog and
tenant catalogs, and reading Iceberg tables as of a prior snapshot. This skill
works in **both execution loci**: inside the BERDL JupyterHub pod (`kbhub`,
full read via Spark and Trino) and off-pod (read-only via REST).

It calls into `BerdlCapability` (`src/kbutillib/domains/kbase/berdl/capability.py`)
and its supporting modules — `naming.py`, `transports.py` — rather than
reimplementing routing or alias logic. Do not hand-roll alias translation or
name deduplication at a call site; both go through `naming.py`.

Read `kbu-dlsession` first if you have not already established which locus
you are running in — this skill assumes locus detection, the import map, and
token resolution are already understood.

## 1. Engine routing: Trino vs. Spark vs. REST

Three read paths, chosen by locus and intent, not by habit:

| Engine | When | Locus | Write? |
|---|---|---|---|
| **Trino** | Interactive reads, cross-catalog joins | in-pod only | **No — read-only by design.** Rejects `INSERT`, `CREATE`, `DROP`, and every other write statement. |
| **Spark** | Heavy ETL, anything that writes, Iceberg syntax Trino doesn't support (time travel, schema evolution) | in-pod only | Yes, via `BerdlCapability.load()` — never a raw `writeTo` (see `kbu-dlload`) |
| **REST** | Any read, off-pod | off-pod only | No — `OffPodTransport` defines no write method at all |

Trino's read-only-ness is a platform property, not something this skill
enforces — a write statement sent to Trino is rejected by the engine itself.
Do not attempt writes through `BerdlCapability.query()`; use `BerdlCapability.load()`
(off-pod: refused with guidance to run it in the pod).

Route through `BerdlCapability.query(sql, **kwargs)`:

```python
from kbutillib.domains.kbase.berdl.capability import BerdlCapability

cap = BerdlCapability()

# In-pod, default engine (Trino, connector='iceberg' — see the alias/connector
# trap below):
rows = cap.query("SELECT * FROM aiale.dataset1 LIMIT 10")

# In-pod, force Spark (needed for time travel / schema evolution syntax):
rows = cap.query("SELECT * FROM aiale.dataset1 LIMIT 10", engine="spark")

# Off-pod (locus is auto-detected; this is the same call):
rows = cap.query("SELECT * FROM aiale.dataset1 LIMIT 10")
```

`query()` never defaults the Trino connector to `'delta_lake'` — it always
passes `connector='iceberg'` explicitly. `get_trino_connection` itself
defaults to `'delta_lake'` (legacy); do not call it directly and take that
default, and do not add a new call site that omits `connector=`.

## 2. The alias trap: `my` is Spark-only

The personal-catalog alias is **engine-specific**, not a universal name:

- **Spark**: the literal string `"my"` (e.g. `my.dataset1`).
- **Trino**: no `my` alias exists — it requires the caller's own **username**
  (e.g. `{username}.dataset1`).

A query written for one engine and run unmodified against the other does not
raise an error you'd notice — it silently fails (wrong catalog, empty result,
or `TABLE_NOT_FOUND` that reads like a typo rather than an engine mismatch).

**Translation always goes through `naming.py`, never ad hoc at the call
site:**

```python
from kbutillib.domains.kbase.berdl.naming import to_trino_alias, to_spark_alias

# Have a Spark-style reference, need Trino:
trino_name = to_trino_alias("my.dataset1", username)   # -> "{username}.dataset1"

# Have a Trino-style reference, need Spark:
spark_name = to_spark_alias(f"{username}.dataset1", username)  # -> "my.dataset1"
```

Both functions only translate the alias segment (the catalog itself, or a
leading `my.`/`{username}.` prefix) and pass everything else through
unchanged, so it is safe to run them on any name without first checking
whether it references the personal catalog.

**Rule:** if you are constructing a query string that will run through both
engines (or you don't yet know which engine `BerdlCapability.query()` will
route it to), build it against one canonical form and translate via these
functions before sending — never string-replace `"my"` or the username by
hand.

## 3. The dual-name problem: dotted vs. underscored

The platform is in a dual-read window. `databases()` — and the raw platform
calls it wraps (`get_databases`, REST `databases/list`) — can return **both**
an Iceberg-style dotted name and a legacy Delta-style underscored name for
the **same logical dataset**: `aiale.dataset1` and `aiale_dataset1`;
`kbaseincubator.fitness` and `kbaseincubator_fitness`. Name alone does not
tell you which is which, and listing both naively makes it look like there
are twice as many datasets as there really are.

**Never call the raw platform database-list functions directly for
discovery.** Always go through `BerdlCapability.databases()` (or a
transport's `.databases()`), which routes every result through
`naming.normalize_databases()`:

```python
for db in cap.databases():
    print(db.name, "(Iceberg)" if db.is_iceberg else "(legacy Delta)",
          f"legacy alias: {db.legacy_alias}" if db.legacy_alias else "")
```

Each `NormalizedDatabase` is one deduplicated logical dataset:

- **`name`** — the preferred display name. Always the dotted (Iceberg) form
  when one was seen, otherwise the underscored (Delta) form.
- **`is_iceberg`** — `True` when `name` is the dotted form.
- **`legacy_alias`** — the underscored counterpart, if the platform also
  returned one for this same dataset. `None` when no counterpart was seen.

The governing rules, unconditionally:

- **Prefer the dotted (Iceberg) form** as the name you display and query
  against.
- **Mark the underscored form as legacy — do not hide it.** It stays
  reachable as `legacy_alias` on the same entry, not silently dropped.
- **Never present the two as unrelated datasets.** Don't emit `aiale.dataset1`
  and `aiale_dataset1` as two separate list entries; that misrepresents one
  dataset as two.

If you are told "I see `X.Y` and `X_Y` in a listing and don't know if they're
the same thing" — they are, by construction, whenever both came from
`normalize_databases()`. If they came from a raw, un-normalized platform
call, route that call through normalization before answering.

## 4. Discovery

Discovery works both engines, both loci — with different affordances.

**Via `BerdlCapability` (preferred, locus-transparent):**

```python
cap.databases()                    # list[NormalizedDatabase], deduplicated
```

`BerdlCapability` does not (yet) forward `tables()`/`table_schema()` at its
own top level; call them on the transport directly when you need them:

```python
transport = cap._get_transport()   # or construct InPodTransport()/
                                    # OffPodTransport() directly if you know
                                    # the locus already
transport.tables("aiale.dataset1")
transport.table_schema("aiale.dataset1", "some_table")
```

**In-pod, via raw `berdl_notebook_utils`, if you need something the transport
doesn't wrap:** `get_databases`, `get_tables`, and `get_table_schema` all
default to `return_json=True` and return a JSON **string**, not a list or
dict — iterating the default return value iterates characters. Always pass
`return_json=False` explicitly (the `InPodTransport` methods already do
this; a raw call does not unless you ask).

**Trino discovery statements**, once you have a `trino.dbapi.Connection`
(via `transport.trino_connection(connector="iceberg")` or
`cap.query(..., engine="spark")` is not needed for these — plain SQL):

```sql
SHOW CATALOGS;
SHOW SCHEMAS FROM aiale;
SHOW TABLES FROM aiale.dataset1;
DESCRIBE aiale.dataset1.some_table;

-- information_schema, for programmatic discovery:
SELECT table_schema, table_name
FROM aiale.information_schema.tables
WHERE table_schema = 'dataset1';

SELECT column_name, data_type
FROM aiale.information_schema.columns
WHERE table_schema = 'dataset1' AND table_name = 'some_table';
```

Remember the alias trap here too: `SHOW SCHEMAS FROM my` is wrong on Trino —
translate `my` to the resolved username first (§2).

## 5. Cross-catalog joins

Trino is the engine for joining the personal catalog against a tenant
catalog in a single query — this is exactly the "interactive reads,
cross-catalog joins" case Trino is for (§1). Resolve the alias before
writing the join (§2):

```python
from kbutillib.domains.kbase.berdl.naming import to_trino_alias

username = "..."  # resolved via governance / session context, not hardcoded
my_table = to_trino_alias("my.private_experiment", username)

sql = f"""
    SELECT p.*, t.annotation
    FROM {my_table} p
    JOIN aiale.dataset1.reference_table t
      ON p.feature_id = t.feature_id
"""
rows = cap.query(sql)
```

A query built this way is portable to a differently-resolved username
without editing the SQL — only the `to_trino_alias` call site needs the
current user.

## 6. Time travel and snapshot inspection (Iceberg-only)

Time travel is the **recovery path after a destructive overwrite** —
`BerdlCapability.load()`'s `overwrite` mode maps to `createOrReplace()`, a
full replace recoverable only through Iceberg snapshot history (see
`kbu-dlload`). Legacy Delta tables (the underscored form, §3) do not support
any of this.

Snapshot/history/time-travel queries need Spark SQL, not Trino — route
through `cap.query(sql, engine="spark")`:

```python
# Inspect available snapshots:
cap.query("SELECT * FROM aiale.dataset1.some_table.snapshots", engine="spark")

# Inspect the change history (which snapshot was current when):
cap.query("SELECT * FROM aiale.dataset1.some_table.history", engine="spark")

# Inspect the underlying data files of the current snapshot:
cap.query("SELECT * FROM aiale.dataset1.some_table.files", engine="spark")

# Read the table as of a specific snapshot id (recover a value from before
# an overwrite):
cap.query(
    "SELECT * FROM aiale.dataset1.some_table VERSION AS OF 1234567890123456789",
    engine="spark",
)

# Read the table as of a timestamp:
cap.query(
    "SELECT * FROM aiale.dataset1.some_table TIMESTAMP AS OF '2026-07-30 12:00:00'",
    engine="spark",
)
```

Typical recovery flow after a bad load: query `.history` to find the
`snapshot_id` that was current immediately before the destructive write,
then `VERSION AS OF` that id to read the prior state — and re-load from that
read if you need to restore it, since Iceberg time travel is read-only; it
does not itself roll the table back.

## 7. Off-pod limitation — read this before promising off-pod discovery

Two separate, honest caveats. Do not paper over either:

**1. The off-pod REST surface returns tenant catalogs only.** At design time,
`OffPodTransport.databases()` (via `KBBERDLUtils.get_database_list()`) never
returned the personal (`my`) catalog — only tenant catalogs like `aiale` and
`kbaseincubator` appeared. **Do not promise off-pod discovery of personal
tables.** If asked to list or query someone's personal-catalog tables
off-pod, say plainly that this is not known to work, rather than attempting
it silently and reporting an empty result as "no personal tables."

**2. Off-pod has not been proven end to end.** The REST route is live and
authenticates (verified in-pod) and is reachable without an SSH tunnel — the
tunnel is only needed for direct MinIO/S3 access, not this query path. But
**token resolution off-pod is unverified on a real off-pod machine**: the
precedence (`KBASE_AUTH_TOKEN` env var → `~/.kbase/token` → config, per
`tokens.py`) has been designed and unit-tested, not exercised against a live
off-pod session. Treat off-pod query results as provisional until validated
against a real off-pod machine, and say so if asked whether off-pod querying
"works" — it authenticates in principle; it has not been confirmed working
in practice off-pod.

## Privacy

AI-ALE and other tenant catalogs hold collaborator material. Do not route
query results — or SQL containing tenant data — through external services,
LLM tools that leave this machine, renderers, or pastebins.
