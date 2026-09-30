"""``kbu arc`` — KOROS arc maintenance verbs.

Currently one verb, ``backfill``, which recovers modelling work that predates
run stamping and writes :class:`~kbutillib.koros_arc_store.records.AnalysisRecord`
rows for it. See :mod:`kbutillib.models_and_analyses.backfill` for the scan
layout, the inferred-provenance marker, the deterministic synthetic run_uid and
the coverage-report contract.

Backfill NEVER runs automatically — it is only ever this explicit command — and
``--dry-run`` reports what it would write without writing.
"""

from __future__ import annotations

from typing import Optional

import click


@click.group("arc")
def arc_cmd() -> None:
    """KOROS arc maintenance verbs."""


@arc_cmd.command("backfill")
@click.option("--project", "project", default=None, help="Limit to one project.")
@click.option("--arc", "arc", default=None, help="Limit to one arc slug.")
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Report what would be written without writing anything.",
)
@click.option(
    "--runs-root",
    "runs_root",
    default=None,
    help="Explicit KOROS runs root; otherwise the store's pinned chain.",
)
@click.option(
    "--owner",
    "owner",
    default=None,
    help="Owner to scope the KBDL object-store scan by (per-owner filter).",
)
@click.option(
    "--no-store",
    "no_store",
    is_flag=True,
    default=False,
    help="Skip the KBDL object-store scan entirely (files only).",
)
def backfill_cmd(
    project: Optional[str],
    arc: Optional[str],
    dry_run: bool,
    runs_root: Optional[str],
    owner: Optional[str],
    no_store: bool,
) -> None:
    """Backfill AnalysisRecords for modelling work that predates stamping.

    Walks ``<runs_root>/*/arcs/*`` for ``*.model.json``, ``fba/*.json`` and
    ``fva/*.json`` and, where the KBDL object store is reachable, additionally
    lists model/fba/fva objects (preferring file artifacts). Every record it
    writes carries ``payload.provenance = "inferred"`` and a deterministic
    synthetic run_uid, so a second run writes no new row. Records it cannot
    attribute to an arc go to the unattributed index.

    Prints a coverage report — arcs scanned, records written, unattributable
    artifacts and referenced-but-missing analyses — so a partial run is visible
    rather than looking like completeness.
    """
    from ...koros_arc_store import KorosArcStore
    from ...koros_arc_store.store import resolve_runs_root
    from ...models_and_analyses.backfill import backfill

    resolved_runs_root = resolve_runs_root(runs_root)
    store = KorosArcStore(runs_root)

    report = backfill(
        store=store,
        runs_root=resolved_runs_root,
        project=project,
        arc=arc,
        dry_run=dry_run,
        owner=owner,
        scan_store=not no_store,
    )
    click.echo(report.as_text(dry_run=dry_run))
