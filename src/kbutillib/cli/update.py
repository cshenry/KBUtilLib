"""``kbu update`` — pull template updates from the parent KBUtilLib install.

Diffs ``.claude/commands/`` and ``.vscode/`` between the recorded
``last_pulled_commit`` and the current HEAD of the parent KBUtilLib source.
Warns before clobbering locally-modified template files.

v1 tier-2 command — must be run from inside a project created by
``kbu new-project`` (i.e., a directory containing ``kbu-project.toml``).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import click

from .manifest import (
    now_utc_iso,
    read_project_manifest,
    sha256_file,
    write_project_manifest,
)


# ---------------------------------------------------------------------------
# Template diff dataclass
# ---------------------------------------------------------------------------


@dataclass
class TemplateDiff:
    """A single file-level difference between template versions."""

    path: str               # relative to template root (forward-slash)
    status: str             # "added" | "modified" | "deleted"
    old_hash: Optional[str]  # sha256:... or None if new
    new_hash: Optional[str]  # sha256:... or None if deleted


# ---------------------------------------------------------------------------
# Tracked template paths (relative to templates/student-project/)
# ---------------------------------------------------------------------------

_TRACKED_TEMPLATE_SUBDIRS = [".claude/commands", ".vscode"]

#: Same paths relative to project root (after copy).
_TRACKED_PROJECT_DIRS = [".claude/commands", ".vscode"]


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------


def _run_git(*args: str, cwd: Path, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _git_rev_parse_head(repo: Path) -> str:
    """Return the current HEAD commit SHA for *repo*, or empty string."""
    result = _run_git("rev-parse", "HEAD", cwd=repo)
    if result.returncode == 0:
        return result.stdout.strip()
    return ""


def _git_show_file(repo: Path, commit: str, relpath: str) -> Optional[bytes]:
    """Return the content of *relpath* at *commit* in *repo*, or None if absent."""
    result = subprocess.run(
        ["git", "show", f"{commit}:{relpath}"],
        cwd=str(repo),
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout
    return None


# ---------------------------------------------------------------------------
# hash helpers
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    """Return SHA-256 hex of *data*."""
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _prefixed(hex_str: str) -> str:
    """Return ``sha256:<hex>``."""
    return f"sha256:{hex_str}"


# ---------------------------------------------------------------------------
# Diff computation
# ---------------------------------------------------------------------------


def _build_diff(
    source: Path,
    last_commit: Optional[str],
    current_commit: str,
) -> list[TemplateDiff]:
    """Build a TemplateDiff list comparing last_commit state vs current HEAD.

    Walks ``templates/student-project/.claude/commands/`` and
    ``templates/student-project/.vscode/`` in the source repo.

    ``last_commit`` being None (first pull) treats all files as "added".
    """
    template_root_rel = "templates/student-project"
    diffs: list[TemplateDiff] = []

    # Build sets of relative paths (relative to template root, forward-slash)
    # for both old commit and new (current files on disk).

    # Current files: walk from source / templates/student-project
    template_abs = source / "templates" / "student-project"
    current_files: dict[str, Path] = {}
    for tracked_sub in _TRACKED_TEMPLATE_SUBDIRS:
        d = template_abs / tracked_sub
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*")):
            if f.is_file():
                rel = f.relative_to(template_abs)
                current_files[rel.as_posix()] = f

    # Old files: from last_commit via git show
    old_hashes: dict[str, str] = {}
    if last_commit:
        for rel_path in current_files:
            full_relpath = f"{template_root_rel}/{rel_path}"
            data = _git_show_file(source, last_commit, full_relpath)
            if data is not None:
                old_hashes[rel_path] = _sha256_bytes(data)

        # Also check if any files existed at last_commit but are now gone
        # We do this by looking at git diff --name-status
        result = _run_git(
            "diff",
            "--name-status",
            last_commit,
            current_commit,
            "--",
            f"{template_root_rel}/.claude/",
            f"{template_root_rel}/.vscode/",
            cwd=source,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if not line.strip():
                    continue
                parts = line.split("\t", 1)
                if len(parts) != 2:
                    continue
                status_letter, git_path = parts[0].strip(), parts[1].strip()
                # Convert full git path to rel_path (relative to template root)
                prefix = template_root_rel + "/"
                if not git_path.startswith(prefix):
                    continue
                rel_path = git_path[len(prefix):]

                if status_letter == "D":
                    # File deleted from template
                    old_data = _git_show_file(source, last_commit, git_path)
                    old_h = _prefixed(_sha256_bytes(old_data)) if old_data else None
                    diffs.append(TemplateDiff(
                        path=rel_path,
                        status="deleted",
                        old_hash=old_h,
                        new_hash=None,
                    ))
    else:
        # No last_commit: treat all current files as added
        pass  # old_hashes is empty; all files appear as "added" below

    # Compare current files against old hashes
    for rel_path, abs_path in current_files.items():
        new_hex = sha256_file(abs_path)
        new_h = _prefixed(new_hex)
        if rel_path not in old_hashes:
            # File is new (added)
            diffs.append(TemplateDiff(
                path=rel_path,
                status="added",
                old_hash=None,
                new_hash=new_h,
            ))
        else:
            # Check if modified
            old_h = _prefixed(old_hashes[rel_path])
            if new_hex != old_hashes[rel_path]:
                diffs.append(TemplateDiff(
                    path=rel_path,
                    status="modified",
                    old_hash=old_h,
                    new_hash=new_h,
                ))
            # If unchanged, no diff entry

    # Deduplicate (deleted entries added above; filter out added dupes)
    # Ensure we don't double-list deleted files
    seen_deleted = {d.path for d in diffs if d.status == "deleted"}
    diffs = [d for d in diffs if not (d.status in ("added", "modified") and d.path in seen_deleted)]

    return diffs


# ---------------------------------------------------------------------------
# Locally-modified detection
# ---------------------------------------------------------------------------


def _detect_locally_modified(
    project_root: Path,
    file_hashes: dict[str, str],
    diff: list[TemplateDiff],
) -> list[str]:
    """Return list of rel paths that the diff would overwrite AND are locally modified.

    A file is locally modified if its current on-disk hash differs from the
    recorded hash in ``[update.file_hashes]``.
    """
    # The diff tells us which files would be written (added or modified)
    would_write = {d.path for d in diff if d.status in ("added", "modified")}
    locally_modified: list[str] = []
    for rel_path, recorded_hash in file_hashes.items():
        if rel_path not in would_write:
            continue
        on_disk = project_root / rel_path
        if not on_disk.is_file():
            continue
        current_hex = sha256_file(on_disk)
        current_prefixed = _prefixed(current_hex)
        if current_prefixed != recorded_hash:
            locally_modified.append(rel_path)
    return locally_modified


# ---------------------------------------------------------------------------
# Apply diff
# ---------------------------------------------------------------------------


def _apply_diff(
    source: Path,
    diff: list[TemplateDiff],
    project_root: Path,
) -> None:
    """Apply *diff* by copying/deleting files between source template and project."""
    template_abs = source / "templates" / "student-project"
    for entry in diff:
        dest = project_root / entry.path
        if entry.status in ("added", "modified"):
            src_file = template_abs / entry.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            import shutil as _shutil
            _shutil.copy2(str(src_file), str(dest))
        elif entry.status == "deleted":
            if dest.is_file():
                dest.unlink()


# ---------------------------------------------------------------------------
# Recompute file_hashes after update
# ---------------------------------------------------------------------------


def _recompute_file_hashes(project_root: Path) -> dict[str, str]:
    """Recompute hashes for all tracked files in the project root."""
    hashes: dict[str, str] = {}
    for tracked_dir in _TRACKED_PROJECT_DIRS:
        d = project_root / tracked_dir
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*")):
            if f.is_file():
                rel = f.relative_to(project_root).as_posix()
                hashes[rel] = _prefixed(sha256_file(f))
    return hashes


# ---------------------------------------------------------------------------
# Format diff summary
# ---------------------------------------------------------------------------


def _format_diff_summary(diff: list[TemplateDiff]) -> str:
    """Return a human-readable summary of *diff*."""
    if not diff:
        return "Already up-to-date."
    lines = [f"Template diff ({len(diff)} file(s)):"]
    for entry in sorted(diff, key=lambda d: d.path):
        lines.append(f"  [{entry.status.upper():8s}] {entry.path}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core update logic
# ---------------------------------------------------------------------------


def update(  # noqa: C901
    set_source: Optional[Path] = None,
    check: bool = False,
    yes: bool = False,
    project_root: Optional[Path] = None,
) -> None:
    """Pull template updates from the parent KBUtilLib install.

    *project_root* defaults to ``Path.cwd()`` (used in tests to override).
    """
    if check and yes:
        click.echo(
            "Error: --check and --yes are mutually exclusive.",
            err=True,
        )
        sys.exit(1)

    if project_root is None:
        project_root = Path.cwd()

    # Read manifest
    try:
        cfg = read_project_manifest(project_root)
    except FileNotFoundError:
        click.echo(
            f"Error: kbu-project.toml not found in {project_root}. "
            "Run from inside a kbu project directory.",
            err=True,
        )
        sys.exit(1)

    # --set-source: relocate source_path and clear last_pulled_commit
    if set_source is not None:
        if "kbutillib" not in cfg:
            cfg["kbutillib"] = {}
        cfg["kbutillib"]["source_path"] = str(set_source.resolve())
        if "update" not in cfg:
            cfg["update"] = {}
        cfg["update"]["last_pulled_commit"] = ""
        write_project_manifest(project_root, cfg)
        click.echo(f"Source path updated to: {set_source.resolve()}")
        return

    # Resolve source path
    source_path_str = cfg.get("kbutillib", {}).get("source_path", "")
    if not source_path_str:
        click.echo(
            "Error: [kbutillib].source_path not set in kbu-project.toml. "
            "Run `kbu update --set-source <path>`.",
            err=True,
        )
        sys.exit(1)

    source = Path(source_path_str)
    if not source.exists():
        click.echo(
            f"Parent KBUtilLib not found at {source}. "
            "Run `kbu update --set-source <new-path>`.",
            err=True,
        )
        sys.exit(1)

    # Pull source repo if it's a git repo (best-effort)
    if (source / ".git").is_dir():
        pull_result = _run_git("pull", cwd=source)
        if pull_result.returncode != 0:
            click.echo(
                f"Warning: git pull on source failed: {pull_result.stderr.strip()}",
                err=True,
            )

    # Get current commit
    current_commit = _git_rev_parse_head(source)

    # Get last pulled commit
    last_commit = cfg.get("update", {}).get("last_pulled_commit") or None
    if last_commit == "":
        last_commit = None

    # Build diff
    diff = _build_diff(source, last_commit, current_commit)

    if not diff:
        click.echo("Already up-to-date.")
        return

    click.echo(_format_diff_summary(diff))

    if check:
        return

    # Detect locally-modified files that would be overwritten
    file_hashes = cfg.get("update", {}).get("file_hashes", {})
    locally_modified = _detect_locally_modified(project_root, file_hashes, diff)

    if locally_modified and not yes:
        click.echo("\nWARNING: these files were modified locally and will be overwritten:")
        for f in locally_modified:
            click.echo(f"  {f}")
        answer = click.prompt("Overwrite locally-modified files? (y/n)")
        if answer.strip().lower() != "y":
            click.echo("Update aborted.")
            return

    # Apply diff
    _apply_diff(source, diff, project_root)

    # Recompute file_hashes and update manifest
    new_hashes = _recompute_file_hashes(project_root)
    now = now_utc_iso()

    if "update" not in cfg:
        cfg["update"] = {}
    cfg["update"]["last_pulled_at"] = now
    cfg["update"]["last_pulled_commit"] = current_commit
    cfg["update"]["file_hashes"] = new_hashes
    write_project_manifest(project_root, cfg)

    click.echo(f"\nUpdate applied. last_pulled_commit -> {current_commit[:12]}...")


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command("update")
@click.option("--set-source", "set_source", default=None, type=click.Path(path_type=Path),
              help="Relocate parent KBUtilLib source path.")
@click.option("--check", is_flag=True, default=False, help="Dry-run; print diff without applying.")
@click.option("--yes", is_flag=True, default=False, help="Bypass all overwrite prompts.")
def update_command(
    set_source: Optional[Path],
    check: bool,
    yes: bool,
) -> None:
    """Pull template updates from the parent KBUtilLib install.

    Must be run from inside a kbu project directory (one with kbu-project.toml).
    """
    update(set_source=set_source, check=check, yes=yes)
