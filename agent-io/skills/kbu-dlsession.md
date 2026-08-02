---
name: KBU Lakehouse Session
description: Foundation skill for BERDL lakehouse sessions — locus detection, the verified import map, the credential escalation ladder, and the access-denial taxonomy
scope: domain
---

# KBU Lakehouse Session

You are an expert on establishing and diagnosing a working session against the
BER Data Lakehouse (BERDL) platform — inside the BERDL JupyterHub pod (`kbhub`
/ `jupyter-chenry`) and from other platform machines (primary-laptop, h100,
email-mac). This is the **foundation skill**: `kbu-dlload`, `kbu-dlquery`, and
`kbu-dltenant` all assume the facts here and do not re-derive them.

Everything here is empirically verified against the installed package on
`kbhub`, not transcribed from `berdl_docs`. The guides are known to be stale
in the two places called out below — where this skill and the guides
disagree, trust this skill.

Do not reimplement any of this logic. It is already encoded in
KBUtilLib's `src/kbutillib/domains/kbase/berdl/` package:

- `capability.py` — `BerdlCapability`: `locus()`, `databases()`,
  `memberships()`, `load()`, `query()`. Start here for anything programmatic.
- `transports.py` — `InPodTransport` (wraps `berdl_notebook_utils`, lazily
  binds `.governance`, `.spark`, `.refresh`) and `OffPodTransport`
  (read-only REST; never imports `berdl_notebook_utils`).
- `naming.py`, `membership.py`, `tokens.py` — pure logic (name
  normalization, `ro`-suffix decoding, token precedence).

```python
from kbutillib.domains.kbase.berdl.capability import BerdlCapability

berdl = BerdlCapability()
berdl.locus()          # 'in_pod' | 'off_pod'
berdl.databases()      # normalized, deduplicated
berdl.memberships()    # {tenant: 'rw' | 'ro'} — in-pod only
```

---

## 1. The Import Map (read this first)

**The single highest-value fact in this skill.** The published `berdl_docs`
guides write every call bare and claim the helpers are "automatically
imported, no imports needed." That claim is true **only inside a notebook
kernel**, where the platform's `startup.py` injects the names into the
global namespace. A skill invoked from a terminal, a script, or a subprocess
gets `ImportError` on its first call — the functions are not missing, they
are namespaced into submodules the guides never document.

This is the verified import map, checked against the installed package
in-pod. Treat it as authoritative over the guides:

| Import path | Contents |
|---|---|
| `berdl_notebook_utils` (top level) | `get_spark_session`, `create_namespace_if_not_exists`, `get_databases`, `get_tables`, `get_table_schema`, `get_db_structure`, `get_trino_connection`, `refresh_spark_environment`, `table_exists`, `remove_table`, `list_namespaces`, `list_tables`, `read_csv`, `spark_to_pandas`, `display_df`, the `mcp_*` family, `get_minio_client`, `get_s3_client`, `get_governance_client`, tenancy v2 (`list_tenants`, `get_tenant_detail`, `get_tenant_members`, `add_tenant_member`, `remove_tenant_member`, `update_tenant_metadata`, `show_my_tenants`), stewardship (`assign_steward`, `remove_steward`, `get_my_steward_tenants`), clusters (`create_cluster`, `delete_cluster`, `get_cluster_status`) |
| `berdl_notebook_utils.governance` | `get_my_groups`, `get_my_workspace`, `get_my_sql_warehouse`, `get_namespace_prefix`, `get_my_policies`, `get_my_accessible_paths`, `check_governance_health`, `get_credentials`, `request_tenant_access`, `list_available_groups`, `get_tenant_stewards`, admin operations (`list_users`, `list_groups`, `add_group_member`, `remove_group_member`, `create_tenant_and_assign_users`), the deprecated sharing functions (see §4 sharing doctrine in `kbu-dltenant`), and the **undocumented** namespace ACLs (`grant_namespace_access`, `revoke_namespace_access`, `list_namespace_access`) and Polaris operations (`ensure_polaris_resources`, `get_polaris_catalog_info`, `provision_polaris_user`, `rotate_polaris_credentials`, `rotate_credentials`, `regenerate_policies`) |
| `berdl_notebook_utils.spark` | `start_spark_connect_server`, `stop_spark_connect_server`, `get_spark_connect_status` |
| `berdl_notebook_utils.refresh` | `refresh_spark_environment`, `rotate_credentials` |
| `data_lakehouse_ingest` | `ingest` |

