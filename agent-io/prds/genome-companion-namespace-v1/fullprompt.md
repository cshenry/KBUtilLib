# Genome companion namespace (`kbaseincubator.genome_companion`)

## Research Report

### Important findings

**1. `fitness` is not partitioned at all, and its file layout is already pathological at 48 genomes.**
`SHOW CREATE TABLE` on five `fitness` tables returns no `PARTITIONED BY` clause,
and the Iceberg `$partitions` metadata returns a single row per table.
The file counts are the finding:
`genome` holds 48 records in **48 files** (161 KB total, ~3.4 KB per file);
`assembly` 47 records in **47 files**;
`function_annotation` 1,118,422 records in **2,880 files** (41 MB, ~14 KB per file);
`propagated_fitness` 14,076,257 records in **2,880 files** (723 MB).
One row per file on the two small tables means the writer commits per row.
Scaling 48 genomes to the ATB corpus's ~2M is a factor of ~41,667,
which puts `gene` near 10^9 rows and, under the current writer, file counts in the millions.
**Partitioning is not merely unspecified here; it is absent, and the write path is the actual defect.**

**2. The partitioning lever this platform actually has is narrower than Iceberg's.**
`clearinghouse_schema.table_configs()` emits only `name`, `schema_sql` and an optional
`partition_by`, and `clearinghouse_bootstrap_adapter._create_table_sql` renders that as
`PARTITIONED BY (\`col\`)` over **bare stored columns**.
There is no properties key, so `write.target-file-size-bytes`, Iceberg sort order,
`write.distribution-mode` and Parquet Bloom filters are **not expressible through this write path at all**
(`clearinghouse_shard.py` says so in terms: "DO NOT try to set an Iceberg sort order ...").
An Iceberg transform such as `bucket(256, genome_id)` therefore cannot be requested here.
This is the single most important constraint on the partitioning decision and it is a property
of our own library, not of Iceberg.

**3. The problem in finding 1 has already been solved once, in this codebase.**
`clearinghouse_shard.py` implements range-cut sharding against exactly this constraint:
estimate total bytes, cut the hex space `[00..00, ff..ff]` into `N = ceil(total/target)`
equal-width ranges (boundaries **computed**, not sampled, because a sha256 key is uniform by
construction), stream once into a bounded sort buffer, and emit each range by external k-way
merge as one ~512 MB sorted file.
It controls the only thing it can — the bytes it writes — and gets file size and physical row
order that way. **The companion namespace should reuse it rather than invent a second answer.**

**4. The clearinghouse already decided, with reasons, not to partition on a hash.**
`clearinghouse_schema.py`: "a good hash scatters uniformly by design, so partitioning on it buys
zero read pruning while multiplying partition count."
Its live partition map is `standardizer_version` for `gene_entity`/`gene_content`/`protein_*`,
`source` for all five `<type>_result` tables, and nothing at all for the other six.
It also split one physical table per entity type because the corpus carries a ~5000:1 row-count
skew and "Iceberg table PROPERTIES ..., query PLANNING scope, and COMMIT concurrency are all
table-level and cannot be tuned per partition."
That rationale transfers directly.

**5. Two of the fourteen shortcomings dissolve on measurement, and one is far worse than stated.**
- `evidence` **is** linked — in the direction nobody looked. `function_annotation.evidence_id`,
  `ontology_annotation.evidence_id` and `transyt_annotation.evidence_id` all point *at* it.
  The row counts reconcile exactly: 1,118,422 + 1,010,656 + 374,965 = **2,504,043**, which is
  `evidence`'s row count to the row, and the per-tool split reconciles for all six tools
  (BAKTA 286,532 + 412,495 = 699,027; DRAM2 281,900 + 311,232 = 593,132;
  PROKKA 328,804 + 147,863 = 476,667; RAST 221,186; KOFAMSCAN 139,066; TRANSYT 374,965).
  So it is a strict 1:1 satellite — and it carries **no information**: 0 of 2,504,043 rows have a
  non-null `evidence_score` or `evidence_probability`, its `evidence_type` merely repeats the
  parent row's `tool`, and its 2,504,043 rows hold only 2,388,346 distinct `evidence_id`s, so it is
  not even a key. It is 2.5M rows of pure indirection.
- `annotation` is not just "wide and tool-shaped". **All 272,076 of its rows are orphans**:
  a `LEFT JOIN ... WHERE gene.gene_id IS NULL` returns 272,076. Its `feature_id` is
  `organism:locus` (`acidovorax_3H11:Ac3H11_1`) while `gene.gene_id` is `{uuid}.{locus}`, and it
  covers 60 distinct organisms against `gene`'s 48 genomes. It is a dead table from an earlier load.

**6. The assembly-scoped decision meets a real counterexample in live data, and survives it.**
`genome` holds 48 rows with 47 distinct `assembly_hash` and zero nulls.
The duplicate pair is `SyringaeB728a` and `SyringaeB728a_mexBdelta` —
**a knockout mutant and its wild-type parent, sharing a byte-identical assembly.**
Assembly-scoped identity is still right, but it follows that an assembly hash **cannot be the
genome's primary key**; it is a many-to-one foreign key. The live corpus already proves it.

**7. `fitness.assembly_hash` IS rule A. Measured, not inferred — and it is good news.**
This was flagged as the one item decision (2) inherits unmeasured, on the grounds that "if even one
row is rule B it is a live data problem and not only a documentation gap".
It is settled, and a row count could never have settled it: the `fitness` corpus (48 genomes,
`source='fitnessDB'`) and the KBDL corpus (4,865 genomes) are **disjoint**, so the intersection of
their hashes is zero under *either* rule. What settles it is recomputing the hash from the bytes.
Three assemblies were fetched from KBase workspace 262939 via their `fasta_handle_ref` and both
rules computed over the real FASTA:

| ref | contigs | bytes | `fitness.assembly_hash` | rule A | rule B |
|---|---|---|---|---|---|
| `262939/41/3` | 1 | 5,049,960 | `a28c8492…9317e` | **match** | no (`a1371107…`) |
| `262939/27/2` | 16 | 5,102,818 | `468dcec3…cc7e3` | **match** | no (`91d48f51…`) |
| `262939/85/3` | 7 | 7,655,939 | `8cbb62e3…79352` | **match** | no (`63cc2ef6…`) |

Rule A is `standardizers.entity_hash("genome", parse_fasta_contigs(raw))`; rule B is
`sha256(raw FASTA bytes)`. Two of the three are multi-contig, so the **sort** step in rule A is
load-bearing and still matches. **So it is a documentation gap and not a data problem**, and the
consequence for this design is better than neutral: `fitness.assembly_hash` already *is* the
clearinghouse's canonical genome entity hash, so the companion namespace joins to
`clearinghouse.genome_entity` with no translation layer.

**8. The source model is richer than the brief records, in three places that change the schema.**
`NonCodingType` has **six** members, not five: `repeat_region` was added for Bakta calling
(`kbdl-build-genome-bakta-calling-v1`) and is a CRISPR array, not an RNA gene.
`CanonicalNonCodingFeature` carries `partial` and `source_id` beyond the five named qualifiers,
plus `extra="allow"` passthrough.
`NonCodingProvenance` has **nine** fields, not five:
`source`, `callers`, `cds_dropped_overlapping_rna`, `cds_overlap_drop_fraction`, `gene_caller`,
`cds_discarded`, `bakta_db`, `bakta_image`, `bakta_driver_sha256`.

**9. Supersession is live machinery with zero live hits against genomes.**
`object_supersedes` holds 28,051 rows, all `reason='atb-recompression-v1'`.
Joined against the 4,865 rows of `genomes`, **0** match on `genome_object_id` and **0** on
`fasta_object_id`, and there are **0** chains of depth > 1.
So the transitive resolver specified by `kbdl-add-non-coding-to-existing-genome-v1` is correct to
build and is, today, entirely unexercised against the genome registry.

**10. `inner_loop`'s 39 `v2_*` tables are not a superset-minus of `fitness`.**
Measured: they include `v2_models`, `v2_model_reactions`, `v2_model_fva`, `v2_reactions`,
`v2_reaction_fva`, `v2_pangenome_neighbors`, `v2_protein_families`, `v2_protein_family_members`,
`v2_mappings`, `v2_annotation_combinations` and more — a modelling layer for which `fitness` has
no equivalent table at all. Retiring `inner_loop` wholesale would drop that layer on the floor.

**11. `KBDLStoreLoad` has run zero times, confirmed by absence.**
`SELECT job_type, state, COUNT(*) FROM jobs` returns rows only for `KBDLBuildGenome`
(4,891 completed / 10 failed) and `KBDLGenomeAnnotation` (3,341 completed / 33 failed).
There is no incumbent loader to preserve compatibility with.

**12. The external scan confirmed the central finding, supplied the constant, and found a defect I had.**
Three results worth promoting out of the linked scan. (a) Independently of me it concluded that the
write path, not the partition spec, is the defect -- "adding partitions to the current writer
MULTIPLIES the file count". (b) ~512 MB is Iceberg's own documented default
`write.target-file-size-bytes` (536,870,912), so the sharder's target is the spec default rather than
a transfer from a foreign corpus. (c) **It found a real collision in this PRD's first draft**: keying
`gene` on `gene_id` alone is unsound, because `genome_id` is derived from the assembly content hash
and `genome_registry._lookup_existing_genome_id` dedupes on `content_hash`, so a rebuild under a
different `gene_caller` reuses the same `{genome_id}_{N}` id space with different coordinates. The
measured analogue is stark: Siqueira et al. 2025 found 897 genes -- ~16% of ORFs -- with IDENTICAL
locus tags at DIFFERENT positions between the GenBank and RefSeq annotations of one identical
assembly. That produced the `genome_build` table and Q4, and it is the clearest return on the
research step in this session.

### Links to full research output

- [External prior-art scan](research/external-prior-art-scan.md) — Iceberg partitioning and file
  sizing at 10^9 rows, genome-annotation data-model prior art (RefSeq, Ensembl, Chado, GFF3/INSDC,
  GA4GH refget/seqcol), identifier-carriage practice, negative-result provenance, and lakehouse
  deprecation practice. **Ran on the CLAUDE backend, not codex** -- see the quality report.
  **LANDED** (86,906 bytes) and folded -- see finding 12, and Q2/Q3/Q4, each of which it changed.
  Note it was verified by reading the committed artifact off the task branch, NOT by Maestro's
  status field, which still read `running` after the artifact was committed.
- [Live lake measurement](research/live-lake-measurement.md) — every number quoted above, with the
  query that produced it.

### Quality report

