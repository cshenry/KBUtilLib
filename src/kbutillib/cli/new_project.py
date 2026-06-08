"""``kbu new-project`` — scaffold a new KBUtilLib research project.

Creates a project directory from the ``templates/research-project/`` tree,
sets up a per-project venv, editable-installs KBUtilLib, registers a
Jupyter kernel, writes ``kbu-project.toml``, and runs ``git init``.

v1 targets macOS only.  On non-macOS, prints the v1 message and exits 1
unless ``KBU_PLATFORM_OVERRIDE=force`` is set.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click

from .manifest import (
    now_utc_iso,
    write_project_manifest,
)
from ._template_ops import (
    copy_template_tree as _copy_template_tree,
    compute_file_hashes as _compute_file_hashes,
    run_venvman_project as _run_venvman_project,
    create_plain_venv as _create_plain_venv,
    parse_virtual_env_from_activate as _parse_virtual_env_from_activate,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_V1_MACOS_ONLY_MESSAGE = (
    "v1 currently targets macOS. Linux/Windows support is planned for v2. "
    "To install KBUtilLib manually for now: "
    "`python -m venv .venv && source .venv/bin/activate && "
    "pip install -e <path-to-KBUtilLib>`. "
    "Then register a Jupyter kernel: "
    "`python -m ipykernel install --user --name=kbutillib`. "
    "You can use the tier-2 skills once "
    "`/path/to/KBUtilLib/.claude/` is on your Claude Code skill search path."
)

#: Tracked subdirectories (relative to project root) that are hashed for update.
_TRACKED_DIRS = [".claude/commands", ".vscode"]


# ---------------------------------------------------------------------------
# KBUtilLib root resolution
# ---------------------------------------------------------------------------


def _kbutillib_root() -> Path:
    """Return the absolute path to the KBUtilLib repo root.

    src/kbutillib/cli/new_project.py → src/kbutillib/cli → src/kbutillib → src → repo_root
    """
    this_file = Path(__file__).resolve()
    return this_file.parent.parent.parent.parent


# ---------------------------------------------------------------------------
# Platform check
# ---------------------------------------------------------------------------


def _is_macos_or_override() -> bool:
    """Return True if this is macOS or KBU_PLATFORM_OVERRIDE=force is set."""
    return sys.platform == "darwin" or os.environ.get("KBU_PLATFORM_OVERRIDE") == "force"


def _is_darwin() -> bool:
    """Return True if this is macOS."""
    return sys.platform == "darwin"


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------


def _git_commit(repo_root: Path) -> str:
    """Return the current HEAD commit SHA in *repo_root*, or empty string."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except OSError:
        pass
    return ""


# ---------------------------------------------------------------------------
# Core new_project logic
# ---------------------------------------------------------------------------


