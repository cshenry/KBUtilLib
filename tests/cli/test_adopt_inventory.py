"""Tests for kbutillib.cli.adopt._inventory — AC #32 through #38.

Each test class documents which Acceptance Criterion it covers.
All tests use ``tmp_path`` fixtures to build fake directory trees.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import nbformat
import pytest

from kbutillib.cli.adopt._inventory import (
    AdoptionInventory,
    _is_relative_path,
    scan_archive,
    write_adoption_notes,
)


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_notebook(
    *,
    markdown_cells: list[str] | None = None,
    code_cells: list[str] | None = None,
) -> dict[str, Any]:
    """Build a minimal nbformat-4 notebook dict."""
    cells = []
    for src in markdown_cells or []:
        cells.append(
            nbformat.v4.new_markdown_cell(src)  # type: ignore[no-untyped-call]
        )
    for src in code_cells or []:
        cells.append(
            nbformat.v4.new_code_cell(src)  # type: ignore[no-untyped-call]
        )
    nb = nbformat.v4.new_notebook(cells=cells)  # type: ignore[no-untyped-call]
    return nb  # type: ignore[return-value]


def _write_notebook(path: Path, nb: dict[str, Any]) -> None:
    """Serialise *nb* to *path* using nbformat."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        nbformat.write(nb, fh)  # type: ignore[arg-type]


def _make_archive(tmp_path: Path) -> Path:
    """Create a standard fixture archive directory used by multiple tests."""
    archive = tmp_path / "archive"
    archive.mkdir()
    return archive


# ── AC #32: paths are relative to archive_dir ─────────────────────────────────


class TestRelativePaths:
    """AC #32 — scan_archive returns paths relative to archive_dir."""

    def test_notebook_paths_are_relative(self, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path)
        nb = _make_notebook(markdown_cells=["# Hello"])
        _write_notebook(archive / "01_explore.ipynb", nb)

        inv = scan_archive(archive)

        assert len(inv.notebooks) == 1
        nb_path = inv.notebooks[0]
        # Must be a relative path (no leading /), specifically just the filename
        assert not nb_path.is_absolute()
        assert nb_path == Path("01_explore.ipynb")

    def test_nested_notebook_path_is_relative(self, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path)
        subdir = archive / "analysis"
        subdir.mkdir()
        nb = _make_notebook(markdown_cells=["# Analysis"])
        _write_notebook(subdir / "nested.ipynb", nb)

        inv = scan_archive(archive)

        assert len(inv.notebooks) == 1
        assert inv.notebooks[0] == Path("analysis") / "nested.ipynb"

    def test_subdir_paths_are_relative(self, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path)
        (archive / "data").mkdir()
        (archive / "data" / "dummy.csv").write_text("a,b\n1,2\n")

        inv = scan_archive(archive)

        assert len(inv.subdirs) == 1
        rel_dir, _ = inv.subdirs[0]
        assert not rel_dir.is_absolute()
        assert rel_dir == Path("data")

    def test_oversize_paths_are_relative(self, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path)
        big = archive / "big.bin"
        big.write_bytes(b"x" * 10_000_001)

        inv = scan_archive(archive)

        assert len(inv.oversize_files) == 1
        rel_file, _ = inv.oversize_files[0]
        assert not rel_file.is_absolute()
        assert rel_file == Path("big.bin")


# ── AC #33: does not follow symlinks ──────────────────────────────────────────


