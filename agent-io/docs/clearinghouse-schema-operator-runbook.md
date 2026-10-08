# Clearinghouse Schema -- Operator Runbook

**PRD**: `clearinghouse-lake-1-schema` (KBDLJobRunningPrototype, on `wip`; not
generally reachable from off-pod worktrees -- treat this document as the
authoritative reference for the in-pod steps).

**Revised 2026-09-17 for `clearinghouse-lake-1d-per-entity-type-tables`**,
which replaced the three-table scheme with FIFTEEN tables named by entity
type (`<entity_type>_<kind>`, type-first). The driver is a ~5000:1 row-count
skew across entity types (gene ~5B, protein ~1B, genome ~10M, function ~1M,
ontology_term ~1M): Iceberg table PROPERTIES (target file size, compaction,
sort order, snapshot expiry), query PLANNING scope, and COMMIT concurrency
are all table-level and cannot be tuned per partition, so a separate physical
table per entity type is the only place those knobs become per-type. The
partition specs below are `-1d`'s. `bootstrap()` reads the live config in
`clearinghouse_schema.py` (`table_configs()`), so the DDL it emits is always
current -- but the expectations this document states are what you compare a
dry-run report against. If you find yourself reading a dry-run report that
disagrees with the text here, check `clearinghouse_schema.py` first: the code
is the authority, this is a description of it.

## Why this document exists, and why it must be a human

The fifteen clearinghouse tables (`<entity_type>_<kind>` for each of the five
entity types and each of the three kinds `entity`/`content`/`result`) this
PRD defines live inside the BERDL JupyterHub pod (host `kbhub`), inside the
`kbaseincubator` tenant. No automated build agent can reach that pod:

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
   and there will not be until OP2 runs: the first partitioned clearinghouse
   table OP2 creates (one of the four `<type>_entity`/`<type>_content` tables
   on `standardizer_version`, or one of the five `<type>_result` tables on
   `source`) will be **among the first partitioned tables on this cluster**.
   **What to do instead -- this is now an OP2 follow-on, not an OP0
   blocker.** Immediately after OP2 creates the tables, run
   `DESCRIBE TABLE EXTENDED` and `SHOW CREATE TABLE` against any partitioned
   one and check the output against the adapter's parsers (see 2.3). If it
   matches neither documented shape, adding a third parser is a one-function
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
   **Status: ANSWERED AND CONFIRMED LIVE, 2026-09-12.** An attended pod
   session probed a namespace that really exists and got an unambiguous
   split -- exactly one form resolves:

       table_exists('genome_quality', namespace='kbaseincubator.genome_clearhouse')  -> True
       table_exists('genome_quality', namespace='genome_clearhouse')                 -> False

   So the **dotted, tenant-prefixed form is the one that addresses a
   namespace**, and the bare form does not resolve at all. That only one
   form returned True is itself the good outcome: had both resolved, the
   argument would not disambiguate and a wrong-namespace write would be
   undetectable from the call site.
   **The values are therefore settled: `namespace="kbaseincubator.clearinghouse"`
   (dotted) with `tenant="kbaseincubator"` supplied separately.** They are
   filled in throughout 2.0b, 2.1, 2.2 and 2.3 below -- as confirmed
   values, not placeholders. Full answer:
   `trigger-inbox/replies/op0-qcd-namespace-args.json`.
   The source-derived reasoning this confirmed, kept because it explains
   *why* the split falls where it does: `BerdlCapability.load()`'s `tenant`
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
   fails outright instead of reporting `'create'`. (It does not raise --
   see the status below.)
   **Status: ANSWERED AND CONFIRMED LIVE, 2026-09-12 -- it returns
   `False`, it does NOT raise.** Probed against the genuinely absent
   namespace:

       table_exists('entity', namespace='kbaseincubator.clearinghouse')  -> False
       stdout: Table kbaseincubator.clearinghouse.entity does not exist.

   The installed `berdl_notebook_utils.table_exists` has no `try`/`except`
   at all -- a bare `db_table = f"{namespace}.{table_name}"; return
   spark.catalog.tableExists(db_table)` -- so this is PySpark's own
   behaviour against this cluster's Iceberg-REST/Polaris catalog, which
   resolves a missing namespace as "table not found" rather than an error.
   **IMPLICATION, and it relaxes the ordering: 2.1's dry run CAN be run
   before OP2.0b creates the namespace.** It will report `'create'` for
   all fifteen tables rather than failing outright. Namespace creation stays
   its own deliberate write step; it simply is not a precondition of the
   dry run. Full answer:
   `trigger-inbox/replies/op0-qcd-namespace-args.json`.

**One live fact did land from the 2026-09-12 attempt**, independent of the
four questions above: `kbaseincubator.clearinghouse` is **absent** from
the live namespace listing, and a same-tenant sibling,
`kbaseincubator.genome_clearhouse`, **exists**. That sibling is now a
**SOURCE** the `lakehouse` adapter reads from (~5.8M genomes in
`genome_quality`; it also holds `skani_distances`), not an unrelated
namespace -- so a clearinghouse bootstrap that seeds genomes READS
`kbaseincubator.genome_clearhouse` and WRITES `kbaseincubator.clearinghouse`
**in the same run**. The two namespaces differ by only five characters and a
missing `in`; **never spell either at a call site -- take both from the
named constants `SOURCE_GENOME_CLEARHOUSE_NAMESPACE` and
`CLEARINGHOUSE_NAMESPACE` in `clearinghouse_schema.py`.** Reading the wrong
one returns the wrong table; writing the wrong one corrupts the source. (The
`clearinghouse` absence is perishable and must be re-checked at OP2 run time,
not assumed from this section.)

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
2. **The `-1d` runbook revision is on `main`.** The fifteen-table layout
   and partition specs this document describes (the four high-volume
   `gene_entity`/`protein_entity`/`gene_content`/`protein_content` on
   `standardizer_version`, all five `<type>_result` on `source`, and the
   remaining six unpartitioned) are
   `clearinghouse-lake-1d-per-entity-type-tables`'s; confirm that revision
   merged before trusting the expectations in 2.1-2.3.
3. **OP0 confirmed `'rw'` membership** on the target tenant (OP0.b).
   **This one is already satisfied**: confirmed 2026-09-12 by an attended
   pod session, `kbaseincubator: 'rw'`, on the same governance principal
   OP2 runs as (see OP0.b). Tick it and move on -- but tick it by reading
   OP0.b, not by trusting this sentence. If OP0.b had come back anything
   other than a confirmed `'rw'`, you would stop here:
   `BerdlCapability.load()` will refuse with `PermissionError`, and its
   own remedy is "an asynchronous human approval step" you cannot
   shortcut from inside this runbook.
