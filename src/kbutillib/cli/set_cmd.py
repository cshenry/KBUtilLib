"""``kbu set`` — interactive project binding for AIAssistant integration.

Provides ``kbu set project``: ranked candidate selection from the AIAssistant
registry with a "create new" path, persisting the binding into kbu-project.toml.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

import click
import yaml

from .binding import resolve_binding, set_binding
from .registry_reader import rank_candidates
from .session import _detect_aiassistant
from .subproject import _find_project_root


# ── slug helpers ────────────────────────────────────────────────────────────────


def _slugify(name: str) -> str:
    """Convert a display name to a ``[a-z0-9-]`` slug (max 40 chars).

    Rules (from Acceptance Criterion #1):
    - Lowercase ASCII.
    - Whitespace and underscores → single ``-``.
    - All other non-alphanumeric (non-hyphen) characters stripped.
    - Consecutive hyphens collapsed to a single ``-``.
    - Trimmed to 40 characters.
    - Leading/trailing hyphens stripped.
    """
    slug = name.lower()
    # Replace whitespace and underscores with hyphens
    slug = re.sub(r"[\s_]+", "-", slug)
    # Strip non-alphanumeric-non-hyphen characters
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    # Collapse consecutive hyphens
    slug = re.sub(r"-{2,}", "-", slug)
    # Trim to 40 chars
    slug = slug[:40]
    # Strip leading/trailing hyphens
    slug = slug.strip("-")
    return slug


def _unique_slug(base_slug: str, existing_ids: set[str]) -> str:
    """Return *base_slug* uniquified against *existing_ids* by appending -2, -3, …"""
    if base_slug not in existing_ids:
        return base_slug
    n = 2
    while True:
        candidate = f"{base_slug[:37]}-{n}" if len(base_slug) + len(str(n)) + 1 > 40 else f"{base_slug}-{n}"
        if candidate not in existing_ids:
            return candidate
        n += 1


def _load_existing_ids(aia_root: Path) -> set[str]:
    """Return the set of project ids currently in the AIAssistant registry."""
    registry_path = aia_root / "state" / "project_registry.yaml"
    try:
        with open(registry_path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        return set(raw.get("projects", {}).keys())
    except Exception:
        return set()


# ── Click group ─────────────────────────────────────────────────────────────────


@click.group(name="set")
def set_cmd() -> None:
    """Set kbu project configuration options."""


# ── set project ─────────────────────────────────────────────────────────────────


@set_cmd.command(name="project")
@click.option(
    "--query", default=None,
    help="Free-text search to pre-filter registry candidates (overrides local title).",
)
@click.option(
    "--limit", default=10, show_default=True,
    help="Number of ranked candidates to display.",
)
@click.pass_context
def set_project_cmd(ctx: click.Context, query: Optional[str], limit: int) -> None:
    """Bind this kbu project to an AIAssistant project_id.

    Ranks candidates from the AIAssistant registry by similarity to the local
    project name.  Offers a "create new" option to define a new project id slug.
    The chosen binding is persisted into kbu-project.toml under [aiassistant].
    """
    project_root = _find_project_root(Path.cwd())

    # Read the local project title for similarity ranking.
    local_title = _read_local_title(project_root)

    aia_root = _detect_aiassistant()
    if aia_root is None:
        click.echo(
            "Error: AIAssistant not detected. "
            "Ensure sessions.db exists at one of the configured paths "
            "(set KBU_AIA_PATHS if needed).",
            err=True,
        )
        ctx.exit(1)
        return

    # Show current binding if present.
    current = resolve_binding(project_root)
    if current:
        click.echo(f"Current binding: {current}")

    # Rank candidates.
    candidates = rank_candidates(local_title, aia_root=aia_root, query=query, limit=limit)

    # Display ranked list.
    click.echo(f"\nCandidates for '{local_title}':")
    for i, c in enumerate(candidates, start=1):
        score_pct = int(c["score"] * 100)
        click.echo(f"  {i:2d}. {c['project_id']!r}  ({c['name']})  [{score_pct}%]")

    create_label = "C"
    click.echo(f"  {create_label}.  Create a new project id")
    click.echo()

    # Prompt for choice.
    choice = click.prompt(
        f"Enter 1-{len(candidates)} to pick, or C to create new",
        default="1" if candidates else "C",
    ).strip().upper()

    if choice == "C" or not candidates:
        _handle_create_new(ctx, project_root, aia_root)
    else:
        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(candidates):
                raise ValueError
        except ValueError:
            click.echo("Invalid selection. Aborted.", err=True)
            ctx.exit(1)
            return

        chosen = candidates[idx]
        project_id = chosen["project_id"]
        project_name = chosen["name"]

        click.echo(f"\nSelected: {project_id!r} ({project_name})")
        if not click.confirm("Persist this binding?"):
            click.echo("Aborted.")
            return

        set_binding(project_root, project_id, project_name)
        click.echo(f"Bound to '{project_id}'.")


def _handle_create_new(ctx: click.Context, project_root: Path, aia_root: Path) -> None:
    """Interactive sub-flow for creating a new project id slug."""
    display_name = click.prompt("New project display name").strip()
    if not display_name:
        click.echo("Name cannot be empty. Aborted.", err=True)
        ctx.exit(1)
        return

    existing_ids = _load_existing_ids(aia_root)
    base_slug = _slugify(display_name)
    if not base_slug:
        click.echo(
            "Could not derive a valid slug from that name. "
            "Use ASCII letters, digits, spaces, or hyphens.",
            err=True,
        )
        ctx.exit(1)
        return

    final_slug = _unique_slug(base_slug, existing_ids)

    if final_slug not in existing_ids:
        click.echo(
            f"\nNew project id (slug): {final_slug!r}  "
            "(warning: not yet in the registry — will be created on first ingest)"
        )
    else:
        click.echo(f"\nProject id (slug): {final_slug!r}")

    if not click.confirm(f"Persist binding as '{final_slug}'?"):
        click.echo("Aborted.")
        return

    set_binding(project_root, final_slug, display_name)
    click.echo(f"Bound to '{final_slug}'.")


def _read_local_title(project_root: Path) -> str:
    """Return the local project title from kbu-project.toml, or a fallback."""
    try:
        from .manifest import read_project_manifest  # noqa: PLC0415
        data = read_project_manifest(project_root)
        proj = data.get("project", {})
        return proj.get("title") or proj.get("name") or project_root.name
    except Exception:
        return project_root.name
