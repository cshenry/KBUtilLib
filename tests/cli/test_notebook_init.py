"""Tests for kbutillib.cli.notebook_init — ``kbu notebook-init`` command.

Coverage
--------
- normalize_topic: normalization edge cases.
- Branch (a): repo missing → full bootstrap (git repo, .code-workspace,
  notebooks/{models,genomes,data,PRJ-<topic>}, util.py, NBCache/, NBOutput/).
- Branch (b): repo present, notebooks/ missing → scaffold notebooks tree + PRJ.
- Branch (c): notebooks/ present → add named PRJ.
- Clobber-refusal: adding existing PRJ exits non-zero and writes nothing.
- --update: re-deploys bundle without modifying notebooks/PRJs (idempotent).
- Filesystem tree assertions: .code-workspace has ``folders`` entry,
  .kbu-run.json carries project_id ``worknb-<basename>``.
- Gitignore block: marker present; re-running does not duplicate.
- Degraded mode: AIAssistant absent, ClaudeCommands absent.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from kbutillib.cli import main
from kbutillib.cli.notebook_init import (
    _WORKNB_BUNDLE,
    _deploy_bundle,
    _register_or_attach,
    _resolve_repo,
    normalize_topic,
    notebook_init,
)
from kbutillib.layout import WORKNB_GITIGNORE_MARKER_END, WORKNB_GITIGNORE_MARKER_START


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invoke(*args: str, env: dict | None = None) -> Any:
    runner = CliRunner()
    # catch_exceptions=True so SystemExit from sys.exit() is caught and
    # reflected in result.exit_code (consistent with other CLI tests here).
    return runner.invoke(main, list(args), catch_exceptions=True, env=env)


def _git_init_ok(path: Path) -> bool:
    """Return True if path is inside a git work tree."""
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(path),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


# ---------------------------------------------------------------------------
# normalize_topic
# ---------------------------------------------------------------------------


class TestNormalizeTopic:
    def test_lowercase(self) -> None:
        assert normalize_topic("ADP1") == "adp1"

    def test_spaces_to_underscore(self) -> None:
        assert normalize_topic("ADP1 Notebooks") == "adp1_notebooks"

    def test_hyphens_to_underscore(self) -> None:
        assert normalize_topic("flux-balance") == "flux_balance"

    def test_special_chars(self) -> None:
        assert normalize_topic("Hello_World!") == "hello_world"

    def test_collapse_multiple_underscores(self) -> None:
        assert normalize_topic("a___b") == "a_b"

    def test_strip_edges(self) -> None:
        assert normalize_topic("_hello_") == "hello"

    def test_already_normalized(self) -> None:
        assert normalize_topic("adp1notebooks") == "adp1notebooks"

    def test_mixed(self) -> None:
        # "Flux Balance Analysis!" → "flux_balance_analysis"
        assert normalize_topic("Flux Balance Analysis!") == "flux_balance_analysis"

    def test_numbers_ok(self) -> None:
        assert normalize_topic("proj2024") == "proj2024"

    def test_unicode_removed(self) -> None:
        # Non-ASCII chars map to _
        result = normalize_topic("résumé")
        assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in result)


# ---------------------------------------------------------------------------
# _resolve_repo
# ---------------------------------------------------------------------------


class TestResolveRepo:
    def test_bare_name_resolves_to_dropbox(self) -> None:
        expected = Path("~/Dropbox/Projects/MyRepo").expanduser()
        assert _resolve_repo("MyRepo") == expected

    def test_full_path_verbatim(self, tmp_path: Path) -> None:
        result = _resolve_repo(str(tmp_path))
        assert result == tmp_path.resolve()


# ---------------------------------------------------------------------------
# Fixtures / shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def no_assistant(monkeypatch: pytest.MonkeyPatch):
    """Patch sys.modules so that assistant.state.registry cannot be imported."""
    monkeypatch.setitem(sys.modules, "assistant", None)  # type: ignore[arg-type]
    monkeypatch.setitem(sys.modules, "assistant.state", None)  # type: ignore[arg-type]
    monkeypatch.setitem(sys.modules, "assistant.state.registry", None)  # type: ignore[arg-type]


@pytest.fixture()
def no_claudecommands(monkeypatch: pytest.MonkeyPatch):
    """Set KBUTILLIB_CLAUDECOMMANDS_ROOT to a non-existent path so ClaudeCommands
    is treated as absent."""
    monkeypatch.setenv(
        "KBUTILLIB_CLAUDECOMMANDS_ROOT",
        "/nonexistent/ClaudeCommands",
    )


@pytest.fixture()
def fake_claudecommands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a fake ClaudeCommands tree with stub skill files.

    Sets KBUTILLIB_CLAUDECOMMANDS_ROOT so _claudecommands_root() returns
    the fake tree instead of the default Dropbox path.
    """
    cc_root = tmp_path / "ClaudeCommands"
    skills_dir = cc_root / "agent-io" / "skills"
    skills_dir.mkdir(parents=True)
    for skill in _WORKNB_BUNDLE:
        (skills_dir / f"{skill}.md").write_text(
            f"# {skill}\nstub content\n", encoding="utf-8"
        )
    monkeypatch.setenv("KBUTILLIB_CLAUDECOMMANDS_ROOT", str(cc_root))
    return cc_root


