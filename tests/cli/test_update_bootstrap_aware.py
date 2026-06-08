"""Tests for the bootstrap-aware ``--add-untracked`` filter in ``kbu update``.

Covers the four scenarios required by the kbu-bootstrap-v1 PRD task
p1-update-bootstrap-aware:

  (a) Non-empty file_hashes + no --add-untracked
      -> 'added' entries for paths absent from file_hashes are filtered out.

  (b) Non-empty file_hashes + --add-untracked
      -> All source-template additions are emitted.

  (c) Empty file_hashes (legacy new-project repo)
      -> All source-template additions are emitted (old behaviour preserved).

  (d) Existing 'modified' diff behaviour unchanged across all three scenarios.

Also covers:
  - CLI flag presence in --help output.
  - --check and --yes mutual exclusion still holds (regression check).
  - _build_diff signature has file_hashes and add_untracked parameters.
  - After a successful --add-untracked run that adds new files, those files
    appear in the recomputed [update.file_hashes].
"""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import tomllib
from click.testing import CliRunner

from kbutillib.cli import main
from kbutillib.cli.manifest import now_utc_iso, sha256_file, write_project_manifest
from kbutillib.cli.update import (
    TemplateDiff,
    _build_diff,
    update,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prefixed(hex_str: str) -> str:
    return f"sha256:{hex_str}"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
            # None means "use default empty dict" so callers can distinguish
            # "explicitly pass empty" from "don't pass at all"
            "file_hashes": file_hashes if file_hashes is not None else {},
        },
    }
    write_project_manifest(project_root, cfg)


def _make_source_with_two_template_files(tmp_path: Path) -> Path:
    """Create a stub source KBUtilLib with two template files.

    - ``.claude/commands/kbu-start.md`` (tracked by bootstrap)
    - ``.vscode/extensions.json``       (deliberately skipped by bootstrap)

    Returns the source root Path.
    """
    source = tmp_path / "KBUtilLib"
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
# Scenario (a): non-empty file_hashes + no add_untracked
#               -> 'added' entries absent from file_hashes are filtered out
# ---------------------------------------------------------------------------


