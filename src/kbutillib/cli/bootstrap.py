"""``kbu bootstrap`` — retrofit kbu-awareness onto an existing git repository.

Unlike ``kbu new-project``, bootstrap runs in cwd and is additive: it never
deletes user content, never auto-commits, and reuses an existing venv when
one is detected.

Preconditions
-------------
1. ``(Path.cwd() / ".git").exists()`` must be True.
2. ``(Path.cwd() / "kbu-project.toml")`` must NOT exist.

On success writes ``kbu-project.toml`` with ``[project].bootstrapped=true``.

v1 targets macOS only.  On non-macOS without ``KBU_PLATFORM_OVERRIDE=force``,
prints the bootstrap macOS-only message and exits 1.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click

from .manifest import (
    now_utc_iso,
    sha256_file,
    write_project_manifest,
)
from ._template_ops import (
    parse_virtual_env_from_activate as _parse_virtual_env_from_activate,
    run_venvman_project as _run_venvman_project,
    create_plain_venv as _create_plain_venv,
)
from ..layout import DEFAULT_SHARED_DIRS


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: macOS-only message specific to bootstrap (has --no-venv escape).
_BOOTSTRAP_MACOS_ONLY_MESSAGE = (
    "v1 currently targets macOS. Linux/Windows support is planned for v2.\n"
    "To use kbu manually on this platform: pip install -e <path-to-KBUtilLib>\n"
    "into your existing venv, then run `kbu bootstrap --no-venv` to copy\n"
    "templates and write kbu-project.toml. Tier-2 skills work cross-platform\n"
    "once the template files are in place."
)

#: Gitignore marker block (exactly as specified in the PRD).
_GITIGNORE_MARKER_OPEN = "# >>> kbu-managed >>>"
_GITIGNORE_MARKER_CLOSE = "# <<< kbu-managed <<<"
_GITIGNORE_BLOCK = """\
# >>> kbu-managed >>>
.venv/
venv/
.ipynb_checkpoints/
nboutput/
.kbcache/
__pycache__/
*.egg-info/
# <<< kbu-managed <<<
"""

#: The `.claude/commands/` skill files bootstrap manages.
#: Note: kbu-migrate.md is listed here but the template file is written by
#: the p4-kbu-migrate-skill task. Bootstrap skips missing source files silently.
_CLAUDE_COMMAND_FILES = [
    ".claude/commands/kbu-start.md",
    ".claude/commands/kbu-plan.md",
    ".claude/commands/kbu-build.md",
    ".claude/commands/kbu-run.md",
    ".claude/commands/kbu-synthesize.md",
    ".claude/commands/kbu-update.md",
    ".claude/commands/kbu-migrate.md",
]

#: The `.claude/agents/` subagent files bootstrap manages.
_CLAUDE_AGENT_FILES = [
    ".claude/agents/kbu-sub-literature-review.md",
    ".claude/agents/kbu-sub-review.md",
    ".claude/agents/kbu-sub-diagnose.md",
    ".claude/agents/kbu-sub-build.md",
]

#: Full closed set of template entries bootstrap handles.
_TEMPLATE_ENTRIES = _CLAUDE_COMMAND_FILES + _CLAUDE_AGENT_FILES + [
    ".vscode/extensions.json",
    "subprojects/.gitkeep",
    "{{project_name}}.code-workspace",
    ".gitignore",
    "README.md",
]


# ---------------------------------------------------------------------------
# KBUTILLIB_ROOT resolution
# ---------------------------------------------------------------------------


def _kbutillib_root() -> Path:
    """Return the absolute path to the KBUtilLib repo root.

    src/kbutillib/cli/bootstrap.py → cli → kbutillib → src → repo_root
    """
    return Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------


def _is_macos_or_override() -> bool:
    """Return True if running on macOS or KBU_PLATFORM_OVERRIDE=force is set."""
    return sys.platform == "darwin" or os.environ.get("KBU_PLATFORM_OVERRIDE") == "force"


def _is_darwin() -> bool:
    return sys.platform == "darwin"


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------


def _git_head_commit(repo_root: Path) -> str:
    """Return the current HEAD SHA of *repo_root*, or empty string."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except OSError:
        pass
    return ""


