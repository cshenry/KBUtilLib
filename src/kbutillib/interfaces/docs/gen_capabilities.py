"""Capability catalog documentation generator.

Reads the :class:`~kbutillib.core.registry.CapabilityRegistry` and renders
markdown pages suitable for MkDocs.

Public API
----------
``generate_index(registry) -> str``
    Render the top-level ``capabilities/index.md`` catalog table (all domains).

``generate_domain_page(domain, specs) -> str``
    Render a single per-domain page from a list of CapabilitySpec objects.

``generate_all(registry, output_dir=None) -> dict[str, str]``
    Generate all pages (index + one per domain).  If *output_dir* is given,
    write the files; always returns a ``{relative_path: markdown_string}`` dict.

Heavy imports (mkdocs, mkdocstrings) are **never** imported at module level;
this module is safe to import with zero doc-build dependencies installed.

Run standalone::

    python -m kbutillib.interfaces.docs.gen_capabilities
"""

from __future__ import annotations

import textwrap
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kbutillib.core.registry import CapabilityRegistry, CapabilitySpec

__all__ = [
    "generate_index",
    "generate_domain_page",
    "generate_all",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HEADER = """\
<!-- AUTO-GENERATED — do not edit manually.
     Regenerate with: python -m kbutillib.interfaces.docs.gen_capabilities -->
"""

_AVAILABILITY_BADGE = {
    True: "✅ available",
    False: "⚠️ unavailable",
}


def _badge(spec: CapabilitySpec) -> str:
    """Return a short availability badge string for *spec*."""
    available, _ = spec.availability()
    return _AVAILABILITY_BADGE[available]


def _transport_badges(spec: CapabilitySpec) -> str:
    """Return a comma-joined, sorted list of transports."""
    return ", ".join(sorted(spec.transports)) if spec.transports else "—"


def _anchor(name: str) -> str:
    """Convert a dotted capability name to a markdown anchor slug."""
    return name.replace(".", "-").replace("_", "-").lower()


# ---------------------------------------------------------------------------
# Page generators
# ---------------------------------------------------------------------------


def generate_index(registry: CapabilityRegistry) -> str:
    """Render the top-level capability catalog as a markdown string.

    Parameters
    ----------
    registry:
        The registry to read capabilities from.

    Returns
    -------
    str
        Markdown content for ``docs/capabilities/index.md``.
    """
    specs = registry.list()

    # Group by domain
    by_domain: dict[str, list[CapabilitySpec]] = defaultdict(list)
    for spec in sorted(specs, key=lambda s: (s.domain, s.name)):
        by_domain[spec.domain or "(ungrouped)"].append(spec)

    lines: list[str] = [_HEADER, "# Capability Catalog\n"]

    total = len(specs)
    domain_count = len(by_domain)
    lines.append(
        f"**{total}** capabilities across **{domain_count}** domain(s).\n"
    )

    # Domain summary table
    lines.append("## Domains\n")
    lines.append("| Domain | Capabilities | Availability |")
    lines.append("|--------|-------------|-------------|")
    for domain in sorted(by_domain):
        domain_specs = by_domain[domain]
        available_count = sum(1 for s in domain_specs if s.availability()[0])
        total_d = len(domain_specs)
        lines.append(
            f"| [{domain}]({domain}/index.md) | {total_d} "
            f"| {available_count}/{total_d} available |"
        )
    lines.append("")

    # Full flat table
    lines.append("## All Capabilities\n")
    lines.append("| Name | Domain | Summary | Transports | Status |")
    lines.append("|------|--------|---------|-----------|--------|")
    for spec in sorted(specs, key=lambda s: (s.domain, s.name)):
        summary = spec.effective_summary().replace("|", "\\|")
        domain_link = spec.domain or "—"
        name_anchor = f"[`{spec.name}`]({spec.domain or 'ungrouped'}/index.md#{_anchor(spec.name)})"
        lines.append(
            f"| {name_anchor} | {domain_link} | {summary} "
            f"| {_transport_badges(spec)} | {_badge(spec)} |"
        )
    lines.append("")

    return "\n".join(lines)


def generate_domain_page(domain: str, specs: list[CapabilitySpec]) -> str:
    """Render a single per-domain capability page as a markdown string.

    Parameters
    ----------
    domain:
        Domain name (e.g. ``"biochem"``).
    specs:
        List of :class:`~kbutillib.core.registry.CapabilitySpec` objects
        belonging to this domain.

    Returns
    -------
    str
        Markdown content for ``docs/capabilities/<domain>/index.md``.
    """
    sorted_specs = sorted(specs, key=lambda s: s.name)
    available_count = sum(1 for s in sorted_specs if s.availability()[0])
    total = len(sorted_specs)

    lines: list[str] = [
        _HEADER,
        f"# Domain: {domain}\n",
        f"**{total}** capabilities — {available_count}/{total} available at runtime.\n",
        "## Summary\n",
        "| Name | Summary | Transports | Status |",
        "|------|---------|-----------|--------|",
    ]
    for spec in sorted_specs:
        summary = spec.effective_summary().replace("|", "\\|")
        anchor = _anchor(spec.name)
        lines.append(
            f"| [`{spec.name}`](#{anchor}) | {summary} "
            f"| {_transport_badges(spec)} | {_badge(spec)} |"
        )
    lines.append("")

    # Per-capability detail sections
    lines.append("## Capability Reference\n")
    for spec in sorted_specs:
        anchor = _anchor(spec.name)
        available, reason = spec.availability()
        status_line = "✅ available" if available else f"⚠️ unavailable — {reason or 'backend not installed'}"

        lines.append(f"### `{spec.name}` {{#{anchor}}}\n")
        lines.append(f"**Status:** {status_line}  ")
        lines.append(f"**Transports:** {_transport_badges(spec)}  ")
        lines.append(f"**Visibility:** {spec.visibility}  ")
        if spec.tags:
            lines.append(f"**Tags:** {', '.join(spec.tags)}  ")
        lines.append("")

        if spec.description:
            # Dedent and include full description
            desc = textwrap.dedent(spec.description).strip()
            lines.append(desc)
            lines.append("")

        # Input model schema
        if spec.input_model is not None:
            try:
                schema = spec.input_model.model_json_schema()
                lines.append("**Input schema:**\n")
                lines.append("```json")
                import json  # noqa: PLC0415

                lines.append(json.dumps(schema, indent=2))
                lines.append("```\n")
            except Exception:  # noqa: BLE001
                pass

        # Output model schema
        if spec.output_model is not None:
            try:
                schema = spec.output_model.model_json_schema()
                lines.append("**Output schema:**\n")
                lines.append("```json")
                import json  # noqa: PLC0415

                lines.append(json.dumps(schema, indent=2))
                lines.append("```\n")
            except Exception:  # noqa: BLE001
                pass

        lines.append("---\n")

    return "\n".join(lines)


def generate_all(
    registry: CapabilityRegistry,
    output_dir: str | Path | None = None,
) -> dict[str, str]:
    """Generate all capability documentation pages.

    Parameters
    ----------
    registry:
        The registry to read capabilities from.
    output_dir:
        If given, write the generated pages under this directory (relative paths
        like ``capabilities/index.md`` are resolved against *output_dir*).
        When ``None``, no files are written.

    Returns
    -------
    dict[str, str]
        Mapping of relative path → markdown string for every generated page.
    """
    pages: dict[str, str] = {}

    # Group by domain
    by_domain: dict[str, list[CapabilitySpec]] = defaultdict(list)
    for spec in registry.list():
        by_domain[spec.domain or "(ungrouped)"].append(spec)

    # Top-level index
    pages["capabilities/index.md"] = generate_index(registry)

    # Per-domain pages
    for domain, specs in by_domain.items():
        safe_domain = domain.replace(" ", "_").replace("(", "").replace(")", "")
        path = f"capabilities/{safe_domain}/index.md"
        pages[path] = generate_domain_page(domain, specs)

    # Optionally write files
    if output_dir is not None:
        out = Path(output_dir)
        for rel_path, content in pages.items():
            dest = out / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

    return pages


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _main() -> None:
    """Entry point for ``python -m kbutillib.interfaces.docs.gen_capabilities``."""
    import argparse  # noqa: PLC0415

    from kbutillib.core.registry import CapabilityRegistry  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        description="Generate capability catalog markdown pages.",
    )
    parser.add_argument(
        "--output-dir",
        default="docs",
        help="Root directory to write pages into (default: docs/).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print pages to stdout instead of writing files.",
    )
    parser.add_argument(
        "--register",
        action="store_true",
        default=True,
        help="Call register_all() to populate the registry (default: True).",
    )
    args = parser.parse_args()

    registry = CapabilityRegistry()

    if args.register:
        try:
            from kbutillib import KBUtilLib  # noqa: PLC0415
            from kbutillib.core.capability import register_all  # noqa: PLC0415
            from kbutillib.core.registry import get_registry  # noqa: PLC0415

            # Use the global registry populated by register_all
            app = KBUtilLib(config_file=None)
            register_all(app, registry=get_registry())
            registry = get_registry()
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] Could not populate registry via register_all: {exc}")

    if args.dry_run:
        pages = generate_all(registry)
        for path, content in sorted(pages.items()):
            print(f"\n{'='*60}\n# {path}\n{'='*60}")
            print(content)
    else:
        pages = generate_all(registry, output_dir=args.output_dir)
        for path in sorted(pages):
            print(f"  wrote {args.output_dir}/{path}")
        print(f"Generated {len(pages)} page(s).")


if __name__ == "__main__":  # pragma: no cover
    _main()
