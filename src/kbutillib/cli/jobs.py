"""``kbu jobs`` — inspect and manage tracked KBase EE2 jobs."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import click

from ..kb_job_utils.pipeline import PipelineState, PipelineStatus
from ..kb_job_utils.state import JobRecord, JobState
from ..kb_job_utils.store import JobStore


# ── formatting helpers ──────────────────────────────────────────────────────

_IS_TTY = sys.stdout.isatty()

_STATUS_COLORS = {
    "created": "\033[36m",     # cyan
    "estimating": "\033[36m",  # cyan
    "queued": "\033[33m",      # yellow
    "running": "\033[34m",     # blue
    "completed": "\033[32m",   # green
    "error": "\033[31m",       # red
    "terminated": "\033[35m",  # magenta
}
_RESET = "\033[0m"


def _colorize(text: str, status: str) -> str:
    """Apply ANSI color to *text* based on *status*, only if stdout is a tty."""
    if not _IS_TTY:
        return text
    color = _STATUS_COLORS.get(status, "")
    if not color:
        return text
    return f"{color}{text}{_RESET}"


def _fmt_dt(dt: datetime) -> str:
    """Format a datetime as a compact UTC ISO-8601 string."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _print_record_detail(rec: JobRecord) -> None:
    """Print a single job record in a human-readable detail format."""
    click.echo(f"Job ID:     {rec.job_id}")
    click.echo(f"Method:     {rec.method}")
    click.echo(f"State:      {_colorize(rec.state.value, rec.state.value)}")
    click.echo(f"Created:    {_fmt_dt(rec.created_at)}")
    click.echo(f"Updated:    {_fmt_dt(rec.updated_at)}")
    if rec.workspace_id is not None:
        click.echo(f"Workspace:  {rec.workspace_id}")
    if rec.narrative_id is not None:
        click.echo(f"Narrative:  {rec.narrative_id}")
    if rec.error_message:
        click.echo(f"Error:      {rec.error_message}")
    if rec.meta:
        click.echo(f"Meta:       {rec.meta}")


def _print_table(records: List[JobRecord]) -> None:
    """Print a fixed-width table of job records."""
    if not records:
        click.echo("No jobs found.")
        return
    # Column widths
    id_w = max(12, max(len(r.job_id) for r in records))
    meth_w = max(8, min(40, max(len(r.method) for r in records)))
    hdr = (
        f"{'JOB_ID':<{id_w}}  {'STATE':<12}  {'METHOD':<{meth_w}}  "
        f"{'UPDATED':<20}"
    )
    click.echo(hdr)
    click.echo("-" * len(hdr))
    for rec in records:
        state_str = _colorize(f"{rec.state.value:<12}", rec.state.value)
        method_str = rec.method[:meth_w]
        click.echo(
            f"{rec.job_id:<{id_w}}  {state_str}  {method_str:<{meth_w}}  "
            f"{_fmt_dt(rec.updated_at):<20}"
        )


def _get_store(ctx: click.Context) -> JobStore:
    """Get the JobStore from the Click context."""
    return ctx.obj["store"]


def _get_kbu(ctx: click.Context):
    """Lazily construct a KBJobUtils from the Click context.

    Only needed for commands that hit EE2 (refresh, cancel, logs).
    """
    from ..kb_job_utils.utils import KBJobUtils
    from ..shared_env_utils import SharedEnvUtils

    obj = ctx.obj
    if "kbu" not in obj:
        env = SharedEnvUtils()
        obj["kbu"] = KBJobUtils(
            env=env,
            kb_version=obj["kb_version"],
            db_path=Path(obj["store_path"]) if obj["store_path"] else None,
        )
    return obj["kbu"]


# ── Click group ─────────────────────────────────────────────────────────────

