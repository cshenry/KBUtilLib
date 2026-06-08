"""``kbu init`` and ``kbu doctor`` — machine-level environment setup.

Creates a virtualenv (venvman or plain .venv), installs KBUtilLib editable,
registers a Jupyter kernel, and writes an idempotency marker at
``~/.config/kbu/init_done.json`` (XDG_CONFIG_HOME respected).

v1 is macOS-only.  On non-macOS, both ``kbu init`` and ``kbu doctor`` print
a one-screen message and exit 1 unless ``KBU_PLATFORM_OVERRIDE=force`` is set.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click

from .manifest import now_utc_iso, read_project_manifest


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


# ---------------------------------------------------------------------------
# Marker file helpers
# ---------------------------------------------------------------------------


def _marker_path() -> Path:
    """Return the path to the init marker file, respecting XDG_CONFIG_HOME."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg:
        base = Path(xdg)
    else:
        base = Path.home() / ".config"
    return base / "kbu" / "init_done.json"


def _read_marker() -> Optional[dict]:
    """Return the parsed marker dict, or None if it does not exist."""
    p = _marker_path()
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def _write_marker(
    kbutillib_repo_path: str,
    kbutillib_commit: str,
    venv_manager: str,
    venv_python: str,
    jupyter_kernel_name: str = "kbutillib",
) -> None:
    """Write the init marker JSON file.

    *venv_manager* must be one of ``"venvman"`` or ``".venv"``.
    """
    p = _marker_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    marker = {
        "version": 1,
        "initialized_at": now_utc_iso(),
        "kbutillib_repo_path": kbutillib_repo_path,
        "kbutillib_commit": kbutillib_commit,
        "venv_manager": venv_manager,
        "venv_python": venv_python,
        "jupyter_kernel_name": jupyter_kernel_name,
    }
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(marker, fh, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------------------
# KBUtilLib root resolution
# ---------------------------------------------------------------------------


def _kbutillib_root() -> Path:
    """Return the absolute path to the KBUtilLib repo root.

    This is the directory that contains this source file's package tree
    (``src/kbutillib``), resolved two levels up from the ``kbutillib``
    package directory.
    """
    # src/kbutillib/cli/init.py → src/kbutillib → src → repo_root
    this_file = Path(__file__).resolve()
    return this_file.parent.parent.parent.parent


def _kbutillib_commit(repo_root: Path) -> str:
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
# Platform check
# ---------------------------------------------------------------------------


def _is_macos_or_override() -> bool:
    """Return True if this is macOS or KBU_PLATFORM_OVERRIDE=force is set."""
    return sys.platform == "darwin" or os.environ.get("KBU_PLATFORM_OVERRIDE") == "force"


def _is_darwin() -> bool:
    """Return True if this is macOS."""
    return sys.platform == "darwin"


# ---------------------------------------------------------------------------
# venv creation helpers
# ---------------------------------------------------------------------------


def _run_venvman(repo_root: Path) -> tuple[Optional[Path], str]:
    """Run venvman to create the kbutillib venv.

    Returns ``(venv_python_path, error_detail)``. On success, the error_detail
    is an empty string. On failure, the path is None and error_detail carries
    the venvman stderr (or stdout fallback) so the caller can surface it.

    venvman writes an ``activate.sh`` into ``--dir``.  We source it to
    discover ``VIRTUAL_ENV``, but since we can't source a shell script from
    Python, we instead read the activate.sh and extract the VIRTUAL_ENV line.
    """
    result = subprocess.run(
        [
            "venvman",
            "create",
            "--project",
            "kbutillib",
            "--dir",
            str(repo_root),
            "--python",
            "3.11",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if not detail:
            detail = f"venvman exited with code {result.returncode}"
        return None, detail
    # Resolve venv python from activate.sh written by venvman
    activate_sh = repo_root / "activate.sh"
    if activate_sh.exists():
        venv_dir = _parse_virtual_env_from_activate(activate_sh)
        if venv_dir:
            candidate = venv_dir / "bin" / "python"
            if candidate.exists():
                return candidate, ""
    return None, "venvman succeeded but VIRTUAL_ENV could not be resolved from activate.sh"


def _parse_virtual_env_from_activate(activate_sh: Path) -> Optional[Path]:
    """Extract the venv path from an activate.sh generated by venvman.

    Two formats are supported:

    1. Legacy: a literal ``VIRTUAL_ENV="/absolute/path"`` line.
    2. Current (venvman >= late-2025): ``VENV_SUBDIR="<name>"`` composed with
       ``${VIRTUAL_ENVIRONMENT_DIRECTORY}/${VENV_SUBDIR}`` at activate time.
       We resolve ``VIRTUAL_ENVIRONMENT_DIRECTORY`` from the current process
       environment (venvman sets it in the user's shell profile).
    """
    try:
        text = activate_sh.read_text(encoding="utf-8")
    except OSError:
        return None

    venv_subdir: Optional[str] = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("VIRTUAL_ENV="):
            value = stripped[len("VIRTUAL_ENV="):].strip().strip('"').strip("'")
            if value:
                return Path(value)
        if stripped.startswith("VENV_SUBDIR="):
            venv_subdir = stripped[len("VENV_SUBDIR="):].strip().strip('"').strip("'")

    if venv_subdir:
        ved = os.environ.get("VIRTUAL_ENVIRONMENT_DIRECTORY")
        if ved:
            return Path(ved) / venv_subdir
    return None


def _create_plain_venv(repo_root: Path) -> Path:
    """Create a plain .venv in *repo_root* and return the python path."""
    subprocess.run(
        [sys.executable, "-m", "venv", ".venv"],
        cwd=str(repo_root),
        check=True,
    )
    return repo_root / ".venv" / "bin" / "python"


# ---------------------------------------------------------------------------
# init_status
# ---------------------------------------------------------------------------


def init_status() -> int:
    """Return the init status exit code.

    Returns:
        0 — marker present AND ``venv_python`` resolves to an executable.
        1 — marker missing.
        2 — marker present but ``venv_python`` no longer resolves.
    """
    marker = _read_marker()
    if marker is None:
        return 1
    venv_python = marker.get("venv_python", "")
    if not venv_python:
        return 2
    p = Path(venv_python)
    if p.is_file() and os.access(str(p), os.X_OK):
        return 0
    return 2


# ---------------------------------------------------------------------------
# core init logic
# ---------------------------------------------------------------------------


def _do_init(repo_root: Path) -> None:  # noqa: C901 — subprocess orchestration
    """Perform the full init sequence: venv → pip install -e → ipykernel → marker.

    Raises ``SystemExit`` on fatal errors.
    """
    # Determine venv manager.
    # On non-Darwin with override, venvman is treated as absent (per PRD).
    use_venvman = _is_darwin() and shutil.which("venvman") is not None

    venv_python: Optional[Path] = None
    venv_manager: str

    if use_venvman:
        click.echo("Detected venvman — running venvman create ...")
        venv_python, venvman_err = _run_venvman(repo_root)
        if venv_python is None:
            click.echo(
                f"Warning: venvman create failed ({venvman_err}) — falling back to python -m venv .venv",
                err=True,
            )
            venv_python = _create_plain_venv(repo_root)
            venv_manager = ".venv"
        else:
            venv_manager = "venvman"
    else:
        click.echo("Creating .venv with python -m venv ...")
        venv_python = _create_plain_venv(repo_root)
        venv_manager = ".venv"

    venv_python_str = str(venv_python)

    # pip install -e <repo_root>
    click.echo(f"Installing KBUtilLib editable from {repo_root} ...")
    subprocess.run(
        [venv_python_str, "-m", "pip", "install", "-e", str(repo_root)],
        check=True,
    )

    # Register Jupyter kernel
    click.echo("Registering Jupyter kernel 'kbutillib' ...")
    subprocess.run(
        [
            venv_python_str,
            "-m",
            "ipykernel",
            "install",
            "--user",
            "--name=kbutillib",
            "--display-name=KBUtilLib (kbu)",
        ],
        check=True,
    )

    # Write marker
    commit = _kbutillib_commit(repo_root)
    _write_marker(
        kbutillib_repo_path=str(repo_root),
        kbutillib_commit=commit,
        venv_manager=venv_manager,
        venv_python=venv_python_str,
        jupyter_kernel_name="kbutillib",
    )
    click.echo("Init complete. Marker written to " + str(_marker_path()))


def _do_update(repo_root: Path, venv_python: str) -> None:
    """Pull KBUtilLib and reinstall editable (--update path)."""
    click.echo("Pulling KBUtilLib from git ...")
    pull_result = subprocess.run(
        ["git", "-C", str(repo_root), "pull"],
        capture_output=True,
        text=True,
        check=False,
    )
    if pull_result.returncode != 0:
        click.echo(
            f"Warning: git pull failed: {pull_result.stderr.strip()}",
            err=True,
        )

    click.echo("Reinstalling KBUtilLib editable ...")
    subprocess.run(
        [venv_python, "-m", "pip", "install", "-e", str(repo_root), "--upgrade"],
        check=True,
    )
    click.echo("Update complete.")


# ---------------------------------------------------------------------------
# doctor probes
# ---------------------------------------------------------------------------


def _probe_init_done() -> tuple[str, str]:
    """Probe: init marker present and venv resolves."""
    code = init_status()
    if code == 0:
        marker = _read_marker()
        venv = marker.get("venv_python", "?") if marker else "?"
        return "PASS", f"init marker present; venv_python={venv}"
    elif code == 1:
        return "FAIL", "init marker missing; run `kbu init`"
    else:
        marker = _read_marker()
        venv = marker.get("venv_python", "?") if marker else "?"
        return "FAIL", f"init marker present but venv_python not found: {venv}; re-run `kbu init`"


def _probe_cursor_on_path() -> tuple[str, str]:
    """Probe: cursor binary on PATH."""
    path = shutil.which("cursor")
    if path:
        return "PASS", f"cursor found at {path}"
    return "FAIL", "cursor not found on PATH; install Cursor from https://cursor.sh"


def _probe_claude_extension(verbose: bool = False) -> tuple[str, str]:
    """Probe: Anthropic Claude Code extension installed in Cursor.

    If cursor is not on PATH, returns SKIP.
    """
    if not shutil.which("cursor"):
        return "SKIP", "cursor not on PATH; cannot check extension"
    try:
        result = subprocess.run(
            ["cursor", "--list-extensions"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        output = result.stdout + result.stderr
        if "anthropic.claude" in output.lower():
            return "PASS", "anthropic.claude-code extension found"
        return "FAIL", "anthropic.claude-code extension not found; install from Cursor marketplace"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "SKIP", f"could not run cursor --list-extensions: {exc}"


def _probe_kbu_version() -> tuple[str, str]:
    """Probe: kbu --version resolves."""
    path = shutil.which("kbu")
    if path:
        try:
            result = subprocess.run(
                ["kbu", "--version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            version_line = (result.stdout + result.stderr).strip().splitlines()
            version = version_line[0] if version_line else "(unknown)"
            if result.returncode == 0:
                return "PASS", f"kbu resolves: {version}"
            return "FAIL", f"kbu --version exited non-zero: {version}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            return "FAIL", f"kbu --version failed: {exc}"
    return "FAIL", "kbu not found on PATH; is the venv activated?"


def _probe_jupyter_kernel() -> tuple[str, str]:
    """Probe: kbutillib kernel registered in Jupyter."""
    try:
        result = subprocess.run(
            ["jupyter", "kernelspec", "list", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if result.returncode != 0:
            return "FAIL", "jupyter kernelspec list failed; is jupyter installed?"
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return "FAIL", "could not parse jupyter kernelspec list --json output"
        kernels = data.get("kernelspecs", {})
        if "kbutillib" in kernels:
            resource_dir = kernels["kbutillib"].get("resource_dir", "?")
            return "PASS", f"kbutillib kernel registered at {resource_dir}"
        return "FAIL", "kbutillib kernel not registered; run `kbu init` to register it"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "FAIL", f"could not run jupyter kernelspec list: {exc}"


def _probe_project_origin() -> str:
    """Return the project-origin info line for the current working directory.

    Reads ``kbu-project.toml`` from cwd.  Returns a plain string (not a
    ``[STATUS] name: detail`` probe tuple) because this is informational only
    and never causes doctor to exit 1.

    - Manifest present with ``[project].bootstrapped = true``:
      ``project origin: bootstrap (<bootstrapped_at>)``
    - Manifest present but ``bootstrapped`` absent or False:
      ``project origin: new-project (<created_at>)``
    - No manifest in cwd:
      ``project origin: (no kbu-project.toml in cwd)``
    """
    try:
        manifest = read_project_manifest(Path.cwd())
    except FileNotFoundError:
        return "project origin: (no kbu-project.toml in cwd)"

    project = manifest.get("project", {})
    bootstrapped = project.get("bootstrapped", False)
    if bootstrapped:
        ts = project.get("bootstrapped_at", "")
        return f"project origin: bootstrap ({ts})"
    else:
        ts = project.get("created_at", "")
        return f"project origin: new-project ({ts})"


# ---------------------------------------------------------------------------
# Click commands
# ---------------------------------------------------------------------------


@click.group()
def init_cmd() -> None:
    """Machine-level init and doctor commands."""


@click.command("init")
@click.option("--status", "mode", flag_value="status", help="Exit 0/1/2 based on init state.")
@click.option("--update", "mode", flag_value="update", help="Pull + reinstall editable.")
def init_command(mode: Optional[str]) -> None:
    """Initialize the KBUtilLib environment (venv, editable install, Jupyter kernel).

    Idempotent: safe to re-run.  Writes a marker at
    ``~/.config/kbu/init_done.json`` on success.

    v1 targets macOS only.  On non-macOS, exits 1 unless
    KBU_PLATFORM_OVERRIDE=force is set.
    """
    if mode == "status":
        code = init_status()
        if code == 0:
            click.echo("init: OK")
        elif code == 1:
            click.echo("init: not initialized")
        else:
            click.echo("init: marker present but venv_python not found")
        sys.exit(code)

    if not _is_macos_or_override():
        click.echo(_V1_MACOS_ONLY_MESSAGE)
        sys.exit(1)

    if mode == "update":
        marker = _read_marker()
        if marker is None:
            click.echo("Not initialized yet. Run `kbu init` first.", err=True)
            sys.exit(1)
        venv_python = marker.get("venv_python", "")
        if not venv_python:
            click.echo("Marker missing venv_python. Re-run `kbu init`.", err=True)
            sys.exit(1)
        repo_root = _kbutillib_root()
        _do_update(repo_root, venv_python)
        return

    # Normal init
    repo_root = _kbutillib_root()
    _do_init(repo_root)


@click.command("doctor")
@click.option("--verbose", is_flag=True, default=False, help="Show extra detail per probe.")
def doctor_command(verbose: bool) -> None:
    """Run environment health checks and print one line per probe.

    Exits 0 if all probes PASS or SKIP; exits 1 if any probe FAILs.
    """
    probes = [
        ("init-done", _probe_init_done),
        ("cursor-on-path", _probe_cursor_on_path),
        ("claude-extension", lambda: _probe_claude_extension(verbose=verbose)),
        ("kbu-version", _probe_kbu_version),
        ("jupyter-kernel", _probe_jupyter_kernel),
    ]

    any_fail = False
    for name, fn in probes:
        status, detail = fn()
        click.echo(f"[{status}] {name}: {detail}")
        if status == "FAIL":
            any_fail = True

    click.echo(_probe_project_origin())

    sys.exit(1 if any_fail else 0)