def new_project(  # noqa: C901 — orchestration function
    path: Path,
    name: str,
    author: str,
    affiliation: str,
    orcid: str,
    first_subproject: Optional[str] = None,
) -> None:
    """Scaffold a new KBUtilLib research project at *path*.

    Steps:
    1. Reject if *path* exists.
    2. Copy ``templates/research-project/`` tree with ``{{project_name}}`` substitution.
    3. Platform gate (macOS or KBU_PLATFORM_OVERRIDE=force).
    4. Create per-project venv (venvman or .venv fallback).
    5. pip install -e <KBUTILLIB_ROOT> in venv.
    6. Register Jupyter kernel.
    7. Write kbu-project.toml with [update.file_hashes].
    8. git init + initial commit.
    9. Create first_subproject if given.
    10. Print Cursor instructions.
    """
    if path.exists():
        click.echo(f"Error: path already exists: {path}", err=True)
        sys.exit(1)

    kbu_root = _kbutillib_root()
    template_src = kbu_root / "templates" / "research-project"

    # Create destination and copy template
    path.mkdir(parents=True)
    if template_src.is_dir():
        _copy_template_tree(template_src, path, {"project_name": name})

    # Platform gate — checked before venv creation
    if not _is_macos_or_override():
        click.echo(_V1_MACOS_ONLY_MESSAGE)
        sys.exit(1)

    # Determine venvman availability (non-Darwin with override → treat absent)
    use_venvman = _is_darwin() and shutil.which("venvman") is not None

    venv_python: Optional[Path]
    venv_manager: str

    if use_venvman:
        click.echo(f"Detected venvman — creating project venv for {name} ...")
        venv_python, venvman_err = _run_venvman_project(name, path)
        if venv_python is None:
            click.echo(
                f"Warning: venvman create failed ({venvman_err}) — falling back to python -m venv .venv",
                err=True,
            )
            venv_python = _create_plain_venv(path)
            venv_manager = ".venv"
        else:
            venv_manager = "venvman"
    else:
        click.echo("Creating .venv with python -m venv ...")
        venv_python = _create_plain_venv(path)
        venv_manager = ".venv"

    venv_python_str = str(venv_python)

    # pip install -e <KBUTILLIB_ROOT> plus ipykernel (needed for the kernel
    # registration step below; ipykernel is in KBUtilLib's [notebook] extra,
    # not base, so it isn't pulled in by the editable install). Mirrors the
    # same fix in kbu init and kbu bootstrap (commit a6fb33d).
    click.echo(f"Installing KBUtilLib editable from {kbu_root} ...")
    subprocess.run(
        [venv_python_str, "-m", "pip", "install", "-e", str(kbu_root), "ipykernel"],
        check=True,
    )

    # Register Jupyter kernel
    click.echo(f"Registering Jupyter kernel '{name}' ...")
    subprocess.run(
        [
            venv_python_str,
            "-m",
            "ipykernel",
            "install",
            "--user",
            f"--name={name}",
            f"--display-name={name} (kbu)",
        ],
        check=True,
    )

    # Compute file hashes for tracked dirs
    file_hashes = _compute_file_hashes(path, _TRACKED_DIRS)

    # Get source commit
    source_commit = _git_commit(kbu_root)
    now = now_utc_iso()

    # Write kbu-project.toml
    manifest: dict = {
        "project": {
            "name": name,
            "created_at": now,
            "authors": [
                {
                    "name": author,
                    "affiliation": affiliation,
                    "orcid": orcid,
                }
            ],
        },
        "kbutillib": {
            "source_path": str(kbu_root),
            "source_commit": source_commit,
        },
        "update": {
            "last_pulled_at": now,
            "last_pulled_commit": source_commit,
            "file_hashes": file_hashes,
        },
    }
    write_project_manifest(path, manifest)

    # git init + initial commit
    subprocess.run(["git", "init"], cwd=str(path), check=True)
    subprocess.run(["git", "add", "."], cwd=str(path), check=True)
    subprocess.run(
        ["git", "commit", "-m", "feat(kbu): initialize project via kbu new-project"],
        cwd=str(path),
        check=True,
    )

    # Create first subproject if given
    if first_subproject:
        click.echo(f"Creating first subproject '{first_subproject}' ...")
        subprocess.run(
            [venv_python_str, "-m", "kbutillib", "subproject", "create", first_subproject],
            cwd=str(path),
            check=False,  # best-effort; don't fail the whole new-project on this
        )

    # Print instructions
    workspace_file = f"{name}.code-workspace"
    click.echo(f"\nProject created at: {path}")
    click.echo(f"Open in Cursor: cursor {path}/{workspace_file}")
    click.echo("Then in Cursor: open terminal -> run claude -> type /kbu-start")


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command("new-project")
@click.argument("path", type=click.Path(path_type=Path))
@click.option("--name", default=None, help="Project name.")
@click.option("--author", default=None, help="Author name.")
@click.option("--affiliation", default=None, help="Author affiliation.")
@click.option("--orcid", default=None, help="Author ORCID.")
@click.option("--first-subproject", default=None, help="Name of the first subproject to create.")
def new_project_command(
    path: Path,
    name: Optional[str],
    author: Optional[str],
    affiliation: Optional[str],
    orcid: Optional[str],
    first_subproject: Optional[str],
) -> None:
    """Scaffold a new KBUtilLib research project.

    PATH is the destination directory (must not exist).

    Prompts for any required arguments not provided via flags.
    """
    if name is None:
        name = click.prompt("Project name")
    if author is None:
        author = click.prompt("Author name")
    if affiliation is None:
        affiliation = click.prompt("Author affiliation")
    if orcid is None:
        orcid = click.prompt("Author ORCID")

    new_project(
        path=path,
        name=name,
        author=author,
        affiliation=affiliation,
        orcid=orcid,
        first_subproject=first_subproject,
    )
