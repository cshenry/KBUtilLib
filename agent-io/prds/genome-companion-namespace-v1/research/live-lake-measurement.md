# Live lake and KBDL measurement

Every number quoted in the PRD, with the query that produced it. All reads are from h100 on
2026-10-25 between 22:15 and 22:24 CDT. The lake reads go through `kbutillib`'s `KBBERDLUtils`
against `https://hub.berdl.kbase.us/apis/mcp/delta`; the KBDL reads are read-only against
`/scratch/kbdl/kbdl.sqlite` (15,0 GB).

## How to reproduce the lake reads

The `kbu-env.sh` wrapper hardcodes the `council` subcommand and cannot run other verbs. Use:

```bash
export ARGO_USER=chenry
export AMSC_TOKEN="$(cat $HOME/.config/amsc/token)"
env -u PYTHONPATH -u VIRTUAL_ENV -u PYTHONHOME TMPDIR=/scratch/chenry/tmp \
  /home/chenry/venvs/kbdl/bin/python <script>
```

inside the script, `from kbutillib.domains.kbase.kb_berdl_utils import KBBERDLUtils`
(the flat `kbutillib.kb_berdl_utils` path is a deprecated shim and warns).

Two traps worth recording:

- **Results are DICTS, not bare lists.** `get_database_tables` returns `{"tables": [...]}`,
  `get_table_columns` returns a dict, and `query` returns
  `{"success": ..., "data": [ {col: val} ], "columns": [...], "row_count": ...}`. A probe that
  treats them as lists silently reads nothing.
- **`KBBERDLUtils.table_configs` does not exist.** The handover brief instructed measuring
  partitioning with it. `table_configs` is a `clearinghouse_schema` DDL emitter for *our own*
  sixteen clearinghouse tables, not generic namespace introspection. Partitioning was measured with
  `SHOW CREATE TABLE` and the Iceberg `$partitions` metadata table instead.
  `DESCRIBE <ns>.<table>` returns an empty result set through this endpoint and is not usable.
- The KBase token (`~/.kbase/token`) expires on roughly a 27-day cycle. It was refreshed
  2026-10-25 00:22 and was live for these reads. A read failure is probably the credential.

## `kbaseincubator.fitness` — 19 tables, columns and row counts

`get_database_tables("kbaseincubator.fitness")` → 19. Note `gene_function`, which the handover
brief's list omitted while stating the count as 19.

| table | rows | columns |
|---|---|---|
| `annotation` | 272,076 | `feature_id, category, verdict, n_methods, n_hypotheses, u_gene, top_hypothesis, rast, ko, bakta, organism` |
| `assembly` | 47 | `assembly_hash, fasta_ref, size, n_contigs, longest_contig, created_at` |
| `condition` | 231 | `condition_id, name, description, group, media, modelseed_compound_id, temperature, ph, aerobic, created_at` |
| `essentiality` | 12,908 | `essentiality_id, gene_id, is_essential, call_source, source_ref, evidence, created_at` |
| `evidence` | 2,504,043 | `evidence_id, evidence_type, evidence_score, evidence_probability, created_at` |
| `experimental_fitness` | 3,441,018 | `experimental_fitness_id, gene_id, condition_id, fitness, t_score, provenance, source_ref, created_at` |
| `function` | 98,711 | `function_id, description, created_at` |
| `function_annotation` | 1,118,422 | `function_annotation_id, gene_id, function_id, tool, evidence_id, source_version, created_at` |
| `function_term` | 9,200 | `function_term_id, function_id, ontology_id, mapping_type, created_at` |
| `gene` | 228,709 | `gene_id, genome_id, locus_id, contig_id, start, end, strand, protein_seq_hash, dna_seq_hash, created_at` |
| `gene_alias` | **0** | `gene_id, alias, type` |
| `gene_function` | **0** | `gene_id, consensus_term_id, n_tools, n_agree, agreement, verdict, created_at` |
| `genome` | 48 | `genome_id, orgid, source, genus, species, strain, taxonomy_id, assembly_hash, created_at` |
| `meta` | **0** | `key, value` |
| `ontology_annotation` | 1,010,656 | `ontology_annotation_id, gene_id, ontology_id, tool, evidence_id, source_version, created_at` |
| `ontology_term` | 20,762 | `ontology_id, namespace, term, label, definition, lineage` |
| `propagated_fitness` | 14,076,257 | `propagated_fitness_id, gene_id, condition_id, max_fitness, min_fitness, average_fitness, n_fitness_hits, created_at` |
| `sequence` | **0** | `seq_hash, seq_type, sequence, length` |
| `transyt_annotation` | 374,965 | `transyt_annotation_id, gene_id, reaction_equation, tc_number, substrate, evidence_id, source_version, created_at` |

