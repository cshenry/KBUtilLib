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

**THE CROSS-FAMILY ADVERSARY RAN, AND THIS DOCUMENT IS ITS SECOND VERSION.**
`codex` on h100 had a dead credential when the first draft was written (refresh token already used →
HTTP 401 → no `OPENAI_API_KEY`), confirmed by two independent Maestro dispatches, so confront was
recorded as **skipped** rather than faked — the invariant forbids a Claude adversary against a
Claude-written PRD. Chris fixed the login; I verified the binary with a tool-using probe and ran
round 1 as `task-16879cc1` (codex/GPT-5, 36,137 bytes,
[report](research/confront-round-1-stall-report.md), freshness confirmed by the artifact path being
absent on `main` at dispatch).

**It returned 18 stall points and its headline was "do not start production loading from this PRD."
All 18 are now folded or explicitly answered**, and three of them reshaped the design rather than
polishing it: Q1 (snapshot versioning — the draft's append-only, global-primary-key and
snapshot-idempotence claims were mutually impossible), Q3 (`assembly` could not be keyed on the
entity hash *and* front several content hashes), and Q6 (reusing the clearinghouse sharder "unchanged"
would have discarded all but one gene per genome, because its key spec does *entity* dedup). The
mutable `is_current` column it attacked is gone entirely (Q15), the read protections became
projections (Q16), and the ledger became terminal-receipt-only after it pointed out that an
append-only `FALSE` row permanently poisons its batch (Q17).

**One flaw was mine in how I ran it, and it should temper how stall 1 is read:** the worker's
worktree is off `main`, which carries neither this PRD nor the sibling PRDs it cites (those are on
`wip` and on the PRD branch), and **I did not embed the taskplan** — the report says so in its own
preamble. So its "absent from this checkout" complaints are partly an artifact of the dispatch. The
valid core survived and is folded: the delivery contract is now pinned in Q19 rather than cited.

**The external scan ran on the CLAUDE backend** (`task-86c02ac1`, 86,906 bytes) after the codex
attempt failed, because a research step is not adversarial and a same-family model is a defensible
substitute there. It still found a real defect the author missed (Q5). But it is same-family and
inherits this session's blind spots; Q6's sizing is where that matters most, which is why the layout
rehearsal measures rather than asserts.

**Confidence.** High on everything measured — the numbers above are live reads, reproducible from
the recorded queries. Medium on Q3's sizing constants, which are transferred from
`clearinghouse_shard.py`'s target rather than measured on this corpus. Low on Q13's retirement
sequencing, for the reader-inventory gap named above.

---

## Revision Log

**Round 1 confront — 2026-10-25 (FOLDED).** `task-16879cc1` (codex/GPT-5) returned 18 stall
points; all 18 are now folded or explicitly answered. The three structural ones reshaped the design:
snapshot versioning became Q1 and re-keyed every table (the draft had asserted append-only *and* a
global primary key *and* snapshot-keyed idempotence, which cannot hold together); `assembly` became
one row per FASTA *representation* with `contig` scoped to it (Q3); and the sharder reuse was
rewritten after it emerged that the clearinghouse key spec does *entity* dedup and would have
discarded all but one gene per genome (Q6). The mutable `is_current` column is gone, replaced by
immutable declarations plus a projection (Q15); three `published_current_*` read projections replace
what were admonitions (Q16); the ledger became terminal-receipt-only (Q17). Eighteen tables, 25
judgement calls.
**A measurement taken during the fold corrected the draft and its own handover brief:** `genome_id`
is **not** a truncated content hash — it is `prefix + a contiguous decimal ordinal` (`unk1` …
`unk4865`), which invalidated the stated rationale for Q6's routing and for Q5's collision mechanism,
though both conclusions survived.

**Round 0b — 2026-10-25 (research fold).** External scan folded. New Q4 (annotation-run scoping)
after the scan found a real collision: `gene_id` alone is not a key. `build_id` threaded through
every gene-referencing table. Eighteen tables, not seventeen.

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
scheme — the same singular domain names, the same separation of `protein_seq_hash` from
`dna_seq_hash` — with the fourteen defects of that scheme decided before any row is written, two of
its tables dropped rather than carried forward, and four more tables added that the scheme has no
equivalent for.

It is a **companion** namespace, not a replacement: it holds what KBDL knows about a genome, it is
loaded by one loader from one delivered snapshot, and it joins outward to
`kbaseincubator.clearinghouse` for entity identity and sequence and to the KBDL object store for
bytes. It stores no sequences of its own.

**Eighteen science tables** in six layers, every row an immutable snapshot version (Q1):

**Identity and structure** — `assembly` (one row per FASTA *representation*), `contig`, `genome`,
`genome_alias`.
**Builds** — `genome_build`, the annotation-run entity (Q5).
**Features** — `gene`, `gene_qualifier`.
**Provenance** — `genome_build_provenance`, `genome_caller_run`.
**Annotation** — `function`, `function_annotation`, `ontology_term`, `ontology_annotation`,
`transyt_annotation`.
**Fitness** — `condition`, `essentiality`, `experimental_fitness`, `propagated_fitness`.

**Three operational tables** that are not science and are not snapshot-versioned:
`load_ledger` (one terminal receipt per delivery and table — Q17), `genome_build_currency` and
`genome_disposition` (immutable declarations, projected rather than mutated — Q15).

**Three read projections**, which are the only sanctioned way to select science (Q16):
`published_current_genome`, `published_current_feature`, `published_current_gene` — the last
excluding `repeat_region`, since a CRISPR array is not a gene.

Twenty-one physical tables and three projections in total.

The loader is an in-pod process reading a delivered KBDL snapshot **plus the full object closure it
points at** (Q19), following `council-record-lakehouse-v1`'s pattern, which is the one existing
instance of this shape on this platform. `KBDLStoreLoad`'s submission path is retired while its
historical job records stay readable (Q20).

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
21. As a `fitness` or `inner_loop` reader, I want this delivery to change nothing of mine, so that
    my queries keep working while retirement is designed separately against a real reader inventory.
22. As a reviewer of this design, I want every judgement call — the fourteen agenda items and the
    eleven the research scan and the adversary added — to carry a decision and a blast radius, so
    that I can reverse any one of them by editing one line.
23. As a consumer, I want one supported way to select published, current, complete science, so that
    I cannot accidentally straddle two snapshots or count a CRISPR array as a gene.
24. As an operator, I want a layout rehearsal through the real ingest path before any production
    load, so that a file-size claim is measured rather than asserted.

## Implementation Decisions

### The namespace and its boundary

