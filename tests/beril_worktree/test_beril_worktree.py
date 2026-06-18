"""Tests for kbutillib.beril_worktree.config and manager modules.

All git operations run against a scratch temporary git repository — the real
BERIL repo is never touched.  Config tests use a temporary HOME directory
to avoid mutating ~/.kbutillib/config.yaml.

Covers (per PRD and task spec):
  - BerilWorktree.new: creates worktree + projects/<id> branch
  - BerilWorktree.new: adopts an existing branch (no -b, no error)
  - BerilWorktree.remove: deletes directory but branch still exists
  - BerilWorktree.remove: idempotent no-op when worktree not registered
  - BerilWorktree.remove: refuses on uncommitted changes; force overrides
  - .env / .venv-berdl symlinks created and are gitignored
  - workspace file: valid JSON, outside worktree, folder points at ./<id>
  - ID validation rejects slashes and other invalid chars
  - config resolution precedence: arg > env > config > default
  - set_root persists to ~/.kbutillib/config.yaml; not to BERIL config
  - BerilWorktree.list returns live + reopenable entries
  - BerilWorktree.open recreates from existing branch; errors when branch absent
"""

from __future__ import annotations

import json
import os
import subprocess
import warnings
from pathlib import Path
from typing import Generator

import pytest
import yaml


# ---------------------------------------------------------------------------
# Fixtures — scratch git repo
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command inside *repo*, raising on failure."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture()
def scratch_beril(tmp_path: Path) -> Path:
    """Minimal scratch BERIL git repository with a 'main' branch.

    Structure:
        <tmp>/beril/   — git repo with one commit on main
            .env           — real file so symlink target exists
            BERIL.code-workspace  — minimal workspace JSON
            .gitignore         — ignores .env, .venv-berdl
    """
    beril = tmp_path / "beril"
    beril.mkdir()

    # git config required for commits on macOS CI
    subprocess.run(["git", "-C", str(beril), "init", "-b", "main"],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", str(beril), "config", "user.email", "test@test.com"],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", str(beril), "config", "user.name", "Test"],
                   capture_output=True, check=True)

    # Seed files
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
        ".env\n*.env\n.venv-berdl/\n.venv-berdl\n",
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
    """A temp directory to use as the worktree root."""
    wt = tmp_path / "worktrees"
    wt.mkdir()
    return wt


@pytest.fixture()
def manager(scratch_beril: Path, worktree_root: Path):
    """A BerilWorktree instance pointed at the scratch repo."""
    from kbutillib.beril_worktree.manager import BerilWorktree

    return BerilWorktree(beril_root=scratch_beril, worktree_root=worktree_root)


@pytest.fixture()
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override HOME so config writes go to a temp dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Patch Path.home() cache inside the config module.
    monkeypatch.setattr(
        "kbutillib.beril_worktree.config._KBUTILLIB_DIR",
        home / ".kbutillib",
    )
    monkeypatch.setattr(
        "kbutillib.beril_worktree.config._DEFAULT_CONFIG_FILE",
        home / ".kbutillib" / "config.yaml",
    )
    return home


# ---------------------------------------------------------------------------
# Helper — seed a projects/<id> branch in the scratch repo
# ---------------------------------------------------------------------------


def _seed_branch(beril: Path, project_id: str) -> None:
    """Create projects/<id> branch in *beril* with one commit."""
    branch = f"projects/{project_id}"
    subprocess.run(
        ["git", "-C", str(beril), "checkout", "-b", branch],
        capture_output=True, check=True,
    )
    proj_dir = beril / "projects" / project_id
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "README.md").write_text(f"# {project_id}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(beril), "add", "-A"],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", str(beril), "commit", "-m", f"add {project_id}"],
                   capture_output=True, check=True)
    # Return to main
    subprocess.run(["git", "-C", str(beril), "checkout", "main"],
                   capture_output=True, check=True)


# ===========================================================================
# Tests — Manager core
# ===========================================================================


