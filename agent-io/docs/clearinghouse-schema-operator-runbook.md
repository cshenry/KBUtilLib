# Clearinghouse Schema -- Operator Runbook

**PRD**: `clearinghouse-lake-1-schema` (KBDLJobRunningPrototype, on `wip`; not
generally reachable from off-pod worktrees -- treat this document as the
authoritative reference for the in-pod steps).

**Revised 2026-09-10 for `clearinghouse-lake-1b-partitioned-scheme`**, which
changed the table layout before OP2 ever ran. The partition specs and the
`result` slot key below are `-1b`'s. `bootstrap()` reads the live config in
`clearinghouse_schema.py`, so the DDL it emits is always current -- but the
expectations this document states are what you compare a dry-run report
against, and before this revision they described the pre-`-1b` layout. If you
find yourself reading a dry-run report that disagrees with the text here,
check `clearinghouse_schema.py` first: the code is the authority, this is a
description of it.

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

Do these four steps **in order**. OP0 is read-only reconnaissance; OP1 is
blocking: do not attempt OP2 or OP3 until it succeeds.

---

## OP0 -- read-only reconnaissance (do this first)

Before OP1, answer these four questions -- all read-only, none of them
writes, alters, or drops anything -- in **one attended pod session**, and
write every answer **verbatim** to
`~/Dropbox/Projects/AIAssistant/trigger-inbox/replies/`.

**Why OP0 has to be an attended, in-pod, human step, and not something
dispatched headlessly.** This exact reconnaissance was already attempted
headlessly once: trigger `004c00fb-1e73-4ebc-b6f0-85909c0c73f4`
(`jane` -> `albert` on `kbhub`, answered 2026-09-12T18:27Z, status
`"partial"`). Read that reply in full before you start --
`~/Dropbox/Projects/AIAssistant/trigger-inbox/replies/004c00fb-1e73-4ebc-b6f0-85909c0c73f4.json`
-- so you don't re-run an attempt that already failed the same way. It
found that two of the four questions below need either the conda
interpreter that has `berdl_notebook_utils` importable
(`/opt/conda/bin/python3.13`) or the MCP SQL-execution tool
(`query_delta_table`, `engine="spark"`), and **both require an
interactive tool-permission approval that no headless poll-wake session
can grant** -- the read-only MCP metadata tools were not gated, but none
of them substitutes (see (a) below). That is exactly why this is an
operator-runbook step rather than an automated one: the approval those
two paths need is a human sitting at an attended session clicking
"allow." **Do not re-dispatch that envelope expecting a different
headless answer -- answer these four from an attended pod session.**

a. **Catalog fixture: the verbatim output of `DESCRIBE TABLE EXTENDED` and
   of `SHOW CREATE TABLE`, against any existing partitioned Iceberg table
   on this cluster.** Why: the adapter's two parsers
   (`clearinghouse_bootstrap_adapter.py`) were written off-pod against two
   documented Spark catalog shapes; this is what confirms which one is
   real on this cluster. If it's neither, a third parser is a
   one-function addition against the module's existing tests, not a
   rewrite.
   **Status: ANSWERED 2026-09-12, and the answer is that this question has
   no subject on this cluster.** A first, headless attempt was blocked at
   the approval prompt described above; an attended pod session then
   settled it. **There is no partitioned Iceberg table anywhere on the
   accessible cluster** -- all tables in the `kbaseincubator` tenant plus
   401 tables across every `rw` tenant (`aiale`, `conwaylab`, `emsl`,
   `ideas`, `kbase`, `kescience`, `globalusers`) were read via pyiceberg
   `table.spec()`, and every one has an empty partition spec. So there is
   no existing DDL to validate the adapter's two parser shapes against,
   and there will not be until OP2 runs: the clearinghouse `entity` table
   (partitioned on `entity_type`) will be **among the first partitioned
   tables on this cluster**.
   **What to do instead -- this is now an OP2 follow-on, not an OP0
   blocker.** Immediately after OP2 creates `entity`, run
   `DESCRIBE TABLE EXTENDED` and `SHOW CREATE TABLE` against it and check
   the output against the adapter's parsers (see 2.3). If it matches
   neither documented shape, adding a third parser is a one-function
   change against the module's existing tests. Do not gate OP2 on a
   pre-existing example that does not exist.
   Full answer: `trigger-inbox/replies/op0-qb-rw-membership.json`.

