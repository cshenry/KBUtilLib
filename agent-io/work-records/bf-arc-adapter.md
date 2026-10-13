# bf-arc-adapter — work record

- **Task:** `bf-arc-adapter` (developer role, repo `KBUtilLib`)
- **Branch:** `maestro/developer/shared-context-this-prompt-is-se-bf-arc-adapter`
- **Base commit:** `384d5d8`
- **Date:** 2026-10-09

## Precondition check (all four PASS)

`clearinghouse-lake-5-convergence` is present on the base commit:

| Check | Result |
|---|---|
| (a) every `<type>_result` config's columns include `parameter_set_hash` | PASS — all five (`genome`, `protein`, `gene`, `function`, `ontology_term`) |
| (b) `clearinghouse_schema` exports `PARAMETER_SET_TABLE` and `table_configs()` has sixteen configs | PASS — `PARAMETER_SET_TABLE = "parameter_set"`, 16 configs, registry last |
| (c) `kbutillib.domains.identity` exports `parameter_set_hash` and `DEFAULT_PARAMETER_SET_HASH` | PASS |
| (d) `examples/clearinghouse_example_seed.toml` exists | PASS |

`clearinghouse_derivation` already publishes the public alias `SLOT_KEY_COLUMNS`
(= `("entity_hash", "entity_type", "result_type", "source",
"parameter_set_hash")`), so nothing needed adding for the standing rule about
the slot key. It is imported, never restated.

## What was built

### 1. `download_object` on the KBDL client

`src/kbutillib/domains/external/kbdl_service_utils.py`

- `KBDLServiceUtils.download_object(object_id, dest_path)` (`kbdl_service_utils.py:918`) streams
  `GET /objects/{object_id}/content` in 1 MiB chunks to a temp file beside the
  destination, hashing in the same pass, and verifies **before** the
  `os.replace`. A mismatch unlinks the temp file and raises
  `KBDLObjectDigestError` — nothing is ever cached.
- `is_content_object(object_id)` (`:345`) and
  `REFERENCE_OBJECT_ID_PREFIX = "ref:"` (`:342`): the sha256 check runs for
  **content objects only**, because a reference object's id is a hash of the
  resolved path (`src/kbdl_service/object_store/references.py` in
  KBDLJobRunningPrototype), so there is nothing for the bytes to equal.
- `KBDLObjectDigestError` (`:308`), `DOWNLOAD_CHUNK_BYTES` (`:355`), and the
  module docstring's endpoint list updated with the new route.

### 2. `ArcSourceAdapter`

`src/kbutillib/domains/kbase/berdl/clearinghouse_shard.py` — `ArcSourceAdapter`
at `:1885`, `ArcFeatureIdConflictError` at `:1657`, `classify_arc_skip` at
`:1600`, `promote_arc_checkpoints` at `:1840`, `_arc_read_member` at `:3125`.
Registered as `"arc"` in `ADAPTER_NAMES` (`:549`) and `_ADAPTER_CLASSES`
(`:3157`), and named in `clearinghouse_manifest`'s allowed-adapter message.

- Opens `runs.sqlite` through `sqlite3.connect("file:<path>?mode=ro",
  uri=True)` — a missing file raises `ManifestSourceError` and **creates
  nothing**. `KorosArcStore`/`RunDatabase` are never touched.
- Job results read with `gzip.open(..., "rt")`; a missing/unparseable file is
  counted `result-file-missing` and is **not** retryable.
- Non-backfillable kinds counted `kind-not-backfillable:<kind>`.
- **Join key is `fasta_object_id[:16]`, never `BuiltGenomeEntry.prefix`.** The
  fast path decides which objects to download; the authoritative index is each
  downloaded object's exact `features[].id`. A fast-path entry that disagrees
  with the object is discarded (`fasta-prefix-mismatch`); two objects claiming
  one feature id raise `ArcFeatureIdConflictError` — counted and **refused**,
  never last-write-wins.
