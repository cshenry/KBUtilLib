"""Tests for ``kbu subproject adopt`` command and state machine additions.

Covers AC #9–#31 from ``agent-io/prds/kbutillib-v2/fullprompt.md``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from kbutillib.cli import main
from kbutillib.cli.manifest import (
    now_utc_iso,
    read_subproject_manifest,
    write_project_manifest,
    write_subproject_manifest,
)
from kbutillib.cli.subproject import (
    _FORWARD,
    _NEXT_ACTION,
    _REVERSE,
    _STATES,
    _check_forward_preconditions,
    _scaffold_subproject,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_project(tmp_path: Path, name: str = "myproj") -> Path:
    """Create a minimal kbu-project.toml and return the project root."""
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    write_project_manifest(root, {
        "project": {"name": name, "title": name, "created_at": now_utc_iso()},
        "kbutillib": {"source_path": "/fake", "source_commit": "abc"},
        "update": {"last_pulled_at": now_utc_iso(), "last_pulled_commit": "abc"},
    })
    return root


def _make_git_repo(path: Path) -> None:
    """Init a git repo at *path* with an initial commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    # Create an initial commit so the repo has a valid HEAD
    (path / ".gitkeep").write_text("")
    subprocess.run(
        ["git", "-C", str(path), "add", ".gitkeep"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"],
        check=True, capture_output=True,
    )


def _make_source_dir(parent: Path, name: str = "source_notebooks") -> Path:
    """Create a source directory with some content."""
    src = parent / name
    src.mkdir(parents=True, exist_ok=True)
    (src / "analysis.txt").write_text("some analysis")
    return src


def _invoke(root: Path, *args: str) -> Any:
    """Run a ``kbu subproject`` command from the project root."""
    runner = CliRunner()
    saved = os.getcwd()
    try:
        os.chdir(root)
        return runner.invoke(main, ["subproject", *args], catch_exceptions=False)
    finally:
        os.chdir(saved)


# ── AC #9: _STATES includes 'migrate' after 'plan' ──────────────────────────


class TestStateMachineUpdate:
    def test_migrate_in_states(self) -> None:
        """AC #9: 'migrate' is in _STATES immediately after 'plan'."""
        assert "migrate" in _STATES
        plan_idx = _STATES.index("plan")
        migrate_idx = _STATES.index("migrate")
        assert migrate_idx == plan_idx + 1

    def test_remaining_order_unchanged(self) -> None:
        """AC #9: existing states retain their order."""
        expected_tail = [
            "p-review", "build", "b-review",
            "run", "synthesize", "s-review", "complete",
        ]
        migrate_idx = _STATES.index("migrate")
        assert _STATES[migrate_idx + 1:] == expected_tail

    def test_forward_migrate_to_p_review(self) -> None:
        """AC #10: _FORWARD['migrate'] == 'p-review'."""
        assert _FORWARD["migrate"] == "p-review"

    def test_no_reverse_for_migrate(self) -> None:
        """AC #10: no _REVERSE entry for 'migrate'."""
        assert "migrate" not in _REVERSE

    def test_next_action_migrate(self) -> None:
        """AC #11: _NEXT_ACTION['migrate'] == 'Migrate'."""
        assert _NEXT_ACTION["migrate"] == "Migrate"

    def test_migrate_precondition_with_research_plan(self, tmp_path: Path) -> None:
        """AC #12: migrate → None when RESEARCH_PLAN.md exists."""
        sp_dir = tmp_path / "sp"
        sp_dir.mkdir()
        (sp_dir / "RESEARCH_PLAN.md").write_text("# Plan\n")
        result = _check_forward_preconditions(sp_dir, {}, "migrate")
        assert result is None

    def test_migrate_precondition_without_research_plan(self, tmp_path: Path) -> None:
        """AC #12: migrate → 'missing-artifact' when RESEARCH_PLAN.md absent."""
        sp_dir = tmp_path / "sp"
        sp_dir.mkdir()
        result = _check_forward_preconditions(sp_dir, {}, "migrate")
        assert result == "missing-artifact"


# ── AC #13: _scaffold_subproject directory list ──────────────────────────────


class TestScaffoldSubproject:
    def test_non_adopted_dirs(self, tmp_path: Path) -> None:
        """AC #13: adopted=False creates exactly the 6-dir canonical set."""
        sp_dir = tmp_path / "sp"
        _scaffold_subproject(sp_dir, "sp", "SP", adopted=False)
        expected = {"notebooks", "figures", "nboutput", ".cache", "literature", "sessions"}
        created = {d.name for d in sp_dir.iterdir() if d.is_dir()}
        assert expected == created

    def test_adopted_dirs_include_archive(self, tmp_path: Path) -> None:
        """AC #13: adopted=True creates 7 dirs including 'archive'."""
        sp_dir = tmp_path / "sp"
        _scaffold_subproject(sp_dir, "sp", "SP", adopted=True)
        assert (sp_dir / "archive").is_dir()
        expected = {"notebooks", "figures", "nboutput", ".cache", "literature", "sessions", "archive"}
        created = {d.name for d in sp_dir.iterdir() if d.is_dir()}
        assert expected == created

    def test_util_py_rendered_from_template(self, tmp_path: Path) -> None:
        """AC #14: util.py is rendered from the Jinja template, not an inline stub."""
        sp_dir = tmp_path / "sp"
        _scaffold_subproject(sp_dir, "myproject", "My Project", adopted=False)
        util_py = sp_dir / "notebooks" / "util.py"
        assert util_py.exists()
        content = util_py.read_text()
        # Template uses {{ project_name }} — should be substituted
        assert "myproject" in content
        assert "{{ project_name }}" not in content
        # Template has NotebookSession import
        assert "NotebookSession" in content

    def test_no_references_md(self, tmp_path: Path) -> None:
        """AC #15: references.md is NOT created (retired in v2)."""
        sp_dir = tmp_path / "sp"
        _scaffold_subproject(sp_dir, "sp", "SP", adopted=False)
        assert not (sp_dir / "references.md").exists()


# ── AC #16: create still writes status='plan' ─────────────────────────────


class TestCreateStillWorks:
    def test_create_writes_plan_status(self, tmp_path: Path) -> None:
        """AC #16: kbu subproject create writes status='plan'."""
        root = _make_project(tmp_path)
        result = _invoke(root, "create", "alpha")
        assert result.exit_code == 0, result.output
        data = read_subproject_manifest(root, "alpha")
        assert data["subproject"]["status"] == "plan"


# ── adopt pre-flight checks ─────────────────────────────────────────────────


class TestAdoptPreFlight:
    def test_refusal_not_in_project(self, tmp_path: Path) -> None:
        """AC #17: adopt exits 1 when cwd has no kbu-project.toml."""
        src = tmp_path / "src"
        src.mkdir()
        # No kbu-project.toml anywhere in tmp_path
        runner = CliRunner()
        saved = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(
                main, ["subproject", "adopt", str(src), "--name", "sp1"],
                catch_exceptions=False,
            )
        finally:
            os.chdir(saved)
        assert result.exit_code != 0
        assert "kbu-project.toml" in result.output.lower() or "bootstrapped" in result.output.lower()

    def test_refusal_path_not_exist(self, tmp_path: Path) -> None:
        """AC #18: adopt exits 1 when path does not exist."""
        root = _make_project(tmp_path)
        result = _invoke(root, "adopt", "/nonexistent/path/xyz", "--name", "sp1")
        assert result.exit_code != 0

    def test_refusal_path_not_directory(self, tmp_path: Path) -> None:
        """AC #18: adopt exits 1 when path is a file, not a directory."""
        root = _make_project(tmp_path)
        f = root / "somefile.txt"
        f.write_text("hello")
        result = _invoke(root, "adopt", str(f), "--name", "sp1")
        assert result.exit_code != 0

    def test_refusal_subproject_already_exists(self, tmp_path: Path) -> None:
        """AC #19: adopt exits 1 when subprojects/<name>/ already exists."""
        root = _make_project(tmp_path)
        # Pre-create the subproject
        (root / "subprojects" / "sp1").mkdir(parents=True)
        write_subproject_manifest(root, "sp1", {
            "subproject": {"name": "sp1", "title": "sp1", "status": "plan",
                           "created_at": now_utc_iso(), "last_session_at": now_utc_iso()},
            "artifacts": {"research_plan": False, "report": False,
                          "reviews": {"plan": [], "build": [], "synthesis": []}},
            "notebooks": [], "session_refs": [],
        })
        src = _make_source_dir(tmp_path)
        result = _invoke(root, "adopt", str(src), "--name", "sp1")
        assert result.exit_code != 0

    def test_refusal_path_inside_destination(self, tmp_path: Path) -> None:
        """AC #20: adopt exits 1 when path is inside the destination."""
        root = _make_project(tmp_path)
        # Create the source inside what would be the destination
        dest_parent = root / "subprojects" / "sp1"
        dest_parent.mkdir(parents=True)
        src = dest_parent / "some_notebooks"
        src.mkdir()
        result = _invoke(root, "adopt", str(src), "--name", "sp1")
        assert result.exit_code != 0

    def test_refusal_cross_repo(self, tmp_path: Path) -> None:
        """AC #21: adopt exits 1 when source is in a different git repo."""
        # Set up project in its own git repo
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        _make_git_repo(project_dir)
        root = project_dir
        write_project_manifest(root, {
            "project": {"name": "proj", "title": "proj", "created_at": now_utc_iso()},
            "kbutillib": {"source_path": "/fake", "source_commit": "abc"},
            "update": {"last_pulled_at": now_utc_iso(), "last_pulled_commit": "abc"},
        })

        # Set up source in a DIFFERENT git repo
        other_repo = tmp_path / "other_repo"
        _make_git_repo(other_repo)
        src = other_repo / "notebooks"
        src.mkdir()
        (src / "nb.txt").write_text("content")

        result = _invoke(root, "adopt", str(src), "--name", "sp1")
        assert result.exit_code != 0
        assert "different git repo" in result.output.lower() or "different" in result.output.lower()

    def test_allow_same_git_repo(self, tmp_path: Path) -> None:
        """AC #22: adopt succeeds when source is in the same git repo."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        _make_git_repo(project_dir)
        root = project_dir
        write_project_manifest(root, {
            "project": {"name": "proj", "title": "proj", "created_at": now_utc_iso()},
            "kbutillib": {"source_path": "/fake", "source_commit": "abc"},
            "update": {"last_pulled_at": now_utc_iso(), "last_pulled_commit": "abc"},
        })

        src = project_dir / "notebooks" / "fitness_loe"
        src.mkdir(parents=True)
        (src / "01_explore.txt").write_text("analysis")

        result = _invoke(root, "adopt", str(src), "--name", "fitness_loe")
        assert result.exit_code == 0, result.output

    def test_allow_not_in_any_git_repo(self, tmp_path: Path) -> None:
        """AC #23: adopt succeeds (with warning) when source is not in any git repo.

        Uses a path outside any git repo by running from a non-git tmp_path.
        """
        root = _make_project(tmp_path)
        # Create source directory that is NOT inside any git repo.
        # We need to be confident it's not inside the git-tracked KBUtilLib
        # repo or any other.  Use a subdirectory of tmp_path that
        # _find_project_root will resolve to our fake root.
        # Since the test project itself is not a git repo (no git init),
        # the source path (inside tmp_path) won't be in any git repo either.
        src = tmp_path / "loose_notebooks"
        src.mkdir()
        (src / "analysis.txt").write_text("content")

        result = _invoke(root, "adopt", str(src), "--name", "imported")
        assert result.exit_code == 0, result.output

    def test_warn_zero_ipynb(self, tmp_path: Path) -> None:
        """AC #24: adopt warns but proceeds when source has zero .ipynb files."""
        root = _make_project(tmp_path)
        src = _make_source_dir(tmp_path)
        # No .ipynb in src
        result = _invoke(root, "adopt", str(src), "--name", "sp1")
        assert result.exit_code == 0, result.output
        # Should warn
        assert "warning" in result.output.lower() or "Warning" in result.output


# ── adopt success criteria ──────────────────────────────────────────────────


class TestAdoptSuccess:
    def _do_adopt(
        self,
        tmp_path: Path,
        src_name: str = "my_notebooks",
        sp_name: str = "my_sp",
    ) -> tuple[Path, Path]:
        """Run adopt and return (project_root, subproject_dir)."""
        root = _make_project(tmp_path)
        src = tmp_path / src_name
        src.mkdir(parents=True)
        (src / "file1.txt").write_text("content")
        sub = src / "subdir"
        sub.mkdir()
        (sub / "file2.txt").write_text("more content")

        result = _invoke(root, "adopt", str(src), "--name", sp_name)
        assert result.exit_code == 0, result.output
        return root, root / "subprojects" / sp_name

    def test_archive_contains_moved_content(self, tmp_path: Path) -> None:
        """AC #25: archive/ contains moved content; source no longer exists."""
        root, sp_dir = self._do_adopt(tmp_path)
        archive = sp_dir / "archive"
        assert archive.is_dir()
        assert (archive / "file1.txt").exists()
        assert (archive / "subdir" / "file2.txt").exists()
        # Source no longer exists
        assert not (tmp_path / "my_notebooks").exists()

    def test_canonical_subdirs_created(self, tmp_path: Path) -> None:
        """AC #26: canonical subdirs created after adopt."""
        root, sp_dir = self._do_adopt(tmp_path)
        for d in ("notebooks", "figures", "nboutput", ".cache", "literature", "sessions"):
            assert (sp_dir / d).is_dir(), f"Expected {d} to exist"

    def test_util_py_exists(self, tmp_path: Path) -> None:
        """AC #26: notebooks/util.py rendered from template."""
        root, sp_dir = self._do_adopt(tmp_path)
        util_py = sp_dir / "notebooks" / "util.py"
        assert util_py.exists()
        assert "NotebookSession" in util_py.read_text()

    def test_manifest_status_is_migrate(self, tmp_path: Path) -> None:
        """AC #27: kbu-subproject.toml has status='migrate'."""
        root, sp_dir = self._do_adopt(tmp_path)
        data = read_subproject_manifest(root, "my_sp")
        assert data["subproject"]["status"] == "migrate"

    def test_manifest_notebooks_empty(self, tmp_path: Path) -> None:
        """AC #30: manifest notebooks: [] is empty at adopt time."""
        root, sp_dir = self._do_adopt(tmp_path)
        data = read_subproject_manifest(root, "my_sp")
        assert data["notebooks"] == []

    def test_adoption_notes_written(self, tmp_path: Path) -> None:
        """AC #28: .adoption-notes.md exists with required sections."""
        root, sp_dir = self._do_adopt(tmp_path)
        notes = sp_dir / ".adoption-notes.md"
        assert notes.exists()
        content = notes.read_text()
        assert "Notebooks found" in content
        assert "Subdirectories found" in content
        assert "Oversize files" in content

    def test_gitignore_appended(self, tmp_path: Path) -> None:
        """AC #29: subproject gitignore lines appended to root .gitignore."""
        root, sp_dir = self._do_adopt(tmp_path)
        gi = root / ".gitignore"
        assert gi.exists()
        content = gi.read_text()
        assert ".cache/" in content
        assert "nboutput/" in content
        assert ".adoption-notes.md" in content

    def test_gitignore_idempotent(self, tmp_path: Path) -> None:
        """AC #29: re-running adopt (different name) doesn't duplicate entries for first subproject."""
        root = _make_project(tmp_path)

        # First adopt
        src1 = tmp_path / "src1"
        src1.mkdir()
        (src1 / "a.txt").write_text("a")
        result = _invoke(root, "adopt", str(src1), "--name", "sp1")
        assert result.exit_code == 0, result.output

        gi = root / ".gitignore"
        content_after_first = gi.read_text()

        # Second adopt with SAME name is refused (sp1 already exists)
        # So test idempotency by running a different name and verifying
        # the sp1 marker is not duplicated
        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "b.txt").write_text("b")
        result2 = _invoke(root, "adopt", str(src2), "--name", "sp2")
        assert result2.exit_code == 0, result2.output

        content_after_second = gi.read_text()
        # sp1 marker should appear exactly once
        assert content_after_second.count("kbu-subproject:sp1") == 2  # open + close

    def test_no_oversize_gitignore_updates(self, tmp_path: Path) -> None:
        """AC #31: adopt does NOT write gitignore entries for oversize files."""
        root = _make_project(tmp_path)
        src = tmp_path / "src_with_big_file"
        src.mkdir()
        # Write a file > 10MB
        big_file = src / "big.h5"
        big_file.write_bytes(b"x" * 10_000_001)

        result = _invoke(root, "adopt", str(src), "--name", "sp_big")
        assert result.exit_code == 0, result.output

        gi = root / ".gitignore"
        if gi.exists():
            content = gi.read_text()
            # The big file name should NOT appear in gitignore
            assert "big.h5" not in content

    def test_manifest_toml_written(self, tmp_path: Path) -> None:
        """After adopt, kbu-subproject.toml file exists."""
        root, sp_dir = self._do_adopt(tmp_path)
        assert (sp_dir / "kbu-subproject.toml").exists()


