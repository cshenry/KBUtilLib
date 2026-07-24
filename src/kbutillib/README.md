# kbutillib package layout

This is the source root for KBUtilLib. Here's what lives where:

## Package structure

```
kbutillib/
├── __init__.py          # Public API — imports all domain classes
├── toolkit.py           # KBUtilLib facade — one entry point for all domains
├── compartments.py      # Metabolic compartment helpers
├── layout.py            # Metabolic model layout utilities
│
├── core/                # Foundation: registry, errors, capability decorator, config
├── domains/             # Domain implementations (biochem, thermo, cheminformatics, ...)
├── interfaces/          # Transport adapters (CLI, MCP, FastAPI, docs)
├── agents/              # KING bundles, researchos, agent utilities
└── king_app/            # Installable KING bundle (main + verab)
```

## Where to look

| I want to...                          | Look in...                          |
|---------------------------------------|-------------------------------------|
| Use the library                       | `from kbutillib import KBUtilLib`   |
| Add a new domain capability           | `domains/<domain>/` + `@capability` |
| Understand the registry               | `core/registry.py`, `core/README.md`|
| Run the MCP server                    | `interfaces/mcp/`                   |
| Run the REST API                      | `interfaces/api/`                   |
| Use the CLI                           | `interfaces/cli/` or `kbu --help`   |
| Deploy to Poplar                      | `../../deploy/poplar/`              |
| Add a KING bundle                     | `king_app/` as template             |

## Adding a new capability (30-second guide)

1. Find your domain: `domains/<domain>/`
2. Add method to `*Impl` class with `@capability(...)`
3. Add `available`/`unavailable_reason` props if dep-gated
4. Run `kbu cap list` to verify
5. See `CONTRIBUTING.md` for the full pattern
