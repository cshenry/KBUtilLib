"""Tests for kbutillib.cli.subproject — state machine, manifest, CLI commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from kbutillib.cli import main
from kbutillib.cli.manifest import (
    now_utc_iso,
    read_subproject_manifest,
    write_subproject_manifest,
)
from kbutillib.cli.subproject import (
    _FORWARD,
    _REVERSE,
    _STATES,
    _parse_verdict,
)


# ── helpers ─────────────────────────────────────────────────────────────────


def _make_project(tmp_path: Path, name: str = "myproj") -> Path:
    """Create a minimal kbu-project.toml in *tmp_path* and return the root."""
    from kbutillib.cli.manifest import write_project_manifest
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    write_project_manifest(root, {
        "project": {"name": name, "title": name, "created_at": now_utc_iso()},
        "kbutillib": {"source_path": "/fake", "source_commit": "abc"},
        "update": {"last_pulled_at": now_utc_iso(), "last_pulled_commit": "abc"},
    })
    return root


def _create_subproject(root: Path, sp_name: str, status: str = "plan") -> Path:
    """Scaffold a subproject directory and write a manifest with the given status."""
    now = now_utc_iso()
    sp_dir = root / "subprojects" / sp_name
    sp_dir.mkdir(parents=True, exist_ok=True)
    (sp_dir / "notebooks").mkdir(exist_ok=True)
    (sp_dir / "sessions").mkdir(exist_ok=True)
    data: dict[str, Any] = {
        "subproject": {
            "name": sp_name,
            "title": sp_name,
            "status": status,
            "created_at": now,
            "last_session_at": now,
        },
        "artifacts": {
            "research_plan": False,
            "report": False,
            "reviews": {"plan": [], "build": [], "synthesis": []},
        },
        "notebooks": [],
        "session_refs": [],
    }
    write_subproject_manifest(root, sp_name, data)
    return sp_dir


def _invoke(root: Path, *args: str) -> Any:
    """Run a ``kbu subproject`` command from the subproject's project root."""
    runner = CliRunner()
    saved = os.getcwd()
    try:
        os.chdir(root)
        return runner.invoke(main, ["subproject", *args], catch_exceptions=False)
    finally:
        os.chdir(saved)


# ── verdict parser ───────────────────────────────────────────────────────────


class TestParseVerdict:
    def test_pass(self, tmp_path: Path) -> None:
        f = tmp_path / "REVIEW_plan_1.md"
        f.write_text("<!-- kbu-review:verdict: pass -->\n# Review\nLooks good.\n")
        assert _parse_verdict(f) == "pass"

    def test_fail(self, tmp_path: Path) -> None:
        f = tmp_path / "REVIEW_plan_1.md"
        f.write_text("<!-- kbu-review:verdict: fail -->\n# Review\nNeeds work.\n")
        assert _parse_verdict(f) == "fail"

    def test_missing_file(self, tmp_path: Path) -> None:
        assert _parse_verdict(tmp_path / "nonexistent.md") is None

    def test_missing_comment(self, tmp_path: Path) -> None:
        f = tmp_path / "REVIEW_plan_1.md"
        f.write_text("# Review\nNo verdict comment.\n")
        assert _parse_verdict(f) is None

    def test_case_insensitive(self, tmp_path: Path) -> None:
        f = tmp_path / "r.md"
        f.write_text("<!-- kbu-review:verdict: PASS -->\n")
        assert _parse_verdict(f) == "pass"

    def test_with_extra_spaces(self, tmp_path: Path) -> None:
        f = tmp_path / "r.md"
        f.write_text("<!--   kbu-review:verdict:   fail   -->\n")
        assert _parse_verdict(f) == "fail"


# ── create subcommand ────────────────────────────────────────────────────────