4. **Q6 -- "what state is `kbaseincubator.clearinghouse` in?" -- has been
   RE-CHECKED at run time, not read off this document or off OP0's
   write-up.**

   **The expected answer changed on 2026-09-20, and an operator carrying
   the old one will misread a correct state as an alarm.** The 2026-09-20
   attended attempt ran 2.0b and created the namespace before hitting the
   seam bug in 2.2 (see 2.0, "the schema-only seam"). So:

   - **PRESENT and EMPTY** (`list_tables` -> `[]`) is now the CORRECT
     resumable state, not a red flag. Tick this precondition and resume
     at 2.2 -- **do not re-run 2.0b.**
   - **ABSENT** means something removed it since; run 2.0b, then 2.2.
   - **PRESENT and NON-EMPTY** is the one that stops you. STOP and
     establish which partition spec those tables carry before writing
     anything -- that is the multi-terabyte-replay hazard this
     precondition exists for.

   The original absence was observed **twice, independently, two days
   apart** -- and neither confirmed the other still held, which is exactly
   how it went stale:
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
   That answer is perishable: if anything creates the namespace under a
   superseded spec (the old three-table scheme, or the tables under any
   partitioning other than `-1d`'s) before OP2 runs -- a stray
   `create_namespace_if_not_exists` call (2.0b) against the wrong spec,
   or any other pod session bootstrapping it ahead of you -- correcting
   it costs a **multi-terabyte replay** (rebuilding the table from source
   under the corrected spec, see 2.2's partition-spec-refusal guidance),
   not a five-minute fix. **Do not confuse this with
   `kbaseincubator.genome_clearhouse`, which EXISTS, is the genome SOURCE
   the `lakehouse` adapter reads (holding `genome_quality` and
   `skani_distances`), and is NOT the write target** -- a near-miss an
   operator skimming namespace names could land on by mistake. The two
   names differ by five characters and one missing `in`, and a genome
   bootstrap touches BOTH in the same run: it reads `genome_clearhouse`
   and writes `clearinghouse`. That adjacency makes the confusion *more*
   dangerous now than when the source was unrelated, not less -- reading
   the wrong namespace returns the wrong table, and writing the wrong one
   corrupts a populated source. **Take both from the named constants
   (`SOURCE_GENOME_CLEARHOUSE_NAMESPACE`, `CLEARINGHOUSE_NAMESPACE` in
   `clearinghouse_schema.py`); never type either at a call site.**

### The fifteen tables this creates, and their partition specs

`bootstrap()` creates the tables `clearinghouse_schema.table_configs()`
emits -- fifteen of them, `<entity_type>_<kind>` (type-first) for each of
the five entity types (`genome`, `protein`, `gene`, `function`,
`ontology_term`) and each of the three kinds (`entity`, `content`,
`result`). **This list is a description of `table_configs()`; the module is
the authority. If a dry-run report disagrees with this list, the module
wins -- read `clearinghouse_schema.py` and treat this table as stale.**

| Table | Kind | `partition_by` |
|---|---|---|
| `genome_entity` | entity | _(none -- unpartitioned)_ |
| `protein_entity` | entity | `standardizer_version` |
| `gene_entity` | entity | `standardizer_version` |
| `function_entity` | entity | _(none -- unpartitioned)_ |
| `ontology_term_entity` | entity | _(none -- unpartitioned)_ |
| `genome_content` | content | _(none -- unpartitioned)_ |
| `protein_content` | content | `standardizer_version` |
| `gene_content` | content | `standardizer_version` |
| `function_content` | content | _(none -- unpartitioned)_ |
| `ontology_term_content` | content | _(none -- unpartitioned)_ |
| `genome_result` | result | `source` |
| `protein_result` | result | `source` |
| `gene_result` | result | `source` |
| `function_result` | result | `source` |
| `ontology_term_result` | result | `source` |

Only four tables partition on `standardizer_version` -- the high-volume
`gene`/`protein` `entity` and `content` tables, where a standardizer bump
is the axis worth pruning on. All five `<type>_result` tables partition on
`source` (format `<tool>/<version>`). The remaining six -- the `genome`,
`function`, and `ontology_term` `entity` and `content` tables -- carry no
`partition_by` key at all, their row counts (~1M-10M) being low enough that
a partition key buys nothing. For the unpartitioned tables the config omits
the `partition_by` key entirely rather than emitting a falsy value, so an
absent key unambiguously means "unpartitioned."

The `entity` and `result` kinds carry a GENERIC schema identical across all
five types. The `content` kind is TYPE-SPECIALIZED: each `<type>_content`
table carries the shared tail (`entity_hash BINARY`,
`standardizer_version STRING`, `observed_at TIMESTAMP`,
`ingest_batch_id STRING`) plus a different type-specific head
(protein/gene carry a `sequence`; `gene_content` also carries a NULLABLE
`protein_entity_hash BINARY`; `genome_content` carries assembly metadata
plus a `fasta_reference` POINTER to the sequence and never the sequence
itself, since 10M genomes inlined would be ~50TB).

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

### The schema-only seam -- what `load()` does when `bootstrap()` calls it

**This is the bug that blocked OP2 on 2026-09-20, and the fix changed
which code actually creates the tables.** Read it before 2.2, because the
expected output of 2.2 changed with it.

`bootstrap()` takes no `dataframes` and no `paths` parameters, so it has
no data source to offer and can never supply one -- every `load()` call it
makes is schema-only by construction. The adapter used to forward those
straight to `BerdlCapability.load()`, which always drives
`data_lakehouse_ingest.ingest()` and whose `build_ingest_config()` raises:

```
ValueError: build_ingest_config: bronze mode (no 'dataframes' given)
requires 'paths' with a 'bronze_base'.
```

Neither half was wrong on its own. Creating an empty table is a DDL
operation, not an ingest; the adapter is the seam where that translation
belongs, and it now does the translation:

- **Schema-only** (no `dataframes`, no `paths.bronze_base`) -- the adapter
  runs one `CREATE TABLE IF NOT EXISTS <ns>.<table> (<schema_sql>) USING
  iceberg [PARTITIONED BY (...)]` per table, on the same Spark session
  `table_exists` probed with. Column types come **verbatim** from
  `table_configs()`'s `schema_sql`, so `entity_hash BINARY` is *declared*
  to the catalog rather than inferred from data. Every statement is built
  before any is run, so a malformed table config creates nothing at all
  rather than the prefix of tables preceding it.
- **Data-bearing** (`dataframes`, or `paths` with a `bronze_base`) --
  forwarded to `BerdlCapability.load()` unchanged. The real corpus loads
  are unaffected, and `BerdlCapability`'s own invariant is intact: every
  *data* write still routes through `data_lakehouse_ingest.ingest`, never
  a raw `writeTo`. No row is written on the DDL path.

**What was deliberately NOT done, and why it matters to you.** No empty
DataFrame and no stub bronze path is fabricated to force the call through
`ingest()`. Either would let the column types be INFERRED, which is the
silent `BINARY` -> `STRING` demotion of `entity_hash` that 2.3 exists to
catch. If you find yourself tempted to hand-roll one at the console to get
past a failure here: that is the same shortcut, and 2.3 is downstream of
it.

**Still not verifiable off-pod**, same caveat as everything else in this
document: that this cluster's Iceberg catalog accepts `USING iceberg` DDL
and honours the declared `BINARY` and `PARTITIONED BY` is asserted by the
emitted statement, not measured. **2.3 is the step that measures it**, and
it is now more load-bearing than before, not less.

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
stub that returns `[]`.** The six unpartitioned tables (`genome_entity`,
`function_entity`, `ontology_term_entity`, `genome_content`,
`function_content`, `ontology_term_content`) are genuinely unpartitioned,
and `bootstrap()` treats both `[]` and `None` as "unpartitioned" -- a stub
that returns `[]` on a parse failure would silently pass one of those
tables' checks having determined nothing at all, defeating the one safety
check `bootstrap()` exists to provide. The error message
names the table, the namespace, and the first ~200 characters of the raw
catalog output, so you can write the missing parser in one round trip.

An `AttributeError` at this step (`'BerdlCapability' object has no
attribute 'table_exists'`) means you skipped the import above and passed
a bare `BerdlCapability()` into `bootstrap()` directly -- it is not a bug
in `bootstrap()`. Import and construct `ClearinghouseBootstrapCapability`
as shown above and pass that instead.

### 2.0b -- create the namespace (before the REAL run, not necessarily before the dry run)

> **ALREADY DONE as of 2026-09-20 -- check before you run this.** The
> attended pod session that day ran this step successfully;
> `kbaseincubator.clearinghouse` exists and is EMPTY (`list_tables` ->
> `[]`). It then hit the 2.2 seam bug (2.0, "the schema-only seam") and
> created no tables. If your Q6 re-check (precondition 4) finds the
> namespace present and empty, **skip this section and resume at 2.2.**
> Everything below applies only if the namespace is genuinely absent.

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
must be re-checked here, not assumed**), **OP2's real run fails without
this step.**

**Be precise about WHERE it fails, because OP0.d settled this and the
obvious guess is wrong.** `table_exists` does NOT fail on an absent
namespace -- it was probed live against `kbaseincubator.clearinghouse`
and returned `False` cleanly, no exception. So the read-only probes are
fine, and `bootstrap()`'s dry run is fine: it will simply report
`'action': 'create'` for all fifteen tables, which is the correct answer.
The failure comes later, at the `data_lakehouse_ingest.ingest()` call the
real run makes into a namespace that is not there.

**So the ordering is a choice, and the better one is dry run first.**
2.1 is side-effect-free and costs nothing, and running it before you
create anything tells you what `bootstrap()` intends while the namespace
is still absent -- which is also the state in which a `'create'` for all
fifteen tables is unambiguously right rather than something you have to
reason about. Create the namespace after the dry run reads clean, and
before 2.2.

Run this from an attended pod session:

```python
from kbutillib.domains.kbase.berdl.transports import InPodTransport

# NAMESPACE (dotted) is used by table_exists, table_configs and BerdlCapability.load().
# Do NOT shorten it for those calls -- OP0.c confirmed the bare form returns False
# for table_exists. The create_namespace_if_not_exists call below is DIFFERENT:
# it prepends tenant_name as the catalog internally, so it takes the bare child name.
NAMESPACE = "kbaseincubator.clearinghouse"  # dotted -- for table_exists / load()
TENANT_NAME = "kbaseincubator"

transport = InPodTransport()
spark = transport.spark_session()
transport.create_namespace_if_not_exists(
    spark,
    namespace="clearinghouse",  # BARE CHILD -- not NAMESPACE (dotted). This function
                                # computes full_ns = f"{tenant_name}.{namespace}",
                                # so passing the dotted form yields
                                # kbaseincubator.kbaseincubator.clearinghouse ->
                                # NoSuchNamespaceException. Confirmed OP2 2026-09-20.
    tenant_name=TENANT_NAME,    # NOTE THE NAME: this parameter is spelled
                                # `tenant_name` here, not `tenant` -- see
                                # the warning below.
    iceberg=True,               # already the default; explicit for clarity.
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

# CONFIRMED LIVE by OP0.c on 2026-09-12. Note that this module's own
# NAMESPACE constant (clearinghouse_schema.NAMESPACE == "clearinghouse",
# bare) is a DIFFERENT thing -- the top-level 'dataset' identifier, not
# this Iceberg-catalog namespace argument. The bare form was probed and
# returned False; it does not address a namespace. Do not substitute it.
NAMESPACE = "kbaseincubator.clearinghouse"
TENANT_NAME = "kbaseincubator"

report = bootstrap(
    my_capability,       # your OP2.0 adapter, not a bare BerdlCapability()
    namespace=NAMESPACE,  # dotted, confirmed live -- see OP0.c.
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
    namespace=NAMESPACE,  # same confirmed value as 2.1 (OP0.c).
    dry_run=False,
)
print(report)
```

**Expected output, first (creating) run** -- every table's report entry has
`'exists': False` and `'action': 'create'`, and `report['load_result']` is
the adapter's schema-only report: `{'schema_only': True, 'dataset': ...,
'namespace': ..., 'tables': [...]}`, one entry per table carrying its
`'name'`, `'operation': 'create_if_not_exists'`, and the exact
`'statement'` that was executed for it.

**Capture `report['load_result']['tables']` before moving on.** Each
entry's `'statement'` is the DDL actually run, so 2.3 can compare what the
catalog reports against what was asked for, without re-deriving it from
`table_configs()`. Since the 2026-09-20 fix this is the only record of the
DDL that created these tables.

**Expected output, a re-run against the same namespace (idempotent)** --
every table's report entry has `'exists': True`, `'actual_partition_by'`
matching `'expected_partition_by'`, and `'action': 'append'`. The
schema-only `load_result` still reports `'create_if_not_exists'` per
table, and `IF NOT EXISTS` makes each statement a no-op against a table
that already exists -- which is correct, since `bootstrap()` has no rows
to append in the first place and has already refused the whole batch if
any live spec disagreed. No table is overwritten and no table's
partitioning is re-specced.

**How to read a partition-spec refusal.** If a table already exists with a
live partition spec that disagrees with this module's config (as of
`clearinghouse-lake-1d`: the four high-volume tables `gene_entity`,
`protein_entity`, `gene_content`, `protein_content` on
`standardizer_version`; all five `<type>_result` on `source`; and the
remaining six `entity`/`content` tables unpartitioned -- see the
fifteen-table list above), `bootstrap()` raises
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

### 2.3 -- acceptance step: confirm `entity_hash` has its declared type

> **Superseded type, 2026-09-21.** `entity_hash` is now declared
> `STRING` (lowercase hex), not `BINARY` -- see OP2R. Where this section
> says `BINARY` below, check for `string`. The reasoning about checking
> one table per kind still holds.

**Do this immediately after table creation succeeds, before any real
corpus is loaded.** `clearinghouse_schema.py`'s own module docstring flags
this explicitly: `entity_hash` is declared as bare `BINARY` in this
module's `schema_sql`, but whether the write path actually honors that
declaration -- as opposed to silently creating a `STRING` column -- is
**not verifiable off-pod** and is not asserted as fact anywhere in the
earlier phases' code or tests.

**The 2026-09-20 seam fix narrowed this risk without removing it, and the
check is unchanged.** Table creation now emits `CREATE TABLE ... USING
iceberg` DDL carrying `entity_hash BINARY` literally (2.0, "the
schema-only seam"), so the type is *declared* to the catalog rather than
inferred by `data_lakehouse_ingest` from data. That removes the inference
step, which was the likeliest demotion path -- but whether this cluster's
Iceberg catalog honours a declared `BINARY` is still unmeasured, and the
DDL statement itself was emitted by code no on-pod run has yet exercised.
**Run this step. Do not skip it because the type is now declared**; a
declaration the catalog quietly ignores looks identical from here to an
inference that guessed wrong, and both cost the same replay. This is
the one step where a silently-wrong type gets caught, and it must be
caught here: discovering *after* the real annotation corpus has been
loaded that `entity_hash` is actually `STRING` means every already-written
row's hash has to be re-encoded (or the table rebuilt from source) at full
corpus scale, instead of a five-minute fix against empty tables.

`entity_hash` is a `BINARY` column on all fifteen tables (it leads the
GENERIC `entity` and `result` schemas and is the first column of every
type-specialized `content` schema), so the risk that this write path
silently demotes it to `STRING` is the same on every table. Check at least
one table of EACH KIND -- one `<type>_entity`, one `<type>_content`, one
`<type>_result` -- since the three kinds carry different `schema_sql`
fragments through the write path and a demotion could in principle hit one
kind's DDL and not another's. Checking all fifteen is fine too and costs
only more `DESCRIBE`s; the minimum bar is one per kind.

Run, against at least one table of each kind (using whichever introspection
this cluster's Iceberg catalog exposes -- `DESCRIBE`, `SHOW CREATE TABLE`,
or Spark's catalog API all work):

```python
# NAMESPACE is the same confirmed value as 2.1/2.2 (OP0.c):
# "kbaseincubator.clearinghouse", dotted. One table of each kind; extend
# the list to all fifteen if you prefer belt-and-braces.
for table in ("genome_entity", "genome_content", "genome_result"):
    print(table, spark.sql(f"DESCRIBE `{NAMESPACE}`.`{table}`").collect())
