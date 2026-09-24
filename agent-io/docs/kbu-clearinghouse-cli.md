# `kbu clearinghouse` — read and operator verbs

Read verbs over the fifteen-table BERDL clearinghouse
(`kbaseincubator.clearinghouse`). Every verb is a thin facade over
`ClearinghouseCapability`
(`src/kbutillib/domains/kbase/berdl/clearinghouse_capability.py`) — the CLI
contains **no query logic, no table-name construction, and no hash handling**
of its own. Source: `src/kbutillib/interfaces/cli/clearinghouse.py`.

## Locus table — read this first

The natural discovery path is "the read verbs worked from my laptop, so I ran
the load verb" — and the load/ingest verbs (a different task's surface) will
**refuse** off-pod with `BerdlLoadRefusedError`, not a missing-flag message.
Every *read* verb below works off-pod and **degrades rather than fails**: an
in-pod-only field is omitted and the omission is reported in `warnings`.

| Verb       | Off-pod | Notes                                                                 |
|------------|---------|-----------------------------------------------------------------------|
| `tables`   | ✅ works | Fifteen tables with kind, entity_type, partition spec, row count.     |
| `stats`    | ✅ works | Per-table row counts. `--include-files` is **in-pod only** (see below).|
| `known`    | ✅ works | Which supplied hashes are present in `<type>_entity`.                  |
| `show`     | ✅ works | One entity: presence + type-specialized content + current-state results.|
| `content`  | ✅ works | Content rows; `--all-types` reads the lossy `all_content` view.        |
| `results`  | ✅ works | Current-state results for `<type>_result`.                            |
| `sources`  | ✅ works | Distinct `source` values with row counts.                            |
| `health`   | ✅ works\* | Data-file fragmentation. \*Needs file metadata (in-pod); off-pod it reports it cannot evaluate, in `warnings`. |
| `stats --include-files` | ⚠️ degrades | File counts/sizes read Iceberg `.files` metadata via Spark — **in-pod only**. Off-pod the file fields are **omitted** and a non-empty `warnings` list says so. |

> Off-pod access is **unproven end to end**. At the time this was written no
> off-pod clearinghouse read had been performed against the live tables; the
> "degrades rather than fails" posture is implemented carefully but not
> live-verified off the pod.

## The output contract (a published interface)

A separate dashboard PRD consumes this contract and nothing else. Treat it as
published.

Under `--json` every verb emits **exactly one** object:

```json
{"schema_version": 1, "verb": "stats", "generated_at": "2026-09-24T19:55:18Z",
 "locus": "off_pod", "data": {...}, "warnings": [...]}
```

Three structural rules:

1. **Human output is a rendering of the same dict.** Each verb builds one
   structure and then either serialises it (`--json`) or renders it as a table.
   There is no second code path that computes anything.
2. **`schema_version` bumps only on a breaking change to `data`.** Additive
   fields do not bump it. **A consumer pinning major `1` must tolerate unknown
   keys.**
3. **`warnings` is never empty for a degraded answer.** An off-pod
   `stats --include-files` that cannot read file counts reports that in
   `warnings` and omits the fields — never silently partial data that looks
   complete.

### stdout discipline

Under `--json`, **stdout carries the envelope and nothing else** — RFC
8259-clean, exactly one object, no banner, no progress line, no warning text.
Every diagnostic and warning goes to **stderr**. Warnings *also* appear in the
envelope's `warnings` list (that is where a dashboard reads them); stderr is for
the human watching a run.

## Row counts

Every count comes from an explicit `SELECT COUNT(*)`, never a transport
`row_count` field. Off-pod that field is the *materialized page size* (e.g.
`5000`), which would look entirely plausible and be wrong for all fifteen
tables. The capability owns this discipline; the CLI just surfaces its numbers.

## Verbs