class TestCreate:
    def test_creates_manifest_with_plan_status(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        result = _invoke(root, "create", "alpha")
        assert result.exit_code == 0, result.output
        data = read_subproject_manifest(root, "alpha")
        assert data["subproject"]["status"] == "plan"

    def test_creates_subproject_toml(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        _invoke(root, "create", "beta")
        assert (root / "subprojects" / "beta" / "kbu-subproject.toml").exists()

    def test_creates_subdirectories(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        _invoke(root, "create", "gamma")
        sp_dir = root / "subprojects" / "gamma"
        assert (sp_dir / "notebooks").is_dir()
        assert (sp_dir / "sessions").is_dir()

    def test_with_title(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        _invoke(root, "create", "delta", "--title", "Delta analysis")
        data = read_subproject_manifest(root, "delta")
        assert data["subproject"]["title"] == "Delta analysis"

    def test_duplicate_fails(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        _invoke(root, "create", "dup")
        result = _invoke(root, "create", "dup")
        assert result.exit_code != 0

    def test_no_artifacts_notebooks_key(self, tmp_path: Path) -> None:
        """Created manifest must not have [artifacts.notebooks]."""
        root = _make_project(tmp_path)
        _invoke(root, "create", "epsilon")
        data = read_subproject_manifest(root, "epsilon")
        assert "notebooks" not in data.get("artifacts", {})


# ── list subcommand ──────────────────────────────────────────────────────────


class TestList:
    def test_tsv_header(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        result = _invoke(root, "list")
        assert result.exit_code == 0, result.output
        assert "name\tstatus\tnext_action" in result.output

    def test_tsv_row_for_existing_subproject(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        _create_subproject(root, "sp1")
        result = _invoke(root, "list")
        assert "sp1" in result.output
        assert "plan" in result.output
        assert "Plan" in result.output

    def test_json_flag(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        _create_subproject(root, "sp1")
        result = _invoke(root, "list", "--json")
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data[0]["name"] == "sp1"
        assert data[0]["status"] == "plan"
        assert "next_action" in data[0]

    def test_empty_project(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        result = _invoke(root, "list")
        assert result.exit_code == 0
        # Only the header row
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 1  # just header
        assert lines[0].startswith("name\t")

    def test_json_empty(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        result = _invoke(root, "list", "--json")
        data = json.loads(result.output)
        assert data == []


# ── status subcommand ────────────────────────────────────────────────────────


class TestStatus:
    def test_shows_state(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        _create_subproject(root, "sp1", status="build")
        result = _invoke(root, "status", "sp1")
        assert result.exit_code == 0, result.output
        assert "build" in result.output

    def test_missing_subproject(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        result = _invoke(root, "status", "nonexistent")
        assert result.exit_code != 0

    def test_json_flag(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        _create_subproject(root, "sp1", status="run")
        result = _invoke(root, "status", "sp1", "--json")
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "run"
        assert data["next_state"] == "synthesize"

    def test_unknown_status_exits_nonzero(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        sp_dir = _create_subproject(root, "sp1")
        # Corrupt the manifest with an unknown status
        data = read_subproject_manifest(root, "sp1")
        data["subproject"]["status"] = "bogus"
        write_subproject_manifest(root, "sp1", data)
        result = _invoke(root, "status", "sp1")
        assert result.exit_code != 0


# ── advance subcommand — forward transitions ─────────────────────────────────


class TestAdvanceForward:
    def _seed_artifacts_for(self, root: Path, sp_name: str, state: str) -> None:
        """Create the artifact files needed to advance FROM *state*."""
        sp_dir = root / "subprojects" / sp_name
        sp_dir.mkdir(parents=True, exist_ok=True)
        (sp_dir / "notebooks").mkdir(exist_ok=True)

        if state in ("plan", "migrate"):
            (sp_dir / "RESEARCH_PLAN.md").write_text("# Plan\n")
        elif state == "p-review":
            f = sp_dir / "REVIEW_plan_1.md"
            f.write_text("<!-- kbu-review:verdict: pass -->\n# Review\n")
        elif state == "build":
            nb_dir = sp_dir / "notebooks"
            (nb_dir / "01_explore.ipynb").write_text("{}")
            (nb_dir / "util.py").write_text("# util\n")
        elif state == "b-review":
            f = sp_dir / "REVIEW_build_1.md"
            f.write_text("<!-- kbu-review:verdict: pass -->\n# Review\n")
        elif state == "run":
            # Seed notebooks data in manifest with last_run_at and modified=false
            data = read_subproject_manifest(root, sp_name)
            data["notebooks"] = [
                {"path": "01.ipynb", "last_run_at": now_utc_iso(), "modified_since_run": False}
            ]
            write_subproject_manifest(root, sp_name, data)
        elif state == "synthesize":
            (sp_dir / "REPORT.md").write_text("# Report\n")
        elif state == "s-review":
            f = sp_dir / "REVIEW_synthesis_1.md"
            f.write_text("<!-- kbu-review:verdict: pass -->\n# Review\n")

    def test_all_forward_transitions(self, tmp_path: Path) -> None:
        """Each state → next state succeeds when artifacts are present."""
        for state, next_state in _FORWARD.items():
            root = _make_project(tmp_path, name=f"proj_{state.replace('-', '_')}")
            _create_subproject(root, "sp", status=state)
            self._seed_artifacts_for(root, "sp", state)
            result = _invoke(root, "advance", "sp")
            assert result.exit_code == 0, (
                f"advance from {state} failed: {result.output}"
            )
            data = read_subproject_manifest(root, "sp")
            assert data["subproject"]["status"] == next_state, (
                f"Expected {next_state}, got {data['subproject']['status']}"
            )

    def test_terminal_state_exits_nonzero(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        _create_subproject(root, "sp", status="complete")
        result = _invoke(root, "advance", "sp")
        assert result.exit_code != 0

    def test_missing_subproject_exits_nonzero(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        result = _invoke(root, "advance", "ghost")
        assert result.exit_code != 0


# ── advance — precondition rejections ───────────────────────────────────────


class TestAdvancePreconditions:
    def test_plan_missing_research_plan(self, tmp_path: Path) -> None:
        """advance plan→p-review without RESEARCH_PLAN.md: missing-artifact."""
        root = _make_project(tmp_path)
        _create_subproject(root, "sp", status="plan")
        # No RESEARCH_PLAN.md
        result = _invoke(root, "advance", "sp")
        assert result.exit_code != 0
        assert "missing-artifact" in result.output

    def test_p_review_no_reviews(self, tmp_path: Path) -> None:
        """advance p-review→build without any REVIEW_plan_*.md: missing-artifact."""
        root = _make_project(tmp_path)
        _create_subproject(root, "sp", status="p-review")
        result = _invoke(root, "advance", "sp")
        assert result.exit_code != 0
        assert "missing-artifact" in result.output

    def test_p_review_fail_verdict(self, tmp_path: Path) -> None:
        """advance p-review→build with only fail verdicts: review-pending."""
        root = _make_project(tmp_path)
        _create_subproject(root, "sp", status="p-review")
        sp_dir = root / "subprojects" / "sp"
        (sp_dir / "REVIEW_plan_1.md").write_text("<!-- kbu-review:verdict: fail -->\n")
        result = _invoke(root, "advance", "sp")
        assert result.exit_code != 0
        assert "review-pending" in result.output

    def test_build_no_notebooks_dir(self, tmp_path: Path) -> None:
        """advance build→b-review without notebooks/: missing-artifact."""
        root = _make_project(tmp_path)
        _create_subproject(root, "sp", status="build")
        # notebooks/ exists but is empty (no .ipynb)
        result = _invoke(root, "advance", "sp")
        assert result.exit_code != 0
        assert "missing-artifact" in result.output

    def test_build_missing_util_py(self, tmp_path: Path) -> None:
        """advance build→b-review with .ipynb but no util.py: missing-artifact."""
        root = _make_project(tmp_path)
        _create_subproject(root, "sp", status="build")
        nb_dir = root / "subprojects" / "sp" / "notebooks"
        nb_dir.mkdir(exist_ok=True)
        (nb_dir / "01_explore.ipynb").write_text("{}")
        # No util.py
        result = _invoke(root, "advance", "sp")
        assert result.exit_code != 0
        assert "missing-artifact" in result.output

    def test_run_stale_notebooks(self, tmp_path: Path) -> None:
        """advance run→synthesize with modified_since_run=true: notebooks-stale."""
        root = _make_project(tmp_path)
        _create_subproject(root, "sp", status="run")
        data = read_subproject_manifest(root, "sp")
        data["notebooks"] = [
            {"path": "01.ipynb", "last_run_at": now_utc_iso(), "modified_since_run": True}
        ]
        write_subproject_manifest(root, "sp", data)
        result = _invoke(root, "advance", "sp")
        assert result.exit_code != 0
        assert "notebooks-stale" in result.output

    def test_run_no_notebooks_registered(self, tmp_path: Path) -> None:
        """advance run→synthesize with empty [[notebooks]]: notebooks-stale."""
        root = _make_project(tmp_path)
        _create_subproject(root, "sp", status="run")
        result = _invoke(root, "advance", "sp")
        assert result.exit_code != 0
        assert "notebooks-stale" in result.output

    def test_synthesize_missing_report(self, tmp_path: Path) -> None:
        """advance synthesize→s-review without REPORT.md: missing-artifact."""
        root = _make_project(tmp_path)
        _create_subproject(root, "sp", status="synthesize")
        result = _invoke(root, "advance", "sp")
        assert result.exit_code != 0
        assert "missing-artifact" in result.output

    def test_s_review_fail_verdict(self, tmp_path: Path) -> None:
        """advance s-review→complete with only fail verdicts: review-pending."""
        root = _make_project(tmp_path)
        _create_subproject(root, "sp", status="s-review")
        sp_dir = root / "subprojects" / "sp"
        (sp_dir / "REVIEW_synthesis_1.md").write_text("<!-- kbu-review:verdict: fail -->\n")
        result = _invoke(root, "advance", "sp")
        assert result.exit_code != 0
        assert "review-pending" in result.output


# ── advance — reverse transitions ────────────────────────────────────────────


class TestAdvanceReverse:
    def test_all_review_reverse(self, tmp_path: Path) -> None:
        """--reverse from each review state moves to the prior action state."""
        for review_state, prior_state in _REVERSE.items():
            root = _make_project(tmp_path, name=f"rev_{review_state.replace('-', '_')}")
            _create_subproject(root, "sp", status=review_state)
            result = _invoke(root, "advance", "sp", "--reverse")
            assert result.exit_code == 0, (
                f"reverse from {review_state} failed: {result.output}"
            )
            data = read_subproject_manifest(root, "sp")
            assert data["subproject"]["status"] == prior_state

    def test_reverse_from_non_review_fails(self, tmp_path: Path) -> None:
        """--reverse from a non-review state should fail."""
        root = _make_project(tmp_path)
        _create_subproject(root, "sp", status="plan")
        result = _invoke(root, "advance", "sp", "--reverse")
        assert result.exit_code != 0

    def test_reverse_no_artifact_check(self, tmp_path: Path) -> None:
        """--reverse must not require any artifacts."""
        root = _make_project(tmp_path)
        _create_subproject(root, "sp", status="p-review")
        # No REVIEW_plan_*.md files — reverse should still succeed
        result = _invoke(root, "advance", "sp", "--reverse")
        assert result.exit_code == 0


# ── set-status subcommand ────────────────────────────────────────────────────


class TestSetStatus:
    def test_bypasses_validation(self, tmp_path: Path) -> None:
        """set-status should jump to any valid state without artifact checks."""
        root = _make_project(tmp_path)
        _create_subproject(root, "sp", status="plan")
        result = _invoke(root, "set-status", "sp", "complete")
        assert result.exit_code == 0, result.output
        data = read_subproject_manifest(root, "sp")
        assert data["subproject"]["status"] == "complete"

    def test_all_valid_states_accepted(self, tmp_path: Path) -> None:
        for state in _STATES:
            root = _make_project(tmp_path, name=f"proj_{state.replace('-', '_')}")
            _create_subproject(root, "sp", status="plan")
            result = _invoke(root, "set-status", "sp", state)
            assert result.exit_code == 0, f"set-status to {state} failed: {result.output}"

    def test_unknown_state_rejected(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        _create_subproject(root, "sp", status="plan")
        result = _invoke(root, "set-status", "sp", "bogus")
        assert result.exit_code != 0

    def test_missing_subproject_fails(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path)
        result = _invoke(root, "set-status", "nonexistent", "plan")
        assert result.exit_code != 0


# ── help / registration ──────────────────────────────────────────────────────


class TestSubprojectHelp:
    def test_subproject_help_lists_commands(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["subproject", "--help"], catch_exceptions=False)
        assert result.exit_code == 0
        for cmd in ("create", "list", "status", "advance", "set-status"):
            assert cmd in result.output

    def test_create_in_empty_temp_project(self, tmp_path: Path) -> None:
        """Regression: kbu subproject create foo in an empty project works."""
        root = _make_project(tmp_path)
        result = _invoke(root, "create", "foo")
        assert result.exit_code == 0, result.output
        assert (root / "subprojects" / "foo" / "kbu-subproject.toml").exists()
        data = read_subproject_manifest(root, "foo")
        assert data["subproject"]["status"] == "plan"

    def test_advance_without_research_plan_stderr_missing_artifact(
        self, tmp_path: Path
    ) -> None:
        """kbu subproject advance foo with no RESEARCH_PLAN.md: non-zero + missing-artifact."""
        root = _make_project(tmp_path)
        _invoke(root, "create", "foo")
        result = _invoke(root, "advance", "foo")
        assert result.exit_code != 0
        assert "missing-artifact" in result.output
