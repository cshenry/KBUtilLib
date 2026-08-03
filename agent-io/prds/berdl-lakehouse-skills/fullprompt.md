# PRD: BERDL Lakehouse Skills

**Status:** rendered 2026-08-01 from `/ai-design`
**Owning repo:** KBUtilLib
**Target tenants (initial):** `aiale`, `kbaseincubator`

---

## Problem Statement

Chris works with the BERDL (BER Data Lakehouse) platform from two places: inside
the BERDL JupyterHub pod (`kbhub` / `jupyter-chenry`), and from his laptop and
other platform machines. He needs to load data into shared tenant catalogs —
starting with AI-ALE and KBase Incubator — and to query, inspect, and administer
that data without re-deriving the platform's semantics every time.

Today there is no encoded knowledge of BERDL in the skill library. An agent asked
to "load this table into the AI-ALE tenant" would work from the published
`berdl_docs` user guides, and would fail — or worse, silently do damage — for
five specific reasons established empirically during design:

1. **Every code example in the guides fails outside a notebook.** The guides'
   central promise is that helper functions are "automatically imported, no
   imports needed." That is true only inside a notebook kernel, where the
   platform's `startup.py` injects the names. A skill runs in a terminal or
   subprocess and gets `ImportError` on the first call. The functions are not
   missing — they are namespaced into submodules the guides never document.

2. **The wrong credential fix damages other sessions.** There are two repair
   paths. One is non-rotating and safe. The other rotates S3 and Polaris
   credentials, invalidating credentials in use by other kernels, running
   scripts, and remote connections. The guides order them deliberately; nothing
   enforces that ordering.

3. **Correct isolation looks like breakage.** `AccessDenied` on a prefix that
   spans tenants is the platform working as designed, not a broken credential.
   An agent that misreads it reaches for the rotating fix in (2) and breaks live
   sessions — a plausible-looking wrong action with real blast radius.

4. **Cleanup is irreversible and easy to get half-right.** Iceberg has no
   `CASCADE`. Dropping a namespace requires purging every table first, and
   `DROP TABLE` without `PURGE` silently orphans the S3 data files.

5. **Tenant write access is an asynchronous human gate.** Read-only and
   read-write are *different groups*. Discovering mid-load that you only hold
   read-only means a Slack approval cycle, not a retry.

Separately, the installed platform package is materially ahead of its
documentation: it exposes namespace-level ACLs, a stewardship role tier, a v2
tenancy API, Spark cluster management, and a first-class MCP client family, none
of which appear in any of the ten user guides. Skills authored from the guides
alone would be both wrong and needlessly limited.

## Solution

Four locus-aware skills, backed by one deep capability module in KBUtilLib.

The skills carry the *encoded facts* — the import map, the credential escalation
ladder, the access-denial taxonomy, purge ordering, alias translation, and the
read-write gate. The capability module carries the *behavior* — locus detection,
name normalization, membership decoding, token resolution, and the write path —
behind a small, stable interface.

A single skill set, not two families. The facts are locus-independent; only the
transport and the write capability differ. Duplicating the facts across in-pod
and off-pod families would let them drift, which is the failure mode that
actually hurts.

Off-pod, writes are refused with guidance rather than emulated. There is no
Spark outside the pod, and pretending otherwise would produce a worse failure
than an honest one.

## User Stories

1. As Chris, I want to load a Spark DataFrame into an AI-ALE tenant namespace, so
   that collaborators on the tenant can query it.
2. As Chris, I want to load a directory of CSV/TSV/Parquet files into a tenant
   namespace with an enforced schema, so that the resulting table has correct
   types rather than inferred ones.
3. As Chris, I want the load to refuse to start if I only hold read-only access
   on the target tenant, so that I find out before staging data rather than
   after.
4. As Chris, I want the load to tell me — before writing — whether the target
   table already exists and whether the operation will create, append, or
   replace it, so that I do not silently destroy a prior version.
