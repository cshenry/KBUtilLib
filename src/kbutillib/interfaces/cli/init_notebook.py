"""``kbu init-notebook`` — bootstrap a notebook project with a venv and templates."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click
import jinja2

from .machine import load_machine_config, resolve_alias

logger = logging.getLogger(__name__)

_MARKER = "# === project-specific helpers below ==="


def _slugify(name: str) -> str:
    """Lowercase, replace non-alphanum with hyphens, collapse runs, strip edges."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)


def _template_dir() -> Path:
    return Path(__file__).resolve().parent / "templates"


def _render_util_template(project_name: str) -> str:
    """Render ``util.py.tmpl`` with the given project name."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_template_dir())),
        keep_trailing_newline=True,
    )
    tmpl = env.get_template("util.py.tmpl")
    return tmpl.render(project_name=project_name)


def _smart_merge_util(existing_content: str, new_header: str) -> Optional[str]:
    """Replace the header above the marker in *existing_content* with *new_header*.

    Returns the merged content, or None if the marker is not found.
    """
    if _MARKER not in existing_content:
        return None
    if _MARKER not in new_header:
        return None

    # Split existing: keep everything from the marker line onward
    idx = existing_content.index(_MARKER)
    below_marker = existing_content[idx + len(_MARKER) :]

    # Split new header: take everything up to and including the marker
    new_idx = new_header.index(_MARKER)
    header_part = new_header[: new_idx + len(_MARKER)]

    return header_part + below_marker


def _venv_path(project_name: str, python_ver: str) -> Path:
    """Standard venv location."""
    return Path("~/VirtualEnvironments").expanduser() / f"kbu.nb-{project_name}-py{python_ver}"


def _check_venvman() -> None:
    """Abort if venvman is not on PATH."""
    if shutil.which("venvman") is None:
        raise click.ClickException(
            "venvman is not installed or not on PATH.\n"
            "Install it with:  pip install venvman\n"
            "Or see: https://github.com/cshenry/EnvironmentManager"
        )


def _run_subprocess(cmd: list[str], label: str) -> None:
    """Run a subprocess and log it; raise on failure."""
    click.echo(f"  -> {label}: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise click.ClickException(f"{label} failed (exit {result.returncode}):\n{stderr}")


@click.command("init-notebook")
@click.option(
    "--project",
    default=None,
    help="Project name (default: current directory basename).",
)
@click.option(
    "--python",
    "python_ver",
    default=None,
    help="Python version for the venv (default: from machine config).",
)
@click.option(
    "--alias",
    default=None,
    help="Override machine alias resolution.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite util.py header and force-pin all notebook kernels.",
)
@click.option(
    "--no-pin-kernels",
    is_flag=True,
    default=False,
    help="Skip Jupyter kernel registration and .ipynb metadata pinning.",
)
@click.option(
    "--no-venv",
    is_flag=True,
    default=False,
    help="Skip venv creation; only generate template files.",
)
def init_notebook_cmd(
    project: Optional[str],
    python_ver: Optional[str],
    alias: Optional[str],
    force: bool,
    no_pin_kernels: bool,
    no_venv: bool,
) -> None:
    """Bootstrap a notebook project with a per-project venv and templates."""
    cwd = Path.cwd()

    # -------------------------------------------------------------------
    # 1. Resolve project name
    # -------------------------------------------------------------------
    raw_name = project or cwd.name
    project_name = _slugify(raw_name)
    if project_name != raw_name:
        click.echo(f"  [warn] Project name slugified: {raw_name!r} -> {project_name!r}")

    click.echo(f"Initializing notebook project: {project_name}")

    # -------------------------------------------------------------------
    # 2. Resolve alias -> load merged machine config
    # -------------------------------------------------------------------
    resolved_alias = alias or resolve_alias(prompt_fallback=not no_venv)
    click.echo(f"  Machine alias: {resolved_alias}")
    config = load_machine_config(resolved_alias)

    if python_ver is None:
        python_ver = config.get("default_python", "3.12")
    click.echo(f"  Python version: {python_ver}")

    editable_installs: list[str] = config.get("editable_installs", [])
    notebook_deps: list[str] = config.get("notebook_deps", [])

    # -------------------------------------------------------------------
    # 3. Pre-flight checks
    # -------------------------------------------------------------------
    notebooks_dir = cwd / "notebooks"
    if not notebooks_dir.exists():
        notebooks_dir.mkdir(parents=True)
        click.echo(f"  Created: {notebooks_dir}")

    # Check editable install paths
    valid_editables: list[Path] = []
    for dep_str in editable_installs:
        dep_path = Path(dep_str).expanduser()
        if dep_path.exists():
            valid_editables.append(dep_path)
        else:
            click.echo(f"  [warn] Editable install path missing, skipping: {dep_path}")

    venv = _venv_path(project_name, python_ver)
    venv_python = venv / "bin" / "python"
    venv_pip = venv / "bin" / "pip"

    created: list[str] = []
    skipped: list[str] = []
    overwritten: list[str] = []

    # -------------------------------------------------------------------
    # 4. Create venv via venvman
    # -------------------------------------------------------------------
    if not no_venv:
        _check_venvman()

        # Check for broken venv
        if venv.exists() and not venv_python.exists():
            raise click.ClickException(
                f"Broken venv detected: {venv} exists but {venv_python} is missing.\n"
                f"Fix with: venvman destroy kbu.nb-{project_name}"
            )

        _run_subprocess(
            [
                "venvman",
                "create",
                "--project",
                f"kbu.nb-{project_name}",
                "--dir",
                str(cwd),
                "--python",
                python_ver,
            ],
            "venvman create",
        )
        created.append("venv")

        # -------------------------------------------------------------------
        # 5. Editable installs
        # -------------------------------------------------------------------
        for dep_path in valid_editables:
            _run_subprocess(
                [str(venv_pip), "install", "-e", str(dep_path)],
                f"pip install -e {dep_path.name}",
            )

        # -------------------------------------------------------------------
        # 6. Notebook dependencies
        # -------------------------------------------------------------------
        if notebook_deps:
            _run_subprocess(
                [str(venv_pip), "install"] + notebook_deps,
                "pip install notebook deps",
            )

    # -------------------------------------------------------------------
    # 7. Render notebooks/util.py
    # -------------------------------------------------------------------
    util_path = notebooks_dir / "util.py"
    rendered = _render_util_template(project_name)

    if not util_path.exists():
        util_path.write_text(rendered)
        created.append("notebooks/util.py")
    elif force:
        existing = util_path.read_text()
        merged = _smart_merge_util(existing, rendered)
        if merged is not None:
            util_path.write_text(merged)
            overwritten.append("notebooks/util.py (header updated, custom code preserved)")
        else:
            # Marker not found in existing file — refuse
            raise click.ClickException(
                f"Cannot smart-merge {util_path}: the marker line\n"
                f"  {_MARKER}\n"
                "is missing from the existing file. Add the marker manually to "
                "separate the generated header from project-specific code, then retry."
            )
    else:
        click.echo(f"  Kept existing: {util_path}")
        skipped.append("notebooks/util.py")

    # -------------------------------------------------------------------
    # 8. activate.sh
    # -------------------------------------------------------------------
    activate_path = cwd / "activate.sh"
    if no_venv and not activate_path.exists():
        # Write a minimal activate.sh pointing to the expected venv
        activate_content = (
            f"# Activate kbu.nb-{project_name} venv\n"
            f'source "{venv}/bin/activate"\n'
        )
        activate_path.write_text(activate_content)
        created.append("activate.sh")
    elif activate_path.exists():
        skipped.append("activate.sh")
    else:
        # venvman wrote it
        created.append("activate.sh (via venvman)")

    # -------------------------------------------------------------------
    # 9. Kernel registration + notebook pinning
    # -------------------------------------------------------------------
    if not no_pin_kernels and not no_venv:
        kernel_name = f"kbu.nb-{project_name}"
        display_name = f"KBU: {project_name}"

        _run_subprocess(
            [
                str(venv_python),
                "-m",
                "ipykernel",
                "install",
                "--user",
                "--name",
                kernel_name,
                "--display-name",
                display_name,
            ],
            "ipykernel install",
        )
        created.append(f"Jupyter kernel: {kernel_name}")

        # Pin .ipynb files
        _conflict_pat = re.compile(r"\(Conflict")
        nb_files = sorted(notebooks_dir.glob("*.ipynb"))
        for nb_path in nb_files:
            if _conflict_pat.search(nb_path.name):
                continue
            try:
                import nbformat

                nb = nbformat.read(str(nb_path), as_version=4)
                current_kernel = nb.metadata.get("kernelspec", {}).get("name", "")

                should_pin = (
                    force
                    or not current_kernel
                    or current_kernel.startswith("kbu.nb-")
                )

                if should_pin:
                    nb.metadata["kernelspec"] = {
                        "name": kernel_name,
                        "display_name": display_name,
                        "language": "python",
                    }
                    nbformat.write(nb, str(nb_path))
                    overwritten.append(f"{nb_path.name} (kernel pinned)")
                else:
                    skipped.append(f"{nb_path.name} (non-kbu kernel: {current_kernel})")
            except Exception as exc:
                click.echo(f"  [warn] Could not pin kernel for {nb_path.name}: {exc}")

    # -------------------------------------------------------------------
    # 10. Summary
    # -------------------------------------------------------------------
    click.echo("")
    click.echo("=== init-notebook summary ===")
    if created:
        click.echo("  Created:")
        for item in created:
            click.echo(f"    + {item}")
    if overwritten:
        click.echo("  Overwritten:")
        for item in overwritten:
            click.echo(f"    ~ {item}")
    if skipped:
        click.echo("  Skipped:")
        for item in skipped:
            click.echo(f"    - {item}")

    click.echo("")
    click.echo("Next steps:")
    click.echo(f"  1. cd {cwd}")
    click.echo(f"  2. source activate.sh")
    click.echo(f"  3. jupyter lab")
