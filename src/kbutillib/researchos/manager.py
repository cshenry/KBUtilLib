"""researchos.manager — ResearchOSProject class for scaffolding Research-OS studies.

Each study lives at ``<researchos_root>/<parent>/<name>/`` and is its own
independent git repository.  The parent directory is a plain organizational
folder on disk (not itself a git repo).
"""

from __future__ import annotations

import json
import re
import subprocess
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from kbutillib.researchos.registry import RegistryResult, register_project
from kbutillib.researchos.tooling import ensure_research_os_binary

# Name validation pattern — same rationale as BERIL's _ID_PATTERN.
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ResearchOSProjectInfo:
    """Lightweight descriptor for a discovered Research-OS project."""

    parent: str
    name: str
    path: Path
    has_workspace: bool


# ---------------------------------------------------------------------------
# ResearchOSProject
# ---------------------------------------------------------------------------


class ResearchOSProject:
    """Scaffold and manage Research-OS studies under a parent project.

    Args:
        researchos_root: Root directory under which parent/name subdirectories
            are created (e.g. ``~/Dropbox/Projects/ResearchOS``).
        tooling_venv: Path to the shared Research-OS tooling venv.
        aiassistant_root: Path to the AIAssistant repository root (used for
            registry integration).
    """

    def __init__(
        self,
        researchos_root: Path,
        tooling_venv: Path,
        aiassistant_root: Path,
    ) -> None:
        self.researchos_root = researchos_root.resolve()
        self.tooling_venv = tooling_venv.resolve()
        self.aiassistant_root = aiassistant_root.resolve()

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def new(
        self,
        parent: str,
        name: str,
        *,
        display_name: Optional[str] = None,
        domain: Optional[str] = None,
        questions: Optional[list[str]] = None,
        workspace_mode: str = "analysis",
        ide: str = "cursor,claude",
        open_cursor: bool = False,
        force: bool = False,
    ) -> Path:
        """Scaffold a new Research-OS study at ``<root>/<parent>/<name>/``.

        Steps (each prints a ``──`` header and ``✓``/``✗`` line):
        1. Validate parent and name.
        2. Resolve paths; mkdir parent_path; check existing project.
        3. Ensure research-os binary (create tooling venv if missing).
        4. Run ``research-os init`` with cwd = parent_path.
        5. Rewrite MCP ``command`` fields to the absolute binary path.
        6. Write ``<name>.code-workspace``.
        7. git init + add + commit.
        8. Register in AIAssistant registry (best-effort).
        9. Open Cursor if requested.

        Args:
            parent: Parent project name (e.g. 'AIALE').
            name: Study name (e.g. 'RoboticLabManuscript').
            display_name: Optional human-readable name passed to research-os init.
            domain: Optional domain string passed to research-os init.
            questions: Optional list of research questions.
            workspace_mode: research-os workspace mode (default 'analysis').
            ide: IDE string passed to research-os init (default 'cursor,claude').
            open_cursor: When True, launch Cursor on the workspace after creation.
            force: When True, pass --force to research-os init and skip the
                non-empty-dir guard.

        Returns:
            Path to the created project directory.

        Raises:
            ValueError: If parent or name is invalid.
            RuntimeError: If project path already exists and is non-empty
                without force, or if research-os init fails.
        """
        # Step 1: Validate names
        print("── Validating names")
        _validate_name(parent)
        _validate_name(name)
        print(f"   ✓ parent={parent!r}, name={name!r}")

        # Step 2: Resolve paths
        print("── Resolving paths")
        parent_path = self.researchos_root / parent
        project_path = parent_path / name
        parent_path.mkdir(parents=True, exist_ok=True)

        if project_path.exists() and any(project_path.iterdir()) and not force:
            raise RuntimeError(
                f"Project directory already exists and is non-empty: {project_path}\n"
                "Pass --force to overwrite, or choose a different name."
            )
        print(f"   ✓ project_path={project_path}")

        # Step 3: Ensure tooling binary
        print("── Ensuring research-os binary")
        try:
            research_os_bin = ensure_research_os_binary(self.tooling_venv)
            print(f"   ✓ research-os binary at {research_os_bin}")
        except RuntimeError as exc:
            raise RuntimeError(
                f"Failed to ensure research-os binary: {exc}"
            ) from exc

        # Step 4: Run research-os init
        print("── Running research-os init")
        self._run_init(
            name=name,
            parent_path=parent_path,
            research_os_bin=research_os_bin,
            display_name=display_name,
            domain=domain,
            questions=questions,
            workspace_mode=workspace_mode,
            ide=ide,
            force=force,
        )
        print(f"   ✓ research-os init completed")

        # Step 5: Rewrite MCP commands
        print("── Rewriting MCP command fields")
        self._rewrite_mcp_commands(project_path, research_os_bin)
        print(f"   ✓ MCP commands rewritten to absolute path")

        # Step 6: Write workspace file
        print("── Writing .code-workspace")
        self._write_workspace(project_path, name)
        print(f"   ✓ {name}.code-workspace written")

        # Step 7: git init + commit
        print("── Initializing git repository")
        try:
            self._git_init_and_commit(project_path, name)
            print(f"   ✓ git repository initialized with initial commit")
        except Exception as exc:  # noqa: BLE001
            warnings.warn(
                f"git initialization failed (scaffold is still usable): {exc}",
                stacklevel=2,
            )
            print(f"   ⚠ git init failed (continuing): {exc}")

        # Step 8: Register in AIAssistant registry
        print("── Registering in AIAssistant project registry")
        reg_result: RegistryResult = register_project(
            parent,
            name,
            project_path,
            aiassistant_root=self.aiassistant_root,
        )
        if reg_result.status == "ok":
            print(f"   ✓ registered {parent}/{name}")
        elif reg_result.status == "skipped":
            print(f"   ↷ skipped (already registered)")
        else:
            print(
                f"   ⚠ registry unavailable — register manually with /ai-registry"
                f" ({reg_result.message})"
            )

        # Step 9: Open Cursor
        if open_cursor:
            ws_file = project_path / f"{name}.code-workspace"
            _open_cursor_workspace(ws_file)

        return project_path

    def open(self, parent: str, name: str) -> Path:  # noqa: A003
        """Return the path to an existing Research-OS project.

        Args:
            parent: Parent project name.
            name: Study name.

        Returns:
            Path to the project directory.

        Raises:
            ValueError: If parent or name is invalid.
            RuntimeError: If the project directory does not exist.
        """
        _validate_name(parent)
        _validate_name(name)

        project_path = self.researchos_root / parent / name
        if not project_path.is_dir():
            raise RuntimeError(
                f"Project not found: {project_path}\n"
                f"Run: kbu researchos new {parent} {name}"
            )
        return project_path

    def list(self) -> list[ResearchOSProjectInfo]:  # noqa: A003
        """Return all Research-OS projects found under the root.

        Walks two levels: ``<root>/<parent>/<name>``. A directory is recognized
        as a Research-OS project if it contains ``.os_state/``.

        Returns:
            List of :class:`ResearchOSProjectInfo` sorted by (parent, name).
        """
        results: list[ResearchOSProjectInfo] = []

        if not self.researchos_root.is_dir():
            return results

        for parent_dir in sorted(self.researchos_root.iterdir()):
            if not parent_dir.is_dir():
                continue
            parent_name = parent_dir.name

            for project_dir in sorted(parent_dir.iterdir()):
                if not project_dir.is_dir():
                    continue
                if not (project_dir / ".os_state").is_dir():
                    continue

                study_name = project_dir.name
                has_ws = any(
                    project_dir.glob(f"{study_name}.code-workspace")
                )
                results.append(
                    ResearchOSProjectInfo(
                        parent=parent_name,
                        name=study_name,
                        path=project_dir,
                        has_workspace=has_ws,
                    )
                )

        return sorted(results, key=lambda r: (r.parent, r.name))

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _run_init(
        self,
        name: str,
        parent_path: Path,
        research_os_bin: Path,
        *,
        display_name: Optional[str],
        domain: Optional[str],
        questions: Optional[list[str]],
        workspace_mode: str,
        ide: str,
        force: bool,
    ) -> None:
        """Run ``research-os init`` with cwd = parent_path.

        research-os init creates the ``<name>/`` subdirectory itself.

        Raises:
            RuntimeError: On non-zero exit, with stderr surfaced.
        """
        cmd: list[str] = [
            str(research_os_bin),
            "init",
            name,
            "--yes",
            "--workspace-mode", workspace_mode,
            "--ide", ide,
            "--mcp-scope", "workspace",
            "--no-color",
        ]
        if display_name is not None:
            cmd += ["--name", display_name]
        if domain is not None:
            cmd += ["--domain", domain]
        if questions:
            for q in questions:
                cmd += ["--questions", q]
        if force:
            cmd.append("--force")

        result = subprocess.run(
            cmd,
            cwd=str(parent_path),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"research-os init failed (rc={result.returncode}):\n"
                + (result.stderr or result.stdout or "").strip()
            )

    def _rewrite_mcp_commands(
        self,
        project_path: Path,
        research_os_bin: Path,
    ) -> None:
        """Rewrite the ``command`` field in MCP config files to the absolute binary path.

        Checks ``.mcp.json``, ``.cursor/mcp.json``, and ``.claude/mcp.json``.
        Missing files are silently skipped.  Existing ``args`` and ``env`` fields
        are left unchanged.

        Args:
            project_path: The project directory (root of the study).
            research_os_bin: Absolute path to the research-os binary.
        """
        mcp_files = [
            project_path / ".mcp.json",
            project_path / ".cursor" / "mcp.json",
            project_path / ".claude" / "mcp.json",
        ]
        for mcp_file in mcp_files:
            if not mcp_file.is_file():
                continue
            try:
                data = json.loads(mcp_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            servers = data.get("mcpServers", {})
            if "research-os" in servers:
                servers["research-os"]["command"] = str(research_os_bin)
                mcp_file.write_text(
                    json.dumps(data, indent=2) + "\n", encoding="utf-8"
                )

    def _write_workspace(self, project_path: Path, name: str) -> None:
        """Write ``<project_path>/<name>.code-workspace``.

        Content:
        - One folder entry: name ``ResearchOS: <name>``, path ``.``
        - Empty settings dict
        - Extensions with ``anthropic.claude-code`` recommended

        Args:
            project_path: The project directory.
            name: The study name (used for the workspace file name and folder label).
        """
        ws_file = project_path / f"{name}.code-workspace"
        workspace = {
            "folders": [{"name": f"ResearchOS: {name}", "path": "."}],
            "settings": {},
            "extensions": {"recommendations": ["anthropic.claude-code"]},
        }
        ws_file.write_text(
            json.dumps(workspace, indent=2) + "\n", encoding="utf-8"
        )

    def _git_init_and_commit(self, project_path: Path, name: str) -> None:
        """Initialize a git repo and create an initial commit.

        Runs:
        1. ``git -C <project_path> init``
        2. ``git -C <project_path> add -A``
        3. ``git -C <project_path> commit -m "Initialize Research OS project <name>"``

        research-os init already writes a ``.gitignore`` — we do not overwrite it.
        Raises RuntimeError on any git failure (caller catches and warns).

        Args:
            project_path: The project directory.
            name: The study name (used in the commit message).
        """
        # Ensure git user config for commits
        _git_run(project_path, ["git", "init"])
        # Configure minimal identity if not set (needed in CI / bare envs)
        try:
            _git_run(project_path, ["git", "config", "user.email", "kbu@localhost"])
            _git_run(project_path, ["git", "config", "user.name", "kbu"])
        except RuntimeError:
            pass  # best-effort; will use global config if set
        _git_run(project_path, ["git", "add", "-A"])
        _git_run(
            project_path,
            ["git", "commit", "-m", f"Initialize Research OS project {name}"],
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _validate_name(name: str) -> None:
    """Raise ValueError if *name* does not match the allowed pattern.

    Pattern: ``^[A-Za-z0-9._-]+$`` (no slashes or other special characters).

    Args:
        name: The parent or study name to validate.

    Raises:
        ValueError: If name is invalid.
    """
    if not _NAME_PATTERN.match(name):
        raise ValueError(
            f"Invalid name: {name!r}\n"
            "Names must match [A-Za-z0-9._-]+ "
            "(no slashes or other special characters)."
        )


def _git_run(project_path: Path, cmd: list[str]) -> None:
    """Run a git command, raising RuntimeError on failure.

    Args:
        project_path: The project directory (used for -C flag when not in cmd).
        cmd: Full command list.

    Raises:
        RuntimeError: On non-zero exit.
    """
    # Inject -C if not already in the command
    if "-C" not in cmd and len(cmd) > 1 and cmd[0] == "git":
        cmd = ["git", "-C", str(project_path)] + cmd[1:]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"git command failed (rc={result.returncode}): {' '.join(cmd)}\n"
            + (result.stderr or result.stdout or "").strip()
        )


def _open_cursor_workspace(ws_file: Path) -> None:
    """Launch Cursor on *ws_file*.

    If ``cursor`` is not on PATH, prints the workspace path and a manual
    instruction rather than failing hard.

    Args:
        ws_file: Path to the ``.code-workspace`` file.
    """
    import shutil

    cursor_bin = shutil.which("cursor")
    if cursor_bin:
        subprocess.Popen([cursor_bin, str(ws_file)])  # noqa: S603
        print(f"Cursor opened: {ws_file}")
    else:
        print(
            f"cursor is not on PATH. Open the workspace manually:\n"
            f"  {ws_file}"
        )