`kbaseincubator.genome_companion`. Created with `create_namespace_if_not_exists` (bare name, per the
operator runbook's dotted/bare rule); all `table_exists`/`load` calls use the dotted form.

It joins outward and stores nothing another store owns:

| It needs | It stores | Owner of the real thing |
|---|---|---|
| assembly bytes | `assembly.fasta_reference` (a pointer) | KBDL object store |
| protein sequence | `gene.protein_seq_hash` | `clearinghouse.protein_content` |
| gene DNA sequence | `gene.dna_seq_hash` | `clearinghouse.gene_content` |
| outward entity identity | `assembly_entity_hash` | `clearinghouse.genome_entity` |
| parameter-set text | `parameter_set_hash` | `clearinghouse.parameter_set` |

**The `stores_content=FALSE` reconciliation, stated because nothing anywhere states it.**
KBDL's local clearinghouse policy sets `stores_content = FALSE` for `genome` on the grounds that
assemblies are megabytes. The lake nonetheless has a `genome_content` table, and the two are
compatible: lake `genome_content` holds *metadata plus a `fasta_reference` pointer* and never the
bytes. `genome_companion.assembly` follows the same rule for the same reason.

### THE SNAPSHOT MODEL — read this before any key below makes sense

Every source-derived science row is an **immutable snapshot version**. Physical identity is:

```
(store_id, snapshot_id, <logical key>)
```

and **every logical key stated in this document applies WITHIN one snapshot, not globally.**

A supported *current* read selects **one** `snapshot_id` per `store_id` — the highest
`snapshot_seq` whose `load_ledger` shows every table in the declared inventory reconciled — and
applies that selection **before any join**. An empty table in the selected snapshot **stays empty**;
there is no fallback to an older version of that table, because a join across two snapshot versions
is exactly the silent corruption this model exists to prevent.

**Why this is first.** The first draft of this PRD asserted that every table was append-only, *and*
that `genome_id` was a primary key, *and* that idempotence was keyed on
`(store_id, snapshot_id, table_name)`. Those three cannot hold together on a second snapshot:
appending duplicates the key, skipping loses enrichment, replacing breaks append-only. The draft
cited `council-record-lakehouse-v1`'s snapshot-versioned keys as its precedent and then failed to
carry the mechanism into its own keys. `council_archive.py:10` and `:1356` are the reference
implementation; copy them rather than re-deriving.

Three operational surfaces are deliberately **NOT** snapshot-versioned science, and each is defined
in its own section below: the load ledger (terminal receipts), build currency and disposition
(immutable declarations plus a projection), and the published read projections.

### The provenance tail, on every table

The last six columns of every table, in this order, declared once as a module constant:

```
store_id STRING, snapshot_id STRING, snapshot_seq BIGINT,
load_batch_id STRING, observed_at TIMESTAMP, loaded_at TIMESTAMP
```

`store_id` is a **declared, durable** id, **never derived from a filesystem path** — a store that
moves off `/scratch` must keep its id or every row it has published becomes unjoinable.
`snapshot_id` is the verified uncompressed digest of the snapshot the row came from. `snapshot_seq`
is an explicit monotonic sequence for that store. `observed_at` is when the fact was true at the
source; `loaded_at` is when the row was written — separate, so reloading an old snapshot does not
make old facts look new. The KBDL store on poplar is `store_id = 'poplar/kbdl-1'`.

### Table: `assembly` — one row per FASTA REPRESENTATION

```
assembly_entity_hash STRING,    -- rule A, order-INdependent; the OUTWARD clearinghouse identity
assembly_content_hash STRING,   -- rule B, order-DEPENDENT; identifies THIS representation
fasta_reference STRING,         -- resolved current FASTA object id
fasta_reference_observed STRING,-- the FASTA object id as it appeared in the snapshot
size BIGINT, n_contigs BIGINT, longest_contig BIGINT, total_length BIGINT, is_closed BOOLEAN,
<provenance tail>
```

**Logical key: `(assembly_entity_hash, assembly_content_hash)`.** The first draft said the key was
`assembly_entity_hash` *and* that one entity hash "may front several content hashes" — a straight
contradiction, since one row holds one content hash and one FASTA pointer. A row is therefore one
*representation*: byte-identical FASTA forms share a row; a reordering or a header change produces a
second row under the same `assembly_entity_hash`.

**There is no column named `assembly_hash`, deliberately.** Rule A is
`standardizers.entity_hash("genome", contigs)` — contigs canonicalized by the DNA rule, empty ones
dropped, **sorted**, `|`-joined, sha256 of the UTF-8 bytes; order-independent. Rule B is sha256 over
the canonical FASTA bytes, header-inclusive and order-dependent. Same contigs in a different order:
same A, different B. **Measured:** live `fitness.assembly_hash` follows rule A, confirmed by
recomputing both rules over three real assemblies (1, 16 and 7 contigs) fetched from KBase workspace
262939 — A matched all three, B none.

### Table: `contig` — scoped by representation, not by entity

```
assembly_content_hash STRING, contig_id STRING, source_id STRING, length BIGINT,
<provenance tail>
```

**Logical key: `(assembly_content_hash, contig_id)`.** Contig ids and source ids belong to a
particular FASTA form, not to the order-independent entity hash: a header change preserves rule A
while changing contig identifiers, so a contig scoped only by entity hash cannot be resolved
unambiguously and a gene's `contig_id` could point at a name absent from the FASTA it was called
against. `fitness` has no contig table at all, which is why its `gene.contig_id` dangles.

### Table: `genome`

```
genome_id STRING,                    -- KBDL's G-number; the logical key WITHIN a snapshot
assembly_entity_hash STRING,         -- FK to assembly's entity identity; MANY-to-one
prefix STRING,
orgid STRING, source STRING,
genus STRING, species STRING, strain STRING,
ncbi_taxid STRING, lineage STRING,
taxonomy_ani DOUBLE, taxonomy_reference STRING, taxonomy_reason STRING,
taxonomy_source_database STRING,
<provenance tail>
```

The taxonomy columns are `TaxonomyBlock`'s eight fields flattened — all of them, because a taxonomy
call that cannot say *why* it was made (`reason`, `ani`, `reference`) is not reusable.

**`genome_id` is the logical key and `assembly_entity_hash` is not,** because the live corpus
contains the counterexample: `fitness.genome` holds 48 rows over 47 distinct assembly hashes, and the
colliding pair is `SyringaeB728a` and `SyringaeB728a_mexBdelta` — a wild-type and its knockout
mutant with a byte-identical assembly. The *hash* stays assembly-scoped, per Chris's decision (1);
the *genome row* needs its own identity, because an assembly does not determine a genome.

### Table: `genome_alias`

```
genome_id STRING, alias_source STRING, alias_id STRING,
<provenance tail>
```

Append-only, multi-valued, **no uniqueness**, fill-only. `alias_source` is a fixed vocabulary
validated at the write boundary; an unknown value is rejected, never coerced:

| `alias_source` | holds | stable? |
|---|---|---|
| `kbdl_genome_id` | the G-number | yes |
| `kbdl_object_id` | the genome JSON object id, **observed and resolved both recorded** | **no — moves on enrichment** |
| `kbdl_fasta_object_id` | the FASTA object id | no — moves on recompression |
| `assembly_accession` | e.g. `GCA_000007465.2` | yes |
| `arc_subject` | the `obj://` subject | yes |
| `ncbi_taxid` | NCBI taxonomy id | yes |

The vocabulary is fixed **at the start**: after the first load, append-only plus fill-only plus
no-production-delete freezes the spelling, and a second spelling of one source is indistinguishable
from a second source.

### Table: `genome_build` — the annotation-run entity

```
build_id STRING,                 -- the logical key; see the canonicalization below
genome_id STRING,
assembly_content_hash STRING,    -- WHICH representation this build was called against
gene_caller STRING,              -- 'bakta' | 'inprocess' | NULL (annotated source)
coordinate_convention STRING,    -- 'span' | 'kbase_location'; see below
structural_features_digest STRING,
built_at TIMESTAMP,
built_by_job_id STRING,
source_designated_current BOOLEAN,  -- what the SOURCE said; an immutable fact, not a pointer
<provenance tail>
```

**`build_id` is fully specified, because a hash with no byte serialization is not a key:**

```
build_id = sha256(canonical_json({
    "identity_version": 1,
    "genome_id":        <str>,
    "assembly_content_hash": <str>,
    "structural_features_digest": <str>,
    "caller_identity":  {"gene_caller": <str|null>,
                         "callers": {<package>: <version|null>, ...},
                         "bakta_db": <str|null>,
                         "bakta_image": <str|null>,
                         "bakta_driver_sha256": <str|null>,
                         "cds_overlap_drop_fraction": <canonical decimal string|null>}
}))
```

`canonical_json` is RFC-8785-style: sorted object keys, no insignificant whitespace, **explicit
nulls** (never omitted keys), UTF-8. Floating thresholds are encoded as **canonical decimal
strings**, never as floats, so `0.5` and `0.50` cannot produce two build ids for one build.
`structural_features_digest` covers every CDS and non-coding feature and its qualifiers, in
feature-id order, and **excludes** taxonomy and all downstream annotation results — so a non-coding
enrichment **does** change the build id (which the metadata tuple alone would not), while
re-annotating does not. **Golden input/preimage/digest vectors are a build deliverable**, not an
afterthought: a hash rule with no test vectors cannot be reimplemented.

`coordinate_convention` is carried as a build field because KBDL's `CoordinateConvention` is
genuinely ambiguous at the source (`span` vs `kbase_location`, resolved per-build by the adapter or
required when no DNA is present to disambiguate), and a strand/coordinate mistake hashes the wrong
DNA while producing entirely plausible rows.

**`source_designated_current` is an immutable fact, not a mutable pointer.** The first draft had an
`is_current BOOLEAN` that the loader mutated; see Q15 for why that is gone.

### Table: `gene`

```
gene_id STRING,          -- KBDL feature id verbatim: {genome_id}_{N} or {genome_id}_nc{N}
build_id STRING,
genome_id STRING,
assembly_content_hash STRING,  -- denormalized from genome_build so gene -> contig resolves
locus_id STRING,         -- source_id; NULL for an ab initio call
contig_id STRING,
start BIGINT, "end" BIGINT, strand STRING,
feature_type STRING,     -- 'CDS'|'tRNA'|'rRNA'|'tmRNA'|'ncRNA'|'misc_RNA'|'repeat_region'
protein_seq_hash STRING, -- NULL for every non-CDS feature_type
dna_seq_hash STRING,     -- always set
caller STRING, caller_version STRING,
product STRING, partial BOOLEAN,
amino_acid STRING, anticodon STRING, ncrna_class STRING, rfam STRING, rpt_family STRING,
<provenance tail>
```

**Logical key: `(build_id, gene_id)`** — see Q5. `assembly_content_hash` is denormalized here rather
than reached through `build_id` so that `gene -> contig` resolves without a join; it costs one
dictionary-encoded string per row, which is near-free in Parquet at ~2M distinct values across a
corpus of 10^9 rows.

`feature_type` carries **seven** values: `CDS` plus `NonCodingType`'s six. `repeat_region` is a
CRISPR array and **is not an RNA gene**; the finer SO/GenBank subtypes (`snoRNA`, `SRP_RNA`,
`RNase_P_RNA`, …) are deliberately not members and fold to `ncRNA` with the raw subtype in
`ncrna_class`, exactly as `map_non_coding_type` does upstream.

The invariant KBDL enforces structurally — a tRNA must never pass as a protein-coding gene — is
enforced here at the write boundary: `protein_seq_hash IS NOT NULL` **iff** `feature_type = 'CDS'`,
both directions an error. The schema cannot express it, so the loader must, and a violation fails the
load.

The five qualifier columns are nullable and sparse, and that is cheap: in Parquet with ZSTD an
all-null column is a run-length-encoded null bitmap, so the ~95% CDS majority pays almost nothing.

### Table: `gene_qualifier`

```
build_id STRING, gene_id STRING, qualifier STRING, value STRING,
<provenance tail>
```

The sidecar for `extra="allow"` passthrough. **Exactly one row per unpromoted extra key**, and
`value` holds **canonical JSON for every value** — strings, numbers, booleans, arrays, objects and
explicit null — preserving array order and duplicates. `str(value)` cannot distinguish a source
string from serialized structure and cannot round-trip an array, and unrolling a list would invent an
order policy. Keys promoted to named columns on `gene` (`product`, `partial`, `amino_acid`,
`anticodon`, `ncrna_class`, `rfam`, `rpt_family`) are **excluded** from this table, so no value
appears twice. Qualifier frequency, recorded into the ledger, counts **distinct `(build_id, gene_id)`
occurrences** per qualifier key.

### Table: `genome_build_provenance`

One row per **build**:

```
build_id STRING,         -- the logical key
genome_id STRING,
non_coding_source STRING,            -- 'called' | 'source' | 'none'
gene_caller STRING,                  -- 'bakta' | 'inprocess' | NULL (annotated source)
cds_dropped_overlapping_rna BIGINT,
cds_overlap_drop_fraction DOUBLE,    -- NULL = "not recorded" = built under the original any-overlap rule
cds_discarded_json STRING,           -- the caller's own discard tallies, canonical JSON
bakta_db STRING, bakta_image STRING, bakta_driver_sha256 STRING,
<provenance tail>
```

`cds_discarded` keys are the *caller's* vocabulary, so they are carried as canonical JSON rather than
promoted to columns we would extend whenever Bakta adds a reason. `bakta_db` is a string
(`"6.0-full"`) because what reproduces a call is which database *content* was used.

### Table: `genome_caller_run`

```
build_id STRING, genome_id STRING, caller_package STRING, caller_version STRING,
features_called BIGINT,
<provenance tail>
```

**One row per caller that RAN, including callers that ran and found nothing** (`features_called = 0`).
This table exists for one fact: "zero tRNAs" and "the tRNA caller never ran" are different things,
and without the zero-hit row they are indistinguishable. Absence of a row means the caller did not
run; `features_called = 0` means it ran and found nothing. **A reader must never infer the first from
a missing count.**

**`features_called` means RETAINED CANONICAL non-coding features attributed to that package — not
raw pre-filter caller hits.** The distinction matters: counting surviving features as if they were
raw hits would label a caller zero-hit after filtering or deduplication removed everything it found,
which is a *different* fact and the opposite of what this table is for. If raw hit counts are wanted
as science, the producer must deliver them as their own field rather than have the loader infer them.
A missing historical version stays **NULL with a recorded quality diagnostic** — never a guessed
default, because inventing provenance is worse than admitting its absence.

A shape that is valid and reads like a contradiction: a bare-FASTA genome built with
`call_non_coding=False` has `non_coding_source='none'` **and** a `gene_caller` that certainly ran.

### Tables: annotation

```
function(function_id, description, <tail>)
   logical key: function_id
function_annotation(build_id, gene_id, function_id, tool, tool_version,
                    parameter_set_hash, score DOUBLE, <tail>)
   logical key: (build_id, gene_id, function_id, tool, tool_version, parameter_set_hash)
ontology_term(ontology_id, namespace, term, label, definition, lineage, <tail>)
   logical key: ontology_id
ontology_annotation(build_id, gene_id, ontology_id, tool, tool_version,
                    parameter_set_hash, score DOUBLE, <tail>)
   logical key: (build_id, gene_id, ontology_id, tool, tool_version, parameter_set_hash)
transyt_annotation(build_id, gene_id, reaction_equation, tc_number, substrate,
                   tool_version, parameter_set_hash, <tail>)
   logical key: (build_id, gene_id, reaction_equation, tool_version, parameter_set_hash)
```

**`build_id` leads every gene-referencing table.** Once `gene` is keyed on `(build_id, gene_id)`, a
bare `gene_id` does not identify a row, so an annotation carrying only `gene_id` cannot say which
structural call it annotates — and with two builds present it would silently match both. The foreign
key is `(build_id, gene_id)` throughout. **The build is resolved from the annotation job's actual
input genome object, never from whichever build is currently designated current**; an annotation
whose input build cannot be recovered is **quarantined**, not attached to a guess.

`tool` is closed to `BAKTA`, `PROKKA`, `DRAM2`, `RAST`, `KOFAMSCAN`, `TRANSYT`, exported as one module
constant. Adding a seventh is a load, not a migration — which is the whole argument against the wide
`annotation` table.

`parameter_set_hash` resolves against the **existing** `clearinghouse.parameter_set` registry, which
keeps the pre-image so the rule stays recomputable. We do not mint a second registry. **`{}` is NOT
universally the default**: `clearinghouse_parameters.py:184` shows TRANSYT *requires* a `taxonomy_id`
and rejects an unidentifiable default, so the per-tool parameter validator already in the repo is
authoritative and must be enforced rather than bypassed. Registry rows are appended **before** the
annotation rows referencing them, and the loader runs the orphan check (distinct
`parameter_set_hash` in annotation tables absent from the registry) and requires it empty.

`score` replaces the `evidence` indirection: nullable, and holding it on the annotation row removes a
2.5M-row join that carried no other information. **Its meaning is per-tool and must come from the
pinned mapping** (below), not from a guess about which number a tool emits.

**One pinned source-to-target mapping and one fixture per supported tool is a build deliverable.**
Tool outputs are not interchangeable schemas; identifier derivation, score meaning, and actual
version and parameter extraction differ per tool, and none of it is inferable from the destination
columns.

### Tables: fitness

```
condition(condition_id, name, description, condition_group, media, modelseed_compound_id,
          temperature DOUBLE, ph DOUBLE, aerobic BOOLEAN, <tail>)
   logical key: condition_id   -- NO build_id: a growth condition is a property of an experiment
essentiality(essentiality_id, build_id, gene_id, is_essential BOOLEAN, call_source,
             source_ref, <tail>)
experimental_fitness(experimental_fitness_id, build_id, gene_id, condition_id,
                     fitness DOUBLE, t_score DOUBLE, provenance, source_ref, <tail>)
propagated_fitness(propagated_fitness_id, build_id, gene_id, condition_id,
                   max_fitness DOUBLE, min_fitness DOUBLE, average_fitness DOUBLE,
                   n_fitness_hits BIGINT, <tail>)
```

The three gene-keyed tables inherit `(build_id, gene_id)` for the reason above. **This is the most
expensive single consequence of build scoping**: `propagated_fitness` is already 14,076,257 rows and
grows as genomes × conditions, so one extra dictionary-encoded string there is the largest storage
cost the decision imposes. It is still right — a fitness value attributed to the wrong structural
call is worse than a wider row — but it is the number to quote if Q5 is revisited.

**These four tables have no KBDL producer today.** KBDL builds and annotates genomes; it does not
hold experimental fitness. They are specified so the namespace is complete and so a later fitness
delivery has a destination, and the load ledger's declared inventory marks them **optional**: a
delivery that carries no fitness data reconciles with zero expected and zero written rows. See Q24.

**The authoritative schema is the schema module, not this prose.** Column lists and types for every
table live in `genome_companion_schema.table_configs()`; this section is the rationale. Where a shape
is inherited from `fitness`, it was captured from the live namespace (measured 2026-10-25) and not
reconstructed from sample values.

### Table: `load_ledger` — terminal receipts, not progress

```
table_name STRING, rows_expected BIGINT, rows_written BIGINT,
reconciled BOOLEAN, started_at TIMESTAMP, finished_at TIMESTAMP,
qualifier_counts_json STRING, shard_digests_json STRING,
<provenance tail>
```

**Logical key: `(load_batch_id, table_name)`**, both from the tail. The first draft listed
`load_batch_id`, `store_id`, `snapshot_id` and `snapshot_seq` as business columns *and* inherited them
in the mandatory tail — duplicate column names, invalid DDL.

Three rules make this operable rather than a trap:

1. **Exactly ONE TERMINAL RECEIPT per `(delivery, table)`, written only AFTER reconciliation.**
   Retry and in-progress state live **outside** the append-only lake table, in the producer-side
   durable batch inventory. An append-only `reconciled = FALSE` row would otherwise **permanently
   poison its batch** under the literal "any FALSE row makes the batch unpublishable" rule, since
   append-only data cannot retract it.
2. **`rows_expected` is derived from the DELIVERED BUNDLE, never from what was written.** A ledger
   that compares a number to itself reconciles trivially and proves nothing.
3. **Completion covers the DECLARED inventory of source-derived tables, including zero-row tables**;
   it **excludes** the ledger reconciling itself (recursive) and **excludes** independently-authored
   curation (a missing curator must not block archival forever). Any unresolved shard blocks
   publication. Concurrent retries are protected by single-writer locking bound to
   `(delivery digest, namespace, schema version)`, reusing council's ambiguous-commit recovery
   (`council_archive.py:29`, `:748`, `:942`).

### Build currency and disposition — immutable declarations plus a projection

```
genome_build_currency(declaration_id, genome_id, build_id, declared_by, declared_at, <tail>)
genome_disposition(declaration_id, genome_id, disposition, reason, declared_by, declared_at, <tail>)
   disposition IN ('published','pilot','trial','superseded','excluded')
```

Both are **append-only logs of immutable declarations**. Neither has a mutable column.

- **Current build** = for each `genome_id`, the declaration with the greatest `declared_at`, tie-broken
  deterministically by `declaration_id`; absent any declaration, the `source_designated_current`
  build of the selected snapshot. This replaces the first draft's mutable `is_current` flag, which
  had no winner rule, no atomic update mechanism, no behaviour for loading an older snapshot after a
  newer one, and no statement of which physical snapshot-version row got mutated. `BerdlCapability.load`
  exposes append and overwrite, not a pointer transaction (`capability.py:458`), so the mutable
  column was not implementable through the sanctioned rail in the first place.
- **Effective disposition** = same rule: latest `declared_at`, deterministic `declaration_id`
  tie-break. **The loader creates NO disposition declaration.** Absence remains **unpublished**;
  absence is never an implicit `published`. The failure directions are asymmetric — wrongly
  withholding a result is visible and fixable, wrongly publishing a trial is neither.

**Nothing downstream may treat "exists in `genome_companion.*`" as "is a result."**
`council-record-lakehouse-v1` paid for this with twenty-one jobs including two cancelled 19,276-item
runs that looked authoritative by label and size. KBDL has its own instance: `object_supersedes`
holds 28,051 rows all reasoned `atb-recompression-v1` — precisely "present in the archive,
superseded as a result".

### The read surface — projections, not admonitions

Three supported projections, which are **the only sanctioned way to select science**:

| projection | selects |
|---|---|
| `published_current_genome` | one fully-reconciled snapshot per store; effective disposition `published` |
| `published_current_feature` | the above, plus the current build; **includes** `repeat_region` |
| `published_current_gene` | the above, but **EXCLUDES** `repeat_region` |

Each joins on full scoped keys and applies snapshot selection **before** any join. **Raw tables are
archival inputs, not a query surface.**

This exists because the protections in the first draft were *admonitions*. One could implement every
base table exactly as specified and still fail the user stories: partial rows stay visible, a query
can straddle two snapshot versions, an excluded genome or a CRISPR array enters a gene count. A
pointer column does not filter `SELECT ... FROM gene`.

If SQL views cannot be created through the sanctioned write rail, the projections are **materialized
as tables** rebuilt per delivery, with their definitions pinned in the schema module so the two
cannot drift — and the acceptance rehearsal asserts the materialized form matches the definition.

### Partitioning and physical layout

| table | `partition_by` | why |
|---|---|---|
| `gene` | `feature_type` | 7 values; prunes the ~95% CDS majority away from the live driving case |
| `function_annotation` | `tool` | 4–6 values; the clearinghouse's `source` analogue |
| `ontology_annotation` | `tool` | as above |
| `genome_caller_run` | `caller_package` | low-cardinality, and "which genomes ran caller X" is real |
| everything else | *(none)* | below the threshold, or no low-cardinality column exists |

**No table is partitioned on a hash.** A good hash scatters uniformly by design, so partitioning on
it buys zero read pruning while multiplying partition count — the clearinghouse settled this with the
same reasoning.

**THE KEY SPACE IS NOT WHAT THE FIRST DRAFT AND ITS HANDOVER BRIEF BOTH SAID. Measured
2026-10-25:** `genome_id` is **not** a truncated content hash. It is `prefix + a contiguous decimal
ordinal` — all 4,865 live ids are `unk1` … `unk4865`, a single prefix `unk` and a dense counter,
100% matching `[A-Za-z_]+\d+` with no gaps. Three consequences, and each invalidates something the
draft asserted:

1. **Equal-width *lexicographic* ranges over this key are catastrophically skewed,** because `unk1`
   captures 1, 10–19, 100–199 and 1000–1999. Measured first-four-character buckets: `unk1` 1111,
   `unk2` 1111, `unk3` 1111, `unk4` 977, `unk5` 111, `unk6` 111. A 64-hex range cut would put most of
   the corpus in a handful of ranges.
2. **Sorting must be numeric on the parsed ordinal, not lexicographic on the string** — otherwise
   `unk10 < unk2` and the physical order is meaningless for pruning.
3. **But a dense ordered counter is BETTER than a hash for this purpose,** not worse: equal-width
   *integer* ranges over a contiguous counter give near-perfectly even shards with computed
   boundaries and no sampling, and min/max statistics on an integer-ordered key prune almost
   perfectly. The news is good; the draft's stated reason for it was simply wrong.

**File size and locality are controlled by the WRITER, because they cannot be controlled by the
partition spec.** `table_configs()` emits only `name`, `schema_sql` and an optional `partition_by`,
rendered as `PARTITIONED BY (\`col\`)` over bare stored columns — so
`write.target-file-size-bytes`, Iceberg sort order, `write.distribution-mode` and Parquet bloom
filters are **not expressible through this write path at all**.

**Reuse `clearinghouse_shard.py`'s bounded spill/merge MECHANISM — and NOT its key spec.** The first
draft said "reuses the range-cutting algorithm unchanged with `genome_id` as the sort key", which
would have lost data: `_key_spec` (`clearinghouse_shard.py:5360`) selects clearinghouse **entity
dedup** keys, so substituting `genome_id` would have **discarded all but one gene per genome**, and
`_range_boundaries` (`:5266`) assumes a 64-hex key space this key is not. Sorting by genome is not
deduplicating by genome. So:

- **An explicit per-table key and routing map**, never a universal instruction:

| table group | route on | sort by | dedup on |
|---|---|---|---|
| `gene`, `gene_qualifier`, the three annotation tables, the three gene-keyed fitness tables | parsed integer ordinal of `genome_id` | `(genome_ordinal, full logical key)` | identical **full snapshot-scoped** key |
| `genome`, `genome_alias`, `genome_build`, `genome_build_provenance`, `genome_caller_run` | parsed integer ordinal of `genome_id` | `(genome_ordinal, full logical key)` | as above |
| `assembly`, `contig` | `assembly_content_hash` (a real 64-hex key — equal-width hex ranges are correct *here*) | `(assembly_content_hash, contig_id)` | as above |
| `function`, `ontology_term`, `condition` | — one bounded stream; small | logical key | as above |

- **Deduplicate only identical full snapshot-scoped keys, and REJECT conflicting duplicate
  payloads** rather than silently keeping one. Two rows with one key and different content is a
  producer defect and must fail the load.
- **The ordinal parse is validated**, and a `genome_id` that does not match `[A-Za-z_]+\d+` fails the
  load rather than being routed by a fallback — a silent fallback is how the whole corpus lands in one
  range.
- **Partitioned tables get independent partition-local merges**, so a shard never spans partitions.

**512 MB is an UNCOMPRESSED ROW-SIZE BUDGET, not a promise about compressed Iceberg files.**
536,870,912 is Iceberg's own documented default `write.target-file-size-bytes`, which is why it is the
starting figure — but `clearinghouse_shard._row_bytes` (`:5301`) counts **string lengths**, not
compressed Parquet bytes, and `_write_parquet` (`:5314`) does not explicitly request ZSTD. The local
algorithm therefore cannot promise final file sizes, and Spark ingestion through the supported path
can repartition or fan out sorted input regardless.

So the layout requirement is **verified, not asserted**, by a rehearsal that is part of the build:
ingest a **multi-shard, multi-partition** sample through the real supported path, then verify the
final Iceberg data files' genome ranges and measure query scan counts. The acceptance bound: **a
single-genome, current-build gene lookup touches at most two data files per represented
`feature_type` partition per selected snapshot**, excluding metadata files. An explicit compressed
file-size acceptance range is set from that measurement, not guessed. **If the rehearsal fails, the
rollout stops and the ingest path is amended before any production load.**

### The loader contract

An **in-pod loader** following `council-record-lakehouse-v1`'s pattern rather than a KBDL push: the
lakehouse write path runs in-pod; KBDL on poplar reaches the lake over a KBase token that expires on
a ~27-day cycle and has already lapsed silently once; and the delivery rail already exists.

**WHAT A DELIVERY CONTAINS — pinned here, because citing another document is what stalled the
adversary.** A delivery is a snapshot **plus the object closure it points at**, because SQLite
registry rows are *not* the genome objects that contain features. Specifically:

1. the SQLite backup (taken through the **sqlite3 backup API**, never a file copy — a `cp` of a live
   WAL database yields a torn backup that opens fine);
2. **every genome JSON, FASTA and annotation object reachable from the selected registry rows and
   from their supersession chains**;
3. a manifest carrying `delivery_id`, `store_id`, `snapshot_id`, `snapshot_seq`, the snapshot schema
   version, producer `created_at` and `deadline_at` in **UTC**, and a relative inventory of
   `(path, bytes, sha256)` for every file.

The loader reads **only** those verified delivered objects — it makes no network call and holds no
credential for the source. A delivery that is SQLite-only would otherwise produce thousands of genome
headers and zero features, or force the loader back onto the credentialed path the rail exists to
avoid.

The ingest rules, inherited with their corrections:

1. **A manifest's presence is not proof of arrival.** The trigger is "the manifest exists **and**
   every file it lists is present **and** sha256-matches". A sync client has already reported a
   directory up to date while a listed file was absent.
2. **A first mismatch means "still syncing"** — retry, with progress persisted outside the lake.
3. **Past the manifest's own `deadline_at` it is a failed delivery,** raised with evidence: which
   file, expected vs observed size, expected vs observed digest. For a legacy mtime-based manifest the
   deadline is **frozen on first observation**, because consumer-side mtime can reset on sync.
4. **A receipt on disk is a cache** whose identity must match the manifest digest, never an authority.
5. **Idempotence** on `(store_id, snapshot_id, table_name)`: a re-run against the same snapshot is a
   no-op; a new `snapshot_seq` writes a new immutable version. This replaces
   `_lookup_existing_genome_id`'s `skipped_already_ingested` behaviour, under which a reload is a
   silent no-op *even when the genome has changed*.
6. **Supersession is resolved from the DELIVERED snapshot's table only** — never a live lookup — so a
   snapshot determines its rows. Q13 covers the two independent chains.
7. **The zero-join guard runs before every relationship assertion**, scoped per Q24.

`KBDLStoreLoad` is retired, with care — see Q20.

## Testing Decisions

**Test external behaviour, not implementation.** What is externally observable here is: what is in
the tables after a load, what the loader refuses, and what a query costs.

**The criterion no stub can satisfy.** Every table-shape test could be made to pass by a fixture that
fakes the lakehouse. The guard is a **real-dependency smoke task in phase 1** that creates one real
table in a scratch namespace against the live BERDL endpoint, writes one row, reads it back, and
asserts the round-tripped types — in particular that `TIMESTAMP` survives with its value, which
`clearinghouse_shard.py` records as a live trap — and that a `PARTITIONED BY` clause actually lands,
read back via `SHOW CREATE TABLE` (`DESCRIBE` returns an empty result set through this endpoint).
Every later phase depends on it. The measured precedent is `kbdl-horizyn-v1`, where eight tasks passed
on injected fixtures and both real defects surfaced in the first phase to touch the real package.

**Rejections, not happy paths.** For each rule the test attempts the *violation* and asserts the
refusal, with the error naming the offending field and value:

- a tRNA carrying a `protein_seq_hash`; a CDS with a null one;
- `feature_type = 'snoRNA'` — a legal upstream *input* subtype that folds to `ncRNA`, and **not** a
  legal `feature_type`;
- `alias_source = 'kbdl_object'`, a near-miss spelling of `kbdl_object_id`;
- `store_id = '/scratch/kbdl'`, a path-like store id;
- a `function_annotation` row with a `tool` and no `tool_version`;
- a `genome_id` that does not parse as `[A-Za-z_]+\d+`;
- two rows with one full logical key and **different payloads**;
- an annotation whose `(build_id, gene_id)` does not resolve **even though its `gene_id` alone would
  resolve under a different build** — the exact ambiguity the compound key exists to prevent.

**The zero-hit caller, both directions.** A caller that ran and found nothing produces a
`genome_caller_run` row with `features_called = 0`; a caller that never ran produces **no row**. Both
assertions must be present — testing only the first cannot distinguish the two states, which is the
entire reason the table exists. And `features_called` is asserted to count **retained** features, by
a fixture where the caller emits features that are then filtered out.

**The rebuild case, which is what build scoping is for.** Load one genome built `inprocess`, then the
same genome rebuilt with `gene_caller='bakta'` at different coordinates. Assert: two `genome_build`
rows, both gene sets retained, **one** `genome` / `assembly` / `contig` set (not duplicated), and that
a `gene_id`-only query returns **two** rows while the `published_current_gene` projection returns one.

**The two-genomes-one-assembly case is a TEST-ONLY fixture, and this is a real limitation.** The
acceptance pair (`SyringaeB728a` / `SyringaeB728a_mexBdelta`) lives in `fitness`, **not** in KBDL, and
KBDL's registry dedupes exactly that shape on `content_hash` — so a KBDL-only loader **cannot
produce the pair**, and this PRD does not repair that. The pair's captured rows ship as a fixture with
an explicit test-only two-id mapping; **`fitness` UUIDs are never ingested into production as KBDL
G-numbers.** Supporting the pair as two production KBDL genomes is gated on a separate registry
identity change, named in Out of Scope. The schema permits two genome ids per assembly; the upstream
registry currently does not supply them.

**Snapshot versioning.** Load snapshot 1; load snapshot 2 with one genome's taxonomy changed. Assert
both versions present, the current projection returning only snapshot 2's, and no primary-key
collision. Then load snapshot 1 **again** and assert a no-op. Then load an *older* snapshot after a
newer one and assert the current projection does not regress.

**Assembly representation.** A reordered-contig fixture produces the same `assembly_entity_hash` and a
different `assembly_content_hash`, **two** `assembly` rows, and two `contig` sets — with each build's
genes resolving to the contigs of their own representation.

**Supersession, two chains, two fixtures.** A FASTA chain of depth 2 resolves into
`assembly.fasta_reference` with the observed id retained; a genome-JSON chain resolves into
`genome_alias` under `kbdl_object_id` with both ids recorded. A cyclic or missing-target chain
**raises**. Note this path has **zero live instances** (0 of 4,865 genomes, 0 chains of depth > 1), so
the fixtures are the only coverage and must be built deliberately.

**`build_id` golden vectors.** Published input/preimage/digest triples, including: a reordered
`callers` dict producing the **same** digest; `0.5` vs `0.50` producing the same digest; an explicit
`null` distinguished from a missing key; and a non-coding enrichment producing a **different** digest
while a re-annotation produces the same one.

**Qualifier round-trip.** A mixed-type fixture — string, integer, float, boolean, explicit null,
array with duplicates and preserved order, nested object — round-trips through `gene_qualifier.value`
as canonical JSON. A promoted key (`rpt_family`) appears on `gene` and **not** in the sidecar.

**The sharder, including the failure it would have had.** Equal-width integer boundaries over the
parsed ordinal are input-independent; per-shard output is sorted numerically (`unk10` after `unk2`);
every input row appears **exactly once** across outputs, including across a buffer-flush boundary;
memory stays bounded with an input larger than the buffer; a deliberately skewed id population fails
loudly; and — the regression test for the defect this fixed — **a fixture of many genes per genome
produces every gene, not one per genome.**

**The layout rehearsal** is the acceptance bound above, measured through the real ingest path, not
asserted from the local algorithm.

**Zero-join guard, correctly scoped.** A blanket non-empty check would reject correct deliveries —
`gene_qualifier` is legitimately near-empty, a no-hit annotation set is legitimately empty, and the
four fitness tables have no KBDL producer at all. So: the guard requires a **non-empty selected
genome/root** for the relationship suite and for production deliveries; an **optional** child table
may be zero-row when its independently derived expected count is zero; a **positive expected count
with zero extracted rows fails**. Key non-nullness is asserted only for keys that are actually
required; explicitly nullable fields stay legal. **The guard's own test** runs the relationship
assertions over a deliberately empty **required** fixture and asserts they **fail** — written so that
deleting the guard makes the test fail.

**Prior art in the codebase:** KBDL's `tests/test_layering.py` is the model for invariant-as-test;
KBUtilLib's clearinghouse bootstrap tests are the model for DDL and partition-spec assertions;
`council_archive.py` is the model for ledger and ambiguous-commit tests.

**Regression framing.** Both repos carry pre-existing failures, so every criterion is "no test that
passed on the base commit fails on the branch, and the tests this task adds pass" — never "the suite
is green".

## Open Questions and Judgement Calls

**Every item below is already decided and reflected in the PRD body and taskplan. The PRD is
buildable as it stands. To change a decision, edit its `DECIDED:` line** — Review Mode will
propagate the consequences through the body, the user stories, the testing decisions and the
taskplan.

Ordered by blast radius descending; within a radius, lowest confidence first.

### Q1. What is a row's identity when the same genome arrives in two snapshots? -- DECIDED: every source-derived science row is an immutable snapshot version with physical identity `(store_id, snapshot_id, <logical key>)`; every logical key in this document applies WITHIN a snapshot; a current read selects ONE fully-reconciled snapshot per store BEFORE any join.
**Blast radius:** IRREVERSIBLE
**Why:** The first draft asserted three things that cannot hold together on a second snapshot: every
table is append-only, `genome_id` is a primary key, and idempotence is keyed on
`(store_id, snapshot_id, table_name)`. Appending duplicates the key; skipping loses enrichment and
taxonomy changes; replacing violates append-only. The provenance tail alone does not choose between
them. Worse, the draft *cited* `council-record-lakehouse-v1`'s snapshot-versioned keys plus a current
projection as its precedent, quoted its provenance triple, and then did not carry the mechanism into
its own keys. Selecting the snapshot **before** any join is the other half: a join evaluated across
two snapshot versions is the silent corruption this exists to prevent, and an empty table in the
selected snapshot must stay empty rather than fall back to an older version.
**If you disagree:** the alternative is an incrementally-maintained current database, which means
in-place updates, which the sanctioned write path does not offer as a row-level operation
(`capability.py:458` gives append and overwrite) and which destroys the ability to attribute a wrong
row to a load. Reverting re-keys all eighteen tables, removes the snapshot-selection step from the
three read projections, and makes reload semantics undefined again.
**Confidence:** high on the need — the three-way contradiction is mechanical, not a matter of taste.
**Medium on the precise selection rule**: "highest `snapshot_seq` with every declared table
reconciled" is the council pattern transplanted, and I have not exercised it against a partially
reconciled multi-store corpus, because none exists yet.

### Q2. If two genomes can share one assembly, what identifies a genome? -- DECIDED: a surrogate `genome_id` (KBDL's G-number) is the logical key within a snapshot; `assembly_entity_hash` is a many-to-one foreign key and is never a key.
**Blast radius:** IRREVERSIBLE
**Why:** The live corpus contains the counterexample. `fitness.genome` holds 48 rows over 47 distinct
assembly hashes with zero nulls, and the colliding pair is `SyringaeB728a` and
`SyringaeB728a_mexBdelta` — a wild-type and its knockout mutant with a byte-identical assembly.
Chris's decision (1), that the genome clearinghouse hash is assembly-scoped, is untouched: the *hash*
is assembly-scoped and the *genome row* needs its own identity, because an assembly does not
determine a genome. Keying on the hash would have merged a mutant into its parent on row 48 of the
smallest table in the namespace.
**If you disagree:** keying `genome` on `assembly_entity_hash` collapses it to 47 rows on the current
corpus and loses `SyringaeB728a_mexBdelta`; `gene`, `genome_alias`, `genome_build`,
`genome_build_provenance`, `genome_caller_run`, both declaration logs and every gene-keyed fitness
table re-key, and the mutant/WT distinction has to live somewhere else — most plausibly a
`genome_alias` row, which cannot work because aliases are fill-only and non-unique.
**Confidence:** high — a measured counterexample in live data. **But note the limitation in Q24**:
the schema permits two genome ids per assembly while KBDL's registry does not currently *supply*
them, so the acceptance fixture is test-only.

### Q3. Can one `assembly` row represent several FASTA forms? -- DECIDED: no. One row per REPRESENTATION, logical key `(assembly_entity_hash, assembly_content_hash)`; `contig` keyed `(assembly_content_hash, contig_id)`; `assembly_entity_hash` remains the outward clearinghouse identity.
**Blast radius:** IRREVERSIBLE
**Why:** The first draft said the primary key was `assembly_entity_hash` *and* that one entity hash
"may front several content hashes" — a straight contradiction, because one row holds one
`assembly_content_hash`, one `fasta_reference` and one observed pointer. With rule A shared by two
content hashes I would have had to discard a representation, violate the key, or invent an
unspecified table. And contigs are worse than a tidiness problem: contig ids and source ids belong to
a particular FASTA form, a header change preserves rule A while changing contig identifiers, so a
contig scoped only by the entity hash lets a gene's `contig_id` point at a name absent from the FASTA
it was actually called against. This is independent of Q2's two-genomes-one-identical-FASTA case.
**If you disagree:** forcing one row per assembly *entity* requires a separate representation table
holding every rule-B-specific pointer and all the contigs, which is the same decision wearing a
different table name — or it requires picking an arbitrary FASTA form and accepting that coordinates
may reference contigs it does not contain. Reverting changes `assembly`, `contig`, `genome_build`,
`gene` and both sharder routing rules for those tables.
**Confidence:** high on the contradiction; **medium on the multiplicity's frequency.** I have not
measured how often one entity hash fronts two content hashes in KBDL — `genomes` has 4,865 rows and
4,865 distinct `content_hash`, so **zero** instances live today. The design must support it because
rule A is order-independent by construction, not because it has been observed.

### Q4. Which hash rule does the assembly column follow? -- DECIDED: neither, because there is no single column. Two columns, `assembly_entity_hash` (rule A) and `assembly_content_hash` (rule B), and no column named `assembly_hash`.
**Blast radius:** IRREVERSIBLE
**Why:** Rule A (`standardizers.entity_hash`: headers stripped, whitespace removed, uppercased, empty
contigs dropped, contigs **sorted**, `|`-joined, sha256) is order-independent and is the
clearinghouse's canonical genome identity. Rule B (sha256 of canonical FASTA bytes) is
header-inclusive and **order-dependent**. Same contigs in a different order: same A, different B.
`fitness.assembly_hash` declares neither rule, and a column that does not declare its rule is one a
reader will join to the wrong thing.
**Measured, so which rule the live column follows is not a hypothesis: it is rule A.** Three
`fitness` assemblies (1, 16 and 7 contigs) were fetched from KBase workspace 262939 via their
`fasta_handle_ref` and both rules recomputed over the real FASTA bytes. Rule A matched the stored
value in all three, rule B in none; two are multi-contig, so rule A's sort step is load-bearing and
still matched. So `assembly_entity_hash` is the *continuous* name for what is already there, the
namespace joins straight to `clearinghouse.genome_entity`, and the existing 47 rows are **not** a
data problem.
**The external scan disagrees about which rule should be PRIMARY, and the objection is good.** GA4GH
refget's seqcol spec explicitly considered and **rejected** "digest each sequence, sort, concatenate,
digest" as its primary collection identifier: it "would work perfectly fine for the simple use case
of asserting two sets of sequences match in content, [but] this is not the only use case", because
order and names matter to aligners, and "the final digest representing a sequence collection can only
answer *one* such question." Not conceded, and the reason is that this PRD carries **both** digests
under rule-naming columns, which answers seqcol's actual complaint; rule A is already the
clearinghouse `entity_hash` fleet-wide, is what `clearinghouse.genome_entity` is keyed on, and is what
the in-flight `clearinghouse-entity-alias-v1` keys `entity_alias` on, so making rule B primary would
re-key the clearinghouse rather than this namespace. Also worth acting on separately: seqcol's point
that "the normalization rule is half the specification" — rule A's case, whitespace, empty-contig and
header handling are all specified in `_standardize_genome`'s **code** and nowhere in prose a lake
reader would find.
**If you disagree:** collapsing to one column makes the other unjoinable; keeping the name
`assembly_hash` for rule A means every KBDL-side reader who knows `content_hash` as "the assembly
hash" joins wrongly and gets zero rows, or — worse, when order happens to match — the right rows for
the wrong reason.
**Confidence:** high, on both the rule and the live data.

### Q5. Is `gene_id` a key, given that one assembly can be annotated more than once? -- DECIDED: NO. Gene identity is scoped by an annotation run: a `genome_build` entity, `gene` keyed on `(build_id, gene_id)`, and `build_id` carried by every gene-referencing table.
**Blast radius:** IRREVERSIBLE
**Why:** The external research scan found this; the first draft had the defect. I keyed `gene` on
`gene_id` alone, reasoning that KBDL mints `{genome_id}_{N}` uniquely within a genome. The hole is
that `genome_registry._lookup_existing_genome_id` **dedupes on `content_hash`** — a hit returns the
**existing** `genome_id` — so rebuilding one assembly with `gene_caller='bakta'` after an
`inprocess` build reuses the same `genome_id`, hence the same `{genome_id}_{N}` id space, with
**different coordinates**. Two rows, one key, silently. (The draft's stated reason — "`genome_id` is
derived from the assembly content hash" — was itself **wrong**; see Q6. The collision is caused by the
registry's dedup, not by hash derivation, and the conclusion survives the correction.)
The disagreement is neither hypothetical nor small: Siqueira et al. 2025
(doi:10.1101/2025.05.27.656432) compared the GenBank and RefSeq annotations of a **single, identical**
*P. putida* KT2440 assembly and found 159 genes only in one, 81 only in the other, and **897 genes —
~16% of total ORFs — with IDENTICAL locus tags at DIFFERENT genomic positions** (455 differing in
start codon, 442 in stop, offsets from ≤27 bp to a few hundred). Every schema that faces this keeps
the sets apart: INSDC gives one assembly two accessions (GCA submitter vs GCF PGAP), Ensembl puts the
alternative set in a parallel `otherfeatures` database on the same schema, and Chado makes the
analysis part of feature identity (`analysisfeature unique (feature_id, analysis_id)`, `analysis`
keyed on `(program, programversion, sourcename)`). There is no prior art anywhere for reconciling
competing structural callers onto one feature row.
`build_id` is fully canonicalized in the body — RFC-8785-style sorted-key JSON with explicit nulls,
thresholds as canonical decimal strings, and a `structural_features_digest` over every feature and
qualifier in feature-id order — because a hash specified as "sha256 over a tuple" has no byte
serialization, and because a metadata-only tuple would **not** change under a non-coding enrichment
that changes the feature set.
**If you disagree:** keying on `gene_id` alone is survivable only while exactly one build per genome
ever reaches the lake — true today, and false the first time a genome is rebuilt under a different
caller, which `kbdl-build-genome-bakta-calling-v1` exists to make routine. Dropping `build_id` removes
`genome_build`, un-keys `gene`, `gene_qualifier`, `genome_build_provenance`, `genome_caller_run`, the
three annotation tables and the three gene-keyed fitness tables, and makes the first Bakta rebuild a
silent key collision. Adding it back afterwards is a full rewrite of the largest tables, because a
`build_id` cannot be inferred for rows already written.
**Confidence:** high on the need. **Medium on `build_id`'s composition** — the field set is my
construction; Chado keys `analysis` on `(program, programversion, sourcename)`, which is coarser and
would not distinguish two Bakta runs against different databases. The `structural_features_digest`
makes it self-correcting (two genuinely different feature sets cannot share a build id whatever the
metadata says), which is what moved this from low to medium. What would raise it: one real rebuild
loaded end to end, which the acceptance rehearsal now requires.

### Q6. How are the tables partitioned, routed and sized? -- DECIDED: partition only on low-cardinality stored columns; route and sort on the PARSED INTEGER ORDINAL of `genome_id` (not a hash, not lexicographically); reuse `clearinghouse_shard.py`'s spill/merge MECHANISM with an explicit per-table key map and NEVER its entity dedup; treat 512 MB as an uncompressed budget and VERIFY the layout through the real ingest path.
**Blast radius:** IRREVERSIBLE
**Why:** Four measured constraints, and the first draft got two of them wrong.
(a) **The key space is not what the draft or its handover brief said.** `genome_id` is **not** a
truncated content hash. Measured 2026-10-25: all 4,865 live ids are `unk1` … `unk4865` — a single
prefix `unk` plus a **contiguous decimal ordinal**, 100% matching `[A-Za-z_]+\d+` with no gaps. So
equal-width *lexicographic* ranges are catastrophically skewed (measured first-four-character
buckets: `unk1` 1111, `unk2` 1111, `unk3` 1111, `unk4` 977, `unk5` 111, `unk6` 111, because `unk1`
captures 1, 10–19, 100–199 and 1000–1999), a 64-hex range cut would collapse the corpus into a few
ranges, and string sorting puts `unk10` before `unk2`. Parsed-integer routing over a dense counter is
*better* than hash routing — perfectly even equal-width ranges with computed boundaries, and
near-perfect min/max pruning — but only once the key is read correctly.
(b) **Reusing the clearinghouse sharder "unchanged" would have lost data.** `_key_spec`
(`clearinghouse_shard.py:5360`) selects clearinghouse **entity dedup** keys, so substituting
`genome_id` would have **discarded all but one gene per genome**; `_range_boundaries` (`:5266`)
assumes a 64-hex space this key is not. Sorting by genome is not deduplicating by genome. Hence an
explicit per-table key/routing map, dedup only on identical **full snapshot-scoped** keys, and
rejection of conflicting duplicate payloads.
(c) **The write path cannot express an Iceberg transform or any table property.** `table_configs()`
emits only `name`, `schema_sql` and a bare `partition_by`, so `bucket(256, genome_id)`,
`write.target-file-size-bytes`, sort order and bloom filters are unavailable. The external scan's
recommendation — `ARRAY['bucket(N, genome_id)']`, N=256 for `gene`, 4096 for `propagated_fitness` —
is better Iceberg advice and is simply not requestable here; it is recorded as the target if the path
widens. It also supplied a trap: Trino's `iceberg.max-partitions-per-writer` defaults to **100** and
refuses writes above it.
(d) **512 MB cannot be promised from the local algorithm.** It *is* Iceberg's documented default
`write.target-file-size-bytes` (536,870,912), which is why it is the starting budget — but
`_row_bytes` (`:5301`) counts string lengths rather than compressed bytes, `_write_parquet` (`:5314`)
does not request ZSTD, and Spark ingestion can repartition sorted input. So the layout is **verified
by a multi-shard, multi-partition rehearsal through the real path**, with the bound "a single-genome
current-build gene lookup touches at most two data files per represented `feature_type` partition per
selected snapshot", and the rollout stops if it fails.
**If you disagree:** the expressible alternative is a materialized `genome_shard` column, which is a
bare stored column — but it needs `max-partitions-per-writer` raised first, forces every reader to
derive the shard before querying, and freezes the derivation rule forever. Changing any of this after
load is a full table rewrite.
**Confidence:** high on (a) and (b), which are measured and read from source. **Medium on the
sizing**, which is why the rehearsal is a build task rather than an assertion.

### Q7. Does the companion namespace store sequences? -- DECIDED: no. `sequence` is not carried; `gene.protein_seq_hash` and `gene.dna_seq_hash` are foreign keys into `clearinghouse.protein_content` and `clearinghouse.gene_content`, and `assembly.fasta_reference` is a pointer into the KBDL object store.
**Blast radius:** IRREVERSIBLE
**Why:** `fitness.sequence` has **0 rows** against 228,709 genes, so the typed sequence table the
scheme appears to offer has never been exercised and nothing depends on it. The clearinghouse exists
to be the non-redundant entity store, already has `gene_content` carrying `sequence`/`seq_length` and
`protein_content`, and partitions them on `standardizer_version` for this volume. A second copy of
10^9 sequences would be the largest cost in the design and the most likely to drift from the store
that owns it.
**If you disagree:** carrying sequences adds a `sequence` table partitioned by `seq_type`, makes this
the largest namespace in the tenant by an order of magnitude, and needs a reconciliation rule for
when its copy and `clearinghouse.gene_content` disagree. It also makes the namespace loadable without
the clearinghouse, which is the one real argument for it.
**Confidence:** high on the storage decision; **medium on the join ergonomics** — a scientist wanting
genes and sequences writes a cross-namespace join, and I did not measure what that costs in Trino
here.

### Q8. `feature_type` as a column or a separate table? -- DECIDED: one `feature_type` column on `gene`, with SEVEN values: `CDS` plus `NonCodingType`'s six.
**Blast radius:** MEDIUM
**Why:** The handover brief said five non-coding values; the source says six — `repeat_region` was
added for Bakta calling and is a CRISPR array, not an RNA gene. A column because the distinction is
one closed enumerated attribute, because `gene` already carries separate protein and DNA hashes so a
non-coding gene fits with no structural change, and because a lookup table would add a join to the
largest table in the namespace to answer "is this a gene".
**If you disagree:** widening the vocabulary to admit the SO subtypes as first-class values
contradicts `map_non_coding_type`, which folds them to `ncRNA` with the raw subtype in `ncrna_class`
specifically to keep the enum stable in a store that cannot delete.
**Confidence:** high — read from `NonCodingType`, including the sixth member the brief omitted.
The adversary explicitly found no blocker here.

### Q9. Where do the RNA qualifiers live? -- DECIDED: seven named nullable columns on `gene` PLUS a `gene_qualifier(build_id, gene_id, qualifier, value)` sidecar whose `value` is CANONICAL JSON for every type.
**Blast radius:** MEDIUM
**Why:** The named qualifiers are what define a tRNA, so they must be retrievable without a join on a
10^9-row table, and sparsity is nearly free (an all-null Parquet/ZSTD column is a run-length-encoded
null bitmap). But `CanonicalNonCodingFeature` sets `extra="allow"` precisely so an annotated source's
unanticipated qualifiers survive, and named columns alone would drop exactly those — a data-loss bug
no test would catch, because the dropped keys are by definition the ones nobody enumerated.
**Canonical JSON rather than `str(value)`** because `str()` cannot distinguish a source string from
serialized structure, cannot round-trip an array, and unrolling a list would invent an order and
duplicate policy. Promoted keys are excluded from the sidecar so nothing appears twice, and the
frequency counts recorded into the ledger count distinct `(build_id, gene_id)` occurrences, so
promoting a frequent qualifier later is an evidenced decision.
**If you disagree:** pure EAV costs a join for every tRNA query and loses typing;
named-columns-only silently drops passthrough.
**Confidence:** high on the shape; **medium on the Parquet null-cost claim**, a well-established
property of the format that I did not measure on this deployment's writer.

### Q10. Where does caller provenance live, and what does a zero-hit run mean? -- DECIDED: three places — per-feature `caller`/`caller_version` on `gene`, one `genome_build_provenance` row per build, and one `genome_caller_run` row per caller per build INCLUDING zero-hit runs, where `features_called` counts RETAINED canonical features.
**Blast radius:** MEDIUM
**Why:** Three different facts. "Which tool produced this feature" belongs to the feature; "which
caller and which Bakta database built this genome" belongs to the build; "which callers ran and what
each found" is a set, and it is the one that cannot be reconstructed from the other two.
`NonCodingProvenance.callers` deliberately includes callers that ran and found nothing, because a
later rebuild could not otherwise tell a genuinely tRNA-free assembly from one built before tRNA
calling existed.
**`features_called` means retained features, not raw hits** — counting survivors as raw hits would
label a caller zero-hit when filtering removed everything it found, which is a different fact and the
opposite of the table's purpose. **A missing historical version stays NULL with a recorded quality
diagnostic**, never a guessed default: inventing provenance is worse than admitting its absence. And
`coordinate_convention` is carried as a build field because the source is genuinely ambiguous
(`span` vs `kbase_location`) and a strand or coordinate mistake hashes the wrong DNA while producing
entirely plausible rows.
Note the warning in the live data: `fitness` **has** a `source_version` column on both annotation
tables and it is **NULL in 100% of rows** — 0 of 1,118,422 and 0 of 1,010,656. A column does not make
provenance happen; the write-boundary validators do.
**If you disagree:** folding all three into a JSON blob on `genome` makes "which genomes ran
pyaragorn 0.3.0" unqueryable, and dropping `genome_caller_run` loses the zero-hit fact permanently,
because it is recoverable from neither the feature rows (there are none) nor the genome row.
**Confidence:** high — the three-way split mirrors the source model exactly.

### Q11. Does the genome alias table live here or in `clearinghouse.entity_alias`? -- DECIDED: here, as `genome_alias`, append-only, multi-valued, no uniqueness, fixed `alias_source` vocabulary validated at the write boundary.
**Blast radius:** MEDIUM
**Why:** Chris's decision (3), and it must be satisfiable before `entity_alias` exists — that is a
separate PRD. The need is measured: KBDL's `genomes` row carries four identifiers (`genome_id`,
`content_hash`, `fasta_object_id`, `genome_object_id`), and `genome_object_id` **moves under
enrichment** while `content_hash` does not, so a single-slot pointer orphans every row that used the
old value. Append-only and fill-only because a superseded identifier is still the right answer to
"what was this called in the snapshot I am reading".
**If you disagree:** `clearinghouse-entity-alias-v1` has now confirmed (its Q1, IRREVERSIBLE, high
confidence) that `entity_alias` **is** generic over `entity_type` — `(entity_type, entity_hash,
alias_source, alias_id, observed_at, ingest_batch_id)` — so `genome_alias` could become a
**projection** of it rather than a second source of truth. That session's own advice is **not yet**:
its join key is `entity_hash` (rule A), which Q4 has now measured and declared on our side, so the
precondition is met — but `entity_alias` has **no retraction** by design, so a projection inherits a
table that cannot express "this identifier is no longer true". Converging via a view, with identical
column spelling, is the path; it changes the loader's input and makes this namespace depend on the
clearinghouse being bootstrapped first. The in-namespace precedent is weak either way:
`fitness.gene_alias` is `(gene_id, alias, type)` with **0 rows** and no `observed_at`.
**Confidence:** medium — right for this PRD's independence, and the sibling may land a generic table
that makes the projection better. It has been told the hash rule is settled.

### Q12. Are `annotation` and `evidence` carried forward? -- DECIDED: neither. Both dropped; `evidence`'s only non-redundant content becomes a nullable `score` on the annotation rows.
**Blast radius:** MEDIUM
**Why:** Measured. `annotation`: 272,076 rows of which **272,076 are orphans** against `gene` — its
`feature_id` is `organism:locus` while `gene.gene_id` is `{uuid}.{locus}`, spanning 60 organisms
against `gene`'s 48 genomes. A dead table from an earlier, differently-keyed load, and also the wide
tool-shaped design (`rast`, `ko`, `bakta` as columns) that does not generalise to six tools.
`evidence`: 2,504,043 rows reconciling **exactly** to the sum of the three annotation tables
(1,118,422 + 1,010,656 + 374,965), with **0 non-null scores and 0 non-null probabilities**, an
`evidence_type` that repeats the parent's `tool`, and only 2,388,346 distinct `evidence_id` values —
so a 1:1 satellite that is not even a key.
**If you disagree:** keeping `evidence` costs a multi-billion-row join at ATB scale to retrieve a
column that could sit on the parent row; keeping `annotation` means specifying an
`organism:locus` → `{genome_id}_{N}` translation nobody has written, for a table whose consensus rule
(`category`, `verdict`, `u_gene`, `n_hypotheses`) is undocumented. If the *consensus* function is
wanted, that is a new properly-keyed table and its own PRD.
**Confidence:** high on `evidence` — the reconciliation is exact across all six tools.
**Medium on `annotation`**: I proved it does not join, not that nobody wants what it holds.

### Q13. How does the lake consume supersession? -- DECIDED: resolve the FASTA chain and the genome-JSON chain SEPARATELY and transitively, from the DELIVERED snapshot only, storing observed and resolved for each — FASTA into `assembly.fasta_reference`/`_observed`, genome JSON into `genome_alias` under `kbdl_object_id`.
**Blast radius:** MEDIUM
**Why:** The first draft said "resolve `genome_object_id` and store the new id in `fasta_reference`",
which conflates two object types: `fasta_reference` points at a FASTA object, `genome_object_id` at a
genome JSON object, and they supersede **independently**. Resolving one into the other's column points
at the wrong kind of object, and the measured data could never have caught it — 0 live supersessions
touch either chain. Resolving only the FASTA would also miss changed feature JSON. Storing observed
*and* resolved costs one column per chain and keeps the snapshot reproducible, which is the point of a
provenance-carrying row.
**Resolution reads the delivered snapshot's table, not a live one**, so a snapshot determines its
rows; that also removes the first draft's gotcha about two loads of one snapshot disagreeing.
Rule B is equated with `fasta_object_id` **only where the pinned object-format contract proves that
equality** — if a storage id hashes compressed bytes, equating it with sha256 of canonical
uncompressed FASTA is false.
**If you disagree:** resolve-at-read moves `object_supersedes` into the query path, so the lake cannot
answer without KBDL's SQLite, defeating the namespace.
**Confidence:** medium, specifically because **this path has zero live instances**: 0 of 4,865 genomes
match `object_supersedes.old_object_id` on either id, and 0 chains exceed depth 1. All 28,051
supersedes rows are `atb-recompression-v1` against archive members. The transitive resolver, the cycle
guard and the type check are built on specification; the first real supersession is their first test.
If session `793ad471`'s general obsolete-id resolution rail ships, **call it** rather than
re-implementing the walk.

### Q14. What does "every row carries ingest provenance" mean concretely? -- DECIDED: the six-column tail `store_id, snapshot_id, snapshot_seq, load_batch_id, observed_at, loaded_at` on every table, with `store_id` never derived from a filesystem path.
**Blast radius:** MEDIUM
**Why:** `fitness` carries only `created_at`, clustered at two instants — which tells you a bulk load
happened and nothing else: not which store, not which snapshot, not whether a row is from a superseded
load. `council-record-lakehouse-v1` paid for the triple and for the path rule specifically: a store
that moves off `/scratch` must keep its id or every row it has published becomes unjoinable.
`observed_at` and `loaded_at` are separate so reloading an old snapshot does not make old facts look
new. Q1 makes `(store_id, snapshot_id)` load-bearing rather than decorative.
**If you disagree:** dropping to the clearinghouse's lighter `ingest_batch_id` + `observed_at` saves
four columns per row and makes Q1's snapshot model unimplementable.
**Confidence:** high.

### Q15. How is "the current build" and "is this published" represented? -- DECIDED: as append-only IMMUTABLE DECLARATIONS (`genome_build_currency`, `genome_disposition`) plus a deterministic projection. There is NO mutable column anywhere in the namespace.
**Blast radius:** MEDIUM
**Why:** The first draft had `genome_build.is_current BOOLEAN`, mutated by the loader, and it was
unimplementable as specified: no rule chose the winner, no behaviour was defined for loading an older
snapshot after a newer one, nothing said which physical snapshot-version row got mutated, and
`BerdlCapability.load` (`capability.py:458`) exposes append and overwrite rather than a pointer
transaction. Arbitrary last-load-wins would promote an old build; clearing the pointer before the new
batch reconciled would publish an incomplete build. Declarations plus a projection need no mutation:
current build is the greatest `declared_at` per `genome_id` with a deterministic `declaration_id`
tie-break, falling back to the selected snapshot's `source_designated_current`; effective disposition
is the same rule. `genome_disposition` had the same defect — multiple append-only declarations with no
precedence rule, so two rows could say `published` and `excluded` simultaneously with no way to choose.
**The loader creates no disposition declaration**, and absence remains unpublished.
**If you disagree:** keeping a mutable flag requires an executable atomic update mechanism,
concurrency control and publication ordering that the sanctioned rail does not provide — and it
removes the ability to ask "what did we consider current last Tuesday".
**Confidence:** high — this replaces something that could not have been built.

### Q16. How do consumers select science safely? -- DECIDED: three supported projections — `published_current_genome`, `published_current_feature`, `published_current_gene` — and raw tables are archival inputs, not a query surface. `published_current_gene` EXCLUDES `repeat_region`; `published_current_feature` includes it.
**Blast radius:** MEDIUM
**Why:** The first draft's protections were *admonitions* — "nothing downstream may treat existence as
a result", "CRISPR arrays must be excluded from anything that means gene". One could implement every
base table exactly and still fail the user stories: partial rows stay visible, a query straddles two
snapshot versions, an excluded genome or a CRISPR array enters a gene count. A flag does not filter
`SELECT ... FROM gene`. Each projection applies snapshot selection, reconciliation, effective
disposition and current build **together, before any join** — which is the only place those four can
be applied consistently.
**If you disagree:** leaving consumers to compose the four filters themselves guarantees that some
consumer gets one wrong, and the wrong answer looks like data rather than a mistake.
**Confidence:** high on the need. **Medium on the mechanism**: if SQL views cannot be created through
the sanctioned rail, these become materialized tables rebuilt per delivery, which costs storage and a
rebuild step. I did not verify which of the two the rail supports — the smoke task settles it.

### Q17. What does the load ledger record, and when? -- DECIDED: exactly ONE TERMINAL RECEIPT per `(delivery, table)`, written only after reconciliation, with `rows_expected` derived from the DELIVERED BUNDLE; retry and in-progress state live OUTSIDE the lake.
**Blast radius:** MEDIUM
**Why:** Three defects in the first draft. Its `load_ledger` repeated four provenance-tail columns as
business columns — **invalid DDL**. The feature loader was told to record per-qualifier counts into a
ledger with nowhere to put them. And "`reconciled = FALSE` on any row makes the batch unpublishable"
plus append-only means a transient failure row **permanently poisons its batch**, because append-only
data cannot retract it. Terminal-receipt-only fixes that: an unreconciled table simply has no receipt,
which is already "not publishable", and retry progress lives in the producer-side durable inventory.
`rows_expected` must come from the bundle because a ledger comparing a number to itself reconciles
trivially. Completion covers the declared inventory **including zero-row tables**, excludes the ledger
reconciling itself (recursive), and excludes independently-authored curation — otherwise a missing
curator blocks archival forever. Reuse council's ambiguous-commit recovery (`council_archive.py:29`,
`:748`, `:942`) and single-writer locking bound to `(delivery digest, namespace, schema version)`.
**If you disagree:** per-shard progress rows in the lake make the ledger a mutable table, which
contradicts Q1 and reintroduces the poisoning.
**Confidence:** high.

