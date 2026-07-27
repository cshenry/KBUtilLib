# Usage Guide

KBUtilLib exposes its capabilities through three transports — a CLI, a stdio MCP server, and
an HTTP API — all backed by the same capability registry. This page shows complete usage examples
for each transport, plus Python API patterns for scripted work.

---

## Python API

### Unified facade

`KBUtilLib` in `toolkit.py` composes all domain implementations as lazy properties. Instantiate
once, use throughout your session:

```python
from kbutillib import KBUtilLib

kbu = KBUtilLib()
```

**Biochemistry:**

```python
# Search ModelSEED biochemistry — no optional dependencies required
hits = kbu.biochem.search_compounds("atp")
atp = kbu.biochem.get_compound_by_id("cpd00002")
rxn = kbu.biochem.get_reaction_by_id("rxn00001")
```

**Thermodynamics:**

```python
# Legacy ModelSEED ΔG lookup (bundled data, always available)
dg = kbu.thermo.get_compound_deltag("cpd00002")

# Predictive ΔG — selects best available backend automatically
result = kbu.predictive_thermo.compound_dgf("cpd00002")
rxn_dg = kbu.predictive_thermo.reaction_dg_prime(
    reaction_id="rxn00001", ph=7.0, ionic_strength=0.1
)

# Check which backends loaded
status = kbu.predictive_thermo.backend_status()
```

**Metabolic modeling:**

```python
# Load a KBase model object (requires KBase token)
model = kbu.model.load_model(workspace="my_ws", model_ref="my_ws/my_model/1")

# Run FBA (requires cobra)
solution = kbu.fba.minimize_active_reactions(model)
loops = kbu.fba.find_flux_loops(model)
```

**Cheminformatics:**

```python
# Check which expansion backends are available
status = kbu.network_expansion.backend_status()

# Expand a seed set (requires minedatabase or retrorules)
result = kbu.network_expansion.expand(
    seed_compounds=["cpd00001", "cpd00002"],
    generations=2,
)
```

**Genome analysis:**

```python
# Load a KBase genome object (requires KBase token)
genome = kbu.genome.get_genome(workspace="my_ws", genome_id="my.genome")

# Sequence search (requires mmseqs2 binary)
hits = kbu.mmseqs.search(query_fasta="/tmp/query.faa", db="/tmp/target_db")
```

**KBase workspace:**

```python
# Requires KB_AUTH_TOKEN env var or ~/.kbase/token
obj = kbu.ws.get_object(workspace="my_ws", ref="my_ws/my_genome/1")
objects = kbu.ws.list_objects(workspace="my_ws", type="KBaseGenomes.Genome")
```

**AI / LLM:**

```python
# Argo LLM gateway (Argonne-hosted — no public access)
response = kbu.argo.chat("Summarize this KEGG pathway description: ...")
```

### Direct domain imports

For production scripts, use the canonical domain paths:

```python
from kbutillib.domains.biochem.ms_biochem_utils import MSBiochemUtils
from kbutillib.domains.thermo.thermo_utils import ThermoUtils
from kbutillib.domains.thermo.predictive_thermo_utils import PredictiveThermoUtils
from kbutillib.domains.cheminformatics.network_expansion_utils import NetworkExpansionUtils
from kbutillib.domains.modeling.ms_fba_utils import MSFBAUtils
from kbutillib.domains.genome.kb_genome_utils import KBGenomeUtils
from kbutillib.domains.kbase.kb_ws_utils import KBWSUtils
from kbutillib.domains.ai.argo_utils import ArgoUtils
```

### Querying the registry from Python

```python
from kbutillib.core.registry import get_registry

reg = get_registry()

# All capabilities
caps = reg.list_capabilities()

# Filter by domain
thermo_caps = reg.list_capabilities(domain="thermo")

# Look up a specific capability
spec = reg.get("biochem.search_compounds")
print(spec.summary)
print(spec.tags)

# Check backend availability without calling the function
available, reason = spec.availability()
if not available:
    print(f"Unavailable: {reason}")
```

---

## CLI (`kbu`)

The `kbu` command is available after any install — no extras required.

### Capability commands