Four tables are empty: `gene_alias`, `gene_function`, `meta`, `sequence`.
`sequence` being empty against 228,709 genes is the load-bearing one — the "typed sequence table"
the scheme appears to offer has never been exercised.

## Partitioning: there is none

`SHOW CREATE TABLE kbaseincubator.fitness.gene`:

```sql
CREATE TABLE kbaseincubator.fitness.gene (
   gene_id varchar, genome_id varchar, locus_id varchar, contig_id varchar,
   start bigint, "end" bigint, strand varchar,
   protein_seq_hash varchar, dna_seq_hash varchar,
   created_at timestamp(6) with time zone
)
WITH (
   compression_codec = 'ZSTD',
   format = 'PARQUET',
   format_version = 2,
   location = 's3a://cdm-lake/tenant-sql-warehouse/kbaseincubator/iceberg/fitness/gene'
)
```

**No `partitioning` clause.** Same for `genome`, `assembly`, `function_annotation` and
`propagated_fitness` — every one is ZSTD Parquet v2, unpartitioned.

## The file-count pathology

`SELECT * FROM kbaseincubator.fitness."<table>$partitions"` returns one row per table
(consistent with unpartitioned) carrying `record_count`, `file_count` and `total_size`:

| table | records | files | total size | records/file | bytes/file |
|---|---|---|---|---|---|
| `genome` | 48 | **48** | 161,257 | **1.0** | ~3.4 KB |
| `assembly` | 47 | **47** | 115,894 | **1.0** | ~2.5 KB |
| `function_annotation` | 1,118,422 | 2,880 | 41,344,512 | 388 | ~14 KB |
| `propagated_fitness` | 14,076,257 | 2,880 | 723,362,389 | 4,887 | ~251 KB |

One record per file on the two small tables means the writer commits per row. 2,880 files appears
on both large tables, which reads as a fixed writer parallelism rather than anything data-driven.

Scaling 48 genomes to ATB's ~2M is ×41,667: `gene` goes to roughly 9.5×10⁹ rows, and under this
writer the file count goes with it.

`$partitions` also exposes per-column min/max and null counts, which is how the
`source_version`-is-always-null finding below was cross-checked:
`function_annotation.source_version` reports `{"min": null, "max": null, "null_count": 1118422}`.

## Integrity findings

```sql
-- two genomes, one assembly
SELECT assembly_hash, COUNT(*) AS n, ARRAY_AGG(orgid) AS orgids
FROM kbaseincubator.fitness.genome GROUP BY assembly_hash HAVING COUNT(*) > 1;
```
→ one row: `08605af950d1443eb71bd0958cb51d47971f4070f86e4ded04cd733fd83e338a`, n=2,
orgids `["SyringaeB728a_mexBdelta", "SyringaeB728a"]`.
**A knockout mutant and its wild-type parent with a byte-identical assembly.**

```sql
SELECT COUNT(*) AS n_genome, COUNT(DISTINCT assembly_hash) AS n_distinct_ah,
       SUM(CASE WHEN assembly_hash IS NULL THEN 1 ELSE 0 END) AS n_null_ah
FROM kbaseincubator.fitness.genome;
```
→ `48, 47, 0`.

```sql
-- genome -> assembly orphans
SELECT COUNT(*) FROM kbaseincubator.fitness.genome g
LEFT JOIN kbaseincubator.fitness.assembly a ON g.assembly_hash = a.assembly_hash
WHERE a.assembly_hash IS NULL;
```
→ `0`. Every genome's assembly resolves.