def _git_config_user_name(cwd: Path) -> str:
    """Return git config user.name from *cwd*, or empty string."""
    try:
        result = subprocess.run(
            ["git", "config", "user.name"],
            cwd=str(cwd),
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
# venv probe helpers
# ---------------------------------------------------------------------------


def _probe_venv(cwd: Path) -> Optional[Path]:
    """Probe for an existing venv in the fixed order.

    Returns the python binary path of the first detected venv, or None.

    Probe order:
    1. $VIRTUAL_ENV env var → <VIRTUAL_ENV>/bin/python
    2. <cwd>/activate.sh   → parsed VIRTUAL_ENV → <dir>/bin/python
    3. <cwd>/.venv/bin/python
    4. <cwd>/venv/bin/python
    """
    # 1. $VIRTUAL_ENV
    virtual_env = os.environ.get("VIRTUAL_ENV", "")
    if virtual_env:
        candidate = Path(virtual_env) / "bin" / "python"
        if candidate.exists():
            return candidate

    # 2. activate.sh
    activate_sh = cwd / "activate.sh"
    if activate_sh.exists():
        venv_dir = _parse_virtual_env_from_activate(activate_sh)
        if venv_dir:
            candidate = venv_dir / "bin" / "python"
            if candidate.exists():
                return candidate

    # 3. .venv/bin/python
    candidate = cwd / ".venv" / "bin" / "python"
    if candidate.exists():
        return candidate

    # 4. venv/bin/python
    candidate = cwd / "venv" / "bin" / "python"
    if candidate.exists():
        return candidate

    return None


def _python_version(python: Path) -> tuple[int, int]:
    """Return (major, minor) of the python binary at *python*, or (0, 0) on error."""
    try:
        result = subprocess.run(
            [str(python), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split(".")
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
    except (OSError, ValueError):
        pass
    return (0, 0)


# ---------------------------------------------------------------------------
# File-conflict helpers
# ---------------------------------------------------------------------------


def _now_bak_suffix() -> str:
    """Return a POSIX-safe UTC timestamp suffix: YYYYMMDDTHHMMSSZ."""
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _read_template_file_bytes(template_src: Path, rel_path: str, name: str) -> Optional[bytes]:
    """Read a template file and apply {{project_name}} substitution to its content.

    Returns bytes after substitution, or None if the file does not exist.
    """
    src = template_src / rel_path
    if not src.exists():
        return None
    try:
        text = src.read_text(encoding="utf-8")
        text = text.replace("{{project_name}}", name)
        return text.encode("utf-8")
    except UnicodeDecodeError:
        return src.read_bytes()


def _sha256_bytes(data: bytes) -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Gitignore append
# ---------------------------------------------------------------------------


def _handle_gitignore(cwd: Path, check: bool) -> str:
    """Append kbu marker block to .gitignore if not already present.

    Returns an action string: 'created', 'appended', 'skipped'.
    Under *check*, returns the same string but makes no filesystem changes.
    """
    gi_path = cwd / ".gitignore"

    if not gi_path.exists():
        if not check:
            gi_path.write_text(_GITIGNORE_BLOCK, encoding="utf-8")
        return "created"

    # File exists: check for marker
    try:
        existing = gi_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        existing = ""

    if _GITIGNORE_MARKER_OPEN in existing:
        return "skipped"

    # Append with proper blank-line spacing
    if not check:
        if existing.endswith("\n\n") or existing.endswith("\n\n\n"):
            new_content = existing + _GITIGNORE_BLOCK
        elif existing.endswith("\n"):
            new_content = existing + "\n" + _GITIGNORE_BLOCK
        else:
            new_content = existing + "\n\n" + _GITIGNORE_BLOCK
        gi_path.write_text(new_content, encoding="utf-8")
    return "appended"


# ---------------------------------------------------------------------------
# Core bootstrap orchestration
# ---------------------------------------------------------------------------


def bootstrap(  # noqa: C901 — orchestration function
    name: str,
    author: Optional[str],
    affiliation: Optional[str],
    orcid: Optional[str],
    first_subproject: Optional[str],
    no_venv: bool,
    no_kernel: bool,
    force_overwrite: bool,
    force_venv: bool,
    check: bool,
    project_root: Optional[Path] = None,
) -> None:
    """Retrofit kbu-awareness onto an existing git repository.

    *project_root* defaults to ``Path.cwd()``. Used by tests to override.
    """
    if project_root is None:
        project_root = Path.cwd()

    # ------------------------------------------------------------------
    # Precondition 1: must be inside a git repository
    # ------------------------------------------------------------------
    if not (project_root / ".git").exists():
        click.echo(
            f"Error: kbu bootstrap must run inside a git repository. cwd={project_root}",
            err=True,
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # Precondition 2: must not already have kbu-project.toml
    # ------------------------------------------------------------------
    if (project_root / "kbu-project.toml").exists():
        click.echo(
            "Error: repo is already kbu-aware (kbu-project.toml present). "
            "Did you mean to run kbu update?",
            err=True,
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # Platform gate
    # ------------------------------------------------------------------
    if not _is_macos_or_override():
        click.echo(_BOOTSTRAP_MACOS_ONLY_MESSAGE)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Resolve KBUTILLIB_ROOT and template source
    # ------------------------------------------------------------------
    kbu_root = _kbutillib_root()
    template_src = kbu_root / "templates" / "research-project"

    # ------------------------------------------------------------------
    # Author triple resolution
    # ------------------------------------------------------------------
    # Under --check: never prompt; render TODOs instead.
    git_author = _git_config_user_name(project_root)

    if check:
        # Dry-run: collect info without prompting
        display_author = author or (git_author if git_author else "(TODO — would prompt: 'Author name')")
        display_affiliation = affiliation or "(TODO — would prompt: 'Author affiliation')"
        display_orcid = orcid or "(TODO — would prompt: 'Author ORCID')"

        # Print the dry-run plan
        click.echo(f"=== kbu bootstrap --check (dry run) in {project_root} ===\n")
        click.echo(f"Project name:  {name}")
        click.echo(f"Author:        {display_author}")
        click.echo(f"Affiliation:   {display_affiliation}")
        click.echo(f"ORCID:         {display_orcid}")
        click.echo("")

        # File-by-file plan
        click.echo("File actions (would perform):")
        _print_check_file_plan(
            project_root=project_root,
            template_src=template_src,
            name=name,
            force_overwrite=force_overwrite,
        )

        # .gitignore plan
        gi_action = _check_gitignore_action(project_root)
        click.echo(f"  .gitignore: {gi_action}")

        # venv plan
        if no_venv:
            click.echo("\nvenv: --no-venv set; skipping venv work")
        else:
            _print_check_venv_plan(project_root=project_root, name=name, force_venv=force_venv)

        click.echo("\nManifest: would write kbu-project.toml ([project].bootstrapped=true)")

        if first_subproject:
            click.echo(f"First subproject: would create first subproject `{first_subproject}` after bootstrap")

        return

    # ------------------------------------------------------------------
    # Non-check: resolve author fields (prompt if needed)
    # ------------------------------------------------------------------
    if author is None:
        if git_author:
            author = git_author
        else:
            author = click.prompt("Author name")
    if affiliation is None:
        affiliation = click.prompt("Author affiliation")
    if orcid is None:
        orcid = click.prompt("Author ORCID")

    # ------------------------------------------------------------------
    # Per-file conflict loop
    # ------------------------------------------------------------------
    # Tracks which files were actually written (for manifest file_hashes).
    files_written: dict[str, str] = {}       # rel_path → sha256:<hex>
    files_backed_up: list[tuple[str, str]] = []   # (rel_path, bak_name)
    files_user_owned: list[str] = []         # rel_path (intentionally skipped)

    # Process .claude/commands/kbu-*.md files
    (project_root / ".claude" / "commands").mkdir(parents=True, exist_ok=True)

    for rel_path in _CLAUDE_COMMAND_FILES:
        dest = project_root / rel_path
        src = template_src / rel_path
        if not src.exists():
            # Template file missing; skip silently
            continue

        # Read template content with substitution
        src_content_bytes = _read_template_file_bytes(template_src, rel_path, name)
        if src_content_bytes is None:
            continue
        src_hash = "sha256:" + hashlib.sha256(src_content_bytes).hexdigest()

        if not dest.exists():
            # Absent → copy
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src_content_bytes)
            files_written[rel_path] = "sha256:" + sha256_file(dest)
        else:
            # Present: compare hashes
            dest_hash = "sha256:" + sha256_file(dest)
            if dest_hash == src_hash:
                # Identical → skip silently
                pass
            else:
                # Different hash → prompt or force
                if force_overwrite:
                    proceed = True
                else:
                    answer = click.prompt(
                        f"  {rel_path} differs from template. Overwrite? (will backup original)",
                        default="y",
                    )
                    proceed = answer.strip().lower() == "y"

                if proceed:
                    # Backup original
                    bak_suffix = _now_bak_suffix()
                    bak_name = f"{dest.name}.bak.{bak_suffix}"
                    bak_path = dest.parent / bak_name
                    shutil.copy2(str(dest), str(bak_path))
                    dest.write_bytes(src_content_bytes)
                    files_written[rel_path] = "sha256:" + sha256_file(dest)
                    files_backed_up.append((rel_path, bak_name))
                # else: user declined → leave as-is; not recorded

    # Process .claude/agents/kbu-sub-*.md files
    (project_root / ".claude" / "agents").mkdir(parents=True, exist_ok=True)

    for rel_path in _CLAUDE_AGENT_FILES:
        dest = project_root / rel_path
        src = template_src / rel_path
        if not src.exists():
            # Template file missing; skip silently
            continue

        # Read template content with substitution
        src_content_bytes = _read_template_file_bytes(template_src, rel_path, name)
        if src_content_bytes is None:
            continue
        src_hash = "sha256:" + hashlib.sha256(src_content_bytes).hexdigest()

        if not dest.exists():
            # Absent → copy
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src_content_bytes)
            files_written[rel_path] = "sha256:" + sha256_file(dest)
        else:
            # Present: compare hashes
            dest_hash = "sha256:" + sha256_file(dest)
            if dest_hash == src_hash:
                # Identical → skip silently
                pass
            else:
                # Different hash → prompt or force
                if force_overwrite:
                    proceed = True
                else:
                    answer = click.prompt(
                        f"  {rel_path} differs from template. Overwrite? (will backup original)",
                        default="y",
                    )
                    proceed = answer.strip().lower() == "y"

                if proceed:
                    # Backup original
                    bak_suffix = _now_bak_suffix()
                    bak_name = f"{dest.name}.bak.{bak_suffix}"
                    bak_path = dest.parent / bak_name
                    shutil.copy2(str(dest), str(bak_path))
                    dest.write_bytes(src_content_bytes)
                    files_written[rel_path] = "sha256:" + sha256_file(dest)
                    files_backed_up.append((rel_path, bak_name))
                # else: user declined → leave as-is; not recorded

    # .vscode/extensions.json — never overwritten
    vscode_json = project_root / ".vscode" / "extensions.json"
    vscode_src = template_src / ".vscode" / "extensions.json"
    if not vscode_json.exists() and vscode_src.exists():
        # Absent → copy
        vscode_json.parent.mkdir(parents=True, exist_ok=True)
        src_bytes = vscode_src.read_bytes()
        vscode_json.write_bytes(src_bytes)
        files_written[".vscode/extensions.json"] = "sha256:" + sha256_file(vscode_json)
    elif vscode_json.exists():
        # Present (any content) → skip + advise; NOT recorded in file_hashes
        click.echo(
            "  kept your existing .vscode/extensions.json; "
            "ensure `anthropic.claude-code` is in `recommendations`"
        )
        files_user_owned.append(".vscode/extensions.json")

    # subprojects/.gitkeep
    sp_dir = project_root / "subprojects"
    sp_gitkeep = sp_dir / ".gitkeep"
    if sp_dir.exists() and any(True for _ in sp_dir.iterdir()):
        # subprojects/ exists with content → skip entirely
        pass
    elif not sp_dir.exists():
        # Absent → create directory and write .gitkeep
        sp_dir.mkdir(parents=True, exist_ok=True)
        sp_gitkeep.write_text("", encoding="utf-8")
        files_written["subprojects/.gitkeep"] = "sha256:" + sha256_file(sp_gitkeep)
    else:
        # Empty directory → write .gitkeep
        sp_gitkeep.write_text("", encoding="utf-8")
        files_written["subprojects/.gitkeep"] = "sha256:" + sha256_file(sp_gitkeep)

    # README.md — skip if existing (don't clobber the user's project README;
    # only deploy when retrofitting a repo without one). Substitutes {{project_name}}.
    readme_dest = project_root / "README.md"
    readme_src = template_src / "README.md"
    if not readme_dest.exists() and readme_src.exists():
        src_bytes = _read_template_file_bytes(template_src, "README.md", name)
        if src_bytes is not None:
            readme_dest.write_bytes(src_bytes)
            files_written["README.md"] = "sha256:" + sha256_file(readme_dest)
    elif readme_dest.exists():
        click.echo("  kept your existing README.md")
        files_user_owned.append("README.md")

    # {{project_name}}.code-workspace — skip if any *.code-workspace exists at root
    existing_workspaces = list(project_root.glob("*.code-workspace"))
    ws_src = template_src / "{{project_name}}.code-workspace"
    if not existing_workspaces and ws_src.exists():
        # No existing workspace → copy with substitution
        ws_dest = project_root / f"{name}.code-workspace"
        src_bytes = _read_template_file_bytes(template_src, "{{project_name}}.code-workspace", name)
        if src_bytes is not None:
            ws_dest.write_bytes(src_bytes)
            files_written[f"{name}.code-workspace"] = "sha256:" + sha256_file(ws_dest)
    # else: skip (existing workspace or template absent)

    # Shared dirs (data/, models/, genomes/) — create with .gitkeep if absent
    for shared_dir in DEFAULT_SHARED_DIRS:
        sd = project_root / shared_dir
        sd_gitkeep = sd / ".gitkeep"
        if not sd.exists():
            sd.mkdir(parents=True, exist_ok=True)
            sd_gitkeep.write_text("", encoding="utf-8")
            files_written[f"{shared_dir}/.gitkeep"] = "sha256:" + sha256_file(sd_gitkeep)
        elif not sd_gitkeep.exists() and not any(True for _ in sd.iterdir()):
            # Dir exists but is empty — write .gitkeep
            sd_gitkeep.write_text("", encoding="utf-8")
            files_written[f"{shared_dir}/.gitkeep"] = "sha256:" + sha256_file(sd_gitkeep)
        # else: dir exists with content → leave alone

    # .gitignore
    _handle_gitignore(project_root, check=False)
    # .gitignore is NOT recorded in file_hashes (user-owned, even after marker append)

    # ------------------------------------------------------------------
    # venv detection / creation / pip / kernel
    # ------------------------------------------------------------------
    venv_python: Optional[Path] = None

    if not no_venv:
        venv_python = _probe_venv(project_root)

        if venv_python is not None:
            # Check Python version compat
            major, minor = _python_version(venv_python)
            if (major, minor) < (3, 11):
                if not force_venv:
                    click.echo(
                        f"Error: detected venv at `{venv_python.parent.parent}` uses python "
                        f"`{major}.{minor}`; kbu requires 3.11+. "
                        "Pass `--force-venv` to install anyway, or `--no-venv` to skip venv work.",
                        err=True,
                    )
                    sys.exit(1)
                # --force-venv: proceed anyway

        else:
            # No venv detected: create one
            # venvman is available only on macOS (or override), and shutil.which("venvman") != None
            use_venvman = _is_darwin() and shutil.which("venvman") is not None

            if use_venvman:
                click.echo(f"Detected venvman — creating project venv for {name} ...")
                venv_python, venvman_err = _run_venvman_project(name, project_root)
                if venv_python is None:
                    click.echo(
                        f"Warning: venvman create failed ({venvman_err}) — "
                        "falling back to python -m venv .venv",
                        err=True,
                    )
                    venv_python = _create_plain_venv(project_root)
            else:
                click.echo("Creating .venv with python -m venv ...")
                venv_python = _create_plain_venv(project_root)

        # pip install -e <KBUTILLIB_ROOT> plus ipykernel (needed for the kernel
        # registration step below; ipykernel is in KBUtilLib's [notebook] extra,
        # not base, so it isn't pulled in by the editable install).
        click.echo(f"Installing KBUtilLib editable from {kbu_root} ...")
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "-e", str(kbu_root), "ipykernel"],
            check=True,
        )

        # Register Jupyter kernel (unless --no-kernel)
        if not no_kernel:
            subprocess.run(
                [
                    str(venv_python),
                    "-m",
                    "ipykernel",
                    "install",
                    "--user",
                    f"--name={name}",
                    f"--display-name={name} (kbu)",
                ],
                check=True,
            )
            click.echo(f"registered jupyter kernel `{name}` (or replaced existing).")

    # ------------------------------------------------------------------
    # Source commit for manifest
    # ------------------------------------------------------------------
    source_commit = _git_head_commit(kbu_root)
    now = now_utc_iso()

    # ------------------------------------------------------------------
    # Write kbu-project.toml
    # ------------------------------------------------------------------
    manifest: dict = {
        "project": {
            "name": name,
            "title": "",
            "created_at": now,
            "bootstrapped": True,
            "bootstrapped_at": now,
            "authors": [
                {
                    "name": author,
                    "affiliation": affiliation,
                    "orcid": orcid,
                }
            ],
        },
        "layout": {
            "shared_dirs": list(DEFAULT_SHARED_DIRS),
        },
        "kbutillib": {
            "source_path": str(kbu_root),
            "source_commit": source_commit,
        },
        "update": {
            "last_pulled_at": now,
            "last_pulled_commit": source_commit,
            "file_hashes": files_written,
        },
    }
    write_project_manifest(project_root, manifest)

    # ------------------------------------------------------------------
    # Success summary
    # ------------------------------------------------------------------
    click.echo(f"\nkbu bootstrap complete in {project_root}\n")

    if files_written:
        click.echo("Files written:")
        for rel in files_written:
            click.echo(f"  {rel}")
        click.echo("")

    if files_backed_up:
        click.echo("Files left alone (already differed; backup created):")
        for rel, bak in files_backed_up:
            click.echo(f"  {rel}  (backup: {bak})")
        click.echo("")

    if files_user_owned:
        click.echo("Files left alone (user-owned):")
        for rel in files_user_owned:
            if rel == ".vscode/extensions.json":
                click.echo(f"  {rel}  (ensure anthropic.claude-code is recommended)")
            else:
                click.echo(f"  {rel}")
        click.echo("")

    click.echo("Manifest written: kbu-project.toml ([project].bootstrapped=true)")

    if not no_venv and not no_kernel and venv_python is not None:
        click.echo(f"Jupyter kernel registered: {name}")

    click.echo("")
    click.echo("Review with `git status`, then commit:")
    click.echo("  git add -A && git commit -m 'chore(kbu): bootstrap kbu-awareness'")
    click.echo("")
    click.echo("Enter the workflow:")
    click.echo("  open Claude Code → /kbu-start")
    click.echo("")
    click.echo("To undo bootstrap:")
    click.echo("  rm kbu-project.toml .claude/commands/kbu-*.md")
    click.echo("  edit .gitignore to remove the `# >>> kbu-managed >>>` block")
    click.echo("  (manual; v1 has no `kbu unbootstrap` command)")

    # ------------------------------------------------------------------
    # --first-subproject
    # ------------------------------------------------------------------
    if first_subproject:
        click.echo(f"\nCreating first subproject '{first_subproject}' ...")
        result = subprocess.run(
            ["kbu", "subproject", "create", first_subproject],
            cwd=str(project_root),
            check=False,
        )
        if result.returncode != 0:
            click.echo(
                f"Warning: subproject create '{first_subproject}' failed (exit {result.returncode}). "
                "Bootstrap itself succeeded.",
                err=True,
            )


# ---------------------------------------------------------------------------
# --check helpers (dry-run only)
# ---------------------------------------------------------------------------


def _check_gitignore_action(project_root: Path) -> str:
    """Return what would happen to .gitignore without making changes."""
    gi_path = project_root / ".gitignore"
    if not gi_path.exists():
        return "would create with kbu marker block"
    try:
        existing = gi_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        existing = ""
    if _GITIGNORE_MARKER_OPEN in existing:
        return "marker present — skip"
    return "would append kbu marker block"


def _print_check_file_plan(
    project_root: Path,
    template_src: Path,
    name: str,
    force_overwrite: bool,
) -> None:
    """Print the dry-run file plan for all template entries."""
    def _check_skill_file(rel_path: str) -> None:
        dest = project_root / rel_path
        src = template_src / rel_path
        if not src.exists():
            click.echo(f"  {rel_path}: template absent — skip")
            return

        src_bytes = _read_template_file_bytes(template_src, rel_path, name)
        if src_bytes is None:
            return
        src_hash = "sha256:" + hashlib.sha256(src_bytes).hexdigest()

        if not dest.exists():
            click.echo(f"  {rel_path}: would copy (absent)")
        else:
            dest_hash = "sha256:" + sha256_file(dest)
            if dest_hash == src_hash:
                click.echo(f"  {rel_path}: identical — skip")
            else:
                if force_overwrite:
                    click.echo(f"  {rel_path}: differs — would overwrite (--force-overwrite; backup .bak.<UTC>)")
                else:
                    click.echo(f"  {rel_path}: differs — would prompt overwrite (backup .bak.<UTC>)")

    # .claude/commands/kbu-*.md files
    for rel_path in _CLAUDE_COMMAND_FILES:
        _check_skill_file(rel_path)

    # .claude/agents/kbu-sub-*.md files
    for rel_path in _CLAUDE_AGENT_FILES:
        _check_skill_file(rel_path)

    # .vscode/extensions.json
    vscode_json = project_root / ".vscode" / "extensions.json"
    vscode_src = template_src / ".vscode" / "extensions.json"
    if not vscode_json.exists() and vscode_src.exists():
        click.echo("  .vscode/extensions.json: would copy (absent)")
    elif vscode_json.exists():
        click.echo("  .vscode/extensions.json: present — skip (user-owned; never overwritten)")

    # subprojects/.gitkeep
    sp_dir = project_root / "subprojects"
    if sp_dir.exists() and any(True for _ in sp_dir.iterdir()):
        click.echo("  subprojects/.gitkeep: subprojects/ has content — skip")
    elif not sp_dir.exists():
        click.echo("  subprojects/.gitkeep: would create subprojects/ and .gitkeep")
    else:
        click.echo("  subprojects/.gitkeep: would create .gitkeep in empty subprojects/")

    # README.md
    readme_dest = project_root / "README.md"
    readme_src = template_src / "README.md"
    if readme_dest.exists():
        click.echo("  README.md: present — skip (user-owned; never overwritten)")
    elif readme_src.exists():
        click.echo("  README.md: would copy with {{project_name}} substitution")

    # *.code-workspace
    existing_workspaces = list(project_root.glob("*.code-workspace"))
    ws_src = template_src / "{{project_name}}.code-workspace"
    if existing_workspaces:
        click.echo(f"  {name}.code-workspace: *.code-workspace already exists — skip")
    elif ws_src.exists():
        click.echo(f"  {name}.code-workspace: would copy with {{{{project_name}}}} substitution")


def _print_check_venv_plan(project_root: Path, name: str, force_venv: bool) -> None:
    """Print the dry-run venv plan."""
    venv_python = _probe_venv(project_root)
    if venv_python is not None:
        major, minor = _python_version(venv_python)
        if (major, minor) < (3, 11):
            if force_venv:
                click.echo(
                    f"\nvenv: detected {venv_python.parent.parent} (python {major}.{minor}) "
                    "— would proceed (--force-venv)"
                )
            else:
                click.echo(
                    f"\nvenv: detected {venv_python.parent.parent} (python {major}.{minor}) "
                    "— would EXIT 1 (kbu requires 3.11+; pass --force-venv to override)"
                )
        else:
            click.echo(
                f"\nvenv: would reuse detected venv at {venv_python.parent.parent} "
                f"(python {major}.{minor})"
            )
    else:
        use_venvman = _is_darwin() and shutil.which("venvman") is not None
        if use_venvman:
            click.echo(f"\nvenv: no venv detected — would run venvman create --project {name} --dir <cwd> --python 3.11")
        else:
            click.echo(f"\nvenv: no venv detected — would run python -m venv .venv")

    click.echo(f"pip: would run <venv_python> -m pip install -e {_kbutillib_root()}")
    click.echo(f"kernel: would register jupyter kernel `{name}`")


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command("bootstrap")
@click.option("--name", "name", default=None, help="Project name (default: cwd directory name).")
@click.option("--first-subproject", "first_subproject", default=None,
              help="Create this subproject after bootstrap completes.")
@click.option("--author", "author", default=None, help="Author name.")
@click.option("--affiliation", "affiliation", default=None, help="Author affiliation.")
@click.option("--orcid", "orcid", default=None, help="Author ORCID.")
@click.option("--no-venv", "no_venv", is_flag=True, default=False,
              help="Skip venv detection, creation, pip install, and kernel registration.")
@click.option("--no-kernel", "no_kernel", is_flag=True, default=False,
              help="Skip Jupyter kernel registration (pip install still runs).")
@click.option("--force-overwrite", "force_overwrite", is_flag=True, default=False,
              help="Skip per-file conflict prompts; still creates .bak.<UTC> backups.")
@click.option("--force-venv", "force_venv", is_flag=True, default=False,
              help="Bypass Python<3.11 venv refusal.")
@click.option("--check", "check", is_flag=True, default=False,
              help="Dry-run: print all actions that would be taken; no filesystem writes.")
def bootstrap_command(
    name: Optional[str],
    first_subproject: Optional[str],
    author: Optional[str],
    affiliation: Optional[str],
    orcid: Optional[str],
    no_venv: bool,
    no_kernel: bool,
    force_overwrite: bool,
    force_venv: bool,
    check: bool,
) -> None:
    """Retrofit kbu-awareness onto an existing git repository.

    Runs in the current directory. Copies template files, appends .gitignore
    entries, detects or creates a venv, pip-installs KBUtilLib editable,
    registers a Jupyter kernel, and writes kbu-project.toml.

    Does NOT git commit. Prints a suggested commit message.
    """
    if name is None:
        name = Path.cwd().name

    bootstrap(
        name=name,
        author=author,
        affiliation=affiliation,
        orcid=orcid,
        first_subproject=first_subproject,
        no_venv=no_venv,
        no_kernel=no_kernel,
        force_overwrite=force_overwrite,
        force_venv=force_venv,
        check=check,
    )
