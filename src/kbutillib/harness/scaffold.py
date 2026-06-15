"""init subcommand — scaffold a per-project harness directory.

Workflow
--------
1. Validate BERIL root (PROJECT.md + .claude/skills/, warn on missing .git).
2. Validate <BERIL_ROOT>/projects/<project-id>/ exists.
3. Resolve / create the harness root; sanitize project-id.
4. Refuse if target harness dir is non-empty unless --force.
5. git init the harness dir.
6. Create BERIL-mirror dirs: notebooks/ data/ user_data/ figures/
7. Write .gitignore, empty DEVLOG.md, harness.toml.
8. Copy kbu-run skill bundle.
9. Copy / render preferences.md.
10. Build venv (venvman path, then plain venv fallback).
11. pip install -U pip wheel.
12. pip install kbutillib (skipped in source-checkout mode).
13. pip install -r requirements.txt if present.
14. Write python path to harness.toml.
15. Initial pull.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import kbutillib as _kbu_pkg

from .config import (
    HarnessConfig,
    _get_kbutillib_version,
    find_harness_toml,
    load_config,
    sanitize_project_id,
    save_config,
)
from .sync import pull as _pull


# ---------------------------------------------------------------------------
# Paths into the installed package
# ---------------------------------------------------------------------------


def _harness_skills_root() -> Path:
    """Absolute path to src/kbutillib/harness/skills/."""
    pkg_dir = Path(_kbu_pkg.__file__).resolve().parent
    p = pkg_dir / "harness" / "skills"
    if not p.is_dir():
        raise FileNotFoundError(
            f"Cannot locate harness skill bundle at {p}. "
            "Ensure KBUtilLib is installed with package data intact."
        )
    return p


def _bundled_preferences_template() -> Path:
    """Return the bundled preferences.md template path (PRD-A default)."""
    pkg_dir = Path(_kbu_pkg.__file__).resolve().parent
    p = pkg_dir / "beril" / "skills" / "kbu" / "preferences.md"
    if not p.is_file():
        raise FileNotFoundError(
            f"Cannot locate bundled preferences.md template at {p}."
        )
    return p


# ---------------------------------------------------------------------------
# BERIL root validation
# ---------------------------------------------------------------------------


def validate_beril_root(beril_root: Path) -> list[str]:
    """Return fatal validation errors (empty = ok)."""
    errors: list[str] = []
    if not beril_root.is_dir():
        errors.append(
            f"BERIL_ROOT does not exist or is not a directory: {beril_root}"
        )
        return errors
    if not (beril_root / "PROJECT.md").is_file():
        errors.append(f"Missing required file: {beril_root / 'PROJECT.md'}")
    if not (beril_root / ".claude" / "skills").is_dir():
        errors.append(
            f"Missing required directory: {beril_root / '.claude' / 'skills'}"
        )
    return errors


# ---------------------------------------------------------------------------
# Dev-checkout detection
# ---------------------------------------------------------------------------


def _is_source_checkout() -> bool:
    """True when kbutillib is running from a source checkout (not installed wheel)."""
    try:
        pkg_file = Path(_kbu_pkg.__file__).resolve()
        # pkg_file is <repo>/src/kbutillib/__init__.py
        # parents[1] should be 'src'
        if pkg_file.parents[1].name == "src":
            pyproject = pkg_file.parents[2] / "pyproject.toml"
            return pyproject.is_file()
    except Exception:  # noqa: BLE001
        pass
    return False


# ---------------------------------------------------------------------------
# Venv builder
# ---------------------------------------------------------------------------


def _build_venv(
    harness_dir: Path,
    project_name: str,
    echo=print,
) -> Optional[str]:
    """Build a Python 3.11 venv in harness_dir/.venv.

    Resolution order:
    1. venvman (kbu-bootstrap path)
    2. plain venv fallback (py3.11 if on PATH, else sys.executable)

    Returns the absolute path to the venv interpreter, or None on failure.
    """
    # Try venvman first
    if shutil.which("venvman"):
        echo("── venv: trying venvman")
        result = subprocess.run(
            [
                "venvman",
                "create",
                "--project",
                project_name,
                "--dir",
                str(harness_dir),
                "--python",
                "3.11",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            # venvman may write activate.sh; try resolving from it
            activate_sh = harness_dir / "activate.sh"
            if activate_sh.exists():
                from kbutillib.cli._template_ops import parse_virtual_env_from_activate
                venv_dir = parse_virtual_env_from_activate(activate_sh)
                if venv_dir:
                    candidate = venv_dir / "bin" / "python"
                    if candidate.exists():
                        echo(f"   ✓ venvman: {candidate}")
                        return str(candidate)
            # Also try the standard .venv location in case venvman put it there
            std = harness_dir / ".venv" / "bin" / "python"
            if std.exists():
                echo(f"   ✓ venvman: {std}")
                return str(std)
        else:
            echo(
                f"   venvman failed (rc={result.returncode}): "
                f"{(result.stderr or result.stdout or '').strip()[:200]} — "
                "falling back to plain venv"
            )

    # Plain venv fallback: prefer python3.11 on PATH, else sys.executable
    echo("── venv: creating .venv with venv module")
    py_candidates = ["python3.11", "python3", sys.executable]
    py_exec: Optional[str] = None
    for candidate in py_candidates:
        if shutil.which(candidate) or Path(candidate).is_file():
            py_exec = candidate if shutil.which(candidate) else candidate
            break
    if py_exec is None:
        py_exec = sys.executable

    result = subprocess.run(
        [py_exec, "-m", "venv", ".venv"],
        cwd=str(harness_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        echo(
            f"   ✗ venv creation failed: {(result.stderr or result.stdout or '').strip()[:200]}"
        )
        return None
    interpreter = str(harness_dir / ".venv" / "bin" / "python")
    echo(f"   ✓ .venv created: {interpreter}")
    return interpreter


# ---------------------------------------------------------------------------
# pip helpers
# ---------------------------------------------------------------------------


def _pip_run(interpreter: str, args: list[str], echo=print) -> bool:
    """Run pip under *interpreter* with *args*; return True on success."""
    cmd = [interpreter, "-m", "pip"] + args
    echo(f"   command: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return True
    # PEP-668 retry
    if "externally-managed-environment" in (result.stderr + result.stdout):
        cmd2 = cmd + ["--break-system-packages"]
        result2 = subprocess.run(cmd2, capture_output=True, text=True, check=False)
        if result2.returncode == 0:
            return True
        echo(
            f"   ✗ pip failed (PEP-668 retry): "
            f"{(result2.stderr or result2.stdout or '').strip()[:200]}"
        )
        return False
    echo(
        f"   ✗ pip failed (rc={result.returncode}): "
        f"{(result.stderr or result.stdout or '').strip()[:200]}"
    )
    return False


# ---------------------------------------------------------------------------
# Main scaffold function
# ---------------------------------------------------------------------------


def init_harness(
    beril_root: Path,
    project_id: str,
    harness_root: Optional[Path] = None,
    force: bool = False,
    echo=print,
) -> tuple[bool, str]:
    """Scaffold a per-project harness.

    Returns (success, message).
    """
    beril_root = beril_root.resolve()

    # 1. Validate BERIL root
    errors = validate_beril_root(beril_root)
    if errors:
        return False, "BERIL root validation failed:\n" + "\n".join(f"  ✗ {e}" for e in errors)

    if not (beril_root / ".git").exists():
        echo(
            f"Warning: {beril_root}/.git not found — BERIL_ROOT is not a git repo. "
            "Continuing anyway."
        )

    # 2. Sanitize project-id and check projects/<id> exists
    pid = sanitize_project_id(project_id)
    project_src = beril_root / "projects" / pid
    if not project_src.is_dir():
        # Also try the original (unsanitized) project_id
        project_src_orig = beril_root / "projects" / project_id
        if project_src_orig.is_dir():
            project_src = project_src_orig
        else:
            return (
                False,
                f"✗ Project directory not found: {project_src} or {project_src_orig}",
            )

    # 3. Resolve harness root
    if harness_root is None:
        resolved_root = Path.home() / "Dropbox" / "Projects" / "kbu-harness"
    else:
        resolved_root = harness_root.resolve() if harness_root.is_absolute() else (Path.cwd() / harness_root).resolve()

    resolved_root.mkdir(parents=True, exist_ok=True)
    harness_dir = resolved_root / pid

    # 4. Refuse if non-empty unless --force
    if harness_dir.exists() and any(harness_dir.iterdir()):
        if not force:
            return (
                False,
                f"✗ Target harness dir is non-empty: {harness_dir}\n"
                "  Use --force to overwrite, or run `kbu harness pull` to update.",
            )
        echo(f"   --force: removing existing {harness_dir}")
        shutil.rmtree(str(harness_dir))

    harness_dir.mkdir(parents=True, exist_ok=True)

    # 5. git init
    echo("── git init")
    result = subprocess.run(
        ["git", "init", str(harness_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False, f"✗ git init failed: {result.stderr.strip()}"
    echo(f"   ✓ git init: {harness_dir}")

    # 6. Mirror dirs
    echo("── scaffold directories")
    for d in ["notebooks", "data", "user_data", "figures"]:
        (harness_dir / d).mkdir(exist_ok=True)
        echo(f"   ✓ {d}/")

    # 7. .gitignore
    echo("── .gitignore")
    gitignore_content = (
        ".venv/\n"
        "__pycache__/\n"
        "*.egg-info/\n"
        ".ipynb_checkpoints/\n"
        ".DS_Store\n"
        "**/.kbcache/\n"
    )
    (harness_dir / ".gitignore").write_text(gitignore_content, encoding="utf-8")
    echo("   ✓ .gitignore written")

    # 8. Empty DEVLOG.md
    (harness_dir / "DEVLOG.md").write_text("", encoding="utf-8")
    echo("   ✓ DEVLOG.md created (empty)")

    # 9. Write preliminary harness.toml (python path added later)
    created_at = _utc_now()
    kbu_ver = _get_kbutillib_version()
    cfg = HarnessConfig(
        beril_root=str(beril_root),
        harness_root=str(resolved_root),
        project_id=pid,
        created_at=created_at,
        kbutillib_version=kbu_ver,
        python=None,
    )
    save_config(harness_dir, cfg)
    echo("   ✓ harness.toml written")

    # 10. Copy kbu-run skill bundle
    echo("── copy kbu-run skill bundle")
    try:
        skills_src = _harness_skills_root() / "kbu-run"
        skills_dest = harness_dir / ".claude" / "skills" / "kbu-run"
        skills_dest.parent.mkdir(parents=True, exist_ok=True)
        if skills_dest.exists():
            shutil.rmtree(str(skills_dest))
        shutil.copytree(str(skills_src), str(skills_dest))
        echo(f"   ✓ kbu-run → {skills_dest}")
    except Exception as exc:  # noqa: BLE001
        return False, f"✗ Failed to copy kbu-run skill bundle: {exc}"

    # 11. Copy / render preferences.md
    echo("── preferences.md")
    prefs_dest = harness_dir / ".claude" / "kbu" / "preferences.md"
    prefs_dest.parent.mkdir(parents=True, exist_ok=True)
    prefs_src_beril = beril_root / ".claude" / "kbu" / "preferences.md"
    if prefs_src_beril.is_file():
        shutil.copy2(str(prefs_src_beril), str(prefs_dest))
        echo(f"   ✓ preferences.md copied from BERIL: {prefs_dest}")
    else:
        # Render bundled default
        try:
            bundled = _bundled_preferences_template()
            shutil.copy2(str(bundled), str(prefs_dest))
            echo(f"   ✓ preferences.md rendered from template: {prefs_dest}")
        except FileNotFoundError as exc:
            echo(f"   ✗ preferences.md template not found: {exc} — skipping")

    # 12. Build venv
    echo("── build venv")
    interpreter = _build_venv(harness_dir, pid, echo=echo)
    if interpreter is None:
        echo("   ✗ venv creation failed — continuing without venv")
    else:
        # pip -U pip wheel
        echo("── pip install -U pip wheel")
        _pip_run(interpreter, ["install", "-U", "pip", "wheel"], echo=echo)

        # pip install kbutillib (skip in source checkout mode)
        if _is_source_checkout():
            echo("── pip install kbutillib: SKIPPED (running from source checkout)")
        else:
            echo("── pip install kbutillib")
            ok = _pip_run(interpreter, ["install", "kbutillib"], echo=echo)
            if ok:
                echo("   ✓ kbutillib installed")
            else:
                echo("   ✗ kbutillib install failed — doctor will report this")

        # pip install requirements.txt if present
        req_file = project_src / "requirements.txt"
        if req_file.is_file():
            echo("── pip install -r requirements.txt")
            ok = _pip_run(interpreter, ["install", "-r", str(req_file)], echo=echo)
            if ok:
                echo("   ✓ requirements.txt installed")
            else:
                echo("   ✗ requirements.txt install failed")

        # Record python path in harness.toml
        cfg.python = str(interpreter)
        save_config(harness_dir, cfg)
        echo(f"   ✓ python path recorded: {interpreter}")

    # 13. Initial pull
    echo("── initial pull")
    ok, msg = _pull(harness_dir, dry_run=False, force=True, exclude_kbcache=False, echo=echo)
    if not ok:
        echo(f"   ✗ initial pull failed: {msg}")
        # Not fatal — harness is still usable, user can run pull manually
    else:
        echo("   ✓ initial pull complete")

    return True, str(harness_dir)


# ---------------------------------------------------------------------------
# Time helper
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    """Return current UTC time as ISO-8601 with trailing Z."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