```sql
-- annotation -> gene orphans
SELECT COUNT(*) FROM kbaseincubator.fitness.annotation a
LEFT JOIN kbaseincubator.fitness.gene g ON a.feature_id = g.gene_id
WHERE g.gene_id IS NULL;
```
→ **272,076 of 272,076.** The whole table is orphaned.
`annotation.feature_id` is `organism:locus` (`acidovorax_3H11:Ac3H11_1`); `gene.gene_id` is
`{uuid}.{locus}` (`01c3ae47-f41f-4199-a48e-cb4aaff4bbf7.AMB_RS00005`).
`SELECT COUNT(DISTINCT organism) FROM annotation` → 60, against `gene`'s 48 distinct `genome_id`.

```sql
SELECT COUNT(*) AS total, COUNT(evidence_score) AS n_score,
       COUNT(evidence_probability) AS n_prob FROM kbaseincubator.fitness.evidence;
```
→ `2504043, 0, 0`. **No evidence row carries a score or a probability.**

```sql
SELECT COUNT(DISTINCT evidence_id) FROM kbaseincubator.fitness.evidence;
```
→ `2,388,346` against 2,504,043 rows. `evidence_id` is **not unique**.

```sql
SELECT COUNT(*) AS total, COUNT(source_version) AS n FROM kbaseincubator.fitness.function_annotation;
SELECT COUNT(*) AS total, COUNT(source_version) AS n FROM kbaseincubator.fitness.ontology_annotation;
```
→ `1118422, 0` and `1010656, 0`. **A tool-version column exists and has never been populated.**

```sql
SELECT COUNT(*) FROM (SELECT genome_id, locus_id, COUNT(*) c
  FROM kbaseincubator.fitness.gene GROUP BY genome_id, locus_id HAVING COUNT(*) > 1);
```
→ `0`. No duplicate `(genome_id, locus_id)` today.

## `evidence` reconciles exactly to the three annotation tables

| tool | `function_annotation` | `ontology_annotation` | `transyt_annotation` | sum | `evidence` by `evidence_type` |
|---|---|---|---|---|---|
| BAKTA | 286,532 | 412,495 | — | 699,027 | **699,027** |
| DRAM2 | 281,900 | 311,232 | — | 593,132 | **593,132** |
| PROKKA | 328,804 | 147,863 | — | 476,667 | **476,667** |
| RAST | 221,186 | — | — | 221,186 | **221,186** |
| KOFAMSCAN | — | 139,066 | — | 139,066 | **139,066** |
| TRANSYT | — | — | 374,965 | 374,965 | **374,965** |
| **total** | **1,118,422** | **1,010,656** | **374,965** | **2,504,043** | **2,504,043** |

Exact across all six. `evidence` is a strict 1:1 satellite of the union of the three annotation
tables, and its `evidence_type` carries nothing the parent row's `tool` does not already carry.

Other measured vocabularies: `ontology_term.namespace` → `KO` 8,685, `GO` 4,468, `COG` 3,992,
`EC` 3,617. `essentiality.call_source` → a single value, `user_upload:essential_genes.csv` (12,908).
`genome.source` → a single value, `fitnessDB` (48).

Sample `assembly.fasta_ref`: `kbase://workspace/262939/object/41/3` — a KBase workspace URI, not a
KBDL object id and not an `obj://` arc subject.

`created_at` is clustered at two instants: `2026-10-16T03:35:28.924725Z` (genome, assembly,
propagated_fitness) and `2026-10-17T18:07:25.792434Z` (the annotation tables) — two bulk loads.

## `kbaseincubator.inner_loop` — 39 `v2_*` tables