### Q18. How does each tool's output reach the annotation tables? -- DECIDED: one PINNED source-to-target mapping and one fixture PER TOOL is a build deliverable; `{}` is used only for a proven default invocation; the existing per-tool parameter validator is enforced; records with unrecoverable required metadata are QUARANTINED, and a quarantined required field makes the batch unpublishable.
**Blast radius:** MEDIUM
**Why:** Tool outputs are not interchangeable schemas. Nothing in the destination columns says which
number is `score`, which text defines a `function_id`, or how a version and parameter set are
extracted — and six tools (`BAKTA`, `PROKKA`, `DRAM2`, `RAST`, `KOFAMSCAN`, `TRANSYT`) differ in all
three. Substituting `{}` for unavailable parameters **invents reproducibility**, and it is
specifically wrong here: `clearinghouse_parameters.py:184` shows TRANSYT requires a `taxonomy_id` and
rejects an unidentifiable default. Quarantine rather than whole-delivery failure because a legacy
annotation missing a version should not block a corpus — but it must not silently become a published
result either, which is why a quarantined required field blocks publication.
**If you disagree:** whole-delivery failure is simpler and stalls on the first legacy record;
silently defaulting is what produced `fitness`'s 100%-NULL `source_version`.
**Confidence:** medium — the mapping is a build deliverable rather than something I could specify
per-tool from here, and that is an honest deferral of detail, not of a decision.

