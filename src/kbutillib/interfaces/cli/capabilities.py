"""``kbu cap`` — capability registry introspection commands.

Sub-commands
------------
list
    List all registered capabilities with optional ``--domain`` / ``--tag``
    filters and an optional ``--json`` output mode.
info
    Show full detail for a single capability (schemas, tags, availability,
    reason) by name.
run
    Validate input via ``input_model`` (if present), call the capability,
    and print the result.  Non-zero exit when the capability is unavailable.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

import click

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_registry() -> Any:
    """Build a populated registry from a fresh KBUtilLib instance.

    Uses the global singleton; callers get the same registry object each time.
    """
    from kbutillib import KBUtilLib
    from kbutillib.core.capability import register_all
    from kbutillib.core.registry import get_registry

    reg = get_registry()
    if len(reg) == 0:
        try:
            kbu = KBUtilLib()
            register_all(kbu, reg)
        except Exception as exc:  # noqa: BLE001
            click.echo(f"Warning: registry population raised: {exc}", err=True)
    return reg


def _availability_str(spec: Any) -> tuple[bool, Optional[str]]:
    """Return ``(available, reason)`` for a spec, never raising."""
    try:
        return spec.availability()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _schema_summary(model: Any) -> str:
    """One-line schema summary for display."""
    if model is None:
        return "—"
    try:
        schema = model.model_json_schema()
        props = schema.get("properties", {})
        fields = ", ".join(props.keys()) if props else "(no fields)"
        return f"{model.__name__}({fields})"
    except Exception:  # noqa: BLE001
        return getattr(model, "__name__", str(model))


# ---------------------------------------------------------------------------
# ``kbu cap`` group
# ---------------------------------------------------------------------------


@click.group(name="cap")
def cap_cmd() -> None:
    """Inspect and invoke capabilities from the registry."""


# ---------------------------------------------------------------------------
# ``kbu cap list``
# ---------------------------------------------------------------------------


@cap_cmd.command(name="list")
@click.option("--domain", default=None, help="Filter by domain (e.g. biochem).")
@click.option("--tag", default=None, help="Filter by tag.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
def list_cmd(domain: Optional[str], tag: Optional[str], as_json: bool) -> None:
    """List registered capabilities.

    Shows name, domain, availability, and summary for each capability.
    Use --domain / --tag to narrow the list.  Use --json for machine-readable
    output.
    """
    reg = _build_registry()
    specs = reg.list(domain=domain, tag=tag)

    if as_json:
        rows = []
        for spec in specs:
            avail, reason = _availability_str(spec)
            rows.append(
                {
                    "name": spec.name,
                    "domain": spec.domain,
                    "available": avail,
                    "reason": reason,
                    "summary": spec.effective_summary(),
                    "tags": list(spec.tags),
                    "visibility": spec.visibility,
                    "transports": sorted(spec.transports),
                }
            )
        click.echo(json.dumps(rows, indent=2))
        return

    if not specs:
        click.echo("No capabilities found.")
        return

    # Human-readable table
    col_name = max(len("CAPABILITY"), max(len(s.name) for s in specs))
    col_dom = max(len("DOMAIN"), max(len(s.domain) for s in specs))
    col_avail = len("AVAILABLE")
    header = (
        f"{'CAPABILITY':<{col_name}}  {'DOMAIN':<{col_dom}}  "
        f"{'AVAILABLE':<{col_avail}}  SUMMARY"
    )
    click.echo(header)
    click.echo("-" * len(header))
    for spec in specs:
        avail, reason = _availability_str(spec)
        avail_str = "yes" if avail else "no"
        summary = spec.effective_summary()
        if not avail and reason:
            summary = f"[unavailable: {reason}]"
        click.echo(
            f"{spec.name:<{col_name}}  {spec.domain:<{col_dom}}  "
            f"{avail_str:<{col_avail}}  {summary}"
        )


# ---------------------------------------------------------------------------
# ``kbu cap info``
# ---------------------------------------------------------------------------


@cap_cmd.command(name="info")
@click.argument("name")
def info_cmd(name: str) -> None:
    """Show full detail for a capability NAME.

    Displays the description, input/output schemas, tags, visibility,
    transports, and current availability (with reason when unavailable).
    """
    from kbutillib.core.errors import CapabilityNotFound

    reg = _build_registry()
    try:
        spec = reg.get(name)
    except CapabilityNotFound:
        click.echo(f"Capability not found: {name!r}", err=True)
        sys.exit(1)

    avail, reason = _availability_str(spec)

    click.echo(f"Name:        {spec.name}")
    click.echo(f"Domain:      {spec.domain}")
    click.echo(f"Visibility:  {spec.visibility}")
    click.echo(f"Transports:  {', '.join(sorted(spec.transports))}")
    click.echo(f"Tags:        {', '.join(spec.tags) if spec.tags else '—'}")
    click.echo(f"Available:   {'yes' if avail else 'no'}")
    if not avail and reason:
        click.echo(f"Reason:      {reason}")
    click.echo(f"Summary:     {spec.effective_summary()}")
    click.echo(f"Input:       {_schema_summary(spec.input_model)}")
    click.echo(f"Output:      {_schema_summary(spec.output_model)}")
    if spec.description:
        click.echo("")
        click.echo("Description:")
        for line in spec.description.splitlines():
            click.echo(f"  {line}")


# ---------------------------------------------------------------------------
# ``kbu cap run``
# ---------------------------------------------------------------------------


@cap_cmd.command(name="run")
@click.argument("name")
@click.option(
    "--json-input",
    default="{}",
    help="JSON object of keyword arguments passed to the capability.",
    metavar="JSON",
)
def run_cmd(name: str, json_input: str) -> None:
    """Call capability NAME with optional JSON input.

    The JSON object from --json-input is unpacked as keyword arguments.
    If the capability declares an ``input_model``, the input is validated
    against it first.  Exits non-zero if the capability is unavailable or
    the call raises.
    """
    from kbutillib.core.errors import CapabilityNotFound

    reg = _build_registry()
    try:
        spec = reg.get(name)
    except CapabilityNotFound:
        click.echo(f"Capability not found: {name!r}", err=True)
        sys.exit(1)

    avail, reason = _availability_str(spec)
    if not avail:
        msg = f"Capability {name!r} is unavailable"
        if reason:
            msg += f": {reason}"
        click.echo(msg, err=True)
        sys.exit(2)

    # Parse JSON input
    try:
        kwargs: dict[str, Any] = json.loads(json_input)
    except json.JSONDecodeError as exc:
        click.echo(f"Invalid JSON input: {exc}", err=True)
        sys.exit(1)

    # Validate via input_model if present
    if spec.input_model is not None:
        try:
            validated = spec.input_model(**kwargs)
            kwargs = validated.model_dump()
        except Exception as exc:  # noqa: BLE001
            click.echo(f"Input validation failed: {exc}", err=True)
            sys.exit(1)

    # Call the capability
    try:
        result = spec.fn(**kwargs)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Capability raised: {exc}", err=True)
        sys.exit(3)

    # Print result
    try:
        click.echo(json.dumps(result, indent=2, default=str))
    except Exception:  # noqa: BLE001
        click.echo(str(result))