- `tables` — the fifteen tables with `kind`, `entity_type`, `partition_by`, `row_count`.
- `stats [--include-files]` — per-table row counts; with `--include-files`, Iceberg data-file counts and average sizes (in-pod only).
- `known --type T (--hash H | --hashes-file F)` — which supplied hashes are present in `<T>_entity`.
- `show --type T --hash H` — one entity: presence + type-specialized content + current-state results, in one output.
- `content --type T (--hash H | --hashes-file F) [--all-types]` — content rows; `--all-types` is the explicitly lossy `all_content` view (shared columns only) and requires no `--type`.
- `results --type T [--hash H] [--source S] [--result-type R]` — current-state results (whole-table via `current_state`, or hash-filtered via `results`).
- `sources [--type T]` — distinct `source` values with row counts.
- `health` — flags tables whose average data-file size is below ~8 MiB. It **reports** the fragmentation condition only; **compaction is owned elsewhere.** The pathology it catches is real on this platform (a table observed at 2.76M rows across 139 files averaging 288 KiB).

## Exit codes

A degraded read still emits a well-formed envelope and exits `0`; callers detect
degradation from `warnings` (and stderr), not the exit code. Only a malformed
`kbu` invocation (click's own parse errors, e.g. `content` without `--type` and
without `--all-types`) exits non-zero.

---

# `kbu clearinghouse` — operator verbs

Four verbs drive the ingest pipeline. They are thin facades over the SAME
capability layer as the read verbs — the CLI holds **no ingest logic of its
own**. `plan`/`shard` call the shard-stage module functions
(`clearinghouse_manifest.load_manifest` / `shard_plan`,
`clearinghouse_shard.shard_manifest`); `load`/`verify` call
`ClearinghouseCapability.ingest_shards` / `verify_run`. All four emit the same
one-object JSON envelope (`schema_version`, `verb`, `generated_at`, `locus`,
`data`, `warnings`) and honour the same stdout discipline: under `--json`,
stdout is the envelope and nothing else; every warning also lands in
`warnings`.

## Locus — the two-stage split

The pipeline is deliberately split into a **shard stage** (anywhere, no pod, no
credentials — just CPU and disk) and an **ingest stage** (in-pod only).

| Verb     | Where           | Writes?                          | Notes |
|----------|-----------------|----------------------------------|-------|
| `plan`   | anywhere        | **nothing**                      | Resolves the manifest, reports per-table row counts and target shard sizes. |
| `shard`  | anywhere        | bronze parquet under `--out`     | Builds the shards. No pod, no credentials. |
| `load`   | **in-pod only** | tables + run ledger              | Ingests shards, verifies postflight, writes the run ledger. Foreground, **not** a daemon. |
| `verify` | **in-pod only** | **nothing**                      | Re-checks a completed run's row counts + snapshots against the ledger. |

`load` and `verify` **refuse early off-pod**, before any transport call, with a
message that **names the locus as the reason** (not a bare
`BerdlLoadRefusedError` from deep in a transport, and not a missing-flag
message). There is **no force flag** — the locus is the reason, not a switch you
can flip.

## `load` is a foreground run, resumable from the ledger

`load` runs in the **foreground**. It is not a daemon and takes no `--daemon`
flag. Resumability comes from the **run ledger** (`ingest_ledger.jsonl`, written
beside the shards): each `(table, batch)` records `started` → `ingested`/`failed`
with its row count and Iceberg snapshot id. A re-run reads the ledger and skips
what already landed.

- `load --dry-run` is **the operator's last gate**. It resolves namespaces, runs
  the pre-write assertion (the dev 1206 write-target guard), reports the write
  mode — and **writes nothing**. Use it before every real load.
- `load --reconcile` reconciles a `started`-but-not-terminal batch left by an
  interrupted run.

## The dev 1206 guard fails closed

Any operator verb that reaches `ingest_shards()` **raises** if the
existence-probe namespace differs from the ingest-target namespace, and calls
`load()` **exactly zero times**. This is the dev 1206 regression (a probe
against `default` while the write went to `tenant.dataset`, silently turning an
append into a destructive overwrite). It is a guard, not a preference: there is
no flag to override it. If it fires, resolve the namespace mismatch — do not
retry.