### Q19. Who writes the tables, and what exactly arrives? -- DECIDED: an in-pod loader reading a delivered snapshot PLUS ITS FULL OBJECT CLOSURE, with the delivery contract pinned in this PRD rather than cited.
**Blast radius:** MEDIUM
**Why:** The lakehouse write path runs in-pod; KBDL on poplar reaches the lake over a KBase token that
expires on a ~27-day cycle and has already lapsed silently mid-build; the delivery rail exists and is
specified. The first draft then described the delivery by *reference* to another PRD, and the
adversary stalled on exactly that: SQLite registry rows are **not** the genome objects containing
features, so a SQLite-only delivery yields thousands of genome headers and zero features, or forces
the loader back onto a credentialed path. Hence the explicit three-part contract in the body —
backup, full reachable object closure including supersession targets, and a digest-bearing manifest.
**If you disagree:** a KBDL push gets the job system's retry and leasing for free but puts the
expiring credential on every load's critical path and keeps GAA in the dependency graph.
**Confidence:** high on the direction; **medium on the closure's size**, which I have not measured —
4,865 genomes' JSON plus FASTA is the first real delivery and its byte count is unknown.

### Q20. What is retired? -- DECIDED: NO `fitness` or `inner_loop` production table or reader endpoint changes here. In KBDL, retire only the unused `KBDLStoreLoad` SUBMISSION path with an explicit "unsupported; use snapshot delivery" error, PRESERVE historical job deserialization, and remove the GAA imports.
**Blast radius:** MEDIUM
**Why:** The first draft said this namespace "supersedes the genome/gene/annotation/fitness subset of
`inner_loop`" and promised user story 21 a "stated window and compatibility story" — with no date, no
table list, no compatibility mapping and no reader inventory. That is not a retirement, it is an
intention, and acting on it could break modelling tables that reference `inner_loop`: measured, it
holds **39** `v2_*` tables of which roughly twenty have no `fitness` equivalent at all (`v2_models`,
`v2_model_reactions`, `v2_model_fva`, `v2_reactions`, `v2_reaction_fva`, `v2_mappings`,
`v2_pangenome_neighbors`, `v2_protein_families`, `v2_fitness_simulations`, `v2_council_candidates`, …).
So this delivery is purely additive, and retirement moves to a separate reader-inventory migration
PRD. On the KBDL side the retirement is real but narrow: `KBDLStoreLoad` has run **zero times**
(confirmed by the absence of any `jobs` row of any state, against 4,891 `KBDLBuildGenome` and 3,341
`KBDLGenomeAnnotation` completions), so there is no incumbent behaviour — but removing the type
outright would break deserialization of historical job rows, so the submission path rejects while the
record remains readable.
**If you disagree:** retiring `inner_loop` tables now requires the exact table set, its dependents,
compatibility queries and dates — a second PRD of comparable size, which is not what the agenda asked
for.
**Confidence:** high on the KBDL half. **Low on the lake half's sequencing** — I measured contents,
not consumers, so I cannot say how long a window anyone needs. That is why the window is no longer
promised here at all.