- Objects fetched once each into `cache_dir` (`<shard out>/objects` under a
  run, `~/clearinghouse-shards/objects` otherwise); a cached file is re-hashed
  on **reuse**, not trusted on presence.
- Four emits: `genome` (per distinct genome object: `contig_sequences`,
  `fasta_object_id`, `ncbi_taxid`, `n_contigs`, `total_length`, `created_at`),
  `protein` / `gene` (per feature), `annotation` (per `(feature, tool)` with a
  non-empty ontology map).
- Tarballs default to the pod's arc backup; a configured-but-absent tarball is
  an **error**. Only `data/batch_ledger.json`, `data/pmi_ledger.json`,
  `data/annotations/<key>.json` and `data/annotations_pmi/<key>.json` are
  opened, by exact name via `tarfile.getmember` — nothing is extracted.
- Tarball `created_at` = the ledger's valid `done_ts`, else the run start,
  counted `timestamp-fallback`. A ledger-only genome emits proteins/genes/
  annotations but no `genome` row without a real FASTA pointer
  (`fasta-pointer-missing`).
- TRANSYT rows carry `{"taxonomy_id": "<decimal string>"}` and are passed to
  lake-5's **shared** `cp.validate_transyt_parameter_set` (called, not
  restated); a genome with no taxid loses its TRANSYT rows
  (`transyt-without-taxonomy`), a taxid the validator refuses is skipped
  (`transyt-parameter-set-invalid`).
- **Index vs selection.** The dependency index is built from every build record
  and every ledger regardless of `since` and drives joins only. Emission comes
  from a frozen ordered selection (`since <= updated_at <= upper_bound`, in
  `(updated_at, record_id)` order), persisted through the run context under
  the adapter name **before any record is read**, so all four sources and any
  resume share it. `--limit` is applied once, to that selection.
- `created_at` for a genome (and its proteins/genes) is the **earliest** build
  record's timestamp, else the ledger's `done_ts`.
- Retryable failures (`object-unavailable`, transport) go to
  `incomplete_reasons()`, which `clearinghouse_backfill.unit_outcome` now turns
  into `incomplete` even when every batch verified.
- Watermark checkpoint under `$KBU_CLEARINGHOUSE_STATE_DIR/sources/`, keyed by
  sha256 of tenant + dataset + absolute `db_path` + KBDL base URL + principal
  fingerprint (sha256 of the bearer token, first 16 hex — never the token).
  Written as a `.candidate.json` only when the read was uncapped, exhausted and
  retry-free; `promote_arc_checkpoints()` (called from `run_backfill` after a
  unit comes out `done`) renames it. Only a promoted file is readable, so a
  capped, incomplete, killed or scratch-dataset run advances nothing.
- Skip accounting is classified: `ARC_SOURCE_RECORD_SKIPS`,
  `ARC_TARGET_ROW_SKIPS`, `ARC_NOTED_REASONS`, `ARC_REFUSAL_REASONS` and
  `classify_arc_skip(reason)` — so the store's 430 records reconcile with the
  report even though four sources read it.

### 3. Manifest grammar

`src/kbutillib/domains/kbase/berdl/clearinghouse_manifest.py`

- `[source.hash] contig_set = "<column>"` — genome only, exactly one of
  `raw_column` / `precomputed` / `contig_set`. The list-ness is enforced per
  row in `clearinghouse_shard._compute_entity_hash`: a `str` is a
  `ShardValueError`, because `entity_hash("genome", "ACGT")` does not raise —
  it hashes the set of characters.
- `[source.result] parameter_set_column = "<column>"` (`_plan_parameter_set` at
  `:1199`, `_PARAMETER_SET_KEYS` at `:1117`) — exactly one of it and
  `parameter_set`. Validated and canonicalised per row by the **same**
  `cp.result_parameter_set_identity` the static form uses, so equal sets
  produce byte-identical canonical text and an identical hash. A refusal is
  `ShardParameterSetError` (`clearinghouse_shard.py:3320`), counted under the new
  `SKIP_PARAMETER_SET = "parameter-set-invalid"` (`:3358`), skipping one row rather than
  failing the run.
