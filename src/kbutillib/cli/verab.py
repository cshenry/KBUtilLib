"""``kbu verab`` — verAB methoxy-aromatic degradation rule CLI.

Thin Click facade over :class:`~kbutillib.domains.cheminformatics.verab.facade.VerabUtils`.
All heavy imports (RDKit, minedatabase, network_expansion) are deferred behind
``_get_toolkit()`` so ``kbu verab --help`` is always fast.

Subcommands
-----------
discover
    Discover verAB O-demethylation rules via Pickaxe.
enumerate
    Enumerate methoxy-aromatic compounds from the biochem DB (requires RDKit).
screen
    Screen rule operators x methoxy-aromatics and cross-reference with biochem DB.
emit-king
    Write a reproducible KING coscientist input directory.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import click


# ---------------------------------------------------------------------------
# Toolkit accessor — monkeypatched in tests via patch("kbutillib.cli.verab._get_toolkit")
# ---------------------------------------------------------------------------


def _get_toolkit() -> Any:
    """Return a :class:`~kbutillib.KBUtilLib` instance.

    Separated into its own function so tests can monkeypatch it without
    importing the full KBUtilLib constructor.
    """
    from kbutillib import KBUtilLib

    return KBUtilLib()


# ---------------------------------------------------------------------------
# verab group
# ---------------------------------------------------------------------------


@click.group("verab")
def verab_cmd() -> None:
    """verAB methoxy-aromatic O-demethylation tools.

    Commands for discovering, enumerating, screening, and emitting KING
    workflow inputs for the verAB lignin-degradation pathway.
    """


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


@verab_cmd.command("discover")
@click.option("--generations", default=1, show_default=True, help="Pickaxe expansion generations.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON to stdout.")
def discover_cmd(generations: int, as_json: bool) -> None:
    """Discover verAB O-demethylation rules via Pickaxe expansion.

    Exits 0 when at least one operator is found, 1 when none are found,
    and 2 on any runtime error.
    """
    try:
        toolkit = _get_toolkit()
        result = toolkit.verab.discover_rules(generations=generations)
    except Exception as exc:
        if as_json:
            click.echo(json.dumps({"error": str(exc)}))
        else:
            click.echo(f"ERROR: {exc}", err=True)
        sys.exit(2)

    if as_json:
        click.echo(json.dumps(result.to_dict()))
    else:
        operators = result.operators or []
        click.echo(f"Found {len(operators)} verAB operator(s): {', '.join(operators) or 'none'}")

    if not result.operators:
        sys.exit(1)


# ---------------------------------------------------------------------------
# enumerate
# ---------------------------------------------------------------------------


@verab_cmd.command("enumerate")
@click.option("--limit", default=None, type=int, help="Stop after N compounds.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON to stdout.")
def enumerate_cmd(limit: int, as_json: bool) -> None:
    """Enumerate methoxy-aromatic compounds from the biochem DB.

    Requires RDKit. Exits 0 on success.
    """
    try:
        toolkit = _get_toolkit()
        compounds = toolkit.verab.enumerate_methoxy_aromatics(limit=limit)
    except Exception as exc:
        if as_json:
            click.echo(json.dumps({"error": str(exc)}))
        else:
            click.echo(f"ERROR: {exc}", err=True)
        sys.exit(2)

    if as_json:
        click.echo(json.dumps({"n_compounds": len(compounds), "compounds": compounds}))
    else:
        click.echo(f"Found {len(compounds)} methoxy-aromatic compound(s).")


# ---------------------------------------------------------------------------
# screen
# ---------------------------------------------------------------------------


@verab_cmd.command("screen")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON to stdout.")
def screen_cmd(as_json: bool) -> None:
    """Screen verAB rules against methoxy-aromatic compounds.

    Runs the discovered operators against the seed/enumerated compounds and
    cross-references predicted products with the biochem DB. Exits 0 on success.
    """
    try:
        toolkit = _get_toolkit()
        report = toolkit.verab.screen()
    except Exception as exc:
        if as_json:
            click.echo(json.dumps({"error": str(exc)}))
        else:
            click.echo(f"ERROR: {exc}", err=True)
        sys.exit(2)

    if as_json:
        click.echo(json.dumps(report.to_dict()))
    else:
        click.echo(
            f"Screened {report.n_source_compounds} compound(s), "
            f"{len(report.records)} record(s) generated."
        )


# ---------------------------------------------------------------------------
# emit-king
# ---------------------------------------------------------------------------


@verab_cmd.command("emit-king")
@click.option(
    "--outdir",
    default="/tmp/king_verab",
    show_default=True,
    help="Output directory for KING workflow artifacts.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON to stdout.")
def emit_king_cmd(outdir: str, as_json: bool) -> None:
    """Emit a reproducible KING coscientist input directory for verAB.

    Writes seed compounds and operator artifacts to *outdir*. Exits 0 on success.
    """
    try:
        toolkit = _get_toolkit()
        artifacts = toolkit.verab.emit_king_workflow(outdir)
    except Exception as exc:
        if as_json:
            click.echo(json.dumps({"error": str(exc)}))
        else:
            click.echo(f"ERROR: {exc}", err=True)
        sys.exit(2)

    if as_json:
        click.echo(json.dumps(artifacts))
    else:
        files = artifacts.get("files", [])
        click.echo(f"Emitted {len(files)} KING artifact(s) to {outdir}.")


__all__ = ["verab_cmd", "_get_toolkit"]