### Q21. Is "exists in the namespace" the same as "is a result"? -- DECIDED: no. `genome_disposition` declarations, and absence means NOT publishable.
**Blast radius:** MEDIUM
**Why:** `council-record-lakehouse-v1` paid for this with twenty-one jobs of which two cancelled
19,276-item runs looked authoritative by label and size. KBDL has its own instance already:
`object_supersedes` holds 28,051 rows, all `atb-recompression-v1`, which is exactly "present in the
archive, superseded as a result". Absence-means-unpublished rather than the reverse because the
failure directions are asymmetric: wrongly withholding a result is visible and fixable, wrongly
publishing a trial is neither.
**If you disagree:** dropping it makes every consumer re-derive curation from `load_batch_id`
heuristics, which is how a trial load becomes a finding.
**Confidence:** high.

### Q22. How do KBDL feature ids map to lake gene ids? -- DECIDED: `gene.gene_id` is the KBDL feature id VERBATIM (`{genome_id}_{N}` for CDS, `{genome_id}_nc{N}` for non-coding); `locus_id` is the upstream `source_id`, NULL for ab initio.
**Blast radius:** LOW
**Why:** Verbatim carriage means an id in the lake is pasteable into KBDL with no translation table to
go stale. The two independent counters are safe rather than dangerous: `_1` and `_nc1` are distinct
strings and cannot collide, and the 3,218 completed `KBDLGenomeAnnotation` jobs that reference CDS ids
can never accidentally match a non-coding id. **The counters are not dense** — a dropped feature
consumes no id number — so no consumer may assume density. `locus_id` is nullable because an ab initio
prediction has no upstream identifier (`CanonicalFeature.source_id` is `str | None` for that reason).
**Correction carried from Q6:** the handover brief described `genome_id` as "the truncated assembly
content hash". It is not — it is `prefix + decimal ordinal` (`unk1` … `unk4865`). The id *format* is
unaffected; the routing consequence is in Q6.
**If you disagree:** minting lake-local gene ids requires a persisted mapping table and makes every
cross-system question a join.
**Confidence:** high — read from `genome_normalize.py` at the two minting sites, and the format
measured from the live registry.

