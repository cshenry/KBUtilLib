"""``kbu verab`` — verAB methoxy-aromatic Pickaxe rule-discovery CLI.

Thin facade over :class:`kbutillib.toolkit.KBUtilLib`'s ``verab`` property
(:class:`~kbutillib.verab_utils.VerabUtils`).  Every subcommand supports
``--json`` for machine-readable output; the human-readable default is a
compact summary.

Exit codes follow the CRAFT CLI convention used by ``kbu king``:
  0 — success / green
  1 — partial / amber (e.g. RDKit absent, no operators found)
  2 — red (unhandled exception / hard failure)

Neither this module nor any of its imports bring in RDKit or minedatabase at
module load time.  All heavy optional-dep work is deferred to the sub-package
methods that own it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import click


# ---------------------------------------------------------------------------
# Toolkit singleton (constructed once per CLI invocation)
# ---------------------------------------------------------------------------


def _get_toolkit():
    """Return a lazily-constructed :class:`~kbutillib.toolkit.KBUtilLib`."""
    from kbutillib.toolkit import KBUtilLib

    return KBUtilLib()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _emit_error(as_json: bool, msg: str) -> None:
    """Print an error message; caller must exit afterwards."""
    if as_json:
        click.echo(json.dumps({"error": msg}))
    else:
        click.echo(f"ERROR: {msg}", err=True)


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------


@click.group("verab")
def verab_cmd() -> None:
    """verAB methoxy-aromatic Pickaxe rule-discovery and genome-screening.

    Wraps the five-step pipeline described in the verAB pickaxe design:
    seed discovery → rule identification → methoxy-aromatic enumeration →
    product screening → KING coscientist artifact emission.

    All subcommands accept ``--json`` for machine-readable output.
    """


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


@verab_cmd.command("discover")
@click.option(
    "--generations",
    default=1,
    show_default=True,
    type=int,
    help="Number of Pickaxe expansion generations.",
)
@click.option(
    "--rule-set",
    "rule_set",
    default="metacyc_generalized",
    show_default=True,
    metavar="RULE_SET",
    help="Pickaxe rule-set identifier (e.g. 'metacyc_generalized').",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def discover_cmd(generations: int, rule_set: str, as_json: bool) -> None:
    """Expand the 5 verAB seed compounds and identify O-demethylation operators.

    Requires a configured network_expansion backend (Pickaxe / minedatabase)
    and — for RDKit-confirmed matches — RDKit.  Degrades to text-only matching
    when RDKit is absent.

    Emits the discovered rule operators on stdout.  Exit 0 if ≥1 operator was
    found; exit 1 if the expansion returned no verAB matches.
    """
    try:
        kbu = _get_toolkit()
        discovery = kbu.verab.discover_rules(
            generations=generations,
            rule_set=rule_set,
        )
    except Exception as exc:
        _emit_error(as_json, f"discover failed: {exc}")
        sys.exit(2)

    result = discovery.to_dict()

    if as_json:
        click.echo(json.dumps(result))
    else:
        n_ops = len(result.get("operators", []))
        n_matches = len(result.get("matches", []))
        click.echo(
            f"kbu verab discover: rule_set={rule_set} generations={generations}"
        )
        click.echo(f"  operators found : {n_ops}")
        click.echo(f"  rule matches    : {n_matches}")
        for op in result.get("operators", []):
            click.echo(f"    {op}")
        for w in result.get("warnings", []):
            click.echo(f"  [WARN] {w}")

    sys.exit(0 if result.get("operators") else 1)


# ---------------------------------------------------------------------------
# enumerate
# ---------------------------------------------------------------------------


@verab_cmd.command("enumerate")
@click.option(
    "--limit",
    default=None,
    type=int,
    metavar="N",
    help="Stop after N hits (default: full biochem DB scan).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def enumerate_cmd(limit: Optional[int], as_json: bool) -> None:
    """Scan the ModelSEED biochemistry DB for methoxy-aromatic compounds.

    Requires RDKit and a configured biochem dependency.  Raises a clear error
    message if either is unavailable.
    """
    try:
        kbu = _get_toolkit()
        compounds = kbu.verab.enumerate_methoxy_aromatics(limit=limit)
    except Exception as exc:
        _emit_error(as_json, f"enumerate failed: {exc}")
        sys.exit(2)

    result: dict = {"n_compounds": len(compounds), "compounds": compounds}

    if as_json:
        click.echo(json.dumps(result))
    else:
        click.echo(
            f"kbu verab enumerate: found {result['n_compounds']} methoxy-aromatic compounds"
            + (f" (limit={limit})" if limit else "")
        )
        for c in result["compounds"][:20]:
            click.echo(f"  {c.get('id', '?')}\t{c.get('name', '?')}")
        if result["n_compounds"] > 20:
            click.echo(f"  ... +{result['n_compounds'] - 20} more (use --json)")


# ---------------------------------------------------------------------------
# screen
# ---------------------------------------------------------------------------


@verab_cmd.command("screen")
@click.option(
    "--operators",
    "operators_str",
    default=None,
    metavar="OP,OP,...",
    help="Comma-separated rule operator ids from 'discover' (default: use seeds only).",
)
@click.option(
    "--generations",
    default=1,
    show_default=True,
    type=int,
    help="Number of expansion generations for screening.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def screen_cmd(operators_str: Optional[str], generations: int, as_json: bool) -> None:
    """Screen methoxy-aromatic products through the four Phase-2 questions.

    Checks each predicted product for: (a) reaction in DB, (b) product in DB,
    (c) downstream pathway, (d) membership in metabolic models.  Optionally
    cross-references genomes for degradation potential.
    """
    operators = (
        [o.strip() for o in operators_str.split(",") if o.strip()]
        if operators_str
        else None
    )
    try:
        kbu = _get_toolkit()
        report = kbu.verab.screen(
            rule_operators=operators,
            generations=generations,
        )
    except Exception as exc:
        _emit_error(as_json, f"screen failed: {exc}")
        sys.exit(2)

    result = report.to_dict()

    if as_json:
        click.echo(json.dumps(result))
    else:
        n_recs = len(result.get("records", []))
        click.echo(
            f"kbu verab screen: {result.get('n_source_compounds', 0)} source compounds, "
            f"{n_recs} screening records"
        )
        for rec in result.get("records", [])[:10]:
            click.echo(
                f"  {rec.get('source_msid', '?')} → "
                f"{str(rec.get('product_smiles', '?'))[:30]} "
                f"in_db={rec.get('product_in_db')} rxn_in_db={rec.get('reaction_in_db')}"
            )
        if n_recs > 10:
            click.echo(f"  ... +{n_recs - 10} more (use --json)")
        for w in result.get("warnings", []):
            click.echo(f"  [WARN] {w}")


# ---------------------------------------------------------------------------
# emit-king
# ---------------------------------------------------------------------------


@verab_cmd.command("emit-king")
@click.option(
    "--outdir",
    default="king_verab",
    show_default=True,
    metavar="PATH",
    help="Directory to write KING workflow artifacts (created if absent).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def emit_king_cmd(outdir: str, as_json: bool) -> None:
    """Write a reproducible KING coscientist workflow directory.

    Emits: seeds.tsv, seeds.csv, discovered_rules.tsv, target_transformation.txt,
    prompt.md, and manifest.json.  No prior 'discover' run is required; pass
    ``--outdir`` to choose where the artifacts land.
    """
    try:
        kbu = _get_toolkit()
        artifacts: dict = kbu.verab.emit_king_workflow(Path(outdir))
    except Exception as exc:
        _emit_error(as_json, f"emit-king failed: {exc}")
        sys.exit(2)

    if as_json:
        click.echo(json.dumps(artifacts))
    else:
        n_files = len(artifacts.get("files", []))
        click.echo(
            f"kbu verab emit-king: outdir={artifacts.get('outdir', outdir)} "
            f"files={n_files} "
            f"operators={artifacts.get('n_operators', 0)} "
            f"seeds={artifacts.get('n_seeds', 0)}"
        )
        for f in artifacts.get("files", []):
            click.echo(f"  {f}")