```

Confirm `entity_hash`'s reported type is `binary`, not `string`, on every
table you check. (On `gene_content` the same check applies to
`protein_entity_hash`, the other `BINARY` column, if you inspect that
table.) If it is `string`, **stop before loading any data**: drop the
affected table(s), adjust however this write path needs to be told to honor
`BINARY` (a pod-only detail this module deliberately does not guess at),
recreate via `bootstrap()`, and re-verify with this same check before
proceeding.

**Second acceptance item, added after OP0.a: validate the adapter's
partition-spec parsers here, because this is the first chance anyone
gets.** OP0.a established there is no pre-existing partitioned Iceberg
table on this cluster, so one of the partitioned clearinghouse tables is
likely the first one. Pick any partitioned table -- e.g. `gene_entity`
(partitioned on `standardizer_version`) or `genome_result` (partitioned on
`source`). While you have the session open, capture the verbatim output of
both:

```python
TABLE = "gene_entity"  # any partitioned table: the four *_entity/*_content
                       # on standardizer_version, or any *_result on source.
print(spark.sql(f"DESCRIBE TABLE EXTENDED `{NAMESPACE}`.`{TABLE}`").collect())
print(spark.sql(f"SHOW CREATE TABLE `{NAMESPACE}`.`{TABLE}`").collect())
```

and check it against `clearinghouse_bootstrap_adapter.py`'s two parsers --
the `# Partitioning` / `Part 0` block and the `PARTITIONED BY (...)`
clause. A re-run of `bootstrap()` exercises this for real: it takes the
`'append'` branch, which calls `table_partition_spec`. If that raises
`PartitionSpecUnparseableError`, this cluster emits a third shape --
**add a parser for it; do not stub the method to return `[]`.** Record
the verbatim output either way, since nobody else has ever seen this
cluster's partitioned-table DDL.

