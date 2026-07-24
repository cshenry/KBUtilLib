"""WP4 — MCP server adapter tests.

Tests
-----
1. ``capability_to_tool_spec`` maps the 3 biochem caps to tool specs
   (name / description / input_schema) — WITHOUT importing ``mcp``.
2. ``import kbutillib.interfaces.mcp.server`` succeeds even if ``mcp`` is not
   installed (lazy imports).
3. The unavailable-handler path: a fake unavailable capability yields a tool
   whose invocation returns an ``"error"`` dict rather than raising.
4. (Skip if mcp missing) Build the server with an in-memory MCP Client, list
   tools, and confirm the biochem tools appear.
"""

from __future__ import annotations

import asyncio
import importlib.util
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXPECTED_BIOCHEM_NAMES = {
    "biochem.search_compounds",
    "biochem.get_compound_by_id",
    "biochem.get_reaction_by_id",
}

MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fresh_registry():
    """Return a fresh, isolated CapabilityRegistry (not the global one)."""
    from kbutillib.core.registry import CapabilityRegistry

    return CapabilityRegistry()


@pytest.fixture()
def biochem_specs(fresh_registry):
    """Register the 3 biochem caps into a fresh registry and return the specs."""
    from kbutillib import KBUtilLib
    from kbutillib.core.capability import register_all

    kbu = KBUtilLib()
    registered = register_all(kbu, registry=fresh_registry)
    biochem = [s for s in registered if s.domain == "biochem"]
    return biochem, fresh_registry


@pytest.fixture()
def unavailable_spec():
    """Return a fake CapabilitySpec whose availability() is always False."""
    from kbutillib.core.registry import CapabilitySpec

    def _always_unavailable():
        return False, "test backend not installed"

    def _dummy_fn(**kwargs: Any) -> Any:
        raise RuntimeError("should never be called")

    return CapabilitySpec(
        name="test.unavailable_tool",
        fn=_dummy_fn,
        description="A fake unavailable capability for testing.",
        domain="test",
        summary="fake unavailable tool",
        tags=("test",),
        visibility="public",
        _availability_fn=_always_unavailable,
    )


# ---------------------------------------------------------------------------
# 1. capability_to_tool_spec — pure, no mcp needed
# ---------------------------------------------------------------------------