@click.group(name="jobs")
@click.option(
    "--store-path",
    default=None,
    type=click.Path(),
    help="Override path to kbjobs.db.",
)
@click.option(
    "--kb-version",
    default="prod",
    type=click.Choice(["prod", "appdev", "ci"]),
    help="KBase environment (default: prod).",
)
@click.pass_context
def jobs_cmd(ctx: click.Context, store_path: Optional[str], kb_version: str) -> None:
    """Inspect and manage tracked KBase EE2 jobs."""
    ctx.ensure_object(dict)
    ctx.obj["kb_version"] = kb_version
    ctx.obj["store_path"] = store_path
    db_path = Path(store_path) if store_path else None
    ctx.obj["store"] = JobStore(db_path=db_path)


# ── status ──────────────────────────────────────────────────────────────────

@jobs_cmd.command()
@click.argument("job_id")
@click.pass_context
def status(ctx: click.Context, job_id: str) -> None:
    """Show detailed status for a single job."""
    store = _get_store(ctx)
    rec = store.get(job_id)
    if rec is None:
        click.echo(f"Job {job_id} not found in local store.", err=True)
        ctx.exit(1)
        return
    _print_record_detail(rec)


# ── list ────────────────────────────────────────────────────────────────────

@jobs_cmd.command(name="list")
@click.option("--status", "filter_status", default=None, help="Filter by state.")
@click.option("--active", is_flag=True, default=False, help="Show only non-terminal jobs.")
@click.option("--limit", default=50, type=int, help="Max rows to show (default 50).")
@click.pass_context
def list_cmd(
    ctx: click.Context,
    filter_status: Optional[str],
    active: bool,
    limit: int,
) -> None:
    """List tracked jobs in a table."""
    store = _get_store(ctx)
    if active:
        records = store.list_active()
    elif filter_status:
        try:
            state = JobState(filter_status)
        except ValueError:
            click.echo(f"Unknown state: {filter_status}", err=True)
            ctx.exit(1)
            return
        records = store.list_by_state(state)
    else:
        records = store.list_all()
    _print_table(records[:limit])


# ── summary ─────────────────────────────────────────────────────────────────

@jobs_cmd.command()
@click.pass_context
def summary(ctx: click.Context) -> None:
    """Show per-status job counts."""
    store = _get_store(ctx)
    all_jobs = store.list_all()
    counts: dict[str, int] = {}
    for rec in all_jobs:
        counts[rec.state.value] = counts.get(rec.state.value, 0) + 1
    if not counts:
        click.echo("No jobs in store.")
        return
    total = sum(counts.values())
    for state_val in sorted(counts):
        label = _colorize(state_val, state_val)
        click.echo(f"  {label}: {counts[state_val]}")
    click.echo(f"  total: {total}")


# ── refresh ─────────────────────────────────────────────────────────────────

@jobs_cmd.command()
@click.argument("job_ids", nargs=-1)
@click.option("--all", "refresh_all_flag", is_flag=True, default=False,
              help="Refresh all jobs, not just active ones.")
@click.option("--active", "refresh_active_flag", is_flag=True, default=False,
              help="Refresh active (non-terminal) jobs (default).")
@click.pass_context
def refresh(
    ctx: click.Context,
    job_ids: tuple,
    refresh_all_flag: bool,
    refresh_active_flag: bool,
) -> None:
    """Force a refresh of job states from EE2.

    By default refreshes active jobs.  Pass specific JOB_IDs to refresh
    those, or --all to refresh everything.
    """
    kbu = _get_kbu(ctx)
    if job_ids:
        results = kbu.check_jobs(list(job_ids))
        click.echo(f"Refreshed {len(results)} job(s).")
        for rec in results.values():
            click.echo(f"  {rec.job_id}: {_colorize(rec.state.value, rec.state.value)}")
    elif refresh_all_flag:
        results = kbu.refresh_all()
        click.echo(f"Refreshed {len(results)} job(s) (all).")
    else:
        results = kbu.refresh_active()
        click.echo(f"Refreshed {len(results)} active job(s).")


# ── logs ────────────────────────────────────────────────────────────────────

