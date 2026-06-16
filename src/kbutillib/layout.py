"""``kbutillib.layout`` — canonical repository layout knowledge.

Every other module that needs to know about directory names, shared dirs, or
gitignore patterns asks this module rather than hard-coding the answers.

Public API (BERIL path)
-----------------------
DEFAULT_SHARED_DIRS : tuple[str, ...]
    The built-in shared-dir names: ("data", "models", "genomes").

read_shared_dirs(project_root)
    Read ``[layout.shared_dirs]`` from ``kbu-project.toml``, falling back to
    ``list(DEFAULT_SHARED_DIRS)`` when absent or when the file is missing.

subproject_subdirs(*, adopted)
    Return the canonical per-subproject subdirectory names.

subproject_gitignore_lines()
    Gitignore patterns written per subproject at adopt / create time.

root_gitignore_lines(shared_dirs)
    Root-level gitignore patterns for large-file types inside shared dirs.

Public API (work-notebook path)
--------------------------------
WORKNB_SHARED_ROOTS : tuple[str, ...]
    Shared directories that live directly under ``notebooks/``:
    ``("models", "genomes", "data")``.

WORKNB_PRJ_SUBDIRS : tuple[str, ...]
    Per-PRJ subdirectories created inside each ``PRJ-<topic>/``:
    ``("NBCache", "NBOutput")``.

WORKNB_GITIGNORE_MARKER_START : str
    Opening marker for the work-notebook gitignore block.

WORKNB_GITIGNORE_MARKER_END : str
    Closing marker for the work-notebook gitignore block.

worknb_gitignore_lines()
    Return the three gitignore patterns for the work-notebook marker block.

apply_worknb_gitignore_block(gitignore_path)
    Append or replace the work-notebook gitignore block in a ``.gitignore``
    file idempotently.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

# ---------------------------------------------------------------------------
# BERIL constants
# ---------------------------------------------------------------------------

#: Built-in shared directory names (tuple for immutability).
DEFAULT_SHARED_DIRS: tuple[str, ...] = ("data", "models", "genomes")

# ---------------------------------------------------------------------------
# Work-notebook constants
# ---------------------------------------------------------------------------

#: Shared directories that live directly under ``notebooks/`` in a
#: work-notebook repo.  Order matches the PRD directory convention.
WORKNB_SHARED_ROOTS: tuple[str, ...] = ("models", "genomes", "data")

#: Per-PRJ subdirectories created inside each ``PRJ-<topic>/`` folder.
WORKNB_PRJ_SUBDIRS: tuple[str, ...] = ("NBCache", "NBOutput")

#: Opening delimiter for the root-level work-notebook gitignore block.
WORKNB_GITIGNORE_MARKER_START: str = "# >>> kbu work-notebook gitignore >>>"

#: Closing delimiter for the root-level work-notebook gitignore block.
WORKNB_GITIGNORE_MARKER_END: str = "# <<< kbu work-notebook gitignore <<<"

# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def read_shared_dirs(project_root: Path) -> list[str]:
    """Return the shared-dir list for *project_root*.

    Reads ``[layout.shared_dirs]`` from ``kbu-project.toml`` in
    *project_root*.  Falls back to ``list(DEFAULT_SHARED_DIRS)`` when:

    - ``kbu-project.toml`` does not exist.
    - The ``[layout]`` table is absent.
    - The ``shared_dirs`` key is absent from ``[layout]``.

    Unknown keys inside ``[layout]`` are silently ignored.

    Raises
    ------
    tomllib.TOMLDecodeError
        If the file exists but is malformed TOML.
    """
    toml_path = project_root / "kbu-project.toml"
    if not toml_path.exists():
        return list(DEFAULT_SHARED_DIRS)

    with toml_path.open("rb") as fh:
        data = tomllib.load(fh)

    layout_section = data.get("layout", {})
    shared = layout_section.get("shared_dirs", None)
    if shared is None:
        return list(DEFAULT_SHARED_DIRS)
    return list(shared)


def subproject_subdirs(*, adopted: bool) -> list[str]:
    """Return the canonical subproject subdirectory names.

    Parameters
    ----------
    adopted:
        When ``True`` (i.e. the subproject was created by ``kbu subproject
        adopt``), ``"archive"`` is appended to the list (7 entries total).
        When ``False``, returns the standard 6-entry list.

    Returns
    -------
    list[str]
        Ordered list of subdirectory names.  The order is contractual —
        tests pin it and scaffolding creates them in order.

        Non-adopted (6 entries):
            ``["notebooks", "figures", "nboutput", ".cache", "literature",
            "sessions"]``

        Adopted (7 entries):
            Same list with ``"archive"`` appended.
    """
    dirs = ["notebooks", "figures", "nboutput", ".cache", "literature", "sessions"]
    if adopted:
        dirs.append("archive")
    return dirs


def subproject_gitignore_lines() -> list[str]:
    """Return the gitignore patterns added per subproject.

    These patterns are written into the *root* ``.gitignore`` under a
    per-subproject marker block when a subproject is created or adopted.

    Returns
    -------
    list[str]
        Exactly, in order: ``[".cache/", "nboutput/", ".adoption-notes.md"]``
    """
    return [".cache/", "nboutput/", ".adoption-notes.md"]


def root_gitignore_lines(shared_dirs: list[str]) -> list[str]:
    """Return root-level gitignore patterns for large-file types in shared dirs.

    For each directory name in *shared_dirs* (preserving input order), emits
    three glob patterns that ignore large binary files anywhere under that
    directory:

    - ``<dir>/**/*.h5``
    - ``<dir>/**/*.pkl``
    - ``<dir>/**/*.parquet``

    No other patterns are included.

    Parameters
    ----------
    shared_dirs:
        List of shared directory names, e.g. ``["data", "models", "genomes"]``.

    Returns
    -------
    list[str]
        ``len(shared_dirs) * 3`` entries, one per (dir, extension) pair.
    """
    extensions = ("*.h5", "*.pkl", "*.parquet")
    lines: list[str] = []
    for d in shared_dirs:
        for ext in extensions:
            lines.append(f"{d}/**/{ext}")
    return lines


# ---------------------------------------------------------------------------
# Work-notebook public functions
# ---------------------------------------------------------------------------


def worknb_gitignore_lines() -> list[str]:
    """Return the gitignore patterns for the work-notebook marker block.

    These three patterns cover the per-PRJ cache and output directories
    (glob-matched under ``notebooks/PRJ-*/``) and the standard Jupyter
    checkpoint directory.

    Returns
    -------
    list[str]
        Exactly, in order:

        1. ``"notebooks/PRJ-*/NBCache/"``
        2. ``"notebooks/PRJ-*/NBOutput/"``
        3. ``".ipynb_checkpoints/"``
    """
    return [
        "notebooks/PRJ-*/NBCache/",
        "notebooks/PRJ-*/NBOutput/",
        ".ipynb_checkpoints/",
    ]


def apply_worknb_gitignore_block(gitignore_path: Path) -> None:
    """Append or replace the work-notebook gitignore block idempotently.

    Reads the file at *gitignore_path* (creating it if absent), locates the
    block delimited by :data:`WORKNB_GITIGNORE_MARKER_START` and
    :data:`WORKNB_GITIGNORE_MARKER_END`, and either replaces it with the
    canonical content from :func:`worknb_gitignore_lines` or appends a new
    block when none is present.

    The operation is idempotent: calling this function twice on the same file
    produces the same result as calling it once.  Lines outside the marker
    block are never modified.

    Parameters
    ----------
    gitignore_path:
        Absolute or relative path to the ``.gitignore`` file to update.
        Parent directories must already exist.

    Raises
    ------
    OSError
        If the file cannot be read or written (e.g. permission error).
    """
    start = WORKNB_GITIGNORE_MARKER_START
    end = WORKNB_GITIGNORE_MARKER_END

    # Read existing content, or start empty.
    if gitignore_path.exists():
        existing = gitignore_path.read_text(encoding="utf-8")
    else:
        existing = ""

    # Build the canonical block (start marker, body lines, end marker).
    body_lines = worknb_gitignore_lines()
    block_lines = [start] + body_lines + [end]
    block_text = "\n".join(block_lines) + "\n"

    # Check whether the marker block is already present.
    if start in existing:
        # Replace the existing block (everything between start and end markers,
        # inclusive).  We do a line-by-line pass so we handle edge-cases such
        # as a missing end marker gracefully.
        output_lines: list[str] = []
        inside = False
        replaced = False
        for line in existing.splitlines(keepends=True):
            stripped = line.rstrip("\n").rstrip("\r")
            if stripped == start:
                if not replaced:
                    # Emit the canonical block in place of the old one.
                    output_lines.append(block_text)
                    replaced = True
                inside = True
                continue
            if inside:
                if stripped == end:
                    inside = False
                # Skip all lines that were inside the old block.
                continue
            output_lines.append(line)
        new_content = "".join(output_lines)
    else:
        # Append the block, ensuring there is exactly one blank line before it
        # when the file already has non-empty content.
        if existing and not existing.endswith("\n"):
            existing += "\n"
        separator = "\n" if existing else ""
        new_content = existing + separator + block_text

    gitignore_path.write_text(new_content, encoding="utf-8")
