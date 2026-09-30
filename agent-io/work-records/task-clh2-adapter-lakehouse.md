# Work record: task-clh2-adapter-lakehouse

**Task:** Build the `lakehouse` source adapter for the clearinghouse shard stage.
**Repo:** KBUtilLib. **Branch:** `maestro/developer/task-clh2-adapter-lakehouse`.
**Base:** main tip `afcb681`.

## What was built

The `lakehouse` source adapter, implementing the `SourceAdapter` interface in
`src/kbutillib/domains/kbase/berdl/clearinghouse_shard.py`. It reads ONE Iceberg
table in the BER Data Lakehouse through `BerdlCapability.query()` and yields each
row as a plain dict for the sharder to standardize, hash, sort, and write to
bronze. It is a READ adapter only: it emits no `INSERT`/`MERGE` and never writes
to Iceberg. The one sanctioned write path remains the `load` verb, which routes
through `data_lakehouse_ingest.ingest` (schema enforcement).

Files changed:
- `clearinghouse_shard.py:271+` — added `LakehouseSourceAdapter`, the module-level
  `_normalize_rows` helper, and `LAKEHOUSE_BYTES_PER_ROW`; registered
  `"lakehouse"` in `_ADAPTER_CLASSES`; updated the module/`SourceAdapter`/
  `get_adapter` docstrings to reflect that `lakehouse` is now built and only
  `mongo` remains planned.
- `clearinghouse_schema.py:114+` — added the two NAMED namespace constants
  `CLEARINGHOUSE_NAMESPACE` (`kbaseincubator.clearinghouse`, the write target)
  and `SOURCE_GENOME_CLEARHOUSE_NAMESPACE` (`kbaseincubator.genome_clearhouse`,
  the source), with the adjacency warning.
- `agent-io/docs/clearinghouse-schema-operator-runbook.md` — corrected the two
  places describing `kbaseincubator.genome_clearhouse` as "unrelated"; it is now
  documented as the genome SOURCE, and the confuse-the-two-namespaces warning is
  strengthened (both namespaces now touched in one run; take both from the named
  constants).
- `tests/berdl/test_clearinghouse_lakehouse_adapter.py` (new) — 14 tests over a
  fake capability standing in for the read boundary; no pod, no network.
- `tests/berdl/test_clearinghouse_manifest_and_shard.py` — updated the stale
  `test_planned_adapters_are_declared_but_unbuilt` to assert only `mongo` is
  unbuilt (it previously asserted `lakehouse` was unbuilt too, which this task
  changes).

## Design decisions

- **Capability injection.** `SourceAdapter.__init__(self, source)` is fixed and
  `get_adapter(source)` passes only `source`. `LakehouseSourceAdapter` therefore
  takes an optional `capability=` kwarg that defaults to lazily constructing a
  `BerdlCapability` (deferred import, so the module stays pod-free and importable
  off-pod). `get_adapter` builds it with a lazy capability; tests inject a fake.
- **Per-engine quoting (Trino, not backticks).** The read routes through
  `query()`, which defaults to Trino in-pod (and REST→Trino off-pod). Trino
  rejects backticks (a defect this repo already paid for — 5fc10ba / 95eac55 /
  dab0fcc), so the FQN is quoted with ANSI double quotes, one pair per dotted
  namespace segment plus the table.
- **Off-pod paging.** In-pod `query()` returns the full result in one statement.
  Off-pod the REST transport caps pages, so the adapter pages with an explicit
  `limit`/`offset` (`OFF_POD_PAGE_SIZE`), ordered by the hash column for a stable
  window, and KEEPS PAGING on an exactly-full page — stopping only on a short
  page — so a source is never silently truncated.
- **estimate_bytes.** A cheap `SELECT COUNT(*)` × `LAKEHOUSE_BYTES_PER_ROW`
  (1024, an upper-ish per-row figure for the short-column genome rows). Exactness
  is not required: equal-width hash ranges self-balance and the skew guard splits
  any range that runs large.

## OP-A fact sheet, recorded per the task

