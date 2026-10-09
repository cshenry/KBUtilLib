# bf-kbdl-genome-archives — work record

- **Task:** `bf-kbdl-genome-archives` (developer role, repo `KBUtilLib`)
- **Branch:** `maestro/developer/shared-context-this-prompt-is-se-bf-kbdl-genome-archives`
- **Base commit:** `e29fe7d`
- **Date:** 2026-10-09

## Precondition check (all four PASS)

`clearinghouse-lake-5-convergence` is present on the base commit:

| Check | Result |
|---|---|
| (a) every `<type>_result` config's columns include `parameter_set_hash` | PASS — all five (`genome`, `protein`, `gene`, `function`, `ontology_term`) carry it in `schema_sql` |
| (b) `clearinghouse_schema` exports `PARAMETER_SET_TABLE` and `table_configs()` has sixteen configs | PASS — `PARAMETER_SET_TABLE = "parameter_set"`; 16 configs, the registry last and unpartitioned |
| (c) `kbutillib.domains.identity` exports `parameter_set_hash` and `DEFAULT_PARAMETER_SET_HASH` | PASS (`DEFAULT_PARAMETER_SET_HASH = 44136fa3…aff8a`) |
| (d) `src/kbutillib/domains/kbase/berdl/examples/clearinghouse_example_seed.toml` exists | PASS |

`clearinghouse_derivation` already publishes the public alias
`SLOT_KEY_COLUMNS` (`clearinghouse_derivation.py:151`). This task writes no
`<type>_result` rows at all (genome `entity` + `content` only), so the slot key
is neither restated nor needed.

## What was built

### 1. `KBDLHashGenomes` on the KBDL client

`src/kbutillib/domains/external/kbdl_service_utils.py`

- `JOB_TYPE_HASH_GENOMES = "KBDLHashGenomes"` (`:208`).
- `OBJECT_TYPE_GENOME_ARCHIVE = "GenomeArchive"` (`:216`) — the ONE spelling
  shared by `upload_object` (which accepts `transform` only for this type) and
  the adapter's `list_objects` filter. Exported in `__all__`; the string was
  already documented in `upload_object`'s docstring, which is where the exact
  type KBDL uses was confirmed.
- `KBDLServiceUtils.submit_hash_genomes(archive_ids)` (`:636`) posts
  `{"schema_version": "1", "job_type": "KBDLHashGenomes", "params":
  {"archives": [{"object_id": …}]}}` through the existing `_submit`, following
  `submit_build_genome`'s pattern of validating before any request is sent: an
  empty list or a non-string id is a `ValueError` with no round trip.

### 2. `@concat` — a closed, null-preserving multi-column pointer derivation

`fasta_reference` for an archive member needs the archive's object id AND the
member's entry name, which `@prefix` (one literal + one column) cannot express.
Rather than concatenating in the adapter, `@prefix` is generalised:

- `clearinghouse_manifest.py:218` adds `"concat"` to `DERIVATIONS`; the
  vocabulary table at `:182-220` documents it beside `@prefix`; `_check_arity`
  (`:894`) requires an EVEN count of at least four arguments, STRICTLY
  ALTERNATING literal text and column name starting with the literal, with a
  non-empty name in every odd position. Two arguments is refused by name
  because two arguments IS `@prefix`.
- `clearinghouse_shard._derive` (`:4236`) evaluates it and is null-preserving
  across EVERY named column — the same reason `@prefix` is: a pointer
  assembled from a missing half (`kbdl:archive:None:x`) is a reference to
  nothing and is indistinguishable in the lake from a real one.

`@prefix` is unchanged, so `arc-kbdl.toml` is untouched.

### 3. `KbdlHashSourceAdapter` (`adapter = "kbdl_hash"`)

`src/kbutillib/domains/kbase/berdl/clearinghouse_shard.py:3326`, with its
constants from `:3221` and four refusal types at `:3281`–`:3316`. Registered in
`ADAPTER_NAMES` and `_ADAPTER_CLASSES` (`:4143`).

- **No sequence crosses the network.** The adapter submits KBDL's own
  `KBDLHashGenomes` job; the archives are read and hashed in place on poplar
  and only digests and contig statistics come back.
- **`units()` → one label per archive**, the archive object id, LEXICOGRAPHICALLY
  sorted. `all_visible = true` costs one `GET /objects` filtered to
  `GenomeArchive` (accepting either `object_type` or `type` on a listing
  record); an explicit `archives` list (TOML list or a comma-separated
  placeholder string) needs no network at all. Neither key, both keys, or an
  empty resolved list is REFUSED — a zero-unit run reports a clean success
  having loaded nothing.
