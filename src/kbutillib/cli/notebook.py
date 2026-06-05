"""``kbu notebook`` — notebook listing, mark-run, and execution.

Provides three subcommands:

- ``list [--json]``     — list notebooks across all subprojects with run status
- ``mark-run <path>``   — record ``last_run_at`` for a notebook in its manifest
- ``exec <path>``       — execute a notebook in-place via nbclient
"""

from __future__ import annotations

import json
import os
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import click
import nbformat

from .manifest import (
    append_notebook_entry_or_update,
    now_utc_iso,
    read_project_manifest,
    read_subproject_manifest,
)
from .subproject import _find_project_root


# ── constants ──────────────────────────────────────────────────────────────

_DEFAULT_CELL_TIMEOUT = 600
_MAX_CELL_OUTPUT_BYTES = 1024 * 1024  # 1 MiB
_OUTPUT_TRUNCATION_FOOTER = "\n[output truncated at 1 MiB]"


# ── helpers ────────────────────────────────────────────────────────────────


def _cell_timeout() -> int:
    """Return per-cell timeout in seconds.

    Reads ``KBU_NOTEBOOK_CELL_TIMEOUT`` env var; defaults to 600.
    """
    raw = os.environ.get("KBU_NOTEBOOK_CELL_TIMEOUT", "")
    if raw.strip():
        try:
            return int(raw.strip())
        except ValueError:
            click.echo(
                f"Warning: KBU_NOTEBOOK_CELL_TIMEOUT={raw!r} is not an integer; "
                f"using default {_DEFAULT_CELL_TIMEOUT}s.",
                err=True,
            )
    return _DEFAULT_CELL_TIMEOUT


def _utc_timestamp_for_backup() -> str:
    """Return a UTC timestamp string suitable for backup filenames.

    Example: ``"20260604T153000Z"``
    """
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _subproject_name_for_notebook(project_root: Path, nb_path: Path) -> Optional[str]:
    """Return the subproject name that owns *nb_path*, or ``None``.

    Resolves by checking whether *nb_path* lives inside
    ``<project_root>/subprojects/<name>/``.
    """
    sp_root = project_root / "subprojects"
    try:
        rel = nb_path.resolve().relative_to(sp_root.resolve())
    except ValueError:
        return None
    return rel.parts[0] if rel.parts else None


def _subproject_order(project_root: Path) -> list[str]:
    """Return subproject names sorted by ``created_at``, oldest first.

    Subprojects without a readable ``created_at`` are sorted alphabetically
    and placed at the end.
    """
    sp_root = project_root / "subprojects"
    if not sp_root.is_dir():
        return []

    names_with_ts: list[tuple[str, str]] = []
    for d in sp_root.iterdir():
        if not d.is_dir() or not (d / "kbu-subproject.toml").exists():
            continue
        try:
            data = read_subproject_manifest(project_root, d.name)
            ts = data.get("subproject", {}).get("created_at", "")
        except Exception:
            ts = ""
        names_with_ts.append((d.name, ts))

    # Sort: non-empty timestamps first (ascending), then empty timestamps
    # alphabetically.
    names_with_ts.sort(key=lambda t: (t[1] == "", t[1], t[0]))
    return [name for name, _ in names_with_ts]


def _notebook_rel_path(sp_dir: Path, nb_path: Path) -> str:
    """Return *nb_path* relative to *sp_dir* as a POSIX string."""
    try:
        return nb_path.relative_to(sp_dir).as_posix()
    except ValueError:
        return nb_path.name


# ── list_notebooks ─────────────────────────────────────────────────────────