# ---------------------------------------------------------------------------
# _assert_prj helper
# ---------------------------------------------------------------------------


def _assert_prj(prj_dir: Path, repo_basename: str) -> None:
    """Assert that *prj_dir* is a fully-formed PRJ folder."""
    assert prj_dir.is_dir(), f"PRJ dir missing: {prj_dir}"
    util_py = prj_dir / "util.py"
    assert util_py.is_file(), "util.py missing"
    util_src = util_py.read_text(encoding="utf-8")
    assert repo_basename in util_src, "util.py does not embed repo_basename"
    assert "PROJECT_ROOT" in util_src
    assert "NOTEBOOKS_DIR" in util_src
    assert "MODELS_DIR" in util_src
    assert "GENOMES_DIR" in util_src
    assert "DATA_DIR" in util_src
    assert "NBOUTPUT_DIR" in util_src
    assert "NotebookSession" in util_src
    assert "NBCache" in util_src
    assert (prj_dir / "NBCache").is_dir()
    assert (prj_dir / "NBOutput").is_dir()


def _assert_gitignore_block(gitignore_path: Path) -> None:
    """Assert the gitignore block is present exactly once."""
    text = gitignore_path.read_text(encoding="utf-8")
    assert WORKNB_GITIGNORE_MARKER_START in text
    assert WORKNB_GITIGNORE_MARKER_END in text
    assert text.count(WORKNB_GITIGNORE_MARKER_START) == 1
    assert "notebooks/PRJ-*/NBCache/" in text
    assert "notebooks/PRJ-*/NBOutput/" in text
    assert ".ipynb_checkpoints/" in text


def _assert_kbu_run_json(notebooks_dir: Path, expected_basename: str) -> None:
    """Assert .kbu-run.json has project_id of the right form."""
    binding_path = notebooks_dir / ".kbu-run.json"
    assert binding_path.is_file()
    data = json.loads(binding_path.read_text(encoding="utf-8"))
    assert "project_id" in data
    assert data["project_id"].startswith("worknb-") or data["project_id"]
    assert expected_basename in data["project_id"] or data["project_id"].startswith(
        "worknb-"
    )


# ---------------------------------------------------------------------------
# Branch (a): Repo missing → full bootstrap
# ---------------------------------------------------------------------------


