"""``kbu subproject`` — subproject lifecycle management.

State machine, artifact precondition validation, and TOML manifest I/O for
kbu subprojects.  All TOML operations are delegated to ``manifest.py``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional

import click

from .manifest import (
    now_utc_iso,
    read_subproject_manifest,
    write_subproject_manifest,
)


# ── state machine ──────────────────────────────────────────────────────────

#: Ordered list of all valid states.
_STATES = [
    "plan",
    "p-review",
    "build",
    "b-review",
    "run",
    "synthesize",
    "s-review",
    "complete",
]

#: Forward transition table: current → next.
_FORWARD: dict[str, str] = {
    "plan": "p-review",
    "p-review": "build",
    "build": "b-review",
    "b-review": "run",
    "run": "synthesize",
    "synthesize": "s-review",
    "s-review": "complete",
}

#: Reverse transition table (review → prior action state).
_REVERSE: dict[str, str] = {
    "p-review": "plan",
    "b-review": "build",
    "s-review": "synthesize",
}

#: Review states that support --reverse.
_REVIEW_STATES = set(_REVERSE.keys())

#: next_action hint for the tier-2 dashboard.
_NEXT_ACTION: dict[str, str] = {
    "plan": "Plan",
    "p-review": "Review",
    "build": "Build",
    "b-review": "Review",
    "run": "Run",
    "synthesize": "Synthesize",
    "s-review": "Review",
    "complete": "Complete",
}


# ── verdict parser ─────────────────────────────────────────────────────────

_VERDICT_PATTERN = re.compile(
    r"<!--\s*kbu-review:verdict:\s*(pass|fail)\s*-->",
    re.IGNORECASE,
)


def _parse_verdict(path: Path) -> Optional[str]:
    """Return ``"pass"`` or ``"fail"`` from the top HTML comment in *path*.

    Returns ``None`` if the file does not exist or contains no verdict comment.
    """
    if not path.exists():
        return None
    # Only scan the first 4 KiB to keep it fast.
    text = path.read_text(encoding="utf-8", errors="replace")[:4096]
    m = _VERDICT_PATTERN.search(text)
    if m:
        return m.group(1).lower()
    return None


# ── artifact precondition validators ──────────────────────────────────────


def _glob_review_files(subproject_dir: Path, stage: str) -> list[Path]:
    """Return all ``REVIEW_<stage>_<n>.md`` files for *stage* in *subproject_dir*."""
    return sorted(subproject_dir.glob(f"REVIEW_{stage}_*.md"))


def _check_forward_preconditions(
    subproject_dir: Path,
    data: dict,
    current_state: str,
) -> Optional[str]:
    """Validate forward preconditions for *current_state*.

    Returns a reason string (one of the enumerated disabled-reason strings)
    on failure, or ``None`` on success.
    """
    if current_state == "plan":
        rp = subproject_dir / "RESEARCH_PLAN.md"
        if not rp.exists():
            return "missing-artifact"

    elif current_state == "p-review":
        reviews = _glob_review_files(subproject_dir, "plan")
        if not reviews:
            return "missing-artifact"
        # Need at least one passing review
        if not any(_parse_verdict(r) == "pass" for r in reviews):
            return "review-pending"

    elif current_state == "build":
        nb_dir = subproject_dir / "notebooks"
        if not nb_dir.is_dir():
            return "missing-artifact"
        if not any(nb_dir.glob("*.ipynb")):
            return "missing-artifact"
        if not (nb_dir / "util.py").exists():
            return "missing-artifact"

    elif current_state == "b-review":
        reviews = _glob_review_files(subproject_dir, "build")
        if not reviews:
            return "missing-artifact"
        if not any(_parse_verdict(r) == "pass" for r in reviews):
            return "review-pending"

    elif current_state == "run":
        notebooks: list[dict] = data.get("notebooks", [])
        if not notebooks:
            # No notebooks registered — treat as stale
            return "notebooks-stale"
        for nb in notebooks:
            if not nb.get("last_run_at"):
                return "notebooks-stale"
            if nb.get("modified_since_run", True):
                return "notebooks-stale"

    elif current_state == "synthesize":
        report = subproject_dir / "REPORT.md"
        if not report.exists():
            return "missing-artifact"

    elif current_state == "s-review":
        reviews = _glob_review_files(subproject_dir, "synthesis")
        if not reviews:
            return "missing-artifact"
        if not any(_parse_verdict(r) == "pass" for r in reviews):
            return "review-pending"

    return None


# ── subproject scaffold ────────────────────────────────────────────────────


def _scaffold_subproject(subproject_dir: Path, name: str, title: str) -> None:
    """Create the full subproject directory layout."""
    subproject_dir.mkdir(parents=True, exist_ok=True)
    # Subdirectories
    for d in ("notebooks", "nboutput", "data", "user_data", "figures", "sessions"):
        (subproject_dir / d).mkdir(exist_ok=True)
    # util.py stub
    util_py = subproject_dir / "notebooks" / "util.py"
    if not util_py.exists():
        util_py.write_text(
            f"# {name} — shared notebook utilities\n"
            "# Add project-wide helpers here.\n"
        )
    # references.md
    refs = subproject_dir / "references.md"
    if not refs.exists():
        refs.write_text(f"# References — {title or name}\n")


# ── helpers ────────────────────────────────────────────────────────────────


def _find_project_root(start: Path) -> Path:
    """Walk up from *start* to find ``kbu-project.toml``.

    Returns the directory containing the manifest, or *start* if not found.
    Callers are expected to validate that the manifest actually exists.
    """
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "kbu-project.toml").exists():
            return candidate
    return current


def _list_subproject_names(project_root: Path) -> list[str]:
    """Return all subproject names sorted by created_at (most-recent first)."""
    sp_root = project_root / "subprojects"
    if not sp_root.is_dir():
        return []
    names = []
    for d in sp_root.iterdir():
        if d.is_dir() and (d / "kbu-subproject.toml").exists():
            names.append(d.name)
    # Sort by created_at if readable, else alphabetically
    def _created_at(name: str) -> str:
        try:
            data = read_subproject_manifest(project_root, name)
            return data.get("subproject", {}).get("created_at", "")
        except Exception:
            return ""

    names.sort(key=_created_at, reverse=True)
    return names


# ── Click group ────────────────────────────────────────────────────────────


@click.group(name="subproject")
def subproject_cmd() -> None:
    """Manage kbu subprojects (state machine, manifest, scaffolding)."""


# ── create ─────────────────────────────────────────────────────────────────


@subproject_cmd.command(name="create")
@click.argument("name")
@click.option("--title", default="", help="Human-readable title for the subproject.")
@click.pass_context
def create_cmd(ctx: click.Context, name: str, title: str) -> None:
    """Create a new subproject named NAME with full directory scaffold."""
    project_root = _find_project_root(Path.cwd())

    sp_dir = project_root / "subprojects" / name
    manifest_path = sp_dir / "kbu-subproject.toml"
    if manifest_path.exists():
        click.echo(f"Subproject '{name}' already exists.", err=True)
        ctx.exit(1)
        return

    _scaffold_subproject(sp_dir, name, title)

    now = now_utc_iso()
    data: dict = {
        "subproject": {
            "name": name,
            "title": title or name,
            "status": "plan",
            "created_at": now,
            "last_session_at": now,
        },
        "artifacts": {
            "research_plan": False,
            "report": False,
            "reviews": {
                "plan": [],
                "build": [],
                "synthesis": [],
            },
        },
        "notebooks": [],
        "session_refs": [],
    }
    write_subproject_manifest(project_root, name, data)
    click.echo(f"Created subproject '{name}' at {sp_dir}")


# ── list ───────────────────────────────────────────────────────────────────


@subproject_cmd.command(name="list")
@click.option("--json", "output_json", is_flag=True, default=False,
              help="Output JSON instead of TSV.")
@click.pass_context
def list_cmd(ctx: click.Context, output_json: bool) -> None:
    """List all subprojects with status and next action."""
    project_root = _find_project_root(Path.cwd())
    names = _list_subproject_names(project_root)

    rows = []
    for name in names:
        try:
            data = read_subproject_manifest(project_root, name)
        except Exception as exc:
            click.echo(f"Warning: could not read manifest for '{name}': {exc}", err=True)
            continue
        sp = data.get("subproject", {})
        status = sp.get("status", "unknown")
        next_action = _NEXT_ACTION.get(status, status)
        rows.append({
            "name": name,
            "status": status,
            "next_action": next_action,
        })

    if output_json:
        click.echo(json.dumps(rows))
        return

    # TSV output — header first
    click.echo("name\tstatus\tnext_action")
    for row in rows:
        click.echo(f"{row['name']}\t{row['status']}\t{row['next_action']}")


# ── status ─────────────────────────────────────────────────────────────────


@subproject_cmd.command(name="status")
@click.argument("name")
@click.option("--json", "output_json", is_flag=True, default=False,
              help="Output JSON instead of human-readable text.")
@click.pass_context
def status_cmd(ctx: click.Context, name: str, output_json: bool) -> None:
    """Show current state, valid transitions, and artifacts for subproject NAME."""
    project_root = _find_project_root(Path.cwd())

    try:
        data = read_subproject_manifest(project_root, name)
    except FileNotFoundError:
        click.echo(f"Subproject '{name}' not found.", err=True)
        ctx.exit(1)
        return

    sp = data.get("subproject", {})
    status = sp.get("status", "")
    if status not in _STATES:
        manifest_path = project_root / "subprojects" / name / "kbu-subproject.toml"
        click.echo(
            f"Error: unknown status value '{status}' in {manifest_path}",
            err=True,
        )
        ctx.exit(1)
        return

    next_state = _FORWARD.get(status)
    reverse_state = _REVERSE.get(status)

    if output_json:
        click.echo(json.dumps({
            "name": name,
            "status": status,
            "next_state": next_state,
            "reverse_state": reverse_state,
            "next_action": _NEXT_ACTION.get(status),
            "subproject": sp,
            "artifacts": data.get("artifacts", {}),
            "notebooks": data.get("notebooks", []),
            "session_refs": data.get("session_refs", []),
        }))
        return

    click.echo(f"Subproject: {name}")
    click.echo(f"Status:     {status}")
    click.echo(f"Next:       {next_state or '(terminal)'}")
    if reverse_state:
        click.echo(f"Reverse:    {reverse_state}  (via --reverse)")
    if sp.get("title"):
        click.echo(f"Title:      {sp['title']}")
    click.echo(f"Created:    {sp.get('created_at', '')}")
    click.echo(f"Last sess:  {sp.get('last_session_at', '')}")


# ── advance ────────────────────────────────────────────────────────────────


@subproject_cmd.command(name="advance")
@click.argument("name")
@click.option("--reverse", "go_reverse", is_flag=True, default=False,
              help="Reverse transition (review fail): move back to prior action state.")
@click.pass_context
def advance_cmd(ctx: click.Context, name: str, go_reverse: bool) -> None:
    """Advance subproject NAME to the next (or prior) state.

    Forward advance validates artifact preconditions.
    Reverse advance (--reverse) skips validation and is only valid from
    review states (p-review, b-review, s-review).
    """
    project_root = _find_project_root(Path.cwd())

    try:
        data = read_subproject_manifest(project_root, name)
    except FileNotFoundError:
        click.echo(f"Subproject '{name}' not found.", err=True)
        ctx.exit(1)
        return

    sp = data.get("subproject", {})
    current = sp.get("status", "")
    if current not in _STATES:
        manifest_path = project_root / "subprojects" / name / "kbu-subproject.toml"
        click.echo(
            f"Error: unknown status value '{current}' in {manifest_path}",
            err=True,
        )
        ctx.exit(1)
        return

    if go_reverse:
        if current not in _REVIEW_STATES:
            click.echo(
                f"Error: --reverse is only valid from a review state "
                f"(p-review, b-review, s-review). Current state: {current}",
                err=True,
            )
            ctx.exit(1)
            return
        new_state = _REVERSE[current]
    else:
        if current not in _FORWARD:
            click.echo(
                f"Error: subproject '{name}' is in terminal state '{current}'.",
                err=True,
            )
            ctx.exit(1)
            return

        subproject_dir = project_root / "subprojects" / name
        reason = _check_forward_preconditions(subproject_dir, data, current)
        if reason:
            click.echo(
                f"Cannot advance '{name}' from '{current}': {reason}",
                err=True,
            )
            sys.exit(1)

        new_state = _FORWARD[current]

    sp["status"] = new_state
    data["subproject"] = sp
    write_subproject_manifest(project_root, name, data)
    click.echo(f"'{name}': {current} → {new_state}")


# ── set-status ─────────────────────────────────────────────────────────────


@subproject_cmd.command(name="set-status")
@click.argument("name")
@click.argument("state")
@click.pass_context
def set_status_cmd(ctx: click.Context, name: str, state: str) -> None:
    """Admin override: set subproject NAME to STATE, bypassing validation."""
    if state not in _STATES:
        click.echo(
            f"Error: unknown state '{state}'. "
            f"Valid states: {', '.join(_STATES)}",
            err=True,
        )
        ctx.exit(1)
        return

    project_root = _find_project_root(Path.cwd())

    try:
        data = read_subproject_manifest(project_root, name)
    except FileNotFoundError:
        click.echo(f"Subproject '{name}' not found.", err=True)
        ctx.exit(1)
        return

    old_state = data.get("subproject", {}).get("status", "(unknown)")
    data.setdefault("subproject", {})["status"] = state
    write_subproject_manifest(project_root, name, data)
    click.echo(f"'{name}': {old_state} → {state}  (admin override)")