@jobs_cmd.command()
@click.argument("job_id")
@click.option("--skip", default=0, type=int, help="Skip first N log lines.")
@click.option("--follow", is_flag=True, default=False,
              help="Poll for new lines every 5s until job is terminal.")
@click.pass_context
def logs(ctx: click.Context, job_id: str, skip: int, follow: bool) -> None:
    """Stream log lines for a job."""
    kbu = _get_kbu(ctx)
    next_skip = skip
    while True:
        result = kbu.get_job_logs(job_id, skip_lines=next_skip)
        lines = result.get("lines", [])
        for entry in lines:
            line = entry.get("line", "")
            click.echo(line)
        last_line = result.get("last_line_number", next_skip)
        next_skip = last_line

        if not follow:
            break

        # Check if job is terminal
        rec = kbu.check_job(job_id)
        if rec.state.is_terminal:
            # One final fetch to get remaining lines
            result = kbu.get_job_logs(job_id, skip_lines=next_skip)
            for entry in result.get("lines", []):
                click.echo(entry.get("line", ""))
            break
        time.sleep(5)


# ── cancel ──────────────────────────────────────────────────────────────────

@jobs_cmd.command()
@click.argument("job_id")
@click.option("--force", is_flag=True, default=False, help="Skip confirmation.")
@click.pass_context
def cancel(ctx: click.Context, job_id: str, force: bool) -> None:
    """Cancel a running EE2 job."""
    if not force:
        if not click.confirm(f"Cancel job {job_id}?"):
            click.echo("Aborted.")
            return
    kbu = _get_kbu(ctx)
    rec = kbu.cancel_job(job_id)
    click.echo(f"Job {rec.job_id}: {_colorize(rec.state.value, rec.state.value)}")


# ── forget ──────────────────────────────────────────────────────────────────

@jobs_cmd.command()
@click.argument("job_ids", nargs=-1, required=True)
@click.option("--force", is_flag=True, default=False, help="Skip confirmation.")
@click.pass_context
def forget(ctx: click.Context, job_ids: tuple, force: bool) -> None:
    """Delete job records from local store (does NOT cancel on server)."""
    if not force:
        if not click.confirm(f"Forget {len(job_ids)} job(s) from local store?"):
            click.echo("Aborted.")
            return
    store = _get_store(ctx)
    removed = 0
    for jid in job_ids:
        if store.delete(jid):
            removed += 1
    click.echo(f"Removed {removed} record(s).")


# ── cleanup ─────────────────────────────────────────────────────────────────

@jobs_cmd.command()
@click.option("--older-than-days", default=30, type=int,
              help="Remove records older than N days (default 30).")
@click.option("--all-statuses", is_flag=True, default=False,
              help="Remove all old records, not just terminal ones.")
@click.option("--force", is_flag=True, default=False, help="Skip confirmation.")
@click.pass_context
def cleanup(
    ctx: click.Context,
    older_than_days: int,
    all_statuses: bool,
    force: bool,
) -> None:
    """Remove old job records from local store."""
    if not force:
        scope = "all statuses" if all_statuses else "terminal only"
        if not click.confirm(
            f"Cleanup records older than {older_than_days} days ({scope})?"
        ):
            click.echo("Aborted.")
            return
    kbu = _get_kbu(ctx)
    deleted = kbu.cleanup(
        older_than_days=older_than_days,
        terminal_only=not all_statuses,
    )
    click.echo(f"Cleaned up {deleted} record(s).")


# ── chain (pipeline) subcommands ──────────────────────────────────────────

_PIPELINE_STATUS_COLORS = {
    "pending": "\033[36m",
    "running": "\033[34m",
    "completed": "\033[32m",
    "error": "\033[31m",
    "terminated": "\033[35m",
}


def _colorize_pipeline(text: str, status: str) -> str:
    """Apply ANSI color to *text* based on pipeline *status*."""
    if not _IS_TTY:
        return text
    color = _PIPELINE_STATUS_COLORS.get(status, "")
    if not color:
        return text
    return f"{color}{text}{_RESET}"