```
v2_annotation_algorithms, v2_annotation_combination_members, v2_annotation_combinations,
v2_assemblies, v2_conditions, v2_council_candidates, v2_experimental_fitness,
v2_fitness_propagation_edges, v2_fitness_simulation_genes, v2_fitness_simulation_reaction,
v2_fitness_simulations, v2_function_classifications, v2_functions, v2_gene_aliases,
v2_gene_annotations, v2_genes, v2_genome_aliases, v2_genome_kbase_objects, v2_genome_sets,
v2_genomes, v2_ingest_ledger, v2_mapping_reaction_functions, v2_mappings, v2_meta, v2_model_fva,
v2_model_gapfill_media, v2_model_reaction_genes, v2_model_reactions, v2_models,
v2_ontology_functions, v2_ontology_terms, v2_ontomap_candidates, v2_pangenome_neighbors,
v2_propagated_fitness, v2_protein_families, v2_protein_family_members, v2_reaction_fva,
v2_reactions, v2_sequences
```

**Roughly twenty of these have no `fitness` equivalent at all** — the modelling layer
(`v2_models`, `v2_model_reactions`, `v2_model_fva`, `v2_model_gapfill_media`, `v2_reactions`,
`v2_reaction_fva`, `v2_mappings`, `v2_mapping_reaction_functions`), the comparative layer
(`v2_pangenome_neighbors`, `v2_protein_families`, `v2_protein_family_members`), the simulation
layer (`v2_fitness_simulations`, `v2_fitness_simulation_genes`, `v2_fitness_simulation_reaction`),
and the candidate/consensus layer (`v2_council_candidates`, `v2_ontomap_candidates`,
`v2_annotation_combinations`, `v2_annotation_combination_members`, `v2_function_classifications`).

So `fitness` is the cleaned-up successor of the **genome/gene/annotation/fitness subset** of
`inner_loop`, not of `inner_loop`.

Two tables worth noting as precedent: `v2_genome_aliases` and `v2_genome_kbase_objects` — the
predecessor scheme *did* carry genome-level identifier tables, which `fitness` dropped.

## `kbaseincubator.clearinghouse` — 19 tables

```
function_content, function_entity, function_result,
gene_content, gene_entity, gene_result,
genome_content, genome_entity, genome_result,
ontology_term_content, ontology_term_entity, ontology_term_result,
protein_content, protein_entity, protein_result,
parameter_set,
all_content, all_entity, all_result
```

15 per-entity-type + `parameter_set` + three `all_*` unions. Matches
`clearinghouse_schema.table_configs()`'s sixteen configs plus the three union views.

## KBDL store (`/scratch/kbdl/kbdl.sqlite`, read-only)

`genomes` schema:
`genome_id, owner, content_hash, prefix, fasta_object_id, genome_object_id, skani_hits_json,
taxonomy_json, created_at, created_by_job_id`

**Four identifiers for one genome in a single row** — `genome_id`, `content_hash`,
`fasta_object_id`, `genome_object_id` — of which `genome_object_id` moves under enrichment.
That is the alias-table requirement, stated by the source schema.

| measurement | value |
|---|---|
| `SELECT COUNT(*) FROM genomes` | 4,865 |
| `SELECT COUNT(DISTINCT content_hash) FROM genomes` | 4,865 (1:1 — no collisions on the KBDL side) |
| `SELECT COUNT(*) FROM genomes WHERE genome_object_id IS NULL` | 0 |
| `SELECT COUNT(*) FROM object_supersedes` | 28,051 |
| `object_supersedes` by `reason` | `atb-recompression-v1` → 28,051 (the only value) |
| genomes whose `genome_object_id` is an `old_object_id` | **0** |
| genomes whose `fasta_object_id` is an `old_object_id` | **0** |
| supersedes chains of depth > 1 | **0** |

`jobs` by `(job_type, state)`, restricted to the three relevant types:

| job_type | state | count |
|---|---|---|
| `KBDLBuildGenome` | completed | 4,891 |
| `KBDLBuildGenome` | failed | 10 |
| `KBDLGenomeAnnotation` | completed | 3,341 |
| `KBDLGenomeAnnotation` | failed | 33 |
| `KBDLStoreLoad` | *(any)* | **no rows** |

`KBDLStoreLoad` has never run, confirmed by absence rather than by a zero count.
Note `jobs` has a `state` column, not `status`.

