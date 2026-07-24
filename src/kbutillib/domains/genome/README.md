# kbutillib.domains.genome — Genome & Annotation Utilities

Genome data management, sequence alignment, ontology mapping, protein language models, and a pluggable annotation pipeline for microbial genomes.

## What lives here

The genome domain covers the full annotation and analysis stack for microbial genomes within KBase: pulling genome objects from the workspace, running high-throughput alignment (MMseqs2, Skani), mapping ontologies, and driving automated annotation via Prokka, DRAM2, or TransyT. The `annotation/` subpackage provides a common `AnnotatorUtils` base and one concrete implementation per tool.

## Modules

| File | Class(es) | Purpose |
|------|-----------|---------|
| `kb_genome_utils.py` | `KBGenomeUtils`, `KBGenomeUtilsImpl` | Load, parse, and manage KBase genome objects |
| `kb_annotation_utils.py` | `KBAnnotationUtils`, `KBAnnotationUtilsImpl` | Coordinate genome annotation runs within KBase |
| `mmseqs_utils.py` | `MMSeqsUtils`, `MMSeqsUtilsImpl` | High-throughput protein sequence search via MMseqs2 |
| `skani_utils.py` | `SkaniUtils`, `SkaniUtilsImpl` | Fast genome-level ANI estimation via Skani |
| `ontomap_utils.py` | `OntomapUtils`, `OntomapUtilsImpl` | Ontology term mapping and functional annotation |
| `annotation/annotator_utils.py` | `AnnotatorUtils` | Abstract base for annotation backends |
| `annotation/prokka.py` | `ProkkaUtils` | Prokka prokaryotic annotation |
| `annotation/dram2.py` | `DRAM2Utils` | DRAM2 metabolic and functional annotation |
| `annotation/transyt.py` | `TransytUtils` | TransyT transport system annotation |

## Facade access

```python
from kbutillib.toolkit import KBUtilLib

kbu = KBUtilLib()

# Load a KBase genome object
genome = kbu.genome.get_genome(workspace="my_ws", genome_id="my.genome")

# Run MMseqs2 search
hits = kbu.mmseqs.search(query_fasta="/tmp/query.faa", db="/tmp/target_db")

# Compute ANI between two genome FASTA files
ani = kbu.skani.compare("/tmp/genome_a.fna", "/tmp/genome_b.fna")
```

## Optional dependencies

| Package / Tool | Enables | Note |
|----------------|---------|------|
| KBase SDK / workspace tokens | `KBGenomeUtils` workspace calls | env token required |
| `mmseqs2` binary | MMseqs2 search | `conda install -c bioconda mmseqs2` |
| `skani` binary | Skani ANI | `conda install -c bioconda skani` |
| `prokka` binary | Prokka annotation | `conda install -c bioconda prokka` |
| `torch` + `transformers` | Protein language models (PLM) | `pip install torch transformers` |

All classes construct and check `available` before attempting I/O. Missing binaries are reported via `unavailable_reason`, not import errors.

## Adding a capability here

See root `CONTRIBUTING.md` → "Add a domain capability".

1. Add the method to the relevant `*Impl` class.
2. Decorate with `@capability(domain="genome", summary="...", tags=("genome",))`.
3. If the method requires a binary (e.g., mmseqs2), check `self.available` inside the method body and raise `BackendUnavailableError` if absent.
4. Run `kbu cap list --domain genome` to confirm.

For new annotation backends: subclass `AnnotatorUtils`, implement `annotate()`, and register the class in `annotation/__init__.py`.