@jobs_cmd.group(name="chain")
def chain_cmd():
    """Manage pipelines (linear job chains)."""


@chain_cmd.command(name="submit")
@click.argument("file", type=click.Path(exists=False), default="-")
@click.pass_context
def chain_submit(ctx: click.Context, file: str) -> None:
    """Submit a pipeline from a JSON file (or stdin with '-').

    The JSON can be either a bare list of EE2 param dicts, or an object
    with keys: steps (required), name, project, tags.
    """
    if file == "-":
        raw = click.get_text_stream("stdin").read()
    else:
        raw = Path(file).read_text()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        click.echo(f"Invalid JSON: {exc}", err=True)
        ctx.exit(1)
        return

    if isinstance(data, list):
        steps = data
        name = None
        project = None
        tags = None
    elif isinstance(data, dict):
        steps = data.get("steps", [])
        name = data.get("name")
        project = data.get("project")
        tags = data.get("tags")
    else:
        click.echo("JSON must be a list of param dicts or an object with 'steps'.", err=True)
        ctx.exit(1)
        return

    if not steps:
        click.echo("No steps provided.", err=True)
        ctx.exit(1)
        return

    kbu = _get_kbu(ctx)
    pipeline = kbu.submit_chain(steps, name=name, project=project, tags=tags)

    # Find the first step's job
    first_job = kbu._find_pipeline_step_job(pipeline.pipeline_id, 0)
    first_job_id = first_job.job_id if first_job else "unknown"

    click.echo(f"Pipeline: {pipeline.pipeline_id}")
    click.echo(f"Steps:    {pipeline.total_steps}")
    click.echo(f"Status:   {_colorize_pipeline(pipeline.status.value, pipeline.status.value)}")
    click.echo(f"Step 0 job: {first_job_id}")


@chain_cmd.command(name="list")
@click.option("--status", "filter_status", default=None, help="Filter by pipeline status.")
@click.option("--project", default=None, help="Filter by project.")
@click.option("--since", default=None, help="Show pipelines created after ISO datetime.")
@click.option("--limit", default=50, type=int, help="Max rows (default 50).")
@click.option("--active", is_flag=True, default=False, help="Show only non-terminal pipelines.")
@click.pass_context
def chain_list(
    ctx: click.Context,
    filter_status: Optional[str],
    project: Optional[str],
    since: Optional[str],
    limit: int,
    active: bool,
) -> None:
    """List pipelines in a table."""
    store = _get_store(ctx)
    ps = None
    if active:
        # List non-terminal: PENDING + RUNNING
        pending = store.list_pipelines(status=PipelineStatus.PENDING, limit=limit)
        running = store.list_pipelines(status=PipelineStatus.RUNNING, limit=limit)
        pipelines = sorted(pending + running, key=lambda p: p.created_at, reverse=True)[:limit]
    elif filter_status:
        try:
            ps = PipelineStatus(filter_status)
        except ValueError:
            click.echo(f"Unknown pipeline status: {filter_status}", err=True)
            ctx.exit(1)
            return
        pipelines = store.list_pipelines(status=ps, limit=limit)
    else:
        since_dt = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since)
                if since_dt.tzinfo is None:
                    since_dt = since_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                click.echo(f"Invalid datetime: {since}", err=True)
                ctx.exit(1)
                return
        pipelines = store.list_pipelines(
            project=project, since=since_dt, limit=limit,
        )

    if not pipelines:
        click.echo("No pipelines found.")
        return

    # Table header
    hdr = f"{'PIPELINE_ID':<14}  {'STATUS':<12}  {'STEP':<8}  {'NAME':<20}  {'CREATED':<20}"
    click.echo(hdr)
    click.echo("-" * len(hdr))
    for p in pipelines:
        status_str = _colorize_pipeline(f"{p.status.value:<12}", p.status.value)
        step_str = f"{p.current_step}/{p.total_steps}"
        name_str = (p.name or "")[:20]
        click.echo(
            f"{p.pipeline_id:<14}  {status_str}  {step_str:<8}  "
            f"{name_str:<20}  {_fmt_dt(p.created_at):<20}"
        )


