"""Tests for kbutillib.cli.migrate — ``kbu migrate`` command.

Covers Acceptance Criteria #39, #40, and #41 from the kbutillib-v2 PRD.

AC #39: ``kbu migrate`` prompts per subproject and per non-canonical item;
        no operations execute without user confirmation.
AC #40: ``kbu migrate`` adds ``[layout.shared_dirs]`` to ``kbu-project.toml``
        when absent; leaves it unchanged when present.
AC #41: ``kbu migrate`` creates root shared dirs with ``.gitkeep`` when
        missing.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from kbutillib.cli import main
from kbutillib.cli.manifest import now_utc_iso, write_project_manifest, write_subproject_manifest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(tmp_path: Path, name: str = "myproj", with_layout: bool = False) -> Path:
    """Create a minimal ``kbu-project.toml`` in *tmp_path* and return the root."""
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    data: dict = {
        "project": {"name": name, "title": name, "created_at": now_utc_iso()},
        "kbutillib": {"source_path": "/fake", "source_commit": "abc"},
        "update": {"last_pulled_at": now_utc_iso(), "last_pulled_commit": "abc"},
    }
    if with_layout:
        data["layout"] = {"shared_dirs": ["data", "models", "genomes"]}
    write_project_manifest(root, data)
    return root


def _create_subproject(root: Path, sp_name: str, status: str = "migrate") -> Path:
    """Scaffold a subproject directory and write its manifest."""
    now = now_utc_iso()
    sp_dir = root / "subprojects" / sp_name
    sp_dir.mkdir(parents=True, exist_ok=True)
    (sp_dir / "notebooks").mkdir(exist_ok=True)
    (sp_dir / "sessions").mkdir(exist_ok=True)
    write_subproject_manifest(root, sp_name, {
        "subproject": {
            "name": sp_name,
            "title": sp_name,
            "status": status,
            "created_at": now,
            "last_session_at": now,
        },
        "artifacts": {
            "research_plan": False,
            "report": False,
            "reviews": {"plan": [], "build": [], "synthesis": []},
        },
        "notebooks": [],
        "session_refs": [],
    })
    return sp_dir


def _invoke_migrate(root: Path, stdin: str = "") -> object:
    """Invoke ``kbu migrate`` from *root* with the given stdin string."""
    runner = CliRunner()
    saved = os.getcwd()
    try:
        os.chdir(root)
        return runner.invoke(
            main,
            ["migrate"],
            input=stdin,
            catch_exceptions=False,
        )
    finally:
        os.chdir(saved)


# ---------------------------------------------------------------------------
# AC #39 — prompts per subproject; no silent operations
# ---------------------------------------------------------------------------


class TestAC39Prompts:
    """kbu migrate prompts the user before acting on subproject content."""

    def test_no_project_root_exits_1(self, tmp_path: Path) -> None:
        """Exit code 1 and error message when cwd has no kbu-project.toml."""
        empty = tmp_path / "empty"
        empty.mkdir()
        result = _invoke_migrate(empty)
        assert result.exit_code == 1
        assert "kbu-bootstrapped" in result.output or "kbu-project.toml" in result.output

    def test_data_dir_prompts_before_move(self, tmp_path: Path) -> None:
        """Prompt text is shown when a data/ directory exists in a subproject."""
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "alpha")
        # Create a data/ dir so the prompt fires
        (sp_dir / "data").mkdir()
        (sp_dir / "data" / "file.csv").write_text("a,b\n", encoding="utf-8")

        # Choose option 4 (skip) — no move should occur
        result = _invoke_migrate(root, stdin="4\n")
        assert result.exit_code == 0, result.output
        # data/ should still be intact
        assert (sp_dir / "data" / "file.csv").exists()
        assert "Options" in result.output

    def test_skip_leaves_data_intact(self, tmp_path: Path) -> None:
        """Choosing 'skip' (option 4) leaves data/ contents unchanged."""
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "beta")
        (sp_dir / "data").mkdir()
        (sp_dir / "data" / "model.pkl").write_bytes(b"\x00\x01")

        result = _invoke_migrate(root, stdin="4\n")
        assert result.exit_code == 0, result.output
        assert (sp_dir / "data" / "model.pkl").exists()

    def test_references_prompts_when_present(self, tmp_path: Path) -> None:
        """Prompt for references.md is shown when the file exists."""
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "gamma")
        (sp_dir / "references.md").write_text("# refs\n", encoding="utf-8")

        # Choose '2' (keep as-is)
        result = _invoke_migrate(root, stdin="2\n")
        assert result.exit_code == 0, result.output
        assert (sp_dir / "references.md").exists()
        assert "references.md" in result.output

    def test_user_data_dir_prompts(self, tmp_path: Path) -> None:
        """Prompt for user_data/ appears when that directory is present."""
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "delta")
        (sp_dir / "user_data").mkdir()
        (sp_dir / "user_data" / "notes.txt").write_text("hi\n", encoding="utf-8")

        # Skip both prompts
        result = _invoke_migrate(root, stdin="4\n")
        assert result.exit_code == 0, result.output
        assert (sp_dir / "user_data" / "notes.txt").exists()
        assert "user_data" in result.output

    def test_no_prompts_when_no_extra_dirs(self, tmp_path: Path) -> None:
        """No data-relocation prompt when subproject has no data/ or user_data/."""
        root = _make_project(tmp_path)
        _create_subproject(root, "epsilon")

        result = _invoke_migrate(root, stdin="")
        assert result.exit_code == 0, result.output
        # "Options" only appears for data-relocation or references prompts
        assert "Options" not in result.output


# ---------------------------------------------------------------------------
# AC #40 — [layout.shared_dirs] added when absent, preserved when present
# ---------------------------------------------------------------------------


class TestAC40LayoutSharedDirs:
    """kbu migrate manages [layout.shared_dirs] in kbu-project.toml."""

    def test_adds_shared_dirs_when_absent(self, tmp_path: Path) -> None:
        """Adds [layout] shared_dirs to kbu-project.toml when not present."""
        root = _make_project(tmp_path, with_layout=False)
        result = _invoke_migrate(root, stdin="")
        assert result.exit_code == 0, result.output

        with (root / "kbu-project.toml").open("rb") as fh:
            data = tomllib.load(fh)

        assert "layout" in data
        assert data["layout"]["shared_dirs"] == ["data", "models", "genomes"]
        assert "Added [layout]" in result.output

    def test_preserves_shared_dirs_when_present(self, tmp_path: Path) -> None:
        """Does not modify [layout.shared_dirs] when already present."""
        root = _make_project(tmp_path, with_layout=True)
        # Modify the existing list to a custom value to verify it's untouched
        import tomli_w
        with (root / "kbu-project.toml").open("rb") as fh:
            existing = tomllib.load(fh)
        existing["layout"]["shared_dirs"] = ["data", "custom_dir"]
        with (root / "kbu-project.toml").open("wb") as fh:
            tomli_w.dump(existing, fh)

        result = _invoke_migrate(root, stdin="")
        assert result.exit_code == 0, result.output

        with (root / "kbu-project.toml").open("rb") as fh:
            after = tomllib.load(fh)

        assert after["layout"]["shared_dirs"] == ["data", "custom_dir"]
        assert "already present" in result.output

    def test_output_confirms_addition(self, tmp_path: Path) -> None:
        """Output message confirms the shared_dirs addition."""
        root = _make_project(tmp_path, with_layout=False)
        result = _invoke_migrate(root, stdin="")
        assert "Added [layout]" in result.output
        assert "shared_dirs" in result.output


# ---------------------------------------------------------------------------
# AC #41 — root shared dirs created with .gitkeep when missing
# ---------------------------------------------------------------------------


class TestAC41SharedDirCreation:
    """kbu migrate creates missing root shared dirs with .gitkeep."""

    def test_creates_missing_shared_dirs(self, tmp_path: Path) -> None:
        """Creates data/, models/, genomes/ with .gitkeep when absent."""
        root = _make_project(tmp_path)
        result = _invoke_migrate(root, stdin="")
        assert result.exit_code == 0, result.output

        for d in ["data", "models", "genomes"]:
            assert (root / d).is_dir(), f"Expected {d}/ to be created"
            assert (root / d / ".gitkeep").exists(), f"Expected {d}/.gitkeep"

    def test_does_not_overwrite_existing_dirs(self, tmp_path: Path) -> None:
        """Does not touch a shared dir that already exists."""
        root = _make_project(tmp_path)
        (root / "data").mkdir()
        (root / "data" / "existing.h5").write_bytes(b"\x00")

        result = _invoke_migrate(root, stdin="")
        assert result.exit_code == 0, result.output
        # existing.h5 must survive
        assert (root / "data" / "existing.h5").exists()
        # No .gitkeep injected into pre-existing dir
        assert not (root / "data" / ".gitkeep").exists()

    def test_creates_only_missing_dirs(self, tmp_path: Path) -> None:
        """Only missing dirs are created; existing ones are left alone."""
        root = _make_project(tmp_path)
        (root / "models").mkdir()

        result = _invoke_migrate(root, stdin="")
        assert result.exit_code == 0, result.output

        # data/ and genomes/ should be created
        assert (root / "data" / ".gitkeep").exists()
        assert (root / "genomes" / ".gitkeep").exists()
        # models/ already existed — no .gitkeep injected
        assert not (root / "models" / ".gitkeep").exists()
        # Output confirms existing vs created
        assert "models/ already exists" in result.output
        assert "Created data/.gitkeep" in result.output

    def test_created_dirs_reported(self, tmp_path: Path) -> None:
        """Newly created dirs are reported in command output."""
        root = _make_project(tmp_path)
        result = _invoke_migrate(root, stdin="")
        assert result.exit_code == 0, result.output
        for d in ["data", "models", "genomes"]:
            assert f"Created {d}/.gitkeep" in result.output


# ---------------------------------------------------------------------------
# Integration: data relocation option 1 (namespaced move)
# ---------------------------------------------------------------------------


class TestDataRelocationMove:
    """Spot-check interactive data/ move into root data/<name>/."""

    def test_option1_moves_namespaced(self, tmp_path: Path) -> None:
        """Option 1 moves data/ contents into root data/<sp_name>/."""
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "spone")
        (sp_dir / "data").mkdir()
        (sp_dir / "data" / "result.csv").write_text("x\n", encoding="utf-8")

        # Option 1 for data/; no user_data or references prompts needed
        result = _invoke_migrate(root, stdin="1\n")
        assert result.exit_code == 0, result.output

        # File moved to root data/spone/
        assert (root / "data" / "spone" / "result.csv").exists()
        # Original data/ should be gone
        assert not (sp_dir / "data").exists()

    def test_option3_renames_to_nboutput(self, tmp_path: Path) -> None:
        """Option 3 renames data/ to nboutput/ in place."""
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "sptwo")
        (sp_dir / "data").mkdir()
        (sp_dir / "data" / "out.txt").write_text("y\n", encoding="utf-8")

        result = _invoke_migrate(root, stdin="3\n")
        assert result.exit_code == 0, result.output

        assert (sp_dir / "nboutput" / "out.txt").exists()
        assert not (sp_dir / "data").exists()


# ---------------------------------------------------------------------------
# Integration: references.md conversion
# ---------------------------------------------------------------------------


class TestReferencesConversion:
    """Spot-check references.md → literature/index.md conversion."""

    def test_option1_converts_references(self, tmp_path: Path) -> None:
        """Option 1 copies content into literature/index.md and removes references.md."""
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "reftest")
        (sp_dir / "references.md").write_text("# My Refs\n- paper1\n", encoding="utf-8")

        # Choose '1' (convert to literature/index.md)
        result = _invoke_migrate(root, stdin="1\n")
        assert result.exit_code == 0, result.output

        index = sp_dir / "literature" / "index.md"
        assert index.exists()
        assert "My Refs" in index.read_text(encoding="utf-8")
        assert not (sp_dir / "references.md").exists()

    def test_option3_deletes_references(self, tmp_path: Path) -> None:
        """Option 3 deletes references.md."""
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "refdelete")
        (sp_dir / "references.md").write_text("# old\n", encoding="utf-8")

        result = _invoke_migrate(root, stdin="3\n")
        assert result.exit_code == 0, result.output
        assert not (sp_dir / "references.md").exists()


# ---------------------------------------------------------------------------
# Gitignore block appended per subproject
# ---------------------------------------------------------------------------


class TestGitignoreBlock:
    """kbu migrate appends gitignore blocks for each subproject (idempotent)."""

    def test_appends_gitignore_block(self, tmp_path: Path) -> None:
        """A gitignore block is written for each subproject."""
        root = _make_project(tmp_path)
        _create_subproject(root, "sp_gi")
        (root / ".gitignore").write_text("*.pyc\n", encoding="utf-8")

        result = _invoke_migrate(root, stdin="")
        assert result.exit_code == 0, result.output

        gi_text = (root / ".gitignore").read_text(encoding="utf-8")
        assert "# >>> kbu-subproject:sp_gi >>>" in gi_text
        assert ".cache/" in gi_text

    def test_idempotent_on_second_run(self, tmp_path: Path) -> None:
        """Running migrate twice does not duplicate the gitignore block."""
        root = _make_project(tmp_path)
        _create_subproject(root, "sp_idem")

        _invoke_migrate(root, stdin="")
        _invoke_migrate(root, stdin="")

        gi_text = (root / ".gitignore").read_text(encoding="utf-8")
        # The marker appears in both open and close lines; count the open marker only.
        assert gi_text.count("# >>> kbu-subproject:sp_idem >>>") == 1