5. As Chris, I want to append to an existing table without the operation failing
   in a confusing way if the table does not yet exist, so that a re-runnable
   loader behaves predictably on first run.
6. As Chris, I want to verify after a load that the rows landed, by row count and
   by snapshot, so that success is confirmed rather than assumed.
7. As Chris, I want to run the same load command off-pod and be told clearly that
   writes require the pod, with the exact command to run there, so that I am not
   left guessing why nothing happened.
8. As Chris, I want to list the databases I can reach without seeing each dataset
   twice under two different names, so that I can tell what is actually there.
9. As Chris, I want to know which of two similarly-named databases is the
   authoritative Iceberg one and which is the legacy Delta one, so that I do not
   query stale data.
10. As Chris, I want to query a table interactively without spinning up Spark, so
    that exploration is fast.
11. As Chris, I want to write a query once and have it work whether it runs
    through Spark or Trino, so that the personal-catalog alias difference does
    not silently break it.
12. As Chris, I want to join a table in my personal catalog against a tenant
    table in one query, so that I can compare private work against shared data.
13. As Chris, I want to read a table as it was before a load, so that I can
    recover from a bad write.
14. As Chris, I want to see which tenants I belong to and at what permission
    level, so that I know what I can write to before I try.
15. As Chris, I want to request access to a tenant I do not belong to and be told
    that approval is a human step that may take hours, so that I plan around it
    rather than polling.
16. As Chris, I want to see who currently has access to a namespace holding AI-ALE
    data, so that I can confirm sensitive collaborator data is not over-shared.
17. As Chris, I want to see the stewards of a tenant, so that I know who to ask
    for access changes.
18. As Chris, I want any change to tenant membership or namespace access to
    require explicit confirmation, so that I cannot casually widen access to
    shared data.
19. As Chris, I want a single command that tells me whether my BERDL environment
    is healthy, so that I can distinguish a real problem from expected isolation.
20. As Chris, I want credential repair to try the non-rotating fix first, so that
    fixing my notebook does not break a long-running job elsewhere.
21. As Chris, I want an `AccessDenied` to be classified as either expected
    isolation or a real credential problem, so that I do not apply a damaging fix
    to a non-problem.
22. As Chris, I want to tear down a namespace completely, including its S3 data,
    so that I do not leave orphaned files I am still paying for.
23. As Chris, I want to be warned before a teardown that it is irreversible and
    told exactly what will be deleted, so that I can stop.
24. As an agent working on Chris's behalf, I want the correct import path for
    every platform helper, so that my first call does not raise `ImportError`.
25. As an agent, I want to detect which locus I am running in without being told,
    so that I route to the right transport automatically.

## Implementation Decisions

### Locus model

Two execution loci, detected at preflight, never assumed:

- **in-pod** — the BERDL JupyterHub pod. Identified by the presence of the
  platform environment (`KBASE_AUTH_TOKEN`, `SPARK_CONNECT_URL`, `S3_ACCESS_KEY`
  all set) together with an importable `berdl_notebook_utils`. Full read and
  write.
- **off-pod** — anywhere else. `berdl_notebook_utils` is a pod-only package and
  will not exist. Read and governance only, over REST.

> **CORRECTED 2026-08-03 by off-pod smoke verification.** Off-pod is **not a
> pod-independent fallback.** The REST surface is a thin remote client to the
> user's *own in-pod Spark Connect server*: all four `OffPodTransport` methods
> delegate to `/apis/mcp/delta`, which returns HTTP 500 — *"Spark Connect server
> for user 'chenry' did not respond to a session-create RPC within 15s"* — while
> the pod's session is unhealthy. The framing "REST/MCP works without Spark" is
> true only of **local** Spark; a **live pod session is still required**. A
> zombied pod therefore takes the off-pod read path down with it, and repairing
> the pod unblocks both loci. See `../berdl-smoke-verification/off-pod-results.md`.

