"""``kbu researchos`` — Research-OS project scaffolder for KBUtilLib.

Subcommands
-----------
kbu researchos new <PARENT> <NAME> [options]
    Scaffold a new Research-OS study at <root>/<PARENT>/<NAME>/.

kbu researchos open <PARENT> <NAME>
    Open Cursor on an existing Research-OS study.

kbu researchos ls [--json]
    List Research-OS projects grouped by parent.

kbu researchos set-root [--root PATH] [--tooling-venv PATH] [--aiassistant-root PATH]
    Persist root/tooling-venv/aiassistant-root to ~/.kbutillib/config.yaml.

Output shape: per-step ``── name``, ``✓``/``✗`` lines, summary block,
return codes 0 all-ok / 1 partial / 2 none (following CRAFT CLI conventions).
"""

from __future__ import annotations

import json as _json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click


# ---------------------------------------------------------------------------
# Context object
# ---------------------------------------------------------------------------


class _ResearchOSCtx:
    """Holds resolved root, tooling_venv, and aiassistant_root."""

    def __init__(
        self,
        root_opt: Optional[str],
        tooling_venv_opt: Optional[str],
        aiassistant_root_opt: Optional[str],
    ) -> None:
        from kbutillib.researchos.config import (
            resolve_aiassistant_root,
            resolve_researchos_root,
            resolve_tooling_venv,
        )

        self.researchos_root = resolve_researchos_root(explicit=root_opt)
        self.tooling_venv = resolve_tooling_venv(explicit=tooling_venv_opt)
        self.aiassistant_root = resolve_aiassistant_root(
            explicit=aiassistant_root_opt
        )

    def manager(self):
        """Return a ResearchOSProject instance."""
        from kbutillib.researchos.manager import ResearchOSProject

        return ResearchOSProject(
            researchos_root=self.researchos_root,
            tooling_venv=self.tooling_venv,
            aiassistant_root=self.aiassistant_root,
        )


def _get_ctx(ctx: click.Context) -> _ResearchOSCtx:
    """Resolve the Research-OS context from the click context."""
    return _ResearchOSCtx(
        root_opt=ctx.obj.get("root"),
        tooling_venv_opt=ctx.obj.get("tooling_venv"),
        aiassistant_root_opt=ctx.obj.get("aiassistant_root"),
    )


# ---------------------------------------------------------------------------
# Cursor launch helper
# ---------------------------------------------------------------------------


def _open_cursor_workspace(ws_file: Path) -> None:
    """Launch Cursor on *ws_file*.

    If ``cursor`` is not on PATH, prints the workspace path and a manual
    instruction rather than failing hard.

    Args:
        ws_file: Path to the ``.code-workspace`` file.
    """
    cursor_bin = shutil.which("cursor")
    if cursor_bin:
        subprocess.Popen([cursor_bin, str(ws_file)])  # noqa: S603
        click.echo(f"Cursor opened: {ws_file}")
    else:
        click.echo(
            f"cursor is not on PATH. Open the workspace manually:\n"
            f"  {ws_file}"
        )


# ---------------------------------------------------------------------------
# researchos group
# ---------------------------------------------------------------------------


@click.group("researchos")
@click.option(
    "--root",
    metavar="PATH",
    default=None,
    help=(
        "Root directory under which parent/name subdirectories are created. "
        "Overrides RESEARCHOS_ROOT env var and config."
    ),
)
@click.option(
    "--tooling-venv",
    "tooling_venv",
    metavar="PATH",
    default=None,
    help=(
        "Path to the shared Research-OS tooling venv. "
        "Overrides RESEARCHOS_TOOLING_VENV env var and config."
    ),
)
@click.option(
    "--aiassistant-root",
    "aiassistant_root",
    metavar="PATH",
    default=None,
    help=(
        "Path to the AIAssistant repository root. "
        "Overrides AIASSISTANT_ROOT env var and config."
    ),
)
@click.pass_context
def researchos_cmd(
    ctx: click.Context,
    root: Optional[str],
    tooling_venv: Optional[str],
    aiassistant_root: Optional[str],
) -> None:
    """Scaffold and manage Research-OS studies under a parent project.

    Studies live at <root>/<PARENT>/<NAME>/ and are registered in the
    AIAssistant project registry.
    """
    ctx.ensure_object(dict)
    ctx.obj["root"] = root
    ctx.obj["tooling_venv"] = tooling_venv
    ctx.obj["aiassistant_root"] = aiassistant_root


# ---------------------------------------------------------------------------
# kbu researchos new
# ---------------------------------------------------------------------------

_WORKSPACE_MODE_CHOICES = click.Choice(
    ["analysis", "tool_build", "exploration", "notebook", "multi_study", "hybrid"],
    case_sensitive=False,
)