def list_notebooks(project_root: Path) -> list[dict[str, Any]]:
    """Scan all subprojects for ``*.ipynb`` files and annotate with run status.

    For each notebook file found under ``subprojects/*/notebooks/*.ipynb``,
    looks up the matching ``[[notebooks]]`` entry in the subproject's
    ``kbu-subproject.toml`` to read ``last_run_at``.  Computes
    ``modified_since_run`` by comparing the notebook's mtime against
    ``last_run_at``.

    Returns a list of dicts with keys:
    - ``path``               — absolute path string
    - ``subproject``         — subproject name
    - ``last_run_at``        — ISO-8601 string or empty string
    - ``modified_since_run`` — bool (True if notebook mtime > last_run_at)

    Ordered by subproject creation time (oldest first) then notebook filename.
    """
    results: list[dict[str, Any]] = []

    for sp_name in _subproject_order(project_root):
        sp_dir = project_root / "subprojects" / sp_name
        nb_dir = sp_dir / "notebooks"
        if not nb_dir.is_dir():
            continue

        # Read recorded entries from manifest
        try:
            sp_data = read_subproject_manifest(project_root, sp_name)
        except Exception:
            sp_data = {}
        recorded: dict[str, dict[str, Any]] = {}
        for entry in sp_data.get("notebooks", []):
            rel = entry.get("path", "")
            if rel:
                recorded[rel] = entry

        # Enumerate notebooks sorted by filename
        nb_files = sorted(nb_dir.glob("*.ipynb"), key=lambda p: p.name)
        for nb_path in nb_files:
            rel_path = _notebook_rel_path(sp_dir, nb_path)
            entry = recorded.get(rel_path, {})
            last_run_at = entry.get("last_run_at", "")

            # Compute modified_since_run
            if last_run_at:
                try:
                    mtime = nb_path.stat().st_mtime
                    # Parse last_run_at (ISO-8601 with Z suffix)
                    run_dt = datetime.fromisoformat(
                        last_run_at.replace("Z", "+00:00")
                    )
                    run_ts = run_dt.timestamp()
                    modified_since_run = mtime > run_ts
                except Exception:
                    modified_since_run = True
            else:
                modified_since_run = True

            results.append({
                "path": str(nb_path),
                "subproject": sp_name,
                "last_run_at": last_run_at,
                "modified_since_run": modified_since_run,
            })

    return results


# ── mark_run ───────────────────────────────────────────────────────────────


def mark_run(path: Path) -> None:
    """Record ``last_run_at = now`` for the notebook at *path* in its manifest.

    Resolves the subproject that owns *path* by walking up from *path*'s
    directory to find the ``kbu-project.toml``.  The notebook path stored in
    the manifest is relative to the subproject directory.

    Raises ``ValueError`` if *path* cannot be resolved to a subproject.
    """
    project_root = _find_project_root(path.parent)
    sp_name = _subproject_name_for_notebook(project_root, path)
    if sp_name is None:
        raise ValueError(
            f"Cannot determine subproject for notebook: {path}\n"
            "Ensure the notebook is inside subprojects/<name>/notebooks/."
        )
    sp_dir = project_root / "subprojects" / sp_name
    rel_path = _notebook_rel_path(sp_dir, path)
    ts = now_utc_iso()
    append_notebook_entry_or_update(project_root, sp_name, rel_path, ts)


# ── exec_notebook ──────────────────────────────────────────────────────────


def _select_kernel(project_root: Path) -> str:
    """Return the best kernel name for this project.

    Reads ``[project].name`` from ``kbu-project.toml`` and checks whether
    a kernel spec with that name exists via ``jupyter_client``.  Falls back
    to ``"python3"`` with a warning to stderr if not found.
    """
    from jupyter_client.kernelspec import find_kernel_specs  # noqa: PLC0415

    project_name: str = ""
    try:
        manifest = read_project_manifest(project_root)
        project_name = manifest.get("project", {}).get("name", "")
    except Exception:
        pass

    available = find_kernel_specs()

    if project_name and project_name in available:
        return project_name

    if "python3" in available:
        if project_name:
            click.echo(
                f"Warning: kernel '{project_name}' not found; "
                "falling back to 'python3'.",
                err=True,
            )
        return "python3"

    # Last resort: first available kernel
    if available:
        first = next(iter(available))
        click.echo(
            f"Warning: neither '{project_name}' nor 'python3' found; "
            f"using '{first}'.",
            err=True,
        )
        return first

    # No kernels at all — let nbclient's default handle it
    click.echo(
        "Warning: no kernel specs found; nbclient will use its default.",
        err=True,
    )
    return ""


def _truncate_outputs(nb: Any) -> None:
    """Truncate cell stream outputs exceeding 1 MiB in-place.

    Appends a footer line to indicate truncation.  Operates on the
    ``outputs`` list of each code cell.
    """
    for cell in nb.cells:
        if cell.cell_type != "code":
            continue
        for output in cell.get("outputs", []):
            if output.get("output_type") not in ("stream", "execute_result", "display_data"):
                continue
            # Handle stream text
            text = output.get("text", None)
            if isinstance(text, str):
                encoded = text.encode("utf-8", errors="replace")
                if len(encoded) > _MAX_CELL_OUTPUT_BYTES:
                    truncated = encoded[:_MAX_CELL_OUTPUT_BYTES].decode("utf-8", errors="replace")
                    output["text"] = truncated + _OUTPUT_TRUNCATION_FOOTER
            # Handle data (display_data / execute_result)
            data = output.get("data", {})
            for mime, content in list(data.items()):
                if isinstance(content, str):
                    encoded = content.encode("utf-8", errors="replace")
                    if len(encoded) > _MAX_CELL_OUTPUT_BYTES:
                        truncated = encoded[:_MAX_CELL_OUTPUT_BYTES].decode("utf-8", errors="replace")
                        data[mime] = truncated + _OUTPUT_TRUNCATION_FOOTER


