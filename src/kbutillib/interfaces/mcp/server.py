"""MCP server adapter — exposes capability registry as MCP tools.

All ``mcp``/``fastmcp`` imports are **lazy** (inside functions).  This module
can be imported without the ``mcp`` package installed; only ``build_server()``,
``run_stdio()``, and ``run_http()`` require it at call time.

Public API
----------
``capability_to_tool_spec(spec) -> dict``
    Pure helper — no mcp needed.  Returns a dict with keys
    ``name``, ``description``, ``input_schema`` (JSON Schema dict).

``build_server(registry=None, app=None) -> FastMCP``
    Build a FastMCP server instance.  ``registry`` defaults to the global
    registry singleton; when ``None`` *and* ``app`` is provided,
    ``register_all(app)`` is called first.  When both are ``None``, a fresh
    :class:`~kbutillib.KBUtilLib` is constructed and ``register_all`` is run.

``run_stdio()``
    Run the MCP server over stdio transport (blocking).

``run_http(host="127.0.0.1", port=8000)``
    Run the MCP server over Streamable HTTP transport (blocking).

``main()``
    Console-script entry point for ``kbu-mcp``.
    Supports ``--list`` (print tool names + availability, no server) and
    ``--stdio`` / ``--http`` transport selection.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kbutillib.core.registry import CapabilityRegistry, CapabilitySpec

__all__ = [
    "capability_to_tool_spec",
    "build_server",
    "run_stdio",
    "run_http",
    "main",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pure helper — usable without mcp installed
# ---------------------------------------------------------------------------


def capability_to_tool_spec(spec: CapabilitySpec) -> dict[str, Any]:
    """Convert a :class:`~kbutillib.core.registry.CapabilitySpec` to a tool spec dict.

    This function does **not** import ``mcp``; it is unit-testable offline.

    Parameters
    ----------
    spec:
        A registered :class:`~kbutillib.core.registry.CapabilitySpec`.

    Returns
    -------
    dict
        A dictionary with keys:

        ``name``
            The capability name (dotted, e.g. ``"biochem.search_compounds"``).
        ``description``
            Human-readable description (effective summary or full description).
        ``input_schema``
            JSON Schema dict derived from ``spec.input_model.model_json_schema()``
            if an input model is present, otherwise an empty object schema.
    """
    name = spec.name
    description = spec.description or spec.effective_summary()

    if spec.input_model is not None:
        try:
            input_schema: dict[str, Any] = spec.input_model.model_json_schema()
        except Exception as exc:  # noqa: BLE001
            logger.warning("capability_to_tool_spec: could not build schema for %r: %s", name, exc)
            input_schema = {"type": "object", "properties": {}}
    else:
        input_schema = {"type": "object", "properties": {}}

    return {
        "name": name,
        "description": description,
        "input_schema": input_schema,
    }


# ---------------------------------------------------------------------------
# Internal: resolve registry
# ---------------------------------------------------------------------------


def _resolve_registry(
    registry: CapabilityRegistry | None,
    app: Any,
) -> CapabilityRegistry:
    """Return a populated registry, building one if needed."""
    from kbutillib.core.capability import register_all
    from kbutillib.core.registry import get_registry

    if registry is not None:
        return registry

    # If no registry provided, use the global singleton.
    reg = get_registry()

    if app is not None:
        # register_all is idempotent for new specs (duplicates are warned + skipped).
        register_all(app, registry=reg)
    else:
        # Build a default KBUtilLib facade and register everything.
        try:
            from kbutillib import KBUtilLib

            kbu = KBUtilLib()
            register_all(kbu, registry=reg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("build_server: could not auto-register capabilities: %s", exc)

    return reg


# ---------------------------------------------------------------------------
# Handler factory — separated so the closure is testable
# ---------------------------------------------------------------------------


def _make_handler(spec: CapabilitySpec) -> Any:
    """Return a coroutine handler for *spec* suitable for FastMCP tool registration.

    If ``spec.availability()`` returns ``(False, reason)`` the handler returns
    an error dict immediately without calling the underlying function.  This
    check is performed at *call time*, not at registration time, so the server
    can start even when backends are unavailable.

    Parameters
    ----------
    spec:
        The :class:`~kbutillib.core.registry.CapabilitySpec` to wrap.

    Returns
    -------
    Callable
        An ``async def`` handler that accepts ``**kwargs`` and returns a result.
    """

    async def handler(**kwargs: Any) -> Any:
        available, reason = spec.availability()
        if not available:
            unavail_msg = f"unavailable: {reason or 'backend not installed'}"
            logger.debug("MCP tool %r called but unavailable: %s", spec.name, reason)
            return {"error": unavail_msg}

        # Validate input via pydantic model if present
        if spec.input_model is not None:
            try:
                validated = spec.input_model(**kwargs)
                call_kwargs = validated.model_dump()
            except Exception as exc:  # noqa: BLE001
                return {"error": f"input validation failed: {exc}"}
        else:
            call_kwargs = kwargs

        try:
            result = spec.fn(**call_kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.exception("MCP tool %r raised: %s", spec.name, exc)
            return {"error": f"tool error: {exc}"}

        # Serialize via output model if available
        if spec.output_model is not None:
            try:
                out = spec.output_model.model_validate({"result": result})
                return out.model_dump()
            except Exception:  # noqa: BLE001
                pass  # fall through to raw result

        return result

    # Give the handler the tool name for introspection
    handler.__name__ = spec.name.replace(".", "_")
    handler.__doc__ = spec.description or spec.effective_summary()
    return handler


# ---------------------------------------------------------------------------
# build_server — lazy mcp import
# ---------------------------------------------------------------------------


def build_server(
    registry: CapabilityRegistry | None = None,
    app: Any = None,
) -> Any:  # -> FastMCP
    """Build and return a FastMCP server with one tool per registered capability.

    Parameters
    ----------
    registry:
        A pre-built :class:`~kbutillib.core.registry.CapabilityRegistry`.
        When ``None``, the global singleton is used.  If the global singleton
        is empty and *app* is provided, ``register_all(app)`` is called.
        If both are ``None``, a default :class:`~kbutillib.KBUtilLib` instance
        is constructed and its capabilities are registered.
    app:
        A ``KBUtilLib`` facade instance used to register capabilities when
        *registry* is ``None`` or empty.  Ignored when *registry* is provided
        and already populated.

    Returns
    -------
    FastMCP
        A configured FastMCP server instance ready to run.

    Raises
    ------
    ImportError
        If the ``mcp`` package is not installed.
    """
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "The 'mcp' package is required to build an MCP server. "
            "Install it with: pip install 'kbutillib[mcp]' or pip install 'mcp>=1.27,<2'"
        ) from exc

    reg = _resolve_registry(registry, app)

    server = FastMCP("KBUtilLib")

    for spec in reg:
        if "mcp" not in spec.transports:
            logger.debug("Skipping %r — not in mcp transports", spec.name)
            continue

        handler = _make_handler(spec)
        tool_spec = capability_to_tool_spec(spec)

        # Register tool with FastMCP
        # FastMCP accepts tool registration via add_tool or the tool() decorator.
        # We use add_tool for programmatic registration.
        try:
            server.add_tool(
                handler,
                name=spec.name,
                description=tool_spec["description"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("build_server: could not register tool %r: %s", spec.name, exc)

    return server


# ---------------------------------------------------------------------------
# Transport runners — lazy mcp import
# ---------------------------------------------------------------------------


def run_stdio(registry: CapabilityRegistry | None = None, app: Any = None) -> None:
    """Run the MCP server over stdio transport (blocking).

    Parameters
    ----------
    registry:
        Optional pre-built registry.  See :func:`build_server`.
    app:
        Optional ``KBUtilLib`` facade.  See :func:`build_server`.

    Raises
    ------
    ImportError
        If the ``mcp`` package is not installed.
    """
    server = build_server(registry=registry, app=app)
    server.run(transport="stdio")


def run_http(
    host: str = "127.0.0.1",
    port: int = 8000,
    registry: CapabilityRegistry | None = None,
    app: Any = None,
) -> None:
    """Run the MCP server over Streamable HTTP transport (blocking).

    Parameters
    ----------
    host:
        Bind host address.  Defaults to ``"127.0.0.1"`` (loopback-only);
        pass ``"0.0.0.0"`` to deliberately widen the bind.
    port:
        Bind port.  Defaults to ``8000``.
    registry:
        Optional pre-built registry.  See :func:`build_server`.
    app:
        Optional ``KBUtilLib`` facade.  See :func:`build_server`.

    Raises
    ------
    ImportError
        If the ``mcp`` package is not installed.
    """
    server = build_server(registry=registry, app=app)
    server.run(transport="streamable-http", host=host, port=port)


# ---------------------------------------------------------------------------
# List helper — pure, no mcp needed
# ---------------------------------------------------------------------------


def _list_tools(registry: CapabilityRegistry) -> None:
    """Print all tool names and their availability to stdout."""
    specs = list(registry)
    if not specs:
        print("No capabilities registered.")
        return

    max_name = max((len(s.name) for s in specs), default=10)
    header = f"{'NAME':<{max_name}}  STATUS    DESCRIPTION"
    print(header)
    print("-" * len(header))
    for spec in sorted(specs, key=lambda s: s.name):
        available, reason = spec.availability()
        status = "available" if available else f"unavailable ({reason or 'unknown'})"
        summary = spec.effective_summary()[:60]
        print(f"{spec.name:<{max_name}}  {status:<35}  {summary}")


# ---------------------------------------------------------------------------
# main() — console script entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:  # noqa: PLR0912
    """Entry point for the ``kbu-mcp`` console script.

    Usage
    -----
    ::

        kbu-mcp --list            # print tool names + availability, no server
        kbu-mcp --stdio           # run stdio transport (default)
        kbu-mcp --http            # run Streamable HTTP transport (loopback)
        kbu-mcp --http --host 0.0.0.0 --port 8000  # deliberately widen the bind

    Parameters
    ----------
    argv:
        Argument list; defaults to ``sys.argv[1:]``.
    """
    parser = argparse.ArgumentParser(
        prog="kbu-mcp",
        description="KBUtilLib MCP server — expose capability registry as MCP tools.",
    )
    transport_group = parser.add_mutually_exclusive_group()
    transport_group.add_argument(
        "--list",
        action="store_true",
        help="Print tool names and availability then exit (no server started).",
    )
    transport_group.add_argument(
        "--stdio",
        action="store_true",
        default=False,
        help="Run stdio transport (default when no transport flag given).",
    )
    transport_group.add_argument(
        "--http",
        action="store_true",
        default=False,
        help="Run Streamable HTTP transport.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host for HTTP transport (default: 127.0.0.1, loopback-only; "
        "pass 0.0.0.0 to deliberately widen the bind).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Bind port for HTTP transport (default: 8000).",
    )
    parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Output tool list as JSON (only valid with --list).",
    )

    args = parser.parse_args(argv)

    # Build the registry
    try:
        from kbutillib import KBUtilLib
        from kbutillib.core.capability import register_all
        from kbutillib.core.registry import get_registry

        reg = get_registry()
        kbu = KBUtilLib()
        register_all(kbu, registry=reg)
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: could not auto-register capabilities: {exc}", file=sys.stderr)
        from kbutillib.core.registry import get_registry

        reg = get_registry()

    if args.list:
        if args.output_json:
            tools = []
            for spec in sorted(reg, key=lambda s: s.name):
                available, reason = spec.availability()
                tool_spec = capability_to_tool_spec(spec)
                tool_spec["available"] = available
                tool_spec["unavailable_reason"] = reason
                tools.append(tool_spec)
            print(json.dumps(tools, indent=2))
        else:
            _list_tools(reg)
        return

    if args.http:
        try:
            run_http(host=args.host, port=args.port, registry=reg)
        except ImportError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        # Default: stdio (also explicit --stdio)
        try:
            run_stdio(registry=reg)
        except ImportError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
