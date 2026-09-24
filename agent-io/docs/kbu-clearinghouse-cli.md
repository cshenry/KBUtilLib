# `kbu clearinghouse` — read verbs

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
