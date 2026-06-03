---
name: KBase Genome Expert
description: Expert on saving, loading, validating, and manipulating KBase Genome objects from notebook contexts
scope: domain
---

# KBase Genome Expert

## 1. What This Skill Covers

This skill covers working with **KBase Genome objects** (`KBaseGenomes.Genome`) from notebook contexts — typically running on primary-laptop or h100 without an active KBase SDK callback server.

Topics covered:
- Loading a Genome from the KBase Workspace (`kbu.ws.get_object`)
- Building a Genome dict from a local FASTA + GFF3 file (`kbu.genome.build_genome_from_fasta_gff`)
- Validating a Genome dict before saving (`kbu.genome.validate_genome`)
- Saving a Genome typed object directly to the Workspace (`kbu.genome.save_genome_object`)
- Saving an Assembly via EE2 and then the Genome in one call (`kbu.genome.save_genome_with_assembly`)
- Common manipulations using existing helpers on `kbu.genome`
- What works without a callback URL and what requires one

Entry point is always `kbu.genome` (a `KBGenomeUtilsImpl` instance accessed via the `KBUtilLib` facade).

```python
from kbutillib import KBUtilLib
kbu = KBUtilLib()
```

---

## 2. Quick Reference: The Save Flow

The canonical end-to-end notebook save flow — provide a FASTA and a populated Genome dict, get back both refs:

```python
from kbutillib import KBUtilLib
from pathlib import Path

kbu = KBUtilLib()

# 1. Build the Genome dict from a FASTA (and optional GFF3)
genome_dict = kbu.genome.build_genome_from_fasta_gff(
    fasta_path=Path("my_genome.fna"),
    gff_path=Path("my_genome.gff"),   # omit if no annotations
    scientific_name="Escherichia coli K-12",
    taxonomy="Bacteria; Proteobacteria; Gammaproteobacteria; Enterobacterales; Enterobacteriaceae; Escherichia",
    genetic_code=11,
    source="User",
)

# 2. Validate before saving (optional but recommended)
errors = kbu.genome.validate_genome(genome_dict, require_assembly_ref=False)
if errors:
    print("Genome validation errors:", errors)
    raise ValueError("Fix errors before saving")

# 3. Save Assembly via EE2 + Genome via direct WS in one call
assembly_ref, genome_ref = kbu.genome.save_genome_with_assembly(
    fasta_path=Path("my_genome.fna"),
    genome_dict=genome_dict,
    workspace="my_workspace",
    base_name="my_genome",
    assembly_suffix="_assembly",   # default
)
print(f"Assembly: {assembly_ref}")
print(f"Genome:   {genome_ref}")
```

Both refs are `"ws_id/obj_id/version"` strings.

---

## 3. Loading Genomes

### From KBase Workspace

Load any object (including `KBaseGenomes.Genome`) via the `ws` sub-utility:

```python
genome_data = kbu.ws.get_object("12345/6/7")      # by ref
genome_data = kbu.ws.get_object("my_genome", ws="my_workspace")  # by name
genome_dict = genome_data["data"]                   # the Genome dict is under "data"
```

### From Local Files (FASTA + optional GFF3)

`build_genome_from_fasta_gff` constructs a full Genome dict from local files:

```python
genome_dict = kbu.genome.build_genome_from_fasta_gff(
    fasta_path="genome.fna",
    gff_path="genome.gff",        # optional; omit for annotation-free genome
    scientific_name="Org name",
    taxonomy="Bacteria; ...",
    genetic_code=11,              # default
    source="User",                # default
    source_id="my_genome",        # default: stem of fasta_path
)
```

The returned dict has: `id`, `scientific_name`, `domain`, `taxonomy`, `genetic_code`, `dna_size`, `num_contigs`, `contig_ids`, `contig_lengths`, `gc_content`, `md5`, `molecule_type`, `source`, `source_id`, `assembly_ref` (empty string — not yet saved), `features`, `cdss`, `mrnas`, `non_coding_features`, `feature_counts`.

**GFF3 type mapping:**
- `gene` → `features` list with `type="gene"`, `cdss=[]` list
- `CDS` → `cdss` list; `parent_gene` set from GFF `Parent` attribute; includes `protein_translation` and `protein_md5`
- `mRNA` → `mrnas` list with `parent_gene`
- `tRNA`, `rRNA`, `ncRNA` → `non_coding_features` list

Feature locations use **1-based inclusive** starts (GFF3 native convention).

### From BV-BRC Local Files

For BV-BRC multi-file layouts (genome_metadata/, genomes/, features/), use:

```python
genome_dict = kbu.genome.load_genome_from_local_files(
    genome_id="1234567.89",
    features_dir="features",
    genomes_dir="genomes",
    metadata_dir="genome_metadata",
)
```

---

## 4. Validating

`validate_genome` does schema-only validation — checks required fields, types, and cross-field consistency. It does NOT recompute MD5 or retranslate sequences.

