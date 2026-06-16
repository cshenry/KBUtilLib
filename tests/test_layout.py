"""Tests for kbutillib.layout — Acceptance Criteria #1–#8 (BERIL) and
work-notebook layout descriptor + gitignore-block helper.

Each AC maps to at least one test method; the AC number is cited in the
docstring.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from kbutillib.layout import (
    DEFAULT_SHARED_DIRS,
    WORKNB_GITIGNORE_MARKER_END,
    WORKNB_GITIGNORE_MARKER_START,
    WORKNB_PRJ_SUBDIRS,
    WORKNB_SHARED_ROOTS,
    apply_worknb_gitignore_block,
    read_shared_dirs,
    root_gitignore_lines,
    subproject_gitignore_lines,
    subproject_subdirs,
    worknb_gitignore_lines,
)


# ---------------------------------------------------------------------------
# AC #1 — DEFAULT_SHARED_DIRS constant
# ---------------------------------------------------------------------------


class TestDefaultSharedDirs:
    """AC #1: DEFAULT_SHARED_DIRS == ("data", "models", "genomes")."""

    def test_value(self) -> None:
        assert DEFAULT_SHARED_DIRS == ("data", "models", "genomes")

    def test_is_tuple(self) -> None:
        """Must be a tuple for immutability."""
        assert isinstance(DEFAULT_SHARED_DIRS, tuple)

    def test_length(self) -> None:
        assert len(DEFAULT_SHARED_DIRS) == 3


# ---------------------------------------------------------------------------
# AC #2 — subproject_subdirs(adopted=False)
# ---------------------------------------------------------------------------


class TestSubprojectSubdirsVirgin:
    """AC #2: subproject_subdirs(adopted=False) returns exactly the 6-entry list."""

    EXPECTED = ["notebooks", "figures", "nboutput", ".cache", "literature", "sessions"]

    def test_exact_list(self) -> None:
        assert subproject_subdirs(adopted=False) == self.EXPECTED

    def test_length(self) -> None:
        assert len(subproject_subdirs(adopted=False)) == 6

    def test_no_archive(self) -> None:
        assert "archive" not in subproject_subdirs(adopted=False)

    def test_order_preserved(self) -> None:
        """Order is contractual — creation order matters."""
        result = subproject_subdirs(adopted=False)
        assert result[0] == "notebooks"
        assert result[-1] == "sessions"


# ---------------------------------------------------------------------------
# AC #3 — subproject_subdirs(adopted=True)
# ---------------------------------------------------------------------------


class TestSubprojectSubdirsAdopted:
    """AC #3: subproject_subdirs(adopted=True) appends "archive" (7 entries)."""

    def test_exact_list(self) -> None:
        expected = [
            "notebooks", "figures", "nboutput", ".cache",
            "literature", "sessions", "archive",
        ]
        assert subproject_subdirs(adopted=True) == expected

    def test_length(self) -> None:
        assert len(subproject_subdirs(adopted=True)) == 7

    def test_archive_is_last(self) -> None:
        assert subproject_subdirs(adopted=True)[-1] == "archive"

    def test_prefix_matches_virgin(self) -> None:
        """The first 6 entries must match the non-adopted list exactly."""
        virgin = subproject_subdirs(adopted=False)
        adopted = subproject_subdirs(adopted=True)
        assert adopted[:6] == virgin


# ---------------------------------------------------------------------------
# AC #4 — subproject_gitignore_lines()
# ---------------------------------------------------------------------------


class TestSubprojectGitignoreLines:
    """AC #4: subproject_gitignore_lines() returns exactly [".cache/", "nboutput/",
    ".adoption-notes.md"] in that order."""

    EXPECTED = [".cache/", "nboutput/", ".adoption-notes.md"]

    def test_exact_list(self) -> None:
        assert subproject_gitignore_lines() == self.EXPECTED

    def test_length(self) -> None:
        assert len(subproject_gitignore_lines()) == 3

    def test_order(self) -> None:
        result = subproject_gitignore_lines()
        assert result[0] == ".cache/"
        assert result[1] == "nboutput/"
        assert result[2] == ".adoption-notes.md"


# ---------------------------------------------------------------------------
# AC #5 — root_gitignore_lines(shared_dirs)
# ---------------------------------------------------------------------------


