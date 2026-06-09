"""Tests for kbutillib.cli.bootstrap — ``kbu bootstrap`` subcommand.

Covers all 38 Acceptance Criteria from the kbu-bootstrap-v1 PRD.
"""

from __future__ import annotations

import hashlib
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
from kbutillib.cli.bootstrap import (
    _BOOTSTRAP_MACOS_ONLY_MESSAGE,
    _CLAUDE_AGENT_FILES,
    _CLAUDE_COMMAND_FILES,
    _GITIGNORE_BLOCK,
    _GITIGNORE_MARKER_OPEN,
    _check_gitignore_action,
    _handle_gitignore,
    _kbutillib_root,
    _probe_venv,
    _python_version,
    bootstrap,
    bootstrap_command,
)
from kbutillib.cli.manifest import now_utc_iso, sha256_file, write_project_manifest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha256_str(s: str) -> str:
    return _sha256(s.encode("utf-8"))


def _make_git_repo(path: Path) -> None:
    """Create a minimal .git directory at *path* (simulates a git repo)."""
    (path / ".git").mkdir(parents=True, exist_ok=True)


def _make_stub_template(kbu_root: Path, project_name: str = "PROJECT") -> None:
    """Create a minimal stub templates/research-project/ in *kbu_root*."""
    from kbutillib.cli.bootstrap import _CLAUDE_AGENT_FILES
    tmpl = kbu_root / "templates" / "research-project"
    (tmpl / ".claude" / "commands").mkdir(parents=True, exist_ok=True)
    (tmpl / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
    (tmpl / ".vscode").mkdir(parents=True, exist_ok=True)
    (tmpl / "subprojects").mkdir(parents=True, exist_ok=True)
    # Shared dirs with .gitkeep
    for shared_dir in ["data", "models", "genomes"]:
        (tmpl / shared_dir).mkdir(parents=True, exist_ok=True)
        (tmpl / shared_dir / ".gitkeep").write_text("", encoding="utf-8")

    for cmd_rel in _CLAUDE_COMMAND_FILES:
        fname = Path(cmd_rel).name
        (tmpl / cmd_rel).write_text(
            f"# {fname} for {{{{project_name}}}}\n",
            encoding="utf-8",
        )
    for agent_rel in _CLAUDE_AGENT_FILES:
        fname = Path(agent_rel).name
        (tmpl / agent_rel).write_text(
            f"---\nname: {fname.replace('.md', '')}\ntype: agent\n---\n# {fname}\n",
            encoding="utf-8",
        )
    (tmpl / ".vscode" / "extensions.json").write_text(
        '{"recommendations": ["anthropic.claude-code"]}',
        encoding="utf-8",
    )
    (tmpl / "subprojects" / ".gitkeep").write_text("", encoding="utf-8")
    (tmpl / "{{project_name}}.code-workspace").write_text(
        '{"folders": [{"path": "{{project_name}}"}]}',
        encoding="utf-8",
    )
    (tmpl / "README.md").write_text(
        "# {{project_name}}\n\nA scientific research project built on KBUtilLib.\n",
        encoding="utf-8",
    )
    # .gitignore is not copied wholesale; bootstrap uses _GITIGNORE_BLOCK.


def _make_venv_python(path: Path) -> Path:
    """Create a fake venv python at *path* and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env python3\n")
    path.chmod(0o755)
    return path


def _subprocess_ok(**kwargs: Any) -> MagicMock:
    r = MagicMock()
    r.returncode = 0
    r.stdout = ""
    r.stderr = ""
    return r


def _make_subprocess_side_effect(
    python_version: str = "3.11",
    git_commit: str = "abc123",
    fail_pip: bool = False,
    fail_venv: bool = False,
    fail_kernel: bool = False,
) -> Any:
    """Build a side_effect for subprocess.run mocks."""
    def _se(cmd, *args, **kwargs):
        if not isinstance(cmd, list):
            return _subprocess_ok()
        cmd_str = " ".join(str(c) for c in cmd)
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""

        if "git" in cmd and "rev-parse" in cmd:
            r.stdout = git_commit + "\n"
        elif "git" in cmd and "config" in cmd and "user.name" in cmd:
            r.stdout = "Test User\n"
        elif "-m" in cmd and "venv" in cmd:
            if fail_venv:
                r.returncode = 1
                r.stderr = "venv failed"
            else:
                cwd = kwargs.get("cwd", ".")
                venv_dir = Path(str(cwd)) / ".venv"
                _make_venv_python(venv_dir / "bin" / "python")
        elif "-m" in cmd and "pip" in cmd:
            if fail_pip:
                r.returncode = 1
                r.stderr = "pip failed"
                raise subprocess.CalledProcessError(1, cmd)
        elif "-m" in cmd and "ipykernel" in cmd:
            if fail_kernel:
                r.returncode = 1
                raise subprocess.CalledProcessError(1, cmd)
        elif "-c" in cmd and "sys.version_info" in cmd_str:
            r.stdout = python_version + "\n"
        return r
    return _se


def _invoke_bootstrap(
    tmp_path: Path,
    kbu_root: Path,
    args: list[str],
    env: dict | None = None,
    subprocess_side_effect: Any = None,
    input: str | None = None,
) -> Any:
    """Invoke ``kbu bootstrap`` via CliRunner in *tmp_path* as cwd."""
    if subprocess_side_effect is None:
        subprocess_side_effect = _make_subprocess_side_effect()

    runner = CliRunner()
    with (
        patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
        patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
        patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
        patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=subprocess_side_effect),
        patch("kbutillib.cli.bootstrap.shutil.which", return_value=None),
    ):
        os.chdir(tmp_path)
        result = runner.invoke(
            main,
            ["bootstrap"] + args,
            catch_exceptions=False,
            env=env,
            input=input,
        )
    return result


# ---------------------------------------------------------------------------
# AC 1: bootstrap_command exported and registered
# ---------------------------------------------------------------------------


class TestAC1Registration:
    def test_bootstrap_command_exported(self) -> None:
        """bootstrap_command is importable from kbutillib.cli.bootstrap."""
        assert bootstrap_command is not None

    def test_bootstrap_in_main_help(self) -> None:
        """kbu --help lists 'bootstrap' as a subcommand."""
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "bootstrap" in result.output

    def test_bootstrap_command_help_exits_0(self) -> None:
        """kbu bootstrap --help exits 0."""
        runner = CliRunner()
        result = runner.invoke(main, ["bootstrap", "--help"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# AC 2: Precondition — not a git repo
# ---------------------------------------------------------------------------


class TestAC2NotGitRepo:
    def test_exits_1_without_git_dir(self, tmp_path: Path) -> None:
        """Exit 1 with 'must run inside a git repository' when .git absent."""
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
        ):
            os.chdir(tmp_path)
            result = runner.invoke(
                main,
                ["bootstrap", "--no-venv"],
                catch_exceptions=False,
            )
        assert result.exit_code == 1
        assert "must run inside a git repository" in (result.output + (result.stderr or ""))

    def test_no_filesystem_writes_when_no_git(self, tmp_path: Path) -> None:
        """No files are written when precondition fails."""
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
        ):
            os.chdir(tmp_path)
            runner.invoke(main, ["bootstrap", "--no-venv"], catch_exceptions=False)
        assert not (tmp_path / "kbu-project.toml").exists()
        assert not (tmp_path / ".claude").exists()

    def test_git_file_worktree_pointer_passes(self, tmp_path: Path) -> None:
        """A .git file (worktree pointer) satisfies the git repo precondition."""
        (tmp_path / ".git").write_text("gitdir: /some/other/path\n")
        (tmp_path / "kbu-project.toml").write_text("")  # trigger the SECOND precondition

        runner = CliRunner()
        with patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True):
            os.chdir(tmp_path)
            result = runner.invoke(main, ["bootstrap", "--no-venv"], catch_exceptions=False)
        # Should fail on kbu-project.toml, NOT on "must run inside a git repo"
        assert "kbu-project.toml" in (result.output + (result.stderr or ""))
        assert "must run inside a git repository" not in (result.output + (result.stderr or ""))


# ---------------------------------------------------------------------------
# AC 3: Precondition — kbu-project.toml already exists
# ---------------------------------------------------------------------------


class TestAC3ManifestExists:
    def test_exits_1_with_existing_manifest(self, tmp_path: Path) -> None:
        """Exit 1 naming kbu-project.toml when it already exists."""
        _make_git_repo(tmp_path)
        (tmp_path / "kbu-project.toml").write_text("[project]\nname = 'x'\n")

        runner = CliRunner()
        with patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True):
            os.chdir(tmp_path)
            result = runner.invoke(main, ["bootstrap", "--no-venv"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "kbu-project.toml" in (result.output + (result.stderr or ""))

    def test_no_writes_when_manifest_exists(self, tmp_path: Path) -> None:
        """Zero filesystem writes when kbu-project.toml is present."""
        _make_git_repo(tmp_path)
        orig = "[project]\nname = 'x'\n"
        (tmp_path / "kbu-project.toml").write_text(orig)

        runner = CliRunner()
        with patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True):
            os.chdir(tmp_path)
            runner.invoke(main, ["bootstrap", "--no-venv"], catch_exceptions=False)
        # Manifest unchanged
        assert (tmp_path / "kbu-project.toml").read_text() == orig
        assert not (tmp_path / ".claude").exists()


# ---------------------------------------------------------------------------
# AC 4: macOS gate
# ---------------------------------------------------------------------------


class TestAC4MacOSGate:
    def test_exits_1_non_darwin_no_override(self, tmp_path: Path) -> None:
        """On non-macOS without override, exits 1 with exact message."""
        _make_git_repo(tmp_path)
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=False),
        ):
            os.chdir(tmp_path)
            result = runner.invoke(main, ["bootstrap"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "v1 currently targets macOS" in result.output
        assert "--no-venv" in result.output

    def test_exact_macos_message(self, tmp_path: Path) -> None:
        """The exact macOS-only message is printed verbatim."""
        _make_git_repo(tmp_path)
        runner = CliRunner()
        with patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=False):
            os.chdir(tmp_path)
            result = runner.invoke(main, ["bootstrap"], catch_exceptions=False)
        assert _BOOTSTRAP_MACOS_ONLY_MESSAGE in result.output

    def test_platform_override_proceeds(self, tmp_path: Path) -> None:
        """KBU_PLATFORM_OVERRIDE=force bypasses the macOS gate."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        runner = CliRunner()
        se = _make_subprocess_side_effect()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=False),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            result = runner.invoke(
                main,
                ["bootstrap", "--no-venv", "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert (tmp_path / "kbu-project.toml").exists()


# ---------------------------------------------------------------------------
# AC 5: Exact flag set
# ---------------------------------------------------------------------------


class TestAC5FlagSet:
    def test_help_has_expected_flags(self) -> None:
        """--help shows exactly the required flags."""
        runner = CliRunner()
        result = runner.invoke(main, ["bootstrap", "--help"])
        for flag in [
            "--name",
            "--first-subproject",
            "--author",
            "--affiliation",
            "--orcid",
            "--no-venv",
            "--no-kernel",
            "--force-overwrite",
            "--force-venv",
            "--check",
        ]:
            assert flag in result.output, f"Missing flag: {flag}"

    def test_no_standalone_force_flag(self) -> None:
        """The string '--force' (without suffix) does not appear as a flag."""
        import re
        runner = CliRunner()
        result = runner.invoke(main, ["bootstrap", "--help"])
        # Find all --flag tokens; none should be exactly '--force'
        flags = re.findall(r"--[\w-]+", result.output)
        assert "--force" not in flags, f"Standalone --force found in flags: {flags}"


# ---------------------------------------------------------------------------
# AC 6: --name defaults to cwd name
# ---------------------------------------------------------------------------


class TestAC6NameDefault:
    def test_name_defaults_to_cwd_name(self, tmp_path: Path) -> None:
        """When --name is not given, the project name is the cwd directory name."""
        repo = tmp_path / "my_project_dir"
        repo.mkdir()
        _make_git_repo(repo)

        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        runner = CliRunner()
        se = _make_subprocess_side_effect()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(repo)
            result = runner.invoke(
                main,
                ["bootstrap", "--no-venv", "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        with open(repo / "kbu-project.toml", "rb") as f:
            cfg = tomllib.load(f)
        assert cfg["project"]["name"] == "my_project_dir"


# ---------------------------------------------------------------------------
# AC 7: Author triple — git default, prompting
# ---------------------------------------------------------------------------


class TestAC7AuthorTriple:
    def test_author_from_git_config(self, tmp_path: Path) -> None:
        """--author defaults from git config user.name when not given."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        def _se(cmd, *args, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if isinstance(cmd, list) and "config" in cmd and "user.name" in cmd:
                r.stdout = "Git Name\n"
            elif isinstance(cmd, list) and "rev-parse" in cmd:
                r.stdout = "abc123\n"
            return r

        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=_se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            result = runner.invoke(
                main,
                ["bootstrap", "--no-venv", "--affiliation", "Lab", "--orcid", "0"],
                input="",   # no prompt for author expected
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        with open(tmp_path / "kbu-project.toml", "rb") as f:
            cfg = tomllib.load(f)
        assert cfg["project"]["authors"][0]["name"] == "Git Name"

    def test_author_fields_in_manifest(self, tmp_path: Path) -> None:
        """All three author fields are written to [[project.authors]]."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            result = runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "Alice", "--affiliation", "MIT", "--orcid", "0000-0001"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        with open(tmp_path / "kbu-project.toml", "rb") as f:
            cfg = tomllib.load(f)
        auth = cfg["project"]["authors"][0]
        assert auth["name"] == "Alice"
        assert auth["affiliation"] == "MIT"
        assert auth["orcid"] == "0000-0001"


# ---------------------------------------------------------------------------
# AC 8: --check never prompts, no filesystem writes
# ---------------------------------------------------------------------------


class TestAC8Check:
    def test_check_no_filesystem_writes(self, tmp_path: Path) -> None:
        """--check makes zero filesystem changes."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        before = set(tmp_path.rglob("*"))

        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=_make_subprocess_side_effect()),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            result = runner.invoke(
                main,
                ["bootstrap", "--check", "--name", "myproj",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        after = set(tmp_path.rglob("*"))
        # Only the KBUtilLib tree and .git were there; nothing new added to tmp_path root
        new_files = after - before
        # Filter out KBUtilLib subtree files (we created those)
        new_project_files = [p for p in new_files if kbu_root not in p.parents and p != kbu_root]
        # .git dir is the only pre-existing path at root
        assert len(new_project_files) == 0, f"Unexpected new files: {new_project_files}"

    def test_check_no_subprocess_write_calls(self, tmp_path: Path) -> None:
        """--check makes no pip install or ipykernel calls."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        calls_seen = []
        def _se(cmd, *args, **kwargs):
            calls_seen.append(list(cmd) if isinstance(cmd, list) else cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = "abc123\n"
            r.stderr = ""
            return r

        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=_se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            runner.invoke(
                main,
                ["bootstrap", "--check", "--name", "p",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )

        # No pip install or ipykernel install should be called under --check
        write_calls = [
            c for c in calls_seen
            if isinstance(c, list) and ("pip" in c or "ipykernel" in c)
        ]
        assert write_calls == [], f"Unexpected write subprocess calls: {write_calls}"

    def test_check_todo_for_missing_author_fields(self, tmp_path: Path) -> None:
        """Under --check, missing author fields render as TODO strings."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        def _se(cmd, *args, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""  # no git config user.name
            r.stderr = ""
            if isinstance(cmd, list) and "rev-parse" in cmd:
                r.stdout = "abc123\n"
            return r

        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=_se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            result = runner.invoke(
                main,
                ["bootstrap", "--check", "--name", "p"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert "TODO" in result.output
        # No kbu-project.toml should exist
        assert not (tmp_path / "kbu-project.toml").exists()


# ---------------------------------------------------------------------------
# AC 9: Closed template entry set
# ---------------------------------------------------------------------------


class TestAC9TemplateSet:
    def test_bootstrap_handles_expected_entries(self, tmp_path: Path) -> None:
        """Bootstrap handles 16 template entries: 7 commands + 4 agents + 5 other."""
        from kbutillib.cli.bootstrap import _TEMPLATE_ENTRIES
        assert len(_TEMPLATE_ENTRIES) == 16
        assert "README.md" in _TEMPLATE_ENTRIES

    def test_claude_commands_count(self) -> None:
        """There are exactly 7 .claude/commands/ files (3 moved to agents, kbu-migrate added)."""
        assert len(_CLAUDE_COMMAND_FILES) == 7

    def test_claude_agents_count(self) -> None:
        """There are exactly 4 .claude/agents/ subagent files."""
        from kbutillib.cli.bootstrap import _CLAUDE_AGENT_FILES
        assert len(_CLAUDE_AGENT_FILES) == 4

    def test_expected_command_files(self) -> None:
        """Command files include new kbu-migrate.md and exclude the 3 moved to agents."""
        expected = {
            ".claude/commands/kbu-start.md",
            ".claude/commands/kbu-plan.md",
            ".claude/commands/kbu-build.md",
            ".claude/commands/kbu-run.md",
            ".claude/commands/kbu-synthesize.md",
            ".claude/commands/kbu-update.md",
            ".claude/commands/kbu-migrate.md",
        }
        assert set(_CLAUDE_COMMAND_FILES) == expected

    def test_expected_agent_files(self) -> None:
        """Agent files are the 3 converted subagents plus the net-new kbu-sub-build."""
        from kbutillib.cli.bootstrap import _CLAUDE_AGENT_FILES
        expected = {
            ".claude/agents/kbu-sub-literature-review.md",
            ".claude/agents/kbu-sub-review.md",
            ".claude/agents/kbu-sub-diagnose.md",
            ".claude/agents/kbu-sub-build.md",
        }
        assert set(_CLAUDE_AGENT_FILES) == expected

    def test_old_command_files_absent(self) -> None:
        """kbu-literature-review, kbu-review, kbu-diagnose are not in commands list."""
        old_files = {
            ".claude/commands/kbu-literature-review.md",
            ".claude/commands/kbu-review.md",
            ".claude/commands/kbu-diagnose.md",
        }
        assert not old_files.intersection(set(_CLAUDE_COMMAND_FILES))


# ---------------------------------------------------------------------------
# AC 10: Per-file conflict matrix — .claude/commands/kbu-*.md
# ---------------------------------------------------------------------------


class TestAC10CommandFileConflict:
    """Table-driven tests for the 9 .claude/commands files × {absent, identical, different}."""

    @pytest.fixture()
    def setup(self, tmp_path: Path):
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)
        return tmp_path, kbu_root

    @pytest.mark.parametrize("rel_path", _CLAUDE_COMMAND_FILES)
    def test_absent_file_is_copied(self, rel_path: str, setup: Any) -> None:
        """Absent .claude/commands file → copied."""
        tmp_path, kbu_root = setup
        _make_git_repo(tmp_path)

        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            result = runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert (tmp_path / rel_path).exists()

    @pytest.mark.parametrize("rel_path", _CLAUDE_COMMAND_FILES[:3])  # sample 3 for speed
    def test_identical_file_silently_skipped(self, rel_path: str, setup: Any) -> None:
        """Identical .claude/commands file → silent skip; NOT in file_hashes."""
        tmp_path, kbu_root = setup
        # Pre-populate with the exact same content as the template
        tmpl_file = kbu_root / "templates" / "research-project" / rel_path
        dest = tmp_path / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Copy with substitution applied (project name = cwd.name = tmp_path.name)
        src_text = tmpl_file.read_text(encoding="utf-8").replace("{{project_name}}", tmp_path.name)
        dest.write_text(src_text, encoding="utf-8")

        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            result = runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        with open(tmp_path / "kbu-project.toml", "rb") as f:
            cfg = tomllib.load(f)
        # Identical file should NOT be in file_hashes
        assert rel_path not in cfg["update"]["file_hashes"]

    @pytest.mark.parametrize("rel_path", _CLAUDE_COMMAND_FILES[:3])
    def test_different_file_prompts_and_overwrites(self, rel_path: str, setup: Any) -> None:
        """Different .claude/commands file → prompt (default y) → overwrite + .bak created."""
        tmp_path, kbu_root = setup
        dest = tmp_path / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        original_content = "# locally modified content\n"
        dest.write_text(original_content, encoding="utf-8")

        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            result = runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                input="y\n" * 9,  # answer y to all prompts
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        # A .bak file should exist
        bak_files = list(dest.parent.glob(f"{dest.name}.bak.*"))
        assert len(bak_files) >= 1, f"Expected .bak file for {rel_path}"
        # The .bak file should contain the original content
        assert bak_files[0].read_text(encoding="utf-8") == original_content

    @pytest.mark.parametrize("rel_path", _CLAUDE_COMMAND_FILES[:3])
    def test_different_file_force_overwrite_skips_prompt(self, rel_path: str, setup: Any) -> None:
        """--force-overwrite overwrites without prompting; .bak still created."""
        tmp_path, kbu_root = setup
        dest = tmp_path / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("# locally modified\n", encoding="utf-8")

        calls_seen = []
        def _se(cmd, *args, **kwargs):
            calls_seen.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = "abc123\n"
            r.stderr = ""
            return r

        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=_se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            result = runner.invoke(
                main,
                ["bootstrap", "--no-venv", "--force-overwrite",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                input="",  # no prompts expected
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        bak_files = list(dest.parent.glob(f"{dest.name}.bak.*"))
        assert len(bak_files) >= 1


# ---------------------------------------------------------------------------
# AC 11: .vscode/extensions.json — never overwritten
# ---------------------------------------------------------------------------


class TestAC11VSCodeExtensions:
    def test_absent_extensions_json_copied(self, tmp_path: Path) -> None:
        """Absent .vscode/extensions.json → copied."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            result = runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert (tmp_path / ".vscode" / "extensions.json").exists()

    def test_present_extensions_json_never_overwritten(self, tmp_path: Path) -> None:
        """Present .vscode/extensions.json → never overwritten; advice printed."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        vscode_dir = tmp_path / ".vscode"
        vscode_dir.mkdir()
        original = '{"recommendations": ["my.extension"]}'
        (vscode_dir / "extensions.json").write_text(original)

        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            result = runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        # File unchanged
        assert (vscode_dir / "extensions.json").read_text() == original
        # Advice message present
        assert "anthropic.claude-code" in result.output

    def test_present_extensions_json_not_in_file_hashes(self, tmp_path: Path) -> None:
        """Skipped .vscode/extensions.json NOT in [update.file_hashes]."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        vscode_dir = tmp_path / ".vscode"
        vscode_dir.mkdir()
        (vscode_dir / "extensions.json").write_text('{"recommendations": []}')

        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        with open(tmp_path / "kbu-project.toml", "rb") as f:
            cfg = tomllib.load(f)
        assert ".vscode/extensions.json" not in cfg["update"]["file_hashes"]


# ---------------------------------------------------------------------------
# AC 12: subprojects/.gitkeep
# ---------------------------------------------------------------------------


class TestAC12SubprojectsGitkeep:
    def test_absent_subprojects_creates_gitkeep(self, tmp_path: Path) -> None:
        """Absent subprojects/ → create dir + .gitkeep."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        assert (tmp_path / "subprojects" / ".gitkeep").exists()

    def test_existing_subprojects_with_content_untouched(self, tmp_path: Path) -> None:
        """subprojects/ with existing content → left alone; no .gitkeep write."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        sp_dir = tmp_path / "subprojects"
        sp_dir.mkdir()
        (sp_dir / "my_analysis").mkdir()
        (sp_dir / "my_analysis" / "notebook.ipynb").write_text("{}")

        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        # .gitkeep should NOT have been created
        assert not (sp_dir / ".gitkeep").exists()
        # existing content intact
        assert (sp_dir / "my_analysis" / "notebook.ipynb").exists()


# ---------------------------------------------------------------------------
# AC 13: *.code-workspace handling
# ---------------------------------------------------------------------------


class TestAC13CodeWorkspace:
    def test_no_existing_workspace_copies_template(self, tmp_path: Path) -> None:
        """No *.code-workspace at root → copy template with substitution."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            runner.invoke(
                main,
                ["bootstrap", "--no-venv", "--name", "myrepo",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        ws = tmp_path / "myrepo.code-workspace"
        assert ws.exists()
        content = ws.read_text(encoding="utf-8")
        assert "{{project_name}}" not in content

    def test_existing_workspace_skips_generation(self, tmp_path: Path) -> None:
        """Existing *.code-workspace → skip; no new workspace created."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)
        (tmp_path / "Foo.code-workspace").write_text("{}")

        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            runner.invoke(
                main,
                ["bootstrap", "--no-venv", "--name", "myrepo",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        # Should still be only Foo.code-workspace (no myrepo.code-workspace)
        workspaces = list(tmp_path.glob("*.code-workspace"))
        assert len(workspaces) == 1
        assert workspaces[0].name == "Foo.code-workspace"


# ---------------------------------------------------------------------------
# README.md handling: skip-if-existing, copy-with-substitution otherwise
# ---------------------------------------------------------------------------


class TestReadmeHandling:
    def _bootstrap(self, tmp_path: Path, project_name: str = "myproj") -> Any:
        """Run bootstrap with a stub kbu_root + venv + git fakes; return CliRunner result."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)
        _make_venv_python(tmp_path / ".venv" / "bin" / "python")

        env_backup = os.environ.pop("VIRTUAL_ENV", None)
        try:
            runner = CliRunner()
            with (
                patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
                patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
                patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
                patch(
                    "kbutillib.cli.bootstrap.subprocess.run",
                    side_effect=_make_subprocess_side_effect(),
                ),
                patch("kbutillib.cli.bootstrap.shutil.which", return_value=None),
            ):
                os.chdir(tmp_path)
                return runner.invoke(
                    main,
                    [
                        "bootstrap", "--name", project_name,
                        "--author", "A", "--affiliation", "B", "--orcid", "0",
                    ],
                    catch_exceptions=False,
                )
        finally:
            if env_backup is not None:
                os.environ["VIRTUAL_ENV"] = env_backup

    def test_readme_absent_copies_with_substitution(self, tmp_path: Path) -> None:
        """No README.md at root → copy template with {{project_name}} substituted."""
        result = self._bootstrap(tmp_path, project_name="myproj")
        assert result.exit_code == 0
        readme = tmp_path / "README.md"
        assert readme.exists()
        content = readme.read_text(encoding="utf-8")
        assert "# myproj" in content
        assert "{{project_name}}" not in content

    def test_readme_present_preserved(self, tmp_path: Path) -> None:
        """README.md already at root → leave untouched."""
        (tmp_path / "README.md").write_text(
            "# my existing repo\n\nDo not clobber.\n", encoding="utf-8"
        )
        result = self._bootstrap(tmp_path, project_name="myproj")
        assert result.exit_code == 0
        content = (tmp_path / "README.md").read_text(encoding="utf-8")
        assert content == "# my existing repo\n\nDo not clobber.\n"

    def test_readme_recorded_in_file_hashes_when_written(self, tmp_path: Path) -> None:
        """README.md copied by bootstrap → recorded in [update.file_hashes]."""
        from kbutillib.cli.manifest import read_project_manifest
        result = self._bootstrap(tmp_path, project_name="myproj")
        assert result.exit_code == 0
        cfg = read_project_manifest(tmp_path)
        assert "README.md" in cfg["update"]["file_hashes"]

    def test_readme_not_recorded_when_user_owned(self, tmp_path: Path) -> None:
        """README.md present from user → NOT added to [update.file_hashes]."""
        from kbutillib.cli.manifest import read_project_manifest
        (tmp_path / "README.md").write_text("# mine\n", encoding="utf-8")
        result = self._bootstrap(tmp_path, project_name="myproj")
        assert result.exit_code == 0
        cfg = read_project_manifest(tmp_path)
        assert "README.md" not in cfg["update"]["file_hashes"]

    def test_check_mode_shows_readme_plan(self, tmp_path: Path) -> None:
        """--check dry-run mentions README.md in the file plan."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch(
                "kbutillib.cli.bootstrap.subprocess.run",
                side_effect=_make_subprocess_side_effect(),
            ),
            patch("kbutillib.cli.bootstrap.shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            result = runner.invoke(
                main,
                [
                    "bootstrap", "--check", "--name", "myproj",
                    "--author", "A", "--affiliation", "B", "--orcid", "0",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert "README.md" in result.output


# ---------------------------------------------------------------------------
# AC 14 + 15: .gitignore append semantics and marker block contents
# ---------------------------------------------------------------------------


class TestAC14And15Gitignore:
    def test_absent_gitignore_creates_with_marker(self, tmp_path: Path) -> None:
        """Absent .gitignore → create UTF-8 with marker block + trailing newline."""
        result = _handle_gitignore(tmp_path, check=False)
        assert result == "created"
        gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert _GITIGNORE_MARKER_OPEN in gi
        assert gi.endswith("\n")

    def test_gitignore_marker_block_exact_contents(self, tmp_path: Path) -> None:
        """The created .gitignore contains exactly the 7-entry marker block."""
        _handle_gitignore(tmp_path, check=False)
        gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        for entry in [".venv/", "venv/", ".ipynb_checkpoints/", "nboutput/",
                      ".kbcache/", "__pycache__/", "*.egg-info/"]:
            assert entry in gi
        assert "# >>> kbu-managed >>>" in gi
        assert "# <<< kbu-managed <<<" in gi

    def test_present_with_marker_skips(self, tmp_path: Path) -> None:
        """Present .gitignore with marker → skip (idempotent)."""
        (tmp_path / ".gitignore").write_text("*.pyc\n" + _GITIGNORE_BLOCK, encoding="utf-8")
        result = _handle_gitignore(tmp_path, check=False)
        assert result == "skipped"

    def test_present_without_marker_appends(self, tmp_path: Path) -> None:
        """Present .gitignore without marker → append block."""
        (tmp_path / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
        result = _handle_gitignore(tmp_path, check=False)
        assert result == "appended"
        gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert "*.pyc" in gi
        assert _GITIGNORE_MARKER_OPEN in gi

    def test_append_blank_line_spacing_single_newline(self, tmp_path: Path) -> None:
        """File ending with single newline → one extra blank line before marker."""
        (tmp_path / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
        _handle_gitignore(tmp_path, check=False)
        gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert "*.pyc\n\n" in gi

    def test_append_blank_line_spacing_double_newline(self, tmp_path: Path) -> None:
        """File ending with \\n\\n → append marker directly."""
        (tmp_path / ".gitignore").write_text("*.pyc\n\n", encoding="utf-8")
        _handle_gitignore(tmp_path, check=False)
        gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        # Should not triple the newline
        assert "*.pyc\n\n" + "# >>> kbu-managed >>>" in gi

    def test_append_blank_line_no_trailing_newline(self, tmp_path: Path) -> None:
        """File not ending with newline → prepend \\n\\n before marker."""
        (tmp_path / ".gitignore").write_text("*.pyc", encoding="utf-8")
        _handle_gitignore(tmp_path, check=False)
        gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert "*.pyc\n\n" in gi

    def test_check_never_writes_gitignore(self, tmp_path: Path) -> None:
        """Under check=True, .gitignore is not created."""
        result = _check_gitignore_action(tmp_path)
        assert result == "would create with kbu marker block"
        assert not (tmp_path / ".gitignore").exists()


# ---------------------------------------------------------------------------
# AC 16: .bak filename format
# ---------------------------------------------------------------------------


class TestAC16BakFormat:
    def test_bak_filename_format(self, tmp_path: Path) -> None:
        """Backup files follow YYYYMMDDTHHMMSSZ format (POSIX-safe, no colons)."""
        import re
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        rel_path = _CLAUDE_COMMAND_FILES[0]
        dest = tmp_path / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("# different content\n")

        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            runner.invoke(
                main,
                ["bootstrap", "--no-venv", "--force-overwrite",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        bak_files = list(dest.parent.glob(f"{dest.name}.bak.*"))
        assert len(bak_files) >= 1
        bak_name = bak_files[0].name
        # Match YYYYMMDDTHHMMSSZ, no colons
        assert re.search(r"\.bak\.\d{8}T\d{6}Z$", bak_name), f"Bad bak format: {bak_name}"
        assert ":" not in bak_name


# ---------------------------------------------------------------------------
# AC 17: venv detection probe order
# ---------------------------------------------------------------------------


class TestAC17VenvProbeOrder:
    def test_probe_1_virtual_env_env_var(self, tmp_path: Path) -> None:
        """$VIRTUAL_ENV env var (probe 1) wins when set."""
        fake_python = tmp_path / "envvenv" / "bin" / "python"
        _make_venv_python(fake_python)
        # Also create .venv (should not be returned)
        _make_venv_python(tmp_path / ".venv" / "bin" / "python")

        with patch.dict(os.environ, {"VIRTUAL_ENV": str(tmp_path / "envvenv")}):
            result = _probe_venv(tmp_path)
        assert result == fake_python

    def test_probe_2_activate_sh(self, tmp_path: Path) -> None:
        """activate.sh (probe 2) wins when VIRTUAL_ENV not set."""
        venv_dir = tmp_path / "myvenv"
        fake_python = _make_venv_python(venv_dir / "bin" / "python")
        activate_sh = tmp_path / "activate.sh"
        activate_sh.write_text(f'VIRTUAL_ENV="{venv_dir}"\nexport VIRTUAL_ENV\n')
        # Also create .venv (should not be returned)
        _make_venv_python(tmp_path / ".venv" / "bin" / "python")

        with patch.dict(os.environ, {}, clear=True):
            # Remove VIRTUAL_ENV if set
            env_backup = os.environ.pop("VIRTUAL_ENV", None)
            try:
                result = _probe_venv(tmp_path)
            finally:
                if env_backup is not None:
                    os.environ["VIRTUAL_ENV"] = env_backup

        assert result == fake_python

    def test_probe_3_dot_venv(self, tmp_path: Path) -> None:
        """.venv/bin/python (probe 3) wins when probes 1 and 2 absent."""
        fake_python = _make_venv_python(tmp_path / ".venv" / "bin" / "python")
        # Also create venv (should not be returned)
        _make_venv_python(tmp_path / "venv" / "bin" / "python")

        env_backup = os.environ.pop("VIRTUAL_ENV", None)
        try:
            result = _probe_venv(tmp_path)
        finally:
            if env_backup is not None:
                os.environ["VIRTUAL_ENV"] = env_backup

        assert result == fake_python

    def test_probe_4_venv(self, tmp_path: Path) -> None:
        """venv/bin/python (probe 4) when probes 1-3 absent."""
        fake_python = _make_venv_python(tmp_path / "venv" / "bin" / "python")

        env_backup = os.environ.pop("VIRTUAL_ENV", None)
        try:
            result = _probe_venv(tmp_path)
        finally:
            if env_backup is not None:
                os.environ["VIRTUAL_ENV"] = env_backup

        assert result == fake_python

    def test_probe_5_no_venv_returns_none(self, tmp_path: Path) -> None:
        """When no venv is detected, returns None."""
        env_backup = os.environ.pop("VIRTUAL_ENV", None)
        try:
            result = _probe_venv(tmp_path)
        finally:
            if env_backup is not None:
                os.environ["VIRTUAL_ENV"] = env_backup
        assert result is None


# ---------------------------------------------------------------------------
# AC 18: venv compat refusal / --force-venv
# ---------------------------------------------------------------------------


class TestAC18VenvCompat:
    def _make_repo_with_old_venv(self, tmp_path: Path, kbu_root: Path, version: str = "3.10") -> Path:
        """Create a git repo with .venv having a fake python reporting *version*."""
        _make_git_repo(tmp_path)
        fake_python = _make_venv_python(tmp_path / ".venv" / "bin" / "python")
        return fake_python

    def test_old_python_exits_1_without_force_venv(self, tmp_path: Path) -> None:
        """Python <3.11 venv exits 1 without --force-venv."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)
        fake_python = _make_venv_python(tmp_path / ".venv" / "bin" / "python")

        def _se(cmd, *args, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if isinstance(cmd, list) and "-c" in cmd and "sys.version_info" in " ".join(cmd):
                r.stdout = "3.10\n"
            elif isinstance(cmd, list) and "rev-parse" in cmd:
                r.stdout = "abc123\n"
            return r

        env_backup = os.environ.pop("VIRTUAL_ENV", None)
        try:
            runner = CliRunner()
            with (
                patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
                patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
                patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
                patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=_se),
                patch("kbutillib.cli.bootstrap.shutil.which", return_value=None),
            ):
                os.chdir(tmp_path)
                result = runner.invoke(
                    main,
                    ["bootstrap", "--author", "A", "--affiliation", "B", "--orcid", "0"],
                    catch_exceptions=False,
                )
        finally:
            if env_backup is not None:
                os.environ["VIRTUAL_ENV"] = env_backup

        assert result.exit_code == 1
        combined = result.output + (result.stderr or "")
        assert "3.10" in combined
        assert "--force-venv" in combined or "--no-venv" in combined

    def test_old_python_with_force_venv_proceeds(self, tmp_path: Path) -> None:
        """--force-venv bypasses the compat check."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)
        _make_venv_python(tmp_path / ".venv" / "bin" / "python")

        def _se(cmd, *args, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if isinstance(cmd, list) and "-c" in cmd and "sys.version_info" in " ".join(cmd):
                r.stdout = "3.10\n"
            elif isinstance(cmd, list) and "rev-parse" in cmd:
                r.stdout = "abc123\n"
            elif isinstance(cmd, list) and "config" in cmd:
                r.stdout = "Test User\n"
            return r

        env_backup = os.environ.pop("VIRTUAL_ENV", None)
        try:
            runner = CliRunner()
            with (
                patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
                patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
                patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
                patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=_se),
                patch("kbutillib.cli.bootstrap.shutil.which", return_value=None),
            ):
                os.chdir(tmp_path)
                result = runner.invoke(
                    main,
                    ["bootstrap", "--force-venv",
                     "--author", "A", "--affiliation", "B", "--orcid", "0"],
                    catch_exceptions=False,
                )
        finally:
            if env_backup is not None:
                os.environ["VIRTUAL_ENV"] = env_backup

        assert result.exit_code == 0
        assert (tmp_path / "kbu-project.toml").exists()


# ---------------------------------------------------------------------------
# AC 19: venv fallback chain
# ---------------------------------------------------------------------------


class TestAC19VenvFallback:
    def test_no_venv_uses_venvman_when_available(self, tmp_path: Path) -> None:
        """No venv detected + venvman available → venvman create called."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)
        # NOTE: do NOT create activate.sh or .venv beforehand, so _probe_venv returns None.
        # We mock _run_venvman_project directly so it doesn't need the real filesystem.

        calls_seen = []

        def _se(cmd, *args, **kwargs):
            calls_seen.append(list(cmd) if isinstance(cmd, list) else cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if isinstance(cmd, list) and "rev-parse" in cmd:
                r.stdout = "abc123\n"
            elif isinstance(cmd, list) and "config" in cmd:
                r.stdout = "Test User\n"
            elif isinstance(cmd, list) and "-c" in cmd:
                r.stdout = "3.11\n"
            return r

        venvman_python = tmp_path / "venvman_venv" / "bin" / "python"
        _make_venv_python(venvman_python)

        env_backup = os.environ.pop("VIRTUAL_ENV", None)
        try:
            runner = CliRunner()
            with (
                patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
                patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
                patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
                patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=_se),
                patch("kbutillib.cli.bootstrap.shutil.which", return_value="/usr/local/bin/venvman"),
                # Mock _run_venvman_project so it doesn't need real venvman
                patch(
                    "kbutillib.cli.bootstrap._run_venvman_project",
                    return_value=(venvman_python, ""),
                ),
            ):
                os.chdir(tmp_path)
                result = runner.invoke(
                    main,
                    ["bootstrap", "--author", "A", "--affiliation", "B", "--orcid", "0"],
                    catch_exceptions=False,
                )
        finally:
            if env_backup is not None:
                os.environ["VIRTUAL_ENV"] = env_backup

        # Verify venvman path was used (pip install should use venvman_python)
        pip_calls = [
            c for c in calls_seen
            if isinstance(c, list) and "pip" in c and str(venvman_python) in c
        ]
        assert len(pip_calls) >= 1, f"Expected pip call with venvman python. calls: {calls_seen}"

    def test_no_venv_no_venvman_uses_python_m_venv(self, tmp_path: Path) -> None:
        """No venv detected + no venvman → python -m venv .venv called."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        calls_seen = []
        def _se(cmd, *args, **kwargs):
            calls_seen.append(list(cmd) if isinstance(cmd, list) else cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if isinstance(cmd, list) and "-m" in cmd and "venv" in cmd:
                cwd = kwargs.get("cwd", str(tmp_path))
                _make_venv_python(Path(cwd) / ".venv" / "bin" / "python")
            elif isinstance(cmd, list) and "rev-parse" in cmd:
                r.stdout = "abc123\n"
            elif isinstance(cmd, list) and "config" in cmd:
                r.stdout = "Test User\n"
            elif isinstance(cmd, list) and "-c" in cmd:
                r.stdout = "3.11\n"
            return r

        env_backup = os.environ.pop("VIRTUAL_ENV", None)
        try:
            runner = CliRunner()
            with (
                patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
                patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
                patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
                patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=_se),
                patch("kbutillib.cli.bootstrap.shutil.which", return_value=None),
            ):
                os.chdir(tmp_path)
                result = runner.invoke(
                    main,
                    ["bootstrap", "--author", "A", "--affiliation", "B", "--orcid", "0"],
                    catch_exceptions=False,
                )
        finally:
            if env_backup is not None:
                os.environ["VIRTUAL_ENV"] = env_backup

        venv_calls = [c for c in calls_seen if isinstance(c, list) and "-m" in c and "venv" in c]
        assert len(venv_calls) >= 1


# ---------------------------------------------------------------------------
# AC 20: --no-venv skips all venv work
# ---------------------------------------------------------------------------


class TestAC20NoVenv:
    def test_no_venv_skips_pip_and_kernel(self, tmp_path: Path) -> None:
        """--no-venv: no pip install or ipykernel subprocess calls."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        calls_seen = []
        def _se(cmd, *args, **kwargs):
            calls_seen.append(list(cmd) if isinstance(cmd, list) else cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = "abc123\n"
            r.stderr = ""
            return r

        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=_se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )

        pip_calls = [c for c in calls_seen if isinstance(c, list) and "pip" in c]
        kernel_calls = [
            c for c in calls_seen
            if isinstance(c, list) and len(c) >= 3 and c[1:3] == ["-m", "ipykernel"]
        ]
        assert pip_calls == []
        assert kernel_calls == []

    def test_no_venv_still_writes_manifest(self, tmp_path: Path) -> None:
        """--no-venv: manifest still written."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        assert (tmp_path / "kbu-project.toml").exists()


# ---------------------------------------------------------------------------
# AC 21: --no-kernel skips only kernel registration
# ---------------------------------------------------------------------------


class TestAC21NoKernel:
    def test_no_kernel_skips_ipykernel_but_runs_pip(self, tmp_path: Path) -> None:
        """--no-kernel: pip install runs; ipykernel install does not."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        calls_seen = []
        def _se(cmd, *args, **kwargs):
            calls_seen.append(list(cmd) if isinstance(cmd, list) else cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if isinstance(cmd, list) and "-m" in cmd and "venv" in cmd:
                cwd = kwargs.get("cwd", str(tmp_path))
                _make_venv_python(Path(cwd) / ".venv" / "bin" / "python")
            elif isinstance(cmd, list) and "rev-parse" in cmd:
                r.stdout = "abc123\n"
            elif isinstance(cmd, list) and "-c" in cmd:
                r.stdout = "3.11\n"
            elif isinstance(cmd, list) and "config" in cmd:
                r.stdout = "Test User\n"
            return r

        env_backup = os.environ.pop("VIRTUAL_ENV", None)
        try:
            runner = CliRunner()
            with (
                patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
                patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
                patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
                patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=_se),
                patch("kbutillib.cli.bootstrap.shutil.which", return_value=None),
            ):
                os.chdir(tmp_path)
                result = runner.invoke(
                    main,
                    ["bootstrap", "--no-kernel",
                     "--author", "A", "--affiliation", "B", "--orcid", "0"],
                    catch_exceptions=False,
                )
        finally:
            if env_backup is not None:
                os.environ["VIRTUAL_ENV"] = env_backup

        assert result.exit_code == 0
        pip_calls = [c for c in calls_seen if isinstance(c, list) and "pip" in c]
        kernel_calls = [
            c for c in calls_seen
            if isinstance(c, list) and len(c) >= 3 and c[1:3] == ["-m", "ipykernel"]
        ]
        assert len(pip_calls) >= 1
        assert kernel_calls == []


# ---------------------------------------------------------------------------
# AC 22: pip install command shape
# ---------------------------------------------------------------------------


class TestAC22PipCommand:
    def test_pip_command_shape(self, tmp_path: Path) -> None:
        """pip install runs as <venv_python> -m pip install -e <KBUTILLIB_ROOT>."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)
        fake_python = _make_venv_python(tmp_path / ".venv" / "bin" / "python")

        pip_calls = []
        def _se(cmd, *args, **kwargs):
            if isinstance(cmd, list) and "pip" in cmd:
                pip_calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if isinstance(cmd, list) and "-c" in cmd:
                r.stdout = "3.11\n"
            elif isinstance(cmd, list) and "rev-parse" in cmd:
                r.stdout = "abc123\n"
            elif isinstance(cmd, list) and "config" in cmd:
                r.stdout = "Test User\n"
            return r

        env_backup = os.environ.pop("VIRTUAL_ENV", None)
        try:
            runner = CliRunner()
            with (
                patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
                patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
                patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
                patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=_se),
                patch("kbutillib.cli.bootstrap.shutil.which", return_value=None),
            ):
                os.chdir(tmp_path)
                result = runner.invoke(
                    main,
                    ["bootstrap", "--author", "A", "--affiliation", "B", "--orcid", "0"],
                    catch_exceptions=False,
                )
        finally:
            if env_backup is not None:
                os.environ["VIRTUAL_ENV"] = env_backup

        assert len(pip_calls) >= 1
        pc = pip_calls[0]
        assert str(fake_python) == pc[0]
        assert pc[1:4] == ["-m", "pip", "install"]
        assert "-e" in pc
        assert str(kbu_root) in pc


# ---------------------------------------------------------------------------
# AC 23: ipykernel command shape
# ---------------------------------------------------------------------------


class TestAC23KernelCommand:
    def test_kernel_command_shape_and_message(self, tmp_path: Path) -> None:
        """ipykernel install runs with correct args; success message printed."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)
        fake_python = _make_venv_python(tmp_path / ".venv" / "bin" / "python")

        kernel_calls = []
        def _se(cmd, *args, **kwargs):
            if (
                isinstance(cmd, list) and len(cmd) >= 3
                and cmd[1:3] == ["-m", "ipykernel"]
            ):
                kernel_calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if isinstance(cmd, list) and "-c" in cmd:
                r.stdout = "3.11\n"
            elif isinstance(cmd, list) and "rev-parse" in cmd:
                r.stdout = "abc123\n"
            elif isinstance(cmd, list) and "config" in cmd:
                r.stdout = "Test User\n"
            return r

        env_backup = os.environ.pop("VIRTUAL_ENV", None)
        try:
            runner = CliRunner()
            with (
                patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
                patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
                patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
                patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=_se),
                patch("kbutillib.cli.bootstrap.shutil.which", return_value=None),
            ):
                os.chdir(tmp_path)
                result = runner.invoke(
                    main,
                    ["bootstrap", "--name", "myproj",
                     "--author", "A", "--affiliation", "B", "--orcid", "0"],
                    catch_exceptions=False,
                )
        finally:
            if env_backup is not None:
                os.environ["VIRTUAL_ENV"] = env_backup

        assert len(kernel_calls) >= 1
        kc = kernel_calls[0]
        assert str(fake_python) == kc[0]
        assert "--name=myproj" in kc
        assert "--display-name=myproj (kbu)" in kc
        assert "--user" in kc
        assert "registered jupyter kernel" in result.output
        assert "myproj" in result.output


# ---------------------------------------------------------------------------
# AC 24–28: Manifest contents
# ---------------------------------------------------------------------------


class TestAC24To28Manifest:
    def _run_bootstrap(self, tmp_path: Path) -> dict:
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        (kbu_root / ".git").mkdir()
        _make_stub_template(kbu_root)

        se = _make_subprocess_side_effect(git_commit="deadbeef123")
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            runner.invoke(
                main,
                ["bootstrap", "--no-venv", "--name", "testproj",
                 "--author", "Alice", "--affiliation", "Lab", "--orcid", "0001"],
                catch_exceptions=False,
            )
        with open(tmp_path / "kbu-project.toml", "rb") as f:
            return tomllib.load(f)

    def test_project_section_fields(self, tmp_path: Path) -> None:
        """[project] has name, created_at, bootstrapped=true, bootstrapped_at."""
        cfg = self._run_bootstrap(tmp_path)
        proj = cfg["project"]
        assert proj["name"] == "testproj"
        assert "created_at" in proj
        assert proj["bootstrapped"] is True
        assert "bootstrapped_at" in proj
        # created_at and bootstrapped_at are the same timestamp
        assert proj["created_at"] == proj["bootstrapped_at"]

    def test_authors_section(self, tmp_path: Path) -> None:
        """[[project.authors]] has name/affiliation/orcid."""
        cfg = self._run_bootstrap(tmp_path)
        auth = cfg["project"]["authors"][0]
        assert auth["name"] == "Alice"
        assert auth["affiliation"] == "Lab"
        assert auth["orcid"] == "0001"

    def test_kbutillib_section(self, tmp_path: Path) -> None:
        """[kbutillib] has source_path and source_commit."""
        cfg = self._run_bootstrap(tmp_path)
        kb = cfg["kbutillib"]
        assert "source_path" in kb
        assert "source_commit" in kb

    def test_update_section(self, tmp_path: Path) -> None:
        """[update] has last_pulled_at, last_pulled_commit, file_hashes."""
        cfg = self._run_bootstrap(tmp_path)
        upd = cfg["update"]
        assert "last_pulled_at" in upd
        assert "last_pulled_commit" in upd
        assert "file_hashes" in upd

    def test_file_hashes_non_empty(self, tmp_path: Path) -> None:
        """[update.file_hashes] has at least one entry for copied files."""
        cfg = self._run_bootstrap(tmp_path)
        fh = cfg["update"]["file_hashes"]
        assert len(fh) >= 1

    def test_file_hashes_values_prefixed(self, tmp_path: Path) -> None:
        """All file_hashes values start with 'sha256:'."""
        cfg = self._run_bootstrap(tmp_path)
        for k, v in cfg["update"]["file_hashes"].items():
            assert v.startswith("sha256:"), f"Bad hash for {k}: {v}"


# ---------------------------------------------------------------------------
# AC 25: source_commit from git rev-parse HEAD
# ---------------------------------------------------------------------------


class TestAC25SourceCommit:
    def test_source_commit_from_git(self, tmp_path: Path) -> None:
        """source_commit is the stripped stdout of git -C <KBUTILLIB_ROOT> rev-parse HEAD."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        (kbu_root / ".git").mkdir()
        _make_stub_template(kbu_root)

        def _se(cmd, *args, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if isinstance(cmd, list) and "rev-parse" in cmd:
                r.stdout = "cafebabe123456\n"
            elif isinstance(cmd, list) and "config" in cmd:
                r.stdout = "Test\n"
            return r

        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=_se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )

        with open(tmp_path / "kbu-project.toml", "rb") as f:
            cfg = tomllib.load(f)
        assert cfg["kbutillib"]["source_commit"] == "cafebabe123456"

    def test_source_commit_empty_when_not_git_repo(self, tmp_path: Path) -> None:
        """source_commit is empty string when KBUTILLIB_ROOT is not a git repo."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        # No .git in kbu_root
        _make_stub_template(kbu_root)

        def _se(cmd, *args, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if isinstance(cmd, list) and "rev-parse" in cmd:
                # Simulate git not finding a repo
                r.returncode = 128
                r.stdout = ""
            elif isinstance(cmd, list) and "config" in cmd:
                r.stdout = "Test\n"
            return r

        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=_se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )

        with open(tmp_path / "kbu-project.toml", "rb") as f:
            cfg = tomllib.load(f)
        assert cfg["kbutillib"]["source_commit"] == ""


# ---------------------------------------------------------------------------
# AC 26: file_hashes membership — exclusions
# ---------------------------------------------------------------------------


class TestAC26FileHashesMembership:
    def test_extensions_json_excluded_when_skipped(self, tmp_path: Path) -> None:
        """Skipped .vscode/extensions.json NOT in file_hashes."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)
        (tmp_path / ".vscode").mkdir()
        (tmp_path / ".vscode" / "extensions.json").write_text('{"recommendations": []}')

        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        with open(tmp_path / "kbu-project.toml", "rb") as f:
            cfg = tomllib.load(f)
        assert ".vscode/extensions.json" not in cfg["update"]["file_hashes"]

    def test_gitignore_excluded_from_file_hashes(self, tmp_path: Path) -> None:
        """.gitignore is NEVER in file_hashes (user-owned)."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        with open(tmp_path / "kbu-project.toml", "rb") as f:
            cfg = tomllib.load(f)
        assert ".gitignore" not in cfg["update"]["file_hashes"]

    def test_workspace_excluded_when_existing_workspace_present(self, tmp_path: Path) -> None:
        """code-workspace skipped → not in file_hashes."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)
        (tmp_path / "Existing.code-workspace").write_text("{}")

        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            runner.invoke(
                main,
                ["bootstrap", "--no-venv", "--name", "myrepo",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        with open(tmp_path / "kbu-project.toml", "rb") as f:
            cfg = tomllib.load(f)
        ws_key = "myrepo.code-workspace"
        assert ws_key not in cfg["update"]["file_hashes"]


# ---------------------------------------------------------------------------
# AC 27: sha256_file helper used for hashes
# ---------------------------------------------------------------------------


class TestAC27HashHelper:
    def test_file_hashes_match_sha256_file(self, tmp_path: Path) -> None:
        """Recorded file hashes match sha256_file(dest) for written files."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        with open(tmp_path / "kbu-project.toml", "rb") as f:
            cfg = tomllib.load(f)
        for rel_path, recorded_hash in cfg["update"]["file_hashes"].items():
            disk_path = tmp_path / rel_path
            assert disk_path.exists(), f"Missing: {disk_path}"
            expected = "sha256:" + sha256_file(disk_path)
            assert recorded_hash == expected, f"Hash mismatch for {rel_path}"


# ---------------------------------------------------------------------------
# AC 28: Timestamps use now_utc_iso helper
# ---------------------------------------------------------------------------


class TestAC28Timestamps:
    def test_timestamps_have_z_suffix(self, tmp_path: Path) -> None:
        """All timestamp fields use ISO-8601 UTC with Z suffix."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        with open(tmp_path / "kbu-project.toml", "rb") as f:
            cfg = tomllib.load(f)
        import re
        z_pattern = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
        for field in ["created_at", "bootstrapped_at"]:
            ts = cfg["project"][field]
            assert z_pattern.match(ts), f"Bad timestamp for {field}: {ts}"
        assert z_pattern.match(cfg["update"]["last_pulled_at"])


# ---------------------------------------------------------------------------
# AC 29: No git add / git commit
# ---------------------------------------------------------------------------


class TestAC29NoGitCommit:
    def test_no_git_add_or_commit_called(self, tmp_path: Path) -> None:
        """Bootstrap never calls git add or git commit."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        calls_seen = []
        def _se(cmd, *args, **kwargs):
            calls_seen.append(list(cmd) if isinstance(cmd, list) else cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = "abc123\n"
            r.stderr = ""
            return r

        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=_se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )

        git_write_calls = [
            c for c in calls_seen
            if isinstance(c, list) and c[0] == "git" and any(
                x in c for x in ["add", "commit"]
            )
        ]
        assert git_write_calls == [], f"Unexpected git write calls: {git_write_calls}"


# ---------------------------------------------------------------------------
# AC 30: No init marker write
# ---------------------------------------------------------------------------


class TestAC30NoInitMarker:
    def test_init_marker_not_written(self, tmp_path: Path) -> None:
        """Bootstrap does NOT create ~/.config/kbu/init_done.json."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        fake_config_dir = tmp_path / "fake_config"
        fake_config_dir.mkdir()

        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
            patch.dict(os.environ, {"XDG_CONFIG_HOME": str(fake_config_dir)}),
        ):
            os.chdir(tmp_path)
            runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )

        # init_done.json should not exist in fake config dir
        assert not (fake_config_dir / "kbu" / "init_done.json").exists()


# ---------------------------------------------------------------------------
# AC 31: Success summary structure
# ---------------------------------------------------------------------------


class TestAC31SuccessSummary:
    def _run_and_get_output(self, tmp_path: Path) -> str:
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            result = runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        return result.output

    def test_success_summary_has_manifest_line(self, tmp_path: Path) -> None:
        output = self._run_and_get_output(tmp_path)
        assert "kbu-project.toml" in output
        assert "bootstrapped=true" in output.lower() or "[project].bootstrapped=true" in output

    def test_success_summary_has_commit_hint(self, tmp_path: Path) -> None:
        output = self._run_and_get_output(tmp_path)
        assert "git" in output
        assert "chore(kbu): bootstrap kbu-awareness" in output

    def test_success_summary_has_workflow_hint(self, tmp_path: Path) -> None:
        output = self._run_and_get_output(tmp_path)
        assert "/kbu-start" in output

    def test_success_summary_has_undo_instructions(self, tmp_path: Path) -> None:
        output = self._run_and_get_output(tmp_path)
        assert "rm kbu-project.toml" in output
        assert ".gitignore" in output

    def test_success_summary_has_files_written(self, tmp_path: Path) -> None:
        output = self._run_and_get_output(tmp_path)
        assert "Files written" in output


# ---------------------------------------------------------------------------
# AC 34: kbu doctor project origin line (already in test_doctor_origin.py;
#         one smoke test here for completeness)
# ---------------------------------------------------------------------------


class TestAC34DoctorOrigin:
    def test_doctor_origin_in_help_output(self) -> None:
        """kbu doctor --help exits 0 (command is registered)."""
        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--help"])
        assert result.exit_code == 0

    def test_bootstrapped_manifest_probe(self, tmp_path: Path) -> None:
        """_probe_project_origin returns 'bootstrap' for bootstrapped manifest."""
        from kbutillib.cli.init import _probe_project_origin
        ts = "2026-06-07T12:00:00Z"
        # Write a real manifest so read_project_manifest finds it from cwd
        write_project_manifest(tmp_path, {
            "project": {"name": "x", "created_at": ts,
                        "bootstrapped": True, "bootstrapped_at": ts,
                        "authors": [{"name": "A", "affiliation": "B", "orcid": "0"}]},
            "kbutillib": {"source_path": "/fake", "source_commit": ""},
            "update": {"last_pulled_at": ts, "last_pulled_commit": "",
                       "file_hashes": {}},
        })
        os.chdir(tmp_path)
        result = _probe_project_origin()
        assert f"project origin: bootstrap ({ts})" == result


# ---------------------------------------------------------------------------
# AC 35: bootstrap module exposes bootstrap_command and bootstrap()
# ---------------------------------------------------------------------------


class TestAC35ModuleExports:
    def test_bootstrap_command_exported(self) -> None:
        from kbutillib.cli.bootstrap import bootstrap_command
        assert callable(bootstrap_command)

    def test_bootstrap_function_exported(self) -> None:
        from kbutillib.cli.bootstrap import bootstrap
        assert callable(bootstrap)

    def test_bootstrap_function_callable_from_tests(self, tmp_path: Path) -> None:
        """bootstrap() orchestration function can be called directly."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        se = _make_subprocess_side_effect()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            bootstrap(
                name="testproj",
                author="Alice",
                affiliation="Lab",
                orcid="0001",
                first_subproject=None,
                no_venv=True,
                no_kernel=False,
                force_overwrite=False,
                force_venv=False,
                check=False,
                project_root=tmp_path,
            )
        assert (tmp_path / "kbu-project.toml").exists()


# ---------------------------------------------------------------------------
# AC 36: _template_ops module exports (already tested in p1; smoke here)
# ---------------------------------------------------------------------------


class TestAC36TemplateOpsModule:
    def test_template_ops_exports(self) -> None:
        """_template_ops exports the required public API."""
        from kbutillib.cli._template_ops import (
            copy_template_tree,
            compute_file_hashes,
            run_venvman_project,
            create_plain_venv,
            parse_virtual_env_from_activate,
        )
        assert all(callable(fn) for fn in [
            copy_template_tree, compute_file_hashes,
            run_venvman_project, create_plain_venv,
            parse_virtual_env_from_activate,
        ])

    def test_new_project_imports_from_template_ops(self) -> None:
        """new_project.py uses helpers from _template_ops (not local defs)."""
        import kbutillib.cli.new_project as np_mod
        # These should be the same objects (not separate definitions)
        from kbutillib.cli._template_ops import copy_template_tree
        assert np_mod._copy_template_tree is copy_template_tree

    def test_parse_virtual_env_supports_venv_subdir_format(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """parse_virtual_env_from_activate resolves the current venvman format.

        Regression: the old parser only matched a literal ``VIRTUAL_ENV=`` line.
        venvman now writes ``VENV_SUBDIR="..."`` and composes the path at
        activate time from ``${VIRTUAL_ENVIRONMENT_DIRECTORY}/${VENV_SUBDIR}``.
        """
        from kbutillib.cli._template_ops import parse_virtual_env_from_activate

        venv_root = tmp_path / "envroot"
        venv_root.mkdir()
        monkeypatch.setenv("VIRTUAL_ENVIRONMENT_DIRECTORY", str(venv_root))
        f = tmp_path / "activate.sh"
        f.write_text(
            'VENV_SUBDIR="myproject-py3.11"\n'
            'VENV_PATH="${VIRTUAL_ENVIRONMENT_DIRECTORY}/${VENV_SUBDIR}"\n',
            encoding="utf-8",
        )
        assert parse_virtual_env_from_activate(f) == venv_root / "myproject-py3.11"


# ---------------------------------------------------------------------------
# AC 37: --first-subproject invocation
# ---------------------------------------------------------------------------


class TestAC37FirstSubproject:
    def test_first_subproject_invokes_subproject_create(self, tmp_path: Path) -> None:
        """--first-subproject causes 'kbu subproject create' to run after bootstrap."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        calls_seen = []
        def _se(cmd, *args, **kwargs):
            calls_seen.append(list(cmd) if isinstance(cmd, list) else cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = "abc123\n"
            r.stderr = ""
            return r

        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=_se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            result = runner.invoke(
                main,
                ["bootstrap", "--no-venv", "--first-subproject", "my_analysis",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        subproject_calls = [
            c for c in calls_seen
            if isinstance(c, list) and "subproject" in c and "create" in c and "my_analysis" in c
        ]
        assert len(subproject_calls) == 1

    def test_first_subproject_failure_does_not_rollback(self, tmp_path: Path) -> None:
        """If --first-subproject fails, bootstrap itself still succeeds."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        def _se(cmd, *args, **kwargs):
            r = MagicMock()
            r.stdout = "abc123\n"
            r.stderr = ""
            if isinstance(cmd, list) and "subproject" in cmd:
                r.returncode = 1
            else:
                r.returncode = 0
            return r

        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=_se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            result = runner.invoke(
                main,
                ["bootstrap", "--no-venv", "--first-subproject", "fail_sp",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        # Bootstrap succeeds; manifest was written
        assert result.exit_code == 0
        assert (tmp_path / "kbu-project.toml").exists()


# ---------------------------------------------------------------------------
# AC 38: --check --first-subproject reports but does not invoke
# ---------------------------------------------------------------------------


class TestAC38CheckFirstSubproject:
    def test_check_reports_first_subproject_without_invoking(self, tmp_path: Path) -> None:
        """--check --first-subproject reports the planned action without running it."""
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)

        calls_seen = []
        def _se(cmd, *args, **kwargs):
            calls_seen.append(list(cmd) if isinstance(cmd, list) else cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = "abc123\n"
            r.stderr = ""
            return r

        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=_se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            result = runner.invoke(
                main,
                ["bootstrap", "--check", "--first-subproject", "my_sp",
                 "--name", "myproj", "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        # Plan output should mention the subproject
        assert "my_sp" in result.output
        # subproject create should NOT have been called
        sp_calls = [
            c for c in calls_seen
            if isinstance(c, list) and "subproject" in c and "create" in c
        ]
        assert sp_calls == []


# ---------------------------------------------------------------------------
# AC 42: bootstrap scaffolds data/, models/, genomes/ with .gitkeep
# AC 43: kbu-project.toml has [layout.shared_dirs]
# AC 44: subagent sources have type: agent frontmatter
# AC 45: subagent sources live at .claude/agents/<name>.md
# AC 47: old slash-command sources removed; kbu-sub-* subagents created
# ---------------------------------------------------------------------------


class TestAC42_47SharedDirsAndAgents:
    """AC #42-#47: shared dirs, layout section, subagent files."""

    @pytest.fixture()
    def setup(self, tmp_path: Path):
        _make_git_repo(tmp_path)
        kbu_root = tmp_path / "KBUtilLib"
        kbu_root.mkdir()
        _make_stub_template(kbu_root)
        return tmp_path, kbu_root

    def _run_bootstrap(self, tmp_path: Path, kbu_root: Path) -> Any:
        se = _make_subprocess_side_effect()
        runner = CliRunner()
        with (
            patch("kbutillib.cli.bootstrap._kbutillib_root", return_value=kbu_root),
            patch("kbutillib.cli.bootstrap._is_macos_or_override", return_value=True),
            patch("kbutillib.cli.bootstrap._is_darwin", return_value=True),
            patch("kbutillib.cli.bootstrap.subprocess.run", side_effect=se),
            patch("shutil.which", return_value=None),
        ):
            os.chdir(tmp_path)
            return runner.invoke(
                main,
                ["bootstrap", "--no-venv",
                 "--author", "A", "--affiliation", "B", "--orcid", "0"],
                catch_exceptions=False,
            )

    def test_ac42_shared_dirs_with_gitkeep(self, setup: Any) -> None:
        """AC #42: bootstrap scaffolds data/, models/, genomes/ with .gitkeep."""
        tmp_path, kbu_root = setup
        result = self._run_bootstrap(tmp_path, kbu_root)
        assert result.exit_code == 0
        for shared_dir in ["data", "models", "genomes"]:
            assert (tmp_path / shared_dir).is_dir(), f"{shared_dir}/ not created"
            assert (tmp_path / shared_dir / ".gitkeep").exists(), f"{shared_dir}/.gitkeep missing"

    def test_ac43_layout_shared_dirs_in_toml(self, setup: Any) -> None:
        """AC #43: kbu-project.toml has [layout.shared_dirs] = ["data","models","genomes"]."""
        tmp_path, kbu_root = setup
        result = self._run_bootstrap(tmp_path, kbu_root)
        assert result.exit_code == 0
        with open(tmp_path / "kbu-project.toml", "rb") as f:
            cfg = tomllib.load(f)
        assert "layout" in cfg, "Missing [layout] section in kbu-project.toml"
        assert cfg["layout"]["shared_dirs"] == ["data", "models", "genomes"]

    def test_ac44_subagent_files_have_type_agent(self, setup: Any) -> None:
        """AC #44: each .claude/agents/ file has type: agent in frontmatter."""
        tmp_path, kbu_root = setup
        result = self._run_bootstrap(tmp_path, kbu_root)
        assert result.exit_code == 0
        for agent_rel in _CLAUDE_AGENT_FILES:
            agent_path = tmp_path / agent_rel
            assert agent_path.exists(), f"{agent_rel} not copied to project"
            content = agent_path.read_text(encoding="utf-8")
            assert "type: agent" in content, f"{agent_rel} missing 'type: agent' in frontmatter"

    def test_ac45_subagent_files_in_agents_dir(self, setup: Any) -> None:
        """AC #45: subagent sources live at .claude/agents/<name>.md."""
        tmp_path, kbu_root = setup
        result = self._run_bootstrap(tmp_path, kbu_root)
        assert result.exit_code == 0
        agents_dir = tmp_path / ".claude" / "agents"
        assert agents_dir.is_dir(), ".claude/agents/ directory not created"
        for agent_rel in _CLAUDE_AGENT_FILES:
            assert (tmp_path / agent_rel).exists(), f"{agent_rel} missing"

    def test_ac47_old_command_files_absent_new_subagents_present(self, setup: Any) -> None:
        """AC #47: old slash-commands (kbu-review etc.) removed; kbu-sub-* present in agents."""
        tmp_path, kbu_root = setup
        result = self._run_bootstrap(tmp_path, kbu_root)
        assert result.exit_code == 0
        # Old files must not be in commands
        for old_name in ["kbu-literature-review.md", "kbu-review.md", "kbu-diagnose.md"]:
            old_path = tmp_path / ".claude" / "commands" / old_name
            assert not old_path.exists(), f"Old command {old_name} should not be present"
        # New subagent files must be present in agents
        for agent_rel in _CLAUDE_AGENT_FILES:
            assert (tmp_path / agent_rel).exists(), f"{agent_rel} should be present"

    def test_shared_dirs_existing_content_untouched(self, setup: Any) -> None:
        """If shared dir already has content, bootstrap leaves it alone."""
        tmp_path, kbu_root = setup
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "important.csv").write_text("col1,col2\n1,2\n", encoding="utf-8")

        result = self._run_bootstrap(tmp_path, kbu_root)
        assert result.exit_code == 0
        # important.csv must still be there
        assert (data_dir / "important.csv").exists()
        # .gitkeep should NOT have been written (dir had content)
        assert not (data_dir / ".gitkeep").exists()
