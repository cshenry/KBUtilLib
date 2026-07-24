# Getting Started with KBUtilLib

KBUtilLib is a domain-organized utility toolkit for KBase, ModelSEED, and bioinformatics workflows. One capability registry, three transports (CLI, MCP, HTTP API).

---

## Install

```console
git clone https://github.com/cshenry/KBUtilLib.git
cd KBUtilLib

# Core library only
pip install -e .

# With specific transports
pip install -e ".[mcp]"      # MCP server for AI agents
pip install -e ".[api]"      # FastAPI HTTP server
pip install -e ".[apidocs]"  # mkdocs documentation

# Everything
pip install -e ".[all]"
```

**Python 3.11+ required.** If you use conda:

```console
conda env create -f environment.yml
conda activate kbutillib
pip install -e ".[all]"
```

---

## First use

```python
from kbutillib.toolkit import KBUtilLib

kbu = KBUtilLib()

# Biochemistry — always available, no optional deps
hits = kbu.biochem.search_compounds("atp")

# Thermodynamics
dg = kbu.thermo.get_compound_deltag("cpd00002")

# Check which backends loaded
print(kbu.predictive_thermo.backend_status())
```

---

## Architecture

```
src/kbutillib/
├── core/           ← @capability decorator, registry, BaseUtils, errors
├── domains/        ← all scientific implementations (canonical paths)
│   ├── ai/         ← ArgoUtils, AiCurationUtils, KbPlmUtils
│   ├── biochem/    ← MSBiochemUtils (@capability reference impl)
│   ├── cheminformatics/ ← NetworkExpansionUtils, VerabUtils
│   ├── external/   ← BvbrcUtils, RcsbPdbUtils, KbUniprotUtils
│   ├── genome/     ← KBGenomeUtils, MMSeqsUtils, Skani, annotation/
│   ├── kbase/      ← KBWSUtils, KBSDKUtils, narrative audit, endpoints
│   ├── modeling/   ← MSFBAUtils, MSReconstructionUtils, KBModelUtils
│   ├── notebook/   ← NotebookSession, VectorStore, EscherUtils
│   └── thermo/     ← ThermoUtils, PredictiveThermoUtils, thermo_predictors/
├── interfaces/     ← transports (mcp/, api/, cli/, docs/)
├── agents/         ← KING bundles, researchos config
└── toolkit.py      ← KBUtilLib facade (lazy-constructed domain properties)
```

The registry is the heart of the system. Every `@capability`-decorated method is registered once and available to all three transports automatically.

---

## CLI quickstart

```console
kbu --help                          # list all commands
kbu doctor                          # check backend availability
kbu cap list                        # all registered capabilities
kbu cap list --domain biochem       # filter by domain
kbu cap info biochem.search_compounds
kbu cap run biochem.search_compounds --query atp
```

---

## Run the MCP server (for AI agents)

Connect Claude Desktop, Cursor, or any MCP client to your capabilities:

```console
pip install -e ".[mcp]"
kbu-mcp                    # starts stdio MCP server
```

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "kbutillib": { "command": "kbu-mcp" }
  }
}
```

See `src/kbutillib/interfaces/mcp/README.md` for Cursor setup and details.

---

## Run the HTTP API

```console
pip install -e ".[api]"
kbu-api                    # uvicorn on http://0.0.0.0:8000
```

Swagger UI at `http://localhost:8000/docs`. See `src/kbutillib/interfaces/api/README.md`.

---

## Run tests

```console
python -m pytest tests/ -q
```

Some tests require optional packages (equilibrator, KBase token). Pre-existing ignores are documented in the `Makefile` and `pyproject.toml`.

---

## Add a new capability

The fastest path is the scaffolder:

```console
kbu new-capability
```

It prompts for domain, name, summary, and tags, then writes a stub with the correct `@capability` decorator. See `CONTRIBUTING.md` for the full pattern.

---

## Where to look next

- Per-domain READMEs: `src/kbutillib/domains/<domain>/README.md`
- Interface details: `src/kbutillib/interfaces/<mcp|api|cli|docs>/README.md`
- Capability pattern: `CONTRIBUTING.md`
- API reference: `mkdocs serve` (requires `[apidocs]`)
