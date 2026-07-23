"""``kbu beril`` — BERIL-root deployer and worktree manager for KBUtilLib.

Subcommands
-----------
kbu beril install <BERIL_ROOT> [--dry-run]
    Copy the three KBUtilLib skill dirs into a BERIL deployment, render
    preferences.md (if absent), discover a Python interpreter, record it
    in install.json, and pip-install/upgrade KBUtilLib into that interpreter.
    --dry-run prints all planned actions without writing any files or running
    pip.

kbu beril doctor <BERIL_ROOT>
    Pure-read health check: skill dirs present, import succeeds, version
    matches, preferences.md present.  Returns 0 only when all checks pass.

kbu beril worktree ...
    Manage parallel BERIL project worktrees.  See ``kbu beril worktree --help``
    for subcommands.

Output shape: per-step ``── name``, ``✓``/``✗`` lines, summary block,
return codes 0 all-ok / 1 partial / 2 none (following CRAFT CLI conventions).
"""

from __future__ import annotations

import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click

import kbutillib as _kbu_pkg

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_SKILL_NAMES = ["kbu", "kbu-notebook", "kbu-fba"]

# The KBUtilLib distribution name (matches pyproject.toml `name`).
_DIST_NAME = "KBUtilLib"


# ---------------------------------------------------------------------------
# Resource path helpers
# ---------------------------------------------------------------------------


def _skills_root() -> Path:
    """Return the path to src/kbutillib/beril/skills/ in the installed package.

    Uses importlib.resources-compatible resolution: walk up from the
    kbutillib package directory to find the beril/skills/ subdirectory.
    This works both from an editable install (the source tree is on
    sys.path) and from a wheel install (package data is inside the wheel).
    """
    pkg_dir = Path(_kbu_pkg.__file__).resolve().parent
    skills = pkg_dir / "beril" / "skills"
    if not skills.is_dir():
        raise FileNotFoundError(
            f"Cannot locate skill bundle at {skills}. "
            "Ensure KBUtilLib is installed with package data intact."
        )
    return skills


def _preferences_template() -> Path:
    """Return the path to the bundled preferences.md template."""
    return _skills_root() / "kbu" / "preferences.md"


# ---------------------------------------------------------------------------
# Interpreter discovery
# ---------------------------------------------------------------------------


def _discover_interpreter(beril_root: Path) -> str:
    """Discover the Python interpreter to use for this BERIL deployment.

    Resolution order:
    1. ``<BERIL_ROOT>/.venv/bin/python`` — if that file exists.
    2. The interpreter running the deployer (``sys.executable``).
    3. ``python3`` on PATH (last resort).

    Returns the interpreter path as a string.
    """
    venv_python = beril_root / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return str(venv_python)
    if sys.executable:
        return sys.executable
    # Final fallback — always truthy, PATH lookup deferred to subprocess.
    return "python3"


# ---------------------------------------------------------------------------
# Version probe helpers
# ---------------------------------------------------------------------------


def _deployer_version() -> str:
    """Return the KBUtilLib version as seen by the deployer's interpreter."""
    try:
        return importlib.metadata.version(_DIST_NAME)
    except importlib.metadata.PackageNotFoundError:
        return ""