@researchos_cmd.command("new")
@click.argument("parent")
@click.argument("name")
@click.option(
    "--name",
    "display_name",
    default=None,
    metavar="TEXT",
    help="Human-readable name passed to research-os init.",
)
@click.option(
    "--domain",
    default=None,
    metavar="TEXT",
    help="Research domain passed to research-os init.",
)
@click.option(
    "--question",
    "questions",
    multiple=True,
    metavar="TEXT",
    help="Research question(s) passed to research-os init. Repeatable.",
)
@click.option(
    "--workspace-mode",
    default="analysis",
    show_default=True,
    type=_WORKSPACE_MODE_CHOICES,
    help="Research-OS workspace mode.",
)
@click.option(
    "--ide",
    default="cursor,claude",
    show_default=True,
    metavar="TEXT",
    help="IDE(s) to configure (passed to research-os init).",
)
@click.option(
    "--open",
    "open_cursor",
    is_flag=True,
    default=False,
    help="Launch Cursor on the workspace after creation.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite an existing non-empty project directory.",
)
@click.pass_context
def new_cmd(
    ctx: click.Context,
    parent: str,
    name: str,
    display_name: Optional[str],
    domain: Optional[str],
    questions: tuple,
    workspace_mode: str,
    ide: str,
    open_cursor: bool,
    force: bool,
) -> None:
    """Scaffold a new Research-OS study at <root>/PARENT/NAME/.

    Runs research-os init, rewrites MCP config command fields to the
    absolute binary path, writes a .code-workspace file, initializes a
    git repository, and registers the study in the AIAssistant project
    registry.
    """
    ros_ctx = _get_ctx(ctx)
    try:
        project_path = ros_ctx.manager().new(
            parent,
            name,
            display_name=display_name,
            domain=domain,
            questions=list(questions) if questions else None,
            workspace_mode=workspace_mode,
            ide=ide,
            open_cursor=False,  # handled below
            force=force,
        )
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"\nProject created: {project_path}")

    if open_cursor:
        ws_file = project_path / f"{name}.code-workspace"
        _open_cursor_workspace(ws_file)


# ---------------------------------------------------------------------------
# kbu researchos open
# ---------------------------------------------------------------------------


@researchos_cmd.command("open")
@click.argument("parent")
@click.argument("name")
@click.pass_context
def open_cmd(ctx: click.Context, parent: str, name: str) -> None:
    """Open Cursor on an existing Research-OS study at <root>/PARENT/NAME/.

    Errors if the project directory does not exist (run ``new`` first).
    """
    ros_ctx = _get_ctx(ctx)
    try:
        project_path = ros_ctx.manager().open(parent, name)
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc

    ws_file = project_path / f"{name}.code-workspace"
    if not ws_file.is_file():
        click.echo(
            f"No .code-workspace file found at {ws_file}.\n"
            "Opening the project directory directly.",
            err=True,
        )
        ws_file = project_path

    _open_cursor_workspace(ws_file)


# ---------------------------------------------------------------------------
# kbu researchos ls
# ---------------------------------------------------------------------------


@researchos_cmd.command("ls")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a stable JSON array sorted by (parent, name).",
)
@click.pass_context
def ls_cmd(ctx: click.Context, as_json: bool) -> None:
    """List Research-OS projects grouped by parent.

    Default output groups projects under a ``── <parent>`` header per parent.
    ``--json`` emits a stable array ``[{"parent","name","path","has_workspace"}]``.
    """
    ros_ctx = _get_ctx(ctx)
    projects = ros_ctx.manager().list()

    if as_json:
        data = [
            {
                "parent": p.parent,
                "name": p.name,
                "path": str(p.path),
                "has_workspace": p.has_workspace,
            }
            for p in projects
        ]
        click.echo(_json.dumps(data, indent=2))
        return

    if not projects:
        click.echo("(no Research-OS projects found)")
        return

    # Group by parent
    by_parent: dict[str, list] = {}
    for p in projects:
        by_parent.setdefault(p.parent, []).append(p)

    for parent_name, entries in sorted(by_parent.items()):
        click.echo(f"── {parent_name}")
        for entry in entries:
            ws_marker = " [workspace]" if entry.has_workspace else ""
            click.echo(f"   {entry.name}{ws_marker}  ({entry.path})")


# ---------------------------------------------------------------------------
# kbu researchos set-root
# ---------------------------------------------------------------------------


@researchos_cmd.command("set-root")
@click.pass_context
def set_root_cmd(ctx: click.Context) -> None:
    """Persist root/tooling-venv/aiassistant-root to ~/.kbutillib/config.yaml.

    Uses ``--root``, ``--tooling-venv``, and/or ``--aiassistant-root`` from
    the group-level options. At least one must be supplied.
    """
    from kbutillib.researchos.config import set_root

    root_opt = ctx.obj.get("root")
    tooling_venv_opt = ctx.obj.get("tooling_venv")
    aiassistant_root_opt = ctx.obj.get("aiassistant_root")

    if root_opt is None and tooling_venv_opt is None and aiassistant_root_opt is None:
        raise click.UsageError(
            "Provide at least one of --root, --tooling-venv, or --aiassistant-root."
        )

    try:
        set_root(
            root=root_opt,
            tooling_venv=tooling_venv_opt,
            aiassistant_root=aiassistant_root_opt,
        )
    except ValueError as exc:  # pragma: no cover
        raise click.ClickException(str(exc)) from exc

    if root_opt is not None:
        click.echo(
            f"Set researchos.root = {Path(root_opt).expanduser().resolve()}"
        )
    if tooling_venv_opt is not None:
        click.echo(
            f"Set researchos.tooling_venv = {Path(tooling_venv_opt).expanduser().resolve()}"
        )
    if aiassistant_root_opt is not None:
        click.echo(
            f"Set researchos.aiassistant_root = "
            f"{Path(aiassistant_root_opt).expanduser().resolve()}"
        )