class TestNoSymlinkFollow:
    """AC #33 — scan_archive does not follow symlinks."""

    def test_symlink_to_dir_not_followed(self, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path)
        real_dir = tmp_path / "real_data"
        real_dir.mkdir()
        nb = _make_notebook(markdown_cells=["# Real"])
        _write_notebook(real_dir / "hidden.ipynb", nb)

        link = archive / "linked"
        link.symlink_to(real_dir)

        inv = scan_archive(archive)

        # The symlinked directory must not be traversed
        notebook_names = [p.name for p in inv.notebooks]
        assert "hidden.ipynb" not in notebook_names

    def test_symlink_to_file_size_check(self, tmp_path: Path) -> None:
        """Symlinked files in the archive directory appear in os.walk filenames.

        The ``followlinks=False`` flag controls *directory* symlink traversal
        only.  A symlinked file is still listed in ``filenames``; the
        implementation records it but uses ``is_file()`` which returns True for
        symlinks-to-files.  This test just verifies the scanner doesn't crash
        on a symlinked file.
        """
        archive = _make_archive(tmp_path)
        real_nb_path = tmp_path / "external.ipynb"
        nb = _make_notebook(markdown_cells=["# External"])
        _write_notebook(real_nb_path, nb)

        link = archive / "linked.ipynb"
        link.symlink_to(real_nb_path)

        # Must not raise; symlinked file may or may not appear depending on platform
        inv = scan_archive(archive)
        assert isinstance(inv, AdoptionInventory)


# ── AC #34: skips .ipynb_checkpoints and other dot dirs ──────────────────────


class TestDotDirSkip:
    """AC #34 — .ipynb_checkpoints and other dotted subdirs are skipped."""

    def test_ipynb_checkpoints_skipped(self, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path)
        checkpoints = archive / ".ipynb_checkpoints"
        checkpoints.mkdir()
        nb = _make_notebook(markdown_cells=["# Checkpoint version"])
        _write_notebook(checkpoints / "01-checkpoint.ipynb", nb)

        inv = scan_archive(archive)

        assert len(inv.notebooks) == 0

    def test_other_dot_dir_skipped(self, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path)
        hidden = archive / ".hidden"
        hidden.mkdir()
        nb = _make_notebook(markdown_cells=["# Hidden"])
        _write_notebook(hidden / "secret.ipynb", nb)

        inv = scan_archive(archive)

        assert len(inv.notebooks) == 0

    def test_dot_dir_not_in_subdirs(self, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path)
        (archive / ".cache").mkdir()
        (archive / ".cache" / "stuff.dat").write_bytes(b"data")

        inv = scan_archive(archive)

        subdir_names = [p.name for p, _ in inv.subdirs]
        assert ".cache" not in subdir_names

    def test_real_notebook_alongside_checkpoint_found(self, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path)
        # Real notebook at top level
        nb = _make_notebook(markdown_cells=["# Main"])
        _write_notebook(archive / "main.ipynb", nb)
        # Checkpoint should be skipped
        checkpoints = archive / ".ipynb_checkpoints"
        checkpoints.mkdir()
        _write_notebook(checkpoints / "main-checkpoint.ipynb", nb)

        inv = scan_archive(archive)

        assert len(inv.notebooks) == 1
        assert inv.notebooks[0] == Path("main.ipynb")

    def test_nested_dot_dir_skipped(self, tmp_path: Path) -> None:
        """Dot dirs nested inside real dirs are also skipped."""
        archive = _make_archive(tmp_path)
        real_sub = archive / "analysis"
        real_sub.mkdir()
        hidden_nested = real_sub / ".hidden_nested"
        hidden_nested.mkdir()
        nb = _make_notebook(markdown_cells=["# Nested hidden"])
        _write_notebook(hidden_nested / "nope.ipynb", nb)

        inv = scan_archive(archive)

        assert len(inv.notebooks) == 0


# ── AC #35: oversize threshold strictly > 10_000_000 ─────────────────────────


