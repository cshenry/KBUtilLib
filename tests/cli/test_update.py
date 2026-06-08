"""Tests for kbutillib.cli.update — ``kbu update`` subcommand."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import tomllib
from click.testing import CliRunner

from kbutillib.cli import main
from kbutillib.cli.manifest import (
    now_utc_iso,
    sha256_file,
    write_project_manifest,
)
from kbutillib.cli.update import (
    TemplateDiff,
    _apply_diff,
    _build_diff,
    _detect_locally_modified,
    _format_diff_summary,
    _recompute_file_hashes,
    update,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _prefixed(hex_str: str) -> str:
    return f"sha256:{hex_str}"


def _make_project_toml(
    project_root: Path,
    source_path: str = "/fake/kbutillib",
    source_commit: str = "abc123",
    last_pulled_commit: str = "abc123",
    file_hashes: dict | None = None,
) -> None:
    """Write a minimal kbu-project.toml to *project_root*."""
    now = now_utc_iso()
    cfg = {
        "project": {
            "name": "test_project",
            "created_at": now,
            "authors": [{"name": "Test", "affiliation": "X", "orcid": "0"}],
        },
        "kbutillib": {
            "source_path": source_path,
            "source_commit": source_commit,
        },
        "update": {
            "last_pulled_at": now,
            "last_pulled_commit": last_pulled_commit,
            "file_hashes": file_hashes or {},
        },
    }
    write_project_manifest(project_root, cfg)


def _make_source_with_template(tmp_path: Path, name: str = "KBUtilLib") -> Path:
    """Create a stub source KBUtilLib directory with template files."""
    source = tmp_path / name
    (source / ".git").mkdir(parents=True)
    tmpl = source / "templates" / "research-project"
    (tmpl / ".claude" / "commands").mkdir(parents=True)
    (tmpl / ".vscode").mkdir(parents=True)
    (tmpl / ".claude" / "commands" / "kbu-start.md").write_text(
        "# kbu-start v2\n", encoding="utf-8"
    )
    (tmpl / ".vscode" / "extensions.json").write_text(
        '{"recommendations": ["anthropic.claude-code"]}', encoding="utf-8"
    )
    return source


# ---------------------------------------------------------------------------
# TemplateDiff dataclass
# ---------------------------------------------------------------------------


class TestTemplateDiff:
    def test_fields(self) -> None:
        d = TemplateDiff(
            path=".claude/commands/kbu-start.md",
            status="modified",
            old_hash="sha256:abc",
            new_hash="sha256:def",
        )
        assert d.path == ".claude/commands/kbu-start.md"
        assert d.status == "modified"
        assert d.old_hash == "sha256:abc"
        assert d.new_hash == "sha256:def"


# ---------------------------------------------------------------------------
# --check + --yes mutually exclusive
# ---------------------------------------------------------------------------


class TestCheckYesMutuallyExclusive:
    def test_check_and_yes_exit_nonzero(self, tmp_path: Path) -> None:
        """--check and --yes together must exit non-zero."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        _make_project_toml(project_root)

        with pytest.raises(SystemExit) as exc:
            update(check=True, yes=True, project_root=project_root)

        assert exc.value.code != 0

    def test_check_and_yes_via_cli(self, tmp_path: Path) -> None:
        """CLI: kbu update --check --yes exits non-zero."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["update", "--check", "--yes"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0

    def test_check_alone_is_ok(self, tmp_path: Path) -> None:
        """--check without --yes is not an error in itself."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        source = _make_source_with_template(tmp_path)
        _make_project_toml(project_root, source_path=str(source))

        # Simulate no diff (already up to date)
        with (
            patch("kbutillib.cli.update._run_git") as mock_git,
            patch("kbutillib.cli.update._build_diff", return_value=[]),
        ):
            mock_git.return_value = MagicMock(returncode=0, stdout="abc123\n", stderr="")
            # Should not raise
            update(check=True, yes=False, project_root=project_root)

    def test_yes_alone_is_ok(self, tmp_path: Path) -> None:
        """--yes without --check is not an error."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        source = _make_source_with_template(tmp_path)
        _make_project_toml(project_root, source_path=str(source))

        with (
            patch("kbutillib.cli.update._run_git") as mock_git,
            patch("kbutillib.cli.update._build_diff", return_value=[]),
        ):
            mock_git.return_value = MagicMock(returncode=0, stdout="abc123\n", stderr="")
            # Should not raise
            update(check=False, yes=True, project_root=project_root)


# ---------------------------------------------------------------------------
# --set-source
# ---------------------------------------------------------------------------


class TestSetSource:
    def test_set_source_rewrites_toml(self, tmp_path: Path) -> None:
        """--set-source updates source_path in kbu-project.toml."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        _make_project_toml(project_root, source_path="/old/path")

        new_source = tmp_path / "new_kbutillib"
        new_source.mkdir()

        update(set_source=new_source, project_root=project_root)

        with open(project_root / "kbu-project.toml", "rb") as fh:
            cfg = tomllib.load(fh)

        assert cfg["kbutillib"]["source_path"] == str(new_source)

    def test_set_source_clears_last_pulled_commit(self, tmp_path: Path) -> None:
        """--set-source clears last_pulled_commit so next update re-evaluates."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        _make_project_toml(project_root, last_pulled_commit="deadbeef")

        new_source = tmp_path / "new_kbutillib"
        new_source.mkdir()

        update(set_source=new_source, project_root=project_root)

        with open(project_root / "kbu-project.toml", "rb") as fh:
            cfg = tomllib.load(fh)

        assert cfg["update"]["last_pulled_commit"] == ""

    def test_set_source_exits_without_applying_diff(self, tmp_path: Path) -> None:
        """--set-source should not attempt to apply any diff."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        _make_project_toml(project_root)

        new_source = tmp_path / "new_kbutillib"
        new_source.mkdir()

        with patch("kbutillib.cli.update._build_diff") as mock_diff:
            update(set_source=new_source, project_root=project_root)
            mock_diff.assert_not_called()


