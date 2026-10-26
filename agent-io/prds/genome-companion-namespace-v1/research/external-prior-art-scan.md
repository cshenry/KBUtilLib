# External prior-art scan

Genome companion namespace v1 — Apache Iceberg / Trino lakehouse for bacterial and
archaeal genome annotation.

Scan date: 2026-10-25. Author: Maestro researcher task `task-86c02ac1`.
Scope: the seven research questions in the task brief, answered as findings, with
sources. Primary specifications were read directly from source (Iceberg `format/spec.md`,
Trino connector docs, INSDC feature table, GFF3 spec, Chado DDL, Ensembl `table.sql`,
refget seqcol spec, Prokka and Bakta source, UniProt user manual). Practitioner blogs
are labelled as such and never used as the sole support for a number that matters.

---

## Recommendations, up front

These are my conclusions. The evidence is in the findings below; each recommendation
names the findings it rests on.

| # | Recommendation | Rests on |
|---|---|---|
| R1 | **Fix write batching and compaction before you touch the partition spec.** The observed layout (48 rows / 48 files; 2,880 files for both the 1.1 M-row and the 14.1 M-row table) is an artefact of commit granularity and writer task fan-out, not of missing partitions. Adding partitions to the current writer multiplies the file count. | F1.1, F1.6 |
| R2 | Partition the per-gene tables as `partitioning = ARRAY['bucket(N, genome_id)']`, and on the multi-tool annotation table as `ARRAY['tool_name', 'bucket(N, genome_id)']`. `tool_name` has cardinality 6 — exactly the low-cardinality categorical that Iceberg partitioning is for — and combining identity + bucket is the documented Trino idiom. | F1.2, F1.3, F2.2 |
| R3 | Choose `N` from bytes, not from genome count. My arithmetic on your own numbers gives `N = 256` for the gene table, `N = 256` for the per-tool annotation table, and `N = 4096` for the propagated-fitness table — but see F1.4: the fitness table's extrapolated size (~32 TB) is the largest unresolved number in the design and should be settled first. Raise `iceberg.max-partitions-per-writer` above its default of 100 before writing any table with `N > 100`. | F1.3, F1.5 |
| R4 | Do **not** partition on a taxonomic rank. It is skewed (11,273 species in AllTheBacteria, dominated by *E. coli* / *S. enterica*) and, more importantly, it is *mutable*: a reclassification changes the partition value of already-written rows, which in Iceberg means rewriting those files. | F1.2 |
| R5 | Set a write-side sort order of `genome_id` (then `tool_name`, then `contig`, `start`) and enable a Parquet bloom filter on `genome_id`. On an opaque hash key, unsorted min/max statistics buy you essentially nothing; sorted min/max plus the Parquet column index plus a bloom filter is what turns a per-genome lookup from a full-partition scan into a few pages. Do not Z-order. | F2.1, F2.2, F2.3 |
| R6 | Key a gene row by `(annotation_run_id, …)`, not by `(assembly_id, locus_tag)`. Two annotations of the *same* assembly disagree about gene content and coordinates at a measured ~16 % rate, and `locus_tag` is reused across them. There is no prior art anywhere for reconciling competing callers onto one feature row; every system that faces this keeps the annotation sets separate. | F3.3 |
| R7 | Carry caller provenance as structured `(tool_name, tool_version, reference_db_name, reference_db_version, parameters)` on an `annotation_run` entity, referenced per feature — the Chado `analysis` / Ensembl `analysis` shape, which has been stable for twenty years. Do not rely on a single free-text `source` string; GFF3 column 2 and MIxS `annot` both lose the version. | F3.2 |
| R8 | Make the assembly identity digest **order-dependent and self-describing** (encode the algorithm and its parameters in the identifier string), and add a *separate, named, order-independent* digest if you need "same contigs, different order" comparisons. This is exactly what refget seqcol concluded after rejecting sorted-concatenated-hash as its primary identifier, and what `seqkit sum` (used by AllTheBacteria) does in practice. | F4.1, F4.2 |
| R9 | Model "tool ran, found nothing" as a row in the `annotation_run` table with a `result_count = 0`, not as the absence of rows in the feature table. This is the gVCF reference-block pattern and the GO `ND` evidence-code pattern. Standards-level provenance models (PROV-O, ProvONE) are silent on it. | F6.1, F6.2, F6.3 |
| R10 | Retire the predecessor tables behind an Iceberg view with the old names and columns, run a dual-write window, and only then drop. Iceberg's schema-evolution guarantees cover *adding/renaming/widening within one table*; they do nothing for a reader that references a retired table or an old column name. | F7.1, F7.2, F7.3 |

Two things in the brief I think are wrong and that change sizing decisions:

* **The gene table is ~10^10 rows at target scale, not 10^9.** 228,709 genes / 48 genomes
  = 4,765 genes per genome; × 2 × 10^6 genomes = **9.5 × 10^9**. (Derived from the brief's
  own numbers.) A 10× sizing error at the top of a partitioning decision propagates.