class TestFilterAdded_NonEmptyHashes_NoAddUntracked:
    """file_hashes is non-empty, add_untracked is False (default).

    kbu-start.md IS in file_hashes; extensions.json is NOT.
    Expected: only kbu-start.md appears as 'added'; extensions.json is suppressed.
    """

    def _make_file_hashes_for_start_only(self, source: Path) -> dict[str, str]:
        start_path = (
            source / "templates" / "research-project" / ".claude" / "commands" / "kbu-start.md"
        )
        return {".claude/commands/kbu-start.md": _prefixed(sha256_file(start_path))}

    def test_build_diff_direct_filters_absent_path(self, tmp_path: Path) -> None:
        """_build_diff with non-empty file_hashes does not emit 'added' for paths absent."""
        source = _make_source_with_two_template_files(tmp_path)

        # file_hashes contains only kbu-start.md; extensions.json is absent.
        file_hashes = self._make_file_hashes_for_start_only(source)

        diffs = _build_diff(
            source=source,
            last_commit=None,     # first pull -> all current files are candidates
            current_commit="abc",
            file_hashes=file_hashes,
            add_untracked=False,
        )

        paths = {d.path for d in diffs}
        assert ".claude/commands/kbu-start.md" in paths, (
            "kbu-start.md should be proposed (it IS in file_hashes)"
        )
        assert ".vscode/extensions.json" not in paths, (
            "extensions.json should be suppressed (absent from file_hashes)"
        )

    def test_build_diff_all_added_entries_respect_filter(self, tmp_path: Path) -> None:
        """All 'added' entries that are filtered out have status='added'."""
        source = _make_source_with_two_template_files(tmp_path)
        # Pass an empty file_hashes to invert: both paths should be suppressed.
        diffs = _build_diff(
            source=source,
            last_commit=None,
            current_commit="abc",
            file_hashes={".claude/commands/kbu-start.md": "sha256:dummy"},
            add_untracked=False,
        )
        # extensions.json not in file_hashes -> must be absent
        assert all(d.path != ".vscode/extensions.json" for d in diffs if d.status == "added")

    def test_update_function_passes_file_hashes_to_build_diff(self, tmp_path: Path) -> None:
        """update() passes manifest file_hashes into _build_diff."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        source = _make_source_with_two_template_files(tmp_path)
        # Only kbu-start.md tracked
        start_abs = (
            source / "templates" / "research-project" / ".claude" / "commands" / "kbu-start.md"
        )
        file_hashes = {".claude/commands/kbu-start.md": _prefixed(sha256_file(start_abs))}
        _make_project_toml(project_root, source_path=str(source), file_hashes=file_hashes)

        captured: list[dict] = []

        original_build_diff = _build_diff.__wrapped__ if hasattr(_build_diff, "__wrapped__") else _build_diff

        def _spy_build_diff(source, last_commit, current_commit, file_hashes=None, add_untracked=False):
            captured.append({"file_hashes": file_hashes, "add_untracked": add_untracked})
            return []

        with (
            patch("kbutillib.cli.update._run_git") as mock_git,
            patch("kbutillib.cli.update._build_diff", side_effect=_spy_build_diff),
        ):
            mock_git.return_value = MagicMock(returncode=0, stdout="newsha\n", stderr="")
            update(check=True, project_root=project_root)

        assert captured, "Expected _build_diff to be called"
        assert captured[0]["file_hashes"] == file_hashes
        assert captured[0]["add_untracked"] is False

    def test_update_cli_does_not_propose_untracked_by_default(self, tmp_path: Path) -> None:
        """Via CLI: kbu update does NOT propose extensions.json when absent from file_hashes."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        source = _make_source_with_two_template_files(tmp_path)
        # file_hashes contains only kbu-start.md
        start_abs = (
            source / "templates" / "research-project" / ".claude" / "commands" / "kbu-start.md"
        )
        file_hashes = {".claude/commands/kbu-start.md": _prefixed(sha256_file(start_abs))}
        _make_project_toml(
            project_root, source_path=str(source), last_pulled_commit="", file_hashes=file_hashes
        )

        runner = CliRunner()
        with (
            patch("kbutillib.cli.update._run_git") as mock_git,
            patch("kbutillib.cli.update.Path.cwd", return_value=project_root),
        ):
            # Make git pull a no-op; rev-parse returns a commit
            mock_git.return_value = MagicMock(returncode=0, stdout="newsha\n", stderr="")
            result = runner.invoke(
                main,
                ["update", "--check"],
                catch_exceptions=False,
            )

        # extensions.json must NOT appear in the output
        assert "extensions.json" not in result.output, (
            f"extensions.json should be suppressed by default. Output:\n{result.output}"
        )


# ---------------------------------------------------------------------------
# Scenario (b): non-empty file_hashes + --add-untracked
#               -> all source-template additions are emitted
# ---------------------------------------------------------------------------


