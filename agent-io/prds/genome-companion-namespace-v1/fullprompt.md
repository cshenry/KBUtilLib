# Genome companion namespace — an annotation companion to the clearinghouse

## Research Report

### Important findings

**1. The mechanism Chris asked for ALREADY EXISTS in the clearinghouse, and the first two drafts of
this PRD were duplicating it.** `clearinghouse_schema._CONTENT_TYPE_HEAD` is a per-entity-type
mapping whose whole purpose is "customize these tables as needed for each entity type". It already
holds, on `main`:

| content table | existing type-specific head |
|---|---|
| `genome_content` | `fasta_reference, taxon_id, assembly_accession, n_contigs, total_length, is_closed` |
| `protein_content` | `sequence, seq_length` |
| `gene_content` | `sequence, seq_length, protein_entity_hash` |
| `function_content` | `definition` |
| `ontology_term_content` | `term_id, name, namespace, definition` |

Every one of those composes onto a shared `_CONTENT_COMMON_TAIL` declared once. So "the genome/gene/
protein ID tables should carry length, sequence, contig counts" is **already the design** — it is
*under-populated*, not absent. The earlier drafts' `assembly`, `contig` and `genome` tables
re-declared `n_contigs`, `total_length`, `is_closed`, `fasta_reference`, the sequence hashes and the
assembly accession. That is the duplication this revision removes.

**2. THE SCHEMA WINDOW IS WIDE OPEN, AND IT IS THE REASON TO DO THIS NOW. Measured 2026-10-27:**

| table | rows | table | rows |
|---|---|---|---|
| `genome_entity` | **0** | `genome_content` | **0** |
| `gene_entity` | **0** | `gene_content` | **0** |
| `protein_entity` | **0** | `protein_content` | **0** |
| `ontology_term_entity` | **0** | `ontology_term_content` | **0** |
| `function_entity` | 47,341 | `function_content` | 47,341 |
| `genome_result` / `gene_result` / `protein_result` / `ontology_term_result` | **0** | `function_result` | 20,036 |
| `parameter_set` | 3 | | |

**Only the `function` type has rows.** Extending the genome, gene and protein content heads costs
nothing today. After the first genome load it is frozen: the clearinghouse is append-only, fill-only,
with no production delete, so a column not added now is a backfill over the whole corpus later. This
is the same closing-window argument that applies to the URI vocabulary, and it is one operator
command away from being permanent.

**3. A clearinghouse gene entity is a SEQUENCE, not a located feature — and that is the seam the
companion exists to fill.** `gene_entity`/`gene_content` are keyed on `entity_hash`, the content hash
of the sequence, with `stores_content=True`. The same gene sequence occurring in a thousand genomes
is **one** entity row. Location, strand, contig and locus tag are therefore not properties the
clearinghouse can hold: they are properties of an *occurrence* of that sequence in a particular
genome under a particular gene call. **That occurrence layer is what the companion namespace is
for**, and it is the one thing nothing else can hold.

**4. The same logic makes per-sequence tool annotation a CLEARINGHOUSE concern, and this is the
largest storage consequence in the design.** `<type>_result` already exists for exactly this —
`(entity_hash, entity_type, result_type, source, parameter_set_hash, result_type_version, payload,
observed_at, ingest_batch_id)`, partitioned on `source`. A BAKTA call on a protein sequence is a fact
about the *sequence*, so at ATB scale the same protein recurring across a thousand genomes needs
**one** result row per tool, not a thousand. Storing per-feature functional annotations in the
companion — as `fitness.function_annotation` does, and as the earlier drafts of this PRD did — would
multiply annotation rows by the average recurrence of a protein across the corpus. On a 2M-genome
corpus that is the difference between roughly 10^8 and 10^10 rows. **The companion must join to
annotations, not copy them.**

**5. A typed result-type registry already exists, so tool-assessed metrics have a proper home.**
`kbdl_service/clearinghouse/result_types.py` is backed by a `clearinghouse_result_types` table
carrying `(tag, version, schema_json, entity_types)`. So a result type can be *declared with a JSON
schema* rather than being an opaque payload. That is what makes it defensible to put CheckM2 and
skani output in `genome_result` instead of in `genome_content` — see Q3.

**6. CheckM2 gives thirteen fields, and they split cleanly along the intrinsic/assessed line.**
`CheckM2Quality` carries `completeness`, `contamination`, `coding_density`, `gc_content`,
`average_gene_length`, `contig_n50`, `genome_size`, `total_coding_sequences`, `total_contigs`,
`max_contig_length`, `translation_table_used`, `completeness_model_used` and `additional_notes`
(where the literal string `"None"` is normalised to `None`, and any other value is preserved verbatim
as a soft low-confidence flag). Of those, `gc_content`, `contig_n50`, `max_contig_length`,
`genome_size` and `total_contigs` are computable from the assembly bytes alone by anyone;
`completeness`, `contamination`, `coding_density`, `total_coding_sequences`,
`completeness_model_used` and `additional_notes` are a *model's assessment* and are meaningless
without the tool, its version and its model. That is the split Q3 acts on.

**7. ALIASES ARE A CLEARINGHOUSE CONCERN AND THE TABLE IS `genome_alias` -- one of FIVE per-type
alias tables. Corrected 2026-11-22; an earlier revision of this section was wrong twice.**
It said `entity_alias` (a single generic table) and that it had "landed on main". Both were wrong.
Chris REVERSED that PRD's central decision: there is no generic `entity_alias`. There are five
per-type tables -- `genome_alias`, `protein_alias`, `gene_alias`, `function_alias`,
`ontology_term_alias` -- plus an `all_alias` union view, with **`alias` as the FOURTH KIND** beside
entity/content/result. His reasoning is the same argument this namespace is built on: there are far
more proteins than genomes, so a shared alias table makes genome-alias reads pay protein scale.
**Verified on `main` 2026-11-22**: `clearinghouse_schema.py` now declares
`_KINDS = ("entity", "content", "result", "alias")`, twenty-one configs,
`_ALIAS_PARTITION_BY = "alias_source"`, a closed `alias_source` vocabulary with legal values
**per entity type**, and an invertible-source set. So the companion writes
**`clearinghouse.genome_alias`**, and its own alias table is deleted rather than redesigned.
**BUT THE TABLE DOES NOT EXIST IN THE LIVE LAKE YET** -- measured 2026-11-22, the namespace holds
19 tables and **no alias table of any name**. The code landed; the bootstrap has not run. So the
loader's alias write has no target until someone bootstraps the alias kind, and that is a
precondition this PRD does not own. Its author also sent
a defect report that applies to the earlier draft's `genome_alias`: **`ncbi_taxid` is a MEMBERSHIP
relation, not an invertible alias** — a taxon names many assemblies, so a scalar reverse lookup on it
legitimately matches every genome in the taxon and would raise a spurious ambiguity error. Deleting
`genome_alias` resolves that by removal rather than by patching a vocabulary.

**8. Carried forward and still true — the `fitness` scheme's measured defects.** Unchanged from the
previous round and still the reason not to copy it wholesale: `fitness` is **completely
unpartitioned** and its file layout is pathological at 48 genomes (`genome` 48 rows in **48 files**,
`function_annotation` 1,118,422 rows in **2,880 files**, `propagated_fitness` 14,076,257 rows in
2,880 files); `fitness.annotation` is **100% orphan** (all 272,076 rows fail to join `gene.gene_id`);
`fitness.evidence` is a 1:1 satellite with **0 non-null scores** and non-unique ids;
`source_version` is **NULL in 100%** of 2.1M annotation rows; `fitness.sequence`, `gene_alias`,
`gene_function` and `meta` are **all 0 rows**; and the live `assembly_hash` follows **rule A**
(`standardizers.entity_hash`), confirmed by recomputing both rules over three real assemblies of 1,
16 and 7 contigs fetched from KBase workspace 262939 — A matched all three, B none.

**9. `genome_id` is a COUNTER, not a hash** — measured, and both the earlier draft and the handover
brief said otherwise. All 4,865 live KBDL ids are `unk1` … `unk4865`: one prefix plus a contiguous
decimal ordinal. This mattered enormously to the previous design, whose sharder routed on it. **Under
this revision it matters much less**, because the companion is keyed on `genome_entity_hash` — a real
64-hex content hash — and the G-number becomes an *alias* in `genome_alias`, not a routing key.
Recording it because it is the reason a whole section of the previous draft was wrong.

**10. KBDL's local policy says `GENOME: stores_content=False`, and that stays true and compatible.**
`kbdl_service/clearinghouse/policy.py` sets `stores_content=False` for genome because assemblies are
megabytes, and `True` for protein, gene DNA, function and ontology term. The lake's `genome_content`
holds *metadata plus a `fasta_reference` pointer*, never the bytes — so enriching that metadata does
not contradict the policy. The policy governs **inlining the assembly**, not describing it.

### Links to full research output

- [External prior-art scan](research/external-prior-art-scan.md) — Iceberg partitioning and sizing at
  10^9 rows, genome-annotation data-model prior art (RefSeq, Ensembl, Chado, GFF3/INSDC, GA4GH
  refget/seqcol), identifier carriage, negative-result provenance, lakehouse deprecation.
  **Its most consequential finding (F3.3) is load-bearing here**: nobody merges competing structural
  annotations, and `(assembly, locus_tag)` is not a key — measured at ~16% disagreement between two
  annotations of one identical assembly.
- [Confront round 1 stall report](research/confront-round-1-stall-report.md) — 18 stall points
  against the previous design. Several are **obsoleted** by this revision rather than folded; the
  Revision Log says which.
- [Live lake measurement](research/live-lake-measurement.md) — every `fitness` and KBDL number quoted
  above with the query that produced it, plus the hash-rule recomputation recipe.
- [Confront round 2 stall report](research/confront-round-2-stall-report.md) — 19 stall points
  against THIS revision, taskplan embedded; all folded (Round 3). Its headline: the task graph did
  not produce an end-to-end loader, and the request/ledger model could not publish a re-request or
  recover a partial write.

**Re-measured 2026-11-22 23:43 CST from primary-laptop** (`KBBERDLUtils`, same endpoint):
`clearinghouse` holds 19 tables and **no alias table**; `COUNT(*)` returns a real **0**
(`success=true`, not an empty result set) on `genome_content`, `genome_entity`, `genome_result`,
`gene_content`, `gene_entity`, `protein_content`, `protein_entity`; `function_content` is 79,972.
One transient 503 succeeded on retry, which is the likeliest explanation of the empty result set
seen earlier the same day. Q2's window is confirmed open. A laptop trap found on the way: a stale
token stored under KBUtilLib's `berdl` namespace in `~/.tokens` outranks `~/.kbase/token` and makes
every BERDL call return 401 "Invalid token" even when the KBase token is fresh.

**`kbaseincubator.genome_clearhouse` is partly identified.** `clearinghouse_schema.py` names it
`SOURCE_GENOME_CLEARHOUSE_NAMESPACE`: the populated genome SOURCE the clearinghouse bootstrap reads
(~5.8M genomes in its `genome_quality`, per the operator runbook). Whether that `genome_quality`
duplicates `genome_quality@1` is still unmeasured (Q20).

### Quality report

**What was searched.** The live BER Data Lakehouse from h100 on 2026-10-25 and again on 2026-10-27:
all 19 `kbaseincubator.fitness` tables for columns and row counts, five for `SHOW CREATE TABLE` and
`$partitions`, eleven integrity and vocabulary queries, the `inner_loop` table list, the
123-namespace catalogue, and **all 19 `kbaseincubator.clearinghouse` tables for row counts** (the
window measurement above). The live KBDL SQLite store read-only for registry counts, job states,
supersession integrity and the `genome_id` format. KBUtilLib `main` source:
`clearinghouse_schema.py` (the content heads, entity/result/tail column tuples, partition maps),
`clearinghouse_bootstrap_adapter.py`, `clearinghouse_shard.py`, `clearinghouse_capability.py`,
`capability.py`, and the landed `clearinghouse-entity-alias-v1` PRD. KBDL source:
`schemas/build_genome.py` in full, `schemas/checkm2.py`, `genome_normalize.py`,
`genome_registry.py`, `clearinghouse/policy.py`, `clearinghouse/result_types.py`,
`job_types/store_load.py`. Three real assemblies fetched from KBase workspace 262939 and both hash
rules recomputed over the real bytes.

**What was NOT searched, and why.**
- **Whether `genome_result`'s `payload STRING` is efficiently queryable in this Trino deployment.**
  Q3 puts CheckM2 and skani there, which makes "select genomes with completeness > 95" a JSON
  extraction rather than a column scan. I did not measure what that costs here, and it is the main
  practical objection to Q3 — which is why Q3 carries a flattening projection and why its blast
  radius is MEDIUM rather than LOW.
- **`fitness`'s writer**, still unidentified. The file-count pathology is a property of it; the rows
  carry `source='fitnessDB'` and `created_at` clustered at two instants, reading as bulk loads from
  outside our repos. This revision supersedes `fitness` rather than rewriting it, so the gap no
  longer blocks anything here.
