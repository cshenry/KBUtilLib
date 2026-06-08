"""``kbutillib.layout`` — canonical repository layout knowledge.

Every other module that needs to know about directory names, shared dirs, or
gitignore patterns asks this module rather than hard-coding the answers.

Public API
----------
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
"""

from __future__ import annotations

import tomllib
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Built-in shared directory names (tuple for immutability).
DEFAULT_SHARED_DIRS: tuple[str, ...] = ("data", "models", "genomes")

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
