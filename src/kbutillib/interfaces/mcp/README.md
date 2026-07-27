# kbutillib.interfaces.mcp — MCP Server

Exposes every `@capability`-decorated function in the registry as a Model Context Protocol (MCP) tool over stdio. Connect it to Claude Desktop, Cursor, or any MCP-compatible agent.

## What lives here

| File | Purpose |
|------|---------|
| `server.py` | MCP server — reads the capability registry at startup, wraps each capability as an MCP tool, and serves over stdio |

The server is transport-only: it contains no domain logic. Every capability it exposes comes from the registry populated by `@capability` decorators in `domains/`.

## Install

```console
pip install -e ".[mcp]"
```

This adds `mcp` (the Python MCP SDK) to your environment. The core library works without it — the MCP server is an optional interface.

## Run

```console
# stdio mode (default — what MCP clients expect)
kbu-mcp

# or equivalently
python -m kbutillib.interfaces.mcp
```

The server starts, loads all registered capabilities, and waits for JSON-RPC messages on stdin/stdout. It does not bind a port.

## Connect to Claude Desktop

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

Restart Claude Desktop. The registered capabilities appear as available tools.

## Connect to Cursor

In Cursor settings → MCP → Add server:

```json
{
  "name": "kbutillib",
  "command": "kbu-mcp",
  "type": "stdio"
}
```

## How a `@capability` becomes an MCP tool

At startup `server.py` calls `register_all()`, which imports every domain module and triggers `@capability` decoration. Each `CapabilitySpec` in the registry is wrapped as an MCP `Tool` with:
- `name`: the capability name (`biochem.search_compounds`)
- `description`: the capability summary
- `inputSchema`: generated from the method's type annotations or the `input_model` pydantic schema

Capabilities that are unavailable (missing optional deps) are still listed with an `unavailable` annotation — agents can introspect availability before calling.

## Verify

```console
kbu cap list    # same capabilities the MCP server will expose
```