- `@prefix(text, column)` — null-preserving, added to `DERIVATIONS` and
  documented beside the others.
- `BackfillInfo.tool_versions` from `[backfill.tool_versions]`
  (`_parse_tool_versions` at `clearinghouse_manifest.py:588`), with
  `DEFAULT_TOOL_VERSION = "1.0"` (`:542`); reached by an adapter through the new
  `SourceAdapter.bind_backfill_info` / `tool_version` hooks and
  `get_adapter(..., backfill=)`.
- New `SourceAdapter` hooks: `watermark()`, `incomplete_reasons()`,
  `bind_backfill_info()`, `tool_version()`. Recorded per source in `run.json`
  via `SourceOutcome.watermark` / `.incomplete_reasons`.
- `shard_manifest` writes a **second** registry batch for per-row parameter
  sets (`write_discovered_parameter_set_shard`, `clearinghouse_shard.py:4830`), necessarily after the result
  tables but ordered first in the reports — the ingest side puts the registry
  table first unconditionally, and the orphan check runs after both batches.

### 4. Preset

`src/kbutillib/domains/kbase/berdl/backfills/arc-kbdl.toml` — four sources
(`arc-genomes`, `arc-proteins`, `arc-genes`, `arc-annotations`), each with
`observed_at_column = "created_at"`, plus `[backfill]` with
`pulls_from = ["kbhub arc store", "KBDL on poplar"]`, `needs_env = []` and
`[backfill.tool_versions]` shipping all six tools at `"1.0"` with the OP-BF4
note. `kbu clearinghouse backfill list` reports exactly
`parameter_set, genome_entity, genome_content, protein_entity, protein_content,
gene_entity, gene_content, protein_result`.

## Verification

Baseline captured **in this task's own venv at the base commit with the tree
clean**, per the task's REGRESSION BASELINE instruction (the committed
`build-baselines/kbutillib.json` was captured in a different, more minimal venv
— it records 99 known-failing nodeids where this venv has 368 — so it was read
and then set aside, as Maestro#10 instructs).

Eleven test modules cannot be collected at all in this venv (hard
`ModuleNotFoundError` on `pandas`/`torch`/`rdkit`); they are excluded by
`--ignore` identically on both sides. `tests/berdl/test_clearinghouse_derivation.py`
collects fine here because `duckdb` is installed.

```
.task-venv/bin/python -m pytest -q -p no:randomly \
  --ignore=tests/biochem/test_escher_utils.py \
  --ignore=tests/biochem/test_ms_biochem_deltag.py \
  --ignore=tests/biochem/test_ms_reaction_similarity.py \
  --ignore=tests/external/test_prott5_utils.py \
  --ignore=tests/kbase/test_kb_annotation_utils_parquet.py \
  --ignore=tests/modeling/test_comprehensive_gapfill_wrapper.py \
  --ignore=tests/modeling/test_ms_community_utils_live.py \
  --ignore=tests/modeling/test_ms_community_utils.py \
  --ignore=tests/modeling/test_ms_reconstruction_utils.py \
  --ignore=tests/modeling/test_ms_remote_solver_utils.py \
  --ignore=tests/notebook \
  tests/
```

| | base `384d5d8` | branch | exit |
|---|---|---|---|
| failed | 359 | 358 | 1 (both) |
| passed | 3526 | 3586 | |
| skipped | 424 | 424 | |
| errors | 9 | 9 | |
| distinct failing nodeids | 368 | 367 | |

`comm -13 <base failures> <branch failures>` is **EMPTY** — no test that passed
on the base commit fails on the branch.

New tests: `tests/berdl/test_clearinghouse_arc_adapter.py`, **59 passed, exit
0** (`.task-venv/bin/python -m pytest -q -p no:randomly
tests/berdl/test_clearinghouse_arc_adapter.py`).

`ruff check` is clean on all four modified source files and the new test file.
Two pre-existing `I001` findings remain in `berdl/__init__.py` and
`clearinghouse_capability.py`, neither of which this task touched.