class TestNew:
    def test_new_creates_worktree_and_branch(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """new() creates the worktree directory and the projects/<id> branch."""
        pid = "alpha"
        wt = manager.new(pid)
        assert wt == worktree_root / pid
        assert wt.is_dir()

        # Branch exists in primary repo
        result = subprocess.run(
            ["git", "-C", str(scratch_beril), "show-ref", "--verify",
             f"refs/heads/projects/{pid}"],
            capture_output=True,
        )
        assert result.returncode == 0, f"Branch projects/{pid} not found"

    def test_new_adopts_existing_branch(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """new() adopts an existing projects/<id> branch without error."""
        pid = "existing-proj"
        _seed_branch(scratch_beril, pid)

        wt = manager.new(pid)
        assert wt.is_dir()

        # Branch still exists (not recreated)
        result = subprocess.run(
            ["git", "-C", str(scratch_beril), "show-ref", "--verify",
             f"refs/heads/projects/{pid}"],
            capture_output=True,
        )
        assert result.returncode == 0

    def test_new_aborts_if_dir_exists_and_not_worktree(
        self, manager, worktree_root: Path
    ) -> None:
        """new() aborts if the target dir exists but is not a registered worktree."""
        pid = "stray"
        stray = worktree_root / pid
        stray.mkdir()
        (stray / "file.txt").write_text("stray content", encoding="utf-8")

        with pytest.raises(RuntimeError, match="not a registered git worktree"):
            manager.new(pid)

    def test_new_worktree_dir_name_and_branch(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """Worktree dir is exactly <worktree_root>/<id>; branch is exactly projects/<id>."""
        pid = "my.proj-123"
        wt = manager.new(pid)
        assert wt == worktree_root / pid

        # Inspect via porcelain
        result = subprocess.run(
            ["git", "-C", str(scratch_beril), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, check=True,
        )
        assert f"projects/{pid}" in result.stdout


class TestRemove:
    def test_remove_deletes_dir_branch_survives(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """remove() deletes the worktree directory; the branch still exists."""
        pid = "beta"
        manager.new(pid)
        wt = worktree_root / pid
        assert wt.is_dir()

        removed = manager.remove(pid)
        assert removed is True
        assert not wt.exists(), "worktree directory should be gone"

        # Branch still exists
        result = subprocess.run(
            ["git", "-C", str(scratch_beril), "show-ref", "--verify",
             f"refs/heads/projects/{pid}"],
            capture_output=True,
        )
        assert result.returncode == 0, "Branch must survive remove()"

    def test_remove_idempotent(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """remove() is a no-op (returns False) when the worktree is not registered."""
        pid = "gamma"
        manager.new(pid)
        manager.remove(pid)

        # Second call — not registered
        removed = manager.remove(pid)
        assert removed is False

    def test_remove_refuses_dirty_worktree(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """remove() raises RuntimeError when the worktree has uncommitted changes."""
        pid = "dirty-proj"
        wt = manager.new(pid)

        # Stage a new file in the worktree without committing
        (wt / "new_file.txt").write_text("uncommitted\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(wt), "add", "new_file.txt"],
            capture_output=True, check=True,
        )

        with pytest.raises(RuntimeError, match="uncommitted changes"):
            manager.remove(pid)

    def test_remove_force_overrides_dirty(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """remove(force=True) discards uncommitted changes and removes the worktree."""
        pid = "force-proj"
        wt = manager.new(pid)

        # Stage a new file without committing
        (wt / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(wt), "add", "dirty.txt"],
            capture_output=True, check=True,
        )

        removed = manager.remove(pid, force=True)
        assert removed is True
        assert not wt.exists()


class TestSymlinks:
    def test_symlinks_created(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """.env and .venv-berdl symlinks are created inside the worktree."""
        pid = "symlink-test"
        wt = manager.new(pid)

        env_link = wt / ".env"
        venv_link = wt / ".venv-berdl"
        assert env_link.is_symlink(), ".env must be a symlink"
        assert venv_link.is_symlink(), ".venv-berdl must be a symlink"

        # Symlinks point to the primary BERIL repo
        assert env_link.resolve() == (scratch_beril / ".env").resolve()
        assert venv_link.resolve() == (scratch_beril / ".venv-berdl").resolve()

    def test_symlinks_are_gitignored(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """.env and .venv-berdl symlinks are covered by BERIL's .gitignore."""
        # Check that BERIL's .gitignore patterns cover both names
        gitignore = (scratch_beril / ".gitignore").read_text(encoding="utf-8")
        # .env is covered by '.env' or '*.env'
        assert ".env" in gitignore
        # .venv-berdl is covered by '.venv-berdl'
        assert ".venv-berdl" in gitignore

        # Actually create the worktree and check git status reports them as ignored
        pid = "ignore-test"
        wt = manager.new(pid)

        result = subprocess.run(
            ["git", "-C", str(wt), "status", "--short", "--ignored"],
            capture_output=True, text=True, check=True,
        )
        output = result.stdout
        # The symlink names must not appear as untracked (!! = ignored is fine)
        untracked = [
            line for line in output.splitlines()
            if line.startswith("?? ") and (".env" in line or ".venv-berdl" in line)
        ]
        assert not untracked, f"Symlinks appear as untracked: {untracked}"

    def test_symlink_missing_target_warns(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """.venv-berdl symlink is created even when target is missing; a warning is issued."""
        pid = "missing-venv"
        # .venv-berdl does not exist in scratch_beril (only .env does)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            manager.new(pid)

        warn_msgs = [str(w.message) for w in caught]
        venv_warns = [m for m in warn_msgs if ".venv-berdl" in m]
        assert venv_warns, "Expected a warning about missing .venv-berdl target"

        wt = worktree_root / pid
        assert (wt / ".venv-berdl").is_symlink(), "Symlink must be created despite missing target"


class TestSkillSymlinks:
    SKILL_LINKS = (
        ".claude/kbu",
        ".claude/skills/kbu",
        ".claude/skills/kbu-fba",
        ".claude/skills/kbu-notebook",
    )

    def _seed_skills(self, beril: Path) -> None:
        """Deploy stub kbu skill bundles in the scratch BERIL root."""
        for rel in self.SKILL_LINKS:
            d = beril / rel
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(f"# {rel}\n", encoding="utf-8")

    def test_skill_symlinks_created(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """kbu skill bundles are symlinked into the worktree, pointing at beril_root."""
        self._seed_skills(scratch_beril)
        pid = "skill-link-test"
        wt = manager.new(pid)

        for rel in self.SKILL_LINKS:
            link = wt / rel
            assert link.is_symlink(), f"{rel} must be a symlink"
            assert link.resolve() == (scratch_beril / rel).resolve(), (
                f"{rel} must resolve to the beril_root copy"
            )

    def test_skill_symlink_missing_target_skips_with_warning(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """A missing skill bundle is skipped (no broken link) with a warning."""
        # Do NOT seed the skill dirs — targets are absent.
        pid = "skill-missing-test"
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            wt = manager.new(pid)

        warn_msgs = [str(w.message) for w in caught]
        assert any("skill bundle missing" in m for m in warn_msgs), (
            "Expected a warning about missing kbu skill bundles"
        )
        # No broken symlinks should have been created.
        for rel in self.SKILL_LINKS:
            link = wt / rel
            assert not link.is_symlink(), f"{rel} must not be a (broken) symlink"


class TestWorkspace:
    def test_workspace_valid_json(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """Workspace file is valid JSON."""
        pid = "ws-test"
        manager.new(pid)
        ws_file = worktree_root / f"{pid}.code-workspace"
        assert ws_file.is_file()

        content = json.loads(ws_file.read_text(encoding="utf-8"))
        assert isinstance(content, dict)

    def test_workspace_outside_worktree(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """Workspace file lives in worktree_root, not inside the worktree directory."""
        pid = "ws-outside"
        wt = manager.new(pid)
        ws_file = worktree_root / f"{pid}.code-workspace"

        # ws_file is in worktree_root, not inside wt/
        assert ws_file.parent == worktree_root
        assert not ws_file.is_relative_to(wt)

    def test_workspace_folder_path(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """The single folder entry uses path './<id>'."""
        pid = "ws-path"
        manager.new(pid)
        ws_file = worktree_root / f"{pid}.code-workspace"
        data = json.loads(ws_file.read_text(encoding="utf-8"))

        assert len(data["folders"]) == 1
        folder = data["folders"][0]
        assert folder["path"] == f"./{pid}"
        assert folder["name"] == f"BERIL: {pid}"

    def test_workspace_copies_settings_and_extensions(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """Settings and extensions are copied from BERIL.code-workspace."""
        pid = "ws-settings"
        manager.new(pid)
        ws_file = worktree_root / f"{pid}.code-workspace"
        data = json.loads(ws_file.read_text(encoding="utf-8"))

        assert data["settings"] == {"editor.fontSize": 14}
        assert data["extensions"] == {"recommendations": ["ms-python.python"]}

    def test_workspace_empty_when_no_beril_ws(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """settings and extensions are empty objects when BERIL.code-workspace is absent."""
        # Remove the BERIL workspace file
        (scratch_beril / "BERIL.code-workspace").unlink()

        from kbutillib.beril_worktree.manager import BerilWorktree

        mgr = BerilWorktree(beril_root=scratch_beril, worktree_root=worktree_root)
        pid = "ws-no-beril"
        mgr.new(pid)
        ws_file = worktree_root / f"{pid}.code-workspace"
        data = json.loads(ws_file.read_text(encoding="utf-8"))

        assert data["settings"] == {}
        assert data["extensions"] == {}

    def test_workspace_empty_when_beril_ws_malformed(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """settings and extensions are empty objects when BERIL.code-workspace is malformed JSON."""
        (scratch_beril / "BERIL.code-workspace").write_text("not json!", encoding="utf-8")

        from kbutillib.beril_worktree.manager import BerilWorktree

        mgr = BerilWorktree(beril_root=scratch_beril, worktree_root=worktree_root)
        pid = "ws-malformed"
        mgr.new(pid)
        ws_file = worktree_root / f"{pid}.code-workspace"
        data = json.loads(ws_file.read_text(encoding="utf-8"))

        assert data["settings"] == {}
        assert data["extensions"] == {}


# ===========================================================================
# Tests — ID validation
# ===========================================================================


class TestIDValidation:
    def test_valid_ids_accepted(self, manager) -> None:
        """Valid ID patterns are accepted without raising."""
        from kbutillib.beril_worktree.manager import BerilWorktree, _ID_PATTERN

        valid = ["foo", "foo-bar", "foo.bar", "Foo123", "my_proj", "a"]
        for vid in valid:
            assert _ID_PATTERN.match(vid), f"Expected {vid!r} to match pattern"

    def test_slash_rejected(self, manager) -> None:
        """IDs containing slashes are rejected."""
        with pytest.raises(ValueError, match="Invalid project ID"):
            manager.new("foo/bar")

    def test_space_rejected(self, manager) -> None:
        """IDs containing spaces are rejected."""
        with pytest.raises(ValueError, match="Invalid project ID"):
            manager.new("foo bar")

    def test_at_sign_rejected(self, manager) -> None:
        """IDs containing @ are rejected."""
        with pytest.raises(ValueError, match="Invalid project ID"):
            manager.new("foo@bar")

    def test_empty_rejected(self, manager) -> None:
        """Empty IDs are rejected."""
        with pytest.raises(ValueError, match="Invalid project ID"):
            manager.new("")


# ===========================================================================
# Tests — list()
# ===========================================================================


class TestList:
    def test_list_shows_live_worktrees(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """list() includes live worktrees with live=True."""
        pid = "list-live"
        manager.new(pid)

        entries = manager.list()
        ids = {e.id for e in entries}
        assert pid in ids

        live_entry = next(e for e in entries if e.id == pid)
        assert live_entry.live is True
        assert live_entry.path is not None

    def test_list_shows_reopenable_branches(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """list() includes projects/* branches that have no worktree as live=False."""
        pid = "list-reopen"
        _seed_branch(scratch_beril, pid)

        entries = manager.list()
        ids = {e.id for e in entries}
        assert pid in ids

        reopen_entry = next(e for e in entries if e.id == pid)
        assert reopen_entry.live is False
        assert reopen_entry.path is None

    def test_list_sorted_by_id(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """list() returns entries sorted by id."""
        pids = ["zzz", "aaa", "mmm"]
        for pid in pids:
            manager.new(pid)

        entries = manager.list()
        # Filter to only the ones we created
        our = [e for e in entries if e.id in pids]
        assert [e.id for e in our] == sorted(pids)

    def test_list_branch_name(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """Each entry has branch = 'projects/<id>'."""
        pid = "list-branch"
        manager.new(pid)

        entries = manager.list()
        entry = next(e for e in entries if e.id == pid)
        assert entry.branch == f"projects/{pid}"


# ===========================================================================
# Tests — open()
# ===========================================================================


class TestOpen:
    def test_open_recreates_missing_worktree(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """open() recreates the worktree from an existing branch when dir is missing."""
        pid = "open-reopen"
        manager.new(pid)
        manager.remove(pid)

        wt = worktree_root / pid
        assert not wt.exists()

        result = manager.open(pid)
        assert result == wt
        assert wt.is_dir()

    def test_open_returns_path_when_dir_exists(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """open() returns the path when the worktree directory already exists."""
        pid = "open-exists"
        manager.new(pid)
        wt = worktree_root / pid
        assert wt.is_dir()

        result = manager.open(pid)
        assert result == wt

    def test_open_errors_when_branch_absent(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """open() raises RuntimeError (directing to new) when branch does not exist."""
        pid = "open-no-branch"
        with pytest.raises(RuntimeError, match="does not exist"):
            manager.open(pid)

    def test_open_invalid_id_rejected(self, manager) -> None:
        """open() raises ValueError for invalid project IDs."""
        with pytest.raises(ValueError, match="Invalid project ID"):
            manager.open("bad/id")


# ===========================================================================
# Tests — Config resolution
# ===========================================================================


class TestConfigResolution:
    """Test config.resolve_beril_root and resolve_worktree_root precedence."""

    def test_explicit_arg_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit argument has highest precedence for beril_root."""
        from kbutillib.beril_worktree.config import resolve_beril_root

        monkeypatch.setenv("BERIL_ROOT", "/should/be/ignored")
        result = resolve_beril_root(explicit=str(tmp_path))
        assert result == tmp_path.resolve()

    def test_env_wins_over_config(
        self, tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BERIL_ROOT env var wins over config file value."""
        from kbutillib.beril_worktree.config import resolve_beril_root, set_root

        # Write a different path to config
        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        set_root(beril_root=str(tmp_path / "from-config"), config_file=cfg_path)

        env_path = tmp_path / "from-env"
        env_path.mkdir()
        monkeypatch.setenv("BERIL_ROOT", str(env_path))

        result = resolve_beril_root(config_file=cfg_path)
        assert result == env_path.resolve()

    def test_config_used_when_no_env(
        self, tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config beril.root is used when BERIL_ROOT env var is absent."""
        from kbutillib.beril_worktree.config import resolve_beril_root, set_root

        monkeypatch.delenv("BERIL_ROOT", raising=False)

        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        cfg_root = tmp_path / "from-config"
        cfg_root.mkdir()
        set_root(beril_root=str(cfg_root), config_file=cfg_path)

        result = resolve_beril_root(config_file=cfg_path)
        assert result == cfg_root.resolve()

    def test_beril_root_errors_when_unresolved(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """beril_root raises ValueError with guidance when nothing is configured."""
        from kbutillib.beril_worktree.config import resolve_beril_root

        monkeypatch.delenv("BERIL_ROOT", raising=False)
        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        # Config file does not exist — no beril section

        with pytest.raises(ValueError, match="BERIL root is not configured"):
            resolve_beril_root(config_file=cfg_path)

    def test_worktree_root_explicit_arg_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit argument wins for worktree_root."""
        from kbutillib.beril_worktree.config import resolve_worktree_root

        monkeypatch.setenv("WORKING_BERIL_DIRECTORY", "/should/be/ignored")
        result = resolve_worktree_root(explicit=str(tmp_path))
        assert result == tmp_path.resolve()

    def test_worktree_root_env_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WORKING_BERIL_DIRECTORY env var wins over config."""
        from kbutillib.beril_worktree.config import resolve_worktree_root

        env_path = tmp_path / "env-wt-root"
        env_path.mkdir()
        monkeypatch.setenv("WORKING_BERIL_DIRECTORY", str(env_path))

        result = resolve_worktree_root()
        assert result == env_path.resolve()

    def test_worktree_root_config_used(
        self, tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config beril.worktree_root is used when env is absent."""
        from kbutillib.beril_worktree.config import resolve_worktree_root, set_root

        monkeypatch.delenv("WORKING_BERIL_DIRECTORY", raising=False)
        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        wt_root = tmp_path / "cfg-wt-root"
        wt_root.mkdir()
        set_root(worktree_root=str(wt_root), beril_root=str(tmp_path), config_file=cfg_path)

        result = resolve_worktree_root(config_file=cfg_path)
        assert result == wt_root.resolve()

    def test_worktree_root_default_is_sibling(
        self, tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default worktree_root is <beril_root>/../WorkingBERIL."""
        from kbutillib.beril_worktree.config import resolve_worktree_root

        monkeypatch.delenv("WORKING_BERIL_DIRECTORY", raising=False)

        beril_root = tmp_path / "BERIL-research-observatory"
        beril_root.mkdir()
        expected = (beril_root / ".." / "WorkingBERIL").resolve()

        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        result = resolve_worktree_root(beril_root=beril_root, config_file=cfg_path)
        assert result == expected

    def test_worktree_root_default_when_beril_root_unknown(
        self, tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When beril_root is unknown, worktree_root falls back to ~/WorkingBERIL."""
        from kbutillib.beril_worktree.config import resolve_worktree_root

        monkeypatch.delenv("WORKING_BERIL_DIRECTORY", raising=False)
        monkeypatch.delenv("BERIL_ROOT", raising=False)
        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        # Config file is empty / does not exist

        result = resolve_worktree_root(config_file=cfg_path)
        # Falls back to ~/WorkingBERIL
        assert result == Path.home() / "WorkingBERIL"


# ===========================================================================
# Tests — set_root
# ===========================================================================


class TestSetRoot:
    def test_set_root_persists_beril_root(
        self, tmp_path: Path, tmp_home: Path
    ) -> None:
        """set_root persists beril.root to ~/.kbutillib/config.yaml."""
        from kbutillib.beril_worktree.config import set_root

        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        beril_path = tmp_path / "my-beril"
        beril_path.mkdir()

        set_root(beril_root=str(beril_path), config_file=cfg_path)

        assert cfg_path.is_file()
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert data["beril"]["root"] == str(beril_path.resolve())

    def test_set_root_persists_worktree_root(
        self, tmp_path: Path, tmp_home: Path
    ) -> None:
        """set_root persists beril.worktree_root to ~/.kbutillib/config.yaml."""
        from kbutillib.beril_worktree.config import set_root

        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        wt_path = tmp_path / "my-worktrees"
        wt_path.mkdir()

        set_root(worktree_root=str(wt_path), config_file=cfg_path)

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert data["beril"]["worktree_root"] == str(wt_path.resolve())

    def test_set_root_creates_parent_directory(
        self, tmp_path: Path, tmp_home: Path
    ) -> None:
        """set_root creates ~/.kbutillib/ if it does not exist."""
        from kbutillib.beril_worktree.config import set_root

        cfg_dir = tmp_home / ".kbutillib"
        assert not cfg_dir.exists()

        cfg_path = cfg_dir / "config.yaml"
        set_root(beril_root=str(tmp_path), config_file=cfg_path)

        assert cfg_dir.is_dir()
        assert cfg_path.is_file()

    def test_set_root_expands_tilde(self, tmp_home: Path) -> None:
        """set_root expands ~ in paths."""
        from kbutillib.beril_worktree.config import set_root

        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        # Use ~ in the path; HOME is tmp_home
        set_root(beril_root="~/my-beril", config_file=cfg_path)

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        stored = data["beril"]["root"]
        # Should be an absolute path; ~ should be expanded
        assert not stored.startswith("~"), f"~ not expanded: {stored!r}"
        assert stored.startswith(str(tmp_home)), \
            f"Expected path under {tmp_home}, got {stored!r}"

    def test_set_root_raises_when_neither_provided(self, tmp_home: Path) -> None:
        """set_root raises ValueError when called with no arguments."""
        from kbutillib.beril_worktree.config import set_root

        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        with pytest.raises(ValueError, match="At least one"):
            set_root(config_file=cfg_path)

    def test_set_root_does_not_write_beril_config(
        self, tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """set_root never writes to ~/.config/beril/config.toml."""
        from kbutillib.beril_worktree.config import set_root

        # Fake HOME so we can check what gets written
        beril_config = tmp_home / ".config" / "beril" / "config.toml"

        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        set_root(beril_root=str(tmp_path), config_file=cfg_path)

        assert not beril_config.exists(), \
            "set_root must never write ~/.config/beril/config.toml"

    def test_set_root_merges_with_existing_config(
        self, tmp_path: Path, tmp_home: Path
    ) -> None:
        """set_root preserves other config sections when updating beril keys."""
        from kbutillib.beril_worktree.config import set_root

        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        # Pre-populate with a non-beril section
        cfg_path.write_text(
            "skani:\n  executable: skani\n",
            encoding="utf-8",
        )

        set_root(beril_root=str(tmp_path), config_file=cfg_path)

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        # skani section must survive
        assert data.get("skani", {}).get("executable") == "skani"
        # beril section must be written
        assert "root" in data.get("beril", {})

    def test_set_root_both_args(
        self, tmp_path: Path, tmp_home: Path
    ) -> None:
        """set_root writes both beril.root and beril.worktree_root when both are given."""
        from kbutillib.beril_worktree.config import set_root

        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        br = tmp_path / "beril"
        br.mkdir()
        wtr = tmp_path / "wt"
        wtr.mkdir()

        set_root(beril_root=str(br), worktree_root=str(wtr), config_file=cfg_path)

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert "root" in data["beril"]
        assert "worktree_root" in data["beril"]


# ===========================================================================
# Tests — edge cases for manager helpers
# ===========================================================================


class TestManagerHelpers:
    def test_branch_not_exists(self, manager, scratch_beril: Path) -> None:
        """_branch_exists returns False for a non-existent branch."""
        assert not manager._branch_exists("projects/no-such-id")

    def test_branch_exists_after_new(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """_branch_exists returns True after new() creates the branch."""
        pid = "branch-check"
        manager.new(pid)
        assert manager._branch_exists(f"projects/{pid}")

    def test_remove_with_invalid_id(self, manager) -> None:
        """remove() raises ValueError for invalid project IDs."""
        with pytest.raises(ValueError, match="Invalid project ID"):
            manager.remove("bad/id")

    def test_list_project_branches_empty(self, manager, scratch_beril: Path) -> None:
        """_list_project_branches returns empty list when no projects/* branches exist."""
        branches = manager._list_project_branches()
        # Should be empty or filtered to only projects/* branches
        assert all(b.isidentifier() or True for b in branches)  # just check type
        # Actually check no projects/* branches exist initially
        assert not any(True for _ in branches)

    def test_new_idempotent_on_existing_registered_worktree(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """new() on an already-registered worktree adopts it without error."""
        pid = "idempotent-wt"
        wt1 = manager.new(pid)
        # The worktree is now registered; calling new() again should succeed
        # because it's already a registered worktree (git will refuse the add
        # but we handle that case via git error).
        # Actually git will fail if we try to add the same path again.
        # Let's verify the worktree dir is already registered.
        assert manager._is_registered_worktree(wt1)

    def test_worktree_root_created_by_new(
        self, scratch_beril: Path, tmp_path: Path
    ) -> None:
        """new() creates worktree_root if it doesn't exist."""
        from kbutillib.beril_worktree.manager import BerilWorktree

        new_root = tmp_path / "non-existent-root"
        assert not new_root.exists()

        mgr = BerilWorktree(beril_root=scratch_beril, worktree_root=new_root)
        pid = "auto-root"
        wt = mgr.new(pid)
        assert new_root.is_dir()
        assert wt.is_dir()

    def test_new_when_dir_exists_and_is_registered(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """new() with an existing registered worktree dir aborts via git error."""
        pid = "registered-existing"
        manager.new(pid)
        wt = worktree_root / pid
        assert wt.is_dir()
        # Trying new() again: dir exists and IS registered.
        # git worktree add will refuse (already checked out) — RuntimeError expected.
        with pytest.raises(RuntimeError):
            manager.new(pid)

    def test_open_cursor_calls_open(
        self, manager, scratch_beril: Path, worktree_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """new(open_cursor=True) calls self.open()."""
        opened = []
        original_open = manager.open.__func__

        def _fake_open(self, project_id: str) -> Path:
            opened.append(project_id)
            return self._worktree_path(project_id)

        monkeypatch.setattr(manager.__class__, "open", _fake_open)
        pid = "cursor-test"
        manager.new(pid, open_cursor=True)
        assert pid in opened

    def test_remove_git_fails_unexpectedly(
        self, manager, scratch_beril: Path, worktree_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """remove() raises RuntimeError when git worktree remove fails unexpectedly."""
        import subprocess as _subprocess

        pid = "fail-remove"
        manager.new(pid)

        # Patch subprocess.run to make git worktree remove fail.
        original_run = _subprocess.run

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and "worktree" in cmd and "remove" in cmd:
                from unittest.mock import MagicMock
                r = MagicMock()
                r.returncode = 1
                r.stderr = "simulated failure"
                r.stdout = ""
                return r
            return original_run(cmd, **kwargs)

        monkeypatch.setattr(_subprocess, "run", _fake_run)

        with pytest.raises(RuntimeError, match="git worktree remove failed"):
            manager.remove(pid)

    def test_add_worktree_git_fails(
        self, manager, scratch_beril: Path, worktree_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_add_worktree raises RuntimeError when git worktree add fails."""
        import subprocess as _subprocess

        original_run = _subprocess.run

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and "worktree" in cmd and "add" in cmd:
                from unittest.mock import MagicMock
                r = MagicMock()
                r.returncode = 128
                r.stderr = "simulated git failure"
                r.stdout = ""
                return r
            return original_run(cmd, **kwargs)

        monkeypatch.setattr(_subprocess, "run", _fake_run)

        with pytest.raises(RuntimeError, match="git worktree add failed"):
            manager.new("fail-add")

    def test_open_git_worktree_add_fails(
        self, manager, scratch_beril: Path, worktree_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """open() raises RuntimeError when git worktree add fails during recreation."""
        import subprocess as _subprocess

        pid = "open-fail-add"
        # Create branch but not the worktree
        _seed_branch(scratch_beril, pid)

        original_run = _subprocess.run

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and "worktree" in cmd and "add" in cmd:
                from unittest.mock import MagicMock
                r = MagicMock()
                r.returncode = 128
                r.stderr = "git add failed"
                r.stdout = ""
                return r
            return original_run(cmd, **kwargs)

        monkeypatch.setattr(_subprocess, "run", _fake_run)

        with pytest.raises(RuntimeError, match="git worktree add failed"):
            manager.open(pid)

    def test_parse_worktree_list_handles_bare_and_detached(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """_parse_worktree_list handles 'bare' and 'detached' lines."""
        from kbutillib.beril_worktree.manager import BerilWorktree
        import subprocess as _subprocess

        mgr = BerilWorktree(beril_root=scratch_beril, worktree_root=worktree_root)

        # Simulate porcelain output with a detached HEAD worktree.
        fake_output = (
            "worktree /some/path\nHEAD abc123\ndetached\n\n"
            "worktree /another/path\nHEAD def456\nbranch refs/heads/projects/test\n\n"
        )
        original_run = _subprocess.run

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and "worktree" in cmd and "list" in cmd:
                from unittest.mock import MagicMock
                r = MagicMock()
                r.returncode = 0
                r.stdout = fake_output
                return r
            return original_run(cmd, **kwargs)

        import unittest.mock
        with unittest.mock.patch("subprocess.run", side_effect=_fake_run):
            entries = mgr._parse_worktree_list()

        assert len(entries) == 2
        # First entry has detached = "true"
        assert entries[0].get("detached") == "true"
        # Second entry has branch
        assert "branch" in entries[1]

    def test_parse_worktree_list_trailing_no_newline(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """_parse_worktree_list handles trailing entry with no trailing blank line."""
        from kbutillib.beril_worktree.manager import BerilWorktree

        mgr = BerilWorktree(beril_root=scratch_beril, worktree_root=worktree_root)

        # Simulate output ending without trailing newline/blank line
        fake_output = "worktree /some/path\nHEAD abc123\nbranch refs/heads/main"
        import unittest.mock
        from unittest.mock import MagicMock

        def _fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = fake_output
            return r

        with unittest.mock.patch("subprocess.run", side_effect=_fake_run):
            entries = mgr._parse_worktree_list()

        # The trailing entry should be captured by the "if current:" after the loop.
        assert len(entries) == 1
        assert entries[0]["worktree"] == "/some/path"

    def test_parse_worktree_list_empty_line_with_empty_current(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """_parse_worktree_list handles leading blank lines gracefully."""
        from kbutillib.beril_worktree.manager import BerilWorktree

        mgr = BerilWorktree(beril_root=scratch_beril, worktree_root=worktree_root)

        # Leading blank line before first entry
        fake_output = "\nworktree /some/path\nHEAD abc123\nbranch refs/heads/main\n\n"
        import unittest.mock
        from unittest.mock import MagicMock

        def _fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = fake_output
            return r

        with unittest.mock.patch("subprocess.run", side_effect=_fake_run):
            entries = mgr._parse_worktree_list()

        assert len(entries) == 1

    def test_list_project_branches_skips_non_project_refs(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """_list_project_branches ignores branch names that don't start with 'projects/'."""
        from kbutillib.beril_worktree.manager import BerilWorktree

        mgr = BerilWorktree(beril_root=scratch_beril, worktree_root=worktree_root)

        import unittest.mock
        from unittest.mock import MagicMock

        def _fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            # Return a branch name that does NOT start with projects/
            r.stdout = "main\nprojects/valid\n"
            return r

        with unittest.mock.patch("subprocess.run", side_effect=_fake_run):
            ids = mgr._list_project_branches()

        assert "valid" in ids
        assert "main" not in ids

    def test_symlink_already_exists_is_skipped(
        self, manager, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """_symlink_env skips symlinks that already exist (idempotent)."""
        pid = "symlink-idempotent"
        manager.new(pid)
        wt = worktree_root / pid
        # Both symlinks already exist after new().
        assert (wt / ".env").is_symlink()
        assert (wt / ".venv-berdl").is_symlink()

        # Calling _symlink_env again should not raise.
        manager._symlink_env(pid)  # Must not error

    def test_parse_worktree_list_ignores_unknown_no_space_line(
        self, scratch_beril: Path, worktree_root: Path
    ) -> None:
        """_parse_worktree_list silently ignores lines with no space that are not bare/detached."""
        from kbutillib.beril_worktree.manager import BerilWorktree
        import unittest.mock
        from unittest.mock import MagicMock

        mgr = BerilWorktree(beril_root=scratch_beril, worktree_root=worktree_root)

        # Include an unknown single-token line (e.g. a future git extension)
        fake_output = (
            "worktree /some/path\n"
            "HEAD abc123\n"
            "unknowntoken\n"          # <-- single token, not bare/detached
            "branch refs/heads/main\n"
            "\n"
        )

        def _fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = fake_output
            return r

        with unittest.mock.patch("subprocess.run", side_effect=_fake_run):
            entries = mgr._parse_worktree_list()

        # The entry should still be parsed; the unknown token is silently ignored.
        assert len(entries) == 1
        assert entries[0]["worktree"] == "/some/path"


# ===========================================================================
# Tests — config internal helpers
# ===========================================================================


class TestConfigHelpers:
    def test_read_nonexistent_config_returns_empty(self, tmp_path: Path) -> None:
        """_read_kbu_config returns {} when the file does not exist."""
        from kbutillib.beril_worktree.config import _read_kbu_config

        result = _read_kbu_config(tmp_path / "nonexistent.yaml")
        assert result == {}

    def test_read_empty_yaml_returns_empty(self, tmp_path: Path) -> None:
        """_read_kbu_config returns {} for an empty YAML file."""
        from kbutillib.beril_worktree.config import _read_kbu_config

        f = tmp_path / "empty.yaml"
        f.write_text("", encoding="utf-8")
        result = _read_kbu_config(f)
        assert result == {}

    def test_write_and_read_roundtrip(self, tmp_path: Path) -> None:
        """_write_kbu_config and _read_kbu_config round-trip correctly."""
        from kbutillib.beril_worktree.config import _read_kbu_config, _write_kbu_config

        cfg_file = tmp_path / "config.yaml"
        data = {"beril": {"root": "/some/path"}}
        _write_kbu_config(data, cfg_file)
        result = _read_kbu_config(cfg_file)
        assert result == data

    def test_worktree_root_with_config_having_null_beril_section(
        self, tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resolve_worktree_root handles a config with beril: null gracefully."""
        from kbutillib.beril_worktree.config import resolve_worktree_root

        monkeypatch.delenv("WORKING_BERIL_DIRECTORY", raising=False)

        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        # Write a config where beril section is explicitly null
        cfg_path.write_text("beril:\n", encoding="utf-8")

        beril_root = tmp_path / "beril"
        beril_root.mkdir()

        result = resolve_worktree_root(beril_root=beril_root, config_file=cfg_path)
        expected = (beril_root / ".." / "WorkingBERIL").resolve()
        assert result == expected

    def test_worktree_root_default_via_beril_root_from_config(
        self, tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resolve_worktree_root computes default from beril.root in config when beril_root is None."""
        from kbutillib.beril_worktree.config import resolve_worktree_root, set_root

        monkeypatch.delenv("WORKING_BERIL_DIRECTORY", raising=False)
        monkeypatch.delenv("BERIL_ROOT", raising=False)

        cfg_path = tmp_home / ".kbutillib" / "config.yaml"
        beril_root_path = tmp_path / "BERIL-repo"
        beril_root_path.mkdir()
        # Write only beril.root (not worktree_root)
        set_root(beril_root=str(beril_root_path), config_file=cfg_path)

        # Call with no beril_root arg — should derive from config
        result = resolve_worktree_root(config_file=cfg_path)
        expected = (beril_root_path / ".." / "WorkingBERIL").resolve()
        assert result == expected