**What was searched.** The live BER Data Lakehouse, directly, from h100 on 2026-10-25 between
22:15 and 22:24 CDT: all 19 `kbaseincubator.fitness` tables for columns and row counts, five of
them for `SHOW CREATE TABLE` and `$partitions`, plus eleven targeted integrity and vocabulary
queries; `kbaseincubator.inner_loop`'s table list; the 123-namespace catalogue; and
`kbaseincubator.clearinghouse`'s 19 tables.
The live KBDL SQLite store (`/scratch/kbdl/kbdl.sqlite`, 15 GB) read-only for registry counts,
job states, and supersession integrity.
KBUtilLib source: `clearinghouse_schema.py`, `clearinghouse_bootstrap_adapter.py`,
`clearinghouse_shard.py`, `clearinghouse_capability.py`, `capability.py`.
KBDL source: `schemas/build_genome.py` (the canonical models in full), `genome_normalize.py`
(id minting), `genome_registry.py`, `clearinghouse/__init__.py`, `job_types/store_load.py`.
`council-record-lakehouse-v1`'s `fullprompt.md` for the provenance triple, the partition table and
the delivery rail.
Three real assemblies fetched from KBase workspace 262939 (`262939/41/3`, `262939/27/2`,
`262939/85/3`) through `WorkspaceClient.get_objects2` and `KBWSUtils.download_blob_file`, with
`standardizers.entity_hash("genome", parse_fasta_contigs(raw))` and `sha256(raw)` computed over the
real bytes -- the measurement that settled the hash rule (finding 7).

**What was NOT searched, and why.**
- **`fitness`'s writer.** Every file-count pathology in finding 1 is a property of whatever wrote
  these tables, and I did not find that code. It is probably not in KBUtilLib or KBDL — the rows
  carry `source='fitnessDB'` and `created_at` timestamps clustered at two instants
  (2026-10-16T03:35:28 and 2026-10-17T18:07:25), which reads as two bulk loads from outside our
  repos. **The remediation in Q3 is therefore specified against our own write path, which I can
  read, and is unverified against theirs, which I cannot.** If `fitness` is to be rewritten in
  place rather than superseded, that writer must be found first.
- **Whether anything reads `fitness` today.** I measured what is in it, not who queries it.
  Q13's retirement path assumes readers exist and must be given a window; if none do, that window
  is wasted work, and if a reader exists that I have not named, the window may be too short.
- **The `kbaseincubator.genome_clearhouse` namespace** (note the spelling — a different namespace
  from `clearinghouse`, holding `genome_quality` and `skani_distances`). It is plainly genome-adjacent
  and I did not open it. It may duplicate or conflict with tables proposed here.
- **Trino/Iceberg version in this deployment,** which bounds which partition transforms and table
  properties are available at all. Q3 is specified to stay inside what our own library can request,
  which makes the answer robust to this gap, but a future widening of the write path would want it.

**THE CROSS-FAMILY ADVERSARY DID NOT RUN, AND THE EXTERNAL SCAN RAN ON CLAUDE.**
Stated prominently because it is the weakest part of this document's provenance.
`codex` on h100 has a dead credential as of 2026-10-26 03:20Z: the binary reports
*"Failed to refresh token: Your access token could not be refreshed because your refresh token was
already used"*, then HTTP 401 `token_expired`, then `Missing environment variable: OPENAI_API_KEY`.
Two independent Maestro dispatches confirmed it, mine (`task-646181ed`) and a sibling session's
(`task-9fba5f0e`), both returning `status=failed / summary='subprocess exit 1'` in under sixty
seconds with no diagnostic. The consequences are deliberately different for the two steps:

- **Confront is SKIPPED, not substituted.** The cross-family invariant is structural -- a Claude
  adversary attacking a Claude-written PRD is not a confront -- so there is no fallback and
  `data.json` records `confront: "skipped: codex credential dead on h100 ..."`. **This PRD has NOT
  been adversarially attacked.** Its decisions have been checked against measurement, not against a
  hostile reader, and that is a real gap in its provenance. Fixing it needs an interactive codex
  login (device/browser flow), which cannot be done headlessly.
- **The external scan was re-dispatched on the Claude backend** (`task-86c02ac1`) because it is a
  research step and not an adversarial one, where a different family is a nice-to-have rather than
  the point. It is still a second model with web access doing work this session did not do. But it
  is same-family, so it inherits this session's blind spots, and the partitioning numbers in Q3 are
  the place where that matters most.

**Confidence.** High on everything measured — the numbers above are live reads, reproducible from
the recorded queries. Medium on Q3's sizing constants, which are transferred from
`clearinghouse_shard.py`'s target rather than measured on this corpus. Low on Q13's retirement
sequencing, for the reader-inventory gap named above.

---

## Revision Log

**Round 0 — 2026-10-25 (draft).** First draft, written single-pass. All sixteen judgement calls
decided and reflected in the body. Three items from the fourteen-shortcoming agenda changed shape
on measurement: #9 (`evidence` linkage) resolved to "linked, and worthless"; #8 (`annotation`)
resolved to "100% orphan, drop it"; #11 (partitioning) resolved to "absent, and the write path is
the defect".

---

## Problem Statement

KBDL builds genomes. It has built 4,865 of them, and it is pointed at a corpus of roughly two
million. Each built genome carries contigs, protein-coding genes, non-coding RNA genes with their
qualifiers, a record of which gene callers ran and at what version, a taxonomy call, and
annotations from up to six independent tools.

None of it is queryable. A scientist who wants to ask "how many archaeal genomes in the corpus
have no tRNA genes at all" — or "which genes did BAKTA and PROKKA disagree about" — cannot, because
the only machine-readable form of a built genome is a JSON object in an object store, addressed by
a hash. The job type that was supposed to make it queryable, `KBDLStoreLoad`, has never run. The
one existing lakehouse namespace in this domain, `kbaseincubator.fitness`, holds 48 genomes loaded
from somewhere else entirely, and would corrupt or lose six distinct kinds of information in a
KBDL genome if asked to hold one today.

The specific gap that forces the issue now: `kbdl-add-non-coding-to-existing-genome-v1` is adding
tRNA, rRNA, tmRNA, ncRNA, misc_RNA and CRISPR features to existing genome objects. Those features
have **no destination**. `fitness.gene` has no column that can say a gene is a tRNA, so an RNA gene
loaded into it today becomes indistinguishable from a protein-coding gene — which is precisely the
confusion KBDL's own schema goes to some length to make structurally impossible.

## Solution

A new Iceberg namespace, `kbaseincubator.genome_companion`, built on the `kbaseincubator.fitness`
scheme — the same singular domain names, the same assembly-keyed shape, the same separation of
`protein_seq_hash` from `dna_seq_hash` — with the fourteen defects of that scheme fixed before any
row is written, and with two tables dropped from it rather than carried forward.

It is a **companion** namespace, not a replacement: it holds what KBDL knows about a genome, it is
loaded by one loader from one source, and it joins outward to
`kbaseincubator.clearinghouse` for entity identity and to the KBDL object store for bytes. It
stores no sequences of its own.

Eighteen tables in six layers:

**Identity and structure** — `assembly`, `contig`, `genome`, `genome_alias`.
**Builds** — `genome_build` (the annotation-run entity; see Q4).
**Features** — `gene`, `gene_qualifier`.
**Provenance** — `genome_build_provenance`, `genome_caller_run`.
**Annotation** — `function`, `function_annotation`, `ontology_term`, `ontology_annotation`,
`transyt_annotation`.
**Fitness** — `condition`, `essentiality`, `experimental_fitness`, `propagated_fitness`.

Plus two operational tables that are not science: `genome_disposition` (curation — existence in the
archive is not "is a result") and `load_ledger` (a delivery is complete only when every table
reconciles).

The loader is an in-pod process reading a delivered KBDL snapshot, following
`council-record-lakehouse-v1`'s pattern, which is the one existing instance of this shape on this
platform. `KBDLStoreLoad` is retired unbuilt.

## User Stories

1. As a comparative genomicist, I want to ask how many genomes in the corpus carry zero tRNA genes,
   so that I can separate genuinely tRNA-poor lineages from assembly artifacts.
2. As a comparative genomicist, I want to distinguish "the tRNA caller ran on this genome and found
   nothing" from "no tRNA caller has ever run on this genome", so that a zero count is interpretable.
3. As a comparative genomicist, I want to retrieve every non-coding feature of a genome with its
   type, product, anticodon, amino acid, ncRNA class and Rfam accession, so that I can compare RNA
   complements across taxa without re-running a caller.
4. As a comparative genomicist, I want CRISPR arrays (`repeat_region`) to be retrievable and to be
   excluded by default from anything that means "gene", so that a CRISPR array never enters a gene
   count.
5. As a metabolic modeller, I want to select only protein-coding genes for a genome in one
   predicate, so that no RNA gene can reach model reconstruction.
6. As a metabolic modeller, I want each gene's protein sequence to be reachable by hash from the
   clearinghouse, so that I do not depend on a second copy of the sequence being in sync.
7. As an annotation methodologist, I want every functional annotation to carry the tool and the
   tool version that produced it, so that I can compare BAKTA 1.9 against BAKTA 1.10 on the same
   genome.
8. As an annotation methodologist, I want annotations from different tools about the same gene to
   coexist as separate rows rather than as columns, so that adding a seventh tool is a load, not a
   schema migration.
9. As an annotation methodologist, I want the parameter set a tool ran under to be identified by
   hash and resolvable to its text, so that two runs of one tool version at different thresholds
   are distinguishable.
10. As a data engineer, I want a query for one genome's genes to read a bounded number of files,
    so that per-genome access does not degrade as the corpus grows to two million genomes.
11. As a data engineer, I want a query for all non-coding features to prune away the
    protein-coding majority, so that the live driving case is cheap.
12. As a data engineer, I want every row to name the store, snapshot and batch it came from, so
    that I can attribute a wrong row to a load.
13. As a data engineer, I want a load to be declared complete only when every table reconciles,
    so that a half-delivered corpus is never published.
14. As a data steward, I want a genome's several identifiers — its KBDL G-number, its current
    genome object id, its FASTA object id, its assembly accession, its arc subject — kept together
    and append-only, so that an identifier changing under enrichment does not orphan the rows that
    used the old one.
15. As a data steward, I want to record that two genomes share one assembly without either of them
    losing its own identity, so that a knockout mutant and its wild-type parent are both
    representable.
16. As a data steward, I want "this genome is in the archive" to be separable from "this genome is
    a result", so that trial and superseded loads do not silently become findings.
17. As a data steward, I want to know which hash rule the assembly column follows by reading the
    column name, so that I cannot join an order-dependent hash to an order-independent one.
18. As an operator, I want the loader to be re-runnable against the same snapshot without
    duplicating rows, so that a failed load is recoverable by retry.
19. As an operator, I want a genome object that has been superseded to resolve to its current form
    at load time, with both ids retained, so that a reload is not pinned to a stale object.
20. As a KBDL maintainer, I want the GAA-dependent `store_load` path retired rather than extended,
    so that GAA's retirement does not block the lake.
21. As a `fitness` reader, I want a stated window and a compatibility story before anything of mine
    is retired, so that my queries do not break without warning.
22. As a reviewer of this design, I want each of the fourteen shortcomings to carry a decision and
    a blast radius, so that I can reverse any one of them by editing one line.

