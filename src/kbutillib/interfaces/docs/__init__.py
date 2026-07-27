"""kbutillib.interfaces.docs — documentation generator namespace package.

Auto-generates MkDocs-compatible markdown from the capability registry.
All heavy imports (mkdocs, mkdocstrings) are lazy — this package imports
cleanly with zero doc-build dependencies installed.

Sub-modules
-----------
gen_capabilities
    Reads the CapabilityRegistry and writes ``docs/capabilities/index.md``
    plus per-domain pages.

mcp_catalog
    Renders the MCP tool catalog (name, description, input schema) as markdown.
"""