class TestRootGitignoreLines:
    """AC #5: root_gitignore_lines(["data","models","genomes"]) returns the
    exact 9-entry list in order."""

    STANDARD_DIRS = ["data", "models", "genomes"]
    EXPECTED_9 = [
        "data/**/*.h5", "data/**/*.pkl", "data/**/*.parquet",
        "models/**/*.h5", "models/**/*.pkl", "models/**/*.parquet",
        "genomes/**/*.h5", "genomes/**/*.pkl", "genomes/**/*.parquet",
    ]

    def test_exact_list_standard_dirs(self) -> None:
        assert root_gitignore_lines(self.STANDARD_DIRS) == self.EXPECTED_9

    def test_length_standard_dirs(self) -> None:
        assert len(root_gitignore_lines(self.STANDARD_DIRS)) == 9

    def test_three_patterns_per_dir(self) -> None:
        """Each dir contributes exactly 3 patterns."""
        result = root_gitignore_lines(self.STANDARD_DIRS)
        for i, d in enumerate(self.STANDARD_DIRS):
            chunk = result[i * 3 : i * 3 + 3]
            assert chunk == [f"{d}/**/*.h5", f"{d}/**/*.pkl", f"{d}/**/*.parquet"]

    def test_custom_single_dir(self) -> None:
        result = root_gitignore_lines(["proteomes"])
        assert result == [
            "proteomes/**/*.h5", "proteomes/**/*.pkl", "proteomes/**/*.parquet"
        ]

    def test_empty_list(self) -> None:
        assert root_gitignore_lines([]) == []

    def test_order_follows_input(self) -> None:
        """Input order is preserved in output."""
        result = root_gitignore_lines(["genomes", "data"])
        assert result[:3] == ["genomes/**/*.h5", "genomes/**/*.pkl", "genomes/**/*.parquet"]
        assert result[3:] == ["data/**/*.h5", "data/**/*.pkl", "data/**/*.parquet"]

    def test_no_extra_patterns(self) -> None:
        """Only h5, pkl, parquet — nothing else."""
        result = root_gitignore_lines(["data"])
        suffixes = {p.split("/**/*.")[1] for p in result}
        assert suffixes == {"h5", "pkl", "parquet"}


# ---------------------------------------------------------------------------
# AC #6 — read_shared_dirs fallback behaviour
# ---------------------------------------------------------------------------


class TestReadSharedDirsFallback:
    """AC #6: read_shared_dirs returns list(DEFAULT_SHARED_DIRS) when the toml
    file is missing, when [layout] is absent, and when shared_dirs key is absent."""

    def test_missing_toml_file(self, tmp_path: Path) -> None:
        """No kbu-project.toml → return defaults."""
        assert read_shared_dirs(tmp_path) == list(DEFAULT_SHARED_DIRS)

    def test_empty_toml_no_layout_section(self, tmp_path: Path) -> None:
        """Present file with no [layout] table → return defaults."""
        toml = tmp_path / "kbu-project.toml"
        toml.write_text('[project]\nname = "test"\n', encoding="utf-8")
        assert read_shared_dirs(tmp_path) == list(DEFAULT_SHARED_DIRS)

    def test_layout_section_without_shared_dirs_key(self, tmp_path: Path) -> None:
        """[layout] present but no shared_dirs key → return defaults."""
        toml = tmp_path / "kbu-project.toml"
        toml.write_text('[layout]\nunknown_key = "foo"\n', encoding="utf-8")
        assert read_shared_dirs(tmp_path) == list(DEFAULT_SHARED_DIRS)

    def test_returns_list_not_tuple(self, tmp_path: Path) -> None:
        """Return type must be list, not tuple."""
        result = read_shared_dirs(tmp_path)
        assert isinstance(result, list)

    def test_returns_copy(self, tmp_path: Path) -> None:
        """Mutating the returned list must not affect DEFAULT_SHARED_DIRS."""
        result = read_shared_dirs(tmp_path)
        result.append("extra")
        assert "extra" not in DEFAULT_SHARED_DIRS


# ---------------------------------------------------------------------------
# AC #7 — read_shared_dirs returns user list verbatim
# ---------------------------------------------------------------------------