class TestOversizeThreshold:
    """AC #35 — oversize threshold is strictly > 10_000_000 bytes."""

    def test_exactly_10_million_not_flagged(self, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path)
        f = archive / "exact.bin"
        f.write_bytes(b"x" * 10_000_000)

        inv = scan_archive(archive)

        assert len(inv.oversize_files) == 0

    def test_one_byte_over_threshold_flagged(self, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path)
        f = archive / "just_over.bin"
        f.write_bytes(b"x" * 10_000_001)

        inv = scan_archive(archive)

        assert len(inv.oversize_files) == 1
        rel_path, size = inv.oversize_files[0]
        assert rel_path == Path("just_over.bin")
        assert size == 10_000_001

    def test_small_file_not_flagged(self, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path)
        f = archive / "small.txt"
        f.write_text("tiny content")

        inv = scan_archive(archive)

        assert len(inv.oversize_files) == 0

    def test_multiple_oversize_files_all_flagged(self, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path)
        for i in range(3):
            f = archive / f"big_{i}.bin"
            f.write_bytes(b"x" * (10_000_001 + i))

        inv = scan_archive(archive)

        assert len(inv.oversize_files) == 3


# ── AC #36: nbformat.read with as_version=4; first markdown cell ───────────────


class TestNotebookReading:
    """AC #36 — nbformat.read is used; first markdown cell source captured."""

    def test_first_markdown_cell_captured_in_path_refs(self, tmp_path: Path) -> None:
        """Relative paths in the first markdown cell must be detected."""
        archive = _make_archive(tmp_path)
        nb = _make_notebook(
            markdown_cells=["# Hypothesis\nLoad `open('data/raw.csv', 'r')` here"],
        )
        _write_notebook(archive / "nb.ipynb", nb)

        inv = scan_archive(archive)

        assert Path("nb.ipynb") in inv.path_refs
        assert "data/raw.csv" in inv.path_refs[Path("nb.ipynb")]

    def test_notebook_with_no_cells_handled(self, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path)
        nb = _make_notebook()
        _write_notebook(archive / "empty.ipynb", nb)

        inv = scan_archive(archive)

        assert Path("empty.ipynb") in inv.notebooks
        # No path refs expected from empty notebook
        assert Path("empty.ipynb") not in inv.path_refs

    def test_code_cell_paths_also_detected(self, tmp_path: Path) -> None:
        """Path refs in code cells (not just markdown) must also be captured."""
        archive = _make_archive(tmp_path)
        nb = _make_notebook(
            code_cells=["import pandas as pd\ndf = pd.read_csv('results/output.tsv')"],
        )
        _write_notebook(archive / "code_nb.ipynb", nb)

        inv = scan_archive(archive)

        assert Path("code_nb.ipynb") in inv.path_refs
        assert "results/output.tsv" in inv.path_refs[Path("code_nb.ipynb")]

    def test_multiple_notebooks_all_scanned(self, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path)
        for i, name in enumerate(["01_load.ipynb", "02_clean.ipynb"]):
            nb = _make_notebook(
                markdown_cells=[f"# Notebook {i}"],
                code_cells=[f"pd.read_csv('data/{name}.csv')"],
            )
            _write_notebook(archive / name, nb)

        inv = scan_archive(archive)

        assert len(inv.notebooks) == 2


# ── AC #37: path classification — relative vs absolute ────────────────────────


class TestPathClassification:
    """AC #37 — paths not starting with /, ~, or {PROJECT_ROOT} are flagged relative."""

    def test_relative_path_flagged(self) -> None:
        assert _is_relative_path("data/foo.csv") is True

    def test_absolute_path_not_flagged(self) -> None:
        assert _is_relative_path("/home/user/data/foo.csv") is False

    def test_tilde_path_not_flagged(self) -> None:
        assert _is_relative_path("~/data/foo.csv") is False

    def test_project_root_token_not_flagged(self) -> None:
        assert _is_relative_path("{PROJECT_ROOT}/data/foo.csv") is False

    def test_project_root_in_middle_not_flagged(self) -> None:
        assert _is_relative_path("some/{PROJECT_ROOT}/data") is False

    def test_empty_string_is_relative(self) -> None:
        # Edge case: empty string has no / or ~ prefix
        assert _is_relative_path("") is True

    def test_dot_relative_path_flagged(self) -> None:
        assert _is_relative_path("./data/foo.csv") is True

    def test_scan_archive_absolute_path_not_in_refs(self, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path)
        nb = _make_notebook(
            code_cells=["pd.read_csv('/absolute/path/data.csv')"],
        )
        _write_notebook(archive / "nb.ipynb", nb)

        inv = scan_archive(archive)

        # Absolute path must not appear in path_refs
        assert Path("nb.ipynb") not in inv.path_refs

    def test_scan_archive_tilde_path_not_in_refs(self, tmp_path: Path) -> None:
        archive = _make_archive(tmp_path)
        nb = _make_notebook(
            code_cells=["open('~/home_data.csv')"],
        )
        _write_notebook(archive / "nb.ipynb", nb)

        inv = scan_archive(archive)

        assert Path("nb.ipynb") not in inv.path_refs