Detection must test **importability of the platform package**, not only
environment variables, because the variables alone do not guarantee the package
is present.

### The import map (highest-value encoded fact)

The published guides write every call bare. Outside a notebook kernel each of
these requires an explicit import. This map was verified against the installed
package in-pod and is authoritative:

| Import path | Contents |
|---|---|
| `berdl_notebook_utils` (top level) | `get_spark_session`, `create_namespace_if_not_exists`, `get_databases`, `get_tables`, `get_table_schema`, `get_db_structure`, `get_trino_connection`, `refresh_spark_environment`, `table_exists`, `remove_table`, `list_namespaces`, `list_tables`, `read_csv`, `spark_to_pandas`, `display_df`, the `mcp_*` family, `get_minio_client`, `get_s3_client`, `get_governance_client`, tenancy v2 (`list_tenants`, `get_tenant_detail`, `get_tenant_members`, `add_tenant_member`, `remove_tenant_member`, `update_tenant_metadata`, `show_my_tenants`), stewardship (`assign_steward`, `remove_steward`, `get_tenant_stewards`, `get_my_steward_tenants`), clusters (`create_cluster`, `delete_cluster`, `get_cluster_status`) |
| `berdl_notebook_utils.governance` | All governance functions the guides describe: `get_my_groups`, `get_my_workspace`, `get_my_sql_warehouse`, `get_namespace_prefix`, `get_my_policies`, `get_my_accessible_paths`, `check_governance_health`, `get_credentials`, `request_tenant_access`, `list_available_groups`, admin operations (`list_users`, `list_groups`, `add_group_member`, `remove_group_member`, `create_tenant_and_assign_users`), the deprecated sharing functions, **and undocumented namespace ACLs** (`grant_namespace_access`, `revoke_namespace_access`, `list_namespace_access`) and Polaris operations (`ensure_polaris_resources`, `get_polaris_catalog_info`, `provision_polaris_user`, `rotate_polaris_credentials`, `rotate_credentials`, `regenerate_policies`) |
| `berdl_notebook_utils.spark` | `start_spark_connect_server`, `stop_spark_connect_server`, `get_spark_connect_status` |
| `berdl_notebook_utils.refresh` | `refresh_spark_environment`, `rotate_credentials` |
| `data_lakehouse_ingest` | `ingest` |

**Authoring rule:** the installed package is the authority, not the guides. Any
agent extending this work must introspect the package rather than transcribe
documentation. The guides are narrative context and are known to be stale in at
least two places (see Further Notes).

**Signatures:** `api-reference.md` in this PRD directory carries the harvested
argument shapes of every helper named above, captured by introspecting the
installed packages in-pod. It is authoritative over the guides and exists so the
build does not need to run on the pod. It also documents six traps the signatures
expose that appear in no guide — most importantly that `get_trino_connection`
defaults to `connector='delta_lake'` rather than Iceberg, and that
`get_databases`, `get_tables` and `get_table_schema` all default to returning a
JSON **string** rather than a list. Read it before wiring any transport.

### Modules to build

New subpackage under the KBase domain of KBUtilLib, `berdl/`:

- **`capability.py` — `BerdlCapability`, the deep module.** Small stable
  interface over substantial behavior:
  - `locus()` → `'in_pod' | 'off_pod'`
  - `databases()` → normalized, deduplicated database list
  - `memberships()` → `{tenant: 'rw' | 'ro'}`
  - `load(...)` → in-pod only; raises off-pod with actionable guidance
  - `query(sql)` → routed to Spark, Trino, or REST by locus and intent
- **`transports.py`** — `InPodTransport` (wraps `berdl_notebook_utils`) and
  `OffPodTransport` (pure `requests`; read-only by construction). The off-pod
  transport must not import `berdl_notebook_utils` at module scope.
- **`tokens.py`** — token resolution, precedence fixed as
  `KBASE_AUTH_TOKEN` env var → `~/.kbase/token` → config.
