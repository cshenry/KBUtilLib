# KBUtilLib

KBUtilLib is a domain-organized utility library for KBase bioinformatics and metabolic
modeling: compound and reaction lookup against ModelSEED, flux balance analysis, genome
annotation, thermodynamic ΔG prediction, metabolic network expansion, external database
access, and AI/LLM workflows — all exposed through a single capability registry.

Every function decorated with `@capability` is registered once and becomes available
simultaneously through three transports: the `kbu` CLI, the `kbu-mcp` MCP stdio server
(for AI agents and Claude Desktop), and the `kbu-api` FastAPI HTTP server. Adding a new
capability requires no transport wiring — the registry handles dispatch automatically.

**Not on PyPI.** Install from source: `git clone … && pip install -e ".[all]"`.
See [Getting Started](../GETTING_STARTED.md) for the step-by-step setup guide.

---

## Documentation

| Section | What it covers |
|---------|----------------|
| [Usage Guide](usage.md) | All three transports: Python API, CLI, HTTP API, MCP — with copy-pasteable examples |
| [Capability Catalog](capabilities/index.md) | Auto-generated table of every registered capability, grouped by domain |
| [API Reference](reference.md) | Module-level API reference (mkdocstrings) |
| [Contributing](contributing.md) | How to add a capability, a domain, and write tests |
| [Code of Conduct](code_of_conduct.md) | Community standards |

---

## Architecture in one paragraph

All domain implementations live under `src/kbutillib/domains/`. Each domain contains one or
more `*Impl` classes whose methods are decorated with `@capability`. At startup, each transport
— the Click CLI, the FastAPI app, and the MCP server — calls `register_all()` to iterate the
`KBUtilLib` facade, collect every decorated method, build `CapabilitySpec` objects, and store
them in the process-wide `CapabilityRegistry`. From that point on, `kbu cap list`, HTTP `GET
/v1/capabilities`, and the MCP tool list all read from the same registry. Optional heavy
dependencies (cobra, rdkit, equilibrator) are never imported at module level; each class
implements an `available` property that the registry checks on demand via `spec.availability()`.

---

## Domains at a glance

| Domain | Key classes | What it does |
|--------|-------------|-------------|
| **biochem** | `MSBiochemUtils` | Compound/reaction search in ModelSEED biochemistry database |
| **thermo** | `ThermoUtils`, `PredictiveThermoUtils` | ΔG lookup and multi-backend prediction (equilibrator, modelseed, molgpk, dgpredictor) |
| **cheminformatics** | `NetworkExpansionUtils`, `VerabUtils` | Metabolic network expansion (Pickaxe/RetroRules) and SMARTS-based rule screening |
| **modeling** | `MSFBAUtils`, `MSReconstructionUtils` | Flux balance analysis and draft model reconstruction |
| **genome** | `KBGenomeUtils`, `MMSeqsUtils`, `SkaniUtils` | KBase genome I/O, sequence alignment, ontology mapping, annotation pipeline |
| **kbase** | `KBWSUtils`, `KBSDKUtils` | KBase workspace API, SDK client, narrative audit, endpoint resolver |
| **ai** | `ArgoUtils`, `AiCurationUtils`, `KBPLMUtils` | Argo LLM gateway, AI curation workflows, protein language models |
| **external** | `BvbrcUtils`, `RcsbPdbUtils`, `KbUniprotUtils` | BVBRC/PATRIC, RCSB PDB, UniProt via KBase |
| **notebook** | `NotebookSession`, `VectorStore`, `EscherUtils` | Provenanced notebook cache, typed vector storage, Escher metabolic maps |

---

## Quick install

```bash
git clone https://github.com/cshenry/KBUtilLib.git
cd KBUtilLib
pip install -e ".[all]"
kbu cap list
```
