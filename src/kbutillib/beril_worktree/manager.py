"""beril_worktree.manager — BerilWorktree class for parallel BERIL sessions.

All git operations run with ``git -C <beril_root>`` and never depend on the
current working directory.
"""

from __future__ import annotations

import json
import re
import subprocess
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Project-ID validation pattern (AC #5)
_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

# Branch prefix for all BERIL project branches.
_BRANCH_PREFIX = "projects/"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class WorktreeInfo:
    """Information about a worktree or a reopenable project branch."""

    id: str
    branch: str
    path: Optional[str]  # None when the branch has no live worktree
    live: bool


# ---------------------------------------------------------------------------
# BerilWorktree
# ---------------------------------------------------------------------------


class BerilWorktree:
    """Manage git worktrees for parallel BERIL project sessions.

    Args:
        beril_root: Absolute path to the primary BERIL git repository checkout.
        worktree_root: Absolute path to the directory under which worktrees
            are created (one subdirectory per project ID).
    """

    def __init__(self, beril_root: Path, worktree_root: Path) -> None:
        self.beril_root = beril_root.resolve()
        self.worktree_root = worktree_root.resolve()

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def new(
        self,
        project_id: str,
        *,
        from_branch: str = "main",
        open_cursor: bool = False,
    ) -> Path:
        """Create (or re-adopt) a git worktree for *project_id*.

        - Creates branch ``projects/<id>`` off *from_branch* when the branch
          does not exist; adopts the existing branch without ``-b`` otherwise.
        - Creates ``.env`` and ``.venv-berdl`` symlinks inside the worktree.
        - Writes a ``<worktree_root>/<id>.code-workspace`` file outside the
          worktree directory.
        - If *open_cursor* is True, attempts to open the workspace in Cursor.

        Args:
            project_id: Project identifier matching ``[A-Za-z0-9._-]+``.
            from_branch: Base branch/ref to create the new branch off when it
                does not already exist.
            open_cursor: When True, launch Cursor on the workspace file.

        Returns:
            Path to the created worktree directory.

        Raises:
            ValueError: If *project_id* is invalid.
            RuntimeError: If the target directory exists but is not a
                registered git worktree, or git commands fail.
        """
        self._validate_id(project_id)
        wt_path = self._worktree_path(project_id)

        # AC #9 — if target dir exists and is NOT a registered worktree, abort.
        if wt_path.exists():
            if not self._is_registered_worktree(wt_path):
                raise RuntimeError(
                    f"Directory already exists and is not a registered git worktree: "
                    f"{wt_path}\n"
                    f"Remove it manually if you want to recreate the worktree here."
                )

        self._add_worktree(project_id, from_branch=from_branch)
        self._symlink_env(project_id)
        self._write_workspace(project_id)

        if open_cursor:
            self.open(project_id)

        return wt_path

    def remove(self, project_id: str, *, force: bool = False) -> bool:
        """Remove the worktree directory for *project_id*.

        Never deletes the ``projects/<id>`` branch — the branch is the durable
        artifact.  Runs ``git worktree prune`` afterward.

        Args:
            project_id: Project identifier.
            force: When True, pass ``--force`` to ``git worktree remove``,
                discarding uncommitted changes.

        Returns:
            True if the worktree was removed; False if it was not registered
            (idempotent no-op).

        Raises:
            ValueError: If *project_id* is invalid.
            RuntimeError: If the worktree has uncommitted changes and *force*
                is False, or if the git command fails for any other reason.
        """
        self._validate_id(project_id)
        wt_path = self._worktree_path(project_id)

        # AC #12 — if the path is not registered, print and return False.
        if not self._is_registered_worktree(wt_path):
            print(f"nothing to remove: {wt_path} is not a registered git worktree")
            return False

        # AC #13 — check for uncommitted changes unless force=True.
        if not force and self._has_uncommitted_changes(wt_path):
            raise RuntimeError(
                f"Worktree at {wt_path} has uncommitted changes.\n"
                "Commit, stash, or discard the changes first, or pass force=True."
            )

        # Build the git worktree remove command.
        cmd = ["git", "-C", str(self.beril_root), "worktree", "remove"]
        if force:
            cmd.append("--force")
        cmd.append(str(wt_path))

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"git worktree remove failed (rc={result.returncode}): "
                f"{(result.stderr or result.stdout).strip()}"
            )

        self._prune()
        return True

    def list(self) -> list[WorktreeInfo]:
        """Return live worktrees and reopenable ``projects/*`` branches.

        Returns:
            List of :class:`WorktreeInfo` sorted by project ID.
        """
        # Collect live worktrees.
        live_entries = self._parse_worktree_list()
        live_paths: dict[str, str] = {}  # id -> path
        for entry in live_entries:
            raw_branch = entry.get("branch", "")
            # Porcelain output has "refs/heads/projects/<id>" — strip the prefix.
            branch = raw_branch.removeprefix("refs/heads/")
            if branch.startswith(_BRANCH_PREFIX):
                pid = branch[len(_BRANCH_PREFIX):]
                live_paths[pid] = entry.get("worktree", "")

        # Collect all projects/* branches.
        all_branches = self._list_project_branches()

        result: list[WorktreeInfo] = []
        seen: set[str] = set()

        for pid in sorted(set(list(live_paths.keys()) + all_branches)):
            if pid in seen:  # pragma: no cover
                continue
            seen.add(pid)
            branch = f"{_BRANCH_PREFIX}{pid}"
            if pid in live_paths:
                result.append(
                    WorktreeInfo(id=pid, branch=branch, path=live_paths[pid], live=True)
                )
            else:
                result.append(
                    WorktreeInfo(id=pid, branch=branch, path=None, live=False)
                )

        return result

    def open(self, project_id: str) -> Path:  # noqa: A003
        """Return the worktree path for *project_id*, recreating it if missing.

        If the worktree directory is missing but the branch ``projects/<id>``
        exists, the worktree is recreated via ``git worktree add``.

        If the branch does not exist, raises RuntimeError directing the user
        to run ``new``.

        Args:
            project_id: Project identifier.

        Returns:
            Path to the worktree directory.

        Raises:
            ValueError: If *project_id* is invalid.
            RuntimeError: If the branch does not exist.
        """
        self._validate_id(project_id)
        wt_path = self._worktree_path(project_id)
        branch = f"{_BRANCH_PREFIX}{project_id}"

        if not wt_path.exists():
            if not self._branch_exists(branch):
                raise RuntimeError(
                    f"Branch '{branch}' does not exist in {self.beril_root}.\n"
                    f"Run: kbu beril worktree new {project_id}"
                )
            # Recreate the worktree from the existing branch.
            cmd = [
                "git", "-C", str(self.beril_root),
                "worktree", "add",
                str(wt_path), branch,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(
                    f"git worktree add failed (rc={result.returncode}): "
                    f"{(result.stderr or result.stdout).strip()}"
                )

        return wt_path

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _worktree_path(self, project_id: str) -> Path:
        """Return the expected worktree directory path for *project_id*."""
        return self.worktree_root / project_id

    def _validate_id(self, project_id: str) -> None:
        """Raise ValueError if *project_id* does not match the allowed pattern."""
        if not _ID_PATTERN.match(project_id):
            raise ValueError(
                f"Invalid project ID: {project_id!r}\n"
                "Project IDs must match [A-Za-z0-9._-]+ "
                "(no slashes or other special characters)."
            )

    def _branch_exists(self, branch: str) -> bool:
        """Return True if *branch* exists in the BERIL repo."""
        result = subprocess.run(
            ["git", "-C", str(self.beril_root), "show-ref", "--verify", "--quiet",
             f"refs/heads/{branch}"],
            capture_output=True,
        )
        return result.returncode == 0

    def _is_registered_worktree(self, path: Path) -> bool:
        """Return True if *path* appears in ``git worktree list`` output."""
        entries = self._parse_worktree_list()
        for entry in entries:
            if Path(entry.get("worktree", "")).resolve() == path.resolve():
                return True
        return False

    def _has_uncommitted_changes(self, wt_path: Path) -> bool:
        """Return True if *wt_path* has uncommitted changes."""
        result = subprocess.run(
            ["git", "-C", str(wt_path), "status", "--porcelain"],
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())

    def _parse_worktree_list(self) -> list[dict[str, str]]:
        """Run ``git worktree list --porcelain`` and parse the output.

        Returns a list of dicts with keys ``worktree``, ``HEAD``, ``branch``
        (and ``bare`` / ``detached`` when present).
        """
        result = subprocess.run(
            ["git", "-C", str(self.beril_root), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
        )
        entries: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                if current:
                    entries.append(current)
                    current = {}
                continue
            if line == "bare" or line == "detached":
                current[line] = "true"
            elif " " in line:
                key, _, val = line.partition(" ")
                current[key] = val
        if current:
            entries.append(current)
        return entries

    def _list_project_branches(self) -> list[str]:
        """Return project IDs (suffix after ``projects/``) for all project branches."""
        result = subprocess.run(
            ["git", "-C", str(self.beril_root), "branch", "--list", "projects/*",
             "--format=%(refname:short)"],
            capture_output=True,
            text=True,
        )
        ids: list[str] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith(_BRANCH_PREFIX):
                ids.append(line[len(_BRANCH_PREFIX):])
        return ids

    def _add_worktree(self, project_id: str, *, from_branch: str) -> None:
        """Run ``git worktree add`` for *project_id*.

        Creates the ``projects/<id>`` branch off *from_branch* when it does
        not exist; adopts the existing branch (no ``-b``) otherwise.
        """
        wt_path = self._worktree_path(project_id)
        branch = f"{_BRANCH_PREFIX}{project_id}"
        self.worktree_root.mkdir(parents=True, exist_ok=True)

        if self._branch_exists(branch):
            # Adopt existing branch — no -b flag.
            cmd = [
                "git", "-C", str(self.beril_root),
                "worktree", "add",
                str(wt_path), branch,
            ]
        else:
            # Create new branch off from_branch.
            cmd = [
                "git", "-C", str(self.beril_root),
                "worktree", "add",
                "-b", branch,
                str(wt_path), from_branch,
            ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed (rc={result.returncode}): "
                f"{(result.stderr or result.stdout).strip()}"
            )

    def _symlink_env(self, project_id: str) -> None:
        """Create .env and .venv-berdl symlinks inside the worktree.

        Both symlinks point at the corresponding paths in ``beril_root``.
        A missing target produces a warning and non-fatal continuation (AC #7).
        """
        wt_path = self._worktree_path(project_id)
        for name in (".env", ".venv-berdl"):
            target = self.beril_root / name
            link = wt_path / name
            if link.exists() or link.is_symlink():
                # Already present (idempotent).
                continue
            link.symlink_to(target)
            if not target.exists():
                warnings.warn(
                    f"Symlink target missing: {target}\n"
                    f"The symlink {link} points at a non-existent path. "
                    "BERDL operations in this worktree may fail until the "
                    "target is created.",
                    stacklevel=3,
                )

    def _write_workspace(self, project_id: str) -> None:
        """Write <worktree_root>/<id>.code-workspace outside the git tree.

        Content: a single ``folders`` entry with name ``BERIL: <id>`` and
        path ``./<id>``, plus the ``settings`` and ``extensions`` top-level
        keys copied from ``<beril_root>/BERIL.code-workspace`` when present,
        or empty objects otherwise (AC #8).
        """
        ws_file = self.worktree_root / f"{project_id}.code-workspace"
        beril_ws = self.beril_root / "BERIL.code-workspace"

        settings: dict = {}
        extensions: dict = {}
        if beril_ws.is_file():
            try:
                beril_data = json.loads(beril_ws.read_text(encoding="utf-8"))
                settings = beril_data.get("settings", {}) or {}
                extensions = beril_data.get("extensions", {}) or {}
            except (json.JSONDecodeError, OSError):
                pass  # Fall through to empty objects.

        workspace = {
            "folders": [{"name": f"BERIL: {project_id}", "path": f"./{project_id}"}],
            "settings": settings,
            "extensions": extensions,
        }
        ws_file.write_text(
            json.dumps(workspace, indent=2) + "\n", encoding="utf-8"
        )

    def _prune(self) -> None:
        """Run ``git worktree prune`` to clear stale admin entries."""
        subprocess.run(
            ["git", "-C", str(self.beril_root), "worktree", "prune"],
            capture_output=True,
        )