**Authoring rule:** the installed package is the authority, not the guides.
Anyone extending this map must introspect the package rather than transcribe
documentation — `agent-io/prds/berdl-lakehouse-skills/api-reference.md`
carries the harvested argument shapes for every helper named above,
including six argument-shape traps that appear in no guide (most
importantly: `get_trino_connection` defaults to `connector='delta_lake'`,
not Iceberg; `get_databases`, `get_tables`, and `get_table_schema` all
default to returning a JSON **string**, not a list).

`InPodTransport.__init__` performs exactly these four imports (top level,
`.governance`, `.spark`, `.refresh`) and binds them as instance attributes,
so any code going through `BerdlCapability`/`InPodTransport` never needs to
repeat this import dance by hand. Reach for a raw import only when writing
new transport code.

**Two places the guides are stale**, both verified against the installed
package:

1. `data_lakehouse_ingest` docstrings claim it writes "Silver **Delta**
   tables." It does not — the implementation uses the Iceberg `writeTo` API
   (`append()` / `createOrReplace()`) with no Delta write path. The
   migration guide is correct here; the package's own docstring is wrong.
2. The "no imports needed" claim above — true only in a notebook kernel.

---

## 2. Locus Detection: In-Pod vs Off-Pod

Two execution loci, detected at preflight, **never assumed**:

- **in-pod** — running inside the BERDL JupyterHub pod (`kbhub` /
  `jupyter-chenry`). Full read and write.
- **off-pod** — anywhere else (primary-laptop, h100, email-mac). Read and
  governance only, over REST. `berdl_notebook_utils` is a pod-only package
  and will not exist here.

**Detection must test importability of `berdl_notebook_utils`, not
environment variables alone.** The pod's environment variables
(`KBASE_AUTH_TOKEN`, `SPARK_CONNECT_URL`, `S3_ACCESS_KEY`) can all be set
without the package actually being installed — a variables-only check would
misclassify that case as in-pod and then fail on the first real call.

`BerdlCapability.locus()` implements exactly this check
(`berdl_notebook_utils_importable()` in `capability.py`, via
`importlib.util.find_spec`, which locates the package without importing —
and therefore without executing — it, so the check itself is safe to run
off-pod with no side effects and no network call):

```python
from kbutillib.domains.kbase.berdl.capability import BerdlCapability

locus = BerdlCapability().locus()   # 'in_pod' | 'off_pod'
```

Do not write a fresh `os.environ.get(...)`-only check. Call `.locus()` (or,
if you need the raw predicate, `berdl_notebook_utils_importable()`) so
detection stays in one place.

---

## 3. The Credential Escalation Ladder

There are two repair paths for a broken BERDL session: one is
**non-rotating and safe**, the other **rotates credentials and breaks other
live sessions**. The ladder below orders every repair step from safest to
most disruptive. **The order is the point** — nothing in the platform
enforces it, so it must be followed deliberately, and steps must not be
skipped or reordered.

1. **Non-rotating first.** Call `get_credentials()` (via
   `InPodTransport.credentials()`) to re-fetch current credentials, then
   `start_spark_connect_server(force_restart=True)` (via
   `InPodTransport.start_spark_connect_server(force_restart=True)`), then
   open a fresh session (`spark_session()` again). This re-fetches whatever
   credentials are currently valid **without rotating them**, so it cannot
   invalidate credentials in use elsewhere. Try this first, every time,
   before anything else on this list.
2. **Kernel restart**, if catalogs are still missing after step 1.
3. **Rotating — `refresh_spark_environment()`** (via
   `InPodTransport.refresh_spark_environment()`, or the equivalent
   `.refresh.rotate_credentials()` /
   `InPodTransport.rotate_credentials()`). This **rotates S3 and Polaris
   credentials and will break other kernels, running scripts, and remote
   connections that are still using the old ones.** It is never the first
   move, and it is not a step to reach for casually — only invoke it after
   steps 1–2 have been tried and catalogs are still missing.
4. **Server restart, then browser refresh**, if step 3 did not resolve it.
5. **Escalate to the BERDL platform team**, if the environment is still
   broken after step 4.

```python
from kbutillib.domains.kbase.berdl.transports import InPodTransport

t = InPodTransport()
t.credentials()                                    # step 1a: non-rotating
t.start_spark_connect_server(force_restart=True)    # step 1b
# ... open a fresh spark_session() and retest ...
# only if still broken:
# t.refresh_spark_environment()                     # step 3: ROTATING — breaks other sessions
```

Do not call `refresh_spark_environment()` or `rotate_credentials()` as a
first troubleshooting reflex, even though it is the more familiar-sounding
"refresh everything" call. Before reaching for *any* of these steps,
classify the symptom against §4 — most `AccessDenied` errors are not
credential problems at all.

---

## 4. The Access-Denial Taxonomy — Classify Before Fixing