## Divergences reconciled (prompt claim → tree fact → what I did)

1. **`SLOT_KEY_COLUMNS` "may need adding".** The prompt said to add a public
   alias "if only a private name exists". Both `_SLOT_KEY_COLUMNS` and the
   public `SLOT_KEY_COLUMNS` already exist in `clearinghouse_derivation`.
   Nothing added; the public name is what the code reads.
2. **`clearinghouse_manifest` lines 116–119.** The prompt said they "already
   anticipate `parameter_set_column`". They did — as a paragraph stating it is
   *not implemented and rejected today*. I replaced that paragraph with the
   implemented semantics rather than leaving a docstring that contradicts the
   code.
3. **The registry batch cannot be written first for per-row sets.** The prompt
   requires the sharder to "emit one `parameter_set` registry row per DISTINCT
   per-row parameter set, exactly as it does for a static parameter_set", but
   `write_parameter_set_registry_shard` runs **before** any record is read and
   takes its rows from plan-time values, which a per-row set does not have. I
   added a second writer that runs after the tables and ordered it first in the
   reports. The invariant that actually matters is preserved and checked: the
   ingest side (`clearinghouse_backfill._target_tables:249`) puts the registry
   table first unconditionally, and the orphan check now runs after both
   batches.
4. **`${KBU_ARC_TARBALLS:-}` split on commas, "empty = none".** The CONFRONT
   ROUND 1 amendment overrides the default to the pod's backup tarball, which I
   followed. But `expand_placeholders` treats an **empty environment value as
   unset**, so `KBU_ARC_TARBALL=""` falls back to the default rather than
   meaning "none" — an operator cannot switch the tarball off through the
   variable. I kept the amendment's default (it is binding), support both a
   list and a comma-separated string, and documented the limitation in the
   preset so it is not discovered at run time.
5. **Tarball member tool.** The prompt says the tarball members are "in the
   SAME feature -> tool -> ontology -> terms shape (tool RAST, ontology
   `role`)". That means the tool key is in the data, so no per-archive
   attribution is needed and one emitter serves both shapes. `ARC_TARBALL_TOOL`
   remains as documentation of the format only.
6. **`cache_dir` needed a placeholder.** The preset could not be pointed at a
   test (or an alternate) object cache, since the only other default is under
   `$HOME`. I added `cache_dir = "${KBU_ARC_CACHE_DIR:-}"` to all four sources
   and made the adapter treat an empty value as unset.
7. **`sqlite3.Row` has no `.get`.** Rows are materialised to plain dicts before
   any Mapping-style access.

## What I did not do, and why

- **No live store was read.** The pod is unreachable from this worktree, as the
  prompt states. Every shape here is a fixture built from Albert's 2026-10-06
  survey; the adapter has never been run against the real `runs.sqlite`, the
  real job results, the real tarball, or a real KBDL endpoint. The first
  production use is the first time any of those paths meets real bytes.
- **Tool versions ship at `"1.0"` for all six tools**, exactly as specified.
  Folding measured versions in is OP-BF4's step, and the TRANSYT label is still
  an open decision for Chris.
- **`build-baselines/kbutillib.json` was left untouched.** It is stale relative
  to a provisioned venv (99 vs 368 known-failing nodeids) and is a trap for the
  next task, but rewriting another task's recorded artifact was not asked for
  and the prompt told me not to trust it rather than to fix it. The measured
  set for this venv is recorded in the table above.
- **One pre-existing failure now passes incidentally.**
  `tests/cli/test_clearinghouse_operator.py::test_plan_invalid_manifest_exits_nonzero_naming_key`
  failed at base because the `raw_column`-for-genome guard fires before the
  content-column check and its message did not contain the substring
  `"sequence"`. My rewrite of that message (to point at the new `contig_set`
  mode) happens to contain it, so the assertion now passes. **The test's intent
  — that the offending *content column* is named — is still not met.** It is
  not a fix and I have not treated it as one.