## Source-side facts read from code

**`NonCodingType` has SIX members** (`schemas/build_genome.py:146`):
`tRNA`, `rRNA`, `tmRNA`, `ncRNA`, `misc_RNA`, `repeat_region`.
The first five are GenBank feature keys verbatim; `repeat_region` was added for Bakta calling and
is the GenBank key for a CRISPR array. The finer SO/GenBank subtypes (`snoRNA`, `SRP_RNA`,
`RNase_P_RNA`, ...) are deliberately **not** members and fold to `ncRNA` with the raw subtype
preserved in `ncrna_class`. `repeat_region` is not a legal *input* type.

**`CanonicalNonCodingFeature`** fields:
`id, source_id, type, location, dna_sequence, product, partial, caller,
amino_acid, anticodon, ncrna_class, rfam`, with `model_config = ConfigDict(extra="allow")`.
It has **no** `protein_translation` field and rejects the key outright, so a serialized
non-coding feature *fails* `GeneSequence` validation — the structural guarantee that a tRNA cannot
reach `build_isolated_model`.

**`NonCodingProvenance`** has **nine** fields:
`source` (`called|source|none`), `callers` (`dict[package, version]`, **including zero-hit runs**),
`cds_dropped_overlapping_rna`, `cds_overlap_drop_fraction` (`None` = built under the original
any-overlap rule), `gene_caller` (`bakta|inprocess|None`), `cds_discarded` (`dict[reason, count]`,
the caller's own vocabulary), `bakta_db` (a string like `"6.0-full"`, deliberately not an
`ObjectRef`), `bakta_image`, `bakta_driver_sha256`.

**`CanonicalGenome`**: `genome_id, prefix, taxonomy, contigs, features, non_coding_features,
non_coding_provenance`.
**`CanonicalContig`**: `id, source_id, length, sequence`.
**`TaxonomyBlock`**: `genus, species, lineage, ncbi_taxid, ani, reference, reason, source_database`.

**Gene id minting** (`genome_normalize.py:2105` and `:2161`):
`id=f"{genome_id}_{counter}"` for CDS and `id=f"{genome_id}_nc{counter}"` for non-coding, on
**independent** counters. So `{g}_1` and `{g}_nc1` coexist as distinct ids and cannot collide.
A dropped feature consumes no id number.

**The GAA dependency** (`job_types/store_load.py`): imports
`genome_annotation_aggregator.config.Config`,
`genome_annotation_aggregator.constrained_store.ConstrainedStoreV2`,
`genome_annotation_aggregator.kbdl_loader.GeneIdMap`, and relies on
`genome_annotation_aggregator.v2_ingest`, `staging_writer` and `store_engine`.
Its module docstring calls itself "the ONE" module that may do so.
`kbdl_service/clearinghouse/__init__.py` states the opposite invariant for everything outside
`job_types`, enforced by `tests/test_layering.py` INVARIANT 1, with a measured rationale:
importing `kbutillib` costs 2,252 modules / 2.41 s.

**The partition-spec ceiling** (KBUtilLib):
`clearinghouse_schema.table_configs()` emits only `name`, `schema_sql` and an optional
`partition_by`; `clearinghouse_bootstrap_adapter._create_table_sql` renders it as
``PARTITIONED BY (`col`)`` over bare stored columns.
`clearinghouse_shard.py` states in terms: "DO NOT try to set an Iceberg sort order,
`write.distribution-mode`, `write.target-file-size-bytes`, or a Parquet Bloom filter from here ...
there is NO properties key, so table properties are not expressible through this write path at
all." Live partition map: `standardizer_version` for `gene_entity`/`gene_content`/`protein_entity`/
`protein_content`; `source` for all five `<type>_result`; nothing for the other six or for
`parameter_set`.

## SETTLED: `fitness.assembly_hash` follows rule A

This was the one item flagged as inherited-unmeasured by decision (2), with the note that
"if even one row is rule B it is a live DATA problem and not only a documentation gap".