### 2.4 -- create the three cross-type UNION ALL views (after table creation)

**Do this only after 2.2's real run has created all fifteen tables and
2.3's acceptance checks pass.** The fifteen-table split fragments what used
to be a single cross-type query surface, so this PRD restores it with three
`UNION ALL` views -- `all_entity`, `all_content`, `all_result` -- each
unioning the five per-type tables of one kind.
`clearinghouse_schema.union_view_sql(kind, *, fqn_prefix=...)` emits the
`CREATE OR REPLACE VIEW ... AS <SELECT> UNION ALL ...` DDL text; like the
rest of that module it is pod-free and creates nothing itself, so an
operator runs the emitted SQL from an attended pod session:

```python
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    TENANT,
    union_view_sql,
)

# The per-type tables live under the tenant-qualified prefix. TENANT is
# "kbaseincubator"; the views are created alongside their tables in the
# clearinghouse namespace.
FQN_PREFIX = f"{TENANT}.clearinghouse"

for kind in ("all_entity", "all_content", "all_result"):
    ddl = union_view_sql(kind, fqn_prefix=FQN_PREFIX)
    print(ddl)
    spark.sql(ddl)
```

- `all_entity` and `all_result` union EVERY column (`SELECT *`), because
  their five per-type tables are schema-identical (the `entity` and
  `result` kinds carry a GENERIC schema across all five types).
- `all_content` CANNOT union every column: the five `<type>_content` tables
  have deliberately divergent type-specialized columns. It selects ONLY the
  columns common to all five (`entity_hash`, `standardizer_version`,
  `observed_at`, `ingest_batch_id`) plus a literal `entity_type`
  discriminator, and reconciles none of the type-specialized columns.

**These views are metadata-only and safe to drop and recreate.** A view
carries no data of its own -- it is a stored `SELECT` over the physical
tables -- so dropping one, or re-running `CREATE OR REPLACE VIEW`, costs
nothing and requires **no multi-terabyte replay**. This is the one step in
OP2 that is freely reversible: if a view is wrong, drop it and re-emit it
from `union_view_sql()`. That is the opposite of the physical tables, whose
partitioning is baked in at creation and correctable only by replay (2.2).

---

## OP2R -- rebuild the fifteen tables with a STRING `entity_hash` (2026-09-21)

**Run this once, before OP3, if the live tables were created under the
old `BINARY` schema** -- which the tables created on 2026-09-20 were.

**Why.** OP2 created the tables through Spark DDL, which accepts `BINARY`,
and 2.3 verified the catalog honoured it. But every *write* goes through
`BerdlCapability.load()` -> `data_lakehouse_ingest.ingest`, which
re-parses `schema_sql` and has no `BINARY` in its type map: OP3 on
2026-09-21 failed with `Unsupported data type 'BINARY' in schema_sql`,
and **no row can be written to a `BINARY` table through the sanctioned
path** (dev 1219). Chris decided on 2026-09-21 to store the hash as the
64-character lowercase hex string the standardizers already emit.
`entity_hash` on all fifteen tables and `gene_content.protein_entity_hash`
are now `STRING`. The cost accepted is doubled hash width.

**This drops fifteen production tables. It is safe ONLY because they are
empty,** and the procedure below refuses to drop anything unless it has
positively counted zero rows in every one of them. Do not work around that
check. If any table holds rows, STOP: that is a replay decision, not a
runbook step.

**Precondition: the STRING schema is the code you are running.**

```python
import kbutillib
from kbutillib.domains.kbase.berdl.clearinghouse_schema import table_configs
print(kbutillib.__file__)
assert all("BINARY" not in c["schema_sql"].upper() for c in table_configs()), \
    "This checkout still declares BINARY -- Dropbox has not caught up. Stop."
print("schema is STRING -- proceed")
```

**R.1 -- count every table. Refuse unless all fifteen are empty.**

```python
from kbutillib.domains.kbase.berdl.clearinghouse_schema import table_configs

NS = ["kbaseincubator", "clearinghouse"]
def fqn(name): return ".".join(f"`{p}`" for p in [*NS, name])

counts = {}
for c in table_configs():
    counts[c["name"]] = spark.sql(f"SELECT COUNT(*) AS n FROM {fqn(c['name'])}").collect()[0]["n"]
print(counts)
assert len(counts) == 15, f"expected 15 tables, counted {len(counts)}"
assert all(n == 0 for n in counts.values()), "NOT EMPTY -- STOP. Do not drop."
print("all fifteen empty -- safe to drop")
```

**`assert len(counts) == 15` above is HISTORY, not a step to re-run.** It
was correct when OP2R ran on 2026-09-21 and it is left here verbatim as
the record of what was executed that day. It predates the parameter-set
registry: `table_configs()` now returns **sixteen** configs (the fifteen
per-entity-type tables plus `parameter_set`), so that assertion would fail
today. Do not "fix" it in place and do not re-run this section to satisfy
it -- OP2R is a completed, dated one-off. The current-contract equivalent
is OP-C1's own precondition check below, which asserts sixteen configs and
the presence of `parameter_set`.

A `TABLE_OR_VIEW_NOT_FOUND` here for any table is also a STOP: it means
the namespace is not in the state this runbook assumes, and dropping the
rest would leave you guessing which tables were ever there.

**R.2 -- drop the three views, then the fifteen tables.** Views first,
since they reference the tables.