# ── advance from migrate state ──────────────────────────────────────────────


class TestAdvanceMigrate:
    def _create_subproject_with_status(
        self, root: Path, sp_name: str, status: str
    ) -> Path:
        now = now_utc_iso()
        sp_dir = root / "subprojects" / sp_name
        sp_dir.mkdir(parents=True, exist_ok=True)
        (sp_dir / "notebooks").mkdir(exist_ok=True)
        write_subproject_manifest(root, sp_name, {
            "subproject": {
                "name": sp_name, "title": sp_name, "status": status,
                "created_at": now, "last_session_at": now,
            },
            "artifacts": {
                "research_plan": False, "report": False,
                "reviews": {"plan": [], "build": [], "synthesis": []},
            },
            "notebooks": [], "session_refs": [],
        })
        return sp_dir

    def test_advance_migrate_with_research_plan(self, tmp_path: Path) -> None:
        """migrate → p-review succeeds with RESEARCH_PLAN.md present."""
        root = _make_project(tmp_path)
        sp_dir = self._create_subproject_with_status(root, "sp", "migrate")
        (sp_dir / "RESEARCH_PLAN.md").write_text("# Plan\n")

        runner = CliRunner()
        saved = os.getcwd()
        try:
            os.chdir(root)
            result = runner.invoke(main, ["subproject", "advance", "sp"],
                                   catch_exceptions=False)
        finally:
            os.chdir(saved)

        assert result.exit_code == 0, result.output
        data = read_subproject_manifest(root, "sp")
        assert data["subproject"]["status"] == "p-review"

    def test_advance_migrate_without_research_plan(self, tmp_path: Path) -> None:
        """migrate → p-review fails without RESEARCH_PLAN.md."""
        root = _make_project(tmp_path)
        sp_dir = self._create_subproject_with_status(root, "sp", "migrate")
        # No RESEARCH_PLAN.md

        runner = CliRunner()
        saved = os.getcwd()
        try:
            os.chdir(root)
            result = runner.invoke(main, ["subproject", "advance", "sp"],
                                   catch_exceptions=False)
        finally:
            os.chdir(saved)

        assert result.exit_code != 0
        assert "missing-artifact" in result.output

    def test_set_status_to_migrate(self, tmp_path: Path) -> None:
        """set-status can set migrate as a valid state."""
        root = _make_project(tmp_path)
        sp_dir = self._create_subproject_with_status(root, "sp", "plan")

        runner = CliRunner()
        saved = os.getcwd()
        try:
            os.chdir(root)
            result = runner.invoke(main, ["subproject", "set-status", "sp", "migrate"],
                                   catch_exceptions=False)
        finally:
            os.chdir(saved)

        assert result.exit_code == 0, result.output
        data = read_subproject_manifest(root, "sp")
        assert data["subproject"]["status"] == "migrate"

    def test_adopt_help_listed(self, tmp_path: Path) -> None:
        """'adopt' subcommand is listed in subproject --help."""
        runner = CliRunner()
        result = runner.invoke(main, ["subproject", "--help"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "adopt" in result.output