class TestFilterAdded_NonEmptyHashes_WithAddUntracked:
    """file_hashes is non-empty, add_untracked is True.

    kbu-start.md IS in file_hashes; extensions.json is NOT.
    Expected: both paths appear as 'added'.
    """

    def test_build_diff_direct_emits_all_when_add_untracked(self, tmp_path: Path) -> None:
        """_build_diff with add_untracked=True emits all source-template additions."""
        source = _make_source_with_two_template_files(tmp_path)
        start_abs = (
            source / "templates" / "research-project" / ".claude" / "commands" / "kbu-start.md"
        )
        file_hashes = {".claude/commands/kbu-start.md": _prefixed(sha256_file(start_abs))}

        diffs = _build_diff(
            source=source,
            last_commit=None,
            current_commit="abc",
            file_hashes=file_hashes,
            add_untracked=True,       # <--- override
        )

        paths = {d.path for d in diffs}
        assert ".claude/commands/kbu-start.md" in paths, "kbu-start.md should be in diffs"
        assert ".vscode/extensions.json" in paths, (
            "extensions.json should be in diffs when --add-untracked is set"
        )

    def test_update_function_with_add_untracked_proposes_all(self, tmp_path: Path) -> None:
        """update(add_untracked=True) passes add_untracked=True into _build_diff."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        source = _make_source_with_two_template_files(tmp_path)
        start_abs = (
            source / "templates" / "research-project" / ".claude" / "commands" / "kbu-start.md"
        )
        file_hashes = {".claude/commands/kbu-start.md": _prefixed(sha256_file(start_abs))}
        _make_project_toml(project_root, source_path=str(source), file_hashes=file_hashes)

        captured: list[dict] = []

        def _spy_build_diff(source, last_commit, current_commit, file_hashes=None, add_untracked=False):
            captured.append({"file_hashes": file_hashes, "add_untracked": add_untracked})
            return []

        with (
            patch("kbutillib.cli.update._run_git") as mock_git,
            patch("kbutillib.cli.update._build_diff", side_effect=_spy_build_diff),
        ):
            mock_git.return_value = MagicMock(returncode=0, stdout="newsha\n", stderr="")
            update(check=True, add_untracked=True, project_root=project_root)

        assert captured[0]["add_untracked"] is True

    def test_update_cli_add_untracked_flag_proposes_all(self, tmp_path: Path) -> None:
        """Via CLI: kbu update --add-untracked proposes extensions.json."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        source = _make_source_with_two_template_files(tmp_path)
        start_abs = (
            source / "templates" / "research-project" / ".claude" / "commands" / "kbu-start.md"
        )
        file_hashes = {".claude/commands/kbu-start.md": _prefixed(sha256_file(start_abs))}
        _make_project_toml(
            project_root, source_path=str(source), last_pulled_commit="", file_hashes=file_hashes
        )

        runner = CliRunner()
        with (
            patch("kbutillib.cli.update._run_git") as mock_git,
            patch("kbutillib.cli.update.Path.cwd", return_value=project_root),
        ):
            mock_git.return_value = MagicMock(returncode=0, stdout="newsha\n", stderr="")
            result = runner.invoke(
                main,
                ["update", "--check", "--add-untracked"],
                catch_exceptions=False,
            )

        # extensions.json MUST appear in the output (proposed as 'added')
        assert "extensions.json" in result.output, (
            f"extensions.json should appear with --add-untracked. Output:\n{result.output}"
        )


# ---------------------------------------------------------------------------
# Scenario (c): empty file_hashes (legacy new-project repo)
#               -> all source-template additions emitted (old behaviour)
# ---------------------------------------------------------------------------