- **Sorted, not listing order**, because KBDL's `ObjectStore.list` orders by
  `added_at` with no tie-break.
- **Job id persisted BEFORE the first poll**, through
  `BackfillRunContext.put_state`, keyed by `(source, unit)`. Attempts are a
  LIST, so a retry is a recorded new attempt rather than an overwrite.
- **Lifecycle:** no recorded attempt → confirm visibility, submit, persist,
  poll, read. A recorded attempt → reattach: `queued`/`running` is polled,
  `completed` is read, and NEITHER resubmits. `failed`/`cancelled` → counted
  `hash-job-failed:<state>:<code>` and the command STOPS with the cause
  (`KbdlHashJobFailedError`), unless `--retry-failed-archives`, which submits a
  recorded new attempt. A reclaimed result (`get_job_result` → 404/409) is the
  only automatic resubmission, and only ONCE; a second loss refuses. Transport
  and auth errors never resubmit.
- **`cancelled` is terminal here** (`KBDL_HASH_TERMINAL_STATES`), a superset of
  the client's own `{completed, failed}`: `poll_until_terminal` would otherwise
  wait out its whole budget on a cancelled job. The adapter `check_job`s once
  first and only polls a non-terminal job, with a bounded `poll_timeout`.
- **Member errors read BY CODE** (`KBDL_HASH_TERMINAL_ERROR_CODES`, `:3259`):
  `invalid_input` is terminal — counted `hash-error:invalid_input`, archive
  still done. `internal_error` and any unrecognised code are retryable — counted
  and added to `incomplete_reasons()`, so the archive's successful rows are
  still emitted, ingested and verified while `unit_outcome` marks the unit
  `incomplete` and the command exits non-zero. Nothing is auto-resubmitted.
- **Per-entry `standardizer_version` mismatch** → counted
  `standardizer-version-mismatch` and retryable: the row is dropped rather than
  loaded under a hash rule this pod does not implement.
- **Version probe:** KBDL exposes no version endpoint, so the probe IS the
  archive's own job. The probed version is the result's top-level
  `standardizer_version` when present, else the first `genomes` entry's; a
  mismatch raises `KbdlHashVersionMismatchError`. Because the driver runs units
  sequentially, a mismatched poplar costs ONE job, not one per archive. An
  errors-only job claims no version (recorded as `null`) and the per-entry
  check still gates every row that does arrive.
- **A vanished / retyped archive** → `KbdlHashArchiveUnavailableError`, with
  the reason recorded in the durable state first, so the unit is incomplete and
  the run stops rather than loading a subset of a corpus it claims to cover.
- **Two kinds, one job:** `kinds = ["entity", "content"]` builds two adapter
  instances; the first persists its job id and the second reads the recorded
  attempt, so one archive costs one job.
- **An archive is not done when its job succeeds.** The adapter records what it
  submitted, read and emitted and never records completion; that judgement is
  `unit_outcome`'s, after ingest and verification.
- `max_in_flight` defaults to **1** and is validated (positive integer) before
  anything is spent; the bound is satisfied by the lifecycle's shape (one
  archive per unit, units sequential) rather than by arithmetic.

### 4. The FROZEN archive list, in the driver

`src/kbutillib/domains/kbase/berdl/clearinghouse_backfill.py`

- `UnitPlan.all_units` (`:282`) — every label the adapter reported BEFORE
  `--limit` capped the count — and `UnitPlan.vanished_units`.
- `freeze_adapter_units(out, *, run_id, plan, limit)` (`:378`): for an
  ADAPTER-unit plan it writes the discovered list to
  `<out>/backfill-state.json` under `frozen_units` on the first invocation and
  drives THAT list on every later one, re-applying `--limit` to it. Frozen
  units that live discovery no longer reports are recorded under
  `vanished_units` and carried on the returned plan — the unit is still driven,
  because it is the adapter that refuses, where the refusal can name the
  archive. Inert for slice and single plans (returns the plan unchanged).
- `src/kbutillib/interfaces/cli/clearinghouse.py:1692` (`backfill run`) and
  `:1952` (`backfill shard`) call it after `--out`/run id are resolved and
  BEFORE `register_run` / `record_planned_units` / `run_backfill` — i.e. before
  any job can be submitted. The `--limit`-selected list is persisted by the
  existing `record_planned_units` call immediately after.
- `backfill run` emits a warning naming vanished units.
- `_backfill_error_types()` (`:1527`) now also catches `ManifestSourceError`, so
  an adapter's own refusal (failed job, vanished archive, version mismatch) is a
  clean non-zero exit with its message instead of a traceback.

### 5. The preset