class TestReadSharedDirsUserList:
    """AC #7: read_shared_dirs returns the user list verbatim when
    [layout.shared_dirs] is set."""

    def test_custom_list(self, tmp_path: Path) -> None:
        toml = tmp_path / "kbu-project.toml"
        toml.write_text(
            '[layout]\nshared_dirs = ["data", "proteomes", "references"]\n',
            encoding="utf-8",
        )
        assert read_shared_dirs(tmp_path) == ["data", "proteomes", "references"]

    def test_superset_of_defaults(self, tmp_path: Path) -> None:
        toml = tmp_path / "kbu-project.toml"
        toml.write_text(
            '[layout]\nshared_dirs = ["data", "models", "genomes", "proteomes"]\n',
            encoding="utf-8",
        )
        result = read_shared_dirs(tmp_path)
        assert result == ["data", "models", "genomes", "proteomes"]

    def test_single_entry_list(self, tmp_path: Path) -> None:
        toml = tmp_path / "kbu-project.toml"
        toml.write_text('[layout]\nshared_dirs = ["data"]\n', encoding="utf-8")
        assert read_shared_dirs(tmp_path) == ["data"]

    def test_empty_shared_dirs_list(self, tmp_path: Path) -> None:
        """User can explicitly opt out of shared dirs with []."""
        toml = tmp_path / "kbu-project.toml"
        toml.write_text('[layout]\nshared_dirs = []\n', encoding="utf-8")
        assert read_shared_dirs(tmp_path) == []

    def test_order_preserved(self, tmp_path: Path) -> None:
        toml = tmp_path / "kbu-project.toml"
        toml.write_text(
            '[layout]\nshared_dirs = ["genomes", "models", "data"]\n',
            encoding="utf-8",
        )
        assert read_shared_dirs(tmp_path) == ["genomes", "models", "data"]


# ---------------------------------------------------------------------------
# AC #8 — tomllib usage and unknown-key tolerance
# ---------------------------------------------------------------------------


class TestReadSharedDirsTomllib:
    """AC #8: read_shared_dirs uses tomllib (stdlib) and silently ignores
    unknown keys in [layout]."""

    def test_ignores_unknown_layout_keys(self, tmp_path: Path) -> None:
        """Extra keys in [layout] must not raise any error."""
        toml = tmp_path / "kbu-project.toml"
        toml.write_text(
            '[layout]\nshared_dirs = ["data"]\nfuture_option = true\n',
            encoding="utf-8",
        )
        # Must not raise; unknown key silently ignored.
        result = read_shared_dirs(tmp_path)
        assert result == ["data"]

    def test_malformed_toml_raises_decode_error(self, tmp_path: Path) -> None:
        """Malformed TOML must re-raise tomllib.TOMLDecodeError."""
        toml = tmp_path / "kbu-project.toml"
        toml.write_text("[[not valid toml ]]]]]\n", encoding="utf-8")
        with pytest.raises(tomllib.TOMLDecodeError):
            read_shared_dirs(tmp_path)

    def test_implementation_uses_tomllib(self) -> None:
        """Verify the module imports tomllib (not tomli or another 3rd-party lib)."""
        import kbutillib.layout as layout_mod
        import inspect
        src = inspect.getsource(layout_mod)
        assert "import tomllib" in src

    def test_multiple_unknown_sections_ignored(self, tmp_path: Path) -> None:
        """Unknown top-level sections beyond [layout] don't affect the result."""
        toml = tmp_path / "kbu-project.toml"
        toml.write_text(
            '[project]\nname = "test"\n\n'
            '[layout]\nshared_dirs = ["data", "models"]\nwidget = 42\n\n'
            '[other]\nfoo = "bar"\n',
            encoding="utf-8",
        )
        assert read_shared_dirs(tmp_path) == ["data", "models"]


# ---------------------------------------------------------------------------
# Work-notebook layout descriptor — constants
# ---------------------------------------------------------------------------


class TestWorknbSharedRoots:
    """WORKNB_SHARED_ROOTS is a tuple of exactly ("models", "genomes", "data")."""

    def test_value(self) -> None:
        assert WORKNB_SHARED_ROOTS == ("models", "genomes", "data")

    def test_is_tuple(self) -> None:
        assert isinstance(WORKNB_SHARED_ROOTS, tuple)

    def test_length(self) -> None:
        assert len(WORKNB_SHARED_ROOTS) == 3

    def test_contains_expected_names(self) -> None:
        assert "models" in WORKNB_SHARED_ROOTS
        assert "genomes" in WORKNB_SHARED_ROOTS
        assert "data" in WORKNB_SHARED_ROOTS

    def test_distinct_from_beril_constant(self) -> None:
        """WORKNB_SHARED_ROOTS is a separate object from DEFAULT_SHARED_DIRS."""
        assert WORKNB_SHARED_ROOTS is not DEFAULT_SHARED_DIRS


