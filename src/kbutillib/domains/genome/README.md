# kbutillib.domains.genome — Genome & Annotation Utilities

Genome data management, sequence alignment, ontology mapping, protein language models, and a
pluggable annotation pipeline for microbial genomes.

---

## What lives here

The genome domain covers the full annotation and analysis stack for microbial genomes within
KBase: loading genome objects from the workspace, running high-throughput alignment (MMseqs2,
Skani), mapping ontologies, and driving automated annotation via Prokka, DRAM2, or TransyT.
The `annotation/` subpackage provides a common `AnnotatorUtils` base and one concrete
implementation per tool.

## Canonical imports

```python
from kbutillib.domains.genome.kb_genome_utils import KBGenomeUtils
from kbutillib.domains.genome.mmseqs_utils import MMSeqsUtils
from kbutillib.domains.genome.skani_utils import SkaniUtils
from kbutillib.domains.genome.ontomap_utils import OntomapUtils
```

---

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

---

## Optional dependencies

| Package / Tool | Enables | Install |
|----------------|---------|---------|
| KBase auth token | `KBGenomeUtils` workspace calls | `export KB_AUTH_TOKEN=...` |
| `mmseqs2` binary | MMseqs2 sequence search | `conda install -c bioconda mmseqs2` |
| `skani` binary | Skani ANI estimation | `conda install -c bioconda skani` |
| `prokka` binary | Prokka annotation | `conda install -c bioconda prokka` |
| `torch` + `transformers` | Protein language models (`KBPLMUtils`) | `pip install torch transformers` |

All classes check `available` before attempting I/O. Missing binaries are reported via
`unavailable_reason`, not import errors.

---

## Usage example

```python
from kbutillib.domains.genome.kb_genome_utils import KBGenomeUtils
from kbutillib.domains.genome.mmseqs_utils import MMSeqsUtils
from kbutillib.domains.genome.skani_utils import SkaniUtils

# Load a KBase genome (requires KB_AUTH_TOKEN)
genome = KBGenomeUtils()
obj = genome.get_genome(workspace="my_ws", genome_id="my.genome")

# MMseqs2 sequence search (requires mmseqs2 binary)
mmseqs = MMSeqsUtils()
print(mmseqs.available)
hits = mmseqs.search(query_fasta="/tmp/query.faa", db="/tmp/target_db")

# Genome-level ANI (requires skani binary)
skani = SkaniUtils()
ani = skani.compare("/tmp/genome_a.fna", "/tmp/genome_b.fna")
```

Via the facade:

```python
from kbutillib import KBUtilLib

kbu = KBUtilLib()
genome = kbu.genome.get_genome(workspace="my_ws", genome_id="my.genome")
ani = kbu.skani.compare("/tmp/a.fna", "/tmp/b.fna")
```

---

## Available capabilities

Capabilities are registered on each `*Impl` class where they are present. Run:

```bash
kbu cap list --domain genome
```

---

## Adding a capability here

See root `CONTRIBUTING.md` → "Add a domain capability".

1. Add the method to the relevant `*Impl` class.
2. Decorate with `@capability(domain="genome", summary="...", tags=("genome",))`.
3. If the method requires an external binary, check `self.available` inside the method body
   and raise `BackendUnavailableError` if absent.
4. Run `kbu cap list --domain genome` to confirm.

For new annotation backends: subclass `AnnotatorUtils`, implement `annotate()`, and register
in `annotation/__init__.py`.
