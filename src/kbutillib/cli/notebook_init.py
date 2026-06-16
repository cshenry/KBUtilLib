"""``kbu notebook-init`` — idempotent work-notebook repo scaffolder.

Signature::

    kbu notebook-init <repo> [--project <topic>] [--update]

Where ``<repo>`` is either:
- A bare name  → resolved to ``~/Dropbox/Projects/<name>``
- A full path  → used verbatim

Behavior branches on detected state:
  1. **Repo missing** → full bootstrap: ``git init``, ``.code-workspace``,
     ``.claude/``, ``notebooks/`` with shared roots + first PRJ.
  2. **Repo present, notebooks/ missing** → scaffold notebooks tree + first PRJ.
  3. **notebooks/ present** → add the named ``PRJ-<topic>/``.
     Refuse (non-zero, no writes) if that PRJ already exists.
  4. **--update** → re-deploy the work-notebook bundle into ``.claude``;
     do not touch notebooks/PRJs.

Design decisions
----------------
- Topic normalization: lowercase ASCII, non-``[a-z0-9]`` → ``_``, collapse
  runs, strip edges (same rule as notebook titles per advisory #1).
- Bundle deployment: direct-copy from ClaudeCommands ``agent-io/skills/``
  (``claude-skills sync-repos`` cannot target an arbitrary path without a
  project_registry.yaml entry; direct-copy is the correct fallback path
  per the PRD clarification #3/#6).  The copy is isolated in
  ``_deploy_bundle()`` so Phase 4 can adjust if needed.
- Registry: when ``assistant.state`` is importable, use
  ``find_by_repo_path`` to attach or ``add_project`` to register.  When
  not importable, write the binding with the name-derived project_id and
  print a notice.

IMPORTANT: Never deploy BERIL skills (kbu, kbu-notebook, kbu-fba,
kbu-start, kbu-migrate, kbu-sub-*) into work-notebook repos.  The
allowed set is exactly {jupyter-dev, kbu-run, synthesize}.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click

from ..layout import (
    WORKNB_GITIGNORE_MARKER_START,
    WORKNB_PRJ_SUBDIRS,
    WORKNB_SHARED_ROOTS,
    apply_worknb_gitignore_block,
)
from .worknb_util import render_worknb_util_template, smart_merge_worknb_util


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Work-notebook bundle: exactly these three skills — no BERIL skills.
_WORKNB_BUNDLE: tuple[str, ...] = ("jupyter-dev", "kbu-run", "synthesize")

#: Expected location of ClaudeCommands (Dropbox-synced).
_CLAUDECOMMANDS_ROOT: Path = Path("~/Dropbox/Projects/ClaudeCommands").expanduser()

#: Skills source directory inside ClaudeCommands.
_CC_SKILLS_DIR: Path = _CLAUDECOMMANDS_ROOT / "agent-io" / "skills"

#: Default Dropbox projects root.
_DROPBOX_PROJECTS: Path = Path("~/Dropbox/Projects").expanduser()


# ---------------------------------------------------------------------------
# Helpers: normalization
# ---------------------------------------------------------------------------


def normalize_topic(topic: str) -> str:
    """Return *topic* normalized to a path-safe folder name.

    Rules (per PRD advisory #1 and clarification #7):
    - Lowercase ASCII only.
    - Any character not in ``[a-z0-9]`` is replaced with ``_``.
    - Runs of ``_`` collapsed to one.
    - Leading and trailing ``_`` stripped.

    Examples
    --------
    >>> normalize_topic("ADP1 Notebooks")
    'adp1_notebooks'
    >>> normalize_topic("flux balance analysis!")
    'flux_balance_analysis'
    """
    lowered = topic.lower()
    replaced = re.sub(r"[^a-z0-9]+", "_", lowered)
    collapsed = re.sub(r"_{2,}", "_", replaced)
    return collapsed.strip("_")


def _resolve_repo(repo: str) -> Path:
    """Resolve a repo argument to an absolute path.

    A bare name (no path separators) expands to
    ``~/Dropbox/Projects/<name>``.  A path with separators is used
    verbatim (expanded via ``Path.expanduser()``).
    """
    if "/" not in repo and "\\" not in repo:
        return _DROPBOX_PROJECTS / repo
    return Path(repo).expanduser().resolve()


# ---------------------------------------------------------------------------
# Helpers: .code-workspace
# ---------------------------------------------------------------------------


def _write_code_workspace(repo_root: Path) -> None:
    """Write a minimal Cursor ``.code-workspace`` file at *repo_root*.

    The file is ``<repo_basename>.code-workspace`` and contains at least
    ``{"folders": [{"path": "."}]}``.  Extra keys (extensions,
    tasks) are permitted per advisory #4; the required entry is
    ``folders``.
    """
    ws_path = repo_root / f"{repo_root.name}.code-workspace"
    content = json.dumps({"folders": [{"path": "."}]}, indent=2) + "\n"
    ws_path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers: bundle deployment
# ---------------------------------------------------------------------------


def _find_claudecommands_root() -> Optional[Path]:
    """Return the ClaudeCommands root if it exists, else None."""
    if _CLAUDECOMMANDS_ROOT.is_dir():
        return _CLAUDECOMMANDS_ROOT
    return None


def _deploy_bundle(repo_root: Path) -> None:
    """Deploy the work-notebook skill bundle into *repo_root*/.claude/commands/.

    Strategy (per PRD clarification #3/#6):
    1. If ClaudeCommands is absent → print notice, return (exit 0).
    2. Otherwise, direct-copy the three skill files from
       ``ClaudeCommands/agent-io/skills/`` into
       ``<repo_root>/.claude/commands/``.  Each skill may have a companion
       ``<skill>/`` context directory; copy that too if present.
    3. BERIL skills are never deployed here.

    This function is intentionally isolated so Phase 4
    (worknb-deploy-integration) can swap to claude-skills if/when the
    CLI gains an arbitrary-path deploy mode.
    """
    cc_root = _find_claudecommands_root()
    if cc_root is None:
        click.echo(
            "[notice] ClaudeCommands not found at "
            f"{_CLAUDECOMMANDS_ROOT} — skipping bundle deployment.",
            err=False,
        )
        return

    skills_src = cc_root / "agent-io" / "skills"
    commands_dir = repo_root / ".claude" / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)

    for skill in _WORKNB_BUNDLE:
        src_file = skills_src / f"{skill}.md"
        if not src_file.is_file():
            click.echo(
                f"[notice] Skill source not found: {src_file} — "
                f"skipping {skill}.",
                err=False,
            )
            continue
        dest = commands_dir / f"{skill}.md"
        shutil.copy2(src_file, dest)

        # Copy companion context directory if present.
        src_context = skills_src / skill
        if src_context.is_dir():
            dest_context = commands_dir / skill
            if dest_context.exists():
                shutil.rmtree(dest_context)
            shutil.copytree(src_context, dest_context)

    click.echo(
        f"  Bundle deployed: {', '.join(_WORKNB_BUNDLE)} -> "
        f"{commands_dir}"
    )


def _init_claude_dir(repo_root: Path) -> None:
    """Initialize the ``.claude/`` directory.

    When ClaudeCommands is present, deploy the bundle.  When absent,
    create an empty ``.claude/`` directory and print a notice.
    """
    claude_dir = repo_root / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    _deploy_bundle(repo_root)


# ---------------------------------------------------------------------------
# Helpers: registry + binding
# ---------------------------------------------------------------------------


def _write_kbu_run_json(notebooks_dir: Path, project_id: str) -> None:
    """Write ``notebooks/.kbu-run.json`` with the given *project_id*."""
    binding = {"project_id": project_id}
    (notebooks_dir / ".kbu-run.json").write_text(
        json.dumps(binding) + "\n", encoding="utf-8"
    )


def _register_or_attach(repo_root: Path) -> str:
    """Register the repo in AIAssistant registry or attach to existing entry.

    Returns the project_id that was registered or attached.

    When ``assistant.state`` is not importable, derives a name-based
    project_id (``worknb-<repo_basename>``) and prints a notice.
    """
    repo_basename = repo_root.name
    default_project_id = f"worknb-{repo_basename}"

    try:
        from assistant.state.registry import add_project, find_by_repo_path
    except ImportError:
        click.echo(
            "[notice] assistant.state not importable — writing .kbu-run.json "
            f"with project_id={default_project_id!r} (no registry entry created).",
            err=False,
        )
        return default_project_id

    # Check for existing registry entry by repo_path.
    repo_abs = str(repo_root.resolve())
    matches = find_by_repo_path(repo_abs)
    if matches:
        project_id = matches[0]
        click.echo(
            f"  Registry: attached to existing entry {project_id!r} "
            f"(repo_path={repo_abs})"
        )
        return project_id

    # Register a new entry.
    try:
        add_project(
            project_id=default_project_id,
            name=repo_basename,
            node_type="project",
            repo_path=repo_abs,
            description=f"Work-notebook repo: {repo_basename}",
            tags=["work-notebook"],
        )
        click.echo(
            f"  Registry: registered new project {default_project_id!r}"
        )
    except Exception as exc:  # noqa: BLE001
        click.echo(
            f"[notice] Registry registration failed ({exc}) — "
            f"using project_id={default_project_id!r}.",
            err=False,
        )

    return default_project_id


# ---------------------------------------------------------------------------
# Helpers: PRJ scaffolding
# ---------------------------------------------------------------------------


def _scaffold_prj(notebooks_dir: Path, norm_topic: str, repo_basename: str) -> Path:
    """Create ``PRJ-<norm_topic>/`` with util.py, NBCache/, NBOutput/.

    Returns the created PRJ directory.
    """
    prj_dir = notebooks_dir / f"PRJ-{norm_topic}"
    prj_dir.mkdir(parents=True, exist_ok=True)

    # Render and write util.py.
    rendered = render_worknb_util_template(repo_basename, norm_topic)
    util_path = prj_dir / "util.py"
    util_path.write_text(rendered, encoding="utf-8")

    # Create per-PRJ cache and output dirs.
    for subdir in WORKNB_PRJ_SUBDIRS:
        (prj_dir / subdir).mkdir(exist_ok=True)

    return prj_dir


# ---------------------------------------------------------------------------
# Core logic: three branch cases + --update
# ---------------------------------------------------------------------------


def _bootstrap_repo(
    repo_root: Path,
    norm_topic: str,
    repo_basename: str,
) -> None:
    """Branch 1: repo does not exist — full bootstrap."""
    click.echo(f"Creating new work-notebook repo at {repo_root} ...")

    repo_root.mkdir(parents=True, exist_ok=True)

    # git init.
    result = subprocess.run(
        ["git", "init"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise click.ClickException(
            f"git init failed: {result.stderr.strip()}"
        )
    click.echo(f"  git init: {repo_root}")

    # .code-workspace.
    _write_code_workspace(repo_root)
    click.echo(f"  Created: {repo_root.name}.code-workspace")

    # .claude/ + bundle.
    _init_claude_dir(repo_root)

    # notebooks/ tree.
    notebooks_dir = repo_root / "notebooks"
    notebooks_dir.mkdir()

    for shared in WORKNB_SHARED_ROOTS:
        (notebooks_dir / shared).mkdir()
        click.echo(f"  Created: notebooks/{shared}/")

    # First PRJ.
    prj_dir = _scaffold_prj(notebooks_dir, norm_topic, repo_basename)
    click.echo(f"  Created: {prj_dir.relative_to(repo_root)}/")

    # Gitignore block.
    apply_worknb_gitignore_block(repo_root / ".gitignore")
    click.echo("  Updated: .gitignore (work-notebook block)")

    # Registry + binding.
    project_id = _register_or_attach(repo_root)
    _write_kbu_run_json(notebooks_dir, project_id)
    click.echo(f"  Wrote: notebooks/.kbu-run.json (project_id={project_id!r})")


def _scaffold_notebooks(
    repo_root: Path,
    norm_topic: str,
    repo_basename: str,
) -> None:
    """Branch 2: repo exists but notebooks/ missing — scaffold + first PRJ."""
    click.echo(f"Scaffolding notebooks/ tree in existing repo at {repo_root} ...")

    notebooks_dir = repo_root / "notebooks"
    notebooks_dir.mkdir()

    for shared in WORKNB_SHARED_ROOTS:
        (notebooks_dir / shared).mkdir()
        click.echo(f"  Created: notebooks/{shared}/")

    # First PRJ.
    prj_dir = _scaffold_prj(notebooks_dir, norm_topic, repo_basename)
    click.echo(f"  Created: {prj_dir.relative_to(repo_root)}/")

    # Gitignore block.
    apply_worknb_gitignore_block(repo_root / ".gitignore")
    click.echo("  Updated: .gitignore (work-notebook block)")

    # Bundle deployment.
    _deploy_bundle(repo_root)

    # Registry + binding.
    project_id = _register_or_attach(repo_root)
    _write_kbu_run_json(notebooks_dir, project_id)
    click.echo(f"  Wrote: notebooks/.kbu-run.json (project_id={project_id!r})")


def _add_prj(
    repo_root: Path,
    norm_topic: str,
    repo_basename: str,
) -> None:
    """Branch 3: notebooks/ exists — add named PRJ-<topic>/."""
    notebooks_dir = repo_root / "notebooks"
    prj_dir = notebooks_dir / f"PRJ-{norm_topic}"

    # Clobber-refusal.
    if prj_dir.exists():
        click.echo(
            f"Error: PRJ-{norm_topic}/ already exists at {prj_dir}. "
            "Use a different --project topic or delete the existing folder first.",
            err=True,
        )
        sys.exit(1)

    click.echo(
        f"Adding PRJ-{norm_topic}/ to existing notebooks/ tree ..."
    )

    _scaffold_prj(notebooks_dir, norm_topic, repo_basename)
    click.echo(f"  Created: {prj_dir.relative_to(repo_root)}/")

    # Update gitignore block (idempotent).
    apply_worknb_gitignore_block(repo_root / ".gitignore")
    click.echo("  Updated: .gitignore (work-notebook block, idempotent)")


def _update_bundle(repo_root: Path) -> None:
    """--update: re-deploy the work-notebook bundle into .claude/."""
    click.echo(f"Re-deploying work-notebook bundle into {repo_root} ...")
    _deploy_bundle(repo_root)
    click.echo("  Bundle update complete.")


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


def notebook_init(
    repo: str,
    topic: Optional[str],
    update: bool,
) -> None:
    """Core logic, separated for testability."""
    repo_root = _resolve_repo(repo)
    repo_basename = repo_root.name

    # --update path: repo must exist; no topic required.
    if update:
        if not repo_root.exists():
            raise click.ClickException(
                f"--update requires an existing repo; {repo_root} does not exist."
            )
        _update_bundle(repo_root)
        return

    # All other branches require --project.
    if topic is None:
        raise click.UsageError(
            "--project <topic> is required unless --update is specified."
        )

    norm_topic = normalize_topic(topic)
    if not norm_topic:
        raise click.UsageError(
            f"The topic {topic!r} normalizes to an empty string — "
            "provide a topic that contains at least one alphanumeric character."
        )
    if norm_topic != topic:
        click.echo(
            f"  [info] Topic normalized: {topic!r} -> {norm_topic!r}"
        )

    # Branch selection.
    if not repo_root.exists():
        _bootstrap_repo(repo_root, norm_topic, repo_basename)
    elif not (repo_root / "notebooks").exists():
        _scaffold_notebooks(repo_root, norm_topic, repo_basename)
    else:
        _add_prj(repo_root, norm_topic, repo_basename)

    click.echo("\nDone.")


@click.command("notebook-init")
@click.argument("repo")
@click.option(
    "--project",
    "topic",
    default=None,
    help="Topic name for the first (or additional) PRJ-<topic>/ folder.",
)
@click.option(
    "--update",
    is_flag=True,
    default=False,
    help="Re-deploy the work-notebook bundle into .claude/; do not scaffold.",
)
def notebook_init_cmd(repo: str, topic: Optional[str], update: bool) -> None:
    """Scaffold or extend a work-notebook repo.

    REPO is either a bare name (resolved to ~/Dropbox/Projects/<name>)
    or an absolute/relative path used verbatim.

    Branches on detected state:

    \b
    - Repo missing       → full bootstrap (git init, .code-workspace, .claude/,
                           notebooks/ with shared roots + first PRJ-<topic>/)
    - Repo present,      → scaffold notebooks/ + first PRJ-<topic>/
      notebooks/ missing
    - notebooks/ present → add the named PRJ-<topic>/ (error if it exists)
    - --update           → re-deploy the work-notebook bundle into .claude/

    Work-notebook bundle deployed: jupyter-dev, kbu-run, synthesize.
    No BERIL skill is ever deployed here.
    """
    notebook_init(repo=repo, topic=topic, update=update)
