"""MCP tool catalog documentation generator.

Renders the MCP tool catalog (tool name, description, input schema) as
MkDocs-compatible markdown.  No ``mcp`` package is required at import time —
all heavy imports are deferred to call time.

Public API
----------
``tool_spec_to_markdown(tool_spec) -> str``
    Render a single MCP tool spec dict as a markdown section.

``generate_mcp_catalog(registry) -> str``
    Render the full MCP tool catalog page from a
    :class:`~kbutillib.core.registry.CapabilityRegistry`.

``generate_mcp_catalog_file(registry, output_path=None) -> str``
    Like ``generate_mcp_catalog`` but optionally writes to *output_path*.

Run standalone::

    python -m kbutillib.interfaces.docs.mcp_catalog
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kbutillib.core.registry import CapabilityRegistry

__all__ = [
    "tool_spec_to_markdown",
    "generate_mcp_catalog",
    "generate_mcp_catalog_file",
]

# ---------------------------------------------------------------------------
# Module-level header injected into every generated file
# ---------------------------------------------------------------------------

_HEADER = """\
<!-- AUTO-GENERATED — do not edit manually.
     Regenerate with: python -m kbutillib.interfaces.docs.mcp_catalog -->
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _schema_table(schema: dict[str, Any]) -> list[str]:
    """Render a JSON Schema ``properties`` dict as a markdown table."""
    props = schema.get("properties", {})
    required = set(schema.get("required", []))

    if not props:
        return ["*(no parameters)*\n"]

    lines = [
        "| Parameter | Type | Required | Description |",
        "|-----------|------|----------|-------------|",
    ]
    for param_name, param_schema in sorted(props.items()):
        param_type = param_schema.get("type", "any")
        req = "✅" if param_name in required else "—"
        desc = param_schema.get("description", "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{param_name}` | `{param_type}` | {req} | {desc} |")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Public generators
# ---------------------------------------------------------------------------


def tool_spec_to_markdown(tool_spec: dict[str, Any]) -> str:
    """Render a single MCP tool spec dict as a markdown section string.

    Parameters
    ----------
    tool_spec:
        Dict with keys ``name``, ``description``, and ``input_schema``
        (as returned by
        :func:`~kbutillib.interfaces.mcp.server.capability_to_tool_spec`).

    Returns
    -------
    str
        Markdown text for this tool.
    """
    name = tool_spec.get("name", "unknown")
    description = tool_spec.get("description", "").strip()
    input_schema = tool_spec.get("input_schema", {})

    anchor = name.replace(".", "-").replace("_", "-").lower()
    lines: list[str] = [
        f"### `{name}` {{#{anchor}}}\n",
    ]

    if description:
        lines.append(description)
        lines.append("")

    lines.append("**Input parameters:**\n")
    lines.extend(_schema_table(input_schema))

    if input_schema:
        lines.append("<details><summary>Raw JSON Schema</summary>\n")
        lines.append("```json")
        lines.append(json.dumps(input_schema, indent=2))
        lines.append("```\n")
        lines.append("</details>\n")

    lines.append("---\n")
    return "\n".join(lines)


def generate_mcp_catalog(registry: CapabilityRegistry) -> str:
    """Render the full MCP tool catalog as a markdown string.

    Only capabilities whose ``transports`` set includes ``"mcp"`` are included.

    Parameters
    ----------
    registry:
        The registry to read capabilities from.

    Returns
    -------
    str
        Markdown content suitable for ``docs/mcp-catalog.md``.
    """
    # Import the pure helper — no mcp package needed
    from kbutillib.interfaces.mcp.server import capability_to_tool_spec  # noqa: PLC0415

    mcp_specs = registry.list(transport="mcp")
    mcp_specs_sorted = sorted(mcp_specs, key=lambda s: (s.domain, s.name))

    tool_specs = [capability_to_tool_spec(spec) for spec in mcp_specs_sorted]

    lines: list[str] = [
        _HEADER,
        "# MCP Tool Catalog\n",
        f"**{len(tool_specs)}** tool(s) exposed over the MCP transport.\n",
    ]

    if not tool_specs:
        lines.append("> No capabilities are registered for the MCP transport.\n")
        return "\n".join(lines)

    # Summary table
    lines.append("## Tools\n")
    lines.append("| Tool | Description | Status |")
    lines.append("|------|-------------|--------|")

    if len(mcp_specs_sorted) != len(tool_specs):
        raise ValueError(
            "mcp_specs_sorted and tool_specs length mismatch: "
            f"{len(mcp_specs_sorted)} != {len(tool_specs)}"
        )
    for spec, ts in zip(mcp_specs_sorted, tool_specs):
        available, _ = spec.availability()
        status = "✅" if available else "⚠️"
        desc_first_line = ts.get("description", "").splitlines()[0] if ts.get("description") else "—"
        desc_first_line = desc_first_line.replace("|", "\\|")
        anchor = spec.name.replace(".", "-").replace("_", "-").lower()
        lines.append(f"| [`{spec.name}`](#{anchor}) | {desc_first_line} | {status} |")

    lines.append("")
    lines.append("## Tool Reference\n")

    for ts in tool_specs:
        lines.append(tool_spec_to_markdown(ts))

    return "\n".join(lines)


def generate_mcp_catalog_file(
    registry: CapabilityRegistry,
    output_path: str | Path | None = None,
) -> str:
    """Generate the MCP catalog markdown and optionally write to *output_path*.

    Parameters
    ----------
    registry:
        The registry to read capabilities from.
    output_path:
        If given, write the markdown to this file path.  The parent directory
        is created if it does not exist.

    Returns
    -------
    str
        The generated markdown string.
    """
    content = generate_mcp_catalog(registry)

    if output_path is not None:
        dest = Path(output_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

    return content


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _main() -> None:
    """Entry point for ``python -m kbutillib.interfaces.docs.mcp_catalog``."""
    import argparse  # noqa: PLC0415

    from kbutillib.core.registry import CapabilityRegistry  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        description="Generate MCP tool catalog markdown page.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write output to this file (default: print to stdout).",
    )
    parser.add_argument(
        "--register",
        action="store_true",
        default=True,
        help="Call register_all() to populate the registry (default: True).",
    )
    parser.add_argument(
        "--no-register",
        action="store_false",
        dest="register",
        help="Do not call register_all(); emit the catalog for an empty registry.",
    )
    args = parser.parse_args()

    registry = CapabilityRegistry()

    if args.register:
        try:
            from kbutillib import KBUtilLib  # noqa: PLC0415
            from kbutillib.core.capability import register_all  # noqa: PLC0415
            from kbutillib.core.registry import get_registry  # noqa: PLC0415

            app = KBUtilLib(config_file=None)
            register_all(app, registry=get_registry())
            registry = get_registry()
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] Could not populate registry via register_all: {exc}")

    content = generate_mcp_catalog_file(registry, output_path=args.output)

    if args.output is None:
        print(content)
    else:
        print(f"Wrote {args.output}")


if __name__ == "__main__":  # pragma: no cover
    _main()