# ── AC #38: all five regex patterns match documented forms ────────────────────


class TestRegexPatterns:
    """AC #38 — regex set matches pd.read_*, open(, Path(, np.load, joblib.load."""

    def _archive_with_code(self, tmp_path: Path, code: str) -> Path:
        archive = _make_archive(tmp_path)
        nb = _make_notebook(code_cells=[code])
        _write_notebook(archive / "nb.ipynb", nb)
        return archive

    def test_pd_read_csv(self, tmp_path: Path) -> None:
        archive = self._archive_with_code(tmp_path, "pd.read_csv('data/in.csv')")
        inv = scan_archive(archive)
        assert "data/in.csv" in inv.path_refs.get(Path("nb.ipynb"), [])

    def test_pd_read_tsv(self, tmp_path: Path) -> None:
        archive = self._archive_with_code(tmp_path, "pd.read_tsv('data/in.tsv')")
        inv = scan_archive(archive)
        assert "data/in.tsv" in inv.path_refs.get(Path("nb.ipynb"), [])

    def test_pd_read_excel(self, tmp_path: Path) -> None:
        archive = self._archive_with_code(tmp_path, 'pd.read_excel("data/table.xlsx")')
        inv = scan_archive(archive)
        assert "data/table.xlsx" in inv.path_refs.get(Path("nb.ipynb"), [])

    def test_pd_read_parquet(self, tmp_path: Path) -> None:
        archive = self._archive_with_code(tmp_path, "pd.read_parquet('data/in.parquet')")
        inv = scan_archive(archive)
        assert "data/in.parquet" in inv.path_refs.get(Path("nb.ipynb"), [])

    def test_pd_read_hdf(self, tmp_path: Path) -> None:
        archive = self._archive_with_code(tmp_path, "pd.read_hdf('store.h5')")
        inv = scan_archive(archive)
        assert "store.h5" in inv.path_refs.get(Path("nb.ipynb"), [])

    def test_pd_read_json(self, tmp_path: Path) -> None:
        archive = self._archive_with_code(tmp_path, "pd.read_json('meta.json')")
        inv = scan_archive(archive)
        assert "meta.json" in inv.path_refs.get(Path("nb.ipynb"), [])

    def test_open(self, tmp_path: Path) -> None:
        archive = self._archive_with_code(tmp_path, "with open('data/raw.txt', 'r') as f:")
        inv = scan_archive(archive)
        assert "data/raw.txt" in inv.path_refs.get(Path("nb.ipynb"), [])

    def test_path(self, tmp_path: Path) -> None:
        archive = self._archive_with_code(tmp_path, "p = Path('figures/fig1.png')")
        inv = scan_archive(archive)
        assert "figures/fig1.png" in inv.path_refs.get(Path("nb.ipynb"), [])

    def test_np_load(self, tmp_path: Path) -> None:
        archive = self._archive_with_code(tmp_path, "arr = np.load('arrays/data.npy')")
        inv = scan_archive(archive)
        assert "arrays/data.npy" in inv.path_refs.get(Path("nb.ipynb"), [])

    def test_joblib_load(self, tmp_path: Path) -> None:
        archive = self._archive_with_code(tmp_path, "model = joblib.load('models/clf.pkl')")
        inv = scan_archive(archive)
        assert "models/clf.pkl" in inv.path_refs.get(Path("nb.ipynb"), [])

    def test_multiple_patterns_in_one_cell(self, tmp_path: Path) -> None:
        code = (
            "import pandas as pd\n"
            "df = pd.read_csv('data/input.csv')\n"
            "arr = np.load('arrays/weights.npy')\n"
            "model = joblib.load('models/rf.pkl')\n"
            "with open('logs/run.log', 'w') as f: pass\n"
            "p = Path('figures/out.png')\n"
        )
        archive = self._archive_with_code(tmp_path, code)
        inv = scan_archive(archive)
        refs = inv.path_refs.get(Path("nb.ipynb"), [])
        assert "data/input.csv" in refs
        assert "arrays/weights.npy" in refs
        assert "models/rf.pkl" in refs
        assert "logs/run.log" in refs
        assert "figures/out.png" in refs

    def test_double_quoted_strings_matched(self, tmp_path: Path) -> None:
        archive = self._archive_with_code(tmp_path, 'pd.read_csv("data/double.csv")')
        inv = scan_archive(archive)
        assert "data/double.csv" in inv.path_refs.get(Path("nb.ipynb"), [])

    def test_whitespace_before_open_paren_matched(self, tmp_path: Path) -> None:
        """Regex patterns allow optional whitespace before (."""
        archive = self._archive_with_code(tmp_path, "np.load  ('arrays/data.npy')")
        inv = scan_archive(archive)
        assert "arrays/data.npy" in inv.path_refs.get(Path("nb.ipynb"), [])


