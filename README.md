# KBUtilLib

[![PyPI](https://img.shields.io/pypi/v/KBUtilLib.svg)][pypi status]
[![Status](https://img.shields.io/pypi/status/KBUtilLib.svg)][pypi status]
[![Python Version](https://img.shields.io/pypi/pyversions/KBUtilLib)][pypi status]
[![License](https://img.shields.io/pypi/l/KBUtilLib)][license]

[![Tests](https://github.com/cshenry/KBUtilLib/workflows/Tests/badge.svg)][tests]
[![Codecov](https://codecov.io/gh/cshenry/KBUtilLib/branch/main/graph/badge.svg)][codecov]

[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)][pre-commit]
[![Ruff codestyle][ruff badge]][ruff project]

KBUtilLib is a domain-organized utility library for KBase bioinformatics: biochemistry search,
metabolic modeling, thermodynamics, genome analysis, cheminformatics, and AI/LLM workflows —
all exposed through a single capability registry and your choice of transport: CLI, MCP stdio
server, or HTTP API.

> **Not on PyPI yet.** Install from source with `pip install -e .` — see [Installation](#installation) below.

**New here?** → **[GETTING_STARTED.md](GETTING_STARTED.md)** walks you from a fresh clone to a
running `kbu` command in under ten minutes.

---

## Architecture

Every capability is registered once — via the `@capability` decorator — and all three transports
read from the same registry. Adding a capability to a domain makes it immediately available to
the CLI, the MCP server, and the HTTP API without any additional wiring.

```
┌─────────────────────────────────────────────────────────┐
│                    @capability registry                  │
│              (core/registry.py — CapabilitySpec)         │
└───────────────────────┬─────────────────────────────────┘
                        │  populated at import time
          ┌─────────────┼──────────────┐
          ▼             ▼              ▼
    domains/         domains/       domains/
    biochem/         modeling/      thermo/  …
    (MSBiochemUtils) (MSFBAUtils)   (ThermoUtils)
          │             │              │
          └─────────────┼──────────────┘
                        │  read at startup
          ┌─────────────┼──────────────┐
          ▼             ▼              ▼
    kbu (CLI)     kbu-mcp (MCP)  kbu-api (HTTP)
    Click          stdio JSON-RPC   FastAPI/uvicorn
```

The `KBUtilLib` facade in `toolkit.py` composes all domain implementations as lazy properties.
Use it for scripted or interactive work when you want one object with access to everything.
For production imports, use the canonical `kbutillib.domains.*` paths.

---

## Installation

KBUtilLib is not on PyPI. Clone the repo, then install from source:

```bash
git clone https://github.com/cshenry/KBUtilLib.git
cd KBUtilLib

# Core library — no transport dependencies
pip install -e .

# With MCP server support (adds mcp SDK)
pip install -e ".[mcp]"

# With FastAPI HTTP server (adds fastapi + uvicorn)
pip install -e ".[api]"

# With auto-generated MkDocs capability catalog
pip install -e ".[apidocs]"

# Everything
pip install -e ".[all]"
```

**Python 3.11+** is required. A conda environment file ships with the repo:

```bash
conda env create -f environment-reorg.yml   # creates the kbutillib-reorg env
conda activate kbutillib-reorg
pip install -e ".[all]"
```

---

## Quick Start

### Python API

```python
from kbutillib import KBUtilLib

kbu = KBUtilLib()

# Biochemistry — ModelSEED compound search (no optional deps required)
hits = kbu.biochem.search_compounds("atp")

# Thermodynamics — legacy ModelSEED ΔG lookup
dg = kbu.thermo.get_compound_deltag("cpd00002")

# Thermodynamics — predictive ΔG via best available backend
result = kbu.predictive_thermo.compound_dgf("cpd00002")

# Cheminformatics — metabolic network expansion
expanded = kbu.network_expansion.expand(seed_compounds=["cpd00001", "cpd00002"], generations=2)

# KBase workspace
obj = kbu.ws.get_object(workspace="my_ws", ref="my_ws/my_genome/1")
```

For production scripts, prefer direct domain imports:

```python
from kbutillib.domains.biochem.ms_biochem_utils import MSBiochemUtils
from kbutillib.domains.modeling.ms_fba_utils import MSFBAUtils
from kbutillib.domains.thermo.predictive_thermo_utils import PredictiveThermoUtils
```

### CLI (`kbu`)

The `kbu` command is available after any install — no extras required.

```bash
kbu --help
kbu doctor                                   # check backend availability
kbu cap list                                 # all registered capabilities
kbu cap list --domain biochem                # filter by domain
kbu cap info biochem.search_compounds        # signature + docstring
kbu cap run biochem.search_compounds --query atp --limit 5
kbu new-capability                           # scaffold a new @capability stub
```

### HTTP API (`kbu-api`)

```bash
pip install -e ".[api]"
kbu-api                                      # starts on http://0.0.0.0:8000

# Health check
curl http://localhost:8000/health

# List capabilities
curl http://localhost:8000/v1/capabilities

# Invoke a capability
curl -X POST http://localhost:8000/v1/tools/biochem.search_compounds \
  -H "Content-Type: application/json" \
  -d '{"query": "atp", "limit": 5}'
```

Swagger UI is at `http://localhost:8000/docs`. Override host/port with `KBU_API_HOST` and
`KBU_API_PORT` environment variables. Bearer-token auth is enabled via `KBU_API_TOKEN`.

---

## Domain Modules

| Domain | Import path | What it provides |
|--------|-------------|-----------------|
| **biochem** | `kbutillib.domains.biochem` | ModelSEED compound and reaction search, ID lookup, cross-reference resolution (`MSBiochemUtils`) |
| **thermo** | `kbutillib.domains.thermo` | ModelSEED ΔG lookup (`ThermoUtils`); multi-backend predictive ΔG (`PredictiveThermoUtils`) with equilibrator, modelseed, modelseed_db, dgpredictor, and molgpk backends |
| **cheminformatics** | `kbutillib.domains.cheminformatics` | Metabolic network expansion via Pickaxe/RetroRules (`NetworkExpansionUtils`); SMARTS-based rule screening (`VerabUtils`) |
| **modeling** | `kbutillib.domains.modeling` | Flux balance analysis (`MSFBAUtils`), draft model reconstruction (`MSReconstructionUtils`), reaction templates (`MSTemplateUtils`), KBase model I/O (`KBModelUtils`) |
| **genome** | `kbutillib.domains.genome` | KBase genome objects (`KBGenomeUtils`), sequence alignment (`MMSeqsUtils`, `SkaniUtils`), ontology mapping (`OntomapUtils`), annotation pipeline (`annotation/`) |
| **kbase** | `kbutillib.domains.kbase` | Workspace API (`KBWSUtils`), SDK client (`KBSDKUtils`), narrative audit, catalog client, endpoint resolver |
| **ai** | `kbutillib.domains.ai` | Argo LLM gateway client (`ArgoUtils`), AI curation pipelines (`AiCurationUtils`), protein language models (`KBPLMUtils`) |
| **external** | `kbutillib.domains.external` | BVBRC/PATRIC genome queries (`BvbrcUtils`), RCSB PDB structure retrieval (`RcsbPdbUtils`), UniProt via KBase (`KbUniprotUtils`) |
| **notebook** | `kbutillib.domains.notebook` | Provenanced notebook cache (`NotebookSession`, `Cache`), typed vector store (`VectorStore`), Escher map rendering (`EscherUtils`) |

---

## Transports

### CLI — `kbu`

Available after `pip install -e .` with no extras.

```bash
kbu cap list                    # tabular listing with tags and backend requirements
kbu cap info <name>             # full signature, docstring, and availability
kbu cap run <name> [--arg val]  # invoke from the shell
kbu doctor                      # check all backends; exits 0 if all pass
kbu new-capability              # interactive scaffold for a new capability
```

### MCP server — `kbu-mcp`

Exposes every registered capability as an MCP tool over stdio. Works with Claude Desktop,
Cursor, and any MCP-compatible client.

```bash
pip install -e ".[mcp]"
kbu-mcp                         # stdio server — no port, no HTTP
```

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "kbutillib": {
      "command": "kbu-mcp"
    }
  }
}
```

**Cursor**: Settings → MCP → Add server → command `kbu-mcp`.

See `src/kbutillib/interfaces/mcp/README.md` for full configuration details.

### HTTP API — `kbu-api`

FastAPI application with bearer-token authentication and auto-generated OpenAPI docs.

```bash
pip install -e ".[api]"
kbu-api
```

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness check |
| `GET` | `/version` | Package version |
| `GET` | `/v1/capabilities` | List all capabilities with metadata |
| `POST` | `/v1/tools/{name}` | Invoke a capability by dotted name |

See `src/kbutillib/interfaces/api/README.md` for auth configuration and deployment options.

---

## Capability Registry

Every public method decorated with `@capability` is registered automatically at import time.
The decorator is completely inert to normal calls — it attaches metadata and registers the
function, then returns the original callable unchanged. No wrappers, no overhead.

```python
from kbutillib.core.capability import capability

@capability(
    domain="biochem",
    summary="Search ModelSEED biochemistry for compounds matching a query string.",
    tags=("biochem", "search"),
    visibility="public",
)
def search_compounds(self, query: str, limit: int = 20) -> list[dict]:
    """Return up to *limit* compound records matching *query*."""
    ...
```

Query the registry:

```bash
kbu cap list                              # all capabilities, tabular
kbu cap info biochem.search_compounds     # detail for one capability
```

```python
from kbutillib.core.registry import get_registry

reg = get_registry()
caps = reg.list_capabilities()                    # all CapabilitySpec objects
spec = reg.get("biochem.search_compounds")        # single spec by name
available, reason = spec.availability()           # check backend readiness
```

---

## Package Structure

```
src/kbutillib/
├── __init__.py              # public API — re-exports all domain classes
├── toolkit.py               # KBUtilLib lazy facade
├── core/                    # registry, @capability, errors, config, BaseUtils
├── domains/
│   ├── biochem/             # MSBiochemUtils (reference @capability implementation)
│   ├── thermo/              # ThermoUtils, PredictiveThermoUtils + thermo_predictors/
│   ├── cheminformatics/     # NetworkExpansionUtils, VerabUtils + verab/
│   ├── modeling/            # MSFBAUtils, MSReconstructionUtils, KBModelUtils, ...
│   ├── genome/              # KBGenomeUtils, MMSeqsUtils, SkaniUtils + annotation/
│   ├── kbase/               # KBWSUtils, KBSDKUtils, narrative audit, endpoints
│   ├── ai/                  # ArgoUtils, AiCurationUtils, KBPLMUtils
│   ├── external/            # BvbrcUtils, RcsbPdbUtils, KbUniprotUtils
│   └── notebook/            # NotebookSession, Cache, VectorStore, EscherUtils
├── interfaces/
│   ├── cli/                 # kbu (Click) — cap list/info/run, new-capability, doctor
│   ├── mcp/                 # kbu-mcp (stdio MCP server)
│   ├── api/                 # kbu-api (FastAPI/uvicorn)
│   └── docs/                # mkdocs catalog generator
├── agents/                  # KING self-install bundles, researchos config
└── deploy/
    ├── poplar/              # systemd + nginx service definitions
    └── docker/              # docker-compose and Dockerfile
```

---

## Migration Note

The flat module layout (`kbutillib.ms_biochem_utils`, etc.) is no longer the primary import
surface. The top-level re-exports remain stable:

```python
# Preferred — stable across releases
from kbutillib import MSBiochemUtils, KBWSUtils, ThermoUtils

# Canonical domain path
from kbutillib.domains.biochem.ms_biochem_utils import MSBiochemUtils
```

---

## Development

```bash
# Scaffold a new capability
kbu new-capability
# → prompts for domain, name, summary, tags, then writes the decorated stub

# Run tests
python -m pytest tests/ -q

# Lint and format
ruff check src/kbutillib
ruff format src/kbutillib
mypy src/kbutillib

# Generate MkDocs capability catalog
python -m kbutillib.interfaces.docs.gen_capabilities
mkdocs serve
```

See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the full contribution guide, test conventions,
and the step-by-step process for adding a new domain or capability.

---

## Deploy

```bash
# Poplar — systemd service + nginx reverse proxy
sudo cp deploy/poplar/kbu-api.service /etc/systemd/system/
sudo systemctl enable --now kbu-api

# Docker
docker compose -f deploy/docker/docker-compose.yml up
```

---

## License

Distributed under the terms of the [MIT license][license].

## Issues

[File an issue][file an issue] with a description and the output of `kbu doctor`.

## Credits

Developed at **Argonne National Laboratory** by Christopher Henry.

[@cjolowicz]: https://github.com/cjolowicz
[pypi status]: https://pypi.org/project/KBUtilLib/
[tests]: https://github.com/cshenry/KBUtilLib/actions?workflow=Tests
[codecov]: https://app.codecov.io/gh/cshenry/KBUtilLib
[pre-commit]: https://github.com/pre-commit/pre-commit
[ruff badge]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json
[ruff project]: https://github.com/charliermarsh/ruff
[file an issue]: https://github.com/cshenry/KBUtilLib/issues

<!-- github-only -->

[license]: https://github.com/cshenry/KBUtilLib/blob/main/LICENSE
[contributor guide]: https://github.com/cshenry/KBUtilLib/blob/main/CONTRIBUTING.md
