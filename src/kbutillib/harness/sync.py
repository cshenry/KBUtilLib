"""pull / push rsync helpers for the kbu harness.

pull: <BERIL_ROOT>/projects/<id>/ → <harness>/
push: <harness>/ → <BERIL_ROOT>/projects/<id>/

rsync -aH --delete --info=stats2 <excludes>

Excludes (always): .git/ .venv/ __pycache__/ .ipynb_checkpoints/ .DS_Store
.kbcache/ is included by default; --exclude-kbcache opts it out.

Trailing slashes on the project dir (both sides) contain --delete to that
subtree and prevent the parent directory itself from being affected.

Preferences sync is one-way BERIL→harness only; pull refreshes
.claude/kbu/preferences.md when the BERIL source is newer (mtime > harness
by >= 1 second); never overwrites local harness edits unless --force;
push never touches preferences.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from .config import HarnessConfig, find_harness_toml, load_config


# ---------------------------------------------------------------------------
# rsync availability probe
# ---------------------------------------------------------------------------


def _require_rsync(echo: Callable) -> Optional[str]:
    """Return rsync path or print error and return None."""
    path = shutil.which("rsync")
    if path is None:
        echo("✗ rsync not found on PATH")
        return None
    return path


# ---------------------------------------------------------------------------
# Build rsync command
# ---------------------------------------------------------------------------


def _rsync_supports_info_stats2() -> bool:
    """Return True if the local rsync binary supports --info=stats2.

    rsync 3.x supports it; the macOS-bundled 2.6.9 / openrsync does not.
    We probe by running rsync --info=stats2 /dev/null /dev/null and checking
    for the 'unrecognized option' error.
    """
    result = subprocess.run(
        ["rsync", "--info=stats2", "/dev/null", "/dev/null"],
        capture_output=True,
        text=True,
        check=False,
    )
    # rc=11 (no such file) or rc=23 (partial) = supported; rc=1 + unrecognized = not
    if "unrecognized option" in (result.stderr or "").lower():
        return False
    return True


_RSYNC_INFO_STATS2: Optional[bool] = None


def _info_stats2_flag() -> list[str]:
    """Return ['--info=stats2'] if rsync supports it, else []."""
    global _RSYNC_INFO_STATS2
    if _RSYNC_INFO_STATS2 is None:
        _RSYNC_INFO_STATS2 = _rsync_supports_info_stats2()
    return ["--info=stats2"] if _RSYNC_INFO_STATS2 else []


def _build_rsync_cmd(
    src: str,
    dest: str,
    exclude_kbcache: bool = False,
    dry_run: bool = False,
    protect_harness_files: bool = False,
) -> list[str]:
    """Build the rsync argument list.

    *protect_harness_files*: when True (pull direction), exclude harness-side
    files that should never be deleted by --delete (harness.toml, DEVLOG.md,
    .claude/, activate.sh).  These are harness-specific and don't exist in the
    BERIL project subtree.
    """
    cmd = [
        "rsync",
        "-aH",
        "--delete",
    ] + _info_stats2_flag() + [
        "--exclude",
        ".git/",
        "--exclude",
        ".venv/",
        "--exclude",
        "__pycache__/",
        "--exclude",
        ".ipynb_checkpoints/",
        "--exclude",
        ".DS_Store",
    ]
    if protect_harness_files:
        # These exist in the harness root but not in BERIL projects/<id>/ and
        # must not be deleted by --delete during pull.
        # This includes: harness metadata, harness scaffold dirs not in BERIL,
        # and the .gitignore we wrote.
        cmd += [
            "--exclude",
            "harness.toml",
            "--exclude",
            "DEVLOG.md",
            "--exclude",
            ".gitignore",
            "--exclude",
            ".claude/",
            "--exclude",
            "activate.sh",
            "--exclude",
            "user_data/",
        ]
    if exclude_kbcache:
        cmd += ["--exclude", ".kbcache/"]
    if dry_run:
        cmd += ["--dry-run", "--itemize-changes"]
    cmd += [src, dest]
    return cmd


# ---------------------------------------------------------------------------
# Preferences sync helper (one-way BERIL→harness)
# ---------------------------------------------------------------------------


def _sync_preferences(
    beril_root: Path,
    harness_dir: Path,
    force: bool = False,
    echo: Callable = print,
) -> None:
    """Copy BERIL preferences.md to harness when BERIL is newer (mtime >= 1s ahead)."""
    prefs_src = beril_root / ".claude" / "kbu" / "preferences.md"
    prefs_dest = harness_dir / ".claude" / "kbu" / "preferences.md"

    if not prefs_src.is_file():
        return  # Nothing to sync

    if prefs_dest.is_file() and not force:
        # Only overwrite if BERIL is strictly newer (>= 1s)
        src_mtime = prefs_src.stat().st_mtime
        dest_mtime = prefs_dest.stat().st_mtime
        if src_mtime - dest_mtime < 1.0:
            return  # harness copy is current or newer — preserve local edits

    prefs_dest.parent.mkdir(parents=True, exist_ok=True)
    import shutil as _shutil
    _shutil.copy2(str(prefs_src), str(prefs_dest))
    echo(f"   ✓ preferences.md synced from BERIL → {prefs_dest}")


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------


def pull(
    harness_dir: Path,
    dry_run: bool = False,
    force: bool = False,
    exclude_kbcache: bool = False,
    echo: Callable = print,
) -> tuple[bool, str]:
    """rsync BERIL project → harness.

    Returns (ok, detail).
    """
    rsync = _require_rsync(echo)
    if rsync is None:
        return False, "rsync not found"

    # Load config
    try:
        cfg = load_config(harness_dir)
    except (FileNotFoundError, ValueError) as exc:
        return False, f"Cannot load harness.toml: {exc}"

    beril_root = Path(cfg.beril_root)
    project_src = beril_root / "projects" / cfg.project_id
    if not project_src.is_dir():
        return False, f"✗ Source project directory not found: {project_src}"

    # Safety: refuse if harness worktree has uncommitted changes (unless --force)
    if not dry_run and not force:
        dirty = _git_has_uncommitted_changes(harness_dir)
        if dirty:
            return (
                False,
                f"✗ Harness has uncommitted changes (git status --porcelain is non-empty). "
                "Run `git commit` or use --force to override.",
            )

    # Trailing slash on both src and dest to contain --delete
    src = str(project_src).rstrip("/") + "/"
    dest = str(harness_dir).rstrip("/") + "/"

    cmd = _build_rsync_cmd(
        src, dest, exclude_kbcache=exclude_kbcache, dry_run=dry_run,
        protect_harness_files=True,
    )
    echo(f"   command: {' '.join(cmd)}")

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode not in (0, 24):  # 24 = some files vanished (ok)
        detail = (result.stderr or result.stdout or "").strip()[:500]
        return False, f"rsync failed (rc={result.returncode}): {detail}"

    # Sync preferences (one-way, not in dry-run)
    if not dry_run:
        _sync_preferences(beril_root, harness_dir, force=force, echo=echo)

    return True, (result.stdout or "").strip()


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


def push(
    harness_dir: Path,
    dry_run: bool = False,
    force: bool = False,
    exclude_kbcache: bool = False,
    echo: Callable = print,
) -> tuple[bool, str]:
    """rsync harness → BERIL project.

    Returns (ok, detail).
    """
    rsync = _require_rsync(echo)
    if rsync is None:
        return False, "rsync not found"

    try:
        cfg = load_config(harness_dir)
    except (FileNotFoundError, ValueError) as exc:
        return False, f"Cannot load harness.toml: {exc}"

    beril_root = Path(cfg.beril_root)
    project_dest = beril_root / "projects" / cfg.project_id
    if not project_dest.is_dir():
        return False, f"✗ Destination project directory not found: {project_dest}"

    # Safety: refuse if BERIL project has incoming changes (unless --force)
    if not dry_run and not force:
        # Check via rsync dry-run whether BERIL→harness would transfer anything
        prefs_src_beril = beril_root / "projects" / cfg.project_id
        src_check = str(prefs_src_beril).rstrip("/") + "/"
        dest_check = str(harness_dir).rstrip("/") + "/"
        check_cmd = _build_rsync_cmd(
            src_check, dest_check, exclude_kbcache=exclude_kbcache, dry_run=True
        )
        check_result = subprocess.run(
            check_cmd, capture_output=True, text=True, check=False
        )
        check_out = (check_result.stdout or "").strip()
        # itemize-changes lines start with '>' (send) or '<' (receive) or 'c' (change)
        incoming_lines = [
            line
            for line in check_out.splitlines()
            if line and not line.startswith(" ") and not line.startswith(".")
            and len(line) > 1 and line[0] in "><c"
        ]
        if incoming_lines:
            return (
                False,
                f"✗ BERIL project has changes not yet pulled to harness "
                f"({len(incoming_lines)} items). Run `kbu harness pull` first, or use --force.",
            )

    # Trailing slash on both src and dest
    src = str(harness_dir).rstrip("/") + "/"
    dest = str(project_dest).rstrip("/") + "/"

    cmd = _build_rsync_cmd(src, dest, exclude_kbcache=exclude_kbcache, dry_run=dry_run)
    echo(f"   command: {' '.join(cmd)}")

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode not in (0, 24):
        detail = (result.stderr or result.stdout or "").strip()[:500]
        return False, f"rsync failed (rc={result.returncode}): {detail}"

    # push NEVER writes preferences back to BERIL
    return True, (result.stdout or "").strip()


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git_has_uncommitted_changes(harness_dir: Path) -> bool:
    """True if `git status --porcelain` in harness_dir is non-empty."""
    result = subprocess.run(
        ["git", "-C", str(harness_dir), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # Not a git repo or git not available — don't block
        return False
    return bool(result.stdout.strip())