`kbaseincubator.genome_clearhouse` contains exactly TWO tables:

- **`genome_quality`** — 5,812,291 rows. Columns: `genome_id`, `source_catalog`,
  `completeness`, `completeness_general`, `completeness_specific`,
  `contamination`, `quality_score`, `contig_n50`, `genome_size`, `gc_content`,
  `coding_density`, `total_cds`, `model`. Example row: `genome_id` =
  `"SAMN34263138"`, `source_catalog` = `"AllTheBacteria"`, then the numeric
  quality/size fields.
- **`skani_distances`** — 9,362,360 rows. Columns: `genome_id`, `source_catalog`,
  `kbdl_object_id`, `db_name`, `db_object_id`, `n_hits`, `top_hit`, `top_ani`,
  `distances` (and the remainder as surveyed).

`source_catalog` takes exactly three values: `BVBRC`, `AllTheBacteria`, `SPIRE`.
`genome_id` is that catalog's own accession. The 5.8M figure corroborates
Chris's independent ~5M-genomes estimate.

### The fasta_reference question — NO FASTA POINTER (the non-error path)

OP-A (Albert, verbatim): "NONE. No column in either table holds a FASTA path,
URL, or blob reference." Per the task's own success criteria, this is the
COMPLETE and CORRECT outcome, not a degraded one:

- `genome_content.fasta_reference` is left **UNPOPULATED**. A genome manifest for
  this source simply does not map `fasta_reference`; the sharder writes NULL for
  it on every row. `fasta_reference` remains a real column in the
  `genome_content` schema (verified in
  `test_fasta_reference_is_a_real_content_column_left_null`), so the null is a
  deliberate, visible omission — not a missing column.
- **NO substitute value** is written: not an empty string, not a placeholder,
  not a synthesized URL, not an assembly accession. Synthesizing a
  `fasta_reference` from `(genome_id, source_catalog)` is externally resolvable
  in principle but is a DESIGN DECISION for Chris (put to him separately), not a
  fact in the data — so it is out of scope for this task and was not done.

## Reconciliations (task prompt vs. tree)

- The prompt says the `lakehouse` adapter is unbuilt and this task builds it —
  matched the tree (`_ADAPTER_CLASSES` had only `file`; `lakehouse`/`mongo` were
  planned-but-unbuilt).
- The existing test `test_planned_adapters_are_declared_but_unbuilt` asserted
  BOTH `mongo` and `lakehouse` were unbuilt. Building `lakehouse` necessarily
  invalidates the `lakehouse` half of that assertion. This is not a weakening:
  the test was updated to assert only `mongo` remains unbuilt, which is the new
  truth this task establishes.
- The write-target namespace already had constants (`schema.TENANT`,
  `schema.NAMESPACE`); I added the fully-qualified `CLEARINGHOUSE_NAMESPACE` and
  the new `SOURCE_GENOME_CLEARHOUSE_NAMESPACE` so BOTH namespaces come from named
  constants, never a string literal at a call site (the task's namespace-safety
  requirement).

## Verification

Environment: the worktree's own `.task-venv` (Python 3.12). `pytest` and
`typing_extensions` were installed into that task venv (it lacked them);
`duckdb` is absent, so `test_clearinghouse_derivation.py` was ignored exactly as
the cached baseline (`build-baselines/kbutillib.json`) already ignores it.

- `pytest tests/berdl/test_clearinghouse_lakehouse_adapter.py` — 14 passed.
- `pytest tests/berdl/ --ignore=tests/berdl/test_clearinghouse_derivation.py` —
  **390 passed, 5 skipped** (the 5 skips are the env-gated `test_clearinghouse_live.py`
  pod tests, pre-existing and unrelated).
- Module imports cleanly off-pod:
  `import kbutillib.domains.kbase.berdl.clearinghouse_shard` succeeds and lists
  adapters `['file', 'lakehouse']`.
- Diff grep for `INSERT`/`MERGE`: the only occurrences are docstring prose
  explaining why they are forbidden; no executable INSERT/MERGE.

Exit code of the berdl run: 0.