**A row count could not have settled it.** The suggested test — intersecting
`fitness.assembly_hash` against KBDL's `genomes.content_hash` — was run and returned **0 of 47**,
and also 0 against `genomes.fasta_object_id`. That result is *uninformative*: the two corpora are
disjoint (48 `fitnessDB` genomes from KBase workspace 262939, versus 4,865 KBDL genomes), so the
intersection is empty under **either** rule. A zero count there is the expected outcome in both
worlds and distinguishes nothing.

What settles it is recomputing the hash from the bytes. Three assemblies were fetched from the
workspace and both rules applied to the real FASTA:

```python
from kbutillib.installed_clients.WorkspaceClient import Workspace
from kbutillib.domains.kbase.kb_ws_utils import KBWSUtils
from kbutillib.domains.identity import standardizers as S
import hashlib
d = ws.get_objects2({"objects": [{"ref": REF}]})["data"][0]["data"]
KBWSUtils().download_blob_file(d["fasta_handle_ref"], out)
raw = open(out, "rb").read()
rule_A = S.entity_hash("genome", S.parse_fasta_contigs(raw))   # sorted-join, order-independent
rule_B = hashlib.sha256(raw).hexdigest()                       # raw bytes, order-dependent
```

| workspace ref | contigs | FASTA bytes | stored `assembly_hash` | rule A | rule B |
|---|---|---|---|---|---|
| `262939/41/3` | 1 | 5,049,960 | `a28c84927703243b871d83fe860c6ae8ebe283817085df1c8107d99a5a79317e` | **MATCH** | `a1371107c3a8e1f157bcc341dd3163768fb30ca09b366f284ae64453aa6d0933` |
| `262939/27/2` | 16 | 5,102,818 | `468dcec392e186daea3cc70c1dc96e2978c051a886296587350ecd7d185cc7e3` | **MATCH** | `91d48f517e20642c426ce4f4966c506d4409647e29f69175d14f7844d02be2f0` |
| `262939/85/3` | 7 | 7,655,939 | `8cbb62e3feaff8b220946ebd9919d97d1c72736e67204e7fb0bb8eff0f379352` | **MATCH** | `63cc2ef6cc42f32856c394540408258cd998c04012b4ca4e58cb18b426865b0b` |

**Rule A in all three; rule B in none.** Two of the three are multi-contig, so rule A's **sort** step
is load-bearing in those cases and the hash still matches — which is the part a single-contig test
could not have shown.

**So it is a documentation gap, not a data problem.** And the consequence is better than neutral:
`fitness.assembly_hash` already *is* `standardizers.entity_hash("genome", contigs)`, the
clearinghouse's canonical genome entity hash, so a companion namespace keyed on
`assembly_entity_hash` joins to `clearinghouse.genome_entity` with no translation layer.

Caveat on the scope of the claim: n=3 of 47, chosen for contig-count spread rather than at random.
It is a strong refutation of "some rows are rule B" (a single rule-B row among the three would have
proved the data problem) but it is not a proof over all 47. The remaining 44 can be checked by the
same recipe at roughly 5 MB of blobstore traffic each.

### Incidental findings from the workspace reads

- The `fitness.genome.genome_id` UUIDs are **KBase workspace Assembly object names**: object
  `262939/41/3` is named `01c3ae47-f41f-4199-a48e-cb4aaff4bbf7_assembly`, and
  `01c3ae47-f41f-4199-a48e-cb4aaff4bbf7` is a `genome_id` in `fitness.gene`. So the scheme's genome
  ids are not opaque — they are workspace-derived, which is worth knowing before treating them as
  content-addressed.
- The Assembly object's own `md5` (`247acf6fa6b7bae8f0edc37d4cbc9e34`, 32 hex) is **not** the
  `assembly_hash`, so a reader must not mistake the KBase assembly md5 for the lake's hash.
- Objects are owned by `chenry` in workspace 262939 (`chenry:narrative_1781730421849`), type
  `KBaseGenomeAnnotations.Assembly-5.1`. The Assembly object carries contig `md5`/`length`/`name`
  but **not** sequences; sequence lives in the blobstore behind `fasta_handle_ref`.