- **`naming.py`** — dotted/underscored normalization and the `my`/`{username}`
  alias translation.
- **`membership.py`** — `ro`-suffix decoding against the available-group list.

The existing `KBBERDLUtils` class is retained. The new subpackage is additive;
its REST client work may reuse `KBBERDLUtils` internally.

### Token resolution (fixes a verified gap)

`KBBERDLUtils` today reads only `~/.kbase/token` and raises
`No KBase token available` even when running in-pod, where `KBASE_AUTH_TOKEN` is
set and works. The capability layer must resolve `KBASE_AUTH_TOKEN` first. A
token value must never be logged, printed, or written to an artifact.

### Name normalization

The platform is in a dual-read window. The database list returns both Iceberg-
style dotted names and legacy Delta-style underscored names, and the same logical
dataset appears under both (`aiale.dataset1` and `aiale_dataset1`;
`kbaseincubator.fitness` and `kbaseincubator_fitness`). Name alone does not
disambiguate.

Rules:
- Prefer the dotted (Iceberg) form. Treat the underscored form as legacy.
- When both exist for the same logical dataset, present the dotted one and mark
  the underscored one as legacy rather than hiding it.
- Never present the two as unrelated datasets.

The personal catalog alias `my` is **Spark-only**. Trino requires `{username}`.
Translation happens in `naming.py`, not at call sites.

### Membership decoding

`get_my_groups()` returns a flat list in which read-only membership is encoded as
a name **suffix**, not a structured field: `enigmaro` means read-only on
`enigma`. The safe decode is: strip a trailing `ro` and test the remainder
against `list_available_groups()`. A naive `endswith("ro")` is wrong and must not
be used — it would misclassify any tenant whose real name ends in those letters.

Verified standing at design time: read-write on `aiale`, `kbaseincubator`,
`kbase`, `globalusers`, `ideas`, `kescience`; read-only on `enigma`,
`microbialdiscoveryforge`, `planetmicrobe`, `refdata`. Both initial targets are
read-write, so the load path is unblocked without an access request.

### Write path

All writes route through `data_lakehouse_ingest.ingest`, not raw `writeTo`,
because `ingest` applies schema enforcement in both source modes. Its signature
is `ingest(config, spark=None, logger=None, minio_client=None, dataframes=None)`.

Two source modes, selected by input type, both first-class:

- **DataFrame mode** — data already in Spark. Pass the frames via `dataframes=`,
  keyed by table name. This short-circuits the bronze read entirely: no MinIO
  staging, and the `paths` config section may be omitted. This is the default
  when a DataFrame is in hand.
- **Bronze mode** — files (CSV, TSV, JSON, XML, Parquet) in S3 or local storage.
  Requires `paths.bronze_base` and per-table `bronze_path`/`format`.

Config schema: required top-level keys are `dataset` and `tables`. Optional:
`tenant`, `is_tenant`, `pipeline_name`, `paths` (`bronze_base` required when
`paths` is present, `silver_base` optional), `defaults`. Each table requires
`name`; optional `comment`, `schema_sql` or `schema` (list of maps with
`column`/`name`, `type`, `nullable`, `comment`), `enabled`, `bronze_path`,
`format`. Config may be an inline dict — no file staging required.

Write semantics that the load skill must handle explicitly:
- Mode is `overwrite` or `append` only.
- **`append` to a non-existent table raises `ValueError`.** The loader must
  detect table existence and choose `overwrite` for first creation, so that a
  re-runnable load does not fail on its first execution.
- `overwrite` maps to `createOrReplace()` — a full replace, recoverable only via
  Iceberg snapshot time travel. Treat as destructive.
- `partition_by` accepts a string or a list.

**Preflight, before any data is staged or written:** resolve locus; refuse off-pod
with guidance; confirm read-write membership on the target tenant; resolve and
report whether the target table exists and whether the operation will create,
append, or replace.