### Q23. Is overlap between features legal? -- DECIDED: yes. No coordinate-uniqueness constraint; the overlap tally persists in `genome_build_provenance` with the threshold it was produced under.
**Blast radius:** LOW
**Why:** `kbdl-add-non-coding-to-existing-genome-v1` deliberately does **not** drop a CDS overlapping
an added RNA, so existing CDS ids stay valid — which means gene rows for one genome **can** overlap,
and any uniqueness rule forbidding it would reject correct data. `cds_overlap_drop_fraction` is
carried beside the count because a count produced under a different threshold is not comparable, and
`NULL` is meaningful: it marks a genome built under the original any-overlap rule and must not be
defaulted.
**If you disagree:** enforcing non-overlap rejects every enriched genome at load.
**Confidence:** high. The adversary explicitly found no blocker here.

### Q24. What must be non-empty, and can the acceptance fixture come from production? -- DECIDED: the zero-join guard requires a non-empty selected genome/root, NOT every table; optional child tables may be zero-row when their derived expected count is zero. The mutant/WT pair is a TEST-ONLY fixture and this PRD does not repair KBDL's content-hash dedup.
**Blast radius:** LOW
**Why:** Two separate corrections. First, a blanket non-empty check would reject **correct**
deliveries: `gene_qualifier` is legitimately near-empty for ab initio genomes, a no-hit annotation set
is legitimately empty, and the four fitness tables have **no KBDL producer at all**. So the guard is
scoped to required relationships, a positive expected count with zero extracted rows fails, and key
non-nullness is checked only for required keys — while the guard's own test still runs over an empty
**required** fixture and asserts failure, which is what keeps the suite from being self-confirming.
Second, the acceptance pair (`SyringaeB728a` / `SyringaeB728a_mexBdelta`) lives in `fitness`, **not**
KBDL, and KBDL's registry dedupes exactly that shape on `content_hash` — so a KBDL-only loader
**cannot produce it**. A lake schema can permit two genome ids; it cannot recover two biological
identities already collapsed by its only upstream registry. The pair ships as a fixture with an
explicit test-only two-id mapping, and `fitness` UUIDs are **never** ingested into production as KBDL
G-numbers, which would be a lie about what a G-number means.
**If you disagree:** requiring production support for that pair now makes a KBDL registry identity
change a hard dependency of this PRD rather than a separate piece of work.
**Confidence:** high on the guard scoping; high on the fixture limitation, which is why it is stated
as a limitation rather than hidden in a test.