class TestCapabilityToToolSpec:
    """Tests for the pure ``capability_to_tool_spec`` helper."""

    def test_import_does_not_require_mcp(self) -> None:
        """``capability_to_tool_spec`` can be imported without mcp installed."""
        from kbutillib.interfaces.mcp.server import capability_to_tool_spec

        assert callable(capability_to_tool_spec)

    def test_biochem_caps_have_correct_names(self, biochem_specs) -> None:
        """The 3 biochem specs map to tool specs with the right names."""
        from kbutillib.interfaces.mcp.server import capability_to_tool_spec

        specs, _reg = biochem_specs
        produced_names = {capability_to_tool_spec(s)["name"] for s in specs}
        assert produced_names == EXPECTED_BIOCHEM_NAMES

    def test_tool_spec_has_description(self, biochem_specs) -> None:
        """Each tool spec has a non-empty description string."""
        from kbutillib.interfaces.mcp.server import capability_to_tool_spec

        specs, _reg = biochem_specs
        for spec in specs:
            tool = capability_to_tool_spec(spec)
            assert isinstance(tool["description"], str)
            assert len(tool["description"]) > 0, f"{spec.name} has empty description"

    def test_tool_spec_has_input_schema(self, biochem_specs) -> None:
        """Each tool spec has an ``input_schema`` dict (JSON Schema)."""
        from kbutillib.interfaces.mcp.server import capability_to_tool_spec

        specs, _reg = biochem_specs
        for spec in specs:
            tool = capability_to_tool_spec(spec)
            assert isinstance(tool["input_schema"], dict), (
                f"{spec.name}: input_schema is not a dict"
            )

    def test_search_compounds_schema_has_properties(self, biochem_specs) -> None:
        """search_compounds input schema has properties (query_identifiers etc.)."""
        from kbutillib.interfaces.mcp.server import capability_to_tool_spec

        specs, _reg = biochem_specs
        sc = next(s for s in specs if s.name == "biochem.search_compounds")
        tool = capability_to_tool_spec(sc)
        schema = tool["input_schema"]
        assert "properties" in schema, "input_schema missing 'properties'"
        props = schema["properties"]
        assert len(props) > 0, "search_compounds schema has no properties"

    def test_get_compound_by_id_schema_has_compound_id(self, biochem_specs) -> None:
        """get_compound_by_id input schema includes 'compound_id'."""
        from kbutillib.interfaces.mcp.server import capability_to_tool_spec

        specs, _reg = biochem_specs
        gc = next(s for s in specs if s.name == "biochem.get_compound_by_id")
        tool = capability_to_tool_spec(gc)
        schema = tool["input_schema"]
        props = schema.get("properties", {})
        assert "compound_id" in props, f"expected 'compound_id' in schema props, got: {list(props)}"

    def test_get_reaction_by_id_schema_has_reaction_id(self, biochem_specs) -> None:
        """get_reaction_by_id input schema includes 'reaction_id'."""
        from kbutillib.interfaces.mcp.server import capability_to_tool_spec

        specs, _reg = biochem_specs
        gr = next(s for s in specs if s.name == "biochem.get_reaction_by_id")
        tool = capability_to_tool_spec(gr)
        schema = tool["input_schema"]
        props = schema.get("properties", {})
        assert "reaction_id" in props, f"expected 'reaction_id' in schema props, got: {list(props)}"

    def test_spec_without_input_model_returns_empty_schema(self) -> None:
        """A spec with no input_model returns an empty object schema."""
        from kbutillib.core.registry import CapabilitySpec
        from kbutillib.interfaces.mcp.server import capability_to_tool_spec

        spec = CapabilitySpec(
            name="test.no_model",
            fn=lambda: None,
            description="no model test",
            domain="test",
        )
        tool = capability_to_tool_spec(spec)
        assert tool["input_schema"] == {"type": "object", "properties": {}}

    def test_spec_with_broken_schema_returns_fallback(self) -> None:
        """A spec whose model_json_schema() raises returns empty schema (no crash)."""
        from kbutillib.core.registry import CapabilitySpec
        from kbutillib.interfaces.mcp.server import capability_to_tool_spec

        bad_model = MagicMock()
        bad_model.model_json_schema.side_effect = RuntimeError("schema error")

        spec = CapabilitySpec(
            name="test.bad_schema",
            fn=lambda: None,
            description="broken schema test",
            domain="test",
            input_model=bad_model,
        )
        tool = capability_to_tool_spec(spec)
        # Should fall back gracefully
        assert tool["input_schema"] == {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# 2. Lazy import — module import must not require mcp
# ---------------------------------------------------------------------------


class TestLazyImport:
    """Verify that importing the server module never requires mcp."""

    def test_module_import_ok_without_mcp(self) -> None:
        """``import kbutillib.interfaces.mcp.server`` succeeds without mcp."""
        import kbutillib.interfaces.mcp.server as s  # noqa: F401

        assert hasattr(s, "build_server")
        assert hasattr(s, "capability_to_tool_spec")
        assert hasattr(s, "run_stdio")
        assert hasattr(s, "run_http")
        assert hasattr(s, "main")

    def test_package_import_ok_without_mcp(self) -> None:
        """``import kbutillib.interfaces.mcp`` succeeds without mcp."""
        import kbutillib.interfaces.mcp as m  # noqa: F401

        assert hasattr(m, "build_server")
        assert hasattr(m, "capability_to_tool_spec")

    def test_build_server_raises_importerror_without_mcp(self) -> None:
        """``build_server()`` raises ImportError with helpful message when mcp missing."""
        if MCP_AVAILABLE:
            pytest.skip("mcp is installed — skipping missing-mcp path test")

        from kbutillib.interfaces.mcp.server import build_server

        with pytest.raises(ImportError, match="mcp"):
            build_server()

    def test_run_stdio_raises_importerror_without_mcp(self) -> None:
        """``run_stdio()`` raises ImportError when mcp missing."""
        if MCP_AVAILABLE:
            pytest.skip("mcp is installed — skipping missing-mcp path test")

        from kbutillib.interfaces.mcp.server import run_stdio

        with pytest.raises(ImportError, match="mcp"):
            run_stdio()

    def test_run_http_raises_importerror_without_mcp(self) -> None:
        """``run_http()`` raises ImportError when mcp missing."""
        if MCP_AVAILABLE:
            pytest.skip("mcp is installed — skipping missing-mcp path test")

        from kbutillib.interfaces.mcp.server import run_http

        with pytest.raises(ImportError, match="mcp"):
            run_http()


# ---------------------------------------------------------------------------
# 3. Unavailable-handler path
# ---------------------------------------------------------------------------


class TestUnavailableHandler:
    """Verify that unavailable caps return an error dict, not an exception."""

    def test_unavailable_spec_availability_returns_false(self, unavailable_spec) -> None:
        """The fake unavailable spec reports (False, reason)."""
        available, reason = unavailable_spec.availability()
        assert available is False
        assert reason is not None

    def test_handler_returns_error_dict_for_unavailable(self, unavailable_spec) -> None:
        """The handler for an unavailable cap returns {error: 'unavailable: ...'} not a raise."""
        from kbutillib.interfaces.mcp.server import _make_handler

        handler = _make_handler(unavailable_spec)
        result = asyncio.get_event_loop().run_until_complete(handler())
        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert "error" in result, f"Expected 'error' key, got keys: {list(result.keys())}"
        assert "unavailable" in result["error"], f"Expected 'unavailable' in error: {result['error']}"

    def test_handler_error_message_contains_reason(self, unavailable_spec) -> None:
        """The error message includes the unavailability reason string."""
        from kbutillib.interfaces.mcp.server import _make_handler

        handler = _make_handler(unavailable_spec)
        result = asyncio.get_event_loop().run_until_complete(handler())
        assert "test backend not installed" in result["error"]

    def test_unavailable_handler_never_calls_fn(self, unavailable_spec) -> None:
        """The underlying fn is never invoked for an unavailable cap."""
        from kbutillib.interfaces.mcp.server import _make_handler

        call_count = [0]
        original_fn = unavailable_spec.fn

        def counting_fn(**kwargs: Any) -> Any:
            call_count[0] += 1
            return original_fn(**kwargs)

        # Patch fn on the spec by rebuilding with counting_fn
        from kbutillib.core.registry import CapabilitySpec

        patched_spec = CapabilitySpec(
            name=unavailable_spec.name,
            fn=counting_fn,
            description=unavailable_spec.description,
            domain=unavailable_spec.domain,
            summary=unavailable_spec.summary,
            tags=unavailable_spec.tags,
            visibility=unavailable_spec.visibility,
            _availability_fn=unavailable_spec._availability_fn,
        )

        handler = _make_handler(patched_spec)
        asyncio.get_event_loop().run_until_complete(handler())
        assert call_count[0] == 0, "fn should NOT be called when cap is unavailable"

    def test_resolve_registry_with_fresh_registry(self, fresh_registry) -> None:
        """_resolve_registry returns the provided registry unchanged."""
        from kbutillib.interfaces.mcp.server import _resolve_registry

        result = _resolve_registry(fresh_registry, app=None)
        assert result is fresh_registry

    def test_list_tools_does_not_raise(self, fresh_registry, unavailable_spec) -> None:
        """_list_tools prints without raising even with unavailable caps."""
        import io
        from contextlib import redirect_stdout

        from kbutillib.interfaces.mcp.server import _list_tools

        fresh_registry.register(unavailable_spec)
        buf = io.StringIO()
        with redirect_stdout(buf):
            _list_tools(fresh_registry)
        output = buf.getvalue()
        assert "test.unavailable_tool" in output
        assert "unavailable" in output

    def test_list_tools_empty_registry(self) -> None:
        """_list_tools handles an empty registry without error."""
        import io
        from contextlib import redirect_stdout

        from kbutillib.core.registry import CapabilityRegistry
        from kbutillib.interfaces.mcp.server import _list_tools

        empty_reg = CapabilityRegistry()
        buf = io.StringIO()
        with redirect_stdout(buf):
            _list_tools(empty_reg)
        assert "No capabilities registered" in buf.getvalue()


# ---------------------------------------------------------------------------
# 4. Integration test — requires mcp installed
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not MCP_AVAILABLE, reason="mcp package not installed")
class TestMCPIntegration:
    """Live integration tests using FastMCP in-memory client (requires mcp)."""

    def test_build_server_returns_fastmcp(self, fresh_registry, biochem_specs) -> None:
        """build_server returns a FastMCP instance."""
        from mcp.server.fastmcp import FastMCP  # type: ignore[import]

        from kbutillib.interfaces.mcp.server import build_server

        _specs, reg = biochem_specs
        server = build_server(registry=reg)
        assert isinstance(server, FastMCP)

    def test_biochem_tools_appear_in_server(self, fresh_registry, biochem_specs) -> None:
        """Biochem tool names appear in the server's tool list."""
        from kbutillib.interfaces.mcp.server import build_server

        _specs, reg = biochem_specs
        server = build_server(registry=reg)

        # FastMCP exposes registered tools via its internal _tool_manager or similar
        # Use the in-memory Client if available, else fall back to introspection
        try:
            from mcp import ClientSession  # type: ignore[import]
            from mcp.client.memory import InMemoryTransport  # type: ignore[import]

            async def _list() -> set[str]:
                client_transport, server_transport = InMemoryTransport.create_pair()
                async with server.run_context(server_transport):
                    async with ClientSession(client_transport) as session:
                        await session.initialize()
                        tools_result = await session.list_tools()
                        return {t.name for t in tools_result.tools}

            tool_names = asyncio.get_event_loop().run_until_complete(_list())
        except (ImportError, AttributeError):
            # Fallback: inspect server internals
            tool_names = _inspect_server_tools(server)

        for expected in EXPECTED_BIOCHEM_NAMES:
            assert expected in tool_names, (
                f"Expected tool {expected!r} not found in server tools: {tool_names}"
            )

    def test_unavailable_tool_listed_in_server(self, unavailable_spec) -> None:
        """An unavailable cap still appears as a tool in the server (listed, not crashed)."""
        from kbutillib.core.registry import CapabilityRegistry
        from kbutillib.interfaces.mcp.server import build_server

        reg = CapabilityRegistry()
        reg.register(unavailable_spec)
        server = build_server(registry=reg)
        tool_names = _inspect_server_tools(server)
        assert "test.unavailable_tool" in tool_names