- **Who reads `fitness` or `inner_loop`.** Unchanged, and still why this delivery changes neither.
- **`kbaseincubator.genome_clearhouse`** (that spelling — `genome_quality`, `skani_distances`). Its
  role is now known (the bootstrap's genome source, see findings) but its `genome_quality` columns
  were still not read: if they match `genome_quality@1`, Q3's result type may duplicate it. Named in
  Q20 as unresolved.
- **The per-user instancing model.** Chris explicitly deferred it ("let's not worry about that yet").
  Q5 makes the namespace a parameter so instancing is possible without a schema change, and designs
  no ACL, quota or cross-instance query.

**Confidence.** High on everything measured — all numbers above are live reads with recorded
queries. High on the clearinghouse/companion split, which is now grounded in the content heads and
`<type>_result` actually existing rather than in an argument. Medium on Q3's placement of
tool-assessed metrics and on Q7's annotation-by-join read ergonomics, both of which trade query
convenience for non-duplication and are the two most likely to come back. **This revision HAS now
been attacked** (confront round 2, Round 3 in the Revision Log), which spends the last of the
three-round budget: Chris's review edits will be the first text in this PRD no adversary has read.

## Revision Log

**Round 3 — 2026-11-22 (confront round 2 FOLDED; ownership moved to Jane).** `task-39f82bc7`
(codex, h100) attacked revision 2 with the taskplan embedded and returned 19 stall points; all 19
are kept and folded, two with a narrower decision than the adversary proposed (#9, #11 — see Q22 and
Q7). The shape changes: SQL views become a hard prerequisite (no materialized fallback); the
delivery gets a producer task, a consumer task and a pinned KBDL-to-KBUtilLib contract (Q21);
`request_genome_membership` replaces "a re-request is a no-op" as the completeness mechanism;
`load_request.status` and `protein_content.is_fragment` are removed; `function_call@1` is declared;
and the existing empty content tables migrate by additive `ALTER TABLE`, never by drop. New
`## Acceptance Criteria` at the end.

**Round 2b — 2026-11-22 (factual correction, no decision moved).** Two statements in round 2 were
wrong and are fixed: the clearinghouse alias table is **`genome_alias`**, one of five per-type
tables (Chris reversed that PRD's generic-table decision), not a single `entity_alias`; and it had
**not** "landed on main" at the time -- what had landed was that PRD's document bundle. As of
2026-11-22 the CODE is on `main` (21 configs, `alias` as a fourth kind) but the tables are **not in
the live lake** (19 tables, no alias table), which is now recorded as a hard precondition on the
loader task. The design conclusion is unchanged and in fact strengthened: per-type is the shape this
namespace would have wanted anyway. Also re-measured: `function_content` has grown 47,341 -> 79,972,
and the genome/gene/protein content heads are **still unextended on `main`**, so Q2's window is
still open.

**Round 2 — 2026-10-27 (REDESIGN, at Chris's direction).** Chris: *"I want to change the plan in this
PRD dramatically to better reflect that this is meant to be a companion to the clearinghouse… we
should not replicate data that is in the clearinghouse."* The namespace is now an **annotation and
occurrence companion**, not a parallel genome store.

- **Deleted outright**, because the clearinghouse holds them: `assembly`, `genome`, `genome_alias`,
  `function`, `ontology_term`. Genome and assembly metadata move into
  `clearinghouse.genome_content`; aliases move to `clearinghouse.genome_alias`;
  function and ontology-term identity are already `function_entity`/`ontology_term_entity`.
- **Clearinghouse content heads EXTENDED per entity type** (Q2), which is the mechanism Chris
  described and which already existed. The window is open: genome, gene and protein tables are all
  **0 rows** today.
- **Per-sequence tool annotation moves to `<type>_result`** (Q7) — the single largest change, worth
  roughly two orders of magnitude of annotation rows at ATB scale.
- **The companion keeps only what nothing else can hold**: the feature-occurrence layer, caller
  provenance, consensus/curation, and experimental fitness.
- **Loading is now SELECTIVE** (Q4): a load is a *request* naming genomes, not a snapshot of a whole
  store. Chris: *"not EVERY genome will be loaded… just the ones we call the load function on."*
- **The namespace is a parameter** (Q5), so a central public instance and later per-user instances
  need no schema change.
- **Confront round 1 is PARTLY OBSOLETED, not all folded.** Still live and folded: stalls 4, 5, 7, 9,
  10, 11, 12, 15, 17, 18 and 19's delivery-closure core. **Obsoleted by the redesign**: stall 3
  (`assembly` is gone — representation multiplicity is now the clearinghouse's `entity_hash` vs
  `content_hash` concern), stall 13's routing half (the sharder no longer routes on a G-number
  counter; the companion keys on a real hash), and stall 16's projection list (rewritten around the
  new tables). **Stall 2 survives in a changed form** (Q6): immutability is now per-request, not
  per-store-snapshot.
- **`genome_id` is a counter, not a hash** — measured during round 1's fold. It now matters much less,
  since the companion keys on `genome_entity_hash`.

**Round 1 confront — 2026-10-25 (FOLDED).** `task-16879cc1` (codex/GPT-5) returned 18 stall points,
all folded; three reshaped that design (snapshot versioning, assembly representation, the sharder's
entity-dedup data-loss risk). The mutable `is_current` column was removed, read projections replaced
admonitions, and the ledger became terminal-receipt-only. Much of that survives here.

**Round 0b — 2026-10-25 (research fold).** External scan folded. Build scoping added after the scan
found that `gene_id` alone is not a key.

**Round 0 — 2026-10-25 (draft).** First draft, single-pass, from the fourteen-shortcoming agenda.

## Problem Statement

KBDL has built 4,865 genomes and is pointed at a corpus of roughly two million. None of what it knows
about them is queryable: a built genome is a JSON object in an object store, addressed by a hash. The
job type that was supposed to make it queryable, `KBDLStoreLoad`, has never run.

Meanwhile the BER Data Lakehouse already has a **clearinghouse**: nineteen tables, five entity types,
content-addressed identity, per-entity content metadata, per-entity tool results, a parameter-set
registry, and — as of this week — an alias table. It is the right place for *what a thing is*. It
holds 47,341 functions and, for genomes, genes and proteins, **nothing at all yet**.

The gap is not a second genome store. It is that **nothing joins a genome to its features, and
nothing holds a feature's location**. A clearinghouse gene entity is a *sequence*; the same sequence
in a thousand genomes is one row, by design. Where that sequence sits — which genome, which contig,
which coordinates, which strand, called by which tool — is a property of an *occurrence*, and there is
no table for it. Without that layer you cannot ask "what are this genome's genes", which makes every
downstream question unanswerable no matter how good the entity store is.

The specific thing forcing the issue: `kbdl-add-non-coding-to-existing-genome-v1` is adding tRNA,
rRNA, tmRNA, ncRNA, misc_RNA and CRISPR features to existing genomes. Those features have no
destination, and `fitness.gene` has no column that can even say a gene is a tRNA.

## Solution

A **companion namespace** that holds the occurrence and annotation layer and nothing else, plus a
**targeted extension of the clearinghouse** to carry the per-entity metadata it is already shaped
for.

**Two changes to the clearinghouse** (both free today — those tables are empty):

1. **Richer per-entity-type content heads** — the existing `_CONTENT_TYPE_HEAD` mechanism, filled in:
   intrinsic assembly metrics on `genome_content`, and the small additions `gene_content` and
   `protein_content` need. Nothing tool-assessed goes here (Q3).
2. **Declared quality and taxonomy result types** on `genome_result`, so CheckM2 and skani output is
   first-class, schema-declared and carries its tool, version and parameter set — using the
   `clearinghouse_result_types` registry that already exists.

**The companion namespace, `<instance>.genome_companion`** — eleven tables in four layers, every row
scoped to a load request:

**Occurrence (the layer nothing else can hold)**
- `contig` — per-contig identity within a genome, which gene coordinates require.
- `genome_feature` — **the core table.** One row per feature occurrence: which genome, which contig,
  coordinates, strand, feature type, and the `gene_entity_hash` / `protein_entity_hash` that point
  into the clearinghouse for the sequence itself.
- `feature_qualifier` — the `extra="allow"` passthrough sidecar, canonical JSON.

**Build provenance (genome-scoped, not sequence-scoped)**
- `genome_build` — the annotation-run entity; gene identity is scoped by it.
- `genome_build_provenance` — the nine `NonCodingProvenance` fields.
- `genome_caller_run` — one row per caller that ran, **including zero-hit runs**.

**Curation and consensus (genuinely per-occurrence)**
- `feature_consensus` — the role `fitness.annotation`/`gene_function` tried to fill, properly keyed:
  a verdict across tools for one feature, referencing a clearinghouse `function_entity_hash`.
- `feature_curation` — human or agent assertions that override or annotate a tool result.

**Experimental (genome and condition specific, so nothing else can hold it either)**
- `condition`, `essentiality`, `experimental_fitness`, `propagated_fitness`.

**Plus operational, not science:** `load_request` (what was asked for), `load_ledger` (terminal
receipts), `genome_build_currency` and `genome_disposition` (immutable declarations).

**And three read projections** that reconstitute a `fitness`-shaped view by joining the companion to
the clearinghouse, so the convenient shape exists without the duplication:
`published_genome`, `published_feature`, `published_gene_annotation`.

The loader is the **KBDL load function, adapted**: it registers entities and content into the
clearinghouse, writes per-sequence tool results there, and writes the occurrence and provenance layer
into the companion — for the genomes a request names, and no others.

## User Stories

1. As a comparative genomicist, I want to ask how many loaded genomes carry zero tRNA genes, so that
   I can separate genuinely tRNA-poor lineages from assembly artifacts.
2. As a comparative genomicist, I want "the tRNA caller ran and found nothing" to be distinguishable
   from "no tRNA caller ever ran", so that a zero count is interpretable.
3. As a comparative genomicist, I want every non-coding feature of a genome with its type, product,
   anticodon, amino acid, ncRNA class and Rfam accession, so that I can compare RNA complements
   without re-running a caller.
4. As a comparative genomicist, I want a gene's sequence and its protein to come from the
   clearinghouse, so that the same sequence in a thousand genomes is one stored copy and cannot drift.
5. As a comparative genomicist, I want to select genomes by CheckM2 completeness and contamination,
   so that I can exclude poor assemblies from an analysis.
6. As a comparative genomicist, I want to know which tool and version produced a quality score, so
   that I do not compare CheckM2 1.0 numbers against CheckM2 1.1 numbers.
7. As a metabolic modeller, I want protein-coding features selectable in one predicate, so that no
   RNA gene and no CRISPR array can reach model reconstruction.
8. As an annotation methodologist, I want a tool's annotation of a protein stored **once per
   sequence**, so that the corpus does not hold a thousand copies of the same BAKTA call.
9. As an annotation methodologist, I want annotations from different tools about one feature to
   coexist and be comparable, so that adding a seventh tool is a load and not a migration.
10. As an annotation methodologist, I want a consensus verdict across tools recorded per feature with
    the rule that produced it, so that `fitness.annotation`'s undocumented verdict is not repeated.
11. As a curator, I want to assert a correction over a tool result without deleting it, so that the
    evidence and the correction are both inspectable.
12. As an operator, I want to load a named set of genomes, so that the namespace holds what we chose
    to put in it rather than everything KBDL has ever built.
13. As an operator, I want a second load of the same genome to be a no-op unless its content changed,
    so that a retry is safe.
14. As an operator, I want a load declared complete only when every table it claimed reconciles, so
    that a half-written request is never published.
15. As an operator, I want one supported way to select published, current, complete science, so that
    I cannot straddle two loads or count a CRISPR array as a gene.
16. As a data steward, I want a genome's several identifiers in the clearinghouse alias table, so
    that there is exactly one place identifiers live.
17. As a data steward, I want the collapse of two genomes with identical assemblies into one entity
    to be DOCUMENTED, TESTED and REVERSIBLE (Q9's `genome_instance` escape hatch), so that a knockout
    mutant and its wild-type parent are a known limitation rather than a silent merge. This revision
    does not make them distinguishable.
18. As a data steward, I want "exists in the namespace" to differ from "is a result", so that trial
    loads do not silently become findings.
19. As a platform engineer, I want the namespace name to be a parameter, so that a public instance
    and later per-user instances need no schema change.
20. As a platform engineer, I want the clearinghouse extended while its genome, gene and protein
    tables are still empty, so that the columns are free rather than a corpus-wide backfill.
21. As a KBDL maintainer, I want the existing GAA-free clearinghouse module extended rather than
    `store_load` revived, so that GAA's retirement stops blocking the lake.
22. As a `fitness` or `inner_loop` reader, I want this delivery to change nothing of mine, so that my
    queries keep working while retirement is designed separately.
23. As a reviewer, I want every judgement call to carry a decision and a blast radius, so that I can
    reverse any one of them by editing one line.

## Implementation Decisions

### The dividing line, stated once

Everything below follows from one rule, and when in doubt apply it rather than this document's
table list:

> **If the fact is true of a SEQUENCE or of an IDENTITY, it belongs to the clearinghouse.
> If it is true of an OCCURRENCE — this sequence, in this genome, at this place, under this gene
> call — it belongs to the companion. If it is true of a GENOME-AND-CONDITION, it belongs to the
> companion.**

| fact | lives in | why |
|---|---|---|
| this genome's assembly has 47 contigs and 4.9 Mb | `clearinghouse.genome_content` | intrinsic to the bytes |
| this genome is 98.7% complete per CheckM2 1.0.2 | `clearinghouse.genome_result` | a tool's assessment, needs version + parameters |
| this protein's sequence and length | `clearinghouse.protein_content` | intrinsic to the bytes |
| BAKTA 1.9 calls this protein a LysR-family regulator | `clearinghouse.protein_result` | a fact about the sequence; one row however many genomes carry it |
| this genome's several identifiers | `clearinghouse.genome_alias` | identity |
| this function's definition | `clearinghouse.function_content` | identity + content |
| gene `unk12_84` sits on contig NC_007626 at 11..1336, `+` strand | **companion** `genome_feature` | an occurrence |
| this genome's gene set was called by Bakta 1.9 against DB 6.0-full | **companion** `genome_build_provenance` | genome-scoped provenance |
| the tRNA caller ran on this genome and found nothing | **companion** `genome_caller_run` | genome-scoped, and a negative |
| three tools agree on this feature's function, one dissents | **companion** `feature_consensus` | per occurrence, per build |
| this gene is essential in minimal medium | **companion** `essentiality` | genome and condition |

**What this rule removes from the previous drafts**: `assembly`, `genome`, `genome_alias`,
`function`, `ontology_term` and every per-feature functional-annotation table. All five of the first
set are clearinghouse concerns; the last is finding 4's two-orders-of-magnitude duplication.

### Change 1 — extend the clearinghouse content heads

`_CONTENT_TYPE_HEAD` is the existing per-entity-type mechanism. Add intrinsic metrics only:

```python
"genome": (                       # EXISTING, unchanged
    ("fasta_reference", "STRING"),
    ("taxon_id", "STRING"),
    ("assembly_accession", "STRING"),
    ("n_contigs", "INT"),
    ("total_length", "BIGINT"),
    ("is_closed", "BOOLEAN"),
                                  # ADDED -- all computable from the bytes alone
    ("gc_content", "DOUBLE"),
    ("contig_n50", "INT"),
    ("max_contig_length", "BIGINT"),
    ("min_contig_length", "BIGINT"),
    ("n_ambiguous_bases", "BIGINT"),
    ("assembly_level", "STRING"),     # 'complete'|'scaffold'|'contig'|NULL (rule below)
),
"protein": (
    ("sequence", "STRING"),           # EXISTING
    ("seq_length", "INT"),            # EXISTING
    ("md5", "STRING"),                # ADDED -- the MD5 refget/KBase-style digest
                                      # (is_fragment REMOVED in round 3: an occurrence fact)
),
"gene": (
    ("sequence", "STRING"),           # EXISTING
    ("seq_length", "INT"),            # EXISTING
    ("protein_entity_hash", "STRING"),# EXISTING
    ("md5", "STRING"),                # ADDED
    ("gc_content", "DOUBLE"),         # ADDED
),
```

Three rules govern what may be added here, and they are the point of the section:

1. **Intrinsic only.** A column belongs here if two independent implementations reading the same
   bytes must agree on its value. `gc_content` qualifies; `completeness` does not.
2. **No column whose meaning depends on a tool version.** If you would need to ask "which tool said
   that" to interpret the number, it is a result, not content.
3. **`assembly_level` is the one judgement call in the list** and is included deliberately: it is
   the vocabulary every external catalogue uses, so materialising it avoids every consumer
   re-deriving it differently. **Only three of the four catalogue values are derivable from the
   bytes, so only three are emitted** (round 3, stall 3): `chromosome` needs submitter knowledge and
   is never produced by derivation.

**Normative derivation rules** (pinned in the schema module with golden inputs and outputs; every
rule is over the canonicalized contigs, upper-cased):

| column | rule |
|---|---|
| `assembly_level` | `is_closed` TRUE → `complete`; `is_closed` FALSE and any contig contains a run of ≥10 consecutive `N` → `scaffold`; `is_closed` FALSE otherwise → `contig`; `is_closed` NULL → NULL |
| `gc_content` | (count of `G`+`C`) ÷ (count of `A`+`C`+`G`+`T`), as a DOUBLE in [0, 1]; ambiguity codes are excluded from both numerator and denominator; denominator 0 (empty or all-ambiguous) → NULL |
| `n_ambiguous_bases` | count of every base that is not `A`, `C`, `G` or `T` |
| `contig_n50` | sort contig lengths descending; the length of the first contig at which the running sum reaches ≥ 50% of `total_length` (no interpolation, no rounding); no contigs → NULL |
| `max_contig_length`, `min_contig_length` | over non-empty contigs; no contigs → NULL |
| gene `gc_content` | the same rule over the gene's DNA |
| `md5` | lowercase hex MD5 of the upper-cased sequence bytes as stored in `sequence` |

**`protein_content.is_fragment` is REMOVED** (round 3, stall 4). Start/stop completeness is a
property of a feature OCCURRENCE, not of the shared protein sequence — the same protein can occur as
partial in one genome and complete in another — so by the dividing line it lives on
`genome_feature.partial`, which already exists. Putting it on fill-only shared content would have
let the first occurrence loaded mislabel every later one.

**One alias source is added** (round 3, stall 10): `kbdl_genome_id` in the genome-only alias
vocabulary, holding the KBDL registry `genomes.genome_id` (`G1234`, a bare accession, or `unk` plus
an ordinal — the format varies by source, so its pattern is only "non-empty, no whitespace, at most
128 characters"). It is **store-scoped and NOT invertible**: the same string means different genomes
in different KBDL stores, so it is written only together with the request's `store_id` and is never
used as a global reverse lookup. This extends another PRD's closed vocabulary and is flagged in Q20.

**`table_configs()` returns 21 configs before and after this change.** Change 1 alters columns
inside existing configs; it adds and removes no table. The five alias configs keep their exact
schemas and partitions.

**The live tables already exist, empty, in the OLD shape** — so `CREATE TABLE IF NOT EXISTS` would
silently keep them unextended. The bootstrap therefore migrates each existing genome/gene/protein
content table by **additive `ALTER TABLE ... ADD COLUMNS`** (nothing is dropped: `is_fragment` was
never live), and only after re-checking `COUNT(*) = 0`
on that table in the same run. After migration it compares the **full live column list** against
`table_configs()` and refuses on any difference. If the sanctioned rail cannot run `ALTER TABLE`
(the phase-1 smoke measures this), the rollout **stops**: dropping and recreating a live table is
not this PRD's to decide, even an empty one.

`function_content` and `ontology_term_content` are **not touched** — they are the only tables with
rows (47,341), so changing them is the one expensive move available, and nothing here needs it.

### Change 2 — declared quality and taxonomy result types

CheckM2 and skani output goes to `clearinghouse.genome_result`, with result types **registered** in
the existing `clearinghouse_result_types` registry (`tag`, `version`, `schema_json`, `entity_types`)
so each payload has a declared JSON schema rather than being opaque:

| `result_type` | `source` | payload fields |
|---|---|---|
| `genome_quality@1` | `checkm2/<version>` | `completeness`, `contamination`, `coding_density`, `total_coding_sequences`, `average_gene_length`, `translation_table_used`, `completeness_model_used`, `additional_notes` |
| `genome_taxonomy@1` | `skani/<version>` or `gtdbtk/<version>` | `genus`, `species`, `lineage`, `ncbi_taxid`, `ani`, `reference`, `reason`, `source_database` |

| `function_call@1` | `<tool>/<version>` | `function_entity_hash`, `function_text`, `score`, `evidence` |

`function_call@1` is new in round 3 (stall 8): nothing in KBDL's clearinghouse code links a
per-sequence annotation to a `function_content` row, and without that link
`published_gene_annotation` has no join key. It is registered for `entity_types` `protein` and
`gene`. The loader registers each called function text as a `function_entity`/`function_content`
row (fill-only, so the 79,972 existing rows are untouched) **before** the results that reference it.

**The three registered schemas are normative** (JSON Schema draft 2020-12, `additionalProperties:
false`, published in the KBDL result-type registry with `tag` = the name before `@` and `version`
= the integer after it, as a string):

| type | required | optional (nullable) | bounds |
|---|---|---|---|
| `genome_quality@1` | `completeness` number, `contamination` number, `completeness_model_used` string | `coding_density` number, `total_coding_sequences` integer, `average_gene_length` number, `translation_table_used` integer, `additional_notes` string | `completeness` and `contamination` in [0, 100]; `coding_density` in [0, 1]; counts ≥ 0 |
| `genome_taxonomy@1` | `source_database` string, `lineage` string | `genus`, `species`, `reference`, `reason` strings; `ncbi_taxid` string matching `^[1-9][0-9]*$`; `ani` number | `ani` in [0, 100] |
| `function_call@1` | `function_entity_hash` 64-hex string, `function_text` non-empty string | `score` number, `evidence` object | none |

Each schema ships with one canonical example payload that validates and one per required field that
does not. **There is no numeric tolerance anywhere**: the loader stores CheckM2's numbers as given
and computes its own intrinsic metrics independently; it never compares the two.

Both carry `source`, `parameter_set_hash` and `result_type_version` for free, because
`_RESULT_COLUMNS` already has them — which is the whole reason this is the right home. `source` is
already the partition key on the five `<type>_result` tables, so "all CheckM2 results" prunes.

**`additional_notes` keeps its upstream semantics**: the literal string `"None"` normalises to null,
and any other value is preserved verbatim as a soft low-confidence flag. Dropping that distinction
would silently convert "CheckM2 flagged a 25% disagreement between its completeness models" into "no
note".

**Taxonomy is a RESULT, not content, and that is a change in posture worth naming.** The earlier
drafts flattened `TaxonomyBlock`'s eight fields onto a companion `genome` table. A taxonomy call is a
tool's assessment with an ANI, a reference and a reason — re-running skani against a new database
legitimately produces a different answer, and both answers are facts. `genome_content.taxon_id`
(already present) stays as the *currently believed* NCBI taxon id for convenience; the assessment and
its provenance live in `genome_result`.

### Change 3 — the companion namespace

#### Instancing

The namespace is a **parameter**, not a constant:

```python
DEFAULT_COMPANION_NAMESPACE = "genome_companion"   # the central public instance
def table_configs(namespace: str = DEFAULT_COMPANION_NAMESPACE) -> list[dict]: ...
```

Every verb takes it; nothing hardcodes it. The central public instance is one value of the
parameter. **Per-user instances are explicitly out of scope** (Chris: *"let's not worry about that
yet"*) — no ACL model, no quota, no cross-instance query is designed here. What this decision buys is
that adding them later needs no schema change.

**The clearinghouse stays single and shared.** A companion row references `genome_entity_hash` and
`gene_entity_hash`, which are content hashes and therefore globally valid — so two companion
instances describing the same genome agree about what it is, without coordinating. That property is
why the split is worth having at all, and it would be lost if each instance held its own copy of
genome metadata.

#### The provenance tail

The last five columns of every companion table, declared once:

```
request_id STRING, store_id STRING, snapshot_id STRING, observed_at TIMESTAMP, loaded_at TIMESTAMP
```

`request_id` is the unit of completeness (Q4) and replaces the previous design's
`load_batch_id`/`snapshot_seq` pair as the thing the ledger reconciles. `store_id` and `snapshot_id`
still record **where the facts came from** — `store_id` is a declared, durable id and is **never
derived from a filesystem path**, because a store that moves off `/scratch` must keep its id or every
row it has published becomes unjoinable. `observed_at` is when the fact was true at the source;
`loaded_at` is when the row was written.

#### Table: `contig`

```
genome_entity_hash STRING, contig_id STRING, source_id STRING,
length BIGINT, gc_content DOUBLE, is_circular BOOLEAN,
<tail>
```
Logical key `(genome_entity_hash, contig_id)`.

`clearinghouse.genome_content` holds `n_contigs`, a count. Per-contig identity is not there and
cannot be: gene coordinates address a named contig, so without this table a `genome_feature` row's
`contig_id` dangles — which is precisely `fitness.gene.contig_id`'s existing defect (no contig table
exists in `fitness` at all).

#### Table: `genome_feature` — the core of the namespace

```
genome_entity_hash STRING,   -- FK -> clearinghouse.genome_entity
build_id STRING,             -- FK -> genome_build; scopes feature identity (Q8)
feature_id STRING,           -- KBDL feature id verbatim: {g}_{N} or {g}_nc{N}
contig_id STRING,            -- FK -> contig, within this genome
start BIGINT, "end" BIGINT, strand STRING,
feature_type STRING,         -- 'CDS'|'tRNA'|'rRNA'|'tmRNA'|'ncRNA'|'misc_RNA'|'repeat_region'
gene_entity_hash STRING,     -- FK -> clearinghouse.gene_entity (the DNA sequence)
protein_entity_hash STRING,  -- FK -> clearinghouse.protein_entity; NULL iff not CDS
locus_tag STRING,            -- the upstream source_id; NULL for an ab initio call
caller STRING, caller_version STRING,
product STRING, partial BOOLEAN,
amino_acid STRING, anticodon STRING, ncrna_class STRING, rfam STRING, rpt_family STRING,
<tail>
```
Logical key `(build_id, feature_id)`. Partitioned by `feature_type`.

**This table is the whole reason the namespace exists.** It is the join between a genome and the
sequences it contains, and it is the only place a coordinate lives. Note what it does **not** carry:
no sequence, no sequence length, no function, no ontology term, no score. Those are all reachable by
hash.

`protein_entity_hash IS NOT NULL` **iff** `feature_type = 'CDS'` — the lake-side expression of KBDL's
structural invariant that a tRNA must never pass as a protein-coding gene. `gene_entity_hash` is
always set: the DNA is the product of a non-coding gene, so a feature with no DNA carries no
information.

`feature_type` carries **seven** values: `CDS` plus `NonCodingType`'s six. `repeat_region` is a CRISPR
array and **is not an RNA gene**; the finer SO/GenBank subtypes (`snoRNA`, `SRP_RNA`, `RNase_P_RNA`, …)
are deliberately not members and fold to `ncRNA` with the raw subtype in `ncrna_class`, exactly as
`map_non_coding_type` does upstream.

The five RNA qualifier columns are nullable and sparse, and that is cheap: in Parquet with ZSTD an
all-null column is a run-length-encoded null bitmap, so the ~95% CDS majority pays almost nothing for
columns it does not use — and the alternative is a join on the largest table in the namespace to
retrieve the fields that *define* a tRNA.

#### Table: `feature_qualifier`

```
build_id STRING, feature_id STRING, qualifier STRING, value STRING,
<tail>
```

The sidecar for `CanonicalNonCodingFeature`'s `extra="allow"` passthrough. **Exactly one row per
unpromoted extra key**, and `value` holds **canonical JSON for every type** — strings, numbers,
booleans, arrays, objects, explicit null — preserving array order and duplicates. `str(value)` cannot
distinguish a source string from serialized structure nor round-trip an array. Keys promoted to named
columns on `genome_feature` are **excluded**, so nothing appears twice. Per-qualifier frequency counts
go into the ledger, counting distinct `(build_id, feature_id)` occurrences, so promoting a frequent
qualifier to a named column later is an evidenced decision.

#### Tables: `genome_build`, `genome_build_provenance`, `genome_caller_run`

```
genome_build(build_id, genome_entity_hash, assembly_content_hash, gene_caller,
             coordinate_convention, structural_features_digest, built_at, built_by_job_id,
             source_designated_current BOOLEAN, <tail>)
genome_build_provenance(build_id, non_coding_source, gene_caller,
             cds_dropped_overlapping_rna BIGINT, cds_overlap_drop_fraction DOUBLE,
             cds_discarded_json STRING, bakta_db, bakta_image, bakta_driver_sha256,
             diagnostics_json STRING, <tail>)
genome_caller_run(build_id, caller_package, caller_version, features_called BIGINT, <tail>)
```

**`build_id` is fully specified**, because a hash described as "sha256 over a tuple" has no byte
serialization:

```
build_id = sha256(canonical_json({
    "identity_version": 1,
    "genome_entity_hash": <str>,
    "assembly_content_hash": <str>,
    "structural_features_digest": <str>,
    "caller_identity": {"gene_caller": <str|null>,
                        "callers": {<package>: <version|null>, ...},
                        "bakta_db": <str|null>, "bakta_image": <str|null>,
                        "bakta_driver_sha256": <str|null>,
                        "cds_overlap_drop_fraction": <canonical decimal string|null>}
}))
```

`canonical_json` is RFC-8785-style: sorted keys, no insignificant whitespace, **explicit nulls**,
UTF-8. Thresholds are **canonical decimal strings**, never floats, so `0.5` and `0.50` cannot mint
two build ids. `structural_features_digest` **excludes** taxonomy and all downstream results — so a
non-coding enrichment **does** change the build id while a re-annotation does not. **Golden
input/preimage/digest vectors are a deliverable**: a hash rule with no test vectors cannot be
reimplemented.

**The digest preimage is normative** (round 3, stall 14):

```
structural_features_digest = sha256_hex(canonical_json([
    {"feature_id": <str>, "contig_id": <str>, "start": <int>, "end": <int>, "strand": "+"|"-",
     "feature_type": <one of the seven>, "gene_entity_hash": <64-hex>,
     "protein_entity_hash": <64-hex|null>, "caller": <str>, "caller_version": <str|null>,
     "product": <str|null>, "partial": <bool|null>, "amino_acid": <str|null>,
     "anticodon": <str|null>, "ncrna_class": <str|null>, "rfam": <str|null>,
     "rpt_family": <str|null>, "qualifiers": {<key>: <canonical JSON value>, ...}}
    , ... ]))          # one object per feature, the array sorted by feature_id (byte order)
```

with coordinates in the **stored convention** below, and `sha256_hex` the lowercase hex digest of
the UTF-8 bytes. `assembly_content_hash` is the clearinghouse `genome_entity` row's own
`content_hash` for that assembly as KBDL's standardizer computes it — **one hasher, KBDL's**; the
companion never recomputes a clearinghouse hash, it reads it from the delivery (Q21).

**Stored coordinates are one convention, always**: 1-based, inclusive, `start ≤ end`, `strand` in
`{+, -}`. `coordinate_convention` (`span` | `kbase_location`) records which convention the SOURCE
used, resolved per build because KBDL's sources are genuinely ambiguous, and the conversion is:

| source convention | `+` strand | `-` strand |
|---|---|---|
| `span` (start is the lower coordinate) | `start = s`, `end = s + len - 1` | `start = s`, `end = s + len - 1` |
| `kbase_location` (start is the 5'-most base) | `start = s`, `end = s + len - 1` | `start = s - len + 1`, `end = s` |

A converted feature with `start < 1` or `end` greater than its contig's `length` is **rejected**,
naming the feature. A strand or coordinate mistake hashes the wrong DNA while producing entirely
plausible rows, which is why the two conventions differ only on the minus strand and the test
fixtures must include minus-strand features under both.

**Full logical keys, every table** (the validators reject two rows sharing one with different
payloads):

| table | logical key |
|---|---|
| `contig` | `(genome_entity_hash, contig_id)` |
| `genome_build` | `(build_id)` |
| `genome_build_provenance` | `(build_id)` |
| `genome_caller_run` | `(build_id, caller_package)` |
| `genome_feature` | `(build_id, feature_id)` |
| `feature_qualifier` | `(build_id, feature_id, qualifier)` |
| `feature_consensus` | `(build_id, feature_id, rule_id, rule_version)` |
| `feature_curation` | `(curation_id)` |
| `condition` | `(condition_id)` |
| `essentiality`, `experimental_fitness`, `propagated_fitness` | their `*_id` |
| `load_request` | `(request_id)` |
| `request_genome_membership` | `(request_id, genome_entity_hash, build_id)` |
| `load_ledger` | `(request_id, table_name)` |
| `genome_build_currency`, `genome_disposition` | `(declaration_id)` |

No key includes `request_id` except the three request-scoped operational tables: an occurrence fact
is the same fact whichever request delivered it, and **content is deduplicated globally, builds by
`build_id`** — never by genome hash alone, which would suppress a rebuild.

`genome_caller_run` holds **one row per caller that RAN, including callers that ran and found
nothing** (`features_called = 0`). Absence of a row means the caller did not run. `features_called`
counts **retained canonical features** attributed to that package, not raw pre-filter hits —
counting survivors as raw hits would label a caller zero-hit when filtering removed everything it
found, which is the opposite of this table's purpose. **`features_called = 0` therefore means no
feature SURVIVED, not that the caller found nothing** — a consumer drawing a biological negative
from it must also read `genome_build_provenance.cds_discarded_json`.

**`caller_version` is mandatory except in two named cases** (round 3, stall 13): `caller =
'source'` (a feature carried through from an annotated input has no caller version to record), and a
historical KBDL object whose caller string carries no version. Both store NULL and write a
diagnostic to `genome_build_provenance.diagnostics_json` — a canonical-JSON array of
`{"code", "feature_id", "detail"}` objects, codes from the closed vocabulary
`caller_version_source`, `caller_version_missing_historical`. Every other caller requires a version
and its absence is a rejection.

`source_designated_current` is an immutable fact (what the source said), not a mutable pointer — see
Q11.

#### Tables: `feature_consensus` and `feature_curation`

```
feature_consensus(build_id, feature_id, function_entity_hash, verdict, n_tools, n_agree,
                  agreement DOUBLE, rule_id STRING, rule_version STRING, <tail>)
feature_curation(curation_id, build_id, feature_id, assertion_type, function_entity_hash,
                 ontology_term_entity_hash, note STRING, asserted_by STRING, asserted_at TIMESTAMP,
                 supersedes_curation_id STRING, <tail>)
```

`feature_consensus` is the role `fitness.annotation` and `fitness.gene_function` both tried to fill
and neither documented. It is per-occurrence because the *inputs* are per-occurrence (which tools ran
on this build) even though each tool's call is per-sequence. **`rule_id`/`rule_version` are
mandatory**: `fitness.annotation` carries `category`, `verdict`, `u_gene` and `n_hypotheses` with no
record of the rule that produced them, which makes 272,076 rows of verdict uninterpretable. A
consensus value without its rule is not a finding.

`feature_curation` is append-only with explicit supersession, because a curator's correction must not
delete the evidence it corrects. `assertion_type` is a closed vocabulary
(`function_override`, `ontology_add`, `feature_reject`, `note`).

**Supersession is per assertion STREAM** (round 3, stall 17). A stream is `(build_id, feature_id,
assertion_type)`. A row's `supersedes_curation_id` must name an existing row **in the same stream**;
the validator rejects a reference to a missing row, to another feature, or to another assertion
type, and rejects a second row superseding the same predecessor (a fork) and any cycle. The
projection returns **one effective head per stream**, so an `ontology_add` and a `note` on one
feature coexist, and a `function_override` never erases an unrelated assertion.

#### Tables: the experimental layer

```
condition(condition_id, name, description, condition_group, media, modelseed_compound_id,
          temperature DOUBLE, ph DOUBLE, aerobic BOOLEAN, <tail>)
essentiality(essentiality_id, build_id, feature_id, is_essential BOOLEAN, call_source,
             source_ref, <tail>)
experimental_fitness(experimental_fitness_id, build_id, feature_id, condition_id,
                     fitness DOUBLE, t_score DOUBLE, provenance, source_ref, <tail>)
propagated_fitness(propagated_fitness_id, build_id, feature_id, condition_id,
                   max_fitness DOUBLE, min_fitness DOUBLE, average_fitness DOUBLE,
                   n_fitness_hits BIGINT, <tail>)
```

Carried from `fitness` in shape, re-keyed onto `(build_id, feature_id)`. `condition` has no
`build_id` — a growth condition is a property of an experiment, not of a gene call.

**These four have no KBDL producer today.** KBDL builds and annotates genomes; it does not hold
experimental fitness. They are declared so the namespace is complete and a later fitness delivery has
a destination, and the ledger marks them **optional**: a request that carries no fitness data
reconciles at zero expected and zero written rows rather than failing.

#### Operational tables

```
load_request(namespace, requested_by, requested_at, selector_json,
             genome_count_requested INT, <tail>)          -- request_id is the tail's
request_genome_membership(genome_entity_hash, build_id, membership STRING,
             owning_request_id STRING, <tail>)            -- request_id is the tail's
load_ledger(request_id, table_name, rows_expected BIGINT, rows_written BIGINT,
            reconciled BOOLEAN, started_at, finished_at,
            qualifier_counts_json, shard_digests_json, <tail>)
genome_build_currency(declaration_id, genome_entity_hash, build_id, declared_by, declared_at, <tail>)
genome_disposition(declaration_id, genome_entity_hash, disposition, reason,
                   declared_by, declared_at, <tail>)
```

`load_request` is new and is the consequence of selective loading (Q4). `selector_json` records *what
was asked for* — a genome id list, an accession list, or a query — so a request is reproducible and
auditable even after the corpus moves underneath it. **`request_id` is declared once, in the
provenance tail, on all three request-scoped tables** (round 3, stall 7: listing it in the head too
emitted duplicate-column DDL). **`load_request` has no `status` column** (stall 16): an immutable
row cannot transition, so a request's status is **derived** — `complete` when every table in the
declared inventory has its terminal receipt, `incomplete` otherwise — and never stored.

**`request_genome_membership` is how a re-request completes** (round 3, stall 15). One immutable row
per `(request_id, genome_entity_hash, build_id)` the request resolved to, with `membership` in
`{written, already_present}` and `owning_request_id` naming the request whose rows actually hold the
facts (itself when `written`; the earlier **complete** request when `already_present`). A request's
`rows_expected` counts only the rows its `written` memberships require, and the request is complete
when those reconcile **and** every `already_present` membership names an owning request that is
itself complete. A row owned by an **incomplete** earlier request
is never counted as present — the new request writes it (idempotently, by full logical key) and
becomes its owner. This replaces "re-requesting is a no-op", which left a second request with no
durable record of what it covered.

`load_ledger` carries **exactly one terminal receipt per `(request_id, table_name)`, written only
after reconciliation.** Retry and in-progress state live **outside** the lake, in the producer's
durable inventory: an append-only `reconciled = FALSE` row would **permanently poison its request**,
because append-only data cannot retract it. `rows_expected` is derived from **the request and the
source**, never from what was written — a ledger comparing a number to itself reconciles trivially.
Completion covers the declared table inventory **including zero-row tables**, excludes the ledger
reconciling itself, and excludes independently-authored curation and disposition.

**The receipted inventory is a named constant** (round 3, stall 16): `load_request`,
`request_genome_membership`, `contig`, `genome_build`, `genome_build_provenance`,
`genome_caller_run`, `genome_feature`, `feature_qualifier`, `feature_consensus`, and the four
experimental tables (optional). `rows_written` is counted **independently of the writer**, by a
`COUNT(*)` over the table filtered to the request's `request_id` after the write commits, and
`shard_digests_json` records a sha256 over each shard's sorted logical keys so a recount can be
checked against what was sent.

**Concurrency: one writer per companion namespace, not per request.** The lock is
`(namespace, schema_version)`, so two requests with different digests can never insert the same
global build concurrently; a second loader waits or refuses. **Ambiguous commits** (the write was
sent, the acknowledgement was lost) are recovered by re-reading by full logical key before any
retry: rows already present with identical payloads count as written, a differing payload is a
rejection, and a receipt is written only after that recount. A receipt that may or may not have
landed is re-read the same way before a second is attempted. This is `council_archive.py`'s
pattern, named in Testing Decisions as prior art.

`genome_build_currency` and `genome_disposition` are **append-only immutable declarations** projected
deterministically — greatest `declared_at` per genome, tie-broken by greatest `declaration_id` — with
currency falling back to `source_designated_current` **only when exactly one of the genome's builds
carries it**. Zero or several source-current builds and no declaration means **no current build**:
the genome is absent from `published_feature` and the projection reports it, fail-closed (round 3,
stall 8). There is **no mutable column anywhere** in the namespace. **The loader writes no
disposition declaration**; absence means **unpublished**, never an implicit published.

### Change 4 — the read projections

Three projections, and they are the only sanctioned way to select science. They are where the
`fitness`-shaped convenience lives, reconstituted by join:

| projection | shape |
|---|---|
| `published_genome` | one row per published genome: `genome_content`'s metadata + the latest `genome_quality@1` and `genome_taxonomy@1` results flattened into columns |
| `published_feature` | `genome_feature` for the current build of published genomes, joined to `gene_content`/`protein_content` for sequence and length; **includes** `repeat_region` |
| `published_gene_annotation` | `published_feature` joined to `clearinghouse.protein_result`/`gene_result` and `function_content`, one row per (feature, tool, function) — **the `fitness.function_annotation` shape, materialised by join rather than stored**; **excludes** `repeat_region` |

Each applies publication, currency and completeness **before any join**. **Raw tables are archival
inputs, not a query surface.**

**The projection rules are normative** (round 3, stall 8), and the schema module pins each as SQL:

- **Latest result** (`published_genome`'s quality and taxonomy columns): per `(entity_hash,
  result_type)`, greatest `observed_at`, ties broken by greatest `source` then greatest
  `parameter_set_hash` (byte order). A NULL `observed_at` never wins.
- **Current build**: the currency rule under Operational tables, fail-closed.
- **Published**: the latest `genome_disposition` per genome is `published`.
- **Complete**: the build's `owning_request_id` (via `request_genome_membership`) is a complete
  request.
- **`published_gene_annotation`**: `published_feature` joined to `protein_result` (CDS, on
  `protein_entity_hash`) or `gene_result` (non-CDS, on `gene_entity_hash`) where
  `result_type = 'function_call'`, then to `function_content` on the payload's
  `function_entity_hash`. One row per `(build_id, feature_id, source, parameter_set_hash,
  function_entity_hash)`; a payload whose `function_entity_hash` has no `function_entity` row is an
  orphan, reported by the rehearsal and never silently dropped.

`published_genome` is what makes Q3 tolerable: the CheckM2 fields arrive as columns even though they
are stored as a declared JSON payload, so "completeness > 95" is a column predicate **at the read
surface** — but it is still JSON extraction at query time underneath, not a physical column scan,
which is why the acceptance rehearsal measures it rather than assuming it.

**SQL views are a HARD PREREQUISITE** (round 3, stall 9; Q22). The previous fallback — materialized
tables rebuilt per request — contradicted the namespace's own rules: append-only rows cannot remove
a genome that stopped being published, and a per-request rebuild misses declarations made between
requests. The phase-1 smoke measures whether views can be created through the sanctioned rail; if
they cannot, the rollout **stops** and the projection mechanism goes back to design.

### Partitioning and physical layout

| table | `partition_by` | why |
|---|---|---|
| `genome_feature` | `feature_type` | 7 values; prunes the ~95% CDS majority from the non-coding driving case |
| everything else | *(none)* | below the threshold, or no low-cardinality column exists |

**No table is partitioned on a hash** — a good hash scatters uniformly, so partitioning on it buys
zero pruning while multiplying partition count. The clearinghouse settled this with the same
reasoning, and `genome_entity_hash` falls under it.

**This is dramatically less partitioning than the previous design needed, and selective loading is
why.** The previous draft sized for 2M genomes arriving as one corpus. A request-scoped namespace
holds what was asked for: the first instance will hold thousands of genomes, not millions, and
`genome_feature` is the only table that reaches 10^7 rows at that scale. The per-sequence annotation
move (Q7) removes the tables that would have reached 10^9 — they are now clearinghouse `<type>_result`
rows, partitioned on `source`, deduplicated across the corpus.

**File size and locality are controlled by the WRITER, because they cannot be controlled by the
partition spec.** `table_configs()` emits only `name`, `schema_sql` and an optional `partition_by`,
rendered as `PARTITIONED BY (\`col\`)` over bare stored columns — so
`write.target-file-size-bytes`, Iceberg sort order, `write.distribution-mode` and Parquet bloom
filters are **not expressible through this write path at all**.

Where sharding is needed, **reuse `clearinghouse_shard.py`'s bounded spill/merge mechanism and NOT
its key spec.** Its `_key_spec` (`clearinghouse_shard.py:5360`) selects clearinghouse **entity dedup**
keys; reusing that for `genome_feature` would **discard all but one feature per genome**. Sorting by
genome is not deduplicating by genome. The companion routes on `genome_entity_hash`, which **is** a
real 64-hex key, so equal-width hex ranges are correct here — unlike the previous design, which tried
to route on a G-number that turned out to be a decimal counter. Dedup is on identical **full
request-scoped** keys only, and conflicting duplicate payloads are **rejected**, not silently
resolved.

**512 MB is an uncompressed row-size budget, not a promise about compressed Iceberg files.** It is
Iceberg's documented default `write.target-file-size-bytes` (536,870,912), which is why it is the
starting figure — but `_row_bytes` (`:5301`) counts string lengths, not compressed bytes,
`_write_parquet` (`:5314`) does not request ZSTD, and Spark ingestion can repartition sorted input.
So the layout is **verified by a rehearsal through the real ingest path**, with the acceptance bound:
**a single-genome current-build feature lookup touches at most two data files per represented
`feature_type` partition.** If the rehearsal fails, the rollout stops.

### Change 5 — the KBDL load function

The loader is the **existing GAA-free `kbdl_service/clearinghouse/` module, extended** — not
`KBDLStoreLoad` revived. That package is forbidden from importing `genome_annotation_aggregator` or
`kbutillib` by `tests/test_layering.py` INVARIANT 1, which is exactly the property that makes it the
right place: GAA's retirement stops blocking the lake.

**A load is a request over a named set, and the three phases are ordered by dependency:**

1. **Register entities and content in the clearinghouse.** For each requested genome: compute
   `entity_hash` (rule A: contigs canonicalized, empty dropped, **sorted**, `|`-joined, sha256 —
   order-independent), upsert `genome_entity` + `genome_content` with the intrinsic metrics, then the
   same for every gene DNA sequence and protein sequence. **Content registration is fill-only**: an
   entity already present is not rewritten.
2. **Write per-sequence tool results to the clearinghouse.** CheckM2 and skani to `genome_result`
   under the declared result types; per-protein and per-gene tool annotations to `protein_result` and
   `gene_result`, keyed on the *sequence* hash — so a protein already annotated by a tool at a
   version is **not re-annotated or re-stored**, which is where the two-orders-of-magnitude saving
   actually happens. **"Already annotated" means the FULL result identity** (round 3, stall 11):
   `(entity_hash, result_type, result_type_version, source, parameter_set_hash)`. The same tool and
   version against a different database or with different parameters is a different result and is
   written; an identical identity with a **different payload** is rejected, never overwritten. A
   context-dependent call (TRANSYT's `taxonomy_id`) differs in `parameter_set_hash`, so its context
   is recorded without breaking per-sequence keying.
3. **Write the occurrence layer to the companion.** `contig`, `genome_build`,
   `genome_build_provenance`, `genome_caller_run`, `genome_feature`, `feature_qualifier`, then the
   ledger receipts.

**Ordering is a correctness requirement, not a preference.** A companion `genome_feature` row whose
`gene_entity_hash` is absent from `gene_entity` is a dangling reference, so phase 1 completes for a
genome before phase 3 writes its features. Likewise `parameter_set` registry rows are appended before
the results referencing them, and the loader runs the orphan check — distinct `parameter_set_hash` in
results absent from the registry — and requires it empty.

**Idempotence is by full logical key, and completeness by membership.** Re-running a request writes
nothing new. Re-requesting a genome whose content and build are unchanged writes **no fact rows**
but does write its `request_genome_membership` row (`already_present`, pointing at the owning
complete request), so the second request has a durable record of what it covered. A **rebuild** of
the same assembly has a different `build_id` and is written — deduplication is by `build_id`, never
by genome hash alone. A genome whose content changed has a different `entity_hash` and is a different
genome by construction. This replaces `_lookup_existing_genome_id`'s `skipped_already_ingested`
behaviour, under which a reload was a silent no-op *even when the genome had changed*.

**Alias precondition, checked before ANY write** (round 3, stalls 6 and 10). The loader reads the
live clearinghouse table list first and **refuses the whole request** — naming the missing table —
if `genome_alias` is absent, or if the genome/gene/protein content tables lack the Change 1 columns.
As measured on 2026-11-22 the alias tables are not live, so **production loading is blocked until
the alias kind is bootstrapped**, which is another PRD's work (Out of Scope). The rehearsal is not
blocked: it bootstraps its own scratch namespaces.

**What a request delivers.** The loader reads a delivered snapshot **plus the object closure it points
at** — the SQLite backup is not enough, because registry rows are not the genome objects that contain
features, and a SQLite-only delivery yields genome headers and zero features. A delivery is: the
backup (taken through the **sqlite3 backup API**, never a file copy — a `cp` of a live WAL database
yields a torn backup that opens fine), every genome JSON / FASTA / annotation object reachable from
the requested rows and their supersession chains, and a manifest carrying `delivery_id`, `store_id`,
`snapshot_id`, the schema version, UTC `created_at` and `deadline_at`, and a `(path, bytes, sha256)`
inventory. **A manifest's presence is not proof of arrival**: the trigger is "the manifest exists
**and** every listed file is present **and** sha256-matches". A first mismatch means still syncing —
retry with progress persisted outside the lake. Past `deadline_at` it is a failed delivery, raised
with which file and expected-versus-observed size and digest.

**Who produces it, who consumes it, and the contract between them** (round 3, stall 12; Q21). The
previous revision specified the delivery and assigned nobody to build it, and left the call
boundary between the two repos unstated:

- **KBDL produces** (`gc2-12`), next to the store it reads: one verb that takes a request selector,
  runs phases 1 and 2 against the clearinghouse (`gc2-6`), then writes the delivery directory — the
  sqlite3-API backup, the object closure, an **`entity_index.jsonl`**, and the manifest last.
- **`entity_index.jsonl` is what makes KBDL the only hasher.** One line per requested genome:
  `genome_entity_hash`, `assembly_content_hash`, `store_id`, `kbdl_genome_id`, and per feature
  `feature_id` → `gene_entity_hash`, `protein_entity_hash`. The companion loader reads hashes from
  it and never recomputes them, so two standardizer implementations cannot drift.
- **KBUtilLib consumes** (`gc2-13`): the manifest validator, the arrival check (exists, every file
  present, every sha256 matching), `deadline_at` handling, and the durable retry inventory — a JSON
  state file **outside the lake**, keyed by `delivery_id`. It yields parsed `CanonicalGenome`
  documents plus their index entries to the phase-3 writer (`gc2-8`), which re-checks that every
  hash it is about to reference exists in the clearinghouse entity tables before writing.
- **The contract is versioned**: the manifest carries `delivery_schema_version: 1`, and the consumer
  refuses any other version. Manifest fields: `delivery_id`, `delivery_schema_version`, `store_id`,
  `snapshot_id`, `request_id`, `selector_json`, `created_at`, `deadline_at` (UTC ISO-8601), and
  `files: [{path, bytes, sha256, role}]` with `role` in `{sqlite_backup, genome_json, fasta,
  annotation, entity_index}`.
- **The end-to-end path is two commands, in order**: KBDL's produce verb, then KBUtilLib's
  `companion load --delivery <dir> --namespace <ns>`. The acceptance task runs both.

**Every write path takes its target namespaces as arguments** — the clearinghouse write target as
well as the companion — defaulting to the production constants. The acceptance rehearsal uses dated
scratch namespaces for **both**, and no acceptance step writes `kbaseincubator.clearinghouse` or the
production companion: a rehearsal into the live clearinghouse would spend Q2's window and Q3's
reversibility before the measurement meant to inform Q3 exists (round 3, stall 18).

**Supersession resolves two independent chains from the delivered snapshot only** — never a live
lookup, so a snapshot determines its rows. The FASTA chain resolves into
`genome_content.fasta_reference`; the genome-JSON chain resolves into `genome_alias` under the
`kbdl_object` source, recording observed and resolved. A cycle, a missing target or a type mismatch
**raises**. Measured caveat: **0 of 4,865 genomes** have either id appearing as an `old_object_id`,
and **0 chains exceed depth 1**, so this path has no live coverage and its fixtures are its only
test.

`KBDLStoreLoad`'s **submission path** is retired with an explicit "unsupported; use the clearinghouse
load function" error, while **historical job-record deserialization is preserved** — removing the
type outright would break reading existing `jobs` rows, which is a different public behaviour from
refusing new submissions. It has run **zero times** (confirmed by the absence of any `jobs` row of any
state, against 4,891 `KBDLBuildGenome` and 3,341 `KBDLGenomeAnnotation` completions), so there is no
incumbent behaviour to preserve beyond readability.

**Nothing in `fitness` or `inner_loop` is touched.** This delivery is purely additive to both.

## Testing Decisions

**Test external behaviour, not implementation.** What is observable: what is in the tables after a
request, what the loader refuses, and what a query costs.

**The criterion no stub can satisfy.** Every schema test here could pass against a fake lakehouse.
The guard is a **real-dependency smoke task in phase 1** that creates one real table in a scratch
namespace against the live BERDL endpoint, writes one row, reads it back, and asserts the
round-tripped types — in particular that `TIMESTAMP` survives with its value, which
`clearinghouse_shard.py` records as a live trap — and that a `PARTITIONED BY` clause actually lands,
read back with `SHOW CREATE TABLE` (`DESCRIBE` returns an empty result set through this endpoint).
Round 3 adds three measurements the design now depends on: that a column named `end` (a reserved
word, and a real `genome_feature` column) survives a real `CREATE`; that `ALTER TABLE ... ADD
COLUMNS` works on the scratch table; and that a SQL view can be created and read. Every later phase
depends on it. **A skipped or inconclusive smoke is a BLOCKED dependency, not a passed gate**
(round 3, stall 1): it persists the endpoint, each result and the view and ALTER capabilities to its
work record, and later phases proceed only on a live pass. Precedent: `kbdl-horizyn-v1`, where eight
tasks passed on fixtures and both real defects surfaced in the first phase touching the real
package.

**The non-duplication test, which is this revision's whole thesis.** Load two genomes that **share a
protein sequence**. Assert: **one** `protein_entity` row, **one** `protein_content` row, **one**
`protein_result` row per tool — and **two** `genome_feature` rows. Then assert
`published_gene_annotation` returns the annotation for **both** features. Written so that storing
annotations per-feature instead would fail it. This is the test that keeps the design from silently
regressing into the previous one.

**Clearinghouse extension tests.** The added content columns round-trip with their declared types; a
`genome_content` row written by the loader carries `gc_content`, `contig_n50`, `max_contig_length`,
`min_contig_length`, `n_ambiguous_bases` and `assembly_level`; every derivation rule in the Change 1
table is checked against its golden inputs — including `is_closed` NULL, a 10-`N` run versus a
9-`N` run, an all-ambiguous contig (`gc_content` NULL), an empty assembly, and an N50 where the
running sum lands exactly on 50%; `table_configs()` returns **21** configs with the five alias
configs byte-identical to the base commit; `protein_content` carries no `is_fragment`; the additive
migration adds the new columns to an existing empty old-shape table, refuses a non-empty one, and
refuses any live-versus-declared column difference; and `function_content`/`ontology_term_content`
are **unchanged** (asserted explicitly, because they are the tables with rows and the ones a careless
migration would touch).

**Result-type tests.** `genome_quality@1`, `genome_taxonomy@1` and `function_call@1` validate against
their registered `schema_json` (the normative table in Change 2), each canonical example passes, and
each is **rejected** when any one required field is missing, when a bound is violated, and when an
unknown field is present; `additional_notes` of the literal
string `"None"` normalises to null while any other value is preserved verbatim; two CheckM2 versions
on one genome produce **two** `genome_result` rows distinguishable by `source`, and
`published_genome` surfaces the later one.

**Rejections, not happy paths.** For each rule the test attempts the violation and asserts the
refusal, with the error naming the offending field and value:
- a non-CDS feature carrying a `protein_entity_hash`; a CDS with a null one;
- `feature_type = 'snoRNA'` — a legal upstream *input* subtype, not a legal `feature_type`;
- a `genome_feature` whose `gene_entity_hash` is absent from `gene_entity` (the dangling reference
  phase ordering exists to prevent);
- a `store_id` that looks like a filesystem path;
- a tool result with no `tool_version`;
- a `feature_consensus` row with no `rule_id`;
- an `assertion_type` outside its closed vocabulary;
- two rows sharing a full request-scoped key with **different payloads**.

**The zero-hit caller, both directions.** A caller that ran and found nothing produces a
`genome_caller_run` row with `features_called = 0`; a caller that never ran produces **no row**. Both
assertions, because testing only the first cannot distinguish the two states. And `features_called` is
asserted to count **retained** features, via a fixture where the caller emits features that are then
filtered out.

**The rebuild case.** Load one genome built `inprocess`, then the same genome rebuilt with
`gene_caller='bakta'` at different coordinates. Assert two `genome_build` rows, both feature sets
retained, **one** `genome_entity`/`genome_content`/`contig` set, one projected current build, that a
`feature_id`-only query returns **two** rows, and that `published_feature` returns one set.

**Selective loading.** A request naming three of ten available genomes loads exactly three; the other
seven appear in **no** companion table. A second request naming an overlapping set does not duplicate
the already-loaded genomes' entities or features. A request naming a genome that does not exist fails
with that genome named, rather than silently loading a smaller set.

**Instancing.** Two companion namespaces are bootstrapped and loaded with overlapping genome sets;
assert each holds its own occurrence rows, that both reference the **same** clearinghouse entity
hashes, and that neither's projection returns the other's rows.

**Two genomes, one assembly.** The `SyringaeB728a` / `SyringaeB728a_mexBdelta` pair — a wild-type and
its knockout mutant with a byte-identical assembly — is loaded and the test asserts the **documented
collapse**: one `genome_entity`, and the second load recorded as `already_present` membership rather
than an error (round 3 reconciled this with Q9; the previous text demanded the pair stay
distinguishable, which the design explicitly does not do).
**This is a TEST-ONLY fixture and the limitation is real**: the pair lives in `fitness`, not KBDL, and
KBDL's registry dedupes exactly that shape on `content_hash`, so a KBDL-only loader cannot produce
it. Worse under this revision: `genome_entity_hash` **is** the assembly hash, so two genomes with
identical assemblies collapse to **one** `genome_entity` by construction. See Q9 — this is the one
place where the clearinghouse-keyed design is strictly weaker than the previous one, and it is
decided rather than hidden.

**Ledger lifecycle.** A table with no terminal receipt is unpublishable; a second receipt for one
`(request_id, table_name)` is rejected; `rows_expected` derived from the request and source, with a
self-comparing ledger rejected; an optional table with zero expected and zero rows reconciles, while a
positive expected with zero rows fails.

**The zero-join guard, correctly scoped.** A blanket non-empty check would reject correct requests —
`feature_qualifier` is legitimately near-empty for ab initio genomes, and the four fitness tables have
no KBDL producer at all. So the guard requires a **non-empty requested genome set** for the
relationship suite; an **optional** table may be zero-row when its derived expected count is zero; a
**positive expected count with zero extracted rows fails**; key non-nullness is checked only for
required keys. **The guard's own two tests**: relationship assertions over an empty **required**
fixture must **fail** (written so deleting the guard fails the test), and an optional table must
**pass** at zero-expected while **failing** at positive-expected.

**The layout rehearsal** is the acceptance bound above, measured through the real ingest path,
**repeated over a second incremental request** as well as the first batch, since the two-files bound
can hold for one ingest and fail under appends.

**The acceptance rehearsal's ground rules** (round 3, stall 18). It writes only dated scratch
namespaces for both the clearinghouse and the companion, and may write a **test-only** publication
declaration there so the projections return rows. Its fixtures are **selected first, by a stated
query recorded in the work record**: a KBDL genome with a zero-hit caller, one with a never-run
caller, one with a CRISPR `repeat_region`, and two sharing a protein. Any case with no qualifying
real genome uses a synthetic fixture and is **reported as synthetic**, never silently skipped. Its
pass/fail thresholds are fixed **before** it runs: the two-files layout bound, a zero orphan count,
and membership-based completeness. The Q3 query-cost comparison (a JSON-extracted predicate against
the flattened projection, same workload, three runs each, medians reported) is a **measurement
with no threshold**: it feeds Chris's Q3 decision and gates nothing.

**Delivery tests.** A complete delivery loads; a delivery missing one listed file, one with a sha256
mismatch, one past `deadline_at`, and one with `delivery_schema_version: 2` are each refused with the
offending file or field named; and a companion load whose `entity_index` names a hash absent from
the clearinghouse entity tables is refused before any companion row is written.

**Request membership.** A second request over an already-loaded genome writes only membership
(`already_present`) and is complete; a request over a genome whose first request is still
incomplete writes the rows itself and owns them; a rebuild in a second request writes a new
`genome_build`.

**Prior art:** KBDL's `tests/test_layering.py` for invariant-as-test; KBUtilLib's clearinghouse
bootstrap tests for DDL and partition assertions; `council_archive.py` for ledger and
ambiguous-commit tests.

**Regression framing.** Both repos carry pre-existing failures, so every criterion is "no test that
passed on the base commit fails on the branch, and the tests this task adds pass" — never "the suite
is green".

## Open Questions and Judgement Calls

**Every item below is already decided and reflected in the PRD body and taskplan. The PRD is
buildable as it stands. To change a decision, edit its `DECIDED:` line** — Review Mode will
propagate the consequences through the body, the stories, the testing decisions and the taskplan.

Ordered by blast radius descending; within a radius, lowest confidence first.

### Q1. What is the dividing line between the clearinghouse and the companion? -- DECIDED: sequence-or-identity facts to the clearinghouse; occurrence facts and genome-and-condition facts to the companion. Nothing is stored in both.
**Blast radius:** IRREVERSIBLE
**Why:** This is Chris's instruction turned into a rule a builder can apply without asking. It is not
an aesthetic preference: a clearinghouse gene entity is keyed on the content hash of its *sequence*
with `stores_content=True`, so the same sequence in a thousand genomes is **one** row by design, and
location is therefore a property the clearinghouse structurally cannot hold. The companion exists to
hold exactly that, and the earlier drafts' `assembly`, `genome`, `genome_alias`, `function` and
`ontology_term` tables were re-declaring columns that already exist in
`clearinghouse._CONTENT_TYPE_HEAD`, `genome_alias`, `function_content` and
`ontology_term_content`.
**If you disagree:** a self-contained companion that duplicates genome metadata is loadable without
the clearinghouse — the one real argument for it — and immediately needs a reconciliation rule for
when its copy and `genome_content` disagree, which is the failure mode this split eliminates by
construction. Reverting re-adds five tables and makes every companion instance an independent
assertion about what a genome is.
**Confidence:** high. The rule is grounded in what those tables already are, not in an argument about
what they should be.

### Q2. Where does per-entity metadata live, and is it safe to add now? -- DECIDED: in `clearinghouse._CONTENT_TYPE_HEAD`, extended per entity type; and YES, because the genome, gene and protein tables are all 0 rows TODAY.
**Blast radius:** IRREVERSIBLE
**Why:** The mechanism Chris described already exists and is the right one — a per-entity-type head
composed onto a shared tail, declared once. The decisive fact is the window: measured 2026-10-27,
`genome_entity`, `genome_content`, `gene_entity`, `gene_content`, `protein_entity`, `protein_content`
and all four `ontology_term_*`/`*_result` genome-gene-protein tables hold **0 rows**, while only
`function_entity`/`function_content` (47,341) and `function_result` (20,036) have data. The
clearinghouse is append-only, fill-only, with no production delete — so a column not added before the
first genome load becomes a corpus-wide backfill. `function_content` and `ontology_term_content` are
deliberately **not touched**, because they are the only tables where a change is expensive.
**If you disagree:** adding these columns later is a backfill over however many genomes have loaded
by then, plus a schema evolution on the live tables. Not adding them at all pushes assembly metrics
into the companion, which re-creates the duplication Q1 removes.
**Round 3 (stalls 2, 4, 10):** the window was re-measured open on 2026-11-22 23:43 CST. The live
tables exist in the old shape, so the extension lands by additive `ALTER TABLE` after a same-run
zero-row recheck, never by drop; `protein_content.is_fragment` is removed (an occurrence fact);
`table_configs()` stays at 21.
**Confidence:** high on the mechanism and on the window; both are measured.

### Q3. Do CheckM2 and skani outputs go in `genome_content` or `genome_result`? -- DECIDED: `genome_result`, as declared result types (`genome_quality@1`, `genome_taxonomy@1`) registered with JSON schemas. Only INTRINSIC metrics go in `genome_content`.
**Blast radius:** MEDIUM
**Why:** **This is the one place I interpreted Chris's instruction differently from how he phrased
it**, and it is flagged rather than buried. He named "checkm scores" as content metadata. The reason
I split them: a column belongs in `content` if two independent implementations reading the same bytes
must agree on its value. `gc_content`, `contig_n50` and `max_contig_length` qualify. `completeness`
and `contamination` are a *model's* assessment — CheckM2 1.0 and 1.1 legitimately disagree, the value
is meaningless without `completeness_model_used`, and `additional_notes` can say the model disagreed
with itself by 25%. `_RESULT_COLUMNS` already carries `source`, `parameter_set_hash` and
`result_type_version`, and `source` is already the partition key, so `genome_result` gives all of that
for free — and `clearinghouse_result_types` already exists to declare a payload schema, so this is
not an opaque blob.
**The practical cost is real and it is why this is MEDIUM**: the payload is a `STRING`, so
"completeness > 95" becomes a JSON extraction rather than a column scan. That is why
`published_genome` flattens the latest quality and taxonomy results into columns — the convenience
Chris asked for exists at the read surface, with the provenance kept underneath.
**If you disagree:** the alternative is `completeness DOUBLE, contamination DOUBLE, coding_density
DOUBLE, quality_tool STRING, quality_tool_version STRING` directly on `genome_content`. That is
cheaper to query and loses the ability to hold two tools' assessments of one genome without a second
content row — and content rows are keyed on `entity_hash`, so two assessments would mean two rows
differing only in provenance, which is what `<type>_result` exists to avoid. It is a one-line change
to `_CONTENT_TYPE_HEAD` plus deleting the two result types, and it is free to make **now** and
expensive after the first load.
**Confidence:** medium. High that the intrinsic/assessed line is the right *principle*; medium that I
drew it where Chris wants it, since he named checkm scores explicitly. **This is the entry most worth
your attention.**
**Round 3:** the reversal window is now protected mechanically. The acceptance rehearsal writes only
scratch namespaces, its query-cost comparison is a measurement with no pass/fail threshold, and
**no production load runs until Chris has recorded his Q3 call after reading that measurement** —
so the cheap-to-reverse state survives until the evidence exists.

### Q4. Is a load a snapshot of a store, or a request for named genomes? -- DECIDED: a REQUEST. `load_request` records the selector; the unit of completeness is `request_id`, not a store snapshot.
**Blast radius:** IRREVERSIBLE
**Why:** Chris: *"not EVERY genome will be loaded… just the ones we call the load function on."* That
is not a tuning note — it changes the identity model. The previous design made every row an immutable
version of a whole-store snapshot, with a current read selecting one fully-reconciled `snapshot_seq`
per store. Under request semantics that machinery is both wrong and unnecessary: a genome is simply
absent until requested, so "the selected snapshot's empty table stays empty" stops being a hazard and
becomes the normal case. Recording `selector_json` is what keeps a request reproducible after the
corpus moves underneath it.
**If you disagree:** whole-corpus snapshot loading gives a single consistent cut across all genomes
at a point in time, which request loading does not — two requests a week apart hold facts from two
different source states, distinguishable only by each row's `snapshot_id`. Reverting re-introduces
`snapshot_seq`, the fully-reconciled-snapshot selection in all three projections, and the
empty-table-stays-empty rule.
**Confidence:** high on the direction, which is Chris's. **Medium on the consistency consequence**: I
have not designed a way to ask "give me a consistent cut across genomes loaded in different
requests", and at some point somebody will want one. The per-row `store_id`/`snapshot_id` makes it
*answerable*; it does not make it *easy*.

### Q5. Is the namespace a constant or a parameter? -- DECIDED: a parameter, defaulting to `genome_companion`. Per-user instancing is designed no further than that.
**Blast radius:** MEDIUM
**Why:** Chris: *"we may make multiple instances of this table… a centralized one for public data…
and possibly separate ones for users, but let's not worry about that yet."* So the cheap half is done
now — every verb takes the namespace, nothing hardcodes it — and the expensive half (ACLs, quotas,
cross-instance queries) is deliberately not designed. What makes this work at all is that the
**clearinghouse stays single and shared**: companion rows reference content hashes, which are globally
valid, so two instances describing one genome agree about what it is without coordinating. That
property is a direct consequence of Q1 and would be lost if each instance held its own genome
metadata.
**If you disagree:** hardcoding one namespace is simpler and makes the eventual second instance a
migration rather than an argument.
**Confidence:** high on parameterising; the deferral is Chris's explicit instruction, not my judgement.

### Q6. How is immutability and reproducibility preserved under request loading? -- DECIDED: every companion row carries `request_id`, `store_id`, `snapshot_id`, `observed_at`, `loaded_at`; the request is the unit of completeness; nothing is mutable.
**Blast radius:** MEDIUM
**Why:** This is confront stall 2 surviving into the new model in changed form. The previous draft
asserted append-only *and* a global primary key *and* snapshot-keyed idempotence, which could not all
hold. Request scoping resolves it differently from snapshot versioning: a re-request of unchanged
content is a no-op because the **content hash is the identity**, so there is nothing to version. A
genome whose content changed has a different `genome_entity_hash` and is a different entity by
construction. `store_id` is never derived from a filesystem path — a store that moves off `/scratch`
must keep its id, or every row it has published becomes unjoinable.
**If you disagree:** dropping `snapshot_id` from the tail saves a column and loses the ability to say
which source state a fact came from, which is what makes a bad load retractable.
**Confidence:** high.

### Q7. Where do per-sequence tool annotations live? -- DECIDED: `clearinghouse.<type>_result`, keyed on the SEQUENCE hash. The companion stores NO per-feature functional annotation; `published_gene_annotation` reconstitutes the `fitness` shape by join.
**Blast radius:** IRREVERSIBLE
**Why:** The largest storage consequence in the design, and the clearest case of "do not replicate".
A BAKTA call on a protein sequence is a fact about the *sequence*. `<type>_result` already exists for
it with `(result_type, source, parameter_set_hash, result_type_version, payload)` and is partitioned
on `source`. At ATB scale the same protein recurs across many genomes: storing the annotation
per-feature multiplies it by that recurrence, and on a 2M-genome corpus that is roughly the difference
between 10^8 and 10^10 annotation rows. `fitness.function_annotation` is exactly the per-feature shape
and holds 1,118,422 rows for **48 genomes**; extrapolated naively to 2M genomes that is ~4.7×10^10.
**If you disagree:** per-feature annotation rows make "this genome's annotations" a single-table scan
instead of a three-way join, which is a real ergonomic loss and the reason `published_gene_annotation`
exists. Reverting adds `feature_function`, `feature_ontology` and `feature_transyt` to the companion,
restores the storage multiplier, and requires a rule for what happens when the per-feature copy and
the clearinghouse result disagree.
**Round 3 (stall 11, kept NARROWER than proposed):** "already annotated" now means the full result
identity `(entity_hash, result_type, result_type_version, source, parameter_set_hash)`, and a
conflicting payload is rejected. The adversary also asked for a relation recording which per-sequence
result "applies to" each feature on each build. **Declined**: under this decision a per-sequence
result is a fact about the sequence wherever it occurs, so `published_gene_annotation` joins every
result for the sequence and claims nothing about a tool having run on that build. Build-scoped
attribution is what `genome_caller_run` holds for callers; if annotation tools need the same, it is a
new table, and it is not needed for anything this PRD delivers.
**Confidence:** high on the storage argument, which is arithmetic over measured row counts.
**Medium on the join cost**, which I did not measure in this Trino deployment — a three-way join at
10^7 companion rows against 10^8 clearinghouse rows is the main thing that could make this
uncomfortable, and the layout rehearsal should measure it.

### Q8. Is `feature_id` a key on its own? -- DECIDED: no. Feature identity is scoped by an annotation run: `genome_build`, with `genome_feature` keyed `(build_id, feature_id)`.
**Blast radius:** IRREVERSIBLE
**Why:** Carried from the previous round, where the external scan found it, and still true. KBDL mints
`{genome_id}_{N}`, and `genome_registry._lookup_existing_genome_id` dedupes on `content_hash` — so
rebuilding one assembly under a different `gene_caller` reuses the same id space with **different
coordinates**. The measured analogue is stark: Siqueira et al. 2025 (doi:10.1101/2025.05.27.656432)
compared GenBank and RefSeq annotations of a **single identical** *P. putida* KT2440 assembly and
found 159 genes only in one, 81 only in the other, and **897 — ~16% of ORFs — with identical locus
tags at different genomic positions**. No schema anywhere merges competing structural callers: INSDC
gives one assembly two accessions, Ensembl uses a parallel `otherfeatures` database, Chado makes the
analysis part of feature identity.
**If you disagree:** keying on `feature_id` alone survives only while one build per genome ever
loads — true today, false the first time `kbdl-build-genome-bakta-calling-v1` rebuilds an in-process
genome. Reverting un-keys `genome_feature`, `feature_qualifier`, `feature_consensus`,
`feature_curation` and the three gene-keyed experimental tables.
**Confidence:** high on the need; **medium on `build_id`'s composition**, which is my construction —
Chado keys `analysis` on `(program, programversion, sourcename)`, which is coarser and would not
distinguish two Bakta runs against different databases. The `structural_features_digest` makes it
self-correcting: two genuinely different feature sets cannot share a build id whatever the metadata
says.

### Q9. Can two genomes with byte-identical assemblies be distinguished? -- DECIDED: NO, under this design, and that is a REGRESSION from the previous draft that I am taking deliberately.
**Blast radius:** IRREVERSIBLE
**Why:** This is the one place the clearinghouse-keyed design is **strictly weaker**, and it deserves
to be read before anything else here is approved. The companion keys genomes on
`genome_entity_hash` — the clearinghouse identity, which is the assembly content hash. Two
biologically distinct genomes with identical assemblies therefore collapse to **one**
`genome_entity`. The live corpus contains exactly that case: `fitness.genome` holds 48 rows over 47
distinct assembly hashes, and the colliding pair is `SyringaeB728a` and `SyringaeB728a_mexBdelta` — a
wild-type and its knockout mutant. The previous draft handled it with a surrogate `genome_id` key;
this one cannot, because its genome identity is not its own to define.
I am taking it because Q1 is worth more: a surrogate genome key in the companion re-introduces a
genome table, which re-introduces genome metadata, which is the duplication the whole revision
removes. And the pair is **not loadable from KBDL anyway** — KBDL's registry dedupes that shape on
`content_hash`, so the previous draft's support for it was theoretical too (its acceptance fixture was
already marked test-only for this reason).
**If you disagree:** the fix is a `genome_instance` table in the companion —
`(instance_id, genome_entity_hash, orgid, strain_note, declared_by)` — giving a surrogate key for
"distinct biological genome sharing an assembly", with `genome_feature` keyed on `instance_id` instead.
That is one small table and a re-key of five, it does **not** re-introduce metadata duplication, and
**I would support adding it the first time a real mutant/parent pair needs to load.** It is not in
this PRD because nothing can currently produce such a pair and designing for it now would be
speculative.
**Confidence:** high on the regression being real and bounded; high that it is currently unreachable;
**this is the decision most likely to need reversing**, and the escape hatch above is deliberately
specified so reversing it is cheap.

### Q10. Does the companion hold contigs? -- DECIDED: yes, a `contig` table keyed `(genome_entity_hash, contig_id)`.
**Blast radius:** MEDIUM
**Why:** `genome_content` holds `n_contigs`, a count. Per-contig identity is not there and should not
be — a content row describes an assembly, and a list of named contigs is not a scalar. But
`genome_feature.contig_id` has to resolve to something, and without this table it dangles, which is
exactly `fitness.gene.contig_id`'s existing defect: `fitness` has no contig table at all. Contig
identity is also genome-scoped rather than sequence-scoped in the way that matters here — a contig
named `NC_007626` means something only within its assembly.
**If you disagree:** contig could become a sixth clearinghouse entity type, which would deduplicate
identical contigs across assemblies — plausible, and a much larger change (it touches `ENTITY_TYPES`,
all three kinds, the policy table and the union views). Or coordinates could reference a contig
*index* rather than a name, which saves the table and loses the ability to report a feature's location
in the terms the source FASTA used.
**Confidence:** high.

### Q11. How are "current build" and "is this published" represented? -- DECIDED: append-only immutable declarations plus a deterministic projection. NO mutable column anywhere in the namespace.
**Blast radius:** MEDIUM
**Why:** Carried from the previous round's confront, where a mutable `is_current` flag was found to be
unimplementable: no winner rule, no behaviour for loading an older state after a newer one, nothing
saying which physical row got mutated, and `BerdlCapability.load` (`capability.py:458`) exposes append
and overwrite rather than a pointer transaction. Declarations need no mutation — current build is the
greatest `declared_at` per genome with a deterministic `declaration_id` tie-break, falling back to
`source_designated_current`; effective disposition is the same rule. **The loader writes no
disposition declaration**, and absence remains unpublished, because the failure directions are
asymmetric: wrongly withholding a result is visible and fixable, wrongly publishing a trial is neither.
**Round 3 (stall 8):** the `source_designated_current` fallback applies only when exactly one build
carries it; zero or several, with no declaration, is no current build — fail-closed and reported.
**If you disagree:** a mutable flag needs an atomic update mechanism the sanctioned rail does not
provide, and loses the ability to ask what was considered current last Tuesday.
**Confidence:** high.

### Q12. What does the load ledger record, and when? -- DECIDED: exactly ONE TERMINAL RECEIPT per `(request_id, table_name)`, after reconciliation, with `rows_expected` derived from the request and the source. Retry state lives outside the lake.
**Blast radius:** MEDIUM
**Why:** Three defects the previous round's confront found, all still applicable. An append-only
`reconciled = FALSE` row **permanently poisons its request**, because append-only data cannot retract
it — terminal-receipt-only fixes that, since an unreconciled table simply has no receipt, which
already means not publishable. `rows_expected` must come from outside the write path or the ledger
compares a number to itself. And completion must cover the declared inventory **including zero-row
tables** while excluding the ledger reconciling itself and independently-authored curation — otherwise
a missing curator blocks archival forever. The four experimental tables are declared **optional**
because KBDL has no fitness producer, so a request carrying none of it reconciles at zero rather than
failing.
**If you disagree:** per-shard progress rows in the lake make the ledger mutable, contradicting Q6.
**Round 3 (stalls 15, 16):** `load_request.status` is removed and derived from receipts;
`request_genome_membership` makes a re-request's completeness durable; the receipted inventory is a
named constant; `rows_written` is an independent recount; the lock is per namespace, not per request
digest; ambiguous commits are recovered by re-reading by full logical key.
**Confidence:** high.

### Q13. Where does consensus across tools live? -- DECIDED: `feature_consensus` in the companion, per `(build_id, feature_id)`, with a MANDATORY `rule_id`/`rule_version`.
**Blast radius:** MEDIUM
**Why:** Per-occurrence even though each tool's call is per-sequence, because the *inputs* are
per-occurrence: which tools ran on this build. The mandatory rule identity is the lesson from
`fitness.annotation`, which carries `category`, `verdict`, `u_gene` and `n_hypotheses` across 272,076
rows with **no record of the rule that produced them** — making every verdict uninterpretable, on top
of the table being 100% orphan. A consensus value without its rule is not a finding.
**If you disagree:** consensus could be computed on read from the clearinghouse results, which keeps
it always-fresh and makes it uncacheable and expensive; or it could be a clearinghouse `*_result` row
under a `consensus@1` type, which would be wrong because consensus depends on which tools ran on a
*build*, not on the sequence.
**Confidence:** medium — the shape is right, but I have not specified the consensus *rule itself*,
deliberately. `rule_id` makes the rule pluggable and versioned; which rule to run first is a separate
decision with its own evidence, and inventing one here would be the same mistake `fitness.annotation`
made.

### Q14. Can a curator override a tool result? -- DECIDED: yes, via append-only `feature_curation` with explicit supersession. Nothing is ever deleted or overwritten.
**Blast radius:** MEDIUM
**Why:** Existence in an archive is not "is a result", and a correction must not destroy the evidence
it corrects — both because the evidence is often still useful and because an unexplained correction is
indistinguishable from a bug. `supersedes_curation_id` makes a chain of corrections inspectable, and
`assertion_type` is a closed vocabulary so a curation cannot mean something the reader has to guess.
**If you disagree:** allowing in-place correction is simpler and loses the audit trail; omitting
curation entirely means every correction becomes a reload.
**Round 3 (stall 17):** supersession is per stream `(build_id, feature_id, assertion_type)`; forks,
cycles, missing predecessors and cross-stream references are rejected; one head per stream.
**Confidence:** medium — this table is new in this revision and has no precedent on the platform to
copy, which is exactly the condition under which I am least confident.

### Q15. What does `feature_type` carry? -- DECIDED: one column with SEVEN values -- `CDS` plus `NonCodingType`'s six.
**Blast radius:** MEDIUM
**Why:** The handover brief said five; the source says six — `repeat_region` was added for Bakta
calling and is a CRISPR array, not an RNA gene. A column rather than a table because the distinction
is one closed enumerated attribute, and a lookup table would add a join to the largest table in the
namespace to answer "is this a gene". The finer SO subtypes are deliberately not members and fold to
`ncRNA` with the raw subtype in `ncrna_class`, keeping the enum stable in a store that cannot delete.
**If you disagree:** admitting the SO subtypes as first-class values contradicts
`map_non_coding_type`, which rejects rather than coerces precisely so an unrecognised annotation is a
loud error instead of a silently reclassified gene.
**Confidence:** high — read from `NonCodingType`, including the sixth member the brief omitted. The
round-1 adversary explicitly found no blocker here.

### Q16. Where do RNA qualifiers live? -- DECIDED: seven named nullable columns on `genome_feature` PLUS a `feature_qualifier` sidecar whose `value` is CANONICAL JSON for every type.
**Blast radius:** MEDIUM
**Why:** The named qualifiers are what *define* a tRNA, so they must be retrievable without a join on
the largest table; sparsity is nearly free in Parquet with ZSTD. But `CanonicalNonCodingFeature` sets
`extra="allow"` precisely so an annotated source's unanticipated qualifiers survive, and named columns
alone would drop exactly those — a data-loss bug no test would catch, because the dropped keys are by
definition the ones nobody enumerated. Canonical JSON rather than `str(value)` because `str()` cannot
distinguish a source string from serialized structure nor round-trip an array.
**If you disagree:** pure EAV costs a join for every tRNA query and loses typing; named-only silently
drops passthrough.
**Confidence:** high on the shape; medium on the Parquet null-cost claim, which is well-established
for the format but which I did not measure on this deployment's writer.

### Q17. Where does caller provenance live, and what is a zero-hit run? -- DECIDED: three places -- per-feature `caller`/`caller_version`, one `genome_build_provenance` row per build, and one `genome_caller_run` row per caller INCLUDING zero-hit runs, where `features_called` counts RETAINED features.
**Blast radius:** MEDIUM
**Why:** Three different facts, and the third cannot be reconstructed from the other two.
`NonCodingProvenance.callers` deliberately includes callers that ran and found nothing, because a
later rebuild could not otherwise tell a genuinely tRNA-free assembly from one built before tRNA
calling existed. `features_called` counts retained features because counting survivors as raw hits
would label a caller zero-hit when filtering removed everything it found — a different fact and the
opposite of the table's purpose. A missing historical version stays NULL with a quality diagnostic;
inventing provenance is worse than admitting its absence. The live warning:
`fitness` **has** a `source_version` column on both annotation tables and it is **NULL in 100% of
2.1M rows**. A column does not make provenance happen; the write-boundary rejections do.
**If you disagree:** folding all three into a JSON blob makes "which genomes ran pyaragorn 0.3.0"
unqueryable, and dropping `genome_caller_run` loses the zero-hit fact permanently.
**Round 3 (stall 13):** `caller_version` is mandatory except for `caller = 'source'` and for
historical objects with no recorded version; both are NULL with a coded entry in
`genome_build_provenance.diagnostics_json`. `features_called = 0` means nothing survived filtering,
not that nothing was found.
**Confidence:** high — the three-way split mirrors the source model exactly.

### Q18. How do consumers select science? -- DECIDED: three projections (`published_genome`, `published_feature`, `published_gene_annotation`); raw tables are archival inputs.
**Blast radius:** MEDIUM
**Why:** Without them the protections in this document are *admonitions*: one could implement every
table exactly and still have a query count a CRISPR array as a gene, read an unpublished genome, or
mix two builds. The projections apply publication, currency and completeness **before any join**, and
`published_gene_annotation` is where the `fitness.function_annotation` shape is reconstituted — so
Chris's "a scheme similar to the fitness DB" is satisfied at the read surface while Q7 keeps the
storage non-duplicated. `published_gene_annotation` excludes `repeat_region`;
`published_feature` includes it.
**If you disagree:** leaving consumers to compose the filters guarantees some consumer gets one wrong,
and a wrong answer looks like data rather than a mistake.
**Confidence:** high on the need. **Medium on the mechanism** — the projections are SQL views, and if
views are not creatable through the sanctioned rail the rollout stops (Q22, round 3); the smoke task
settles which.

### Q19. Who loads, and in what order? -- DECIDED: the existing GAA-free `kbdl_service/clearinghouse/` module, extended; three ordered phases -- entities and content, then per-sequence results, then the companion occurrence layer. `KBDLStoreLoad`'s submission path is retired but its historical records stay readable.
**Blast radius:** MEDIUM
**Why:** The ordering is a correctness requirement: a `genome_feature` row whose `gene_entity_hash` is
absent from `gene_entity` is a dangling reference, so phase 1 completes for a genome before phase 3
writes its features. The module choice follows from `tests/test_layering.py` INVARIANT 1, which
forbids that package from importing GAA — the property that makes GAA's retirement stop blocking the
lake. `KBDLStoreLoad` has run **zero times** (confirmed by absence of any `jobs` row of any state
against 4,891 and 3,341 completions for the other two types), so there is no incumbent behaviour —
but deleting the type outright would break deserialization of historical `jobs` rows, which is a
different public behaviour from refusing new submissions.
**If you disagree:** a push from KBDL gets the job system's retry and leasing for free and puts an
expiring KBase credential on every load's critical path.
**Confidence:** high.

### Q21. Who builds the delivery, and what crosses the KBDL/KBUtilLib boundary? -- DECIDED: KBDL produces (phases 1-2 plus a delivery with an `entity_index`), KBUtilLib consumes (manifest, arrival, deadline, retry inventory) and writes phase 3; KBDL is the only hasher; the manifest is versioned.
**Blast radius:** MEDIUM
**Why:** round 2 specified the delivery and assigned no task to build it, and never said how the
KBUtilLib phase-3 writer learns the clearinghouse hashes KBDL computed (stall 12). Recomputing them in
KBUtilLib would be a second standardizer implementation, and two hashers drift silently — a drifted
hash is a dangling reference that looks like a valid one. Keeping the producer beside the store and
the consumer beside the companion follows each repo's existing layering (KBDL's clearinghouse package
may not import `kbutillib`).
**If you disagree:** a single-repo loader means either KBDL importing `kbutillib` (breaking
`tests/test_layering.py` INVARIANT 1) or KBUtilLib reading KBDL's SQLite store directly; either
removes tasks `gc2-12` and `gc2-13` and rewrites `gc2-6` and `gc2-8`. Once a delivery has been
consumed in production, `delivery_schema_version` 1 is a contract with a reader, and changing it means
a version 2 plus a consumer that accepts both.
**Confidence:** medium — the split follows the layering, but the manifest has never carried a real
delivery, and the per-genome delivery size (Q20) is still unmeasured.

### Q22. What happens if SQL views cannot be created through the sanctioned rail? -- DECIDED: the rollout STOPS. There is no materialized fallback.
**Blast radius:** MEDIUM
**Why:** round 2's fallback (materialized projection tables rebuilt per request) contradicted three
of this namespace's own rules (stall 9): append-only rows cannot un-publish a genome, a per-request
rebuild misses declarations made between requests, and "never drop or replace a live table" forbids
the rebuild itself. A versioned projection snapshot with an atomic reader pointer would work, but it
is a design of its own, and designing it for a case the phase-1 smoke may show does not exist is
speculative.
**If you disagree:** specify a versioned projection-snapshot table plus a reader-pointer declaration,
its refresh triggers (each terminal receipt and each currency or disposition declaration) and its
recovery after a failed refresh; that adds a task after `gc2-9` and changes `gc2-5`'s projection
deliverable from SQL views to a refresher.
**Confidence:** medium — I have no measurement of view support on this rail; `gc2-1` produces it.

### Q20. What I could not decide and did not guess

Seven external facts are unreconciled. None blocks the BUILD; items 6 and 7 block PRODUCTION
LOADING.

1. **Whether `genome_result.payload` is efficiently queryable in this Trino deployment.** Q3 rests on
   it being tolerable, mitigated by `published_genome` flattening. If JSON extraction is slow enough
   to matter at corpus scale, Q3's alternative (quality columns directly on `genome_content`) becomes
   the better call — and it is free to switch **before** the first load, not after.
2. **What is in `kbaseincubator.genome_clearhouse`'s `genome_quality`** — that spelling, a
   *different* namespace from `clearinghouse`. Its role is now known: it is the clearinghouse
   bootstrap's genome SOURCE (`SOURCE_GENOME_CLEARHOUSE_NAMESPACE`, ~5.8M genomes per the operator
   runbook). Its columns were still not read. If they match `genome_quality@1`, Q3's result type
   may duplicate it, and the right move might be to seed `genome_result` from it rather than recompute.
3. **Who reads `fitness` and `inner_loop`.** Measured their contents, not their consumers — which is
   why this delivery changes neither and why no retirement window is promised.
4. **The delivery's byte size.** The loader needs the object closure for the requested genomes; I have
   not measured how large that is per genome, and it bounds how big a single request can practically
   be.
5. **The consensus rule itself** (Q13). `rule_id` makes it pluggable and versioned; which rule to run
   is a separate decision with its own evidence, and inventing one here would repeat
   `fitness.annotation`'s mistake of shipping unexplained verdicts.
6. **Who bootstraps the alias kind into the live lake, and when.** The code is on `main`; the tables
   are not live (re-measured 2026-11-22). The loader refuses production requests until they exist.
   This PRD also adds a `kbdl_genome_id` source to that PRD's closed genome vocabulary; its owner
   should agree, and I did not ask.
7. **Whether the sanctioned rail supports `ALTER TABLE ADD COLUMNS` and `CREATE VIEW`.** Both are
   now hard prerequisites (Change 1's migration, Q22), and `gc2-1` measures them. Either failing
   stops the rollout rather than being worked around.

## Gotchas and Unintuitive Consequences

**A genome's annotations are a three-way join, and that is the deliberate cost of Q7.** "Show me this
genome's functions" is `genome_feature` → `protein_result` → `function_content`, not a single scan.
`published_gene_annotation` hides it, but anyone querying raw tables will feel it, and anyone
benchmarking against `fitness.function_annotation` is comparing a join to a scan.

**Two genomes with identical assemblies are ONE genome here.** Q9 is a regression and it will surprise
someone: loading a knockout mutant and its wild-type parent yields one `genome_entity`, and the second
load looks like a successful no-op rather than an error. The escape hatch is specified in Q9; until it
is built, this is a real limitation and not a bug to be reported.

**A query asking for "genes" gets CRISPR arrays unless it uses the projection.** `repeat_region` is a
`feature_type` value on `genome_feature` and is not a gene. `SELECT COUNT(*) FROM genome_feature WHERE
genome_entity_hash = ?` is the wrong query and will not look wrong.

**Loading a genome twice across two requests writes only membership, and `rows_written = 0` is
success.** Because identity is the content hash, a second request naming an already-loaded genome
writes no fact rows — only `request_genome_membership` rows marked `already_present`. Its receipts
show `rows_written = 0` against `rows_expected = 0` for every fact table. Reading that as a failure
is the predictable misdiagnosis; reading `already_present` as "this request's data" is the other
one, because the rows carry the OWNING request's `request_id`, not the second request's.

**A genome whose first request never completed is re-written by the next request that names it.**
Membership only points at a complete owner. So an interrupted request's partial rows are not
deleted (nothing is), but a later request writes the same rows by full logical key, identical
payloads deduplicate, and the later request becomes the owner. Two `request_id`s then hold the same
facts in the raw tables; the projections read only the complete owner.

**One writer per namespace serializes every load into it.** The lock is per `(namespace,
schema_version)`, so a large request blocks a small one. That is the price of making concurrent
inserts of one global build impossible; per-user instances (Q5) are the intended way to get
parallelism.

**A genome can silently drop out of `published_feature`.** Currency is fail-closed: a genome with
two builds both marked `source_designated_current` and no declaration has NO current build, so it
disappears from the feature projection while staying in `published_genome`. The projection reports
it; anyone not reading that report sees a genome with metadata and no genes.

**`genome_content.taxon_id` is fill-only, so it goes stale.** It is the taxon believed when the
genome was first registered. A later `genome_taxonomy@1` result with a different answer does not
update it, and nothing will. Use `published_genome`'s taxonomy columns for anything that matters.

**Fragment status is no longer on the protein.** A consumer looking for "is this protein a
fragment" in `protein_content` finds nothing: it is `genome_feature.partial`, per occurrence, because
one sequence can be partial in one genome and complete in another.

**The companion trusts KBDL's hashes.** `entity_index.jsonl` is the only source of the gene and
protein hashes on `genome_feature`. The loader checks that each one exists in the clearinghouse, so a
corrupt index fails loudly, but a KBDL standardizer bug would propagate unchallenged: there is no
second hasher to catch it, by design (Q21).

**A rebuild grows the companion without adding a genome.** Build scoping means re-annotating loaded
genomes under a second caller doubles `genome_feature`, `feature_qualifier`, `genome_build_provenance`
and `genome_caller_run` while leaving `genome_entity`, `genome_content` and `contig` untouched. "Rows
in `genome_feature` over genomes loaded" is therefore not a mean gene count and drifts upward for
reasons unrelated to biology.

**`build_id` changes on enrichment, so annotations do not follow.** `structural_features_digest`
covers every feature, so adding non-coding features mints a **new build** rather than amending the
old one — which is what keeps the two feature sets distinguishable, and which also means
`feature_consensus` and `feature_curation` rows attached to the old build do **not** apply to the new
one. Carrying curation forward across an enrichment is a deliberate operation nobody has specified;
it is in Out of Scope.

**Extending `_CONTENT_TYPE_HEAD` changes the DDL for tables that may already exist elsewhere.** The
genome/gene/protein tables are empty *in this instance*. Any other deployment that bootstrapped the
clearinghouse earlier has the old shape, and the bootstrap adapter **refuses** on a partitioning
mismatch but says nothing about a missing column — so a stale deployment will accept writes and
silently drop the new fields. Round 3 makes the bootstrap compare the full column list and migrate
by additive `ALTER TABLE`, but **only an EMPTY table migrates**: a deployment that already loaded
genomes under the old shape is refused, and backfilling it is a separate operation nobody has
designed.

**`function_content` is the only table with rows, so it is the only one where a careless migration
costs something.** 47,341 rows. This design does not touch it, and the test suite asserts that
explicitly — because "extend the content heads" is exactly the kind of instruction that gets applied
uniformly to all five types by someone being tidy.

**Per-sequence annotation means a protein's annotation can appear before any genome containing it.**
Phase 2 writes `protein_result` keyed on the sequence hash. If a later request loads a genome
containing an already-annotated protein, that genome's features arrive pre-annotated — which is the
intended saving, and which also means "when was this annotated" is a property of the sequence, not of
the genome, and will not match the genome's `loaded_at`.

**`assembly_level` is derived, so it can disagree with an external catalogue — and it never says
`chromosome`.** It is computed from closure and N-runs by the rule in Change 1, and `chromosome`
needs submitter knowledge, so a query for `assembly_level = 'chromosome'` returns nothing however
many chromosome-level NCBI assemblies are loaded. NCBI's own assembly level for the same
accession may differ, because theirs reflects submitter intent and ours reflects the bytes. Storing it
is still right — otherwise every consumer re-derives it differently — but it is not authoritative
against NCBI and must not be used to join to NCBI's value.

**Taxonomy as a result means a genome can have two taxonomies at once.** Two skani runs against
different reference databases produce two `genome_result` rows, both facts. `published_genome` picks
the latest by `observed_at`, which is a *convention at the read surface*, not a statement that the
older call was wrong.

## Sources Consulted

**Round 3 (Jane, primary-laptop, 2026-11-22):**
- Live clearinghouse re-measure at 23:43 CST via `KBBERDLUtils` (counts in the Research Report). The first
  attempt failed 401 on every call, `SELECT 42` included: a stale `berdl`-namespace token in `~/.tokens`
  (sha8 `c646faf2`) outranked the fresh `~/.kbase/token` (sha8 `3de59074`).
- KBUtilLib `main` 65db6ea `clearinghouse_schema.py`: `table_configs()` docstring (twenty-one configs),
  `_GENOME_ONLY_ALIAS_SOURCES` (no KBDL genome-id source), `_RESULT_COLUMNS`, `CLEARINGHOUSE_NAMESPACE` as a
  module constant, `SOURCE_GENOME_CLEARHOUSE_NAMESPACE` and its docstring.
- KBDLJobRunningPrototype `main` 3ffd1c4: `clearinghouse/store.py` result dataclasses (no function-hash
  field anywhere in `clearinghouse/`; no existing annotation result type), and the `span` / `kbase_location`
  definitions in `kbdl-file-load-and-genome-sources-v1`'s taskplan.
- Confront round 2 stall report, `task-39f82bc7` (blob `97604b9`, commit `50f5517`, path absent on `main`
  65db6ea at dispatch).

**Live lakehouse reads (h100, via `kbutillib` `KBBERDLUtils`):**
- **2026-10-27:** all 19 `kbaseincubator.clearinghouse` tables, `SELECT COUNT(*)` each — the open-window
  measurement (finding 2). `genome_*`, `gene_*`, `protein_*`, `ontology_term_*` all 0;
  `function_entity`/`function_content` 47,341; `function_result` 20,036; `parameter_set` 3.
- **2026-10-25:** all 19 `kbaseincubator.fitness` tables for columns and row counts; `SHOW CREATE
  TABLE` and `"<t>$partitions"` for `gene`, `genome`, `assembly`, `function_annotation`,
  `propagated_fitness` (the no-partitioning and file-count findings); eleven integrity and vocabulary
  queries (duplicate `assembly_hash`; genome→assembly orphans; `annotation`→`gene` orphans;
  `evidence` null-score and distinct-id counts; `source_version` null counts; `tool` and
  `evidence_type` group-bys; `ontology_term.namespace`; `essentiality.call_source`; duplicate
  `(genome_id, locus_id)`); `kbaseincubator.inner_loop`'s 39 `v2_*` tables; the 123-namespace
  catalogue.
- Three assemblies fetched from KBase workspace 262939 (`262939/41/3`, `/27/2`, `/85/3`) via
  `WorkspaceClient.get_objects2` + `KBWSUtils.download_blob_file`, with
  `standardizers.entity_hash("genome", parse_fasta_contigs(raw))` and `sha256(raw)` computed over the
  real bytes — the measurement that settled the hash rule.

**Live KBDL store (`/scratch/kbdl/kbdl.sqlite`, read-only):** `genomes` schema and counts (4,865 rows,
4,865 distinct `content_hash`, 0 null `genome_object_id`); the `genome_id` format
(`unk1`…`unk4865`, 100% matching `[A-Za-z_]+\d+`, contiguous, lexicographic bucket skew measured);
`jobs` by `(job_type, state)` — `KBDLBuildGenome` 4,891/10, `KBDLGenomeAnnotation` 3,341/33, **no
`KBDLStoreLoad` row of any state**; `object_supersedes` 28,051 rows all `atb-recompression-v1`, 0 join
hits against the genome registry on either id, 0 chains of depth > 1.

**KBUtilLib `main` source:**
- `domains/kbase/berdl/clearinghouse_schema.py` — `_CONTENT_TYPE_HEAD` (all five heads, read in full
  and quoted in finding 1), `_ENTITY_COLUMNS`, `_RESULT_COLUMNS`, `_CONTENT_COMMON_TAIL`,
  `_PARAMETER_SET_COLUMNS`, `ENTITY_TYPES`, `_ENTITY_PARTITION_BY`, `_CONTENT_PARTITION_BY`,
  `table_configs`, and the module docstring's partitioning rationale and no-parenthesis guard.
- `domains/kbase/berdl/clearinghouse_bootstrap_adapter.py` — `_create_table_sql`, the
  `PARTITIONED BY (\`col\`)` rendering that bounds what a partition spec can be.
- `domains/kbase/berdl/clearinghouse_shard.py` — the range-cutting algorithm, `_key_spec` (:5360),
  `_range_boundaries` (:5266), `_row_bytes` (:5301), `_write_parquet` (:5314), and the explicit
  statement that sort order and table properties are not expressible through this write path.
- `domains/kbase/berdl/capability.py` — `_normalize_partition_by`, and `load`'s append/overwrite
  surface (:458).
- `domains/kbase/berdl/clearinghouse_parameters.py` — the per-tool parameter validator; TRANSYT
  requires `taxonomy_id` and rejects an unidentifiable default (:184).
- `agent-io/docs/clearinghouse-schema-operator-runbook.md` — dotted vs bare namespace strings.
- `agent-io/prds/clearinghouse-entity-alias-v1/fullprompt.md` (**landed on `main`**) — the
  per-type `<type>_alias` table shape, `alias_source` partitioning, the per-entity-type legal
  alias_source values, and the invertible-source set. NOTE its central decision was REVERSED by Chris
  from one generic table to five per-type tables; the landed code on `main` is the per-type shape.
- `agent-io/prds/council-record-lakehouse-v1/fullprompt.md` — the store-identity rule, the ledger and
  ambiguous-commit pattern, the delivery rail and its four corrections (`council_archive.py:10`,
  `:29`, `:748`, `:942`, `:1356`).

**KBDL source:**
- `schemas/build_genome.py` — `NonCodingType` (six members), `CanonicalNonCodingFeature`,
  `NonCodingProvenance` (nine fields), `CanonicalFeature`, `CanonicalContig`, `CanonicalGenome`,
  `TaxonomyBlock`, `CoordinateConvention`, `_NCRNA_SUBTYPES`, `map_non_coding_type`.
- `schemas/checkm2.py` — `CheckM2Quality`'s thirteen fields and the `"None"`→`None` normalisation of
  `additional_notes` (finding 6).
- `genome_normalize.py` — gene id minting at the two sites (`{genome_id}_{N}` at :2105,
  `{genome_id}_nc{N}` at :2161) on independent counters.
- `genome_registry.py` — `content_hash` dedup semantics, `_lookup_existing_genome_id`.
- `clearinghouse/policy.py` — `EntityTypePolicy`, `stores_content` per type (genome `False`; protein,
  gene DNA, function, ontology term `True`).
- `clearinghouse/result_types.py` — the `clearinghouse_result_types` registry
  (`tag`, `version`, `schema_json`, `entity_types`), finding 5.
- `clearinghouse/__init__.py` — the GAA-free layering invariant and its measured rationale
  (importing `kbutillib` costs 2,252 modules / 2.41 s).
- `job_types/store_load.py` — the GAA imports (`Config`, `ConstrainedStoreV2`,
  `kbdl_loader.GeneIdMap`, `v2_ingest`, `staging_writer`, `store_engine`).

**Read and found uninformative, or found to be wrong:**
- `KBBERDLUtils.table_configs` — **does not exist.** The handover brief instructed measuring `fitness`
  partitioning with it; `table_configs` is a `clearinghouse_schema` DDL emitter for *our* tables, not
  generic introspection. Partitioning was measured with `SHOW CREATE TABLE` and `$partitions`.
- `DESCRIBE kbaseincubator.fitness.<t>` — returns an empty result set through this endpoint; not a
  usable introspection path.
- `kbdl_service/store_load.py` — does not exist at the brief's path; the module is
  `kbdl_service/job_types/store_load.py`.
- **The handover brief's claim that `genome_id` is "the truncated assembly content hash"** — false,
  measured. It is `prefix + decimal ordinal`. Recorded because two decisions in the previous draft
  rested on it.

## Out of Scope

- **Per-user companion instances.** Chris deferred them explicitly. Q5 parameterises the namespace so
  they need no schema change; no ACL model, quota, or cross-instance query is designed.
- **`clearinghouse.genome_alias` itself** — designed and code-landed as
  `clearinghouse-entity-alias-v1` (whose central decision Chris reversed from one generic table to
  five per-type tables). This PRD *uses* `genome_alias` and deletes its own alias table; it does not
  redesign it. Its author's finding that `ncbi_taxid` is a membership relation rather than an
  invertible alias is resolved here by removal.
- **Bootstrapping the alias tables into the live lake.** The code is on `main`; the tables are not in
  the namespace (19 tables, no alias table, measured 2026-11-22). The companion loader's alias write
  has no target until that bootstrap runs, and this PRD does not own it. **This is a hard
  precondition on the loader task, not a nice-to-have.**
- **A surrogate genome key for distinct genomes sharing an assembly.** Q9 takes the regression
  deliberately and specifies the escape hatch (`genome_instance`); building it now would be
  speculative, since nothing can currently produce such a pair.
- **The consensus rule.** Q13 makes it pluggable and versioned; choosing it needs its own evidence.
- **Carrying curation forward across an enrichment.** A new `build_id` means `feature_consensus` and
  `feature_curation` rows do not follow; specifying that migration is deliberately left out.
- **Contig as a sixth clearinghouse entity type.** Named as the alternative in Q10 and not taken; it
  touches `ENTITY_TYPES`, all three kinds, the policy table and the union views.
- **Retiring `fitness` or `inner_loop`, and any compatibility window.** This delivery is purely
  additive to both. Retirement needs a reader inventory this PRD does not have and `fitness`'s writer
  identified; it belongs to a separate migration PRD.
- **Rewriting `kbaseincubator.fitness` in place**, including its file-count pathology. Superseded, not
  repaired.
- **GA4GH seqcol adoption.** `genome-identity-v1` Q13 left it open and lake-5 D5.1 holds. The external
  scan's finding that seqcol explicitly rejected sorted-concatenated-hash as a primary identifier is
  recorded against that future decision rather than acted on, because rule A is already the
  clearinghouse identity fleet-wide and changing it would re-key the clearinghouse, not this namespace.
- **`kbaseincubator.genome_clearhouse`** (`genome_quality`, `skani_distances`) — identified as the
  bootstrap's genome source, columns not read, not claimed, not ruled out. Q20.2.
- **A versioned projection-snapshot mechanism.** Views are a hard prerequisite (Q22); the fallback is
  designed only if `gc2-1` shows views cannot be created.
- **Backfilling a deployment that loaded genomes under the old content shape.** The migration
  handles empty tables only.
- **Build-scoped attribution of per-sequence annotation results** (Q7, round 3).

## Further Notes

**On why this is a better design than the two drafts before it.** The first two drafts treated the
clearinghouse as a neighbour to join to and then quietly rebuilt its genome layer alongside it —
`assembly`, `genome`, `genome_alias`, `function`, `ontology_term`, and per-feature annotation tables
that would have multiplied one fact by the number of genomes carrying the sequence. Each of those
individually looked like a reasonable table. The duplication was only visible once the question
"what does the clearinghouse already hold?" was asked against the source rather than against memory —
`_CONTENT_TYPE_HEAD` turned out to be exactly the per-entity-type customization mechanism the brief
described as missing.

**The one load-bearing idea.** A content-addressed entity store makes *identity* and *sequence facts*
free to share and impossible to duplicate, and it makes *location* impossible to express. A companion
that holds only what the hash cannot — the occurrence, the build that produced it, the consensus over
it, and the experiment performed on it — is complementary rather than parallel. Every table in this
revision either holds an occurrence fact, a genome-scoped provenance fact, a per-occurrence judgement,
or an experimental measurement. If a future table does not fit one of those four, it probably belongs
in the clearinghouse.

**What `fitness` got right and is preserved.** Separate protein and DNA sequence references with a
typed sequence store, so a non-coding gene is a first-class row rather than a protein-coding gene with
a null — that survives here as `gene_entity_hash` always set and `protein_entity_hash` null iff not
CDS. The narrow one-row-per-(gene, assignment, tool) annotation shape, rather than tool-as-column —
that survives as `published_gene_annotation`, reconstituted by join instead of stored. And the
singular domain table names, which this revision keeps.

**What `fitness` got wrong and is not repeated.** A wide tool-shaped `annotation` table that does not
generalise and is 100% orphan; an `evidence` satellite with 2.5M rows and zero information; verdicts
with no record of the rule that produced them; a `source_version` column populated in zero rows; four
empty tables that were never wired; and no contig table at all, leaving `gene.contig_id` pointing at
nothing.

## Acceptance Criteria

1. The phase-1 smoke records a LIVE pass for typed round-trip, partition landing, a column named `end`, `ALTER TABLE ADD COLUMNS` and view creation; a skipped or inconclusive smoke blocks every later task.
2. `table_configs()` returns 21 configs, the five alias configs byte-identical to the base commit.
3. Every Change 1 derivation rule (`assembly_level`, `gc_content`, `n_ambiguous_bases`, `contig_n50`, min/max contig length, gene `gc_content`, `md5`) matches its golden inputs and outputs, including the NULL cases.
4. `protein_content` has no `is_fragment` column; fragment status is `genome_feature.partial`.
5. `genome_quality@1`, `genome_taxonomy@1` and `function_call@1` are registered with the exact schemas in Change 2, and each rejects a missing required field, an out-of-bound value and an unknown field.
6. Task dependencies: `gc2-3` depends on `gc2-1`; `gc2-6` on `gc2-2` and `gc2-3`; `gc2-8` on `gc2-6` and `gc2-13`; acceptance on `gc2-12`.
7. The loader refuses a whole request, naming the table, before any write when `genome_alias` is absent or the content tables lack the Change 1 columns.
8. `request_id` appears exactly once in the DDL of `load_request`, `request_genome_membership` and `load_ledger`; `load_request` has no `status` column.
9. The projections implement the normative rules: latest-result ordering and tie-breaks, fail-closed currency, the `function_call@1` join to `function_content`, and orphan reporting.
10. If views cannot be created, the rollout stops; no materialized projection table is created.
11. `kbdl_genome_id` exists as a store-scoped, non-invertible genome alias source, written only with `store_id`.
12. Existing empty old-shape content tables migrate by additive `ALTER TABLE` after a same-run zero-row recheck, followed by a full column-list comparison; a non-empty table is refused.
13. Per-sequence results deduplicate on `(entity_hash, result_type, result_type_version, source, parameter_set_hash)`; an identical identity with a different payload is rejected.
14. KBDL produces a delivery (sqlite3-API backup, object closure, `entity_index.jsonl`, manifest last) and KBUtilLib consumes it with arrival, digest, deadline and `delivery_schema_version` checks and a retry inventory outside the lake.
15. The companion loader takes every clearinghouse hash from `entity_index.jsonl`, never recomputes one, and refuses any hash absent from the clearinghouse entity tables before writing.
16. `caller_version` is NULL only for `caller = 'source'` or a historical object without one, each with a coded `diagnostics_json` entry; any other missing version is rejected.
17. `structural_features_digest` follows the normative preimage, coordinates are stored 1-based inclusive with the conversion table applied, out-of-contig features are rejected, and golden vectors cover minus-strand features under both source conventions.
18. Every table enforces the full logical key in the Implementation Decisions table, rejecting a duplicate key with a different payload.
19. A re-request writes `already_present` membership and is complete; a genome owned by an incomplete request is re-written and re-owned; a rebuild writes a new `genome_build`.
20. The receipted inventory is the named constant; `rows_written` is an independent post-commit recount; the writer lock is per `(namespace, schema_version)`; ambiguous commits are re-read by full key before any retry.
21. Curation supersession is per `(build_id, feature_id, assertion_type)` stream, rejecting forks, cycles, missing predecessors and cross-stream references, with one head per stream.
22. Acceptance writes only dated scratch namespaces for the clearinghouse and the companion, selects fixtures by a recorded query (synthetic substitutes reported as synthetic), fixes thresholds before running, reports the Q3 query-cost measurement without a threshold, and repeats the layout check over a second incremental request.
23. No production load runs until Chris has recorded his Q3 decision after the acceptance measurement.
24. Every task's `success_criteria` names each mandatory externally observable behaviour, negative cases and prerequisites included, and keeps the base-commit regression comparison.