class TestWorknbPrjSubdirs:
    """WORKNB_PRJ_SUBDIRS is a tuple of exactly ("NBCache", "NBOutput")."""

    def test_value(self) -> None:
        assert WORKNB_PRJ_SUBDIRS == ("NBCache", "NBOutput")

    def test_is_tuple(self) -> None:
        assert isinstance(WORKNB_PRJ_SUBDIRS, tuple)

    def test_length(self) -> None:
        assert len(WORKNB_PRJ_SUBDIRS) == 2

    def test_order(self) -> None:
        assert WORKNB_PRJ_SUBDIRS[0] == "NBCache"
        assert WORKNB_PRJ_SUBDIRS[1] == "NBOutput"


class TestWorknbGitignoreMarkers:
    """Marker constants use the specified delimiter strings."""

    def test_start_marker(self) -> None:
        assert WORKNB_GITIGNORE_MARKER_START == "# >>> kbu work-notebook gitignore >>>"

    def test_end_marker(self) -> None:
        assert WORKNB_GITIGNORE_MARKER_END == "# <<< kbu work-notebook gitignore <<<"

    def test_markers_are_distinct(self) -> None:
        assert WORKNB_GITIGNORE_MARKER_START != WORKNB_GITIGNORE_MARKER_END


# ---------------------------------------------------------------------------
# Work-notebook layout descriptor — worknb_gitignore_lines()
# ---------------------------------------------------------------------------


class TestWorknbGitignoreLines:
    """worknb_gitignore_lines() returns exactly the three specified patterns."""

    EXPECTED = [
        "notebooks/PRJ-*/NBCache/",
        "notebooks/PRJ-*/NBOutput/",
        ".ipynb_checkpoints/",
    ]

    def test_exact_list(self) -> None:
        assert worknb_gitignore_lines() == self.EXPECTED

    def test_length(self) -> None:
        assert len(worknb_gitignore_lines()) == 3

    def test_order(self) -> None:
        result = worknb_gitignore_lines()
        assert result[0] == "notebooks/PRJ-*/NBCache/"
        assert result[1] == "notebooks/PRJ-*/NBOutput/"
        assert result[2] == ".ipynb_checkpoints/"

    def test_returns_new_list_each_call(self) -> None:
        """Mutations to the returned list must not affect subsequent calls."""
        first = worknb_gitignore_lines()
        first.append("extra")
        second = worknb_gitignore_lines()
        assert len(second) == 3


# ---------------------------------------------------------------------------
# Work-notebook gitignore block helper — apply_worknb_gitignore_block()
# ---------------------------------------------------------------------------


