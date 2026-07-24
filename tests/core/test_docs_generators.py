"""WP8 — docs generators tests.

Tests
-----
1.  Import ``kbutillib.interfaces.docs`` cleanly (no heavy deps).
2.  Import ``gen_capabilities`` cleanly.
3.  Import ``mcp_catalog`` cleanly.
4.  ``generate_index`` returns a string containing expected headings.
5.  ``generate_index`` table includes all registered capability names.
6.  ``generate_index`` includes domain names.
7.  ``generate_index`` includes transport columns.
8.  ``generate_domain_page`` returns a string with domain heading.
9.  ``generate_domain_page`` includes per-capability anchors.
10. ``generate_domain_page`` includes availability status badges.
11. ``generate_all`` returns a dict with at least 2 entries (index + domain page).
12. ``generate_all`` dict keys follow expected path pattern.
13. ``tool_spec_to_markdown`` renders a tool name heading.
14. ``tool_spec_to_markdown`` includes parameter table when properties given.
15. ``generate_mcp_catalog`` returns a string with MCP Catalog heading.
16. ``generate_mcp_catalog`` lists all MCP-transport capabilities.
17. ``generate_mcp_catalog_file`` with ``output_path=None`` returns same string
    as ``generate_mcp_catalog`` (no file I/O).
18. Empty registry: ``generate_index`` still returns a valid markdown string.
19. Empty registry: ``generate_mcp_catalog`` returns a no-tools notice.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fixtures — isolated registry with dummy capabilities (no real backends)
# ---------------------------------------------------------------------------


def _make_spec(
    name: str,
    domain: str = "test",
    description: str = "",
    summary: str = "",
    tags: tuple[str, ...] = (),
    visibility: str = "public",
    transports: frozenset[str] | None = None,
    available: bool = True,
) -> Any:
    """Build a minimal CapabilitySpec for tests (no real fn needed)."""
    from kbutillib.core.registry import CapabilitySpec

    fn = MagicMock(return_value=None)
    availability_fn = (lambda av=available: av) if not available else None

    return CapabilitySpec(
        name=name,
        fn=fn,
        description=description or f"Description for {name}.",
        domain=domain,
        summary=summary or f"Summary of {name}",
        tags=tags,
        visibility=visibility,
        transports=transports if transports is not None else frozenset({"lib", "cli", "mcp", "api"}),
        _availability_fn=availability_fn,
    )


@pytest.fixture()
def small_registry():
    """A fresh CapabilityRegistry with 3 dummy capabilities across 2 domains."""
    from kbutillib.core.registry import CapabilityRegistry

    reg = CapabilityRegistry()
    reg.register(_make_spec("alpha.do_thing", domain="alpha", description="Alpha capability.", summary="Do the thing"))
    reg.register(_make_spec("alpha.other_thing", domain="alpha", description="Another alpha cap.", available=True))
    reg.register(
        _make_spec(
            "beta.run_analysis",
            domain="beta",
            description="Beta analysis capability.",
            available=False,
            transports=frozenset({"lib", "mcp"}),
        )
    )
    return reg


@pytest.fixture()
def empty_registry():
    """A fresh, empty CapabilityRegistry."""
    from kbutillib.core.registry import CapabilityRegistry

    return CapabilityRegistry()


# ---------------------------------------------------------------------------
# 1–3: Clean imports
# ---------------------------------------------------------------------------


def test_import_docs_namespace() -> None:
    """Import kbutillib.interfaces.docs without heavy deps."""
    import kbutillib.interfaces.docs  # noqa: F401


def test_import_gen_capabilities() -> None:
    """Import gen_capabilities without heavy deps."""
    import kbutillib.interfaces.docs.gen_capabilities  # noqa: F401


def test_import_mcp_catalog() -> None:
    """Import mcp_catalog without heavy deps."""
    import kbutillib.interfaces.docs.mcp_catalog  # noqa: F401


# ---------------------------------------------------------------------------
# 4–7: generate_index
# ---------------------------------------------------------------------------


def test_generate_index_returns_string(small_registry: Any) -> None:
    """generate_index returns a non-empty string."""
    from kbutillib.interfaces.docs.gen_capabilities import generate_index

    result = generate_index(small_registry)
    assert isinstance(result, str)
    assert len(result) > 0


def test_generate_index_contains_heading(small_registry: Any) -> None:
    """generate_index contains the '# Capability Catalog' heading."""
    from kbutillib.interfaces.docs.gen_capabilities import generate_index

    result = generate_index(small_registry)
    assert "# Capability Catalog" in result


def test_generate_index_contains_all_capability_names(small_registry: Any) -> None:
    """generate_index includes all 3 registered capability names."""
    from kbutillib.interfaces.docs.gen_capabilities import generate_index

    result = generate_index(small_registry)
    assert "alpha.do_thing" in result
    assert "alpha.other_thing" in result
    assert "beta.run_analysis" in result


def test_generate_index_contains_domain_names(small_registry: Any) -> None:
    """generate_index references both domains alpha and beta."""
    from kbutillib.interfaces.docs.gen_capabilities import generate_index

    result = generate_index(small_registry)
    assert "alpha" in result
    assert "beta" in result


def test_generate_index_contains_transport_column(small_registry: Any) -> None:
    """generate_index table includes Transports column."""
    from kbutillib.interfaces.docs.gen_capabilities import generate_index

    result = generate_index(small_registry)
    assert "Transports" in result or "transports" in result.lower()


# ---------------------------------------------------------------------------
# 8–10: generate_domain_page
# ---------------------------------------------------------------------------


def test_generate_domain_page_returns_string(small_registry: Any) -> None:
    """generate_domain_page returns a non-empty string for domain 'alpha'."""
    from kbutillib.interfaces.docs.gen_capabilities import generate_domain_page

    specs = small_registry.list(domain="alpha")
    result = generate_domain_page("alpha", specs)
    assert isinstance(result, str)
    assert len(result) > 0


def test_generate_domain_page_contains_domain_heading(small_registry: Any) -> None:
    """generate_domain_page heading includes the domain name."""
    from kbutillib.interfaces.docs.gen_capabilities import generate_domain_page

    specs = small_registry.list(domain="alpha")
    result = generate_domain_page("alpha", specs)
    assert "alpha" in result
    assert "#" in result  # at least one heading marker


def test_generate_domain_page_contains_capability_anchors(small_registry: Any) -> None:
    """generate_domain_page includes per-capability section headings."""
    from kbutillib.interfaces.docs.gen_capabilities import generate_domain_page

    specs = small_registry.list(domain="alpha")
    result = generate_domain_page("alpha", specs)
    assert "alpha.do_thing" in result
    assert "alpha.other_thing" in result


def test_generate_domain_page_contains_availability_badges(small_registry: Any) -> None:
    """generate_domain_page shows availability status symbols."""
    from kbutillib.interfaces.docs.gen_capabilities import generate_domain_page

    # beta.run_analysis is unavailable
    specs = small_registry.list(domain="beta")
    result = generate_domain_page("beta", specs)
    # Unavailable badge should appear
    assert "⚠️" in result or "unavailable" in result.lower()


# ---------------------------------------------------------------------------
# 11–12: generate_all
# ---------------------------------------------------------------------------


def test_generate_all_returns_dict(small_registry: Any) -> None:
    """generate_all returns a dict with at least one entry per domain + index."""
    from kbutillib.interfaces.docs.gen_capabilities import generate_all

    pages = generate_all(small_registry)
    assert isinstance(pages, dict)
    # index + alpha page + beta page = ≥3
    assert len(pages) >= 3


def test_generate_all_keys_include_index(small_registry: Any) -> None:
    """generate_all dict always has 'capabilities/index.md' key."""
    from kbutillib.interfaces.docs.gen_capabilities import generate_all

    pages = generate_all(small_registry)
    assert "capabilities/index.md" in pages


def test_generate_all_domain_keys_present(small_registry: Any) -> None:
    """generate_all dict includes per-domain keys for alpha and beta."""
    from kbutillib.interfaces.docs.gen_capabilities import generate_all

    pages = generate_all(small_registry)
    assert any("alpha" in k for k in pages)
    assert any("beta" in k for k in pages)


# ---------------------------------------------------------------------------
# 13–14: tool_spec_to_markdown
# ---------------------------------------------------------------------------


def test_tool_spec_to_markdown_heading() -> None:
    """tool_spec_to_markdown includes a heading with the tool name."""
    from kbutillib.interfaces.docs.mcp_catalog import tool_spec_to_markdown

    spec = {
        "name": "alpha.do_thing",
        "description": "Does a thing.",
        "input_schema": {},
    }
    result = tool_spec_to_markdown(spec)
    assert "alpha.do_thing" in result
    assert "#" in result


def test_tool_spec_to_markdown_parameter_table() -> None:
    """tool_spec_to_markdown renders a parameter table when properties present."""
    from kbutillib.interfaces.docs.mcp_catalog import tool_spec_to_markdown

    spec = {
        "name": "beta.run_analysis",
        "description": "Run an analysis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sample_id": {"type": "string", "description": "The sample ID."},
                "threshold": {"type": "number", "description": "Detection threshold."},
            },
            "required": ["sample_id"],
        },
    }
    result = tool_spec_to_markdown(spec)
    assert "sample_id" in result
    assert "threshold" in result
    # required marker for sample_id
    assert "✅" in result


# ---------------------------------------------------------------------------
# 15–17: generate_mcp_catalog / generate_mcp_catalog_file
# ---------------------------------------------------------------------------


def test_generate_mcp_catalog_heading(small_registry: Any) -> None:
    """generate_mcp_catalog contains the '# MCP Tool Catalog' heading."""
    from kbutillib.interfaces.docs.mcp_catalog import generate_mcp_catalog

    result = generate_mcp_catalog(small_registry)
    assert "# MCP Tool Catalog" in result


def test_generate_mcp_catalog_lists_mcp_caps(small_registry: Any) -> None:
    """generate_mcp_catalog lists capabilities with mcp transport."""
    from kbutillib.interfaces.docs.mcp_catalog import generate_mcp_catalog

    result = generate_mcp_catalog(small_registry)
    # alpha caps + beta.run_analysis all have 'mcp' in transports
    assert "alpha.do_thing" in result
    assert "beta.run_analysis" in result


def test_generate_mcp_catalog_file_no_io(small_registry: Any) -> None:
    """generate_mcp_catalog_file with output_path=None returns same as generate_mcp_catalog."""
    from kbutillib.interfaces.docs.mcp_catalog import (
        generate_mcp_catalog,
        generate_mcp_catalog_file,
    )

    expected = generate_mcp_catalog(small_registry)
    result = generate_mcp_catalog_file(small_registry, output_path=None)
    assert result == expected


# ---------------------------------------------------------------------------
# 18–19: edge cases — empty registry
# ---------------------------------------------------------------------------


def test_generate_index_empty_registry(empty_registry: Any) -> None:
    """generate_index on empty registry returns valid markdown with heading."""
    from kbutillib.interfaces.docs.gen_capabilities import generate_index

    result = generate_index(empty_registry)
    assert isinstance(result, str)
    assert "# Capability Catalog" in result
    # Should mention 0 capabilities
    assert "0" in result


def test_generate_mcp_catalog_empty_registry(empty_registry: Any) -> None:
    """generate_mcp_catalog on empty registry returns no-tools notice."""
    from kbutillib.interfaces.docs.mcp_catalog import generate_mcp_catalog

    result = generate_mcp_catalog(empty_registry)
    assert isinstance(result, str)
    assert "# MCP Tool Catalog" in result
    # Should mention 0 tools or say no capabilities
    assert "0" in result or "No capabilities" in result or "no capabilities" in result.lower()