## Implementation Decisions

### The namespace and its boundary

`kbaseincubator.genome_companion`. Created with
`create_namespace_if_not_exists` (bare name, per the operator runbook's dotted/bare rule);
all `table_exists`/`load` calls use the dotted form.

It joins outward and stores nothing that another store owns:

| It needs | It stores | Owner of the real thing |
|---|---|---|
| assembly bytes | `assembly.fasta_reference` (a pointer) | KBDL object store |
| protein sequence | `gene.protein_seq_hash` | `clearinghouse.protein_content` |
| gene DNA sequence | `gene.dna_seq_hash` | `clearinghouse.gene_content` |
| entity identity | `assembly_entity_hash` | `clearinghouse.genome_entity` |
| parameter-set text | `parameter_set_hash` | `clearinghouse.parameter_set` |

**The `stores_content=FALSE` reconciliation, stated because nothing anywhere states it.**
KBDL's local clearinghouse policy sets `stores_content = FALSE` for `genome` on the grounds that
assemblies are megabytes. The lake nonetheless has a `genome_content` table, and the two are
compatible: lake `genome_content` holds *metadata plus a `fasta_reference` pointer*
(`entity_hash, fasta_reference, taxon_id, assembly_accession, n_contigs, total_length, is_closed,
standardizer_version, observed_at, ingest_batch_id`) and never the bytes. `genome_companion.assembly`
follows the same rule for the same reason. A reader who notices the apparent contradiction should
be able to resolve it from this paragraph.

### The provenance tail, on every table

Every table in the namespace carries, as its last six columns, in this order:

```
store_id STRING, snapshot_id STRING, snapshot_seq BIGINT,
load_batch_id STRING, observed_at TIMESTAMP, loaded_at TIMESTAMP
```

inherited verbatim in meaning from `council-record-lakehouse-v1`:
`store_id` is a **declared, durable** id for the originating store and is **never derived from a
filesystem path**; `snapshot_id` is the verified uncompressed digest of the snapshot the row came
from; `snapshot_seq` is an explicit monotonic sequence for that store. `observed_at` is when the
*fact* was true at the source; `loaded_at` is when this row was written. The two are separate
because a reload of an old snapshot must not make old facts look new.

The KBDL store on poplar is assigned `store_id = 'poplar/kbdl-1'`, by the same rule that assigned
`'poplar/council-1'`.

### Table: `assembly`

```
assembly_entity_hash STRING,   -- rule A: standardizers.entity_hash("genome", contigs)
assembly_content_hash STRING,  -- rule B: KBDL content_hash = sha256(canonical FASTA bytes)
fasta_reference STRING,        -- KBDL object id of the current FASTA object
fasta_reference_observed STRING, -- the id as it appeared in the snapshot, pre-supersession-resolve
size BIGINT, n_contigs BIGINT, longest_contig BIGINT, total_length BIGINT, is_closed BOOLEAN,
<provenance tail>
```

**There is no column named `assembly_hash`, deliberately.** Rule A strips headers, removes
whitespace, uppercases, **sorts** the contigs, joins on `|` and takes sha256 — order-independent,
and it is the clearinghouse's canonical genome identity. Rule B is sha256 over the canonical FASTA
bytes, header-inclusive and **order-dependent**, and is asserted equal to `fasta_object_id`.
Two submissions of the same contigs in a different order produce the **same A and a different B**.
Carrying both under one name is how a reader joins an order-dependent hash to an order-independent
one and gets a silently wrong answer; carrying them under names that state the rule makes that
mistake impossible to make by accident.

Primary key is `assembly_entity_hash`. `assembly_content_hash` is not unique within the table:
one entity hash may front several content hashes (the same contigs submitted in different orders).

### Table: `contig`

```
assembly_entity_hash STRING, contig_id STRING, source_id STRING, length BIGINT,
<provenance tail>
```

New; `fitness` has none, which is why `fitness.gene.contig_id` dangles against nothing.
`contig_id` is `CanonicalContig.id`, `source_id` is `CanonicalContig.source_id` (always known,
unlike a feature's). Sequence is not stored — see Q4.

### Table: `genome`

```
genome_id STRING,                    -- KBDL G-number; THE primary key
assembly_entity_hash STRING,         -- FK to assembly; MANY-to-one, not one-to-one
prefix STRING,
orgid STRING, source STRING,
genus STRING, species STRING, strain STRING,
ncbi_taxid STRING, lineage STRING,
taxonomy_ani DOUBLE, taxonomy_reference STRING, taxonomy_reason STRING, taxonomy_source_database STRING,
<provenance tail>
```

The taxonomy columns are `TaxonomyBlock`'s eight fields flattened, which is the whole block:
a taxonomy call that cannot say *why* it was made (`reason`, `ani`, `reference`) is not reusable.

**`genome_id` is the key and `assembly_entity_hash` is not,** because the live corpus already
contains a counterexample: `SyringaeB728a` and `SyringaeB728a_mexBdelta` have a byte-identical
assembly and are a wild-type and its knockout mutant. See Q1.

### Table: `genome_alias`

```
genome_id STRING, alias_source STRING, alias_id STRING,
<provenance tail>
```

Append-only, multi-valued, **no uniqueness constraint**, fill-only. `alias_source` is drawn from a
fixed vocabulary validated at the write boundary; an unknown value is rejected, not coerced:

| `alias_source` | what it holds | stable? |
|---|---|---|
| `kbdl_genome_id` | the G-number (redundant with the key, carried so a reader can round-trip) | yes |
| `kbdl_object_id` | the current genome JSON object id | **no — moves on enrichment** |
| `kbdl_fasta_object_id` | the FASTA object id | no — moves on recompression |
| `assembly_accession` | e.g. `GCA_000007465.2` | yes |
| `arc_subject` | the `obj://` subject in the arc store | yes |
| `ncbi_taxid` | NCBI taxonomy id | yes |

The vocabulary is fixed **at the start** rather than grown, for the same reason
`council-record-lakehouse-v1` fixed its own: after the first load, append-only plus fill-only plus
no-production-delete freezes the spelling, and a second spelling of one source is indistinguishable
from a second source.

**Why this is a companion table and not a dependency on `clearinghouse.entity_alias`:** the
companion namespace must be loadable before `entity_alias` exists (that is a separate PRD, not yet
written), and the two have different retention and partitioning. If `entity_alias` ships generic
over `entity_type`, `genome_alias` becomes a projection of it rather than a second source of truth —
see Q8's "If you disagree".

### Table: `genome_build`

```
build_id STRING,          -- sha256 over (genome_id, gene_caller, caller versions, bakta_db,
                          --  bakta_image, bakta_driver_sha256, cds_overlap_drop_fraction)
genome_id STRING,
gene_caller STRING,       -- 'bakta' | 'inprocess' | NULL (annotated source)
built_at TIMESTAMP,
built_by_job_id STRING,
is_current BOOLEAN,       -- exactly one TRUE per genome_id; a MUTABLE pointer, see below
<provenance tail>
```

**This table exists because of the research scan, and it fixes a collision the first draft had.**
It is the `analysis` / `annotation_run` entity that Chado (`analysis` keyed on
`(program, programversion, sourcename)`, with `analysisfeature unique (feature_id, analysis_id)`)
and Ensembl (`analysis`, plus a parallel `otherfeatures` database on the same core schema) have both
carried for twenty years, and that INSDC expresses as two accessions for one assembly
(GCA = submitter annotation, GCF = NCBI PGAP re-annotation). See Q4.

`is_current` is the one MUTABLE column in the namespace and it is deliberate: every other table is
append-only, and "which build should a reader use by default" is a question append-only data cannot
answer. It is a pointer, not a fact, and it carries the usual consequence — flipping it changes what
an unqualified query returns without changing any row of `gene`.

### Table: `gene`

```
gene_id STRING,          -- KBDL feature id verbatim: {genome_id}_{N} or {genome_id}_nc{N}
build_id STRING,         -- WHICH BUILD called this gene set; see Q4
genome_id STRING,
locus_id STRING,         -- CanonicalFeature.source_id / CanonicalNonCodingFeature.source_id; NULL for ab initio
contig_id STRING,
start BIGINT, "end" BIGINT, strand STRING,
feature_type STRING,     -- 'CDS' | 'tRNA' | 'rRNA' | 'tmRNA' | 'ncRNA' | 'misc_RNA' | 'repeat_region'
protein_seq_hash STRING, -- NULL for every non-CDS feature_type
dna_seq_hash STRING,     -- always set
caller STRING,           -- 'source', or the package name
caller_version STRING,   -- the version, split out of KBDL's "pyaragorn 0.3.0"
product STRING, partial BOOLEAN,
amino_acid STRING, anticodon STRING, ncrna_class STRING, rfam STRING, rpt_family STRING,
<provenance tail>
```

**`feature_type` carries SEVEN values, not six.** `NonCodingType` has six members —
`tRNA`, `rRNA`, `tmRNA`, `ncRNA`, `misc_RNA`, `repeat_region` — and `CDS` is the seventh.
`repeat_region` is a CRISPR array and **is not an RNA gene**; the finer SO/GenBank subtypes
(`snoRNA`, `SRP_RNA`, `RNase_P_RNA`, ...) are deliberately **not** members and fold to `ncRNA` with
the raw subtype in `ncrna_class`, exactly as `map_non_coding_type` does upstream. Keeping the
vocabulary closed is what keeps it stable in a store that cannot delete.

The invariant KBDL enforces structurally — a tRNA must never pass as a protein-coding gene — is
enforced here by a write-boundary validator: `protein_seq_hash IS NOT NULL` **iff**
`feature_type = 'CDS'`. The schema cannot express it, so the loader must, and a violation fails the
load rather than writing the row.

**The five qualifier columns are nullable and sparse, and that is cheap.** In Parquet with ZSTD an
all-null column costs a run-length-encoded null bitmap and nothing else, so the ~95% of rows that
are CDS pay essentially nothing for five columns they do not use. The alternative — a join table
for five fields — would cost a join on the largest table in the namespace to retrieve the fields
that define a tRNA.

`rpt_family` is the sixth qualifier, carrying `"CRISPR"` for a `repeat_region`, and is present
because the upstream model carries it as an `extra` key with exactly the role `ncrna_class` has for
an ncRNA.

### Table: `gene_qualifier`

```
build_id STRING, gene_id STRING, qualifier STRING, value STRING,
<provenance tail>
```

The narrow sidecar for `CanonicalNonCodingFeature`'s and `CanonicalFeature`'s
`model_config = ConfigDict(extra="allow")` passthrough. An annotated GenBank source can carry
qualifiers nobody enumerated; the named columns above cover the ones the model names, and this
table is what keeps the rest from being dropped on the floor. Expected to be empty or near-empty
for ab initio genomes and non-empty for carried-through annotated sources.

It is **not** an alternative to the named columns and must not grow into one: a qualifier that
appears on more than a few per cent of rows is a candidate for promotion to a named column in a
later revision, and the loader emits a count per distinct `qualifier` into the load ledger so that
promotion decision has evidence.

### Table: `genome_build_provenance`

One row per BUILD -- not per genome, see Q4 -- carrying `NonCodingProvenance`'s nine fields
plus the gene-caller identity:

```
build_id STRING,         -- THE PRIMARY KEY
genome_id STRING,
non_coding_source STRING,            -- 'called' | 'source' | 'none'
gene_caller STRING,                  -- 'bakta' | 'inprocess' | NULL (annotated source)
cds_dropped_overlapping_rna BIGINT,
cds_overlap_drop_fraction DOUBLE,    -- NULL means "not recorded" = built under the original any-overlap rule
cds_discarded_json STRING,           -- the caller's own discard tallies, canonical JSON
bakta_db STRING, bakta_image STRING, bakta_driver_sha256 STRING,
<provenance tail>
```

`cds_discarded` is a `dict[str, int]` whose keys are the *caller's* vocabulary, not ours, so it is
carried as canonical JSON rather than promoted to columns we would have to extend every time Bakta
adds a discard reason. `bakta_db` is a string (`"6.0-full"`), not an object reference, because what
reproduces a call is which database *content* was used.

### Table: `genome_caller_run`

```
build_id STRING, genome_id STRING, caller_package STRING, caller_version STRING,
features_called BIGINT,
<provenance tail>
```

**One row per caller that RAN, including callers that ran and found nothing** (`features_called = 0`).
This table exists for exactly one fact, and it is the fact the brief singled out: "zero tRNAs" and
"the tRNA caller never ran" are different things about a genome, and without the zero-hit row they
are indistinguishable. It is `NonCodingProvenance.callers` — a `dict[package, version]` that
deliberately includes zero-hit runs — unrolled into rows so it is queryable.

Absence of a row means the caller did not run. `features_called = 0` means it ran and found nothing.
A reader must never infer the first from a missing count.

### Tables: `function`, `function_annotation`, `ontology_term`, `ontology_annotation`, `transyt_annotation`

Carried from `fitness` **in the narrow shape it already uses**, with three changes:
`evidence_id` is dropped (Q9), `source_version` is split into `tool_version` and actually populated,
and `parameter_set_hash` is added.

```
function(function_id, description, <tail>)
function_annotation(gene_id, function_id, tool, tool_version, parameter_set_hash, score, <tail>)
ontology_term(ontology_id, namespace, term, label, definition, lineage, <tail>)
ontology_annotation(gene_id, ontology_id, tool, tool_version, parameter_set_hash, score, <tail>)
transyt_annotation(gene_id, reaction_equation, tc_number, substrate, tool_version, parameter_set_hash, <tail>)
```

`tool` is drawn from the measured live vocabulary plus KBDL's wired set:
`BAKTA`, `PROKKA`, `DRAM2`, `RAST`, `KOFAMSCAN`, `TRANSYT`. Adding a seventh is a load, not a
migration — which is the entire argument against the wide `annotation` table.

`parameter_set_hash` resolves against `clearinghouse.parameter_set`, which already keeps the
pre-image (`canonical_json`) so the hash rule stays recomputable. A default run is `{}`, whose hash
is `DEFAULT_PARAMETER_SET_HASH`. We do not mint a second registry.

`score` replaces the `evidence` indirection: it is nullable, it is the thing `evidence_score` was
supposed to hold, and holding it on the annotation row removes a 2.5M-row join that carries no
other information.

### Tables: `condition`, `essentiality`, `experimental_fitness`, `propagated_fitness`

Carried from `fitness` unchanged in shape, plus the provenance tail. They are the part of the
scheme that works: `experimental_fitness` already carries a `provenance` column and a `source_ref`,
`essentiality` already carries `call_source`, and the live data shows a single honest value
(`user_upload:essential_genes.csv`). Nothing in the fourteen-item agenda touches them.

### Tables: `genome_disposition` and `load_ledger`

```
genome_disposition(genome_id, disposition, reason, declared_by, declared_at, <tail>)
   disposition IN ('published','pilot','trial','superseded','excluded')
load_ledger(load_batch_id, store_id, snapshot_id, snapshot_seq, table_name,
            rows_expected BIGINT, rows_written BIGINT, reconciled BOOLEAN,
            started_at, finished_at, <tail>)
```

**Nothing downstream may treat "exists in `genome_companion.*`" as "is a result."**
This is `council-record-lakehouse-v1`'s lesson, and KBDL has its own instance of the same
distinction already: `object_supersedes` holds 28,051 rows all reasoned `atb-recompression-v1`,
which is exactly "present in the archive, superseded as a result". A genome with no disposition row
is **not** publishable; absence is not an implicit `published`.

`load_ledger` makes a delivery complete only when **every** table reconciles. A partially-written
batch is not publishable, and `reconciled = FALSE` on any row of a `load_batch_id` makes the whole
batch unpublishable.

### Partitioning and physical layout

| table | `partition_by` | why |
|---|---|---|
| `gene` | `feature_type` | 7 values; prunes the ~95% CDS majority away from the live driving case |
| `function_annotation` | `tool` | 4–6 values; the direct analogue of the clearinghouse's `source` |
| `ontology_annotation` | `tool` | as above |
| `genome_caller_run` | `caller_package` | low-cardinality, and "which genomes ran caller X" is a real query |
| everything else | *(none)* | below the threshold where partitioning pays, or no low-cardinality column exists |

**No table is partitioned on a hash.** The clearinghouse already settled this with a reason that
transfers verbatim: a good hash scatters uniformly by design, so partitioning on it buys zero read
pruning while multiplying partition count. `genome_id` is a truncated sha256 and falls under that
rule.

**File size and per-genome locality are controlled by the WRITER, not by the partition spec,**
because they cannot be controlled by the partition spec: `table_configs()` emits only `name`,
`schema_sql` and `partition_by`, so `write.target-file-size-bytes`, Iceberg sort order,
`write.distribution-mode` and Bloom filters are not expressible through this write path at all.
The loader therefore reuses `clearinghouse_shard.py`'s range-cutting algorithm unchanged — estimate
bytes, cut the key space into `N = ceil(total/target)` equal-width ranges with **computed**
boundaries, stream once into a bounded sort buffer, emit each range by external k-way merge — with
`genome_id` as the sort key rather than `entity_hash`, targeting ~512 MB per file.

Sorting physically by `genome_id` is what makes "all features for one genome" read a bounded number
of files: min/max statistics on a sorted key prune, on an unsorted one they do not. This is the
whole remediation for the measured pathology (2,880 files for 1.1M rows; 48 files for 48 rows), and
it is a property of how we write, which is the only lever we hold.

### The loader contract

An **in-pod loader**, following `council-record-lakehouse-v1`'s two-in-pod-loader pattern rather
than a KBDL push, for three reasons that are properties of this deployment:
the lakehouse write path (`BerdlCapability.load`) runs in-pod;
KBDL on poplar reaches the lake over a KBase token that **expires on roughly a 27-day cycle** and
has already lapsed silently once;
and the delivery rail (a snapshot plus a manifest in a Dropbox folder, which the pod can see)
already exists and is specified.

The contract, inherited with its corrections:

1. **A manifest's presence is not proof of arrival.** The trigger is "the manifest exists **and**
   every file it lists is present and sha256-matches". A sync client has already reported a
   directory up to date while a file was absent on the consumer.
2. **A first mismatch means "still syncing"** — retry, with progress persisted.
3. **After the manifest's own `deadline_at` it is a failed delivery,** raised with evidence:
   which file, expected vs observed size, expected vs observed digest.
4. **The producer snapshots through the sqlite3 backup API, never a file copy** — a `cp` of a live
   WAL SQLite yields a torn backup that opens fine.
5. **Idempotence by `(store_id, snapshot_id, table_name)`.** A re-run against the same snapshot is
   a no-op, not a duplicate. This replaces `_lookup_existing_genome_id`'s
   `skipped_already_ingested` behaviour, under which a reload is currently a silent no-op even when
   the genome has changed — which is the bug that would otherwise force "enrichment must always
   precede first load".
6. **Supersession is resolved at load time and both ids are kept** (Q10).
7. **The zero-join guard runs BEFORE the join.** Assert the input is non-empty and every join key
   non-null, because a relationship test evaluated over an empty join passes vacuously — which is
   how this class of defect survives having tests.

`KBDLStoreLoad` is retired unbuilt. It has run zero times (confirmed by the absence of any row for
it in `jobs`), and it is the one module in KBDL that imports `genome_annotation_aggregator` —
`Config`, `ConstrainedStoreV2`, `kbdl_loader.GeneIdMap`, `v2_ingest`, `staging_writer`,
`store_engine`. `kbdl_service/clearinghouse/` is GAA-free by construction and enforced by
`tests/test_layering.py`. Nothing is lost by designing fresh, and GAA's retirement stops blocking.

### What is dropped from the `fitness` scheme

| dropped | measured reason |
|---|---|
| `annotation` | 272,076 rows, **272,076 of them orphans** against `gene`; `organism:locus` ids from a different, older load covering 60 organisms against `gene`'s 48 |
| `evidence` | 2,504,043 rows; **0 non-null scores, 0 non-null probabilities**; `evidence_type` duplicates the parent's `tool`; only 2,388,346 distinct ids, so not even a key |
| `sequence` | 0 rows in `fitness` despite 228,709 genes; sequences belong to `clearinghouse.{protein,gene}_content` (Q4) |
| `gene_alias` | 0 rows, and `(gene_id, alias, type)` has no `observed_at`/`ingest_batch_id`; superseded by the same pattern `genome_alias` uses, added when a gene-level need is demonstrated |
| `gene_function` | 0 rows; a consensus table whose rule nobody has stated. Out of scope — see Out of Scope |
| `meta` | 0 rows; replaced by `load_ledger`, which records the same class of thing with a schema |

## Testing Decisions

**Test external behaviour, not implementation.** The externally observable behaviours here are:
what is in the tables after a load, what the loader refuses, and what a query costs.

**The criterion no stub can satisfy.** Every table-shape test in this plan could be made to pass by
a fixture that fakes the lakehouse. The guard against that is a **real-dependency smoke task in the
first phase** that creates one real table in a scratch namespace against the live BERDL endpoint,
writes one row, reads it back, and asserts the round-tripped types — in particular that
`TIMESTAMP` survives, which `clearinghouse_shard.py` records as a live trap, and that a
`PARTITIONED BY` clause actually lands. Every later phase depends on it. The measured precedent is
`kbdl-horizyn-v1`, where eight tasks passed on injected fixtures and both real defects surfaced only
in the first phase that touched the real package.

**Modules tested, and what each test must actually catch:**

- **The write-boundary validators.** `protein_seq_hash IS NOT NULL iff feature_type = 'CDS'` —
  tested by attempting to load a tRNA carrying a protein hash and asserting the load *fails*.
  A test that only loads valid rows proves nothing. Likewise an `alias_source` outside the fixed
  vocabulary, and a `feature_type` outside the seven.
- **The zero-hit caller round trip.** Build a genome with a caller that returns nothing, load it,
  and assert `genome_caller_run` has a row with `features_called = 0` — and separately, that a
  genome where the caller never ran has **no row**. The two assertions must both be present;
  testing only the first cannot distinguish the two states, which is the entire point of the table.
- **The zero-join guard.** A test that runs the relationship assertions over a deliberately **empty**
  input and asserts they *fail*. A relationship test over an empty join passes vacuously; this is
  the test that catches that, and without it the suite is self-confirming.
- **Idempotence.** Load the same snapshot twice; assert row counts are identical and
  `load_ledger` records one reconciled batch, not two.
- **Supersession resolution.** A fixture where the snapshot's `genome_object_id` appears as
  `old_object_id` in `object_supersedes`; assert the loaded row carries the *new* id in
  `fasta_reference` and the *observed* id in `fasta_reference_observed`. Note that this case has
  **zero live instances** today (measured: 0 of 4,865), so the fixture is the only coverage and
  must be built deliberately.
- **Overlapping features are legal.** Load a genome with a CDS overlapping an RNA gene; assert both
  rows land and neither is rejected. `v2_genes` effectively never had this case.
- **The two-genomes-one-assembly case.** Load the `SyringaeB728a` / `SyringaeB728a_mexBdelta` pair;
  assert two `genome` rows, one `assembly` row, and that neither genome is lost or merged. This is
  a real pair in live `fitness` and should be used rather than a synthetic one.

**Prior art in the codebase:** `tests/test_layering.py` in KBDL (the invariant-as-test pattern that
keeps `clearinghouse/` GAA-free) is the model for the validator tests. KBUtilLib's existing
clearinghouse bootstrap tests are the model for the DDL and partition-spec assertions.

**Regression framing.** Both repos carry pre-existing test failures. Every success criterion is
phrased as "no test that passed on the base commit fails on the branch, and the tests this task
adds pass", never "the suite is green".

## Open Questions and Judgement Calls

**Every item below is already decided and reflected in the PRD body and taskplan. The PRD is
buildable as it stands. To change a decision, edit its `DECIDED:` line** — Review Mode will
propagate the consequences through the body, the user stories, the testing decisions and the
taskplan.

Ordered by blast radius descending; within a radius, lowest confidence first.

### Q1. If two genomes can share one assembly, what is a genome's primary key? -- DECIDED: a surrogate `genome_id` (KBDL's G-number). `assembly_entity_hash` is a many-to-one foreign key and is never a key.
**Blast radius:** IRREVERSIBLE
**Why:** The live corpus already contains the counterexample. `kbaseincubator.fitness.genome` holds
48 rows with 47 distinct `assembly_hash` and zero nulls, and the colliding pair is `SyringaeB728a`
and `SyringaeB728a_mexBdelta` — a wild-type and its knockout mutant, byte-identical assemblies,
biologically distinct genomes. Chris's decision (1), that the genome clearinghouse hash is
assembly-scoped, is untouched by this: the *hash* is assembly-scoped, and the *genome row* needs its
own identity because an assembly does not determine a genome. Making the hash the key would have
silently merged a mutant into its parent on the 48th row of the smallest table in the namespace.
**If you disagree:** making `assembly_entity_hash` the key collapses `genome` to 47 rows on the
current corpus and loses `SyringaeB728a_mexBdelta` entirely; `gene.genome_id`,
`genome_alias.genome_id`, `genome_build_provenance.genome_id`, `genome_caller_run.genome_id`,
`genome_disposition.genome_id` and every fitness-layer table's `gene_id` prefix all re-key, and the
mutant/WT distinction must then be carried somewhere else — most plausibly as a `genome_alias`
row, which does not work because aliases are fill-only and non-unique.
**Confidence:** high — this is a measured counterexample in live data, not a hypothetical.

### Q2. Which hash rule does the assembly column follow? -- DECIDED: neither, because there is no single column. Two columns, `assembly_entity_hash` (rule A) and `assembly_content_hash` (rule B), and no column named `assembly_hash`.
**Blast radius:** IRREVERSIBLE
**Why:** Rule A (`standardizers.entity_hash`: headers stripped, whitespace removed, uppercased,
contigs **sorted**, `|`-joined, sha256) is order-independent and is the clearinghouse's canonical
genome identity. Rule B (KBDL `content_hash`: sha256 of canonical FASTA bytes) is header-inclusive
and **order-dependent**, and is asserted equal to `fasta_object_id`. The same contigs in a different
order give the same A and a different B. `fitness.assembly_hash` declares neither rule, and a column
that does not declare its rule is one a reader will join to the wrong thing. Naming the rule in the
column is the only fix that cannot be forgotten.

**Measured, so which rule the live column follows is not a hypothesis: it is rule A.**
Three `fitness` assemblies (1, 16 and 7 contigs) were fetched from KBase workspace 262939 via their
`fasta_handle_ref` and both rules recomputed over the real FASTA bytes. Rule A matched the stored
`assembly_hash` in all three and rule B in none (Research Report finding 7). Two of the three are
multi-contig, so rule A's sort step is load-bearing and still matches. That makes
`assembly_entity_hash` the *continuous* name for what is already there, lets the companion namespace
join straight to `clearinghouse.genome_entity` with no translation, and means the existing 47 rows
are **not** a data problem -- which is what the opposite outcome would have been.
**If you disagree:** collapsing to one column requires choosing which rule, which makes the other
unjoinable; keeping the name `assembly_hash` for rule A means every KBDL-side reader who knows
`content_hash` as "the assembly hash" joins wrongly and gets zero rows (the benign failure) or, when
order happens to match, the right rows for the wrong reason (the dangerous one). The `assembly`,
`contig`, `genome` tables and both loader hash paths change.
**The external scan disagrees with which rule should be PRIMARY, and the objection is a good one.**
GA4GH refget's seqcol spec explicitly CONSIDERED and REJECTED "digest each sequence, sort the
digests, concatenate, digest that" as its primary collection identifier -- in its own words, it
"would work perfectly fine for the simple use case of asserting two sets of sequences match in
content, [but] this is not the only use case", because for many aligners sequence ORDER and NAMES
matter, so "the final digest should differ if the underlying sequences had different order". Its
governing principle is that "the final digest representing a sequence collection can only answer ONE
such question." By that standard rule A answers "same content?" and cannot answer "same reference?",
and the scan recommends making the primary digest order-dependent and self-describing with a
separately-named order-independent digest beside it.
**The decision stands, and here is the honest reason it is not simply conceded.** This PRD carries
BOTH digests under rule-naming columns, which satisfies seqcol's real complaint -- that one digest
was being asked two questions. Which one is the JOIN KEY is a different matter, and rule A is already
the clearinghouse's `entity_hash` fleet-wide, is what `clearinghouse.genome_entity` is keyed on, is
what the in-flight `clearinghouse-entity-alias-v1` keys `entity_alias` on, and has now been measured
to be what live `fitness.assembly_hash` already holds. Making rule B primary would re-key the
clearinghouse, not just this namespace, and that is out of this PRD's scope. The scan's point is
recorded here so that a future seqcol adoption (Out of Scope, and `genome-identity-v1` Q13) starts
from it rather than rediscovering it. Also noted from the scan and worth acting on independently:
"the normalization rule is half the specification" -- rule A's case handling, whitespace stripping,
empty-contig dropping and header exclusion are all specified in `_standardize_genome`, which is good,
but they are specified in CODE and nowhere in prose that a lake reader would find.
**Confidence:** high, and raised during this session from high-on-the-rule to
high-on-the-rule-AND-on-the-live-data by the three-assembly recomputation above. Both rules are read
from source, the divergence condition (contig order) is explicit in each, and the live column has
been shown to follow rule A on a 1-contig, a 7-contig and a 16-contig assembly.

### Q3. How are the tables partitioned and sized at 10^9 rows? -- DECIDED: partition ONLY on low-cardinality stored columns (`feature_type`, `tool`, `caller_package`); partition on NO hash; control file size and per-genome locality in the WRITER by reusing `clearinghouse_shard.py`'s range-cutting at ~512 MB, sorted by `genome_id`.
**Blast radius:** IRREVERSIBLE
**Why:** Three measured constraints converge. (a) Our write path cannot express an Iceberg
transform or any table property — `table_configs()` emits only `name`, `schema_sql` and a bare
`partition_by` column, so `bucket(256, genome_id)` and `write.target-file-size-bytes` are simply
unavailable. (b) The clearinghouse already decided not to partition on a hash, because a uniform
hash buys zero pruning while multiplying partitions. (c) The actual defect in `fitness` is not the
partition spec but the writer: 48 rows in 48 files and 1.1M rows in 2,880 files are write-side
pathologies that no partition spec would fix. So the lever is the one `clearinghouse_shard.py`
already built and documented.
**If you disagree:** the realistic alternative is to materialise a `genome_shard` column (the first
two hex characters of `genome_id`, 256 values) and partition the big tables on it — which *is*
expressible, since it is a bare stored column. That buys genuine pruning for per-genome queries at
the cost of every reader having to compute the shard from the genome id before querying, plus one
more column on `gene`, `function_annotation`, `ontology_annotation` and the three fitness tables,
plus a derivation rule that is then frozen forever. Changing this after load is a full table
rewrite — ~723 MB today for `propagated_fitness` alone, and multi-terabyte at ATB scale.
**Confidence:** medium, and the external scan moved it in both directions at once.
**It CONFIRMED the central claim and supplied the constant.** Independently of me, the scan's first
recommendation is to "fix write batching and compaction BEFORE you touch the partition spec -- the
observed layout is an artefact of commit granularity and writer task fan-out, not of missing
partitions", and that "adding partitions to the current writer MULTIPLIES the file count". And
~512 MB turns out not to be a foreign transfer at all: it is Iceberg's own documented default
`write.target-file-size-bytes` (536,870,912). Both of those raise confidence.
**It DISAGREED on the partition spec**, and the disagreement deserves reading rather than
dismissing: it recommends `partitioning = ARRAY['bucket(N, genome_id)']` on the per-gene tables and
`ARRAY['tool', 'bucket(N, genome_id)']` on the annotation tables, with N sized from BYTES rather than
genome count -- N=256 for `gene`, N=256 per-tool, N=4096 for `propagated_fitness`. That is better
advice about ICEBERG than this decision is. It is simply not available through OUR write path, which
can request only a bare stored column. So the decision stands as written, and the scan has named the
target to aim at if the write path is ever widened -- which is now a worthwhile piece of work with a
concrete payoff attached.
**The scan also supplied a trap I did not know, and it applies to the `genome_shard` alternative
above:** Trino's `iceberg.max-partitions-per-writer` defaults to **100** and will REFUSE a write to a
table exceeding it, so the 256-value shard column would need that setting raised FIRST or the first
load fails. It further recommends a Parquet bloom filter on `genome_id` and an explicit write-side
sort order; neither is expressible here, which is the same ceiling restated twice more.
What would raise this to high: file counts and per-genome query cost measured on this corpus after
the sharder lands, which is the acceptance rehearsal's job.

### Q4. Is `gene_id` a key, given that one assembly can be annotated more than once? -- DECIDED: NO. Gene identity is scoped by an annotation run: a `genome_build` entity, and `gene` keyed on `(build_id, gene_id)`.
**Blast radius:** IRREVERSIBLE
**Why:** **This decision is the external research scan's doing, and the first draft of this PRD had
the defect it names.** I had `gene` keyed on `gene_id` alone, reasoning that KBDL mints
`{genome_id}_{N}` uniquely within a genome. That reasoning has a hole: `genome_id` is derived from the
assembly content hash and does NOT change when the caller changes, and `genome_registry`'s
`_lookup_existing_genome_id` DEDUPES ON `content_hash` -- a HIT returns the EXISTING `genome_id`. So
rebuilding one assembly with `gene_caller='bakta'` after an `inprocess` build yields the SAME
`genome_id`, hence the SAME `{genome_id}_{N}` id space, with DIFFERENT coordinates. Two rows, one key,
silently.
And the disagreement is not hypothetical or small. Siqueira et al. 2025
(doi:10.1101/2025.05.27.656432) compared the GenBank and RefSeq annotations of a *single, identical*
*P. putida* KT2440 assembly: 159 genes only in one, 81 only in the other, and **897 genes -- ~16% of
total ORFs -- carrying IDENTICAL locus tags at DIFFERENT genomic positions** (455 differing in start
codon, 442 in stop, offsets from <=27 bp to a few hundred). Every schema that faces this keeps the
sets apart rather than merging them: INSDC gives one assembly two accessions (GCA submitter vs GCF
PGAP), Ensembl puts the alternative set in a parallel `otherfeatures` database on the same schema,
and Chado makes the analysis part of feature identity (`analysisfeature unique (feature_id,
analysis_id)`, `analysis` keyed on `(program, programversion, sourcename)`). There is no prior art
anywhere for reconciling competing structural callers onto one feature row.
**If you disagree:** keying on `gene_id` alone is what the first draft did and it is survivable ONLY
while exactly one build per genome ever reaches the lake -- which is true TODAY (KBDL holds one
`genome_object_id` per genome) and stops being true the first time a genome is rebuilt under a
different `gene_caller`, which `kbdl-build-genome-bakta-calling-v1` exists to make routine. Dropping
`build_id` removes the `genome_build` table, un-keys `gene`, `gene_qualifier`,
`genome_build_provenance` and `genome_caller_run`, and removes the `is_current` pointer -- and makes
the first Bakta rebuild of an in-process genome a silent key collision rather than a second row.
Adding it back afterwards is a full rewrite of the four largest tables, because a `build_id` cannot
be inferred for rows already written.
**Confidence:** high on the need, **medium on the shape.** High because the collision is mechanical
and I can point at the dedup that causes it. Medium because `build_id`'s composition (a hash over
caller identity plus database and driver digests) is my construction, not something the scan or any
cited schema specifies -- Chado keys its `analysis` on `(program, programversion, sourcename)`, which
is coarser and would not distinguish two Bakta runs against different databases. What would raise it:
one real rebuild of an existing genome under a different caller, loaded end to end, with the
acceptance rehearsal asserting two builds and two gene sets.

### Q5. Does the companion namespace store sequences? -- DECIDED: no. `sequence` is not carried; `gene.protein_seq_hash` and `gene.dna_seq_hash` are foreign keys into `clearinghouse.protein_content` and `clearinghouse.gene_content`, and `assembly.fasta_reference` is a pointer into the KBDL object store.
**Blast radius:** IRREVERSIBLE
**Why:** `fitness.sequence` has **0 rows** against 228,709 genes, so the "typed sequence table" the
scheme appears to offer has never actually been exercised and nothing depends on it. Meanwhile the
clearinghouse exists precisely to be the non-redundant store of entities, already has
`gene_content` carrying `sequence`/`seq_length` and `protein_content`, and partitions them on
`standardizer_version` for this volume. A second copy of 10^9 sequences in the companion namespace
is both the largest cost in the design and the one most likely to drift out of sync with the store
that owns it.
**If you disagree:** carrying sequences adds a `sequence` table partitioned by `seq_type`, makes the
companion namespace the largest in the tenant by an order of magnitude, and requires a
reconciliation rule for the case where its copy and `clearinghouse.gene_content` disagree. It also
makes the namespace loadable without the clearinghouse, which is the one real argument for it.
**Confidence:** high on the storage decision; **medium on the join ergonomics** — a scientist who
wants genes and sequences now writes a cross-namespace join, and I did not measure what that costs
in Trino here.

### Q6. `feature_type` as a column or a separate table? -- DECIDED: one `feature_type` column on `gene`, with SEVEN values: `CDS` plus the six `NonCodingType` members.
**Blast radius:** MEDIUM
**Why:** The brief said five non-coding values; the source says six — `repeat_region` was added for
Bakta calling and is a CRISPR array, not an RNA gene. A column rather than a table because the
distinction is one closed enumerated attribute of a feature, because `gene` already carries separate
`protein_seq_hash` and `dna_seq_hash` so a non-coding gene fits with no structural change, and
because a separate table would require a join on the largest table in the namespace to answer "is
this a gene".
**If you disagree:** a `feature_type` lookup table adds a join to every feature query and buys
nothing, since the vocabulary is closed by design upstream. Widening the vocabulary instead (adding
the SO subtypes as first-class values) contradicts `map_non_coding_type`, which deliberately folds
them to `ncRNA` with the subtype in `ncrna_class` to keep the enum stable in a store that cannot
delete.
**Confidence:** high — the vocabulary is read from `NonCodingType` in source, including the sixth
member the brief omitted.

### Q7. Where do the RNA qualifiers live? -- DECIDED: six named nullable columns on `gene` (`product`, `partial`, `amino_acid`, `anticodon`, `ncrna_class`, `rfam`, `rpt_family`) PLUS a narrow `gene_qualifier(gene_id, qualifier, value)` sidecar for the `extra="allow"` passthrough.
**Blast radius:** MEDIUM
**Why:** The named qualifiers are what define a tRNA, so they must be retrievable without a join on
a 10^9-row table. Sparsity is nearly free: in Parquet with ZSTD an all-null column is a
run-length-encoded null bitmap, so the ~95% CDS majority pays almost nothing. But
`CanonicalNonCodingFeature` sets `model_config = ConfigDict(extra="allow")` specifically so an
annotated source's unanticipated qualifiers survive, and named columns alone would drop exactly
those. Both, therefore — and the loader counts distinct `qualifier` values into the load ledger so
that promoting a frequent one to a named column later is an evidenced decision rather than a guess.
**If you disagree:** pure EAV costs a join for every tRNA query and loses type information;
named-columns-only silently drops passthrough qualifiers, which is a data-loss bug that no test
would catch because the dropped keys are by definition the ones nobody enumerated.
**Confidence:** high on the shape; **medium on the Parquet null-cost claim**, which is a
well-established property of the format but which I did not measure on this deployment's writer.

### Q8. Where does caller provenance live? -- DECIDED: three places, because there are three different facts. Per-feature `caller`/`caller_version` columns on `gene`; one `genome_build_provenance` row per genome; one `genome_caller_run` row per caller per genome, INCLUDING zero-hit runs.
**Blast radius:** MEDIUM
**Why:** "Which tool produced this feature" is a property of the feature; "which gene caller and
which Bakta database built this genome" is a property of the genome; "which callers ran, and what
each found" is a set, and it is the one that cannot be reconstructed from the other two. The
zero-hit row is the whole reason the third table exists: `NonCodingProvenance.callers` deliberately
includes callers that ran and found nothing, because "zero tRNAs" and "the tRNA caller never ran"
are different facts and a later rebuild could not otherwise tell a genuinely tRNA-free assembly
from one built before tRNA calling existed. Note also that `fitness` *has* a `source_version` column
on both annotation tables and it is **NULL in 100% of rows** (0 of 1,118,422 and 0 of 1,010,656) —
so a column alone does not make provenance happen; the loader contract has to require it.
**If you disagree:** folding all three into a JSON blob on `genome` makes "which genomes ran
pyaragorn 0.3.0" unqueryable; dropping `genome_caller_run` loses the zero-hit fact permanently,
because it is not recoverable from the feature rows (there are none) or from the genome row.
**Confidence:** high — the three-way split mirrors the source model exactly.

### Q9. Does the genome alias table live here or in `clearinghouse.entity_alias`? -- DECIDED: here, as `genome_alias`, append-only, multi-valued, no uniqueness, with a fixed `alias_source` vocabulary validated at the write boundary.
**Blast radius:** MEDIUM
**Why:** This is Chris's decision (3) and it must be satisfiable before `entity_alias` exists —
that is a separate PRD, not yet written, and this namespace should not be blocked on it. The need
is concrete and measured: `genomes` in the KBDL registry carries four identifiers in one row
(`genome_id`, `content_hash`, `fasta_object_id`, `genome_object_id`), and `genome_object_id`
**moves under enrichment** while `content_hash` does not — so a single-slot pointer orphans every
row that used the old value. Append-only and fill-only because a superseded identifier is still the
right answer to "what was this called in the snapshot I am reading".
**If you disagree:** if `clearinghouse.entity_alias` ships generic over `entity_type` (which its
design intends), `genome_alias` should become a **projection** of it rather than a second source of
truth — a view, or a loader that reads from it. That changes the loader's input, the table's
writer, and makes this namespace depend on the clearinghouse being bootstrapped first. The
companion-side table and its vocabulary stay the same either way, which is why this is MEDIUM and
not IRREVERSIBLE. Note that the in-namespace precedent is weak: `fitness.gene_alias` is
`(gene_id, alias, type)` with **0 rows** and no `observed_at`/`ingest_batch_id`.
**Confidence:** medium — the decision is right for *this* PRD's independence, but the sibling
session writing `clearinghouse-entity-alias-v1` (session `44109e55`, in a worktree on KBUtilLib
`main` as of 22:26 CDT) may land a generic table that makes the projection the better answer. It has
been messaged about exactly this.

### Q10. Are `annotation` and `evidence` carried forward? -- DECIDED: neither. Both are dropped. `evidence`'s only non-redundant content, a score, becomes a nullable `score` column on the annotation rows.
**Blast radius:** MEDIUM
**Why:** Measured, not inferred. `annotation`: 272,076 rows, of which **272,076 are orphans** against
`gene` — its `feature_id` is `organism:locus` while `gene.gene_id` is `{uuid}.{locus}`, and it spans
60 organisms against `gene`'s 48 genomes. It is a dead table from an earlier, differently-keyed
load, and it is also the wide tool-shaped design (`rast`, `ko`, `bakta` as *columns*) that does not
generalise. `evidence`: 2,504,043 rows that reconcile exactly to the sum of the three annotation
tables (1,118,422 + 1,010,656 + 374,965), with **0 non-null scores and 0 non-null probabilities**,
an `evidence_type` that merely repeats the parent's `tool`, and only 2,388,346 distinct
`evidence_id`s — so it is a 1:1 satellite that is not a key and carries no information.
**If you disagree:** keeping `evidence` costs a 2.5-billion-row join at ATB scale to retrieve a
column we could have put on the parent row; keeping `annotation` means specifying the id-translation
from `organism:locus` to `{genome_id}_{N}` that nobody has written, for a table whose consensus rule
(`category`, `verdict`, `u_gene`, `n_hypotheses`) is undocumented. If the *consensus* function of
`annotation` is wanted, that is a new, properly-keyed table and belongs in its own PRD.
**Confidence:** high on `evidence` — the reconciliation is exact across all six tools.
**Medium on `annotation`**: I proved it does not join, not that nobody wants what it holds.

### Q11. How does the lake consume genome-object supersession? -- DECIDED: resolve transitively at load time, and store BOTH — the resolved current id in `fasta_reference`, the id as observed in the snapshot in `fasta_reference_observed`.
**Blast radius:** MEDIUM
**Why:** Storing only the observed id pins the lake to objects that may have been recompressed away;
storing only the resolved id destroys the ability to reproduce what the snapshot actually said,
which is the one thing a provenance-carrying row exists for. Both costs one column.
**If you disagree:** resolve-at-read moves the `object_supersedes` table into the query path, which
means the lake cannot answer without KBDL's SQLite, defeating the point of the namespace.
**Confidence:** medium, and specifically because **this path has zero live instances**: joined
against `genomes`, 0 of 4,865 rows match `object_supersedes.old_object_id` on either
`genome_object_id` or `fasta_object_id`, and there are 0 chains of depth > 1. All 28,051 supersedes
rows are `atb-recompression-v1` against archive members. So the transitive resolver is built on a
specification rather than on observed behaviour, and the first real supersession will be its first
test. What would raise confidence: one end-to-end enrichment that actually supersedes a registered
genome object.

### Q12. What does "every row carries ingest provenance" mean concretely? -- DECIDED: the six-column tail `store_id, snapshot_id, snapshot_seq, load_batch_id, observed_at, loaded_at` on every table, with `store_id` never derived from a filesystem path.
**Blast radius:** MEDIUM
**Why:** `fitness` carries only `created_at`, and its values are clustered at two instants, which
tells you a bulk load happened and nothing else — not which store, not which snapshot, not whether
a row is from a superseded load. `council-record-lakehouse-v1` paid for the triple already and for
the path rule specifically: a store that moves off `/scratch` must keep its id, or every row it has
published becomes unjoinable. `observed_at` and `loaded_at` are separate so that reloading an old
snapshot does not make old facts look new.
**If you disagree:** dropping to `ingest_batch_id` + `observed_at` (the clearinghouse's lighter
pair) saves four columns per row and loses the ability to attribute a row to a snapshot, which is
what makes a bad load retractable.
**Confidence:** high.

### Q13. What is retired, and what is not? -- DECIDED: this namespace supersedes only the genome / gene / annotation / fitness subset of `inner_loop`'s `v2_*` tables. The modelling tables are explicitly NOT retired and keep `inner_loop` as their home.
**Blast radius:** MEDIUM
**Why:** Measured: `inner_loop` holds **39** `v2_*` tables, and they are not a relabelled `fitness`.
They include `v2_models`, `v2_model_reactions`, `v2_model_fva`, `v2_model_gapfill_media`,
`v2_reactions`, `v2_reaction_fva`, `v2_pangenome_neighbors`, `v2_protein_families`,
`v2_protein_family_members`, `v2_mappings`, `v2_mapping_reaction_functions`,
`v2_annotation_combinations`, `v2_fitness_simulations` and more — an entire modelling layer for
which `fitness` has **no equivalent table at all**. "Retire `inner_loop`" would drop that layer.
**If you disagree:** declaring the whole of `inner_loop` retired requires first designing homes for
roughly twenty modelling tables, which is a second PRD of comparable size and is not what the
fourteen-item agenda asked for.
**Confidence:** high on the table inventory, which is a live read. **Low on the sequencing** — I did
not measure who reads `inner_loop` or `fitness` today, so the length of the compatibility window in
the retirement path is asserted, not evidenced. See the terminal entry.

### Q14. Who writes the tables? -- DECIDED: an in-pod loader reading a delivered KBDL snapshot, following `council-record-lakehouse-v1`'s pattern. `KBDLStoreLoad` is retired unbuilt.
**Blast radius:** MEDIUM
**Why:** The lakehouse write path runs in-pod; KBDL on poplar reaches the lake over a KBase token
that expires on a ~27-day cycle and has already lapsed silently once mid-build; and the delivery
rail (snapshot + manifest in a Dropbox folder the pod can see) is already specified and corrected.
`KBDLStoreLoad` has run **zero times** — confirmed by the absence of any `jobs` row for that type
against 4,891 `KBDLBuildGenome` and 3,341 `KBDLGenomeAnnotation` completions — so there is no
incumbent behaviour to preserve, and it is the single module that imports GAA.
**If you disagree:** a KBDL push makes the loader a job type with the job system's retry and
leasing for free, but puts the expiring credential on the critical path of every load and keeps
GAA in the dependency graph.
**Confidence:** high.

### Q15. How do KBDL feature ids map to lake gene ids? -- DECIDED: `gene.gene_id` is the KBDL feature id verbatim (`{genome_id}_{N}` for CDS, `{genome_id}_nc{N}` for non-coding); `gene.locus_id` is the upstream `source_id`, NULL for ab initio calls.
**Blast radius:** LOW
**Why:** Verbatim carriage means an id in the lake is pasteable into KBDL and vice versa, with no
translation table to go stale. The two independent counters are safe rather than dangerous: `_1`
and `_nc1` are distinct strings, so they cannot collide, and the 3,218 completed
`KBDLGenomeAnnotation` jobs that reference CDS ids can never accidentally match a non-coding id.
`locus_id` is nullable because a feature predicted ab initio from a bare FASTA has no upstream
identifier — `CanonicalFeature.source_id` is `str | None` for exactly that reason.
**If you disagree:** minting lake-local gene ids requires a persisted mapping table and makes every
cross-system question a join.
**Confidence:** high — the minting is read from `genome_normalize.py` at the two sites that do it.

### Q16. Is "exists in the namespace" the same as "is a result"? -- DECIDED: no. A `genome_disposition` table carries `published | pilot | trial | superseded | excluded`, and absence of a row means NOT publishable.
**Blast radius:** MEDIUM
**Why:** `council-record-lakehouse-v1` paid for this with twenty-one jobs of which two cancelled
19,276-item runs looked authoritative by label and size. KBDL has its own instance already:
`object_supersedes` holds 28,051 rows, all `atb-recompression-v1`, which is precisely "present in
the archive, superseded as a result". Absence-means-not-published rather than
absence-means-published because the failure directions are asymmetric: wrongly withholding a result
is visible and fixable, wrongly publishing a trial is neither.
**If you disagree:** dropping the table makes every downstream consumer re-derive curation from
`load_batch_id` heuristics, which is how a trial load becomes a finding.
**Confidence:** high.

### Q17. Is overlap between features legal? -- DECIDED: yes. No coordinate-uniqueness constraint on `gene`; the overlap tally persists in `genome_build_provenance.cds_dropped_overlapping_rna` with the threshold it was produced under.
**Blast radius:** LOW
**Why:** `kbdl-add-non-coding-to-existing-genome-v1` deliberately does *not* drop a CDS that
overlaps an added RNA, so that existing CDS ids stay valid — which means gene rows for one genome
**can** overlap in coordinates, and any uniqueness rule that forbade it would reject correct data.
`cds_overlap_drop_fraction` is carried alongside the count because a count produced under a
different threshold is not comparable, and `NULL` is meaningful: it marks a genome built under the
original any-overlap rule.
**If you disagree:** enforcing non-overlap rejects every enriched genome at load.
**Confidence:** high — measured in the live corpus as zero duplicate `(genome_id, locus_id)` pairs
today, which is consistent with both rules; the decision rests on the upstream PRD's stated
behaviour rather than on current data.

### Q18. What I could not decide and did not guess

Four external facts are unreconciled, and none of them blocks the build:

1. **Who reads `fitness` and `inner_loop` today.** I measured their contents, not their consumers.
   Q12's retirement path specifies a compatibility window whose *length* is therefore asserted, not
   evidenced. If no reader exists, the window is wasted work; if one exists that I have not named,
   it may be too short. Determining this needs lakehouse query-log access, which I do not have from
   h100.
2. **What wrote `fitness`.** Every file-count pathology in the Research Report is a property of that
   writer, and it is not in KBUtilLib or KBDL — the rows carry `source='fitnessDB'` and
   `created_at` clustered at two instants, which reads as a bulk load from outside our repos.
   Q3's remediation is specified against *our* write path, which I can read. **If the intent is to
   rewrite `fitness` in place rather than supersede it, that writer must be found first, and this
   PRD does not do that.**
3. **How many genomes are to be enriched.** This is blocked continuation 180 and Chris's to answer.
   It does not change any table shape, but it changes whether the loader's first run is 4,865 rows
   or 2,000,000, and therefore whether the sharding in Q3 is exercised at all on day one.
4. **What is in `kbaseincubator.genome_clearhouse`** (that spelling — a different namespace from
   `clearinghouse`, holding `genome_quality` and `skani_distances`). It is plainly genome-adjacent
   and I did not open it. If it holds per-genome quality metrics, those may belong in this namespace
   rather than beside it, and this PRD neither claims them nor rules them out.

## Gotchas and Unintuitive Consequences

**A query that asks for "genes" will get CRISPR arrays unless it says otherwise.**
`repeat_region` is a `feature_type` value on the `gene` table and it is not a gene. Every count,
every export, every model-reconstruction input must filter. The design puts it in `gene` anyway
because it shares every property that matters — a genomic interval, DNA, no protein, a caller to
record — but the consequence is that `SELECT COUNT(*) FROM gene WHERE genome_id = ?` is the wrong
query and will not look wrong.

**Partitioning `gene` by `feature_type` creates one enormous partition and six small ones.**
That is intended — it prunes the CDS majority away from non-coding queries — but it means the CDS
partition gets no benefit at all from the partition spec, and its file layout rests entirely on the
writer-side sharding in Q3. If the sharding is skipped or misconfigured, `gene` degrades to
`fitness`'s current pathology and the partition spec will not reveal it.

**`fasta_reference` resolving at load time means two loads of the same snapshot can disagree.**
Supersession is resolved against `object_supersedes` *as it stands when the loader runs*. Load the
same snapshot before and after a recompression campaign and the `fasta_reference` differs while
`fasta_reference_observed`, `snapshot_id` and every other column are identical. This is correct —
it is what resolve-at-load means — but it breaks the intuition that a snapshot determines its rows,
and the idempotence key `(store_id, snapshot_id, table_name)` will treat the second load as a
no-op, so **the newer resolution is silently not applied.** A re-resolve is a deliberate operation,
not a side effect of reloading.

**The zero-join guard is the only thing standing between this design and a self-confirming test
suite.** Every relationship assertion here — genes resolve to genomes, annotations resolve to genes,
aliases resolve to genomes — passes vacuously over an empty table. The namespace starts empty.
So on day one, before any load, the entire integrity suite is green and means nothing. The guard
must assert non-emptiness *first*, and the test for the guard must itself run over an empty input
and assert failure.

**Dropping `evidence` is not reversible by adding it back later.** The scores it would have held are
`NULL` in the source today, so a later `evidence` table would be reconstructed from annotation rows
that never carried a score — i.e. it would be re-created empty, which is what it effectively is now.
Nothing is lost, but nobody should expect that re-adding the table recovers anything.

**`genome_alias` being fill-only means a wrong alias is permanent.** There is no uniqueness and no
delete, deliberately, so an incorrect `kbdl_object_id` written by a buggy loader stays in the table
forever and every reader sees two candidate object ids with no rule for choosing. The mitigation is
the provenance tail — a reader can prefer the row with the highest `snapshot_seq` — but that is a
*convention*, not a constraint, and it must be documented at the read surface or it will not be
followed.

**The namespace is independently loadable but not independently useful.** Q4 puts sequences in the
clearinghouse, so a deployment that bootstraps `genome_companion` without the clearinghouse gets a
namespace where `protein_seq_hash` resolves to nothing. The tables load fine and every integrity
check passes. The failure is a scientist getting no sequence back, which looks like a data gap
rather than a missing dependency.

**`source_version` being 100% NULL in `fitness` is the warning, not the exception.** A column exists
for tool version in the live namespace and nothing has ever populated it. This design adds
`tool_version`, `caller_version` and `parameter_set_hash`, all of which can be `NULL` as easily.
Schema does not make provenance happen; the loader's write-boundary validators do, which is why
they are specified as rejections rather than as defaults.

**`genome_build.is_current` is the one mutable column, and flipping it silently changes query answers.**
Every other table here is append-only, so a reader can reason about `snapshot_seq` and get a stable
answer. `is_current` cannot work that way -- "which build should an unqualified query use" is not a
fact a snapshot can carry. The consequence: flipping it rewrites the meaning of every query that
does not name a `build_id`, while changing not one row of `gene`. Any dashboard or notebook that
reads `gene` without joining `genome_build` will change its numbers under a rebuild and give no sign
that anything happened.

**A rebuild makes the corpus grow without any genome being added.** Because gene identity is scoped
by `build_id` (Q4), re-annotating the existing 4,865 genomes under a second caller DOUBLES `gene`,
`gene_qualifier`, `genome_build_provenance` and `genome_caller_run` -- and leaves `genome`,
`assembly` and `contig` untouched. "Rows in `gene` divided by rows in `genome`" is therefore not a
mean gene count, and will drift upward over time for reasons that have nothing to do with biology.

**Two genomes sharing one assembly means per-assembly aggregates double-count.**
`SELECT COUNT(*) FROM genome JOIN assembly USING (assembly_entity_hash)` returns 48 on the current
corpus, not 47. Any "per assembly" statistic must aggregate on `assembly`, not on the join.

## Sources Consulted

**Live lakehouse reads (h100, 2026-10-25, 22:15–22:24 CDT, via `kbutillib` `KBBERDLUtils`):**
- `get_database_list()` — 123 namespaces.
- `get_database_tables("kbaseincubator.fitness")` — 19 tables (the brief's list omitted `gene_function`).
- `get_table_columns` + `SELECT COUNT(*)` for all 19 `fitness` tables.
- `SHOW CREATE TABLE` and `SELECT * FROM "<t>$partitions"` for `gene`, `genome`, `assembly`,
  `function_annotation`, `propagated_fitness` — the source of the no-partitioning and file-count findings.
- Eleven integrity/vocabulary queries: duplicate `assembly_hash`; genome→assembly orphans;
  `annotation`→`gene` orphans; `evidence` null-score and distinct-id counts; `source_version` null
  counts on both annotation tables; `tool` and `evidence_type` group-bys; `ontology_term.namespace`
  group-by; `essentiality.call_source` group-by; duplicate `(genome_id, locus_id)`.
- `get_database_tables("kbaseincubator.inner_loop")` — 39 `v2_*` tables.
- `get_database_tables("kbaseincubator.clearinghouse")` — 19 tables (15 per-entity-type +
  `parameter_set` + 3 `all_*` unions).

**Live KBDL store (`/scratch/kbdl/kbdl.sqlite`, 15 GB, read-only):**
- `genomes` schema and counts — 4,865 rows, 4,865 distinct `content_hash`, 0 null `genome_object_id`.
- `jobs` by `(job_type, state)` — `KBDLBuildGenome` 4,891 completed / 10 failed;
  `KBDLGenomeAnnotation` 3,341 completed / 33 failed; **no `KBDLStoreLoad` row of any state**.
- `object_supersedes` — 28,051 rows, all `reason='atb-recompression-v1'`; 0 join hits against
  `genomes.genome_object_id`; 0 against `genomes.fasta_object_id`; 0 chains of depth > 1.

**KBUtilLib source:**
- `domains/kbase/berdl/clearinghouse_schema.py` — module docstring (the partitioning rationale, the
  per-entity-type split, the `parameter_set` registry), `_ENTITY_PARTITION_BY`,
  `_CONTENT_PARTITION_BY`, `_content_columns`, `table_configs`.
- `domains/kbase/berdl/clearinghouse_bootstrap_adapter.py` — `_create_table_sql` (the
  `PARTITIONED BY (\`col\`)` rendering that bounds what a partition spec can be).
- `domains/kbase/berdl/clearinghouse_shard.py` — the range-cutting algorithm and the explicit
  statement that sort order and table properties are not expressible through this write path.
- `domains/kbase/berdl/capability.py` — `_normalize_partition_by`.
- `agent-io/docs/clearinghouse-schema-operator-runbook.md` — dotted vs bare namespace strings.
- `agent-io/prds/council-record-lakehouse-v1/fullprompt.md` — the `store_id`/`snapshot_id`/
  `snapshot_seq` triple and the path rule, the partition table, `job_disposition`, the delivery rail
  and its four corrections.

**KBDL source:**
- `schemas/build_genome.py` — `NonCodingType` (six members), `CanonicalNonCodingFeature`,
  `NonCodingProvenance` (nine fields), `CanonicalFeature`, `CanonicalContig`, `CanonicalGenome`,
  `TaxonomyBlock`, `CoordinateConvention`, `_NCRNA_SUBTYPES`.
- `genome_normalize.py` — gene id minting at the two sites (`{genome_id}_{N}`, `{genome_id}_nc{N}`).
- `genome_registry.py` — `content_hash` dedup semantics and the write-lock rationale.
- `clearinghouse/__init__.py` — the GAA-free layering invariant and its rationale.
- `job_types/store_load.py` — the GAA imports (`Config`, `ConstrainedStoreV2`,
  `kbdl_loader.GeneIdMap`, `v2_ingest`, `staging_writer`, `store_engine`).

**Read and found uninformative:**
- `KBBERDLUtils.table_configs` — **does not exist.** The brief instructed measuring `fitness`
  partitioning with it; `table_configs` is a `clearinghouse_schema` DDL emitter for *our* 16 tables,
  not a generic introspection call. Partitioning was measured with `SHOW CREATE TABLE` and
  `$partitions` instead.
- `DESCRIBE kbaseincubator.fitness.<t>` — returns an empty result set through this endpoint on
  every table tried; not a usable introspection path here.
- `kbdl_service/store_load.py` — does not exist at that path (the brief's spelling);
  the module is `kbdl_service/job_types/store_load.py`.

## Out of Scope

- **`clearinghouse.entity_alias`.** Chris's decision (3) generalised over all five entity types is a
  separate PRD, being written by session `44109e55`. This PRD carries a genome-specific
  `genome_alias` that can become a projection of it (Q8).
- **The four-element architecture reconciliation** — which layer owns identity vs bytes vs derived
  results vs run provenance, how the arc store attaches, and the decision list across the ~14
  existing PRDs. That is session `f84749dc`'s, by an explicit boundary set on 2026-10-25 at 22:34Z.
- **Retiring `inner_loop`'s modelling layer.** Roughly twenty `v2_*` tables (`v2_models`,
  `v2_reactions`, `v2_model_fva`, `v2_pangenome_neighbors`, `v2_protein_families`, ...) have no
  `fitness` equivalent and need homes designed. Q12 explicitly leaves them where they are.
- **Rewriting `kbaseincubator.fitness` in place.** This PRD creates a new namespace. Whether
  `fitness` is migrated, dual-written or left alone depends on the reader inventory this PRD does
  not have (Q17.1) and on finding its writer (Q17.2).
- **A consensus/verdict table.** `fitness.annotation` and `fitness.gene_function` both attempt one
  (`category`, `verdict`, `u_gene`, `n_hypotheses`, `n_tools`, `agreement`) and both are dropped —
  the first because it is 100% orphan, the second because it is empty and its rule is unstated.
  Multi-tool consensus is a real need and deserves its own PRD with its rule written down.
- **Gene-level aliases.** `fitness.gene_alias` is dropped as empty and under-specified; a gene-level
  alias table should be added when a demonstrated need names its vocabulary.
- **`kbaseincubator.genome_clearhouse`** (`genome_quality`, `skani_distances`) — not opened, not
  claimed, not ruled out (Q17.4).
- **GA4GH seqcol adoption.** `genome-identity-v1` Q13 left this open and lake-5 D5.1 holds. Worth
  re-costing periodically: the longer the corpus grows under the bespoke rule, the more expensive
  adopting the standard becomes, and nobody has costed that curve.

## Further Notes

**On why this builds on `fitness` rather than on the clearinghouse.** The clearinghouse is a store
of *entities* — non-redundant, content-addressed, one row per distinct thing. A genome's gene set is
not an entity set; it is a *structure*, with coordinates, strands, containment and overlap. The
`fitness` scheme is the right base precisely because it already models that structure correctly:
assembly-keyed, contig-bearing, with separate protein and DNA hashes. The two are complementary and
this design joins them rather than choosing.

**On the one thing `fitness` gets right that is easy to lose.** Separate `protein_seq_hash` and
`dna_seq_hash` with a typed sequence reference is what lets a non-coding gene be a first-class row
with no structural change. It would have been tempting to "simplify" to one `seq_hash` plus a type
column. That would have made a tRNA and a CDS indistinguishable at the schema level, which is the
exact confusion `CanonicalNonCodingFeature` goes out of its way to make structurally impossible
upstream. Preserve it.
