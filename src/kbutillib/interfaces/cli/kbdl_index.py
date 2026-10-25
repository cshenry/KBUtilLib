"""``kbu kbdl-index`` -- index swept KBDL job results into the KOROS arc store.

Runs the same index the sweep loop's ``--index`` phase runs, standalone, so the
backlog can be indexed and re-indexed deliberately. ``--dry-run`` writes nothing
to the run DB: it reports the counts and how each would-be record compares with
rows already in the store (REPLACE = same record_id, DUPLICATE = same run_uid
under a different record_id).
"""

from __future__ import annotations

import json
import os
from typing import Optional, Tuple

import click

from ...kbdl_index import sweep as _sweep


def _read_token(path: Optional[str]) -> Optional[str]:
    """Contents of the token file, stripped; never printed or logged."""
    if not path:
        return None
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        return fh.read().strip()


@click.command("kbdl-index")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Write nothing; report counts and replace-vs-duplicate overlap.")
@click.option("--job-type", "job_types", multiple=True,
              help="Restrict to a job type (BuildGenome / GenomeAnnotation); repeatable.")
@click.option("--token-file", "token_file", default=None,
              help="Read the KBase token from this file instead of the environment. "
                   "The contents are never printed.")
@click.option("--fetch-finished-at", "fetch_finished", is_flag=True, default=False,
              help="Call check_job for each indexable job to fill finished_at "
                   "(fail-soft; default off, otherwise submitted_at is used).")
@click.option("--max-finished-calls", "finished_limit", type=int, default=0,
              help="Bound the number of check_job calls (0 = unbounded).")
@click.option("--results-root", default=None, help="Default: ~/.kbdl/job-results.")
@click.option("--ledger", "ledger_path", default=None,
              help="Default: ~/.kbdl/sweep_ledger.jsonl.")
@click.option("--runs-root", default=None,
              help="Explicit KOROS runs root; otherwise the store's pinned chain.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the result as JSON.")
def kbdl_index_cmd(dry_run: bool, job_types: Tuple[str, ...], token_file: Optional[str],
                   fetch_finished: bool, finished_limit: int,
                   results_root: Optional[str], ledger_path: Optional[str],
                   runs_root: Optional[str], as_json: bool) -> None:
    """Index swept KBDL BuildGenome / GenomeAnnotation results into the arc store."""
    from ...domains.external.kbdl_service_utils import KBDLServiceUtils
    from ...kbdl_index.run import (
        created_at_sources, normalize_job_types, overlap_vs_existing,
        population_ids, run_index)
    from ...koros_arc_store import KorosArcStore

    try:
        types = normalize_job_types(job_types)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--job-type")
    results_root = results_root or _sweep.ROOT
    ledger_path = ledger_path or _sweep.LEDGER
    token = _read_token(token_file)
    kbdl = KBDLServiceUtils(token=token) if token else KBDLServiceUtils()
    store = KorosArcStore(runs_root)

    res = run_index(store, kbdl, ledger_path, results_root, str(store.runs_root),
                    dry_run=dry_run, job_types=types, fetch_finished=fetch_finished,
                    finished_limit=finished_limit, log=click.echo)
    out = {"report": res.report.__dict__, "summary": res.report.summary()}
    if dry_run:
        out["dated_from"] = created_at_sources(res.captured)
        out["finished_at_filled"] = res.finished_at_filled
        out["overlap"] = overlap_vs_existing(
            store, res.captured, population_ids(ledger_path, results_root, types))
    if as_json:
        click.echo(json.dumps(out, indent=2, default=str))
    else:
        click.echo(out["summary"])
        if dry_run:
            click.echo(f"dated_from={out['dated_from']} undated={res.report.undated}")
            ov = out["overlap"]
            click.echo("overlap: " + ", ".join(
                f"{k}={v}" for k, v in ov.items() if not k.endswith("examples")))
