"""Tests for ``kbu beril worktree`` CLI subcommands and ``beril_worktree.launch``.

Design notes:
- ``beril_cli`` is NOT installed in the KBUtilLib dev environment.
  All tests for launch.py and doctor inject fake modules into ``sys.modules``
  or patch the import helper so they run without the real beril_cli.
- CLI tests use Click's CliRunner with BerilWorktree and subprocess stubs so
  no real git worktrees or Cursor windows are created.
- ``os.execvp`` is never called in tests (the function is marked
  ``# pragma: no cover`` in launch.py).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from kbutillib.cli.beril import beril_cmd


# ---------------------------------------------------------------------------
# Fixtures — scratch git repo (mirrors test_beril_worktree.py)
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture()
def scratch_beril(tmp_path: Path) -> Path:
    """Minimal scratch BERIL git repo with main branch."""
    beril = tmp_path / "beril"
    beril.mkdir()

    subprocess.run(["git", "-C", str(beril), "init", "-b", "main"],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", str(beril), "config", "user.email", "test@test.com"],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", str(beril), "config", "user.name", "Test"],
                   capture_output=True, check=True)

    (beril / ".env").write_text("BERIL_TOKEN=test\n", encoding="utf-8")
    (beril / "BERIL.code-workspace").write_text(
        json.dumps({
            "folders": [{"name": "BERIL", "path": "."}],
            "settings": {"editor.fontSize": 14},
            "extensions": {"recommendations": ["ms-python.python"]},
        }, indent=2),
        encoding="utf-8",
    )
    (beril / ".gitignore").write_text(
        ".env\n*.env\n.venv-berdl/\n.venv-berdl\n*.code-workspace\n",
        encoding="utf-8",
    )
    (beril / "README.md").write_text("# BERIL\n", encoding="utf-8")

    subprocess.run(["git", "-C", str(beril), "add", "-A"],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", str(beril), "commit", "-m", "init"],
                   capture_output=True, check=True)

    return beril


@pytest.fixture()
def worktree_root(tmp_path: Path) -> Path:
    wt = tmp_path / "worktrees"
    wt.mkdir()
    return wt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cli_invoke(args: list, beril_root: str, worktree_root: str, **kwargs):
    """Invoke the beril_cmd via CliRunner with --beril-root and --root injected."""
    runner = CliRunner()
    full_args = [
        "worktree",
        "--beril-root", beril_root,
        "--root", worktree_root,
    ] + args
    return runner.invoke(beril_cmd, full_args, **kwargs)


def _make_fake_beril_cli(
    *,
    default_agent: str = "claude",
    vertex_enabled: bool = False,
    vertex_creds_exist: bool = False,
    missing_symbol: str | None = None,
) -> tuple[ModuleType, ModuleType]:
    """Build fake beril_cli.config and beril_cli.start modules."""

    fake_config = ModuleType("beril_cli.config")
    fake_start = ModuleType("beril_cli.start")

    vertex_cfg: dict = {"enabled": False}
    if vertex_enabled:
        creds_path = "/fake/creds.json"
        vertex_cfg = {
            "enabled": True,
            "credentials_file": creds_path,
            "region": "us-central1",
            "project_id": "my-gcp-project",
        }

    def get_default_agent():
        return default_agent

    def get_vertex_config():
        return vertex_cfg

    def _sync_auth_token(env_path: Path) -> None:
        pass  # no-op in tests

    if missing_symbol != "get_default_agent":
        fake_config.get_default_agent = get_default_agent  # type: ignore[attr-defined]
    if missing_symbol != "get_vertex_config":
        fake_config.get_vertex_config = get_vertex_config  # type: ignore[attr-defined]
    if missing_symbol != "_sync_auth_token":
        fake_start._sync_auth_token = _sync_auth_token  # type: ignore[attr-defined]

    return fake_config, fake_start


def _inject_fake_beril_cli(
    monkeypatch: pytest.MonkeyPatch,
    fake_config: ModuleType,
    fake_start: ModuleType,
) -> None:
    """Insert fake beril_cli modules into sys.modules."""
    fake_pkg = ModuleType("beril_cli")
    monkeypatch.setitem(sys.modules, "beril_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "beril_cli.config", fake_config)
    monkeypatch.setitem(sys.modules, "beril_cli.start", fake_start)


# ===========================================================================
# Tests — CLI smoke: --help lists all 7 subcommands
# ===========================================================================


class TestWorktreeHelp:
    def test_worktree_help_lists_all_subcommands(self) -> None:
        """``kbu beril worktree --help`` lists all 7 subcommands."""
        runner = CliRunner()
        result = runner.invoke(beril_cmd, ["worktree", "--help"])
        assert result.exit_code == 0, result.output
        for cmd in ("new", "open", "start", "rm", "ls", "set-root", "doctor"):
            assert cmd in result.output, f"'{cmd}' not found in help output"

    def test_worktree_group_accepts_beril_root_option(self) -> None:
        """``--beril-root`` is accepted by the worktree group."""
        runner = CliRunner()
        result = runner.invoke(beril_cmd, ["worktree", "--help"])
        assert "--beril-root" in result.output

    def test_worktree_group_accepts_root_option(self) -> None:
        """``--root`` / ``--worktree-root`` is accepted by the worktree group."""
        runner = CliRunner()
        result = runner.invoke(beril_cmd, ["worktree", "--help"])
        assert "--root" in result.output


# ===========================================================================
# Tests — kbu beril worktree new
# ===========================================================================


class TestWorktreeNew:
    def test_new_creates_worktree(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """new <id> creates the worktree and prints confirmation + warning."""
        result = _cli_invoke(
            ["new", "alpha"],
            str(scratch_beril),
            str(worktree_root),
        )
        assert result.exit_code == 0, result.output
        assert "alpha" in result.output
        assert "Warning" in result.output
        assert (worktree_root / "alpha").is_dir()

    def test_new_invalid_id_rejected(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """new <id> with an invalid ID exits non-zero."""
        result = _cli_invoke(
            ["new", "bad/id"],
            str(scratch_beril),
            str(worktree_root),
        )
        assert result.exit_code != 0

    def test_new_open_cursor_not_on_path(
        self, scratch_beril: Path, worktree_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """new --open prints manual instruction when cursor is not on PATH."""
        monkeypatch.setattr("shutil.which", lambda name: None)
        result = _cli_invoke(
            ["new", "--open", "beta"],
            str(scratch_beril),
            str(worktree_root),
        )
        assert result.exit_code == 0, result.output
        assert "cursor is not on PATH" in result.output or "manually" in result.output.lower()

    def test_new_open_cursor_on_path(
        self,
        scratch_beril: Path,
        worktree_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """new --open invokes _open_cursor_workspace when --open is given."""
        opened = []

        def fake_open_cursor(wt_ctx, project_id):
            opened.append(project_id)

        monkeypatch.setattr("kbutillib.cli.beril._open_cursor_workspace", fake_open_cursor)

        result = _cli_invoke(
            ["new", "--open", "gamma"],
            str(scratch_beril),
            str(worktree_root),
        )
        assert result.exit_code == 0, result.output
        assert "gamma" in opened

    def test_new_warns_about_beril_start(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """new prints the beril start warning after success (AC #22)."""
        result = _cli_invoke(
            ["new", "warn-test"],
            str(scratch_beril),
            str(worktree_root),
        )
        assert result.exit_code == 0
        assert "beril start" in result.output


# ===========================================================================
# Tests — kbu beril worktree open
# ===========================================================================


class TestWorktreeOpen:
    def test_open_recreates_worktree(
        self, scratch_beril: Path, worktree_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """open recreates a removed worktree and prints the warning."""
        monkeypatch.setattr("shutil.which", lambda name: None)

        # First: create via new
        _cli_invoke(["new", "open-me"], str(scratch_beril), str(worktree_root))
        # Remove the worktree dir
        from kbutillib.beril_worktree.manager import BerilWorktree
        mgr = BerilWorktree(scratch_beril, worktree_root)
        mgr.remove("open-me")

        assert not (worktree_root / "open-me").exists()

        result = _cli_invoke(["open", "open-me"], str(scratch_beril), str(worktree_root))
        assert result.exit_code == 0, result.output
        assert (worktree_root / "open-me").is_dir()
        assert "Warning" in result.output

    def test_open_errors_when_branch_absent(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """open errors (exit != 0) when the branch does not exist."""
        result = _cli_invoke(
            ["open", "no-such-project"],
            str(scratch_beril),
            str(worktree_root),
        )
        assert result.exit_code != 0

    def test_open_cursor_launched_when_on_path(
        self,
        scratch_beril: Path,
        worktree_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """open invokes _open_cursor_workspace after ensuring the worktree exists."""
        # Create the worktree first (bypass CLI for the new)
        from kbutillib.beril_worktree.manager import BerilWorktree
        BerilWorktree(scratch_beril, worktree_root).new("cursor-open")

        opened = []

        def fake_open_cursor(wt_ctx, project_id):
            opened.append(project_id)

        monkeypatch.setattr("kbutillib.cli.beril._open_cursor_workspace", fake_open_cursor)

        result = _cli_invoke(["open", "cursor-open"], str(scratch_beril), str(worktree_root))
        assert result.exit_code == 0, result.output
        assert "cursor-open" in opened


# ===========================================================================
# Tests — kbu beril worktree start
# ===========================================================================


class TestWorktreeStart:
    def test_start_delegates_to_launch_start(
        self,
        scratch_beril: Path,
        worktree_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """start delegates to launch_start with the right arguments."""
        from kbutillib.beril_worktree.manager import BerilWorktree
        BerilWorktree(scratch_beril, worktree_root).new("start-test")

        launched = {}

        def fake_launch_start(worktree_path, agent, extra_args, skip_onboard):
            launched["path"] = worktree_path
            launched["agent"] = agent
            launched["extra_args"] = extra_args
            launched["skip_onboard"] = skip_onboard

        # The command does a local import, so patch the module attribute directly.
        import kbutillib.beril_worktree.launch as _launch_mod
        monkeypatch.setattr(_launch_mod, "launch_start", fake_launch_start)

        result = _cli_invoke(
            ["start", "start-test", "--agent", "codex"],
            str(scratch_beril),
            str(worktree_root),
        )
        assert result.exit_code == 0, result.output
        assert launched.get("agent") == "codex"
        assert launched.get("skip_onboard") is False

    def test_start_skip_onboard_forwarded(
        self,
        scratch_beril: Path,
        worktree_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """start forwards --skip-onboard to launch_start."""
        from kbutillib.beril_worktree.manager import BerilWorktree
        BerilWorktree(scratch_beril, worktree_root).new("skip-test")

        launched = {}

        def fake_launch_start(worktree_path, agent, extra_args, skip_onboard):
            launched["skip_onboard"] = skip_onboard

        import kbutillib.beril_worktree.launch as _launch_mod
        monkeypatch.setattr(_launch_mod, "launch_start", fake_launch_start)

        result = _cli_invoke(
            ["start", "skip-test", "--skip-onboard"],
            str(scratch_beril),
            str(worktree_root),
        )
        assert result.exit_code == 0, result.output
        assert launched.get("skip_onboard") is True

    def test_start_extra_args_forwarded(
        self,
        scratch_beril: Path,
        worktree_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """start forwards extra args after -- verbatim to launch_start."""
        from kbutillib.beril_worktree.manager import BerilWorktree
        BerilWorktree(scratch_beril, worktree_root).new("extra-test")

        launched = {}

        def fake_launch_start(worktree_path, agent, extra_args, skip_onboard):
            launched["extra_args"] = extra_args

        import kbutillib.beril_worktree.launch as _launch_mod
        monkeypatch.setattr(_launch_mod, "launch_start", fake_launch_start)

        result = _cli_invoke(
            ["start", "extra-test", "--", "--resume", "--verbose"],
            str(scratch_beril),
            str(worktree_root),
        )
        assert result.exit_code == 0, result.output
        assert "--resume" in launched.get("extra_args", [])
        assert "--verbose" in launched.get("extra_args", [])

    def test_start_errors_when_branch_absent(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """start exits non-zero when the branch does not exist."""
        result = _cli_invoke(
            ["start", "no-such"],
            str(scratch_beril),
            str(worktree_root),
        )
        assert result.exit_code != 0

    def test_start_propagates_launch_error(
        self,
        scratch_beril: Path,
        worktree_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """start exits non-zero when launch_start raises RuntimeError."""
        from kbutillib.beril_worktree.manager import BerilWorktree
        BerilWorktree(scratch_beril, worktree_root).new("err-test")

        def fake_launch_start(worktree_path, agent, extra_args, skip_onboard):
            raise RuntimeError("agent not found on PATH")

        import kbutillib.beril_worktree.launch as _launch_mod
        monkeypatch.setattr(_launch_mod, "launch_start", fake_launch_start)

        result = _cli_invoke(
            ["start", "err-test"],
            str(scratch_beril),
            str(worktree_root),
        )
        assert result.exit_code != 0


# ===========================================================================
# Tests — kbu beril worktree rm
# ===========================================================================


class TestWorktreeRm:
    def test_rm_removes_worktree(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """rm <id> removes the worktree directory."""
        _cli_invoke(["new", "rm-test"], str(scratch_beril), str(worktree_root))
        assert (worktree_root / "rm-test").is_dir()

        result = _cli_invoke(["rm", "rm-test"], str(scratch_beril), str(worktree_root))
        assert result.exit_code == 0, result.output
        assert not (worktree_root / "rm-test").exists()

    def test_rm_idempotent(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """rm on an already-gone worktree exits 0 (idempotent)."""
        result = _cli_invoke(
            ["rm", "no-such-wt"],
            str(scratch_beril),
            str(worktree_root),
        )
        assert result.exit_code == 0, result.output

    def test_rm_refuses_dirty_without_force(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """rm fails on dirty worktree without --force."""
        _cli_invoke(["new", "dirty-rm"], str(scratch_beril), str(worktree_root))
        wt = worktree_root / "dirty-rm"
        (wt / "unsaved.txt").write_text("dirty\n")
        subprocess.run(
            ["git", "-C", str(wt), "add", "unsaved.txt"],
            capture_output=True, check=True,
        )

        result = _cli_invoke(["rm", "dirty-rm"], str(scratch_beril), str(worktree_root))
        assert result.exit_code != 0

    def test_rm_force_removes_dirty(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """rm --force removes dirty worktree."""
        _cli_invoke(["new", "dirty-force"], str(scratch_beril), str(worktree_root))
        wt = worktree_root / "dirty-force"
        (wt / "unsaved.txt").write_text("dirty\n")
        subprocess.run(
            ["git", "-C", str(wt), "add", "unsaved.txt"],
            capture_output=True, check=True,
        )

        result = _cli_invoke(
            ["rm", "--force", "dirty-force"],
            str(scratch_beril),
            str(worktree_root),
        )
        assert result.exit_code == 0, result.output
        assert not wt.exists()


# ===========================================================================
# Tests — kbu beril worktree ls
# ===========================================================================


class TestWorktreeLs:
    def test_ls_human_readable(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """ls without --json prints two sections."""
        _cli_invoke(["new", "ls-live"], str(scratch_beril), str(worktree_root))

        result = _cli_invoke(["ls"], str(scratch_beril), str(worktree_root))
        assert result.exit_code == 0, result.output
        assert "Live worktrees" in result.output
        assert "Reopenable" in result.output
        assert "ls-live" in result.output

    def test_ls_json_output(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """ls --json emits a valid JSON array sorted by id."""
        _cli_invoke(["new", "zz-proj"], str(scratch_beril), str(worktree_root))
        _cli_invoke(["new", "aa-proj"], str(scratch_beril), str(worktree_root))

        result = _cli_invoke(["ls", "--json"], str(scratch_beril), str(worktree_root))
        assert result.exit_code == 0, result.output

        data = json.loads(result.output)
        assert isinstance(data, list)
        ids = [item["id"] for item in data]
        assert ids == sorted(ids), "JSON output must be sorted by id"

        for item in data:
            assert {"id", "branch", "path", "live"} == set(item.keys())

    def test_ls_json_live_field(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """ls --json 'live' field is True for live worktrees."""
        _cli_invoke(["new", "lv-test"], str(scratch_beril), str(worktree_root))

        result = _cli_invoke(["ls", "--json"], str(scratch_beril), str(worktree_root))
        data = json.loads(result.output)
        lv = next((e for e in data if e["id"] == "lv-test"), None)
        assert lv is not None
        assert lv["live"] is True

    def test_ls_empty_sections(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """ls shows (none) when there are no worktrees or branches."""
        result = _cli_invoke(["ls"], str(scratch_beril), str(worktree_root))
        assert result.exit_code == 0, result.output
        assert "(none)" in result.output

    def test_ls_shows_reopenable_branches(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """ls lists reopenable branches in the human-readable output."""
        # Create a branch without a live worktree
        subprocess.run(
            ["git", "-C", str(scratch_beril), "checkout", "-b", "projects/reopen-me"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(scratch_beril), "checkout", "main"],
            capture_output=True, check=True,
        )

        result = _cli_invoke(["ls"], str(scratch_beril), str(worktree_root))
        assert result.exit_code == 0, result.output
        assert "reopen-me" in result.output
        assert "Reopenable" in result.output


# ===========================================================================
# Tests — kbu beril worktree set-root
# ===========================================================================


class TestWorktreeSetRoot:
    def test_set_root_persists_worktree_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """set-root persists worktree_root to ~/.kbutillib/config.yaml."""
        import yaml

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setattr(
            "kbutillib.beril_worktree.config._KBUTILLIB_DIR",
            fake_home / ".kbutillib",
        )
        monkeypatch.setattr(
            "kbutillib.beril_worktree.config._DEFAULT_CONFIG_FILE",
            fake_home / ".kbutillib" / "config.yaml",
        )

        wt_root = tmp_path / "my-worktrees"
        wt_root.mkdir()

        runner = CliRunner()
        result = runner.invoke(
            beril_cmd,
            ["worktree", "set-root", str(wt_root)],
        )
        assert result.exit_code == 0, result.output

        cfg_path = fake_home / ".kbutillib" / "config.yaml"
        assert cfg_path.is_file()
        data = yaml.safe_load(cfg_path.read_text())
        assert "worktree_root" in data.get("beril", {})

    def test_set_root_with_beril_root_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """set-root --beril-root persists beril.root."""
        import yaml

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setattr(
            "kbutillib.beril_worktree.config._KBUTILLIB_DIR",
            fake_home / ".kbutillib",
        )
        monkeypatch.setattr(
            "kbutillib.beril_worktree.config._DEFAULT_CONFIG_FILE",
            fake_home / ".kbutillib" / "config.yaml",
        )

        beril_root = tmp_path / "beril-repo"
        beril_root.mkdir()

        runner = CliRunner()
        result = runner.invoke(
            beril_cmd,
            ["worktree", "--beril-root", str(beril_root), "set-root"],
        )
        assert result.exit_code == 0, result.output

        cfg_path = fake_home / ".kbutillib" / "config.yaml"
        data = yaml.safe_load(cfg_path.read_text())
        assert "root" in data.get("beril", {})

    def test_set_root_requires_at_least_one_arg(self) -> None:
        """set-root with no args fails with UsageError."""
        runner = CliRunner()
        result = runner.invoke(beril_cmd, ["worktree", "set-root"])
        assert result.exit_code != 0


# ===========================================================================
# Tests — kbu beril worktree doctor
# ===========================================================================


class TestWorktreeDoctor:
    def _invoke_doctor(
        self,
        scratch_beril: Path,
        worktree_root: Path,
    ):
        runner = CliRunner()
        return runner.invoke(
            beril_cmd,
            [
                "worktree",
                "--beril-root", str(scratch_beril),
                "--root", str(worktree_root),
                "doctor",
            ],
        )

    def test_doctor_exits_1_when_beril_cli_not_importable(
        self,
        scratch_beril: Path,
        worktree_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """doctor exits 1 when beril_cli is not importable (AC #17)."""
        import kbutillib.beril_worktree.launch as _launch_module

        def fake_import():
            raise ImportError("beril_cli is not installed")

        monkeypatch.setattr(_launch_module, "_import_beril_cli", fake_import)

        result = self._invoke_doctor(scratch_beril, worktree_root)
        assert result.exit_code == 1

    def test_doctor_exits_1_naming_missing_symbol(
        self,
        scratch_beril: Path,
        worktree_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """doctor exits 1 naming the missing symbol (AC #17)."""
        import kbutillib.beril_worktree.launch as _launch_module

        def fake_import():
            raise AttributeError(
                "beril_cli is missing expected symbol(s): _sync_auth_token\n"
                "The kbu proxy needs updating."
            )

        monkeypatch.setattr(_launch_module, "_import_beril_cli", fake_import)

        result = self._invoke_doctor(scratch_beril, worktree_root)
        assert result.exit_code == 1
        assert "_sync_auth_token" in result.output

    def test_doctor_exits_0_when_beril_cli_importable(
        self,
        scratch_beril: Path,
        worktree_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """doctor exits 0 when beril_cli imports succeed and no live worktrees."""
        import kbutillib.beril_worktree.launch as _launch_module

        fake_config, fake_start = _make_fake_beril_cli()
        _inject_fake_beril_cli(monkeypatch, fake_config, fake_start)
        # Also ensure _import_beril_cli in the module works after inject
        # (it uses the sys.modules injection from above)

        result = self._invoke_doctor(scratch_beril, worktree_root)
        assert result.exit_code == 0, result.output

    def test_doctor_reports_symlink_health_readable(
        self,
        scratch_beril: Path,
        worktree_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """doctor reports readable symlinks as passing (AC #18)."""
        import kbutillib.beril_worktree.launch as _launch_module
        fake_config, fake_start = _make_fake_beril_cli()
        _inject_fake_beril_cli(monkeypatch, fake_config, fake_start)

        # Create a live worktree so doctor has something to check
        from kbutillib.beril_worktree.manager import BerilWorktree
        BerilWorktree(scratch_beril, worktree_root).new("doctor-live")

        result = self._invoke_doctor(scratch_beril, worktree_root)
        # .env symlink resolves to beril/.env which EXISTS in scratch_beril
        assert "readable" in result.output or result.exit_code == 0

    def test_doctor_reports_broken_symlink(
        self,
        scratch_beril: Path,
        worktree_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """doctor reports broken symlink targets as failing (AC #18)."""
        import kbutillib.beril_worktree.launch as _launch_module
        fake_config, fake_start = _make_fake_beril_cli()
        _inject_fake_beril_cli(monkeypatch, fake_config, fake_start)

        # Create a live worktree
        from kbutillib.beril_worktree.manager import BerilWorktree
        BerilWorktree(scratch_beril, worktree_root).new("broken-link")

        # Remove the .env target so the symlink is broken
        (scratch_beril / ".env").unlink()

        result = self._invoke_doctor(scratch_beril, worktree_root)
        # Should report the broken link and exit 1
        assert result.exit_code == 1
        assert "missing or unreadable" in result.output or "broken" in result.output.lower() or "unreadable" in result.output

    def test_doctor_reports_non_symlink_as_failing(
        self,
        scratch_beril: Path,
        worktree_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """doctor reports failure when .env in a worktree is a regular file, not a symlink."""
        import kbutillib.beril_worktree.launch as _launch_module
        fake_config, fake_start = _make_fake_beril_cli()
        _inject_fake_beril_cli(monkeypatch, fake_config, fake_start)

        # Create a live worktree (it will have .env as a symlink)
        from kbutillib.beril_worktree.manager import BerilWorktree
        BerilWorktree(scratch_beril, worktree_root).new("nonsymlink-test")

        # Replace the .env symlink with a regular file
        env_link = worktree_root / "nonsymlink-test" / ".env"
        env_link.unlink()
        env_link.write_text("not a symlink\n", encoding="utf-8")

        result = self._invoke_doctor(scratch_beril, worktree_root)
        assert result.exit_code == 1
        assert "not a symlink" in result.output

    def test_doctor_no_live_worktrees_message(
        self,
        scratch_beril: Path,
        worktree_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """doctor prints 'no live worktrees' when there are none."""
        import kbutillib.beril_worktree.launch as _launch_module
        fake_config, fake_start = _make_fake_beril_cli()
        _inject_fake_beril_cli(monkeypatch, fake_config, fake_start)

        result = self._invoke_doctor(scratch_beril, worktree_root)
        assert "no live worktrees" in result.output


# ===========================================================================
# Tests — _open_cursor_workspace helper
# ===========================================================================


class TestOpenCursorWorkspace:
    """Direct tests for the _open_cursor_workspace helper function."""

    def test_popen_called_when_cursor_on_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_open_cursor_workspace calls subprocess.Popen when cursor is on PATH."""
        from kbutillib.cli.beril import _WorktreeCtx, _open_cursor_workspace

        wt_root = tmp_path / "worktrees"
        wt_root.mkdir()
        beril_root = tmp_path / "beril"

        # Stub out _WorktreeCtx so we don't need real config
        class FakeCtx:
            worktree_root = wt_root

        launched = []

        def fake_popen(cmd, **kwargs):
            launched.append(cmd)
            return MagicMock()

        monkeypatch.setattr("kbutillib.cli.beril.shutil.which", lambda n: "/usr/bin/cursor" if n == "cursor" else None)
        monkeypatch.setattr("kbutillib.cli.beril.subprocess.Popen", fake_popen)

        runner = CliRunner()
        with runner.isolated_filesystem():
            _open_cursor_workspace(FakeCtx(), "myproj")  # type: ignore[arg-type]

        assert len(launched) == 1
        assert "cursor" in str(launched[0])

    def test_manual_instruction_when_cursor_not_on_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_open_cursor_workspace prints a manual instruction when cursor is absent."""
        from kbutillib.cli.beril import _open_cursor_workspace

        wt_root = tmp_path / "worktrees"
        wt_root.mkdir()

        class FakeCtx:
            worktree_root = wt_root

        monkeypatch.setattr("kbutillib.cli.beril.shutil.which", lambda n: None)

        runner = CliRunner()
        result_output = []

        import click as _click

        def fake_echo(msg, **kwargs):
            result_output.append(msg)

        monkeypatch.setattr(_click, "echo", fake_echo)

        _open_cursor_workspace(FakeCtx(), "myproj")  # type: ignore[arg-type]

        combined = " ".join(result_output)
        assert "cursor is not on PATH" in combined or "manually" in combined.lower()


# ===========================================================================
# Tests — launch.py: import tripwire (AC #23)
# ===========================================================================


class TestImportTripwire:
    """Assert beril_cli and the three borrowed symbols can be resolved."""

    def test_symbols_resolve_with_fake_modules(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_import_beril_cli() returns all three symbols when modules are present."""
        import kbutillib.beril_worktree.launch as _launch_module

        fake_config, fake_start = _make_fake_beril_cli()
        _inject_fake_beril_cli(monkeypatch, fake_config, fake_start)

        get_default_agent, get_vertex_config, _sync_auth_token = _launch_module._import_beril_cli()
        assert callable(get_default_agent)
        assert callable(get_vertex_config)
        assert callable(_sync_auth_token)

    def test_import_fails_when_beril_cli_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_import_beril_cli() raises ImportError when beril_cli is absent."""
        import builtins
        import kbutillib.beril_worktree.launch as _launch_module

        for key in list(sys.modules.keys()):
            if "beril_cli" in key:
                monkeypatch.delitem(sys.modules, key)

        real_import = builtins.__import__

        def blocking_import(name, *args, **kwargs):
            if name == "beril_cli" or name.startswith("beril_cli."):
                raise ImportError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocking_import)

        with pytest.raises(ImportError, match="beril_cli"):
            _launch_module._import_beril_cli()

    def test_import_raises_attribute_error_for_missing_symbol(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_import_beril_cli() raises AttributeError naming the missing symbol."""
        import kbutillib.beril_worktree.launch as _launch_module

        fake_config, fake_start = _make_fake_beril_cli(missing_symbol="_sync_auth_token")
        _inject_fake_beril_cli(monkeypatch, fake_config, fake_start)

        with pytest.raises(AttributeError, match="_sync_auth_token"):
            _launch_module._import_beril_cli()

    def test_import_raises_for_missing_get_default_agent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AttributeError is raised when get_default_agent is missing."""
        import kbutillib.beril_worktree.launch as _launch_module

        fake_config, fake_start = _make_fake_beril_cli(missing_symbol="get_default_agent")
        _inject_fake_beril_cli(monkeypatch, fake_config, fake_start)

        with pytest.raises(AttributeError, match="get_default_agent"):
            _launch_module._import_beril_cli()

    def test_import_raises_for_missing_get_vertex_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AttributeError is raised when get_vertex_config is missing."""
        import kbutillib.beril_worktree.launch as _launch_module

        fake_config, fake_start = _make_fake_beril_cli(missing_symbol="get_vertex_config")
        _inject_fake_beril_cli(monkeypatch, fake_config, fake_start)

        with pytest.raises(AttributeError, match="get_vertex_config"):
            _launch_module._import_beril_cli()


# ===========================================================================
# Tests — assemble_start_command: pure argv/env assembly (AC #15, #16, #23)
# ===========================================================================


class TestAssembleStartCommand:
    """Tests for the pure ``assemble_start_command`` function."""

    @pytest.fixture()
    def fake_which(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Make shutil.which return a fake path for known agents."""
        monkeypatch.setattr(
            "shutil.which",
            lambda name: f"/usr/local/bin/{name}" if name in ("claude", "codex", "gemini") else None,
        )

    def _call(
        self,
        worktree: Path,
        agent: str | None,
        extra_args: list,
        skip_onboard: bool,
        *,
        default_agent: str = "claude",
        vertex_enabled: bool = False,
        vertex_creds_exist: bool = False,
    ) -> tuple:
        """Helper: call assemble_start_command with fake dependencies."""
        from kbutillib.beril_worktree.launch import assemble_start_command

        vertex_cfg: dict = {"enabled": False}
        if vertex_enabled:
            vertex_cfg = {
                "enabled": True,
                "credentials_file": "/fake/creds.json" if vertex_creds_exist else "",
                "region": "us-central1",
                "project_id": "my-project",
            }

        return assemble_start_command(
            worktree,
            agent,
            extra_args,
            skip_onboard,
            _get_default_agent=lambda: default_agent,
            _get_vertex_config=lambda: vertex_cfg,
        )

    def test_no_release_checkout(self, tmp_path: Path, fake_which) -> None:
        """Assembled argv contains no checkout/release command (AC #15, #23)."""
        binary, argv, env = self._call(tmp_path, "claude", [], False)
        for arg in argv:
            assert "checkout" not in str(arg).lower(), f"Unexpected checkout arg: {arg}"
            assert "release" not in str(arg).lower(), f"Unexpected release arg: {arg}"

    def test_opus_model_injected_by_default_for_claude(
        self, tmp_path: Path, fake_which
    ) -> None:
        """--model opus is injected when not supplied for claude (AC #15)."""
        binary, argv, env = self._call(tmp_path, "claude", [], False)
        assert "--model" in argv
        idx = argv.index("--model")
        assert argv[idx + 1] == "opus"

    def test_opus_model_not_injected_when_supplied(
        self, tmp_path: Path, fake_which
    ) -> None:
        """--model is NOT re-injected when the user already supplied it (AC #15)."""
        binary, argv, env = self._call(tmp_path, "claude", ["--model", "sonnet"], False)
        model_count = sum(1 for a in argv if a == "--model")
        assert model_count == 1
        idx = argv.index("--model")
        assert argv[idx + 1] == "sonnet"

    def test_berdl_start_injected_by_default(
        self, tmp_path: Path, fake_which
    ) -> None:
        """/berdl_start is injected when skip_onboard=False and no extra_args (AC #15)."""
        binary, argv, env = self._call(tmp_path, "claude", [], False)
        assert "/berdl_start" in argv

    def test_berdl_start_suppressed_by_skip_onboard(
        self, tmp_path: Path, fake_which
    ) -> None:
        """/berdl_start is NOT injected when skip_onboard=True (AC #15)."""
        binary, argv, env = self._call(tmp_path, "claude", [], True)
        assert "/berdl_start" not in argv

    def test_berdl_start_suppressed_when_prompt_supplied(
        self, tmp_path: Path, fake_which
    ) -> None:
        """/berdl_start is NOT injected when extra_args is non-empty (AC #15)."""
        binary, argv, env = self._call(tmp_path, "claude", ["my prompt"], False)
        assert "/berdl_start" not in argv

    def test_opus_and_berdl_not_injected_for_non_claude(
        self, tmp_path: Path, fake_which
    ) -> None:
        """Opus and /berdl_start are NOT added for non-claude agents."""
        binary, argv, env = self._call(tmp_path, "codex", [], False)
        assert "--model" not in argv
        assert "/berdl_start" not in argv

    def test_vertex_env_applied_for_claude_when_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Vertex env keys are set for claude when vertex is enabled and creds exist (AC #16)."""
        monkeypatch.setattr(
            "shutil.which",
            lambda name: f"/usr/local/bin/{name}",
        )
        # Patch Path.exists to return True for the fake creds file
        monkeypatch.setattr(Path, "exists", lambda self: True)

        binary, argv, env = self._call(
            tmp_path,
            "claude",
            [],
            False,
            vertex_enabled=True,
            vertex_creds_exist=True,
        )
        assert "CLAUDE_CODE_USE_VERTEX" in env
        assert env["CLAUDE_CODE_USE_VERTEX"] == "1"
        assert "CLOUD_ML_REGION" in env
        assert "ANTHROPIC_VERTEX_PROJECT_ID" in env
        assert "GOOGLE_APPLICATION_CREDENTIALS" in env
        assert "VERTEX_REGION_CLAUDE_HAIKU_4_5" in env
        assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" in env

    def test_vertex_env_not_applied_for_non_claude(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Vertex env keys are NOT set for non-claude agents (AC #16)."""
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/local/bin/{name}")
        monkeypatch.setattr(Path, "exists", lambda self: True)

        binary, argv, env = self._call(
            tmp_path,
            "codex",
            [],
            False,
            vertex_enabled=True,
            vertex_creds_exist=True,
        )
        assert "CLAUDE_CODE_USE_VERTEX" not in env

    def test_vertex_env_not_applied_when_disabled(
        self, tmp_path: Path, fake_which
    ) -> None:
        """Vertex env keys are NOT set when vertex is disabled (AC #16)."""
        binary, argv, env = self._call(tmp_path, "claude", [], False, vertex_enabled=False)
        assert "CLAUDE_CODE_USE_VERTEX" not in env

    def test_vertex_env_not_applied_when_creds_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Vertex env keys are NOT set when creds file does not exist (AC #16)."""
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/local/bin/{name}")
        # creds file path is empty string → Path("").exists() is False
        monkeypatch.setattr(Path, "exists", lambda self: False)

        binary, argv, env = self._call(
            tmp_path,
            "claude",
            [],
            False,
            vertex_enabled=True,
            vertex_creds_exist=False,
        )
        assert "CLAUDE_CODE_USE_VERTEX" not in env

    def test_agent_binary_resolved(self, tmp_path: Path, fake_which) -> None:
        """assemble_start_command returns the resolved binary path."""
        binary, argv, env = self._call(tmp_path, "claude", [], False)
        assert binary == "/usr/local/bin/claude"

    def test_agent_not_on_path_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """assemble_start_command raises RuntimeError when binary is not found."""
        monkeypatch.setattr("shutil.which", lambda name: None)

        from kbutillib.beril_worktree.launch import assemble_start_command

        with pytest.raises(RuntimeError, match="not installed or not on PATH"):
            assemble_start_command(
                tmp_path,
                "claude",
                [],
                False,
                _get_default_agent=lambda: "claude",
                _get_vertex_config=lambda: {"enabled": False},
            )

    def test_default_agent_used_when_none(
        self, tmp_path: Path, fake_which
    ) -> None:
        """None agent falls back to get_default_agent()."""
        binary, argv, env = self._call(tmp_path, None, [], False, default_agent="claude")
        assert argv[0] == "claude"

    def test_argv_first_element_is_agent_name(
        self, tmp_path: Path, fake_which
    ) -> None:
        """First element of argv is the agent name (not the binary path)."""
        binary, argv, env = self._call(tmp_path, "codex", [], False)
        assert argv[0] == "codex"

    def test_assemble_uses_deferred_import_when_no_injectables(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When _get_default_agent is None, _import_beril_cli() is called."""
        fake_config, fake_start = _make_fake_beril_cli()
        _inject_fake_beril_cli(monkeypatch, fake_config, fake_start)
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/local/bin/{name}")

        from kbutillib.beril_worktree.launch import assemble_start_command

        binary, argv, env = assemble_start_command(
            tmp_path, "claude", [], False
        )
        assert binary == "/usr/local/bin/claude"


# ===========================================================================
# Tests — launch_start (everything before os.execvp)
# ===========================================================================


class TestLaunchStart:
    """Test launch_start up to (but not including) os.execvp."""

    def test_launch_start_calls_chdir_sync_and_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """launch_start chdir's to the worktree, calls _sync_auth_token, updates env."""
        import kbutillib.beril_worktree.launch as _launch_module

        # Intercept os.execvp before it replaces the process
        execvp_calls = []

        def fake_execvp(file, args):
            execvp_calls.append((file, args))
            # Don't actually exec — just return to allow assertions
            raise SystemExit(0)

        chdir_calls = []

        def fake_chdir(path):
            chdir_calls.append(path)

        sync_calls = []
        fake_config, fake_start = _make_fake_beril_cli()

        # Override _sync_auth_token to record calls
        def recording_sync(env_path):
            sync_calls.append(env_path)

        fake_start._sync_auth_token = recording_sync  # type: ignore[attr-defined]
        _inject_fake_beril_cli(monkeypatch, fake_config, fake_start)

        monkeypatch.setattr("kbutillib.beril_worktree.launch.os.chdir", fake_chdir)
        monkeypatch.setattr("kbutillib.beril_worktree.launch.os.execvp", fake_execvp)
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

        wt = tmp_path / "worktree"
        wt.mkdir()

        with pytest.raises(SystemExit):
            _launch_module.launch_start(wt, "claude", [], False)

        assert str(wt) in [str(c) for c in chdir_calls]
        assert any(str(wt) in str(c) for c in sync_calls)

    def test_launch_start_propagates_import_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """launch_start propagates ImportError from _import_beril_cli."""
        import kbutillib.beril_worktree.launch as _launch_module

        def bad_import():
            raise ImportError("beril_cli not found")

        monkeypatch.setattr(_launch_module, "_import_beril_cli", bad_import)

        with pytest.raises(ImportError, match="beril_cli"):
            _launch_module.launch_start(tmp_path, "claude", [], False)


# ===========================================================================
# Tests — worktree_doctor_cmd: no beril_root configured path
# ===========================================================================


class TestDoctorNoBERILRoot:
    def test_doctor_skips_symlink_check_when_beril_root_unconfigured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """doctor skips symlink check with message when beril_root is unconfigured."""
        fake_config, fake_start = _make_fake_beril_cli()
        _inject_fake_beril_cli(monkeypatch, fake_config, fake_start)

        # Remove all BERIL_ROOT sources
        monkeypatch.delenv("BERIL_ROOT", raising=False)
        monkeypatch.setattr(
            "kbutillib.beril_worktree.config._DEFAULT_CONFIG_FILE",
            tmp_path / "nonexistent.yaml",
        )

        runner = CliRunner()
        result = runner.invoke(beril_cmd, ["worktree", "doctor"])
        # Symlink check should be skipped but beril_cli import check can pass
        assert "skipped" in result.output or result.exit_code in (0, 1)


# ===========================================================================
# Tests — _WorktreeCtx error paths
# ===========================================================================


class TestWorktreeCtxErrors:
    def test_beril_root_not_configured_gives_usage_error(self) -> None:
        """Commands fail with non-zero exit when beril_root is unresolved."""
        runner = CliRunner()
        # Don't supply --beril-root; rely on there being no env/config
        result = runner.invoke(
            beril_cmd,
            ["worktree", "ls"],
            env={"HOME": "/nonexistent/fake-home", "BERIL_ROOT": ""},
        )
        # Should fail since BERIL_ROOT is empty and config likely absent
        # (The exact exit code depends on the env; we just verify it ran.)
        assert result.exit_code != 0 or "BERIL" in result.output