`src/kbutillib/domains/kbase/berdl/backfills/kbdl-genome-archives.toml`
(picked up by the existing `backfills/*.toml` package-data glob).

`[backfill] pulls_from = ["KBDL on poplar"]`, `needs_env = []`. One source:
`adapter = "kbdl_hash"`, `all_visible = true`, `max_in_flight = 1`,
`base_url = "${KBU_KBDL_BASE_URL:-}"`, `entity_type = "genome"`,
`kinds = ["entity", "content"]`, `observed_at_column = "observed_at"`.
`[source.hash] precomputed = "entity_hash"`. `[source.content]`:
`fasta_reference = "@concat(kbdl:archive:,archive_object_id,:,entry_name)"`,
`n_contigs`, `total_length`. `taxon_id`, `assembly_accession` and `is_closed`
are UNMAPPED — a hash job reads FASTA and assigns no taxonomy, consults no
assembly registry and makes no claim about closure, so null is the honest
value and a later source can fill them in for the same `entity_hash`.

### 6. Tests

`tests/berdl/test_clearinghouse_kbdl_hash_adapter.py` — 61 tests. A
`FakeKbdlClient` serves object listings and job records out of dicts and
records every submission, poll, metadata call and result read; the real
client's `submit_hash_genomes` is exercised against a recording `_request`
(the `_RecordingClient` pattern from `test_clearinghouse_arc_adapter.py`). The
run context is the REAL `cb.BackfillRunContext`, so `get_state`/`put_state`
are the shipped ones. Covers: the posted job type and params; `@concat`
grammar, arity and null-preservation; `all_visible` filtered to `GenomeArchive`;
lexicographic sort against a reverse-ordered listing; one job per archive
naming only that archive; the id on disk at the first `check_job`; a recorded
finished job read not resubmitted (and no visibility re-check); a queued job
reattached and polled; a reclaimed result resubmitted exactly once and a
second loss refused; a transport error not resubmitting; `failed` and
`cancelled` stopping with the cause and counting it; `--retry-failed-archives`
recording a new attempt; a succeeded job never resubmitted even with that
flag; `invalid_input` terminal vs `internal_error`/unknown retryable with rows
still loaded; per-entry version mismatch skipped, counted and incomplete; the
probe refusing before any other job is submitted; a vanished and a retyped
archive refused; an unrestricted multi-archive read refused; `max_in_flight`
defaulting to 1 and a malformed value refused before anything is spent; the
preset planning one unit per archive with no job and no record read;
`describe_preset` working with no client; the frozen list persisted,
`--limit` re-applied to it, and rediscovery ignored on resume; and the preset
sharded end to end into `genome_entity` + `genome_content` with the exact
pointer `kbdl:archive:<archive_object_id>:<entry_name>`.

`tests/cli/test_clearinghouse_backfill_cli.py` — 2 added tests driving the real
click CLI with the adapter wired to a fake client: `backfill run` plans one
adapter unit per archive, submits one job each and leaves the sorted frozen
list on disk; and `--resume` after the store moved on drives the frozen list
(the new archive does NOT join the run), stops non-zero naming the vanished
archive, records it under `vanished_units`, and does not redo or resubmit the
unit already recorded done.

## Verification

Same interpreter and venv for base and branch
(`.task-venv`, CPython 3.12, pytest 9.1.1). `pytest` and `duckdb` were not
installed in the task venv and were `pip install`ed into it (this task's own
environment) before the baseline run.

**Baseline captured on this worktree while it was still bit-identical to
`e29fe7d`** (clean tree, nothing applied), rather than from
`build-baselines/kbutillib.json` — that cache was captured at `0f84803` in a
different interpreter (3.11.15) and the task prompt said not to trust it:

```
python -m pytest -q tests/berdl tests/cli tests/external \
    --ignore=tests/external/test_prott5_utils.py
→ 249 failed, 1591 passed, 15 skipped   exit 1
```

`tests/external/test_prott5_utils.py` cannot be COLLECTED in this venv
(`numpy` absent) and is excluded from both runs by the same flag. All 249
baseline failures are in `tests/cli` and trace to optional dependencies missing
from the task venv; **none** are in `tests/berdl` or `tests/external`.

**Branch, identical command:**

```
python -m pytest -q tests/berdl tests/cli tests/external \
    --ignore=tests/external/test_prott5_utils.py
→ 249 failed, 1653 passed, 15 skipped   exit 1
```

`comm -13` of the two failure NODEID sets is **empty in both directions**: no
test that passed at `e29fe7d` fails on the branch, and none that failed
started passing. The +62 passes are this task's new tests.

Also run:

```
python -m pytest -q tests/berdl/test_clearinghouse_kbdl_hash_adapter.py  → 61 passed, exit 0
python -m pytest -q tests/test_python_floor_syntax.py \
    tests/models_and_analyses/test_backfill.py                           → 20 passed, exit 0
ruff check <the six files this task touched>                             → All checks passed, exit 0
```

`ruff check src/kbutillib tests/berdl` reports 68 pre-existing findings, every
one of them in a file this task did not touch.

## Divergences reconciled between the prompt and the tree

1. **"A failed job → skip the unit's records as `hash-job-failed:<code>` and
   continue" (BUILD) vs "a failed or cancelled job … STOPS the command with the
   terminal cause" (confront round 1).** The amendment is binding and
   overrides. Implemented as BOTH: the reason is counted
   (`hash-job-failed:<state>:<code>`) and recorded in the durable state, and
   then `KbdlHashJobFailedError` stops the command. The success criteria's
   "skips and counts job failures" is satisfied by the count; the amendment's
   "stops the command" by the raise.
2. **`max_in_flight` default 4 (BUILD) vs 1 (round 1).** 1, as amended.
3. **"persist the full list and the `--limit`-selected list VIA THE RUN
   CONTEXT before submitting any job" (round 2) — the run context does not
   exist at that point.** `BackfillRunContext` is constructed per unit INSIDE
   `run_backfill`, after planning; `units()` is called by `plan_units` with no
   context at all (`clearinghouse_backfill.py:329` → `cs.get_adapter(source)`).
   The freeze therefore went into the run's own durable record,
   `<out>/backfill-state.json`, through the new `freeze_adapter_units`, called
   from the CLI before `register_run`/`record_planned_units`/`run_backfill` —
   the same file `record_planned_units` already writes the `--limit`-selected
   list to, and still strictly before any job. The run context is used for what
   it can carry: each archive's job attempts, per `(source, unit)`.
4. **"the sharder fills `parameter_set_hash`; every manifest result source must
   declare a static `parameter_set`" (precondition) — not applicable.** This
   preset writes `genome_entity` and `genome_content` only; it declares no
   result source and emits no registry batch. Verified: the preset's
   `_target_tables` is exactly the two genome tables.
5. **The orientation's line numbers were all CORRECT at the base commit** — the
   `@prefix` branch at `clearinghouse_shard.py:3299`, the derivation docs at
   `clearinghouse_manifest.py:182-204`, the prefix validation at `:875`,
   `BackfillRunContext` at `clearinghouse_backfill.py:448`, `_submit` at
   `kbdl_service_utils.py:517` and `submit_build_genome` at `:576`. Nothing
   needed rediscovering. The task offered "extend the prefix derivation or add
   a small closed concat derivation"; the latter was chosen, so `@prefix` and
   `arc-kbdl.toml` keep their exact meaning.
6. **`SourceAdapter.units()`'s docstring already said "the genome archive's
   will be one archive"** and `BackfillRunContext`,
   `UNIT_KIND_ADAPTER`, `plan_units`' adapter branch and the CLI's
   `--retry-failed-archives` flag were all already present and shaped for this
   adapter. Nothing was rebuilt; the adapter was fitted to them.
7. **`backfills/` is `src/kbutillib/domains/kbase/berdl/backfills/`**, not a
   repo-root `backfills/`. The preset went there, where `preset_root()` looks.

## What was NOT done, and why

- **No live run against poplar.** The service is unreachable from this worktree
  and the developer role may not touch live hosts; every KBDL interaction is
  covered by a fake. The job-output shape, the `GenomeArchive` type string, the
  error codes and the 30-day retention behaviour are taken from the task's
  FACTS and from `kbdl_service_utils`' own documented contract, not verified
  against the running service.
- **The submit/persist crash gap is DOCUMENTED, not closed** (as the task
  instructed). A crash between `POST /jobs` returning a job id and the state
  file being replaced leaves a running job with no recorded id, and the resume
  submits another. Closing it needs a server-side idempotency key
  (submit-with-token), which KBDL does not offer. The cost is one wasted job
  and a second identical result — not a wrong row, because the rows are keyed
  by `entity_hash` and the ingest is fill-only. Stated in the adapter docstring.
- **No concurrent unit execution.** The task said sequential is fine;
  `max_in_flight` is validated and bounds the lifecycle rather than enabling
  concurrency, and `run_backfill` drives units one at a time.
- **No `build-baselines/kbutillib.json` refresh.** The existing file is for a
  different interpreter and commit; rewriting it from a venv that cannot even
  collect seven test modules would replace one stale cache with another. The
  baseline this task actually used is reproduced above with its exact command.