* **"~2 million genomes" is now 2,440,377.** The 2025 AllTheBacteria community-resource
  paper reports 2,440,377 high-quality genomes across 11,273 species
  ([PMC13370402](https://pmc.ncbi.nlm.nih.gov/articles/PMC13370402/)).

---

## Findings that bear on the design

### 1. Iceberg partitioning and file sizing for feature tables at 10^9–10^10 rows

#### F1.1 — The observed small-files pathology is a writer problem, and partitioning would make it worse

Arithmetic on the numbers in the brief (all derived, not sourced):

| Observation | Derived value |
|---|---|
| 723 MiB / 2,880 files | **251 KB average file** |
| 14,076,257 rows / 2,880 files | 4,887 rows/file |
| 1,118,422 rows / 2,880 files | 388 rows/file |
| 723 MiB / 14,076,257 rows | **53.9 bytes/row compressed** |
| 2,880 files / 48 genomes | **exactly 60 files per genome, in both tables** |
| 48 genome rows / 48 files | exactly 1 file per commit |

Two signatures, not one. The genome table's 1-file-per-row says *one commit per genome*.
The per-gene tables' *exactly 60 files per genome* in two tables of very different sizes
says *60 writer tasks per genome*, each flushing its own file regardless of size. Neither
is a partitioning symptom. Iceberg's default `write.target-file-size-bytes` is
536870912 (512 MB) ([Iceberg configuration](https://iceberg.apache.org/docs/latest/configuration/));
251 KB is **2,000× below** it.

The 251 KB figure has a second, sharper consequence: Trino's
`parquet.small-file-threshold` default is `3MB`, and below that size "a Parquet file is
read entirely"
([Trino file formats](https://trino.io/docs/current/object-storage/file-formats.html)).
Every file in the current layout is read in full on every query that touches it, so
*no* row-group or page-level pruning can reduce I/O at all today. Any measurement of
"does sorting/clustering help?" taken against the current layout is measuring nothing.

Partitioning multiplies this. Adding a partition spec with `N` values to a writer that
already emits 60 files per genome yields up to `60 × min(genomes_in_batch, N)` files per
commit. **Fix the writer first.** The concrete levers:

* Set `write.distribution-mode = hash` so rows are shuffled to one writer per partition
  rather than every task writing into every partition. Iceberg documents the modes as
  `none` (don't shuffle), `hash` (hash-distribute by partition key), `range`
  (range-distribute by partition or sort key); the default is engine-specific and "not set"
  at the table level ([Iceberg configuration](https://iceberg.apache.org/docs/latest/configuration/)).
* Batch many genomes per commit. At 2.4 M genomes, one commit per genome produces ≥2.4 M
  snapshots; planning cost and metadata size track snapshot and manifest counts, not
  partition counts (F1.5).

#### F1.2 — `bucket(N, genome_id)` vs. hash-prefix truncation vs. a taxonomic rank

**What the spec actually provides.** Iceberg defines eight partition transforms:
`identity`, `bucket[N]`, `truncate[W]`, `year`, `month`, `day`, `hour`, `void`
([spec, Partition Transforms](https://iceberg.apache.org/spec/#partition-transforms)).
`bucket[N]` is `(murmur3_x86_32_hash(x) & Integer.MAX_VALUE) % N`, using "the 32-bit
Murmur3 hash, x86 variant, seeded with 0". `truncate[W]` on a `string` is "Substring of
length `L`: `v.substring(0, L)`", truncated "to a valid UTF-8 string with no more than
`L` code points". Both accept `string` sources, so both are available for a sha256-hex
`genome_id`.

**`bucket(N)` — the standard answer, with two caveats.**
Trino's own documented example combines identity and bucket transforms:
`partitioning = ARRAY['month(order_date)', 'bucket(account_number, 10)', 'country']`
([Trino Iceberg connector](https://trino.io/docs/current/connector/iceberg.html)).
Caveat one: bucketing destroys order, so you cannot range-prune on `genome_id`
(irrelevant here — an opaque hash has no meaningful ranges). Caveat two: to compute a
bucket client-side you need the Iceberg hash. Trino exposes it as
`SELECT system.bucket('trino', 16)`, usable in a `WHERE` clause
(`WHERE system.bucket(custkey, 16) = 2`), but you do not need to: Iceberg derives the
partition predicate from the column predicate automatically — "Deriving partition
predicates from column predicates on the table data is used to separate the logical
queries from physical storage"
([spec, Partitioning](https://iceberg.apache.org/spec/#partitioning)). So
`WHERE genome_id = '…'` prunes to one bucket with no query rewriting.

**Truncate-on-hash-prefix — defensible, and better in one specific way.** Because a
sha256-derived id is already uniformly distributed, `truncate(genome_id, 2)` over hex
gives 256 near-equal partitions and `truncate(genome_id, 3)` gives 4,096, with no need
for murmur3 at all. Its real advantage over `bucket`: truncation is *order-preserving*,
so file-level min/max bounds on `genome_id` remain meaningful and partition values are
human-legible and trivially computable in any language. Its disadvantage is that the
partition count is locked to powers of 16 (256 → 4,096 is a 16× jump, with nothing in
between), whereas `N` in `bucket[N]` is free and the spec explicitly supports changing it:
"Changing the number of buckets as a table grows is possible by evolving the partition
spec" ([spec, Bucket Transform Details](https://iceberg.apache.org/spec/#bucket-transform-details)).
I did not find any published head-to-head benchmark of the two on an opaque hash key;
I am reasoning from the spec, not from a measurement.

**A low-cardinality biological attribute (taxonomy) — reject it, for two reasons.**
First, skew. AllTheBacteria's 2,440,377 genomes span 11,273 GTDB species, and the paper's
Figure 1 is specifically about the counts of the 20 most common species, with
*S. enterica* and *E. coli* at the top
([PMC13370402](https://pmc.ncbi.nlm.nih.gov/articles/PMC13370402/)). Mean 216 genomes per
species, with the head of the distribution orders of magnitude above that, is the textbook
skewed partition key — which is exactly the case for which practitioner guidance says to
reach for `bucket[N]` instead ([OLake Iceberg partitioning guide](https://olake.io/docs/writers/iceberg/partitioning/):
use `bucket[N]` "when one value dominates (e.g., a single large tenant)").
Second, and worse: **taxonomy is mutable.** Iceberg stores the partition tuple per data
file and requires "Partition values for a data file must be the same for all records
stored in the data file" ([spec, Partitioning](https://iceberg.apache.org/spec/#partitioning)).
A GTDB reclassification changes the correct partition value of rows already written, which
can only be honoured by rewriting those files. A content-derived `genome_id` never changes;
a species call does, on every GTDB release. Taxonomy belongs in a column (and in a
genome-level dimension table), not in the partition spec.

#### F1.3 — Choosing `N`: the literature gives no formula, only per-partition size targets, and they contradict each other

I looked specifically for an authoritative bucket-count formula. **There isn't one.** What
exists is per-partition and per-file size guidance, and the sources disagree by two orders
of magnitude:

| Source | Per-partition target | Per-file target |
|---|---|---|
| [Iceberg default](https://iceberg.apache.org/docs/latest/configuration/) | — | `write.target-file-size-bytes` = 536870912 (**512 MB**) |
| [Trino default](https://trino.io/docs/current/connector/iceberg.html) | — | `iceberg.target-max-file-size` = **1GB** |
| [Starburst, practitioner blog](https://www.starburst.io/blog/best-practices-for-optimizing-apache-iceberg-performance/) | "**1GB – 10GB** range, but ideally be **10GB – 100GB**"; partition only once the table is "at least **1TB**" | "100MB+ each"; keep "**100-1000 files**" |
| [Databricks guidance for Delta](https://docs.databricks.com/aws/en/lakehouse-architecture/performance-efficiency/best-practices) | do not partition tables below **1TB**; only partition by a column if each partition is "at least **1GB**" | — |
| [OLake, practitioner blog](https://olake.io/docs/writers/iceberg/partitioning/) | — | "Aim for **100 – 10,000 files** per partition folder" |
| [datalakehousehub, practitioner blog](https://datalakehousehub.com/blog/iceberg-partitioning-sort-order-zorder/) | "at least several target-sized files" per partition | 512 MB in the worked example |

Starburst's "ideally 10 GB – 100 GB per partition" and Databricks' "at least 1 GB per
partition" differ by 100×, and OLake's "100–10,000 files per partition" versus Starburst's
"100–1000 files" *for the whole table* are not reconcilable. I am flagging this rather than
picking a winner. The only numbers with the weight of a specification behind them are the
Iceberg and Trino defaults.

**Derived sizing for your tables**, using the brief's numbers and the one measured
compression figure (53.9 B/row from the fitness table), at 512 MB files and a target of
"several target-sized files per partition" (the weakest and most widely agreed criterion):

| Table | Derived rows at 2.4 M genomes | Assumed B/row | Derived size | Files at 512 MB | Suggested `N` | Per-bucket |
|---|---|---|---|---|---|---|
| `gene` | 4,765/genome × 2.44 M = **1.16 × 10^10** | 100 (strings: locus tag, product) | **~1.2 TB** | ~2,300 | **256** | ~4.5 GB, ~9 files |
| per-tool annotation | "several ×" gene ≈ 3.5 × 10^10 | 80 | ~2.8 TB | ~5,500 | **256** (× 6 tool partitions = 1,536 partitions) | ~1.8 GB/partition, ~4 files |
| propagated fitness | 61.5/gene × 1.16 × 10^10 = **7.1 × 10^11** | 53.9 (measured) | **~38 TB** | ~75,000 | **4096** | ~9.3 GB, ~18 files |

The 100 B/row and 80 B/row figures are my estimates, not measurements — the brief gives a
size only for the fitness table. **Measure them**: `SELECT sum(file_size_in_bytes),
sum(record_count) FROM "<table>$files"` gives you the real number in one query.

#### F1.4 — The propagated-fitness table is the design's biggest unresolved number

61.5 propagated-fitness rows per gene today (14,076,257 / 228,709). If that ratio is
intrinsic to the method, the table is ~7 × 10^11 rows / ~38 TB at target scale. If instead
the fitness source set is fixed (a bounded set of experiments propagated outward), the
ratio falls as the corpus grows and the table is far smaller. **The difference is between
a table you partition with `bucket(256)` and a table that dominates your entire storage
footprint and needs its own design.** I cannot resolve this from the brief; nothing in the
external literature answers it. It should be settled before `N` is chosen.

#### F1.5 — Documented failure modes of over-partitioning

**Trino will refuse the write.** `iceberg.max-partitions-per-writer` defaults to **100**
([Trino Iceberg connector](https://trino.io/docs/current/connector/iceberg.html)); exceeding
it produces `Exceeded limit of 100 open writers for partitions`. The limit originated as a
hardcoded `maxOpenWriters = 100` in `IcebergPageSink.java`
([trinodb/trino#6650](https://github.com/trinodb/trino/issues/6650)) and is now configurable.
Amazon Athena has the analogous 100-partition-per-write limit with a documented CTAS +
`INSERT INTO` workaround
([Athena docs](https://docs.aws.amazon.com/athena/latest/ug/ctas-insert-into.html)) — relevant
if any consumer reads through Athena. **Any `N > 100` requires raising this setting.**

**Planning cost tracks manifests and snapshots, not partitions.** Iceberg plans scans by
first filtering manifests using partition value ranges in the manifest list, then reading
each surviving manifest
([Iceberg performance docs](https://iceberg.apache.org/docs/latest/performance/)). So the
cost driver is manifest count. A practitioner post-mortem at scale reports a table with
"45 million data files (~50 TB data)" carrying "5 TB metadata footprint across ~200,000
files", "200KB–1MB stats per file", and a planner that OOM'd "after reading stats for ~22
million files" ([e6data, practitioner blog](https://e6data.com/blog/apache-iceberg-million-files-metadata)).
Their summary — "a million 5MB files is often worse than a few 5GB files" and "file count
kills faster than data volume" — is the single sentence I would put in front of this design.

**Stats are capped.** `write.metadata.metrics.max-inferred-column-defaults` defaults to
**100**: "Defines the maximum number of columns for which metrics are collected. Columns
are included with a pre-order traversal of the schema"
([Iceberg configuration](https://iceberg.apache.org/docs/latest/configuration/)). Wide
annotation tables silently lose min/max for columns past the 100th. Note the Trino wrinkle:
Trino "applies this limit only to ORC writes. The Parquet and Avro writers ignore it"
([Trino Iceberg connector](https://trino.io/docs/current/connector/iceberg.html)) — so under
Trino+Parquet you are not subject to it, but under Spark you are.

**Under-partitioning's failure mode** is simply that every query reads every file, because
the manifest-list partition-range filter has nothing to filter on and (F2.1) min/max on an
opaque hash prunes nothing. That is the current state.

#### F1.6 — Compaction strategy for a continuously appended table

**`rewrite_data_files` defaults, from the Iceberg Spark procedures doc**
([spark-procedures](https://iceberg.apache.org/docs/latest/spark-procedures/)):

| Option | Default |
|---|---|
| `strategy` | `binpack` (alternative: `sort`) |
| `target-file-size-bytes` | 536870912 (512 MB), inherited from `write.target-file-size-bytes` |
| `min-file-size-bytes` | **75 % of target** — files below this are rewritten regardless of other criteria |
| `max-file-size-bytes` | **180 % of target** |
| `min-input-files` | **5** — any group with ≥5 files is rewritten regardless |
| `max-file-group-size-bytes` | 107374182400 (100 GB) |
| `partial-progress.enabled` | `false`; `partial-progress.max-commits` 10 |
| `delete-ratio-threshold` | 0.3 |
| `output-spec-id` | current spec — "Data will be reorganized during the rewrite to align with the output partitioning" |

`output-spec-id` is the lever that makes partition evolution cheap to *complete*: evolve the
spec, then rewrite with `output-spec-id` pointing at the new spec, partition by partition.
`sort_order` accepts `zorder(c1,c2,c3)`, `hilbert(c1,c2,c3)`, or plain
`(ColumnName SortDirection NullOrder)` lists; "`hilbert()` and `zorder()` cannot be combined
with each other, nor with plain column sort orders, in a single `sort_order`."

**Trino equivalents** ([Trino Iceberg connector](https://trino.io/docs/current/connector/iceberg.html)):

* `ALTER TABLE t EXECUTE optimize` — merges files below `file_size_threshold`, default
  **100MB**; "By default, `optimize` writes the rewritten data files using the table's
  existing sort order, configured with the `sorted_by` table property." Accepts a `WHERE`
  on partition columns, and on metadata columns:
  `WHERE "$file_modified_time" > date_trunc('day', CURRENT_TIMESTAMP)` — which is how you
  compact only yesterday's appends.
* `ALTER TABLE t EXECUTE optimize_manifests` — "Rewrites manifest files to cluster them by
  partitioning columns… when there are partition filters in read queries but the manifest
  files are not grouped by partitions." Bounded by `commit.manifest.target-size-bytes`,
  default 8388608 (8 MB).
* `ALTER TABLE t EXECUTE expire_snapshots(retention_threshold => '7d')` and
  `remove_orphan_files(retention_threshold => '7d')`.

**Why `optimize_manifests` matters specifically for your write pattern.** The Iceberg
maintenance doc: "Manifests in the metadata tree are automatically compacted in the order
they are added, which makes queries faster when the write pattern aligns with read filters…
When a table's write pattern doesn't align with the query pattern, metadata can be rewritten
to re-group data files into manifests"
([maintenance](https://iceberg.apache.org/docs/latest/maintenance/)). Appending genome-by-genome
and reading genome-by-genome *is* aligned — one commit's files all belong to one bucket.
Appending in large multi-genome batches is *not* aligned: each batch's manifest spans all
`N` buckets, so a single-genome query must open every manifest. If you batch writes (R1),
you must also schedule `optimize_manifests`.

A maintenance cadence that follows from the above, as a starting point to be tuned:
`optimize` filtered to recent files daily; full-table `optimize` with the sort order
monthly; `optimize_manifests` after each large batch load; `expire_snapshots` daily with a
7-day threshold; `remove_orphan_files` weekly. Also set
`write.metadata.delete-after-commit.enabled = true` (default `false`) so old
`metadata.json` files are reaped — with `write.metadata.previous-versions-max` at its
default of **100**, a table that commits per genome accumulates metadata files fast
([Iceberg configuration](https://iceberg.apache.org/docs/latest/configuration/),
[maintenance](https://iceberg.apache.org/docs/latest/maintenance/)).

---

### 2. Sort order and clustering; what min/max actually buys on an opaque hash key

#### F2.1 — Unsorted min/max on a hash key prunes nothing; sorted min/max prunes to one file

Iceberg's data filtering uses, after partition values, "column-level value counts, null
counts, lower bounds, and upper bounds… to eliminate files that cannot match the query
predicate" ([performance](https://iceberg.apache.org/docs/latest/performance/)). For a
uniformly distributed hash key in an unsorted file, the `[lower_bound, upper_bound]`
interval of every file covers nearly the entire key space, so no file can be eliminated.

The clearest quantification I found is a practitioner methodology piece that measures
"median span fraction" (a file's value range width divided by the column's total range):
well-clustered columns show spans "far below 1.0" while "unsorted tables show spans close
to 1.0", and for a single-value lookup the number of files probed drops "from near the
total file count in unsorted data to one or two files after global sorting"
([datalakehousehub](https://datalakehousehub.com/blog/iceberg-partitioning-sort-order-zorder/)).
That is a practitioner blog, not a benchmark paper — but the mechanism is exactly what the
Iceberg performance doc describes, and I found no source that contradicts it.

**Statistics truncation is not a problem here.** Iceberg's `write.metadata.metrics.default`
is `truncate(16)` ([Iceberg configuration](https://iceberg.apache.org/docs/latest/configuration/)),
so string bounds are the first 16 characters. For a 64-hex-character sha256 id, 16 hex
characters = 64 bits of prefix — ample to order and separate 2.4 × 10^6 ids. You do **not**
need to raise the metrics mode for `genome_id`; you need the data sorted so the bounds mean
something.

#### F2.2 — The three pruning layers below the file, and why all three matter at 2.4 M genomes

Even with bucket partitioning at `N = 256`, a per-genome query lands in a partition holding
~2.44 M / 256 ≈ **9,500 genomes** and ~4.5 GB. Pruning *inside* the partition is where the
query is actually won. Three mechanisms, all on by default in Trino:

1. **Row-group min/max.** Row groups default to 128 MB in both Iceberg
   (`write.parquet.row-group-size-bytes` = 134217728) and Trino
   (`parquet.writer.row-group-size` = `128 MB`). On data sorted by `genome_id` this prunes
   to one row group.
2. **Parquet column/offset index (page skipping).** `parquet.use-column-index` defaults to
   `true`: "Skip reading Parquet pages by using Parquet column indices"
   ([Trino file formats](https://trino.io/docs/current/object-storage/file-formats.html)).
   Pages default to `parquet.writer.page-size` = `1 MB` with
   `parquet.writer.page-value-count` = `80000`. **This is the mechanism that makes a
   per-genome point lookup viable**: on sorted data it reduces the read from a 128 MB row
   group to a handful of 1 MB pages per projected column.
3. **Parquet bloom filters.** `parquet.use-bloom-filter` defaults to `true` for reads; write
   them via the Trino table property `parquet_bloom_filter_columns`, or Iceberg's
   `write.parquet.bloom-filter-enabled.column.<col>` (FPP default 0.01, bitset capped at
   `write.parquet.bloom-filter-max-bytes` = 1 MB). Bloom filters are per column chunk, i.e.
   per row group, so the 1 MB cap is sized against the distinct `genome_id` values *in one
   row group* (a few thousand), not against all 2.4 M — it is comfortably sufficient.
   Bloom filters are the one mechanism that prunes an opaque equality predicate **without**
   requiring the data to be sorted, which makes them the right insurance against newly
   appended, not-yet-compacted files.

So: **sort order for the compacted body of the table, bloom filter for everything.**

#### F2.3 — Write-side sort order, and why not Z-order

Trino: `sorted_by = ARRAY['genome_id', 'tool_name', 'contig', 'start']`, settable at create
time or via `ALTER TABLE … SET PROPERTIES sorted_by`, with explicit direction/null ordering
supported (`ARRAY['order_date DESC NULLS FIRST', …]`). Trino notes "Sorting is particularly
beneficial when the sorted columns show a high cardinality and are used as a filter for
selective reads" and that "The sort order applies to the contents written within each
output file independently and not the entire dataset"
([Trino Iceberg connector](https://trino.io/docs/current/connector/iceberg.html)) — i.e.
Trino gives you a *local* sort; the *global* clustering comes from the bucket partitioning
plus periodic `optimize`. Iceberg's spec is explicit that writers may skip the sort:
"Writers should use this default sort order to sort the data on write, but are not required
to if the default order is prohibitively expensive, as it would be for streaming writes"
([spec, Sorting](https://iceberg.apache.org/spec/#sorting)). Sort order is also evolvable
independently of the data, like the partition spec
([evolution](https://iceberg.apache.org/docs/latest/evolution/)).

**Do not Z-order.** Z-order and Hilbert curves trade per-column precision for multi-column
coverage, and are for the case where "two or three independent filters each matter, none
dominates"; "Adding more columns quickly leaves every column with wide file ranges"
([datalakehousehub](https://datalakehousehub.com/blog/iceberg-partitioning-sort-order-zorder/)).
Your access pattern has a dominant filter (`genome_id`, present in the primary query) and a
secondary one (`tool_name`) whose cardinality is 6 and which is therefore better handled as
an identity partition field (R2). A hierarchical sort on `(genome_id, tool_name, …)` strictly
dominates a Z-order here. The genomics analogue in the Delta world agrees in shape —
Glow/Databricks genomics guidance is Z-ORDER on `(chromosome, position)`, i.e. on the two
*independent* coordinate dimensions, not on the primary entity key
([Glow docs](https://glow.readthedocs.io/en/latest/etl/vcf2delta.html)).

**An honest limit of all of the above.** "Give me all features for one genome" at 2.4 M
genomes is a point lookup over a 10^10-row table. Parquet + Iceberg can get it down to a
few pages per column, but the design is tuned for scans, and the 512 MB / 128 MB defaults
reflect that. If the per-genome lookup is interactive and high-QPS, the literature's answer
is not "tune Iceberg harder" — it is to serve that access path from an index or a row store
and keep Iceberg for the analytic scans. I found no published pattern for sub-100ms
single-entity lookup over Iceberg at this cardinality.

---

### 3. Prior art in genome-scale annotation data models

#### F3.1 — Feature type for coding vs. non-coding: everyone uses one typed column from a controlled vocabulary, never a boolean or separate tables

| System | Mechanism | Concretely |
|---|---|---|
| **INSDC / GenBank / ENA** | Feature *key* from a closed vocabulary, plus a mandatory sub-class qualifier for the catch-all | Separate keys `CDS`, `tRNA`, `rRNA`, `tmRNA`, `ncRNA`, `misc_RNA`. `ncRNA` has **mandatory** `/ncRNA_class="TYPE"` drawn from the INSDC ncRNA controlled vocabulary, with the explicit escape hatch: classes "not yet in the INSDC `/ncRNA_class` controlled vocabulary can be annotated by entering `/ncRNA_class="other"` with either `/product=…` OR `/note=…`". And a hard rule: "the ncRNA feature is not used for ribosomal and transfer RNA annotation, for which the rRNA and tRNA feature keys should be used" ([INSDC Feature Table, `FT_current.txt`](https://ftp.ebi.ac.uk/pub/databases/embl/doc/FT_current.txt)) |
| **GFF3** | Column 3 `type` | "constrained to be either a term from the Sequence Ontology or an SO accession number… it must be `sequence_feature` (SO:0000110) or an `is_a` child of it" ([GFF3 spec](https://github.com/The-Sequence-Ontology/Specifications/blob/master/gff3.md)) |
| **Sequence Ontology** | Sibling terms under `gene` (SO:0000704) | `protein_coding_gene` (SO:0001217), `ncRNA_gene` (SO:0001263) ([SO browser](http://www.sequenceontology.org/browser/current_svn/term/SO:0001217)) |
| **Chado (GMOD)** | `feature.type_id` → `cvterm` | One typed column, SO-valued. Uniqueness is `unique (organism_id, uniquename, type_id)` — note type participates in the key ([`sequence.sql`](https://github.com/GMOD/Chado/blob/master/chado/modules/sequence/sequence.sql)) |
| **Ensembl core** | `gene.biotype VARCHAR(40)` + a `biotype` lookup table | The `biotype` table carries `biotype_group` ("'coding', 'pseudogene', 'snoncoding', 'lnoncoding', 'mnoncoding', 'LRG', 'undefined', 'no_group'") **and** `so_acc` / `so_term` mapping each biotype to its Sequence Ontology accession ([`table.sql`](https://github.com/Ensembl/ensembl/blob/main/sql/table.sql)) |
| **NCBI Datasets** | Aggregate counts by class | `geneCounts.total`, `.proteinCoding`, `.nonCoding` ("transcribed non-coding genes (e.g. lncRNAs, miRNAs, rRNAs, etc…)"), `.pseudogene`, `.other` ([genome assembly data report](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/reference-docs/data-reports/genome-assembly/)) |
| **Bakta** | `feature["type"]` | Values include `cds`, `sorf`, `tRNA`, `tmRNA`, `rRNA`, `ncRNA`, `ncRNA-region`, `crispr`, `oriC`/`oriV`/`oriT`, `gap` ([Bakta docs](https://bakta.readthedocs.io/en/latest/cli/inputoutput.html), [`bakta/io/json.py`](https://github.com/oschwengers/bakta/blob/main/bakta/io/json.py)) |

**Bearing on the design.** A single `feature_type` column, SO-accession-valued (or
INSDC-feature-key-valued with a separate `so_term` column, which is what Ensembl's
`biotype` table does), with a second `feature_subclass` column for the `ncRNA_class`
dimension. The grouping you actually want to query on ("all non-coding") should be a
*derived* column or a lookup table — Ensembl's `biotype_group` is exactly that pattern, and
it exists because `biotype` alone is too granular to filter on. Do not model coding vs.
non-coding as a boolean; both INSDC and Ensembl needed three-plus groups (coding,
pseudogene, several flavours of non-coding), and a boolean cannot represent `pseudogene`.

#### F3.2 — Per-feature caller provenance including tool and version: the `analysis` table is the twenty-year-stable answer

**The structured answer, twice independently invented.**

Chado, `companalysis` module ([`companalysis.sql`](https://github.com/GMOD/Chado/blob/master/chado/modules/companalysis/companalysis.sql)):

```sql
create table analysis (
    analysis_id bigserial primary key,
    name varchar(255), description text,
    program varchar(255) not null,
    programversion varchar(255) not null,
    algorithm varchar(255),
    sourcename varchar(255), sourceversion varchar(255), sourceuri text,
    timeexecuted timestamp not null default current_timestamp,
    constraint analysis_c1 unique (program, programversion, sourcename)
);
```

with `analysisfeature (feature_id, analysis_id, rawscore, normscore, significance, identity)`
and `unique (feature_id, analysis_id)` linking each feature to the run that produced it.
Note which columns are `not null`: `program` **and** `programversion`. And note
`sourceuri`: "an optional, permanent URL or URI for the source of the analysis. The idea is
that someone could recreate the analysis directly by going to this URI and fetching the
source data (e.g. the blast database, or the training model)."

Ensembl core ([`table.sql`](https://github.com/Ensembl/ensembl/blob/main/sql/table.sql)):

```sql
CREATE TABLE analysis (
  analysis_id SMALLINT UNSIGNED AUTO_INCREMENT,
  created datetime, logic_name VARCHAR(128) NOT NULL,
  db VARCHAR(120), db_version VARCHAR(40), db_file VARCHAR(120),
  program VARCHAR(80), program_version VARCHAR(40), program_file VARCHAR(80),
  parameters TEXT, module VARCHAR(80), module_version VARCHAR(40),
  gff_source VARCHAR(40), gff_feature VARCHAR(40),
  UNIQUE KEY logic_name_idx (logic_name)
);
```

`gene.analysis_id` is `NOT NULL`, and so is the `analysis_id` on `transcript`,
`protein_feature`, `simple_feature`, `repeat_feature`, `prediction_transcript`, and
`object_xref`. **Every assertion in Ensembl carries its producing analysis.** The
separation of `program`/`program_version` from `db`/`db_version` is the distinction you need
for BAKTA (tool version *and* database version are independent), and `parameters TEXT`
holds the invocation.

**The semi-structured answer, and its limits.** INSDC's `/inference` qualifier carries tool
*and version* in a structured string:

```
Value format  "[CATEGORY:]TYPE[ (same species)][:EVIDENCE_BASIS]"
   CATEGORY ∈ {COORDINATES, DESCRIPTION, EXISTENCE}
   TYPE     ∈ {"ab initio prediction", "profile", "alignment",
               "similar to AA sequence", "nucleotide motif", "protein motif", …}
   EVIDENCE_BASIS is "either a reference to a database entry (including accession
   and version) or an algorithm (including version)"
Example       /inference="COORDINATES:profile:tRNAscan:2.1"
              /inference="ab initio prediction:Genscan:2.0"
              /inference="alignment:Splign:1.26p:RefSeq:NM_000041.2,INSD:BC003557.1"
```

([INSDC Feature Table](https://ftp.ebi.ac.uk/pub/databases/embl/doc/FT_current.txt); the
recommended acronym vocabulary is at
<https://www.insdc.org/submitting-standards/inference-qualifiers/>.) This is a genuinely
good design — category, evidence type, tool, version, and reference accession in one
parseable field — and it is what Prokka emits. Prokka's own source:

```perl
my $tool = "Aragorn:".$tools{aragorn}->{VERSION};
...  -source => $tool,
     -tag    => { 'inference' => "COORDINATES:profile:$tool", ... }
```

and for Prodigal, `my $tool = "Prodigal:".$tools{prodigal}->{VERSION};`
([`prokka`](https://github.com/tseemann/prokka/blob/master/bin/prokka)). So in Prokka output
the tool *and version* appear both in GFF3 column 2 and in `/inference`. Note also, at
`prokka:599`, the literal source comment `# FIXME version ?` on the rRNA path — per-feature
version provenance is imperfect even in the reference implementation.

**Where the community standards fall short, concretely:**

* **GFF3 column 2 carries the tool but not, by spec, the version**: "The source is a free
  text qualifier intended to describe the algorithm or operating procedure that generated
  this feature. Typically this is the name of a piece of software… In effect, the source is
  used to extend the feature ontology by adding a qualifier to the type"
  ([GFF3 spec](https://github.com/The-Sequence-Ontology/Specifications/blob/master/gff3.md)).
  Free text, single-valued, no version slot. Prokka putting `Aragorn:1.2.38` there is a
  convention, not a standard.
* **Bakta records versions at the *run* level, not the feature level.** `bakta/io/json.py`
  writes a single top-level `version` object: `{"bakta": <cfg.version>, "db": {"version":
  "<major>.<minor>", "type": …}}` ([`json.py`](https://github.com/oschwengers/bakta/blob/main/bakta/io/json.py)).
  Per-feature, the tool identity is implied by `type` and by the CDS annotation sub-objects
  (`ips`, `psc`), not stated.
* **MIxS is weakest of all.** `annot` (MIXS:0000059) is "Tool used for annotation, or for
  cases where annotation was provided by a community jamboree or model organism database
  rather than by a specific submitter", expected value "name of tool or pipeline used, or
  annotation source description", example `prokka` — **range String, cardinality 0..1**
  ([MIxS term annot](https://genomicsstandardsconsortium.github.io/mixs/0000059/)). You
  cannot express "annotated by BAKTA 1.9.3 *and* PROKKA 1.14.6" in MIxS at all. Contrast
  `assembly_software`, whose description does demand "Tool(s) used for assembly, including
  version number and parameters"
  ([MIxS Sequencing subset](https://genomicsstandardsconsortium.github.io/mixs/Sequencing/)) —
  the standard is internally inconsistent about whether versions are required.
* **NCBI Datasets** has the structured fields but only per assembly, not per feature:
  `annotationInfo.name` ("Annotation name"), `.provider` ("Source of the annotation"),
  `.releaseDate`, `.method` ("Software tools used to calculate annotation"), `.pipeline`
  ("NCBI annotation pipeline used"), `.softwareVersion`, `.status`
  ([genome assembly data report](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/reference-docs/data-reports/genome-assembly/)).
* **NMDC** separates it cleanly along PROV lines: `FunctionalAnnotation` has
  `subject` (→ GeneProduct), `has_function`, and `was_generated_by` (→ a
  `MetagenomeAnnotation` / `WorkflowExecution`), with versioning carried on the workflow
  execution, not the annotation: `WorkflowExecution.version` is "The NMDC release tag for a
  given workflow release used for data processing" and `git_url` is "The url that points to
  the exact software repository location used to run a workflow", alongside
  `execution_resource`, `started_at_time`, `ended_at_time`, `was_informed_by`, `has_input`,
  `has_output`
  ([FunctionalAnnotation](https://microbiomedata.github.io/nmdc-schema/FunctionalAnnotation/),
  [WorkflowExecution](https://microbiomedata.github.io/nmdc-schema/WorkflowExecution/)).
  `git_url` is a nice touch your design could copy: a commit-pinned repository URL is
  stronger provenance than a version string.

**Bearing on the design.** Build an `annotation_run` table with the union of the Chado and
Ensembl column sets plus NMDC's `git_url`: `(annotation_run_id, tool_name, tool_version,
reference_db_name, reference_db_version, parameters, git_url, started_at, ended_at,
execution_resource)`, with a uniqueness constraint in the spirit of Chado's
`unique (program, programversion, sourcename)`. Reference it from every feature and every
annotation row. Do **not** store a free-text `source` string as the only provenance; the two
mature relational schemas both rejected that, and MIxS's single-string `annot` is the
cautionary example.

#### F3.3 — Multiple competing annotations of the same feature: nobody merges them, and the measured disagreement rate explains why

This is the most consequential finding in the scan.

**INSDC/NCBI keep competing annotations as separate top-level records.** A GenBank
assembly (`GCA_…`) carries the submitter's annotation; the RefSeq copy of the *same*
assembly (`GCF_…`) carries NCBI's PGAP annotation. "A RefSeq (GCF) genome assembly
represents an NCBI-derived copy of a submitted GenBank (GCA) assembly"; RefSeq re-annotated
all prokaryotic assemblies with PGAP v3 in 2015 and v4.1 in 2017, and now runs a rolling
re-annotation schedule, while "GenBank records are not revisited as often"
([GCA and GCF explained](https://www.ncbi.nlm.nih.gov/datasets/docs/v1/reference-docs/gca-and-gcf-explained/)).
Two annotations, two accessions, no per-feature reconciliation.

**And the disagreement is large.** Siqueira, Eng, Mukhopadhyay & Guazzaroni (2025),
"Differences in GenBank and RefSeq annotations may affect genomics data interpretation for
*Pseudomonas putida* KT2440", bioRxiv 2025.05.27.656432,
doi:[10.1101/2025.05.27.656432](https://doi.org/10.1101/2025.05.27.656432), compared the two
annotations of a *single, identical* assembly and found:

* **159** genes present only in the GenBank annotation;
* **81** genes present only in the RefSeq annotation;
* **897** genes — "approximately 16 % of total ORFs" — with **identical locus tags but
  different genomic positions**, of which 455 differ in start codon and 442 in stop codon,
  with offsets from ≤27 bp to a few hundred bp.

The authors' conclusion is that researchers must "deliberately choose, and faithfully
describe, their specific reference materials."

**Ensembl's answer is the same shape: separate databases.** The core schema's `biotype.db_type`
enum includes `'core'`, `'otherfeatures'`, `'rnaseq'`, `'vega'`, `'cdna'` and others
([`table.sql`](https://github.com/Ensembl/ensembl/blob/main/sql/table.sql)). The
`otherfeatures` database is a parallel database on the *same core schema* holding an
alternative annotation set. Ensembl does not put two callers' genes in one `gene` table.

**Chado's answer is to make the analysis part of feature identity.** `analysisfeature` has
`unique (feature_id, analysis_id)`, and the companion `analysis` is keyed on
`(program, programversion, sourcename)` — so the same genomic interval called by two
programs is two `feature` rows, each decorated by its own `analysisfeature`.

**Bearing on the design — and this is a schema-level consequence, not a tuning tip:**

1. **A gene's identity must be scoped by its annotation run.** `(assembly_id, locus_tag)` is
   not a key: the KT2440 result shows the same `locus_tag` denoting different coordinates in
   two annotations of the same assembly. Key genes on `(annotation_run_id, …)`.
2. **Do not build a "canonical gene" table that multiple tools write functional annotations
   into.** The brief lists six callers (BAKTA, PROKKA, DRAM2, RAST, KOFAMSCAN, TRANSYT); the
   first four are *structural* callers that will produce mutually incompatible gene sets. If
   your functional-annotation table joins to a gene table, it must join to the gene table
   *of the same structural run*.
3. **If you need cross-tool comparison, model it explicitly** as a derived
   feature-correspondence table (`gene_a_id, gene_b_id, relationship, overlap_bp`), computed
   and versioned like any other analysis. No external schema I found provides a standard for
   this, so it is yours to define; the honest framing is "there is no prior art, we are
   inventing it, here is the evidence that we must."

---

### 4. Sequence/assembly identity

#### F4.1 — refget sequences: content-addressed single sequences are settled

Two checksum algorithms serve as sequence identifiers: MD5 (32 hex chars, namespaced
`md5:6aef897c3d6ff0c78aff06ac189178dd`) and the GA4GH digest, written `SQ.` + base64url,
e.g. `SQ.aKF498dAxcJAqme6QYQ7EZ07-fiw8Kw2`. The older `TRUNC512` is deprecated.
**Normalization is mandatory and specified**: "all non-base symbols (`\n`, spaces, etc) must
be removed and then the rest uppercased"; the GA4GH digest is SHA-512 of the canonicalized
bytes, base64url-encoding the first 24 bytes. The spec is candid about truncation risk —
"the consequence of a sequence derived hash collision appearing would be catastrophic" —
and commits only to increasing the retained byte count if collisions appear. Circular
chromosome support is optional, declared via `service-info`
([refget sequences spec](https://ga4gh.github.io/refget/sequences/)).

**Lesson for an ad-hoc scheme**: the normalization rule is half the specification. A
`sha256` of "the contigs" is undefined until you have written down case handling, line
terminators, non-ACGT symbol handling, and whether the record header participates.

#### F4.2 — refget seqcol: the spec explicitly considered and rejected "sha256 of sorted contigs" as the primary identifier

This is the direct answer to the question in the brief, in the spec's own words
([seqcol rationale](https://github.com/ga4gh/refget/blob/master/docs/seqcols/seqcol_rationale.md)):

> "it would be simple to just digest each sequence, sort the digests, concatenate them, and
> then digest that string — and in fact, this was an early simple proposal for the sequence
> collection standard. But while this would work perfectly fine for the simple use case of
> asserting two *sets of sequences* match in content, this is not the only use case…"

The rejection argument: "for many aligners, the order of the sequences matter. Therefore,
the final digest representing the collection should *differ* if the underlying sequences had
different order… the output of the aligner is going to specify the location of each read
*with respect to the name of a sequence*… Therefore, the final digest should *differ* if the
names of the sequences do not match." And the general principle: "the final digest
representing a sequence collection can only answer *one* such question."

**What seqcol specifies instead** ([seqcol spec](https://ga4gh.github.io/refget/seqcols/),
[README source](https://github.com/ga4gh/refget/blob/master/docs/seqcols/README.md)):

* **Digest algorithm.** Five steps: (1) organize into the canonical seqcol object
  representation and **filter out non-inherent attributes**; (2) apply
  [RFC-8785 JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785) to each
  attribute value individually; (3) digest each with the GA4GH digest algorithm
  (`sha512t24u` → 32-character base64url string); (4) apply RFC-8785 again to the resulting
  object; (5) digest again.
* **Order-dependence is deliberate.** `names`, `lengths` and `sequences` are **collated**
  arrays — "the values of the attribute match 1-to-1 with the sequences in the collection
  and are represented in the same order." The base schema's
  `ga4gh.inherent` list is `[names, sequences]` — `lengths` is *not* inherent and does not
  contribute to the digest. So the top-level identity digest **is** order-dependent and
  **does** depend on contig names.
* **Order-independence is a separate, named, non-inherent attribute.** `sorted_sequences`
  (OPTIONAL): "Take the array of the `sequences` attribute… and sort it lexicographically",
  giving "a digest representing an order-invariant set of unnamed sequences", explicitly
  motivated by the fact that doing this through the comparison function "could be
  computationally prohibitive" at scale. Likewise `sorted_name_length_pairs` (RECOMMENDED)
  gives "a digest for an order-invariant coordinate system", computed by digesting each
  canonicalized `{"name":…,"length":…}` object and **sorting the digests
  lexicographically**.
* **Comparison is a first-class operation, not digest equality.** The `/comparison`
  endpoint returns `digests`, `attributes` (`a_only` / `b_only` / `a_and_b`), and
  `array_elements` with `a_count`, `b_count`, `a_and_b_count` and **`a_and_b_same_order`**,
  which is "*true* if all matching elements are in the same order in the two arrays", and
  `null` when fewer than two elements overlap or when duplicates are unbalanced
  ([compare_collections](https://github.com/ga4gh/refget/blob/master/docs/seqcols/compare_collections.md)).
* **Interoperability warning.** "We RECOMMEND *not changing the inherent attributes list*,
  because this will keep the identifiers compatible across implementations. Implementations
  that use different inherent attributes are still compliant… but do so at the cost of
  top-level digest interoperability." A private digest definition is a private namespace.

**The documented pitfalls, restated as they apply to you.** An order-*dependent* digest
means a tool that re-sorts contigs by length (very common: SPAdes, many submission
pipelines) produces a *different* genome id for identical biology. An order-*independent*
digest means two collections that will give different alignment outputs share an id, and
you lose the ability to detect a contig-reordering. seqcol's resolution — order-dependent
primary identity plus optional order-independent secondary digests — is the only published
resolution I found, and it is the one I recommend.

#### F4.3 — What AllTheBacteria actually does, and it is informative

ATB's assembly table stores `assembly_seqkit_sum`, "the output of running `seqkit sum` on
the assembly" ([ATB SQLite metadata](https://allthebacteria.org/docs/metadata_sqlite/)).
Reading `seqkit sum`'s source
([`sum.go`](https://github.com/shenwei356/seqkit/blob/master/seqkit/cmd/sum.go)), the
algorithm is documented in the command's own help text:

```
2. Computing the hash (xxhash) for all sequences or k-mers of a circular
   complete genome (-c/--circular).
3. Sorting all hash values, for ignoring the order of sequences.
4. Computing MD5 digest from the hash values, sequences length, and
   the number of sequences.
...
We add meta information to the message digest, with the format of:
    seqkit.<version>_<seq type><seq structure><strand>[with seqid]_<kmer size>_<seq digest>
e.g. seqkit.v0.1_DCD_k31_e59dad6d561f1f1f28ebf185c6f4c183
```

So the de-facto standard in the 2.4 M-genome corpus is: **order-independent** (hashes are
sorted), optionally **rotation-independent** for circular genomes (k-mer based, with the
digest changing "with different values of k-mer size"), sequence-ID-insensitive by default
("sequence headers and qualities are skipped, only sequences matter") with an opt-in
`-i/--include-id`, and — critically — **the identifier string carries its own algorithm
version and parameters** (`seqkit.v0.1`, `DCD`, `k31`).

That last property is the one design detail I would copy verbatim. A bare
`sha256:<64 hex>` genome id is unversioned: the day you change the normalization rule you
silently fork the namespace with no way to tell old ids from new. Encode the recipe:
`atb.v1_sha256_sorted_<hex>`.

#### F4.4 — "Identical assemblies, biologically distinct genomes": there is no standard, and content-addressing cannot express it

I looked for a standard way to say "these two genomes have identical assemblies but are
biologically distinct" (a knockout mutant and its wild-type parent). **I did not find one,
and I believe the reason is structural rather than an oversight.**

seqcol states the design intent plainly: non-inherent attributes exist precisely so that
"the same collection with two different authors" does not get different digests
([README](https://github.com/ga4gh/refget/blob/master/docs/seqcols/README.md)). A
content-derived identifier is *defined* to collide for identical content; that is its
entire value proposition. So a content-addressed assembly id **must** collide for a
knockout whose assembly is byte-identical to its parent, and no amount of spec reading will
change that.

What the ecosystems do instead is keep biological identity in a *different* identifier
space: INSDC's `/strain`, `/isolate`, `/sub_strain` and `/mut` qualifiers on the source
feature; NCBI's `biosample` ("NCBI BioSample from which the sequences in the genome assembly
were obtained") carried alongside `accession` in the assembly data report
([NCBI data report](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/reference-docs/data-reports/genome-assembly/));
ATB's `sample_accession` as the primary key of the `assembly` table, with
`assembly_seqkit_sum` as a *column*, not the key
([ATB SQLite metadata](https://allthebacteria.org/docs/metadata_sqlite/)).

**Bearing on the design: separate the two entities.** `assembly` keyed by the content
digest (one row per distinct byte content, deduplicated by construction), and `genome`
keyed by a surrogate or sample-scoped id with a foreign key to `assembly` and columns for
strain / isolate / genotype / BioSample. A knockout and its parent are then two `genome`
rows pointing at one `assembly` row — which is also the truthful representation, since they
genuinely do share an assembly. Collapsing them into one entity keyed on the digest loses
information irrecoverably. This is my recommendation, extrapolated from how ATB and NCBI
structure their tables; it is not something any standard mandates.

---

### 5. Identifier carriage / alias tables

#### F5.1 — The shape everyone converges on: a namespace table + an (entity, namespace, accession) link table + a currency flag

| System | Vocabulary table | Link table | Currency / history |
|---|---|---|---|
| **Chado** | `db` (+ `dbxref` with `unique (db_id, accession, version)`, and an explicit `version` column) | `feature_dbxref (feature_id, dbxref_id, is_current)` plus `feature.dbxref_id` for the single primary | `feature_dbxref.is_current` — "True if this secondary dbxref is the most up to date accession in the corresponding db. **Retired accessions should set this field to false**" |
| **Ensembl** | `external_db (db_name, db_release, status, priority, type, …)` with `UNIQUE KEY (db_name, db_release)` | `object_xref (ensembl_id, ensembl_object_type, xref_id, linkage_annotation, analysis_id)` with `UNIQUE (xref_id, ensembl_object_type, ensembl_id, analysis_id)`; `xref (external_db_id, dbprimary_acc, display_label, version, info_type, info_text)` | `stable_id_event (old_stable_id, old_version, new_stable_id, new_version, mapping_session_id, type, score)` + `mapping_session`, `gene_archive`, `peptide_archive`, `seq_region_mapping` |
| **INSDC** | `/db_xref="<database>:<identifier>"`, where "the complete list of allowed database types is kept at <https://www.insdc.org/submitting-standards/dbxref-qualifier-vocabulary/>" | multiple `/db_xref` per feature | `/locus_tag` (stable) + `/old_locus_tag` ("feature tag assigned for tracking purposes") |
| **UniProtKB** | `dbxref.txt`, a release-stamped controlled list — **190 `Abbrev:` entries** in release 2026_03, each with `AC` (e.g. `DB-0286`), `Abbrev`, `Name`, `Ref`, and a `Db_URL` template | `DR` lines, multi-valued | primary + secondary accessions; `delac_sp.txt` / `delac_tr.txt` deleted-accession lists |
| **Bioregistry** | a metaregistry "synthesiz[ing] and substantially expand[ing] upon 20 existing registries" (Hoyt *et al.* 2022) | — | — |

Sources: [Chado `sequence.sql`](https://github.com/GMOD/Chado/blob/master/chado/modules/sequence/sequence.sql),
[Chado `db.sql`](https://github.com/GMOD/Chado/blob/master/chado/modules/db/db.sql),
[Ensembl `table.sql`](https://github.com/Ensembl/ensembl/blob/main/sql/table.sql),
[INSDC `FT_current.txt`](https://ftp.ebi.ac.uk/pub/databases/embl/doc/FT_current.txt),
[UniProt `dbxref.txt`](https://ftp.uniprot.org/pub/databases/uniprot/current_release/knowledgebase/complete/docs/dbxref.txt),
[Hoyt *et al.*, *Sci Data* 9:714 (2022)](https://doi.org/10.1038/s41597-022-01807-3).

#### F5.2 — What they do when an identifier changes while a sibling identifier stays stable

This is the question in the brief, and there are four distinct, well-documented answers.

**(a) INSDC: a stable tag plus an append-only list of retired tags, on the feature.**
`/locus_tag` is "a submitter-supplied, systematic, **stable** identifier for a gene and its
associated features, used for tracking purposes"; "If a `/locus_tag` needs to be re-assigned
the `/old_locus_tag` qualifier should be used to store the old value." And the cardinality
rule is exactly what an append-only alias table needs: "Multiple `/old_locus_tag`
qualifiers with distinct values are allowed within a single feature; `/old_locus_tag` and
`/locus_tag` values must not be identical within a single feature." (Contrast
`/locus_tag`: "Multiple `/locus_tag` values are not allowed within one feature for entries
created after 15-OCT-2004.") There is also a negative format constraint — a `/locus_tag`
value "should not be in a format which resembles INSD accession numbers, accession.version,
or `/protein_id` identifiers", and the value format is given as `"text"(single token) but
not "<1-5 letters><5-9 digit integer>[.<integer>]"`. ([INSDC Feature Table](https://ftp.ebi.ac.uk/pub/databases/embl/doc/FT_current.txt))

**(b) UniProtKB: never delete an accession; add primaries, retain the old ones as
secondary.** From the user manual, verbatim
([UniProt user manual §3.2](https://web.expasy.org/docs/userman.html)):

> "Entries will have more than one accession number if they have been merged or split. For
> example, when two entries are merged into one, the accession numbers from both entries are
> stored in the AC line(s). **If an existing entry is split into two or more entries (a rare
> occurrence), the original accession numbers are retained in all the derived entries and a
> new primary accession number is added to all the entries.** An accession number is dropped
> only when the data to which it was assigned have been completely removed from the
> database."

**The demerge case is the one that breaks naive alias-table designs**, and it is worth
stating explicitly: after a split, one secondary accession legitimately maps to *several*
entities. So `UNIQUE (namespace, external_id)` is **wrong** for an alias table; the key is
`UNIQUE (namespace, external_id, entity_id)` with an `is_primary` (or Chado's `is_current`)
flag, and resolution of a non-primary identifier may legitimately return more than one row.
UniProt also publishes deleted-accession tombstone files (`delac_sp.txt`, `delac_tr.txt`),
i.e. "this id existed and is gone" is itself recorded data — the same instinct as F6.

**(c) Ensembl: a full event log, not a flag.** `stable_id_event` records, per mapping
session, "what happened to all gene, transcript and translation stable IDs during a mapping
session. This includes which IDs where deleted, created and related to each other. Each
event is represented by one or more rows", with `old_stable_id`/`old_version` →
`new_stable_id`/`new_version`, a `type` enum, and a `score` ("Combined mapping score"). A
`NULL` on one side encodes creation or deletion; many-to-many rows encode splits and merges;
the `score` records *how confident* the mapping is. `mapping_session`,
`gene_archive`/`peptide_archive` (retired sequences) and `seq_region_mapping` complete the
picture ([`table.sql`](https://github.com/Ensembl/ensembl/blob/main/sql/table.sql)).

**(d) Chado: a boolean, with a documented semantic.** `feature_dbxref.is_current`,
"Retired accessions should set this field to false" — and separately `feature_synonym` with
both `is_current` ("whether the linked synonym is the current -official- symbol") and
`is_internal` (a synonym retained for lookup but not for display — the documented use case
is "there was a typo which went public").

**Bearing on the design.** An append-only alias table with:
`(entity_id, namespace, external_id, is_primary, valid_from, valid_to, source_run_id)` and
`UNIQUE (namespace, external_id, entity_id)` — **not** `UNIQUE (namespace, external_id)`.
If identifier succession matters (it does, if any consumer caches ids), add Ensembl's
`stable_id_event` shape as a separate table rather than trying to encode succession in flags
on the alias table; a boolean cannot represent a split.

#### F5.3 — Validation and vocabulary control for the namespace column

Four practices, all documented:

1. **A closed, centrally published list, versioned.** INSDC: "the complete list of allowed
   database types is kept at …/dbxref-qualifier-vocabulary/". UniProt: `dbxref.txt`, carrying
   `Release: 2026_03 of 02-Sep-2026` and 190 abbreviations, each with a stable internal
   `AC` (`DB-0286`) *as well as* a human abbreviation. **Give the namespace a stable
   surrogate key, not just a string** — UniProt did, Ensembl did (`external_db_id`), Chado
   did (`db_id`). Renaming a namespace then costs one row.
2. **Version the namespace, not just the identifier.** Ensembl's
   `UNIQUE KEY (db_name, db_release)` on `external_db` makes "GO as of release X" a distinct
   namespace row. Chado's `dbxref` has its own `version` column and
   `unique (db_id, accession, version)`, documented as "A dbxref is generally written as
   `<DB>:<ACCESSION>` or as `<DB>:<ACCESSION>:<VERSION>`."
3. **Regex validation per namespace.** Bioregistry records a pattern for both the local
   unique identifier and the CURIE — for GO, `^\d{7}$` and `^GO:\d{7}$` respectively
   ([Bioregistry GO record](https://bioregistry.io/registry/go)). UniProt publishes its own:
   `[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}`
   ([UniProt user manual](https://web.expasy.org/docs/userman.html)). A lakehouse can't
   enforce a regex as a constraint, so this belongs in the ingest validator plus a scheduled
   assertion query.
4. **Beware the "banana."** Bioregistry's term for "a redundant prefix and delimiter that
   may appear in local unique identifiers" — for GO, the banana is `GO:`, so `GO:0006915`,
   `0006915` and `GO:GO:0006915` all occur in the wild and Bioregistry "automatically strips
   off the banana when standardizing local unique identifiers and CURIEs"
   ([Bioregistry GO record](https://bioregistry.io/registry/go)). Your four target
   ontologies are precisely the bad cases: **GO** carries a banana; **EC** numbers are
   dotted with variable depth and `-` wildcards (`1.1.1.-`); **KO** is `K#####` with an
   embedded letter; **COG** is `COG####`. Decide once whether the `external_id` column
   stores the bare local id or the CURIE, write it in the schema comment, and validate it —
   this is the single most likely source of silent join failures in the whole design. GFF3
   has the same convention and the same ambiguity: both `Dbxref` and `Ontology_term` take
   "the ID of the cross referenced object in the form `DBTAG:ID`", with the parsing rule
   "IDs can contain unescaped colons but DBTAGs cannot, so parsing code should split on the
   first colon encountered"
   ([GFF3 spec](https://github.com/The-Sequence-Ontology/Specifications/blob/master/gff3.md)).
5. **Provide a documented escape hatch, so that unknown values don't become dirty data.**
   INSDC's `ncRNA_class="other"` + mandatory `/product` or `/note` is the model: an explicit
   "not in the CV" value plus a required free-text field, rather than letting submitters
   invent namespace strings.

One further Ensembl detail worth copying: `xref.info_type` is an enum —
`NONE, PROJECTION, MISC, DEPENDENT, DIRECT, SEQUENCE_MATCH, INFERRED_PAIR, PROBE, UNMAPPED,
COORDINATE_OVERLAP, CHECKSUM` — recording *how* the cross-reference was derived, and
`object_xref.analysis_id` records *which run* derived it. An alias table whose rows don't
say how they were obtained cannot be audited or selectively retracted.

---

### 6. Provenance of a negative result

The brief's requirement — "tRNA caller ran and found nothing" must be distinguishable from
"tRNA caller never ran" — is real and under-served. My finding is: **the generic provenance
standards are silent; the domain-specific conventions exist and are good; the annotation
tools themselves do it badly.**

#### F6.1 — W3C PROV and ProvONE: an Activity with no generated Entity is representable but not assertable

PROV-O's starting-point terms are `prov:Entity`, `prov:Activity`, `prov:Agent`,
`prov:wasGeneratedBy`, `prov:used`, `prov:wasAssociatedWith`, `prov:startedAtTime`,
`prov:endedAtTime`, `prov:wasDerivedFrom`; the software/plan dimension is carried by
`prov:Plan` via `prov:qualifiedAssociation` / `prov:Association`
([PROV-O](https://www.w3.org/TR/prov-o/)). Because an `Activity` is a first-class resource,
an activity with no outgoing `wasGeneratedBy` is perfectly legal, and that is as close as
PROV comes. **PROV defines no vocabulary for asserting that an activity produced nothing**,
and therefore no way to distinguish "produced nothing" from "outputs not recorded" —
PROV-O is explicitly an open-world, incomplete-by-design model. I checked ProvONE
(`Process` / `ProcessExec`, `InputPort` / `OutputPort`, reusing `prov:wasGeneratedBy`;
[ProvONE model](http://vcvcomputing.com/provone/provone.html)) and found the same gap. If
someone has written a ProvONE or RO-Crate profile for empty results, I did not find it.

**So: do not expect a standard to carry this for you.** A row saying "the run happened" is
necessary but not sufficient; you need a positive cardinality assertion.

#### F6.2 — The bioinformatics conventions that *do* solve it, and they are the right model

**(a) GO's `ND` evidence code — the canonical statement of the problem.** `ND` ("No
biological Data available") "indicates that a curator has looked but not been able to find
information that supports making an annotation to any term", and "should be used only for
annotations to the root terms, Molecular Function (GO:0003674), Biological Process
(GO:0008150), or Cellular Component (GO:0005575)". The purpose, stated in the GO wiki
almost in the brief's own words: it "allows users to distinguish between unannotated genes
(for which the literature has not been completely reviewed and thus no GO annotation has
been made) and uncharacterized genes (GO annotation with ND code)"
([GO wiki, ND evidence code](https://wiki.geneontology.org/No_biological_Data_available_(ND)_evidence_code)).
Note the *mechanism*: a row is written, pointing at a sentinel term, carrying an evidence
code that means "we looked". Absence of a row means "we didn't look."

**(b) gVCF reference blocks — the same idea in a high-volume data format.** A gVCF records
variant sites *and* banded runs of confidently non-variant positions as explicit records
with the symbolic ALT allele `<NON_REF>`, an `END` INFO tag, and per-block confidence
(minimum `GQ`); "A reference block means the caller assessed and was confident in reference;
no block at all means it was never assessed"
([GATK, GVCF](https://gatk.broadinstitute.org/hc/en-us/articles/360035531812-GVCF-Genomic-Variant-Call-Format)).
The motivation is precisely the design hazard in the brief: in plain VCF, "a merge has no way
to tell whether a sample missing from the merged file at a given position was confidently
reference-homozygous or simply never assessed there." gVCF additionally shows the *scaling*
answer: you band the negative result (one row per interval) rather than materialising one
row per negative observation.

**(c) Ensembl `unmapped_object` / `unmapped_reason` — the relational implementation.**

```sql
CREATE TABLE unmapped_object (
  unmapped_object_id INT UNSIGNED AUTO_INCREMENT,
  type ENUM('xref','cDNA','Marker') NOT NULL,
  analysis_id SMALLINT UNSIGNED NOT NULL,
  external_db_id INT UNSIGNED, identifier VARCHAR(255) NOT NULL,
  unmapped_reason_id INT UNSIGNED NOT NULL,
  query_score DOUBLE, target_score DOUBLE,
  ensembl_id INT UNSIGNED DEFAULT '0', ensembl_object_type ENUM(...), parent VARCHAR(255)
);
CREATE TABLE unmapped_reason (
  unmapped_reason_id INT UNSIGNED AUTO_INCREMENT,
  summary_description VARCHAR(255), full_description VARCHAR(255)
);
```

([`table.sql`](https://github.com/Ensembl/ensembl/blob/main/sql/table.sql)). A dedicated
table for "this analysis attempted this object and produced nothing", with a `NOT NULL`
`analysis_id`, a controlled reason vocabulary, and the scores that were achieved. This is
the closest relational prior art to what the brief asks for, and it is twenty years old.

**(d) AllTheBacteria's `status` file — the pipeline-level version.**
`status.202505.tsv.gz` carries `Sample`, `Status` ("PASS" or "FAIL:…"), `Dataset`,
`HQ_filter`, `Assembly_on_OSF`, and "tracks the result of trying to download the reads, run
sylph, assemble, and then human decontamination"; there is also a dedicated
`sylph.no_matches.tsv.gz` for "samples without matches" and
`hq_set.removed_samples.tsv.gz` for rejected samples
([ATB metadata docs](https://allthebacteria.org/docs/sample_metadata/)). The corpus you
intend to ingest already ships its negative results as first-class files — and as separate
files per negative *kind*. If you ingest ATB, ingest these too; dropping them loses the
ability to distinguish "not in our lakehouse" from "ATB tried and failed".

**(e) GO's `NOT` qualifier** asserts the stronger claim that a gene product is *not*
associated with a term — a different thing from "no data", and a reminder to keep the three
states apart: *not attempted*, *attempted, nothing found*, *attempted, negative result
established*. I read the GO evidence-code guide and did not find the `NOT` qualifier
documented on that page; I am reporting the distinction, not a verified GO specification
of `NOT`.

#### F6.3 — What the annotation tools in your pipeline actually do: nothing

Prokka's tRNA path ends with `msg("Found $num_trna tRNAs")` — a **log line**. Zero tRNAs
produces no record in the GFF, GBK, or TSV output; the only trace is stdout
([`prokka`](https://github.com/tseemann/prokka/blob/master/bin/prokka)). Bakta's JSON
carries `stats` and a top-level `version` object, but the `features` array simply omits
absent types ([`json.py`](https://github.com/oschwengers/bakta/blob/main/bakta/io/json.py)).
So **the information you need is not in the tools' output files** and must be captured at
pipeline-orchestration time, from the exit status and the per-type counts, into your own
run table. This is not a schema choice you can defer to the ingest layer later — the signal
is destroyed if you don't capture it at run time.

**Bearing on the design.** An `annotation_run` table with one row per
`(assembly_id, tool_name, tool_version, feature_type_attempted)` carrying
`status ∈ {succeeded, failed, skipped}`, `result_count` (0 is meaningful and must be
distinguishable from NULL), `exit_code`, `started_at`, `ended_at`, and a controlled
`reason_code` on the Ensembl `unmapped_reason` model. Absence of a row means "never
attempted" — and that must be written down in the schema comment, because it is the one
piece of semantics a reader cannot infer. Follow gVCF and band where you can: one row per
`(run, feature_type)` with a count, not one row per negative observation.

---

### 7. Schema evolution and deprecation in a lakehouse

#### F7.1 — What Iceberg guarantees, exactly

Supported schema changes are **add, drop, rename, update (widen), reorder**, and "Iceberg
schema updates are **metadata changes**, so no data files need to be rewritten." The
correctness guarantees are enumerated: added columns never read existing values from
another column; dropping a column doesn't change other columns' values; updating a column
doesn't change others; reordering doesn't change name→value association. The mechanism is
column IDs: "When you add a column, it is assigned a new ID so existing data is never used
by mistake" — contrasted explicitly with name-tracking formats that "can inadvertently
un-delete a column if a name is reused"
([evolution](https://iceberg.apache.org/docs/latest/evolution/)).

Valid primitive type promotions, from the spec
([Schema Evolution](https://iceberg.apache.org/spec/#schema-evolution)):

| From | v1/v2 | v3+ |
|---|---|---|
| `unknown` | — | any type |
| `int` | `long` | `long` |
| `date` | — | `timestamp`, `timestamp_ns` (not `timestamptz`) |
| `float` | `double` | `double` |
| `decimal(P,S)` | `decimal(P',S)` if `P' > P` | same |

**And one trap that bears directly on bucket partitioning:** "Type promotion is not allowed
for a field that is referenced by `source-id` … of a partition field if the partition
transform would produce a different value after promoting the type. For example, `bucket[N]`
produces different hash values for `34` and `"34"` (2017239379 != -427558391)… when an `int`
field is the source for a bucket partition field, it may be promoted to `long` but not to
`string`." Once `genome_id` is a bucket source, **its type is effectively frozen.** Declare
it `string` (or `fixed`/`binary` if you decide to store raw digest bytes) and never revisit.

Partition spec and sort order are both independently evolvable and non-rewriting: "the old
data written with an earlier spec remains unchanged. New data is written using the new spec
in a new layout. Metadata for each of the partition versions is kept separately… you get
split planning"; "Partition evolution is a metadata operation and does not eagerly rewrite
files" ([evolution](https://iceberg.apache.org/docs/latest/evolution/)). The spec's
cautions: "When evolving a spec, changes should not cause partition field IDs to change",
and partition fields with an unknown transform must be ignored by readers but must never be
written ([spec, Partition Evolution](https://iceberg.apache.org/spec/#partition-evolution)).

**What Iceberg does *not* give you — the limits that matter for retiring a table set:**

1. **Nothing protects a reader that references a retired *table*.** All of the above is
   within-table.
2. **A rename is safe for the data and breaking for the consumer.** Field IDs make the
   rename free; every SQL query, dbt model and notebook that names the old column still
   breaks.
3. **Drop-then-re-add with the same name does not restore old data.** The new column gets a
   new ID, so old files read `NULL`. (This follows directly from guarantee #1 above.)
4. **Trino's `ALTER TABLE SET PROPERTIES` is restricted** to `format`, `format_version`,
   `partitioning`, `sorted_by`, `max_commit_retry`, `delete_after_commit_enabled`,
   `max_previous_versions`, `max_inferred_metrics_column_defaults`,
   `object_store_layout_enabled`, `data_location`
   ([Trino Iceberg connector](https://trino.io/docs/current/connector/iceberg.html)).
   `target_max_file_size` and `parquet_writer_row_group_size` are **not** in that list —
   plan the sizing properties at `CREATE TABLE` time or expect a `CREATE OR REPLACE TABLE AS`
   (which Trino does support as an atomic operation producing a new snapshot).

#### F7.2 — Views as the compatibility shim: Iceberg specifies them, Trino implements them

The Iceberg **view spec** gives a catalog-level, versioned view object:
`current-version-id`, a `version-log` with "the timestamp and `version-id` for every change
to `current-version-id`", and per-version `representations` — "A view version can have more
than one representation. All representations for a version must express the same underlying
definition. Engines are free to choose the representation to use", with the SQL
representation carrying a `dialect` field ("e.g., \"trino\" or \"spark\"") and at most one
representation per dialect. Crucially: "**View versions are immutable.** Once a version is
created, it cannot be changed… If a view definition changes… a new version must be created"
([Iceberg view spec](https://github.com/apache/iceberg/blob/main/format/view-spec.md)).

Trino's Iceberg connector "supports SQL view management" with a `location` view property,
and separately supports materialized views (view definition + an Iceberg storage table,
with `storage_schema` and snapshot-retention properties)
([Trino Iceberg connector](https://trino.io/docs/current/connector/iceberg.html)).

**Bearing on the design.** A versioned, multi-dialect, immutable-per-version view object is
exactly the right shim: create views carrying the *predecessor* table names and column
names over the successor tables, so existing readers keep working unchanged while you cut
over. The version log gives you an audit trail of when the shim's definition changed. Note
two caveats I would verify before relying on them: the view spec's multi-dialect support
means a view written by Trino may not be readable by Spark unless a Spark representation is
also stored; and a view cannot reproduce a *column type* narrowing, so a predecessor column
whose type you want to change must be cast in the view body.

#### F7.3 — The deprecation process itself: expand / migrate / contract

The pattern is well-established outside the lakehouse world and transfers directly. Three
phases ([Pete Hodgson, "Expand/Contract: making a breaking change without a big bang"](https://blog.thepete.net/blog/2023/12/05/expand/contract-making-a-breaking-change-without-a-big-bang/)):

1. **Expand** — make the backward-compatible change: create the successor tables, add the
   new columns. Nothing the old readers depend on is removed.
2. **Migrate** — dual-write to both old and new; backfill history; cut reads over behind a
   flag, gradually. "Dual-write is more expensive at the application layer — every write
   path gets an extra branch — but it converts a single high-risk cutover into a series of
   low-risk, independently revertible steps."
3. **Contract** — drop the old tables and the dual-write code once nothing reads the old
   path.

For the lakehouse specialisation, the practitioner consensus is: treat schemas as APIs,
enforce compatibility contracts in CI, run v1 and v2 in parallel, validate automatically
([schema versioning for analytics](https://warpdriven.ai/blog/industry-1/schema-versioning-best-practices-analytics-deprecate-without-chaos-109)
— practitioner blog, cited for the pattern, not for any number). I found no
peer-reviewed or vendor-specification treatment of lakehouse table-set deprecation; this
part of the literature is entirely practitioner-blog grade, and I am labelling it as such.

One Iceberg-specific addition the generic pattern misses: **you can tell whether anyone is
still reading the predecessor.** Query the table's `$snapshots` / history and your engine's
query log, and keep the predecessor for at least one full `expire_snapshots` retention
window after the last observed read, so a reader that discovers the breakage can time-travel
back while you fix it.

---

### 8. Where the literature is silent or contradictory

Stated plainly, because the output contract asks for it:

1. **No formula for bucket count.** I looked specifically. The guidance that exists is
   per-partition and per-file size targets, and those **contradict each other by up to 100×**
   (F1.3: Databricks "≥1 GB per partition" vs. Starburst "ideally 10 GB–100 GB"; OLake
   "100–10,000 files per partition" vs. Starburst "100–1000 files" per table). The
   "bucket(8/16 for 1–10 GB, 32/64 for 10–100 GB, 128/256 for 100 GB–1 TB" table that
   surfaces in search results **does not appear in the sources it is attributed to** — I
   checked the OLake page it was credited to and it contains no such table. Treat it as
   unsourced.
2. **No published head-to-head of `bucket[N]` vs. hash-prefix `truncate[W]`** on an opaque
   high-cardinality key. My comparison in F1.2 is reasoning from the spec's stated semantics,
   not from a measurement.
3. **No standard for "identical assembly, distinct genome."** F4.4. Content-addressing is
   *defined* to collide on identical content; the ecosystems solve this by keeping
   biological identity in a separate identifier space, but no specification says so.
4. **No standard for reconciling competing structural annotations onto one feature.** F3.3.
   Every system I examined keeps the annotation sets separate instead. If you need
   cross-caller feature correspondence, you are inventing it.
5. **PROV, PROV-O and ProvONE are silent on empty results.** F6.1. The useful prior art is
   all domain-specific (GO `ND`, gVCF reference blocks, Ensembl `unmapped_object`, ATB
   `status.tsv`) and none of it is a standard you can cite as compliance.
6. **MIxS is internally inconsistent about tool versions** — `assembly_software` demands
   "version number and parameters", `annot` is a single optional string with example
   `prokka`. F3.2. Don't treat MIxS as a sufficient provenance model.
7. **Lakehouse table-set deprecation has no specification-grade literature.** F7.3 rests on
   practitioner blogs plus the Iceberg view spec.
8. **The quantitative claims about Iceberg metadata at scale** (45 M files / 5 TB metadata /
   OOM at ~22 M files; planning latency growth with snapshot count) come from practitioner
   post-mortems, not from a benchmark with a published methodology. The *mechanism* is
   documented in the Iceberg performance docs; the *magnitudes* are anecdotal. I used them
   for direction, not for sizing.
9. **I could not verify the ATB species-count skew quantitatively.** I have the cardinality
   (11,273 species over 2,440,377 genomes) and the qualitative statement that *S. enterica*
   and *E. coli* head the distribution, but not the per-species counts. The recommendation
   against taxonomic partitioning (R4) rests mainly on mutability, which is certain, and only
   secondarily on skew, which I have not measured.

---

## What I did not search, and why

**Deliberately out of scope**

* **Internal KBUtilLib / BERDL design documents, the existing table DDL, and the current
  writer code.** The task asked for an *external* prior-art scan. Consequences: everything
  in F1.1 and F1.3 is arithmetic on the five numbers in the brief, not on the real tables.
  Before acting on R3 you should replace my assumed bytes-per-row with
  `SELECT sum(file_size_in_bytes), sum(record_count) FROM "<table>$files"`, and confirm the
  "60 files per genome" diagnosis by looking at the writer. If the brief's numbers are
  stale or rounded, my derived table sizes move with them.
* **Benchmarking anything.** No Trino or Iceberg cluster was available in this worktree, and
  the task is a literature scan. Every performance statement here is sourced or derived, and
  none is measured. The two claims I would most want measured before committing: (a) the
  per-genome read amplification under `bucket(256)` + sort + bloom filter, and (b) whether
  `truncate(genome_id, 2)` beats `bucket(256)` in practice.
* **The six annotation tools' algorithms and relative quality** (BAKTA vs. PROKKA vs. DRAM2
  vs. RAST vs. KOFAMSCAN vs. TRANSYT). The question was about schema prior art, not caller
  choice. I read Bakta's and Prokka's *output schemas* and provenance handling only.
* **Fitness-data methodology** (RB-TnSeq, fitness propagation by orthology). F1.4 flags the
  sizing consequence but I did not investigate whether 61.5 rows/gene is intrinsic. That is
  a domain question for the pipeline authors, not a literature question, and it is the single
  highest-value open item.

**Searched but not resolved, and what would resolve it**

* **IMG/M's actual relational schema.** JGI publishes pipeline SOPs (the MAP v.4 and MGAP
  SOPs) but not the IMG database DDL, and I found no public schema document. The SOPs
  establish that annotation is stamped with a pipeline version at the genome level; I could
  not verify whether IMG carries per-feature caller provenance. Resolving it needs either a
  JGI contact or the IMG GitLab (`code.jgi.doe.gov/img/img-pipelines/img-annotation-pipeline`),
  which I did not attempt to read.
* **ENA's own relational schema.** I used INSDC's feature-table specification, which ENA
  implements and which is the normative document, but not ENA's internal tables. I judged
  the spec sufficient; if you need ENA-specific behaviour (e.g. how ENA stores multiple
  `/old_locus_tag` values), the schema would be needed.
* **AllTheBacteria's released Parquet schema.** This is the closest prior art to the design
  and I got only partway. I established the file set (`assembly.parquet`,
  `assembly_stats.parquet`, `checkm2.parquet`, `sylph.parquet`, `run.parquet`,
  `mlst.parquet`), some column names (`sample_accession`, `sylph_species`, `N50`,
  `Completeness_General`, `aws_url`), the ~35 GB download footprint, and that these are
  **flat, one-file-per-table, unpartitioned** — i.e. ATB is *not* prior art for partitioning
  a 10^10-row feature table, because its released tables are 2.4 M-row sample metadata. What
  I did **not** establish: whether the 2025 "AI-ready sequence tables" mentioned in the
  community-resource paper are partitioned, and what their per-gene layout is. **This is the
  highest-value remaining search in the whole scan** — downloading one and running
  `parquet-tools inspect` would answer, in minutes, several questions I answered from
  first principles. I did not do it because it is a 35 GB download on a shared host.
* **RO-Crate / Workflow Run Crate profiles for empty results.** I confirmed PROV-O and
  ProvONE are silent (F6.1) and found the 2023 Workflow Run Crate paper
  ([arXiv:2312.07852](https://arxiv.org/abs/2312.07852)) but did not read its profile
  specification to check whether a `CreateAction` with an empty `result` is given a defined
  meaning. If a standard for this exists anywhere, that is where it would be.
* **GO's `NOT` qualifier specification.** The GO evidence-code guide I read does not cover
  qualifiers. I report the three-state distinction in F6.2(e) as a conceptual point, not as a
  verified GO spec. The GAF 2.2 specification would settle it.
* **The Iceberg v4 manifest changes.** Several sources reference Parquet-based manifests in
  Iceberg v4 as a fix for metadata-layer scaling. The brief specifies `format_version 2`, so
  I did not pursue it — but if this namespace will live for years, v3 (deletion vectors;
  Trino calls v3 support "experimental") and v4 (columnar manifests) are worth a dedicated
  look before the partition spec is frozen, since F7.1 shows some choices (bucket source
  column type) are effectively permanent.
* **Trino's `preferred write partitioning` behaviour** relative to
  `iceberg.max-partitions-per-writer`. I documented the limit and its default of 100 (F1.5)
  but did not determine whether Trino's write-side repartitioning means a single writer
  never sees more than a handful of buckets in practice, which would make the limit a
  non-issue. This needs a test on your cluster, not more reading.
* **Non-English and paywalled literature.** WebSearch here is US-indexed; I did not use
  institutional access for anything behind a paywall. The one paper that matters most
  (Siqueira *et al.* 2025) is an open bioRxiv preprint, so this cost nothing in that case.