@chain_cmd.command(name="status")
@click.argument("pipeline_id")
@click.pass_context
def chain_status(ctx: click.Context, pipeline_id: str) -> None:
    """Show detailed status for a pipeline."""
    store = _get_store(ctx)
    pipeline = store.get_pipeline(pipeline_id)
    if pipeline is None:
        click.echo(f"Pipeline {pipeline_id} not found.", err=True)
        ctx.exit(1)
        return

    click.echo(f"Pipeline ID: {pipeline.pipeline_id}")
    if pipeline.name:
        click.echo(f"Name:        {pipeline.name}")
    click.echo(f"Status:      {_colorize_pipeline(pipeline.status.value, pipeline.status.value)}")
    if pipeline.project:
        click.echo(f"Project:     {pipeline.project}")
    if pipeline.tags:
        click.echo(f"Tags:        {', '.join(pipeline.tags)}")
    click.echo(f"Steps:       {pipeline.current_step}/{pipeline.total_steps}")
    click.echo(f"Created:     {_fmt_dt(pipeline.created_at)}")
    if pipeline.last_advanced_at:
        click.echo(f"Last adv:    {_fmt_dt(pipeline.last_advanced_at)}")
    if pipeline.finished_at:
        click.echo(f"Finished:    {_fmt_dt(pipeline.finished_at)}")

    # Step table
    click.echo("")
    step_hdr = f"{'IDX':<5}  {'NAME':<20}  {'JOB_ID':<16}  {'STATUS':<12}"
    click.echo(step_hdr)
    click.echo("-" * len(step_hdr))

    all_jobs = store.list_all()
    for idx, step in enumerate(pipeline.spec):
        step_name = (step.name or step.params.get("method", ""))[:20]
        # Find job for this step
        job_id = ""
        job_status = ""
        for job in all_jobs:
            meta = job.meta or {}
            if (meta.get("pipeline_id") == pipeline.pipeline_id
                    and meta.get("pipeline_step") == idx):
                job_id = job.job_id
                job_status = job.state.value
                break
        status_display = _colorize(job_status, job_status) if job_status else "-"
        click.echo(
            f"{idx:<5}  {step_name:<20}  {job_id or '-':<16}  {status_display:<12}"
        )


@chain_cmd.command(name="cancel")
@click.argument("pipeline_id")
@click.option("--force", is_flag=True, default=False, help="Skip confirmation.")
@click.pass_context
def chain_cancel(ctx: click.Context, pipeline_id: str, force: bool) -> None:
    """Cancel a running pipeline."""
    if not force:
        if not click.confirm(f"Cancel pipeline {pipeline_id}?"):
            click.echo("Aborted.")
            return
    kbu = _get_kbu(ctx)
    try:
        pipeline = kbu.cancel_pipeline(pipeline_id)
    except KeyError:
        click.echo(f"Pipeline {pipeline_id} not found.", err=True)
        ctx.exit(1)
        return
    click.echo(
        f"Pipeline {pipeline.pipeline_id}: "
        f"{_colorize_pipeline(pipeline.status.value, pipeline.status.value)}"
    )


@chain_cmd.command(name="advance")
@click.pass_context
def chain_advance(ctx: click.Context) -> None:
    """Force a one-shot pipeline advancement pass."""
    kbu = _get_kbu(ctx)
    changed = kbu.advance_pipelines()
    if not changed:
        click.echo("No pipelines advanced.")
    else:
        for p in changed:
            click.echo(
                f"  {p.pipeline_id}: step {p.current_step}/{p.total_steps} "
                f"→ {_colorize_pipeline(p.status.value, p.status.value)}"
            )