def _inspect_server_tools(server: Any) -> set[str]:
    """Extract registered tool names from a FastMCP server via introspection."""
    # FastMCP keeps tools in various internal structures depending on version
    for attr in ("_tool_manager", "_tools", "tools", "_registry"):
        obj = getattr(server, attr, None)
        if obj is None:
            continue
        if isinstance(obj, dict):
            return set(obj.keys())
        if hasattr(obj, "_tools"):
            inner = getattr(obj, "_tools", {})
            if isinstance(inner, dict):
                return set(inner.keys())
        if hasattr(obj, "list_tools"):
            result = obj.list_tools()
            if hasattr(result, "__iter__"):
                return {getattr(t, "name", str(t)) for t in result}
    # Last resort: look at the server object for any tool-list attr
    return set()


# ---------------------------------------------------------------------------
# 5. main() CLI — smoke test (no server started)
# ---------------------------------------------------------------------------


class TestMain:
    """Smoke tests for the ``main()`` entry point (--list mode, no server)."""

    def test_main_list_runs_without_mcp(self, capsys) -> None:
        """``main(['--list'])`` prints a tool table without starting a server."""
        from kbutillib.interfaces.mcp.server import main

        # Should not raise even if mcp is absent
        try:
            main(["--list"])
        except SystemExit as exc:
            # A non-zero exit is a failure; 0 is acceptable
            assert exc.code in (None, 0), f"main --list exited with code {exc.code}"

        captured = capsys.readouterr()
        # Either tools were listed or "No capabilities registered" appeared
        assert captured.out, "main --list produced no output"

    def test_main_list_json_runs_without_mcp(self, capsys) -> None:
        """``main(['--list', '--json'])`` emits valid JSON without starting a server."""
        from kbutillib.interfaces.mcp.server import main

        try:
            main(["--list", "--json"])
        except SystemExit as exc:
            assert exc.code in (None, 0)

        captured = capsys.readouterr()
        if captured.out.strip():
            parsed = __import__("json").loads(captured.out)
            assert isinstance(parsed, list)
