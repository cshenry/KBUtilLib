# kbutillib.domains.external — External Data Services

Clients for public biological databases: BVBRC/PATRIC, RCSB PDB, UniProt via KBase, and PATRIC
workspace access.

---

## What lives here

Four utility classes provide programmatic access to external biological databases without
requiring direct API credentials beyond what each service offers publicly. All clients follow the
lazy-import pattern: they construct cleanly and report availability before making any network
calls.

## Canonical imports

```python
from kbutillib.domains.external.bvbrc_utils import BvbrcUtils
from kbutillib.domains.external.rcsb_pdb_utils import RcsbPdbUtils
from kbutillib.domains.external.kb_uniprot_utils import KbUniprotUtils
from kbutillib.domains.external.patric_ws_utils import PatricWsUtils
```

---

## Modules

| File | Class(es) | Purpose |
|------|-----------|---------|
| `bvbrc_utils.py` | `BvbrcUtils`, `BvbrcUtilsImpl` | BVBRC (formerly PATRIC) genome/feature queries via REST |
| `patric_ws_utils.py` | `PatricWsUtils`, `PatricWsUtilsImpl` | PATRIC workspace API for genome and feature data access |
| `rcsb_pdb_utils.py` | `RcsbPdbUtils`, `RcsbPdbUtilsImpl` | RCSB PDB structure search, metadata retrieval, mmCIF download |
| `kb_uniprot_utils.py` | `KbUniprotUtils`, `KbUniprotUtilsImpl` | UniProt lookup via KBase integration layer |

---

## Optional dependencies

| Package | Enables | Install |
|---------|---------|---------|
| `requests` | All HTTP clients | included in core dependencies |
| `biopython` | PDB mmCIF parsing in `RcsbPdbUtils` | `pip install biopython` |

No optional packages are needed for availability checks or metadata queries. Structure downloads
and parsing require `biopython`.

---

## Usage example

```python
from kbutillib.domains.external.bvbrc_utils import BvbrcUtils
from kbutillib.domains.external.rcsb_pdb_utils import RcsbPdbUtils
from kbutillib.domains.external.kb_uniprot_utils import KbUniprotUtils

# BVBRC genome search
bvbrc = BvbrcUtils()
genomes = bvbrc.search_genomes(taxon_id=573)

# RCSB PDB
pdb = RcsbPdbUtils()
meta = pdb.get_entry("1ABC")
pdb.download_mmcif("1ABC", outdir="/tmp")  # requires biopython

# UniProt via KBase
uniprot = KbUniprotUtils()
record = uniprot.get_protein("P00533")
```

Via the facade:

```python
from kbutillib import KBUtilLib

kbu = KBUtilLib()
genomes = kbu.bvbrc.search_genomes(taxon_id=573)
pdb_meta = kbu.rcsb_pdb.get_entry("1ABC")
```

---

## Available capabilities

Capabilities are registered on each `*Impl` class where they are present. Run:

```bash
kbu cap list --domain external
```

---

## Adding a capability here

See root `CONTRIBUTING.md` → "Add a domain capability".

1. Add the method to the relevant `*Impl` class (e.g., `BvbrcUtilsImpl`).
2. Decorate with `@capability(domain="external", summary="...", tags=("external", "readonly"))`.
3. Run `kbu cap list --domain external` to confirm.
4. Network-dependent tests should use `pytest.mark.network` or `monkeypatch` fixtures.

Use `kbu new-capability external.<name>` to scaffold the stub.
