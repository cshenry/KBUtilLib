# KBUtilLib

[![PyPI](https://img.shields.io/pypi/v/KBUtilLib.svg)][pypi status]
[![Status](https://img.shields.io/pypi/status/KBUtilLib.svg)][pypi status]
[![Python Version](https://img.shields.io/pypi/pyversions/KBUtilLib)][pypi status]
[![License](https://img.shields.io/pypi/l/KBUtilLib)][license]

[![Tests](https://github.com/cshenry/KBUtilLib/workflows/Tests/badge.svg)][tests]
[![Codecov](https://codecov.io/gh/cshenry/KBUtilLib/branch/main/graph/badge.svg)][codecov]

[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)][pre-commit]
[![Ruff codestyle][ruff badge]][ruff project]

> **A modular utility framework for scientific computing and bioinformatics**
>
> KBUtilLib provides domain-organized utilities for KBase data access, genomics, biochemistry,
> metabolic modeling, thermodynamics, cheminformatics, and AI workflows — all exposed through
> a single capability registry and your choice of transport: CLI, MCP, or HTTP API.

**New here?** See **[GETTING_STARTED.md](GETTING_STARTED.md)** for the first-time-user
onboarding guide — it walks you from a clean machine to a running `kbu` environment.

---

## Overview

KBUtilLib is built around a **one-registry, many-transports** architecture. Every capability
is registered once via the `@capability` decorator in `core/registry.py`. The three transports
— `kbu` (CLI), `kbu-mcp` (Model Context Protocol stdio server), and `kbu-api` (FastAPI HTTP) —
all read from that same registry, so adding a new capability makes it available everywhere
without any additional wiring.

The `KBUtilLib` facade in `toolkit.py` composes all domain implementations as lazy properties.
Use it for interactive or scripted work when you want a single object with access to everything.
For production imports, prefer the canonical domain paths described below.

---

## Installation

```console
# Core library (no transport dependencies)
pip install -e .

# With MCP server support
pip install -e ".[mcp]"

# With FastAPI HTTP server
pip install -e ".[api]"

# With auto-generated mkdocs capability catalog
pip install -e ".[apidocs]"

# Everything
pip install -e ".[all]"
```

**Python 3.11+** is required. A conda environment file is provided:

```console
conda env create -f environment.yml   # creates kbutillib-reorg env with [all]
conda activate kbutillib-reorg
```

---

## Quick Start

### Unified facade

```python
from kbutillib import KBUtilLib

kbu = KBUtilLib()

# Biochemistry
hits = kbu.biochem.search_compounds("atp")

# Thermodynamics (ModelSEED legacy)
dg = kbu.thermo.get_compound_deltag("cpd00002")

# Predictive thermodynamics (equilibrator → modelseed dispatch)
result = kbu.predictive_thermo.predict_reaction_deltag(reaction)

# Cheminformatics — network expansion
expanded = kbu.network_expansion.run_expansion(seed_compounds)

# Cheminformatics — Verab rule-based screening
rules = kbu.verab.discover_rules(generations=1)

# KBase workspace
obj = kbu.ws.get_object(workspace_id, obj_ref)
```

### Direct domain imports (canonical paths)

```python
from kbutillib.domains.biochem.ms_biochem_utils import MSBiochemUtils
from kbutillib.domains.thermo.predictive_thermo_utils import PredictiveThermoUtils
from kbutillib.domains.cheminformatics.verab.facade import VerabUtils
from kbutillib.domains.genome.kb_genome_utils import KBGenomeUtils
from kbutillib.domains.modeling.ms_fba_utils import MSFBAUtils
from kbutillib.core.registry import CapabilityRegistry
```

### Top-level convenience imports

All public classes are re-exported from the package root for backward compatibility:

```python
from kbutillib import (
    KBGenomeUtils, MSBiochemUtils, KBModelUtils,
    KBWSUtils, ThermoUtils, PredictiveThermoUtils,
)
```

---

## Package Structure

```
src/kbutillib/
├── __init__.py              # public API surface — re-exports all facade + Impl classes
├── __main__.py              # `python -m kbutillib` → interfaces.cli
├── toolkit.py               # KBUtilLib lazy facade (composes all *Impl domains)
├── layout.py                # shared compartment-layout helpers
├── compartments.py          # biochemistry compartment definitions
│
├── core/                    # infrastructure shared by every domain
│   ├── registry.py          # CapabilityRegistry + CapabilitySpec
│   ├── capability.py        # @capability decorator (zero overhead to normal calls)
│   ├── config.py            # pydantic v2 Config model
│   ├── errors.py            # BackendUnavailableError, CapabilityError
│   ├── base_utils.py        # BaseUtils — logging, config, dependency management
│   ├── shared_env_utils.py  # SharedEnvUtils — config files + auth tokens
│   └── dependency_manager.py
│
├── domains/                 # all domain implementations (canonical homes)
│   ├── biochem/             # MSBiochemUtils — compound/reaction lookup (ModelSEED)
│   ├── thermo/              # ThermoUtils (ModelSEED legacy), PredictiveThermoUtils (facade)
│   │   └── thermo_predictors/  # equilibrator, modelseed, modelseed_db, dgpredictor, molgpk
│   ├── cheminformatics/     # NetworkExpansionUtils; base + pickaxe/retrorules backends
│   │   └── verab/           # VerabUtils — rule discovery, screening, KING artifacts, SMARTS
│   ├── modeling/            # MSFBAUtils, MSReconstructionUtils, KBModelUtils, MSTemplateUtils,
│   │                        #   ModelStandardizationUtils, model_directionality, model_helpers
│   ├── genome/              # KBGenomeUtils, KBAnnotationUtils, MMSeqsUtils, SkaniUtils,
│   │   │                    #   OntomapUtils, KBPLMUtils
│   │   └── annotation/      # AnnotatorUtils, ProkkaUtils, DRAM2Utils, TransytUtils
│   ├── external/            # BvbrcUtils, RcsbPdbUtils, KBUniProtUtils, PatricWSUtils,
│   │                        #   KBReadsUtils
│   ├── kbase/               # KBWSUtils, KBSDKUtils, KBCallbackUtils, KBReadsUtils,
│   │                        #   KBBERDLUtils, KBNarrativeAudit, kbase_catalog_client,
│   │                        #   kbase_endpoints
│   ├── ai/                  # AiCurationUtils, ArgoUtils, KBPLMUtils
│   └── notebook/            # NotebookUtils, VectorStore, EscherUtils
│
├── interfaces/              # transport adapters — all lazy-imported, no core dependency
│   ├── mcp/                 # stdio MCP server  →  `kbu-mcp` script
│   │   └── server.py
│   ├── api/                 # FastAPI HTTP app   →  `kbu-api` script
│   │   └── app.py
│   ├── cli/                 # Click root: `kbu`, `kbu cap list/info/run`, `kbu new-capability`
│   └── docs/                # mkdocs + capability catalog generators
│
├── agents/                  # KING self-install bundles, researchos config
│   ├── king_app/            # KING agent bundles (JSON + skill markdown)
│   │   └── verab/           # Verab-specific KING bundle
│   └── researchos/          # researchos configuration generation
│
└── deploy/                  # deployment helpers
    ├── poplar/              # systemd + nginx service definitions
    └── docker/              # docker-compose and Dockerfile
```

---

## Domain Modules

| Domain | Module path | What it contains |
|--------|-------------|-----------------|
| **biochem** | `domains/biochem/` | ModelSEED compound & reaction lookup, biochemistry search |
| **thermo** | `domains/thermo/` | ModelSEED thermodynamics (`ThermoUtils`); multi-backend predictive ΔG (`PredictiveThermoUtils`) with equilibrator, modelseed, modelseed_db, dgpredictor, and molgpk backends |
| **cheminformatics** | `domains/cheminformatics/` | Network expansion (`NetworkExpansionUtils`) with pickaxe and RetroRules backends; Verab rule-based reaction screening |
| **modeling** | `domains/modeling/` | Flux balance analysis (`MSFBAUtils`), model reconstruction (`MSReconstructionUtils`), template management (`MSTemplateUtils`), model standardization, model helpers |
| **genome** | `domains/genome/` | KBase genome utilities, sequence alignment (`MMSeqsUtils`, `SkaniUtils`), ontology mapping (`OntomapUtils`), protein language models (`KBPLMUtils`); annotation subpackage (Prokka, DRAM2, TransyT, generic annotator) |
| **external** | `domains/external/` | BVBRC/PATRIC access, RCSB PDB queries, KBase UniProt integration, reads utilities |
| **kbase** | `domains/kbase/` | Workspace API (`KBWSUtils`), SDK utilities, callback service, narrative audit, catalog client, endpoint resolver |
| **ai** | `domains/ai/` | Argo LLM inference (`ArgoUtils`), AI curation workflows (`AiCurationUtils`) |
| **notebook** | `domains/notebook/` | Jupyter display utilities (`NotebookUtils`), vector store, Escher metabolic map integration |

---

## Transports

### CLI — `kbu`

```console
kbu --help
kbu doctor                  # check backend availability (graceful on missing optional deps)
kbu cap list                # list all registered capabilities
kbu cap info <name>         # show signature, docstring, and required backends
kbu cap run <name> [args]   # invoke a capability from the shell

kbu new-capability          # scaffold a new @capability-decorated function
```

Install: available automatically after `pip install -e .` (no extras required).

### MCP server — `kbu-mcp`

Exposes every registered capability as an MCP tool over stdio, ready for Claude Desktop or
any MCP-compatible client.

```console
pip install -e ".[mcp]"
kbu-mcp                     # start stdio MCP server
```

Configure in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "kbutillib": {
      "command": "kbu-mcp"
    }
  }
}
```

### HTTP API — `kbu-api`

FastAPI application exposing the capability registry over HTTP with bearer-token auth.

```console
pip install -e ".[api]"
kbu-api                     # start uvicorn on default port
```

Key endpoints:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness check |
| `GET` | `/version` | Package version |
| `GET` | `/v1/capabilities` | List all capabilities |
| `POST` | `/v1/tools/{name}` | Invoke a capability |

### API docs — `mkdocs`

```console
pip install -e ".[apidocs]"
mkdocs serve                # live capability catalog + API reference
mkdocs build                # static site
```

---

## Capability Registry

Every public function decorated with `@capability` is registered automatically at import time.
The decorator is transparent to normal Python calls — it adds no runtime overhead and does not
alter the function's signature or behavior.

```python
from kbutillib.core.capability import capability

@capability(
    name="biochem.search_compounds",
    description="Search ModelSEED biochemistry for compounds matching a query string.",
    tags=["biochem", "search"],
    backends=["modelseed"],
)
def search_compounds(self, query: str, limit: int = 20) -> list[dict]:
    ...
```

Discover registered capabilities:

```console
kbu cap list                # tabular listing with tags and backend requirements
kbu cap info biochem.search_compounds
```

Or from Python:

```python
from kbutillib.core.registry import CapabilityRegistry

reg = CapabilityRegistry.get()
caps = reg.list_capabilities()
```

---

## Migration Note

The **flat module layout** (`kbutillib.ms_biochem_utils`, `kbutillib.verab_utils`, etc.) has
been removed as the primary import surface. Canonical paths are now under `kbutillib.domains`.

**If you imported from flat modules before:**

```python
# Old (no longer works as a direct file)
from kbutillib.ms_biochem_utils import MSBiochemUtils

# Preferred — top-level re-export (stable, unchanged)
from kbutillib import MSBiochemUtils

# Or — explicit canonical path
from kbutillib.domains.biochem.ms_biochem_utils import MSBiochemUtils
```

The top-level `from kbutillib import <ClassName>` path remains stable across releases.
Direct submodule imports (`kbutillib.<flat_name>`) should be updated to the `domains` path.

---

## Development

### Scaffold a new capability

```console
kbu new-capability
# Interactive prompt: domain, name, description, tags, backends
# Writes a stub under domains/<domain>/ and registers it automatically
```

### Conda environment setup

```console
conda env create -f environment.yml
conda activate kbutillib-reorg
pip install -e ".[all]"
```

### Run tests

```console
python -m pytest tests/ -q
```

Certain tests require optional heavy dependencies (equilibrator, KBase network access).
Pre-existing collection ignores are documented in `coder/baseline.md`.

### Linting

```console
ruff check src/kbutillib
mypy src/kbutillib
```

---

## Deploy

### Poplar (systemd + nginx)

Service definitions and an nginx reverse-proxy config live under `deploy/poplar/`. Suitable
for running `kbu-api` as a persistent system service.

```console
# Example (adjust paths in the service file first)
sudo cp deploy/poplar/kbu-api.service /etc/systemd/system/
sudo systemctl enable --now kbu-api
```

### Docker

```console
docker compose -f deploy/docker/docker-compose.yml up
```

The compose file starts `kbu-api` behind nginx. Override environment variables for token
configuration and port bindings.

---

## Contributing

Contributions are very welcome. See the [Contributor Guide] for code style, testing
requirements, and the PR process.

## License

Distributed under the terms of the [MIT license][license].
_KBUtilLib_ is free and open source software.

## Issues

[File an issue][file an issue] with a detailed description and the output of `kbu doctor`.

## Credits

This project was generated from Christopher Henry's [cookiecutter-henry-hypermodern-python]
template, based on [@cjolowicz]'s [uv hypermodern python cookiecutter] template.

**Developed at Argonne National Laboratory**

[@cjolowicz]: https://github.com/cjolowicz
[pypi]: https://pypi.org/
[pypi status]: https://pypi.org/project/KBUtilLib/
[tests]: https://github.com/cshenry/KBUtilLib/actions?workflow=Tests
[codecov]: https://app.codecov.io/gh/cshenry/KBUtilLib
[pre-commit]: https://github.com/pre-commit/pre-commit
[ruff badge]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json
[ruff project]: https://github.com/charliermarsh/ruff
[cookiecutter-henry-hypermodern-python]: https://github.com/chenry/cookiecutter-henry-hypermodern-python
[uv hypermodern python cookiecutter]: https://github.com/bosd/cookiecutter-uv-hypermodern-python
[file an issue]: https://github.com/cshenry/KBUtilLib/issues
[pip]: https://pip.pypa.io/

<!-- github-only -->

[license]: https://github.com/cshenry/KBUtilLib/blob/main/LICENSE
[contributor guide]: https://github.com/cshenry/KBUtilLib/blob/main/CONTRIBUTING.md