class TestFilterAdded_EmptyHashes_LegacyBehaviour:
    """file_hashes is empty (or absent).  Legacy behaviour: all source files proposed."""

    def test_build_diff_direct_empty_hashes_no_filter(self, tmp_path: Path) -> None:
        """_build_diff with empty file_hashes emits all source-template additions."""
        source = _make_source_with_two_template_files(tmp_path)

        diffs = _build_diff(
            source=source,
            last_commit=None,
            current_commit="abc",
            file_hashes={},          # empty -> no filter
            add_untracked=False,
        )

        paths = {d.path for d in diffs}
        assert ".claude/commands/kbu-start.md" in paths
        assert ".vscode/extensions.json" in paths

    def test_build_diff_direct_none_hashes_no_filter(self, tmp_path: Path) -> None:
        """_build_diff with file_hashes=None (default) also emits all additions."""
        source = _make_source_with_two_template_files(tmp_path)

        diffs = _build_diff(
            source=source,
            last_commit=None,
            current_commit="abc",
            # file_hashes not passed -> defaults to None
        )

        paths = {d.path for d in diffs}
        assert ".claude/commands/kbu-start.md" in paths
        assert ".vscode/extensions.json" in paths

    def test_update_cli_empty_hashes_proposes_all(self, tmp_path: Path) -> None:
        """Via CLI: legacy repo with empty file_hashes proposes all template files."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        source = _make_source_with_two_template_files(tmp_path)
        # Empty file_hashes -> legacy mode
        _make_project_toml(
            project_root,
            source_path=str(source),
            last_pulled_commit="",
            file_hashes={},
        )

        runner = CliRunner()
        with (
            patch("kbutillib.cli.update._run_git") as mock_git,
            patch("kbutillib.cli.update.Path.cwd", return_value=project_root),
        ):
            mock_git.return_value = MagicMock(returncode=0, stdout="newsha\n", stderr="")
            result = runner.invoke(
                main,
                ["update", "--check"],
                catch_exceptions=False,
            )

        # Both files should be proposed
        assert "kbu-start.md" in result.output, (
            f"kbu-start.md should be in output. Got:\n{result.output}"
        )
        assert "extensions.json" in result.output, (
            f"extensions.json should be in output (legacy empty-hashes). Got:\n{result.output}"
        )


# ---------------------------------------------------------------------------
# Scenario (d): 'modified' diff behaviour unchanged across all three scenarios
# ---------------------------------------------------------------------------


class TestModifiedBehaviourUnchanged:
    """Verify that 'modified' entries are always emitted, regardless of
    file_hashes contents or the add_untracked flag.

    Modified entries exist in old_hashes (not in the "new additions" path),
    so the bootstrap filter must never touch them.
    """

    def _make_source_with_modified_file(self, tmp_path: Path) -> tuple[Path, str]:
        """Create a source where kbu-start.md has changed since last_commit.

        Returns (source_path, last_commit_sha_hex).  We fake the last commit
        by hashing the "old" content and storing that as the old hash.
        """
        source = tmp_path / "KBUtilLib"
        (source / ".git").mkdir(parents=True)
        tmpl = source / "templates" / "research-project"
        (tmpl / ".claude" / "commands").mkdir(parents=True)
        (tmpl / ".vscode").mkdir(parents=True)
        (tmpl / ".claude" / "commands" / "kbu-start.md").write_text(
            "# kbu-start v2 NEW\n", encoding="utf-8"
        )
        (tmpl / ".vscode" / "extensions.json").write_text(
            '{"recommendations": []}', encoding="utf-8"
        )
        return source

    def _patch_old_hash(self, source: Path, rel_path: str, old_content: bytes):
        """Return a patcher for _git_show_file that returns old_content for rel_path."""
        full = f"templates/research-project/{rel_path}"

        def _fake_show(repo, commit, relpath):
            if relpath == full:
                return old_content
            return None

        return patch("kbutillib.cli.update._git_show_file", side_effect=_fake_show)

    def test_modified_emitted_with_non_empty_hashes_no_add_untracked(self, tmp_path: Path) -> None:
        """Modified files are emitted even when file_hashes is non-empty + no add_untracked."""
        source = self._make_source_with_modified_file(tmp_path)
        old_content = b"# kbu-start v1 OLD\n"
        # file_hashes has kbu-start.md (modified) but NOT extensions.json (skipped)
        start_abs = source / "templates" / "research-project" / ".claude" / "commands" / "kbu-start.md"
        old_hex = _sha256_bytes(old_content)
        file_hashes = {".claude/commands/kbu-start.md": _prefixed(sha256_file(start_abs))}

        with self._patch_old_hash(source, ".claude/commands/kbu-start.md", old_content):
            # Also need to patch the git diff --name-status call to return nothing
            with patch("kbutillib.cli.update._run_git") as mock_git:
                mock_git.return_value = MagicMock(returncode=0, stdout="", stderr="")
                diffs = _build_diff(
                    source=source,
                    last_commit="oldcommit",
                    current_commit="newcommit",
                    file_hashes=file_hashes,
                    add_untracked=False,
                )

        modified = [d for d in diffs if d.status == "modified"]
        assert any(d.path == ".claude/commands/kbu-start.md" for d in modified), (
            "kbu-start.md should be 'modified' when its content changed"
        )

    def test_modified_emitted_with_non_empty_hashes_with_add_untracked(self, tmp_path: Path) -> None:
        """Modified files are emitted when file_hashes non-empty + add_untracked=True."""
        source = self._make_source_with_modified_file(tmp_path)
        old_content = b"# kbu-start v1 OLD\n"
        start_abs = source / "templates" / "research-project" / ".claude" / "commands" / "kbu-start.md"
        file_hashes = {".claude/commands/kbu-start.md": _prefixed(sha256_file(start_abs))}

        with self._patch_old_hash(source, ".claude/commands/kbu-start.md", old_content):
            with patch("kbutillib.cli.update._run_git") as mock_git:
                mock_git.return_value = MagicMock(returncode=0, stdout="", stderr="")
                diffs = _build_diff(
                    source=source,
                    last_commit="oldcommit",
                    current_commit="newcommit",
                    file_hashes=file_hashes,
                    add_untracked=True,
                )

        modified = [d for d in diffs if d.status == "modified"]
        assert any(d.path == ".claude/commands/kbu-start.md" for d in modified)

    def test_modified_emitted_with_empty_hashes(self, tmp_path: Path) -> None:
        """Modified files are emitted when file_hashes is empty (legacy mode)."""
        source = self._make_source_with_modified_file(tmp_path)
        old_content = b"# kbu-start v1 OLD\n"
        start_abs = source / "templates" / "research-project" / ".claude" / "commands" / "kbu-start.md"

        with self._patch_old_hash(source, ".claude/commands/kbu-start.md", old_content):
            with patch("kbutillib.cli.update._run_git") as mock_git:
                mock_git.return_value = MagicMock(returncode=0, stdout="", stderr="")
                diffs = _build_diff(
                    source=source,
                    last_commit="oldcommit",
                    current_commit="newcommit",
                    file_hashes={},
                    add_untracked=False,
                )

        modified = [d for d in diffs if d.status == "modified"]
        assert any(d.path == ".claude/commands/kbu-start.md" for d in modified)

    def test_extensions_json_never_modified_if_not_in_old_hashes(self, tmp_path: Path) -> None:
        """Extensions.json that's only in current but not in old commit stays 'added'
        (filtered or not), never becomes 'modified'.
        """
        source = self._make_source_with_modified_file(tmp_path)
        # Only kbu-start.md was in the "old" commit; extensions.json is brand-new.
        old_content = b"# kbu-start v1 OLD\n"

        with self._patch_old_hash(source, ".claude/commands/kbu-start.md", old_content):
            with patch("kbutillib.cli.update._run_git") as mock_git:
                mock_git.return_value = MagicMock(returncode=0, stdout="", stderr="")
                diffs = _build_diff(
                    source=source,
                    last_commit="oldcommit",
                    current_commit="newcommit",
                    file_hashes={".claude/commands/kbu-start.md": "sha256:x"},
                    add_untracked=False,
                )

        # extensions.json not in file_hashes -> suppressed entirely (not 'modified')
        extensions_entries = [d for d in diffs if d.path == ".vscode/extensions.json"]
        assert not extensions_entries, (
            "extensions.json should be fully absent when not in file_hashes + no add_untracked"
        )


# ---------------------------------------------------------------------------
# After --add-untracked run: newly-added files appear in recomputed file_hashes
# ---------------------------------------------------------------------------


class TestAddUntrackedUpdatesFileHashes:
    """After a successful kbu update --add-untracked run that adds new files,
    those files are included in the recomputed [update.file_hashes].

    _recompute_file_hashes walks the tracked dirs unconditionally, so newly
    copied files automatically end up in the hash map.
    """

    def test_newly_added_file_included_after_add_untracked(self, tmp_path: Path) -> None:
        project_root = tmp_path / "proj"
        (project_root / ".claude" / "commands").mkdir(parents=True)
        (project_root / ".vscode").mkdir(parents=True)
        source = tmp_path / "KBUtilLib"
        (source / ".git").mkdir(parents=True)
        tmpl = source / "templates" / "research-project"
        (tmpl / ".claude" / "commands").mkdir(parents=True)
        (tmpl / ".vscode").mkdir(parents=True)
        (tmpl / ".claude" / "commands" / "kbu-start.md").write_text(
            "# kbu-start\n", encoding="utf-8"
        )
        (tmpl / ".vscode" / "extensions.json").write_text(
            '{"recommendations": []}', encoding="utf-8"
        )

        # Project already has kbu-start.md; extensions.json is new (not in file_hashes).
        (project_root / ".claude" / "commands" / "kbu-start.md").write_text(
            "# kbu-start\n", encoding="utf-8"
        )
        start_hash = _prefixed(sha256_file(project_root / ".claude" / "commands" / "kbu-start.md"))
        _make_project_toml(
            project_root,
            source_path=str(source),
            file_hashes={".claude/commands/kbu-start.md": start_hash},
        )

        # Diff that _build_diff would return when add_untracked=True:
        # extensions.json is 'added'.
        ext_source_hash = _prefixed(
            sha256_file(tmpl / ".vscode" / "extensions.json")
        )
        fake_diff = [
            TemplateDiff(
                path=".vscode/extensions.json",
                status="added",
                old_hash=None,
                new_hash=ext_source_hash,
            )
        ]

        def _fake_apply(src, diff, proj_root):
            # Simulate copying extensions.json into project
            dest = proj_root / ".vscode" / "extensions.json"
            (tmpl / ".vscode" / "extensions.json").read_bytes()
            dest.write_bytes((tmpl / ".vscode" / "extensions.json").read_bytes())

        with (
            patch("kbutillib.cli.update._run_git") as mock_git,
            patch("kbutillib.cli.update._build_diff", return_value=fake_diff),
            patch("kbutillib.cli.update._apply_diff", side_effect=_fake_apply),
        ):
            mock_git.return_value = MagicMock(returncode=0, stdout="newsha\n", stderr="")
            update(yes=True, add_untracked=True, project_root=project_root)

        with open(project_root / "kbu-project.toml", "rb") as fh:
            cfg = tomllib.load(fh)

        hashes = cfg["update"]["file_hashes"]
        assert ".vscode/extensions.json" in hashes, (
            "extensions.json should appear in [update.file_hashes] after --add-untracked run"
        )
        assert ".claude/commands/kbu-start.md" in hashes, (
            "kbu-start.md should still be in [update.file_hashes]"
        )


# ---------------------------------------------------------------------------
# CLI surface checks
# ---------------------------------------------------------------------------


class TestCLISurface:
    def test_add_untracked_appears_in_help(self) -> None:
        """kbu update --help shows --add-untracked."""
        runner = CliRunner()
        result = runner.invoke(main, ["update", "--help"])
        assert result.exit_code == 0
        assert "--add-untracked" in result.output, (
            f"--add-untracked not found in help. Got:\n{result.output}"
        )

    def test_check_yes_mutual_exclusion_still_holds(self, tmp_path: Path) -> None:
        """Regression: --check and --yes remain mutually exclusive."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["update", "--check", "--yes"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0

    def test_check_and_add_untracked_compose_freely(self, tmp_path: Path) -> None:
        """--check and --add-untracked compose without error."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        source = _make_source_with_two_template_files(tmp_path)
        _make_project_toml(project_root, source_path=str(source), file_hashes={})

        runner = CliRunner()
        with (
            patch("kbutillib.cli.update._run_git") as mock_git,
            patch("kbutillib.cli.update.Path.cwd", return_value=project_root),
        ):
            mock_git.return_value = MagicMock(returncode=0, stdout="newsha\n", stderr="")
            result = runner.invoke(
                main,
                ["update", "--check", "--add-untracked"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, f"--check --add-untracked should not error. Got:\n{result.output}"

    def test_yes_and_add_untracked_compose_freely(self, tmp_path: Path) -> None:
        """--yes and --add-untracked compose without error."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        source = _make_source_with_two_template_files(tmp_path)
        _make_project_toml(project_root, source_path=str(source), file_hashes={})

        with (
            patch("kbutillib.cli.update._run_git") as mock_git,
            patch("kbutillib.cli.update._build_diff", return_value=[]),
        ):
            mock_git.return_value = MagicMock(returncode=0, stdout="newsha\n", stderr="")
            # Should not raise
            update(yes=True, add_untracked=True, project_root=project_root)


# ---------------------------------------------------------------------------
# _build_diff signature check
# ---------------------------------------------------------------------------


class TestBuildDiffSignature:
    def test_file_hashes_and_add_untracked_in_signature(self) -> None:
        """_build_diff has file_hashes and add_untracked in its signature."""
        sig = inspect.signature(_build_diff)
        assert "file_hashes" in sig.parameters, (
            "_build_diff must have a file_hashes parameter"
        )
        assert "add_untracked" in sig.parameters, (
            "_build_diff must have an add_untracked parameter"
        )

    def test_file_hashes_defaults_to_none(self) -> None:
        param = inspect.signature(_build_diff).parameters["file_hashes"]
        assert param.default is None

    def test_add_untracked_defaults_to_false(self) -> None:
        param = inspect.signature(_build_diff).parameters["add_untracked"]
        assert param.default is False