def exec_notebook(path: Path, allow_errors: bool = False) -> None:
    """Execute the notebook at *path* in-place.

    Steps:
    1. Creates a backup at ``<path>.bak.<UTC-timestamp>.ipynb``.
    2. Selects a kernel (project name → python3 → first available).
    3. Executes via ``nbclient.NotebookClient`` with per-cell timeout from
       ``KBU_NOTEBOOK_CELL_TIMEOUT`` (default 600s).
    4. Truncates stream outputs exceeding 1 MiB per cell.
    5. Writes the executed notebook back to *path*.

    With ``allow_errors=False`` (default), raises
    ``nbclient.exceptions.CellExecutionError`` on cell errors and
    ``nbclient.exceptions.CellTimeoutError`` on timeout.
    With ``allow_errors=True``, continues past cell errors.

    Raises:
        nbclient.exceptions.CellExecutionError: if a cell raises and
            *allow_errors* is False.
        nbclient.exceptions.CellTimeoutError: if a cell exceeds the timeout.
        FileNotFoundError: if *path* does not exist.
    """
    import nbclient  # noqa: PLC0415

    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"Notebook not found: {path}")

    # 1. Backup
    ts = _utc_timestamp_for_backup()
    backup_path = path.with_suffix(f".bak.{ts}.ipynb")
    import shutil  # noqa: PLC0415
    shutil.copy2(path, backup_path)

    # 2. Read
    with open(path, encoding="utf-8") as fh:
        nb = nbformat.read(fh, as_version=4)

    # 3. Kernel selection
    project_root = _find_project_root(path.parent)
    kernel_name = _select_kernel(project_root)

    # 4. Execute
    timeout = _cell_timeout()
    client = nbclient.NotebookClient(
        nb,
        timeout=timeout,
        kernel_name=kernel_name,
        allow_errors=allow_errors,
    )

    client.execute()

    # 5. Truncate outputs
    _truncate_outputs(nb)

    # 6. Write back
    with open(path, "w", encoding="utf-8") as fh:
        nbformat.write(nb, fh)


# ── Click group ────────────────────────────────────────────────────────────


@click.group(name="notebook")
def notebook_cmd() -> None:
    """List, mark, and execute notebooks in kbu subprojects."""


# ── list ───────────────────────────────────────────────────────────────────


@notebook_cmd.command(name="list")
@click.option("--json", "output_json", is_flag=True, default=False,
              help="Output full records as a JSON array.")
@click.pass_context
def list_cmd(ctx: click.Context, output_json: bool) -> None:
    """List all notebooks across subprojects with run status."""
    project_root = _find_project_root(Path.cwd())
    records = list_notebooks(project_root)

    if output_json:
        click.echo(json.dumps(records))
        return

    # TSV output
    click.echo("path\tsubproject\tlast_run_at\tmodified_since_run")
    for rec in records:
        modified = "true" if rec["modified_since_run"] else "false"
        click.echo(
            f"{rec['path']}\t{rec['subproject']}\t{rec['last_run_at']}\t{modified}"
        )


# ── mark-run ───────────────────────────────────────────────────────────────


@notebook_cmd.command(name="mark-run")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def mark_run_cmd(ctx: click.Context, path: Path) -> None:
    """Record last_run_at for PATH in the subproject manifest."""
    try:
        mark_run(path.resolve())
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        ctx.exit(1)
        return
    click.echo(f"Marked {path} as run.")


# ── exec ───────────────────────────────────────────────────────────────────


@notebook_cmd.command(name="exec")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--allow-errors", is_flag=True, default=False,
              help="Continue execution past cell errors.")
@click.pass_context
def exec_cmd(ctx: click.Context, path: Path, allow_errors: bool) -> None:
    """Execute the notebook at PATH in-place.

    Creates a backup, runs all cells, writes results back, and records
    last_run_at in the subproject manifest on success.
    """
    from nbclient.exceptions import CellExecutionError, CellTimeoutError  # noqa: PLC0415

    nb_path = path.resolve()
    try:
        exec_notebook(nb_path, allow_errors=allow_errors)
    except CellExecutionError as exc:
        click.echo(f"Error: cell execution failed:\n{exc}", err=True)
        ctx.exit(1)
        return
    except CellTimeoutError as exc:
        click.echo(f"Error: cell timed out:\n{exc}", err=True)
        ctx.exit(1)
        return
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        ctx.exit(1)
        return

    # Auto-mark run on success
    try:
        mark_run(nb_path)
    except ValueError as exc:
        click.echo(
            f"Warning: notebook executed but could not update manifest: {exc}",
            err=True,
        )

    click.echo(f"Executed {path} successfully.")