## Operator walkthrough

The shard stage needs CPU and disk; the ingest stage needs the pod. So the
normal flow builds shards on a capable machine, moves them into the pod, and
ingests there.

1. **Plan (anywhere).** Confirm the manifest resolves and see the row counts and
   how many shards each table will split into:

   ```bash
   kbu clearinghouse plan ./manifest.toml --json
   ```

   An invalid manifest exits non-zero and **names the offending key** (e.g.
   `sequence` is not a legal genome content column, or `entity_type = "plasmid"`
   is not a known entity type). Nothing is written.

2. **Shard (on a CPU machine).** Build the bronze parquet shards:

   ```bash
   kbu clearinghouse shard ./manifest.toml --out ./bronze --target-bytes 536870912
   ```

   `--target-bytes` defaults to ~512 MiB. The output is one directory per target
   table of sorted bronze parquet, plus a `shard`-envelope report (one row per
   shard: table, batch, path, rows, hash range).

3. **Move the shards into the pod.** Copy `./bronze` to a path visible inside the
   BERDL JupyterHub pod. (`plan`/`shard` never needed credentials; from here on
   you must be in-pod.)

4. **Dry-run — the last gate (in-pod).** Prove the write target before writing:

   ```bash
   kbu clearinghouse load ./bronze --dry-run --json
   ```

   This resolves namespaces, runs the dev 1206 pre-write assertion, reports the
   write mode, and writes nothing. The envelope's `warnings` says it was a
   dry run.

5. **Load (in-pod).** Ingest for real:

   ```bash
   kbu clearinghouse load ./bronze --json
   ```

   Foreground; resumable from the ledger if interrupted.

6. **Verify (in-pod).** Re-check the completed run against its ledger:

   ```bash
   kbu clearinghouse verify ./bronze --json
   ```

   `verify` does its own explicit `COUNT(*)` per table and confirms every
   snapshot the ledger recorded still exists in the table's Iceberg history. It
   **reports** discrepancies (in `data.discrepancies` **and** `warnings`); it
   does not raise on a discrepancy, precisely so you can inspect a run that a
   load flagged as failed.

## A load reported FAILED may still have landed rows — `verify`, don't blindly re-run

`load()` can return `success=true` with a **null postflight** row count when it
could not read the table back to confirm the write (dev 1194). This CLI treats a
null postflight as a **FAILED load** — but the rows may have landed anyway; only
the *verification* failed. So the correct response to a FAILED-on-null-postflight
load is:

```bash
kbu clearinghouse verify ./bronze          # what actually landed?
kbu clearinghouse load ./bronze --reconcile # only if verify shows a gap
```

**Never blindly re-run a plain `load`** after a null-postflight FAILED. Use
`verify` to see the ground truth, then `--reconcile` to close any real gap.

### The one case duplicates are not harmless: re-ingested CONTENT shards

For `<type>_entity` and `<type>_result`, current state is derived (dedup by hash
/ current-state selection), so a duplicate ingest is absorbed by the derivation.
`<type>_content` has **no current-state derivation** — it is append-only content
keyed by hash with no "latest wins" collapse. Re-ingesting a **content** shard
therefore **duplicates content rows** with no downstream layer to remove them.
This is the one case where blindly re-running an ingest is genuinely harmful, and
the reason the null-postflight response above is `verify` + `--reconcile`, never
a blind re-run.

## Exit codes

`plan` on an invalid manifest exits **non-zero** with the offending key named,
having written nothing. `load`/`verify` off-pod exit non-zero (locus refusal,
message naming the locus). A successful verb — including a `load` whose ledger
recorded a `failed` table, or a `verify` that reported discrepancies — emits a
well-formed envelope and exits `0`; the operator reads `data`/`warnings` for the
outcome, exactly as with the read verbs.