### Q25. What I could not decide and did not guess

Five external facts are unreconciled. None blocks the build.

1. **Who reads `fitness` and `inner_loop` today.** I measured contents, not consumers. This is why
   Q20 no longer promises a compatibility window at all: the honest position is that retirement needs
   a reader inventory I cannot obtain from h100, so this delivery changes nothing of theirs.
2. **What wrote `fitness`.** Every file-count pathology in the Research Report is a property of that
   writer, and it is not in KBUtilLib or KBDL — the rows carry `source='fitnessDB'` and `created_at`
   clustered at two instants, reading as bulk loads from outside our repos. Q6's remediation is
   specified against *our* write path. **If the intent is to rewrite `fitness` in place rather than
   supersede it, that writer must be found first, and this PRD does not do it.**
3. **How many genomes are to be enriched.** Blocked continuation 180, and Chris's. It changes no
   table shape, but it decides whether the first load is 4,865 rows or 2,000,000 — i.e. whether Q6's
   sharding is exercised at all on day one.
4. **The delivery's byte size.** Q19 pins *what* a delivery contains; I have not measured how large
   the object closure for 4,865 genomes actually is, and it bounds whether the rail is practical for
   the ATB corpus without chunking.
5. **What is in `kbaseincubator.genome_clearhouse`** — that spelling, a different namespace from
   `clearinghouse`, holding `genome_quality` and `skani_distances`. Plainly genome-adjacent, not
   opened. If it holds per-genome quality metrics they may belong in this namespace rather than beside
   it; neither claimed nor ruled out.