class TestApplyWorknbGitignoreBlockAppend:
    """apply_worknb_gitignore_block appends the block to a non-existent file."""

    def test_creates_file_when_absent(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        assert not gi.exists()
        apply_worknb_gitignore_block(gi)
        assert gi.exists()

    def test_block_contains_start_marker(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        apply_worknb_gitignore_block(gi)
        content = gi.read_text(encoding="utf-8")
        assert WORKNB_GITIGNORE_MARKER_START in content

    def test_block_contains_end_marker(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        apply_worknb_gitignore_block(gi)
        content = gi.read_text(encoding="utf-8")
        assert WORKNB_GITIGNORE_MARKER_END in content

    def test_block_contains_all_patterns(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        apply_worknb_gitignore_block(gi)
        content = gi.read_text(encoding="utf-8")
        for pattern in worknb_gitignore_lines():
            assert pattern in content

    def test_appends_to_existing_content(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("*.pyc\n__pycache__/\n", encoding="utf-8")
        apply_worknb_gitignore_block(gi)
        content = gi.read_text(encoding="utf-8")
        assert "*.pyc" in content
        assert WORKNB_GITIGNORE_MARKER_START in content

    def test_existing_content_not_modified(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        pre_existing = "*.pyc\n__pycache__/\n"
        gi.write_text(pre_existing, encoding="utf-8")
        apply_worknb_gitignore_block(gi)
        content = gi.read_text(encoding="utf-8")
        assert content.startswith(pre_existing)

    def test_start_before_end_in_output(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        apply_worknb_gitignore_block(gi)
        content = gi.read_text(encoding="utf-8")
        assert content.index(WORKNB_GITIGNORE_MARKER_START) < content.index(
            WORKNB_GITIGNORE_MARKER_END
        )


class TestApplyWorknbGitignoreBlockIdempotent:
    """Calling apply_worknb_gitignore_block twice produces the same result."""

    def test_idempotent_on_empty_file(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        apply_worknb_gitignore_block(gi)
        after_first = gi.read_text(encoding="utf-8")
        apply_worknb_gitignore_block(gi)
        after_second = gi.read_text(encoding="utf-8")
        assert after_first == after_second

    def test_idempotent_on_file_with_prior_content(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("*.pyc\n", encoding="utf-8")
        apply_worknb_gitignore_block(gi)
        after_first = gi.read_text(encoding="utf-8")
        apply_worknb_gitignore_block(gi)
        after_second = gi.read_text(encoding="utf-8")
        assert after_first == after_second

    def test_single_marker_block_after_two_calls(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        apply_worknb_gitignore_block(gi)
        apply_worknb_gitignore_block(gi)
        content = gi.read_text(encoding="utf-8")
        assert content.count(WORKNB_GITIGNORE_MARKER_START) == 1
        assert content.count(WORKNB_GITIGNORE_MARKER_END) == 1

    def test_single_marker_block_after_three_calls(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        for _ in range(3):
            apply_worknb_gitignore_block(gi)
        content = gi.read_text(encoding="utf-8")
        assert content.count(WORKNB_GITIGNORE_MARKER_START) == 1

    def test_prior_content_preserved_after_two_calls(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("*.pyc\n__pycache__/\n", encoding="utf-8")
        apply_worknb_gitignore_block(gi)
        apply_worknb_gitignore_block(gi)
        content = gi.read_text(encoding="utf-8")
        assert "*.pyc" in content
        assert "__pycache__/" in content


class TestApplyWorknbGitignoreBlockReplace:
    """apply_worknb_gitignore_block replaces a stale block with canonical content."""

    def test_replaces_stale_body_line(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        stale = (
            WORKNB_GITIGNORE_MARKER_START + "\n"
            "notebooks/PRJ-*/OldCache/\n"
            + WORKNB_GITIGNORE_MARKER_END + "\n"
        )
        gi.write_text(stale, encoding="utf-8")
        apply_worknb_gitignore_block(gi)
        content = gi.read_text(encoding="utf-8")
        assert "OldCache" not in content
        assert "notebooks/PRJ-*/NBCache/" in content

    def test_content_before_block_preserved_on_replace(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        before = "*.pyc\n"
        stale_block = (
            WORKNB_GITIGNORE_MARKER_START + "\n"
            "old_line/\n"
            + WORKNB_GITIGNORE_MARKER_END + "\n"
        )
        gi.write_text(before + stale_block, encoding="utf-8")
        apply_worknb_gitignore_block(gi)
        content = gi.read_text(encoding="utf-8")
        assert "*.pyc" in content
        assert "old_line" not in content

    def test_content_after_block_preserved_on_replace(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        stale_block = (
            WORKNB_GITIGNORE_MARKER_START + "\n"
            "old_line/\n"
            + WORKNB_GITIGNORE_MARKER_END + "\n"
        )
        after = "node_modules/\n"
        gi.write_text(stale_block + after, encoding="utf-8")
        apply_worknb_gitignore_block(gi)
        content = gi.read_text(encoding="utf-8")
        assert "node_modules/" in content
        assert "old_line" not in content


# ---------------------------------------------------------------------------
# BERIL functions unmodified — regression guard
# ---------------------------------------------------------------------------


class TestBerilFunctionsUnchanged:
    """Smoke-test that BERIL layout functions still return their original values."""

    def test_subproject_subdirs_non_adopted(self) -> None:
        assert subproject_subdirs(adopted=False) == [
            "notebooks", "figures", "nboutput", ".cache", "literature", "sessions"
        ]

    def test_subproject_subdirs_adopted(self) -> None:
        result = subproject_subdirs(adopted=True)
        assert result[-1] == "archive"
        assert len(result) == 7

    def test_subproject_gitignore_lines(self) -> None:
        assert subproject_gitignore_lines() == [
            ".cache/", "nboutput/", ".adoption-notes.md"
        ]

    def test_default_shared_dirs_unchanged(self) -> None:
        assert DEFAULT_SHARED_DIRS == ("data", "models", "genomes")

    def test_root_gitignore_lines_unchanged(self) -> None:
        result = root_gitignore_lines(["data"])
        assert result == ["data/**/*.h5", "data/**/*.pkl", "data/**/*.parquet"]
