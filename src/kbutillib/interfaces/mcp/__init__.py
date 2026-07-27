"""kbutillib.interfaces.mcp — MCP transport adapter.

Exposes KBUtilLib capabilities registered in the capability registry as MCP
tools.  The ``mcp`` / ``fastmcp`` packages are imported **lazily** (inside
functions) so that this package can be imported without them installed.

Entry points
------------
``build_server(registry=None, app=None)``
    Build and return a FastMCP server instance with tools derived from the
    capability registry.

``run_stdio()``
    Run the MCP server over stdio (blocking).

``run_http(host, port)``
    Run the MCP server over Streamable HTTP (blocking).

``main()``
    Console-script entry point for ``kbu-mcp``.
"""

from kbutillib.interfaces.mcp.server import (
    build_server,
    capability_to_tool_spec,
    main,
    run_http,
    run_stdio,
)

__all__ = [
    "build_server",
    "capability_to_tool_spec",
    "main",
    "run_http",
    "run_stdio",
]