def _installed_version_under(interpreter: str) -> Optional[str]:
    """Return KBUtilLib version installed under *interpreter*, or None.

    Runs ``interpreter -c "import importlib.metadata; print(importlib.metadata.version('KBUtilLib'))"``
    and captures stdout.  Returns None on ImportError, PackageNotFoundError,
    or any subprocess failure.
    """
    script = (
        "import importlib.metadata, sys; "
        f"v = importlib.metadata.version({_DIST_NAME!r}); "
        "sys.stdout.write(v)"
    )
    try:
        result = subprocess.run(
            [interpreter, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            v = result.stdout.strip()
            return v if v else None
        return None
    except Exception:  # noqa: BLE001
        return None


def _import_succeeds_under(interpreter: str) -> bool:
    """Return True iff ``import kbutillib`` succeeds under *interpreter*."""
    try:
        result = subprocess.run(
            [interpreter, "-c", "import kbutillib"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# install.json helpers
# ---------------------------------------------------------------------------


def _install_json_path(beril_root: Path) -> Path:
    return beril_root / ".claude" / "kbu" / "install.json"


def _read_install_json(beril_root: Path) -> Optional[dict]:
    p = _install_json_path(beril_root)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_install_json(beril_root: Path, interpreter: str) -> None:
    p = _install_json_path(beril_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "kbutillib_version": _deployer_version(),
        "interpreter": interpreter,
    }
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_beril_root(beril_root: Path) -> list[str]:
    """Return a list of fatal validation errors for *beril_root*.

    Errors (non-empty list) mean install/doctor must abort.
    """
    errors: list[str] = []
    if not beril_root.is_dir():
        errors.append(f"BERIL_ROOT does not exist or is not a directory: {beril_root}")
        return errors  # Stop here; other checks require the dir to exist.
    if not (beril_root / ".claude" / "skills").is_dir():
        errors.append(
            f"Missing required directory: {beril_root / '.claude' / 'skills'} "
            "(BERIL deployment must have .claude/skills/)"
        )
    if not (beril_root / "PROJECT.md").is_file():
        errors.append(
            f"Missing required file: {beril_root / 'PROJECT.md'} "
            "(BERIL deployment must have PROJECT.md)"
        )
    return errors


# ---------------------------------------------------------------------------
# pip install step
# ---------------------------------------------------------------------------


def _build_pip_cmd(interpreter: str) -> list[str]:
    """Build the pip install/upgrade command list."""
    return [
        interpreter,
        "-m",
        "pip",
        "install",
        "--upgrade",
        _DIST_NAME,
    ]


def _run_pip_install(interpreter: str) -> tuple[bool, str]:
    """Run pip install --upgrade KBUtilLib under *interpreter*.

    Returns (ok, detail). Retries once with --break-system-packages on a
    PEP-668 error (exit code 1 + 'externally-managed-environment' in stderr).
    """
    cmd = _build_pip_cmd(interpreter)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return False, f"interpreter not found: {interpreter}"
    except Exception as exc:  # noqa: BLE001
        return False, f"pip subprocess raised: {exc}"

    if result.returncode == 0:
        return True, "pip install succeeded"

    # PEP-668 retry
    if result.returncode != 0 and "externally-managed-environment" in (
        result.stderr + result.stdout
    ):
        cmd_bsp = cmd + ["--break-system-packages"]
        click.echo(
            f"   (PEP-668 detected — retrying with --break-system-packages)",
            err=True,
        )
        try:
            result2 = subprocess.run(cmd_bsp, capture_output=True, text=True)
        except Exception as exc:  # noqa: BLE001
            return False, f"pip retry raised: {exc}"
        if result2.returncode == 0:
            return True, "pip install succeeded (--break-system-packages)"
        return (
            False,
            f"pip install failed (rc={result2.returncode}): "
            + (result2.stderr or result2.stdout or "").strip()[:200],
        )

    return (
        False,
        f"pip install failed (rc={result.returncode}): "
        + (result.stderr or result.stdout or "").strip()[:200],
    )


# ---------------------------------------------------------------------------
# kbu beril install
# ---------------------------------------------------------------------------


@click.command("install")
@click.argument("beril_root", metavar="BERIL_ROOT")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help=(
        "Print all planned actions and resolved paths; "
        "do not copy files or run pip."
    ),
)
def install_cmd(beril_root: str, dry_run: bool) -> None:
    """Install KBUtilLib skills into a BERIL deployment at BERIL_ROOT.

    Copies the three skill dirs (kbu, kbu-notebook, kbu-fba) into
    BERIL_ROOT/.claude/skills/, renders preferences.md if absent, discovers
    a Python interpreter, records it in .claude/kbu/install.json, and
    pip-installs KBUtilLib into that interpreter.

    Idempotent: re-running overwrites skill dirs in place and skips the pip
    step if the installed version already matches.
    """
    root = Path(beril_root).resolve()
    deployer_ver = _deployer_version()

    click.echo(f"kbu beril install v{deployer_ver}", err=True)
    click.echo(f"BERIL_ROOT: {root}", err=True)
    if dry_run:
        click.echo("(dry-run: no files will be written)", err=True)
    click.echo("", err=True)

    # --- Validate root ---
    errors = _validate_beril_root(root)
    if errors:
        for e in errors:
            click.echo(f"Error: {e}", err=True)
        sys.exit(2)

    # Warn (not fail) if .git is absent
    if not (root / ".git").exists():
        click.echo(
            f"Warning: {root}/.git not found — BERIL_ROOT is not a git repo. "
            "Continuing anyway.",
            err=True,
        )

    n_ok = 0
    n_fail = 0
    steps: list[tuple[str, bool, str]] = []  # (step_name, ok, detail)

    # ── Step 1: copy skill dirs ──────────────────────────────────────────────
    click.echo("── Copy skill dirs", err=True)
    try:
        src_root = _skills_root()
    except FileNotFoundError as exc:
        click.echo(f"   ✗ Cannot locate skill bundle: {exc}", err=True)
        sys.exit(2)

    dest_skills_dir = root / ".claude" / "skills"
    for skill_name in _SKILL_NAMES:
        src = src_root / skill_name
        dest = dest_skills_dir / skill_name
        if dry_run:
            click.echo(
                f"   [dry-run] would copy {src} → {dest}",
                err=True,
            )
            steps.append((f"skill-dir:{skill_name}", True, "dry-run"))
            n_ok += 1
        else:
            try:
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(str(src), str(dest))
                click.echo(f"   ✓ {skill_name} → {dest}", err=True)
                steps.append((f"skill-dir:{skill_name}", True, str(dest)))
                n_ok += 1
            except Exception as exc:  # noqa: BLE001
                click.echo(f"   ✗ {skill_name}: {exc}", err=True)
                steps.append((f"skill-dir:{skill_name}", False, str(exc)))
                n_fail += 1

    # ── Step 2: preferences.md (render-if-absent, never clobber) ────────────
    click.echo("", err=True)
    click.echo("── preferences.md", err=True)
    prefs_dest = root / ".claude" / "kbu" / "preferences.md"
    prefs_src = _preferences_template()
    if dry_run:
        if prefs_dest.exists():
            click.echo(
                f"   [dry-run] preferences.md already exists at {prefs_dest} — would preserve",
                err=True,
            )
        else:
            click.echo(
                f"   [dry-run] would render {prefs_src} → {prefs_dest}",
                err=True,
            )
        steps.append(("preferences.md", True, "dry-run"))
        n_ok += 1
    elif prefs_dest.exists():
        click.echo(
            f"   ✓ preferences.md already present at {prefs_dest} — preserved (not overwritten)",
            err=True,
        )
        steps.append(("preferences.md", True, "preserved"))
        n_ok += 1
    else:
        try:
            prefs_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(prefs_src), str(prefs_dest))
            click.echo(f"   ✓ preferences.md rendered → {prefs_dest}", err=True)
            steps.append(("preferences.md", True, f"rendered → {prefs_dest}"))
            n_ok += 1
        except Exception as exc:  # noqa: BLE001
            click.echo(f"   ✗ preferences.md: {exc}", err=True)
            steps.append(("preferences.md", False, str(exc)))
            n_fail += 1

    # ── Step 3: interpreter discovery ───────────────────────────────────────
    click.echo("", err=True)
    click.echo("── Interpreter discovery", err=True)
    interpreter = _discover_interpreter(root)
    click.echo(f"   interpreter: {interpreter}", err=True)
    if dry_run:
        click.echo(
            f"   [dry-run] would write install.json with interpreter={interpreter}",
            err=True,
        )
        steps.append(("interpreter", True, f"dry-run: {interpreter}"))
        n_ok += 1
    else:
        try:
            _write_install_json(root, interpreter)
            click.echo(
                f"   ✓ install.json written → {_install_json_path(root)}", err=True
            )
            steps.append(("interpreter", True, interpreter))
            n_ok += 1
        except Exception as exc:  # noqa: BLE001
            click.echo(f"   ✗ install.json: {exc}", err=True)
            steps.append(("interpreter", False, str(exc)))
            n_fail += 1

    # ── Step 4: pip install ──────────────────────────────────────────────────
    click.echo("", err=True)
    click.echo("── pip install KBUtilLib", err=True)
    pip_cmd = _build_pip_cmd(interpreter)
    click.echo(f"   command: {' '.join(pip_cmd)}", err=True)

    if dry_run:
        click.echo("   [dry-run] skipping pip install", err=True)
        steps.append(("pip-install", True, "dry-run: skipped"))
        n_ok += 1
    else:
        # Check if already at the right version
        installed_ver = _installed_version_under(interpreter)
        if installed_ver and installed_ver == deployer_ver:
            click.echo(
                f"   ✓ already at {installed_ver} — skipping pip install",
                err=True,
            )
            steps.append(("pip-install", True, f"already at {installed_ver}"))
            n_ok += 1
        else:
            if installed_ver:
                click.echo(
                    f"   (installed: {installed_ver}, deployer: {deployer_ver} — upgrading)",
                    err=True,
                )
            else:
                click.echo("   (not installed — installing)", err=True)
            ok, detail = _run_pip_install(interpreter)
            marker = "✓" if ok else "✗"
            click.echo(f"   {marker} {detail}", err=True)
            steps.append(("pip-install", ok, detail))
            if ok:
                n_ok += 1
            else:
                n_fail += 1

    # ── Summary ──────────────────────────────────────────────────────────────
    n_total = n_ok + n_fail
    click.echo("", err=True)
    click.echo("═" * 60, err=True)
    click.echo("kbu beril install summary:", err=True)
    click.echo(f"  Steps OK:   {n_ok}/{n_total}", err=True)
    click.echo(f"  Steps FAIL: {n_fail}", err=True)
    if n_fail:
        click.echo("", err=True)
        click.echo("Failed steps:", err=True)
        for name, ok, detail in steps:
            if not ok:
                click.echo(f"  - {name}: {detail}", err=True)
    click.echo("═" * 60, err=True)

    if n_fail == 0:
        click.echo("All steps completed successfully.", err=True)
        sys.exit(0)
    elif n_ok > 0:
        click.echo("Partial install. Resolve the issues above and re-run.", err=True)
        sys.exit(1)
    else:
        click.echo("Install failed. Resolve the issues above and re-run.", err=True)
        sys.exit(2)


# ---------------------------------------------------------------------------
# kbu beril doctor
# ---------------------------------------------------------------------------


@click.command("doctor")
@click.argument("beril_root", metavar="BERIL_ROOT")
def doctor_cmd(beril_root: str) -> None:
    """Check KBUtilLib health in a BERIL deployment at BERIL_ROOT.

    Reports pass/fail for:
      1. Three skill dirs present in .claude/skills/
      2. ``import kbutillib`` succeeds under the install.json interpreter
      3. Installed version matches the deployer version
      4. preferences.md present

    Exits 0 only when all checks pass.
    """
    root = Path(beril_root).resolve()
    deployer_ver = _deployer_version()

    click.echo(f"kbu beril doctor v{deployer_ver}", err=True)
    click.echo(f"BERIL_ROOT: {root}", err=True)
    click.echo("", err=True)

    # Validate root (fatal errors only — we still run checks for diagnostics)
    errors = _validate_beril_root(root)
    if errors:
        for e in errors:
            click.echo(f"Error: {e}", err=True)
        sys.exit(2)

    # Resolve interpreter from install.json (or discover on the fly)
    install_data = _read_install_json(root)
    if install_data and install_data.get("interpreter"):
        interpreter = install_data["interpreter"]
    else:
        interpreter = _discover_interpreter(root)
        click.echo(
            f"   Note: install.json not found; using discovered interpreter: {interpreter}",
            err=True,
        )

    n_ok = 0
    n_fail = 0

    # ── Check 1: skill dirs ──────────────────────────────────────────────────
    click.echo("── Skill dirs in .claude/skills/", err=True)
    dest_skills_dir = root / ".claude" / "skills"
    for skill_name in _SKILL_NAMES:
        skill_dir = dest_skills_dir / skill_name
        if skill_dir.is_dir():
            click.echo(f"   ✓ {skill_name}", err=True)
            n_ok += 1
        else:
            click.echo(f"   ✗ {skill_name} — missing at {skill_dir}", err=True)
            n_fail += 1

    # ── Check 2: import kbutillib ────────────────────────────────────────────
    click.echo("", err=True)
    click.echo(f"── import kbutillib (interpreter: {interpreter})", err=True)
    if _import_succeeds_under(interpreter):
        click.echo("   ✓ import kbutillib succeeded", err=True)
        n_ok += 1
    else:
        click.echo(
            f"   ✗ import kbutillib FAILED under {interpreter} — "
            "run `kbu beril install` to fix",
            err=True,
        )
        n_fail += 1

    # ── Check 3: version matches deployer ───────────────────────────────────
    click.echo("", err=True)
    click.echo("── KBUtilLib version", err=True)
    installed_ver = _installed_version_under(interpreter)
    if installed_ver and installed_ver == deployer_ver:
        click.echo(
            f"   ✓ installed {installed_ver} matches deployer {deployer_ver}",
            err=True,
        )
        n_ok += 1
    elif installed_ver:
        click.echo(
            f"   ✗ version mismatch: installed {installed_ver}, "
            f"deployer {deployer_ver} — run `kbu beril install` to upgrade",
            err=True,
        )
        n_fail += 1
    else:
        click.echo(
            f"   ✗ cannot determine installed version under {interpreter}",
            err=True,
        )
        n_fail += 1

    # ── Check 4: preferences.md ──────────────────────────────────────────────
    click.echo("", err=True)
    click.echo("── preferences.md", err=True)
    prefs = root / ".claude" / "kbu" / "preferences.md"
    if prefs.is_file():
        click.echo(f"   ✓ preferences.md present at {prefs}", err=True)
        n_ok += 1
    else:
        click.echo(
            f"   ✗ preferences.md missing at {prefs} — "
            "run `kbu beril install` to render it",
            err=True,
        )
        n_fail += 1

    # ── Summary ──────────────────────────────────────────────────────────────
    n_total = n_ok + n_fail
    click.echo("", err=True)
    click.echo("═" * 60, err=True)
    click.echo("kbu beril doctor summary:", err=True)
    click.echo(f"  Checks OK:   {n_ok}/{n_total}", err=True)
    click.echo(f"  Checks FAIL: {n_fail}", err=True)
    click.echo("═" * 60, err=True)

    if n_fail == 0:
        click.echo("All checks passed.", err=True)
        sys.exit(0)
    else:
        click.echo("One or more checks failed. Run `kbu beril install` to fix.", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# kbu beril worktree — group-level options and context object
# ---------------------------------------------------------------------------

# Warning printed after successful new / open / start (AC #22).
_WORKTREE_WARNING = (
    "Warning: do NOT run 'beril start' directly inside a worktree — it will "
    "run _checkout_release and detach your worktree off its project branch. "
    "Use 'kbu beril worktree start' instead."
)


class _WorktreeCtx:
    """Holds resolved beril_root and worktree_root for the worktree subcommands."""

    def __init__(self, beril_root_opt: Optional[str], worktree_root_opt: Optional[str]) -> None:
        from kbutillib.beril_worktree.config import (
            resolve_beril_root,
            resolve_worktree_root,
        )

        try:
            self.beril_root = resolve_beril_root(explicit=beril_root_opt)
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc

        self.worktree_root = resolve_worktree_root(
            explicit=worktree_root_opt,
            beril_root=self.beril_root,
        )

    def manager(self):  # type: ignore[return]
        """Return a BerilWorktree instance for these roots."""
        from kbutillib.beril_worktree.manager import BerilWorktree

        return BerilWorktree(
            beril_root=self.beril_root,
            worktree_root=self.worktree_root,
        )


# ---------------------------------------------------------------------------
# kbu beril worktree <group>
# ---------------------------------------------------------------------------


@click.group("worktree")
@click.option(
    "--beril-root",
    metavar="PATH",
    default=None,
    help=(
        "Path to the primary BERIL repository checkout. "
        "Overrides BERIL_ROOT env var and config."
    ),
)
@click.option(
    "--root",
    "--worktree-root",
    "worktree_root",
    metavar="PATH",
    default=None,
    help=(
        "Root directory under which per-project worktrees are created. "
        "Overrides WORKING_BERIL_DIRECTORY env var and config."
    ),
)
@click.pass_context
def worktree_cmd(ctx: click.Context, beril_root: Optional[str], worktree_root: Optional[str]) -> None:
    """Manage parallel BERIL project git worktrees.

    Each worktree is a separate working directory on its own
    ``projects/<id>`` branch, backed by the primary BERIL checkout's
    single ``.git`` object store.
    """
    ctx.ensure_object(dict)
    ctx.obj["beril_root"] = beril_root
    ctx.obj["worktree_root"] = worktree_root


def _get_ctx(ctx: click.Context) -> _WorktreeCtx:
    """Resolve worktree context from the click context object."""
    return _WorktreeCtx(
        beril_root_opt=ctx.obj.get("beril_root"),
        worktree_root_opt=ctx.obj.get("worktree_root"),
    )


# ---------------------------------------------------------------------------
# kbu beril worktree new
# ---------------------------------------------------------------------------


@worktree_cmd.command("new")
@click.argument("project_id")
@click.option(
    "--open",
    "open_cursor",
    is_flag=True,
    default=False,
    help="Launch Cursor on the workspace after creating the worktree.",
)
@click.pass_context
def worktree_new_cmd(ctx: click.Context, project_id: str, open_cursor: bool) -> None:
    """Create (or re-adopt) a worktree for PROJECT_ID.

    Creates branch ``projects/<id>`` off ``main`` when it does not exist;
    adopts the existing branch otherwise.  Symlinks ``.env`` and
    ``.venv-berdl`` into the worktree from the primary BERIL checkout.
    Writes a ``<id>.code-workspace`` file in the worktree root.
    """
    wt_ctx = _get_ctx(ctx)
    try:
        wt_path = wt_ctx.manager().new(project_id, open_cursor=False)
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Worktree created: {wt_path}")

    if open_cursor:
        _open_cursor_workspace(wt_ctx, project_id)

    click.echo(_WORKTREE_WARNING)


# ---------------------------------------------------------------------------
# kbu beril worktree open
# ---------------------------------------------------------------------------


@worktree_cmd.command("open")
@click.argument("project_id")
@click.pass_context
def worktree_open_cmd(ctx: click.Context, project_id: str) -> None:
    """Open Cursor on the worktree for PROJECT_ID.

    Recreates the worktree directory from the existing ``projects/<id>``
    branch if its directory is missing.  Errors if the branch does not
    exist (run ``new`` first).
    """
    wt_ctx = _get_ctx(ctx)
    try:
        wt_ctx.manager().open(project_id)
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc

    _open_cursor_workspace(wt_ctx, project_id)
    click.echo(_WORKTREE_WARNING)


# ---------------------------------------------------------------------------
# kbu beril worktree start
# ---------------------------------------------------------------------------


@worktree_cmd.command("start")
@click.argument("project_id")
@click.option("--agent", default=None, metavar="NAME", help="Agent to launch (default: from beril config).")
@click.option(
    "--skip-onboard",
    is_flag=True,
    default=False,
    help="Do not inject the /berdl_start onboarding prompt.",
)
@click.argument("extra_args", nargs=-1, metavar="[-- ARGS...]")
@click.pass_context
def worktree_start_cmd(
    ctx: click.Context,
    project_id: str,
    agent: Optional[str],
    skip_onboard: bool,
    extra_args: tuple,
) -> None:
    """Launch the configured agent inside the worktree for PROJECT_ID.

    Behaves like ``beril start`` but skips the release-tag checkout so
    the worktree stays on its ``projects/<id>`` branch.  Imports
    ``get_default_agent``, ``get_vertex_config``, and
    ``_sync_auth_token`` from ``beril_cli`` directly.

    Pass additional arguments to the agent after ``--``, e.g.:

        kbu beril worktree start foo -- --resume
    """
    import kbutillib.beril_worktree.launch as _launch_module

    wt_ctx = _get_ctx(ctx)
    try:
        wt_path = wt_ctx.manager().open(project_id)
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        _launch_module.launch_start(
            worktree_path=wt_path,
            agent=agent,
            extra_args=list(extra_args),
            skip_onboard=skip_onboard,
        )
    except (RuntimeError, ImportError, AttributeError) as exc:
        raise click.ClickException(str(exc)) from exc


# ---------------------------------------------------------------------------
# kbu beril worktree rm
# ---------------------------------------------------------------------------


@worktree_cmd.command("rm")
@click.argument("project_id")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Remove even when the worktree has uncommitted changes.",
)
@click.pass_context
def worktree_rm_cmd(ctx: click.Context, project_id: str, force: bool) -> None:
    """Remove the worktree directory for PROJECT_ID.

    Keeps the ``projects/<id>`` branch in the primary BERIL repository so
    it can be reopened later.  Idempotent: prints "nothing to remove" and
    exits 0 when the worktree is not registered.
    """
    wt_ctx = _get_ctx(ctx)
    try:
        wt_ctx.manager().remove(project_id, force=force)
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


# ---------------------------------------------------------------------------
# kbu beril worktree ls
# ---------------------------------------------------------------------------


@worktree_cmd.command("ls")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a stable JSON array sorted by id.",
)
@click.pass_context
def worktree_ls_cmd(ctx: click.Context, as_json: bool) -> None:
    """List live worktrees and reopenable project branches.

    Default output is human-readable with two sections (live / reopenable).
    ``--json`` emits a stable array ``[{"id","branch","path","live"}]``
    sorted by id (AC #14).
    """
    wt_ctx = _get_ctx(ctx)
    entries = wt_ctx.manager().list()

    if as_json:
        import json as _json

        data = [
            {
                "id": e.id,
                "branch": e.branch,
                "path": e.path,
                "live": e.live,
            }
            for e in entries
        ]
        click.echo(_json.dumps(data, indent=2))
        return

    live = [e for e in entries if e.live]
    reopenable = [e for e in entries if not e.live]

    click.echo("Live worktrees:")
    if live:
        for e in live:
            click.echo(f"  {e.id:30s}  {e.branch}  ({e.path})")
    else:
        click.echo("  (none)")

    click.echo("")
    click.echo("Reopenable branches (no live worktree):")
    if reopenable:
        for e in reopenable:
            click.echo(f"  {e.id:30s}  {e.branch}")
    else:
        click.echo("  (none)")


# ---------------------------------------------------------------------------
# kbu beril worktree set-root
# ---------------------------------------------------------------------------


@worktree_cmd.command("set-root")
@click.argument("worktree_root_path", metavar="WORKTREE_ROOT", required=False, default=None)
@click.pass_context
def worktree_set_root_cmd(ctx: click.Context, worktree_root_path: Optional[str]) -> None:
    """Persist beril_root and/or worktree_root to ~/.kbutillib/config.yaml.

    Uses ``--beril-root`` (from the group) and/or the positional
    WORKTREE_ROOT argument to set ``beril.root`` and/or
    ``beril.worktree_root``.  Expands ``~``, resolves to absolute paths,
    and creates the config file and its parent directory if missing.
    Never writes to BERIL's own config.
    """
    from kbutillib.beril_worktree.config import set_root

    beril_root_opt = ctx.obj.get("beril_root")

    if beril_root_opt is None and worktree_root_path is None:
        raise click.UsageError(
            "Provide at least one of --beril-root PATH or a WORKTREE_ROOT argument."
        )

    try:
        set_root(
            beril_root=beril_root_opt,
            worktree_root=worktree_root_path,
        )
    except ValueError as exc:  # pragma: no cover
        raise click.ClickException(str(exc)) from exc

    if beril_root_opt is not None:
        click.echo(f"Set beril.root = {Path(beril_root_opt).expanduser().resolve()}")
    if worktree_root_path is not None:
        click.echo(f"Set beril.worktree_root = {Path(worktree_root_path).expanduser().resolve()}")


# ---------------------------------------------------------------------------
# kbu beril worktree doctor
# ---------------------------------------------------------------------------


@worktree_cmd.command("doctor")
@click.pass_context
def worktree_doctor_cmd(ctx: click.Context) -> None:
    """Verify BERIL launch-helper imports and worktree symlink health.

    Checks:
      1. ``beril_cli`` is importable.
      2. The three borrowed symbols (``get_default_agent``,
         ``get_vertex_config``, ``_sync_auth_token``) resolve.
      3. Each configured worktree's ``.env`` and ``.venv-berdl`` symlink
         targets exist and are readable (AC #18).

    Exits 0 on success, 1 on any failure (AC #17).
    """
    import kbutillib.beril_worktree.launch as _launch_module

    n_ok = 0
    n_fail = 0

    # ── Check 1 & 2: beril_cli imports ─────────────────────────────────
    click.echo("── beril_cli import check", err=True)
    try:
        _launch_module._import_beril_cli()
        click.echo(
            f"   ✓ beril_cli imported; symbols {', '.join(_launch_module._BERIL_CLI_SYMBOLS)} resolved",
            err=True,
        )
        n_ok += 1
    except ImportError as exc:
        click.echo(f"   ✗ {exc}", err=True)
        n_fail += 1
    except AttributeError as exc:
        click.echo(f"   ✗ {exc}", err=True)
        n_fail += 1

    # ── Check 3: worktree symlink health ────────────────────────────────
    click.echo("", err=True)
    click.echo("── Worktree symlink health", err=True)

    try:
        wt_ctx = _get_ctx(ctx)
        entries = wt_ctx.manager().list()
        live_entries = [e for e in entries if e.live and e.path is not None]
    except click.UsageError as exc:
        click.echo(f"   (skipped — beril_root not configured: {exc})", err=True)
        live_entries = []

    if not live_entries:
        click.echo("   (no live worktrees to check)", err=True)
    else:
        for entry in live_entries:
            wt_path = Path(entry.path)  # type: ignore[arg-type]
            for name in (".env", ".venv-berdl"):
                link = wt_path / name
                if not link.is_symlink():
                    click.echo(f"   ✗ {entry.id}/{name}: not a symlink at {link}", err=True)
                    n_fail += 1
                    continue
                target = link.resolve()
                try:
                    target.stat()
                    click.echo(f"   ✓ {entry.id}/{name} → {target} (readable)", err=True)
                    n_ok += 1
                except OSError:
                    click.echo(
                        f"   ✗ {entry.id}/{name} → {target} (target missing or unreadable)",
                        err=True,
                    )
                    n_fail += 1

    # ── Summary ─────────────────────────────────────────────────────────
    click.echo("", err=True)
    click.echo("═" * 60, err=True)
    click.echo("kbu beril worktree doctor summary:", err=True)
    click.echo(f"  Checks OK:   {n_ok}", err=True)
    click.echo(f"  Checks FAIL: {n_fail}", err=True)
    click.echo("═" * 60, err=True)

    if n_fail == 0:
        click.echo("All checks passed.", err=True)
        sys.exit(0)
    else:
        click.echo("One or more checks failed.", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Cursor launch helper (shared by new --open and open)
# ---------------------------------------------------------------------------


def _open_cursor_workspace(wt_ctx: _WorktreeCtx, project_id: str) -> None:
    """Launch Cursor on the <id>.code-workspace file.

    If ``cursor`` is not on PATH, prints the workspace path and a manual
    instruction rather than failing hard (AC #7 precedent).
    """
    ws_file = wt_ctx.worktree_root / f"{project_id}.code-workspace"
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
# beril group
# ---------------------------------------------------------------------------


@click.group("beril")
def beril_cmd() -> None:
    """BERIL-root deployer: install KBUtilLib skills and verify deployment health."""


beril_cmd.add_command(install_cmd, name="install")
beril_cmd.add_command(doctor_cmd, name="doctor")
beril_cmd.add_command(worktree_cmd, name="worktree")
