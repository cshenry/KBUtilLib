"""kbutillib.interfaces — transport adapter namespace package.

Thin adapters that read from the capability registry and expose KBUtilLib
capabilities over various transports (MCP, HTTP API, CLI).  Heavy transport
dependencies (mcp, fastapi, etc.) are imported lazily inside each sub-package
so that ``import kbutillib`` never requires them.
"""
