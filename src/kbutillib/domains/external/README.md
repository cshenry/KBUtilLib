# kbutillib.domains.external — External Data Services

Clients for public biological databases: BVBRC/PATRIC, RCSB PDB, UniProt via KBase, and the TransyT transport system database.

## What lives here

Four utility classes provide programmatic access to external databases without requiring direct API credentials beyond what each service offers publicly. All clients follow the same lazy-import pattern: they construct cleanly and report availability before making any network calls.

## Modules

| File | Class(es) | Purpose |
|------|-----------|---------|
| `bvbrc_utils.py` | `BvbrcUtils`, `BvbrcUtilsImpl` | BVBRC (formerly PATRIC) genome/feature queries via REST |
| `patric_ws_utils.py` | `PatricWsUtils`, `PatricWsUtilsImpl` | PATRIC workspace API for genome and feature data access |
| `rcsb_pdb_utils.py` | `RcsbPdbUtils`, `RcsbPdbUtilsImpl` | RCSB PDB structure search, metadata retrieval, mmCIF download |
| `kb_uniprot_utils.py` | `KbUniprotUtils`, `KbUniprotUtilsImpl` | UniProt lookup via KBase integration layer |

## Facade access

```python
from kbutillib.toolkit import KBUtilLib

kbu = KBUtilLib()

# BVBRC genome search
genomes = kbu.bvbrc.search_genomes(taxon_id=573)

# RCSB PDB structure
pdb_meta = kbu.rcsb_pdb.get_entry("1ABC")
kbu.rcsb_pdb.download_mmcif("1ABC", outdir="/tmp")

# UniProt
uniprot_record = kbu.uniprot.get_protein("P00533")
```

## Optional dependencies

| Package | Enables | Note |
|---------|---------|------|
| `requests` | All HTTP clients | included in core |
| `biopython` | PDB mmCIF parsing in `rcsb_pdb_utils` | `pip install biopython` |

No optional packages are needed for availability checks or metadata queries. Structure downloads and parsing require `biopython`.

## Adding a capability here

See root `CONTRIBUTING.md` → "Add a domain capability".

1. Add the method to the relevant `*Impl` class (e.g., `BvbrcUtilsImpl`).
2. Decorate with `@capability(domain="external", summary="...", tags=("external", "readonly"))`.
3. Run `kbu cap list --domain external` to confirm.
4. Network-dependent tests should use `pytest.mark.network` or `monkeypatch` fixtures.

Use `kbu new-capability external.<name>` to scaffold the stub.