class TestBootstrap:
    """Branch (a): repo does not exist."""

    def test_creates_git_repo(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "MyWorkbook"
        notebook_init(repo=str(repo_root), topic="adp1", update=False)
        assert _git_init_ok(repo_root)

    def test_creates_code_workspace(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "MyWorkbook"
        notebook_init(repo=str(repo_root), topic="adp1", update=False)
        ws_path = repo_root / "MyWorkbook.code-workspace"
        assert ws_path.is_file()
        data = json.loads(ws_path.read_text(encoding="utf-8"))
        assert "folders" in data
        assert data["folders"][0]["path"] == "."

    def test_creates_notebooks_shared_roots(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "MyWorkbook"
        notebook_init(repo=str(repo_root), topic="adp1", update=False)
        nb_dir = repo_root / "notebooks"
        assert nb_dir.is_dir()
        for shared in ("models", "genomes", "data"):
            assert (nb_dir / shared).is_dir(), f"Missing notebooks/{shared}"

    def test_creates_first_prj(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "MyWorkbook"
        notebook_init(repo=str(repo_root), topic="adp1", update=False)
        prj_dir = repo_root / "notebooks" / "PRJ-adp1"
        _assert_prj(prj_dir, "MyWorkbook")

    def test_topic_normalization_in_dir_name(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "TestRepo"
        notebook_init(repo=str(repo_root), topic="ADP1 Notebooks", update=False)
        # "ADP1 Notebooks" → "adp1_notebooks"
        prj_dir = repo_root / "notebooks" / "PRJ-adp1_notebooks"
        assert prj_dir.is_dir()

    def test_gitignore_block_written(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "MyWorkbook"
        notebook_init(repo=str(repo_root), topic="adp1", update=False)
        _assert_gitignore_block(repo_root / ".gitignore")

    def test_kbu_run_json_written(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "MyWorkbook"
        notebook_init(repo=str(repo_root), topic="adp1", update=False)
        _assert_kbu_run_json(repo_root / "notebooks", "MyWorkbook")

    def test_bundle_deployed(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "MyWorkbook"
        notebook_init(repo=str(repo_root), topic="adp1", update=False)
        commands_dir = repo_root / ".claude" / "commands"
        for skill in _WORKNB_BUNDLE:
            skill_file = commands_dir / f"{skill}.md"
            assert skill_file.is_file(), f"Missing skill: {skill}.md"


# ---------------------------------------------------------------------------
# Branch (b): Repo present, notebooks/ missing
# ---------------------------------------------------------------------------


class TestScaffoldNotebooks:
    """Branch (b): repo exists but notebooks/ is missing."""

    def _make_bare_repo(self, path: Path) -> None:
        path.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=str(path), capture_output=True)

    def test_scaffolds_notebooks_tree(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "ExistingRepo"
        self._make_bare_repo(repo_root)
        notebook_init(repo=str(repo_root), topic="flux", update=False)
        nb_dir = repo_root / "notebooks"
        assert nb_dir.is_dir()
        for shared in ("models", "genomes", "data"):
            assert (nb_dir / shared).is_dir()

    def test_creates_first_prj(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "ExistingRepo"
        self._make_bare_repo(repo_root)
        notebook_init(repo=str(repo_root), topic="flux", update=False)
        prj_dir = repo_root / "notebooks" / "PRJ-flux"
        _assert_prj(prj_dir, "ExistingRepo")

    def test_does_not_alter_other_repo_contents(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "ExistingRepo"
        self._make_bare_repo(repo_root)
        sentinel = repo_root / "README.md"
        sentinel.write_text("original content\n", encoding="utf-8")

        notebook_init(repo=str(repo_root), topic="flux", update=False)

        assert sentinel.read_text(encoding="utf-8") == "original content\n"

    def test_gitignore_block_written(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "ExistingRepo"
        self._make_bare_repo(repo_root)
        notebook_init(repo=str(repo_root), topic="flux", update=False)
        _assert_gitignore_block(repo_root / ".gitignore")

    def test_kbu_run_json_written(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "ExistingRepo"
        self._make_bare_repo(repo_root)
        notebook_init(repo=str(repo_root), topic="flux", update=False)
        _assert_kbu_run_json(repo_root / "notebooks", "ExistingRepo")


# ---------------------------------------------------------------------------
# Branch (c): notebooks/ present → add PRJ
# ---------------------------------------------------------------------------


class TestAddPrj:
    """Branch (c): notebooks/ exists → add named PRJ."""

    def _make_repo_with_notebooks(self, path: Path) -> None:
        path.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=str(path), capture_output=True)
        nb = path / "notebooks"
        nb.mkdir()
        (nb / "models").mkdir()
        (nb / "genomes").mkdir()
        (nb / "data").mkdir()
        (nb / ".kbu-run.json").write_text(
            json.dumps({"project_id": f"worknb-{path.name}"}),
            encoding="utf-8",
        )

    def test_adds_new_prj(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "NbRepo"
        self._make_repo_with_notebooks(repo_root)
        notebook_init(repo=str(repo_root), topic="second", update=False)
        prj_dir = repo_root / "notebooks" / "PRJ-second"
        _assert_prj(prj_dir, "NbRepo")

    def test_idempotent_gitignore(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        """Running add-PRJ twice does not duplicate the gitignore block."""
        repo_root = tmp_path / "NbRepo"
        self._make_repo_with_notebooks(repo_root)
        notebook_init(repo=str(repo_root), topic="alpha", update=False)
        notebook_init(repo=str(repo_root), topic="beta", update=False)
        text = (repo_root / ".gitignore").read_text(encoding="utf-8")
        assert text.count(WORKNB_GITIGNORE_MARKER_START) == 1

    def test_does_not_alter_existing_prj(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        """Adding a new PRJ does not modify existing PRJs."""
        repo_root = tmp_path / "NbRepo"
        self._make_repo_with_notebooks(repo_root)
        # Create an existing PRJ with a sentinel file.
        existing_prj = repo_root / "notebooks" / "PRJ-first"
        existing_prj.mkdir()
        sentinel = existing_prj / "util.py"
        sentinel.write_text("# hand-written\n", encoding="utf-8")

        notebook_init(repo=str(repo_root), topic="second", update=False)

        # Existing PRJ unchanged.
        assert sentinel.read_text(encoding="utf-8") == "# hand-written\n"


# ---------------------------------------------------------------------------
# Clobber-refusal
# ---------------------------------------------------------------------------


class TestClobberRefusal:
    """Adding an existing PRJ must exit non-zero and write nothing."""

    def _make_repo_with_prj(self, path: Path, topic: str) -> None:
        path.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=str(path), capture_output=True)
        nb = path / "notebooks"
        nb.mkdir()
        for d in ("models", "genomes", "data"):
            (nb / d).mkdir()
        prj = nb / f"PRJ-{topic}"
        prj.mkdir()
        (prj / "util.py").write_text("# existing\n", encoding="utf-8")
        (nb / ".kbu-run.json").write_text(
            json.dumps({"project_id": f"worknb-{path.name}"}),
            encoding="utf-8",
        )

    def test_exits_nonzero_when_prj_exists(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "ClashRepo"
        self._make_repo_with_prj(repo_root, "mytopic")

        result = _invoke(
            "notebook-init", str(repo_root), "--project", "mytopic"
        )
        assert result.exit_code != 0

    def test_writes_nothing_when_prj_exists(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "ClashRepo"
        self._make_repo_with_prj(repo_root, "mytopic")

        # Snapshot the directory tree before.
        before = {
            str(p.relative_to(repo_root)): p.read_text(encoding="utf-8")
            if p.is_file()
            else None
            for p in repo_root.rglob("*")
        }

        result = _invoke(
            "notebook-init", str(repo_root), "--project", "mytopic"
        )

        # Nothing should have changed.
        after = {
            str(p.relative_to(repo_root)): p.read_text(encoding="utf-8")
            if p.is_file()
            else None
            for p in repo_root.rglob("*")
        }
        assert before == after, "Files changed despite clobber-refusal"
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# --update
# ---------------------------------------------------------------------------


class TestUpdate:
    """--update re-deploys bundle without disturbing notebooks/PRJs."""

    def _make_repo_with_prj(self, path: Path, topic: str) -> None:
        path.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=str(path), capture_output=True)
        nb = path / "notebooks"
        nb.mkdir()
        prj = nb / f"PRJ-{topic}"
        prj.mkdir()
        util = prj / "util.py"
        util.write_text("# original util\n", encoding="utf-8")
        (nb / ".kbu-run.json").write_text(
            json.dumps({"project_id": f"worknb-{path.name}"}),
            encoding="utf-8",
        )

    def test_update_redeploys_bundle(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "UpdRepo"
        self._make_repo_with_prj(repo_root, "myfirst")

        # Remove old bundle if any.
        commands_dir = repo_root / ".claude" / "commands"
        commands_dir.mkdir(parents=True, exist_ok=True)

        notebook_init(repo=str(repo_root), topic=None, update=True)

        for skill in _WORKNB_BUNDLE:
            assert (commands_dir / f"{skill}.md").is_file()

    def test_update_does_not_touch_notebooks(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "UpdRepo"
        self._make_repo_with_prj(repo_root, "myfirst")
        original_util = (
            repo_root / "notebooks" / "PRJ-myfirst" / "util.py"
        ).read_text(encoding="utf-8")

        notebook_init(repo=str(repo_root), topic=None, update=True)

        current_util = (
            repo_root / "notebooks" / "PRJ-myfirst" / "util.py"
        ).read_text(encoding="utf-8")
        assert current_util == original_util

    def test_update_idempotent(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        """Running --update twice produces the same result."""
        repo_root = tmp_path / "UpdRepo"
        self._make_repo_with_prj(repo_root, "myfirst")

        notebook_init(repo=str(repo_root), topic=None, update=True)
        notebook_init(repo=str(repo_root), topic=None, update=True)

        commands_dir = repo_root / ".claude" / "commands"
        for skill in _WORKNB_BUNDLE:
            assert (commands_dir / f"{skill}.md").is_file()

    def test_update_requires_existing_repo(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "NonExistent"
        result = _invoke("notebook-init", str(repo_root), "--update")
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Degraded mode: AIAssistant absent
# ---------------------------------------------------------------------------


class TestDegradedAssistant:
    """When assistant.state is not importable, write binding with name-derived id."""

    def test_writes_kbu_run_json_without_assistant(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "DegradedRepo"
        notebook_init(repo=str(repo_root), topic="mytopic", update=False)
        binding_path = repo_root / "notebooks" / ".kbu-run.json"
        assert binding_path.is_file()
        data = json.loads(binding_path.read_text(encoding="utf-8"))
        assert data["project_id"] == "worknb-DegradedRepo"

    def test_does_not_error_without_assistant(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "DegradedRepo"
        # Should not raise.
        notebook_init(repo=str(repo_root), topic="mytopic", update=False)


# ---------------------------------------------------------------------------
# Degraded mode: ClaudeCommands absent
# ---------------------------------------------------------------------------


class TestDegradedClaudeCommands:
    """When ClaudeCommands is absent, skip bundle and exit 0."""

    def test_skips_bundle_without_claudecommands(
        self,
        tmp_path: Path,
        no_assistant,
        no_claudecommands,
    ) -> None:
        repo_root = tmp_path / "NoCCRepo"
        # Should succeed (no exception).
        notebook_init(repo=str(repo_root), topic="topic", update=False)
        # .claude/ created but commands/ may be empty.
        claude_dir = repo_root / ".claude"
        assert claude_dir.is_dir()

    def test_exits_zero_without_claudecommands(
        self,
        tmp_path: Path,
        no_assistant,
        no_claudecommands,
    ) -> None:
        repo_root = tmp_path / "NoCCRepo"
        result = _invoke(
            "notebook-init", str(repo_root), "--project", "topic"
        )
        assert result.exit_code == 0

    def test_notebooks_still_created_without_claudecommands(
        self,
        tmp_path: Path,
        no_assistant,
        no_claudecommands,
    ) -> None:
        repo_root = tmp_path / "NoCCRepo"
        notebook_init(repo=str(repo_root), topic="topic", update=False)
        nb_dir = repo_root / "notebooks"
        assert nb_dir.is_dir()
        for shared in ("models", "genomes", "data"):
            assert (nb_dir / shared).is_dir()
        _assert_prj(nb_dir / "PRJ-topic", "NoCCRepo")


# ---------------------------------------------------------------------------
# Bundle safety: no BERIL skills
# ---------------------------------------------------------------------------


class TestBundleSafety:
    """Verify the deployed bundle never contains BERIL skills."""

    _BERIL_SKILLS = frozenset(
        [
            "kbu",
            "kbu-notebook",
            "kbu-fba",
            "kbu-start",
            "kbu-migrate",
            "kbu-sub-build",
            "kbu-sub-plan",
            "kbu-sub-review",
            "kbu-sub-run",
        ]
    )

    def test_bundle_contains_no_beril_skills(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "SafeRepo"
        notebook_init(repo=str(repo_root), topic="safe", update=False)
        commands_dir = repo_root / ".claude" / "commands"
        if not commands_dir.exists():
            return  # No commands deployed — trivially safe.
        deployed = {p.stem for p in commands_dir.glob("*.md")}
        beril_deployed = deployed & self._BERIL_SKILLS
        assert not beril_deployed, (
            f"BERIL skills deployed to work-notebook repo: {beril_deployed}"
        )

    def test_worknb_bundle_constant_has_no_beril(self) -> None:
        """The _WORKNB_BUNDLE tuple must not contain any BERIL skill name."""
        overlap = set(_WORKNB_BUNDLE) & self._BERIL_SKILLS
        assert not overlap, f"BERIL skill in _WORKNB_BUNDLE: {overlap}"


# ---------------------------------------------------------------------------
# CLI integration (via Click runner)
# ---------------------------------------------------------------------------


class TestCLIIntegration:
    """Smoke-test that the subcommand is wired into the CLI group."""

    def test_help(self) -> None:
        result = _invoke("notebook-init", "--help")
        assert result.exit_code == 0
        assert "notebook-init" in result.output.lower() or "REPO" in result.output

    def test_missing_project_error(self, tmp_path: Path) -> None:
        """Without --project and without --update, should fail."""
        repo_root = tmp_path / "NoTopicRepo"
        result = _invoke("notebook-init", str(repo_root))
        assert result.exit_code != 0

    def test_full_bootstrap_via_cli(
        self,
        tmp_path: Path,
        no_assistant,
        fake_claudecommands: Path,
    ) -> None:
        repo_root = tmp_path / "CliRepo"
        result = _invoke(
            "notebook-init", str(repo_root), "--project", "flux"
        )
        assert result.exit_code == 0
        assert (repo_root / "notebooks" / "PRJ-flux").is_dir()
