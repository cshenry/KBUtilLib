# Getting Started with KBUtilLib

KBUtilLib is not on PyPI. You install it from a clone, then use it through one of three
transports: the `kbu` CLI, the `kbu-mcp` stdio MCP server, or the `kbu-api` FastAPI server.
This guide walks you from a fresh machine to a running environment.

---

## Prerequisites

- **Python 3.11 or newer** — check with `python --version`
- **Git** — to clone the repo
- **conda** (recommended) or a virtual environment of your choice

---

## Step 1 — Clone the repository

```bash
git clone https://github.com/cshenry/KBUtilLib.git
cd KBUtilLib
```

---

## Step 2 — Create and activate a conda environment

A ready-made environment file is included:

```bash
conda env create -f environment-reorg.yml
conda activate kbutillib-reorg
```

If you prefer a plain virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate    # Linux / macOS
# .venv\Scripts\activate     # Windows
```

---

## Step 3 — Install in editable mode

Install with all extras so every transport and the docs generator are available:

```bash
pip install -e ".[all]"
```

If you only need specific transports:

```bash
pip install -e .              # core library + CLI only
pip install -e ".[mcp]"       # adds kbu-mcp (MCP stdio server)
pip install -e ".[api]"       # adds kbu-api (FastAPI HTTP server)
pip install -e ".[apidocs]"   # adds mkdocs capability catalog
```

---

## Step 4 — Verify the install

```bash
kbu --help
```

You should see the top-level command list with `cap`, `doctor`, and `new-capability` groups.

```bash
python -c "from kbutillib import KBUtilLib; print('ok')"
```

Both commands completing without errors means the install is healthy.

---

## Step 5 — Explore registered capabilities

```bash
kbu cap list
```

This prints a table of every `@capability`-decorated function in the library. Biochem ships
three out of the box:

```
NAME                              DOMAIN       TAGS
biochem.search_compounds          biochem      biochem, search
biochem.get_compound_by_id        biochem      biochem, lookup
biochem.get_reaction_by_id        biochem      biochem, lookup
...
```

Filter by domain:

```bash
kbu cap list --domain thermo
kbu cap info biochem.search_compounds   # full signature and docstring
```

---

## Step 6 — First Python usage

Open a Python shell or notebook:

```python
from kbutillib import KBUtilLib

kbu = KBUtilLib()

# Biochemistry — no optional dependencies required
hits = kbu.biochem.search_compounds("atp")
print(hits[0])  # {'id': 'cpd00002', 'name': 'ATP', ...}

# Specific compound by ModelSEED ID
atp = kbu.biochem.get_compound_by_id("cpd00002")
print(atp["formula"])  # C10H12N5O13P3

# ModelSEED thermodynamics (bundled data — always available)
dg = kbu.thermo.get_compound_deltag("cpd00002")
print(dg)  # ΔGf in kJ/mol
```

The `KBUtilLib()` facade gives you access to every domain through lazy properties — nothing
is imported until you access it for the first time.

---

## Step 7 — Run the HTTP API

```bash
pip install -e ".[api]"    # skip if you used [all]
kbu-api
```

The server starts at `http://0.0.0.0:8000`. In another terminal:

```bash
# Health check
curl http://localhost:8000/health
# → {"status": "ok"}

# List all capabilities
curl http://localhost:8000/v1/capabilities

# Invoke a capability
curl -X POST http://localhost:8000/v1/tools/biochem.search_compounds \
  -H "Content-Type: application/json" \
  -d '{"query": "glucose", "limit": 3}'
```

The Swagger UI is at `http://localhost:8000/docs`.

Override the default host and port:

```bash
KBU_API_HOST=127.0.0.1 KBU_API_PORT=9000 kbu-api
```

Enable bearer-token authentication:

```bash
KBU_API_TOKEN=mysecrettoken kbu-api
# then pass: -H "Authorization: Bearer mysecrettoken"
```

Stop the server with `Ctrl-C`.

---

## Step 8 — Run the MCP server

The MCP server exposes every registered capability as an MCP tool over stdio. It requires
no port — the client launches the process directly.

```bash
pip install -e ".[mcp]"    # skip if you used [all]
kbu-mcp                    # starts stdio MCP server (blocks; Ctrl-C to stop)
```

### Connect Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "kbutillib": {
      "command": "kbu-mcp"
    }
  }
}
```

Restart Claude Desktop. The KBUtilLib tools appear in the MCP tool list.

If `kbu-mcp` is not on `PATH` in Claude's environment, use the full path:

```json
{
  "mcpServers": {
    "kbutillib": {
      "command": "/path/to/your/venv/bin/kbu-mcp"
    }
  }
}
```

### Connect Cursor

Go to **Settings → MCP → Add server**, set the command to `kbu-mcp` (or the full path),
and save. See `src/kbutillib/interfaces/mcp/README.md` for more client examples.

---

## Step 9 — Check backend availability

```bash
kbu doctor
```

This probes every registered backend (rdkit, cobra, equilibrator, KBase token, external
binaries) and prints which are ready and which are missing. Missing backends are expected
for optional dependencies — the library degrades gracefully.

---

## Next steps

| What | Where |
|------|-------|
| Domain-specific capabilities | `src/kbutillib/domains/<domain>/README.md` |
| Transport details (auth, env vars, config) | `src/kbutillib/interfaces/<cli\|mcp\|api>/README.md` |
| Add a new capability | [CONTRIBUTING.md](CONTRIBUTING.md) |
| core registry and decorator | `src/kbutillib/core/README.md` |
| MkDocs capability catalog | `pip install -e ".[apidocs]"` then `mkdocs serve` |
| Run tests | `python -m pytest tests/ -q` |