**Postflight:** verify by row count and by reading the table's snapshot history,
and report the new snapshot.

### Off-pod write refusal

Off-pod, `load()` raises with: the reason (writes require a Spark session, which
exists only in the pod), the target machine (`kbhub`), and the exact command or
notebook cell to run there. It must not stage data, generate artifacts, or
dispatch work.

### Read path

- **Trino** for interactive reads and cross-catalog joins. Read-only by design;
  it rejects all writes.
- **Spark** for heavy ETL and anything that writes.
- **REST/MCP** off-pod. Note the REST surface returned only tenant catalogs at
  design time — the personal catalog did not appear — so off-pod discovery of
  personal tables should not be promised.

Time travel, snapshot inspection (`.snapshots`, `.history`, `.files`), and schema
evolution are Iceberg-only and belong to the query and load skills.

### Teardown

Iceberg (Polaris REST) does not support `CASCADE`. Namespace teardown is:
enumerate tables, `DROP TABLE ... PURGE` each, then `DROP NAMESPACE`. Omitting
`PURGE` orphans the S3 data files. Enumeration must request a Python list rather
than the default JSON string.

Teardown is destructive and irreversible. It requires explicit confirmation and
must first display exactly what will be deleted.

### Credential and access diagnostics

Escalation ladder, in order, and the order is the point:

1. **Non-rotating first** — `get_credentials()` then
   `start_spark_connect_server(force_restart=True)`, then a fresh session. This
   re-fetches current credentials **without rotating them**, so it cannot
   invalidate credentials in use elsewhere.
2. Kernel restart, if catalogs are still missing.
3. **Rotating** — `refresh_spark_environment()`. This rotates S3 and Polaris
   credentials and **will break other kernels, running scripts, and remote
   connections** using the old ones. Never the first move.
4. Server restart, then browser refresh.
5. Escalate to the BERDL platform team.

Before any of that, classify the symptom. These are **expected isolation, not
faults**, and must not trigger a credential fix:
- `AccessDenied` on a prefix spanning multiple tenants or users.
- `AccessDenied` listing the Spark job-logs bucket, which is write-only for users.
- A prefix listing that returns more than expected because `aws s3 ls` does no
  implicit trailing-slash matching — a query for `kbase` also matches
  `kbaseincubator`. This affects both initial target tenants directly.

Genuine credential drift looks different: the tenant browser never renders,
favorites are empty, the UI lags, or reads and writes return 403 on paths the
user does hold.

### Tenancy, ACLs, stewardship

Inspection is always available: `list_namespace_access`, `get_tenant_stewards`,
`get_tenant_members`, `list_tenants`, `get_my_groups`, `show_my_tenants`.

Mutation requires explicit confirmation: `grant_namespace_access`,
`revoke_namespace_access`, `assign_steward`, `remove_steward`,
`add_tenant_member`, `remove_tenant_member`.

Sharing doctrine: the `share_table` / `unshare_table` / `make_table_public` /
`make_table_private` family is **deprecated** and must not be emitted. Sharing is
achieved by creating in a tenant catalog, and refined with namespace ACLs. A model
trained on older documentation will reach for the deprecated functions; the skill
must actively steer away from them.

Access requests are an **asynchronous human approval step** via Slack, typically
same-day during business hours. The skill sets that expectation and does not poll.

### Skills

- **`berdl-session`** — locus detection, environment validation, the import map,
  the credential ladder, the access-denial taxonomy, and the virtualenv/kernel
  procedure including the `pip install --user` hazard. Foundation for the others.
- **`berdl-load`** — in-pod only. Preflight, source-mode routing, schema
  enforcement, write, verify. Owns namespace lifecycle including purge-ordered
  teardown and schema evolution.
- **`berdl-query`** — both loci. Discovery, Trino/Spark routing, alias
  translation, cross-catalog joins, time travel.