# ── write_adoption_notes integration ─────────────────────────────────────────


class TestWriteAdoptionNotes:
    """Integration smoke tests for write_adoption_notes."""

    def test_creates_file(self, tmp_path: Path) -> None:
        sp_dir = tmp_path / "subprojects" / "mysp"
        sp_dir.mkdir(parents=True)
        archive = sp_dir / "archive"
        archive.mkdir()
        nb = _make_notebook(markdown_cells=["# Test"])
        _write_notebook(archive / "nb.ipynb", nb)

        write_adoption_notes(sp_dir, archive, Path("/original/path"))

        assert (sp_dir / ".adoption-notes.md").exists()

    def test_sections_present(self, tmp_path: Path) -> None:
        sp_dir = tmp_path / "subprojects" / "mysp"
        sp_dir.mkdir(parents=True)
        archive = sp_dir / "archive"
        archive.mkdir()
        nb = _make_notebook(
            markdown_cells=["# Test"],
            code_cells=["pd.read_csv('data/input.csv')"],
        )
        _write_notebook(archive / "nb.ipynb", nb)
        (archive / "data").mkdir()
        (archive / "data" / "stuff.csv").write_text("a,b\n")
        (archive / "big.bin").write_bytes(b"x" * 10_000_001)

        write_adoption_notes(sp_dir, archive, Path("/orig"))

        text = (sp_dir / ".adoption-notes.md").read_text(encoding="utf-8")
        assert "## Notebooks found" in text
        assert "## Subdirectories found" in text
        assert "## Oversize files (>10MB)" in text
        assert "## Per-notebook path references" in text

    def test_oversize_section_shows_file(self, tmp_path: Path) -> None:
        sp_dir = tmp_path / "subprojects" / "mysp"
        sp_dir.mkdir(parents=True)
        archive = sp_dir / "archive"
        archive.mkdir()
        (archive / "large.bin").write_bytes(b"x" * 10_000_001)

        write_adoption_notes(sp_dir, archive, Path("/orig"))

        text = (sp_dir / ".adoption-notes.md").read_text(encoding="utf-8")
        assert "large.bin" in text