```python
for view in ("all_entity", "all_content", "all_result"):
    spark.sql(f"DROP VIEW IF EXISTS {fqn(view)}")
for c in table_configs():
    spark.sql(f"DROP TABLE IF EXISTS {fqn(c['name'])}")
print(spark.sql("SHOW TABLES IN `kbaseincubator`.`clearinghouse`").collect())
```

The final listing should be empty. **Do not drop the namespace** -- it
stays, empty, and is the correct state for R.3. The names come from
`table_configs()` rather than being typed by hand, so this cannot drop a
table the module does not own.

**R.3 -- recreate.** Run 2.1 (dry run: all fifteen `'action': 'create'`)
and then 2.2 exactly as written above. Capture
`report['load_result']['tables']` -- each `'statement'` should now read
`entity_hash STRING`, and none should contain `BINARY`.

**R.4 -- acceptance.** Run 2.3, now checking that `entity_hash` (and
`gene_content.protein_entity_hash`) report `string` on at least one table
of each kind, and that the partition specs are unchanged. Then 2.4 to
recreate the three views. Then re-run the 2.1 dry run once more: all
fifteen should now report `exists=True`, `'action': 'append'`, and
`actual_partition_by` equal to `expected_partition_by` -- the proof that
the partition-spec reader still works against the recreated tables.

Then OP3.

## OP3 -- run the parity check

`scripts/clearinghouse_parity_check.py` proves that `current_state_sql()`
behaves identically on the real Spark/Iceberg engine as it does on the
DuckDB surrogate the CI suite uses. Run it from inside the pod, after OP1
and OP2 have both succeeded, and after **OP-C2** has created and
bootstrapped the parity namespace:

```bash
python scripts/clearinghouse_parity_check.py \
    --namespace kbaseincubator.clearinghouse_parity
```

`--namespace` is REQUIRED and has no default: a default would be the
namespace fixture rows land in whenever somebody forgets the flag, and the
only namespace anyone would default to is production. Exit codes are 0
(all properties pass), 1 (a property failed), 2 (run off-pod) and 3
(`--namespace` was production, or malformed).

What it does:

1. Builds a small, explicitly-typed Spark DataFrame from
   `kbutillib.domains.kbase.berdl.clearinghouse_parity_fixture` --
   **the exact same fixture rows** `tests/berdl/test_clearinghouse_derivation.py`
   imports and inserts into its DuckDB surrogate (see that module's
   docstring). Sharing one module, rather than hand-retyping a lookalike
   fixture in this script, is what keeps the two from silently drifting
   apart -- a parity check run against a fixture that no longer matches
   what the DuckDB tests exercise would prove nothing.
2. Appends those rows to the real, live per-type `<entity_type>_result`
   tables through `BerdlCapability.load()` -- the same sanctioned write path
   OP2 uses -- never a raw `pyiceberg` write (see "if something goes wrong"
   below). After the fifteen-table split there is no single `result` table;
   each fixture row lands in the `<entity_type>_result` table resolved by
   `clearinghouse_schema.table_name` for its `entity_type`.
3. Runs the real `current_state_sql()` SQL text against the live table via
   Spark, once per property, and asserts the same properties the DuckDB
   tests assert -- one per entry in `PARITY_CASES`, which is **eight** as
   of this revision (the six below plus `genome_fasta_invariance` and
   `parameter_set_forks_slot`). Count the PASS lines against
   `len(PARITY_CASES)` in the fixture module rather than against a number
   typed here, which is how this list came to be stale in the first place:
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
`WHERE source LIKE 'parity-check/%'`. **Parity runs now target
`kbaseincubator.clearinghouse_parity`, not production, and the script
REFUSES the production namespace.** `--namespace` is a required argument
and `check_target_namespace()` rejects
`kbaseincubator.clearinghouse` before a Spark session is opened, let alone
before any write -- because the `parity-check/` prefix labels these rows
but does not isolate them, the demo readers treat every row in the
production namespace as real data, and the `<type>_result` tables are
append-only with no supported un-append. See **OP-C2** below, which
creates the parity namespace and bootstraps the five `<type>_result`
configs into it. Within that namespace the fixture is permanent and
harmless: the rows occupy their own
`(entity_hash, entity_type, result_type, source, parameter_set_hash)`
slots, and re-running the script appends more rows to those same slots
without changing any current-state answer.

**Rows already in production from the 2026-09-21 run stay there** -- they
are what OP-C1's guard counts, and OP-C1 drops and recreates those tables
anyway. Do not attempt to delete them individually.

**Record the result.** After running OP3, note in this table (or your own
operational log) the date, who ran it, and whether all six properties
passed:

| Date | Operator | Result |
|---|---|---|
| 2026-09-21 | Albert (kbhub), Chris present | OP2R rebuilt all fifteen as STRING (no BINARY), R.4 append-ready + partitions matched; OP3 ALL SIX PASS, exit 0. Write appended (existed_before=True, effective_mode=append) — D1 did not fire. Three-part FQN `kbaseincubator.clearinghouse.protein_result` resolved for the write; postflight row_count null (dev 1194, expected). |

If a per-type table FQN does not resolve, see the docstring of
`_result_table_fqn` in `scripts/clearinghouse_parity_check.py` -- it builds
the three-part, tenant-qualified form
(e.g. `kbaseincubator.clearinghouse.protein_result`), but
`BerdlCapability.load()`'s own postflight queries use a different, two-part
form (`clearinghouse.protein_result`, no tenant segment), and which form
actually resolves against the live catalog is not verifiable off-pod. Try
the two-part form next, and record in this log which one worked.

---

## The parameter-set change -- deploy and OP-C1..OP-C4 (2026-10)

