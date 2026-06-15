"""Tests for ``kbu beril install`` and ``kbu beril doctor``.

Uses a temporary fake BERIL root (PROJECT.md + .claude/skills/ + git init
+ empty commit + tag v0) to verify:

- Three skill dirs land and are untracked (git status --porcelain)
- preferences.md renders-if-absent and is never clobbered
- Re-install is idempotent (same content, still untracked)
- pip step is skipped when MOCKED version probe matches deployer
- doctor: green on clean install, non-zero when a skill dir is missing or
  import fails
- After ``git checkout v0`` the skill dirs + .claude/kbu/ survive (they
  are untracked and therefore not disturbed by checkout)
- --dry-run leaves the fake root unchanged
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from kbutillib.cli.beril import (
    _DIST_NAME,
    _SKILL_NAMES,
    beril_cmd,
    doctor_cmd,
    install_cmd,
)

# ---------------------------------------------------------------------------
# Fake BERIL root fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_beril_root(tmp_path: Path) -> Path:
    """Create a minimal BERIL root with git history.

    Layout:
        <tmp>/PROJECT.md         — required sentinel file
        <tmp>/.claude/skills/    — required skill target dir
        <tmp>/.git/              — git repo with one commit + tag v0
    """
    root = tmp_path / "fake_beril"
    root.mkdir()

    # Required sentinels
    (root / "PROJECT.md").write_text("# Fake BERIL Project\n", encoding="utf-8")
    (root / ".claude" / "skills").mkdir(parents=True)

    # Initialise git and create an empty tagged commit
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    # Create an initial commit so tags have something to point at.
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "init")
    _git(root, "tag", "v0")

    return root


def _git(cwd: Path, *args: str) -> str:
    """Run git in *cwd* and return stdout; raises on non-zero exit."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _git_porcelain(root: Path) -> list[str]:
    """Return non-empty lines from ``git status --porcelain`` in *root*."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Helper: run kbu beril <sub> via Click test runner
# ---------------------------------------------------------------------------


def _run_install(root: Path, extra_args: list[str] | None = None) -> "click.testing.Result":
    runner = CliRunner()
    args = ["install", str(root)] + (extra_args or [])
    return runner.invoke(beril_cmd, args, catch_exceptions=False)


def _run_doctor(root: Path) -> "click.testing.Result":
    runner = CliRunner()
    return runner.invoke(beril_cmd, ["doctor", str(root)], catch_exceptions=False)


# ---------------------------------------------------------------------------
# Helpers for mocking the pip skip / import-success paths
# ---------------------------------------------------------------------------

_MODULE = "kbutillib.cli.beril"


# ---------------------------------------------------------------------------
# 1. Skill dir copy
# ---------------------------------------------------------------------------


class TestInstallSkillDirs:
    """kbu beril install copies all three skill dirs into .claude/skills/."""

    def test_skill_dirs_land(self, fake_beril_root: Path) -> None:
        """All three skill dirs appear under .claude/skills/ after install."""
        with (
            patch(f"{_MODULE}._installed_version_under", return_value=None),
            patch(f"{_MODULE}._run_pip_install", return_value=(True, "mocked pip ok")),
        ):
            result = _run_install(fake_beril_root)
        assert result.exit_code == 0, result.output
        for name in _SKILL_NAMES:
            dest = fake_beril_root / ".claude" / "skills" / name
            assert dest.is_dir(), f"Expected skill dir {dest}"

    def test_skill_dirs_untracked(self, fake_beril_root: Path) -> None:
        """Copied skill dirs are untracked (not staged or committed) in the fake repo.

        git porcelain may report '?? .claude/' (umbrella) rather than listing
        each sub-path individually, so we check that .claude/ is untracked AND
        that the skill dirs actually exist on disk — together these confirm the
        dirs were never 'git add'-ed.
        """
        with (
            patch(f"{_MODULE}._installed_version_under", return_value=None),
            patch(f"{_MODULE}._run_pip_install", return_value=(True, "mocked pip ok")),
        ):
            _run_install(fake_beril_root)

        lines = _git_porcelain(fake_beril_root)
        untracked = [l for l in lines if l.startswith("??")]
        # git may report ".claude/" as a single umbrella untracked entry or
        # expand to sub-paths — either way, ".claude" must appear somewhere.
        assert any(".claude" in l for l in untracked), (
            f"Expected .claude/ (or sub-paths) to appear as untracked; "
            f"porcelain output: {lines}"
        )
        # Confirm the actual dirs exist so the untracked status is meaningful.
        for name in _SKILL_NAMES:
            assert (fake_beril_root / ".claude" / "skills" / name).is_dir()

    def test_skill_dirs_idempotent(self, fake_beril_root: Path) -> None:
        """Running install twice is idempotent: dirs still present, no error."""
        with (
            patch(f"{_MODULE}._installed_version_under", return_value=None),
            patch(f"{_MODULE}._run_pip_install", return_value=(True, "mocked pip ok")),
        ):
            r1 = _run_install(fake_beril_root)
            r2 = _run_install(fake_beril_root)
        assert r1.exit_code == 0
        assert r2.exit_code == 0
        for name in _SKILL_NAMES:
            assert (fake_beril_root / ".claude" / "skills" / name).is_dir()


# ---------------------------------------------------------------------------
# 2. preferences.md render-if-absent, never-clobber
# ---------------------------------------------------------------------------


class TestPreferencesMd:
    """preferences.md is rendered if absent, never overwritten if present."""

    def test_preferences_rendered_if_absent(self, fake_beril_root: Path) -> None:
        """preferences.md is created at .claude/kbu/preferences.md on first install."""
        dest = fake_beril_root / ".claude" / "kbu" / "preferences.md"
        assert not dest.exists()

        with (
            patch(f"{_MODULE}._installed_version_under", return_value=None),
            patch(f"{_MODULE}._run_pip_install", return_value=(True, "mocked pip ok")),
        ):
            _run_install(fake_beril_root)

        assert dest.is_file(), f"preferences.md not rendered at {dest}"
        content = dest.read_text(encoding="utf-8")
        assert len(content) > 0, "preferences.md is empty after render"

    def test_preferences_not_clobbered(self, fake_beril_root: Path) -> None:
        """A pre-existing preferences.md is left byte-for-byte unchanged."""
        prefs_dir = fake_beril_root / ".claude" / "kbu"
        prefs_dir.mkdir(parents=True, exist_ok=True)
        sentinel = "# CUSTOM PREFERENCES — DO NOT OVERWRITE\n"
        (prefs_dir / "preferences.md").write_text(sentinel, encoding="utf-8")

        with (
            patch(f"{_MODULE}._installed_version_under", return_value=None),
            patch(f"{_MODULE}._run_pip_install", return_value=(True, "mocked pip ok")),
        ):
            _run_install(fake_beril_root)

        actual = (prefs_dir / "preferences.md").read_text(encoding="utf-8")
        assert actual == sentinel, (
            "preferences.md was overwritten; expected the sentinel text to be preserved"
        )

    def test_preferences_untracked_after_render(self, fake_beril_root: Path) -> None:
        """Rendered preferences.md is untracked in the git repo.

        git porcelain may report '?? .claude/' (umbrella) or individual paths;
        we accept either as long as .claude/ appears untracked and the file exists.
        """
        with (
            patch(f"{_MODULE}._installed_version_under", return_value=None),
            patch(f"{_MODULE}._run_pip_install", return_value=(True, "mocked pip ok")),
        ):
            _run_install(fake_beril_root)

        lines = _git_porcelain(fake_beril_root)
        untracked = [l for l in lines if l.startswith("??")]
        assert any(".claude" in l for l in untracked), (
            f".claude/ not listed as untracked; porcelain: {lines}"
        )
        # Also confirm the file actually exists.
        assert (fake_beril_root / ".claude" / "kbu" / "preferences.md").is_file()


# ---------------------------------------------------------------------------
# 3. pip step: skip when version matches; run when not installed
# ---------------------------------------------------------------------------


class TestPipStep:
    """pip install is skipped when the installed version matches the deployer."""

    def test_pip_skipped_when_version_matches(self, fake_beril_root: Path) -> None:
        """pip install is NOT called when installed version == deployer version."""
        from kbutillib.cli.beril import _deployer_version

        deployer_ver = _deployer_version()

        with (
            patch(
                f"{_MODULE}._installed_version_under", return_value=deployer_ver
            ) as mock_probe,
            patch(f"{_MODULE}._run_pip_install") as mock_pip,
        ):
            result = _run_install(fake_beril_root)

        assert result.exit_code == 0, result.output
        mock_pip.assert_not_called()

    def test_pip_called_when_not_installed(self, fake_beril_root: Path) -> None:
        """pip install IS called when version probe returns None (not installed)."""
        with (
            patch(f"{_MODULE}._installed_version_under", return_value=None),
            patch(
                f"{_MODULE}._run_pip_install", return_value=(True, "mocked ok")
            ) as mock_pip,
        ):
            result = _run_install(fake_beril_root)

        assert result.exit_code == 0
        mock_pip.assert_called_once()

    def test_pip_called_when_version_mismatch(self, fake_beril_root: Path) -> None:
        """pip install IS called when installed version differs from deployer."""
        with (
            patch(f"{_MODULE}._installed_version_under", return_value="0.0.1"),
            patch(
                f"{_MODULE}._run_pip_install", return_value=(True, "mocked ok")
            ) as mock_pip,
        ):
            result = _run_install(fake_beril_root)

        assert result.exit_code == 0
        mock_pip.assert_called_once()


# ---------------------------------------------------------------------------
# 4. doctor: green on clean install; non-zero when skill dir missing or import fails
# ---------------------------------------------------------------------------


class TestDoctor:
    """doctor returns 0 on a clean install, non-zero on missing skill dir or failed import."""

    def _do_install(self, root: Path) -> None:
        with (
            patch(f"{_MODULE}._installed_version_under", return_value=None),
            patch(f"{_MODULE}._run_pip_install", return_value=(True, "mocked pip ok")),
        ):
            r = _run_install(root)
        assert r.exit_code == 0, r.output + (r.stderr or "")

    def test_doctor_green_after_clean_install(self, fake_beril_root: Path) -> None:
        """doctor exits 0 after a successful install with mocked version/import checks."""
        from kbutillib.cli.beril import _deployer_version

        deployer_ver = _deployer_version()
        self._do_install(fake_beril_root)

        with (
            patch(f"{_MODULE}._import_succeeds_under", return_value=True),
            patch(f"{_MODULE}._installed_version_under", return_value=deployer_ver),
        ):
            result = _run_doctor(fake_beril_root)

        assert result.exit_code == 0, result.output

    def test_doctor_fail_missing_skill_dir(self, fake_beril_root: Path) -> None:
        """doctor exits non-zero when a skill dir is missing."""
        from kbutillib.cli.beril import _deployer_version

        deployer_ver = _deployer_version()
        self._do_install(fake_beril_root)

        # Remove one skill dir
        shutil.rmtree(fake_beril_root / ".claude" / "skills" / "kbu")

        with (
            patch(f"{_MODULE}._import_succeeds_under", return_value=True),
            patch(f"{_MODULE}._installed_version_under", return_value=deployer_ver),
        ):
            result = _run_doctor(fake_beril_root)

        assert result.exit_code != 0, "doctor should fail when a skill dir is missing"

    def test_doctor_fail_import_fails(self, fake_beril_root: Path) -> None:
        """doctor exits non-zero when import kbutillib fails under the interpreter."""
        from kbutillib.cli.beril import _deployer_version

        deployer_ver = _deployer_version()
        self._do_install(fake_beril_root)

        with (
            patch(f"{_MODULE}._import_succeeds_under", return_value=False),
            patch(f"{_MODULE}._installed_version_under", return_value=deployer_ver),
        ):
            result = _run_doctor(fake_beril_root)

        assert result.exit_code != 0, "doctor should fail when import fails"

    def test_doctor_fail_version_mismatch(self, fake_beril_root: Path) -> None:
        """doctor exits non-zero when installed version mismatches deployer."""
        self._do_install(fake_beril_root)

        with (
            patch(f"{_MODULE}._import_succeeds_under", return_value=True),
            patch(f"{_MODULE}._installed_version_under", return_value="0.0.1"),
        ):
            result = _run_doctor(fake_beril_root)

        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# 5. git checkout v0 survival
# ---------------------------------------------------------------------------


class TestGitCheckoutSurvival:
    """Skill dirs and .claude/kbu/ survive a git checkout v0 (they are untracked)."""

    def test_skill_dirs_survive_git_checkout(self, fake_beril_root: Path) -> None:
        """After checkout v0, all skill dirs and .claude/kbu/ are still present."""
        with (
            patch(f"{_MODULE}._installed_version_under", return_value=None),
            patch(f"{_MODULE}._run_pip_install", return_value=(True, "mocked pip ok")),
        ):
            r = _run_install(fake_beril_root)
        assert r.exit_code == 0

        # Confirm items are untracked before checkout
        lines = _git_porcelain(fake_beril_root)
        untracked = [l for l in lines if l.startswith("??")]
        assert any(".claude" in l for l in untracked), (
            f".claude/ not untracked before checkout; porcelain: {lines}"
        )

        # git checkout v0 — should leave untracked files untouched
        _git(fake_beril_root, "checkout", "v0")

        # Skill dirs still present
        for name in _SKILL_NAMES:
            dest = fake_beril_root / ".claude" / "skills" / name
            assert dest.is_dir(), f"{dest} missing after git checkout v0"

        # .claude/kbu/ still present
        kbu_dir = fake_beril_root / ".claude" / "kbu"
        assert kbu_dir.is_dir(), f".claude/kbu/ missing after git checkout v0"

        # preferences.md still present
        prefs = kbu_dir / "preferences.md"
        assert prefs.is_file(), f"preferences.md missing after git checkout v0"


# ---------------------------------------------------------------------------
# 6. --dry-run leaves root unchanged
# ---------------------------------------------------------------------------


class TestDryRun:
    """--dry-run prints actions but writes nothing to disk."""

    def test_dry_run_no_files_written(self, fake_beril_root: Path) -> None:
        """After --dry-run, the fake root is in the same state as before."""
        # Snapshot the state before dry-run
        before = _collect_paths(fake_beril_root)

        result = _run_install(fake_beril_root, extra_args=["--dry-run"])
        assert result.exit_code == 0, result.output

        after = _collect_paths(fake_beril_root)
        assert after == before, (
            f"--dry-run modified the fake root.\n"
            f"Before: {sorted(before)}\n"
            f"After:  {sorted(after)}"
        )

    def test_dry_run_mentions_skill_dirs(self, fake_beril_root: Path) -> None:
        """--dry-run output mentions all three skill dir names."""
        result = _run_install(fake_beril_root, extra_args=["--dry-run"])
        assert result.exit_code == 0
        combined = result.output
        for name in _SKILL_NAMES:
            assert name in combined, (
                f"--dry-run output did not mention skill '{name}'"
            )

    def test_dry_run_mentions_interpreter(self, fake_beril_root: Path) -> None:
        """--dry-run output mentions the resolved interpreter path."""
        result = _run_install(fake_beril_root, extra_args=["--dry-run"])
        assert result.exit_code == 0
        combined = result.output
        assert "interpreter" in combined.lower(), (
            "--dry-run output did not mention 'interpreter'"
        )


# ---------------------------------------------------------------------------
# Utility: collect all relative paths under a directory
# ---------------------------------------------------------------------------


def _collect_paths(root: Path) -> set[str]:
    """Return the set of all relative path strings under *root*."""
    paths: set[str] = set()
    for p in root.rglob("*"):
        # Exclude git internal files that change on every operation.
        rel = p.relative_to(root)
        parts = rel.parts
        if ".git" in parts:
            continue
        paths.add(str(rel))
    return paths


# ---------------------------------------------------------------------------
# 7. Validation: missing .claude/skills/ or PROJECT.md
# ---------------------------------------------------------------------------


class TestValidation:
    """install exits 2 when the root lacks required structure."""

    def test_missing_skills_dir(self, tmp_path: Path) -> None:
        """install fails when .claude/skills/ is absent."""
        root = tmp_path / "no_skills"
        root.mkdir()
        (root / "PROJECT.md").write_text("# proj\n", encoding="utf-8")
        # intentionally no .claude/skills/

        runner = CliRunner()
        result = runner.invoke(
            beril_cmd,
            ["install", str(root)],
            catch_exceptions=False,
        )
        assert result.exit_code == 2

    def test_missing_project_md(self, tmp_path: Path) -> None:
        """install fails when PROJECT.md is absent."""
        root = tmp_path / "no_project_md"
        root.mkdir()
        (root / ".claude" / "skills").mkdir(parents=True)
        # intentionally no PROJECT.md

        runner = CliRunner()
        result = runner.invoke(
            beril_cmd,
            ["install", str(root)],
            catch_exceptions=False,
        )
        assert result.exit_code == 2