```python
# Pre-save: assembly_ref must be present and non-empty (default)
errors = kbu.genome.validate_genome(genome_dict)
# Returns [] if valid, or a list of human-readable error strings.

# Pre-assembly: assembly_ref doesn't exist yet
errors = kbu.genome.validate_genome(genome_dict, require_assembly_ref=False)
```

**Fields checked:** `id`, `scientific_name`, `domain`, `genetic_code` (int), `dna_size` (int>0), `num_contigs` (int>0), `contig_ids` (list[str]), `contig_lengths` (list[int], same length as `contig_ids`), `gc_content` (float in [0,1]), `md5` (non-empty str), `molecule_type`, `source`, `source_id`, `taxonomy`, `assembly_ref` (conditionally), `features` / `cdss` / `mrnas` / `non_coding_features` (lists), `feature_counts` (dict).

**Per-feature checks:** each feature in all four lists must have `id` (str, unique across all lists), `type` (str), `location` (list of `[contig_id, start_int, strand_str, length_int]` tuples where `contig_id` must appear in `contig_ids`).

**What it does NOT check:** codon-table correctness, MD5 recomputation, protein/DNA agreement, content overlap between features.

---

## 5. Common Manipulations

These helpers exist on `kbu.genome` (via the legacy `KBGenomeUtils` delegate):

```python
# Cache load — pull a genome (or other feature container) from WS or file into memory
obj = kbu.genome.load_kbase_gene_container(ref_or_filename, ws=workspace_id, localname="my_key")

# Extract all features (genes + CDSs + mRNAs + non-coding) from cached object
all_features = kbu.genome.object_to_features("my_key")

# Feature lookup by id
feature = kbu.genome.get_ftr("my_key", "gene_id_001")

# Alias lookup
aliases = kbu.genome.ftr_to_aliases("my_key", "gene_id_001")
ftrs_for_alias = kbu.genome.alias_to_ftrs("my_key", "WP_001234567.1")

# Protein extraction
proteins = kbu.genome.object_to_proteins(ref)  # returns list of [id, protein_seq]

# Pure-string helpers (no WS access needed)
rc = kbu.genome.reverse_complement("ATCG")
aa = kbu.genome.translate_sequence("ATGATGATG", genetic_code=11)
gc = kbu.genome.calculate_gc_content("ATGCATGC")

# Taxonomy aggregation and synthetic genome
consensus_tax, tax_dict = kbu.genome.aggregate_taxonomies(genomes_list, asv_id="ASV1")
synthetic = kbu.genome.create_synthetic_genome("ASV1", genomes_list, taxonomy="Bacteria; ...")

# Annotation update — NOTE: requires SDK callback context (see Section 6)
result = kbu.genome.add_annotations_to_object(ref, suffix="_annotated", annotations=anno_dict)
```

---

## 6. Notebook-vs-SDK Callback Note

Notebook contexts (primary-laptop, h100) typically run **without** an SDK callback server. The table below summarizes what works where:

| Operation | Transport | Works without callback URL? |
|---|---|---|
| `kbu.genome.save_genome_object(...)` | Direct Workspace `save_objects` | Yes |
| `kbu.genome.save_assembly_from_fasta(...)` | EE2 job → AssemblyUtil | Yes (EE2 submits on your behalf) |
| `kbu.genome.save_genome_with_assembly(...)` | EE2 + Workspace | Yes |
| `kbu.ws.get_object(...)` / `kbu.ws.save_ws_object(...)` | Direct Workspace | Yes |
| `kbu.callback.gfu_client()` | SDK callback server | **No** — raises `ImportError` or connection error without SDK install |
| `kbu.callback.afu_client()` | SDK callback server | **No** — same constraint |
| `kbu.genome.add_annotations_to_object(...)` | Annotation API (callback-required) | **No** |

**Injection escape hatch:** if you have a pre-built `GenomeFileUtilClient` or `AssemblyUtilClient` from a notebook that does have a callback URL, inject it:

```python
kbu.callback.set_callback_client("GenomeFileUtil", my_gfu_client)
kbu.callback.set_callback_client("AssemblyUtil", my_afu_client)
```

**`installed_clients/` shipping constraint:** `KBUtilLib/installed_clients/` ships only `Workspace`, `EE2`, `AbstractHandle`, `baseclient`, and `authclient`. `AssemblyUtilClient` and `GenomeFileUtilClient` are imported lazily inside `kb_callback_utils.py` but are expected from a separate KBase SDK install. Without that install, `kbu.callback.gfu_client()` and `kbu.callback.afu_client()` raise `ImportError`.

---

## 7. Related Skills

- `/kbutillib-expert` — full KBUtilLib reference (composition architecture, all sub-utilities, config, job management)
- `/kb-sdk-dev` — developing KBase SDK apps and working inside SDK app containers (where callback URLs are available)
- `/modelseedpy-expert` — ModelSEED-Python for FBA and metabolic model analysis using the same `kbu` facade