Results are now keyed by tool, tool version **and a parameter-set hash**,
so the same protein run through the same tool version with different
parameters (a different threshold; TRANSYT's required NCBI taxonomy id) is
a distinct result rather than one silently overwriting the other. That
adds a `parameter_set_hash` column to the five `<type>_result` tables and
a sixteenth table, the parameter-set registry, which records what each
hash means.

**Resource parameters -- threads, memory, paths, batch sizes, hostnames --
must NEVER appear in a parameter set.** They describe *how* a run was
executed, not *what* was computed, so including one forks a slot that
should not fork: the same protein, same tool, same real parameters would
land as two "distinct" results because somebody moved the job to a
different host or gave it more threads. A parameter set contains only the
parameters the caller set on top of the tool version's defaults, and a
default run is `{}`.

**Build-complete and operationally-complete are separate: the code can be
merged, released and green in CI while OP-C1 through OP-C4 are still
owed** -- the lake does not change until an operator runs them.

### Deploying the parameter-set change

Two installed trees import KBUtilLib and both must be moved before OP-C1:

| Where | Tree | Parked on |
|---|---|---|
| BERDL pod (`kbhub`) | `/global_share/KBaseUtilities/KBUtilLib` | branch `deploy/main-20260926` |
| poplar | `~/venvs/kbdl` imports the Dropbox tree | `wip` |

On each, **merge KBUtilLib `main` in**. Not a reset, not a re-clone, not a
checkout of `main` over the top: a merge, so that anything committed
locally on that tree survives.

> **STOP POLICY -- read before you type `git merge`.**
>
> **If the merge would touch uncommitted local changes, or if it
> conflicts, STOP and leave every file exactly as it is for the
> operator.** Do not `git reset`, do not `git checkout --` a path, do not
> discard, do not `git stash`, and do not resolve a conflict by picking a
> side unattended.
>
> The reason is specific, not procedural caution: these are **deployed**
> trees. An uncommitted edit in `/global_share/KBaseUtilities/KBUtilLib`
> is most likely somebody's fix applied directly in the pod to get a
> demo working, and it exists nowhere else -- not in git, not on another
> machine. Discarding it is unrecoverable and you will not know what you
> destroyed until the thing it fixed breaks again. A stash is no better
> here: it moves the work somewhere nobody looking at this tree will
> think to look.
>
> Check first, so you find this out before the merge and not during it:
>
> ```bash
> git -C /global_share/KBaseUtilities/KBUtilLib status --porcelain
> git -C /global_share/KBaseUtilities/KBUtilLib fetch origin
> git -C /global_share/KBaseUtilities/KBUtilLib merge --no-commit --no-ff origin/main
> ```
>
> A non-empty `status --porcelain` is a STOP. A failed `merge` is a STOP
> (`git merge --abort` to return to the pre-merge state is fine -- that
> restores, it does not discard). Report which files, and hand it back.

**After the merge, smoke-check from the INSTALLED tree** -- not from a
checkout you happen to have, and not from this worktree. Print
`kbutillib.__file__` first and confirm it is the tree you just merged
into; a smoke check that passes against the wrong tree is worse than no
smoke check, because it reports the deploy as done.

```python
import kbutillib
print(kbutillib.__file__)   # CONFIRM this is the tree you merged into

from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    PARAMETER_SET_TABLE, table_configs,
)
from kbutillib.domains.kbase.berdl.clearinghouse_derivation import current_state_sql
from kbutillib.domains.identity import DEFAULT_PARAMETER_SET_HASH, parameter_set_hash
import kbutillib.domains.kbase.berdl.clearinghouse_manifest as cm

# 1. Sixteen configs, and parameter_set_hash in EVERY result schema.
configs = table_configs()
assert len(configs) == 16, f"expected 16 configs, got {len(configs)}"
assert {c["name"] for c in configs} >= {PARAMETER_SET_TABLE}
results = [c for c in configs if c["name"].endswith("_result")]
assert len(results) == 5, f"expected 5 result tables, got {len(results)}"
assert all("parameter_set_hash" in c["schema_sql"] for c in results)

# 2. current_state_sql emits the FIVE-tuple slot key.
sql = current_state_sql("kbaseincubator.clearinghouse.protein_result")
assert "parameter_set_hash" in sql, "slot key is still the old 4-tuple"

# 3. The manifest REFUSES a result source with no parameter_set.
try:
    cm.shard_plan(cm.load_manifest("""
    [[source]]
    name = "smoke"
    adapter = "file"
    path = "x.parquet"
    format = "parquet"
    entity_type = "protein"
    kinds = ["result"]
    [source.hash]
    raw_column = "aa"
    [source.result]
    result_type = "@const(annotation)"
    source = "@const(bakta/1.9)"
    result_type_version = "@const(1.0)"
    payload = "@json(product)"
    """))
except cm.ManifestError as exc:
    assert "parameter_set" in str(exc)
else:
    raise AssertionError("manifest accepted a result source with no parameter_set")

# 4. The default hash imports and is what it should be.
assert DEFAULT_PARAMETER_SET_HASH == parameter_set_hash({})
assert DEFAULT_PARAMETER_SET_HASH == (
    "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
)
print("deploy smoke check PASSED -- proceed to OP-C1")
```

**Only when all four pass, on both trees, proceed to OP-C1.**

### OP-C1 -- add `parameter_set_hash` and the parameter-set registry (2026-10)

**When this runs.** After the deploy above, and **BEFORE any
`kbu clearinghouse backfill run`.** That ordering is the whole reason this
is cheap: nothing can write real data to the lake until the backfill
command is deployed, so the five `<type>_result` tables still hold nothing
but the 2026-09-21 parity fixture. **No writer lockout is needed** -- there
are no writers yet. Run it after the first backfill and you are no longer
doing OP-C1, you are doing a migration with data in it, which is a
different and much more expensive problem.

**C1.0 -- precondition: the importing tree actually carries the change.**

```python
import kbutillib
print(kbutillib.__file__)
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    PARAMETER_SET_TABLE, _RESULT_COLUMNS, table_configs,
)
assert "parameter_set_hash" in [name for name, _type in _RESULT_COLUMNS], \
    "this tree's _RESULT_COLUMNS has no parameter_set_hash -- STOP, the deploy did not land"
assert PARAMETER_SET_TABLE in {c["name"] for c in table_configs()}, \
    "table_configs() has no 'parameter_set' registry config -- STOP"
print("precondition OK -- this tree carries the parameter-set change")
```

Either assertion firing is a **STOP**. It means the tree you are about to
drop production tables from does not have the schema you intend to
recreate them with, and the drop would leave you with no tables and no way
to make the new ones.

**C1.1 -- the guard. It refuses the drop unless the tables hold nothing
but the parity fixture.**

```python
from kbutillib.domains.kbase.berdl.clearinghouse_schema import table_configs

NS = ["kbaseincubator", "clearinghouse"]
def fqn(name): return ".".join(f"`{p}`" for p in [*NS, name])

PARITY_PREFIX = "parity-check/"
EXPECTED_PARITY_ROWS = 13   # measured 2026-10-06; see the note below

result_tables = [c["name"] for c in table_configs() if c["name"].endswith("_result")]
assert len(result_tables) == 5, result_tables

null_source = 0
non_parity = 0
parity = 0
for name in result_tables:
    t = fqn(name)
    # NULL sources are counted EXPLICITLY and SEPARATELY. See why below --
    # this is not redundant with the non_parity count.
    null_source += spark.sql(
        f"SELECT COUNT(*) AS n FROM {t} WHERE source IS NULL"
    ).collect()[0]["n"]
    non_parity += spark.sql(
        f"SELECT COUNT(*) AS n FROM {t} "
        f"WHERE source IS NOT NULL AND source NOT LIKE '{PARITY_PREFIX}%'"
    ).collect()[0]["n"]
    parity += spark.sql(
        f"SELECT COUNT(*) AS n FROM {t} WHERE source LIKE '{PARITY_PREFIX}%'"
    ).collect()[0]["n"]

print(f"null_source={null_source} non_parity={non_parity} parity={parity}")
assert null_source == 0, "a row has a NULL source -- STOP. Replay decision, not a runbook step."
assert non_parity == 0, "a row's source is outside 'parity-check/' -- STOP. Replay decision."
assert parity == EXPECTED_PARITY_ROWS, \
    f"expected exactly {EXPECTED_PARITY_ROWS} parity rows, counted {parity} -- STOP"
print("guard PASSED -- the result tables hold nothing but the parity fixture")
```

**Why `source IS NULL` is counted on its own, and why dropping it would
make the guard silently useless.** In SQL, `NULL NOT LIKE 'parity-check/%'`
evaluates to **NULL, not TRUE**. A row with a NULL `source` therefore does
*not* satisfy a bare `source NOT LIKE 'parity-check/%'` predicate, is not
counted by it, and **sails straight through a guard that only looks at
that count** -- the guard would report zero non-parity rows and authorise
the drop while real, unidentifiable rows sat in the table. So NULL is
counted by an explicit `source IS NULL`, and the `NOT LIKE` branch is
narrowed with `source IS NOT NULL` to keep the two counts disjoint and the
arithmetic honest.

**Do not delete this guard, and do not bypass it.** It is the only thing
standing between this procedure and an irreversible drop of production
tables. **Any row it finds that is not one of the 13 parity rows means a
replay decision -- a judgement about what that data is and how it gets
reproduced -- not a runbook step.** Stop, report what you found, and get
that decision made. Editing `EXPECTED_PARITY_ROWS` until the assertion
passes is the specific failure this paragraph exists to forbid.

> **On the number 13.** That is a count **measured against the live lake on
> 2026-10-06**, from the OP3 run of 2026-09-21. It is not derivable from
> today's fixture module: `ALL_PARITY_ROWS` now holds **19** rows, because
> `genome_fasta_invariance` (4 rows) and `parameter_set_forks_slot` (2
> rows) were added to `PARITY_CASES` after that run. So **13 is right only
> if OP3 has not been re-run against production since 2026-10-06.** If the
> count comes back 19, or 32, that is almost certainly a later OP3 run
> against production rather than corruption -- but it is still a STOP, and
> still a decision for a human, because the guard cannot tell the
> difference between "another parity run" and "something wrote rows we do
> not understand". Re-measure, write the new number and its date here, and
> say who authorised it.

**C1.2 -- drop the `all_result` view, then the five result tables.** The
view first, since it references the tables.

```python
spark.sql(f"DROP VIEW IF EXISTS {fqn('all_result')}")
for name in result_tables:          # from table_configs(), never typed by hand
    spark.sql(f"DROP TABLE IF EXISTS {fqn(name)}")
print(spark.sql("SHOW TABLES IN `kbaseincubator`.`clearinghouse`").collect())
```

The names come from `table_configs()` so this cannot drop a table the
module does not own. **Do not drop the namespace** and do not touch the
ten `_entity`/`_content` tables -- they are unchanged by this migration.
The `all_entity` and `all_content` views are also unaffected; leave them.

**C1.3 -- recreate the five result tables AND create the registry.** Dry
run first, then the real run. The registry config is included in the same
`tables` list, so one `bootstrap()` call does both.

```python
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    PARAMETER_SET_TABLE, bootstrap, table_configs,
)

wanted = {*result_tables, PARAMETER_SET_TABLE}          # five + the registry
configs = [c for c in table_configs() if c["name"] in wanted]
assert len(configs) == 6, [c["name"] for c in configs]

# DRY RUN -- expect 'action': 'create' for all six.
report = bootstrap(cap, namespace="kbaseincubator.clearinghouse",
                   tables=configs, dry_run=True)
print(report)

# REAL RUN -- only after the dry run reads as six creates.
report = bootstrap(cap, namespace="kbaseincubator.clearinghouse",
                   tables=configs, dry_run=False)
print(report["load_result"]["tables"])
```

`cap` is the `ClearinghouseBootstrapCapability(BerdlCapability())` from
section 2.0 -- a bare `BerdlCapability()` raises `AttributeError` here.
Every `'statement'` in the real run's output should carry
`parameter_set_hash STRING`, and the registry's should carry
`parameter_set_hash`, `canonical_json`, `observed_at` and
`ingest_batch_id` with **no partition clause** -- the registry is
UNPARTITIONED by design.

**C1.4 -- recreate the `all_result` view.** Per **section 2.4**, exactly
as written there. The registry is **not** part of any `all_<kind>` union
view: it is not entity-typed and has none of the union's columns.

**C1.5 -- acceptance: `DESCRIBE` all five result tables AND the registry.**

```python
for name in [*result_tables, PARAMETER_SET_TABLE]:
    rows = spark.sql(f"DESCRIBE TABLE {fqn(name)}").collect()
    cols = {r["col_name"]: r["data_type"] for r in rows}
    assert "parameter_set_hash" in cols, f"{name} HAS NO parameter_set_hash"
    assert cols["parameter_set_hash"] == "string", (name, cols["parameter_set_hash"])
    print(name, "parameter_set_hash OK")

# Partition specs UNCHANGED: 'source' is still the only partition column on
# each result table, and the registry has none. parameter_set_hash is
# DELIBERATELY NOT a partition key -- it is a hash, and partitioning on it
# would shatter every table into one partition per parameter set.
for name in result_tables:
    print(name, cap.table_partition_spec(name, namespace="kbaseincubator.clearinghouse"))
print(PARAMETER_SET_TABLE,
      cap.table_partition_spec(PARAMETER_SET_TABLE, namespace="kbaseincubator.clearinghouse"))
```

Expect `['source']` for each of the five, and `None` or `[]` for the
registry.

**Why this `DESCRIBE` step is not optional belt-and-braces.**
`bootstrap()` verifies **partition specs only**. It has no column-level
comparison anywhere: a table whose partition spec matches but whose schema
is missing `parameter_set_hash` passes `bootstrap()` clean and then fails
at the first write, in the pod, against the live table -- which is exactly
the shape of task 914. `bootstrap()` **cannot** detect a missing column, so
this step is the only thing in the procedure that does.

### OP-C2 -- parity in its own namespace

The parity fixture no longer goes anywhere near production. It gets a
namespace of its own.

**C2.1 -- create the namespace. `bootstrap()` does not create namespaces**
-- see section 2.0b, which establishes this and the trap in the argument
spelling. Use that pattern, with the new child name:

```python
from kbutillib.domains.kbase.berdl.transports import InPodTransport

transport = InPodTransport()
spark = transport.spark_session()
transport.create_namespace_if_not_exists(
    spark,
    namespace="clearinghouse_parity",  # BARE CHILD -- not the dotted form.
                                       # The dotted form yields
                                       # kbaseincubator.kbaseincubator.* ->
                                       # NoSuchNamespaceException (2.0b).
    tenant_name="kbaseincubator",      # `tenant_name` HERE; `tenant` on
                                       # BerdlCapability.load() -- see 2.0b.
    iceberg=True,
)
```

**C2.2 -- bootstrap ONLY the five `<type>_result` configs into it.** Not
all sixteen. The parity harness reads and writes result rows and nothing
else, so the ten `_entity`/`_content` tables and the registry have no
reason to exist there.

```python
configs = [c for c in table_configs() if c["name"].endswith("_result")]
assert len(configs) == 5
bootstrap(cap, namespace="kbaseincubator.clearinghouse_parity",
          tables=configs, dataset="clearinghouse_parity", dry_run=True)
bootstrap(cap, namespace="kbaseincubator.clearinghouse_parity",
          tables=configs, dataset="clearinghouse_parity", dry_run=False)
```

**C2.3 -- run the parity check against it.**

```bash
python scripts/clearinghouse_parity_check.py \
    --namespace kbaseincubator.clearinghouse_parity
```

Expect **one PASS line per entry in `PARITY_CASES`, which is eight as of
this revision** (`duplicate_collapse`, `newest_wins`,
`ingest_batch_id_tie_break`, `term_removal`, `source_isolation`,
`result_type_version_outside_slot_key`, `genome_fasta_invariance`,
`parameter_set_forks_slot`), a final `ALL 8 PROPERTIES PASS` line, and
exit 0. **Count against `len(PARITY_CASES)`, not against a number in this
document** -- the script prints the count from the fixture module, so the
two cannot disagree, and a property added later raises the expected number
without anyone editing this page.

Passing `--namespace kbaseincubator.clearinghouse` here exits **3**
without opening a Spark session. That is the guard working, not a
malfunction; do not look for a flag to override it.

**THIS IS A DERIVATION-ONLY HARNESS.** It proves one thing: that
`current_state_sql()` behaves on real Spark/Iceberg the way it does on the
DuckDB surrogate. **It has no parameter-set registry**, by design -- C2.2
bootstraps only the result configs. So **the orphan / registry-integrity
check is never run against this namespace**: every fixture row's
`parameter_set_hash` is an orphan there by construction, and that is
correct and expected rather than a finding. Run integrity checks against
production only (OP-C4). Do not "fix" the parity namespace by adding a
registry to it, and do not read a `kbu clearinghouse health` report taken
against it as meaningful.

### OP-C3 -- the demo seed

This is the first real data in the clearinghouse: a uniform 1/256 sample
of the `seq_protein_bakta` Mongo collection on poplar, mapped by
`src/kbutillib/domains/kbase/berdl/examples/clearinghouse_example_seed.toml`
and loaded by the backfill command's `mongo-protein-bakta` preset.

> **PRECONDITION -- ACKNOWLEDGMENT, AND IT IS A HARD GATE.** OP-C3 requires
> a **committed revision** of the sibling PRD `clearinghouse-backfill-v1`
> that has folded lake-5's round-1 changes, specifically:
>
> - the load of `parameter_set` is **registry-first** and **hash-keyed,
>   fill-only** (registry rows written before the result rows that
>   reference them; a hash already present is not rewritten);
> - **no gate on an outside party**;
> - **no precondition requiring KBDL's `/clearinghouse` routes to be
>   removed**.
>
> If that revision is not committed, **OP-C3 is BLOCKED.** Stop here.
>
> **KBDL's `/clearinghouse` routes are never removed to unblock OP-C3.**
> There is no version of this step that trades those routes for progress.
> KBDL's own SQLite clearinghouse stays exactly as it is; it is a separate
> system that happens to share a name. If you find yourself considering it,
> the answer is that OP-C3 waits.

**C3.0 -- is the backfill command even deployed?**

```bash
kbu clearinghouse backfill --help
```

**If this does not succeed, STOP.** As of this revision it does *not*:
`kbu clearinghouse` exposes `content`, `health`, `known`, `load`,
`parameter-sets`, `plan`, `results`, `shard`, `show`, `sources`, `stats`,
`tables` and `verify`, and `backfill` is **absent** -- it arrives with the
sibling PRD. A `No such command 'backfill'` error is the expected answer
today and means OP-C3 is not yet runnable. It is not something to work
around with `kbu clearinghouse shard` and `load` by hand.

**C3.1 -- SCRATCH FIRST. Always.**

```bash
kbu clearinghouse backfill run mongo-protein-bakta \
    --slice 00 --limit 12000 --dataset clearinghouse_bf_scratch
```

**If `clearinghouse_bf_scratch` already exists, `DESCRIBE` its result
tables BEFORE you run this, and STOP if they lack
`parameter_set_hash`.** A scratch namespace left over from a pre-change
run has the old eight-column result schema; appending into it either fails
or, worse, succeeds against a schema that cannot hold the hash, and then
the promotion gate below is checking a table that was never able to carry
the thing being verified.

```python
for name in [c["name"] for c in table_configs() if c["name"].endswith("_result")]:
    t = ".".join(f"`{p}`" for p in ["kbaseincubator", "clearinghouse_bf_scratch", name])
    cols = {r["col_name"] for r in spark.sql(f"DESCRIBE TABLE {t}").collect()}
    assert "parameter_set_hash" in cols, f"{name} is pre-change -- STOP, do not append"
```

**C3.2 -- the promotion gate. Promote ONLY when every one of these holds.**

1. **Non-zero counts** in `protein_entity`, `protein_content` **and**
   `protein_result` in the scratch dataset. A zero in any of the three
   means the load did not do what the mapping says.
2. **Every result row's `source` is `bakta/mongo-seq_protein_bakta`** --
   exactly that, no other value present.
3. **Every result row's `parameter_set_hash` is
   `DEFAULT_PARAMETER_SET_HASH`** (`44136fa3...aff8a`). The seed declares
   `parameter_set = {}`, a default run, because the store recorded no
   parameters.
4. **The registry holds the `{}` row** -- one row whose
   `canonical_json` is `{}` and whose hash is that same default.
5. **The integrity check is EMPTY** (`kbu clearinghouse health`, no
   parameter-set findings). Registry-first writing is what makes this
   true; a finding here means result rows reference a hash the registry
   does not explain.
6. **Any skipped documents are reported, with reasons.** A silent skip
   count is not acceptable: ~28% of documents in the surveyed per-tool
   collections are `_id`-only stubs, so skips are *expected* -- which is
   exactly why they must be enumerated by reason rather than shrugged off.
   An unexplained skip is a STOP.

```python
NS_S = ["kbaseincubator", "clearinghouse_bf_scratch"]
def sfqn(name): return ".".join(f"`{p}`" for p in [*NS_S, name])

for name in ("protein_entity", "protein_content", "protein_result"):
    n = spark.sql(f"SELECT COUNT(*) AS n FROM {sfqn(name)}").collect()[0]["n"]
    print(name, n)
    assert n > 0, f"{name} is EMPTY -- do not promote"

print(spark.sql(f"""
  SELECT source, parameter_set_hash, COUNT(*) AS n
  FROM {sfqn('protein_result')} GROUP BY source, parameter_set_hash
""").collect())
# Expect exactly ONE group: ('bakta/mongo-seq_protein_bakta', '44136fa3...aff8a').

print(spark.sql(f"""
  SELECT DISTINCT parameter_set_hash, canonical_json FROM {sfqn('parameter_set')}
""").collect())
# Expect the {} row.
```

**C3.3 -- PRODUCTION: the same command, without `--dataset`.**

```bash
kbu clearinghouse backfill run mongo-protein-bakta --slice 00 --limit 12000
```

**Do not invent flags beyond these.** `--slice`, `--limit` and
`--dataset` are the three this procedure uses. If the load needs something
those cannot express, that is a change to the backfill command and its
PRD, decided and reviewed there -- not a flag guessed at the prompt
against production.

### OP-C4 -- verify from primary-laptop (off-pod)

Independent confirmation from outside the pod, over Trino. This is a
read-only step and should be run by someone who did not run OP-C1..OP-C3.

1. **`COUNT(*)` per table, reported table by table.** Not a total, and
   **never a transport `row_count`** -- off-pod that field is a **page
   size**, not a table count (dev 1194), so it will happily report a round
   number that is simply how many rows came back in one page. Issue a real
   `SELECT COUNT(*)` against each table and write down sixteen numbers.
2. **Parity rows in production must be ZERO.**

   ```sql
   -- across all five <type>_result tables; expect 0 everywhere
   SELECT COUNT(*) FROM "kbaseincubator"."clearinghouse"."protein_result"
   WHERE source LIKE 'parity-check/%';
   ```

   Non-zero means either OP-C1 did not actually recreate the tables, or
   somebody ran the parity script against production after it. Both are a
   STOP.
3. **`DESCRIBE` all five result tables and the registry**, showing
   `parameter_set_hash` present and the partition specs unchanged
   (`source` only on the five; none on the registry). Note the quoting:
   Trino **rejects backquoted identifiers** -- use double quotes.
4. **`kbu clearinghouse parameter-sets`** lists **at least** the `{}` row.
   More rows is fine and expected as other tools land; the `{}` row
   missing while result rows carry the default hash is an orphan and a
   STOP.
5. **The integrity check returns nothing.** `kbu clearinghouse health`
   reports no parameter-set findings.

### Run record -- OP-C1..OP-C4

Empty template. **Fill a row in only after you have actually run the
step; do not pre-populate it with expected results.**

| Step | Date | Operator | Result |
|---|---|---|---|
| Deploy (pod) |  |  |  |
| Deploy (poplar) |  |  |  |
| OP-C1 |  |  |  |
| OP-C2 |  |  |  |
| OP-C3 scratch |  |  |  |
| OP-C3 production |  |  |  |
| OP-C4 |  |  |  |


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