- **`berdl-tenant`** — both loci. Membership, access requests, ACLs, stewardship,
  admin operations, sharing doctrine.

### Deployment

`kbhub` is already a registered sync target in ClaudeCommands with
`domains: ['*']`, so a domain-scoped skill auto-subscribes; no new sync work is
required. **Skill registration must be performed from primary-laptop** — a Linux
register writes a non-portable home path and breaks macOS sync.

## Testing Decisions

Good tests here exercise external behavior — given this input, does the layer
produce the right decision — rather than asserting on internal call sequences.
The pure-logic modules are tested because their logic is deterministic, their
failures are silent, and the cost of a bug is high (a misclassified `ro` suffix
silently grants or denies a write; a wrong token precedence silently fails
in-pod). They require no network and run in ordinary CI.

Tested:

- **`naming.py`** — dotted/underscored pairing and deduplication; correct
  preference for the Iceberg form; legacy marking; `my` ↔ `{username}` alias
  translation in both directions.
- **`membership.py`** — `ro`-suffix decoding against a fixture group list,
  including the case where a tenant's real name ends in the suffix letters and
  must **not** be misclassified.
- **`tokens.py`** — precedence order env → file → config; correct behavior when
  earlier sources are absent or empty; no token value ever appears in output.
- **`capability.py`** — ingest-config construction for both source modes
  (DataFrame mode omits `paths`; bronze mode includes it); create-vs-append mode
  selection given table existence; off-pod `load()` raises with actionable
  guidance.

Not tested automatically: the two transports (require live BERDL) and the four
skills (verified by hand against `kbhub`).

Prior art in the codebase: GAA's `BerdlAdapter` is constructed with injectable
Spark and MinIO clients specifically so it can be exercised with mocks, and its
test module demonstrates the pattern. This PRD deliberately does not adopt the
mocked-transport layer, but that prior art is the reference if it is added later.

## Out of Scope

- Spark cluster provisioning (`create_cluster`, `delete_cluster`,
  `get_cluster_status`). Deferred to a later version.
- Notebook or script generation as an off-pod write path.
- Maestro dispatch of writes to `kbhub`.
- A live end-to-end smoke test requiring pod credentials.
- Mocked-transport tests.
- Migrating existing legacy Delta namespaces to Iceberg.
- Any change to the `berdl_docs` repository. Documentation corrections found
  during this work are recorded here, not submitted upstream.
- Retiring or refactoring the existing `KBBERDLUtils` class.

## Further Notes

**The published guides are stale in at least two verified places.** Both were
confirmed against the installed package in-pod:

1. `data_lakehouse_ingest` docstrings state it writes "Silver **Delta** tables."
   It does not. The implementation uses the Iceberg `writeTo` API with
   `append()` / `createOrReplace()`, and the package contains no Delta write path.
   The migration guide is correct here; the package's own docstring is wrong.
2. The guides present the helper surface as import-free. That holds only in a
   notebook kernel.

The underscored legacy namespaces are **migration history**, not a product of the
ingest path. An early hypothesis that `ingest()` was still emitting Delta tables
was investigated and disproved.

**Privacy.** AI-ALE holds collaborator material. The BERDL documentation's own
Claude Code integration section warns that query results sent through the MCP
server reach external servers. Tenant data should be treated with the same care
as other sensitive project content: do not route tenant contents through external
services, renderers, or pastebins.

**Off-pod is currently unproven end to end.** The REST route is live and
authenticates (verified in-pod), and it is reachable without an SSH tunnel — the
tunnel is needed only for direct MinIO/S3 access. But token resolution off-pod is
the gap described above, and the personal catalog did not appear in REST results.
The off-pod half of `berdl-query` should be validated against a real off-pod
machine before it is relied upon.

**Empirical grounding.** Every claim in this PRD marked as verified was checked
in-session on `kbhub` on 2026-07-31/2026-08-01 against the live platform and the
installed packages, not inferred from documentation.
