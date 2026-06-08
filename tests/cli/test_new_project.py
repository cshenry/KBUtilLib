"""Tests for kbutillib.cli.new_project — ``kbu new-project`` subcommand."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
import tomllib
from click.testing import CliRunner

from kbutillib.cli import main
from kbutillib.cli.new_project import (
    _compute_file_hashes,
    _copy_template_tree,
    _kbutillib_root,
    new_project,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invoke(*args: str, env: dict | None = None) -> Any:
    runner = CliRunner(mix_stderr=False)
    return runner.invoke(main, list(args), catch_exceptions=False, env=env)


def _make_stub_template(root: Path) -> None:
    """Create a minimal stub templates/research-project/ tree in *root*.

    Includes:
    - .claude/commands/kbu-start.md  (with {{project_name}} placeholder)
    - .vscode/extensions.json
    - {{project_name}}.code-workspace  (filename uses placeholder)
    - subprojects/.gitkeep
    """
    tmpl = root / "templates" / "research-project"
    (tmpl / ".claude" / "commands").mkdir(parents=True)
    (tmpl / ".vscode").mkdir(parents=True)
    (tmpl / "subprojects").mkdir(parents=True)

    (tmpl / ".claude" / "commands" / "kbu-start.md").write_text(
        "# kbu-start for {{project_name}}\nProject: {{project_name}}\n",
        encoding="utf-8",
    )
    (tmpl / ".vscode" / "extensions.json").write_text(
        '{"recommendations": ["anthropic.claude-code"]}',
        encoding="utf-8",
    )
    (tmpl / "{{project_name}}.code-workspace").write_text(
        '{"folders": [{"path": "{{project_name}}"}]}',
        encoding="utf-8",
    )
    (tmpl / "subprojects" / ".gitkeep").write_text("", encoding="utf-8")


# ---------------------------------------------------------------------------
# Template substitution
# ---------------------------------------------------------------------------


class TestCopyTemplateTree:
    def test_filename_substitution(self, tmp_path: Path) -> None:
        """{{project_name}} in filenames is replaced."""
        src = tmp_path / "tmpl"
        (src / "subdir").mkdir(parents=True)
        (src / "{{project_name}}.code-workspace").write_text(
            "workspace", encoding="utf-8"
        )

        dest = tmp_path / "dest"
        dest.mkdir()
        _copy_template_tree(src, dest, {"project_name": "myproj"})

        assert (dest / "myproj.code-workspace").exists()
        assert not (dest / "{{project_name}}.code-workspace").exists()

    def test_content_substitution(self, tmp_path: Path) -> None:
        """{{project_name}} in file content is replaced."""
        src = tmp_path / "tmpl"
        src.mkdir()
        (src / "README.md").write_text(
            "Welcome to {{project_name}}!\n", encoding="utf-8"
        )

        dest = tmp_path / "dest"
        dest.mkdir()
        _copy_template_tree(src, dest, {"project_name": "cool_project"})

        content = (dest / "README.md").read_text(encoding="utf-8")
        assert "cool_project" in content
        assert "{{project_name}}" not in content

    def test_directory_name_substitution(self, tmp_path: Path) -> None:
        """{{project_name}} in directory names is replaced."""
        src = tmp_path / "tmpl"
        sub = src / "{{project_name}}_dir"
        sub.mkdir(parents=True)
        (sub / "file.txt").write_text("hello", encoding="utf-8")

        dest = tmp_path / "dest"
        dest.mkdir()
        _copy_template_tree(src, dest, {"project_name": "alpha"})

        assert (dest / "alpha_dir" / "file.txt").exists()


# ---------------------------------------------------------------------------
# new_project() core behaviour (with mocked subprocess)
# ---------------------------------------------------------------------------


class TestNewProjectCore:
    @pytest.fixture()
    def stub_kbu_root(self, tmp_path: Path) -> Path:
        """A fake KBUtilLib root with a stub template tree."""
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        (kbu_root / ".git").mkdir()  # make it look like a git repo
        _make_stub_template(kbu_root)
        return kbu_root

    def _mock_subprocess_run_factory(self, venv_python_path: str):
        """Return a side_effect function that simulates subprocess calls."""
        def _side_effect(cmd, *args, **kwargs):
            # git rev-parse HEAD
            if cmd[0] == "git" and "rev-parse" in cmd:
                r = MagicMock()
                r.returncode = 0
                r.stdout = "abc123deadbeef\n"
                return r
            # venvman — not called when mocked out
            # python -m venv .venv
            if cmd[0] == sys.executable and cmd[1:3] == ["-m", "venv"]:
                venv_dir = Path(kwargs.get("cwd", ".")) / ".venv"
                (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
                (venv_dir / "bin" / "python").write_text("#!/usr/bin/env python3\n")
                (venv_dir / "bin" / "python").chmod(0o755)
                r = MagicMock()
                r.returncode = 0
                return r
            # pip install / ipykernel / git init / git add / git commit / subproject
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r
        return _side_effect

    def test_creates_project_toml(self, tmp_path: Path, stub_kbu_root: Path) -> None:
        """new_project writes kbu-project.toml with required sections."""
        dest = tmp_path / "myproject"

        with (
            patch("kbutillib.cli.new_project._kbutillib_root", return_value=stub_kbu_root),
            patch("kbutillib.cli.new_project._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.new_project._is_darwin", return_value=True),
            patch("shutil.which", return_value=None),  # no venvman
            patch("subprocess.run") as mock_run,
        ):
            # Make python -m venv create .venv/bin/python
            def _side_effect(cmd, *args, **kwargs):
                cwd = kwargs.get("cwd", ".")
                if isinstance(cmd, list) and "-m" in cmd and "venv" in cmd:
                    venv = Path(cwd) / ".venv"
                    (venv / "bin").mkdir(parents=True, exist_ok=True)
                    py = venv / "bin" / "python"
                    py.write_text("#!/usr/bin/env python3")
                    py.chmod(0o755)
                r = MagicMock()
                r.returncode = 0
                r.stdout = "abc123\n"
                r.stderr = ""
                return r

            mock_run.side_effect = _side_effect

            new_project(
                path=dest,
                name="myproject",
                author="Test User",
                affiliation="Test Lab",
                orcid="0000-0001-0002-0003",
            )

        assert dest.exists()
        toml_path = dest / "kbu-project.toml"
        assert toml_path.exists()

        with open(toml_path, "rb") as fh:
            cfg = tomllib.load(fh)

        assert cfg["project"]["name"] == "myproject"
        assert cfg["project"]["authors"][0]["name"] == "Test User"
        assert cfg["project"]["authors"][0]["affiliation"] == "Test Lab"
        assert cfg["project"]["authors"][0]["orcid"] == "0000-0001-0002-0003"
        assert "source_path" in cfg["kbutillib"]
        assert "source_commit" in cfg["kbutillib"]
        assert "last_pulled_at" in cfg["update"]
        assert "last_pulled_commit" in cfg["update"]
        assert "file_hashes" in cfg["update"]

    def test_file_hashes_populated(self, tmp_path: Path, stub_kbu_root: Path) -> None:
        """[update.file_hashes] contains SHA-256 for tracked files."""
        dest = tmp_path / "hashproj"

        with (
            patch("kbutillib.cli.new_project._kbutillib_root", return_value=stub_kbu_root),
            patch("kbutillib.cli.new_project._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.new_project._is_darwin", return_value=True),
            patch("shutil.which", return_value=None),
            patch("subprocess.run") as mock_run,
        ):
            def _side_effect(cmd, *args, **kwargs):
                cwd = kwargs.get("cwd", ".")
                if isinstance(cmd, list) and "-m" in cmd and "venv" in cmd:
                    venv = Path(cwd) / ".venv"
                    (venv / "bin").mkdir(parents=True, exist_ok=True)
                    py = venv / "bin" / "python"
                    py.write_text("#!/usr/bin/env python3")
                    py.chmod(0o755)
                r = MagicMock()
                r.returncode = 0
                r.stdout = "abc123\n"
                r.stderr = ""
                return r

            mock_run.side_effect = _side_effect

            new_project(
                path=dest,
                name="hashproj",
                author="A",
                affiliation="B",
                orcid="0000",
            )

        with open(dest / "kbu-project.toml", "rb") as fh:
            cfg = tomllib.load(fh)

        hashes = cfg["update"]["file_hashes"]
        # The stub template has .claude/commands/kbu-start.md and .vscode/extensions.json
        assert any("kbu-start.md" in k for k in hashes), f"hashes: {hashes}"
        assert any("extensions.json" in k for k in hashes), f"hashes: {hashes}"
        for v in hashes.values():
            assert v.startswith("sha256:"), f"hash should be prefixed: {v}"

    def test_rejects_existing_path(self, tmp_path: Path, stub_kbu_root: Path) -> None:
        """new_project exits 1 if destination already exists."""
        existing = tmp_path / "existing"
        existing.mkdir()

        with (
            patch("kbutillib.cli.new_project._kbutillib_root", return_value=stub_kbu_root),
            pytest.raises(SystemExit) as exc,
        ):
            new_project(
                path=existing,
                name="existing",
                author="A",
                affiliation="B",
                orcid="0",
            )
        assert exc.value.code == 1

    def test_non_darwin_exits_without_override(
        self, tmp_path: Path, stub_kbu_root: Path
    ) -> None:
        """On non-macOS without override, exits 1 before creating venv."""
        dest = tmp_path / "linuxproj"

        with (
            patch("kbutillib.cli.new_project._kbutillib_root", return_value=stub_kbu_root),
            patch("kbutillib.cli.new_project._is_macos_or_override", return_value=False),
            patch("subprocess.run") as mock_run,
            pytest.raises(SystemExit) as exc,
        ):
            new_project(
                path=dest,
                name="linuxproj",
                author="A",
                affiliation="B",
                orcid="0",
            )

        assert exc.value.code == 1
        # No venv should have been attempted
        for c in mock_run.call_args_list:
            cmd = c.args[0] if c.args else c.kwargs.get("args", [])
            if isinstance(cmd, list):
                assert "venv" not in " ".join(str(x) for x in cmd), \
                    f"venv should not be called on non-Darwin: {cmd}"

    def test_non_darwin_with_override_proceeds(
        self, tmp_path: Path, stub_kbu_root: Path
    ) -> None:
        """On non-macOS WITH override, proceeds with python -m venv."""
        dest = tmp_path / "overrideproj"

        with (
            patch("kbutillib.cli.new_project._kbutillib_root", return_value=stub_kbu_root),
            patch("kbutillib.cli.new_project._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.new_project._is_darwin", return_value=False),
            patch("shutil.which", return_value=None),  # no venvman on non-Darwin
            patch("subprocess.run") as mock_run,
        ):
            def _side_effect(cmd, *args, **kwargs):
                cwd = kwargs.get("cwd", ".")
                if isinstance(cmd, list) and "-m" in cmd and "venv" in cmd:
                    venv = Path(cwd) / ".venv"
                    (venv / "bin").mkdir(parents=True, exist_ok=True)
                    py = venv / "bin" / "python"
                    py.write_text("#!/usr/bin/env python3")
                    py.chmod(0o755)
                r = MagicMock()
                r.returncode = 0
                r.stdout = "deadbeef\n"
                r.stderr = ""
                return r

            mock_run.side_effect = _side_effect

            new_project(
                path=dest,
                name="overrideproj",
                author="A",
                affiliation="B",
                orcid="0",
            )

        assert (dest / "kbu-project.toml").exists()

    def test_first_subproject_invoked(self, tmp_path: Path, stub_kbu_root: Path) -> None:
        """When first_subproject is set, subprocess is called to create it."""
        dest = tmp_path / "subprojtest"
        calls_recorded = []

        with (
            patch("kbutillib.cli.new_project._kbutillib_root", return_value=stub_kbu_root),
            patch("kbutillib.cli.new_project._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.new_project._is_darwin", return_value=True),
            patch("shutil.which", return_value=None),
            patch("subprocess.run") as mock_run,
        ):
            def _side_effect(cmd, *args, **kwargs):
                calls_recorded.append(cmd)
                cwd = kwargs.get("cwd", ".")
                if isinstance(cmd, list) and "-m" in cmd and "venv" in cmd:
                    venv = Path(cwd) / ".venv"
                    (venv / "bin").mkdir(parents=True, exist_ok=True)
                    py = venv / "bin" / "python"
                    py.write_text("#!/usr/bin/env python3")
                    py.chmod(0o755)
                r = MagicMock()
                r.returncode = 0
                r.stdout = "deadbeef\n"
                r.stderr = ""
                return r

            mock_run.side_effect = _side_effect

            new_project(
                path=dest,
                name="subprojtest",
                author="A",
                affiliation="B",
                orcid="0",
                first_subproject="my_analysis",
            )

        # Check that the subproject create command was invoked
        subproject_calls = [
            c for c in calls_recorded
            if isinstance(c, list) and "subproject" in c and "create" in c and "my_analysis" in c
        ]
        assert len(subproject_calls) == 1, f"Expected subproject create call; got: {calls_recorded}"

    def test_template_filename_substituted_in_project(
        self, tmp_path: Path, stub_kbu_root: Path
    ) -> None:
        """{{project_name}}.code-workspace is renamed to <name>.code-workspace."""
        dest = tmp_path / "wstest"

        with (
            patch("kbutillib.cli.new_project._kbutillib_root", return_value=stub_kbu_root),
            patch("kbutillib.cli.new_project._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.new_project._is_darwin", return_value=True),
            patch("shutil.which", return_value=None),
            patch("subprocess.run") as mock_run,
        ):
            def _side_effect(cmd, *args, **kwargs):
                cwd = kwargs.get("cwd", ".")
                if isinstance(cmd, list) and "-m" in cmd and "venv" in cmd:
                    venv = Path(cwd) / ".venv"
                    (venv / "bin").mkdir(parents=True, exist_ok=True)
                    py = venv / "bin" / "python"
                    py.write_text("#!/usr/bin/env python3")
                    py.chmod(0o755)
                r = MagicMock()
                r.returncode = 0
                r.stdout = "abc123\n"
                r.stderr = ""
                return r

            mock_run.side_effect = _side_effect

            new_project(
                path=dest,
                name="wstest",
                author="A",
                affiliation="B",
                orcid="0",
            )

        # Workspace file should be named after the project
        assert (dest / "wstest.code-workspace").exists()
        assert not any(dest.glob("{{*}}.code-workspace"))

        # Content should also have substitution
        ws_content = (dest / "wstest.code-workspace").read_text(encoding="utf-8")
        assert "{{project_name}}" not in ws_content

    def test_template_content_substituted(
        self, tmp_path: Path, stub_kbu_root: Path
    ) -> None:
        """{{project_name}} in .claude/commands/kbu-start.md is replaced."""
        dest = tmp_path / "contenttest"

        with (
            patch("kbutillib.cli.new_project._kbutillib_root", return_value=stub_kbu_root),
            patch("kbutillib.cli.new_project._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.new_project._is_darwin", return_value=True),
            patch("shutil.which", return_value=None),
            patch("subprocess.run") as mock_run,
        ):
            def _side_effect(cmd, *args, **kwargs):
                cwd = kwargs.get("cwd", ".")
                if isinstance(cmd, list) and "-m" in cmd and "venv" in cmd:
                    venv = Path(cwd) / ".venv"
                    (venv / "bin").mkdir(parents=True, exist_ok=True)
                    py = venv / "bin" / "python"
                    py.write_text("#!/usr/bin/env python3")
                    py.chmod(0o755)
                r = MagicMock()
                r.returncode = 0
                r.stdout = "abc123\n"
                r.stderr = ""
                return r

            mock_run.side_effect = _side_effect

            new_project(
                path=dest,
                name="contenttest",
                author="A",
                affiliation="B",
                orcid="0",
            )

        kbu_start = dest / ".claude" / "commands" / "kbu-start.md"
        assert kbu_start.exists()
        content = kbu_start.read_text(encoding="utf-8")
        assert "contenttest" in content
        assert "{{project_name}}" not in content


# ---------------------------------------------------------------------------
# CLI integration (via CliRunner)
# ---------------------------------------------------------------------------


class TestNewProjectCLI:
    def test_command_registered(self) -> None:
        """``kbu new-project --help`` exits 0."""
        runner = CliRunner()
        result = runner.invoke(main, ["new-project", "--help"])
        assert result.exit_code == 0
        assert "new-project" in result.output or "PATH" in result.output

    def test_rejects_existing_path_via_cli(self, tmp_path: Path) -> None:
        """CLI exits 1 when destination already exists."""
        existing = tmp_path / "existing"
        existing.mkdir()

        runner = CliRunner()
        with patch("kbutillib.cli.new_project._kbutillib_root", return_value=tmp_path):
            result = runner.invoke(
                main,
                [
                    "new-project",
                    str(existing),
                    "--name", "existing",
                    "--author", "A",
                    "--affiliation", "B",
                    "--orcid", "0",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# _compute_file_hashes
# ---------------------------------------------------------------------------


class TestComputeFileHashes:
    def test_hashes_tracked_dirs_only(self, tmp_path: Path) -> None:
        """Only .claude/commands/ and .vscode/ files are hashed."""
        (tmp_path / ".claude" / "commands").mkdir(parents=True)
        (tmp_path / ".vscode").mkdir(parents=True)
        (tmp_path / "other").mkdir(parents=True)

        (tmp_path / ".claude" / "commands" / "kbu-start.md").write_text("content", encoding="utf-8")
        (tmp_path / ".vscode" / "extensions.json").write_text("{}", encoding="utf-8")
        (tmp_path / "other" / "ignored.txt").write_text("ignored", encoding="utf-8")

        hashes = _compute_file_hashes(tmp_path, [".claude/commands", ".vscode"])

        assert ".claude/commands/kbu-start.md" in hashes
        assert ".vscode/extensions.json" in hashes
        assert "other/ignored.txt" not in hashes
        for v in hashes.values():
            assert v.startswith("sha256:")

    def test_empty_dirs_yield_empty_hashes(self, tmp_path: Path) -> None:
        hashes = _compute_file_hashes(tmp_path, [".claude/commands", ".vscode"])
        assert hashes == {}
