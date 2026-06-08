"""``kbu migrate`` — retrofit a bootstrapped repo onto KBUtilLib 2.0 layout.

This is a **repo-level** command (contrast with ``kbu subproject adopt``,
which is a per-subproject operation).  ``kbu migrate`` is idempotent and
interactive: every non-trivial operation is presented to the user before
execution.

Steps performed
---------------
1. Verify the cwd is inside a bootstrapped project (``kbu-project.toml``
   found in a parent).
2. If ``[layout.shared_dirs]`` is absent from ``kbu-project.toml``, add it
   with :data:`kbutillib.layout.DEFAULT_SHARED_DIRS`.
3. For each shared dir not already present at the project root, create the
   directory and an empty ``.gitkeep``.
4. Walk ``subprojects/<name>/`` for each registered subproject:
   a. If ``data/`` exists, prompt where to move its contents
      (four options; see below).
   b. If ``user_data/`` exists, same prompt.
   c. If ``references.md`` exists, prompt to convert to
      ``literature/index.md``, keep as-is, or delete.
   d. Ensure ``.cache/`` and ``literature/`` dirs exist (create silently
      if absent — these are empty scaffolding dirs, not user data).
5. Append per-subproject gitignore lines to root ``.gitignore`` (one
   marker-delimited block per subproject, idempotent).

Data relocation options for ``data/`` and ``user_data/``
---------------------------------------------------------
1. Move into ``root data/<subproject-name>/`` (namespaced).
2. Merge into root ``data/`` (flat, collisions abort).
3. Keep as ``nboutput/`` (rename in place).
4. Skip (leave as-is).
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path
from typing import Optional

import click
import tomli_w

from kbutillib import layout as _layout
from kbutillib.cli.subproject import (
    _append_subproject_gitignore,
    _find_project_root,
    _list_subproject_names,
)


# ---------------------------------------------------------------------------
# TOML helpers
# ---------------------------------------------------------------------------


def _add_layout_shared_dirs(project_root: Path) -> bool:
    """Add ``[layout.shared_dirs]`` to ``kbu-project.toml`` if absent.

    Returns ``True`` if the file was modified, ``False`` if it was already
    present or if ``kbu-project.toml`` does not exist.
    """
    toml_path = project_root / "kbu-project.toml"
    if not toml_path.exists():
        return False

    with toml_path.open("rb") as fh:
        data = tomllib.load(fh)

    if "layout" in data and "shared_dirs" in data["layout"]:
        return False  # already present

    layout_section = data.get("layout", {})
    layout_section["shared_dirs"] = list(_layout.DEFAULT_SHARED_DIRS)
    data["layout"] = layout_section

    with toml_path.open("wb") as fh:
        tomli_w.dump(data, fh)
    return True


# ---------------------------------------------------------------------------
# Subproject data-relocation helpers
# ---------------------------------------------------------------------------


def _move_namespaced(src_dir: Path, root_data: Path, sp_name: str) -> None:
    """Move *src_dir* contents into ``root_data/<sp_name>/``."""
    dest = root_data / sp_name
    dest.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        shutil.move(str(item), str(dest / item.name))
    src_dir.rmdir()


def _merge_flat(src_dir: Path, root_data: Path) -> Optional[str]:
    """Merge *src_dir* contents into *root_data* (flat).

    Returns an error message if any collision is detected; ``None`` on
    success.  The move is atomic-ish: if a collision is found, nothing
    is moved.
    """
    root_data.mkdir(parents=True, exist_ok=True)
    items = list(src_dir.iterdir())
    for item in items:
        if (root_data / item.name).exists():
            return f"Collision: {root_data / item.name!s} already exists."
    for item in items:
        shutil.move(str(item), str(root_data / item.name))
    src_dir.rmdir()
    return None


def _rename_to_nboutput(src_dir: Path) -> None:
    """Rename *src_dir* to ``nboutput/`` within its parent."""
    target = src_dir.parent / "nboutput"
    src_dir.rename(target)


# ---------------------------------------------------------------------------
# Prompt-and-act for a data-like directory
# ---------------------------------------------------------------------------

_DATA_RELOCATION_CHOICES = {
    "1": "Move into root data/<subproject-name>/",
    "2": "Merge into root data/ (flat)",
    "3": "Keep as nboutput/ (rename in place)",
    "4": "Skip (leave as-is)",
}


def _prompt_data_relocation(
    dir_label: str,
    sp_dir: Path,
    sp_name: str,
    project_root: Path,
) -> None:
    """Interactively prompt the user about relocating *dir_label* in *sp_dir*.

    *dir_label* is ``"data"`` or ``"user_data"``.  The root shared data
    directory is always ``<project_root>/data/``.
    """
    src_dir = sp_dir / dir_label
    if not src_dir.is_dir():
        return

    click.echo(f"\n  [{sp_name}] Found {dir_label}/ with {sum(1 for _ in src_dir.iterdir())} item(s).")
    click.echo("  Options:")
    for key, desc in _DATA_RELOCATION_CHOICES.items():
        click.echo(f"    {key}) {desc}")

    choice = click.prompt(
        "  Choice",
        type=click.Choice(list(_DATA_RELOCATION_CHOICES.keys())),
        default="4",
    )

    root_data = project_root / "data"

    if choice == "1":
        _move_namespaced(src_dir, root_data, sp_name)
        click.echo(f"  Moved {dir_label}/ -> data/{sp_name}/")
    elif choice == "2":
        err = _merge_flat(src_dir, root_data)
        if err:
            click.echo(f"  Warning: {err} Skipping.", err=True)
        else:
            click.echo(f"  Merged {dir_label}/ -> data/")
    elif choice == "3":
        _rename_to_nboutput(src_dir)
        click.echo(f"  Renamed {dir_label}/ -> nboutput/")
    else:
        click.echo(f"  Skipped {dir_label}/.")


# ---------------------------------------------------------------------------
# Prompt-and-act for references.md
# ---------------------------------------------------------------------------


def _prompt_references(sp_dir: Path, sp_name: str) -> None:
    """Interactively prompt the user about ``references.md`` in *sp_dir*."""
    ref_path = sp_dir / "references.md"
    if not ref_path.exists():
        return

    click.echo(f"\n  [{sp_name}] Found references.md.")
    click.echo("  Options:")
    click.echo("    1) Convert to literature/index.md (preserves content)")
    click.echo("    2) Keep as-is")
    click.echo("    3) Delete")

    choice = click.prompt(
        "  Choice",
        type=click.Choice(["1", "2", "3"]),
        default="2",
    )

    if choice == "1":
        lit_dir = sp_dir / "literature"
        lit_dir.mkdir(exist_ok=True)
        index_path = lit_dir / "index.md"
        index_path.write_text(ref_path.read_text(encoding="utf-8"), encoding="utf-8")
        ref_path.unlink()
        click.echo(f"  Converted references.md -> literature/index.md")
    elif choice == "3":
        ref_path.unlink()
        click.echo(f"  Deleted references.md.")
    else:
        click.echo("  Kept references.md as-is.")


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command(name="migrate")
@click.pass_context
def migrate_cmd(ctx: click.Context) -> None:
    """Retrofit a bootstrapped repo onto KBUtilLib 2.0 layout.

    Walks each subproject and interactively proposes layout changes.
    No file is moved or deleted without explicit user confirmation.
    """
    project_root = _find_project_root(Path.cwd())

    # ── 1. Pre-flight: must be bootstrapped ─────────────────────────────────
    if not (project_root / "kbu-project.toml").exists():
        click.echo(
            "Error: not inside a kbu-bootstrapped project "
            "(no kbu-project.toml found in any parent directory).",
            err=True,
        )
        ctx.exit(1)
        return

    click.echo(f"kbu migrate — project root: {project_root}")

    # ── 2. Ensure [layout.shared_dirs] in kbu-project.toml ──────────────────
    modified = _add_layout_shared_dirs(project_root)
    if modified:
        click.echo(
            f"  Added [layout] shared_dirs = {list(_layout.DEFAULT_SHARED_DIRS)!r} "
            "to kbu-project.toml."
        )
    else:
        click.echo("  [layout.shared_dirs] already present — no change.")

    # ── 3. Create missing shared dirs ───────────────────────────────────────
    shared_dirs = _layout.read_shared_dirs(project_root)
    for d in shared_dirs:
        dir_path = project_root / d
        if not dir_path.exists():
            dir_path.mkdir(parents=True, exist_ok=True)
            (dir_path / ".gitkeep").touch()
            click.echo(f"  Created {d}/.gitkeep")
        else:
            click.echo(f"  {d}/ already exists — no change.")

    # ── 4. Walk subprojects ──────────────────────────────────────────────────
    sp_names = _list_subproject_names(project_root)
    if not sp_names:
        click.echo("  No subprojects found.")
    else:
        click.echo(f"\nProcessing {len(sp_names)} subproject(s)...")

    for sp_name in sp_names:
        sp_dir = project_root / "subprojects" / sp_name
        click.echo(f"\nSubproject: {sp_name}")

        # 4a/b. data/ and user_data/ relocation
        _prompt_data_relocation("data", sp_dir, sp_name, project_root)
        _prompt_data_relocation("user_data", sp_dir, sp_name, project_root)

        # 4c. references.md
        _prompt_references(sp_dir, sp_name)

        # 4d. Ensure .cache/ and literature/ exist
        for scaffolding_dir in [".cache", "literature"]:
            d_path = sp_dir / scaffolding_dir
            if not d_path.exists():
                d_path.mkdir(parents=True, exist_ok=True)
                click.echo(f"  Created {sp_name}/{scaffolding_dir}/")

        # 5. Append per-subproject gitignore block (idempotent)
        _append_subproject_gitignore(project_root, sp_name)

    click.echo("\nMigration complete.")