## Gotchas and Unintuitive Consequences

**A query that asks for "genes" gets CRISPR arrays unless it uses the projection.**
`repeat_region` is a `feature_type` value on `gene` and it is not a gene. `SELECT COUNT(*) FROM gene
WHERE genome_id = ?` is the wrong query and will not look wrong. This is why
`published_current_gene` excludes it and `published_current_feature` does not — but the raw table is
still there and still queryable.

**A rebuild grows the corpus without adding a genome.** Because gene identity is build-scoped,
re-annotating the existing 4,865 genomes under a second caller **doubles** `gene`, `gene_qualifier`,
`genome_build_provenance`, `genome_caller_run` and every gene-keyed annotation and fitness table —
while leaving `genome`, `assembly` and `contig` untouched. "Rows in `gene` divided by rows in
`genome`" is therefore not a mean gene count and will drift upward for reasons unrelated to biology.

**The same genome in two snapshots is two rows, and that is correct.** Snapshot versioning means row
counts grow with every delivery even when nothing changed upstream, unless the loader's
same-snapshot no-op is working. A sudden doubling of `gene` is the expected signature of a *new*
snapshot and of a *broken idempotence check* alike — only the `load_ledger` distinguishes them.

**Partitioning `gene` by `feature_type` makes one enormous partition and six small ones.** Intended,
since it prunes the CDS majority away from the non-coding driving case — but the CDS partition gets no
benefit from the partition spec at all, and its layout rests entirely on the writer-side sharding. If
the sharder is misconfigured, `gene` degrades to `fitness`'s current pathology and **the partition
spec will not reveal it**; only the layout rehearsal will.

**The sharder's routing key is a counter, so a prefix change is a cliff.** All 4,865 live ids share
the prefix `unk`. Routing parses `[A-Za-z_]+\d+` and fails loudly on anything else — which is correct
— but it means the day KBDL starts minting a second prefix, every ordinal collides across prefixes
unless routing keys on `(prefix, ordinal)`. The schema permits it; a naive implementation that parses
only the digits will silently interleave two genome populations.

**An empty table in the selected snapshot stays empty, and that will look like data loss.** It is the
deliberate consequence of selecting one snapshot before joining: no fallback to an older version of
that table. An operator seeing zero annotations for a genome that had them last week is seeing a
delivery that did not carry them, not a bug — and the only way to tell is the ledger's declared
inventory.

**Dropping `evidence` is not reversible by adding it back.** The scores it would hold are NULL in the
source today, so a reconstructed `evidence` table would be re-created empty. Nothing is lost, but
nobody should expect re-adding it to recover anything.

**`genome_alias` is fill-only, so a wrong alias is permanent.** No uniqueness, no delete,
deliberately — so an incorrect `kbdl_object_id` written by a buggy loader stays forever and readers
see two candidate object ids. The mitigation is the provenance tail: prefer the greatest
`snapshot_seq`. That is a **convention, not a constraint**, and it must be documented at the read
surface or it will not be followed. `clearinghouse.entity_alias` has the identical property and no
retraction either, which is the real reason Q11 does not project from it yet.

**The namespace is independently loadable but not independently useful.** Q7 puts sequences in the
clearinghouse, so a deployment that bootstraps `genome_companion` without it gets a namespace where
`protein_seq_hash` resolves to nothing. Every table loads and every integrity check passes; the
failure is a scientist getting no sequence back, which looks like a data gap rather than a missing
dependency.

**`source_version` being 100% NULL in `fitness` is the warning, not the exception.** A tool-version
column exists in the live namespace and nothing has ever populated it. This design adds
`tool_version`, `caller_version` and `parameter_set_hash`, all of which can be NULL just as easily.
Schema does not make provenance happen; the write-boundary rejections do, which is why they are
specified as refusals rather than defaults — and why Q18 chooses quarantine over a placeholder.

**Two genomes sharing one assembly means per-assembly aggregates double-count.**
`SELECT COUNT(*) FROM genome JOIN assembly USING (assembly_entity_hash)` returns 48 on the current
corpus, not 47. Any "per assembly" statistic must aggregate on `assembly`, not on the join. And since
Q3 makes `assembly` one row per *representation*, a reordered resubmission adds a row there too — so
`COUNT(*) FROM assembly` is not a count of distinct assemblies either; `COUNT(DISTINCT
assembly_entity_hash)` is.

**`build_id` changes on enrichment, which is the point and also a cost.** `structural_features_digest`
covers every feature, so adding non-coding features to a genome mints a **new build** rather than
amending the old one — which is what keeps the two gene sets distinguishable, and which also means
every annotation attached to the old build does **not** automatically apply to the new one. Carrying
annotations forward across an enrichment is a deliberate operation nobody has specified yet.

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
- **Repairing KBDL's content-hash deduplication** so that two biologically distinct genomes sharing
  one assembly can both exist as production G-numbers. The schema here permits two genome ids per
  assembly; the upstream registry does not currently supply them, and the acceptance pair is a
  test-only fixture because of it (Q24). That registry identity change is its own piece of work and
  is a prerequisite for *production* support of the mutant/wild-type case, not for this delivery.
- **Retiring `fitness` or `inner_loop`, and any compatibility window for them.** Q20 makes this
  delivery purely additive to both. Retirement needs a reader inventory this PRD does not have
  (Q25.1) and `fitness`'s writer identified (Q25.2), so it belongs to a separate migration PRD.
- **Carrying annotations forward across an enrichment.** Because `build_id` covers the structural
  feature set, enriching a genome mints a new build, and annotations attached to the old build do not
  automatically apply to the new one. Specifying that migration is deliberately left out.
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