b. **`BerdlCapability().memberships()` -- does this identity hold `'rw'`
   on the target tenant?** Why: `BerdlCapability.load()` raises
   `PermissionError` outright without it, and that error's own message
   calls the remedy "an asynchronous human approval step"
   (`capability.py`, `load()`'s preflight) -- so a `'no'` here is the
   **longest lead time in this entire sequence**, and is the reason OP0
   runs before everything else, including OP1.
   **Status: ANSWERED 2026-09-12 -- `'rw'` CONFIRMED. This gate is
   CLEAR.** A headless attempt gained nothing (`memberships()` sits behind
   the approval-gated interpreter, and no MCP tool exposes membership); an
   attended pod session then ran it and `memberships()` returned cleanly,
   no traceback, with `kbaseincubator: 'rw'` -- not merely `'ro'`. So
   `BerdlCapability.load()` will NOT raise `PermissionError` for a write
   to this tenant, and **the asynchronous human approval step is NOT in
   this sequence's critical path.**
   **The identity question is settled too, and settled the strong way.**
   The answer was obtained as `chenry` / polaris client
   `f1541da43077cb29`, derived from the pod's `KBASE_AUTH_TOKEN`. There is
   **no separate service account on kbhub** -- every pod process, attended
   or unattended, shares that one governance principal. So this is the
   identity OP2 itself will run as; it is not an interactive-login
   privilege that the operator step would fail to inherit. That
   distinction usually has to be checked separately and here it does not.
   Full answer: `trigger-inbox/replies/op0-qb-rw-membership.json`.

c. **Which `namespace` and `tenant`/`tenant_name` argument values actually
   address `kbaseincubator.clearinghouse`.** The MCP lists namespaces
   dotted (e.g. `kbaseincubator.genome_clearhouse`), while this codebase's
   two relevant calls take `namespace` and a tenant argument separately,
   and `BerdlCapability.load()` defaults `namespace="default"`. **DO NOT
   GUESS THIS** -- a wrong `namespace` is precisely the
   silent-overwrite-under-the-wrong-name failure `bootstrap()`'s required
   `namespace` argument exists to prevent (see 2.1's dry-run warning).
   **Status: answered from reading installed pod source, NOT confirmed
   live.** The 2026-09-12 reply: `BerdlCapability.load()`'s `tenant`
   parameter is used only for the read-write membership check
   (`target_tenant = tenant or dataset`, `capability.py`) and is never
   combined with `namespace` to build a table path; `namespace` instead
   goes straight through to `berdl_notebook_utils.table_exists`, whose
   installed body does a bare `f"{namespace}.{table_name}"`. Every
   existing `kbaseincubator` namespace observed live is addressed as the
   whole dotted string (`kbaseincubator.genome_clearhouse`,
   `kbaseincubator.fitness`, `kbaseincubator.pangea`). Putting those
   together, the likely shape is `namespace="kbaseincubator.clearinghouse"`
   (the whole dotted string) with a bare `tenant="kbaseincubator"`
   supplied separately, for the permission check only -- **but this is
   source-derived, not a confirmed live result.** Sections 2.1, 2.2, and
   2.3 below mark their `namespace` example values as unresolved
   placeholders for exactly this reason -- do not fill them in from this
   paragraph alone; confirm end to end (a real `table_exists` call, or
   2.1's dry run) before trusting it for the real run.

d. **What `berdl_notebook_utils.table_exists` returns for a namespace
   that does not exist** -- `False`, or a raise. Needed because of OP2.0b
   below: if it raises, the namespace must exist (OP2.0b) before anything
   calls `table_exists` or runs 2.1's dry run against it, or that call
   fails outright instead of reporting `'create'`.
   **Status: partially answered from source, not confirmed live.** The
   installed `berdl_notebook_utils.table_exists` has no `try`/`except` at
   all -- a bare `db_table = f"{namespace}.{table_name}"; return
   spark.catalog.tableExists(db_table)` -- so it will not itself swallow
   or translate anything. Whether this cluster's Iceberg-REST/Polaris
   catalog makes PySpark's own `tableExists()` raise or return `False` for
   a missing namespace is exactly what source-reading cannot settle, and
   the 2026-09-12 attempt could not run this against a real missing
   namespace to observe it. Confirm by observation, not inference.

**One live fact did land from the 2026-09-12 attempt**, independent of the
four questions above: `kbaseincubator.clearinghouse` is **absent** from
the live namespace listing, and a same-tenant sibling,
`kbaseincubator.genome_clearhouse`, **exists** and is unrelated (see OP2's
precondition list below -- that absence is perishable and must be
re-checked at OP2 run time, not assumed from this section).

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

**Preconditions -- confirm all four before running anything below.**

1. **The adapter exists and is wired.** `ClearinghouseBootstrapCapability`
   (`src/kbutillib/domains/kbase/berdl/clearinghouse_bootstrap_adapter.py`)
   is importable, and you are constructing it as shown in 2.0 -- not
   passing a bare `BerdlCapability()` into `bootstrap()`.
2. **The `-1b` runbook revision is on `main`.** The partition specs and
   table layout this document describes (`entity` on `entity_type`,
   `result` on `[source, entity_type]` in that order, `canonical_content`
   unpartitioned) are `clearinghouse-lake-1b-partitioned-scheme`'s;
   confirm that revision merged before trusting the expectations in
   2.1-2.3.
3. **OP0 confirmed `'rw'` membership** on the target tenant (OP0.b).
   **This one is already satisfied**: confirmed 2026-09-12 by an attended
   pod session, `kbaseincubator: 'rw'`, on the same governance principal
   OP2 runs as (see OP0.b). Tick it and move on -- but tick it by reading
   OP0.b, not by trusting this sentence. If OP0.b had come back anything
   other than a confirmed `'rw'`, you would stop here:
   `BerdlCapability.load()` will refuse with `PermissionError`, and its
   own remedy is "an asynchronous human approval step" you cannot
   shortcut from inside this runbook.
4. **Q6 -- "does `kbaseincubator.clearinghouse` exist?" -- has been
   RE-CHECKED at run time, not read off this document or off OP0's
   write-up.** This absence has now been observed **twice, independently,
   two days apart** -- and neither confirms the other still holds:
     - Albert, in-pod, 2026-09-10, read-only, no `COUNT(*)`, nothing
       written (trigger `64960fc9`; PRD `clearinghouse-lake-1c-pod-operator`
       names this trigger as the source of record and its own notes call
       the answer "PERISHABLE" in as many words).
     - OP0's own 2026-09-12 attempt (trigger
       `004c00fb-1e73-4ebc-b6f0-85909c0c73f4`), which independently found it
       absent from the live namespace listing.

   **Two confirmations two days apart make this answer sound more settled,
   not less perishable -- that inference is exactly backwards. Do not cite
   either date, or both together, as though repetition were durability;
   re-run the check.**
   That answer is perishable: if anything creates the namespace under the
   old, unpartitioned pre-`-1b` spec before OP2 runs -- a stray
   `create_namespace_if_not_exists` call (2.0b) against the wrong spec,
   or any other pod session bootstrapping it ahead of you -- correcting
   it costs a **multi-terabyte replay** (rebuilding the table from source
   under the corrected spec, see 2.2's partition-spec-refusal guidance),
   not a five-minute fix. **Do not confuse this with
   `kbaseincubator.genome_clearhouse`, which EXISTS, is unrelated, and
   holds `genome_quality` and `skani_distances`** -- a near-miss an
   operator skimming namespace names could land on by mistake.

### 2.0 -- the capability seam you will hit here (read this first)

`bootstrap()` (`clearinghouse_schema.bootstrap()`) requires its injected
`capability` argument to expose three things:

- `capability.load(*, dataset, tables, namespace, tenant=None, pipeline_name=None, ...)`
  -- same shape as `BerdlCapability.load()`.
- `capability.table_exists(name, namespace=namespace) -> bool` -- read-only.
- `capability.table_partition_spec(name, namespace=namespace) -> list[str] | None`
  -- read-only (`bootstrap()`'s own contract still accepts `None` as
  meaning "unpartitioned"; see below for what the concrete adapter
  actually returns).

**The stock `BerdlCapability` implements only `load()`.** Passing a bare
`BerdlCapability()` straight into `bootstrap()` raises `AttributeError`
(`'BerdlCapability' object has no attribute 'table_exists'`, wrapped by
`bootstrap()` into `BootstrapIndeterminateStateError`, since the lookup
happens inside its existence-check `try`/`except`) -- there is no
`table_exists`/`table_partition_spec` reader anywhere on `BerdlCapability`
itself.

**Why `bootstrap()` needs these two read-only probes at all, rather than
just calling `load()` and looking at what it reports.**
`BerdlCapability.load()` already resolves create-vs-append per table
internally (`select_write_mode()`), but it can only report what it found
*after* it has already written -- its per-table `existed_before`/
`effective_mode` report is produced in the same pass that calls
`data_lakehouse_ingest.ingest`. A caller that wants to **refuse** a write
*before* it happens -- specifically, before appending to a table whose
live partitioning disagrees with this module's config -- cannot use
`load()` alone as that probe, because calling it **is** the write.
`bootstrap()`'s `BootstrapPartitionSpecMismatchError` (2.2, below) is the
one place that safety exists, and it only works because these two probes
answer before any write is attempted.

**You do not need to build an adapter -- the prior phase of this PRD
already did.** Import and construct it like this:

```python
from kbutillib.domains.kbase.berdl.capability import BerdlCapability
from kbutillib.domains.kbase.berdl.clearinghouse_bootstrap_adapter import (
    ClearinghouseBootstrapCapability,
)

my_capability = ClearinghouseBootstrapCapability(BerdlCapability())
```

`ClearinghouseBootstrapCapability`
(`src/kbutillib/domains/kbase/berdl/clearinghouse_bootstrap_adapter.py`)
**wraps** a `BerdlCapability` -- it does not subclass it, and this module
never modifies `capability.py`, `transports.py`, or
`clearinghouse_schema.py`. It resolves one Spark session lazily (on first
use, not in `__init__`) and shares it between the `table_exists` probe
and the forwarded `load()` call, so the guard and the write it guards can
never end up looking at two different sessions. Concretely, on this
adapter: `table_exists(name, *, namespace)` (`namespace` is keyword-only)
delegates to `InPodTransport.table_exists(spark, name,
namespace=namespace)` -- the exact probe `BerdlCapability.load()` itself
uses -- rather than a hand-rolled query, so this guard can never disagree
with the write it guards; and `table_partition_spec(name, *, namespace)`
(also keyword-only) is annotated `-> list[str]` and **never returns
`None`** -- it either returns the live partition columns, `[]` for a
table it positively confirms is unpartitioned, or raises (see below). If
you need `spark` or `transport` overridden (to reuse a session you
already opened), the constructor takes both as optional keyword-only
arguments: `ClearinghouseBootstrapCapability(BerdlCapability(),
spark=my_spark, transport=my_transport)`.

**Hand-rolling another adapter is no longer the expected path.** Use
`ClearinghouseBootstrapCapability`; if it is genuinely insufficient for
something this runbook doesn't anticipate, raise that as its own issue
rather than writing a parallel adapter that the rest of this document
doesn't know about.

**The one rule an operator can defeat, and must not.** If
`table_partition_spec` raises `PartitionSpecUnparseableError`
(`clearinghouse_bootstrap_adapter.PartitionSpecUnparseableError`), that is
the adapter **refusing to guess** at a catalog response it does not
recognise -- it is not a bug to patch around. **The fix is to add a
parser for this cluster's real output** (a pure function alongside the
module's existing two, `_parse_describe_table_extended_partition_spec`
and `_parse_show_create_table_partition_spec`) -- **never to pass in a
stub that returns `[]`.** `canonical_content` is genuinely unpartitioned,
and `bootstrap()` treats both `[]` and `None` as "unpartitioned" -- a stub
that returns `[]` on a parse failure would silently pass
`canonical_content`'s check having determined nothing at all, defeating
the one safety check `bootstrap()` exists to provide. The error message
names the table, the namespace, and the first ~200 characters of the raw
catalog output, so you can write the missing parser in one round trip.

An `AttributeError` at this step (`'BerdlCapability' object has no
attribute 'table_exists'`) means you skipped the import above and passed
a bare `BerdlCapability()` into `bootstrap()` directly -- it is not a bug
in `bootstrap()`. Import and construct `ClearinghouseBootstrapCapability`
as shown above and pass that instead.

### 2.0b -- create the namespace (do this before the dry run)

**Nothing in the sanctioned write path creates a namespace.** `bootstrap()`
does not -- it only creates tables inside a namespace it assumes already
exists. `BerdlCapability.load()` goes straight from its membership
preflight to its per-table existence probe to the
`data_lakehouse_ingest.ingest()` call; there is no namespace-creation step
anywhere in between.
`InPodTransport.create_namespace_if_not_exists`
(`src/kbutillib/domains/kbase/berdl/transports.py`, ~line 169) exists, and
**nothing in this repo calls it** (`grep -rn create_namespace_if_not_exists
src/` finds only its own definition). Since OP0 found
`kbaseincubator.clearinghouse` absent from the live namespace listing (see
OP0 and the precondition list below -- **that finding is perishable and
must be re-checked here, not assumed**), **OP2 fails without this step**:
`table_exists`, `table_partition_spec`, and `load()` are all asking about
a namespace that does not exist yet.

Run this from an attended pod session, before 2.1's dry run:

```python
from kbutillib.domains.kbase.berdl.transports import InPodTransport

# NAMESPACE/TENANT_NAME are UNRESOLVED PLACEHOLDERS -- see OP0.c, and the
# matching note in 2.1 below. Fill them in from OP0's confirmed answer,
# not from memory, and not as a literal copy of the placeholder itself.
NAMESPACE = "<<OP0.c -- confirm before use>>"
TENANT_NAME = "<<OP0.c -- confirm before use>>"

transport = InPodTransport()
spark = transport.spark_session()
transport.create_namespace_if_not_exists(
    spark,
    namespace=NAMESPACE,      # UNRESOLVED -- see above.
    tenant_name=TENANT_NAME,  # NOTE THE NAME: this parameter is spelled
                              # `tenant_name` here, not `tenant` -- see
                              # the warning below.
    iceberg=True,             # already the default; explicit for clarity.
)
```

**Read both signatures yourself before you fill in `TENANT_NAME` -- the
two calls you make back to back spell the tenant argument differently,
and assuming they're the same name is a real trap.**
`InPodTransport.create_namespace_if_not_exists(self, spark,
namespace="default", tenant_name=None, iceberg=True)` takes `tenant_name`.
`BerdlCapability.load(...)` (which 2.2's real run calls, through the
adapter) takes `tenant`, not `tenant_name` -- there is no `tenant_name`
parameter anywhere on `BerdlCapability`. Passing the wrong keyword gets
you a `TypeError` at best; passing the right keyword with the wrong
*value* gets you a wrong-tenant permission check at worst. Use the
argument values OP0 established (OP0.c), not guessed ones, for both
calls -- and confirm them against OP0's own caveat that its finding is
source-derived, not live-confirmed.

**Why this is a separate operator step, not folded into `bootstrap()`.**
Creating the namespace is a **write** -- it is the act that commits the
tenant and the name -- and putting it inside `bootstrap()` would mean a
*dry run* could no longer be run without first creating something. That
would defeat the entire purpose of 2.1 below: a dry run is supposed to be
side-effect-free, and "silently create the namespace as a side effect of
checking whether one is needed" is exactly the kind of undisclosed write
this runbook's other warnings tell you not to accept from any step in
this sequence.

### 2.1 -- dry run first

Once you have a capability satisfying the contract above, run `bootstrap()`
with `dry_run=True` **before** the real run:

```python
from kbutillib.domains.kbase.berdl.clearinghouse_schema import bootstrap

# NAMESPACE/TENANT_NAME are UNRESOLVED PLACEHOLDERS, not confirmed values --
# see OP0.c. Source-derived reading (NOT live-confirmed): the whole dotted
# string "kbaseincubator.clearinghouse" for NAMESPACE, a bare
# "kbaseincubator" for TENANT_NAME. This module's own NAMESPACE constant
# (clearinghouse_schema.NAMESPACE == "clearinghouse", bare) is a DIFFERENT
# thing -- the top-level 'dataset' identifier, not this Iceberg-catalog
# namespace argument -- and must not be assumed to be the same string.
# Fill these in from OP0's confirmed answer, not from this comment.
NAMESPACE = "<<OP0.c -- confirm before use>>"
TENANT_NAME = "<<OP0.c -- confirm before use>>"

report = bootstrap(
    my_capability,       # your OP2.0 adapter, not a bare BerdlCapability()
    namespace=NAMESPACE,  # UNRESOLVED -- see above and OP0.c; do not run
                          # this with the placeholder still in place.
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
    namespace=NAMESPACE,  # same UNRESOLVED placeholder as 2.1 -- confirm
                          # against OP0.c before running; do not re-guess
                          # a literal here even if 2.1's dry run "worked".
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
live partition spec that disagrees with this module's config (as of
`clearinghouse-lake-1b`: `entity` is partitioned on `entity_type`, `result`
on `[source, entity_type]`, and `canonical_content` carries no `partition_by`
key at all -- bucketing it is deliberately deferred pending the `-3-transport`
and `-4-jobs` access patterns), `bootstrap()` raises
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
# NAMESPACE is the same UNRESOLVED placeholder as 2.1/2.2 -- OP0.c, not a
# literal you retype here from this document.
for table in ("entity", "canonical_content", "result"):
    print(table, spark.sql(f"DESCRIBE `{NAMESPACE}`.`{table}`").collect())
```

Confirm `entity_hash`'s reported type is `binary`, not `string`, in every
table that has the column (`entity`, `canonical_content`, `result`). If it
is `string`, **stop before loading any data**: drop the affected table(s),
adjust however this write path needs to be told to honor `BINARY` (a
pod-only detail this module deliberately does not guess at), recreate via
`bootstrap()`, and re-verify with this same check before proceeding.

**Second acceptance item, added after OP0.a: validate the adapter's
partition-spec parsers here, because this is the first chance anyone
gets.** OP0.a established there is no pre-existing partitioned Iceberg
table on this cluster, so `entity` (partitioned on `entity_type`) is
likely the first one. While you have the session open, capture the
verbatim output of both:

```python
print(spark.sql(f"DESCRIBE TABLE EXTENDED `{NAMESPACE}`.`entity`").collect())
print(spark.sql(f"SHOW CREATE TABLE `{NAMESPACE}`.`entity`").collect())
```

and check it against `clearinghouse_bootstrap_adapter.py`'s two parsers --
the `# Partitioning` / `Part 0` block and the `PARTITIONED BY (...)`
clause. A re-run of `bootstrap()` exercises this for real: it takes the
`'append'` branch, which calls `table_partition_spec`. If that raises
`PartitionSpecUnparseableError`, this cluster emits a third shape --
**add a parser for it; do not stub the method to return `[]`.** Record
the verbatim output either way, since nobody else has ever seen this
cluster's partitioned-table DDL.

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

**Known-defect note, task 914.** `_build_fixture_dataframe`'s Spark `Row`
construction used to be built from a hand-maintained keyword field list
that omitted `entity_type` from the `result` schema's eight declared
columns, which would have made every OP3 run fail its write step against
the live table the first time an operator tried it (filed as task 914).
**As of this revision that defect is already fixed** (commit `a4e332d`,
"fix: build parity-check fixture rows positionally from the declared
schema" -- an ancestor of this document's own base commit): rows are now
built positionally from the same parsed column list that builds the
schema, so a column is picked up automatically rather than needing a
keyword list kept in sync, and the invariant is regression-tested off-pod
in `tests/berdl/test_clearinghouse_schema.py::TestParityFixtureMatchesResultSchema`.
**OP3 is not currently blocked by task 914.** (This corrects the
design-time expectation for this task, which described the defect as
still open; read the code before trusting a description of it, here as
everywhere else in this document.) It does not block OP2 either way.

**Every fixture row's `source` carries the `parity-check/` prefix**
(`PARITY_SOURCE_PREFIX` in the fixture module) so these rows can never be
mistaken for real tool output, and can be found again later with
`WHERE source LIKE 'parity-check/%'`. **These rows are expected to remain
in the append-only `result` table permanently** -- this is expected and
harmless: they occupy their own
`(entity_hash, entity_type, result_type, source)` slots, distinct from any
real corpus's slots, and re-running this script
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
`BerdlCapability()`** means you skipped OP2.0's import -- construct
`ClearinghouseBootstrapCapability(BerdlCapability())` (from
`kbutillib.domains.kbase.berdl.clearinghouse_bootstrap_adapter`) and pass
that instead; see that section for the exact import and construction.

**A namespace-resolution warning on a dry run** (OP2.1) means stop and
verify the `namespace` argument before proceeding -- see that section for
why proceeding risks an overwrite.