# ---------------------------------------------------------------------------
# Missing source path
# ---------------------------------------------------------------------------


class TestMissingSource:
    def test_missing_source_exits_with_message(self, tmp_path: Path) -> None:
        """If source_path on disk is absent, exits 1 with helpful message."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        _make_project_toml(project_root, source_path="/nonexistent/path/kbutillib")

        with pytest.raises(SystemExit) as exc:
            update(project_root=project_root)

        assert exc.value.code == 1

    def test_missing_source_message_contains_path(self, tmp_path: Path) -> None:
        """Error message names the missing path."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        _make_project_toml(project_root, source_path="/very/specific/missing/path")

        runner = CliRunner()
        with patch("kbutillib.cli.update.Path.cwd", return_value=project_root):
            result = runner.invoke(
                main,
                ["update"],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        # Output (stdout or stderr) should mention the missing path
        combined = result.output
        assert "/very/specific/missing/path" in combined or "not found" in combined.lower()


# ---------------------------------------------------------------------------
# --check dry-run
# ---------------------------------------------------------------------------


class TestCheckDryRun:
    def test_check_does_not_write(self, tmp_path: Path) -> None:
        """--check does not modify kbu-project.toml."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        source = _make_source_with_template(tmp_path)
        _make_project_toml(project_root, source_path=str(source), last_pulled_commit="oldsha")

        # Record original mtime
        toml_path = project_root / "kbu-project.toml"
        original_mtime = toml_path.stat().st_mtime

        diff = [
            TemplateDiff(
                path=".claude/commands/kbu-start.md",
                status="modified",
                old_hash="sha256:old",
                new_hash="sha256:new",
            )
        ]

        with (
            patch("kbutillib.cli.update._run_git") as mock_git,
            patch("kbutillib.cli.update._build_diff", return_value=diff),
        ):
            mock_git.return_value = MagicMock(returncode=0, stdout="newsha\n", stderr="")
            update(check=True, project_root=project_root)

        # TOML should not have changed
        assert toml_path.stat().st_mtime == original_mtime

    def test_check_prints_diff_summary(self, tmp_path: Path) -> None:
        """--check prints the diff summary."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        source = _make_source_with_template(tmp_path)
        _make_project_toml(project_root, source_path=str(source))

        diff = [
            TemplateDiff(".claude/commands/kbu-start.md", "modified", "sha256:a", "sha256:b"),
        ]

        runner = CliRunner()
        with (
            patch("kbutillib.cli.update._run_git") as mock_git,
            patch("kbutillib.cli.update._build_diff", return_value=diff),
            patch("kbutillib.cli.update.Path.cwd", return_value=project_root),
        ):
            mock_git.return_value = MagicMock(returncode=0, stdout="newsha\n", stderr="")
            result = runner.invoke(
                main,
                ["update", "--check"],
                catch_exceptions=False,
            )

        assert "kbu-start.md" in result.output or "MODIFIED" in result.output.upper()


# ---------------------------------------------------------------------------
# Locally-modified detection
# ---------------------------------------------------------------------------


class TestLocallyModifiedDetection:
    def test_unmodified_file_not_flagged(self, tmp_path: Path) -> None:
        """A file matching its recorded hash is not flagged."""
        project_root = tmp_path / "proj"
        (project_root / ".claude" / "commands").mkdir(parents=True)

        content = "# original content\n"
        f = project_root / ".claude" / "commands" / "kbu-start.md"
        f.write_text(content, encoding="utf-8")

        recorded_hex = sha256_file(f)
        file_hashes = {".claude/commands/kbu-start.md": _prefixed(recorded_hex)}
        diff = [TemplateDiff(".claude/commands/kbu-start.md", "modified", "sha256:old", "sha256:new")]

        modified = _detect_locally_modified(project_root, file_hashes, diff)
        assert ".claude/commands/kbu-start.md" not in modified

    def test_modified_file_is_flagged(self, tmp_path: Path) -> None:
        """A file whose content changed after pull is flagged as locally_modified."""
        project_root = tmp_path / "proj"
        (project_root / ".claude" / "commands").mkdir(parents=True)

        original = "# original content\n"
        f = project_root / ".claude" / "commands" / "kbu-start.md"
        f.write_text(original, encoding="utf-8")

        # Record hash of original content
        original_hex = sha256_file(f)
        file_hashes = {".claude/commands/kbu-start.md": _prefixed(original_hex)}

        # Simulate user modifying the file after initial pull
        f.write_text("# MODIFIED BY STUDENT\n", encoding="utf-8")

        diff = [TemplateDiff(".claude/commands/kbu-start.md", "modified", "sha256:old", "sha256:new")]

        modified = _detect_locally_modified(project_root, file_hashes, diff)
        assert ".claude/commands/kbu-start.md" in modified

    def test_file_not_in_diff_not_checked(self, tmp_path: Path) -> None:
        """A locally-modified file that the diff doesn't touch is not flagged."""
        project_root = tmp_path / "proj"
        (project_root / ".claude" / "commands").mkdir(parents=True)

        f = project_root / ".claude" / "commands" / "kbu-diagnose.md"
        f.write_text("# modified by researcher\n", encoding="utf-8")

        # Record different hash (simulate modification)
        file_hashes = {".claude/commands/kbu-diagnose.md": _prefixed("aabbcc")}

        # But the diff only touches kbu-start.md
        diff = [TemplateDiff(".claude/commands/kbu-start.md", "modified", "sha256:x", "sha256:y")]

        modified = _detect_locally_modified(project_root, file_hashes, diff)
        assert ".claude/commands/kbu-diagnose.md" not in modified


# ---------------------------------------------------------------------------
# Clobber-with-warn behaviour
# ---------------------------------------------------------------------------


class TestClobberWithWarn:
    def test_yes_bypasses_prompt(self, tmp_path: Path) -> None:
        """--yes applies diff without prompting even if files are locally modified."""
        project_root = tmp_path / "proj"
        (project_root / ".claude" / "commands").mkdir(parents=True)
        source = _make_source_with_template(tmp_path)

        f = project_root / ".claude" / "commands" / "kbu-start.md"
        f.write_text("# modified by researcher\n", encoding="utf-8")

        recorded_hex = sha256_file(f)
        # Simulate modification: record a different hash so it looks locally modified
        fake_hash = _prefixed("0" * 64)

        _make_project_toml(
            project_root,
            source_path=str(source),
            file_hashes={".claude/commands/kbu-start.md": fake_hash},
        )

        diff = [
            TemplateDiff(
                ".claude/commands/kbu-start.md",
                "modified",
                "sha256:old",
                "sha256:new",
            )
        ]

        with (
            patch("kbutillib.cli.update._run_git") as mock_git,
            patch("kbutillib.cli.update._build_diff", return_value=diff),
            patch("kbutillib.cli.update._apply_diff") as mock_apply,
            patch("kbutillib.cli.update._recompute_file_hashes", return_value={}),
        ):
            mock_git.return_value = MagicMock(returncode=0, stdout="newsha\n", stderr="")
            # With --yes, no prompt should be raised; apply_diff should be called
            update(yes=True, project_root=project_root)

        mock_apply.assert_called_once()

    def test_prompt_fires_for_locally_modified(self, tmp_path: Path) -> None:
        """When locally-modified files would be overwritten, prompt fires without --yes."""
        project_root = tmp_path / "proj"
        (project_root / ".claude" / "commands").mkdir(parents=True)
        source = _make_source_with_template(tmp_path)

        f = project_root / ".claude" / "commands" / "kbu-start.md"
        f.write_text("# modified by researcher\n", encoding="utf-8")
        fake_hash = _prefixed("0" * 64)  # different from actual

        _make_project_toml(
            project_root,
            source_path=str(source),
            file_hashes={".claude/commands/kbu-start.md": fake_hash},
        )

        diff = [
            TemplateDiff(
                ".claude/commands/kbu-start.md",
                "modified",
                "sha256:old",
                "sha256:new",
            )
        ]

        prompt_called = []

        with (
            patch("kbutillib.cli.update._run_git") as mock_git,
            patch("kbutillib.cli.update._build_diff", return_value=diff),
            patch("kbutillib.cli.update._apply_diff"),
            patch("kbutillib.cli.update._recompute_file_hashes", return_value={}),
            patch("click.prompt", side_effect=lambda *a, **kw: prompt_called.append(True) or "n") as mock_prompt,
        ):
            mock_git.return_value = MagicMock(returncode=0, stdout="newsha\n", stderr="")
            update(yes=False, project_root=project_root)

        assert len(prompt_called) > 0, "Prompt should have been called for locally-modified file"

    def test_abort_on_n(self, tmp_path: Path) -> None:
        """When user answers 'n' to overwrite prompt, update is aborted."""
        project_root = tmp_path / "proj"
        (project_root / ".claude" / "commands").mkdir(parents=True)
        source = _make_source_with_template(tmp_path)

        f = project_root / ".claude" / "commands" / "kbu-start.md"
        f.write_text("# modified by researcher\n", encoding="utf-8")
        fake_hash = _prefixed("0" * 64)

        _make_project_toml(
            project_root,
            source_path=str(source),
            file_hashes={".claude/commands/kbu-start.md": fake_hash},
        )

        diff = [
            TemplateDiff(
                ".claude/commands/kbu-start.md",
                "modified",
                "sha256:old",
                "sha256:new",
            )
        ]

        toml_before = (project_root / "kbu-project.toml").read_bytes()

        with (
            patch("kbutillib.cli.update._run_git") as mock_git,
            patch("kbutillib.cli.update._build_diff", return_value=diff),
            patch("kbutillib.cli.update._apply_diff") as mock_apply,
            patch("kbutillib.cli.update._recompute_file_hashes", return_value={}),
            patch("click.prompt", return_value="n"),
        ):
            mock_git.return_value = MagicMock(returncode=0, stdout="newsha\n", stderr="")
            update(yes=False, project_root=project_root)

        # apply_diff should NOT have been called
        mock_apply.assert_not_called()

        # TOML should be unchanged
        assert (project_root / "kbu-project.toml").read_bytes() == toml_before


# ---------------------------------------------------------------------------
# Post-apply file_hashes recomputed
# ---------------------------------------------------------------------------


class TestPostApplyHashes:
    def test_file_hashes_recomputed_after_update(self, tmp_path: Path) -> None:
        """After successful update, [update.file_hashes] is recomputed."""
        project_root = tmp_path / "proj"
        (project_root / ".claude" / "commands").mkdir(parents=True)
        (project_root / ".vscode").mkdir(parents=True)
        source = _make_source_with_template(tmp_path)

        (project_root / ".claude" / "commands" / "kbu-start.md").write_text(
            "# old content\n", encoding="utf-8"
        )
        (project_root / ".vscode" / "extensions.json").write_text(
            "{}", encoding="utf-8"
        )

        old_hash = _prefixed(sha256_file(project_root / ".claude" / "commands" / "kbu-start.md"))
        _make_project_toml(
            project_root,
            source_path=str(source),
            file_hashes={
                ".claude/commands/kbu-start.md": old_hash,
                ".vscode/extensions.json": _prefixed(sha256_file(project_root / ".vscode" / "extensions.json")),
            },
        )

        diff = [
            TemplateDiff(
                ".claude/commands/kbu-start.md",
                "modified",
                old_hash,
                "sha256:new",
            )
        ]

        def _fake_apply(source_arg, diff_arg, project_root_arg):
            # Simulate the file being updated
            (project_root_arg / ".claude" / "commands" / "kbu-start.md").write_text(
                "# new content from update\n", encoding="utf-8"
            )

        with (
            patch("kbutillib.cli.update._run_git") as mock_git,
            patch("kbutillib.cli.update._build_diff", return_value=diff),
            patch("kbutillib.cli.update._apply_diff", side_effect=_fake_apply),
        ):
            mock_git.return_value = MagicMock(returncode=0, stdout="newsha123\n", stderr="")
            update(yes=True, project_root=project_root)

        with open(project_root / "kbu-project.toml", "rb") as fh:
            cfg = tomllib.load(fh)

        hashes = cfg["update"]["file_hashes"]
        new_hex = sha256_file(project_root / ".claude" / "commands" / "kbu-start.md")
        assert hashes[".claude/commands/kbu-start.md"] == _prefixed(new_hex)
        assert cfg["update"]["last_pulled_commit"] == "newsha123"


# ---------------------------------------------------------------------------
# _apply_diff
# ---------------------------------------------------------------------------


class TestApplyDiff:
    def test_copies_added_file(self, tmp_path: Path) -> None:
        """_apply_diff copies an 'added' file from source template to project."""
        source = tmp_path / "src"
        (source / "templates" / "research-project" / ".claude" / "commands").mkdir(parents=True)
        new_file = source / "templates" / "research-project" / ".claude" / "commands" / "kbu-new.md"
        new_file.write_text("# new skill\n", encoding="utf-8")

        project = tmp_path / "proj"
        (project / ".claude" / "commands").mkdir(parents=True)

        diff = [TemplateDiff(".claude/commands/kbu-new.md", "added", None, "sha256:abc")]
        _apply_diff(source, diff, project)

        dest = project / ".claude" / "commands" / "kbu-new.md"
        assert dest.exists()
        assert dest.read_text(encoding="utf-8") == "# new skill\n"

    def test_copies_modified_file(self, tmp_path: Path) -> None:
        """_apply_diff overwrites a 'modified' file."""
        source = tmp_path / "src"
        (source / "templates" / "research-project" / ".claude" / "commands").mkdir(parents=True)
        (source / "templates" / "research-project" / ".claude" / "commands" / "kbu-start.md").write_text(
            "# updated content\n", encoding="utf-8"
        )

        project = tmp_path / "proj"
        (project / ".claude" / "commands").mkdir(parents=True)
        (project / ".claude" / "commands" / "kbu-start.md").write_text(
            "# old content\n", encoding="utf-8"
        )

        diff = [TemplateDiff(".claude/commands/kbu-start.md", "modified", "sha256:old", "sha256:new")]
        _apply_diff(source, diff, project)

        content = (project / ".claude" / "commands" / "kbu-start.md").read_text(encoding="utf-8")
        assert "updated content" in content

    def test_deletes_removed_file(self, tmp_path: Path) -> None:
        """_apply_diff removes a 'deleted' file from the project."""
        source = tmp_path / "src"
        source.mkdir(parents=True)

        project = tmp_path / "proj"
        (project / ".claude" / "commands").mkdir(parents=True)
        to_delete = project / ".claude" / "commands" / "kbu-old.md"
        to_delete.write_text("# old skill\n", encoding="utf-8")

        diff = [TemplateDiff(".claude/commands/kbu-old.md", "deleted", "sha256:abc", None)]
        _apply_diff(source, diff, project)

        assert not to_delete.exists()


# ---------------------------------------------------------------------------
# _format_diff_summary
# ---------------------------------------------------------------------------


class TestFormatDiffSummary:
    def test_empty_diff(self) -> None:
        assert _format_diff_summary([]) == "Already up-to-date."

    def test_non_empty_diff_contains_path(self) -> None:
        diff = [
            TemplateDiff(".claude/commands/kbu-start.md", "modified", "sha256:a", "sha256:b"),
            TemplateDiff(".vscode/extensions.json", "added", None, "sha256:c"),
        ]
        summary = _format_diff_summary(diff)
        assert "kbu-start.md" in summary
        assert "extensions.json" in summary
        assert "2" in summary  # file count


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------


class TestUpdateCLIRegistration:
    def test_update_registered(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["update", "--help"])
        assert result.exit_code == 0
        assert "--check" in result.output
        assert "--yes" in result.output
        assert "--set-source" in result.output