```bash
# List all capabilities
kbu cap list

# Filter by domain
kbu cap list --domain biochem
kbu cap list --domain thermo

# Inspect a specific capability
kbu cap info biochem.search_compounds
# → prints: name, domain, summary, tags, input schema, availability

# Run a capability from the shell
kbu cap run biochem.search_compounds --query atp
kbu cap run biochem.search_compounds --query glucose --limit 10
kbu cap run biochem.get_compound_by_id --compound_id cpd00002
```

### Developer commands

```bash
# Check all backend availability
kbu doctor
# → prints each backend with a ✓ or ✗, then exits 0 if all pass

# Scaffold a new capability
kbu new-capability
# → interactive prompt: domain, name, summary, tags
# → writes a decorated stub under domains/<domain>/

# Version
kbu --version
```

---

## HTTP API (`kbu-api`)

### Start the server

```bash
pip install -e ".[api]"
kbu-api
# Starts uvicorn on http://0.0.0.0:8000
```

Environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `KBU_API_HOST` | `0.0.0.0` | Bind address |
| `KBU_API_PORT` | `8000` | Bind port |
| `KBU_API_TOKEN` | *(unset)* | Bearer token for auth; unset disables auth |

### Endpoints

**Health check:**

```bash
curl http://localhost:8000/health
# → {"status": "ok"}
```

**Package version:**

```bash
curl http://localhost:8000/version
# → {"version": "1.0.0"}
```

**List all capabilities:**

```bash
curl http://localhost:8000/v1/capabilities
# → [{"name": "biochem.search_compounds", "domain": "biochem", ...}, ...]
```

**Invoke a capability:**

```bash
# Search compounds
curl -X POST http://localhost:8000/v1/tools/biochem.search_compounds \
  -H "Content-Type: application/json" \
  -d '{"query": "atp", "limit": 5}'

# Get compound by ID
curl -X POST http://localhost:8000/v1/tools/biochem.get_compound_by_id \
  -H "Content-Type: application/json" \
  -d '{"compound_id": "cpd00002"}'

# Check thermo backend status
curl -X POST http://localhost:8000/v1/tools/thermo.backend_status \
  -H "Content-Type: application/json" \
  -d '{}'
```

**With bearer-token auth:**

```bash
KBU_API_TOKEN=mysecrettoken kbu-api &

curl -X POST http://localhost:8000/v1/tools/biochem.search_compounds \
  -H "Authorization: Bearer mysecrettoken" \
  -H "Content-Type: application/json" \
  -d '{"query": "atp"}'
```

**Swagger UI:** open `http://localhost:8000/docs` in a browser to explore and test endpoints
interactively.

---

## MCP server (`kbu-mcp`)

### Start the server

```bash
pip install -e ".[mcp]"
kbu-mcp
# Starts stdio MCP server — blocks until Ctrl-C or the client closes the connection
```

The server reads the capability registry at startup and exposes each capability as an MCP tool.
It does not bind a port — the client launches and communicates with the process directly over
stdin/stdout.

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "kbutillib": {
      "command": "kbu-mcp"
    }
  }
}
```

On Windows: `%APPDATA%\Claude\claude_desktop_config.json`.

If `kbu-mcp` is not on the system PATH in Claude's environment, use the full path:

```json
{
  "mcpServers": {
    "kbutillib": {
      "command": "/path/to/venv/bin/kbu-mcp"
    }
  }
}
```

Restart Claude Desktop after saving. The KBUtilLib tools appear in the tool list.

### Cursor

Go to **Settings → MCP → Add server**, set command to `kbu-mcp`, and save. Cursor will spawn
the process when you start a session.

### Calling tools via MCP

Once connected, any MCP client can call tools using the capability's dotted name:

```json
{
  "tool": "biochem.search_compounds",
  "arguments": {"query": "glucose", "limit": 5}
}
```

The response follows the MCP tool result format with the capability's return value serialized
as JSON.

---

## Environment and authentication

**KBase token** (required for workspace/SDK calls):

```bash
export KB_AUTH_TOKEN="your-kbase-token"
# or place the token in ~/.kbase/token
```

**Config file** (optional — overrides defaults):

```bash
~/.kbutillib/config.yaml     # per-user config
config.yaml                   # repo-root config (dev override)
```

Access config values in Python:

```python
from kbutillib.core.config import load_config

cfg = load_config()
ws_url = cfg.get("workspace.url", "https://ci.kbase.us/services/ws")
```
