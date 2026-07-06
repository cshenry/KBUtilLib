"""Hermetic tests for kbutillib.researchos modules.

All subprocess calls (research-os init, python -m venv, pip install, git,
registry python3 -c) are mocked so no network, no real research-os binary,
and NO writes to the real AIAssistant registry occur.

Covers:
- config.py: resolution precedence (explicit > env > config > default) for
  root, tooling-venv, and aiassistant-root; set_root round-trip.
- tooling.py: present binary returned without reinstall; absent with create=True
  calls venv + pip; create=False raises.
- registry.py: _slug; register_project argv; ok on rc=0; failed (no raise)
  on rc!=0 or missing src.
- manager.py: _validate_name; path composition; _run_init cwd; _rewrite_mcp_commands;
  _write_workspace; new() orchestration; list() two-level filtering; git failure
  warn-and-continue; registry failure warn-and-continue.
- CLI: --help; new/open positionals; ls --json; set-root; cursor not on PATH.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, call, patch

import pytest
import yaml
from click.testing import CliRunner

from kbutillib.cli.researchos import researchos_cmd


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture()
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override HOME and patch config module constants so writes go to tmp."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "kbutillib.researchos.config._KBUTILLIB_DIR",
        home / ".kbutillib",
    )
    monkeypatch.setattr(
        "kbutillib.researchos.config._DEFAULT_CONFIG_FILE",
        home / ".kbutillib" / "config.yaml",
    )
    monkeypatch.setattr(
        "kbutillib.researchos.config._DEFAULT_ROOT",
        home / "Dropbox" / "Projects" / "ResearchOS",
    )
    monkeypatch.setattr(
        "kbutillib.researchos.config._DEFAULT_TOOLING_VENV",
        home / ".venvs" / "research-os",
    )
    monkeypatch.setattr(
        "kbutillib.researchos.config._DEFAULT_AIASSISTANT_ROOT",
        home / "Dropbox" / "Projects" / "AIAssistant",
    )
    return home


@pytest.fixture()
def ros_root(tmp_path: Path) -> Path:
    """A temporary Research-OS root directory."""
    root = tmp_path / "ResearchOS"
    root.mkdir()
    return root


@pytest.fixture()
def tooling_venv(tmp_path: Path) -> Path:
    """A temporary tooling venv directory (without real research-os)."""
    venv = tmp_path / "venvs" / "research-os"
    venv.mkdir(parents=True)
    return venv


@pytest.fixture()
def aiassistant_root(tmp_path: Path) -> Path:
    """A temporary AIAssistant root with src/ present."""
    root = tmp_path / "AIAssistant"
    (root / "src").mkdir(parents=True)
    return root


@pytest.fixture()
def manager(ros_root: Path, tooling_venv: Path, aiassistant_root: Path):
    """A ResearchOSProject instance with temp directories."""
    from kbutillib.researchos.manager import ResearchOSProject

    return ResearchOSProject(
        researchos_root=ros_root,
        tooling_venv=tooling_venv,
        aiassistant_root=aiassistant_root,
    )


# ---------------------------------------------------------------------------
# Helpers: fake subprocess.run for init / venv / pip / git / registry
# ---------------------------------------------------------------------------


def _make_fake_run(
    *,
    init_rc: int = 0,
    venv_rc: int = 0,
    pip_rc: int = 0,
    git_rc: int = 0,
    registry_rc: int = 0,
    registry_stdout: str = "",
    create_project_dir: bool = True,
    ros_root: Path | None = None,
):
    """Return a fake subprocess.run that succeeds for all expected calls.

    Args:
        init_rc: Return code for research-os init.
        venv_rc: Return code for python -m venv.
        pip_rc: Return code for pip install.
        git_rc: Return code for git commands.
        registry_rc: Return code for the registry python3 -c subprocess.
        registry_stdout: stdout for the registry call.
        create_project_dir: If True, fake init creates the <name>/.os_state/ dir.
        ros_root: Required when create_project_dir=True (to resolve cwd).
    """
    calls: list = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        r = MagicMock()
        r.stdout = ""
        r.stderr = ""

        if not isinstance(cmd, (list, tuple)):
            r.returncode = 0
            return r

        cmd_str = " ".join(str(c) for c in cmd)

        # research-os init
        if "init" in cmd_str and ("research-os" in cmd_str or "research_os" in cmd_str.replace("-", "_")):
            r.returncode = init_rc
            # Simulate research-os creating the project directory
            if create_project_dir and init_rc == 0 and ros_root is not None:
                cwd = kwargs.get("cwd") or str(Path.cwd())
                # Find the name from the cmd (3rd positional arg after 'init')
                try:
                    idx = list(cmd).index("init")
                    proj_name = cmd[idx + 1]
                    proj_path = Path(cwd) / proj_name
                    (proj_path / ".os_state").mkdir(parents=True, exist_ok=True)
                    (proj_path / ".mcp.json").write_text(
                        json.dumps({
                            "mcpServers": {
                                "research-os": {
                                    "command": "research-os",
                                    "args": ["serve"],
                                }
                            }
                        }),
                        encoding="utf-8",
                    )
                    cursor_dir = proj_path / ".cursor"
                    cursor_dir.mkdir(exist_ok=True)
                    (cursor_dir / "mcp.json").write_text(
                        json.dumps({
                            "mcpServers": {
                                "research-os": {
                                    "command": "research-os",
                                    "args": ["serve"],
                                }
                            }
                        }),
                        encoding="utf-8",
                    )
                    claude_dir = proj_path / ".claude"
                    claude_dir.mkdir(exist_ok=True)
                    (claude_dir / "mcp.json").write_text(
                        json.dumps({
                            "mcpServers": {
                                "research-os": {
                                    "command": "research-os",
                                    "args": ["serve"],
                                }
                            }
                        }),
                        encoding="utf-8",
                    )
                except (ValueError, IndexError):
                    pass
            elif init_rc != 0:
                r.stderr = "research-os init failed"
            return r

        # python -m venv (venv creation)
        if "-m" in str(cmd) and "venv" in cmd_str:
            r.returncode = venv_rc
            if venv_rc != 0:
                r.stderr = "venv creation failed"
            else:
                # Simulate the binary being created after venv
                pass
            return r

        # pip install
        if "pip" in cmd_str and "install" in cmd_str:
            r.returncode = pip_rc
            if pip_rc != 0:
                r.stderr = "pip install failed"
            return r

        # git
        if cmd[0] == "git" or (len(cmd) > 1 and "git" in cmd[0]):
            r.returncode = git_rc
            if git_rc != 0:
                r.stderr = "git command failed"
            return r

        # registry python3 -c
        if (cmd[0] in (sys.executable, "python3", "python") and
                len(cmd) > 1 and cmd[1] == "-c"):
            r.returncode = registry_rc
            r.stdout = registry_stdout
            if registry_rc != 0:
                r.stderr = "registry error"
            return r

        r.returncode = 0
        return r

    return fake_run, calls


# ===========================================================================
# Tests — config.py
# ===========================================================================


class TestConfigResolution:
    """Test resolution precedence for all three config paths."""

    def test_root_explicit_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit arg wins for researchos_root."""
        from kbutillib.researchos.config import resolve_researchos_root

        monkeypatch.setenv("RESEARCHOS_ROOT", "/should/be/ignored")
        result = resolve_researchos_root(explicit=str(tmp_path))
        assert result == tmp_path.resolve()

    def test_root_env_wins_over_config(
        self, tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RESEARCHOS_ROOT env wins over config."""
        from kbutillib.researchos.config import resolve_researchos_root, set_root

        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        set_root(root=str(tmp_path / "from-config"), config_file=cfg_path)

        env_path = tmp_path / "from-env"
        env_path.mkdir()
        monkeypatch.setenv("RESEARCHOS_ROOT", str(env_path))

        result = resolve_researchos_root(config_file=cfg_path)
        assert result == env_path.resolve()

    def test_root_config_used_when_no_env(
        self, tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config researchos.root is used when env is absent."""
        from kbutillib.researchos.config import resolve_researchos_root, set_root

        monkeypatch.delenv("RESEARCHOS_ROOT", raising=False)
        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        cfg_root = tmp_path / "from-config"
        cfg_root.mkdir()
        set_root(root=str(cfg_root), config_file=cfg_path)

        result = resolve_researchos_root(config_file=cfg_path)
        assert result == cfg_root.resolve()

    def test_root_default_when_nothing_configured(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default root is used when nothing is configured."""
        from kbutillib.researchos.config import resolve_researchos_root

        monkeypatch.delenv("RESEARCHOS_ROOT", raising=False)
        cfg_path = tmp_home / ".kbutillib" / "config.yaml"

        result = resolve_researchos_root(config_file=cfg_path)
        # Should return the default (patched to tmp_home/Dropbox/...)
        assert "ResearchOS" in str(result) or str(result).endswith("ResearchOS")

    def test_tooling_venv_explicit_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit arg wins for tooling_venv."""
        from kbutillib.researchos.config import resolve_tooling_venv

        monkeypatch.setenv("RESEARCHOS_TOOLING_VENV", "/should/be/ignored")
        result = resolve_tooling_venv(explicit=str(tmp_path))
        assert result == tmp_path.resolve()

    def test_tooling_venv_env_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RESEARCHOS_TOOLING_VENV env wins over config."""
        from kbutillib.researchos.config import resolve_tooling_venv

        env_path = tmp_path / "env-venv"
        env_path.mkdir()
        monkeypatch.setenv("RESEARCHOS_TOOLING_VENV", str(env_path))

        result = resolve_tooling_venv()
        assert result == env_path.resolve()

    def test_tooling_venv_config_used(
        self, tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config researchos.tooling_venv is used when env is absent."""
        from kbutillib.researchos.config import resolve_tooling_venv, set_root

        monkeypatch.delenv("RESEARCHOS_TOOLING_VENV", raising=False)
        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        venv_path = tmp_path / "my-venv"
        venv_path.mkdir()
        set_root(tooling_venv=str(venv_path), config_file=cfg_path)

        result = resolve_tooling_venv(config_file=cfg_path)
        assert result == venv_path.resolve()

    def test_tooling_venv_default(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default tooling_venv is used when nothing is configured."""
        from kbutillib.researchos.config import resolve_tooling_venv

        monkeypatch.delenv("RESEARCHOS_TOOLING_VENV", raising=False)
        cfg_path = tmp_home / ".kbutillib" / "config.yaml"

        result = resolve_tooling_venv(config_file=cfg_path)
        assert "research-os" in str(result)

    def test_aiassistant_root_explicit_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit arg wins for aiassistant_root."""
        from kbutillib.researchos.config import resolve_aiassistant_root

        monkeypatch.setenv("AIASSISTANT_ROOT", "/should/be/ignored")
        result = resolve_aiassistant_root(explicit=str(tmp_path))
        assert result == tmp_path.resolve()

    def test_aiassistant_root_env_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AIASSISTANT_ROOT env wins over config."""
        from kbutillib.researchos.config import resolve_aiassistant_root

        env_path = tmp_path / "ai-root"
        env_path.mkdir()
        monkeypatch.setenv("AIASSISTANT_ROOT", str(env_path))

        result = resolve_aiassistant_root()
        assert result == env_path.resolve()

    def test_aiassistant_root_config_used(
        self, tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config researchos.aiassistant_root is used when env is absent."""
        from kbutillib.researchos.config import resolve_aiassistant_root, set_root

        monkeypatch.delenv("AIASSISTANT_ROOT", raising=False)
        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        ai_root = tmp_path / "my-ai"
        ai_root.mkdir()
        set_root(aiassistant_root=str(ai_root), config_file=cfg_path)

        result = resolve_aiassistant_root(config_file=cfg_path)
        assert result == ai_root.resolve()

    def test_aiassistant_root_default(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default aiassistant_root is used when nothing is configured."""
        from kbutillib.researchos.config import resolve_aiassistant_root

        monkeypatch.delenv("AIASSISTANT_ROOT", raising=False)
        cfg_path = tmp_home / ".kbutillib" / "config.yaml"

        result = resolve_aiassistant_root(config_file=cfg_path)
        assert "AIAssistant" in str(result)


# ===========================================================================
# Tests — set_root round-trip
# ===========================================================================


class TestSetRoot:
    def test_set_root_persists_root(
        self, tmp_path: Path, tmp_home: Path
    ) -> None:
        """set_root persists researchos.root."""
        from kbutillib.researchos.config import set_root

        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        root_path = tmp_path / "my-ros"
        root_path.mkdir()

        set_root(root=str(root_path), config_file=cfg_path)

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert data["researchos"]["root"] == str(root_path.resolve())

    def test_set_root_persists_tooling_venv(
        self, tmp_path: Path, tmp_home: Path
    ) -> None:
        """set_root persists researchos.tooling_venv."""
        from kbutillib.researchos.config import set_root

        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        venv_path = tmp_path / "my-venv"
        venv_path.mkdir()

        set_root(tooling_venv=str(venv_path), config_file=cfg_path)

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert data["researchos"]["tooling_venv"] == str(venv_path.resolve())

    def test_set_root_persists_aiassistant_root(
        self, tmp_path: Path, tmp_home: Path
    ) -> None:
        """set_root persists researchos.aiassistant_root."""
        from kbutillib.researchos.config import set_root

        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        ai_path = tmp_path / "my-ai"
        ai_path.mkdir()

        set_root(aiassistant_root=str(ai_path), config_file=cfg_path)

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert data["researchos"]["aiassistant_root"] == str(ai_path.resolve())

    def test_set_root_creates_parent_dir(self, tmp_home: Path) -> None:
        """set_root creates ~/.kbutillib/ if it does not exist."""
        from kbutillib.researchos.config import set_root

        cfg_dir = tmp_home / ".kbutillib"
        assert not cfg_dir.exists()

        cfg_path = cfg_dir / "config.yaml"
        set_root(root="/tmp/ros", config_file=cfg_path)

        assert cfg_dir.is_dir()
        assert cfg_path.is_file()

    def test_set_root_preserves_other_sections(
        self, tmp_path: Path, tmp_home: Path
    ) -> None:
        """set_root preserves other config sections."""
        from kbutillib.researchos.config import set_root

        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("beril:\n  root: /some/beril\n", encoding="utf-8")

        set_root(root=str(tmp_path), config_file=cfg_path)

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert data.get("beril", {}).get("root") == "/some/beril"
        assert "researchos" in data

    def test_set_root_raises_when_no_args(self, tmp_home: Path) -> None:
        """set_root raises ValueError when called with no arguments."""
        from kbutillib.researchos.config import set_root

        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        with pytest.raises(ValueError, match="At least one"):
            set_root(config_file=cfg_path)

    def test_set_root_expands_tilde(self, tmp_home: Path) -> None:
        """set_root expands ~ in paths before storing."""
        from kbutillib.researchos.config import set_root

        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        set_root(root="~/ros-test", config_file=cfg_path)

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        stored = data["researchos"]["root"]
        assert not stored.startswith("~"), f"~ not expanded: {stored!r}"

    def test_set_root_all_three(self, tmp_path: Path, tmp_home: Path) -> None:
        """set_root writes all three keys when all are given."""
        from kbutillib.researchos.config import set_root

        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        set_root(
            root=str(tmp_path / "ros"),
            tooling_venv=str(tmp_path / "venv"),
            aiassistant_root=str(tmp_path / "ai"),
            config_file=cfg_path,
        )

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert "root" in data["researchos"]
        assert "tooling_venv" in data["researchos"]
        assert "aiassistant_root" in data["researchos"]


# ===========================================================================
# Tests — tooling.py
# ===========================================================================


class TestEnsureResearchOSBinary:
    def test_returns_existing_binary_without_reinstalling(
        self, tmp_path: Path
    ) -> None:
        """Returns the binary path immediately when it already exists."""
        from kbutillib.researchos.tooling import ensure_research_os_binary

        venv = tmp_path / "venv"
        bin_dir = venv / "bin"
        bin_dir.mkdir(parents=True)
        bin_path = bin_dir / "research-os"
        bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
        bin_path.chmod(0o755)

        with patch("subprocess.run") as mock_run:
            result = ensure_research_os_binary(venv)
            mock_run.assert_not_called()

        assert result == bin_path

    def test_creates_venv_and_installs_when_absent(
        self, tmp_path: Path
    ) -> None:
        """Creates venv and pip-installs when binary is missing and create=True."""
        from kbutillib.researchos.tooling import ensure_research_os_binary

        venv = tmp_path / "venv"
        bin_dir = venv / "bin"

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            # Simulate venv creation and pip install creating the binary
            if "-m" in str(cmd) and "venv" in " ".join(str(c) for c in cmd):
                bin_dir.mkdir(parents=True, exist_ok=True)
            elif "install" in " ".join(str(c) for c in cmd):
                bin_path = bin_dir / "research-os"
                bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
                bin_path.chmod(0o755)
            return r

        with patch("subprocess.run", side_effect=fake_run):
            result = ensure_research_os_binary(venv, create=True)

        assert result == venv / "bin" / "research-os"

    def test_raises_when_create_false_and_missing(self, tmp_path: Path) -> None:
        """Raises RuntimeError when binary is missing and create=False."""
        from kbutillib.researchos.tooling import ensure_research_os_binary

        venv = tmp_path / "venv"

        with pytest.raises(RuntimeError, match="research-os binary not found"):
            ensure_research_os_binary(venv, create=False)

    def test_raises_when_venv_creation_fails(self, tmp_path: Path) -> None:
        """Raises RuntimeError when venv creation fails."""
        from kbutillib.researchos.tooling import ensure_research_os_binary

        venv = tmp_path / "venv"

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 1
            r.stderr = "venv failed"
            r.stdout = ""
            return r

        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(RuntimeError, match="Failed to create tooling venv"):
                ensure_research_os_binary(venv, create=True)

    def test_raises_when_pip_fails(self, tmp_path: Path) -> None:
        """Raises RuntimeError when pip install fails."""
        from kbutillib.researchos.tooling import ensure_research_os_binary

        venv = tmp_path / "venv"
        bin_dir = venv / "bin"

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            cmd_str = " ".join(str(c) for c in cmd)
            if "-m" in cmd_str and "venv" in cmd_str:
                r.returncode = 0
                bin_dir.mkdir(parents=True, exist_ok=True)
            elif "install" in cmd_str:
                r.returncode = 1
                r.stderr = "pip failed"
            else:
                r.returncode = 0
            r.stdout = ""
            r.stderr = r.stderr if hasattr(r, "stderr") else ""
            return r

        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(RuntimeError, match="pip install research-os failed"):
                ensure_research_os_binary(venv, create=True)


# ===========================================================================
# Tests — registry.py
# ===========================================================================


class TestSlug:
    def test_slug_lowercases(self) -> None:
        from kbutillib.researchos.registry import _slug

        assert _slug("AIALE") == "aiale"

    def test_slug_replaces_non_alphanumeric_with_hyphen(self) -> None:
        from kbutillib.researchos.registry import _slug

        assert _slug("My Study 2024!") == "my-study-2024"

    def test_slug_strips_leading_trailing_hyphens(self) -> None:
        from kbutillib.researchos.registry import _slug

        assert _slug("!hello!") == "hello"

    def test_slug_compresses_runs(self) -> None:
        from kbutillib.researchos.registry import _slug

        assert _slug("a---b") == "a-b"

    def test_slug_alphanumeric_unchanged(self) -> None:
        from kbutillib.researchos.registry import _slug

        assert _slug("abc123") == "abc123"

    def test_slug_real_example(self) -> None:
        from kbutillib.researchos.registry import _slug

        assert _slug("RoboticLabManuscript") == "roboticlabmanuscript"


class TestRegisterProject:
    def test_returns_ok_on_rc_zero(
        self, tmp_path: Path, aiassistant_root: Path
    ) -> None:
        """register_project returns ok result when subprocess exits 0."""
        from kbutillib.researchos.registry import register_project

        project_path = tmp_path / "project"
        project_path.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="", stderr=""
            )
            result = register_project(
                "AIALE", "RoboticLabManuscript", project_path,
                aiassistant_root=aiassistant_root,
            )

        assert result.status == "ok"

    def test_returns_failed_on_rc_nonzero(
        self, tmp_path: Path, aiassistant_root: Path
    ) -> None:
        """register_project returns failed result (no raise) when subprocess exits non-zero."""
        from kbutillib.researchos.registry import register_project

        project_path = tmp_path / "project"
        project_path.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="import error"
            )
            result = register_project(
                "AIALE", "MyStudy", project_path,
                aiassistant_root=aiassistant_root,
            )

        assert result.status == "failed"
        assert "import error" in result.message or "rc=1" in result.message

    def test_returns_failed_when_src_missing(self, tmp_path: Path) -> None:
        """register_project returns failed when aiassistant_root/src is missing."""
        from kbutillib.researchos.registry import register_project

        ai_root = tmp_path / "AIAssistant"
        ai_root.mkdir()
        # No src/ directory
        project_path = tmp_path / "project"
        project_path.mkdir()

        with patch("subprocess.run") as mock_run:
            result = register_project(
                "AIALE", "MyStudy", project_path,
                aiassistant_root=ai_root,
            )
            mock_run.assert_not_called()

        assert result.status == "failed"
        assert "src" in result.message

    def test_subprocess_uses_correct_path_ids(
        self, tmp_path: Path, aiassistant_root: Path
    ) -> None:
        """register_project builds script with correct parent_id and study_id."""
        from kbutillib.researchos.registry import register_project

        project_path = tmp_path / "ResearchOS" / "AIALE" / "RoboticLabManuscript"
        project_path.mkdir(parents=True)

        captured_script = []

        def fake_run(cmd, **kwargs):
            if len(cmd) >= 3 and cmd[1] == "-c":
                captured_script.append(cmd[2])
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            result = register_project(
                "AIALE", "RoboticLabManuscript", project_path,
                aiassistant_root=aiassistant_root,
            )

        assert result.status == "ok"
        assert len(captured_script) == 1
        script = captured_script[0]

        # Check that the script uses the correct IDs
        assert "'aiale'" in script
        assert "'aiale-roboticlabmanuscript'" in script
        assert str(aiassistant_root / "src") in script

    def test_returns_skipped_when_already_registered(
        self, tmp_path: Path, aiassistant_root: Path
    ) -> None:
        """register_project returns skipped when subprocess reports skip."""
        from kbutillib.researchos.registry import register_project

        project_path = tmp_path / "project"
        project_path.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="skip: aiale-mystudy already registered",
                stderr="",
            )
            result = register_project(
                "AIALE", "MyStudy", project_path,
                aiassistant_root=aiassistant_root,
            )

        assert result.status == "skipped"

    def test_does_not_raise_on_subprocess_exception(
        self, tmp_path: Path, aiassistant_root: Path
    ) -> None:
        """register_project never raises — returns failed instead."""
        from kbutillib.researchos.registry import register_project

        project_path = tmp_path / "project"
        project_path.mkdir()

        with patch("subprocess.run", side_effect=OSError("file not found")):
            result = register_project(
                "AIALE", "MyStudy", project_path,
                aiassistant_root=aiassistant_root,
            )

        assert result.status == "failed"
        assert "subprocess raised" in result.message


# ===========================================================================
# Tests — manager.py
# ===========================================================================


class TestValidateName:
    def test_valid_names_accepted(self) -> None:
        from kbutillib.researchos.manager import _validate_name

        valid = ["AIALE", "my-study", "Study_2024", "a.b", "ABC123"]
        for name in valid:
            _validate_name(name)  # should not raise

    def test_slash_rejected(self) -> None:
        from kbutillib.researchos.manager import _validate_name

        with pytest.raises(ValueError, match="Invalid name"):
            _validate_name("foo/bar")

    def test_space_rejected(self) -> None:
        from kbutillib.researchos.manager import _validate_name

        with pytest.raises(ValueError, match="Invalid name"):
            _validate_name("foo bar")

    def test_at_sign_rejected(self) -> None:
        from kbutillib.researchos.manager import _validate_name

        with pytest.raises(ValueError, match="Invalid name"):
            _validate_name("foo@bar")

    def test_empty_rejected(self) -> None:
        from kbutillib.researchos.manager import _validate_name

        with pytest.raises(ValueError, match="Invalid name"):
            _validate_name("")


class TestPathComposition:
    def test_nested_path_structure(
        self, manager, ros_root: Path
    ) -> None:
        """Verifies <root>/<parent>/<name> nesting in _run_init cwd."""
        captured_cwd = []
        captured_name_arg = []

        parent_path = ros_root / "AIALE"
        parent_path.mkdir(parents=True, exist_ok=True)

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            cmd_list = list(cmd)
            if "init" in cmd_list:
                captured_cwd.append(kwargs.get("cwd"))
                idx = cmd_list.index("init")
                captured_name_arg.append(cmd_list[idx + 1])
            return r

        with patch("subprocess.run", side_effect=fake_run):
            manager._run_init(
                name="MyStudy",
                parent_path=parent_path,
                research_os_bin=Path("/fake/bin/research-os"),
                display_name=None,
                domain=None,
                questions=None,
                workspace_mode="analysis",
                ide="cursor,claude",
                force=False,
            )

        assert len(captured_cwd) == 1
        assert captured_cwd[0] == str(parent_path)
        assert captured_name_arg[0] == "MyStudy"


class TestRewriteMcpCommands:
    def test_rewrites_all_present_mcp_files(
        self, tmp_path: Path
    ) -> None:
        """Rewrites command field in all three MCP files when present."""
        from kbutillib.researchos.manager import ResearchOSProject

        project_path = tmp_path / "project"
        project_path.mkdir()

        original_data = {
            "mcpServers": {
                "research-os": {
                    "command": "research-os",
                    "args": ["serve"],
                    "env": {"KEY": "val"},
                }
            }
        }

        (project_path / ".mcp.json").write_text(
            json.dumps(original_data), encoding="utf-8"
        )
        (project_path / ".cursor").mkdir()
        (project_path / ".cursor" / "mcp.json").write_text(
            json.dumps(original_data), encoding="utf-8"
        )
        (project_path / ".claude").mkdir()
        (project_path / ".claude" / "mcp.json").write_text(
            json.dumps(original_data), encoding="utf-8"
        )

        mgr = ResearchOSProject(
            researchos_root=tmp_path,
            tooling_venv=tmp_path / "venv",
            aiassistant_root=tmp_path / "ai",
        )
        abs_bin = Path("/fake/venv/bin/research-os")
        mgr._rewrite_mcp_commands(project_path, abs_bin)

        for mcp_file in [
            project_path / ".mcp.json",
            project_path / ".cursor" / "mcp.json",
            project_path / ".claude" / "mcp.json",
        ]:
            data = json.loads(mcp_file.read_text(encoding="utf-8"))
            cmd = data["mcpServers"]["research-os"]["command"]
            assert cmd == str(abs_bin), f"Expected {abs_bin}, got {cmd} in {mcp_file}"
            # args and env must be preserved
            assert data["mcpServers"]["research-os"]["args"] == ["serve"]
            assert data["mcpServers"]["research-os"]["env"] == {"KEY": "val"}

    def test_skips_missing_mcp_files(self, tmp_path: Path) -> None:
        """Missing MCP files are silently skipped (no error)."""
        from kbutillib.researchos.manager import ResearchOSProject

        project_path = tmp_path / "project"
        project_path.mkdir()
        # No MCP files created

        mgr = ResearchOSProject(
            researchos_root=tmp_path,
            tooling_venv=tmp_path / "venv",
            aiassistant_root=tmp_path / "ai",
        )
        mgr._rewrite_mcp_commands(project_path, Path("/fake/bin/research-os"))
        # Should not raise

    def test_leaves_args_and_env_intact(self, tmp_path: Path) -> None:
        """Rewrite only changes command field, not args or env."""
        from kbutillib.researchos.manager import ResearchOSProject

        project_path = tmp_path / "project"
        project_path.mkdir()

        data = {
            "mcpServers": {
                "research-os": {
                    "command": "research-os",
                    "args": ["--port", "3000"],
                    "env": {"FOO": "bar", "BAZ": "qux"},
                }
            }
        }
        (project_path / ".mcp.json").write_text(json.dumps(data), encoding="utf-8")

        mgr = ResearchOSProject(
            researchos_root=tmp_path,
            tooling_venv=tmp_path / "venv",
            aiassistant_root=tmp_path / "ai",
        )
        mgr._rewrite_mcp_commands(project_path, Path("/abs/research-os"))

        result = json.loads((project_path / ".mcp.json").read_text(encoding="utf-8"))
        ros = result["mcpServers"]["research-os"]
        assert ros["command"] == "/abs/research-os"
        assert ros["args"] == ["--port", "3000"]
        assert ros["env"] == {"FOO": "bar", "BAZ": "qux"}


class TestWriteWorkspace:
    def test_workspace_valid_json(self, tmp_path: Path) -> None:
        """Workspace file is valid JSON."""
        from kbutillib.researchos.manager import ResearchOSProject

        project_path = tmp_path / "project"
        project_path.mkdir()

        mgr = ResearchOSProject(
            researchos_root=tmp_path,
            tooling_venv=tmp_path / "venv",
            aiassistant_root=tmp_path / "ai",
        )
        mgr._write_workspace(project_path, "MyStudy")

        ws_file = project_path / "MyStudy.code-workspace"
        assert ws_file.is_file()
        data = json.loads(ws_file.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    def test_workspace_folder_name_and_path(self, tmp_path: Path) -> None:
        """Workspace folder has correct name and path='.'."""
        from kbutillib.researchos.manager import ResearchOSProject

        project_path = tmp_path / "project"
        project_path.mkdir()

        mgr = ResearchOSProject(
            researchos_root=tmp_path,
            tooling_venv=tmp_path / "venv",
            aiassistant_root=tmp_path / "ai",
        )
        mgr._write_workspace(project_path, "MyStudy")

        data = json.loads((project_path / "MyStudy.code-workspace").read_text())
        assert len(data["folders"]) == 1
        folder = data["folders"][0]
        assert folder["name"] == "ResearchOS: MyStudy"
        assert folder["path"] == "."

    def test_workspace_extensions_include_claude_code(self, tmp_path: Path) -> None:
        """Workspace extensions include anthropic.claude-code."""
        from kbutillib.researchos.manager import ResearchOSProject

        project_path = tmp_path / "project"
        project_path.mkdir()

        mgr = ResearchOSProject(
            researchos_root=tmp_path,
            tooling_venv=tmp_path / "venv",
            aiassistant_root=tmp_path / "ai",
        )
        mgr._write_workspace(project_path, "MyStudy")

        data = json.loads((project_path / "MyStudy.code-workspace").read_text())
        recs = data.get("extensions", {}).get("recommendations", [])
        assert "anthropic.claude-code" in recs

    def test_workspace_has_empty_settings(self, tmp_path: Path) -> None:
        """Workspace has empty settings dict."""
        from kbutillib.researchos.manager import ResearchOSProject

        project_path = tmp_path / "project"
        project_path.mkdir()

        mgr = ResearchOSProject(
            researchos_root=tmp_path,
            tooling_venv=tmp_path / "venv",
            aiassistant_root=tmp_path / "ai",
        )
        mgr._write_workspace(project_path, "MyStudy")

        data = json.loads((project_path / "MyStudy.code-workspace").read_text())
        assert data["settings"] == {}


class TestNewOrchestration:
    def _make_manager_and_fake_run(
        self, ros_root: Path, tooling_venv: Path, aiassistant_root: Path, **kwargs
    ):
        from kbutillib.researchos.manager import ResearchOSProject

        fake_run, calls = _make_fake_run(
            ros_root=ros_root,
            create_project_dir=True,
            **kwargs
        )
        mgr = ResearchOSProject(
            researchos_root=ros_root,
            tooling_venv=tooling_venv,
            aiassistant_root=aiassistant_root,
        )
        # Create a fake binary in the tooling venv
        bin_dir = tooling_venv / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        bin_path = bin_dir / "research-os"
        bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
        bin_path.chmod(0o755)

        return mgr, fake_run, calls

    def test_new_creates_project_at_correct_path(
        self,
        ros_root: Path,
        tooling_venv: Path,
        aiassistant_root: Path,
    ) -> None:
        """new() creates project at <root>/<parent>/<name>/."""
        mgr, fake_run, _ = self._make_manager_and_fake_run(
            ros_root, tooling_venv, aiassistant_root
        )

        with patch("subprocess.run", side_effect=fake_run):
            result = mgr.new("AIALE", "MyStudy")

        assert result == ros_root / "AIALE" / "MyStudy"

    def test_new_raises_on_existing_nonempty_dir_without_force(
        self,
        ros_root: Path,
        tooling_venv: Path,
        aiassistant_root: Path,
    ) -> None:
        """new() raises RuntimeError when project dir exists and is non-empty without force."""
        from kbutillib.researchos.manager import ResearchOSProject

        mgr = ResearchOSProject(
            researchos_root=ros_root,
            tooling_venv=tooling_venv,
            aiassistant_root=aiassistant_root,
        )
        # Pre-create a non-empty project directory
        project_path = ros_root / "AIALE" / "MyStudy"
        project_path.mkdir(parents=True)
        (project_path / "existing.txt").write_text("content")

        with pytest.raises(RuntimeError, match="non-empty"):
            mgr.new("AIALE", "MyStudy")

    def test_new_warns_and_continues_on_git_failure(
        self,
        ros_root: Path,
        tooling_venv: Path,
        aiassistant_root: Path,
    ) -> None:
        """new() warns and continues when git commit step fails."""
        mgr, fake_run, _ = self._make_manager_and_fake_run(
            ros_root, tooling_venv, aiassistant_root, git_rc=1
        )

        with patch("subprocess.run", side_effect=fake_run):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                result = mgr.new("AIALE", "GitFail")

        # Should still return the project path (not raise)
        assert result == ros_root / "AIALE" / "GitFail"
        # Warning should have been issued
        warn_msgs = [str(w.message) for w in caught]
        assert any("git" in m.lower() for m in warn_msgs)

    def test_new_warns_and_continues_on_registry_failure(
        self,
        ros_root: Path,
        tooling_venv: Path,
        aiassistant_root: Path,
    ) -> None:
        """new() warns and continues when registry subprocess fails."""
        mgr, fake_run, _ = self._make_manager_and_fake_run(
            ros_root, tooling_venv, aiassistant_root, registry_rc=1
        )

        with patch("subprocess.run", side_effect=fake_run):
            result = mgr.new("AIALE", "RegFail")

        # Should return the project path (registry failure is best-effort)
        assert result == ros_root / "AIALE" / "RegFail"

    def test_new_validates_parent_name(
        self,
        ros_root: Path,
        tooling_venv: Path,
        aiassistant_root: Path,
    ) -> None:
        """new() raises ValueError for invalid parent name."""
        from kbutillib.researchos.manager import ResearchOSProject

        mgr = ResearchOSProject(
            researchos_root=ros_root,
            tooling_venv=tooling_venv,
            aiassistant_root=aiassistant_root,
        )
        with pytest.raises(ValueError, match="Invalid name"):
            mgr.new("bad/parent", "MyStudy")

    def test_new_validates_study_name(
        self,
        ros_root: Path,
        tooling_venv: Path,
        aiassistant_root: Path,
    ) -> None:
        """new() raises ValueError for invalid study name."""
        from kbutillib.researchos.manager import ResearchOSProject

        mgr = ResearchOSProject(
            researchos_root=ros_root,
            tooling_venv=tooling_venv,
            aiassistant_root=aiassistant_root,
        )
        with pytest.raises(ValueError, match="Invalid name"):
            mgr.new("AIALE", "bad study!")

    def test_new_init_cwd_is_parent_path(
        self,
        ros_root: Path,
        tooling_venv: Path,
        aiassistant_root: Path,
    ) -> None:
        """_run_init is called with cwd=<root>/<parent>."""
        captured_cwd = []

        # Create tooling bin
        _seed_tooling_venv(tooling_venv)

        from kbutillib.researchos.manager import ResearchOSProject

        mgr = ResearchOSProject(
            researchos_root=ros_root,
            tooling_venv=tooling_venv,
            aiassistant_root=aiassistant_root,
        )

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            cmd_list = list(cmd)
            cmd_str = " ".join(str(c) for c in cmd_list)
            # Match research-os init call (not git init)
            if "research-os" in cmd_str and "init" in cmd_list:
                captured_cwd.append(kwargs.get("cwd"))
                # Create project dir structure so new() can continue
                cwd = kwargs.get("cwd")
                idx = cmd_list.index("init")
                proj_name = cmd_list[idx + 1]
                proj_path = Path(cwd) / proj_name
                (proj_path / ".os_state").mkdir(parents=True, exist_ok=True)
            return r

        with patch("subprocess.run", side_effect=fake_run):
            mgr.new("AIALE", "CwdTest")

        assert len(captured_cwd) >= 1
        assert captured_cwd[0] == str(ros_root / "AIALE")


class TestList:
    def test_list_returns_projects_with_os_state(
        self,
        ros_root: Path,
        tooling_venv: Path,
        aiassistant_root: Path,
    ) -> None:
        """list() returns projects containing .os_state/."""
        from kbutillib.researchos.manager import ResearchOSProject

        mgr = ResearchOSProject(
            researchos_root=ros_root,
            tooling_venv=tooling_venv,
            aiassistant_root=aiassistant_root,
        )

        # Create two projects under AIALE with .os_state
        for name in ["StudyA", "StudyB"]:
            p = ros_root / "AIALE" / name
            (p / ".os_state").mkdir(parents=True, exist_ok=True)

        # Create a directory without .os_state (should be excluded)
        not_a_project = ros_root / "AIALE" / "NotAProject"
        not_a_project.mkdir(parents=True)

        results = mgr.list()
        names = [(r.parent, r.name) for r in results]
        assert ("AIALE", "StudyA") in names
        assert ("AIALE", "StudyB") in names
        assert ("AIALE", "NotAProject") not in names

    def test_list_sorted_by_parent_then_name(
        self,
        ros_root: Path,
        tooling_venv: Path,
        aiassistant_root: Path,
    ) -> None:
        """list() returns projects sorted by (parent, name)."""
        from kbutillib.researchos.manager import ResearchOSProject

        mgr = ResearchOSProject(
            researchos_root=ros_root,
            tooling_venv=tooling_venv,
            aiassistant_root=aiassistant_root,
        )

        for parent, name in [("ZZZ", "StudyA"), ("AAA", "StudyC"), ("AAA", "StudyB")]:
            p = ros_root / parent / name
            (p / ".os_state").mkdir(parents=True, exist_ok=True)

        results = mgr.list()
        keys = [(r.parent, r.name) for r in results]
        assert keys == sorted(keys)

    def test_list_has_workspace_true_when_code_workspace_present(
        self,
        ros_root: Path,
        tooling_venv: Path,
        aiassistant_root: Path,
    ) -> None:
        """list() sets has_workspace=True when .code-workspace is present."""
        from kbutillib.researchos.manager import ResearchOSProject

        mgr = ResearchOSProject(
            researchos_root=ros_root,
            tooling_venv=tooling_venv,
            aiassistant_root=aiassistant_root,
        )

        p = ros_root / "AIALE" / "WithWs"
        (p / ".os_state").mkdir(parents=True, exist_ok=True)
        (p / "WithWs.code-workspace").write_text("{}", encoding="utf-8")

        results = mgr.list()
        entry = next(r for r in results if r.name == "WithWs")
        assert entry.has_workspace is True

    def test_list_has_workspace_false_when_no_code_workspace(
        self,
        ros_root: Path,
        tooling_venv: Path,
        aiassistant_root: Path,
    ) -> None:
        """list() sets has_workspace=False when no .code-workspace present."""
        from kbutillib.researchos.manager import ResearchOSProject

        mgr = ResearchOSProject(
            researchos_root=ros_root,
            tooling_venv=tooling_venv,
            aiassistant_root=aiassistant_root,
        )

        p = ros_root / "AIALE" / "NoWs"
        (p / ".os_state").mkdir(parents=True, exist_ok=True)
        # No .code-workspace file

        results = mgr.list()
        entry = next(r for r in results if r.name == "NoWs")
        assert entry.has_workspace is False

    def test_list_empty_when_root_not_exists(
        self,
        tmp_path: Path,
        tooling_venv: Path,
        aiassistant_root: Path,
    ) -> None:
        """list() returns [] when researchos_root does not exist."""
        from kbutillib.researchos.manager import ResearchOSProject

        mgr = ResearchOSProject(
            researchos_root=tmp_path / "nonexistent",
            tooling_venv=tooling_venv,
            aiassistant_root=aiassistant_root,
        )

        assert mgr.list() == []

    def test_list_two_level_walk(
        self,
        ros_root: Path,
        tooling_venv: Path,
        aiassistant_root: Path,
    ) -> None:
        """list() walks exactly two levels and does not recurse deeper."""
        from kbutillib.researchos.manager import ResearchOSProject

        mgr = ResearchOSProject(
            researchos_root=ros_root,
            tooling_venv=tooling_venv,
            aiassistant_root=aiassistant_root,
        )

        # A nested study (3 levels deep) should NOT appear
        deep = ros_root / "AIALE" / "StudyA" / "SubStudy"
        (deep / ".os_state").mkdir(parents=True, exist_ok=True)
        # Also create a valid two-level study
        valid = ros_root / "AIALE" / "StudyA"
        (valid / ".os_state").mkdir(parents=True, exist_ok=True)

        results = mgr.list()
        names = [r.name for r in results]
        assert "StudyA" in names
        assert "SubStudy" not in names


class TestOpen:
    def test_open_returns_path_when_exists(
        self,
        ros_root: Path,
        tooling_venv: Path,
        aiassistant_root: Path,
    ) -> None:
        """open() returns the project path when it exists."""
        from kbutillib.researchos.manager import ResearchOSProject

        mgr = ResearchOSProject(
            researchos_root=ros_root,
            tooling_venv=tooling_venv,
            aiassistant_root=aiassistant_root,
        )
        project_path = ros_root / "AIALE" / "MyStudy"
        project_path.mkdir(parents=True)

        result = mgr.open("AIALE", "MyStudy")
        assert result == project_path

    def test_open_raises_when_not_exists(
        self,
        ros_root: Path,
        tooling_venv: Path,
        aiassistant_root: Path,
    ) -> None:
        """open() raises RuntimeError directing to new() when project not found."""
        from kbutillib.researchos.manager import ResearchOSProject

        mgr = ResearchOSProject(
            researchos_root=ros_root,
            tooling_venv=tooling_venv,
            aiassistant_root=aiassistant_root,
        )

        with pytest.raises(RuntimeError, match="kbu researchos new"):
            mgr.open("AIALE", "NoSuchStudy")


# ===========================================================================
# Tests — CLI
# ===========================================================================


class _CliInvoker:
    """Helper for invoking researchos CLI with standard group options."""

    def __init__(
        self,
        ros_root: Path,
        tooling_venv: Path,
        aiassistant_root: Path,
    ):
        self.ros_root = ros_root
        self.tooling_venv = tooling_venv
        self.aiassistant_root = aiassistant_root
        self.runner = CliRunner()

    def invoke(self, args: list, **kwargs):
        full_args = [
            "--root", str(self.ros_root),
            "--tooling-venv", str(self.tooling_venv),
            "--aiassistant-root", str(self.aiassistant_root),
        ] + args
        return self.runner.invoke(researchos_cmd, full_args, **kwargs)


@pytest.fixture()
def cli(ros_root: Path, tooling_venv: Path, aiassistant_root: Path) -> _CliInvoker:
    return _CliInvoker(ros_root, tooling_venv, aiassistant_root)


class TestCLIHelp:
    def test_researchos_help_lists_subcommands(self) -> None:
        """kbu researchos --help lists all subcommands."""
        runner = CliRunner()
        result = runner.invoke(researchos_cmd, ["--help"])
        assert result.exit_code == 0, result.output
        for cmd in ("new", "open", "ls", "set-root"):
            assert cmd in result.output

    def test_new_help_shows_flags(self) -> None:
        """kbu researchos new --help shows documented flags."""
        runner = CliRunner()
        result = runner.invoke(researchos_cmd, ["new", "--help"])
        assert result.exit_code == 0, result.output
        assert "PARENT" in result.output
        assert "NAME" in result.output
        for flag in ("--domain", "--question", "--workspace-mode", "--open", "--force"):
            assert flag in result.output

    def test_open_help_shows_positionals(self) -> None:
        """kbu researchos open --help shows PARENT and NAME positionals."""
        runner = CliRunner()
        result = runner.invoke(researchos_cmd, ["open", "--help"])
        assert result.exit_code == 0, result.output
        assert "PARENT" in result.output
        assert "NAME" in result.output


def _seed_tooling_venv(tooling_venv: Path) -> Path:
    """Create a fake research-os binary in the tooling venv."""
    bin_dir = tooling_venv / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    bin_path = bin_dir / "research-os"
    bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
    bin_path.chmod(0o755)
    return bin_path


class TestCLINew:
    def test_new_succeeds_with_mocked_subprocess(
        self,
        cli: _CliInvoker,
        ros_root: Path,
        tooling_venv: Path,
    ) -> None:
        """new PARENT NAME creates project at correct path."""
        _seed_tooling_venv(tooling_venv)
        fake_run, _ = _make_fake_run(ros_root=ros_root, create_project_dir=True)

        with patch("subprocess.run", side_effect=fake_run):
            result = cli.invoke(["new", "AIALE", "MyStudy"])

        assert result.exit_code == 0, result.output
        assert "MyStudy" in result.output or str(ros_root / "AIALE" / "MyStudy") in result.output

    def test_new_invalid_parent_rejected(self, cli: _CliInvoker) -> None:
        """new with invalid parent exits non-zero."""
        result = cli.invoke(["new", "bad/parent", "MyStudy"])
        assert result.exit_code != 0

    def test_new_invalid_name_rejected(self, cli: _CliInvoker) -> None:
        """new with invalid study name exits non-zero."""
        result = cli.invoke(["new", "AIALE", "bad study!"])
        assert result.exit_code != 0

    def test_new_open_cursor_not_on_path(
        self, cli: _CliInvoker, ros_root: Path, tooling_venv: Path,
        monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """new --open prints manual instruction when cursor is not on PATH."""
        _seed_tooling_venv(tooling_venv)
        fake_run, _ = _make_fake_run(ros_root=ros_root, create_project_dir=True)

        with patch("subprocess.run", side_effect=fake_run):
            with patch("kbutillib.cli.researchos.shutil.which", return_value=None):
                result = cli.invoke(["new", "--open", "AIALE", "OpenTest"])

        assert result.exit_code == 0, result.output
        assert "cursor is not on PATH" in result.output or "manually" in result.output.lower()

    def test_new_git_failure_continues(
        self, cli: _CliInvoker, ros_root: Path, tooling_venv: Path
    ) -> None:
        """new continues and exits 0 even when git fails."""
        _seed_tooling_venv(tooling_venv)
        fake_run, _ = _make_fake_run(
            ros_root=ros_root, create_project_dir=True, git_rc=1
        )

        with patch("subprocess.run", side_effect=fake_run):
            result = cli.invoke(["new", "AIALE", "GitFail"])

        # Should exit 0 (warn and continue)
        assert result.exit_code == 0, result.output


class TestCLIOpen:
    def test_open_opens_cursor_when_project_exists(
        self, cli: _CliInvoker, ros_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """open PARENT NAME invokes cursor when project exists."""
        project_path = ros_root / "AIALE" / "MyStudy"
        project_path.mkdir(parents=True)
        ws_file = project_path / "MyStudy.code-workspace"
        ws_file.write_text("{}", encoding="utf-8")

        opened = []
        monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/cursor" if n == "cursor" else None)
        monkeypatch.setattr(
            "kbutillib.cli.researchos.subprocess.Popen",
            lambda cmd, **kw: opened.append(cmd),
        )

        result = cli.invoke(["open", "AIALE", "MyStudy"])
        assert result.exit_code == 0, result.output
        assert len(opened) == 1

    def test_open_errors_when_project_not_found(self, cli: _CliInvoker) -> None:
        """open PARENT NAME exits non-zero when project does not exist."""
        result = cli.invoke(["open", "AIALE", "NoSuchStudy"])
        assert result.exit_code != 0


class TestCLILs:
    def test_ls_empty_message(self, cli: _CliInvoker) -> None:
        """ls shows (no Research-OS projects found) when root is empty."""
        result = cli.invoke(["ls"])
        assert result.exit_code == 0, result.output
        assert "no Research-OS projects" in result.output or "(none" in result.output

    def test_ls_human_readable_groups_by_parent(
        self, cli: _CliInvoker, ros_root: Path
    ) -> None:
        """ls groups projects under ── <parent> header."""
        (ros_root / "AIALE" / "StudyA" / ".os_state").mkdir(parents=True, exist_ok=True)
        (ros_root / "AIALE" / "StudyB" / ".os_state").mkdir(parents=True, exist_ok=True)

        result = cli.invoke(["ls"])
        assert result.exit_code == 0, result.output
        assert "── AIALE" in result.output
        assert "StudyA" in result.output
        assert "StudyB" in result.output

    def test_ls_json_output(self, cli: _CliInvoker, ros_root: Path) -> None:
        """ls --json emits valid JSON array."""
        (ros_root / "AIALE" / "StudyA" / ".os_state").mkdir(parents=True, exist_ok=True)

        result = cli.invoke(["ls", "--json"])
        assert result.exit_code == 0, result.output

        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert set(data[0].keys()) == {"parent", "name", "path", "has_workspace"}

    def test_ls_json_sorted(self, cli: _CliInvoker, ros_root: Path) -> None:
        """ls --json output is sorted by (parent, name)."""
        for parent, name in [("ZZZ", "StudyA"), ("AAA", "StudyC"), ("AAA", "StudyB")]:
            (ros_root / parent / name / ".os_state").mkdir(parents=True, exist_ok=True)

        result = cli.invoke(["ls", "--json"])
        assert result.exit_code == 0, result.output

        data = json.loads(result.output)
        keys = [(d["parent"], d["name"]) for d in data]
        assert keys == sorted(keys)


class TestCLISetRoot:
    def test_set_root_persists_with_root_option(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """set-root --root persists researchos.root."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setattr(
            "kbutillib.researchos.config._DEFAULT_CONFIG_FILE",
            fake_home / ".kbutillib" / "config.yaml",
        )

        ros_root = tmp_path / "ros"
        ros_root.mkdir()

        runner = CliRunner()
        result = runner.invoke(
            researchos_cmd,
            ["--root", str(ros_root), "set-root"],
        )
        assert result.exit_code == 0, result.output

        cfg_path = fake_home / ".kbutillib" / "config.yaml"
        assert cfg_path.is_file()
        data = yaml.safe_load(cfg_path.read_text())
        assert "root" in data.get("researchos", {})

    def test_set_root_requires_at_least_one_option(self) -> None:
        """set-root with no options fails with UsageError."""
        runner = CliRunner()
        result = runner.invoke(researchos_cmd, ["set-root"])
        assert result.exit_code != 0
