# kbutillib.agents — KIND Bundles & ResearchOS

Self-contained KIND agent bundles that can be installed into Claude Code, Cursor, or other MCP-compatible agents, plus the `researchos/` configuration manager.

## What lives here

| Path | Purpose |
|------|---------|
| `kind_install.py` | Bundle install/uninstall/status logic — called by `kbu kind install/uninstall/status` |
| `researchos/` | Generates `.mcp` and `.claude` configuration files for researchos environments |

**Note on `kind_app/`:** The canonical KIND bundle directory is `src/kbutillib/kind_app/` (at the package root, not here). It contains `bundle.json`, `skill.md`, and the `verab/` sub-bundle. `interfaces/cli/king.py` resolves bundles from that root directory. A stale duplicate at `agents/kind_app/` was removed — if you need to reference the bundle location, it is always `src/kbutillib/kind_app/`.

## KIND bundles

A KIND bundle is a directory containing:
- `bundle.json` — bundle metadata: `id`, `name`, `description`, `capabilities`, `mcp_tools`
- `skill.md` — natural-language skill description for the agent
- Optional sub-bundles (e.g., `verab/`) for domain-specific capabilities

## Install a KIND bundle

```console
# List available bundles in the registry
kbu kind status

# Install the main kbu bundle into your agent config
kbu kind install kind_app

# Install the Verab bundle
kbu kind install kind_app/verab

# Uninstall
kbu kind uninstall kind_app
```

`kind_install.py` writes the MCP server entry into the agent's configuration file (e.g., `~/.claude/config.json` or `.cursor/mcp.json`) and records the install in a local state file.

## researchos/


## Adding a new bundle

1. Create `src/kbutillib/kind_app/<bundle_name>/bundle.json` with required fields.
2. Write `skill.md` describing the bundle's capabilities for the agent.
3. Register the bundle in `kind_app/bundle.json`'s `sub_bundles` list.
4. Test with `kbu kind status` and `kbu kind install <bundle_name>`.