**Classify the symptom before touching the credential ladder.** Misreading
expected isolation as breakage and reaching for the rotating fix (§3 step 3)
is the single most damaging mistake this skill exists to prevent — it
invalidates credentials other live kernels, scripts, and connections are
still relying on, to "fix" something that was never broken.

### Expected isolation — not faults, do not trigger a credential fix

- `AccessDenied` on a prefix that spans multiple tenants or users. This is
  the platform's tenant isolation working as designed.
- `AccessDenied` listing the Spark job-logs bucket — that bucket is
  **write-only for users** by design; being denied a `list` there is
  correct behavior, not a credential problem.
- A prefix listing that returns **more** than expected, because `aws s3 ls`
  does no implicit trailing-slash matching. A query for prefix `kbase` also
  matches `kbaseincubator` — this is not a leak or a misconfiguration, it is
  string-prefix matching. **This affects both of this PRD's initial target
  tenants directly** (`kbase` vs `kbaseincubator`): a listing scoped to
  `kbase` will also surface `kbaseincubator` objects, and that surplus is
  expected, not evidence of broken scoping. Always disambiguate with an
  explicit trailing slash (or an exact-prefix check) before concluding a
  listing is wrong.

### Genuine credential drift — looks different, and does warrant §3

- The tenant browser never renders.
- Favorites are empty.
- The UI lags noticeably.
- Reads and/or writes return `403` on paths the user **does** hold
  legitimate access to (not a cross-tenant path, not the job-logs bucket).

If the symptom matches the first list, do not touch the credential ladder —
the fix is understanding the scope you queried, not rotating anything. Only
symptoms matching the second list warrant walking §3, and even then start
at step 1, never step 3.

---

## 5. Virtualenv and Kernel Procedure

### The pip `--user` shadowing hazard

Running `pip install` **outside an activated virtualenv** does not fail —
it silently falls back to a `--user` install into `~/.local`. Because
`~/.local` sits ahead of the base image's site-packages on the interpreter's
import path, this **shadows the base image for the entire server**, not
just the current kernel. Symptoms are indirect and easy to misattribute:
the file browser breaks, Spark breaks, and other unrelated notebook
functionality degrades — none of which look, on their face, like a
packaging problem.

**It survives restarts.** A kernel restart, a server restart, even a pod
restart does not undo it, because the shadowing package is sitting in
`~/.local`, not in any ephemeral kernel state. It has to be found and
removed explicitly.

**Before installing anything:** confirm a virtualenv is actually active —
check that `pip -V` / `which pip` resolves inside the venv path, not the
base interpreter, before running `pip install`.

**Recovery, once shadowing is suspected:**

1. Identify the shadowing package and its install location:
   ```bash
   pip show <package>          # look at the "Location:" line
   ```
   A `Location:` under `~/.local/lib/pythonX.Y/site-packages` (rather than
   the venv's or the base image's site-packages) confirms a `--user`
   install is shadowing the base image.
2. Remove it — with **no** virtualenv active, since the `--user` install
   sits outside any venv and `pip uninstall` must run in the same context
   it was installed in to find it:
   ```bash
   pip uninstall <package>
   ```
   If `pip uninstall` cannot find it (environment mismatch), remove it
   directly:
   ```bash
   rm -rf ~/.local/lib/python*/site-packages/<package>*
   ```
3. Restart the kernel so the import path is rebuilt without `~/.local`
   ahead of the base image.
4. Reinstall correctly, if the package is actually needed, **inside an
   activated virtualenv**:
   ```bash
   source /path/to/venv/bin/activate
   pip -V   # confirm this now resolves inside the venv before installing
   pip install <package>
   ```

Never install directly against the pod's base interpreter, even for a
"just this once" package — the fix in step 4 is not optional convenience,
it is what prevents the hazard from recurring on the next kernel.

---

## 6. Related Skills

- `kbu-dlload` — in-pod only. Preflight, source-mode routing, schema
  enforcement, write, verify. Owns namespace lifecycle including
  purge-ordered teardown and schema evolution. Assumes the locus detection
  and credential ladder from this skill.
- `kbu-dlquery` — both loci. Discovery, Trino/Spark routing, alias
  translation, cross-catalog joins, time travel.
- `kbu-dltenant` — both loci. Membership, access requests, ACLs,
  stewardship, admin operations, sharing doctrine.
- `kbutillib-expert` — full KBUtilLib reference (composition architecture,
  all sub-utilities, config, job management).

**Privacy note:** AI-ALE tenant data holds collaborator material. Do not
route tenant query results, credentials, or session diagnostics through
external services, renderers, or pastebins — the BERDL documentation's own
Claude Code integration section warns that MCP-routed query results reach
external servers.
