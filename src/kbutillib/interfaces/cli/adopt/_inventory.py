"""Adoption inventory scanner for ``kbu subproject adopt``.

Provides :func:`scan_archive` (pure function, easily unit-testable) and
:func:`write_adoption_notes` which serialises the inventory to a markdown
worksheet inside the new subproject directory.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import nbformat


# ── regex set ─────────────────────────────────────────────────────────────────

#: Compiled patterns used to detect path references inside notebook source.
#: Each pattern must have exactly one capturing group whose content is the
#: path string being passed to the function.
_PATH_REF_PATTERNS: list[re.Pattern[str]] = [
    # pd.read_csv/tsv/excel/parquet/hdf/json("path")
    re.compile(
        r"""pd\.read_(?:csv|tsv|excel|parquet|hdf|json)\s*\(\s*["']([^"']+)["']"""
    ),
    # open("path"
    re.compile(r"""open\s*\(\s*["']([^"']+)["']"""),
    # Path("path"
    re.compile(r"""Path\s*\(\s*["']([^"']+)["']"""),
    # np.load("path"
    re.compile(r"""np\.load\s*\(\s*["']([^"']+)["']"""),
    # joblib.load("path"
    re.compile(r"""joblib\.load\s*\(\s*["']([^"']+)["']"""),
]


def _is_relative_path(path_str: str) -> bool:
    """Return True iff *path_str* is classified as a relative path reference.

    A path is *relative* when it does **not** start with ``/``, does **not**
    start with ``~``, and does **not** contain the literal token
    ``{PROJECT_ROOT}``.
    """
    if path_str.startswith("/"):
        return False
    if path_str.startswith("~"):
        return False
    if "{PROJECT_ROOT}" in path_str:
        return False
    return True


def _should_skip_dir(name: str, *, is_top_level: bool) -> bool:
    """Return True if a directory with *name* should be excluded from traversal.

    Skips ``.ipynb_checkpoints`` and any other dot-prefixed directory **other
    than** the top-level archive dir itself (which is handled by the caller).
    """
    if is_top_level:
        return False
    if name == ".ipynb_checkpoints":
        return True
    if name.startswith("."):
        return True
    return False


def _extract_notebook_path_refs(notebook_path: Path) -> list[str]:
    """Read *notebook_path* and return all relative path-reference hits.

    Uses :func:`nbformat.read` with ``as_version=4``.  Scans all cell
    sources (code and markdown) and returns a de-duplicated list of the
    matched path strings that are classified as relative.
    """
    try:
        nb = nbformat.read(str(notebook_path), as_version=4)
    except Exception:
        return []

    hits: list[str] = []
    for cell in nb.cells:
        source: str = cell.get("source", "")
        for pattern in _PATH_REF_PATTERNS:
            for m in pattern.finditer(source):
                path_str = m.group(1)
                if _is_relative_path(path_str) and path_str not in hits:
                    hits.append(path_str)
    return hits


def _read_first_markdown_cell(notebook_path: Path) -> str:
    """Return the ``source`` field of the first markdown cell in *notebook_path*.

    Returns an empty string if there are no markdown cells or if the
    notebook cannot be read.
    """
    try:
        nb = nbformat.read(str(notebook_path), as_version=4)
    except Exception:
        return ""
    for cell in nb.cells:
        if cell.get("cell_type") == "markdown":
            return cell.get("source", "")
    return ""


# ── dataclass ──────────────────────────────────────────────────────────────────


@dataclass
class AdoptionInventory:
    """Result of scanning an ``archive/`` directory.

    All :class:`~pathlib.Path` values are **relative to** *archive_dir*.
    """

    #: All ``.ipynb`` files found, relative to *archive_dir*.
    notebooks: list[Path] = field(default_factory=list)

    #: ``(relative_path, total_size_in_bytes)`` for each sub-directory.
    subdirs: list[tuple[Path, int]] = field(default_factory=list)

    #: ``(relative_path, size_in_bytes)`` for files strictly ``> 10_000_000`` bytes.
    oversize_files: list[tuple[Path, int]] = field(default_factory=list)

    #: ``{relative_notebook_path: [matched_path_strings, ...]}`` for regex hits.
    path_refs: dict[Path, list[str]] = field(default_factory=dict)


# ── public API ─────────────────────────────────────────────────────────────────


def scan_archive(archive_dir: Path) -> AdoptionInventory:
    """Scan *archive_dir* and return an :class:`AdoptionInventory`.

    Traversal rules:

    * Does **not** follow symlinks (``followlinks=False``).
    * Skips directories named ``.ipynb_checkpoints`` and any dot-prefixed
      directory *inside* *archive_dir* (the top-level dir itself is not
      skipped).
    * Records all ``.ipynb`` files with paths **relative to** *archive_dir*.
    * Computes per-subdir total size (bytes, non-recursive for the dir entry
      itself; recursive for files inside — follows the natural walk).
    * Flags files whose size is strictly ``> 10_000_000`` bytes as oversize.
    * Extracts relative path references from each notebook via the
      :data:`_PATH_REF_PATTERNS` regex set.

    Parameters
    ----------
    archive_dir:
        Absolute path to the ``archive/`` directory to scan.

    Returns
    -------
    AdoptionInventory
        Populated inventory.
    """
    inventory = AdoptionInventory()

    # We use os.walk so we can prune dirs in-place.
    for dirpath_str, dirnames, filenames in os.walk(
        str(archive_dir), followlinks=False
    ):
        dirpath = Path(dirpath_str)
        is_top = dirpath == archive_dir

        # Prune directories in-place (modifies dirnames so os.walk respects it)
        dirnames[:] = [
            d for d in dirnames
            if not _should_skip_dir(d, is_top_level=False)
        ]

        if not is_top:
            # Record this subdir with its total size
            rel_dir = dirpath.relative_to(archive_dir)
            dir_size = sum(
                (dirpath / f).stat().st_size
                for f in filenames
                if (dirpath / f).is_file()
            )
            inventory.subdirs.append((rel_dir, dir_size))

        for fname in filenames:
            fpath = dirpath / fname
            if not fpath.is_file():
                # skip symlinks to non-files etc.
                continue
            rel_file = fpath.relative_to(archive_dir)
            fsize = fpath.stat().st_size

            if fsize > 10_000_000:
                inventory.oversize_files.append((rel_file, fsize))

            if fname.endswith(".ipynb"):
                inventory.notebooks.append(rel_file)
                refs = _extract_notebook_path_refs(fpath)
                if refs:
                    inventory.path_refs[rel_file] = refs

    return inventory


def write_adoption_notes(
    subproject_dir: Path,
    archive_dir: Path,
    source_path: Path,
) -> None:
    """Write ``.adoption-notes.md`` into *subproject_dir*.

    Scans *archive_dir* via :func:`scan_archive` and renders a markdown
    worksheet with sections for:

    * Notebooks found
    * Subdirectories found
    * Oversize files (>10 MB)
    * Per-notebook path-reference grep hits

    Parameters
    ----------
    subproject_dir:
        The newly-created subproject directory (the file is written here).
    archive_dir:
        The ``archive/`` directory created by ``kbu subproject adopt``.
    source_path:
        The original source path that was adopted (for context in the notes).
    """
    inv = scan_archive(archive_dir)
    notes_path = subproject_dir / ".adoption-notes.md"

    lines: list[str] = []
    lines.append("# Adoption Notes\n")
    lines.append(
        f"Original source: `{source_path}`\n"
    )
    lines.append("")

    # ── Notebooks ──────────────────────────────────────────────────────────
    lines.append("## Notebooks found\n")
    if inv.notebooks:
        for nb in sorted(inv.notebooks):
            first_cell = _read_first_markdown_cell(archive_dir / nb)
            excerpt = first_cell.splitlines()[0][:120] if first_cell else ""
            lines.append(f"- `{nb}`")
            if excerpt:
                lines.append(f"  > {excerpt}")
    else:
        lines.append("_No notebooks found._")
    lines.append("")

    # ── Subdirectories ──────────────────────────────────────────────────────
    lines.append("## Subdirectories found\n")
    if inv.subdirs:
        for rel_dir, size in sorted(inv.subdirs):
            lines.append(f"- `{rel_dir}` ({size:,} bytes)")
    else:
        lines.append("_No subdirectories._")
    lines.append("")

    # ── Oversize files ──────────────────────────────────────────────────────
    lines.append("## Oversize files (>10MB)\n")
    if inv.oversize_files:
        for rel_file, size in sorted(inv.oversize_files):
            lines.append(f"- `{rel_file}` ({size:,} bytes)")
    else:
        lines.append("_None._")
    lines.append("")

    # ── Path reference hits ─────────────────────────────────────────────────
    lines.append("## Per-notebook path references\n")
    if inv.path_refs:
        for nb_path in sorted(inv.path_refs):
            lines.append(f"### `{nb_path}`\n")
            for ref in inv.path_refs[nb_path]:
                lines.append(f"- `{ref}`")
            lines.append("")
    else:
        lines.append("_No relative path references detected._")
    lines.append("")

    notes_path.write_text("\n".join(lines), encoding="utf-8")
