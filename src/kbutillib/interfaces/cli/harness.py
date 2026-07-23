"""``kbu harness`` — per-project local programmatic-execution harness CLI.

Subcommands
-----------
kbu harness init <BERIL_ROOT> <project-id> [--harness-root PATH] [--force]
    Scaffold a harness repo (git init, venv, skill bundle, initial pull).

kbu harness pull [--dry-run] [--force] [--exclude-kbcache]
    rsync BERIL project → harness.

kbu harness push [--dry-run] [--force] [--exclude-kbcache]
    rsync harness → BERIL project.

kbu harness run [notebooks…] [--on local|h100] [--json] [--h100-inbox PATH]
    Execute notebooks via nbconvert --execute --inplace.

kbu harness doctor
    Health check: venv, import kbutillib, harness.toml, nbconvert.

kbu harness status
    Show harness.toml fields + last DEVLOG entry.

Output shape: per-step ``── name``, ✓/✗ result lines, ``Summary:`` block,
return codes 0 all-ok / 1 partial / 2 none (CRAFT / kbu beril convention).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click


# ---------------------------------------------------------------------------
# helpers shared across subcommands
# ---------------------------------------------------------------------------


def _echo(msg: str, *, err: bool = True) -> None:
    click.echo(msg, err=err)


def _find_harness_dir() -> Optional[Path]:
    """Search upward from CWD for harness.toml; return dir or None."""
    from kbutillib.harness.config import find_harness_toml
    return find_harness_toml()


def _require_harness_dir() -> Path:
    """Return harness dir or exit 2 with a clear message."""
    d = _find_harness_dir()
    if d is None:
        click.echo(
            "✗ No harness.toml found searching upward from current directory.\n"
            "  Run `kbu harness init` first, or cd into the harness directory.",
            err=True,
        )
        sys.exit(2)
    return d


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@click.command("init")
@click.argument("beril_root", metavar="BERIL_ROOT")
@click.argument("project_id", metavar="PROJECT_ID")
@click.option(
    "--harness-root",
    default=None,
    metavar="PATH",
    help="Root directory that will contain the harness (default: ~/Dropbox/Projects/kbu-harness/).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite an existing non-empty harness directory.",
)
def init_cmd(
    beril_root: str, project_id: str, harness_root: Optional[str], force: bool
) -> None:
    """Scaffold a per-project harness at <harness-root>/<project-id>/.

    Validates BERIL_ROOT (PROJECT.md + .claude/skills/), creates a git repo,
    builds a venv, copies the kbu-run skill bundle, and does an initial pull.
    """
    from kbutillib.harness.scaffold import init_harness

    br = Path(beril_root).expanduser().resolve()
    hr = Path(harness_root).expanduser() if harness_root else None

    _echo(f"kbu harness init")
    _echo(f"BERIL_ROOT: {br}")
    _echo(f"project-id: {project_id}")
    _echo("")

    ok, detail = init_harness(
        beril_root=br,
        project_id=project_id,
        harness_root=hr,
        force=force,
        echo=lambda msg: _echo(msg),
    )

    _echo("")
    if ok:
        _echo(f"── Summary:")
        _echo(f"   ✓ Harness created: {detail}")
        sys.exit(0)
    else:
        _echo(f"── Summary:")
        _echo(f"   ✗ Init failed: {detail}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------


@click.command("pull")
@click.option("--dry-run", is_flag=True, default=False, help="Print planned transfer; copy nothing.")
@click.option("--force", is_flag=True, default=False, help="Pull even with uncommitted harness changes.")
@click.option("--exclude-kbcache", is_flag=True, default=False, help="Exclude .kbcache/ from sync.")
def pull_cmd(dry_run: bool, force: bool, exclude_kbcache: bool) -> None:
    """rsync <BERIL_ROOT>/projects/<id>/ → harness."""
    from kbutillib.harness.sync import pull

    harness_dir = _require_harness_dir()
    _echo("── pull")

    ok, detail = pull(
        harness_dir,
        dry_run=dry_run,
        force=force,
        exclude_kbcache=exclude_kbcache,
        echo=lambda msg: _echo(msg),
    )

    _echo("")
    _echo("── Summary:")
    if ok:
        _echo("   ✓ pull complete")
        sys.exit(0)
    else:
        _echo(f"   ✗ pull failed: {detail}")
        sys.exit(1 if "rsync" in detail.lower() or "uncommitted" in detail.lower() else 2)


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


@click.command("push")
@click.option("--dry-run", is_flag=True, default=False, help="Print planned transfer; copy nothing.")
@click.option("--force", is_flag=True, default=False, help="Push even if BERIL has incoming changes.")
@click.option("--exclude-kbcache", is_flag=True, default=False, help="Exclude .kbcache/ from sync.")
def push_cmd(dry_run: bool, force: bool, exclude_kbcache: bool) -> None:
    """rsync harness → <BERIL_ROOT>/projects/<id>/."""
    from kbutillib.harness.sync import push

    harness_dir = _require_harness_dir()
    _echo("── push")

    ok, detail = push(
        harness_dir,
        dry_run=dry_run,
        force=force,
        exclude_kbcache=exclude_kbcache,
        echo=lambda msg: _echo(msg),
    )

    _echo("")
    _echo("── Summary:")
    if ok:
        _echo("   ✓ push complete")
        sys.exit(0)
    else:
        _echo(f"   ✗ push failed: {detail}")
        # 2 = source missing, 1 = partial/rsync error
        sys.exit(2 if "not found" in detail.lower() else 1)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@click.command("run")
@click.argument("notebooks", nargs=-1, metavar="[NOTEBOOK]...")
@click.option(
    "--on",
    "on_target",
    default="local",
    type=click.Choice(["local", "h100"]),
    help="Where to run: local (default) or h100 (dispatch via ai-cowork).",
)
@click.option("--json", "output_json", is_flag=True, default=False, help="Print JSON result.")
@click.option(
    "--h100-inbox",
    default=None,
    metavar="PATH",
    help="Override h100 inbox path (default: ~/Dropbox/Projects/AIAssistant/cowork-inbox/h100/).",
)
def run_cmd(
    notebooks: tuple[str, ...],
    on_target: str,
    output_json: bool,
    h100_inbox: Optional[str],
) -> None:
    """Execute notebooks via nbconvert --execute --inplace.

    With no notebooks specified, runs all notebooks/*.ipynb in lexicographic order.
    Stops at the first failure.
    """
    from kbutillib.harness.runner import RunResult, discover_notebooks, run_notebooks
    from kbutillib.harness.devlog import append_entry
    import time

    harness_dir = _require_harness_dir()
    _echo("── run")

    # Resolve notebook paths
    nb_paths: Optional[list[Path]] = None
    if notebooks:
        nb_paths = [Path(nb) for nb in notebooks]
        # Make absolute if relative
        nb_paths = [
            (harness_dir / nb).resolve() if not nb.is_absolute() else nb
            for nb in nb_paths
        ]

    t0 = time.monotonic()
    try:
        results, overall = run_notebooks(
            harness_dir,
            notebooks=nb_paths,
            on=on_target,
            h100_inbox_override=h100_inbox,
            echo=lambda msg: _echo(msg),
        )
    except RuntimeError as exc:
        _echo(f"   ✗ {exc}")
        sys.exit(1)

    runtime_total = time.monotonic() - t0

    if overall == "none-matched":
        _echo("✗ no notebooks matched in notebooks/")
        sys.exit(2)

    if overall == "dispatched":
        _echo("")
        _echo("── Summary:")
        _echo("   ✓ dispatched to h100")
        sys.exit(0)

    # Append devlog entry for local runs
    if on_target == "local" and results:
        try:
            devlog = harness_dir / "DEVLOG.md"
            nb_names = [r.notebook for r in results]
            append_entry(
                devlog_path=devlog,
                action="run",
                notebooks=nb_names,
                scope="full",
                where="local",
                outcome="ok" if overall == "ok" else "failed",
                runtime_s=runtime_total,
                traceback=results[-1].error if results and not results[-1].executed else None,
            )
        except Exception as exc:  # noqa: BLE001
            _echo(f"   (devlog write failed: {exc})")

    if output_json:
        data = {
            "results": [r.to_dict() for r in results],
            "overall_status": overall,
        }
        click.echo(json.dumps(data), err=False)

    _echo("")
    _echo("── Summary:")
    n_ok = sum(1 for r in results if r.executed and r.outputs_present)
    n_total = len(results)
    _echo(f"   {n_ok}/{n_total} notebooks executed with outputs")

    if overall == "ok":
        sys.exit(0)
    elif overall == "partial":
        sys.exit(1)
    else:
        sys.exit(1)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@click.command("doctor")
def doctor_cmd() -> None:
    """Health check: venv, import kbutillib, harness.toml, nbconvert.

    Exits 0 only when all checks pass.
    """
    from kbutillib.harness.config import find_harness_toml, load_config

    harness_dir = _require_harness_dir()
    _echo("── kbu harness doctor")
    _echo(f"   harness: {harness_dir}")
    _echo("")

    n_ok = 0
    n_fail = 0
    n_total = 5  # total possible checks

    # ── Check 1: harness.toml valid ─────────────────────────────────────────
    _echo("── harness.toml")
    try:
        cfg = load_config(harness_dir)
        _echo(f"   ✓ harness.toml valid (project_id={cfg.project_id})")
        n_ok += 1
        cfg_ok = True
    except Exception as exc:  # noqa: BLE001
        _echo(f"   ✗ harness.toml invalid or missing: {exc}")
        n_fail += 1
        cfg_ok = False
        cfg = None  # type: ignore[assignment]

    # ── Check 2: beril_root exists ──────────────────────────────────────────
    _echo("")
    _echo("── beril_root")
    if cfg_ok and cfg is not None:
        br = Path(cfg.beril_root)
        if br.is_dir():
            _echo(f"   ✓ beril_root exists: {br}")
            n_ok += 1
        else:
            _echo(f"   ✗ beril_root not found: {br}")
            n_fail += 1
    else:
        _echo("   ✗ cannot check beril_root (harness.toml invalid)")
        n_fail += 1

    # ── Check 3: venv present + python path ─────────────────────────────────
    _echo("")
    _echo("── venv / interpreter")
    interpreter: Optional[str] = None
    if cfg_ok and cfg is not None:
        # Check python field
        if cfg.python:
            py_path = Path(cfg.python)
            if py_path.is_file():
                _echo(f"   ✓ python field present and file exists: {py_path}")
                n_ok += 1
                interpreter = str(py_path)
            else:
                _echo(f"   ✗ python field set but file not found: {py_path}")
                n_fail += 1
        else:
            _echo("   ✗ python field missing from harness.toml")
            n_fail += 1
            # Try fallback
            fallback = harness_dir / ".venv" / "bin" / "python"
            if fallback.is_file():
                interpreter = str(fallback)
    else:
        venv_python = harness_dir / ".venv" / "bin" / "python"
        if venv_python.is_file():
            _echo(f"   ✓ .venv/bin/python present: {venv_python}")
            n_ok += 1
            interpreter = str(venv_python)
        else:
            _echo(f"   ✗ .venv not found at {harness_dir / '.venv'}")
            n_fail += 1

    # ── Check 4: import kbutillib ────────────────────────────────────────────
    _echo("")
    _echo("── import kbutillib")
    if interpreter:
        result = subprocess.run(
            [interpreter, "-c", "import kbutillib"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            _echo(f"   ✓ import kbutillib succeeded under {interpreter}")
            n_ok += 1
        else:
            _echo(f"   ✗ import kbutillib FAILED under {interpreter}")
            n_fail += 1
    else:
        _echo("   ✗ cannot check import (no venv interpreter found)")
        n_fail += 1

    # ── Check 5: nbconvert importable ───────────────────────────────────────
    _echo("")
    _echo("── nbconvert")
    if interpreter:
        result = subprocess.run(
            [interpreter, "-c", "import nbconvert"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            _echo(f"   ✓ nbconvert importable under {interpreter}")
            n_ok += 1
        else:
            _echo(f"   ✗ nbconvert not importable under {interpreter}")
            n_fail += 1
    else:
        _echo("   ✗ cannot check nbconvert (no venv interpreter found)")
        n_fail += 1

    # ── Summary ──────────────────────────────────────────────────────────────
    _echo("")
    _echo("kbu harness doctor summary:")
    _echo(f"  Checks OK: {n_ok}/{n_total}")
    _echo(f"  Checks FAIL: {n_fail}")

    sys.exit(0 if n_fail == 0 else 1)


# ---------------------------------------------------------------------------
# status (optional, low-cost)
# ---------------------------------------------------------------------------


@click.command("status")
def status_cmd() -> None:
    """Print harness.toml fields and the last DEVLOG entry."""
    from kbutillib.harness.config import load_config

    harness_dir = _require_harness_dir()
    try:
        cfg = load_config(harness_dir)
    except Exception as exc:  # noqa: BLE001
        _echo(f"✗ Cannot read harness.toml: {exc}")
        sys.exit(1)

    _echo("── harness.toml")
    _echo(f"   project_id:        {cfg.project_id}")
    _echo(f"   beril_root:        {cfg.beril_root}")
    _echo(f"   harness_root:      {cfg.harness_root}")
    _echo(f"   created_at:        {cfg.created_at}")
    _echo(f"   kbutillib_version: {cfg.kbutillib_version}")
    if cfg.python:
        _echo(f"   python:            {cfg.python}")

    devlog = harness_dir / "DEVLOG.md"
    if devlog.is_file():
        text = devlog.read_text(encoding="utf-8")
        # Find last ## entry
        entries = [
            line
            for line in text.splitlines()
            if line.startswith("## ")
        ]
        if entries:
            _echo("")
            _echo("── last DEVLOG entry")
            _echo(f"   {entries[-1]}")
    sys.exit(0)


# ---------------------------------------------------------------------------
# harness group
# ---------------------------------------------------------------------------


@click.group("harness")
def harness_cmd() -> None:
    """Per-project local programmatic-execution harness for BERIL modeling projects."""


harness_cmd.add_command(init_cmd, name="init")
harness_cmd.add_command(pull_cmd, name="pull")
harness_cmd.add_command(push_cmd, name="push")
harness_cmd.add_command(run_cmd, name="run")
harness_cmd.add_command(doctor_cmd, name="doctor")
harness_cmd.add_command(status_cmd, name="status")
